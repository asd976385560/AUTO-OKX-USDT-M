# -*- coding: utf-8 -*-
"""V2.0 §6 —— RSS 新闻 adapter（确定性抓取 + 规整 → news_writer 落库）。

实测可达源：cointelegraph/decrypt/coindesk/theblock/cryptoslate/bitcoinist。
2026-08-12 CryptoPotato 持续 403/timeout 后，以官网明确提供且生产同路径实测可达的
CryptoSlate RSS 替换。中文快讯三源（jinse/panews/odaily）
已于 06-27 换端点复活（enabled 状态与端点以 sources/registry.json 为准），由 news_collect
按各自 adapter（news_jinse/news_panews/news_odaily）迭代；blockbeats 仍待端点恢复
（registry 中 enabled:false）。

统一写入契约：
  - **写库走 news_writer**（禁手写 INSERT，红线④）。
  - **event_time = pubDate 转 UTC+8 字符串**（红线②；旧版用 UTC-Z）。
  - 多币：标题抽 ticker → symbols → news_writer 落 news_events_index。
  - severity/tags = 规则标签（确定性，非 LLM 判断，红线⑩）。

本模块只「取 + 确定性规整」，**不塞 LLM**；情绪/影响的真判断归 analyst。
零模型名（红线①）。
"""
from __future__ import annotations

import argparse
import html
import json
import re
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
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OKX-V2-RSS/1.0"

DEFAULT_FEEDS = [
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("TheBlock", "https://www.theblock.co/rss.xml"),
    # 2026-08-12：CryptoPotato 持续 403/timeout；CryptoSlate 官网 RSS 链接
    # https://cryptoslate.com/feed/ 经生产相同代理/解析链实测 120905 bytes、
    # 近24h 10条，故替换而非静默降低英文源数量。
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ("Bitcoinist", "https://bitcoinist.com/feed/"),
]

# Same-publisher structured fallbacks are permitted only after both transports
# on the canonical feed URL fail.  The endpoint remains on the publisher's
# official domain and exposes the same public WordPress posts as the homepage.
OFFICIAL_PUBLISHER_FALLBACKS = {
    "https://bitcoinist.com/feed/": (
        "wordpress_posts",
        "https://bitcoinist.com/?rest_route=/wp/v2/posts&per_page=20"
        "&_fields=id,date_gmt,link,title",
    ),
}

# 币种抽取：curated 常见 ticker（词边界匹配，避免 ON/ARB 等误命中普通词）
_COINS = [
    "BTC", "BITCOIN", "ETH", "ETHEREUM", "SOL", "SOLANA", "XRP", "RIPPLE",
    "BNB", "DOGE", "DOGECOIN", "ADA", "CARDANO", "AVAX", "LINK", "CHAINLINK",
    "TRX", "TRON", "TON", "DOT", "POLKADOT", "MATIC", "POLYGON", "SHIB",
    "LTC", "LITECOIN", "BCH", "UNI", "AAVE", "ARB", "ARBITRUM", "OP",
    "OPTIMISM", "SUI", "APT", "APTOS", "INJ", "SEI", "TIA", "PEPE", "WIF",
    "NEAR", "FIL", "ATOM", "ETC", "XLM", "ICP", "HBAR", "RNDR", "RENDER",
]
_COIN_TO_SYM = {
    "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL", "RIPPLE": "XRP",
    "DOGECOIN": "DOGE", "CARDANO": "ADA", "CHAINLINK": "LINK", "TRON": "TRX",
    "POLKADOT": "DOT", "POLYGON": "MATIC", "LITECOIN": "LTC", "ARBITRUM": "ARB",
    "OPTIMISM": "OP", "APTOS": "APT", "RENDER": "RNDR",
}
_COIN_RE = re.compile(r"\b(" + "|".join(sorted(_COINS, key=len, reverse=True)) + r")\b", re.I)

# 规则标签（确定性）
_TAG_RULES = [
    ("hack", re.compile(r"\b(hack|exploit|breach|drain|stolen|attack|rug)\b", re.I)),
    ("regulatory", re.compile(r"\b(SEC|lawsuit|regulat|court|ban|sue|fine|settle|CFTC|DOJ)\b", re.I)),
    ("listing", re.compile(r"\b(listing|lists?|delist|launch|debut)\b", re.I)),
    ("etf", re.compile(r"\b(ETF|inflow|outflow)\b", re.I)),
    ("macro", re.compile(r"\b(Fed|inflation|CPI|rate cut|FOMC|jobs report|GDP)\b", re.I)),
    ("whale", re.compile(r"\b(whale|million|billion|moves?|transfer)\b", re.I)),
]
# severity：critical/high 关键词
_SEV_HIGH = re.compile(r"\b(hack|exploit|breach|drain|stolen|SEC|lawsuit|ban|crash|plunge|surge|ETF approv)\b", re.I)
_SEV_MED = re.compile(r"\b(listing|launch|partnership|upgrade|fork|rate cut|FOMC)\b", re.I)


def _fetch(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _fetch_retry(url: str, timeout: int = 6) -> tuple[str, str, int]:
    """Retry the same publisher URL, then make two bounded httpx attempts.

    The second httpx attempt is deliberately the same official URL.  It covers
    short-lived proxy/TLS EOFs without introducing a mirror or silently
    changing publisher provenance.  The returned attempt count excludes the
    caller's initial urllib attempt.
    """
    try:
        return _fetch(url, timeout=timeout), "urllib", 1
    except Exception as urllib_error:  # noqa: BLE001
        httpx_errors: list[Exception] = []
        for httpx_attempt in (1, 2):
            if httpx_attempt == 2:
                time.sleep(0.5)
            try:
                text = _fetch_text_httpx(
                    url,
                    timeout=float(timeout),
                    headers={
                        "User-Agent": UA,
                        "Accept": "application/rss+xml, application/xml, */*",
                    },
                )
                return text, "httpx", 1 + httpx_attempt
            except Exception as httpx_error:  # noqa: BLE001
                httpx_errors.append(httpx_error)
        httpx_detail = "; ".join(
            f"httpx{index}={type(error).__name__}: {error}"
            for index, error in enumerate(httpx_errors, start=1)
        )
        final_error = httpx_errors[-1]
        raise RuntimeError(
            "alternate transports failed: "
            f"urllib={type(urllib_error).__name__}: {urllib_error}; "
            f"{httpx_detail}"
        ) from final_error


def _parse_wordpress_posts(
    name: str,
    payload_text: str,
    max_age_hours: int,
) -> list[dict]:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError("official WordPress fallback is not JSON") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("official WordPress fallback posts missing")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    output: list[dict] = []
    for post in payload:
        if not isinstance(post, dict):
            raise ValueError("official WordPress fallback post is not an object")
        post_id = post.get("id")
        raw_title = post.get("title")
        title_value = (
            raw_title.get("rendered") if isinstance(raw_title, dict) else None)
        title = html.unescape(
            re.sub(r"<[^>]+>", "", str(title_value or ""))
        ).strip()
        link = str(post.get("link") or "").strip()
        published = str(post.get("date_gmt") or "").strip()
        if not post_id or not title or not link or not published:
            raise ValueError("official WordPress fallback required fields invalid")
        if not re.match(r"^https://(?:www\.)?bitcoinist\.com/", link, re.I):
            raise ValueError("official WordPress fallback link host invalid")
        try:
            published_dt = datetime.fromisoformat(published)
            if published_dt.tzinfo is None:
                published_dt = published_dt.replace(tzinfo=timezone.utc)
            else:
                published_dt = published_dt.astimezone(timezone.utc)
        except ValueError as exc:
            raise ValueError(
                f"official WordPress fallback date_gmt invalid: {published}"
            ) from exc
        if published_dt < cutoff:
            continue
        output.append({
            "source": f"rss:{name.lower()}",
            "title": title,
            "url": link,
            "event_time": published_dt.astimezone(CST).strftime(TS_FMT),
            "symbols": _extract_symbols(title),
            "severity": _severity(title),
            "tags": _tags(title),
            "level": "B",
            "raw": {
                "feed": name,
                "post_id": post_id,
                "date_gmt": published,
                "transport": "official_wordpress_rest",
            },
        })
    return output


def _fetch_official_publisher_fallback(
    name: str,
    feed_url: str,
    max_age_hours: int,
    *,
    timeout: int = 8,
) -> tuple[list[dict], str, int]:
    configured = OFFICIAL_PUBLISHER_FALLBACKS.get(feed_url)
    if configured is None:
        raise ValueError("no official publisher fallback configured")
    kind, endpoint = configured
    last_error: Exception | None = None
    for attempt in (1, 2):
        if attempt == 2:
            time.sleep(0.5)
        try:
            payload_text = _fetch_text_httpx(
                endpoint,
                timeout=float(timeout),
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json,*/*",
                },
            )
            if kind == "wordpress_posts":
                return (
                    _parse_wordpress_posts(name, payload_text, max_age_hours),
                    "official_wordpress_rest",
                    attempt,
                )
            raise ValueError(f"unsupported publisher fallback kind: {kind}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(
        "official publisher fallback failed: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def _extract_symbols(title: str) -> list[str]:
    found = []
    for m in _COIN_RE.finditer(title or ""):
        tok = m.group(1).upper()
        sym = _COIN_TO_SYM.get(tok, tok)
        inst = f"{sym}-USDT-SWAP"
        if inst not in found:
            found.append(inst)
    return found


def _severity(title: str) -> str:
    if _SEV_HIGH.search(title or ""):
        return "high"
    if _SEV_MED.search(title or ""):
        return "medium"
    return "low"


def _tags(title: str) -> list[str]:
    return [name for name, rx in _TAG_RULES if rx.search(title or "")]


def _parse_feed(name: str, xml_text: str, max_age_hours: int) -> list[dict]:
    out: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    # 解析失败必须冒泡给 fetch_rss_items，才能在子源账本中明确记 failed；
    # 空但合法的 feed 仍是 ok/0 行，避免把自然无新闻误判为故障。
    root = ET.fromstring(xml_text)
    # RSS <item> 与 Atom <entry> 都覆盖
    items = root.findall(".//item") or root.findall(
        ".//{http://www.w3.org/2005/Atom}entry")
    for item in items:
        title = (item.findtext("title")
                 or item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not link:
            le = item.find("{http://www.w3.org/2005/Atom}link")
            if le is not None:
                link = (le.get("href") or "").strip()
        pub = (item.findtext("pubDate")
               or item.findtext("{http://www.w3.org/2005/Atom}updated")
               or item.findtext("{http://www.w3.org/2005/Atom}published") or "").strip()
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
            "source": f"rss:{name.lower()}",
            "title": title, "url": link, "event_time": event_time,
            "symbols": _extract_symbols(title),
            "severity": _severity(title), "tags": _tags(title),
            "level": "B",
            "raw": {"feed": name, "pubDate": pub},
        })
    return out


def fetch_rss_items(feeds=None, max_age_hours: int = 24,
                    errors: list[str] | None = None,
                    outcomes: list[dict] | None = None,
                    retry_timeout: int = 6,
                    retry_delay: float = 0.5) -> list[dict]:
    """errors（2026-07-07 可观测性）：调用方传入 list 时，每个失败 feed 的简短原因
    （feed 名 + HTTP 码/超时等，逐条截 150 字）append 进去——供 collect() 把 err 带回
    news_collect → ledger.collection_runs.err（degraded 行也必须带明细，
    无法区分限流/网络）。多源部分失败是常态：err 是「哪些 feed 挂了」的清单，
    成功 feed 不受影响。不传则行为同旧版（只打 stderr）。"""
    feeds = feeds or DEFAULT_FEEDS
    items: list[dict] = []
    results: list[dict] = []
    for idx, (name, url) in enumerate(feeds):
        source_id = f"rss:{name.lower()}"
        if idx > 0:
            time.sleep(0.4)  # 跨域限速
        try:
            parsed = _parse_feed(name, _fetch(url), max_age_hours)
            results.append({
                "id": source_id, "name": name, "endpoint": url,
                "status": "ok", "items": parsed, "attempts": 1,
            })
        except Exception as e:  # noqa: BLE001
            results.append({
                "id": source_id, "name": name, "endpoint": url,
                "status": "failed", "items": [], "attempts": 1,
                "err": f"{type(e).__name__}: {e}"[:150],
            })

    # 两遍式有界重试：先让全部发布方各获一次机会，再只重试失败子源。
    # 这样一个慢/坏端点不会阻止其后的健康源留下独立证据；第二次仍失败才记账。
    failed_indexes = [
        index for index, result in enumerate(results)
        if result["status"] == "failed"
    ]
    for retry_index, index in enumerate(failed_indexes):
        result = results[index]
        if retry_index > 0 or retry_delay > 0:
            time.sleep(max(0.0, retry_delay))
        try:
            retry_text, retry_transport, transport_attempts = _fetch_retry(
                result["endpoint"], timeout=retry_timeout)
            parsed = _parse_feed(result["name"], retry_text, max_age_hours)
            result.update({
                "status": "ok", "items": parsed, "attempts": 2,
                "recovered_after_retry": True,
                "transport": retry_transport,
                "transport_attempts": 1 + transport_attempts,
            })
            result.pop("err", None)
        except Exception as retry_error:  # noqa: BLE001
            try:
                parsed, transport, publisher_attempts = (
                    _fetch_official_publisher_fallback(
                        result["name"], result["endpoint"], max_age_hours,
                    )
                )
                result.update({
                    "status": "ok", "items": parsed, "attempts": 2,
                    "recovered_after_retry": True,
                    "recovered_after_publisher_fallback": True,
                    "transport": transport,
                    # initial urllib + retry urllib + two failed httpx attempts
                    # + publisher attempts
                    "transport_attempts": 4 + publisher_attempts,
                })
                result.pop("err", None)
            except Exception as publisher_error:  # noqa: BLE001
                if result["endpoint"] in OFFICIAL_PUBLISHER_FALLBACKS:
                    detail = (
                        f"feed={type(retry_error).__name__}: {retry_error}; "
                        f"publisher={type(publisher_error).__name__}: "
                        f"{publisher_error}"
                    )
                else:
                    detail = f"{type(retry_error).__name__}: {retry_error}"
                result.update({"attempts": 2, "err": detail[:150]})

    for result in results:
        parsed = result.pop("items")
        items.extend(parsed)
        result.pop("name", None)
        if result["status"] == "failed":
            print(
                f"[WARN] {result['id']} fetch failed after retry: "
                f"{result.get('err')}",
                file=sys.stderr,
            )
            if errors is not None:
                errors.append(
                    f"{result['id']}: {result.get('err')}"[:150])
        result["fetched"] = len(parsed)
        if outcomes is not None:
            outcomes.append(result)
    return items


def collect(db_path: str, max_age_hours: int = 24, apply: bool = False) -> dict:
    fetch_errs: list[str] = []
    outcomes: list[dict] = []
    items = fetch_rss_items(
        max_age_hours=max_age_hours, errors=fetch_errs, outcomes=outcomes)
    err_txt = "; ".join(fetch_errs)[:150] if fetch_errs else None
    retry_recovered = sum(
        1 for row in outcomes if row.get("recovered_after_retry"))
    if not apply:
        out = {"ok": True, "dry_run": True, "fetched": len(items),
               "subsources": outcomes, "retry_recovered": retry_recovered,
               "sample": [{"t": it["title"][:70], "et": it["event_time"],
                           "sym": it["symbols"], "sev": it["severity"]}
                          for it in items[:8]]}
        if err_txt:
            out["err"] = err_txt
        return out
    res = news_writer.write_news(items, db_path)
    res["fetched"] = len(items)
    res["subsources"] = outcomes
    res["retry_recovered"] = retry_recovered
    if err_txt and not res.get("err"):
        res["err"] = err_txt
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 RSS news adapter → news_writer")
    ap.add_argument("--db", default=str(news_writer.DEFAULT_NEWS_DB))
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    args = ap.parse_args()
    res = collect(args.db, max_age_hours=args.hours, apply=args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
