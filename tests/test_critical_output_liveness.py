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

import audit_critical_output_liveness as audit  # noqa: E402


class CriticalOutputLivenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_root = self.root / "db"
        self.status_dir = self.root / "stage-status"
        self.db_root.mkdir()
        self.status_dir.mkdir()
        self._create_databases()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_databases(self) -> None:
        schemas = {
            "ledger.db": (
                "CREATE TABLE collection_runs "
                "(cycle_id TEXT, source TEXT, status TEXT, ts TEXT)"),
            "analysis.db": (
                "CREATE TABLE analysis_runs "
                "(cycle_id TEXT PRIMARY KEY, ts TEXT, status TEXT)"),
            "live_trades.db": (
                "CREATE TABLE trade_cycles "
                "(cycle_id TEXT PRIMARY KEY, ts TEXT, mode TEXT)"),
            "account.db": (
                "CREATE TABLE account_snapshots "
                "(ts TEXT, profile TEXT, totalEq REAL)"),
        }
        for name, statement in schemas.items():
            connection = sqlite3.connect(self.db_root / name)
            try:
                connection.execute(statement)
                connection.commit()
            finally:
                connection.close()

    def _insert_cycles(self) -> None:
        quarter = ["05:00", "05:15", "05:30", "05:45", "06:00"]
        connection = sqlite3.connect(self.db_root / "ledger.db")
        try:
            connection.executemany(
                "INSERT INTO collection_runs VALUES (?,?,?,?)",
                [(f"2026-08-21T{hhmm}", "fast", "ok",
                  f"2026-08-21 {hhmm}:30") for hhmm in quarter])
            connection.execute(
                "INSERT INTO collection_runs VALUES (?,?,?,?)",
                ("2026-08-21T05:00", "slow", "ok",
                 "2026-08-21 05:04:00"))
            connection.commit()
        finally:
            connection.close()
        connection = sqlite3.connect(self.db_root / "analysis.db")
        try:
            connection.executemany(
                "INSERT INTO analysis_runs VALUES (?,?,?)",
                [(f"2026-08-21T{hhmm}", f"2026-08-21 {hhmm}:40", "ok")
                 for hhmm in quarter[:3]])
            connection.commit()
        finally:
            connection.close()
        connection = sqlite3.connect(self.db_root / "live_trades.db")
        try:
            connection.executemany(
                "INSERT INTO trade_cycles VALUES (?,?,?)",
                [(f"2026-08-21T{hhmm}", f"2026-08-21 {hhmm}:50", "live")
                 for hhmm in quarter])
            connection.commit()
        finally:
            connection.close()
        connection = sqlite3.connect(self.db_root / "account.db")
        try:
            connection.executemany(
                "INSERT INTO account_snapshots VALUES (?,?,?)",
                [(f"2026-08-21 {hhmm}:55", "live", 100.0)
                 for hhmm in quarter[:4]])
            connection.commit()
        finally:
            connection.close()
        for hhmm in quarter[:4]:
            path = self.status_dir / f"push-2026-08-21T{hhmm.replace(':', '-')}.json"
            path.write_text(json.dumps({
                "cycle_id": f"2026-08-21T{hhmm}",
                "status": "succeeded",
                "finished_at": f"2026-08-21 {hhmm}:59",
            }), encoding="utf-8")

    def test_reports_age_and_trailing_zero_slots_without_alert_threshold(self):
        self._insert_cycles()
        payload = audit.build(
            db_root=self.db_root,
            stage_status_dir=self.status_dir,
            activation_start="2026-08-21 05:00:00",
            as_of="2026-08-21 06:15:00",
            finality_seconds=900,
        )
        rows = {row["name"]: row for row in payload["observations"]}
        self.assertEqual(0, rows["market_fast_writer_receipt"][
            "consecutive_zero_output_slots"])
        self.assertEqual(1, rows["market_slow_writer_receipt"][
            "consecutive_zero_output_slots"])
        self.assertEqual(2, rows["analyst_writer"][
            "consecutive_zero_output_slots"])
        self.assertEqual(2, rows["analyst_writer"][
            "maximum_zero_output_streak_slots"])
        self.assertEqual({"2": 1}, rows["analyst_writer"][
            "zero_output_streak_distribution"])
        self.assertEqual(0, rows["trades_writer_terminal"][
            "consecutive_zero_output_slots"])
        self.assertEqual(0, rows["market_fast_writer_receipt"][
            "maximum_zero_output_streak_slots"])
        self.assertEqual(1, rows["account_snapshot_writer"][
            "consecutive_zero_output_slots"])
        self.assertEqual(1, rows["push_pipeline_terminal"][
            "consecutive_zero_output_slots"])
        self.assertEqual(
            "NOT_EVALUATED_BEFORE_ACTIVATION",
            rows["analyst_writer"]["alert_status"])
        self.assertEqual(2, rows["analyst_writer"]["alert_threshold_slots"])
        self.assertIsNone(rows["analyst_writer"][
            "effective_alert_threshold_slots"])
        self.assertEqual(
            "OBSERVED_REGISTERED_THRESHOLD",
            payload["overall_status"])
        self.assertEqual(0, payload["safety"]["production_database_writes"])
        self.assertEqual(0, payload["safety"]["orders_placed"])

    def test_missing_database_is_partial_not_green(self):
        (self.db_root / "analysis.db").unlink()
        payload = audit.build(
            db_root=self.db_root,
            stage_status_dir=self.status_dir,
            activation_start="2026-08-21 05:00:00",
            as_of="2026-08-21 06:15:00",
            finality_seconds=900,
        )
        analyst = next(
            row for row in payload["observations"]
            if row["name"] == "analyst_writer")
        self.assertEqual("SOURCE_ERROR", analyst["observation_status"])
        self.assertTrue(analyst["source_errors"])
        self.assertEqual(
            "PARTIAL_REGISTERED_THRESHOLD",
            payload["overall_status"])

    def test_registered_threshold_uses_only_post_activation_mature_slots(self):
        self._insert_cycles()
        registration = {
            "status": "REGISTERED_FORWARD_ONLY",
            "activation_cst": "2026-08-21T05:00:00+08:00",
            "activated": True,
            "alert_threshold_slots": 2,
            "effective_alert_threshold_slots": 2,
            "comparison": ">=",
            "cadence_semantics": "each_writer_own_expected_slot_cadence",
            "historical_rejudgement": False,
            "external_alert_wiring": False,
            "scheduler_authority": False,
            "trading_authority": False,
            "calibration_observations": [],
        }
        with mock.patch.object(
            audit.thresholds,
            "critical_output_zero_streak_registration_facts",
            return_value=registration,
        ):
            payload = audit.build(
                db_root=self.db_root,
                stage_status_dir=self.status_dir,
                activation_start="2026-08-21 05:00:00",
                as_of="2026-08-21 06:15:00",
                finality_seconds=900,
            )
        rows = {row["name"]: row for row in payload["observations"]}
        self.assertEqual(2, rows["analyst_writer"][
            "alert_consecutive_zero_output_slots"])
        self.assertEqual(
            "ALERT_CONDITION_OBSERVED",
            rows["analyst_writer"]["alert_status"])
        self.assertEqual(
            "OBSERVED_ALERT_CONDITION", payload["overall_status"])
        self.assertFalse(payload["safety"]["external_send"])
        self.assertEqual(0, payload["safety"]["orders_placed"])

    def test_main_writes_bounded_json_artifact(self):
        self._insert_cycles()
        output = self.root / "out" / "liveness.json"
        rc = audit.main([
            "--db-root", str(self.db_root),
            "--stage-status-dir", str(self.status_dir),
            "--activation-start", "2026-08-21 05:00:00",
            "--as-of", "2026-08-21 06:15:00",
            "--finality-seconds", "900",
            "--json-out", str(output),
        ])
        self.assertEqual(0, rc)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(6, len(payload["observations"]))
        self.assertFalse(list(output.parent.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
