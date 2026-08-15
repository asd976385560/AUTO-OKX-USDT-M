from __future__ import annotations

import sqlite3
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_positioning_coverage as audit  # noqa: E402
import collect_data  # noqa: E402


def _database(root: Path, rows: list[tuple]) -> Path:
    path = root / "market.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT);
        CREATE TABLE market_positioning(
          ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,
          long_ratio REAL,short_ratio REAL,long_short_ratio REAL,source TEXT
        );
        """
    )
    tick_ts = "2026-08-11T19:00:02Z"
    connection.executemany(
        "INSERT INTO tick_snapshots VALUES(?,?)",
        [(tick_ts, symbol) for symbol in (
            "AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP")],
    )
    connection.executemany(
        "INSERT INTO market_positioning VALUES(?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return path


def _row(symbol: str, long_ratio: float = 0.6) -> tuple:
    short_ratio = 1.0 - long_ratio
    return (
        "2026-08-11T18:00:00Z", "2026-08-11T19:00:25Z",
        "2026-08-12T03:00", symbol, "1H", long_ratio, short_ratio,
        long_ratio / short_ratio, audit.DEFAULT_SOURCE,
    )


class PositioningCoverageAuditTests(unittest.TestCase):
    def test_storage_contract_requires_cycle_scoped_primary_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy_path = root / "legacy.db"
            target_path = root / "target.db"
            legacy = sqlite3.connect(legacy_path)
            legacy.execute(
                "CREATE TABLE market_positioning("
                "ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,source TEXT,"
                "PRIMARY KEY(ts,symbol,timeframe))"
            )
            legacy.close()
            target = sqlite3.connect(target_path)
            target.execute(
                "CREATE TABLE market_positioning("
                "ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,source TEXT,"
                "PRIMARY KEY(cycle_id,symbol,timeframe,source))"
            )
            target.close()
            legacy_result = audit.audit_positioning_storage_contract(
                legacy_path)
            target_result = audit.audit_positioning_storage_contract(
                target_path)
        self.assertEqual("NOT_MET", legacy_result["status"])
        self.assertFalse(
            legacy_result["cross_cycle_upstream_ts_reuse_supported"])
        self.assertEqual("PASSED", target_result["status"])
        self.assertTrue(
            target_result["cross_cycle_upstream_ts_reuse_supported"])

    def test_complete_valid_batch_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _database(Path(tmp), [
                _row("AAA-USDT-SWAP"),
                _row("BBB-USDT-SWAP"),
                _row("CCC-USDT-SWAP"),
            ])
            result = audit.audit_positioning_coverage(
                path, now=datetime(2026, 8, 11, 19, 15,
                                   tzinfo=timezone.utc))
        self.assertEqual("PASSED", result["status"])
        self.assertEqual(1.0, result["coverage_rate"])
        self.assertEqual([], result["invalid_rows"])

    def test_missing_symbol_is_in_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _database(Path(tmp), [
                _row("AAA-USDT-SWAP"), _row("BBB-USDT-SWAP")])
            result = audit.audit_positioning_coverage(
                path, now=datetime(2026, 8, 11, 19, 15,
                                   tzinfo=timezone.utc))
        self.assertEqual("NOT_MET", result["status"])
        self.assertEqual(["CCC-USDT-SWAP"], result["missing_symbols"])
        self.assertEqual(0.666667, result["coverage_rate"])

    def test_invalid_ratio_cannot_count_as_covered(self) -> None:
        bad = list(_row("CCC-USDT-SWAP"))
        bad[7] = 99.0
        with tempfile.TemporaryDirectory() as tmp:
            path = _database(Path(tmp), [
                _row("AAA-USDT-SWAP"), _row("BBB-USDT-SWAP"), tuple(bad)])
            result = audit.audit_positioning_coverage(
                path, now=datetime(2026, 8, 11, 19, 15,
                                   tzinfo=timezone.utc))
        self.assertEqual("NOT_MET", result["status"])
        self.assertEqual(0.666667, result["coverage_rate"])
        self.assertIn(
            "long_short_ratio_derivation_mismatch",
            result["invalid_rows"][0]["errors"],
        )

    def test_oldest_source_row_controls_maximum_age_and_freshness_gate(self):
        old = list(_row("CCC-USDT-SWAP"))
        old[0] = "2026-08-11T17:00:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            path = _database(Path(tmp), [
                _row("AAA-USDT-SWAP"), _row("BBB-USDT-SWAP"), tuple(old)])
            result = audit.audit_positioning_coverage(
                path,
                now=datetime(2026, 8, 11, 19, 0, tzinfo=timezone.utc),
                maximum_source_age_minutes=90,
            )
        self.assertEqual("NOT_MET", result["status"])
        self.assertEqual(120.0, result["maximum_source_age_minutes"])
        self.assertEqual(60.0, result["minimum_source_age_minutes"])
        stale = next(
            item for item in result["invalid_rows"]
            if item["symbol"] == "CCC-USDT-SWAP")
        self.assertIn("source_ts_stale", stale["errors"])

    def test_forward_hourly_slots_require_same_slot_official_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT);"
                "CREATE TABLE market_positioning("
                "ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,"
                "timeframe TEXT,long_ratio REAL,short_ratio REAL,"
                "long_short_ratio REAL,source TEXT);"
            )
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
                for symbol in (
                    "AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP")
            ]
            connection.executemany(
                "INSERT INTO tick_snapshots VALUES(?,?)",
                [("2026-08-11T18:00:02Z", item["instId"])
                 for item in instruments],
            )
            for cycle, slot_utc, source_utc, collected_utc in (
                ("2026-08-12T01:00", "2026-08-11T17:00:02Z",
                 "2026-08-11T16:00:00Z", "2026-08-11T17:01:00Z"),
                ("2026-08-12T02:00", "2026-08-11T18:00:02Z",
                 "2026-08-11T17:00:00Z", "2026-08-11T18:01:00Z"),
            ):
                collect_data.freeze_official_instrument_snapshot(
                    connection, instruments, cycle_id=cycle,
                    collected_ts_utc=slot_utc)
                connection.executemany(
                    "INSERT INTO market_positioning VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        (source_utc, collected_utc, cycle, item["instId"],
                         "1H", 0.6, 0.4, 1.5, audit.DEFAULT_SOURCE)
                        for item in instruments
                    ],
                )
            connection.commit()
            connection.close()
            result = audit.audit_positioning_forward_coverage(
                path,
                as_of=audit._parse_cst("2026-08-12T02:06:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-12T01:00:00+08:00"),
                minimum_slots=2,
            )
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE official_instrument_snapshot_runs "
                "SET payload_sha256='bad' "
                "WHERE cycle_id='2026-08-12T02:00'"
            )
            connection.commit()
            connection.close()
            tampered = audit.audit_positioning_forward_coverage(
                path,
                as_of=audit._parse_cst("2026-08-12T02:06:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-12T01:00:00+08:00"),
                minimum_slots=2,
            )
        self.assertEqual("PASSED", result["status"])
        self.assertEqual(2, result["passed_slots"])
        self.assertEqual(1.0, result["symbol_coverage_rate"])
        self.assertEqual("NOT_MET", tampered["status"])
        official = tampered["slots"][1]["official_instrument_snapshot"]
        self.assertIn("payload_sha256_mismatch", official["reasons"])

    def test_quarter_hour_availability_uses_00_30_batches_and_90m_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT);"
                "CREATE TABLE market_positioning("
                "ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,"
                "timeframe TEXT,long_ratio REAL,short_ratio REAL,"
                "long_short_ratio REAL,source TEXT);"
            )
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
                for symbol in (
                    "AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP")
            ]
            cycles = (
                ("2026-08-12T02:30", "2026-08-11T18:30:02Z"),
                ("2026-08-12T02:45", "2026-08-11T18:45:02Z"),
                ("2026-08-12T03:00", "2026-08-11T19:00:02Z"),
            )
            for cycle, snapshot_ts in cycles:
                connection.executemany(
                    "INSERT INTO tick_snapshots VALUES(?,?)",
                    [(snapshot_ts, item["instId"]) for item in instruments],
                )
                collect_data.freeze_official_instrument_snapshot(
                    connection,
                    instruments,
                    cycle_id=cycle,
                    collected_ts_utc=snapshot_ts,
                )
            for cycle, source_utc, collected_utc in (
                ("2026-08-12T02:30", "2026-08-11T18:00:00Z",
                 "2026-08-11T18:31:00Z"),
                ("2026-08-12T03:00", "2026-08-11T19:00:00Z",
                 "2026-08-11T19:01:00Z"),
            ):
                connection.executemany(
                    "INSERT INTO market_positioning VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        (source_utc, collected_utc, cycle, item["instId"],
                         "1H", 0.6, 0.4, 1.5, audit.DEFAULT_SOURCE)
                        for item in instruments
                    ],
                )
            connection.commit()
            connection.close()
            result = audit.audit_positioning_decision_availability(
                path,
                as_of=audit._parse_cst("2026-08-12T03:06:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-12T02:30:00+08:00"),
                minimum_slots=3,
            )
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE market_positioning SET ts='2026-08-11T16:00:00Z' "
                "WHERE cycle_id='2026-08-12T02:30' "
                "AND symbol='CCC-USDT-SWAP'"
            )
            connection.commit()
            connection.close()
            stale = audit.audit_positioning_decision_availability(
                path,
                as_of=audit._parse_cst("2026-08-12T03:06:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-12T02:30:00+08:00"),
                minimum_slots=3,
            )
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            connection.execute(
                "UPDATE official_instrument_snapshot_rows "
                "SET list_time_utc='2026-08-11T18:50:00Z' "
                "WHERE cycle_id='2026-08-12T02:30' "
                "AND symbol='BBB-USDT-SWAP'"
            )
            official_rows = connection.execute(
                "SELECT symbol,list_time_utc,state,settle_ccy,ct_type,"
                "inst_category,ct_val,lot_sz "
                "FROM official_instrument_snapshot_rows "
                "WHERE cycle_id='2026-08-12T02:30' ORDER BY symbol"
            ).fetchall()
            official_hash = audit._official_hash([
                audit._canonical_official_row(row) for row in official_rows
            ])
            connection.execute(
                "UPDATE official_instrument_snapshot_runs "
                "SET payload_sha256=? WHERE cycle_id='2026-08-12T02:30'",
                (official_hash,),
            )
            connection.commit()
            connection.close()
            future_listing = audit.audit_positioning_decision_availability(
                path,
                as_of=audit._parse_cst("2026-08-12T03:06:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-12T02:30:00+08:00"),
                minimum_slots=3,
            )
        self.assertEqual("PASSED", result["status"])
        self.assertEqual(3, result["passed_slots"])
        self.assertEqual(
            "2026-08-12T02:30", result["slots"][1][
                "positioning_collection_cycle_id"])
        self.assertEqual("NOT_MET", stale["status"])
        bad = next(
            row for row in stale["slots"][0]["invalid_rows"]
            if row["symbol"] == "CCC-USDT-SWAP")
        self.assertIn("source_ts_stale_for_decision", bad["errors"])
        official = future_listing["slots"][0]["official_instrument_snapshot"]
        bad_metadata = next(
            row for row in official["invalid_metadata_examples"]
            if row["symbol"] == "BBB-USDT-SWAP")
        self.assertIn("list_time_after_slot", bad_metadata["reasons"])
        self.assertEqual("NOT_MET", future_listing["slots"][0]["status"])

    def test_bounded_retry_receipt_matches_database_and_official_universe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "market.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE market_positioning("
                "ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,"
                "timeframe TEXT,long_ratio REAL,short_ratio REAL,"
                "long_short_ratio REAL,source TEXT);"
                "CREATE TABLE official_instrument_snapshot_rows("
                "cycle_id TEXT,symbol TEXT);"
            )
            symbols = ["AAA-USDT-SWAP", "BBB-USDT-SWAP"]
            connection.executemany(
                "INSERT INTO official_instrument_snapshot_rows VALUES(?,?)",
                [("2026-08-13T13:00", symbol) for symbol in symbols],
            )
            connection.executemany(
                "INSERT INTO market_positioning VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("2026-08-13T05:00:00Z", "2026-08-13T05:01:00Z",
                     "2026-08-13T13:00", symbol, "1H", 0.6, 0.4, 1.5,
                     audit.DEFAULT_SOURCE)
                    for symbol in symbols
                ],
            )
            connection.commit()
            connection.close()
            encoded = json.dumps(
                symbols, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
            receipt = {
                "schema_version": 1,
                "artifact_type": "current_natural_positioning_collection_receipt",
                "status": "PASSED",
                "ok": True,
                "degraded": False,
                "cycle": "2026-08-13T13:00",
                "selected": symbols,
                "selected_count": 2,
                "selected_symbols_sha256": hashlib.sha256(encoded).hexdigest(),
                "wrote": {"positioning": 2},
                "positioning_coverage_rate": 1.0,
                "natural_current_cycle_guard": True,
                "historical_backfill_allowed": False,
                "retry": {
                    "initial_requested_symbols": 2,
                    "initial_valid_symbols": 1,
                    "initial_invalid_symbols": 1,
                    "initial_invalid_symbol_values": ["BBB-USDT-SWAP"],
                    "retry_requested_symbols": 1,
                    "retry_requested_symbol_values": ["BBB-USDT-SWAP"],
                    "retry_recovered_symbols": 1,
                    "retry_recovered_symbol_values": ["BBB-USDT-SWAP"],
                    "final_failed_symbols": 0,
                    "final_failed_symbol_values": [],
                    "retry_attempts_per_symbol": 1,
                    "unbounded_retry": False,
                    "historical_retry": False,
                    "shared_available_at_utc": "2026-08-13T05:01:00Z",
                },
                "safety": {
                    "natural_current_cycle_only": True,
                    "historical_backfill_allowed": False,
                    "production_model_mutation": False,
                    "production_threshold_mutation": False,
                    "orders_placed": 0,
                },
            }
            receipt_root = root / "receipts"
            receipt_path = audit._receipt_path(
                receipt_root, "2026-08-13T13:00")
            receipt_path.parent.mkdir(parents=True)
            receipt_path.write_text(
                json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
            passed = audit.audit_positioning_collection_receipts(
                path,
                receipt_root,
                as_of=audit._parse_cst("2026-08-13T13:06:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-13T13:00:00+08:00"),
                minimum_slots=1,
            )
            receipt["retry"]["final_failed_symbol_values"] = [
                "BBB-USDT-SWAP"]
            receipt_path.write_text(
                json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
            tampered = audit.audit_positioning_collection_receipts(
                path,
                receipt_root,
                as_of=audit._parse_cst("2026-08-13T13:06:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-13T13:00:00+08:00"),
                minimum_slots=1,
            )
            # 即使整体覆盖仍可高于宽松目标，任一来源过时仍须失败。
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE market_positioning SET ts='2026-08-13T02:00:00Z' "
                "WHERE cycle_id='2026-08-13T13:00' "
                "AND symbol='BBB-USDT-SWAP'"
            )
            connection.commit()
            connection.close()
            receipt["retry"]["final_failed_symbol_values"] = []
            receipt["status"] = "NOT_MET"
            receipt["ok"] = False
            receipt["degraded"] = True
            receipt_path.write_text(
                json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
            stale = audit.audit_positioning_collection_receipts(
                path,
                receipt_root,
                as_of=audit._parse_cst("2026-08-13T13:06:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-13T13:00:00+08:00"),
                minimum_slots=1,
                target_rate=0.5,
            )
        self.assertEqual("PASSED", passed["status"])
        self.assertEqual(1, passed["passed_slots"])
        self.assertEqual(1.0, passed["symbol_coverage_rate"])
        self.assertEqual("NOT_MET", tampered["status"])
        self.assertIn(
            "final_failed_set_mismatch",
            tampered["slots"][0]["reasons"],
        )
        self.assertEqual("NOT_MET", stale["status"])
        self.assertIn(
            "database_invalid_rows", stale["slots"][0]["reasons"])

    def test_observed_receipt_failure_is_not_hidden_by_minimum_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "market.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE market_positioning("
                "ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,"
                "timeframe TEXT,long_ratio REAL,short_ratio REAL,"
                "long_short_ratio REAL,source TEXT);"
                "CREATE TABLE official_instrument_snapshot_rows("
                "cycle_id TEXT,symbol TEXT);"
            )
            connection.execute(
                "INSERT INTO official_instrument_snapshot_rows VALUES(?,?)",
                ("2026-08-13T13:30", "AAA-USDT-SWAP"),
            )
            connection.commit()
            connection.close()
            result = audit.audit_positioning_collection_receipts(
                path,
                root / "receipts",
                as_of=audit._parse_cst("2026-08-13T13:36:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-13T13:30:00+08:00"),
                minimum_slots=48,
            )
            before_start = audit.audit_positioning_collection_receipts(
                path,
                root / "receipts",
                as_of=audit._parse_cst("2026-08-13T13:34:00+08:00"),
                forward_start=audit._parse_cst(
                    "2026-08-13T13:30:00+08:00"),
                minimum_slots=48,
            )
        self.assertEqual("NOT_MET", result["status"])
        self.assertEqual(1, result["expected_slots"])
        self.assertEqual("INSUFFICIENT_EVIDENCE", before_start["status"])
        self.assertEqual(0, before_start["expected_slots"])


if __name__ == "__main__":
    unittest.main()
