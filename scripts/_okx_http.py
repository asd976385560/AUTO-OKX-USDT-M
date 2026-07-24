# -*- coding: utf-8 -*-
r"""OKX 公共行情 HTTP 客户端。httpx，**无鉴权**（公共端点）。

对齐消费者事实契约：
    collect_data:  fetch_tickers_all_sync() / fetch_candles_batch_sync(syms, bar, limit=60)
                   / fetch_funding_rates_batch_sync(syms) / fetch_open_interest_all_sync()
    collect_market_features:
                   fetch_orderbooks_batch_sync(syms, depth=50)
                   / fetch_recent_trades_batch_sync(syms, limit=500)
    collect_slow:  fetch_candles_batch_sync(...) / fetch_instruments_sync("SWAP")

网络代理由 `OKX_PROXY_URL` 显式配置；本模块使用 `trust_env=False`，不隐式继承代理。
鉴权类（账户/下单）走 _okxcli（okx CLI 直连），不在本模块。

返回形态（OKX `data` 数组原样，与旧消费者一致）：
  - tickers/instruments → list[dict]
  - candles → {sym: [ [ts,o,h,l,c,vol,...], ... ]}
  - funding → {instId: {fundingRate, nextFundingRate, fundingTime, ...}}
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional, Sequence

import httpx

_BASE = os.environ.get("OKX_HTTP_BASE", "https://www.okx.com")
_PROXY = os.environ.get("OKX_PROXY_URL") or None
_TIMEOUT = float(os.environ.get("OKX_HTTP_TIMEOUT", "30"))
_WORKERS = int(os.environ.get("OKX_HTTP_WORKERS", "8"))
# 限速：按端点各一桶（OKX 公共行情限频是 per-endpoint per IP，不是跨端点全局）。
# 旧实现用单个全局锁把所有端点串到 ~9 req/s，致 funding 387 次 + candles 387 次被迫串行各 ~43s。
# 2026-06-30 改：每个 path 独立令牌桶——同端点串行、不同端点可并发各走自己的间隔：
#   candles /market/candles：OKX 40 次/2s=20/s → 0.055s(~18.2/s) 留 ~10% 余量（2026-07-16
#     默认 0.055；可用 OKX_HTTP_CANDLES_INTERVAL 覆盖）；
#   funding /public/funding-rate：OKX 20 次/2s=10/s → 0.11s(~9/s)；其余默认 0.11s。
# 仍受 _get_data 的 429 退避兜底（真撞限频自动重试）。env 可逐端点覆盖。
_MIN_INTERVAL = float(os.environ.get("OKX_HTTP_MIN_INTERVAL", "0.11"))
_ENDPOINT_INTERVAL = {
    "/api/v5/market/candles": float(os.environ.get("OKX_HTTP_CANDLES_INTERVAL", "0.055")),
    "/api/v5/public/funding-rate": float(os.environ.get("OKX_HTTP_FUNDING_INTERVAL", "0.11")),
}

_BUCKETS: dict[str, list] = {}
_BUCKETS_LOCK = threading.Lock()


def _bucket(path: str) -> list:
    with _BUCKETS_LOCK:
        b = _BUCKETS.get(path)
        if b is None:
            b = [threading.Lock(), [0.0]]
            _BUCKETS[path] = b
        return b


def _throttle(path: str) -> None:
    """每端点独立令牌桶：同端点串行限速、不同端点互不阻塞（可并发）。"""
    interval = _ENDPOINT_INTERVAL.get(path, _MIN_INTERVAL)
    lock, last = _bucket(path)
    with lock:
        now = time.monotonic()
        wait = interval - (now - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.monotonic()


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=_BASE, trust_env=False, proxy=_PROXY, timeout=_TIMEOUT,
        headers={"User-Agent": "okx-cex-auto/1.0", "accept": "application/json"},
    )


def _get_data(client: httpx.Client, path: str, params: Optional[dict] = None,
              retries: int = 2) -> list:
    """GET 公共端点 -> data 数组。code!=0 / HTTP 错误 / 429 退避重试。"""
    last: Optional[Exception] = None
    for i in range(retries + 1):
        _throttle(path)
        try:
            r = client.get(path, params=params)
            r.raise_for_status()
            j = r.json()
            code = str(j.get("code", "0")) if isinstance(j, dict) else "0"
            if code not in ("0", ""):
                raise RuntimeError(f"OKX code={code} msg={j.get('msg')}")
            data = j.get("data") if isinstance(j, dict) else j
            return data or []
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < retries:
                time.sleep(0.3 * (i + 1))
    raise RuntimeError(f"okx GET {path} {params or ''} failed: {last}")


def _batch(symbols: Sequence[str], path_fn, params_fn, post_fn) -> dict:
    """共享 client + 线程池并发取每个 symbol；单 symbol 失败置默认（不拖垮整批）。"""
    out: dict[str, Any] = {}
    syms = [s for s in symbols if s]
    if not syms:
        return out
    with _client() as c:
        with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
            futs = {ex.submit(_get_data, c, path_fn(s), params_fn(s)): s for s in syms}
            for f in as_completed(futs):
                s = futs[f]
                try:
                    out[s] = post_fn(f.result())
                except Exception:  # noqa: BLE001
                    out[s] = post_fn([])
    return out


# ---------------------------------------------------------------------------
def fetch_tickers_all_sync() -> list[dict]:
    """所有 SWAP ticker（单次调用返全量）。"""
    with _client() as c:
        return _get_data(c, "/api/v5/market/tickers", {"instType": "SWAP"})


def fetch_instruments_sync(inst_type: str = "SWAP") -> list[dict]:
    """SWAP instruments（ctVal/lotSz/minSz 等）。"""
    with _client() as c:
        return _get_data(c, "/api/v5/public/instruments", {"instType": inst_type})


def fetch_candles_batch_sync(symbols: Sequence[str], bar: str = "1H",
                             limit: int = 60) -> dict:
    """{sym: [candle 数组]}（OKX data 原样，倒序新→旧）。"""
    return _batch(
        symbols,
        lambda s: "/api/v5/market/candles",
        lambda s: {"instId": s, "bar": bar, "limit": str(limit)},
        lambda data: data,
    )


def fetch_funding_rates_batch_sync(symbols: Sequence[str]) -> dict:
    """{instId: funding dict}（每个请求 symbol 都有键；无数据=空 dict）。

    2026-07-15（D1 性能批）：主路径改单次批量 `instId=ANY`（实测一次调用返全市场
    ~505 行/2.6s，字段同逐币端点），替代 407 次逐 symbol × 0.11s 限速地板 ≈45s——
    该地板是 fast_collect 最大耗时项，也是 fail-slow 网络窗里 407 次调用被批量放大
    致 240s cron kill 的最大尾部风险面。任何异常回退旧逐 symbol 并发路径（fail-safe，
    返回形态两条路径完全一致）。"""
    syms = [s for s in symbols if s]
    if not syms:
        return {}
    try:
        with _client() as c:
            rows = _get_data(c, "/api/v5/public/funding-rate", {"instId": "ANY"})
        by_id = {r.get("instId"): r for r in rows
                 if isinstance(r, dict) and r.get("instId")}
        if not by_id:
            raise RuntimeError("funding instId=ANY returned no rows")
        return {s: by_id.get(s, {}) for s in syms}
    except Exception:  # noqa: BLE001 — 批量端点异常时回退逐币老路径
        return _batch(
            symbols,
            lambda s: "/api/v5/public/funding-rate",
            lambda s: {"instId": s},
            lambda data: (data[0] if data else {}),
        )


def fetch_open_interest_all_sync(inst_type: str = "SWAP") -> dict:
    """全市场 OI，返回 ``{instId: row}``。

    OKX 公共端点支持仅传 instType 一次取全量；字段含 oi/oiCcy/oiUsd/ts。
    """
    with _client() as c:
        rows = _get_data(c, "/api/v5/public/open-interest", {"instType": inst_type})
    return {
        row.get("instId"): row
        for row in rows
        if isinstance(row, dict) and row.get("instId")
    }


def fetch_orderbooks_batch_sync(symbols: Sequence[str], depth: int = 50) -> dict:
    """批量取订单簿快照，默认每侧 50 档。"""
    depth = max(1, min(int(depth), 400))
    return _batch(
        symbols,
        lambda s: "/api/v5/market/books",
        lambda s: {"instId": s, "sz": str(depth)},
        lambda data: (data[0] if data else {}),
    )


def fetch_recent_trades_batch_sync(symbols: Sequence[str], limit: int = 500) -> dict:
    """批量取最近逐笔成交样本（最多 500 条/币）。"""
    limit = max(1, min(int(limit), 500))
    return _batch(
        symbols,
        lambda s: "/api/v5/market/trades",
        lambda s: {"instId": s, "limit": str(limit)},
        lambda data: data,
    )


# candles 的 instId 形参在 _batch 用 symbol 本身，funding 同；但 OKX 返回的 data 内
# 已含 instId，consumer 用外层 key 即可。下面便于命令行自检：
def _selftest() -> int:
    import sys, json
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    t = fetch_tickers_all_sync()
    print(f"tickers: {len(t)} (sample instId={t[0].get('instId') if t else None})")
    ins = fetch_instruments_sync("SWAP")
    print(f"instruments: {len(ins)}")
    syms = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    fr = fetch_funding_rates_batch_sync(syms)
    print(f"funding: {{k: v.get('fundingRate') for ...}} = "
          + json.dumps({k: v.get("fundingRate") for k, v in fr.items()}, ensure_ascii=False))
    oi = fetch_open_interest_all_sync("SWAP")
    print(f"open_interest: {len(oi)}")
    cd = fetch_candles_batch_sync(syms, "1H", 5)
    print(f"candles: {{sym: n}} = " + json.dumps({k: len(v) for k, v in cd.items()}))
    books = fetch_orderbooks_batch_sync(syms, 50)
    print("books: " + json.dumps({k: len(v.get("bids") or []) for k, v in books.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
