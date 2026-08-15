# -*- coding: utf-8 -*-
"""V2.0 §7 契约 B —— 确定性下单/止损/平仓/回读（仅 live）。

live 下单**唯一路径**：order_executor.open_position()，其内部**强制调** risk_validator.validate()
（LLM 物理越不过闸）。本模块把 live_trader.md §4/§8 现为「LLM 手拼 okx 命令 + 手挂止损」的
下单层搬成确定性代码，回执仍喂 trades_writer 落库（writer 不变）。

核心不变量（方案 §7）：
  - **OPEN**：装配现场 → 强制 risk_validator → 市价开仓**即附挂 SL**，并可选附挂
    与决策卡 target 一致的 TP（SL 原子保护不变量不因 TP 失败而降级）
    → 附挂失败则独立 algo SL（重试1）→ 仍失败立即市价平掉刚开仓（不留裸实盘仓）
    → 回读 fills 求真实成交（拉不到 → repair_queue + reject + P0）。
  - **CLOSE**（2026-07-03 主路径反转）：OKX API 现仓确认 posSide → reduceOnly 市价单
    （拿 ordId 即时确认；绝不翻反向仓）主路径 → 被拒转 swap close 兜底 → 51087 下架/
    51001 不存在明确拒因 → fills→订单状态双源确认求真 pnl，两端点均无 →
    unconfirmed(pnl=NULL)+repair_queue。
**2026-08-06 demo 全量下线**：本模块此前还有一条独立的 demo 定仓分支（不走 live 的
1%/20%/98% 比例，改按 symbol/side/tdMode 实时读 OKX Demo max-size；合约规格另带
x-simulated-trading 头现拉 Demo 环境 ctVal/lotSz/minSz）。该分支连同 demo 账本与历史
数据已全部移除，`_require_live_profile()` 现对任何非 live profile 硬拒——**不静默当
live 处理**，否则残留调用方会拿真钱执行一笔它以为是模拟的订单。

现仓/equity 权威一律 OKX API（禁 position_snapshots GROUP BY，红线 #6）。
ctVal/lotSz：market.db.instruments_cache → 缺/stale 现拉 → 仍缺 reject（不拿默认 1.0 蒙）。
零模型名（红线 #1）。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_CORE = os.path.dirname(os.path.abspath(__file__))
_CORE_LIB = os.path.join(_CORE, "lib")
_PROJECT_ROOT = Path(
    os.environ.get("OKX_ROOT") or Path(__file__).resolve().parents[1]
).resolve()
_COLLECTORS = os.environ.get(
    "OKX_COLLECTORS_DIR", str(_PROJECT_ROOT / "collectors"))
for _p in (_CORE, _CORE_LIB, _COLLECTORS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import risk_validator as rv          # noqa: E402  core/risk_validator.py
import account_capacity as ac        # noqa: E402  core/account_capacity.py
import _okxorder as ox               # noqa: E402  core/lib/_okxorder.py
import ledger                        # noqa: E402  collectors/ledger.py（connect ro/WAL）
import execution_intent as ei         # noqa: E402  core/execution_intent.py
from core import actor_attestation as actor_att  # noqa: E402
from decision_card import (  # noqa: E402
    EXIT_MODES,
    validate_card,
    validate_multitimeframe_analysis,
)
from experience_contract import validate_contract as validate_experience_contract  # noqa: E402
from multitimeframe_gate import (  # noqa: E402
    check_multitimeframe_readiness,
    resolve_execution_evidence_anchor,
)

DEFAULT_DB_ROOT = Path(
    os.environ.get("OKX_DB_ROOT", str(_PROJECT_ROOT / "db")))
FILLS_RETRY = 3
FILLS_RETRY_WAIT = 1.5
# 2026-07-03：订单状态第二权威源（demo fills 端点延迟 6-52s，订单状态端点即时）。
# 常数勿再上调——order_executor 在 trader 会话 exec 里跑，sleep 也占 gateway 宿主时长。
ORDER_CONFIRM_RETRY = 3
ORDER_CONFIRM_WAIT = 1.0
_EPS = 1e-9
_FILL_TS_SKEW_MS = 60000  # 本地/交易所时钟偏差容差（fills 时间窗；60s 容 RDP/云主机时钟漂移）
_CONFIRMED_OPEN_FILL_SOURCES = frozenset(
    {"fills", "order_status", "orders_history"})
_CST = timezone(timedelta(hours=8))
_LIVE_CYCLE_SIDE_EFFECT_DEADLINE_SECONDS = 13 * 60


def _cycle_side_effect_reject(
    cycle_id: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Reject a new live exchange side effect after the natural-cycle cutoff.

    Killing the local ``openclaw agent`` client does not necessarily cancel the
    already-dispatched gateway turn.  A late turn must therefore be unable to
    start an order or a standalone protection change after ``cycle+13:00``.
    Protective continuation for an order already submitted (SL verification,
    emergency unwind, post-add/post-reduce resize) is deliberately not routed
    through this start gate: once exposure exists, safety completion wins over
    the clock.
    """
    raw_cycle = str(cycle_id or "").strip()
    try:
        cycle_start = datetime.strptime(
            raw_cycle, "%Y-%m-%dT%H:%M").replace(tzinfo=_CST)
    except (TypeError, ValueError):
        return {
            "action_taken": "REJECT",
            "reject_reason": "cycle_id_invalid",
            "reject_detail": (
                "非 dry-run 交易副作用要求 cycle_id 严格为 YYYY-MM-DDTHH:MM；"
                f"当前值={raw_cycle!r}，未触发交易所写入"),
            "side_effect_deadline": {
                "cycle_id": raw_cycle or None,
                "deadline_at": None,
                "checked_at": None,
                "comparison": ">=",
            },
        }

    checked_at = now or datetime.now(_CST)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=_CST)
    else:
        checked_at = checked_at.astimezone(_CST)
    deadline = cycle_start + timedelta(
        seconds=_LIVE_CYCLE_SIDE_EFFECT_DEADLINE_SECONDS)
    if checked_at < deadline:
        return None
    deadline_text = deadline.strftime("%Y-%m-%d %H:%M:%S")
    checked_text = checked_at.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "action_taken": "REJECT",
        "reject_reason": "cycle_side_effect_deadline_exceeded",
        "reject_detail": (
            f"本轮交易副作用硬截止为 {deadline_text} UTC+8；"
            f"检查时刻 {checked_text} 已到或超过截止，未触发新的交易所写入"),
        "side_effect_deadline": {
            "cycle_id": raw_cycle,
            "deadline_at": deadline_text,
            "checked_at": checked_text,
            "comparison": ">=",
        },
    }


class PositionsUnavailable(RuntimeError):
    """OKX 现仓 API 失败 —— 敞口未知，禁按「零仓」放行（S2a fail-safe，2026-07-02）。"""


class TradeLedgerUnavailable(RuntimeError):
    """profile 交易账本缺失、不可读或含无法安全轧差的数据。"""


# ---------------------------------------------------------------------------
# 现场装配（全 OKX API / instruments_cache）
# ---------------------------------------------------------------------------
def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _exchange_fill_time(
    rows: list[dict[str, Any]],
    *,
    fields: tuple[str, ...],
    source_prefix: str,
) -> tuple[Optional[str], Optional[str]]:
    """取交易所回包中最后一个权威成交/完成时间，归一为 CST 秒级字符串。

    fields 按权威优先级排列：每行只使用第一个有效字段，再跨行取最大值。
    cTime 是订单创建时间，不是成交时间，调用方不得把它放进 fields。
    """
    latest_ms: Optional[float] = None
    latest_field: Optional[str] = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_ms = None
        row_field = None
        for field in fields:
            candidate = _to_float(row.get(field))
            if candidate is None or not math.isfinite(candidate) or candidate <= 0:
                continue
            # OKX 当前返回毫秒；兼容明确的秒级 epoch，但拒绝臆测其他格式。
            row_ms = candidate * 1000.0 if candidate < 100_000_000_000 else candidate
            row_field = field
            break
        if row_ms is not None and (latest_ms is None or row_ms > latest_ms):
            latest_ms = row_ms
            latest_field = row_field
    if latest_ms is None or latest_field is None:
        return None, None
    try:
        fill_ts = datetime.fromtimestamp(
            latest_ms / 1000.0, tz=_CST).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None, None
    return fill_ts, f"{source_prefix}.{latest_field}"


def _validate_confirmed_open_fill(
    fill: dict[str, Any],
    fill_source: str,
    *,
    dryrun: bool = False,
    approved_sz: Optional[float] = None,
) -> tuple[bool, Optional[str]]:
    """确认 OPEN 成交只接受交易所成交/订单端点，并要求真实数量与均价。

    仓位增量、历史全量近似只能证明“需要修复”，不能合成 confirmed OPEN。
    dry-run 没有真实成交，显式保留其模拟回执兼容性。
    """
    if fill_source not in _CONFIRMED_OPEN_FILL_SOURCES:
        return False, f"untrusted_fill_source:{fill_source}"
    if not fill.get("ok"):
        return False, "fill_not_confirmed"
    if dryrun and fill.get("dryrun"):
        return True, None
    fill_sz = _to_float(fill.get("fill_sz"))
    fill_px = _to_float(fill.get("fill_px"))
    if fill_sz is None or not math.isfinite(fill_sz) or fill_sz <= 0:
        return False, "invalid_fill_sz"
    if fill_px is None or not math.isfinite(fill_px) or fill_px <= 0:
        return False, "invalid_fill_px"
    approved = _to_float(approved_sz)
    if approved is not None and math.isfinite(approved) and approved > 0:
        size_tol = max(_EPS, approved * 1e-9)
        if fill_sz > approved + size_tol:
            return False, "fill_sz_exceeds_approved"
    return True, None


def _validate_confirmed_close_fill(
    fill: dict[str, Any],
    fill_source: str,
    *,
    dryrun: bool = False,
    requested_sz: Optional[float] = None,
) -> tuple[bool, Optional[str]]:
    """Validate the complete authoritative contract used for a confirmed close.

    A disappearing position proves only that exposure is gone; it does not
    identify this order's fill quantity, price, or fill time.  Confirmed close
    accounting therefore requires one trusted order/fill endpoint plus all
    fields required by the normal writer contract.
    """
    valid, error = _validate_confirmed_open_fill(
        fill,
        fill_source,
        dryrun=dryrun,
        approved_sz=requested_sz,
    )
    if not valid:
        return valid, error
    if dryrun and fill.get("dryrun"):
        return True, None
    fill_ts = str(fill.get("fill_ts") or "").strip()
    if not fill_ts:
        return False, "invalid_fill_ts"
    try:
        datetime.strptime(fill_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False, "invalid_fill_ts"
    if not str(fill.get("ts_source") or "").strip():
        return False, "invalid_ts_source"
    return True, None


def _open_fill_accounting(
    fill: dict[str, Any],
    *,
    approved_sz: float,
    mark_px: float,
    ct_val: float,
    effective_lev: float,
    dryrun: bool = False,
) -> dict[str, Any]:
    """按真实 fill_sz 计算 OPEN 数量、名义和保证金；approved_sz 仅作审计留痕。"""
    actual_sz = _to_float(fill.get("fill_sz"))
    fill_px = _to_float(fill.get("fill_px"))
    if dryrun and fill.get("dryrun"):
        actual_sz = actual_sz or approved_sz
        fill_px = fill_px or mark_px
    if actual_sz is None or not math.isfinite(actual_sz) or actual_sz <= 0:
        raise ValueError("confirmed OPEN missing positive fill_sz")
    if fill_px is None or not math.isfinite(fill_px) or fill_px <= 0:
        raise ValueError("confirmed OPEN missing positive fill_px")
    notional = actual_sz * ct_val * fill_px
    margin = (notional / effective_lev) if effective_lev else None
    return {
        "sz": actual_sz,
        "approved_sz": approved_sz,
        "fill_px": fill_px,
        "notional": notional,
        "margin": margin,
        "partial_fill": actual_sz < approved_sz - _EPS,
        "fill_ratio": actual_sz / approved_sz if approved_sz > 0 else None,
    }


def _single_order_fill_audit(
    fill_margin: Any, equity: Any,
) -> tuple[Optional[float], Optional[bool]]:
    """成交后单笔保证金复审（纯函数，可单测）：返回 (占净值比, 是否破 15% 硬边界)。

    口径 = 真实成交保证金 ÷ 执行时 equity（与 validator 预检同源）。任一输入
    缺失/非法 → (None, None)，不臆算不误报。突破处置 = 告警 + repair_queue
    人工出口，绝不阻断不追溯（主人拍板口径，2026-08-08）。"""
    try:
        margin = float(fill_margin)
        eq = float(equity)
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(margin) and math.isfinite(eq)) or margin < 0 or eq <= 0:
        return None, None
    ratio = margin / eq
    return ratio, ratio > rv.MAX_SINGLE_ORDER_IMR_RATIO + _EPS


def fetch_equity(profile: str) -> Optional[float]:
    r = ox.get_balance(profile)
    for row in r.get("data", []):
        if isinstance(row, dict) and row.get("totalEq"):
            return _to_float(row.get("totalEq"))
    return None


def fetch_open_positions(profile: str) -> list[dict[str, Any]]:
    """OKX API 现仓 → validator 所需 {symbol, side, sz, notional, lev, avgPx}。

    S2a（2026-07-02）：API 失败（ok=False）时**抛 PositionsUnavailable**，禁把失败静默
    当成「零持仓」——否则组合影响判断失真、close 假报 no_open_position。
    （同侧闸 2026-07-15 已取消，本 fail-safe 语义不变。）
    """
    r = ox.get_positions(profile)
    if not r.get("ok"):
        raise PositionsUnavailable(
            str(r.get("sMsg") or r.get("error") or "positions api failed"))
    out: list[dict[str, Any]] = []
    for item in r.get("data", []):
        if not isinstance(item, dict):
            continue
        sz = _to_float(item.get("pos") or item.get("sz")) or 0.0
        if abs(sz) <= 0:
            continue
        pos_side = str(item.get("posSide") or "").lower()
        if pos_side not in ("long", "short"):
            pos_side = "short" if sz < 0 else "long"
        out.append({
            "symbol": item.get("instId"),
            "side": pos_side,
            "sz": abs(sz),
            "posId": str(item.get("posId") or "") or None,
            "cTime": str(item.get("cTime") or "") or None,
            "notional": _to_float(item.get("notionalUsd")),
            "lev": _to_float(item.get("lever")),
            "avgPx": _to_float(item.get("avgPx") or item.get("markPx")),
        })
    return out


# `_DEMO_SPEC_MEMO` / `_fetch_demo_instrument()` 随 2026-08-06 demo 全量下线移除：
# 二者只服务 demo 环境的合约规格现拉（instruments_cache 只存 live 口径，demo 同名
# 合约规格可能不同，故当年必须带 x-simulated-trading 头直连 public 端点）。


def _require_live_profile(profile: Any, where: str) -> None:
    """2026-08-06 demo 全量下线后，本模块只服务 `profile='live'`。

    **必须硬拒而不是静默当 live 处理**：demo 下线前，`profile='demo'` 走的是完全
    不同的容量分支（`okx_demo_max_size_only`）、不同的合约规格来源和不同的账本。
    若有残留调用方（旧脚本、手工临时脚本、未更新的回放工具）仍传 'demo'，静默把
    它当 live 就意味着**拿真钱执行一笔本以为是模拟的订单**。宁可 raise。
    """
    value = str(profile).strip().lower()
    if value != "live":
        raise ValueError(
            f"{where}: 只支持 profile='live'，收到 {profile!r}。"
            "demo 已于 2026-08-06 全量下线；若这是旧脚本请更新调用方，"
            "**不要**改成 live 直接重跑——先确认它本来要不要动真钱。"
        )


def fetch_instrument_specs(symbol: str, profile: str,
                           db_root: Path = DEFAULT_DB_ROOT) -> dict[str, Any]:
    """ctVal/lotSz/minSz：instruments_cache（market.db）优先 → 缺则现拉。"""
    _require_live_profile(profile, "fetch_instrument_specs")
    ct_val = lot_sz = min_sz = None
    src = None
    db_root = Path(db_root)  # 调用方（trader agent）常传 str 路径——强制 Path，避免 str/str TypeError
    market_db = db_root / "market.db"
    if market_db.exists():
        try:
            con = ledger.connect(market_db, readonly=True)
            try:
                row = con.execute(
                    "SELECT ctVal, lotSz FROM instruments_cache WHERE instId=?",
                    (symbol,)).fetchone()
            finally:
                con.close()
            if row and row["ctVal"] is not None and row["lotSz"] is not None:
                ct_val = _to_float(row["ctVal"])
                lot_sz = _to_float(row["lotSz"])
                # 旧缓存没有 minSz；live 风控保持原行为，以 lotSz 作为物理最小单位。
                min_sz = lot_sz
                src = "cache"
        except Exception:
            pass
    if ct_val is None or lot_sz is None or ct_val <= 0 or lot_sz <= 0:
        inst = ox.get_instrument(symbol, profile)
        if inst:
            ct_val = _to_float(inst.get("ctVal"))
            lot_sz = _to_float(inst.get("lotSz"))
            min_sz = _to_float(inst.get("minSz")) or lot_sz
            src = "live_fetch"
    return {
        "ct_val": ct_val, "lot_sz": lot_sz, "min_sz": min_sz,
        "source": src, "spec_source": src,
    }


def _avg_fill(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合 fills → 加权均价/数量/pnl，并取最后一笔权威成交时间。"""
    tot_sz = 0.0
    tot_quote = 0.0
    tot_pnl = 0.0
    for f in fills:
        sz = _to_float(f.get("fillSz")) or 0.0
        px = _to_float(f.get("fillPx")) or 0.0
        pnl = _to_float(f.get("fillPnl")) or 0.0
        tot_sz += sz
        tot_quote += sz * px
        tot_pnl += pnl
    fill_px = (tot_quote / tot_sz) if tot_sz > 0 else None
    fill_ts, ts_source = _exchange_fill_time(
        fills, fields=("fillTime", "ts"), source_prefix="fills")
    return {
        "fill_px": fill_px, "fill_sz": tot_sz, "pnl": tot_pnl,
        "n": len(fills), "fill_ts": fill_ts, "ts_source": ts_source,
    }


def _filter_fills_since(fills: list[dict[str, Any]],
                        since_ms: Optional[float]) -> list[dict[str, Any]]:
    """S2c（2026-07-02）：无 ordId 时按成交时刻过滤，剔除本次操作之前的历史成交。

    close 走 `swap close` 不返回 ordId，若按 symbol 聚合全部 fills 会混入开仓/历史成交
    → pnl/fill_px 错。带 since_ms（本次操作发起时刻）时只保留其后的成交（留 5s 时钟容差）。
    """
    if since_ms is None:
        return fills
    out = []
    for f in fills:
        t = _to_float(f.get("fillTime") or f.get("ts"))
        if t is not None and t >= since_ms - _FILL_TS_SKEW_MS:
            out.append(f)
    return out


def _read_fills(symbol: str, profile: str, ord_id: Optional[str],
                since_ms: Optional[float] = None) -> dict[str, Any]:
    """带小重试（市价单 fills 偶发短延迟）。拉不到 → ok=False。

    ord_id 给定 → 交易所侧精确过滤；否则用 since_ms 做时间窗过滤（S2c，防历史成交混入）。
    """
    if ox.is_dryrun():
        return {"ok": True, "fill_px": None, "fill_sz": None, "pnl": 0.0,
                "n": 0, "fill_ts": None, "ts_source": None, "dryrun": True}

    last_raw: list = []

    def _agg_if_any(raw: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        nonlocal last_raw
        if not raw:
            return None
        if ord_id:
            agg = _avg_fill(raw)
            agg["ok"] = True
            return agg
        fills = _filter_fills_since(raw, since_ms)
        if fills:
            agg = _avg_fill(fills)
            agg["ok"] = True
            return agg
        # 2026-07-03 改：时间窗滤空不再第一拍即 approx——demo fills 端点延迟 6-52s 下
        # 历史成交会污染 fill_px（昨夜实测 approx 64476 vs 真实 61733）。继续重试等新 fill
        # 落端点；最终仍失败时把全量聚合作为 approx_agg 附带返回，是否采用由调用点决策
        # （调用点先试订单状态第二权威源，approx 只做最后兜底且带标）。
        last_raw = raw
        return None

    for attempt in range(FILLS_RETRY):
        got = _agg_if_any(ox.get_fills(symbol, profile, ord_id=ord_id))
        if got:
            return got
        if attempt < FILLS_RETRY - 1:
            time.sleep(FILLS_RETRY_WAIT)
    # 兜底再查 archive
    got = _agg_if_any(ox.get_fills(symbol, profile, ord_id=ord_id, archive=True))
    if got:
        return got
    approx_agg = None
    if last_raw:
        approx_agg = _avg_fill(last_raw)
        approx_agg["approx"] = True
    return {
        "ok": False, "fill_px": None, "fill_sz": None, "pnl": None, "n": 0,
        "fill_ts": None, "ts_source": None, "approx_agg": approx_agg,
    }


def _fill_from_order(o: dict[str, Any]) -> Optional[dict[str, Any]]:
    """订单状态行 → 合成 fill 聚合（CLI 字段全字符串，显式转 float）。

    仅 terminal 状态可确认：filled，或 canceled 且 accFillSz>0 的已撤部分成交。
    live/partially_filled 仍可能继续成交，不能过早把当下数量写成最终 OPEN。"""
    acc = _to_float(o.get("accFillSz")) or 0.0
    if acc <= 0:
        return None
    state = str(o.get("state") or "").lower()
    if state not in ("filled", "canceled"):
        return None
    fill_ts, ts_source = _exchange_fill_time(
        [o], fields=("fillTime", "uTime"), source_prefix="order_status")
    return {"ok": True, "fill_px": _to_float(o.get("avgPx")), "fill_sz": acc,
            "pnl": _to_float(o.get("pnl")) or 0.0, "n": 1,
            "source": "order_status",
            "partial": state == "canceled",
            "fill_ts": fill_ts, "ts_source": ts_source}


def _confirm_order_filled(symbol: str, profile: str,
                          ord_id: Optional[str]) -> Optional[dict[str, Any]]:
    """fills 拉不到时的第二权威源：按 ordId 查订单状态（2026-07-03：demo fills
    端点延迟 6-52s，订单状态端点实测即时 134ms）。

    返回：合成 fill（accFillSz>0，含部分成交）/ {"ok":False,"state":"canceled"}
    （交易所确认 0 成交=干净未成交）/ None（端点拉不到或订单仍 live → 不可判定，
    caller 走原 fail-safe 拒单路径，禁 fail-open）。"""
    if not ord_id:
        return None
    for attempt in range(ORDER_CONFIRM_RETRY):
        o = ox.get_order(symbol, ord_id, profile)
        if o:
            f = _fill_from_order(o)
            if f:
                return f
            if str(o.get("state")) == "canceled":
                return {"ok": False, "state": "canceled"}
        if attempt < ORDER_CONFIRM_RETRY - 1:
            time.sleep(ORDER_CONFIRM_WAIT)
    return None


def _find_orders_since(symbol: str, profile: str, pos_side: str,
                       since_ms: float,
                       reduce_only: bool) -> Optional[dict[str, Any]]:
    """无 ordId 路径（`swap close` 不返 ordId / 开仓超时恢复）的第二权威源：
    orders-history 按 时间窗+posSide+reduceOnly 反查本次操作产生的订单并聚合。

    实测 `swap close` 的平仓单以独立 ordId+reduceOnly=true 出现在 orders-history，
    avgPx/pnl 字段完整（2026-07-03 verified 61733.2/0.16874 vs 被污染 approx 64476）。
    无命中 → None（caller 决定 approx 兜底或拒单）。"""
    for attempt in range(ORDER_CONFIRM_RETRY):
        rows = ox.get_orders_history(symbol, profile)
        hits = []
        for o in rows:
            if str(o.get("posSide", "")).lower() != str(pos_side).lower():
                continue
            ro = str(o.get("reduceOnly", "")).lower() in ("true", "1")
            if ro != reduce_only:
                continue
            ct = _to_float(o.get("cTime"))
            if ct is None or ct < since_ms - _FILL_TS_SKEW_MS:
                continue
            if (_to_float(o.get("accFillSz")) or 0.0) > 0:
                hits.append(o)
        if hits:
            tot_sz = sum(_to_float(o.get("accFillSz")) or 0.0 for o in hits)
            tot_quote = sum((_to_float(o.get("accFillSz")) or 0.0)
                            * (_to_float(o.get("avgPx")) or 0.0) for o in hits)
            tot_pnl = sum(_to_float(o.get("pnl")) or 0.0 for o in hits)
            fill_ts, ts_source = _exchange_fill_time(
                hits, fields=("fillTime", "uTime"),
                source_prefix="orders_history")
            return {"ok": True,
                    "fill_px": (tot_quote / tot_sz) if tot_sz > 0 else None,
                    "fill_sz": tot_sz, "pnl": tot_pnl, "n": len(hits),
                    "source": "orders_history",
                    "fill_ts": fill_ts, "ts_source": ts_source}
        if attempt < ORDER_CONFIRM_RETRY - 1:
            time.sleep(ORDER_CONFIRM_WAIT)
    return None


_LEDGER_POSITION_ACTIONS = {
    "open": 1.0,
    "add": 1.0,
    "close": -1.0,
    "stop_loss": -1.0,
    "reduce": -1.0,
}
_POSITION_SZ_TOL = 1e-8


def _trade_ledger_path(profile: str, db_root: Path) -> Path:
    _require_live_profile(profile, "_trade_ledger_path")
    return Path(db_root) / "live_trades.db"


def _read_trade_ledger_positions(
    profile: str,
    db_root: Path,
) -> dict[tuple[str, str], float]:
    """只读轧差 profile trades；相关成交行损坏时 fail-closed。"""
    path = _trade_ledger_path(profile, db_root)
    if not path.is_file():
        raise TradeLedgerUnavailable(f"{path.name}:missing")
    try:
        con = ledger.connect(path, readonly=True)
        try:
            rows = con.execute(
                "SELECT symbol,action,side,sz FROM trades ORDER BY rowid"
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:
        raise TradeLedgerUnavailable(
            f"{path.name}:query_failed:{type(exc).__name__}") from exc

    positions: dict[tuple[str, str], float] = {}
    for index, row in enumerate(rows, start=1):
        action = str(row["action"] or "").strip().lower()
        sign = _LEDGER_POSITION_ACTIONS.get(action)
        if sign is None:
            continue
        symbol = str(row["symbol"] or "").strip().upper()
        side = str(row["side"] or "").strip().lower()
        size = _to_float(row["sz"])
        if not symbol or side not in ("long", "short"):
            raise TradeLedgerUnavailable(
                f"{path.name}:invalid_key:row={index}")
        if size is None or not math.isfinite(size) or size <= 0:
            raise TradeLedgerUnavailable(
                f"{path.name}:invalid_sz:row={index}")
        key = (symbol, side)
        positions[key] = positions.get(key, 0.0) + sign * size
    return {
        key: size for key, size in positions.items()
        if abs(size) > _POSITION_SZ_TOL
    }


def _api_position_sizes(
    api_positions: list[dict[str, Any]],
) -> dict[tuple[str, str], float]:
    """已由 OKX API 取得的全仓列表归一为 symbol+side 数量全集。"""
    out: dict[tuple[str, str], float] = {}
    for index, item in enumerate(api_positions or [], start=1):
        symbol = str(item.get("symbol") or item.get("instId") or "").strip().upper()
        side = str(item.get("side") or item.get("posSide") or "").strip().lower()
        size = _to_float(item.get("sz") if item.get("sz") is not None
                         else item.get("pos"))
        if not symbol or side not in ("long", "short"):
            raise PositionsUnavailable(f"invalid normalized position key at row {index}")
        if size is None or not math.isfinite(size) or size <= 0:
            raise PositionsUnavailable(f"invalid normalized position size at row {index}")
        key = (symbol, side)
        out[key] = out.get(key, 0.0) + abs(size)
    return out


def _verify_pretrade_ledger_positions(
    profile: str,
    db_root: Path,
    api_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """交易前以全集合比较账本轧差与本次 OKX API 全仓。"""
    ledger_positions = _read_trade_ledger_positions(profile, db_root)
    exchange_positions = _api_position_sizes(api_positions)
    diffs: list[dict[str, Any]] = []
    for symbol, side in sorted(set(ledger_positions) | set(exchange_positions)):
        ledger_sz = ledger_positions.get((symbol, side), 0.0)
        exchange_sz = exchange_positions.get((symbol, side), 0.0)
        delta = ledger_sz - exchange_sz
        if abs(delta) <= _POSITION_SZ_TOL:
            continue
        diffs.append({
            "symbol": symbol,
            "side": side,
            "ledger_sz": round(ledger_sz, 12),
            "exchange_sz": round(exchange_sz, 12),
            "delta": round(delta, 12),
        })
    return {
        "ok": not diffs,
        "profile": "live",
        "ledger_groups": len(ledger_positions),
        "exchange_groups": len(exchange_positions),
        "diffs": diffs,
    }


def _position_size(positions: list[dict[str, Any]], symbol: str, side: str) -> float:
    for p in positions or []:
        if p.get("symbol") == symbol and str(p.get("side", "")).lower() == side:
            return _to_float(p.get("sz")) or 0.0
    return 0.0


def _position_fingerprint_error(
    actual: Optional[dict[str, Any]],
    *,
    expected_exists: Optional[bool],
    expected_sz: Optional[float],
    expected_pos_id: Optional[str],
    expected_c_time: Optional[str],
) -> Optional[str]:
    if expected_exists is None:
        return None
    if not isinstance(expected_exists, bool):
        return "expected_exists_not_bool"
    actual_exists = actual is not None
    if actual_exists != expected_exists:
        return f"exists expected={expected_exists} actual={actual_exists}"
    if not expected_exists:
        return None
    expected_size = _to_float(expected_sz)
    actual_size = _to_float((actual or {}).get("sz"))
    if expected_size is None or expected_size <= 0:
        return "expected_sz_invalid"
    if actual_size is None or actual_size <= 0:
        return "actual_sz_invalid"
    if abs(actual_size - expected_size) > max(_EPS, expected_size * 1e-8):
        return f"sz expected={expected_size} actual={actual_size}"
    if expected_pos_id not in (None, ""):
        actual_pos_id = str((actual or {}).get("posId") or "")
        if actual_pos_id != str(expected_pos_id):
            return f"posId expected={expected_pos_id} actual={actual_pos_id or None}"
    if expected_c_time not in (None, ""):
        actual_c_time = str((actual or {}).get("cTime") or "")
        if actual_c_time != str(expected_c_time):
            return f"cTime expected={expected_c_time} actual={actual_c_time or None}"
    return None


def _verify_open_settled(symbol: str, side: str, profile: str, pre_sz: float,
                         wait: float = 2.0, tries: int = 3) -> Optional[bool]:
    """S2b（2026-07-02）：下单写超时后判定是否真成交——重拉现仓比对 symbol+side 张数。

    返回 True=已成交（张数增长）/ False=未成交（连续拉到且未增长）/ None=现仓拉不到不可判定。
    """
    for i in range(tries):
        time.sleep(wait)
        try:
            post = fetch_open_positions(profile)
        except PositionsUnavailable:
            post = None
        if post is not None:
            if _position_size(post, symbol, side) > pre_sz + _EPS:
                return True
            if i >= 1:  # 连续两次拉到且未增长 → 判未成交
                return False
    return None


_SCRIPTS_DIR = os.environ.get(
    "OKX_SCRIPTS_DIR", str(_PROJECT_ROOT / "scripts"))
_AUTOHEAL_TIMEOUT_SEC = 180
_AUTOHEAL_CONTRACT_VERSION = 1


def _autoheal_client_result(profile: str, db_root, cycle_id: Optional[str],
                            request_id: str, *, status: str, rc: int,
                            reason: str,
                            finding_kind: str | None =
                            "AUTOHEAL-CONTRACT-INVALID") -> dict[str, Any]:
    finding = ({"kind": finding_kind, "sev": "P1", "reason": reason}
               if finding_kind else None)
    return {
        "contract_version": _AUTOHEAL_CONTRACT_VERSION,
        "request_id": request_id,
        "profile": str(profile),
        "cycle": str(cycle_id) if cycle_id is not None else None,
        "db_root": str(Path(db_root).resolve()),
        "status": status,
        "applied": False,
        "p0": False,
        "blocking": rc != 0,
        "reason": reason,
        "findings": [finding] if finding else [],
        "healed": [],
        "needs_human": [finding] if finding else [],
        "rc": rc,
    }


def _read_autoheal_contract(path: Path, *, request_id: str, profile: str,
                            cycle_id: Optional[str], db_root: Path,
                            returncode: int) -> dict[str, Any]:
    """Read json-out only and bind it to this exact pretrade request."""
    expected_cycle = str(cycle_id) if cycle_id is not None else None
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > 2_000_000:
            raise ValueError("json-out exceeds 2 MB")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("json-out root is not an object")
        checks = {
            "contract_version": data.get("contract_version") == _AUTOHEAL_CONTRACT_VERSION,
            "request_id": data.get("request_id") == request_id,
            "profile": data.get("profile") == str(profile),
            "cycle": data.get("cycle") == expected_cycle,
            "db_root": Path(str(data.get("db_root") or "")).resolve()
                       == db_root.resolve(),
            "status": data.get("status") in {
                "ok", "applied", "needs_human", "error", "skipped",
                "p0_blocked",
            },
            "applied": type(data.get("applied")) is bool,
            "p0": type(data.get("p0")) is bool,
            "blocking": type(data.get("blocking")) is bool,
            "findings": isinstance(data.get("findings"), list),
            "healed": isinstance(data.get("healed"), list),
            "needs_human": isinstance(data.get("needs_human"), list),
            "rc": type(data.get("rc")) is int
                  and data.get("rc") in (0, 1, 2, 3, 4),
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise ValueError("contract fields invalid: " + ",".join(failed))
        if data["blocking"] != (data["rc"] != 0):
            raise ValueError("blocking/rc mismatch")
        if data["p0"] != (data["rc"] == 4):
            raise ValueError("p0/rc mismatch")
        status_by_rc = {
            0: {"ok", "applied"}, 1: {"needs_human"}, 2: {"error"},
            3: {"skipped"}, 4: {"p0_blocked"},
        }
        if data["status"] not in status_by_rc[data["rc"]]:
            raise ValueError("status/rc mismatch")
        healed_applied = any(
            isinstance(item, dict) and item.get("applied") is True
            for item in data["healed"])
        if data["applied"] != healed_applied:
            raise ValueError("applied/healed mismatch")
        findings_p0 = any(
            isinstance(item, dict)
            and str(item.get("sev") or "").upper() == "P0"
            for item in data["findings"])
        if data["p0"] != findings_p0:
            raise ValueError("p0/findings mismatch")
        if int(returncode) != data["rc"]:
            raise ValueError(
                f"process rc={returncode} differs from contract rc={data['rc']}")
        return data
    except Exception as exc:  # noqa: BLE001
        return _autoheal_client_result(
            profile, db_root, cycle_id, request_id,
            status="contract_invalid", rc=2,
            reason=f"{type(exc).__name__}: {exc}",
        )


def _try_autoheal_ledger(profile: str, db_root,
                         cycle_id: Optional[str]) -> dict[str, Any]:
    """插入点 B：pretrade 账仓不一致时，拒单前给一次确定性自愈机会（2026-08-04）。

    公开版只运行诊断，不传任何写入开关。本层不复制分级规则。

    只读取原子 `--json-out`，严格核对 request/profile/cycle/db_root/rc。
    缺失、损坏、过期或任何非零契约都返回 blocking 结果。只有安全
    `applied=true, blocking=false` 时调用方才可重跑账仓校验。
    `--self-cycle` 让本轮自己的 running runner 不被误判为互斥冲突。
    环境变量 `OKX_DISABLE_LEDGER_AUTOHEAL=1` 可一键关掉本层（回退纯人工口径）。
    """
    request_id = uuid.uuid4().hex
    resolved_db_root = Path(db_root).resolve()
    if os.environ.get("OKX_DISABLE_LEDGER_AUTOHEAL") == "1":
        return _autoheal_client_result(
            profile, resolved_db_root, cycle_id, request_id,
            status="disabled", rc=0,
            reason="OKX_DISABLE_LEDGER_AUTOHEAL=1",
            finding_kind=None,
        )
    out_json = Path(tempfile.gettempdir()) / (
        f"okx-ledger-autoheal-{os.getpid()}-{request_id}.json")
    try:
        import subprocess  # 局部 import：仅此罕见分支需要，不拖累安全层常态路径

        heal_py = os.path.join(_SCRIPTS_DIR, "ledger_autoheal.py")
        if not os.path.exists(heal_py):
            return _autoheal_client_result(
                profile, resolved_db_root, cycle_id, request_id,
                status="client_error", rc=2,
                reason=f"ledger_autoheal.py missing: {heal_py}",
            )
        out_json.unlink(missing_ok=True)
        cmd = [sys.executable, heal_py, "--profile", str(profile),
               "--db-root", str(resolved_db_root),
               "--request-id", request_id, "--json-out", str(out_json)]
        # Public-release boundary: never append --apply or
        # --enable-unrecorded, regardless of inherited environment values.
        if cycle_id:
            cmd += ["--self-cycle", str(cycle_id)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=_AUTOHEAL_TIMEOUT_SEC)
        return _read_autoheal_contract(
            out_json, request_id=request_id, profile=str(profile),
            cycle_id=cycle_id, db_root=resolved_db_root,
            returncode=proc.returncode,
        )
    except Exception as exc:  # noqa: BLE001
        return _autoheal_client_result(
            profile, resolved_db_root, cycle_id, request_id,
            status="client_error", rc=2,
            reason=f"{type(exc).__name__}: {exc}",
        )
    finally:
        try:
            out_json.unlink(missing_ok=True)
        except OSError:
            pass


def _autoheal_audit_view(result: dict[str, Any]) -> dict[str, Any]:
    """Keep the receipt useful without copying order ids or raw API payloads."""
    findings = []
    for item in result.get("findings", [])[:20]:
        if not isinstance(item, dict):
            continue
        findings.append({
            "kind": str(item.get("kind") or "UNKNOWN"),
            "sev": str(item.get("sev") or ""),
            "symbol": item.get("symbol"),
            "side": item.get("side"),
            "reason": str(item.get("reason") or "")[:240],
        })
    return {
        "contract_version": result.get("contract_version"),
        "request_id": result.get("request_id"),
        "status": result.get("status"),
        "rc": result.get("rc"),
        "applied": result.get("applied"),
        "p0": result.get("p0"),
        "blocking": result.get("blocking"),
        "findings": findings,
    }


def _verify_trigger_placed(
    symbol: str,
    pos_side: str,
    profile: str,
    trigger_kind: str,
    trigger_px: Optional[float] = None,
    tol_pct: float = 0.001,
    retries: int = 2,
    *,
    expected_sz: Optional[float] = None,
    since_ms: Optional[float] = None,
    expected_algo_id: Optional[str] = None,
    expected_ord_id: Optional[str] = None,
) -> dict[str, Any]:
    """回读 pending algo，确认是“本次、同侧、足量、有效”的 TP/SL。

    附挂 SL 没有调用方已知的 algoId，因此必须以 cTime>=本次下单时刻识别，旧的
    同价单不能通过。独立 algo 必须精确匹配刚返回的 algoId；即使个别展示字段缺失，
    这个强身份仍可兼容，但只要字段存在就必须与本次请求一致。
    """
    kind = str(trigger_kind or "").lower()
    if kind not in ("sl", "tp"):
        return {"verified": False, "found": [], "error": "invalid_trigger_kind"}
    field = "slTriggerPx" if kind == "sl" else "tpTriggerPx"
    expected_trigger = _to_float(trigger_px)
    if (expected_trigger is None or not math.isfinite(expected_trigger)
            or expected_trigger <= 0):
        return {"verified": False, "found": [],
                "error": f"invalid_{kind}_trigger_px"}
    expected_size = _to_float(expected_sz)
    if (expected_size is None or not math.isfinite(expected_size)
            or expected_size <= 0):
        return {"verified": False, "found": [], "error": "invalid_expected_sz"}
    start_ms = _to_float(since_ms)
    if start_ms is None or not math.isfinite(start_ms) or start_ms <= 0:
        return {"verified": False, "found": [], "error": "invalid_since_ms"}
    pos_side = str(pos_side or "").lower()
    if pos_side not in ("long", "short"):
        return {"verified": False, "found": [], "error": "invalid_pos_side"}
    close_side = "sell" if pos_side == "long" else "buy"
    tolerance = _to_float(tol_pct)
    if tolerance is None or not math.isfinite(tolerance) or tolerance < 0:
        return {"verified": False, "found": [], "error": "invalid_tolerance"}
    try:
        retry_count = max(1, int(retries))
    except (TypeError, ValueError, OverflowError):
        return {"verified": False, "found": [], "error": "invalid_retries"}

    expected_algo = (
        str(expected_algo_id) if expected_algo_id not in (None, "") else None)
    expected_order = (
        str(expected_ord_id) if expected_ord_id not in (None, "") else None)
    found: list[dict[str, Any]] = []
    for attempt in range(retry_count):
        try:
            algos = ox.get_algo_orders(symbol, profile)
        except Exception:
            algos = []
        found = []
        for a in algos:
            if not isinstance(a, dict):
                continue
            row_trigger = _to_float(a.get(field))
            if (row_trigger is None or not math.isfinite(row_trigger)
                    or row_trigger <= 0):
                continue
            algo_id = str(a.get("algoId") or "")
            exact_algo = bool(expected_algo and algo_id == expected_algo)
            errors: list[str] = []

            inst_id = str(a.get("instId") or "")
            if inst_id and inst_id != symbol:
                errors.append("symbol")
            if expected_algo and not exact_algo:
                errors.append("algoId")

            # pending 端点本身代表活动单；有 state 时仍必须显式是 live。
            state = str(a.get("state") or "").lower()
            if state and state != "live":
                errors.append("state")
            elif not state and not exact_algo:
                errors.append("state_missing")

            row_pos_side = str(a.get("posSide") or "").lower()
            if row_pos_side and row_pos_side != pos_side:
                errors.append("posSide")
            elif not row_pos_side and not exact_algo:
                errors.append("posSide_missing")
            row_side = str(a.get("side") or "").lower()
            if row_side and row_side != close_side:
                errors.append("side")
            elif not row_side and not exact_algo:
                errors.append("side_missing")

            raw_reduce_only = a.get("reduceOnly")
            if raw_reduce_only not in (None, ""):
                if str(raw_reduce_only).lower() not in ("true", "1"):
                    errors.append("reduceOnly")
            elif not exact_algo:
                errors.append("reduceOnly_missing")

            created_ms = _to_float(
                a.get("cTime") or a.get("createTime") or a.get("ts"))
            if created_ms is not None:
                # 不放宽到历史时间窗；稍有时钟疑义就走 belt SL，而不是让旧单过闸。
                if created_ms < start_ms:
                    errors.append("created_before_request")
            elif not exact_algo:
                errors.append("cTime_missing")

            row_sz = _to_float(a.get("sz"))
            if row_sz is not None:
                size_tol = max(_EPS, expected_size * 1e-9)
                if abs(row_sz - expected_size) > size_tol:
                    errors.append("sz")
            elif not exact_algo:
                errors.append("sz_missing")

            if abs(row_trigger - expected_trigger) / expected_trigger > tolerance:
                errors.append(field)

            linked = a.get("linkedOrd")
            linked_ord_id = ""
            if isinstance(linked, dict):
                linked_ord_id = str(linked.get("ordId") or "")
            if expected_order and linked_ord_id and linked_ord_id != expected_order:
                errors.append("linkedOrd")

            summary = {
                "algoId": algo_id or None,
                field: row_trigger,
                "posSide": row_pos_side or None,
                "side": row_side or None,
                "reduceOnly": raw_reduce_only,
                "state": state or None,
                "cTime": created_ms,
                "sz": row_sz,
                "errors": errors,
            }
            found.append(summary)
            if not errors:
                return {"verified": True, "found": found, "matched": summary}
        if attempt < retry_count - 1:
            time.sleep(1.0)
    return {"verified": False, "found": found}


def _verify_sl_placed(
    symbol: str, pos_side: str, profile: str,
    sl_trigger_px: Optional[float] = None, tol_pct: float = 0.001,
    retries: int = 2, *, expected_sz: Optional[float] = None,
    since_ms: Optional[float] = None,
    expected_algo_id: Optional[str] = None,
    expected_ord_id: Optional[str] = None,
) -> dict[str, Any]:
    return _verify_trigger_placed(
        symbol, pos_side, profile, "sl", sl_trigger_px, tol_pct, retries,
        expected_sz=expected_sz, since_ms=since_ms,
        expected_algo_id=expected_algo_id, expected_ord_id=expected_ord_id,
    )


def _verify_tp_placed(
    symbol: str, pos_side: str, profile: str,
    tp_trigger_px: Optional[float] = None, tol_pct: float = 0.001,
    retries: int = 2, *, expected_sz: Optional[float] = None,
    since_ms: Optional[float] = None,
    expected_algo_id: Optional[str] = None,
    expected_ord_id: Optional[str] = None,
) -> dict[str, Any]:
    return _verify_trigger_placed(
        symbol, pos_side, profile, "tp", tp_trigger_px, tol_pct, retries,
        expected_sz=expected_sz, since_ms=since_ms,
        expected_algo_id=expected_algo_id, expected_ord_id=expected_ord_id,
    )


# ---------------------------------------------------------------------------
# OPEN
# ---------------------------------------------------------------------------
def validate_receipt_context(
    context: Optional[dict[str, Any]],
    *,
    cycle_id: Optional[str] = None,
    required: bool = True,
    expected_symbol: Optional[str] = None,
    expected_side: Optional[str] = None,
    expected_regime: Optional[str] = None,
    require_experience: bool = False,
) -> list[str]:
    """Validate the full decision envelope before any exchange side effect.

    Agents pass a JSON-decoded dict here.  This deliberately moves JSON/Python
    syntax and decision-card failures in front of ``place_market_open``.
    """
    if context is None:
        return ["receipt_context 必填"] if required else []
    if not isinstance(context, dict):
        return ["receipt_context 必须是 dict（建议先 json.loads 有效 JSON）"]
    errors: list[str] = []
    if str(context.get("status") or "").strip().lower() != "ok":
        errors.append("receipt_context.status 必须是 ok")
    if context.get("decision_protocol") != "decision_card_v1":
        errors.append("receipt_context.decision_protocol 必须是 decision_card_v1")
    errors.extend(validate_card(context.get("decision_card"),
                                "receipt_context.decision_card"))
    card = context.get("decision_card")
    if expected_symbol is not None:
        errors.extend(validate_multitimeframe_analysis(
            card,
            "receipt_context.decision_card",
            expected_cycle=str(cycle_id or ""),
            expected_side=expected_side,
            expected_symbol=expected_symbol,
        ))
    history = (
        card.get("historical_experience")
        if isinstance(card, dict) else None
    )
    evidence_contract = (
        history.get("evidence_contract")
        if isinstance(history, dict) else None
    )
    if require_experience or evidence_contract is not None:
        expected_as_of = None
        try:
            if cycle_id:
                expected_as_of = datetime.strptime(
                    cycle_id, "%Y-%m-%dT%H:%M"
                ).strftime("%Y-%m-%d %H:%M:00")
        except (TypeError, ValueError):
            expected_as_of = None
        contract_errors = validate_experience_contract(
            evidence_contract,
            expected_symbol=expected_symbol,
            expected_side=expected_side,
            expected_regime=expected_regime,
            expected_action="open" if expected_symbol else None,
            expected_profile="live" if expected_symbol else None,
            expected_as_of=expected_as_of,
        )
        errors.extend(
            "receipt_context.decision_card.historical_experience."
            f"evidence_contract: {item}"
            for item in contract_errors
        )
    ctx_cycle = context.get("cycle_id")
    if cycle_id and ctx_cycle != cycle_id:
        errors.append(
            f"receipt_context.cycle_id={ctx_cycle!r} 与参数 cycle_id={cycle_id!r} 不一致")
    try:
        json.dumps(context, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append(f"receipt_context 不是有效 JSON 数据: {exc}")
    return errors


def open_position(
    symbol: str,
    side: str,
    intended_sz: float,
    lev: float,
    sl_trigger_px: Optional[float],
    profile: str,
    mgn_mode: str = "cross",
    mark_px: Optional[float] = None,
    equity: Optional[float] = None,
    open_positions: Optional[list[dict[str, Any]]] = None,
    reasoning: str = "",
    db_root: Path = DEFAULT_DB_ROOT,
    cycle_id: Optional[str] = None,
    available_margin: Optional[float] = None,
    receipt_context: Optional[dict[str, Any]] = None,
    account_imr: Optional[float] = None,
    tp_trigger_px: Optional[float] = None,
    expected_pre_position_exists: Optional[bool] = None,
    expected_pre_position_sz: Optional[float] = None,
    expected_pre_position_pos_id: Optional[str] = None,
    expected_pre_position_c_time: Optional[str] = None,
    target_stop_risk_pct_equity: Optional[float] = None,
) -> dict[str, Any]:
    _require_live_profile(profile, "open_position")
    profile_label = "live"
    side = str(side or "").lower()
    action_taken = "OPEN_LONG" if side == "long" else "OPEN_SHORT"
    capacity_audit: Optional[dict[str, Any]] = None
    position_reconciliation_audit: Optional[dict[str, Any]] = None
    multitimeframe_readiness_audit: Optional[dict[str, Any]] = None
    multitimeframe_evidence_anchor_audit: Optional[dict[str, Any]] = None
    intent_path = Path(db_root) / "ledger.db"
    intent_fingerprint: Optional[str] = None
    intent_ord_id: Optional[str] = None

    def receipt(ok: bool, **kw) -> dict[str, Any]:
        # receipt_context 已在下单前完整验证；返回时直接携带，调用方只需
        # json.dump(result)，不再在成交后手拼 JSON/Python 字面量。
        base = dict(receipt_context or {})
        base.update({"profile": profile_label, "ok": ok,
                "action_taken": kw.pop("action_taken", action_taken),
                "symbol": symbol, "side": side, "trades": kw.pop("trades", []),
                "p0": kw.pop("p0", False), "cycle_id": cycle_id})
        if capacity_audit is not None:
            base["capacity"] = dict(capacity_audit)
        if position_reconciliation_audit is not None:
            base["position_reconciliation"] = dict(position_reconciliation_audit)
        if multitimeframe_readiness_audit is not None:
            base["multitimeframe_readiness"] = dict(
                multitimeframe_readiness_audit)
        if multitimeframe_evidence_anchor_audit is not None:
            base["multitimeframe_evidence_anchor"] = dict(
                multitimeframe_evidence_anchor_audit)
        base.update(kw)
        return base

    def _intent_kwargs(now_ts: Optional[str] = None) -> dict[str, Any]:
        return {
            "profile": profile_label,
            "cycle_id": str(cycle_id),
            "symbol": symbol,
            "side": side,
            "fingerprint": str(intent_fingerprint),
            "now_ts": now_ts or ledger.now_cst(),
        }

    def _finish_clean(result: dict[str, Any], error: str) -> dict[str, Any]:
        if intent_fingerprint:
            try:
                ei.mark_failed_clean(intent_path, error=error, **_intent_kwargs())
            except Exception as exc:
                result["intent_persist_warning"] = (
                    f"failed_clean transition failed: {type(exc).__name__}: {exc}")
                result["p0"] = True
        return result

    def _finish_uncertain(result: dict[str, Any], error: str) -> dict[str, Any]:
        if intent_fingerprint:
            try:
                ei.mark_uncertain(
                    intent_path, ord_id=intent_ord_id, error=error,
                    **_intent_kwargs())
            except Exception as exc:
                result["intent_persist_warning"] = (
                    f"uncertain transition failed: {type(exc).__name__}: {exc}")
        result["p0"] = True
        return result

    def _finish_completed(result: dict[str, Any]) -> dict[str, Any]:
        if intent_fingerprint:
            try:
                ei.mark_completed(
                    intent_path, ord_id=intent_ord_id, receipt=result,
                    error=None, **_intent_kwargs())
            except Exception as exc:
                # 先前 reserved/submitting/submitted 状态仍会阻断重下；成交回执
                # 继续返给 writer，避免因幂等存储告警反而丢主账事实。
                result["intent_persist_warning"] = (
                    f"completed transition failed: {type(exc).__name__}: {exc}")
                result["p0"] = True
                _enqueue_repair(
                    profile_label, symbol, intent_ord_id,
                    "execution_intent_complete_failed", db_root)
        return result

    # 字符串数字是 CLI/JSON 常态，入口统一成有限正数。非法 SL 在任何
    # 账户/下单 I/O 前 fail-safe 拒绝，避免已成交后才在回读比较处爆类型异常。
    if sl_trigger_px is None:
        return receipt(False, action_taken="REJECT", reject_reason="no_sl",
                       reject_detail="开仓必须提供止损价（§4 红线）")
    normalized_sl = _to_float(sl_trigger_px)
    if (normalized_sl is None or not math.isfinite(normalized_sl)
            or normalized_sl <= 0):
        return receipt(False, action_taken="REJECT", reject_reason="bad_sl",
                       reject_detail=f"止损价非法: {sl_trigger_px}")
    sl_trigger_px = normalized_sl
    if tp_trigger_px is not None:
        normalized_tp = _to_float(tp_trigger_px)
        if (normalized_tp is None or not math.isfinite(normalized_tp)
                or normalized_tp <= 0):
            return receipt(
                False, action_taken="REJECT", reject_reason="bad_tp",
                reject_detail=f"止盈价非法: {tp_trigger_px}")
        tp_trigger_px = normalized_tp
    normalized_lev = _to_float(lev)
    if (normalized_lev is None or not math.isfinite(normalized_lev)
            or normalized_lev <= 0):
        return receipt(False, action_taken="REJECT", reject_reason="bad_lev",
                       reject_detail=f"杠杆非法: {lev}")
    lev = normalized_lev
    normalized_sz = _to_float(intended_sz)
    if (normalized_sz is None or not math.isfinite(normalized_sz)
            or normalized_sz <= 0):
        return receipt(False, action_taken="REJECT", reject_reason="bad_sz",
                       reject_detail=f"开仓张数非法: {intended_sz}")
    intended_sz = normalized_sz

    # 非 dry-run 必须把完整执行决策卡作为上下文传入，并在任何账户/交易所
    # I/O 前完成 JSON 与卡片校验。这样 Python 中误写 JSON 的 true/false 会在
    # 调 open_position 之前失败，而不是成交后组回执时失败。
    ctx_errors = validate_receipt_context(
        receipt_context,
        cycle_id=cycle_id,
        required=not ox.is_dryrun(),
        expected_symbol=symbol,
        expected_side=side,
        expected_regime=(
            str((receipt_context or {}).get("regime") or "") or None
        ),
        require_experience=not ox.is_dryrun(),
    )
    if ctx_errors:
        return receipt(
            False, action_taken="REJECT",
            reject_reason="receipt_context_invalid",
            reject_detail="；".join(ctx_errors))
    card = (receipt_context or {}).get("decision_card") or {}
    rr = card.get("risk_reward") if isinstance(card, dict) else None
    exit_mode = None
    if isinstance(rr, dict) and "exit_mode" in rr:
        exit_mode = str(rr.get("exit_mode") or "").strip().lower()
        if exit_mode not in EXIT_MODES:
            return receipt(
                False, action_taken="REJECT",
                reject_reason="exit_mode_invalid",
                reject_detail=(
                    "risk_reward.exit_mode 必须是 "
                    "fixed_tp|dynamic_exit|no_fixed_tp"),
            )
        if exit_mode == "fixed_tp" and tp_trigger_px is None:
            return receipt(
                False, action_taken="REJECT",
                reject_reason="fixed_tp_required",
                reject_detail="exit_mode=fixed_tp 必须传 tp_trigger_px",
            )
        if exit_mode in {"dynamic_exit", "no_fixed_tp"} \
                and tp_trigger_px is not None:
            return receipt(
                False, action_taken="REJECT",
                reject_reason="tp_not_allowed_for_exit_mode",
                reject_detail=(
                    f"exit_mode={exit_mode} 时开仓不得附挂固定 TP；"
                    "卡内 target 仅作 EV/参考目标"),
            )
    if not ox.is_dryrun() and not cycle_id:
        return receipt(
            False, action_taken="REJECT", reject_reason="cycle_id_required",
            reject_detail="非 dry-run 开仓必须提供调度 cycle_id")
    if not ox.is_dryrun():
        deadline_reject = _cycle_side_effect_reject(cycle_id)
        if deadline_reject:
            return receipt(False, **deadline_reject)

    # Wave1 序6 接管闸（终稿边界表 #3 / T4）：分析与执行 actor epoch 不同
    # （overloaded 切换等）时，OPEN/ADD 必须携带确定性重验凭证；同 actor
    # 正常轮零负担直通。非 dry-run 时间线不可得时无法证明未接管，必须在
    # 任何账户/订单 I/O 前 fail-closed；CLOSE/REDUCE 去风险恒不受阻。
    actor_timeline_note = None
    if not ox.is_dryrun():
        try:
            _analysis_ts = None
            _acon = None
            try:
                _acon = sqlite3.connect(
                    f"file:{Path(db_root) / 'analysis.db'}?mode=ro",
                    uri=True, timeout=5)
                _row = _acon.execute(
                    "SELECT ts FROM analysis_runs WHERE cycle_id=?",
                    (cycle_id,)).fetchone()
                _analysis_ts = _row[0] if _row else None
            except sqlite3.Error:
                _analysis_ts = None
            finally:
                if _acon is not None:
                    _acon.close()
            _tl = actor_att.timeline_state(cycle_id, _analysis_ts)
        except Exception as exc:  # noqa: BLE001
            _tl = {"available": False,
                   "reason": f"timeline_error:{type(exc).__name__}"}
        if not _tl.get("available"):
            return receipt(
                False, action_taken="REJECT",
                reject_reason="actor_timeline_required",
                reject_detail=(
                    "非 dry-run OPEN/ADD 必须能解析本 cycle actor 时间线；"
                    f"当前不可用: {_tl.get('reason')}"),
            )
        elif _tl.get("handoff_detected"):
            _att = (receipt_context or {}).get("actor_attestation")
            _att_errors = actor_att.verify_attestation(
                _att, cycle_id, db_root=db_root)
            if _att_errors:
                return receipt(
                    False, action_taken="REJECT",
                    reject_reason="handoff_attestation_required",
                    reject_detail=(
                        "检测到分析与执行 actor 不同"
                        f"（epoch {_tl.get('analysis_epoch')}→"
                        f"{_tl.get('current_epoch')}），接管必须先跑 "
                        "scripts/actor_attestation.py 重验并把产物整份放入 "
                        "receipt_context['actor_attestation']: "
                        + "；".join(_att_errors)))
            actor_timeline_note = (
                f"handoff_verified:chain={_tl.get('actor_chain_hash')}")

    # 先于任何交易所读取占住逻辑意图；相同已完成请求直接返回原回执。
    # 同 profile 任一标的存在 in-flight/uncertain 时全局 fail-closed，避免在
    # 前一笔交易真实状态尚未对清时继续扩大账户状态分叉。
    if not ox.is_dryrun():
        request = {
            "profile": profile_label,
            "cycle_id": cycle_id,
            "symbol": symbol,
            "action": "open",
            "side": side,
            "intended_sz": intended_sz,
            "lev": lev,
            "sl_trigger_px": sl_trigger_px,
            "tp_trigger_px": tp_trigger_px,
            "mgn_mode": mgn_mode,
            "expected_pre_position_exists": expected_pre_position_exists,
            "expected_pre_position_sz": expected_pre_position_sz,
            "expected_pre_position_pos_id": expected_pre_position_pos_id,
            "expected_pre_position_c_time": expected_pre_position_c_time,
            "target_stop_risk_pct_equity": target_stop_risk_pct_equity,
        }
        try:
            reserved = ei.reserve(
                intent_path, profile=profile_label, cycle_id=str(cycle_id),
                symbol=symbol, side=side, request=request,
                now_ts=ledger.now_cst())
        except Exception as exc:
            return receipt(
                False, action_taken="REJECT",
                reject_reason="execution_intent_store_failed",
                reject_detail=f"幂等意图库不可用，拒绝触发订单: {type(exc).__name__}: {exc}",
                p0=True)
        if reserved["status"] == "replay":
            cached = dict(reserved["receipt"])
            cached["idempotent_replay"] = True
            cached["intent_state"] = "completed"
            return cached
        if reserved["status"] != "reserved":
            blocker = reserved.get("blocking_intent")
            blocker = blocker if isinstance(blocker, dict) else {}
            blocker_ref = (
                f"profile={blocker.get('profile') or profile_label},"
                f"cycle={blocker.get('cycle_id')},"
                f"symbol={blocker.get('symbol')},"
                f"action={blocker.get('action')},side={blocker.get('side')},"
                f"state={blocker.get('state') or reserved.get('state')},"
                f"ordId={blocker.get('ord_id') or reserved.get('ord_id')}"
            )
            is_profile_block = (
                reserved.get("reason") == "profile_pending_intent")
            _enqueue_repair(
                profile_label, symbol, reserved.get("ord_id"),
                f"execution_intent_blocked:{reserved.get('reason')}:"
                f"{blocker_ref}:pending_count={reserved.get('pending_count')}",
                db_root)
            if is_profile_block:
                reject_reason = "execution_intent_profile_blocked"
                reject_detail = (
                    "同一 profile 存在尚未完成或未确认清洁失败的执行意图；"
                    f"已在任何交易所读取/下单前全局阻断。阻塞项: {blocker_ref}。"
                    "需先核对 execution_intents、journal、交易所与主账并完成对账")
            else:
                reject_reason = "execution_intent_blocked"
                reject_detail = (
                    "同一 cycle/symbol/side 已有未决或冲突执行意图；"
                    "已阻断重复下单，需先核对 journal/交易所/主账")
            return receipt(
                False, action_taken="REJECT",
                reject_reason=reject_reason,
                reject_detail=reject_detail,
                intent_state=reserved.get("state"),
                intent_reason=reserved.get("reason"),
                blocking_intent=blocker or None,
                pending_intent_count=reserved.get("pending_count"),
                p0=True)
        intent_fingerprint = str(reserved["fingerprint"])

    # 目标2硬闸：OPEN/ADD 只允许使用本调度 cycle 对应的精确已收盘
    # 15m/1H/4H K线，且 OHLCV 与全部决策指标都有效。新上市暖机不足、
    # 陈旧回退、库不可读均 clean reject；CLOSE/REDUCE 不经过本函数，去风险
    # 路径不受影响。闸门只读 market.db，位于任何交易所读取/下单之前。
    multitimeframe_readiness_audit = check_multitimeframe_readiness(
        db_root, symbol, str(cycle_id))
    if not multitimeframe_readiness_audit.get("ready"):
        gaps = [
            f"{row.get('timeframe')}:{row.get('classification')}"
            for row in multitimeframe_readiness_audit.get("timeframes", [])
            if not row.get("ready")
        ]
        detail = (
            "OPEN/ADD 必须具备本 cycle 精确已收盘的15m/1H/4H OHLCV及"
            "MA5/MA20/ATR14/RSI14/MACD完整指标；"
            f"当前未就绪: {','.join(gaps) or multitimeframe_readiness_audit.get('error') or 'unknown'}"
        )
        return _finish_clean(
            receipt(
                False,
                action_taken="REJECT",
                reject_reason="multitimeframe_data_not_ready",
                reject_detail=detail,
            ),
            "multitimeframe_data_not_ready",
        )
    card_multitimeframe = (
        ((receipt_context or {}).get("decision_card") or {}).get(
            "multitimeframe_analysis")
        if isinstance((receipt_context or {}).get("decision_card"), dict)
        else None
    )
    supplied_contract = (
        card_multitimeframe.get("evidence_contract")
        if isinstance(card_multitimeframe, dict) else None
    )
    actual_contract = multitimeframe_readiness_audit.get("evidence_contract")
    multitimeframe_evidence_anchor_audit = resolve_execution_evidence_anchor(
        db_root,
        symbol,
        str(cycle_id),
        side,
        supplied_contract,
        actual_contract,
    )
    if not multitimeframe_evidence_anchor_audit.get("ok"):
        return _finish_clean(
            receipt(
                False,
                action_taken="REJECT",
                reject_reason="multitimeframe_context_mismatch",
                reject_detail=(
                    "决策卡三周期证据既不等于 market.db 当前精确闭合真值，"
                    "也不等于 analysis.db 中本 cycle 经 writer 验证的持久化证据；"
                    "拒绝使用旧周期、篡改或未持久化证据开仓"
                ),
            ),
            "multitimeframe_context_mismatch",
        )

    # ── 装配现场：硬闸输入一律以 OKX API 为权威，禁 caller 注入绕闸 ──
    # 非 dryrun 一律以 API 真值为准，caller 传值仅留作偏差留痕。
    # 现仓 API 失败 = 敞口未知 → 拒单，不当零仓。
    input_divergence: list[str] = []
    if not ox.is_dryrun():
        caller_equity = equity
        caller_available_margin = available_margin
        caller_account_imr = account_imr
        # demo 分支（容量不读 balance，改按 account max-size 实时取）随 2026-08-06
        # 全量下线移除；只剩 live 的余额/IMR 路径。
        # live 余额只拉一次：totalEq 是全账户折 USD 权益，不等于
        # USDT-SWAP 可用结算币保证金；必须从 details.USDT 取
        # availBal/availEq；账户组合 IMR 同样只认这次 balance 的顶层 imr。
        # 任一缺失都禁回退 caller/totalEq。
        try:
            balance_payload = ox.get_balance(profile)
        except Exception as exc:
            balance_payload = {"ok": False, "error": str(exc)}
        capacity = ac.extract_settlement_capacity(balance_payload, "USDT")
        api_equity = _to_float(capacity.get("total_equity"))
        api_available_margin = _to_float(capacity.get("available_margin"))
        api_account_imr = _to_float(capacity.get("account_imr"))
        capacity_audit = {
            "settlement_ccy": capacity.get("settlement_ccy", "USDT"),
            "source": capacity.get("source"),
            "available_margin": api_available_margin,
            "account_imr": api_account_imr,
            "account_imr_source": capacity.get("account_imr_source"),
            "account_imr_error": capacity.get("account_imr_error"),
            "account_mgn_ratio_observation_only": capacity.get(
                "account_mgn_ratio_observation_only"
            ),
            "frozen_balance": capacity.get("frozen_balance"),
            "error": capacity.get("error"),
        }
        if (api_account_imr is None
                or not math.isfinite(api_account_imr)
                or api_account_imr < 0):
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="account_imr_fetch_failed",
                reject_detail=(
                    "账户初始保证金 imr 不可用，拒开"
                    "（禁回退 caller/持仓本地估算）: "
                    f"{capacity.get('account_imr_error') or capacity.get('error')}"),
                p0=True,
            ), "account_imr_fetch_failed")
        if (not capacity.get("ok") or api_available_margin is None
                or not math.isfinite(api_available_margin)
                or api_available_margin < 0):
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="available_margin_fetch_failed",
                reject_detail=(
                    "USDT 可用保证金不可用，拒开（禁回退 totalEq/caller）: "
                    f"{capacity.get('error')}"),
                p0=True,
            ), "available_margin_fetch_failed")
        if (api_equity is None or not math.isfinite(api_equity)
                or api_equity <= 0):
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="equity_fetch_failed",
                reject_detail="totalEq 非法，拒开（禁回退 caller）",
                p0=True,
            ), "equity_fetch_failed")
        if (caller_equity is not None
                and abs((_to_float(caller_equity) or 0.0)
                        - api_equity) > 1.0):
            input_divergence.append(
                f"equity caller={caller_equity} → API={api_equity}")
        if (caller_available_margin is not None
                and abs((_to_float(caller_available_margin) or 0.0)
                        - api_available_margin) > 1.0):
            input_divergence.append(
                "available_margin "
                f"caller={caller_available_margin} → API={api_available_margin}")
        if (caller_account_imr is not None
                and abs((_to_float(caller_account_imr) or 0.0)
                        - api_account_imr) > 1.0):
            input_divergence.append(
                f"account_imr caller={caller_account_imr} "
                f"→ API={api_account_imr}")
        equity = api_equity
        available_margin = api_available_margin
        account_imr = api_account_imr
        try:
            api_positions = fetch_open_positions(profile)
        except PositionsUnavailable as exc:
            return _finish_clean(receipt(False, action_taken="REJECT",
                           reject_reason="positions_fetch_failed",
                           reject_detail=f"现仓 API 失败，拒开（不当零仓放行）: {exc}", p0=True),
                                 "positions_fetch_failed")
        if open_positions is not None and len(open_positions) != len(api_positions):
            input_divergence.append(
                f"positions caller={len(open_positions)} → API={len(api_positions)}")
        open_positions = api_positions
        # 交易前账仓闸：只使用本次 OKX API 全仓与 profile 真账本轧差，不采信
        # caller 快照。必须在 mark/规格/杠杆/下单前完成，任何不可读或差异都
        # failed_clean，修账/对账后方可重试同一 intent。
        try:
            position_check = _verify_pretrade_ledger_positions(
                profile_label, db_root, api_positions)
        except TradeLedgerUnavailable as exc:
            _enqueue_repair(
                profile_label, symbol, None,
                f"pretrade_ledger_unavailable:{exc}", db_root)
            return _finish_clean(
                receipt(
                    False, action_taken="REJECT",
                    reject_reason="ledger_unavailable",
                    reject_detail=(
                        "交易前 profile 交易账本缺失、不可读或含非法成交行；"
                        f"已在 mark/下单前阻断: {exc}"),
                    position_reconciliation={
                        "ok": False, "profile": profile_label,
                        "error": str(exc),
                    },
                    p0=True,
                ),
                "pretrade_ledger_unavailable",
            )
        except PositionsUnavailable as exc:
            _enqueue_repair(
                profile_label, symbol, None,
                f"pretrade_api_positions_invalid:{exc}", db_root)
            return _finish_clean(
                receipt(
                    False, action_taken="REJECT",
                    reject_reason="positions_fetch_failed",
                    reject_detail=(
                        "OKX API 全仓归一结果非法，无法执行交易前账仓核对: "
                        f"{exc}"),
                    p0=True,
                ),
                "pretrade_api_positions_invalid",
            )
        position_reconciliation_audit = position_check
        if not position_check["ok"]:
            autoheal_result = _try_autoheal_ledger(
                profile_label, db_root, cycle_id)
            autoheal_audit = _autoheal_audit_view(autoheal_result)
            position_reconciliation_audit = {
                **position_check,
                "autoheal": autoheal_audit,
            }
            if autoheal_result.get("blocking"):
                kinds = sorted({
                    str(item.get("kind") or "UNKNOWN")
                    for item in autoheal_result.get("findings", [])
                    if isinstance(item, dict)
                })
                detail = (
                    f"ledger_autoheal status={autoheal_result.get('status')} "
                    f"rc={autoheal_result.get('rc')} "
                    f"findings={','.join(kinds) or 'UNKNOWN'}"
                )
                _enqueue_repair(
                    profile_label, symbol, None,
                    f"pretrade_ledger_autoheal_blocked:{detail}", db_root)
                return _finish_clean(
                    receipt(
                        False, action_taken="REJECT",
                        reject_reason="pretrade_ledger_autoheal_blocked",
                        reject_detail=(
                            "交易前账仓不一致，且账本自愈契约为阻断态；"
                            "已在重新校验、mark、风险计算和下单前停止。"
                            f" {detail}"),
                        p0=True,
                    ),
                    "pretrade_ledger_autoheal_blocked",
                )
            if autoheal_result.get("applied") is True:
                # 只有 rc=0/blocking=false 且真写入后才重验；仍使用同份
                # api_positions，不为放行重复打 API。
                try:
                    position_check = _verify_pretrade_ledger_positions(
                        profile_label, db_root, api_positions)
                    position_reconciliation_audit = {
                        **position_check,
                        "autoheal": autoheal_audit,
                    }
                except (TradeLedgerUnavailable, PositionsUnavailable):
                    pass
        if not position_check["ok"]:
            diffs = position_check["diffs"]
            compact = ";".join(
                f"{d['symbol']}/{d['side']}:"
                f"db={d['ledger_sz']},okx={d['exchange_sz']},"
                f"delta={d['delta']}"
                for d in diffs[:4]
            )
            if len(diffs) > 4:
                compact += f";...+{len(diffs) - 4}"
            _enqueue_repair(
                profile_label, symbol, None,
                f"pretrade_ledger_position_mismatch:"
                f"count={len(diffs)}:{compact}", db_root)
            return _finish_clean(
                receipt(
                    False, action_taken="REJECT",
                    reject_reason="pretrade_ledger_position_mismatch",
                    reject_detail=(
                        "交易前账本轧差与 OKX API 全仓不一致；"
                        f"已在 mark/下单前阻断，共 {len(diffs)} 项: {compact}"),
                    position_reconciliation={
                        **position_check,
                        "diffs": diffs[:20],
                        "truncated": len(diffs) > 20,
                    },
                    p0=True,
                ),
                "pretrade_ledger_position_mismatch",
            )
        # mark_px：API 失败一律拒（同 fail-safe，防注入架空价影响 sz/notional/SL 偏离校验）
        api_mark = ox.get_mark_price(symbol, profile)
        if api_mark is None:
            return _finish_clean(receipt(False, action_taken="REJECT",
                           reject_reason="mark_px_fetch_failed",
                           reject_detail="mark_px API 失败，拒开（禁回退 caller 值）", p0=True),
                                 "mark_px_fetch_failed")
        mark_px = api_mark
        if input_divergence:  # 注入尝试可观测（不阻断，已用真值）
            print(f"[order_executor] WARN input_divergence: {input_divergence}", file=sys.stderr)
    else:
        # dryrun/单测：live 允许显式注入 equity/available_margin/account_imr，
        # 缺项时由同一次 balance 补齐。
        if (equity is None or available_margin is None
                or account_imr is None):
            try:
                capacity = ac.extract_settlement_capacity(
                    ox.get_balance(profile), "USDT")
            except Exception:
                capacity = {"ok": False, "error": "balance_unavailable"}
            if equity is None:
                equity = _to_float(capacity.get("total_equity"))
            if available_margin is None and capacity.get("ok"):
                available_margin = _to_float(capacity.get("available_margin"))
            if account_imr is None:
                account_imr = _to_float(capacity.get("account_imr"))
            capacity_audit = {
                "settlement_ccy": capacity.get("settlement_ccy", "USDT"),
                "source": capacity.get("source"),
                "available_margin": available_margin,
                "account_imr": account_imr,
                "account_imr_source": capacity.get("account_imr_source"),
                "account_imr_error": capacity.get("account_imr_error"),
                "account_mgn_ratio_observation_only": capacity.get(
                    "account_mgn_ratio_observation_only"
                ),
                "frozen_balance": capacity.get("frozen_balance"),
                "error": capacity.get("error"),
            }
        elif available_margin is not None and account_imr is not None:
            capacity_audit = {
                "settlement_ccy": "USDT", "source": "caller_dryrun",
                "available_margin": available_margin,
                "account_imr": _to_float(account_imr),
                "frozen_balance": None,
            }
        account_imr = _to_float(account_imr)
        if (account_imr is None or not math.isfinite(account_imr)
                or account_imr < 0):
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="account_imr_fetch_failed",
                reject_detail=(
                    "账户初始保证金 imr 不可用，拒开"
                    "（dryrun/单测须显式注入或由 balance 提供）"),
                p0=True,
            ), "account_imr_fetch_failed")
        if open_positions is None:
            try:
                open_positions = fetch_open_positions(profile)
            except PositionsUnavailable:
                open_positions = []
        if mark_px is None:
            mark_px = ox.get_mark_price(symbol, profile)
    # 开仓前同标的同侧张数：>0 即本次是**加仓**，成交后须把止损收敛并扩到全仓。
    # 取自已核对过的 open_positions（非快照），成交后不再重取以免与 fill 竞态。
    pre_position_sz = _position_size(open_positions or [], symbol, side)
    actual_pre_position = next((
        row for row in (open_positions or [])
        if row.get("symbol") == symbol
        and str(row.get("side") or "").lower() == side
    ), None)
    fingerprint_error = _position_fingerprint_error(
        actual_pre_position,
        expected_exists=expected_pre_position_exists,
        expected_sz=expected_pre_position_sz,
        expected_pos_id=expected_pre_position_pos_id,
        expected_c_time=expected_pre_position_c_time,
    )
    if fingerprint_error:
        return _finish_clean(receipt(
            False,
            action_taken="REJECT",
            reject_reason="pre_position_semantics_changed",
            reject_detail=(
                "执行时同 symbol/side 仓位与 plan/facts 指纹不一致；"
                "已在 set_leverage/order I/O 前 clean reject: "
                + fingerprint_error
            ),
            expected_pre_position={
                "exists": expected_pre_position_exists,
                "sz": expected_pre_position_sz,
                "posId": expected_pre_position_pos_id,
                "cTime": expected_pre_position_c_time,
            },
            actual_pre_position=actual_pre_position,
        ), "pre_position_semantics_changed")
    if tp_trigger_px is not None:
        mark_for_tp = _to_float(mark_px)
        if mark_for_tp is None or not math.isfinite(mark_for_tp) or mark_for_tp <= 0:
            return _finish_clean(receipt(
                False, action_taken="REJECT", reject_reason="mark_px_fetch_failed",
                reject_detail="止盈几何校验需要有效 mark_px"),
                "mark_px_fetch_failed")
        bad_geometry = (
            side == "long" and tp_trigger_px <= mark_for_tp
        ) or (
            side == "short" and tp_trigger_px >= mark_for_tp
        )
        if bad_geometry:
            return _finish_clean(receipt(
                False, action_taken="REJECT", reject_reason="bad_tp_geometry",
                reject_detail=(
                    f"止盈方向非法: side={side} mark={mark_px} tp={tp_trigger_px}"),
            ), "bad_tp_geometry")
        target = _to_float(rr.get("target")) if isinstance(rr, dict) else None
        if (target is None
                or abs(target - tp_trigger_px) / tp_trigger_px > 0.001):
            return _finish_clean(receipt(
                False, action_taken="REJECT", reject_reason="tp_context_mismatch",
                reject_detail=(
                    "tp_trigger_px 必须与已验证 decision_card.risk_reward.target "
                    f"一致（target={target}, tp={tp_trigger_px}）"),
            ), "tp_context_mismatch")
    specs = fetch_instrument_specs(symbol, profile, db_root)
    ct_val = specs.get("ct_val")
    lot_sz = specs.get("lot_sz")
    min_sz = specs.get("min_sz")
    authoritative_target_sizing: Optional[dict[str, Any]] = None
    if target_stop_risk_pct_equity is not None:
        authoritative_target_sizing = rv.size_for_target_stop_risk(
            mark_px=mark_px,
            ct_val=ct_val,
            lot_sz=lot_sz,
            min_order_size=min_sz,
            equity=equity,
            sl_trigger_px=sl_trigger_px,
            target_risk_pct_equity=target_stop_risk_pct_equity,
        )
        if authoritative_target_sizing.get("ok") is not True:
            return _finish_clean(receipt(
                False,
                action_taken="REJECT",
                reject_reason="authoritative_target_sizing_failed",
                reject_detail=str(
                    authoritative_target_sizing.get("error")
                    or "unknown_target_sizing_error"
                ),
                authoritative_target_sizing=authoritative_target_sizing,
            ), "authoritative_target_sizing_failed")
        intended_sz = authoritative_target_sizing["intended_sz"]
    new_open = not any(
        (p.get("symbol") == symbol and str(p.get("side", "")).lower() == side)
        for p in open_positions)
    lev_warn = None

    # demo 的两阶段 max-size 定仓分支（预检 → set_leverage → account max-size →
    # 按 minSz/lotSz 收敛）随 2026-08-06 全量下线移除；只剩 live 的完整预算闸。
    # Live 保持 1% 名义下限、可用保证金×98% 与组合 IMR 66.6% 整单拒绝规则。
    v = rv.validate(
        symbol=symbol, side=side, intended_sz=intended_sz, lev=lev,
        mark_px=mark_px, ct_val=ct_val, lot_sz=lot_sz, equity=equity,
        open_positions=open_positions, sl_trigger_px=sl_trigger_px,
        profile="live", available_margin=available_margin,
        account_imr=account_imr,
    )

    if not v["approved"]:
        return _finish_clean(receipt(
            False, action_taken="REJECT",
            reject_reason=v["reject_reason"],
            reject_detail=v["reject_detail"], risk=v,
        ), f"risk_reject:{v['reject_reason']}")
    approved_sz = v["approved_sz"]
    # 加仓时不改杠杆；validator 已用现仓实际杠杆记录成交保证金口径。
    effective_lev = (_to_float((v.get("math") or {}).get("effective_lev"))
                     or lev)

    # live 在完整预算闸通过后才设杠杆。前面的分析、现场读取和风控可能
    # 消耗较久，因此在首次交易所写前再次检查自然轮硬截止。
    if not ox.is_dryrun():
        deadline_reject = _cycle_side_effect_reject(cycle_id)
        if deadline_reject:
            return _finish_clean(
                receipt(False, risk=v, **deadline_reject),
                str(deadline_reject["reject_reason"]),
            )
    if new_open:
        lr = ox.set_leverage(symbol, lev, mgn_mode, profile)
        if not lr.get("ok"):
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="set_leverage_failed",
                reject_detail=str(lr.get("sMsg") or lr.get("error")),
                risk=v,
            ), "set_leverage_failed")

    # ── 市价开仓（只附挂 SL）──
    # OKX 的普通 conditional algo 同时给 TP+SL 时只执行 SL 逻辑；因此 executor
    # 不把 fixed TP 混进主单参数。SL 先成为硬保护，TP 随后以独立 reduceOnly
    # conditional 单挂出并按精确 algoId 回读。
    # set_leverage 是独立写请求；它返回后若刚好跨过硬截止，仍不得再开仓。
    if not ox.is_dryrun():
        deadline_reject = _cycle_side_effect_reject(cycle_id)
        if deadline_reject:
            return _finish_clean(
                receipt(False, risk=v, **deadline_reject),
                str(deadline_reject["reject_reason"]),
            )
    # The first fingerprint check deliberately precedes set_leverage so a
    # stale OPEN/ADD plan cannot mutate exchange state.  Specs, sizing, risk
    # checks, and set_leverage can still take long enough for the position to
    # change underneath us, so bind the order itself to one final API read.
    # Unified-runner calls always provide ``expected_*``; legacy internal
    # callers without an expected fingerprint keep their existing I/O shape.
    if (not ox.is_dryrun()
            and expected_pre_position_exists is not None):
        try:
            latest_positions = fetch_open_positions(profile)
        except PositionsUnavailable as exc:
            return _finish_clean(receipt(
                False,
                action_taken="REJECT",
                reject_reason="pre_order_positions_unavailable",
                reject_detail=(
                    "订单紧前现仓 API 不可用，无法确认 OPEN/ADD 语义；"
                    f"已 fail-closed，未下单: {exc}"
                ),
                risk=v,
            ), "pre_order_positions_unavailable")
        latest_pre_position = next((
            row for row in latest_positions
            if row.get("symbol") == symbol
            and str(row.get("side") or "").lower() == side
        ), None)
        late_fingerprint_error = _position_fingerprint_error(
            latest_pre_position,
            expected_exists=expected_pre_position_exists,
            expected_sz=expected_pre_position_sz,
            expected_pos_id=expected_pre_position_pos_id,
            expected_c_time=expected_pre_position_c_time,
        )
        if late_fingerprint_error:
            return _finish_clean(receipt(
                False,
                action_taken="REJECT",
                reject_reason="pre_position_semantics_changed",
                reject_detail=(
                    "订单紧前同 symbol/side 仓位已不同于 plan/facts；"
                    "已 clean reject，未下单: " + late_fingerprint_error
                ),
                expected_pre_position={
                    "exists": expected_pre_position_exists,
                    "sz": expected_pre_position_sz,
                    "posId": expected_pre_position_pos_id,
                    "cTime": expected_pre_position_c_time,
                },
                actual_pre_position=latest_pre_position,
                risk=v,
            ), "pre_position_semantics_changed")
    pre_sz = _position_size(open_positions, symbol, side)
    pre_place_ms = int(time.time() * 1000)
    if intent_fingerprint:
        try:
            ei.mark_submitting(intent_path, error=None, **_intent_kwargs())
        except Exception as exc:
            return receipt(
                False, action_taken="REJECT",
                reject_reason="execution_intent_transition_failed",
                reject_detail=(
                    "订单前幂等状态无法固化，已 fail-closed，未触发订单: "
                    f"{type(exc).__name__}: {exc}"), p0=True)
    try:
        pr = ox.place_market_open(
            symbol, side, approved_sz, profile, mgn_mode=mgn_mode,
            sl_trigger_px=sl_trigger_px, tp_trigger_px=None)
    except Exception as exc:
        _enqueue_repair(profile_label, symbol, None,
                        "place_exception_ambiguous", db_root)
        return _finish_uncertain(
            receipt(False, action_taken="REJECT",
                    reject_reason="place_exception_ambiguous",
                    reject_detail=(
                        "下单调用异常，是否到达交易所未知；已阻断重试并入 repair_queue: "
                        f"{type(exc).__name__}: {exc}"),
                    risk=v),
            "place_exception_ambiguous")
    recovered_timeout = False
    if not pr.get("ok"):
        sc = pr.get("sCode")
        # S2b（2026-07-02）：写超时/连接歧义（有 error 无业务 sCode）≠ 干净未成交。
        # 命令可能已达交易所且订单已成交 —— 回读现仓判定真实状态，禁静默 REJECT 造幽灵仓。
        ambiguous = (not sc) and bool(pr.get("error"))
        if ambiguous and not ox.is_dryrun():
            settled = _verify_open_settled(symbol, side, profile, pre_sz)
            if settled is None:
                _enqueue_repair(profile, symbol, None,
                                "place_ambiguous_unverifiable", db_root)
                return _finish_uncertain(receipt(False, action_taken="REJECT",
                               reject_reason="place_ambiguous",
                               reject_detail="下单写超时且现仓回读不可判定，已写 repair_queue 待人工核对",
                               risk=v, p0=True), "place_ambiguous_unverifiable")
            if not settled:
                return _finish_clean(
                    receipt(False, action_taken="REJECT", reject_reason="place_failed",
                            reject_detail="下单写超时，现仓回读确认未成交", risk=v),
                    "place_timeout_confirmed_no_fill")
            recovered_timeout = True  # 实际成交 → 落正常流程（无 ordId，靠时间窗回读 fills）
        else:
            if sc == ox.SCODE_DELISTED:
                reason = "delisted"
            elif sc == ox.SCODE_NOT_EXIST:
                reason = "instrument_not_exist"
            else:
                reason = "place_failed"
            return _finish_clean(receipt(False, action_taken="REJECT", reject_reason=reason,
                           reject_detail=f"sCode={sc} {pr.get('sMsg') or pr.get('error')}",
                           risk=v), f"place_rejected:{reason}")

    ord_id = None
    for row in pr.get("data", []):
        if isinstance(row, dict) and row.get("ordId"):
            ord_id = row["ordId"]
            break
    intent_ord_id = str(ord_id) if ord_id not in (None, "") else None
    if intent_fingerprint:
        try:
            ei.mark_submitted(
                intent_path, ord_id=intent_ord_id, error=None,
                **_intent_kwargs())
        except Exception as exc:
            # submitting 已固化，重跑仍会被拦；继续保护 SL/回读/journal，
            # 最终 completed 再尝试补齐状态。
            print(
                f"[order_executor] WARN execution intent submitted 写失败 "
                f"{symbol}: {exc}", file=sys.stderr)

    # fill 求真可能在正常返回或 SL 失败 unwind 分支被需要，进程内只做一次并
    # memo，避免重试窗口与 journal 重复。正常路径保护完成后再求真；SL 全失败
    # 路径在 unwind 前先求真，并将 open/close 都标为 unwind 人工审计对。
    fill_resolution: Optional[tuple[dict[str, Any], str]] = None
    open_fill_journaled = False

    def resolve_open_fill() -> tuple[dict[str, Any], str]:
        nonlocal fill_resolution
        if fill_resolution is not None:
            return fill_resolution
        try:
            fa0 = _read_fills(symbol, profile, ord_id,
                              since_ms=None if ord_id else pre_place_ms)
        except Exception as exc:
            print(f"[order_executor] WARN open fills 回读异常 {symbol}: {exc}",
                  file=sys.stderr)
            fa0 = {"ok": False, "fill_px": None, "fill_sz": None,
                   "pnl": None, "n": 0}
        source0 = "fills"
        contract_errors: list[str] = []
        initial_valid, initial_error = _validate_confirmed_open_fill(
            fa0, source0, dryrun=ox.is_dryrun(), approved_sz=approved_sz)
        if fa0.get("ok") and not initial_valid:
            contract_errors.append(f"{source0}:{initial_error}")
            fa0 = dict(fa0, ok=False,
                       fill_validation_error=initial_error)
        if not fa0.get("ok") and not ox.is_dryrun():
            try:
                alt = (_confirm_order_filled(symbol, profile, ord_id) if ord_id
                       else _find_orders_since(symbol, profile, side, pre_place_ms,
                                               reduce_only=False))
            except Exception as exc:
                print(f"[order_executor] WARN open order 回读异常 {symbol}: {exc}",
                      file=sys.stderr)
                alt = None
            if alt and alt.get("ok"):
                fa0, source0 = alt, str(alt.get("source") or "order_status")
                alt_valid, alt_error = _validate_confirmed_open_fill(
                    fa0, source0, approved_sz=approved_sz)
                if not alt_valid:
                    contract_errors.append(f"{source0}:{alt_error}")
                    fa0 = dict(fa0, ok=False,
                               fill_validation_error=alt_error)
            elif alt and alt.get("state") == "canceled":
                contract_errors.clear()
                fa0 = {"ok": False, "state": "canceled", "fill_px": None,
                       "fill_sz": 0.0, "pnl": 0.0, "n": 0}
        if (not fa0.get("ok") and recovered_timeout
                and fa0.get("approx_agg")):
            # 现仓/历史聚合只证明“需要人工修复”，不得合成 confirmed OPEN。
            _enqueue_repair(profile, symbol, ord_id,
                            "position_verified_fills_missing:"
                            "approx_agg_repair_evidence", db_root)
        if not fa0.get("ok") and contract_errors:
            _enqueue_repair(
                profile, symbol, ord_id,
                "open_fill_contract_invalid:" + ",".join(contract_errors),
                db_root)
        fill_resolution = (fa0, source0)
        return fill_resolution

    def make_open_trade(fa0: dict[str, Any], fill_source0: str,
                        sl_mode0: str, sl_verified0: bool,
                        algo_id0: Optional[str], tp_mode0: str,
                        tp_verified0: bool) -> dict[str, Any]:
        accounting = _open_fill_accounting(
            fa0, approved_sz=approved_sz, mark_px=mark_px,
            ct_val=ct_val, effective_lev=effective_lev,
            dryrun=ox.is_dryrun())
        # 2026-08-08 单笔保证金闸：按真实成交保证金复审（执行时 equity 口径）。
        # 异常滑点突破 15% 硬边界 → 如实记账 + 告警 + repair_queue 人工出口，
        # 不阻断不追溯——后续每笔 OPEN/ADD 本就重过 validator 预检（14.7% 定仓
        # 预算），不设新封锁状态（主人拍板口径）。
        so_ratio, so_breached = _single_order_fill_audit(
            accounting.get("margin"), equity)
        if so_breached:
            print(
                f"[order_executor] WARN 单笔成交保证金 {so_ratio:.1%} 超硬边界 "
                f"{rv.MAX_SINGLE_ORDER_IMR_RATIO:.0%}（滑点/口径漂移），"
                f"如实记账仅告警 sym={symbol}", file=sys.stderr)
            _enqueue_repair(profile, symbol, ord_id,
                            f"single_order_cap_breached:{so_ratio:.4f}", db_root)
        # 2026-08-10 Wave1 序7 止损风险闸：按真实成交价复审止损风险（与保证金
        # 复审同款口径：滑点突破 5% 硬边界 → 如实记账 + 告警 + repair_queue，
        # 不阻断不追溯；validator 预检已按 mark 缩量）。
        so_risk_usdt = None
        so_risk_pct = None
        so_risk_breached = False
        _fill_px_a = accounting.get("fill_px")
        _notional_a = accounting.get("notional")
        if (sl_trigger_px is not None and _fill_px_a and _notional_a
                and equity):
            _dist = abs(float(_fill_px_a) - float(sl_trigger_px)) / float(
                _fill_px_a)
            so_risk_usdt = float(_notional_a) * (
                _dist + rv.RISK_FEE_BUFFER_PCT + rv.RISK_SLIPPAGE_BUFFER_PCT)
            so_risk_pct = so_risk_usdt / float(equity)
            so_risk_breached = (
                so_risk_pct > rv.MAX_SINGLE_ORDER_RISK_PCT_EQUITY + 1e-9)
        if so_risk_breached:
            print(
                f"[order_executor] WARN 单笔成交止损风险 {so_risk_pct:.1%} "
                f"超硬边界 {rv.MAX_SINGLE_ORDER_RISK_PCT_EQUITY:.0%}"
                f"（滑点/口径漂移），如实记账仅告警 sym={symbol}",
                file=sys.stderr)
            _enqueue_repair(
                profile, symbol, ord_id,
                f"single_order_risk_cap_breached:{so_risk_pct:.4f}", db_root)
        return {
            "symbol": symbol, "action": "open", "side": side,
            "sz": accounting["sz"],
            "fill_sz": accounting["sz"],
            "approved_sz": accounting["approved_sz"],
            "partial_fill": accounting["partial_fill"],
            "fill_ratio": accounting["fill_ratio"],
            "fill_px": accounting["fill_px"], "px": accounting["fill_px"],
            "lev": effective_lev, "margin": accounting["margin"],
            "notional": accounting["notional"], "pnl": 0.0,
            "channel": "live",
            "reason": reasoning, "open_id": ord_id, "sl_trigger_px": sl_trigger_px,
            "algo_id": algo_id0, "sl_mode": sl_mode0,
            "sl_verified": sl_verified0, "fill_source": fill_source0,
            "tp_trigger_px": tp_trigger_px, "tp_mode": tp_mode0,
            "tp_algo_id": tp_algo_id,
            "exit_mode": exit_mode or ("fixed_tp" if tp_trigger_px is not None
                                        else "legacy_optional_tp"),
            "tp_verified": tp_verified0,
            "fill_ts": fa0.get("fill_ts"),
            "ts_source": fa0.get("ts_source"),
            # 回执带本环境真实 ct_val，writer 补算优先用行内值。
            "ct_val": ct_val, "ordId": ord_id,
            # 单笔保证金审计（push/复盘直读；HOLD/WAIT/ADJUST_PROTECTION
            # 无 open/reduce/close 成交行，天然无此字段）
            "single_order_imr_ratio": so_ratio,
            "max_single_order_imr_ratio": rv.MAX_SINGLE_ORDER_IMR_RATIO,
            "single_order_cap_breached": so_breached,
            # 止损风险审计（Wave1 序7；口径 = 名义×(成交价距SL+0.2%缓冲)÷equity）
            "single_order_risk_usdt": so_risk_usdt,
            "single_order_risk_pct_equity": so_risk_pct,
            "max_single_order_risk_pct_equity": (
                rv.MAX_SINGLE_ORDER_RISK_PCT_EQUITY),
            "single_order_risk_cap_breached": so_risk_breached,
            "risk_clamped": bool(v.get("clamped")),
            "risk_adjustments": list(v.get("adjustments") or []),
        }

    def journal_open_once(trade0: dict[str, Any], *, unwind: bool = False,
                          journal_action: Optional[str] = None) -> None:
        nonlocal open_fill_journaled
        if open_fill_journaled:
            return
        _journal_fill("live", trade0, db_root, cycle_id,
                      journal_action or action_taken, unwind=unwind)
        open_fill_journaled = True

    # ── 止损保障（sl_mode 如实标注）──
    #   attached：随主单附挂，`sl_attached` 只表示带参下单成功；必须回读确认真挂上；
    #   algo：独立 reduceOnly algo，返回 algoId 后仍须 pending 回读通过才算 verified。
    #   超时恢复路径 attached 状态未知 → 不采信附挂，强制走独立 algo 补挂（belt）。
    algo_id = None
    tp_algo_id = None
    tp_mode = "none"
    tp_verified = False
    tp_warning = None
    attached_ok = bool(pr.get("sl_attached")) and not recovered_timeout
    # 附挂 SL 用 get_algo_orders 回读；验证后才置 sl_verified=True。
    # 回读不到 → attached_ok=False，落回下方独立 algo SL 兜底防裸仓。
    # dryrun 跳过（无真单可查）。
    sl_verified = False
    if attached_ok and sl_trigger_px is not None and not ox.is_dryrun():
        try:
            _vsl = _verify_sl_placed(
                symbol, side, profile, sl_trigger_px,
                expected_sz=approved_sz, since_ms=pre_place_ms,
                expected_ord_id=ord_id)
        except Exception as exc:
            # 回读是证明层，异常不得穿透跳过 belt SL/journal/unwind。
            _vsl = {"verified": False, "found": [],
                    "error": f"verify_exception:{type(exc).__name__}"}
            print(f"[order_executor] WARN 附挂 SL 回读异常 {symbol}: {exc}",
                  file=sys.stderr)
        if _vsl.get("verified"):
            sl_verified = True
        else:
            attached_ok = False
            print(f"[order_executor] WARN 附挂 SL 回读未确认 {symbol}（走独立 algo SL belt）",
                  file=sys.stderr)
    sl_mode = "attached" if attached_ok else "none"
    sl_secured = attached_ok or sl_trigger_px is None
    if sl_trigger_px is not None and not sl_secured:
        for _ in range(2):  # 首次 + 重试 1
            algo_place_ms = int(time.time() * 1000)
            try:
                ar = ox.place_algo_sl(symbol, side, approved_sz, sl_trigger_px,
                                      profile, mgn_mode=mgn_mode)
            except Exception as exc:
                # CLI/网络/回包解析异常等价于「未挂上」，继续重试；
                # 两次均失败则进 fail-safe unwind，禁让异常直接杀进程。
                print(f"[order_executor] WARN 独立 SL 下单异常 {symbol}: {exc}",
                      file=sys.stderr)
                ar = {"ok": False, "error": str(exc)}
            if ar.get("ok"):
                candidate_algo_id = None
                for row in ar.get("data", []):
                    if isinstance(row, dict) and row.get("algoId"):
                        candidate_algo_id = str(row["algoId"])
                        break
                try:
                    _vsl = _verify_sl_placed(
                        symbol, side, profile, sl_trigger_px,
                        expected_sz=approved_sz, since_ms=algo_place_ms,
                        expected_algo_id=candidate_algo_id)
                except Exception as exc:
                    _vsl = {
                        "verified": False, "found": [],
                        "error": f"verify_exception:{type(exc).__name__}",
                    }
                    print(
                        f"[order_executor] WARN 独立 SL 回读异常 {symbol}: {exc}",
                        file=sys.stderr)
                if _vsl.get("verified"):
                    sl_secured = True
                    sl_mode, sl_verified = "algo", True
                    algo_id = candidate_algo_id
                    break
                print(
                    f"[order_executor] WARN 独立 SL 已受理但回读未确认 {symbol} "
                    f"algoId={candidate_algo_id}",
                    file=sys.stderr)
        if not sl_secured:
            # S2e：进入 UNWIND 即先落 repair_queue。为了既不丢已成交 open，
            # 又不让 demo 自动重放单独补 open 造成幽灵仓，open 与后续 close 都带
            # unwind=true，统一进 P1 人工成对审计。
            _enqueue_repair(profile, symbol, ord_id, "sl_failed_unwinding", db_root)
            fa_unwind, source_unwind = resolve_open_fill()
            if fa_unwind.get("ok"):
                trade_unwind_open = make_open_trade(
                    fa_unwind, source_unwind, "none", False, None,
                    "none", False)
                journal_open_once(trade_unwind_open, unwind=True,
                                  journal_action="UNWIND_OPEN")
            elif fa_unwind.get("state") != "canceled":
                # 现仓增量只写 repair 证据，禁止合成 confirmed OPEN。
                try:
                    post_sz = _position_size(fetch_open_positions(profile), symbol, side)
                except Exception:
                    post_sz = None
                if post_sz is not None and post_sz > pre_sz + _EPS:
                    _enqueue_repair(
                        profile, symbol, ord_id,
                        "open_position_delta_repair_evidence:"
                        f"{post_sz - pre_sz}", db_root)
            unwind = close_position(symbol, profile, pos_side=side,
                                    mgn_mode=mgn_mode, db_root=db_root,
                                    reasoning="unwind: SL 挂单失败，平掉裸仓",
                                    cycle_id=cycle_id, _unwind=True,
                                    receipt_context=receipt_context)
            if (not open_fill_journaled and unwind.get("ok")
                    and unwind.get("trades")):
                # unwind 确认曾有仓位，但不能替代 OPEN 成交端点；只进入 repair。
                close_trade = unwind["trades"][0]
                _enqueue_repair(
                    profile, symbol, ord_id,
                    "open_unwind_repair_evidence:"
                    f"close_sz={close_trade.get('sz')}", db_root)
            if not unwind.get("ok"):
                _enqueue_repair(profile, symbol, ord_id,
                                "naked_position_unwind_failed", db_root)
                return _finish_uncertain(receipt(False, action_taken="UNWIND",
                               reject_reason="naked_position_unwind_failed",
                               reject_detail="SL 全失败且平裸仓也失败 → 无止损裸仓，已双写 repair_queue",
                               risk=v, unwind=unwind, p0=True),
                                         "naked_position_unwind_failed")
            return _finish_completed(receipt(False, action_taken="UNWIND",
                           reject_reason="sl_failed_unwound",
                           reject_detail="附挂+独立 SL 均失败，已市价平掉裸仓",
                           risk=v, unwind=unwind, p0=True))

    # ── 回读真实成交（ord_id 缺失=超时恢复路径 → 用下单时刻做时间窗，防历史成交混入）──
    fa, fill_source = resolve_open_fill()
    if fa.get("state") == "canceled":
        # 交易所级确认 0 成交（canceled+accFillSz=0）→ 干净拒单（非 p0）。
        # 独立 algo SL 已挂的话此刻成悬挂单（无仓时 reduceOnly 无害）→ 记 repair 提示撤单。
        if algo_id:
            _enqueue_repair(profile, symbol, ord_id,
                            "open_canceled_dangling_algo_sl", db_root)
        if tp_algo_id:
            _enqueue_repair(profile, symbol, ord_id,
                            "open_canceled_dangling_algo_tp", db_root)
        return _finish_clean(receipt(False, action_taken="REJECT",
                       reject_reason="open_not_filled",
                       reject_detail="订单状态确认未成交（canceled, accFillSz=0）",
                       risk=v, ord_id=ord_id), "open_canceled_no_fill")
    if not fa.get("ok"):
        # 两端点都确认不了 → 原 fail-safe：repair_queue + reject + P0
        _enqueue_repair(profile, symbol, ord_id, "open_fills_missing", db_root)
        return _finish_uncertain(receipt(False, action_taken="REJECT",
                       reject_reason="fills_missing",
                       reject_detail="开仓后 fills/订单状态均拉不到，已写 repair_queue",
                       risk=v, ord_id=ord_id, p0=True), "open_fills_missing")

    # 从这里开始已是权威确认的成交，立即落一次 journal。固定 TP 是非关键腿，
    # 必须在这条成交真值留痕之后才尝试，且只按实际 fill_sz 挂，不能拿 approved_sz
    # 覆盖部分成交后的真实仓位。
    trade = make_open_trade(
        fa, fill_source, sl_mode, sl_verified, algo_id, tp_mode, tp_verified)
    journal_open_once(trade)

    # ── 可选止盈：独立 reduceOnly conditional 单 ──
    # TP 缺失不等于裸仓：SL 已安全确认时不平仓，只把未兑现的止盈保护写 repair。
    tp_size = _to_float(fa.get("fill_sz"))
    if tp_size is None and ox.is_dryrun():
        tp_size = approved_sz
    if tp_trigger_px is not None and ox.is_dryrun():
        # DRYRUN 只证明 fixed-TP 分支进入独立保护计划，不伪称交易所已回读确认，
        # 也绝不能向生产 repair_queue 写一条虚假的 TP 缺失告警。
        tp_mode, tp_verified = "dryrun_simulated", False
    elif tp_trigger_px is not None and tp_size is not None and tp_size > 0:
        tp_place_ms = int(time.time() * 1000)
        try:
            tp_place = ox.place_algo_tp(
                symbol, side, tp_size, tp_trigger_px, profile,
                mgn_mode=mgn_mode)
        except Exception as exc:
            tp_place = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "data": [],
            }
        if tp_place.get("ok"):
            tp_algo_id = next((
                str(row.get("algoId"))
                for row in tp_place.get("data", [])
                if isinstance(row, dict) and row.get("algoId")
            ), None)
            if not tp_algo_id:
                _vtp = {
                    "verified": False, "found": [],
                    "error": "tp_algo_id_missing",
                }
            else:
                try:
                    _vtp = _verify_tp_placed(
                        symbol, side, profile, tp_trigger_px,
                        expected_sz=tp_size, since_ms=tp_place_ms,
                        expected_algo_id=tp_algo_id)
                except Exception as exc:
                    _vtp = {
                        "verified": False, "found": [],
                        "error": f"verify_exception:{type(exc).__name__}",
                    }
        else:
            _vtp = {
                "verified": False, "found": [],
                "error": str(tp_place.get("sMsg") or tp_place.get("error")),
            }
        if _vtp.get("verified"):
            tp_mode, tp_verified = "independent_algo", True
        else:
            tp_warning = "tp_unsecured"
    elif tp_trigger_px is not None:
        tp_warning = "tp_unsecured"
    if tp_warning:
        _enqueue_repair(profile, symbol, ord_id, "tp_unsecured_after_open", db_root)
        print(
            f"[order_executor] WARN 可选 TP 未回读确认 {symbol}；SL 已保护，"
            "不 unwind，已入 repair_queue",
            file=sys.stderr,
        )
    trade["tp_mode"] = tp_mode
    trade["tp_verified"] = tp_verified
    trade["tp_algo_id"] = tp_algo_id

    # ── 加仓后收敛并扩到全仓（主人 2026-08-13 授权「加仓后自动扩到全仓」）────────
    # 加仓**必然**留下第二张分档止损：本函数每笔成交都自挂 approved_sz 大小的
    # reduceOnly SL，而 _verify_sl_placed 要求 cTime >= 本次请求时刻，命中的必是新单。
    # 不收敛就长期停在「两张分档单」——总覆盖达全仓（非裸仓），但不满足终局唯一契约，
    # 且此后任何**不带** consolidate_extra_sl 的改单都会被 duplicate_sl_before_change 硬拒。
    # 与 reduce_position 的 post_reduce_resize 完全对称：失败只外显 + 入 repair，
    # **绝不反向抹掉已确认的成交**，也绝不为了「改单成功」去撤止损制造裸仓。
    # 阶段3 真单验证见 docs/protection_amend_stage3_acceptance_20260813.md。
    protection_sync: Optional[dict[str, Any]] = None
    protection_p0 = False
    is_add = pre_position_sz > _EPS
    if is_add and sl_trigger_px is not None and ox.is_dryrun():
        protection_sync = {"ok": True, "dryrun": True,
                           "planned_full_sz": pre_position_sz + approved_sz}
    elif is_add and sl_trigger_px is not None:
        sync = adjust_protection(
            symbol, profile, pos_side=side, resize_to_full_position=True,
            consolidate_extra_sl=True,
            reasoning=reasoning or "加仓后收敛并同步全仓止损数量",
            db_root=db_root, cycle_id=cycle_id,
            receipt_context=receipt_context,
            reason_code="post_add_resize",
        )
        protection_sync = {
            key: sync.get(key) for key in (
                "ok", "action_taken", "reject_reason", "reject_detail",
                "p0", "path", "protection_state", "applied", "consolidated_from")
            if sync.get(key) is not None
        }
        if not sync.get("ok"):
            _enqueue_repair(
                profile, symbol, ord_id,
                f"add_protection_sync_failed:{sync.get('reject_reason')}", db_root)
            protection_p0 = bool(
                sync.get("p0")
                or sync.get("reject_reason") in {
                    "no_sl_to_preserve", "naked_after_change"})
            print(f"[order_executor] WARN 加仓后扩全仓止损失败 {symbol}: "
                  f"{sync.get('reject_reason')}；成交已确认不回滚，已入 repair_queue",
                  file=sys.stderr)

    return _finish_completed(receipt(True, trades=[trade], risk=v, ord_id=ord_id,
                   is_add=is_add, pre_position_sz=pre_position_sz,
                   authoritative_target_sizing=authoritative_target_sizing,
                   protection_sync=protection_sync, p0=protection_p0,
                   clamped=v.get("clamped"), adjustments=v.get("adjustments"),
                   lev_warn=lev_warn,
                   sl_mode=sl_mode, sl_verified=sl_verified,
                   tp_mode=tp_mode, tp_verified=tp_verified,
                   tp_algo_id=tp_algo_id,
                   tp_warning=tp_warning,
                   recovered_timeout=recovered_timeout,
                   fill_source=fill_source,
                   spec_source=specs.get("spec_source"),
                   input_divergence=input_divergence or None))


# ---------------------------------------------------------------------------
# CLOSE（健壮平仓）
# ---------------------------------------------------------------------------
def close_position(
    symbol: str,
    profile: str,
    pos_side: Optional[str] = None,
    mgn_mode: str = "cross",
    reasoning: str = "",
    db_root: Path = DEFAULT_DB_ROOT,
    cycle_id: Optional[str] = None,
    _unwind: bool = False,
    receipt_context: Optional[dict[str, Any]] = None,
    expected_pre_position_exists: Optional[bool] = None,
    expected_pre_position_sz: Optional[float] = None,
    expected_pre_position_pos_id: Optional[str] = None,
    expected_pre_position_c_time: Optional[str] = None,
) -> dict[str, Any]:
    _require_live_profile(profile, "close_position")
    resolved_side = pos_side

    def receipt(ok: bool, **kw) -> dict[str, Any]:
        # 与 OPEN 相同：执行前完整验证，执行后只原样携带决策上下文，
        # 禁止 Agent 在成交后再手工拼 status/protocol/card/cycle。
        base = dict(receipt_context or {})
        base.update({"profile": "live", "ok": ok,
                "cycle_id": cycle_id,
                "action_taken": kw.pop("action_taken", "CLOSE"),
                "symbol": symbol, "trades": kw.pop("trades", []),
                "p0": kw.pop("p0", False)})
        if resolved_side:
            base["side"] = resolved_side
        base.update(kw)
        return base

    # 非 dry-run 与 OPEN 共用同一个完整回执上下文校验器，且必须发生在
    # fetch_open_positions / 下单等任何 OKX I/O 之前。
    ctx_errors = validate_receipt_context(
        receipt_context, cycle_id=cycle_id, required=not ox.is_dryrun())
    if ctx_errors:
        return receipt(
            False, action_taken="REJECT",
            reject_reason="receipt_context_invalid",
            reject_detail="；".join(ctx_errors))
    if not ox.is_dryrun() and not cycle_id:
        return receipt(
            False, action_taken="REJECT", reject_reason="cycle_id_required",
            reject_detail="非 dry-run 平仓必须提供调度 cycle_id")
    if not ox.is_dryrun() and not _unwind:
        deadline_reject = _cycle_side_effect_reject(cycle_id)
        if deadline_reject:
            return receipt(False, **deadline_reject)

    # ── OKX API 现仓确认 posSide（S2a：API 失败 → 拒，禁当"无仓已平"假成功）──
    try:
        positions = fetch_open_positions(profile)
    except PositionsUnavailable as exc:
        return receipt(False, reject_reason="positions_fetch_failed",
                       reject_detail=f"现仓 API 失败，无法确认平仓目标: {exc}", p0=True)
    match = None
    for p in positions:
        if p["symbol"] == symbol and (pos_side is None or p["side"] == pos_side):
            match = p
            break
    fingerprint_error = _position_fingerprint_error(
        match,
        expected_exists=expected_pre_position_exists,
        expected_sz=expected_pre_position_sz,
        expected_pos_id=expected_pre_position_pos_id,
        expected_c_time=expected_pre_position_c_time,
    )
    if fingerprint_error:
        return receipt(
            False,
            action_taken="REJECT",
            reject_reason="pre_position_fingerprint_changed",
            reject_detail=(
                "执行时平仓目标已不同于 plan/facts；未发送订单: "
                + fingerprint_error
            ),
            expected_pre_position={
                "exists": expected_pre_position_exists,
                "sz": expected_pre_position_sz,
                "posId": expected_pre_position_pos_id,
                "cTime": expected_pre_position_c_time,
            },
            actual_pre_position=match,
        )
    if match is None:
        return receipt(True, action_taken="CLOSE", note="no_open_position",
                       reject_detail=f"{symbol} 无对应现仓（可能已平）")
    side = match["side"]
    resolved_side = side
    pos_sz = _to_float(match.get("sz"))
    if pos_sz is None or not math.isfinite(pos_sz) or pos_sz <= 0:
        return receipt(
            False, action_taken="REJECT",
            reject_reason="invalid_position_size",
            reject_detail=f"OKX 现仓数量非法，拒绝发送平仓单: {match.get('sz')!r}",
            p0=True)

    # ── 平仓下单（2026-07-03 主路径反转：reduceOnly 市价单优先，swap close 降兜底）──
    # swap close（close-position 端点）不返回 ordId，而 demo 的 fills / orders-history
    # 列表端点索引延迟达分钟级（实测平仓 5min 后列表仍不见单）→ 无 ordId 即无法即时
    # 确认成交/pnl（旧「全量聚合 approx」被历史成交污染 fill_px 的根因）。交易所侧
    # swap close 本就生成一张 reduceOnly 市价单——改为显式下 reduceOnly 单拿到 ordId
    # （per-order GET 实测即时 134ms）；reduceOnly 语义保证绝不翻反向仓。
    if not ox.is_dryrun() and not _unwind:
        deadline_reject = _cycle_side_effect_reject(cycle_id)
        if deadline_reject:
            return receipt(False, **deadline_reject)
    close_start_ms = int(time.time() * 1000)
    used_reduce_only = True
    reduce_ord_id = None
    rr = ox.place_reduce_only_market(symbol, side, pos_sz, profile,
                                     mgn_mode=mgn_mode)
    if rr.get("ok"):
        for row in rr.get("data", []):
            if isinstance(row, dict) and row.get("ordId"):
                reduce_ord_id = row["ordId"]
                break
    else:
        used_reduce_only = False
        sc = rr.get("sCode")
        if (not sc) and rr.get("error") and not ox.is_dryrun():
            # S2b（close 侧）：写超时（error 无业务 sCode）→ 现仓回读判定是否已平，禁误报 close_failed。
            time.sleep(2.0)
            try:
                still = _position_size(fetch_open_positions(profile), symbol, side)
            except PositionsUnavailable:
                still = None
            if still is not None and still <= _EPS:
                pass  # 现仓已消失 → 平仓实际成交，落确认流程（无 ordId）
            else:
                cr = ox.close_position_cli(symbol, mgn_mode, side, profile)
                if not cr.get("ok"):
                    _enqueue_repair(profile, symbol, None, "close_ambiguous_unclosed", db_root)
                    return receipt(False, reject_reason="close_failed",
                                   reject_detail="reduceOnly 写超时且现仓仍在、swap close 兜底失败，已写 repair_queue",
                                   p0=True)
        elif sc == ox.SCODE_DELISTED:
            return receipt(False, reject_reason="delisted",
                           reject_detail=f"{symbol} 已下架，close 失败 sCode={sc}")
        elif sc == ox.SCODE_NOT_EXIST:
            return receipt(False, reject_reason="instrument_not_exist",
                           reject_detail=f"{symbol} 不存在 sCode={sc}")
        else:
            # reduceOnly 被拒（精度/并发减仓等）→ swap close（server-side 全平）兜底
            cr = ox.close_position_cli(symbol, mgn_mode, side, profile)
            if not cr.get("ok"):
                sc2 = cr.get("sCode")
                return receipt(False, reject_reason="close_failed",
                               reject_detail=f"reduceOnly sCode={sc} 且 swap close 兜底失败 "
                                             f"sCode={sc2} {cr.get('sMsg') or cr.get('error')}",
                               p0=True)

    # ── 残留核实（并发减仓/SL 触发等窄窗；reduceOnly 不会翻仓，残留=没平干净）──
    if not ox.is_dryrun():
        time.sleep(2.0)  # 市价成交后仓位快照更新需 1-2s，立查会误报残留
        try:
            residue = _position_size(fetch_open_positions(profile), symbol, side)
        except PositionsUnavailable:
            residue = None
        if residue is not None and residue > _EPS:
            cr2 = ox.close_position_cli(symbol, mgn_mode, side, profile)  # 全平兜底
            try:
                residue = _position_size(fetch_open_positions(profile), symbol, side)
            except PositionsUnavailable:
                residue = None
            if residue is None or residue > _EPS:
                _enqueue_repair(profile, symbol, reduce_ord_id,
                                "close_residual_position", db_root)
                return receipt(False, reject_reason="close_incomplete",
                               reject_detail=f"平仓后仍有残留仓 sz={residue}，全平兜底后未归零，已写 repair_queue",
                               p0=True)

    # ── 回读真实成交求 pnl（S2c：reduceOnly 有 ordId 精确过滤；swap close 无 ordId 用时间窗）──
    fa = _read_fills(symbol, profile, reduce_ord_id,
                     since_ms=None if reduce_ord_id else close_start_ms)
    fill_source = "fills"
    pnl_approx = False
    if not fa.get("ok") and not ox.is_dryrun():
        # 2026-07-03：第二权威源——reduceOnly 有 ordId 查订单状态；swap close 无 ordId
        # 从 orders-history 按时间窗反查平仓单（实测其 avgPx/pnl 完整且即时）。
        alt = (_confirm_order_filled(symbol, profile, reduce_ord_id)
               if reduce_ord_id
               else _find_orders_since(symbol, profile, side, close_start_ms,
                                       reduce_only=True))
        if alt and alt.get("ok"):
            fa, fill_source = alt, alt["source"]
        elif alt and alt.get("state") == "canceled":
            # 平仓单被撤且 0 成交=手里有「没平掉」的交易所证据 → 重拉现仓核实；
            # 仓仍在/不可判定 → 不得报平仓成功（禁 fail-open）。
            try:
                still = _position_size(fetch_open_positions(profile), symbol, side)
            except PositionsUnavailable:
                still = None
            if still is None or still > _EPS:
                _enqueue_repair(profile, symbol, reduce_ord_id,
                                "close_canceled_position_remains", db_root)
                return receipt(False, reject_reason="close_not_filled",
                               reject_detail="平仓单 canceled 0 成交且现仓仍在/不可判定，已写 repair_queue",
                               p0=True)
            # 现仓已消失（可能被 SL/他单平掉）→ 仓位状态 OK，pnl 未知，继续走下方兜底
    if not fa.get("ok") and not ox.is_dryrun():
        # 2026-07-03：废除 close 侧 approx 全量聚合兜底——demo 列表端点分钟级延迟下
        # 聚合必混历史成交（实测 fill_px 63483 vs 真 61876、pnl 4.63 全是垃圾数字）。
        # 如实记 unconfirmed：pnl=None（trades.pnl NULL → cum_pnl 跳过、经验库 outcome
        # 置空）+ repair 供事后对账回填（列表端点最终会索引到本单）。
        _enqueue_repair(profile, symbol, reduce_ord_id, "close_pnl_unconfirmed",
                        db_root)
        fill_source = "unconfirmed"
        pnl_approx = True  # 沿用标记语义：该 pnl/fill_px 不可信（此处为 None）
        print(f"[order_executor] WARN close 成交确认两端点均未见,pnl 记 unconfirmed "
              f"sym={symbol} ordId={reduce_ord_id}", file=sys.stderr)

    # 位置归零不等于本订单成交事实完整。若权威端点缺数量/均价/成交时间，
    # 降级为 unconfirmed 并等待对账，绝不拿请求前仓位 pos_sz 合成 fill_sz。
    fill_contract_error = None
    if fa.get("ok"):
        confirmed_valid, fill_contract_error = _validate_confirmed_close_fill(
            fa,
            fill_source,
            dryrun=ox.is_dryrun(),
            requested_sz=pos_sz,
        )
        if not confirmed_valid and not ox.is_dryrun():
            _enqueue_repair(
                profile, symbol, reduce_ord_id,
                f"close_fill_contract_invalid:{fill_contract_error}", db_root)
            fill_source = "unconfirmed"
            pnl_approx = True
            fa = {
                "ok": False,
                "fill_px": None,
                "fill_sz": None,
                "pnl": None,
                "fill_ts": None,
                "ts_source": None,
            }

    confirmed_fill = bool(fa.get("ok"))
    actual_fill_sz = _to_float(fa.get("fill_sz")) if confirmed_fill else None
    if confirmed_fill and ox.is_dryrun() and fa.get("dryrun"):
        actual_fill_sz = pos_sz
    pnl = fa.get("pnl") if confirmed_fill else None
    fill_px = fa.get("fill_px") if confirmed_fill else None
    fill_ts = fa.get("fill_ts") if confirmed_fill else None
    ts_source = fa.get("ts_source") if confirmed_fill else None
    # 2026-07-07: close 行也带本环境真实 ct_val（fail-safe：拉不到不带，writer 回退缓存）
    close_ct_val = None
    try:
        close_ct_val = (fetch_instrument_specs(symbol, profile, db_root) or {}).get("ct_val")
    except Exception:
        pass
    trade = {
        "symbol": symbol, "action": "close", "side": side,
        # confirmed：sz 与 fill_sz 同取交易所权威实际成交数量；
        # unconfirmed：sz 仅是本次请求/仓前审计量，fill_sz 保持 NULL。
        "sz": actual_fill_sz if confirmed_fill else pos_sz,
        "fill_sz": actual_fill_sz,
        "requested_sz": pos_sz,
        "pre_position_sz": pos_sz,
        "fill_px": fill_px, "px": fill_px, "pnl": pnl,
        "channel": "live", "reason": reasoning,
        "reduce_only_fallback": used_reduce_only,
        "fill_source": fill_source, "pnl_approx": pnl_approx,
        "fill_ts": fill_ts, "ts_source": ts_source,
        "ct_val": close_ct_val, "ordId": reduce_ord_id,
    }
    if confirmed_fill:
        trade["partial_fill"] = actual_fill_sz < pos_sz - _EPS
        trade["fill_ratio"] = actual_fill_sz / pos_sz
    elif fill_contract_error:
        trade["fill_contract_error"] = fill_contract_error
    _journal_fill("live", trade, db_root, cycle_id,
                  "UNWIND_CLOSE" if _unwind else "CLOSE", unwind=_unwind)
    return receipt(True, action_taken="CLOSE", trades=[trade],
                   reduce_only_fallback=used_reduce_only,
                   fills_ok=confirmed_fill,
                   fill_source=fill_source)


# ---------------------------------------------------------------------------
# REDUCE（Agent 自主部分减仓；绝不静默升级为全平）
# ---------------------------------------------------------------------------
def reduce_position(
    symbol: str,
    profile: str,
    reduce_sz: float,
    *,
    pos_side: str,
    mgn_mode: str = "cross",
    reasoning: str = "",
    db_root: Path = DEFAULT_DB_ROOT,
    cycle_id: Optional[str] = None,
    receipt_context: Optional[dict[str, Any]] = None,
    expected_pre_position_exists: Optional[bool] = None,
    expected_pre_position_sz: Optional[float] = None,
    expected_pre_position_pos_id: Optional[str] = None,
    expected_pre_position_c_time: Optional[str] = None,
) -> dict[str, Any]:
    """Reduce part of one live position with an exact reduce-only market order.

    This is deliberately separate from close_position: requested size
    must remain strictly below the current position, and every order failure is
    returned as-is.  There is no server-side full-close fallback, so a failed
    partial reduction can never silently become a complete exit.
    """
    _require_live_profile(profile, "reduce_position")
    side = str(pos_side or "").strip().lower()
    intent_path = Path(db_root) / "ledger.db"
    intent_fingerprint: Optional[str] = None
    intent_ord_id: Optional[str] = None

    def receipt(ok: bool, **kw) -> dict[str, Any]:
        base = dict(receipt_context or {})
        base.update({
            "profile": "live",
            "ok": ok,
            "cycle_id": cycle_id,
            "action_taken": kw.pop("action_taken", "REDUCE"),
            "symbol": symbol,
            "side": side or None,
            "trades": kw.pop("trades", []),
            "p0": kw.pop("p0", False),
        })
        base.update(kw)
        return base

    requested_sz = _to_float(reduce_sz)
    if requested_sz is None or not math.isfinite(requested_sz) or requested_sz <= 0:
        return receipt(
            False, action_taken="REJECT", reject_reason="bad_reduce_sz",
            reject_detail=f"部分减仓张数必须是有限正数: {reduce_sz!r}")
    if side not in {"long", "short"}:
        return receipt(
            False, action_taken="REJECT", reject_reason="pos_side_required",
            reject_detail="部分减仓必须显式指定 pos_side=long|short")

    # 与 CLOSE 一样不启用 OPEN/ADD 的多周期和 actor 闸；但完整决策卡与 cycle
    # 仍在任何交易所 I/O 前验证，保证 Agent 裁决可追溯。
    ctx_errors = validate_receipt_context(
        receipt_context, cycle_id=cycle_id, required=not ox.is_dryrun())
    if ctx_errors:
        return receipt(
            False, action_taken="REJECT",
            reject_reason="receipt_context_invalid",
            reject_detail="；".join(ctx_errors))
    if not ox.is_dryrun() and not cycle_id:
        return receipt(
            False, action_taken="REJECT", reject_reason="cycle_id_required",
            reject_detail="非 dry-run 部分减仓必须提供调度 cycle_id")
    if not ox.is_dryrun():
        deadline_reject = _cycle_side_effect_reject(cycle_id)
        if deadline_reject:
            return receipt(False, **deadline_reject)

    def _intent_kwargs(now_ts: Optional[str] = None) -> dict[str, Any]:
        return {
            "profile": "live",
            "cycle_id": str(cycle_id),
            "symbol": symbol,
            "side": side,
            "action": "reduce",
            "fingerprint": str(intent_fingerprint),
            "now_ts": now_ts or ledger.now_cst(),
        }

    def _finish_clean(result: dict[str, Any], error: str) -> dict[str, Any]:
        if intent_fingerprint:
            try:
                ei.mark_failed_clean(intent_path, error=error, **_intent_kwargs())
            except Exception as exc:
                result["intent_persist_warning"] = (
                    f"failed_clean transition failed: {type(exc).__name__}: {exc}")
                result["p0"] = True
        return result

    def _finish_uncertain(result: dict[str, Any], error: str) -> dict[str, Any]:
        if intent_fingerprint:
            try:
                ei.mark_uncertain(
                    intent_path, ord_id=intent_ord_id, error=error,
                    **_intent_kwargs())
            except Exception as exc:
                result["intent_persist_warning"] = (
                    f"uncertain transition failed: {type(exc).__name__}: {exc}")
        result["p0"] = True
        return result

    def _finish_completed(result: dict[str, Any]) -> dict[str, Any]:
        if intent_fingerprint:
            try:
                ei.mark_completed(
                    intent_path, ord_id=intent_ord_id, receipt=result,
                    error=None, **_intent_kwargs())
            except Exception as exc:
                result["intent_persist_warning"] = (
                    f"completed transition failed: {type(exc).__name__}: {exc}")
                result["p0"] = True
                _enqueue_repair(
                    "live", symbol, intent_ord_id,
                    "reduce_execution_intent_complete_failed", db_root)
        return result

    # 独立 action=reduce 键保留 OPEN 历史键不变，同时为重复部分减仓提供持久幂等。
    if not ox.is_dryrun():
        request = {
            "profile": "live", "cycle_id": cycle_id, "symbol": symbol,
            "action": "reduce", "side": side, "reduce_sz": requested_sz,
            "mgn_mode": mgn_mode,
            "expected_pre_position_exists": expected_pre_position_exists,
            "expected_pre_position_sz": expected_pre_position_sz,
            "expected_pre_position_pos_id": expected_pre_position_pos_id,
            "expected_pre_position_c_time": expected_pre_position_c_time,
        }
        try:
            reserved = ei.reserve(
                intent_path, profile="live", cycle_id=str(cycle_id),
                symbol=symbol, side=side, action="reduce", request=request,
                now_ts=ledger.now_cst())
        except Exception as exc:
            return receipt(
                False, action_taken="REJECT",
                reject_reason="execution_intent_store_failed",
                reject_detail=(
                    "减仓幂等意图库不可用，未发送订单: "
                    f"{type(exc).__name__}: {exc}"), p0=True)
        if reserved.get("status") == "replay":
            cached = dict(reserved["receipt"])
            cached["idempotent_replay"] = True
            cached["intent_state"] = "completed"
            return cached
        if reserved.get("status") != "reserved":
            blocker = reserved.get("blocking_intent") or {}
            _enqueue_repair(
                "live", symbol, reserved.get("ord_id"),
                "reduce_execution_intent_blocked:"
                f"{reserved.get('reason')}:{blocker}", db_root)
            return receipt(
                False, action_taken="REJECT",
                reject_reason="execution_intent_blocked",
                reject_detail=(
                    "存在未确认的执行意图，部分减仓未发送；"
                    "可在核实当前仓位后选择完整 close 去风险"),
            )
        intent_fingerprint = str(reserved["fingerprint"])

    # 现仓真值与规格只在 intent 预留后读取。
    try:
        positions = fetch_open_positions(profile)
    except PositionsUnavailable as exc:
        return _finish_clean(receipt(
            False, action_taken="REJECT",
            reject_reason="positions_fetch_failed",
            reject_detail=f"现仓 API 失败，无法确认部分减仓目标: {exc}", p0=True,
        ), "positions_fetch_failed")
    match = next((
        row for row in positions
        if row.get("symbol") == symbol
        and str(row.get("side") or "").lower() == side
    ), None)
    fingerprint_error = _position_fingerprint_error(
        match,
        expected_exists=expected_pre_position_exists,
        expected_sz=expected_pre_position_sz,
        expected_pos_id=expected_pre_position_pos_id,
        expected_c_time=expected_pre_position_c_time,
    )
    if fingerprint_error:
        return _finish_clean(receipt(
            False, action_taken="REJECT",
            reject_reason="pre_position_fingerprint_changed",
            reject_detail=(
                "执行时减仓目标已不同于 plan/facts；未发送订单: "
                + fingerprint_error
            ),
            expected_pre_position={
                "exists": expected_pre_position_exists,
                "sz": expected_pre_position_sz,
                "posId": expected_pre_position_pos_id,
                "cTime": expected_pre_position_c_time,
            },
            actual_pre_position=match,
        ), "pre_position_fingerprint_changed")
    if match is None:
        return _finish_clean(receipt(
            False, action_taken="REJECT", reject_reason="no_position",
            reject_detail=f"{symbol} 当前无 {side} live 持仓，未发送减仓单",
        ), "no_position")
    pre_position_sz = _to_float(match.get("sz"))
    if (pre_position_sz is None or not math.isfinite(pre_position_sz)
            or pre_position_sz <= 0):
        return _finish_clean(receipt(
            False, action_taken="REJECT",
            reject_reason="invalid_position_size",
            reject_detail=f"OKX 现仓数量非法: {match.get('sz')!r}", p0=True,
        ), "invalid_position_size")

    specs = fetch_instrument_specs(symbol, profile, db_root)
    lot_sz = _to_float(specs.get("lot_sz"))
    min_sz = _to_float(specs.get("min_sz")) or lot_sz
    if (lot_sz is None or not math.isfinite(lot_sz) or lot_sz <= 0
            or min_sz is None or not math.isfinite(min_sz) or min_sz <= 0):
        return _finish_clean(receipt(
            False, action_taken="REJECT",
            reject_reason="instrument_specs_missing",
            reject_detail=(
                f"部分减仓需要有效 lotSz/minSz，当前 lotSz={lot_sz} "
                f"minSz={min_sz}"),
        ), "instrument_specs_missing")
    approved_sz = rv._round_down_to_step(requested_sz, lot_sz)
    if approved_sz < min_sz - _EPS:
        return _finish_clean(receipt(
            False, action_taken="REJECT", reject_reason="reduce_below_min_sz",
            reject_detail=(
                f"请求 {requested_sz} 按 lotSz={lot_sz} 向下取整为 {approved_sz}，"
                f"低于 minSz={min_sz}"),
        ), "reduce_below_min_sz")
    size_tol = max(_EPS, pre_position_sz * 1e-9)
    if approved_sz >= pre_position_sz - size_tol:
        return _finish_clean(receipt(
            False, action_taken="REJECT",
            reject_reason="partial_reduce_requires_less_than_position",
            reject_detail=(
                f"部分减仓 approved_sz={approved_sz} 必须严格小于现仓 "
                f"{pre_position_sz}；完整退出请调用 close_position"),
        ), "partial_reduce_requires_less_than_position")
    planned_remaining = pre_position_sz - approved_sz
    if planned_remaining < min_sz - size_tol:
        return _finish_clean(receipt(
            False, action_taken="REJECT", reject_reason="reduce_would_leave_dust",
            reject_detail=(
                f"减仓后计划剩余 {planned_remaining} 小于 minSz={min_sz}；"
                "请选择更小减仓量或完整 close"),
        ), "reduce_would_leave_dust")

    if not ox.is_dryrun():
        deadline_reject = _cycle_side_effect_reject(cycle_id)
        if deadline_reject:
            return _finish_clean(
                receipt(False, **deadline_reject),
                str(deadline_reject["reject_reason"]),
            )
    if intent_fingerprint:
        try:
            ei.mark_submitting(intent_path, error=None, **_intent_kwargs())
        except Exception as exc:
            return receipt(
                False, action_taken="REJECT",
                reject_reason="execution_intent_transition_failed",
                reject_detail=(
                    "订单前幂等状态无法固化，未发送减仓单: "
                    f"{type(exc).__name__}: {exc}"), p0=True)

    reduce_start_ms = int(time.time() * 1000)
    try:
        placed = ox.place_reduce_only_market(
            symbol, side, approved_sz, profile, mgn_mode=mgn_mode)
    except Exception as exc:
        _enqueue_repair(
            "live", symbol, None, "reduce_place_exception_ambiguous", db_root)
        return _finish_uncertain(receipt(
            False, action_taken="REJECT",
            reject_reason="reduce_place_exception_ambiguous",
            reject_detail=(
                "部分减仓调用异常，是否到达交易所未知；未重试、未升级全平，"
                f"已入 repair_queue: {type(exc).__name__}: {exc}"),
        ), "reduce_place_exception_ambiguous")
    if not placed.get("ok"):
        sc = placed.get("sCode")
        ambiguous = not sc and bool(placed.get("error"))
        if ambiguous and not ox.is_dryrun():
            _enqueue_repair(
                "live", symbol, None, "reduce_place_ambiguous", db_root)
            return _finish_uncertain(receipt(
                False, action_taken="REJECT",
                reject_reason="reduce_place_ambiguous",
                reject_detail=(
                    "部分减仓写入结果不明；未重试、未升级全平，已入 repair_queue"),
            ), "reduce_place_ambiguous")
        reason = (
            "delisted" if sc == ox.SCODE_DELISTED
            else "instrument_not_exist" if sc == ox.SCODE_NOT_EXIST
            else "reduce_place_failed")
        return _finish_clean(receipt(
            False, action_taken="REJECT", reject_reason=reason,
            reject_detail=f"sCode={sc} {placed.get('sMsg') or placed.get('error')}",
        ), f"reduce_place_rejected:{reason}")

    ord_id = next((
        str(row.get("ordId")) for row in placed.get("data", [])
        if isinstance(row, dict) and row.get("ordId")
    ), None)
    intent_ord_id = ord_id
    if not ord_id and not ox.is_dryrun():
        _enqueue_repair("live", symbol, None, "reduce_ord_id_missing", db_root)
        return _finish_uncertain(receipt(
            False, action_taken="REJECT", reject_reason="reduce_ord_id_missing",
            reject_detail=(
                "交易所接受部分减仓但未返回唯一 ordId；禁止模糊归因，已入 repair_queue"),
        ), "reduce_ord_id_missing")
    if intent_fingerprint:
        try:
            ei.mark_submitted(
                intent_path, ord_id=intent_ord_id, error=None,
                **_intent_kwargs())
        except Exception as exc:
            print(
                f"[order_executor] WARN reduce intent submitted 写失败 "
                f"{symbol}: {exc}", file=sys.stderr)

    try:
        fa = _read_fills(
            symbol, profile, ord_id,
            since_ms=None if ord_id else reduce_start_ms)
    except Exception as exc:
        print(
            f"[order_executor] WARN reduce fills 回读异常 {symbol}: {exc}",
            file=sys.stderr)
        fa = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    fill_source = "fills"
    if not fa.get("ok") and not ox.is_dryrun():
        try:
            alt = _confirm_order_filled(symbol, profile, ord_id)
        except Exception as exc:
            print(
                f"[order_executor] WARN reduce order 回读异常 {symbol}: {exc}",
                file=sys.stderr)
            alt = None
        if alt and alt.get("ok"):
            fa = alt
            fill_source = str(alt.get("source") or "order_status")
        elif alt and alt.get("state") == "canceled":
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="reduce_not_filled",
                reject_detail="部分减仓单 canceled 且权威订单状态确认 0 成交",
                ord_id=ord_id,
            ), "reduce_canceled_no_fill")
    if not fa.get("ok"):
        _enqueue_repair(
            "live", symbol, ord_id, "reduce_fill_unconfirmed", db_root)
        return _finish_uncertain(receipt(
            False, action_taken="REJECT",
            reject_reason="reduce_fill_unconfirmed",
            reject_detail=(
                "部分减仓已提交但 fills/订单状态均未确认；禁止按请求量猜成交，"
                "已入 repair_queue"), ord_id=ord_id,
        ), "reduce_fill_unconfirmed")

    fill_valid, fill_error = _validate_confirmed_close_fill(
        fa, fill_source, dryrun=ox.is_dryrun(), requested_sz=approved_sz)
    if not fill_valid:
        _enqueue_repair(
            "live", symbol, ord_id,
            f"reduce_fill_contract_invalid:{fill_error}", db_root)
        return _finish_uncertain(receipt(
            False, action_taken="REJECT",
            reject_reason="reduce_fill_contract_invalid",
            reject_detail=(
                "部分减仓成交端点字段不完整，禁止合成数量/价格/时间: "
                f"{fill_error}"), ord_id=ord_id,
        ), f"reduce_fill_contract_invalid:{fill_error}")

    actual_fill_sz = _to_float(fa.get("fill_sz"))
    fill_px = _to_float(fa.get("fill_px"))
    if ox.is_dryrun() and fa.get("dryrun"):
        actual_fill_sz = approved_sz
    if actual_fill_sz is None or actual_fill_sz <= 0:
        return _finish_uncertain(receipt(
            False, action_taken="REJECT",
            reject_reason="reduce_fill_size_invalid",
            reject_detail="权威成交数量缺失，禁止写入减仓账本", ord_id=ord_id,
        ), "reduce_fill_size_invalid")

    post_position_sz: Optional[float]
    position_delta_warning = None
    if ox.is_dryrun():
        post_position_sz = max(0.0, pre_position_sz - actual_fill_sz)
    else:
        time.sleep(1.0)
        try:
            post_position_sz = _position_size(
                fetch_open_positions(profile), symbol, side)
        except PositionsUnavailable:
            post_position_sz = None
        expected_remaining = max(0.0, pre_position_sz - actual_fill_sz)
        if (post_position_sz is None
                or abs(post_position_sz - expected_remaining)
                > max(_EPS, pre_position_sz * 1e-8)):
            position_delta_warning = (
                f"expected={expected_remaining}, observed={post_position_sz}")
            _enqueue_repair(
                "live", symbol, ord_id,
                f"reduce_position_delta_mismatch:{position_delta_warning}", db_root)

    trade = {
        "symbol": symbol, "action": "reduce", "side": side,
        "sz": actual_fill_sz, "fill_sz": actual_fill_sz,
        "requested_sz": requested_sz, "approved_sz": approved_sz,
        "pre_position_sz": pre_position_sz,
        "post_position_sz": post_position_sz,
        "fill_px": fill_px, "px": fill_px, "pnl": fa.get("pnl"),
        "channel": "live", "reason": reasoning,
        "fill_source": fill_source, "pnl_approx": False,
        "fill_ts": fa.get("fill_ts"), "ts_source": fa.get("ts_source"),
        "ct_val": specs.get("ct_val"), "ordId": ord_id,
        "partial_fill": actual_fill_sz < approved_sz - _EPS,
        "fill_ratio": actual_fill_sz / approved_sz,
    }
    _journal_fill("live", trade, db_root, cycle_id, "REDUCE")

    # 减仓成交已确认后，把原全仓止损数量同步到剩余仓位。失败不反向抹掉
    # 已成交事实，但必须外显并入 repair；任何路径都不撤销止损制造裸仓。
    protection_sync: dict[str, Any]
    protection_p0 = False
    if ox.is_dryrun():
        protection_sync = {
            "ok": True, "dryrun": True,
            "planned_remaining_sz": post_position_sz,
        }
    elif post_position_sz is not None and post_position_sz > _EPS:
        sync = adjust_protection(
            symbol, profile, pos_side=side, resize_to_full_position=True,
            consolidate_extra_sl=True,
            reasoning=reasoning or "部分减仓后同步全仓止损数量",
            db_root=db_root, cycle_id=cycle_id,
            receipt_context=receipt_context,
            reason_code="post_reduce_resize",
        )
        protection_sync = {
            key: sync.get(key) for key in (
                "ok", "action_taken", "reject_reason", "reject_detail",
                "p0", "path", "protection_state", "applied")
            if sync.get(key) is not None
        }
        if not sync.get("ok"):
            _enqueue_repair(
                "live", symbol, ord_id,
                f"reduce_protection_sync_failed:{sync.get('reject_reason')}",
                db_root)
            protection_p0 = bool(
                sync.get("p0")
                or sync.get("reject_reason") in {
                    "no_sl_to_preserve", "naked_after_change"
                })
    elif post_position_sz == 0:
        protection_sync = {
            "ok": True,
            "note": "position_gone_after_confirmed_partial_order",
        }
    else:
        protection_sync = {
            "ok": False,
            "reject_reason": "post_position_unavailable",
        }

    return _finish_completed(receipt(
        True, action_taken="REDUCE", trades=[trade], ord_id=ord_id,
        requested_sz=requested_sz, approved_sz=approved_sz,
        pre_position_sz=pre_position_sz, post_position_sz=post_position_sz,
        position_delta_warning=position_delta_warning,
        protection_sync=protection_sync, p0=protection_p0,
        fill_source=fill_source,
    ))


# ---------------------------------------------------------------------------
# 执行 journal：执行即留痕
# ---------------------------------------------------------------------------
JOURNAL_SUBDIR = "journal"


def journal_path(profile_norm: str, db_root: Path = DEFAULT_DB_ROOT) -> Path:
    """journal 文件路径。默认挂在 db_root 下——tests 用临时 db_root 时自动隔离；
    microtest 用生产 db_root，隔离靠 TEST- 哨兵 cycle（重放/监控层默认排除）。
    OKX_EXEC_JOURNAL_DIR 可显式覆盖。"""
    base = os.environ.get("OKX_EXEC_JOURNAL_DIR")
    jdir = Path(base) if base else (Path(db_root) / JOURNAL_SUBDIR)
    return jdir / f"exec_{profile_norm}.jsonl"


def _journal_fill(profile_norm: str, trade: dict[str, Any],
                  db_root: Path = DEFAULT_DB_ROOT,
                  cycle_id: Optional[str] = None,
                  action_taken: Optional[str] = None,
                  unwind: bool = False) -> None:
    """成交即留痕（append-only JSONL，每 profile 一文件=单写方）。

    成交确认后同进程落一行 journal；trades_writer --from-journal 可重放，
    collection_monitor 扫「>15min 未入账」补救。fail-open：journal 写失败绝不
    阻断交易回执返回（真金路径优先），仅 stderr WARN。dryrun 不留痕（合成成交
    重放会污染账本）。unwind=True 标记 SL 失败平裸仓的 close（回执 trades=[]，
    重放层对其一律 P1 人工，见 close_position S2e 注释）。"""
    try:
        if ox.is_dryrun():
            return
        p = journal_path(profile_norm, db_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": ledger.now_cst(), "ms": int(time.time() * 1000),
               "profile": profile_norm, "cycle_id": cycle_id,
               "action_taken": action_taken, "trade": trade}
        if unwind:
            rec["unwind"] = True
        line = json.dumps(rec, ensure_ascii=False)
        # 黏行守卫（核验修 2026-07-16）：上一次 append 若被中途杀出撕裂残行（无尾换行），
        # 直接续写会把残行和本记录黏成一行、两条全废。写前查末字节，缺换行先补一个——
        # 残行仍坏（重放解析跳过并计数告警），但本记录保住。
        prefix = ""
        try:
            if p.exists() and p.stat().st_size > 0:
                with open(p, "rb") as rf:
                    rf.seek(-1, 2)
                    if rf.read(1) != b"\n":
                        prefix = "\n"
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(prefix + line + "\n")
    except Exception as exc:
        print(f"[order_executor] WARN exec journal 写失败(忽略): {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# repair_queue（fills 失败入队，复用现有表）
# ---------------------------------------------------------------------------
def _enqueue_repair(profile: str, symbol: str, ord_id: Optional[str],
                    reason: str, db_root: Path = DEFAULT_DB_ROOT) -> None:
    """写 account.db.repair_queue（复用既有表 schema：check_name/issue/fix_action/...）。"""
    account_db = Path(db_root) / "account.db"  # 容 str 路径
    if not account_db.exists():
        return
    try:
        ts = ledger.now_cst()
        issue = f"[{profile}] {symbol} ord={ord_id}: {reason}"
        fills_file = (
            _PROJECT_ROOT / "tmp" /
            f"repair_{profile}_{symbol}_fills.json"
        ).as_posix()
        python_wrapper = (
            _PROJECT_ROOT / "scripts" / "run_okx_python.ps1"
        ).as_posix()
        okx_cli = (_PROJECT_ROOT / "scripts" / "_okxcli.py").as_posix()
        fix = (
            f"pwsh -NoProfile -File {python_wrapper} {okx_cli} "
            f"--profile {profile} --compact --out-file {fills_file} "
            f"swap fills --instId {symbol} --archive"
        )
        con = ledger.connect(account_db)
        try:
            exists = con.execute(
                "SELECT 1 FROM repair_queue WHERE check_name='order_executor' "
                "AND issue=? AND status IN ('open','pending') LIMIT 1",
                (issue,)).fetchone()
            if not exists:
                con.execute(
                    "INSERT INTO repair_queue (ts, check_name, issue, fix_action, "
                    "status, created_utc) VALUES (?,?,?,?,?,?)",
                    (ts, "order_executor", issue, fix, "pending", ts))
            con.commit()
        finally:
            con.close()
    except Exception as exc:  # repair 写失败不可再静默：裸仓待修记录丢失是 P0 盲区（P2-4）
        print(f"[order_executor] WARN repair_queue 写失败({reason}): {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 保护单调整（移动止损 / 止盈 / 加仓后扩全仓）——2026-08-13 主人授权
# ---------------------------------------------------------------------------
# 授权边界（主人 2026-08-13 逐条拍板，改动前必读）：
#   1. **止损调整方向＝完全自主**：可收紧也可放宽，**不重算** 5% 单笔止损风险预算。
#      这意味着开仓时经 risk_validator 核准的「最大亏损」在持仓期内不再是硬上限。
#      仍保留 `MAX_SL_DEVIATION`(30%) 偏离闸——它防的是填错价/填错标的这类事故
#      （止损误设成 0.01 会「表面已保护、实则永不触发」，比裸仓更隐蔽），不是风险偏好闸。
#   2. **止损不可撤只能替换**：持仓存续期间必须始终有一张已回读确认的全仓止损。
#      本函数任何分支都不得让持仓变成裸仓——这是 P0 不变量，优先级高于「改单成功」。
#   3. **加仓后自动扩到全仓**：`new_sz` 走 amend 的 `--newSz`。
#
# **加仓会产生第二张止损**（2026-08-13 阶段3 从代码事实推定、非猜测）：`open_position`
# 每笔成交都自挂一张 `approved_sz` 大小的 reduceOnly 止损，且 `_verify_sl_placed` 要求
# `cTime >= 本次请求时刻`——因此加仓后交易所上必然是「旧档 + 新档」两张，而不是一张全仓单。
# 此时总覆盖量已达全仓（非裸仓），但不满足「恰好一张全仓止损」的终局契约，
# 而本函数第 3 段的 `duplicate_sl_before_change` 会直接拒绝。所以「加仓后自动扩到全仓」
# 必须显式走收敛路径 `consolidate_extra_sl=True`：
#     幸存单 amend 到全仓 → 独立回读确认 → 才撤多余单
# 顺序不可颠倒；撤单永远发生在「接替单已被交易所确认覆盖全仓」之后，
# 任一瞬间覆盖量都 ≥ 现仓。默认 False，保持人工改单时对残单状态的严格拒绝。
#
# 主路径＝`ox.amend_algo_protection`（CLI `swap algo amend`，交易所服务端原子生效，
# 全程无「零张止损」或「两张止损」的中间窗口）。仅当 amend 失败（算法单已触发/已消失）
# 才退化为「挂新单→回读确认→撤旧单」——该顺序保证两种失败都安全：撤旧失败最多留下
# 两张全仓 reduceOnly 止损（先触发的平掉仓，后一张自动作废，无害）；挂新失败则旧单仍在。
# 替换时若需要同一单承载 TP+SL，必须以 ordType=oco 新挂；普通 conditional 同时传两腿
# 会忽略 TP，禁止把参数被 CLI 接受误当作止盈已生效。
#
# 止盈无风险预算含义（reduceOnly 只减不增仓），缺失也不构成裸仓，故校验较松。
#
# **阶段3 真单验证已通过**（2026-08-13 22:43~22:55，live DOGE 1→2 张，交易所独立回读确认）：
#   ① amend 移动止损 0.067724→0.068776，algoId 3829560867421102080 **不变**
#      （证实 amend 不换单、cTime 不刷新，故后置断言只能用 assert_protection_state）；
#   ② 加仓后交易所上确为**两张分档止损**（新档 …994947575808 @0.06762 sz=1.0 +
#      旧档 …867421102080 @0.068776 sz=1.0），与上文的代码事实推定逐字吻合；
#   ③ `consolidate_extra_sl=True` 收敛为一张全仓：幸存单取同量中更早挂的旧档、
#      价取最保护的一档 0.068776（未隐式放松）、撤单发生在扩仓回读确认之后，
#      终局独立回读恰好 1 张 sz=2.0。path=amend_consolidate。
#   验收记录见 docs/protection_amend_stage3_acceptance_20260813.md。
#
# 生产调用方（2026-08-13 起两条，完全对称，失败都只外显+入 repair，绝不回滚成交）：
#   - `reduce_position` 部分减仓成交后同步止损到剩余仓位（reason_code=post_reduce_resize）；
#   - `open_position` **加仓**成交后收敛分档止损并扩到全仓（reason_code=post_add_resize），
#     即主人 2026-08-13 授权的「加仓后自动扩到全仓」。仅当开仓前同侧已有仓位时触发；
#     全新开仓只有一张 SL，多跑一次改单纯属加风险。契约见 tests/test_add_protection_sync.py。

_PROTECTION_TOL_PCT = 0.001          # 回读比价容差（与 _verify_trigger_placed 同口径）
_PROTECTION_SIZE_REL_TOL = 1e-9


def _live_protection_rows(symbol: str, pos_side: str,
                          profile: str) -> list[dict[str, Any]]:
    """读该仓当前所有 live reduceOnly 保护单（close 方向、同 posSide）。

    只做事实归集，不做判断；调用方据此选目标单与做后置断言。
    """
    close_side = "sell" if pos_side == "long" else "buy"
    try:
        algos = ox.get_algo_orders(symbol, profile)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for a in algos:
        if not isinstance(a, dict):
            continue
        if str(a.get("instId") or "") not in ("", symbol):
            continue
        state = str(a.get("state") or "").lower()
        if state and state != "live":
            continue
        row_pos_side = str(a.get("posSide") or "").lower()
        if row_pos_side and row_pos_side != pos_side:
            continue
        row_side = str(a.get("side") or "").lower()
        if row_side and row_side != close_side:
            continue
        raw_reduce_only = a.get("reduceOnly")
        if raw_reduce_only not in (None, "") and \
                str(raw_reduce_only).lower() not in ("true", "1"):
            continue
        rows.append({
            "algoId": str(a.get("algoId") or "") or None,
            "slTriggerPx": _to_float(a.get("slTriggerPx")),
            "tpTriggerPx": _to_float(a.get("tpTriggerPx")),
            "sz": _to_float(a.get("sz")),
            "cTime": _to_float(a.get("cTime") or a.get("createTime")),
            "state": state or None,
        })
    return rows


def assert_protection_state(symbol: str, pos_side: str, profile: str, *,
                            expected_sl_px: float, expected_sz: float,
                            expected_tp_px: Optional[float] = None,
                            retries: int = 2) -> dict[str, Any]:
    """后置断言：该仓当前**恰好**有一张 live 全仓止损，且触发价＝期望值。

    与 `_verify_sl_placed` 的区别：后者验证「本次新挂的单」（要求 cTime 晚于请求时刻），
    amend 不会刷新 cTime，故不适用；本函数验证的是**保护状态的终局事实**，
    顺带检出「旧单没撤干净」这种 `_verify_sl_placed` 命中首个匹配即返回会漏掉的情况。
    """
    last: dict[str, Any] = {}
    for attempt in range(max(1, int(retries))):
        rows = _live_protection_rows(symbol, pos_side, profile)
        sl_rows = [r for r in rows
                   if r["slTriggerPx"] is not None and r["slTriggerPx"] > 0]
        matched = []
        for r in sl_rows:
            px_ok = abs(r["slTriggerPx"] - expected_sl_px) / expected_sl_px \
                <= _PROTECTION_TOL_PCT
            tp_ok = (
                expected_tp_px is None
                or (
                    r.get("tpTriggerPx") is not None
                    and abs(r["tpTriggerPx"] - expected_tp_px) / expected_tp_px
                    <= _PROTECTION_TOL_PCT
                )
            )
            sz_ok = r["sz"] is None or abs(r["sz"] - expected_sz) <= max(
                _EPS, expected_sz * _PROTECTION_SIZE_REL_TOL)
            if px_ok and tp_ok and sz_ok:
                matched.append(r)
        last = {
            "ok": len(matched) == 1 and len(sl_rows) == 1,
            "live_sl_count": len(sl_rows),
            "matched_count": len(matched),
            "rows": sl_rows,
            "duplicate_sl": len(sl_rows) > 1,
            "naked": len(sl_rows) == 0,
            "expected_tp_px": expected_tp_px,
        }
        if last["ok"]:
            return last
        if attempt < max(1, int(retries)) - 1:
            time.sleep(1.0)
    return last


def _cancel_stale_protection(symbol: str, profile: str,
                             rows: list[dict[str, Any]], db_root: Path,
                             *, note: str) -> list[str]:
    """撤掉已被取代的保护单，返回撤失败的 algoId 列表。

    **调用方必须先独立回读确认接替单已覆盖全仓**——本函数只负责撤，不做保护性判断。
    撤失败不抛异常：多留一张 reduceOnly 止损是「过度保护」（先触发者平仓，
    另一张自动作废），比中断流程更安全；但必入 repair_queue 由人工收敛。
    """
    failed: list[str] = []
    for row in rows:
        algo_id = str((row or {}).get("algoId") or "")
        if not algo_id:
            continue
        try:
            res = ox.cancel_algo_order(symbol, algo_id, profile)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not res.get("ok"):
            failed.append(algo_id)
    if failed:
        _enqueue_repair(profile, symbol, None,
                        f"{note}:{','.join(failed)}", db_root)
        print(f"[order_executor] WARN 残余保护单撤单失败 {symbol} algoIds={failed}"
              "（仍是过度保护而非裸仓）；已入 repair_queue", file=sys.stderr)
    return failed


def validate_protection_change(pos_side: str, mark_px: Optional[float],
                               new_sl_trigger_px: Optional[float],
                               new_tp_trigger_px: Optional[float]
                               ) -> list[str]:
    """纯函数确定性校验（无 I/O）。**刻意不含风险预算重算**（主人 2026-08-13 拍板）。

    只挡「这单在交易所层面没有意义或几乎必是填错」的情形：
      - 止损方向错（多头止损在现价之上／空头在其下）＝下单即触发，等同市价平仓而非止损；
      - 偏离 mark 超 `MAX_SL_DEVIATION`(30%)＝疑似填错价或错标的；
      - 止盈方向错（多头止盈在现价之下／空头在其上）＝同理立即成交。
    """
    errors: list[str] = []
    side = str(pos_side or "").lower()
    if side not in ("long", "short"):
        return ["pos_side 非法（须 long|short）"]
    mark = _to_float(mark_px)
    if mark is None or not math.isfinite(mark) or mark <= 0:
        return ["mark_px 缺失或非法（禁用其它字段猜）"]
    if new_sl_trigger_px is None and new_tp_trigger_px is None:
        return ["未提供任何新的保护价（sl/tp 至少一个）"]

    if new_sl_trigger_px is not None:
        sl = _to_float(new_sl_trigger_px)
        if sl is None or not math.isfinite(sl) or sl <= 0:
            errors.append("新止损价非法（须为正有限数）")
        else:
            if side == "long" and sl >= mark:
                errors.append(
                    f"多头止损 {sl} 不低于现价 {mark}：下单即触发＝市价平仓，非止损")
            if side == "short" and sl <= mark:
                errors.append(
                    f"空头止损 {sl} 不高于现价 {mark}：下单即触发＝市价平仓，非止损")
            dev = abs(sl - mark) / mark
            if dev > rv.MAX_SL_DEVIATION:
                errors.append(
                    f"止损偏离现价 {dev:.1%} > "
                    f"{rv.MAX_SL_DEVIATION:.0%}（疑填错价/错标的）")

    if new_tp_trigger_px is not None:
        tp = _to_float(new_tp_trigger_px)
        if tp is None or not math.isfinite(tp) or tp <= 0:
            errors.append("新止盈价非法（须为正有限数）")
        else:
            if side == "long" and tp <= mark:
                errors.append(f"多头止盈 {tp} 不高于现价 {mark}：下单即触发")
            if side == "short" and tp >= mark:
                errors.append(f"空头止盈 {tp} 不低于现价 {mark}：下单即触发")
    return errors


def adjust_protection(
    symbol: str,
    profile: str,
    *,
    pos_side: Optional[str] = None,
    new_sl_trigger_px: Optional[float] = None,
    new_tp_trigger_px: Optional[float] = None,
    resize_to_full_position: bool = False,
    consolidate_extra_sl: bool = False,
    reasoning: Optional[str] = None,
    db_root: Path = DEFAULT_DB_ROOT,
    cycle_id: Optional[str] = None,
    receipt_context: Optional[dict[str, Any]] = None,
    reason_code: str = "agent_adjust",
    expected_pre_position_exists: Optional[bool] = None,
    expected_pre_position_sz: Optional[float] = None,
    expected_pre_position_pos_id: Optional[str] = None,
    expected_pre_position_c_time: Optional[str] = None,
) -> dict[str, Any]:
    """调整现有持仓的保护单（移动止损 / 设改止盈 / 加仓后扩到全仓）。

    返回 `trades_writer` 风格回执：`{profile, ok, action_taken, symbol, ...}`，
    `action_taken` ∈ `ADJUST_PROTECTION` | `REJECT`。**不改变持仓数量**，
    因此不产生 open/add/close/reduce 成交行；成功回执以
    `decision=hold,n_orders=0` 写入 `trade_cycles.raw` 保留完整保护变更审计。

    `consolidate_extra_sl=True` 时额外允许「多张分档止损 → 一张全仓止损」的收敛
    （加仓后的必经状态，见本节顶部注释）；默认 False 时残单状态一律拒绝改单。

    不变量（任何返回路径都成立）：调用前有全仓止损 ⇒ 调用后仍有全仓止损。
    """
    _require_live_profile(profile, "adjust_protection")
    started_ms = time.time() * 1000.0
    intent_path = Path(db_root) / "ledger.db"
    intent_fingerprint: Optional[str] = None
    intent_algo_id: Optional[str] = None
    # OPEN/REDUCE 内部的保护数量同步已由父订单 intent 覆盖；除此之外的
    # 所有独立保护调整都必须自建 action 键。不能让调用方仅靠换一个
    # reason_code 就绕过执行意图，从而把已发生的交易所改单误报成零副作用。
    standalone_intent = reason_code not in {
        "post_add_resize", "post_reduce_resize",
    }

    def receipt(ok: bool, **kw) -> dict[str, Any]:
        base = dict(receipt_context or {})
        action_taken = kw.pop("action_taken", "ADJUST_PROTECTION")
        base.update({
            "profile": "live", "mode": "live", "ok": ok,
            "cycle_id": cycle_id, "action_taken": action_taken,
            "symbol": symbol, "trades": [], "n_orders": 0,
            "p0": kw.pop("p0", False),
            "protection_change": {
                "reason_code": reason_code,
                "requested_sl": _to_float(new_sl_trigger_px),
                "requested_tp": _to_float(new_tp_trigger_px),
                "resize_to_full_position": bool(resize_to_full_position),
            },
        })
        # 保护调整是有交易所副作用、但无成交行的正式业务动作。
        # 不让 Agent 猜 `decision=adjust_protection|traded`；返回即可原样交 writer。
        if ok and action_taken == "ADJUST_PROTECTION":
            base.update({"status": "ok", "decision": "hold", "errors": []})
        if reasoning:
            base["reasoning"] = reasoning
        base.update(kw)
        return base

    def _intent_kwargs(now_ts: Optional[str] = None) -> dict[str, Any]:
        return {
            "profile": "live",
            "cycle_id": str(cycle_id),
            "symbol": symbol,
            "side": str(pos_side),
            "action": "adjust_protection",
            "fingerprint": str(intent_fingerprint),
            "now_ts": now_ts or ledger.now_cst(),
        }

    def _finish_clean(result: dict[str, Any], error: str) -> dict[str, Any]:
        if intent_fingerprint:
            try:
                ei.mark_failed_clean(
                    intent_path, error=error, **_intent_kwargs())
                result["intent_state"] = "failed_clean"
            except Exception as exc:
                result["intent_persist_warning"] = (
                    "failed_clean transition failed: "
                    f"{type(exc).__name__}: {exc}")
                result["p0"] = True
        return result

    def _finish_uncertain(result: dict[str, Any], error: str) -> dict[str, Any]:
        if intent_fingerprint:
            try:
                ei.mark_uncertain(
                    intent_path, ord_id=intent_algo_id, error=error,
                    **_intent_kwargs())
                result["intent_state"] = "uncertain"
            except Exception as exc:
                result["intent_persist_warning"] = (
                    "uncertain transition failed: "
                    f"{type(exc).__name__}: {exc}")
        # Exchange-side ambiguity is always fail-closed, but ``p0`` remains
        # reserved for a naked position / lost persistence.  A verified old
        # stop or duplicate reduceOnly stops are protected non-P0 states.
        result["exchange_side_effect_uncertain"] = True
        return result

    def _finish_completed(result: dict[str, Any]) -> dict[str, Any]:
        if intent_fingerprint:
            try:
                ei.mark_completed(
                    intent_path, ord_id=intent_algo_id, receipt=result,
                    error=None, **_intent_kwargs())
                result["intent_state"] = "completed"
            except Exception as exc:
                # submitting 状态仍会阻断后续交易；交易所事实
                # 继续返给 writer，避免因 intent 告警丢掉主账审计。
                result["intent_persist_warning"] = (
                    "completed transition failed: "
                    f"{type(exc).__name__}: {exc}")
                result["p0"] = True
                _enqueue_repair(
                    "live", symbol, intent_algo_id,
                    "adjust_protection_execution_intent_complete_failed",
                    db_root)
        return result

    ctx_errors = validate_receipt_context(
        receipt_context, cycle_id=cycle_id, required=not ox.is_dryrun())
    if ctx_errors:
        return receipt(False, action_taken="REJECT",
                       reject_reason="receipt_context_invalid",
                       reject_detail="；".join(ctx_errors))
    if not ox.is_dryrun() and not cycle_id:
        return receipt(False, action_taken="REJECT",
                       reject_reason="cycle_id_required",
                       reject_detail="非 dry-run 调整保护单必须提供调度 cycle_id")
    if new_sl_trigger_px is None and new_tp_trigger_px is None \
            and not resize_to_full_position:
        return receipt(False, action_taken="REJECT",
                       reject_reason="no_change_requested",
                       reject_detail="未提供新的止损/止盈价，也未要求扩到全仓")
    if not ox.is_dryrun() and standalone_intent:
        deadline_reject = _cycle_side_effect_reject(cycle_id)
        if deadline_reject:
            return receipt(False, **deadline_reject)

    # ── 1. 现仓求真（禁用快照；positions API 失败必须 fail-closed）──────────
    requested_side = str(pos_side or "").strip().lower() or None
    if requested_side not in (None, "long", "short"):
        return receipt(False, action_taken="REJECT",
                       reject_reason="pos_side_invalid",
                       reject_detail="保护调整 pos_side 必须是 long|short")
    try:
        positions = fetch_open_positions(profile)
    except PositionsUnavailable as exc:
        return receipt(False, action_taken="REJECT",
                       reject_reason="positions_unavailable",
                       reject_detail=f"OKX 现仓查询失败，拒绝盲改保护单: {exc}")
    matches = [
        p for p in positions
        if p.get("symbol") == symbol
        and (requested_side is None
             or str(p.get("side") or "").lower() == requested_side)
    ]
    if requested_side is None and len(matches) > 1:
        return receipt(False, action_taken="REJECT",
                       reject_reason="pos_side_required",
                       reject_detail=f"{symbol} 同时存在多空持仓，保护调整必须显式指定 pos_side")
    pos = matches[0] if matches else None
    fingerprint_error = _position_fingerprint_error(
        pos,
        expected_exists=expected_pre_position_exists,
        expected_sz=expected_pre_position_sz,
        expected_pos_id=expected_pre_position_pos_id,
        expected_c_time=expected_pre_position_c_time,
    )
    if fingerprint_error:
        return _finish_clean(receipt(
            False, action_taken="REJECT",
            reject_reason="pre_position_fingerprint_changed",
            reject_detail=(
                "执行时保护调整目标已不同于 plan/facts；未发送改单: "
                + fingerprint_error
            ),
            expected_pre_position={
                "exists": expected_pre_position_exists,
                "sz": expected_pre_position_sz,
                "posId": expected_pre_position_pos_id,
                "cTime": expected_pre_position_c_time,
            },
            actual_pre_position=pos,
        ), "pre_position_fingerprint_changed")
    if pos is None:
        return receipt(False, action_taken="REJECT",
                       reject_reason="no_position",
                       reject_detail=f"{symbol} 当前无 live 持仓，无保护单可调整")
    pos_side = str(pos.get("side") or "").lower()
    full_sz = _to_float(pos.get("sz"))
    if pos_side not in ("long", "short") or not full_sz or full_sz <= 0:
        return receipt(False, action_taken="REJECT",
                       reject_reason="position_unreadable",
                       reject_detail=f"持仓方向/数量不可读: side={pos_side} sz={full_sz}")

    try:
        mark_px = ox.get_mark_price(symbol, profile)
    except Exception as exc:
        mark_px = None
        print(f"[order_executor] WARN mark price 拉取异常 {symbol}: {exc}",
              file=sys.stderr)

    # ── 2. 确定性校验（不重算风险预算——主人 2026-08-13 拍板）───────────────
    errors = validate_protection_change(
        pos_side, mark_px, new_sl_trigger_px, new_tp_trigger_px) \
        if (new_sl_trigger_px is not None or new_tp_trigger_px is not None) \
        else []
    if errors:
        return receipt(False, action_taken="REJECT",
                       reject_reason="protection_change_invalid",
                       reject_detail="；".join(errors),
                       mark_px=mark_px)

    # ── 3. 现有保护单事实 ──────────────────────────────────────────────────
    existing = _live_protection_rows(symbol, pos_side, profile)
    original_tp_rows = [
        row for row in existing
        if row.get("tpTriggerPx") is not None and row["tpTriggerPx"] > 0
    ]
    sl_rows = [r for r in existing
               if r["slTriggerPx"] is not None and r["slTriggerPx"] > 0]
    stale_rows: list[dict[str, Any]] = []
    implied_sl: Optional[float] = None
    if len(sl_rows) > 1 and not consolidate_extra_sl:
        # 多张止损＝先前改单留下的残单；此状态下 amend 哪一张都无法保证终局唯一。
        _enqueue_repair(profile, symbol, None,
                        "adjust_protection_duplicate_sl_before_change", db_root)
        return receipt(False, action_taken="REJECT",
                       reject_reason="duplicate_sl_before_change",
                       reject_detail=f"改单前已存在 {len(sl_rows)} 张 live 止损，"
                                     "已入 repair_queue，需人工收敛后再调整",
                       existing_protection=sl_rows)
    if len(sl_rows) > 1:
        # 收敛：选「数量最大」的那张当幸存单（amend 到全仓的相对改动最小），
        # 同量则取最早挂的；排序键全确定性，同一现场必得同一选择。
        ordered = sorted(
            sl_rows,
            key=lambda r: (-(r["sz"] or 0.0), r["cTime"] or 0.0,
                           str(r["algoId"] or "")))
        stale_rows = ordered[1:]
        sl_rows = [ordered[0]]
        if new_sl_trigger_px is None:
            # 未显式给价时取**最保护**的一档（多头最高、空头最低），
            # 绝不因收敛而隐式放松保护；该价若已在现价错侧则退回幸存单原价。
            prices = [r["slTriggerPx"] for r in ordered]
            candidate = max(prices) if pos_side == "long" else min(prices)
            if not validate_protection_change(pos_side, mark_px, candidate, None):
                implied_sl = candidate
    target = sl_rows[0] if sl_rows else None
    target_sl = new_sl_trigger_px if new_sl_trigger_px is not None else (
        implied_sl if implied_sl is not None
        else (target["slTriggerPx"] if target else None))
    target_tp = new_tp_trigger_px if new_tp_trigger_px is not None else (
        target.get("tpTriggerPx") if target else None)
    force_oco_replace = new_tp_trigger_px is not None
    if target_sl is None:
        return receipt(False, action_taken="REJECT",
                       reject_reason="no_sl_to_preserve",
                       reject_detail="该仓当前无止损且本次未提供新止损价；"
                                     "本函数绝不产生无止损持仓")
    # 收敛必然以全仓为目标：只留一张单，它就必须覆盖整个仓位。
    want_sz = full_sz if (resize_to_full_position or target is None
                          or stale_rows) else None
    if target is not None and target.get("sz") is not None \
            and abs(target["sz"] - full_sz) > max(_EPS, full_sz * 1e-9) \
            and not resize_to_full_position:
        # 止损数量已与现仓不符（多半是加仓后没扩）：顺带纠正，不给「部分保护」留口子。
        want_sz = full_sz

    if ox.is_dryrun():
        return receipt(True, action_taken="ADJUST_PROTECTION", dryrun=True,
                       mark_px=mark_px, pos_side=pos_side, full_sz=full_sz,
                       existing_protection=sl_rows,
                       consolidate_from=stale_rows or None,
                       planned={"algoId": target["algoId"] if target else None,
                                "path": ("oco_replace" if force_oco_replace
                                         else "amend" if target
                                         else "place_new"),
                                "sl": target_sl, "tp": _to_float(target_tp),
                                "sz": want_sz,
                                "cancel_after": [
                                    r.get("algoId") for r in (
                                    existing if force_oco_replace
                                        else stale_rows)
                                ] or None})

    # 独立保护调整必须在第一次交易所写前固化 intent。
    # 执行成功而 writer 失败时，failure report 将看到该意图
    # 并 fail-closed，不得声称「exchange side effect = none」。
    if standalone_intent:
        if not ox.is_dryrun():
            deadline_reject = _cycle_side_effect_reject(cycle_id)
            if deadline_reject:
                return receipt(False, **deadline_reject)
        request = {
            "profile": "live",
            "cycle_id": cycle_id,
            "symbol": symbol,
            "action": "adjust_protection",
            "side": pos_side,
            "new_sl_trigger_px": _to_float(new_sl_trigger_px),
            "new_tp_trigger_px": _to_float(new_tp_trigger_px),
            "resize_to_full_position": bool(resize_to_full_position),
            "consolidate_extra_sl": bool(consolidate_extra_sl),
            "reason_code": reason_code,
            "target_sl": _to_float(target_sl),
            "target_tp": _to_float(target_tp),
            "target_sz": _to_float(want_sz or full_sz),
            "existing_algo_ids": sorted(
                str(row.get("algoId")) for row in existing
                if row.get("algoId") not in (None, "")
            ),
            "expected_pre_position_exists": expected_pre_position_exists,
            "expected_pre_position_sz": expected_pre_position_sz,
            "expected_pre_position_pos_id": expected_pre_position_pos_id,
            "expected_pre_position_c_time": expected_pre_position_c_time,
        }
        try:
            reserved = ei.reserve(
                intent_path, profile="live", cycle_id=str(cycle_id),
                symbol=symbol, side=str(pos_side),
                action="adjust_protection", request=request,
                now_ts=ledger.now_cst())
        except Exception as exc:
            return receipt(
                False, action_taken="REJECT", p0=True,
                reject_reason="execution_intent_store_failed",
                reject_detail=(
                    "保护调整幂等意图库不可用，未写交易所: "
                    f"{type(exc).__name__}: {exc}"))
        if reserved.get("status") == "replay":
            cached = dict(reserved["receipt"])
            cached["idempotent_replay"] = True
            cached["intent_state"] = "completed"
            return cached
        if reserved.get("status") != "reserved":
            blocker = reserved.get("blocking_intent") or {}
            _enqueue_repair(
                "live", symbol, reserved.get("ord_id"),
                "adjust_protection_execution_intent_blocked:"
                f"{reserved.get('reason') or reserved.get('state')}",
                db_root)
            return receipt(
                False, action_taken="REJECT", p0=True,
                reject_reason="execution_intent_blocked",
                reject_detail=(
                    "已有未决或冲突的保护调整意图，"
                    "拒绝重复写交易所"),
                intent_state=reserved.get("state"),
                intent_reason=reserved.get("reason"),
                blocking_intent=blocker or None)
        intent_fingerprint = str(reserved["fingerprint"])
        try:
            ei.mark_submitting(
                intent_path, error=None, **_intent_kwargs())
        except Exception as exc:
            return _finish_clean(receipt(
                False, action_taken="REJECT", p0=True,
                reject_reason="execution_intent_transition_failed",
                reject_detail=(
                    "交易所写前 intent 无法固化，已 fail-closed："
                    f"{type(exc).__name__}: {exc}")),
                "mark_submitting_failed")

    # intent 固化也可能在锁等待期间跨过截止点；在第一次交易所写前最后检查。
    # 内部保护收尾不在 standalone_intent 路径，不会因此被中断。
    if not ox.is_dryrun() and standalone_intent:
        deadline_reject = _cycle_side_effect_reject(cycle_id)
        if deadline_reject:
            return _finish_clean(
                receipt(False, **deadline_reject),
                str(deadline_reject["reject_reason"]),
            )

    # Reserve/intent persistence and protection discovery may span enough time
    # for the old position to close and a new one to reopen with the same
    # symbol/side.  Rebind the first amend/place to the expected position
    # identity immediately before any protection write.
    if (not ox.is_dryrun() and standalone_intent
            and expected_pre_position_exists is not None):
        try:
            latest_positions = fetch_open_positions(profile)
        except PositionsUnavailable as exc:
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="pre_write_positions_unavailable",
                reject_detail=(
                    "保护单写入紧前现仓 API 不可用；无法确认仍为同一仓位，"
                    f"已 fail-closed: {exc}"
                ),
            ), "pre_write_positions_unavailable")
        latest_matches = [
            row for row in latest_positions
            if row.get("symbol") == symbol
            and (requested_side is None
                 or str(row.get("side") or "").lower() == requested_side)
        ]
        latest_pos = latest_matches[0] if latest_matches else None
        late_fingerprint_error = _position_fingerprint_error(
            latest_pos,
            expected_exists=expected_pre_position_exists,
            expected_sz=expected_pre_position_sz,
            expected_pos_id=expected_pre_position_pos_id,
            expected_c_time=expected_pre_position_c_time,
        )
        if late_fingerprint_error:
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="pre_position_fingerprint_changed",
                reject_detail=(
                    "保护单写入紧前目标仓位已不同于 plan/facts；"
                    "未发送 amend/place: " + late_fingerprint_error
                ),
                expected_pre_position={
                    "exists": expected_pre_position_exists,
                    "sz": expected_pre_position_sz,
                    "posId": expected_pre_position_pos_id,
                    "cTime": expected_pre_position_c_time,
                },
                actual_pre_position=latest_pos,
            ), "pre_position_fingerprint_changed")

    # ── 4. 执行：amend 主路径 → 失败退「挂新→回读→撤旧」──────────────────
    path = None
    amend_result = None
    fallback_result = None
    new_algo_id = None
    # 收敛时幸存单的价可能来自被撤的那一档（取最保护者），必须显式写进 amend；
    # 非收敛路径维持原语义：没显式给价就只改数量，绝不「顺手」动触发价。
    amend_sl = new_sl_trigger_px
    if amend_sl is None and stale_rows and target_sl is not None:
        amend_sl = target_sl
    if force_oco_replace:
        # 明确设置/移动 TP 时不用普通 conditional amend 猜双腿语义：先挂一张
        # 全仓 OCO 并双腿回读，再撤全部旧保护。全程至少有一张已确认 SL。
        path = "oco_replace"
    elif target is not None and target.get("algoId"):
        path = "amend"
        intent_algo_id = str(target["algoId"])
        amend_result = ox.amend_algo_protection(
            symbol, target["algoId"], profile,
            new_sl_trigger_px=amend_sl,
            new_tp_trigger_px=_to_float(new_tp_trigger_px),
            new_sz=want_sz)
        if not amend_result.get("ok"):
            path = "replace_fallback"
            print(f"[order_executor] WARN amend 失败({amend_result.get('sMsg')})，"
                  f"退化为挂新→撤旧 {symbol}", file=sys.stderr)
        elif stale_rows:
            # 收敛路径：幸存单必须先被交易所确认已覆盖全仓，才允许撤多余单。
            # 顺序不可颠倒——先撤后确认就等于自己制造裸口窗。
            time.sleep(1.0)
            confirm_rows = [
                r for r in _live_protection_rows(symbol, pos_side, profile)
                if r["slTriggerPx"] is not None and r["slTriggerPx"] > 0]
            survivor_now = next(
                (r for r in confirm_rows
                 if r.get("algoId") == target.get("algoId")), None)
            expect_sz = want_sz or full_sz
            covered = bool(
                survivor_now
                and survivor_now.get("sz") is not None
                and abs(survivor_now["sz"] - expect_sz)
                <= max(_EPS, expect_sz * _PROTECTION_SIZE_REL_TOL)
                and abs(survivor_now["slTriggerPx"] - target_sl) / target_sl
                <= _PROTECTION_TOL_PCT)
            if not covered:
                # 幸存单没扩上去：多余单一张不撤，持仓仍是分档全覆盖（非裸仓），
                # 干净拒绝交人工。
                _enqueue_repair(
                    profile, symbol, None,
                    "adjust_protection_consolidate_survivor_unconfirmed",
                    db_root)
                return _finish_uncertain(receipt(
                    False, action_taken="REJECT",
                    reject_reason="consolidate_survivor_unconfirmed",
                    reject_detail="收敛时幸存止损单回读未达全仓，已停止撤单；"
                                  "持仓仍由多张分档止损全覆盖，需人工收敛",
                    protection_state={"rows": confirm_rows,
                                      "expected_sz": expect_sz,
                                      "expected_sl_px": target_sl},
                    consolidate_from=stale_rows, path="amend_consolidate"),
                    "consolidate_survivor_unconfirmed")
            _cancel_stale_protection(
                symbol, profile, stale_rows, db_root,
                note="adjust_protection_consolidate_cancel_failed")
            path = "amend_consolidate"
    else:
        path = "place_new"

    if path in ("place_new", "replace_fallback", "oco_replace"):
        if target_tp is not None:
            place = ox.place_algo_protection(
                symbol, pos_side, want_sz or full_sz, target_sl, profile,
                tp_trigger_px=_to_float(target_tp))
        else:
            place = ox.place_algo_sl(symbol, pos_side, want_sz or full_sz,
                                     target_sl, profile)
        fallback_result = place
        if not place.get("ok"):
            # 挂新失败：旧单（若有）仍在 → 持仓保护未被破坏，干净拒绝。
            still = assert_protection_state(
                symbol, pos_side, profile,
                expected_sl_px=(target["slTriggerPx"] if target else target_sl),
                expected_sz=(target.get("sz") or full_sz) if target else full_sz,
                retries=1)
            if still.get("naked"):
                _enqueue_repair(profile, symbol, None,
                                "adjust_protection_naked_after_failed_place",
                                db_root)
            failed = receipt(
                False, action_taken="REJECT",
                reject_reason="protection_place_failed",
                reject_detail=(
                    f"新止损挂单失败: "
                    f"{place.get('sMsg') or place.get('error')}"),
                p0=bool(still.get("naked")),
                protection_state=still, path=path)
            # 交易所明确拒绝，且回读证明原保护的价格/数量
            # 仍完整一致，才能证成 failed_clean；其余任何情况
            # 仍按「可能已写交易所」阻断。
            if still.get("ok") is True and not still.get("naked"):
                return _finish_clean(
                    failed, "protection_place_failed_old_state_verified")
            return _finish_uncertain(failed, "protection_place_failed")
        new_algo_id = None
        for row in place.get("data", []):
            if isinstance(row, dict) and row.get("algoId"):
                new_algo_id = str(row["algoId"])
                break
        if new_algo_id:
            intent_algo_id = new_algo_id
        verified = _verify_sl_placed(
            symbol, pos_side, profile, target_sl,
            expected_sz=want_sz or full_sz, since_ms=started_ms,
            expected_algo_id=new_algo_id)
        tp_verified = (
            _verify_tp_placed(
                symbol, pos_side, profile, _to_float(target_tp),
                expected_sz=want_sz or full_sz, since_ms=started_ms,
                expected_algo_id=new_algo_id,
            )
            if target_tp is not None else {"verified": True}
        )
        if not verified.get("verified") or not tp_verified.get("verified"):
            _enqueue_repair(profile, symbol, None,
                            "adjust_protection_new_leg_unverified", db_root)
            return _finish_uncertain(receipt(False, action_taken="REJECT",
                           reject_reason="new_protection_unverified",
                           reject_detail="新保护单的 SL/TP 回读未全部确认；旧单未撤，"
                                         "持仓仍受原止损保护",
                           verify={"sl": verified, "tp": tp_verified}, path=path),
                           "new_protection_unverified")
        # 新单已确认，才允许撤旧单——顺序不可颠倒（先撤后挂＝裸仓窗口）。
        # 收敛场景下「旧单」是幸存单 + 全部多余分档单，一并撤。
        doomed = (
            list(existing)
            if path == "oco_replace"
            else ([target] if target is not None else []) + stale_rows
        )
        if doomed:
            _cancel_stale_protection(
                symbol, profile, doomed, db_root,
                note="adjust_protection_stale_sl_not_cancelled")

    # ── 5. 后置断言：终局必须恰好一张全仓止损，且价＝期望 ──────────────────
    state = assert_protection_state(
        symbol, pos_side, profile,
        expected_sl_px=target_sl, expected_sz=want_sz or full_sz,
        expected_tp_px=_to_float(target_tp))
    if state.get("naked"):
        # 理论上不可达（挂新失败已提前返回）；真发生＝裸仓 P0，必须外显。
        _enqueue_repair(profile, symbol, None,
                        "adjust_protection_naked_after_change", db_root)
        return _finish_uncertain(receipt(False, action_taken="REJECT", p0=True,
                       reject_reason="naked_after_change",
                       reject_detail="改单后回读不到任何止损＝裸仓，已入 repair_queue",
                       protection_state=state, path=path),
                       "naked_after_change")
    if not state.get("ok"):
        _enqueue_repair(profile, symbol, None,
                        "adjust_protection_state_unconfirmed", db_root)
        return _finish_uncertain(receipt(False, action_taken="REJECT",
                       reject_reason="protection_state_unconfirmed",
                       reject_detail="改单后保护状态与期望不符（重复单或价格/数量不匹配），"
                                     "持仓仍有止损但需人工核对",
                       protection_state=state, path=path),
                       "protection_state_unconfirmed")
    stale_tp_cancelled: list[str] = []
    stale_tp_failed: list[str] = []
    if new_tp_trigger_px is not None:
        # 旧版本可能把 TP 与 SL 分成两张 conditional algo。新 OCO 已经在上面
        # 逐腿确认后，旧独立 TP 若仍 live 会在旧价提前退出，故此处再清理残单；
        # 顺序仍然不会制造裸仓窗口。
        applied_algo_id = str(
            new_algo_id or (target or {}).get("algoId") or "")
        active_ids = {
            str(row.get("algoId") or "")
            for row in _live_protection_rows(symbol, pos_side, profile)
        }
        stale_tp_rows = [
            row for row in original_tp_rows
            if str(row.get("algoId") or "")
            and str(row.get("algoId")) != applied_algo_id
            and str(row.get("algoId")) in active_ids
        ]
        if stale_tp_rows:
            stale_tp_cancelled = [
                str(row.get("algoId")) for row in stale_tp_rows
            ]
            stale_tp_failed = _cancel_stale_protection(
                symbol, profile, stale_tp_rows, db_root,
                note="adjust_protection_stale_tp_not_cancelled")
    return _finish_completed(receipt(
                   True, action_taken="ADJUST_PROTECTION", path=path,
                   mark_px=mark_px, pos_side=pos_side, full_sz=full_sz,
                   protection_state=state,
                   applied={"sl": target_sl, "tp": _to_float(target_tp),
                            "sz": want_sz or full_sz,
                            "algoId": new_algo_id or (target or {}).get("algoId")},
                   consolidated_from=([r.get("algoId") for r in stale_rows]
                                      or None),
                   stale_tp_cancelled=stale_tp_cancelled or None,
                   stale_tp_failed=stale_tp_failed or None,
                   protection_warning=(
                       "stale_tp_cancel_failed" if stale_tp_failed else None),
                   previous={"sl": (target or {}).get("slTriggerPx"),
                             "sz": (target or {}).get("sz"),
                             "algoId": (target or {}).get("algoId")}))
