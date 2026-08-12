# -*- coding: utf-8 -*-
"""导出 V2.0 业务数据库 schema 到 db/schema.sql。"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from _db_ro import connect_ro

SCHEMA_VERSION = "V2.0"
DEFAULT_DB_ROOT = Path(r"./db")
# V2.0 规范库；drill.db 永久保留为业务只读历史归档，唯一授权维护写入口
# 为 scripts/archive/drill/drill_reconcile.py --apply（2026-08-06 归档）。
# schema 导出不得暗示删除该库或维护入口。
DBS = (
    "market.db",
    "news.db",
    "account.db",
    "lessons.db",
    "drill.db",
    "regime.db",
    "analysis.db",
    "live_trades.db",
    "ledger.db",
    "qq_push_dedupe.db",
)
# 排除 sqlite 系统表
EXCLUDE = ("sqlite_sequence", "sqlite_stat1")
CST = timezone(timedelta(hours=8))


def normalize_schema_comments(db_name: str, table_name: str, sql: str) -> str:
    """清理旧 DDL 内嵌注释；只改注释，不改表、列、约束或索引。"""
    if db_name == "live_trades.db" and table_name == "trade_cycles":
        return re.sub(
            r"(?m)^(\s*mode\s+TEXT,\s+--\s*).*$",
            r"\1live（由 trades_writer 按 profile 写入）",
            sql,
        )
    return sql


def export_schema(
    db_root: Path,
    out_path: Path,
    *,
    db_names: Iterable[str] = DBS,
    exported_at: str | None = None,
) -> tuple[int, int]:
    """只读各业务库并写出权威 DDL；返回（字符数，逻辑行数）。"""
    root = Path(db_root)
    stamp = exported_at or datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "-- OKX 永续合约自主交易系统 - 数据库 Schema",
        f"-- 导出时间: {stamp} CST",
        f"-- 版本: {SCHEMA_VERSION}",
        f"-- 数据库目录: {root}\\",
        "-- 本文件供 AI 读取表结构使用，不要手动编辑（改 schema 后跑 export_schema.py 重生成）",
        "-- 核心拆分库由 init_v20_dbs.py 初始化；增量变更走 apply_* 幂等迁移脚本",
        "",
    ]

    for db_name in db_names:
        db_path = root / db_name
        if not db_path.exists():
            lines.append(f"-- 数据库 {db_name} 不存在")
            lines.append("")
            continue
        lines.extend(
            [
                "-- " + "=" * 60,
                f"-- 数据库: {db_name}",
                "-- " + "=" * 60,
                "",
            ]
        )
        conn = connect_ro(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' AND sql IS NOT NULL ORDER BY name"
            )
            for name, sql in cursor.fetchall():
                if name in EXCLUDE:
                    continue
                lines.append(normalize_schema_comments(db_name, name, sql) + ";")
                lines.append("")

            cursor.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND sql IS NOT NULL ORDER BY name"
            )
            for name, sql in cursor.fetchall():
                # 自动索引的约束已经包含在 CREATE TABLE 中。
                if name.startswith("sqlite_autoindex"):
                    continue
                lines.append(sql + ";")
                lines.append("")
        finally:
            conn.close()

    output = "\n".join(lines)
    Path(out_path).write_text(output, encoding="utf-8")
    return len(output), len(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出 V2.0 SQLite 权威 DDL")
    ap.add_argument("--db-root", default=str(DEFAULT_DB_ROOT))
    ap.add_argument("--out", help="输出文件；默认 <db-root>/schema.sql")
    args = ap.parse_args(argv)
    db_root = Path(args.db_root)
    out_path = Path(args.out) if args.out else db_root / "schema.sql"
    chars, lines = export_schema(db_root, out_path)
    print(f"schema.sql written to {out_path}")
    print(f"  {chars} chars, {lines} lines, {SCHEMA_VERSION}, {len(DBS)} databases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
