# -*- coding: utf-8 -*-
"""Add the decision-card payload to analysis_signals, idempotently.

The legacy score columns stay in place for historical readers and rollback
compatibility.  New decision_card_v1 receipts store their structured evidence
in ``decision_card`` and may leave the legacy score columns NULL.

The CLI is read-only by default.  Mutation requires both ``--apply`` and
``--backup-dir``; all selected databases are backed up before the first write.
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
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, _project_path('scripts'))
from migration_guard import (  # noqa: E402
    add_migration_arguments,
    backup_databases,
    resolve_apply,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def inspect(db_path: Path, table: str) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    con = sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        table_present = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None
        cols = (
            {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            if table_present
            else set()
        )
        return {
            "ok": table_present,
            "dry_run": True,
            "db": str(db_path),
            "table": table,
            "table_present": table_present,
            "decision_card_present": "decision_card" in cols,
            "would_add": table_present and "decision_card" not in cols,
        }
    finally:
        con.close()


def _apply_column(db_path: Path, table: str, backup_path: Path) -> dict:
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        changed = False
        if "decision_card" not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN decision_card TEXT")
            changed = True
        con.commit()
        after = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if "decision_card" not in after:
            raise RuntimeError(f"{table}.decision_card column verification failed")
    finally:
        con.close()

    return {
        "ok": True,
        "db": str(db_path),
        "table": table,
        "changed": changed,
        "decision_card": "present",
        "backup": str(backup_path),
    }


def apply(db_path: Path, backup_dir: Path | None) -> dict:
    """Compatibility API with the same signature and a now-mandatory backup."""
    if backup_dir is None:
        raise ValueError("backup_dir is required")
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    backups = backup_databases([db_path], backup_dir, "decision-card-schema")
    return _apply_column(
        db_path, "analysis_signals", backups[db_path.resolve()])


def apply_lessons(db_path: Path, backup_dir: Path | None) -> dict:
    """Compatibility API for the optional lessons database."""
    if backup_dir is None:
        raise ValueError("backup_dir is required")
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    backups = backup_databases([db_path], backup_dir, "decision-card-schema")
    return _apply_column(
        db_path, "missed_opportunities", backups[db_path.resolve()])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=_project_path('db', 'analysis.db'))
    ap.add_argument("--lessons-db", default=None)
    add_migration_arguments(ap)
    args = ap.parse_args()
    apply_changes = resolve_apply(ap, args)
    analysis_db = Path(args.db)
    lessons_db = Path(args.lessons_db) if args.lessons_db else None

    if not apply_changes:
        result = inspect(analysis_db, "analysis_signals")
        lessons_result = (
            inspect(lessons_db, "missed_opportunities")
            if lessons_db
            else None
        )
    else:
        targets = [analysis_db] + ([lessons_db] if lessons_db else [])
        backups = backup_databases(
            targets, Path(args.backup_dir), "decision-card-schema")
        result = _apply_column(
            analysis_db,
            "analysis_signals",
            backups[analysis_db.resolve()],
        )
        lessons_result = (
            _apply_column(
                lessons_db,
                "missed_opportunities",
                backups[lessons_db.resolve()],
            )
            if lessons_db
            else None
        )
    print(json.dumps(
        {
            "ok": result["ok"] and (
                lessons_result is None or lessons_result["ok"]),
            "mode": "apply" if apply_changes else "dry-run",
            "analysis": result,
            "lessons": lessons_result,
        },
        ensure_ascii=False,
    ))
    return 0 if result["ok"] and (
        lessons_result is None or lessons_result["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
