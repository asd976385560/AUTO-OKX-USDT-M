# -*- coding: utf-8 -*-
"""OKX 公共行情 WebSocket 的可重建影子缓存。

此模块只操作 ``ws_market_cache.db``。它绝不写 ``market.db``，因此现有
fast/slow/features writer 仍是生产行情库的唯一写方。
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
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(os.environ.get("OKX_ROOT", _public_project_path()))
DEFAULT_CACHE_DB = Path(
    os.environ.get("OKX_WS_CACHE_DB", str(ROOT / "db" / "ws_market_cache.db"))
)
SCHEMA_PATH = ROOT / "db" / "ws_market_cache_schema.sql"
CST = timezone(timedelta(hours=8))
SUPPORTED_BARS = ("15m", "1H", "4H", "1D", "1W", "1M")
CANDLE_RETENTION = 120
TRADE_RETENTION = 500


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def universe_sha256(symbols: Iterable[str]) -> str:
    normalized = sorted({str(item).strip().upper() for item in symbols if str(item).strip()})
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def ms_to_iso(value: int | str | None) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return None


def _month_start(local: datetime) -> datetime:
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def expected_closed_start_ms(
    bar: str,
    at: datetime | None = None,
) -> int:
    """返回 ``at`` 时刻已经闭合的最新 OKX 非-utc K线起点。

    OKX 的 ``1D/1W/1M`` 使用 UTC+8 边界；较短周期也按同一地区边界
    计算，避免未来扩展时把 ``*utc`` 语义混入现行合同。
    """
    if bar not in SUPPORTED_BARS:
        raise ValueError(f"unsupported OKX candle bar: {bar}")
    current = (at or utc_now()).astimezone(CST)
    if bar == "15m":
        start = current.replace(
            minute=(current.minute // 15) * 15,
            second=0,
            microsecond=0,
        ) - timedelta(minutes=15)
    elif bar == "1H":
        start = current.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    elif bar == "4H":
        start = current.replace(
            hour=(current.hour // 4) * 4,
            minute=0,
            second=0,
            microsecond=0,
        ) - timedelta(hours=4)
    elif bar == "1D":
        start = current.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    elif bar == "1W":
        this_week = (
            current.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=current.weekday())
        )
        start = this_week - timedelta(days=7)
    else:
        this_month = _month_start(current)
        prior_day = this_month - timedelta(days=1)
        start = _month_start(prior_day)
    return int(start.astimezone(timezone.utc).timestamp() * 1000)


def candle_bar_end_ms(bar: str, start_ms: int) -> int:
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).astimezone(CST)
    if bar == "15m":
        end = start + timedelta(minutes=15)
    elif bar == "1H":
        end = start + timedelta(hours=1)
    elif bar == "4H":
        end = start + timedelta(hours=4)
    elif bar == "1D":
        end = start + timedelta(days=1)
    elif bar == "1W":
        end = start + timedelta(days=7)
    elif bar == "1M":
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1, day=1)
        else:
            end = start.replace(month=start.month + 1, day=1)
    else:
        raise ValueError(f"unsupported OKX candle bar: {bar}")
    return int(end.astimezone(timezone.utc).timestamp() * 1000)


def subscription_payload_size(args: Sequence[dict[str, Any]]) -> int:
    payload = {"op": "subscribe", "args": list(args)}
    return len(canonical_json(payload).encode("utf-8"))


def shard_subscription_args(
    args: Sequence[dict[str, Any]],
    max_bytes: int = 48 * 1024,
) -> list[list[dict[str, Any]]]:
    if max_bytes <= subscription_payload_size([]):
        raise ValueError("subscription byte limit is too small")
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for arg in args:
        candidate = [*current, dict(arg)]
        if subscription_payload_size(candidate) <= max_bytes:
            current = candidate
            continue
        if not current:
            raise ValueError(f"single subscription arg exceeds {max_bytes} bytes: {arg!r}")
        shards.append(current)
        current = [dict(arg)]
        if subscription_payload_size(current) > max_bytes:
            raise ValueError(f"single subscription arg exceeds {max_bytes} bytes: {arg!r}")
    if current:
        shards.append(current)
    return shards


def subscription_arg_key(arg: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(arg).encode("utf-8")).hexdigest()


def signed_crc32(value: str) -> int:
    import zlib

    raw = zlib.crc32(value.encode("utf-8"))
    return raw - 2**32 if raw >= 2**31 else raw


def orderbook_checksum(bids: Sequence[Sequence[Any]], asks: Sequence[Sequence[Any]]) -> int:
    parts: list[str] = []
    for index in range(max(min(25, len(bids)), min(25, len(asks)))):
        if index < min(25, len(bids)):
            parts.extend((str(bids[index][0]), str(bids[index][1])))
        if index < min(25, len(asks)):
            parts.extend((str(asks[index][0]), str(asks[index][1])))
    return signed_crc32(":".join(parts))


def apply_orderbook_update(
    current_bids: Sequence[Sequence[Any]],
    current_asks: Sequence[Sequence[Any]],
    update_bids: Sequence[Sequence[Any]],
    update_asks: Sequence[Sequence[Any]],
) -> tuple[list[list[Any]], list[list[Any]]]:
    def merge(
        current: Sequence[Sequence[Any]],
        updates: Sequence[Sequence[Any]],
        reverse: bool,
    ) -> list[list[Any]]:
        levels = {str(row[0]): list(row) for row in current if len(row) >= 2}
        for row in updates:
            if len(row) < 2:
                continue
            price = str(row[0])
            try:
                remove = float(row[1]) == 0
            except (TypeError, ValueError):
                remove = False
            if remove:
                levels.pop(price, None)
            else:
                levels[price] = list(row)
        return sorted(
            levels.values(),
            key=lambda row: float(row[0]),
            reverse=reverse,
        )[:400]

    return (
        merge(current_bids, update_bids, True),
        merge(current_asks, update_asks, False),
    )


@dataclass(frozen=True)
class CacheHealth:
    healthy: bool
    heartbeat_age_s: float | None
    service_status: str | None
    reason: str | None


class CacheStore:
    """单写者缓存。一个服务进程只创建一个实例。"""

    def __init__(self, path: Path | str = DEFAULT_CACHE_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), timeout=10, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        self._connection.executescript(schema)
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def set_meta(self, key: str, value: Any) -> None:
        now = utc_now_iso()
        encoded = value if isinstance(value, str) else canonical_json(value)
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value,updated_at) VALUES(?,?,?)",
                (key, encoded, now),
            )
            self._connection.commit()

    def upsert_instruments(
        self,
        rows: Sequence[dict[str, Any]],
        source: str,
    ) -> int:
        now = utc_now_iso()
        values = []
        for row in rows:
            inst_id = str(row.get("instId") or "").strip().upper()
            if not inst_id:
                continue
            values.append(
                (
                    inst_id,
                    str(row.get("state") or "") or None,
                    str(row.get("settleCcy") or "") or None,
                    str(row.get("ctType") or "") or None,
                    _safe_int(row.get("ts") or row.get("uTime")),
                    now,
                    source,
                    canonical_json(row),
                )
            )
        with self._lock:
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO instruments(
                    inst_id,state,settle_ccy,ct_type,exchange_ts,received_at,source,payload_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                values,
            )
            self._connection.commit()
        return len(values)

    def upsert_latest(
        self,
        channel: str,
        rows: Sequence[dict[str, Any]],
        conn_epoch: str,
    ) -> int:
        now = utc_now_iso()
        values = []
        for row in rows:
            inst_id = str(row.get("instId") or "").strip().upper()
            if not inst_id:
                continue
            values.append(
                (
                    channel,
                    inst_id,
                    _safe_int(row.get("ts")),
                    now,
                    conn_epoch,
                    canonical_json(row),
                )
            )
        with self._lock:
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO latest_market(
                    channel,inst_id,exchange_ts,received_at,conn_epoch,payload_json
                ) VALUES(?,?,?,?,?,?)
                """,
                values,
            )
            self._connection.commit()
        return len(values)

    def upsert_candles(
        self,
        inst_id: str,
        timeframe: str,
        rows: Sequence[Sequence[Any]],
        conn_epoch: str,
        source: str = "ws",
    ) -> int:
        if timeframe not in SUPPORTED_BARS:
            raise ValueError(f"unsupported candle timeframe: {timeframe}")
        now = utc_now_iso()
        received_ms = int(time.time() * 1000)
        values = []
        for row in rows:
            if len(row) < 9 or str(row[8]) != "1":
                continue
            ts_ms = _safe_int(row[0])
            if ts_ms is None:
                continue
            end_ms = candle_bar_end_ms(timeframe, ts_ms)
            values.append(
                (
                    inst_id,
                    timeframe,
                    ts_ms,
                    _safe_text(row, 1),
                    _safe_text(row, 2),
                    _safe_text(row, 3),
                    _safe_text(row, 4),
                    _safe_text(row, 5),
                    _safe_text(row, 6),
                    _safe_text(row, 7),
                    1,
                    end_ms,
                    max(0, received_ms - end_ms),
                    now,
                    conn_epoch,
                    source,
                )
            )
        if not values:
            return 0
        with self._lock:
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO candles(
                    inst_id,timeframe,ts_ms,open,high,low,close,volume,volume_ccy,
                    volume_quote,confirm,bar_end_ms,close_latency_ms,received_at,
                    conn_epoch,source
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            self._connection.execute(
                """
                DELETE FROM candles
                WHERE inst_id=? AND timeframe=? AND ts_ms NOT IN (
                    SELECT ts_ms FROM candles
                    WHERE inst_id=? AND timeframe=?
                    ORDER BY ts_ms DESC LIMIT ?
                )
                """,
                (inst_id, timeframe, inst_id, timeframe, CANDLE_RETENTION),
            )
            self._connection.commit()
        return len(values)

    def upsert_candle_batches(
        self,
        entries: Sequence[
            tuple[str, str, Sequence[Sequence[Any]], str, str]
        ],
    ) -> int:
        now = utc_now_iso()
        received_ms = int(time.time() * 1000)
        values = []
        touched: set[tuple[str, str]] = set()
        for inst_id, timeframe, rows, conn_epoch, source in entries:
            if timeframe not in SUPPORTED_BARS:
                continue
            for row in rows:
                if len(row) < 9 or str(row[8]) != "1":
                    continue
                ts_ms = _safe_int(row[0])
                if ts_ms is None:
                    continue
                end_ms = candle_bar_end_ms(timeframe, ts_ms)
                values.append(
                    (
                        inst_id,
                        timeframe,
                        ts_ms,
                        _safe_text(row, 1),
                        _safe_text(row, 2),
                        _safe_text(row, 3),
                        _safe_text(row, 4),
                        _safe_text(row, 5),
                        _safe_text(row, 6),
                        _safe_text(row, 7),
                        1,
                        end_ms,
                        max(0, received_ms - end_ms),
                        now,
                        conn_epoch,
                        source,
                    )
                )
                touched.add((inst_id, timeframe))
        with self._lock:
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO candles(
                    inst_id,timeframe,ts_ms,open,high,low,close,volume,volume_ccy,
                    volume_quote,confirm,bar_end_ms,close_latency_ms,received_at,
                    conn_epoch,source
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            for inst_id, timeframe in touched:
                self._connection.execute(
                    """
                    DELETE FROM candles
                    WHERE inst_id=? AND timeframe=? AND ts_ms NOT IN (
                        SELECT ts_ms FROM candles
                        WHERE inst_id=? AND timeframe=?
                        ORDER BY ts_ms DESC LIMIT ?
                    )
                    """,
                    (
                        inst_id,
                        timeframe,
                        inst_id,
                        timeframe,
                        CANDLE_RETENTION,
                    ),
                )
            self._connection.commit()
        return len(values)

    def upsert_book(
        self,
        inst_id: str,
        book: dict[str, Any],
        conn_epoch: str,
        valid: bool = True,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO books(
                    inst_id,exchange_ts,seq_id,checksum,valid,received_at,conn_epoch,
                    bids_json,asks_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    inst_id,
                    _safe_int(book.get("ts")),
                    _safe_int(book.get("seqId")),
                    _safe_int(book.get("checksum")),
                    int(bool(valid)),
                    now,
                    conn_epoch,
                    canonical_json((book.get("bids") or [])[:50]),
                    canonical_json((book.get("asks") or [])[:50]),
                ),
            )
            self._connection.commit()

    def invalidate_book(self, inst_id: str, conn_epoch: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE books SET valid=0,received_at=?,conn_epoch=? WHERE inst_id=?",
                (utc_now_iso(), conn_epoch, inst_id),
            )
            self._connection.commit()

    def insert_trades(
        self,
        inst_id: str,
        rows: Sequence[dict[str, Any]],
        conn_epoch: str,
    ) -> int:
        now = utc_now_iso()
        values = []
        for row in rows:
            trade_id = str(row.get("tradeId") or "").strip()
            ts_ms = _safe_int(row.get("ts"))
            if not trade_id or ts_ms is None:
                continue
            values.append(
                (inst_id, trade_id, ts_ms, now, conn_epoch, canonical_json(row))
            )
        with self._lock:
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO trades(
                    inst_id,trade_id,exchange_ts,received_at,conn_epoch,payload_json
                ) VALUES(?,?,?,?,?,?)
                """,
                values,
            )
            self._connection.execute(
                """
                DELETE FROM trades
                WHERE inst_id=? AND trade_id NOT IN (
                    SELECT trade_id FROM trades WHERE inst_id=?
                    ORDER BY exchange_ts DESC, trade_id DESC LIMIT ?
                )
                """,
                (inst_id, inst_id, TRADE_RETENTION),
            )
            self._connection.commit()
        return len(values)

    def register_connection(
        self,
        group_id: str,
        endpoint: str,
        channels: Sequence[str],
        conn_epoch: str,
        args: Sequence[dict[str, Any]],
        reconnect_count: int,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO connection_health(
                    group_id,endpoint,channels_json,status,conn_epoch,connected_at,
                    disconnected_at,last_message_at,last_pong_at,expected_args,
                    acked_args,reconnect_count,recovery_ms,last_error,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    group_id,
                    endpoint,
                    canonical_json(sorted(set(channels))),
                    "connecting",
                    conn_epoch,
                    now,
                    None,
                    None,
                    None,
                    len(args),
                    0,
                    reconnect_count,
                    None,
                    None,
                    now,
                ),
            )
            self._connection.execute(
                "DELETE FROM subscriptions WHERE group_id=?", (group_id,)
            )
            self._connection.executemany(
                """
                INSERT INTO subscriptions(
                    group_id,arg_key,channel,inst_id,status,updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                [
                    (
                        group_id,
                        subscription_arg_key(arg),
                        str(arg.get("channel") or ""),
                        str(arg.get("instId") or ""),
                        "pending",
                        now,
                    )
                    for arg in args
                ],
            )
            self._connection.commit()

    def acknowledge_subscription(self, group_id: str, arg: dict[str, Any]) -> int:
        key = subscription_arg_key(arg)
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                "UPDATE subscriptions SET status='acked',updated_at=? "
                "WHERE group_id=? AND arg_key=?",
                (now, group_id, key),
            )
            acked = int(
                self._connection.execute(
                    "SELECT count(*) FROM subscriptions "
                    "WHERE group_id=? AND status='acked'",
                    (group_id,),
                ).fetchone()[0]
            )
            self._connection.execute(
                "UPDATE connection_health SET acked_args=?,updated_at=? WHERE group_id=?",
                (acked, now, group_id),
            )
            self._connection.commit()
        return acked

    def acknowledge_subscriptions(
        self,
        group_id: str,
        args: Sequence[dict[str, Any]],
    ) -> int:
        now = utc_now_iso()
        keys = [subscription_arg_key(arg) for arg in args]
        with self._lock:
            self._connection.executemany(
                "UPDATE subscriptions SET status='acked',updated_at=? "
                "WHERE group_id=? AND arg_key=?",
                [(now, group_id, key) for key in keys],
            )
            acked = int(
                self._connection.execute(
                    "SELECT count(*) FROM subscriptions "
                    "WHERE group_id=? AND status='acked'",
                    (group_id,),
                ).fetchone()[0]
            )
            self._connection.execute(
                "UPDATE connection_health SET acked_args=?,updated_at=? WHERE group_id=?",
                (acked, now, group_id),
            )
            self._connection.commit()
        return acked

    def upsert_books(
        self,
        entries: Sequence[tuple[str, dict[str, Any], str, bool]],
    ) -> int:
        now = utc_now_iso()
        values = []
        for inst_id, book, conn_epoch, valid in entries:
            values.append(
                (
                    inst_id,
                    _safe_int(book.get("ts")),
                    _safe_int(book.get("seqId")),
                    _safe_int(book.get("checksum")),
                    int(bool(valid)),
                    now,
                    conn_epoch,
                    canonical_json((book.get("bids") or [])[:50]),
                    canonical_json((book.get("asks") or [])[:50]),
                )
            )
        with self._lock:
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO books(
                    inst_id,exchange_ts,seq_id,checksum,valid,received_at,conn_epoch,
                    bids_json,asks_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            self._connection.commit()
        return len(values)

    def insert_trade_batches(
        self,
        entries: Sequence[tuple[str, Sequence[dict[str, Any]], str]],
    ) -> int:
        now = utc_now_iso()
        values = []
        touched: set[str] = set()
        for inst_id, rows, conn_epoch in entries:
            touched.add(inst_id)
            for row in rows:
                trade_id = str(row.get("tradeId") or "").strip()
                ts_ms = _safe_int(row.get("ts"))
                if not trade_id or ts_ms is None:
                    continue
                values.append(
                    (
                        inst_id,
                        trade_id,
                        ts_ms,
                        now,
                        conn_epoch,
                        canonical_json(row),
                    )
                )
        with self._lock:
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO trades(
                    inst_id,trade_id,exchange_ts,received_at,conn_epoch,payload_json
                ) VALUES(?,?,?,?,?,?)
                """,
                values,
            )
            for inst_id in touched:
                self._connection.execute(
                    """
                    DELETE FROM trades
                    WHERE inst_id=? AND trade_id NOT IN (
                        SELECT trade_id FROM trades WHERE inst_id=?
                        ORDER BY exchange_ts DESC, trade_id DESC LIMIT ?
                    )
                    """,
                    (inst_id, inst_id, TRADE_RETENTION),
                )
            self._connection.commit()
        return len(values)

    def connection_ready(self, group_id: str, recovery_ms: int | None = None) -> None:
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                """
                UPDATE connection_health
                SET status='connected',connected_at=coalesce(connected_at,?),
                    recovery_ms=?,last_error=NULL,updated_at=?
                WHERE group_id=?
                """,
                (now, recovery_ms, now, group_id),
            )
            self._connection.execute(
                "INSERT INTO connection_events(ts,group_id,conn_epoch,event,duration_ms,detail) "
                "SELECT ?,group_id,conn_epoch,'ready',?,NULL FROM connection_health "
                "WHERE group_id=?",
                (now, recovery_ms, group_id),
            )
            self._connection.commit()

    def mark_stream_incomplete(
        self,
        channel: str,
        inst_ids: Sequence[str],
        conn_epoch: str,
        reason: str,
    ) -> int:
        now = utc_now_iso()
        rows = [
            (channel, str(inst_id), "incomplete", reason[:500], conn_epoch, now)
            for inst_id in sorted({str(item) for item in inst_ids if str(item)})
        ]
        with self._lock:
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO stream_completeness(
                    channel,inst_id,status,reason,conn_epoch,updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                rows,
            )
            self._connection.commit()
        return len(rows)

    def mark_stream_complete(
        self,
        channel: str,
        inst_ids: Sequence[str],
        conn_epoch: str,
    ) -> int:
        now = utc_now_iso()
        rows = sorted({str(item) for item in inst_ids if str(item)})
        changed = 0
        with self._lock:
            for inst_id in rows:
                cursor = self._connection.execute(
                    """
                    UPDATE stream_completeness
                    SET status='complete',reason=NULL,updated_at=?
                    WHERE channel=? AND inst_id=? AND conn_epoch=?
                    """,
                    (now, channel, inst_id, conn_epoch),
                )
                changed += max(0, int(cursor.rowcount or 0))
            self._connection.commit()
        return changed

    def connection_message(self, group_id: str, pong: bool = False) -> None:
        now = utc_now_iso()
        sql = (
            "UPDATE connection_health SET last_message_at=?,last_pong_at=?,updated_at=? "
            "WHERE group_id=?"
            if pong
            else "UPDATE connection_health SET last_message_at=?,updated_at=? WHERE group_id=?"
        )
        params = (now, now, now, group_id) if pong else (now, now, group_id)
        with self._lock:
            self._connection.execute(sql, params)
            self._connection.commit()

    def connection_failed(self, group_id: str, error: str) -> None:
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                """
                UPDATE connection_health
                SET status='disconnected',disconnected_at=?,last_error=?,updated_at=?
                WHERE group_id=?
                """,
                (now, error[:1000], now, group_id),
            )
            self._connection.execute(
                "UPDATE subscriptions SET status='stale',updated_at=? WHERE group_id=?",
                (now, group_id),
            )
            self._connection.execute(
                "INSERT INTO connection_events(ts,group_id,conn_epoch,event,detail) "
                "SELECT ?,group_id,conn_epoch,'disconnected',? FROM connection_health "
                "WHERE group_id=?",
                (now, error[:1000], group_id),
            )
            self._connection.commit()

    def heartbeat_sample(
        self,
        *,
        pid: int,
        rss_bytes: int | None,
        cpu_cores: float | None,
        pending_latest: int,
        pending_candles: int,
        pending_books: int,
        pending_trades: int,
        required_drops: int = 0,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value,updated_at) VALUES('heartbeat',?,?)",
                (now, now),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value,updated_at) VALUES('service_status','running',?)",
                (now,),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value,updated_at) VALUES('pid',?,?)",
                (str(pid), now),
            )
            self._connection.execute(
                """
                INSERT OR REPLACE INTO service_samples(
                    ts,pid,rss_bytes,cpu_cores,pending_latest,pending_candles,
                    pending_books,pending_trades,required_drops
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    now,
                    pid,
                    rss_bytes,
                    cpu_cores,
                    pending_latest,
                    pending_candles,
                    pending_books,
                    pending_trades,
                    required_drops,
                ),
            )
            self._connection.execute(
                "DELETE FROM service_samples "
                "WHERE datetime(ts) < datetime('now','-8 days')"
            )
            self._connection.execute(
                "DELETE FROM connection_events "
                "WHERE datetime(ts) < datetime('now','-8 days')"
            )
            self._connection.commit()

    def mark_stopped(self) -> None:
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value,updated_at) "
                "VALUES('service_status','stopped',?)",
                (now,),
            )
            self._connection.commit()

    def quick_check(self) -> str:
        with self._lock:
            return str(self._connection.execute("PRAGMA quick_check").fetchone()[0])

    def seed_candles_from_market_db(
        self,
        market_db: Path | str,
        symbols: Sequence[str],
    ) -> int:
        """用现有权威缓存减轻冷启动；仅保留当时已闭合的历史柱。"""
        source = sqlite3.connect(
            f"file:{Path(market_db).as_posix()}?mode=ro", uri=True, timeout=20
        )
        source.row_factory = sqlite3.Row
        inserted = 0
        pending: list[
            tuple[str, str, Sequence[Sequence[Any]], str, str]
        ] = []
        try:
            for symbol in symbols:
                for bar in SUPPORTED_BARS:
                    expected = expected_closed_start_ms(bar)
                    rows = source.execute(
                        """
                        SELECT ts,o,h,l,c,v FROM kline_cache
                        WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?
                        """,
                        (symbol, bar, CANDLE_RETENTION),
                    ).fetchall()
                    candle_rows = []
                    for row in reversed(rows):
                        ts_ms = iso_to_ms(row["ts"])
                        if ts_ms is None or ts_ms > expected:
                            continue
                        candle_rows.append(
                            [
                                str(ts_ms),
                                _none_or_text(row["o"]),
                                _none_or_text(row["h"]),
                                _none_or_text(row["l"]),
                                _none_or_text(row["c"]),
                                None,
                                None,
                                _none_or_text(row["v"]),
                                "1",
                            ]
                        )
                    if candle_rows:
                        pending.append(
                            (
                                symbol,
                                bar,
                                candle_rows,
                                "local-seed",
                                "market_db_seed",
                            )
                        )
                    if len(pending) >= 24:
                        inserted += self.upsert_candle_batches(pending)
                        pending = []
            if pending:
                inserted += self.upsert_candle_batches(pending)
        finally:
            source.close()
        return inserted


class CacheReader:
    def __init__(self, path: Path | str = DEFAULT_CACHE_DB) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _readonly(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def health(self, max_heartbeat_age_s: float = 60.0) -> CacheHealth:
        if not self.path.exists():
            return CacheHealth(False, None, None, "cache_missing")
        try:
            with self._readonly() as connection:
                rows = {
                    row["key"]: row["value"]
                    for row in connection.execute(
                        "SELECT key,value FROM cache_meta WHERE key IN ('heartbeat','service_status')"
                    )
                }
        except sqlite3.Error as exc:
            return CacheHealth(False, None, None, f"cache_error:{type(exc).__name__}")
        status = rows.get("service_status")
        heartbeat = rows.get("heartbeat")
        heartbeat_ms = iso_to_ms(heartbeat)
        age = (
            max(0.0, time.time() - heartbeat_ms / 1000)
            if heartbeat_ms is not None
            else None
        )
        if status != "running":
            return CacheHealth(False, age, status, "service_not_running")
        if age is None or age > max_heartbeat_age_s:
            return CacheHealth(False, age, status, "heartbeat_stale")
        return CacheHealth(True, age, status, None)

    def read_instruments(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        del inst_type
        with self._readonly() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM instruments ORDER BY inst_id"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def live_symbols(self) -> list[str]:
        with self._readonly() as connection:
            rows = connection.execute(
                """
                SELECT inst_id FROM instruments
                WHERE state='live' AND settle_ccy='USDT' AND ct_type='linear'
                ORDER BY inst_id
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def channel_ack_coverage(
        self,
        channel: str,
        expected_inst_ids: Sequence[str],
    ) -> tuple[int, int]:
        expected = sorted({str(item) for item in expected_inst_ids if str(item)})
        if not expected:
            return 0, 0
        observed = self.current_acked_symbols(channel, expected)
        return len(observed), len(expected)

    def current_acked_symbols(
        self,
        channel: str,
        inst_ids: Sequence[str],
    ) -> set[str]:
        """Return ACKed symbols whose owning connection is currently ready."""
        expected = sorted({str(item) for item in inst_ids if str(item)})
        observed: set[str] = set()
        if not expected:
            return observed
        with self._readonly() as connection:
            for chunk in _chunks(expected, 400):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT subscription.inst_id
                    FROM subscriptions AS subscription
                    JOIN connection_health AS health
                      ON health.group_id=subscription.group_id
                     AND health.status='connected'
                    WHERE subscription.channel=?
                      AND subscription.status='acked'
                      AND subscription.inst_id IN ({placeholders})
                    """,
                    (channel, *chunk),
                ).fetchall()
                observed.update(str(row[0]) for row in rows)
        return observed

    def connection_epochs(self, channel: str | None = None) -> tuple[str, ...]:
        with self._readonly() as connection:
            rows = connection.execute(
                "SELECT channels_json,conn_epoch FROM connection_health "
                "WHERE status='connected'"
            ).fetchall()
        epochs = set()
        for row in rows:
            try:
                channels = json.loads(row["channels_json"])
            except (TypeError, json.JSONDecodeError):
                channels = []
            if channel is None or channel in channels:
                epochs.add(str(row["conn_epoch"]))
        return tuple(sorted(epochs))

    def complete_stream_symbols(
        self,
        channel: str,
        inst_ids: Sequence[str],
    ) -> set[str]:
        requested = sorted({str(item) for item in inst_ids if str(item)})
        observed: set[str] = set()
        with self._readonly() as connection:
            for chunk in _chunks(requested, 400):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT stream.inst_id
                    FROM stream_completeness AS stream
                    JOIN subscriptions AS subscription
                      ON subscription.channel=stream.channel
                     AND subscription.inst_id=stream.inst_id
                     AND subscription.status='acked'
                    JOIN connection_health AS health
                      ON health.group_id=subscription.group_id
                     AND health.status='connected'
                     AND health.conn_epoch=stream.conn_epoch
                    WHERE stream.channel=? AND stream.status='complete'
                      AND stream.inst_id IN ({placeholders})
                    """,
                    (channel, *chunk),
                ).fetchall()
                observed.update(str(row[0]) for row in rows)
        return observed

    def read_latest(
        self,
        channel: str,
        inst_ids: Sequence[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        requested = sorted({str(item) for item in (inst_ids or []) if str(item)})
        out: dict[str, dict[str, Any]] = {}
        with self._readonly() as connection:
            if not requested:
                rows = connection.execute(
                    "SELECT inst_id,payload_json FROM latest_market WHERE channel=?",
                    (channel,),
                ).fetchall()
                return {str(row[0]): json.loads(row[1]) for row in rows}
            for chunk in _chunks(requested, 400):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT inst_id,payload_json FROM latest_market "
                    f"WHERE channel=? AND inst_id IN ({placeholders})",
                    (channel, *chunk),
                ).fetchall()
                out.update({str(row[0]): json.loads(row[1]) for row in rows})
        return out

    def read_latest_current(
        self,
        channel: str,
        inst_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """只返回已确认订阅且属于当前连接 epoch 的最新状态。"""
        requested = sorted({str(item) for item in inst_ids if str(item)})
        out: dict[str, dict[str, Any]] = {}
        with self._readonly() as connection:
            for chunk in _chunks(requested, 350):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT market.inst_id,market.payload_json
                    FROM latest_market AS market
                    JOIN subscriptions AS subscription
                      ON subscription.channel=market.channel
                     AND subscription.inst_id=market.inst_id
                     AND subscription.status='acked'
                    JOIN connection_health AS health
                      ON health.group_id=subscription.group_id
                     AND health.status='connected'
                     AND health.conn_epoch=market.conn_epoch
                    WHERE market.channel=?
                      AND market.inst_id IN ({placeholders})
                    """,
                    (channel, *chunk),
                ).fetchall()
                out.update({str(row[0]): json.loads(row[1]) for row in rows})
        return out

    def read_candles(
        self,
        symbols: Sequence[str],
        bar: str,
        limit: int,
    ) -> dict[str, list[list[Any]]]:
        requested = sorted({str(item) for item in symbols if str(item)})
        grouped: dict[str, list[list[Any]]] = {symbol: [] for symbol in requested}
        with self._readonly() as connection:
            for chunk in _chunks(requested, 350):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT inst_id,ts_ms,open,high,low,close,volume,volume_ccy,
                           volume_quote,confirm
                    FROM candles
                    WHERE timeframe=? AND inst_id IN ({placeholders})
                    ORDER BY inst_id,ts_ms DESC
                    """,
                    (bar, *chunk),
                ).fetchall()
                for row in rows:
                    bucket = grouped[str(row["inst_id"])]
                    if len(bucket) >= limit:
                        continue
                    bucket.append(
                        [
                            str(row["ts_ms"]),
                            row["open"],
                            row["high"],
                            row["low"],
                            row["close"],
                            row["volume"],
                            row["volume_ccy"],
                            row["volume_quote"],
                            str(row["confirm"]),
                        ]
                    )
        return grouped

    def raw_ws_candle_symbols(
        self,
        symbols: Sequence[str],
        bar: str,
        ts_ms: int,
    ) -> set[str]:
        requested = sorted({str(item) for item in symbols if str(item)})
        observed: set[str] = set()
        with self._readonly() as connection:
            for chunk in _chunks(requested, 350):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT inst_id FROM candles
                    WHERE timeframe=? AND ts_ms=? AND source='ws'
                      AND inst_id IN ({placeholders})
                    """,
                    (bar, int(ts_ms), *chunk),
                ).fetchall()
                observed.update(str(row[0]) for row in rows)
        return observed

    def local_history_candle_symbols(
        self,
        symbols: Sequence[str],
        bar: str,
        ts_ms: int,
    ) -> set[str]:
        """Return exact closed rows seeded from the authoritative market DB.

        These rows are REST-derived local history, never raw WS evidence.  The
        market-source adapter may reuse them for immutable long-period history
        without relabelling them as WS or repeating the same remote history
        request every natural hour.
        """
        requested = sorted({str(item) for item in symbols if str(item)})
        observed: set[str] = set()
        with self._readonly() as connection:
            for chunk in _chunks(requested, 350):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT inst_id FROM candles
                    WHERE timeframe=? AND ts_ms=? AND source='market_db_seed'
                      AND inst_id IN ({placeholders})
                    """,
                    (bar, int(ts_ms), *chunk),
                ).fetchall()
                observed.update(str(row[0]) for row in rows)
        return observed

    def instrument_list_times(
        self, symbols: Sequence[str]
    ) -> dict[str, int | None]:
        requested = sorted({str(item) for item in symbols if str(item)})
        observed: dict[str, int | None] = {symbol: None for symbol in requested}
        with self._readonly() as connection:
            for chunk in _chunks(requested, 350):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT inst_id,payload_json FROM instruments "
                    f"WHERE inst_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    try:
                        payload = json.loads(str(row["payload_json"] or "{}"))
                    except json.JSONDecodeError:
                        payload = {}
                    observed[str(row["inst_id"])] = _safe_int(payload.get("listTime"))
        return observed

    def read_books(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        requested = sorted({str(item) for item in symbols if str(item)})
        out: dict[str, dict[str, Any]] = {}
        with self._readonly() as connection:
            for chunk in _chunks(requested, 400):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT * FROM books WHERE valid=1 AND inst_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    out[str(row["inst_id"])] = {
                        "ts": str(row["exchange_ts"] or ""),
                        "seqId": row["seq_id"],
                        "checksum": row["checksum"],
                        "bids": json.loads(row["bids_json"]),
                        "asks": json.loads(row["asks_json"]),
                    }
        return out

    def read_books_current(
        self, symbols: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Read only valid books bound to the current connected epoch."""
        requested = sorted({str(item) for item in symbols if str(item)})
        out: dict[str, dict[str, Any]] = {}
        with self._readonly() as connection:
            for chunk in _chunks(requested, 350):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT book.*
                    FROM books AS book
                    JOIN subscriptions AS subscription
                      ON subscription.channel='books'
                     AND subscription.inst_id=book.inst_id
                     AND subscription.status='acked'
                    JOIN connection_health AS health
                      ON health.group_id=subscription.group_id
                     AND health.status='connected'
                     AND health.conn_epoch=book.conn_epoch
                    WHERE book.valid=1
                      AND book.inst_id IN ({placeholders})
                    """,
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    out[str(row["inst_id"])] = {
                        "ts": str(row["exchange_ts"] or ""),
                        "seqId": row["seq_id"],
                        "checksum": row["checksum"],
                        "bids": json.loads(row["bids_json"]),
                        "asks": json.loads(row["asks_json"]),
                    }
        return out

    def read_trades(
        self,
        symbols: Sequence[str],
        limit: int = TRADE_RETENTION,
    ) -> dict[str, list[dict[str, Any]]]:
        requested = sorted({str(item) for item in symbols if str(item)})
        out: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in requested}
        with self._readonly() as connection:
            for chunk in _chunks(requested, 350):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT inst_id,payload_json FROM trades
                    WHERE inst_id IN ({placeholders})
                    ORDER BY inst_id,exchange_ts DESC,trade_id DESC
                    """,
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    bucket = out[str(row["inst_id"])]
                    if len(bucket) < limit:
                        bucket.append(json.loads(row["payload_json"]))
        return out

    def health_snapshot(self) -> dict[str, Any]:
        with self._readonly() as connection:
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            connections = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM connection_health ORDER BY group_id"
                ).fetchall()
            ]
            latest_sample = connection.execute(
                "SELECT * FROM service_samples ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            counts = {
                "instruments": connection.execute("SELECT count(*) FROM instruments").fetchone()[0],
                "latest_market": connection.execute("SELECT count(*) FROM latest_market").fetchone()[0],
                "candles": connection.execute("SELECT count(*) FROM candles").fetchone()[0],
                "books": connection.execute("SELECT count(*) FROM books WHERE valid=1").fetchone()[0],
                "trades": connection.execute("SELECT count(*) FROM trades").fetchone()[0],
                "complete_streams": connection.execute(
                    "SELECT count(*) FROM stream_completeness WHERE status='complete'"
                ).fetchone()[0],
            }
        return {
            "health": self.health().__dict__,
            "quick_check": quick,
            "counts": counts,
            "connections": connections,
            "latest_sample": dict(latest_sample) if latest_sample else None,
        }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_text(row: Sequence[Any], index: int) -> str | None:
    if index >= len(row) or row[index] is None:
        return None
    return str(row[index])


def _none_or_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])
