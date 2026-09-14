# -*- coding: utf-8 -*-
"""Build read-only 15m/1H/4H evidence for OPEN or position-exit review.

Single-symbol mode preserves the OPEN evidence contract.  ``--facts-file``
adds a batch position-review mode: one local call reads every current position,
its original open plan when uniquely attributable, observed peak UPL, and exact
closed 15m/1H/4H evidence.  Since 2026-08-17 it also emits a soft
``protection_floor`` per position (observed peak R vs. R locked by the live SL)
and ``giveback_pct_actionable``.  Review flags are attention prompts only; this
tool never decides, submits, or constrains a close/reduce/protection action.
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
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core import risk_validator as rv
from core.multitimeframe_gate import (
    check_multitimeframe_readiness,
    check_multitimeframe_readiness_batch,
)
from scripts.live_decision_facts import validate_facts
from scripts import _acceptance_thresholds as thresholds

# ── 软性保护地板（2026-08-17 亏损复盘整改）────────────────────────────
# 背景：CRV 多单曾达 +3.8R 却在原始 SL 平仓 -1.2R；16 笔曾达 ≥1R 的仓 8 笔亏损收场。
# 这里只按「已观察峰值 R」与「当前 live SL 锁定的 R」算出地板与缺口并打标记，
# 供 Agent 逐仓复核（ADJUST_PROTECTION 或写明理由）；不下单、不改库、不阻断任何动作。
PROTECTION_FLOOR_L1_PEAK_R = 1.0          # 峰值 ≥1R：SL 至少锁到保本 + 手续费/滑点缓冲
PROTECTION_FLOOR_L2_PEAK_R = 2.0          # 峰值 ≥2R：SL 至少锁到 max(1R, 峰值×50%)
PROTECTION_FLOOR_L2_MIN_LOCK_R = 1.0
PROTECTION_FLOOR_L2_LOCK_FRACTION = 0.5
# 保本缓冲与风控闸同源：taker 双边手续费 + 入场/触发滑点预算（core/risk_validator.py）
PROTECTION_FLOOR_BREAKEVEN_BUFFER_PCT = (
    rv.RISK_FEE_BUFFER_PCT + rv.RISK_SLIPPAGE_BUFFER_PCT)
PROTECTION_FLOOR_POLICY = "soft_review_flag_not_a_gate"
FACTS_FILE_WAIT_SECONDS = 30.0
FACTS_FILE_POLL_SECONDS = 0.10
CANDIDATE_MANIFEST_SCHEMA = "briefing_candidate_manifest_v1"
CANDIDATE_MANIFEST_SCHEMA_V2 = "briefing_candidate_manifest_v2"
CANDIDATE_MANIFEST_SCHEMA_V3 = "briefing_candidate_manifest_v3_side_neutral"
CANDIDATE_BUNDLE_SCHEMA = "candidate_evidence_bundle_v1"
CANDIDATE_BUNDLE_SCHEMA_V2 = "candidate_evidence_bundle_v2"
CANDIDATE_BUNDLE_SCHEMA_V3 = "candidate_review_bundle_v3_side_neutral"
READY_POOL_SCHEMA = "briefing_ready_pool_v1"
READY_POOL_SCHEMA_SIDE_NEUTRAL = "briefing_ready_pool_v2_side_neutral"
OPPORTUNITY_STATE_VERSION = "opportunity_state_v2"
POSITION_EXIT_DECISION_VIEW_SCHEMA = "position_exit_decision_view_v1"
POSITION_EXIT_DECISION_VIEW_SCHEMA_V2 = (
    "position_exit_decision_view_v2_no_timeframes")
SUPPORTED_OPPORTUNITY_STATE_VERSIONS = {
    "opportunity_state_v1", "opportunity_state_v2",
    "opportunity_state_v3_all_timeframes", "side_neutral_review_v1"}
CANDIDATE_BUNDLE_MAXIMUM = 16
CANDIDATE_EVIDENCE_DIR = Path(os.environ.get(
    "OKX_CANDIDATE_EVIDENCE_DIR", _public_project_path('logs', 'candidate-evidence')))
_CANDIDATE_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*-USDT-SWAP$")
_CANDIDATE_ID_RE = re.compile(r"^cand_[0-9a-f]{20}$")
_OPPORTUNITY_ID_RE = re.compile(r"^opp_[0-9a-f]{20}$")
CST = timezone(timedelta(hours=8))


def _atomic_json(path: Path, payload: dict, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
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
            temporary = Path(handle.name)
            json.dump(
                payload, handle, ensure_ascii=False,
                indent=None if compact else 2,
                separators=(",", ":") if compact else None,
                allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def build_position_exit_decision_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a compact, hash-bound view for prompt-time position decisions."""
    minimal_policy = payload.get("timeframe_judgment_used") is False
    rows: list[dict[str, Any]] = []
    for item in payload.get("positions") or []:
        if not isinstance(item, dict):
            continue
        current = item.get("current") \
            if isinstance(item.get("current"), dict) else {}
        path = item.get("open_plan_and_path") \
            if isinstance(item.get("open_plan_and_path"), dict) else {}
        floor = path.get("protection_floor") \
            if isinstance(path.get("protection_floor"), dict) else {}
        contract = item.get("evidence_contract") \
            if isinstance(item.get("evidence_contract"), dict) else {}
        timeframes = contract.get("timeframes") \
            if isinstance(contract.get("timeframes"), dict) else {}
        compact_timeframes: dict[str, Any] = {}
        for timeframe in ("15m", "1H", "4H"):
            frame = timeframes.get(timeframe) \
                if isinstance(timeframes.get(timeframe), dict) else {}
            values = frame.get("values") \
                if isinstance(frame.get("values"), dict) else {}
            proof = frame.get("closed_bar_proof") \
                if isinstance(frame.get("closed_bar_proof"), dict) else {}
            confirmation = proof.get("ws_confirmation") \
                if isinstance(proof.get("ws_confirmation"), dict) else {}
            close = _number(values.get("c"))
            ma20 = _number(values.get("ma20"))
            macd_hist = _number(values.get("macd_hist"))
            compact_timeframes[timeframe] = {
                "ready": frame.get("ready") is True,
                "bar_ts": frame.get("observed_bar_ts"),
                "closed_bar_proven": proof.get("proven") is True,
                "source": confirmation.get("source"),
                "open": _number(values.get("o")),
                "high": _number(values.get("h")),
                "low": _number(values.get("l")),
                "close": close,
                "volume": _number(values.get("v")),
                "ma5": _number(values.get("ma5")),
                "ma20": ma20,
                "atr14": _number(values.get("atr14")),
                "price_vs_ma20": (
                    "above" if close is not None and ma20 is not None and close > ma20
                    else "below" if close is not None and ma20 is not None and close < ma20
                    else "equal" if close is not None and ma20 is not None
                    else "unknown"),
                "rsi14": _number(values.get("rsi14")),
                "macd_hist": macd_hist,
                "macd_sign": (
                    "positive" if macd_hist is not None and macd_hist > 0
                    else "negative" if macd_hist is not None and macd_hist < 0
                    else "zero" if macd_hist is not None else "unknown"),
            }
        row = {
            "symbol": item.get("symbol"),
            "side": item.get("side"),
            "current": {
                key: current.get(key) for key in (
                    "avgPx", "markPx", "position_age_hours", "upl",
                    "upl_pct_initial_margin", "signed_price_return_pct_from_entry",
                    "pnl_at_stop_from_entry_usdt", "secured_profit_at_stop_usdt",
                    "profit_retention_at_stop_pct_of_current_upl",
                    "giveback_to_stop_pct_of_current_upl")
            },
            "path": {
                "status": path.get("status"),
                "reason": path.get("reason"),
                "review_flags": path.get("review_flags") or [],
                "fill_px": path.get("fill_px"),
                "initial_sl": path.get("initial_sl"),
                "original_target": path.get("original_target"),
                "original_exit_mode": path.get("original_exit_mode"),
                "target_semantics": path.get("target_semantics"),
                "target_reached": path.get("target_reached"),
                "remaining_sz": path.get("remaining_sz"),
                "path_basis": path.get("path_basis"),
                "initial_risk_current_size_usdt": path.get(
                    "initial_risk_current_size_usdt"),
                "current_r_gross": path.get("current_r_gross"),
                "observed_peak_upl_usdt": path.get("observed_peak_upl_usdt"),
                "observed_peak_upl_at": path.get("observed_peak_upl_at"),
                "observed_peak_r_gross": path.get("observed_peak_r_gross"),
                "giveback_from_observed_peak_usdt": path.get(
                    "giveback_from_observed_peak_usdt"),
                "giveback_from_observed_peak_pct": path.get(
                    "giveback_from_observed_peak_pct"),
                "giveback_pct_actionable": path.get("giveback_pct_actionable"),
                "protection_floor": {
                    key: floor.get(key) for key in (
                        "status", "level", "breach", "required_protected_r",
                        "protected_r_at_current_sl_gross", "shortfall_r",
                        "current_live_sl_px", "suggested_min_sl_px",
                        "live_sl_verified", "reason")
                },
            },
            "gaps": item.get("gaps") or [],
            "error": item.get("error"),
            "timeframe_judgment_used": False if minimal_policy else True,
        }
        if not minimal_policy:
            row.update({
                "multitimeframe_ready": item.get("multitimeframe_ready") is True,
                "multitimeframe_status": item.get("multitimeframe_status"),
                "timeframes": compact_timeframes,
            })
        rows.append(row)
    execution_context: dict[str, Any] = {}
    policy = payload.get("action_policy")
    if isinstance(policy, dict):
        execution_context = {
            "action_policy": {
                "position_truth_verified": policy.get("position_truth_verified"),
                "allowed_executor_actions": list(policy.get("allowed_executor_actions") or []),
                "open_add_allowed_by_facts": policy.get("open_add_allowed_by_facts"),
            },
            "account_risk_context": dict(payload.get("account_risk_context") or {}),
            "execution_instruction": (
                "Position plan actions must be explicitly allowed by action_policy. "
                "When open_add_allowed_by_facts=false, omit OPEN/ADD from the execution "
                "plan; retain only permitted position decisions or HOLD."
            ),
        }
    view: dict[str, Any] = {
        "schema": (
            POSITION_EXIT_DECISION_VIEW_SCHEMA_V2
            if minimal_policy else POSITION_EXIT_DECISION_VIEW_SCHEMA),
        "mode": "read_only_compact_projection",
        "cycle_id": payload.get("cycle_id"),
        "facts_hash": payload.get("facts_hash"),
        "source_evidence_hash": payload.get("evidence_hash"),
        "source_status": payload.get("status"),
        **execution_context,
        "position_count": payload.get("position_count"),
        "protection_floor_breach_count": payload.get(
            "protection_floor_breach_count"),
        "protection_floor_breaches": payload.get(
            "protection_floor_breaches") or [],
        "review_policy": payload.get("review_policy"),
        "positions": rows,
        "production_database_writes": 0,
        "orders_placed": 0,
        "decision_authority": False,
        "runner_authority": False,
        "timeframe_judgment_used": False if minimal_policy else True,
    }
    view["view_hash"] = hashlib.sha256(
        json.dumps(view, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return view


def _current_natural_cycle() -> str:
    now = datetime.now(CST)
    return now.replace(
        minute=(now.minute // 15) * 15, second=0, microsecond=0,
    ).strftime("%Y-%m-%dT%H:%M")


def _production_candidate_output(path: Path) -> bool:
    try:
        return path.resolve().parent == CANDIDATE_EVIDENCE_DIR.resolve()
    except OSError:
        return False


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("facts file must contain one JSON object")
    return payload


def _canonical_sha256(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def load_ready_pool_artifact(path: Path, cycle_id: str) -> dict:
    payload = _load_json(path)
    supplied_hash = str(payload.get("ready_pool_sha256") or "")
    core = dict(payload)
    core.pop("ready_pool_sha256", None)
    if payload.get("schema") not in {
            READY_POOL_SCHEMA, READY_POOL_SCHEMA_SIDE_NEUTRAL}:
        raise ValueError("ready pool schema mismatch")
    if str(payload.get("cycle_id") or "") != str(cycle_id):
        raise ValueError("ready pool cycle mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
        raise ValueError("ready pool sha256 is missing or invalid")
    if supplied_hash != _canonical_sha256(core):
        raise ValueError("ready pool sha256 mismatch")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("ready pool items must be a list")
    if payload.get("ready_count") != len(items):
        raise ValueError("ready pool count mismatch")
    selected = [
        item for item in items
        if isinstance(item, dict) and item.get("selected_for_manifest") is True
    ]
    if payload.get("selected_count") != len(selected):
        raise ValueError("ready pool selected count mismatch")
    seen_symbols: set[str] = set()
    for rank, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"ready pool item[{rank}] must be an object")
        if item.get("ready_pool_rank") != rank:
            raise ValueError(f"ready pool item[{rank}] rank mismatch")
        symbol = str(item.get("symbol") or "").strip().upper()
        if not _CANDIDATE_SYMBOL_RE.fullmatch(symbol) or symbol in seen_symbols:
            raise ValueError(f"ready pool item[{rank}] symbol invalid or duplicated")
        seen_symbols.add(symbol)
    return payload


def candidate_evidence_paths(
    cycle_id: str,
    *,
    root: Path | None = None,
) -> dict[str, Path]:
    safe_cycle = str(cycle_id).replace(":", "-")
    directory = Path(root) if root is not None else CANDIDATE_EVIDENCE_DIR
    return {
        "manifest": directory / f"briefing-candidates-{safe_cycle}.json",
        "ready_pool": directory / f"briefing-ready-pool-{safe_cycle}.json",
        "bundle": directory / f"candidate-bundle-{safe_cycle}.json",
        "decision_slice": directory / f"candidate-decision-slice-{safe_cycle}.json",
        "status": directory / f"candidate-bundle-status-{safe_cycle}.json",
    }


def load_candidate_manifest(path: Path, cycle_id: str) -> tuple[dict, str]:
    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidate manifest must be one JSON object")
    supplied_hash = str(payload.get("manifest_sha256") or "")
    core = dict(payload)
    core.pop("manifest_sha256", None)
    expected_hash = _canonical_sha256(core)
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
        raise ValueError("candidate manifest sha256 is missing or invalid")
    if supplied_hash != expected_hash:
        raise ValueError("candidate manifest sha256 mismatch")
    if payload.get("schema") not in {
            CANDIDATE_MANIFEST_SCHEMA, CANDIDATE_MANIFEST_SCHEMA_V2,
            CANDIDATE_MANIFEST_SCHEMA_V3}:
        raise ValueError("candidate manifest schema mismatch")
    if str(payload.get("cycle_id") or "") != str(cycle_id):
        raise ValueError("candidate manifest cycle mismatch")
    identity_contract = payload.get("identity_contract")
    closure_no_identity = bool(
        identity_contract == "symbol_review_v2_full_manifest_no_identity_gate"
        and thresholds.minimal_contract_closure_active(cycle_id)
    )
    if identity_contract not in {
            None, "candidate_id_v1_exact_manifest_no_alias",
            "symbol_review_v1_side_selected_by_agent",
            "symbol_review_v2_full_manifest_no_identity_gate"}:
        raise ValueError("candidate manifest identity contract mismatch")
    if (
        identity_contract == "symbol_review_v2_full_manifest_no_identity_gate"
        and not closure_no_identity
    ):
        raise ValueError("candidate manifest closure identity contract inactive")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidate manifest candidates must be a list")
    maximum = thresholds.candidate_manifest_maximum(cycle_id)
    if len(candidates) > maximum:
        raise ValueError(
            f"candidate manifest exceeds {maximum} items")
    if int(payload.get("candidate_count") or 0) != len(candidates):
        raise ValueError("candidate manifest count mismatch")
    review_symbols: list[str] | None = None
    if payload.get("schema") in {
            CANDIDATE_MANIFEST_SCHEMA_V2, CANDIDATE_MANIFEST_SCHEMA_V3}:
        if closure_no_identity:
            raw_review_symbols = payload.get("review_symbols")
            if not isinstance(raw_review_symbols, list):
                raise ValueError("candidate manifest review_symbols invalid")
            review_symbols = [
                str(symbol or "").strip().upper()
                for symbol in raw_review_symbols
            ]
            if (
                payload.get("review_candidate_ids") is not None
                and payload.get("review_candidate_ids") != []
            ):
                raise ValueError(
                    "closure manifest must not carry review_candidate_ids")
            review_refs = review_symbols
        else:
            review_ids = payload.get("review_candidate_ids")
            if not isinstance(review_ids, list):
                raise ValueError(
                    "candidate manifest review_candidate_ids invalid")
            review_refs = review_ids
        if int(payload.get("review_slice_count") or 0) != len(review_refs):
            raise ValueError("candidate manifest review slice count mismatch")
        if len(review_refs) > thresholds.RELAXED_DECISION_SLICE_MAXIMUM:
            raise ValueError("candidate manifest review slice exceeds deep limit")
    seen: set[str] = set()
    seen_ids: set[str] = set()
    id_contract = False if closure_no_identity else (
        bool(identity_contract) or any(
            isinstance(item, dict) and item.get("candidate_id") is not None
            for item in candidates
        )
    )
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            raise ValueError(f"candidate[{index}] must be an object")
        if int(candidate.get("ordinal") or 0) != index:
            raise ValueError(f"candidate[{index}] ordinal mismatch")
        symbol = str(candidate.get("symbol") or "").strip().upper()
        if not _CANDIDATE_SYMBOL_RE.fullmatch(symbol):
            raise ValueError(f"candidate[{index}] symbol invalid: {symbol!r}")
        if symbol in seen:
            raise ValueError(f"candidate symbol duplicated: {symbol}")
        seen.add(symbol)
        candidate_id = str(candidate.get("candidate_id") or "")
        if id_contract:
            if not _CANDIDATE_ID_RE.fullmatch(candidate_id):
                raise ValueError(f"candidate[{index}] candidate_id invalid")
            if candidate_id in seen_ids:
                raise ValueError(f"candidate_id duplicated: {candidate_id}")
            seen_ids.add(candidate_id)
        side_neutral = payload.get("schema") == CANDIDATE_MANIFEST_SCHEMA_V3
        if side_neutral:
            if candidate.get("side") is not None:
                raise ValueError(f"candidate[{index}] side must be null")
            if candidate.get("eligible_sides") != ["long", "short"]:
                raise ValueError(f"candidate[{index}] eligible_sides invalid")
            if candidate.get("layer") != "all_market":
                raise ValueError(f"candidate[{index}] layer invalid")
        else:
            if candidate.get("side") not in {"long", "short"}:
                raise ValueError(f"candidate[{index}] side invalid")
            if candidate.get("layer") not in {"mature", "early"}:
                raise ValueError(f"candidate[{index}] layer invalid")
        if not isinstance(candidate.get("rotation_due"), bool):
            raise ValueError(f"candidate[{index}] rotation_due invalid")
        for field in ("recent_deep_dives_6h", "recent_rejections_6h"):
            value = candidate.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"candidate[{index}] {field} invalid")
        if candidate["recent_rejections_6h"] > candidate["recent_deep_dives_6h"]:
            raise ValueError(f"candidate[{index}] rejection count exceeds dives")
        prior_hash = candidate.get("prior_evidence_hash")
        if prior_hash is not None and not re.fullmatch(
                r"[0-9a-f]{64}", str(prior_hash)):
            raise ValueError(f"candidate[{index}] prior_evidence_hash invalid")
        if id_contract and not isinstance(
                candidate.get("new_evidence_required"), bool):
            raise ValueError(
                f"candidate[{index}] new_evidence_required invalid")
    if closure_no_identity:
        assert review_symbols is not None
        if (
            len(set(review_symbols)) != len(review_symbols)
            or any(not _CANDIDATE_SYMBOL_RE.fullmatch(symbol)
                   for symbol in review_symbols)
        ):
            raise ValueError(
                "candidate manifest review_symbols invalid or duplicated")
        missing_review_symbols = [
            symbol for symbol in review_symbols if symbol not in seen
        ]
        if missing_review_symbols:
            raise ValueError(
                "candidate manifest review_symbols outside manifest:"
                + ",".join(missing_review_symbols))
    ready_pool_ref = payload.get("ready_pool")
    if ready_pool_ref is not None:
        if not isinstance(ready_pool_ref, dict):
            raise ValueError("candidate manifest ready_pool reference invalid")
        ready_pool_status = str(
            ready_pool_ref.get("status") or "PASSED").upper()
        if ready_pool_status == "ERROR":
            if not str(ready_pool_ref.get("error") or "").strip():
                raise ValueError("candidate manifest ready pool error missing")
            return payload, hashlib.sha256(raw_bytes).hexdigest()
        if ready_pool_status != "PASSED":
            raise ValueError("candidate manifest ready pool status invalid")
        ready_pool_path = Path(str(ready_pool_ref.get("path") or "")).resolve()
        expected_name = (
            "briefing-ready-pool-" + str(cycle_id).replace(":", "-") + ".json")
        if ready_pool_path.parent != path.resolve().parent:
            raise ValueError("candidate manifest ready pool directory mismatch")
        if ready_pool_path.name != expected_name:
            raise ValueError("candidate manifest ready pool filename mismatch")
        ready_pool = load_ready_pool_artifact(ready_pool_path, cycle_id)
        if ready_pool_ref.get("sha256") != ready_pool.get("ready_pool_sha256"):
            raise ValueError("candidate manifest ready pool hash mismatch")
        if ready_pool_ref.get("ready_count") != ready_pool.get("ready_count"):
            raise ValueError("candidate manifest ready pool count mismatch")
        selected = sorted(
            [item for item in ready_pool["items"]
             if item.get("selected_for_manifest") is True],
            key=lambda item: int(item.get("selected_ordinal") or 0),
        )
        if len(selected) != len(candidates):
            raise ValueError("candidate manifest ready pool selection length mismatch")
        for index, (candidate, pool_item) in enumerate(
                zip(candidates, selected, strict=True), start=1):
            if payload.get("schema") == CANDIDATE_MANIFEST_SCHEMA_V3:
                for field, pool_field in (
                    ("symbol", "symbol"),
                    ("eligible_sides", "eligible_sides"),
                    ("ready_pool_rank", "ready_pool_rank"),
                    ("rank_version", "rank_version"),
                ):
                    if candidate.get(field) != pool_item.get(pool_field):
                        raise ValueError(
                            f"candidate[{index}] ready pool {field} mismatch")
                if pool_item.get("selected_layer") != "all_market":
                    raise ValueError(
                        f"candidate[{index}] ready pool layer mismatch")
                continue
            state = str(candidate.get("opportunity_state") or "")
            side = str(candidate.get("side") or "")
            first_seen = str(candidate.get("first_seen_cycle") or "")
            state_version = str(candidate.get("state_version") or "")
            if state_version not in SUPPORTED_OPPORTUNITY_STATE_VERSIONS:
                raise ValueError(f"candidate[{index}] state version mismatch")
            identity_parts = [
                str(candidate.get("symbol") or "").upper(),
                side.lower(), first_seen,
            ]
            if state_version != "opportunity_state_v1":
                identity_parts.insert(0, state_version)
            expected_opportunity_id = "opp_" + hashlib.sha256(
                "|".join(identity_parts).encode("utf-8")).hexdigest()[:20]
            if state not in {"ENTRY_READY", "EXTENDED", "TRIGGERING", "EARLY_WATCH"}:
                raise ValueError(f"candidate[{index}] opportunity state invalid")
            if candidate.get("layer") == "mature" and state not in {
                    "ENTRY_READY", "EXTENDED"}:
                raise ValueError(f"candidate[{index}] mature/state mismatch")
            if candidate.get("layer") == "early" and state not in {
                    "TRIGGERING", "EARLY_WATCH"}:
                raise ValueError(f"candidate[{index}] early/state mismatch")
            if not _OPPORTUNITY_ID_RE.fullmatch(
                    str(candidate.get("opportunity_id") or "")):
                raise ValueError(f"candidate[{index}] opportunity_id invalid")
            if candidate.get("opportunity_id") != expected_opportunity_id:
                raise ValueError(f"candidate[{index}] opportunity_id mismatch")
            for field, pool_field in (
                ("symbol", "symbol"), ("side", "side"),
                ("layer", "selected_layer"),
                ("opportunity_id", "opportunity_id"),
                ("opportunity_state", "opportunity_state"),
                ("state_version", "state_version"),
                ("first_seen_cycle", "first_seen_cycle"),
                ("first_seen_ts_utc", "first_seen_ts_utc"),
                ("first_seen_price", "first_seen_price"),
                ("ready_pool_rank", "ready_pool_rank"),
                ("initial_invalidation", "initial_invalidation"),
                ("rank_version", "rank_version"),
            ):
                if candidate.get(field) != pool_item.get(pool_field):
                    raise ValueError(
                        f"candidate[{index}] ready pool {field} mismatch")
    return payload, hashlib.sha256(raw_bytes).hexdigest()


def resolve_manifest_candidate(
    path: Path,
    cycle_id: str,
    candidate_id: str,
) -> tuple[dict, dict]:
    """Resolve one exact candidate identity; aliases and symbol guesses are forbidden."""
    normalized_id = str(candidate_id or "").strip().lower()
    if not _CANDIDATE_ID_RE.fullmatch(normalized_id):
        raise ValueError("candidate_id is missing or invalid")
    manifest, _ = load_candidate_manifest(path, cycle_id)
    matches = [
        candidate for candidate in (manifest.get("candidates") or [])
        if isinstance(candidate, dict)
        and str(candidate.get("candidate_id") or "").strip().lower()
        == normalized_id
    ]
    if len(matches) != 1:
        raise ValueError(
            "candidate_id must resolve exactly once in the exact-cycle manifest")
    return manifest, matches[0]


def build_candidate_bound_evidence(
    db_root: Path,
    manifest_path: Path,
    candidate_id: str,
    cycle_id: str,
) -> dict[str, Any]:
    """Build single-candidate evidence from manifest identity, never free-form symbol."""
    manifest, candidate = resolve_manifest_candidate(
        manifest_path, cycle_id, candidate_id)
    symbol = str(candidate["symbol"]).strip().upper()
    result = check_multitimeframe_readiness(db_root, symbol, cycle_id)
    contract = result.get("evidence_contract")
    if isinstance(contract, dict) and contract.get("symbol") != symbol:
        raise ValueError("evidence contract symbol differs from manifest identity")
    return {
        "ok": bool(result.get("ready")),
        "status": result.get("status"),
        "scope": "exact_manifest_candidate",
        "identity_contract": "candidate_id_v1_exact_manifest_no_alias",
        "candidate_id": candidate["candidate_id"],
        "symbol": symbol,
        "side": candidate["side"],
        "layer": candidate["layer"],
        "opportunity_id": candidate.get("opportunity_id"),
        "opportunity_state": candidate.get("opportunity_state"),
        "cycle_id": str(cycle_id),
        "candidate_manifest": str(manifest_path),
        "candidate_manifest_sha256": manifest.get("manifest_sha256"),
        "evidence_contract": contract,
        "gaps": _gaps(result),
        "error": result.get("error"),
        "mode": "read_only",
        "production_database_writes": 0,
        "orders_placed": 0,
    }


def build_retired_single_candidate_receipt(
    cycle_id: str,
    *,
    symbol: str | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Return the closure-era no-op receipt for legacy single-item CLIs.

    A single symbol/id used to be an alternate admission path that rebuilt
    timeframe evidence and enforced an exact manifest identity.  The owner has
    retired both behaviours.  Keeping the CLI parseable is useful for old
    operators and rollback tooling, but it must not query market bars, resolve
    a manifest identity, or authorize a decision.
    """
    normalized_symbol = str(symbol or "").strip().upper() or None
    normalized_candidate_id = str(candidate_id or "").strip() or None
    return {
        "schema_version": 1,
        "artifact_type": "retired_single_candidate_review_v1",
        "ok": True,
        "status": "NOT_REQUIRED",
        "scope": "side_neutral_review_no_single_candidate_gate",
        "cycle_id": str(cycle_id),
        "symbol_reference": normalized_symbol,
        "candidate_id_reference": normalized_candidate_id,
        "identity_enforced": False,
        "timeframe_judgment_used": False,
        "decision_authority": False,
        "runner_authority": False,
        "mode": "read_only",
        "production_database_writes": 0,
        "orders_placed": 0,
    }


def build_candidate_evidence_bundle(
    db_root: Path,
    manifest_path: Path,
    cycle_id: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    manifest, manifest_file_sha256 = load_candidate_manifest(
        manifest_path, cycle_id)
    candidates = list(manifest.get("candidates") or [])
    if manifest.get("schema") == CANDIDATE_MANIFEST_SCHEMA_V3:
        symbol_review = bool(
            manifest.get("identity_contract")
            == "symbol_review_v2_full_manifest_no_identity_gate"
            and thresholds.minimal_contract_closure_active(cycle_id)
        )
        review_ids = set(manifest.get("review_candidate_ids") or [])
        review_symbols = list(manifest.get("review_symbols") or [])
        review_symbol_set = set(review_symbols)
        screening_index = []
        screening_by_symbol: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            item = {
                "ordinal": int(candidate["ordinal"]),
                "layer": "all_market",
                "symbol": str(candidate["symbol"]),
                "side": None,
                "eligible_sides": ["long", "short"],
                "last": candidate.get("last"),
                "chg24h": candidate.get("chg24h"),
                "rotation_due": candidate.get("rotation_due") is True,
                "ready_pool_rank": candidate.get("ready_pool_rank"),
                "rank_version": candidate.get("rank_version"),
                "ready": True,
                "status": "REVIEWABLE_NO_TIMEFRAME_JUDGMENT",
                "gaps": [],
                "error": None,
                "timeframe_judgment_used": False,
            }
            if not symbol_review:
                item["candidate_id"] = candidate.get("candidate_id")
            else:
                item["review_identity"] = "symbol"
                item["selected_for_review"] = (
                    item["symbol"] in review_symbol_set)
            item["review_hash"] = _canonical_sha256(item)
            screening_index.append(item)
            screening_by_symbol[item["symbol"]] = item
        if symbol_review:
            items = []
            for review_ordinal, symbol in enumerate(review_symbols, start=1):
                source = screening_by_symbol[str(symbol)]
                item = {
                    **source,
                    "ordinal": review_ordinal,
                    "manifest_ordinal": source["ordinal"],
                }
                item.pop("review_hash", None)
                item["review_hash"] = _canonical_sha256(item)
                items.append(item)
        else:
            items = [
                dict(item) for item in screening_index
                if item.get("candidate_id") in review_ids
            ]
        payload = {
            "schema_version": 3,
            "artifact_type": CANDIDATE_BUNDLE_SCHEMA_V3,
            "mode": "read_only",
            "scope": "briefing_candidates_exact_cycle",
            "cycle_id": str(cycle_id),
            "tick_ts": manifest.get("tick_ts"),
            "candidate_manifest": str(manifest_path),
            "candidate_manifest_sha256": manifest.get("manifest_sha256"),
            "candidate_manifest_file_sha256": manifest_file_sha256,
            "candidate_count": len(items),
            "manifest_count": len(candidates),
            "screened_count": len(screening_index),
            "ready_count": len(screening_index),
            "not_ready_count": 0,
            "decision_slice_count": len(items),
            "decision_ready_count": len(items),
            "complete_contract_count": 0,
            "screening_index": screening_index,
            "items": items,
            "elapsed_seconds": round(time.perf_counter() - started, 6),
            "production_database_writes": 0,
            "orders_placed": 0,
            "timeframe_judgment_used": False,
            "ok": True,
            "status": "PASSED",
        }
        if symbol_review:
            payload.update({
                "review_identity_contract": (
                    "symbol_review_v2_full_manifest_no_identity_gate"),
                "review_symbols": review_symbols,
            })
        payload["bundle_sha256"] = _canonical_sha256(payload)
        return payload
    results = check_multitimeframe_readiness_batch(
        db_root,
        [str(candidate["symbol"]) for candidate in candidates],
        cycle_id,
    )
    if len(results) != len(candidates):
        raise RuntimeError("candidate batch result count mismatch")
    items = []
    screening_index = []
    review_ids = set(manifest.get("review_candidate_ids") or [])
    for candidate, result in zip(candidates, results, strict=True):
        contract = result.get("evidence_contract")
        index_item = {
            "ordinal": int(candidate["ordinal"]),
            "candidate_id": candidate.get("candidate_id"),
            "layer": candidate["layer"],
            "symbol": str(candidate["symbol"]),
            "side": candidate["side"],
            "last": candidate.get("last"),
            "chg24h": candidate.get("chg24h"),
            "rotation_due": candidate.get("rotation_due") is True,
            "recent_deep_dives_6h": int(
                candidate.get("recent_deep_dives_6h") or 0),
            "recent_rejections_6h": int(
                candidate.get("recent_rejections_6h") or 0),
            "prior_evidence_hash": candidate.get("prior_evidence_hash"),
            "new_evidence_required": (
                candidate.get("new_evidence_required") is True),
            "opportunity_id": candidate.get("opportunity_id"),
            "opportunity_state": candidate.get("opportunity_state"),
            "state_version": candidate.get("state_version"),
            "first_seen_cycle": candidate.get("first_seen_cycle"),
            "first_seen_ts_utc": candidate.get("first_seen_ts_utc"),
            "first_seen_price": candidate.get("first_seen_price"),
            "state_entered_cycle": candidate.get("state_entered_cycle"),
            "previous_state": candidate.get("previous_state"),
            "state_transition": candidate.get("state_transition"),
            "regime_first_seen": candidate.get("regime_first_seen"),
            "regime_first_seen_source_ts": candidate.get(
                "regime_first_seen_source_ts"),
            "regime_current": candidate.get("regime_current"),
            "regime_current_source_ts": candidate.get(
                "regime_current_source_ts"),
            "initial_invalidation": candidate.get("initial_invalidation"),
            "current_invalidation": candidate.get("current_invalidation"),
            "trend_strength": candidate.get("trend_strength"),
            "entry_timing": candidate.get("entry_timing"),
            "ready_pool_rank": candidate.get("ready_pool_rank"),
            "rank_version": candidate.get("rank_version"),
            "ready": result.get("ready") is True,
            "status": result.get("status"),
            "gaps": _gaps(result),
            "error": result.get("error"),
            "evidence_hash": (
                str((contract or {}).get("evidence_hash") or "") or None),
        }
        screening_index.append(index_item)
        if (
            manifest.get("schema") != CANDIDATE_MANIFEST_SCHEMA_V2
            or candidate.get("candidate_id") in review_ids
        ):
            items.append({
            **index_item,
            "evidence_contract": contract,
        })
    complete_contracts = sum(
        isinstance(item.get("evidence_contract"), dict)
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(item.get("evidence_hash") or "")))
        for item in items
    )
    relaxed = manifest.get("schema") == CANDIDATE_MANIFEST_SCHEMA_V2
    bundle_ok = (
        complete_contracts == len(items)
        and (
            not relaxed
            or all(item.get("ready") is True for item in screening_index))
    )
    payload = {
        "schema_version": 2 if relaxed else 1,
        "artifact_type": (
            CANDIDATE_BUNDLE_SCHEMA_V2 if relaxed else CANDIDATE_BUNDLE_SCHEMA),
        "mode": "read_only",
        "scope": "briefing_candidates_exact_cycle",
        "cycle_id": str(cycle_id),
        "tick_ts": manifest.get("tick_ts"),
        "candidate_manifest": str(manifest_path),
        "candidate_manifest_sha256": manifest.get("manifest_sha256"),
        "candidate_manifest_file_sha256": manifest_file_sha256,
        "candidate_count": len(items),
        "manifest_count": len(candidates),
        "screened_count": len(screening_index),
        "ready_count": sum(item["ready"] for item in screening_index),
        "not_ready_count": sum(not item["ready"] for item in screening_index),
        "decision_slice_count": len(items),
        "decision_ready_count": sum(item["ready"] for item in items),
        "complete_contract_count": complete_contracts,
        "screening_index": screening_index if relaxed else None,
        "items": items,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "production_database_writes": 0,
        "orders_placed": 0,
        "ok": bundle_ok,
        "status": "PASSED" if bundle_ok else "DEGRADED",
    }
    payload["bundle_sha256"] = _canonical_sha256(payload)
    return payload


def validate_candidate_evidence_bundle(
    payload: Any,
    *,
    expected_cycle: str,
    expected_manifest_path: Path | None = None,
) -> list[str]:
    """Validate the complete batch artifact without reading business state."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["bundle must be an object"]
    supplied_hash = str(payload.get("bundle_sha256") or "")
    core = dict(payload)
    core.pop("bundle_sha256", None)
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
        errors.append("bundle sha256 missing or invalid")
    elif supplied_hash != _canonical_sha256(core):
        errors.append("bundle sha256 mismatch")
    if payload.get("artifact_type") not in {
            CANDIDATE_BUNDLE_SCHEMA, CANDIDATE_BUNDLE_SCHEMA_V2,
            CANDIDATE_BUNDLE_SCHEMA_V3}:
        errors.append("bundle artifact type mismatch")
    if payload.get("scope") != "briefing_candidates_exact_cycle":
        errors.append("bundle scope mismatch")
    if payload.get("mode") != "read_only":
        errors.append("bundle mode mismatch")
    if str(payload.get("cycle_id") or "") != str(expected_cycle):
        errors.append("bundle cycle mismatch")
    if payload.get("production_database_writes") != 0:
        errors.append("bundle database-write declaration mismatch")
    if payload.get("orders_placed") != 0:
        errors.append("bundle order declaration mismatch")
    items = payload.get("items")
    if not isinstance(items, list):
        return errors + ["bundle items must be a list"]
    side_neutral = payload.get("artifact_type") == CANDIDATE_BUNDLE_SCHEMA_V3
    symbol_review = bool(
        side_neutral
        and payload.get("review_identity_contract")
        == "symbol_review_v2_full_manifest_no_identity_gate"
        and thresholds.minimal_contract_closure_active(expected_cycle)
    )
    if (
        payload.get("review_identity_contract")
        == "symbol_review_v2_full_manifest_no_identity_gate"
        and not symbol_review
    ):
        errors.append("bundle closure symbol review contract inactive")
    relaxed = payload.get("artifact_type") in {
        CANDIDATE_BUNDLE_SCHEMA_V2, CANDIDATE_BUNDLE_SCHEMA_V3}
    maximum = (
        thresholds.RELAXED_DECISION_SLICE_MAXIMUM
        if relaxed else CANDIDATE_BUNDLE_MAXIMUM)
    if len(items) > maximum:
        errors.append("bundle decision slice maximum exceeded")
    if payload.get("candidate_count") != len(items):
        errors.append("bundle candidate count mismatch")
    screening_index = payload.get("screening_index") if relaxed else items
    if not isinstance(screening_index, list):
        errors.append("bundle screening index invalid")
        screening_index = []
    if payload.get("screened_count") != len(screening_index):
        errors.append("bundle screened count mismatch")
    ready_count = sum(
        isinstance(item, dict) and item.get("ready") is True
        for item in screening_index)
    if payload.get("ready_count") != ready_count:
        errors.append("bundle ready count mismatch")
    if payload.get("not_ready_count") != len(screening_index) - ready_count:
        errors.append("bundle not-ready count mismatch")
    if relaxed:
        if payload.get("decision_slice_count") != len(items):
            errors.append("bundle decision slice count mismatch")
        if payload.get("decision_ready_count") != sum(
                isinstance(item, dict) and item.get("ready") is True
                for item in items):
            errors.append("bundle decision ready count mismatch")
    seen: set[str] = set()
    complete_contracts = 0
    manifest_candidates: list[dict] | None = None
    manifest: dict | None = None
    manifest_id_contract = False
    if expected_manifest_path is not None:
        try:
            manifest, _ = load_candidate_manifest(
                expected_manifest_path, expected_cycle)
            manifest_candidates = list(manifest.get("candidates") or [])
            manifest_id_contract = any(
                isinstance(candidate, dict)
                and candidate.get("candidate_id") is not None
                for candidate in manifest_candidates
            )
            if payload.get("candidate_manifest_sha256") != manifest.get(
                    "manifest_sha256"):
                errors.append("bundle manifest hash mismatch")
            if Path(str(payload.get("candidate_manifest") or "")).resolve() != (
                    expected_manifest_path.resolve()):
                errors.append("bundle manifest path mismatch")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"manifest invalid:{type(exc).__name__}:{exc}")
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f"bundle item[{index}] must be an object")
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if item.get("ordinal") != index:
            errors.append(f"bundle item[{index}] ordinal mismatch")
        if not _CANDIDATE_SYMBOL_RE.fullmatch(symbol) or symbol in seen:
            errors.append(f"bundle item[{index}] symbol invalid or duplicated")
        seen.add(symbol)
        if side_neutral:
            review_hash = str(item.get("review_hash") or "")
            item_core = dict(item)
            item_core.pop("review_hash", None)
            if (
                item.get("side") is None
                and item.get("eligible_sides") == ["long", "short"]
                and item.get("timeframe_judgment_used") is False
                and review_hash == _canonical_sha256(item_core)
            ):
                pass
            else:
                errors.append(f"bundle item[{index}] side-neutral review invalid")
            if symbol_review:
                if (
                    item.get("review_identity") != "symbol"
                    or "candidate_id" in item
                    or not isinstance(item.get("manifest_ordinal"), int)
                ):
                    errors.append(
                        f"bundle item[{index}] symbol review identity invalid")
        else:
            contract = item.get("evidence_contract")
            evidence_hash = str(item.get("evidence_hash") or "")
            if (
                isinstance(contract, dict)
                and contract.get("cycle_id") == expected_cycle
                and contract.get("symbol") == symbol
                and contract.get("evidence_hash") == evidence_hash
                and re.fullmatch(r"[0-9a-f]{64}", evidence_hash)
            ):
                complete_contracts += 1
            else:
                errors.append(f"bundle item[{index}] evidence contract invalid")
        if manifest_candidates is not None:
            candidate = (
                next((row for row in manifest_candidates
                      if str(row.get("symbol") or "").strip().upper()
                      == symbol), None)
                if symbol_review else
                manifest_candidates[index - 1]
                if index <= len(manifest_candidates) else None
            )
            if candidate is None:
                errors.append(
                    f"bundle item[{index}] symbol missing from manifest")
                continue
            fields = (
                ["ordinal", "symbol", "side", "layer"]
                if side_neutral else [
                    "ordinal", "symbol", "side", "layer", "rotation_due",
                    "recent_deep_dives_6h", "recent_rejections_6h",
                ])
            if manifest_id_contract and not side_neutral:
                fields.extend((
                    "candidate_id", "prior_evidence_hash",
                    "new_evidence_required",
                    "opportunity_id", "opportunity_state",
                    "state_version", "first_seen_cycle", "first_seen_ts_utc",
                    "first_seen_price", "state_entered_cycle",
                    "previous_state", "state_transition",
                    "regime_first_seen", "regime_first_seen_source_ts",
                    "regime_current", "regime_current_source_ts",
                    "initial_invalidation",
                    "current_invalidation", "trend_strength",
                    "entry_timing", "ready_pool_rank", "rank_version",
                ))
            for field in fields:
                item_value = (
                    item.get("manifest_ordinal")
                    if symbol_review and field == "ordinal"
                    else item.get(field)
                )
                if item_value != candidate.get(field):
                    errors.append(f"bundle item[{index}] manifest {field} mismatch")
    if manifest_candidates is not None:
        expected_manifest_rows = screening_index if relaxed else items
        if len(manifest_candidates) != len(expected_manifest_rows):
            errors.append("bundle manifest candidate length mismatch")
        if symbol_review:
            expected_review_symbols = list(
                (manifest or {}).get("review_symbols") or [])
            actual_review_symbols = [
                str(item.get("symbol") or "")
                for item in items if isinstance(item, dict)
            ]
            if payload.get("review_symbols") != expected_review_symbols:
                errors.append("bundle review_symbols differ from manifest")
            if actual_review_symbols != expected_review_symbols:
                errors.append("bundle items differ from symbol review order")
            for screen_index, screen_item in enumerate(
                    screening_index, start=1):
                if not isinstance(screen_item, dict):
                    errors.append(
                        f"bundle screening[{screen_index}] must be object")
                    continue
                screen_core = dict(screen_item)
                screen_hash = str(screen_core.pop("review_hash", ""))
                expected_candidate = manifest_candidates[screen_index - 1]
                if (
                    screen_item.get("ordinal") != screen_index
                    or screen_item.get("symbol")
                    != expected_candidate.get("symbol")
                    or screen_item.get("review_identity") != "symbol"
                    or "candidate_id" in screen_item
                    or screen_hash != _canonical_sha256(screen_core)
                ):
                    errors.append(
                        f"bundle screening[{screen_index}] symbol identity invalid")
    if payload.get("complete_contract_count") != complete_contracts:
        errors.append("bundle complete-contract count mismatch")
    if payload.get("ok") is not True or payload.get("status") != "PASSED":
        errors.append("bundle is not passed")
    return errors


def load_candidate_evidence_bundle(
    path: Path,
    *,
    expected_cycle: str,
    expected_manifest_path: Path | None = None,
) -> dict:
    payload = _load_json(path)
    errors = validate_candidate_evidence_bundle(
        payload,
        expected_cycle=expected_cycle,
        expected_manifest_path=expected_manifest_path,
    )
    if errors:
        raise ValueError("; ".join(errors))
    return payload


def _load_json_when_ready(
    path: Path,
    *,
    wait_seconds: float = FACTS_FILE_WAIT_SECONDS,
    poll_seconds: float = FACTS_FILE_POLL_SECONDS,
) -> dict:
    """Wait only for the declared atomic facts dependency, then read it.

    ``live_decision_facts.py`` publishes the cycle-specific file atomically.
    This bounded wait protects the read-only batch tool from an accidental
    parallel tool batch; it does not retry exchange I/O, analysis, or trading.
    """
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        try:
            return _load_json(path)
        except FileNotFoundError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(max(0.001, float(poll_seconds)), remaining))


def _gaps(result: dict) -> list[dict]:
    return [
        {
            "timeframe": row.get("timeframe"),
            "classification": row.get("classification"),
            "raw_errors": row.get("raw_errors"),
            "indicator_errors": row.get("indicator_errors"),
        }
        for row in result.get("timeframes", [])
        if not row.get("ready")
    ]


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _protection_floor(
    *,
    side: str,
    fill_px: float | None,
    base_qty: float | None,
    initial_risk: float | None,
    peak_r: float | None,
    protected_pnl: float | None,
    live_sl_px: float | None,
    size_comparable: bool,
) -> dict:
    """Soft profit-protection floor derived from observed peak R vs. current SL.

    Level 1 (peak >= 1R): live SL must lock at least breakeven + fee/slippage
    buffer.  Level 2 (peak >= 2R): live SL must lock at least
    max(1R, 50% of peak R).  ``breach`` is a review flag only: the Agent must
    ADJUST_PROTECTION to ``suggested_min_sl_px`` (or tighter) or write the
    per-position reason for not doing so.  Nothing here places orders, writes
    the database, or blocks any action.
    """
    floor: dict[str, Any] = {
        "policy": PROTECTION_FLOOR_POLICY,
        "status": "unavailable",
        "level": 0,
        "breach": False,
        "observed_peak_r_gross": _rounded(peak_r),
        "protected_r_at_current_sl_gross": None,
        "required_protected_r": None,
        "shortfall_r": None,
        "current_live_sl_px": _rounded(live_sl_px, 10),
        "suggested_min_sl_px": None,
        "rules": {
            "level_1": (
                f"observed_peak_r>={PROTECTION_FLOOR_L1_PEAK_R:g}: SL >= breakeven "
                f"+ {PROTECTION_FLOOR_BREAKEVEN_BUFFER_PCT:.2%} fee/slippage buffer"
            ),
            "level_2": (
                f"observed_peak_r>={PROTECTION_FLOOR_L2_PEAK_R:g}: SL >= max("
                f"{PROTECTION_FLOOR_L2_MIN_LOCK_R:g}R, "
                f"{PROTECTION_FLOOR_L2_LOCK_FRACTION:.0%} of peak R)"
            ),
        },
    }
    if not size_comparable:
        floor["reason"] = "path_not_comparable_after_add_or_multiple_legs"
        return floor
    if (
        fill_px is None or base_qty is None or not base_qty
        or not initial_risk or peak_r is None or side not in {"long", "short"}
    ):
        floor["reason"] = "peak_or_initial_risk_unavailable"
        return floor

    if peak_r >= PROTECTION_FLOOR_L2_PEAK_R:
        level = 2
    elif peak_r >= PROTECTION_FLOOR_L1_PEAK_R:
        level = 1
    else:
        level = 0
    breakeven_buffer_usdt = fill_px * base_qty * PROTECTION_FLOOR_BREAKEVEN_BUFFER_PCT
    required_pnl = None
    if level == 1:
        required_pnl = breakeven_buffer_usdt
    elif level == 2:
        required_r = max(
            PROTECTION_FLOOR_L2_MIN_LOCK_R,
            PROTECTION_FLOOR_L2_LOCK_FRACTION * peak_r,
        )
        required_pnl = max(breakeven_buffer_usdt, required_r * initial_risk)

    protected_r = (
        protected_pnl / initial_risk if protected_pnl is not None else None
    )
    floor.update({
        "status": "evaluated",
        "level": level,
        "protected_r_at_current_sl_gross": _rounded(protected_r),
        "live_sl_verified": protected_pnl is not None,
    })
    if level == 0 or required_pnl is None:
        floor["reason"] = "peak_below_1r_no_floor_required"
        return floor

    required_r_value = required_pnl / initial_risk
    if side == "long":
        suggested_px = fill_px + required_pnl / base_qty
    else:
        suggested_px = fill_px - required_pnl / base_qty
    protected_value = protected_pnl if protected_pnl is not None else 0.0
    shortfall = max(0.0, (required_pnl - protected_value) / initial_risk)
    breach = protected_pnl is None or protected_value + 1e-9 < required_pnl
    floor.update({
        "required_protected_r": _rounded(required_r_value),
        "required_locked_pnl_usdt": _rounded(required_pnl),
        "shortfall_r": _rounded(shortfall),
        "breach": bool(breach),
        "suggested_min_sl_px": _rounded(suggested_px, 10),
        "suggested_min_sl_px_note": (
            "round to instrument tickSz toward the tighter side "
            "(long: up, short: down); Agent may choose tighter"
        ),
        "reason": (
            "live_sl_unverified" if protected_pnl is None
            else ("current_sl_below_floor" if breach else "floor_satisfied")
        ),
    })
    return floor


def _open_plan_context(
    connection: sqlite3.Connection | None,
    position: dict,
) -> dict:
    symbol = str(position.get("instId") or "")
    side = str(position.get("posSide") or "").lower()
    base_qty = _number(position.get("base_qty"))
    current_contracts = _number(position.get("contracts"))
    mark_px = _number(position.get("markPx"))
    current_upl = _number(position.get("upl"))
    context: dict[str, Any] = {
        "status": "unavailable",
        "source": "account.db.trade_experiences+position_snapshots",
        "symbol": symbol,
        "side": side,
        "review_flags": [],
        "review_flags_are_non_binding": True,
        "automatic_exit_authorized": False,
    }
    if connection is None:
        context["reason"] = "account_db_unavailable"
        return context
    try:
        rows = connection.execute(
            "SELECT id,cycle_id,ts,open_sz,remaining_sz,raw "
            "FROM trade_experiences WHERE profile='live' AND status='open' "
            "AND symbol=? AND lower(side)=? AND COALESCE(remaining_sz,0)>0 "
            "ORDER BY ts,id",
            (symbol, side),
        ).fetchall()
    except sqlite3.Error as exc:
        context["reason"] = f"open_plan_query_failed:{type(exc).__name__}"
        return context
    context["open_leg_count"] = len(rows)
    if len(rows) != 1:
        context["reason"] = (
            "open_plan_missing" if not rows else "multiple_open_legs_not_aggregated"
        )
        return context

    row = rows[0]
    raw = _json_object(row["raw"])
    card = _json_object(raw.get("decision_card"))
    risk_reward = _json_object(card.get("risk_reward"))
    fill_px = _number(raw.get("fill_px"))
    initial_sl = _number(raw.get("sl_trigger_px"))
    target = _number(risk_reward.get("target"))
    exit_mode = str(risk_reward.get("exit_mode") or "").strip().lower()
    if not exit_mode:
        exit_mode = "legacy_unspecified"
    open_sz = _number(row["open_sz"])
    remaining_sz = _number(row["remaining_sz"])
    size_comparable = bool(
        open_sz is not None and remaining_sz is not None
        and current_contracts is not None
        and abs(open_sz - remaining_sz) <= 1e-8
        and abs(remaining_sz - current_contracts) <= 1e-8
    )
    # 2026-08-17：只 REDUCE 过的唯一开仓腿（无 ADD，avgPx 不变）按「每张 R」复算路径，
    # 让保护地板对留 runner 的仓位同样可用；ADD 会形成第二条 open 腿，走 multiple_open_legs。
    reduce_only_path = bool(
        not size_comparable
        and open_sz is not None and remaining_sz is not None
        and current_contracts is not None and current_contracts > 0
        and open_sz > remaining_sz + 1e-8
        and abs(remaining_sz - current_contracts) <= 1e-8
    )
    path_basis = (
        "full_size" if size_comparable
        else ("per_contract_restated_to_current_size" if reduce_only_path
              else "not_comparable")
    )
    ct_val = (
        base_qty / current_contracts
        if base_qty is not None and current_contracts else None
    )
    risk_per_contract = None
    if fill_px is not None and initial_sl is not None and ct_val:
        risk_per_contract = abs(fill_px - initial_sl) * ct_val
        if risk_per_contract <= 0:
            risk_per_contract = None
    initial_risk = None          # 原始全仓（open_sz 张）的初始风险
    initial_risk_current = None  # 当前张数的初始风险 = R 分母
    if risk_per_contract and (size_comparable or reduce_only_path):
        initial_risk = risk_per_contract * open_sz
        initial_risk_current = risk_per_contract * current_contracts
    current_r = (
        current_upl / initial_risk_current
        if current_upl is not None and initial_risk_current else None
    )
    protected_pnl = _number(position.get("pnl_at_stop_from_entry_usdt"))
    protected_r = (
        protected_pnl / initial_risk_current
        if protected_pnl is not None and initial_risk_current else None
    )
    target_reached = None
    if target is not None and mark_px is not None and side in {"long", "short"}:
        target_reached = mark_px >= target if side == "long" else mark_px <= target

    peak_ts = None
    peak_upl = None              # 按当前张数口径的峰值浮盈（full_size 时即原始峰值）
    peak_upl_per_contract = None
    if size_comparable or reduce_only_path:
        try:
            snap_cols = {
                str(col[1]) for col in connection.execute(
                    "PRAGMA table_info(position_snapshots)").fetchall()
            }
            if "sz" in snap_cols:
                side_sql = " AND (side IS NULL OR lower(side)=?)" if "side" in snap_cols else ""
                params: tuple = (symbol, str(row["ts"])) + ((side,) if side_sql else ())
                peak = connection.execute(
                    "SELECT ts,upl,sz FROM position_snapshots "
                    "WHERE profile='live' AND symbol=? AND ts>=? "
                    "AND upl IS NOT NULL AND sz IS NOT NULL AND sz>0"
                    f"{side_sql} ORDER BY upl/sz DESC,rowid DESC LIMIT 1",
                    params,
                ).fetchone()
                if peak is not None:
                    peak_ts = str(peak["ts"])
                    peak_upl_per_contract = _number(peak["upl"]) / float(peak["sz"])
                    peak_upl = peak_upl_per_contract * current_contracts
            elif size_comparable:
                # 无 sz 列的旧快照表：只有张数从未变化时原始 upl 峰值才可比
                peak = connection.execute(
                    "SELECT ts,upl FROM position_snapshots "
                    "WHERE profile='live' AND symbol=? AND ts>=? "
                    "ORDER BY upl DESC,rowid DESC LIMIT 1",
                    (symbol, str(row["ts"])),
                ).fetchone()
                if peak is not None:
                    peak_ts = str(peak["ts"])
                    peak_upl = _number(peak["upl"])
                    if peak_upl is not None and current_contracts:
                        peak_upl_per_contract = peak_upl / current_contracts
        except sqlite3.Error:
            peak_ts = None
            peak_upl = None
            peak_upl_per_contract = None
    giveback_usdt = None
    giveback_pct = None
    peak_r = None
    if peak_upl is not None and current_upl is not None and peak_upl > 0:
        giveback_usdt = max(0.0, peak_upl - current_upl)
        giveback_pct = giveback_usdt / peak_upl * 100.0
    if peak_upl is not None and initial_risk_current:
        peak_r = peak_upl / initial_risk_current

    flags: list[str] = []
    if position.get("margin_return_review_at_or_above_50pct") is True:
        flags.append("official_margin_return_at_or_above_50pct")
    if target_reached is True:
        flags.append("original_target_reached")
    if current_r is not None and current_r >= 2:
        flags.append("current_profit_at_or_above_2r")
    elif current_r is not None and current_r >= 1:
        flags.append("current_profit_at_or_above_1r")
    if (
        peak_r is not None and peak_r >= 1
        and giveback_pct is not None and giveback_pct >= 25
    ):
        flags.append(
            "observed_peak_at_or_above_1r_and_giveback_at_or_above_25pct")
    if current_r is not None and current_r >= 1 and (
        protected_r is None or protected_r <= 0
    ):
        flags.append("profit_at_or_above_1r_not_locked_by_current_sl")
    if exit_mode == "legacy_unspecified":
        flags.append("legacy_exit_mode_requires_fresh_agent_choice")

    live_sl = position.get("sl") if isinstance(position.get("sl"), dict) else {}
    protection_floor = _protection_floor(
        side=side,
        fill_px=fill_px,
        base_qty=base_qty,
        initial_risk=initial_risk_current,
        peak_r=peak_r,
        protected_pnl=protected_pnl,
        live_sl_px=_number(live_sl.get("trigger_px")),
        size_comparable=bool(size_comparable or reduce_only_path),
    )
    protection_floor["path_basis"] = path_basis
    if protection_floor.get("breach"):
        flags.append(
            f"protection_floor_breach_level_{protection_floor.get('level')}")
    # 峰值不足 1R 时「回吐百分比」没有决策意义（2 USDT 峰值回吐 50% 只是 1 USDT），
    # 只在峰值 ≥1R 时才把 giveback 当退出论据；这里只打标，不替 Agent 决定。
    giveback_pct_actionable = bool(
        peak_r is not None and peak_r >= PROTECTION_FLOOR_L1_PEAK_R)

    context.update({
        "status": "unique_open_leg",
        "experience_id": int(row["id"]),
        "open_cycle_id": str(row["cycle_id"]),
        "opened_at": str(row["ts"]),
        "fill_px": _rounded(fill_px, 10),
        "initial_sl": _rounded(initial_sl, 10),
        "original_target": _rounded(target, 10),
        "original_exit_mode": exit_mode,
        "target_semantics": (
            "explicit_exit_mode" if exit_mode != "legacy_unspecified"
            else "legacy_target_not_known_to_be_exchange_tp"
        ),
        "target_reached": target_reached,
        "open_sz": _rounded(open_sz, 10),
        "remaining_sz": _rounded(remaining_sz, 10),
        "current_contracts": _rounded(current_contracts, 10),
        "path_size_comparable": size_comparable,
        "path_basis": path_basis,
        "initial_risk_usdt": _rounded(initial_risk),
        "initial_risk_current_size_usdt": _rounded(initial_risk_current),
        "risk_per_contract_usdt": _rounded(risk_per_contract, 8),
        "current_r_gross": _rounded(current_r),
        "protected_r_at_current_sl_gross": _rounded(protected_r),
        "observed_peak_upl_usdt": _rounded(peak_upl),
        "observed_peak_upl_per_contract_usdt": _rounded(peak_upl_per_contract, 8),
        "observed_peak_upl_at": peak_ts,
        "observed_peak_r_gross": _rounded(peak_r),
        "giveback_from_observed_peak_usdt": _rounded(giveback_usdt),
        "giveback_from_observed_peak_pct": _rounded(giveback_pct),
        "giveback_pct_actionable": giveback_pct_actionable,
        "giveback_pct_actionable_rule": (
            "giveback_from_observed_peak_pct is an exit argument only when "
            f"observed_peak_r_gross >= {PROTECTION_FLOOR_L1_PEAK_R:g}"
        ),
        "protection_floor": protection_floor,
        "review_flags": flags,
    })
    return context


def build_position_exit_batch(
    db_root: Path,
    facts: dict,
    cycle_id: str,
) -> dict:
    minimal_policy = (
        thresholds.minimal_decision_contract_active(cycle_id)
        or thresholds.minimal_contract_closure_active(cycle_id)
    )
    validation_errors = validate_facts(
        facts, expected_cycle=cycle_id, expected_profile="live")
    payload: dict[str, Any] = {
        "schema_version": 2 if minimal_policy else 1,
        "mode": "read_only",
        "scope": "all_current_position_exit_review",
        "cycle_id": cycle_id,
        "facts_hash": facts.get("facts_hash"),
        "facts_validation_errors": validation_errors,
        "positions": [],
        "protection_floor_breaches": [],
        "protection_floor_breach_count": 0,
        "review_policy": {
            "evidence_only": True,
            "review_flags_are_non_binding": True,
            "automatic_exit_authorized": False,
            "agent_retains_choice": [
                "hold", "close", "reduce", "adjust_protection",
            ],
            "protection_floor": (
                "soft review obligation: breach=true requires "
                "ADJUST_PROTECTION to suggested_min_sl_px (or tighter) or an "
                "explicit per-position reason in agent_judgement; no gate"
            ),
        },
        "production_database_writes": 0,
        "orders_placed": 0,
        "timeframe_judgment_used": False if minimal_policy else True,
    }
    if validation_errors:
        payload.update({"ok": False, "status": "FACTS_INVALID"})
        payload["evidence_hash"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        return payload

    # The compact view is the Agent's primary position input. Carry the exact
    # validated action permissions and margin state into it, ahead of long
    # position lists; a projection never creates execution permission.
    policy = facts.get("action_policy")
    if isinstance(policy, dict):
        payload["action_policy"] = {
            "position_truth_verified": policy.get("position_truth_verified"),
            "allowed_executor_actions": list(policy.get("allowed_executor_actions") or []),
            "open_add_allowed_by_facts": policy.get("open_add_allowed_by_facts"),
        }
        balance = facts.get("balance") or {}
        payload["account_risk_context"] = {
            key: balance.get(key) for key in (
                "current_portfolio_imr_ratio", "max_portfolio_imr_ratio",
                "portfolio_imr_ratio_unit", "headroom_before_cap_usdt",
                "portfolio_margin_state", "new_open_current_ratio_gate")
        }
    account_db = db_root / "account.db"
    connection: sqlite3.Connection | None = None
    if account_db.is_file():
        try:
            connection = sqlite3.connect(
                f"file:{account_db.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=5,
            )
            connection.row_factory = sqlite3.Row
        except sqlite3.Error:
            connection = None
    try:
        for raw_position in facts.get("positions") or []:
            if not isinstance(raw_position, dict):
                continue
            symbol = str(raw_position.get("instId") or "")
            mtf = (
                {} if minimal_policy
                else check_multitimeframe_readiness(db_root, symbol, cycle_id))
            open_plan = _open_plan_context(connection, raw_position)
            floor = open_plan.get("protection_floor") or {}
            if floor.get("breach"):
                payload["protection_floor_breaches"].append({
                    "symbol": symbol,
                    "side": raw_position.get("posSide"),
                    "level": floor.get("level"),
                    "observed_peak_r_gross": floor.get("observed_peak_r_gross"),
                    "protected_r_at_current_sl_gross": floor.get(
                        "protected_r_at_current_sl_gross"),
                    "required_protected_r": floor.get("required_protected_r"),
                    "shortfall_r": floor.get("shortfall_r"),
                    "current_live_sl_px": floor.get("current_live_sl_px"),
                    "suggested_min_sl_px": floor.get("suggested_min_sl_px"),
                })
            position_row = {
                "symbol": symbol,
                "side": raw_position.get("posSide"),
                "current": {
                    key: raw_position.get(key)
                    for key in (
                        "avgPx", "markPx", "position_age_hours", "upl",
                        "upl_pct_initial_margin",
                        "signed_price_return_pct_from_entry",
                        "margin_return_review_at_or_above_50pct",
                        "pnl_at_stop_from_entry_usdt",
                        "secured_profit_at_stop_usdt",
                        "profit_retention_at_stop_pct_of_current_upl",
                        "giveback_to_stop_pct_of_current_upl",
                    )
                },
                "open_plan_and_path": open_plan,
                "gaps": [],
                "error": None,
                "timeframe_judgment_used": False if minimal_policy else True,
            }
            if not minimal_policy:
                position_row.update({
                    "multitimeframe_ready": bool(mtf.get("ready")),
                    "multitimeframe_status": mtf.get("status"),
                    "evidence_contract": mtf.get("evidence_contract"),
                    "gaps": _gaps(mtf),
                    "error": mtf.get("error"),
                })
            payload["positions"].append(position_row)
    finally:
        if connection is not None:
            connection.close()
    all_ready = (
        True if minimal_policy else all(
            row.get("multitimeframe_ready") is True
            for row in payload["positions"])
    )
    payload["ok"] = all_ready
    payload["status"] = (
        "PASSED" if all_ready else "PARTIAL_MTF")
    payload["position_count"] = len(payload["positions"])
    payload["protection_floor_breach_count"] = len(
        payload["protection_floor_breaches"])
    if not minimal_policy:
        payload["multitimeframe_ready_count"] = sum(
            1 for row in payload["positions"]
            if row.get("multitimeframe_ready") is True)
    payload["evidence_hash"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-root", type=Path, default=Path(_public_project_path('db')))
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--symbol")
    scope.add_argument("--candidate-id")
    scope.add_argument("--facts-file", type=Path)
    scope.add_argument("--candidates-file", type=Path)
    parser.add_argument("--candidate-manifest-file", type=Path)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--out-file", type=Path, required=True)
    parser.add_argument("--decision-view-file", type=Path)
    args = parser.parse_args(argv)
    closure_policy = thresholds.minimal_contract_closure_active(
        args.cycle_id)
    if args.decision_view_file is not None and args.facts_file is None:
        parser.error("--decision-view-file is only valid with --facts-file")
    if (
        not closure_policy
        and bool(args.candidate_id) != bool(args.candidate_manifest_file)
    ):
        parser.error(
            "--candidate-id and --candidate-manifest-file must be supplied together")
    if args.candidates_file is not None:
        if (
            _production_candidate_output(args.out_file)
            and str(args.cycle_id) != _current_natural_cycle()
        ):
            print(json.dumps({
                "ok": False,
                "status": "REFUSED_IMMUTABLE_HISTORY",
                "cycle_id": args.cycle_id,
                "out_file": str(args.out_file),
                "production_database_writes": 0,
                "orders_placed": 0,
            }, ensure_ascii=False))
            return 2
        try:
            payload = build_candidate_evidence_bundle(
                args.db_root, args.candidates_file, args.cycle_id)
        except Exception as exc:  # noqa: BLE001
            payload = {
                "schema_version": 1,
                "artifact_type": CANDIDATE_BUNDLE_SCHEMA,
                "ok": False,
                "status": "ERROR",
                "mode": "read_only",
                "scope": "briefing_candidates_exact_cycle",
                "cycle_id": args.cycle_id,
                "error": f"{type(exc).__name__}:{exc}",
                "production_database_writes": 0,
                "orders_placed": 0,
            }
        _atomic_json(args.out_file, payload)
        print(json.dumps({
            "ok": bool(payload.get("ok")),
            "status": payload.get("status"),
            "scope": payload.get("scope"),
            "cycle_id": args.cycle_id,
            "candidate_count": payload.get("candidate_count", 0),
            "ready_count": payload.get("ready_count", 0),
            "not_ready_count": payload.get("not_ready_count", 0),
            "elapsed_seconds": payload.get("elapsed_seconds"),
            "bundle_sha256": payload.get("bundle_sha256"),
            "out_file": str(args.out_file),
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 0 if payload.get("ok") else 2
    if args.facts_file is not None:
        try:
            payload = build_position_exit_batch(
                args.db_root,
                _load_json_when_ready(args.facts_file),
                args.cycle_id,
            )
        except Exception as exc:  # noqa: BLE001
            payload = {
                "schema_version": 2 if closure_policy else 1,
                "ok": False,
                "status": "ERROR",
                "mode": "read_only",
                "scope": "all_current_position_exit_review",
                "cycle_id": args.cycle_id,
                "error": f"{type(exc).__name__}:{exc}",
                "production_database_writes": 0,
                "orders_placed": 0,
                "timeframe_judgment_used": (
                    False if closure_policy else True),
            }
        _atomic_json(args.out_file, payload)
        if args.decision_view_file is not None:
            _atomic_json(
                args.decision_view_file,
                build_position_exit_decision_view(payload),
                compact=True,
            )
        summary = {
            "ok": bool(payload.get("ok")),
            "status": payload.get("status"),
            "scope": payload.get("scope"),
            "cycle_id": args.cycle_id,
            "position_count": payload.get("position_count", 0),
            "protection_floor_breach_count": payload.get(
                "protection_floor_breach_count", 0),
            "protection_floor_breaches": [
                f"{row.get('symbol')}/{row.get('side')} L{row.get('level')} "
                f"peak={row.get('observed_peak_r_gross')}R "
                f"locked={row.get('protected_r_at_current_sl_gross')}R "
                f"need>={row.get('required_protected_r')}R "
                f"sl>={row.get('suggested_min_sl_px')}"
                for row in payload.get("protection_floor_breaches") or []
            ],
            "evidence_hash": payload.get("evidence_hash"),
            "out_file": str(args.out_file),
            "decision_view_file": (
                str(args.decision_view_file)
                if args.decision_view_file is not None else None),
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        if payload.get("timeframe_judgment_used") is not False:
            summary["multitimeframe_ready_count"] = payload.get(
                "multitimeframe_ready_count", 0)
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if payload.get("ok") else 2
    if args.candidate_id is not None:
        if closure_policy:
            payload = build_retired_single_candidate_receipt(
                args.cycle_id,
                candidate_id=args.candidate_id,
            )
            _atomic_json(args.out_file, payload)
            print(json.dumps({
                "ok": True,
                "status": payload["status"],
                "scope": payload["scope"],
                "candidate_id_reference": payload["candidate_id_reference"],
                "cycle_id": args.cycle_id,
                "identity_enforced": False,
                "timeframe_judgment_used": False,
                "out_file": str(args.out_file),
                "production_database_writes": 0,
                "orders_placed": 0,
            }, ensure_ascii=False))
            return 0
        try:
            payload = build_candidate_bound_evidence(
                args.db_root,
                args.candidate_manifest_file,
                args.candidate_id,
                args.cycle_id,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed before evidence use
            payload = {
                "ok": False,
                "status": "ERROR",
                "scope": "exact_manifest_candidate",
                "identity_contract": "candidate_id_v1_exact_manifest_no_alias",
                "candidate_id": args.candidate_id,
                "cycle_id": args.cycle_id,
                "error": f"{type(exc).__name__}:{exc}",
                "mode": "read_only",
                "production_database_writes": 0,
                "orders_placed": 0,
            }
        _atomic_json(args.out_file, payload)
        print(json.dumps({
            "ok": bool(payload.get("ok")),
            "status": payload.get("status"),
            "scope": payload.get("scope"),
            "candidate_id": payload.get("candidate_id"),
            "symbol": payload.get("symbol"),
            "cycle_id": args.cycle_id,
            "evidence_hash": (
                (payload.get("evidence_contract") or {}).get("evidence_hash")
            ),
            "out_file": str(args.out_file),
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 0 if payload.get("ok") else 2
    if closure_policy:
        payload = build_retired_single_candidate_receipt(
            args.cycle_id,
            symbol=args.symbol,
        )
        _atomic_json(args.out_file, payload)
        print(json.dumps({
            "ok": True,
            "status": payload["status"],
            "scope": payload["scope"],
            "symbol_reference": payload["symbol_reference"],
            "cycle_id": args.cycle_id,
            "identity_enforced": False,
            "timeframe_judgment_used": False,
            "out_file": str(args.out_file),
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 0
    result = check_multitimeframe_readiness(
        args.db_root, args.symbol, args.cycle_id)
    payload = {
        "ok": bool(result.get("ready")),
        "status": result.get("status"),
        "symbol": args.symbol,
        "cycle_id": args.cycle_id,
        "evidence_contract": result.get("evidence_contract"),
        "gaps": _gaps(result),
        "error": result.get("error"),
        "mode": "read_only",
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    _atomic_json(args.out_file, payload)
    print(json.dumps({
        "ok": payload["ok"],
        "status": payload["status"],
        "symbol": args.symbol,
        "cycle_id": args.cycle_id,
        "evidence_hash": (
            (payload.get("evidence_contract") or {}).get("evidence_hash")
        ),
        "out_file": str(args.out_file),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
