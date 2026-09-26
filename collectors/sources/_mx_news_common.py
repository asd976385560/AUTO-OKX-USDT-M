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

try:
    from . import _coin_names  # noqa: E402  包内加载
except ImportError:  # 裸模块加载（run_okx_python / 单测直接 import）
    import os as _coin_os
    import sys as _coin_sys
    _SOURCES_DIR = _coin_os.path.dirname(_coin_os.path.abspath(__file__))
    if _SOURCES_DIR not in _coin_sys.path:
        _coin_sys.path.insert(0, _SOURCES_DIR)
    import _coin_names  # noqa: E402

ENDPOINT = "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search"
DEFAULT_TIMEOUT_SEC = 8.0

MX_QUERY = "加密货币最新消息"
GEO_QUERIES = (
    "地缘局势最新消息",
    "中东局势最新动态",
    "中美关系最新消息",
    "俄乌战争最新进展",
)


class MXQuotaExceeded(RuntimeError):
    """Non-retryable daily free-quota exhaustion from the MX API."""

_HIGH = (
    "暴涨", "暴跌", "崩盘", "重大", "破纪录", "历史新高", "急跌", "ETF",
    "降息", "加息", "战争", "军事", "冲突", "制裁", "核", "导弹",
    "经济危机", "金融风险", "黑天鹅", "主权债务", "革命", "政变", "紧急状态",
)
# 本源私有补充名（公共表见 _coin_names.py）：ASCII 名整词、中文名子串。
_EXTRA_NAMES = {
    "OKX平台币": "OKB", "欧易平台币": "OKB", "Trump": "TRUMP", "Allo": "ALLO",
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
        error = f"MX business code={code}: {message[:110]}"
        if str(code) == "113":
            raise MXQuotaExceeded(error)
        raise RuntimeError(error)
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
    """文本 → 合约列表：公共登记表（_coin_names）+ 本源私有补充名（OKB/ALLO/TRUMP）。"""
    return _coin_names.extract_symbols(text, extra_names=_EXTRA_NAMES)


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
