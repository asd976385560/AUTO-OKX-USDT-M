# -*- coding: utf-8 -*-
"""Bounded current-cycle recovery for order-book and recent-trade payloads.

The frozen ``collect_market_features.py`` imports these call signatures.  A
small production wrapper monkey-patches only those two transport references,
keeping every frozen research dependency byte-identical while giving the
runtime one fresh-client retry over the exact missing set.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, Sequence

import _okx_http as okx


INITIAL_TIMEOUT_SECONDS = 35.0
COLD_RETRY_TIMEOUT_SECONDS = 25.0
COLD_RETRY_DELAY_SECONDS = 0.75
MINIMUM_COVERAGE = 0.99
MAXIMUM_FETCH_PHASES = 2

_TRANSPORT: dict[str, dict[str, Any]] = {}
_TRANSPORT_LOCK = threading.Lock()


def _safe_batch(
    symbols: Sequence[str],
    *,
    path: str,
    params: Callable[[str], dict[str, str]],
    post: Callable[[list], Any],
    timeout_s: float,
    request_retries: int,
    outcomes: dict[str, dict],
) -> dict:
    syms = list(symbols)
    try:
        return okx._batch(
            syms,
            lambda _symbol: path,
            params,
            post,
            batch_timeout_s=timeout_s,
            request_retries=request_retries,
            outcomes=outcomes,
        )
    except Exception as exc:  # noqa: BLE001
        for symbol in syms:
            outcomes[symbol] = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
        return {}


def _recover(
    symbols: Sequence[str],
    *,
    source: str,
    path: str,
    params: Callable[[str], dict[str, str]],
    post: Callable[[list], Any],
    valid: Callable[[Any], bool],
) -> dict:
    syms = [str(symbol) for symbol in symbols if str(symbol or "").strip()]
    if not syms:
        return {}
    initial_outcomes: dict[str, dict] = {}
    initial = _safe_batch(
        syms,
        path=path,
        params=params,
        post=post,
        timeout_s=INITIAL_TIMEOUT_SECONDS,
        request_retries=2,
        outcomes=initial_outcomes,
    )
    selected = {
        symbol: initial.get(symbol)
        for symbol in syms
        if valid(initial.get(symbol))
    }
    initial_valid = len(selected)
    missing = [symbol for symbol in syms if symbol not in selected]
    retry_requested = initial_valid / len(syms) < MINIMUM_COVERAGE
    retry_outcomes: dict[str, dict] = {}
    recovered = 0
    if retry_requested and missing:
        time.sleep(COLD_RETRY_DELAY_SECONDS)
        retry = _safe_batch(
            missing,
            path=path,
            params=params,
            post=post,
            timeout_s=COLD_RETRY_TIMEOUT_SECONDS,
            request_retries=0,
            outcomes=retry_outcomes,
        )
        for symbol in missing:
            value = retry.get(symbol)
            if valid(value):
                selected[symbol] = value
                recovered += 1
    final = {symbol: selected.get(symbol, post([])) for symbol in syms}
    stats = {
        "contract_version": 1,
        "source": source,
        "attempts": 1 + int(retry_requested and bool(missing)),
        "maximum_fetch_phases": MAXIMUM_FETCH_PHASES,
        "historical_retry": False,
        "unbounded_retry": False,
        "initial_timeout_seconds": INITIAL_TIMEOUT_SECONDS,
        "initial_valid": initial_valid,
        "initial_coverage_rate": initial_valid / len(syms),
        "initial_transport_failures": sum(
            not bool(item.get("ok")) for item in initial_outcomes.values()
        ),
        "cold_retry_requested": retry_requested,
        "cold_retry_symbols": len(missing) if retry_requested else 0,
        "cold_retry_timeout_seconds": (
            COLD_RETRY_TIMEOUT_SECONDS if retry_requested else 0.0
        ),
        "cold_retry_transport_failures": sum(
            not bool(item.get("ok")) for item in retry_outcomes.values()
        ),
        "recovered_after_cold_retry": recovered,
        "final_valid": len(selected),
        "final_coverage_rate": len(selected) / len(syms),
    }
    with _TRANSPORT_LOCK:
        _TRANSPORT[source] = stats
    return final


def fetch_orderbooks_batch_sync(
    symbols: Sequence[str], depth: int = 50
) -> dict:
    depth = max(1, min(int(depth), 400))
    return _recover(
        symbols,
        source="orderbooks",
        path="/api/v5/market/books",
        params=lambda symbol: {"instId": symbol, "sz": str(depth)},
        post=lambda data: (data[0] if data else {}),
        valid=lambda value: bool(
            isinstance(value, dict) and value.get("bids") and value.get("asks")
        ),
    )


def fetch_recent_trades_batch_sync(
    symbols: Sequence[str], limit: int = 500
) -> dict:
    limit = max(1, min(int(limit), 500))
    return _recover(
        symbols,
        source="recent_trades",
        path="/api/v5/market/trades",
        params=lambda symbol: {"instId": symbol, "limit": str(limit)},
        post=lambda data: data,
        valid=lambda value: bool(isinstance(value, list) and value),
    )


def transport_snapshot() -> dict[str, dict[str, Any]]:
    with _TRANSPORT_LOCK:
        return copy.deepcopy(_TRANSPORT)
