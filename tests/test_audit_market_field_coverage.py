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

import audit_market_field_coverage as audit  # noqa: E402
import collect_data  # noqa: E402


class MarketFieldCoverageAuditTests(unittest.TestCase):
    def _db(self, root: str) -> Path:
        path = Path(root) / "market.db"
        connection = sqlite3.connect(path)
        def instrument(symbol: str, list_time_ms: str) -> dict:
            return {
                "instId": symbol,
                "listTime": list_time_ms,
                "state": "live",
                "settleCcy": "USDT",
                "ctType": "linear",
                "instCategory": "1",
                "ctVal": "1",
                "lotSz": "0.1",
            }
        a = instrument("A-USDT-SWAP", "1785542400000")
        b = instrument("B-USDT-SWAP", "1786493700000")
        collect_data.freeze_official_instrument_snapshot(
            connection,
            [a],
            cycle_id="2026-08-12T08:00",
            collected_ts_utc="2026-08-12T00:00:01Z",
        )
        collect_data.freeze_official_instrument_snapshot(
            connection,
            [a, b],
            cycle_id="2026-08-12T08:15",
            collected_ts_utc="2026-08-12T00:15:01Z",
        )
        connection.execute("""
            CREATE TABLE tick_snapshots(
                ts TEXT,symbol TEXT,last REAL,bid REAL,ask REAL,vol24h REAL,
                fundingRate REAL,oi REAL,chg24h REAL,
                PRIMARY KEY(ts,symbol)
            )
        """)
        good = (10.0, 9.9, 10.1, 100.0, 0.0001, 200.0, 1.5)
        connection.execute(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            ("2026-08-12T00:00:02Z", "A-USDT-SWAP", *good),
        )
        connection.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("2026-08-12T00:15:02Z", "A-USDT-SWAP", *good),
                ("2026-08-12T00:15:02Z", "B-USDT-SWAP", *good),
            ],
        )
        connection.commit()
        connection.close()
        return path

    def test_listing_time_reconstructs_denominator_and_all_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = audit.audit_market_field_coverage(
                self._db(temporary),
                as_of=datetime(2026, 8, 12, 0, 21, tzinfo=timezone.utc),
                forward_start=audit._parse_cst("2026-08-12T08:00:00+08:00"),
                target_rate=0.99,
                minimum_slots=2,
            )

        self.assertEqual(payload["status"], "PASSED")
        self.assertEqual(payload["counts"]["expected_slots"], 2)
        self.assertEqual(payload["counts"]["expected_symbol_rows"], 3)
        self.assertEqual(payload["rates"]["all_fields_complete_rate"], 1.0)
        self.assertEqual(payload["slots"][0]["expected_symbols"], 1)
        self.assertEqual(payload["slots"][1]["expected_symbols"], 2)

    def test_missing_symbol_is_not_hidden_by_observed_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "DELETE FROM tick_snapshots WHERE symbol='B-USDT-SWAP'"
            )
            connection.commit()
            connection.close()
            payload = audit.audit_market_field_coverage(
                path,
                as_of=datetime(2026, 8, 12, 0, 21, tzinfo=timezone.utc),
                forward_start=audit._parse_cst("2026-08-12T08:00:00+08:00"),
                target_rate=0.99,
                minimum_slots=2,
            )

        self.assertEqual(payload["status"], "NOT_MET")
        self.assertEqual(payload["counts"]["expected_symbol_rows"], 3)
        self.assertEqual(payload["counts"]["all_fields_valid_symbol_rows"], 2)
        self.assertEqual(payload["slots"][1]["missing_symbols"], 1)

    def test_crossed_quote_fails_executable_and_complete_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE tick_snapshots SET bid=11,ask=10 "
                "WHERE symbol='B-USDT-SWAP'"
            )
            connection.commit()
            connection.close()
            payload = audit.audit_market_field_coverage(
                path,
                as_of=datetime(2026, 8, 12, 0, 21, tzinfo=timezone.utc),
                forward_start=audit._parse_cst("2026-08-12T08:00:00+08:00"),
                target_rate=0.99,
                minimum_slots=2,
            )

        self.assertEqual(payload["status"], "NOT_MET")
        self.assertEqual(
            payload["counts"]["field_valid_symbol_rows"]["executable_quote"],
            2,
        )
        self.assertEqual(payload["counts"]["all_fields_valid_symbol_rows"], 2)

    def test_tampered_official_snapshot_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._db(temporary)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE official_instrument_snapshot_rows SET lot_sz=0.2 "
                "WHERE cycle_id='2026-08-12T08:15' "
                "AND symbol='B-USDT-SWAP'"
            )
            connection.commit()
            connection.close()
            payload = audit.audit_market_field_coverage(
                path,
                as_of=datetime(2026, 8, 12, 0, 21, tzinfo=timezone.utc),
                forward_start=audit._parse_cst(
                    "2026-08-12T08:00:00+08:00"),
                target_rate=0.99,
                minimum_slots=2,
            )

        self.assertEqual(payload["status"], "NOT_MET")
        official = payload["slots"][1]["official_instrument_snapshot"]
        self.assertEqual(official["status"], "NOT_MET")
        self.assertIn("payload_sha256_mismatch", official["reasons"])
        self.assertEqual(
            payload["official_instrument_evidence"]["snapshot_slot_rate"],
            0.5,
        )

    def test_missing_same_slot_official_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "market.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE tick_snapshots("
                "ts TEXT,symbol TEXT,last REAL,bid REAL,ask REAL,vol24h REAL,"
                "fundingRate REAL,oi REAL,chg24h REAL)"
            )
            connection.commit()
            connection.close()

            payload = audit.audit_market_field_coverage(
                path,
                as_of=datetime(2026, 8, 12, 0, 21, tzinfo=timezone.utc),
                forward_start=audit._parse_cst(
                    "2026-08-12T08:00:00+08:00"),
                minimum_slots=2,
            )

        self.assertEqual(payload["status"], "NOT_MET")
        self.assertEqual(
            payload["official_instrument_evidence"]["snapshot_slot_rate"],
            0.0,
        )
        self.assertEqual(
            payload["slots"][0]["official_instrument_snapshot"]["reasons"],
            ["official_snapshot_tables_missing"],
        )

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
            payload = audit.audit_market_field_coverage(
                path,
                as_of=datetime(2026, 8, 12, 0, 21, tzinfo=timezone.utc),
                forward_start=audit._parse_cst(
                    "2026-08-12T08:00:00+08:00"),
                target_rate=0.99,
                minimum_slots=2,
            )

        official = payload["slots"][1]["official_instrument_snapshot"]
        self.assertIn("official_snapshot_source_invalid", official["reasons"])
        self.assertEqual(payload["status"], "NOT_MET")


if __name__ == "__main__":
    unittest.main()
