# -*- coding: utf-8 -*-
"""OKX Support 公共端点客户端。

公告接口与行情接口分离，避免改变前向评估已锁定的 ``_okx_http.py``。
域名、代理、重试和响应校验继续复用同一官方 HTTP 基础设施；本模块额外按
Support API 的 5 次/2 秒限制做 0.5 秒串行节流。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import _okx_http

_SOURCES = str(Path(__file__).resolve().parents[1] / "collectors" / "sources")
if _SOURCES not in sys.path:
    sys.path.insert(0, _SOURCES)
import _news_http  # noqa: E402


_INTERVAL = float(os.environ.get("OKX_HTTP_ANNOUNCEMENTS_INTERVAL", "0.5"))
_LOCK = threading.Lock()
_LAST = 0.0


def _support_throttle() -> None:
    global _LAST
    with _LOCK:
        wait = _INTERVAL - (time.monotonic() - _LAST)
        if wait > 0:
            time.sleep(wait)
        _LAST = time.monotonic()


def _schannel_get_data(
    request_url: str,
    params: dict | None,
    timeout: float,
    *,
    transport_stats: dict | None = None,
) -> list:
    """Use Windows Schannel once for the exact failed official URL."""
    query = urlencode(params or {}, doseq=True)
    url = f"{request_url}?{query}" if query else request_url
    text = _news_http._fetch_text_schannel(
        url,
        timeout=float(timeout),
        headers={
            "User-Agent": "okx-cex-auto/1.0",
            "accept": "application/json",
        },
        proxy=_okx_http._PROXY,
    )
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("OKX support response is not an object")
    code = str(payload.get("code", "0"))
    if code not in ("0", ""):
        raise RuntimeError(
            f"OKX code={code} msg={payload.get('msg')}"
        )
    data = payload.get("data")
    if data is not None and not isinstance(data, list):
        raise ValueError("OKX support data is not a list")
    if transport_stats is not None:
        transport_stats["schannel_fallback_successes"] = int(
            transport_stats.get("schannel_fallback_successes", 0)
        ) + 1
    return data or []


def _get(path: str, params: dict | None, request_timeout_s: float | None,
         transport_stats: dict | None = None) -> list:
    _support_throttle()
    with _okx_http._client() as client:
        return _okx_http._get_data(
            client,
            path,
            params,
            deadline=_okx_http._deadline_from_timeout(request_timeout_s),
            transport_fallback=lambda url, query, timeout: _schannel_get_data(
                url,
                query,
                timeout,
                transport_stats=transport_stats,
            ),
        )


def fetch_support_announcement_types_sync(
    request_timeout_s: float | None = None,
    transport_stats: dict | None = None,
) -> list[dict]:
    """返回当前站点可用的公告类别。"""
    return _get(
        "/api/v5/support/announcement-types",
        None,
        request_timeout_s,
        transport_stats,
    )


def fetch_support_announcements_sync(
    ann_type: str | None = None,
    page: int = 1,
    request_timeout_s: float | None = None,
    transport_stats: dict | None = None,
) -> dict:
    """返回指定公告类别的一页官方公告。"""
    params: dict = {"page": str(max(1, int(page)))}
    if ann_type:
        params["annType"] = str(ann_type)
    rows = _get(
        "/api/v5/support/announcements",
        params,
        request_timeout_s,
        transport_stats,
    )
    if rows and isinstance(rows[0], dict):
        first = rows[0]
        return {
            "details": list(first.get("details") or []),
            "totalPage": str(first.get("totalPage") or "0"),
        }
    return {"details": [], "totalPage": "0"}
