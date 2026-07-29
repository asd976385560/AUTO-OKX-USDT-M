# -*- coding: utf-8 -*-
r"""experience_hygiene.py — trade_experiences 一次性卫生迁移（rank6 2026-07-16）。

背景（架构审查 + 07-16 复核实测，87 行表）：写侧此前裸 INSERT 零幂等 + close 只配最新
open → ①15 组精确重复（同 profile+symbol+side+action+cycle_id）36 行=21 行冗余，closed
重复直接放大 find_similar 的 n/win_rate；②~18 条陈旧悬挂 open（永远等不到 close，还会
偷走未来无新 open 时的 close 匹配）；③毒行 id80（错配 open 得 pnl_pct=+38.1%，恒入引用
样本拉偏 avg_pnl_pct）。写侧幂等已同日修入 trade_experience_writer/trades_writer（防再犯），
本脚本清存量。

处置：
  ①重复组：留最优一行（closed 有 outcome > pnl_pct 非空 > score 非空 > id 最小），余删；
  ②毒行（--poison-ids，默认 80）：pnl_pct=NULL、hit_1R=0——行保留但退出引用样本
    （find_similar 过滤 pnl_pct IS NOT NULL）；
  ③陈旧悬挂 open（status='open' 且 ts < --stale-cutoff，默认 2026-07-16 00:00:00）：
    status='expired'——对全部消费方隐身（closed 正向过滤 / open 匹配都不再命中），
    行体保留可审计。当前真实持仓的 open 行（07-16 的 87/88/89/90）在 cutoff 之后不受影响。

默认 dry-run 全量打印处置计划；--apply 先备份 account.db 到 tmp/archive/ 再执行。
幂等：重复执行第二遍应零变更。
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
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ACCOUNT_DB = Path(os.environ.get("OKX_ACCOUNT_DB", _project_path('db', 'account.db')))
ARCHIVE_ROOT = Path(_project_path('tmp', 'archive'))


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
    con.row_factory = sqlite3.Row
    return con


def plan_dupes(con) -> list[dict]:
    """重复组处置计划：每组留最优一行，余标删。"""
    groups = con.execute(
        "SELECT profile, symbol, side, action, cycle_id, COUNT(*) n "
        "FROM trade_experiences "
        "GROUP BY profile, symbol, side, action, cycle_id HAVING n > 1").fetchall()
    plan = []
    for g in groups:
        rows = con.execute(
            "SELECT id, status, pnl_pct, score_total, ts FROM trade_experiences "
            "WHERE profile=? AND symbol=? AND side=? AND action=? AND cycle_id=? "
            "ORDER BY (status='closed') DESC, (pnl_pct IS NOT NULL) DESC, "
            "(score_total IS NOT NULL) DESC, id ASC",
            (g["profile"], g["symbol"], g["side"], g["action"], g["cycle_id"])
        ).fetchall()
        keep, drop = rows[0], rows[1:]
        plan.append({
            "group": f"{g['profile']}/{g['symbol']}/{g['side']}/{g['action']}/{g['cycle_id']}",
            "keep": {"id": keep["id"], "status": keep["status"],
                     "pnl_pct": keep["pnl_pct"], "score": keep["score_total"]},
            "drop_ids": [r["id"] for r in drop],
        })
    return plan


def plan_poison(con, ids: list[int]) -> list[dict]:
    plan = []
    for pid in ids:
        r = con.execute(
            "SELECT id, profile, symbol, side, status, pnl_pct, hold_hours "
            "FROM trade_experiences WHERE id=?", (pid,)).fetchone()
        if r is None:
            continue
        if r["pnl_pct"] is None:
            continue  # 已处理过（幂等）
        plan.append({"id": r["id"], "symbol": r["symbol"], "profile": r["profile"],
                     "old_pnl_pct": r["pnl_pct"], "hold_hours": r["hold_hours"],
                     "action": "pnl_pct=NULL, hit_1R=0（错配 open 的产物，退出引用样本）"})
    return plan


def plan_stale_opens(con, cutoff: str, drop_ids: set[int]) -> list[dict]:
    rows = con.execute(
        "SELECT id, profile, symbol, side, ts, cycle_id FROM trade_experiences "
        "WHERE status='open' AND ts < ? ORDER BY id", (cutoff,)).fetchall()
    return [{"id": r["id"], "profile": r["profile"], "symbol": r["symbol"],
             "side": r["side"], "ts": r["ts"], "cycle_id": r["cycle_id"]}
            for r in rows if r["id"] not in drop_ids]  # 重复组已删的不重复列


def main() -> int:
    ap = argparse.ArgumentParser(description="trade_experiences 卫生迁移（rank6）")
    ap.add_argument("--apply", action="store_true", help="真执行（默认 dry-run 只打印计划）")
    ap.add_argument("--poison-ids", default="80",
                    help="毒行 id 列表（逗号分隔，默认 80；空串跳过该步）")
    ap.add_argument("--stale-cutoff", default="2026-07-16 00:00:00",
                    help="悬挂 open 陈旧判定线（ts 早于此且仍 open → expired）")
    args = ap.parse_args()

    if not ACCOUNT_DB.exists():
        print(json.dumps({"ok": False, "error": f"account.db 不存在: {ACCOUNT_DB}"}))
        return 2
    poison_ids = [int(x) for x in str(args.poison_ids).split(",") if x.strip().isdigit()]

    con = connect(ACCOUNT_DB)
    try:
        total_before = con.execute("SELECT COUNT(*) FROM trade_experiences").fetchone()[0]
        dupes = plan_dupes(con)
        drop_ids = {i for d in dupes for i in d["drop_ids"]}
        poison = plan_poison(con, [i for i in poison_ids if i not in drop_ids])
        stale = plan_stale_opens(con, args.stale_cutoff, drop_ids)

        report = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "db": str(ACCOUNT_DB), "total_before": total_before,
            "dry_run": not args.apply,
            "dup_groups": len(dupes), "drop_rows": len(drop_ids),
            "poison_rows": len(poison), "stale_open_rows": len(stale),
            "plan": {"dupes": dupes, "poison": poison, "stale_opens": stale},
        }
        if not args.apply:
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 0

        # ── 真执行：先备份 ──
        bdir = ARCHIVE_ROOT / f"{datetime.now():%Y%m%d}-rank6-exp-hygiene"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ACCOUNT_DB, bdir / "account.db.bak")
        (bdir / "hygiene_plan.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        for i in sorted(drop_ids):
            cur.execute("DELETE FROM trade_experiences WHERE id=?", (i,))
        for p in poison:
            cur.execute("UPDATE trade_experiences SET pnl_pct=NULL, hit_1R=0 "
                        "WHERE id=?", (p["id"],))
        for s in stale:
            cur.execute("UPDATE trade_experiences SET status='expired' WHERE id=?",
                        (s["id"],))
        con.commit()
        total_after = con.execute("SELECT COUNT(*) FROM trade_experiences").fetchone()[0]
        report["applied"] = {"deleted": len(drop_ids), "poisoned": len(poison),
                             "expired": len(stale), "total_after": total_after,
                             "backup": str(bdir)}
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
