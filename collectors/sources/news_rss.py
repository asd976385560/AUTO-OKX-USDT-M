# -*- coding: utf-8 -*-
"""V2.0 §6 —— RSS 新闻 adapter（确定性抓取 + 规整 → news_writer 落库）。

实测可达源（2026-06-26 再探：6 个英文 RSS 全 200 OK）：cointelegraph/decrypt/coindesk/
theblock + cryptopotato/bitcoinist（后两 06-26 扩源）。中文快讯三源（jinse/panews/odaily）
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

_COLLECTORS = str(Path(__file__).resolve().parents[1])  # <PROJECT_ROOT>\collectors
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)
import news_writer  # noqa: E402

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
    # 2026-06-26 扩源：探测连通 200 OK（cryptopotato ~36/bitcoinist ~8 条）；
    # 其余候选(ambcrypto/utoday/cryptoslate/newsbtc) SSL handshake timeout 受阻，未加。
    ("CryptoPotato", "https://cryptopotato.com/feed/"),
    ("Bitcoinist", "https://bitcoinist.com/feed/"),
]

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
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[WARN] {name} RSS parse error: {e}", file=sys.stderr)
        return out
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
                    errors: list[str] | None = None) -> list[dict]:
    """errors（2026-07-07 可观测性）：调用方传入 list 时，每个失败 feed 的简短原因
    （feed 名 + HTTP 码/超时等，逐条截 150 字）append 进去——供 collect() 把 err 带回
    news_collect → ledger.collection_runs.err（degraded 行也必须带明细，
    无法区分限流/网络）。多源部分失败是常态：err 是「哪些 feed 挂了」的清单，
    成功 feed 不受影响。不传则行为同旧版（只打 stderr）。"""
    feeds = feeds or DEFAULT_FEEDS
    items: list[dict] = []
    for idx, (name, url) in enumerate(feeds):
        if idx > 0:
            time.sleep(0.4)  # 跨域限速
        try:
            items += _parse_feed(name, _fetch(url), max_age_hours)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"[WARN] {name} fetch failed: {e}", file=sys.stderr)
            if errors is not None:
                errors.append(f"{name}: {e}"[:150])
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] {name}: {e}", file=sys.stderr)
            if errors is not None:
                errors.append(f"{name}: {type(e).__name__}: {e}"[:150])
    return items


def collect(db_path: str, max_age_hours: int = 24, apply: bool = False) -> dict:
    fetch_errs: list[str] = []
    items = fetch_rss_items(max_age_hours=max_age_hours, errors=fetch_errs)
    err_txt = "; ".join(fetch_errs)[:150] if fetch_errs else None
    if not apply:
        out = {"ok": True, "dry_run": True, "fetched": len(items),
               "sample": [{"t": it["title"][:70], "et": it["event_time"],
                           "sym": it["symbols"], "sev": it["severity"]}
                          for it in items[:8]]}
        if err_txt:
            out["err"] = err_txt
        return out
    res = news_writer.write_news(items, db_path)
    res["fetched"] = len(items)
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
