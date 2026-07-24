# -*- coding: utf-8 -*-
"""Add the decision-card payload to analysis_signals, idempotently.

The legacy score columns stay in place for historical readers and rollback
compatibility.  New decision_card_v1 receipts store their structured evidence
in ``decision_card`` and may leave the legacy score columns NULL.
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def _online_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(target, timeout=10)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def apply(db_path: Path, backup_dir: Path | None) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    backup_path = None
    if backup_dir is not None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"{db_path.stem}-pre-decision-card-{stamp}.db"
        _online_backup(db_path, backup_path)

    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        cols = {row[1] for row in con.execute("PRAGMA table_info(analysis_signals)")}
        changed = False
        if "decision_card" not in cols:
            con.execute("ALTER TABLE analysis_signals ADD COLUMN decision_card TEXT")
            changed = True
        con.commit()
        after = {row[1] for row in con.execute("PRAGMA table_info(analysis_signals)")}
        if "decision_card" not in after:
            raise RuntimeError("decision_card column verification failed")
    finally:
        con.close()

    return {
        "ok": True,
        "db": str(db_path),
        "changed": changed,
        "decision_card": "present",
        "backup": str(backup_path) if backup_path else None,
    }


def apply_lessons(db_path: Path, backup_dir: Path | None) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    backup_path = None
    if backup_dir is not None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"{db_path.stem}-pre-decision-card-{stamp}.db"
        _online_backup(db_path, backup_path)
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        cols = {row[1] for row in con.execute("PRAGMA table_info(missed_opportunities)")}
        changed = False
        if "decision_card" not in cols:
            con.execute("ALTER TABLE missed_opportunities ADD COLUMN decision_card TEXT")
            changed = True
        con.commit()
        after = {row[1] for row in con.execute("PRAGMA table_info(missed_opportunities)")}
        if "decision_card" not in after:
            raise RuntimeError("missed_opportunities.decision_card column verification failed")
    finally:
        con.close()
    return {
        "ok": True,
        "db": str(db_path),
        "changed": changed,
        "decision_card": "present",
        "backup": str(backup_path) if backup_path else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=_project_path('db', 'analysis.db'))
    ap.add_argument("--backup-dir", default=None)
    ap.add_argument("--lessons-db", default=None)
    args = ap.parse_args()
    result = apply(
        Path(args.db),
        Path(args.backup_dir) if args.backup_dir else None,
    )
    lessons_result = (
        apply_lessons(
            Path(args.lessons_db),
            Path(args.backup_dir) if args.backup_dir else None,
        )
        if args.lessons_db
        else None
    )
    print(json.dumps(
        {"ok": True, "analysis": result, "lessons": lessons_result},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
