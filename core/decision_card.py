# -*- coding: utf-8 -*-
"""Shared contract for agent-owned market decisions.

Market signals, rankings, regimes, news and historical outcomes are evidence,
not deterministic gates.  This module validates that an agent made the
evidence and its own judgement auditable; it does not decide whether to trade.
"""
from __future__ import annotations

from typing import Any

try:
    from .multitimeframe_gate import validate_evidence_contract
except ImportError:  # top-level import used by order_executor's core sys.path
    from multitimeframe_gate import validate_evidence_contract


PROTOCOL = "decision_card_v1"
CORE_FIELDS = (
    "direction_evidence",
    "opposing_evidence",
    "execution_conditions",
    "invalidation_point",
    "risk_reward",
    "portfolio_impact",
)
HISTORY_USAGE = {"adopt", "partial", "ignore", "none"}
DECISION_TIMEFRAMES = ("15m", "1H", "4H")
TIMEFRAME_DIRECTIONS = {"long", "short", "neutral"}
MULTITIMEFRAME_SELECTION_METHOD = (
    "relative_rank_1_among_15m_1H_4H_not_calibrated"
)


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def validate_card(card: Any, path: str = "decision_card") -> list[str]:
    """Validate audit completeness without turning evidence into trade gates."""
    errors: list[str] = []
    if not isinstance(card, dict):
        return [f"{path} 必须是 dict"]

    for key in CORE_FIELDS:
        if key not in card:
            errors.append(f"{path} 缺少 {key}")
        elif not _present(card[key]):
            errors.append(f"{path}.{key} 不能为空")

    history = card.get("historical_experience")
    if not isinstance(history, dict):
        errors.append(f"{path}.historical_experience 必须是 dict")
    else:
        for key in ("matched_wins", "matched_losses", "missed_opportunities"):
            if key not in history or not isinstance(history.get(key), list):
                errors.append(f"{path}.historical_experience.{key} 必须是 list")
        usage = str(history.get("usage") or "").lower()
        if usage not in HISTORY_USAGE:
            errors.append(
                f"{path}.historical_experience.usage 必须是 "
                "adopt|partial|ignore|none"
            )
        if not _present(history.get("reason")):
            errors.append(f"{path}.historical_experience.reason 不能为空")

    if not _present(card.get("agent_judgement")):
        errors.append(f"{path}.agent_judgement 不能为空")
    overrides = card.get("reference_overrides")
    if overrides is None or not isinstance(overrides, list):
        errors.append(f"{path}.reference_overrides 必须是 list（无覆盖时填 []）")
    return errors


def validate_multitimeframe_analysis(
    card: Any,
    path: str = "decision_card",
    *,
    expected_cycle: str | None = None,
    expected_side: str | None = None,
    expected_symbol: str | None = None,
) -> list[str]:
    """Validate the structured three-timeframe selection for OPEN/ADD.

    This is deliberately an audit contract, not a confidence estimator.  Until
    the independent forward gate is proven, the card must keep calibrated
    confidence null and claim permission false.  ``relative_rank`` is ordinal
    only: it proves which of the three explicit analyses the agent selected;
    it must never be displayed as a 90% probability.
    """
    if not isinstance(card, dict):
        return [f"{path} 必须是 dict"]
    block = card.get("multitimeframe_analysis")
    block_path = f"{path}.multitimeframe_analysis"
    if not isinstance(block, dict):
        return [f"{block_path} 必须是 dict（OPEN/ADD 必填）"]

    errors: list[str] = []
    cycle = block.get("cycle_id")
    if not _present(cycle):
        errors.append(f"{block_path}.cycle_id 不能为空")
    elif expected_cycle is not None and str(cycle) != str(expected_cycle):
        errors.append(
            f"{block_path}.cycle_id={cycle!r} 与本轮 {expected_cycle!r} 不一致"
        )

    required = block.get("required_timeframes")
    if required != list(DECISION_TIMEFRAMES):
        errors.append(
            f"{block_path}.required_timeframes 必须严格为 "
            f"{list(DECISION_TIMEFRAMES)!r}"
        )

    timeframes = block.get("timeframes")
    if not isinstance(timeframes, dict):
        errors.append(f"{block_path}.timeframes 必须是 dict")
        timeframes = {}
    elif set(timeframes) != set(DECISION_TIMEFRAMES):
        errors.append(
            f"{block_path}.timeframes 必须且只能包含 15m/1H/4H"
        )

    ranks: list[int] = []
    directions: dict[str, str] = {}
    for timeframe in DECISION_TIMEFRAMES:
        row = timeframes.get(timeframe)
        row_path = f"{block_path}.timeframes.{timeframe}"
        if not isinstance(row, dict):
            errors.append(f"{row_path} 必须是 dict")
            continue
        direction = str(row.get("direction") or "").strip().lower()
        if direction not in TIMEFRAME_DIRECTIONS:
            errors.append(f"{row_path}.direction 必须是 long|short|neutral")
        else:
            directions[timeframe] = direction
        evidence = row.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(not _present(item) for item in evidence)
        ):
            errors.append(f"{row_path}.evidence 必须是非空证据 list")
        rank = row.get("relative_rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank not in (1, 2, 3):
            errors.append(f"{row_path}.relative_rank 必须是整数 1|2|3")
        else:
            ranks.append(rank)
    if len(ranks) == len(DECISION_TIMEFRAMES) and sorted(ranks) != [1, 2, 3]:
        errors.append(
            f"{block_path} 三个 relative_rank 必须恰为 1,2,3（1=相对最高）"
        )

    selected_timeframe = str(block.get("selected_timeframe") or "")
    if selected_timeframe not in DECISION_TIMEFRAMES:
        errors.append(
            f"{block_path}.selected_timeframe 必须是 15m|1H|4H"
        )
    else:
        selected_row = timeframes.get(selected_timeframe)
        if (
            isinstance(selected_row, dict)
            and selected_row.get("relative_rank") != 1
        ):
            errors.append(
                f"{block_path}.selected_timeframe 必须指向 relative_rank=1"
            )

    selected_direction = str(
        block.get("selected_direction") or ""
    ).strip().lower()
    if selected_direction not in {"long", "short"}:
        errors.append(f"{block_path}.selected_direction 必须是 long|short")
    if expected_side is not None and selected_direction != str(expected_side).lower():
        errors.append(
            f"{block_path}.selected_direction={selected_direction!r} "
            f"与 OPEN/ADD side={expected_side!r} 不一致"
        )
    if (
        selected_timeframe in directions
        and selected_direction
        and directions[selected_timeframe] != selected_direction
    ):
        errors.append(
            f"{block_path}.selected_direction 必须与所选周期 direction 一致"
        )

    if not _present(block.get("selection_reason")):
        errors.append(f"{block_path}.selection_reason 不能为空")
    if block.get("selection_method") != MULTITIMEFRAME_SELECTION_METHOD:
        errors.append(
            f"{block_path}.selection_method 必须是 "
            f"{MULTITIMEFRAME_SELECTION_METHOD!r}"
        )
    if block.get("calibrated_confidence") is not None:
        errors.append(
            f"{block_path}.calibrated_confidence 在独立90%门通过前必须为 null"
        )
    if block.get("confidence_claim_allowed") is not False:
        errors.append(
            f"{block_path}.confidence_claim_allowed 必须为 false"
        )
    contract_errors = validate_evidence_contract(
        block.get("evidence_contract"),
        expected_symbol=expected_symbol,
        expected_cycle=expected_cycle,
    )
    errors.extend(
        f"{block_path}.evidence_contract: {item}"
        for item in contract_errors
    )
    return errors


def compact_text(value: Any, limit: int = 180) -> str:
    """Turn a card field into compact human-readable text for briefings/push."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(
                    str(
                        item.get("summary")
                        or item.get("evidence")
                        or item.get("reason")
                        or item
                    )
                )
            else:
                parts.append(str(item))
        text = "；".join(parts)
    elif isinstance(value, dict):
        parts = [
            f"{key}={item}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        ]
        text = "；".join(parts)
    else:
        text = str(value or "")
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return text[:limit]
