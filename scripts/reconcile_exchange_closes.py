# -*- coding: utf-8 -*-
r"""交易所侧平仓（SL 触发等）落账对账（F2，2026-07-06）。

背景：algo 止损单被交易所执行后，账本（live/demo trades.db）无任何落账路径——
仓位从 position_snapshots 消失、trades 表却无 close 行，形成「幽灵仓」：
  - 账本轧差净持仓 > OKX API 现仓 → 幽灵（多为 SL 触发平仓漏记账）；

逻辑：
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
      raw 标 reconcile_source='exchange_fills_sl'）；目标 cycle 已存在时先读原行，
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

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


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


def consume_recorded(groups, rows, t0_dt):
    """账本已有 close/stop_loss/reduce 行 → 销账对应 fills 组（sz 相等 + 时间窗内）。

    返回 (remaining_groups, consume_notes)。销不掉的账本 close 行只记备注
    （可能超 fills API 窗口）——最终以「剩余组合计 == 幽灵 sz」硬门兜底。
    """
    notes, remaining = [], list(groups)
    for r in rows:
        act = (r["action"] or "").lower()
        if act not in ("close", "stop_loss", "reduce"):
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
        "reasoning": (f"reconcile_exchange_closes 补账：交易所侧平仓（SL 触发）漏落账；"
                      f"fills 实证 {len(all_fills)} 笔 ordId={','.join(ord_ids)} "
                      f"平仓时刻={close_ts} pnl={tot_pnl}"),
        "deviation": None,
        "degradation": None,
        "pnl": tot_pnl,
        "raw": {"reconcile_source": "exchange_fills_sl", "close_ts": close_ts,
                "ord_ids": ord_ids, "fills": fills_evidence},
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
        "reconcile_source": "exchange_fills_sl",
        "reconciled_at": datetime.now(CST).strftime(TS_FMT),
        "symbol": sym, "side": side, "ghost_sz": ghost_sz,
        "close_ts": close_ts, "pnl": tot_pnl, "wavg_px": wavg_px,
        "ord_ids": ord_ids,
        "fills": fills_evidence,
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
        "action": f"reconcile: {sym} {side} close {tot_sz:g} @ {wavg_px:.6g} (exchange-side SL)",
        "note": (f"reconcile_exchange_closes 补账 pnl={tot_pnl}"
                 + (f" | 原行 note: {prev_note[:300]}" if prev_note else "")),
        "n_orders": len(trades),
        "equity": (prev["equity"] if prev else None),
        "trades": trades,
        "raw": raw_obj,
        "_profile": profile,
    }
    result = trades_writer.write_trades(data, Path(db_path))
    # 经验库闭环（非致命）：只喂 reconcile 那一行，防止合并进来的原 trades 重复写经验
    exp = trades_writer.write_experiences(
        {"cycle_id": cycle_id, "trades": [reconcile_trade]}, profile, close_ts)
    return {"cycle_id": cycle_id, "close_ts": close_ts, "sz": tot_sz, "pnl": tot_pnl,
            "wavg_px": wavg_px, "ord_ids": ord_ids, "writer": result, "exp": exp,
            "merged_prev_trades": len(prev_trades)}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="交易所侧平仓（SL 触发）落账对账")
    ap.add_argument("--profile", choices=["live", "demo"], required=True)
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--apply", action="store_true",
                    help="对精确匹配幽灵经 trades_writer 补 close 行（默认 dry-run 只报告）")
    args = ap.parse_args()

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
        print("\n结论: 无幽灵仓（账本 ≤ 现仓）✓")
        con.close()
        return 0

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
            fills = fetch_reduce_fills(args.profile, sym, side, t0_ms)
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
        if remaining and abs(rem_sz - ghost_sz) <= SZ_TOL:
            exact.append(((sym, side), ghost_sz, remaining, detail))
            continue
        # 规则 b：唯一单组 sz == 幽灵 sz（其余剩余组=独立未记账成交，只报告不写）
        hits = [g for g in remaining if abs(g["sz"] - ghost_sz) <= SZ_TOL]
        if len(hits) == 1:
            leftover = [g for g in remaining if g is not hits[0]]
            detail.append(f"  规则b命中：唯一 ordId={hits[0]['ordId']} 组 "
                          f"sz={hits[0]['sz']:g} == 幽灵 sz；其余 {len(leftover)} 组"
                          f"为独立未记账成交（只报告不写）")
            for g in leftover:
                detail.append(f"  [LEFTOVER] ordId={g['ordId']} sz={g['sz']:g} "
                              f"px≈{g['wavg_px']:.6g} pnl={g['pnl']:+.6g} "
                              f"t={fill_dt(g['t_last_ms']).strftime(TS_FMT)} "
                              f"—— 疑似未记账小额往返（净额自平），人工核")
            exact.append(((sym, side), ghost_sz, hits, detail))
        else:
            reason = (f"剩余 fills 组无法唯一对齐幽灵 sz（sz 相等组 {len(hits)} 个）"
                      if remaining else "窗口内无未销账的平仓 fills")
            fuzzy.append(((sym, side), ghost_sz, reason, detail))

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

    rc_apply_err = False
    if args.apply and exact:
        print(f"\n== APPLY：补账 {len(exact)} 项（经 trades_writer.write_trades）==")
        for (sym, side), ghost_sz, matched, _ in exact:
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
    print("\n结论: 精确幽灵已全部补账 ✓（复跑 dry 验证轧差与现仓一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
