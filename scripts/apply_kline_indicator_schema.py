# -*- coding: utf-8 -*-
r"""apply_kline_indicator_schema.py — kline_cache 扩展指标列迁移（2026-08-13）。

规格书「技术指标」维度补全：为 market.db.kline_cache 增加布林带与 OBV 列
（boll20_mid / boll20_up / boll20_dn / obv，均 REAL 可空）。列清单唯一事实源 =
`scripts/_kline_indicators.py::EXTENDED_COLUMNS`，本脚本只做幂等 ALTER。

行为：
  - 默认 dry-run 只输出计划；--apply 才执行 ALTER TABLE ADD COLUMN。
  - 幂等：已存在的列跳过；重复执行安全。
  - 不回填历史行（历史 NULL 如实表示"当时未计算"，禁止伪造指标）；
    新列由 collect_data.py / collect_slow.py 的 migration-aware 写入自然填充。
  - 迁移后必须按 README「文件联动」跑 export_schema.py 重生成 db/schema.sql
    （schema.sql 禁手编）+ check_doc_versions.py。

扩展列不进入多周期 OPEN 证据契约与既有 99% 预注册审计口径。零模型名。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _kline_indicators import EXTENDED_COLUMNS  # noqa: E402
from migration_guard import (  # noqa: E402
    add_migration_arguments,
    backup_databases,
    resolve_apply,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def plan_migration(con: sqlite3.Connection) -> dict:
    has_table = bool(con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='kline_cache'").fetchone())
    if not has_table:
        return {"ok": False, "error": "kline_cache 表不存在"}
    existing = {
        str(r[1]) for r in con.execute("PRAGMA table_info(kline_cache)")
    }
    missing = [c for c in EXTENDED_COLUMNS if c not in existing]
    return {
        "ok": True,
        "existing_extended": [c for c in EXTENDED_COLUMNS if c in existing],
        "missing": missing,
        "already_complete": not missing,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="kline_cache BOLL/OBV 扩展列迁移（默认 dry-run）")
    ap.add_argument("--db", default=r".\db\market.db")
    add_migration_arguments(ap)
    args = ap.parse_args()
    apply = resolve_apply(ap, args)
    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"库不存在: {db_path}"},
                         ensure_ascii=False))
        return 2

    read_con = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=15)
    read_con.execute("PRAGMA busy_timeout=10000")
    try:
        plan = plan_migration(read_con)
    finally:
        read_con.close()
    if not plan.get("ok"):
        print(json.dumps({**plan, "db": str(db_path)}, ensure_ascii=False))
        return 2
    report = {
        "db": str(db_path),
        "dry_run": not apply,
        **plan,
    }
    if not apply or plan["already_complete"]:
        report["action"] = (
            "noop-already-complete" if plan["already_complete"]
            else "plan-only")
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    backups = backup_databases(
        [db_path], Path(args.backup_dir), "kline-indicator-schema")
    con = sqlite3.connect(str(db_path), timeout=15)
    con.execute("PRAGMA busy_timeout=10000")
    try:
        current = plan_migration(con)
        if not current.get("ok"):
            print(json.dumps({**current, "db": str(db_path)}, ensure_ascii=False))
            return 2
        for col in current["missing"]:
            con.execute(f"ALTER TABLE kline_cache ADD COLUMN {col} REAL")
        con.commit()
        after = plan_migration(con)
        report.update({
            "action": "applied",
            "backup": str(backups[db_path.resolve()]),
            "added": current["missing"],
            "post_missing": after.get("missing"),
        })
        ok = not after.get("missing")
        report["ok"] = ok
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if ok else 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
