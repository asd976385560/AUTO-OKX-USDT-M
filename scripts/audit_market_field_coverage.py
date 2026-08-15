#!/usr/bin/env python3
"""Audit scheduled per-symbol market fields without hiding missing rows.

The reference universe comes only from the official instrument snapshot frozen
inside the same natural slot.  Its row count and canonical SHA-256 are rebuilt
independently before the first ticker snapshot in that slot is checked symbol
by symbol.  Missing or corrupt official snapshots, missing ticker slots,
missing symbols, null/non-finite fields, and crossed executable quotes remain
in the denominator.

SQLite is opened read-only.  The script atomically replaces only the explicit
JSON evidence file; it never recollects, backfills, dispatches, changes a
production threshold, or places an order.
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
from typing import Any, Callable

import _acceptance_thresholds as thresholds


CST = timezone(timedelta(hours=8))
UTC = timezone.utc
SLOT_MINUTES = 15
DEFAULT_DB = Path(r".\db\market.db")
DEFAULT_OUTPUT = Path(
    r".\reports\quality\market-field-coverage-audit.json")
DEFAULT_FORWARD_START = "2026-08-12T22:45:00+08:00"
OFFICIAL_SNAPSHOT_SOURCE = "okx_public_instruments_live_usdt_linear_swap"


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


def _parse_utc(value: str) -> datetime:
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slot_floor(value: datetime) -> datetime:
    local = value.astimezone(CST)
    return local.replace(
        minute=(local.minute // SLOT_MINUTES) * SLOT_MINUTES,
        second=0,
        microsecond=0,
    )


def _ensure_slot_aligned(value: datetime, label: str) -> None:
    if value != _slot_floor(value):
        raise ValueError(f"{label} must align to a 15-minute CST slot")


def _completed_end_exclusive(as_of: datetime, grace_minutes: int) -> datetime:
    cutoff = as_of.astimezone(CST) - timedelta(minutes=grace_minutes)
    return _slot_floor(cutoff) + timedelta(minutes=SLOT_MINUTES)


def _expected_slots(start: datetime, end_exclusive: datetime) -> list[datetime]:
    _ensure_slot_aligned(start, "window start")
    _ensure_slot_aligned(end_exclusive, "window end")
    if end_exclusive < start:
        raise ValueError("window end must not precede start")
    output: list[datetime] = []
    current = start
    while current < end_exclusive:
        output.append(current)
        current += timedelta(minutes=SLOT_MINUTES)
    return output


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0


def _nonnegative(value: Any) -> bool:
    return _finite(value) and float(value) >= 0


FIELD_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "last": _positive,
    "bid": _positive,
    "ask": _positive,
    "vol24h": _nonnegative,
    "fundingRate": _finite,
    "oi": _nonnegative,
    "chg24h": _finite,
}
DERIVED_FIELDS = ("executable_quote",)


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


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


def _official_snapshot_hash(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _official_snapshot_for_slot(
    connection: sqlite3.Connection,
    cycle_id: str,
    slot_utc: datetime,
) -> tuple[set[str], dict[str, Any]]:
    required_tables = {
        "official_instrument_snapshot_runs",
        "official_instrument_snapshot_rows",
    }
    present_tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not required_tables.issubset(present_tables):
        return set(), {
            "status": "MISSING",
            "reasons": ["official_snapshot_tables_missing"],
            "header_symbol_count": 0,
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
            "header_symbol_count": 0,
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
    observed_hash = _official_snapshot_hash(rows)
    header_count = int(header["symbol_count"])
    reasons: list[str] = []
    if int(header["complete"]) != 1:
        reasons.append("header_not_complete")
    if header_count != len(rows):
        reasons.append("header_row_count_mismatch")
    if str(header["payload_sha256"]) != observed_hash:
        reasons.append("payload_sha256_mismatch")
    if str(header["source"]) != OFFICIAL_SNAPSHOT_SOURCE:
        reasons.append("official_snapshot_source_invalid")
    collected_at: datetime | None = None
    try:
        collected_at = _parse_utc(str(header["collected_ts_utc"]))
    except (TypeError, ValueError):
        reasons.append("collected_ts_invalid")
    if collected_at is not None:
        if not slot_utc <= collected_at < slot_utc + timedelta(minutes=15):
            reasons.append("collected_ts_outside_slot")
    invalid_rows: list[dict[str, Any]] = []
    universe: set[str] = set()
    valid_metadata = 0
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
        if not _positive(row["ct_val"]):
            row_reasons.append("ct_val_invalid")
        if not _positive(row["lot_sz"]):
            row_reasons.append("lot_sz_invalid")
        try:
            listing = _parse_utc(str(row["list_time_utc"]))
            if listing > slot_utc + timedelta(minutes=15):
                row_reasons.append("list_time_after_slot")
        except (TypeError, ValueError):
            row_reasons.append("list_time_invalid")
        if row_reasons:
            if len(invalid_rows) < 20:
                invalid_rows.append({
                    "symbol": symbol,
                    "reasons": row_reasons,
                })
        else:
            valid_metadata += 1
    denominator = len(rows)
    metadata_rate = valid_metadata / denominator if denominator else 0.0
    if not rows:
        reasons.append("official_snapshot_empty")
    return universe, {
        "status": "PASSED" if not reasons else "NOT_MET",
        "reasons": reasons,
        "collected_ts_utc": header["collected_ts_utc"],
        "source": header["source"],
        "header_symbol_count": header_count,
        "observed_row_count": len(rows),
        "stored_payload_sha256": header["payload_sha256"],
        "observed_payload_sha256": observed_hash,
        "valid_metadata_rows": valid_metadata,
        "invalid_metadata_rows": denominator - valid_metadata,
        "metadata_coverage_rate": round(metadata_rate, 6),
        "invalid_metadata_examples": invalid_rows,
    }


def _field_checks(row: sqlite3.Row | None) -> dict[str, bool]:
    if row is None:
        return {
            **{field: False for field in FIELD_VALIDATORS},
            "executable_quote": False,
        }
    checks = {
        field: validator(row[field])
        for field, validator in FIELD_VALIDATORS.items()
    }
    checks["executable_quote"] = bool(
        checks["bid"]
        and checks["ask"]
        and float(row["ask"]) >= float(row["bid"])
    )
    return checks


def _snapshot_for_slot(
    connection: sqlite3.Connection,
    slot: datetime,
) -> tuple[str | None, dict[str, sqlite3.Row], float | None, list[str]]:
    slot_utc = slot.astimezone(UTC)
    end_utc = slot_utc + timedelta(minutes=SLOT_MINUTES)
    snapshots = [
        str(row[0]) for row in connection.execute(
            "SELECT DISTINCT ts FROM tick_snapshots WHERE ts>=? AND ts<? "
            "ORDER BY ts",
            (_iso_utc(slot_utc), _iso_utc(end_utc)),
        ).fetchall()
    ]
    if not snapshots:
        return None, {}, None, []
    selected = snapshots[0]
    rows = connection.execute(
        "SELECT ts,symbol,last,bid,ask,vol24h,fundingRate,oi,chg24h "
        "FROM tick_snapshots WHERE ts=? ORDER BY symbol",
        (selected,),
    ).fetchall()
    by_symbol = {str(row["symbol"]): row for row in rows}
    delay = (_parse_utc(selected) - slot_utc).total_seconds()
    return selected, by_symbol, delay, snapshots


def audit_market_field_coverage(
    market_db: Path,
    *,
    as_of: datetime,
    forward_start: datetime,
    target_rate: float | None = None,
    minimum_slots: int = 96,
    grace_minutes: int = 5,
    maximum_snapshot_delay_seconds: int = 300,
) -> dict[str, Any]:
    # None = 按预注册激活边界解析（边界前 0.99、边界起 0.95）。
    if target_rate is None:
        target_rate = thresholds.coverage_target_rate(as_of)
    if not 0 < target_rate <= 1:
        raise ValueError("target_rate must be in (0,1]")
    if minimum_slots <= 0:
        raise ValueError("minimum_slots must be positive")
    if not 0 <= grace_minutes < SLOT_MINUTES:
        raise ValueError("grace_minutes must be in [0,15)")
    if maximum_snapshot_delay_seconds < 0:
        raise ValueError("maximum_snapshot_delay_seconds must be nonnegative")
    as_of = as_of.astimezone(CST)
    forward_start = forward_start.astimezone(CST)
    _ensure_slot_aligned(forward_start, "forward_start")
    end_exclusive = max(
        forward_start, _completed_end_exclusive(as_of, grace_minutes))
    slots = _expected_slots(forward_start, end_exclusive)
    field_names = (*FIELD_VALIDATORS.keys(), *DERIVED_FIELDS)
    field_valid = {field: 0 for field in field_names}
    expected_symbol_rows = 0
    all_fields_valid = 0
    observed_slots = 0
    timely_slots = 0
    passed_slots = 0
    slot_rows: list[dict[str, Any]] = []

    connection = _ro(market_db)
    try:
        for slot in slots:
            slot_utc = slot.astimezone(UTC)
            cycle_id = slot.strftime("%Y-%m-%dT%H:%M")
            universe, official_snapshot = _official_snapshot_for_slot(
                connection, cycle_id, slot_utc)
            expected_count = len(universe)
            expected_symbol_rows += expected_count
            snapshot_ts, observed, delay, snapshot_candidates = (
                _snapshot_for_slot(connection, slot))
            if snapshot_ts is not None:
                observed_slots += 1
            timely = bool(
                snapshot_ts is not None
                and delay is not None
                and 0 <= delay <= maximum_snapshot_delay_seconds
            )
            if timely:
                timely_slots += 1
            slot_valid = {field: 0 for field in field_names}
            slot_complete = 0
            invalid_examples: list[dict[str, Any]] = []
            for symbol in sorted(universe):
                checks = _field_checks(observed.get(symbol) if timely else None)
                for field, valid in checks.items():
                    if valid:
                        field_valid[field] += 1
                        slot_valid[field] += 1
                complete = all(checks.values())
                if complete:
                    all_fields_valid += 1
                    slot_complete += 1
                elif len(invalid_examples) < 20:
                    invalid_examples.append({
                        "symbol": symbol,
                        "invalid_fields": sorted(
                            field for field, valid in checks.items() if not valid
                        ),
                    })
            all_rate = slot_complete / expected_count if expected_count else 0.0
            metadata_pass = (
                official_snapshot["status"] == "PASSED"
                and official_snapshot["metadata_coverage_rate"] >= target_rate
            )
            slot_pass = metadata_pass and timely and all_rate >= target_rate
            if slot_pass:
                passed_slots += 1
            observed_symbols = set(observed)
            slot_rows.append({
                "cycle_id": cycle_id,
                "official_instrument_snapshot": official_snapshot,
                "snapshot_ts_utc": snapshot_ts,
                "snapshot_delay_seconds": delay,
                "snapshot_candidates_in_slot": len(snapshot_candidates),
                "expected_symbols": expected_count,
                "observed_expected_symbols": len(universe & observed_symbols),
                "missing_symbols": len(universe - observed_symbols),
                "extra_symbols": len(observed_symbols - universe),
                "field_valid_symbols": slot_valid,
                "all_fields_valid_symbols": slot_complete,
                "all_fields_complete_rate": round(all_rate, 6),
                "timely": timely,
                "status": "PASSED" if slot_pass else "NOT_MET",
                "invalid_examples": invalid_examples,
            })
    finally:
        connection.close()

    expected_slots = len(slots)
    field_rates = {
        field: (
            field_valid[field] / expected_symbol_rows
            if expected_symbol_rows else 0.0
        )
        for field in field_names
    }
    all_fields_rate = (
        all_fields_valid / expected_symbol_rows
        if expected_symbol_rows else 0.0
    )
    slot_pass_rate = passed_slots / expected_slots if expected_slots else 0.0
    timely_rate = timely_slots / expected_slots if expected_slots else 0.0
    official_snapshot_passed = sum(
        1 for row in slot_rows
        if row["official_instrument_snapshot"]["status"] == "PASSED"
    )
    official_metadata_valid = sum(
        int(row["official_instrument_snapshot"]["valid_metadata_rows"])
        for row in slot_rows
    )
    official_metadata_rows = sum(
        int(row["official_instrument_snapshot"]["observed_row_count"])
        for row in slot_rows
    )
    official_snapshot_rate = (
        official_snapshot_passed / expected_slots if expected_slots else 0.0
    )
    official_metadata_rate = (
        official_metadata_valid / official_metadata_rows
        if official_metadata_rows else 0.0
    )
    requirements = {
        "minimum_slots_met": expected_slots >= minimum_slots,
        "official_snapshot_slot_rate_at_least_target": (
            official_snapshot_rate >= target_rate),
        "official_metadata_rate_at_least_target": (
            official_metadata_rate >= target_rate),
        "every_field_rate_at_least_target": all(
            rate >= target_rate for rate in field_rates.values()),
        "all_fields_row_rate_at_least_target": all_fields_rate >= target_rate,
        "slot_pass_rate_at_least_target": slot_pass_rate >= target_rate,
        "timely_snapshot_rate_at_least_target": timely_rate >= target_rate,
    }
    if not requirements["minimum_slots_met"]:
        status = "INSUFFICIENT_EVIDENCE"
    elif all(requirements.values()):
        status = "PASSED"
    else:
        status = "NOT_MET"
    return {
        "schema_version": 1,
        "artifact_type": "scheduled_market_field_coverage_audit",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "as_of_cst": as_of.isoformat(),
        "forward_start_cst": forward_start.isoformat(),
        "end_exclusive_cst": end_exclusive.isoformat(),
        "mode": "read_only",
        "target_rate": target_rate,
        "target_rate_migration": thresholds.coverage_migration_facts(as_of),
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics(
            {
                "official_snapshot_slot_rate": official_snapshot_rate,
                "official_metadata_rate": official_metadata_rate,
                "all_fields_row_rate": all_fields_rate,
                "timely_snapshot_rate": timely_rate,
                **{
                    f"field_rate.{name}": rate
                    for name, rate in field_rates.items()
                },
                "slot_pass_rate": slot_pass_rate,
            },
            target_dependent=("slot_pass_rate",),
        ),
        "minimum_slots": minimum_slots,
        "slot_grace_minutes": grace_minutes,
        "maximum_snapshot_delay_seconds": maximum_snapshot_delay_seconds,
        "contracts": {
            "reference_universe": (
                "same-slot immutable official live USDT linear swap snapshot; "
                "header row count and canonical SHA-256 rebuilt independently"
            ),
            "field_denominator": "expected symbol rows across every scheduled slot",
            "missing_semantics": (
                "missing slots, late snapshots, missing symbols, invalid fields, "
                "and crossed quotes remain in the denominator"
            ),
            "executable_quote": "finite positive bid and ask with ask >= bid",
            "no_backfill": True,
        },
        "official_instrument_evidence": {
            "expected_slots": expected_slots,
            "passed_snapshot_slots": official_snapshot_passed,
            "snapshot_slot_rate": round(official_snapshot_rate, 6),
            "metadata_rows": official_metadata_rows,
            "valid_metadata_rows": official_metadata_valid,
            "metadata_coverage_rate": round(official_metadata_rate, 6),
        },
        "counts": {
            "expected_slots": expected_slots,
            "observed_slots": observed_slots,
            "missing_slots": expected_slots - observed_slots,
            "timely_slots": timely_slots,
            "late_or_missing_slots": expected_slots - timely_slots,
            "passed_slots": passed_slots,
            "failed_slots": expected_slots - passed_slots,
            "expected_symbol_rows": expected_symbol_rows,
            "all_fields_valid_symbol_rows": all_fields_valid,
            "field_valid_symbol_rows": field_valid,
        },
        "rates": {
            "field_coverage_rates": {
                field: round(rate, 6) for field, rate in field_rates.items()
            },
            "all_fields_complete_rate": round(all_fields_rate, 6),
            "slot_pass_rate": round(slot_pass_rate, 6),
            "timely_snapshot_rate": round(timely_rate, 6),
        },
        "requirements": requirements,
        "status": status,
        "slots": slot_rows,
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "production_execution_authorized": False,
        "orders_placed": 0,
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
    parser.add_argument("--market-db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--forward-start", default=DEFAULT_FORWARD_START)
    parser.add_argument(
        "--target-rate", type=float, default=None,
        help="default: resolved from the pre-registered activation boundary")
    parser.add_argument("--minimum-slots", type=int, default=96)
    parser.add_argument("--grace-minutes", type=int, default=5)
    parser.add_argument("--maximum-snapshot-delay-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        payload = audit_market_field_coverage(
            args.market_db,
            as_of=(
                _parse_cst(args.as_of) if args.as_of else datetime.now(CST)
            ),
            forward_start=_parse_cst(args.forward_start),
            target_rate=args.target_rate,
            minimum_slots=args.minimum_slots,
            grace_minutes=args.grace_minutes,
            maximum_snapshot_delay_seconds=(
                args.maximum_snapshot_delay_seconds),
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
        "ok": True,
        "output": str(args.json_out),
        "status": payload["status"],
        "expected_slots": payload["counts"]["expected_slots"],
        "expected_symbol_rows": payload["counts"]["expected_symbol_rows"],
        "all_fields_complete_rate": payload["rates"][
            "all_fields_complete_rate"],
        "slot_pass_rate": payload["rates"]["slot_pass_rate"],
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
