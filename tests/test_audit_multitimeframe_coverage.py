from __future__ import annotations

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
            # Current/forming bars must not substitute for the exact closed bar.
            current_ts = {
                "15m": "2026-08-12T06:30:00Z",
                "1H": "2026-08-12T06:00:00Z",
                "4H": "2026-08-12T04:00:00Z",
            }[timeframe]
            con.execute(
                "INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (current_ts, "A-USDT-SWAP", timeframe, *ready),
            )
        con.commit()
        con.close()
        return path

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
            gap = next(g for g in row["gaps"] if g["symbol"] == "C-USDT-SWAP")
            self.assertEqual("insufficient_history", gap["classification"])

    def test_exact_closed_bar_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            con = sqlite3.connect(path)
            con.execute(
                "DELETE FROM kline_cache WHERE symbol='B-USDT-SWAP' "
                "AND tf='4H' AND ts='2026-08-12T00:00:00Z'"
            )
            con.execute(
                "INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("2026-08-12T04:00:00Z", "B-USDT-SWAP", "4H",
                 1.0, 1.2, 0.9, 1.1, 10.0, 1.05, 1.0, 0.1, 55.0, 0.02),
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


if __name__ == "__main__":
    unittest.main()
