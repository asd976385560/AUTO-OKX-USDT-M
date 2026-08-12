from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import collect_market_features  # noqa: E402


class MarketFeatureSelectionQualityTests(unittest.TestCase):
    def test_liquidity_and_oi_eligible_pool_precedes_focus_and_uses_ctval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            focus = Path(tmp) / "focus.md"
            focus.write_text("## 关注币种\n- FOCUS\n", encoding="utf-8")
            con = sqlite3.connect(":memory:")
            con.executescript(
                """
                CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT,last REAL,vol24h REAL);
                CREATE TABLE derivatives(ts TEXT,symbol TEXT,oi_usd REAL);
                CREATE TABLE instruments_cache(instId TEXT,ctVal REAL);
                """
            )
            tick_ts = "2026-08-11T14:15:00Z"
            rows = [
                # qv=6m only after multiplying ctVal=10; this catches the old formula.
                (tick_ts, "ELIGIBLE-USDT-SWAP", 2.0, 300_000.0),
                (tick_ts, "ELIGIBLE2-USDT-SWAP", 1.0, 7_000_000.0),
                (tick_ts, "FOCUS-USDT-SWAP", 1.0, 20_000_000.0),
            ]
            con.executemany("INSERT INTO tick_snapshots VALUES(?,?,?,?)", rows)
            con.executemany(
                "INSERT INTO instruments_cache VALUES(?,?)",
                [
                    ("ELIGIBLE-USDT-SWAP", 10.0),
                    ("ELIGIBLE2-USDT-SWAP", 1.0),
                    ("FOCUS-USDT-SWAP", 1.0),
                ],
            )
            con.executemany(
                "INSERT INTO derivatives VALUES(?,?,?)",
                [
                    (tick_ts, "ELIGIBLE-USDT-SWAP", 6_000_000.0),
                    (tick_ts, "ELIGIBLE2-USDT-SWAP", 6_000_000.0),
                    (tick_ts, "FOCUS-USDT-SWAP", 1_000_000.0),
                ],
            )
            selected = collect_market_features.select_symbols(con, 2, focus)
            con.close()
            self.assertEqual(
                selected,
                ["ELIGIBLE2-USDT-SWAP", "ELIGIBLE-USDT-SWAP"],
            )

    def test_positioning_universe_does_not_apply_liquidity_or_oi_gate(self) -> None:
        con = sqlite3.connect(":memory:")
        con.executescript(
            """
            CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT,last REAL,vol24h REAL);
            CREATE TABLE instruments_cache(instId TEXT,ctVal REAL);
            """
        )
        tick_ts = "2026-08-11T18:00:02Z"
        rows = [
            (tick_ts, "BTC-USDT-SWAP", 100.0, 1_000.0),
            (tick_ts, "TINY-USDT-SWAP", 0.1, 2.0),
            (tick_ts, "NOT-USDC-SWAP", 1.0, 9_000_000.0),
        ]
        con.executemany("INSERT INTO tick_snapshots VALUES(?,?,?,?)", rows)
        con.executemany(
            "INSERT INTO instruments_cache VALUES(?,?)",
            [(row[1], 1.0) for row in rows],
        )
        selected = collect_market_features.select_positioning_symbols(con, 10)
        con.close()
        self.assertEqual(selected[0], "BTC-USDT-SWAP")
        self.assertIn("TINY-USDT-SWAP", selected)
        self.assertNotIn("NOT-USDC-SWAP", selected)


if __name__ == "__main__":
    unittest.main()
