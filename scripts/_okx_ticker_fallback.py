# -*- coding: utf-8 -*-
"""Bounded official aggregate and single-instrument ticker recovery.

This recovery helper is intentionally separate from ``_okx_http.py`` because
that shared transport is a SHA-256-frozen dependency of the v7 research
challenger.  Production aggregate-ticker recovery can therefore evolve
without invalidating the immutable research boundary.
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlencode

import _okx_http as okx

_SOURCES = str(Path(__file__).resolve().parents[1] / "collectors" / "sources")
if _SOURCES not in sys.path:
    sys.path.insert(0, _SOURCES)
import _news_http  # noqa: E402


PATH = "/api/v5/market/ticker"
ALL_TICKERS_PATH = "/api/v5/market/tickers"


def fetch_tickers_all_schannel_sync(
    request_url: str,
    params: dict | None,
    timeout: float,
    *,
    transport: dict[str, Any] | None = None,
) -> list[dict]:
    """Fetch the exact failed aggregate-ticker URL with Windows Schannel.

    This is a transport-only fallback callback for ``_okx_http._get_data``.
    It accepts only the configured official aggregate SWAP endpoint, preserves
    the configured proxy and TLS verification, and shares the caller's
    original current-cycle deadline.
    """
    allowed_urls = {
        f"{base}{ALL_TICKERS_PATH}" for base in okx._BASE_URLS
    }
    normalized_params = {
        str(key): str(value) for key, value in (params or {}).items()
    }
    if request_url not in allowed_urls:
        raise ValueError("Schannel ticker fallback rejected non-official URL")
    if normalized_params != {"instType": "SWAP"}:
        raise ValueError("Schannel ticker fallback rejected unexpected query")

    if transport is not None:
        transport["schannel_fallback_requested"] = int(
            transport.get("schannel_fallback_requested", 0)
        ) + 1
    query = urlencode(normalized_params, doseq=True)
    url = f"{request_url}?{query}"
    try:
        text = _news_http._fetch_text_schannel(
            url,
            timeout=float(timeout),
            headers={
                "User-Agent": "okx-cex-auto/1.0",
                "accept": "application/json",
            },
            proxy=okx._PROXY,
        )
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("OKX ticker response is not an object")
        code = str(payload.get("code", "0"))
        if code not in ("0", ""):
            raise RuntimeError(
                f"OKX code={code} msg={payload.get('msg')}"
            )
        data = payload.get("data")
        if data is not None and not isinstance(data, list):
            raise ValueError("OKX ticker data is not a list")
        rows = data or []
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("OKX ticker data contains a non-object row")
    except Exception as exc:  # noqa: BLE001 - bounded transport telemetry
        if transport is not None:
            errors = transport.setdefault(
                "schannel_fallback_error_types", []
            )
            if isinstance(errors, list):
                errors.append(type(exc).__name__)
        raise

    if transport is not None:
        transport["schannel_fallback_successes"] = int(
            transport.get("schannel_fallback_successes", 0)
        ) + 1
    return rows


def _request_ticker(client, base: str, symbol: str, deadline: float) -> dict:
    if time.monotonic() >= deadline:
        raise TimeoutError("official single-ticker deadline exceeded")
    okx._throttle(PATH)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("official single-ticker deadline exceeded")
    response = client.get(
        f"{base}{PATH}",
        params={"instId": symbol},
        timeout=max(0.1, min(okx._TIMEOUT, remaining)),
    )
    response.raise_for_status()
    payload = response.json()
    code = str(payload.get("code", "0")) if isinstance(payload, dict) else "0"
    if code not in ("0", ""):
        message = payload.get("msg") if isinstance(payload, dict) else ""
        raise RuntimeError(f"OKX code={code} msg={message}")
    rows = payload.get("data") if isinstance(payload, dict) else payload
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    if not row.get("instId"):
        raise RuntimeError("official single-ticker returned no usable row")
    return row


def _batch_on_domain(
    symbols: Sequence[str],
    *,
    base: str,
    deadline: float,
    outcomes: dict[str, dict] | None,
) -> dict[str, dict]:
    result: dict[str, dict] = {}
    with okx._client() as client:
        with ThreadPoolExecutor(
            max_workers=max(1, min(6, okx._WORKERS))
        ) as executor:
            futures = {
                executor.submit(
                    _request_ticker, client, base, symbol, deadline
                ): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    result[symbol] = future.result()
                    if outcomes is not None:
                        outcomes[symbol] = {"ok": True, "error_type": None}
                except Exception as exc:  # noqa: BLE001
                    result[symbol] = {}
                    if outcomes is not None:
                        outcomes[symbol] = {
                            "ok": False,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        }
    return result


def fetch_tickers_batch_sync(
    symbols: Sequence[str],
    batch_timeout_s: float | None = None,
    *,
    outcomes: dict[str, dict] | None = None,
    transport: dict[str, Any] | None = None,
) -> dict[str, dict]:
    """Probe official domains, then make one breadth-first missing-set pass."""
    syms = [str(symbol) for symbol in symbols if str(symbol or "").strip()]
    if not syms:
        return {}
    budget = okx._TIMEOUT if batch_timeout_s is None else max(
        0.1, float(batch_timeout_s)
    )
    deadline = time.monotonic() + budget
    selected_base: str | None = None
    probe_error_types: list[str] = []
    probe_attempts = 0
    for base in okx._BASE_URLS:
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            break
        probe_attempts += 1
        try:
            # A TLS/proxy failure can leave an httpx connection pool unusable.
            # Isolate each configured-domain probe so the fallback domain does
            # not inherit the primary domain's failed connection state.
            with okx._client() as client:
                _request_ticker(
                    client,
                    base,
                    syms[0],
                    min(deadline, time.monotonic() + min(3.0, remaining)),
                )
            selected_base = base
            break
        except Exception as exc:  # noqa: BLE001
            probe_error_types.append(type(exc).__name__)
    if transport is not None:
        transport.update({
            "probe_attempts": probe_attempts,
            "probe_error_types": probe_error_types,
            "selected_base": selected_base,
            "fresh_client_per_probe": True,
        })
    if selected_base is None:
        raise RuntimeError(
            "official single-ticker probe failed on all configured domains"
        )
    return _batch_on_domain(
        syms,
        base=selected_base,
        deadline=deadline,
        outcomes=outcomes,
    )
