# -*- coding: utf-8 -*-
"""V2.0 §6 —— PANews 中文快讯 adapter（确定性抓取 + 规整 → news_writer 落库）。

源端点（2026-06-27 探测）：`https://www.panewslab.com/rss.xml`（HTTP 200，
`application/rss+xml`，100 条快讯/feed）。早先 `webapi/flashnews` 端点 06-25 起 404
（站点改 Nuxt 架构）；`api.panewslab.com` / `rss.panewslab.com` SSL/超时受阻 —— 本
adapter 改走站点 RSS（同一站点原生供给，无需 webapi）。

RSS item 结构：title(中文) / link / pubDate(RFC822 GMT) / guid(uuid) /
description(正文摘要)。event_time = pubDate 转 UTC+8 字符串（红线②）。

与 news_rss.py 同构（_fetch / _extract_symbols / _severity / _tags / _parse /
fetch_items / collect / main）；区别仅在：
  - 单一中文源（非多英文 feed）；
  - 币种抽取兼顾中文币名（比特币→BTC 以太坊→ETH …）+ $TICKER/大写 ticker；
  - severity/tags 关键词兼顾中英文（黑客/盗/监管/上线/巨鲸 …）。

本模块只「取 + 确定性规整」，**不塞 LLM**（红线⑩）；情绪/影响真判断归 analyst。
写库走 news_writer（禁手写 INSERT，红线④）。零模型名（红线①）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

_COLLECTORS = str(Path(__file__).resolve().parents[1])  # ./collectors
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)
import news_writer  # noqa: E402
try:  # 兼容生产 sys.path 模块导入与项目包导入两种入口
    from ._news_http import fetch_text as _fetch_text_httpx  # type: ignore
except ImportError:  # pragma: no cover - production imports adapters by module name
    from _news_http import fetch_text as _fetch_text_httpx  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
SOURCE_ID = "panews"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 站点原生 RSS（webapi/flashnews 已 404；此为同站可达端点）
DEFAULT_ENDPOINT = "https://www.panewslab.com/rss.xml"
OFFICIAL_PAGE_ENDPOINT = "https://panews.io/newsflash"
_ARTICLE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)

# ── 币种抽取 ──────────────────────────────────────────────────────────────
# 1) 中文币名 → 符号（先匹配，避免「比特币」被英文规则漏掉）
_CN_COIN_MAP = {
    "比特币": "BTC", "以太坊": "ETH", "以太币": "ETH", "索拉纳": "SOL",
    "瑞波": "XRP", "瑞波币": "XRP", "狗狗币": "DOGE", "狗狗": "DOGE",
    "莱特币": "LTC", "波场": "TRX", "波卡": "DOT", "艾达币": "ADA",
    "柴犬币": "SHIB", "屎币": "SHIB", "门罗币": "XMR", "大零币": "ZEC",
    "比特现金": "BCH", "恒星币": "XLM", "唯链": "VET", "波场币": "TRX",
    "泰达币": "USDT", "稳定币": None,  # 「稳定币」泛指，不映射具体符号
}
# 2) 英文/大写 ticker（词边界匹配；curated 避免 ON/ARB 等误命中普通词）
_COINS = [
    "BTC", "BITCOIN", "ETH", "ETHEREUM", "SOL", "SOLANA", "XRP", "RIPPLE",
    "BNB", "DOGE", "DOGECOIN", "ADA", "CARDANO", "AVAX", "LINK", "CHAINLINK",
    "TRX", "TRON", "TON", "DOT", "POLKADOT", "MATIC", "POLYGON", "SHIB",
    "LTC", "LITECOIN", "BCH", "UNI", "AAVE", "ARB", "ARBITRUM", "OP",
    "OPTIMISM", "SUI", "APT", "APTOS", "INJ", "SEI", "TIA", "PEPE", "WIF",
    "NEAR", "FIL", "ATOM", "ETC", "XLM", "ICP", "HBAR", "RNDR", "RENDER",
    "HYPE", "ZEC", "TRUMP", "POPCAT", "WBTC", "USDT", "USDC", "POND",
    "ALCX", "ARDR", "NFP", "CAP", "ARX", "POPMART",
]
_COIN_TO_SYM = {
    "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL", "RIPPLE": "XRP",
    "DOGECOIN": "DOGE", "CARDANO": "ADA", "CHAINLINK": "LINK", "TRON": "TRX",
    "POLKADOT": "DOT", "POLYGON": "MATIC", "LITECOIN": "LTC", "ARBITRUM": "ARB",
    "OPTIMISM": "OP", "APTOS": "APT", "RENDER": "RNDR",
}
# 注：Python re 把 CJK 当作 word char，`\b` 在「美国HYPE现货」里不触发 → ticker 漏抽。
# 改用 ASCII 边界 lookaround：ticker 两侧不得是 ASCII 字母/数字（容许紧贴中文/标点），
# 既能命中「ZEC空单」「美国HYPE」，又不会在 BITCOINIST 等更长拉丁词内部误命中。
_COIN_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(_COINS, key=len, reverse=True))
    + r")(?![A-Za-z0-9])", re.I)

# ── 规则标签（确定性；中英文关键词）─────────────────────────────────────────
_TAG_RULES = [
    ("hack", re.compile(
        r"(hack|exploit|breach|drain|stolen|attack|rug|"
        r"黑客|被盗|盗走|盗窃|攻击|漏洞|诈骗|被黑|提款机)", re.I)),
    ("regulatory", re.compile(
        r"(SEC|CFTC|DOJ|lawsuit|regulat|court|\bban\b|sue|fine|settle|MiCA|"
        r"监管|诉讼|法庭|起诉|禁止|罚款|合规|牌照|立法|警示名单|证监会|金管局|"
        r"司法部|众议员|议员|提案)", re.I)),
    ("listing", re.compile(
        r"(listing|lists?|delist|launch|debut|"
        r"上线|下架|首发|上市|发行|推出|开放注册)", re.I)),
    ("etf", re.compile(r"(ETF|inflow|outflow|现货ETF|净流入|净流出)", re.I)),
    ("macro", re.compile(
        r"(Fed|inflation|CPI|rate cut|FOMC|GDP|PCE|Nasdaq|"
        r"美联储|加息|降息|通胀|纳指|美股|半导体|指数|停火|无人机)", re.I)),
    ("whale", re.compile(
        r"(whale|million|billion|moves?|transfer|"
        r"巨鲸|鲸鱼|提取|提走|存入|抛售|套现|建仓|空单|多单|杠杆)", re.I)),
    ("funding", re.compile(
        r"(融资|募资|种子轮|领投|参投|基金|Pre-Seed|Series|raise|funding round)",
        re.I)),
    ("stablecoin", re.compile(r"(stablecoin|稳定币|USDT|USDC|泰铢|日元稳定币)", re.I)),
]
# severity：critical/high 关键词（中英文）
_SEV_HIGH = re.compile(
    r"(hack|exploit|breach|drain|stolen|SEC|lawsuit|\bban\b|crash|plunge|surge|"
    r"ETF approv|黑客|被盗|盗走|盗窃|攻击|漏洞|起诉|诉讼|禁止|禁挖|下架|"
    r"暴跌|崩盘|清算|跌破|深跌|警示名单)", re.I)
_SEV_MED = re.compile(
    r"(listing|launch|partnership|upgrade|fork|rate cut|FOMC|"
    r"上线|首发|上市|发行|推出|融资|募资|加息|降息|合作|合规|牌照|净流出|净流入)",
    re.I)


def _fetch(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.panewslab.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _stable_dedupe_hash(article_id: str) -> str:
    return hashlib.sha256(
        f"panews|{article_id.strip().lower()}".encode("utf-8")
    ).hexdigest()[:32]


def _resolve_devalue(values: list, ref, seen: set[int] | None = None):
    """Resolve the small reference-array format embedded by React Router SSR."""
    if isinstance(ref, bool) or not isinstance(ref, int):
        return ref
    if ref < 0:
        return None
    if ref >= len(values):
        raise ValueError(f"PANews page reference out of range: {ref}")
    seen = set(seen or ())
    if ref in seen:
        raise ValueError(f"PANews page cyclic reference: {ref}")
    seen.add(ref)
    value = values[ref]
    if isinstance(value, dict):
        resolved = {}
        for raw_key, raw_value in value.items():
            key = (
                _resolve_devalue(values, int(raw_key[1:]), seen)
                if isinstance(raw_key, str) and re.fullmatch(r"_\d+", raw_key)
                else raw_key
            )
            resolved[str(key)] = _resolve_devalue(values, raw_value, seen)
        return resolved
    if isinstance(value, list):
        return [_resolve_devalue(values, child, seen) for child in value]
    return value


def _parse_official_page(page_text: str, max_age_hours: int) -> list[dict]:
    """Strictly parse PANews' server-rendered official newsflash payload."""
    chunks: list[str] = []
    for match in re.finditer(
        r"streamController\.enqueue\((.*?)\);\s*</script>",
        page_text,
        re.S,
    ):
        argument = match.group(1).strip()
        decoded = json.loads(argument)
        if not isinstance(decoded, str):
            raise ValueError("PANews stream chunk is not a JSON string")
        chunks.append(decoded)
    if not chunks:
        raise ValueError("PANews official page stream payload missing")

    payload = None
    parse_errors: list[str] = []
    for chunk in chunks:
        try:
            values = json.loads(chunk)
            if not isinstance(values, list):
                continue
            resolved = _resolve_devalue(values, 0)
            candidate = (
                resolved.get("loaderData", {})
                .get("routes/newsflash", {})
                .get("payload")
            )
            if isinstance(candidate, dict) and isinstance(
                    candidate.get("articles"), list):
                payload = candidate
                break
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            parse_errors.append(f"{type(exc).__name__}: {exc}")
    if payload is None:
        detail = "; ".join(parse_errors)[:240]
        raise ValueError(f"PANews official page articles missing: {detail}")

    articles = payload["articles"]
    if not articles:
        raise ValueError("PANews official page returned an empty initial article page")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    out: list[dict] = []
    for article in articles:
        if not isinstance(article, dict):
            raise ValueError("PANews official page article is not an object")
        article_id = str(article.get("id") or "").strip().lower()
        title = str(article.get("title") or "").strip()
        published = str(article.get("publishedAt") or "").strip()
        if not _ARTICLE_ID_RE.fullmatch(article_id) or not title or not published:
            raise ValueError("PANews official page article required fields invalid")
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"PANews official page publishedAt invalid: {published}"
            ) from exc
        if dt.tzinfo is None:
            raise ValueError("PANews official page publishedAt lacks timezone")
        if dt.astimezone(timezone.utc) < cutoff:
            continue
        description = str(article.get("desc") or "").strip()
        canonical_url = (
            "https://www.panewslab.com/zh/articles/" + article_id)
        out.append({
            "source": SOURCE_ID,
            "title": title,
            "url": canonical_url,
            "event_time": dt.astimezone(CST).strftime(TS_FMT),
            "symbols": _extract_symbols(title),
            "severity": _severity(title),
            "tags": _tags(title),
            "level": "B",
            "dedupe_hash": _stable_dedupe_hash(article_id),
            "raw": {
                "feed": "panews_official_page",
                "guid": article_id,
                "publishedAt": published,
                "description": description,
                "isImportant": bool(article.get("isImportant")),
            },
        })
    return out


def _extract_symbols(title: str) -> list[str]:
    title = title or ""
    found: list[str] = []

    def _add(sym: str) -> None:
        if not sym:
            return
        inst = f"{sym}-USDT-SWAP"
        if inst not in found:
            found.append(inst)

    # 1) 中文币名优先
    for cn, sym in _CN_COIN_MAP.items():
        if cn in title:
            _add(sym)
    # 2) 大写 ticker（含 $TICKER）
    for m in _COIN_RE.finditer(title):
        tok = m.group(1).upper()
        _add(_COIN_TO_SYM.get(tok, tok))
    return found


def _severity(title: str) -> str:
    if _SEV_HIGH.search(title or ""):
        return "high"
    if _SEV_MED.search(title or ""):
        return "medium"
    return "low"


def _tags(title: str) -> list[str]:
    return [name for name, rx in _TAG_RULES if rx.search(title or "")]


def _parse(xml_text: str, max_age_hours: int) -> list[dict]:
    out: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        event_time = None
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
                # 红线②：转 UTC+8 字符串（非 UTC-Z）
                event_time = dt.astimezone(CST).strftime(TS_FMT)
            except (ValueError, TypeError):
                event_time = None  # 解析失败 → NULL，禁伪 now
        out.append({
            "source": SOURCE_ID,
            "title": title, "url": link or None, "event_time": event_time,
            "symbols": _extract_symbols(title),
            "severity": _severity(title), "tags": _tags(title),
            "level": "B",
            "dedupe_hash": _stable_dedupe_hash(guid) if guid else None,
            "raw": {"feed": "panews_rss", "guid": guid, "pubDate": pub},
        })
    return out


def fetch_items(endpoint: str = DEFAULT_ENDPOINT, max_age_hours: int = 24,
                timeout: int = 15, errors: list[str] | None = None,
                retry_timeout: int = 6,
                official_page_endpoint: str = OFFICIAL_PAGE_ENDPOINT,
                page_timeout: int = 8,
                retry_stats: dict | None = None) -> list[dict]:
    """errors（2026-07-06 可观测性）：调用方传入 list 时，fetch 失败的简短原因
    （HTTP 码/超时等，截 150 字）append 进去——供 collect() 把 err 带回
    news_collect → ledger.collection_runs.err（degraded 行也必须带明细，
    无法区分限流/网络）。不传则行为同旧版（只打 stderr）。"""
    last_error: Exception | None = None
    for attempt in (1, 2):
        if attempt == 2:
            time.sleep(0.5)
        try:
            parsed = _parse(
                _fetch(endpoint, timeout=(timeout if attempt == 1 else retry_timeout)),
                max_age_hours,
            )
            if retry_stats is not None:
                retry_stats.update({
                    "attempts": attempt,
                    "recovered_after_retry": attempt == 2,
                })
            return parsed
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    rss_error = last_error
    for page_attempt in (1, 2):
        if page_attempt == 2:
            time.sleep(0.5)
        try:
            page_text = _fetch_text_httpx(
                official_page_endpoint,
                timeout=float(page_timeout),
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            parsed = _parse_official_page(page_text, max_age_hours)
            if retry_stats is not None:
                retry_stats.update({
                    "attempts": 2 + page_attempt,
                    "rss_attempts": 2,
                    "official_page_attempts": page_attempt,
                    "recovered_after_fallback": True,
                    "transport": "official_page",
                })
            return parsed
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    message = (
        f"rss={type(rss_error).__name__}: {rss_error}; "
        f"official_page={type(last_error).__name__}: {last_error}"
    )[:150]
    print(f"[WARN] PANews fetch failed after retry: {message}", file=sys.stderr)
    if errors is not None:
        errors.append(f"fetch: {message}"[:150])
    if retry_stats is not None:
        retry_stats.update({
            "attempts": 4,
            "rss_attempts": 2,
            "official_page_attempts": 2,
            "final_failed": True,
        })
    return []


def _reuse_existing_hashes(items: list[dict], db_path: str) -> int:
    """Reuse legacy hashes for already-seen PANews UUID URLs without writing."""
    urls = sorted({str(item.get("url") or "") for item in items if item.get("url")})
    path = Path(str(db_path))
    if not urls or not path.exists():
        return 0
    placeholders = ",".join("?" for _ in urls)
    con = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT url, hash FROM news_items "
            f"WHERE source=? AND url IN ({placeholders}) ORDER BY id DESC",
            (SOURCE_ID, *urls),
        ).fetchall()
    finally:
        con.close()
    existing: dict[str, str] = {}
    for url, value in rows:
        if url not in existing and re.fullmatch(r"[0-9a-f]{32,64}", str(value or "")):
            existing[url] = str(value)
    reused = 0
    for item in items:
        value = existing.get(str(item.get("url") or ""))
        if value:
            item["dedupe_hash"] = value
            reused += 1
    return reused


def collect(db_path: str, endpoint: str = DEFAULT_ENDPOINT,
            max_age_hours: int = 24, apply: bool = False) -> dict:
    fetch_errs: list[str] = []
    retry_stats: dict = {}
    items = fetch_items(endpoint=endpoint, max_age_hours=max_age_hours,
                        errors=fetch_errs, retry_stats=retry_stats)
    err_txt = "; ".join(fetch_errs)[:150] if fetch_errs else None
    if not apply:
        out = {"ok": True, "dry_run": True, "fetched": len(items),
               "retry_stats": retry_stats,
               "sample": [{"t": it["title"][:70], "et": it["event_time"],
                           "sym": it["symbols"], "sev": it["severity"],
                           "tags": it["tags"]}
                          for it in items[:8]]}
        if err_txt:
            out["err"] = err_txt
        return out
    existing_hash_reused = _reuse_existing_hashes(items, db_path)
    res = news_writer.write_news(items, db_path)
    res["fetched"] = len(items)
    res["retry_stats"] = retry_stats
    res["existing_hash_reused"] = existing_hash_reused
    if err_txt and not res.get("err"):
        res["err"] = err_txt
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 PANews news adapter → news_writer")
    ap.add_argument("--db", default=str(news_writer.DEFAULT_NEWS_DB))
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    args = ap.parse_args()
    res = collect(args.db, endpoint=args.endpoint,
                  max_age_hours=args.hours, apply=args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
