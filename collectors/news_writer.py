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


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


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
DEFAULT_NEWS_DB = Path(os.environ.get("OKX_DB_ROOT", _public_project_path('db'))) / "news.db"

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


_X_STATUS_RE = re.compile(
    r"(?:^|//)(?:www\.)?(?:x|twitter)\.com/(?:[^/]+/)+status/(\d+)", re.IGNORECASE)


def x_status_id(url: Optional[str]) -> Optional[str]:
    """取 X 帖文的 status id（帖文不变标识）；非 X 帖文链接返回 None。"""
    m = _X_STATUS_RE.search(str(url or ""))
    return m.group(1) if m else None


def dedupe_hash_for(source: str, title: str, url: Optional[str],
                    event_time: Optional[str]) -> str:
    """去重指纹。X 帖文按 status id 定键，其余来源保持 title 指纹不变。

    2026-09-12：同一条 X 帖子每轮被模型重新概括，title 随措辞变化 → 旧的
    title 指纹判为新事件，把同帖反复入库（实测整体约 8.9 行/帖，最严重的
    status/2096940695416344942 入库 58 次、tokenomist_ai 58 行仅 1 条帖）。
    status id 是帖文的不变标识，故 X 类改用它；非 X 源仍需 title+event_time
    区分「同标题不同时刻」，行为刻意不动。
    """
    sid = x_status_id(url)
    if sid:
        return hashlib.sha256(
            ("x_status|" + sid).encode("utf-8")).hexdigest()[:32]
    return compute_hash(source, title, url, event_time)


# ── 2026-08-10 Wave0-4 时间与来源分层（终稿 T1）────────────────────────────
# 三层时间：event_occurred_at（事件真实发生）/ published_at（媒体发布，=旧
# event_time 语义）/ first_seen_at（事件簇首次入库）。决策侧“催化新鲜度”只准用
# event_occurred_at；first_seen_at 仅说明系统何时首次看到。DOT 事故根因就是把
# 8/7 的 Form RW 在 8/10 的观察/转发时间误当成事件发生时间。

PRIMARY_DOMAINS = (
    "sec.gov", "federalreserve.gov", "treasury.gov", "ecb.europa.eu",
    "bis.org", "imf.org", "cftc.gov", "justice.gov", "whitehouse.gov",
    "okx.com", "grayscale.com",
    # 2026-08-18 A3-lite：解锁日历所有者=该数据类的「指标所有者官方网页」
    # （来源优先级第三档）。仅在 scout 把日历页/原始排期 URL 写入
    # primary_source_url 时生效，用于解锁催化过「一级源核实」门；
    # 转述性媒体贴不得附（见 news_scout.md 解锁条目）。
    "tokenomist.ai", "defillama.com",
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

# 2026-08-20：相对日期层。此前只认显式日期，实测近 7 天 source_grade=primary 且
# 带标的的 509 条里 421 条（82.7%）拿不到 event_occurred_at——因为币圈标题绝大多数
# 用「yesterday / in the past 24 hours / 今日」这类相对表述。而角色契约规定「事件日
# 未知不得写 fresh」「负 EV 候选只有经一级源核实的新鲜催化才可 ev_override」，于是
# 催化通道实际上被自己的抽取覆盖率掐死。
#
# 只收**能唯一钉到某一天**的表述，仍守「宁缺勿假」：
#   - 明确指日的（today/yesterday/今日/昨日）；
#   - 明确以观察时刻为界的滚动 24h 聚合事件（past 24 hours/过去24小时）——这类
#     事件本身跨两天，锚到观察日是它唯一可判定的口径；
#   - 「recently/近期/本周/this week」一律不收，太粗，钉不到天。
# 相对层的 event_date_source 单独打标（relative_title / relative_raw.<key>），因此
# event_time_confidence 也随之可区分，审计能把它和显式日期分开统计。
_RELATIVE_DAY_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\byesterday\b", re.IGNORECASE), -1),
    (re.compile(r"昨[日天]"), -1),
    (re.compile(r"\btoday\b", re.IGNORECASE), 0),
    (re.compile(r"今[日天]"), 0),
    (re.compile(
        r"\b(?:in|over|during)\s+the\s+(?:past|last)\s+24\s*h(?:ours?)?\b",
        re.IGNORECASE), 0),
    (re.compile(r"\bpast\s+24\s*h(?:ours?)?\b", re.IGNORECASE), 0),
    (re.compile(r"(?:过去|近|最近)\s*24\s*小时"), 0),
    (re.compile(r"24\s*小时内"), 0),
)


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


def extract_relative_event_date(text: str, ref_ts: str) -> Optional[str]:
    """从相对表述钉出事件日（'yesterday' / '今日' / 'past 24 hours'）。

    取不出返回 None。锚点是调用方给的参考时刻（优先媒体发布时刻，缺则采集时刻）；
    只回落到「当天」或「前一天」，绝不产生未来日期。
    """
    body = str(text or "")
    if not body:
        return None
    try:
        ref = datetime.strptime(str(ref_ts)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    for pattern, delta_days in _RELATIVE_DAY_PATTERNS:
        if pattern.search(body):
            return (ref + timedelta(days=delta_days)).strftime("%Y-%m-%d")
    return None


def extract_event_date_with_source(
        title: str, raw: Any, ref_ts: str,
        relative_ref_ts: Optional[str] = None) -> tuple[Optional[str], str]:
    """事件日 + 来源标签。优先级：标题显式 > 标题相对 > 正文显式 > 正文相对。

    标题相对刻意排在正文显式之前：正文里的日期常是预测目标日、解锁日或引用的旧
    事件日，实测出现过「今天突破的行情」被正文里一个未来日期覆盖成 scheduled 的
    情况；而标题里的 'yesterday/今日' 说的就是本条新闻自己的事件日。
    """
    rel_ref = relative_ref_ts or ref_ts
    occurred = extract_event_date(title, ref_ts)
    if occurred:
        return occurred, "extracted_title"
    occurred = extract_relative_event_date(title, rel_ref)
    if occurred:
        return occurred, "relative_title"
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
        for key in ("content", "body", "summary", "description", "text"):
            occurred = extract_relative_event_date(
                str(value.get(key) or ""), rel_ref)
            if occurred:
                return occurred, f"relative_raw.{key}"
    return None, "unknown"


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """规整一条新闻 dict（不写库）。缺 event_time → None（禁 fallback now）。"""
    title = str(item.get("title") or "").strip()
    source = str(item.get("source") or "").strip()
    url = item.get("url")
    # 2026-09-13 来源可信度防护：x_search 不可用时 scout 会降级走 web_search，
    # 但旧契约要求每条都标 source="x_search"，实测近 36h 465 行里 382 行(82%)
    # 其实是 cointelegraph/binance/coinmarketcap 等网页。url 不是 X 帖文链接的
    # 一律确定性重标为 web_search（V3 的 6acef28 同一做法），原始自报保留在 raw。
    provenance_relabeled = False
    if source == "x_search" and x_status_id(url) is None:
        source = "web_search"
        provenance_relabeled = True
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
        "provenance_relabeled": provenance_relabeled,
    }


def write_news(items: list[dict[str, Any]], db_path: str | os.PathLike = DEFAULT_NEWS_DB
               ) -> dict[str, Any]:
    """批量写新闻；任一空 source 在连接数据库前整批 fail-closed。"""
    db_path = Path(str(db_path))
    if not db_path.exists():
        return {"ok": False, "error": f"news.db 不存在: {db_path}"}
    normalized_items = [normalize_item(item) for item in items]
    invalid_source_indices = [
        index for index, item in enumerate(normalized_items)
        if not item["source"]
    ]
    if invalid_source_indices:
        return {
            "ok": False,
            "error": "news_source_required",
            "inserted": 0,
            "invalid_source_indices": invalid_source_indices,
        }
    ingested = now_cst()
    con = ledger.connect(db_path)
    try:
        cols = _table_columns(con, "news_items")
        has_idx = bool(con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='news_events_index'").fetchone())
        inserted = deduped = idx_rows = 0
        skipped_empty = 0
        relabeled = sum(1 for it in normalized_items if it.get("provenance_relabeled"))
        time_layers_active = "first_seen_at" in cols and "cluster_id" in cols
        for it in normalized_items:
            if not it["title"]:
                skipped_empty += 1
                continue
            h = (it["dedupe_hash"]
                 or dedupe_hash_for(it["source"], it["title"], it["url"],
                                    it["event_time"]))
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
                # 相对表述锚到媒体发布时刻更准（采集可能跨过零点），缺则用采集
                # 时刻；显式日期仍以采集时刻为参考年，行为不变。
                occurred, date_source = extract_event_date_with_source(
                    it["title"], it["raw"], ingested,
                    relative_ref_ts=(it["event_time"] or ingested))
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
            if not (cur.rowcount and cur.rowcount > 0) and has_idx:
                # 去重命中仍要并入多币索引：同一条帖子跨轮可能带出新币种，
                # 按 status id 定键后这些条目会被判重，若不在此补索引就会丢。
                # news_events_index 主键含 ts，INSERT OR IGNORE 拦不住不同 ts
                # 的重复，故先显式查 (symbol, news_id) 是否已存在。
                hit = con.execute(
                    "SELECT id FROM news_items WHERE hash=?", (h,)).fetchone()
                if hit:
                    for sym in it["symbols"]:
                        if not sym:
                            continue
                        if con.execute(
                                "SELECT 1 FROM news_events_index "
                                "WHERE symbol=? AND news_id=? LIMIT 1",
                                (sym, hit[0])).fetchone():
                            continue
                        con.execute(
                            "INSERT OR IGNORE INTO news_events_index "
                            "(symbol, ts, news_id) VALUES (?,?,?)",
                            (sym, ingested, hit[0]))
                        idx_rows += 1
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
                "relabeled_web": relabeled,
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
