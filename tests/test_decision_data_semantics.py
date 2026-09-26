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
# backfill_experience_regime 于 2026-08-06 归档进 archive/migrations/。
# 本用例**刻意跟着走**而不是一并删除：它验的是回填的候选判定与幂等，而归档
# ≠ 销毁——主人仍可授权单次重跑，届时这层回归就是它敢跑的依据。
# （archive/README 的「归档脚本不得被新代码导入」约束的是生产代码，不是钉住
#   其行为的回归用例。）
MIGRATIONS = SCRIPTS / "archive" / "migrations"
for _p in (SCRIPTS, MIGRATIONS):
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

    def test_current_cycle_contract_oi_exposes_only_valid_direct_rows(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE market_contract_statistics("
            "ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,"
            "timeframe TEXT,oi_contracts REAL,oi_ccy REAL,oi_usd REAL,"
            "taker_sell_usd REAL,taker_buy_usd REAL,taker_buy_ratio REAL,"
            "raw TEXT,source TEXT)"
        )
        cycle = "2026-08-22T08:45"
        source = decision_briefing.CONTRACT_STATS_SOURCE
        rows = [
            (
                "2026-08-22T00:30:00Z", "2026-08-22T00:46:37Z",
                cycle, "DIRECT-USDT-SWAP", "15m", 1, 1, 9_000_000,
                40, 60, 0.6, "{}", source,
            ),
            (
                "2026-08-22T00:30:00Z", "2026-08-22T00:46:43Z",
                cycle, "FALLBACK-USDT-SWAP", "15m", 1, 1, 8_000_000,
                50, 50, 0.5,
                json.dumps({
                    "method":
                    "official_public_oi_trades_candle_reconciled_fallback"
                }),
                source,
            ),
            (
                "2026-08-22T00:30:00Z", "2026-08-22T00:46:43Z",
                cycle, "CARRY-USDT-SWAP", "15m", 1, 1, 7_000_000,
                50, 50, 0.5,
                json.dumps({"method": "official_previous_batch_carry_forward"}),
                source,
            ),
        ]
        con.executemany(
            "INSERT INTO market_contract_statistics "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

        evidence = decision_briefing.current_cycle_contract_oi_evidence(
            con,
            cycle,
            {"DIRECT-USDT-SWAP", "FALLBACK-USDT-SWAP", "CARRY-USDT-SWAP"},
            available_at="2026-08-22T00:47:00Z",
        )
        con.close()

        self.assertEqual("AVAILABLE", evidence["status"])
        self.assertEqual(2, evidence["valid_symbols"])
        self.assertEqual(2, evidence["collected_timestamp_count"])
        self.assertEqual({
            "DIRECT-USDT-SWAP": 9_000_000.0,
            "FALLBACK-USDT-SWAP": 8_000_000.0,
        }, evidence["values"])
        self.assertEqual(
            1,
            evidence["invalid_reason_counts"]
            ["method:official_previous_batch_carry_forward"],
        )

    def test_candidate_rows_never_join_previous_derivatives_snapshot(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(
            "CREATE TABLE tick_snapshots("
            "ts TEXT,symbol TEXT,last REAL,chg24h REAL,vol24h REAL);"
            "CREATE TABLE derivatives("
            "ts TEXT,symbol TEXT,oi_usd REAL,funding_rate REAL);"
            "CREATE TABLE instruments_cache(instId TEXT,ctVal REAL);"
        )
        current = "2026-08-22T00:45:02Z"
        con.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
            [
                (current, "STALE-USDT-SWAP", 2, 1, 3_000_000),
                (current, "EXACT-USDT-SWAP", 2, 1, 3_000_000),
            ],
        )
        con.executemany(
            "INSERT INTO derivatives VALUES(?,?,?,?)",
            [
                ("2026-08-22T00:30:02Z", "STALE-USDT-SWAP", 9_000_000, 0.1),
                (current, "EXACT-USDT-SWAP", 8_000_000, 0.2),
            ],
        )
        con.executemany(
            "INSERT INTO instruments_cache VALUES(?,?)",
            [("STALE-USDT-SWAP", 1), ("EXACT-USDT-SWAP", 1)],
        )

        rows = {
            row["symbol"]: row
            for row in decision_briefing.candidate_market_rows(con, current)
        }
        con.close()

        self.assertIsNone(rows["STALE-USDT-SWAP"]["oi_usd"])
        self.assertIsNone(rows["STALE-USDT-SWAP"]["funding_rate"])
        self.assertEqual(8_000_000, rows["EXACT-USDT-SWAP"]["oi_usd"])
        self.assertEqual(0.2, rows["EXACT-USDT-SWAP"]["funding_rate"])

    def test_candidate_oi_prefers_instantaneous_then_same_cycle_direct(self):
        evidence = {"values": {"AAA-USDT-SWAP": 8_000_000.0}}
        self.assertEqual(
            (9_000_000.0, "derivatives_current"),
            decision_briefing.resolve_candidate_oi_usd(
                9_000_000, "AAA-USDT-SWAP", evidence),
        )
        self.assertEqual(
            (8_000_000.0, "contract_statistics_current_cycle_direct"),
            decision_briefing.resolve_candidate_oi_usd(
                None, "AAA-USDT-SWAP", evidence),
        )
        self.assertEqual(
            (None, "unavailable"),
            decision_briefing.resolve_candidate_oi_usd(
                None, "MISSING-USDT-SWAP", evidence),
        )

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
        self.assertIn("点差=1.25bp", result["text"])
        self.assertIn("失衡=-0.30", result["text"])
        self.assertIn("流样本/跨度=500/120s", result["text"])
        self.assertIn("账户多空比=1.40", result["text"])

        unavailable = decision_briefing.candidate_soft_evidence(
            {"spread_bps": float("nan"), "imbalance_25bp": 0.0},
            {"long_short_ratio": 1.4},
            positioning_batch_passed=False,
        )
        self.assertFalse(unavailable["micro_available"])
        self.assertFalse(unavailable["positioning_available"])
        self.assertEqual("µ=N/A 账户多空比=N/A", unavailable["text"])

        stale_flow = decision_briefing.candidate_soft_evidence({
            "spread_bps": 1.0,
            "imbalance_25bp": 0.1,
            "taker_buy_ratio": 0.9,
            "cvd_notional_usd": 99_000,
            "sample_count": 500,
            "sample_span_ms": 31 * 60_000,
        })
        self.assertTrue(stale_flow["micro_available"])
        self.assertIn("流=N/A(样本跨度过期)", stale_flow["text"])
        self.assertNotIn("买盘=90%", stale_flow["text"])


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
            # 前向 v4（2026-09-26 对照 V3）：writer 落 v4 epoch 并另存 features_v3；
            # regime 仍直接看 features.regime。
            stored_vec = json.loads(row[1])
            self.assertEqual(stored_vec.get("v"), 4)
            self.assertEqual(
                stored_vec.get("feature_epoch"),
                "experience_features_v4_v3parity",
            )
            self.assertEqual(stored_vec["features"].get("v"), 4)
            self.assertEqual(
                stored_vec["features_v3"].get("feature_epoch"),
                "experience_features_v3_strict_24h",
            )
            self.assertEqual(stored_vec["features"]["regime"], "range")
            self.assertEqual(stored_vec["features"]["side"], "long")



if __name__ == "__main__":
    unittest.main()
