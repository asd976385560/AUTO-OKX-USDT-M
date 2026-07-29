# -*- coding: utf-8 -*-
"""公开宏观数据 schema 迁移（幂等，支持 WAL 在线备份）。

regime.db:
  - macro_observations 标准化日频事实表；
  - cross_market 增加 dxy_calc_ecb / dxy_calc_ecb_d1 / fear_greed /
    fear_greed_label。ETF 复用既有 btc_etf_net_flow_usd。
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
sys.path.insert(0, _project_path('scripts'))

import ledger  # noqa: E402
from public_macro import TABLE_DDL  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
NEW_COLUMNS = {
    "dxy_calc_ecb": "REAL",
    "dxy_calc_ecb_d1": "REAL",
    "fear_greed": "REAL",
    "fear_greed_label": "TEXT",
}


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}


def online_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"backup integrity_check={integrity}")
    finally:
        dst.close()
        src.close()


def migrate(
    db_root: Path, *, dry_run: bool = False, backup_dir: Path | None = None
) -> dict:
    path = db_root / "regime.db"
    if not path.exists():
        return {"ok": False, "error": f"regime.db not found: {path}"}
    con = ledger.connect(path, readonly=dry_run)
    try:
        have = columns(con, "cross_market")
        missing = {name: typ for name, typ in NEW_COLUMNS.items() if name not in have}
        table_present = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='macro_observations'"
        ).fetchone() is not None
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "macro_observations_present": table_present,
                "columns_to_add": sorted(missing),
            }
    finally:
        con.close()

    backup = None
    if backup_dir is not None:
        stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f"regime-pre-public-macro-{stamp}.db"
        online_backup(path, backup)

    con = ledger.connect(path)
    try:
        con.executescript(TABLE_DDL)
        added = []
        have = columns(con, "cross_market")
        for name, typ in NEW_COLUMNS.items():
            if name not in have:
                con.execute(f"ALTER TABLE cross_market ADD COLUMN {name} {typ}")
                added.append(name)
        con.commit()
    finally:
        con.close()
    result = {
        "ok": True,
        "table": "macro_observations",
        "columns_added": added,
    }
    if backup is not None:
        result["backup"] = str(backup)
    return result


def verify(db_root: Path) -> dict:
    con = ledger.connect(db_root / "regime.db", readonly=True)
    try:
        tables = {
            row["name"]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing_columns = sorted(NEW_COLUMNS.keys() - columns(con, "cross_market"))
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        return {
            "ok": (
                "macro_observations" in tables
                and not missing_columns
                and integrity == "ok"
            ),
            "macro_observations_present": "macro_observations" in tables,
            "missing_columns": missing_columns,
            "integrity_check": integrity,
        }
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="公开宏观数据 schema 迁移")
    parser.add_argument("--db-root", default=_project_path('db'))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--backup-dir")
    args = parser.parse_args()
    root = Path(args.db_root)
    result = migrate(
        root,
        dry_run=args.dry_run,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        return 1
    if args.verify and not args.dry_run:
        checked = verify(root)
        print(json.dumps({"verify": checked}, ensure_ascii=False, indent=2))
        return 0 if checked["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
