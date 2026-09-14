# -*- coding: utf-8 -*-
r"""apply_cross_market_staleness_schema.py — 宏观观测日类型化列（2026-08-19 D4）。

为 regime.db.cross_market 增加 dxy_as_of / vix_as_of / spx_as_of /
gold_as_of / btc_etf_as_of（均 TEXT 可空，值为 'YYYY-MM-DD' 观测日）。

背景：观测日此前只活在 ``source_meta`` 这个 JSON 串里，消费方必须解析才能
判断陈旧；而 ``vix``/``spx`` 的 source_meta **连观测日都没有**（只有
``{"source":"fred"}``）。实测 ``btc_etf_net_flow_usd`` 的 source_as_of 卡在
2026-08-03（15 天前）、值恒 170100000.0，``carried_forward`` 却是空数组。

**不加 ``*_stale`` 布尔列**：陈旧度是「读取时刻 − 观测日」的函数，落库会瞬间
过期。阈值判定统一放消费侧纯函数（可单测、改阈值不动库）。

安全性：cross_market 含 VIRTUAL 生成列 ``btc_mcap_chg_24h_usd``；SQLite 允许
对含 VIRTUAL 生成列的表 ADD COLUMN（新列须可空、非 PK/UNIQUE），且不重写行。
但所有 ``SELECT *`` + 位置索引的读方会位移 —— 已核对 collect_slow /
decision_briefing 均用显式列名；``_regime_read.latest_cross_market`` 请在
生产机复核一次。

行为：默认 dry-run；``--apply`` 才 ALTER。幂等。不回填历史（历史 NULL 如实
表示「当时未记观测日」）。迁移后必须跑 ``export_schema.py`` + ``check_doc_versions.py``。
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

STALENESS_COLUMNS = {
    "dxy_as_of": "TEXT",       # FRED DTWEXBGS 观测日
    "vix_as_of": "TEXT",       # FRED VIXCLS
    "spx_as_of": "TEXT",       # FRED SP500
    "gold_as_of": "TEXT",      # XAUT 采样时刻（=本行 ts）
    "btc_etf_as_of": "TEXT",   # ETF 净流的官方观测日
}


def plan_migration(con: sqlite3.Connection) -> dict:
    has_table = bool(con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='cross_market'").fetchone())
    if not has_table:
        return {"ok": False, "error": "cross_market 表不存在"}
    existing = {str(r[1]) for r in con.execute(
        "PRAGMA table_info(cross_market)")}
    missing = [c for c in STALENESS_COLUMNS if c not in existing]
    return {"ok": True, "missing": missing, "already_complete": not missing}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="cross_market 观测日类型化列迁移（默认 dry-run）")
    ap.add_argument("--db", default=_public_project_path('db', 'regime.db'))
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
            con.execute(
                f"ALTER TABLE cross_market ADD COLUMN {col} "
                f"{STALENESS_COLUMNS[col]}")
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
