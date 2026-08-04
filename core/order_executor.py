# -*- coding: utf-8 -*-
"""V2.0 §7 契约 B —— 确定性下单/止损/平仓/回读（live + Demo 分 profile 定仓）。

live 下单**唯一路径**：order_executor.open_position()，其内部**强制调** risk_validator.validate()
（LLM 物理越不过闸）。本模块把 live_trader.md §4/§8 现为「LLM 手拼 okx 命令 + 手挂止损」的
下单层搬成确定性代码，回执仍喂 trades_writer 落库（writer 不变）。

核心不变量（方案 §7）：
  - **OPEN**：装配现场 → 强制 risk_validator → 市价开仓**即附挂 SL**（原子无裸仓窗口）
    → 附挂失败则独立 algo SL（重试1）→ 仍失败立即市价平掉刚开仓（不留裸实盘仓）
    → 回读 fills 求真实成交（拉不到 → repair_queue + reject + P0）。
  - **CLOSE**（2026-07-03 主路径反转）：OKX API 现仓确认 posSide → reduceOnly 市价单
    （拿 ordId 即时确认；绝不翻反向仓）主路径 → 被拒转 swap close 兜底 → 51087 下架/
    51001 不存在明确拒因 → fills→订单状态双源确认求真 pnl，两端点均无 →
    unconfirmed(pnl=NULL)+repair_queue。
  - Demo：不使用 live 的 1%/20%/98% 仓位比例；公共安全预检后，新开先确认目标
    杠杆，再按 symbol/side/tdMode 实时读取 OKX Demo max-size。查询失败不下单。

现仓/equity 权威一律 OKX API（禁 position_snapshots GROUP BY，红线 #6）。
ctVal/lotSz：live=market.db.instruments_cache → 缺/stale 现拉 → 仍缺 reject（不拿默认 1.0 蒙）；
Demo=带 x-simulated-trading 头现拉 Demo 环境 ctVal/lotSz/minSz（缓存只存 live 口径）；
失败即拒绝，禁止回退 live 规格。
零模型名（红线 #1）。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_CORE = os.path.dirname(os.path.abspath(__file__))
_CORE_LIB = os.path.join(_CORE, "lib")
_COLLECTORS = os.environ.get("OKX_COLLECTORS_DIR", _project_path('collectors'))
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
from decision_card import validate_card  # noqa: E402

DEFAULT_DB_ROOT = Path(os.environ.get("OKX_DB_ROOT", _project_path('db')))
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
            "notional": _to_float(item.get("notionalUsd")),
            "lev": _to_float(item.get("lever")),
            "avgPx": _to_float(item.get("avgPx") or item.get("markPx")),
        })
    return out


_DEMO_SPEC_MEMO: dict[str, dict[str, Any]] = {}  # symbol → demo 现拉规格（进程内 memo）
_OKX_PUBLIC_INSTRUMENTS = "https://www.okx.com/api/v5/public/instruments"


def _fetch_demo_instrument(symbol: str) -> Optional[dict[str, Any]]:
    """现拉 demo 环境 instrument 元数据。

    CLI `market instruments` 的 demo profile 不保证带 x-simulated-trading 头；demo 环境
    同名合约规格可能与 live 不同，因此必须带头直连 public 端点（无需凭证）。
    任何失败返 None；Demo 调用方必须 fail-closed，禁止回退 live 规格。"""
    try:
        import httpx  # vendored <PROJECT_ROOT>\Lib\site-packages（wrapper 注入 PYTHONPATH）
        r = httpx.get(_OKX_PUBLIC_INSTRUMENTS,
                      params={"instType": "SWAP", "instId": symbol},
                      headers={"x-simulated-trading": "1"}, timeout=20)
        for row in (r.json().get("data") or []):
            if isinstance(row, dict) and row.get("instId") == symbol:
                return row
    except Exception as exc:
        print(f"[order_executor] WARN demo instruments HTTP fetch failed for "
              f"{symbol}: {exc}", file=sys.stderr)
    return None


def fetch_instrument_specs(symbol: str, profile: str,
                           db_root: Path = DEFAULT_DB_ROOT) -> dict[str, Any]:
    """ctVal/lotSz/minSz。live：instruments_cache（market.db）优先 → 缺则现拉。

    demo：instruments_cache 只存 live 口径，demo 同名合约规格可能不同；跳过缓存主路径，
    带 x-simulated-trading 头现拉 Demo 环境规格（进程内 memo）；现拉失败直接返回空规格，
    由开仓闸拒绝，禁止用 live 元数据替代。"""
    ct_val = lot_sz = min_sz = None
    src = None
    db_root = Path(db_root)  # 调用方（trader agent）常传 str 路径——强制 Path，避免 str/str TypeError
    is_demo = str(profile).lower() == "demo" or "demo" in str(profile).lower()
    if is_demo:
        memo = _DEMO_SPEC_MEMO.get(symbol)
        if memo:
            return dict(memo)
        inst = _fetch_demo_instrument(symbol)
        if inst:
            ct_val = _to_float(inst.get("ctVal"))
            lot_sz = _to_float(inst.get("lotSz"))
            min_sz = _to_float(inst.get("minSz"))
        if (ct_val is not None and lot_sz is not None and min_sz is not None
                and ct_val > 0 and lot_sz > 0 and min_sz > 0):
            out = {"ct_val": ct_val, "lot_sz": lot_sz, "min_sz": min_sz,
                   "source": "demo_fetch", "spec_source": "demo_fetch"}
            _DEMO_SPEC_MEMO[symbol] = dict(out)
            return out
        print(
            f"[order_executor] WARN demo instrument fetch/spec invalid for {symbol}; "
            "fail-closed without live fallback",
            file=sys.stderr,
        )
        return {
            "ct_val": ct_val, "lot_sz": lot_sz, "min_sz": min_sz,
            "source": "demo_fetch_failed",
            "spec_source": "demo_fetch_failed",
        }
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
    profile_label = (
        "demo" if str(profile).lower() == "demo"
        or "demo" in str(profile).lower() else "live")
    return Path(db_root) / f"{profile_label}_trades.db"


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
        "profile": (
            "demo" if str(profile).lower() == "demo"
            or "demo" in str(profile).lower() else "live"),
        "ledger_groups": len(ledger_positions),
        "exchange_groups": len(exchange_positions),
        "diffs": diffs,
    }


def _position_size(positions: list[dict[str, Any]], symbol: str, side: str) -> float:
    for p in positions or []:
        if p.get("symbol") == symbol and str(p.get("side", "")).lower() == side:
            return _to_float(p.get("sz")) or 0.0
    return 0.0


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


_SCRIPTS_DIR = os.environ.get("OKX_SCRIPTS_DIR", _project_path('scripts'))
_AUTOHEAL_TIMEOUT_SEC = 180


def _try_autoheal_ledger(profile: str, db_root, cycle_id: Optional[str]) -> bool:
    """插入点 B：pretrade 账仓不一致时，拒单前给一次确定性自愈机会（2026-08-04）。

    Live 永久只读；即使设置 `OKX_LEDGER_AUTOHEAL_APPLY=1` 或
    `OKX_LEDGER_AUTOHEAL_UNRECORDED=1`，本层也不会向 Live 子进程传写参数。
    两个开关只允许 Demo 账本自愈。EXACT 判定、单轮上限、方向限制和幂等等
    闸门都在 `scripts/ledger_autoheal.py` 内，本层不复制规则。

    **fail-closed 语义不变**：本函数只可能让「账本修好后本就该通过」的校验重新通过，
    绝不会让校验不通过的单放行——调用方在自愈后必须重跑
    `_verify_pretrade_ledger_positions`，仍不 ok 照旧拒单。

    子进程隔离 + 全异常吞掉：自愈崩溃/超时一律返回 False，退回原有拒单路径。
    `--self-cycle` 让本轮自己的 running runner 不被误判为互斥冲突。
    环境变量 `OKX_DISABLE_LEDGER_AUTOHEAL=1` 可一键关掉本层。
    """
    if os.environ.get("OKX_DISABLE_LEDGER_AUTOHEAL") == "1":
        return False
    try:
        import subprocess  # 局部 import：仅此罕见分支需要，不拖累安全层常态路径

        heal_py = os.path.join(_SCRIPTS_DIR, "ledger_autoheal.py")
        if not os.path.exists(heal_py):
            return False
        apply_enabled = (
            str(profile) == "demo"
            and os.environ.get("OKX_LEDGER_AUTOHEAL_APPLY") == "1"
        )
        unrecorded_enabled = (
            apply_enabled
            and os.environ.get("OKX_LEDGER_AUTOHEAL_UNRECORDED") == "1"
        )
        cmd = [sys.executable, heal_py, "--profile", str(profile),
               "--db-root", str(db_root)]
        if apply_enabled:
            cmd.append("--apply")
        if unrecorded_enabled:
            cmd.append("--enable-unrecorded")
        if cycle_id:
            cmd += ["--self-cycle", str(cycle_id)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=_AUTOHEAL_TIMEOUT_SEC)
        data = json.loads(proc.stdout or "{}")
        # Live classification can never repair the ledger in public code. Keep
        # the caller fail-closed even if stale subprocess output claims a write.
        if str(profile) == "live":
            return False
        return any(h.get("applied") for h in data.get("healed", []))
    except Exception:  # noqa: BLE001  自愈失败绝不影响拒单主路径
        return False


def _verify_sl_placed(
    symbol: str,
    pos_side: str,
    profile: str,
    sl_trigger_px: Optional[float] = None,
    tol_pct: float = 0.001,
    retries: int = 2,
    *,
    expected_sz: Optional[float] = None,
    since_ms: Optional[float] = None,
    expected_algo_id: Optional[str] = None,
    expected_ord_id: Optional[str] = None,
) -> dict[str, Any]:
    """回读 pending algo，确认是“本次、同侧、足量、有效”的保护性止损。

    附挂 SL 没有调用方已知的 algoId，因此必须以 cTime>=本次下单时刻识别，旧的
    同价单不能通过。独立 algo 必须精确匹配刚返回的 algoId；即使个别展示字段缺失，
    这个强身份仍可兼容，但只要字段存在就必须与本次请求一致。
    """
    expected_sl = _to_float(sl_trigger_px)
    if (expected_sl is None or not math.isfinite(expected_sl)
            or expected_sl <= 0):
        return {"verified": False, "found": [],
                "error": "invalid_sl_trigger_px"}
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
            slpx = _to_float(a.get("slTriggerPx"))
            if slpx is None or not math.isfinite(slpx) or slpx <= 0:
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

            if abs(slpx - expected_sl) / expected_sl > tolerance:
                errors.append("slTriggerPx")

            linked = a.get("linkedOrd")
            linked_ord_id = ""
            if isinstance(linked, dict):
                linked_ord_id = str(linked.get("ordId") or "")
            if expected_order and linked_ord_id and linked_ord_id != expected_order:
                errors.append("linkedOrd")

            summary = {
                "algoId": algo_id or None,
                "slTriggerPx": slpx,
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


# ---------------------------------------------------------------------------
# OPEN
# ---------------------------------------------------------------------------
def validate_receipt_context(
    context: Optional[dict[str, Any]],
    *,
    cycle_id: Optional[str] = None,
    required: bool = True,
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
) -> dict[str, Any]:
    is_demo = str(profile).lower() == "demo" or "demo" in str(profile).lower()
    profile_label = "demo" if is_demo else "live"
    side = str(side or "").lower()
    action_taken = "OPEN_LONG" if side == "long" else "OPEN_SHORT"
    capacity_audit: Optional[dict[str, Any]] = None
    position_reconciliation_audit: Optional[dict[str, Any]] = None
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
                       reject_detail="开仓必须提供止损价（§4 红线，live/demo 一致）")
    normalized_sl = _to_float(sl_trigger_px)
    if (normalized_sl is None or not math.isfinite(normalized_sl)
            or normalized_sl <= 0):
        return receipt(False, action_taken="REJECT", reject_reason="bad_sl",
                       reject_detail=f"止损价非法: {sl_trigger_px}")
    sl_trigger_px = normalized_sl
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
        receipt_context, cycle_id=cycle_id, required=not ox.is_dryrun())
    if ctx_errors:
        return receipt(
            False, action_taken="REJECT",
            reject_reason="receipt_context_invalid",
            reject_detail="；".join(ctx_errors))
    if not ox.is_dryrun() and not cycle_id:
        return receipt(
            False, action_taken="REJECT", reject_reason="cycle_id_required",
            reject_detail="非 dry-run 开仓必须提供调度 cycle_id")

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
            "mgn_mode": mgn_mode,
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

    # ── 装配现场：硬闸输入一律以 OKX API 为权威，禁 caller 注入绕闸 ──
    # 非 dryrun 一律以 API 真值为准，caller 传值仅留作偏差留痕。
    # 现仓 API 失败 = 敞口未知 → 拒单，不当零仓。
    input_divergence: list[str] = []
    if not ox.is_dryrun():
        caller_equity = equity
        caller_available_margin = available_margin
        caller_account_imr = account_imr
        if is_demo:
            # Demo 容量不读取 balance/totalEq/availBal；这些字段与 live 的抵押品、
            # 账户模式口径不同。真正容量在公共安全预检及（必要时）set_leverage 后，
            # 按 symbol/side/tdMode 从 account max-size 实时取得。
            equity = None
            available_margin = None
            account_imr = None
            if caller_equity is not None:
                input_divergence.append(
                    f"demo equity caller={caller_equity} ignored_for_sizing")
            if caller_available_margin is not None:
                input_divergence.append(
                    "demo available_margin "
                    f"caller={caller_available_margin} ignored_for_sizing")
            if caller_account_imr is not None:
                input_divergence.append(
                    "demo account_imr "
                    f"caller={caller_account_imr} ignored_for_sizing")
        else:
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
        if not position_check["ok"] and _try_autoheal_ledger(
                profile_label, db_root, cycle_id):
            # 自愈写了 close 行 → 用同一份 api_positions 重新校验（不重复打 API）。
            # 仍不 ok 就落回下面原有的 fail-closed 拒单路径，语义不变。
            try:
                position_check = _verify_pretrade_ledger_positions(
                    profile_label, db_root, api_positions)
                position_reconciliation_audit = position_check
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
        # 缺项时由同一次 balance 补齐；Demo 即使 dryrun 也不得把 balance 或
        # account_imr 当成容量，后续仍走 account max-size 只读查询。
        if is_demo:
            available_margin = None
            account_imr = None
        elif (equity is None or available_margin is None
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
        if not is_demo:
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
    specs = fetch_instrument_specs(symbol, profile, db_root)
    ct_val = specs.get("ct_val")
    lot_sz = specs.get("lot_sz")
    min_sz = specs.get("min_sz")
    new_open = not any(
        (p.get("symbol") == symbol and str(p.get("side", "")).lower() == side)
        for p in open_positions)
    lev_warn = None

    if is_demo:
        # 第一阶段只校公共安全与交易所规格。必须先于 set_leverage，防止一个本应
        # 被拒绝的请求也修改 Demo 账户杠杆配置。
        preflight = rv.validate(
            symbol=symbol, side=side, intended_sz=intended_sz, lev=lev,
            mark_px=mark_px, ct_val=ct_val, lot_sz=lot_sz, equity=equity,
            open_positions=open_positions, sl_trigger_px=sl_trigger_px,
            profile="demo", available_margin=None,
            min_order_size=min_sz, preflight_only=True,
        )
        if not preflight["approved"]:
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason=preflight["reject_reason"],
                reject_detail=preflight["reject_detail"],
                risk=preflight,
            ), f"risk_reject:{preflight['reject_reason']}")

        # OKX CLI 当前 max-size 不透传 leverage，接口会使用账户当前杠杆。
        # 新开必须先成功设置目标杠杆；加仓则不改，沿用 API 现仓杠杆。
        if new_open:
            lr = ox.set_leverage(
                symbol, lev, mgn_mode, profile,
                pos_side=side if mgn_mode == "isolated" else None,
            )
            if not lr.get("ok"):
                return _finish_clean(receipt(
                    False, action_taken="REJECT",
                    reject_reason="set_leverage_failed",
                    reject_detail=str(lr.get("sMsg") or lr.get("error")),
                    risk=preflight,
                ), "set_leverage_failed")

        try:
            max_size_payload = ox.get_max_size(symbol, mgn_mode, profile)
        except Exception as exc:
            max_size_payload = {"ok": False, "error": str(exc)}
        directional_capacity = ac.extract_directional_max_size(
            max_size_payload, symbol, side)
        exchange_max_size = _to_float(
            directional_capacity.get("max_size"))
        capacity_audit = {
            "source": directional_capacity.get("source"),
            "inst_id": symbol,
            "side": side,
            "td_mode": mgn_mode,
            "direction_field": directional_capacity.get("direction_field"),
            "direction_value": directional_capacity.get("direction_value"),
            "max_size": exchange_max_size,
            "queried_after_target_leverage": bool(new_open),
            "error": directional_capacity.get("error"),
        }
        if (not directional_capacity.get("ok")
                or exchange_max_size is None
                or not math.isfinite(exchange_max_size)
                or exchange_max_size < 0):
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="demo_max_size_fetch_failed",
                reject_detail=(
                    "OKX Demo 实时最大可开张数不可用，拒开；"
                    "禁止回退 balance/totalEq/max-avail-size/live 公式: "
                    f"{directional_capacity.get('error')}"),
                risk=preflight, p0=True,
            ), "demo_max_size_fetch_failed")

        # 第二阶段只按交易所 minSz/lotSz 与本次方向 max-size 定仓。
        v = rv.validate(
            symbol=symbol, side=side, intended_sz=intended_sz, lev=lev,
            mark_px=mark_px, ct_val=ct_val, lot_sz=lot_sz, equity=equity,
            open_positions=open_positions, sl_trigger_px=sl_trigger_px,
            profile="demo", available_margin=None,
            exchange_max_size=exchange_max_size,
            min_order_size=min_sz,
        )
    else:
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

    # live 仍在完整预算闸通过后设杠杆；Demo 已在 max-size 查询前完成。
    if not is_demo and new_open:
        lr = ox.set_leverage(symbol, lev, mgn_mode, profile)
        if not lr.get("ok"):
            return _finish_clean(receipt(
                False, action_taken="REJECT",
                reject_reason="set_leverage_failed",
                reject_detail=str(lr.get("sMsg") or lr.get("error")),
                risk=v,
            ), "set_leverage_failed")

    # ── 市价开仓（附挂 SL，原子）──
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
            sl_trigger_px=sl_trigger_px)
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
                        algo_id0: Optional[str]) -> dict[str, Any]:
        accounting = _open_fill_accounting(
            fa0, approved_sz=approved_sz, mark_px=mark_px,
            ct_val=ct_val, effective_lev=effective_lev,
            dryrun=ox.is_dryrun())
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
            "channel": "demo" if is_demo else "live",
            "reason": reasoning, "open_id": ord_id, "sl_trigger_px": sl_trigger_px,
            "algo_id": algo_id0, "sl_mode": sl_mode0,
            "sl_verified": sl_verified0, "fill_source": fill_source0,
            "fill_ts": fa0.get("fill_ts"),
            "ts_source": fa0.get("ts_source"),
            # 回执带本环境真实 ct_val，writer 补算优先用行内值。
            "ct_val": ct_val, "ordId": ord_id,
        }

    def journal_open_once(trade0: dict[str, Any], *, unwind: bool = False,
                          journal_action: Optional[str] = None) -> None:
        nonlocal open_fill_journaled
        if open_fill_journaled:
            return
        _journal_fill("demo" if is_demo else "live", trade0, db_root, cycle_id,
                      journal_action or action_taken, unwind=unwind)
        open_fill_journaled = True

    # ── 止损保障（sl_mode 如实标注）──
    #   attached：随主单附挂，`sl_attached` 只表示带参下单成功；必须回读确认真挂上；
    #   algo：独立 reduceOnly algo，返回 algoId 后仍须 pending 回读通过才算 verified。
    #   超时恢复路径 attached 状态未知 → 不采信附挂，强制走独立 algo 补挂（belt）。
    algo_id = None
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
                    fa_unwind, source_unwind, "none", False, None)
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

    # 从这里开始已是权威确认的成交，立即落一次 journal；后面只组回执，
    # 不再进入任何 SL I/O，故 SL 回读/补挂异常无法跳过此留痕。
    trade = make_open_trade(fa, fill_source, sl_mode, sl_verified, algo_id)
    journal_open_once(trade)
    return _finish_completed(receipt(True, trades=[trade], risk=v, ord_id=ord_id,
                   clamped=v.get("clamped"), adjustments=v.get("adjustments"),
                   lev_warn=lev_warn,
                   sl_mode=sl_mode, sl_verified=sl_verified,
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
) -> dict[str, Any]:
    is_demo = str(profile).lower() == "demo" or "demo" in str(profile).lower()
    resolved_side = pos_side

    def receipt(ok: bool, **kw) -> dict[str, Any]:
        # 与 OPEN 相同：执行前完整验证，执行后只原样携带决策上下文，
        # 禁止 Agent 在成交后再手工拼 status/protocol/card/cycle。
        base = dict(receipt_context or {})
        base.update({"profile": "demo" if is_demo else "live", "ok": ok,
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
        "channel": "demo" if is_demo else "live", "reason": reasoning,
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
    _journal_fill("demo" if is_demo else "live", trade, db_root, cycle_id,
                  "UNWIND_CLOSE" if _unwind else "CLOSE", unwind=_unwind)
    return receipt(True, action_taken="CLOSE", trades=[trade],
                   reduce_only_fallback=used_reduce_only,
                   fills_ok=confirmed_fill,
                   fill_source=fill_source)


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
        fills_file = _project_path('tmp', f'repair_{profile}_{symbol}_fills.json')
        wrapper = "'" + _project_path(
            "scripts", "run_okx_python.ps1").replace("'", "''") + "'"
        cli = "'" + _project_path(
            "scripts", "_okxcli.py").replace("'", "''") + "'"
        fix = (
            f"pwsh -NoProfile -File {wrapper} {cli} "
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
