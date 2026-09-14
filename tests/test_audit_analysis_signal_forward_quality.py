from __future__ import annotations

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

import audit_analysis_signal_forward_quality as audit  # noqa: E402


def _market() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE tick_snapshots("
        "ts TEXT,symbol TEXT,last REAL,bid REAL,ask REAL,"
        "PRIMARY KEY(ts,symbol))"
    )
    return con


def _signal(side: str = "long") -> dict:
    action = "open_long" if side == "long" else "open_short"
    return {
        "cycle_id": "2026-08-11T08:00",
        "symbol": "BTC-USDT-SWAP",
        "action": action,
        "side": side,
        "confidence": 0.8,
        "decision_card": None,
        "analysis_completed_at_cst": "2026-08-11 08:05:00",
        "regime": "trend_up",
    }


class AnalysisSignalForwardQualityTests(unittest.TestCase):
    def test_entry_is_strictly_after_analysis_completion(self) -> None:
        con = _market()
        con.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
            [
                ("2026-08-11T00:05:00Z", "BTC-USDT-SWAP", 100, 99.9, 100.1),
                ("2026-08-11T00:15:00Z", "BTC-USDT-SWAP", 101, 100.9, 101.1),
                ("2026-08-11T00:30:00Z", "BTC-USDT-SWAP", 102, 101.9, 102.1),
                ("2026-08-11T01:15:00Z", "BTC-USDT-SWAP", 104, 103.9, 104.1),
                ("2026-08-11T04:15:00Z", "BTC-USDT-SWAP", 108, 107.9, 108.1),
            ],
        )
        rows = audit.label_signal(
            con,
            _signal("long"),
            cost_bps=20,
            max_delay_minutes=20,
            market_max_ts=datetime(2026, 8, 11, 5, tzinfo=timezone.utc),
        )
        con.close()
        self.assertEqual("2026-08-11T00:15:00Z", rows[0]["entry_ts_utc"])
        self.assertTrue(rows[0]["after_cost_hit"])

    def test_short_uses_bid_entry_and_ask_exit(self) -> None:
        con = _market()
        con.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
            [
                ("2026-08-11T00:15:00Z", "BTC-USDT-SWAP", 100, 99.8, 100.2),
                ("2026-08-11T00:30:00Z", "BTC-USDT-SWAP", 98, 97.8, 98.2),
                ("2026-08-11T01:15:00Z", "BTC-USDT-SWAP", 97, 96.8, 97.2),
                ("2026-08-11T04:15:00Z", "BTC-USDT-SWAP", 96, 95.8, 96.2),
            ],
        )
        rows = audit.label_signal(
            con,
            _signal("short"),
            cost_bps=20,
            max_delay_minutes=20,
            market_max_ts=datetime(2026, 8, 11, 5, tzinfo=timezone.utc),
        )
        con.close()
        self.assertEqual("bid", rows[0]["entry_price_source"])
        self.assertEqual("ask", rows[0]["exit_price_source"])
        self.assertTrue(rows[0]["after_cost_hit"])

    def test_horizon_selection_uses_discovery_metrics(self) -> None:
        rows = []
        for index in range(40):
            for horizon in audit.HORIZONS:
                hit = horizon == "1H" or (horizon == "4H" and index < 20)
                rows.append({
                    "horizon": horizon,
                    "outcome_status": "matured",
                    "after_cost_hit": hit,
                    "cycle_id": f"cycle-{index}",
                    "decision_date_cst": f"2026-07-{20 + index % 10:02d}",
                    "symbol": f"S{index}",
                    "side": "long",
                    "signed_return_after_cost": 0.01 if hit else -0.01,
                })
        selected = audit.select_horizon_from_discovery(
            rows, minimum_n=30, minimum_days=7, minimum_cycles=20)
        self.assertEqual("1H", selected["selected_horizon"])
        self.assertEqual("eligible_choice", selected["selection_status"])

    def test_missing_exit_is_distinguished_from_immature(self) -> None:
        con = _market()
        con.execute(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
            ("2026-08-11T00:15:00Z", "BTC-USDT-SWAP", 100, 99.9, 100.1),
        )
        rows = audit.label_signal(
            con,
            _signal("long"),
            cost_bps=20,
            max_delay_minutes=20,
            market_max_ts=datetime(2026, 8, 11, 0, 20, tzinfo=timezone.utc),
        )
        con.close()
        self.assertEqual("immature", rows[0]["outcome_status"])

    def test_current_policy_cohort_uses_registered_cycle_boundary(self) -> None:
        rows = [
            {"cycle_id": "2026-08-17T16:29", "horizon": "4H"},
            {"cycle_id": "2026-08-17T16:30", "horizon": "4H"},
            {"cycle_id": "not-a-cycle", "horizon": "4H"},
        ]
        selected, invalid = audit._rows_at_or_after_cycle_boundary(
            rows,
            audit._parse_time("2026-08-17T16:30:00+08:00"),
        )
        self.assertEqual(
            ["2026-08-17T16:30"],
            [row["cycle_id"] for row in selected],
        )
        self.assertEqual(1, invalid)

    def test_matured_labels_survive_tick_retention(self) -> None:
        populated = _market()
        populated.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
            [
                ("2026-08-11T00:15:00Z", "BTC-USDT-SWAP", 100, 99.9, 100.1),
                ("2026-08-11T00:30:00Z", "BTC-USDT-SWAP", 101, 100.9, 101.1),
                ("2026-08-11T01:15:00Z", "BTC-USDT-SWAP", 102, 101.9, 102.1),
                ("2026-08-11T04:15:00Z", "BTC-USDT-SWAP", 105, 104.9, 105.1),
            ],
        )
        prior = audit.label_signal(
            populated,
            _signal("long"),
            cost_bps=20,
            max_delay_minutes=20,
            market_max_ts=datetime(2026, 8, 11, 5, tzinfo=timezone.utc),
        )
        populated.close()

        pruned = _market()
        current = audit.label_signal(
            pruned,
            _signal("long"),
            cost_bps=20,
            max_delay_minutes=20,
            market_max_ts=datetime(2026, 8, 22, 5, tzinfo=timezone.utc),
        )
        pruned.close()
        merged, diagnostic = audit._merge_prior_matured_labels(
            current, prior, prior_sha256="a" * 64)

        self.assertEqual(["matured"] * 3, [row["outcome_status"] for row in merged])
        self.assertEqual(3, diagnostic["reused_after_market_retention"])
        self.assertEqual(0, diagnostic["frozen_value_drift_rows"])
        self.assertTrue(all(
            row["label_evidence_source"]
            == "prior_frozen_label_retention_reuse"
            for row in merged
        ))

    def test_prior_label_csv_round_trip_preserves_types(self) -> None:
        con = _market()
        con.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
            [
                ("2026-08-11T00:15:00Z", "BTC-USDT-SWAP", 100, 99.9, 100.1),
                ("2026-08-11T00:30:00Z", "BTC-USDT-SWAP", 101, 100.9, 101.1),
                ("2026-08-11T01:15:00Z", "BTC-USDT-SWAP", 102, 101.9, 102.1),
                ("2026-08-11T04:15:00Z", "BTC-USDT-SWAP", 105, 104.9, 105.1),
            ],
        )
        rows = audit.label_signal(
            con,
            _signal("long"),
            cost_bps=20,
            max_delay_minutes=20,
            market_max_ts=datetime(2026, 8, 11, 5, tzinfo=timezone.utc),
        )
        con.close()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.csv"
            audit._atomic_csv(path, rows)
            loaded, digest = audit._read_prior_labels(path)
        self.assertEqual(3, len(loaded))
        self.assertEqual(64, len(digest))
        self.assertIsInstance(loaded[0]["entry_executable"], float)
        self.assertIsInstance(loaded[0]["after_cost_hit"], bool)


if __name__ == "__main__":
    unittest.main()
