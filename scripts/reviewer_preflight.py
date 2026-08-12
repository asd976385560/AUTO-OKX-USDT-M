# -*- coding: utf-8 -*-
"""Bounded, read-only preflight for the 08:05 reviewer.

The script waits only for the daily-maintenance hand-off manifest.  It does
not run maintenance, edit a report, send a message, or infer readiness from a
stale artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
DEFAULT_READY_DIR = Path(os.environ.get(
    "OKX_REVIEWER_READY_DIR", r"./reports/quality"))
REQUIRED_CRITICAL_STEPS = frozenset({
    "reconcile",
    "account_bills",
    "quality_metrics",
})
ACCEPTED_RCS = {
    "reconcile": {0, 1},
    "account_bills": {0},
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


def validate_manifest(payload: dict, business_date: str) -> dict:
    errors: list[str] = []
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

    declared = payload.get("critical_steps")
    if not isinstance(declared, list):
        errors.append("critical_steps missing")
        declared_set = set()
    else:
        declared_set = {str(item) for item in declared}
        missing = REQUIRED_CRITICAL_STEPS - declared_set
        if missing:
            errors.append(
                "critical_steps incomplete: " + ",".join(sorted(missing)))

    steps = payload.get("steps")
    if not isinstance(steps, dict):
        errors.append("steps missing")
        steps = {}
    for name in sorted(REQUIRED_CRITICAL_STEPS):
        step = steps.get(name)
        if not isinstance(step, dict):
            errors.append(f"{name} step missing")
            continue
        if step.get("completed") is not True:
            errors.append(f"{name} step incomplete")
        if step.get("accepted") is not True:
            errors.append(f"{name} step not accepted")
        if step.get("rc") not in ACCEPTED_RCS[name]:
            errors.append(f"{name} step rc invalid")

    quality = steps.get("quality_metrics")
    if isinstance(quality, dict):
        errors.extend(_validate_quality_artifact(
            quality.get("artifact"), business_date))

    reconcile = steps.get("reconcile")
    reconcile_rc = (
        reconcile.get("rc") if isinstance(reconcile, dict) else None)
    provisional_required = reconcile_rc == 1
    expected_mode = (
        "provisional" if provisional_required else "final_candidate")
    if payload.get("provisional_required") is not provisional_required:
        errors.append("provisional_required differs from reconcile result")
    if payload.get("report_mode") != expected_mode:
        errors.append("report_mode differs from reconcile result")

    return {
        "ok": not errors,
        "business_date": business_date,
        "run_id": payload.get("run_id"),
        "report_mode": expected_mode if not errors else "blocked",
        "provisional_required": provisional_required,
        "errors": errors,
        "auto_send": False,
    }


def wait_for_manifest(
    path: Path,
    business_date: str,
    wait_seconds: float,
    poll_seconds: float,
) -> dict:
    deadline = time.monotonic() + wait_seconds
    last_result = {
        "ok": False,
        "business_date": business_date,
        "report_mode": "blocked",
        "errors": ["ready manifest not found"],
        "auto_send": False,
    }
    while True:
        if path.exists():
            try:
                last_result = validate_manifest(
                    _read_json(path), business_date)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                    ValueError) as exc:
                last_result = {
                    "ok": False,
                    "business_date": business_date,
                    "report_mode": "blocked",
                    "errors": [
                        f"ready manifest invalid: {type(exc).__name__}: {exc}"
                    ],
                    "auto_send": False,
                }
            if last_result["ok"]:
                return {**last_result, "manifest": str(path)}
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
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
