# -*- coding: utf-8 -*-
"""V2.0 §6 迁移 —— news_items 加列（新闻边缘 schema 升级）。

加 4 列（幂等：只补缺的）：
  - ingested_at TEXT —— 采集落库时刻（UTC+8），与 event_time 分离（修「源时间缺就 now」伪新鲜）
  - event_time  TEXT —— 原始事件/发布时刻（源给的；缺则 NULL，**禁** fallback now）
  - severity    TEXT —— 规则化事件严重度 critical|high|medium|low（独立于 A/B/C level；
                        **不**扩 level 的 CHECK——迁移麻烦）
  - tags        TEXT —— JSON 规则标签（regulatory|listing|hack|macro|whale…）

多 symbol 复用既有 `news_events_index`（不在此动）。dedup 保留 hash 唯一索引。
索引：(severity, ts)、(event_time)。

用法：
  python apply_news_edge_schema.py --db-root ./db [--dry-run]
  python apply_news_edge_schema.py --db-root ./db --apply \
      --backup-dir <BACKUP_DIR> [--verify]
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
import sys
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
sys.path.insert(0, _project_path('scripts'))
import ledger  # noqa: E402
from migration_guard import (  # noqa: E402
    add_migration_arguments,
    backup_databases,
    resolve_apply,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

NEW_COLS = {
    "ingested_at": "TEXT",
    "event_time": "TEXT",
    "severity": "TEXT",
    "tags": "TEXT",
}
INDEXES = [
    ("idx_news_severity_ts", "news_items(severity, ts)"),
    ("idx_news_event_time", "news_items(event_time)"),
]


def existing_cols(con, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def migrate(
    db_root: Path,
    dry_run: bool = True,
    backup_dir: Path | None = None,
) -> dict:
    news = db_root / "news.db"
    if not news.exists():
        return {"ok": False, "error": f"news.db 不存在: {news}"}
    if not dry_run and backup_dir is None:
        raise ValueError("backup_dir is required when dry_run=False")
    backups = (
        backup_databases([news], backup_dir, "news-edge-schema")
        if not dry_run
        else {}
    )
    con = ledger.connect(news, readonly=dry_run)
    try:
        have = existing_cols(con, "news_items")
        to_add = {c: t for c, t in NEW_COLS.items() if c not in have}
        if dry_run:
            return {"ok": True, "dry_run": True, "existing": sorted(have),
                    "to_add": sorted(to_add)}
        for col, typ in to_add.items():
            con.execute(f"ALTER TABLE news_items ADD COLUMN {col} {typ}")
        for name, target in INDEXES:
            con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
        con.commit()
        return {
            "ok": True,
            "added": sorted(to_add),
            "already_present": sorted(have & set(NEW_COLS)),
            "backups": [str(path) for path in backups.values()],
        }
    finally:
        con.close()


def verify(db_root: Path) -> dict:
    con = ledger.connect(db_root / "news.db", readonly=True)
    try:
        have = existing_cols(con, "news_items")
        missing = sorted(set(NEW_COLS) - have)
        return {"ok": not missing, "missing": missing, "cols": sorted(have)}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 news_items 加列迁移")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--verify", action="store_true")
    add_migration_arguments(ap)
    args = ap.parse_args()
    apply_changes = resolve_apply(ap, args)
    root = Path(args.db_root)

    res = migrate(
        root,
        dry_run=not apply_changes,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        return 1
    if args.verify and apply_changes:
        v = verify(root)
        print("-- verify --")
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0 if v["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
