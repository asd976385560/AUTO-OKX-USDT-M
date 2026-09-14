# -*- coding: utf-8 -*-
"""Canonical structural contract for candidate screening and deep dives.

The contract is deliberately trade-neutral.  It measures whether a candidate
was bound to the exact briefing/bundle and whether the Agent recorded both
sides of the reasoning plus an invalidation condition.  During consume only,
an OPEN signal without a structurally valid matching deep dive is removed;
non-OPEN actions are never removed here, so position exits remain available.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scripts.multitimeframe_decision_evidence import (
    candidate_evidence_paths,
    load_candidate_evidence_bundle,
    load_candidate_manifest,
    load_ready_pool_artifact,
)
from scripts import _acceptance_thresholds as thresholds


QUALITY_SCHEMA = "candidate_quality_contract_v1"
DEEP_DIVE_REQUIRED_FIELDS = {
    "symbol",
    "side",
    "layer",
    "evidence_hash",
    "decision",
    "supporting_evidence",
    "opposing_evidence",
    "invalidation_condition",
    "reason_code",
    "reason",
}
IDENTITY_V2_REQUIRED_FIELDS = {"candidate_id", "reason_family"}
SHORTFALL_REASONS = {
    "candidate_shortfall",
    "bundle_degraded",
    "finalize_reserve",
}
COMPLETE_REASONS = {"target_reached", *SHORTFALL_REASONS}
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_REASON_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
_CANDIDATE_ID_RE = re.compile(r"cand_[0-9a-f]{20}")
REASON_FAMILIES = {
    "MTF_CONFLICT",
    "ENTRY_EXTENDED",
    "LOWER_TIMEFRAME_AGAINST",
    "CATALYST_WEAK",
    "MICROSTRUCTURE_AGAINST",
    "COST_OR_LIQUIDITY",
    "REPEAT_NO_NEW_EVIDENCE",
    "DATA_NOT_READY",
    "OTHER_AGENT_JUDGMENT",
}
ENTRY_READY_VETO_KINDS = {
    "cost_or_liquidity",
    "microstructure",
    "invalidation",
    "cost_adjusted_ev",
}
_VETO_OPERATORS = {">", ">=", "<", "<=", "==", "!="}
RELAXED_DECISIONS = {
    "provisional_open", "open", "accept", "vetoed", "reject", "watch", "wait",
}
_REASON_FAMILY_TOKENS = (
    ("DATA_NOT_READY", ("not_ready", "missing", "unproven", "invalid")),
    ("REPEAT_NO_NEW_EVIDENCE", ("repeat", "repeated", "no_new")),
    ("CATALYST_WEAK", ("catalyst", "primary", "news")),
    ("COST_OR_LIQUIDITY", ("spread", "cost", "thin", "liquidity", "wide_atr")),
    ("MICROSTRUCTURE_AGAINST", ("micro", "flow", "absorption", "crowded", "book")),
    ("ENTRY_EXTENDED", ("chase", "extended", "late", "overbought", "oversold")),
    ("LOWER_TIMEFRAME_AGAINST", ("15m", "fifteen", "ltf", "bounce", "reclaim")),
    ("MTF_CONFLICT", ("mtf", "4h", "1h", "htf", "aligned", "macd", "structure")),
)

_RETIRED_TIMEFRAME_REASON_RE = re.compile(
    r"(?:"
    # ASCII-only boundaries are intentional: Python's Unicode ``\b`` sees
    # both ``F`` and the following Chinese character as word characters, so
    # the old pattern missed the real production wording ``MTF未确认``.
    r"(?<![A-Za-z0-9])MTF|"
    # Bare ``1h`` is also an ordinary duration (for example ``新开不足1h``).
    # Treat explicit bars as retired authority only when the surrounding text
    # actually names a timeframe judgement, indicator or readiness contract.
    r"(?<![A-Za-z0-9])15m\s*[/、]\s*1H\s*[/、]\s*4H"
    r"(?![A-Za-z0-9])|"
    r"(?:缺少?|缺乏|无|没有|等待)(?:15m|1H|4H)(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])(?:15m|1H|4H)(?![A-Za-z0-9])"
    r"(?=[-_\s]*(?:not[-_\s]*ready|ready|timeframe|tf|K线|周期|级别|"
    r"走势|趋势|方向|结构|信号|确认|共振|对齐|授权|证据|冲突|反向|同向|"
    r"RSI|MACD|ATR|trend|direction|structure|signal|confirm|align|conflict))|"
    r"三周期|多周期|"
    r"(?:高低|高/低|高、低|跨|长短|大小|高|低)(?:级别)?周期|"
    r"(?:周期|级别)(?:共振|一致|对齐|确认|授权|证据|方向|冲突)|"
    r"(?:15分钟|十五分钟|1小时|一小时|4小时|四小时)"
    r"(?:级别|周期|K线|结构|方向|信号|趋势|确认|证据)|"
    r"(?:multi(?:ple)?|cross|higher|lower)[-_\s]*"
    r"(?:timeframe|time[-_\s]*frame|tf)s?|"
    r"(?:timeframe|time[-_\s]*frame)[-_\s]*"
    r"(?:align(?:ment)?|confirm(?:ation)?|conflict|confluence)|"
    r"(?<![A-Za-z0-9])(?:timeframe|time[-_\s]*frame)s?"
    r"(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])(?:HTF|LTF)(?![A-Za-z0-9])"
    r")",
    re.IGNORECASE)
_RETIRED_OPPORTUNITY_AUTHORITY_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9_])(?:ENTRY_READY|EARLY_WATCH|(?-i:EXTENDED|TRIGGERING))"
    r"(?![A-Za-z0-9_])|"
    # Lower-case extended/triggering are also ordinary market prose. Keep
    # named uppercase states forbidden; other spellings need a state cue,
    # just as an ordinary duration is distinguished from timeframe authority.
    r"^\s*(?:extended|triggering)\s*$|"
    r"(?:opportunity[-_\s]*state|state|状态|阶段)"
    r"[-_\s:=：]*(?:is|为)?[-_\s]*"
    r"(?:extended|triggering)(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])(?:extended|triggering)(?![A-Za-z0-9_])"
    r"[-_\s]*(?:state|phase|candidate|layer|bucket|quota|authori[sz]ation|"
    r"状态|阶段|候选|分层|层级|授权)|"
    r"(?:成熟|早期)(?:/早期)?(?:候选|结构|状态|分层|层级|组别|组|信号|授权)|"
    r"(?:mature|early)(?:[-_\s]*stage)?[-_\s]*"
    r"(?:candidate|setup|signal|state|layer|bucket|quota|authori[sz]ation)|"
    r"(?:candidate|setup|signal|state)[-_\s]*(?:is[-_\s]*)?"
    r"(?:mature|early)"
    r")",
    re.IGNORECASE)
_SOFT_ONLY_REASON_RE = re.compile(
    r"(?:"
    r"成交额|成交量|量能|交投|成交活跃度|"
    r"(?<![A-Za-z0-9])OI(?![A-Za-z0-9])|未平仓(?:量|合约)|持仓量|"
    r"流动性|无催化|缺催化|缺乏催化|没有催化|无新闻|缺新闻|"
    r"无事件驱动|缺事件驱动|缺乏事件驱动|无消息驱动|"
    r"(?:已有|现有|当前)(?:\d+|[零一二三四五六七八九十百两]+)?"
    r"(?:个)?(?:仓|仓位)|(?:仓位|持仓)(?:数|过多|太多|较多|已满|满载)|"
    r"(?<![A-Za-z0-9])IMR(?![A-Za-z0-9])|保证金|风控预算|"
    r"风险预算|预算紧张|"
    r"(?:low|weak|thin|poor|insufficient|limited)[-_\s]*"
    r"(?:volume|turnover|liquidity|open[-_\s]*interest|oi)|"
    r"(?:volume|turnover|liquidity|open[-_\s]*interest|oi)"
    r"[-_\s]*(?:low|weak|thin|poor|insufficient|limited)|"
    r"(?:no|missing|weak)[-_\s]*(?:catalyst|news|event[-_\s]*driver)|"
    r"(?:too[-_\s]*many|many|existing)[-_\s]*"
    r"(?:positions|holdings)|(?:position|holding)[-_\s]*count|"
    r"(?:margin|risk)[-_\s]*(?:budget|headroom|capacity)[-_\s]*"
    r"(?:tight|low|limited|insufficient)|"
    r"(?:tight|low|limited|insufficient)[-_\s]*"
    r"(?:margin|risk)[-_\s]*(?:budget|headroom|capacity)|"
    r"margin[-_\s]*(?:usage|utilization)|"
    r"portfolio[-_\s]*(?:crowded|full|capacity|headroom)"
    r")",
    re.IGNORECASE)
_INDEPENDENT_REJECT_EVIDENCE_RE = re.compile(
    r"(?:点差|滑点|订单簿|盘口深度|深度不足|CVD|失衡|买盘|卖盘|"
    r"spread|slippage|order[-_\s]*book|market[-_\s]*depth|"
    r"funding|资金费|basis|多空比|价格冲突|方向冲突|信号冲突|"
    r"无净方向|失效|invalidation|止损几何|成本调整|cost_adjusted)",
    re.IGNORECASE)
_HARD_REJECT_EVIDENCE_RE = re.compile(
    r"(?:"
    r"(?:超过|超出|达到|触发).{0,20}"
    r"(?:66\.6%|15%|5%|98%|10x|硬上限|硬闸)|"
    r"(?:硬闸|硬上限).{0,8}(?:拒绝|阻断|触发)|"
    r"(?:exceed(?:s|ed)?|above|over|breach(?:es|ed)?|at[-_\s]*or[-_\s]*over)"
    r".{0,20}(?:hard[-_\s]*)?(?:gate|cap|limit)|"
    r"hard[-_\s]*gate.{0,8}(?:reject|block|fail)|"
    r"(?:单笔|本单)[-_\s]*IMR.{0,20}"
    r"(?:>|>=|超过|超出|高于).{0,12}15%|"
    r"(?:预计)?组合[-_\s]*IMR.{0,20}"
    r"(?:>|>=|超过|超出|高于).{0,12}66\.6%|"
    r"(?:止损风险|stop[-_\s]*risk).{0,20}"
    r"(?:>|>=|超过|超出|高于).{0,12}5%|"
    r"(?:projected[-_\s]*)?(?:portfolio[-_\s]*)?imr.{0,20}"
    r"(?:>|>=|超过|超出).{0,12}(?:66\.6%|cap|limit)|"
    r"single[-_\s]*(?:order[-_\s]*)?imr.{0,20}"
    r"(?:>|>=|exceed).{0,12}(?:15%|cap|limit)|"
    r"(?:stop[-_\s]*risk|止损风险).{0,20}"
    r"(?:>|>=|超过|超出|exceed).{0,12}(?:5%|cap|limit)|"
    r"facts\.status\s*=\s*blocking|"
    r"(?:账户|account).{0,12}(?:不可验证|未验证|unverified)|"
    r"(?:账仓|ledger).{0,12}(?:不一致|mismatch)|"
    r"(?:intent).{0,12}(?:冲突|conflict)|"
    r"(?:SL|止损).{0,12}(?:缺失|非法|不可验证|missing|invalid)"
    r")",
    re.IGNORECASE)
_NEGATED_HARD_REJECT_EVIDENCE_RE = re.compile(
    r"(?:"
    r"(?:未|尚未|没有|并未|不曾)(?:触发|达到|超过|超出|高于|突破)"
    r".{0,20}(?:硬闸|硬上限|上限|阈值|66\.6%|15%|5%)|"
    r"(?:低于|小于|未达).{0,20}(?:硬闸|硬上限|上限|阈值|66\.6%|15%|5%)|"
    r"(?:not|does[-_\s]*not|did[-_\s]*not|has[-_\s]*not|"
    r"below|under|within).{0,24}"
    r"(?:trigger|reach|exceed|above|over|breach|hard[-_\s]*(?:gate|cap|limit)|"
    r"66\.6%|15%|5%)"
    r")",
    re.IGNORECASE)


def closure_retired_authority_reason_errors(reason: str) -> list[str]:
    """Reject any retired timeframe/state authority in closure prose."""
    text = str(reason or "").strip()
    errors: list[str] = []
    if _RETIRED_TIMEFRAME_REASON_RE.search(text):
        errors.append("retired_timeframe_reason_forbidden")
    if _RETIRED_OPPORTUNITY_AUTHORITY_RE.search(text):
        errors.append("retired_opportunity_authority_forbidden")
    return errors


def closure_soft_only_reject_reason_errors(reason: str) -> list[str]:
    """Reject soft-only non-OPEN reasons; OPEN observations stay admissible."""
    text = str(reason or "").strip()
    errors: list[str] = []
    hard_reject_proven = bool(
        _HARD_REJECT_EVIDENCE_RE.search(text)
        and not _NEGATED_HARD_REJECT_EVIDENCE_RE.search(text)
    )
    if (
        _SOFT_ONLY_REASON_RE.search(text)
        and not _INDEPENDENT_REJECT_EVIDENCE_RE.search(text)
        and not hard_reject_proven
    ):
        errors.append("soft_observation_cannot_be_sole_reject_reason")
    return errors


def closure_reject_reason_errors(reason: str) -> list[str]:
    """Closure non-OPEN reason contract (retired authority + soft-only veto)."""
    return (
        closure_retired_authority_reason_errors(reason)
        + closure_soft_only_reject_reason_errors(reason)
    )


def classify_reason_family(reason_code: str, reason: str = "") -> str:
    """Map free detail to one stable diagnostic family, never a trade gate."""
    text = f"{reason_code} {reason}".lower()
    for family, tokens in _REASON_FAMILY_TOKENS:
        if any(token in text for token in tokens):
            return family
    return "OTHER_AGENT_JUDGMENT"


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _evidence_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_nonempty_text(item) for item in value)
    )


def _invalidation_condition(value: Any) -> bool:
    return (
        _nonempty_text(value)
        or isinstance(value, dict) and bool(value)
        or isinstance(value, list) and bool(value)
    )


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return number == number and abs(number) != float("inf")


def _entry_ready_veto_errors(value: Any) -> list[str]:
    veto = _dict(value)
    errors: list[str] = []
    if str(veto.get("kind") or "").strip().lower() not in ENTRY_READY_VETO_KINDS:
        errors.append("entry_ready_primary_disqualifier_kind_invalid")
    if not _nonempty_text(veto.get("metric")):
        errors.append("entry_ready_primary_disqualifier_metric_missing")
    observed_ok = _finite_number(veto.get("observed_value"))
    boundary_ok = _finite_number(veto.get("boundary_value"))
    if not observed_ok:
        errors.append("entry_ready_primary_disqualifier_observed_invalid")
    operator = str(veto.get("operator") or "").strip()
    if operator not in _VETO_OPERATORS:
        errors.append("entry_ready_primary_disqualifier_operator_invalid")
    if not boundary_ok:
        errors.append("entry_ready_primary_disqualifier_boundary_invalid")
    if not _nonempty_text(veto.get("source_path")):
        errors.append("entry_ready_primary_disqualifier_source_missing")
    forbidden_observation = " ".join((
        str(veto.get("metric") or ""),
        str(veto.get("source_path") or ""),
    )).lower()
    normalized_observation = re.sub(
        r"[^a-z0-9]+", "_", forbidden_observation).strip("_")
    observation_tokens = set(normalized_observation.split("_"))
    if (
        any(token in normalized_observation for token in (
            "quote_volume", "turnover", "vol24h", "open_interest", "oi_usd",
            "oiusd", "volume_24h", "24h_volume"))
        or "volume" in observation_tokens
        or "oi" in observation_tokens
        or ({"volume", "24h"} <= observation_tokens)
    ):
        errors.append("entry_ready_primary_disqualifier_removed_market_threshold")
    if observed_ok and boundary_ok and operator in _VETO_OPERATORS:
        observed = float(veto["observed_value"])
        boundary = float(veto["boundary_value"])
        relation = {
            ">": observed > boundary,
            ">=": observed >= boundary,
            "<": observed < boundary,
            "<=": observed <= boundary,
            "==": observed == boundary,
            "!=": observed != boundary,
        }[operator]
        if not relation:
            errors.append("entry_ready_primary_disqualifier_relation_false")
    return errors


def _same_path(left: Any, right: Any) -> bool:
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except (OSError, TypeError, ValueError):
        return str(left or "") == str(right or "")


def _int(value: Any) -> int | None:
    return (
        int(value)
        if isinstance(value, int) and not isinstance(value, bool)
        else None
    )


def _signal_evidence_hash(signal: dict) -> str:
    card = signal.get("decision_card")
    mtf = card.get("multitimeframe_analysis") if isinstance(card, dict) else None
    contract = mtf.get("evidence_contract") if isinstance(mtf, dict) else None
    return str(contract.get("evidence_hash") or "") if isinstance(
        contract, dict) else ""


def normalize_candidate_quality(
    *,
    cycle_id: str,
    raw: Any,
    signals: Any,
    phase: str,
    evidence_root: Path | None = None,
) -> tuple[dict, list, dict]:
    """Return canonical raw, safe signals, and a deterministic quality result."""
    inner = _dict(raw)
    safe_signals = list(signals) if isinstance(signals, list) else []
    relaxed_policy = thresholds.decision_restriction_removal_active(cycle_id)
    minimal_policy = thresholds.minimal_decision_contract_active(cycle_id)
    closure_policy = thresholds.minimal_contract_closure_active(cycle_id)
    supported_phases = {"shadow", "consume", "manifest_only", "rollback"}
    if phase not in supported_phases:
        return inner, safe_signals, {
            "schema": QUALITY_SCHEMA,
            "phase": phase,
            "status": "NOT_APPLICABLE",
            "errors": [],
            "rejected_open_signals": [],
        }

    paths = candidate_evidence_paths(cycle_id, root=evidence_root)
    errors: list[str] = []
    manifest: dict | None = None
    bundle: dict | None = None
    manifest_error = None
    bundle_error = None
    try:
        manifest, _ = load_candidate_manifest(paths["manifest"], cycle_id)
    except Exception as exc:  # noqa: BLE001 - bounded structural diagnostic
        manifest_error = f"{type(exc).__name__}:{exc}"
        errors.append(f"manifest:{manifest_error}")
    bundle_expected = phase in {"shadow", "consume"}
    if manifest is not None and bundle_expected:
        try:
            bundle = load_candidate_evidence_bundle(
                paths["bundle"],
                expected_cycle=cycle_id,
                expected_manifest_path=paths["manifest"],
            )
        except Exception as exc:  # noqa: BLE001
            bundle_error = f"{type(exc).__name__}:{exc}"

    manifest_items = list(manifest.get("candidates") or []) if manifest else []
    ready_pool_ref = (
        manifest.get("ready_pool")
        if isinstance(manifest, dict)
        and isinstance(manifest.get("ready_pool"), dict) else {})
    ready_pool_status = (
        str(ready_pool_ref.get("status") or "PASSED").upper()
        if ready_pool_ref else "UNAVAILABLE")
    if ready_pool_ref and ready_pool_status != "PASSED":
        errors.append(f"ready_pool:{ready_pool_status.lower()}")
    ready_pool_by_symbol: dict[str, dict] = {}
    if ready_pool_ref and ready_pool_status == "PASSED":
        try:
            ready_pool_payload = load_ready_pool_artifact(
                Path(str(ready_pool_ref.get("path") or "")), cycle_id)
            ready_pool_by_symbol = {
                str(item.get("symbol") or "").upper(): item
                for item in (ready_pool_payload.get("items") or [])
                if isinstance(item, dict) and item.get("symbol")
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ready_pool_reload:{type(exc).__name__}:{exc}")
    identity_v2 = bool(manifest_items) and all(
        isinstance(item, dict) and _CANDIDATE_ID_RE.fullmatch(
            str(item.get("candidate_id") or ""))
        for item in manifest_items
    )
    manifest_by_symbol = {
        str(item.get("symbol") or "").upper(): item
        for item in manifest_items if isinstance(item, dict)
    }
    manifest_by_id = {
        str(item.get("candidate_id") or ""): item
        for item in manifest_items
        if isinstance(item, dict) and item.get("candidate_id")
    }
    bundle_items = list(bundle.get("items") or []) if bundle else []
    bundle_by_symbol = {
        str(item.get("symbol") or "").upper(): item
        for item in bundle_items if isinstance(item, dict)
    }
    bundle_by_id = {
        str(item.get("candidate_id") or ""): item
        for item in bundle_items
        if isinstance(item, dict) and item.get("candidate_id")
    }
    bundle_passed = bundle is not None
    pool_count = len(manifest_items) if manifest is not None else None
    screened_count = (
        int(bundle.get("screened_count") or 0)
        if bundle_passed else 0)
    ready_count = (
        int(bundle.get(
            "decision_ready_count", bundle.get("ready_count")) or 0)
        if bundle_passed else None)
    canonical_screening = {
        "schema": (
            "candidate_screening_v3_side_neutral"
            if minimal_policy else "candidate_screening_v1"),
        "identity_contract": (
            "candidate_id_v1" if identity_v2 else "legacy_symbol_identity"),
        "phase": phase,
        "pool_count": pool_count,
        "screened_count": screened_count,
        "ready_count": ready_count,
        "manifest_count": len(manifest_items) if manifest is not None else None,
        "decision_slice_count": (
            int(bundle.get("decision_slice_count") or len(bundle_items))
            if bundle_passed else None),
        "full_screening_ready_count": (
            int(bundle.get("ready_count") or 0)
            if bundle_passed else None),
        "not_ready_count": (
            (len(bundle_items) if relaxed_policy else pool_count) - ready_count
            if isinstance(pool_count, int) and isinstance(ready_count, int)
            else None
        ),
        "briefing_sha256": (
            manifest.get("manifest_sha256") if manifest is not None else None),
        "bundle_sha256": (
            bundle.get("bundle_sha256") if bundle is not None else None),
        "bundle_path": str(paths["bundle"]),
        "bundle_status": (
            "PASSED" if bundle_passed else
            "DEGRADED" if bundle_expected else "NOT_APPLICABLE"),
        "gaps": [
            {
                "symbol": item.get("symbol"),
                "gaps": list(item.get("gaps") or []),
                "error": item.get("error"),
            }
            for item in bundle_items
            if item.get("ready") is not True
        ],
        "manifest_error": manifest_error,
        "bundle_error": bundle_error,
        "full_ready_pool_status": ready_pool_status,
        "full_ready_pool_path": ready_pool_ref.get("path"),
        "full_ready_pool_sha256": ready_pool_ref.get("sha256"),
        "full_ready_count": ready_pool_ref.get("ready_count"),
        "full_ready_pool_error": ready_pool_ref.get("error"),
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    if minimal_policy:
        canonical_screening.update({
            "identity_contract": (
                "symbol_review_v2_full_manifest_no_identity_gate"
                if closure_policy else
                "symbol_review_v1_side_selected_by_agent"),
            "ready_count": int(bundle.get("ready_count") or 0)
            if bundle_passed else None,
            "not_ready_count": 0 if bundle_passed else None,
            "timeframe_judgment_used": False,
        })
    reported_screening = _dict(inner.get("candidate_screening"))
    for field in (
        "pool_count", "screened_count", "ready_count", "briefing_sha256",
        "bundle_sha256", "bundle_path",
    ):
        matches = (
            _same_path(
                reported_screening.get(field), canonical_screening.get(field))
            if field == "bundle_path"
            else reported_screening.get(field) == canonical_screening.get(field)
        )
        if field in reported_screening and not matches:
            errors.append(f"candidate_screening.{field}:reported_mismatch")
    inner["candidate_screening"] = canonical_screening

    if minimal_policy:
        reported_entries = inner.get("candidates_deep_dived_v2")
        entries = (
            list(reported_entries) if isinstance(reported_entries, list)
            else [])
        policy_errors = list(errors)
        if not isinstance(reported_entries, list):
            policy_errors.append("candidates_deep_dived_v2:missing_or_not_list")
        signal_by_symbol: dict[str, list[dict]] = {}
        for signal_index, signal in enumerate(safe_signals):
            if not isinstance(signal, dict):
                continue
            action = str(signal.get("action") or "").strip().lower()
            if action not in {"open_long", "open_short"}:
                continue
            symbol = str(signal.get("symbol") or "").strip().upper()
            signal_by_symbol.setdefault(symbol, []).append(signal)
            if closure_policy:
                policy_errors.extend(
                    f"signal[{signal_index}]:{item_error}"
                    for item_error in closure_retired_authority_reason_errors(
                        str(signal.get("reasoning") or ""))
                )
        reviewable_symbols = (
            set(manifest_by_symbol) if closure_policy else set(bundle_by_symbol))
        seen_symbols: set[str] = set()
        valid_entries: list[dict] = []
        invalid_entries: list[dict] = []
        open_decisions = {
            "provisional_open", "open", "accept", "open_long", "open_short"}
        non_open_decisions = {
            "veto", "vetoed", "reject", "watch", "wait", "drop", "dropped"}
        for index, value in enumerate(entries):
            entry = _dict(value)
            item_errors: list[str] = []
            symbol = str(entry.get("symbol") or "").strip().upper()
            reported_decision = str(
                entry.get("decision") or "").strip().lower()
            decision = reported_decision
            reason = str(entry.get("reason") or "").strip()
            if not symbol or symbol in seen_symbols:
                item_errors.append("symbol_missing_or_duplicated")
            seen_symbols.add(symbol)
            candidate = manifest_by_symbol.get(symbol)
            if candidate is None:
                item_errors.append(
                    "outside_full_manifest"
                    if closure_policy else "outside_side_neutral_review_slice")
            elif symbol not in reviewable_symbols:
                item_errors.append("outside_side_neutral_review_slice")
            if not reason:
                item_errors.append("reason_missing")
            if closure_policy:
                item_errors.extend(
                    closure_retired_authority_reason_errors(reason))
            symbol_signals = signal_by_symbol.get(symbol, [])
            if len(symbol_signals) > 1:
                item_errors.append("multiple_open_signals_for_symbol")
            selected_side = None
            if symbol_signals:
                action = str(symbol_signals[0].get("action") or "").lower()
                selected_side = "long" if action == "open_long" else "short"
                decision = "provisional_open"
            elif reported_decision in open_decisions:
                item_errors.append("provisional_open_signal_missing")
            else:
                # Owner removed candidate-quality consume filtering.  The
                # Agent's non-open wording is diagnostic only; do not revive
                # the old ENTRY_READY veto allowlist or market-threshold test.
                decision = "reject"
                if closure_policy:
                    item_errors.extend(
                        closure_soft_only_reject_reason_errors(reason))
            canonical_entry = {
                "symbol": symbol,
                "eligible_sides": ["long", "short"],
                "selected_side": selected_side,
                "decision": decision,
                "reason": reason,
                "primary_disqualifier": entry.get("primary_disqualifier"),
                "candidate_id": (
                    None if closure_policy else candidate.get("candidate_id")
                    if isinstance(candidate, dict) else None),
                "review_hash": (
                    None if closure_policy else
                    (bundle_by_symbol.get(symbol) or {}).get("review_hash")),
                "timeframe_judgment_used": False,
                "quality_valid": not item_errors,
                "quality_errors": item_errors,
            }
            entries[index] = canonical_entry
            if item_errors:
                invalid_entries.append({
                    "index": index, "symbol": symbol or None,
                    "errors": item_errors,
                })
            else:
                valid_entries.append(canonical_entry)
        reported_coverage = _dict(inner.get("candidate_coverage"))
        reported_limit = _int(reported_coverage.get("dynamic_limit"))
        dynamic_limit = min(
            len(bundle_items), max(len(entries), reported_limit or 0))
        expected_target = len(entries)
        for invalid in invalid_entries:
            policy_errors.extend(
                f"candidate[{invalid['index']}]:{item_error}"
                for item_error in invalid["errors"])
        inner["candidates_deep_dived_v2"] = entries
        inner["candidates_deep_dived"] = [
            {
                "instId": entry["symbol"],
                "decision": entry["decision"],
                "reason": entry["reason"],
            }
            for entry in valid_entries
        ]
        inner["candidate_coverage"] = {
            "schema": "candidate_review_coverage_v3_side_neutral",
            "dynamic_floor": min(3, expected_target),
            "dynamic_limit": dynamic_limit,
            "dynamic_target": expected_target,
            "actual_count": len(valid_entries),
            "reported_count": len(entries),
            "quality_valid_count": len(valid_entries),
            "quality_invalid_count": len(invalid_entries),
            "not_deep_dived_count": max(len(manifest_items) - len(valid_entries), 0),
            "stop_reason": str(reported_coverage.get("stop_reason") or ""),
            "rotation_required": False,
            "rotation_satisfied": None,
            "quality_status": "MET" if not policy_errors else "NOT_MET",
            "timeframe_judgment_used": False,
        }
        inner["candidate_identity"] = {
            "schema": "candidate_identity_observation_v2_side_neutral",
            "active": False,
            "identity_contract": (
                "symbol_review_v2_full_manifest_no_identity_gate"
                if closure_policy else
                "symbol_review_v1_side_selected_by_agent"),
            "rejected_open_signals": [],
            "non_open_actions_affected": False,
        }
        inner["candidate_quality"] = {
            "schema": QUALITY_SCHEMA,
            "open_filter_active": False,
            "rejected_open_signals": [],
            "timeframe_judgment_used": False,
        }
        inner["side_regime_soft_veto_shadow"] = {
            "schema": "side_regime_soft_veto_shadow_v1",
            "active": False,
            "mode": "retired_by_owner_policy",
            "counterfactuals": [],
            "signals_mutated": False,
            "signal_write_authority": False,
            "order_authority": False,
            "scheduler_authority": False,
        }
        result = {
            "schema": QUALITY_SCHEMA,
            "phase": phase,
            "status": "MET" if not policy_errors else "NOT_MET",
            "errors": policy_errors,
            "invalid_entries": invalid_entries,
            "rejected_open_signals": [],
            "open_filter_active": False,
            "policy": (
                thresholds.MINIMAL_CONTRACT_CLOSURE_POLICY
                if closure_policy else
                thresholds.MINIMAL_DECISION_CONTRACT_POLICY),
            "policy_blocking_errors": policy_errors,
        }
        return inner, safe_signals, result

    reported_entries = inner.get("candidates_deep_dived_v2")
    entries = list(reported_entries) if isinstance(reported_entries, list) else []
    if not isinstance(reported_entries, list):
        errors.append("candidates_deep_dived_v2:missing_or_not_list")
    valid_entries: list[dict] = []
    invalid_entries: list[dict] = []
    seen: set[str] = set()
    for index, entry_value in enumerate(entries):
        entry = _dict(entry_value)
        item_errors: list[str] = []
        required_fields = set(DEEP_DIVE_REQUIRED_FIELDS)
        if relaxed_policy:
            required_fields.discard("evidence_hash")
        if identity_v2 and not relaxed_policy:
            required_fields.update(IDENTITY_V2_REQUIRED_FIELDS)
        missing = sorted(required_fields - set(entry))
        if missing:
            item_errors.append("missing:" + ",".join(missing))
        candidate_id = str(entry.get("candidate_id") or "").strip().lower()
        reported_symbol = str(entry.get("symbol") or "").strip().upper()
        symbol = reported_symbol
        side = str(entry.get("side") or "").strip().lower()
        layer = str(entry.get("layer") or "").strip().lower()
        evidence_hash = str(entry.get("evidence_hash") or "").strip().lower()
        decision = str(entry.get("decision") or "").strip().lower()
        reason_code = str(entry.get("reason_code") or "").strip().lower()
        reason = str(entry.get("reason") or "").strip()
        reported_reason_family = str(
            entry.get("reason_family") or "").strip().upper()
        canonical_reason_family = (
            reported_reason_family or None
            if relaxed_policy else classify_reason_family(reason_code, reason))
        candidate = (
            manifest_by_symbol.get(symbol)
            if relaxed_policy else
            manifest_by_id.get(candidate_id)
            if identity_v2 else manifest_by_symbol.get(symbol)
        )
        if identity_v2 and not relaxed_policy and candidate is None:
            item_errors.append("candidate_id_missing_or_outside_exact_pool")
        if candidate is not None and identity_v2:
            symbol = str(candidate.get("symbol") or "").strip().upper()
        bundle_item = (
            bundle_by_symbol.get(symbol)
            if relaxed_policy else
            bundle_by_id.get(candidate_id)
            if identity_v2 else bundle_by_symbol.get(symbol)
        )
        if not symbol or symbol in seen:
            item_errors.append("symbol_missing_or_duplicated")
        seen.add(symbol)
        if candidate is None:
            if not identity_v2:
                item_errors.append("outside_exact_briefing_pool")
        else:
            if identity_v2 and reported_symbol != symbol:
                item_errors.append("reported_symbol_mismatch_candidate_id")
            if side != candidate.get("side"):
                item_errors.append("side_mismatch")
            if layer != candidate.get("layer"):
                item_errors.append("layer_mismatch")
        if identity_v2 and not relaxed_policy:
            if not _CANDIDATE_ID_RE.fullmatch(candidate_id):
                item_errors.append("candidate_id_invalid")
            if reported_reason_family not in REASON_FAMILIES:
                item_errors.append("reason_family_invalid")
        if not relaxed_policy and not _HASH_RE.fullmatch(evidence_hash):
            item_errors.append("evidence_hash_invalid")
        if bundle_passed and not relaxed_policy:
            if bundle_item is None:
                item_errors.append("bundle_item_missing")
            else:
                if bundle_item.get("ready") is not True:
                    item_errors.append("bundle_item_not_ready")
                if evidence_hash != bundle_item.get("evidence_hash"):
                    item_errors.append("bundle_evidence_hash_mismatch")
        identity_errors: list[str] = []
        if identity_v2 and not relaxed_policy:
            if candidate is None:
                identity_errors.append("candidate_id_missing_or_outside_exact_pool")
            else:
                if reported_symbol != str(candidate.get("symbol") or "").upper():
                    identity_errors.append("reported_symbol_mismatch_candidate_id")
                if side != candidate.get("side"):
                    identity_errors.append("side_mismatch")
                if layer != candidate.get("layer"):
                    identity_errors.append("layer_mismatch")
            if not _CANDIDATE_ID_RE.fullmatch(candidate_id):
                identity_errors.append("candidate_id_invalid")
            if not _HASH_RE.fullmatch(evidence_hash):
                identity_errors.append("evidence_hash_invalid")
            if bundle_passed:
                if bundle_item is None:
                    identity_errors.append("bundle_item_missing")
                else:
                    if bundle_item.get("ready") is not True:
                        identity_errors.append("bundle_item_not_ready")
                    if evidence_hash != bundle_item.get("evidence_hash"):
                        identity_errors.append("bundle_evidence_hash_mismatch")
        if not decision:
            item_errors.append("decision_missing")
        elif relaxed_policy and decision not in RELAXED_DECISIONS:
            item_errors.append("decision_invalid_for_lightweight_policy")
        if not _evidence_list(entry.get("supporting_evidence")):
            item_errors.append("supporting_evidence_invalid")
        if not _evidence_list(entry.get("opposing_evidence")):
            item_errors.append("opposing_evidence_invalid")
        if not _invalidation_condition(entry.get("invalidation_condition")):
            item_errors.append("invalidation_condition_invalid")
        if not _REASON_CODE_RE.fullmatch(reason_code):
            item_errors.append("reason_code_invalid")
        if not reason:
            item_errors.append("reason_missing")
        if not relaxed_policy and candidate is not None and (
                candidate.get("new_evidence_required") is True
                or int(candidate.get("recent_rejections_6h") or 0) >= 3):
            prior_hash = str(
                candidate.get("prior_evidence_hash") or "").strip().lower()
            if identity_v2 and not _HASH_RE.fullmatch(prior_hash):
                item_errors.append("prior_evidence_hash_missing")
            elif prior_hash == evidence_hash:
                item_errors.append("new_evidence_hash_unchanged")
        canonical_entry = {
            **entry,
            "candidate_id": candidate_id or None,
            "symbol": symbol,
            "reported_symbol": (
                reported_symbol if reported_symbol != symbol else None),
            "side": side,
            "layer": layer,
            "evidence_hash": evidence_hash,
            "decision": decision,
            "reason_code": reason_code,
            "reason_family": canonical_reason_family,
            "reported_reason_family": (
                reported_reason_family
                if reported_reason_family != canonical_reason_family else None),
            "reason_family_normalized": (
                False if relaxed_policy else
                reported_reason_family != canonical_reason_family),
            "reason": reason,
            "quality_valid": not item_errors,
            "quality_errors": item_errors,
            "identity_valid": (
                None if relaxed_policy else identity_v2 and not identity_errors),
            "identity_errors": identity_errors,
        }
        if candidate is not None:
            opportunity_fields = (
                "opportunity_id", "opportunity_state", "state_version",
                "first_seen_cycle", "first_seen_ts_utc", "first_seen_price",
                "state_entered_cycle", "previous_state", "state_transition",
                "regime_first_seen", "regime_first_seen_source_ts",
                "regime_current", "regime_current_source_ts",
                "initial_invalidation", "current_invalidation",
                "trend_strength", "entry_timing", "ready_pool_rank",
                "rank_version",
            )
            canonical_entry["reported_opportunity_context"] = {
                field: entry.get(field) for field in opportunity_fields
                if field in entry and entry.get(field) != candidate.get(field)
            } or None
            for field in opportunity_fields:
                canonical_entry[field] = candidate.get(field)
        signal_keys = {
            (
                str(signal.get("symbol") or "").strip().upper(),
                "long" if str(signal.get("action") or "").lower() == "open_long"
                else "short",
            )
            for signal in safe_signals if isinstance(signal, dict)
            and str(signal.get("action") or "").lower() in {
                "open_long", "open_short"}
        }
        if relaxed_policy and canonical_entry.get("opportunity_state") in {
                "ENTRY_READY", "EXTENDED", "TRIGGERING", "EARLY_WATCH"}:
            key = (symbol, side)
            open_decisions = {"open", "accept", "provisional_open"}
            if key in signal_keys:
                if decision not in open_decisions:
                    item_errors.append(
                        "entry_ready_open_signal_decision_inconsistent")
            elif decision in open_decisions:
                item_errors.append("entry_ready_provisional_open_signal_missing")
            else:
                item_errors.extend(_entry_ready_veto_errors(
                    entry.get("primary_disqualifier")))
            canonical_entry["opportunity_state_default_authorized"] = True
            canonical_entry["entry_ready_default_authorized"] = (
                canonical_entry.get("opportunity_state") == "ENTRY_READY")
        canonical_entry["quality_valid"] = not item_errors
        canonical_entry["quality_errors"] = item_errors
        if item_errors:
            invalid_entries.append({
                "index": index,
                "symbol": symbol or None,
                "errors": item_errors,
            })
        else:
            valid_entries.append(canonical_entry)
        entries[index] = canonical_entry
    inner["candidates_deep_dived_v2"] = entries
    inner["candidates_deep_dived"] = [
        {
            "candidate_id": entry.get("candidate_id"),
            "instId": entry["symbol"],
            "evidence_hash": entry["evidence_hash"],
            "decision": entry["decision"],
            "reason_family": entry.get("reason_family"),
            "reason": entry["reason"],
        }
        for entry in valid_entries
    ]

    reported_coverage = _dict(inner.get("candidate_coverage"))
    dynamic_limit = _int(reported_coverage.get("dynamic_limit"))
    if dynamic_limit is None or not 0 <= dynamic_limit <= 8:
        errors.append("candidate_coverage.dynamic_limit:invalid")
        dynamic_limit = 0
    expected_target = (
        min(dynamic_limit, ready_count)
        if isinstance(ready_count, int)
        else dynamic_limit
    )
    reported_target = _int(reported_coverage.get("dynamic_target"))
    if reported_target != expected_target:
        errors.append("candidate_coverage.dynamic_target:mismatch")
    dynamic_floor = min(3, expected_target)
    actual_count = len(entries)
    valid_count = len(valid_entries)
    effective_count = valid_count if relaxed_policy else actual_count
    utilization = (
        round(effective_count / expected_target, 6) if expected_target else None)
    stop_reason = str(reported_coverage.get("stop_reason") or "").strip()
    if stop_reason not in COMPLETE_REASONS:
        errors.append("candidate_coverage.stop_reason:invalid")
    if effective_count < expected_target and stop_reason not in SHORTFALL_REASONS:
        errors.append("candidate_coverage.stop_reason:shortfall_unexplained")
    if (
        relaxed_policy and effective_count < expected_target
        and stop_reason == "candidate_shortfall"
    ):
        errors.append("candidate_coverage.candidate_shortfall:unproven")
    if effective_count >= expected_target and stop_reason != "target_reached":
        errors.append("candidate_coverage.stop_reason:target_reached_expected")
    if actual_count > dynamic_limit:
        errors.append("candidate_coverage.actual_count:exceeds_dynamic_limit")

    ready_rotation_symbols = {
        symbol for symbol, candidate in manifest_by_symbol.items()
        if candidate.get("rotation_due") is True
        and (
            not bundle_passed
            or (bundle_by_symbol.get(symbol) or {}).get("ready") is True
        )
    }
    valid_symbols = {entry["symbol"] for entry in valid_entries}
    rotation_required = bool(ready_rotation_symbols and expected_target > 0)
    rotation_satisfied = (
        bool(valid_symbols & ready_rotation_symbols)
        if rotation_required else None
    )
    if rotation_required and rotation_satisfied is not True:
        errors.append("candidate_coverage.rotation:not_satisfied")
    screening_rate = (
        round(screened_count / pool_count, 6) if pool_count else None)
    valid_rate = round(valid_count / actual_count, 6) if actual_count else None
    quality_status = (
        "NOT_MET" if errors or invalid_entries
        else "NOT_MEASURABLE" if expected_target == 0 and effective_count == 0
        else "MET" if effective_count >= expected_target
        else "NOT_MET"
    )
    canonical_coverage = {
        "schema": "candidate_coverage_v2",
        "identity_contract": (
            "candidate_id_v1" if identity_v2 else "legacy_symbol_identity"),
        "dynamic_floor": dynamic_floor,
        "dynamic_limit": dynamic_limit,
        "dynamic_target": expected_target,
        "actual_count": effective_count,
        "reported_count": actual_count,
        "quality_valid_count": valid_count,
        "quality_invalid_count": len(invalid_entries),
        "structure_valid_rate": valid_rate,
        "dynamic_target_utilization": utilization,
        "not_deep_dived_count": (
            max(pool_count - effective_count, 0)
            if isinstance(pool_count, int) else None
        ),
        "stop_reason": stop_reason,
        "rotation_required": rotation_required,
        "rotation_satisfied": rotation_satisfied,
        "rotation_eligible_symbols": sorted(ready_rotation_symbols),
        "screening_coverage_rate": screening_rate,
        "quality_status": quality_status,
        "reason_family_counts": {
            family: sum(
                entry.get("reason_family") == family for entry in entries)
            for family in sorted(REASON_FAMILIES)
            if any(entry.get("reason_family") == family for entry in entries)
        },
        "rejection_reason_family_counts": {
            family: sum(
                entry.get("reason_family") == family
                and entry.get("decision") in {
                    "reject", "rejected", "drop", "dropped"}
                for entry in entries)
            for family in sorted(REASON_FAMILIES)
            if any(
                entry.get("reason_family") == family
                and entry.get("decision") in {
                    "reject", "rejected", "drop", "dropped"}
                for entry in entries)
        },
        "watch_reason_family_counts": {
            family: sum(
                entry.get("reason_family") == family
                and entry.get("decision") in {"watch", "wait"}
                for entry in entries)
            for family in sorted(REASON_FAMILIES)
            if any(
                entry.get("reason_family") == family
                and entry.get("decision") in {"watch", "wait"}
                for entry in entries)
        },
        "errors": errors,
        "invalid_entries": invalid_entries,
    }
    inner["candidate_coverage"] = canonical_coverage
    if effective_count < expected_target:
        inner["candidate_evidence_shortfall"] = {
            "observed_candidate_count": pool_count,
            "dynamic_target": expected_target,
            "actual_count": effective_count,
            "reason": stop_reason,
        }
    else:
        inner.pop("candidate_evidence_shortfall", None)

    identity_enforced = thresholds.candidate_exact_identity_enforced(cycle_id)
    identity_by_key = {
        (entry["symbol"], entry["side"]): entry
        for entry in entries if entry.get("identity_valid") is True
    }
    identity_rejected_open_signals: list[dict] = []
    if identity_enforced:
        retained_signals = []
        for index, signal_value in enumerate(safe_signals):
            if not isinstance(signal_value, dict):
                retained_signals.append(signal_value)
                continue
            action = str(signal_value.get("action") or "").strip().lower()
            if action not in {"open_long", "open_short"}:
                retained_signals.append(signal_value)
                continue
            symbol = str(signal_value.get("symbol") or "").strip().upper()
            side = "long" if action == "open_long" else "short"
            entry = identity_by_key.get((symbol, side))
            signal_hash = _signal_evidence_hash(signal_value)
            supplied_candidate_id = str(
                signal_value.get("candidate_id") or "").strip().lower()
            rejection = None
            if entry is None:
                rejection = "exact_candidate_identity_missing_or_invalid"
            elif not supplied_candidate_id:
                rejection = "exact_candidate_id_signal_missing"
            elif signal_hash != entry.get("evidence_hash"):
                rejection = "exact_candidate_identity_evidence_hash_mismatch"
            elif supplied_candidate_id and supplied_candidate_id != entry.get(
                    "candidate_id"):
                rejection = "exact_candidate_id_signal_mismatch"
            if rejection:
                identity_rejected_open_signals.append({
                    "index": index,
                    "symbol": symbol or None,
                    "side": side,
                    "reason": rejection,
                })
                continue
            retained_signals.append({
                **signal_value,
                "candidate_id": entry.get("candidate_id"),
                "opportunity_id": entry.get("opportunity_id"),
                "candidate_identity_contract": (
                    "candidate_id_v1_exact_manifest_no_alias"),
            })
        safe_signals = retained_signals
    inner["candidate_identity"] = {
        "schema": "candidate_exact_identity_enforcement_v1",
        "active": identity_enforced,
        "identity_contract": (
            "candidate_id_v1_exact_manifest_no_alias"),
        "identity_valid_deep_dives": len(identity_by_key),
        "rejected_open_signals": identity_rejected_open_signals,
        "non_open_actions_affected": False,
    }

    soft_shadow_active = thresholds.side_regime_soft_veto_shadow_active(
        cycle_id)
    counterfactuals: list[dict] = []
    excluded_counts = {
        "short_strict": 0,
        "unsupported_layer_reason_pair": 0,
        "quality_invalid": sum(
            entry.get("quality_valid") is not True for entry in entries),
        "canonical_context_missing": 0,
        "non_rejection_decision": 0,
    }
    if soft_shadow_active:
        for entry in valid_entries:
            family = entry.get("reason_family")
            layer = entry.get("layer")
            side = entry.get("side")
            if entry.get("decision") not in {
                    "reject", "rejected", "drop", "dropped"}:
                excluded_counts["non_rejection_decision"] += 1
                continue
            if side == "short" and family in {
                    "LOWER_TIMEFRAME_AGAINST", "ENTRY_EXTENDED"}:
                excluded_counts["short_strict"] += 1
                continue
            supported = (
                layer == "early" and side == "long"
                and family == "LOWER_TIMEFRAME_AGAINST"
            ) or (
                layer == "mature" and side == "long"
                and family == "ENTRY_EXTENDED"
            )
            if not supported:
                excluded_counts["unsupported_layer_reason_pair"] += 1
                continue
            bundle_item = bundle_by_id.get(entry.get("candidate_id"))
            contract = (
                bundle_item.get("evidence_contract")
                if isinstance(bundle_item, dict) else None)
            timeframe = (
                (contract.get("timeframes") or {}).get("15m")
                if isinstance(contract, dict) else None)
            values = timeframe.get("values") if isinstance(timeframe, dict) else None
            anchor_source = "sealed_candidate_bundle"
            if not isinstance(timeframe, dict) or not isinstance(values, dict):
                ready_item = ready_pool_by_symbol.get(
                    str(entry.get("symbol") or "").upper())
                metric = (
                    (ready_item.get("timeframes") or {}).get("15m")
                    if isinstance(ready_item, dict) else None)
                if isinstance(metric, dict):
                    timeframe = {
                        "observed_bar_ts": metric.get("ts"),
                        "values": {"c": metric.get("close")},
                    }
                    values = timeframe["values"]
                    anchor_source = "hashed_ready_pool"
            if (
                not entry.get("opportunity_id")
                or not entry.get("regime_current")
                or not isinstance(timeframe, dict)
                or not isinstance(values, dict)
            ):
                excluded_counts["canonical_context_missing"] += 1
                continue
            counterfactuals.append({
                "opportunity_id": entry.get("opportunity_id"),
                "candidate_id": entry.get("candidate_id"),
                "symbol": entry.get("symbol"),
                "side": side,
                "layer": layer,
                "opportunity_state": entry.get("opportunity_state"),
                "regime_first_seen": entry.get("regime_first_seen"),
                "regime_current": entry.get("regime_current"),
                "reason_family": family,
                "reason_code": entry.get("reason_code"),
                "evidence_hash": entry.get("evidence_hash"),
                "anchor": {
                    "timeframe": "15m",
                    "bar_ts": timeframe.get("observed_bar_ts"),
                    "close": values.get("c"),
                    "source": anchor_source,
                },
                "counterfactual_disposition": "continue_review_only",
                "outcome_status": "PENDING_FORWARD_OBSERVATION",
            })
    inner["side_regime_soft_veto_shadow"] = {
        "schema": "side_regime_soft_veto_shadow_v1",
        "active": soft_shadow_active,
        "mode": "shadow_observe_only",
        "policy_id": "long_timing_veto_observe_v1",
        "cycle_id": cycle_id,
        "counterfactuals": counterfactuals,
        "excluded_counts": excluded_counts,
        "signals_mutated": False,
        "signal_write_authority": False,
        "order_authority": False,
        "scheduler_authority": False,
    }

    valid_by_key = {
        (entry["symbol"], entry["side"]): entry for entry in valid_entries}
    rejected_open_signals: list[dict] = []
    if phase == "consume" and not relaxed_policy:
        retained_signals = []
        for index, signal_value in enumerate(safe_signals):
            if not isinstance(signal_value, dict):
                retained_signals.append(signal_value)
                continue
            action = str(signal_value.get("action") or "").strip().lower()
            if action not in {"open_long", "open_short"}:
                retained_signals.append(signal_value)
                continue
            symbol = str(signal_value.get("symbol") or "").strip().upper()
            side = "long" if action == "open_long" else "short"
            entry = valid_by_key.get((symbol, side))
            signal_hash = _signal_evidence_hash(signal_value)
            rejection = None
            if entry is None:
                rejection = "quality_valid_deep_dive_missing"
            elif signal_hash != entry.get("evidence_hash"):
                rejection = "open_signal_evidence_hash_mismatch"
            if rejection:
                rejected_open_signals.append({
                    "index": index,
                    "symbol": symbol or None,
                    "side": side,
                    "reason": rejection,
                })
            else:
                retained_signals.append(signal_value)
        safe_signals = retained_signals
    inner["candidate_quality"] = {
        "schema": QUALITY_SCHEMA,
        "phase": phase,
        "status": quality_status,
        "quality_valid_deep_dives": valid_count,
        "reported_deep_dives": actual_count,
        "rejected_open_signals": rejected_open_signals,
        "identity_rejected_open_signals": identity_rejected_open_signals,
        "position_exit_path_blocked": False,
        "production_database_writes": 0,
        "orders_placed": 0,
        "open_filter_active": phase == "consume" and not relaxed_policy,
        "owner_policy": (
            thresholds.DECISION_RESTRICTION_REMOVAL_POLICY
            if relaxed_policy else "legacy"),
    }
    policy_blocking_errors = []
    if relaxed_policy:
        policy_blocking_errors.extend(
            error for error in errors
            if error.startswith("candidate_coverage."))
        for invalid in invalid_entries:
            policy_blocking_errors.extend(
                f"candidate[{invalid['index']}]:{error}"
                for error in invalid.get("errors", [])
                if error.startswith("entry_ready_"))
    return inner, safe_signals, {
        "schema": QUALITY_SCHEMA,
        "phase": phase,
        "status": quality_status,
        "errors": errors,
        "rejected_open_signals": rejected_open_signals,
        "identity_rejected_open_signals": identity_rejected_open_signals,
        "identity_enforcement_active": identity_enforced,
        "soft_veto_shadow_active": soft_shadow_active,
        "screening": canonical_screening,
        "coverage": canonical_coverage,
        "policy_blocking_errors": policy_blocking_errors,
    }
