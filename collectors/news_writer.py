# -*- coding: utf-8 -*-
"""V2.0 §6 —— news_items 唯一确定性 writer（快采 + news-scout 都经它）。

红线「写库必走 writer」：采集器/scout 严禁手写 INSERT news.db，一律经本模块。
确定性校验 + 去重（hash 唯一索引）+ event_time/ingested_at 分离（修「源时间缺就 now」
伪新鲜 bug）+ 多 symbol 进 news_events_index。LLM 取数（scout）/HTTP 抓取（快采）都只
负责「取 + 规整」，落库走本 writer。

migration-aware：news_items 新列（ingested_at/event_time/severity/tags）存在才写，
迁移未跑时只写老列（安全，apply_news_edge_schema 跑后自动启用全列）。

2026-08-10 Wave0-4 时间/来源分层（apply_news_time_layers_schema 跑后启用）：
event_occurred_at（标题/结构化正文显式日期提取，取不出=NULL 宁缺勿假）/
published_at（=旧 event_time 语义）/ first_seen_at（事件簇首次被系统观察，重复采集
只推进 last_seen_at）/ source_grade（primary=官方域名 | aggregator=社媒 | secondary）。
first_seen_at 只表示“观察首见”，绝不代表事件新鲜度；催化时效只由
event_occurred_at 派生。8/7 事件被 8/10 转发刷成"4 分钟前"是 DOT 事故根因。

零模型名（红线 #1）；中文 title 经此走 UTF-8（脚本入口 reconfigure，不靠 pwsh wrapper）。
"""
from __future__ import annotations

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
DEFAULT_NEWS_DB = Path(os.environ.get("OKX_DB_ROOT", r"./db")) / "news.db"

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


# ── 2026-08-10 Wave0-4 时间与来源分层（终稿 T1）────────────────────────────
# 三层时间：event_occurred_at（事件真实发生）/ published_at（媒体发布，=旧
# event_time 语义）/ first_seen_at（事件簇首次入库）。决策侧“催化新鲜度”只准用
# event_occurred_at；first_seen_at 仅说明系统何时首次看到。DOT 事故根因就是把
# 8/7 的 Form RW 在 8/10 的观察/转发时间误当成事件发生时间。

PRIMARY_DOMAINS = (
    "sec.gov", "federalreserve.gov", "treasury.gov", "ecb.europa.eu",
    "bis.org", "imf.org", "cftc.gov", "justice.gov", "whitehouse.gov",
    "okx.com", "grayscale.com",
)
AGGREGATOR_DOMAINS = ("x.com", "twitter.com", "t.me")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
# 刻意区分大小写：小写 "may 20" 是情态动词+数字的高频误报（"BTC may 20%…"），
# 只认标题里首字母大写的月名（实际源数据形如 "(Aug 7)"）。
_EN_DATE_RE = re.compile(
    r"\(?\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2})\b\)?")
_CN_DATE_RE = re.compile(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")


def source_grade(url: Optional[str], source: Optional[str]) -> str:
    """primary=官方一级源域名 | aggregator=社媒转发 | secondary=其余媒体。"""
    u = str(url or "").lower()
    host = ""
    m = re.match(r"https?://([^/]+)", u)
    if m:
        host = m.group(1)
    for d in PRIMARY_DOMAINS:
        if host == d or host.endswith("." + d):
            return "primary"
    for d in AGGREGATOR_DOMAINS:
        if host == d or host.endswith("." + d):
            return "aggregator"
    return "secondary"


def cluster_id_for(url: Optional[str], dedupe_hash: str) -> str:
    """事件簇 v1：同 url 精确键（同一贴文被 15 分钟轮重复采集是主要刷新源）；
    无 url 时退回 dedupe hash 自身（单行簇）。跨源语义聚簇属 Wave2 相似度 v2。"""
    u = str(url or "").strip().lower().rstrip("/")
    if u:
        return hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]
    return f"h-{dedupe_hash[:16]}"


def _canonical_url(url: Optional[str]) -> str:
    return str(url or "").strip().lower().rstrip("/")


def _tag_tokens(tags: Any) -> list[str]:
    value = tags
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [x for x in re.split(r"[,|\s]+", value) if x]
    if isinstance(value, dict):
        value = list(value.keys())
    if not isinstance(value, list):
        return []
    tokens = {
        re.sub(r"[^a-z0-9_-]+", "", str(x).lower())
        for x in value if str(x).strip()
    }
    return sorted(tokens - {"news", "high", "critical", "medium", "low"})


def event_key_for(*, url: Optional[str], dedupe_hash: str,
                  primary_source_url: Optional[str],
                  event_date: Optional[str], symbols: list[Any],
                  tags: Any) -> str:
    """Conservative event key: primary document, then structured semantics, then URL."""
    primary = _canonical_url(primary_source_url)
    if primary:
        base = "primary|" + primary
    else:
        syms = sorted({str(x).strip().upper() for x in symbols if str(x).strip()})
        tag_tokens = _tag_tokens(tags)
        if event_date and syms and tag_tokens:
            base = "semantic|" + "|".join([
                event_date, ",".join(syms), ",".join(tag_tokens),
            ])
        else:
            canonical = _canonical_url(url)
            base = "url|" + canonical if canonical else "hash|" + dedupe_hash
    return "ev-" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]


def _nearest_yearless_date(month: int, day: int,
                           ref: datetime) -> Optional[datetime]:
    candidates = []
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            dt = datetime(year, month, day)
        except ValueError:
            continue
        if dt <= ref + timedelta(days=90):
            candidates.append(dt)
    return min(candidates, key=lambda dt: abs((dt - ref).total_seconds())) \
        if candidates else None


def extract_event_date(title: str, ref_ts: str) -> Optional[str]:
    """从标题提取显式事件日期（'on Aug 7' / '8月7日' / ISO）。取不出返回 None——
    宁缺勿假。无年份日期在 ref 年前后取最近候选，并允许未来 90 天内的已排期事件。"""
    text = str(title or "")
    try:
        ref = datetime.strptime(ref_ts[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        ref = datetime.now(CST).replace(tzinfo=None)

    m = _ISO_DATE_RE.search(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            dt = datetime(y, mo, d)
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%d") if dt <= ref + timedelta(days=90) else None

    m = _CN_DATE_RE.search(text)
    if m:
        if m.group(1):
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        else:
            dt = _nearest_yearless_date(int(m.group(2)), int(m.group(3)), ref)
            if dt is None:
                return None
        return dt.strftime("%Y-%m-%d") if dt <= ref + timedelta(days=90) else None

    m = _EN_DATE_RE.search(text)
    if m:
        mo = _MONTHS[m.group(1).lower()[:3]]
        dt = _nearest_yearless_date(mo, int(m.group(2)), ref)
        return dt.strftime("%Y-%m-%d") if dt is not None else None
    return None


def extract_event_date_with_source(title: str, raw: Any,
                                   ref_ts: str) -> tuple[Optional[str], str]:
    occurred = extract_event_date(title, ref_ts)
    if occurred:
        return occurred, "extracted_title"
    value = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {"text": value}
    if isinstance(value, dict):
        for key in ("content", "body", "summary", "description", "text"):
            occurred = extract_event_date(str(value.get(key) or ""), ref_ts)
            if occurred:
                return occurred, f"extracted_raw.{key}"
    return None, "unknown"


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
    primary_source_url = str(item.get("primary_source_url") or "").strip() or None
    # 该字段会被决策卡当作一级证据，禁止仅凭采集器自报；必须命中 writer 的
    # 官方域名白名单。普通媒体/社媒链接仍保留在 url，不冒充一级源。
    if primary_source_url and source_grade(primary_source_url, None) != "primary":
        primary_source_url = None
    return {
        "source": source, "title": title, "url": url, "event_time": event_time,
        "level": level, "severity": severity, "tags": tags,
        "symbol": symbol, "symbols": symbols or ([symbol] if symbol else []),
        "sentiment": item.get("sentiment"),
        "primary_source_url": primary_source_url,
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
        time_layers_active = "first_seen_at" in cols and "cluster_id" in cols
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
            if time_layers_active:
                # 重复采集只推进 last_seen_at。first_seen_at 是观察首见，
                # 绝不用于催化新鲜度；新鲜度只由 event_occurred_at 派生。
                cid = cluster_id_for(it["url"], h)
                grade = source_grade(it["url"], it["source"])
                occurred, date_source = extract_event_date_with_source(
                    it["title"], it["raw"], ingested)
                confidence = (
                    date_source if occurred
                    else ("published_fallback" if it["event_time"] else "unknown"))
                primary_url = (
                    it["url"] if grade == "primary"
                    else it["primary_source_url"]
                )
                event_key = event_key_for(
                    url=it["url"], dedupe_hash=h,
                    primary_source_url=primary_url,
                    event_date=occurred, symbols=it["symbols"], tags=it["tags"],
                )
                group_col = "event_key" if "event_key" in cols else "cluster_id"
                group_val = event_key if group_col == "event_key" else cid
                prev_seen = con.execute(
                    "SELECT MIN(COALESCE(first_seen_at, ingested_at, ts)) "
                    f"FROM news_items WHERE {group_col}=?", (group_val,)).fetchone()[0]
                row.update({
                    "published_at": it["event_time"],
                    "cluster_id": cid,
                    "source_grade": grade,
                    "primary_source_url": primary_url,
                    "event_occurred_at": occurred,
                    "event_time_confidence": confidence,
                    "event_date_source": date_source,
                    "event_key": event_key,
                    "news_time_version": 2,
                    "first_seen_at": prev_seen or ingested,
                    "last_seen_at": ingested,
                })
            fields = [c for c in row if c in cols]
            placeholders = ",".join("?" for _ in fields)
            cur = con.execute(
                f"INSERT OR IGNORE INTO news_items ({','.join(fields)}) "
                f"VALUES ({placeholders})",
                tuple(row[c] for c in fields))
            if not (cur.rowcount and cur.rowcount > 0) and time_layers_active:
                con.execute(
                    "UPDATE news_items SET last_seen_at=? WHERE hash=?",
                    (ingested, h))
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
                    c for c in ("ingested_at", "event_time", "severity", "tags",
                                "event_occurred_at", "published_at",
                                "first_seen_at", "last_seen_at", "cluster_id",
                                "source_grade", "primary_source_url",
                                "event_time_confidence", "event_date_source",
                                "event_key", "news_time_version")
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
