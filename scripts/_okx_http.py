# -*- coding: utf-8 -*-
r"""OKX 公共行情 HTTP 客户端（重建 2026-06-26）。httpx，**无鉴权**（公共端点）。

原件 2026-06-25 ~22:40 丢失（见 memory v2-incident-20260625-missing-modules）。
对齐消费者事实契约：
    collect_data:  fetch_tickers_all_sync() / fetch_candles_batch_sync(syms, bar, limit=60)
                   / fetch_funding_rates_batch_sync(syms) / fetch_open_interest_all_sync()
    collect_market_features:
                   fetch_orderbooks_batch_sync(syms, depth=50)
                   / fetch_recent_trades_batch_sync(syms, limit=500)
                   / fetch_contract_long_short_ratios_batch_sync(syms)
                   / fetch_contract_open_interest_history_batch_sync(syms)
                   / fetch_contract_taker_volumes_batch_sync(syms)
    collect_slow:  fetch_candles_batch_sync(...) / fetch_instruments_sync("SWAP")

连通（[[okx-connectivity-proxy]]）：旧域 `www.okx.com` 本机被 DNS 劫持（→169.254.0.2）；
公共 REST 默认走官方推荐 `openapi.okx.com`，旧域只在原重试预算内回退。httpx 仍须
**trust_env=False + 显式 proxy=OKX_PROXY_URL**（Clash 做远程 DNS 绕开劫持）。
鉴权类（账户/下单）走 _okxcli（okx CLI 直连），不在本模块。

返回形态（OKX `data` 数组原样，与旧消费者一致）：
  - tickers/instruments → list[dict]
  - candles → {sym: [ [ts,o,h,l,c,vol,...], ... ]}
  - funding → {instId: {fundingRate, nextFundingRate, fundingTime, ...}}
"""
from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional, Sequence

import httpx

_OFFICIAL_GLOBAL_BASE = "https://openapi.okx.com"
_LEGACY_GLOBAL_BASE = "https://www.okx.com"


def _resolve_base_urls(
    primary_override: str | None,
    fallback_override: str | None,
) -> tuple[str, ...]:
    """Resolve a bounded, ordered REST endpoint list.

    OKX now recommends ``openapi.okx.com`` for global REST traffic while
    retaining ``www.okx.com`` as a compatible legacy domain.  The legacy host
    is therefore a default network fallback, not a second data source: both
    return the same official API contract and the existing retry count remains
    the total request budget.

    A configured primary (for example a required regional domain) is treated
    as authoritative.  It receives no implicit global fallback; operators must
    explicitly set ``OKX_HTTP_FALLBACK_BASES`` if their registration permits
    another endpoint.
    """
    explicit_primary = str(primary_override or "").strip()
    primary = explicit_primary or _OFFICIAL_GLOBAL_BASE
    if fallback_override is None:
        fallbacks = [] if explicit_primary else [_LEGACY_GLOBAL_BASE]
    else:
        fallbacks = [
            value.strip()
            for value in re.split(r"[;,\s]+", str(fallback_override))
            if value.strip()
        ]

    resolved: list[str] = []
    for value in [primary, *fallbacks]:
        normalized = value.rstrip("/")
        if not normalized.startswith(("https://", "http://")):
            raise ValueError(f"invalid OKX HTTP base URL: {value!r}")
        if normalized not in resolved:
            resolved.append(normalized)
    return tuple(resolved)


_BASE_URLS = _resolve_base_urls(
    os.environ.get("OKX_HTTP_BASE"),
    os.environ.get("OKX_HTTP_FALLBACK_BASES"),
)
_BASE = _BASE_URLS[0]
_PROXY = os.environ.get("OKX_PROXY_URL") or None
_TIMEOUT = float(os.environ.get("OKX_HTTP_TIMEOUT", "30"))
_WORKERS = int(os.environ.get("OKX_HTTP_WORKERS", "8"))
_STATS_WORKERS = int(os.environ.get("OKX_HTTP_STATS_WORKERS", "48"))
# 两个15m合约统计端点会同时启动；各48路会让同一代理瞬时承受约96条
# TLS请求并产生SSL EOF/整批长尾。隔离A/B在同一427币周期证实各12路可在
# 30秒内100%直采，因此使用独立并发上限，不影响整点1H持仓倾向端点。
_CONTRACT_STATS_WORKERS = int(os.environ.get(
    "OKX_HTTP_CONTRACT_STATS_WORKERS", "12"))
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
    # Trading Statistics 的规则是 IP + Instrument ID；每个 symbol 独立限频。
    "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract": float(
        os.environ.get("OKX_HTTP_CONTRACT_STATS_INTERVAL", "0.42")
    ),
    "/api/v5/rubik/stat/contracts/open-interest-history": float(
        os.environ.get("OKX_HTTP_CONTRACT_OI_INTERVAL", "0.22")
    ),
    "/api/v5/rubik/stat/taker-volume-contract": float(
        os.environ.get("OKX_HTTP_CONTRACT_TAKER_INTERVAL", "0.42")
    ),
}

_CONTRACT_STATS_PERIODS = {
    "5m", "15m", "30m", "1H", "2H", "4H",
    "6H", "12H", "1D", "2D", "3D", "5D", "1W", "1M", "3M",
    "6Hutc", "12Hutc", "1Dutc", "2Dutc", "3Dutc", "5Dutc",
    "1Wutc", "1Mutc", "3Mutc",
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


def _throttle(path: str, throttle_key: str | None = None) -> None:
    """按官方限频维度节流。

    普通公共行情按 endpoint 共用桶；明确声明 ``IP + Instrument ID`` 的
    Trading Statistics 端点可传 symbol 键，每币独立桶。这样全宇宙只发每币
    一次请求，不会被错误地串行到 fast_collect 的 40 秒超时之外。
    """
    interval = _ENDPOINT_INTERVAL.get(path, _MIN_INTERVAL)
    bucket_key = f"{path}|{throttle_key}" if throttle_key else path
    lock, last = _bucket(bucket_key)
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
              retries: int = 2, deadline: float | None = None,
              throttle_key: str | None = None) -> list:
    """GET public data with one shared retry/deadline budget.

    Attempts rotate through the configured official domains.  Domain failover
    never adds attempts beyond ``retries + 1`` and never converts an exhausted
    request into usable data, so callers retain their fail-closed semantics.
    """
    last: Optional[Exception] = None
    for i in range(retries + 1):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"okx batch deadline exceeded: {path}")
        _throttle(path, throttle_key)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"okx batch deadline exceeded: {path}")
        try:
            timeout = None
            if deadline is not None:
                timeout = max(0.1, min(_TIMEOUT, deadline - time.monotonic()))
                if timeout <= 0.1 and time.monotonic() >= deadline:
                    raise TimeoutError(f"okx batch deadline exceeded: {path}")
            request_kwargs = {"params": params}
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            base = _BASE_URLS[i % len(_BASE_URLS)]
            request_url = f"{base}{path}" if len(_BASE_URLS) > 1 else path
            r = client.get(request_url, **request_kwargs)
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
                delay = 0.3 * (i + 1)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    delay = min(delay, remaining)
                time.sleep(delay)
    raise RuntimeError(f"okx GET {path} {params or ''} failed: {last}")


def _batch(symbols: Sequence[str], path_fn, params_fn, post_fn,
           batch_timeout_s: float | None = None, *,
           workers: int | None = None, throttle_key_fn=None,
           request_retries: int = 2,
           outcomes: dict[str, dict] | None = None) -> dict:
    """共享 client + 线程池并发取每个 symbol；单 symbol 失败置默认（不拖垮整批）。"""
    out: dict[str, Any] = {}
    syms = [s for s in symbols if s]
    if not syms:
        return out
    deadline = (
        time.monotonic() + max(0.1, float(batch_timeout_s))
        if batch_timeout_s is not None else None
    )
    with _client() as c:
        with ThreadPoolExecutor(max_workers=max(1, workers or _WORKERS)) as ex:
            futs = {
                ex.submit(
                    _get_data, c, path_fn(s), params_fn(s),
                    retries=max(0, int(request_retries)),
                    deadline=deadline,
                    throttle_key=(throttle_key_fn(s)
                                  if throttle_key_fn is not None else None),
                ): s
                for s in syms
            }
            for f in as_completed(futs):
                s = futs[f]
                try:
                    out[s] = post_fn(f.result())
                    if outcomes is not None:
                        outcomes[s] = {"ok": True, "error_type": None}
                except Exception as exc:  # noqa: BLE001
                    out[s] = post_fn([])
                    if outcomes is not None:
                        outcomes[s] = {
                            "ok": False,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        }
    return out


# ---------------------------------------------------------------------------
def _deadline_from_timeout(timeout_s: float | None) -> float | None:
    if timeout_s is None:
        return None
    return time.monotonic() + max(0.1, float(timeout_s))


def fetch_tickers_all_sync(request_timeout_s: float | None = None) -> list[dict]:
    """所有 SWAP ticker（单次调用返全量）。"""
    with _client() as c:
        return _get_data(
            c,
            "/api/v5/market/tickers",
            {"instType": "SWAP"},
            deadline=_deadline_from_timeout(request_timeout_s),
        )


def fetch_instruments_sync(inst_type: str = "SWAP") -> list[dict]:
    """SWAP instruments（ctVal/lotSz/minSz 等）。"""
    with _client() as c:
        return _get_data(c, "/api/v5/public/instruments", {"instType": inst_type})


def fetch_candles_batch_sync(symbols: Sequence[str], bar: str = "1H",
                             limit: int = 60,
                             batch_timeout_s: float | None = None) -> dict:
    """{sym: [candle 数组]}（OKX data 原样，倒序新→旧）。"""
    return _batch(
        symbols,
        lambda s: "/api/v5/market/candles",
        lambda s: {"instId": s, "bar": bar, "limit": str(limit)},
        lambda data: data,
        batch_timeout_s=batch_timeout_s,
    )


def fetch_funding_rates_batch_sync(
    symbols: Sequence[str],
    batch_timeout_s: float | None = None,
) -> dict:
    """{instId: funding dict}（每个请求 symbol 都有键；无数据=空 dict）。

    2026-07-15（D1 性能批）：主路径改单次批量 `instId=ANY`（实测一次调用返全市场
    ~505 行/2.6s，字段同逐币端点），替代 407 次逐 symbol × 0.11s 限速地板 ≈45s——
    该地板是 fast_collect 最大耗时项，也是 fail-slow 网络窗里 407 次调用被批量放大
    致 240s cron kill 的最大尾部风险面。任何异常回退旧逐 symbol 并发路径（fail-safe，
    返回形态两条路径完全一致）。"""
    syms = [s for s in symbols if s]
    if not syms:
        return {}
    deadline = _deadline_from_timeout(batch_timeout_s)
    try:
        with _client() as c:
            rows = _get_data(
                c,
                "/api/v5/public/funding-rate",
                {"instId": "ANY"},
                deadline=deadline,
            )
        by_id = {r.get("instId"): r for r in rows
                 if isinstance(r, dict) and r.get("instId")}
        if not by_id:
            raise RuntimeError("funding instId=ANY returned no rows")
        return {s: by_id.get(s, {}) for s in syms}
    except Exception:  # noqa: BLE001 — 批量端点异常时回退逐币老路径
        remaining = (
            max(0.1, deadline - time.monotonic())
            if deadline is not None else None
        )
        return _batch(
            symbols,
            lambda s: "/api/v5/public/funding-rate",
            lambda s: {"instId": s},
            lambda data: (data[0] if data else {}),
            batch_timeout_s=remaining,
        )


def fetch_open_interest_all_sync(
    inst_type: str = "SWAP",
    request_timeout_s: float | None = None,
) -> dict:
    """全市场 OI，返回 ``{instId: row}``。

    OKX 公共端点支持仅传 instType 一次取全量；字段含 oi/oiCcy/oiUsd/ts。
    """
    with _client() as c:
        rows = _get_data(
            c,
            "/api/v5/public/open-interest",
            {"instType": inst_type},
            deadline=_deadline_from_timeout(request_timeout_s),
        )
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


def fetch_recent_trades_batch_sync(
    symbols: Sequence[str],
    limit: int = 500,
    batch_timeout_s: float | None = None,
    *,
    outcomes: dict[str, dict] | None = None,
) -> dict:
    """批量取最近逐笔成交样本（最多500条/币），可选逐币传输结果。"""
    limit = max(1, min(int(limit), 500))
    return _batch(
        symbols,
        lambda s: "/api/v5/market/trades",
        lambda s: {"instId": s, "limit": str(limit)},
        lambda data: data,
        batch_timeout_s=batch_timeout_s,
        outcomes=outcomes,
    )


def fetch_contract_long_short_ratios_batch_sync(
    symbols: Sequence[str],
    period: str = "1H",
    limit: int = 1,
    batch_timeout_s: float | None = None,
) -> dict:
    """官方合约账户多空比，返回 ``{symbol: [[ts, ratio], ...]}``。

    OKX Trading Statistics 合同允许 FUTURES/SWAP，单次最多100条、最多保留
    1,440条。生产只取最新已闭合1H条目；每个 symbol 是独立限频键。
    """
    if period not in _CONTRACT_STATS_PERIODS:
        raise ValueError(f"unsupported contract statistics period: {period}")
    limit = max(1, min(int(limit), 100))
    path = "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract"
    return _batch(
        symbols,
        lambda _symbol: path,
        lambda symbol: {
            "instId": symbol,
            "period": period,
            "limit": str(limit),
        },
        lambda data: data,
        batch_timeout_s=batch_timeout_s,
        workers=max(_WORKERS, min(64, _STATS_WORKERS)),
        throttle_key_fn=lambda symbol: symbol,
    )


def fetch_contract_open_interest_history_batch_sync(
    symbols: Sequence[str],
    period: str = "15m",
    limit: int = 1,
    batch_timeout_s: float | None = None,
    *,
    request_retries: int = 2,
    outcomes: dict[str, dict] | None = None,
) -> dict:
    """官方单合约持仓量历史，返回 ``{symbol: rows}``。

    行合同为 ``[ts, oi_contracts, oi_ccy, oi_usd]``。Trading Statistics
    以 Instrument ID 为限频维度；调用方每轮对每币仅发一次请求。
    """
    if period not in _CONTRACT_STATS_PERIODS:
        raise ValueError(f"unsupported contract statistics period: {period}")
    limit = max(1, min(int(limit), 100))
    path = "/api/v5/rubik/stat/contracts/open-interest-history"
    return _batch(
        symbols,
        lambda _symbol: path,
        lambda symbol: {
            "instId": symbol,
            "period": period,
            "limit": str(limit),
        },
        lambda data: data,
        batch_timeout_s=batch_timeout_s,
        workers=max(1, min(64, _CONTRACT_STATS_WORKERS)),
        throttle_key_fn=lambda symbol: symbol,
        request_retries=request_retries,
        outcomes=outcomes,
    )


def fetch_contract_taker_volumes_batch_sync(
    symbols: Sequence[str],
    period: str = "15m",
    unit: str = "2",
    limit: int = 1,
    batch_timeout_s: float | None = None,
    *,
    request_retries: int = 2,
    outcomes: dict[str, dict] | None = None,
) -> dict:
    """官方单合约主动买卖量，返回 ``{symbol: rows}``。

    行合同为 ``[ts, sell_volume, buy_volume]``；``unit='2'`` 固定美元口径，
    避免不同合约面值不可比。每个 Instrument ID 每轮仅发一次请求。
    """
    if period not in _CONTRACT_STATS_PERIODS:
        raise ValueError(f"unsupported contract statistics period: {period}")
    unit = str(unit)
    if unit not in {"0", "1", "2"}:
        raise ValueError(f"unsupported contract taker volume unit: {unit}")
    limit = max(1, min(int(limit), 100))
    path = "/api/v5/rubik/stat/taker-volume-contract"
    return _batch(
        symbols,
        lambda _symbol: path,
        lambda symbol: {
            "instId": symbol,
            "period": period,
            "unit": unit,
            "limit": str(limit),
        },
        lambda data: data,
        batch_timeout_s=batch_timeout_s,
        workers=max(1, min(64, _CONTRACT_STATS_WORKERS)),
        throttle_key_fn=lambda symbol: symbol,
        request_retries=request_retries,
        outcomes=outcomes,
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
