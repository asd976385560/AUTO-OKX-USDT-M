# -*- coding: utf-8 -*-
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(_public_project_path())
SCRIPTS = ROOT / "scripts"
# audit_ws_market_rest_sample 于 2026-08-26 归档进 archive/（主人 2026-08-25 裁决
# 彻底取消 ws_first 后的独立 REST 核验层，归档时运行链已零调用方）。本用例刻意
# 跟着走而不是删除：归档≠销毁，钉住轮转抽样与状态判定行为，日后若授权单次
# 重跑即为依据（archive/README 例外条款）。
ARCHIVE = SCRIPTS / "archive"
for _p in (SCRIPTS, ARCHIVE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _okx_market_source as market_source
import audit_ws_market_health as ws_health_audit
import manage_ws_market_phase as phase_manager
from _okx_ws_cache import (
    CANDLE_RETENTION,
    CacheReader,
    CacheStore,
    apply_orderbook_update,
    candle_bar_end_ms,
    expected_closed_start_ms,
    orderbook_checksum,
    shard_subscription_args,
    subscription_payload_size,
)
from okx_public_ws_service import (
    ConnectionGroup,
    ConnectionRunner,
    StreamState,
    build_static_groups,
    flush_pending,
    reconnect_delay_base,
)


def instrument(symbol: str) -> dict:
    return {
        "instId": symbol,
        "instType": "SWAP",
        "settleCcy": "USDT",
        "quoteCcy": "USDT",
        "ctType": "linear",
        "state": "live",
        "ctVal": "1",
        "lotSz": "1",
    }


class SubscriptionTests(unittest.TestCase):
    def test_current_and_growth_universe_stay_under_48_kib(self) -> None:
        for count in (438, 439):
            symbols = [f"S{index:04d}-USDT-SWAP" for index in range(count)]
            groups = build_static_groups(symbols)
            self.assertTrue(groups)
            self.assertTrue(
                all(subscription_payload_size(group.args) <= 48 * 1024 for group in groups)
            )
            observed = [
                (arg.get("channel"), arg.get("instId"), arg.get("instType"))
                for group in groups
                for arg in group.args
            ]
            self.assertEqual(len(observed), len(set(observed)))

    def test_single_oversized_arg_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            shard_subscription_args(
                [{"channel": "tickers", "instId": "X" * 500}], max_bytes=100
            )

    def test_reconnect_backoff_is_exponential_and_bounded(self) -> None:
        self.assertEqual(
            [reconnect_delay_base(index) for index in range(8)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0],
        )

    def test_runner_advances_backoff_after_failed_open_attempts(self) -> None:
        async def exercise() -> list[float]:
            stop_event = asyncio.Event()
            logger = mock.Mock()
            runner = ConnectionRunner(
                mock.Mock(), mock.Mock(), mock.Mock(), logger, stop_event
            )
            group = ConnectionGroup(
                group_id="test-public-state",
                endpoint="wss://example.invalid",
                args=({"channel": "tickers", "instId": "BTC-USDT-SWAP"},),
            )
            attempts = 0
            observed_delays: list[float] = []

            async def fake_open(*_args):
                nonlocal attempts
                attempts += 1
                if attempts <= 3:
                    raise ConnectionResetError("test")
                stop_event.set()
                return object(), "epoch-ready"

            async def fake_wait(_stop_event, delay: float) -> None:
                observed_delays.append(delay)

            runner._open_subscribed = fake_open
            with mock.patch(
                "okx_public_ws_service._wait_or_stop", side_effect=fake_wait
            ), mock.patch(
                "okx_public_ws_service.random.uniform", return_value=0.0
            ):
                await runner.run(group)
            error_events = [
                call for call in logger.emit.call_args_list
                if call.args and call.args[0] == "connection_error"
            ]
            self.assertEqual(
                [call.kwargs["consecutive_failure_attempt"] for call in error_events],
                [1, 2, 3],
            )
            self.assertEqual(
                [call.kwargs["backoff_base_seconds"] for call in error_events],
                [1.0, 2.0, 4.0],
            )
            self.assertEqual(
                [call.kwargs["backoff_seconds"] for call in error_events],
                [1.0, 2.0, 4.0],
            )
            return observed_delays

        self.assertEqual(asyncio.run(exercise()), [1.0, 2.0, 4.0])


class CandleBoundaryTests(unittest.TestCase):
    def test_okx_utc8_boundaries(self) -> None:
        at = datetime(2026, 8, 23, 2, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(
            expected_closed_start_ms("15m", at),
            int(datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc).timestamp() * 1000),
        )
        self.assertEqual(
            expected_closed_start_ms("1D", at),
            int(datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc).timestamp() * 1000),
        )
        self.assertEqual(
            expected_closed_start_ms("1W", at),
            int(datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc).timestamp() * 1000),
        )
        self.assertEqual(
            expected_closed_start_ms("1M", at),
            int(datetime(2026, 6, 30, 16, 0, tzinfo=timezone.utc).timestamp() * 1000),
        )


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temp.name) / "cache.db"
        self.store = CacheStore(self.cache_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_only_confirmed_candle_is_persisted(self) -> None:
        ts_ms = expected_closed_start_ms("15m")
        rows = [
            [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "0"],
            [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"],
        ]
        wrote = self.store.upsert_candles(
            "BTC-USDT-SWAP", "15m", rows, "epoch-1"
        )
        self.assertEqual(wrote, 1)
        cached = CacheReader(self.cache_path).read_candles(
            ["BTC-USDT-SWAP"], "15m", 60
        )
        self.assertEqual(cached["BTC-USDT-SWAP"], [rows[1]])

    def test_candle_retention_is_120(self) -> None:
        base = expected_closed_start_ms("15m") - 130 * 15 * 60 * 1000
        rows = [
            [
                str(base + index * 15 * 60 * 1000),
                "1",
                "2",
                "0.5",
                "1.5",
                "10",
                "10",
                "15",
                "1",
            ]
            for index in range(130)
        ]
        self.store.upsert_candles("BTC-USDT-SWAP", "15m", rows, "epoch-1")
        cached = CacheReader(self.cache_path).read_candles(
            ["BTC-USDT-SWAP"], "15m", 500
        )
        self.assertEqual(len(cached["BTC-USDT-SWAP"]), 120)

    def test_book_snapshot_update_and_sequence_failure(self) -> None:
        state = StreamState(self.store)
        bids = [["100", "1", "0", "1"]]
        asks = [["101", "2", "0", "1"]]
        snapshot = {
            "ts": "1",
            "seqId": 10,
            "prevSeqId": -1,
            "bids": bids,
            "asks": asks,
            "checksum": orderbook_checksum(bids, asks),
        }
        state.ingest(
            {"channel": "books", "instId": "BTC-USDT-SWAP"},
            "snapshot",
            [snapshot],
            "epoch-1",
        )
        new_bids, new_asks = apply_orderbook_update(
            bids, asks, [["100", "3", "0", "1"]], []
        )
        update = {
            "ts": "2",
            "seqId": 11,
            "prevSeqId": 10,
            "bids": [["100", "3", "0", "1"]],
            "asks": [],
            "checksum": orderbook_checksum(new_bids, new_asks),
        }
        state.ingest(
            {"channel": "books", "instId": "BTC-USDT-SWAP"},
            "update",
            [update],
            "epoch-1",
        )
        flush_pending(self.store, state.take_pending())
        book = CacheReader(self.cache_path).read_books(["BTC-USDT-SWAP"])
        self.assertEqual(book["BTC-USDT-SWAP"]["bids"][0][1], "3")

        broken = dict(update, seqId=12, prevSeqId=99)
        with self.assertRaisesRegex(Exception, "book_sequence_break"):
            state.ingest(
                {"channel": "books", "instId": "BTC-USDT-SWAP"},
                "update",
                [broken],
                "epoch-1",
            )
        self.assertNotIn(
            "BTC-USDT-SWAP",
            CacheReader(self.cache_path).read_books(["BTC-USDT-SWAP"]),
        )

    def test_current_okx_zero_checksum_relies_on_sequence(self) -> None:
        state = StreamState(self.store)
        snapshot = {
            "ts": "1",
            "seqId": 20,
            "prevSeqId": -1,
            "bids": [["100", "1", "0", "1"]],
            "asks": [["101", "2", "0", "1"]],
            "checksum": 0,
        }
        state.ingest(
            {"channel": "books", "instId": "BTC-USDT-SWAP"},
            "snapshot",
            [snapshot],
            "epoch-current",
        )
        update = {
            "ts": "2",
            "seqId": 21,
            "prevSeqId": 20,
            "bids": [["100", "3", "0", "1"]],
            "asks": [],
            "checksum": 0,
        }
        state.ingest(
            {"channel": "books", "instId": "BTC-USDT-SWAP"},
            "update",
            [update],
            "epoch-current",
        )
        flush_pending(self.store, state.take_pending())
        book = CacheReader(self.cache_path).read_books(["BTC-USDT-SWAP"])
        self.assertEqual(book["BTC-USDT-SWAP"]["seqId"], 21)

    def test_latest_state_requires_current_connection_epoch(self) -> None:
        symbol = "BTC-USDT-SWAP"
        args = [{"channel": "tickers", "instId": symbol}]
        self.store.register_connection(
            "state-test", "wss://example", ["tickers"], "epoch-new", args, 0
        )
        self.store.acknowledge_subscriptions("state-test", args)
        self.store.connection_ready("state-test", 10)
        self.store.upsert_latest(
            "tickers", [{"instId": symbol, "last": "1"}], "epoch-old"
        )
        reader = CacheReader(self.cache_path)
        self.assertEqual({}, reader.read_latest_current("tickers", [symbol]))
        self.store.upsert_latest(
            "tickers", [{"instId": symbol, "last": "2"}], "epoch-new"
        )
        self.assertEqual(
            "2", reader.read_latest_current("tickers", [symbol])[symbol]["last"]
        )


class MarketSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache_path = self.root / "cache.db"
        self.log_dir = self.root / "logs"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def config(self, mode: str, **extra) -> Path:
        path = self.root / f"config-{mode}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": mode,
                    "cache_db": str(self.cache_path),
                    "heartbeat_max_age_seconds": 60,
                    **extra,
                }
            ),
            encoding="utf-8",
        )
        return path

    def environment(self, mode: str, **extra):
        return mock.patch.dict(
            os.environ,
            {
                "OKX_MARKET_SOURCE_CONFIG": str(self.config(mode, **extra)),
                "OKX_WS_LOG_DIR": str(self.log_dir),
            },
        )

    def ready_cache(self, symbols: list[str], bar: str = "15m") -> CacheStore:
        store = CacheStore(self.cache_path)
        store.upsert_instruments([instrument(symbol) for symbol in symbols], "test")
        args = [{"channel": f"candle{bar}", "instId": symbol} for symbol in symbols]
        store.register_connection(
            "candle-test", "wss://example", [f"candle{bar}"], "epoch-1", args, 0
        )
        store.acknowledge_subscriptions("candle-test", args)
        store.connection_ready("candle-test", 10)
        store.heartbeat_sample(
            pid=1,
            rss_bytes=1,
            cpu_cores=0.1,
            pending_latest=0,
            pending_candles=0,
            pending_books=0,
            pending_trades=0,
        )
        return store

    def test_shadow_delegates_to_rest_only(self) -> None:
        rest_value = {
            "BTC-USDT-SWAP": [["1", "1", "1", "1", "1", "1", "1", "1", "1"]]
        }
        with self.environment("shadow"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            return_value=rest_value,
        ) as rest:
            value = market_source.fetch_candles_batch_sync(
                ["BTC-USDT-SWAP"], "15m", 60, 10
            )
        self.assertEqual(value, rest_value)
        rest.assert_called_once()

    def test_ws_first_uses_exact_confirmed_candle_without_rest(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = self.ready_cache([symbol])
        ts_ms = expected_closed_start_ms("15m")
        row = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        store.upsert_candles(symbol, "15m", [row], "epoch-1")
        store.close()
        with self.environment("ws_first"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            side_effect=AssertionError("REST must not be called"),
        ):
            batch = market_source.get_candles_batch([symbol], "15m", 60, 10)
        self.assertEqual(batch.source, "ws")
        self.assertEqual(batch.data[symbol][0], row)
        self.assertEqual(batch.missing, ())

    def test_ws_first_recovers_only_missing_symbol(self) -> None:
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        store = self.ready_cache(symbols)
        ts_ms = expected_closed_start_ms("15m")
        btc = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        eth = [str(ts_ms), "2", "3", "1", "2.5", "11", "11", "16", "1"]
        store.upsert_candles(symbols[0], "15m", [btc], "epoch-1")
        store.close()

        def recover(requested, *args, **kwargs):
            self.assertEqual(list(requested), [symbols[1]])
            outcomes = kwargs.get("outcomes")
            if outcomes is not None:
                outcomes[symbols[1]] = {
                    "ok": True,
                    "error_type": None,
                    "root_error_type": None,
                    "error_type_chain": [],
                }
            return {symbols[1]: [eth]}

        with self.environment("ws_first"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            side_effect=recover,
        ):
            batch = market_source.get_candles_batch(symbols, "15m", 60, 10)
        self.assertEqual(batch.source, "mixed")
        self.assertEqual(batch.ws_count, 1)
        self.assertEqual(batch.rest_count, 1)
        self.assertEqual(batch.data[symbols[0]][0], btc)
        self.assertEqual(batch.data[symbols[1]][0], eth)

    def test_ws_first_candle_partial_ack_recovers_only_missing_symbol(self) -> None:
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        store = self.ready_cache([symbols[0]])
        store.upsert_instruments([instrument(symbols[1])], "test")
        ts_ms = expected_closed_start_ms("15m")
        btc = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        eth = [str(ts_ms), "2", "3", "1", "2.5", "11", "11", "16", "1"]
        store.upsert_candles(symbols[0], "15m", [btc], "epoch-1")
        store.close()

        def recover(requested, *args, **kwargs):
            self.assertEqual([symbols[1]], list(requested))
            return {symbols[1]: [eth]}

        with self.environment("ws_first"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            side_effect=recover,
        ) as rest:
            batch = market_source.get_candles_batch(symbols, "15m", 60, 10)
        rest.assert_called_once()
        self.assertEqual("mixed", batch.source)
        self.assertEqual(1, batch.ws_count)
        self.assertEqual(1, batch.rest_count)
        self.assertEqual((), batch.missing)

    def test_ws_first_waits_for_natural_confirm_before_rest_recovery(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = self.ready_cache([symbol])
        ts_ms = expected_closed_start_ms("15m")
        row = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        store.close()

        def insert_late() -> None:
            time.sleep(0.05)
            late_store = CacheStore(self.cache_path)
            late_store.upsert_candles(symbol, "15m", [row], "epoch-1")
            late_store.close()

        worker = threading.Thread(target=insert_late)
        worker.start()
        with self.environment(
            "ws_first", ws_candle_wait_seconds=0.5
        ), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            side_effect=AssertionError("REST recovery should not run"),
        ):
            batch = market_source.get_candles_batch([symbol], "15m", 60, 10)
        worker.join(timeout=1)
        self.assertEqual("ws", batch.source)
        self.assertEqual(1, batch.ws_count)
        self.assertEqual(0, batch.rest_count)

    def test_dual_read_returns_rest_and_records_exact_match(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = self.ready_cache([symbol])
        ts_ms = expected_closed_start_ms("15m")
        row = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        store.upsert_candles(symbol, "15m", [row], "epoch-1")
        store.close()
        with self.environment("dual_read"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            return_value={symbol: [row]},
        ):
            batch = market_source.get_candles_batch([symbol], "15m", 60, 10)
        self.assertEqual("rest", batch.source)
        self.assertEqual(1, batch.ws_count)
        self.assertEqual(1, batch.rest_count)
        self.assertEqual(0, batch.comparison["unresolved_mismatches"])
        self.assertTrue(batch.connection_epochs)

    def test_dual_read_treats_decimal_formatting_as_exactly_equal(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = self.ready_cache([symbol])
        ts_ms = expected_closed_start_ms("15m")
        ws_row = [
            str(ts_ms), "1.5000", "2.00", "0.500", "1.50",
            "10", "10", "161777782342.59908000", "1",
        ]
        rest_row = [
            str(ts_ms), "1.5", "2", "0.5", "1.500",
            "10", "10", "161777782342.59908", "1",
        ]
        store.upsert_candles(symbol, "15m", [ws_row], "epoch-1")
        store.close()
        with self.environment("dual_read"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            return_value={symbol: [rest_row]},
        ):
            batch = market_source.get_candles_batch([symbol], "15m", 60, 10)
        self.assertEqual(0, batch.comparison["unresolved_mismatches"])
        self.assertEqual(2, batch.comparison["contract_version"])
        self.assertEqual("exact_decimal", batch.comparison["numeric_semantics"])

    def test_dual_read_refreshes_ws_snapshot_after_rest_boundary_race(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = self.ready_cache([symbol])
        ts_ms = expected_closed_start_ms("15m")
        row = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        store.close()

        def rest_fetch(*args, **kwargs):
            late_store = CacheStore(self.cache_path)
            late_store.upsert_candles(symbol, "15m", [row], "epoch-1")
            late_store.close()
            return {symbol: [row]}

        with self.environment("dual_read"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            side_effect=rest_fetch,
        ):
            batch = market_source.get_candles_batch([symbol], "15m", 60, 10)
        self.assertEqual(1, batch.ws_count)
        self.assertEqual(1, batch.comparison["compared"])
        self.assertEqual(0, batch.comparison["ws_missing"])
        self.assertEqual(0, batch.comparison["unresolved_mismatches"])

    def test_dual_read_receipt_counts_missing_exact_rest_close(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = self.ready_cache([symbol])
        ts_ms = expected_closed_start_ms("15m")
        ws_row = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        old_row = [
            str(ts_ms - 900_000), "1", "2", "0.5", "1.5", "10", "10", "15", "1"
        ]
        store.upsert_candles(symbol, "15m", [ws_row], "epoch-1")
        store.close()
        with self.environment("dual_read"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            return_value={symbol: [old_row]},
        ):
            batch = market_source.get_candles_batch([symbol], "15m", 60, 10)
        self.assertEqual(1, batch.ws_count)
        self.assertEqual(0, batch.rest_count)
        self.assertEqual((symbol,), batch.missing)
        self.assertEqual(1, batch.comparison["rest_missing"])

    def test_seeded_history_is_not_counted_as_raw_ws_close(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = self.ready_cache([symbol], bar="1M")
        ts_ms = expected_closed_start_ms("1M")
        row = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        store.upsert_candles(
            symbol, "1M", [row], "local-seed", source="market_db_seed"
        )
        store.close()
        with self.environment("dual_read"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            return_value={symbol: [row]},
        ):
            batch = market_source.get_candles_batch([symbol], "1M", 60, 10)
        self.assertEqual(0, batch.ws_count)
        self.assertEqual(1, batch.rest_count)
        self.assertEqual(1, batch.comparison["ws_missing"])
        self.assertEqual(0, batch.comparison["unresolved_mismatches"])
        self.assertIn(
            "raw_ws_closed_candle_incomplete:0/1", batch.fallback_reasons
        )

    def test_ws_first_reuses_seeded_monthly_history_without_rest(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = self.ready_cache([symbol], bar="1M")
        ts_ms = expected_closed_start_ms("1M")
        row = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        store.upsert_candles(
            symbol, "1M", [row], "local-seed", source="market_db_seed"
        )
        store.close()
        with self.environment("ws_first"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            side_effect=AssertionError("closed monthly history must stay local"),
        ):
            batch = market_source.get_candles_batch([symbol], "1M", 60, 10)
        self.assertEqual("local_history", batch.source)
        self.assertEqual(0, batch.ws_count)
        self.assertEqual(0, batch.rest_count)
        self.assertEqual((), batch.missing)
        self.assertEqual(row, batch.data[symbol][0])
        self.assertIn("local_history_reuse:1", batch.fallback_reasons)

    def test_pre_listing_candle_is_not_an_expected_missing_row(self) -> None:
        symbol = "NEW-USDT-SWAP"
        store = self.ready_cache([symbol], bar="1M")
        ts_ms = expected_closed_start_ms("1M")
        payload = instrument(symbol)
        payload["listTime"] = str(candle_bar_end_ms("1M", ts_ms) + 1)
        store.upsert_instruments([payload], "test")
        store.close()
        history_row = [
            str(ts_ms + 1), "1", "2", "0.5", "1.5", "10", "10", "15", "0"
        ]
        with self.environment("ws_first"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            return_value={symbol: [history_row]},
        ) as rest:
            batch = market_source.get_candles_batch([symbol], "1M", 60, 10)
        self.assertEqual(0, batch.ws_count)
        self.assertEqual(1, batch.rest_count)
        self.assertEqual((), batch.missing)
        self.assertEqual([history_row], batch.data[symbol])
        self.assertIn(
            "historical_rest_recovery_pre_listing:1", batch.fallback_reasons
        )
        rest.assert_called_once()

    def test_dual_read_exposes_candle_mismatch_without_switching_source(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = self.ready_cache([symbol])
        ts_ms = expected_closed_start_ms("15m")
        ws_row = [str(ts_ms), "1", "2", "0.5", "1.5", "10", "10", "15", "1"]
        rest_row = [str(ts_ms), "1", "2", "0.5", "9.9", "10", "10", "15", "1"]
        store.upsert_candles(symbol, "15m", [ws_row], "epoch-1")
        store.close()
        with self.environment("dual_read"), mock.patch.object(
            market_source._rest,
            "fetch_candles_batch_sync",
            return_value={symbol: [rest_row]},
        ):
            batch = market_source.get_candles_batch([symbol], "15m", 60, 10)
        self.assertEqual("rest", batch.source)
        self.assertEqual(rest_row, batch.data[symbol][0])
        self.assertEqual(1, batch.comparison["unresolved_mismatches"])

    def test_ws_first_trades_falls_back_when_reconnect_gap_is_unrepaired(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = CacheStore(self.cache_path)
        store.upsert_instruments([instrument(symbol)], "test")
        args = [{"channel": "trades-all", "instId": symbol}]
        store.register_connection(
            "trades-test", "wss://example", ["trades-all"], "epoch-1", args, 0
        )
        store.acknowledge_subscriptions("trades-test", args)
        store.connection_ready("trades-test", 10)
        store.insert_trades(
            symbol,
            [{"instId": symbol, "tradeId": "1", "ts": "1", "px": "1", "sz": "1"}],
            "epoch-1",
        )
        store.mark_stream_incomplete(
            "trades-all", [symbol], "epoch-1", "test_gap"
        )
        store.heartbeat_sample(
            pid=1,
            rss_bytes=1,
            cpu_cores=0.1,
            pending_latest=0,
            pending_candles=0,
            pending_books=0,
            pending_trades=0,
        )
        store.close()
        rest_rows = [{"instId": symbol, "tradeId": "2", "ts": "2", "px": "2", "sz": "1"}]
        with self.environment("ws_first"), mock.patch.object(
            market_source._rest,
            "fetch_recent_trades_batch_sync",
            return_value={symbol: rest_rows},
        ) as rest:
            rows = market_source.fetch_recent_trades_batch_sync([symbol], 500, 10)
        rest.assert_called_once()
        self.assertEqual(rest_rows, rows[symbol])

    def test_ws_first_orderbook_uses_cache_without_rest(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = CacheStore(self.cache_path)
        store.upsert_instruments([instrument(symbol)], "test")
        args = [{"channel": "books", "instId": symbol}]
        store.register_connection(
            "books-test", "wss://example", ["books"], "epoch-1", args, 0
        )
        store.acknowledge_subscriptions("books-test", args)
        store.connection_ready("books-test", 10)
        expected = {
            "ts": "1",
            "seqId": "2",
            "checksum": "0",
            "bids": [["1", "2"]],
            "asks": [["2", "1"]],
        }
        store.upsert_book(symbol, expected, "epoch-1")
        store.heartbeat_sample(
            pid=1,
            rss_bytes=1,
            cpu_cores=0.1,
            pending_latest=0,
            pending_candles=0,
            pending_books=0,
            pending_trades=0,
        )
        store.close()
        with self.environment("ws_first"), mock.patch.object(
            market_source._rest,
            "fetch_orderbooks_batch_sync",
            side_effect=AssertionError("REST must not be called"),
        ):
            batch = market_source.get_orderbooks_batch([symbol], 50, 10)
        self.assertEqual("ws", batch.source)
        self.assertEqual(1, batch.ws_count)
        self.assertEqual(0, batch.rest_count)
        self.assertEqual(expected["bids"], batch.data[symbol]["bids"])

    def test_ws_first_orderbook_recovers_only_unacked_symbol(self) -> None:
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        store = CacheStore(self.cache_path)
        store.upsert_instruments([instrument(symbol) for symbol in symbols], "test")
        args = [{"channel": "books", "instId": symbols[0]}]
        store.register_connection(
            "books-test", "wss://example", ["books"], "epoch-1", args, 0
        )
        store.acknowledge_subscriptions("books-test", args)
        store.connection_ready("books-test", 10)
        first = {
            "ts": "1",
            "seqId": "2",
            "checksum": "0",
            "bids": [["1", "2"]],
            "asks": [["2", "1"]],
        }
        second = {
            "ts": "2",
            "seqId": "3",
            "checksum": "0",
            "bids": [["3", "2"]],
            "asks": [["4", "1"]],
        }
        store.upsert_book(symbols[0], first, "epoch-1")
        store.heartbeat_sample(
            pid=1,
            rss_bytes=1,
            cpu_cores=0.1,
            pending_latest=0,
            pending_candles=0,
            pending_books=0,
            pending_trades=0,
        )
        store.close()

        def recover(requested, *args, **kwargs):
            self.assertEqual([symbols[1]], list(requested))
            return {symbols[1]: second}

        with self.environment("ws_first"), mock.patch.object(
            market_source._rest,
            "fetch_orderbooks_batch_sync",
            side_effect=recover,
        ) as rest:
            batch = market_source.get_orderbooks_batch(symbols, 50, 10)
        rest.assert_called_once()
        self.assertEqual("mixed", batch.source)
        self.assertEqual(1, batch.ws_count)
        self.assertEqual(1, batch.rest_count)
        self.assertEqual((), batch.missing)
        self.assertEqual(first["bids"], batch.data[symbols[0]]["bids"])
        self.assertEqual(second, batch.data[symbols[1]])

    def test_ws_first_orderbook_rejects_stale_connection_epoch(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = CacheStore(self.cache_path)
        store.upsert_instruments([instrument(symbol)], "test")
        args = [{"channel": "books", "instId": symbol}]
        store.register_connection(
            "books-test", "wss://example", ["books"], "epoch-old", args, 0
        )
        store.acknowledge_subscriptions("books-test", args)
        store.connection_ready("books-test", 10)
        store.upsert_book(
            symbol,
            {"ts": "1", "seqId": "2", "bids": [["1", "1"]], "asks": [["2", "1"]]},
            "epoch-old",
        )
        store.register_connection(
            "books-test", "wss://example", ["books"], "epoch-new", args, 1
        )
        store.acknowledge_subscriptions("books-test", args)
        store.connection_ready("books-test", 10)
        store.heartbeat_sample(
            pid=1,
            rss_bytes=1,
            cpu_cores=0.1,
            pending_latest=0,
            pending_candles=0,
            pending_books=0,
            pending_trades=0,
        )
        store.close()
        reader = CacheReader(self.cache_path)
        self.assertEqual({}, reader.read_books_current([symbol]))

    def test_repaired_trade_stream_can_be_used(self) -> None:
        symbol = "BTC-USDT-SWAP"
        store = CacheStore(self.cache_path)
        store.upsert_instruments([instrument(symbol)], "test")
        args = [{"channel": "trades-all", "instId": symbol}]
        store.register_connection(
            "trades-test", "wss://example", ["trades-all"], "epoch-1", args, 0
        )
        store.acknowledge_subscriptions("trades-test", args)
        store.connection_ready("trades-test", 10)
        expected = {"instId": symbol, "tradeId": "1", "ts": "1", "px": "1", "sz": "1"}
        store.insert_trades(symbol, [expected], "epoch-1")
        store.mark_stream_incomplete(
            "trades-all", [symbol], "epoch-1", "bootstrap"
        )
        self.assertEqual(
            1, store.mark_stream_complete("trades-all", [symbol], "epoch-1")
        )
        store.heartbeat_sample(
            pid=1,
            rss_bytes=1,
            cpu_cores=0.1,
            pending_latest=0,
            pending_candles=0,
            pending_books=0,
            pending_trades=0,
        )
        store.close()
        with self.environment("ws_first"), mock.patch.object(
            market_source._rest,
            "fetch_recent_trades_batch_sync",
            side_effect=AssertionError("REST must not be called"),
        ):
            rows = market_source.fetch_recent_trades_batch_sync([symbol], 500, 10)
        self.assertEqual("1", rows[symbol][0]["tradeId"])

    def test_market_batch_receipt_never_embeds_data(self) -> None:
        batch = market_source.MarketBatch(
            data={"secret": "payload"},
            source="rest",
            ws_count=0,
            rest_count=1,
            missing=(),
            as_of="2026-01-01T00:00:00Z",
            universe_sha256="abc",
        )
        self.assertNotIn("data", batch.receipt())

    def test_pending_mode_activates_only_at_registered_boundary(self) -> None:
        config = {
            "mode": "dual_read",
            "pending_mode": "ws_first",
            "activation_boundary": "2026-08-24T11:00:00+08:00",
        }
        before = datetime.fromisoformat("2026-08-24T10:59:59+08:00")
        boundary = datetime.fromisoformat("2026-08-24T11:00:00+08:00")
        self.assertEqual(
            "dual_read", market_source.effective_source_mode(config, before)
        )
        self.assertEqual(
            "ws_first", market_source.effective_source_mode(config, boundary)
        )


class PhaseManagerTests(unittest.TestCase):
    def test_shadow_advance_requires_mature_passed_24h_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit_path = root / "shadow.json"
            audit_path.write_text(
                json.dumps(
                    {
                        "mode": "shadow",
                        "status": "PASSED",
                        "generated_at": "2026-08-24T02:30:00Z",
                        "window": {
                            "mature": True,
                            "required_hours": 24,
                            "elapsed_hours": 24.1,
                        },
                        "current_gates": {"all": True},
                        "candle_window_summary": {
                            "combined_close_latency_seconds": {"p99": 4.5}
                        },
                        "resources": {"recovery_seconds": {"p99": 3.2}},
                    }
                ),
                encoding="utf-8",
            )
            updated, summary = phase_manager.plan_transition(
                {"schema_version": 1, "mode": "shadow", "phase_history": []},
                target="dual_read",
                audit_path=audit_path,
                shadow_audit_path=None,
                threshold_registration_path=None,
                now=datetime.fromisoformat("2026-08-24T10:30:00+08:00"),
                reason="test",
            )
        self.assertEqual("dual_read", updated["mode"])
        self.assertIsNone(updated["pending_mode"])
        self.assertEqual("shadow", summary["current_mode"])

    def test_pending_shadow_audit_cannot_advance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending.json"
            path.write_text(
                json.dumps(
                    {
                        "mode": "shadow",
                        "status": "PENDING",
                        "window": {"mature": False, "required_hours": 24},
                        "current_gates": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(phase_manager.PhaseError, "audit_not_passed"):
                phase_manager.plan_transition(
                    {"mode": "shadow"},
                    target="dual_read",
                    audit_path=path,
                    shadow_audit_path=None,
                    threshold_registration_path=None,
                    now=datetime.now(timezone.utc),
                    reason="test",
                )

    def test_dual_read_window_restart_requires_explicit_flag(self) -> None:
        config = {"schema_version": 1, "mode": "dual_read", "phase_history": []}
        with self.assertRaisesRegex(
            phase_manager.PhaseError, "dual_read_restart_requires_explicit_flag"
        ):
            phase_manager.plan_transition(
                config,
                target="dual_read",
                audit_path=None,
                shadow_audit_path=None,
                threshold_registration_path=None,
                now=datetime.fromisoformat("2026-08-24T15:20:00+08:00"),
                reason="test",
            )
        updated, summary = phase_manager.plan_transition(
            config,
            target="dual_read",
            audit_path=None,
            shadow_audit_path=None,
            threshold_registration_path=None,
            now=datetime.fromisoformat("2026-08-24T15:20:00+08:00"),
            reason="test",
            restart_window=True,
        )
        self.assertEqual("dual_read", updated["mode"])
        self.assertEqual(
            "2026-08-24T15:20:00+08:00", updated["phase_started_at_cst"]
        )
        self.assertEqual(
            "dual_read_window_restart", summary["evidence"]["kind"]
        )

    def test_force_cutover_records_waived_maturity_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow_path = root / "shadow.json"
            dual_path = root / "dual.json"
            shadow_path.write_text(
                json.dumps(
                    {
                        "mode": "shadow",
                        "status": "PASSED",
                        "generated_at": "2026-08-24T06:16:13Z",
                        "window": {
                            "mature": True,
                            "required_hours": 24,
                            "elapsed_hours": 24.2,
                        },
                        "current_gates": {"all": True},
                        "candle_window_summary": {
                            "combined_close_latency_seconds": {"p99": 7.5}
                        },
                        "resources": {"recovery_seconds": {"p99": 24.5}},
                    }
                ),
                encoding="utf-8",
            )
            dual_path.write_text(
                json.dumps(
                    {
                        "mode": "dual_read",
                        "status": "PENDING",
                        "generated_at": "2026-08-24T12:49:26Z",
                        "window": {
                            "mature": False,
                            "required_hours": 48,
                            "elapsed_hours": 2.4,
                        },
                        "current_gates": {
                            "service_heartbeat": True,
                            "raw_ws_coverage_gte_99pct": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            updated, summary = phase_manager.plan_transition(
                {"schema_version": 1, "mode": "dual_read", "phase_history": []},
                target="ws_first",
                audit_path=dual_path,
                shadow_audit_path=shadow_path,
                threshold_registration_path=None,
                now=datetime.fromisoformat("2026-08-24T20:55:00+08:00"),
                reason="explicit_user_request",
                force_cutover=True,
            )
        self.assertEqual("ws_first", updated["mode"])
        self.assertIsNone(updated["pending_mode"])
        self.assertEqual("explicit_user_force_cutover", summary["evidence"]["kind"])
        self.assertIn(
            "dual_read_48h_maturity",
            summary["evidence"]["waived_requirements"],
        )


class AuditWindowTests(unittest.TestCase):
    def test_health_interpretation_is_phase_and_status_aware(self) -> None:
        shadow = ws_health_audit.phase_interpretation("shadow", "PENDING")
        dual = ws_health_audit.phase_interpretation("dual_read", "PENDING")
        ws_first = ws_health_audit.phase_interpretation("ws_first", "PENDING")
        rest_only = ws_health_audit.phase_interpretation("rest_only", "PASSED")
        failed = ws_health_audit.phase_interpretation("ws_first", "NOT_MET")

        self.assertIn("推进dual_read", shadow)
        self.assertNotIn("推进ws_first", shadow)
        self.assertIn("推进ws_first", dual)
        self.assertIn("当前已运行ws_first", ws_first)
        self.assertIn("回滚生产模式", rest_only)
        self.assertIn("硬门未通过", failed)
        for text in (shadow, dual, ws_first, rest_only, failed):
            self.assertIn("全部保留在分母", text)

    def test_health_exit_code_separates_not_met_from_process_failure(self) -> None:
        self.assertEqual(0, ws_health_audit.exit_code_for_status("PENDING"))
        self.assertEqual(0, ws_health_audit.exit_code_for_status("PASSED"))
        self.assertEqual(1, ws_health_audit.exit_code_for_status("NOT_MET"))
        self.assertEqual(2, ws_health_audit.exit_code_for_status("BROKEN"))

    def test_dual_read_uses_configured_phase_boundary(self) -> None:
        started, normalized, basis = ws_health_audit._resolve_window_start(
            {
                "mode": "dual_read",
                "phase_started_at_cst": "2026-08-24T14:16:35.697888+08:00",
            },
            "2026-08-23T06:02:58Z",
        )
        self.assertEqual("config_phase_started_at_cst", basis)
        self.assertEqual("2026-08-24T06:16:35.697888Z", normalized)
        self.assertEqual(
            "2026-08-24T06:16:36Z",
            ws_health_audit._sqlite_query_start(started),
        )

    def test_missing_phase_boundary_falls_back_to_service_start(self) -> None:
        started, normalized, basis = ws_health_audit._resolve_window_start(
            {"mode": "shadow"}, "2026-08-23T06:02:58Z"
        )
        self.assertEqual("cache_service_started_at", basis)
        self.assertEqual("2026-08-23T06:02:58Z", normalized)
        self.assertEqual(
            "2026-08-23T06:02:58Z",
            ws_health_audit._sqlite_query_start(started),
        )

    def test_retention_window_start_matches_cache_retention(self) -> None:
        """保留视界起点必须与裁剪常量精确对应：期望收盘数 == CANDLE_RETENTION。"""
        now = datetime.fromisoformat("2026-08-26T03:42:16+00:00")
        for bar in ("15m", "1H", "4H", "1D", "1W", "1M"):
            start = ws_health_audit._retention_window_start(bar, now)
            self.assertEqual(
                CANDLE_RETENTION,
                ws_health_audit._expected_close_count(bar, start, now),
                bar,
            )

    def test_effective_window_start_clamps_only_beyond_retention(self) -> None:
        """15m 窗口超 30 小时按保留视界钳制；1H 未超 120 小时保持相位锚点。"""
        now = datetime.fromisoformat("2026-08-26T03:42:16+00:00")
        phase_started = datetime.fromisoformat("2026-08-24T12:54:07+00:00")

        clamped_started, clamped = ws_health_audit._effective_window_start(
            "15m", phase_started, now
        )
        self.assertTrue(clamped)
        self.assertEqual(
            CANDLE_RETENTION,
            ws_health_audit._expected_close_count("15m", clamped_started, now),
        )

        kept_started, kept_clamped = ws_health_audit._effective_window_start(
            "1H", phase_started, now
        )
        self.assertFalse(kept_clamped)
        self.assertEqual(phase_started, kept_started)

        self.assertEqual(
            (None, False),
            ws_health_audit._effective_window_start("15m", None, now),
        )


class RestRotationAuditTests(unittest.TestCase):



    def test_recovery_metric_ignores_planned_rebuild(self) -> None:
        events = [
            {"id": 1, "ts": "2026-08-23T01:00:00Z", "group_id": "g1", "event": "disconnected", "detail": "cancelled"},
            {"id": 2, "ts": "2026-08-23T01:00:02Z", "group_id": "g1", "event": "ready", "detail": None},
            {"id": 3, "ts": "2026-08-23T01:01:00Z", "group_id": "g2", "event": "disconnected", "detail": "network"},
            {"id": 4, "ts": "2026-08-23T01:01:06Z", "group_id": "g2", "event": "ready", "detail": None},
        ]
        self.assertEqual([6.0], ws_health_audit._actual_recovery_seconds(events))




if __name__ == "__main__":
    unittest.main()
