from __future__ import annotations

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

import apply_contract_statistics_schema as schema  # noqa: E402
import audit_contract_statistics_coverage as audit  # noqa: E402
import collect_market_features as collector  # noqa: E402


class ContractStatisticsCoverageAuditTests(unittest.TestCase):
    def test_cli_defaults_to_preregistered_forward_window(self) -> None:
        payload = {
            "overall_status": "PENDING_FORWARD_EVIDENCE",
            "status": "PASSED",
            "latest_cycle_id": "2026-08-12T17:15",
            "valid_symbols": 431,
            "universe_symbols": 431,
            "coverage_rate": 1.0,
            "forward_after_remediation": {"expected_slots": 6},
        }
        with (
            mock.patch.object(
                audit, "audit_contract_statistics", return_value=payload,
            ) as audited,
            mock.patch.object(audit, "_atomic_json"),
        ):
            status = audit.main(["--json-out", "unused-quality.json"])

        self.assertEqual(0, status)
        self.assertEqual(
            audit._parse_cst(audit.DEFAULT_FORWARD_START),
            audited.call_args.kwargs["forward_start"],
        )

    def test_separate_read_only_universe_database_audits_isolated_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            universe_db = root / "universe.db"
            statistics_db = root / "statistics.db"
            con = sqlite3.connect(universe_db)
            con.execute("CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT)")
            con.executemany(
                "INSERT INTO tick_snapshots VALUES(?,?)",
                [
                    ("2026-08-12T00:31:00Z", "BTC-USDT-SWAP"),
                    ("2026-08-12T00:31:00Z", "ETH-USDT-SWAP"),
                ],
            )
            con.commit()
            con.close()
            con = sqlite3.connect(statistics_db)
            con.execute(schema.DDL)
            rows = [
                (
                    "2026-08-12T00:15:00Z", "2026-08-12T00:31:00Z",
                    "2026-08-12T08:30", symbol, "15m",
                    100.0, 10.0, 1000.0, 40.0, 60.0, 0.6, "{}",
                    audit.SOURCE,
                )
                for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
            ]
            con.executemany(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
            )
            con.commit()
            con.close()

            result = audit.audit_contract_statistics(
                statistics_db, universe_db_path=universe_db)

            self.assertEqual(result["availability_status"], "PASSED")
            self.assertEqual(result["analysis_ready_status"], "PASSED")
            self.assertEqual(result["status"], "PASSED")
            self.assertEqual(result["coverage_rate"], 1.0)
            self.assertEqual(result["statistics_db"], str(statistics_db))
            self.assertEqual(result["universe_db"], str(universe_db))

    def test_latest_exact_batch_passes_and_stale_row_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "market.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT)")
            con.executemany(
                "INSERT INTO tick_snapshots VALUES(?,?)",
                [
                    ("2026-08-11T20:15:00Z", "BTC-USDT-SWAP"),
                    ("2026-08-11T20:15:00Z", "ETH-USDT-SWAP"),
                ],
            )
            con.execute(schema.DDL)
            con.execute(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "2026-08-11T19:45:00Z", "2026-08-11T20:01:00Z",
                    "2026-08-12T04:00", "BTC-USDT-SWAP", "15m",
                    100.0, 10.0, 1000.0, 40.0, 60.0, 0.6, "{}",
                    audit.SOURCE,
                ),
            )
            rows = [
                (
                    "2026-08-11T20:00:00Z", "2026-08-11T20:16:00Z",
                    "2026-08-12T04:15", symbol, "15m",
                    100.0, 10.0, 1000.0, 40.0, 60.0, 0.6, "{}",
                    audit.SOURCE,
                )
                for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
            ]
            con.executemany(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            con.commit()
            con.close()

            passed = audit.audit_contract_statistics(db_path)
            self.assertEqual(passed["status"], "PASSED")
            self.assertEqual(passed["coverage_rate"], 1.0)
            self.assertEqual(
                ["2026-08-12T04:15", "2026-08-12T04:00"],
                [row["cycle_id"] for row in passed["recent_batches"]],
            )
            self.assertEqual(
                "latest_batch_full_validation",
                passed["recent_batches"][0]["evidence_class"],
            )
            self.assertEqual(
                "historical_observation_only",
                passed["recent_batches"][1]["evidence_class"],
            )
            self.assertEqual(
                0.5, passed["recent_batches"][1]["observed_coverage_rate"])

            con = sqlite3.connect(db_path)
            con.execute(
                "UPDATE market_contract_statistics SET ts=? WHERE symbol=?",
                ("2026-08-11T18:00:00Z", "ETH-USDT-SWAP"),
            )
            con.commit()
            con.close()
            failed = audit.audit_contract_statistics(db_path)
            self.assertEqual(failed["status"], "NOT_MET")
            self.assertEqual(failed["coverage_rate"], 0.5)
            self.assertEqual(
                failed["invalid_symbols"]["ETH-USDT-SWAP"],
                ["stale_source_time"],
            )

    def test_direct_and_carried_coverage_are_separate_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "market.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT)")
            con.executemany(
                "INSERT INTO tick_snapshots VALUES(?,?)",
                [
                    ("2026-08-12T00:31:00Z", "BTC-USDT-SWAP"),
                    ("2026-08-12T00:31:00Z", "ETH-USDT-SWAP"),
                ],
            )
            con.execute(schema.DDL)
            previous = (
                "2026-08-12T00:00:00Z", "2026-08-12T00:16:00Z",
                "2026-08-12T08:15", "ETH-USDT-SWAP", "15m",
                100.0, 10.0, 1000.0, 40.0, 60.0, 0.6, "{}",
                audit.SOURCE,
            )
            con.execute(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", previous,
            )
            direct = (
                "2026-08-12T00:15:00Z", "2026-08-12T00:31:00Z",
                "2026-08-12T08:30", "BTC-USDT-SWAP", "15m",
                110.0, 11.0, 1100.0, 45.0, 55.0, 0.55, "{}",
                audit.SOURCE,
            )
            completed, quality, errors = (
                collector.complete_contract_statistics_with_previous_batch(
                    con, [direct],
                    ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                    "2026-08-12T08:30",
                    available_at="2026-08-12T00:31:00Z",
                )
            )
            self.assertEqual(errors, [])
            self.assertEqual(quality["direct_coverage_rate"], 0.5)
            self.assertEqual(quality["carry_forward_rate"], 0.5)
            con.executemany(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", completed,
            )
            con.commit()
            con.close()

            result = audit.audit_contract_statistics(db_path)
            self.assertEqual(result["availability_status"], "PASSED")
            self.assertEqual(result["analysis_ready_status"], "NOT_MET")
            self.assertEqual(result["status"], "NOT_MET")
            self.assertEqual(result["coverage_rate"], 1.0)
            self.assertEqual(result["direct_coverage_rate"], 0.5)
            self.assertEqual(result["carry_forward_rate"], 0.5)
            self.assertEqual(
                result["method_counts"], {
                    "rubik_common_bucket": 1,
                    "official_previous_batch_carry_forward": 1,
                },
            )
            self.assertEqual(result["valid_method_counts"], result["method_counts"])

            con = sqlite3.connect(db_path)
            con.execute(
                "UPDATE market_contract_statistics SET oi_usd=9999 "
                "WHERE cycle_id='2026-08-12T08:30' "
                "AND symbol='ETH-USDT-SWAP'"
            )
            con.commit()
            con.close()
            tampered = audit.audit_contract_statistics(db_path)
            self.assertEqual(tampered["status"], "NOT_MET")
            self.assertIn(
                "carry_value_digest_mismatch",
                tampered["invalid_symbols"]["ETH-USDT-SWAP"],
            )
            self.assertIn(
                "carry_values_changed",
                tampered["invalid_symbols"]["ETH-USDT-SWAP"],
            )

    def test_carried_row_cannot_reference_another_carried_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "market.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT)")
            con.execute(
                "INSERT INTO tick_snapshots VALUES(?,?)",
                ("2026-08-12T01:01:00Z", "BTC-USDT-SWAP"),
            )
            con.execute(schema.DDL)
            direct = (
                "2026-08-12T00:00:00Z", "2026-08-12T00:16:00Z",
                "2026-08-12T08:15", "BTC-USDT-SWAP", "15m",
                100.0, 10.0, 1000.0, 40.0, 60.0, 0.6, "{}",
                audit.SOURCE,
            )
            con.execute(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", direct,
            )
            first, _, _ = (
                collector.complete_contract_statistics_with_previous_batch(
                    con, [], ["BTC-USDT-SWAP"], "2026-08-12T08:30",
                    available_at="2026-08-12T00:31:00Z",
                )
            )
            con.execute(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", first[0],
            )
            prior_raw = first[0][11]
            value_contract = {
                "ts": first[0][0], "symbol": first[0][3],
                "oi_contracts": first[0][5], "oi_ccy": first[0][6],
                "oi_usd": first[0][7], "taker_sell_usd": first[0][8],
                "taker_buy_usd": first[0][9], "taker_buy_ratio": first[0][10],
            }
            second_raw = {
                "method": collector.CONTRACT_STATS_CARRY_METHOD,
                "semantics": "availability continuity only; excluded from model features",
                "carried_from_cycle_id": first[0][2],
                "carried_from_collected_ts": first[0][1],
                "origin_cycle_id": direct[2],
                "origin_collected_ts": direct[1],
                "origin_method": "rubik_common_bucket",
                "carry_count": 2,
                "source_age_seconds": 3660.0,
                "value_contract_sha256": __import__("hashlib").sha256(
                    json.dumps(value_contract, sort_keys=True, separators=(",", ":"))
                    .encode("utf-8")
                ).hexdigest(),
                "prior_raw_sha256": __import__("hashlib").sha256(
                    prior_raw.encode("utf-8")
                ).hexdigest(),
            }
            second = (
                first[0][0], "2026-08-12T01:01:00Z", "2026-08-12T09:00",
                *first[0][3:11], json.dumps(second_raw), first[0][12],
            )
            con.execute(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", second,
            )
            con.commit()
            con.close()

            result = audit.audit_contract_statistics(db_path)

            self.assertEqual(result["status"], "NOT_MET")
            self.assertIn(
                "carry_prior_not_direct",
                result["invalid_symbols"]["BTC-USDT-SWAP"],
            )
            self.assertIn(
                "carry_origin_not_direct",
                result["invalid_symbols"]["BTC-USDT-SWAP"],
            )

    def test_forward_window_counts_missing_slots_and_cannot_pass_early(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "market.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT)")
            con.executemany(
                "INSERT INTO tick_snapshots VALUES(?,?)",
                [
                    ("2026-08-12T00:15:02Z", "BTC-USDT-SWAP"),
                    ("2026-08-12T00:30:02Z", "BTC-USDT-SWAP"),
                    ("2026-08-12T00:45:02Z", "BTC-USDT-SWAP"),
                ],
            )
            con.execute(schema.DDL)
            con.executemany(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "2026-08-12T00:00:00Z", collected, cycle,
                        "BTC-USDT-SWAP", "15m", 100.0, 10.0, 1000.0,
                        40.0, 60.0, 0.6, "{}", audit.SOURCE,
                    )
                    for cycle, collected in (
                        ("2026-08-12T08:15", "2026-08-12T00:16:00Z"),
                        ("2026-08-12T08:45", "2026-08-12T00:46:00Z"),
                    )
                ],
            )
            con.commit()
            con.close()

            result = audit.audit_contract_statistics(
                db_path,
                forward_start=audit._parse_cst("2026-08-12T08:15:00+08:00"),
                as_of=audit._parse_cst("2026-08-12T08:52:00+08:00"),
                forward_minimum_slots=4,
                grace_minutes=5,
            )

            forward = result["forward_after_remediation"]
            self.assertEqual(3, forward["expected_slots"])
            self.assertEqual(2, forward["observed_slots"])
            self.assertEqual(1, forward["missing_slots"])
            self.assertEqual(2 / 3, forward["slot_pass_rate"])
            self.assertEqual(2 / 3, forward["analysis_ready_slot_pass_rate"])
            self.assertEqual("INSUFFICIENT_EVIDENCE", forward["status"])
            self.assertEqual("PENDING_FORWARD_EVIDENCE", result["overall_status"])
            missing = next(
                row for row in forward["slots"]
                if row["cycle_id"] == "2026-08-12T08:30"
            )
            self.assertEqual("NOT_MET", missing["status"])
            self.assertEqual(0, missing["valid_symbols"])

    def test_forward_gate_rejects_carry_even_when_availability_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "market.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT)")
            con.executemany(
                "INSERT INTO tick_snapshots VALUES(?,?)",
                [
                    (ts, symbol)
                    for ts in (
                        "2026-08-12T00:30:02Z",
                        "2026-08-12T00:45:02Z",
                    )
                    for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
                ],
            )
            con.execute(schema.DDL)
            prior = (
                "2026-08-12T00:00:00Z", "2026-08-12T00:16:00Z",
                "2026-08-12T08:15", "ETH-USDT-SWAP", "15m",
                100.0, 10.0, 1000.0, 40.0, 60.0, 0.6, "{}",
                audit.SOURCE,
            )
            con.execute(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", prior,
            )
            first_direct = (
                "2026-08-12T00:15:00Z", "2026-08-12T00:31:00Z",
                "2026-08-12T08:30", "BTC-USDT-SWAP", "15m",
                110.0, 11.0, 1100.0, 45.0, 55.0, 0.55, "{}",
                audit.SOURCE,
            )
            first_rows, _, errors = (
                collector.complete_contract_statistics_with_previous_batch(
                    con, [first_direct],
                    ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                    "2026-08-12T08:30",
                    available_at="2026-08-12T00:31:00Z",
                )
            )
            self.assertEqual([], errors)
            con.executemany(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", first_rows,
            )
            second_rows = [
                (
                    "2026-08-12T00:30:00Z", "2026-08-12T00:46:00Z",
                    "2026-08-12T08:45", symbol, "15m",
                    120.0, 12.0, 1200.0, 50.0, 50.0, 0.5, "{}",
                    audit.SOURCE,
                )
                for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
            ]
            con.executemany(
                "INSERT INTO market_contract_statistics VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)", second_rows,
            )
            con.commit()
            con.close()

            result = audit.audit_contract_statistics(
                db_path,
                forward_start=audit._parse_cst(
                    "2026-08-12T08:30:00+08:00"),
                as_of=audit._parse_cst("2026-08-12T08:52:00+08:00"),
                forward_minimum_slots=2,
                grace_minutes=5,
            )

            forward = result["forward_after_remediation"]
            self.assertEqual("PASSED", result["analysis_ready_status"])
            self.assertEqual("PASSED", forward["status"])
            self.assertEqual("NOT_MET", forward["analysis_ready_status"])
            self.assertEqual(1.0, forward["availability_coverage_rate"])
            self.assertEqual(0.75, forward["direct_coverage_rate"])
            self.assertEqual("NOT_MET", result["overall_status"])


if __name__ == "__main__":
    unittest.main()
