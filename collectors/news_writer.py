# -*- coding: utf-8 -*-
"""V2.0 §6 —— news_items 唯一确定性 writer（快采 + news-scout 都经它）。

红线「写库必走 writer」：采集器/scout 严禁手写 INSERT news.db，一律经本模块。
确定性校验 + 去重（hash 唯一索引）+ event_time/ingested_at 分离（修「源时间缺就 now」
伪新鲜 bug）+ 多 symbol 进 news_events_index。LLM 取数（scout）/HTTP 抓取（快采）都只
负责「取 + 规整」，落库走本 writer。

migration-aware：news_items 新列（ingested_at/event_time/severity/tags）存在才写，
迁移未跑时只写老列（安全，apply_news_edge_schema 跑后自动启用全列）。

零模型名（红线 #1）；中文 title 经此走 UTF-8（脚本入口 reconfigure，不靠 pwsh wrapper）。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

_COLLECTORS = os.path.dirname(os.path.abspath(__file__))
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)
import ledger  # noqa: E402  复用 connect（WAL/ro 单一来源）+ now_cst

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
DEFAULT_NEWS_DB = Path(os.environ.get("OKX_DB_ROOT", _project_path('db'))) / "news.db"

VALID_LEVELS = {"A", "B", "C"}
VALID_SEVERITY = {"critical", "high", "medium", "low"}


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _table_columns(con, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def compute_hash(source: str, title: str, url: Optional[str],
                 event_time: Optional[str]) -> str:
    """去重指纹：source+title+url+event_time（event_time 纳入避免同标题不同时刻被吞）。"""
    base = "|".join([str(source or ""), str(title or ""), str(url or ""),
                     str(event_time or "")])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """规整一条新闻 dict（不写库）。缺 event_time → None（禁 fallback now）。"""
    title = str(item.get("title") or "").strip()
    source = str(item.get("source") or "").strip()
    url = item.get("url")
    event_time = item.get("event_time") or None   # 缺则 NULL，**禁** now
    level = item.get("level") or "C"
    if level not in VALID_LEVELS:
        level = "C"
    severity = item.get("severity")
    if severity is not None and severity not in VALID_SEVERITY:
        severity = None
    tags = item.get("tags")
    if isinstance(tags, (list, dict)):
        tags = json.dumps(tags, ensure_ascii=False)
    # 多 symbol：symbols(list) 优先，主币用 symbol 或 symbols[0]
    symbols = item.get("symbols")
    if isinstance(symbols, str):
        symbols = [symbols]
    symbol = item.get("symbol")
    if not symbol and symbols:
        symbol = symbols[0]
    dedupe_hash = str(item.get("dedupe_hash") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32,64}", dedupe_hash):
        dedupe_hash = None
    return {
        "source": source, "title": title, "url": url, "event_time": event_time,
        "level": level, "severity": severity, "tags": tags,
        "symbol": symbol, "symbols": symbols or ([symbol] if symbol else []),
        "sentiment": item.get("sentiment"),
        "raw": item.get("raw") if item.get("raw") is not None else item,
        # 迁移旧采集路径时可传既有稳定指纹，避免切 writer 当轮重复落同一事件。
        # 仅接受 32..64 位十六进制；普通 adapter 仍由本 writer 统一计算。
        "dedupe_hash": dedupe_hash,
    }


def write_news(items: list[dict[str, Any]], db_path: str | os.PathLike = DEFAULT_NEWS_DB
               ) -> dict[str, Any]:
    """批量写 news_items（去重）+ 多币进 news_events_index。返回 {inserted, deduped, ...}。"""
    db_path = Path(str(db_path))
    if not db_path.exists():
        return {"ok": False, "error": f"news.db 不存在: {db_path}"}
    ingested = now_cst()
    con = ledger.connect(db_path)
    try:
        cols = _table_columns(con, "news_items")
        has_idx = bool(con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='news_events_index'").fetchone())
        inserted = deduped = idx_rows = 0
        skipped_empty = 0
        for raw_item in items:
            it = normalize_item(raw_item)
            if not it["title"]:
                skipped_empty += 1
                continue
            h = (it["dedupe_hash"]
                 or compute_hash(it["source"], it["title"], it["url"], it["event_time"]))
            # 组装列（只写存在的列，migration-aware）
            row = {
                "ts": ingested,  # 老列 ts 保持（采集落库时刻）
                "source": it["source"], "hash": h, "level": it["level"],
                "symbol": it["symbol"], "title": it["title"], "url": it["url"],
                "sentiment": it["sentiment"],
                "raw": json.dumps(it["raw"], ensure_ascii=False),
            }
            if "ingested_at" in cols:
                row["ingested_at"] = ingested
            if "event_time" in cols:
                row["event_time"] = it["event_time"]
            if "severity" in cols:
                row["severity"] = it["severity"]
            if "tags" in cols:
                row["tags"] = it["tags"]
            fields = [c for c in row if c in cols]
            placeholders = ",".join("?" for _ in fields)
            cur = con.execute(
                f"INSERT OR IGNORE INTO news_items ({','.join(fields)}) "
                f"VALUES ({placeholders})",
                tuple(row[c] for c in fields))
            if cur.rowcount and cur.rowcount > 0:
                inserted += 1
                news_id = cur.lastrowid
                if has_idx and len(it["symbols"]) > 0:
                    for sym in it["symbols"]:
                        if not sym:
                            continue
                        con.execute(
                            "INSERT OR IGNORE INTO news_events_index "
                            "(symbol, ts, news_id) VALUES (?,?,?)",
                            (sym, ingested, news_id))
                        idx_rows += 1
            else:
                deduped += 1
        con.commit()
        return {"ok": True, "inserted": inserted, "deduped": deduped,
                "index_rows": idx_rows, "skipped_empty": skipped_empty,
                "new_cols_active": sorted(
                    c for c in ("ingested_at", "event_time", "severity", "tags")
                    if c in cols)}
    finally:
        con.close()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="V2.0 news_writer（stdin JSON list）")
    ap.add_argument("--db", default=str(DEFAULT_NEWS_DB))
    ap.add_argument("--stdin", action="store_true",
                    help="从 stdin 读 news 列表 JSON（scout/采集器用）")
    args = ap.parse_args()
    if args.stdin:
        # 修复 V2.2: PowerShell 管道破坏 UTF-8 bytes → surrogate 错误
        raw_bytes = sys.stdin.buffer.read()
        raw = raw_bytes.decode("utf-8", errors="replace")
        raw = re.sub(r"[\udc80-\udcff]", "?", raw)
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": f"JSON 解析失败: {e}"},
                             ensure_ascii=False))
            return 1
        if isinstance(items, dict):
            items = items.get("items") or items.get("news") or [items]
        res = write_news(items, args.db)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    print(json.dumps({"ok": False, "error": "需要 --stdin"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
