# -*- coding: utf-8 -*-
"""Bounded, read-only preflight for the 08:05 reviewer.

The script waits only for the daily-maintenance hand-off manifest.  It does
not run maintenance, edit a report, send a message, or infer readiness from a
stale artifact.
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
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from report_diagnostic_receipt import (
    build_receipt, compact_summary, failure, manifest_receipt,
    verify_manifest_receipt,
)

CST = timezone(timedelta(hours=8))
DEFAULT_READY_DIR = Path(os.environ.get(
    "OKX_REVIEWER_READY_DIR", _public_project_path('reports', 'quality')))
EXIT_QUALITY_REPORT_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_SCHEMA_VERSION = 2
EXIT_QUALITY_METHOD_VERSION = "exit_quality_v2_forward_frozen"
# 2026-08-19 G1：净 R 口径起用 v2；此处是**校验**侧，接受 v1|v2，
# 边界前归档的 v1 工件继续通过（历史不反向加责）。
EXIT_QUALITY_PEAK_METHOD_VERSION = "peak_giveback_forward_v2"
EXIT_QUALITY_PEAK_METHOD_VERSIONS_ACCEPTED = (
    "peak_giveback_forward_v1", "peak_giveback_forward_v2")
EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE = "2026-08-15T14:45"
EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD = (
    "authoritative_exit_fill_market_16x15m_v1")
BASE_REQUIRED_CRITICAL_STEPS = frozenset({
    "reconcile",
    "account_bills",
    "missed_opportunities",
    "ledger_invariants",
    "quality_metrics",
})
REQUIRED_CRITICAL_STEPS = BASE_REQUIRED_CRITICAL_STEPS | {"exit_quality"}
EXIT_QUALITY_DEGRADED_STEP = "exit_quality"
PROVISIONAL_DEGRADE_FROM = "2026-08-21"
ACCEPTED_RCS = {
    "reconcile": {0, 1},
    "account_bills": {0},
    "missed_opportunities": {0},
    "ledger_invariants": {0},
    "exit_quality": {0},
    "quality_metrics": {0},
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def today_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be an object")
    return payload


def _validate_quality_artifact(
    artifact: object, business_date: str
) -> list[str]:
    if not isinstance(artifact, dict):
        return ["quality_metrics artifact metadata missing"]
    path_text = str(artifact.get("path") or "").strip()
    expected_sha = str(artifact.get("sha256") or "").strip().lower()
    if not path_text or not expected_sha:
        return ["quality_metrics artifact path/hash missing"]
    path = Path(path_text)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"quality_metrics artifact unreadable: {type(exc).__name__}"]
    errors = []
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        errors.append("quality_metrics artifact hash differs")
    if not isinstance(payload, dict):
        errors.append("quality_metrics artifact root invalid")
    else:
        if str(payload.get("ts") or "")[:10] != business_date:
            errors.append("quality_metrics artifact business date differs")
        if not isinstance(payload.get("metrics"), dict):
            errors.append("quality_metrics artifact metrics missing")
    return errors


def _required_critical_steps(business_date: str) -> frozenset[str]:
    if f"{business_date} 08:00:00" < EXIT_QUALITY_REPORT_ACTIVATION_TS:
        return BASE_REQUIRED_CRITICAL_STEPS
    return REQUIRED_CRITICAL_STEPS


def _validate_exit_quality_artifact(
    artifact: object, business_date: str
) -> list[str]:
    """Verify manifest hash and identity without importing the producer."""
    if not isinstance(artifact, dict):
        return ["exit_quality artifact metadata missing"]
    path_text = str(artifact.get("path") or "").strip()
    expected_sha = str(artifact.get("sha256") or "").strip().lower()
    expected_size = artifact.get("size_bytes")
    if (
        not path_text or not expected_sha
        or not isinstance(expected_size, int) or expected_size < 0
    ):
        return ["exit_quality artifact path/hash/size missing"]
    path = Path(path_text)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"exit_quality artifact unreadable: {type(exc).__name__}"]
    errors = []
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        errors.append("exit_quality artifact hash differs")
    if len(raw) != expected_size:
        errors.append("exit_quality artifact size differs")
    if not isinstance(payload, dict):
        return errors + ["exit_quality artifact root invalid"]
    report_end = f"{business_date} 08:00:00"
    end = datetime.strptime(report_end, "%Y-%m-%d %H:%M:%S")
    report_start = (end - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    candidate_start = (end - timedelta(days=1, hours=4)).strftime(
        "%Y-%m-%d %H:%M:%S")
    candidate_end = (end - timedelta(hours=4)).strftime(
        "%Y-%m-%d %H:%M:%S")
    peak_effective_start = max(
        candidate_start, EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS)
    expected_peak_status = (
        "PENDING" if peak_effective_start >= candidate_end else "COMPLETE")
    peak_value = payload.get("peak_giveback")
    margin_value = payload.get("margin_return_review")
    missed_value = payload.get("missed_take_profit")
    peak = peak_value if isinstance(peak_value, dict) else {}
    margin = margin_value if isinstance(margin_value, dict) else {}
    missed = missed_value if isinstance(missed_value, dict) else {}
    missed_classes_value = missed.get("classification_counts")
    missed_classes = (
        missed_classes_value if isinstance(missed_classes_value, dict) else {})
    try:
        generated_at = datetime.strptime(
            str(payload.get("generated_at") or "")[:19],
            "%Y-%m-%d %H:%M:%S")
    except ValueError:
        generated_at = None
    checks = {
        "schema": payload.get("schema_version") == EXIT_QUALITY_SCHEMA_VERSION,
        "method": payload.get("method_version") == EXIT_QUALITY_METHOD_VERSION,
        "business date": payload.get("business_date") == business_date,
        "window closed before generation": (
            generated_at is not None and generated_at >= end),
        "report activation": payload.get("report_activation_cst")
        == EXIT_QUALITY_REPORT_ACTIVATION_TS,
        "margin activation": payload.get("margin_fact_activation_cycle")
        == EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE,
        "counterfactual activation": payload.get(
            "counterfactual_activation_cst")
        == EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS,
        "report window": (
            (payload.get("report_window") or {}).get("start_ts"),
            (payload.get("report_window") or {}).get("end_ts"),
            (payload.get("report_window") or {}).get("end_exclusive"),
        ) == (report_start, report_end, True),
        "candidate window": (
            (payload.get("candidate_window") or {}).get("start_ts"),
            (payload.get("candidate_window") or {}).get("end_ts"),
            (payload.get("candidate_window") or {}).get("end_exclusive"),
        ) == (candidate_start, candidate_end, True),
        "blocks": all(isinstance(payload.get(key), dict) for key in (
            "peak_giveback", "margin_return_review", "missed_take_profit")),
        "counterfactual upstream": (
            missed.get("evidence_method_version")
            == EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD
            and missed.get("counterfactual_activation_cst")
            == EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS
            and missed.get("upstream_status") == "READY"
        ),
        "strict live open scopes": (
            peak.get("method_version")
            in EXIT_QUALITY_PEAK_METHOD_VERSIONS_ACCEPTED
            and peak.get("fact_activation_cst")
            == EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS
            and peak.get("status") == expected_peak_status
            and (
                (peak.get("effective_window") or {}).get("start_ts"),
                (peak.get("effective_window") or {}).get("end_ts"),
                (peak.get("effective_window") or {}).get("end_exclusive"),
            ) == (peak_effective_start, candidate_end, True)
            and all(isinstance(peak.get(key), int) and peak.get(key) >= 0
                    for key in ("candidate_closed_rows",
                                "pre_activation_excluded_rows"))
            and all(isinstance(peak.get(key), int) and peak.get(key) >= 0
                for key in ("source_closed_rows", "excluded_non_live_rows",
                            "excluded_non_open_rows", "closed_rows"))
            and all(isinstance(margin.get(key), int) and margin.get(key) >= 0
                    for key in ("source_candidate_cycle_rows",
                                "excluded_non_live_cycle_rows",
                                "excluded_non_open_position_rows",
                                "total_position_cycles"))
            and "requested_unconfirmed" in (
                margin.get("disposition_counts") or {})
            and all(isinstance(missed.get(key), int) and missed.get(key) >= 0
                    for key in ("source_closed_rows",
                                "excluded_profile_count",
                                "excluded_fallback_count"))
            and all(isinstance(missed_classes.get(key), int)
                    and missed_classes.get(key) >= 0
                    for key in ("missed_take_profit", "excluded_profile",
                                "excluded_fallback"))
            and isinstance(missed.get("pool_size"), int)
            and missed.get("pool_size") >= 0
            and missed.get("pool_size")
            == missed_classes.get("missed_take_profit")
        ),
        "safety": payload.get("safety") == {
            "production_database_writes": 0,
            "cycles_replayed": 0,
            "window_extended": False,
            "orders_placed": 0,
        },
    }
    errors.extend(
        f"exit_quality artifact {name} differs"
        for name, valid in checks.items() if not valid)
    return errors


def validate_manifest(payload: dict, business_date: str) -> dict:
    errors: list[str] = []
    diagnostic_failures: list[dict] = []
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if payload.get("business_date") != business_date:
        errors.append("business_date differs")
    if payload.get("state") != "ready" or payload.get("ready") is not True:
        errors.append("maintenance hand-off is not ready")
    if payload.get("auto_send") is not False:
        errors.append("auto_send must be false")
    if not str(payload.get("run_id") or "").strip():
        errors.append("run_id missing")
    if str(payload.get("maintenance_started_at") or "")[:10] != business_date:
        errors.append("maintenance_started_at differs")
    if str(payload.get("critical_steps_completed_at") or "")[:10] != business_date:
        errors.append("critical_steps_completed_at differs")

    required_steps = _required_critical_steps(business_date)
    declared = payload.get("critical_steps")
    if not isinstance(declared, list):
        errors.append("critical_steps missing")
        declared_set = set()
    else:
        declared_set = {str(item) for item in declared}
        missing = required_steps - declared_set
        if missing:
            errors.append(
                "critical_steps incomplete: " + ",".join(sorted(missing)))

    steps = payload.get("steps")
    if not isinstance(steps, dict):
        errors.append("steps missing")
        steps = {}
    raw_degraded = payload.get("degraded_critical_steps")
    if raw_degraded is None:
        raw_degraded = []
    if not isinstance(raw_degraded, list):
        errors.append("degraded_critical_steps invalid")
        raw_degraded = []
    declared_degraded = {str(item) for item in raw_degraded}
    degradable = (
        {EXIT_QUALITY_DEGRADED_STEP}
        if business_date >= PROVISIONAL_DEGRADE_FROM else set()
    )
    unexpected_degraded = declared_degraded - degradable
    if unexpected_degraded:
        errors.append(
            "unexpected degraded critical steps: "
            + ",".join(sorted(unexpected_degraded)))

    for name in sorted(required_steps):
        error_start = len(errors)
        step = steps.get(name)
        if not isinstance(step, dict):
            errors.append(f"{name} step missing")
            diagnostic_failures.append(failure(name, reason=errors[-1]))
            continue
        if step.get("completed") is not True:
            errors.append(f"{name} step incomplete")
        if name in declared_degraded:
            if step.get("accepted") is not False:
                errors.append(f"{name} degraded step must be unaccepted")
            rc = step.get("rc")
            if not isinstance(rc, int) or rc in ACCEPTED_RCS[name]:
                errors.append(f"{name} degraded step rc invalid")
        else:
            if step.get("accepted") is not True:
                errors.append(f"{name} step not accepted")
            if step.get("rc") not in ACCEPTED_RCS[name]:
                errors.append(f"{name} step rc invalid")
        if len(errors) > error_start:
            diagnostic_failures.append(failure(
                name, step, reason=compact_summary(errors[error_start:])))

    quality = steps.get("quality_metrics")
    if isinstance(quality, dict):
        artifact_errors = _validate_quality_artifact(
            quality.get("artifact"), business_date)
        errors.extend(artifact_errors)
        if artifact_errors:
            diagnostic_failures.append(failure(
                "quality_metrics", quality, reason=compact_summary(artifact_errors)))
    if "exit_quality" in required_steps:
        exit_step = steps.get("exit_quality")
        if (
            isinstance(exit_step, dict)
            and EXIT_QUALITY_DEGRADED_STEP not in declared_degraded
        ):
            artifact_errors = _validate_exit_quality_artifact(
                exit_step.get("artifact"), business_date)
            errors.extend(artifact_errors)
            if artifact_errors:
                diagnostic_failures.append(failure(
                    "exit_quality", exit_step, reason=compact_summary(artifact_errors)))

    derived_degraded = sorted(
        name for name in degradable
        if isinstance(steps.get(name), dict)
        and steps[name].get("completed") is True
        and steps[name].get("accepted") is not True
    )
    if declared_degraded != set(derived_degraded):
        errors.append("degraded_critical_steps differ from step results")
    if (
        payload.get("provisional_degrade_from") is not None
        and payload.get("provisional_degrade_from") != PROVISIONAL_DEGRADE_FROM
    ):
        errors.append("provisional_degrade_from differs")

    reconcile = steps.get("reconcile")
    reconcile_rc = (
        reconcile.get("rc") if isinstance(reconcile, dict) else None)
    provisional_required = reconcile_rc == 1 or bool(derived_degraded)
    expected_mode = (
        "provisional" if provisional_required else "final_candidate")
    if payload.get("provisional_required") is not provisional_required:
        errors.append("provisional_required differs from critical-step result")
    if payload.get("report_mode") != expected_mode:
        errors.append("report_mode differs from critical-step result")
    expected_reasons = (
        (["live_reconcile_unresolved"] if reconcile_rc == 1 else [])
        + [f"critical_step_degraded:{name}" for name in derived_degraded]
    )
    raw_reasons = payload.get("provisional_reasons")
    if raw_reasons is None:
        raw_reasons = []
    if raw_reasons != expected_reasons:
        errors.append("provisional_reasons differ from critical-step result")

    try:
        verify_manifest_receipt(payload, business_date)
    except (OSError, ValueError, TypeError) as exc:
        diagnostic_error = compact_summary(f"{type(exc).__name__}: {exc}")
        errors.append(diagnostic_error)
        diagnostic_failures.insert(0, failure(
            "reviewer_preflight.verify_diagnostic_receipt", reason=diagnostic_error))

    result = {
        "ok": not errors,
        "business_date": business_date,
        "run_id": payload.get("run_id"),
        "report_mode": expected_mode if not errors else "blocked",
        "provisional_required": provisional_required,
        "errors": errors,
        "auto_send": False,
    }
    if result["report_mode"] in {"blocked", "provisional"}:
        # Build snapshots while polling, but emit only the terminal result.
        # With no new failure this is byte-identical to maintenance's receipt.
        if payload.get("business_date") != business_date:
            result["diagnostic_receipt"] = build_receipt(
                business_date, None, "blocked", [failure(
                    "reviewer_preflight.identity", reason="business_date differs")])
        else:
            result["diagnostic_receipt"] = manifest_receipt(
                payload, business_date, result["report_mode"],
                validation_failures=diagnostic_failures, errors=errors)
    return result


def _unreadable_manifest_result(
    business_date: str, error: str, *, failed_step: str = "reviewer_preflight.read_manifest"
) -> dict:
    return {
        "ok": False,
        "business_date": business_date,
        "report_mode": "blocked",
        "errors": [error],
        "auto_send": False,
        "diagnostic_receipt": build_receipt(
            business_date, None, "blocked",
            [failure(failed_step, reason=error)]),
    }


def wait_for_manifest(
    path: Path,
    business_date: str,
    wait_seconds: float,
    poll_seconds: float,
) -> dict:
    deadline = time.monotonic() + wait_seconds
    last_result = _unreadable_manifest_result(
        business_date, "ready manifest not found")
    while True:
        if path.exists():
            failed_step = "reviewer_preflight.read_manifest"
            try:
                payload = _read_json(path)
                failed_step = "reviewer_preflight.validate_manifest"
                last_result = validate_manifest(payload, business_date)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                    ValueError, TypeError, AttributeError) as exc:
                last_result = _unreadable_manifest_result(
                    business_date,
                    f"ready manifest invalid: {type(exc).__name__}: {exc}",
                    failed_step=failed_step)
            if last_result["ok"]:
                return {**last_result, "manifest": str(path)}
        else:
            last_result = _unreadable_manifest_result(
                business_date, "ready manifest not found")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                **last_result,
                "manifest": str(path),
                "timed_out": wait_seconds > 0,
            }
        time.sleep(min(poll_seconds, remaining))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="read-only bounded reviewer readiness preflight")
    parser.add_argument("--ready-dir", default=str(DEFAULT_READY_DIR))
    parser.add_argument("--business-date", default=today_cst())
    parser.add_argument("--wait-seconds", type=float, default=1200.0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 1800:
        parser.error("--wait-seconds must be between 0 and 1800")
    if not 0.05 <= args.poll_seconds <= 30:
        parser.error("--poll-seconds must be between 0.05 and 30")
    try:
        datetime.strptime(args.business_date, "%Y-%m-%d")
    except ValueError:
        parser.error("--business-date must be YYYY-MM-DD")

    path = Path(args.ready_dir) / (
        f"reviewer_ready_{args.business_date}.json")
    result = wait_for_manifest(
        path,
        args.business_date,
        args.wait_seconds,
        args.poll_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
