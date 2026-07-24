# -*- coding: utf-8 -*-
"""ts_audit.py — 全库时间戳格式巡检哨兵（架构评审 #6，2026-07-07，只读）。

扫所有 db/*.db 的时间戳列，统计每列 UTC-Z（`...Z`）vs CST 裸串（空格）vs 其它的分布，
检出「UTC-Z 残留」——即需要归一迁移的表/列。给「入库即 UTC+8 归一」迁移定口径 +
作常设哨兵（挂 monitor/reviewer 可选）：若本应全 CST 的表冒出 Z 行 = 有写方漏归一。

只读（mode=ro），不改任何库。
用法：ts_audit.py [--db-root <PROJECT_ROOT>\\db] [--json]
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 时间戳列名启发式：命中即候选（再按取样值确认是时间戳）
_TS_NAME = re.compile(r"(^|_)(ts|time|at|utc)$|_(ts|at|utc)$|time$", re.IGNORECASE)
_TS_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")


def _ro(p: Path):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=8)


def _ts_columns(con, table: str) -> list[str]:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    cand = [c for c in cols if _TS_NAME.search(c) or c.lower() in
            ("ts", "dispatched_at", "ingested_at", "event_time", "reset_ts",
             "close_ts", "open_ts", "week_start_ts", "month_start_ts")]
    confirmed = []
    for c in cand:
        try:
            v = con.execute(
                f"SELECT {c} FROM {table} WHERE {c} IS NOT NULL LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            continue
        if v and v[0] and _TS_VALUE.match(str(v[0])):
            confirmed.append(c)
    return confirmed


def audit_db(path: Path) -> list[dict]:
    out = []
    try:
        con = _ro(path)
    except sqlite3.OperationalError:
        return out
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            for c in _ts_columns(con, t):
                try:
                    total = con.execute(
                        f"SELECT COUNT(*) FROM {t} WHERE {c} IS NOT NULL").fetchone()[0]
                    z = con.execute(
                        f"SELECT COUNT(*) FROM {t} WHERE {c} LIKE '%Z'").fetchone()[0]
                    space = con.execute(
                        f"SELECT COUNT(*) FROM {t} WHERE {c} LIKE '% %'").fetchone()[0]
                except sqlite3.OperationalError:
                    continue
                if total == 0:
                    continue
                other = total - z - space
                out.append({
                    "db": path.name, "table": t, "column": c, "total": total,
                    "utc_z": z, "cst_space": space, "other": other,
                    "fmt": ("UTC-Z" if z == total else "CST" if space == total
                            else "MIXED" if z and space else "OTHER"),
                })
    finally:
        con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="全库时间戳格式巡检（只读）")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = Path(args.db_root)
    rows = []
    for db in sorted(root.glob("*.db")):
        rows.extend(audit_db(db))

    mixed = [r for r in rows if r["fmt"] == "MIXED"]
    utc_z = [r for r in rows if r["fmt"] == "UTC-Z"]

    if args.json:
        print(json.dumps({"columns": rows, "mixed": mixed, "utc_z_tables": utc_z},
                         ensure_ascii=False, indent=1))
        return 1 if mixed else 0

    print("=== 全库时间戳格式巡检 ===")
    print(f"{'db':16} {'table':20} {'column':14} {'fmt':6} {'total':>8} {'Z':>7} {'CST':>7}")
    print("-" * 90)
    for r in rows:
        print(f"{r['db']:16} {r['table']:20} {r['column']:14} {r['fmt']:6} "
              f"{r['total']:>8} {r['utc_z']:>7} {r['cst_space']:>7}")
    print("-" * 90)
    print(f"UTC-Z 表（迁移目标）: {len(utc_z)} 列 | MIXED（写方漏归一，最危险）: {len(mixed)} 列")
    for r in mixed:
        print(f"  ⚠️ MIXED {r['db']}.{r['table']}.{r['column']}: Z={r['utc_z']} / CST={r['cst_space']}")
    # MIXED = 同列两格式并存 = 有写方漏归一，返回非 0 供哨兵告警
    return 1 if mixed else 0


if __name__ == "__main__":
    raise SystemExit(main())
