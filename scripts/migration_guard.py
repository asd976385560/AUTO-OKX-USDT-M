# -*- coding: utf-8 -*-
"""Shared fail-closed CLI and backup guard for public migration scripts."""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def add_migration_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the common, explicit migration mode and backup arguments."""
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the migration (default: read-only dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly select the default read-only inspection mode",
    )
    parser.add_argument(
        "--backup-dir",
        help="required with --apply; receives verified SQLite online backups",
    )


def resolve_apply(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> bool:
    """Validate the fail-closed CLI contract before any database is opened."""
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir")
    return bool(args.apply)


def online_backup(source: Path, target: Path) -> None:
    """Create and integrity-check a WAL-safe SQLite online backup."""
    source = source.resolve()
    target = target.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target, timeout=30)
    try:
        src.execute("PRAGMA busy_timeout=30000")
        dst.execute("PRAGMA busy_timeout=30000")
        src.backup(dst)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(
                f"backup integrity_check failed for {target}: {integrity}"
            )
    finally:
        dst.close()
        src.close()


def backup_databases(
    sources: Iterable[Path],
    backup_dir: Path,
    label: str,
) -> dict[Path, Path]:
    """Back up every unique source before callers open any source for writing."""
    unique: list[Path] = []
    seen: set[Path] = set()
    for raw_source in sources:
        source = Path(raw_source).resolve()
        if source not in seen:
            unique.append(source)
            seen.add(source)
    if not unique:
        raise ValueError("at least one database is required")
    missing = [str(path) for path in unique if not path.is_file()]
    if missing:
        raise FileNotFoundError("database not found: " + ", ".join(missing))

    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    root = Path(backup_dir).resolve()
    backups: dict[Path, Path] = {}
    for source in unique:
        target = root / f"{source.stem}-pre-{safe_label}-{stamp}.db"
        online_backup(source, target)
        backups[source] = target
    return backups
