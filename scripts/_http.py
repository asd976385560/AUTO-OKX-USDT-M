# -*- coding: utf-8 -*-
r"""通用 HTTP 工具（重建 2026-06-26）。

原件 2026-06-25 ~22:40 丢失（见 memory v2-incident-20260625-missing-modules）。
对齐 collect_slow.py 事实契约：
    from _http import TokenBucket, get_json, load_coingecko_key, load_fred_key, make_client

用于宏观数据源（FRED / CoinGecko / DefiLlama / frankfurter）——这些站点经系统代理
（trust_env=True 让 httpx 读 HTTPS_PROXY）可达；OKX 行情走独立 _okx_http（trust_env=False
+ OKX_PROXY_URL）。凭证运行期从 config.md §4 读（env 优先），本模块不硬编码 key（红线 #5）。
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

_CONFIG = Path(os.environ.get("OKX_CONFIG_MD", r"./config.md"))
_UA = "okx-cex-auto/1.0"


class TokenBucket:
    """简单令牌桶限速：rate_per_sec 速率补充、capacity 上限。take() 阻塞到有令牌。"""

    def __init__(self, rate_per_sec: float = 1.0, capacity: float = 2.0):
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._ts = time.monotonic()
        self._lock = threading.Lock()

    def take(self, n: float = 1.0) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._ts) * self.rate)
                self._ts = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = (n - self._tokens) / self.rate if self.rate > 0 else 0.1
                time.sleep(max(deficit, 0.01))


def make_client(timeout: float = 25.0, trust_env: bool = True,
                headers: Optional[dict] = None) -> httpx.Client:
    """宏观源 client：trust_env=True 让 httpx 走系统代理（FRED 等需经 Clash）。"""
    h = {"User-Agent": _UA, "accept": "application/json"}
    if headers:
        h.update(headers)
    return httpx.Client(trust_env=trust_env, timeout=timeout,
                        follow_redirects=True, headers=h)


def get_json(client: httpx.Client, url: str, *, params: Optional[dict] = None,
             headers: Optional[dict] = None, bucket: Optional[TokenBucket] = None,
             timeout: Optional[float] = None) -> Any:
    """GET -> JSON。bucket 非空则先限速。HTTP 错误抛 httpx.HTTPStatusError（调用方 catch）。"""
    if bucket is not None:
        bucket.take()
    kw: dict[str, Any] = {}
    if params is not None:
        kw["params"] = params
    if headers is not None:
        kw["headers"] = headers
    if timeout is not None:
        kw["timeout"] = timeout
    resp = client.get(url, **kw)
    resp.raise_for_status()
    return resp.json()


def _key_from_config(section_re: str) -> str:
    """从 config.md 抠某节的 API Key 单元格（镜像 run_okx_python.ps1 的 MX 正则）。"""
    try:
        txt = _CONFIG.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(section_re, txt, re.S)
    return m.group(1).strip() if m else ""


def load_fred_key() -> str:
    """FRED key：env 优先（FRED_API_KEY/FRED_KEY），否则 config.md §4.1。缺则空串。"""
    for k in ("FRED_API_KEY", "FRED_KEY"):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return _key_from_config(
        r"###\s*4\.1\s*FRED.*?\|\s*API\s*Key\s*\|\s*([^|\s][^|]*?)\s*\|")


def load_coingecko_key() -> str:
    """CoinGecko key：env 优先（COINGECKO_API_KEY/CG_API_KEY），否则 config.md §4.3。缺则空串。"""
    for k in ("COINGECKO_API_KEY", "CG_API_KEY", "COINGECKO_KEY"):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return _key_from_config(
        r"###\s*4\.3\s*CoinGecko.*?\|\s*API\s*Key\s*\|\s*([^|\s][^|]*?)\s*\|")
