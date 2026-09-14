# -*- coding: utf-8 -*-
"""Compact local diagnostics; no database, subprocess, network, or repair access.

Maintenance archives one create-only receipt per run. Read-only consumers emit
the same sealed value on stdout, once at their terminal outcome. Diagnostic
text is evidence, never an executable recovery instruction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

CONTRACT_VERSION = 1
SUMMARY_LIMIT = 384
CRITICAL_STEP_ORDER = (
    "reconcile", "account_bills", "missed_opportunities", "exit_quality",
    "ledger_invariants", "quality_metrics",
)
SAFETY_BOUNDARY = (
    "No backfill, reconciliation apply, database writes, or report delivery."
)


def compact_summary(value: object) -> str:
    """Keep the error tail, with bounded text and common credentials removed."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(item) for item in value)
    text = str(value or "")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(
        r"(?i)\b(authorization\s*[:=]\s*(?:(?:bearer|basic)\s+)?|bearer\s+)\S+",
        r"\1[REDACTED]", text)
    text = re.sub(
        r'''(?ix)(["']?(?:ok-access-(?:key|sign|passphrase)|api[_-]?key|
        access[_-]?token|token|secret|password|passphrase|signature)["']?
        \s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
        r"\1[REDACTED]", text)
    lines = text.strip().splitlines()[-3:]
    text = " | ".join(" ".join(line.split()) for line in lines if line.strip())
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return text if len(text) <= SUMMARY_LIMIT else "..." + text[-(SUMMARY_LIMIT - 3):]


def failure(step: str, result: dict | None = None, *, reason: str = "") -> dict:
    result = result or {}
    artifact = result.get("artifact")
    artifact_error = artifact.get("error") if isinstance(artifact, dict) else ""
    return {
        "failed_step": step,
        "return_code": result.get("rc"),
        # Empty means no stderr was captured; never fabricate it from stdout.
        "stderr_summary": compact_summary(
            result.get("stderr_summary", result.get("stderr"))),
        "failure_summary": compact_summary(
            reason or result.get("error") or artifact_error
            or ("step incomplete" if result.get("completed") is False
                else "step not accepted")),
    }


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def receipt_bytes(receipt: dict) -> bytes:
    return _canonical(receipt) + b"\n"


def build_receipt(business_date: str, run_id: str | None, report_mode: str,
                  failures: list[dict]) -> dict:
    if report_mode not in {"blocked", "provisional"} or not failures:
        raise ValueError("diagnostic receipt requires a blocked/provisional cause")
    end = datetime.strptime(business_date, "%Y-%m-%d").replace(hour=8)
    start = end - timedelta(days=1)
    # Detach from mutable step/manifest objects before sealing the snapshot.
    failures = json.loads(_canonical({"failures": failures}))["failures"]
    primary = failures[0]
    action = (
        "STOP normal reporting; inspect the named step's existing logs and "
        "frozen artifacts read-only for this window."
        if report_mode == "blocked" else
        "Keep the provisional label and unavailable sections explicit; inspect "
        "the named step's existing evidence read-only. Existing validators and "
        "STOP rules still govern publication."
    )
    receipt = {
        "schema_version": CONTRACT_VERSION,
        "kind": "daily_report_diagnostic",
        "business_date": business_date,
        "run_id": run_id,
        "report_mode": report_mode,
        **primary,
        "affected_window": {
            "start_ts": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_ts": end.strftime("%Y-%m-%d %H:%M:%S"),
            "end_exclusive": True,
            "timezone": "UTC+08:00",
        },
        "safe_next_action": action + " " + SAFETY_BOUNDARY,
        "auto_send": False,
    }
    if len(failures) > 1:
        receipt["additional_failures"] = failures[1:]
    if any(item["failed_step"] in {"exit_quality", "missed_opportunities"}
           for item in failures):
        receipt["candidate_window"] = {
            "start_ts": (start - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_ts": (end - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_exclusive": True,
            "timezone": "UTC+08:00",
        }
    receipt["receipt_id"] = "sha256:" + hashlib.sha256(_canonical(receipt)).hexdigest()
    return receipt


def manifest_receipt(manifest: dict, business_date: str, report_mode: str,
                     *, validation_failures: list[dict] | None = None,
                     errors: list[str] | None = None) -> dict:
    """Choose the first hard failure, keeping other causes in the same receipt."""
    hard, soft = [], []
    same_day = manifest.get("business_date") == business_date
    steps = manifest.get("steps") if same_day else {}
    steps = steps if isinstance(steps, dict) else {}
    degraded = manifest.get("degraded_critical_steps") or []
    required = manifest.get("critical_steps") or []
    degraded = degraded if isinstance(degraded, list) else []
    required = required if isinstance(required, list) else []
    for name in CRITICAL_STEP_ORDER:
        if name not in required or not same_day:
            continue
        step = steps.get(name)
        if not isinstance(step, dict):
            hard.append(failure(name, reason="step missing"))
        elif step.get("completed") is not True or step.get("accepted") is not True:
            target = soft if name in degraded else hard
            target.append(failure(name, step))
        elif name == "reconcile" and step.get("rc") == 1:
            soft.append(failure(name, step, reason="live reconciliation unresolved"))
    validation_failures = validation_failures or []
    ordered = (hard + validation_failures + soft
               if manifest.get("report_mode") == "blocked"
               else validation_failures + hard + soft)
    # A validator may describe the same failed step again; retain one cause.
    causes, seen = [], set()
    for item in ordered:
        if item["failed_step"] not in seen:
            causes.append(item)
            seen.add(item["failed_step"])
    if not causes:
        causes = [failure("reviewer_preflight", reason=compact_summary(errors)
                          or "maintenance hand-off is not ready")]
    return build_receipt(business_date, manifest.get("run_id") if same_day else None,
                         report_mode, causes)


def verify_manifest_receipt(manifest: dict, business_date: str) -> None:
    """Check both the seal and its binding to the step snapshot, read-only."""
    if manifest.get("diagnostic_contract_version") is None:
        return  # Existing historical manifests retain their original contract.
    if manifest["diagnostic_contract_version"] != CONTRACT_VERSION:
        raise ValueError("diagnostic contract version invalid")
    mode = manifest.get("report_mode")
    receipt = manifest.get("diagnostic_receipt")
    if mode == "final_candidate" or manifest.get("state") == "running":
        if receipt is not None:
            raise ValueError("unexpected diagnostic receipt")
        return
    expected = manifest_receipt(manifest, business_date, mode)
    if receipt != expected:
        raise ValueError("diagnostic receipt differs from critical-step snapshot")
    path = manifest.get("diagnostic_receipt_path")
    if path and Path(path).read_bytes() != receipt_bytes(expected):
        raise ValueError("immutable diagnostic receipt bytes differ")


def write_once_receipt(directory: Path, receipt: dict) -> Path:
    """Create once per maintenance run; never replace a conflicting receipt."""
    identity = _canonical({key: receipt[key] for key in ("business_date", "run_id")})
    key = hashlib.sha256(identity).hexdigest()
    path = directory / f"daily_report_diagnostic_{key}.json"
    raw = receipt_bytes(receipt)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError("immutable diagnostic receipt conflict") from None
    return path


def main(argv=None) -> int:
    """Stdout-only receipt for later reviewer STOP paths (writer/validator/etc.)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-date", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--report-mode", choices=("blocked", "provisional"), required=True)
    parser.add_argument("--failed-step", required=True)
    parser.add_argument("--return-code", type=int)
    parser.add_argument("--stderr-file", type=Path)
    parser.add_argument("--reason", default="reviewer STOP")
    args = parser.parse_args(argv)
    try:
        # Bounded tail read; the original evidence file remains untouched.
        stderr = b""
        if args.stderr_file:
            with args.stderr_file.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 16384))
                stderr = handle.read(16384)
        receipt = build_receipt(args.business_date, args.run_id, args.report_mode, [
            failure(args.failed_step, {"rc": args.return_code, "stderr": stderr},
                    reason=args.reason),
        ])
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(receipt_bytes(receipt).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
