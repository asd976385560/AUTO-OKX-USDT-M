# -*- coding: utf-8 -*-
"""Read-only forward audit for the 16-screen / dynamic-8 candidate rollout."""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scripts import _acceptance_thresholds as thresholds
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import stage_runner  # noqa: E402
from scripts.multitimeframe_decision_evidence import (
    CANDIDATE_EVIDENCE_DIR,
    _canonical_sha256,
    candidate_evidence_paths,
    load_candidate_evidence_bundle,
    load_candidate_manifest,
)


CST = thresholds.CST
DEFAULT_ANALYSIS_DB = Path(_public_project_path('db', 'analysis.db'))
DEFAULT_STATUS_DIR = Path(_public_project_path('logs', 'stage-status'))
DEFAULT_JSON_OUT = Path(
    _public_project_path('reports', 'quality', 'candidate-bundle-rollout-latest.json'))
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, sort_keys=True,
                      indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _average(values: list[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return round(sum(clean) / len(clean), 6) if clean else None


def _percentile(values: list[float], fraction: float) -> float | None:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return None
    index = max(0, min(len(clean) - 1, int((len(clean) - 1) * fraction)))
    return round(clean[index], 6)


def _planned_cycles(
    start: datetime,
    end: datetime,
    *,
    finality_seconds: int,
) -> list[str]:
    cursor = start.replace(second=0, microsecond=0)
    mature_before = end - timedelta(seconds=max(0, finality_seconds))
    cycles = []
    while cursor <= mature_before:
        cycles.append(cursor.strftime("%Y-%m-%dT%H:%M"))
        cursor += timedelta(minutes=15)
    return cycles


def _analysis_rows(analysis_db: Path, cycles: list[str]) -> dict[str, dict]:
    if not cycles or not analysis_db.exists():
        return {}
    connection = sqlite3.connect(
        analysis_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows: dict[str, dict] = {}
        for offset in range(0, len(cycles), 300):
            batch = cycles[offset:offset + 300]
            marks = ",".join("?" for _ in batch)
            for row in connection.execute(
                "SELECT cycle_id,status,raw FROM analysis_runs "
                f"WHERE cycle_id IN ({marks})", batch,
            ):
                payload = _load_raw(row["raw"])
                inner = payload.get("raw") if isinstance(payload, dict) else None
                inner = _load_raw(inner)
                rows[str(row["cycle_id"])] = {
                    "status": str(row["status"] or ""),
                    "raw": inner,
                }
        return rows
    finally:
        connection.close()


def _load_raw(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strict_sla(cycle: str, status_dir: Path) -> tuple[bool, int | None, str | None]:
    safe = cycle.replace(":", "-")
    live = _load_json(status_dir / f"live-{safe}.json") or {}
    push = _load_json(status_dir / f"push-{safe}.json") or {}
    sla = stage_runner.build_complete_cycle_sla(
        cycle,
        push.get("post_live_reconcile")
        if isinstance(push.get("post_live_reconcile"), dict) else {},
        live_status=live,
    )
    elapsed = sla.get("elapsed_seconds")
    failure_kind = str(live.get("failure_kind") or "").strip() or None
    return (
        sla.get("strict_cycle_pass") is True,
        int(elapsed) if isinstance(elapsed, int) else None,
        failure_kind,
    )


def _load_audit_artifact_pair(paths: dict[str, Path], cycle: str) -> tuple:
    """Reuse a manifest only when the full bundle validator proves its hash.

    The bundle loader still revalidates the manifest, ready pool, and all bundle
    contracts. A separately read manifest with the same canonical digest is
    the exact content that validation accepted. This avoids running the full
    manifest/ready-pool validation twice for every successful historical slot.
    All failures retain the original ordered validation and partial diagnostics.
    No validator option, persistent cache, or trading path is changed.
    """
    try:
        candidate = _load_json(paths["manifest"])
        if not isinstance(candidate, dict):
            raise ValueError("manifest unavailable")
        core = dict(candidate)
        supplied = core.pop("manifest_sha256", None)
        digest = _canonical_sha256(core)
        if supplied != digest or candidate.get("cycle_id") != cycle:
            raise ValueError("manifest content binding invalid")
        validated = load_candidate_evidence_bundle(
            paths["bundle"], expected_cycle=cycle,
            expected_manifest_path=paths["manifest"])
        if validated.get("candidate_manifest_sha256") != digest:
            raise ValueError("validated manifest changed during audit")
        return candidate, validated, None
    except Exception:  # preserve original failure ordering and diagnostics
        manifest = None
        bundle = None
        error = None
        try:
            manifest, _ = load_candidate_manifest(paths["manifest"], cycle)
            bundle = load_candidate_evidence_bundle(
                paths["bundle"], expected_cycle=cycle,
                expected_manifest_path=paths["manifest"])
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}:{exc}"
        return manifest, bundle, error


def _cycle_row(
    cycle: str,
    *,
    phase: str,
    evidence_dir: Path,
    analysis: dict,
    status_dir: Path,
) -> dict:
    minimal_policy = thresholds.minimal_decision_contract_active(cycle)
    closure_policy = thresholds.minimal_contract_closure_active(cycle)
    paths = candidate_evidence_paths(cycle, root=evidence_dir)
    runtime = _load_json(paths["status"]) or {}
    manifest, bundle, artifact_error = _load_audit_artifact_pair(paths, cycle)
    manifest_items = list(manifest.get("candidates") or []) if manifest else []
    bundle_items = list(bundle.get("items") or []) if bundle else []
    bundle_by_symbol = {
        str(item.get("symbol") or "").upper(): item
        for item in bundle_items if isinstance(item, dict)
    }
    raw = analysis.get("raw") if isinstance(analysis, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    legacy = raw.get("candidates_deep_dived")
    legacy = legacy if isinstance(legacy, list) else []
    parity_denominator = 0
    parity_numerator = 0
    for item in legacy:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("instId") or item.get("symbol") or "").upper()
        evidence_hash = str(item.get("evidence_hash") or "").lower()
        if not symbol or not _HASH_RE.fullmatch(evidence_hash):
            continue
        parity_denominator += 1
        if (bundle_by_symbol.get(symbol) or {}).get("evidence_hash") == evidence_hash:
            parity_numerator += 1
    coverage = raw.get("candidate_coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    strict_pass, elapsed, failure_kind = _strict_sla(cycle, status_dir)
    manifest_side_neutral = bool(
        manifest is not None
        and manifest.get("schema") == "briefing_candidate_manifest_v3_side_neutral"
        and all(
            isinstance(item, dict)
            and item.get("side") is None
            and item.get("eligible_sides") == ["long", "short"]
            and item.get("layer") == "all_market"
            for item in manifest_items
        )
    )
    bundle_side_neutral = bool(
        bundle is not None
        and bundle.get("artifact_type")
        == "candidate_review_bundle_v3_side_neutral"
        and bundle.get("timeframe_judgment_used") is False
        and all(
            isinstance(item, dict)
            and item.get("side") is None
            and item.get("eligible_sides") == ["long", "short"]
            and item.get("timeframe_judgment_used") is False
            for item in bundle_items
        )
    )
    retired_keys = {
        "timeframes", "evidence_contract", "multitimeframe_analysis",
        "direction_evidence", "opposing_evidence", "execution_conditions",
        "invalidation_point", "risk_reward", "portfolio_impact",
    }

    def contains_retired_key(value: Any) -> bool:
        if isinstance(value, dict):
            return bool(retired_keys.intersection(value)) or any(
                contains_retired_key(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_retired_key(item) for item in value)
        return False

    review_slice_consistent = bool(
        manifest is not None
        and bundle is not None
        and bundle.get("manifest_count") == manifest.get("candidate_count")
        and bundle.get("screened_count") == manifest.get("candidate_count")
        and bundle.get("candidate_count") == manifest.get("review_slice_count")
        and bundle.get("decision_slice_count")
        == manifest.get("review_slice_count")
    )
    handoff = _load_json(
        SCRIPT_DIR.parent / "tmp"
        / f"live_input_handoff_{cycle.replace(':', '-')}.json")
    closure_contract = bool(
        not closure_policy
        or (
            manifest is not None
            and manifest.get("identity_contract")
            == "symbol_review_v2_full_manifest_no_identity_gate"
            and isinstance(manifest.get("review_symbols"), list)
            and "review_candidate_ids" not in manifest
            and all("candidate_id" not in item for item in manifest_items)
            and bundle is not None
            and bundle.get("identity_contract")
            == "symbol_review_v2_full_manifest_no_identity_gate"
            and all("candidate_id" not in item for item in bundle_items)
            and handoff.get("status") == "ready"
        )
    )
    return {
        "cycle_id": cycle,
        "slot_minute": cycle[-2:],
        "phase": phase,
        "policy": (
            thresholds.MINIMAL_CONTRACT_CLOSURE_POLICY
            if closure_policy else
            thresholds.MINIMAL_DECISION_CONTRACT_POLICY
            if minimal_policy else "legacy_candidate_bundle_rollout"),
        "three_period_judgment_required": not minimal_policy,
        "six_field_decision_card_required": not minimal_policy,
        "runtime_status": str(runtime.get("status") or "MISSING"),
        "bundle_valid": bundle is not None,
        "artifact_error": artifact_error,
        "pool_count": len(manifest_items) if manifest is not None else None,
        "screened_count": (
            int(bundle.get("screened_count") or 0)
            if bundle is not None else 0),
        "ready_count": bundle.get("ready_count") if bundle else None,
        "decision_slice_count": (
            bundle.get("decision_slice_count") if bundle else None),
        "decision_ready_count": (
            bundle.get("decision_ready_count") if bundle else None),
        "identity_cycle_order_hash_consistent": bundle is not None,
        "manifest_side_neutral": manifest_side_neutral,
        "bundle_side_neutral": bundle_side_neutral,
        "review_slice_consistent": review_slice_consistent,
        "closure_contract": closure_contract,
        "retired_judgment_payload_absent": bool(
            manifest is not None and bundle is not None
            and not contains_retired_key(manifest)
            and not contains_retired_key(bundle)),
        "bundle_elapsed_seconds": (
            float(bundle.get("elapsed_seconds"))
            if bundle is not None
            and isinstance(bundle.get("elapsed_seconds"), (int, float))
            else None
        ),
        "bundle_wall_elapsed_seconds": (
            float(runtime.get("wall_elapsed_seconds"))
            if isinstance(runtime.get("wall_elapsed_seconds"), (int, float))
            else None
        ),
        "single_bundle_parity_numerator": parity_numerator,
        "single_bundle_parity_denominator": parity_denominator,
        "analysis_available": bool(analysis),
        "analysis_status": analysis.get("status") if isinstance(analysis, dict) else None,
        "reported_deep_dives": coverage.get("actual_count"),
        "quality_valid_deep_dives": coverage.get("quality_valid_count"),
        "dynamic_target_utilization": coverage.get("dynamic_target_utilization"),
        "rotation_required": coverage.get("rotation_required"),
        "rotation_satisfied": coverage.get("rotation_satisfied"),
        "candidate_quality_status": coverage.get("quality_status"),
        "strict_cycle_pass": strict_pass,
        "business_terminal_elapsed_seconds": elapsed,
        "live_failure_kind": failure_kind,
        "new_candidate_bundle_failure_type": bool(
            failure_kind and failure_kind.startswith("candidate_bundle_")),
    }


def _minimal_summary(rows: list[dict]) -> dict:
    """Validate the owner-approved side-neutral contract over 3/12 slots.

    This is deliberately a new epoch.  It never reuses the legacy shadow or
    consume gates (MTF parity, four-state quality, identity filtering,
    rotation, or the 96-slot maturity window).
    """
    basic_slots = int(thresholds.RELAXED_BASIC_CONFIRMATION_SLOTS)
    full_slots = int(thresholds.RELAXED_ZERO_OPEN_WATCHDOG_THRESHOLD_SLOTS)
    valid_artifacts = sum(row.get("bundle_valid") is True for row in rows)
    side_neutral = sum(
        row.get("manifest_side_neutral") is True
        and row.get("bundle_side_neutral") is True
        and row.get("review_slice_consistent") is True
        for row in rows)
    retired_absent = sum(
        row.get("retired_judgment_payload_absent") is True for row in rows)
    strict_passes = sum(row.get("strict_cycle_pass") is True for row in rows)
    closure_rows = [
        row for row in rows
        if row.get("policy") == thresholds.MINIMAL_CONTRACT_CLOSURE_POLICY]
    closure_passes = sum(
        row.get("closure_contract") is True for row in closure_rows)
    pool = sum(int(row.get("pool_count") or 0) for row in rows)
    screened = sum(int(row.get("screened_count") or 0) for row in rows)
    gates = {
        "artifact_integrity": _rate(valid_artifacts, len(rows)) == 1.0,
        "full_market_screening": _rate(screened, pool) == 1.0,
        "side_neutral_contract": _rate(side_neutral, len(rows)) == 1.0,
        "retired_judgment_payload_absent": (
            _rate(retired_absent, len(rows)) == 1.0),
        "strict_cycle_success": _rate(strict_passes, len(rows)) == 1.0,
    }
    if closure_rows:
        gates["closure_contract"] = (
            _rate(closure_passes, len(closure_rows)) == 1.0)
    all_pass = bool(rows) and all(gates.values())
    if rows and not all_pass:
        status = "NOT_MET"
    elif len(rows) >= full_slots:
        status = "FULL_CONFIRMED"
    elif len(rows) >= basic_slots:
        status = "BASIC_CONFIRMED"
    else:
        status = "PENDING_FORWARD_EVIDENCE"
    return {
        "policy": (
            thresholds.MINIMAL_CONTRACT_CLOSURE_POLICY
            if closure_rows else thresholds.MINIMAL_DECISION_CONTRACT_POLICY),
        "planned_mature_slots": len(rows),
        "basic_confirmation_slots": basic_slots,
        "full_confirmation_slots": full_slots,
        "artifact_integrity": {
            "numerator": valid_artifacts, "denominator": len(rows),
            "rate": _rate(valid_artifacts, len(rows)),
        },
        "full_market_screening": {
            "numerator": screened, "denominator": pool,
            "rate": _rate(screened, pool),
        },
        "side_neutral_contract": {
            "numerator": side_neutral, "denominator": len(rows),
            "rate": _rate(side_neutral, len(rows)),
        },
        "retired_judgment_payload_absent": {
            "numerator": retired_absent, "denominator": len(rows),
            "rate": _rate(retired_absent, len(rows)),
        },
        "strict_cycle_success": {
            "numerator": strict_passes, "denominator": len(rows),
            "rate": _rate(strict_passes, len(rows)),
        },
        "closure_contract": {
            "numerator": closure_passes,
            "denominator": len(closure_rows),
            "rate": _rate(closure_passes, len(closure_rows)),
        },
        "gates": gates,
        "status": status,
        "rollback_required": False,
        "legacy_mtf_state_card_gates_applied": False,
        "historical_rejudgement": False,
        "zero_denominator_is_green": False,
    }


def _shadow_summary(rows: list[dict]) -> dict:
    pool = sum(int(row.get("pool_count") or 0) for row in rows)
    screened = sum(int(row.get("screened_count") or 0) for row in rows)
    identity_passes = sum(
        row.get("identity_cycle_order_hash_consistent") is True for row in rows)
    parity_numerator = sum(
        int(row.get("single_bundle_parity_numerator") or 0) for row in rows)
    parity_denominator = sum(
        int(row.get("single_bundle_parity_denominator") or 0) for row in rows)
    bundle_elapsed = [
        row["bundle_elapsed_seconds"] for row in rows
        if isinstance(row.get("bundle_elapsed_seconds"), (int, float))]
    wall_elapsed = [
        row["bundle_wall_elapsed_seconds"] for row in rows
        if isinstance(row.get("bundle_wall_elapsed_seconds"), (int, float))]
    screening_rate = _rate(screened, pool)
    identity_rate = _rate(identity_passes, len(rows))
    parity_rate = _rate(parity_numerator, parity_denominator)
    bundle_p90 = _percentile(bundle_elapsed, 0.90)
    hard_timeout_ok = (
        len(wall_elapsed) == len(rows)
        and all(
            value <= thresholds.candidate_bundle_timeout_seconds(
                rows[index]["cycle_id"])
            for index, value in enumerate(wall_elapsed))
    )
    gates = {
        "minimum_slots": len(rows) >= thresholds.CANDIDATE_BUNDLE_SHADOW_MINIMUM_SLOTS,
        "screening_coverage": screening_rate == thresholds.CANDIDATE_SCREENING_TARGET_RATE,
        "contract_parity": parity_rate == thresholds.CANDIDATE_BUNDLE_CONTRACT_PARITY_TARGET_RATE,
        "identity_cycle_order_hash": identity_rate == 1.0,
        "bundle_p90": (
            bundle_p90 is not None
            and bundle_p90 <= thresholds.CANDIDATE_BUNDLE_P90_TARGET_SECONDS),
        "hard_timeout": hard_timeout_ok,
    }
    return {
        "planned_mature_slots": len(rows),
        "minimum_slots": thresholds.CANDIDATE_BUNDLE_SHADOW_MINIMUM_SLOTS,
        "screening": {
            "numerator": screened, "denominator": pool, "rate": screening_rate},
        "contract_parity": {
            "numerator": parity_numerator,
            "denominator": parity_denominator,
            "rate": parity_rate,
        },
        "identity_cycle_order_hash": {
            "numerator": identity_passes,
            "denominator": len(rows),
            "rate": identity_rate,
        },
        "bundle_elapsed": {
            "observations": len(bundle_elapsed),
            "p90_seconds": bundle_p90,
            "target_max_seconds": thresholds.CANDIDATE_BUNDLE_P90_TARGET_SECONDS,
            "wall_hard_timeout_seconds": thresholds.CANDIDATE_BUNDLE_TIMEOUT_SECONDS,
            "hard_timeout_observations": len(wall_elapsed),
        },
        "gates": gates,
        "status": (
            "READY_FOR_CONSUME" if gates and all(gates.values())
            else "NOT_MET" if len(rows) >= thresholds.CANDIDATE_BUNDLE_SHADOW_MINIMUM_SLOTS
            else "PENDING_FORWARD_EVIDENCE"
        ),
        "zero_denominator_is_green": False,
    }


def _consume_summary(rows: list[dict]) -> dict:
    healthy = [row for row in rows if row.get("bundle_valid") is True]
    pool = sum(int(row.get("pool_count") or 0) for row in healthy)
    screened = sum(int(row.get("screened_count") or 0) for row in healthy)
    actual = sum(
        int(row.get("reported_deep_dives") or 0) for row in rows
        if isinstance(row.get("reported_deep_dives"), int))
    valid = sum(
        int(row.get("quality_valid_deep_dives") or 0) for row in rows
        if isinstance(row.get("quality_valid_deep_dives"), int))
    utilizations = [
        float(row["dynamic_target_utilization"]) for row in rows
        if isinstance(row.get("dynamic_target_utilization"), (int, float))]
    rotation_rows = [row for row in rows if row.get("rotation_required") is True]
    rotation_passes = sum(
        row.get("rotation_satisfied") is True for row in rotation_rows)
    strict_passes = sum(row.get("strict_cycle_pass") is True for row in rows)
    new_failures = sum(
        row.get("new_candidate_bundle_failure_type") is True for row in rows)
    by_slot = {}
    slot_p90_ok = True
    for minute, cap in thresholds.CANDIDATE_CONSUME_SLOT_P90_MAX_SECONDS.items():
        values = [
            row["business_terminal_elapsed_seconds"] for row in rows
            if row.get("slot_minute") == minute
            and isinstance(row.get("business_terminal_elapsed_seconds"), int)]
        p90 = _percentile(values, 0.90)
        passed = p90 is not None and p90 <= cap
        slot_p90_ok = slot_p90_ok and passed
        by_slot[f":{minute}"] = {
            "observations": len(values), "p90_seconds": p90,
            "maximum_seconds": cap, "passed": passed,
        }
    screening_rate = _rate(screened, pool)
    valid_rate = _rate(valid, actual)
    utilization = _average(utilizations)
    rotation_rate = _rate(rotation_passes, len(rotation_rows))
    strict_rate = _rate(strict_passes, len(rows))
    gates = {
        "minimum_slots": len(rows) >= thresholds.CANDIDATE_BUNDLE_CONSUME_MINIMUM_SLOTS,
        "healthy_slot_screening": screening_rate == 1.0,
        "structure_valid_rate": (
            valid_rate is not None
            and valid_rate >= thresholds.CANDIDATE_STRUCTURE_VALID_TARGET_RATE),
        "dynamic_target_utilization": (
            utilization is not None
            and utilization >= thresholds.CANDIDATE_DYNAMIC_TARGET_UTILIZATION_TARGET_RATE),
        "rotation_compliance": (
            rotation_rate is not None
            and rotation_rate >= thresholds.CANDIDATE_ROTATION_COMPLIANCE_TARGET_RATE),
        "strict_cycle_pass_rate": (
            strict_rate is not None
            and strict_rate >= thresholds.CANDIDATE_CONSUME_STRICT_CYCLE_PASS_TARGET_RATE),
        "no_new_candidate_bundle_failure_type": new_failures == 0,
        "slot_business_terminal_p90": slot_p90_ok,
    }
    mature = gates["minimum_slots"]
    rollback_required = (
        new_failures > 0 or (mature and not all(gates.values())))
    return {
        "planned_mature_slots": len(rows),
        "minimum_slots": thresholds.CANDIDATE_BUNDLE_CONSUME_MINIMUM_SLOTS,
        "healthy_slot_screening": {
            "healthy_slots": len(healthy),
            "numerator": screened, "denominator": pool, "rate": screening_rate},
        "structure_valid_deep_dives": {
            "numerator": valid, "denominator": actual, "rate": valid_rate},
        "dynamic_target_utilization": {
            "observations": len(utilizations), "average": utilization},
        "rotation_compliance": {
            "numerator": rotation_passes,
            "denominator": len(rotation_rows), "rate": rotation_rate},
        "strict_cycle_success": {
            "numerator": strict_passes,
            "denominator": len(rows), "rate": strict_rate},
        "new_candidate_bundle_failure_type_count": new_failures,
        "slot_business_terminal_p90": by_slot,
        "gates": gates,
        "status": (
            "ROLLBACK_REQUIRED" if rollback_required
            else "MET" if mature and all(gates.values())
            else "PENDING_FORWARD_EVIDENCE"
        ),
        "rollback_required": rollback_required,
        "rollback_boundary_semantics": (
            "register the next natural :00 as consume_end in the threshold "
            "authority; do not delete failed cycles or bundle artifacts"
        ),
        "zero_denominator_is_green": False,
    }


def audit_candidate_bundle_rollout(
    *,
    as_of: datetime,
    evidence_dir: Path = CANDIDATE_EVIDENCE_DIR,
    analysis_db: Path = DEFAULT_ANALYSIS_DB,
    status_dir: Path = DEFAULT_STATUS_DIR,
    finality_seconds: int = 900,
    shadow_activation_cst: str | None = None,
    consume_activation_cst: str | None = None,
) -> dict:
    shadow_text = (
        shadow_activation_cst
        if shadow_activation_cst is not None
        else thresholds.CANDIDATE_BUNDLE_SHADOW_ACTIVATION_CST)
    consume_text = (
        consume_activation_cst
        if consume_activation_cst is not None
        else thresholds.CANDIDATE_BUNDLE_CONSUME_ACTIVATION_CST)
    minimal_text = thresholds.MINIMAL_DECISION_CONTRACT_ACTIVATION_CST
    closure_text = thresholds.MINIMAL_CONTRACT_CLOSURE_ACTIVATION_CST
    registration = thresholds.candidate_bundle_registration_facts(as_of)
    if shadow_text is None:
        return {
            "ok": True,
            "schema_version": 1,
            "status": "UNREGISTERED",
            "as_of_cst": as_of.astimezone(CST).isoformat(),
            "registration": registration,
            "shadow": _shadow_summary([]),
            "consume": {"status": "NOT_ACTIVATED", "rollback_required": False},
            "minimal_policy": {
                "status": "NOT_ACTIVATED", "rollback_required": False},
            "closure_policy": {
                "status": "NOT_ACTIVATED", "rollback_required": False},
            "cycles": [],
            "safety": _safety(),
        }
    shadow_start = thresholds.parse_cst(shadow_text)
    consume_start = thresholds.parse_cst(consume_text) if consume_text else None
    minimal_start = (
        thresholds.parse_cst(str(minimal_text)) if minimal_text else None)
    closure_start = (
        thresholds.parse_cst(str(closure_text)) if closure_text else None)
    legacy_end = min(as_of, minimal_start) if minimal_start else as_of
    shadow_end = min(legacy_end, consume_start) if consume_start else legacy_end
    shadow_cycles = _planned_cycles(
        shadow_start, shadow_end, finality_seconds=finality_seconds)
    registered_consume_end = (
        thresholds.parse_cst(thresholds.CANDIDATE_BUNDLE_CONSUME_END_CST)
        if thresholds.CANDIDATE_BUNDLE_CONSUME_END_CST else as_of)
    consume_end = min(registered_consume_end, legacy_end)
    consume_cycles = (
        _planned_cycles(
            consume_start, consume_end,
            finality_seconds=finality_seconds)
        if consume_start else [])
    minimal_end = min(as_of, closure_start) if closure_start else as_of
    minimal_cycles = (
        _planned_cycles(
            minimal_start, minimal_end, finality_seconds=finality_seconds)
        if minimal_start and as_of >= minimal_start else [])
    closure_cycles = (
        _planned_cycles(
            closure_start, as_of, finality_seconds=finality_seconds)
        if closure_start and as_of >= closure_start else [])
    all_cycles = list(dict.fromkeys([
        *shadow_cycles, *consume_cycles, *minimal_cycles, *closure_cycles]))
    analysis = _analysis_rows(analysis_db, all_cycles)
    rows = [
        _cycle_row(
            cycle,
            phase=(
                "shadow" if cycle in set(shadow_cycles)
                else "consume" if cycle in set(consume_cycles)
                else "closure" if cycle in set(closure_cycles)
                else "minimal"),
            evidence_dir=evidence_dir,
            analysis=analysis.get(cycle, {}),
            status_dir=status_dir,
        )
        for cycle in all_cycles
    ]
    shadow_rows = [row for row in rows if row["phase"] == "shadow"]
    consume_rows = [row for row in rows if row["phase"] == "consume"]
    minimal_rows = [row for row in rows if row["phase"] == "minimal"]
    closure_rows = [row for row in rows if row["phase"] == "closure"]
    shadow = _shadow_summary(shadow_rows)
    consume = (
        _consume_summary(consume_rows)
        if consume_start else {"status": "NOT_ACTIVATED", "rollback_required": False}
    )
    minimal = (
        _minimal_summary(minimal_rows)
        if minimal_start else {
            "status": "NOT_ACTIVATED", "rollback_required": False})
    closure = (
        _minimal_summary(closure_rows)
        if closure_start else {
            "status": "NOT_ACTIVATED", "rollback_required": False})
    return {
        "ok": True,
        "schema_version": 1,
        "status": (
            closure.get("status") if closure_start
            else minimal.get("status") if minimal_start
            else consume.get("status") if consume_start else shadow["status"]),
        "as_of_cst": as_of.astimezone(CST).isoformat(),
        "registration": registration,
        "effective_boundaries": {
            "shadow_activation_cst": shadow_text,
            "consume_activation_cst": consume_text,
            "consume_end_cst": thresholds.CANDIDATE_BUNDLE_CONSUME_END_CST,
            "legacy_contract_end_exclusive_cst": minimal_text,
            "minimal_policy_activation_cst": minimal_text,
            "closure_policy_activation_cst": closure_text,
            "historical_rejudgement": False,
        },
        "shadow": shadow,
        "consume": consume,
        "minimal_policy": minimal,
        "closure_policy": closure,
        "failure_types": dict(sorted(Counter(
            str(row.get("live_failure_kind") or "none") for row in rows).items())),
        "cycles": rows,
        "safety": _safety(),
    }


def _safety() -> dict:
    return {
        "business_databases_read_only": True,
        "network_calls": 0,
        "orders": 0,
        "dispatches": 0,
        "production_database_writes": 0,
        "scheduler_changes": 0,
        "model_or_provider_changes": 0,
        "vpn_scope": "excluded",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of")
    parser.add_argument("--evidence-dir", type=Path, default=CANDIDATE_EVIDENCE_DIR)
    parser.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    parser.add_argument("--status-dir", type=Path, default=DEFAULT_STATUS_DIR)
    parser.add_argument("--finality-seconds", type=int, default=900)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    args = parser.parse_args(argv)
    try:
        result = audit_candidate_bundle_rollout(
            as_of=(thresholds.parse_cst(args.as_of)
                   if args.as_of else datetime.now(CST)),
            evidence_dir=args.evidence_dir,
            analysis_db=args.analysis_db,
            status_dir=args.status_dir,
            finality_seconds=args.finality_seconds,
        )
        _atomic_json(args.json_out, result)
        print(json.dumps({
            "ok": True,
            "status": result.get("status"),
            "shadow_status": (result.get("shadow") or {}).get("status"),
            "consume_status": (result.get("consume") or {}).get("status"),
            "minimal_policy_status": (
                result.get("minimal_policy") or {}).get("status"),
            "closure_policy_status": (
                result.get("closure_policy") or {}).get("status"),
            "json_out": str(args.json_out),
            "production_database_writes": 0,
            "orders": 0,
        }, ensure_ascii=False))
        # The owner-approved minimal epoch has no automatic rollback authority.
        # Only a still-active legacy consume epoch can request its registered
        # rollback; once the minimal boundary exists that old gate is frozen.
        legacy_rollback = (
            thresholds.MINIMAL_DECISION_CONTRACT_ACTIVATION_CST is None
            and (result.get("consume") or {}).get("rollback_required") is True)
        return 1 if legacy_rollback else 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
        }, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
