# -*- coding: utf-8 -*-
"""Shared HTTP helpers for OKX slow collectors.

Kept deliberately small: synchronous httpx client, simple token bucket,
and config.md key extraction for FRED / CoinGecko.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx


class TokenBucket:
    def __init__(self, rate_per_sec: float = 1.0, capacity: int = 1) -> None:
        self.rate_per_sec = float(rate_per_sec)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.updated_at = time.monotonic()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.updated_at
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
            self.updated_at = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            missing = 1 - self.tokens
            wait_s = missing / max(self.rate_per_sec, 0.000001)
        time.sleep(wait_s)
        with self._lock:
            self.updated_at = time.monotonic()
            self.tokens = max(0.0, self.tokens - 1)


def make_client() -> httpx.Client:
    return httpx.Client(
        trust_env=True,
        timeout=25.0,
        follow_redirects=True,
        headers={"User-Agent": "okx-cex-auto/1.0"},
    )


def get_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    bucket: TokenBucket | None = None,
) -> Any:
    if bucket is not None:
        bucket.wait()
    resp = client.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return resp.json()


def _config_text() -> str:
    path = Path(__file__).resolve().parents[1] / "config.md"
    return path.read_text(encoding="utf-8")


def _extract_table_value(section_title: str, key_label: str) -> str:
    text = _config_text()
    section_match = re.search(rf"###\s+{re.escape(section_title)}.*?(?=\n###\s+|\Z)", text, re.S)
    section = section_match.group(0) if section_match else text
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] == key_label:
            value = cells[1].strip().strip("`")
            if not value or value in {"待填写", "TODO", "placeholder"} or value.startswith("<REDACTED_"):
                raise RuntimeError(f"config.md missing {section_title}/{key_label}")
            return value
    raise RuntimeError(f"config.md missing {section_title}/{key_label}")


def load_fred_key() -> str:
    return _extract_table_value("4.1 FRED（美联储经济数据）", "API Key")


def load_coingecko_key() -> str:
    return _extract_table_value("4.3 CoinGecko（加密市场宏观数据）", "API Key")
