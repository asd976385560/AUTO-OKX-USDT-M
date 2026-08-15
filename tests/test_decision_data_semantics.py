import json
from datetime import datetime, timezone
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _p in (SCRIPTS,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import decision_briefing  # noqa: E402
from collectors import trades_writer  # noqa: E402


TRADE_EXPERIENCES_DDL = """
CREATE TABLE trade_experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    profile TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    action TEXT,
    regime TEXT,
    regime_stale INTEGER DEFAULT 0,
    score_total INTEGER,
    confidence REAL,
    playbook_ref TEXT,
    hypothesis_id TEXT,
    market_snapshot TEXT,
    experience_vector TEXT,
    pnl_pct REAL,
    hold_hours REAL,
    is_gross_profit_close INTEGER,
    status TEXT DEFAULT 'open',
    raw TEXT,
    experience_summary TEXT,
    open_sz REAL,
    remaining_sz REAL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    close_count INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT
)
"""


class DxyObservationSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE cross_market(ts TEXT, dxy REAL, source_meta TEXT)"
        )

    def tearDown(self):
        self.db.close()

    def _insert(self, ts, value, source_as_of):
        meta = {"dxy": {"source": "fred"}}
        if source_as_of is not None:
            meta["dxy"]["source_as_of"] = source_as_of
        self.db.execute(
            "INSERT INTO cross_market(ts,dxy,source_meta) VALUES(?,?,?)",
            (ts, value, json.dumps(meta)),
        )

    def test_observation_rows_deduplicate_hourly_carry_forward(self):
        self._insert("2026-07-27T21:02:02Z", 120.7105, "2026-07-24")
        self._insert("2026-07-28T01:02:02Z", 120.7105, "2026-07-24")
        self._insert("2026-07-31T12:02:02Z", 120.7105, "2026-07-24")
        self._insert("2026-07-20T21:02:02Z", 120.5315, "2026-07-17")
        self._insert("2026-07-13T21:02:02Z", 120.5046, "2026-07-10")
        self._insert("2026-07-31T08:02:02Z", 999.0, None)

        rows = decision_briefing._dxy_observation_rows(self.db)

        self.assertEqual([row["observation_date"] for row in rows], [
            "2026-07-24", "2026-07-17", "2026-07-10"
        ])
        self.assertEqual([row["dxy"] for row in rows], [
            120.7105, 120.5315, 120.5046
        ])

    def test_three_day_carry_forward_suppresses_zone_but_not_facts(self):
        observations = [
            {"observation_date": "2026-07-24", "dxy": 120.7105},
            {"observation_date": "2026-07-17", "dxy": 120.5315},
            {"observation_date": "2026-07-10", "dxy": 120.5046},
        ]

        fresh = decision_briefing._dxy_zone_state(
            120.7105, observations, frozen_days=1
        )
        stale = decision_briefing._dxy_zone_state(
            120.7105, observations, frozen_days=3
        )

        self.assertEqual(fresh["status"], "ELEVATED")
        self.assertAlmostEqual(fresh["z"], 1.4040, places=3)
        self.assertEqual(stale["status"], "STALE")
        self.assertIsNone(stale["z"])
        self.assertEqual(stale["reason"], "carry_forward_stale")

    def test_missing_source_dates_never_become_fake_observations(self):
        observations = [
            {"observation_date": "2026-07-24", "dxy": 120.7105},
            {"observation_date": "2026-07-17", "dxy": 120.5315},
        ]

        result = decision_briefing._dxy_zone_state(
            120.7105, observations, frozen_days=1
        )

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "observation_sample_insufficient")


class DecisionMarketDataSemanticsTests(unittest.TestCase):
    def test_requested_timeframes_and_closed_candle_only(self):
        self.assertEqual(decision_briefing.DECISION_TIMEFRAMES, ("15m", "1H", "4H"))
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE kline_cache(ts TEXT,symbol TEXT,tf TEXT,c REAL,ma5 REAL,"
            "ma20 REAL,rsi14 REAL,macd_hist REAL)"
        )
        con.executemany(
            "INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?)",
            [
                ("2026-08-11T14:00:00Z", "BTC-USDT-SWAP", "15m", 1, 1, 2, 40, -1),
                # This bar is still open at evaluation 14:16 and must not be read.
                ("2026-08-11T14:15:00Z", "BTC-USDT-SWAP", "15m", 3, 3, 2, 70, 1),
            ],
        )
        row = decision_briefing.latest_closed_kline(
            con, "BTC-USDT-SWAP", "15m", "2026-08-11T14:16:00Z"
        )
        con.close()
        self.assertEqual(row["ts"], "2026-08-11T14:00:00Z")
        self.assertEqual(row["macd_hist"], -1)

    def test_quote_volume_uses_okx_contract_value(self):
        self.assertEqual(
            decision_briefing.quote_volume_usd(2, 300_000, 10),
            6_000_000,
        )
        self.assertIsNone(decision_briefing.quote_volume_usd(2, 300_000, None))

    def test_positioning_evidence_fails_closed_on_stale_or_incomplete_rows(self):
        expected = {
            "AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"
        }
        rows = [
            {
                "symbol": symbol,
                "ts": "2026-08-12T01:30:00Z",
                "long_ratio": 0.6,
                "short_ratio": 0.4,
                "long_short_ratio": 1.5,
            }
            for symbol in sorted(expected)
        ]
        evaluated = datetime(2026, 8, 12, 2, 45, tzinfo=timezone.utc)
        fresh = decision_briefing.positioning_evidence_quality(
            rows, expected, now=evaluated)
        stale_rows = [dict(row) for row in rows]
        stale_rows[0]["ts"] = "2026-08-12T01:00:00Z"
        stale = decision_briefing.positioning_evidence_quality(
            stale_rows, expected, now=evaluated)
        incomplete = decision_briefing.positioning_evidence_quality(
            rows[:-1], expected, now=evaluated)

        self.assertEqual("PASSED", fresh["status"])
        self.assertEqual(75.0, fresh["maximum_source_age_minutes"])
        self.assertEqual("NOT_MET", stale["status"])
        self.assertIn(
            "source_ts_stale_for_decision",
            stale["invalid_rows"][0]["reasons"],
        )
        self.assertEqual("NOT_MET", incomplete["status"])
        self.assertEqual(2 / 3, incomplete["coverage_rate"])

    def test_candidate_soft_evidence_is_informative_but_never_a_gate(self):
        result = decision_briefing.candidate_soft_evidence(
            {
                "spread_bps": 1.25,
                "imbalance_25bp": -0.30,
                "taker_buy_ratio": 0.62,
                "cvd_notional_usd": 45_000,
                "sample_count": 500,
                "sample_span_ms": 120_000,
                "buy_slippage_500usd_bps": 0.8,
                "sell_slippage_500usd_bps": 1.1,
            },
            {"long_short_ratio": 1.4},
            positioning_batch_passed=True,
        )
        self.assertTrue(result["micro_available"])
        self.assertTrue(result["positioning_available"])
        self.assertIn("spread=1.25bp", result["text"])
        self.assertIn("imb=-0.30", result["text"])
        self.assertIn("flowN/span=500/120s", result["text"])
        self.assertIn("acctL/S=1.40", result["text"])

        unavailable = decision_briefing.candidate_soft_evidence(
            {"spread_bps": float("nan"), "imbalance_25bp": 0.0},
            {"long_short_ratio": 1.4},
            positioning_batch_passed=False,
        )
        self.assertFalse(unavailable["micro_available"])
        self.assertFalse(unavailable["positioning_available"])
        self.assertEqual("µ=N/A acctL/S=N/A", unavailable["text"])

        stale_flow = decision_briefing.candidate_soft_evidence({
            "spread_bps": 1.0,
            "imbalance_25bp": 0.1,
            "taker_buy_ratio": 0.9,
            "cvd_notional_usd": 99_000,
            "sample_count": 500,
            "sample_span_ms": 31 * 60_000,
        })
        self.assertTrue(stale_flow["micro_available"])
        self.assertIn("flow=N/A(stale_span)", stale_flow["text"])
        self.assertNotIn("buy=90%", stale_flow["text"])


class ExperienceRegimePointInTimeTests(unittest.TestCase):
    @staticmethod
    def _make_regime_db(root: Path):
        con = sqlite3.connect(root / "regime.db")
        try:
            con.execute("CREATE TABLE cross_market(ts TEXT, regime TEXT)")
            con.executemany(
                "INSERT INTO cross_market(ts,regime) VALUES(?,?)",
                [
                    ("2026-07-28T01:00:00Z", "range"),
                    ("2026-07-28T03:00:00Z", "trend_up"),
                ],
            )
            con.commit()
        finally:
            con.close()

    def test_writer_fills_missing_regime_at_trade_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_regime_db(root)
            con = sqlite3.connect(root / "account.db")
            try:
                con.execute(TRADE_EXPERIENCES_DDL)
                con.commit()
            finally:
                con.close()

            payload = {
                "cycle_id": "2026-07-28T10:00",
                "decision_protocol": "decision_card_v1",
                "trades": [
                    {
                        "symbol": "AAPL-USDT-SWAP",
                        "action": "open",
                        "side": "long",
                        "sz": 1,
                        "fill_ts": "2026-07-28T10:00:00+08:00",
                    }
                ],
            }
            with mock.patch.dict(
                "os.environ", {"OKX_ACCOUNT_DB": str(root / "account.db")}
            ):
                result = trades_writer.write_experiences(
                    payload, "live", "2026-07-28 10:00:00"
                )

            self.assertEqual(result, {"exp": 1})
            con = sqlite3.connect(root / "account.db")
            try:
                row = con.execute(
                    "SELECT regime,experience_vector FROM trade_experiences"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(row[0], "range")
            # Wave2 序9：writer 落 v2 特征载荷；regime 语义直接看 features.regime
            stored_vec = json.loads(row[1])
            self.assertEqual(stored_vec.get("v"), 2)
            self.assertEqual(stored_vec["features"]["regime"], "range")
            self.assertEqual(stored_vec["features"]["side"], "long")

if __name__ == "__main__":
    unittest.main()
