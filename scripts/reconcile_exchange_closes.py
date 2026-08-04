# -*- coding: utf-8 -*-
r"""交易所侧/执行后平仓漏落账对账（F2，2026-07-06）。

背景：algo 止损或主动平仓已在交易所成交后，账本（live/demo trades.db）可能漏写——
仓位从 position_snapshots 消失、trades 表却无 close 行，形成「幽灵仓」：
  - 账本轧差净持仓 > OKX API 现仓 → 幽灵；
  - 实证：live LINK-USDT-SWAP 11.4 张（2026-07-05 10:11:55 SL 卖出 pnl=-1.5276）、
    demo LAB-USDT-SWAP 110 张（2026-07-06 04:22:40 SL 卖出 pnl=-5.346）。

逻辑（先例＝2026-07-04 demo ATOM 补账：fills 实证 → trades_writer.write_trades 直调）：
  1. 读该 profile trades 轧差净持仓（open/add 加、close/stop_loss/reduce 减，按 symbol+side）；
  2. 对比 OKX API 现仓（`account positions --instType SWAP`）；
  3. 账本多出的幽灵仓 → 按 symbol 回读 fills（recent + --archive 合并去重），
     取 open 窗口起点之后的反向平仓成交，按 ordId 分组；
  4. 已有账本 close 行先「销账」对应 fills 组（sz 相等 + 时间窗内）；
  5. 【精确匹配】判定（满足其一才可补，匹配集张数合计恒 == 幽灵 sz）：
     a) 剩余 fills 组张数合计 == 幽灵 sz → 全部剩余组即匹配集；
     b) 恰有唯一一个剩余组 sz == 幽灵 sz → 该组即匹配集（其余剩余组=独立未记账
        成交，如小额同 sz 往返，净额为 0 不影响轧差——只报告 [LEFTOVER] 不写）；
     两者都不满足 → 模糊，只报告；
  6. --apply：精确匹配项经 collectors/trades_writer.write_trades 直调补一行 close
     （action='close'，pnl=fills fillPnl 合计，cycle_id=平仓时刻所在 15min 槽，
      优先用执行 journal 还原主动平仓语义，否则中性标记 exchange fills）；
      目标 cycle 已存在时先读原行，
      原有 trades 行合并进 payload（write_trades 是 REPLACE+DELETE 语义，不合并会销账）。

只报告不写的类别：
  - [GHOST-FUZZY]  fills 对不上幽灵 sz / API 失败 → 人工核；
  - [OVER_CLOSED]  账本净持仓为负（close 多于 open，缺 open 行）→ 非本脚本可补；
  - [UNRECORDED]   交易所有仓账本无（下单成功未记账）→ 非本脚本可补。

退出码：0=无幽灵（或 --apply 全部补完）；1=有精确可补项（dry，待 --apply）；
        3=存在模糊幽灵（需人工）；2=API/库/写入错误。

用法：
  pwsh -NoProfile -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 \
      <PROJECT_ROOT>/scripts/reconcile_exchange_closes.py --profile live [--db-root <PROJECT_ROOT>/db] [--apply]
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
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, _project_path('scripts'))
sys.path.insert(0, _project_path('collectors'))
from _okxcli import okx_json  # noqa: E402
import trades_writer  # noqa: E402  （硬化 writer：write_trades / write_experiences / normalize_ts）

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
SZ_TOL = 1e-6              # 张数比较容差（float 累加误差级）
CONSUME_WINDOW_MIN = 45    # 账本 close 行 ts ↔ fills 组时间匹配窗口（分钟）
OPEN_TS_BUFFER_MIN = 10    # fills 检索窗口起点 = 最早 open 行 ts - buffer（fills 常先于落库 ts）
RAW_FILLS_CAP = 50         # raw 里最多存多少条 fills 明细


def f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def norm_side(s):
    s = (s or "").lower()
    if s in ("sell", "short"):
        return "short"
    if s in ("buy", "long"):
        return "long"
    return s or "?"


def rows_of(payload):
    if isinstance(payload, dict):
        return payload.get("data") or []
    return payload or []


def parse_ts(s):
    """账本 ts（UTC+8 字符串，先经 writer normalize）→ aware datetime；坏格式返 None。"""
    try:
        return datetime.strptime(trades_writer.normalize_ts(str(s or "")), TS_FMT).replace(tzinfo=CST)
    except (ValueError, TypeError):
        return None


def fill_dt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=CST)


def slot_cycle_id(dt_obj):
    """UTC+8 datetime → 所在 15min 槽 cycle_id 'YYYY-MM-DDTHH:MM'。"""
    return dt_obj.replace(minute=(dt_obj.minute // 15) * 15).strftime("%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------------------
# 账本 / 现仓
# ---------------------------------------------------------------------------
def ledger_rows(con):
    """全量 trades 行 → {(symbol, side): [row, ...]}（rowid 序）。"""
    by_key = defaultdict(list)
    for r in con.execute(
            "SELECT id, cycle_id, ts, symbol, action, side, sz, fill_px, lev, pnl "
            "FROM trades ORDER BY rowid"):
        by_key[(r["symbol"], norm_side(r["side"]))].append(r)
    return by_key


def net_of(rows):
    net = 0.0
    for r in rows:
        act = (r["action"] or "").lower()
        sz = f(r["sz"], 0.0) or 0.0
        if act in ("open", "add"):
            net += sz
        elif act in ("close", "stop_loss", "reduce"):
            net -= sz
    return net


def venue_positions(profile):
    """OKX API 现仓 {(instId, side): sz}。失败抛异常（exit 2）。"""
    rows = rows_of(okx_json("account", "positions", "--instType", "SWAP",
                            global_args=["--profile", profile]))
    out = defaultdict(float)
    for r in rows:
        if not isinstance(r, dict):
            continue
        pos = f(r.get("pos"), 0.0)
        if not pos:
            continue
        side = r.get("posSide") if r.get("posSide") in ("long", "short") else (
            "long" if pos > 0 else "short")
        out[(r.get("instId"), side)] += abs(pos)
    return dict(out)


# ---------------------------------------------------------------------------
# fills 回读 + ordId 分组 + 已记账销账
# ---------------------------------------------------------------------------
def fetch_reduce_fills(profile, sym, side, t0_ms):
    """回读 sym 的反向平仓成交（recent + --archive 合并、tradeId 去重）。

    平仓腿判定：side=反向（long→sell / short→buy）且 posSide 匹配持仓方向；
    posSide 非 long/short（net 模式历史）时要求 fillPnl≠0。
    """
    reduce_side = "sell" if side == "long" else "buy"
    merged, seen = [], set()
    errors = []
    for extra in ([], ["--archive"]):
        try:
            fills = rows_of(okx_json("swap", "fills", "--instId", sym, *extra,
                                     global_args=["--profile", profile]))
        except Exception as e:  # noqa: BLE001 —— 单源失败不致命，两源全失败才报
            errors.append(f"fills{' --archive' if extra else ''} 失败: {e}")
            continue
        for x in fills:
            if not isinstance(x, dict):
                continue
            key = x.get("tradeId") or (
                f"{x.get('ordId')}|{x.get('fillTime')}|{x.get('fillSz')}|{x.get('fillPx')}")
            if key in seen:
                continue
            seen.add(key)
            if int(x.get("fillTime") or 0) < t0_ms:
                continue
            if x.get("side") != reduce_side:
                continue
            ps = x.get("posSide")
            if ps in ("long", "short"):
                if ps != side:
                    continue
            elif abs(f(x.get("fillPnl"), 0.0) or 0.0) <= 1e-12:
                continue  # net 模式无 posSide：以 fillPnl≠0 认平仓腿
            merged.append(x)
    if not merged and len(errors) >= 2:
        raise RuntimeError("; ".join(errors))
    return merged


def fetch_open_fills(profile, sym, side, t0_ms):
    """回读 sym 的**开仓腿**成交（P2·2026-08-04）——`fetch_reduce_fills` 的镜像。

    开仓腿判定：side=同向（long→buy / short→sell）且 posSide 匹配持仓方向；
    posSide 非 long/short（net 模式历史）时要求 fillPnl==0（开仓不产生已实现盈亏，
    与平仓腿的 fillPnl≠0 正好互补）。
    """
    open_side = "buy" if side == "long" else "sell"
    merged, seen = [], set()
    errors = []
    for extra in ([], ["--archive"]):
        try:
            fills = rows_of(okx_json("swap", "fills", "--instId", sym, *extra,
                                     global_args=["--profile", profile]))
        except Exception as e:  # noqa: BLE001 —— 单源失败不致命，两源全失败才报
            errors.append(f"fills{' --archive' if extra else ''} 失败: {e}")
            continue
        for x in fills:
            if not isinstance(x, dict):
                continue
            key = x.get("tradeId") or (
                f"{x.get('ordId')}|{x.get('fillTime')}|{x.get('fillSz')}|{x.get('fillPx')}")
            if key in seen:
                continue
            seen.add(key)
            if int(x.get("fillTime") or 0) < t0_ms:
                continue
            if x.get("side") != open_side:
                continue
            ps = x.get("posSide")
            if ps in ("long", "short"):
                if ps != side:
                    continue
            elif abs(f(x.get("fillPnl"), 0.0) or 0.0) > 1e-12:
                continue  # net 模式无 posSide：以 fillPnl==0 认开仓腿
            merged.append(x)
    if not merged and len(errors) >= 2:
        raise RuntimeError("; ".join(errors))
    return merged


def group_by_ord(fills):
    """fills → [{ordId, sz, pnl, wavg_px, t_last_ms, fills}]（按时间升序）。"""
    groups = defaultdict(list)
    for x in fills:
        groups[x.get("ordId") or "?"].append(x)
    out = []
    for oid, xs in groups.items():
        sz = sum(f(x.get("fillSz"), 0.0) or 0.0 for x in xs)
        pnl = sum(f(x.get("fillPnl"), 0.0) or 0.0 for x in xs)
        wavg = (sum((f(x.get("fillPx"), 0.0) or 0.0) * (f(x.get("fillSz"), 0.0) or 0.0)
                    for x in xs) / sz) if sz else 0.0
        out.append({"ordId": oid, "sz": sz, "pnl": pnl, "wavg_px": wavg,
                    "t_last_ms": max(int(x.get("fillTime") or 0) for x in xs),
                    "fills": xs})
    out.sort(key=lambda g: g["t_last_ms"])
    return out


CLOSE_ACTIONS = ("close", "stop_loss", "reduce")
OPEN_ACTIONS = ("open", "add")


def consume_recorded(groups, rows, t0_dt, actions=CLOSE_ACTIONS):
    """账本已有成交行 → 销账对应 fills 组（sz 相等 + 时间窗内）。

    `actions` 默认平仓腿（幽灵仓补 close 用）；P2 补 open 时传 `OPEN_ACTIONS`
    销账已记录的开仓行，逻辑完全对称。

    返回 (remaining_groups, consume_notes)。销不掉的账本行只记备注
    （可能超 fills API 窗口）——最终以「剩余组合计 == 目标 sz」硬门兜底。
    """
    notes, remaining = [], list(groups)
    for r in rows:
        act = (r["action"] or "").lower()
        if act not in actions:
            continue
        r_dt = parse_ts(r["ts"])
        if r_dt is None or (t0_dt is not None and r_dt < t0_dt):
            continue
        r_sz = f(r["sz"], 0.0) or 0.0
        best, best_gap = None, None
        for g in remaining:
            if abs(g["sz"] - r_sz) > SZ_TOL:
                continue
            gap = abs((fill_dt(g["t_last_ms"]) - r_dt).total_seconds())
            if gap <= CONSUME_WINDOW_MIN * 60 and (best_gap is None or gap < best_gap):
                best, best_gap = g, gap
        if best is not None:
            remaining.remove(best)
            notes.append(f"账本行 id={r['id']}({act} sz={r_sz} ts={r['ts']}) ↔ "
                         f"ordId={best['ordId']} 已销账")
        else:
            notes.append(f"账本行 id={r['id']}({act} sz={r_sz} ts={r['ts']}) 无对应 fills 组"
                         f"（可能超 API 窗口，靠合计硬门兜底）")
    return remaining, notes


def find_journal_close(db_path, profile, sym, ord_ids):
    """按 ordId 从 append-only 执行 journal 找已确认 close；找不到返回 None。"""
    wanted = {str(value) for value in ord_ids if value not in (None, "")}
    if not wanted:
        return None
    path = Path(db_path).parent / "journal" / f"exec_{profile}.jsonl"
    if not path.is_file():
        return None
    found = None
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            trade = record.get("trade")
            if not isinstance(trade, dict):
                continue
            if str(trade.get("symbol") or "") != str(sym):
                continue
            if str(trade.get("action") or "").lower() != "close":
                continue
            trade_ord_ids = {
                str(trade.get("ordId") or ""),
                *[
                    str(value)
                    for value in ((trade.get("raw") or {}).get("ord_ids") or [])
                ],
            }
            if wanted.isdisjoint(trade_ord_ids):
                continue
            found = {
                "line_no": line_no,
                "record": record,
                "trade": trade,
                "path": str(path),
            }
    return found


def repair_existing_from_journal(db_path, profile, ord_id, con_ro):
    """把已补账但误标为 SL 的 close 元数据改为执行 journal 实情。

    仅在唯一 trade 行明确含目标 ordId、且 journal 的 symbol/side/sz/px/pnl
    与主账一致时允许写；成交事实不变，仍经 trades_writer 整 cycle 重写。
    """
    matches = []
    rows = con_ro.execute(
        "SELECT id,cycle_id,ts,symbol,action,side,sz,fill_px,lev,margin,notional,"
        "score_total,reasoning,deviation,degradation,pnl,raw "
        "FROM trades ORDER BY rowid"
    ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row["raw"] or "{}")
        except (json.JSONDecodeError, TypeError):
            raw = {}
        row_ord_ids = {
            str(raw.get("ordId") or ""),
            *[str(value) for value in (raw.get("ord_ids") or [])],
        }
        if str(ord_id) in row_ord_ids:
            matches.append((row, raw))
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError(
            f"ordId={ord_id} 主账命中 {len(matches)} 行，拒绝元数据校正"
        )
    target, target_raw = matches[0]

    journal = find_journal_close(
        db_path, profile, target["symbol"], [str(ord_id)]
    )
    if not journal:
        raise RuntimeError(f"ordId={ord_id} 未找到执行 journal close，拒绝校正")
    jt = journal["trade"]
    comparisons = (
        ("side", str(target["side"]), str(jt.get("side"))),
        ("sz", f(target["sz"], 0.0), f(jt.get("sz"), 0.0)),
        ("fill_px", f(target["fill_px"], 0.0), f(jt.get("fill_px"), 0.0)),
        ("pnl", f(target["pnl"], 0.0), f(jt.get("pnl"), 0.0)),
    )
    for name, actual, expected in comparisons:
        if name == "side":
            equal = actual == expected
        else:
            equal = abs(actual - expected) <= SZ_TOL
        if not equal:
            raise RuntimeError(
                f"ordId={ord_id} journal {name} 不一致: ledger={actual} journal={expected}"
            )

    cycle_id = target["cycle_id"]
    prev = con_ro.execute(
        "SELECT cycle_id,ts,decision,n_orders,equity,note,raw "
        "FROM trade_cycles WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if not prev:
        raise RuntimeError(f"cycle={cycle_id} trade_cycles 缺失，拒绝校正")
    clean_note = f"execution journal 已核验主动平仓 ordId={ord_id}"
    if (
        target_raw.get("reconcile_source") == "execution_journal_recovery"
        and clean_note in (prev["note"] or "")
        and "exchange-side SL" not in (prev["note"] or "")
        and (target["reasoning"] or "") == (jt.get("reason") or "")
    ):
        return {"status": "already_consistent", "cycle_id": cycle_id}
    cycle_trades = [
        dict(row)
        for row in con_ro.execute(
            "SELECT symbol,action,side,sz,fill_px,lev,margin,notional,"
            "score_total,reasoning,deviation,degradation,pnl,raw "
            "FROM trades WHERE cycle_id=? ORDER BY rowid",
            (cycle_id,),
        ).fetchall()
    ]
    replaced = 0
    for trade in cycle_trades:
        try:
            raw = json.loads(trade.get("raw") or "{}")
        except (json.JSONDecodeError, TypeError):
            raw = {}
        row_ord_ids = {
            str(raw.get("ordId") or ""),
            *[str(value) for value in (raw.get("ord_ids") or [])],
        }
        if str(ord_id) not in row_ord_ids:
            continue
        raw.setdefault("original_reconcile_source", raw.get("reconcile_source"))
        raw["reconcile_source"] = "execution_journal_recovery"
        raw["journal_path"] = journal["path"]
        raw["journal_line"] = journal["line_no"]
        raw["journal_ts"] = journal["record"].get("ts")
        raw["action_taken"] = journal["record"].get("action_taken")
        trade["raw"] = raw
        trade["reasoning"] = jt.get("reason") or trade.get("reasoning")
        replaced += 1
    if replaced != 1:
        raise RuntimeError(f"ordId={ord_id} cycle 内替换行数={replaced}，拒绝校正")

    try:
        cycle_raw = json.loads(prev["raw"] or "{}")
    except (json.JSONDecodeError, TypeError):
        cycle_raw = {}
    if not isinstance(cycle_raw, dict):
        cycle_raw = {"original_raw": cycle_raw}
    cycle_raw.setdefault("original_note", prev["note"])
    cycle_raw.setdefault(
        "original_reconcile_source", cycle_raw.get("reconcile_source")
    )
    cycle_raw["reconcile_source"] = "execution_journal_recovery"
    cycle_raw["journal_evidence"] = {
        "path": journal["path"],
        "line": journal["line_no"],
        "ts": journal["record"].get("ts"),
        "ord_id": str(ord_id),
    }
    data = {
        "cycle_id": cycle_id,
        "ts": prev["ts"],
        "decision": prev["decision"],
        "action": (
            f"journal recovery: {target['symbol']} {target['side']} "
            f"close {target['sz']:g} @ {target['fill_px']:.6g}"
        ),
        "note": (
            clean_note
        ),
        "n_orders": len(cycle_trades),
        "equity": prev["equity"],
        "trades": cycle_trades,
        "raw": cycle_raw,
        "_profile": profile,
    }
    result = trades_writer.maintenance_write_trades(
        data,
        Path(db_path),
        trusted_timestamp=data.get("ts"),
        preserve_equity_none=True,
    )
    if not result.get("ok") or result.get("refused"):
        raise RuntimeError(f"trades_writer 拒绝 journal 元数据校正: {result}")
    return {
        "status": "repaired",
        "cycle_id": cycle_id,
        "ord_id": str(ord_id),
        "journal_line": journal["line_no"],
        "writer": result,
    }


# ---------------------------------------------------------------------------
# 补账（--apply，经硬化 writer；目标 cycle 已存在时合并原行防销账）
# ---------------------------------------------------------------------------
def apply_reconcile(db_path, profile, sym, side, ghost_sz, matched, con_ro,
                    open_lev=None):
    """把精确匹配的 fills 组集合补成一行 close（trades_writer.write_trades 直调）。

    write_trades 对 trade_cycles 是 INSERT OR REPLACE、对 trades 是 DELETE+INSERT——
    目标 cycle 已存在时必须先读原行并把原 trades 合并进 payload，否则会销掉同 cycle 已有账。
    open_lev = 该幽灵最近 open/add 行的 lev（writer 补算 margin 用，可 None）。
    """
    all_fills = [x for g in matched for x in g["fills"]]
    tot_sz = sum(f(x.get("fillSz"), 0.0) or 0.0 for x in all_fills)
    tot_pnl = round(sum(f(x.get("fillPnl"), 0.0) or 0.0 for x in all_fills), 6)
    wavg_px = (sum((f(x.get("fillPx"), 0.0) or 0.0) * (f(x.get("fillSz"), 0.0) or 0.0)
                   for x in all_fills) / tot_sz) if tot_sz else None
    close_dt = fill_dt(max(int(x.get("fillTime") or 0) for x in all_fills))
    close_ts = close_dt.strftime(TS_FMT)
    cycle_id = slot_cycle_id(close_dt)
    ord_ids = sorted({g["ordId"] for g in matched})
    journal = find_journal_close(db_path, profile, sym, ord_ids)
    journal_trade = journal["trade"] if journal else {}
    reconcile_source = (
        "execution_journal_recovery" if journal else "exchange_fills_reconcile"
    )

    # 目标 cycle 原行（只读连接查）
    prev = con_ro.execute(
        "SELECT cycle_id, ts, decision, n_orders, equity, note, raw FROM trade_cycles "
        "WHERE cycle_id=?", (cycle_id,)).fetchone()
    prev_trades = con_ro.execute(
        "SELECT symbol, action, side, sz, fill_px, lev, margin, notional, score_total, "
        "reasoning, deviation, degradation, pnl, raw FROM trades WHERE cycle_id=? "
        "ORDER BY rowid", (cycle_id,)).fetchall()

    trades = [dict(t) for t in prev_trades]
    fills_evidence = [{"ts": fill_dt(x.get("fillTime")).strftime(TS_FMT),
                       "px": x.get("fillPx"), "sz": x.get("fillSz"),
                       "pnl": x.get("fillPnl"), "ordId": x.get("ordId"),
                       "tradeId": x.get("tradeId"), "execType": x.get("execType")}
                      for x in all_fills[:RAW_FILLS_CAP]]
    reconcile_trade = {
        "symbol": sym,
        "action": "close",
        "side": side,
        "sz": tot_sz,
        "fill_px": round(wavg_px, 8) if wavg_px else None,
        "lev": open_lev,
        "margin": None,
        "notional": None,
        "score_total": None,
        "reasoning": (
            journal_trade.get("reason")
            or (
                f"reconcile_exchange_closes 补账：交易所侧平仓漏落账；"
                f"fills 实证 {len(all_fills)} 笔 ordId={','.join(ord_ids)} "
                f"平仓时刻={close_ts} pnl={tot_pnl}"
            )
        ),
        "deviation": None,
        "degradation": None,
        "pnl": tot_pnl,
        "raw": {
            "reconcile_source": reconcile_source,
            "close_ts": close_ts,
            "ord_ids": ord_ids,
            "fills": fills_evidence,
            "journal_path": journal.get("path") if journal else None,
            "journal_line": journal.get("line_no") if journal else None,
            "journal_ts": (
                journal["record"].get("ts") if journal else None
            ),
        },
    }
    trades.append(reconcile_trade)

    prev_note = (prev["note"] if prev else "") or ""
    prev_raw_obj = None
    if prev and prev["raw"]:
        try:
            prev_raw_obj = json.loads(prev["raw"]) if len(prev["raw"]) < 20000 else \
                {"_truncated": prev["raw"][:2000]}
        except (json.JSONDecodeError, TypeError):
            prev_raw_obj = {"_unparsed": str(prev["raw"])[:2000]}

    raw_obj = {
        "reconcile_source": reconcile_source,
        "reconciled_at": datetime.now(CST).strftime(TS_FMT),
        "symbol": sym, "side": side, "ghost_sz": ghost_sz,
        "close_ts": close_ts, "pnl": tot_pnl, "wavg_px": wavg_px,
        "ord_ids": ord_ids,
        "fills": fills_evidence,
        "journal_evidence": (
            {
                "path": journal["path"],
                "line": journal["line_no"],
                "ts": journal["record"].get("ts"),
            }
            if journal
            else None
        ),
        "prev_cycle": ({"decision": prev["decision"], "n_orders": prev["n_orders"],
                        "ts": prev["ts"], "note": prev_note[:1000],
                        "raw": prev_raw_obj} if prev else None),
    }

    data = {
        "cycle_id": cycle_id,
        # 原 cycle 已有 trades 行时保留原完成时刻（write_trades 会把所有行 ts 统一重写）；
        # 否则用 fills 实证的平仓时刻。
        "ts": (prev["ts"] if (prev and prev_trades) else close_ts),
        "decision": "traded",
        "action": (
            f"reconcile: {sym} {side} close {tot_sz:g} @ {wavg_px:.6g} "
            f"({'execution journal' if journal else 'exchange fills'})"
        ),
        "note": (f"reconcile_exchange_closes 补账 pnl={tot_pnl}"
                 + (f" | 原行 note: {prev_note[:300]}" if prev_note else "")),
        "n_orders": len(trades),
        "equity": (prev["equity"] if prev else None),
        "trades": trades,
        "raw": raw_obj,
        "_profile": profile,
    }
    result = trades_writer.maintenance_write_trades(
        data,
        Path(db_path),
        trusted_timestamp=data.get("ts"),
        preserve_equity_none=True,
    )
    if not result.get("ok") or result.get("refused"):
        raise RuntimeError(f"trades_writer 拒绝补账: {result}")
    # 经验库闭环（非致命）：只喂 reconcile 那一行，防止合并进来的原 trades 重复写经验
    exp = trades_writer.write_experiences(
        {"cycle_id": cycle_id, "trades": [reconcile_trade]}, profile, close_ts)
    return {"cycle_id": cycle_id, "close_ts": close_ts, "sz": tot_sz, "pnl": tot_pnl,
            "wavg_px": wavg_px, "ord_ids": ord_ids, "writer": result, "exp": exp,
            "merged_prev_trades": len(prev_trades)}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def match_exact_groups(remaining, target_sz):
    """精确匹配判定（唯一定义源）——幽灵仓补 close 与 UNRECORDED 补 open 共用。

    规则（满足其一才算精确，匹配集张数合计恒 == target_sz）：
      a) 剩余组张数合计 == target_sz → 全部剩余组即匹配集；
      b) 恰有唯一一个剩余组 sz == target_sz → 该组即匹配集（其余为独立未记账成交）。
    返回 (matched_or_None, leftover, reason)；matched 为 None 表示模糊。
    """
    rem_sz = sum(g["sz"] for g in remaining)
    if remaining and abs(rem_sz - target_sz) <= SZ_TOL:
        return list(remaining), [], None
    hits = [g for g in remaining if abs(g["sz"] - target_sz) <= SZ_TOL]
    if len(hits) == 1:
        leftover = [g for g in remaining if g is not hits[0]]
        return hits, leftover, None
    reason = (f"剩余 fills 组无法唯一对齐 sz={target_sz:g}（sz 相等组 {len(hits)} 个）"
              if remaining else "窗口内无未销账的成交")
    return None, list(remaining), reason


def apply_unrecorded(db_path, profile, sym, side, missing_sz, matched, con_ro,
                     *, lev=None, card=None, intent=None, sl_probe=None):
    """P2·2026-08-04：把交易所已有、账本却没记的仓位补成一行 `open`。

    与 `apply_reconcile`（补 close）互为镜像，共用 `maintenance_write_trades` 宽松路径。
    **不编造决策卡**：`card` 由调用方从 `analysis_signals` 取真卡；取不到就传 None，
    此时 payload 不带 `decision_protocol`（合法），并在 degradation 标注卡缺失。

    `intent` = `execution_intents` 归属证据（T1 有 / T2 无）；`sl_probe` = 交易所侧
    algo 止损探测结果。两者都只入 raw 留痕，不参与放行判定（判定在调用方）。
    """
    all_fills = [x for g in matched for x in g["fills"]]
    tot_sz = sum(f(x.get("fillSz"), 0.0) or 0.0 for x in all_fills)
    wavg_px = (sum((f(x.get("fillPx"), 0.0) or 0.0) * (f(x.get("fillSz"), 0.0) or 0.0)
                   for x in all_fills) / tot_sz) if tot_sz else None
    open_dt = fill_dt(max(int(x.get("fillTime") or 0) for x in all_fills))
    open_ts = open_dt.strftime(TS_FMT)
    ord_ids = sorted({g["ordId"] for g in matched})
    # cycle 归属优先用 intent 的真 cycle_id（T1）；无 intent 则落成交时刻所在槽（T2）
    cycle_id = (intent or {}).get("cycle_id") or slot_cycle_id(open_dt)

    prev = con_ro.execute(
        "SELECT cycle_id, ts, decision, n_orders, equity, note, raw FROM trade_cycles "
        "WHERE cycle_id=?", (cycle_id,)).fetchone()
    prev_trades = con_ro.execute(
        "SELECT symbol, action, side, sz, fill_px, lev, margin, notional, score_total, "
        "reasoning, deviation, degradation, pnl, raw FROM trades WHERE cycle_id=? "
        "ORDER BY rowid", (cycle_id,)).fetchall()
    trades = [dict(t) for t in prev_trades]

    fills_evidence = [{"ts": fill_dt(x.get("fillTime")).strftime(TS_FMT),
                       "px": x.get("fillPx"), "sz": x.get("fillSz"),
                       "ordId": x.get("ordId"), "tradeId": x.get("tradeId"),
                       "execType": x.get("execType")}
                      for x in all_fills[:RAW_FILLS_CAP]]
    # 末道防线：卡不合法就降级为无卡（带着坏卡写会被 writer 整单拒绝→补账失败→交易继续冻结）。
    # 与 ledger_autoheal._card_for 的前置校验重复是有意的：本函数对任何调用方都必须安全。
    if card is not None:
        try:
            from core.decision_card import validate_card  # noqa: PLC0415

            if validate_card(card, "decision_card"):
                card = None
        except Exception:  # noqa: BLE001
            card = None

    degradation = []
    if card is None:
        degradation.append("decision_card_missing")
    if intent is None:
        degradation.append("no_execution_intent")
    if sl_probe is not None and not sl_probe.get("has_sl"):
        degradation.append("naked_position_no_algo_sl")

    recon_trade = {
        "symbol": sym,
        "action": "open",
        "side": side,
        "sz": tot_sz,
        "fill_px": round(wavg_px, 8) if wavg_px else None,
        "lev": lev,
        "margin": None,
        "notional": None,
        "score_total": None,
        "reasoning": (
            f"ledger_autoheal 补账（UNRECORDED）：交易所有仓账本无；"
            f"fills 实证 {len(all_fills)} 笔 ordId={','.join(ord_ids)} "
            f"开仓时刻={open_ts} sz={tot_sz:g}"
            + ("；intent 归属已核" if intent else "；**无 execution_intent 归属证据**")
        ),
        "deviation": None,
        "degradation": ",".join(degradation) or None,
        "pnl": 0.0,
        "raw": {
            "reconcile_source": "exchange_fills_unrecorded",
            "open_ts": open_ts,
            "ord_ids": ord_ids,
            "fills": fills_evidence,
            "intent": intent,
            "sl_probe": sl_probe,
            "decision_card_source": "analysis_signals" if card else None,
        },
    }
    if card:
        recon_trade["decision_card"] = card
    trades.append(recon_trade)

    prev_note = (prev["note"] if prev else "") or ""
    data = {
        "cycle_id": cycle_id,
        "ts": (prev["ts"] if (prev and prev_trades) else open_ts),
        "decision": "traded",
        "action": (f"autoheal-unrecorded: {sym} {side} open {tot_sz:g} "
                   f"@ {wavg_px:.6g}" if wavg_px else
                   f"autoheal-unrecorded: {sym} {side} open {tot_sz:g}"),
        "note": (f"ledger_autoheal 补 UNRECORDED open sz={tot_sz:g}"
                 + (f" | 原行 note: {prev_note[:300]}" if prev_note else "")),
        "n_orders": len(trades),
        "equity": (prev["equity"] if prev else None),
        "trades": trades,
        "raw": {
            "reconcile_source": "exchange_fills_unrecorded",
            "reconciled_at": datetime.now(CST).strftime(TS_FMT),
            "symbol": sym, "side": side, "missing_sz": missing_sz,
            "open_ts": open_ts, "wavg_px": wavg_px, "ord_ids": ord_ids,
            "fills": fills_evidence, "intent": intent, "sl_probe": sl_probe,
            "degradation": degradation,
        },
        "_profile": profile,
    }
    if card:
        data["decision_protocol"] = "decision_card_v1"
        data["decision_card"] = card

    result = trades_writer.maintenance_write_trades(
        data, Path(db_path), trusted_timestamp=data["ts"], preserve_equity_none=True)
    if not result.get("ok") or result.get("refused"):
        raise RuntimeError(f"trades_writer 拒绝补 open: {result}")
    exp = trades_writer.write_experiences(
        {"cycle_id": cycle_id, "trades": [recon_trade]}, profile, open_ts)
    return {"cycle_id": cycle_id, "open_ts": open_ts, "sz": tot_sz,
            "wavg_px": wavg_px, "ord_ids": ord_ids, "writer": result, "exp": exp,
            "degradation": degradation, "merged_prev_trades": len(prev_trades)}


def classify(profile, by_key, nets, ven):
    """账本轧差 ↔ OKX 现仓差异分级（唯一定义源）。

    只做判定：不打印、不写库。内部会为幽灵组回读 fills（只读 API）。
    调用方一律 import 本函数，**禁止各处自写分级规则**——CLI 与
    `ledger_autoheal.py` 共用同一套 EXACT/FUZZY 口径，避免两边漂移。

    返回 dict:
      ghosts      [((sym, side), ghost_sz), ...]        账本 > 现仓
      over_closed [((sym, side), net), ...]             账本净持仓为负
      unrecorded  [((sym, side), venue_sz), ...]        现仓 > 账本
      exact       [((sym, side), ghost_sz, matched, detail), ...]  可补
      fuzzy       [((sym, side), ghost_sz, reason, detail), ...]   只报告
    """
    ghosts, over_closed = [], []
    for k, net in nets.items():
        if net < -SZ_TOL:
            over_closed.append((k, net))
            continue
        ven_sz = ven.get(k, 0.0)
        if net > ven_sz + SZ_TOL:
            ghosts.append((k, net - ven_sz))
    unrecorded = [(k, sz) for k, sz in ven.items()
                  if sz > nets.get(k, 0.0) + SZ_TOL]

    exact, fuzzy = [], []
    for (sym, side), ghost_sz in ghosts:
        rows = by_key[(sym, side)]
        opens = [parse_ts(r["ts"]) for r in rows
                 if (r["action"] or "").lower() in ("open", "add")]
        opens = [d for d in opens if d is not None]
        if not opens:
            fuzzy.append(((sym, side), ghost_sz, "账本无可解析的 open 行 ts", []))
            continue
        t0_dt = min(opens) - timedelta(minutes=OPEN_TS_BUFFER_MIN)
        t0_ms = int(t0_dt.timestamp() * 1000)
        try:
            fills = fetch_reduce_fills(profile, sym, side, t0_ms)
        except Exception as e:  # noqa: BLE001
            fuzzy.append(((sym, side), ghost_sz, f"fills API 失败: {e}", []))
            continue
        groups = group_by_ord(fills)
        remaining, notes = consume_recorded(groups, rows, t0_dt)
        rem_sz = sum(g["sz"] for g in remaining)
        detail = [f"窗口起点 {t0_dt.strftime(TS_FMT)}，平仓腿 fills {len(fills)} 笔 / "
                  f"{len(groups)} 组，销账后剩 {len(remaining)} 组合计 {rem_sz:g} 张"]
        detail += notes
        for g in remaining:
            detail.append(f"  剩余组 ordId={g['ordId']} sz={g['sz']:g} "
                          f"px≈{g['wavg_px']:.6g} pnl={g['pnl']:+.6g} "
                          f"t={fill_dt(g['t_last_ms']).strftime(TS_FMT)}")
        hit, leftover, reason = match_exact_groups(remaining, ghost_sz)
        if hit is None:
            fuzzy.append(((sym, side), ghost_sz, reason, detail))
            continue
        if leftover:
            detail.append(f"  规则b命中：唯一 ordId={hit[0]['ordId']} 组 "
                          f"sz={hit[0]['sz']:g} == 幽灵 sz；其余 {len(leftover)} 组"
                          f"为独立未记账成交（只报告不写）")
            for g in leftover:
                detail.append(f"  [LEFTOVER] ordId={g['ordId']} sz={g['sz']:g} "
                              f"px≈{g['wavg_px']:.6g} pnl={g['pnl']:+.6g} "
                              f"t={fill_dt(g['t_last_ms']).strftime(TS_FMT)} "
                              f"—— 疑似未记账小额往返（净额自平），人工核")
        exact.append(((sym, side), ghost_sz, hit, detail))

    return {"ghosts": ghosts, "over_closed": over_closed,
            "unrecorded": unrecorded, "exact": exact, "fuzzy": fuzzy}


def main():
    ap = argparse.ArgumentParser(description="交易所侧/执行后平仓漏落账对账")
    ap.add_argument("--profile", choices=["live", "demo"], required=True)
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--apply", action="store_true",
                    help="对精确匹配幽灵经 trades_writer 补 close 行（默认 dry-run 只报告）")
    ap.add_argument("--ordid",
                    help="仅 apply 含该 ordId 的唯一 GHOST-EXACT；live --apply 必填")
    args = ap.parse_args()
    if args.apply and args.profile == "live" and not args.ordid:
        print("[reconcile][ERROR] live --apply 必须同时给 --ordid；"
              "禁止宽口径一次补全部精确项")
        return 2

    db_root = Path(args.db_root)
    db_path = db_root / f"{args.profile}_trades.db"
    if not db_path.exists():
        print(f"[reconcile][ERROR] 账本不存在: {db_path}")
        return 2
    # 经验库/equity 兜底同步指向本 db-root（测试副本时不碰真 account.db）
    os.environ["OKX_ACCOUNT_DB"] = str(db_root / "account.db")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"== 交易所侧平仓落账对账 profile={args.profile} @ "
          f"{datetime.now(CST).strftime(TS_FMT)} ({mode}) ==")

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    by_key = ledger_rows(con)
    nets = {k: net_of(rows) for k, rows in by_key.items()}
    nets = {k: v for k, v in nets.items() if abs(v) > SZ_TOL}

    try:
        ven = venue_positions(args.profile)
    except Exception as e:  # noqa: BLE001
        print(f"[reconcile][ERROR] OKX 现仓 API 失败: {e}")
        con.close()
        return 2

    print(f"账本轧差净持仓 {len(nets)} 组: "
          + ("; ".join(f"{k[0]}/{k[1]}={v:g}" for k, v in nets.items()) or "空"))
    print(f"OKX 现仓 {len(ven)} 组: "
          + ("; ".join(f"{k[0]}/{k[1]}={v:g}" for k, v in ven.items()) or "空"))

    verdict = classify(args.profile, by_key, nets, ven)
    ghosts = verdict["ghosts"]
    over_closed = verdict["over_closed"]
    unrecorded = verdict["unrecorded"]
    exact, fuzzy = verdict["exact"], verdict["fuzzy"]

    if over_closed:
        print(f"\n[OVER_CLOSED] {len(over_closed)} 组（账本净持仓为负=close 多于 open，"
              f"缺 open 行；只报告，非本脚本可补）:")
        for (sym, side), net in over_closed:
            print(f"  {sym} {side} net={net:g}")
    if unrecorded:
        print(f"\n[UNRECORDED] {len(unrecorded)} 组（交易所有仓账本无/账本少记；"
              f"只报告，人工核 orders-history 后经 writer 补 open）:")
        for (sym, side), sz in unrecorded:
            print(f"  {sym} {side} venue={sz:g} ledger={nets.get((sym, side), 0.0):g}")

    if not ghosts:
        if args.apply and args.ordid:
            try:
                metadata_result = repair_existing_from_journal(
                    db_path, args.profile, args.ordid, con
                )
            except Exception as exc:  # noqa: BLE001
                print(f"\n[reconcile][ERROR] journal 元数据校正失败: {exc}")
                con.close()
                return 2
            if metadata_result is None:
                print(
                    f"\n[reconcile][ERROR] ordId={args.ordid} 未命中已落账 close；"
                    "拒绝把无幽灵仓误报为修复成功"
                )
                con.close()
                return 2
            print(
                "\n[JOURNAL-METADATA] "
                f"ordId={args.ordid} status={metadata_result['status']} "
                f"cycle={metadata_result['cycle_id']} "
                f"journal_line={metadata_result.get('journal_line', '-')}"
            )
        print("\n结论: 无幽灵仓（账本 ≤ 现仓）✓")
        con.close()
        return 0

    for (sym, side), ghost_sz, matched, detail in exact:
        close_dt = fill_dt(max(g["t_last_ms"] for g in matched))
        print(f"\n[GHOST-EXACT] {sym} {side} sz={ghost_sz:g} → 精确匹配，"
              f"平仓时刻={close_dt.strftime(TS_FMT)} cycle={slot_cycle_id(close_dt)}"
              + ("（--apply 可补账）" if not args.apply else "（补账中…）"))
        for line in detail:
            print(f"  {line}")
    for (sym, side), ghost_sz, reason, detail in fuzzy:
        print(f"\n[GHOST-FUZZY] {sym} {side} sz={ghost_sz:g} → 模糊，只报告不写：{reason}")
        for line in detail:
            print(f"  {line}")

    apply_exact = exact
    if args.ordid:
        apply_exact = [
            row for row in exact
            if str(args.ordid) in {str(g.get("ordId")) for g in row[2]}
        ]
        if len(apply_exact) != 1:
            con.close()
            print(f"\n结论: --ordid={args.ordid} 必须唯一命中 1 个 "
                  f"GHOST-EXACT，实际={len(apply_exact)}（exit 2）")
            return 2

    rc_apply_err = False
    if args.apply and exact:
        print(f"\n== APPLY：补账 {len(apply_exact)} 项（经 trades_writer.write_trades）==")
        for (sym, side), ghost_sz, matched, _ in apply_exact:
            open_lev = None
            for r in reversed(by_key[(sym, side)]):
                if (r["action"] or "").lower() in ("open", "add") and r["lev"]:
                    open_lev = r["lev"]
                    break
            try:
                res = apply_reconcile(db_path, args.profile, sym, side,
                                      ghost_sz, matched, con, open_lev=open_lev)
                print(f"  [APPLIED] {sym} {side} close sz={res['sz']:g} "
                      f"pnl={res['pnl']:+g} px≈{res['wavg_px']:.6g} "
                      f"cycle={res['cycle_id']} close_ts={res['close_ts']} "
                      f"ordId={','.join(res['ord_ids'])} "
                      f"writer={res['writer']} exp={res['exp']} "
                      f"merged_prev_trades={res['merged_prev_trades']}")
            except Exception as e:  # noqa: BLE001
                rc_apply_err = True
                print(f"  [APPLY-ERROR] {sym} {side}: {e}")

    con.close()
    if rc_apply_err:
        print("\n结论: 补账存在失败项（exit 2）")
        return 2
    if fuzzy:
        print("\n结论: 存在模糊幽灵，需人工核（exit 3）")
        return 3
    if exact and not args.apply:
        print("\n结论: 有精确可补幽灵（exit 1，加 --apply 补账）")
        return 1
    if args.apply and len(apply_exact) < len(exact):
        print(f"\n结论: 指定 ordId 已补，但仍有 "
              f"{len(exact) - len(apply_exact)} 个其他 GHOST-EXACT 未处理（exit 1）")
        return 1
    print("\n结论: 精确幽灵已全部补账 ✓（复跑 dry 验证轧差与现仓一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
