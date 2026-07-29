# -*- coding: utf-8 -*-
"""Create a consistent, verified backup set before reconciliation repairs.

SQLite databases are copied with the online backup API so active WAL content is
included.  Default is dry-run; ``--apply`` creates a new, otherwise-empty
directory and writes a manifest after every source and backup passes
``PRAGMA quick_check``.
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
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


DEFAULT_DATABASES = ("live_trades.db", "account.db", "ledger.db")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quick_check(path: Path, readonly: bool) -> str:
    if readonly:
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    else:
        con = sqlite3.connect(str(path), timeout=15)
    try:
        return str(con.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        con.close()


def _online_backup(source: Path, target: Path) -> dict:
    source_check = _quick_check(source, readonly=True)
    if source_check != "ok":
        raise RuntimeError(f"source quick_check failed: {source} -> {source_check}")
    src = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro", uri=True, timeout=15)
    dst = sqlite3.connect(str(target), timeout=15)
    try:
        src.execute("PRAGMA busy_timeout=5000")
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    backup_check = _quick_check(target, readonly=True)
    if backup_check != "ok":
        raise RuntimeError(f"backup quick_check failed: {target} -> {backup_check}")
    return {
        "source": str(source),
        "backup": str(target),
        "source_quick_check": source_check,
        "backup_quick_check": backup_check,
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--database", action="append",
                    help="db-root 下的文件名；可重复，默认备份 live/account/ledger")
    ap.add_argument("--copy-file", action="append", default=[],
                    help="额外复制的普通文件；可重复")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db_root = Path(args.db_root)
    output = Path(args.output_dir)
    db_names = list(args.database or DEFAULT_DATABASES)
    db_sources = [db_root / name for name in db_names]
    plain_sources = [Path(value) for value in args.copy_file]
    missing = [str(path) for path in db_sources + plain_sources
               if not path.is_file()]
    plan = {
        "ok": not missing,
        "apply": bool(args.apply),
        "output_dir": str(output),
        "databases": [str(path) for path in db_sources],
        "files": [str(path) for path in plain_sources],
        "missing": missing,
    }
    if missing:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 2
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if output.exists() and any(output.iterdir()):
        plan.update({"ok": False, "error": "output-dir 已存在且非空，拒绝覆盖"})
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 2
    output.mkdir(parents=True, exist_ok=True)

    manifest = {
        "ok": False,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "output_dir": str(output),
        "databases": [],
        "files": [],
    }
    for source in db_sources:
        manifest["databases"].append(
            _online_backup(source, output / source.name))
    for source in plain_sources:
        target = output / source.name
        shutil.copy2(source, target)
        manifest["files"].append({
            "source": str(source),
            "backup": str(target),
            "bytes": target.stat().st_size,
            "sha256": _sha256(target),
        })
    manifest["ok"] = True
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
