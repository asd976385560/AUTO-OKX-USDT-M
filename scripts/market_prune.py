# -*- coding: utf-8 -*-
"""market_prune.py — market.db 高频表保留裁剪（架构评审 · market.db pruning 设计，2026-07-07）。

market.db 1.06GB、tick_snapshots/derivatives 各 ~1.3M 行/52天且无 pruning，~20MB/天增长。
本模块裁掉超保留窗的旧 tick/derivatives 行——**kline_cache 有 2019 起历史深度（相似度回测用）
绝不裁**。

**单 writer 纪律（红线）**：market.db 唯一 writer = fast_collect。删行=写库=第二 writer 会
撞锁/坏 WAL。故：
  - `prune(con, ...)` 收 fast_collect **自己的写连接**，在其写事务内删 → 仍单 writer（设计接线）。
  - CLI（本文件直接跑）**只 dry-run**（mode=ro 只统计将删多少），**禁 --apply 独立删**（会成第二 writer）。

保留窗底线 = volume_anomaly 的 27 天基线（query_state.check_volume_anomaly 用 tick 27 天算异常）；
默认 45 天留足余量。归一比较用 _tsnorm.sql_norm 处理 ts 混格式（derivatives.ts 有 1.3M Z + 8795 CST）。

文件收缩：DELETE 释放的页被新插入复用 → 文件停增（稳态 ~0.87GB），但不自动缩。首次一次性
VACUUM（主人停 fast_collect → VACUUM → 拉起）可回收初值 ~150MB；日常 DELETE 停增即够，VACUUM 可选。

用法（只读设计验证）：
  market_prune.py --retention-days 45          # dry-run：列各表将删行数/占比
  market_prune.py --retention-days 30 --json
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
from pathlib import Path

sys.path.insert(0, _project_path('scripts'))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import _tsnorm  # noqa: E402

PRUNE_TABLES = ("tick_snapshots", "derivatives")
PROTECT_TABLES = ("kline_cache",)          # 历史深度，绝不裁
VOLUME_BASELINE_DAYS = 27                    # volume_anomaly 基线 = 保留窗硬底线
DEFAULT_RETENTION_DAYS = 45
# 增量回收每轮上限（页）。market.db page_size=4096 → 20000 页≈80MB。DELETE 只把页挂 freelist、
# 文件不缩；market.db auto_vacuum=INCREMENTAL(2)，故 prune 后跟一句 incremental_vacuum 即可把页
# 还给 OS——不停机、单 writer。分块（非一次性全清）避免首裁 ~200MB 积压把 WAL 冲成尖峰。
DEFAULT_RECLAIM_PAGES = 20000


def _cutoff_clause(days: int, col: str = "ts") -> str:
    """归一后 < CST now - days → 待删。用 _tsnorm.sql_norm 统一处理 Z/CST 混格式。"""
    return f"{_tsnorm.sql_norm(col)} < datetime('now','+8 hours','-{days} days')"


def prune(con: sqlite3.Connection, retention_days: int = DEFAULT_RETENTION_DAYS,
          tables=PRUNE_TABLES, apply: bool = False) -> dict:
    """裁剪超保留窗的旧行。con 须为**可写连接（apply=True 时）= fast_collect 的连接**（单 writer）。
    apply=False 仅统计。返回 {table: {deleted/would_delete, total}}。保护表拒删。"""
    if retention_days < VOLUME_BASELINE_DAYS:
        raise ValueError(f"retention_days={retention_days} < volume 基线 {VOLUME_BASELINE_DAYS}，"
                         f"会挖空 volume_anomaly 27 天基线，拒绝")
    out = {}
    for t in tables:
        if t in PROTECT_TABLES:
            out[t] = {"skipped": "protected"}
            continue
        where = _cutoff_clause(retention_days)
        try:
            total = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {where}").fetchone()[0]
        except sqlite3.OperationalError as e:
            out[t] = {"error": str(e)}
            continue
        rec = {"total": total, "match": n, "pct": round(100 * n / total, 1) if total else 0}
        if apply and n > 0:
            con.execute(f"DELETE FROM {t} WHERE {where}")
            rec["deleted"] = n
        else:
            rec["would_delete"] = n
        out[t] = rec
    if apply:
        con.commit()
    return out


def incremental_reclaim(con: sqlite3.Connection,
                        max_pages: int = DEFAULT_RECLAIM_PAGES) -> dict:
    """把 DELETE 释放的 freelist 页还给 OS（文件真缩）。**须 auto_vacuum=INCREMENTAL(2)**，
    否则 PRAGMA incremental_vacuum 无效——非 2 直接跳过（fail-safe，不报错不阻断）。
    con 须为**可写连接 = fast_collect 的连接**（单 writer）；须在 autocommit（prune 已 commit）下调。
    分块 max_pages 避免一次性回收把 WAL 冲成尖峰；max_pages=None 则清空整个 freelist。
    返回回收统计。绝不 raise（回收失败不该阻断采集，调用方仍 try 包裹兜底）。"""
    try:
        av = con.execute("PRAGMA auto_vacuum").fetchone()[0]
        if av != 2:
            return {"skipped": f"auto_vacuum={av}（非 INCREMENTAL，incremental_vacuum 无效）"}
        free_before = con.execute("PRAGMA freelist_count").fetchone()[0]
        if not free_before or free_before <= 0:
            return {"freelist_before": free_before or 0, "reclaimed_pages": 0}
        n = min(free_before, max_pages) if max_pages else free_before
        con.execute(f"PRAGMA incremental_vacuum({int(n)})")
        con.commit()
        free_after = con.execute("PRAGMA freelist_count").fetchone()[0]
        ps = con.execute("PRAGMA page_size").fetchone()[0] or 4096
        freed = free_before - free_after
        return {"freelist_before": free_before, "freelist_after": free_after,
                "reclaimed_pages": freed, "reclaimed_mb": round(freed * ps / 1024 / 1024, 1),
                "remaining_mb": round(free_after * ps / 1024 / 1024, 1)}
    except sqlite3.Error as e:
        return {"error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description="market.db 保留裁剪（CLI 只 dry-run，守单 writer）")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    # CLI 一律只读 mode=ro——绝不独立 --apply 删（那会成 market.db 第二 writer，违红线）。
    db = Path(args.db_root) / "market.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        stats = prune(con, args.retention_days, apply=False)
    finally:
        con.close()
    result = {"retention_days": args.retention_days, "mode": "dry-run (只读)",
              "note": "真 apply 只能由 fast_collect 内 prune(con,apply=True) 做（单 writer）",
              "tables": stats}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"=== market.db 保留裁剪 dry-run（保留 {args.retention_days} 天）===")
        for t, r in stats.items():
            if "would_delete" in r:
                print(f"  {t:16} 将删 {r['would_delete']:>9,} / {r['total']:,} 行 ({r['pct']}%)")
            else:
                print(f"  {t:16} {r}")
        print("  kline_cache      跳过（历史深度，绝不裁）")
        print("  ⚠️ CLI 只 dry-run；真裁由 fast_collect 内单 writer 调用 prune(con,apply=True)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
