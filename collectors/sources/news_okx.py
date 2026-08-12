# -*- coding: utf-8 -*-
"""V2.0 §6 —— OKX news CLI adapter（important + latest → news_writer）。

挂在 okx-news-rss cron（news_collect registry 驱动），与 RSS/中文快讯同链、
**不进 fast_collect**（避免拖 15m 行情预算）。

2026-07-27：OKX CLI 1.4.2 复核 `okx news latest|important`
可返回数据；registry `okx_news` 由此 adapter 提供（required=false）。

契约（与其它 news_* 一致）：
  - 写库只经 news_writer（禁手写 INSERT）
  - event_time 来自 cTime(ms)→UTC+8；缺则 NULL，禁 fallback now
  - severity/level 由 importance 规则映射（确定性，非 LLM）
  - 失败隔离：本源 fail 不拖垮 news_collect 其它源

零模型名（红线①）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_COLLECTORS = str(Path(__file__).resolve().parents[1])  # ./collectors
_SCRIPTS = str(Path(r"./scripts"))
for _p in (_COLLECTORS, _SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import news_writer  # noqa: E402
from _okxcli import okx_json  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
SOURCE_ID = "okx_news"

# 限时：registry timeout_sec=20；单次 CLI 略短，给 important+latest 留双份
_CLI_TIMEOUT = 12.0
_DEFAULT_IMPORTANT_LIMIT = 20
_DEFAULT_LATEST_LIMIT = 30

_COIN_TO_SWAP = {
    "BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "SOL": "SOL-USDT-SWAP",
    "XRP": "XRP-USDT-SWAP", "BNB": "BNB-USDT-SWAP", "DOGE": "DOGE-USDT-SWAP",
    "ADA": "ADA-USDT-SWAP", "AVAX": "AVAX-USDT-SWAP", "LINK": "LINK-USDT-SWAP",
    "TRX": "TRX-USDT-SWAP", "TON": "TON-USDT-SWAP", "DOT": "DOT-USDT-SWAP",
    "MATIC": "MATIC-USDT-SWAP", "SHIB": "SHIB-USDT-SWAP", "LTC": "LTC-USDT-SWAP",
    "BCH": "BCH-USDT-SWAP", "UNI": "UNI-USDT-SWAP", "AAVE": "AAVE-USDT-SWAP",
    "ARB": "ARB-USDT-SWAP", "OP": "OP-USDT-SWAP", "SUI": "SUI-USDT-SWAP",
    "APT": "APT-USDT-SWAP", "INJ": "INJ-USDT-SWAP", "SEI": "SEI-USDT-SWAP",
    "TIA": "TIA-USDT-SWAP", "PEPE": "PEPE-USDT-SWAP", "WIF": "WIF-USDT-SWAP",
    "NEAR": "NEAR-USDT-SWAP", "FIL": "FIL-USDT-SWAP", "ATOM": "ATOM-USDT-SWAP",
    "ETC": "ETC-USDT-SWAP", "XLM": "XLM-USDT-SWAP", "ICP": "ICP-USDT-SWAP",
    "HBAR": "HBAR-USDT-SWAP", "HYPE": "HYPE-USDT-SWAP", "ZEC": "ZEC-USDT-SWAP",
}


def _ms_to_cst(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        ms = int(str(value))
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(CST)
        return dt.strftime(TS_FMT)
    except (TypeError, ValueError, OSError):
        return None


def _importance_to_level_sev(importance: str | None) -> tuple[str, str]:
    imp = (importance or "").strip().lower()
    if imp == "high":
        return "A", "high"
    if imp == "medium":
        return "B", "medium"
    return "C", "low"


def _symbols_from_item(item: dict) -> list[str]:
    out: list[str] = []
    for ccy in item.get("ccyList") or []:
        c = str(ccy or "").upper().strip()
        if c in _COIN_TO_SWAP:
            out.append(_COIN_TO_SWAP[c])
    # ccySentiments 可能带币
    for s in item.get("ccySentiments") or []:
        if not isinstance(s, dict):
            continue
        c = str(s.get("ccy") or "").upper().strip()
        if c in _COIN_TO_SWAP:
            sym = _COIN_TO_SWAP[c]
            if sym not in out:
                out.append(sym)
    return out


def _sentiment_hint(item: dict) -> str | None:
    """粗极性 hint（可选）；真判断归 analyst。"""
    sents = item.get("ccySentiments") or []
    if not sents:
        return None
    label_map = {"bullish": "bullish", "bearish": "bearish", "neutral": "neutral"}
    labels = []
    for s in sents:
        if not isinstance(s, dict):
            continue
        lab = (s.get("sentiment") or s.get("label") or "").lower()
        if lab in label_map:
            labels.append(label_map[lab])
    if not labels:
        return None
    # 多数表决
    from collections import Counter
    return Counter(labels).most_common(1)[0][0]


def _normalize_payload(payload) -> list[dict]:
    """okx CLI 可能返 {details:[...]} 或 list。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        # 可能是 [ {details: [...]}, ... ] 或直接 details 列表
        out = []
        for el in payload:
            if isinstance(el, dict) and "details" in el:
                out.extend(el.get("details") or [])
            elif isinstance(el, dict) and (el.get("title") or el.get("id")):
                out.append(el)
        return out
    if isinstance(payload, dict):
        return list(payload.get("details") or [])
    return []


def _item_to_news(item: dict) -> dict | None:
    title = (item.get("title") or item.get("summary") or "").strip()
    if not title:
        return None
    level, severity = _importance_to_level_sev(item.get("importance"))
    platforms = item.get("platformList") or []
    source = SOURCE_ID
    if platforms:
        # 保留平台信息在 tags；source 固定 okx_news 便于账本/去重口径
        pass
    symbols = _symbols_from_item(item)
    event_time = _ms_to_cst(item.get("cTime"))
    tags = ["okx_feed"]
    if platforms:
        tags.extend(str(p) for p in platforms[:6])
    return {
        "source": source,
        "title": title[:500],
        "url": item.get("sourceUrl") or item.get("url"),
        "event_time": event_time,
        "symbols": symbols,
        "symbol": symbols[0] if symbols else None,
        "severity": severity,
        "level": level,
        "tags": tags,
        "sentiment": _sentiment_hint(item),
        "raw": {
            "id": item.get("id"),
            "importance": item.get("importance"),
            "platformList": platforms,
            "ccyList": item.get("ccyList"),
            "summary": item.get("summary"),
            "cTime": item.get("cTime"),
        },
    }


def fetch_items(important_limit: int = _DEFAULT_IMPORTANT_LIMIT,
                latest_limit: int = _DEFAULT_LATEST_LIMIT,
                timeout_sec: float = _CLI_TIMEOUT,
                errors: list[str] | None = None,
                retry_stats: dict | None = None) -> list[dict]:
    """拉 important + latest，失败端点在全部首轮结束后各重试一次。

    两遍式顺序避免 important 的长超时直接吃掉 latest 的机会。默认首轮8秒、
    重试6秒，两个端点全失败最坏约28秒，仍在 registry 的30秒源预算内。
    """
    raw_items: list[dict] = []
    failed: list[tuple[str, int, str]] = []
    recovered = 0
    initial_timeout = max(1.0, min(float(timeout_sec), 8.0))
    retry_timeout = max(1.0, min(float(timeout_sec), 6.0))
    for kind, lim in (("important", important_limit), ("latest", latest_limit)):
        try:
            payload = okx_json(
                "news", kind, "--limit", str(lim),
                timeout_sec=initial_timeout, retries=0,
            )
            batch = _normalize_payload(payload)
            raw_items.extend(batch)
        except Exception as e:  # noqa: BLE001
            msg = f"{kind}: {type(e).__name__}: {e}"[:150]
            print(f"[WARN] okx news initial {msg}; retrying", file=sys.stderr)
            failed.append((kind, lim, msg))

    for index, (kind, lim, _initial_error) in enumerate(failed):
        if index == 0:
            time.sleep(0.5)
        try:
            payload = okx_json(
                "news", kind, "--limit", str(lim),
                timeout_sec=retry_timeout, retries=0,
            )
            raw_items.extend(_normalize_payload(payload))
            recovered += 1
        except Exception as e:  # noqa: BLE001
            msg = f"{kind}: {type(e).__name__}: {e}"[:150]
            print(f"[WARN] okx news {msg}", file=sys.stderr)
            if errors is not None:
                errors.append(msg)

    if retry_stats is not None:
        retry_stats.update({
            "initial_failed": len(failed),
            "recovered_after_retry": recovered,
            "final_failed": len(failed) - recovered,
        })

    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "")
        if iid and iid in seen_ids:
            continue
        if iid:
            seen_ids.add(iid)
        news = _item_to_news(it)
        if not news:
            continue
        tkey = news["title"].lower()
        if tkey in seen_titles:
            continue
        seen_titles.add(tkey)
        out.append(news)
    return out


def collect(db_path: str, apply: bool = False,
            important_limit: int = _DEFAULT_IMPORTANT_LIMIT,
            latest_limit: int = _DEFAULT_LATEST_LIMIT,
            timeout_sec: float = _CLI_TIMEOUT) -> dict:
    fetch_errs: list[str] = []
    retry_stats: dict = {}
    items = fetch_items(
        important_limit=important_limit,
        latest_limit=latest_limit,
        timeout_sec=timeout_sec,
        errors=fetch_errs,
        retry_stats=retry_stats,
    )
    err_txt = "; ".join(fetch_errs)[:150] if fetch_errs else None
    if not apply:
        out = {
            "ok": True,
            "dry_run": True,
            "fetched": len(items),
            "retry_stats": retry_stats,
            "sample": [
                {
                    "t": it["title"][:70],
                    "et": it["event_time"],
                    "sym": it.get("symbols"),
                    "sev": it.get("severity"),
                }
                for it in items[:8]
            ],
        }
        if err_txt:
            out["err"] = err_txt
        # 全失败且 0 条 → ok=False 让 news_collect 记 failed
        if err_txt and not items:
            out["ok"] = False
        return out

    if not items and err_txt:
        return {"ok": False, "fetched": 0, "inserted": 0, "err": err_txt}

    res = news_writer.write_news(items, db_path)
    res["fetched"] = len(items)
    res["retry_stats"] = retry_stats
    if err_txt and not res.get("err"):
        res["err"] = err_txt
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 OKX news CLI adapter → news_writer")
    ap.add_argument("--db", default=str(news_writer.DEFAULT_NEWS_DB))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--important-limit", type=int, default=_DEFAULT_IMPORTANT_LIMIT)
    ap.add_argument("--latest-limit", type=int, default=_DEFAULT_LATEST_LIMIT)
    ap.add_argument("--timeout", type=float, default=_CLI_TIMEOUT)
    args = ap.parse_args()
    res = collect(
        args.db,
        apply=args.apply,
        important_limit=args.important_limit,
        latest_limit=args.latest_limit,
        timeout_sec=args.timeout,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
