# -*- coding: utf-8 -*-
"""Wave1 序5 —— RR/净 EV 确定性计算器（终稿边界表 #1/#4）。

职责：从决策卡 `risk_reward` 的 entry/stop/target 数字重算几何 RR、含摩擦净 RR、
盈亏平衡胜率与 EV，并给出**可拒写的算术一致性错误**。DOT 事故形态（卡字段
rr=1.0、文字"≈0.9"、几何 0.895、用历史队列平均收益偷换本笔 EV 后结论"微正"）
在此被机械终结。

边界纪律（主人 2026-08-10 拍板）：
  - 硬拒写只针对**算术不一致**（rr 字段 vs 几何、方向几何非法、数字缺失）；
  - 负 EV **不拒单**：要求结构化 `risk_reward.ev_override`（reason + p_win_claim），
    模型可以做不受欢迎的判断，但必须显式承认基线并给出修正后的胜率；
  - p_win 只取 evidence_contract 三个具名 scope 的 wins/n（首个 n≥5 的），
    **禁止**用 avg_pnl_pct 冒充概率；样本不足 → status=indeterminate，无 EV 闸。

摩擦假设与 risk_validator 单一真源（RISK_FEE_BUFFER_PCT + RISK_SLIPPAGE_BUFFER_PCT，
共 0.2% 名义/回合）：reward_net = reward - 摩擦，risk_net = risk + 摩擦。
纯函数、无 I/O；analyst_writer 调用并把 ev_check 块注入落库卡。
"""
from __future__ import annotations

import math
from typing import Any, Optional

try:
    from core.risk_validator import (
        RISK_FEE_BUFFER_PCT,
        RISK_SLIPPAGE_BUFFER_PCT,
    )
except ImportError:  # executor 语境：core/ 目录裸导入风格
    from risk_validator import (  # type: ignore[no-redef]
        RISK_FEE_BUFFER_PCT,
        RISK_SLIPPAGE_BUFFER_PCT,
    )

EV_CHECK_VERSION = "ev_calculator_v2"
RR_FIELD_TOLERANCE = 0.05      # 卡 rr 字段与几何 gross_rr 的绝对容差
MIN_P_SAMPLE_N = 5             # p_win 可用的最小 scope 样本量
FRICTION_PCT = RISK_FEE_BUFFER_PCT + RISK_SLIPPAGE_BUFFER_PCT

_P_SCOPE_ORDER = (
    ("exact_setup", "exact_setup"),
    ("same_symbol_similar", "same_symbol_similar"),
    ("cross_symbol_similar", "cross_symbol_similar"),
)


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def wilson_ci95(wins: int, n: int) -> tuple[float, float]:
    """95% Wilson 置信区间（对小样本不撒谎的标准做法）。"""
    if n <= 0:
        return (0.0, 1.0)
    z = 1.959963984540054
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def evidence_p_win(evidence_contract: Any) -> dict[str, Any]:
    """从 evidence_contract 具名 scope 取确定性 p_win；不足样本 → indeterminate。"""
    out: dict[str, Any] = {
        "p_win": None, "p_scope": None, "p_n": None,
        "p_wins": None, "p_ci95": None,
    }
    if not isinstance(evidence_contract, dict):
        return out
    summaries = evidence_contract.get("summaries")
    if not isinstance(summaries, dict):
        return out
    for key, scope_name in _P_SCOPE_ORDER:
        summary = summaries.get(key)
        if not isinstance(summary, dict):
            continue
        n = summary.get("n")
        wins = summary.get("wins")
        if (isinstance(n, int) and isinstance(wins, int)
                and not isinstance(n, bool) and not isinstance(wins, bool)
                and n >= MIN_P_SAMPLE_N and 0 <= wins <= n):
            out.update({
                "p_win": wins / n, "p_scope": scope_name,
                "p_n": n, "p_wins": wins,
                "p_ci95": [round(x, 4) for x in wilson_ci95(wins, n)],
            })
            return out
    return out


def build_ev_check(card: Any, side: str) -> tuple[dict[str, Any], list[str]]:
    """重算一张 open 决策卡的 RR/EV；返回 (ev_check 块, 拒写错误列表)。

    错误列表非空 = 算术/契约不一致，writer 必须拒写；ev_check 块在无错误时
    由 writer 注入卡内（覆盖模型手写的同名块——canonical 值只此一家）。
    """
    errors: list[str] = []
    side = str(side or "").lower()
    if not isinstance(card, dict):
        return {}, ["decision_card 必须是 dict"]
    rr_block = card.get("risk_reward")
    if not isinstance(rr_block, dict):
        return {}, ["risk_reward 必须是含 entry/stop/target 数字的 dict"]

    entry = _num(rr_block.get("entry"))
    stop = _num(rr_block.get("stop"))
    target = _num(rr_block.get("target"))
    if entry is None or stop is None or target is None or entry <= 0:
        return {}, [
            "open 决策卡 risk_reward 必须含数值 entry/stop/target"
            f"（现值 entry={rr_block.get('entry')!r} "
            f"stop={rr_block.get('stop')!r} target={rr_block.get('target')!r}）"
        ]

    if side == "long":
        risk = (entry - stop) / entry
        reward = (target - entry) / entry
        if not (stop < entry < target):
            errors.append(
                f"risk_reward 几何非法：long 需 stop<entry<target，"
                f"现 stop={stop} entry={entry} target={target}")
    elif side == "short":
        risk = (stop - entry) / entry
        reward = (entry - target) / entry
        if not (target < entry < stop):
            errors.append(
                f"risk_reward 几何非法：short 需 target<entry<stop，"
                f"现 target={target} entry={entry} stop={stop}")
    else:
        return {}, [f"side 非法: {side!r}"]
    if errors:
        return {}, errors

    gross_rr = reward / risk
    reward_net = reward - FRICTION_PCT
    risk_net = risk + FRICTION_PCT
    net_rr = reward_net / risk_net if risk_net > 0 else None
    if reward_net <= 0:
        errors.append(
            f"目标距离 {reward:.4%} 不足以覆盖摩擦 {FRICTION_PCT:.2%}"
            "（净回报 ≤0，几何上无法盈利）")
        return {}, errors
    breakeven_p = 1.0 / (1.0 + net_rr)

    claimed_rr = _num(rr_block.get("rr"))
    if claimed_rr is None:
        errors.append("risk_reward.rr 必须是数值，且与 entry/stop/target 几何重算一致")
    elif abs(claimed_rr - gross_rr) > RR_FIELD_TOLERANCE:
        errors.append(
            f"risk_reward.rr={claimed_rr} 与几何重算 gross_rr={gross_rr:.4f} "
            f"矛盾（容差 {RR_FIELD_TOLERANCE}）——按 entry/stop/target 修正 rr "
            "或修正价位后整文件重写")

    history = card.get("historical_experience")
    contract = history.get("evidence_contract") if isinstance(history, dict) \
        else None
    p_info = evidence_p_win(contract)
    ev_r = None
    status = "indeterminate"
    if p_info["p_win"] is not None and net_rr is not None:
        ev_r = p_info["p_win"] * net_rr - (1.0 - p_info["p_win"])
        status = "computed"

    override = rr_block.get("ev_override")
    override_present = isinstance(override, dict)
    claim_ev_r = None
    p_claim = None
    override_reason = None
    if override_present:
        override_reason = str(override.get("reason") or "").strip()
        p_claim = _num(override.get("p_win_claim"))
        if not override_reason:
            errors.append("ev_override.reason 不能为空")
        if p_claim is None or not (0.0 < p_claim < 1.0):
            errors.append(
                f"ev_override.p_win_claim 必须是 (0,1) 数值，"
                f"现值 {override.get('p_win_claim')!r}")
        elif net_rr is not None:
            claim_ev_r = p_claim * net_rr - (1.0 - p_claim)
    if status == "computed" and ev_r is not None and ev_r < 0:
        if not override_present:
            errors.append(
                f"按 {p_info['p_scope']}(n={p_info['p_n']}) 胜率 "
                f"{p_info['p_win']:.2%} 计算 ev_r={ev_r:.3f} 为负"
                f"（盈亏平衡需 {breakeven_p:.2%}）。负 EV 不禁开，但必须提供 "
                "risk_reward.ev_override={reason, p_win_claim}：写明为何本笔"
                "修正后胜率高于历史基线")
        else:
            if (p_claim is not None and p_info["p_win"] is not None
                    and p_claim <= p_info["p_win"]):
                errors.append(
                    "ev_override.p_win_claim 必须高于确定性历史基线 "
                    f"{p_info['p_win']:.2%}；否则不构成对负 EV 的修正"
                )

    ev_check = {
        "version": EV_CHECK_VERSION,
        "status": status,
        "entry": entry, "stop": stop, "target": target, "side": side,
        "risk_pct": round(risk, 6), "reward_pct": round(reward, 6),
        "friction_pct": FRICTION_PCT,
        "gross_rr": round(gross_rr, 4),
        "net_rr": round(net_rr, 4) if net_rr is not None else None,
        "breakeven_p": round(breakeven_p, 4),
        "p_win": (round(p_info["p_win"], 4)
                  if p_info["p_win"] is not None else None),
        "p_scope": p_info["p_scope"],
        "p_n": p_info["p_n"],
        "p_ci95": p_info["p_ci95"],
        "ev_r": round(ev_r, 4) if ev_r is not None else None,
        "override_present": override_present,
        "override_p_win_claim": (
            round(p_claim, 4) if p_claim is not None else None),
        "claim_ev_r": (round(claim_ev_r, 4)
                       if claim_ev_r is not None else None),
        "accepts_negative_ev": (
            claim_ev_r < 0 if claim_ev_r is not None else False),
        "note": (
            "p_win 取 evidence_contract 首个 n≥5 具名 scope 的 wins/n；"
            "历史平均收益不得代替本笔 EV；indeterminate 时无 EV 要求"
        ),
    }
    return ev_check, errors
