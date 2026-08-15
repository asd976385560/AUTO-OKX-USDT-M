# -*- coding: utf-8 -*-
"""Refresh live quality evidence in the goal acceptance report artifact."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _acceptance_thresholds as thresholds


CST = timezone(timedelta(hours=8))


def _coverage_target_pct(now: datetime | None = None) -> str:
    """当前生效的完善率/完整度闸门（预注册激活边界解析，四族同一数值）。"""
    return f"{thresholds.coverage_target_rate(now or datetime.now(CST)):.0%}"


def _credibility_target_pct(now: datetime | None = None) -> str:
    """当前生效的前向校准门（预注册激活边界解析；点精度与 Wilson 同值）。"""
    return f"{thresholds.shadow_target_precision(now or datetime.now(CST)):.0%}"
CONTRACT_DIRECT_METHODS = {
    "rubik_common_bucket",
    "official_public_oi_trades_candle_reconciled_fallback",
}


def _validated_contract_coverage_split(
    evidence: dict,
    *,
    label: str,
) -> dict[str, int | float]:
    """Fail closed unless direct, carried and available coverage reconcile."""
    required = (
        "universe_symbols", "valid_symbols", "coverage_rate",
        "direct_valid_symbols", "direct_coverage_rate",
        "carried_forward_valid_symbols", "carry_forward_rate",
        "valid_method_counts", "carry_forward_semantics",
    )
    missing = [field for field in required if field not in evidence]
    if missing:
        raise ValueError(
            f"{label} contract-statistics split fields missing: {missing}")
    universe = int(evidence["universe_symbols"])
    valid = int(evidence["valid_symbols"])
    direct = int(evidence["direct_valid_symbols"])
    carried = int(evidence["carried_forward_valid_symbols"])
    coverage = float(evidence["coverage_rate"])
    direct_rate = float(evidence["direct_coverage_rate"])
    carry_rate = float(evidence["carry_forward_rate"])
    if universe <= 0 or min(valid, direct, carried) < 0:
        raise ValueError(f"{label} contract-statistics split counts invalid")
    if direct + carried != valid or valid > universe:
        raise ValueError(
            f"{label} contract-statistics direct+carry does not equal valid")
    expected_rates = (
        valid / universe, direct / universe, carried / universe,
    )
    for name, observed, expected in zip(
        ("coverage_rate", "direct_coverage_rate", "carry_forward_rate"),
        (coverage, direct_rate, carry_rate),
        expected_rates,
    ):
        if not math.isfinite(observed) or not math.isclose(
            observed, expected, rel_tol=1e-12, abs_tol=1e-12,
        ):
            raise ValueError(
                f"{label} contract-statistics {name} disagrees with counts")
    method_counts = evidence["valid_method_counts"]
    if not isinstance(method_counts, dict):
        raise ValueError(
            f"{label} contract-statistics valid_method_counts invalid")
    direct_methods = sum(
        int(method_counts.get(method, 0)) for method in CONTRACT_DIRECT_METHODS)
    carry_methods = int(
        method_counts.get("official_previous_batch_carry_forward", 0))
    if direct_methods != direct or carry_methods != carried:
        raise ValueError(
            f"{label} contract-statistics method counts disagree with split")
    semantics = str(evidence["carry_forward_semantics"])
    if (
        "excluded from model features" not in semantics
        or "not counted as direct" not in semantics
    ):
        raise ValueError(
            f"{label} contract-statistics carry semantics are incomplete")
    return {
        "universe": universe,
        "valid": valid,
        "direct": direct,
        "carried": carried,
        "coverage": coverage,
        "direct_rate": direct_rate,
        "carry_rate": carry_rate,
    }


def _require_contract_quarter_cycle(cycle_id: str, *, label: str) -> None:
    try:
        parsed = datetime.strptime(str(cycle_id), "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError(f"{label} contract-statistics cycle invalid") from exc
    if parsed.minute % 15 != 0:
        raise ValueError(
            f"{label} contract-statistics cycle is not a 15m boundary")


def _one(items: list[dict], key: str, value: str) -> dict:
    matches = [item for item in items if item.get(key) == value]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {key}={value!r}, got {len(matches)}")
    return matches[0]


def _sync_title_block(manifest: dict) -> None:
    """Keep the visible first heading aligned with the report manifest title."""
    matches = [
        block for block in manifest.get("blocks", [])
        if block.get("id") == "title"
    ]
    if len(matches) > 1:
        raise ValueError("duplicate title blocks")
    if matches:
        matches[0]["body"] = f"# {manifest['title']}"


def _upsert_id(items: list[dict], item_id: str) -> dict:
    matches = [item for item in items if item.get("id") == item_id]
    if len(matches) > 1:
        raise ValueError(f"duplicate id={item_id!r}")
    if matches:
        return matches[0]
    item = {"id": item_id}
    items.append(item)
    return item


def _upsert_key(
    items: list[dict], key: str, value: str,
) -> dict:
    matches = [item for item in items if item.get(key) == value]
    if len(matches) > 1:
        raise ValueError(f"duplicate {key}={value!r}")
    if matches:
        return matches[0]
    item = {key: value}
    items.append(item)
    return item


def _audit_utc(audit: dict) -> str:
    value = str(audit["evaluated_at_cst"])
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=CST)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_audit_utc(audit: dict) -> str:
    value = str(audit.get("generated_at_cst") or audit["as_of_cst"])
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_report_instant(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _advance_report_generated_at(artifact: dict, candidate: object) -> str:
    """Advance report timestamps monotonically across evidence refresh order."""
    candidate_dt = _parse_report_instant(candidate)
    if candidate_dt is None:
        raise ValueError("invalid report evidence timestamp")
    manifest = artifact["manifest"]
    snapshot = artifact["snapshot"]
    instants = [candidate_dt]
    for existing in (manifest.get("generatedAt"), snapshot.get("generatedAt")):
        parsed = _parse_report_instant(existing)
        if parsed is not None:
            instants.append(parsed)
    latest = max(instants)
    generated_at = latest.isoformat().replace("+00:00", "Z")
    manifest["generatedAt"] = generated_at
    snapshot["generatedAt"] = generated_at
    title = str(
        manifest.get("title") or ". 四项目标实施与前向验收"
    )
    title_suffix = f"（{latest.astimezone(CST).strftime('%Y-%m-%d %H:%M')}）"
    if re.search(r"（[^（）]*）$", title):
        manifest["title"] = re.sub(r"（[^（）]*）$", title_suffix, title)
    else:
        manifest["title"] = title + title_suffix
    _sync_title_block(manifest)
    return generated_at


def refresh_report_completeness(
    artifact: dict,
    audit: dict,
    push_audit: dict,
    *,
    audit_relative_path: str,
    push_audit_relative_path: str,
) -> dict:
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if push_audit.get("artifact_type") != (
        "push_report_and_delivery_completeness_audit"
    ):
        raise ValueError("invalid Push completeness audit artifact type")
    if push_audit.get("mode") != "read_only_business_data":
        raise ValueError("Push completeness audit is not read-only")
    expected = int(audit["expected"])
    valid = int(audit["valid"])
    rate = float(audit["completeness_rate"])
    target = float(audit["target_rate"])
    target_pct = f"{target:.0%}"
    invalid = int(audit.get("invalid", expected - valid))
    if (
        expected <= 0
        or not (0 <= valid <= expected)
        or invalid != expected - valid
        or not math.isfinite(rate)
        or not math.isclose(rate, valid / expected, abs_tol=1e-12)
        or not (0 < target <= 1)
    ):
        raise ValueError("invalid report-completeness audit counts")
    expected_daily_status = "PASSED" if rate >= target else "NOT_MET"
    if str(audit.get("status", expected_daily_status)) != expected_daily_status:
        raise ValueError("daily report status disagrees with counts")
    for field, expected_value in (
        ("auto_send", False),
        ("database_write", False),
        ("production_order_authorized", False),
    ):
        if audit.get(field) is not expected_value:
            raise ValueError(f"daily report audit unsafe field: {field}")

    push_window = push_audit.get("window") or {}
    push_counts = push_audit.get("counts") or {}
    push_rates = push_audit.get("rates") or {}
    push_statuses = push_audit.get("statuses") or {}
    push_safety = push_audit.get("safety") or {}
    push_expected = int(push_counts.get("expected_slots", -1))
    push_present = int(push_counts.get("pipeline_present", -1))
    push_missing = int(push_counts.get("missing_pipeline_slots", -1))
    push_report_complete = int(push_counts.get("report_complete", -1))
    push_report_incomplete = int(push_counts.get("report_incomplete", -1))
    push_delivery_confirmed = int(push_counts.get("delivery_confirmed", -1))
    push_delivery_unconfirmed = int(
        push_counts.get("delivery_unconfirmed", -1))
    push_exact_complete = int(
        push_counts.get("delivered_report_complete", -1))
    push_exact_incomplete = int(
        push_counts.get("delivered_report_incomplete", -1))
    push_failure_slots = int(push_counts.get("failure_slots", -1))
    push_attempts = int(push_counts.get("pipeline_attempts", -1))
    push_duplicate_attempts = int(
        push_counts.get("duplicate_pipeline_attempts", -1))
    push_archive_attempts = int(
        push_counts.get("archive_attempts_checked", -1))
    push_days = int(push_window.get("days", -1))
    push_schedule = int(push_window.get("schedule_minutes", -1))
    if (
        push_expected <= 0
        or push_days <= 0
        or push_schedule != 15
        or push_expected != push_days * 96
        or int(push_window.get("expected_slots", -1)) != push_expected
        or push_window.get("completed_calendar_days") is not True
        or not (
            0 <= push_exact_complete <= push_report_complete
            <= push_present <= push_expected
        )
        or not (0 <= push_delivery_confirmed <= push_expected)
        or push_missing != push_expected - push_present
        or push_report_incomplete != push_expected - push_report_complete
        or push_delivery_unconfirmed != push_expected - push_delivery_confirmed
        or push_exact_incomplete != push_expected - push_exact_complete
        or push_failure_slots != push_expected - push_exact_complete
        or push_attempts < push_present
        or push_duplicate_attempts != push_attempts - push_present
        or push_archive_attempts != push_attempts
    ):
        raise ValueError("invalid Push completeness audit counts")
    push_report_rate = float(push_rates.get("report_completeness_rate", -1))
    push_delivery_rate = float(
        push_rates.get("delivered_report_completeness_rate", -1))
    push_presence_rate = float(push_rates.get("pipeline_presence_rate", -1))
    push_receipt_rate = float(
        push_rates.get("delivery_confirmation_rate", -1))
    for label, observed, numerator in (
        ("pipeline presence", push_presence_rate, push_present),
        ("report completeness", push_report_rate, push_report_complete),
        ("delivery confirmation", push_receipt_rate, push_delivery_confirmed),
        ("delivered report completeness", push_delivery_rate,
         push_exact_complete),
    ):
        if not math.isfinite(observed) or not math.isclose(
            observed, numerator / push_expected, abs_tol=1e-12,
        ):
            raise ValueError(f"Push {label} rate disagrees with counts")
    push_target = float(push_audit.get("target_rate", -1))
    if not math.isclose(push_target, target, abs_tol=1e-12):
        raise ValueError("Push and daily report targets disagree")
    expected_push_report_status = (
        "PASSED" if push_report_rate >= push_target else "NOT_MET")
    expected_push_delivery_status = (
        "PASSED" if push_delivery_rate >= push_target else "NOT_MET")
    expected_push_overall = (
        "PASSED"
        if expected_push_report_status == "PASSED"
        and expected_push_delivery_status == "PASSED"
        else "NOT_MET"
    )
    if (
        str(push_statuses.get("report_completeness_status"))
        != expected_push_report_status
        or str(push_statuses.get("delivered_report_completeness_status"))
        != expected_push_delivery_status
        or str(push_statuses.get("overall_status")) != expected_push_overall
        or str(push_audit.get("status")) != expected_push_overall
    ):
        raise ValueError("Push completeness statuses disagree with counts")
    expected_push_safety = {
        "auto_resend": False,
        "historical_backfill": False,
        "production_database_writes": 0,
        "production_report_mutation": False,
        "production_threshold_change_allowed": False,
        "production_order_authorized": False,
        "orders_placed": 0,
    }
    for field, expected_value in expected_push_safety.items():
        if push_safety.get(field) != expected_value:
            raise ValueError(f"Push completeness audit unsafe field: {field}")
    daily_rows = push_audit.get("daily")
    if not isinstance(daily_rows, list) or len(daily_rows) != push_days:
        raise ValueError("Push daily rows disagree with window")
    daily_dates = [str(row.get("date")) for row in daily_rows]
    if len(set(daily_dates)) != push_days:
        raise ValueError("Push daily rows contain duplicate dates")
    for row in daily_rows:
        row_expected = int(row.get("expected_slots", -1))
        row_present = int(row.get("pipeline_present", -1))
        row_report = int(row.get("report_complete", -1))
        row_exact = int(row.get("delivered_report_complete", -1))
        if (
            row_expected != 96
            or not (0 <= row_exact <= row_report <= row_present <= row_expected)
            or int(row.get("missing_pipeline_slots", -1))
            != row_expected - row_present
            or not math.isclose(
                float(row.get("report_completeness_rate", -1)),
                row_report / row_expected,
                abs_tol=1e-12,
            )
            or not math.isclose(
                float(row.get("delivered_report_completeness_rate", -1)),
                row_exact / row_expected,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("invalid Push daily completeness row")
    if (
        sum(int(row["pipeline_present"]) for row in daily_rows) != push_present
        or sum(int(row["report_complete"]) for row in daily_rows)
        != push_report_complete
        or sum(int(row["delivered_report_complete"]) for row in daily_rows)
        != push_exact_complete
    ):
        raise ValueError("Push daily rows do not reconcile to total counts")

    failure_rows = push_audit.get("failure_rows")
    if (
        not isinstance(failure_rows, list)
        or len(failure_rows) != push_failure_slots
        or len({str(row.get("cycle")) for row in failure_rows})
        != push_failure_slots
        or any(
            row.get("delivered_report_complete") is not False
            or not isinstance(row.get("reasons"), list)
            or not row.get("reasons")
            for row in failure_rows
        )
    ):
        raise ValueError("Push failure rows disagree with counts")

    forward = push_audit.get("forward_after_remediation")
    if not isinstance(forward, dict):
        raise ValueError("Push forward evidence is required")
    if str(push_audit.get("forward_start_cst")) != (
        "2026-08-12T16:00:00+08:00"
    ):
        raise ValueError("Push forward start disagrees with preregistration")
    if int(push_audit.get("slot_finality_grace_minutes", -1)) != 45:
        raise ValueError("Push finality grace must remain 45 minutes")
    try:
        forward_start_dt = datetime.fromisoformat(str(forward["start_cst"]))
        forward_end_dt = datetime.fromisoformat(
            str(forward["end_exclusive_cst"]))
        push_as_of_dt = datetime.fromisoformat(str(push_audit["as_of_cst"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid Push forward timestamps") from exc
    if (
        forward_start_dt.tzinfo is None
        or forward_end_dt.tzinfo is None
        or push_as_of_dt.tzinfo is None
    ):
        raise ValueError("Push forward timestamps must be timezone-aware")
    forward_start_dt = forward_start_dt.astimezone(CST)
    forward_end_dt = forward_end_dt.astimezone(CST)
    push_as_of_dt = push_as_of_dt.astimezone(CST)
    preregistered_start = datetime.fromisoformat(
        "2026-08-12T16:00:00+08:00").astimezone(CST)
    if forward_start_dt != preregistered_start:
        raise ValueError("Push forward window start is not preregistered start")
    if (
        forward_start_dt.second
        or forward_start_dt.microsecond
        or forward_start_dt.minute % 15
        or forward_end_dt.second
        or forward_end_dt.microsecond
        or forward_end_dt.minute % 15
        or forward_end_dt < forward_start_dt
    ):
        raise ValueError("Push forward window is not 15-minute aligned")
    expected_forward_end = (
        push_as_of_dt - timedelta(minutes=45)
    ).replace(
        minute=(push_as_of_dt - timedelta(minutes=45)).minute // 15 * 15,
        second=0,
        microsecond=0,
    ) + timedelta(minutes=15)
    expected_forward_end = max(preregistered_start, expected_forward_end)
    if forward_end_dt != expected_forward_end:
        raise ValueError("Push forward end disagrees with finality grace")

    forward_target = float(forward.get("target_rate", -1))
    forward_minimum = int(forward.get("minimum_slots", -1))
    forward_counts = forward.get("counts") or {}
    forward_rates = forward.get("rates") or {}
    forward_statuses = forward.get("statuses") or {}
    forward_expected = int(forward_counts.get("expected_slots", -1))
    forward_present = int(forward_counts.get("pipeline_present", -1))
    forward_report_complete = int(
        forward_counts.get("report_complete", -1))
    forward_delivery_confirmed = int(
        forward_counts.get("delivery_confirmed", -1))
    forward_exact_complete = int(
        forward_counts.get("delivered_report_complete", -1))
    forward_attempts = int(forward_counts.get("pipeline_attempts", -1))
    forward_duplicates = int(
        forward_counts.get("duplicate_pipeline_attempts", -1))
    forward_archive_attempts = int(
        forward_counts.get("archive_attempts_checked", -1))
    forward_failures = int(forward_counts.get("failure_slots", -1))
    duration_seconds = (forward_end_dt - forward_start_dt).total_seconds()
    if (
        not math.isclose(forward_target, push_target, abs_tol=1e-12)
        or forward_minimum != 96
        or duration_seconds < 0
        or duration_seconds % (15 * 60) != 0
        or forward_expected != int(duration_seconds // (15 * 60))
        or not (
            0 <= forward_exact_complete <= forward_report_complete
            <= forward_present <= forward_expected
        )
        or not (0 <= forward_delivery_confirmed <= forward_expected)
        or int(forward_counts.get("missing_pipeline_slots", -1))
        != forward_expected - forward_present
        or int(forward_counts.get("report_incomplete", -1))
        != forward_expected - forward_report_complete
        or int(forward_counts.get("delivery_unconfirmed", -1))
        != forward_expected - forward_delivery_confirmed
        or int(forward_counts.get("delivered_report_incomplete", -1))
        != forward_expected - forward_exact_complete
        or forward_failures != forward_expected - forward_exact_complete
        or forward_attempts < forward_present
        or forward_duplicates != forward_attempts - forward_present
        or forward_archive_attempts != forward_attempts
    ):
        raise ValueError("invalid Push forward completeness counts")
    forward_report_rate = float(
        forward_rates.get("report_completeness_rate", -1))
    forward_delivery_rate = float(
        forward_rates.get("delivered_report_completeness_rate", -1))
    for label, observed, numerator in (
        ("pipeline presence", forward_rates.get("pipeline_presence_rate", -1),
         forward_present),
        ("report completeness", forward_report_rate,
         forward_report_complete),
        ("delivery confirmation",
         forward_rates.get("delivery_confirmation_rate", -1),
         forward_delivery_confirmed),
        ("delivered report completeness", forward_delivery_rate,
         forward_exact_complete),
    ):
        expected_rate = numerator / forward_expected if forward_expected else 0.0
        try:
            observed_rate = float(observed)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid Push forward {label} rate") from exc
        if not math.isfinite(observed_rate) or not math.isclose(
            observed_rate, expected_rate, abs_tol=1e-12,
        ):
            raise ValueError(
                f"Push forward {label} rate disagrees with counts")
    if forward_expected < forward_minimum:
        expected_forward_report_status = "INSUFFICIENT_EVIDENCE"
        expected_forward_delivery_status = "INSUFFICIENT_EVIDENCE"
    else:
        expected_forward_report_status = (
            "PASSED" if forward_report_rate >= forward_target else "NOT_MET")
        expected_forward_delivery_status = (
            "PASSED" if forward_delivery_rate >= forward_target else "NOT_MET")
    expected_forward_status = (
        "PASSED"
        if expected_forward_report_status == "PASSED"
        and expected_forward_delivery_status == "PASSED"
        else "INSUFFICIENT_EVIDENCE"
        if "INSUFFICIENT_EVIDENCE" in {
            expected_forward_report_status, expected_forward_delivery_status}
        else "NOT_MET"
    )
    if (
        str(forward_statuses.get("report_completeness_status"))
        != expected_forward_report_status
        or str(forward_statuses.get("delivered_report_completeness_status"))
        != expected_forward_delivery_status
        or str(forward_statuses.get("overall_status"))
        != expected_forward_status
        or str(forward.get("status")) != expected_forward_status
    ):
        raise ValueError("Push forward statuses disagree with counts")
    expected_combined_status = (
        "PASSED"
        if expected_push_overall == "PASSED"
        and expected_forward_status == "PASSED"
        else "PENDING_FORWARD_EVIDENCE"
        if expected_forward_status == "INSUFFICIENT_EVIDENCE"
        else "NOT_MET"
    )
    if str(push_audit.get("overall_status")) != expected_combined_status:
        raise ValueError("Push combined status disagrees with both windows")
    forward_daily = forward.get("daily")
    if not isinstance(forward_daily, list):
        raise ValueError("Push forward daily rows missing")
    if (
        sum(int(row.get("expected_slots", -1)) for row in forward_daily)
        != forward_expected
        or sum(int(row.get("pipeline_present", -1)) for row in forward_daily)
        != forward_present
        or sum(int(row.get("report_complete", -1)) for row in forward_daily)
        != forward_report_complete
        or sum(
            int(row.get("delivered_report_complete", -1))
            for row in forward_daily
        ) != forward_exact_complete
    ):
        raise ValueError("Push forward daily rows do not reconcile")
    forward_failure_rows = forward.get("failure_rows")
    if (
        not isinstance(forward_failure_rows, list)
        or len(forward_failure_rows) != forward_failures
        or len({str(row.get("cycle")) for row in forward_failure_rows})
        != forward_failures
    ):
        raise ValueError("Push forward failure rows disagree with counts")

    daily_passed = expected_daily_status == "PASSED"
    push_report_passed = expected_push_report_status == "PASSED"
    push_delivery_passed = expected_push_delivery_status == "PASSED"
    forward_passed = expected_forward_status == "PASSED"
    passed = (
        daily_passed and push_report_passed and push_delivery_passed
        and forward_passed
    )
    generated_at = _advance_report_generated_at(artifact, _audit_utc(audit))
    generated_at = _advance_report_generated_at(
        artifact, _audit_utc(push_audit))
    start = str(audit["window"]["start_date"])
    end = str(audit["window"]["end_date"])
    rate_pct = rate * 100
    push_start = str(push_window["start_date"])
    push_end = str(push_window["end_date"])
    push_report_pct = push_report_rate * 100
    push_delivery_pct = push_delivery_rate * 100
    forward_report_pct = forward_report_rate * 100
    forward_delivery_pct = forward_delivery_rate * 100

    manifest = artifact["manifest"]
    source = _one(manifest["sources"], "id", "report_quality")
    source.update({
        "label": "日报与Push严格计划槽校验当前快照",
        "path": push_audit_relative_path,
        "evidence_paths": [audit_relative_path, push_audit_relative_path],
    })
    source_query = source["query"]
    source_query.update({
        "engine": "Canonical validators + JSONL + SQLite receipts",
        "language": "python/sql",
        "sql": (
            "WITH reviewed(artifact_family,numerator,denominator,"
            "completeness_rate) AS (\n  VALUES\n"
            f"    ('Push 报告完整性',{push_report_complete},"
            f"{push_expected},{push_report_rate:.8f}),\n"
            f"    ('Push 精确送达',{push_exact_complete},"
            f"{push_expected},{push_delivery_rate:.8f}),\n"
            f"    ('日报历史校验',{valid},{expected},{rate:.8f})\n)\n"
            "SELECT * FROM reviewed"
        ),
        "description": (
            "Push按每个北京时间15分钟计划槽重建分母，独立复验生产归档并"
            "要求精确sent回执哈希一致；日报逐日调用canonical validator。"
            "两者缺失工件均进入分母。"
        ),
        "executed_at": generated_at,
        "tables_used": [
            "logs/push/pipeline_runs.jsonl",
            "logs/push/qq_push_dedupe.jsonl",
            "db/qq_push_dedupe.db.sent",
            "reports/agents/v2-push-*.md",
            "account.db.daily_reports",
            "reports/daily-reports",
            audit_relative_path,
            push_audit_relative_path,
        ],
        "filters": [
            f"Push为{push_start}至{push_end}共{push_expected}个计划槽",
            f"日报为{start}至{end}共{expected}份",
            f"Push归档与精确送达、日报各自独立达到{target_pct}",
            "不补推历史缺槽",
        ],
    })

    snapshot = artifact["snapshot"]
    headline = snapshot["datasets"]["headline"][0]
    headline.update({
        "push_report_completeness_rate": push_report_rate,
        "push_report_complete": push_report_complete,
        "push_expected_slots": push_expected,
        "push_missing_slots": push_missing,
        "push_delivery_rate": push_delivery_rate,
        "push_delivery_complete": push_exact_complete,
        "push_validation_rate": push_report_rate,
        "push_rolling_status": expected_push_overall,
        "push_forward_expected_slots": forward_expected,
        "push_forward_minimum_slots": forward_minimum,
        "push_forward_report_rate": forward_report_rate,
        "push_forward_delivery_rate": forward_delivery_rate,
        "push_forward_status": expected_forward_status,
        "push_combined_status": expected_combined_status,
        "daily_report_validation_rate": rate,
        "daily_report_valid": valid,
        "daily_report_expected": expected,
        "report_target_rate": target,
        "report_and_push_gate_passed": passed,
    })
    report_rows = snapshot["datasets"]["report_quality"]
    report_rows[:] = [
        row for row in report_rows
        if row.get("artifact_family") != "最新周报校验"
    ]
    push_report_matches = [
        row for row in report_rows
        if row.get("artifact_family") in {"Push 结构校验", "Push 报告完整性"}
    ]
    if len(push_report_matches) != 1:
        raise ValueError("expected exactly one Push report-quality row")
    push_report_row = push_report_matches[0]
    push_report_row.update({
        "artifact_family": "Push 报告完整性",
        "completeness_rate": push_report_rate,
        "target_rate": push_target,
        "numerator": push_report_complete,
        "denominator": push_expected,
        "latest_status": f"{push_start}至{push_end}严格计划槽",
        "status": "达标" if push_report_passed else "未达标",
    })
    push_delivery_matches = [
        row for row in report_rows
        if row.get("artifact_family") in {"Push 投递确认", "Push 精确送达"}
    ]
    if len(push_delivery_matches) != 1:
        raise ValueError("expected exactly one Push delivery row")
    push_delivery_row = push_delivery_matches[0]
    push_delivery_row.update({
        "artifact_family": "Push 精确送达",
        "completeness_rate": push_delivery_rate,
        "target_rate": push_target,
        "numerator": push_exact_complete,
        "denominator": push_expected,
        "latest_status": "sent回执哈希与独立有效归档精确一致",
        "status": "达标" if push_delivery_passed else "未达标",
    })
    daily_row = _one(report_rows, "artifact_family", "日报历史校验")
    daily_row.update({
        "completeness_rate": rate,
        "target_rate": target,
        "numerator": valid,
        "denominator": expected,
        "latest_status": f"{start}至{end} canonical validator",
        "status": "达标" if daily_passed else "未达标",
    })

    gate = _one(
        snapshot["datasets"]["gates"], "goal", "报告与推送完整度")
    gate.update({
        "current": (
            f"Push报告 {push_report_pct:.3f}%（{push_report_complete}/"
            f"{push_expected}）；精确送达 {push_delivery_pct:.3f}%"
            f"（{push_exact_complete}/{push_expected}）；日报 "
            f"{rate_pct:.3f}%（{valid}/{expected}）；Push前向"
            f"{forward_expected}/{forward_minimum}槽，报告"
            f"{forward_report_pct:.3f}%、精确送达"
            f"{forward_delivery_pct:.3f}%（{expected_forward_status}）"
        ),
        "status": "达标" if passed else "未达标",
        "next_gate": (
            "保持96槽/日和逐日报自然审计；缺失Push只告警，不历史补推；"
            "任何报告修订继续执行dry-run、隔离副本、备份和人工重发评审"
        ),
    })

    push_card = _one(manifest["cards"], "id", "push_card")
    push_card["description"] = (
        "Push完整归档、精确sent送达和逐日报canonical validator完整率。")
    push_card["metrics"] = [
        {
            "label": "Push报告完整率",
            "field": "push_report_completeness_rate",
            "format": "percent",
        },
        {
            "label": "Push精确送达率",
            "field": "push_delivery_rate",
            "format": "percent",
        },
        {
            "label": "日报完整率",
            "field": "daily_report_validation_rate",
            "format": "percent",
        },
        {
            "label": "目标",
            "field": "report_target_rate",
            "format": "percent",
        },
    ]

    blocks = manifest["blocks"]
    executive = _one(blocks, "id", "executive_summary")
    body = executive["body"]
    body = re.sub(
        r"最新日报也已.*?Push结构与投递近14日均高于99%。",
        (
            "日报基线窗口已按canonical validator复核；Push改按完整计划槽"
            f"重建后，报告完整{push_report_complete}/{push_expected}、精确"
            f"送达{push_exact_complete}/{push_expected}，整体未达{target_pct}。"
        ),
        body,
    )
    body = re.sub(
        r"历史日报有效率仍为[0-9.]+%。",
        f"日报基线窗口完整率已为{rate_pct:.3f}%。",
        body,
    )
    executive["body"] = body

    reports_block = _one(blocks, "id", "reports_section")
    reports_block["body"] = (
        (
            f"## 日报与Push三个独立门均达到{target_pct}\n\n"
            if passed else
            f"## 日报已达{target_pct}，Push严格计划槽完整度仍未达标\n\n"
        )
        + f"Push窗口 {push_start} 至 {push_end} 应有{push_expected}槽，"
        f"生产归档经独立复验完整{push_report_complete}槽（"
        f"{push_report_pct:.3f}%），sent回执哈希与有效归档精确一致"
        f"{push_exact_complete}槽（{push_delivery_pct:.3f}%），缺管线记录"
        f"{push_missing}槽。日报窗口 {start} 至 {end} 共{expected}份，"
        f"{valid}/{expected}通过（{rate_pct:.3f}%）。缺失Push只保留故障"
        f"事实和告警，不历史补推、不重推。修复后前向窗当前"
        f"{forward_expected}/{forward_minimum}槽，报告"
        f"{forward_report_pct:.3f}%、精确送达{forward_delivery_pct:.3f}%；"
        + (
            "前向门已通过。"
            if forward_passed else
            f"不足96槽或未达{target_pct}，不得替代长期门。"
        )
        + "报告修订仍要求dry-run、隔离"
        "副本、备份与人工重发评审。"
    )
    gates_block = _one(blocks, "id", "gates_section")
    gates_block["body"] = (
        "## 当前结论与下一验收闸\n\n"
        + (
            "报告与推送完整度三个独立门均达到硬门槛；"
            if passed else
            "日报完整率已达标，但Push报告和精确送达按完整计划槽均未达到"
            f"{target_pct}，所以报告与推送总体门仍未通过；"
        )
        + "全市场影子判断、增强版时间点"
        f"校准及冻结模型未来前向评估链均已落地。{_credibility_target_pct()}"
        "可信度仍明确未达，"
        "4H逐币99%和当日三次自然调度的+50%吞吐仍需由后续真实调度"
        "证据验收。只有其余硬门槛分别被独立证据满足后，才允许提交真实"
        "交易扩容评审。"
    )
    _sync_title_block(manifest)
    return artifact


def refresh_source_health(
    artifact: dict,
    audit: dict,
    *,
    audit_relative_path: str,
) -> dict:
    """Replace the legacy observed-row fast rate with scheduled-slot evidence."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if audit.get("artifact_type") != "scheduled_source_health_audit":
        raise ValueError("unexpected source-health artifact type")
    rolling = audit["rolling"]
    forward = audit["forward_after_remediation"]
    expected = int(rolling["expected_slots"])
    observed = int(rolling["observed_rows"])
    missing = int(rolling["missing_slots"])
    complete = int(rolling["complete_slots"])
    available = int(rolling["available_slots"])
    rolling_rate = float(rolling["complete_rate"])
    rolling_available_rate = float(rolling["available_rate"])
    forward_complete = int(forward["complete_slots"])
    forward_available = int(forward["available_slots"])
    forward_rate = float(forward["complete_rate"])
    forward_available_rate = float(forward["available_rate"])
    target = float(audit["target_rate"])
    target_pct = f"{target:.0%}"
    if (
        expected <= 0
        or not 0 <= observed <= expected
        or missing != expected - observed
        or not 0 <= complete <= available <= expected
        or not 0 <= forward_complete <= forward_available
        <= int(forward["expected_slots"])
        or not 0 <= rolling_rate <= 1
        or not 0 <= rolling_available_rate <= 1
        or not 0 <= forward_rate <= 1
        or not 0 <= forward_available_rate <= 1
        or abs(rolling_rate - complete / expected) > 1e-6
        or abs(rolling_available_rate - available / expected) > 1e-6
        or (
            int(forward["expected_slots"]) > 0
            and abs(
                forward_rate
                - forward_complete / int(forward["expected_slots"])
            ) > 1e-6
        )
        or (
            int(forward["expected_slots"]) > 0
            and abs(
                forward_available_rate
                - forward_available / int(forward["expected_slots"])
            ) > 1e-6
        )
    ):
        raise ValueError("invalid source-health audit counts")

    generated_at = _source_audit_utc(audit)
    as_of_cst = _parse_source_cst(audit["as_of_cst"])
    manifest = artifact["manifest"]
    _advance_report_generated_at(artifact, generated_at)
    sources = manifest["sources"]
    matches = [item for item in sources if item.get("id") == "source_health"]
    if len(matches) > 1:
        raise ValueError("duplicate source_health sources")
    if matches:
        source = matches[0]
    else:
        source = {"id": "source_health"}
        sources.append(source)
    source.update({
        "label": "fast计划槽14日与修复后前向健康审计",
        "path": audit_relative_path,
        "query": {
            "engine": "SQLite + deterministic schedule reconstruction",
            "language": "python/sql",
            "sql": (
                "SELECT cycle_id,status,ts,rows,latency_ms,err FROM "
                "collection_runs WHERE source='fast' AND cycle_id>=? "
                "AND cycle_id<? ORDER BY cycle_id"
            ),
            "description": (
                "按北京时间每15分钟重建所有应运行槽；仅ok计严格完整，"
                "degraded与缺行均进入严格分母；ok+degraded可用率只作诊断。"
            ),
            "executed_at": generated_at,
            "tables_used": ["ledger.db.collection_runs", audit_relative_path],
            "filters": [
                f"rolling [{rolling['start_cst']},{rolling['end_exclusive_cst']})",
                f"forward [{forward['start_cst']},{forward['end_exclusive_cst']})",
                f"前向至少{forward['minimum_slots']}个自然槽",
            ],
            "metric_definitions": [
                "严格完整率=ok计划槽/全部应运行计划槽",
                "运行可用率=(ok+degraded计划槽)/全部应运行计划槽，仅作诊断",
                "degraded与缺失账本行均不计入严格完整率",
            ],
        },
    })

    snapshot = artifact["snapshot"]
    datasets = snapshot["datasets"]
    headline = datasets["headline"][0]
    per_symbol_min = min(
        float(row["coverage_rate"]) for row in datasets["coverage"])
    headline.update({
        "per_symbol_minimum_coverage_rate": per_symbol_min,
        "fast_rolling_complete_rate": rolling_rate,
        "fast_rolling_available_rate": rolling_available_rate,
        "fast_forward_complete_rate": forward_rate,
        "fast_forward_available_rate": forward_available_rate,
        # Compatibility aliases now deliberately carry the strict rate so
        # older report blocks cannot accidentally promote degraded to complete.
        "fast_rolling_usable_rate": rolling_rate,
        "fast_forward_usable_rate": forward_rate,
        "fast_forward_expected_slots": int(forward["expected_slots"]),
        "fast_forward_minimum_slots": int(forward["minimum_slots"]),
        "minimum_coverage_rate": min(per_symbol_min, rolling_rate),
    })

    status_counts = rolling.get("raw_status_counts") or {}
    legacy_fast = [
        row for row in datasets.get("source_health", [])
        if row.get("source") == "fast"
    ]
    strict_fast = [
        row for row in datasets.get("fast_source_health", [])
        if row.get("source") == "fast"
    ]
    fast_candidates = legacy_fast + strict_fast
    if len(fast_candidates) != 1:
        raise ValueError(
            "expected exactly one source='fast' across source-health datasets, "
            f"got {len(fast_candidates)}"
        )
    fast_row = fast_candidates[0]
    fast_row.update({
        "complete_rate": rolling_rate,
        "available_rate": rolling_available_rate,
        "usable_rate": rolling_rate,
        "ok_rate": round(int(status_counts.get("ok", 0)) / expected, 6),
        "degraded_rate": round(
            int(status_counts.get("degraded", 0)) / expected, 6),
        "incomplete_rate": round((expected - complete) / expected, 6),
        "unavailable_rate": round((expected - available) / expected, 6),
        "runs": expected,
        "observed_rows": observed,
        "missing_slots": missing,
        "forward_complete_rate": forward_rate,
        "forward_available_rate": forward_available_rate,
        "forward_usable_rate": forward_rate,
        "forward_expected_slots": int(forward["expected_slots"]),
        "forward_minimum_slots": int(forward["minimum_slots"]),
        "forward_status": str(forward["status"]),
        "target_rate": target,
        "status": "达标" if audit["overall_status"] == "PASSED" else "未达标",
    })
    datasets["source_health"] = [
        row for row in datasets["source_health"] if row.get("source") != "fast"
    ]
    datasets["fast_source_health"] = [fast_row]

    gate = _one(datasets["gates"], "goal", "关键数据完善率")
    gate.update({
        "current": (
            f"fast计划槽14日严格完整 {rolling_rate:.3%}（{complete}/{expected}，"
            f"运行可用{rolling_available_rate:.3%}，"
            f"缺行{missing}）；4H逐币 {per_symbol_min:.3%}（缺6币）；"
            f"修复后前向 {forward_rate:.3%}（{forward['expected_slots']}/"
            f"{forward['minimum_slots']}槽）"
        ),
        "status": "达标" if audit["overall_status"] == "PASSED" else "未达标",
        "next_gate": (
            "08:00自然快照验证BRKB/SHOP闭合4H后逐币覆盖；fast修复后前向"
            f"至少{forward['minimum_slots']}槽且≥{target_pct}，"
            f"14日滚动窗也须≥{target_pct}"
        ),
    })

    coverage_card = _one(manifest["cards"], "id", "coverage_card")
    coverage_card["description"] = (
        "逐币关键市场数据族与fast严格完整计划槽分别验收，取二者最低值，"
        "不以已有账本行或平均数掩盖缺口。"
    )
    source_table = _one(manifest["tables"], "id", "source_health_table")
    source_table.update({
        "title": "其它近14日采集来源可用率",
        "subtitle": "各来源保留已审阅的独立调度口径；fast见上方严格计划槽表。",
        "sourceId": "baseline",
    })
    strict_table = {
        "id": "fast_source_health_table",
        "title": "fast计划槽14日与修复后前向严格完整率",
        "subtitle": (
            "仅ok计完整；degraded与缺失行进入分母；不足96个前向自然槽不得判定。"
        ),
        "dataset": "fast_source_health",
        "sourceId": "source_health",
        "density": "dense",
        "columns": [
            {"field": "source", "label": "来源", "type": "text"},
            {"field": "complete_rate", "label": "14日严格完整率", "format": "percent"},
            {"field": "available_rate", "label": "14日运行可用率", "format": "percent"},
            {"field": "runs", "label": "应运行槽", "format": "number"},
            {"field": "observed_rows", "label": "账本行", "format": "number"},
            {"field": "missing_slots", "label": "缺失槽", "format": "number"},
            {"field": "forward_complete_rate", "label": "前向严格完整率", "format": "percent"},
            {"field": "forward_available_rate", "label": "前向运行可用率", "format": "percent"},
            {"field": "forward_expected_slots", "label": "前向槽数", "format": "number"},
            {"field": "forward_status", "label": "前向状态", "type": "text"},
            {"field": "status", "label": f"整体{target_pct}", "type": "text"},
        ],
        "defaultSort": {"field": "complete_rate", "direction": "asc"},
        "layout": "full",
    }
    existing_strict = [
        table for table in manifest["tables"]
        if table.get("id") == "fast_source_health_table"
    ]
    if existing_strict:
        existing_strict[0].clear()
        existing_strict[0].update(strict_table)
    else:
        manifest["tables"].append(strict_table)

    blocks = manifest["blocks"]
    strict_block = {
        "id": "fast_source_health_block",
        "type": "table",
        "tableId": "fast_source_health_table",
        "layout": "full",
    }
    existing_strict_blocks = [
        block for block in blocks if block.get("id") == "fast_source_health_block"
    ]
    if existing_strict_blocks:
        existing_strict_blocks[0].clear()
        existing_strict_blocks[0].update(strict_block)
    else:
        insert_at = next(
            (index + 1 for index, block in enumerate(blocks)
             if block.get("id") == "source_health_block"),
            len(blocks),
        )
        blocks.insert(insert_at, strict_block)
    executive = _one(blocks, "id", "executive_summary")
    executive["body"] = re.sub(
        r"最低关键数据覆盖仍为[0-9.]+%",
        (
            f"最低严格完整率仍为{rolling_rate:.3%}（fast计划槽；逐币4H"
            f"为{per_symbol_min:.3%}）"
        ),
        executive["body"],
    )
    data_block = _one(blocks, "id", "data_section")
    data_block["body"] = (
        f"## 官方行情主域已更新，逐币4H仍待99%、"
        f"历史计划槽仍待{target_pct}\n\n"
        "Ticker、合约元数据、15m及资金费/OI逐币覆盖均为100%；1H为"
        f"99.063%，4H为{per_symbol_min:.3%}。BRKB与SHOP当前各有33根4H，"
        "MACD需要34根且只允许已收盘K线，最早由08:00自然快照验收；其余"
        "4个新合约保持wait_data，不虚构历史。公共REST已按OKX官方更新为"
        "openapi.okx.com主域，并在原3次总尝试内有界回退www.okx.com。"
        f"严格计划槽审计重建14日{expected}槽，其中完整{complete}、运行可用"
        f"{available}、缺账本行{missing}，严格完整率{rolling_rate:.3%}；修复后前向"
        f"{forward['expected_slots']}/{forward['minimum_slots']}槽、"
        f"当前{forward_rate:.3%}，证据尚不足。"
    )
    gates_block = _one(blocks, "id", "gates_section")
    report_gate_passed = bool(
        artifact["snapshot"]["datasets"]["headline"][0].get(
            "report_and_push_gate_passed", False
        )
    )
    gates_block["body"] = (
        "## 当前结论与下一验收闸\n\n"
        + (
            "报告与推送完整度三个独立门均已达到硬门槛；"
            if report_gate_passed else
            "报告与推送完整度总体门仍未通过；"
        )
        + "全市场影子判断、冻结模型未来"
        f"前向评估和fast计划槽双窗审计均已落地。{_credibility_target_pct()}"
        "可信度仍明确未达，"
        "fast 14日可用率、4H逐币99%和当日三次自然调度的+50%吞吐仍需"
        "分别由后续真实调度证据验收。只有其余硬门槛独立满足后，才允许"
        "提交真实交易扩容评审。"
    )
    methods = _one(blocks, "id", "methods")
    if "缺失计划槽" not in methods["body"]:
        methods["body"] += (
            "\n\n来源健康率按应运行计划槽重建，缺失账本行计不可用；修复后"
            "前向窗与14日滚动窗并列展示，不能用短窗替代长期门槛。"
        )
    _sync_title_block(manifest)
    return artifact


def refresh_credibility_evidence(
    artifact: dict,
    calibration: dict,
    policy: dict,
    signal_audit: dict,
    *,
    calibration_relative_path: str,
    policy_relative_path: str,
    signal_relative_path: str,
) -> dict:
    """Refresh corrected time-split research and actual production-signal evidence."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if calibration.get("schema_version") != 2:
        raise ValueError("unexpected calibration schema")
    if policy.get("artifact_type") != "multitimeframe_policy_diagnostic":
        raise ValueError("unexpected policy diagnostic")
    if signal_audit.get("artifact_type") != (
        "analysis_signal_forward_quality_audit"
    ):
        raise ValueError("unexpected analysis-signal audit")

    holdout = calibration["holdout"]
    rules = calibration["current_alignment_rule_test_baseline"]
    selected = policy["selected_policy"]
    selected_holdout = selected["historical_holdout"]
    oracle = policy["oracle_ranking_diagnostic"]["historical_holdout"]
    production = signal_audit["retrospective_evaluation"]
    target = float(policy["acceptance"]["target_precision"])

    manifest = artifact["manifest"]
    sources = manifest["sources"]
    calibration_source = _upsert_id(sources, "calibration")
    calibration_source.update({
        "label": "右截尾修正后的15m/1H/4H时间切分校准",
        "path": calibration_relative_path,
        "query": {
            "engine": "SQLite + NumPy/Pandas",
            "language": "python/sql",
            "description": (
                "仅保留每个观察点六个候选标签全部成熟的行；训练、校准、"
                "历史留出严格按时间切分并隔离4小时。"
            ),
            "executed_at": calibration["generated_at_utc"],
            "tables_used": [
                "market.db.tick_snapshots",
                "market.db.kline_cache",
                "market.db.market_microstructure",
                "market.db.market_trade_flow",
                "market.db.market_positioning",
                calibration_relative_path,
            ],
            "filters": [
                "3个周期 x 2个方向共6个候选全部成熟",
                "扣除20bp成本缓冲",
                "历史留出只作诊断，不作为未来独立证明",
            ],
            "metric_definitions": [
                "精确率=选中方向扣20bp后收益为正的样本/全部选中样本",
                "ECE=概率箱加权绝对校准误差",
            ],
        },
    })
    policy_source = _upsert_id(sources, "policy_diagnostic")
    policy_source.update({
        "label": "多周期候选排序与策略选择诊断",
        "path": policy_relative_path,
        "query": {
            "engine": "Deterministic policy diagnostics",
            "language": "python",
            "description": (
                "策略只在校准窗选择，随后一次性报告历史留出；神谕行使用"
                "未来标签，只是固定候选族不可交易的上限。"
            ),
            "executed_at": policy["generated_at_utc"],
            "tables_used": [
                calibration_relative_path,
                policy_relative_path,
            ],
            "filters": [
                f"校准选择策略={selected['policy']}",
                "固定13个探索策略",
                "不允许修改生产阈值或下单",
            ],
            "metric_definitions": [
                "神谕成功率=每个完整观察点六候选中至少一个成功的比例",
                "捕获率=最高概率候选命中/存在成功候选的观察点",
            ],
        },
    })
    signal_source = _upsert_id(sources, "analysis_signal_forward")
    signal_source.update({
        "label": "实际生产LLM开多/开空信号结果审计",
        "path": signal_relative_path,
        "query": {
            "engine": "SQLite deterministic outcome audit",
            "language": "python/sql",
            "description": (
                "只评估terminal status=ok的生产open_long/open_short；按分析完成"
                "后的首个可执行买卖价入场并扣20bp。"
            ),
            "executed_at": signal_audit["generated_at_cst"],
            "tables_used": [
                "analysis.db.analysis_runs",
                "analysis.db.analysis_signals",
                "market.db.tick_snapshots",
                signal_relative_path,
            ],
            "filters": [
                "发现窗只选择一个固定周期",
                "评估窗为回顾性且非独立未来窗",
                "N、日期数和周期数分别外显",
            ],
            "metric_definitions": [
                "long按ask入/bid出，short按bid入/ask出",
                "可信度90%必须有独立未来窗、N>=100和>=100周期",
            ],
        },
    })
    combined = _upsert_id(sources, "credibility_evidence")
    combined.update({
        "label": "可信度组合证据快照",
        "path": policy_relative_path,
        "query": {
            "engine": "Referenced deterministic evidence",
            "language": "python/json",
            "sql": (
                "WITH evidence(method,precision_after_cost,n,ece,"
                "evidence_class) AS (\n  VALUES\n"
                f"    ('enhanced_holdout',{float(holdout['precision']):.12f},"
                f"{int(holdout['n'])},{float(holdout['ece']):.12f},"
                "'historical_diagnostic'),\n"
                f"    ('selected_policy',{float(selected_holdout['precision']):.12f},"
                f"{int(selected_holdout['n'])},{float(selected_holdout['ece']):.12f},"
                "'historical_diagnostic'),\n"
                f"    ('production_signal_4h',"
                f"{float(production['precision_after_cost']):.12f},"
                f"{int(production['n'])},NULL,"
                "'retrospective_not_independent_forward'),\n"
                f"    ('hindsight_oracle',"
                f"{float(oracle['oracle_any_candidate_success_rate']):.12f},"
                f"{int(oracle['complete_observations'])},NULL,"
                "'diagnostic_upper_bound')\n)\nSELECT * FROM evidence"
            ),
            "description": (
                "图表合并右截尾修正校准、校准窗选定策略的历史留出、"
                "实际生产信号审计和不可交易神谕上限。"
            ),
            "executed_at": policy["generated_at_utc"],
            "tables_used": [
                calibration_relative_path,
                policy_relative_path,
                signal_relative_path,
            ],
        },
    })

    credibility_rows: list[dict] = []
    for horizon in ("15m", "1H", "4H"):
        row = rules[horizon]
        credibility_rows.append({
            "method": f"一致度规则 {horizon}",
            "precision_after_cost": float(row["precision_after_cost"]),
            "n": int(row["n"]),
            "wilson_low": float(row["wilson_95_low"]),
            "wilson_high": float(row["wilson_95_high"]),
            "ece": None,
            "days": int(row["distinct_days"]),
            "cycles": int(row["distinct_cycles"]),
            "mean_signed_return_after_cost": float(
                row["mean_signed_return_after_cost"]),
            "result_type": "rule_historical_holdout",
            "evidence_class": "historical_diagnostic",
        })
    credibility_rows.extend([
        {
            "method": "增强模型（右截尾修正）",
            "precision_after_cost": float(holdout["precision"]),
            "n": int(holdout["n"]),
            "wilson_low": float(holdout["wilson_95_low"]),
            "wilson_high": float(holdout["wilson_95_high"]),
            "ece": float(holdout["ece"]),
            "days": int(holdout["distinct_days"]),
            "cycles": int(holdout["distinct_cycles"]),
            "mean_signed_return_after_cost": float(
                holdout["mean_signed_return_after_cost"]),
            "result_type": "enhanced_historical_holdout",
            "evidence_class": "historical_diagnostic",
        },
        {
            "method": f"校准选策 {selected['policy']}",
            "precision_after_cost": float(selected_holdout["precision"]),
            "n": int(selected_holdout["n"]),
            "wilson_low": float(selected_holdout["wilson_95_low"]),
            "wilson_high": float(selected_holdout["wilson_95_high"]),
            "ece": float(selected_holdout["ece"]),
            "days": int(selected_holdout["distinct_days"]),
            "cycles": int(selected_holdout["distinct_cycles"]),
            "mean_signed_return_after_cost": float(
                selected_holdout["mean_signed_return_after_cost"]),
            "result_type": "selected_policy_historical_holdout",
            "evidence_class": "historical_diagnostic",
        },
        {
            "method": "实际生产LLM信号 4H",
            "precision_after_cost": float(production["precision_after_cost"]),
            "n": int(production["n"]),
            "wilson_low": float(production["wilson_95_low"]),
            "wilson_high": float(production["wilson_95_high"]),
            "ece": None,
            "days": int(production["distinct_days"]),
            "cycles": int(production["distinct_cycles"]),
            "mean_signed_return_after_cost": float(
                production["mean_signed_return_after_cost"]),
            "result_type": "production_signal_retrospective",
            "evidence_class": "retrospective_not_independent_forward",
        },
        {
            "method": "固定候选族事后神谕（不可交易）",
            "precision_after_cost": float(
                oracle["oracle_any_candidate_success_rate"]),
            "n": int(oracle["complete_observations"]),
            "wilson_low": None,
            "wilson_high": None,
            "ece": None,
            "days": int(selected_holdout["distinct_days"]),
            "cycles": int(selected_holdout["distinct_cycles"]),
            "mean_signed_return_after_cost": None,
            "result_type": "non_tradable_hindsight_oracle",
            "evidence_class": "diagnostic_upper_bound",
        },
        {
            "method": "生产可信度目标",
            "precision_after_cost": target,
            "n": 100,
            "wilson_low": None,
            "wilson_high": None,
            "ece": 0.05,
            "days": 5,
            "cycles": 100,
            "mean_signed_return_after_cost": None,
            "result_type": "acceptance_target",
            "evidence_class": "target",
        },
    ])

    datasets = artifact["snapshot"]["datasets"]
    datasets["credibility"] = credibility_rows
    headline = datasets["headline"][0]
    headline.update({
        "holdout_precision": float(holdout["precision"]),
        "holdout_target": target,
        "holdout_n": int(holdout["n"]),
        "holdout_ece": float(holdout["ece"]),
        "policy_holdout_precision": float(selected_holdout["precision"]),
        "policy_holdout_n": int(selected_holdout["n"]),
        "policy_holdout_ece": float(selected_holdout["ece"]),
        "production_signal_precision": float(
            production["precision_after_cost"]),
        "production_signal_n": int(production["n"]),
        "production_signal_wilson_low": float(production["wilson_95_low"]),
        "production_signal_wilson_high": float(production["wilson_95_high"]),
        "oracle_candidate_ceiling": float(
            oracle["oracle_any_candidate_success_rate"]),
        "top_probability_capture_rate": float(
            oracle["capture_rate_when_any_candidate_succeeds"]),
    })
    gate = _one(datasets["gates"], "goal", "分析可信度")
    gate.update({
        "current": (
            f"实际生产LLM信号4H {production['precision_after_cost']:.3%}"
            f"（N={production['n']}，非独立回顾窗）；校准选策历史留出 "
            f"{selected_holdout['precision']:.3%}（N={selected_holdout['n']}，"
            f"ECE={selected_holdout['ece']:.3%}）"
        ),
        "status": "未达标",
        "next_gate": (
            "保持现有生产阈值与下单规模；修复候选排序后预注册新策略，"
            "只用未来新增影子窗验收N>=100、>=5天、>=100周期和ECE<=5pp"
        ),
    })

    card = _one(manifest["cards"], "id", "credibility_card")
    card.update({
        "description": (
            "实际生产LLM信号与校准窗选定策略分别展示；前者样本不足且"
            "不是独立未来窗，后者只是历史诊断。"
        ),
        "sourceId": "credibility_evidence",
        "metrics": [
            {"label": "生产信号4H精度", "field": "production_signal_precision",
             "format": "percent"},
            {"label": "生产信号N", "field": "production_signal_n",
             "format": "number"},
            {"label": "生产信号Wilson下界",
             "field": "production_signal_wilson_low", "format": "percent"},
            {"label": "校准选策留出精度", "field": "policy_holdout_precision",
             "format": "percent"},
            {"label": "校准选策N", "field": "policy_holdout_n",
             "format": "number"},
            {"label": "校准选策ECE", "field": "policy_holdout_ece",
             "format": "percent"},
        ],
    })
    chart = _one(manifest["charts"], "id", "credibility_chart")
    chart.update({
        "title": "扣20bp方向精确率：历史诊断、生产信号与上限",
        "subtitle": (
            "实际生产信号仅N=11；固定候选族事后神谕也低于90%，"
            "生产阈值保持不变。"
        ),
        "question": "当前可执行选择是否有证据达到90%？",
        "rationale": (
            "同一零基线尺度区分真实可执行结果、历史诊断、不可交易上限和目标。"
        ),
        "sourceId": "credibility_evidence",
    })
    credibility_block = _one(
        manifest["blocks"], "id", "credibility_section")
    credibility_block["body"] = (
        "## 排序能力是主要短板，90%仍没有证据支持\n\n"
        f"修正右截尾后，增强模型历史留出精度为{holdout['precision']:.3%}"
        f"（N={holdout['n']}，ECE={holdout['ece']:.3%}）；只在校准窗选择的"
        f"{selected['policy']}策略，在历史留出为"
        f"{selected_holdout['precision']:.3%}（N={selected_holdout['n']}，"
        f"Wilson {selected_holdout['wilson_95_low']:.3%}–"
        f"{selected_holdout['wilson_95_high']:.3%}，ECE="
        f"{selected_holdout['ece']:.3%}）。两者都远低于90%。\n\n"
        f"实际生产LLM开多/开空信号按发现窗预选4H后，回顾评估为"
        f"{production['precision_after_cost']:.3%}（N={production['n']}，"
        f"Wilson {production['wilson_95_low']:.3%}–"
        f"{production['wilson_95_high']:.3%}，{production['distinct_days']}天/"
        f"{production['distinct_cycles']}周期）；它既不足100样本，也不是"
        "独立未来窗。更关键的是，固定六候选族使用未来标签的不可交易神谕"
        f"上限也只有{oracle['oracle_any_candidate_success_rate']:.3%}，最高"
        f"概率候选只捕获{oracle['capture_rate_when_any_candidate_succeeds']:.3%}"
        "的可成功机会。当前问题不是把阈值调高即可解决，而是候选排序与信号"
        "信息量不足；因此没有修改生产阈值、风控或订单规模。"
    )
    return artifact


def refresh_selective_credibility(
    artifact: dict,
    shared: dict,
    independent: dict,
    *,
    shared_relative_path: str,
    independent_relative_path: str,
) -> dict:
    """Add nested nonlinear selective-model evidence without production changes."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    for diagnostic in (shared, independent):
        if diagnostic.get("artifact_type") != (
            "selective_multitimeframe_diagnostic"
        ):
            raise ValueError("unexpected selective diagnostic")
        if diagnostic.get("acceptance", {}).get(
            "production_status"
        ) != "NO_CHANGE_ALLOWED":
            raise ValueError("selective diagnostic must forbid production change")

    shared_confirmation = shared["evaluation"]["internal_confirmation"]
    shared_holdout = shared["evaluation"]["historical_holdout"]
    independent_confirmation = independent["evaluation"][
        "internal_confirmation"
    ]
    independent_holdout = independent["evaluation"]["historical_holdout"]
    target = float(shared["acceptance"]["target_precision"])
    if target != float(independent["acceptance"]["target_precision"]):
        raise ValueError("selective target mismatch")

    manifest = artifact["manifest"]
    sources = manifest["sources"]
    shared_source = _upsert_id(sources, "selective_shared")
    shared_source.update({
        "label": "共享非线性选择模型嵌套时间验证",
        "path": shared_relative_path,
        "query": {
            "engine": "Deterministic NumPy/Pandas histogram GBDT",
            "language": "python",
            "description": (
                "共享模型在训练后按时间拆分模型选择、阈值选择和内部确认，"
                "各窗之间隔离4小时；历史留出已经查看，只作回顾诊断。"
            ),
            "executed_at": shared["generated_at_utc"],
            "tables_used": [
                str(shared["input_panel"]),
                shared_relative_path,
            ],
            "filters": [
                "每个观察点固定15m/1H/4H x long/short六候选",
                "扣20bp成本缓冲",
                "阈值只能在阈值选择窗确定",
                "生产阈值与订单规模不可修改",
            ],
            "metric_definitions": [
                "内部确认精确率=冻结模型和阈值在后续确认窗的扣成本成功率",
                "历史留出是已查看的回顾诊断，不是独立未来证明",
            ],
        },
    })
    independent_source = _upsert_id(sources, "selective_independent")
    independent_source.update({
        "label": "分候选非线性模型复杂度对照",
        "path": independent_relative_path,
        "query": {
            "engine": "Deterministic per-candidate histogram GBDT",
            "language": "python",
            "description": (
                "为六个周期方向候选分别拟合非线性模型，使用与共享模型"
                "完全相同的时间切分、成本口径和验收门槛。"
            ),
            "executed_at": independent["generated_at_utc"],
            "tables_used": [
                str(independent["input_panel"]),
                independent_relative_path,
            ],
            "filters": [
                "共享模型的等口径复杂度对照",
                "扣20bp成本缓冲",
                "生产阈值与订单规模不可修改",
            ],
        },
    })

    def selective_row(
        method: str, result: dict, result_type: str, evidence_class: str,
    ) -> dict:
        return {
            "method": method,
            "precision_after_cost": float(result["precision"]),
            "n": int(result["n"]),
            "wilson_low": float(result["wilson_95_low"]),
            "wilson_high": float(result["wilson_95_high"]),
            "ece": float(result["ece"]),
            "days": int(result["distinct_days"]),
            "cycles": int(result["distinct_cycles"]),
            "mean_signed_return_after_cost": float(
                result["mean_signed_return_after_cost"]),
            "result_type": result_type,
            "evidence_class": evidence_class,
        }

    nonlinear_rows = [
        selective_row(
            "共享GBDT 内部确认", shared_confirmation,
            "shared_gbdt_internal_confirmation", "nested_internal_confirmation",
        ),
        selective_row(
            "共享GBDT 历史留出", shared_holdout,
            "shared_gbdt_historical_holdout", "historical_diagnostic",
        ),
        selective_row(
            "分候选GBDT 内部确认", independent_confirmation,
            "independent_gbdt_internal_confirmation",
            "nested_internal_confirmation",
        ),
        selective_row(
            "分候选GBDT 历史留出", independent_holdout,
            "independent_gbdt_historical_holdout", "historical_diagnostic",
        ),
    ]
    datasets = artifact["snapshot"]["datasets"]
    result_types = {row["result_type"] for row in nonlinear_rows}
    datasets["credibility"] = [
        row for row in datasets["credibility"]
        if row.get("result_type") not in result_types
    ] + nonlinear_rows
    primary_types = {
        "enhanced_historical_holdout",
        "selected_policy_historical_holdout",
        "production_signal_retrospective",
        "shared_gbdt_internal_confirmation",
        "shared_gbdt_historical_holdout",
        "independent_gbdt_internal_confirmation",
        "independent_gbdt_historical_holdout",
        "acceptance_target",
    }
    datasets["credibility_primary"] = [
        row for row in datasets["credibility"]
        if row.get("result_type") in primary_types
    ]

    sql_rows = []
    for row in datasets["credibility_primary"]:
        method = str(row["method"]).replace("'", "''")
        ece = "NULL" if row["ece"] is None else f"{float(row['ece']):.12f}"
        evidence_class = str(row["evidence_class"]).replace("'", "''")
        sql_rows.append(
            f"    ('{method}',{float(row['precision_after_cost']):.12f},"
            f"{int(row['n'])},{ece},'{evidence_class}')"
        )
    combined = _upsert_id(sources, "credibility_evidence")
    combined_query = combined.setdefault("query", {})
    combined_query.update({
        "engine": "Referenced deterministic evidence",
        "language": "python/json/sql",
        "sql": (
            "WITH evidence(method,precision_after_cost,n,ece,evidence_class) "
            "AS (\n  VALUES\n" + ",\n".join(sql_rows) +
            "\n)\nSELECT * FROM evidence"
        ),
        "description": (
            "同图比较线性历史诊断、实际生产信号、共享与分候选GBDT的"
            "内部确认/历史留出，以及90%目标。"
        ),
        "executed_at": independent["generated_at_utc"],
        "tables_used": list(dict.fromkeys(
            list(combined_query.get("tables_used", [])) + [
                shared_relative_path, independent_relative_path,
            ]
        )),
        "filters": [
            "所有模型统一扣20bp",
            "内部确认与历史留出明确分层",
            "历史结果不授权生产变更",
        ],
        "metric_definitions": [
            "精确率=冻结选择在相应时间窗中扣20bp后方向收益为正的比例",
            "90%可信度还要求独立未来窗N>=100、>=5天、>=100周期且ECE<=5pp",
        ],
    })

    headline = datasets["headline"][0]
    headline.update({
        "credibility_target_rate": target,
        "shared_confirmation_precision": float(shared_confirmation["precision"]),
        "shared_confirmation_n": int(shared_confirmation["n"]),
        "shared_confirmation_ece": float(shared_confirmation["ece"]),
        "shared_holdout_precision": float(shared_holdout["precision"]),
        "shared_holdout_n": int(shared_holdout["n"]),
        "independent_confirmation_precision": float(
            independent_confirmation["precision"]),
        "independent_confirmation_n": int(independent_confirmation["n"]),
        "independent_confirmation_ece": float(
            independent_confirmation["ece"]),
        "independent_holdout_precision": float(independent_holdout["precision"]),
        "independent_holdout_n": int(independent_holdout["n"]),
    })
    gate = _one(datasets["gates"], "goal", "分析可信度")
    gate.update({
        "current": (
            f"生产LLM 4H回顾 {headline['production_signal_precision']:.3%}"
            f"（N={headline['production_signal_n']}）；共享GBDT内部确认 "
            f"{shared_confirmation['precision']:.3%}（N={shared_confirmation['n']}，"
            f"ECE={shared_confirmation['ece']:.3%}）；分候选GBDT内部确认 "
            f"{independent_confirmation['precision']:.3%}"
            f"（N={independent_confirmation['n']}，"
            f"ECE={independent_confirmation['ece']:.3%}）"
        ),
        "status": "未达标",
        "next_gate": (
            "保留生产阈值、风控和订单规模；积累新OI/主动买卖量的前向历史，"
            "预注册后仅用未来影子窗验收N>=100、>=5天、>=100周期、"
            "精确率>=90%且ECE<=5pp"
        ),
    })
    card = _one(manifest["cards"], "id", "credibility_card")
    card.update({
        "description": (
            "生产信号样本很小；非线性模型展示未用于选模的内部确认，"
            "历史留出只作为稳定性对照。"
        ),
        "sourceId": "credibility_evidence",
        "metrics": [
            {"label": "生产信号4H精度", "field": "production_signal_precision",
             "format": "percent"},
            {"label": "90%目标", "field": "credibility_target_rate",
             "format": "percent"},
            {"label": "生产信号N", "field": "production_signal_n",
             "format": "number"},
            {"label": "共享GBDT确认", "field": "shared_confirmation_precision",
             "format": "percent"},
            {"label": "分候选GBDT确认",
             "field": "independent_confirmation_precision",
             "format": "percent"},
        ],
    })
    chart = _one(manifest["charts"], "id", "credibility_chart")
    chart.update({
        "title": "扣20bp方向精确率：生产、确认窗、历史留出与目标",
        "subtitle": (
            f"共享/分候选GBDT内部确认分别为"
            f"{shared_confirmation['precision']:.1%}/"
            f"{independent_confirmation['precision']:.1%}；两者均未达90%。"
        ),
        "dataset": "credibility_primary",
        "sourceId": "credibility_evidence",
    })
    section = _one(manifest["blocks"], "id", "credibility_section")
    section["body"] = (
        "## 增加非线性复杂度没有修复候选排序，90%仍未获证明\n\n"
        f"共享GBDT只在阈值选择窗达到{shared['threshold_selection']['precision']:.3%}"
        f"（N={shared['threshold_selection']['n']}），随后未参与阈值选择的内部"
        f"确认窗降至{shared_confirmation['precision']:.3%}"
        f"（N={shared_confirmation['n']}，Wilson "
        f"{shared_confirmation['wilson_95_low']:.3%}–"
        f"{shared_confirmation['wilson_95_high']:.3%}，ECE="
        f"{shared_confirmation['ece']:.3%}）；已查看的历史留出为"
        f"{shared_holdout['precision']:.3%}（N={shared_holdout['n']}），"
        "所有被选样本仍集中在4H。\n\n"
        f"把六个候选拆成独立模型也没有改善：阈值选择窗"
        f"{independent['threshold_selection']['precision']:.3%}"
        f"（N={independent['threshold_selection']['n']}），内部确认"
        f"{independent_confirmation['precision']:.3%}"
        f"（N={independent_confirmation['n']}），历史留出"
        f"{independent_holdout['precision']:.3%}"
        f"（N={independent_holdout['n']}），确认窗和留出平均扣成本收益均为负。"
        f"实际生产LLM信号回顾仍为{headline['production_signal_precision']:.3%}"
        f"（N={headline['production_signal_n']}）。这些负结果说明模型复杂度"
        "不是根治方案，当前信息量和时变稳定性不足；因此没有修改生产阈值、"
        "风控、订单规模或下单链路。"
    )
    return artifact


def refresh_ranking_credibility(
    artifact: dict,
    ranking: dict,
    *,
    ranking_relative_path: str,
) -> dict:
    """Add groupwise candidate-ranking evidence without changing production."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if ranking.get("artifact_type") != (
        "groupwise_multitimeframe_ranking_diagnostic"
    ):
        raise ValueError("unexpected ranking diagnostic")
    acceptance = ranking.get("acceptance", {})
    if acceptance.get("production_status") != "NO_CHANGE_ALLOWED":
        raise ValueError("ranking diagnostic must forbid production change")
    if ranking.get("production_threshold_change_allowed") is not False:
        raise ValueError("ranking diagnostic threshold gate must fail closed")

    confirmation = ranking["evaluation"]["internal_confirmation"]
    holdout = ranking["evaluation"]["historical_holdout"]
    target = float(acceptance["target_precision"])
    manifest = artifact["manifest"]
    source = _upsert_id(manifest["sources"], "ranking_diagnostic")
    source.update({
        "label": "六候选组内排序嵌套时间诊断",
        "path": ranking_relative_path,
        "query": {
            "engine": "Deterministic NumPy/Pandas listwise and ridge models",
            "language": "python",
            "description": (
                "同一symbol-cycle的15m/1H/4H x long/short六候选共同排序；"
                "训练、选模、阈值和内部确认按时间隔离，历史留出只作回顾。"
            ),
            "executed_at": ranking["generated_at_utc"],
            "tables_used": [str(ranking["input_panel"]), ranking_relative_path],
            "filters": [
                "完整六候选观察",
                "统一扣20bp成本缓冲",
                "train-only缺失填补、缩尾和标准化",
                "内部确认不参与选模或阈值选择",
                "生产阈值与订单规模不可修改",
            ],
            "metric_definitions": [
                "组内排序精确率=每个观察点最高分候选扣20bp后成功的比例",
                "任一候选成功率只是六选一事后上限，不代表方向或期限可预测",
            ],
        },
    })

    def row(method: str, result: dict, result_type: str,
            evidence_class: str) -> dict:
        return {
            "method": method,
            "precision_after_cost": float(result["precision"]),
            "n": int(result["n"]),
            "wilson_low": float(result["wilson_95_low"]),
            "wilson_high": float(result["wilson_95_high"]),
            "ece": float(result["ece"]),
            "days": int(result["distinct_days"]),
            "cycles": int(result["distinct_cycles"]),
            "mean_signed_return_after_cost": float(
                result["mean_signed_return_after_cost"]),
            "result_type": result_type,
            "evidence_class": evidence_class,
        }

    ranking_rows = [
        row(
            "组内排序 内部确认", confirmation,
            "groupwise_ranking_internal_confirmation",
            "nested_internal_confirmation",
        ),
        row(
            "组内排序 历史留出", holdout,
            "groupwise_ranking_historical_holdout",
            "historical_diagnostic",
        ),
    ]
    datasets = artifact["snapshot"]["datasets"]
    result_types = {item["result_type"] for item in ranking_rows}
    datasets["credibility"] = [
        item for item in datasets["credibility"]
        if item.get("result_type") not in result_types
    ] + ranking_rows
    existing_primary = datasets.get("credibility_primary") or []
    datasets["credibility_primary"] = [
        item for item in existing_primary
        if item.get("result_type") not in result_types
    ] + ranking_rows

    combined = _upsert_id(manifest["sources"], "credibility_evidence")
    query = combined.setdefault("query", {})
    sql_rows = []
    for item in datasets["credibility_primary"]:
        method = str(item["method"]).replace("'", "''")
        ece = "NULL" if item.get("ece") is None else f"{float(item['ece']):.12f}"
        evidence = str(item.get("evidence_class") or "").replace("'", "''")
        sql_rows.append(
            f"    ('{method}',{float(item['precision_after_cost']):.12f},"
            f"{int(item['n'])},{ece},'{evidence}')"
        )
    query.update({
        "engine": "Referenced deterministic evidence",
        "language": "python/json/sql",
        "sql": (
            "WITH evidence(method,precision_after_cost,n,ece,evidence_class) "
            "AS (\n  VALUES\n" + ",\n".join(sql_rows)
            + "\n)\nSELECT * FROM evidence"
        ),
        "description": (
            "同图比较生产信号、线性/GBDT/组内排序的确认或历史结果与90%目标。"
        ),
        "executed_at": ranking["generated_at_utc"],
        "tables_used": list(dict.fromkeys(
            list(query.get("tables_used", [])) + [ranking_relative_path]
        )),
        "filters": [
            "统一扣20bp",
            "内部确认与历史留出分层",
            "历史结果不授权生产变更",
        ],
        "metric_definitions": [
            "精确率=冻结选择在相应时间窗中扣20bp后方向收益为正的比例",
            "90%还要求独立未来窗N>=100、>=5天、>=100周期且ECE<=5pp",
        ],
    })

    headline = datasets["headline"][0]
    headline.update({
        "ranking_confirmation_precision": float(confirmation["precision"]),
        "ranking_confirmation_n": int(confirmation["n"]),
        "ranking_confirmation_ece": float(confirmation["ece"]),
        "ranking_holdout_precision": float(holdout["precision"]),
        "ranking_holdout_n": int(holdout["n"]),
        "ranking_target_rate": target,
    })
    gate = _one(datasets["gates"], "goal", "分析可信度")
    gate.update({
        "current": (
            f"生产LLM 4H回顾 {headline['production_signal_precision']:.3%}"
            f"（N={headline['production_signal_n']}）；共享/分候选GBDT内部确认 "
            f"{headline.get('shared_confirmation_precision', 0):.3%}/"
            f"{headline.get('independent_confirmation_precision', 0):.3%}；"
            f"组内排序内部确认 {confirmation['precision']:.3%}"
            f"（N={confirmation['n']}，ECE={confirmation['ece']:.3%}）"
        ),
        "status": "未达标",
        "next_gate": (
            "不再凭历史复杂度试验扩大交易；待新增官方OI/主动买卖量形成"
            "前向历史后预注册模型，并仅在独立未来影子窗验收90%硬门槛"
        ),
    })
    card = _one(manifest["cards"], "id", "credibility_card")
    metrics = card.setdefault("metrics", [])
    if not any(item.get("field") == "ranking_confirmation_precision"
               for item in metrics):
        metrics.append({
            "label": "组内排序确认",
            "field": "ranking_confirmation_precision",
            "format": "percent",
        })
    chart = _one(manifest["charts"], "id", "credibility_chart")
    chart.update({
        "dataset": "credibility_primary",
        "sourceId": "credibility_evidence",
        "subtitle": (
            f"共享/分候选GBDT/组内排序内部确认为"
            f"{headline.get('shared_confirmation_precision', 0):.1%}/"
            f"{headline.get('independent_confirmation_precision', 0):.1%}/"
            f"{confirmation['precision']:.1%}，均未达90%。"
        ),
    })
    selected_oracle = ranking["selected_subset_oracle_diagnostic"][
        "internal_confirmation"]["any_candidate_success_rate"]
    all_oracle = ranking["label_profile"]["historical_holdout"][
        "any_candidate_success_rate"]
    section = _one(manifest["blocks"], "id", "credibility_section")
    section["body"] = (
        "## 六候选共同排序仍未识别出稳定方向，90%没有获证明\n\n"
        f"最佳组内排序为 `{ranking['model_family']['selected_model']}`。阈值"
        f"选择窗最高仅{ranking['threshold_selection']['precision']:.3%}"
        f"（N={ranking['threshold_selection']['n']}），随后内部确认为"
        f"{confirmation['precision']:.3%}（N={confirmation['n']}，Wilson "
        f"{confirmation['wilson_95_low']:.3%}–"
        f"{confirmation['wilson_95_high']:.3%}，ECE="
        f"{confirmation['ece']:.3%}），历史留出回顾为"
        f"{holdout['precision']:.3%}（N={holdout['n']}）。\n\n"
        f"内部确认被阈值选中的样本中，事后至少一个候选成功的比例虽为"
        f"{selected_oracle:.3%}，但这来自同时提供三期限和两个相反方向；"
        f"全部历史留出观察的该上限仅{all_oracle:.3%}。它是事后六选一上限，"
        "不是可预测性证据。列表排序与收益岭回归都无法稳定识别成功侧，"
        "因此没有修改生产阈值、风控、订单规模或下单链路。"
    )
    return artifact


def refresh_directional_separability(
    artifact: dict,
    diagnostic: dict,
    *,
    diagnostic_relative_path: str,
) -> dict:
    """Add volatility-opportunity versus direction-ranking evidence."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if diagnostic.get("artifact_type") != "directional_separability_diagnostic":
        raise ValueError("unexpected directional separability diagnostic")
    acceptance = diagnostic.get("acceptance", {})
    if acceptance.get("production_status") != "NO_CHANGE_ALLOWED":
        raise ValueError("directional diagnostic must forbid production change")
    if diagnostic.get("production_threshold_change_allowed") is not False:
        raise ValueError("directional diagnostic threshold gate must fail closed")

    confirmation = diagnostic["evaluation"]["internal_confirmation"]
    holdout = diagnostic["evaluation"]["historical_holdout"]
    root_cause = diagnostic["root_cause"]
    manifest = artifact["manifest"]
    source = _upsert_id(manifest["sources"], "directional_separability")
    source.update({
        "label": "波动机会与方向可分性诊断",
        "path": diagnostic_relative_path,
        "query": {
            "engine": "Deterministic Pandas cross-sectional policy audit",
            "language": "python",
            "description": (
                "固定概率差/一致性策略族仅在模型选择窗选型，随后原样检查"
                "阈值窗、内部确认和已查看历史留出。"
            ),
            "executed_at": diagnostic["generated_at_utc"],
            "tables_used": [diagnostic_relative_path],
            "filters": [
                "每个symbol-hour恰有15m/1H/4H x long/short六候选",
                "统一扣20bp后标签",
                "逐UTC小时横截面选择，不用未来标签调整阈值",
                "固定32策略族；各窗事后最好值只作问题范围诊断",
                "生产阈值、风控和订单规模不可修改",
            ],
            "metric_definitions": [
                "任一候选成功率=六个相反方向/期限中事后至少一个扣成本成功",
                "方向精确率=事前最高分候选扣成本后成功的比例",
                "排名缺口=任一候选成功率-事前选择精确率",
                "90%声明要求点精度和Wilson 95%下界都不低于90%",
            ],
        },
    })

    def row(method: str, result: dict, result_type: str,
            evidence_class: str) -> dict:
        return {
            "method": method,
            "precision_after_cost": float(result["precision"]),
            "n": int(result["n"]),
            "wilson_low": float(result["wilson_95_low"]),
            "wilson_high": float(result["wilson_95_high"]),
            "ece": None,
            "days": int(result["distinct_days"]),
            "cycles": int(result["distinct_cycles"]),
            "mean_signed_return_after_cost": float(
                result["mean_signed_return_after_cost"]),
            "result_type": result_type,
            "evidence_class": evidence_class,
        }

    directional_rows = [
        row(
            "方向间距 内部确认", confirmation,
            "directional_margin_internal_confirmation",
            "nested_internal_confirmation",
        ),
        row(
            "方向间距 历史留出", holdout,
            "directional_margin_historical_holdout",
            "historical_diagnostic",
        ),
    ]
    datasets = artifact["snapshot"]["datasets"]
    result_types = {item["result_type"] for item in directional_rows}
    for dataset_id in ("credibility", "credibility_primary"):
        existing = datasets.get(dataset_id) or []
        datasets[dataset_id] = [
            item for item in existing
            if item.get("result_type") not in result_types
        ] + directional_rows

    combined = _upsert_id(manifest["sources"], "credibility_evidence")
    query = combined.setdefault("query", {})
    sql_rows = []
    for item in datasets["credibility_primary"]:
        method = str(item["method"]).replace("'", "''")
        ece = "NULL" if item.get("ece") is None else f"{float(item['ece']):.12f}"
        evidence = str(item.get("evidence_class") or "").replace("'", "''")
        sql_rows.append(
            f"    ('{method}',{float(item['precision_after_cost']):.12f},"
            f"{int(item['n'])},{ece},'{evidence}')"
        )
    query.update({
        "engine": "Referenced deterministic evidence",
        "language": "python/json/sql",
        "sql": (
            "WITH evidence(method,precision_after_cost,n,ece,evidence_class) "
            "AS (\n  VALUES\n" + ",\n".join(sql_rows)
            + "\n)\nSELECT * FROM evidence"
        ),
        "description": (
            "同图比较生产信号、GBDT、组内排序和方向间距的确认/历史结果。"
        ),
        "executed_at": diagnostic["generated_at_utc"],
        "tables_used": list(dict.fromkeys(
            list(query.get("tables_used", [])) + [diagnostic_relative_path]
        )),
        "filters": [
            "统一扣20bp",
            "内部确认与历史留出分层",
            "历史结果不授权生产变更",
        ],
        "metric_definitions": [
            "精确率=冻结选择在相应时间窗中扣20bp后方向收益为正的比例",
            "90%声明同时要求Wilson 95%下界>=90%和独立未来窗",
        ],
    })

    headline = datasets["headline"][0]
    headline.update({
        "directional_confirmation_precision": float(confirmation["precision"]),
        "directional_confirmation_wilson_low": float(
            confirmation["wilson_95_low"]),
        "directional_confirmation_n": int(confirmation["n"]),
        "directional_holdout_precision": float(holdout["precision"]),
        "directional_holdout_n": int(holdout["n"]),
        "directional_holdout_oracle_rate": float(
            root_cause["historical_holdout_oracle_any_candidate_success_rate"]),
        "directional_holdout_ranking_gap": float(
            root_cause["historical_holdout_ranking_gap"]),
    })
    gate = _one(datasets["gates"], "goal", "分析可信度")
    gate["current"] = (
        str(gate["current"])
        + f"；方向间距内部确认 {confirmation['precision']:.3%}"
        f"（N={confirmation['n']}，Wilson下界"
        f"{confirmation['wilson_95_low']:.3%}）"
    )
    gate["status"] = "未达标"
    gate["next_gate"] = (
        "用08:00起冻结影子记录的方向概率差与官方15m合约OI/主动买卖量"
        "形成新未来样本；只在点精度和Wilson下界都达到90%后再谈风险审批"
    )
    chart = _one(manifest["charts"], "id", "credibility_chart")
    chart["subtitle"] = (
        f"方向间距内部确认{confirmation['precision']:.1%}、历史留出"
        f"{holdout['precision']:.1%}；高任一候选成功率没有转化为方向精度。"
    )
    section = _one(manifest["blocks"], "id", "credibility_section")
    section["body"] = str(section["body"]).rstrip() + (
        "\n\n进一步把波动机会与方向选择拆开后，模型选择窗固定的 `"
        f"{diagnostic['selected_policy']['policy']}` 策略在内部确认仅"
        f"{confirmation['precision']:.3%}（N={confirmation['n']}，Wilson下界"
        f"{confirmation['wilson_95_low']:.3%}），历史留出仅"
        f"{holdout['precision']:.3%}（N={holdout['n']}）。后者被选样本中"
        f"事后至少一个候选成功率为"
        f"{holdout['selected_subset_any_candidate_success_rate']:.3%}，但实际"
        f"只抓到{holdout['capture_rate_when_any_candidate_succeeds']:.3%}的"
        "机会；这证明绝对概率主要识别波动，而非可靠方向。固定32规则族"
        "即使逐窗事后挑最好规则也不足48%，所以继续拒绝生产扩容。"
    )
    return artifact


def refresh_news_source_health(
    artifact: dict,
    audit: dict,
    *,
    audit_relative_path: str,
) -> dict:
    """Add strict scheduled-slot news-source evidence to the full report."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if audit.get("artifact_type") != "scheduled_news_source_health_audit":
        raise ValueError("unexpected news-source audit")
    if (
        bool(audit.get("production_mutation", True))
        or bool(audit.get("collector_retry_triggered", True))
        or bool(audit.get("stage_dispatch_triggered", True))
        or int(audit.get("orders_placed", -1)) != 0
        or bool(audit.get("production_execution_authorized", True))
    ):
        raise ValueError("news-source audit safety flags invalid")
    forward = audit["forward_after_remediation"]
    rows = list(forward["sources"])
    if not rows:
        raise ValueError("news-source audit has no source rows")
    target = float(audit["target_rate"])
    target_pct = f"{target:.0%}"
    if not 0 < target <= 1:
        raise ValueError("news-source target invalid")
    source_ids = [str(item.get("source") or "") for item in rows]
    if any(not source for source in source_ids) or len(set(source_ids)) != len(
        source_ids
    ):
        raise ValueError("news-source ids invalid or duplicated")
    required_sources = {
        "okx_news", "rss_en", "rss:bitcoinist", "rss:coindesk",
        "rss:cointelegraph", "rss:cryptoslate", "rss:decrypt",
        "rss:theblock",
    }
    if not required_sources.issubset(source_ids):
        raise ValueError("news-source critical source set incomplete")
    for item in rows:
        expected = int(item["expected_slots"])
        observed = int(item["observed_rows"])
        missing = int(item["missing_slots"])
        complete = int(item["complete_slots"])
        degraded_or_failed = int(item["degraded_or_failed_slots"])
        minimum = int(item["minimum_slots"])
        strict_rate = float(item["strict_complete_rate"])
        available_rate = float(item["available_rate"])
        interval = int(item["schedule_minutes"])
        expected_minimum = max(
            1,
            int(math.ceil(
                int(audit["minimum_window_hours"]) * 60 / interval
            )),
        ) if interval > 0 else -1
        raw_counts = item.get("raw_status_counts")
        if not isinstance(raw_counts, dict):
            raise ValueError("news-source raw status counts missing")
        raw_total = sum(int(value) for value in raw_counts.values())
        available = int(raw_counts.get("ok", 0)) + int(
            raw_counts.get("degraded", 0)
        )
        expected_strict_rate = complete / expected if expected else 0.0
        expected_available_rate = available / expected if expected else 0.0
        expected_status = (
            "INSUFFICIENT_EVIDENCE" if expected < minimum else
            "PASSED" if strict_rate >= target else "NOT_MET"
        )
        if (
            min(expected, observed, missing, complete, degraded_or_failed) < 0
            or interval < 15 or interval % 15 != 0
            or minimum != expected_minimum
            or not math.isclose(
                float(item["target_rate"]), target,
                rel_tol=0.0, abs_tol=1.0e-12,
            )
            or str(item.get("start_cst")) != str(audit["forward_start_cst"])
            or observed + missing != expected
            or complete + degraded_or_failed != observed
            or complete != int(raw_counts.get("ok", 0))
            or complete > available or available > observed
            or raw_total != observed
            or int(item["exception_count"]) != expected - complete
            or not math.isclose(
                strict_rate, expected_strict_rate,
                rel_tol=0.0, abs_tol=1.0e-6,
            )
            or not math.isclose(
                available_rate, expected_available_rate,
                rel_tol=0.0, abs_tol=1.0e-6,
            )
            or str(item.get("status")) != expected_status
        ):
            raise ValueError(
                f"news-source evidence disagrees for {item['source']}"
            )

    def window_status(selected: list[dict]) -> str:
        if any(str(item["status"]) == "NOT_MET" for item in selected):
            return "NOT_MET"
        if any(
            str(item["status"]) == "INSUFFICIENT_EVIDENCE"
            for item in selected
        ):
            return "INSUFFICIENT_EVIDENCE"
        return "PASSED"

    critical_roles = {"required", "official_required", "required_subsource"}
    critical_rows = [item for item in rows if item.get("role") in critical_roles]
    expected_critical_status = window_status(critical_rows)
    expected_all_status = window_status(rows)
    expected_overall_status = {
        "PASSED": "PASSED",
        "INSUFFICIENT_EVIDENCE": "PENDING_FORWARD_EVIDENCE",
        "NOT_MET": "NOT_MET",
    }[expected_all_status]
    if (
        str(forward.get("critical_status")) != expected_critical_status
        or str(forward.get("all_sources_status")) != expected_all_status
        or str(audit.get("overall_status")) != expected_overall_status
    ):
        raise ValueError("news-source aggregate status disagrees with rows")
    manifest = artifact["manifest"]
    _advance_report_generated_at(artifact, _source_audit_utc(audit))
    source = _upsert_id(manifest["sources"], "news_source_health")
    values = []
    for item in rows:
        source_id = str(item["source"]).replace("'", "''")
        role = str(item["role"]).replace("'", "''")
        status = str(item["status"]).replace("'", "''")
        values.append(
            f"    ('{source_id}','{role}',{int(item['expected_slots'])},"
            f"{int(item['complete_slots'])},"
            f"{float(item['strict_complete_rate']):.12f},'{status}')"
        )
    source.update({
        "label": "新闻父源与英文RSS子源严格计划槽审计",
        "path": audit_relative_path,
        "query": {
            "engine": "SQLite read-only scheduled-slot reconstruction",
            "language": "python/sql",
            "sql": (
                "WITH news_health(source,role,expected_slots,complete_slots,"
                "strict_complete_rate,status) AS (\n  VALUES\n"
                + ",\n".join(values)
                + "\n)\nSELECT * FROM news_health"
            ),
            "description": (
                "按registry真实15m/60m/120m计划槽重建分母；缺行和degraded"
                "均不计入严格完整率，成功请求0条仍是合法无事件。"
            ),
            "executed_at": audit["generated_at_cst"],
            "tables_used": [
                "ledger.db.collection_runs",
                "collectors/sources/registry.json",
                audit_relative_path,
            ],
            "filters": [
                f"forward_start={audit['forward_start_cst']}",
                f"minimum_window_hours={audit['minimum_window_hours']}",
                "critical=rss_en+okx_news+six RSS publishers",
            ],
            "metric_definitions": [
                "严格完整率=status ok的应运行槽/全部应运行槽",
                f"可用率=(ok+degraded)/全部应运行槽，仅作诊断不替代{target_pct}门槛",
            ],
        },
    })
    dataset_rows = [{
        "source": item["source"],
        "role": item["role"],
        "schedule_minutes": int(item["schedule_minutes"]),
        "expected_slots": int(item["expected_slots"]),
        "observed_rows": int(item["observed_rows"]),
        "complete_slots": int(item["complete_slots"]),
        "missing_slots": int(item["missing_slots"]),
        "strict_complete_rate": float(item["strict_complete_rate"]),
        "available_rate": float(item["available_rate"]),
        "exception_count": int(item["exception_count"]),
        "status": item["status"],
    } for item in rows]
    datasets = artifact["snapshot"]["datasets"]
    datasets["news_source_health"] = dataset_rows
    critical = [item for item in dataset_rows if item["role"] in critical_roles]
    all_complete = sum(item["complete_slots"] for item in dataset_rows)
    all_expected = sum(item["expected_slots"] for item in dataset_rows)
    headline = datasets["headline"][0]
    headline.update({
        "news_forward_critical_status": forward["critical_status"],
        "news_forward_all_status": forward["all_sources_status"],
        "news_critical_source_count": len(critical),
        "news_forward_expected_source_slots": sum(
            item["expected_slots"] for item in critical),
        "news_forward_complete_source_slots": sum(
            item["complete_slots"] for item in critical),
        "news_forward_all_complete_source_slots": all_complete,
        "news_forward_all_expected_source_slots": all_expected,
        "news_minimum_window_hours": int(audit["minimum_window_hours"]),
        "news_forward_start_cst": str(audit["forward_start_cst"]),
    })
    table = _upsert_id(manifest["tables"], "news_source_health_table")
    table.update({
        "title": "新闻来源严格计划槽前向验收",
        "subtitle": (
            "ok才计完整；degraded与缺行进入分母，成功0条是合法无事件。"
        ),
        "dataset": "news_source_health",
        "sourceId": "news_source_health",
        "density": "dense",
        "columns": [
            {"field": "source", "label": "来源", "type": "text"},
            {"field": "role", "label": "角色", "type": "text"},
            {"field": "schedule_minutes", "label": "节奏(分钟)", "format": "number"},
            {"field": "complete_slots", "label": "完整槽", "format": "number"},
            {"field": "expected_slots", "label": "应运行槽", "format": "number"},
            {"field": "strict_complete_rate", "label": "严格完整率", "format": "percent"},
            {"field": "available_rate", "label": "可用率", "format": "percent"},
            {"field": "status", "label": f"24h/{target_pct}闸门", "type": "text"},
        ],
        "defaultSort": {"field": "source", "direction": "asc"},
        "layout": "full",
    })
    block = {
        "id": "news_source_health_block",
        "type": "table",
        "tableId": "news_source_health_table",
        "layout": "full",
    }
    blocks = manifest["blocks"]
    existing = next(
        (item for item in blocks if item.get("id") == block["id"]), None)
    if existing is not None:
        existing.clear()
        existing.update(block)
    else:
        insert_at = next(
            (index + 1 for index, item in enumerate(blocks)
             if item.get("id") in {"fast_source_health_block", "source_health_block"}),
            len(blocks),
        )
        blocks.insert(insert_at, block)
    section = _upsert_id(blocks, "news_source_section")
    child_rows = [
        item for item in dataset_rows if item["role"] == "required_subsource"
    ]
    child_complete = sum(item["complete_slots"] for item in child_rows)
    child_expected = sum(item["expected_slots"] for item in child_rows)
    panews = next(
        (item for item in dataset_rows if item["source"] == "panews"), None)
    panews_text = (
        f"PANews官方RSS主路径与官网服务端页面有界回退当前严格槽"
        f"{panews['complete_slots']}/{panews['expected_slots']}="
        f"{panews['strict_complete_rate']:.3%}，状态{panews['status']}。"
        if panews else "PANews当前证据缺失。"
    )
    section.update({
        "type": "markdown",
        "body": (
            "## 全部已启用确定性新闻源均按严格槽累计，24小时前向证据仍在积累\n\n"
            "持续403/超时的CryptoPotato已由官网明确提供且生产同路径实测"
            "可达的CryptoSlate RSS替换；请求成功但0条新事件记为完整。自"
            f"{audit['forward_start_cst']}起，六个英文发布方当前严格槽合计"
            f"{child_complete}/{child_expected}；全部已启用确定性新闻源合计"
            f"{all_complete}/{all_expected}。关键源子状态为"
            f"{forward['critical_status']}，总体状态为{forward['all_sources_status']}，"
            f"仍须至少{audit['minimum_window_hours']}小时后才可判定{target_pct}。"
            f"其中尚未到应运行时点的低频源仍按各自计划槽和24小时最小分母"
            f"验收，不能用当前零槽提前判定。{panews_text}"
        ),
    })
    blocks.remove(section)
    insert_at = next(
        (index + 1 for index, item in enumerate(blocks)
         if item.get("id") == "data_section"),
        2,
    )
    blocks.insert(insert_at, section)
    gate = _one(datasets["gates"], "goal", "关键数据完善率")
    base_current = re.sub(r"；新闻严格前向.*$", "", str(gate.get("current") or ""))
    gate["current"] = (
        base_current
        + f"；新闻严格前向{all_complete}/{all_expected}全部已启用源槽，"
        f"状态{forward['all_sources_status']}"
    )
    gate["status"] = "未达标"
    gate["next_gate"] = (
        "4H与fast继续独立验收；全部已启用确定性新闻源至少观察24小时，"
        f"每源严格完整率均须>={target_pct}，关键源状态仅作子指标"
    )
    return artifact


POSITIONING_BATCH_PRIMARY_KEY = (
    "cycle_id", "symbol", "timeframe", "source",
)
POSITIONING_HOURLY_FORWARD_START = "2026-08-13T03:00:00+08:00"
POSITIONING_AVAILABILITY_FORWARD_START = "2026-08-13T03:00:00+08:00"
POSITIONING_HOURLY_MINIMUM_SLOTS = 24
POSITIONING_AVAILABILITY_MINIMUM_SLOTS = 96


def _validate_positioning_forward_window(
    window: dict,
    *,
    label: str,
    expected_start: str,
    expected_schedule_minutes: int,
    expected_minimum_slots: int,
    target: float,
) -> dict:
    if not isinstance(window, dict):
        raise ValueError(f"positioning {label} window missing")
    if (
        str(window.get("start_cst")) != expected_start
        or int(window.get("schedule_minutes", -1)) != expected_schedule_minutes
        or int(window.get("minimum_slots", -1)) != expected_minimum_slots
        or not math.isclose(
            float(window.get("target_rate", -1)), target,
            rel_tol=0.0, abs_tol=1.0e-12,
        )
    ):
        raise ValueError(f"positioning {label} governance changed")
    slots = list(window.get("slots") or [])
    expected_slots = int(window.get("expected_slots", -1))
    if expected_slots != len(slots):
        raise ValueError(f"positioning {label} slot count disagrees")
    passed_slots = 0
    expected_symbol_rows = 0
    valid_symbol_rows = 0
    official_passed = 0
    for slot in slots:
        denominator = int(slot.get("expected_symbols", -1))
        valid = int(slot.get("valid_symbols", -1))
        coverage = float(slot.get("coverage_rate", -1))
        official = slot.get("official_instrument_snapshot")
        if (
            not isinstance(official, dict)
            or denominator <= 0
            or not 0 <= valid <= denominator
            or not math.isclose(
                coverage, valid / denominator,
                rel_tol=0.0, abs_tol=1.0e-6,
            )
        ):
            raise ValueError(f"positioning {label} slot values disagree")
        official_ok = str(official.get("status")) == "PASSED"
        metadata_rate = float(official.get("metadata_coverage_rate", 0.0))
        slot_passed = bool(
            official_ok
            and metadata_rate >= target
            and not list(slot.get("batch_reasons") or [])
            and coverage >= target
            and int(slot.get("invalid_row_count", 0)) == 0
            and not list(slot.get("duplicate_symbols") or [])
            and not list(slot.get("extra_symbols") or [])
        )
        expected_slot_status = "PASSED" if slot_passed else "NOT_MET"
        if str(slot.get("status")) != expected_slot_status:
            raise ValueError(f"positioning {label} slot status disagrees")
        passed_slots += int(slot_passed)
        expected_symbol_rows += denominator
        valid_symbol_rows += valid
        official_passed += int(official_ok)
    slot_rate = passed_slots / expected_slots if expected_slots else 0.0
    symbol_rate = (
        valid_symbol_rows / expected_symbol_rows
        if expected_symbol_rows else 0.0
    )
    official_rate = (
        official_passed / expected_slots if expected_slots else 0.0
    )
    reported = {
        "passed_slots": int(window.get("passed_slots", -1)),
        "expected_symbol_rows": int(window.get("expected_symbol_rows", -1)),
        "valid_symbol_rows": int(window.get("valid_symbol_rows", -1)),
    }
    expected_counts = {
        "passed_slots": passed_slots,
        "expected_symbol_rows": expected_symbol_rows,
        "valid_symbol_rows": valid_symbol_rows,
    }
    if reported != expected_counts:
        raise ValueError(f"positioning {label} aggregate counts disagree")
    for field, value in (
        ("slot_pass_rate", slot_rate),
        ("symbol_coverage_rate", symbol_rate),
        ("official_snapshot_slot_rate", official_rate),
    ):
        if not math.isclose(
            float(window.get(field, -1)), value,
            rel_tol=0.0, abs_tol=1.0e-6,
        ):
            raise ValueError(f"positioning {label} {field} disagrees")
    requirements = {
        "minimum_slots_met": expected_slots >= expected_minimum_slots,
        "slot_pass_rate_at_least_target": slot_rate >= target,
        "symbol_coverage_rate_at_least_target": symbol_rate >= target,
        "official_snapshot_slot_rate_at_least_target": official_rate >= target,
    }
    if window.get("requirements") != requirements:
        raise ValueError(f"positioning {label} requirements disagree")
    expected_status = (
        "INSUFFICIENT_EVIDENCE"
        if not requirements["minimum_slots_met"]
        else "PASSED"
        if all(requirements.values())
        else "NOT_MET"
    )
    if str(window.get("status")) != expected_status:
        raise ValueError(f"positioning {label} status disagrees")
    return {
        "start_cst": expected_start,
        "expected_slots": expected_slots,
        "passed_slots": passed_slots,
        "minimum_slots": expected_minimum_slots,
        "slot_pass_rate": slot_rate,
        "symbol_coverage_rate": symbol_rate,
        "official_snapshot_slot_rate": official_rate,
        "status": expected_status,
    }


def _missing_positioning_window(
    *, start: str, minimum_slots: int,
) -> dict:
    return {
        "start_cst": start,
        "expected_slots": 0,
        "passed_slots": 0,
        "minimum_slots": minimum_slots,
        "slot_pass_rate": 0.0,
        "symbol_coverage_rate": 0.0,
        "official_snapshot_slot_rate": 0.0,
        "status": "NOT_PROVIDED",
    }


def refresh_positioning_coverage(
    artifact: dict,
    natural_audit: dict,
    isolated_audit: dict,
    *,
    natural_relative_path: str,
    isolated_relative_path: str,
) -> dict:
    """Add latest, immutable-batch and forward-window positioning evidence."""
    for audit in (natural_audit, isolated_audit):
        if audit.get("artifact_type") != "positioning_coverage_audit":
            raise ValueError("unexpected positioning audit")
        if audit.get("source") != "okx_rest_contract_long_short_ratio":
            raise ValueError("unexpected positioning source")
    natural_rate = float(natural_audit["coverage_rate"])
    isolated_rate = float(isolated_audit["coverage_rate"])
    target = float(natural_audit["minimum_rate"])
    target_pct = f"{target:.0%}"
    if not 0 <= natural_rate <= 1 or not 0 <= isolated_rate <= 1:
        raise ValueError("invalid positioning coverage rate")
    natural_valid = int(natural_audit["valid_symbols"])
    natural_universe = int(natural_audit["universe_symbols"])
    isolated_valid = int(isolated_audit["valid_symbols"])
    isolated_universe = int(isolated_audit["universe_symbols"])
    if (
        natural_universe <= 0
        or isolated_universe <= 0
        or not 0 <= natural_valid <= natural_universe
        or not 0 <= isolated_valid <= isolated_universe
        or not math.isclose(
            natural_rate, natural_valid / natural_universe,
            rel_tol=0.0, abs_tol=1.0e-6,
        )
        or not math.isclose(
            isolated_rate, isolated_valid / isolated_universe,
            rel_tol=0.0, abs_tol=1.0e-6,
        )
    ):
        raise ValueError("positioning counts and rates disagree")
    natural_expected_status = (
        "PASSED"
        if (
            natural_rate >= target
            and not list(natural_audit.get("invalid_rows") or [])
            and not list(natural_audit.get("duplicate_symbols") or [])
            and not list(natural_audit.get("extra_symbols") or [])
        )
        else "NOT_MET"
    )
    isolated_expected_status = (
        "PASSED"
        if (
            isolated_rate >= target
            and not list(isolated_audit.get("invalid_rows") or [])
            and not list(isolated_audit.get("duplicate_symbols") or [])
            and not list(isolated_audit.get("extra_symbols") or [])
        )
        else "NOT_MET"
    )
    if (
        str(natural_audit.get("status")) != natural_expected_status
        or str(isolated_audit.get("status")) != isolated_expected_status
    ):
        raise ValueError("positioning latest status disagrees")
    safety_fields = {
        "mode", "production_database_writes",
        "production_threshold_change_allowed", "orders_placed",
    }
    if safety_fields & set(natural_audit):
        if (
            str(natural_audit.get("mode")) != "read_only"
            or int(natural_audit.get("production_database_writes", -1)) != 0
            or bool(natural_audit.get(
                "production_threshold_change_allowed", True))
            or int(natural_audit.get("orders_placed", -1)) != 0
        ):
            raise ValueError("positioning audit safety flags invalid")

    storage = natural_audit.get("storage_contract")
    if isinstance(storage, dict):
        expected_key = list(POSITIONING_BATCH_PRIMARY_KEY)
        storage_passed = bool(
            storage.get("expected_primary_key") == expected_key
            and storage.get("actual_primary_key") == expected_key
            and bool(storage.get(
                "cross_cycle_upstream_ts_reuse_supported", False))
            and str(storage.get("status")) == "PASSED"
        )
        if str(storage.get("status")) == "PASSED" and not storage_passed:
            raise ValueError("positioning storage contract status disagrees")
    else:
        storage_passed = False

    hourly_raw = natural_audit.get("forward_after_remediation")
    hourly = (
        _validate_positioning_forward_window(
            hourly_raw,
            label="hourly",
            expected_start=POSITIONING_HOURLY_FORWARD_START,
            expected_schedule_minutes=60,
            expected_minimum_slots=POSITIONING_HOURLY_MINIMUM_SLOTS,
            target=target,
        )
        if isinstance(hourly_raw, dict)
        else _missing_positioning_window(
            start=POSITIONING_HOURLY_FORWARD_START,
            minimum_slots=POSITIONING_HOURLY_MINIMUM_SLOTS,
        )
    )
    availability_raw = natural_audit.get("decision_availability_forward")
    availability = (
        _validate_positioning_forward_window(
            availability_raw,
            label="availability",
            expected_start=POSITIONING_AVAILABILITY_FORWARD_START,
            expected_schedule_minutes=15,
            expected_minimum_slots=POSITIONING_AVAILABILITY_MINIMUM_SLOTS,
            target=target,
        )
        if isinstance(availability_raw, dict)
        else _missing_positioning_window(
            start=POSITIONING_AVAILABILITY_FORWARD_START,
            minimum_slots=POSITIONING_AVAILABILITY_MINIMUM_SLOTS,
        )
    )
    latest_passed = natural_expected_status == "PASSED"
    if isinstance(hourly_raw, dict) and isinstance(availability_raw, dict):
        if not storage_passed:
            expected_overall = "NOT_MET"
        elif "INSUFFICIENT_EVIDENCE" in {
            hourly["status"], availability["status"],
        }:
            expected_overall = "PENDING_FORWARD_EVIDENCE"
        elif (
            latest_passed
            and hourly["status"] == "PASSED"
            and availability["status"] == "PASSED"
        ):
            expected_overall = "PASSED"
        else:
            expected_overall = "NOT_MET"
        if str(natural_audit.get("overall_status")) != expected_overall:
            raise ValueError("positioning overall status disagrees")
    else:
        expected_overall = "NOT_MET"

    manifest = artifact["manifest"]
    _advance_report_generated_at(
        artifact, str(natural_audit["generated_at_utc"]),
    )
    sources = manifest["sources"]
    natural_source = _upsert_id(sources, "positioning_coverage")
    natural_source.update({
        "label": "官方REST持仓倾向不可覆盖批次与双前向审计",
        "path": natural_relative_path,
        "query": {
            "engine": "SQLite read-only exact-batch audit",
            "language": "python/sql",
            "sql": (
                "SELECT * FROM market_positioning WHERE source=? "
                "AND timeframe='1H' AND collected_ts=? ORDER BY symbol"
            ),
            "description": (
                "以最新ticker交易宇宙为分母，先验证cycle级不可覆盖主键，"
                "再对最新自然批次、24个整点批次窗和96个15分钟决策"
                "可用性窗逐币校验缺失、重复、比例代数和真实来源时间。"
            ),
            "executed_at": natural_audit["generated_at_utc"],
            "tables_used": [
                "market.db.tick_snapshots",
                "market.db.official_instrument_snapshot_runs/rows",
                "market.db.market_positioning",
                natural_relative_path,
            ],
            "filters": [
                "最新USDT线性SWAP ticker宇宙",
                "source=okx_rest_contract_long_short_ratio",
                "timeframe=1H且精确collected_ts批次",
                "批次主键=cycle_id+symbol+timeframe+source",
                "小时与15分钟窗均从2026-08-13 03:00 +08:00重新累计",
            ],
            "metric_definitions": [
                "覆盖率=无重复且比例合法的宇宙内symbol/最新ticker宇宙",
                "long/short为账户数占比，不是持仓名义金额",
                "最终验收=最新批次、存储契约、24整点窗、96决策槽均通过",
            ],
        },
    })
    isolated_source = _upsert_id(sources, "positioning_isolated")
    isolated_source.update({
        "label": "官方REST持仓倾向隔离全量验收",
        "path": isolated_relative_path,
        "query": {
            "engine": "Isolated SQLite read-only audit",
            "language": "python/sql",
            "description": (
                "隔离数据库验证427币全量传输、解析和写入合同；不能替代"
                "自然生产周期证据。"
            ),
            "executed_at": isolated_audit["generated_at_utc"],
            "tables_used": [isolated_relative_path],
            "filters": ["隔离数据库", "orders_placed=0"],
        },
    })
    official = [s for s in sources if s.get("id") == "official_okx"]
    if len(official) == 1:
        official[0].update({
            "href": "https://www.okx.com/docs-v5/en/",
            "query": {
                "description": (
                    "官方公共市场接口与Trading Statistics合约账户多空比说明。"
                ),
                "url": "https://www.okx.com/docs-v5/en/",
                "tables_used": [
                    "OKX V5 public market endpoints",
                    "GET /api/v5/rubik/stat/contracts/"
                    "long-short-account-ratio-contract",
                ],
            },
        })

    datasets = artifact["snapshot"]["datasets"]
    coverage_row = _upsert_key(
        datasets["coverage"], "data_family", "official_positioning_1H")
    coverage_row.update({
        "valid_symbols": natural_valid,
        "universe": natural_universe,
        "coverage_rate": natural_rate,
        "target_rate": target,
        "gap_to_target_pp": round((natural_rate - target) * 100, 3),
        "status": "达标" if natural_rate >= target else "未达标",
        "acceptance_status": (
            "达标" if expected_overall == "PASSED" else "未达标"),
        "overall_status": expected_overall,
    })
    datasets["positioning_coverage"] = [
        {
            "evidence": "隔离全量验收",
            "cycle": isolated_audit["latest_batch_collected_ts"],
            "valid_symbols": int(isolated_audit["valid_symbols"]),
            "universe_symbols": int(isolated_audit["universe_symbols"]),
            "coverage_rate": isolated_rate,
            "missing": ", ".join(isolated_audit["missing_symbols"]) or "无",
            "status": isolated_audit["status"],
            "storage_contract_status": "N/A",
            "hourly_forward_status": "N/A",
            "availability_forward_status": "N/A",
            "overall_status": "ISOLATED_ONLY",
        },
        {
            "evidence": "最新自然生产批次",
            "cycle": natural_audit["latest_batch_collected_ts"],
            "valid_symbols": int(natural_audit["valid_symbols"]),
            "universe_symbols": int(natural_audit["universe_symbols"]),
            "coverage_rate": natural_rate,
            "missing": ", ".join(natural_audit["missing_symbols"]) or "无",
            "status": natural_audit["status"],
            "storage_contract_status": (
                "PASSED" if storage_passed else "NOT_MET"),
            "hourly_forward_slots": hourly["expected_slots"],
            "hourly_forward_minimum_slots": hourly["minimum_slots"],
            "hourly_forward_status": hourly["status"],
            "availability_forward_slots": availability["expected_slots"],
            "availability_forward_minimum_slots": availability["minimum_slots"],
            "availability_forward_status": availability["status"],
            "overall_status": expected_overall,
        },
    ]
    headline = datasets["headline"][0]
    headline.update({
        "positioning_coverage_rate": natural_rate,
        "positioning_valid_symbols": int(natural_audit["valid_symbols"]),
        "positioning_universe_symbols": int(natural_audit["universe_symbols"]),
        "positioning_missing_symbols": len(natural_audit["missing_symbols"]),
        "positioning_storage_contract_status": (
            "PASSED" if storage_passed else "NOT_MET"),
        "positioning_hourly_forward_expected_slots": hourly["expected_slots"],
        "positioning_hourly_forward_minimum_slots": hourly["minimum_slots"],
        "positioning_hourly_forward_status": hourly["status"],
        "positioning_availability_forward_expected_slots": (
            availability["expected_slots"]),
        "positioning_availability_forward_minimum_slots": (
            availability["minimum_slots"]),
        "positioning_availability_forward_status": availability["status"],
        "positioning_overall_status": expected_overall,
    })

    table = _upsert_id(manifest["tables"], "positioning_coverage_table")
    table.update({
        "title": "官方REST持仓倾向：隔离、自然批次与长期验收",
        "subtitle": (
            "隔离成功不替代自然证据；最新批次达标也不替代"
            "24个整点与96个决策槽。"),
        "dataset": "positioning_coverage",
        "sourceId": "positioning_coverage",
        "density": "dense",
        "columns": [
            {"field": "evidence", "label": "证据类型", "type": "text"},
            {"field": "cycle", "label": "批次完成时刻", "type": "text"},
            {"field": "valid_symbols", "label": "有效币", "format": "number"},
            {"field": "universe_symbols", "label": "宇宙", "format": "number"},
            {"field": "coverage_rate", "label": "覆盖率", "format": "percent"},
            {"field": "missing", "label": "缺失", "type": "text"},
            {"field": "status", "label": "最新批次", "type": "text"},
            {"field": "storage_contract_status", "label": "批次键", "type": "text"},
            {"field": "hourly_forward_status", "label": "24整点窗", "type": "text"},
            {"field": "availability_forward_status", "label": "96决策槽", "type": "text"},
            {"field": "overall_status", "label": "总体", "type": "text"},
        ],
        "defaultSort": {"field": "evidence", "direction": "asc"},
        "layout": "full",
    })
    blocks = manifest["blocks"]
    positioning_block = {
        "id": "positioning_coverage_block",
        "type": "table",
        "tableId": "positioning_coverage_table",
        "layout": "full",
    }
    existing = [
        block for block in blocks
        if block.get("id") == "positioning_coverage_block"
    ]
    if existing:
        existing[0].clear()
        existing[0].update(positioning_block)
    else:
        insert_at = next(
            (index + 1 for index, block in enumerate(blocks)
             if block.get("id") == "coverage_block"),
            len(blocks),
        )
        blocks.insert(insert_at, positioning_block)
    coverage_chart = _one(manifest["charts"], "id", "coverage_chart")
    coverage_chart["subtitle"] = (
        f"官方持仓倾向最新自然批次已过{target_pct}，双前向窗仍按自然时间累计；"
        "4H新上市标的历史仍待成熟。"
    )

    per_symbol_min = min(
        float(row["coverage_rate"]) for row in datasets["coverage"])
    fast_rows = datasets.get("fast_source_health") or []
    fast = fast_rows[0] if fast_rows else None
    gate = _one(datasets["gates"], "goal", "关键数据完善率")
    fast_text = (
        f"fast计划槽14日 {float(fast['usable_rate']):.3%}，"
        f"前向 {float(fast['forward_usable_rate']):.3%}"
        f"（{fast['forward_expected_slots']}/{fast['forward_minimum_slots']}槽）"
        if fast else "fast严格计划槽待刷新"
    )
    gate.update({
        "current": (
            f"官方1H持仓倾向自然批次 {natural_rate:.3%}"
            f"（{natural_audit['valid_symbols']}/{natural_audit['universe_symbols']}，"
            f"缺{len(natural_audit['missing_symbols'])}币），批次键"
            f"{'通过' if storage_passed else '未通过'}，小时前向"
            f"{hourly['expected_slots']}/{hourly['minimum_slots']}槽"
            f"（{hourly['status']}），15分钟可用性前向"
            f"{availability['expected_slots']}/{availability['minimum_slots']}槽"
            f"（{availability['status']}）；4H逐币 "
            f"{per_symbol_min:.3%}；{fast_text}"
        ),
        "status": "未达标",
        "next_gate": (
            "持仓倾向从2026-08-13 03:00重新累计24个整点批次和96个"
            f"15分钟决策槽，三项率均须>={target_pct}；同时复核4H逐币覆盖，"
            f"fast前向至少96槽且14日滚动窗均须>={target_pct}"
        ),
    })
    data_block = _one(blocks, "id", "data_section")
    missing_text = ", ".join(natural_audit["missing_symbols"]) or "无"
    one_hour_rate = next(
        (float(row["coverage_rate"]) for row in datasets["coverage"]
         if str(row.get("data_family")) == "1H"),
        0.0,
    )
    four_hour_rate = next(
        (float(row["coverage_rate"]) for row in datasets["coverage"]
         if str(row.get("data_family")) == "4H"),
        per_symbol_min,
    )
    data_block["body"] = (
        f"## 持仓倾向最新批次已达{target_pct}，不可覆盖批次键已生效，"
        "双前向窗仍待成熟\n\n"
        "Ticker、合约元数据、15m及资金费/OI逐币覆盖均为100%；"
        f"1H为{one_hour_rate:.3%}，4H为{four_hour_rate:.3%}。"
        "官方Trading Statistics "
        "REST已独立于100币盘口集合，按最新全部USDT线性SWAP采1H账户"
        f"多空比：隔离验收{isolated_audit['valid_symbols']}/"
        f"{isolated_audit['universe_symbols']}，最新自然生产批次"
        f"{natural_audit['valid_symbols']}/{natural_audit['universe_symbols']}="
        f"{natural_rate:.3%}；精确批次缺失为{missing_text}。旧主键造成的"
        "历史缺行没有人工补写，小时批次完整性与15分钟决策可用性均从"
        f"03:00重新累计，当前分别为{hourly['expected_slots']}/"
        f"{hourly['minimum_slots']}槽和{availability['expected_slots']}/"
        f"{availability['minimum_slots']}槽，总体状态{expected_overall}。"
        "公共REST主域为openapi.okx.com，旧域只在原请求总预算内有界回退；"
        "4H闭合历史、fast长期与前向计划槽继续独立验收。"
    )
    return artifact


def refresh_multitimeframe_coverage(
    artifact: dict,
    audit: dict,
    *,
    audit_relative_path: str,
) -> dict:
    """Refresh exact-closed OHLCV and indicator-readiness evidence."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if audit.get("artifact_type") != (
        "multitimeframe_closed_bar_coverage_audit"
    ):
        raise ValueError("unexpected multitimeframe coverage audit")
    rows = list(audit.get("timeframes") or [])
    if {str(row.get("timeframe")) for row in rows} != {"15m", "1H", "4H"}:
        raise ValueError("multitimeframe audit must contain 15m/1H/4H")
    universe = int(audit["universe_symbols"])
    target = float(audit["minimum_rate"])
    if universe <= 0 or not 0 < target <= 1:
        raise ValueError("invalid multitimeframe audit denominator or target")
    if (
        str(audit.get("mode")) != "read_only"
        or int(audit.get("production_database_writes", -1)) != 0
        or bool(audit.get("production_threshold_change_allowed", True))
        or int(audit.get("orders_placed", -1)) != 0
    ):
        raise ValueError("multitimeframe audit safety flags invalid")
    raw_statuses: list[str] = []
    readiness_statuses: list[str] = []
    for row in rows:
        if int(row["universe_symbols"]) != universe:
            raise ValueError("multitimeframe denominators disagree")
        observed = int(row.get("observed_exact_bar_rows", -1))
        raw_valid = int(row["raw_ohlcv_valid_symbols"])
        ready = int(row["analysis_ready_symbols"])
        raw_rate = float(row["raw_ohlcv_coverage_rate"])
        ready_rate = float(row["analysis_ready_rate"])
        if (
            not 0 <= ready <= raw_valid <= observed <= universe
            or not 0 <= raw_rate <= 1
            or not 0 <= ready_rate <= 1
            or not math.isclose(
                raw_rate, raw_valid / universe,
                rel_tol=0.0, abs_tol=1.0e-6,
            )
            or not math.isclose(
                ready_rate, ready / universe,
                rel_tol=0.0, abs_tol=1.0e-6,
            )
        ):
            raise ValueError("multitimeframe counts or rates disagree")
        gaps = list(row.get("gaps") or [])
        gap_counts = row.get("gap_counts")
        expected_classes = {
            "source_data_invalid", "insufficient_history", "indicator_invalid",
        }
        if not isinstance(gap_counts, dict) or set(gap_counts) != expected_classes:
            raise ValueError("multitimeframe gap counts invalid")
        actual_gap_counts = {
            classification: sum(
                1 for gap in gaps
                if str(gap.get("classification")) == classification
            )
            for classification in expected_classes
        }
        gap_symbols = [str(gap.get("symbol") or "") for gap in gaps]
        if (
            actual_gap_counts != {
                key: int(gap_counts[key]) for key in expected_classes
            }
            or len(gaps) != universe - ready
            or actual_gap_counts["source_data_invalid"] != universe - raw_valid
            or len(set(gap_symbols)) != len(gap_symbols)
            or any(not symbol for symbol in gap_symbols)
        ):
            raise ValueError("multitimeframe gaps disagree with counts")
        expected_raw_status = "PASSED" if raw_rate >= target else "NOT_MET"
        expected_ready_status = "PASSED" if ready_rate >= target else "NOT_MET"
        if (
            str(row.get("raw_ohlcv_status")) != expected_raw_status
            or str(row.get("analysis_ready_status")) != expected_ready_status
        ):
            raise ValueError("multitimeframe row status disagrees with rates")
        raw_statuses.append(expected_raw_status)
        readiness_statuses.append(expected_ready_status)
    expected_data_status = (
        "PASSED" if all(status == "PASSED" for status in raw_statuses)
        else "NOT_MET"
    )
    expected_readiness_status = (
        "PASSED" if all(status == "PASSED" for status in readiness_statuses)
        else "NOT_MET"
    )
    expected_status = (
        "PASSED"
        if expected_data_status == expected_readiness_status == "PASSED"
        else "NOT_MET"
    )
    if (
        str(audit.get("data_completeness_status")) != expected_data_status
        or str(audit.get("analysis_readiness_status"))
        != expected_readiness_status
        or str(audit.get("status")) != expected_status
    ):
        raise ValueError("multitimeframe aggregate status disagrees with rows")

    generated_at = _source_audit_utc(audit)
    manifest = artifact["manifest"]
    _advance_report_generated_at(artifact, generated_at)
    sources = manifest["sources"]
    source = _upsert_id(sources, "multitimeframe_coverage")
    targets_sql = ",\n".join(
        "    ('{}','{}')".format(
            str(row["timeframe"]).replace("'", "''"),
            str(row["expected_closed_bar_ts"]).replace("'", "''"),
        )
        for row in rows
    )
    source.update({
        "label": "15m/1H/4H精确已收盘覆盖审计",
        "path": audit_relative_path,
        "query": {
            "engine": "SQLite + deterministic closed-bar audit",
            "language": "sql/python",
            "sql": (
                "WITH latest_tick AS (SELECT MAX(ts) ts FROM tick_snapshots "
                "WHERE ts<=:evaluation_at),\n"
                "universe AS (SELECT DISTINCT t.symbol FROM tick_snapshots t "
                "JOIN latest_tick x ON t.ts=x.ts WHERE t.symbol LIKE "
                "'%-USDT-SWAP'),\n"
                "targets(timeframe,bar_ts) AS (VALUES\n" + targets_sql +
                "\n), exact_rows AS (\n"
                "  SELECT targets.timeframe,targets.bar_ts,u.symbol,k.* FROM "
                "universe u CROSS JOIN targets LEFT JOIN kline_cache k ON "
                "k.symbol=u.symbol AND k.tf=targets.timeframe AND "
                "k.ts=targets.bar_ts\n)\n"
                "SELECT timeframe,bar_ts,COUNT(*) universe_symbols,"
                "SUM(o>0 AND h>0 AND l>0 AND c>0 AND v>=0) raw_valid,"
                "SUM(o>0 AND h>0 AND l>0 AND c>0 AND v>=0 AND ma5 IS NOT "
                "NULL AND ma20 IS NOT NULL AND atr14 IS NOT NULL AND rsi14 "
                "IS NOT NULL AND macd_hist IS NOT NULL) analysis_ready "
                "FROM exact_rows GROUP BY timeframe,bar_ts"
            ),
            "description": (
                "按最新ticker全宇宙与UTC周期边界选择精确已收盘K线；"
                "原始OHLCV和指标就绪分别验收。"
            ),
            "executed_at": generated_at,
            "tables_used": [
                "market.db.tick_snapshots",
                "market.db.kline_cache",
                audit_relative_path,
            ],
            "filters": [
                f"evaluation_at={audit['evaluation_at_utc']}",
                "最新ticker中的全部-USDT-SWAP",
                "只接受精确最新已收盘15m/1H/4H bar",
                "新上市历史不足仍保留在分母",
            ],
            "metric_definitions": [
                "原始OHLCV完整率=精确闭合bar中OHLC为正且成交量非负的币数/全宇宙",
                "指标就绪率=原始有效且MA5/MA20/ATR14/RSI14/MACD均有效的币数/全宇宙",
            ],
        },
    })

    datasets = artifact["snapshot"]["datasets"]
    datasets["multitimeframe_coverage"] = [
        {
            "timeframe": str(row["timeframe"]),
            "expected_closed_bar_ts": str(row["expected_closed_bar_ts"]),
            "universe_symbols": universe,
            "raw_ohlcv_valid_symbols": int(row["raw_ohlcv_valid_symbols"]),
            "raw_ohlcv_coverage_rate": float(row["raw_ohlcv_coverage_rate"]),
            "analysis_ready_symbols": int(row["analysis_ready_symbols"]),
            "analysis_ready_rate": float(row["analysis_ready_rate"]),
            "source_data_invalid": int(
                row["gap_counts"].get("source_data_invalid", 0)),
            "insufficient_history": int(
                row["gap_counts"].get("insufficient_history", 0)),
            "indicator_invalid": int(
                row["gap_counts"].get("indicator_invalid", 0)),
            "raw_status": str(row["raw_ohlcv_status"]),
            "readiness_status": str(row["analysis_ready_status"]),
        }
        for row in rows
    ]
    for audit_row in datasets["multitimeframe_coverage"]:
        coverage_row = _one(
            datasets["coverage"], "data_family", audit_row["timeframe"])
        coverage_row.update({
            "valid_symbols": audit_row["analysis_ready_symbols"],
            "universe": universe,
            "coverage_rate": audit_row["analysis_ready_rate"],
            "target_rate": target,
            "gap_to_target_pp": round(
                (audit_row["analysis_ready_rate"] - target) * 100, 3),
            "status": (
                "达标" if audit_row["analysis_ready_rate"] >= target
                else "未达标"),
            "coverage_semantics": "exact_closed_bar_analysis_readiness",
            "raw_ohlcv_coverage_rate": audit_row["raw_ohlcv_coverage_rate"],
        })
    headline = datasets["headline"][0]
    headline.update({
        "latest_market_universe_symbols": universe,
        "multitimeframe_raw_minimum_rate": min(
            row["raw_ohlcv_coverage_rate"]
            for row in datasets["multitimeframe_coverage"]),
        "multitimeframe_analysis_ready_minimum_rate": min(
            row["analysis_ready_rate"]
            for row in datasets["multitimeframe_coverage"]),
        "multitimeframe_data_completeness_status": str(
            audit["data_completeness_status"]),
        "multitimeframe_analysis_readiness_status": str(
            audit["analysis_readiness_status"]),
    })
    per_symbol_min = min(
        float(row["coverage_rate"]) for row in datasets["coverage"])
    fast_rolling_rate = float(
        (datasets.get("fast_source_health") or [{}])[0].get(
            "usable_rate", 1.0))
    headline["per_symbol_minimum_coverage_rate"] = per_symbol_min
    headline["minimum_coverage_rate"] = min(
        per_symbol_min, fast_rolling_rate)
    table = _upsert_id(manifest["tables"], "multitimeframe_coverage_table")
    table.update({
        "title": "已收盘多周期数据与指标覆盖",
        "subtitle": (
            f"全{universe}币固定分母；新区历史不足不排除，形成中K线不计入"),
        "dataset": "multitimeframe_coverage",
        "sourceId": "multitimeframe_coverage",
        "columns": [
            {"field": "timeframe", "label": "周期"},
            {"field": "expected_closed_bar_ts", "label": "闭合bar UTC"},
            {"field": "raw_ohlcv_valid_symbols", "label": "OHLCV有效币", "format": "number"},
            {"field": "raw_ohlcv_coverage_rate", "label": "OHLCV完整率", "format": "percent"},
            {"field": "analysis_ready_symbols", "label": "指标就绪币", "format": "number"},
            {"field": "analysis_ready_rate", "label": "指标就绪率", "format": "percent"},
            {"field": "insufficient_history", "label": "历史不足", "format": "number"},
            {"field": "source_data_invalid", "label": "源数据异常", "format": "number"},
        ],
        "defaultSort": {"field": "analysis_ready_rate", "direction": "desc"},
    })
    block = {
        "id": "multitimeframe_coverage_block",
        "type": "table",
        "tableId": "multitimeframe_coverage_table",
        "layout": "full",
    }
    blocks = manifest["blocks"]
    existing = [
        item for item in blocks
        if item.get("id") == "multitimeframe_coverage_block"]
    if existing:
        existing[0].clear()
        existing[0].update(block)
    else:
        insert_at = next(
            (index + 1 for index, item in enumerate(blocks)
             if item.get("id") == "coverage_block"),
            len(blocks),
        )
        blocks.insert(insert_at, block)

    coverage_source = _upsert_id(sources, "coverage_evidence")
    value_rows = []
    for row in datasets["coverage"]:
        family = str(row["data_family"]).replace("'", "''")
        status = str(row.get("status", "")).replace("'", "''")
        value_rows.append(
            f"    ('{family}',{int(row['valid_symbols'])},{int(row['universe'])},"
            f"{float(row['coverage_rate']):.12f},'{status}')"
        )
    coverage_source.update({
        "label": "关键市场数据族组合覆盖证据",
        "path": audit_relative_path,
        "query": {
            "engine": "Referenced SQLite/JSON evidence",
            "language": "sql/python/json",
            "sql": (
                "WITH coverage(data_family,valid_symbols,universe,coverage_rate,"
                "status) AS (\n  VALUES\n" + ",\n".join(value_rows) +
                "\n)\nSELECT * FROM coverage"
            ),
            "description": (
                "各关键数据族独立验收；15m/1H/4H使用精确已收盘指标就绪率。"
            ),
            "executed_at": generated_at,
            "tables_used": [
                "market.db.tick_snapshots",
                "market.db.kline_cache",
                "market.db.market_positioning",
                "market.db.market_contract_statistics",
                audit_relative_path,
            ],
            "filters": ["最新交易宇宙", "各数据族独立>=99%"],
            "metric_definitions": [
                "15m/1H/4H覆盖率=精确已收盘bar全指标就绪symbol/最新ticker全宇宙"
            ],
        },
    })
    coverage_chart = _one(manifest["charts"], "id", "coverage_chart")
    four_hour = _one(
        datasets["multitimeframe_coverage"], "timeframe", "4H")
    contract = next(
        (row for row in datasets["coverage"]
         if row.get("data_family") == "official_contract_stats_15m"), None)
    contract_chart_text = (
        ""
        if not contract else
        f"；15m合约统计最新模型可用直采"
        f"{float(contract['direct_coverage_rate']):.3%}、运行可用性"
        f"{float(contract['availability_coverage_rate']):.3%}，严格前向直采"
        f"{float(contract['forward_direct_coverage_rate']):.3%}（"
        f"{int(contract['forward_expected_slots'])}/"
        f"{int(contract['forward_minimum_slots'])}槽）"
    )
    coverage_chart.update({
        "subtitle": (
            "已收盘OHLCV分别为"
            + "、".join(
                f"{row['timeframe']} {row['raw_ohlcv_coverage_rate']:.3%}"
                for row in datasets["multitimeframe_coverage"]
            )
            + f"；4H指标就绪"
            f"{four_hour['analysis_ready_rate']:.3%}，"
            f"原始闭合K线缺口{four_hour['source_data_invalid']}个、"
            f"历史不足{four_hour['insufficient_history']}个、"
            f"指标异常{four_hour['indicator_invalid']}个"
            f"{contract_chart_text}。"),
        "sourceId": "coverage_evidence",
    })

    fast = (datasets.get("fast_source_health") or [{}])[0]
    positioning = next(
        (row for row in datasets["coverage"]
         if row.get("data_family") == "official_positioning_1H"), None)
    positioning_headline = datasets["headline"][0]
    positioning_window_text = (
        f"，批次键{positioning_headline.get('positioning_storage_contract_status', 'NOT_MET')}，"
        f"小时前向{positioning_headline.get('positioning_hourly_forward_expected_slots', 0)}/"
        f"{positioning_headline.get('positioning_hourly_forward_minimum_slots', 24)}槽，"
        f"15分钟可用性{positioning_headline.get('positioning_availability_forward_expected_slots', 0)}/"
        f"{positioning_headline.get('positioning_availability_forward_minimum_slots', 96)}槽，"
        f"总体{positioning_headline.get('positioning_overall_status', 'NOT_MET')}"
    )
    gate = _one(datasets["gates"], "goal", "关键数据完善率")
    current_parts = [
        "已收盘原始OHLCV " + "、".join(
            f"{row['timeframe']} {row['raw_ohlcv_coverage_rate']:.3%}"
            for row in datasets["multitimeframe_coverage"]
        ),
        f"4H指标就绪 {four_hour['analysis_ready_rate']:.3%}"
        f"（{four_hour['analysis_ready_symbols']}/{universe}）",
    ]
    if positioning:
        current_parts.append(
            f"官方1H持仓倾向最新批次 "
            f"{float(positioning['coverage_rate']):.3%}"
            f"{positioning_window_text}")
    if contract:
        current_parts.append(
            f"15m合约OI/主动买卖量最新直采 "
            f"{float(contract['direct_coverage_rate']):.3%}、运行可用性 "
            f"{float(contract['availability_coverage_rate']):.3%}，严格前向 "
            f"{float(contract['forward_direct_coverage_rate']):.3%}（"
            f"{int(contract['forward_expected_slots'])}/"
            f"{int(contract['forward_minimum_slots'])}槽）")
    if fast:
        current_parts.append(
            f"fast 14日 {float(fast.get('usable_rate', 0)):.3%}，前向"
            f"{fast.get('forward_expected_slots', 0)}/"
            f"{fast.get('forward_minimum_slots', 96)}槽")
    gate.update({
        "current": "；".join(current_parts),
        "status": "未达标",
        "next_gate": (
            (
                "保持新区4H指标自然成熟记录；"
                if four_hour["analysis_ready_rate"] >= target else
                "等待新区4H指标自然成熟；"
            )
            + "fast前向至少96槽且14日滚动窗>=99%；"
            "持仓倾向至少24个整点与96个决策槽均>=99%；"
            "合约统计最新直采和至少96个前向槽均>=99%；"
            "新闻关键源完成24小时严格前向验收"),
    })
    four_hour_source_gaps = sorted(
        str(gap["symbol"])
        for gap in next(
            row for row in rows if str(row["timeframe"]) == "4H"
        ).get("gaps", [])
        if gap.get("classification") == "source_data_invalid"
    )
    four_hour_history_gaps = sorted(
        str(gap["symbol"])
        for gap in next(
            row for row in rows if str(row["timeframe"]) == "4H"
        ).get("gaps", [])
        if gap.get("classification") == "insufficient_history"
    )
    four_hour_indicator_gaps = sorted(
        str(gap["symbol"])
        for gap in next(
            row for row in rows if str(row["timeframe"]) == "4H"
        ).get("gaps", [])
        if gap.get("classification") == "indicator_invalid"
    )
    data_block = _one(blocks, "id", "data_section")
    data_heading = (
        "## 三周期原始数据与4H指标就绪均已跨过99%"
        if four_hour["analysis_ready_rate"] >= target else
        "## 原始多周期数据已过99%，4H指标仍等待新区自然成熟"
    )
    supporting_parts: list[str] = []
    if positioning:
        supporting_parts.append(
            "官方1H持仓倾向最新批次"
            f"{int(positioning['valid_symbols'])}/{int(positioning['universe'])}="
            f"{float(positioning['coverage_rate']):.3%}"
            f"{positioning_window_text}"
        )
    if contract:
        supporting_parts.append(
            "官方15m合约统计本轮直采"
            f"{int(contract['valid_symbols'])}/{int(contract['universe'])}="
            f"{float(contract['direct_coverage_rate']):.3%}，"
            "修复后严格前向"
            f"{float(contract['forward_direct_coverage_rate']):.3%}"
            f"（{int(contract['forward_expected_slots'])}/"
            f"{int(contract['forward_minimum_slots'])}槽）"
        )
    if fast:
        supporting_parts.append(
            f"fast 14日严格完整率{float(fast.get('usable_rate', 0)):.3%}，"
            f"前向{int(fast.get('forward_expected_slots', 0))}/"
            f"{int(fast.get('forward_minimum_slots', 96))}槽"
        )
    data_block["body"] = (
        data_heading + "\n\n"
        "精确已收盘原始OHLCV分别为" + "、".join(
            f"{row['timeframe']} {row['raw_ohlcv_valid_symbols']}/{universe}="
            f"{row['raw_ohlcv_coverage_rate']:.3%}"
            for row in datasets["multitimeframe_coverage"]
        ) + "；"
        f"指标就绪分别为" + "、".join(
            f"{row['timeframe']} {row['analysis_ready_symbols']}/{universe}="
            f"{row['analysis_ready_rate']:.3%}"
            for row in datasets["multitimeframe_coverage"]
        ) + "。4H缺口按原因拆分：精确闭合K线缺口" +
        ("、".join(four_hour_source_gaps) if four_hour_source_gaps else "无") +
        "；历史不足" +
        ("、".join(four_hour_history_gaps) if four_hour_history_gaps else "无") +
        "；指标异常" +
        ("、".join(four_hour_indicator_gaps) if four_hour_indicator_gaps else "无") +
        "。这些交易对仍留在固定分母，未补造K线或指标。"
        + (
            "\n\n" + "；".join(supporting_parts) + "。"
            if supporting_parts else ""
        )
        + "所有比例按当前审计固定分母独立计算，不以其他数据族平均抵消。"
    )
    _sync_title_block(manifest)
    return artifact


def refresh_asset_class_coverage(
    artifact: dict,
    audit: dict,
    *,
    audit_relative_path: str,
) -> dict:
    """Add row-presence and official semantic compatibility evidence."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if audit.get("artifact_type") != "official_asset_class_coverage_audit":
        raise ValueError("unexpected asset-class coverage audit")
    if audit.get("mode") != "read_only":
        raise ValueError("asset-class audit must be read-only")
    if (
        int(audit.get("production_database_writes", -1)) != 0
        or int(audit.get("orders_placed", -1)) != 0
        or bool(audit.get("collector_triggered"))
    ):
        raise ValueError("asset-class audit safety flags invalid")

    universe = int(audit["official_universe_symbols"])
    local_rows = int(audit["local_rows"])
    compatible = int(audit["official_compatible_symbols"])
    target = float(audit["minimum_rate"])
    row_rate = float(audit["row_coverage_rate"])
    compatibility_rate = float(audit["official_compatibility_rate"])
    if universe <= 0 or not 0 <= compatible <= local_rows <= universe:
        raise ValueError("asset-class counts invalid")
    if not 0 < target <= 1:
        raise ValueError("asset-class target invalid")
    if not math.isclose(
        row_rate, local_rows / universe, rel_tol=1e-12, abs_tol=1e-12,
    ):
        raise ValueError("asset-class row coverage disagrees with counts")
    if not math.isclose(
        compatibility_rate, compatible / universe,
        rel_tol=1e-12, abs_tol=1e-12,
    ):
        raise ValueError("asset-class compatibility disagrees with counts")
    missing = list(audit.get("missing_symbols") or [])
    mismatches = list(audit.get("mismatches") or [])
    unsupported = list(audit.get("unsupported_official_symbols") or [])
    if len(missing) != universe - local_rows:
        raise ValueError("asset-class missing symbols disagree with counts")
    if len(mismatches) != local_rows - compatible:
        raise ValueError("asset-class mismatches disagree with counts")
    if universe - compatible != len(missing) + len(mismatches):
        raise ValueError("asset-class gap totals disagree with counts")
    checks = audit.get("checks")
    if not isinstance(checks, dict) or set(checks) != {
        "row_coverage_at_least_target",
        "official_compatibility_at_least_target",
        "no_unsupported_official_categories",
    }:
        raise ValueError("asset-class checks invalid")
    expected_checks = {
        "row_coverage_at_least_target": row_rate >= target,
        "official_compatibility_at_least_target": compatibility_rate >= target,
        "no_unsupported_official_categories": not unsupported,
    }
    if checks != expected_checks:
        raise ValueError("asset-class checks disagree with evidence")
    expected_status = "PASSED" if all(expected_checks.values()) else "NOT_MET"
    if str(audit.get("status")) != expected_status:
        raise ValueError("asset-class status disagrees with evidence")

    generated_at = datetime.fromisoformat(
        str(audit["generated_at_cst"])
    ).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = artifact["manifest"]
    source = _upsert_id(manifest["sources"], "asset_class_coverage")
    source.update({
        "label": "OKX官方资产类别覆盖与语义兼容审计",
        "path": audit_relative_path,
        "query": {
            "engine": "OKX public metadata + SQLite read-only audit",
            "language": "python/sql",
            "sql": (
                "SELECT symbol,asset_class,source,updated_at "
                "FROM instrument_class WHERE symbol IN (:official_universe)"
            ),
            "description": (
                "以OKX当前live线性USDT永续及instCategory为固定分母，"
                "把本地分类有行率与官方类别兼容率分开验收。"
            ),
            "executed_at": generated_at,
            "tables_used": [
                "OKX GET /api/v5/public/instruments instCategory",
                "market.db.instrument_class",
                audit_relative_path,
            ],
            "filters": [
                "instType=SWAP, settleCcy=USDT, ctType=linear, state=live",
                "官方1=crypto、3=stocks、4=commodities",
                "ETF/指数细分类可兼容官方stocks大类",
            ],
            "metric_definitions": [
                "有行率=官方宇宙中存在本地分类行的symbol/官方宇宙",
                "官方兼容率=本地类别与官方instCategory兼容的symbol/官方宇宙",
            ],
        },
    })
    official = _upsert_id(manifest["sources"], "official_okx")
    official_query = dict(official.get("query") or {})
    official_tables = list(official_query.get("tables_used") or [])
    for table_name in (
        "GET /api/v5/public/instruments instCategory",
    ):
        if table_name not in official_tables:
            official_tables.append(table_name)
    official.update({
        "label": "OKX V5 API 官方文档",
        "href": "https://www.okx.com/docs-v5/en/",
        "query": {
            **official_query,
            "url": "https://www.okx.com/docs-v5/en/",
            "description": (
                "官方公共市场、Trading Statistics与instrument instCategory"
                "字段定义。"
            ),
            "tables_used": official_tables,
        },
    })

    datasets = artifact["snapshot"]["datasets"]
    datasets["asset_class_coverage"] = [
        {
            "metric": "本地分类有行率",
            "valid_symbols": local_rows,
            "universe_symbols": universe,
            "coverage_rate": row_rate,
            "missing_or_mismatch_count": len(missing),
            "status": "达标" if row_rate >= target else "未达标",
        },
        {
            "metric": "OKX官方类别兼容率",
            "valid_symbols": compatible,
            "universe_symbols": universe,
            "coverage_rate": compatibility_rate,
            "missing_or_mismatch_count": (
                universe - compatible),
            "status": (
                "达标" if compatibility_rate >= target and not unsupported
                else "未达标"),
        },
    ]
    for family, valid, rate, passed in (
        ("asset_class_row_presence", local_rows, row_rate, row_rate >= target),
        (
            "asset_class_official_compatibility",
            compatible,
            compatibility_rate,
            compatibility_rate >= target and not unsupported,
        ),
    ):
        coverage_row = _upsert_key(
            datasets["coverage"], "data_family", family)
        coverage_row.update({
            "valid_symbols": valid,
            "universe": universe,
            "coverage_rate": rate,
            "target_rate": target,
            "gap_to_target_pp": round((rate - target) * 100, 3),
            "status": "达标" if passed else "未达标",
        })
    headline = datasets["headline"][0]
    headline.update({
        "latest_market_universe_symbols": universe,
        "asset_class_row_coverage_rate": row_rate,
        "asset_class_official_compatibility_rate": compatibility_rate,
        "asset_class_missing_symbols": len(missing),
        "asset_class_mismatch_symbols": len(mismatches),
        "asset_class_unsupported_symbols": len(unsupported),
    })
    fast_rate = float(
        (datasets.get("fast_source_health") or [{}])[0].get(
            "usable_rate", 1.0))
    per_symbol_min = min(
        float(row["coverage_rate"]) for row in datasets["coverage"])
    headline["per_symbol_minimum_coverage_rate"] = per_symbol_min
    headline["minimum_coverage_rate"] = min(per_symbol_min, fast_rate)

    table = _upsert_id(manifest["tables"], "asset_class_coverage_table")
    table.update({
        "title": "资产分类完整性与官方语义一致性",
        "subtitle": (
            f"OKX当前{universe}个线性USDT永续；有行与语义兼容分开验收。"),
        "dataset": "asset_class_coverage",
        "sourceId": "asset_class_coverage",
        "columns": [
            {"field": "metric", "label": "指标", "type": "text"},
            {"field": "valid_symbols", "label": "有效币", "format": "number"},
            {"field": "universe_symbols", "label": "宇宙", "format": "number"},
            {"field": "coverage_rate", "label": "覆盖率", "format": "percent"},
            {"field": "missing_or_mismatch_count", "label": "缺口", "format": "number"},
            {"field": "status", "label": "99%验收", "type": "text"},
        ],
        "defaultSort": {"field": "metric", "direction": "asc"},
    })
    block = {
        "id": "asset_class_coverage_block",
        "type": "table",
        "tableId": "asset_class_coverage_table",
        "layout": "full",
    }
    blocks = manifest["blocks"]
    existing = [
        item for item in blocks
        if item.get("id") == "asset_class_coverage_block"]
    if existing:
        existing[0].clear()
        existing[0].update(block)
    else:
        insert_at = next(
            (index + 1 for index, item in enumerate(blocks)
             if item.get("id") == "coverage_block"),
            len(blocks),
        )
        blocks.insert(insert_at, block)

    coverage_source = _upsert_id(manifest["sources"], "coverage_evidence")
    value_rows = []
    for row in datasets["coverage"]:
        family = str(row["data_family"]).replace("'", "''")
        status = str(row.get("status", "")).replace("'", "''")
        value_rows.append(
            f"    ('{family}',{int(row['valid_symbols'])},"
            f"{int(row['universe'])},{float(row['coverage_rate']):.12f},"
            f"'{status}')"
        )
    query = dict(coverage_source.get("query") or {})
    query.update({
        "sql": (
            "WITH coverage(data_family,valid_symbols,universe,coverage_rate,"
            "status) AS (\n  VALUES\n" + ",\n".join(value_rows)
            + "\n)\nSELECT * FROM coverage"
        ),
        "executed_at": generated_at,
    })
    tables_used = list(query.get("tables_used") or [])
    for item in (
        "market.db.instrument_class", audit_relative_path,
    ):
        if item not in tables_used:
            tables_used.append(item)
    query["tables_used"] = tables_used
    coverage_source["query"] = query

    chart = _one(manifest["charts"], "id", "coverage_chart")
    base_subtitle = re.sub(
        r"；资产分类有行率.*$", "", str(chart.get("subtitle") or ""))
    chart["subtitle"] = (
        base_subtitle
        + f"；资产分类有行率{row_rate:.3%}、官方兼容率"
        f"{compatibility_rate:.3%}。"
    )
    gate = _one(datasets["gates"], "goal", "关键数据完善率")
    base_current = re.sub(
        r"；资产分类有行率.*$", "", str(gate.get("current") or ""))
    gate["current"] = (
        base_current
        + f"；资产分类有行率{row_rate:.3%}、官方兼容率"
        f"{compatibility_rate:.3%}"
    )
    gate["status"] = "未达标"
    data_block = _one(manifest["blocks"], "id", "data_section")
    body_without_asset = re.sub(
        r"\n\n资产分类审计：.*$", "", str(data_block.get("body") or ""),
        flags=re.S,
    )
    data_block["body"] = (
        body_without_asset
        + "\n\n资产分类审计：本地有行"
        f"{local_rows}/{universe}={row_rate:.3%}，OKX官方instCategory兼容率"
        f"{compatible}/{universe}={compatibility_rate:.3%}；缺行"
        f"{len(missing)}、语义冲突{len(mismatches)}、未知官方类别"
        f"{len(unsupported)}。"
    )
    _advance_report_generated_at(artifact, generated_at)
    _sync_title_block(manifest)
    return artifact


def refresh_contract_statistics_coverage(
    artifact: dict,
    natural_audit: dict,
    isolated_audit: dict,
    *,
    natural_relative_path: str,
    isolated_relative_path: str,
) -> dict:
    """Add official 15m OI/taker statistics and natural-cycle evidence."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if natural_audit.get("artifact_type") != (
        "contract_statistics_coverage_audit"
    ):
        raise ValueError("unexpected contract-statistics natural audit")
    if isolated_audit.get("artifact_type") != (
        "contract_statistics_isolated_acceptance"
    ):
        raise ValueError("unexpected contract-statistics isolated audit")
    source = "okx_rest_contract_oi_taker_15m"
    if natural_audit.get("source") != source or isolated_audit.get("source") != source:
        raise ValueError("unexpected contract-statistics source")
    for label, evidence in (
        ("natural", natural_audit), ("isolated", isolated_audit),
    ):
        if (
            int(evidence.get("production_database_writes", -1)) != 0
            or int(evidence.get("orders_placed", -1)) != 0
        ):
            raise ValueError(
                f"{label} contract-statistics evidence is not read-only")
    isolated_result = isolated_audit["audit"]
    natural_split = _validated_contract_coverage_split(
        natural_audit, label="natural")
    isolated_split = _validated_contract_coverage_split(
        isolated_result, label="isolated")
    _require_contract_quarter_cycle(
        natural_audit["latest_cycle_id"], label="natural")
    _require_contract_quarter_cycle(
        isolated_audit["cycle_id"], label="isolated")
    natural_rate = float(natural_split["coverage"])
    isolated_rate = float(isolated_split["coverage"])
    natural_direct_rate = float(natural_split["direct_rate"])
    natural_carry_rate = float(natural_split["carry_rate"])
    target = float(natural_audit["minimum_coverage"])
    if (
        not math.isfinite(target) or not 0 < target <= 1
        or not 0 <= natural_rate <= 1 or not 0 <= isolated_rate <= 1
    ):
        raise ValueError("invalid contract-statistics coverage rate")
    natural_availability_checks = natural_audit.get("availability_checks")
    natural_analysis_checks = natural_audit.get("analysis_ready_checks")
    natural_recent_batches = natural_audit.get("recent_batches")
    natural_duplicate_symbols = natural_audit.get("duplicate_symbols")
    natural_extra_symbols = natural_audit.get("extra_symbols")
    if (
        not isinstance(natural_recent_batches, list)
        or not isinstance(natural_duplicate_symbols, list)
        or not isinstance(natural_extra_symbols, list)
    ):
        raise ValueError(
            "natural contract-statistics structural evidence missing")
    latest_batch_rows = [
        row for row in natural_recent_batches
        if isinstance(row, dict)
        and str(row.get("cycle_id")) == str(natural_audit["latest_cycle_id"])
    ]
    if len(latest_batch_rows) != 1:
        raise ValueError(
            "natural contract-statistics latest batch evidence missing")
    latest_batch = latest_batch_rows[0]
    required_availability_checks = {
        "coverage_at_least_target": natural_rate >= target,
        "single_collected_timestamp": bool(
            latest_batch.get("single_collected_timestamp")),
        "no_duplicates": not natural_duplicate_symbols,
        "no_extra_symbols": not natural_extra_symbols,
        "universe_nonempty": int(natural_audit["universe_symbols"]) > 0,
    }
    required_analysis_checks = {
        "direct_coverage_at_least_target": natural_direct_rate >= target,
        "single_collected_timestamp": required_availability_checks[
            "single_collected_timestamp"],
        "no_duplicates": required_availability_checks["no_duplicates"],
        "no_extra_symbols": required_availability_checks["no_extra_symbols"],
        "universe_nonempty": required_availability_checks["universe_nonempty"],
    }
    if (
        not isinstance(natural_availability_checks, dict)
        or not isinstance(natural_analysis_checks, dict)
        or {
            key: bool(natural_availability_checks.get(key))
            for key in required_availability_checks
        } != required_availability_checks
        or {
            key: bool(natural_analysis_checks.get(key))
            for key in required_analysis_checks
        } != required_analysis_checks
        or str(natural_audit.get("availability_status"))
        != ("PASSED" if all(required_availability_checks.values()) else "NOT_MET")
        or str(natural_audit.get("analysis_ready_status"))
        != ("PASSED" if all(required_analysis_checks.values()) else "NOT_MET")
        or str(natural_audit.get("status"))
        != str(natural_audit.get("analysis_ready_status"))
    ):
        raise ValueError(
            "natural contract-statistics status disagrees with checks")
    isolated_checks = isolated_result.get("checks")
    required_isolated_checks = {
        "coverage_at_least_99pct": isolated_rate >= 0.99,
        "no_extra_symbols": True,
        "no_duplicate_symbols": True,
        "valid_values_and_times": True,
        "single_collected_timestamp": True,
        "sqlite_quick_check": True,
    }
    if (
        not isinstance(isolated_checks, dict)
        or {
            key: bool(isolated_checks.get(key))
            for key in required_isolated_checks
        } != required_isolated_checks
        or str(isolated_result.get("status"))
        != ("PASSED" if all(required_isolated_checks.values()) else "NOT_MET")
    ):
        raise ValueError(
            "isolated contract-statistics status disagrees with checks")
    forward = natural_audit.get("forward_after_remediation")
    if not isinstance(forward, dict):
        raise ValueError("contract-statistics forward window missing")
    forward_required = (
        "start_cst", "end_exclusive_cst", "expected_slots",
        "observed_slots", "missing_slots", "passed_slots", "failed_slots",
        "slot_pass_rate", "analysis_ready_slots",
        "analysis_not_ready_slots", "analysis_ready_slot_pass_rate",
        "expected_symbol_rows", "valid_symbol_rows",
        "availability_coverage_rate", "direct_valid_symbol_rows",
        "direct_coverage_rate", "carried_forward_valid_symbol_rows",
        "carry_forward_rate", "target_rate", "minimum_slots", "status",
        "analysis_ready_status", "missing_slot_semantics", "slots",
    )
    missing_forward = [field for field in forward_required if field not in forward]
    if missing_forward:
        raise ValueError(
            f"contract-statistics forward fields missing: {missing_forward}")
    forward_expected = int(forward["expected_slots"])
    forward_observed = int(forward["observed_slots"])
    forward_missing = int(forward["missing_slots"])
    forward_passed = int(forward["passed_slots"])
    forward_failed = int(forward["failed_slots"])
    forward_analysis_ready_slots = int(forward["analysis_ready_slots"])
    forward_analysis_not_ready_slots = int(
        forward["analysis_not_ready_slots"])
    forward_minimum = int(forward["minimum_slots"])
    forward_expected_rows = int(forward["expected_symbol_rows"])
    forward_valid_rows = int(forward["valid_symbol_rows"])
    forward_direct_rows = int(forward["direct_valid_symbol_rows"])
    forward_carry_rows = int(forward["carried_forward_valid_symbol_rows"])
    forward_availability_rate = float(forward["availability_coverage_rate"])
    forward_direct_rate = float(forward["direct_coverage_rate"])
    forward_carry_rate = float(forward["carry_forward_rate"])
    forward_slot_pass_rate = float(forward["slot_pass_rate"])
    forward_analysis_slot_pass_rate = float(
        forward["analysis_ready_slot_pass_rate"])
    forward_target = float(forward["target_rate"])
    forward_slots = forward["slots"]
    forward_start = _parse_source_cst(str(forward["start_cst"]))
    forward_end = _parse_source_cst(str(forward["end_exclusive_cst"]))
    if (
        forward_expected < 0 or forward_observed < 0 or forward_missing < 0
        or forward_minimum < 1 or forward_expected_rows < 0
        or min(forward_valid_rows, forward_direct_rows, forward_carry_rows) < 0
        or forward_observed + forward_missing != forward_expected
        or forward_passed + forward_failed != forward_expected
        or not 0 <= forward_analysis_ready_slots <= forward_expected
        or forward_analysis_ready_slots + forward_analysis_not_ready_slots
        != forward_expected
        or forward_direct_rows + forward_carry_rows != forward_valid_rows
        or forward_valid_rows > forward_expected_rows
        or not isinstance(forward_slots, list)
        or len(forward_slots) != forward_expected
        or not math.isfinite(forward_target)
        or not math.isclose(
            forward_target, target, rel_tol=1e-12, abs_tol=1e-12)
        or forward_start.second != 0 or forward_start.microsecond != 0
        or forward_start.minute % 15 != 0
        or forward_end.second != 0 or forward_end.microsecond != 0
        or forward_end.minute % 15 != 0
        or forward_end < forward_start
        or forward_end - forward_start
        != timedelta(minutes=15 * forward_expected)
        or str(forward["missing_slot_semantics"])
        != "unavailable_and_in_denominator"
    ):
        raise ValueError("contract-statistics forward counts invalid")
    expected_slot_rates = (
        forward_passed / forward_expected if forward_expected else 0.0,
        forward_analysis_ready_slots / forward_expected
        if forward_expected else 0.0,
    )
    for observed, expected in zip(
        (forward_slot_pass_rate, forward_analysis_slot_pass_rate),
        expected_slot_rates,
    ):
        if not math.isfinite(observed) or not math.isclose(
            observed, expected, rel_tol=1e-12, abs_tol=1e-12,
        ):
            raise ValueError(
                "contract-statistics forward slot rates disagree")
    if forward_expected_rows:
        expected_forward_rates = (
            forward_valid_rows / forward_expected_rows,
            forward_direct_rows / forward_expected_rows,
            forward_carry_rows / forward_expected_rows,
        )
        for observed, expected in zip(
            (forward_availability_rate, forward_direct_rate, forward_carry_rate),
            expected_forward_rates,
        ):
            if not math.isfinite(observed) or not math.isclose(
                observed, expected, rel_tol=1e-12, abs_tol=1e-12,
            ):
                raise ValueError("contract-statistics forward rates disagree")
    elif any(
        not math.isfinite(rate) or not math.isclose(
            rate, 0.0, rel_tol=0.0, abs_tol=1e-12)
        for rate in (
            forward_availability_rate, forward_direct_rate,
            forward_carry_rate,
        )
    ):
        raise ValueError("contract-statistics empty forward rates invalid")

    slot_totals = {
        "observed": 0,
        "availability_passed": 0,
        "analysis_ready": 0,
        "universe": 0,
        "valid": 0,
        "direct": 0,
        "carried": 0,
    }
    slot_required = (
        "cycle_id", "universe_symbols", "batch_rows", "valid_symbols",
        "availability_coverage_rate", "direct_valid_symbols",
        "direct_coverage_rate", "carried_forward_valid_symbols",
        "carry_forward_rate", "duplicate_symbols", "extra_symbols",
        "single_collected_timestamp", "status", "analysis_ready_status",
    )
    for index, slot in enumerate(forward_slots):
        if not isinstance(slot, dict):
            raise ValueError("contract-statistics forward slot invalid")
        missing_slot_fields = [
            field for field in slot_required if field not in slot]
        if missing_slot_fields:
            raise ValueError(
                "contract-statistics forward slot fields missing: "
                f"{missing_slot_fields}")
        expected_cycle = (
            forward_start + timedelta(minutes=15 * index)
        ).strftime("%Y-%m-%dT%H:%M")
        if str(slot["cycle_id"]) != expected_cycle:
            raise ValueError(
                "contract-statistics forward slots are not contiguous")
        slot_universe = int(slot["universe_symbols"])
        slot_batch = int(slot["batch_rows"])
        slot_valid = int(slot["valid_symbols"])
        slot_direct = int(slot["direct_valid_symbols"])
        slot_carried = int(slot["carried_forward_valid_symbols"])
        slot_duplicates = int(slot["duplicate_symbols"])
        slot_extras = int(slot["extra_symbols"])
        slot_single_collected = bool(slot["single_collected_timestamp"])
        if (
            slot_universe <= 0 or min(
                slot_batch, slot_valid, slot_direct, slot_carried,
                slot_duplicates, slot_extras,
            ) < 0
            or slot_direct + slot_carried != slot_valid
            or slot_valid > slot_universe
        ):
            raise ValueError(
                "contract-statistics forward slot counts invalid")
        slot_rates = (
            float(slot["availability_coverage_rate"]),
            float(slot["direct_coverage_rate"]),
            float(slot["carry_forward_rate"]),
        )
        for observed, expected in zip(
            slot_rates,
            (
                slot_valid / slot_universe,
                slot_direct / slot_universe,
                slot_carried / slot_universe,
            ),
        ):
            if not math.isfinite(observed) or not math.isclose(
                observed, expected, rel_tol=1e-12, abs_tol=1e-12,
            ):
                raise ValueError(
                    "contract-statistics forward slot rates disagree")
        slot_status = str(slot["status"])
        slot_analysis_status = str(slot["analysis_ready_status"])
        expected_slot_status = (
            "PASSED" if (
                slot_rates[0] >= target and slot_single_collected
                and slot_duplicates == 0 and slot_extras == 0
            ) else "NOT_MET"
        )
        expected_slot_analysis_status = (
            "PASSED" if (
                slot_rates[1] >= target and slot_single_collected
                and slot_duplicates == 0 and slot_extras == 0
            ) else "NOT_MET"
        )
        if (
            slot_status != expected_slot_status
            or slot_analysis_status != expected_slot_analysis_status
        ):
            raise ValueError(
                "contract-statistics forward slot status disagrees with evidence")
        slot_totals["observed"] += int(slot_batch > 0)
        slot_totals["availability_passed"] += int(slot_status == "PASSED")
        slot_totals["analysis_ready"] += int(
            slot_analysis_status == "PASSED")
        slot_totals["universe"] += slot_universe
        slot_totals["valid"] += slot_valid
        slot_totals["direct"] += slot_direct
        slot_totals["carried"] += slot_carried
    if slot_totals != {
        "observed": forward_observed,
        "availability_passed": forward_passed,
        "analysis_ready": forward_analysis_ready_slots,
        "universe": forward_expected_rows,
        "valid": forward_valid_rows,
        "direct": forward_direct_rows,
        "carried": forward_carry_rows,
    }:
        raise ValueError(
            "contract-statistics forward slots disagree with aggregate")
    expected_forward_availability_status = (
        "INSUFFICIENT_EVIDENCE" if forward_expected < forward_minimum else
        "PASSED" if (
            forward_slot_pass_rate >= target
            and forward_availability_rate >= target
        ) else "NOT_MET"
    )
    expected_forward_analysis_status = (
        "INSUFFICIENT_EVIDENCE" if forward_expected < forward_minimum else
        "PASSED" if (
            forward_analysis_slot_pass_rate >= target
            and forward_direct_rate >= target
        ) else "NOT_MET"
    )
    if (
        str(forward["status"]) != expected_forward_availability_status
        or str(forward["analysis_ready_status"])
        != expected_forward_analysis_status
    ):
        raise ValueError(
            "contract-statistics forward status disagrees with evidence")
    expected_overall_status = (
        "NOT_MET"
        if str(natural_audit["analysis_ready_status"]) != "PASSED" else
        "PENDING_FORWARD_EVIDENCE"
        if expected_forward_analysis_status == "INSUFFICIENT_EVIDENCE" else
        expected_forward_analysis_status
    )
    if str(natural_audit.get("overall_status")) != expected_overall_status:
        raise ValueError(
            "contract-statistics overall status disagrees with evidence")
    latest_direct_passed = (
        natural_direct_rate >= target
        and natural_audit.get("analysis_ready_status", natural_audit.get("status"))
        == "PASSED"
    )
    forward_gate_passed = (
        forward_expected >= forward_minimum
        and forward["analysis_ready_status"] == "PASSED"
        and forward_analysis_slot_pass_rate >= target
        and forward_direct_rate >= target
        and forward["status"] == "PASSED"
        and forward_availability_rate >= target
    )
    contract_gate_passed = latest_direct_passed and forward_gate_passed

    manifest = artifact["manifest"]
    generated_at = _parse_source_cst(
        natural_audit["generated_at_utc"]
    ).astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z")
    _advance_report_generated_at(artifact, generated_at)
    sources = manifest["sources"]
    natural_source = _upsert_id(sources, "contract_statistics_coverage")
    natural_source.update({
        "label": "官方15m合约OI与主动买卖量自然生产审计",
        "path": natural_relative_path,
        "query": {
            "engine": "SQLite read-only exact-cycle audit",
            "language": "python/sql",
            "sql": (
                "SELECT * FROM market_contract_statistics WHERE cycle_id="
                f"'{natural_audit['latest_cycle_id']}' AND source='{source}' "
                "ORDER BY symbol"
            ),
            "description": (
                "以最新USDT线性SWAP ticker宇宙为分母，对最新自然周期逐币"
                "校验缺失、重复、非负值、主动买入占比代数和来源时滞；"
                "把本轮直采、90分钟内一跳续用与严格可用率分开，续用保持"
                "原始源时间并禁止进入模型；同时保留最近批次观测历史。"
            ),
            "executed_at": natural_audit["generated_at_utc"],
            "tables_used": [
                "market.db.tick_snapshots",
                "market.db.market_contract_statistics",
                natural_relative_path,
            ],
            "filters": [
                "最新USDT线性SWAP ticker宇宙",
                "timeframe=15m",
                f"source={source}",
                f"最大来源时滞={natural_audit['maximum_source_lag_seconds']}秒",
            ],
            "metric_definitions": [
                "严格可用率=(本轮直采+合规一跳续用)/最新ticker宇宙",
                "本轮直采率=官方共同桶或官方逐笔严格对账行/最新ticker宇宙",
                "续用率=90分钟内、可验证直采原点的续用行/最新ticker宇宙",
                "主动买入占比=主动买入USD/(主动买入USD+主动卖出USD)",
                "历史批次仅保存观测覆盖，不冒充最新批次完整合法性复验",
            ],
        },
    })
    isolated_source = _upsert_id(sources, "contract_statistics_isolated")
    isolated_source.update({
        "label": "官方15m合约统计隔离全量网络验收",
        "path": isolated_relative_path,
        "query": {
            "engine": "Isolated SQLite network acceptance",
            "language": "python/sql",
            "description": (
                "在合法15分钟边界的隔离数据库中验证全宇宙请求、解析、"
                "代数、一跳续用和写入合同；直采与续用分栏，不写生产库且"
                "不能替代自然调度。"
            ),
            "executed_at": isolated_audit["generated_at_utc"],
            "tables_used": [isolated_relative_path],
            "filters": ["隔离数据库", "orders_placed=0"],
        },
    })
    official = _upsert_id(sources, "official_okx")
    official.update({
        "label": "OKX V5 API 官方文档",
        "href": "https://www.okx.com/docs-v5/en/",
    })
    official_query = official.setdefault("query", {})
    official_query.update({
        "description": (
            "官方公共市场接口与Trading Statistics合约账户多空比、"
            "持仓量历史和合约主动买卖量说明。"
        ),
        "url": "https://www.okx.com/docs-v5/en/",
        "tables_used": [
            "OKX V5 public market endpoints",
            "GET /api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
            "GET /api/v5/rubik/stat/contracts/open-interest-history",
            "GET /api/v5/rubik/stat/taker-volume-contract?unit=2",
        ],
    })

    datasets = artifact["snapshot"]["datasets"]
    coverage_row = _upsert_key(
        datasets["coverage"], "data_family", "official_contract_stats_15m")
    coverage_row.update({
        "valid_symbols": int(natural_split["direct"]),
        "universe": int(natural_audit["universe_symbols"]),
        "coverage_rate": natural_direct_rate,
        "availability_valid_symbols": int(natural_audit["valid_symbols"]),
        "availability_coverage_rate": natural_rate,
        "direct_valid_symbols": int(natural_split["direct"]),
        "direct_coverage_rate": natural_direct_rate,
        "carried_forward_valid_symbols": int(natural_split["carried"]),
        "carry_forward_rate": natural_carry_rate,
        "forward_direct_coverage_rate": forward_direct_rate,
        "forward_availability_coverage_rate": forward_availability_rate,
        "forward_expected_slots": forward_expected,
        "forward_minimum_slots": forward_minimum,
        "target_rate": target,
        "gap_to_target_pp": round((natural_direct_rate - target) * 100, 3),
        "status": "达标" if contract_gate_passed else "未达标",
    })
    history = sorted(
        natural_audit.get("recent_batches", []),
        key=lambda row: str(row["cycle_id"]),
    )
    contract_rows = [{
        "evidence": "隔离全量网络验收",
        "cycle": isolated_audit["cycle_id"],
        "valid_symbols": int(isolated_result["valid_symbols"]),
        "universe_symbols": int(isolated_result["universe_symbols"]),
        "coverage_rate": isolated_rate,
        "direct_valid_symbols": int(isolated_split["direct"]),
        "direct_coverage_rate": float(isolated_split["direct_rate"]),
        "carried_forward_valid_symbols": int(isolated_split["carried"]),
        "carry_forward_rate": float(isolated_split["carry_rate"]),
        "missing_count": len(isolated_result["missing_symbols"]),
        "evidence_class": "隔离严格验收",
        "status": isolated_result["status"],
    }, {
        "evidence": "修复后严格计划槽",
        "cycle": (
            f"{str(forward['start_cst'])[5:16]}~"
            f"{str(forward['end_exclusive_cst'])[5:16]}"
        ),
        "valid_symbols": forward_valid_rows,
        "universe_symbols": forward_expected_rows,
        "coverage_rate": forward_availability_rate,
        "direct_valid_symbols": forward_direct_rows,
        "direct_coverage_rate": forward_direct_rate,
        "carried_forward_valid_symbols": forward_carry_rows,
        "carry_forward_rate": forward_carry_rate,
        "missing_count": forward_missing,
        "evidence_class": (
            f"计划槽{forward_expected}/{forward_minimum}；缺槽计失败"
        ),
        "status": str(forward["analysis_ready_status"]),
    }]
    for row in history:
        latest = row["cycle_id"] == natural_audit["latest_cycle_id"]
        contract_rows.append({
            "evidence": (
                f"{str(row['cycle_id'])[-5:]}自然生产"
                + ("（串行新架构）" if latest else "（旧并发架构）")
            ),
            "cycle": row["cycle_id"],
            "valid_symbols": (
                int(natural_audit["valid_symbols"])
                if latest else int(row["observed_universe_symbols"])
            ),
            "universe_symbols": int(natural_audit["universe_symbols"]),
            "coverage_rate": (
                natural_rate if latest else float(row["observed_coverage_rate"])
            ),
            "direct_valid_symbols": (
                int(natural_split["direct"]) if latest else None),
            "direct_coverage_rate": (
                natural_direct_rate if latest else None),
            "carried_forward_valid_symbols": (
                int(natural_split["carried"]) if latest else None),
            "carry_forward_rate": (
                natural_carry_rate if latest else None),
            "missing_count": len(row["missing_symbols"]),
            "evidence_class": (
                "最新批次完整校验" if latest else "历史覆盖观测"
            ),
            "status": (
                natural_audit["status"] if latest else
                ("OBSERVED_PASS" if float(row["observed_coverage_rate"]) >= target
                else "OBSERVED_NOT_MET")
            ),
        })
    latest_cycle_label = str(natural_audit["latest_cycle_id"]).split("T")[-1]
    latest_passed = latest_direct_passed
    datasets["contract_statistics_coverage"] = contract_rows
    headline = datasets["headline"][0]
    headline.update({
        "contract_statistics_coverage_rate": natural_direct_rate,
        "contract_statistics_availability_coverage_rate": natural_rate,
        "contract_statistics_direct_coverage_rate": natural_direct_rate,
        "contract_statistics_carry_forward_rate": natural_carry_rate,
        "contract_statistics_valid_symbols": int(natural_split["direct"]),
        "contract_statistics_availability_valid_symbols": int(
            natural_audit["valid_symbols"]),
        "contract_statistics_universe_symbols": int(
            natural_audit["universe_symbols"]),
        "contract_statistics_missing_symbols": len(
            natural_audit["missing_symbols"]),
        "contract_statistics_source_lag_max_seconds": float(
            natural_audit["source_lag_seconds"]["max"]),
        "contract_statistics_forward_direct_coverage_rate": forward_direct_rate,
        "contract_statistics_forward_availability_coverage_rate": (
            forward_availability_rate),
        "contract_statistics_forward_expected_slots": forward_expected,
        "contract_statistics_forward_minimum_slots": forward_minimum,
        "contract_statistics_forward_status": forward["analysis_ready_status"],
    })
    per_symbol_min = min(
        float(row["coverage_rate"]) for row in datasets["coverage"])
    fast_rolling_rate = float(
        (datasets.get("fast_source_health") or [{}])[0].get(
            "usable_rate", 1.0))
    headline["per_symbol_minimum_coverage_rate"] = per_symbol_min
    headline["minimum_coverage_rate"] = min(
        per_symbol_min, fast_rolling_rate)

    table = _upsert_id(manifest["tables"], "contract_statistics_coverage_table")
    table.update({
        "title": "官方15m合约OI与主动买卖量：隔离及自然批次",
        "subtitle": (
            f"{latest_cycle_label}最新自然批次按来源时效和数值合同完整校验；"
            f"计划槽前向{forward_expected}/{forward_minimum}，缺槽计失败；"
            "一跳续用只算运行可用性，不算模型可用直采。"
        ),
        "dataset": "contract_statistics_coverage",
        "sourceId": "contract_statistics_coverage",
        "density": "dense",
        "columns": [
            {"field": "evidence", "label": "证据类型", "type": "text"},
            {"field": "cycle", "label": "周期", "type": "text"},
            {"field": "valid_symbols", "label": "有效/观测币", "format": "number"},
            {"field": "universe_symbols", "label": "宇宙", "format": "number"},
            {"field": "coverage_rate", "label": "运行可用率", "format": "percent"},
            {"field": "direct_coverage_rate", "label": "本轮直采率", "format": "percent"},
            {"field": "carry_forward_rate", "label": "续用率", "format": "percent"},
            {"field": "missing_count", "label": "缺失币数", "format": "number"},
            {"field": "evidence_class", "label": "证据级别", "type": "text"},
            {"field": "status", "label": "99%状态", "type": "text"},
        ],
        "defaultSort": {"field": "cycle", "direction": "asc"},
        "layout": "full",
    })
    blocks = manifest["blocks"]
    contract_block = {
        "id": "contract_statistics_coverage_block",
        "type": "table",
        "tableId": "contract_statistics_coverage_table",
        "layout": "full",
    }
    existing = [
        block for block in blocks
        if block.get("id") == "contract_statistics_coverage_block"
    ]
    if existing:
        existing[0].clear()
        existing[0].update(contract_block)
    else:
        insert_at = next(
            (index + 1 for index, block in enumerate(blocks)
             if block.get("id") == "positioning_coverage_block"),
            next(
                (index + 1 for index, block in enumerate(blocks)
                 if block.get("id") == "coverage_block"),
                len(blocks),
            ),
        )
        blocks.insert(insert_at, contract_block)

    coverage_source = _upsert_id(sources, "coverage_evidence")
    value_rows = []
    for row in datasets["coverage"]:
        family = str(row["data_family"]).replace("'", "''")
        status = str(row.get("status", "")).replace("'", "''")
        value_rows.append(
            f"    ('{family}',{int(row['valid_symbols'])},{int(row['universe'])},"
            f"{float(row['coverage_rate']):.12f},'{status}')"
        )
    coverage_source.update({
        "label": "关键市场数据族组合覆盖证据",
        "path": natural_relative_path,
        "query": {
            "engine": "Referenced SQLite/JSON evidence",
            "language": "sql/python/json",
            "sql": (
                "WITH coverage(data_family,valid_symbols,universe,coverage_rate,"
                "status) AS (\n  VALUES\n" + ",\n".join(value_rows) +
                "\n)\nSELECT * FROM coverage"
            ),
            "description": (
                "各关键数据族以自己的严格分母独立验收；组合图不做加权平均。"
            ),
            "executed_at": natural_audit["generated_at_utc"],
            "tables_used": [
                "market.db.tick_snapshots",
                "market.db.kline_cache",
                "market.db.market_positioning",
                "market.db.market_contract_statistics",
                natural_relative_path,
                isolated_relative_path,
            ],
            "filters": ["最新交易宇宙", "各数据族独立>=99%"],
            "metric_definitions": [
                "覆盖率=该数据族严格有效symbol/该周期最新交易宇宙"
            ],
        },
    })
    coverage_chart = _one(manifest["charts"], "id", "coverage_chart")
    coverage_chart.update({
        "subtitle": (
            "官方1H持仓倾向最新批次与最新15m合约OI/主动买卖量模型可用直采均已过"
            f"99%；后者计划槽前向直采{forward_direct_rate:.3%}、"
            f"{forward_expected}/{forward_minimum}槽，最新与长期门均已通过。"
            if contract_gate_passed else
            "官方1H持仓倾向最新批次与最新15m合约OI/主动买卖量模型可用直采均已过"
            f"99%；后者计划槽前向直采{forward_direct_rate:.3%}，仅"
            f"{forward_expected}/{forward_minimum}槽，仍未通过长期门。"
            if latest_passed else
            f"官方1H持仓倾向最新批次已过99%（双前向窗另验）；{latest_cycle_label}最新15m合约"
            f"OI/主动买卖量运行可用性{natural_rate:.3%}，但模型可用"
            f"直采仅{natural_direct_rate:.3%}，低于99%。"
        ),
        "sourceId": "coverage_evidence",
    })

    positioning = _one(
        datasets["coverage"], "data_family", "official_positioning_1H")
    positioning_headline = datasets["headline"][0]
    positioning_window_text = (
        f"，批次键{positioning_headline.get('positioning_storage_contract_status', 'NOT_MET')}，"
        f"小时前向{positioning_headline.get('positioning_hourly_forward_expected_slots', 0)}/"
        f"{positioning_headline.get('positioning_hourly_forward_minimum_slots', 24)}槽，"
        f"15分钟可用性{positioning_headline.get('positioning_availability_forward_expected_slots', 0)}/"
        f"{positioning_headline.get('positioning_availability_forward_minimum_slots', 96)}槽，"
        f"总体{positioning_headline.get('positioning_overall_status', 'NOT_MET')}"
    )
    coverage_4h = _one(datasets["coverage"], "data_family", "4H")
    fast = (datasets.get("fast_source_health") or [{}])[0]
    gate = _one(datasets["gates"], "goal", "关键数据完善率")
    gate.update({
        "current": (
            f"官方1H持仓倾向最新批次 {positioning['coverage_rate']:.3%}"
            f"（{positioning['valid_symbols']}/{positioning['universe']}）"
            f"{positioning_window_text}；"
            f"15m合约OI/主动买卖量模型可用直采 {natural_direct_rate:.3%}"
            f"（{natural_split['direct']}/{natural_split['universe']}）；"
            f"运行可用性 {natural_rate:.3%}、合规续用 "
            f"{natural_carry_rate:.3%}；"
            f"严格前向直采 {forward_direct_rate:.3%}、运行可用性 "
            f"{forward_availability_rate:.3%}（{forward_expected}/"
            f"{forward_minimum}槽）；"
            f"4H逐币 {coverage_4h['coverage_rate']:.3%}；fast 14日 "
            f"{float(fast.get('usable_rate', 0)):.3%}，修复后前向 "
            f"{fast.get('forward_expected_slots', 0)}/"
            f"{fast.get('forward_minimum_slots', 96)}槽"
        ),
        "status": "未达标",
        "next_gate": (
            "后续自然快照复核4H逐币覆盖；持仓倾向24整点与96决策槽均达99%；"
            "合约统计直采与计划槽均至少96槽；"
            "fast前向至少96槽且14日滚动窗均须>=99%"
        ),
    })
    first_failed = next(
        (row for row in contract_rows if row["status"] == "OBSERVED_NOT_MET"),
        None,
    )
    failed_text = (
        f"首次自然周期{first_failed['valid_symbols']}/"
        f"{first_failed['universe_symbols']}="
        f"{first_failed['coverage_rate']:.3%}，"
        if first_failed else ""
    )
    historical_full_cycles = [
        str(row["cycle_id"]).split("T")[-1]
        for row in history
        if row["cycle_id"] != natural_audit["latest_cycle_id"]
        and float(row["observed_coverage_rate"]) >= target
    ]
    historical_full_text = (
        "、".join(historical_full_cycles[-3:])
        + "曾有不低于99%的行覆盖观测，但历史观测不冒充当前严格复验；"
        if historical_full_cycles else ""
    )
    missing_count = len(natural_audit.get("missing_symbols", []))
    invalid_count = len(natural_audit.get("invalid_symbols", {}))
    contract_heading = (
        "## 最新15m合约直采与96槽严格前向窗均已通过99%"
        if contract_gate_passed else
        "## 最新15m合约直采过99%，但严格前向窗尚未完成"
        if latest_passed else
        "## 官方1H持仓倾向最新批次通过，双前向窗另验；最新15m合约模型可用直采低于99%"
    )
    data_block = _one(blocks, "id", "data_section")
    data_block["body"] = (
        contract_heading + "\n\n"
        "Ticker、合约元数据、15m及资金费/OI逐币覆盖均为100%；"
        f"官方1H账户多空比最新自然批次{positioning['valid_symbols']}/"
        f"{positioning['universe']}={positioning['coverage_rate']:.3%}"
        f"{positioning_window_text}。"
        "新增官方15m合约持仓量与主动买卖量不再与盘口并发争抢网络："
        f"{failed_text}改为基础快采后串行执行并保留一次有界失败重试，"
        f"{historical_full_text}{latest_cycle_label}最新自然周期严格有效"
        f"{natural_audit['valid_symbols']}/"
        f"{natural_audit['universe_symbols']}={natural_rate:.3%}，"
        f"其中本轮直采{natural_split['direct']}/"
        f"{natural_split['universe']}={natural_direct_rate:.3%}、"
        f"90分钟内一跳续用{natural_split['carried']}/"
        f"{natural_split['universe']}={natural_carry_rate:.3%}；"
        f"缺行{missing_count}币、来源超时效或数值无效{invalid_count}币，"
        f"最大来源时滞{natural_audit['source_lag_seconds']['max']:.0f}秒，"
        "续用保留原始源时间、引用可验证直采原点且不进入模型；没有人工补采"
        "或历史回填。隔离验收同样分开直采与续用，只证明网络和解析合同。"
        f"严格计划槽前向直采{forward_direct_rows}/{forward_expected_rows}="
        f"{forward_direct_rate:.3%}、运行可用性{forward_availability_rate:.3%}，"
        f"当前仅{forward_expected}/{forward_minimum}槽；4H逐币覆盖为"
        f"{coverage_4h['coverage_rate']:.3%}。合约续用仅保障运行连续性，"
        "不计入模型可用99%分子；"
        "fast 14日计划槽和96槽前向窗继续独立验收。"
    )
    _sync_title_block(manifest)
    return artifact


def refresh_runtime_evidence(
    artifact: dict,
    evaluation: dict,
    model_shadow: dict,
    *,
    evaluation_relative_path: str,
    model_relative_path: str,
) -> dict:
    """Refresh natural daily throughput and the first frozen forward cycle."""
    if artifact.get("surface") != "report":
        raise ValueError("artifact surface must be report")
    if evaluation.get("artifact_type") != (
        "full_universe_shadow_judgment_evaluation"
    ):
        raise ValueError("unexpected universe-shadow evaluation")
    if model_shadow.get("artifact_type") != "frozen_multitimeframe_model_shadow":
        raise ValueError("unexpected frozen-model shadow artifact")
    if bool(evaluation.get("production_mutation")) or int(
        evaluation.get("orders_placed", 0)
    ) != 0:
        raise ValueError("universe-shadow evaluation changed production")
    if (
        bool(model_shadow.get("production_mutation"))
        or bool(model_shadow.get("production_execution_authorized"))
        or bool(model_shadow.get("production_threshold_change_allowed"))
        or bool(model_shadow.get("confidence_claim_allowed"))
        or int(model_shadow.get("orders_placed", 0)) != 0
    ):
        raise ValueError("frozen-model shadow is not research-only")

    daily = evaluation.get("daily_throughput") or {}
    latest = daily.get("latest_day") or {}
    required_daily = (
        "date",
        "snapshots",
        "judgment_records",
        "minimum_records_target",
        "minimum_snapshots_target",
        "minimum_unique_symbols_target",
        "unique_symbols",
        "daily_target_met",
    )
    if any(field not in latest for field in required_daily):
        raise ValueError("latest daily throughput is incomplete")
    snapshots = int(latest["snapshots"])
    records = int(latest["judgment_records"])
    records_target = int(latest["minimum_records_target"])
    snapshots_target = int(latest["minimum_snapshots_target"])
    unique_symbols = int(latest["unique_symbols"])
    unique_target = int(latest["minimum_unique_symbols_target"])
    if (
        snapshots < 1
        or records < 1
        or records_target < 1
        or snapshots_target < 1
        or unique_symbols < 1
        or unique_target < 1
    ):
        raise ValueError("invalid daily throughput counts")
    if int(evaluation.get("snapshots_loaded", -1)) != snapshots:
        raise ValueError("snapshot count disagrees with latest daily throughput")
    expected_daily_checks = {
        "minimum_records_met": records >= records_target,
        "minimum_snapshots_met": snapshots >= snapshots_target,
        "minimum_unique_symbols_met": unique_symbols >= unique_target,
    }
    for field, expected in expected_daily_checks.items():
        if field in latest and bool(latest[field]) != expected:
            raise ValueError(f"daily throughput {field} disagrees with counts")
    expected_daily_target = all(expected_daily_checks.values())
    if bool(latest["daily_target_met"]) != expected_daily_target:
        raise ValueError("daily throughput target status disagrees with counts")

    model_metrics = model_shadow.get("metrics") or {}
    model_audit = model_shadow.get("data_audit") or {}
    enrichment = model_audit.get("enrichment") or {}
    frozen_clock = model_audit.get("frozen_feature_clock") or {}
    scored = int(model_metrics.get("scored_symbols", 0))
    selected = int(model_metrics.get("selected_signals", 0))
    side_counts = model_metrics.get("side_counts") or {}
    long_n = int(side_counts.get("long", 0))
    short_n = int(side_counts.get("short", 0))
    contract_rows = int(enrichment.get("contract_statistics_available_rows", 0))
    scoring_rows = int(model_audit.get("scoring_ready_rows", 0))
    if scored <= 0 or scored != scoring_rows or not 0 <= selected <= scored:
        raise ValueError("invalid frozen-model scored/selected counts")
    if long_n + short_n != selected:
        raise ValueError("frozen-model side counts disagree")
    if not bool(model_shadow.get("forward_evidence_eligible")):
        raise ValueError("model artifact is not eligible forward evidence")
    if model_shadow.get("status") != "ready_for_forward_shadow":
        raise ValueError("model artifact is not ready for forward shadow")

    generated_at = str(evaluation.get("generated_at_utc") or "")
    manifest = artifact["manifest"]
    _advance_report_generated_at(artifact, generated_at)

    runtime_source = _upsert_id(manifest["sources"], "runtime")
    runtime_source.update({
        "label": "全市场影子判断自然日累计评估",
        "path": evaluation_relative_path,
        "query": {
            "engine": "Deterministic JSON evaluation + SQLite labels",
            "language": "python/sql",
            "sql": (
                "WITH natural_day(business_date,snapshots_observed,"
                "judgment_records,unique_symbols,records_target,"
                "snapshot_target,daily_target_met) AS (VALUES (\n"
                f"  '{latest['date']}',{snapshots},{records},{unique_symbols},"
                f"{records_target},{snapshots_target},"
                f"{1 if bool(latest['daily_target_met']) else 0}\n"
                ")) SELECT * FROM natural_day"
            ),
            "description": (
                "只汇总00:00/08:00/16:00自然调度生成的全宇宙影子工件；"
                "不把手工重建、真实成交或未落盘调度计入判断吞吐。"
            ),
            "executed_at": generated_at,
            "tables_used": [
                "reports/quality/universe-shadow/2026-08-12",
                "market.db.tick_snapshots",
                evaluation_relative_path,
            ],
            "filters": [
                f"business_date={latest['date']}",
                "仅自然00:00/08:00/16:00周期",
                "每个工件必须通过结构与安全标志校验",
            ],
            "metric_definitions": [
                "判断量=自然全宇宙影子工件中的judgment_records之和",
                "日目标=历史14日显式信号均值的150%，向上取整为993条",
                "唯一交易对=当日全部自然工件records中的symbol并集",
            ],
        },
    })
    model_source = _upsert_id(manifest["sources"], "model_shadow_forward")
    model_source.update({
        "label": "冻结多周期模型首个独立未来影子周期",
        "path": model_relative_path,
        "query": {
            "engine": "Frozen JSON model artifact",
            "language": "python/json",
            "sql": (
                "WITH model_forward(cycle_id,scored_symbols,selected_signals,"
                "long_signals,short_signals,contract_feature_symbols,"
                "confidence_claim_allowed,production_authorized) AS (VALUES (\n"
                f"  '{model_shadow['cycle_id']}',{scored},{selected},{long_n},"
                f"{short_n},{contract_rows},0,0\n"
                ")) SELECT * FROM model_forward"
            ),
            "description": (
                "冻结参数只对点时数据完整且通过既有流动性门的交易对评分；"
                "新合约统计只保存为未来重训特征，不改变冻结概率或入场时钟。"
            ),
            "executed_at": str(model_shadow["generated_at_utc"]),
            "tables_used": [
                "market.db.tick_snapshots",
                "market.db.market_microstructure",
                "market.db.market_trade_flow",
                "market.db.market_positioning",
                "market.db.market_contract_statistics",
                model_relative_path,
            ],
            "filters": [
                f"cycle_id={model_shadow['cycle_id']}",
                "冻结参数SHA-256固定",
                "生产授权=false",
            ],
            "metric_definitions": [
                "scored_symbols=既有流动性门且冻结特征在10分钟点时窗内完整的币数",
                "selected_signals=冻结研究概率达到固定离线阈值的币数",
                "前向证据仅在冻结后自然周期计入；尚未成熟的标签不计精度",
            ],
        },
    })

    datasets = artifact["snapshot"]["datasets"]
    observed = _one(datasets["throughput"], "target_or_capacity", "observed")
    observed.update({
        "judgments_per_day": records,
        "universe": unique_symbols,
        "schedule_runs_per_day": snapshots,
        "acceptance_state": (
            "当日目标已由三个自然周期证明"
            if bool(latest["daily_target_met"])
            else f"当日进行中，已完成{snapshots}/{snapshots_target}自然周期"
        ),
    })
    datasets["model_shadow_forward"] = [{
        "cycle_id": str(model_shadow["cycle_id"]),
        "scored_symbols": scored,
        "selected_signals": selected,
        "long_signals": long_n,
        "short_signals": short_n,
        "contract_feature_symbols": contract_rows,
        "frozen_clock_delay_seconds": float(
            frozen_clock.get("maximum_decision_delay_seconds", 0)),
        "all_enrichment_delay_seconds": float(
            enrichment.get("maximum_ready_decision_delay_seconds", 0)),
        "forward_evidence_eligible": True,
        "confidence_claim_allowed": False,
        "production_execution_authorized": False,
        "status": str(model_shadow["status"]),
    }]
    headline = datasets["headline"][0]
    observed_design_capacity = unique_symbols * snapshots_target
    headline.update({
        "shadow_records_observed_today": records,
        "shadow_snapshots_observed_today": snapshots,
        "shadow_unique_symbols_today": unique_symbols,
        "shadow_daily_target_met": bool(latest["daily_target_met"]),
        "shadow_capacity_per_day": observed_design_capacity,
        "signals_plus_50_target": records_target,
        "model_forward_cycles": 1,
        "model_forward_scored_symbols": scored,
        "model_forward_selected_signals": selected,
        "model_forward_long_signals": long_n,
        "model_forward_short_signals": short_n,
        "model_forward_contract_feature_symbols": contract_rows,
        "model_forward_confidence_claim_allowed": False,
    })
    horizons = evaluation.get("horizons") or []
    natural_15m = next(
        (row for row in horizons if row.get("horizon") == "15m"), None)
    if natural_15m:
        headline.update({
            "natural_15m_precision": (
                float(natural_15m["after_cost_precision_pct"]) / 100),
            "natural_15m_n": int(natural_15m["n_labeled"]),
        })

    gate = _one(datasets["gates"], "goal", "300+币与判断量+50%")
    gate.update({
        "current": (
            f"当日自然调度 {snapshots}/{snapshots_target} 次、"
            f"{records}/{records_target} 条；唯一交易对"
            f"{unique_symbols}/{unique_target}"
        ),
        "status": "达标" if bool(latest["daily_target_met"]) else "待连续验收",
        "next_gate": (
            "维持自然日连续审计，不以手工工件或真实成交量代替"
            if bool(latest["daily_target_met"])
            else "等待16:00第三个自然周期；不以手工快照代替"
        ),
    })

    throughput_chart = _one(manifest["charts"], "id", "throughput_chart")
    throughput_chart["subtitle"] = (
        f"当日自然进度{snapshots}/{snapshots_target}、{records}/{records_target}条；"
        "设计容量不等于已验收结果。"
    )
    throughput_card = _one(manifest["cards"], "id", "throughput_card")
    throughput_card["description"] = (
        f"截至{latest['date']} {snapshots}个自然周期的可审计判断；"
        "真实成交不计入吞吐目标。"
    )
    blocks = manifest["blocks"]
    throughput_section = _one(blocks, "id", "throughput_section")
    throughput_heading = (
        f"## {unique_symbols}个交易对已完成{snapshots}/{snapshots_target}"
        f"自然周期，当日累计{records}条"
    )
    throughput_result = (
        f"已同时达到至少{unique_target}个交易对、{records_target}条和"
        f"{snapshots_target}次自然周期的当日+50%目标；后续按自然日连续观察"
        if bool(latest["daily_target_met"]) else
        f"已超过{unique_target}个交易对覆盖门，但尚未同时达到"
        f"{records_target}条和{snapshots_target}次自然周期的当日+50%目标"
    )
    throughput_section["body"] = (
        throughput_heading + "\n\n"
        f"截至当前，{latest['date']}共完成{snapshots}个自然调度周期，覆盖"
        f"{unique_symbols}个唯一交易对、写出{records}条逐币判断；"
        + throughput_result + "。"
        f"按当日唯一交易对和{snapshots_target}个计划周期折算容量为"
        f"{observed_design_capacity:,}条/日；"
        "判断量只表示可审计分析记录，不代表真实成交，也不代表90%可信度。"
    )

    model_section = _upsert_id(blocks, "model_forward_section")
    model_section.update({
        "type": "markdown",
        "sourceId": "model_shadow_forward",
        "body": (
            "## 首个冻结模型未来周期已落地，但尚无成熟可信度结论\n\n"
            f"{model_shadow['cycle_id']}自然周期对{scored}个既有流动性合格币"
            f"完成冻结评分，固定阈值选出{selected}个研究信号（多{long_n}、"
            f"空{short_n}）。官方合约统计特征在{contract_rows}/{scored}个"
            "评分币上可用，但不参与旧冻结特征时钟；全部记录仍为"
            "confidence_claim_allowed=false、production_execution_authorized=false。"
            "该周期标签尚未成熟，不能计算或声称90%精度。"
        ),
        "layout": "full",
    })
    model_table = _upsert_id(manifest["tables"], "model_shadow_forward_table")
    model_table.update({
        "title": "冻结模型首个未来影子周期",
        "subtitle": "只展示覆盖与安全状态；尚未成熟的标签不计算精度",
        "dataset": "model_shadow_forward",
        "sourceId": "model_shadow_forward",
        "columns": [
            {"field": "cycle_id", "label": "自然周期"},
            {"field": "scored_symbols", "label": "评分币", "format": "number"},
            {"field": "selected_signals", "label": "研究信号", "format": "number"},
            {"field": "long_signals", "label": "多", "format": "number"},
            {"field": "short_signals", "label": "空", "format": "number"},
            {"field": "contract_feature_symbols", "label": "合约特征可用币", "format": "number"},
            {"field": "confidence_claim_allowed", "label": "允许可信度声明"},
            {"field": "production_execution_authorized", "label": "生产授权"},
        ],
        "defaultSort": {"field": "cycle_id", "direction": "desc"},
    })
    model_block = _upsert_id(blocks, "model_shadow_forward_block")
    model_block.update({
        "type": "table",
        "tableId": "model_shadow_forward_table",
        "layout": "full",
    })
    for item in (model_section, model_block):
        blocks.remove(item)
    credibility_index = next(
        (index for index, item in enumerate(blocks)
         if item.get("id") == "credibility_section"),
        len(blocks),
    )
    blocks[credibility_index:credibility_index] = [model_section, model_block]

    _sync_title_block(manifest)
    return artifact


def refresh_overall_narrative(artifact: dict) -> dict:
    """Rebuild answer-first narrative from the already-refreshed datasets."""
    datasets = artifact["snapshot"]["datasets"]
    headline = datasets["headline"][0]
    fast = (datasets.get("fast_source_health") or [{}])[0]
    report_row = _one(
        datasets["report_quality"], "artifact_family", "日报历史校验")
    push_report_row = _one(
        datasets["report_quality"], "artifact_family", "Push 报告完整性")
    push_delivery_row = _one(
        datasets["report_quality"], "artifact_family", "Push 精确送达")
    report_gate_passed = bool(headline.get(
        "report_and_push_gate_passed", False))
    positioning = _one(
        datasets["coverage"], "data_family", "official_positioning_1H")
    contract = next(
        (row for row in datasets["coverage"]
         if row.get("data_family") == "official_contract_stats_15m"),
        None,
    )
    coverage_4h = _one(datasets["coverage"], "data_family", "4H")
    multitimeframe_rows = datasets.get("multitimeframe_coverage") or []
    multitimeframe_4h = next(
        (row for row in multitimeframe_rows
         if row.get("timeframe") == "4H"), None)
    observed_records = int(headline.get("shadow_records_observed_today", 0))
    observed_snapshots = int(headline.get("shadow_snapshots_observed_today", 0))
    observed_symbols = int(
        headline.get("shadow_unique_symbols_today", headline["universe"]))
    throughput_target = int(round(float(headline["signals_plus_50_target"])))
    throughput_met = bool(headline.get("shadow_daily_target_met", False))
    multitimeframe_ready = bool(
        multitimeframe_4h
        and float(multitimeframe_4h["analysis_ready_rate"]) >= 0.99
    )
    ranking_text = (
        f"，组内排序内部确认"
        f"{headline['ranking_confirmation_precision']:.3%}"
        f"（N={headline['ranking_confirmation_n']}）"
        if "ranking_confirmation_precision" in headline else ""
    )
    directional_text = (
        f"，方向间距内部确认"
        f"{headline['directional_confirmation_precision']:.3%}"
        f"（N={headline['directional_confirmation_n']}，Wilson下界"
        f"{headline['directional_confirmation_wilson_low']:.3%}）"
        if "directional_confirmation_precision" in headline else ""
    )
    news_rows = datasets.get("news_source_health") or []
    news_critical = [
        row for row in news_rows
        if row.get("role") in {"required", "official_required", "required_subsource"}
    ]
    news_complete = sum(int(row.get("complete_slots", 0)) for row in news_critical)
    news_expected = sum(int(row.get("expected_slots", 0)) for row in news_critical)
    news_text = (
        f"，新闻关键源严格前向{news_complete}/{news_expected}槽"
        if news_rows else ""
    )
    contract_rows = datasets.get("contract_statistics_coverage") or []
    natural_contract_rows = [
        row for row in contract_rows
        if str(row.get("evidence", "")).endswith("自然生产")
        or "自然生产（" in str(row.get("evidence", ""))
    ]
    contract_evidence = (
        max(natural_contract_rows, key=lambda row: str(row.get("cycle", "")))
        if natural_contract_rows else {}
    )
    positioning_evidence = (
        (datasets.get("positioning_coverage") or [{}, {}])[-1]
    )
    positioning_overall_status = str(
        headline.get("positioning_overall_status", "NOT_MET"))
    positioning_gate_passed = positioning_overall_status == "PASSED"
    positioning_window_text = (
        f"批次键{headline.get('positioning_storage_contract_status', 'NOT_MET')}，"
        f"小时前向{headline.get('positioning_hourly_forward_expected_slots', 0)}/"
        f"{headline.get('positioning_hourly_forward_minimum_slots', 24)}槽，"
        f"15分钟可用性{headline.get('positioning_availability_forward_expected_slots', 0)}/"
        f"{headline.get('positioning_availability_forward_minimum_slots', 96)}槽，"
        f"总体{positioning_overall_status}"
    )
    latest_contract_passed = bool(
        contract
        and float(contract["coverage_rate"])
        >= float(contract.get("target_rate", 0.99))
    )
    contract_gate_passed = bool(
        contract
        and str(contract.get("status")) == "达标"
    )
    lowest_coverage = min(
        datasets["coverage"], key=lambda row: float(row["coverage_rate"]))
    lowest_label = {
        "official_contract_stats_15m": "官方15m合约OI/主动买卖量",
        "official_positioning_1H": "官方1H账户多空比",
        "asset_class_row_presence": "资产分类有行率",
        "asset_class_official_compatibility": "资产分类官方兼容率",
    }.get(str(lowest_coverage["data_family"]), str(lowest_coverage["data_family"]))
    contract_text = (
        f"官方15m合约OI与主动买卖量"
        f"{contract_evidence.get('cycle', '最新')}自然周期"
        f"{int(contract.get('availability_valid_symbols', 0))}/"
        f"{contract['universe']}="
        f"{float(contract.get('availability_coverage_rate', 0)):.3%}运行可用，"
        f"本轮模型可用直采{contract['valid_symbols']}/{contract['universe']}="
        f"{float(contract.get('direct_coverage_rate', 0)):.3%}、"
        f"一跳续用"
        f"{float(contract.get('carry_forward_rate', 0)):.3%}，"
        f"严格前向直采{float(contract.get('forward_direct_coverage_rate', 0)):.3%}"
        f"（{int(contract.get('forward_expected_slots', 0))}/"
        f"{int(contract.get('forward_minimum_slots', 96))}槽），"
        + (
            "最新与96槽长期门均达到99%。"
            if contract_gate_passed else
            "最新直采达到99%，96槽长期门仍未完成。"
            if latest_contract_passed else
            "最新直采也尚未达到99%完整门槛。"
        )
        if contract else "官方15m合约OI与主动买卖量仍待自然验收。"
    )
    blocks = artifact["manifest"]["blocks"]
    executive = _one(blocks, "id", "executive_summary")
    executive["body"] = (
        "## 技术结论\n\n"
        "本轮已把15m/1H/4H判断固定为已收盘K线，并部署每日00:00、08:00、"
        "16:00三次、全交易宇宙逐币可审计的只读影子判断。当前已有"
        f"{observed_snapshots}/3个自然周期，覆盖{observed_symbols}个唯一"
        f"交易对、累计写出{observed_records}条；设计容量"
        f"{headline['shadow_capacity_per_day']:,}条/日，高于+50%目标"
        f"{headline['signals_plus_50_target']:.2f}条。官方REST账户多空比"
        f"{positioning_evidence.get('cycle', '最新')}自然批次"
        f"{positioning['valid_symbols']}/{positioning['universe']}="
        f"{positioning['coverage_rate']:.3%}，{positioning_window_text}；"
        f"{contract_text}"
        f"资产分类有行率{headline.get('asset_class_row_coverage_rate', 0):.3%}、"
        f"官方兼容率{headline.get('asset_class_official_compatibility_rate', 0):.3%}；"
        f"日报{report_row['numerator']}/{report_row['denominator']}通过；Push报告"
        f"{push_report_row['numerator']}/{push_report_row['denominator']}="
        f"{float(push_report_row['completeness_rate']):.3%}，精确送达"
        f"{push_delivery_row['numerator']}/{push_delivery_row['denominator']}="
        f"{float(push_delivery_row['completeness_rate']):.3%}，"
        + (
            "报告与推送总体门已通过。\n\n"
            if report_gate_passed else
            "报告与推送总体门未通过。\n\n"
        )
        +
        "可信度没有达标，也没有被包装成达标：实际生产LLM开多/开空信号"
        f"4H回顾精度为{headline['production_signal_precision']:.3%}"
        f"（N={headline['production_signal_n']}，Wilson下界"
        f"{headline['production_signal_wilson_low']:.3%}）；共享GBDT内部确认"
        f"{headline.get('shared_confirmation_precision', 0):.3%}"
        f"（N={headline.get('shared_confirmation_n', 0)}），分候选GBDT内部确认"
        f"{headline.get('independent_confirmation_precision', 0):.3%}"
        f"（N={headline.get('independent_confirmation_n', 0)}）{ranking_text}"
        f"{directional_text}，"
        "均远低于90%。"
        + (
            f"三周期精确已收盘原始OHLCV最低"
            f"{headline['multitimeframe_raw_minimum_rate']:.3%}，"
            f"4H指标就绪{multitimeframe_4h['analysis_ready_rate']:.3%}；"
            if multitimeframe_4h else ""
        )
        + f"当前最低逐币族为{lowest_label} "
        f"{float(lowest_coverage['coverage_rate']):.3%}，fast 14日"
        f"计划槽为{float(fast.get('usable_rate', 0)):.3%}，修复后前向仅"
        f"{fast.get('forward_expected_slots', 0)}/"
        f"{fast.get('forward_minimum_slots', 96)}槽{news_text}。"
        + (
            "当日全宇宙判断吞吐已由三个自然周期达到目标，但这不授权交易扩容；"
            if throughput_met else
            "当日全宇宙判断吞吐尚待后续自然周期闭合；"
        )
        + "继续拒绝生产交易扩容，等待来源长窗及模型成熟标签。"
    )
    gates = _one(blocks, "id", "gates_section")
    gates["body"] = (
        "## 未通过的硬门槛仍需自然时间证据\n\n"
        + (
            f"报告与推送完整度三个独立门均达到{_coverage_target_pct()}；"
            if report_gate_passed else
            f"日报已达到{_coverage_target_pct()}，但Push报告与精确送达按"
            f"完整计划槽仍未达到{_coverage_target_pct()}；"
        )
        + (
            "官方1H持仓倾向最新批次、不可覆盖批次键、24整点窗与96决策槽"
            f"均达到{_coverage_target_pct()}；"
            if positioning_gate_passed else
            f"官方1H持仓倾向最新批次已达到{_coverage_target_pct()}，"
            "但24整点窗与96决策槽"
            f"总体仍为{positioning_overall_status}；"
        )
        + (
            "官方15m合约OI/主动买卖量最新直采与96槽前向均达到99%，"
            if contract_gate_passed else
            "官方15m合约OI/主动买卖量尚未同时通过最新直采与96槽前向门，"
        )
        + "但四项目标不允许平均抵消。三周期原始OHLCV已过99%，"
        + (
            "4H全指标就绪率也已过99%；"
            if multitimeframe_ready else
            "4H全指标就绪率仍未达99%；"
        )
        + f"fast 14日长期窗口仍未达{_coverage_target_pct()}，当日判断量为"
        f"{observed_snapshots}/3自然周期、{observed_records}/{throughput_target}条"
        + ("，该吞吐硬门已达标；" if throughput_met else "，该吞吐硬门未达标；")
        + f"分析可信度也远低于{_credibility_target_pct()}。"
        "新闻子源虽已逐发布方记账，严格前向窗"
        "尚不足24小时。下一步只观察后续自然日吞吐、持仓倾向24整点与"
        "96决策槽、至少96个fast与合约统计前向槽、新闻24小时前向槽，"
        "并在新合约统计积累出足够历史后"
        "预注册新的独立未来模型验收。任何一步失败都不扩大真实交易。"
    )
    methods = _one(blocks, "id", "methods")
    methods["body"] = (
        "## 方法与定义\n\n"
        "覆盖率按每个关键数据族独立计算。资产分类以OKX官方instCategory"
        "为大类权威，有行率与语义兼容率分开；manual行受保护，未知类别"
        "不猜测。15m/1H/4H只选择UTC边界上精确"
        "已收盘K线，原始OHLCV完整率与全指标就绪率分开；新区历史不足仍"
        "留在全宇宙分母，是合法但不可交易的缺口。官方持仓倾向按一个精确"
        "collected_ts生产批次对最新ticker宇宙"
        "验收，账户多空比是账户数量比，不是持仓名义金额。合约统计按15m"
        "最新共同闭合桶采持仓量和USD主动买卖量，主动买入占比按买入/买卖"
        "合计复算；运行可用率等于本轮直采率加90分钟内一跳续用率，续用保持"
        "原始源时间、必须引用可验证直采原点且不进入模型，因此关键数据99%"
        "只认本轮直采。最新直采与修复后至少96个计划槽的直采率、逐槽通过率"
        "均须达到99%；缺槽进入分母。隔离验收只证明传输解析合同，不能替代"
        "自然生产。"
        "来源健康率按全部应运行计划槽重建，缺失账本行计不可用；新闻严格"
        "完整率只认ok，degraded与缺行均进分母，成功请求0条是合法无事件；"
        "修复后短窗与14日滚动窗并列展示。\n\n"
        "可信度定义为所选方向在15m/1H/4H对应周期使用可执行买卖价并额外"
        "扣20bp后的精确率，同时要求N、日期、周期、Wilson区间、ECE和独立"
        "未来窗。非线性诊断使用训练、模型选择、阈值选择、内部确认和历史"
        "留出时间分层，各相邻窗隔离4小时；组内排序进一步把同一观察的六个"
        "候选共同优化，但其内部确认仍独立于阈值选择。历史留出已被查看，"
        "只能作回顾诊断。任何历史结果都不能授权订单。"
    )
    limitations = _upsert_id(blocks, "limitations")
    limitations.update({
        "type": "markdown",
        "body": (
            "## 限制、不确定性与稳健性结果\n\n"
            + (
                f"最新合约统计运行可用性"
                f"{float(contract.get('availability_coverage_rate', 0)):.3%}、"
                f"模型可用直采"
                f"{float(contract.get('direct_coverage_rate', 0)):.3%}、"
                f"一跳续用"
                f"{float(contract.get('carry_forward_rate', 0)):.3%}；"
                f"严格前向直采"
                f"{float(contract.get('forward_direct_coverage_rate', 0)):.3%}"
                f"且仅{int(contract.get('forward_expected_slots', 0))}/"
                f"{int(contract.get('forward_minimum_slots', 96))}槽。"
                + (
                    "最新与长期门均已通过；"
                    if contract_gate_passed else
                    "因此尚未同时通过最新与长期99%门；"
                )
                if contract else
                "合约统计尚无自然验收证据；"
            )
            + (
                f"4H仍有{multitimeframe_4h['insufficient_history']}个新区"
                "因历史不足未形成全指标；对应原始OHLCV覆盖按逐周期审计"
                "如实保留，不以形成中K线或伪造指标补齐；"
                if multitimeframe_4h else ""
            )
            + "04:15旧并发架构的364/427失败被保留。新OI/主动买卖量"
            "尚无足够前向历史进入重训，当前冻结影子模型继续使用旧特征前缀。"
            "共享GBDT在阈值选择窗与内部确认窗明显反转，分候选GBDT和组内"
            "排序确认结果仍低且平均扣成本收益为负，显示时变和选择偏差风险。"
            "六选一事后上限因同时包含相反方向而机械偏高，不是可预测性证据。"
            f"新闻逐子源严格前向窗从"
            f"{headline.get('news_forward_start_cst', '预注册修复时点')}开始，"
            "短窗不能替代24小时验收。离线样本"
            "共享市场因子，20bp是统一成本缓冲而非逐单费用复原。"
        ),
    })
    further = _upsert_id(blocks, "further_questions")
    further.update({
        "type": "markdown",
        "body": (
            "## 后续需要回答的问题\n\n"
            "96个前向槽后，fast长期窗口是否仍受历史缺口拖累？合约统计"
            "最新直采率、前向直采符号行率和逐槽通过率能否同时保持99%？"
            "新闻关键父源"
            f"和六个RSS发布方在24小时后能否各自保持"
            f"{_coverage_target_pct()}严格完整率？"
            + (
                f"当日三个自然周期已累计{observed_records}条并达到"
                f"{throughput_target}条吞吐目标，后续日是否能持续保持？"
                if throughput_met else
                f"后续自然周期能否使当日量由{observed_records}条达到至少"
                f"{throughput_target}条？"
            )
            + "新合约OI和主动买卖量积累后，预注册模型在"
            f"完全独立未来窗能否同时达到{_credibility_target_pct()}"
            "精确率、N>=100、>=5天、>=100周期和ECE<=5pp？在这些问题有"
            "肯定证据前，目标保持进行中。"
        ),
    })
    for item in (limitations, further):
        blocks.remove(item)
        source_index = next(
            (index for index, block in enumerate(blocks)
             if block.get("id") == "sources_section"),
            len(blocks),
        )
        blocks.insert(source_index, item)
    sources_block = _one(blocks, "id", "sources_section")
    sources_block["body"] = (
        "## Sources\n\n"
        "主要证据来自严格计划槽来源审计、官方持仓倾向与合约OI/主动买卖量"
        "的隔离/自然批次审计、新闻逐发布方严格槽审计、右截尾修正的多周期"
        "校准、共享/分候选GBDT及组内排序嵌套时间诊断、实际生产分析信号"
        "结果审计、自然全宇宙吞吐、首个冻结模型未来周期、日报与Push"
        "确定性校验，"
        "以及 [OKX V5 API 官方文档]"
        "(https://www.okx.com/docs-v5/en/)。所有研究链路均为只读，"
        "本轮没有修改生产阈值、没有补派历史周期、没有下单。"
    )
    _sync_title_block(artifact["manifest"])
    return artifact


def _parse_source_cst(value: str) -> datetime:
    # 采集侧工件时间戳混有 UTC-Z 写法（market 三表/cross_market 口径）。Python
    # 3.11+ 的 fromisoformat 直接吃 'Z'，3.10 会抛 Invalid isoformat string——
    # 先归一到 '+00:00' 让两侧解析结果完全一致（与 render_push_report._ts_age_minutes
    # 同写法），语义不变：带 Z 即 UTC，随后仍统一 astimezone 到 UTC+8。
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def _atomic_write_json(path: Path, payload: dict) -> None:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--audit-relative-path", required=True)
    parser.add_argument("--push-audit", required=True)
    parser.add_argument("--push-audit-relative-path", required=True)
    parser.add_argument("--source-health-audit")
    parser.add_argument("--source-health-relative-path")
    parser.add_argument("--calibration-metrics")
    parser.add_argument("--calibration-relative-path")
    parser.add_argument("--policy-diagnostic")
    parser.add_argument("--policy-relative-path")
    parser.add_argument("--signal-audit")
    parser.add_argument("--signal-relative-path")
    parser.add_argument("--selective-diagnostic")
    parser.add_argument("--selective-relative-path")
    parser.add_argument("--selective-independent-diagnostic")
    parser.add_argument("--selective-independent-relative-path")
    parser.add_argument("--ranking-diagnostic")
    parser.add_argument("--ranking-relative-path")
    parser.add_argument("--directional-diagnostic")
    parser.add_argument("--directional-relative-path")
    parser.add_argument("--news-source-health-audit")
    parser.add_argument("--news-source-health-relative-path")
    parser.add_argument("--positioning-audit")
    parser.add_argument("--positioning-relative-path")
    parser.add_argument("--positioning-isolated-audit")
    parser.add_argument("--positioning-isolated-relative-path")
    parser.add_argument("--contract-statistics-audit")
    parser.add_argument("--contract-statistics-relative-path")
    parser.add_argument("--contract-statistics-isolated-audit")
    parser.add_argument("--contract-statistics-isolated-relative-path")
    parser.add_argument("--multitimeframe-audit")
    parser.add_argument("--multitimeframe-relative-path")
    parser.add_argument("--asset-class-audit")
    parser.add_argument("--asset-class-relative-path")
    parser.add_argument("--universe-evaluation")
    parser.add_argument("--universe-relative-path")
    parser.add_argument("--model-shadow")
    parser.add_argument("--model-shadow-relative-path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and build the refreshed report in memory without writing it",
    )
    args = parser.parse_args(argv)
    artifact_path = Path(args.artifact)
    audit_path = Path(args.audit)
    push_audit_path = Path(args.push_audit)
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    push_audit = json.loads(push_audit_path.read_text(encoding="utf-8"))
    updated = refresh_report_completeness(
        artifact,
        audit,
        push_audit,
        audit_relative_path=args.audit_relative_path,
        push_audit_relative_path=args.push_audit_relative_path,
    )
    source_result = None
    if bool(args.source_health_audit) != bool(args.source_health_relative_path):
        raise ValueError(
            "--source-health-audit and --source-health-relative-path are paired")
    if args.source_health_audit:
        source_result = json.loads(
            Path(args.source_health_audit).read_text(encoding="utf-8"))
        updated = refresh_source_health(
            updated,
            source_result,
            audit_relative_path=args.source_health_relative_path,
        )
    credibility_args = (
        args.calibration_metrics,
        args.calibration_relative_path,
        args.policy_diagnostic,
        args.policy_relative_path,
        args.signal_audit,
        args.signal_relative_path,
    )
    if any(credibility_args) and not all(credibility_args):
        raise ValueError("credibility evidence arguments must be supplied together")
    credibility_result = None
    if all(credibility_args):
        calibration_result = json.loads(
            Path(args.calibration_metrics).read_text(encoding="utf-8"))
        policy_result = json.loads(
            Path(args.policy_diagnostic).read_text(encoding="utf-8"))
        credibility_result = json.loads(
            Path(args.signal_audit).read_text(encoding="utf-8"))
        updated = refresh_credibility_evidence(
            updated,
            calibration_result,
            policy_result,
            credibility_result,
            calibration_relative_path=args.calibration_relative_path,
            policy_relative_path=args.policy_relative_path,
            signal_relative_path=args.signal_relative_path,
        )
    selective_args = (
        args.selective_diagnostic,
        args.selective_relative_path,
        args.selective_independent_diagnostic,
        args.selective_independent_relative_path,
    )
    if any(selective_args) and not all(selective_args):
        raise ValueError("selective evidence arguments must be supplied together")
    selective_result = None
    if all(selective_args):
        selective_result = json.loads(
            Path(args.selective_diagnostic).read_text(encoding="utf-8"))
        independent_selective = json.loads(
            Path(args.selective_independent_diagnostic).read_text(
                encoding="utf-8"))
        updated = refresh_selective_credibility(
            updated,
            selective_result,
            independent_selective,
            shared_relative_path=args.selective_relative_path,
            independent_relative_path=(
                args.selective_independent_relative_path),
        )
    if bool(args.ranking_diagnostic) != bool(args.ranking_relative_path):
        raise ValueError(
            "--ranking-diagnostic and --ranking-relative-path are paired")
    ranking_result = None
    if args.ranking_diagnostic:
        ranking_result = json.loads(
            Path(args.ranking_diagnostic).read_text(encoding="utf-8"))
        updated = refresh_ranking_credibility(
            updated,
            ranking_result,
            ranking_relative_path=args.ranking_relative_path,
        )
    if bool(args.directional_diagnostic) != bool(args.directional_relative_path):
        raise ValueError(
            "--directional-diagnostic and --directional-relative-path are paired")
    directional_result = None
    if args.directional_diagnostic:
        directional_result = json.loads(
            Path(args.directional_diagnostic).read_text(encoding="utf-8"))
        updated = refresh_directional_separability(
            updated,
            directional_result,
            diagnostic_relative_path=args.directional_relative_path,
        )
    if bool(args.news_source_health_audit) != bool(
        args.news_source_health_relative_path
    ):
        raise ValueError(
            "--news-source-health-audit and --news-source-health-relative-path "
            "are paired")
    news_source_result = None
    if args.news_source_health_audit:
        news_source_result = json.loads(
            Path(args.news_source_health_audit).read_text(encoding="utf-8"))
        updated = refresh_news_source_health(
            updated,
            news_source_result,
            audit_relative_path=args.news_source_health_relative_path,
        )
    positioning_args = (
        args.positioning_audit,
        args.positioning_relative_path,
        args.positioning_isolated_audit,
        args.positioning_isolated_relative_path,
    )
    if any(positioning_args) and not all(positioning_args):
        raise ValueError("positioning evidence arguments must be supplied together")
    positioning_result = None
    if all(positioning_args):
        positioning_result = json.loads(
            Path(args.positioning_audit).read_text(encoding="utf-8"))
        isolated_positioning = json.loads(
            Path(args.positioning_isolated_audit).read_text(encoding="utf-8"))
        updated = refresh_positioning_coverage(
            updated,
            positioning_result,
            isolated_positioning,
            natural_relative_path=args.positioning_relative_path,
            isolated_relative_path=args.positioning_isolated_relative_path,
        )
    contract_args = (
        args.contract_statistics_audit,
        args.contract_statistics_relative_path,
        args.contract_statistics_isolated_audit,
        args.contract_statistics_isolated_relative_path,
    )
    if any(contract_args) and not all(contract_args):
        raise ValueError(
            "contract-statistics evidence arguments must be supplied together")
    contract_result = None
    if all(contract_args):
        contract_result = json.loads(
            Path(args.contract_statistics_audit).read_text(encoding="utf-8"))
        isolated_contract = json.loads(
            Path(args.contract_statistics_isolated_audit).read_text(
                encoding="utf-8"))
        updated = refresh_contract_statistics_coverage(
            updated,
            contract_result,
            isolated_contract,
            natural_relative_path=args.contract_statistics_relative_path,
            isolated_relative_path=(
                args.contract_statistics_isolated_relative_path),
        )
    if bool(args.multitimeframe_audit) != bool(
        args.multitimeframe_relative_path
    ):
        raise ValueError(
            "--multitimeframe-audit and --multitimeframe-relative-path are paired")
    multitimeframe_result = None
    if args.multitimeframe_audit:
        multitimeframe_result = json.loads(
            Path(args.multitimeframe_audit).read_text(encoding="utf-8"))
        updated = refresh_multitimeframe_coverage(
            updated,
            multitimeframe_result,
            audit_relative_path=args.multitimeframe_relative_path,
        )
    if bool(args.asset_class_audit) != bool(args.asset_class_relative_path):
        raise ValueError(
            "--asset-class-audit and --asset-class-relative-path are paired")
    asset_class_result = None
    if args.asset_class_audit:
        asset_class_result = json.loads(
            Path(args.asset_class_audit).read_text(encoding="utf-8"))
        updated = refresh_asset_class_coverage(
            updated,
            asset_class_result,
            audit_relative_path=args.asset_class_relative_path,
        )
    runtime_args = (
        args.universe_evaluation,
        args.universe_relative_path,
        args.model_shadow,
        args.model_shadow_relative_path,
    )
    if any(runtime_args) and not all(runtime_args):
        raise ValueError("runtime evidence arguments must be supplied together")
    runtime_result = None
    model_shadow_result = None
    if all(runtime_args):
        runtime_result = json.loads(
            Path(args.universe_evaluation).read_text(encoding="utf-8"))
        model_shadow_result = json.loads(
            Path(args.model_shadow).read_text(encoding="utf-8"))
        updated = refresh_runtime_evidence(
            updated,
            runtime_result,
            model_shadow_result,
            evaluation_relative_path=args.universe_relative_path,
            model_relative_path=args.model_shadow_relative_path,
        )
    if (
        credibility_result is not None
        and positioning_result is not None
        and selective_result is not None
        and contract_result is not None
    ):
        updated = refresh_overall_narrative(updated)
    if not args.dry_run:
        _atomic_write_json(artifact_path, updated)
    print(json.dumps({
        "ok": True,
        "artifact": str(artifact_path),
        "artifact_written": not args.dry_run,
        "report_generated_at": updated["manifest"].get("generatedAt"),
        "daily_report_valid": audit["valid"],
        "daily_report_expected": audit["expected"],
        "daily_report_rate": audit["completeness_rate"],
        "push_report_complete": push_audit["counts"]["report_complete"],
        "push_expected_slots": push_audit["counts"]["expected_slots"],
        "push_report_rate": push_audit["rates"]["report_completeness_rate"],
        "push_delivery_complete": (
            push_audit["counts"]["delivered_report_complete"]),
        "push_delivery_rate": (
            push_audit["rates"]["delivered_report_completeness_rate"]),
        "push_rolling_status": push_audit["statuses"]["overall_status"],
        "push_forward_expected_slots": (
            push_audit["forward_after_remediation"]["counts"][
                "expected_slots"]),
        "push_forward_minimum_slots": (
            push_audit["forward_after_remediation"]["minimum_slots"]),
        "push_forward_rate": (
            push_audit["forward_after_remediation"]["rates"][
                "delivered_report_completeness_rate"]),
        "push_forward_status": (
            push_audit["forward_after_remediation"]["statuses"][
                "overall_status"]),
        "push_overall_status": push_audit["overall_status"],
        "source_health_status": (
            source_result["overall_status"] if source_result else None),
        "credibility_status": (
            policy_result["acceptance"]["status"]
            if credibility_result is not None else None
        ),
        "positioning_status": (
            positioning_result["status"] if positioning_result else None
        ),
        "positioning_storage_contract_status": (
            positioning_result.get("storage_contract", {}).get("status")
            if positioning_result
            else None
        ),
        "positioning_overall_status": (
            positioning_result.get("overall_status", "NOT_MET")
            if positioning_result else None
        ),
        "positioning_hourly_forward_slots": (
            positioning_result.get("forward_after_remediation", {}).get(
                "expected_slots", 0)
            if positioning_result else None
        ),
        "positioning_hourly_forward_minimum_slots": (
            positioning_result.get("forward_after_remediation", {}).get(
                "minimum_slots", POSITIONING_HOURLY_MINIMUM_SLOTS)
            if positioning_result else None
        ),
        "positioning_availability_forward_slots": (
            positioning_result.get("decision_availability_forward", {}).get(
                "expected_slots", 0)
            if positioning_result else None
        ),
        "positioning_availability_forward_minimum_slots": (
            positioning_result.get("decision_availability_forward", {}).get(
                "minimum_slots", POSITIONING_AVAILABILITY_MINIMUM_SLOTS)
            if positioning_result else None
        ),
        "selective_status": (
            selective_result["acceptance"]["confidence_90_status"]
            if selective_result else None
        ),
        "ranking_status": (
            ranking_result["acceptance"]["confidence_90_status"]
            if ranking_result else None
        ),
        "directional_status": (
            directional_result["acceptance"]["confidence_90_status"]
            if directional_result else None
        ),
        "news_source_health_status": (
            news_source_result["overall_status"]
            if news_source_result else None
        ),
        "contract_statistics_status": (
            contract_result["status"] if contract_result else None
        ),
        "contract_statistics_overall_status": (
            contract_result["overall_status"] if contract_result else None
        ),
        "contract_statistics_forward_slots": (
            contract_result["forward_after_remediation"]["expected_slots"]
            if contract_result else None
        ),
        "contract_statistics_forward_minimum_slots": (
            contract_result["forward_after_remediation"]["minimum_slots"]
            if contract_result else None
        ),
        "multitimeframe_status": (
            multitimeframe_result["status"]
            if multitimeframe_result else None
        ),
        "asset_class_status": (
            asset_class_result["status"] if asset_class_result else None
        ),
        "runtime_daily_target_met": (
            runtime_result["daily_throughput"]["latest_day"]["daily_target_met"]
            if runtime_result else None
        ),
        "model_shadow_status": (
            model_shadow_result["status"] if model_shadow_result else None
        ),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
