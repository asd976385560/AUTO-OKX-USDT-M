# -*- coding: utf-8 -*-
r"""apply_repair_queue_schema.py — v7.0e.5 治本：补 account.db.repair_queue 表

背景：v7.0d/e skill.md §4.5 / cron-trigger.md §3.1 / template-unified.md §11 都引用
  account.db.repair_queue 表，但实际 schema 没有此表（v7.0e.5 审计发现 P0 失真）。
本脚本幂等创建该表，3 列：ts / check_name / issue / fix_action。
同时往 doc_versions 表写一条变更记录。

调用：
  pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 \
    <PROJECT_ROOT>\scripts\apply_repair_queue_schema.py --db-root <DB_ROOT>
  # 真写必须显式授权并先在线备份：
  ... apply_repair_queue_schema.py --db-root <DB_ROOT> --apply \
    --backup-dir <BACKUP_DIR>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from migration_guard import (  # noqa: E402
    add_migration_arguments,
    backup_databases,
    resolve_apply,
)

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"

DB_PATH = _PROJECT_ROOT / "db" / "account.db"

DDL = """
CREATE TABLE IF NOT EXISTS repair_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    issue       TEXT NOT NULL,
    fix_action  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    cycle_id    INTEGER,
    created_utc TEXT NOT NULL
);
"""

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_repair_queue_ts ON repair_queue(ts);",
    "CREATE INDEX IF NOT EXISTS idx_repair_queue_check ON repair_queue(check_name);",
    "CREATE INDEX IF NOT EXISTS idx_repair_queue_status ON repair_queue(status);",
]

DOC_VERSION_INSERT = """
INSERT OR REPLACE INTO doc_versions (doc_path, doc_version, last_updated, updated_by, change_summary)
VALUES (
    'scripts/apply_repair_queue_schema.py',
    '7.0e.5',
    ?,
    '维护者指令（自动化工具协助执行）',
    'v7.0e.5 治本：补 account.db.repair_queue 表（3 列 ts/check_name/issue/fix_action + status + cycle_id + 3 索引），幂等创建；写 doc_versions'
);
"""


def inspect(db_path: Path) -> dict:
    if not db_path.is_file():
        return {"ok": False, "error": f"account.db 不存在: {db_path}"}
    conn = sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='repair_queue'"
        ).fetchone() is not None
        cols = (
            [row[1] for row in conn.execute("PRAGMA table_info(repair_queue)")]
            if present
            else []
        )
        return {
            "ok": True,
            "dry_run": True,
            "db": str(db_path),
            "repair_queue_present": present,
            "columns": cols,
            "would_create": not present,
        }
    finally:
        conn.close()


def apply_schema(db_path: Path, backup_dir: Path) -> dict:
    if not db_path.is_file():
        return {"ok": False, "error": f"account.db 不存在: {db_path}"}
    backups = backup_databases(
        [db_path], backup_dir, "repair-queue-schema")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        cur = conn.cursor()
        # 幂等建表
        cur.execute(DDL)
        for ddl in INDEX_DDL:
            cur.execute(ddl)
        # 写 doc_versions
        now_cst = datetime.now(CST).strftime(TS_FMT)
        cur.execute(DOC_VERSION_INSERT, (now_cst,))
        conn.commit()
        # 校验表存在
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='repair_queue'"
        ).fetchone()
        if not row:
            print("[FAIL] repair_queue 表创建后仍找不到", file=sys.stderr)
            return 3
        # 校验列
        cols = cur.execute("PRAGMA table_info(repair_queue)").fetchall()
        col_names = [c[1] for c in cols]
        return {
            "ok": True,
            "db": str(db_path),
            "columns": col_names,
            "backup": str(backups[db_path.resolve()]),
            "doc_version": "7.0e.5",
        }
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return {"ok": False, "error": str(e), "db": str(db_path)}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="幂等创建 account.db.repair_queue")
    parser.add_argument(
        "--db-root", default=str(_PROJECT_ROOT / "db"))
    add_migration_arguments(parser)
    args = parser.parse_args()
    apply_changes = resolve_apply(parser, args)
    db_path = Path(args.db_root) / "account.db"
    result = (
        apply_schema(db_path, Path(args.backup_dir))
        if apply_changes
        else inspect(db_path)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
