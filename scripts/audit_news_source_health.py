#!/usr/bin/env python3
"""Audit deterministic news sources by expected Beijing-time schedule slots.

The legacy source metric divides only by ledger rows that exist and treats
``degraded`` as usable.  For a 99% completeness claim that is too permissive:
missing schedule slots and partial publisher failures must remain visible.

This audit reports two rates for every configured deterministic news source:
strict completeness (``status=ok`` only) and availability (``ok`` or
``degraded``).  It also audits each English RSS publisher independently from
the first forward slot where child outcomes are recorded.  A successful fetch
with zero new articles remains complete; natural no-event is not missing data.

SQLite access is read-only.  The only write is an atomic JSON evidence file.
The script never recollects, dispatches, changes a threshold, or places an
order.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import _acceptance_thresholds as thresholds
import audit_source_health as scheduled


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "collectors" / "sources"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))
import news_rss  # noqa: E402


DEFAULT_LEDGER = ROOT / "db" / "ledger.db"
DEFAULT_REGISTRY = SOURCE_DIR / "registry.json"
DEFAULT_OUTPUT = ROOT / "reports" / "quality" / "news-source-health-audit.json"
DEFAULT_FORWARD_START = "2026-08-12T16:15:00+08:00"
STRICT_OK = frozenset({"ok"})
AVAILABLE = frozenset({"ok", "degraded"})


@dataclass(frozen=True)
class NewsSourceSpec:
    source: str
    interval_minutes: int
    role: str
    endpoint: str
    historical_eligible: bool
    # 2026-08-13：新接入源的预注册前向起点（registry `audit_forward_start_cst`）。
    # 源诞生前的槽不进分母（不反向加责），但也绝不缩短既有源的前向证据窗；
    # 生效起点=max(全局 forward_start, 本源起点)，年轻源在攒满最小窗前只判
    # INSUFFICIENT_EVIDENCE，不判 NOT_MET，也不判 PASSED。
    forward_start_cst: str | None = None

    @property
    def critical(self) -> bool:
        return self.role in {"required", "official_required", "required_subsource"}


def _validate_interval(value: Any) -> int:
    interval = int(value or 15)
    if interval < 15 or interval > 1440 or interval % 15 != 0:
        raise ValueError(f"invalid news interval: {interval}")
    return interval


def _load_specs(registry_path: Path) -> list[NewsSourceSpec]:
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise ValueError("registry sources must be a list")
    specs: list[NewsSourceSpec] = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "news" or not item.get("enabled"):
            continue
        adapter = str(item.get("adapter") or "")
        if not adapter or adapter == "EXTERNAL_SCOUT":
            continue
        source = str(item.get("id") or "").strip()
        if not source:
            raise ValueError("enabled news source missing id")
        if bool(item.get("required")):
            role = "required"
        elif source == "okx_news":
            # The goal explicitly requires official dynamics even though the
            # legacy runtime keeps failures isolated from the whole news job.
            role = "official_required"
        else:
            role = "optional"
        specs.append(NewsSourceSpec(
            source=source,
            interval_minutes=_validate_interval(item.get("poll_interval_min")),
            role=role,
            endpoint=str(item.get("endpoint") or ""),
            historical_eligible=True,
            forward_start_cst=(
                str(item.get("audit_forward_start_cst")).strip()
                if item.get("audit_forward_start_cst") else None
            ),
        ))
    parent_ids = {spec.source for spec in specs}
    if "rss_en" not in parent_ids or "okx_news" not in parent_ids:
        raise ValueError("registry must contain enabled rss_en and okx_news")
    for name, endpoint in news_rss.DEFAULT_FEEDS:
        specs.append(NewsSourceSpec(
            source=f"rss:{name.lower()}",
            interval_minutes=15,
            role="required_subsource",
            endpoint=endpoint,
            historical_eligible=False,
        ))
    duplicates = [
        name for name, count in Counter(spec.source for spec in specs).items()
        if count > 1
    ]
    if duplicates:
        raise ValueError("duplicate news source specs: " + ",".join(duplicates))
    return sorted(specs, key=lambda spec: (not spec.critical, spec.source))


def _due_cycles(
    start: datetime,
    end_exclusive: datetime,
    interval_minutes: int,
) -> list[str]:
    cycles = scheduled._expected_cycles(start, end_exclusive)
    due: list[str] = []
    for cycle in cycles:
        slot = datetime.strptime(cycle, "%Y-%m-%dT%H:%M").replace(
            tzinfo=scheduled.CST)
        minute_of_day = slot.hour * 60 + slot.minute
        if minute_of_day % interval_minutes == 0:
            due.append(cycle)
    return due


def _read_records(
    ledger_db: Path,
    sources: list[str],
    start: datetime,
    end_exclusive: datetime,
) -> dict[str, dict[str, dict[str, Any]]]:
    if not ledger_db.is_file():
        raise FileNotFoundError(str(ledger_db))
    connection = sqlite3.connect(
        f"file:{ledger_db.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in sources)
    try:
        rows = connection.execute(
            "SELECT source,cycle_id,status,ts,rows,latency_ms,err "
            f"FROM collection_runs WHERE source IN ({placeholders}) "
            "AND cycle_id>=? AND cycle_id<? ORDER BY source,cycle_id",
            (*sources, scheduled._cycle_id(start),
             scheduled._cycle_id(end_exclusive)),
        ).fetchall()
    finally:
        connection.close()
    output: dict[str, dict[str, dict[str, Any]]] = {
        source: {} for source in sources
    }
    for row in rows:
        output[str(row["source"])][str(row["cycle_id"])] = dict(row)
    return output


def _summarize_source(
    spec: NewsSourceSpec,
    *,
    start: datetime,
    end_exclusive: datetime,
    records: dict[str, dict[str, Any]],
    target_rate: float,
    minimum_slots: int,
) -> dict[str, Any]:
    expected = _due_cycles(start, end_exclusive, spec.interval_minutes)
    raw_status = Counter()
    strict_complete = 0
    available = 0
    observed = 0
    exceptions: list[dict[str, Any]] = []
    for cycle in expected:
        row = records.get(cycle)
        if row is None:
            if len(exceptions) < 20:
                exceptions.append({
                    "cycle_id": cycle,
                    "status": "missing",
                    "err": "expected news source slot has no ledger row",
                })
            continue
        observed += 1
        status = str(row.get("status") or "unknown").strip().lower()
        raw_status[status] += 1
        if status in STRICT_OK:
            strict_complete += 1
        if status in AVAILABLE:
            available += 1
        if status not in STRICT_OK and len(exceptions) < 20:
            exceptions.append({
                "cycle_id": cycle,
                "status": status,
                "finished_at_cst": row.get("ts"),
                "err": str(row.get("err") or "")[:500],
            })
    denominator = len(expected)
    complete_rate = strict_complete / denominator if denominator else 0.0
    available_rate = available / denominator if denominator else 0.0
    if denominator < minimum_slots:
        status = "INSUFFICIENT_EVIDENCE"
    elif complete_rate >= target_rate:
        status = "PASSED"
    else:
        status = "NOT_MET"
    exception_count = denominator - strict_complete
    return {
        "source": spec.source,
        "role": spec.role,
        "endpoint": spec.endpoint,
        "schedule_minutes": spec.interval_minutes,
        "start_cst": start.isoformat(),
        "end_exclusive_cst": end_exclusive.isoformat(),
        "expected_slots": denominator,
        "observed_rows": observed,
        "missing_slots": denominator - observed,
        "complete_slots": strict_complete,
        "degraded_or_failed_slots": observed - strict_complete,
        "strict_complete_rate": round(complete_rate, 6),
        "available_rate": round(available_rate, 6),
        "target_rate": target_rate,
        "minimum_slots": minimum_slots,
        "status": status,
        "raw_status_counts": dict(sorted(raw_status.items())),
        "exception_count": exception_count,
        "exception_examples": exceptions,
        "zero_event_semantics": "successful_fetch_is_complete_even_when_rows_zero",
    }


def _window_status(rows: list[dict[str, Any]], *, critical_only: bool) -> str:
    selected = [
        row for row in rows
        if not critical_only or row["role"] in {
            "required", "official_required", "required_subsource"
        }
    ]
    if any(row["status"] == "NOT_MET" for row in selected):
        return "NOT_MET"
    if any(row["status"] == "INSUFFICIENT_EVIDENCE" for row in selected):
        return "INSUFFICIENT_EVIDENCE"
    return "PASSED"


def _overall_status(all_sources_status: str) -> str:
    """The 99% goal covers every enabled deterministic source, not a subset."""
    if all_sources_status == "PASSED":
        return "PASSED"
    if all_sources_status == "INSUFFICIENT_EVIDENCE":
        return "PENDING_FORWARD_EVIDENCE"
    return "NOT_MET"


def audit_news_source_health(
    *,
    ledger_db: Path,
    registry_path: Path,
    as_of: datetime,
    forward_start: datetime,
    rolling_days: int = 14,
    target_rate: float | None = None,
    minimum_window_hours: int = 24,
    grace_minutes: int = 5,
) -> dict[str, Any]:
    if rolling_days <= 0 or minimum_window_hours <= 0:
        raise ValueError("window sizes must be positive")
    # None = 按预注册激活边界解析；逐源 audit_forward_start_cst 前向起点不受影响。
    if target_rate is None:
        target_rate = thresholds.coverage_target_rate(as_of)
    if not 0 < target_rate <= 1:
        raise ValueError("target_rate must be in (0,1]")
    if not 0 <= grace_minutes < 15:
        raise ValueError("grace_minutes must be in [0,15)")
    forward_start = forward_start.astimezone(scheduled.CST)
    scheduled._ensure_slot_aligned(forward_start, "forward_start")
    as_of = as_of.astimezone(scheduled.CST)
    end_exclusive = scheduled._completed_end_exclusive(as_of, grace_minutes)
    rolling_start = end_exclusive - timedelta(days=rolling_days)
    earliest = min(rolling_start, forward_start)
    specs = _load_specs(registry_path)
    records = _read_records(
        ledger_db, [spec.source for spec in specs], earliest, end_exclusive)

    def _spec_effective_start(spec: NewsSourceSpec, base: datetime) -> datetime:
        """新接入源按其预注册起点收窄分母（诞生前不加责）；只可推迟不可提前。"""
        if not spec.forward_start_cst:
            return base
        own = scheduled._parse_cst(spec.forward_start_cst)
        own = own.astimezone(scheduled.CST)
        scheduled._ensure_slot_aligned(
            own, f"{spec.source}.audit_forward_start_cst")
        return max(base, own)

    rolling_rows = []
    for spec in specs:
        if not spec.historical_eligible:
            continue
        spec_rolling_start = _spec_effective_start(spec, rolling_start)
        minimum = len(_due_cycles(
            rolling_start, end_exclusive, spec.interval_minutes))
        rolling_rows.append(_summarize_source(
            spec,
            start=spec_rolling_start,
            # A preregistered source can have an activation time after the
            # audit's current as-of boundary.  Its pre-activation denominator
            # is exactly zero; represent that as an empty half-open window
            # instead of asking the shared scheduler for a reversed window.
            end_exclusive=max(spec_rolling_start, end_exclusive),
            records=records[spec.source],
            target_rate=target_rate,
            minimum_slots=minimum,
        ))
    forward_rows = []
    safe_forward_end = max(forward_start, end_exclusive)
    for spec in specs:
        slots_per_hour = 60 / spec.interval_minutes
        minimum = max(1, int(math.ceil(minimum_window_hours * slots_per_hour)))
        spec_forward_start = _spec_effective_start(spec, forward_start)
        forward_rows.append(_summarize_source(
            spec,
            start=spec_forward_start,
            end_exclusive=max(spec_forward_start, safe_forward_end),
            records=records[spec.source],
            target_rate=target_rate,
            minimum_slots=minimum,
        ))
    critical_status = _window_status(forward_rows, critical_only=True)
    all_status = _window_status(forward_rows, critical_only=False)
    overall = _overall_status(all_status)
    return {
        "schema_version": 1,
        "artifact_type": "scheduled_news_source_health_audit",
        "generated_at_cst": datetime.now(scheduled.CST).isoformat(),
        "as_of_cst": as_of.isoformat(),
        "forward_start_cst": forward_start.isoformat(),
        "target_rate": target_rate,
        "target_rate_migration": thresholds.coverage_migration_facts(as_of),
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics({
            f"{row['source']}.strict_complete_rate": row.get(
                "strict_complete_rate")
            for row in forward_rows
        }),
        "minimum_window_hours": minimum_window_hours,
        "slot_grace_minutes": grace_minutes,
        "strict_complete_semantics": "status_ok_only; missing_and_degraded_are_in_denominator",
        "available_semantics": "status_ok_or_degraded; diagnostic_only",
        "overall_scope": "all_enabled_deterministic_news_sources",
        "rolling_parent_sources": {
            "days": rolling_days,
            "legacy_caveat": (
                "Before 2026-08-12 05:30 rss_en parent rows could hide a "
                "failed child publisher; use this window as diagnostic only."
            ),
            "sources": rolling_rows,
        },
        "forward_after_remediation": {
            "critical_status": critical_status,
            "all_sources_status": all_status,
            "sources": forward_rows,
        },
        "overall_status": overall,
        "production_mutation": False,
        "collector_retry_triggered": False,
        "stage_dispatch_triggered": False,
        "orders_placed": 0,
        "production_execution_authorized": False,
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-db", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--forward-start", default=DEFAULT_FORWARD_START)
    parser.add_argument("--rolling-days", type=int, default=14)
    parser.add_argument(
        "--target-rate", type=float, default=None,
        help="default: resolved from the pre-registered activation boundary")
    parser.add_argument("--minimum-window-hours", type=int, default=24)
    parser.add_argument("--grace-minutes", type=int, default=5)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = audit_news_source_health(
            ledger_db=args.ledger_db,
            registry_path=args.registry,
            as_of=(scheduled._parse_cst(args.as_of) if args.as_of
                   else datetime.now(scheduled.CST)),
            forward_start=scheduled._parse_cst(args.forward_start),
            rolling_days=args.rolling_days,
            target_rate=args.target_rate,
            minimum_window_hours=args.minimum_window_hours,
            grace_minutes=args.grace_minutes,
        )
        _atomic_json(args.json_out, payload)
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_mutation": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    forward = payload["forward_after_remediation"]
    print(json.dumps({
        "ok": True,
        "output": str(args.json_out),
        "overall_status": payload["overall_status"],
        "critical_status": forward["critical_status"],
        "all_sources_status": forward["all_sources_status"],
        "source_count": len(forward["sources"]),
        "production_mutation": False,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
