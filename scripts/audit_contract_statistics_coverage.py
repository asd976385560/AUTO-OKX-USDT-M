#!/usr/bin/env python3
"""Audit official 15m contract statistics without hiding missing slots.

The latest batch is validated row by row.  When a remediation start is
provided, every expected Beijing-time quarter-hour is also reconstructed;
missing batches remain in the denominator and a 96-slot forward gate cannot
pass early.  SQLite access is read-only and only the explicit JSON artifact is
atomically replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB = Path(r"./db/market.db")
DEFAULT_OUTPUT = Path(
    r"./reports/quality/contract-statistics-coverage-audit.json")
SOURCE = "okx_rest_contract_oi_taker_15m"
MAX_SOURCE_LAG_SECONDS = 5_400
CST = timezone(timedelta(hours=8))
SLOT_MINUTES = 15
DEFAULT_FORWARD_START = "2026-08-12T16:00:00+08:00"
DIRECT_METHODS = {
    "rubik_common_bucket",
    "official_public_oi_trades_candle_reconciled_fallback",
}


def _parse_cst(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def _slot_floor(value: datetime) -> datetime:
    value = value.astimezone(CST)
    return value.replace(
        minute=(value.minute // SLOT_MINUTES) * SLOT_MINUTES,
        second=0,
        microsecond=0,
    )


def _ensure_slot_aligned(value: datetime, label: str) -> None:
    if value != _slot_floor(value):
        raise ValueError(f"{label} must align to a 15-minute CST slot")


def _completed_end_exclusive(as_of: datetime, grace_minutes: int) -> datetime:
    cutoff = as_of.astimezone(CST) - timedelta(minutes=grace_minutes)
    return _slot_floor(cutoff) + timedelta(minutes=SLOT_MINUTES)


def _cycle_id(value: datetime) -> str:
    return value.astimezone(CST).strftime("%Y-%m-%dT%H:%M")


def _expected_cycles(start: datetime, end_exclusive: datetime) -> list[str]:
    _ensure_slot_aligned(start, "window start")
    _ensure_slot_aligned(end_exclusive, "window end")
    if end_exclusive < start:
        raise ValueError("window end must not precede start")
    result: list[str] = []
    current = start
    while current < end_exclusive:
        result.append(_cycle_id(current))
        current += timedelta(minutes=SLOT_MINUTES)
    return result


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


def _ticker_universe(
    db_path: Path,
    *,
    cycle_id: str | None = None,
) -> tuple[str, set[str]]:
    """Load the latest universe, optionally at or just after a cycle boundary."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=20)
    try:
        if cycle_id is None:
            latest_tick = con.execute(
                "SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
        else:
            cycle = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M").replace(
                tzinfo=CST)
            cycle_utc = cycle.astimezone(timezone.utc)
            upper = cycle_utc + timedelta(minutes=SLOT_MINUTES)
            lower = cycle_utc - timedelta(minutes=SLOT_MINUTES)
            latest_tick = con.execute(
                "SELECT MIN(ts) FROM tick_snapshots WHERE ts>=? AND ts<?",
                (
                    cycle_utc.isoformat().replace("+00:00", "Z"),
                    upper.isoformat().replace("+00:00", "Z"),
                ),
            ).fetchone()[0]
            if not latest_tick:
                latest_tick = con.execute(
                    "SELECT MAX(ts) FROM tick_snapshots WHERE ts>=? AND ts<?",
                    (
                        lower.isoformat().replace("+00:00", "Z"),
                        upper.isoformat().replace("+00:00", "Z"),
                    ),
                ).fetchone()[0]
        if not latest_tick:
            raise ValueError("ticker universe missing")
        universe = {
            str(row[0]) for row in con.execute(
                "SELECT DISTINCT symbol FROM tick_snapshots "
                "WHERE ts=? AND symbol LIKE '%-USDT-SWAP'",
                (latest_tick,),
            )
        }
        if not universe:
            raise ValueError("ticker universe empty")
        return str(latest_tick), universe
    finally:
        con.close()


def _latest_ticker_universe(db_path: Path) -> tuple[str, set[str]]:
    """Compatibility wrapper for callers that need the current universe."""
    return _ticker_universe(db_path)


def _summarize_forward_window(
    *,
    statistics_db: Path,
    universe_db: Path,
    start: datetime,
    end_exclusive: datetime,
    minimum_coverage: float,
    maximum_source_lag_seconds: int,
    minimum_slots: int,
) -> dict[str, Any]:
    expected = _expected_cycles(start, end_exclusive)
    rows: list[dict[str, Any]] = []
    observed_slots = 0
    passed_slots = 0
    analysis_ready_slots = 0
    expected_symbol_rows = 0
    valid_symbol_rows = 0
    direct_symbol_rows = 0
    carry_symbol_rows = 0
    fallback_universe_count = len(_latest_ticker_universe(universe_db)[1])
    for expected_cycle in expected:
        try:
            result = audit_contract_statistics(
                statistics_db,
                universe_db_path=universe_db,
                minimum_coverage=minimum_coverage,
                maximum_source_lag_seconds=maximum_source_lag_seconds,
                history_limit=1,
                cycle_id=expected_cycle,
            )
            universe_count = int(result["universe_symbols"])
            batch_rows = int(result["batch_rows"])
            valid = int(result["valid_symbols"])
            direct = int(result["direct_valid_symbols"])
            carried = int(result["carried_forward_valid_symbols"])
            slot_status = str(result["availability_status"])
            analysis_ready_status = str(result["analysis_ready_status"])
            if batch_rows:
                observed_slots += 1
            if slot_status == "PASSED":
                passed_slots += 1
            if analysis_ready_status == "PASSED":
                analysis_ready_slots += 1
            expected_symbol_rows += universe_count
            valid_symbol_rows += valid
            direct_symbol_rows += direct
            carry_symbol_rows += carried
            rows.append({
                "cycle_id": expected_cycle,
                "ticker_ts": result["latest_ticker_ts"],
                "universe_symbols": universe_count,
                "batch_rows": batch_rows,
                "valid_symbols": valid,
                "availability_coverage_rate": float(result["coverage_rate"]),
                "direct_valid_symbols": direct,
                "direct_coverage_rate": float(result["direct_coverage_rate"]),
                "carried_forward_valid_symbols": carried,
                "carry_forward_rate": float(result["carry_forward_rate"]),
                "missing_symbols": len(result["missing_symbols"]),
                "invalid_symbols": len(result["invalid_symbols"]),
                "duplicate_symbols": len(result["duplicate_symbols"]),
                "extra_symbols": len(result["extra_symbols"]),
                "single_collected_timestamp": bool(
                    result["analysis_ready_checks"][
                        "single_collected_timestamp"]),
                "status": slot_status,
                "analysis_ready_status": analysis_ready_status,
            })
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            expected_symbol_rows += fallback_universe_count
            rows.append({
                "cycle_id": expected_cycle,
                "ticker_ts": None,
                "universe_symbols": fallback_universe_count,
                "batch_rows": 0,
                "valid_symbols": 0,
                "availability_coverage_rate": 0.0,
                "direct_valid_symbols": 0,
                "direct_coverage_rate": 0.0,
                "carried_forward_valid_symbols": 0,
                "carry_forward_rate": 0.0,
                "missing_symbols": fallback_universe_count,
                "invalid_symbols": 0,
                "duplicate_symbols": 0,
                "extra_symbols": 0,
                "single_collected_timestamp": False,
                "status": "NOT_MET",
                "analysis_ready_status": "NOT_MET",
                "audit_error": f"{type(exc).__name__}: {exc}",
            })
    expected_slots = len(expected)
    slot_pass_rate = passed_slots / expected_slots if expected_slots else 0.0
    analysis_ready_slot_pass_rate = (
        analysis_ready_slots / expected_slots if expected_slots else 0.0)
    availability_rate = (
        valid_symbol_rows / expected_symbol_rows
        if expected_symbol_rows else 0.0
    )
    direct_rate = (
        direct_symbol_rows / expected_symbol_rows
        if expected_symbol_rows else 0.0
    )
    carry_rate = (
        carry_symbol_rows / expected_symbol_rows
        if expected_symbol_rows else 0.0
    )
    if expected_slots < minimum_slots:
        status = "INSUFFICIENT_EVIDENCE"
    elif (
        slot_pass_rate >= minimum_coverage
        and availability_rate >= minimum_coverage
    ):
        status = "PASSED"
    else:
        status = "NOT_MET"
    if expected_slots < minimum_slots:
        analysis_ready_status = "INSUFFICIENT_EVIDENCE"
    elif (
        direct_rate >= minimum_coverage
        and analysis_ready_slot_pass_rate >= minimum_coverage
    ):
        analysis_ready_status = "PASSED"
    else:
        analysis_ready_status = "NOT_MET"
    return {
        "start_cst": start.isoformat(),
        "end_exclusive_cst": end_exclusive.isoformat(),
        "expected_slots": expected_slots,
        "observed_slots": observed_slots,
        "missing_slots": expected_slots - observed_slots,
        "passed_slots": passed_slots,
        "failed_slots": expected_slots - passed_slots,
        "slot_pass_rate": slot_pass_rate,
        "analysis_ready_slots": analysis_ready_slots,
        "analysis_not_ready_slots": expected_slots - analysis_ready_slots,
        "analysis_ready_slot_pass_rate": analysis_ready_slot_pass_rate,
        "expected_symbol_rows": expected_symbol_rows,
        "valid_symbol_rows": valid_symbol_rows,
        "availability_coverage_rate": availability_rate,
        "direct_valid_symbol_rows": direct_symbol_rows,
        "direct_coverage_rate": direct_rate,
        "carried_forward_valid_symbol_rows": carry_symbol_rows,
        "carry_forward_rate": carry_rate,
        "target_rate": minimum_coverage,
        "minimum_slots": minimum_slots,
        "missing_slot_semantics": "unavailable_and_in_denominator",
        "status": status,
        "analysis_ready_status": analysis_ready_status,
        "slots": rows,
    }


def audit_contract_statistics(
    db_path: Path,
    *,
    universe_db_path: Path | None = None,
    minimum_coverage: float = 0.99,
    maximum_source_lag_seconds: int = MAX_SOURCE_LAG_SECONDS,
    history_limit: int = 8,
    cycle_id: str | None = None,
    forward_start: datetime | None = None,
    as_of: datetime | None = None,
    forward_minimum_slots: int = 96,
    grace_minutes: int = 5,
) -> dict[str, Any]:
    if history_limit < 1:
        raise ValueError("history_limit must be positive")
    if not 0 < minimum_coverage <= 1:
        raise ValueError("minimum_coverage must be in (0,1]")
    if forward_minimum_slots < 1:
        raise ValueError("forward_minimum_slots must be positive")
    if not 0 <= grace_minutes < SLOT_MINUTES:
        raise ValueError("grace_minutes must be in [0,15)")
    universe_source = universe_db_path or db_path
    uri = db_path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        latest_cycle = cycle_id or con.execute(
            "SELECT MAX(cycle_id) FROM market_contract_statistics "
            "WHERE source=?", (SOURCE,),
        ).fetchone()[0]
        if not latest_cycle:
            raise ValueError("contract-statistics batch missing")
        _ensure_slot_aligned(
            _parse_cst(str(latest_cycle)), "contract-statistics cycle")
        latest_tick, universe = _ticker_universe(
            universe_source, cycle_id=str(latest_cycle))
        rows = con.execute(
            "SELECT * FROM market_contract_statistics "
            "WHERE cycle_id=? AND source=? ORDER BY symbol",
            (latest_cycle, SOURCE),
        ).fetchall()
        carry_prior_rows: dict[str, sqlite3.Row | None] = {}
        for row in rows:
            try:
                raw = json.loads(str(row["raw"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict) or raw.get("method") != (
                "official_previous_batch_carry_forward"
            ):
                continue
            prior_cycle = str(raw.get("carried_from_cycle_id") or "")
            carry_prior_rows[str(row["symbol"])] = con.execute(
                "SELECT * FROM market_contract_statistics "
                "WHERE cycle_id=? AND symbol=? AND timeframe='15m' "
                "AND source=? LIMIT 1",
                (prior_cycle, str(row["symbol"]), SOURCE),
            ).fetchone()
        history_headers = con.execute(
            "SELECT cycle_id,MIN(collected_ts) AS first_collected_ts,"
            "MAX(collected_ts) AS last_collected_ts,COUNT(*) AS batch_rows,"
            "COUNT(DISTINCT symbol) AS distinct_symbols "
            "FROM market_contract_statistics WHERE source=? "
            "GROUP BY cycle_id ORDER BY cycle_id DESC LIMIT ?",
            (SOURCE, history_limit),
        ).fetchall()
        recent_batches: list[dict[str, Any]] = []
        for header in history_headers:
            cycle_id = str(header["cycle_id"])
            observed_symbols = {
                str(row[0]) for row in con.execute(
                    "SELECT DISTINCT symbol FROM market_contract_statistics "
                    "WHERE cycle_id=? AND source=?",
                    (cycle_id, SOURCE),
                )
            }
            universe_symbols = observed_symbols & universe
            recent_batches.append({
                "cycle_id": cycle_id,
                "first_collected_ts": header["first_collected_ts"],
                "last_collected_ts": header["last_collected_ts"],
                "batch_rows": int(header["batch_rows"]),
                "distinct_symbols": int(header["distinct_symbols"]),
                "observed_universe_symbols": len(universe_symbols),
                "observed_coverage_rate": (
                    len(universe_symbols) / len(universe) if universe else 0.0
                ),
                "missing_symbols": sorted(universe - observed_symbols),
                "extra_symbols": sorted(observed_symbols - universe),
                "single_collected_timestamp": (
                    header["first_collected_ts"] == header["last_collected_ts"]
                ),
                "evidence_class": (
                    "latest_batch_full_validation"
                    if cycle_id == str(latest_cycle)
                    else "historical_observation_only"
                ),
            })
    finally:
        con.close()

    symbols = [str(row["symbol"]) for row in rows]
    duplicate_symbols = sorted({
        symbol for symbol in symbols if symbols.count(symbol) > 1})
    invalid: dict[str, list[str]] = {}
    valid_symbols: set[str] = set()
    direct_symbols: set[str] = set()
    carry_symbols: set[str] = set()
    method_counts: dict[str, int] = {}
    valid_method_counts: dict[str, int] = {}
    carry_details: dict[str, dict[str, Any]] = {}
    source_lags: list[float] = []
    collected_times = {str(row["collected_ts"]) for row in rows}
    for row in rows:
        symbol = str(row["symbol"])
        reasons: list[str] = []
        lag: float | None = None
        try:
            raw = json.loads(str(row["raw"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = None
            reasons.append("invalid_raw_json")
        method = (
            str(raw.get("method") or "rubik_common_bucket")
            if isinstance(raw, dict) else "invalid_raw_json"
        )
        method_counts[method] = method_counts.get(method, 0) + 1
        numeric = (
            "oi_contracts", "oi_ccy", "oi_usd",
            "taker_sell_usd", "taker_buy_usd",
        )
        values = [float(row[name]) for name in numeric]
        if any(value < 0 for value in values):
            reasons.append("negative_value")
        total = float(row["taker_sell_usd"]) + float(row["taker_buy_usd"])
        ratio = row["taker_buy_ratio"]
        if total > 0:
            expected = float(row["taker_buy_usd"]) / total
            if ratio is None or abs(float(ratio) - expected) > 1e-12:
                reasons.append("taker_ratio_algebra")
        elif ratio is not None:
            reasons.append("zero_volume_ratio")
        try:
            source_time = datetime.fromisoformat(
                str(row["ts"]).replace("Z", "+00:00"))
            available_time = datetime.fromisoformat(
                str(row["collected_ts"]).replace("Z", "+00:00"))
            lag = (available_time - source_time).total_seconds()
            source_lags.append(lag)
            if lag < 0:
                reasons.append("future_source_time")
            elif lag > maximum_source_lag_seconds:
                reasons.append("stale_source_time")
        except ValueError:
            reasons.append("invalid_time")
        if symbol not in universe:
            reasons.append("outside_latest_universe")
        if method == "official_previous_batch_carry_forward":
            carry_symbols.add(symbol)
            required = (
                "semantics", "carried_from_cycle_id",
                "carried_from_collected_ts", "origin_cycle_id",
                "origin_collected_ts", "origin_method", "carry_count",
                "source_age_seconds", "value_contract_sha256",
                "prior_raw_sha256",
            )
            if not isinstance(raw, dict) or any(
                key not in raw for key in required
            ):
                reasons.append("carry_metadata_missing")
            elif (
                "excluded from model features"
                not in str(raw.get("semantics") or "")
            ):
                reasons.append("carry_semantics_invalid")
            else:
                try:
                    declared_age = float(raw["source_age_seconds"])
                    declared_count = int(raw["carry_count"])
                except (TypeError, ValueError):
                    reasons.append("carry_metadata_invalid")
                else:
                    if (
                        not math.isfinite(declared_age)
                        or lag is None
                        or abs(declared_age - lag) > 1.0
                        or declared_count < 1
                    ):
                        reasons.append("carry_metadata_invalid")
                for digest_key in (
                    "value_contract_sha256", "prior_raw_sha256",
                ):
                    digest = str(raw.get(digest_key) or "")
                    if len(digest) != 64 or any(
                        char not in "0123456789abcdef" for char in digest
                    ):
                        reasons.append("carry_digest_invalid")
                        break
                prior = carry_prior_rows.get(symbol)
                if prior is None:
                    reasons.append("carry_prior_row_missing")
                else:
                    try:
                        prior_raw = json.loads(str(prior["raw"]))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        prior_raw = None
                    prior_method = (
                        str(prior_raw.get("method") or "rubik_common_bucket")
                        if isinstance(prior_raw, dict) else "invalid_raw_json"
                    )
                    if prior_method not in DIRECT_METHODS:
                        reasons.append("carry_prior_not_direct")
                    if (
                        str(prior["cycle_id"])
                        != str(raw.get("carried_from_cycle_id"))
                        or str(prior["collected_ts"])
                        != str(raw.get("carried_from_collected_ts"))
                    ):
                        reasons.append("carry_prior_reference_mismatch")
                    prior_digest = hashlib.sha256(
                        str(prior["raw"]).encode("utf-8")
                    ).hexdigest()
                    if prior_digest != str(raw.get("prior_raw_sha256")):
                        reasons.append("carry_prior_raw_digest_mismatch")
                    if (
                        str(raw.get("origin_cycle_id"))
                        != str(prior["cycle_id"])
                        or str(raw.get("origin_collected_ts"))
                        != str(prior["collected_ts"])
                        or str(raw.get("origin_method")) != prior_method
                        or int(raw.get("carry_count") or 0) != 1
                    ):
                        reasons.append("carry_origin_not_direct")
                    value_contract = {
                        "ts": str(row["ts"]),
                        "symbol": symbol,
                        "oi_contracts": float(row["oi_contracts"]),
                        "oi_ccy": float(row["oi_ccy"]),
                        "oi_usd": float(row["oi_usd"]),
                        "taker_sell_usd": float(row["taker_sell_usd"]),
                        "taker_buy_usd": float(row["taker_buy_usd"]),
                        "taker_buy_ratio": (
                            float(row["taker_buy_ratio"])
                            if row["taker_buy_ratio"] is not None else None
                        ),
                    }
                    value_digest = hashlib.sha256(
                        json.dumps(
                            value_contract, sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    if value_digest != str(raw.get("value_contract_sha256")):
                        reasons.append("carry_value_digest_mismatch")
                    numeric_columns = (
                        "oi_contracts", "oi_ccy", "oi_usd",
                        "taker_sell_usd", "taker_buy_usd",
                    )
                    if (
                        str(prior["ts"]) != str(row["ts"])
                        or any(
                            not math.isclose(
                                float(prior[column]), float(row[column]),
                                rel_tol=1e-12, abs_tol=1e-12,
                            )
                            for column in numeric_columns
                        )
                        or (
                            prior["taker_buy_ratio"] is None
                            and row["taker_buy_ratio"] is not None
                        )
                        or (
                            prior["taker_buy_ratio"] is not None
                            and row["taker_buy_ratio"] is None
                        )
                        or (
                            prior["taker_buy_ratio"] is not None
                            and row["taker_buy_ratio"] is not None
                            and not math.isclose(
                                float(prior["taker_buy_ratio"]),
                                float(row["taker_buy_ratio"]),
                                rel_tol=1e-12, abs_tol=1e-12,
                            )
                        )
                    ):
                        reasons.append("carry_values_changed")
                carry_details[symbol] = {
                    "source_age_seconds": raw.get("source_age_seconds"),
                    "carry_count": raw.get("carry_count"),
                    "origin_cycle_id": raw.get("origin_cycle_id"),
                    "origin_method": raw.get("origin_method"),
                }
        elif method in DIRECT_METHODS:
            direct_symbols.add(symbol)
        else:
            reasons.append("unknown_method")
        if reasons:
            invalid[symbol] = reasons
        else:
            valid_symbols.add(symbol)
            valid_method_counts[method] = valid_method_counts.get(method, 0) + 1
    valid_coverage = len(valid_symbols & universe) / len(universe) if universe else 0.0
    direct_coverage = len(direct_symbols & valid_symbols & universe) / len(
        universe) if universe else 0.0
    carry_coverage = len(carry_symbols & valid_symbols & universe) / len(
        universe) if universe else 0.0
    availability_checks = {
        "coverage_at_least_target": valid_coverage >= minimum_coverage,
        "single_collected_timestamp": len(collected_times) == 1,
        "no_duplicates": not duplicate_symbols,
        "no_extra_symbols": not (set(symbols) - universe),
        "universe_nonempty": bool(universe),
    }
    analysis_ready_checks = {
        "direct_coverage_at_least_target": (
            direct_coverage >= minimum_coverage),
        "single_collected_timestamp": len(collected_times) == 1,
        "no_duplicates": not duplicate_symbols,
        "no_extra_symbols": not (set(symbols) - universe),
        "universe_nonempty": bool(universe),
    }
    availability_status = (
        "PASSED" if all(availability_checks.values()) else "NOT_MET")
    analysis_ready_status = (
        "PASSED" if all(analysis_ready_checks.values()) else "NOT_MET")
    payload = {
        "schema_version": 1,
        "artifact_type": "contract_statistics_coverage_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "statistics_db": str(db_path),
        "universe_db": str(universe_source),
        "source": SOURCE,
        "timeframe": "15m",
        "latest_ticker_ts": latest_tick,
        "latest_cycle_id": latest_cycle,
        "collected_ts": next(iter(collected_times)) if len(collected_times) == 1 else None,
        "minimum_coverage": minimum_coverage,
        "maximum_source_lag_seconds": maximum_source_lag_seconds,
        "universe_symbols": len(universe),
        "batch_rows": len(rows),
        "valid_symbols": len(valid_symbols & universe),
        "coverage_rate": valid_coverage,
        "direct_valid_symbols": len(direct_symbols & valid_symbols & universe),
        "direct_coverage_rate": direct_coverage,
        "carried_forward_valid_symbols": len(
            carry_symbols & valid_symbols & universe),
        "carry_forward_rate": carry_coverage,
        "method_counts": method_counts,
        "valid_method_counts": valid_method_counts,
        "carry_forward_details": carry_details,
        "carry_forward_semantics": (
            "availability continuity within 90m; excluded from model features; "
            "not counted as direct current-batch collection"
        ),
        "missing_symbols": sorted(universe - set(symbols)),
        "invalid_symbols": invalid,
        "extra_symbols": sorted(set(symbols) - universe),
        "duplicate_symbols": duplicate_symbols,
        "source_lag_seconds": {
            "min": min(source_lags) if source_lags else None,
            "max": max(source_lags) if source_lags else None,
        },
        "recent_batches": recent_batches,
        "availability_checks": availability_checks,
        "analysis_ready_checks": analysis_ready_checks,
        "checks": analysis_ready_checks,
        "availability_status": availability_status,
        "analysis_ready_status": analysis_ready_status,
        "status": analysis_ready_status,
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    if forward_start is not None:
        start = forward_start.astimezone(CST)
        _ensure_slot_aligned(start, "forward_start")
        effective_as_of = (as_of or datetime.now(CST)).astimezone(CST)
        end_exclusive = _completed_end_exclusive(
            effective_as_of, grace_minutes)
        forward = _summarize_forward_window(
            statistics_db=db_path,
            universe_db=universe_source,
            start=start,
            end_exclusive=max(start, end_exclusive),
            minimum_coverage=minimum_coverage,
            maximum_source_lag_seconds=maximum_source_lag_seconds,
            minimum_slots=forward_minimum_slots,
        )
        payload["as_of_cst"] = effective_as_of.isoformat()
        payload["slot_grace_minutes"] = grace_minutes
        payload["forward_after_remediation"] = forward
        if payload["analysis_ready_status"] != "PASSED":
            payload["overall_status"] = "NOT_MET"
        elif forward["analysis_ready_status"] == "INSUFFICIENT_EVIDENCE":
            payload["overall_status"] = "PENDING_FORWARD_EVIDENCE"
        else:
            payload["overall_status"] = forward["analysis_ready_status"]
    else:
        payload["overall_status"] = payload["status"]
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--universe-db", type=Path,
        help=(
            "optional read-only database supplying the latest ticker universe; "
            "useful for independently auditing an isolated statistics database"
        ),
    )
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-coverage", type=float, default=0.99)
    parser.add_argument(
        "--maximum-source-lag-seconds", type=int,
        default=MAX_SOURCE_LAG_SECONDS,
    )
    parser.add_argument("--history-limit", type=int, default=8)
    parser.add_argument(
        "--forward-start",
        default=DEFAULT_FORWARD_START,
        help=(
            "CST/ISO remediation slot for strict scheduled evidence; default "
            f"{DEFAULT_FORWARD_START}"
        ),
    )
    parser.add_argument("--as-of", help="CST/ISO timestamp; default now")
    parser.add_argument("--forward-minimum-slots", type=int, default=96)
    parser.add_argument("--grace-minutes", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        payload = audit_contract_statistics(
            args.db,
            universe_db_path=args.universe_db,
            minimum_coverage=args.minimum_coverage,
            maximum_source_lag_seconds=args.maximum_source_lag_seconds,
            history_limit=args.history_limit,
            forward_start=(
                _parse_cst(args.forward_start) if args.forward_start else None),
            as_of=_parse_cst(args.as_of) if args.as_of else None,
            forward_minimum_slots=args.forward_minimum_slots,
            grace_minutes=args.grace_minutes,
        )
        _atomic_json(args.json_out, payload)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "ok": payload["overall_status"] != "NOT_MET",
        "status": payload["status"],
        "cycle": payload["latest_cycle_id"],
        "valid": payload["valid_symbols"],
        "universe": payload["universe_symbols"],
        "coverage": payload["coverage_rate"],
        "overall_status": payload["overall_status"],
        "forward_expected_slots": (
            payload.get("forward_after_remediation", {}).get(
                "expected_slots")),
        "json_out": str(args.json_out),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0 if payload["overall_status"] != "NOT_MET" else 1


if __name__ == "__main__":
    raise SystemExit(main())
