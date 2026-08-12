# -*- coding: utf-8 -*-
"""Isolated full-universe acceptance for official 15m contract statistics.

The live market database is opened read-only only to obtain the latest USDT
linear SWAP universe.  Network results are written to a brand-new isolated
SQLite target and audited there.  Existing files and production databases are
never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import apply_contract_statistics_schema as schema
import collect_market_features as collector


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _universe(source_db: Path, maximum: int) -> list[str]:
    uri = source_db.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=20)
    try:
        latest = con.execute(
            "SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
        if not latest:
            raise ValueError("source market database has no ticker snapshot")
        rows = con.execute(
            "SELECT DISTINCT symbol FROM tick_snapshots "
            "WHERE ts=? AND symbol LIKE '%-USDT-SWAP' ORDER BY symbol",
            (latest,),
        ).fetchall()
        return [str(row[0]) for row in rows[:maximum]]
    finally:
        con.close()


def _contract_values(source_db: Path, symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    uri = source_db.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=20)
    try:
        placeholders = ",".join("?" for _ in symbols)
        return {
            str(row[0]): float(row[1])
            for row in con.execute(
                "SELECT instId,ctVal FROM instruments_cache "
                f"WHERE instId IN ({placeholders})",
                symbols,
            ).fetchall()
            if row[1] is not None and float(row[1]) > 0
        }
    finally:
        con.close()


def _latest_prior_rows(
    source_db: Path,
    symbols: list[str],
    cycle_id: str,
) -> list[tuple]:
    """Read one prior official row per symbol without mutating the source DB."""
    if not symbols:
        return []
    uri = source_db.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=20)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='market_contract_statistics'"
        ).fetchone()
        if not exists:
            return []
        placeholders = ",".join("?" for _ in symbols)
        rows = con.execute(
            "SELECT ts,collected_ts,cycle_id,symbol,timeframe,"
            "       oi_contracts,oi_ccy,oi_usd,taker_sell_usd,"
            "       taker_buy_usd,taker_buy_ratio,raw,source "
            "FROM market_contract_statistics "
            "WHERE source=? AND timeframe='15m' AND cycle_id<>?"
            f"  AND symbol IN ({placeholders}) "
            "ORDER BY symbol,collected_ts DESC,cycle_id DESC",
            (collector.CONTRACT_STATS_SOURCE, cycle_id, *symbols),
        ).fetchall()
        latest_direct: dict[str, tuple] = {}
        for row in rows:
            item = tuple(row)
            symbol = str(item[3])
            if (
                symbol not in latest_direct
                and collector.contract_statistics_row_method(item)
                in collector.CONTRACT_STATS_DIRECT_METHODS
            ):
                latest_direct[symbol] = item
        return [latest_direct[symbol] for symbol in sorted(latest_direct)]
    finally:
        con.close()


def _audit(
    con: sqlite3.Connection,
    universe: list[str],
    cycle_id: str,
) -> dict[str, Any]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM market_contract_statistics WHERE cycle_id=? "
        "AND source=? ORDER BY symbol",
        (cycle_id, collector.CONTRACT_STATS_SOURCE),
    ).fetchall()
    symbols = [str(row["symbol"]) for row in rows]
    expected = set(universe)
    actual = set(symbols)
    duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
    invalid: list[str] = []
    method_counts: dict[str, int] = {}
    methods_by_symbol: dict[str, str] = {}
    direct_symbols: set[str] = set()
    carry_symbols: set[str] = set()
    collected_times = {str(row["collected_ts"]) for row in rows}
    for row in rows:
        try:
            method = str(
                (json.loads(str(row["raw"])) or {}).get("method")
                or "rubik_common_bucket"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            method = "invalid_raw_json"
            invalid.append(f"{row['symbol']}:raw_json")
        method_counts[method] = method_counts.get(method, 0) + 1
        methods_by_symbol[str(row["symbol"])] = method
        if method == collector.CONTRACT_STATS_CARRY_METHOD:
            carry_symbols.add(str(row["symbol"]))
        elif method in {
            "rubik_common_bucket",
            "official_public_oi_trades_candle_reconciled_fallback",
        }:
            direct_symbols.add(str(row["symbol"]))
        else:
            invalid.append(f"{row['symbol']}:unknown_method")
        total = float(row["taker_sell_usd"]) + float(row["taker_buy_usd"])
        ratio = row["taker_buy_ratio"]
        if total > 0:
            expected_ratio = float(row["taker_buy_usd"]) / total
            if ratio is None or abs(float(ratio) - expected_ratio) > 1e-12:
                invalid.append(f"{row['symbol']}:taker_ratio")
        elif ratio is not None:
            invalid.append(f"{row['symbol']}:zero_volume_ratio")
        try:
            source_time = datetime.fromisoformat(
                str(row["ts"]).replace("Z", "+00:00"))
            available_time = datetime.fromisoformat(
                str(row["collected_ts"]).replace("Z", "+00:00"))
            lag_seconds = (available_time - source_time).total_seconds()
            if lag_seconds < 0:
                invalid.append(f"{row['symbol']}:future_source_time")
            elif lag_seconds > collector.CONTRACT_STATS_PRIMARY_MAX_AGE_S:
                invalid.append(f"{row['symbol']}:stale_source_time")
        except ValueError:
            invalid.append(f"{row['symbol']}:invalid_time")
    invalid_symbols = {
        item.split(":", 1)[0] for item in invalid if ":" in item
    }
    valid_actual = (actual & expected) - invalid_symbols
    valid_method_counts: dict[str, int] = {}
    for symbol in valid_actual:
        method = methods_by_symbol[symbol]
        valid_method_counts[method] = valid_method_counts.get(method, 0) + 1
    coverage = len(valid_actual) / len(expected) if expected else 0.0
    direct_coverage = (
        len(direct_symbols & valid_actual) / len(expected) if expected else 0.0)
    carry_coverage = (
        len(carry_symbols & valid_actual) / len(expected) if expected else 0.0)
    checks = {
        "coverage_at_least_99pct": coverage >= 0.99,
        "no_extra_symbols": not (actual - expected),
        "no_duplicate_symbols": not duplicates,
        "valid_values_and_times": not invalid,
        "single_collected_timestamp": len(collected_times) == 1,
        "sqlite_quick_check": con.execute(
            "PRAGMA quick_check").fetchone()[0] == "ok",
    }
    return {
        "status": "PASSED" if all(checks.values()) else "NOT_MET",
        "universe_symbols": len(expected),
        "batch_rows": len(rows),
        "valid_symbols": len(valid_actual),
        "coverage_rate": coverage,
        "direct_valid_symbols": len(direct_symbols & valid_actual),
        "direct_coverage_rate": direct_coverage,
        "carried_forward_valid_symbols": len(carry_symbols & valid_actual),
        "carry_forward_rate": carry_coverage,
        "carry_forward_semantics": (
            "availability continuity within 90m; excluded from model features; "
            "not counted as direct current-batch collection"
        ),
        "missing_symbols": sorted(expected - actual),
        "extra_symbols": sorted(actual - expected),
        "duplicate_symbols": duplicates,
        "invalid_rows": invalid,
        "method_counts": method_counts,
        "valid_method_counts": valid_method_counts,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=r"./db/market.db")
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--max-symbols", type=int, default=500)
    parser.add_argument("--cycle-id")
    args = parser.parse_args(argv)
    cycle_id = args.cycle_id or datetime.now().astimezone().strftime(
        "%Y-%m-%dT%H:%M")
    try:
        collector.contract_statistics_bucket_window_ms(cycle_id)
    except ValueError as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    source_db = Path(args.source_db)
    target_db = Path(args.target_db)
    json_out = Path(args.json_out)
    if not source_db.exists():
        print(json.dumps({"ok": False, "error": "source db missing"}))
        return 2
    if target_db.exists():
        print(json.dumps({
            "ok": False,
            "error": f"isolated target already exists: {target_db}",
        }, ensure_ascii=False))
        return 2
    maximum = max(3, min(int(args.max_symbols), 1000))
    started = time.monotonic()
    try:
        universe = _universe(source_db, maximum)
        contract_values = _contract_values(source_db, universe)
        prior_rows = _latest_prior_rows(source_db, universe, cycle_id)
        target_db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(target_db), timeout=30)
        try:
            con.execute(schema.DDL)
            for statement in schema.INDEXES:
                con.execute(statement)
            seeded_prior_rows = collector.write_contract_statistics_rows(
                con, prior_rows)
            carryable_rows, _ = (
                collector.latest_valid_direct_contract_statistics_rows(
                    con,
                    universe,
                    cycle_id,
                    available_at=(
                        collector.contract_statistics_carry_preflight_time()),
                )
            )
            fetched_rows, errors = collector.fetch_contract_statistics_rows(
                universe,
                cycle_id,
                contract_values=contract_values,
                carryable_symbols=set(carryable_rows),
            )
            completed_rows, completion_quality, carry_errors = (
                collector.complete_contract_statistics_with_previous_batch(
                    con,
                    fetched_rows,
                    universe,
                    cycle_id,
                    available_at=collector.utc_now_iso(),
                )
            )
            errors.extend(carry_errors)
            written_current_rows = collector.write_contract_statistics_rows(
                con, completed_rows)
            con.commit()
            audit = _audit(con, universe, cycle_id)
        finally:
            con.close()
        digest = hashlib.sha256(target_db.read_bytes()).hexdigest()
        payload = {
            "schema_version": 1,
            "artifact_type": "contract_statistics_isolated_acceptance",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "isolated_full_universe_network_acceptance",
            "source_db": str(source_db),
            "source_db_access": (
                "read_only_universe_contract_values_and_latest_prior_rows"),
            "target_db": str(target_db),
            "target_db_sha256": digest,
            "cycle_id": cycle_id,
            "source": collector.CONTRACT_STATS_SOURCE,
            "period": "15m",
            "taker_unit": "USD",
            "seeded_prior_rows": seeded_prior_rows,
            "fetched_current_rows": len(fetched_rows),
            "written_current_rows": written_current_rows,
            "completion_quality": completion_quality,
            "audit": audit,
            "errors": errors,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        _atomic_json(json_out, payload)
        print(json.dumps({
            "ok": audit["status"] == "PASSED",
            "status": audit["status"],
            "coverage": audit["coverage_rate"],
            "direct_coverage": audit["direct_coverage_rate"],
            "carry_forward_rate": audit["carry_forward_rate"],
            "valid": audit["valid_symbols"],
            "universe": audit["universe_symbols"],
            "errors": len(errors),
            "latency_ms": payload["latency_ms"],
            "json_out": str(json_out),
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 0 if audit["status"] == "PASSED" else 1
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
