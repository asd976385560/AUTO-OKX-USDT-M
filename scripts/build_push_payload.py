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
  live_trades.db  trade_cycles[cycle]（decision/n_orders/note/raw）+ trades[cycle]（逐笔）
  market.db    tick_snapshots（BTC/ETH 真价 + chg24h）
  regime.db    cross_market（regime/dxy/vix）
  account.db   account_snapshots / position_snapshots（资金/持仓兜底；render 另有权威覆盖）
  ledger.db    collection_runs（本 cycle status!=ok → 运行故障进异常段）
  cum_pnl.py   累计收益兜底（render 另有权威覆盖）

资金/累计收益/轮次/耗时/channel 由 render 权威覆盖。本脚本优先用同 cycle
live_facts 的交易前快照作基线，只投影 facts.as_of 后至构建时点的账本成交；payload
带同 cycle positions_projected_cycle 时，render 不再用较旧 position_snapshots 覆盖持仓数。

用法:
  build_push_payload.py [--cycle 2026-07-07T12:00] [--db-root .\\db] [--out-file x.json]
  缺 --cycle 时取 analysis_runs 最新 cycle。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.risk_validator import MAX_PORTFOLIO_IMR_RATIO

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
DEFAULT_DB = r".\db"
BUSINESS_ERROR_REPORTABLE_FROM = "2026-08-14T02:15"
INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM = "2026-08-15T08:00"
INTER_REPORT_WINDOW_MINUTES = 15
INTER_REPORT_RECONCILE_SOURCES = {
    "exchange_fills_reconcile",
    "execution_journal_recovery",
}
INTER_REPORT_DIRECT_FILL_SOURCE = "fills"
INTER_REPORT_DIRECT_TS_SOURCE = "fills.fillTime"

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


def _sl_buffer_pct(side, mark_px, sl_px):
    """现价→SL 触发价的方向感知缓冲%（2026-08-13 双口径显示）。

    long: (mark−sl)/mark；short: (sl−mark)/mark；×100 保留 1 位。
    可为 ≤0（价格已到/越过触发边界的瞬时状态），render 显式标注不吞。
    任一输入缺失/非法 → None（render 回退只显计划口径，绝不伪造）。
    """
    try:
        mark = float(mark_px)
        sl = float(sl_px)
    except (TypeError, ValueError):
        return None
    if mark <= 0 or sl <= 0:
        return None
    s = str(side or "").lower()
    if s == "long":
        return round((mark - sl) / mark * 100, 1)
    if s == "short":
        return round((sl - mark) / mark * 100, 1)
    return None


def _latest_fresh_last(db_root, symbol, as_of, max_age_min=30):
    """as_of 前最近且不早于 max_age_min 的 tick last（market.db，UTC-Z ts）。

    fallback 持仓路径的现价来源；过旧/缺失 → None（宁缺勿假，
    render 回退只显计划口径）。canonical live_facts 路径直接用同轮 markPx，不走这里。
    """
    as_of_dt = _as_cst_datetime(as_of)
    if as_of_dt is None:
        return None
    # 归一为 naive UTC 墙钟，与 tick_snapshots 的 UTC-Z 字符串同域比较。
    as_of_utc = as_of_dt.astimezone(timezone.utc).replace(tzinfo=None)
    row = _one(
        db_root, "market.db",
        "SELECT ts,last FROM tick_snapshots WHERE symbol=? "
        "AND last IS NOT NULL AND ts<=? ORDER BY ts DESC LIMIT 1",
        (symbol, as_of_utc.strftime("%Y-%m-%dT%H:%M:%SZ")))
    if not row or row["last"] is None:
        return None
    try:
        tick_dt = datetime.strptime(str(row["ts"]), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    if (as_of_utc - tick_dt).total_seconds() > max_age_min * 60:
        return None
    try:
        return float(row["last"])
    except (TypeError, ValueError):
        return None


def _open_sl_info(db_root, prof, symbol, avg_px):
    """当前持仓的 SL 计划口径（从建仓 open trade 的 `raw.sl_trigger_px` 确定性读，**不查 API**）。
    根治「SL未挂」误导缺口（2026-07-09）：position_snapshots 不带 SL，render 恒显 SL未挂——
    但 order_executor 开仓时已把附挂 SL 的 slTriggerPx 记进 open trade 的 raw（含 sl_verified）。
    entry 定位同 _hold_min（最后全平后的 open/add）；取最近一笔合格 open 的 sl_trigger_px。
    返回 (sl_pct, sl_px)：sl_pct = |sl_trigger − avg_px| / avg_px × 100 —— 这是
    **开仓口径的计划止损距离**（entry 与 SL 均冻结，随价格不变，语义即如此）；
    现价缓冲另由 _sl_buffer_pct 计算。无 SL 记录 → (None, None)（render 显真『SL未挂』）。"""
    try:
        ap = float(avg_px)
    except (TypeError, ValueError):
        return None, None
    if not ap:
        return None, None
    rows = _rows(db_root, f"{prof}_trades.db",
                 "SELECT ts, action, raw FROM trades WHERE symbol=? ORDER BY ts", (symbol,))
    if not rows:
        return None, None
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
            return round(abs(slt - ap) / ap * 100, 1), slt
    return None, None
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
    # trade_cycles.raw 已经反序列化为 dict；再次 json.loads(dict) 会抛异常并
    # 静默回退空卡，导致 push 又选到 analysis 阶段的旧 decision_card。
    if isinstance(s, (dict, list)):
        return s
    try:
        v = json.loads(s)
        return v if isinstance(v, (dict, list)) else {}
    except Exception:
        return {}


def _open_trade_decisions(trades):
    """Return one frozen decision entry per actual OPEN/ADD symbol+side leg.

    Multiple fills of the same leg share one market decision.  If their frozen
    cards disagree, keep the leg but mark it conflicting so report validation
    fails closed instead of choosing one silently.
    """
    entries = []
    by_leg = {}
    for trade in trades or []:
        if str(trade.get("action") or "").lower() not in _OPEN:
            continue
        symbol = str(trade.get("symbol") or "").strip()
        side = str(trade.get("side") or "").strip().lower()
        key = (symbol, side)
        raw = _loads(trade.get("raw"))
        card = _loads(raw.get("decision_card")) if isinstance(raw, dict) else {}
        card = card if isinstance(card, dict) else {}
        if key not in by_leg:
            entry = {
                "symbol": symbol,
                "side": side,
                "decision_card": card,
                "conflicting_cards": False,
            }
            by_leg[key] = entry
            entries.append(entry)
            continue
        existing = by_leg[key]
        previous = existing.get("decision_card") or {}
        if not previous and card:
            existing["decision_card"] = card
        elif previous and card and previous != card:
            existing["decision_card"] = {}
            existing["conflicting_cards"] = True
    return entries


def _first_open_trade_card(trades):
    """Return the first non-conflicting card frozen on an OPEN/ADD ledger leg."""
    for entry in _open_trade_decisions(trades):
        card = entry.get("decision_card")
        if isinstance(card, dict) and card:
            return card
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
                    "_projected_after_baseline": True,
                    "_projected_open_after_baseline": True,
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
            current["_projected_after_baseline"] = True
            current["_projected_open_after_baseline"] = True
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
                current["_projected_after_baseline"] = True

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


def _trade_business_identity(row: dict) -> dict:
    """Return the stable business identity of one persisted fill row.

    Reports use this identity for a fail-closed build/send attestation.  The
    fields are deliberately limited to immutable execution facts; display-only
    reasoning and optional risk annotations must not make an otherwise
    unchanged report look stale.
    """
    return {
        "id": _ledger_order(row.get("id"), 0),
        "ts": str(row.get("ts") or ""),
        "symbol": str(row.get("symbol") or ""),
        "action": str(row.get("action") or "").lower(),
        "side": str(row.get("side") or "").lower(),
        "sz": _float_or_none(row.get("sz")),
        "fill_px": _float_or_none(row.get("fill_px")),
        "pnl": _float_or_none(row.get("pnl")),
    }


def _business_report_attestation(
    cycle: str, tc: dict, trades: list[dict], *, profile: str = "live"
) -> dict:
    """Seal the exact terminal and fill set represented by this payload."""
    identities = [_trade_business_identity(row) for row in trades]
    body = {
        "schema_version": 1,
        "profile": profile,
        "cycle_id": str(cycle),
        "decision": str(tc.get("decision") or "").strip().lower(),
        "n_orders": int(tc.get("n_orders")),
        "trade_count": len(identities),
        "trades": identities,
    }
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    body["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return body


def _inter_report_window(cycle: str) -> tuple[str, str]:
    """Return the half-open CST report interval ``(cycle-15m, cycle]``."""
    try:
        end = datetime.strptime(str(cycle), "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid report cycle for inter-report fills") from exc
    start = end - timedelta(minutes=INTER_REPORT_WINDOW_MINUTES)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _inter_report_exchange_identity(row: dict) -> dict | None:
    """Return a stable identity for an exchange-proved prior-cycle fill.

    Besides reconciliation rows, a normal executor receipt can land after its
    own cycle's report path has already failed. Such a row is authoritative
    only when it keeps both the OKX fills endpoint provenance and a unique
    order id; otherwise it remains outside the interval attestation.
    """
    raw = _loads(row.get("raw"))
    if not isinstance(raw, dict):
        return None
    ord_ids = []
    candidates = raw.get("ord_ids")
    if not isinstance(candidates, list):
        candidates = []
    candidates = [
        *candidates,
        raw.get("ord_id"),
        raw.get("ordId"),
        *[
            fill.get("ordId") or fill.get("ord_id")
            for fill in (raw.get("fills") or [])
            if isinstance(fill, dict)
        ],
    ]
    for value in candidates:
        token = str(value or "").strip()
        if token and token not in ord_ids:
            ord_ids.append(token)
    reconcile_source = str(raw.get("reconcile_source") or "").strip()
    if reconcile_source in INTER_REPORT_RECONCILE_SOURCES:
        proof_source = reconcile_source
    elif (
        str(raw.get("fill_source") or "").strip()
        == INTER_REPORT_DIRECT_FILL_SOURCE
        and str(raw.get("ts_source") or "").strip()
        == INTER_REPORT_DIRECT_TS_SOURCE
        and ord_ids
    ):
        proof_source = INTER_REPORT_DIRECT_FILL_SOURCE
    else:
        return None
    return {
        "id": _ledger_order(row.get("id"), 0),
        "original_cycle_id": str(row.get("cycle_id") or ""),
        "ts": str(row.get("ts") or ""),
        "symbol": str(row.get("symbol") or ""),
        "action": str(row.get("action") or "").lower(),
        "side": str(row.get("side") or "").lower(),
        "sz": _float_or_none(row.get("sz")),
        "fill_px": _float_or_none(row.get("fill_px")),
        "pnl": _float_or_none(row.get("pnl")),
        "reconcile_source": proof_source,
        "ord_ids": sorted(ord_ids),
    }


def _inter_report_exchange_attestation(
    db_root: str, cycle: str, *, profile: str = "live"
) -> dict:
    """Seal exchange-proved fills omitted from the current-cycle trade set.

    A fill keeps its original business cycle for ledger attribution. Reporting
    it separately in ``(cycle-15m, cycle]`` preserves that truth while ensuring
    a late normal executor receipt or protective/external recovery is visible
    exactly once.
    """
    start, end = _inter_report_window(cycle)
    rows = _rows(
        db_root,
        f"{profile}_trades.db",
        "SELECT id,cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw "
        "FROM trades WHERE ts>? AND ts<=? "
        "AND (cycle_id IS NULL OR cycle_id!=?) ORDER BY ts,id",
        (start, end, cycle),
    )
    identities = []
    for row in rows:
        identity = _inter_report_exchange_identity(row)
        if identity is not None:
            identities.append(identity)
    body = {
        "schema_version": 1,
        "profile": profile,
        "cycle_id": str(cycle),
        "window_start_exclusive_cst": start,
        "window_end_inclusive_cst": end,
        "fill_count": len(identities),
        "fills": identities,
    }
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    body["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return body


def _failure_report_attestation(
    cycle: str, context: dict, intent_safety: dict, *, profile: str = "live"
) -> dict:
    """Seal the proved absence of a business terminal for a failure report."""
    body = {
        "schema_version": 1,
        "profile": profile,
        "cycle_id": str(cycle),
        "terminal": "absent",
        "trade_count": 0,
        "failure_kind": str(
            context.get("failure_kind") or "agent_process_failed"),
        "intent_rows": int(intent_safety.get("intent_rows") or 0),
        "failed_clean_rows": int(
            intent_safety.get("failed_clean_rows") or 0),
        "unsafe_rows": int(intent_safety.get("unsafe_rows") or 0),
    }
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    body["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return body


def _map_decision(dec: str) -> str:
    d = str(dec or "").lower()
    return {"traded": "TRADED", "hold": "HOLD", "skip": "WAIT",
            "degraded": "DEGRADED", "error": "ERROR"}.get(d, "UNKNOWN")


def _verified_adjustment_item(raw: object) -> dict | None:
    """Return normalized protection evidence only after full readback checks."""
    if not isinstance(raw, dict) or raw.get("dryrun") is True:
        return None
    change = raw.get("protection_change")
    state = raw.get("protection_state")
    applied = raw.get("applied")
    path = str(raw.get("path") or "").strip()
    requested = bool(
        isinstance(change, dict)
        and (
            change.get("requested_sl") is not None
            or change.get("requested_tp") is not None
            or change.get("resize_to_full_position") is True
        )
    )
    sl = _float_or_none(applied.get("sl")) if isinstance(applied, dict) else None
    sz = _float_or_none(applied.get("sz")) if isinstance(applied, dict) else None
    if not (
        requested
        and isinstance(state, dict)
        and state.get("ok") is True
        and sl is not None and sl > 0
        and sz is not None and sz > 0
        and path in {
            "amend", "amend_consolidate", "place_new",
            "replace_fallback", "oco_replace",
        }
    ):
        return None
    return raw


def _action_from_cycle(raw: dict, decision: str) -> str:
    """Prefer only a verified executor side effect over the coarse decision."""
    action = str((raw or {}).get("action_taken") or "").strip().upper()
    if action == "ADJUST_PROTECTION":
        if _verified_adjustment_item(raw) is not None:
            return "ADJUST"
        return _map_decision(decision)
    if action == "ADJUST":
        # Historical free-form alias: without executor readback it is a HOLD,
        # not evidence that an exchange protection order changed.
        return _map_decision(decision)
    allowed = {
        "OPEN_LONG", "OPEN_SHORT", "CLOSE", "STOP_LOSS",
        "HOLD", "WAIT", "NONE", "REDUCE", "ADD",
    }
    return action if action in allowed else _map_decision(decision)


def _upstream_failure_card(context: dict) -> dict:
    kind = str(context.get("failure_kind") or "agent_process_failed")
    if context.get("stage") == "collection":
        missing = ",".join(
            str(item) for item in (
                context.get("missing_required_sources") or [])) or "unknown"
        return {
            "direction_evidence": [
                f"必需采集源未齐（{missing}），没有完整的当轮市场方向证据"
            ],
            "opposing_evidence": [
                "Agent 与 executor 路径均未启动，禁止用旧数据推断方向或成交"
            ],
            "execution_conditions": "fail-closed：本轮禁止 OPEN/ADD/下单",
            "invalidation_point": "不适用；该卡只记录采集失败闭环，不构成交易判断",
            "risk_reward": "不适用；零新增敞口",
            "portfolio_impact": "零新增风险；既有仓位事实仅作只读展示",
            "historical_experience": {
                "matched_wins": [],
                "matched_losses": [],
                "missed_opportunities": [],
                "usage": "not_used",
                "reason": "采集闸失败时禁止用历史经验替代当轮市场证据",
            },
            "agent_judgement": (
                f"Agent 未启动；系统按 fail-closed 记 WAIT（{kind}）"),
            "reference_overrides": [],
        }
    checks = (
        (context.get("business_check") or {}).get("checks")
        if isinstance(context.get("business_check"), dict) else []
    )
    checks = checks if isinstance(checks, list) else []
    analysis_found = any(
        isinstance(item, dict)
        and item.get("db") == "analysis.db"
        and item.get("table") == "analysis_runs"
        and item.get("found") is True
        for item in checks
    )
    if analysis_found:
        direction_evidence = [
            "analysis.db 已形成分析证据，但分析候选不等于交易执行裁决"
        ]
        opposing_evidence = [
            "live_trades.db.trade_cycles 交易终态缺失，禁止推断已 HOLD 或已执行"
        ]
        judgement = (
            f"Agent 已形成分析，但未形成交易终态；系统按 fail-closed 记 WAIT（{kind}）")
    else:
        direction_evidence = ["上游 live 阶段失败，未形成可执行方向证据"]
        opposing_evidence = ["缺少有效分析/交易业务终态，禁止推断方向"]
        judgement = (
            f"Agent 未形成当轮判断；系统按 fail-closed 记 WAIT（{kind}）")
    return {
        "direction_evidence": direction_evidence,
        "opposing_evidence": opposing_evidence,
        "execution_conditions": "fail-closed：本轮禁止 OPEN/ADD/下单",
        "invalidation_point": "不适用；该卡只记录失败闭环，不构成交易判断",
        "risk_reward": "不适用；零新增敞口",
        "portfolio_impact": "零新增风险；既有仓位事实仅作只读展示",
        "historical_experience": {
            "matched_wins": [],
            "matched_losses": [],
            "missed_opportunities": [],
            "usage": "not_used",
            "reason": "上游失败时禁止用历史经验替代当轮分析",
        },
        "agent_judgement": judgement,
        "reference_overrides": [],
    }


def _trade_terminal_valid(tc: dict) -> bool:
    decision = str(tc.get("decision") or "").strip().lower()
    try:
        n_orders = int(tc.get("n_orders"))
    except (TypeError, ValueError):
        return False
    return (
        (decision == "traded" and n_orders > 0)
        or (decision in {"hold", "skip"} and n_orders == 0)
    )


def _compact_execution_token(value, limit: int = 180) -> str:
    rendered = " ".join(str(value or "-").replace("\r", " ")
                        .replace("\n", " ").split())
    return rendered[:limit]


def _verified_adjustment_execution(raw: dict, decision: str) -> dict | None:
    """Render only executor-verified protection changes as an execution fact."""
    batch = raw.get("protection_changes") if isinstance(raw, dict) else None
    if isinstance(batch, list) and batch:
        candidates = batch
    elif _action_from_cycle(raw, decision) == "ADJUST":
        candidates = [raw]
    else:
        return None
    verified = [_verified_adjustment_item(item) for item in candidates]
    if any(item is None for item in verified):
        return None
    segments = []
    first_sl = "-"
    for item in verified:
        assert isinstance(item, dict)
        applied = item.get("applied") if isinstance(item.get("applied"), dict) else {}
        state = item.get("protection_state") \
            if isinstance(item.get("protection_state"), dict) else {}
        rows = [row for row in state.get("rows") or [] if isinstance(row, dict)]
        readback_state = next(
            (str(row.get("state")) for row in rows if row.get("state")),
            "verified",
        )
        symbol = str(item.get("symbol") or "-")
        path = str(item.get("path") or "-")
        sl = _px(applied.get("sl"))
        first_sl = sl if first_sl == "-" else first_sl
        tp = _px(applied.get("tp")) if applied.get("tp") is not None else "-"
        sz = _num_or_dash(applied.get("sz"))
        algo_id = str(applied.get("algoId") or "-")
        segments.append(
            f"{symbol} path={path} sz={sz} SL={sl} TP={tp} "
            f"algoId={algo_id} readback=verified/{readback_state}"
        )
    shown = "; ".join(segments[:4])
    if len(segments) > 4:
        shown += f"; +{len(segments) - 4} more"
    return {
        "result": (
            f"ADJUST_PROTECTION batch={len(segments)} no_fill {shown} "
            "exchange_side_effect=protection_only"
        ),
        "fill_px": "no_fill",
        "stop_px": first_sl,
    }


def _require_business_error_terminal(
    db_root: str,
    cycle: str,
    tc: dict,
    raw: dict,
    trades: list[dict],
) -> dict:
    """Validate a zero-side-effect ERROR/DEGRADED terminal for reporting."""
    if str(cycle) < BUSINESS_ERROR_REPORTABLE_FROM:
        raise ValueError("business error reporting not active for this cycle")
    decision = str(tc.get("decision") or "").strip().lower()
    try:
        n_orders = int(tc.get("n_orders"))
    except (TypeError, ValueError) as exc:
        raise ValueError("business error terminal n_orders invalid") from exc
    if decision not in {"error", "degraded"} or n_orders != 0 or trades:
        raise ValueError("business error report requires zero orders/trades")
    if str(raw.get("status") or "").strip().lower() != decision:
        raise ValueError("business error raw status mismatch")
    try:
        if int(raw.get("n_orders")) != 0:
            raise ValueError("business error raw n_orders must be zero")
    except (TypeError, ValueError) as exc:
        raise ValueError("business error raw n_orders must be zero") from exc
    action = str(raw.get("action_taken") or "").strip().upper()
    allowed = {"REJECT"} if decision == "error" else {"REJECT", "WAIT", "HOLD"}
    if action not in allowed:
        raise ValueError("business error raw action is not reportable")
    raw_trades = raw.get("trades")
    if not isinstance(raw_trades, list) or raw_trades:
        raise ValueError("business error raw trades must be an empty list")
    reason = (
        raw.get("reject_reason")
        or next((item for item in raw.get("errors") or [] if item), None)
        or tc.get("note")
    )
    if not reason:
        raise ValueError("business error terminal missing reason")
    intent_safety = _require_failure_intents_clean(db_root, cycle)
    return {
        **intent_safety,
        "decision": decision,
        "raw_action": action,
        "reason": str(reason),
    }


def _business_error_execution(
    raw: dict,
    decision: str,
    symbol: str,
    safety: dict,
) -> dict:
    reason = _compact_execution_token(
        raw.get("reject_reason") or safety.get("reason"), 90)
    detail = _compact_execution_token(
        raw.get("reject_detail")
        or next((item for item in raw.get("errors") or [] if item), "-"),
        180,
    )
    return {
        "result": (
            f"{str(raw.get('action_taken') or 'REJECT').upper()} {symbol} "
            f"no_fill orders=0 exchange_side_effect=none "
            f"reason={reason} detail={detail}"
        ),
        "fill_px": "no_fill",
        "stop_px": "-",
    }


def _require_failure_intents_clean(db_root: str, cycle: str) -> dict:
    """Prove no exchange-submitted intent can be hidden by a WAIT report."""
    try:
        con = connect(db_root, "ledger.db")
        try:
            rows = [dict(row) for row in con.execute(
                "SELECT state,ord_id,submitted_at,completed_at "
                "FROM execution_intents WHERE profile='live' AND cycle_id=?",
                (cycle,),
            )]
        finally:
            con.close()
    except Exception as exc:
        raise ValueError(
            "failure report requires readable execution_intents") from exc
    unsafe = [
        row for row in rows
        if (
            str(row.get("state") or "").strip().lower() != "failed_clean"
            or row.get("ord_id") not in (None, "")
            or row.get("submitted_at") not in (None, "")
            or row.get("completed_at") not in (None, "")
        )
    ]
    if unsafe:
        states = sorted({
            str(row.get("state") or "<missing>") for row in unsafe})
        raise ValueError(
            "failure report blocked by non-clean execution intents: "
            + ",".join(states))
    return {
        "intent_rows": len(rows),
        "failed_clean_rows": len(rows),
        "unsafe_rows": 0,
        "status": "PASSED",
    }


def build(
    db_root: str,
    cycle: str,
    now: datetime | None = None,
    upstream_failure: dict | None = None,
) -> dict:
    now = now or datetime.now(CST)
    hhmm = cycle.split("T")[1] if "T" in cycle else now.strftime("%H:%M")
    failure_report = upstream_failure is not None
    if failure_report:
        valid_stage = (
            isinstance(upstream_failure, dict)
            and upstream_failure.get("stage") in {"live", "collection"}
            and upstream_failure.get("cycle_id") == cycle
            and upstream_failure.get("status") == "failed"
            and upstream_failure.get("profile_lease_released") is True
            and int(upstream_failure.get("returncode") or 0) != 0
        )
        collection_safe = (
            upstream_failure.get("stage") != "collection"
            or (
                upstream_failure.get("same_cycle_live_dispatched") is False
                and bool(upstream_failure.get("missing_required_sources"))
                and bool(upstream_failure.get("collection_receipt_sha256"))
            )
        )
        if not valid_stage or not collection_safe:
            raise ValueError("invalid upstream failure contract")

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
                 "ORDER BY CASE action WHEN 'close' THEN 0 WHEN 'reduce' THEN 0 "
                 "WHEN 'adjust_protection' THEN 0 WHEN 'open_long' THEN 1 "
                 "WHEN 'open_short' THEN 1 WHEN 'hold' THEN 2 ELSE 3 END,rowid",
                 (cycle,))
    top_sig = sigs[0] if sigs else {}
    open_cands = [s for s in sigs if str(s.get("action") or "").startswith("open")]

    # ── 交易段（双盘）─────────────────────────────────────
    # 2026-08-06 demo 全量下线：只剩 live 一本账。
    books = {}
    for prof in ("live",):
        tc_row = _one(db_root, f"{prof}_trades.db",
                      "SELECT decision,n_orders,equity,note,raw FROM trade_cycles WHERE cycle_id=?",
                      (cycle,))
        tc = tc_row or {}
        tr = _rows(db_root, f"{prof}_trades.db",
                   "SELECT id,ts,symbol,action,side,sz,fill_px,lev,margin,notional,pnl,reasoning,raw "
                   "FROM trades WHERE cycle_id=? ORDER BY id", (cycle,))
        tc_raw = _loads(tc.get("raw"))
        # 2026-08-08 单笔保证金闸：从回执 raw.trades 透传单笔审计字段
        # （执行时 equity 口径，比 payload 侧重算更权威；缺失静默省略）。
        _receipt_trades = [t for t in (tc_raw.get("trades") or [])
                           if isinstance(t, dict)]
        for _row in tr:
            _match = next(
                (t for t in _receipt_trades
                 if t.get("symbol") == _row.get("symbol")
                 and t.get("action") == _row.get("action")
                 and t.get("side") == _row.get("side")), None)
            if _match:
                for _k in ("single_order_imr_ratio",
                           "max_single_order_imr_ratio",
                           "single_order_cap_breached", "risk_clamped",
                           "single_order_risk_pct_equity",
                           "max_single_order_risk_pct_equity",
                           "single_order_risk_cap_breached"):
                    if _match.get(_k) is not None:
                        _row[_k] = _match.get(_k)
        # present=False 专指「本轮 trade_cycles 行尚未落库」，与「行在但 decision 未知」
        # 严格区分：缺行不得当成 UNKNOWN 动作——那会让 render 的标题校验挂掉。
        books[prof] = {"tc": tc, "trades": tr, "raw": tc_raw,
                       "present": tc_row is not None}

    all_trades = books["live"]["trades"]
    inter_report_exchange_attestation = _inter_report_exchange_attestation(
        db_root, cycle)
    business_report_attestation = (
        _business_report_attestation(
            cycle, books["live"]["tc"], all_trades)
        if books["live"]["present"]
        else None
    )
    live_decision = str(
        books["live"]["tc"].get("decision") or "").strip().lower()
    business_error_safety = None
    if live_decision in {"error", "degraded"}:
        business_error_safety = _require_business_error_terminal(
            db_root,
            cycle,
            books["live"]["tc"],
            books["live"]["raw"],
            all_trades,
        )
    failure_intent_safety = None
    if failure_report:
        if books["live"]["present"]:
            # Any persisted business-cycle row is more authoritative than a
            # synthetic runner-failure report.  Invalid/partial rows remain an
            # explicit fail-closed gap and must not be relabelled as WAIT.
            raise ValueError(
                "upstream failure report forbidden when live trade terminal exists")
        if all_trades:
            raise ValueError(
                "upstream failure report blocked by persisted trade rows")
        failure_intent_safety = _require_failure_intents_clean(db_root, cycle)
        business_report_attestation = _failure_report_attestation(
            cycle, upstream_failure, failure_intent_safety)

    # ── headline action / symbol / confidence ─────────────
    action = _action_from_trades(all_trades)
    if action in (None, "TRADED"):
        # 只由已落库的盘推导头条动作：未落库的盘没有动作可言，拿它顶 UNKNOWN
        # 会把一轮干净的 live HOLD 报成未知。
        decs = [_action_from_cycle(
                    books[p]["raw"], books[p]["tc"].get("decision"))
                for p in ("live",) if books[p]["present"]]
        action = next(
            (label for label in (
                "ERROR", "DEGRADED", "UNKNOWN", "ADJUST", "HOLD", "WAIT",
                "NONE", "TRADED")
             if label in decs),
            "UNKNOWN",
        )

    open_trade = next(
        (row for row in all_trades
         if str(row.get("action") or "").lower() in _OPEN),
        None,
    )
    open_trade_symbol = str((open_trade or {}).get("symbol") or "").strip()
    open_trade_side = str((open_trade or {}).get("side") or "").strip().lower()
    open_trade_decisions = _open_trade_decisions(all_trades)
    open_trade_card = _first_open_trade_card(all_trades)
    if all_trades:
        syms, seen = [], set()
        for t in all_trades:
            s = _short(t.get("symbol"))
            if s and s not in seen:
                seen.add(s); syms.append(s)
        symbol = "/".join(syms[:3])
        card_symbol = _short((open_trade or all_trades[0]).get("symbol"))
        conf_sig = next(
            (s for s in sigs if _short(s.get("symbol")) == card_symbol),
            top_sig,
        )
    else:
        symbol = _short(top_sig.get("symbol")) or "BTC"
        conf_sig = top_sig
    card = open_trade_card or _loads(conf_sig.get("decision_card"))
    if not isinstance(card, dict):
        card = {}
    if failure_report:
        action = "WAIT"
        card = _upstream_failure_card(upstream_failure)
    confidence = "-"  # 旧推送键保留；新协议不显示或消费评分

    # ── summary（确定性组装，含市场实质）────────────────
    _ACTION_CN = {"HOLD": "HOLD 维持", "WAIT": "WAIT 观望", "OPEN_LONG": "开多",
                  "OPEN_SHORT": "开空", "CLOSE": "平仓", "STOP_LOSS": "止损",
                  "ADJUST": "调整", "ADD": "加仓", "REDUCE": "减仓",
                  "TRADED": "已成交", "DEGRADED": "DEGRADED 降级",
                  "ERROR": "ERROR 失败", "UNKNOWN": "UNKNOWN 未知",
                  "PENDING": "PENDING 未落库"}
    def _action_cn(value):
        return "/".join(_ACTION_CN.get(part, part) for part in str(value).split("/"))

    action_cn = _action_cn(action)
    # 两盘动作逐盘推导（成交行优先、无成交回退 decision）；
    # 2026-08-06 demo 全量下线：只剩实盘一段，不再有双盘一致/分歧之分。
    book_acts = {"live": (
        "PENDING" if not books["live"]["present"]
        else (_action_from_trades(books["live"]["trades"])
              or _action_from_cycle(
                  books["live"]["raw"],
                  books["live"]["tc"].get("decision"))))}
    head_cn = f"实盘{action_cn}"
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
    if failure_report:
        kind = upstream_failure["failure_kind"]
        failure_stage = upstream_failure.get("stage")
        failure_checks = (
            (upstream_failure.get("business_check") or {}).get("checks")
            if isinstance(upstream_failure.get("business_check"), dict)
            else []
        )
        failure_checks = failure_checks if isinstance(
            failure_checks, list) else []
        failure_analysis_found = any(
            isinstance(item, dict)
            and item.get("db") == "analysis.db"
            and item.get("table") == "analysis_runs"
            and item.get("found") is True
            for item in failure_checks
        )
        if failure_stage == "collection":
            missing = ",".join(
                str(item) for item in (
                    upstream_failure.get("missing_required_sources") or []))
            summary = (
                f"实盘WAIT：采集闸失败（{kind}，缺失={missing or 'unknown'}），"
                "Agent/执行器未启动；禁止用旧数据形成交易判断，零新增风险")
        elif failure_analysis_found:
            summary = (
                f"实盘WAIT：上游 live 阶段失败（{kind}），分析已落库但交易业务终态"
                "缺失；禁止把分析候选冒充执行裁决，零新增风险")
        else:
            summary = (
                f"实盘WAIT：上游 live 阶段失败（{kind}），本轮未形成有效分析/"
                "交易业务终态；禁止下单，零新增风险")
    elif business_error_safety is not None:
        summary = (
            f"实盘{action}：本轮形成显式业务拒绝/错误终态，"
            "orders=0、exchange_side_effect=none；保留原始失败原因并继续报告")

    # ── decision.reason（主体=headline 币 analyst 理由，恒在且最相关；叠成交理由+宏观+校准+教训）──
    live_raw = books["live"]["raw"]
    reason_bits = []
    live_facts = live_raw.get("live_facts")
    facts_authoritative = (
        isinstance(live_facts, dict) and live_facts.get("status") == "ok"
    )
    if facts_authoritative:
        trade_card = _loads(live_raw.get("decision_card"))
        if isinstance(trade_card, dict) and trade_card and not open_trade_card:
            # analysis card 生成于实时 facts 之前；交易回执 card 才能引用已核验现仓。
            card = trade_card
    if facts_authoritative:
        fact_parts = []
        for item in live_facts.get("positions") or []:
            if not isinstance(item, dict):
                continue
            sl = item.get("sl") if isinstance(item.get("sl"), dict) else {}
            fact_parts.append(
                f"{_short(item.get('instId'))} {item.get('posSide')} "
                f"{item.get('contracts')}张(ctVal={item.get('ctVal')}) "
                f"持有{item.get('position_age_hours')}h mark={item.get('markPx')} "
                f"SL={sl.get('trigger_px') if sl.get('verified') else '未核验'}"
            )
        ratio = (live_facts.get("balance") or {}).get(
            "current_portfolio_imr_ratio"
        )
        if ratio is not None:
            fact_parts.append(f"IMR/权益={float(ratio) * 100:.2f}%")
        if fact_parts:
            reason_bits.append(
                f"交易所事实({live_facts.get('as_of')})：" + "；".join(fact_parts)
            )
    if card.get("agent_judgement"):
        reason_bits.append(f"Agent裁决：{card['agent_judgement']}")
    head_reason = conf_sig.get("reasoning")
    if head_reason and not facts_authoritative:
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
               or [])  # trader raw 键逐轮变名，全兜
    if isinstance(lessons, str):
        lessons = [lessons]
    lesson_strs = [s for s in (_fmt_lesson(x) for x in lessons[:3]) if s][:2]
    if lesson_strs:
        reason_bits.append("教训：" + "；".join(lesson_strs))
    reason = " | ".join(reason_bits) or "regime、技术面、新闻、经验库综合确认。"
    if failure_report:
        if upstream_failure.get("stage") == "collection":
            reason = (
                "采集器已形成唯一失败终局，必需源账本仍未齐；系统独立确认 Agent/"
                "executor 未启动，且分析、交易周期、成交均不存在。"
                f"failure_kind={upstream_failure['failure_kind']}，"
                "只生成失败闭环报告，不补采、不补写业务账本、不重派、不下单。")
        elif failure_analysis_found:
            reason = (
                "Agent 分析已落库，但交易终态未形成；监督器已确认 live failed 且租约释放。"
                f"failure_kind={upstream_failure['failure_kind']}，"
                "系统只生成失败闭环报告，不补写交易账本、不重派、不下单。")
        else:
            reason = (
                "Agent 未形成当轮判断；监督器已确认 live failed 且租约释放。"
                f"failure_kind={upstream_failure['failure_kind']}，"
                "系统只生成失败闭环报告，不补写分析/交易账本、不重派、不下单。")
    elif business_error_safety is not None:
        reason_bits.append(
            "业务错误闭环："
            f"action={business_error_safety['raw_action']}，"
            f"reason={business_error_safety['reason']}，"
            "execution intents 已验证无交易所提交或完成痕迹。")
        reason = " | ".join(reason_bits)

    # ── 经验引用（market_summary.quant.playbook_matches 主源；experiences_cited 兜底）──
    play = {"play_id": "-", "play_title": "-", "hit_rate": "-",
            "avg_return": "-", "uncertainty": "-"}
    pbm = quant.get("playbook_matches") if isinstance(quant, dict) else None
    if isinstance(pbm, list) and pbm and isinstance(pbm[0], dict):
        p0 = pbm[0]
        play["play_id"] = p0.get("id", "-")
        play["play_title"] = str(p0.get("note") or p0.get("summary") or p0.get("title") or "-")[:60]
        play["hit_rate"] = _num_or_dash(p0.get("wr"))
    cited = (live_raw.get("experiences_cited") or [])
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
            row = {"symbol": t.get("symbol"), "action": t.get("action"),
                   "side": t.get("side"), "sz": t.get("sz"),
                   "fill_px": _px(t.get("fill_px")), "lev": t.get("lev"),
                   "pnl": _r2(t.get("pnl"))}
            # 2026-08-08 单笔保证金审计透传（字段缺失省略，历史 payload 形状不变）
            for k in ("single_order_imr_ratio", "max_single_order_imr_ratio",
                      "single_order_cap_breached", "risk_clamped",
                      "single_order_risk_pct_equity",
                      "max_single_order_risk_pct_equity",
                      "single_order_risk_cap_breached"):
                if t.get(k) is not None:
                    row[k] = t.get(k)
            out.append(row)
        return out

    # ── 持仓段（as-of 最近快照 + 其后全部已落账成交，剔哨兵）──
    def _positions_from_live_facts():
        facts = books["live"].get("raw", {}).get("live_facts")
        if not isinstance(facts, dict) or facts.get("status") != "ok":
            return None
        if str(facts.get("cycle_id") or "") != str(cycle):
            return None
        facts_as_of = str(facts.get("as_of") or "")
        if _as_cst_datetime(facts_as_of) is None:
            return None
        fact_positions = facts.get("positions")
        if not isinstance(fact_positions, list):
            return None
        balance = facts.get("balance") if isinstance(
            facts.get("balance"), dict
        ) else {}
        equity = _float_or_none(balance.get("totalEq"))
        out = []
        for item in fact_positions:
            if not isinstance(item, dict):
                return None
            sl = item.get("sl") if isinstance(item.get("sl"), dict) else {}
            avg_px = _float_or_none(item.get("avgPx"))
            sl_px = _float_or_none(sl.get("trigger_px")) if sl.get("verified") else None
            imr = _float_or_none(item.get("position_imr"))
            age = _float_or_none(item.get("position_age_hours"))
            sl_pct = None
            if avg_px and sl_px:
                sl_pct = round(abs(sl_px - avg_px) / avg_px * 100, 1)
            # 2026-08-13 双口径：facts 同轮 markPx 直接给现价缓冲（零额外 I/O）；
            # markPx 缺失才回退最近 fresh tick，仍取不到 → None（只显计划口径）。
            mark_px = _float_or_none(item.get("markPx"))
            if mark_px is None and sl_px is not None:
                mark_px = _latest_fresh_last(db_root, item.get("instId"), now)
            sl_buffer = _sl_buffer_pct(item.get("posSide"), mark_px, sl_px) \
                if sl_px is not None else None
            out.append({
                "symbol": item.get("instId"),
                "side": item.get("posSide"),
                "sz": item.get("contracts"),
                "avgPx": _px(avg_px),
                "lev": item.get("lever"),
                "upl": _r2(item.get("upl")),
                "upl_pct_initial_margin": _float_or_none(
                    item.get("upl_pct_initial_margin")),
                "margin_return_review_at_or_above_50pct": bool(
                    item.get("margin_return_review_at_or_above_50pct")),
                "secured_profit_at_stop_usdt": _r2(
                    item.get("secured_profit_at_stop_usdt")),
                "profit_retention_at_stop_pct_of_current_upl": (
                    _float_or_none(item.get(
                        "profit_retention_at_stop_pct_of_current_upl"))
                ),
                "giveback_to_stop_pct_of_current_upl": _float_or_none(
                    item.get("giveback_to_stop_pct_of_current_upl")),
                "notional_usd": _r2(item.get("mark_notional_usdt")),
                "margin_usd": _r2(imr),
                "margin_pct": (
                    round(imr / equity * 100, 1)
                    if imr is not None and equity else None
                ),
                "hold_min": round(age * 60) if age is not None else None,
                "sl_pct": sl_pct, "sl_px": _px(sl_px),
                "sl_buffer_pct": sl_buffer,
                "profile": "live",
            })
        return {"rows": out, "as_of": facts_as_of}

    def _complete_projected_fact_rows(rows, prof):
        """Complete only rows changed after the immutable live-facts snapshot."""
        eqr = _one(
            db_root, "account.db",
            "SELECT totalEq FROM account_snapshots WHERE profile=? "
            "ORDER BY ts DESC,rowid DESC LIMIT 1", (prof,))
        eq = _float_or_none((eqr or {}).get("totalEq"))
        out = []
        for source in rows:
            row = dict(source)
            projected = row.pop("_projected_after_baseline", False) is True
            projected_open = row.pop(
                "_projected_open_after_baseline", False) is True
            if not projected:
                out.append(row)
                continue
            symbol = str(row.get("symbol") or "")
            side = str(row.get("side") or "").lower()
            qty = _float_or_none(row.get("sz"))
            avg_px = _float_or_none(row.get("avgPx"))
            lev = _float_or_none(row.get("lev"))
            mark_px = _latest_fresh_last(db_root, symbol, now)
            ctv = _one(
                db_root, "market.db",
                "SELECT ctVal FROM instruments_cache WHERE instId=?", (symbol,))
            ct_val = _float_or_none((ctv or {}).get("ctVal"))
            reference_px = mark_px if mark_px is not None else avg_px
            notional = (
                qty * ct_val * reference_px
                if qty is not None and ct_val is not None
                and reference_px is not None else None
            )
            margin = notional / lev if notional is not None and lev else None
            upl = _float_or_none(row.get("upl"))
            if (
                mark_px is not None and avg_px is not None
                and qty is not None and ct_val is not None
                and side in {"long", "short"}
            ):
                signed_move = mark_px - avg_px
                upl = signed_move * qty * ct_val * (1 if side == "long" else -1)
            sl_px = _float_or_none(row.get("sl_px"))
            sl_pct = _float_or_none(row.get("sl_pct"))
            if projected_open or sl_px is None:
                sl_pct, sl_px = _open_sl_info(
                    db_root, prof, symbol, avg_px)
            out.append({
                "symbol": symbol,
                "side": side,
                "sz": qty,
                "avgPx": _px(avg_px),
                "lev": lev,
                "upl": _r2(upl),
                "notional_usd": _r2(notional),
                "margin_usd": _r2(margin),
                "margin_pct": (
                    round(margin / eq * 100, 1)
                    if margin is not None and eq else None
                ),
                "hold_min": _hold_min(db_root, prof, symbol, now),
                "sl_pct": sl_pct,
                "sl_px": _px(sl_px),
                "sl_buffer_pct": _sl_buffer_pct(side, mark_px, sl_px)
                if sl_px is not None else None,
                "profile": prof,
            })
        return out

    def _positions(prof):
        ledger_trades = _rows(
            db_root, f"{prof}_trades.db",
            "SELECT id AS ledger_rowid,ts,cycle_id,symbol,action,side,sz,fill_px,lev "
            "FROM trades ORDER BY id",
        )
        if prof == "live":
            canonical = _positions_from_live_facts()
            if canonical is not None:
                rows = _project_positions_through_trades(
                    canonical["rows"], ledger_trades,
                    canonical["as_of"], as_of=now)
                return _complete_projected_fact_rows(rows, prof)
        snapshot_ts, rows = _latest_position_snapshot(db_root, prof, now)
        # 只把这些行用于持仓内存投影；headline / execution / trades 段仍严格展示
        # 当前 cycle，避免把交错 cycle 的成交误报为本轮执行。
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
            sl_pct, sl_px = _open_sl_info(db_root, prof, r["symbol"], r["avgPx"])
            sl_buffer = _sl_buffer_pct(
                r["side"],
                _latest_fresh_last(db_root, r["symbol"], now),
                sl_px,
            ) if sl_px else None
            out.append({"symbol": r["symbol"], "side": r["side"], "sz": r["sz"],
                        "avgPx": _px(r["avgPx"]), "lev": r["lev"], "upl": _r2(r["upl"]),
                        "notional_usd": _r2(notional), "margin_usd": _r2(margin),
                        "margin_pct": margin_pct,
                        "hold_min": _hold_min(db_root, prof, r["symbol"], now),
                        "sl_pct": sl_pct, "sl_px": _px(sl_px),
                        "sl_buffer_pct": sl_buffer,
                        "profile": prof})
        return out

    live_pos = _positions("live")
    positions = live_pos

    # ── 资产段兜底（render 权威覆盖）──────────────────────
    def _assets(prof):
        a = _one(db_root, "account.db",
                 "SELECT totalEq,availBal,upl FROM account_snapshots "
                 "WHERE profile=? ORDER BY ts DESC,rowid DESC LIMIT 1",
                 (prof,)) or {}
        cum = None
        try:
            sys.path.insert(0, r".\scripts")
            import cum_pnl
            info = cum_pnl.cum_for(db_root, prof)
            cum = info.get("cum_pnl") if info.get("ok") else None
        except Exception:
            pass
        n_pos = len(live_pos)
        return {"equity": a.get("totalEq"), "realized_pnl": cum, "positions": n_pos,
                "availBal": a.get("availBal")}

    assets = {"live": _assets("live")}

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
                selected.get("max_portfolio_imr_ratio")
                or MAX_PORTFOLIO_IMR_RATIO
            ),
            "portfolio_imr_ratio_unit": selected.get(
                "portfolio_imr_ratio_unit", "fraction"),
            "portfolio_imr_source": selected.get(
                "portfolio_imr_source", "account.balance.imr"),
            "current_portfolio_imr_source": selected.get(
                "portfolio_imr_source"),
        }

    #  随 2026-08-06 demo 全量下线移除：它提取 Demo OPEN
    # 的交易所实时 max-size 留痕，对应的 sizing_policy 分支已从 risk_validator 删除。
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
        "available_margin": {
            "live_usdt": assets["live"].get("availBal"),
        },
        "lev": live_lev if live_lev != 0 else "-",
        "side_pct": _side_pct(live_pos),
        "position_count": len(live_pos),
        "status": (
            "BLOCKED_UPSTREAM_FAILURE"
            if failure_report
            else (
                "PASS"
                if str(books["live"]["tc"].get("decision") or "").lower()
                in {"traded", "hold", "skip"}
                else str(
                    books["live"]["tc"].get("decision") or "UNKNOWN"
                ).upper()
            )
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
    for p in ("live",):
        if not books[p]["present"]:
            if failure_report:
                if upstream_failure.get("stage") == "collection":
                    exceptions.append({
                        "name": "collection_gate",
                        "status": "failed",
                        "detail": (
                            "采集失败终态："
                            f"{upstream_failure['failure_kind']}；缺失必需源="
                            + ",".join(
                                str(item) for item in upstream_failure.get(
                                    "missing_required_sources") or [])
                            + "；Agent 未派发，报告按 WAIT/零新增风险闭环"),
                    })
                else:
                    exceptions.append({
                        "name": f"{p}_trader",
                        "status": "failed",
                        "detail": (
                            "监督器失败终态："
                            f"{upstream_failure['failure_kind']}；"
                            "未补写 trade_cycles，报告按 WAIT/零新增风险闭环"),
                    })
            else:
                # 正常报告路径仍把缺行视为异常，绝不静默降级。
                exceptions.append({
                    "name": f"{p}_trader", "status": "pending",
                    "detail": "本轮 trade_cycles 未落库——push 闸要求 live 落库，"
                              "出现即为异常"})
            continue
        decision = str(books[p]["tc"].get("decision") or "").lower()
        if decision in {"degraded", "error"}:
            exceptions.append({"name": f"{p}_trader", "status": decision,
                               "detail": str(books[p]["tc"].get("note"))[:120]})

    # ── HH:01 宏观 + 全市场段（:00 整点，恢复 agent 每整点带的段）──
    # 键名 is_hh01 为历史遗留（旧世界慢采 :02、扩展段标 HH:01）；2026-08-08 采集
    # 整并后慢采归 :00 hourly，判定一直是 :00。键名保留兼容历史归档 payload 重渲染，
    # 展示文案已改 HH:00。
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

    execution = {
        "result": "", "fill_px": "-", "stop_px": "-",
        "db_rows_live": len(books["live"]["trades"]),
    }
    adjustment_execution = _verified_adjustment_execution(
        books["live"]["raw"], books["live"]["tc"].get("decision"))
    if adjustment_execution is not None:
        execution.update(adjustment_execution)
    if business_error_safety is not None:
        execution.update(_business_error_execution(
            books["live"]["raw"], live_decision, symbol,
            business_error_safety))

    return {
        "cycle_id": cycle,
        "hhmm": hhmm,
        "cycle_count": 0,          # render 权威覆盖
        "cycle_duration_s": 0,     # render 权威覆盖
        "channel": "live",        # render 硬编码覆盖
        "symbol": symbol or "BTC",
        "confidence": confidence,
        "summary": summary,
        "action_taken": action,
        "decision": {
            "summary": summary,
            "reason": reason,
            "origin": (
                "system_failure_fallback" if failure_report
                else "business_error_terminal"
                if business_error_safety is not None
                else "exchange_reconcile_after_business_terminal"
                if (
                    card
                    and live_raw.get("business_context_preserved") is True
                    and live_raw.get("reconcile_source") in {
                        "exchange_fills_reconcile",
                        "execution_journal_recovery",
                    }
                )
                else "business_terminal"),
            "decision_protocol": "decision_card_v1" if card else "legacy_score",
            "decision_card": card,
            "multitimeframe_analysis": (
                card.get("multitimeframe_analysis")
                if isinstance(card.get("multitimeframe_analysis"), dict)
                else None
            ),
            "multitimeframe_expected_symbol": open_trade_symbol or None,
            "multitimeframe_expected_side": (
                open_trade_side if open_trade_side in {"long", "short"} else None
            ),
            "multitimeframe_analyses": open_trade_decisions,
            **play,
        },
        "execution": execution,
        "risk": risk,
        "trades": {"live": _fmt_trades(books["live"]["trades"])},
        **({
            "business_report_attestation": business_report_attestation,
        } if business_report_attestation is not None else {}),
        "inter_report_exchange_attestation": (
            inter_report_exchange_attestation),
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
        **({
            "report_mode": "upstream_failure",
            "upstream_failure": dict(upstream_failure),
            "execution_intent_safety": failure_intent_safety,
            "production_database_writes": 0,
            "orders_placed": 0,
        } if failure_report else {}),
        **({
            "report_mode": "business_error",
            "execution_intent_safety": business_error_safety,
            "production_database_writes": 0,
            "orders_placed": 0,
        } if business_error_safety is not None else {}),
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
