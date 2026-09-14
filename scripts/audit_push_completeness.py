# -*- coding: utf-8 -*-
"""Audit scheduled Push reports and exact delivery on a strict slot denominator.

The production pipeline is expected to produce one report for every Beijing-time
15-minute cycle.  This audit reconstructs that schedule instead of dividing only
by pipeline rows that happened to exist.  A report is complete only when a
production archive exists under ``reports/agents``, the logged hard checks agree,
and the archived Markdown independently passes the pure format validator.

Delivery is fail-closed: the exact ``push:{cycle}`` identity must have a
``status='sent'`` receipt in the dedupe database (or an equivalent explicit
``mark(status=sent)`` event), and the receipt content hash must match an
independently valid archive for that cycle.  Missing slots, no-send runs,
pending/failed receipts, content drift, and missing archives remain failures.

The audit is read-only with respect to business data.  A failed effective gate
(95% from the registered activation boundary; 99% before it) is an
observed result, so the command exits zero after a successful audit and writes
``NOT_MET`` to its JSON artifact.  It never resends, backfills, or places orders.
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from stage_failure_contract import REPORT_RECONCILE_BARRIER_FROM
from typing import Callable

import _acceptance_thresholds as thresholds
import validate_push_format
from _audit_artifact_context import resolve_audit_output


CST = timezone(timedelta(hours=8))
# 闸门数值由预注册激活边界解析（边界前 0.99、边界起 0.95）；这两个常量只是
# 迁移登记的显式化，实际判定一律走 thresholds.coverage_target_rate(as_of)。
TARGET_RATE = thresholds.COVERAGE_TARGET_RATE
LEGACY_TARGET_RATE = thresholds.COVERAGE_LEGACY_TARGET_RATE
SCHEDULE_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SCHEDULE_MINUTES
DEFAULT_FORWARD_START = "2026-08-12T16:00:00+08:00"
BUSINESS_ATTESTATION_REQUIRED_FROM = "2026-08-14T07:00"
INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM = "2026-08-15T08:00"
_CYCLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)$")


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def _days_inclusive(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end date must not be earlier than start date")
    return [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    ]


def _expected_cycles(start: date, end: date) -> list[str]:
    cycles: list[str] = []
    for day in _days_inclusive(start, end):
        anchor = datetime.combine(day, datetime.min.time())
        for offset in range(SLOTS_PER_DAY):
            cycles.append(
                (anchor + timedelta(minutes=offset * SCHEDULE_MINUTES)).strftime(
                    "%Y-%m-%dT%H:%M"
                )
            )
    return cycles


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
        minute=(value.minute // SCHEDULE_MINUTES) * SCHEDULE_MINUTES,
        second=0,
        microsecond=0,
    )


def _ensure_slot_aligned(value: datetime, *, label: str) -> None:
    if value != _slot_floor(value):
        raise ValueError(f"{label} must align to a 15-minute CST slot")


def _completed_end_exclusive(
    as_of: datetime,
    finality_grace_minutes: int,
) -> datetime:
    cutoff = as_of.astimezone(CST) - timedelta(
        minutes=finality_grace_minutes
    )
    return _slot_floor(cutoff) + timedelta(minutes=SCHEDULE_MINUTES)


def _expected_cycles_between(
    start: datetime,
    end_exclusive: datetime,
) -> list[str]:
    _ensure_slot_aligned(start, label="window start")
    _ensure_slot_aligned(end_exclusive, label="window end")
    if end_exclusive < start:
        raise ValueError("window end must not precede start")
    cycles: list[str] = []
    current = start
    while current < end_exclusive:
        cycles.append(current.strftime("%Y-%m-%dT%H:%M"))
        current += timedelta(minutes=SCHEDULE_MINUTES)
    return cycles


def _delivery_key(cycle: str) -> str:
    return hashlib.sha256(
        f"default|push:{cycle}".encode("utf-8")
    ).hexdigest()


def _load_jsonl(path: Path) -> tuple[list[dict], int, int]:
    rows: list[dict] = []
    malformed = 0
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            total += 1
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                malformed += 1
                continue
            if not isinstance(row, dict):
                malformed += 1
                continue
            rows.append(row)
    return rows, malformed, total


def _pipeline_attempts(
    path: Path,
    expected: set[str],
) -> tuple[dict[str, list[dict]], dict]:
    rows, malformed, total = _load_jsonl(path)
    attempts: dict[str, list[dict]] = defaultdict(list)
    invalid_cycle_rows = 0
    outside_window_rows = 0
    nonproduction_context_rows = 0
    stale_legacy_probe_rows = 0
    for row in rows:
        cycle = str(row.get("cycle") or "")
        if not _CYCLE_RE.fullmatch(cycle):
            invalid_cycle_rows += 1
            continue
        if cycle not in expected:
            outside_window_rows += 1
            continue
        context = str(row.get("execution_context") or "").strip().lower()
        if (
            row.get("natural_production_evidence") is False
            or context in {"test", "probe"}
        ):
            nonproduction_context_rows += 1
            continue
        # Before execution_context existed, tests repeatedly used one fixed
        # old cycle and appended to the production JSONL.  A real business
        # push is same-slot only; a row recorded >2h after its cycle cannot be
        # natural production and is retained solely as contamination evidence.
        if not context:
            try:
                observed = _parse_cst(str(row.get("ts") or ""))
                slot = _parse_cst(cycle)
                if (observed - slot).total_seconds() > 2 * 3600:
                    stale_legacy_probe_rows += 1
                    continue
            except (TypeError, ValueError):
                pass
        attempts[cycle].append(row)
    for cycle_rows in attempts.values():
        cycle_rows.sort(key=lambda row: str(row.get("ts") or ""))
    return dict(attempts), {
        "lines_total": total,
        "malformed_lines": malformed,
        "invalid_cycle_rows": invalid_cycle_rows,
        "outside_window_rows": outside_window_rows,
        "nonproduction_context_rows": nonproduction_context_rows,
        "stale_legacy_probe_rows": stale_legacy_probe_rows,
    }


def _strip_archive_header(text: str) -> str:
    normalized = text.replace("\r\n", "\n")
    if normalized.startswith("# ") and "\n\n" in normalized:
        return normalized.split("\n\n", 1)[1]
    return normalized


def _under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _validate_archive_attempt(
    row: dict,
    *,
    cycle: str,
    reports_dir: Path,
    validator: Callable[[str], dict],
) -> dict:
    reasons: list[str] = []
    steps = row.get("steps")
    if not isinstance(steps, dict):
        return {"complete": False, "content_hash": None,
                "reasons": ["pipeline steps missing"]}

    build = steps.get("build") if isinstance(steps.get("build"), dict) else {}
    render = steps.get("render") if isinstance(steps.get("render"), dict) else {}
    logged_validate = (
        steps.get("validate")
        if isinstance(steps.get("validate"), dict)
        else {}
    )
    archive = (
        steps.get("archive")
        if isinstance(steps.get("archive"), dict)
        else {}
    )
    if build.get("ok") is not True:
        reasons.append("build not successful")
    if render.get("rc") != 0:
        reasons.append("render rc is not zero")
    if (
        logged_validate.get("rc") != 0
        or list(logged_validate.get("errors") or [])
        or list(logged_validate.get("missing") or [])
    ):
        reasons.append("logged format validation failed")
    if archive.get("rc") != 0:
        reasons.append("archive rc is not zero")
    if archive.get("hard_check") is not True:
        reasons.append("archive hard check is not true")
    if archive.get("degraded"):
        reasons.append("archive is degraded")

    # From the pre-registered forward boundary, a visually complete archive is
    # not sufficient evidence.  The production run must also prove that the
    # same-cycle live runner was immutable/released and that the business
    # terminal/fill set was re-read both before archive and immediately before
    # the irreversible external send.  This is intentionally versioned so
    # historical reports are not judged by a contract that did not exist.
    if cycle >= BUSINESS_ATTESTATION_REQUIRED_FROM:
        pre_archive = (
            steps.get("business_attestation_pre_archive")
            if isinstance(steps.get("business_attestation_pre_archive"), dict)
            else {}
        )
        pre_send = (
            steps.get("business_attestation_pre_send")
            if isinstance(steps.get("business_attestation_pre_send"), dict)
            else {}
        )
        for label, attestation in (
            ("pre-archive", pre_archive), ("pre-send", pre_send)
        ):
            if (
                attestation.get("ok") is not True
                or attestation.get("required") is not True
            ):
                reasons.append(f"business attestation {label} missing or failed")
            stage = attestation.get("live_stage_terminal")
            if not isinstance(stage, dict) or (
                stage.get("profile_lease_released") is not True
                or stage.get("same_cycle_active_lease") is not False
                or not str(stage.get("finished_at") or "").strip()
            ):
                reasons.append(f"live terminal proof {label} incomplete")
            if cycle >= REPORT_RECONCILE_BARRIER_FROM:
                barrier = (
                    stage.get("report_reconcile_barrier")
                    if isinstance(stage, dict)
                    and isinstance(
                        stage.get("report_reconcile_barrier"), dict)
                    else {}
                )
                if (
                    barrier.get("required") is not True
                    or barrier.get("report_safe") is not True
                    or barrier.get("status") not in {"ok", "applied"}
                    or barrier.get("rc") != 0
                    or barrier.get("blocking") is not False
                ):
                    reasons.append(
                        f"report reconcile barrier {label} incomplete")
        common_fields = ("mode", "trade_count")
        for field in common_fields:
            if pre_archive.get(field) != pre_send.get(field):
                reasons.append(
                    f"business attestation {field} drifted before send")
        if pre_archive.get("mode") == "business_terminal":
            for field in ("decision", "n_orders", "sha256"):
                if pre_archive.get(field) != pre_send.get(field):
                    reasons.append(
                        f"business attestation {field} drifted before send")
            if not re.fullmatch(
                r"[0-9a-f]{64}", str(pre_archive.get("sha256") or "")
            ):
                reasons.append("business attestation sha256 is invalid")
        elif pre_archive.get("mode") == "upstream_failure":
            failure_fields = (
                "schema_version", "profile", "cycle_id", "terminal",
                "trade_count", "failure_kind", "intent_rows",
                "failed_clean_rows", "unsafe_rows", "sha256",
            )
            for field in failure_fields:
                if pre_archive.get(field) != pre_send.get(field):
                    reasons.append(
                        f"failure attestation {field} drifted before send")
            if (
                pre_archive.get("schema_version") != 1
                or pre_archive.get("profile") != "live"
                or pre_archive.get("cycle_id") != cycle
                or pre_archive.get("terminal") != "absent"
                or pre_archive.get("trade_count") != 0
                or not str(pre_archive.get("failure_kind") or "").strip()
                or pre_archive.get("unsafe_rows") != 0
                or pre_archive.get("intent_rows")
                != pre_archive.get("failed_clean_rows")
            ):
                reasons.append("failure report business absence proof is invalid")
            failure_body = {
                field: pre_archive.get(field)
                for field in failure_fields if field != "sha256"
            }
            canonical = json.dumps(
                failure_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            expected_sha = hashlib.sha256(
                canonical.encode("utf-8")).hexdigest()
            if (
                not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(pre_archive.get("sha256") or ""),
                )
                or pre_archive.get("sha256") != expected_sha
            ):
                reasons.append("failure attestation sha256 is invalid")
        else:
            reasons.append("business attestation mode is invalid")

        if cycle >= INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM:
            interval_fields = (
                "inter_report_exchange_schema_version",
                "inter_report_fill_count",
                "inter_report_sha256",
                "inter_report_window_start_exclusive_cst",
                "inter_report_window_end_inclusive_cst",
            )
            for label, attestation in (
                ("pre-archive", pre_archive), ("pre-send", pre_send)
            ):
                if attestation.get(
                        "inter_report_exchange_required") is not True:
                    reasons.append(
                        f"inter-report exchange attestation {label} missing")
            for field in interval_fields:
                if pre_archive.get(field) != pre_send.get(field):
                    reasons.append(
                        f"inter-report exchange attestation {field} drifted "
                        "before send")
            try:
                interval_end = datetime.strptime(cycle, "%Y-%m-%dT%H:%M")
                interval_start = interval_end - timedelta(minutes=15)
            except ValueError:
                reasons.append("inter-report exchange interval cycle invalid")
            else:
                if (
                    pre_archive.get(
                        "inter_report_window_start_exclusive_cst")
                    != interval_start.strftime("%Y-%m-%d %H:%M:%S")
                    or pre_archive.get(
                        "inter_report_window_end_inclusive_cst")
                    != interval_end.strftime("%Y-%m-%d %H:%M:%S")
                ):
                    reasons.append(
                        "inter-report exchange interval boundaries invalid")
            interval_count = pre_archive.get("inter_report_fill_count")
            interval_schema = pre_archive.get(
                "inter_report_exchange_schema_version")
            if (
                interval_schema not in {1, 2}
                or not isinstance(interval_count, int)
                or isinstance(interval_count, bool)
                or interval_count < 0
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(pre_archive.get("inter_report_sha256") or ""),
                )
            ):
                reasons.append(
                    "inter-report exchange attestation summary invalid")

    archive_text = str(archive.get("path") or "").strip()
    if not archive_text:
        reasons.append("archive path missing")
        return {"complete": False, "content_hash": None, "reasons": reasons}
    archive_path = Path(archive_text)
    if not _under_root(archive_path, reports_dir):
        reasons.append("archive is outside the production reports directory")
        return {"complete": False, "content_hash": None, "reasons": reasons}
    if not archive_path.is_file():
        reasons.append("archive file missing")
        return {"complete": False, "content_hash": None, "reasons": reasons}

    try:
        file_bytes = archive_path.stat().st_size
        logged_bytes = int(archive.get("bytes"))
        if logged_bytes != file_bytes:
            reasons.append("archive byte count disagrees with file")
    except (OSError, TypeError, ValueError):
        reasons.append("archive byte count is invalid")

    try:
        archived = archive_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        reasons.append(f"archive unreadable: {type(exc).__name__}")
        return {"complete": False, "content_hash": None, "reasons": reasons}
    raw_content = _strip_archive_header(archived)
    expected_clock = cycle[11:16]
    if f"【{expected_clock}】" not in raw_content[:500]:
        reasons.append("archive title does not match cycle clock")
    try:
        # 生产 validator 按 cycle 应用版本化报告契约；测试/外部注入的纯
        # 单参数 validator 保持原接口，避免历史审计适配器被迫升级。
        if validator is validate_push_format.validate:
            independent = validator(archived, cycle_id=cycle)
        else:
            independent = validator(archived)
    except Exception as exc:  # one corrupt report must not hide other slots
        reasons.append(f"independent validator error: {type(exc).__name__}")
    else:
        if not isinstance(independent, dict) or not independent.get("ok"):
            reasons.append("independent archive validation failed")

    content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    return {
        "complete": not reasons,
        "content_hash": content_hash,
        "archive_path": str(archive_path),
        "reasons": reasons,
    }


def _delivery_receipts(
    *,
    expected_cycles: list[str],
    dedupe_db: Path,
    event_log: Path,
) -> tuple[dict[str, set[str]], dict[str, datetime], dict]:
    key_to_cycle = {_delivery_key(cycle): cycle for cycle in expected_cycles}
    receipts: dict[str, set[str]] = defaultdict(set)
    delivered_at: dict[str, datetime] = {}
    db_rows_in_window = 0
    invalid_delivery_timestamps = 0
    connection = sqlite3.connect(
        f"file:{dedupe_db}?mode=ro", uri=True, timeout=10
    )
    try:
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sent'"
        ).fetchone()
        if table is None:
            raise ValueError("dedupe database is missing sent table")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(sent)").fetchall()
        }
        required = {"k", "content_hash", "status", "updated_at"}
        if not required.issubset(columns):
            raise ValueError("dedupe sent table is missing required columns")
        for row in connection.execute(
            "SELECT k,content_hash,status,updated_at FROM sent"
        ):
            cycle = key_to_cycle.get(str(row["k"] or ""))
            if cycle is None:
                continue
            db_rows_in_window += 1
            content_hash = str(row["content_hash"] or "")
            if row["status"] == "sent" and re.fullmatch(
                r"[0-9a-f]{64}", content_hash
            ):
                receipts[cycle].add(content_hash)
                try:
                    stamp = _parse_cst(str(row["updated_at"] or ""))
                except (TypeError, ValueError):
                    invalid_delivery_timestamps += 1
                else:
                    previous = delivered_at.get(cycle)
                    if previous is None or stamp < previous:
                        delivered_at[cycle] = stamp
    finally:
        connection.close()

    expected_set = set(expected_cycles)
    events, malformed, total = _load_jsonl(event_log)
    claims_by_key: dict[str, tuple[str, str]] = {}
    event_sent_receipts = 0
    for event in events:
        dkey = str(event.get("dedupe_key") or "")
        if not dkey.startswith("push:"):
            continue
        cycle = dkey.removeprefix("push:")
        if cycle not in expected_set:
            continue
        if str(event.get("target") or "") != "default":
            continue
        event_key = str(event.get("key") or "")
        if event.get("event") == "claim":
            content_hash = str(event.get("content_hash") or "")
            if re.fullmatch(r"[0-9a-f]{64}", content_hash):
                claims_by_key[event_key] = (cycle, content_hash)
        elif (
            event.get("event") == "mark"
            and event.get("status") == "sent"
            and event_key in claims_by_key
        ):
            claimed_cycle, content_hash = claims_by_key[event_key]
            if claimed_cycle == cycle:
                before = len(receipts[cycle])
                receipts[cycle].add(content_hash)
                try:
                    stamp = _parse_cst(str(event.get("ts") or ""))
                except (TypeError, ValueError):
                    invalid_delivery_timestamps += 1
                else:
                    previous = delivered_at.get(cycle)
                    if previous is None or stamp < previous:
                        delivered_at[cycle] = stamp
                if len(receipts[cycle]) > before:
                    event_sent_receipts += 1
        # duplicate_skip alone is not accepted: qq_push also skips a live
        # pending claim.  It is delivery evidence only when the DB or a prior
        # explicit mark already proves status=sent.

    return dict(receipts), delivered_at, {
        "dedupe_rows_in_window": db_rows_in_window,
        "event_lines_total": total,
        "event_malformed_lines": malformed,
        "event_sent_receipts_added": event_sent_receipts,
        "invalid_delivery_timestamps": invalid_delivery_timestamps,
    }


def _summarize_cycles(
    *,
    expected_cycles: list[str],
    attempts: dict[str, list[dict]],
    receipts: dict[str, set[str]],
    reports_dir: Path,
    archive_validator: Callable[[str], dict],
    target_rate: float,
    minimum_slots: int,
) -> dict:
    """Apply one fail-closed report/delivery contract to an exact cycle list."""
    if minimum_slots <= 0:
        raise ValueError("minimum_slots must be positive")
    failure_rows: list[dict] = []
    day_accumulator: dict[str, dict[str, int]] = {}
    for cycle in expected_cycles:
        day = cycle[:10]
        metrics = day_accumulator.setdefault(day, {
            "expected_slots": 0,
            "pipeline_present": 0,
            "report_complete": 0,
            "delivery_confirmed": 0,
            "delivered_report_complete": 0,
        })
        metrics["expected_slots"] += 1

    pipeline_present = 0
    report_complete = 0
    delivery_confirmed = 0
    delivered_report_complete = 0
    archive_attempts_checked = 0
    for cycle in expected_cycles:
        cycle_attempts = attempts.get(cycle, [])
        day_metrics = day_accumulator[cycle[:10]]
        if cycle_attempts:
            pipeline_present += 1
            day_metrics["pipeline_present"] += 1
        archive_results = [
            _validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=reports_dir,
                validator=archive_validator,
            )
            for row in cycle_attempts
        ]
        archive_attempts_checked += len(archive_results)
        valid_hashes = {
            str(result["content_hash"])
            for result in archive_results
            if result.get("complete") and result.get("content_hash")
        }
        report_ok = bool(valid_hashes)
        receipt_hashes = receipts.get(cycle, set())
        delivery_ok = bool(receipt_hashes)
        exact_ok = bool(valid_hashes.intersection(receipt_hashes))
        if report_ok:
            report_complete += 1
            day_metrics["report_complete"] += 1
        if delivery_ok:
            delivery_confirmed += 1
            day_metrics["delivery_confirmed"] += 1
        if exact_ok:
            delivered_report_complete += 1
            day_metrics["delivered_report_complete"] += 1

        if not (report_ok and exact_ok):
            reasons: list[str] = []
            if not cycle_attempts:
                reasons.append("missing_pipeline_slot")
            if cycle_attempts and not report_ok:
                reasons.append("no_independently_valid_production_archive")
            if not delivery_ok:
                reasons.append("exact_delivery_receipt_missing")
            elif report_ok and not exact_ok:
                reasons.append("delivered_content_hash_not_in_valid_archives")
            attempt_failures = [
                {
                    "ts": str(row.get("ts") or ""),
                    "reasons": result.get("reasons", []),
                }
                for row, result in zip(cycle_attempts, archive_results)
                if not result.get("complete")
            ]
            failure_rows.append({
                "cycle": cycle,
                "pipeline_attempts": len(cycle_attempts),
                "report_complete": report_ok,
                "delivery_confirmed": delivery_ok,
                "delivered_report_complete": exact_ok,
                "reasons": reasons,
                "attempt_failures": attempt_failures,
            })

    expected = len(expected_cycles)
    rates = {
        "pipeline_presence_rate": (
            pipeline_present / expected if expected else 0.0),
        "report_completeness_rate": (
            report_complete / expected if expected else 0.0),
        "delivery_confirmation_rate": (
            delivery_confirmed / expected if expected else 0.0),
        "delivered_report_completeness_rate": (
            delivered_report_complete / expected if expected else 0.0),
    }
    if expected < minimum_slots:
        report_status = "INSUFFICIENT_EVIDENCE"
        delivery_status = "INSUFFICIENT_EVIDENCE"
    else:
        report_status = (
            "PASSED"
            if rates["report_completeness_rate"] >= target_rate
            else "NOT_MET"
        )
        delivery_status = (
            "PASSED"
            if rates["delivered_report_completeness_rate"] >= target_rate
            else "NOT_MET"
        )
    if report_status == "PASSED" and delivery_status == "PASSED":
        status = "PASSED"
    elif "INSUFFICIENT_EVIDENCE" in {report_status, delivery_status}:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "NOT_MET"

    daily = []
    for day, counts in sorted(day_accumulator.items()):
        denominator = counts["expected_slots"]
        daily.append({
            "date": day,
            **counts,
            "missing_pipeline_slots": (
                denominator - counts["pipeline_present"]),
            "report_completeness_rate": (
                counts["report_complete"] / denominator),
            "delivered_report_completeness_rate": (
                counts["delivered_report_complete"] / denominator),
        })
    return {
        "target_rate": target_rate,
        "minimum_slots": minimum_slots,
        "counts": {
            "expected_slots": expected,
            "pipeline_present": pipeline_present,
            "missing_pipeline_slots": expected - pipeline_present,
            "pipeline_attempts": sum(
                len(attempts.get(cycle, [])) for cycle in expected_cycles),
            "duplicate_pipeline_attempts": sum(
                max(0, len(attempts.get(cycle, [])) - 1)
                for cycle in expected_cycles
            ),
            "archive_attempts_checked": archive_attempts_checked,
            "report_complete": report_complete,
            "report_incomplete": expected - report_complete,
            "delivery_confirmed": delivery_confirmed,
            "delivery_unconfirmed": expected - delivery_confirmed,
            "delivered_report_complete": delivered_report_complete,
            "delivered_report_incomplete": (
                expected - delivered_report_complete),
            "failure_slots": len(failure_rows),
        },
        "rates": rates,
        "statuses": {
            "report_completeness_status": report_status,
            "delivered_report_completeness_status": delivery_status,
            "overall_status": status,
        },
        "status": status,
        "daily": daily,
        "failure_rows": failure_rows,
    }


def _latency_percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[max(0, min(len(ordered) - 1, index))]


def _pass_rate_recovery_projection(
    *,
    passes: int,
    planned: int,
    target_rate: float,
    minimum_slots: int,
) -> dict:
    """Return a fail-preserving best-case path to the registered rate."""
    if passes < 0 or planned < 0 or passes > planned:
        raise ValueError("invalid pass-rate counts")
    if minimum_slots <= 0:
        raise ValueError("minimum_slots must be positive")
    target = Fraction(str(target_rate))
    if not 0 <= target <= 1:
        raise ValueError("target_rate must be between zero and one")

    baseline_cycles = max(planned, minimum_slots)
    additional_to_baseline = baseline_cycles - planned
    baseline_passes = passes + additional_to_baseline
    baseline_rate = baseline_passes / baseline_cycles

    if target == 0:
        additional_to_target = 0
        finite_recovery_possible = True
    elif planned == 0:
        additional_to_target = 1
        finite_recovery_possible = True
    elif Fraction(passes, planned) >= target:
        additional_to_target = 0
        finite_recovery_possible = True
    elif target == 1:
        additional_to_target = None
        finite_recovery_possible = False
    else:
        required = (target * planned - passes) / (1 - target)
        additional_to_target = max(
            0,
            (required.numerator + required.denominator - 1)
            // required.denominator,
        )
        finite_recovery_possible = True

    return {
        "diagnostic_only": True,
        "assumption": (
            "Every additional mature natural cycle is timely; all observed "
            "late, missing-barrier, and missing-receipt cycles remain failures. "
            "This is not a forecast, trade quota, target change, or authorization."
        ),
        "additional_all_timely_cycles_to_minimum_slots": additional_to_baseline,
        "best_case_at_minimum_or_current_denominator": {
            "expected_slots": baseline_cycles,
            "timely_deliveries": baseline_passes,
            "timely_delivery_rate": round(baseline_rate, 6),
            "target_reachable": Fraction(
                baseline_passes, baseline_cycles) >= target,
        },
        "minimum_additional_all_timely_cycles_to_target": additional_to_target,
        "earliest_total_slots_at_target_if_no_more_failures": (
            planned + additional_to_target
            if additional_to_target is not None else None
        ),
        "earliest_timely_deliveries_at_target_if_no_more_failures": (
            passes + additional_to_target
            if additional_to_target is not None else None
        ),
        "finite_recovery_possible": finite_recovery_possible,
    }


def _delivered_complete_recovery_projection(
    *,
    passes: int,
    planned: int,
    target_rate: float,
    minimum_slots: int,
) -> dict:
    """Return the exact fail-preserving path for Push completeness.

    Reuse the Fraction-based solver used by latency, but expose names and an
    assumption that match complete-and-delivered reports instead of timing.
    """
    base = _pass_rate_recovery_projection(
        passes=passes,
        planned=planned,
        target_rate=target_rate,
        minimum_slots=minimum_slots,
    )
    baseline = base["best_case_at_minimum_or_current_denominator"]
    return {
        "diagnostic_only": True,
        "assumption": (
            "Every additional mature natural cycle has a complete, "
            "independently valid report and matching exact sent receipt; all "
            "observed missing, incomplete, and unconfirmed cycles remain "
            "failures. This is not a forecast, resend plan, target change, "
            "or authorization."
        ),
        "additional_all_success_cycles_to_minimum_slots": base[
            "additional_all_timely_cycles_to_minimum_slots"],
        "best_case_at_minimum_or_current_denominator": {
            "expected_slots": baseline["expected_slots"],
            "delivered_report_complete": baseline["timely_deliveries"],
            "delivered_report_completeness_rate": baseline[
                "timely_delivery_rate"],
            "target_reachable": baseline["target_reachable"],
        },
        "minimum_additional_all_success_cycles_to_target": base[
            "minimum_additional_all_timely_cycles_to_target"],
        "earliest_total_slots_at_target_if_no_more_failures": base[
            "earliest_total_slots_at_target_if_no_more_failures"],
        "earliest_delivered_complete_at_target_if_no_more_failures": base[
            "earliest_timely_deliveries_at_target_if_no_more_failures"],
        "finite_recovery_possible": base["finite_recovery_possible"],
    }


def _push_delivery_latency(
    *,
    expected_cycles: list[str],
    delivered_at: dict[str, datetime],
    stage_status_dir: Path,
    as_of: datetime,
    pipeline_attempts: dict[str, list[dict]] | None = None,
) -> dict:
    """Audit §3 record-complete -> exact ``sent`` latency on a fixed slot set."""
    registration = thresholds.push_latency_registration_facts(as_of)
    activation = _parse_cst(registration["activation_cst"])
    target = registration["target_seconds"]
    active_cycles = [
        cycle for cycle in expected_cycles
        if _parse_cst(cycle) >= activation
    ]
    failure_rows: list[dict] = []
    latencies: list[int] = []
    timely = 0
    reason_counts: dict[str, int] = defaultdict(int)
    anchor_source_counts: dict[str, int] = defaultdict(int)

    def pipeline_anchor(cycle: str) -> datetime | None:
        """Recover the exact barrier only from two matching sent attestations.

        ``stage-status`` is intentionally a short-lived operational surface,
        while ``pipeline_runs.jsonl`` is protected audit evidence.  This
        fallback is used only when the stage file has been physically rotated;
        an existing failed or dirty stage file always remains authoritative.
        """
        rows = (pipeline_attempts or {}).get(cycle) or []
        for row in reversed(rows):
            if (
                row.get("cycle") != cycle
                or row.get("ok") is not True
                or row.get("send_status") != "sent"
            ):
                continue
            steps = row.get("steps")
            if not isinstance(steps, dict):
                continue
            anchors: list[datetime] = []
            valid = True
            for key in (
                "business_attestation_pre_archive",
                "business_attestation_pre_send",
            ):
                attestation = steps.get(key)
                if not isinstance(attestation, dict) or (
                    attestation.get("ok") is not True
                    or attestation.get("required") is not True
                    or attestation.get("mode") != "business_terminal"
                ):
                    valid = False
                    break
                stage = attestation.get("live_stage_terminal")
                barrier = (
                    stage.get("report_reconcile_barrier")
                    if isinstance(stage, dict) else None
                )
                if not isinstance(stage, dict) or (
                    stage.get("status") != "succeeded"
                    or type(stage.get("returncode")) is not int
                    or stage.get("returncode") != 0
                    or stage.get("profile_lease_released") is not True
                    or stage.get("same_cycle_active_lease") is not False
                    or not str(stage.get("finished_at") or "").strip()
                ):
                    valid = False
                    break
                if not isinstance(barrier, dict) or (
                    barrier.get("required") is not True
                    or barrier.get("profile") != "live"
                    or barrier.get("cycle_id") != cycle
                    or barrier.get("status") not in {"ok", "applied"}
                    or type(barrier.get("rc")) is not int
                    or barrier.get("rc") != 0
                    or barrier.get("blocking") is not False
                    or barrier.get("contract_valid") is not True
                    or barrier.get("report_safe") is not True
                ):
                    valid = False
                    break
                try:
                    anchors.append(_parse_cst(str(barrier["finished_at"])))
                except (KeyError, TypeError, ValueError):
                    valid = False
                    break
            if valid and len(anchors) == 2 and anchors[0] == anchors[1]:
                return anchors[0]
        return None

    for cycle in active_cycles:
        safe = cycle.replace(":", "-")
        try:
            live = json.loads(
                (stage_status_dir / f"live-{safe}.json").read_text(
                    encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            live = None
        reason = None
        anchor = None
        barrier = (
            live.get("report_reconcile_barrier")
            if isinstance(live, dict) else None
        )
        if not isinstance(live, dict):
            anchor = pipeline_anchor(cycle)
            if anchor is None:
                reason = "live_stage_status_missing"
            else:
                anchor_source_counts["pipeline_attestation_fallback"] += 1
        elif (
            live.get("status") != "succeeded"
            or type(live.get("returncode")) is not int
            or live.get("returncode") != 0
        ):
            reason = "live_stage_not_succeeded"
        elif not isinstance(barrier, dict):
            reason = "record_reconcile_barrier_missing"
        elif (
            barrier.get("required") is not True
            or barrier.get("profile") != "live"
            or barrier.get("cycle_id") != cycle
            or barrier.get("status") not in {"ok", "applied"}
            or type(barrier.get("rc")) is not int
            or barrier.get("rc") != 0
            or barrier.get("contract_valid") is not True
            or barrier.get("report_safe") is not True
        ):
            reason = "record_reconcile_barrier_not_clean"
        else:
            try:
                anchor = _parse_cst(str(barrier["finished_at"]))
                anchor_source_counts["stage_status"] += 1
            except (KeyError, TypeError, ValueError):
                reason = "record_reconcile_completion_ts_invalid"

        receipt_at = delivered_at.get(cycle)
        latency = None
        if reason is None and receipt_at is None:
            reason = "exact_delivery_receipt_missing"
        elif reason is None and anchor is not None and receipt_at is not None:
            latency = int((receipt_at - anchor).total_seconds())
            if latency < 0:
                reason = "delivery_before_record_completion"
                latency = None
            else:
                latencies.append(latency)
                if target is not None and latency <= int(target):
                    timely += 1
                else:
                    reason = "delivery_latency_above_target"

        if reason is not None:
            reason_counts[reason] += 1
            failure_rows.append({
                "cycle": cycle,
                "reason": reason,
                "latency_seconds": latency,
            })

    denominator = len(active_cycles)
    timely_rate = timely / denominator if denominator else None
    required_rate = float(registration["required_pass_rate"])
    minimum_slots = int(registration["minimum_slots"])
    recovery_projection = _pass_rate_recovery_projection(
        passes=timely,
        planned=denominator,
        target_rate=required_rate,
        minimum_slots=minimum_slots,
    )
    if target is None:
        status = "NOT_ACTIVE"
    elif denominator < minimum_slots:
        status = "PENDING_FORWARD_EVIDENCE"
    elif timely_rate is not None and timely_rate >= required_rate:
        status = "PASSED"
    else:
        status = "NOT_MET"
    return {
        "registration": registration,
        "window": {
            "start_cst": registration["activation_cst"],
            "end_cycle_inclusive": active_cycles[-1] if active_cycles else None,
            "expected_slots": denominator,
        },
        "counts": {
            "expected_slots": denominator,
            "measured_deliveries": len(latencies),
            "timely_deliveries": timely,
            "failures": denominator - timely,
        },
        "rates": {
            "measurement_coverage_rate": (
                len(latencies) / denominator if denominator else None),
            "timely_delivery_rate": timely_rate,
        },
        "latency_seconds": {
            "p50": _latency_percentile(latencies, 0.50),
            "p90": _latency_percentile(latencies, 0.90),
            "p95": _latency_percentile(latencies, 0.95),
            "p99": _latency_percentile(latencies, 0.99),
            "max": max(latencies) if latencies else None,
        },
        "failure_reason_counts": dict(sorted(reason_counts.items())),
        "anchor_source_counts": dict(sorted(anchor_source_counts.items())),
        "stage_status_retention_fallback": (
            "only when the exact stage-status file is absent, accept the "
            "matching clean business-terminal barrier timestamp independently "
            "preserved in both pre-archive and pre-send sent-pipeline "
            "attestations; existing failed or dirty stage status never falls "
            "back"
        ),
        "failure_rows": failure_rows,
        "recovery_projection": recovery_projection,
        "status": status,
    }


def audit_push_completeness(
    *,
    start: date,
    end: date,
    pipeline_log: Path,
    event_log: Path,
    dedupe_db: Path,
    reports_dir: Path,
    stage_status_dir: Path = Path(_public_project_path('logs', 'stage-status')),
    archive_validator: Callable[[str], dict] = validate_push_format.validate,
    evaluated_at: str | None = None,
    forward_start: datetime | None = None,
    as_of: datetime | None = None,
    forward_minimum_slots: int = 96,
    finality_grace_minutes: int = 45,
) -> dict:
    """Return strict report and exact-delivery rates for every planned slot."""
    if forward_minimum_slots <= 0:
        raise ValueError("forward_minimum_slots must be positive")
    if finality_grace_minutes < 15:
        raise ValueError("finality_grace_minutes must be at least 15")
    rolling_cycles = _expected_cycles(start, end)
    effective_as_of = (as_of or datetime.now(CST)).astimezone(CST)
    target_rate = thresholds.coverage_target_rate(effective_as_of)
    if not 0 < target_rate <= 1:
        raise ValueError("target rate must be in (0,1]")
    forward_cycles: list[str] = []
    forward_end_exclusive: datetime | None = None
    if forward_start is not None:
        forward_start = forward_start.astimezone(CST)
        _ensure_slot_aligned(forward_start, label="forward_start")
        forward_end_exclusive = _completed_end_exclusive(
            effective_as_of, finality_grace_minutes)
        forward_cycles = _expected_cycles_between(
            forward_start,
            max(forward_start, forward_end_exclusive),
        )
    all_cycles = sorted(set(rolling_cycles).union(forward_cycles))
    expected_set = set(all_cycles)
    attempts, pipeline_diagnostics = _pipeline_attempts(
        pipeline_log, expected_set
    )
    receipts, delivered_at, delivery_diagnostics = _delivery_receipts(
        expected_cycles=all_cycles,
        dedupe_db=dedupe_db,
        event_log=event_log,
    )
    rolling = _summarize_cycles(
        expected_cycles=rolling_cycles,
        attempts=attempts,
        receipts=receipts,
        reports_dir=reports_dir,
        archive_validator=archive_validator,
        target_rate=target_rate,
        minimum_slots=len(rolling_cycles),
    )
    latency_cycles = forward_cycles if forward_start is not None else rolling_cycles
    delivery_latency = _push_delivery_latency(
        expected_cycles=latency_cycles,
        delivered_at=delivered_at,
        stage_status_dir=stage_status_dir,
        as_of=effective_as_of,
        pipeline_attempts=attempts,
    )
    payload = {
        "schema_version": 2,
        "artifact_type": "push_report_and_delivery_completeness_audit",
        "evaluated_at_cst": evaluated_at or datetime.now(CST).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "mode": "read_only_business_data",
        "window": {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "end_inclusive": True,
            "completed_calendar_days": True,
            "days": len(_days_inclusive(start, end)),
            "schedule_minutes": SCHEDULE_MINUTES,
            "expected_slots": len(rolling_cycles),
        },
        "metric_definitions": {
            "report_complete": (
                "planned slot has a production archive whose logged build, "
                "render, validate and archive hard checks pass, whose path and "
                "byte count are valid, and whose Markdown independently "
                "passes the pure format validator; from "
                f"{BUSINESS_ATTESTATION_REQUIRED_FROM}, both pre-archive and "
                "pre-send live-terminal/business attestations must also pass "
                "and agree; from "
                f"{INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM}, both "
                "attestations must additionally agree on the half-open "
                "inter-report exchange-fill interval, count, and SHA-256"
            ),
            "delivery_confirmed": (
                "exact sha256(default|push:{cycle}) identity has an explicit "
                "sent receipt; pending, failed and unproven duplicate skips "
                "do not count"
            ),
            "delivered_report_complete": (
                "sent receipt content_hash matches an independently valid "
                "production archive for the same planned cycle"
            ),
            "push_delivery_latency": (
                "successful live report reconcile barrier finished_at to the "
                "same-cycle exact sent receipt updated_at; every registered "
                "planned slot stays in the denominator"
            ),
        },
        "target_rate": target_rate,
        "target_rate_migration": thresholds.coverage_migration_facts(
            effective_as_of),
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics(
            dict(rolling["rates"])),
        "counts": rolling["counts"],
        "rates": rolling["rates"],
        "statuses": rolling["statuses"],
        "status": rolling["status"],
        "daily": rolling["daily"],
        "failure_rows": rolling["failure_rows"],
        "delivery_latency": delivery_latency,
        "diagnostics": {
            "pipeline": pipeline_diagnostics,
            "delivery": delivery_diagnostics,
        },
        "inputs": {
            "pipeline_log": str(pipeline_log),
            "delivery_event_log": str(event_log),
            "dedupe_db": str(dedupe_db),
            "production_reports_dir": str(reports_dir),
            "stage_status_dir": str(stage_status_dir),
        },
        "safety": {
            "auto_resend": False,
            "historical_backfill": False,
            "production_database_writes": 0,
            "production_report_mutation": False,
            "production_threshold_change_allowed": False,
            "production_order_authorized": False,
            "orders_placed": 0,
        },
    }
    if forward_start is not None and forward_end_exclusive is not None:
        forward = _summarize_cycles(
            expected_cycles=forward_cycles,
            attempts=attempts,
            receipts=receipts,
            reports_dir=reports_dir,
            archive_validator=archive_validator,
            target_rate=target_rate,
            minimum_slots=forward_minimum_slots,
        )
        forward["recovery_projection"] = (
            _delivered_complete_recovery_projection(
                passes=forward["counts"]["delivered_report_complete"],
                planned=forward["counts"]["expected_slots"],
                target_rate=target_rate,
                minimum_slots=forward_minimum_slots,
            )
        )
        payload["as_of_cst"] = effective_as_of.isoformat()
        payload["forward_start_cst"] = forward_start.isoformat()
        payload["slot_finality_grace_minutes"] = finality_grace_minutes
        payload["forward_legacy_target_diagnostics"] = (
            thresholds.legacy_rate_diagnostics(dict(forward["rates"])))
        payload["forward_after_remediation"] = {
            "start_cst": forward_start.isoformat(),
            "end_exclusive_cst": max(
                forward_start, forward_end_exclusive).isoformat(),
            **forward,
        }
        if rolling["status"] == "PASSED" and forward["status"] == "PASSED":
            payload["overall_status"] = "PASSED"
        elif forward["status"] == "INSUFFICIENT_EVIDENCE":
            payload["overall_status"] = "PENDING_FORWARD_EVIDENCE"
        else:
            payload["overall_status"] = "NOT_MET"
    else:
        payload["overall_status"] = rolling["status"]
    latency_status = str(delivery_latency.get("status") or "NOT_ACTIVE")
    payload["statuses"]["push_delivery_latency_status"] = latency_status
    if latency_status == "NOT_MET":
        payload["overall_status"] = "NOT_MET"
    elif (
        latency_status == "PENDING_FORWARD_EVIDENCE"
        and payload["overall_status"] == "PASSED"
    ):
        payload["overall_status"] = "PENDING_FORWARD_EVIDENCE"
    return payload


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
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
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="read-only strict Push report and delivery completeness audit"
    )
    dates = parser.add_mutually_exclusive_group()
    dates.add_argument(
        "--days",
        type=int,
        default=14,
        help="completed calendar days ending yesterday (default: 14)",
    )
    dates.add_argument("--start", type=_parse_day)
    parser.add_argument("--end", type=_parse_day)
    parser.add_argument(
        "--pipeline-log", default=_public_project_path('logs', 'push', 'pipeline_runs.jsonl')
    )
    parser.add_argument(
        "--event-log", default=_public_project_path('logs', 'push', 'qq_push_dedupe.jsonl')
    )
    parser.add_argument(
        "--dedupe-db", default=_public_project_path('db', 'qq_push_dedupe.db')
    )
    parser.add_argument(
        "--reports-dir", default=_public_project_path('reports', 'agents')
    )
    parser.add_argument(
        "--stage-status-dir", default=_public_project_path('logs', 'stage-status')
    )
    parser.add_argument(
        "--forward-start",
        default=DEFAULT_FORWARD_START,
        help="pre-registered CST remediation start (default: 2026-08-12 16:00)",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="CST/ISO evaluation time; default now",
    )
    parser.add_argument("--forward-minimum-slots", type=int, default=96)
    parser.add_argument("--finality-grace-minutes", type=int, default=45)
    parser.add_argument(
        "--json-out",
        default=_public_project_path('reports', 'quality', 'push-completeness-audit.json'),
    )
    parser.add_argument("--execution-context", choices=("production", "test", "probe"))
    parser.add_argument("--artifact-root")
    args = parser.parse_args(argv)
    if args.start is None and args.end is not None:
        parser.error("--end requires --start")
    if args.days is not None and args.days <= 0:
        parser.error("--days must be positive")
    if args.forward_minimum_slots <= 0:
        parser.error("--forward-minimum-slots must be positive")
    if args.finality_grace_minutes < 15:
        parser.error("--finality-grace-minutes must be at least 15")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    yesterday = datetime.now(CST).date() - timedelta(days=1)
    if args.start is not None:
        start = args.start
        end = args.end or yesterday
    else:
        end = yesterday
        start = end - timedelta(days=args.days - 1)
    if end > yesterday:
        print(json.dumps({
            "ok": False,
            "error": "window must contain completed Beijing calendar days only",
            "latest_allowed_end": yesterday.isoformat(),
        }, ensure_ascii=False), file=sys.stderr)
        return 2

    required = {
        "pipeline_log": Path(args.pipeline_log),
        "event_log": Path(args.event_log),
        "dedupe_db": Path(args.dedupe_db),
        "reports_dir": Path(args.reports_dir),
        "stage_status_dir": Path(args.stage_status_dir),
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        print(json.dumps({
            "ok": False,
            "error": "missing input",
            "paths": missing,
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        result = audit_push_completeness(
            start=start,
            end=end,
            forward_start=(
                _parse_cst(args.forward_start)
                if args.forward_start else None
            ),
            as_of=_parse_cst(args.as_of) if args.as_of else datetime.now(CST),
            forward_minimum_slots=args.forward_minimum_slots,
            finality_grace_minutes=args.finality_grace_minutes,
            **required,
        )
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.json_out:
        output, context, context_fields = resolve_audit_output(
            args.json_out,
            tool_name="audit_push_completeness",
            execution_context=args.execution_context,
            artifact_root=args.artifact_root,
        )
        result["artifact_context"] = context_fields
        _atomic_write_json(output, result)
    else:
        output, context = None, None
    print(json.dumps({
        "ok": True,
        "artifact": str(output) if output else None,
        "execution_context": context,
        "window": result["window"],
        "counts": result["counts"],
        "rates": result["rates"],
        "statuses": result["statuses"],
        "overall_status": result["overall_status"],
        "forward_after_remediation": ({
            "start_cst": result["forward_after_remediation"]["start_cst"],
            "end_exclusive_cst": (
                result["forward_after_remediation"]["end_exclusive_cst"]),
            "minimum_slots": (
                result["forward_after_remediation"]["minimum_slots"]),
            "counts": result["forward_after_remediation"]["counts"],
            "rates": result["forward_after_remediation"]["rates"],
            "statuses": result["forward_after_remediation"]["statuses"],
            "recovery_projection": result[
                "forward_after_remediation"]["recovery_projection"],
        } if "forward_after_remediation" in result else None),
        "delivery_latency": result["delivery_latency"],
        "safety": result["safety"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
