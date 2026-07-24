# -*- coding: utf-8 -*-
"""V2.0 §7 契约 A —— 确定性风控闸（纯函数）。

把 live_trader.md §3 现为「LLM 散文 + 手算硬上限」的风控搬成一段纯函数代码：
输入 symbol/side/intended_sz/lev/mark_px/ct_val/lot_sz/equity/可用结算币保证金/现仓 →
算每张保证金、最大张数、校硬上限（单笔保证金≤20% / 可用保证金 / 杠杆≤10x /
名义≥1%）、止损距校验
→ 返回 approved_sz 或拒因。**LLM 物理越不过它**（live 下单唯一路径 order_executor 内部
强制调本闸）。

设计要点（方案 §7）：
  - **纯函数**：无网络 / 无 DB / 无副作用，全部输入显式传入 → 100% 可单测。
  - **现仓权威**：调用方（order_executor）必从 OKX API 取现仓传入；禁 position_snapshots
    GROUP BY（红线 #6）。
  - **勿用 ctVal 直接比硬上限**（红线 #8 / MEMORY §3 bug）：先算「每张保证金 =
    mark_px × ct_val ÷ lev」再比预算。
  - **硬上限 = 模块级常量**（hardcoded）：仅主人改码才动，禁 registry/LLM 改
    （红线「不放宽风控自学」）。
  - **demo**：与 live 使用同一套硬上限和止损要求；只差执行环境，不再作为“无硬上限实验场”。

本模块零模型名 / 零 provider 字段（红线 #1 天然合规）。
"""
from __future__ import annotations

import math
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 硬上限常量（live/demo 同一套；hardcoded，禁外部改 —— 红线「不放宽风控自学」）
# ---------------------------------------------------------------------------
MAX_MARGIN_PCT = 0.20        # 每笔交易保证金占净值 ≤ 20%（越界 → clamp 下调 sz）
MAX_LEVERAGE = 10.0          # 杠杆 ≤ 10x（越界 → reject）
# 同侧暴露写入 math_box 供组合观察，由 Agent 自主裁决，不参与 approve/reject。
MIN_NOTIONAL_PCT = 0.01      # 单笔名义价值 ≥ 1% 净值（不足 → clamp 上调 sz）
MAX_SL_DEVIATION = 0.30      # 止损价偏离 mark_px ≤ 30%（超 → reject，防填错标的）
# 市价单按 mark_px 估保证金，需给价格漂移/手续费留确定性余量。该值不改变「每笔保证金
# ≤20%×净值」语义，只用于第二条独立约束：本笔不得耗尽当前可用结算币保证金。
AVAILABLE_MARGIN_USE_PCT = 0.98

_EPS = 1e-9


# ---------------------------------------------------------------------------
# lot_sz 步进取整
# ---------------------------------------------------------------------------
def _round_down_to_step(value: float, step: float) -> float:
    """向下取整到 step 的整数倍（保证金上限方向：宁少不多）。"""
    if step is None or step <= 0:
        return value
    n = math.floor((value + _EPS) / step)
    return round(n * step, 12)


def _round_up_to_step(value: float, step: float) -> float:
    """向上取整到 step 的整数倍（名义下限方向：宁多不少，确保过下限）。"""
    if step is None or step <= 0:
        return value
    n = math.ceil((value - _EPS) / step)
    return round(n * step, 12)


def _same_side_notional(open_positions: list[dict[str, Any]], side: str,
                        exclude_symbol: Optional[str] = None) -> float:
    """同侧现有名义价值合计（USDT）。

    open_positions 每项需含 'side' 与 'notional'（USDT，调用方从 OKX position
    payload 的 notionalUsd 直接取，或 sz×ctVal×markPx 算）。缺 notional 记 0。
    """
    total = 0.0
    for p in open_positions or []:
        if str(p.get("side", "")).lower() != side:
            continue
        sym = p.get("symbol") or p.get("instId")
        if exclude_symbol is not None and sym == exclude_symbol:
            continue
        n = p.get("notional")
        if n is None:
            n = p.get("notionalUsd")
        try:
            total += abs(float(n)) if n is not None else 0.0
        except (TypeError, ValueError):
            pass
    return total


def _position_open(open_positions: list[dict[str, Any]], symbol: str,
                   side: str) -> bool:
    """该 symbol+side 是否已有现仓（用于判「新开」vs「加仓」）。"""
    for p in open_positions or []:
        sym = p.get("symbol") or p.get("instId")
        if sym == symbol and str(p.get("side", "")).lower() == side:
            return True
    return False


def portfolio_observation(
    open_positions: Optional[list[dict[str, Any]]],
    equity: Optional[float],
) -> dict[str, Any]:
    """组合级只读指标；仅留痕/提示，不参与 approve/reject。

    输入现仓必须沿用调用方的 OKX API 权威数据。notional 缺失记 0，杠杆缺失的保证金
    不臆算，并在 margin_coverage 中显式反映。
    """
    positions = open_positions or []
    long_notional = short_notional = estimated_margin = 0.0
    margin_known = 0
    notionals: list[float] = []
    for p in positions:
        raw = p.get("notional")
        if raw is None:
            raw = p.get("notionalUsd")
        try:
            notional = abs(float(raw)) if raw is not None else 0.0
        except (TypeError, ValueError):
            notional = 0.0
        notionals.append(notional)
        side = str(p.get("side", "")).lower()
        if side == "long":
            long_notional += notional
        elif side == "short":
            short_notional += notional
        try:
            lev = float(p.get("lev") or p.get("lever"))
            if lev > 0 and notional > 0:
                estimated_margin += notional / lev
                margin_known += 1
        except (TypeError, ValueError):
            pass

    gross = long_notional + short_notional
    net = long_notional - short_notional
    try:
        eq_value = float(equity) if equity is not None else 0.0
    except (TypeError, ValueError):
        eq_value = 0.0
    eq = eq_value if eq_value > 0 else None
    same_side_pct = max(long_notional, short_notional) / gross if gross else 0.0
    largest_pct_gross = max(notionals, default=0.0) / gross if gross else 0.0
    warnings: list[str] = []
    if eq and gross / eq >= 3.0:
        warnings.append("gross_exposure_ge_3x_equity")
    if eq and estimated_margin / eq >= 0.40:
        warnings.append("estimated_margin_ge_40pct_equity")
    if len(positions) >= 2 and same_side_pct >= 0.80:
        warnings.append("same_side_concentration_ge_80pct")
    if len(positions) >= 2 and largest_pct_gross >= 0.60:
        warnings.append("largest_position_ge_60pct_gross")
    return {
        "observation_only": True,
        "position_count": len(positions),
        "gross_notional": gross,
        "long_notional": long_notional,
        "short_notional": short_notional,
        "net_notional": net,
        "gross_to_equity": gross / eq if eq else None,
        "net_to_equity": net / eq if eq else None,
        "estimated_margin": estimated_margin,
        "estimated_margin_pct_equity": estimated_margin / eq if eq else None,
        "same_side_pct": same_side_pct,
        "largest_position_pct_gross": largest_pct_gross,
        "margin_coverage": margin_known / len(positions) if positions else 1.0,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 主闸
# ---------------------------------------------------------------------------
def validate(
    symbol: str,
    side: str,
    intended_sz: float,
    lev: float,
    mark_px: Optional[float],
    ct_val: Optional[float],
    lot_sz: Optional[float],
    equity: Optional[float],
    open_positions: Optional[list[dict[str, Any]]] = None,
    sl_trigger_px: Optional[float] = None,
    profile: str = "live",
    available_margin: Optional[float] = None,
) -> dict[str, Any]:
    """确定性风控闸。

    返回 dict:
      {
        approved: bool,            # 是否放行
        approved_sz: float|None,   # 放行张数（已 lot_sz 取整）；reject 时 None
        clamped: bool,             # 是否被 clamp（与 intended 不同）
        adjustments: [str],        # clamp/调整人读说明（留痕 / push 展示）
        reject_reason: str|None,   # 拒因（machine-readable code）
        reject_detail: str|None,   # 拒因人读说明
        math: {...},               # 全部中间量（透明留痕）
      }
    """
    open_positions = open_positions or []
    side = str(side or "").lower()
    adjustments: list[str] = []

    def _reject(code: str, detail: str) -> dict[str, Any]:
        return {
            "approved": False, "approved_sz": None, "clamped": False,
            "adjustments": adjustments, "reject_reason": code,
            "reject_detail": detail, "math": math_box,
        }

    def _approve(sz: float, clamped: bool) -> dict[str, Any]:
        return {
            "approved": True, "approved_sz": sz, "clamped": clamped,
            "adjustments": adjustments, "reject_reason": None,
            "reject_detail": None, "math": math_box,
        }

    math_box: dict[str, Any] = {
        "symbol": symbol, "side": side, "profile": profile,
        "intended_sz": intended_sz, "lev": lev, "mark_px": mark_px,
        "ct_val": ct_val, "lot_sz": lot_sz, "equity": equity,
        "available_margin": available_margin,
        "portfolio_observation": portfolio_observation(open_positions, equity),
    }

    # ── 数据完备性（instrument 未知/缺 = 下架或不存在；demo 也拒，因物理无法定仓）──
    if mark_px is None or mark_px <= 0:
        return _reject("bad_mark_px", f"mark_px 非法: {mark_px}")
    if equity is None or equity <= 0:
        return _reject("bad_equity", f"equity 非法: {equity}")
    if ct_val is None or ct_val <= 0 or lot_sz is None or lot_sz <= 0:
        return _reject("instrument_unknown",
                       f"{symbol} ctVal/lotSz 缺失（下架/不存在/未缓存）: "
                       f"ctVal={ct_val} lotSz={lot_sz}")
    if side not in ("long", "short"):
        return _reject("bad_side", f"side 非法: {side!r}")
    if intended_sz is None or intended_sz <= 0:
        return _reject("bad_sz", f"intended_sz 非法: {intended_sz}")
    try:
        lev = float(lev)
    except (TypeError, ValueError):
        return _reject("bad_lev", f"lev 非法: {lev}")
    if not math.isfinite(lev) or lev <= 0:
        return _reject("bad_lev", f"lev 非有限正数: {lev}")
    math_box["lev"] = lev
    # 执行闸 fail-safe：结算币可用保证金未知时不得仅按 totalEq 放行。参数保留默认值是
    # 为旧调用方提供受控 REJECT（而非 TypeError），不是允许回退旧口径。
    if available_margin is None:
        return _reject(
            "available_margin_missing",
            "当前可用结算币保证金未知，拒开（禁仅按 totalEq 估算）",
        )
    try:
        available_margin = float(available_margin)
    except (TypeError, ValueError):
        return _reject("bad_available_margin",
                       f"available_margin 非法: {available_margin}")
    if not math.isfinite(available_margin) or available_margin < 0:
        return _reject("bad_available_margin",
                       f"available_margin 非有限非负数: {available_margin}")

    # 加仓不会调用 set_leverage，保证金必须按 OKX 现仓实际杠杆估算，不能按 caller 请求值。
    # 否则现仓5x、请求10x会把增量保证金低估一半，越过20%/可用资金预检。
    existing_position = next(
        (
            p for p in open_positions
            if (p.get("symbol") or p.get("instId")) == symbol
            and str(p.get("side", "")).lower() == side
        ),
        None,
    )
    effective_lev = lev
    if existing_position is not None:
        existing_lev_raw = existing_position.get("lev")
        if existing_lev_raw in (None, ""):
            existing_lev_raw = existing_position.get("lever")
        try:
            existing_lev = float(existing_lev_raw)
        except (TypeError, ValueError):
            existing_lev = 0.0
        if not math.isfinite(existing_lev) or existing_lev <= 0:
            return _reject(
                "existing_leverage_unknown",
                f"{symbol} {side} 现仓杠杆缺失/非法，拒绝加仓: {existing_lev_raw}",
            )
        effective_lev = existing_lev
        if abs(effective_lev - lev) > _EPS:
            adjustments.append(
                f"加仓沿用现仓杠杆 {effective_lev:g}x（请求 {lev:g}x 不生效）"
            )
    math_box["requested_lev"] = lev
    math_box["effective_lev"] = effective_lev

    # ── 核心计算（红线 #8：先算每张保证金，勿用 ctVal 直接比上限）──
    per_contract_notional = mark_px * ct_val            # 每张名义（USDT）
    per_contract_margin = per_contract_notional / effective_lev  # 每张保证金（USDT）
    equity_margin_budget = MAX_MARGIN_PCT * equity      # 每笔硬上限：20% 净值
    available_margin_budget = None
    if available_margin is not None:
        available_margin_budget = available_margin * AVAILABLE_MARGIN_USE_PCT
    # 两条独立上限取小：①单笔≤20%净值；②不得超过当前可用结算币保证金（留2%余量）。
    margin_budget = min(
        equity_margin_budget,
        available_margin_budget if available_margin_budget is not None
        else equity_margin_budget,
    )
    notional_floor = MIN_NOTIONAL_PCT * equity          # 1% 净值

    # 最大张数（保证金上限，向下取整到 lot_sz）
    max_sz_margin = _round_down_to_step(margin_budget / per_contract_margin, lot_sz)
    # 名义下限张数（向上取整到 lot_sz）
    min_notional_sz = _round_up_to_step(notional_floor / per_contract_notional, lot_sz)
    # 最小可交易单位 = 一个 lot
    min_unit_margin = lot_sz * per_contract_margin

    math_box.update({
        "per_contract_notional": per_contract_notional,
        "per_contract_margin": per_contract_margin,
        "equity_margin_budget": equity_margin_budget,
        "available_margin_raw": available_margin,
        "available_margin_use_pct": AVAILABLE_MARGIN_USE_PCT,
        "available_margin_budget": available_margin_budget,
        "margin_budget": margin_budget,
        "margin_budget_binding": (
            "available_margin"
            if available_margin_budget is not None
            and available_margin_budget < equity_margin_budget - _EPS
            else "single_trade_20pct_equity"
        ),
        "notional_floor": notional_floor,
        "max_sz_margin": max_sz_margin,
        "min_notional_sz": min_notional_sz,
        "min_unit_margin": min_unit_margin,
    })

    new_open = existing_position is None
    same_side_existing = _same_side_notional(open_positions, side)
    math_box["new_open"] = new_open
    math_box["same_side_existing_notional"] = same_side_existing
    math_box["open_position_count"] = len(open_positions)

    # ── 止损距校验（与硬上限独立，填错保护；传了才校）──
    sl_dev = None
    if sl_trigger_px is not None:
        try:
            sl_dev = abs(float(sl_trigger_px) - mark_px) / mark_px
            math_box["sl_deviation"] = sl_dev
        except (TypeError, ValueError):
            sl_dev = None

    # ===================================================================
    # live/demo：同一套硬上限闸（判定顺序短路，方案 §7）
    # ===================================================================
    # 1. 杠杆 > 10x → reject
    if lev > MAX_LEVERAGE + _EPS:
        return _reject("leverage_exceeds",
                       f"杠杆 {lev}x > 硬上限 {MAX_LEVERAGE:.0f}x")
    if effective_lev > MAX_LEVERAGE + _EPS:
        return _reject(
            "existing_leverage_exceeds",
            f"现仓杠杆 {effective_lev}x > 硬上限 {MAX_LEVERAGE:.0f}x，禁加仓",
        )
    # 2. instrument 未知已在上面拒（数据完备性段）
    # 3. 最小单位（1 lot）保证金 > 20%×equity → reject（连 1 张都超）
    if min_unit_margin > equity_margin_budget + _EPS:
        return _reject("min_unit_over_budget",
                       f"最小 {lot_sz} 张保证金 {min_unit_margin:.2f} > 预算 "
                       f"{equity_margin_budget:.2f}（20%×净值），禁开仓")
    # 3b. 可用结算币连最小单位都覆盖不了 → 本地拒绝，不把 51008 留给交易所。
    if (available_margin_budget is not None
            and min_unit_margin > available_margin_budget + _EPS):
        return _reject(
            "insufficient_available_margin",
            f"最小 {lot_sz} 张保证金 {min_unit_margin:.2f} > 当前可用保证金预算 "
            f"{available_margin_budget:.2f}（可用 {available_margin:.2f}×"
            f"{AVAILABLE_MARGIN_USE_PCT:.0%}），禁开仓",
        )
    # 4. 持仓数量仅记入 math_box/portfolio_observation，Agent 自主评估组合影响，
    #    不再形成并发仓位数量硬闸。
    # 5.（已取消 2026-07-15 主人拍板）同侧名义 ≤60% 硬闸移除——除硬上限+强制 SL 外不加
    #    条件，同侧集中度交 LLM 自主判断、失误走经验学习闭环（trade_experiences/missed_opps）。
    #    同侧暴露仍记 math_box["same_side_existing_notional"]（上方 #闸前装配）供回执观察。

    # 6. 互斥可行性：名义下限张数 > 最大保证金张数 → infeasible reject
    if min_notional_sz > max_sz_margin + _EPS:
        if (available_margin_budget is not None
                and available_margin_budget < equity_margin_budget - _EPS):
            return _reject(
                "available_margin_infeasible",
                f"当前可用保证金预算 {available_margin_budget:.2f} 最多开 "
                f"{max_sz_margin} 张，达不到名义下限 {min_notional_sz} 张",
            )
        return _reject("infeasible",
                       f"名义下限 {min_notional_sz} 张的保证金已超 20% 上限"
                       f"（max {max_sz_margin} 张），下限与上限打架")

    # 7. clamp：approved_sz = clamp(intended, 名义下限, 最大保证金张数)
    approved_sz = _round_down_to_step(intended_sz, lot_sz)
    clamped = False
    if approved_sz > max_sz_margin:
        approved_sz = max_sz_margin
        clamped = True
        binding = math_box["margin_budget_binding"]
        reason = ("当前可用保证金上限" if binding == "available_margin"
                  else "单笔保证金 20% 上限")
        adjustments.append(f"intended {intended_sz} 张 → approved {approved_sz} 张"
                           f"（{reason}）")
    if approved_sz < min_notional_sz:
        approved_sz = min_notional_sz
        clamped = True
        adjustments.append(f"intended {intended_sz} 张 → approved {approved_sz} 张"
                           f"（名义 1% 下限）")
    if abs(approved_sz - intended_sz) > _EPS and not clamped:
        clamped = True
        adjustments.append(f"lot_sz 取整: {intended_sz} → {approved_sz}")

    if approved_sz <= 0:
        return _reject("zero_after_clamp", "clamp 后张数 ≤ 0")

    # 8. 止损距校验（最后；前面都过才校，避免无谓计算）
    if sl_dev is not None and sl_dev > MAX_SL_DEVIATION:
        return _reject("sl_deviation_exceeds",
                       f"止损价 {sl_trigger_px} 偏离 mark {mark_px} 达 {sl_dev:.1%} "
                       f"> {MAX_SL_DEVIATION:.0%}（疑填错标的）")

    math_box["approved_notional"] = approved_sz * per_contract_notional
    math_box["approved_margin"] = approved_sz * per_contract_margin
    return _approve(approved_sz, clamped)


# ---------------------------------------------------------------------------
# 预算辅助（供 decision_briefing 调，让 LLM 提 sz 前就知边界）
# ---------------------------------------------------------------------------
def position_budget(mark_px: float, ct_val: float, lot_sz: float,
                    equity: float, lev: float,
                    available_margin: Optional[float] = None) -> dict[str, Any]:
    """给定现场算「此 equity/lev 下本币最大可开张数 + 名义下限张数」。

    decision_briefing 用它给 LLM 标各币可开区间，减少越界重提。
    """
    if not all([mark_px, ct_val, lot_sz, equity, lev]) or min(
            mark_px, ct_val, lot_sz, equity, lev) <= 0:
        return {"ok": False}
    per_contract_notional = mark_px * ct_val
    per_contract_margin = per_contract_notional / lev
    equity_margin_budget = MAX_MARGIN_PCT * equity
    available_margin_budget = None
    if available_margin is not None:
        try:
            available_margin = float(available_margin)
        except (TypeError, ValueError):
            return {"ok": False}
        if not math.isfinite(available_margin) or available_margin < 0:
            return {"ok": False}
        available_margin_budget = available_margin * AVAILABLE_MARGIN_USE_PCT
    margin_budget = min(
        equity_margin_budget,
        available_margin_budget if available_margin_budget is not None
        else equity_margin_budget,
    )
    max_sz = _round_down_to_step(margin_budget / per_contract_margin, lot_sz)
    min_sz = _round_up_to_step(MIN_NOTIONAL_PCT * equity / per_contract_notional, lot_sz)
    return {
        "ok": True,
        "max_sz_margin": max_sz,
        "min_notional_sz": min_sz,
        "per_contract_margin": per_contract_margin,
        "per_contract_notional": per_contract_notional,
        "equity_margin_budget": equity_margin_budget,
        "available_margin_raw": available_margin,
        "available_margin_budget": available_margin_budget,
        "margin_budget": margin_budget,
        "margin_budget_binding": (
            "available_margin"
            if available_margin_budget is not None
            and available_margin_budget < equity_margin_budget - _EPS
            else "single_trade_20pct_equity"
        ),
        "feasible": min_sz <= max_sz,
    }
