from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import universe_judgment_snapshot as snapshot  # noqa: E402


def create_market(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tick_snapshots(
          ts TEXT NOT NULL, symbol TEXT NOT NULL, last REAL, bid REAL, ask REAL,
          vol24h REAL, fundingRate REAL, oi REAL, chg24h REAL,
          PRIMARY KEY(ts,symbol)
        );
        CREATE TABLE derivatives(
          ts TEXT NOT NULL, symbol TEXT NOT NULL, funding_rate REAL,
          funding_time TEXT, next_funding_time TEXT, premium REAL,
          oi REAL, oi_ccy REAL, oi_usd REAL, PRIMARY KEY(ts,symbol)
        );
        CREATE TABLE kline_cache(
          ts TEXT NOT NULL, symbol TEXT NOT NULL, tf TEXT NOT NULL,
          o REAL,h REAL,l REAL,c REAL,v REAL,ma5 REAL,ma20 REAL,atr14 REAL,
          rsi14 REAL,macd_hist REAL,PRIMARY KEY(ts,symbol,tf)
        );
        CREATE TABLE instruments_cache(instId TEXT PRIMARY KEY,ctVal REAL,lotSz REAL);
        CREATE TABLE instrument_class(symbol TEXT PRIMARY KEY,asset_class TEXT,source TEXT,updated_at TEXT);
        CREATE TABLE market_microstructure(
          ts TEXT NOT NULL,symbol TEXT NOT NULL,PRIMARY KEY(ts,symbol)
        );
        CREATE TABLE market_trade_flow(
          ts TEXT NOT NULL,symbol TEXT NOT NULL,PRIMARY KEY(ts,symbol)
        );
        """
    )
    return con


def create_news(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE news_items(source TEXT,symbol TEXT,first_seen_at TEXT,"
        "ingested_at TEXT,ts TEXT)"
    )
    return con


def insert_symbol(con: sqlite3.Connection, symbol: str, *, ct_val: float = 10.0) -> None:
    tick_ts = "2026-08-11T14:15:00Z"
    con.execute(
        "INSERT INTO tick_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
        (tick_ts, symbol, 2.0, 1.99, 2.01, 300000.0, 0.0001, 100.0, 1.0),
    )
    con.execute(
        "INSERT INTO derivatives VALUES(?,?,?,?,?,?,?,?,?)",
        (tick_ts, symbol, 0.0001, None, None, 0.0, 1.0, 1.0, 6_000_000.0),
    )
    con.execute("INSERT INTO instruments_cache VALUES(?,?,?)", (symbol, ct_val, 1.0))
    con.execute(
        "INSERT INTO instrument_class VALUES(?,?,?,?)",
        (symbol, "crypto", "test", "2026-08-11 22:00:00"),
    )
    closed_by_tf = {
        "15m": "2026-08-11T14:00:00Z",
        "1H": "2026-08-11T13:00:00Z",
        "4H": "2026-08-11T08:00:00Z",
    }
    for tf, closed_ts in closed_by_tf.items():
        for index in range(35):
            # Historical rows only need to make the indicator-history contract explicit.
            ts = f"2026-07-{index + 1:02d}T00:00:00Z" if index < 31 else f"2026-08-0{index - 30}T00:00:00Z"
            con.execute(
                "INSERT OR IGNORE INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, symbol, tf, 2.0, 2.1, 1.9, 2.0, 10.0, 2.1, 2.2, 0.1, 40.0, -0.1),
            )
        con.execute(
            "INSERT OR REPLACE INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (closed_ts, symbol, tf, 2.0, 2.1, 1.8, 1.9, 10.0, 2.0, 2.1, 0.1, 40.0, -0.2),
        )
    con.execute("INSERT INTO market_microstructure VALUES(?,?)", (tick_ts, symbol))
    con.execute("INSERT INTO market_trade_flow VALUES(?,?)", (tick_ts, symbol))


class UniverseJudgmentSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.market = create_market(self.root / "market.db")
        self.news = create_news(self.root / "news.db")
        self.evaluation = datetime(2026, 8, 11, 14, 16, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.market.close()
        self.news.close()
        self.tmp.cleanup()

    def _finish(self) -> None:
        self.news.executemany(
            "INSERT INTO news_items VALUES(?,?,?,?,?)",
            [
                ("rss_en", "TEST-USDT-SWAP", "2026-08-11 22:15:30", None, None),
                ("okx_news", None, "2026-08-11 22:15:30", None, None),
            ],
        )
        self.market.commit()
        self.news.commit()

    def test_uses_only_closed_candles_and_contract_value_for_volume(self) -> None:
        insert_symbol(self.market, "TEST-USDT-SWAP", ct_val=10.0)
        # The 14:15 candle is strongly bullish but is still open at 14:16.
        self.market.execute(
            "INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-08-11T14:15:00Z", "TEST-USDT-SWAP", "15m",
                2.0, 3.1, 1.9, 3.0, 10.0, 2.8, 2.5, 0.1, 75.0, 0.5,
            ),
        )
        self._finish()
        payload = snapshot.build_snapshot(
            self.root,
            evaluation_utc=self.evaluation,
            cycle_id="2026-08-11T22:15",
            min_screened_symbols=1,
            min_completeness_pct=90,
        )
        record = payload["records"][0]
        self.assertEqual(record["timeframes"]["15m"]["bar_ts_utc"], "2026-08-11T14:00:00Z")
        self.assertEqual(record["judgment"], "short_bias")
        self.assertEqual(record["market"]["quote_volume_24h_usd"], 6_000_000.0)
        self.assertIsNone(record["calibrated_confidence"])
        self.assertFalse(record["production_execution_authorized"])

    def test_insufficient_history_is_explicit_and_fail_closed(self) -> None:
        insert_symbol(self.market, "TEST-USDT-SWAP")
        self.market.execute(
            "UPDATE kline_cache SET ma5=NULL,ma20=NULL,atr14=NULL,rsi14=NULL,macd_hist=NULL "
            "WHERE symbol='TEST-USDT-SWAP' AND tf='4H'"
        )
        self.market.execute(
            "DELETE FROM kline_cache WHERE symbol='TEST-USDT-SWAP' AND tf='4H' "
            "AND ts NOT IN (SELECT ts FROM kline_cache WHERE symbol='TEST-USDT-SWAP' AND tf='4H' ORDER BY ts DESC LIMIT 8)"
        )
        self._finish()
        payload = snapshot.build_snapshot(
            self.root,
            evaluation_utc=self.evaluation,
            cycle_id="2026-08-11T22:15",
            min_screened_symbols=1,
        )
        record = payload["records"][0]
        self.assertEqual(record["timeframes"]["4H"]["status"], "insufficient_history")
        self.assertEqual(record["judgment"], "wait_data")
        self.assertEqual(record["execution_readiness"], "data_incomplete")
        self.assertFalse(payload["quality_gates"]["analysis_ready_at_least_target"])

    def test_main_writes_atomic_structured_artifact(self) -> None:
        insert_symbol(self.market, "TEST-USDT-SWAP")
        self._finish()
        output = self.root / "out" / "snapshot.json"
        rc = snapshot.main([
            "--db-root", str(self.root),
            "--json-out", str(output),
            "--as-of", "2026-08-11T14:16:00Z",
            "--cycle-id", "2026-08-11T22:15",
            "--min-screened-symbols", "1",
            "--min-completeness-pct", "90",
        ])
        self.assertEqual(rc, 0)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(artifact["metrics"]["judgment_records"], 1)
        self.assertEqual(artifact["orders_placed"], 0)
        self.assertFalse(artifact["production_mutation"])


if __name__ == "__main__":
    unittest.main()
