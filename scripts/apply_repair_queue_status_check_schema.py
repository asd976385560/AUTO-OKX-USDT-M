# -*- coding: utf-8 -*-
r"""apply_repair_queue_status_check_schema.py — repair_queue.status CHECK 约束迁移。

背景（2026-08-09 代码一致性审计 P2-1，主人 2026-08-10 拍板执行）：status 词汇
纪律（open=噪音类 / pending=真金类 / closed+resolved=终态）此前纯靠约定，写方
三处硬编码、schema 无 CHECK。语义所有权已在代码层解决（sync_repair_queue
fail-closed + 家族窄前缀），本迁移补最后一层库级词汇护栏。

SQLite 加 CHECK 须重建表：备份（backup API + quick_check）→ 单事务
建新表(含 CHECK)→ 整表拷贝 → drop/rename → 重建三索引。AUTOINCREMENT 的
sqlite_sequence 随显式 id 拷贝与 RENAME 自动跟随。

默认 dry-run 只打印计划与现状；--apply 必须配 --backup-dir。幂等：已含
CHECK 时直接报告 already-applied。任何未知 status 值存在时 fail-closed 拒迁。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ALLOWED_STATUSES = ("open", "pending", "closed", "resolved")

NEW_DDL = """CREATE TABLE repair_queue_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    issue       TEXT NOT NULL,
    fix_action  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('open','pending','closed','resolved')),
    cycle_id    INTEGER,
    created_utc TEXT NOT NULL,
    closed_at   TEXT,
    closed_by   TEXT,
    resolution  TEXT
)"""

COLS = ("id, ts, check_name, issue, fix_action, status, cycle_id, "
        "created_utc, closed_at, closed_by, resolution")

INDEXES = (
    "CREATE INDEX idx_repair_queue_check ON repair_queue(check_name)",
    "CREATE INDEX idx_repair_queue_status ON repair_queue(status)",
    "CREATE INDEX idx_repair_queue_ts ON repair_queue(ts)",
)


def table_sql(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='repair_queue'"
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError("repair_queue 表不存在")
    return str(row[0])


def check_expression_selftest() -> None:
    """在内存库执行同一 DDL 并试插非法值，验证 CHECK 表达式真的拦。"""
    mem = sqlite3.connect(":memory:")
    mem.execute(NEW_DDL.replace("repair_queue_new", "repair_queue"))
    mem.execute(
        "INSERT INTO repair_queue(ts,check_name,issue,status,created_utc) "
        "VALUES('t','c','i','pending','t')")
    try:
        mem.execute(
            "INSERT INTO repair_queue(ts,check_name,issue,status,created_utc) "
            "VALUES('t','c','i','bogus','t')")
    except sqlite3.IntegrityError:
        mem.close()
        return
    mem.close()
    raise RuntimeError("CHECK 表达式自检失败：非法 status 未被拦截")


def main() -> int:
    ap = argparse.ArgumentParser(description="repair_queue.status CHECK 约束迁移（默认 dry-run）")
    ap.add_argument("--db", default=r"./db/account.db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backup-dir", default=None,
                    help="--apply 必填：备份 account.db 的目录")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        ap.error("--apply and --dry-run are mutually exclusive")
    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"库不存在: {db_path}"}))
        return 2

    con = sqlite3.connect(str(db_path), timeout=15)
    con.execute("PRAGMA busy_timeout=10000")
    try:
        current_sql = table_sql(con)
        already = "CHECK" in current_sql and "'resolved'" in current_sql
        statuses = {str(r[0]) for r in con.execute(
            "SELECT DISTINCT status FROM repair_queue")}
        unknown = sorted(statuses - set(ALLOWED_STATUSES))
        n_before = con.execute("SELECT COUNT(*) FROM repair_queue").fetchone()[0]

        report = {
            "db": str(db_path), "dry_run": not args.apply,
            "already_applied": already, "rows": n_before,
            "distinct_statuses": sorted(statuses), "unknown_statuses": unknown,
            "allowed": list(ALLOWED_STATUSES),
        }
        if already:
            print(json.dumps({**report, "ok": True, "action": "none"},
                             ensure_ascii=False, indent=1))
            return 0
        if unknown:
            print(json.dumps({**report, "ok": False,
                              "error": "存在未知 status，fail-closed 拒迁"},
                             ensure_ascii=False, indent=1))
            return 2
        if not args.apply:
            print(json.dumps({**report, "ok": True, "action": "plan-only"},
                             ensure_ascii=False, indent=1))
            return 0
        if not args.backup_dir:
            print(json.dumps({**report, "ok": False,
                              "error": "--apply 必须配 --backup-dir"},
                             ensure_ascii=False, indent=1))
            return 2

        check_expression_selftest()

        bdir = Path(args.backup_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_path = bdir / f"account.db.bak_repairq-check_{tag}"
        bak = sqlite3.connect(str(bak_path))
        with bak:
            con.backup(bak)
        bak_ok = bak.execute("PRAGMA quick_check").fetchone()[0]
        bak.close()
        if bak_ok != "ok":
            print(json.dumps({**report, "ok": False,
                              "error": f"备份 quick_check={bak_ok}，中止"},
                             ensure_ascii=False, indent=1))
            return 2

        con.execute("BEGIN IMMEDIATE")
        con.execute(NEW_DDL)
        con.execute(
            f"INSERT INTO repair_queue_new({COLS}) SELECT {COLS} FROM repair_queue")
        n_copied = con.execute(
            "SELECT COUNT(*) FROM repair_queue_new").fetchone()[0]
        if n_copied != n_before:
            con.rollback()
            print(json.dumps({**report, "ok": False,
                              "error": f"拷贝行数不符 {n_copied}!={n_before}，已回滚"},
                             ensure_ascii=False, indent=1))
            return 2
        con.execute("DROP TABLE repair_queue")
        con.execute("ALTER TABLE repair_queue_new RENAME TO repair_queue")
        for idx in INDEXES:
            con.execute(idx)
        con.commit()

        final_sql = table_sql(con)
        n_after = con.execute("SELECT COUNT(*) FROM repair_queue").fetchone()[0]
        qc = con.execute("PRAGMA quick_check").fetchone()[0]
        print(json.dumps({
            **report, "ok": ("CHECK" in final_sql and n_after == n_before
                             and qc == "ok"),
            "action": "applied", "rows_after": n_after, "quick_check": qc,
            "backup": str(bak_path),
            "check_in_ddl": "CHECK" in final_sql,
        }, ensure_ascii=False, indent=1))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
