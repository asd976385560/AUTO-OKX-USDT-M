import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

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
    hit_1R INTEGER,
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
            self.assertEqual(json.loads(row[1])[2:5], [0.0, 1.0, 0.0])

if __name__ == "__main__":
    unittest.main()
