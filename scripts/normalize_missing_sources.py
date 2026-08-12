# -*- coding: utf-8 -*-
r"""存量 `analysis_runs.missing_sources` 标签归一（2026-08-05·人工维护）。

背景：该字段由 Agent 自由文本写入，同一含义出现过两种拼写
（实测 2026-08-05 有 9 行 `dxy_zone_stale_carryforward`，而 06-27 以来一直用的
规范形是 `dxy_zone_stale_carry_forward`），按 key 聚合的统计会把同一件事算成两件。

写入侧已在 `collectors/analyst_writer.normalize_missing_sources` 归一（新数据不再分叉），
本脚本只处理**存量**。别名表复用 writer 的 `MISSING_SOURCE_ALIASES`——单一定义源，
禁止在此另写一份。

只改 `missing_sources` 一列，不动 raw / market_summary / signals，
不改 rowid / 主键 / ts。默认 dry-run，`--apply` 才写。

退出码：0=无需处理或已处理完；1=有待处理项（dry）；2=错误。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"./collectors")

from analyst_writer import (  # noqa: E402
    MISSING_SOURCE_ALIASES,
    normalize_missing_sources,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="存量 missing_sources 标签归一")
    ap.add_argument("--db", default=r"./db/analysis.db")
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[ERROR] 库不存在: {db}")
        return 2

    print(f"== missing_sources 归一 @ {db} ({'APPLY' if args.apply else 'DRY-RUN'}) ==")
    print(f"别名表（来自 analyst_writer，单一定义源）: "
          f"{json.dumps(MISSING_SOURCE_ALIASES, ensure_ascii=False)}\n")

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    todo = []
    for r in con.execute(
            "SELECT cycle_id, missing_sources FROM analysis_runs "
            "WHERE missing_sources IS NOT NULL AND missing_sources <> ''"):
        try:
            cur = json.loads(r["missing_sources"])
        except (json.JSONDecodeError, TypeError):
            continue
        new = normalize_missing_sources(cur)
        if new != cur:
            todo.append((r["cycle_id"], cur, new))
    con.close()

    if not todo:
        print("结论: 无需归一 ✓")
        return 0

    for cycle_id, old, new in todo:
        print(f"  {cycle_id}\n    旧: {json.dumps(old, ensure_ascii=False)}"
              f"\n    新: {json.dumps(new, ensure_ascii=False)}")
    print(f"\n待归一 {len(todo)} 行")

    if not args.apply:
        print("DRY-RUN：未写库（加 --apply 执行）")
        return 1

    wcon = sqlite3.connect(str(db), timeout=15)
    wcon.execute("PRAGMA busy_timeout=15000")
    try:
        with wcon:
            for cycle_id, _old, new in todo:
                wcon.execute(
                    "UPDATE analysis_runs SET missing_sources=? WHERE cycle_id=?",
                    (json.dumps(new, ensure_ascii=False), cycle_id))
    finally:
        wcon.close()
    print(f"已归一 {len(todo)} 行 ✓（复跑本脚本应报「无需归一」）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
