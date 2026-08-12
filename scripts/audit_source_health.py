# -*- coding: utf-8 -*-
"""Audit scheduled fast-source completeness without hiding degraded slots.

The legacy quality metric divides by rows present in ``collection_runs``.  A
cron slot that never wrote a row is therefore invisible.  This audit rebuilds
every expected Beijing-time quarter-hour slot, counts missing rows as unusable,
and reports an exact rolling window plus a pre-registered remediation window.

SQLite access is read-only.  The script atomically replaces only the explicit
JSON evidence file; it never recollects, dispatches, changes a threshold, or
authorizes an order.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))
SLOT_MINUTES = 15
COMPLETE_STATUSES = frozenset({"ok"})
AVAILABLE_STATUSES = frozenset({"ok", "degraded"})
DEFAULT_LEDGER = Path(r"./db/ledger.db")
DEFAULT_OUTPUT = Path(r"./reports/quality/source-health-audit.json")
DEFAULT_FORWARD_START = "2026-08-12T16:00:00+08:00"


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


def _completed_end_exclusive(as_of: datetime, grace_minutes: int) -> datetime:
    """Return the first slot after the last cycle whose grace has elapsed."""
    cutoff = as_of.astimezone(CST) - timedelta(minutes=grace_minutes)
    return _slot_floor(cutoff) + timedelta(minutes=SLOT_MINUTES)


def _cycle_id(value: datetime) -> str:
    return value.astimezone(CST).strftime("%Y-%m-%dT%H:%M")


def _ensure_slot_aligned(value: datetime, label: str) -> None:
    if value != _slot_floor(value):
        raise ValueError(f"{label} must align to a 15-minute CST slot")


def _expected_cycles(start: datetime, end_exclusive: datetime) -> list[str]:
    _ensure_slot_aligned(start, "window start")
    _ensure_slot_aligned(end_exclusive, "window end")
    if end_exclusive < start:
        raise ValueError("window end must not precede start")
    cycles: list[str] = []
    current = start
    while current < end_exclusive:
        cycles.append(_cycle_id(current))
        current += timedelta(minutes=SLOT_MINUTES)
    return cycles


def _failure_kind(status: str, error: str) -> str:
    text = f"{status} {error}".lower()
    if "unexpected_eof" in text or "ssl eof" in text:
        return "ssl_eof"
    if "market_quality_fail_closed" in text:
        return "market_quality_fail_closed"
    if "timeout" in text:
        return "timeout"
    if "503" in text:
        return "http_503"
    if "10061" in text or "connection" in text:
        return "connectivity"
    return f"status:{status or 'unknown'}"


def _summarize_window(
    *,
    start: datetime,
    end_exclusive: datetime,
    records: dict[str, dict[str, Any]],
    target_rate: float,
    minimum_slots: int,
) -> dict[str, Any]:
    expected = _expected_cycles(start, end_exclusive)
    expected_set = set(expected)
    complete = 0
    available = 0
    observed = 0
    raw_status = Counter()
    failure_kinds = Counter()
    exceptions: list[dict[str, Any]] = []

    for cycle in expected:
        row = records.get(cycle)
        if row is None:
            failure_kinds["missing_collection_run"] += 1
            exceptions.append({
                "cycle_id": cycle,
                "status": "missing",
                "failure_kind": "missing_collection_run",
                "err": "expected fast collection slot has no ledger row",
            })
            continue
        observed += 1
        status = str(row.get("status") or "unknown").strip().lower()
        raw_status[status] += 1
        if status in COMPLETE_STATUSES:
            complete += 1
            available += 1
            continue
        if status in AVAILABLE_STATUSES:
            available += 1
        error = str(row.get("err") or "")
        kind = _failure_kind(status, error)
        failure_kinds[kind] += 1
        exceptions.append({
            "cycle_id": cycle,
            "status": status,
            "failure_kind": kind,
            "finished_at_cst": row.get("ts"),
            "err": error[:500],
        })

    denominator = len(expected)
    complete_rate = complete / denominator if denominator else 0.0
    available_rate = available / denominator if denominator else 0.0
    if denominator < minimum_slots:
        status = "INSUFFICIENT_EVIDENCE"
    elif complete_rate >= target_rate:
        status = "PASSED"
    else:
        status = "NOT_MET"

    consecutive_complete = 0
    consecutive_available = 0
    for cycle in reversed(expected):
        row = records.get(cycle)
        row_status = str((row or {}).get("status") or "").strip().lower()
        if row is None or row_status not in COMPLETE_STATUSES:
            break
        consecutive_complete += 1
    for cycle in reversed(expected):
        row = records.get(cycle)
        row_status = str((row or {}).get("status") or "").strip().lower()
        if row is None or row_status not in AVAILABLE_STATUSES:
            break
        consecutive_available += 1

    return {
        "start_cst": start.isoformat(),
        "end_exclusive_cst": end_exclusive.isoformat(),
        "expected_slots": denominator,
        "observed_rows": observed,
        "missing_slots": denominator - observed,
        "complete_slots": complete,
        "incomplete_slots": denominator - complete,
        "complete_rate": round(complete_rate, 6),
        "available_slots": available,
        "unavailable_slots": denominator - available,
        "available_rate": round(available_rate, 6),
        "target_rate": target_rate,
        "minimum_slots": minimum_slots,
        "status": status,
        "consecutive_complete_slots_at_end": consecutive_complete,
        "consecutive_available_slots_at_end": consecutive_available,
        "raw_status_counts": dict(sorted(raw_status.items())),
        "failure_kind_counts": dict(sorted(failure_kinds.items())),
        "exceptions": exceptions,
        "unexpected_rows_outside_expected_slots": sorted(
            cycle for cycle in records if cycle not in expected_set
        ),
    }


def _read_records(
    ledger_db: Path,
    start: datetime,
    end_exclusive: datetime,
    source: str,
) -> dict[str, dict[str, Any]]:
    if not ledger_db.is_file():
        raise FileNotFoundError(str(ledger_db))
    connection = sqlite3.connect(
        f"file:{ledger_db.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT cycle_id,status,ts,rows,latency_ms,err "
            "FROM collection_runs WHERE source=? AND cycle_id>=? AND cycle_id<? "
            "ORDER BY cycle_id",
            (source, _cycle_id(start), _cycle_id(end_exclusive)),
        ).fetchall()
    finally:
        connection.close()
    return {str(row["cycle_id"]): dict(row) for row in rows}


def audit_source_health(
    *,
    ledger_db: Path,
    as_of: datetime,
    forward_start: datetime,
    rolling_days: int = 14,
    target_rate: float = 0.99,
    forward_minimum_slots: int = 96,
    grace_minutes: int = 5,
    source: str = "fast",
) -> dict[str, Any]:
    if rolling_days <= 0:
        raise ValueError("rolling_days must be positive")
    if not 0 < target_rate <= 1:
        raise ValueError("target_rate must be in (0,1]")
    if forward_minimum_slots <= 0:
        raise ValueError("forward_minimum_slots must be positive")
    if not 0 <= grace_minutes < SLOT_MINUTES:
        raise ValueError("grace_minutes must be in [0,15)")

    as_of = as_of.astimezone(CST)
    forward_start = forward_start.astimezone(CST)
    _ensure_slot_aligned(forward_start, "forward_start")
    end_exclusive = _completed_end_exclusive(as_of, grace_minutes)
    rolling_start = end_exclusive - timedelta(days=rolling_days)
    earliest = min(rolling_start, forward_start)
    records = _read_records(ledger_db, earliest, end_exclusive, source)
    rolling_records = {
        cycle: row for cycle, row in records.items()
        if _cycle_id(rolling_start) <= cycle < _cycle_id(end_exclusive)
    }
    forward_records = {
        cycle: row for cycle, row in records.items()
        if _cycle_id(forward_start) <= cycle < _cycle_id(end_exclusive)
    }
    rolling = _summarize_window(
        start=rolling_start,
        end_exclusive=end_exclusive,
        records=rolling_records,
        target_rate=target_rate,
        minimum_slots=rolling_days * 24 * 4,
    )
    forward = _summarize_window(
        start=forward_start,
        end_exclusive=max(forward_start, end_exclusive),
        records=forward_records,
        target_rate=target_rate,
        minimum_slots=forward_minimum_slots,
    )
    if rolling["status"] == "PASSED" and forward["status"] == "PASSED":
        overall = "PASSED"
    elif forward["status"] == "INSUFFICIENT_EVIDENCE":
        overall = "PENDING_FORWARD_EVIDENCE"
    else:
        overall = "NOT_MET"

    return {
        "schema_version": 2,
        "artifact_type": "scheduled_source_health_audit",
        "generated_at_cst": datetime.now(CST).isoformat(),
        "as_of_cst": as_of.isoformat(),
        "source": source,
        "schedule": "every 15 minutes Beijing time",
        "slot_grace_minutes": grace_minutes,
        "strict_complete_semantics": "status_ok_only; degraded_and_missing_are_in_denominator",
        "available_semantics": "status_ok_or_degraded; diagnostic_only",
        "missing_slot_semantics": "incomplete_unavailable_and_in_denominator",
        "target_rate": target_rate,
        "rolling": rolling,
        "forward_after_remediation": forward,
        "overall_status": overall,
        "production_mutation": False,
        "collector_retry_triggered": False,
        "stage_dispatch_triggered": False,
        "orders_placed": 0,
        "production_execution_authorized": False,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit scheduled source availability")
    parser.add_argument("--ledger-db", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source", default="fast")
    parser.add_argument("--as-of", default=None, help="CST/ISO timestamp; default now")
    parser.add_argument("--forward-start", default=DEFAULT_FORWARD_START)
    parser.add_argument("--rolling-days", type=int, default=14)
    parser.add_argument("--target-rate", type=float, default=0.99)
    parser.add_argument("--forward-minimum-slots", type=int, default=96)
    parser.add_argument("--grace-minutes", type=int, default=5)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = audit_source_health(
            ledger_db=args.ledger_db,
            as_of=_parse_cst(args.as_of) if args.as_of else datetime.now(CST),
            forward_start=_parse_cst(args.forward_start),
            rolling_days=args.rolling_days,
            target_rate=args.target_rate,
            forward_minimum_slots=args.forward_minimum_slots,
            grace_minutes=args.grace_minutes,
            source=args.source,
        )
        _atomic_write_json(args.json_out, payload)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_mutation": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "ok": True,
        "output": str(args.json_out),
        "overall_status": payload["overall_status"],
        "rolling_complete_rate": payload["rolling"]["complete_rate"],
        "rolling_available_rate": payload["rolling"]["available_rate"],
        "forward_complete_rate": payload["forward_after_remediation"]["complete_rate"],
        "forward_available_rate": payload["forward_after_remediation"]["available_rate"],
        "forward_expected_slots": payload["forward_after_remediation"]["expected_slots"],
        "production_mutation": False,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
