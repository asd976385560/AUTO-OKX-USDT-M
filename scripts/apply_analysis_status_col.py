# -*- coding: utf-8 -*-
r"""V2.0 幂等迁移：为 analysis_runs 增加 status 列。

analyst_writer.py already emits `status` (OPTIONAL_RUN_COLS includes it), but the
analysis_runs table lacked the column, so gate=skipped/stale/error could not be
persisted (downstream had to infer from regime IS NULL). This adds:

    analysis_runs.status TEXT DEFAULT 'ok'

Safe & idempotent: ADD COLUMN is non-destructive (existing rows default to 'ok');
re-running is a no-op once the column exists.

用法:
    python apply_analysis_status_col.py --root <PROJECT_ROOT>\db
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import sys
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
import ledger  # noqa: E402  复用 connect()（WAL + busy_timeout 单一来源）

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def has_col(con, table: str, col: str) -> bool:
    return any(r["name"] == col for r in con.execute(f"PRAGMA table_info({table})").fetchall())


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 幂等迁移：analysis_runs 加 status 列")
    ap.add_argument("--root", default=_project_path('db'))
    args = ap.parse_args()

    db = Path(args.root) / "analysis.db"
    if not db.exists():
        print(f"[SKIP] {db} 不存在（新装走 init_v20_dbs.py）")
        return 0

    con = ledger.connect(db)
    try:
        if not any(r["name"] == "analysis_runs"
                   for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()):
            print("[SKIP] analysis_runs 表不存在（先跑 init_v20_dbs.py）")
            return 0
        if has_col(con, "analysis_runs", "status"):
            print("[OK] analysis_runs.status 已存在 —— 无操作（幂等）")
        else:
            con.execute("ALTER TABLE analysis_runs ADD COLUMN status TEXT DEFAULT 'ok'")
            con.commit()
            print("[APPLIED] 已加 analysis_runs.status TEXT DEFAULT 'ok'")
        cols = [r["name"] for r in con.execute("PRAGMA table_info(analysis_runs)").fetchall()]
        print("analysis_runs 列:", cols)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
