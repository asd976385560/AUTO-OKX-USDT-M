from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_multitimeframe_coverage as audit  # noqa: E402


KLINE_DDL = """
CREATE TABLE kline_cache(
  ts TEXT NOT NULL,symbol TEXT NOT NULL,tf TEXT NOT NULL,
  o REAL,h REAL,l REAL,c REAL,v REAL,
  ma5 REAL,ma20 REAL,atr14 REAL,rsi14 REAL,macd_hist REAL,
  PRIMARY KEY(ts,symbol,tf)
)
"""


class MultitimeframeCoverageAuditTests(unittest.TestCase):
    def _db(self, root: str) -> Path:
        path = Path(root) / "market.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT)")
        con.execute(
            "CREATE TABLE instruments_cache("
            "instId TEXT PRIMARY KEY,list_time_utc TEXT)"
        )
        con.executemany(
            "INSERT INTO instruments_cache VALUES(?,?)",
            [
                ("A-USDT-SWAP", "2026-07-01T00:00:00Z"),
                ("B-USDT-SWAP", "2026-07-01T00:00:00Z"),
                ("C-USDT-SWAP", "2026-08-12T05:45:00Z"),
            ],
        )
        con.execute(KLINE_DDL)
        con.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?)",
            [
                ("2026-08-12T06:30:02Z", "A-USDT-SWAP"),
                ("2026-08-12T06:30:02Z", "B-USDT-SWAP"),
                ("2026-08-12T06:30:02Z", "C-USDT-SWAP"),
            ],
        )
        ready = (1.0, 1.2, 0.9, 1.1, 10.0, 1.05, 1.0, 0.1, 55.0, 0.02)
        for timeframe, bar_ts in (
            ("15m", "2026-08-12T06:15:00Z"),
            ("1H", "2026-08-12T05:00:00Z"),
            ("4H", "2026-08-12T00:00:00Z"),
        ):
            closed = datetime.fromisoformat(bar_ts.replace("Z", "+00:00"))
            step = {
                "15m": timedelta(minutes=15),
                "1H": timedelta(hours=1),
                "4H": timedelta(hours=4),
            }[timeframe]
            for symbol in ("A-USDT-SWAP", "B-USDT-SWAP"):
                con.execute(
                    "INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bar_ts, symbol, timeframe, *ready),
                )
                for index in range(
                    1, audit.MINIMUM_BARS_FOR_FULL_INDICATORS
                ):
                    history_ts = (closed - step * index).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    con.execute(
                        "INSERT INTO kline_cache "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (history_ts, symbol, timeframe, *ready),
                    )
            con.execute(
                "INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bar_ts, "C-USDT-SWAP", timeframe,
                 *ready[:5], None, None, None, None, None),
            )
            # The immediate successor proves the preceding target row was
            # fetched after its close; it is never substituted as the target.
            current_ts = {
                "15m": "2026-08-12T06:30:00Z",
                "1H": "2026-08-12T06:00:00Z",
                "4H": "2026-08-12T04:00:00Z",
            }[timeframe]
            for symbol in ("A-USDT-SWAP", "B-USDT-SWAP", "C-USDT-SWAP"):
                con.execute(
                    "INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (current_ts, symbol, timeframe, *ready),
                )
        con.commit()
        con.close()
        return path

    @staticmethod
    def _ws_first_cache(root: Path, *, mismatch_symbol: str | None = None) -> Path:
        cache = root / "ws_market_cache.db"
        con = sqlite3.connect(cache)
        con.execute(
            "CREATE TABLE candles("
            "inst_id TEXT,timeframe TEXT,ts_ms INTEGER,"
            "open TEXT,high TEXT,low TEXT,close TEXT,"
            "volume TEXT,volume_ccy TEXT,volume_quote TEXT,"
            "confirm INTEGER,bar_end_ms INTEGER,close_latency_ms INTEGER,"
            "received_at TEXT,conn_epoch TEXT,source TEXT,"
            "PRIMARY KEY(inst_id,timeframe,ts_ms))"
        )
        for timeframe, expected, successor in (
            ("15m", "2026-08-12T06:15:00Z", "2026-08-12T06:30:00Z"),
            ("1H", "2026-08-12T05:00:00Z", "2026-08-12T06:00:00Z"),
            ("4H", "2026-08-12T00:00:00Z", "2026-08-12T04:00:00Z"),
        ):
            for symbol in ("A-USDT-SWAP", "B-USDT-SWAP", "C-USDT-SWAP"):
                close = "9.9" if symbol == mismatch_symbol else "1.1"
                con.execute(
                    "INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        symbol, timeframe,
                        int(datetime.fromisoformat(
                            expected.replace("Z", "+00:00")
                        ).timestamp() * 1000),
                        "1.0", "1.2", "0.9", close,
                        "10.0", "10.0", "10.0", 1,
                        int(datetime.fromisoformat(
                            successor.replace("Z", "+00:00")
                        ).timestamp() * 1000),
                        5, "2026-08-12T06:30:05Z", "epoch-1", "ws",
                    ),
                )
        con.commit()
        con.close()
        (root / "ws_market_source.json").write_text(
            json.dumps({
                "schema_version": 1,
                "mode": "ws_first",
                "pending_mode": None,
                "cache_db": str(cache),
            }),
            encoding="utf-8",
        )
        return cache

    @staticmethod
    def _remove_successors(path: Path) -> None:
        con = sqlite3.connect(path)
        for timeframe, successor in (
            ("15m", "2026-08-12T06:30:00Z"),
            ("1H", "2026-08-12T06:00:00Z"),
            ("4H", "2026-08-12T04:00:00Z"),
        ):
            con.execute(
                "DELETE FROM kline_cache WHERE tf=? AND ts=?",
                (timeframe, successor),
            )
        con.commit()
        con.close()

    def test_separates_raw_completeness_from_new_listing_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = audit.audit_multitimeframe_coverage(
                self._db(temporary),
                minimum_rate=0.99,
                now=datetime(2026, 8, 12, 6, 30, 2, tzinfo=timezone.utc),
            )
        self.assertEqual("PASSED", payload["data_completeness_status"])
        self.assertEqual("NOT_MET", payload["analysis_readiness_status"])
        self.assertEqual("NOT_MET", payload["status"])
        for row in payload["timeframes"]:
            self.assertEqual(1.0, row["raw_ohlcv_coverage_rate"])
            self.assertEqual(0.666667, row["analysis_ready_rate"])
            projection = row["analysis_readiness_projection"]
            self.assertEqual(
                "PROJECTED_FROM_OFFICIAL_WARMUP", projection["status"])
            self.assertEqual(3, projection["required_ready_symbols"])
            self.assertEqual(1, projection["additional_ready_symbols_needed"])
            self.assertIsNotNone(
                projection["projected_threshold_ready_at_utc"])
            gap = next(g for g in row["gaps"] if g["symbol"] == "C-USDT-SWAP")
            self.assertEqual("insufficient_history", gap["classification"])
            self.assertEqual(
                "official_new_listing_warmup", gap["history_semantics"])
            self.assertIsNotNone(gap["earliest_full_indicator_ready_at_utc"])

    def test_exact_closed_bar_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            con = sqlite3.connect(path)
            con.execute(
                "DELETE FROM kline_cache WHERE symbol='B-USDT-SWAP' "
                "AND tf='4H' AND ts='2026-08-12T00:00:00Z'"
            )
            con.commit()
            con.close()
            payload = audit.audit_multitimeframe_coverage(
                path,
                minimum_rate=0.5,
                now=datetime(2026, 8, 12, 6, 30, 2, tzinfo=timezone.utc),
            )
        four_hour = next(
            row for row in payload["timeframes"] if row["timeframe"] == "4H")
        gap = next(
            item for item in four_hour["gaps"]
            if item["symbol"] == "B-USDT-SWAP")
        self.assertEqual("source_data_invalid", gap["classification"])
        self.assertEqual(["missing_closed_bar"], gap["raw_errors"])

    def test_target_row_without_successor_is_not_proven_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            con = sqlite3.connect(path)
            con.execute(
                "DELETE FROM kline_cache WHERE symbol='B-USDT-SWAP' "
                "AND tf='1H' AND ts='2026-08-12T06:00:00Z'"
            )
            con.commit()
            con.close()
            payload = audit.audit_multitimeframe_coverage(
                path,
                minimum_rate=0.5,
                now=datetime(2026, 8, 12, 6, 30, 2, tzinfo=timezone.utc),
            )

        one_hour = next(
            row for row in payload["timeframes"]
            if row["timeframe"] == "1H")
        gap = next(
            item for item in one_hour["gaps"]
            if item["symbol"] == "B-USDT-SWAP")
        self.assertEqual("source_data_invalid", gap["classification"])
        self.assertEqual(
            ["closed_state_unproven_no_successor_bar"], gap["raw_errors"])
        self.assertFalse(gap["closed_bar_proof"]["proven"])

    def test_ws_first_confirmed_rows_are_equivalent_closure_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._db(temporary)
            self._remove_successors(path)
            self._ws_first_cache(root)

            payload = audit.audit_multitimeframe_coverage(
                path,
                minimum_rate=0.99,
                now=datetime(2026, 8, 12, 6, 30, 2, tzinfo=timezone.utc),
            )

        self.assertEqual("PASSED", payload["data_completeness_status"])
        self.assertEqual("NOT_MET", payload["analysis_readiness_status"])
        self.assertEqual("ws_first", payload[
            "closed_bar_proof_context"]["source_mode"])
        for row in payload["timeframes"]:
            self.assertEqual(3, row["closed_bar_proven_symbols"])
            self.assertEqual(1.0, row["closed_bar_proof_rate"])
            self.assertEqual(
                {"ws_confirmed_candle_exact_match": 3},
                row["closed_bar_proof_method_counts"],
            )

    def test_ws_confirmation_mismatch_stays_in_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._db(temporary)
            self._remove_successors(path)
            self._ws_first_cache(root, mismatch_symbol="B-USDT-SWAP")

            payload = audit.audit_multitimeframe_coverage(
                path,
                minimum_rate=0.5,
                now=datetime(2026, 8, 12, 6, 30, 2, tzinfo=timezone.utc),
            )

        for row in payload["timeframes"]:
            gap = next(
                item for item in row["gaps"]
                if item["symbol"] == "B-USDT-SWAP")
            self.assertEqual("source_data_invalid", gap["classification"])
            confirmation = gap["closed_bar_proof"]["ws_confirmation"]
            self.assertEqual(
                "market_ws_ohlcv_mismatch", confirmation["reason"])
            self.assertEqual(["c"], confirmation["mismatched_fields"])

    def test_missing_indicator_after_sufficient_history_is_not_new_listing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            con = sqlite3.connect(path)
            closed = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            for index in range(1, audit.MINIMUM_BARS_FOR_FULL_INDICATORS):
                bar_ts = (
                    closed - timedelta(hours=4 * index)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                con.execute(
                    "INSERT OR IGNORE INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        bar_ts,
                        "C-USDT-SWAP", "4H",
                        1.0, 1.2, 0.9, 1.1, 10.0,
                        None, None, None, None, None,
                    ),
                )
            con.commit()
            con.close()
            payload = audit.audit_multitimeframe_coverage(
                path,
                now=datetime(2026, 8, 12, 6, 30, 2, tzinfo=timezone.utc),
            )
        four_hour = next(
            row for row in payload["timeframes"] if row["timeframe"] == "4H")
        gap = next(
            item for item in four_hour["gaps"]
            if item["symbol"] == "C-USDT-SWAP")
        self.assertEqual("indicator_invalid", gap["classification"])

    def test_prefilled_indicators_do_not_bypass_minimum_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            con = sqlite3.connect(path)
            con.execute(
                "DELETE FROM kline_cache WHERE symbol='A-USDT-SWAP' "
                "AND tf='4H' AND ts<'2026-08-12T00:00:00Z'"
            )
            con.commit()
            con.close()
            payload = audit.audit_multitimeframe_coverage(
                path,
                now=datetime(2026, 8, 12, 6, 30, 2, tzinfo=timezone.utc),
            )
        four_hour = next(
            row for row in payload["timeframes"] if row["timeframe"] == "4H")
        gap = next(
            item for item in four_hour["gaps"]
            if item["symbol"] == "A-USDT-SWAP")
        self.assertEqual("insufficient_history", gap["classification"])
        self.assertEqual(1, gap["bars_seen"])
        self.assertEqual("historical_collection_gap", gap["history_semantics"])
        self.assertEqual(
            "NO_DETERMINISTIC_PROJECTION",
            four_hour["analysis_readiness_projection"]["status"],
        )


if __name__ == "__main__":
    unittest.main()
