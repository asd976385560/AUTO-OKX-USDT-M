# -*- coding: utf-8 -*-
"""OKX 公共行情 WS-first / REST-recovery 统一读取接口。

实际模式由 ``config/ws_market_source.json`` 决定；配置缺失、损坏或模式非法时
一律 fail-closed 到 ``rest_only``，因此只部署代码不会切换生产事实源。现有
消费者继续使用与 ``_okx_http`` 相同的函数形态；每次调用内部形成
``MarketBatch`` 来源回执，并在 dual_read/ws_first 时读取独立的 WS 缓存。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generic, Optional, Sequence, TypeVar

import _okx_http as _rest
from _okx_ws_cache import (
    CacheReader,
    DEFAULT_CACHE_DB,
    candle_bar_end_ms,
    canonical_json,
    expected_closed_start_ms,
    universe_sha256,
    utc_now_iso,
)

ROOT = Path(os.environ.get("OKX_ROOT", _public_project_path()))
DEFAULT_CONFIG_PATH = ROOT / "config" / "ws_market_source.json"
DEFAULT_LOG_DIR = ROOT / "logs" / "ws-market"
VALID_MODES = {"shadow", "dual_read", "ws_first", "rest_only"}
T = TypeVar("T")


@dataclass(frozen=True)
class MarketBatch(Generic[T]):
    data: T
    source: str
    ws_count: int
    rest_count: int
    missing: tuple[str, ...]
    as_of: str
    universe_sha256: str
    connection_epochs: tuple[str, ...] = ()
    fallback_reasons: tuple[str, ...] = ()
    comparison: dict[str, Any] = field(default_factory=dict)

    def receipt(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("data", None)
        return payload


_config_lock = threading.Lock()
_config_cache: tuple[Path, float | None, dict[str, Any]] | None = None
_log_lock = threading.Lock()


def source_config_path() -> Path:
    return Path(os.environ.get("OKX_MARKET_SOURCE_CONFIG", str(DEFAULT_CONFIG_PATH)))


def load_source_config() -> dict[str, Any]:
    global _config_cache
    path = source_config_path()
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = None
    with _config_lock:
        if (
            _config_cache is not None
            and _config_cache[0] == path
            and _config_cache[1] == modified
        ):
            return dict(_config_cache[2])
        if modified is None:
            config = {
                "schema_version": 1,
                "mode": "rest_only",
                "cache_db": str(DEFAULT_CACHE_DB),
                "heartbeat_max_age_seconds": 60,
                "configuration_error": "config_missing",
            }
        else:
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                config = {
                    "schema_version": 1,
                    "mode": "rest_only",
                    "cache_db": str(DEFAULT_CACHE_DB),
                    "heartbeat_max_age_seconds": 60,
                    "configuration_error": f"{type(exc).__name__}:{exc}",
                }
        mode = str(config.get("mode") or "rest_only")
        if mode not in VALID_MODES:
            config["configuration_error"] = f"invalid_mode:{mode}"
            config["mode"] = "rest_only"
        configured_mode = str(config.get("mode") or "rest_only")
        config["configured_mode"] = configured_mode
        config["mode"] = effective_source_mode(config)
        _config_cache = (path, modified, dict(config))
        return config


def current_source_mode() -> str:
    return str(load_source_config().get("mode") or "rest_only")


def effective_source_mode(
    config: dict[str, Any],
    now: datetime | None = None,
) -> str:
    configured = str(config.get("mode") or "rest_only")
    if configured not in VALID_MODES:
        return "rest_only"
    pending = config.get("pending_mode")
    boundary = config.get("activation_boundary")
    if not pending or str(pending) not in VALID_MODES or not boundary:
        return configured
    try:
        parsed = datetime.fromisoformat(str(boundary).replace("Z", "+00:00"))
    except ValueError:
        return "rest_only"
    if parsed.tzinfo is None:
        return "rest_only"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return str(pending) if current >= parsed else configured


def _reader(config: dict[str, Any]) -> CacheReader:
    return CacheReader(Path(config.get("cache_db") or DEFAULT_CACHE_DB))


def _cache_health(reader: CacheReader, config: dict[str, Any]):
    return reader.health(float(config.get("heartbeat_max_age_seconds") or 60))


def _record(endpoint: str, mode: str, batch: MarketBatch[Any]) -> None:
    payload = {
        "ts": utc_now_iso(),
        "endpoint": endpoint,
        "mode": mode,
        **batch.receipt(),
    }
    line = canonical_json(payload) + "\n"
    directory = Path(os.environ.get("OKX_WS_LOG_DIR", str(DEFAULT_LOG_DIR)))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"adapter-{time.strftime('%Y-%m-%d')}.jsonl"
        with _log_lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
    except OSError:
        # 遥测失败不能把已成功的权威市场读取改成业务失败。
        return


def _batch(
    *,
    data: T,
    source: str,
    ws_count: int,
    rest_count: int,
    expected: Sequence[str],
    observed: Sequence[str],
    fallback_reasons: Sequence[str] = (),
    comparison: dict[str, Any] | None = None,
) -> MarketBatch[T]:
    expected_set = sorted({str(item) for item in expected if str(item)})
    observed_set = {str(item) for item in observed if str(item)}
    return MarketBatch(
        data=data,
        source=source,
        ws_count=int(ws_count),
        rest_count=int(rest_count),
        missing=tuple(item for item in expected_set if item not in observed_set),
        as_of=utc_now_iso(),
        universe_sha256=universe_sha256(expected_set),
        fallback_reasons=tuple(str(item) for item in fallback_reasons if str(item)),
        comparison=comparison or {},
    )


def _mapping_comparison(rest_data: dict, ws_data: dict) -> dict[str, Any]:
    rest_keys = set(rest_data)
    ws_keys = set(ws_data)
    return {
        "kind": "coverage_only_live_values",
        "rest_count": len(rest_keys),
        "ws_count": len(ws_keys),
        "intersection": len(rest_keys & ws_keys),
        "rest_only": sorted(rest_keys - ws_keys)[:20],
        "ws_only": sorted(ws_keys - rest_keys)[:20],
    }


def _closed_candle_comparison(
    symbols: Sequence[str],
    bar: str,
    rest_data: dict[str, list[list[Any]]],
    ws_data: dict[str, list[list[Any]]],
) -> dict[str, Any]:
    expected_ts = str(expected_closed_start_ms(bar))
    mismatches: list[dict[str, Any]] = []
    compared = 0
    rest_missing = 0
    ws_missing = 0
    for symbol in symbols:
        rest_row = _candle_at(rest_data.get(symbol) or [], expected_ts)
        ws_row = _candle_at(ws_data.get(symbol) or [], expected_ts)
        if rest_row is None:
            rest_missing += 1
        if ws_row is None:
            ws_missing += 1
        if rest_row is None or ws_row is None:
            continue
        compared += 1
        # 生产 kline_cache 的 V 字段是 OKX volume_quote（原数组下标7）；
        # 本地冷种子不保留未被消费者使用的 vol/volCcy，因此双读只比较
        # 当前权威写方实际消费的 ts/O/H/L/C/quote-volume/confirm。
        compared_indexes = (0, 1, 2, 3, 4, 7, 8)
        rest_normalized = [
            _canonical_decimal_text(rest_row[index]) for index in compared_indexes
        ]
        ws_normalized = [
            _canonical_decimal_text(ws_row[index]) for index in compared_indexes
        ]
        if rest_normalized != ws_normalized:
            mismatches.append(
                {
                    "symbol": symbol,
                    "rest": rest_normalized,
                    "ws": ws_normalized,
                }
            )
    return {
        "contract_version": 2,
        "numeric_semantics": "exact_decimal",
        "kind": "closed_candle_exact",
        "bar": bar,
        "expected_ts": expected_ts,
        "expected_symbols": len(set(symbols)),
        "compared": compared,
        "rest_missing": rest_missing,
        "ws_missing": ws_missing,
        "unresolved_mismatches": len(mismatches),
        "mismatch_samples": mismatches[:8],
    }


def get_instruments_batch(inst_type: str = "SWAP") -> MarketBatch[list[dict]]:
    config = load_source_config()
    mode = str(config["mode"])
    if mode in {"rest_only", "shadow"}:
        rows = _rest.fetch_instruments_sync(inst_type)
        batch = _batch(
            data=rows,
            source="rest",
            ws_count=0,
            rest_count=len(rows),
            expected=[str(row.get("instId") or "") for row in rows],
            observed=[str(row.get("instId") or "") for row in rows],
        )
        _record("instruments", mode, batch)
        return batch
    reader = _reader(config)
    health = _cache_health(reader, config)
    ws_rows = reader.read_instruments(inst_type) if health.healthy else []
    if mode == "dual_read":
        rest_rows = _rest.fetch_instruments_sync(inst_type)
        comparison = _mapping_comparison(
            {str(row.get("instId")): row for row in rest_rows if row.get("instId")},
            {str(row.get("instId")): row for row in ws_rows if row.get("instId")},
        )
        batch = _batch(
            data=rest_rows,
            source="rest",
            ws_count=len(ws_rows),
            rest_count=len(rest_rows),
            expected=[str(row.get("instId") or "") for row in rest_rows],
            observed=[str(row.get("instId") or "") for row in rest_rows],
            fallback_reasons=(() if health.healthy else (health.reason or "ws_unhealthy",)),
            comparison=comparison,
        )
        batch = _attach_epochs(batch, reader, "instruments")
        _record("instruments", mode, batch)
        return batch
    if health.healthy and ws_rows:
        batch = _batch(
            data=ws_rows,
            source="ws",
            ws_count=len(ws_rows),
            rest_count=0,
            expected=[str(row.get("instId") or "") for row in ws_rows],
            observed=[str(row.get("instId") or "") for row in ws_rows],
        )
    else:
        rest_rows = _rest.fetch_instruments_sync(inst_type)
        batch = _batch(
            data=rest_rows,
            source="rest",
            ws_count=len(ws_rows),
            rest_count=len(rest_rows),
            expected=[str(row.get("instId") or "") for row in rest_rows],
            observed=[str(row.get("instId") or "") for row in rest_rows],
            fallback_reasons=(health.reason or "ws_instruments_empty",),
        )
    batch = _attach_epochs(batch, reader, "instruments")
    _record("instruments", mode, batch)
    return batch


def fetch_instruments_sync(inst_type: str = "SWAP") -> list[dict]:
    return get_instruments_batch(inst_type).data


def get_tickers_batch(
    request_timeout_s: float | None = None,
    *,
    transport_fallback: Callable[[str, Optional[dict], float], list] | None = None,
    transport_fallback_reserve_s: float = 0.0,
) -> MarketBatch[list[dict]]:
    config = load_source_config()
    mode = str(config["mode"])

    def rest_call() -> list[dict]:
        return _rest.fetch_tickers_all_sync(
            request_timeout_s,
            transport_fallback=transport_fallback,
            transport_fallback_reserve_s=transport_fallback_reserve_s,
        )

    if mode in {"rest_only", "shadow"}:
        rows = rest_call()
        batch = _rows_batch(rows, "rest")
        _record("tickers", mode, batch)
        return batch
    reader = _reader(config)
    health = _cache_health(reader, config)
    expected = reader.live_symbols() if health.healthy else []
    ws_map = (
        reader.read_latest_current("tickers", expected)
        if health.healthy
        else {}
    )
    acked, ack_expected = (
        reader.channel_ack_coverage("tickers", expected) if expected else (0, 0)
    )
    ws_ready = bool(expected) and len(ws_map) == len(expected) and acked == ack_expected
    if mode == "dual_read":
        rest_rows = rest_call()
        rest_map = {
            str(row.get("instId")): row for row in rest_rows if row.get("instId")
        }
        batch = _batch(
            data=rest_rows,
            source="rest",
            ws_count=len(ws_map),
            rest_count=len(rest_rows),
            expected=list(rest_map),
            observed=list(rest_map),
            fallback_reasons=(() if ws_ready else (_ws_reason(health, acked, ack_expected),)),
            comparison=_mapping_comparison(rest_map, ws_map),
        )
        batch = _attach_epochs(batch, reader, "tickers")
        _record("tickers", mode, batch)
        return batch
    if ws_ready:
        rows = [ws_map[symbol] for symbol in expected]
        batch = _batch(
            data=rows,
            source="ws",
            ws_count=len(rows),
            rest_count=0,
            expected=expected,
            observed=expected,
        )
    else:
        rows = rest_call()
        batch = _rows_batch(
            rows,
            "rest",
            ws_count=len(ws_map),
            fallback_reasons=(_ws_reason(health, acked, ack_expected),),
        )
    batch = _attach_epochs(batch, reader, "tickers")
    _record("tickers", mode, batch)
    return batch


def fetch_tickers_all_sync(
    request_timeout_s: float | None = None,
    *,
    transport_fallback: Callable[[str, Optional[dict], float], list] | None = None,
    transport_fallback_reserve_s: float = 0.0,
) -> list[dict]:
    return get_tickers_batch(
        request_timeout_s,
        transport_fallback=transport_fallback,
        transport_fallback_reserve_s=transport_fallback_reserve_s,
    ).data


def get_candles_batch(
    symbols: Sequence[str],
    bar: str = "1H",
    limit: int = 60,
    batch_timeout_s: float | None = None,
    *,
    outcomes: dict[str, dict] | None = None,
) -> MarketBatch[dict[str, list[list[Any]]]]:
    requested = sorted({str(symbol) for symbol in symbols if str(symbol)})
    config = load_source_config()
    mode = str(config["mode"])
    if mode in {"rest_only", "shadow"}:
        data = _rest.fetch_candles_batch_sync(
            requested, bar, limit, batch_timeout_s, outcomes=outcomes
        )
        batch = _mapping_batch(data, requested, "rest")
        _record(f"candles:{bar}", mode, batch)
        return batch
    reader = _reader(config)
    health = _cache_health(reader, config)
    ws_data = (
        reader.read_candles(requested, bar, limit) if health.healthy else {}
    )
    expected_ts = str(expected_closed_start_ms(bar))
    applicable = _applicable_candle_symbols(
        reader, requested, bar, int(expected_ts)
    )
    raw_ws_symbols = (
        reader.raw_ws_candle_symbols(requested, bar, int(expected_ts))
        if health.healthy
        else set()
    )
    ws_usable = {
        symbol
        for symbol in applicable
        if symbol in raw_ws_symbols
        and _candle_at(ws_data.get(symbol) or [], expected_ts) is not None
    }
    acked, ack_expected = (
        reader.channel_ack_coverage(f"candle{bar}", requested)
        if health.healthy
        else (0, len(requested))
    )
    if mode == "dual_read":
        rest_outcomes: dict[str, dict] = {}
        rest_data = _rest.fetch_candles_batch_sync(
            requested,
            bar,
            limit,
            batch_timeout_s,
            outcomes=rest_outcomes,
        )
        if outcomes is not None:
            outcomes.update(rest_outcomes)
        # Natural slots start exactly on a candle boundary.  The first WS read can
        # therefore precede OKX's confirm=1 push by a few seconds while the REST
        # batch is still in flight.  Refresh after REST completes so dual-read
        # compares the settled same-slot snapshot rather than a pre-close race.
        if health.healthy:
            ws_data = reader.read_candles(requested, bar, limit)
            raw_ws_symbols = reader.raw_ws_candle_symbols(
                applicable, bar, int(expected_ts)
            )
            ws_usable = {
                symbol
                for symbol in applicable
                if symbol in raw_ws_symbols
                and _candle_at(ws_data.get(symbol) or [], expected_ts) is not None
            }
        rest_usable = {
            symbol
            for symbol in applicable
            if _candle_at(rest_data.get(symbol) or [], expected_ts) is not None
        }
        batch = _batch(
            data=rest_data,
            source="rest",
            ws_count=len(ws_usable),
            rest_count=len(rest_usable),
            expected=applicable,
            observed=rest_usable,
            fallback_reasons=_candle_ws_reasons(
                health, acked, ack_expected, len(ws_usable), len(applicable)
            ),
            comparison=_closed_candle_comparison(
                applicable,
                bar,
                rest_data,
                {
                    symbol: ws_data.get(symbol) or []
                    if symbol in ws_usable
                    else []
                    for symbol in applicable
                },
            ),
        )
        batch = _attach_epochs(batch, reader, f"candle{bar}")
        _record(f"candles:{bar}", mode, batch)
        return batch
    # Monthly close events occur only at the month boundary.  If the exact
    # immutable previous-month row is already present as authoritative local
    # history, it also prevents an unnecessary 15-second WS-close wait on
    # every ordinary hourly cycle.  At a real month boundary the expected row
    # changes, so this set is empty and the normal WS wait/recovery path remains.
    local_history_usable: set[str] = set()
    if bar == "1M" and health.healthy:
        local_history_symbols = reader.local_history_candle_symbols(
            applicable, bar, int(expected_ts)
        )
        local_history_usable = {
            symbol
            for symbol in applicable
            if symbol not in ws_usable
            and symbol in local_history_symbols
            and _candle_at(ws_data.get(symbol) or [], expected_ts) is not None
        }
    wait_seconds = min(
        30.0, max(0.0, float(config.get("ws_candle_wait_seconds") or 0.0))
    )
    if (
        wait_seconds > 0
        and health.healthy
        and acked == ack_expected
        and len(ws_usable | local_history_usable) < len(applicable)
    ):
        deadline = time.monotonic() + wait_seconds
        while (
            len(ws_usable | local_history_usable) < len(applicable)
            and time.monotonic() < deadline
        ):
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            ws_data = reader.read_candles(requested, bar, limit)
            raw_ws_symbols = reader.raw_ws_candle_symbols(
                applicable, bar, int(expected_ts)
            )
            ws_usable = {
                symbol
                for symbol in applicable
                if symbol in raw_ws_symbols
                and _candle_at(ws_data.get(symbol) or [], expected_ts) is not None
            }
    local_history_usable -= ws_usable
    nonapplicable = sorted(set(requested) - set(applicable))
    locally_usable = ws_usable | local_history_usable
    missing = [symbol for symbol in applicable if symbol not in locally_usable]
    if not health.healthy:
        missing = applicable
        local_history_usable = set()
        locally_usable = set()
    rest_requested = sorted(set(missing) | set(nonapplicable))
    rest_data: dict[str, list[list[Any]]] = {}
    rest_outcomes: dict[str, dict] = {}
    if rest_requested:
        rest_data = _rest.fetch_candles_batch_sync(
            rest_requested,
            bar,
            limit,
            batch_timeout_s,
            outcomes=rest_outcomes,
        )
    merged = {
        symbol: (
            ws_data.get(symbol) or []
            if symbol not in rest_requested
            else rest_data.get(symbol) or []
        )
        for symbol in requested
    }
    if outcomes is not None:
        for symbol in requested:
            if symbol not in applicable:
                outcomes[symbol] = rest_outcomes.get(
                    symbol,
                    {
                        "ok": bool(rest_data.get(symbol)),
                        "error_type": None,
                        "root_error_type": None,
                        "error_type_chain": [],
                    },
                )
                outcomes[symbol]["source"] = "rest_history_pre_listing"
            elif symbol not in missing:
                outcomes[symbol] = {
                    "ok": True,
                    "error_type": None,
                    "root_error_type": None,
                    "error_type_chain": [],
                    "source": (
                        "ws"
                        if symbol in ws_usable
                        else "local_history"
                    ),
                }
            else:
                outcomes[symbol] = rest_outcomes.get(
                    symbol,
                    {
                        "ok": bool(rest_data.get(symbol)),
                        "error_type": None,
                        "root_error_type": None,
                        "error_type_chain": [],
                        "source": "rest_recovery",
                    },
                )
    if not rest_requested:
        if local_history_usable and ws_usable:
            source = "ws_local_history"
        elif local_history_usable:
            source = "local_history"
        else:
            source = "ws"
    else:
        source = "mixed" if locally_usable else "rest"
    reasons = []
    if missing:
        reasons.append(
            _ws_reason(health, acked, ack_expected)
            if not health.healthy or acked != ack_expected
            else f"missing_closed_candle:{len(missing)}"
        )
    if nonapplicable:
        reasons.append(
            f"historical_rest_recovery_pre_listing:{len(nonapplicable)}"
        )
    if local_history_usable:
        reasons.append(f"local_history_reuse:{len(local_history_usable)}")
    rest_recovered = {
        symbol
        for symbol in missing
        if _candle_at(rest_data.get(symbol) or [], expected_ts) is not None
    }
    batch = _batch(
        data=merged,
        source=source,
        ws_count=len(ws_usable),
        rest_count=sum(1 for symbol in rest_requested if rest_data.get(symbol)),
        expected=applicable,
        observed=sorted(ws_usable | local_history_usable | rest_recovered),
        fallback_reasons=reasons,
    )
    batch = _attach_epochs(batch, reader, f"candle{bar}")
    _record(f"candles:{bar}", mode, batch)
    return batch


def fetch_candles_batch_sync(
    symbols: Sequence[str],
    bar: str = "1H",
    limit: int = 60,
    batch_timeout_s: float | None = None,
    *,
    outcomes: dict[str, dict] | None = None,
) -> dict:
    return get_candles_batch(
        symbols,
        bar,
        limit,
        batch_timeout_s,
        outcomes=outcomes,
    ).data


def fetch_funding_rates_batch_sync(
    symbols: Sequence[str],
    batch_timeout_s: float | None = None,
) -> dict:
    return _latest_mapping_call(
        endpoint="funding-rate",
        channel="funding-rate",
        symbols=symbols,
        rest_call=lambda subset: _rest.fetch_funding_rates_batch_sync(
            subset, batch_timeout_s
        ),
        partial_recovery=True,
    ).data


def fetch_open_interest_all_sync(
    inst_type: str = "SWAP",
    request_timeout_s: float | None = None,
) -> dict:
    return _aggregate_mapping_call(
        endpoint="open-interest",
        channel="open-interest",
        rest_call=lambda: _rest.fetch_open_interest_all_sync(
            inst_type, request_timeout_s
        ),
    ).data


def fetch_mark_prices_all_sync(
    inst_type: str = "SWAP",
    request_timeout_s: float | None = None,
) -> dict:
    return _aggregate_mapping_call(
        endpoint="mark-price",
        channel="mark-price",
        rest_call=lambda: _rest.fetch_mark_prices_all_sync(
            inst_type, request_timeout_s
        ),
    ).data


def fetch_index_tickers_all_sync(
    quote_ccy: str = "USDT",
    request_timeout_s: float | None = None,
) -> dict:
    config = load_source_config()
    mode = str(config["mode"])
    if mode in {"rest_only", "shadow"}:
        data = _rest.fetch_index_tickers_all_sync(quote_ccy, request_timeout_s)
        batch = _mapping_batch(data, list(data), "rest")
        _record("index-tickers", mode, batch)
        return batch.data
    reader = _reader(config)
    health = _cache_health(reader, config)
    expected = sorted(
        {
            symbol.removesuffix("-SWAP")
            for symbol in (reader.live_symbols() if health.healthy else [])
        }
    )
    ws_data = (
        reader.read_latest_current("index-tickers", expected)
        if expected
        else {}
    )
    acked, ack_expected = (
        reader.channel_ack_coverage("index-tickers", expected)
        if expected
        else (0, 0)
    )
    rest_call = lambda: _rest.fetch_index_tickers_all_sync(
        quote_ccy, request_timeout_s
    )
    batch = _resolve_aggregate(
        endpoint="index-tickers",
        mode=mode,
        health=health,
        expected=expected,
        ws_data=ws_data,
        acked=acked,
        ack_expected=ack_expected,
        rest_call=rest_call,
        reader=reader,
        channel="index-tickers",
    )
    return batch.data


def get_orderbooks_batch(
    symbols: Sequence[str],
    depth: int = 50,
    batch_timeout_s: float | None = None,
    *,
    outcomes: dict[str, dict] | None = None,
) -> MarketBatch[dict]:
    requested = sorted({str(symbol) for symbol in symbols if str(symbol)})
    batch = _partial_mapping_call(
        endpoint="books",
        channel="books",
        symbols=requested,
        rest_call=lambda subset: _rest.fetch_orderbooks_batch_sync(
            subset,
            depth,
            batch_timeout_s,
            outcomes=outcomes,
        ),
        cache_call=lambda reader: reader.read_books_current(requested),
    )
    if outcomes is not None and current_source_mode() == "ws_first":
        for symbol in requested:
            if batch.data.get(symbol) and symbol not in outcomes:
                outcomes[symbol] = {
                    "ok": True,
                    "error_type": None,
                    "root_error_type": None,
                    "error_type_chain": [],
                    "source": "ws",
                }
    return batch


def fetch_orderbooks_batch_sync(
    symbols: Sequence[str],
    depth: int = 50,
    batch_timeout_s: float | None = None,
    *,
    outcomes: dict[str, dict] | None = None,
) -> dict:
    return get_orderbooks_batch(
        symbols,
        depth,
        batch_timeout_s,
        outcomes=outcomes,
    ).data


def get_recent_trades_batch(
    symbols: Sequence[str],
    limit: int = 500,
    batch_timeout_s: float | None = None,
    *,
    outcomes: dict[str, dict] | None = None,
) -> MarketBatch[dict]:
    requested = sorted({str(symbol) for symbol in symbols if str(symbol)})
    batch = _partial_mapping_call(
        endpoint="trades-all",
        channel="trades-all",
        symbols=requested,
        rest_call=lambda subset: _rest.fetch_recent_trades_batch_sync(
            subset,
            limit,
            batch_timeout_s,
            outcomes=outcomes,
        ),
        cache_call=lambda reader: reader.read_trades(requested, limit),
        require_complete_stream=True,
    )
    if outcomes is not None and current_source_mode() == "ws_first":
        for symbol in requested:
            if batch.data.get(symbol) and symbol not in outcomes:
                outcomes[symbol] = {
                    "ok": True,
                    "error_type": None,
                    "root_error_type": None,
                    "error_type_chain": [],
                    "source": "ws",
                }
    return batch


def fetch_recent_trades_batch_sync(
    symbols: Sequence[str],
    limit: int = 500,
    batch_timeout_s: float | None = None,
    *,
    outcomes: dict[str, dict] | None = None,
) -> dict:
    return get_recent_trades_batch(
        symbols,
        limit,
        batch_timeout_s,
        outcomes=outcomes,
    ).data


def _latest_mapping_call(
    *,
    endpoint: str,
    channel: str,
    symbols: Sequence[str],
    rest_call: Callable[[Sequence[str]], dict],
    partial_recovery: bool,
) -> MarketBatch[dict]:
    del partial_recovery
    requested = sorted({str(symbol) for symbol in symbols if str(symbol)})
    return _partial_mapping_call(
        endpoint=endpoint,
        channel=channel,
        symbols=requested,
        rest_call=rest_call,
        cache_call=lambda reader: reader.read_latest_current(channel, requested),
    )


def _aggregate_mapping_call(
    *,
    endpoint: str,
    channel: str,
    rest_call: Callable[[], dict],
) -> MarketBatch[dict]:
    config = load_source_config()
    mode = str(config["mode"])
    if mode in {"rest_only", "shadow"}:
        data = rest_call()
        batch = _mapping_batch(data, list(data), "rest")
        _record(endpoint, mode, batch)
        return batch
    reader = _reader(config)
    health = _cache_health(reader, config)
    expected = reader.live_symbols() if health.healthy else []
    ws_data = (
        reader.read_latest_current(channel, expected) if expected else {}
    )
    acked, ack_expected = (
        reader.channel_ack_coverage(channel, expected) if expected else (0, 0)
    )
    return _resolve_aggregate(
        endpoint=endpoint,
        mode=mode,
        health=health,
        expected=expected,
        ws_data=ws_data,
        acked=acked,
        ack_expected=ack_expected,
        rest_call=rest_call,
        reader=reader,
        channel=channel,
    )


def _resolve_aggregate(
    *,
    endpoint: str,
    mode: str,
    health,
    expected: Sequence[str],
    ws_data: dict,
    acked: int,
    ack_expected: int,
    rest_call: Callable[[], dict],
    reader: CacheReader,
    channel: str,
) -> MarketBatch[dict]:
    ready = (
        health.healthy
        and bool(expected)
        and len(ws_data) == len(expected)
        and acked == ack_expected
    )
    if mode == "dual_read":
        rest_data = rest_call()
        batch = _batch(
            data=rest_data,
            source="rest",
            ws_count=len(ws_data),
            rest_count=len(rest_data),
            expected=list(rest_data),
            observed=list(rest_data),
            fallback_reasons=(() if ready else (_ws_reason(health, acked, ack_expected),)),
            comparison=_mapping_comparison(rest_data, ws_data),
        )
    elif ready:
        batch = _mapping_batch(ws_data, expected, "ws")
    else:
        rest_data = rest_call()
        batch = _mapping_batch(
            rest_data,
            list(rest_data),
            "rest",
            ws_count=len(ws_data),
            fallback_reasons=(_ws_reason(health, acked, ack_expected),),
        )
    batch = _attach_epochs(batch, reader, channel)
    _record(endpoint, mode, batch)
    return batch


def _partial_mapping_call(
    *,
    endpoint: str,
    channel: str,
    symbols: Sequence[str],
    rest_call: Callable[[Sequence[str]], dict],
    cache_call: Callable[[CacheReader], dict],
    require_complete_stream: bool = False,
) -> MarketBatch[dict]:
    requested = sorted({str(symbol) for symbol in symbols if str(symbol)})
    config = load_source_config()
    mode = str(config["mode"])
    if mode in {"rest_only", "shadow"}:
        data = rest_call(requested)
        batch = _mapping_batch(data, requested, "rest")
        _record(endpoint, mode, batch)
        return batch
    reader = _reader(config)
    health = _cache_health(reader, config)
    ws_data = cache_call(reader) if health.healthy else {}
    complete_stream = (
        reader.complete_stream_symbols(channel, requested)
        if require_complete_stream and health.healthy
        else set(requested)
    )
    acked_symbols = (
        reader.current_acked_symbols(channel, requested)
        if health.healthy
        else set()
    )
    usable = {
        symbol
        for symbol in requested
        if (
            ws_data.get(symbol)
            and symbol in complete_stream
            and symbol in acked_symbols
        )
    }
    acked, ack_expected = len(acked_symbols), len(requested)
    if mode == "dual_read":
        rest_data = rest_call(requested)
        batch = _batch(
            data=rest_data,
            source="rest",
            ws_count=len(usable),
            rest_count=sum(1 for value in rest_data.values() if value),
            expected=requested,
            observed=[symbol for symbol in requested if rest_data.get(symbol)],
            fallback_reasons=(
                ()
                if (
                    health.healthy
                    and acked == ack_expected
                    and (not require_complete_stream or len(complete_stream) == len(requested))
                )
                else (_ws_reason(health, acked, ack_expected),)
            ),
            comparison=_mapping_comparison(rest_data, ws_data),
        )
        batch = _attach_epochs(batch, reader, channel)
        _record(endpoint, mode, batch)
        return batch
    missing = [symbol for symbol in requested if symbol not in usable]
    if not health.healthy:
        missing = requested
    rest_data = rest_call(missing) if missing else {}
    merged = {
        symbol: (
            ws_data.get(symbol)
            if symbol not in missing
            else rest_data.get(symbol, {} if endpoint != "trades-all" else [])
        )
        for symbol in requested
    }
    source = "ws" if not missing else ("mixed" if usable else "rest")
    batch = _batch(
        data=merged,
        source=source,
        ws_count=len(requested) - len(missing),
        rest_count=sum(1 for symbol in missing if rest_data.get(symbol)),
        expected=requested,
        observed=[symbol for symbol in requested if merged.get(symbol)],
        fallback_reasons=(
            (
                _ws_reason(health, acked, ack_expected)
                if not health.healthy or acked != ack_expected
                else (
                    f"stream_incomplete:{len(requested) - len(complete_stream)}"
                    if require_complete_stream and len(complete_stream) != len(requested)
                    else f"missing_ws_rows:{len(missing)}"
                )
            ),
        )
        if missing
        else (),
    )
    batch = _attach_epochs(batch, reader, channel)
    _record(endpoint, mode, batch)
    return batch


def _rows_batch(
    rows: Sequence[dict],
    source: str,
    *,
    ws_count: int = 0,
    fallback_reasons: Sequence[str] = (),
) -> MarketBatch[list[dict]]:
    symbols = [str(row.get("instId") or "") for row in rows if row.get("instId")]
    return _batch(
        data=list(rows),
        source=source,
        ws_count=ws_count,
        rest_count=(len(rows) if source == "rest" else 0),
        expected=symbols,
        observed=symbols,
        fallback_reasons=fallback_reasons,
    )


def _mapping_batch(
    data: dict,
    expected: Sequence[str],
    source: str,
    *,
    ws_count: int | None = None,
    fallback_reasons: Sequence[str] = (),
) -> MarketBatch[dict]:
    observed = [str(key) for key, value in data.items() if value]
    return _batch(
        data=data,
        source=source,
        ws_count=(len(observed) if source == "ws" else int(ws_count or 0)),
        rest_count=(len(observed) if source == "rest" else 0),
        expected=expected,
        observed=observed,
        fallback_reasons=fallback_reasons,
    )


def _ws_reason(health, acked: int, expected: int) -> str:
    if not health.healthy:
        return str(health.reason or "ws_unhealthy")
    if acked != expected:
        return f"subscription_ack_incomplete:{acked}/{expected}"
    return "ws_rows_incomplete"


def _candle_ws_reasons(
    health, acked: int, ack_expected: int, usable: int, requested: int
) -> tuple[str, ...]:
    if not health.healthy or acked != ack_expected:
        return (_ws_reason(health, acked, ack_expected),)
    if usable != requested:
        return (f"raw_ws_closed_candle_incomplete:{usable}/{requested}",)
    return ()


def _applicable_candle_symbols(
    reader: CacheReader,
    requested: Sequence[str],
    bar: str,
    expected_ts_ms: int,
) -> list[str]:
    bar_end_ms = candle_bar_end_ms(bar, int(expected_ts_ms))
    list_times = reader.instrument_list_times(requested)
    return [
        symbol
        for symbol in requested
        if list_times.get(symbol) is None
        or int(list_times[symbol]) < int(bar_end_ms)
    ]


def _attach_epochs(
    batch: MarketBatch[T],
    reader: CacheReader,
    channel: str,
) -> MarketBatch[T]:
    return replace(batch, connection_epochs=reader.connection_epochs(channel))


def _candle_at(rows: Sequence[Sequence[Any]], expected_ts: str):
    for row in rows:
        if len(row) >= 9 and str(row[0]) == expected_ts and str(row[8]) == "1":
            return row
    return None


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _canonical_decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if not number.is_finite():
        return text
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def source_contract_sha256() -> str:
    path = Path(__file__)
    return hashlib.sha256(path.read_bytes()).hexdigest()
