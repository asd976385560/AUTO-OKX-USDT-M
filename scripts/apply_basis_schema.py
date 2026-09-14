# -*- coding: utf-8 -*-
r"""apply_basis_schema.py — derivatives 基差列迁移（2026-08-19 D2）。

为 market.db.derivatives 增加 mark_px / index_px / basis_bp（均 REAL 可空）。
basis_bp = (mark-index)/index*1e4，由采集端算好落库 —— 不用生成列：index_px
可能为 0/NULL，生成列除零会在 SELECT 期抛错，污染所有读方。

背景：``scripts/_okx_http.py`` 里的
``fetch_mark_price_candles_history_batch_sync`` / ``fetch_index_candles_...``
全仓零调用方（造好了没接线）。但本批**不接那两个历史函数**——它们是
per-instId 分页，~400 币 × 0.11s 端点地板会直接吃掉 15m 采集预算；基差要的
是即时读数。改用 ``/api/v5/public/mark-price?instType=SWAP`` 与
``/api/v5/market/index-tickers?quoteCcy=USDT`` 两次单批全量。

行为：默认 dry-run；``--apply`` 才 ALTER。幂等：已存在列跳过。**不回填历史**
（历史 NULL 如实表示「当时未采」，禁伪造）。迁移后必须跑 ``export_schema.py``
重生成 ``db/schema.sql``（schema.sql 禁手编）+ ``check_doc_versions.py``。
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
from pathlib import Path

BASIS_COLUMNS = ("mark_px", "index_px", "basis_bp")


def plan_migration(con: sqlite3.Connection) -> dict:
    has_table = bool(con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='derivatives'").fetchone())
    if not has_table:
        return {"ok": False, "error": "derivatives 表不存在"}
    existing = {str(r[1]) for r in con.execute(
        "PRAGMA table_info(derivatives)")}
    missing = [c for c in BASIS_COLUMNS if c not in existing]
    return {"ok": True, "missing": missing, "already_complete": not missing}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="derivatives 基差列迁移（默认 dry-run）")
    ap.add_argument("--db", default=_public_project_path('db', 'market.db'))
    from migration_guard import add_migration_arguments, resolve_apply, backup_databases
    add_migration_arguments(ap)
    args = ap.parse_args()
    apply = resolve_apply(ap, args)
    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"库不存在: {db_path}"},
                         ensure_ascii=False))
        return 2
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    con.execute("PRAGMA busy_timeout=10000")
    try:
        plan = plan_migration(con)
        if not plan.get("ok"):
            print(json.dumps({**plan, "db": str(db_path)}, ensure_ascii=False))
            return 2
        report = {"db": str(db_path), "dry_run": not args.apply, **plan}
        if not args.apply or plan["already_complete"]:
            report["action"] = ("noop-already-complete"
                                if plan["already_complete"] else "plan-only")
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 0
        con.close()
        backup_databases([db_path], Path(args.backup_dir), Path(__file__).stem)
        con = sqlite3.connect(db_path, timeout=15)
        con.execute("PRAGMA busy_timeout=10000")
        for col in plan["missing"]:
            con.execute(f"ALTER TABLE derivatives ADD COLUMN {col} REAL")
        con.commit()
        after = plan_migration(con)
        report.update({"action": "applied", "added": plan["missing"],
                       "post_missing": after.get("missing"),
                       "ok": not after.get("missing")})
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report["ok"] else 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
