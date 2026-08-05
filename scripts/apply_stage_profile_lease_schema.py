# -*- coding: utf-8 -*-
"""Add the cross-cycle live/demo profile lease table to ``ledger.db``.

The default mode is a read-only inspection. Applying the migration requires
both ``--apply`` and ``--backup-dir``; a verified SQLite online backup is
completed before the target is opened for writing.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


_PROJECT_ROOT = Path(
    os.environ.get("OKX_ROOT") or Path(__file__).resolve().parents[1]
).resolve()
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from migration_guard import (  # noqa: E402
    add_migration_arguments,
    backup_databases,
    resolve_apply,
)


TABLE = "stage_profile_leases"
EXPECTED_COLUMNS = ("profile", "cycle_id", "acquired_at", "expires_at")
EXPECTED_SCHEMA = (
    ("profile", "TEXT", 0, 1),
    ("cycle_id", "TEXT", 1, 0),
    ("acquired_at", "TEXT", 1, 0),
    ("expires_at", "TEXT", 1, 0),
)

DDL = """
CREATE TABLE IF NOT EXISTS stage_profile_leases (
    profile       TEXT PRIMARY KEY,
    cycle_id      TEXT NOT NULL,
    acquired_at   TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
"""


def _state(db_path: Path) -> dict[str, object]:
    """Read the current lease-table state without creating the database."""
    db_path = db_path.resolve()
    if not db_path.is_file():
        return {
            "ok": False,
            "db": str(db_path),
            "error": f"ledger.db does not exist: {db_path}",
        }

    con = sqlite3.connect(
        db_path.as_uri() + "?mode=ro", uri=True, timeout=30
    )
    try:
        con.execute("PRAGMA busy_timeout=30000")
        present = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone() is not None
        table_info = (
            list(con.execute(f"PRAGMA table_info({TABLE})"))
            if present else []
        )
        columns = [row[1] for row in table_info]
        schema = [
            (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in table_info
        ]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        schema_matches = tuple(schema) == EXPECTED_SCHEMA
        return {
            "ok": integrity == "ok" and (not present or schema_matches),
            "db": str(db_path),
            "table": TABLE,
            "present": present,
            "columns": columns,
            "schema": schema,
            "schema_matches": schema_matches,
            "integrity_check": integrity,
        }
    finally:
        con.close()


def inspect(db_path: Path) -> dict[str, object]:
    """Return a read-only dry-run report."""
    state = _state(db_path)
    return {
        **state,
        "dry_run": True,
        "applied": False,
        "expected_columns": list(EXPECTED_COLUMNS),
        "expected_schema": list(EXPECTED_SCHEMA),
        "would_create": bool(state.get("ok") and not state.get("present")),
    }


def apply_schema(db_path: Path, backup_dir: Path) -> dict[str, object]:
    """Back up ``ledger.db`` and apply only the profile-lease DDL."""
    db_path = db_path.resolve()
    if not db_path.is_file():
        return {
            "ok": False,
            "applied": False,
            "db": str(db_path),
            "error": f"ledger.db does not exist: {db_path}",
        }

    try:
        backups = backup_databases(
            [db_path], backup_dir, "stage-profile-lease-schema"
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "applied": False,
            "db": str(db_path),
            "error": f"backup failed: {exc}",
        }

    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("BEGIN IMMEDIATE")
        present_before = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone() is not None
        con.execute(DDL)
        table_info = list(con.execute(f"PRAGMA table_info({TABLE})"))
        columns = [row[1] for row in table_info]
        schema = [
            (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in table_info
        ]
        if tuple(schema) != EXPECTED_SCHEMA:
            raise RuntimeError(
                f"unexpected {TABLE} schema: {schema}; "
                f"expected {list(EXPECTED_SCHEMA)}"
            )
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(
                f"target integrity_check failed for {db_path}: {integrity}"
            )
        con.commit()
    except Exception as exc:  # noqa: BLE001
        con.rollback()
        return {
            "ok": False,
            "applied": False,
            "db": str(db_path),
            "backup": str(backups[db_path]),
            "error": str(exc),
        }
    finally:
        con.close()

    return {
        "ok": True,
        "applied": True,
        "changed": not present_before,
        "db": str(db_path),
        "backup": str(backups[db_path]),
        "table": TABLE,
        "columns": columns,
        "schema": schema,
        "integrity_check": integrity,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely add ledger.db.stage_profile_leases"
    )
    parser.add_argument(
        "--db-root",
        default=str(_PROJECT_ROOT / "db"),
        help="directory containing ledger.db (default: <PROJECT_ROOT>/db)",
    )
    add_migration_arguments(parser)
    args = parser.parse_args()
    apply_changes = resolve_apply(parser, args)

    db_path = Path(args.db_root) / "ledger.db"
    result = (
        apply_schema(db_path, Path(args.backup_dir))
        if apply_changes
        else inspect(db_path)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
