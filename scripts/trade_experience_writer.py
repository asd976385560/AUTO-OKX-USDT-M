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

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import json
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


def _now_cst() -> str:
    return datetime.now(CST).strftime(TS_FMT)


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
            "AND raw LIKE ? LIMIT 1",
            (profile, symbol, f"%{oid}%")).fetchone()
        return r is not None
    r = conn.execute(
        "SELECT id FROM trade_experiences WHERE profile=? AND symbol=? AND side=? "
        "AND action=? AND cycle_id=? LIMIT 1",
        (profile, symbol, side, action, cycle_id)).fetchone()
    return r is not None


def insert_or_update_experiences(conn: sqlite3.Connection, data: dict,
                                 cycle_count: int | str,
                                 now_ts: Optional[str] = None) -> dict:
    """对 payload 的每笔 trade 写/更经验行（用 caller conn，不 commit）。

    返回 {opened, closed, fallback, deduped}。caller 在已开事务内调（同 cycle_runs）。
    """
    now_ts = now_ts or _now_cst()
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
        playbook_ref = t.get("playbook_ref") or data.get("playbook_ref")
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
            conn.execute(
                "INSERT INTO trade_experiences (cycle_id, ts, profile, symbol, "
                "side, action, regime, regime_stale, score_total, confidence, "
                "playbook_ref, hypothesis_id, market_snapshot, experience_vector, "
                "status, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cycle_id, now_ts, profile, symbol, side, "open", regime,
                 regime_stale, score, confidence, playbook_ref, hypothesis_id,
                 market_snapshot, json.dumps(vec),
                 "open", json.dumps(t, ensure_ascii=False)))
            opened += 1

        elif action_taken in CLOSE_ACTIONS:
            # 幂等：本笔 close 已应用过（UPDATE 时 close_ordId 已并进
            # open 行 raw / fallback 行 raw 本身含 ordId）→ 跳过，不再消费别的 open 行。
            if _dup_exists(conn, profile, symbol, side, "close", cycle_id,
                           _trade_ordid(t)):
                deduped += 1
                continue
            pnl = t.get("pnl")
            if pnl is not None and t.get("pnl_approx"):
                # 2026-07-03：approx pnl（fills 全量聚合兜底，可能混历史成交）不进经验库
                # outcome——置 None → pnl_pct=None（closed 行保留，不污染 find_similar 引用）。
                pnl = None
            # 找平仓时刻之前最近一条可闭合经验（同 profile+symbol+side）。
            #
            # 历史 reconcile 可能晚于真实平仓执行；必须按 close_ts 匹配此前最近的 open，
            # 禁止闭合平仓后新开的现仓。
            # hygiene 标成 expired 的历史悬挂 open 也应允许被有交易所证据的补账 close
            # 正确闭合，因此纳入候选；时间上限保证不会消费未来开仓。
            row = conn.execute(
                "SELECT id, ts, raw FROM trade_experiences WHERE profile=? AND "
                "symbol=? AND side=? AND status IN ('open','expired') AND ts<=? "
                "ORDER BY ts DESC, id DESC LIMIT 1",
                (profile, symbol, side, now_ts)).fetchone()
            pnl_pct = None
            if pnl is not None:
                entry_notional = None
                if row is not None:
                    try:
                        rawd = json.loads(row[2] or "{}")
                        # 优先用 open 回执自带 notional（order_executor 已算
                        # fill_px×sz×ctVal），禁止遗漏 ctVal。
                        # 缺 notional 才回退，且回退必须带 ctVal，无 ctVal 置 None 不伪造。
                        entry_notional = rawd.get("notional")
                        if entry_notional is not None:
                            entry_notional = float(entry_notional)
                        else:
                            ep = rawd.get("fill_px") or rawd.get("px")
                            esz = rawd.get("sz")
                            cv = rawd.get("ct_val") or rawd.get("ctVal")
                            if ep and esz and cv:
                                entry_notional = float(ep) * float(esz) * float(cv)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
                if entry_notional:
                    try:
                        pnl_pct = round(float(pnl) / entry_notional * 100.0, 4)
                    except (TypeError, ValueError, ZeroDivisionError):
                        pnl_pct = None
            hit_1r = (1 if (pnl_pct is not None and pnl_pct > 0) else 0)
            if row is not None:
                hold = _hold_hours(row[1], now_ts)
                # 把平仓身份（close_ordId/close_cycle_id）并进 open 行 raw，
                # UPDATE 零留痕，同笔 close 重喂找不到 open 就走 fallback 裸 INSERT 重复行；
                # 有此标记后上方 _dup_exists 按 ordId 即可拦住。raw 只增键不改原开仓字段
                # （entry_notional 等消费不受影响）。解析失败跳过增记（fail-safe）。
                raw_upd = None
                try:
                    rawd = json.loads(row[2] or "{}")
                    if isinstance(rawd, dict):
                        rawd["close_ordId"] = _trade_ordid(t)
                        rawd["close_cycle_id"] = cycle_id
                        raw_upd = json.dumps(rawd, ensure_ascii=False)
                except (json.JSONDecodeError, TypeError, ValueError):
                    raw_upd = None
                conn.execute(
                    "UPDATE trade_experiences SET status='closed', pnl_pct=?, "
                    "hold_hours=?, hit_1R=?, ts=COALESCE(ts, ?), "
                    "raw=COALESCE(?, raw) WHERE id=?",
                    (pnl_pct, hold, hit_1r, now_ts, raw_upd, row[0]))
                closed += 1
            else:
                # 无匹配 open（reconcile fallback）→ 直接写 closed 行，outcome 不丢。
                # 幂等：close 重喂时首喂已把 open 行 UPDATE 成 closed，二喂找不到
                # open 即落到本分支裸 INSERT——07-13 DATA close 三连重复(id70/71/72)正是
                # 此路径产物。写前按 ordId/cycle 精确键查重。
                if _dup_exists(conn, profile, symbol, side, "close", cycle_id,
                               _trade_ordid(t)):
                    deduped += 1
                    continue
                vec = _simutil.experience_vector({
                    "regime": regime, "side": side, "action": "close",
                    "score_total": score, "regime_stale": regime_stale,
                    "symbol": symbol})
                conn.execute(
                    "INSERT INTO trade_experiences (cycle_id, ts, profile, symbol, "
                    "side, action, regime, regime_stale, score_total, confidence, "
                    "playbook_ref, hypothesis_id, market_snapshot, "
                    "experience_vector, pnl_pct, hit_1R, status, raw) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cycle_id, now_ts, profile, symbol, side, "close", regime,
                     regime_stale, score, confidence, playbook_ref, hypothesis_id,
                     market_snapshot, json.dumps(vec), pnl_pct, hit_1r, "closed",
                     json.dumps(t, ensure_ascii=False)))
                fallback += 1

    return {"opened": opened, "closed": closed, "fallback": fallback,
            "deduped": deduped}
