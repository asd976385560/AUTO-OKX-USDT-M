# -*- coding: utf-8 -*-
"""V2.0 §7 契约 A —— 确定性风控闸（纯函数）。

把 live_trader.md §3 现为「LLM 散文 + 手算硬上限」的风控搬成一段纯函数代码：
输入 symbol/side/intended_sz/lev/mark_px/ct_val/lot_sz/equity/容量/现仓 →
校预计成交后组合 IMR/totalEq≤66.6%、单笔增量保证金≤15%净值、单笔止损风险
（含双边费用与滑点缓冲）≤5%净值、可用保证金×98%、名义≥1%、杠杆≤10x、
交易所 minSz/lotSz 与止损距
→ 返回 approved_sz 或拒因。**LLM 物理越不过它**（live 下单唯一路径 order_executor 内部
强制调本闸）。

设计要点（方案 §7）：
  - **纯函数**：无网络 / 无 DB / 无副作用，全部输入显式传入 → 100% 可单测。
  - **现仓权威**：调用方（order_executor）必从 OKX API 取现仓传入；禁 position_snapshots
    GROUP BY（红线 #6）。
  - **勿用 ctVal 直接比硬上限**（红线 #8 / MEMORY §3 bug）：先算「本单增量 IMR =
    approved_sz × mark_px × ct_val ÷ effective_lev」，再叠加账户实时 IMR。
  - **硬上限 = 模块级常量**（hardcoded）：仅主人改码才动，禁 registry/LLM 改
    （红线「不放宽风控自学」）。

本模块零模型名 / 零 provider 字段（红线 #1 天然合规）。
"""
from __future__ import annotations

import math
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 风控常量（2026-08-06 demo 下线后只剩 live 一套口径）。
# ---------------------------------------------------------------------------
MAX_PORTFOLIO_IMR_RATIO = 0.666  # 预计成交后组合 account.imr / totalEq 硬上限
MAX_LEVERAGE = 10.0          # 杠杆 ≤ 10x（越界 → reject）
# 同侧暴露写入 math_box 供组合观察，由 Agent 自主裁决，不参与 approve/reject。
MIN_NOTIONAL_PCT = 0.01      # 单笔名义价值 ≥ 1% 净值（不足 → clamp 上调 sz）
MAX_SL_DEVIATION = 0.30      # 止损价偏离 mark_px ≤ 30%（超 → reject，防填错标的）
# 市价单按 mark_px 估增量 IMR，另保留可用资金余量，避免本笔耗尽结算币保证金。
AVAILABLE_MARGIN_USE_PCT = 0.98
# 单笔增量保证金硬上限（2026-08-08 主人拍板）：每次 OPEN/ADD 的增量 IMR ≤ 15% 净值。
# 直接动因：08-08 06:30 SNDK 单笔 606 USDT ≈ 60% 净值——组合闸 63.8%<66.6% 如设计放行，
# 但决策卡把 10 张保证金自估成 "$50-80"（实际 $1212，错 10-20 倍）；历史 137 笔 OPEN/ADD
# 仅 5 笔 >15%，均值 5.1%，上限只削极端单。定仓预算另留市价滑点余量：
# budget = 0.15×0.98 = 14.7% 净值；对外硬边界 15% 供成交后审计
# （order_executor 回执 single_order_cap_breached：滑点突破只告警不阻断、不追溯现仓）。
MAX_SINGLE_ORDER_IMR_RATIO = 0.15
SINGLE_ORDER_SIZING_HEADROOM_PCT = 0.98
# 单笔止损风险预算（2026-08-10 Wave1 主人拍板）：风险 = 名义×(止损距离+费用/滑点
# 缓冲) ≤ 5% 净值。5% 为主人确认的当前硬预算；它与 15% 单笔增量保证金闸
# 正交，专门约束“止损被打时最多损失多少净值”。
# 与保证金/可用资金闸取最小、按 lot 向下缩量；lot 粒度容不下时整笔拒绝。
# live OPEN/ADD 恒带 SL（executor 入口 no_sl 硬拒），本闸在生产恒有输入；
# sl_trigger_px=None 只发生在非下单路径，此时跳过并在 math 留痕。
MAX_SINGLE_ORDER_RISK_PCT_EQUITY = 0.05
RISK_FEE_BUFFER_PCT = 0.0010       # taker 0.05% × 双边，计入风险距离
RISK_SLIPPAGE_BUFFER_PCT = 0.0010  # 入场 + SL 触发市价滑点合并预算

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


def size_for_target_stop_risk(
    *,
    mark_px: float,
    ct_val: float,
    lot_sz: float,
    equity: float,
    sl_trigger_px: float,
    target_risk_pct_equity: float,
    min_order_size: Optional[float] = None,
) -> dict[str, Any]:
    """Convert one explicit stop-risk target into deterministic contracts.

    This helper is sizing only; it never approves an order.  The caller must
    still pass the resulting ``intended_sz`` through :func:`validate` using
    authoritative execution-time account and market inputs.  Keeping the
    conversion here prevents Agents/runners from reimplementing the fee,
    slippage, minimum-notional, and lot-rounding rules.
    """
    values = {
        "mark_px": mark_px,
        "ct_val": ct_val,
        "lot_sz": lot_sz,
        "equity": equity,
        "sl_trigger_px": sl_trigger_px,
        "target_risk_pct_equity": target_risk_pct_equity,
    }
    normalized: dict[str, float] = {}
    for name, raw in values.items():
        if isinstance(raw, bool):
            return {"ok": False, "error": f"{name}_invalid"}
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": f"{name}_invalid"}
        if not math.isfinite(number) or number <= 0:
            return {"ok": False, "error": f"{name}_invalid"}
        normalized[name] = number

    target_pct = normalized["target_risk_pct_equity"]
    if target_pct > MAX_SINGLE_ORDER_RISK_PCT_EQUITY + _EPS:
        return {
            "ok": False,
            "error": "target_risk_pct_exceeds_hard_cap",
            "target_risk_pct_equity": target_pct,
            "max_single_order_risk_pct_equity": (
                MAX_SINGLE_ORDER_RISK_PCT_EQUITY
            ),
        }

    mark = normalized["mark_px"]
    stop = normalized["sl_trigger_px"]
    contract_value = normalized["ct_val"]
    lot = normalized["lot_sz"]
    total_equity = normalized["equity"]
    stop_distance_pct = abs(stop - mark) / mark
    if stop_distance_pct > MAX_SL_DEVIATION + _EPS:
        return {
            "ok": False,
            "error": "sl_deviation_exceeds",
            "stop_distance_pct": stop_distance_pct,
            "max_sl_deviation": MAX_SL_DEVIATION,
        }

    per_contract_notional = mark * contract_value
    risk_distance_pct = (
        stop_distance_pct + RISK_FEE_BUFFER_PCT + RISK_SLIPPAGE_BUFFER_PCT
    )
    per_contract_risk = per_contract_notional * risk_distance_pct
    target_risk_usdt = total_equity * target_pct
    intended_sz = _round_down_to_step(target_risk_usdt / per_contract_risk, lot)

    minimum_sz = lot
    if min_order_size is not None:
        if isinstance(min_order_size, bool):
            return {"ok": False, "error": "min_order_size_invalid"}
        try:
            normalized_min = float(min_order_size)
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "min_order_size_invalid"}
        if not math.isfinite(normalized_min) or normalized_min <= 0:
            return {"ok": False, "error": "min_order_size_invalid"}
        minimum_sz = max(minimum_sz, _round_up_to_step(normalized_min, lot))
    min_notional_sz = _round_up_to_step(
        MIN_NOTIONAL_PCT * total_equity / per_contract_notional,
        lot,
    )
    minimum_sz = max(minimum_sz, min_notional_sz)
    minimum_risk_usdt = minimum_sz * per_contract_risk
    if intended_sz + _EPS < minimum_sz:
        return {
            "ok": False,
            "error": "target_risk_below_minimum_order",
            "target_risk_usdt": target_risk_usdt,
            "minimum_risk_usdt": minimum_risk_usdt,
            "minimum_sz": minimum_sz,
            "min_notional_sz": min_notional_sz,
        }

    intended_risk_usdt = intended_sz * per_contract_risk
    return {
        "ok": True,
        "intended_sz": intended_sz,
        "target_risk_pct_equity": target_pct,
        "target_risk_usdt": target_risk_usdt,
        "intended_risk_usdt": intended_risk_usdt,
        "intended_risk_pct_equity": intended_risk_usdt / total_equity,
        "stop_distance_pct": stop_distance_pct,
        "risk_distance_pct": risk_distance_pct,
        "risk_fee_buffer_pct": RISK_FEE_BUFFER_PCT,
        "risk_slippage_buffer_pct": RISK_SLIPPAGE_BUFFER_PCT,
        "per_contract_notional": per_contract_notional,
        "per_contract_risk": per_contract_risk,
        "lot_sz": lot,
        "minimum_sz": minimum_sz,
        "min_notional_sz": min_notional_sz,
        "max_single_order_risk_pct_equity": MAX_SINGLE_ORDER_RISK_PCT_EQUITY,
    }


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
    account_imr: Optional[float] = None,
    exchange_max_size: Optional[float] = None,
    min_order_size: Optional[float] = None,
    preflight_only: bool = False,
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
    # 2026-08-06 demo 全量下线：本模块只服务 live；order_executor 已在入口
    # _require_live_profile() 硬拒非 live profile。
    profile_label = "live"
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
        "symbol": symbol, "side": side, "profile": profile_label,
        "intended_sz": intended_sz, "lev": lev, "mark_px": mark_px,
        "ct_val": ct_val, "lot_sz": lot_sz, "equity": equity,
        "available_margin": available_margin,
        "account_imr": account_imr,
        "exchange_max_size": exchange_max_size,
        "min_order_size": min_order_size,
        "preflight_only": bool(preflight_only),
        "portfolio_observation": portfolio_observation(open_positions, equity),
    }

    # ── 数据完备性（instrument 未知/缺 = 下架或不存在 → 物理无法定仓）──
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
    # Live 同时依赖结算币可用保证金与账户实时组合 IMR。
    if True:
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
        if account_imr is None:
            return _reject(
                "account_imr_missing",
                "当前账户组合 IMR 未知，拒开（禁用 totalEq-availBal 猜测）",
            )
        try:
            account_imr = float(account_imr)
        except (TypeError, ValueError):
            return _reject("bad_account_imr", f"account_imr 非法: {account_imr}")
        if not math.isfinite(account_imr) or account_imr < 0:
            return _reject(
                "bad_account_imr",
                f"account_imr 非有限非负数: {account_imr}",
            )
    else:
        math_box["available_margin_observation_only"] = available_margin
        math_box["account_imr_observation_only"] = account_imr

    # 加仓不会调用 set_leverage，保证金必须按 OKX 现仓实际杠杆估算，不能按 caller 请求值。
    # 否则现仓5x、请求10x会把增量 IMR 低估一半，越过组合/可用资金预检。
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

    # ── 公共换算（红线 #8：先算每张保证金，勿用 ctVal 直接比上限）──
    per_contract_notional = mark_px * ct_val            # 每张名义（USDT）
    per_contract_margin = per_contract_notional / effective_lev  # 每张保证金（USDT）
    available_margin_budget = None
    if available_margin is not None:
        available_margin_budget = available_margin * AVAILABLE_MARGIN_USE_PCT
    # Live 张数只按当前可用结算币×98%收敛；组合66.6%是随后执行的整单拒绝闸，
    # 不按剩余组合空间 clamp。
    notional_floor = MIN_NOTIONAL_PCT * equity      # live：1% 净值
    max_sz_available = _round_down_to_step(
        available_margin_budget / per_contract_margin, lot_sz)
    min_notional_sz = _round_up_to_step(
        notional_floor / per_contract_notional, lot_sz)
    # 最小可交易单位 = 一个 lot
    min_unit_margin = lot_sz * per_contract_margin
    # 单笔增量保证金：硬边界 15% 净值；定仓预算再留 2% 滑点余量（=14.7%），
    # 保证市价成交后仍在硬边界内。
    single_order_margin_cap = MAX_SINGLE_ORDER_IMR_RATIO * equity
    single_order_sizing_budget = (
        single_order_margin_cap * SINGLE_ORDER_SIZING_HEADROOM_PCT)
    max_sz_single_order = _round_down_to_step(
        single_order_sizing_budget / per_contract_margin, lot_sz)
    # 单笔止损风险预算（Wave1 序 7）：与保证金维度正交——保证金闸管"占用多少"，
    # 本闸管"止损打掉会亏多少"。SNDK 教训：15% IMR 闸对 24% 净值的止损风险无感知。
    single_order_risk_cap = MAX_SINGLE_ORDER_RISK_PCT_EQUITY * equity

    math_box.update({
        "per_contract_notional": per_contract_notional,
        "per_contract_margin": per_contract_margin,
        "available_margin_raw": available_margin,
        "available_margin_use_pct": AVAILABLE_MARGIN_USE_PCT,
        "available_margin_budget": available_margin_budget,
        "margin_budget_binding": "available_margin",
        "notional_floor": notional_floor,
        "max_sz_available": max_sz_available,
        "min_notional_sz": min_notional_sz,
        "min_unit_margin": min_unit_margin,
        "max_single_order_imr_ratio": MAX_SINGLE_ORDER_IMR_RATIO,
        "single_order_sizing_headroom_pct": SINGLE_ORDER_SIZING_HEADROOM_PCT,
        "single_order_margin_cap": single_order_margin_cap,
        "single_order_sizing_budget": single_order_sizing_budget,
        "max_sz_single_order": max_sz_single_order,
        "max_single_order_risk_pct_equity": MAX_SINGLE_ORDER_RISK_PCT_EQUITY,
        "risk_fee_buffer_pct": RISK_FEE_BUFFER_PCT,
        "risk_slippage_buffer_pct": RISK_SLIPPAGE_BUFFER_PCT,
        "single_order_risk_cap": single_order_risk_cap,
        "account_imr": account_imr,
        "max_portfolio_imr_ratio": MAX_PORTFOLIO_IMR_RATIO,
        "portfolio_imr_ratio_unit": "fraction",
        "portfolio_imr_source": "account.balance.imr",
    })

    new_open = existing_position is None
    same_side_existing = _same_side_notional(open_positions, side)
    math_box["new_open"] = new_open
    math_box["same_side_existing_notional"] = same_side_existing
    math_box["open_position_count"] = len(open_positions)

    # ── 止损校验（与硬上限独立；传了才校）──
    # 止损不仅要“离 mark 不远”，还必须位于能真正止损的一侧。否则 long 的
    # 止损高于 mark / short 的止损低于 mark 会变成方向反了的条件单。
    sl_dev = None
    sl_value = None
    if sl_trigger_px is not None:
        try:
            sl_value = float(sl_trigger_px)
        except (TypeError, ValueError, OverflowError):
            return _reject("bad_sl", f"止损价非法: {sl_trigger_px}")
        if not math.isfinite(sl_value) or sl_value <= 0:
            return _reject("bad_sl", f"止损价非有限正数: {sl_trigger_px}")
        sl_dev = abs(sl_value - mark_px) / mark_px
        math_box["sl_trigger_px"] = sl_value
        math_box["sl_deviation"] = sl_dev
        if side == "long" and sl_value >= mark_px:
            return _reject(
                "sl_direction_invalid",
                f"long 止损价 {sl_value} 必须低于 mark {mark_px}",
            )
        if side == "short" and sl_value <= mark_px:
            return _reject(
                "sl_direction_invalid",
                f"short 止损价 {sl_value} 必须高于 mark {mark_px}",
            )

        if sl_dev > MAX_SL_DEVIATION:
            return _reject(
                "sl_deviation_exceeds",
                f"止损价 {sl_trigger_px} 偏离 mark {mark_px} 达 {sl_dev:.1%} "
                f"> {MAX_SL_DEVIATION:.0%}（疑填错标的）",
            )

    # ── 单笔止损风险预算（Wave1 序 7；有 SL 才可算，live 下单路径恒有）──
    max_sz_risk = None
    per_contract_risk = None
    risk_dist_pct = None
    if sl_dev is not None:
        risk_dist_pct = sl_dev + RISK_FEE_BUFFER_PCT + RISK_SLIPPAGE_BUFFER_PCT
        per_contract_risk = per_contract_notional * risk_dist_pct
        max_sz_risk = _round_down_to_step(
            single_order_risk_cap / per_contract_risk, lot_sz)
        math_box.update({
            "risk_dist_pct": risk_dist_pct,
            "per_contract_risk": per_contract_risk,
            "max_sz_risk": max_sz_risk,
        })
    else:
        math_box["single_order_risk_cap_skipped"] = "no_sl_provided"

    # ── 公共安全闸 ──
    if lev > MAX_LEVERAGE + _EPS:
        return _reject("leverage_exceeds",
                       f"杠杆 {lev}x > 硬上限 {MAX_LEVERAGE:.0f}x")
    if effective_lev > MAX_LEVERAGE + _EPS:
        return _reject(
            "existing_leverage_exceeds",
            f"现仓杠杆 {effective_lev}x > 硬上限 {MAX_LEVERAGE:.0f}x，禁加仓",
        )

    rounded_intended = _round_down_to_step(intended_sz, lot_sz)
    # demo 的两阶段定仓分支（minSz/lotSz 物理下单校验 + preflight_only 预检 +
    # exchange_max_size 收敛）随 2026-08-06 全量下线整块移除。preflight_only 是
    # 那条路径专用的入口，现已无任何合法用法——保留参数并硬拒，避免残留调用方
    # 以为自己拿到了一次「只预检不定仓」的放行。
    if preflight_only:
        return _reject(
            "preflight_profile_invalid",
            "preflight_only 随 demo 两阶段执行路径于 2026-08-06 下线，已无合法用法",
        )

    # ── Live 仓位预算闸（1% / 可用保证金×98% / 组合 IMR 66.6%）──
    # 可用结算币连最小单位都覆盖不了 → 本地拒绝，不把 51008 留给交易所。
    if (available_margin_budget is not None
            and min_unit_margin > available_margin_budget + _EPS):
        return _reject(
            "insufficient_available_margin",
            f"最小 {lot_sz} 张保证金 {min_unit_margin:.2f} > 当前可用保证金预算 "
            f"{available_margin_budget:.2f}（可用 {available_margin:.2f}×"
            f"{AVAILABLE_MARGIN_USE_PCT:.0%}），禁开仓",
        )
    # 持仓数量仅记入 math_box/portfolio_observation，Agent 自主评估组合影响，
    #    不再形成并发仓位数量硬闸。
    # 5.（已取消 2026-07-15 主人拍板）同侧名义 ≤60% 硬闸移除——除硬上限+强制 SL 外不加
    #    条件，同侧集中度交 LLM 自主判断、失误走经验学习闭环（trade_experiences/missed_opps）。
    #    同侧暴露仍记 math_box["same_side_existing_notional"]（上方 #闸前装配）供回执观察。

    # 单笔预算连最小可交易单位都容不下 → 本地拒绝（与可用资金闸同型）。
    if min_unit_margin > single_order_sizing_budget + _EPS:
        return _reject(
            "single_order_cap_infeasible",
            f"最小 {lot_sz} 张保证金 {min_unit_margin:.2f} > 单笔定仓预算 "
            f"{single_order_sizing_budget:.2f}"
            f"（{MAX_SINGLE_ORDER_IMR_RATIO:.0%} 净值×"
            f"{SINGLE_ORDER_SIZING_HEADROOM_PCT:.0%} 滑点余量），禁开仓",
        )

    # 互斥可行性：名义下限张数 > 可用资金最大张数 → reject。
    if min_notional_sz > max_sz_available + _EPS:
        return _reject(
            "available_margin_infeasible",
            f"当前可用保证金预算 {available_margin_budget:.2f} 最多开 "
            f"{max_sz_available} 张，达不到名义下限 {min_notional_sz} 张",
        )
    # 名义 1% 下限与单笔 15% 上限冲突（lot 粒度导致）→ 整笔拒绝，不上调绕闸。
    if min_notional_sz > max_sz_single_order + _EPS:
        return _reject(
            "single_order_cap_infeasible",
            f"名义 1% 下限需 {min_notional_sz} 张，超过单笔保证金上限最多 "
            f"{max_sz_single_order} 张，整笔拒绝",
        )
    # 止损风险预算的可行性（与保证金闸同型；只在有 SL 时生效）。
    if max_sz_risk is not None:
        if lot_sz * per_contract_risk > single_order_risk_cap + _EPS:
            return _reject(
                "single_order_risk_cap_infeasible",
                f"最小 {lot_sz} 张的止损风险 "
                f"{lot_sz * per_contract_risk:.2f} > 单笔风险预算 "
                f"{single_order_risk_cap:.2f}"
                f"（{MAX_SINGLE_ORDER_RISK_PCT_EQUITY:.0%} 净值；"
                f"止损距 {sl_dev:.2%}+缓冲），禁开仓",
            )
        if min_notional_sz > max_sz_risk + _EPS:
            return _reject(
                "single_order_risk_cap_infeasible",
                f"名义 1% 下限需 {min_notional_sz} 张，超过单笔止损风险预算最多 "
                f"{max_sz_risk} 张（止损距 {sl_dev:.2%} 过宽），整笔拒绝",
            )

    # 仅按可用资金/名义下限 clamp；组合66.6%越界时整笔 reject，不缩量。
    approved_sz = _round_down_to_step(intended_sz, lot_sz)
    clamped = False
    if approved_sz > max_sz_available:
        approved_sz = max_sz_available
        clamped = True
        adjustments.append(f"intended {intended_sz} 张 → approved {approved_sz} 张"
                           "（当前可用保证金上限）")
    if approved_sz > max_sz_single_order:
        approved_sz = max_sz_single_order
        clamped = True
        adjustments.append(
            f"intended {intended_sz} 张 → approved {approved_sz} 张"
            f"（单笔保证金上限 {MAX_SINGLE_ORDER_IMR_RATIO:.0%} 净值，"
            f"定仓预算 {single_order_sizing_budget:.2f} USDT）")
    if max_sz_risk is not None and approved_sz > max_sz_risk:
        approved_sz = max_sz_risk
        clamped = True
        adjustments.append(
            f"intended {intended_sz} 张 → approved {approved_sz} 张"
            f"（单笔止损风险预算 {MAX_SINGLE_ORDER_RISK_PCT_EQUITY:.0%} 净值 "
            f"= {single_order_risk_cap:.2f} USDT，止损距 {sl_dev:.2%}+缓冲 "
            f"{RISK_FEE_BUFFER_PCT + RISK_SLIPPAGE_BUFFER_PCT:.2%}）")
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

    approved_notional = approved_sz * per_contract_notional
    incremental_order_imr = approved_sz * per_contract_margin
    projected_account_imr = account_imr + incremental_order_imr
    current_portfolio_imr_ratio = account_imr / equity
    projected_portfolio_imr_ratio = projected_account_imr / equity
    math_box.update({
        "approved_notional": approved_notional,
        "approved_margin": incremental_order_imr,
        "incremental_order_imr": incremental_order_imr,
        "single_order_imr_ratio": incremental_order_imr / equity,
        "projected_account_imr": projected_account_imr,
        "current_portfolio_imr_ratio": current_portfolio_imr_ratio,
        "projected_portfolio_imr_ratio": projected_portfolio_imr_ratio,
    })
    if per_contract_risk is not None:
        approved_risk_usdt = approved_sz * per_contract_risk
        math_box.update({
            "approved_risk_usdt": approved_risk_usdt,
            "single_order_risk_pct_equity": approved_risk_usdt / equity,
            "approved_stop_loss_usdt": (
                approved_sz * per_contract_notional * sl_dev),
        })
    if projected_portfolio_imr_ratio > MAX_PORTFOLIO_IMR_RATIO + _EPS:
        return _reject(
            "portfolio_margin_cap_exceeded",
            "预计成交后组合初始保证金率 "
            f"{projected_portfolio_imr_ratio:.2%} > "
            f"{MAX_PORTFOLIO_IMR_RATIO:.1%}，整笔拒绝 OPEN/ADD（不缩量）",
        )
    return _approve(approved_sz, clamped)


# ---------------------------------------------------------------------------
# 预算辅助（2026-08-08 核实：当前无生产调用方——briefing 未接线；字段与 validate
# 同口径维护，供测试与后续接线。Agent 侧的确定性预算入口是 live_decision_facts
# 的 balance.single_order_margin_budget_usdt）
# ---------------------------------------------------------------------------
def position_budget(mark_px: float, ct_val: float, lot_sz: float,
                    equity: Optional[float], lev: float,
                    available_margin: Optional[float] = None,
                    account_imr: Optional[float] = None,
                    profile: str = "live",
                    exchange_max_size: Optional[float] = None,
                    min_order_size: Optional[float] = None) -> dict[str, Any]:
    """给出 Live 开仓容量提示（不预批订单）。

    返回可用资金张数上限、组合 IMR 剩余空间与单笔 15% 预算提示
    （``single_order_sizing_budget``/``max_sz_single_order`` 等，2026-08-08 与
    validate 同口径）；执行时超 66.6% 仍整单拒绝，不由本 helper 自动改小订单。
    最终以 :func:`validate` 为准。（Demo 的 exchange_max_size 归一分支已随
    2026-08-06 demo 全量下线移除；``exchange_max_size``/``min_order_size`` 参数
    仅为签名兼容保留。）
    """
    # demo 的 exchange_max_size 归一分支随 2026-08-06 全量下线移除。
    common = [mark_px, ct_val, lot_sz, lev]
    if not all(common) or min(common) <= 0:
        return {"ok": False}
    per_contract_notional = mark_px * ct_val
    per_contract_margin = per_contract_notional / lev
    if equity is None or equity <= 0:
        return {"ok": False}
    try:
        available_margin = float(available_margin)
        account_imr = float(account_imr)
    except (TypeError, ValueError):
        return {"ok": False}
    if (not math.isfinite(available_margin) or available_margin < 0
            or not math.isfinite(account_imr) or account_imr < 0):
        return {"ok": False}
    available_margin_budget = available_margin * AVAILABLE_MARGIN_USE_PCT
    max_sz_available = _round_down_to_step(
        available_margin_budget / per_contract_margin, lot_sz)
    single_order_sizing_budget = (
        MAX_SINGLE_ORDER_IMR_RATIO * equity * SINGLE_ORDER_SIZING_HEADROOM_PCT)
    max_sz_single_order = _round_down_to_step(
        single_order_sizing_budget / per_contract_margin, lot_sz)
    portfolio_imr_budget = MAX_PORTFOLIO_IMR_RATIO * equity
    remaining_portfolio_imr = max(0.0, portfolio_imr_budget - account_imr)
    max_sz_portfolio = _round_down_to_step(
        remaining_portfolio_imr / per_contract_margin, lot_sz)
    min_sz = _round_up_to_step(MIN_NOTIONAL_PCT * equity / per_contract_notional, lot_sz)
    return {
        "ok": True,
        "max_sz_available": max_sz_available,
        "max_sz_single_order": max_sz_single_order,
        "single_order_sizing_budget": single_order_sizing_budget,
        "max_single_order_imr_ratio": MAX_SINGLE_ORDER_IMR_RATIO,
        "max_sz_portfolio_observation": max_sz_portfolio,
        "min_notional_sz": min_sz,
        "per_contract_margin": per_contract_margin,
        "per_contract_notional": per_contract_notional,
        "account_imr": account_imr,
        "portfolio_imr_budget": portfolio_imr_budget,
        "remaining_portfolio_imr": remaining_portfolio_imr,
        "current_portfolio_imr_ratio": account_imr / equity,
        "max_portfolio_imr_ratio": MAX_PORTFOLIO_IMR_RATIO,
        "portfolio_imr_ratio_unit": "fraction",
        "portfolio_imr_source": "account.balance.imr",
        "available_margin_raw": available_margin,
        "available_margin_budget": available_margin_budget,
        "margin_budget_binding": "available_margin",
        "feasible": (
            min_sz <= max_sz_available
            and min_sz <= max_sz_single_order
            and min_sz <= max_sz_portfolio
        ),
    }
