# -*- coding: utf-8 -*-
"""C3（2026-07-03）ts 时间格式幂等迁移：UTC-Z → CST-space。

背景：news_items.ts / news_events_index.ts / account_snapshots.ts /
position_snapshots.ts / trade_events.ts 混写 'YYYY-MM-DDTHH:MM:SSZ'（UTC）与
'YYYY-MM-DD HH:MM:SS'（UTC+8）两种格式——词典序 'T'(0x54)>' '(0x20) 反复造成
假 stale / 漏计数误诊。写方已切 CST（collect_data / jobb_live_account_check /
demo_account_check，2026-07-03 C3），本脚本把历史 Z 行一次性转成 CST（+8h）。

范围（仅下列 5 表；market.db tick/kline/derivatives 全 Z 表内自洽不动，
regime.db cross_market 不动）：
    news.db    : news_items.ts, news_events_index.ts
    account.db : account_snapshots.ts, position_snapshots.ts, trade_events.ts

幂等性：只匹配严格 'YYYY-MM-DDTHH:MM:SSZ' 格式的行（正则整串匹配）；转换后的行
不再匹配 → 可反复跑。非标准 Z 行（如带毫秒）不动、单独计数上报。

主键冲突（account_snapshots PK(ts,profile) / position_snapshots PK(ts,profile,symbol) /
news_events_index PK(symbol,ts,news_id)）：目标 CST 值已存在同键行时跳过该行并计数
（conflict——两行是同一时刻的重复快照，保留现状待人工裁决，不自动删数据）。

用法（默认 dry-run，只读打开、逐表计数不写）：
    pwsh -NoProfile -File <PROJECT_ROOT>\\scripts\\run_okx_python.ps1 ^
        <PROJECT_ROOT>\\scripts\\apply_ts_cst_migration.py --db-root <PROJECT_ROOT>\\db
    加 --apply 才真写（生产执行须主人/主循环拍板；跑前备份 db）。

退出码：0=成功（dry-run 或 apply 完成）；1=库不可达/执行错误。
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
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 严格整串匹配：秒级精度 + Z 结尾（写方 utc_now_iso/ms_to_iso/_parse_mx_date 的唯一产物）
Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# (db 文件名, 表名, ts 列名)
TARGETS = [
    ("news.db", "news_items", "ts"),
    ("news.db", "news_events_index", "ts"),
    ("account.db", "account_snapshots", "ts"),
    ("account.db", "position_snapshots", "ts"),
    ("account.db", "trade_events", "ts"),
]


def z_to_cst(z: str) -> str:
    """'2026-07-03T05:00:00Z' → '2026-07-03 13:00:00'（+8h）。调用方保证已匹配 Z_RE。"""
    dt = datetime.strptime(z, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=8)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def scan_table(con: sqlite3.Connection, table: str, col: str) -> dict:
    """扫描一张表：返回匹配行 [(rowid, ts)]、非标 Z 行计数、总行数。"""
    total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    z_like = con.execute(
        f"SELECT rowid, {col} FROM {table} WHERE {col} LIKE '%Z'"
    ).fetchall()
    matched = [(rid, ts) for rid, ts in z_like if ts and Z_RE.match(str(ts))]
    nonstandard = len(z_like) - len(matched)
    return {"total": total, "matched": matched, "nonstandard_z": nonstandard}


def migrate_table(con: sqlite3.Connection, table: str, col: str,
                  rows: list[tuple], apply: bool) -> dict:
    """逐行 UPDATE by rowid；IntegrityError（主键/唯一冲突）跳过计数。"""
    updated = conflicts = 0
    samples: list[dict] = []
    if apply:
        con.execute("BEGIN IMMEDIATE")
    for rid, old_ts in rows:
        new_ts = z_to_cst(str(old_ts))
        if len(samples) < 3:
            samples.append({"rowid": rid, "old": str(old_ts), "new": new_ts})
        if not apply:
            continue
        try:
            con.execute(
                f"UPDATE {table} SET {col}=? WHERE rowid=?", (new_ts, rid))
            updated += 1
        except sqlite3.IntegrityError:
            conflicts += 1
    if apply:
        con.commit()
    return {"updated": updated, "conflicts": conflicts, "samples": samples}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ts UTC-Z → CST 幂等迁移（news_items/news_events_index/"
                    "account_snapshots/position_snapshots/trade_events）")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--apply", action="store_true",
                    help="真写（默认 dry-run 只读计数）")
    args = ap.parse_args()

    db_root = Path(args.db_root)
    mode = "APPLY" if args.apply else "DRY-RUN"
    report = {"mode": mode, "db_root": str(db_root), "tables": {}, "ok": True}

    # 按 db 分组打开一次连接
    by_db: dict[str, list[tuple[str, str]]] = {}
    for db_name, table, col in TARGETS:
        by_db.setdefault(db_name, []).append((table, col))

    for db_name, tables in by_db.items():
        db_path = db_root / db_name
        if not db_path.exists():
            report["tables"][db_name] = {"error": f"db 不存在: {db_path}"}
            report["ok"] = False
            continue
        try:
            if args.apply:
                con = sqlite3.connect(str(db_path), timeout=15)
                con.execute("PRAGMA busy_timeout=15000")
            else:
                # dry-run 一律只读打开（生产库绝对禁写）
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
        except Exception as e:  # noqa: BLE001
            report["tables"][db_name] = {"error": f"打开失败: {e}"}
            report["ok"] = False
            continue
        try:
            for table, col in tables:
                key = f"{db_name}:{table}"
                if not table_exists(con, table):
                    report["tables"][key] = {"skipped": "表不存在"}
                    continue
                scan = scan_table(con, table, col)
                res = migrate_table(con, table, col, scan["matched"], args.apply)
                report["tables"][key] = {
                    "total_rows": scan["total"],
                    "z_matched": len(scan["matched"]),
                    "nonstandard_z_skipped": scan["nonstandard_z"],
                    "updated": res["updated"],
                    "conflicts_skipped": res["conflicts"],
                    "samples": res["samples"],
                }
        except Exception as e:  # noqa: BLE001
            report["tables"][db_name] = {"error": f"执行错误: {e}"}
            report["ok"] = False
        finally:
            con.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
