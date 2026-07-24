# -*- coding: utf-8 -*-
"""V2.0 §7 契约 B —— 确定性下单/止损/平仓/回读（live + demo 同代码路径 + 同硬上限）。

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
  - demo：与 live 使用同一套 risk_validator 硬上限；只差 OKX profile / 执行环境。

现仓/equity 权威一律 OKX API（禁 position_snapshots GROUP BY，红线 #6）。
ctVal/lotSz：live=market.db.instruments_cache → 缺/stale 现拉 → 仍缺 reject（不拿默认 1.0 蒙）；
demo=带 x-simulated-trading 头现拉 demo 环境规格（F1 2026-07-06，缓存只存 live 口径、
demo 同名合约可 100x 不同）→ 失败回退 live 口径带 spec_source 留痕。
零模型名（红线 #1）。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import json
import math
import os
import sys
import time
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

DEFAULT_DB_ROOT = Path(os.environ.get("OKX_DB_ROOT", _project_path('db')))
FILLS_RETRY = 3
FILLS_RETRY_WAIT = 1.5
# 2026-07-03：订单状态第二权威源（demo fills 端点延迟 6-52s，订单状态端点即时）。
# 常数勿再上调——order_executor 在 trader 会话 exec 里跑，sleep 也占 gateway 宿主时长。
ORDER_CONFIRM_RETRY = 3
ORDER_CONFIRM_WAIT = 1.0
_EPS = 1e-9
_FILL_TS_SKEW_MS = 60000  # 本地/交易所时钟偏差容差（fills 时间窗；60s 容 RDP/云主机时钟漂移）


class PositionsUnavailable(RuntimeError):
    """OKX 现仓 API 失败 —— 敞口未知，禁按「零仓」放行（S2a fail-safe，2026-07-02）。"""


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
    任何失败返 None（caller 回退 live 缓存并带标，禁静默错尺度）。"""
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
    """ctVal/lotSz。live：instruments_cache（market.db）优先 → 缺则现拉 → 仍缺返回 None。

    demo：instruments_cache 只存 live 口径，demo 同名合约规格可能不同；跳过缓存主路径，
    带 x-simulated-trading 头现拉 demo
    环境规格（进程内 memo）；现拉失败才回退 live 缓存/现拉，返回带
    spec_source='live_cache_fallback'/'live_fetch_fallback' 留痕（不再静默错尺度）。"""
    ct_val = lot_sz = None
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
        if ct_val is not None and lot_sz is not None and ct_val > 0 and lot_sz > 0:
            out = {"ct_val": ct_val, "lot_sz": lot_sz,
                   "source": "demo_fetch", "spec_source": "demo_fetch"}
            _DEMO_SPEC_MEMO[symbol] = dict(out)
            return out
        # demo 现拉失败 → 回退 live 口径（规格可能与 demo 环境不同，带标留痕，禁静默错尺度）
        print(f"[order_executor] WARN demo instrument fetch failed for {symbol}, "
              "falling back to live specs (may be wrong-scaled)", file=sys.stderr)
        ct_val = lot_sz = None
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
                src = "cache"
        except Exception:
            pass
    if ct_val is None or lot_sz is None or ct_val <= 0 or lot_sz <= 0:
        inst = ox.get_instrument(symbol, profile)
        if inst:
            ct_val = _to_float(inst.get("ctVal"))
            lot_sz = _to_float(inst.get("lotSz"))
            src = "live_fetch"
    if is_demo and src:
        # demo 落到 live 口径（缓存/CLI 现拉均为 live 元数据）——尺度可能失真，回执留痕
        src = {"cache": "live_cache_fallback",
               "live_fetch": "live_fetch_fallback"}.get(src, src)
    return {"ct_val": ct_val, "lot_sz": lot_sz, "source": src, "spec_source": src}


def _avg_fill(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合 fills → 加权均价 fill_px / 总 fillSz / 总 pnl。"""
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
    return {"fill_px": fill_px, "fill_sz": tot_sz, "pnl": tot_pnl, "n": len(fills)}


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
                "n": 0, "dryrun": True}

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
        # 时间窗滤空不再第一拍即 approx——demo fills 端点可能延迟，
        # 历史成交会污染 fill_px。继续重试等新 fill
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
    return {"ok": False, "fill_px": None, "fill_sz": None, "pnl": None, "n": 0,
            "approx_agg": approx_agg}


def _fill_from_order(o: dict[str, Any]) -> Optional[dict[str, Any]]:
    """订单状态行 → 合成 fill 聚合（CLI 字段全字符串，显式转 float）。

    accFillSz>0 即视为有成交——含 canceled 部分成交（部分成交后撤销不得落
    fills_missing 造幽灵 reject，标 partial）。accFillSz<=0 返 None。"""
    acc = _to_float(o.get("accFillSz")) or 0.0
    if acc <= 0:
        return None
    return {"ok": True, "fill_px": _to_float(o.get("avgPx")), "fill_sz": acc,
            "pnl": _to_float(o.get("pnl")) or 0.0, "n": 1,
            "source": "order_status",
            "partial": str(o.get("state")) == "canceled"}


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

    `swap close` 的平仓单以独立 ordId+reduceOnly=true 出现在 orders-history，
    avgPx/pnl 字段用于与聚合近似值区分。
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
            return {"ok": True,
                    "fill_px": (tot_quote / tot_sz) if tot_sz > 0 else None,
                    "fill_sz": tot_sz, "pnl": tot_pnl, "n": len(hits),
                    "source": "orders_history"}
        if attempt < ORDER_CONFIRM_RETRY - 1:
            time.sleep(ORDER_CONFIRM_WAIT)
    return None


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


def _verify_sl_placed(symbol: str, pos_side: str, profile: str,
                      sl_trigger_px: Optional[float] = None,
                      tol_pct: float = 0.03, retries: int = 2) -> dict[str, Any]:
    """#5（2026-07-07）：开仓后回读确认 SL 真挂上——`okx swap algo orders --instId` 列 pending
    algo（附挂 SL 以仓位关联条件单出现），找 slTriggerPx 非空的单（传 sl_trigger_px 则还需 ±tol
    匹配）。带重试吸收下单后 algo 短暂延迟，降误判致重复挂 belt SL 的风险。
    返回 {verified, found}。verified=True ⟺ 找到匹配的 pending SL algo。

    OKX/调用方的价格字段常是字符串。比较前必须先归一化；非数值、非有限值、
    <=0 均按「未验证」安全失败，不让类型异常穿透中断后续 belt SL/unwind。"""
    expected_sl = None
    if sl_trigger_px is not None:
        expected_sl = _to_float(sl_trigger_px)
        if (expected_sl is None or not math.isfinite(expected_sl)
                or expected_sl <= 0):
            return {"verified": False, "found": [],
                    "error": "invalid_sl_trigger_px"}
    tolerance = _to_float(tol_pct)
    if tolerance is None or not math.isfinite(tolerance) or tolerance < 0:
        return {"verified": False, "found": [], "error": "invalid_tolerance"}
    try:
        retry_count = max(1, int(retries))
    except (TypeError, ValueError, OverflowError):
        return {"verified": False, "found": [], "error": "invalid_retries"}
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
            slpx = a.get("slTriggerPx")
            if slpx in (None, "", "0", 0):
                continue
            slpx_f = _to_float(slpx)
            if slpx_f is None or not math.isfinite(slpx_f) or slpx_f <= 0:
                continue
            found.append({"algoId": a.get("algoId"), "slTriggerPx": slpx_f,
                          "side": str(a.get("side", "")).lower(), "state": a.get("state")})
        matched = found
        if expected_sl is not None and found:
            matched = [f for f in found
                       if abs(f["slTriggerPx"] - expected_sl) / expected_sl <= tolerance]
        if matched:
            return {"verified": True, "found": found}
        if attempt < retry_count - 1:
            time.sleep(1.0)
    return {"verified": False, "found": found}


# ---------------------------------------------------------------------------
# OPEN
# ---------------------------------------------------------------------------
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
) -> dict[str, Any]:
    is_demo = str(profile).lower() == "demo" or "demo" in str(profile).lower()
    side = str(side or "").lower()
    action_taken = "OPEN_LONG" if side == "long" else "OPEN_SHORT"
    capacity_audit: Optional[dict[str, Any]] = None

    def receipt(ok: bool, **kw) -> dict[str, Any]:
        base = {"profile": "demo" if is_demo else "live", "ok": ok,
                "action_taken": kw.pop("action_taken", action_taken),
                "symbol": symbol, "side": side, "trades": kw.pop("trades", []),
                "p0": kw.pop("p0", False)}
        if capacity_audit is not None:
            base["capacity"] = dict(capacity_audit)
        base.update(kw)
        return base

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

    # ── 装配现场：硬闸输入一律以 OKX API 为权威，禁 caller 注入绕闸 ──
    # 非 dryrun 一律以 API 真值为准，caller 传值仅留作偏差留痕。
    # 现仓 API 失败 = 敞口未知 → 拒单，不当零仓。
    input_divergence: list[str] = []
    if not ox.is_dryrun():
        # 余额只拉一次：totalEq 是全账户折 USD 权益，不等于 USDT-SWAP
        # 可用结算币保证金。必须同时从 details.USDT 取 availBal/availEq；
        # 缺失禁回退 caller/totalEq，否则会对交易所提出必然资金不足的大单。
        caller_equity = equity
        caller_available_margin = available_margin
        try:
            balance_payload = ox.get_balance(profile)
        except Exception as exc:
            balance_payload = {"ok": False, "error": str(exc)}
        capacity = ac.extract_settlement_capacity(balance_payload, "USDT")
        api_equity = _to_float(capacity.get("total_equity"))
        api_available_margin = _to_float(capacity.get("available_margin"))
        capacity_audit = {
            "settlement_ccy": capacity.get("settlement_ccy", "USDT"),
            "source": capacity.get("source"),
            "available_margin": api_available_margin,
            "frozen_balance": capacity.get("frozen_balance"),
            "error": capacity.get("error"),
        }
        if (not capacity.get("ok") or api_available_margin is None
                or not math.isfinite(api_available_margin)
                or api_available_margin < 0):
            return receipt(False, action_taken="REJECT",
                           reject_reason="available_margin_fetch_failed",
                           reject_detail="USDT 可用保证金不可用，拒开（禁回退 totalEq/caller）: "
                                         f"{capacity.get('error')}", p0=True)
        if api_equity is None or not math.isfinite(api_equity) or api_equity <= 0:
            return receipt(False, action_taken="REJECT",
                           reject_reason="equity_fetch_failed",
                           reject_detail="totalEq 非法，拒开（禁回退 caller）", p0=True)
        if (caller_equity is not None
                and abs((_to_float(caller_equity) or 0.0) - api_equity) > 1.0):
            input_divergence.append(f"equity caller={caller_equity} → API={api_equity}")
        if (caller_available_margin is not None
                and abs((_to_float(caller_available_margin) or 0.0)
                        - api_available_margin) > 1.0):
            input_divergence.append(
                f"available_margin caller={caller_available_margin} → API={api_available_margin}")
        equity = api_equity
        available_margin = api_available_margin
        try:
            api_positions = fetch_open_positions(profile)
        except PositionsUnavailable as exc:
            return receipt(False, action_taken="REJECT",
                           reject_reason="positions_fetch_failed",
                           reject_detail=f"现仓 API 失败，拒开（不当零仓放行）: {exc}", p0=True)
        if open_positions is not None and len(open_positions) != len(api_positions):
            input_divergence.append(
                f"positions caller={len(open_positions)} → API={len(api_positions)}")
        open_positions = api_positions
        # mark_px：API 失败一律拒（同 fail-safe，防注入架空价影响 sz/notional/SL 偏离校验）
        api_mark = ox.get_mark_price(symbol, profile)
        if api_mark is None:
            return receipt(False, action_taken="REJECT",
                           reject_reason="mark_px_fetch_failed",
                           reject_detail="mark_px API 失败，拒开（禁回退 caller 值）", p0=True)
        mark_px = api_mark
        if input_divergence:  # 注入尝试可观测（不阻断，已用真值）
            print(f"[order_executor] WARN input_divergence: {input_divergence}", file=sys.stderr)
    else:
        # dryrun/单测：允许显式注入；缺值时尝试用同一 balance 回包补齐。
        # helper 不可用时保留 None 交给 validator 做 controlled reject，不伪造余额。
        if equity is None or available_margin is None:
            try:
                capacity = ac.extract_settlement_capacity(ox.get_balance(profile), "USDT")
            except Exception:
                capacity = {"ok": False, "error": "balance_unavailable"}
            if equity is None:
                equity = _to_float(capacity.get("total_equity"))
            if available_margin is None and capacity.get("ok"):
                available_margin = _to_float(capacity.get("available_margin"))
            if capacity.get("ok"):
                capacity_audit = {
                    "settlement_ccy": capacity.get("settlement_ccy", "USDT"),
                    "source": capacity.get("source"),
                    "available_margin": available_margin,
                    "frozen_balance": capacity.get("frozen_balance"),
                }
        elif available_margin is not None:
            capacity_audit = {
                "settlement_ccy": "USDT", "source": "caller_dryrun",
                "available_margin": available_margin, "frozen_balance": None,
            }
        if open_positions is None:
            try:
                open_positions = fetch_open_positions(profile)
            except PositionsUnavailable:
                open_positions = []
        if mark_px is None:
            mark_px = ox.get_mark_price(symbol, profile)
    specs = fetch_instrument_specs(symbol, profile, db_root)
    ct_val, lot_sz = specs["ct_val"], specs["lot_sz"]

    # ── 强制风控闸 ──
    v = rv.validate(
        symbol=symbol, side=side, intended_sz=intended_sz, lev=lev,
        mark_px=mark_px, ct_val=ct_val, lot_sz=lot_sz, equity=equity,
        open_positions=open_positions, sl_trigger_px=sl_trigger_px,
        profile="demo" if is_demo else "live",
        available_margin=available_margin,
    )
    if not v["approved"]:
        return receipt(False, action_taken="REJECT",
                       reject_reason=v["reject_reason"],
                       reject_detail=v["reject_detail"], risk=v)
    approved_sz = v["approved_sz"]
    # 加仓时 OKX 不允许在有仓状态随意改杠杆；validator 已用现仓实际杠杆
    # 重算本笔预算。成交回执必须沿用同一口径，禁用 caller lev 低估 margin。
    effective_lev = (_to_float((v.get("math") or {}).get("effective_lev"))
                     or lev)

    # ── 设杠杆（仅新开；加仓不动杠杆，OKX 拒改有仓杠杆）──
    new_open = not any(
        (p.get("symbol") == symbol and str(p.get("side", "")).lower() == side)
        for p in open_positions)
    lev_warn = None
    if new_open:
        lr = ox.set_leverage(symbol, lev, mgn_mode, profile)
        if not lr.get("ok"):
            # live：杠杆设失败 → 不在未知杠杆下开仓
            if not is_demo:
                return receipt(False, action_taken="REJECT",
                               reject_reason="set_leverage_failed",
                               reject_detail=str(lr.get("sMsg") or lr.get("error")),
                               risk=v)
            lev_warn = f"demo set_leverage failed: {lr.get('sMsg') or lr.get('error')}"

    # ── 市价开仓（附挂 SL，原子）──
    pre_sz = _position_size(open_positions, symbol, side)
    pre_place_ms = int(time.time() * 1000)
    pr = ox.place_market_open(
        symbol, side, approved_sz, profile, mgn_mode=mgn_mode,
        sl_trigger_px=sl_trigger_px)
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
                return receipt(False, action_taken="REJECT",
                               reject_reason="place_ambiguous",
                               reject_detail="下单写超时且现仓回读不可判定，已写 repair_queue 待人工核对",
                               risk=v, p0=True)
            if not settled:
                return receipt(False, action_taken="REJECT", reject_reason="place_failed",
                               reject_detail="下单写超时，现仓回读确认未成交", risk=v)
            recovered_timeout = True  # 实际成交 → 落正常流程（无 ordId，靠时间窗回读 fills）
        else:
            if sc == ox.SCODE_DELISTED:
                reason = "delisted"
            elif sc == ox.SCODE_NOT_EXIST:
                reason = "instrument_not_exist"
            else:
                reason = "place_failed"
            return receipt(False, action_taken="REJECT", reject_reason=reason,
                           reject_detail=f"sCode={sc} {pr.get('sMsg') or pr.get('error')}",
                           risk=v)

    ord_id = None
    for row in pr.get("data", []):
        if isinstance(row, dict) and row.get("ordId"):
            ord_id = row["ordId"]
            break

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
            elif alt and alt.get("state") == "canceled":
                fa0 = {"ok": False, "state": "canceled", "fill_px": None,
                       "fill_sz": 0.0, "pnl": 0.0, "n": 0}
        if (not fa0.get("ok") and recovered_timeout
                and fa0.get("approx_agg")):
            # 现仓回读已确证仓位存在，两成交端点延迟时才允许带标 approx。
            _enqueue_repair(profile, symbol, ord_id,
                            "position_verified_fills_missing", db_root)
            fa0 = dict(fa0["approx_agg"], ok=True)
            source0 = "approx_agg"
        fill_resolution = (fa0, source0)
        return fill_resolution

    def make_open_trade(fa0: dict[str, Any], fill_source0: str,
                        sl_mode0: str, sl_verified0: bool,
                        algo_id0: Optional[str]) -> dict[str, Any]:
        fill_px0 = fa0.get("fill_px") or mark_px
        notional0 = approved_sz * (ct_val or 0) * (fill_px0 or 0)
        margin0 = (notional0 / effective_lev) if effective_lev else None
        return {
            "symbol": symbol, "action": "open", "side": side, "sz": approved_sz,
            "fill_px": fill_px0, "px": fill_px0, "lev": effective_lev,
            "margin": margin0,
            "notional": notional0, "pnl": 0.0,
            "channel": "demo" if is_demo else "live",
            "reason": reasoning, "open_id": ord_id, "sl_trigger_px": sl_trigger_px,
            "algo_id": algo_id0, "sl_mode": sl_mode0,
            "sl_verified": sl_verified0, "fill_source": fill_source0,
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
    #   algo：独立 reduceOnly algo，返回 algoId = 交易所已受理 → sl_verified=True。
    #   超时恢复路径 attached 状态未知 → 不采信附挂，强制走独立 algo 补挂（belt）。
    algo_id = None
    attached_ok = bool(pr.get("sl_attached")) and not recovered_timeout
    # 附挂 SL 用 get_algo_orders 回读；验证后才置 sl_verified=True。
    # 回读不到 → attached_ok=False，落回下方独立 algo SL 兜底防裸仓。
    # dryrun 跳过（无真单可查）。
    sl_verified = False
    if attached_ok and sl_trigger_px is not None and not ox.is_dryrun():
        try:
            _vsl = _verify_sl_placed(symbol, side, profile, sl_trigger_px)
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
                sl_secured = True
                sl_mode, sl_verified = "algo", True
                for row in ar.get("data", []):
                    if isinstance(row, dict) and row.get("algoId"):
                        algo_id = row["algoId"]
                        break
                break
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
                # fills/订单列表延迟时，现仓增量也是已成交的权威证据。
                try:
                    post_sz = _position_size(fetch_open_positions(profile), symbol, side)
                except Exception:
                    post_sz = None
                if post_sz is not None and post_sz > pre_sz + _EPS:
                    position_fa = {"ok": True, "fill_px": mark_px,
                                   "fill_sz": post_sz - pre_sz, "pnl": 0.0, "n": 0}
                    trade_unwind_open = make_open_trade(
                        position_fa, "position_delta", "none", False, None)
                    journal_open_once(trade_unwind_open, unwind=True,
                                      journal_action="UNWIND_OPEN")
            unwind = close_position(symbol, profile, pos_side=side,
                                    mgn_mode=mgn_mode, db_root=db_root,
                                    reasoning="unwind: SL 挂单失败，平掉裸仓",
                                    cycle_id=cycle_id, _unwind=True)
            if (not open_fill_journaled and unwind.get("ok")
                    and unwind.get("trades")):
                # close_position 能产生成交回执，证明它在平前权威读到了现仓。
                # 即使 open fills 端点仍延迟，也补一条带 unwind 标记的 open 与 close 成对。
                close_trade = unwind["trades"][0]
                inferred_fa = {"ok": True, "fill_px": mark_px,
                               "fill_sz": close_trade.get("sz") or approved_sz,
                               "pnl": 0.0, "n": 0}
                trade_unwind_open = make_open_trade(
                    inferred_fa, "position_confirmed_by_unwind", "none", False, None)
                journal_open_once(trade_unwind_open, unwind=True,
                                  journal_action="UNWIND_OPEN_RECOVERED")
            if not unwind.get("ok"):
                _enqueue_repair(profile, symbol, ord_id,
                                "naked_position_unwind_failed", db_root)
                return receipt(False, action_taken="UNWIND",
                               reject_reason="naked_position_unwind_failed",
                               reject_detail="SL 全失败且平裸仓也失败 → 无止损裸仓，已双写 repair_queue",
                               risk=v, unwind=unwind, p0=True)
            return receipt(False, action_taken="UNWIND",
                           reject_reason="sl_failed_unwound",
                           reject_detail="附挂+独立 SL 均失败，已市价平掉裸仓",
                           risk=v, unwind=unwind, p0=True)

    # ── 回读真实成交（ord_id 缺失=超时恢复路径 → 用下单时刻做时间窗，防历史成交混入）──
    fa, fill_source = resolve_open_fill()
    if fa.get("state") == "canceled":
        # 交易所级确认 0 成交（canceled+accFillSz=0）→ 干净拒单（非 p0）。
        # 独立 algo SL 已挂的话此刻成悬挂单（无仓时 reduceOnly 无害）→ 记 repair 提示撤单。
        if algo_id:
            _enqueue_repair(profile, symbol, ord_id,
                            "open_canceled_dangling_algo_sl", db_root)
        return receipt(False, action_taken="REJECT",
                       reject_reason="open_not_filled",
                       reject_detail="订单状态确认未成交（canceled, accFillSz=0）",
                       risk=v, ord_id=ord_id)
    if not fa.get("ok"):
        # 两端点都确认不了 → 原 fail-safe：repair_queue + reject + P0
        _enqueue_repair(profile, symbol, ord_id, "open_fills_missing", db_root)
        return receipt(False, action_taken="REJECT",
                       reject_reason="fills_missing",
                       reject_detail="开仓后 fills/订单状态均拉不到，已写 repair_queue",
                       risk=v, ord_id=ord_id, p0=True)

    # 从这里开始已是权威确认的成交，立即落一次 journal；后面只组回执，
    # 不再进入任何 SL I/O，故 SL 回读/补挂异常无法跳过此留痕。
    trade = make_open_trade(fa, fill_source, sl_mode, sl_verified, algo_id)
    journal_open_once(trade)
    return receipt(True, trades=[trade], risk=v, ord_id=ord_id,
                   clamped=v.get("clamped"), adjustments=v.get("adjustments"),
                   lev_warn=lev_warn,
                   sl_mode=sl_mode, sl_verified=sl_verified,
                   recovered_timeout=recovered_timeout,
                   fill_source=fill_source,
                   spec_source=specs.get("spec_source"),
                   input_divergence=input_divergence or None)


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
) -> dict[str, Any]:
    is_demo = str(profile).lower() == "demo" or "demo" in str(profile).lower()

    def receipt(ok: bool, **kw) -> dict[str, Any]:
        base = {"profile": "demo" if is_demo else "live", "ok": ok,
                "action_taken": kw.pop("action_taken", "CLOSE"),
                "symbol": symbol, "trades": kw.pop("trades", []),
                "p0": kw.pop("p0", False)}
        base.update(kw)
        return base

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
    pos_sz = match["sz"]

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
    pnl = fa.get("pnl") if fa.get("ok") else None
    fill_px = fa.get("fill_px") if fa.get("ok") else None
    # 2026-07-07: close 行也带本环境真实 ct_val（fail-safe：拉不到不带，writer 回退缓存）
    close_ct_val = None
    try:
        close_ct_val = (fetch_instrument_specs(symbol, profile, db_root) or {}).get("ct_val")
    except Exception:
        pass
    trade = {
        "symbol": symbol, "action": "close", "side": side, "sz": pos_sz,
        "fill_px": fill_px, "px": fill_px, "pnl": pnl,
        "channel": "demo" if is_demo else "live", "reason": reasoning,
        "reduce_only_fallback": used_reduce_only,
        "fill_source": fill_source, "pnl_approx": pnl_approx,
        "ct_val": close_ct_val, "ordId": reduce_ord_id,
    }
    _journal_fill("demo" if is_demo else "live", trade, db_root, cycle_id,
                  "UNWIND_CLOSE" if _unwind else "CLOSE", unwind=_unwind)
    return receipt(True, action_taken="CLOSE", trades=[trade],
                   reduce_only_fallback=used_reduce_only,
                   fills_ok=bool(fa.get("ok")),
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
        fix = f"okx --profile {profile} swap fills --instId {symbol} --archive"
        con = ledger.connect(account_db)
        try:
            con.execute(
                "INSERT INTO repair_queue (ts, check_name, issue, fix_action, "
                "status, created_utc) VALUES (?,?,?,?,?,?)",
                (ts, "order_executor", issue, fix, "pending", ts))
            con.commit()
        finally:
            con.close()
    except Exception as exc:  # repair 写失败不可再静默：裸仓待修记录丢失是 P0 盲区（P2-4）
        print(f"[order_executor] WARN repair_queue 写失败({reason}): {exc}", file=sys.stderr)
