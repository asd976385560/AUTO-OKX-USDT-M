# -*- coding: utf-8 -*-
"""Fail-open-to-legacy launcher for the read-only candidate evidence bundle."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts import _acceptance_thresholds as thresholds
from scripts.multitimeframe_decision_evidence import (
    candidate_evidence_paths,
    load_candidate_evidence_bundle,
    load_candidate_manifest,
)


_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
EVIDENCE_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "multitimeframe_decision_evidence.py"
)
PYTHON_WRAPPER = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_okx_python.ps1")
POWERSHELL_EXE = os.environ.get("OKX_PWSH_EXE", "pwsh")
CST = timezone(timedelta(hours=8))


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True,
                      indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _bounded_text(value: Any, limit: int = 1000) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def prepare_candidate_bundle(
    *,
    cycle_id: str,
    db_root: Path,
    phase: str,
    timeout_seconds: int,
    evidence_root: Path | None = None,
) -> dict:
    """Run one local batch and always return/write a bounded fallback receipt."""
    paths = candidate_evidence_paths(cycle_id, root=evidence_root)
    started = time.perf_counter()
    base = {
        "schema": "candidate_bundle_runtime_v1",
        "cycle_id": str(cycle_id),
        "phase": str(phase),
        "manifest_path": str(paths["manifest"]),
        "ready_pool_path": str(paths["ready_pool"]),
        "bundle_path": str(paths["bundle"]),
        "decision_slice_path": str(paths["decision_slice"]),
        "status_path": str(paths["status"]),
        "hard_timeout_seconds": int(timeout_seconds),
        "production_database_writes": 0,
        "orders_placed": 0,
        "fallback_path": (
            "preloaded_briefing_review_slice"
            if thresholds.decision_restriction_removal_active(cycle_id)
            else "legacy_single_symbol"),
    }
    if phase not in {"shadow", "consume"}:
        return {
            **base,
            "status": "OFF",
            "fallback_required": False,
            "wall_elapsed_seconds": 0.0,
        }
    if evidence_root is None:
        now = datetime.now(CST)
        current_cycle = now.replace(
            minute=(now.minute // 15) * 15,
            second=0, microsecond=0,
        ).strftime("%Y-%m-%dT%H:%M")
        if str(cycle_id) != current_cycle:
            return {
                **base,
                "status": "REFUSED_IMMUTABLE_HISTORY",
                "fallback_required": True,
                "decision_consumes_bundle": False,
                "manifest_valid": False,
                "error": "historical production bundle/status overwrite refused",
                "status_receipt_written": False,
                "wall_elapsed_seconds": 0.0,
            }
    status: dict
    manifest: dict | None = None
    manifest_context = {
        "manifest_valid": False,
        "briefing_sha256": None,
        "ready_pool_status": "UNAVAILABLE",
        "ready_pool_sha256": None,
        "full_ready_count": None,
        "candidate_count": None,
    }
    try:
        manifest, _ = load_candidate_manifest(paths["manifest"], cycle_id)
        ready_pool = (
            manifest.get("ready_pool")
            if isinstance(manifest.get("ready_pool"), dict) else {})
        manifest_context = {
            "manifest_valid": True,
            "briefing_sha256": manifest.get("manifest_sha256"),
            "ready_pool_status": str(
                ready_pool.get("status") or "PASSED").upper(),
            "ready_pool_sha256": ready_pool.get("sha256"),
            "full_ready_count": ready_pool.get("ready_count"),
            "candidate_count": manifest.get("candidate_count"),
        }
        command = [
            POWERSHELL_EXE,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(PYTHON_WRAPPER),
            str(EVIDENCE_SCRIPT),
            "--db-root", str(db_root),
            "--candidates-file", str(paths["manifest"]),
            "--cycle-id", str(cycle_id),
            "--out-file", str(paths["bundle"]),
        ]
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout_seconds)),
            creationflags=_CREATE_NO_WINDOW,
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"bundle_process_rc={process.returncode};"
                f"stdout={_bounded_text(process.stdout)};"
                f"stderr={_bounded_text(process.stderr)}"
            )
        bundle = load_candidate_evidence_bundle(
            paths["bundle"],
            expected_cycle=cycle_id,
            expected_manifest_path=paths["manifest"],
        )
        closure_symbol_review = thresholds.minimal_contract_closure_active(
            cycle_id)
        minimal_policy = (
            thresholds.minimal_decision_contract_active(cycle_id)
            or closure_symbol_review
        )
        decision_slice = {
            "schema": (
                "candidate_decision_slice_v3_symbol_review"
                if closure_symbol_review else
                "candidate_decision_slice_v2_side_neutral"
                if minimal_policy
                else "candidate_decision_slice_v1"),
            "cycle_id": str(cycle_id),
            "source_bundle": str(paths["bundle"]),
            "source_bundle_sha256": bundle.get("bundle_sha256"),
            "manifest_count": bundle.get(
                "manifest_count", bundle.get("candidate_count")),
            "decision_slice_count": len(bundle.get("items") or []),
            "items": list(bundle.get("items") or []),
            "production_database_writes": 0,
            "orders_placed": 0,
            "timeframe_judgment_used": (
                False if minimal_policy
                else True),
        }
        if closure_symbol_review:
            decision_slice.update({
                "review_identity_contract": (
                    "symbol_review_v2_full_manifest_no_identity_gate"),
                "review_symbols": list(bundle.get("review_symbols") or []),
            })
        decision_slice_sha = hashlib.sha256(
            json.dumps(
                decision_slice, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        decision_slice["decision_slice_sha256"] = decision_slice_sha
        _atomic_json(paths["decision_slice"], decision_slice)
        status = {
            **base,
            **manifest_context,
            "status": "PASSED",
            "fallback_required": phase == "shadow",
            "decision_consumes_bundle": phase == "consume",
            "candidate_count": bundle.get("candidate_count"),
            "manifest_count": bundle.get(
                "manifest_count", bundle.get("candidate_count")),
            "screened_count": bundle.get("screened_count"),
            "ready_count": bundle.get(
                "decision_ready_count", bundle.get("ready_count")),
            "full_screening_ready_count": bundle.get("ready_count"),
            "not_ready_count": bundle.get("not_ready_count"),
            "decision_slice_count": bundle.get(
                "decision_slice_count", bundle.get("candidate_count")),
            "decision_slice_path": str(paths["decision_slice"]),
            "decision_slice_sha256": decision_slice_sha,
            "bundle_sha256": bundle.get("bundle_sha256"),
            "bundle_elapsed_seconds": bundle.get("elapsed_seconds"),
            "stdout_summary": _bounded_text(process.stdout),
            "error": None,
        }
    except subprocess.TimeoutExpired as exc:
        status = {
            **base,
            **manifest_context,
            "status": "DEGRADED",
            "fallback_required": True,
            "decision_consumes_bundle": False,
            "error": (
                f"TimeoutExpired:{int(timeout_seconds)}s;"
                f"stdout={_bounded_text(exc.stdout)};"
                f"stderr={_bounded_text(exc.stderr)}"
            ),
        }
    except Exception as exc:  # noqa: BLE001 - degradation must not stop exits
        status = {
            **base,
            **manifest_context,
            "status": "DEGRADED",
            "fallback_required": True,
            "decision_consumes_bundle": False,
            "error": _bounded_text(f"{type(exc).__name__}:{exc}"),
        }
    status["wall_elapsed_seconds"] = round(
        time.perf_counter() - started, 6)
    try:
        _atomic_json(paths["status"], status)
    except Exception as exc:  # noqa: BLE001
        status["status_receipt_error"] = _bounded_text(
            f"{type(exc).__name__}:{exc}")
    return status
