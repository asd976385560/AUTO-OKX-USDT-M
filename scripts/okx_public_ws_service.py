# -*- coding: utf-8 -*-
"""OKX 公共行情 WebSocket 常驻缓存服务。

只连接 ``/public`` 与 ``/business`` 公共频道，只写可重建的
``ws_market_cache.db``。本进程不触发分析、交易、writer 或 Push。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import asyncio
import contextlib
import ctypes
import json
import os
import random
import signal
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from _okx_http import fetch_instruments_sync, fetch_recent_trades_batch_sync
from _okx_ws_cache import (
    CacheReader,
    CacheStore,
    DEFAULT_CACHE_DB,
    apply_orderbook_update,
    canonical_json,
    orderbook_checksum,
    shard_subscription_args,
    subscription_arg_key,
    subscription_payload_size,
    universe_sha256,
    utc_now_iso,
)

ROOT = Path(os.environ.get("OKX_ROOT", _public_project_path()))
DEFAULT_MARKET_DB = ROOT / "db" / "market.db"
DEFAULT_LOG_DIR = ROOT / "logs" / "ws-market"
PUBLIC_WS_URL = os.environ.get(
    "OKX_WS_PUBLIC_URL", "wss://ws.okx.com:8443/ws/v5/public"
)
BUSINESS_WS_URL = os.environ.get(
    "OKX_WS_BUSINESS_URL", "wss://ws.okx.com:8443/ws/v5/business"
)
PROXY_URL = os.environ.get("OKX_PROXY_URL") or None
SUBSCRIPTION_MAX_BYTES = 48 * 1024
PING_IDLE_SECONDS = 20.0
PONG_TIMEOUT_SECONDS = 10.0
ACK_TIMEOUT_SECONDS = 90.0
STATIC_REFRESH_SECONDS = 30.0
DYNAMIC_REFRESH_SECONDS = 20.0
FLUSH_INTERVAL_SECONDS = 0.5
HEARTBEAT_SECONDS = 5.0
TRADE_BOOTSTRAP_RETRY_DELAY_SECONDS = 3.0
TRADE_BOOTSTRAP_RETRY_TIMEOUT_SECONDS = 20.0
RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


class ServiceError(RuntimeError):
    pass


class BookSequenceError(ServiceError):
    pass


def reconnect_delay_base(consecutive_failures: int) -> float:
    """Return the bounded backoff for consecutive failed connection attempts."""
    index = min(max(0, int(consecutive_failures)), len(RECONNECT_DELAYS) - 1)
    return RECONNECT_DELAYS[index]


class StructuredLog:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        payload = {"ts": utc_now_iso(), "event": event, **fields}
        line = canonical_json(payload) + "\n"
        path = self.directory / f"service-{time.strftime('%Y-%m-%d')}.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise ServiceError("okx_public_ws_service_already_running") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is None:
            return
        with contextlib.suppress(OSError):
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


@dataclass(frozen=True)
class ConnectionGroup:
    group_id: str
    endpoint: str
    args: tuple[dict[str, Any], ...]

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(sorted({str(arg.get("channel") or "") for arg in self.args}))


def _group_shards(
    prefix: str,
    endpoint: str,
    args: Sequence[dict[str, Any]],
) -> list[ConnectionGroup]:
    return [
        ConnectionGroup(
            group_id=f"{prefix}-{index:02d}",
            endpoint=endpoint,
            args=tuple(shard),
        )
        for index, shard in enumerate(
            shard_subscription_args(args, SUBSCRIPTION_MAX_BYTES), start=1
        )
    ]


def build_static_groups(symbols: Sequence[str]) -> list[ConnectionGroup]:
    syms = sorted({str(symbol) for symbol in symbols if str(symbol)})
    index_ids = sorted({symbol.removesuffix("-SWAP") for symbol in syms})
    public_state = [
        {"channel": channel, "instId": symbol}
        for channel in ("tickers", "funding-rate", "open-interest", "mark-price")
        for symbol in syms
    ]
    public_state.extend(
        {"channel": "index-tickers", "instId": index_id}
        for index_id in index_ids
    )
    candle_fast = [
        {"channel": f"candle{bar}", "instId": symbol}
        for bar in ("15m", "1H", "4H")
        for symbol in syms
    ]
    candle_slow = [
        {"channel": f"candle{bar}", "instId": symbol}
        for bar in ("1D", "1W", "1M")
        for symbol in syms
    ]
    groups = [
        ConnectionGroup(
            group_id="public-instruments-01",
            endpoint=PUBLIC_WS_URL,
            args=({"channel": "instruments", "instType": "SWAP"},),
        )
    ]
    if public_state:
        groups.extend(_group_shards("public-state", PUBLIC_WS_URL, public_state))
    if candle_fast:
        groups.extend(
            _group_shards("business-candle-fast", BUSINESS_WS_URL, candle_fast)
        )
    if candle_slow:
        groups.extend(
            _group_shards("business-candle-slow", BUSINESS_WS_URL, candle_slow)
        )
    return groups


def build_dynamic_groups(symbols: Sequence[str]) -> list[ConnectionGroup]:
    syms = sorted({str(symbol) for symbol in symbols if str(symbol)})
    if not syms:
        return []
    books = [{"channel": "books", "instId": symbol} for symbol in syms]
    trades = [{"channel": "trades-all", "instId": symbol} for symbol in syms]
    return [
        *_group_shards("public-books", PUBLIC_WS_URL, books),
        *_group_shards("business-trades", BUSINESS_WS_URL, trades),
    ]


class ConnectionRateLimiter:
    def __init__(self, minimum_interval_s: float = 0.55) -> None:
        self.minimum_interval_s = minimum_interval_s
        self._lock = asyncio.Lock()
        self._last_started = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self.minimum_interval_s - (now - self._last_started)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_started = time.monotonic()


class StreamState:
    def __init__(self, store: CacheStore) -> None:
        self.store = store
        self.latest: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
        self.candles: dict[
            tuple[str, str, str], tuple[list[Any], str]
        ] = {}
        self.instruments: dict[str, dict[str, Any]] = {}
        self.books: dict[str, tuple[dict[str, Any], str, bool]] = {}
        self.book_state: dict[str, dict[str, Any]] = {}
        self.trades: dict[
            tuple[str, str], tuple[dict[str, Any], str]
        ] = {}
        self.required_drops = 0

    def ingest(
        self,
        arg: dict[str, Any],
        action: str | None,
        data: Sequence[Any],
        conn_epoch: str,
    ) -> None:
        channel = str(arg.get("channel") or "")
        if channel in {
            "tickers",
            "funding-rate",
            "open-interest",
            "mark-price",
            "index-tickers",
        }:
            for row in data:
                if not isinstance(row, dict):
                    continue
                inst_id = str(row.get("instId") or arg.get("instId") or "")
                if inst_id:
                    self.latest[(channel, inst_id)] = (dict(row), conn_epoch)
            return
        if channel == "instruments":
            for row in data:
                if isinstance(row, dict) and row.get("instId"):
                    self.instruments[str(row["instId"])] = dict(row)
            return
        if channel.startswith("candle"):
            timeframe = channel.removeprefix("candle")
            inst_id = str(arg.get("instId") or "")
            for row in data:
                if (
                    inst_id
                    and isinstance(row, list)
                    and len(row) >= 9
                    and str(row[8]) == "1"
                ):
                    self.candles[(inst_id, timeframe, str(row[0]))] = (
                        list(row),
                        conn_epoch,
                    )
            return
        if channel == "books":
            inst_id = str(arg.get("instId") or "")
            for row in data:
                if isinstance(row, dict):
                    self._ingest_book(inst_id, action, row, conn_epoch)
            return
        if channel in {"trades", "trades-all"}:
            inst_id = str(arg.get("instId") or "")
            for row in data:
                if not isinstance(row, dict):
                    continue
                trade_id = str(row.get("tradeId") or "")
                resolved = str(row.get("instId") or inst_id)
                if trade_id and resolved:
                    self.trades[(resolved, trade_id)] = (dict(row), conn_epoch)

    def _ingest_book(
        self,
        inst_id: str,
        action: str | None,
        row: dict[str, Any],
        conn_epoch: str,
    ) -> None:
        if not inst_id:
            return
        if action == "snapshot":
            bids = sorted(
                [list(level) for level in row.get("bids") or []],
                key=lambda level: float(level[0]),
                reverse=True,
            )[:400]
            asks = sorted(
                [list(level) for level in row.get("asks") or []],
                key=lambda level: float(level[0]),
            )[:400]
        else:
            current = self.book_state.get(inst_id)
            if not current or not current.get("valid"):
                self.store.invalidate_book(inst_id, conn_epoch)
                raise BookSequenceError(f"{inst_id}:book_update_without_snapshot")
            previous = _to_int(row.get("prevSeqId"))
            current_seq = _to_int(current.get("seqId"))
            if previous is None or current_seq is None or previous != current_seq:
                current["valid"] = False
                self.store.invalidate_book(inst_id, conn_epoch)
                raise BookSequenceError(
                    f"{inst_id}:book_sequence_break:{previous}!={current_seq}"
                )
            bids, asks = apply_orderbook_update(
                current.get("bids") or [],
                current.get("asks") or [],
                row.get("bids") or [],
                row.get("asks") or [],
            )
        checksum = _to_int(row.get("checksum"))
        # OKX 已将 books/books50-l2-tbt/books-l2-tbt 的 checksum 废弃，
        # 当前固定推 0，并明确要求只用 seqId/prevSeqId 验证连续性。
        # 非零值仅为兼容旧回放样本；此时仍执行旧 CRC 校验。
        if checksum not in (None, 0) and orderbook_checksum(bids, asks) != checksum:
            self.store.invalidate_book(inst_id, conn_epoch)
            raise BookSequenceError(f"{inst_id}:book_checksum_mismatch")
        normalized = {
            "ts": row.get("ts"),
            "seqId": row.get("seqId"),
            "prevSeqId": row.get("prevSeqId"),
            "checksum": checksum,
            "bids": bids,
            "asks": asks,
            "valid": True,
        }
        self.book_state[inst_id] = normalized
        self.books[inst_id] = (normalized, conn_epoch, True)

    def take_pending(self) -> dict[str, Any]:
        pending = {
            "latest": self.latest,
            "candles": self.candles,
            "instruments": self.instruments,
            "books": self.books,
            "trades": self.trades,
        }
        self.latest = {}
        self.candles = {}
        self.instruments = {}
        self.books = {}
        self.trades = {}
        return pending

    def pending_counts(self) -> dict[str, int]:
        return {
            "pending_latest": len(self.latest),
            "pending_candles": len(self.candles),
            "pending_books": len(self.books),
            "pending_trades": len(self.trades),
        }


def flush_pending(store: CacheStore, pending: dict[str, Any]) -> dict[str, int]:
    wrote = {"latest": 0, "candles": 0, "instruments": 0, "books": 0, "trades": 0}
    instrument_rows = list(pending["instruments"].values())
    if instrument_rows:
        wrote["instruments"] = store.upsert_instruments(instrument_rows, "ws")
    latest_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (channel, _inst_id), (row, epoch) in pending["latest"].items():
        latest_groups.setdefault((channel, epoch), []).append(row)
    for (channel, epoch), rows in latest_groups.items():
        wrote["latest"] += store.upsert_latest(channel, rows, epoch)
    candle_entries = [
        (inst_id, timeframe, [row], epoch, "ws")
        for (inst_id, timeframe, _ts), (row, epoch) in pending["candles"].items()
    ]
    if candle_entries:
        wrote["candles"] = store.upsert_candle_batches(candle_entries)
    book_entries = [
        (inst_id, book, epoch, valid)
        for inst_id, (book, epoch, valid) in pending["books"].items()
    ]
    if book_entries:
        wrote["books"] = store.upsert_books(book_entries)
    trade_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (inst_id, _trade_id), (row, epoch) in pending["trades"].items():
        trade_groups.setdefault((inst_id, epoch), []).append(row)
    if trade_groups:
        wrote["trades"] = store.insert_trade_batches(
            [(inst_id, rows, epoch) for (inst_id, epoch), rows in trade_groups.items()]
        )
    return wrote


class ConnectionRunner:
    def __init__(
        self,
        store: CacheStore,
        state: StreamState,
        limiter: ConnectionRateLimiter,
        logger: StructuredLog,
        stop_event: asyncio.Event,
    ) -> None:
        self.store = store
        self.state = state
        self.limiter = limiter
        self.logger = logger
        self.stop_event = stop_event
        self._last_health_update: dict[str, float] = {}
        self._bootstrap_tasks: set[asyncio.Task] = set()

    async def run(self, group: ConnectionGroup) -> None:
        reconnect_count = 0
        consecutive_failures = 0
        while not self.stop_event.is_set():
            ws = None
            epoch = str(uuid.uuid4())
            started = time.monotonic()
            try:
                ws, epoch = await self._open_subscribed(
                    group, reconnect_count, epoch
                )
                consecutive_failures = 0
                reconnect_count += 1
                while not self.stop_event.is_set():
                    raw = await self._recv_with_heartbeat(ws)
                    kind, payload = self._process_wire(group, epoch, raw)
                    if kind == "notice":
                        new_epoch = str(uuid.uuid4())
                        replacement, new_epoch = await self._open_subscribed(
                            group, reconnect_count, new_epoch
                        )
                        reconnect_count += 1
                        await ws.close(code=1000, reason="service upgrade handover")
                        ws, epoch = replacement, new_epoch
                        self.logger.emit(
                            "rolling_handover",
                            group_id=group.group_id,
                            conn_epoch=epoch,
                        )
                    elif kind == "pong":
                        self.store.connection_message(group.group_id, pong=True)
                    elif payload is not None:
                        now = time.monotonic()
                        previous = self._last_health_update.get(group.group_id, 0.0)
                        if now - previous >= HEARTBEAT_SECONDS:
                            self.store.connection_message(group.group_id)
                            self._last_health_update[group.group_id] = now
            except asyncio.CancelledError:
                self._invalidate_gap_sensitive_streams(
                    group, epoch, "planned_connection_rebuild"
                )
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()
                self.store.connection_failed(group.group_id, "cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - fault domain is one connection
                error = f"{type(exc).__name__}:{exc}"
                self._invalidate_gap_sensitive_streams(group, epoch, error)
                self.store.connection_failed(group.group_id, error)
                backoff_base = reconnect_delay_base(consecutive_failures)
                backoff_jitter = random.uniform(0.0, 0.35)
                delay = backoff_base + backoff_jitter
                self.logger.emit(
                    "connection_error",
                    group_id=group.group_id,
                    conn_epoch=epoch,
                    error=error[:1000],
                    consecutive_failure_attempt=consecutive_failures + 1,
                    backoff_base_seconds=backoff_base,
                    backoff_jitter_seconds=round(backoff_jitter, 3),
                    backoff_seconds=round(delay, 3),
                )
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()
                consecutive_failures += 1
                await _wait_or_stop(self.stop_event, delay)
            else:
                recovery_ms = int((time.monotonic() - started) * 1000)
                self.logger.emit(
                    "connection_closed",
                    group_id=group.group_id,
                    recovery_ms=recovery_ms,
                )

    async def _open_subscribed(
        self,
        group: ConnectionGroup,
        reconnect_count: int,
        epoch: str,
    ):
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise ServiceError("websockets_dependency_missing") from exc
        self.store.register_connection(
            group.group_id,
            group.endpoint,
            group.channels,
            epoch,
            group.args,
            reconnect_count,
        )
        await self.limiter.wait()
        connect_kwargs: dict[str, Any] = {
            "open_timeout": 20,
            "close_timeout": 5,
            "ping_interval": None,
            "max_size": 4 * 1024 * 1024,
            "max_queue": 4096,
            "compression": None,
        }
        if PROXY_URL:
            connect_kwargs["proxy"] = PROXY_URL
        started = time.monotonic()
        ws = await connect(group.endpoint, **connect_kwargs)
        await ws.send(canonical_json({"op": "subscribe", "args": list(group.args)}))
        expected = {subscription_arg_key(arg): arg for arg in group.args}
        acked: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
        while len(acked) < len(expected):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await ws.close()
                raise ServiceError(
                    f"subscription_ack_timeout:{len(acked)}/{len(expected)}"
                )
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if raw == "pong":
                continue
            payload = json.loads(raw)
            event = str(payload.get("event") or "")
            if event == "error":
                await ws.close()
                raise ServiceError(
                    f"subscription_error:{payload.get('code')}:{payload.get('msg')}"
                )
            if event == "subscribe" and isinstance(payload.get("arg"), dict):
                arg = dict(payload["arg"])
                key = subscription_arg_key(arg)
                if key in expected:
                    acked[key] = expected[key]
                continue
            if payload.get("data") and isinstance(payload.get("arg"), dict):
                self.state.ingest(
                    dict(payload["arg"]),
                    payload.get("action"),
                    payload.get("data") or [],
                    epoch,
                )
        acked_count = self.store.acknowledge_subscriptions(
            group.group_id, list(acked.values())
        )
        if acked_count != len(expected):
            await ws.close()
            raise ServiceError(f"subscription_ack_store_mismatch:{acked_count}/{len(expected)}")
        recovery_ms = int((time.monotonic() - started) * 1000)
        self.store.connection_ready(group.group_id, recovery_ms)
        self.logger.emit(
            "connection_ready",
            group_id=group.group_id,
            conn_epoch=epoch,
            endpoint=group.endpoint,
            args=len(group.args),
            payload_bytes=subscription_payload_size(group.args),
            recovery_ms=recovery_ms,
        )
        if "trades-all" in group.channels:
            symbols = [
                str(arg.get("instId") or "")
                for arg in group.args
                if arg.get("channel") == "trades-all" and arg.get("instId")
            ]
            self.store.mark_stream_incomplete(
                "trades-all", symbols, epoch, "rest_bootstrap_pending"
            )
            task = asyncio.create_task(
                self._bootstrap_recent_trades(group, epoch, symbols),
                name=f"okx-ws-trade-bootstrap-{group.group_id}",
            )
            self._bootstrap_tasks.add(task)
            task.add_done_callback(self._bootstrap_tasks.discard)
        return ws, epoch

    def _invalidate_gap_sensitive_streams(
        self,
        group: ConnectionGroup,
        epoch: str,
        reason: str,
    ) -> None:
        if "trades-all" not in group.channels:
            return
        symbols = [
            str(arg.get("instId") or "")
            for arg in group.args
            if arg.get("channel") == "trades-all" and arg.get("instId")
        ]
        self.store.mark_stream_incomplete(
            "trades-all", symbols, epoch, f"connection_gap:{reason}"
        )

    async def _bootstrap_recent_trades(
        self,
        group: ConnectionGroup,
        epoch: str,
        symbols: Sequence[str],
    ) -> None:
        outcomes: dict[str, dict] = {}
        try:
            data = await asyncio.to_thread(
                lambda: fetch_recent_trades_batch_sync(
                    symbols,
                    500,
                    35.0,
                    outcomes=outcomes,
                )
            )
            await asyncio.to_thread(
                self.store.insert_trade_batches,
                [
                    (symbol, data.get(symbol) or [], epoch)
                    for symbol in symbols
                ],
            )
            complete = [
                symbol
                for symbol in symbols
                if (outcomes.get(symbol) or {}).get("ok") is True
            ]
            changed = await asyncio.to_thread(
                self.store.mark_stream_complete,
                "trades-all",
                complete,
                epoch,
            )
            failed = [symbol for symbol in symbols if symbol not in set(complete)]
            self.logger.emit(
                "trade_rest_bootstrap",
                group_id=group.group_id,
                conn_epoch=epoch,
                requested=len(symbols),
                complete=len(complete),
                state_rows_changed=changed,
                rows=sum(len(data.get(symbol) or []) for symbol in symbols),
                failures=len(failed),
            )
            if failed:
                await asyncio.sleep(TRADE_BOOTSTRAP_RETRY_DELAY_SECONDS)
                retry_outcomes: dict[str, dict] = {}
                retry_data = await asyncio.to_thread(
                    lambda: fetch_recent_trades_batch_sync(
                        failed,
                        500,
                        TRADE_BOOTSTRAP_RETRY_TIMEOUT_SECONDS,
                        outcomes=retry_outcomes,
                    )
                )
                await asyncio.to_thread(
                    self.store.insert_trade_batches,
                    [
                        (symbol, retry_data.get(symbol) or [], epoch)
                        for symbol in failed
                    ],
                )
                recovered = [
                    symbol
                    for symbol in failed
                    if (retry_outcomes.get(symbol) or {}).get("ok") is True
                ]
                retry_changed = await asyncio.to_thread(
                    self.store.mark_stream_complete,
                    "trades-all",
                    recovered,
                    epoch,
                )
                self.logger.emit(
                    "trade_rest_bootstrap_retry",
                    group_id=group.group_id,
                    conn_epoch=epoch,
                    requested=len(failed),
                    recovered=len(recovered),
                    remaining=len(failed) - len(recovered),
                    state_rows_changed=retry_changed,
                    rows=sum(
                        len(retry_data.get(symbol) or []) for symbol in failed
                    ),
                    bounded_attempts=2,
                )
        except Exception as exc:  # noqa: BLE001 - incomplete state remains fail-safe
            self.logger.emit(
                "trade_rest_bootstrap_error",
                group_id=group.group_id,
                conn_epoch=epoch,
                requested=len(symbols),
                error=f"{type(exc).__name__}:{exc}"[:1000],
            )

    async def _recv_with_heartbeat(self, ws):
        try:
            return await asyncio.wait_for(ws.recv(), timeout=PING_IDLE_SECONDS)
        except TimeoutError:
            await ws.send("ping")
            return await asyncio.wait_for(ws.recv(), timeout=PONG_TIMEOUT_SECONDS)

    def _process_wire(
        self,
        group: ConnectionGroup,
        epoch: str,
        raw: str | bytes,
    ) -> tuple[str, dict[str, Any] | None]:
        if raw == "pong" or raw == b"pong":
            return "pong", None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        event = str(payload.get("event") or "")
        if event == "notice":
            return "notice", payload
        if event == "error":
            raise ServiceError(
                f"ws_event_error:{payload.get('code')}:{payload.get('msg')}"
            )
        if payload.get("data") and isinstance(payload.get("arg"), dict):
            self.state.ingest(
                dict(payload["arg"]),
                payload.get("action"),
                payload.get("data") or [],
                epoch,
            )
            return "data", payload
        return "control", payload


async def flush_loop(
    store: CacheStore,
    state: StreamState,
    logger: StructuredLog,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        await _wait_or_stop(stop_event, FLUSH_INTERVAL_SECONDS)
        pending = state.take_pending()
        if not any(pending[key] for key in pending):
            continue
        try:
            wrote = await asyncio.to_thread(flush_pending, store, pending)
            if wrote["candles"] or wrote["instruments"]:
                logger.emit("cache_flush", wrote=wrote)
        except Exception as exc:  # noqa: BLE001 - cache fault must be visible
            state.required_drops += sum(len(value) for value in pending.values())
            logger.emit(
                "cache_flush_error",
                error=f"{type(exc).__name__}:{exc}",
                dropped={key: len(value) for key, value in pending.items()},
            )


async def heartbeat_loop(
    store: CacheStore,
    state: StreamState,
    stop_event: asyncio.Event,
) -> None:
    previous_wall = time.monotonic()
    previous_cpu = time.process_time()
    while not stop_event.is_set():
        now_wall = time.monotonic()
        now_cpu = time.process_time()
        wall_delta = max(0.001, now_wall - previous_wall)
        cpu_cores = max(0.0, (now_cpu - previous_cpu) / wall_delta)
        previous_wall, previous_cpu = now_wall, now_cpu
        counts = state.pending_counts()
        await asyncio.to_thread(
            store.heartbeat_sample,
            pid=os.getpid(),
            rss_bytes=_process_rss_bytes(),
            cpu_cores=round(cpu_cores, 6),
            required_drops=state.required_drops,
            **counts,
        )
        await _wait_or_stop(stop_event, HEARTBEAT_SECONDS)


async def manage_groups(
    kind: str,
    reader: CacheReader,
    market_db: Path,
    runner: ConnectionRunner,
    stop_event: asyncio.Event,
    logger: StructuredLog,
) -> None:
    current_hash = ""
    tasks: list[asyncio.Task] = []
    refresh = STATIC_REFRESH_SECONDS if kind == "static" else DYNAMIC_REFRESH_SECONDS
    try:
        while not stop_event.is_set():
            symbols = (
                reader.live_symbols()
                if kind == "static"
                else _latest_feature_symbols(market_db)
            )
            next_hash = universe_sha256(symbols)
            if next_hash != current_hash:
                await _cancel_tasks(tasks)
                groups = (
                    build_static_groups(symbols)
                    if kind == "static"
                    else build_dynamic_groups(symbols)
                )
                tasks = [
                    asyncio.create_task(
                        runner.run(group), name=f"okx-ws-{group.group_id}"
                    )
                    for group in groups
                ]
                current_hash = next_hash
                logger.emit(
                    "groups_rebuilt",
                    kind=kind,
                    symbols=len(symbols),
                    universe_sha256=current_hash,
                    groups=len(groups),
                    subscriptions=sum(len(group.args) for group in groups),
                )
            await _wait_or_stop(stop_event, refresh)
    finally:
        await _cancel_tasks(tasks)


async def run_service(
    cache_db: Path,
    market_db: Path,
    log_dir: Path,
) -> int:
    logger = StructuredLog(log_dir)
    store = CacheStore(cache_db)
    reader = CacheReader(cache_db)
    stop_event = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop_event.set)
        instruments, bootstrap_source = await asyncio.to_thread(
            _bootstrap_instruments, market_db
        )
        store.upsert_instruments(instruments, bootstrap_source)
        live_symbols = _live_symbols_from_instruments(instruments)
        existing_count = reader.health_snapshot()["counts"]["candles"]
        if existing_count == 0 and market_db.exists() and live_symbols:
            seeded = await asyncio.to_thread(
                store.seed_candles_from_market_db, market_db, live_symbols
            )
            logger.emit("local_candle_seed", rows=seeded, symbols=len(live_symbols))
        store.set_meta("service_started_at", utc_now_iso())
        store.set_meta("bootstrap_source", bootstrap_source)
        store.set_meta("universe_sha256", universe_sha256(live_symbols))
        state = StreamState(store)
        limiter = ConnectionRateLimiter()
        runner = ConnectionRunner(store, state, limiter, logger, stop_event)
        tasks = [
            asyncio.create_task(flush_loop(store, state, logger, stop_event)),
            asyncio.create_task(heartbeat_loop(store, state, stop_event)),
            asyncio.create_task(
                manage_groups("static", reader, market_db, runner, stop_event, logger)
            ),
            asyncio.create_task(
                manage_groups("dynamic", reader, market_db, runner, stop_event, logger)
            ),
        ]
        logger.emit(
            "service_started",
            pid=os.getpid(),
            cache_db=str(cache_db),
            market_db=str(market_db),
            live_symbols=len(live_symbols),
            bootstrap_source=bootstrap_source,
            proxy_configured=bool(PROXY_URL),
        )
        await stop_event.wait()
        await _cancel_tasks(tasks)
        final_pending = state.take_pending()
        if any(final_pending[key] for key in final_pending):
            await asyncio.to_thread(flush_pending, store, final_pending)
        logger.emit("service_stopped", pid=os.getpid())
        return 0
    finally:
        with contextlib.suppress(Exception):
            store.mark_stopped()
        store.close()


def _bootstrap_instruments(market_db: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        rows = fetch_instruments_sync("SWAP")
        if not rows:
            raise ServiceError("official_instruments_empty")
        return rows, "okx_rest_bootstrap"
    except Exception as exc:  # noqa: BLE001 - shadow may use local fallback
        rows = _market_instrument_fallback(market_db)
        if not rows:
            raise ServiceError(
                f"instrument_bootstrap_failed:{type(exc).__name__}:{exc}"
            ) from exc
        return rows, f"market_db_fallback:{type(exc).__name__}"


def _market_instrument_fallback(market_db: Path) -> list[dict[str, Any]]:
    if not market_db.exists():
        return []
    connection = sqlite3.connect(
        f"file:{market_db.as_posix()}?mode=ro", uri=True, timeout=10
    )
    try:
        rows = connection.execute(
            "SELECT instId,ctVal,lotSz,state,inst_category FROM instruments_cache"
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "instId": row[0],
            "ctVal": row[1],
            "lotSz": row[2],
            "state": row[3],
            "instCategory": row[4],
            "instType": "SWAP",
            "settleCcy": "USDT",
            "quoteCcy": "USDT",
            "ctType": "linear",
        }
        for row in rows
    ]


def _live_symbols_from_instruments(rows: Sequence[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(row.get("instId"))
            for row in rows
            if row.get("instId")
            and row.get("instType") == "SWAP"
            and row.get("settleCcy") == "USDT"
            and row.get("ctType") == "linear"
            and row.get("state") == "live"
        }
    )


def _latest_feature_symbols(market_db: Path) -> list[str]:
    if not market_db.exists():
        return []
    connection = sqlite3.connect(
        f"file:{market_db.as_posix()}?mode=ro", uri=True, timeout=5
    )
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='market_feature_selection_rows'"
        ).fetchone()
        if not table:
            return []
        cycle = connection.execute(
            "SELECT cycle_id FROM market_feature_selection_rows "
            "ORDER BY cycle_id DESC LIMIT 1"
        ).fetchone()
        if not cycle:
            return []
        rows = connection.execute(
            "SELECT symbol FROM market_feature_selection_rows "
            "WHERE cycle_id=? ORDER BY selection_rank LIMIT 100",
            (cycle[0],),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


def _read_live_symbols(market_db: Path) -> list[str]:
    return [
        str(row["instId"])
        for row in _market_instrument_fallback(market_db)
        if row.get("state") == "live"
    ]


def _process_rss_bytes() -> int | None:
    if os.name != "nt":
        try:
            import resource

            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
        except Exception:
            return None

    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_memory = psapi.GetProcessMemoryInfo
    get_memory.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
        wintypes.DWORD,
    ]
    get_memory.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    ok = get_memory(process, ctypes.byref(counters), counters.cb)
    return int(counters.WorkingSetSize) if ok else None


async def _wait_or_stop(stop_event: asyncio.Event, timeout_s: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, timeout_s))
    except TimeoutError:
        return


async def _cancel_tasks(tasks: Sequence[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_configuration(market_db: Path) -> dict[str, Any]:
    symbols = _read_live_symbols(market_db)
    dynamic_symbols = _latest_feature_symbols(market_db)
    groups = [*build_static_groups(symbols), *build_dynamic_groups(dynamic_symbols)]
    return {
        "ok": bool(symbols) and all(
            subscription_payload_size(group.args) <= SUBSCRIPTION_MAX_BYTES
            for group in groups
        ),
        "mode": "validate_only",
        "live_symbols": len(symbols),
        "dynamic_symbols": len(dynamic_symbols),
        "groups": [
            {
                "group_id": group.group_id,
                "endpoint": group.endpoint,
                "channels": group.channels,
                "args": len(group.args),
                "payload_bytes": subscription_payload_size(group.args),
            }
            for group in groups
        ],
        "max_payload_bytes": max(
            (subscription_payload_size(group.args) for group in groups), default=0
        ),
        "limit_bytes": SUBSCRIPTION_MAX_BYTES,
        "proxy_configured": bool(PROXY_URL),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="OKX 公共行情 WebSocket 缓存服务")
    parser.add_argument("--cache-db", default=str(DEFAULT_CACHE_DB))
    parser.add_argument("--market-db", default=str(DEFAULT_MARKET_DB))
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    market_db = Path(args.market_db)
    if args.validate_only:
        print(canonical_json(validate_configuration(market_db)))
        return 0
    cache_db = Path(args.cache_db)
    lock_path = cache_db.with_suffix(cache_db.suffix + ".lock")
    try:
        with SingleInstanceLock(lock_path):
            return asyncio.run(
                run_service(cache_db, market_db, Path(args.log_dir))
            )
    except ServiceError as exc:
        print(
            canonical_json({"ok": False, "error": str(exc), "pid": os.getpid()}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
