# -*- coding: utf-8 -*-
"""V2.0 §6 —— 金色财经 (jinse) 快讯 adapter（确定性抓取 + 规整 → news_writer 落库）。

工作端点（2026-06-27 探测）：`https://api.jinse.com.cn/noah/v3/lives?limit=N`
返回 JSON `{news,count,list:[{id,content,content_prefix,created_at,link,grade,...}]}`，
是金色财经「快讯/lives」流（每条一条 flash-news，中文）。

⚠️ 网络事实（务必记录，便于按需判断换源）：
  - api.jinse.cn / api.jinse.com / www.jinse.* / m.jinse.* 一律 SSL UNEXPECTED_EOF /
    handshake timeout（本机经代理到这些 host 的 TLS 被掐断，端点本身无关）。
  - **唯一可达 host = api.jinse.com.cn**（`.com.cn` 顶级域），TLS 正常握手 200 OK。
  采集器只迭代本可达 host；若日后 api.jinse.com.cn 也被掐，全 jinse host 即被网络层封锁。

与 news_rss 同构：
  - 写库走 news_writer（禁手写 INSERT，红线④）。
  - event_time = created_at(unix 秒) 转 UTC+8 字符串（红线②）；缺则 NULL，禁伪 now。
  - 标题抽 ticker / 中文币名 → symbols → news_writer 落 news_events_index。
  - severity/tags = 规则标签（确定性，非 LLM，红线⑩）。

零模型名（红线①）。本模块只「取 + 确定性规整」，情绪/影响真判断归 analyst。
"""
from __future__ import annotations

import argparse
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
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REQ_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.jinse.com.cn",
}

# 唯一可达 host（见模块 docstring 网络事实）。注册表 endpoint 与此一致。
DEFAULT_ENDPOINT = "https://api.jinse.com.cn/noah/v3/lives"
# 快讯落地页前缀（link 为空时回退用 prefix_link + id）
LIVE_LINK_PREFIX = "https://m.jinse.com.cn/lives/"

SOURCE_ID = "jinse"

# ── 币种抽取：英文 ticker（词边界）+ 中文币名映射 ──────────────────────────
_COINS = [
    "BTC", "BITCOIN", "ETH", "ETHEREUM", "SOL", "SOLANA", "XRP", "RIPPLE",
    "BNB", "DOGE", "DOGECOIN", "ADA", "CARDANO", "AVAX", "LINK", "CHAINLINK",
    "TRX", "TRON", "TON", "DOT", "POLKADOT", "MATIC", "POLYGON", "SHIB",
    "LTC", "LITECOIN", "BCH", "UNI", "AAVE", "ARB", "ARBITRUM", "OP",
    "OPTIMISM", "SUI", "APT", "APTOS", "INJ", "SEI", "TIA", "PEPE", "WIF",
    "NEAR", "FIL", "ATOM", "ETC", "XLM", "ICP", "HBAR", "RNDR", "RENDER",
    "HYPE", "ORDI", "BLUR", "JTO", "PYTH", "STX", "RUNE", "FTM",
]
_COIN_TO_SYM = {
    "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL", "RIPPLE": "XRP",
    "DOGECOIN": "DOGE", "CARDANO": "ADA", "CHAINLINK": "LINK", "TRON": "TRX",
    "POLKADOT": "DOT", "POLYGON": "MATIC", "LITECOIN": "LTC", "ARBITRUM": "ARB",
    "OPTIMISM": "OP", "APTOS": "APT", "RENDER": "RNDR",
}
# 边界用「非 ASCII 字母数字」前后视（而非 \b）——中文与 ticker 直接相邻时
# Python \b 失效（CJK 也是 \w，"中BTC和" 无 ASCII 边界），故用 (?<![A-Za-z0-9])
# 显式排除两侧 ASCII 字母数字，CJK/标点/空白/串首尾均算边界。
_COIN_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(_COINS, key=len, reverse=True))
    + r")(?![A-Za-z0-9])", re.I)

# 中文币名 → SYM（子串匹配，确定性 dict）
_ZH_COIN = {
    "比特币": "BTC", "以太坊": "ETH", "以太币": "ETH", "索拉纳": "SOL",
    "瑞波": "XRP", "瑞波币": "XRP", "狗狗币": "DOGE", "狗狗": "DOGE",
    "莱特币": "LTC", "卡尔达诺": "ADA", "波卡": "DOT", "波场": "TRX",
    "柚子": "EOS", "门罗": "XMR", "艾达": "ADA", "雪崩": "AVAX",
    "比特现金": "BCH", "恒星": "XLM", "狗币": "DOGE", "屎币": "SHIB",
    "佩佩": "PEPE",
}

# ── 规则标签（确定性，中英双语关键词）─────────────────────────────────────
_TAG_RULES = [
    ("hack", re.compile(
        r"(hack|exploit|breach|drain|stolen|attack|rug|被盗|攻击|漏洞|黑客|被黑|跑路|盗取)", re.I)),
    ("regulatory", re.compile(
        r"(SEC|lawsuit|regulat|court|ban|sue|fine|settle|CFTC|DOJ|监管|起诉|诉讼|法院|禁止|罚款|合规|制裁|立案)", re.I)),
    ("listing", re.compile(
        r"(listing|lists?|delist|launch|debut|上线|上币|下架|首发|发行|主网上线)", re.I)),
    ("etf", re.compile(r"(ETF|inflow|outflow|净流入|净流出|流入|流出)", re.I)),
    ("macro", re.compile(
        r"(Fed|inflation|CPI|rate cut|FOMC|jobs report|GDP|美联储|通胀|加息|降息|非农|利率|经济数据)", re.I)),
    ("whale", re.compile(
        r"(whale|million|billion|moves?|transfer|巨鲸|鲸鱼|转入|转出|转移|大额|地址)", re.I)),
]
# severity：critical/high → medium → low（中英关键词）
_SEV_HIGH = re.compile(
    r"(hack|exploit|breach|drain|stolen|SEC|lawsuit|ban|crash|plunge|surge|"
    r"ETF approv|被盗|暴跌|崩盘|清算|跳水|起诉|禁止|黑客|暴涨|闪崩|制裁)", re.I)
_SEV_MED = re.compile(
    r"(listing|launch|partnership|upgrade|fork|rate cut|FOMC|上线|上币|"
    r"合作|升级|主网|降息|加息|发行|首发)", re.I)


def _fetch(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=REQ_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _fetch_alternate(url: str, timeout: int = 6) -> str:
    """Retry the same publisher URL through the bounded alternate TLS chain."""
    return _fetch_text_alternate(
        url, timeout=float(timeout), headers=REQ_HEADERS)


def _extract_symbols(text: str) -> list[str]:
    found: list[str] = []
    text = text or ""
    # 英文 ticker / 英文币名（词边界）
    for m in _COIN_RE.finditer(text):
        tok = m.group(1).upper()
        sym = _COIN_TO_SYM.get(tok, tok)
        inst = f"{sym}-USDT-SWAP"
        if inst not in found:
            found.append(inst)
    # 中文币名（子串）
    for zh, sym in _ZH_COIN.items():
        if zh in text:
            inst = f"{sym}-USDT-SWAP"
            if inst not in found:
                found.append(inst)
    return found


def _severity(text: str) -> str:
    if _SEV_HIGH.search(text or ""):
        return "high"
    if _SEV_MED.search(text or ""):
        return "medium"
    return "low"


def _tags(text: str) -> list[str]:
    return [name for name, rx in _TAG_RULES if rx.search(text or "")]


def _event_time_from_unix(ts) -> str | None:
    """created_at(unix 秒) → UTC+8 字符串；缺/非法 → None（禁伪 now，红线②）。"""
    if ts in (None, "", 0, "0"):
        return None
    try:
        sec = int(ts)
        if sec <= 0:
            return None
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.astimezone(CST).strftime(TS_FMT)
    except (ValueError, TypeError, OSError):
        return None


def _parse(payload: str, max_age_hours: int) -> list[dict]:
    out: list[dict] = []
    data = json.loads(payload)
    # lives 端点：list 直挂顶层；timelines 端点：list 在 data 下（兼容两形）
    container = data.get("data") if isinstance(data.get("data"), dict) else data
    rows = container.get("list") if isinstance(container, dict) else None
    if not isinstance(rows, list):
        raise ValueError("jinse payload missing list array")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 快讯标题优先 content_prefix（即【】内摘要），缺则用 content 全文
        title = (str(row.get("content_prefix") or "").strip()
                 or str(row.get("content") or "").strip())
        if not title:
            continue
        content = str(row.get("content") or "").strip()
        item_id = row.get("id")
        # link 常为空 → 回退落地页
        link = str(row.get("link") or "").strip()
        if not link and item_id:
            link = f"{LIVE_LINK_PREFIX}{item_id}.html"
        link = link or None

        created = row.get("created_at")
        # max_age 过滤（按 unix 时间；解析失败的不滤掉，event_time 留 None）
        if created not in (None, "", 0, "0"):
            try:
                dt = datetime.fromtimestamp(int(created), tz=timezone.utc)
                if dt < cutoff:
                    continue
            except (ValueError, TypeError, OSError):
                pass
        event_time = _event_time_from_unix(created)

        # 抽币 / 标签：用全文 content（信息更全），symbols 不限标题
        scan_text = content or title
        out.append({
            "source": SOURCE_ID,
            "title": title,
            "url": link,
            "event_time": event_time,
            "symbols": _extract_symbols(scan_text),
            "severity": _severity(scan_text),
            "tags": _tags(scan_text),
            "level": "B",
            "raw": {
                "id": item_id,
                "content": content,
                "content_prefix": row.get("content_prefix"),
                "grade": row.get("grade"),
                "created_at": created,
                "created_at_zh": row.get("created_at_zh"),
                "link": row.get("link"),
            },
        })
    return out


def fetch_items(endpoint: str = DEFAULT_ENDPOINT, limit: int = 30,
                max_age_hours: int = 24,
                errors: list[str] | None = None,
                retry_timeout: int = 6,
                retry_stats: dict | None = None) -> list[dict]:
    """errors（2026-07-07 可观测性）：调用方传入 list 时，fetch 失败的简短原因
    （HTTP 码/SSL/超时等，截 150 字）append 进去——供 collect() 把 err 带回
    news_collect → ledger.collection_runs.err（degraded 行也必须带明细，
    无法区分限流/TLS 被掐/网络）。不传则行为同旧版（只打 stderr）。"""
    sep = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{sep}limit={limit}"
    last_error: Exception | None = None
    for attempt in (1, 2):
        if attempt == 2:
            time.sleep(0.5)
        try:
            fetch = _fetch if attempt == 1 else _fetch_alternate
            parsed = _parse(
                fetch(url, timeout=(15 if attempt == 1 else retry_timeout)),
                max_age_hours,
            )
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
    print(f"[WARN] jinse fetch failed after retry: {message}", file=sys.stderr)
    if errors is not None:
        errors.append(f"fetch: {message}"[:150])
    if retry_stats is not None:
        retry_stats.update({"attempts": 2, "final_failed": True})
    return []


def collect(db_path: str, endpoint: str = DEFAULT_ENDPOINT, limit: int = 30,
            max_age_hours: int = 24, apply: bool = False) -> dict:
    fetch_errs: list[str] = []
    retry_stats: dict = {}
    items = fetch_items(endpoint=endpoint, limit=limit,
                        max_age_hours=max_age_hours, errors=fetch_errs,
                        retry_stats=retry_stats)
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
    ap = argparse.ArgumentParser(description="V2.0 jinse(金色财经)快讯 adapter → news_writer")
    ap.add_argument("--db", default=str(news_writer.DEFAULT_NEWS_DB))
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    args = ap.parse_args()
    res = collect(args.db, endpoint=args.endpoint, limit=args.limit,
                  max_age_hours=args.hours, apply=args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
