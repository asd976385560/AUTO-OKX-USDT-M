# -*- coding: utf-8 -*-
"""repair_queue 人工关单工具（D6 L2，2026-07-15）。

repair_queue 两类行的关单纪律：
  - check_name='push_format'（validate_push_format 写）：噪音类，L1 已自动自愈关单；
    历史积压可用 --bulk-open-pushformat 批量关（须给 --resolution 证据说明）。
  - 其余（order_executor 真金 fail-safe 行，status='pending'）：**只准人工逐条关**
    （--ids 显式指定），关前先核对对应订单/持仓已人工处置完毕。

用法（默认 dry-run，--apply 才动库）：
  repair_queue_tool.py --list [--status open,pending]
  repair_queue_tool.py --ids 158,159 --resolution "已核对xx" [--apply]
  repair_queue_tool.py --bulk-open-pushformat --resolution "证据说明" [--apply]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
CST = timezone(timedelta(hours=8))
DB_PATH = Path(__file__).resolve().parent.parent / "db" / "account.db"


def _conn(rw: bool, db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open exactly ``db_path``; callers using an isolated db-root must pass it."""
    db_path = db_path.resolve()
    con = (sqlite3.connect(str(db_path), timeout=15) if rw
           else sqlite3.connect(db_path.as_uri() + "?mode=ro",
                                uri=True, timeout=15))
    con.row_factory = sqlite3.Row
    return con


def do_list(status_filter: str, db_path: Path = DB_PATH) -> int:
    con = _conn(rw=False, db_path=db_path)
    q = "SELECT id, ts, check_name, status, substr(issue,1,60) issue FROM repair_queue"
    args: list = []
    if status_filter:
        sts = [s.strip() for s in status_filter.split(",") if s.strip()]
        q += f" WHERE status IN ({','.join('?' * len(sts))})"
        args = sts
    q += " ORDER BY id"
    rows = con.execute(q, args).fetchall()
    for r in rows:
        print(f"  #{r['id']:<4} {r['ts']} [{r['status']:<7}] {r['check_name']:<22} {r['issue']}")
    agg = con.execute(
        "SELECT status, COUNT(*) n FROM repair_queue GROUP BY status").fetchall()
    print(f"\n共 {len(rows)} 行匹配 | 全表: " + " ".join(f"{r['status']}={r['n']}" for r in agg))
    con.close()
    return 0


def do_close(ids: list[int] | None, bulk_pushformat: bool, resolution: str,
             apply: bool, closed_by: str = "repair_queue_tool:manual",
             db_path: Path = DB_PATH, quiet: bool = False) -> int:
    """关单唯一入口。`closed_by` 供确定性调用方（如 ledger_autoheal）标注来源，
    人工 CLI 路径保持默认 `repair_queue_tool:manual` 不变。"""
    def emit(*args, **kwargs) -> None:
        if not quiet:
            print(*args, **kwargs)

    if not resolution or len(resolution.strip()) < 8:
        print("[FAIL] --resolution 必填且须为有意义的处置说明（>=8 字符）", file=sys.stderr)
        return 2
    con = _conn(rw=False, db_path=db_path)
    if bulk_pushformat:
        rows = con.execute(
            "SELECT id, ts, check_name, status FROM repair_queue "
            "WHERE check_name='push_format' AND status='open' ORDER BY id").fetchall()
    else:
        rows = con.execute(
            f"SELECT id, ts, check_name, status FROM repair_queue "
            f"WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        missing = set(ids) - {r["id"] for r in rows}
        if missing:
            print(f"[FAIL] id 不存在: {sorted(missing)}", file=sys.stderr)
            con.close()
            return 2
        already = [r["id"] for r in rows if r["status"] in ("closed", "resolved")]
        if already:
            emit(f"[WARN] 已是关闭态将跳过: {already}")
            rows = [r for r in rows if r["status"] not in ("closed", "resolved")]
    con.close()
    if not rows:
        emit("无可关行。")
        return 0
    for r in rows:
        emit(f"  关 -> #{r['id']} {r['ts']} [{r['status']}] {r['check_name']}")
    if not apply:
        emit(f"\nDRY-RUN：{len(rows)} 行将被关（加 --apply 执行）")
        return 0
    now_cst = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    target_ids = [r["id"] for r in rows]
    wcon = _conn(rw=True, db_path=db_path)
    wcon.execute("PRAGMA busy_timeout=15000")
    with wcon:
        cur = wcon.execute(
            f"UPDATE repair_queue SET status='closed', closed_at=?, "
            f"closed_by=?, resolution=? "
            f"WHERE id IN ({','.join('?' * len(target_ids))}) "
            f"AND status NOT IN ('closed','resolved')",
            [now_cst, closed_by, resolution.strip(), *target_ids])
    emit(f"已关 {cur.rowcount} 行（closed_by={closed_by}）")
    wcon.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="repair_queue 人工关单（默认 dry-run）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--ids", type=str, help="逗号分隔 id（order_executor 行只准走这里逐条关）")
    g.add_argument("--bulk-open-pushformat", action="store_true",
                   help="批量关全部 open 的 push_format 行（须 --resolution 给证据）")
    ap.add_argument("--status", type=str, default="", help="--list 的状态过滤，逗号分隔")
    ap.add_argument("--resolution", type=str, default="", help="处置说明（关单必填）")
    ap.add_argument("--apply", action="store_true", help="真执行（默认 dry-run）")
    args = ap.parse_args()
    if not DB_PATH.exists():
        print(f"[FAIL] account.db 不存在: {DB_PATH}", file=sys.stderr)
        return 2
    if args.list:
        return do_list(args.status)
    ids = None
    if args.ids:
        try:
            ids = [int(x) for x in args.ids.split(",") if x.strip()]
        except ValueError:
            print("[FAIL] --ids 须为逗号分隔整数", file=sys.stderr)
            return 2
    return do_close(ids, args.bulk_open_pushformat, args.resolution, args.apply)


if __name__ == "__main__":
    sys.exit(main())
