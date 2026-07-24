# -*- coding: utf-8 -*-
"""Shared contract for agent-owned market decisions.

Market signals, rankings, regimes, news and historical outcomes are evidence,
not deterministic gates.  This module validates that an agent made the
evidence and its own judgement auditable; it does not decide whether to trade.
"""
from __future__ import annotations

from typing import Any


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
