# -*- coding: utf-8 -*-
r"""apply_news_time_layers_schema.py — 新闻时间/来源分层迁移（Wave 0 序 4）。

背景（reports/quality/judgment_optimization_plan_20260810.md 缺陷 #1 / T1）：
news_items 只有采集时刻 ts/ingested_at 与媒体时间 event_time，briefing 的
"X 分钟前"按入库 ts 计算——8/7 的 Grayscale Form RW 经 8/10 转发被当成
"4 分钟前"的新催化（DOT 空单直接根因）。且同一贴文被 15 分钟轮重复采集，
每次都"刷新"新鲜度。

新增/维护 11 列（全部可空，ALTER ADD COLUMN，无重建）：
  event_occurred_at      事件真实发生日（标题显式日期提取；取不出=NULL）
  event_time_confidence  extracted_title | extracted_raw.* | published_fallback | unknown
  event_date_source      标题/正文具体提取来源；无日期=unknown
  published_at           媒体发布时间（=旧 event_time 语义，回填自 event_time）
  first_seen_at          事件簇首次被系统观察——仅观察年龄，不代表催化新鲜度
  last_seen_at           事件簇最近采集时刻（重复采集只推进它）
  cluster_id             事件簇 v1 键（sha256(url)[:16]；无 url 单行簇）
  source_grade           primary | aggregator | secondary（域名判定）
  primary_source_url     一级源链接（本行自身为 primary 时=url）
  event_key              保守事件键（一级文档 > 日期+标的+结构化标签 > URL）
  news_time_version      当前时间语义版本（v2）

回填全量行：cluster_id/source_grade/published_at/event_occurred_at 逐行算；
first_seen_at = event_key 内最早 COALESCE(ingested_at, ts)；last_seen_at = 本行自身
采集时刻。news_events_index 一次性预载，禁止对每条新闻做一次索引查询。语义提取与
分级逻辑 import 自 collectors/news_writer.py（写方是语义唯一所有者，迁移不复制实现）。

默认 dry-run；--apply 必配 --backup-dir（backup API + quick_check）。幂等：
列已存在时跳过加列；v2 会重算所有旧版本行，修正旧的年份回滚和首见口径。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_COLLECTORS = str(Path(__file__).resolve().parent.parent / "collectors")
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)
import news_writer  # noqa: E402  语义唯一所有者

NEW_COLUMNS = (
    ("event_occurred_at", "TEXT"),
    ("event_time_confidence", "TEXT"),
    ("published_at", "TEXT"),
    ("first_seen_at", "TEXT"),
    ("last_seen_at", "TEXT"),
    ("cluster_id", "TEXT"),
    ("source_grade", "TEXT"),
    ("primary_source_url", "TEXT"),
    ("event_date_source", "TEXT"),
    ("event_key", "TEXT"),
    ("news_time_version", "INTEGER"),
)


def columns(con: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in con.execute("PRAGMA table_info(news_items)")}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="news_items 时间/来源分层迁移（默认 dry-run）")
    ap.add_argument("--db", default=r"./db/news.db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backup-dir", default=None)
    args = ap.parse_args()
    if args.apply and args.dry_run:
        ap.error("--apply and --dry-run are mutually exclusive")
    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"库不存在: {db_path}"}))
        return 2

    con = sqlite3.connect(str(db_path), timeout=20)
    con.execute("PRAGMA busy_timeout=15000")
    con.row_factory = sqlite3.Row
    try:
        have = columns(con)
        missing_cols = [c for c, _ in NEW_COLUMNS if c not in have]
        total = con.execute("SELECT COUNT(*) FROM news_items").fetchone()[0]
        unfilled = (
            con.execute(
                "SELECT COUNT(*) FROM news_items "
                "WHERE COALESCE(news_time_version,0)<>2"
            ).fetchone()[0]
            if "news_time_version" in have else total
        )
        first_seen_mismatches = 0
        if {"event_key", "first_seen_at"}.issubset(have):
            first_seen_mismatches = int(con.execute(
                "WITH firsts AS ("
                " SELECT event_key, MIN(COALESCE(ingested_at,ts)) AS expected"
                " FROM news_items WHERE event_key IS NOT NULL GROUP BY event_key"
                ") SELECT COUNT(*) FROM news_items n JOIN firsts f"
                " ON f.event_key=n.event_key"
                " WHERE COALESCE(n.first_seen_at,'')<>COALESCE(f.expected,'')"
            ).fetchone()[0])
        report = {
            "db": str(db_path), "dry_run": not args.apply,
            "rows": total, "missing_columns": missing_cols,
            "rows_to_backfill": unfilled,
            "first_seen_rows_to_normalize": first_seen_mismatches,
        }
        if not missing_cols and unfilled == 0 and first_seen_mismatches == 0:
            print(json.dumps({**report, "ok": True, "action": "none"},
                             ensure_ascii=False, indent=1))
            return 0
        if not args.apply:
            print(json.dumps({**report, "ok": True, "action": "plan-only"},
                             ensure_ascii=False, indent=1))
            return 0
        if not args.backup_dir:
            print(json.dumps({**report, "ok": False,
                              "error": "--apply 必须配 --backup-dir"},
                             ensure_ascii=False, indent=1))
            return 2

        bdir = Path(args.backup_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_path = bdir / f"news.db.bak_time-layers_{tag}"
        bak = sqlite3.connect(str(bak_path))
        with bak:
            con.backup(bak)
        qc = bak.execute("PRAGMA quick_check").fetchone()[0]
        bak.close()
        if qc != "ok":
            print(json.dumps({**report, "ok": False,
                              "error": f"备份 quick_check={qc}，中止"},
                             ensure_ascii=False, indent=1))
            return 2

        for col, typ in NEW_COLUMNS:
            if col not in have:
                con.execute(f"ALTER TABLE news_items ADD COLUMN {col} {typ}")
        con.commit()

        # 回填：逐行算派生列 → 新事件键的最早观察时间二次 pass。
        # 索引必须一次性预载；历史库 8 万+ 行逐行 SELECT 会退化成数分钟级。
        rows = con.execute(
            "SELECT id, ts, ingested_at, event_time, title, url, hash, "
            "source, symbol, tags, raw, primary_source_url "
            "FROM news_items WHERE COALESCE(news_time_version,0)<>2").fetchall()
        indexed_symbols: dict[int, list[str]] = {}
        try:
            for news_id, symbol in con.execute(
                    "SELECT news_id, symbol FROM news_events_index "
                    "WHERE symbol IS NOT NULL AND TRIM(symbol)<>'' "
                    "GROUP BY news_id, symbol"):
                indexed_symbols.setdefault(int(news_id), []).append(str(symbol))
        except sqlite3.Error:
            # 旧库允许没有 news_events_index；此时仅使用 news_items.symbol。
            indexed_symbols = {}
        con.execute("BEGIN IMMEDIATE")
        event_first: dict[str, str] = {}
        updates = []
        for r in rows:
            seen = r["ingested_at"] or r["ts"] or ""
            cid = news_writer.cluster_id_for(r["url"], r["hash"] or "")
            grade = news_writer.source_grade(r["url"], r["source"])
            occurred, date_source = news_writer.extract_event_date_with_source(
                r["title"] or "", r["raw"], seen)
            confidence = (
                date_source if occurred
                else ("published_fallback" if r["event_time"] else "unknown"))
            candidate_primary = r["primary_source_url"]
            primary = r["url"] if grade == "primary" else (
                candidate_primary
                if candidate_primary
                and news_writer.source_grade(candidate_primary, None) == "primary"
                else None
            )
            syms = [r["symbol"]] if r["symbol"] else []
            syms.extend(indexed_symbols.get(int(r["id"]), ()))
            event_key = news_writer.event_key_for(
                url=r["url"], dedupe_hash=r["hash"] or "",
                primary_source_url=primary, event_date=occurred,
                symbols=syms, tags=r["tags"],
            )
            updates.append((r["id"], cid, event_key, grade, occurred,
                            confidence, date_source, r["event_time"], seen,
                            primary))
            prev = event_first.get(event_key)
            if prev is None or (seen and seen < prev):
                event_first[event_key] = seen
        # 已回填过的事件键也纳入观察首见基准
        if unfilled != total:
            for r in con.execute(
                    "SELECT event_key, MIN(COALESCE(first_seen_at, "
                    "ingested_at, ts)) AS fs FROM news_items "
                    "WHERE event_key IS NOT NULL GROUP BY event_key"):
                key, fs = r["event_key"], r["fs"]
                if fs and (key not in event_first or fs < event_first[key]):
                    event_first[key] = fs
        write_rows = [
            (cid, grade, occurred, confidence, published,
             event_first.get(event_key) or seen, seen, primary,
             date_source, event_key, rid)
            for (rid, cid, event_key, grade, occurred, confidence, date_source,
                 published, seen, primary) in updates
        ]
        con.executemany(
            "UPDATE news_items SET cluster_id=?, source_grade=?, "
            "event_occurred_at=?, event_time_confidence=?, published_at=?, "
            "first_seen_at=?, last_seen_at=?, primary_source_url=?, "
            "event_date_source=?, event_key=?, news_time_version=2 "
            "WHERE id=?", write_rows)
        n_upd = len(write_rows)

        # 并发采集可在 ALTER 已提交、历史回填尚未提交期间写入 v2 行。那些新行的
        # first_seen_at 必须与随后恢复出来的历史同事件行一起归一，否则仍会把旧事件
        # 显示为刚发现。按主键批量修正，避免无 event_key 索引时逐键 UPDATE 全表扫描。
        global_first = dict(con.execute(
            "SELECT event_key, MIN(COALESCE(ingested_at,ts)) "
            "FROM news_items WHERE event_key IS NOT NULL GROUP BY event_key"
        ).fetchall())
        first_seen_fixes = [
            (global_first[event_key], rid)
            for rid, event_key, first_seen in con.execute(
                "SELECT id,event_key,first_seen_at FROM news_items "
                "WHERE event_key IS NOT NULL")
            if global_first.get(event_key)
            and first_seen != global_first[event_key]
        ]
        con.executemany(
            "UPDATE news_items SET first_seen_at=? WHERE id=?",
            first_seen_fixes,
        )
        con.commit()

        qc2 = con.execute("PRAGMA quick_check").fetchone()[0]
        grades = dict(con.execute(
            "SELECT source_grade, COUNT(*) FROM news_items "
            "GROUP BY source_grade").fetchall())
        conf = dict(con.execute(
            "SELECT event_time_confidence, COUNT(*) FROM news_items "
            "GROUP BY event_time_confidence").fetchall())
        print(json.dumps({
            **report, "ok": qc2 == "ok", "action": "applied",
            "backup": str(bak_path), "columns_added": missing_cols,
            "rows_backfilled": n_upd,
            "first_seen_rows_normalized": len(first_seen_fixes),
            "quick_check": qc2,
            "source_grade_distribution": grades,
            "confidence_distribution": conf,
        }, ensure_ascii=False, indent=1))
        return 0 if qc2 == "ok" else 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
