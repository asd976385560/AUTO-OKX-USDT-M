# -*- coding: utf-8 -*-
r"""apply_repair_queue_schema.py — v7.0e.5 治本：补 account.db.repair_queue 表

背景：v7.0d/e skill.md §4.5 / cron-trigger.md §3.1 / template-unified.md §11 都引用
  account.db.repair_queue 表，但实际 schema 没有此表（v7.0e.5 审计发现 P0 失真）。
本脚本幂等创建该表，3 列：ts / check_name / issue / fix_action。
同时往 doc_versions 表写一条变更记录。

调用：
  pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\apply_repair_queue_schema.py
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "account.db")

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


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"[FAIL] account.db 不存在: {DB_PATH}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(DB_PATH, timeout=30)
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
        print(f"[OK] repair_queue 表已就位：列={col_names}")
        print(f"[OK] doc_versions 已写入 apply_repair_queue_schema.py v7.0e.5")
        return 0
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
