# -*- coding: utf-8 -*-
r"""Strict forward audit for complete-cycle latency and candidate breadth.

The SLA clock starts at the natural UTC+8 cycle boundary and stops only after
the push stage's live reconciliation monitor reports a clean, timestamped
result.  The pass condition is strictly ``elapsed_seconds < 870``.  Missing,
failed, skipped, malformed, or exactly-870-second cycles remain in the planned
denominator and fail closed.

Candidate breadth is diagnostic rather than an order quota: the audit counts
valid per-cycle MTF evidence artifacts and final open cards, but never requires
a minimum trade or long/short mix.  The fixed forward baseline begins only
after both the hourly critical-path optimization and the 2..3 candidate/runtime
contract were deployed.

Reads: stage-status JSON, analysis.db, tmp/mtf evidence.
Writes: one atomic quality JSON only.  No network, order, dispatch, retry, or
business-database mutation.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(r".")
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import stage_runner  # noqa: E402

CST = timezone(timedelta(hours=8))
DEFAULT_FORWARD_START = "2026-08-15T00:00:00+08:00"
DEFAULT_STATUS_DIR = ROOT / "logs" / "stage-status"
DEFAULT_ANALYSIS_DB = ROOT / "db" / "analysis.db"
DEFAULT_MTF_DIR = ROOT / "tmp"
DEFAULT_JSON_OUT = ROOT / "reports" / "quality" / "complete-cycle-sla-audit.json"
SLOT = timedelta(minutes=15)


def parse_cst(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def floor_slot(value: datetime) -> datetime:
    value = value.astimezone(CST)
    minute = (value.minute // 15) * 15
    return value.replace(minute=minute, second=0, microsecond=0)


def cycle_id(value: datetime) -> str:
    return value.astimezone(CST).strftime("%Y-%m-%dT%H:%M")


def planned_cycles(start: datetime, as_of: datetime, finality_seconds: int) -> list[str]:
    start_slot = floor_slot(start)
    if start != start_slot:
        raise ValueError("forward_start must be an exact 15-minute boundary")
    mature_through = floor_slot(as_of - timedelta(seconds=finality_seconds))
    if mature_through < start_slot:
        return []
    values = []
    cursor = start_slot
    while cursor <= mature_through:
        values.append(cycle_id(cursor))
        cursor += SLOT
    return values


def load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def open_card_counts(analysis_db: Path, cycles: list[str]) -> dict[str, int]:
    if not cycles or not analysis_db.exists():
        return {}
    uri = analysis_db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        placeholders = ",".join("?" for _ in cycles)
        rows = connection.execute(
            "SELECT cycle_id,COUNT(*) FROM analysis_signals "
            f"WHERE cycle_id IN ({placeholders}) "
            "AND action IN ('open_long','open_short') GROUP BY cycle_id",
            cycles,
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]): int(row[1]) for row in rows}


def valid_mtf_evidence(mtf_dir: Path, cycle: str) -> list[str]:
    safe_cycle = cycle.replace(":", "-")
    symbols: set[str] = set()
    for path in mtf_dir.glob(f"mtf_{safe_cycle}_*.json"):
        value = load_json(path)
        if (
            not value
            or value.get("ok") is not True
            or value.get("status") != "PASSED"
            or value.get("cycle_id") != cycle
            or value.get("production_database_writes") != 0
            or value.get("orders_placed") != 0
        ):
            continue
        symbol = str(value.get("symbol") or "").strip()
        contract = value.get("evidence_contract")
        if not symbol or not isinstance(contract, dict):
            continue
        if contract.get("cycle_id") != cycle or contract.get("symbol") != symbol:
            continue
        if contract.get("protocol") != "multitimeframe_market_evidence_v1":
            continue
        if contract.get("required_timeframes") != ["15m", "1H", "4H"]:
            continue
        timeframes = contract.get("timeframes")
        if (
            not isinstance(timeframes, dict)
            or set(timeframes) != {"15m", "1H", "4H"}
            or not all(
                isinstance(timeframes.get(name), dict)
                and timeframes[name].get("ready") is True
                for name in ("15m", "1H", "4H")
            )
            or not str(contract.get("evidence_hash") or "").strip()
        ):
            continue
        symbols.add(symbol)
    return sorted(symbols)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def _cycle_started_offset_seconds(cycle: str, started_at: object) -> int | None:
    """Return stage start offset from the natural UTC+8 cycle boundary.

    ``live.started_at`` is the first production-stage timestamp available in
    the stage status.  The offset therefore measures the combined delay from
    required-input readiness, dispatch and scheduler hand-off; it must not be
    described as collection time alone.
    """
    if not str(started_at or "").strip():
        return None
    try:
        started = parse_cst(str(started_at))
        cycle_start = parse_cst(cycle)
    except (TypeError, ValueError):
        return None
    return int((started - cycle_start).total_seconds())


def _whole_seconds_from_milliseconds(value: object) -> int | None:
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds < 0:
        return None
    return int(round(milliseconds / 1000.0))


def _seconds_summary(values: list[int]) -> dict:
    clean = [int(value) for value in values if isinstance(value, int)]
    return {
        "observations": len(clean),
        "average_seconds": (
            round(sum(clean) / len(clean), 3) if clean else None),
        "p50_seconds": _percentile(clean, 0.50),
        "p90_seconds": _percentile(clean, 0.90),
        "max_seconds": max(clean) if clean else None,
    }


def _slot_group_summary(label: str, rows: list[dict]) -> dict:
    planned = len(rows)
    live_offsets = [
        row["live_start_offset_seconds"] for row in rows
        if isinstance(row.get("live_start_offset_seconds"), int)
    ]
    child_budgets = [
        row["live_child_budget_seconds"] for row in rows
        if isinstance(row.get("live_child_budget_seconds"), int)
    ]
    live_runtimes = [
        row["live_runtime_seconds"] for row in rows
        if isinstance(row.get("live_runtime_seconds"), int)
    ]
    elapsed = [
        row["complete_cycle_sla"].get("elapsed_seconds") for row in rows
        if isinstance(row.get("complete_cycle_sla"), dict)
        and isinstance(row["complete_cycle_sla"].get("elapsed_seconds"), int)
    ]
    deep_counts = [int(row.get("mtf_deep_dive_count") or 0) for row in rows]
    final_counts = [int(row.get("final_open_card_count") or 0) for row in rows]
    complete = sum(bool(row["complete_cycle_sla"].get("complete")) for row in rows)
    under = sum(
        bool(row["complete_cycle_sla"].get("under_14m30")) for row in rows)
    return {
        "slot": label,
        "tier": "hourly" if label == ":00" else "quarter",
        "planned_cycles": planned,
        "live_started_cycles": len(live_offsets),
        "complete_cycles": complete,
        "strictly_under_14m30": under,
        "failures": planned - under,
        "strict_pass_rate": _rate(under, planned),
        "live_start_offset": _seconds_summary(live_offsets),
        "live_child_budget": _seconds_summary(child_budgets),
        "live_runtime": _seconds_summary(live_runtimes),
        "complete_cycle_elapsed": _seconds_summary(elapsed),
        "candidate_observation": {
            "cycles_with_2_or_3_deep_dives": sum(
                2 <= value <= 3 for value in deep_counts),
            "deep_dive_distribution": dict(
                sorted(Counter(deep_counts).items())),
            "average_deep_dives": (
                round(sum(deep_counts) / planned, 4) if planned else None),
            "final_open_card_distribution": dict(
                sorted(Counter(final_counts).items())),
        },
    }


def _difference(left: object, right: object, *, digits: int = 3) -> float | None:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    return round(float(left) - float(right), digits)


def slot_observation(rows: list[dict]) -> dict:
    """Describe time/candidate allocation for :00/:15/:30/:45 separately."""
    labels = (":00", ":15", ":30", ":45")
    by_slot = []
    for label in labels:
        minute = label[1:]
        grouped = [row for row in rows if row.get("slot_minute") == minute]
        by_slot.append(_slot_group_summary(label, grouped))

    hourly = by_slot[0]
    quarter_rows = [row for row in rows if row.get("slot_minute") != "00"]
    pooled_quarter = _slot_group_summary(":15/:30/:45 pooled", quarter_rows)
    hourly_start = hourly["live_start_offset"]["average_seconds"]
    quarter_start = pooled_quarter["live_start_offset"]["average_seconds"]
    hourly_deep = hourly["candidate_observation"]["average_deep_dives"]
    quarter_deep = pooled_quarter["candidate_observation"]["average_deep_dives"]
    pass_gap = _difference(
        hourly.get("strict_pass_rate"),
        pooled_quarter.get("strict_pass_rate"),
        digits=6,
    )
    return {
        "definition": {
            "slot_population": "all planned natural 15-minute cycles",
            "hourly_slot": ":00",
            "quarter_slots": [":15", ":30", ":45"],
            "live_start_offset": (
                "live.started_at minus natural cycle boundary; includes "
                "required-input readiness, dispatcher and scheduler delay, "
                "so it is not collection-only latency"
            ),
            "candidate_depth": (
                "valid exact-cycle 15m/1H/4H evidence artifacts; diagnostic "
                "only, never a trade or direction quota"
            ),
        },
        "by_slot": by_slot,
        "hourly_vs_pooled_quarter": {
            "hourly": hourly,
            "pooled_quarter": pooled_quarter,
            "average_live_start_offset_delta_seconds": _difference(
                hourly_start, quarter_start),
            "strict_pass_rate_gap_percentage_points": (
                round(pass_gap * 100.0, 3) if pass_gap is not None else None),
            "average_deep_dive_delta": _difference(hourly_deep, quarter_deep),
        },
    }


def audit_complete_cycle_sla(
    *,
    forward_start: datetime,
    as_of: datetime,
    finality_seconds: int,
    minimum_slots: int,
    status_dir: Path,
    analysis_db: Path,
    mtf_dir: Path,
) -> dict:
    cycles = planned_cycles(forward_start, as_of, finality_seconds)
    cards = open_card_counts(analysis_db, cycles)
    rows = []
    elapsed_values: list[int] = []
    complete_count = under_count = 0
    deep_counts: list[int] = []
    final_counts: list[int] = []

    for cycle in cycles:
        safe_cycle = cycle.replace(":", "-")
        live = load_json(status_dir / f"live-{safe_cycle}.json")
        push = load_json(status_dir / f"push-{safe_cycle}.json")
        # Always rebuild from the primary live + post-reconcile evidence.  Older
        # push status files may contain a pre-hardening SLA object that counted
        # a successfully delivered failure report even though live itself had
        # failed; that cached derived field is not authoritative.
        if isinstance(push, dict):
            sla = stage_runner.build_complete_cycle_sla(
                cycle,
                push.get("post_live_reconcile"),
                live_status=live if isinstance(live, dict) else {},
            )
        else:
            sla = stage_runner.build_complete_cycle_sla(
                cycle,
                {},
                live_status=live if isinstance(live, dict) else {},
            )

        complete = bool(sla.get("complete"))
        under = bool(sla.get("under_14m30"))
        if complete:
            complete_count += 1
        if under:
            under_count += 1
        elapsed = sla.get("elapsed_seconds")
        if isinstance(elapsed, int):
            elapsed_values.append(elapsed)

        mtf_symbols = valid_mtf_evidence(mtf_dir, cycle)
        deep_count = len(mtf_symbols)
        final_count = int(cards.get(cycle, 0))
        live_started_offset = _cycle_started_offset_seconds(
            cycle, live.get("started_at") if isinstance(live, dict) else None)
        live_runtime = _whole_seconds_from_milliseconds(
            live.get("duration_ms") if isinstance(live, dict) else None)
        child_budget = None
        if isinstance(live, dict):
            try:
                raw_child_budget = live.get("child_budget_seconds")
                if raw_child_budget is not None and float(raw_child_budget) >= 0:
                    child_budget = int(round(float(raw_child_budget)))
            except (TypeError, ValueError):
                child_budget = None
        deep_counts.append(deep_count)
        final_counts.append(final_count)
        rows.append({
            "cycle_id": cycle,
            "slot_minute": cycle[-2:],
            "tier": "hourly" if cycle.endswith(":00") else "quarter",
            "live_status": live.get("status") if isinstance(live, dict) else "missing",
            "push_status": push.get("status") if isinstance(push, dict) else "missing",
            "live_start_offset_seconds": live_started_offset,
            "live_child_budget_seconds": child_budget,
            "live_runtime_seconds": live_runtime,
            "complete_cycle_sla": sla,
            "mtf_deep_dive_count": deep_count,
            "mtf_deep_dive_symbols": mtf_symbols,
            "final_open_card_count": final_count,
        })

    planned = len(cycles)
    failures = planned - under_count
    if failures:
        overall_status = "NOT_MET"
    elif planned < minimum_slots:
        overall_status = "PENDING_FORWARD_EVIDENCE"
    else:
        overall_status = "MET"

    return {
        "schema_version": 2,
        "generated_at": as_of.strftime("%Y-%m-%d %H:%M:%S%z"),
        "forward_window": {
            "start_cst": forward_start.isoformat(),
            "mature_end_inclusive": cycles[-1] if cycles else None,
            "finality_seconds": finality_seconds,
            "minimum_slots": minimum_slots,
            "baseline_fixed": True,
        },
        "definition": {
            "clock_start": "natural_cycle_boundary_cst",
            "clock_stop": "successful_clean_post_live_reconcile_timestamp",
            "threshold_seconds": 870,
            "comparison": "<",
            "missing_failed_skipped_or_exactly_870": "fail",
        },
        "strict_sla": {
            "planned_cycles": planned,
            "complete_cycles": complete_count,
            "strictly_under_14m30": under_count,
            "failures": failures,
            "completion_rate": _rate(complete_count, planned),
            "strict_pass_rate": _rate(under_count, planned),
            "p50_seconds": _percentile(elapsed_values, 0.50),
            "p90_seconds": _percentile(elapsed_values, 0.90),
            "max_seconds": max(elapsed_values) if elapsed_values else None,
            "status": overall_status,
        },
        "candidate_observation": {
            "target_deep_dive_range": [2, 3],
            "target_is_not_a_trade_quota": True,
            "cycles_with_2_or_3_deep_dives": sum(2 <= value <= 3 for value in deep_counts),
            "deep_dive_distribution": dict(sorted(Counter(deep_counts).items())),
            "maximum_deep_dives": max(deep_counts) if deep_counts else None,
            "average_deep_dives": (
                round(sum(deep_counts) / len(deep_counts), 4) if deep_counts else None),
            "final_open_card_distribution": dict(sorted(Counter(final_counts).items())),
            "maximum_final_open_cards": max(final_counts) if final_counts else None,
            "no_minimum_final_open_cards": True,
            "no_direction_quota": True,
        },
        "slot_observation": slot_observation(rows),
        "cycles": rows,
        "safety": {
            "business_databases_read_only": True,
            "network_calls": 0,
            "orders": 0,
            "dispatches": 0,
            "repairs_or_retries": 0,
        },
    }


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="strict complete-cycle SLA forward audit")
    parser.add_argument("--forward-start", default=DEFAULT_FORWARD_START)
    parser.add_argument("--as-of")
    parser.add_argument("--finality-seconds", type=int, default=900)
    parser.add_argument("--minimum-slots", type=int, default=96)
    parser.add_argument("--status-dir", default=str(DEFAULT_STATUS_DIR))
    parser.add_argument("--analysis-db", default=str(DEFAULT_ANALYSIS_DB))
    parser.add_argument("--mtf-dir", default=str(DEFAULT_MTF_DIR))
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT))
    args = parser.parse_args(argv)
    try:
        result = audit_complete_cycle_sla(
            forward_start=parse_cst(args.forward_start),
            as_of=parse_cst(args.as_of) if args.as_of else datetime.now(CST),
            finality_seconds=args.finality_seconds,
            minimum_slots=args.minimum_slots,
            status_dir=Path(args.status_dir),
            analysis_db=Path(args.analysis_db),
            mtf_dir=Path(args.mtf_dir),
        )
        atomic_write_json(Path(args.json_out), result)
    except Exception as exc:  # noqa: BLE001 - CLI must emit a bounded cause
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({
        "ok": True,
        "artifact": args.json_out,
        "strict_sla": result["strict_sla"],
        "candidate_observation": result["candidate_observation"],
        "slot_observation": result["slot_observation"],
        "forward_window": result["forward_window"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
