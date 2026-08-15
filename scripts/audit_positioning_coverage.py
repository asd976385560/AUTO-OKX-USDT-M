# -*- coding: utf-8 -*-
"""Audit official contract positioning against the live decision universe.

The audit is read-only.  It validates one exact ``collected_ts`` batch from
``market_positioning`` against the latest USDT linear SWAP ticker universe,
including missing/extra symbols, duplicates, ratio algebra and source times.
It separately accumulates an hourly collection-completeness gate and a
quarter-hour decision-availability gate.  Only the explicit JSON evidence
file is atomically replaced.
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

import _acceptance_thresholds as thresholds


CST = timezone(timedelta(hours=8))
DEFAULT_MARKET_DB = Path(r".\db\market.db")
DEFAULT_OUTPUT = Path(
    r".\reports\quality\positioning-coverage-audit.json")
DEFAULT_RECEIPT_ROOT = Path(
    r".\reports\quality\positioning-current")
DEFAULT_SOURCE = "okx_rest_contract_long_short_ratio"
DEFAULT_MAXIMUM_SOURCE_AGE_MINUTES = 90
DEFAULT_FORWARD_START = "2026-08-13T03:00:00+08:00"
DEFAULT_FORWARD_MINIMUM_SLOTS = 24
DEFAULT_AVAILABILITY_FORWARD_START = "2026-08-13T03:00:00+08:00"
DEFAULT_AVAILABILITY_FORWARD_MINIMUM_SLOTS = 96
DEFAULT_RECEIPT_FORWARD_START = "2026-08-13T13:30:00+08:00"
DEFAULT_RECEIPT_FORWARD_MINIMUM_SLOTS = 48
OFFICIAL_SNAPSHOT_SOURCE = "okx_public_instruments_live_usdt_linear_swap"
SLOT_MINUTES = 60
AVAILABILITY_SLOT_MINUTES = 15
RECEIPT_SLOT_MINUTES = 30
POSITIONING_COLLECTION_MINUTES = (0, 30)
POSITIONING_PRIMARY_KEY = ("cycle_id", "symbol", "timeframe", "source")


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def audit_positioning_storage_contract(market_db: Path) -> dict[str, Any]:
    """Prove that later collections cannot replace an earlier cycle by ts."""
    connection = _ro(market_db)
    try:
        info = connection.execute(
            "PRAGMA table_info(market_positioning)").fetchall()
    finally:
        connection.close()
    actual = tuple(
        str(row[1])
        for row in sorted(
            (row for row in info if int(row[5]) > 0),
            key=lambda row: int(row[5]),
        )
    )
    return {
        "expected_primary_key": list(POSITIONING_PRIMARY_KEY),
        "actual_primary_key": list(actual),
        "cross_cycle_upstream_ts_reuse_supported": (
            actual == POSITIONING_PRIMARY_KEY),
        "status": "PASSED" if actual == POSITIONING_PRIMARY_KEY else "NOT_MET",
    }


def _parse_ts(value: str) -> datetime:
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _hour_floor(value: datetime) -> datetime:
    local = value.astimezone(CST)
    return local.replace(minute=0, second=0, microsecond=0)


def _quarter_floor(value: datetime) -> datetime:
    local = value.astimezone(CST)
    return local.replace(
        minute=(local.minute // AVAILABILITY_SLOT_MINUTES)
        * AVAILABILITY_SLOT_MINUTES,
        second=0,
        microsecond=0,
    )


def _expected_hour_slots(
    start: datetime,
    as_of: datetime,
    grace_minutes: int,
) -> list[datetime]:
    if start != _hour_floor(start):
        raise ValueError("forward_start must align to a CST hour")
    cutoff = as_of.astimezone(CST) - timedelta(minutes=grace_minutes)
    end_exclusive = _hour_floor(cutoff) + timedelta(hours=1)
    if end_exclusive <= start:
        return []
    slots: list[datetime] = []
    current = start
    while current < end_exclusive:
        slots.append(current)
        current += timedelta(hours=1)
    return slots


def _expected_quarter_slots(
    start: datetime,
    as_of: datetime,
    grace_minutes: int,
) -> list[datetime]:
    if start != _quarter_floor(start):
        raise ValueError("availability_forward_start must align to a CST quarter hour")
    cutoff = as_of.astimezone(CST) - timedelta(minutes=grace_minutes)
    end_exclusive = _quarter_floor(cutoff) + timedelta(
        minutes=AVAILABILITY_SLOT_MINUTES)
    if end_exclusive <= start:
        return []
    slots: list[datetime] = []
    current = start
    while current < end_exclusive:
        slots.append(current)
        current += timedelta(minutes=AVAILABILITY_SLOT_MINUTES)
    return slots


def _expected_receipt_slots(
    start: datetime,
    as_of: datetime,
    grace_minutes: int,
) -> list[datetime]:
    local = start.astimezone(CST)
    if local.minute not in POSITIONING_COLLECTION_MINUTES or any((
        local.second, local.microsecond,
    )):
        raise ValueError("receipt_forward_start must align to :00/:30")
    cutoff = as_of.astimezone(CST) - timedelta(minutes=grace_minutes)
    slots: list[datetime] = []
    current = local
    while current <= cutoff:
        slots.append(current)
        current += timedelta(minutes=RECEIPT_SLOT_MINUTES)
    return slots


def _receipt_path(root: Path, cycle_id: str) -> Path:
    slug = cycle_id.replace("-", "").replace(":", "")
    return root / cycle_id[:10] / f"positioning-{slug}.json"


def _symbol_list_hash(symbols: list[str]) -> str:
    encoded = json.dumps(
        symbols, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _receipt_slot_evidence(
    connection: sqlite3.Connection,
    *,
    slot: datetime,
    receipt_root: Path,
    source: str,
    target_rate: float,
    maximum_source_age_minutes: float,
) -> dict[str, Any]:
    cycle_id = slot.strftime("%Y-%m-%dT%H:%M")
    path = _receipt_path(receipt_root, cycle_id)
    reasons: list[str] = []
    payload: dict[str, Any] = {}
    file_sha256: str | None = None
    if not path.is_file():
        reasons.append("receipt_missing")
    else:
        try:
            raw = path.read_bytes()
            file_sha256 = hashlib.sha256(raw).hexdigest()
            loaded = json.loads(raw.decode("utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("receipt root must be an object")
            payload = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            reasons.append("receipt_invalid_json")

    selected = payload.get("selected") if isinstance(payload, dict) else None
    if not isinstance(selected, list) or any(
        not isinstance(symbol, str) or not symbol for symbol in selected
    ):
        selected_symbols: list[str] = []
        reasons.append("selected_symbols_invalid")
    else:
        selected_symbols = list(selected)
    selected_set = set(selected_symbols)
    if len(selected_set) != len(selected_symbols):
        reasons.append("selected_symbols_duplicate")

    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "official_instrument_snapshot_rows" not in tables:
        official_set: set[str] = set()
        reasons.append("official_snapshot_rows_table_missing")
    else:
        official_set = {
            str(row[0]) for row in connection.execute(
                "SELECT symbol FROM official_instrument_snapshot_rows "
                "WHERE cycle_id=?", (cycle_id,),
            ).fetchall()
        }
        if not official_set:
            reasons.append("official_snapshot_universe_missing")
        elif selected_set != official_set:
            reasons.append("selected_universe_mismatch")

    rows = connection.execute(
        "SELECT ts,collected_ts,symbol,long_ratio,short_ratio,"
        "long_short_ratio,source FROM market_positioning "
        "WHERE cycle_id=? AND timeframe='1H' AND source=? ORDER BY symbol",
        (cycle_id, source),
    ).fetchall()
    counts: dict[str, int] = {}
    invalid_symbols: set[str] = set()
    collected_values: set[str] = set()
    for row in rows:
        symbol = str(row["symbol"])
        counts[symbol] = counts.get(symbol, 0) + 1
        collected_values.add(str(row["collected_ts"]))
        row_reasons = _ratio_errors(row)
        try:
            source_at = _parse_ts(row["ts"])
            collected_at = _parse_ts(row["collected_ts"])
            age = (collected_at - source_at).total_seconds() / 60.0
            if age < -1:
                row_reasons.append("source_ts_after_availability")
            elif age > maximum_source_age_minutes:
                row_reasons.append("source_ts_stale_at_availability")
        except (TypeError, ValueError):
            row_reasons.append("source_or_availability_ts_invalid")
        if row_reasons:
            invalid_symbols.add(symbol)
    observed_set = set(counts)
    duplicate = {symbol for symbol, count in counts.items() if count != 1}
    final_failed = selected_set - observed_set
    extra = observed_set - selected_set
    valid_set = (
        (selected_set & observed_set) - invalid_symbols - duplicate
    )
    denominator = len(selected_set)
    coverage = len(valid_set) / denominator if denominator else 0.0
    if duplicate:
        reasons.append("database_duplicate_symbols")
    if extra:
        reasons.append("database_extra_symbols")
    if invalid_symbols:
        reasons.append("database_invalid_rows")
    if len(collected_values) != 1:
        reasons.append("database_availability_not_uniform")

    def _integer(container: dict, key: str) -> int | None:
        value = container.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    if payload:
        if payload.get("schema_version") != 1:
            reasons.append("schema_version_invalid")
        if payload.get("artifact_type") != (
            "current_natural_positioning_collection_receipt"
        ):
            reasons.append("artifact_type_invalid")
        if payload.get("cycle") != cycle_id:
            reasons.append("receipt_cycle_mismatch")
        if payload.get("natural_current_cycle_guard") is not True:
            reasons.append("current_cycle_guard_not_proven")
        if payload.get("historical_backfill_allowed") is not False:
            reasons.append("historical_backfill_not_forbidden")
        safety = payload.get("safety")
        if not isinstance(safety, dict) or any((
            safety.get("natural_current_cycle_only") is not True,
            safety.get("historical_backfill_allowed") is not False,
            safety.get("production_model_mutation") is not False,
            safety.get("production_threshold_mutation") is not False,
            safety.get("orders_placed") != 0,
        )):
            reasons.append("safety_contract_invalid")
        if _integer(payload, "selected_count") != denominator:
            reasons.append("selected_count_mismatch")
        if payload.get("selected_symbols_sha256") != _symbol_list_hash(
            selected_symbols
        ):
            reasons.append("selected_symbols_sha256_mismatch")
        wrote = payload.get("wrote")
        if not isinstance(wrote, dict) or _integer(wrote, "positioning") != len(rows):
            reasons.append("written_row_count_mismatch")
        try:
            claimed_coverage = float(payload.get("positioning_coverage_rate"))
        except (TypeError, ValueError):
            claimed_coverage = -1.0
        actual_row_coverage = len(observed_set & selected_set) / denominator if denominator else 0.0
        if abs(claimed_coverage - actual_row_coverage) > 1e-12:
            reasons.append("claimed_coverage_mismatch")

        retry = payload.get("retry")
        if not isinstance(retry, dict):
            reasons.append("retry_contract_missing")
        else:
            initial_invalid = retry.get("initial_invalid_symbol_values")
            retry_requested = retry.get("retry_requested_symbol_values")
            recovered = retry.get("retry_recovered_symbol_values")
            claimed_failed = retry.get("final_failed_symbol_values")
            lists_valid = all(isinstance(value, list) for value in (
                initial_invalid, retry_requested, recovered, claimed_failed,
            ))
            if not lists_valid:
                reasons.append("retry_symbol_sets_invalid")
            else:
                invalid_set = set(initial_invalid)
                requested_set = set(retry_requested)
                recovered_set = set(recovered)
                claimed_failed_set = set(claimed_failed)
                if invalid_set != requested_set:
                    reasons.append("retry_not_exact_initial_failure_set")
                if not requested_set.issubset(selected_set):
                    reasons.append("retry_requested_outside_universe")
                if not recovered_set.issubset(requested_set & observed_set):
                    reasons.append("retry_recovered_set_invalid")
                if claimed_failed_set != final_failed:
                    reasons.append("final_failed_set_mismatch")
                count_contract = (
                    _integer(retry, "initial_requested_symbols") == denominator
                    and _integer(retry, "initial_invalid_symbols") == len(invalid_set)
                    and _integer(retry, "initial_valid_symbols")
                    == denominator - len(invalid_set)
                    and _integer(retry, "retry_requested_symbols")
                    == len(requested_set)
                    and _integer(retry, "retry_recovered_symbols")
                    == len(recovered_set)
                    and _integer(retry, "final_failed_symbols")
                    == len(claimed_failed_set)
                )
                if not count_contract:
                    reasons.append("retry_count_contract_invalid")
            retry_version = retry.get("retry_contract_version", 1)
            if retry_version == 1:
                if retry.get("retry_attempts_per_symbol") != 1:
                    reasons.append("retry_safety_contract_invalid")
            elif retry_version == 2:
                waves = retry.get("retry_waves")
                if not isinstance(waves, list):
                    reasons.append("retry_waves_missing")
                    waves = []
                if len(waves) > 2:
                    reasons.append("retry_wave_limit_exceeded")
                remaining_set = set(initial_invalid or [])
                all_recovered: set[str] = set()
                for index, wave in enumerate(waves, start=1):
                    if not isinstance(wave, dict):
                        reasons.append("retry_wave_invalid")
                        continue
                    requested_values = wave.get("requested_symbol_values")
                    recovered_values = wave.get("recovered_symbol_values")
                    remaining_values = wave.get("remaining_symbol_values")
                    if not all(isinstance(value, list) for value in (
                        requested_values, recovered_values, remaining_values,
                    )):
                        reasons.append("retry_wave_symbol_sets_invalid")
                        continue
                    wave_requested = set(requested_values)
                    wave_recovered = set(recovered_values)
                    wave_remaining = set(remaining_values)
                    if wave.get("wave") != index:
                        reasons.append("retry_wave_order_invalid")
                    if wave_requested != remaining_set:
                        reasons.append("retry_wave_not_exact_remaining_set")
                    if not wave_recovered.issubset(
                        wave_requested & observed_set
                    ):
                        reasons.append("retry_wave_recovered_set_invalid")
                    if wave_recovered & all_recovered:
                        reasons.append("retry_wave_recovered_duplicate")
                    expected_remaining = wave_requested - wave_recovered
                    if wave_remaining != expected_remaining:
                        reasons.append("retry_wave_remaining_set_invalid")
                    if any((
                        _integer(wave, "requested_symbols")
                        != len(wave_requested),
                        _integer(wave, "recovered_symbols")
                        != len(wave_recovered),
                        _integer(wave, "remaining_symbols")
                        != len(wave_remaining),
                        wave.get("request_retries_per_symbol") != 0,
                    )):
                        reasons.append("retry_wave_count_contract_invalid")
                    all_recovered.update(wave_recovered)
                    remaining_set = wave_remaining
                if set(recovered or []) != all_recovered:
                    reasons.append("retry_wave_aggregate_recovered_mismatch")
                if set(claimed_failed or []) != remaining_set:
                    reasons.append("retry_wave_final_failed_mismatch")
                if any((
                    retry.get("retry_wave_count") != len(waves),
                    retry.get("retry_attempts_per_symbol") != 2,
                    retry.get("retry_max_attempts_per_symbol") != 2,
                    float(retry.get("maximum_network_budget_seconds") or 0)
                    > 54.0,
                    _integer(retry, "maximum_official_requests_per_symbol")
                    is None,
                    _integer(retry, "maximum_official_requests_per_symbol")
                    > 4,
                )):
                    reasons.append("retry_safety_contract_invalid")
            else:
                reasons.append("retry_contract_version_invalid")
            if any((
                retry.get("unbounded_retry") is not False,
                retry.get("historical_retry") is not False,
            )):
                reasons.append("retry_safety_contract_invalid")
            shared_available = retry.get("shared_available_at_utc")
            if len(collected_values) != 1 or shared_available not in collected_values:
                reasons.append("shared_availability_mismatch")

        expected_pass = coverage >= target_rate and not reasons
        if payload.get("status") != ("PASSED" if expected_pass else "NOT_MET"):
            reasons.append("receipt_status_mismatch")
        if payload.get("ok") is not expected_pass:
            reasons.append("receipt_ok_mismatch")

    passed = bool(payload) and not reasons and coverage >= target_rate
    return {
        "cycle_id": cycle_id,
        "receipt_path": str(path),
        "receipt_sha256": file_sha256,
        "receipt_present": path.is_file(),
        "expected_symbols": len(official_set or selected_set),
        "selected_symbols": denominator,
        "database_rows": len(rows),
        "valid_symbols": len(valid_set),
        "coverage_rate": round(coverage, 6),
        "initial_invalid_symbols": (
            (payload.get("retry") or {}).get("initial_invalid_symbols")
            if isinstance(payload.get("retry"), dict) else None
        ),
        "retry_recovered_symbols": (
            (payload.get("retry") or {}).get("retry_recovered_symbols")
            if isinstance(payload.get("retry"), dict) else None
        ),
        "final_failed_symbols": sorted(final_failed),
        "reasons": sorted(set(reasons)),
        "status": "PASSED" if passed else "NOT_MET",
    }


def audit_positioning_collection_receipts(
    market_db: Path,
    receipt_root: Path,
    *,
    as_of: datetime,
    forward_start: datetime,
    minimum_slots: int = DEFAULT_RECEIPT_FORWARD_MINIMUM_SLOTS,
    target_rate: float | None = None,
    source: str = DEFAULT_SOURCE,
    maximum_source_age_minutes: float = DEFAULT_MAXIMUM_SOURCE_AGE_MINUTES,
    grace_minutes: int = 5,
) -> dict[str, Any]:
    """独立核对有界重试收据、官方宇宙与最终数据库批次。"""
    if minimum_slots <= 0:
        raise ValueError("receipt_minimum_slots must be positive")
    # None = 按预注册激活边界解析（边界前 0.99、边界起 0.95）。
    if target_rate is None:
        target_rate = thresholds.coverage_target_rate(as_of)
    if not 0 < target_rate <= 1:
        raise ValueError("target_rate must be in (0,1]")
    if maximum_source_age_minutes <= 0:
        raise ValueError("maximum_source_age_minutes must be positive")
    if not 0 <= grace_minutes < RECEIPT_SLOT_MINUTES:
        raise ValueError("receipt_grace_minutes must be in [0,30)")
    slots = _expected_receipt_slots(
        forward_start.astimezone(CST), as_of.astimezone(CST), grace_minutes)
    connection = _ro(market_db)
    try:
        rows = [
            _receipt_slot_evidence(
                connection,
                slot=slot,
                receipt_root=receipt_root,
                source=source,
                target_rate=target_rate,
                maximum_source_age_minutes=maximum_source_age_minutes,
            )
            for slot in slots
        ]
    finally:
        connection.close()
    expected_slots = len(rows)
    passed_slots = sum(row["status"] == "PASSED" for row in rows)
    present_slots = sum(bool(row["receipt_present"]) for row in rows)
    expected_symbols = sum(int(row["expected_symbols"]) for row in rows)
    valid_symbols = sum(int(row["valid_symbols"]) for row in rows)
    slot_rate = passed_slots / expected_slots if expected_slots else 0.0
    present_rate = present_slots / expected_slots if expected_slots else 0.0
    symbol_rate = valid_symbols / expected_symbols if expected_symbols else 0.0
    requirements = {
        "minimum_slots_met": expected_slots >= minimum_slots,
        "receipt_presence_rate_at_least_target": present_rate >= target_rate,
        "receipt_slot_pass_rate_at_least_target": slot_rate >= target_rate,
        "symbol_coverage_rate_at_least_target": symbol_rate >= target_rate,
    }
    observed_quality = all(
        value for key, value in requirements.items()
        if key != "minimum_slots_met"
    )
    if expected_slots > 0 and not observed_quality:
        status = "NOT_MET"
    elif not requirements["minimum_slots_met"]:
        status = "INSUFFICIENT_EVIDENCE"
    elif all(requirements.values()):
        status = "PASSED"
    else:
        status = "NOT_MET"
    return {
        "start_cst": forward_start.astimezone(CST).isoformat(),
        "as_of_cst": as_of.astimezone(CST).isoformat(),
        "schedule_minutes": RECEIPT_SLOT_MINUTES,
        "slot_grace_minutes": grace_minutes,
        "minimum_slots": minimum_slots,
        "target_rate": target_rate,
        "target_rate_migration": thresholds.coverage_migration_facts(as_of),
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics(
            {
                "receipt_presence_rate": present_rate,
                "symbol_coverage_rate": symbol_rate,
                "slot_pass_rate": slot_rate,
            },
            target_dependent=("slot_pass_rate",),
        ),
        "expected_slots": expected_slots,
        "receipt_present_slots": present_slots,
        "passed_slots": passed_slots,
        "receipt_presence_rate": round(present_rate, 6),
        "slot_pass_rate": round(slot_rate, 6),
        "expected_symbol_rows": expected_symbols,
        "valid_symbol_rows": valid_symbols,
        "symbol_coverage_rate": round(symbol_rate, 6),
        "requirements": requirements,
        "status": status,
        "slots": rows,
    }


def _canonical_official_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "symbol": str(row["symbol"] or "").strip().upper(),
        "list_time_utc": row["list_time_utc"],
        "state": row["state"],
        "settle_ccy": row["settle_ccy"],
        "ct_type": row["ct_type"],
        "inst_category": row["inst_category"],
        "ct_val": row["ct_val"],
        "lot_sz": row["lot_sz"],
    }


def _official_hash(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _official_snapshot_for_cycle(
    connection: sqlite3.Connection,
    cycle_id: str,
    slot_utc: datetime,
    slot_width: timedelta = timedelta(hours=1),
) -> tuple[set[str], dict[str, Any]]:
    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {
        "official_instrument_snapshot_runs",
        "official_instrument_snapshot_rows",
    }
    if not required.issubset(tables):
        return set(), {
            "status": "MISSING",
            "reasons": ["official_snapshot_tables_missing"],
            "observed_row_count": 0,
            "valid_metadata_rows": 0,
            "metadata_coverage_rate": 0.0,
        }
    header = connection.execute(
        "SELECT collected_ts_utc,symbol_count,payload_sha256,complete,source "
        "FROM official_instrument_snapshot_runs WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if header is None:
        return set(), {
            "status": "MISSING",
            "reasons": ["official_snapshot_header_missing"],
            "observed_row_count": 0,
            "valid_metadata_rows": 0,
            "metadata_coverage_rate": 0.0,
        }
    raw_rows = connection.execute(
        "SELECT symbol,list_time_utc,state,settle_ccy,ct_type,inst_category,"
        "ct_val,lot_sz FROM official_instrument_snapshot_rows "
        "WHERE cycle_id=? ORDER BY symbol",
        (cycle_id,),
    ).fetchall()
    rows = [_canonical_official_row(row) for row in raw_rows]
    observed_hash = _official_hash(rows)
    reasons: list[str] = []
    header_count = int(header["symbol_count"])
    if int(header["complete"]) != 1:
        reasons.append("header_not_complete")
    if header_count != len(rows):
        reasons.append("header_row_count_mismatch")
    if str(header["payload_sha256"]) != observed_hash:
        reasons.append("payload_sha256_mismatch")
    if str(header["source"]) != OFFICIAL_SNAPSHOT_SOURCE:
        reasons.append("official_snapshot_source_invalid")
    try:
        collected = _parse_ts(header["collected_ts_utc"])
        if not slot_utc <= collected < slot_utc + slot_width:
            reasons.append("collected_ts_outside_slot")
    except (TypeError, ValueError):
        reasons.append("collected_ts_invalid")
    universe: set[str] = set()
    valid_metadata = 0
    invalid_examples: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row["symbol"])
        row_reasons: list[str] = []
        if not symbol.endswith("-USDT-SWAP"):
            row_reasons.append("symbol_not_usdt_swap")
        else:
            universe.add(symbol)
        if str(row["state"] or "").lower() != "live":
            row_reasons.append("state_not_live")
        if str(row["settle_ccy"] or "").upper() != "USDT":
            row_reasons.append("settle_ccy_not_usdt")
        if str(row["ct_type"] or "").lower() != "linear":
            row_reasons.append("ct_type_not_linear")
        if not str(row["inst_category"] or "").strip():
            row_reasons.append("inst_category_missing")
        for field in ("ct_val", "lot_sz"):
            try:
                valid_value = math.isfinite(float(row[field])) and float(row[field]) > 0
            except (TypeError, ValueError):
                valid_value = False
            if not valid_value:
                row_reasons.append(f"{field}_invalid")
        try:
            if _parse_ts(row["list_time_utc"]) > slot_utc + slot_width:
                row_reasons.append("list_time_after_slot")
        except (TypeError, ValueError):
            row_reasons.append("list_time_invalid")
        if row_reasons:
            if len(invalid_examples) < 20:
                invalid_examples.append({
                    "symbol": symbol, "reasons": row_reasons})
        else:
            valid_metadata += 1
    if not rows:
        reasons.append("official_snapshot_empty")
    metadata_rate = valid_metadata / len(rows) if rows else 0.0
    return universe, {
        "status": "PASSED" if not reasons else "NOT_MET",
        "reasons": reasons,
        "collected_ts_utc": header["collected_ts_utc"],
        "source": header["source"],
        "header_symbol_count": header_count,
        "observed_row_count": len(rows),
        "valid_metadata_rows": valid_metadata,
        "metadata_coverage_rate": round(metadata_rate, 6),
        "stored_payload_sha256": header["payload_sha256"],
        "observed_payload_sha256": observed_hash,
        "invalid_metadata_examples": invalid_examples,
    }


def _ratio_errors(row: sqlite3.Row) -> list[str]:
    errors: list[str] = []
    values: dict[str, float] = {}
    for name in ("long_ratio", "short_ratio", "long_short_ratio"):
        try:
            value = float(row[name])
        except (TypeError, ValueError):
            errors.append(f"{name}_missing_or_non_numeric")
            continue
        if not math.isfinite(value):
            errors.append(f"{name}_non_finite")
            continue
        values[name] = value
    if len(values) != 3:
        return errors
    if not 0 <= values["long_ratio"] <= 1:
        errors.append("long_ratio_out_of_range")
    if not 0 <= values["short_ratio"] <= 1:
        errors.append("short_ratio_out_of_range")
    if values["long_short_ratio"] < 0:
        errors.append("long_short_ratio_negative")
    if abs(values["long_ratio"] + values["short_ratio"] - 1.0) > 1e-6:
        errors.append("account_shares_do_not_sum_to_one")
    if values["short_ratio"] > 0:
        derived = values["long_ratio"] / values["short_ratio"]
        tolerance = max(1e-6, abs(values["long_short_ratio"]) * 1e-6)
        if abs(derived - values["long_short_ratio"]) > tolerance:
            errors.append("long_short_ratio_derivation_mismatch")
    return errors


def audit_positioning_coverage(
    market_db: Path,
    *,
    minimum_rate: float | None = None,
    source: str = DEFAULT_SOURCE,
    now: datetime | None = None,
    maximum_source_age_minutes: float = DEFAULT_MAXIMUM_SOURCE_AGE_MINUTES,
) -> dict[str, Any]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # None = 按预注册激活边界解析（边界前 0.99、边界起 0.95）。
    if minimum_rate is None:
        minimum_rate = thresholds.coverage_target_rate(now_utc)
    if not 0 < minimum_rate <= 1:
        raise ValueError("minimum_rate must be in (0,1]")
    if maximum_source_age_minutes <= 0:
        raise ValueError("maximum_source_age_minutes must be positive")
    connection = _ro(market_db)
    try:
        latest_tick = connection.execute(
            "SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
        universe_rows = connection.execute(
            "SELECT DISTINCT symbol FROM tick_snapshots "
            "WHERE ts=? AND symbol LIKE '%-USDT-SWAP' ORDER BY symbol",
            (latest_tick,),
        ).fetchall()
        universe = {str(row[0]) for row in universe_rows}
        latest_collected = connection.execute(
            "SELECT MAX(collected_ts) FROM market_positioning "
            "WHERE source=? AND timeframe='1H'",
            (source,),
        ).fetchone()[0]
        rows = [] if latest_collected is None else connection.execute(
            "SELECT ts,collected_ts,cycle_id,symbol,timeframe,long_ratio,"
            "short_ratio,long_short_ratio,source FROM market_positioning "
            "WHERE source=? AND timeframe='1H' AND collected_ts=? "
            "ORDER BY symbol",
            (source, latest_collected),
        ).fetchall()
    finally:
        connection.close()

    symbol_counts: dict[str, int] = {}
    invalid: list[dict[str, Any]] = []
    source_times: list[datetime] = []
    for row in rows:
        symbol = str(row["symbol"])
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        row_errors = _ratio_errors(row)
        try:
            source_time = _parse_ts(str(row["ts"]))
            source_times.append(source_time)
            age_minutes = (now_utc - source_time).total_seconds() / 60.0
            if age_minutes < -1:
                row_errors.append("source_ts_in_future")
            elif age_minutes > maximum_source_age_minutes:
                row_errors.append("source_ts_stale")
        except (TypeError, ValueError):
            row_errors.append("source_ts_invalid")
        if row_errors:
            invalid.append({"symbol": symbol, "errors": row_errors})

    observed = set(symbol_counts)
    duplicate_symbols = sorted(
        symbol for symbol, count in symbol_counts.items() if count != 1)
    invalid_symbols = {item["symbol"] for item in invalid}
    missing = sorted(universe - observed)
    extra = sorted(observed - universe)
    valid_symbols = (
        (universe & observed) - invalid_symbols - set(duplicate_symbols)
    )
    denominator = len(universe)
    coverage_rate = len(valid_symbols) / denominator if denominator else 0.0
    if latest_collected is None:
        status = "NO_DATA"
    elif (
        coverage_rate >= minimum_rate
        and not invalid
        and not duplicate_symbols
        and not extra
    ):
        status = "PASSED"
    else:
        status = "NOT_MET"

    min_source = min(source_times) if source_times else None
    max_source = max(source_times) if source_times else None
    return {
        "schema_version": 1,
        "artifact_type": "positioning_coverage_audit",
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_cst": now_utc.astimezone(CST).isoformat(),
        "mode": "read_only",
        "source": source,
        "timeframe": "1H",
        "latest_ticker_ts": latest_tick,
        "latest_batch_collected_ts": latest_collected,
        "source_ts_min": min_source.isoformat() if min_source else None,
        "source_ts_max": max_source.isoformat() if max_source else None,
        "maximum_source_age_minutes": (
            (now_utc - min_source).total_seconds() / 60.0
            if min_source else None
        ),
        "minimum_source_age_minutes": (
            (now_utc - max_source).total_seconds() / 60.0
            if max_source else None
        ),
        "maximum_allowed_source_age_minutes": maximum_source_age_minutes,
        "universe_symbols": denominator,
        "batch_rows": len(rows),
        "observed_unique_symbols": len(observed),
        "valid_symbols": len(valid_symbols),
        "coverage_rate": round(coverage_rate, 6),
        "minimum_rate": minimum_rate,
        "minimum_rate_migration": thresholds.coverage_migration_facts(now_utc),
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics({
            "coverage_rate": coverage_rate,
        }),
        "missing_symbols": missing,
        "extra_symbols": extra,
        "duplicate_symbols": duplicate_symbols,
        "invalid_rows": invalid,
        "status": status,
        "contracts": {
            "universe": "latest ticker symbols ending -USDT-SWAP",
            "availability_clock": "network batch completion collected_ts",
            "freshness": (
                "every source row must be no more than the configured age; "
                "maximum_source_age_minutes is computed from the oldest row"
            ),
            "long_short_ratio": "account-count ratio, not position notional",
        },
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "orders_placed": 0,
    }


def _forward_slot_evidence(
    connection: sqlite3.Connection,
    *,
    slot: datetime,
    source: str,
    target_rate: float,
    maximum_source_age_minutes: float,
    fallback_denominator: int,
) -> dict[str, Any]:
    slot_utc = slot.astimezone(timezone.utc)
    cycle_id = slot.strftime("%Y-%m-%dT%H:00")
    universe, official = _official_snapshot_for_cycle(
        connection, cycle_id, slot_utc)
    rows = connection.execute(
        "SELECT ts,collected_ts,cycle_id,symbol,timeframe,long_ratio,"
        "short_ratio,long_short_ratio,source FROM market_positioning "
        "WHERE source=? AND timeframe='1H' AND cycle_id=? "
        "ORDER BY symbol,collected_ts",
        (source, cycle_id),
    ).fetchall()
    collected_values = {str(row["collected_ts"]) for row in rows}
    batch_reasons: list[str] = []
    collected_at: datetime | None = None
    if len(collected_values) != 1:
        batch_reasons.append(
            "positioning_batch_missing" if not collected_values
            else "multiple_collected_batches_for_cycle")
    else:
        try:
            collected_at = _parse_ts(next(iter(collected_values)))
            if not slot_utc <= collected_at < slot_utc + timedelta(hours=1):
                batch_reasons.append("collected_ts_outside_slot")
        except (TypeError, ValueError):
            batch_reasons.append("collected_ts_invalid")

    symbol_counts: dict[str, int] = {}
    invalid: list[dict[str, Any]] = []
    source_times: list[datetime] = []
    for row in rows:
        symbol = str(row["symbol"])
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        row_errors = _ratio_errors(row)
        try:
            source_time = _parse_ts(row["ts"])
            source_times.append(source_time)
            if collected_at is not None:
                age = (collected_at - source_time).total_seconds() / 60.0
                if age < -1:
                    row_errors.append("source_ts_after_collection")
                elif age > maximum_source_age_minutes:
                    row_errors.append("source_ts_stale_at_collection")
        except (TypeError, ValueError):
            row_errors.append("source_ts_invalid")
        if row_errors:
            invalid.append({"symbol": symbol, "errors": row_errors})

    observed = set(symbol_counts)
    duplicate = sorted(
        symbol for symbol, count in symbol_counts.items() if count != 1)
    invalid_symbols = {item["symbol"] for item in invalid}
    missing = sorted(universe - observed)
    extra = sorted(observed - universe) if universe else sorted(observed)
    valid_symbols = (
        (universe & observed) - invalid_symbols - set(duplicate)
        if universe else set()
    )
    denominator = len(universe) if universe else fallback_denominator
    coverage = len(valid_symbols) / denominator if denominator else 0.0
    metadata_rate = float(official["metadata_coverage_rate"])
    passed = (
        official["status"] == "PASSED"
        and metadata_rate >= target_rate
        and not batch_reasons
        and coverage >= target_rate
        and not invalid
        and not duplicate
        and not extra
    )
    min_source = min(source_times) if source_times else None
    max_source = max(source_times) if source_times else None
    return {
        "cycle_id": cycle_id,
        "official_instrument_snapshot": official,
        "expected_symbols": denominator,
        "positioning_rows": len(rows),
        "observed_unique_symbols": len(observed),
        "valid_symbols": len(valid_symbols),
        "coverage_rate": round(coverage, 6),
        "batch_reasons": batch_reasons,
        "collected_ts": next(iter(collected_values))
        if len(collected_values) == 1 else None,
        "source_ts_min": min_source.isoformat() if min_source else None,
        "source_ts_max": max_source.isoformat() if max_source else None,
        "maximum_source_age_at_collection_minutes": (
            (collected_at - min_source).total_seconds() / 60.0
            if collected_at and min_source else None),
        "missing_symbols": missing,
        "extra_symbols": extra,
        "duplicate_symbols": duplicate,
        "invalid_rows": invalid[:20],
        "invalid_row_count": len(invalid),
        "status": "PASSED" if passed else "NOT_MET",
    }


def _availability_slot_evidence(
    connection: sqlite3.Connection,
    *,
    slot: datetime,
    source: str,
    target_rate: float,
    maximum_source_age_minutes: float,
    fallback_denominator: int,
    maximum_collection_delay_minutes: float = 10.0,
) -> dict[str, Any]:
    """Verify positioning is usable by one exact 15-minute decision slot."""
    slot_utc = slot.astimezone(timezone.utc)
    cycle_id = slot.strftime("%Y-%m-%dT%H:%M")
    universe, official = _official_snapshot_for_cycle(
        connection,
        cycle_id,
        slot_utc,
        slot_width=timedelta(minutes=AVAILABILITY_SLOT_MINUTES),
    )
    collection_slot = (
        slot if slot.minute in POSITIONING_COLLECTION_MINUTES
        else slot - timedelta(minutes=AVAILABILITY_SLOT_MINUTES)
    )
    collection_cycle = collection_slot.strftime("%Y-%m-%dT%H:%M")
    rows = connection.execute(
        "SELECT ts,collected_ts,cycle_id,symbol,timeframe,long_ratio,"
        "short_ratio,long_short_ratio,source FROM market_positioning "
        "WHERE source=? AND timeframe='1H' AND cycle_id=? "
        "ORDER BY symbol,collected_ts",
        (source, collection_cycle),
    ).fetchall()
    collected_values = {str(row["collected_ts"]) for row in rows}
    batch_reasons: list[str] = []
    collected_at: datetime | None = None
    if len(collected_values) != 1:
        batch_reasons.append(
            "positioning_batch_missing" if not collected_values
            else "multiple_collected_batches_for_cycle")
    else:
        try:
            collected_at = _parse_ts(next(iter(collected_values)))
            collection_slot_utc = collection_slot.astimezone(timezone.utc)
            delay = (collected_at - collection_slot_utc).total_seconds() / 60.0
            if delay < 0 or delay > maximum_collection_delay_minutes:
                batch_reasons.append("positioning_collection_delay_invalid")
        except (TypeError, ValueError):
            batch_reasons.append("collected_ts_invalid")

    official_collected: datetime | None = None
    try:
        official_collected = _parse_ts(official["collected_ts_utc"])
    except (KeyError, TypeError, ValueError):
        batch_reasons.append("official_collected_ts_invalid")
    decision_available_at = (
        max(collected_at, official_collected)
        if collected_at is not None and official_collected is not None
        else None
    )

    symbol_counts: dict[str, int] = {}
    invalid: list[dict[str, Any]] = []
    source_times: list[datetime] = []
    for row in rows:
        symbol = str(row["symbol"])
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        row_errors = _ratio_errors(row)
        try:
            source_time = _parse_ts(row["ts"])
            source_times.append(source_time)
            if decision_available_at is not None:
                age = (
                    decision_available_at - source_time
                ).total_seconds() / 60.0
                if age < -1:
                    row_errors.append("source_ts_after_decision_availability")
                elif age > maximum_source_age_minutes:
                    row_errors.append("source_ts_stale_for_decision")
        except (TypeError, ValueError):
            row_errors.append("source_ts_invalid")
        if row_errors:
            invalid.append({"symbol": symbol, "errors": row_errors})

    observed = set(symbol_counts)
    duplicate = sorted(
        symbol for symbol, count in symbol_counts.items() if count != 1)
    invalid_symbols = {item["symbol"] for item in invalid}
    missing = sorted(universe - observed)
    extra = sorted(observed - universe) if universe else sorted(observed)
    valid_symbols = (
        (universe & observed) - invalid_symbols - set(duplicate)
        if universe else set()
    )
    denominator = len(universe) if universe else fallback_denominator
    coverage = len(valid_symbols) / denominator if denominator else 0.0
    metadata_rate = float(official["metadata_coverage_rate"])
    passed = (
        official["status"] == "PASSED"
        and metadata_rate >= target_rate
        and not batch_reasons
        and coverage >= target_rate
        and not invalid
        and not duplicate
        and not extra
    )
    min_source = min(source_times) if source_times else None
    max_source = max(source_times) if source_times else None
    return {
        "cycle_id": cycle_id,
        "positioning_collection_cycle_id": collection_cycle,
        "official_instrument_snapshot": official,
        "expected_symbols": denominator,
        "positioning_rows": len(rows),
        "observed_unique_symbols": len(observed),
        "valid_symbols": len(valid_symbols),
        "coverage_rate": round(coverage, 6),
        "batch_reasons": batch_reasons,
        "collected_ts": next(iter(collected_values))
        if len(collected_values) == 1 else None,
        "decision_available_at_utc": (
            decision_available_at.isoformat()
            if decision_available_at else None),
        "source_ts_min": min_source.isoformat() if min_source else None,
        "source_ts_max": max_source.isoformat() if max_source else None,
        "maximum_source_age_at_decision_minutes": (
            (decision_available_at - min_source).total_seconds() / 60.0
            if decision_available_at and min_source else None),
        "minimum_source_age_at_decision_minutes": (
            (decision_available_at - max_source).total_seconds() / 60.0
            if decision_available_at and max_source else None),
        "missing_symbols": missing,
        "extra_symbols": extra,
        "duplicate_symbols": duplicate,
        "invalid_rows": invalid[:20],
        "invalid_row_count": len(invalid),
        "status": "PASSED" if passed else "NOT_MET",
    }


def audit_positioning_decision_availability(
    market_db: Path,
    *,
    as_of: datetime,
    forward_start: datetime,
    minimum_slots: int = DEFAULT_AVAILABILITY_FORWARD_MINIMUM_SLOTS,
    target_rate: float | None = None,
    source: str = DEFAULT_SOURCE,
    maximum_source_age_minutes: float = DEFAULT_MAXIMUM_SOURCE_AGE_MINUTES,
    grace_minutes: int = 5,
) -> dict[str, Any]:
    """Audit all quarter-hour decision slots after the twice-hourly fix."""
    if minimum_slots <= 0:
        raise ValueError("availability_minimum_slots must be positive")
    # None = 按预注册激活边界解析（边界前 0.99、边界起 0.95）。
    if target_rate is None:
        target_rate = thresholds.coverage_target_rate(as_of)
    if not 0 < target_rate <= 1:
        raise ValueError("target_rate must be in (0,1]")
    if maximum_source_age_minutes <= 0:
        raise ValueError("maximum_source_age_minutes must be positive")
    if not 0 <= grace_minutes < AVAILABILITY_SLOT_MINUTES:
        raise ValueError("availability_grace_minutes must be in [0,15)")
    start = forward_start.astimezone(CST)
    evaluated = as_of.astimezone(CST)
    slots = _expected_quarter_slots(start, evaluated, grace_minutes)
    connection = _ro(market_db)
    try:
        latest_tick = connection.execute(
            "SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
        fallback_denominator = int(connection.execute(
            "SELECT COUNT(DISTINCT symbol) FROM tick_snapshots "
            "WHERE ts=? AND symbol LIKE '%-USDT-SWAP'",
            (latest_tick,),
        ).fetchone()[0])
        rows = [
            _availability_slot_evidence(
                connection,
                slot=slot,
                source=source,
                target_rate=target_rate,
                maximum_source_age_minutes=maximum_source_age_minutes,
                fallback_denominator=fallback_denominator,
            )
            for slot in slots
        ]
    finally:
        connection.close()
    expected_slots = len(rows)
    passed_slots = sum(row["status"] == "PASSED" for row in rows)
    expected_symbol_rows = sum(int(row["expected_symbols"]) for row in rows)
    valid_symbol_rows = sum(int(row["valid_symbols"]) for row in rows)
    official_passed = sum(
        row["official_instrument_snapshot"]["status"] == "PASSED"
        for row in rows)
    slot_rate = passed_slots / expected_slots if expected_slots else 0.0
    coverage = (
        valid_symbol_rows / expected_symbol_rows
        if expected_symbol_rows else 0.0)
    official_rate = (
        official_passed / expected_slots if expected_slots else 0.0)
    requirements = {
        "minimum_slots_met": expected_slots >= minimum_slots,
        "slot_pass_rate_at_least_target": slot_rate >= target_rate,
        "symbol_coverage_rate_at_least_target": coverage >= target_rate,
        "official_snapshot_slot_rate_at_least_target": (
            official_rate >= target_rate),
    }
    observed_quality = all(
        value for key, value in requirements.items()
        if key != "minimum_slots_met"
    )
    if expected_slots > 0 and not observed_quality:
        status = "NOT_MET"
    elif not requirements["minimum_slots_met"]:
        status = "INSUFFICIENT_EVIDENCE"
    elif all(requirements.values()):
        status = "PASSED"
    else:
        status = "NOT_MET"
    return {
        "start_cst": start.isoformat(),
        "as_of_cst": evaluated.isoformat(),
        "schedule_minutes": AVAILABILITY_SLOT_MINUTES,
        "positioning_collection_minutes": list(
            POSITIONING_COLLECTION_MINUTES),
        "slot_grace_minutes": grace_minutes,
        "minimum_slots": minimum_slots,
        "target_rate": target_rate,
        "target_rate_migration": thresholds.coverage_migration_facts(evaluated),
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics(
            {
                "symbol_coverage_rate": coverage,
                "slot_pass_rate": slot_rate,
            },
            target_dependent=("slot_pass_rate",),
        ),
        "maximum_source_age_minutes": maximum_source_age_minutes,
        "expected_slots": expected_slots,
        "passed_slots": passed_slots,
        "expected_symbol_rows": expected_symbol_rows,
        "valid_symbol_rows": valid_symbol_rows,
        "slot_pass_rate": round(slot_rate, 6),
        "symbol_coverage_rate": round(coverage, 6),
        "official_snapshot_slot_rate": round(official_rate, 6),
        "requirements": requirements,
        "status": status,
        "slots": rows,
    }


def audit_positioning_forward_coverage(
    market_db: Path,
    *,
    as_of: datetime,
    forward_start: datetime,
    minimum_slots: int = DEFAULT_FORWARD_MINIMUM_SLOTS,
    target_rate: float | None = None,
    source: str = DEFAULT_SOURCE,
    maximum_source_age_minutes: float = DEFAULT_MAXIMUM_SOURCE_AGE_MINUTES,
    grace_minutes: int = 5,
) -> dict[str, Any]:
    if minimum_slots <= 0:
        raise ValueError("minimum_slots must be positive")
    # None = 按预注册激活边界解析（边界前 0.99、边界起 0.95）。
    if target_rate is None:
        target_rate = thresholds.coverage_target_rate(as_of)
    if not 0 < target_rate <= 1:
        raise ValueError("target_rate must be in (0,1]")
    if maximum_source_age_minutes <= 0:
        raise ValueError("maximum_source_age_minutes must be positive")
    if not 0 <= grace_minutes < SLOT_MINUTES:
        raise ValueError("grace_minutes must be in [0,60)")
    start = forward_start.astimezone(CST)
    evaluated = as_of.astimezone(CST)
    slots = _expected_hour_slots(start, evaluated, grace_minutes)
    connection = _ro(market_db)
    try:
        latest_tick = connection.execute(
            "SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
        fallback_denominator = int(connection.execute(
            "SELECT COUNT(DISTINCT symbol) FROM tick_snapshots "
            "WHERE ts=? AND symbol LIKE '%-USDT-SWAP'",
            (latest_tick,),
        ).fetchone()[0])
        rows = [
            _forward_slot_evidence(
                connection,
                slot=slot,
                source=source,
                target_rate=target_rate,
                maximum_source_age_minutes=maximum_source_age_minutes,
                fallback_denominator=fallback_denominator,
            )
            for slot in slots
        ]
    finally:
        connection.close()
    expected_slots = len(rows)
    passed_slots = sum(row["status"] == "PASSED" for row in rows)
    expected_symbol_rows = sum(int(row["expected_symbols"]) for row in rows)
    valid_symbol_rows = sum(int(row["valid_symbols"]) for row in rows)
    official_passed = sum(
        row["official_instrument_snapshot"]["status"] == "PASSED"
        for row in rows)
    slot_rate = passed_slots / expected_slots if expected_slots else 0.0
    coverage = (
        valid_symbol_rows / expected_symbol_rows
        if expected_symbol_rows else 0.0)
    official_rate = (
        official_passed / expected_slots if expected_slots else 0.0)
    requirements = {
        "minimum_slots_met": expected_slots >= minimum_slots,
        "slot_pass_rate_at_least_target": slot_rate >= target_rate,
        "symbol_coverage_rate_at_least_target": coverage >= target_rate,
        "official_snapshot_slot_rate_at_least_target": (
            official_rate >= target_rate),
    }
    observed_quality = all(
        value for key, value in requirements.items()
        if key != "minimum_slots_met"
    )
    if expected_slots > 0 and not observed_quality:
        status = "NOT_MET"
    elif not requirements["minimum_slots_met"]:
        status = "INSUFFICIENT_EVIDENCE"
    elif all(requirements.values()):
        status = "PASSED"
    else:
        status = "NOT_MET"
    return {
        "start_cst": start.isoformat(),
        "as_of_cst": evaluated.isoformat(),
        "schedule_minutes": SLOT_MINUTES,
        "slot_grace_minutes": grace_minutes,
        "minimum_slots": minimum_slots,
        "target_rate": target_rate,
        "target_rate_migration": thresholds.coverage_migration_facts(evaluated),
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics(
            {
                "symbol_coverage_rate": coverage,
                "slot_pass_rate": slot_rate,
            },
            target_dependent=("slot_pass_rate",),
        ),
        "maximum_source_age_minutes": maximum_source_age_minutes,
        "expected_slots": expected_slots,
        "passed_slots": passed_slots,
        "expected_symbol_rows": expected_symbol_rows,
        "valid_symbol_rows": valid_symbol_rows,
        "slot_pass_rate": round(slot_rate, 6),
        "symbol_coverage_rate": round(coverage, 6),
        "official_snapshot_slot_rate": round(official_rate, 6),
        "requirements": requirements,
        "status": status,
        "slots": rows,
    }


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-db", type=Path, default=DEFAULT_MARKET_DB)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--minimum-rate", type=float, default=None,
        help="default: resolved from the pre-registered activation boundary")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument(
        "--maximum-source-age-minutes", type=float,
        default=DEFAULT_MAXIMUM_SOURCE_AGE_MINUTES,
    )
    parser.add_argument("--as-of", help="CST/ISO timestamp; default now")
    parser.add_argument("--forward-start", default=DEFAULT_FORWARD_START)
    parser.add_argument(
        "--forward-minimum-slots", type=int,
        default=DEFAULT_FORWARD_MINIMUM_SLOTS,
    )
    parser.add_argument("--forward-grace-minutes", type=int, default=5)
    parser.add_argument(
        "--availability-forward-start",
        default=DEFAULT_AVAILABILITY_FORWARD_START,
    )
    parser.add_argument(
        "--availability-minimum-slots", type=int,
        default=DEFAULT_AVAILABILITY_FORWARD_MINIMUM_SLOTS,
    )
    parser.add_argument(
        "--availability-grace-minutes", type=int, default=5)
    parser.add_argument(
        "--receipt-root", type=Path, default=DEFAULT_RECEIPT_ROOT)
    parser.add_argument(
        "--receipt-forward-start", default=DEFAULT_RECEIPT_FORWARD_START)
    parser.add_argument(
        "--receipt-minimum-slots", type=int,
        default=DEFAULT_RECEIPT_FORWARD_MINIMUM_SLOTS,
    )
    parser.add_argument("--receipt-grace-minutes", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        storage_contract = audit_positioning_storage_contract(args.market_db)
        payload = audit_positioning_coverage(
            args.market_db,
            minimum_rate=args.minimum_rate,
            source=args.source,
            maximum_source_age_minutes=args.maximum_source_age_minutes,
        )
        forward = audit_positioning_forward_coverage(
            args.market_db,
            as_of=_parse_cst(args.as_of) if args.as_of else datetime.now(CST),
            forward_start=_parse_cst(args.forward_start),
            minimum_slots=args.forward_minimum_slots,
            target_rate=args.minimum_rate,
            source=args.source,
            maximum_source_age_minutes=args.maximum_source_age_minutes,
            grace_minutes=args.forward_grace_minutes,
        )
        availability = audit_positioning_decision_availability(
            args.market_db,
            as_of=_parse_cst(args.as_of) if args.as_of else datetime.now(CST),
            forward_start=_parse_cst(args.availability_forward_start),
            minimum_slots=args.availability_minimum_slots,
            target_rate=args.minimum_rate,
            source=args.source,
            maximum_source_age_minutes=args.maximum_source_age_minutes,
            grace_minutes=args.availability_grace_minutes,
        )
        receipt_forward = audit_positioning_collection_receipts(
            args.market_db,
            args.receipt_root,
            as_of=_parse_cst(args.as_of) if args.as_of else datetime.now(CST),
            forward_start=_parse_cst(args.receipt_forward_start),
            minimum_slots=args.receipt_minimum_slots,
            target_rate=args.minimum_rate,
            source=args.source,
            maximum_source_age_minutes=args.maximum_source_age_minutes,
            grace_minutes=args.receipt_grace_minutes,
        )
        payload["forward_after_remediation"] = forward
        payload["decision_availability_forward"] = availability
        payload["bounded_retry_receipts_forward"] = receipt_forward
        payload["storage_contract"] = storage_contract
        forward_statuses = {
            forward["status"], availability["status"],
            receipt_forward["status"],
        }
        current_statuses = {
            payload["status"], storage_contract["status"],
            forward["status"], availability["status"],
            receipt_forward["status"],
        }
        if "NOT_MET" in current_statuses or "NO_DATA" in current_statuses:
            payload["overall_status"] = "NOT_MET"
        elif "INSUFFICIENT_EVIDENCE" in forward_statuses:
            payload["overall_status"] = "PENDING_FORWARD_EVIDENCE"
        elif (
            payload["status"] == "PASSED"
            and forward["status"] == "PASSED"
            and availability["status"] == "PASSED"
            and receipt_forward["status"] == "PASSED"
        ):
            payload["overall_status"] = "PASSED"
        else:
            payload["overall_status"] = "NOT_MET"
        _atomic_json(args.json_out, payload)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "ok": True,
        "status": payload["status"],
        "coverage_rate": payload["coverage_rate"],
        "valid_symbols": payload["valid_symbols"],
        "universe_symbols": payload["universe_symbols"],
        "batch_rows": payload["batch_rows"],
        "storage_contract_status": payload["storage_contract"]["status"],
        "overall_status": payload["overall_status"],
        "forward_expected_slots": payload["forward_after_remediation"][
            "expected_slots"],
        "availability_expected_slots": payload[
            "decision_availability_forward"]["expected_slots"],
        "receipt_expected_slots": payload[
            "bounded_retry_receipts_forward"]["expected_slots"],
        "json_out": str(args.json_out),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
