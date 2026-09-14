"""Read-only liveness observations for cycle-critical production outputs.

The forward-only zero-output threshold is resolved from the single acceptance
source.  Registration never wires an external alert and has no scheduling or
trading authority; historical observations remain calibration evidence only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import _acceptance_thresholds as thresholds


CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_ROOT = ROOT / "db"
DEFAULT_STAGE_STATUS_DIR = ROOT / "logs" / "stage-status"
DEFAULT_JSON_OUT = ROOT / "reports" / "quality" / "critical-output-liveness.json"
PUSH_NAME_RE = re.compile(
    r"^push-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})\.json$")


def parse_cst(value: str | datetime) -> datetime:
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


def cycle_id(value: datetime) -> str:
    return value.astimezone(CST).strftime("%Y-%m-%dT%H:%M")


def floor_cadence(value: datetime, cadence_minutes: int) -> datetime:
    local = value.astimezone(CST).replace(second=0, microsecond=0)
    minute = (local.minute // cadence_minutes) * cadence_minutes
    return local.replace(minute=minute)


def ceil_cadence(value: datetime, cadence_minutes: int) -> datetime:
    floored = floor_cadence(value, cadence_minutes)
    if value.astimezone(CST) == floored:
        return floored
    return floored + timedelta(minutes=cadence_minutes)


def planned_cycles(
    activation_start: datetime,
    as_of: datetime,
    *,
    cadence_minutes: int,
    finality_seconds: int,
) -> list[str]:
    start = ceil_cadence(activation_start, cadence_minutes)
    mature = floor_cadence(
        as_of - timedelta(seconds=finality_seconds), cadence_minutes)
    rows: list[str] = []
    cursor = start
    while cursor <= mature:
        rows.append(cycle_id(cursor))
        cursor += timedelta(minutes=cadence_minutes)
    return rows


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def query_cycle_rows(
    path: Path,
    sql: str,
    params: tuple[Any, ...],
) -> list[tuple[str, str | None]]:
    connection = connect_ro(path)
    try:
        return [
            (str(row["cycle_id"]), str(row["output_ts"]) if row["output_ts"] else None)
            for row in connection.execute(sql, params)
        ]
    finally:
        connection.close()


def query_account_snapshot_rows(
    path: Path,
    start_cycle: str,
    end_cycle: str,
) -> list[tuple[str, str | None]]:
    start_ts = start_cycle.replace("T", " ") + ":00"
    end_ts = (
        parse_cst(end_cycle + ":00") + timedelta(minutes=15)
    ).strftime("%Y-%m-%d %H:%M:%S")
    connection = connect_ro(path)
    try:
        output: dict[str, str] = {}
        for row in connection.execute(
            "SELECT ts FROM account_snapshots WHERE profile='live' "
            "AND ts>=? AND ts<? ORDER BY ts",
            (start_ts, end_ts),
        ):
            stamp = str(row["ts"])
            mapped = cycle_id(floor_cadence(parse_cst(stamp), 15))
            output[mapped] = stamp
        return sorted(output.items())
    finally:
        connection.close()


def query_push_rows(
    status_dir: Path,
    expected: set[str],
) -> tuple[list[tuple[str, str | None]], list[str]]:
    rows: list[tuple[str, str | None]] = []
    errors: list[str] = []
    if not status_dir.is_dir():
        return rows, [f"stage status directory missing: {status_dir}"]
    for path in status_dir.glob("push-*.json"):
        match = PUSH_NAME_RE.match(path.name)
        if not match:
            continue
        candidate = f"{match.group(1)}T{match.group(2)}:{match.group(3)}"
        if candidate not in expected:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        identity = str(payload.get("cycle_id") or "")
        if identity != candidate:
            errors.append(
                f"{path.name}: cycle identity mismatch {identity!r} != {candidate!r}")
            continue
        stamp = payload.get("finished_at") or payload.get("started_at")
        rows.append((candidate, str(stamp) if stamp else None))
    return rows, errors


def observe(
    *,
    name: str,
    owner: str,
    cadence_minutes: int,
    expected_cycles: list[str],
    output_rows: list[tuple[str, str | None]],
    as_of: datetime,
    alert_expected_cycles: list[str],
    threshold_registration: dict[str, Any],
    source_errors: list[str] | None = None,
) -> dict[str, Any]:
    expected = set(expected_cycles)
    by_cycle: dict[str, str | None] = {}
    for output_cycle, stamp in output_rows:
        if output_cycle in expected:
            by_cycle[output_cycle] = stamp
    missing = [item for item in expected_cycles if item not in by_cycle]
    completed_streaks: list[int] = []
    running_streak = 0
    for item in expected_cycles:
        if item not in by_cycle:
            running_streak += 1
        elif running_streak:
            completed_streaks.append(running_streak)
            running_streak = 0
    if running_streak:
        completed_streaks.append(running_streak)
    streak_distribution: dict[str, int] = {}
    for length in completed_streaks:
        key = str(length)
        streak_distribution[key] = streak_distribution.get(key, 0) + 1
    trailing = 0
    for item in reversed(expected_cycles):
        if item in by_cycle:
            break
        trailing += 1
    latest_cycle = max(by_cycle) if by_cycle else None
    latest_ts = by_cycle.get(latest_cycle) if latest_cycle else None
    age_seconds: float | None = None
    if latest_ts:
        try:
            age_seconds = round(
                max(0.0, (as_of - parse_cst(latest_ts)).total_seconds()), 3)
        except (TypeError, ValueError):
            pass
    errors = list(source_errors or [])
    alert_expected = list(alert_expected_cycles)
    alert_trailing = 0
    for item in reversed(alert_expected):
        if item in by_cycle:
            break
        alert_trailing += 1
    registered_threshold = int(
        threshold_registration["alert_threshold_slots"])
    effective_threshold = threshold_registration.get(
        "effective_alert_threshold_slots")
    if errors:
        alert_status = "NOT_EVALUATED_SOURCE_ERROR"
    elif not threshold_registration.get("activated"):
        alert_status = "NOT_EVALUATED_BEFORE_ACTIVATION"
    elif not alert_expected:
        alert_status = "NOT_EVALUATED_NO_MATURE_POST_ACTIVATION_SLOTS"
    elif alert_trailing >= registered_threshold:
        alert_status = "ALERT_CONDITION_OBSERVED"
    else:
        alert_status = "BELOW_ALERT_THRESHOLD"
    return {
        "name": name,
        "owner": owner,
        "cadence_minutes": cadence_minutes,
        "expected_slots": len(expected_cycles),
        "observed_output_slots": len(by_cycle),
        "missing_output_slots": len(missing),
        "latest_expected_cycle": expected_cycles[-1] if expected_cycles else None,
        "latest_output_cycle": latest_cycle,
        "latest_output_ts": latest_ts,
        "age_seconds_since_last_output": age_seconds,
        "consecutive_zero_output_slots": trailing,
        "maximum_zero_output_streak_slots": max(completed_streaks, default=0),
        "zero_output_streak_distribution": streak_distribution,
        "recent_missing_cycles": missing[-16:],
        "source_errors": errors,
        "observation_status": (
            "SOURCE_ERROR" if errors
            else "ZERO_OUTPUT_OBSERVED" if trailing
            else "CURRENT_OUTPUT_PRESENT"
        ),
        "alert_threshold_slots": registered_threshold,
        "effective_alert_threshold_slots": effective_threshold,
        "alert_window_activation_cst": threshold_registration["activation_cst"],
        "alert_window_expected_slots": len(alert_expected),
        "alert_window_latest_expected_cycle": (
            alert_expected[-1] if alert_expected else None),
        "alert_consecutive_zero_output_slots": alert_trailing,
        "alert_status": alert_status,
    }


def build(
    *,
    db_root: Path,
    stage_status_dir: Path,
    activation_start: str | datetime,
    as_of: str | datetime,
    finality_seconds: int | None = None,
) -> dict[str, Any]:
    evaluated_at = parse_cst(as_of)
    activation = parse_cst(activation_start)
    finality = (
        thresholds.post_push_monitor_deadline_seconds(evaluated_at)
        if finality_seconds is None else int(finality_seconds)
    )
    if finality < 0:
        raise ValueError("finality_seconds must be non-negative")
    threshold_registration = (
        thresholds.critical_output_zero_streak_registration_facts(
            evaluated_at))
    alert_activation = max(
        activation,
        parse_cst(threshold_registration["activation_cst"]),
    )
    quarter = planned_cycles(
        activation, evaluated_at, cadence_minutes=15,
        finality_seconds=finality)
    hourly = planned_cycles(
        activation, evaluated_at, cadence_minutes=60,
        finality_seconds=finality)
    alert_quarter = planned_cycles(
        alert_activation, evaluated_at, cadence_minutes=15,
        finality_seconds=finality)
    alert_hourly = planned_cycles(
        alert_activation, evaluated_at, cadence_minutes=60,
        finality_seconds=finality)
    if not quarter:
        raise ValueError("no mature quarter-hour cycles in observation window")

    observations: list[dict[str, Any]] = []

    def add_db_observation(
        *,
        name: str,
        owner: str,
        cadence: int,
        expected: list[str],
        alert_expected: list[str],
        loader: Callable[[], list[tuple[str, str | None]]],
    ) -> None:
        errors: list[str] = []
        try:
            rows = loader()
        except (OSError, sqlite3.Error, ValueError) as exc:
            rows = []
            errors.append(f"{type(exc).__name__}: {exc}")
        observations.append(observe(
            name=name, owner=owner, cadence_minutes=cadence,
            expected_cycles=expected, output_rows=rows, as_of=evaluated_at,
            alert_expected_cycles=alert_expected,
            threshold_registration=threshold_registration,
            source_errors=errors))

    ledger_db = db_root / "ledger.db"
    analysis_db = db_root / "analysis.db"
    trades_db = db_root / "live_trades.db"
    account_db = db_root / "account.db"

    add_db_observation(
        name="market_fast_writer_receipt",
        owner="collect_data.py + collectors.ledger",
        cadence=15,
        expected=quarter,
        alert_expected=alert_quarter,
        loader=lambda: query_cycle_rows(
            ledger_db,
            "SELECT cycle_id,ts AS output_ts FROM collection_runs "
            "WHERE source='fast' AND cycle_id>=? AND cycle_id<=?",
            (quarter[0], quarter[-1])),
    )
    add_db_observation(
        name="market_slow_writer_receipt",
        owner="collect_slow.py + collectors.ledger",
        cadence=60,
        expected=hourly,
        alert_expected=alert_hourly,
        loader=lambda: query_cycle_rows(
            ledger_db,
            "SELECT cycle_id,ts AS output_ts FROM collection_runs "
            "WHERE source='slow' AND cycle_id>=? AND cycle_id<=?",
            (hourly[0], hourly[-1])),
    )
    add_db_observation(
        name="analyst_writer",
        owner="collectors/analyst_writer.py",
        cadence=15,
        expected=quarter,
        alert_expected=alert_quarter,
        loader=lambda: query_cycle_rows(
            analysis_db,
            "SELECT cycle_id,ts AS output_ts FROM analysis_runs "
            "WHERE cycle_id>=? AND cycle_id<=?",
            (quarter[0], quarter[-1])),
    )
    add_db_observation(
        name="trades_writer_terminal",
        owner="collectors/trades_writer.py",
        cadence=15,
        expected=quarter,
        alert_expected=alert_quarter,
        loader=lambda: query_cycle_rows(
            trades_db,
            "SELECT cycle_id,ts AS output_ts FROM trade_cycles "
            "WHERE mode='live' AND cycle_id>=? AND cycle_id<=?",
            (quarter[0], quarter[-1])),
    )
    add_db_observation(
        name="account_snapshot_writer",
        owner="scripts/jobb_live_account_check.py",
        cadence=15,
        expected=quarter,
        alert_expected=alert_quarter,
        loader=lambda: query_account_snapshot_rows(
            account_db, quarter[0], quarter[-1]),
    )
    push_rows, push_errors = query_push_rows(stage_status_dir, set(quarter))
    observations.append(observe(
        name="push_pipeline_terminal",
        owner="scripts/push_pipeline.py + stage_runner.py",
        cadence_minutes=15,
        expected_cycles=quarter,
        output_rows=push_rows,
        as_of=evaluated_at,
        alert_expected_cycles=alert_quarter,
        threshold_registration=threshold_registration,
        source_errors=push_errors,
    ))

    source_error_count = sum(len(row["source_errors"]) for row in observations)
    alert_condition_count = sum(
        row["alert_status"] == "ALERT_CONDITION_OBSERVED"
        for row in observations)
    return {
        "schema_version": 2,
        "artifact_type": "critical_output_liveness_observation",
        "generated_at_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "as_of_cst": evaluated_at.strftime("%Y-%m-%d %H:%M:%S"),
        "activation_start_cst": activation.isoformat(),
        "finality_seconds": finality,
        "latest_mature_quarter_cycle": quarter[-1],
        "latest_mature_hourly_cycle": hourly[-1] if hourly else None,
        "threshold_registration": threshold_registration,
        "overall_status": (
            "PARTIAL_REGISTERED_THRESHOLD"
            if source_error_count
            else "OBSERVED_ALERT_CONDITION"
            if alert_condition_count
            else "OBSERVED_REGISTERED_THRESHOLD"
        ),
        "observations": observations,
        "scope": {
            "included": [row["name"] for row in observations],
            "delegated_companion_audits": [
                "audit_news_source_health.py",
                "audit_report_completeness.py",
                "audit_periodic_report_completeness.py",
                "audit_push_completeness.py",
            ],
            "excluded_conditional_writers": [
                "trade_experiences (only expected after fills)",
                "repair_queue (event driven)",
                "account_bills (daily and cash-flow-window driven)",
            ],
        },
        "semantics": (
            "Zero output means no authoritative terminal/receipt for an expected "
            "mature slot; it never means no trade, no news, or no business action. "
            "Only post-activation mature slots can satisfy the registered condition. "
            "This artifact is alert-only evidence and cannot trigger trading."
        ),
        "safety": {
            "database_mode": "sqlite_mode_ro",
            "production_database_writes": 0,
            "historical_backfill": False,
            "external_send": False,
            "orders_placed": 0,
            "scheduler_changes": 0,
        },
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="read-only cycle-critical output liveness observation")
    parser.add_argument("--db-root", type=Path, default=DEFAULT_DB_ROOT)
    parser.add_argument(
        "--stage-status-dir", type=Path, default=DEFAULT_STAGE_STATUS_DIR)
    parser.add_argument(
        "--activation-start",
        default=thresholds.SLA_V3_REGISTRATION_ACTIVATION_CST)
    parser.add_argument("--as-of", default=datetime.now(CST).isoformat())
    parser.add_argument("--finality-seconds", type=int)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build(
        db_root=args.db_root,
        stage_status_dir=args.stage_status_dir,
        activation_start=args.activation_start,
        as_of=args.as_of,
        finality_seconds=args.finality_seconds,
    )
    atomic_write_json(args.json_out, payload)
    print(json.dumps({
        "ok": not payload["overall_status"].startswith("PARTIAL"),
        "overall_status": payload["overall_status"],
        "json_out": str(args.json_out),
        "observations": len(payload["observations"]),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 1 if payload["overall_status"].startswith("PARTIAL") else 0


if __name__ == "__main__":
    raise SystemExit(main())
