# -*- coding: utf-8 -*-
"""V2.0 §8.5 —— 交易经验写入（由 trades_writer 挂钩，live+demo 同写 account.db）。

每笔交易完成写结构化经验入 account.db.trade_experiences（LLM 长期记忆）。开仓写 open 行
（含决策背景 + experience_vector），平仓 UPDATE 该行为 closed（补 pnl_pct/hold_hours/hit_1R）。
caller 提供 account.db 连接并负责 commit；交易账与经验跨库，经验失败不阻塞交易记账。

`experience_vector` 由 `_simutil.experience_vector` 编码（与 find_similar_experience 同空间）。
本模块用 caller 传入的 conn.execute（**不** 自己 commit、**不** 开新连接）——保证同事务。

    决策卡随 trade raw 一并保存；L2 教训摘要由 reviewer 流程
异步补，不阻塞交易（本模块不写 summary）。

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
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, _project_path('scripts'))
import _simutil  # noqa: E402

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
OPEN_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "ADD"}
CLOSE_ACTIONS = {"CLOSE", "STOP_LOSS", "REDUCE"}
PLAYBOOK_REF_RE = re.compile(r"playbook\s*#\s*(\d+)", re.IGNORECASE)


def _now_cst() -> str:
    return datetime.now(CST).strftime(TS_FMT)


def _parse_playbook_ref_ids(value) -> set[int]:
    """Extract canonical playbook ids without treating unrelated numbers as refs."""
    refs: set[int] = set()
    if value is None or isinstance(value, bool):
        return refs
    if isinstance(value, int):
        if value > 0:
            refs.add(value)
        return refs
    if isinstance(value, float):
        if value > 0 and value.is_integer():
            refs.add(int(value))
        return refs
    if isinstance(value, (list, tuple, set)):
        for item in value:
            refs.update(_parse_playbook_ref_ids(item))
        return refs
    if isinstance(value, dict):
        if "playbook_ref" in value:
            refs.update(_parse_playbook_ref_ids(value.get("playbook_ref")))
        return refs

    text = str(value).strip()
    if not text:
        return refs
    if text.startswith(("[", "{")):
        try:
            refs.update(_parse_playbook_ref_ids(json.loads(text)))
            return refs
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if re.fullmatch(r"#?\s*\d+", text):
        number = int(text.replace("#", "").strip())
        if number > 0:
            refs.add(number)
    for match in PLAYBOOK_REF_RE.finditer(text):
        number = int(match.group(1))
        if number > 0:
            refs.add(number)
    return refs


def _canonical_playbook_ref(data: dict, trade: dict):
    """Collect explicit and decision-card references for future outcome attribution."""
    refs: set[int] = set()
    candidates = (trade.get("playbook_ref"), data.get("playbook_ref"))
    for candidate in candidates:
        refs.update(_parse_playbook_ref_ids(candidate))

    card = trade.get("decision_card") or data.get("decision_card")
    if isinstance(card, dict):
        history = card.get("historical_experience")
        if isinstance(history, dict):
            for bucket in ("profitable", "unprofitable", "missed_opportunities"):
                rows = history.get(bucket)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict):
                        refs.update(
                            _parse_playbook_ref_ids(row.get("playbook_ref")))

    if refs:
        return json.dumps(sorted(refs), ensure_ascii=False)
    # Preserve a non-canonical explicit value for audit instead of silently
    # discarding it; current-stat aggregation will ignore unparseable refs.
    return trade.get("playbook_ref") or data.get("playbook_ref")


def table_exists(conn: sqlite3.Connection) -> bool:
    r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                     "AND name='trade_experiences'").fetchone()
    return r is not None


def _pos_side(trade: dict, action_taken: str) -> str:
    """成 long/short（持仓方向，非 buy/sell）。"""
    s = str(trade.get("side", "")).strip().lower()
    if s in ("long", "open_long", "buy"):
        base = "long"
    elif s in ("short", "open_short", "sell"):
        base = "short"
    else:
        base = "long" if action_taken == "OPEN_LONG" else (
            "short" if action_taken == "OPEN_SHORT" else s)
    return base


def _hold_hours(open_ts: Optional[str], now_ts: str) -> Optional[float]:
    if not open_ts:
        return None
    try:
        a = datetime.strptime(open_ts, TS_FMT).replace(tzinfo=CST)
        b = datetime.strptime(now_ts, TS_FMT).replace(tzinfo=CST)
        return round(max(0.0, (b - a).total_seconds() / 3600.0), 2)
    except (ValueError, TypeError):
        return None


def _trade_ordid(t: dict) -> Optional[str]:
    for k in ("ordId", "ord_id", "open_id"):
        v = t.get(k)
        if v not in (None, "", 0):
            return str(v)
    return None


QUANTITY_COLUMNS = {
    "open_sz": "REAL",
    "remaining_sz": "REAL",
    "realized_pnl": "REAL NOT NULL DEFAULT 0",
    "close_count": "INTEGER NOT NULL DEFAULT 0",
    "closed_at": "TEXT",
}
_EPS = 1e-9


def ensure_quantity_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema guard; production migration normally runs first."""
    cols = {str(r[1]) for r in conn.execute(
        "PRAGMA table_info(trade_experiences)")}
    for name, ddl in QUANTITY_COLUMNS.items():
        if name not in cols:
            conn.execute(
                f"ALTER TABLE trade_experiences ADD COLUMN {name} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_experience_open_qty "
        "ON trade_experiences(profile,symbol,side,status,ts)")


def _positive(v) -> Optional[float]:
    try:
        out = abs(float(v))
    except (TypeError, ValueError):
        return None
    return out if out > _EPS else None


def _raw_dict(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return dict(value) if isinstance(value, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _entry_notional(raw: dict, qty: Optional[float] = None) -> Optional[float]:
    value = _positive(raw.get("notional"))
    if value is not None:
        raw_sz = _positive(raw.get("sz"))
        if qty is not None and raw_sz and qty < raw_sz - _EPS:
            return value * qty / raw_sz
        return value
    px = _positive(raw.get("fill_px") or raw.get("px"))
    sz = qty or _positive(raw.get("sz"))
    cv = _positive(raw.get("ct_val") or raw.get("ctVal"))
    if px and sz and cv:
        return px * sz * cv
    return None


def _close_event(
    trade: dict, cycle_id: str, now_ts: str, consumed_sz: float,
    allocated_pnl: Optional[float],
) -> dict:
    return {
        "ordId": _trade_ordid(trade),
        "cycle_id": cycle_id,
        "ts": now_ts,
        "sz": consumed_sz,
        "pnl": allocated_pnl,
        "fill_px": trade.get("fill_px") or trade.get("px"),
    }


def _dup_exists(conn: sqlite3.Connection, profile: str, symbol: str, side: str,
                action: str, cycle_id: str, oid: Optional[str]) -> bool:
    """写侧幂等判定：同笔交易的经验行是否已存在。

    带 ordId → 严格按 ordId 判（存于 raw JSON，子串即中，19 位唯一号）——同 cycle
    两笔不同 ordId 的真实成交各写各的，不误合；无 ordId → 按
    (profile,symbol,side,action,cycle_id) 精确键（同 cycle 无 ordId 重喂 100% 是
    重复；两笔同 cycle 同形无 ordId 真实成交被合=保守方向，可接受）。
    防止 writer 重喂同一成交时产生重复经验行。"""
    if oid:
        r = conn.execute(
            "SELECT id FROM trade_experiences WHERE profile=? AND symbol=? "
            "AND status NOT IN ('superseded','orphaned') AND raw LIKE ? LIMIT 1",
            (profile, symbol, f"%{oid}%")).fetchone()
        return r is not None
    r = conn.execute(
        "SELECT id FROM trade_experiences WHERE profile=? AND symbol=? AND side=? "
        "AND action=? AND cycle_id=? "
        "AND status NOT IN ('superseded','orphaned') LIMIT 1",
        (profile, symbol, side, action, cycle_id)).fetchone()
    return r is not None


def insert_or_update_experiences(conn: sqlite3.Connection, data: dict,
                                 cycle_count: int | str,
                                 now_ts: Optional[str] = None) -> dict:
    """对 payload 的每笔 trade 写/更经验行（用 caller conn，不 commit）。

    返回 {opened, closed, fallback, deduped}。caller 在已开事务内调（同 cycle_runs）。
    """
    now_ts = now_ts or _now_cst()
    ensure_quantity_schema(conn)
    cycle_id = data.get("cycle_id") or str(cycle_count)
    profile = data.get("profile", "live")
    action_taken = data.get("action_taken", "")
    regime = data.get("regime")
    regime_stale = 1 if data.get("regime_stale") else 0
    confidence = data.get("confidence")
    market_snapshot = data.get("market_snapshot")
    if market_snapshot is not None and not isinstance(market_snapshot, str):
        market_snapshot = json.dumps(market_snapshot, ensure_ascii=False)

    opened = closed = fallback = deduped = 0
    for t in (data.get("trades") or []):
        if not isinstance(t, dict):
            continue
        symbol = t.get("symbol") or t.get("inst_id")
        if not symbol:
            continue
        side = _pos_side(t, action_taken)
        score = t.get("score_total") if t.get("score_total") is not None else \
            data.get("total_score") or data.get("score_total")
        playbook_ref = _canonical_playbook_ref(data, t)
        hypothesis_id = data.get("hypothesis_id")

        if action_taken in OPEN_ACTIONS:
            if _dup_exists(conn, profile, symbol, side, "open", cycle_id,
                           _trade_ordid(t)):
                deduped += 1
                continue
            vec = _simutil.experience_vector({
                "regime": regime, "side": side, "action": "open",
                "score_total": score, "regime_stale": regime_stale,
                "symbol": symbol,
            })
            open_sz = _positive(t.get("sz"))
            conn.execute(
                "INSERT INTO trade_experiences (cycle_id, ts, profile, symbol, "
                "side, action, regime, regime_stale, score_total, confidence, "
                "playbook_ref, hypothesis_id, market_snapshot, experience_vector, "
                "status, open_sz, remaining_sz, realized_pnl, close_count, raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cycle_id, now_ts, profile, symbol, side, "open", regime,
                 regime_stale, score, confidence, playbook_ref, hypothesis_id,
                 market_snapshot, json.dumps(vec),
                 "open", open_sz, open_sz, 0.0, 0,
                 json.dumps(t, ensure_ascii=False)))
            opened += 1

        elif action_taken in CLOSE_ACTIONS:
            # 幂等：close ordId 会写入各被消费 open 的 close_events；同笔重喂
            # 直接跳过，不再消费下一条 open。
            if _dup_exists(conn, profile, symbol, side, "close", cycle_id,
                           _trade_ordid(t)):
                deduped += 1
                continue
            pnl = t.get("pnl")
            if pnl is not None and t.get("pnl_approx"):
                # 2026-07-03：approx pnl（fills 全量聚合兜底，可能混历史成交）不进经验库
                # outcome——置 None → pnl_pct=None（closed 行保留，不污染 find_similar 引用）。
                pnl = None
            close_sz = _positive(t.get("sz"))
            try:
                pnl_value = float(pnl) if pnl is not None else None
            except (TypeError, ValueError):
                pnl_value = None
            remaining_close = close_sz

            # FIFO 数量消费。一个 close 可跨多条 open；一个 open 也可由多次
            # reduce/close 分批消费。只匹配平仓时刻之前的同盘同币同持仓方向。
            rows = conn.execute(
                "SELECT id,ts,status,raw,open_sz,remaining_sz,realized_pnl,"
                "close_count FROM trade_experiences WHERE profile=? AND "
                "symbol=? AND side=? AND action='open' "
                "AND status IN ('open','expired') AND ts<=? "
                "ORDER BY ts ASC,id ASC",
                (profile, symbol, side, now_ts)).fetchall()
            for row in rows:
                if remaining_close is not None and remaining_close <= _EPS:
                    break
                rawd = _raw_dict(row[3])
                open_sz = _positive(row[4]) or _positive(rawd.get("sz"))
                row_remaining = _positive(row[5])
                if row_remaining is None:
                    row_remaining = open_sz
                if row_remaining is None:
                    continue
                # 缺 close sz 时不猜跨腿分摊：只消费第一条候选的全部剩余量，
                # 并让 outcome 保持未知；正常 order_executor close 一定带 sz。
                consume = (
                    row_remaining if remaining_close is None
                    else min(row_remaining, remaining_close)
                )
                if consume <= _EPS:
                    continue
                allocated_pnl = None
                if pnl_value is not None and close_sz:
                    allocated_pnl = pnl_value * consume / close_sz
                previous_realized = float(row[6] or 0.0)
                new_realized = previous_realized + (allocated_pnl or 0.0)
                new_remaining = max(0.0, row_remaining - consume)
                fully_closed = new_remaining <= _EPS

                events = rawd.get("close_events")
                if not isinstance(events, list):
                    events = []
                events.append(_close_event(
                    t, cycle_id, now_ts, consume, allocated_pnl))
                rawd["close_events"] = events
                rawd["close_ordId"] = _trade_ordid(t)
                rawd["close_cycle_id"] = cycle_id

                pnl_pct = None
                all_pnl_known = all(
                    isinstance(e, dict) and e.get("pnl") is not None
                    for e in events)
                notional = _entry_notional(rawd, open_sz)
                if fully_closed and all_pnl_known and notional:
                    pnl_pct = round(new_realized / notional * 100.0, 4)
                hit_1r = (
                    1 if pnl_pct is not None and pnl_pct > 0
                    else (0 if fully_closed and pnl_pct is not None else None)
                )
                hold = _hold_hours(row[1], now_ts) if fully_closed else None
                new_status = (
                    "closed" if fully_closed
                    else ("expired" if row[2] == "expired" else "open")
                )
                conn.execute(
                    "UPDATE trade_experiences SET status=?,open_sz=?,"
                    "remaining_sz=?,realized_pnl=?,close_count=?,pnl_pct=?,"
                    "hold_hours=?,hit_1R=?,closed_at=?,raw=? WHERE id=?",
                    (new_status, open_sz, new_remaining, new_realized,
                     int(row[7] or 0) + 1, pnl_pct, hold, hit_1r,
                     now_ts if fully_closed else None,
                     json.dumps(rawd, ensure_ascii=False), row[0]))
                if fully_closed:
                    closed += 1
                if remaining_close is not None:
                    remaining_close = max(0.0, remaining_close - consume)
                else:
                    remaining_close = 0.0
                    break

            # 无匹配 open 或账面 open 量不足：保留一条 closed fallback，
            # 明确 unmatched_sz；不把整笔 close pnl 错配给某一条 open。
            if remaining_close is None or remaining_close > _EPS:
                unmatched_sz = remaining_close
                unmatched_pnl = None
                if pnl_value is not None and close_sz and unmatched_sz is not None:
                    unmatched_pnl = pnl_value * unmatched_sz / close_sz
                vec = _simutil.experience_vector({
                    "regime": regime, "side": side, "action": "close",
                    "score_total": score, "regime_stale": regime_stale,
                    "symbol": symbol})
                fallback_raw = dict(t)
                fallback_raw["unmatched_sz"] = unmatched_sz
                fallback_raw["close_cycle_id"] = cycle_id
                conn.execute(
                    "INSERT INTO trade_experiences (cycle_id, ts, profile, symbol, "
                    "side, action, regime, regime_stale, score_total, confidence, "
                    "playbook_ref, hypothesis_id, market_snapshot, "
                    "experience_vector,pnl_pct,hit_1R,status,open_sz,"
                    "remaining_sz,realized_pnl,close_count,closed_at,raw) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cycle_id, now_ts, profile, symbol, side, "close", regime,
                     regime_stale, score, confidence, playbook_ref, hypothesis_id,
                     market_snapshot, json.dumps(vec), None, None, "closed",
                     0.0, 0.0, unmatched_pnl or 0.0, 1, now_ts,
                     json.dumps(fallback_raw, ensure_ascii=False)))
                fallback += 1

    return {"opened": opened, "closed": closed, "fallback": fallback,
            "deduped": deduped}
