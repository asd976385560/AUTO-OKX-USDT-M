# -*- coding: utf-8 -*-
"""demo 虚拟盘账实核对 v2（T8 + 2026-06-13 首夜规划快修组）。

v1（2026-06-12）：ghost / unrecorded / sz_diff 三类对账。
v2（2026-06-13）曾包含 demo 权益快照写入；V2.0 当前已收敛为
`jobb_live_account_check.py --profile demo` 单一权威 writer。本脚本默认只做账实核对，
不写 account_snapshots，也不写 drill.db。
  B. pnl 回填预览：drill 里 close_reason LIKE 'demo-reconcile%' 且 pnl IS NULL 的行，
     按 `swap fills` 真实平仓腿（fillPnl≠0）求和，按 sz 加权展示建议值。
  C. SL 同步预览：`swap algo orders` 在挂单的 slTriggerPx/triggerPx 与 drill open 行
     stop_loss_px 对比，只报告差异。

v3（2026-07-03）：对账基准切 V2.0 真账本 demo_trades.db（trades 轧差净持仓；drill.db 只留
  B/C 段历史行维护）；GHOST 只报告不自动改账（V2.0 账本 writer 纪律）。

退出码: 0=账实一致；1=仅 pnl 待回填（drill 历史行,良性）；3=ghost/unrecorded/sz_diff 账实差异；2=API/库错误
用法: ... run_okx_python.ps1 scripts/demo_account_check.py [--db-root <PROJECT_ROOT>\\db]
写维护统一走 scripts/drill_reconcile.py --apply --backup-dir <目录>。
"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, _project_path('scripts'))
from _okxcli import okx_json  # noqa: E402

BASE = ["--profile", "demo"]


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


def f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def venue_state():
    """返回 (positions dict{(sym,side):sz}, detail list, upl_sum, totalEq, availBal)。"""
    pos_rows = rows_of(okx_json("account", "positions", "--instType", "SWAP", global_args=BASE))
    out, detail, upl_sum = defaultdict(float), [], 0.0
    for r in pos_rows:
        if not isinstance(r, dict):
            continue
        pos = f(r.get("pos"), 0.0)
        if not pos:
            continue
        side = r.get("posSide") if r.get("posSide") in ("long", "short") else ("long" if pos > 0 else "short")
        out[(r.get("instId"), side)] += abs(pos)
        upl_sum += f(r.get("upl"), 0.0) or 0.0
        detail.append(f"{r.get('instId')} {side} {abs(pos)}@{r.get('avgPx')} upl={r.get('upl')}")
    total_eq = avail = None
    for b in rows_of(okx_json("account", "balance", global_args=BASE)):
        if isinstance(b, dict):
            total_eq = f(b.get("totalEq"))
            for d in b.get("details") or []:
                if isinstance(d, dict) and d.get("ccy") == "USDT":
                    avail = f(d.get("availBal") or d.get("availEq"))
                    break
            break
    return out, detail, upl_sum, total_eq, avail


def backfill_pnl(drl):
    """只读预览历史 drill 行的 pnl/close_px 建议值。"""
    rows = drl.execute(
        "SELECT id, symbol, side, sz, ts FROM drill_trades "
        "WHERE status='closed' AND pnl IS NULL AND close_reason LIKE 'demo-reconcile%'"
    ).fetchall()
    if not rows:
        return 0, []
    notes, fixed = [], 0
    by_sym = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    for sym, group in by_sym.items():
        t0 = min(r["ts"] for r in group)
        try:
            t0_ms = int((datetime.fromisoformat(str(t0).replace("Z", "+00:00"))
                         - timedelta(hours=1)).timestamp() * 1000)
        except Exception:
            t0_ms = 0
        try:
            fills = rows_of(okx_json("swap", "fills", "--instId", sym, global_args=BASE))
        except Exception as e:
            notes.append(f"{sym}: fills API 失败 {e}")
            continue

        def pick_closing(rows_):
            return [x for x in rows_
                    if isinstance(x, dict) and abs(f(x.get("fillPnl"), 0.0) or 0.0) > 1e-12
                    and int(x.get("fillTime") or 0) >= t0_ms]

        closing = pick_closing(fills)
        if not closing:
            # 近窗无平仓腿 → --archive 兜底（algo 触发/历史窗口外的成交可能只在归档）
            try:
                closing = pick_closing(rows_of(
                    okx_json("swap", "fills", "--instId", sym, "--archive", global_args=BASE)))
            except Exception:
                pass
        if not closing:
            notes.append(f"{sym}: 无平仓腿 fills（窗口 {t0} 起），pnl 维持 NULL")
            continue
        total_pnl = sum(f(x.get("fillPnl"), 0.0) for x in closing)
        tot_fill_sz = sum(f(x.get("fillSz"), 0.0) or 0.0 for x in closing) or 1.0
        wavg_px = sum((f(x.get("fillPx"), 0.0) or 0.0) * (f(x.get("fillSz"), 0.0) or 0.0)
                      for x in closing) / tot_fill_sz
        tot_sz = sum(f(r["sz"], 0.0) or 0.0 for r in group) or 1.0
        for r in group:
            share = round(total_pnl * (f(r["sz"], 0.0) or 0.0) / tot_sz, 6)
            notes.append(f"{sym} id={r['id']} sz={r['sz']} → pnl={share:+.4f} px≈{wavg_px:.6g}")
            fixed += 1
    return fixed, notes


def sync_sl(drl):
    """只读预览交易所 algo SL 与历史 drill 行的差异。"""
    try:
        algos = rows_of(okx_json("swap", "algo", "orders", global_args=BASE))
    except Exception as e:
        return 0, [f"algo orders API 失败: {e}"]
    sl_map = {}
    for a in algos:
        if not isinstance(a, dict):
            continue
        px = f(a.get("slTriggerPx")) or f(a.get("triggerPx"))
        if px:
            sl_map[a.get("instId")] = px
    if not sl_map:
        return 0, ["无在挂 algo 止损单"]
    notes, n = [], 0
    for r in drl.execute("SELECT id, symbol, stop_loss_px FROM drill_trades WHERE status='open'").fetchall():
        px = sl_map.get(r["symbol"])
        if px is None:
            continue
        if r["stop_loss_px"] is None or abs(f(r["stop_loss_px"], 0.0) - px) > 1e-12:
            notes.append(f"{r['symbol']} id={r['id']} stop_loss_px {r['stop_loss_px']} → {px}")
            n += 1
    return n, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument(
        "--apply",
        action="store_true",
        help="已停用；写维护统一走 drill_reconcile.py",
    )
    ap.add_argument("--backup-dir", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.apply:
        print(
            "[demo_check][ERROR] 本脚本只读；写维护请使用 "
            "drill_reconcile.py --apply --backup-dir <目录>"
        )
        sys.exit(2)

    try:
        ven, ven_detail, upl_sum, total_eq, avail = venue_state()
    except Exception as e:
        print(f"[demo_check][ERROR] 虚拟盘 API 失败: {e}")
        sys.exit(2)

    drill_path = Path(args.db_root) / "drill.db"
    drl = sqlite3.connect(
        f"file:{drill_path.as_posix()}?mode=ro", uri=True, timeout=15
    )
    drl.row_factory = sqlite3.Row

    # demo 账户/持仓快照统一由 jobb_live_account_check.py --profile demo 写入。
    print(
        f"== demo 账实核对 v2 @ "
        f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z (REPORT-ONLY) =="
    )
    print(f"DEMO_EQUITY totalEq={total_eq} availBal={avail} upl={round(upl_sum,4)} source=OKX_API")
    print(
        f"（↑ demo 资产/绩效展示唯一口径：P5 current_equity / 推送『模拟盘资金』"
        f"一律用 totalEq={total_eq}；非开仓容量，OPEN 只认实时 max-size）"
    )
    print(f"虚拟盘 {len(ven)} 仓: " + ("; ".join(ven_detail) if ven_detail else "空"))

    # 2026-07-03 对账基准切换（主人拍板）：drill.db.drill_trades（V1.x 演练账本，06-13 起死库，
    # demo 有仓时恒误报 UNRECORDED）→ demo_trades.db.trades（V2.0 demo 真账本）按 open/close
    # 轧差出净持仓。基准库只读打开；GHOST 不再 --apply 自动改账（V2.0 账本唯一 writer=trader
    # 经 trades_writer，本脚本只报告，修账走人工核 orders-history 后经 writer 补账）。
    led = defaultdict(float)
    demo_db = str(args.db_root).replace("\\", "/") + "/demo_trades.db"
    dcon = sqlite3.connect(f"file:{demo_db}?mode=ro", uri=True, timeout=15)
    dcon.row_factory = sqlite3.Row
    n_rows = 0
    for r in dcon.execute("SELECT symbol, action, side, sz FROM trades"):
        act = (r["action"] or "").lower()
        sz = f(r["sz"], 0.0) or 0.0
        k = (r["symbol"], norm_side(r["side"]))
        if act in ("open", "add"):
            led[k] += sz
            n_rows += 1
        elif act in ("close", "stop_loss", "reduce"):
            led[k] -= sz
            n_rows += 1
    dcon.close()
    led = {k: v for k, v in led.items() if abs(v) > 1e-9}
    print(f"账本(demo_trades.db 轧差) {n_rows} 行 / 净持仓 {len(led)} 组")

    ghosts = [(k, sz) for k, sz in led.items() if k not in ven]
    unrecorded = [(k, sz) for k, sz in ven.items() if k not in led]
    sz_diff = [(k, sz, ven[k]) for k, sz in led.items() if k in ven and abs(ven[k] - sz) > 1e-9]

    if ghosts:
        print(f"\n[GHOST] {len(ghosts)} 组（账本有净持仓、交易所无——多为平仓漏记账）:")
        for (sym, side), sz in ghosts:
            print(f"  {sym} {side} sz={sz} → 人工核 orders-history 后经 trades_writer 补 close 行"
                  f"（本脚本只报告不改 V2.0 账本）")
    if unrecorded:
        print(f"\n[UNRECORDED][P2] {len(unrecorded)} 组（下单成功未记账）:")
        for (sym, side), sz in unrecorded:
            print(f"  {sym} {side} sz={sz} → 按 fills 真实 avgPx 补记 drill 行")
    if sz_diff:
        print(f"\n[SZ_DIFF] {len(sz_diff)} 组:")
        for (sym, side), lsz, vsz in sz_diff:
            print(f"  {sym} {side} 账本={lsz} 虚拟盘={vsz} → 按 fills 修正 sz")

    # B. pnl 回填
    fixed, notes = backfill_pnl(drl)
    if notes:
        print(f"\n[PNL-BACKFILL](预览 {fixed}):")
        for x in notes:
            print(f"  {x}")

    # C. SL 同步
    n_sl, sl_notes = sync_sl(drl)
    if sl_notes:
        print(f"\n[SL-SYNC](预览 {n_sl}):")
        for x in sl_notes:
            print(f"  {x}")

    drl.close()
    # 退出码契约区分 ghost/unrecorded/sz_diff 与“仅 pnl 待回填”：
    #   0=账实一致 / 1=仅 pnl 待维护（良性，只读报告） / 3=ghost/unrecorded/sz_diff 账实差异。
    # 注（2026-07-15 修正）：对账基准自 v3 起已是 demo_trades.db 轧差（上一版注释失实）。
    # 07-13 起的恒 rc=3 实为账本漏记（ETH 三笔平仓+一对探针开平未回执）——07-15 已按 fills
    # 实录经 trades_writer 补账（RECON-20260715，备份 tmp/archive/20260715-eth-ghost-recon/）。
    # 日后再见持续 rc=3：先跑轧差 vs API 现仓对照找漏记行，按本例经 writer 补账，勿疑基准。
    if ghosts or unrecorded or sz_diff:
        print("\n结论: 账实差异（exit 3：ghost/unrecorded/sz_diff，基准=demo_trades.db 轧差）")
        sys.exit(3)
    if notes and any("pnl=" in x for x in notes):
        print("\n结论: 仅 pnl 待维护（exit 1；写维护统一走 drill_reconcile.py）")
        sys.exit(1)
    print("\n结论: 账实一致 ✓")
    sys.exit(0)


if __name__ == "__main__":
    main()
