# -*- coding: utf-8 -*-
"""Shared contract for agent-owned market decisions.

Market signals, rankings, regimes, news and historical outcomes are evidence,
not deterministic gates.  This module validates that an agent made the
evidence and its own judgement auditable; it does not decide whether to trade.
"""
from __future__ import annotations

import math
from typing import Any

try:
    from .multitimeframe_gate import validate_evidence_contract
except ImportError:  # top-level import used by order_executor's core sys.path
    from multitimeframe_gate import validate_evidence_contract


PROTOCOL = "decision_card_v1"
LIGHTWEIGHT_OPEN_CONTRACT = "lightweight_open_v1"
MINIMAL_DECISION_PROTOCOL = "minimal_decision_v2"
OPEN_EXECUTION_PACKAGE_KEY = "open_execution_package"
OPEN_EXECUTION_PACKAGE_CONTRACT = "open_execution_package_v1"
OPEN_EXECUTION_PACKAGE_FIELDS = frozenset({
    "contract", "entry", "stop", "target", "exit_mode",
})
CLOSURE_RETIRED_STRUCTURE_KEYS = frozenset({
    "decision_card",
    "lightweight_open_v1",
    "open_execution_packages",
    "candidate_id",
    "candidate_identity",
    "candidate_identity_contract",
    "review_hash",
    "review_candidate_ids",
    "opportunity_id",
    "opportunity_state",
    "opportunity_state_default_authorized",
    "opportunity_state_priority",
    "state_version",
    "mature_eligible",
    "early_eligible",
    "previous_state",
    "entry_timing",
    "trend_strength",
    "timeframe_judgment_used",
    "required_timeframes",
})
CORE_FIELDS = (
    "direction_evidence",
    "opposing_evidence",
    "execution_conditions",
    "invalidation_point",
    "risk_reward",
    "portfolio_impact",
)
HISTORY_USAGE = {"adopt", "partial", "ignore", "none"}
EXIT_MODES = {"fixed_tp", "dynamic_exit", "no_fixed_tp"}
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


def is_lightweight_open_card(card: Any) -> bool:
    return (
        isinstance(card, dict)
        and card.get("contract") == LIGHTWEIGHT_OPEN_CONTRACT
    )


def is_open_execution_package(package: Any) -> bool:
    """Return whether *package* is the exact flat closure wire contract."""
    return (
        isinstance(package, dict)
        and not validate_open_execution_package(package)
    )


def _positive_finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def validate_lightweight_open_card(
    card: Any,
    path: str = "decision_card",
) -> list[str]:
    """Validate the owner-approved minimal OPEN contract."""
    if not isinstance(card, dict):
        return [f"{path} 必须是 dict"]
    errors: list[str] = []
    if card.get("contract") != LIGHTWEIGHT_OPEN_CONTRACT:
        errors.append(
            f"{path}.contract 必须是 {LIGHTWEIGHT_OPEN_CONTRACT}")
    side = str(card.get("side") or "").strip().lower()
    if side not in {"long", "short"}:
        errors.append(f"{path}.side 必须是 long|short")
    if not _present(card.get("reasoning")):
        errors.append(f"{path}.reasoning 不能为空")
    risk_reward = card.get("risk_reward")
    if not isinstance(risk_reward, dict):
        return errors + [f"{path}.risk_reward 必须是 dict"]
    entry = _positive_finite(risk_reward.get("entry"))
    stop = _positive_finite(risk_reward.get("stop"))
    target = _positive_finite(risk_reward.get("target"))
    if entry is None or stop is None or target is None:
        errors.append(
            f"{path}.risk_reward 必须含正有限 entry/stop/target")
    elif side == "long" and not stop < entry < target:
        errors.append(
            f"{path}.risk_reward long 必须满足 stop<entry<target")
    elif side == "short" and not target < entry < stop:
        errors.append(
            f"{path}.risk_reward short 必须满足 target<entry<stop")
    exit_mode = str(risk_reward.get("exit_mode") or "").strip().lower()
    if exit_mode not in EXIT_MODES:
        errors.append(
            f"{path}.risk_reward.exit_mode 必须是 "
            "fixed_tp|dynamic_exit|no_fixed_tp")
    return errors


def validate_open_execution_package(
    package: Any,
    path: str = OPEN_EXECUTION_PACKAGE_KEY,
    *,
    expected_side: str | None = None,
) -> list[str]:
    """Validate the exact flat OPEN/ADD machine package.

    Side and reasoning belong to the action/receipt, not this package.  An
    exact key set prevents legacy card prose such as ``news_context`` or
    ``regime_scope`` from leaking back into the execution wire format.
    """
    if not isinstance(package, dict):
        return [f"{path} 必须是 dict"]
    errors: list[str] = []
    actual_fields = set(package)
    missing = sorted(OPEN_EXECUTION_PACKAGE_FIELDS - actual_fields)
    extras = sorted(actual_fields - OPEN_EXECUTION_PACKAGE_FIELDS)
    if missing:
        errors.append(f"{path} 缺少字段: {','.join(missing)}")
    if extras:
        errors.append(f"{path} 禁止额外字段: {','.join(extras)}")
    if package.get("contract") != OPEN_EXECUTION_PACKAGE_CONTRACT:
        errors.append(
            f"{path}.contract 必须是 {OPEN_EXECUTION_PACKAGE_CONTRACT}")
    entry = _positive_finite(package.get("entry"))
    stop = _positive_finite(package.get("stop"))
    target = _positive_finite(package.get("target"))
    if entry is None or stop is None or target is None:
        errors.append(f"{path} 必须含正有限 entry/stop/target")
    side = str(expected_side or "").strip().lower()
    if side and side not in {"long", "short"}:
        errors.append(f"{path} expected_side 必须是 long|short")
    elif entry is not None and stop is not None and target is not None:
        if side == "long" and not stop < entry < target:
            errors.append(f"{path} long 必须满足 stop<entry<target")
        elif side == "short" and not target < entry < stop:
            errors.append(f"{path} short 必须满足 target<entry<stop")
    exit_mode = str(package.get("exit_mode") or "").strip().lower()
    if exit_mode not in EXIT_MODES:
        errors.append(
            f"{path}.exit_mode 必须是 fixed_tp|dynamic_exit|no_fixed_tp")
    return errors


def canonical_open_execution_package(payload: Any) -> dict[str, Any] | None:
    """Convert the legacy analysis-column card to the flat closure package.

    The compatibility conversion exists only at trusted read boundaries.
    Existing historical rows/journals keep their frozen ``lightweight_open_v1``
    value; no historical data is rewritten.
    """
    if is_open_execution_package(payload):
        return {
            "contract": payload["contract"],
            "entry": payload["entry"],
            "stop": payload["stop"],
            "target": payload["target"],
            "exit_mode": payload["exit_mode"],
        }
    if not is_lightweight_open_card(payload):
        return None
    if validate_lightweight_open_card(payload):
        return None
    risk_reward = payload.get("risk_reward")
    if not isinstance(risk_reward, dict):
        return None
    package = {
        "contract": OPEN_EXECUTION_PACKAGE_CONTRACT,
        "entry": risk_reward.get("entry"),
        "stop": risk_reward.get("stop"),
        "target": risk_reward.get("target"),
        "exit_mode": risk_reward.get("exit_mode"),
    }
    return package if not validate_open_execution_package(
        package, expected_side=payload.get("side")) else None


def closure_retired_structure_paths(
    value: Any,
    path: str = "$",
) -> list[str]:
    """Locate retired machine structures without scanning prose values."""
    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            lowered = key.lower()
            child_path = f"{path}.{key}"
            if (
                lowered in CLOSURE_RETIRED_STRUCTURE_KEYS
                or "multitimeframe" in lowered
                or (
                    lowered == "contract"
                    and str(child or "").strip().lower()
                    == LIGHTWEIGHT_OPEN_CONTRACT
                )
            ):
                found.append(child_path)
            found.extend(closure_retired_structure_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(
                closure_retired_structure_paths(child, f"{path}[{index}]"))
    return found


def scrub_closure_retired_structures(value: Any) -> Any:
    """Remove retired machine structures while preserving ordinary prose."""
    if isinstance(value, dict):
        if str(value.get("contract") or "").strip().lower() \
                == LIGHTWEIGHT_OPEN_CONTRACT:
            return {}
        cleaned: dict[Any, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if (
                lowered in CLOSURE_RETIRED_STRUCTURE_KEYS
                or "multitimeframe" in lowered
                or (
                    lowered == "contract"
                    and str(child or "").strip().lower()
                    == LIGHTWEIGHT_OPEN_CONTRACT
                )
            ):
                continue
            cleaned[raw_key] = scrub_closure_retired_structures(child)
        return cleaned
    if isinstance(value, list):
        return [scrub_closure_retired_structures(child) for child in value]
    return value


def validate_card(
    card: Any,
    path: str = "decision_card",
    *,
    require_exit_mode: bool = False,
) -> list[str]:
    """Validate audit completeness without turning evidence into trade gates.

    ``require_exit_mode``（2026-08-20）：``open_*`` 卡必须**显式**给出
    ``risk_reward.exit_mode``。此前本函数只在该键存在时校验取值，缺键一律
    放行，而手册（agents/live_trader.md）早已把它列为 open_* 必填 —— 规则
    因此只活在 ``analyst_writer`` 的本地检查里，``trades_writer`` /
    ``order_executor`` / ``ledger_autoheal`` 三个共用本函数的写方全都不知道
    它（与 G6「必需源集合被第二次手写」同一类缺陷）。

    刻意做成 **opt-in** 而不是无条件必填：``ledger_autoheal`` 复放的是库里
    的历史卡，2026-08-14 之前的卡合法地没有这个键，无条件必填会让补账对
    老仓直接失败、把交易冻在那里。调用方自己知道「这是不是一张新开仓卡」。
    """
    if is_lightweight_open_card(card):
        return validate_lightweight_open_card(card, path)
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
    risk_reward = card.get("risk_reward")
    if isinstance(risk_reward, dict) and "exit_mode" in risk_reward:
        exit_mode = str(risk_reward.get("exit_mode") or "").strip().lower()
        if exit_mode not in EXIT_MODES:
            errors.append(
                f"{path}.risk_reward.exit_mode 必须是 "
                "fixed_tp|dynamic_exit|no_fixed_tp"
            )
    elif require_exit_mode:
        # 缺键与取值非法要分开报：前者是「没写」，后者是「写错」，
        # 排查方向完全不同（一个查 Agent 输出契约，一个查枚举）。
        errors.append(
            f"{path}.risk_reward.exit_mode 开仓卡必须显式给出"
            "（fixed_tp|dynamic_exit|no_fixed_tp），当前缺失"
        )
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
            f"{block_path}.calibrated_confidence 在当前独立前向门通过且主人"
            "另行风险批准前必须为 null"
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
