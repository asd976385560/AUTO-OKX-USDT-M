# -*- coding: utf-8 -*-
"""Bounded current-cycle recovery for incomplete contract statistics.

This entrypoint is intentionally separate from the frozen model bundle.  It is
invoked only by ``collectors/fast_collect.py`` after the normal contract
statistics step fails its strict direct-coverage gate.  The recovery:

* accepts only the current natural 15-minute cycle;
* derives the denominator from that cycle's immutable official instrument
  snapshot;
* requests only symbols that still lack a valid direct row;
* opens one cold process/client wave with at most one request per endpoint and
  symbol, no historical retry and no loop;
* replaces only carry/invalid rows with newly validated direct official rows.

It never reads credentials, calls an Agent or places an order.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _okx_http
import collect_market_features as collector


CST = timezone(timedelta(hours=8))
ROOT = Path(r".")
MINIMUM_DIRECT_RATE = 0.99
RECOVERY_COOLDOWN_SECONDS = 3.0
RECOVERY_BATCH_TIMEOUT_SECONDS = 18.0
RECOVERY_WORKERS_PER_ENDPOINT = 4
EXPECTED_SNAPSHOT_SOURCE = "okx_public_instruments_live_usdt_linear_swap"


def _cycle_for(value: datetime) -> str:
    current = value.astimezone(CST)
    minute = (current.minute // 15) * 15
    return current.replace(
        minute=minute, second=0, microsecond=0
    ).strftime("%Y-%m-%dT%H:%M")


def require_current_natural_cycle(
    cycle_id: str,
    *,
    now: datetime | None = None,
) -> None:
    try:
        parsed = datetime.strptime(str(cycle_id), "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("cycle must use YYYY-MM-DDTHH:MM") from exc
    if parsed.minute not in (0, 15, 30, 45):
        raise ValueError("cycle must align to a natural 15-minute boundary")
    current = now or datetime.now(CST)
    if _cycle_for(current) != cycle_id:
        raise ValueError("historical/future cycle recovery is forbidden")


def _expected_symbols(con: sqlite3.Connection, cycle_id: str) -> list[str]:
    run = con.execute(
        "SELECT symbol_count,complete,source "
        "FROM official_instrument_snapshot_runs WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if run is None:
        raise RuntimeError("official instrument snapshot missing")
    expected_count, complete, source = int(run[0]), int(run[1]), str(run[2])
    if complete != 1 or source != EXPECTED_SNAPSHOT_SOURCE:
        raise RuntimeError("official instrument snapshot contract invalid")
    rows = con.execute(
        "SELECT symbol,state,settle_ccy,ct_type "
        "FROM official_instrument_snapshot_rows WHERE cycle_id=? "
        "ORDER BY symbol",
        (cycle_id,),
    ).fetchall()
    symbols = [str(row[0]) for row in rows]
    if (
        expected_count <= 0
        or len(rows) != expected_count
        or len(symbols) != len(set(symbols))
        or any(
            str(row[1]).lower() != "live"
            or str(row[2]).upper() != "USDT"
            or str(row[3]).lower() != "linear"
            for row in rows
        )
    ):
        raise RuntimeError("official instrument snapshot rows invalid")
    return symbols


def _valid_direct_symbols(
    con: sqlite3.Connection,
    cycle_id: str,
    expected: list[str],
    *,
    available_at: str,
) -> set[str]:
    expected_set = set(expected)
    prior_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT ts,collected_ts,cycle_id,symbol,timeframe,"
            "oi_contracts,oi_ccy,oi_usd,taker_sell_usd,taker_buy_usd,"
            "taker_buy_ratio,raw,source "
            "FROM market_contract_statistics "
            "WHERE cycle_id=? AND timeframe='15m' AND source=?",
            (cycle_id, collector.CONTRACT_STATS_SOURCE),
        ).fetchall()
    finally:
        con.row_factory = prior_factory
    valid: set[str] = set()
    for row in rows:
        symbol = str(row[3])
        if symbol not in expected_set:
            continue
        if collector.contract_statistics_row_method(row) not in (
            collector.CONTRACT_STATS_DIRECT_METHODS
        ):
            continue
        if not collector.contract_statistics_row_issues(
            row,
            available_at=available_at,
            expected_symbol=symbol,
        ):
            valid.add(symbol)
    return valid


def _fetch_once(
    symbols: list[str],
    cycle_id: str,
    *,
    collected_ts: str,
) -> tuple[list[tuple], list[str], dict[str, int]]:
    oi_outcomes: dict[str, dict] = {}
    taker_outcomes: dict[str, dict] = {}
    errors: list[str] = []
    prior_workers = _okx_http._CONTRACT_STATS_WORKERS
    _okx_http._CONTRACT_STATS_WORKERS = min(
        max(1, int(prior_workers)), RECOVERY_WORKERS_PER_ENDPOINT
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            oi_future = executor.submit(
                _okx_http.fetch_contract_open_interest_history_batch_sync,
                symbols,
                "15m",
                8,
                RECOVERY_BATCH_TIMEOUT_SECONDS,
                request_retries=0,
                outcomes=oi_outcomes,
            )
            taker_future = executor.submit(
                _okx_http.fetch_contract_taker_volumes_batch_sync,
                symbols,
                "15m",
                "2",
                8,
                RECOVERY_BATCH_TIMEOUT_SECONDS,
                request_retries=0,
                outcomes=taker_outcomes,
            )
            try:
                open_interest = oi_future.result()
            except Exception as exc:  # noqa: BLE001 - whole-source evidence
                open_interest = {}
                errors.append(
                    f"open_interest:{type(exc).__name__}:{str(exc)[:180]}"
                )
            try:
                taker_volume = taker_future.result()
            except Exception as exc:  # noqa: BLE001 - whole-source evidence
                taker_volume = {}
                errors.append(
                    f"taker_volume:{type(exc).__name__}:{str(exc)[:180]}"
                )
    finally:
        _okx_http._CONTRACT_STATS_WORKERS = prior_workers

    rows: list[tuple] = []
    for symbol in symbols:
        try:
            row = collector.contract_statistics_row(
                open_interest.get(symbol) or [],
                taker_volume.get(symbol) or [],
                cycle_id,
                collected_ts,
                symbol,
            )
            if collector.contract_statistics_row_method(row) not in (
                collector.CONTRACT_STATS_DIRECT_METHODS
            ):
                raise ValueError("recovery row is not direct")
            issues = collector.contract_statistics_row_issues(
                row,
                available_at=collected_ts,
                expected_symbol=symbol,
            )
            if issues:
                raise ValueError(
                    "recovery row validation failed: " + ",".join(issues)
                )
            rows.append(row)
        except Exception as exc:  # noqa: BLE001 - per-symbol fail closed
            if len(errors) < 20:
                errors.append(
                    f"{symbol}:{type(exc).__name__}:{str(exc)[:160]}"
                )
    transport = {
        "open_interest_ok": sum(
            bool(value.get("ok")) for value in oi_outcomes.values()
        ),
        "taker_volume_ok": sum(
            bool(value.get("ok")) for value in taker_outcomes.values()
        ),
        "open_interest_failed": sum(
            not bool(value.get("ok")) for value in oi_outcomes.values()
        ),
        "taker_volume_failed": sum(
            not bool(value.get("ok")) for value in taker_outcomes.values()
        ),
    }
    return rows, errors, transport


def recover(
    db_root: Path,
    cycle_id: str,
    *,
    now: datetime | None = None,
    cooldown_seconds: float = RECOVERY_COOLDOWN_SECONDS,
) -> dict:
    require_current_natural_cycle(cycle_id, now=now)
    if cooldown_seconds < 0 or cooldown_seconds > 5:
        raise ValueError("cooldown_seconds must be between 0 and 5")
    market_db = Path(db_root) / "market.db"
    if not market_db.is_file():
        raise FileNotFoundError(f"market database missing: {market_db}")
    try:
        production_db = (
            market_db.resolve() == (ROOT / "db" / "market.db").resolve()
        )
    except OSError:
        production_db = False

    con = sqlite3.connect(str(market_db), timeout=20)
    con.execute("PRAGMA busy_timeout=15000")
    try:
        expected = _expected_symbols(con, cycle_id)
        initial_at = collector.utc_now_iso()
        initial_direct = _valid_direct_symbols(
            con, cycle_id, expected, available_at=initial_at
        )
        initial_rate = len(initial_direct) / len(expected)
        missing = [symbol for symbol in expected if symbol not in initial_direct]
        if initial_rate >= MINIMUM_DIRECT_RATE:
            return {
                "ok": True,
                "degraded": False,
                "status": "already_complete",
                "cycle": cycle_id,
                "expected_symbols": len(expected),
                "initial_direct_symbols": len(initial_direct),
                "initial_direct_coverage_rate": initial_rate,
                "attempted_symbols": 0,
                "recovered_symbols": 0,
                "remaining_symbols": len(missing),
                "final_direct_symbols": len(initial_direct),
                "final_direct_coverage_rate": initial_rate,
                "wrote": {"contract_statistics_recovery": 0},
                "recovery_contract": {
                    "current_natural_cycle_only": True,
                    "historical_retry": False,
                    "unbounded_retry": False,
                    "recovery_waves": 0,
                    "maximum_requests_per_symbol": 0,
                    "maximum_network_budget_seconds": 0.0,
                },
                "production_database_writes": 0,
                "orders_placed": 0,
            }

        time.sleep(float(cooldown_seconds))
        collected_ts = collector.utc_now_iso()
        rows, errors, transport = _fetch_once(
            missing, cycle_id, collected_ts=collected_ts
        )
        wrote = collector.write_contract_statistics_rows(con, rows)
        con.commit()

        final_at = collector.utc_now_iso()
        final_direct = _valid_direct_symbols(
            con, cycle_id, expected, available_at=final_at
        )
        final_rate = len(final_direct) / len(expected)
        recovered_symbols = len(final_direct - initial_direct)
        passed = final_rate >= MINIMUM_DIRECT_RATE
        return {
            "ok": passed,
            "degraded": not passed,
            "status": "recovered" if passed else "recovery_incomplete",
            "cycle": cycle_id,
            "expected_symbols": len(expected),
            "initial_direct_symbols": len(initial_direct),
            "initial_direct_coverage_rate": initial_rate,
            "attempted_symbols": len(missing),
            "attempted_symbol_values": missing,
            "recovered_symbols": recovered_symbols,
            "remaining_symbols": len(expected) - len(final_direct),
            "final_direct_symbols": len(final_direct),
            "final_direct_coverage_rate": final_rate,
            "minimum_direct_coverage_rate": MINIMUM_DIRECT_RATE,
            "transport": transport,
            "errors": errors[:20],
            "wrote": {"contract_statistics_recovery": wrote},
            "recovery_contract": {
                "current_natural_cycle_only": True,
                "historical_retry": False,
                "unbounded_retry": False,
                "recovery_waves": 1,
                "cooldown_seconds": float(cooldown_seconds),
                "workers_per_endpoint": RECOVERY_WORKERS_PER_ENDPOINT,
                "request_retries_per_endpoint": 0,
                "maximum_requests_per_symbol": 2,
                "maximum_network_budget_seconds": (
                    float(cooldown_seconds)
                    + RECOVERY_BATCH_TIMEOUT_SECONDS
                ),
            },
            "production_database_writes": wrote if production_db else 0,
            "isolated_database_writes": 0 if production_db else wrote,
            "orders_placed": 0,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-root", type=Path, default=ROOT / "db")
    parser.add_argument("--cycle", required=True)
    args = parser.parse_args()
    try:
        result = recover(args.db_root, args.cycle)
    except Exception as exc:  # noqa: BLE001 - structured parent receipt
        result = {
            "ok": False,
            "degraded": True,
            "status": "recovery_error",
            "cycle": args.cycle,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
