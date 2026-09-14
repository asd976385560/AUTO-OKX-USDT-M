# -*- coding: utf-8 -*-
"""Migrate market_positioning to an immutable per-collection batch key.

The legacy primary key ``(ts, symbol, timeframe)`` lets a later collection
replace an earlier cycle whenever OKX returns the same upstream data timestamp.
This migration changes only that key to
``(cycle_id, symbol, timeframe, source)``.  Existing rows are preserved exactly;
missing historical rows are deliberately not reconstructed or backfilled.

The command is plan-only by default.  ``--apply`` requires a verified SQLite
backup directory, rebuilds the table inside one ``BEGIN IMMEDIATE`` transaction,
and validates row counts, the target primary key, indexes and ``quick_check``.
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


TABLE = "market_positioning"
TEMP_TABLE = "market_positioning__batch_key_v2"
REQUIRED_COLUMNS = (
    "ts", "collected_ts", "cycle_id", "symbol", "timeframe",
    "long_ratio", "short_ratio", "long_short_ratio", "raw", "source",
)
LEGACY_PRIMARY_KEY = ("ts", "symbol", "timeframe")
TARGET_PRIMARY_KEY = ("cycle_id", "symbol", "timeframe", "source")
TARGET_DDL = f"""
CREATE TABLE {TEMP_TABLE} (
    ts               TEXT NOT NULL,
    collected_ts     TEXT NOT NULL,
    cycle_id         TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    timeframe        TEXT NOT NULL DEFAULT '1H',
    long_ratio       REAL,
    short_ratio      REAL,
    long_short_ratio REAL,
    raw              TEXT,
    source           TEXT NOT NULL DEFAULT 'okx_cli_top_long_short',
    PRIMARY KEY (cycle_id, symbol, timeframe, source)
)
"""
INDEXES = (
    "CREATE INDEX idx_positioning_cycle "
    "ON market_positioning(cycle_id)",
    "CREATE INDEX idx_positioning_symbol_ts "
    "ON market_positioning(symbol, ts)",
)


def _table_info(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return list(connection.execute(f"PRAGMA table_info({TABLE})"))


def _columns(rows: list[sqlite3.Row]) -> tuple[str, ...]:
    return tuple(str(row["name"]) for row in rows)


def _primary_key(rows: list[sqlite3.Row]) -> tuple[str, ...]:
    keyed = [row for row in rows if int(row["pk"]) > 0]
    return tuple(
        str(row["name"])
        for row in sorted(keyed, key=lambda item: int(item["pk"]))
    )


def _target_key_duplicates(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM ("
        "SELECT cycle_id,symbol,timeframe,source "
        "FROM market_positioning "
        "GROUP BY cycle_id,symbol,timeframe,source HAVING COUNT(*) > 1"
        ")"
    ).fetchone()
    return int(row[0])


def _backup(
    source: sqlite3.Connection,
    db_path: Path,
    backup_dir: Path,
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"{db_path.name}.bak_positioning-key_{stamp}"
    destination = sqlite3.connect(str(target), timeout=30)
    try:
        source.backup(destination)
        check = destination.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        destination.close()
    if check != "ok":
        raise RuntimeError(f"backup quick_check={check}")
    return target


def _schema_state(connection: sqlite3.Connection) -> dict[str, Any]:
    exists = bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE,),
    ).fetchone())
    temp_exists = bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (TEMP_TABLE,),
    ).fetchone())
    if not exists:
        return {
            "state": "missing",
            "columns": (),
            "primary_key": (),
            "temporary_table_exists": temp_exists,
        }
    info = _table_info(connection)
    columns = _columns(info)
    primary_key = _primary_key(info)
    if columns != REQUIRED_COLUMNS:
        state = "column_mismatch"
    elif primary_key == TARGET_PRIMARY_KEY:
        state = "target"
    elif primary_key == LEGACY_PRIMARY_KEY:
        state = "legacy"
    else:
        state = "primary_key_mismatch"
    return {
        "state": state,
        "columns": columns,
        "primary_key": primary_key,
        "temporary_table_exists": temp_exists,
    }


def _validate_target(
    connection: sqlite3.Connection,
    expected_rows: int,
) -> dict[str, Any]:
    state = _schema_state(connection)
    actual_rows = int(connection.execute(
        f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
    indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (TABLE,),
        )
    }
    required_indexes = {
        "idx_positioning_cycle", "idx_positioning_symbol_ts",
    }
    quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    result = {
        **state,
        "expected_rows": expected_rows,
        "actual_rows": actual_rows,
        "row_count_preserved": actual_rows == expected_rows,
        "target_key_duplicates": _target_key_duplicates(connection),
        "required_indexes_present": required_indexes.issubset(indexes),
        "quick_check": quick_check,
    }
    result["passed"] = (
        state["state"] == "target"
        and not state["temporary_table_exists"]
        and result["row_count_preserved"]
        and result["target_key_duplicates"] == 0
        and result["required_indexes_present"]
        and quick_check == "ok"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=_public_project_path('db', 'market.db'))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir")
    args = parser.parse_args(argv)
    db_path = Path(args.db)
    if not db_path.is_file():
        print(json.dumps({"ok": False, "error": f"db missing: {db_path}"}))
        return 2

    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.execute("PRAGMA busy_timeout=20000")
    try:
        state = _schema_state(connection)
        if state["state"] == "target":
            rows = int(connection.execute(
                f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
            validation = _validate_target(connection, rows)
            if not validation["passed"]:
                print(json.dumps({
                    "ok": False,
                    "error": "target schema validation failed",
                    "validation": validation,
                }, ensure_ascii=False))
                return 2
            print(json.dumps({
                "ok": True,
                "db": str(db_path),
                "dry_run": not args.apply,
                "action": "none",
                "historical_backfill": False,
                "validation": validation,
            }, ensure_ascii=False))
            return 0
        if state["state"] != "legacy" or state["temporary_table_exists"]:
            print(json.dumps({
                "ok": False,
                "error": "market_positioning schema is not a clean legacy layout",
                "expected_columns": REQUIRED_COLUMNS,
                "legacy_primary_key": LEGACY_PRIMARY_KEY,
                "target_primary_key": TARGET_PRIMARY_KEY,
                "actual": state,
            }, ensure_ascii=False))
            return 2

        row_count = int(connection.execute(
            f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
        duplicate_keys = _target_key_duplicates(connection)
        precheck = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        plan = {
            "ok": duplicate_keys == 0 and precheck == "ok",
            "db": str(db_path),
            "dry_run": not args.apply,
            "action": "plan-only" if not args.apply else "apply",
            "legacy_primary_key": LEGACY_PRIMARY_KEY,
            "target_primary_key": TARGET_PRIMARY_KEY,
            "existing_rows": row_count,
            "target_key_duplicates": duplicate_keys,
            "pre_migration_quick_check": precheck,
            "historical_backfill": False,
            "historical_rows_reconstructed": 0,
        }
        if not plan["ok"]:
            print(json.dumps({
                **plan,
                "error": "pre-migration validation failed",
            }, ensure_ascii=False))
            return 2
        if not args.apply:
            print(json.dumps(plan, ensure_ascii=False))
            return 0
        if not args.backup_dir:
            print(json.dumps({
                **plan,
                "ok": False,
                "error": "--apply requires --backup-dir",
            }, ensure_ascii=False))
            return 2

        backup_path = _backup(connection, db_path, Path(args.backup_dir))
        connection.close()
        connection = sqlite3.connect(db_path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.row_factory = sqlite3.Row

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(TARGET_DDL)
        columns = ",".join(REQUIRED_COLUMNS)
        connection.execute(
            f"INSERT INTO {TEMP_TABLE} ({columns}) "
            f"SELECT {columns} FROM {TABLE}"
        )
        copied_rows = int(connection.execute(
            f"SELECT COUNT(*) FROM {TEMP_TABLE}").fetchone()[0])
        if copied_rows != row_count:
            raise RuntimeError(
                f"copy row count mismatch source={row_count} target={copied_rows}")
        connection.execute(f"DROP TABLE {TABLE}")
        connection.execute(f"ALTER TABLE {TEMP_TABLE} RENAME TO {TABLE}")
        for statement in INDEXES:
            connection.execute(statement)
        validation = _validate_target(connection, row_count)
        if not validation["passed"]:
            raise RuntimeError(
                "post-migration validation failed: "
                + json.dumps(validation, ensure_ascii=False)
            )
        connection.commit()
        print(json.dumps({
            **plan,
            "action": "applied",
            "backup": str(backup_path),
            "validation": validation,
        }, ensure_ascii=False))
        return 0
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        connection.rollback()
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False))
        return 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
