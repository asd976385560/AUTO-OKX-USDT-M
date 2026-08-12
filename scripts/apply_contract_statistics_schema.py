# -*- coding: utf-8 -*-
"""Create the official 15m contract-statistics shadow table safely.

The migration is plan-only by default.  ``--apply`` requires a SQLite backup
directory and validates that backup before creating the append-only table and
indexes.  It does not backfill history or modify existing market tables.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


TABLE = "market_contract_statistics"
REQUIRED_COLUMNS = (
    "ts", "collected_ts", "cycle_id", "symbol", "timeframe",
    "oi_contracts", "oi_ccy", "oi_usd", "taker_sell_usd",
    "taker_buy_usd", "taker_buy_ratio", "raw", "source",
)
DDL = """
CREATE TABLE IF NOT EXISTS market_contract_statistics (
    ts TEXT NOT NULL,
    collected_ts TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL CHECK (timeframe = '15m'),
    oi_contracts REAL NOT NULL CHECK (oi_contracts >= 0),
    oi_ccy REAL NOT NULL CHECK (oi_ccy >= 0),
    oi_usd REAL NOT NULL CHECK (oi_usd >= 0),
    taker_sell_usd REAL NOT NULL CHECK (taker_sell_usd >= 0),
    taker_buy_usd REAL NOT NULL CHECK (taker_buy_usd >= 0),
    taker_buy_ratio REAL CHECK (
        taker_buy_ratio IS NULL
        OR (taker_buy_ratio >= 0 AND taker_buy_ratio <= 1)
    ),
    raw TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (cycle_id, symbol, timeframe, source)
)
"""
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_contract_statistics_symbol_ts "
    "ON market_contract_statistics(symbol, ts)",
    "CREATE INDEX IF NOT EXISTS idx_contract_statistics_cycle "
    "ON market_contract_statistics(cycle_id, source)",
)


def _table_columns(con: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in con.execute(f"PRAGMA table_info({TABLE})")
    )


def _backup(
    source: sqlite3.Connection,
    db_path: Path,
    backup_dir: Path,
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"{db_path.name}.bak_contract-stats_{stamp}"
    destination = sqlite3.connect(str(target), timeout=30)
    try:
        source.backup(destination)
        check = destination.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        destination.close()
    if check != "ok":
        raise RuntimeError(f"backup quick_check={check}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=r"./db/market.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup-dir")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"db missing: {db_path}"}))
        return 2

    con = sqlite3.connect(str(db_path), timeout=30)
    con.execute("PRAGMA busy_timeout=20000")
    try:
        exists = bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone())
        columns = _table_columns(con) if exists else ()
        if exists and columns != REQUIRED_COLUMNS:
            print(json.dumps({
                "ok": False,
                "error": "existing table schema mismatch",
                "expected_columns": REQUIRED_COLUMNS,
                "actual_columns": columns,
            }, ensure_ascii=False))
            return 2
        plan = {
            "ok": True,
            "db": str(db_path),
            "dry_run": not args.apply,
            "table": TABLE,
            "table_exists": exists,
            "historical_backfill": False,
            "existing_table_mutations": 0,
        }
        if exists:
            print(json.dumps({**plan, "action": "none"}, ensure_ascii=False))
            return 0
        if not args.apply:
            print(json.dumps({**plan, "action": "plan-only"}, ensure_ascii=False))
            return 0
        if not args.backup_dir:
            print(json.dumps({
                **plan,
                "ok": False,
                "error": "--apply requires --backup-dir",
            }, ensure_ascii=False))
            return 2

        backup_path = _backup(con, db_path, Path(args.backup_dir))
        con.execute(DDL)
        for statement in INDEXES:
            con.execute(statement)
        con.commit()
        actual = _table_columns(con)
        check = con.execute("PRAGMA quick_check").fetchone()[0]
        if actual != REQUIRED_COLUMNS or check != "ok":
            raise RuntimeError(
                f"post-migration validation failed columns={actual} check={check}")
        print(json.dumps({
            **plan,
            "action": "applied",
            "backup": str(backup_path),
            "quick_check": check,
            "columns": actual,
        }, ensure_ascii=False))
        return 0
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        con.rollback()
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False))
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
