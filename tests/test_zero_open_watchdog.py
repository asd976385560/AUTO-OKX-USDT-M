# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts import zero_open_watchdog as watchdog
from scripts import stage_runner


class ZeroOpenWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "analysis.db"
        connection = sqlite3.connect(self.db)
        connection.executescript(
            "CREATE TABLE analysis_runs ("
            "cycle_id TEXT PRIMARY KEY,status TEXT,mode TEXT,raw TEXT);"
            "CREATE TABLE analysis_signals ("
            "cycle_id TEXT,symbol TEXT,action TEXT);"
        )
        connection.close()
        self.activation = "2026-09-01T04:00"

    def _insert_slots(
        self,
        count: int,
        *,
        open_at: int | None = None,
        error_at: int | None = None,
        omit_at: int | None = None,
        mismatch_at: int | None = None,
    ) -> str:
        start = datetime.strptime(self.activation, "%Y-%m-%dT%H:%M")
        connection = sqlite3.connect(self.db)
        end = self.activation
        try:
            for index in range(count):
                cycle = (start + timedelta(minutes=15 * index)).strftime(
                    "%Y-%m-%dT%H:%M")
                end = cycle
                if index == omit_at:
                    continue
                action = "open_long" if index == open_at else None
                raw_signals = ([{"action": action}] if action else [])
                if index == mismatch_at:
                    raw_signals = [{"action": "open_short"}]
                raw = {
                    "signals": raw_signals,
                    "raw": {
                        "candidate_coverage": {
                            "reason_family_counts": {
                                "ENTRY_EXTENDED": 1,
                            },
                        },
                        "side_regime_soft_veto_shadow": {
                            "counterfactuals": [{"candidate_id": "cand_x"}],
                        },
                    },
                }
                connection.execute(
                    "INSERT INTO analysis_runs VALUES (?,?,?,?)",
                    (
                        cycle,
                        "error" if index == error_at else "ok",
                        "full",
                        json.dumps(raw),
                    ),
                )
                if action:
                    connection.execute(
                        "INSERT INTO analysis_signals VALUES (?,?,?)",
                        (cycle, "AAA-USDT-SWAP", action),
                    )
            connection.commit()
        finally:
            connection.close()
        return end

    def _evaluate(self, end: str, threshold: int = 96) -> dict:
        return watchdog.evaluate_zero_open_watchdog(
            self.db,
            end,
            activation_cycle=self.activation,
            threshold_slots=threshold,
        )

    def test_95_slots_below_threshold_and_96_alerts(self):
        end95 = self._insert_slots(95)
        result95 = self._evaluate(end95)
        self.assertEqual("BELOW_THRESHOLD", result95["status"])
        self.assertEqual(95, result95["successful_zero_open_slots"])
        self.assertFalse(result95["alert_required"])

        end96 = self._insert_one_after(end95)
        result96 = self._evaluate(end96)
        self.assertEqual("ALERT_CONDITION_OBSERVED", result96["status"])
        self.assertEqual(96, result96["successful_zero_open_slots"])
        self.assertTrue(result96["alert_required"])
        self.assertEqual(96, result96["reason_family_counts"]["ENTRY_EXTENDED"])
        self.assertEqual(96, result96["soft_veto_shadow_counterfactual_count"])

    def _insert_one_after(self, prior: str) -> str:
        cycle = (datetime.strptime(prior, "%Y-%m-%dT%H:%M")
                 + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
        raw = {
            "signals": [],
            "raw": {
                "candidate_coverage": {
                    "reason_family_counts": {"ENTRY_EXTENDED": 1}},
                "side_regime_soft_veto_shadow": {
                    "counterfactuals": [{"candidate_id": "cand_x"}]},
            },
        }
        connection = sqlite3.connect(self.db)
        connection.execute(
            "INSERT INTO analysis_runs VALUES (?,?,?,?)",
            (cycle, "ok", "full", json.dumps(raw)),
        )
        connection.commit()
        connection.close()
        return cycle

    def test_97th_slot_keeps_episode_dedupe_key(self):
        end96 = self._insert_slots(96)
        result96 = self._evaluate(end96)
        end97 = self._insert_one_after(end96)
        result97 = self._evaluate(end97)
        self.assertEqual(
            result96["alert_dedupe_key"], result97["alert_dedupe_key"])
        self.assertEqual(97, result97["successful_zero_open_slots"])

    def test_gap_error_and_open_each_reset_exact_streak(self):
        end = self._insert_slots(20, omit_at=10)
        self.assertEqual(9, self._evaluate(end, threshold=5)[
            "successful_zero_open_slots"])

        self.setUp_fresh()
        end = self._insert_slots(20, error_at=10)
        self.assertEqual(9, self._evaluate(end, threshold=5)[
            "successful_zero_open_slots"])

        self.setUp_fresh()
        end = self._insert_slots(20, open_at=10)
        result = self._evaluate(end, threshold=5)
        self.assertEqual(9, result["successful_zero_open_slots"])
        self.assertEqual("accepted_open_signal_observed", result["stop_reason"])
        self.assertEqual(1, result["accepted_open_signals"])

    def setUp_fresh(self):
        connection = sqlite3.connect(self.db)
        connection.execute("DELETE FROM analysis_signals")
        connection.execute("DELETE FROM analysis_runs")
        connection.commit()
        connection.close()

    def test_raw_table_mismatch_fails_closed(self):
        end = self._insert_slots(4, mismatch_at=3)
        result = self._evaluate(end, threshold=2)
        self.assertEqual("SOURCE_INCONSISTENT", result["status"])
        self.assertFalse(result["source_consistent"])
        self.assertFalse(result["alert_required"])
        self.assertIsNone(result["accepted_open_signals"])

    def test_before_activation_is_inactive(self):
        result = watchdog.evaluate_zero_open_watchdog(
            self.db,
            "2026-09-01T03:45",
            activation_cycle=self.activation,
            threshold_slots=96,
        )
        self.assertEqual("INACTIVE", result["status"])

    def test_closure_requires_strict_live_business_terminal(self):
        end = self._insert_slots(1)
        stage_dir = Path(self.temporary.name) / "stage-status"
        stage_dir.mkdir()
        stage_path = stage_dir / f"live-{end.replace(':', '-')}.json"
        stage_path.write_text(json.dumps({
            "stage": "live",
            "cycle_id": end,
            "status": "failed",
            "returncode": 86,
            "business_check": {
                "ok": False,
                "failure_kind": "business_output_missing",
            },
            "business_terminal_gate": {
                "status": "not_met",
                "strict_cycle_pass": False,
            },
        }), encoding="utf-8")
        live_db = Path(self.temporary.name) / "live_trades.db"
        connection = sqlite3.connect(live_db)
        connection.execute(
            "CREATE TABLE trade_cycles("
            "cycle_id TEXT PRIMARY KEY,decision TEXT,n_orders INTEGER,raw TEXT)"
        )
        connection.commit()
        connection.close()

        with mock.patch.object(
            watchdog.thresholds,
            "minimal_contract_closure_active",
            return_value=True,
        ):
            failed = watchdog.evaluate_zero_open_watchdog(
                self.db,
                end,
                activation_cycle=self.activation,
                threshold_slots=12,
                stage_status_dir=stage_dir,
                live_trades_db=live_db,
            )
        self.assertEqual(0, failed["successful_zero_open_slots"])
        self.assertEqual(1, failed["observed_natural_slots"])
        self.assertEqual(0, failed["strict_business_success_slots"])
        self.assertEqual(1, failed["failed_natural_slots"])
        reasons = failed["failure_details"][0]["reasons"]
        self.assertIn("live_stage_status=failed", reasons)
        self.assertIn("business_check_not_ok", reasons)
        self.assertIn("trade_cycle_missing", reasons)

        terminal = {
            "schema_version": 1,
            "cycle_id": end,
            "status": "completed",
            "completed_at_cst": "2026-09-01 04:01:00",
        }
        stage_path.write_text(json.dumps({
            "stage": "live",
            "cycle_id": end,
            "status": "succeeded",
            "returncode": 0,
            "business_check": {"ok": True, "business_terminal": terminal},
            "business_terminal_gate": {
                "status": "met",
                "strict_cycle_pass": True,
            },
        }), encoding="utf-8")
        connection = sqlite3.connect(live_db)
        connection.execute(
            "INSERT INTO trade_cycles VALUES(?,?,?,?)",
            (end, "hold", 0, json.dumps({
                "status": "ok",
                "batch_status": "completed",
                "batch_ok": True,
                "business_terminal": terminal,
            })),
        )
        connection.commit()
        connection.close()
        with mock.patch.object(
            watchdog.thresholds,
            "minimal_contract_closure_active",
            return_value=True,
        ):
            succeeded = watchdog.evaluate_zero_open_watchdog(
                self.db,
                end,
                activation_cycle=self.activation,
                threshold_slots=12,
                stage_status_dir=stage_dir,
                live_trades_db=live_db,
            )
        self.assertEqual(1, succeeded["successful_zero_open_slots"])
        self.assertEqual(1, succeeded["observed_natural_slots"])
        self.assertEqual(1, succeeded["strict_business_success_slots"])
        self.assertEqual(0, succeeded["failed_natural_slots"])
        self.assertEqual([], succeeded["failure_details"])

    def test_artifact_is_atomic_and_declares_no_authority(self):
        end = self._insert_slots(2)
        result = self._evaluate(end, threshold=2)
        output = Path(self.temporary.name) / "quality"
        path = watchdog.publish_watchdog_artifact(result, output)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(persisted["writer_authority"])
        self.assertFalse(persisted["executor_authority"])
        self.assertFalse(persisted["dispatch_authority"])
        self.assertEqual([], list(output.glob("*.tmp")))


class ZeroOpenStageIntegrationTests(unittest.TestCase):
    def test_alert_is_deduplicated_and_does_not_call_business_paths(self):
        result = {
            "schema": watchdog.SCHEMA,
            "cycle_id": "2026-09-02T04:00",
            "status": "ALERT_CONDITION_OBSERVED",
            "alert_required": True,
            "alert_dedupe_key": "zero-open-watchdog:v1:a:b",
        }
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(
                    watchdog, "evaluate_zero_open_watchdog",
                    return_value=dict(result)),
                mock.patch.object(
                    watchdog, "publish_watchdog_artifact",
                    return_value=Path(temporary) / "artifact.json"),
                mock.patch.object(
                    watchdog, "render_alert", return_value="alert"),
                mock.patch.object(stage_runner, "STATUS_DIR", Path(temporary)),
                mock.patch.object(
                    stage_runner.subprocess, "run",
                    return_value=mock.Mock(returncode=0)) as run,
                mock.patch.dict(
                    "os.environ", {"OKX_STAGE_RUNNER_NO_ALERT": "0"}),
            ):
                observed = stage_runner._run_zero_open_watchdog(
                    "2026-09-02T04:00")
        self.assertTrue(observed["alert"]["delivered"])
        command = run.call_args.args[0]
        self.assertIn("--dedupe-key", command)
        self.assertIn(result["alert_dedupe_key"], command)
        self.assertNotIn("order_executor", " ".join(map(str, command)))
        self.assertNotIn("dispatcher", " ".join(map(str, command)))

    def test_evaluator_failure_never_raises_or_authorizes_retry(self):
        with mock.patch.object(
                watchdog, "evaluate_zero_open_watchdog",
                side_effect=RuntimeError("boom")):
            observed = stage_runner._run_zero_open_watchdog(
                "2026-09-02T04:00")
        self.assertEqual("EVALUATOR_ERROR", observed["status"])
        self.assertFalse(observed["retry_authority"])
        self.assertFalse(observed["executor_authority"])

    def test_artifact_failure_does_not_suppress_alert(self):
        result = {
            "schema": watchdog.SCHEMA,
            "cycle_id": "2026-09-02T04:00",
            "status": "ALERT_CONDITION_OBSERVED",
            "alert_required": True,
            "alert_dedupe_key": "zero-open-watchdog:v1:a:b",
        }
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(
                    watchdog, "evaluate_zero_open_watchdog",
                    return_value=dict(result)),
                mock.patch.object(
                    watchdog, "publish_watchdog_artifact",
                    side_effect=OSError("disk full")),
                mock.patch.object(
                    watchdog, "render_alert", return_value="alert"),
                mock.patch.object(stage_runner, "STATUS_DIR", Path(temporary)),
                mock.patch.object(
                    stage_runner.subprocess, "run",
                    return_value=mock.Mock(returncode=0)) as run,
                mock.patch.dict(
                    "os.environ", {"OKX_STAGE_RUNNER_NO_ALERT": "0"}),
            ):
                observed = stage_runner._run_zero_open_watchdog(
                    "2026-09-02T04:00")
        self.assertIn("OSError", observed["artifact_error"])
        self.assertTrue(observed["alert"]["delivered"])
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
