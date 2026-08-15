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

import audit_market_feature_coverage as audit  # noqa: E402
import collect_data  # noqa: E402
import collect_market_features as features  # noqa: E402


MICRO_DDL = """
CREATE TABLE market_microstructure(
 ts TEXT NOT NULL,cycle_id TEXT NOT NULL,symbol TEXT NOT NULL,
 depth_levels INTEGER NOT NULL,best_bid REAL,best_ask REAL,mid_px REAL,
 spread_bps REAL,bid_depth_10bp_usd REAL,ask_depth_10bp_usd REAL,
 bid_depth_25bp_usd REAL,ask_depth_25bp_usd REAL,bid_depth_50bp_usd REAL,
 ask_depth_50bp_usd REAL,imbalance_10bp REAL,imbalance_25bp REAL,
 imbalance_50bp REAL,buy_slippage_100usd_bps REAL,
 sell_slippage_100usd_bps REAL,buy_slippage_500usd_bps REAL,
 sell_slippage_500usd_bps REAL,buy_slippage_1000usd_bps REAL,
 sell_slippage_1000usd_bps REAL,book_ts TEXT,seq_id INTEGER,
 raw_bids TEXT,raw_asks TEXT,source TEXT NOT NULL,PRIMARY KEY(ts,symbol)
)
"""
FLOW_DDL = """
CREATE TABLE market_trade_flow(
 ts TEXT NOT NULL,cycle_id TEXT NOT NULL,symbol TEXT NOT NULL,
 sample_count INTEGER NOT NULL,sample_start TEXT,sample_end TEXT,
 sample_span_ms INTEGER,buy_qty_contracts REAL,sell_qty_contracts REAL,
 buy_notional_usd REAL,sell_notional_usd REAL,taker_buy_ratio REAL,
 cvd_notional_usd REAL,largest_trade_usd REAL,raw_sample TEXT,
 source TEXT NOT NULL,PRIMARY KEY(ts,symbol)
)
"""


def _milliseconds(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return str(int(parsed.timestamp() * 1000))


class MarketFeatureCoverageAuditTests(unittest.TestCase):
    def _db(self, root: str) -> Path:
        path = Path(root) / "market.db"
        connection = sqlite3.connect(path)
        connection.execute(MICRO_DDL)
        connection.execute(FLOW_DDL)
        instruments = [
            {
                "instId": symbol,
                "listTime": "1782864000000",
                "state": "live",
                "settleCcy": "USDT",
                "ctType": "linear",
                "instCategory": "1",
                "ctVal": "1",
                "lotSz": "0.1",
            }
            for symbol in ("A-USDT-SWAP", "B-USDT-SWAP")
        ]
        for cycle, slot_ts in (
            ("2026-08-12T08:00", "2026-08-12T00:00:02Z"),
            ("2026-08-12T08:15", "2026-08-12T00:15:02Z"),
        ):
            collect_data.freeze_official_instrument_snapshot(
                connection,
                instruments,
                cycle_id=cycle,
                collected_ts_utc=slot_ts,
            )
            features.freeze_market_feature_selection(
                connection,
                ["A-USDT-SWAP", "B-USDT-SWAP"],
                cycle_id=cycle,
                collected_ts_utc=slot_ts,
                max_symbols=2,
            )
            book_ts = slot_ts.replace("02Z", "03Z")
            bids = [[100.0 - index * 0.01, 10.0] for index in range(50)]
            asks = [[100.1 + index * 0.01, 10.0] for index in range(50)]
            book = {
                "bids": bids,
                "asks": asks,
                "ts": _milliseconds(book_ts),
                "seqId": 12345,
            }
            trade_start = slot_ts.replace("02Z", "01Z")
            trade_end = slot_ts.replace("02Z", "04Z")
            trades = [
                {
                    "px": str(100 + index / 1000),
                    "sz": "1",
                    "side": "buy" if index % 2 == 0 else "sell",
                    "ts": _milliseconds(
                        trade_start if index < 25 else trade_end),
                }
                for index in range(50)
            ]
            for symbol in ("A-USDT-SWAP", "B-USDT-SWAP"):
                connection.execute(
                    "INSERT INTO market_microstructure VALUES(" +
                    ",".join("?" for _ in range(28)) + ")",
                    features.book_features(
                        book, 1.0, cycle, slot_ts, symbol),
                )
                connection.execute(
                    "INSERT INTO market_trade_flow VALUES(" +
                    ",".join("?" for _ in range(16)) + ")",
                    features.flow_features(
                        trades, 1.0, cycle, slot_ts, symbol),
                )
        connection.commit()
        connection.close()
        return path

    def _audit(self, path: Path) -> dict:
        return audit.audit_market_feature_coverage(
            path,
            as_of=datetime(2026, 8, 12, 0, 21, tzinfo=timezone.utc),
            forward_start=audit._parse_cst(
                "2026-08-12T08:00:00+08:00"),
            target_rate=0.99,
            minimum_slots=2,
            expected_symbols_per_slot=2,
        )

    def test_two_complete_slots_pass_with_fixed_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = self._audit(self._db(temporary))
        self.assertEqual(payload["status"], "PASSED")
        self.assertEqual(payload["counts"]["expected_symbol_rows"], 4)
        self.assertEqual(payload["rates"]["microstructure_coverage_rate"], 1.0)
        self.assertEqual(payload["rates"]["trade_flow_coverage_rate"], 1.0)
        self.assertEqual(payload["rates"]["combined_coverage_rate"], 1.0)

    def test_missing_flow_row_remains_in_fixed_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "DELETE FROM market_trade_flow "
                "WHERE cycle_id='2026-08-12T08:15' "
                "AND symbol='B-USDT-SWAP'"
            )
            connection.commit()
            connection.close()
            payload = self._audit(path)
        self.assertEqual(payload["status"], "NOT_MET")
        self.assertEqual(payload["counts"]["expected_symbol_rows"], 4)
        self.assertEqual(payload["counts"]["trade_flow_valid_symbol_rows"], 3)
        self.assertEqual(payload["rates"]["trade_flow_coverage_rate"], 0.75)

    def test_tampered_selection_hash_fails_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE market_feature_selection_rows "
                "SET symbol='X-USDT-SWAP' "
                "WHERE cycle_id='2026-08-12T08:15' AND selection_rank=2"
            )
            connection.commit()
            connection.close()
            payload = self._audit(path)
        selection = payload["slots"][1]["selection_snapshot"]
        self.assertEqual(selection["status"], "NOT_MET")
        self.assertIn("payload_sha256_mismatch", selection["reasons"])
        self.assertEqual(payload["rates"]["selection_snapshot_slot_rate"], 0.5)

    def test_tampered_official_snapshot_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE official_instrument_snapshot_runs "
                "SET payload_sha256='bad' "
                "WHERE cycle_id='2026-08-12T08:15'"
            )
            connection.commit()
            connection.close()
            payload = self._audit(path)
        official = payload["slots"][1]["official_instrument_snapshot"]
        self.assertEqual(official["status"], "NOT_MET")
        self.assertIn("payload_sha256_mismatch", official["reasons"])
        self.assertEqual(payload["rates"]["official_snapshot_slot_rate"], 0.5)
        self.assertEqual(
            payload["counts"]["microstructure_valid_symbol_rows"], 2)

    def test_tampered_official_snapshot_count_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE official_instrument_snapshot_runs SET symbol_count=3 "
                "WHERE cycle_id='2026-08-12T08:15'"
            )
            connection.commit()
            connection.close()
            payload = self._audit(path)
        official = payload["slots"][1]["official_instrument_snapshot"]
        self.assertIn("header_row_count_mismatch", official["reasons"])
        self.assertEqual(payload["slots"][1]["status"], "NOT_MET")

    def test_invalid_official_ct_val_is_reported_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE official_instrument_snapshot_rows SET ct_val=0 "
                "WHERE cycle_id='2026-08-12T08:15' "
                "AND symbol='B-USDT-SWAP'"
            )
            connection.commit()
            connection.close()
            payload = self._audit(path)
        official = payload["slots"][1]["official_instrument_snapshot"]
        self.assertIn("payload_sha256_mismatch", official["reasons"])
        self.assertIn(
            "ct_val_invalid",
            official["invalid_metadata_examples"][0]["reasons"],
        )
        self.assertEqual(payload["slots"][1]["status"], "NOT_MET")

    def test_invalid_official_snapshot_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE official_instrument_snapshot_runs SET source='other' "
                "WHERE cycle_id='2026-08-12T08:15'"
            )
            connection.commit()
            connection.close()
            payload = self._audit(path)
        official = payload["slots"][1]["official_instrument_snapshot"]
        self.assertIn("official_snapshot_source_invalid", official["reasons"])
        self.assertEqual(payload["slots"][1]["status"], "NOT_MET")

    def test_inconsistent_derived_spread_is_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE market_microstructure SET spread_bps=999 "
                "WHERE cycle_id='2026-08-12T08:15' "
                "AND symbol='B-USDT-SWAP'"
            )
            connection.commit()
            connection.close()
            payload = self._audit(path)
        self.assertEqual(payload["status"], "NOT_MET")
        self.assertEqual(
            payload["counts"]["microstructure_valid_symbol_rows"], 3)
        example = payload["slots"][1]["invalid_examples"][0]
        self.assertIn("spread_bps_mismatch", example["microstructure_errors"])

    def test_subsecond_span_rounding_is_allowed_but_larger_drift_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            original = connection.execute(
                "SELECT sample_span_ms FROM market_trade_flow "
                "WHERE cycle_id='2026-08-12T08:15' "
                "AND symbol='B-USDT-SWAP'"
            ).fetchone()[0]
            connection.execute(
                "UPDATE market_trade_flow SET sample_span_ms=? "
                "WHERE cycle_id='2026-08-12T08:15' "
                "AND symbol='B-USDT-SWAP'",
                (original + 999,),
            )
            connection.commit()
            payload = self._audit(path)
            self.assertEqual(payload["status"], "PASSED")
            connection.execute(
                "UPDATE market_trade_flow SET sample_span_ms=? "
                "WHERE cycle_id='2026-08-12T08:15' "
                "AND symbol='B-USDT-SWAP'",
                (original + 1001,),
            )
            connection.commit()
            connection.close()
            payload = self._audit(path)
        self.assertEqual(payload["status"], "NOT_MET")
        example = payload["slots"][1]["invalid_examples"][0]
        self.assertIn("sample_span_mismatch", example["trade_flow_errors"])


if __name__ == "__main__":
    unittest.main()
