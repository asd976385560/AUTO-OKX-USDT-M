# -*- coding: utf-8 -*-
"""V2.0 §6 —— BlockBeats（律动 theblockbeats.news）中文快讯 adapter。

确定性抓取 BlockBeats 公开快讯 open-api → 规整 → news_writer 落库。镜像 news_rss.py
结构（_fetch / _extract_symbols / _severity / _tags / _parse / fetch_items / collect /
main），与 RSS adapter 唯一区别是源是 JSON 快讯（非 RSS/XML），且 title/正文为中文，需
中文币名映射（比特币->BTC 等）。

**当前端点状态（2026-06-27 实测，经 wrapper 代理）**：
  open-api 公开快讯端点活着但返空——
    GET https://api.theblockbeats.news/v1/open-api/open-flash?size=20&type=push&lang=cn
    → HTTP 200 application/json `{"status":0,"message":"","data":[]}`（status=0 即成功，
      但 data 永远 []）。穷尽 type(push/news/all/0-3/flash)×size(5-100)×page×
      lang(cn/en/zh/zh-cn)×max_time/last_id×GET/POST 全部 data[]=0。
  非 open 的 /v1/open-api/flash 需有效 key（返 `无效的key`）。
  www.theblockbeats.news 被代理 MITM（自签证书 / lander 跳转），站内前端 API 不可达。
  → 结论：端点结构正确、健康存活，但运营方对无 key 访问清空/封了数据。本 adapter 解析逻辑
    已就绪，**端点一旦恢复返数据即自动产出**；当前 fetch 为 0 条。

本模块只「取 + 确定性规整」，不塞 LLM（红线⑩）；情绪/影响判断归 analyst。
event_time 由源 unix 时间戳转 UTC+8 字符串，缺则 NULL（红线②，禁伪 now）。
写库走 news_writer.write_news（禁手写 INSERT，红线④）。零模型名（红线①）。
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

_COLLECTORS = str(Path(__file__).resolve().parents[1])  # ./collectors
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)
import news_writer  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
SOURCE_ID = "blockbeats"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 公开快讯端点（验证最规范的结构；恢复返数据即自动产出）。
DEFAULT_ENDPOINT = (
    "https://api.theblockbeats.news/v1/open-api/open-flash"
    "?size=30&type=push&lang=cn"
)

# 英文 ticker（词边界匹配，避免 ON/ARB 误命中）
_COINS = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "TRX",
    "TON", "DOT", "MATIC", "SHIB", "LTC", "BCH", "UNI", "AAVE", "ARB", "OP",
    "SUI", "APT", "INJ", "SEI", "TIA", "PEPE", "WIF", "NEAR", "FIL", "ATOM",
    "ETC", "XLM", "ICP", "HBAR", "RNDR", "ORDI", "JUP", "STX", "RUNE",
]
_COIN_RE = re.compile(r"\b(" + "|".join(sorted(_COINS, key=len, reverse=True)) + r")\b", re.I)
# $TICKER 形式（$BTC / $PEPE）
_DOLLAR_RE = re.compile(r"\$([A-Za-z]{2,10})\b")
# 中文币名 → ticker
_CN_COINS = {
    "比特币": "BTC", "以太坊": "ETH", "以太": "ETH", "索拉纳": "SOL", "瑞波": "XRP",
    "瑞波币": "XRP", "狗狗币": "DOGE", "狗狗": "DOGE", "莱特币": "LTC", "波卡": "DOT",
    "波场": "TRX", "艾达币": "ADA", "卡尔达诺": "ADA", "链克": "LINK", "柴犬": "SHIB",
    "雪崩": "AVAX", "币安币": "BNB", "门罗币": "XMR", "稳定币": None,
}


def _fetch(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.theblockbeats.news/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _extract_symbols(text: str) -> list[str]:
    found: list[str] = []
    text = text or ""

    def _add(sym: str | None) -> None:
        if not sym:
            return
        inst = f"{sym.upper()}-USDT-SWAP"
        if inst not in found:
            found.append(inst)

    # 中文币名
    for cn, sym in _CN_COINS.items():
        if cn in text:
            _add(sym)
    # $TICKER
    for m in _DOLLAR_RE.finditer(text):
        _add(m.group(1))
    # 裸英文 ticker（词边界）
    for m in _COIN_RE.finditer(text):
        _add(m.group(1))
    return found


# severity：critical/high 中英关键词（确定性，非 LLM）
_SEV_HIGH = re.compile(
    r"(黑客|被盗|攻击|漏洞|清算|暴跌|暴涨|崩盘|跳水|SEC|起诉|诉讼|禁令|查封|监管|罚款|"
    r"\bhack\b|\bexploit\b|\bbreach\b|\bstolen\b|\bdrain\b|\blawsuit\b|\bban\b|"
    r"\bSEC\b|\bcrash\b|\bplunge\b|\bsurge\b)", re.I)
_SEV_MED = re.compile(
    r"(上线|上币|首发|发布|升级|合作|分叉|空投|降息|加息|减半|"
    r"\blisting\b|\blist(s|ed)?\b|\blaunch\b|\bupgrade\b|\bpartnership\b|"
    r"\bfork\b|\bairdrop\b|\bFOMC\b)", re.I)

# 规则标签（确定性，中英）
_TAG_RULES = [
    ("hack", re.compile(r"(黑客|被盗|攻击|漏洞|盗取|\bhack\b|\bexploit\b|\bbreach\b|"
                        r"\bdrain\b|\bstolen\b|\brug\b)", re.I)),
    ("regulatory", re.compile(r"(监管|起诉|诉讼|禁令|罚款|查封|合规|法院|\bSEC\b|"
                              r"\blawsuit\b|\bregulat|\bcourt\b|\bban\b|\bCFTC\b|"
                              r"\bDOJ\b)", re.I)),
    ("listing", re.compile(r"(上线|上币|首发|下架|\blisting\b|\blist(s|ed)?\b|"
                           r"\bdelist\b|\blaunch\b|\bdebut\b)", re.I)),
    ("etf", re.compile(r"(ETF|净流入|净流出|\bETF\b|\binflow\b|\boutflow\b)", re.I)),
    ("macro", re.compile(r"(美联储|降息|加息|通胀|CPI|非农|GDP|\bFed\b|\binflation\b|"
                         r"\bCPI\b|\bFOMC\b|\brate cut\b)", re.I)),
    ("whale", re.compile(r"(巨鲸|大户|转入|转出|增持|减持|\bwhale\b|\bmillion\b|"
                         r"\bbillion\b|\btransfer\b)", re.I)),
]


def _severity(text: str) -> str:
    if _SEV_HIGH.search(text or ""):
        return "high"
    if _SEV_MED.search(text or ""):
        return "medium"
    return "low"


def _tags(text: str) -> list[str]:
    return [name for name, rx in _TAG_RULES if rx.search(text or "")]


def _to_event_time(val) -> str | None:
    """源给 unix 秒/毫秒时间戳 → UTC+8 字符串；缺/不可解析 → None（禁伪 now）。"""
    if val is None or val == "":
        return None
    try:
        n = int(val)
    except (ValueError, TypeError):
        # 可能已是 'YYYY-MM-DD HH:MM:SS' 形式
        s = str(val).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", s):
            return s.replace("T", " ")[:19]
        return None
    if n <= 0:
        return None
    if n > 1e12:  # 毫秒
        n //= 1000
    try:
        dt = datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.astimezone(CST).strftime(TS_FMT)


def _parse_flash(body: str, max_age_hours: int) -> list[dict]:
    out: list[dict] = []
    try:
        j = json.loads(body)
    except json.JSONDecodeError as e:
        print(f"[WARN] {SOURCE_ID} JSON parse error: {e}", file=sys.stderr)
        return out
    data = j.get("data") if isinstance(j, dict) else None
    # data 可能直接是 list，或嵌在 data.data / data.list
    if isinstance(data, dict):
        for k in ("data", "list", "items", "rows"):
            if isinstance(data.get(k), list):
                data = data.get(k)
                break
    if not isinstance(data, list):
        if isinstance(j, dict) and j.get("status") not in (0, 200, None):
            print(f"[WARN] {SOURCE_ID} status={j.get('status')} "
                  f"message={j.get('message')!r}", file=sys.stderr)
        return out

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for it in data:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or it.get("content")
                 or it.get("abstract") or "").strip()
        if not title:
            continue
        url = (it.get("link") or it.get("url") or "").strip() or None
        if not url and it.get("id"):
            url = f"https://www.theblockbeats.news/flash/{it.get('id')}"
        raw_ts = (it.get("create_time") or it.get("add_time")
                  or it.get("created_at") or it.get("time")
                  or it.get("publish_time"))
        event_time = _to_event_time(raw_ts)
        # 时效裁剪（仅当能解析出时间时；时间缺则保留，由 writer 去重）
        if event_time is not None:
            try:
                et_dt = datetime.strptime(event_time, TS_FMT).replace(tzinfo=CST)
                if et_dt < cutoff:
                    continue
            except ValueError:
                pass
        # 抽币 + 标签从 title + abstract 一起判
        blob = " ".join(str(it.get(k) or "") for k in ("title", "abstract", "content"))
        out.append({
            "source": SOURCE_ID,
            "title": title,
            "url": url,
            "event_time": event_time,
            "symbols": _extract_symbols(blob),
            "severity": _severity(blob),
            "tags": _tags(blob),
            "level": "B",
            "raw": it,
        })
    return out


def fetch_items(endpoint: str = DEFAULT_ENDPOINT, max_age_hours: int = 24,
                errors: list[str] | None = None) -> list[dict]:
    """errors（2026-07-07 可观测性）：调用方传入 list 时，fetch 失败的简短原因
    （HTTP 码/超时等，截 150 字）append 进去——供 collect() 把 err 带回
    news_collect → ledger.collection_runs.err（degraded 行也必须带明细，
    无法区分限流/网络）。不传则行为同旧版（只打 stderr）。"""
    try:
        body = _fetch(endpoint)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[WARN] {SOURCE_ID} fetch failed: {e}", file=sys.stderr)
        if errors is not None:
            errors.append(f"fetch: {e}"[:150])
        return []
    except Exception as e:  # noqa: BLE001
        print(f"[ERR] {SOURCE_ID}: {e}", file=sys.stderr)
        if errors is not None:
            errors.append(f"{type(e).__name__}: {e}"[:150])
        return []
    time.sleep(0.1)
    return _parse_flash(body, max_age_hours)


def collect(db_path: str, endpoint: str = DEFAULT_ENDPOINT,
            max_age_hours: int = 24, apply: bool = False) -> dict:
    fetch_errs: list[str] = []
    items = fetch_items(endpoint=endpoint, max_age_hours=max_age_hours,
                        errors=fetch_errs)
    err_txt = "; ".join(fetch_errs)[:150] if fetch_errs else None
    if not apply:
        out = {"ok": True, "dry_run": True, "fetched": len(items),
               "endpoint": endpoint,
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
    ap = argparse.ArgumentParser(
        description="V2.0 BlockBeats 快讯 adapter → news_writer")
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
