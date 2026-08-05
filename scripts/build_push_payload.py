# -*- coding: utf-8 -*-
"""build_push_payload.py — 从库确定性组装 QQ 推送的结构化 JSON（替掉 push agent 手拼）。

背景：push 环节 render/validate/qq_push/archive/system_state 五步早已脚本化，
唯一由 LLM agent 做的是"每轮临场拼那份结构化 JSON"——正是长 session 漂移源
（少段少键 / action_taken 造复合标签 / symbol=UNKNOWN / equity 填错盘 / 抄骨架占位价）。
本脚本把这一步也脚本化：只读库、确定性组装 §6.1 骨架 JSON，输出交
render_push_report.py 固定渲染。凡"像 LLM 写的" summary/decision.reason，实为
analyst 已写进 analysis.db 的产出，本脚本 verbatim 取用而非复述。

数据来源（全部只读 mode=ro）：
  analysis.db  analysis_runs[cycle]（regime/market_summary/raw/missing_sources）
               analysis_signals[cycle]（每币 action/side/decision_card/reasoning）
  live/demo_trades.db  trade_cycles[cycle]（decision/n_orders/note/raw）+ trades[cycle]（逐笔）
  market.db    tick_snapshots（BTC/ETH 真价 + chg24h）
  regime.db    cross_market（regime/dxy/vix）
  account.db   account_snapshots / position_snapshots（资金/持仓兜底；render 另有权威覆盖）
  ledger.db    collection_runs（本 cycle status!=ok → 运行故障进异常段）
  cum_pnl.py   累计收益兜底（render 另有权威覆盖）

资金/持仓数/累计收益/轮次/耗时/channel 由 render 权威覆盖，本脚本填真兜底值即可。

用法:
  build_push_payload.py [--cycle 2026-07-07T12:00] [--db-root <PROJECT_ROOT>\\db] [--out-file x.json]
  缺 --cycle 时取 analysis_runs 最新 cycle。
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


import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
DEFAULT_DB = _project_path('db')

# trades.action(+side) → 标准枚举（render/validate 只认单枚举，禁复合标签）
_OPEN = {"open", "add"}
_CLOSE = {"close", "reduce", "stop_loss"}


def connect(db_root: str, name: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_root}\\{name}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _rows(db_root, name, sql, args=()):
    try:
        con = connect(db_root, name)
        try:
            return [dict(r) for r in con.execute(sql, args)]
        finally:
            con.close()
    except Exception:
        return []


def _hold_min(db_root, prof, symbol, now):
    """当前持仓的持有分钟：entry = 最后一次全平（close/stop_loss）之后最早的 open/add。
    用「最后平仓边界」而非净额追踪——对孤儿 close / OVER_CLOSED（trades 与交易所现仓
    不完全对账）鲁棒。查不到合格 open（预 V2.0 建仓 / 对账缺口）→ None（显 '-'）。
    close 轮兜底（2026-07-09）：本轮刚平仓时 CLOSE 已落库、position_snapshots 滞后仍显示该仓
    但已无 open-after-close → 回退显示「刚平仓位」持有时长 = last_close − 该仓建仓 open
    （否则 close 轮显 '持有-' 误人；hold 轮不受影响，仍走 now − 当前建仓）。"""
    def _p(s):
        try:
            return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        except (ValueError, TypeError):
            return None
    rows = _rows(db_root, f"{prof}_trades.db",
                 "SELECT ts, action FROM trades WHERE symbol=? ORDER BY ts", (symbol,))
    if not rows:
        return None
    closes = sorted(str(r["ts"]) for r in rows
                    if str(r.get("action") or "").lower() in ("close", "stop_loss"))
    last_close = closes[-1] if closes else ""
    opens_after = [str(r["ts"]) for r in rows
                   if str(r.get("action") or "").lower() in ("open", "add")
                   and str(r["ts"]) > last_close]
    if opens_after:  # 当前持仓：now − 建仓
        t = _p(min(opens_after))
        if t is None:
            return None
        m = int((now - t).total_seconds() / 60)
        return m if m >= 0 else None
    # close 轮兜底：当前无仓，回退「刚平仓位」持有时长 = last_close − 建仓 open
    if not closes:
        return None
    prev_close = closes[-2] if len(closes) >= 2 else ""   # 界定刚平仓位的建仓窗
    opens_closed = [str(r["ts"]) for r in rows
                    if str(r.get("action") or "").lower() in ("open", "add")
                    and prev_close < str(r["ts"]) < last_close]
    if not opens_closed:
        return None
    t_open, t_close = _p(min(opens_closed)), _p(last_close)
    if t_open is None or t_close is None:
        return None
    m = int((t_close - t_open).total_seconds() / 60)
    return m if m >= 0 else None


def _open_sl_pct(db_root, prof, symbol, avg_px):
    """当前持仓的 SL 距离%（从建仓 open trade 的 `raw.sl_trigger_px` 确定性读，**不查 API**）。
    根治「SL未挂」误导缺口（2026-07-09）：position_snapshots 不带 SL，render 恒显 SL未挂——
    但 order_executor 开仓时已把附挂 SL 的 slTriggerPx 记进 open trade 的 raw（含 sl_verified）。
    entry 定位同 _hold_min（最后全平后的 open/add）；取最近一笔合格 open 的 sl_trigger_px。
    距离% = |sl_trigger − avg_px| / avg_px × 100；无 SL 记录 → None（render 显真『SL未挂』=真裸仓）。"""
    try:
        ap = float(avg_px)
    except (TypeError, ValueError):
        return None
    if not ap:
        return None
    rows = _rows(db_root, f"{prof}_trades.db",
                 "SELECT ts, action, raw FROM trades WHERE symbol=? ORDER BY ts", (symbol,))
    if not rows:
        return None
    closes = [str(r["ts"]) for r in rows
              if str(r.get("action") or "").lower() in ("close", "stop_loss")]
    last_close = max(closes) if closes else ""
    opens = [r for r in rows
             if str(r.get("action") or "").lower() in ("open", "add")
             and str(r["ts"]) > last_close]
    for r in reversed(opens):  # 最近一笔合格 open 的 SL = 当前有效止损
        slt = _loads(r.get("raw")).get("sl_trigger_px")
        try:
            slt = float(slt)
        except (TypeError, ValueError):
            slt = None
        if slt:
            return round(abs(slt - ap) / ap * 100, 1)
    return None


def _one(db_root, name, sql, args=()):
    r = _rows(db_root, name, sql, args)
    return r[0] if r else None


def _public_macro_snapshot(db_root):
    """公开宏观观测表优先；读取失败时由调用方使用 cross_market 快照兜底。"""
    try:
        from public_macro import latest_snapshot

        con = connect(db_root, "regime.db")
        try:
            return latest_snapshot(con)
        finally:
            con.close()
    except Exception:
        return {}


def _loads(s):
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, (dict, list)) else {}
    except Exception:
        return {}


def _summary_section(value):
    """把 market_summary 子段归一成 dict。

    统一分析 Agent 首轮曾把 macro/news/quant/sentiment 写成纯文本。分析内容仍然有效，
    推送层应降级使用 summary，而不能因下游直接 `.get()` 导致整轮推送失败。
    """
    if isinstance(value, dict):
        return value
    if value is None or value == "":
        return {}
    return {"summary": str(value)}


def _px(v):
    """价格四舍五入到 6 位有效数字（0.419307 / 63718.8），非数值原样。"""
    if isinstance(v, (int, float)):
        try:
            return float(f"{v:.6g}")
        except Exception:
            return v
    return v


def _r2(v):
    """金额/百分比保留 2 位小数，非数值原样。"""
    return round(v, 2) if isinstance(v, (int, float)) else v


def _ratio_pct(v):
    """数据库中的日变动比率（0.001）转为展示百分比（0.10）。"""
    return round(v * 100, 2) if isinstance(v, (int, float)) else v


def _float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_cst_datetime(value):
    """把账本时间归一到有时区的 CST datetime；无法解析返回 None。"""
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            # fromisoformat 不认尾部小写 z；显式兼容历史 UTC-Z 行。
            dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw[-1:].upper() == "Z" else raw)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def _ledger_order(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _project_positions_through_trades(snapshot_rows, trades, snapshot_ts, as_of=None):
    """把快照后至 payload 构建时点的全部已落库成交投影到持仓。

    trader 可能跨 cycle 交错完成：例如当前 push 对应 16:15，但较慢的 16:00
    trader 在 16:16 快照之后才把 fill 落账。故不能只重放当前 cycle。成交严格按
    ``(trade.ts, ledger_rowid)`` 重放，并只应用 ``snapshot_ts < trade.ts <= as_of``：
    下界避免快照已包含的成交被重复叠加，上界避免历史重建时吸收后续成交。
    该函数只做内存投影，不写账本；后续 fast_collect 仍以 OKX API 快照收敛为权威。
    """
    positions = {}
    order = []
    for raw in snapshot_rows or []:
        row = dict(raw)
        symbol = str(row.get("symbol") or "").strip()
        side = str(row.get("side") or "").strip().lower()
        qty = _float_or_none(row.get("sz"))
        if not symbol or symbol == "__FLAT__" or not side or not qty or qty <= 0:
            continue
        key = (symbol, side)
        positions[key] = row
        order.append(key)

    snapshot_dt = _as_cst_datetime(snapshot_ts)
    as_of_dt = _as_cst_datetime(as_of)
    ordered_trades = []
    for index, raw in enumerate(trades or []):
        trade = dict(raw)
        trade_dt = _as_cst_datetime(trade.get("ts"))
        # 有时间边界时，无法定位到时间轴的坏行不参与投影，避免把未知成交
        # 错投到当前持仓；合法账本行均应命中此分支之外。
        if trade_dt is None and (snapshot_dt is not None or as_of_dt is not None):
            continue
        if snapshot_dt is not None and trade_dt <= snapshot_dt:
            continue
        if as_of_dt is not None and trade_dt > as_of_dt:
            continue
        row_order = _ledger_order(
            trade.get("ledger_rowid", trade.get("id", trade.get("rowid"))), index
        )
        ordered_trades.append((trade_dt, row_order, index, trade))

    # trade_dt 为 None 仅可能发生在无任何时间边界的兼容调用；放在最前并按
    # ledger_rowid 保序。生产 build 总会传 snapshot_ts/as_of。
    floor_dt = datetime.min.replace(tzinfo=CST)
    ordered_trades.sort(key=lambda item: (item[0] or floor_dt, item[1], item[2]))

    for _trade_dt, _row_order, _index, trade in ordered_trades:
        symbol = str(trade.get("symbol") or "").strip()
        side = str(trade.get("side") or "").strip().lower()
        action = str(trade.get("action") or "").strip().lower()
        qty = _float_or_none(trade.get("sz"))
        if not symbol or not side or not qty or qty <= 0:
            continue
        key = (symbol, side)

        if action in _OPEN:
            current = positions.get(key)
            if current is None:
                current = {
                    "symbol": symbol, "side": side, "sz": qty,
                    "avgPx": trade.get("fill_px"), "lev": trade.get("lev"), "upl": 0.0,
                }
                positions[key] = current
                order.append(key)
                continue
            old_qty = _float_or_none(current.get("sz")) or 0.0
            new_qty = old_qty + qty
            old_avg = _float_or_none(current.get("avgPx"))
            fill_px = _float_or_none(trade.get("fill_px"))
            if new_qty > 0 and old_avg is not None and fill_px is not None:
                current["avgPx"] = (old_avg * old_qty + fill_px * qty) / new_qty
            elif fill_px is not None:
                current["avgPx"] = fill_px
            current["sz"] = new_qty
            if trade.get("lev") not in (None, ""):
                current["lev"] = trade.get("lev")
        elif action in _CLOSE:
            current = positions.get(key)
            if current is None:
                continue
            old_qty = _float_or_none(current.get("sz")) or 0.0
            new_qty = old_qty - qty
            if new_qty <= 1e-12:
                positions.pop(key, None)
            else:
                current["sz"] = new_qty
                old_upl = _float_or_none(current.get("upl"))
                if old_upl is not None and old_qty > 0:
                    current["upl"] = old_upl * new_qty / old_qty

    return [positions[key] for key in order if key in positions]


def _latest_position_snapshot(db_root, prof, as_of):
    """取 as_of 之前最近一批完整持仓快照（按真实时间，rowid 仅作同刻破同）。"""
    as_of_dt = _as_cst_datetime(as_of)
    batches = _rows(
        db_root, "account.db",
        "SELECT ts,MAX(rowid) AS batch_rowid FROM position_snapshots "
        "WHERE profile=? GROUP BY ts ORDER BY ts DESC",
        (prof,),
    )
    candidates = []
    for index, batch in enumerate(batches):
        batch_dt = _as_cst_datetime(batch.get("ts"))
        if batch_dt is None or (as_of_dt is not None and batch_dt > as_of_dt):
            continue
        candidates.append((
            batch_dt,
            _ledger_order(batch.get("batch_rowid"), index),
            str(batch.get("ts") or ""),
        ))
    if not candidates:
        return "", []
    _batch_dt, _batch_rowid, snapshot_ts = max(candidates, key=lambda item: (item[0], item[1]))
    rows = _rows(
        db_root, "account.db",
        "SELECT symbol,side,sz,avgPx,lev,upl FROM position_snapshots "
        "WHERE profile=? AND ts=? AND symbol!='__FLAT__' ORDER BY rowid",
        (prof, snapshot_ts),
    )
    return snapshot_ts, rows


def _short(sym: str) -> str:
    s = str(sym or "").strip()
    for suf in ("-USDT-SWAP", "-USDC-SWAP", "-USD-SWAP", "-USDT", "-USDC", "-USD"):
        if s.upper().endswith(suf):
            return s[: -len(suf)]
    return s


def _fmt_lesson(x):
    """教训条目格式化：dict（lessons_matched 形态）→ '{id}[{occ}x]: {note}'，
    match==False 的不命中项跳过（返 None）；已是字符串（applied_lessons 形态）原样。"""
    if isinstance(x, dict):
        if x.get("match") is False:
            return None
        _id = x.get("id") or x.get("name") or "lesson"
        occ = x.get("occurrences") or x.get("hit_count")
        note = x.get("note") or x.get("trigger_condition") or ""
        tag = f"[{occ}x]" if occ else ""
        return f"{_id}{tag}: {note}".strip(": ").strip()
    return str(x)


def _num_or_dash(v):
    """数值（含数值字符串）原样返回，否则 '-'（防非数值 wr 被 render 加 % 成 '未验证%'）。"""
    if isinstance(v, (int, float)):
        return v
    try:
        float(str(v))
        return v
    except (ValueError, TypeError):
        return "-"


def _short_dxy(dxy_trend: str) -> str:
    """兼容 dxy_trend 键；实际指标为 FRED USD_BROAD(DTWEXBGS)，非 ICE DXY。"""
    t = str(dxy_trend or "").strip()
    if not t:
        return "USD_BROAD -"
    head = t.split(",")[0].strip()
    tail = "承压" if ("承压" in t or "压制" in t) else ""
    return f"USD_BROAD {head} {tail}".strip()


def _usd_broad_summary(macro: dict) -> str:
    """Render old and current USD_BROAD macro schemas without false missing."""
    legacy = str(macro.get("dxy_trend") or "").strip()
    if legacy:
        return _short_dxy(legacy)
    value = _float_or_none(
        macro.get(
            "dxy_broad_usd_trade_weighted",
            macro.get(
                "dxy_broad_value",
                macro.get("usd_broad_dtwexbgs", macro.get("usd_broad")),
            ),
        )
    )
    zone = str(
        macro.get("dxy_broad_zone")
        or macro.get("dxy_zone")
        or macro.get("usd_broad_zone")
        or ""
    ).strip()
    parts = ["USD_BROAD"]
    if value is not None:
        parts.append(str(_r2(value)))
    if zone:
        parts.append(zone)
    return " ".join(parts) if len(parts) > 1 else "USD_BROAD -"


def latest_cycle(db_root: str) -> str | None:
    r = _one(db_root, "analysis.db",
             "SELECT cycle_id FROM analysis_runs "
             "ORDER BY ts DESC,rowid DESC LIMIT 1")
    return r["cycle_id"] if r else None


def _action_from_trades(trades: list) -> str | None:
    """一批 trades → 去重后的动作枚举，保留成交顺序。

    同轮既平仓又开仓时不能只取首笔动作，否则后续把全部 symbol 拼到首笔动作后，
    会生成 ``CLOSE A/B/C`` 这类把新开仓误报成平仓的标题。
    """
    if not trades:
        return None
    actions = []
    for t in trades:
        act = str(t.get("action") or "").lower()
        side = str(t.get("side") or "").lower()
        if act in _OPEN:
            label = (
                "OPEN_LONG" if side == "long"
                else "OPEN_SHORT" if side == "short"
                else "ADD"
            )
        elif act == "stop_loss":
            label = "STOP_LOSS"
        elif act in _CLOSE:
            label = "REDUCE" if act == "reduce" else "CLOSE"
        else:
            label = "ADJUST"
        if label not in actions:
            actions.append(label)
    return "/".join(actions)


def _map_decision(dec: str) -> str:
    d = str(dec or "").lower()
    return {"traded": "TRADED", "hold": "HOLD", "skip": "WAIT",
            "degraded": "DEGRADED", "error": "ERROR"}.get(d, "UNKNOWN")


def build(db_root: str, cycle: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(CST)
    hhmm = cycle.split("T")[1] if "T" in cycle else now.strftime("%H:%M")

    # ── analyst 产出 ──────────────────────────────────────
    ar = _one(db_root, "analysis.db",
              "SELECT regime,regime_stale,mode,status,market_summary,missing_sources,raw "
              "FROM analysis_runs WHERE cycle_id=?", (cycle,)) or {}
    ms = _loads(ar.get("market_summary"))
    macro = _summary_section(ms.get("macro")) if isinstance(ms, dict) else {}
    news = _summary_section(ms.get("news")) if isinstance(ms, dict) else {}
    quant = _summary_section(ms.get("quant")) if isinstance(ms, dict) else {}
    senti = _summary_section(ms.get("sentiment")) if isinstance(ms, dict) else {}
    regime = ar.get("regime") or macro.get("regime") or "-"

    sigs = _rows(db_root, "analysis.db",
                 "SELECT symbol,total,action,side,confidence,reasoning,decision_card "
                 "FROM analysis_signals WHERE cycle_id=? "
                 "ORDER BY CASE action WHEN 'close' THEN 0 WHEN 'open_long' THEN 1 "
                 "WHEN 'open_short' THEN 1 WHEN 'hold' THEN 2 ELSE 3 END,rowid",
                 (cycle,))
    top_sig = sigs[0] if sigs else {}
    open_cands = [s for s in sigs if str(s.get("action") or "").startswith("open")]

    # ── 交易段（双盘）─────────────────────────────────────
    books = {}
    for prof in ("live", "demo"):
        tc = _one(db_root, f"{prof}_trades.db",
                  "SELECT decision,n_orders,equity,note,raw FROM trade_cycles WHERE cycle_id=?",
                  (cycle,)) or {}
        tr = _rows(db_root, f"{prof}_trades.db",
                   "SELECT ts,symbol,action,side,sz,fill_px,lev,margin,notional,pnl,reasoning "
                   "FROM trades WHERE cycle_id=? ORDER BY id", (cycle,))
        books[prof] = {"tc": tc, "trades": tr, "raw": _loads(tc.get("raw"))}

    all_trades = books["live"]["trades"] + books["demo"]["trades"]

    # ── headline action / symbol / confidence ─────────────
    action = _action_from_trades(all_trades)
    if action in (None, "TRADED"):
        decs = [_map_decision(books[p]["tc"].get("decision")) for p in ("live", "demo")]
        action = next(
            (label for label in ("ERROR", "DEGRADED", "UNKNOWN", "HOLD", "WAIT", "TRADED")
             if label in decs),
            "UNKNOWN",
        )

    if all_trades:
        syms, seen = [], set()
        for t in all_trades:
            s = _short(t.get("symbol"))
            if s and s not in seen:
                seen.add(s); syms.append(s)
        symbol = "/".join(syms[:3])
        conf_sig = next((s for s in sigs if _short(s.get("symbol")) == syms[0]), top_sig)
    else:
        symbol = _short(top_sig.get("symbol")) or "BTC"
        conf_sig = top_sig
    card = _loads(conf_sig.get("decision_card"))
    if not isinstance(card, dict):
        card = {}
    confidence = "-"  # 旧推送键保留；新协议不显示或消费评分

    # ── summary（确定性组装，含市场实质）────────────────
    _ACTION_CN = {"HOLD": "HOLD 维持", "WAIT": "WAIT 观望", "OPEN_LONG": "开多",
                  "OPEN_SHORT": "开空", "CLOSE": "平仓", "STOP_LOSS": "止损",
                  "ADJUST": "调整", "ADD": "加仓", "REDUCE": "减仓",
                  "TRADED": "已成交", "DEGRADED": "DEGRADED 降级",
                  "ERROR": "ERROR 失败", "UNKNOWN": "UNKNOWN 未知"}
    def _action_cn(value):
        return "/".join(_ACTION_CN.get(part, part) for part in str(value).split("/"))

    action_cn = _action_cn(action)
    # 两盘动作逐盘推导（成交行优先、无成交回退 decision）；
    # 一致才冠“实盘/模拟双盘”，分歧则并列展示。
    book_acts = {}
    for prof in ("live", "demo"):
        book_acts[prof] = (_action_from_trades(books[prof]["trades"])
                           or _map_decision(books[prof]["tc"].get("decision")))
    if book_acts["live"] == book_acts["demo"]:
        head_cn = f"双盘{action_cn}"
    else:
        head_cn = (f"live {_action_cn(book_acts['live'])}"
                   f"/demo {_action_cn(book_acts['demo'])}")
    news_evts = news.get("events", []) if isinstance(news, dict) else []
    top_news = ""
    for e in news_evts:
        if isinstance(e, dict) and str(e.get("severity")).lower() == "high" and e.get("headline"):
            top_news = f"；关注 {str(e['headline'])[:36]}"
            break
    if not top_news and news.get("summary"):
        top_news = f"；{str(news['summary'])[:36]}"
    summary = (f"{head_cn}：regime={regime}，{_usd_broad_summary(macro)}，"
               f"{len(open_cands)} 个 open 候选{top_news}")

    # ── decision.reason（主体=headline 币 analyst 理由，恒在且最相关；叠成交理由+宏观+校准+教训）──
    live_raw = books["live"]["raw"]
    demo_raw = books["demo"]["raw"]
    reason_bits = []
    if card.get("agent_judgement"):
        reason_bits.append(f"Agent裁决：{card['agent_judgement']}")
    head_reason = conf_sig.get("reasoning")
    if head_reason:
        reason_bits.append(str(head_reason))
    if all_trades:  # 有成交补该笔下单理由（与信号理由互补）
        tr0 = all_trades[0].get("reasoning")
        if tr0 and str(tr0) != str(head_reason):
            reason_bits.append(f"执行：{tr0}")
    macro_line = "；".join(
        str(x) for x in (
            macro.get("dxy_trend") or _usd_broad_summary(macro),
            macro.get("risk_appetite"),
            macro.get("regime_stability_24h"),
            macro.get("summary") or macro.get("verdict"),
        ) if x
    )
    if macro_line:
        reason_bits.append(f"宏观：{macro_line}")
    calib = quant.get("calibration_30d")  # quant.calibration_30d 甜区/纪律
    if isinstance(calib, dict):
        parts = []
        for k, v in calib.items():
            if isinstance(v, dict) and v.get("wr") is not None:
                seg = f"{k}:wr{v.get('wr')}"
                if v.get("avg_pnl_pct") is not None:
                    seg += f"/avg{v.get('avg_pnl_pct')}"
                parts.append(seg)
        if parts:
            reason_bits.append("校准30d " + " ".join(parts[:2]))
    lessons = (live_raw.get("applied_lessons") or live_raw.get("lessons_matched")
               or demo_raw.get("lesson_applied") or [])  # trader raw 键逐轮变名，全兜
    if isinstance(lessons, str):
        lessons = [lessons]
    lesson_strs = [s for s in (_fmt_lesson(x) for x in lessons[:3]) if s][:2]
    if lesson_strs:
        reason_bits.append("教训：" + "；".join(lesson_strs))
    reason = " | ".join(reason_bits) or "regime、技术面、新闻、经验库综合确认。"

    # ── 经验引用（market_summary.quant.playbook_matches 主源；experiences_cited 兜底）──
    play = {"play_id": "-", "play_title": "-", "hit_rate": "-",
            "avg_return": "-", "uncertainty": "-"}
    pbm = quant.get("playbook_matches") if isinstance(quant, dict) else None
    if isinstance(pbm, list) and pbm and isinstance(pbm[0], dict):
        p0 = pbm[0]
        play["play_id"] = p0.get("id", "-")
        play["play_title"] = str(p0.get("note") or p0.get("summary") or p0.get("title") or "-")[:60]
        play["hit_rate"] = _num_or_dash(p0.get("wr"))
    cited = (live_raw.get("experiences_cited") or demo_raw.get("experiences_cited") or [])
    if cited and isinstance(cited[0], dict):
        c = cited[0]
        play.update(play_id=c.get("id", play["play_id"]),
                    play_title=str(c.get("summary", play["play_title"]))[:60],
                    hit_rate=_num_or_dash(c.get("win_rate")),
                    avg_return=_num_or_dash(c.get("avg_pnl_pct")))
    # 最大不确定性（宏观矛盾 + BTC 情绪背离 + BTC.D）
    unc_bits = []
    if macro.get("risk_appetite"):
        unc_bits.append(str(macro["risk_appetite"]))
    per_coin = senti.get("per_coin", {}) if isinstance(senti, dict) else {}
    btc_s = per_coin.get("BTC-USDT-SWAP") if isinstance(per_coin, dict) else None
    if isinstance(btc_s, dict) and btc_s.get("bull_pct") is not None:
        unc_bits.append(f"BTC情绪多{btc_s.get('bull_pct')}%/{btc_s.get('mentions')}提及")
    if macro.get("btc_d"):
        unc_bits.append(f"BTC.D {macro['btc_d']}")
    if unc_bits:
        play["uncertainty"] = "；".join(unc_bits)

    # ── trades 段 ─────────────────────────────────────────
    def _fmt_trades(tr):
        out = []
        for t in tr:
            out.append({"symbol": t.get("symbol"), "action": t.get("action"),
                        "side": t.get("side"), "sz": t.get("sz"),
                        "fill_px": _px(t.get("fill_px")), "lev": t.get("lev"),
                        "pnl": _r2(t.get("pnl"))})
        return out

    # ── 持仓段（as-of 最近快照 + 其后全部已落账成交，剔哨兵）──
    def _positions(prof):
        snapshot_ts, rows = _latest_position_snapshot(db_root, prof, now)
        # 只把这些行用于持仓内存投影；headline / execution / trades 段仍严格展示
        # 当前 cycle，避免把交错 cycle 的成交误报为本轮执行。
        ledger_trades = _rows(
            db_root, f"{prof}_trades.db",
            "SELECT id AS ledger_rowid,ts,cycle_id,symbol,action,side,sz,fill_px,lev "
            "FROM trades ORDER BY id",
        )
        rows = _project_positions_through_trades(
            rows, ledger_trades, snapshot_ts, as_of=now
        )
        # 2026-07-15 主人要求：持仓行补名义/保证金（USD + 占净值%）。ctVal 取
        # market.db.instruments_cache，公式与 risk_validator 同口径 sz×ctVal×avgPx÷lev；
        # ctVal/净值缺失 → 字段 None，render 静默省略（不断行）。
        eqr = _one(db_root, "account.db",
                   "SELECT totalEq FROM account_snapshots WHERE profile=? "
                   "ORDER BY ts DESC,rowid DESC LIMIT 1", (prof,))
        eq = eqr["totalEq"] if eqr and isinstance(eqr["totalEq"], (int, float)) \
            and eqr["totalEq"] > 0 else None
        out = []
        for r in rows:
            notional = margin = margin_pct = None
            try:
                ctv = _one(db_root, "market.db",
                           "SELECT ctVal FROM instruments_cache WHERE instId=?",
                           (r["symbol"],))
                if ctv and ctv["ctVal"] and r["sz"] and r["avgPx"] and r["lev"]:
                    notional = float(r["sz"]) * float(ctv["ctVal"]) * float(r["avgPx"])
                    margin = notional / float(r["lev"])
                    margin_pct = round(margin / eq * 100, 1) if eq else None
            except (TypeError, ValueError, ZeroDivisionError):
                notional = margin = margin_pct = None
            out.append({"symbol": r["symbol"], "side": r["side"], "sz": r["sz"],
                        "avgPx": _px(r["avgPx"]), "lev": r["lev"], "upl": _r2(r["upl"]),
                        "notional_usd": _r2(notional), "margin_usd": _r2(margin),
                        "margin_pct": margin_pct,
                        "hold_min": _hold_min(db_root, prof, r["symbol"], now),
                        "sl_pct": _open_sl_pct(db_root, prof, r["symbol"], r["avgPx"]),
                        "profile": prof})
        return out

    live_pos = _positions("live")
    demo_pos = _positions("demo")
    positions = live_pos + demo_pos

    # ── 资产段兜底（render 权威覆盖）──────────────────────
    def _assets(prof):
        a = _one(db_root, "account.db",
                 "SELECT totalEq,availBal,upl FROM account_snapshots "
                 "WHERE profile=? ORDER BY ts DESC,rowid DESC LIMIT 1",
                 (prof,)) or {}
        cum = None
        try:
            sys.path.insert(0, _project_path('scripts'))
            import cum_pnl
            info = cum_pnl.cum_for(db_root, prof)
            cum = info.get("cum_pnl") if info.get("ok") else None
        except Exception:
            pass
        n_pos = len(live_pos if prof == "live" else demo_pos)
        return {"equity": a.get("totalEq"), "realized_pnl": cum, "positions": n_pos,
                "availBal": a.get("availBal")}

    assets = {"live": _assets("live"), "demo": _assets("demo")}

    # ── 风控段（确定性计算）───────────────────────────────
    def _walk_mappings(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from _walk_mappings(child)
        elif isinstance(value, list):
            for child in value:
                yield from _walk_mappings(child)

    def _live_portfolio_imr_summary():
        """组装 Live 组合 IMR 展示证据。

        当前值与预计值只接受本轮 executor ``risk.math`` 中由同次 Live balance
        生成的 canonical 字段。没有权威 ``account.imr/totalEq`` 证据时保持空值，
        不用逐仓保证金求和、``mgnRatio``、gross 或 net 猜测组合 IMR。
        """
        candidates = []
        for node in _walk_mappings(books["live"].get("raw")):
            current_ratio = _float_or_none(
                node.get("current_portfolio_imr_ratio"))
            projected_ratio = _float_or_none(
                node.get("projected_portfolio_imr_ratio"))
            if current_ratio is None and projected_ratio is None:
                continue
            if ((current_ratio is not None and current_ratio < 0)
                    or (projected_ratio is not None and projected_ratio < 0)):
                continue
            candidates.append({
                "account_imr": _float_or_none(node.get("account_imr")),
                "incremental_order_imr": _float_or_none(
                    node.get("incremental_order_imr")),
                "projected_account_imr": _float_or_none(
                    node.get("projected_account_imr")),
                "current_portfolio_imr_ratio": current_ratio,
                "projected_portfolio_imr_ratio": projected_ratio,
                "max_portfolio_imr_ratio": _float_or_none(
                    node.get("max_portfolio_imr_ratio")),
                "portfolio_imr_ratio_unit": str(
                    node.get("portfolio_imr_ratio_unit") or "fraction"),
                "portfolio_imr_source": str(
                    node.get("portfolio_imr_source")
                    or "account.balance.imr"),
            })

        # 同一 cycle 若有多个 OPEN/ADD 尝试，选择预计比例最高的一条完整 risk
        # 记录作保守展示，但绝不把不同记录的金额/比率拼成一个伪快照。
        selected = max(
            candidates,
            key=lambda item: (
                item["projected_portfolio_imr_ratio"]
                if item["projected_portfolio_imr_ratio"] is not None
                else item["current_portfolio_imr_ratio"]
            ),
            default={},
        )

        return {
            "account_imr": _r2(selected.get("account_imr")),
            "incremental_order_imr": _r2(
                selected.get("incremental_order_imr")),
            "projected_account_imr": _r2(
                selected.get("projected_account_imr")),
            "current_portfolio_imr_ratio": selected.get(
                "current_portfolio_imr_ratio"),
            "projected_portfolio_imr_ratio": selected.get(
                "projected_portfolio_imr_ratio"),
            "max_portfolio_imr_ratio": (
                selected.get("max_portfolio_imr_ratio") or 0.666
            ),
            "portfolio_imr_ratio_unit": selected.get(
                "portfolio_imr_ratio_unit", "fraction"),
            "portfolio_imr_source": selected.get(
                "portfolio_imr_source", "account.balance.imr"),
            "current_portfolio_imr_source": selected.get(
                "portfolio_imr_source"),
        }

    def _demo_capacity_summary():
        """提取 Demo OPEN 的交易所实时容量留痕，绝不以快照 availBal 兜底。"""
        raw = books["demo"].get("raw")
        candidates = []
        seen = set()
        for node in _walk_mappings(raw):
            source = str(node.get("source") or "")
            has_exchange_cap = any(
                node.get(key) not in (None, "")
                for key in (
                    "max_size", "exchange_max_sz", "exchange_max_size",
                    "max_sz_exchange",
                )
            )
            if "max-size" not in source and not has_exchange_cap:
                continue
            direction_field = node.get(
                "direction_field", node.get("side_field"))
            exchange_max = _float_or_none(node.get("max_size"))
            if exchange_max is None:
                exchange_max = _float_or_none(node.get("exchange_max_sz"))
            if exchange_max is None:
                exchange_max = _float_or_none(node.get("exchange_max_size"))
            if exchange_max is None:
                exchange_max = _float_or_none(node.get("max_sz_exchange"))
            if exchange_max is None:
                exchange_max = _float_or_none(node.get("direction_value"))
            if exchange_max is None:
                if str(direction_field).lower() == "maxsell":
                    exchange_max = _float_or_none(
                        node.get("max_sell", node.get("maxSell")))
                else:
                    exchange_max = _float_or_none(
                        node.get("max_buy", node.get("maxBuy")))
            key = (
                str(node.get("inst_id") or node.get("instId") or
                    node.get("symbol") or ""),
                str(node.get("side") or ""),
                str(direction_field or ""),
                exchange_max,
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "source": source or "account.max-size",
                "profile": "demo",
                "inst_id": key[0] or None,
                "side": key[1] or None,
                "td_mode": node.get("td_mode", node.get("tdMode")),
                "direction_field": direction_field,
                "direction_value": _float_or_none(node.get("direction_value")),
                "max_size": exchange_max,
                "exchange_max_sz": exchange_max,
                "requested_lev": _float_or_none(node.get("requested_lev")),
                "effective_lev": _float_or_none(node.get("effective_lev")),
            })

        opens = [
            trade for trade in books["demo"]["trades"]
            if str(trade.get("action") or "").lower() in _OPEN
        ]
        entries = []
        for trade in opens:
            symbol = str(trade.get("symbol") or "")
            side = str(trade.get("side") or "").lower()
            cap = next(
                (item for item in candidates
                 if (not item.get("inst_id") or item.get("inst_id") == symbol)
                 and (not item.get("side")
                      or str(item.get("side")).lower() == side)),
                {},
            )
            entries.append({
                **cap,
                "inst_id": cap.get("inst_id") or symbol or None,
                "side": cap.get("side") or trade.get("side"),
                "actual_open_sz": _float_or_none(trade.get("sz")),
                "effective_lev": (
                    cap.get("effective_lev")
                    if cap.get("effective_lev") is not None
                    else _float_or_none(trade.get("lev"))
                ),
            })
        if not entries and candidates:
            # REJECT 轮也保留实时容量证据，但明确没有实际成交。
            entries = [{**item, "actual_open_sz": None} for item in candidates]
        first = entries[0] if entries else {}
        return {
            "sizing_policy": "okx_demo_max_size_only",
            "entries": entries,
            "source": first.get("source"),
            "inst_id": first.get("inst_id"),
            "actual_open_sz": first.get("actual_open_sz"),
            "max_size": first.get("max_size"),
            "exchange_max_sz": first.get("exchange_max_sz"),
            "effective_lev": first.get("effective_lev"),
        }

    def _side_pct(pos):
        # 2026-07-15：改用真名义 notional_usd 加权（旧 sz×avgPx 漏乘 ctVal，跨币种权重
        # 失真——BTC 一张被放大 100 倍）；notional 缺失行回退旧口径保持可算。
        if not pos:
            return None
        def _w(p):
            v = p.get("notional_usd")
            return v if isinstance(v, (int, float)) else (p["sz"] or 0) * (p["avgPx"] or 0)
        longn = sum(_w(p) for p in pos if str(p["side"]).lower() in ("long", "buy"))
        total = sum(_w(p) for p in pos)
        if total <= 0:
            return None
        return round(max(longn, total - longn) / total * 100, 1)

    live_lev = max((p["lev"] or 0 for p in live_pos), default="-")
    live_portfolio_imr = _live_portfolio_imr_summary()
    risk = {
        # 新 payload 只发组合 IMR 契约；旧 margin_pct/live_margin_pct 仅由
        # render 对历史 payload 做只读兼容，本 builder 不再生成。
        **live_portfolio_imr,
        "demo_capacity": _demo_capacity_summary(),
        "available_margin": {
            "live_usdt": assets["live"].get("availBal"),
            "demo_usdt": assets["demo"].get("availBal"),
            "demo_scope": "account_snapshot_display_only_not_open_capacity",
        },
        "lev": live_lev if live_lev != 0 else "-",
        "side_pct": _side_pct(live_pos),
        "position_count": len(live_pos),
        "status": (
            "PASS"
            if str(books["live"]["tc"].get("decision") or "").lower()
            in {"traded", "hold", "skip"}
            else str(
                books["live"]["tc"].get("decision") or "UNKNOWN"
            ).upper()
        ),
    }

    # ── 行情段（真价，修 ETH 抄占位价 bug）────────────────
    def _tick(sym):
        return _one(db_root, "market.db",
                    "SELECT last,chg24h FROM tick_snapshots WHERE symbol=? "
                    "ORDER BY ts DESC LIMIT 1", (sym,)) or {}
    btc, eth = _tick("BTC-USDT-SWAP"), _tick("ETH-USDT-SWAP")
    cm = _one(db_root, "regime.db",
              "SELECT dxy,dxy_d1,vix,vix_d1,spx,spx_d1,btc_dominance,btc_etf_flow,"
              "defillama_tvl_total,btc_etf_net_flow_usd,dxy_calc_ecb,"
              "dxy_calc_ecb_d1,fear_greed,fear_greed_label,source_meta "
              "FROM cross_market ORDER BY ts DESC LIMIT 1") or {}
    market = {
        "btc": _px(btc.get("last")), "btc_chg24h": _r2(btc.get("chg24h")),
        "eth": _px(eth.get("last")), "eth_chg24h": _r2(eth.get("chg24h")),
        "regime": regime, "dxy": _r2(cm.get("dxy")),
    }

    # ── 异常段（仅本 cycle 采集运行故障）──────────────────
    faults = _rows(db_root, "ledger.db",
                   "SELECT source,status,err FROM collection_runs "
                   "WHERE cycle_id=? AND status!='ok'", (cycle,))
    exceptions = [{"name": f["source"], "status": f["status"],
                   "detail": (f["err"] or "-")[:120]} for f in faults]
    for p in ("live", "demo"):
        decision = str(books[p]["tc"].get("decision") or "").lower()
        if decision in {"degraded", "error"}:
            exceptions.append({"name": f"{p}_trader", "status": decision,
                               "detail": str(books[p]["tc"].get("note"))[:120]})

    # ── HH:01 宏观 + 全市场段（:00 整点，恢复 agent 每整点带的段）──
    is_hh01 = str(hhmm).endswith(":00")
    macro_block = {}
    if is_hh01:
        _etf, _tvl = cm.get("btc_etf_flow"), cm.get("defillama_tvl_total")
        _public_macro = _public_macro_snapshot(db_root)
        _dxy_calc_row = _public_macro.get("dxy_calc_ecb") or {}
        _fear_row = _public_macro.get("fear_greed") or {}
        _dxy_calc = _dxy_calc_row.get("value", cm.get("dxy_calc_ecb"))
        _dxy_calc_d1 = _public_macro.get(
            "dxy_calc_ecb_d1", cm.get("dxy_calc_ecb_d1")
        )
        _fear_value = _fear_row.get("value", cm.get("fear_greed"))
        _fear_label = _fear_row.get("label", cm.get("fear_greed_label"))
        try:
            _macro_meta = json.loads(cm.get("source_meta") or "{}")
        except (TypeError, json.JSONDecodeError):
            _macro_meta = {}
        _etf_meta = _macro_meta.get("btc_etf_net_flow_usd") or {}
        _etf_hard = cm.get("btc_etf_net_flow_usd")
        _etf_provisional = _etf_meta.get("provisional_value_usd")
        _etf_confirmed_row = _public_macro.get("etf_confirmed") or {}
        _etf_provisional_row = _public_macro.get("etf_provisional") or {}
        _etf_conflict_row = _public_macro.get("etf_conflict") or {}
        if _etf_confirmed_row:
            _etf_hard = _etf_confirmed_row.get("value")
            _etf_provisional = None
            _etf_meta = {
                "status": "cross_checked",
                "source_as_of": _etf_confirmed_row.get("observation_date"),
            }
        elif _etf_conflict_row:
            _etf_hard = None
            _etf_provisional = None
            _etf_meta = {
                "status": "conflict",
                "source_as_of": _etf_conflict_row.get("observation_date"),
            }
        elif _etf_provisional_row:
            _etf_hard = None
            _etf_provisional = _etf_provisional_row.get("value")
            _etf_meta = {
                "status": "provisional_single_source",
                "source_as_of": _etf_provisional_row.get("observation_date"),
                "source": _etf_provisional_row.get("source"),
            }
        macro_block = {
            "enabled": True,
            "dxy": _r2(cm.get("dxy")), "dxy_d1": _ratio_pct(cm.get("dxy_d1")),
            "dxy_calc_ecb": _r2(_dxy_calc),
            "dxy_calc_ecb_d1": _ratio_pct(_dxy_calc_d1),
            "vix": _r2(cm.get("vix")), "spx": _r2(cm.get("spx")), "spx_d1": _ratio_pct(cm.get("spx_d1")),
            "fear_greed": _r2(_fear_value),
            "fear_greed_label": _fear_label or "-",
            "btc_dominance": _r2(cm.get("btc_dominance")),
            "btc_mcap_chg_24h_usd": (f"{_etf / 1e9:+.2f}B" if isinstance(_etf, (int, float)) else "-"),
            "btc_etf_net_flow_usd": (
                f"{_etf_hard / 1e6:+.1f}M"
                if isinstance(_etf_hard, (int, float))
                else (
                    f"{_etf_provisional / 1e6:+.1f}M provisional"
                    if isinstance(_etf_provisional, (int, float))
                    else "-"
                )
            ),
            "btc_etf_flow_status": _etf_meta.get("status") or "missing",
            "btc_etf_flow_as_of": _etf_meta.get("source_as_of") or "-",
            "tvl": (f"{_tvl / 1e9:.1f}B" if isinstance(_tvl, (int, float)) else "-"),
            "degraded_sources": ",".join(f["source"] for f in faults) or "无",
        }
        # 下面两处 MAX(ts) 取最新批次：本表 ts 全列统一 UTC-Z（ts_audit MIXED=0），词典序即
        # 时序，走覆盖索引 ~2ms。勿改 rowid DESC（INSERT OR REPLACE 会改 rowid）；勿改
        # datetime(ts)（临时 B 树全排序 ~436ms，本函数每 15min 跑）。2026-07-29 审计实测。
        try:  # 全市场 TOP 涨跌 + 资金费异常
            tks = [t for t in _rows(db_root, "market.db",
                   "SELECT symbol,chg24h FROM tick_snapshots WHERE ts=("
                   "SELECT MAX(ts) FROM tick_snapshots) AND chg24h IS NOT NULL")
                   if t.get("chg24h") is not None]
            gain = sorted(tks, key=lambda t: t["chg24h"], reverse=True)[:3]
            lose = sorted(tks, key=lambda t: t["chg24h"])[:3]
            market["top_gainers"] = " ".join(f"{_short(t['symbol'])}{t['chg24h']:+.1f}%" for t in gain)
            market["top_losers"] = " ".join(f"{_short(t['symbol'])}{t['chg24h']:+.1f}%" for t in lose)
            drv = sorted([d for d in _rows(db_root, "market.db",
                          "SELECT symbol,funding_rate FROM derivatives WHERE ts=("
                          "SELECT MAX(ts) FROM derivatives) AND funding_rate IS NOT NULL")
                          if d.get("funding_rate") is not None],
                         key=lambda d: abs(d["funding_rate"]), reverse=True)[:3]
            market["funding_anomalies"] = " ".join(
                f"{_short(d['symbol'])}{d['funding_rate'] * 100:+.3f}%" for d in drv) or "无"
        except Exception:
            pass

    # ── 时间线 ────────────────────────────────────────────
    mins_hh01 = (61 - now.minute) % 60 or 60
    timeline = {"next_hh01_min": mins_hh01, "next_review_time": "08:05"}

    return {
        "cycle_id": cycle,
        "hhmm": hhmm,
        "cycle_count": 0,          # render 权威覆盖
        "cycle_duration_s": 0,     # render 权威覆盖
        "channel": "live|demo",    # render 硬编码覆盖
        "symbol": symbol or "BTC",
        "confidence": confidence,
        "summary": summary,
        "action_taken": action,
        "decision": {
            "summary": summary,
            "reason": reason,
            "decision_protocol": "decision_card_v1" if card else "legacy_score",
            "decision_card": card,
            **play,
        },
        "execution": {
            "result": "", "fill_px": "-", "stop_px": "-",
            # 报 build 真读到的双盘 trades 落库行数（0 是合法值），
            # 不使用与 cycle 无法可靠关联的代理值。
            "db_rows_live": len(books["live"]["trades"]),
            "db_rows_demo": len(books["demo"]["trades"]),
        },
        "risk": risk,
        "trades": {"live": _fmt_trades(books["live"]["trades"]),
                   "demo": _fmt_trades(books["demo"]["trades"])},
        "assets": assets,
        "market": market,
        "positions": positions,
        # render 收到与 cycle 相同的标记时，持仓数使用上述 as-of 快照+窗口成交
        # 投影，不再被交易前 position_snapshots 旧批次覆盖。
        "positions_projected_cycle": cycle,
        "positions_projected_as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "exceptions": exceptions,
        "timeline": timeline,
        "is_hh01": is_hh01,
        "macro": macro_block,
        "title": f"【{hhmm}】{action} {symbol}",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="从库确定性组装 QQ 推送结构化 JSON")
    ap.add_argument("--cycle", default=None)
    ap.add_argument("--db-root", default=DEFAULT_DB)
    ap.add_argument("--out-file", default=None)
    args = ap.parse_args()
    cycle = args.cycle or latest_cycle(args.db_root)
    if not cycle:
        print('{"ok":false,"error":"no cycle"}')
        return 1
    payload = build(args.db_root, cycle)
    out = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.out_file:
        with open(args.out_file, "w", encoding="utf-8") as f:
            f.write(out)
        print(json.dumps({"ok": True, "cycle": cycle, "out_file": args.out_file},
                         ensure_ascii=False))
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
