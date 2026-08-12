# -*- coding: utf-8 -*-
"""Small read-only SQLite helper for OKX cron/diagnostic sessions.

Usage examples (always call through run_okx_python.ps1):
  query_db.py ./db//account.db system_state
  query_db.py ./db//account.db cycle_runs --limit 20
  query_db.py ./db//account.db --list-tables
  query_db.py ./db//account.db --sql "SELECT * FROM system_state"

This script intentionally only supports read-only statements.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
READONLY_SQL_RE = re.compile(r"^\s*(select|with|pragma)\b", re.IGNORECASE)

DEFAULT_LIMITS = {
    "account_snapshots": 3,
    "position_snapshots": 20,
    "tick_snapshots": 500,
    "derivatives": 500,
    "kline_cache": 1000,
    "cross_market": 5,
    "scoring_history": 100,
    "trade_events": 50,
    "cycle_runs": 10,
    "playbook": 20,
    "news_items": 50,
    "coin_sentiment": 200,
    "instruments": 10,
}

ORDER_COLUMNS = {
    "tick_snapshots": "ts",
    "derivatives": "ts",
    "kline_cache": "ts",
    "cross_market": "ts",
    "scoring_history": "ts",
    "trade_events": "ts",
    "cycle_runs": "cycle_count",
    "playbook": "ts",
    "news_items": "ts",
    "coin_sentiment": "ts",
}

# 2026-07-03：混合 ts 格式表（UTC-Z 与 CST-space 并发写入）按归一化时间排序——
# 裸列词典序 'T'(0x54)>' '(0x20) 会让 Z 行恒排 CST 行前，"最新 N 条"失真。
ORDER_EXPRS = {
    "news_items": "(CASE WHEN ts LIKE '%Z' THEN datetime(ts) ELSE datetime(ts,'-8 hours') END)",
    "coin_sentiment": "(CASE WHEN ts LIKE '%Z' THEN datetime(ts) ELSE datetime(ts,'-8 hours') END)",
}

ROWID_DEFAULT_TABLES = {
    "account_snapshots",
    "position_snapshots",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read rows from an OKX SQLite DB as JSON.")
    parser.add_argument("db_path", help="SQLite database path")
    parser.add_argument("table", nargs="?", help="Table name to query")
    parser.add_argument("--limit", type=int, default=None, help="Max rows for table mode")
    parser.add_argument("--tf", default=None, help="Optional kline_cache tf filter, e.g. 15m/1H/4H/1D")
    parser.add_argument("--list-tables", action="store_true", help="List table names and exit")
    parser.add_argument("--schema", action="store_true", help="Print PRAGMA table_info(table) and exit")
    parser.add_argument("--sql", default=None, help="Read-only SELECT/WITH/PRAGMA SQL to execute")
    parser.add_argument("--order-by", choices=("auto", "ts", "rowid"), default="auto",
                        help="Table mode ordering. auto uses rowid for tables with legacy non-ISO ts rows.")
    return parser.parse_args()


def quote_ident(name: str) -> str:
    if not IDENT_RE.match(name):
        raise SystemExit(f"invalid table/identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def rows_to_json(rows: list[sqlite3.Row]) -> str:
    return json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2, default=str)


def main() -> int:
    # 入口强制 UTF-8（README 编码约定）：否则 Windows locale=GBK 时
    # print 含中文/emoji 的列（如 trade_cycles.note ⚠ / analysis market_summary）会 UnicodeEncodeError 崩。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    db_path = Path(args.db_path)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        if args.list_tables:
            rows = cur.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
            ).fetchall()
            print(rows_to_json(rows))
            return 0

        if args.sql:
            sql = args.sql.strip()
            if not READONLY_SQL_RE.match(sql):
                raise SystemExit("--sql only allows read-only SELECT/WITH/PRAGMA statements")
            rows = cur.execute(sql).fetchall()
            print(rows_to_json(rows))
            return 0

        if not args.table:
            raise SystemExit("missing table name; use --help or --list-tables")
        table = args.table
        if not table_exists(cur, table):
            raise SystemExit(f"table not found: {table}")

        if args.schema:
            rows = cur.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
            print(rows_to_json(rows))
            return 0

        limit = args.limit if args.limit is not None else DEFAULT_LIMITS.get(table, 100)
        if limit <= 0 or limit > 5000:
            raise SystemExit("--limit must be between 1 and 5000")

        quoted = quote_ident(table)
        params: list[object] = []
        where_sql = ""
        if table == "kline_cache" and args.tf:
            where_sql = " WHERE tf=?"
            params.append(args.tf)

        if args.order_by == "rowid" or (args.order_by == "auto" and table in ROWID_DEFAULT_TABLES):
            sql = f"SELECT * FROM {quoted}{where_sql} ORDER BY ROWID DESC LIMIT ?"
        else:
            order_col = "ts" if args.order_by == "ts" else ORDER_COLUMNS.get(table)
            order_expr = ORDER_EXPRS.get(table) if order_col == "ts" else None
            if order_expr:
                sql = f"SELECT * FROM {quoted}{where_sql} ORDER BY {order_expr} DESC LIMIT ?"
            elif order_col:
                sql = f"SELECT * FROM {quoted}{where_sql} ORDER BY {quote_ident(order_col)} DESC LIMIT ?"
            else:
                sql = f"SELECT * FROM {quoted}{where_sql} ORDER BY ROWID DESC LIMIT ?"
        params.append(limit)
        rows = cur.execute(sql, params).fetchall()
        print(rows_to_json(rows))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
