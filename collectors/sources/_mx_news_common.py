# -*- coding: utf-8 -*-
"""妙想 news-search adapters 的共享确定性取数与规整逻辑。

本模块不写库；调用方统一经 collectors/news_writer.py 落库。凭证只从
MX_APIKEY 环境变量读取，生产 wrapper 负责从受控配置注入。
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from typing import Any

import httpx

ENDPOINT = "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search"
DEFAULT_TIMEOUT_SEC = 8.0

MX_QUERY = "加密货币最新消息"
GEO_QUERIES = (
    "地缘局势最新消息",
    "中东局势最新动态",
    "中美关系最新消息",
    "俄乌战争最新进展",
)

_HIGH = (
    "暴涨", "暴跌", "崩盘", "重大", "破纪录", "历史新高", "急跌", "ETF",
    "降息", "加息", "战争", "军事", "冲突", "制裁", "核", "导弹",
    "经济危机", "金融风险", "黑天鹅", "主权债务", "革命", "政变", "紧急状态",
)
_SYMBOLS = {
    "BTC": ("BTC", "比特币", "Bitcoin"),
    "ETH": ("ETH", "以太坊", "Ethereum"),
    "SOL": ("SOL", "Solana"),
    "OKB": ("OKB", "OKX平台币", "欧易平台币"),
    "DOGE": ("DOGE", "狗狗币", "Dogecoin"),
    "TRUMP": ("TRUMP", "Trump"),
    "ALLO": ("ALLO", "Allo"),
}


def api_key() -> str | None:
    value = os.environ.get("MX_APIKEY")
    return value.strip() if value and value.strip() else None


def search(query: str, *, key: str, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> list[dict]:
    with httpx.Client(trust_env=False, timeout=timeout_sec) as client:
        response = client.post(
            ENDPOINT,
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"query": query},
        )
        response.raise_for_status()
        payload = response.json()
    code = (payload or {}).get("code")
    if code not in (None, 0, "0"):
        message = str(
            (payload or {}).get("message")
            or (payload or {}).get("msg")
            or "unknown business error"
        ).strip()
        raise RuntimeError(f"MX business code={code}: {message[:110]}")
    rows = (
        ((payload or {}).get("data") or {})
        .get("data", {})
        .get("llmSearchResponse", {})
        .get("data")
        or []
    )
    return [row for row in rows if isinstance(row, dict)]


def _event_time(value: Any) -> str | None:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return None


def _symbols(text: str) -> list[str]:
    found: list[str] = []
    for code, words in _SYMBOLS.items():
        if any(
            re.search(
                rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])",
                text,
                re.IGNORECASE,
            )
            if word.isascii()
            else word in text
            for word in words
        ):
            found.append(f"{code}-USDT-SWAP")
    return found


def normalize(row: dict, *, source: str, fingerprint_prefix: str,
              tags: list[str]) -> dict | None:
    code = str(row.get("code") or "").strip()
    title = str(row.get("title") or "").strip()
    if not code or not title:
        return None
    content = str(row.get("content") or "")
    level = "A" if any(word in title for word in _HIGH) else "B"
    symbols = _symbols(f"{title} {content}")
    return {
        "source": source,
        "title": title,
        "url": row.get("jumpUrl"),
        "event_time": _event_time(row.get("date")),
        "symbols": symbols,
        "symbol": symbols[0] if symbols else None,
        "level": level,
        "severity": "high" if level == "A" else "medium",
        "tags": tags,
        "raw": row,
        # source-specific 稳定指纹，避免相同事件重复写入。
        "dedupe_hash": hashlib.sha1(
            f"{fingerprint_prefix}|{code}".encode("utf-8")
        ).hexdigest(),
    }
