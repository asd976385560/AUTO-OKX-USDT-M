# -*- coding: utf-8 -*-
"""V2.0 §6 —— Odaily 星球日报快讯 adapter（确定性抓取 + 规整 → news_writer 落库）。

实测可达端点（2026-06-27 重探，复活 odaily）：
  GET https://web-api.odaily.news/newsflash/page?page=1&pageSize=16
  → code=200，data.list 返回 ~20 条真实快讯（每条含 id/title/description/
    publishTimestamp(unix ms)/isImportant/tags/newsUrl）。

为何此端点而非旧 registry 记录的 www.odaily.news/v1/openapi/feeds：
  - 站点已迁 Next.js App Router（RSC）；www.odaily.news/v1/openapi/* 全返 SPA HTML，
    api.odaily.news/v1/openapi/* 全返 {code:404,"No endpoint"}。
  - 真实 XHR base 在页面 chunk 里写死 baseURL=https://web-api.odaily.news，
    快讯分页路径 /newsflash/page（GET，相对该 base）。

与 news_rss 一致的契约（红线）：
  - **写库走 news_writer**（禁手写 INSERT，红线④）。
  - **event_time = publishTimestamp(ms) 转 UTC+8 字符串**（红线②；缺则 NULL，禁伪 now）。
  - 标题/正文抽 ticker + 中文币名映射 → symbols → news_writer 落 news_events_index。
  - severity/tags = 规则标签（确定性，非 LLM 判断，红线⑩）。

本模块只「取 + 确定性规整」，**不塞 LLM**；情绪/影响的真判断归 analyst。
零模型名（红线①）。
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

_COLLECTORS = str(Path(__file__).resolve().parents[1])  # .\collectors
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)
import news_writer  # noqa: E402
try:
    from ._news_http import fetch_text as _fetch_text_alternate  # type: ignore
except ImportError:
    from _news_http import fetch_text as _fetch_text_alternate  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
SOURCE_ID = "odaily"

# 实测工作端点（base 来自页面 chunk baseURL；路径来自 newsflash/page chunk）
ENDPOINT = "https://web-api.odaily.news/newsflash/page"
DETAIL_URL_FMT = "https://www.odaily.news/zh-CN/newsflash/{id}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REQ_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.odaily.news/",
    "Origin": "https://www.odaily.news",
}

# 币种抽取：英文 ticker（词边界，避免 ON/ARB 等误命中普通词）
_COINS = [
    "BTC", "BITCOIN", "ETH", "ETHEREUM", "SOL", "SOLANA", "XRP", "RIPPLE",
    "BNB", "DOGE", "DOGECOIN", "ADA", "CARDANO", "AVAX", "LINK", "CHAINLINK",
    "TRX", "TRON", "TON", "DOT", "POLKADOT", "MATIC", "POLYGON", "SHIB",
    "LTC", "LITECOIN", "BCH", "UNI", "AAVE", "ARB", "ARBITRUM", "OP",
    "OPTIMISM", "SUI", "APT", "APTOS", "INJ", "SEI", "TIA", "PEPE", "WIF",
    "NEAR", "FIL", "ATOM", "ETC", "XLM", "ICP", "HBAR", "RNDR", "RENDER",
    "HYPE", "ZEC", "LIT",
]
_COIN_TO_SYM = {
    "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL", "RIPPLE": "XRP",
    "DOGECOIN": "DOGE", "CARDANO": "ADA", "CHAINLINK": "LINK", "TRON": "TRX",
    "POLKADOT": "DOT", "POLYGON": "MATIC", "LITECOIN": "LTC", "ARBITRUM": "ARB",
    "OPTIMISM": "OP", "APTOS": "APT", "RENDER": "RNDR",
}
_COIN_RE = re.compile(
    r"\b(" + "|".join(sorted(_COINS, key=len, reverse=True)) + r")\b", re.I)
# $TICKER 形式（$BTC / $HYPE）
_DOLLAR_RE = re.compile(r"\$([A-Za-z]{2,10})\b")
# 中文币名 → SYM（确定性字典）
_CN_COIN_TO_SYM = {
    "比特币": "BTC", "以太坊": "ETH", "以太币": "ETH", "索拉纳": "SOL",
    "瑞波": "XRP", "瑞波币": "XRP", "狗狗币": "DOGE", "狗狗": "DOGE",
    "莱特币": "LTC", "波场": "TRX", "波卡": "DOT", "艾达币": "ADA",
}

# 规则标签（确定性，中英双语关键词）
_TAG_RULES = [
    ("hack", re.compile(
        r"(hack|exploit|breach|drain|stolen|攻击|被盗|漏洞|清算|爆仓|rug)", re.I)),
    ("regulatory", re.compile(
        r"(SEC|CFTC|DOJ|lawsuit|regulat|court|ban|sue|fine|settle|监管|调查|"
        r"诉讼|起诉|罚款|禁令|合规|参议员|游说)", re.I)),
    ("listing", re.compile(
        r"(listing|delist|launch|debut|上线|上币|下架|首发|发行|开盘)", re.I)),
    ("etf", re.compile(r"(ETF|inflow|outflow|净流入|净流出|流入|流出)", re.I)),
    ("macro", re.compile(
        r"(Fed|inflation|CPI|rate cut|FOMC|GDP|美联储|通胀|降息|加息|非农|利率)", re.I)),
    ("whale", re.compile(
        r"(whale|million|billion|巨鲸|鲸鱼|增持|减持|买入|卖出|出售|质押|存入|空单|多单|做空|做多)",
        re.I)),
]
# severity：high / medium 关键词（中英双语）
_SEV_HIGH = re.compile(
    r"(hack|exploit|breach|drain|stolen|SEC|CFTC|lawsuit|ban|crash|plunge|"
    r"攻击|被盗|漏洞|黑客|暴跌|崩盘|清算|爆仓|起诉|诉讼|禁令|调查)", re.I)
_SEV_MED = re.compile(
    r"(listing|launch|partnership|upgrade|fork|rate cut|FOMC|ETF|"
    r"上线|上币|发行|合作|升级|降息|加息|净流入|净流出)", re.I)


def _fetch(page: int = 1, page_size: int = 16, timeout: int = 15) -> dict:
    url = f"{ENDPOINT}?page={page}&pageSize={page_size}"
    req = urllib.request.Request(url, headers=REQ_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _fetch_alternate(page: int = 1, page_size: int = 16,
                     timeout: int = 6) -> dict:
    """Retry the same publisher URL through the bounded alternate TLS chain."""
    url = f"{ENDPOINT}?page={page}&pageSize={page_size}"
    text = _fetch_text_alternate(
        url, timeout=float(timeout), headers=REQ_HEADERS)
    return json.loads(text)


def _strip_html(s: str) -> str:
    """去 HTML 标签 + 反转义实体（description 是 <p>…</p> 富文本）。"""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_symbols(text: str) -> list[str]:
    found: list[str] = []

    def _add(sym: str) -> None:
        inst = f"{sym}-USDT-SWAP"
        if inst not in found:
            found.append(inst)

    t = text or ""
    # 英文 ticker / 全称
    for m in _COIN_RE.finditer(t):
        tok = m.group(1).upper()
        _add(_COIN_TO_SYM.get(tok, tok))
    # $TICKER
    for m in _DOLLAR_RE.finditer(t):
        tok = m.group(1).upper()
        if tok in _COINS or tok in _COIN_TO_SYM:
            _add(_COIN_TO_SYM.get(tok, tok))
    # 中文币名
    for cn, sym in _CN_COIN_TO_SYM.items():
        if cn in t:
            _add(sym)
    return found


def _severity(text: str, is_important: bool = False) -> str:
    if _SEV_HIGH.search(text or ""):
        return "high"
    if _SEV_MED.search(text or ""):
        return "medium"
    # isImportant 标记 → 至少 medium（站点编辑判定的重点快讯）
    if is_important:
        return "medium"
    return "low"


def _tags(text: str) -> list[str]:
    return [name for name, rx in _TAG_RULES if rx.search(text or "")]


def _event_time(publish_ms) -> str | None:
    """publishTimestamp(unix ms) → UTC+8 字符串；缺/非法 → None（禁伪 now）。"""
    if publish_ms in (None, 0, ""):
        return None
    try:
        ts = int(publish_ms)
    except (TypeError, ValueError):
        return None
    # 已知是毫秒；防御性：>1e12 视为毫秒
    if ts > 1_000_000_000_000:
        ts = ts / 1000.0
    try:
        return datetime.fromtimestamp(ts, CST).strftime(TS_FMT)
    except (OSError, OverflowError, ValueError):
        return None


def _parse(payload: dict, max_age_hours: int) -> list[dict]:
    out: list[dict] = []
    if not isinstance(payload, dict):
        raise ValueError("odaily payload is not an object")
    if payload.get("code") != 200:
        raise ValueError(
            f"odaily code={payload.get('code')} msg={payload.get('msg')}")
    data = payload.get("data") or {}
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("odaily payload missing data.list")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for it in items:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        nid = it.get("id")
        body = _strip_html(it.get("description") or "")
        event_time = _event_time(it.get("publishTimestamp"))
        # 时效过滤（仅当能拿到 event_time 才裁；缺 time 不裁，保留交 writer）
        if event_time is not None:
            try:
                dt = datetime.strptime(event_time, TS_FMT).replace(tzinfo=CST)
                if dt < cutoff:
                    continue
            except ValueError:
                pass
        # 抽币：标题 + 正文（正文常含更明确币名）
        scan = f"{title} {body}"
        url = DETAIL_URL_FMT.format(id=nid) if nid is not None else None
        out.append({
            "source": SOURCE_ID,
            "title": title,
            "url": url,
            "event_time": event_time,
            "symbols": _extract_symbols(scan),
            "severity": _severity(scan, bool(it.get("isImportant"))),
            "tags": _tags(scan),
            "level": "B",
            "raw": {
                "id": nid,
                "isImportant": it.get("isImportant"),
                "publishTimestamp": it.get("publishTimestamp"),
                "newsUrl": it.get("newsUrl"),
                "newsUrlType": it.get("newsUrlType"),
                "tags": it.get("tags"),
                "description": it.get("description"),
            },
        })
    return out


def fetch_items(page_size: int = 16, max_age_hours: int = 24,
                errors: list[str] | None = None,
                retry_timeout: int = 6,
                retry_stats: dict | None = None) -> list[dict]:
    """errors（2026-07-07 可观测性）：调用方传入 list 时，fetch 失败的简短原因
    （HTTP 码/超时等，截 150 字）append 进去——供 collect() 把 err 带回
    news_collect → ledger.collection_runs.err（degraded 行也必须带明细，
    无法区分限流/网络）。不传则行为同旧版（只打 stderr）。"""
    last_error: Exception | None = None
    for attempt in (1, 2):
        if attempt == 2:
            time.sleep(0.5)
        try:
            fetch = _fetch if attempt == 1 else _fetch_alternate
            payload = fetch(
                page=1, page_size=page_size,
                timeout=(15 if attempt == 1 else retry_timeout),
            )
            parsed = _parse(payload, max_age_hours)
            if retry_stats is not None:
                retry_stats.update({
                    "attempts": attempt,
                    "recovered_after_retry": attempt == 2,
                    "transport": (
                        "urllib" if attempt == 1 else "alternate_http"),
                })
            return parsed
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    message = f"{type(last_error).__name__}: {last_error}"[:150]
    print(f"[WARN] odaily fetch failed after retry: {message}", file=sys.stderr)
    if errors is not None:
        errors.append(f"fetch: {message}"[:150])
    if retry_stats is not None:
        retry_stats.update({"attempts": 2, "final_failed": True})
    return []


def collect(db_path: str, page_size: int = 16, max_age_hours: int = 24,
            apply: bool = False) -> dict:
    fetch_errs: list[str] = []
    retry_stats: dict = {}
    items = fetch_items(page_size=page_size, max_age_hours=max_age_hours,
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
    res = news_writer.write_news(items, db_path)
    res["fetched"] = len(items)
    res["retry_stats"] = retry_stats
    if err_txt and not res.get("err"):
        res["err"] = err_txt
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 Odaily 快讯 adapter → news_writer")
    ap.add_argument("--db", default=str(news_writer.DEFAULT_NEWS_DB))
    ap.add_argument("--page-size", type=int, default=16)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    args = ap.parse_args()
    res = collect(args.db, page_size=args.page_size, max_age_hours=args.hours,
                  apply=args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
