# -*- coding: utf-8 -*-
"""Pre-registered, forward-only acceptance-threshold migrations.

单一事实源：任何验收口径变更都在这里登记**一次**预注册激活边界，消费脚本按本
次运行的 ``as_of`` 解析当次判定阈值。边界只向前生效——边界之前的运行仍按老口径
判定，已归档的证据文件不重算、不重判，也不回填历史窗口的结论。

同时提供「按老口径的达成率」诊断助手：闸门取新值，老值继续外显，长期信息不丢。

本模块只有常量与纯函数：不读库、不写文件、不发请求、不下单。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping


CST = timezone(timedelta(hours=8))

# 2026-08-15 主人拍板（《OKX 目标任务提示词 V2.1》§1/§3）：数据完善率与报告/
# 推送完整度四族审计闸门 99% → 95%。边界=本批部署后的第一个整点，只向前生效。
COVERAGE_TARGET_ACTIVATION_CST = "2026-08-15T20:00:00+08:00"
COVERAGE_LEGACY_TARGET_RATE = 0.99
COVERAGE_TARGET_RATE = 0.95

# 2026-08-15 主人拍板（同上 §2）：前向影子标签校准门 90% → 80%。点精度与
# Wilson 95% 下界是孪生阈值，必须同步下调，否则门实际仍卡在 90%。
SHADOW_CALIBRATION_ACTIVATION_CST = "2026-08-15T20:00:00+08:00"
SHADOW_LEGACY_TARGET_PRECISION = 0.90
SHADOW_TARGET_PRECISION = 0.80


def parse_cst(value: str | datetime) -> datetime:
    """Parse a CST timestamp; naive input is interpreted as Beijing time."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def _resolve(
    as_of: str | datetime,
    *,
    activation_cst: str,
    legacy_value: float,
    migrated_value: float,
) -> float:
    """Return the threshold in force for a run performed at ``as_of``."""
    return (
        migrated_value
        if parse_cst(as_of) >= parse_cst(activation_cst)
        else legacy_value
    )


def coverage_target_rate(as_of: str | datetime) -> float:
    """完善率/完整度四族闸门：激活边界起 0.95，之前仍是 0.99。"""
    return _resolve(
        as_of,
        activation_cst=COVERAGE_TARGET_ACTIVATION_CST,
        legacy_value=COVERAGE_LEGACY_TARGET_RATE,
        migrated_value=COVERAGE_TARGET_RATE,
    )


def shadow_target_precision(as_of: str | datetime) -> float:
    """前向影子标签校准门（点精度与 Wilson 下界共用同一数值）。"""
    return _resolve(
        as_of,
        activation_cst=SHADOW_CALIBRATION_ACTIVATION_CST,
        legacy_value=SHADOW_LEGACY_TARGET_PRECISION,
        migrated_value=SHADOW_TARGET_PRECISION,
    )


def coverage_migration_facts(as_of: str | datetime) -> dict[str, Any]:
    """Payload block proving which caliber judged this run, and why."""
    return {
        "activation_cst": COVERAGE_TARGET_ACTIVATION_CST,
        "activated": (
            parse_cst(as_of) >= parse_cst(COVERAGE_TARGET_ACTIVATION_CST)),
        "legacy_target_rate": COVERAGE_LEGACY_TARGET_RATE,
        "migrated_target_rate": COVERAGE_TARGET_RATE,
        "effective_target_rate": coverage_target_rate(as_of),
        "semantics": (
            "forward_only; runs before the boundary keep the legacy caliber; "
            "archived evidence is never recomputed or re-judged"
        ),
    }


def shadow_migration_facts(as_of: str | datetime) -> dict[str, Any]:
    """Payload block for the forward calibration gate migration."""
    return {
        "activation_cst": SHADOW_CALIBRATION_ACTIVATION_CST,
        "activated": (
            parse_cst(as_of)
            >= parse_cst(SHADOW_CALIBRATION_ACTIVATION_CST)),
        "legacy_target_precision": SHADOW_LEGACY_TARGET_PRECISION,
        "migrated_target_precision": SHADOW_TARGET_PRECISION,
        "effective_target_precision": shadow_target_precision(as_of),
        "twin_thresholds": (
            "point precision and Wilson 95% lower bound move together"
        ),
        "semantics": (
            "forward_only; passing the gate stays "
            "MET_FORWARD_SHADOW_REQUIRES_RISK_APPROVAL"
        ),
    }


def legacy_rate_diagnostics(
    rates: Mapping[str, Any],
    *,
    target_dependent: Iterable[str] = (),
) -> dict[str, Any]:
    """按老口径 0.99 复判已发布的率，作诊断列，不参与闸门。

    ``target_dependent`` 列出那些自身计算就依赖当次阈值的率（例如逐槽
    ``slot_pass_rate``）：它们在新旧口径下不同源，拿去和 0.99 比会是苹果对橘
    子，因此显式排除并点名，而不是悄悄照比。
    """
    excluded = sorted({str(name) for name in target_dependent})
    comparable = {
        str(name): (
            None if value is None else bool(float(value) >= COVERAGE_LEGACY_TARGET_RATE)
        )
        for name, value in rates.items()
        if str(name) not in set(excluded)
    }
    return {
        "legacy_target_rate": COVERAGE_LEGACY_TARGET_RATE,
        "rates_at_least_legacy_target": comparable,
        "all_comparable_rates_at_least_legacy_target": (
            all(value is True for value in comparable.values())
            if comparable else None
        ),
        "target_dependent_rates_excluded": excluded,
        "diagnostic_only": True,
    }
