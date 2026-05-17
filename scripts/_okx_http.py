# -*- coding: utf-8 -*-
"""
_okx_http.py —— OKX public market data via direct HTTP (no CLI subprocess).
Safe for repeated asyncio.run() calls from the same module.

Public endpoints (no signing required):
  - GET /api/v5/market/candles         → K-lines (per symbol)
  - GET /api/v5/market/tickers           → 24h tickers (ALL contracts in ONE call!)
  - GET /api/v5/public/instruments      → instrument list
  - GET /api/v5/public/funding-rate     → funding rate (per symbol, public!)
"""
from __future__ import annotations

import asyncio
import httpx
import os
import time
from typing import Any

# DNS hijack workaround: use pilot proxy IP directly
_PILOT_IP = os.environ.get("OKX_PILOT_IP", "8.212.1.102")
OKX_PUBLIC_BASE = f"https://{_PILOT_IP}/api/v5"
_PROXY_URL = os.environ.get("OKX_PROXY_URL", None)
_SSL_VERIFY = os.environ.get("OKX_SSL_VERIFY", "0") in ("1", "true", "True")
_HOST_HEADER = "www.okx.com"

# Concurrency limit: OKX public rate limit = 20 req/2s per IP
# We use 8 to stay well under the limit (leaves headroom for bursts)
_MAX_CONCURRENT = 8

# ─── HTTP client (created fresh per asyncio.run, not module-level) ─────────

class _Client:
    """Thin wrapper around httpx.AsyncClient with semaphore concurrency control."""

    def __init__(self, sem: asyncio.Semaphore | None = None) -> None:
        self._sem = sem or asyncio.Semaphore(_MAX_CONCURRENT)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "_Client":
        self._client = httpx.AsyncClient(
            proxy=_PROXY_URL,
            timeout=25.0,
            verify=_SSL_VERIFY,
            trust_env=False,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    async def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._sem:
            for attempt in range(3):
                try:
                    resp = await self._client.get(
                        url,
                        params=params,
                        headers={"Host": _HOST_HEADER},
                    )
                    resp.raise_for_status()
                    return resp.json()
                except Exception as exc:
                    if attempt < 2 and ("429" in str(exc) or "Too Many Requests" in str(exc)):
                        wait = 1.0 + attempt * 1.5  # 1s, 2.5s
                        await asyncio.sleep(wait)
                        continue
                    raise


def _ensure_ok(data: dict[str, Any]) -> None:
    if data.get("code") != "0":
        raise RuntimeError(f"OKX API error {data.get('code')}: {data.get('msg', data)}")


# ─── Public API (async) ────────────────────────────────────────────────────

async def fetch_instruments(inst_type: str = "SWAP") -> list[dict[str, Any]]:
    """Fetch all instruments of given type. One HTTP call."""
    async with _Client() as client:
        data = await client.get(f"{OKX_PUBLIC_BASE}/public/instruments",
                               {"instType": inst_type})
    _ensure_ok(data)
    return data.get("data") or []


async def fetch_tickers_all() -> list[dict[str, Any]]:
    """
    Fetch 24h ticker data for ALL USDT-M SWAP contracts.
    SINGLE API CALL — no looping!
    Returns list of tickers.
    """
    async with _Client() as client:
        data = await client.get(f"{OKX_PUBLIC_BASE}/market/tickers",
                               {"instType": "SWAP"})
    _ensure_ok(data)
    return data.get("data") or []


async def fetch_candles(inst_id: str, bar: str, limit: int = 100) -> list[list[Any]]:
    """
    Fetch K-line candles for one symbol.
    Returns list of [ts, open, high, low, close, vol, ...], newest first.
    """
    async with _Client() as client:
        data = await client.get(f"{OKX_PUBLIC_BASE}/market/candles",
                               {"instId": inst_id, "bar": bar, "limit": limit})
    _ensure_ok(data)
    return data.get("data") or []


async def fetch_candles_batch(
    symbols: list[str], bar: str, limit: int = 100,
) -> dict[str, list[list[Any]]]:
    """
    Fetch candles for multiple symbols concurrently.
    Returns {symbol: [candle, ...]}.
    Uses a single shared HTTP client for connection reuse.
    """
    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async def one(client: _Client, sym: str) -> tuple[str, list[list[Any]]]:
        try:
            async with sem:
                data = await client.get(
                    f"{OKX_PUBLIC_BASE}/market/candles",
                    {"instId": sym, "bar": bar, "limit": limit},
                )
            _ensure_ok(data)
            return sym, data.get("data") or []
        except Exception as exc:
            print(f"  [warn] {sym}: {exc}")
            return sym, []

    async with _Client() as client:
        results = await asyncio.gather(*[one(client, s) for s in symbols])
    return dict(results)


async def fetch_funding_rate(inst_id: str) -> dict[str, Any]:
    """Fetch current funding rate for one symbol (public endpoint)."""
    async with _Client() as client:
        data = await client.get(f"{OKX_PUBLIC_BASE}/public/funding-rate",
                               {"instId": inst_id})
    _ensure_ok(data)
    items = data.get("data") or []
    return items[0] if items else {}


async def fetch_funding_rates_batch(
    symbols: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch funding rates for multiple symbols concurrently.
    Uses a single shared HTTP client for connection reuse."""
    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async def one(client: _Client, sym: str) -> tuple[str, dict[str, Any]]:
        try:
            async with sem:
                data = await client.get(
                    f"{OKX_PUBLIC_BASE}/public/funding-rate",
                    {"instId": sym},
                )
            _ensure_ok(data)
            items = data.get("data") or []
            return sym, items[0] if items else {}
        except Exception as exc:
            print(f"  [warn] funding {sym}: {exc}")
            return sym, {}

    async with _Client() as client:
        results = await asyncio.gather(*[one(client, s) for s in symbols])
    return dict(results)


# ─── Sync wrappers (for use in sync collect_data.py) ───────────────────────

def fetch_tickers_all_sync() -> list[dict[str, Any]]:
    return asyncio.run(fetch_tickers_all())


def fetch_candles_batch_sync(
    symbols: list[str], bar: str, limit: int = 100,
) -> dict[str, list[list[Any]]]:
    return asyncio.run(fetch_candles_batch(symbols, bar, limit))


def fetch_funding_rates_batch_sync(symbols: list[str]) -> dict[str, dict[str, Any]]:
    return asyncio.run(fetch_funding_rates_batch(symbols))


def fetch_instruments_sync(inst_type: str = "SWAP") -> list[dict[str, Any]]:
    return asyncio.run(fetch_instruments(inst_type))


async def _fetch_sentiment_rank(period: str = "24h", limit: int = 50) -> list[dict[str, Any]]:
    """Fetch OKX news sentiment rank. One HTTP call."""
    async with _Client() as client:
        data = await client.get(
            f"{OKX_PUBLIC_BASE}/news/sentiment-rank",
            {"period": period, "limit": str(limit)},
        )
    _ensure_ok(data)
    return data.get("data") or []




if __name__ == "__main__":
    async def bench():
        t0 = time.time()
        tickers = await fetch_tickers_all()
        t1 = time.time()
        usdt_swap = [t for t in tickers if t.get("instId", "").endswith("-USDT-SWAP")]
        print(f"tickers: {t1-t0:.2f}s  total={len(tickers)}  USDT-M={len(usdt_swap)}")

        symbols = [t["instId"] for t in usdt_swap]
        t0 = time.time()
        klines = await fetch_candles_batch(symbols, "15m", 60)
        t1 = time.time()
        ok = sum(1 for v in klines.values() if v)
        print(f"292 klines 15m: {t1-t0:.2f}s  {ok}/{len(symbols)} ok")

    asyncio.run(bench())
