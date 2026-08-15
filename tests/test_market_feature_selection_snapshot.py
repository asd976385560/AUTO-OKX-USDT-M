from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import collect_market_features as features  # noqa: E402


class MarketFeatureSelectionSnapshotTests(unittest.TestCase):
    def test_insert_reuse_and_rank_change_conflict_never_overwrite(self) -> None:
        connection = sqlite3.connect(":memory:")
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
        inserted = features.freeze_market_feature_selection(
            connection,
            symbols,
            cycle_id="2026-08-12T23:15",
            collected_ts_utc="2026-08-12T15:15:02Z",
            max_symbols=3,
        )
        reused = features.freeze_market_feature_selection(
            connection,
            symbols,
            cycle_id="2026-08-12T23:15",
            collected_ts_utc="2026-08-12T15:15:30Z",
            max_symbols=3,
        )
        conflict = features.freeze_market_feature_selection(
            connection,
            list(reversed(symbols)),
            cycle_id="2026-08-12T23:15",
            collected_ts_utc="2026-08-12T15:15:45Z",
            max_symbols=3,
        )
        stored = connection.execute(
            "SELECT selection_rank,symbol "
            "FROM market_feature_selection_rows ORDER BY selection_rank"
        ).fetchall()
        connection.close()

        self.assertEqual(inserted["status"], "inserted")
        self.assertEqual(reused["status"], "reused")
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(
            stored,
            [(1, "BTC-USDT-SWAP"), (2, "ETH-USDT-SWAP"),
             (3, "SOL-USDT-SWAP")],
        )

    def test_tampered_rows_are_conflict_not_reuse(self) -> None:
        connection = sqlite3.connect(":memory:")
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
        inserted = features.freeze_market_feature_selection(
            connection,
            symbols,
            cycle_id="2026-08-12T23:15",
            collected_ts_utc="2026-08-12T15:15:02Z",
            max_symbols=3,
        )
        connection.execute(
            "UPDATE market_feature_selection_rows SET symbol='XRP-USDT-SWAP' "
            "WHERE selection_rank=2"
        )
        connection.commit()
        result = features.freeze_market_feature_selection(
            connection,
            symbols,
            cycle_id="2026-08-12T23:15",
            collected_ts_utc="2026-08-12T15:15:30Z",
            max_symbols=3,
        )
        connection.close()

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(
            result["stored_payload_sha256"], inserted["payload_sha256"])
        self.assertNotEqual(
            result["stored_payload_sha256"],
            result["stored_observed_payload_sha256"],
        )

    def test_batch_gate_requires_each_family_at_least_99_percent(self) -> None:
        self.assertTrue(features.market_feature_batch_passed(
            selected_count=100,
            microstructure_rows=99,
            trade_flow_rows=100,
        ))
        self.assertTrue(features.market_feature_batch_passed(
            selected_count=100,
            microstructure_rows=100,
            trade_flow_rows=99,
        ))
        self.assertFalse(features.market_feature_batch_passed(
            selected_count=100,
            microstructure_rows=98,
            trade_flow_rows=100,
        ))
        self.assertFalse(features.market_feature_batch_passed(
            selected_count=0,
            microstructure_rows=0,
            trade_flow_rows=0,
        ))

    def test_selection_shorter_than_configured_denominator_fails(self) -> None:
        connection = sqlite3.connect(":memory:")
        with self.assertRaisesRegex(ValueError, "exactly match"):
            features.freeze_market_feature_selection(
                connection,
                ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                cycle_id="2026-08-12T23:15",
                collected_ts_utc="2026-08-12T15:15:02Z",
                max_symbols=3,
            )
        header = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='market_feature_selection_runs'"
        ).fetchone()
        connection.close()
        self.assertIsNone(header)


if __name__ == "__main__":
    unittest.main()
