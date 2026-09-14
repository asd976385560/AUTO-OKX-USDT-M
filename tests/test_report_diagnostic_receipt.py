# -*- coding: utf-8 -*-
"""Focused diagnostic tests: local JSON only; no databases or real processes."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import daily_maintenance as dm  # noqa: E402
import report_diagnostic_receipt as diagnostic  # noqa: E402
import reviewer_preflight as preflight  # noqa: E402

DAY = "2026-09-05"


class DiagnosticReceiptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # Tripwires apply even to temporary databases and hidden child commands.
        for target in ("sqlite3.connect", "socket.socket", "subprocess.Popen",
                       "os.system"):
            patch = mock.patch(target, side_effect=AssertionError(
                f"validation side effect forbidden: {target}"))
            patch.start()
            self.addCleanup(patch.stop)
        for target in ("QUALITY_REPORT_DIR", "REVIEWER_READY_DIR"):
            patch = mock.patch.object(dm, target, self.root)
            patch.start()
            self.addCleanup(patch.stop)
        # The exit-quality algorithm is outside this test's scope. Its existing
        # independent validator is exercised elsewhere; no producer is imported.
        patch = mock.patch.object(preflight, "_validate_exit_quality_artifact",
                                  return_value=[])
        patch.start()
        self.addCleanup(patch.stop)
        quality = self.root / f"quality_metrics_{DAY}.json"
        quality.write_text(json.dumps({"ts": f"{DAY} 08:00:00", "metrics": {}}),
                           encoding="utf-8")

    def report(self, *, failed=(), reconcile_rc=0):
        steps = {
            name: {"rc": 2 if name in failed else 0,
                   "accepted": name not in failed,
                   "completed_at": f"{DAY} 08:00:00",
                   "stderr": [f"{name}: exact failure"] if name in failed else []}
            for name in dm.REVIEWER_CRITICAL_STEPS
        }
        if reconcile_rc:
            steps["reconcile"].update(rc=reconcile_rc, accepted=reconcile_rc == 1,
                                      stderr=["unresolved exchange difference"])
        steps["quality_metrics"]["artifact"] = dm._quality_artifact(DAY)
        return {"business_date": DAY, "run_id": "test-maintenance-run",
                "started_at": f"{DAY} 07:55:00",
                "critical_steps_completed_at": f"{DAY} 08:00:00",
                "steps": steps}

    def check_receipt(self, receipt, mode, step):
        self.assertEqual(mode, receipt["report_mode"])
        self.assertEqual(step, receipt["failed_step"])
        self.assertEqual({"start_ts": "2026-09-04 08:00:00",
                          "end_ts": "2026-09-05 08:00:00",
                          "end_exclusive": True, "timezone": "UTC+08:00"},
                         receipt["affected_window"])
        self.assertFalse(receipt["auto_send"])
        self.assertIn(diagnostic.SAFETY_BOUNDARY, receipt["safe_next_action"])
        body = dict(receipt)
        receipt_id = body.pop("receipt_id")
        digest = hashlib.sha256(json.dumps(
            body, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual("sha256:" + digest, receipt_id)
        self.assertLessEqual(len(receipt["stderr_summary"]), diagnostic.SUMMARY_LIMIT)

    def test_blocked_hard_step_precedes_degradation_and_preserves_stderr(self):
        report = self.report(failed=("ledger_invariants", "exit_quality"))
        manifest = dm.write_reviewer_manifest(report, "completed")
        result = preflight.validate_manifest(manifest, DAY)
        self.assertFalse(result["ok"])
        receipt = result["diagnostic_receipt"]
        self.check_receipt(receipt, "blocked", "ledger_invariants")
        self.assertEqual("ledger_invariants: exact failure", receipt["stderr_summary"])
        self.assertEqual(2, receipt["return_code"])
        self.assertEqual("exit_quality", receipt["additional_failures"][0]["failed_step"])
        self.assertEqual(manifest["diagnostic_receipt"], receipt)

    def test_provisional_exit_failure_retains_candidate_window_and_single_receipt(self):
        manifest = dm.write_reviewer_manifest(self.report(failed=("exit_quality",)),
                                              "completed")
        result = preflight.validate_manifest(manifest, DAY)
        self.assertTrue(result["ok"], result["errors"])
        self.check_receipt(result["diagnostic_receipt"], "provisional", "exit_quality")
        self.assertEqual(manifest["diagnostic_receipt"], result["diagnostic_receipt"])
        self.assertEqual("2026-09-04 04:00:00",
                         result["diagnostic_receipt"]["candidate_window"]["start_ts"])
        self.assertEqual("2026-09-05 04:00:00",
                         result["diagnostic_receipt"]["candidate_window"]["end_ts"])
        self.assertEqual(1, len(list((self.root / "diagnostics").glob("*.json"))))

    def test_rc1_and_degraded_exit_have_distinct_causes_in_one_receipt(self):
        manifest = dm.build_reviewer_manifest(
            self.report(failed=("exit_quality",), reconcile_rc=1), "completed")
        result = preflight.validate_manifest(manifest, DAY)
        self.assertTrue(result["ok"], result["errors"])
        receipt = result["diagnostic_receipt"]
        self.check_receipt(receipt, "provisional", "reconcile")
        self.assertEqual("live reconciliation unresolved", receipt["failure_summary"])
        self.assertEqual("exit_quality", receipt["additional_failures"][0]["failed_step"])

    def test_republication_is_identical_and_conflicting_run_receipt_is_never_replaced(self):
        report = self.report(failed=("exit_quality",))
        first = dm.write_reviewer_manifest(report, "completed")
        path = Path(first["diagnostic_receipt_path"])
        raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
        # Later noncritical failures and completion timestamps must not drift it.
        report.update(ok=False, completed_at=f"{DAY} 08:15:00")
        report["steps"]["noncritical"] = {"rc": 9, "accepted": False}
        second = dm.write_reviewer_manifest(report, "completed")
        self.assertEqual(first["diagnostic_receipt"], second["diagnostic_receipt"])
        self.assertEqual(raw, path.read_bytes())
        self.assertEqual(mtime, path.stat().st_mtime_ns)
        report["steps"]["exit_quality"]["stderr"] = ["different evidence"]
        with self.assertRaisesRegex(ValueError, "immutable diagnostic receipt conflict"):
            dm.write_reviewer_manifest(report, "completed")
        self.assertEqual(raw, path.read_bytes())
        self.assertEqual(1, len(list((self.root / "diagnostics").glob("*.json"))))
        stored = json.loads(Path(first["path"]).read_text(encoding="utf-8"))
        self.assertEqual(first["diagnostic_receipt"], stored["diagnostic_receipt"])

    def test_sealed_snapshot_detaches_from_later_mutable_step_changes(self):
        report = self.report(failed=("exit_quality",))
        manifest = dm.build_reviewer_manifest(report, "completed")
        original = diagnostic.receipt_bytes(manifest["diagnostic_receipt"])
        report["steps"]["exit_quality"]["stderr"].append("later text")
        manifest["steps"]["exit_quality"]["stderr_summary"] = "changed"
        self.assertEqual(original, diagnostic.receipt_bytes(manifest["diagnostic_receipt"]))
        result = preflight.validate_manifest(manifest, DAY)
        self.assertFalse(result["ok"])
        self.assertEqual("reviewer_preflight.verify_diagnostic_receipt",
                         result["diagnostic_receipt"]["failed_step"])

    def test_archived_receipt_tampering_blocks_without_repair(self):
        manifest = dm.write_reviewer_manifest(self.report(failed=("exit_quality",)),
                                              "completed")
        path = Path(manifest["diagnostic_receipt_path"])
        path.write_bytes(b"tampered\n")
        result = preflight.validate_manifest(manifest, DAY)
        self.assertFalse(result["ok"])
        self.assertIn("immutable diagnostic receipt bytes differ", " ".join(result["errors"]))
        self.assertEqual(b"tampered\n", path.read_bytes())

    def test_artifact_drift_names_the_exact_validator_step(self):
        manifest = dm.write_reviewer_manifest(self.report(failed=("exit_quality",)),
                                              "completed")
        path = Path(manifest["steps"]["quality_metrics"]["artifact"]["path"])
        path.write_text('{"ts":"2026-09-05 08:00:00","metrics":{"drift":1}}',
                        encoding="utf-8")
        result = preflight.validate_manifest(manifest, DAY)
        self.assertFalse(result["ok"])
        receipt = result["diagnostic_receipt"]
        self.check_receipt(receipt, "blocked", "quality_metrics")
        self.assertIn("hash differs", receipt["failure_summary"])
        self.assertEqual("", receipt["stderr_summary"])

    def test_running_and_final_candidate_do_not_archive_failure_receipts(self):
        for state in ("running", "completed"):
            with self.subTest(state=state):
                manifest = dm.write_reviewer_manifest(self.report(), state)
                self.assertNotIn("diagnostic_receipt", manifest)
        self.assertFalse((self.root / "diagnostics").exists())
        self.assertTrue(preflight.validate_manifest(manifest, DAY)["ok"])

    def run_maintenance(self, outcomes, *, quality_artifact=None):
        steps = [(name, [name], 1, (0, 1) if name == "reconcile" else (0,))
                 for name in dm.REVIEWER_CRITICAL_STEPS]
        steps.append(("later_noncritical", ["later_noncritical"], 1, (0,)))
        handoff = {}

        def run(command, **kwargs):
            name = command[1]
            self.assertEqual("production", kwargs["env"]["OKX_AUDIT_EXECUTION_CONTEXT"])
            if name == "later_noncritical":
                path = self.root / f"reviewer_ready_{DAY}.json"
                handoff.update(json.loads(path.read_text(encoding="utf-8")))
            outcome = outcomes.get(name)
            if isinstance(outcome, Exception):
                raise outcome
            rc, stderr = outcome or (0, "")
            return subprocess.CompletedProcess(command, rc, stdout="not stderr", stderr=stderr)

        stdout = io.StringIO()
        artifact = quality_artifact or dm._quality_artifact(DAY)
        with (mock.patch.object(dm, "STEPS", steps),
              mock.patch.object(dm, "now_cst", return_value=f"{DAY} 08:00:00"),
              mock.patch.object(dm, "_quality_artifact", return_value=artifact),
              mock.patch.object(dm, "_exit_quality_artifact", return_value={"valid": True}),
              mock.patch.object(dm.subprocess, "run", side_effect=run) as child,
              mock.patch("sys.stdout", stdout)):
            rc = dm.main([])
        self.assertEqual(len(steps), child.call_count)  # Existing fail-safe order.
        output = json.loads(stdout.getvalue())
        self.assertEqual(handoff.get("diagnostic_receipt"),
                         output["reviewer_ready"].get("diagnostic_receipt"))
        return rc, output

    def test_real_maintenance_control_flow_captures_accepted_rc1_stderr(self):
        rc, output = self.run_maintenance({"reconcile": (1, "warning: unresolved")})
        self.assertEqual(0, rc)
        manifest = output["reviewer_ready"]
        self.assertTrue(manifest["ready"])
        receipt = manifest["diagnostic_receipt"]
        self.check_receipt(receipt, "provisional", "reconcile")
        self.assertEqual("warning: unresolved", receipt["stderr_summary"])
        self.assertTrue(preflight.validate_manifest(manifest, DAY)["ok"])

    def test_timeout_keeps_partial_stderr_and_exact_step(self):
        timeout = subprocess.TimeoutExpired("account_bills", 1,
                                            stderr=b"page 2 failed\nTimeoutError: read timeout")
        rc, output = self.run_maintenance({"account_bills": timeout})
        self.assertEqual(1, rc)
        receipt = output["reviewer_ready"]["diagnostic_receipt"]
        self.check_receipt(receipt, "blocked", "account_bills")
        self.assertIn("TimeoutError: read timeout", receipt["stderr_summary"])
        self.assertIn("TimeoutExpired", receipt["failure_summary"])
        self.assertEqual(99, receipt["return_code"])

    def test_accepted_process_with_rejected_artifact_retains_artifact_error(self):
        rc, output = self.run_maintenance({}, quality_artifact={
            "valid": False, "error": "ValueError: quality artifact identity differs"})
        self.assertEqual(1, rc)
        receipt = output["reviewer_ready"]["diagnostic_receipt"]
        self.check_receipt(receipt, "blocked", "quality_metrics")
        self.assertEqual(0, receipt["return_code"])
        self.assertEqual("", receipt["stderr_summary"])
        self.assertIn("artifact identity differs", receipt["failure_summary"])

    def test_initial_handoff_write_failure_stops_before_any_command(self):
        stdout = io.StringIO()
        with (mock.patch.object(dm, "now_cst", return_value=f"{DAY} 07:55:00"),
              mock.patch.object(dm, "_atomic_write_json", side_effect=OSError("disk full")),
              mock.patch.object(dm.subprocess, "run") as child,
              mock.patch("sys.stdout", stdout)):
            rc = dm.main([])
        self.assertEqual(2, rc)
        child.assert_not_called()
        receipt = json.loads(stdout.getvalue())["reviewer_ready"]["diagnostic_receipt"]
        self.check_receipt(receipt, "blocked", "reviewer_ready.write")
        self.assertIn("disk full", receipt["failure_summary"])

    def test_missing_manifest_polls_then_emits_one_compact_receipt_without_files(self):
        before = set(self.root.iterdir())
        stdout = io.StringIO()
        with (mock.patch.object(preflight.time, "monotonic", side_effect=[0, 0, 1, 2]),
              mock.patch.object(preflight.time, "sleep") as sleep,
              mock.patch("sys.stdout", stdout)):
            rc = preflight.main(["--ready-dir", str(self.root), "--business-date", DAY,
                                 "--wait-seconds", "2", "--poll-seconds", "0.05"])
        self.assertEqual(1, rc)
        self.assertEqual(2, sleep.call_count)
        self.assertEqual(1, len(stdout.getvalue().splitlines()))
        result = json.loads(stdout.getvalue())
        self.check_receipt(result["diagnostic_receipt"], "blocked",
                           "reviewer_preflight.read_manifest")
        self.assertTrue(result["timed_out"])
        self.assertEqual(before, set(self.root.iterdir()))

    def test_malformed_manifest_emits_receipt_without_claiming_a_maintenance_failure(self):
        path = self.root / f"reviewer_ready_{DAY}.json"
        path.write_bytes(b"{broken")
        result = preflight.wait_for_manifest(path, DAY, 0, 0.05)
        receipt = result["diagnostic_receipt"]
        self.check_receipt(receipt, "blocked", "reviewer_preflight.read_manifest")
        self.assertIn("JSONDecodeError", receipt["failure_summary"])
        self.assertIsNone(receipt["run_id"])
        self.assertEqual(b"{broken", path.read_bytes())

    def test_running_to_provisional_polling_emits_only_the_final_snapshot(self):
        report = self.report(failed=("exit_quality",))
        running = dm.write_reviewer_manifest(report, "running")
        completed = dm.write_reviewer_manifest(report, "completed")
        stdout = io.StringIO()
        with (mock.patch.object(preflight, "_read_json", side_effect=[running, completed]),
              mock.patch.object(preflight.time, "sleep") as sleep,
              mock.patch("sys.stdout", stdout)):
            rc = preflight.main(["--ready-dir", str(self.root), "--business-date", DAY,
                                 "--wait-seconds", "2", "--poll-seconds", "0.05"])
        self.assertEqual(0, rc)
        sleep.assert_called_once()
        self.assertEqual(1, len(stdout.getvalue().splitlines()))
        self.assertEqual(completed["diagnostic_receipt"],
                         json.loads(stdout.getvalue())["diagnostic_receipt"])

    def test_stale_manifest_does_not_attach_old_run_or_window_to_new_day(self):
        manifest = dm.build_reviewer_manifest(self.report(failed=("exit_quality",)),
                                              "completed")
        manifest["business_date"] = "2026-09-04"
        result = preflight.validate_manifest(manifest, DAY)
        self.assertFalse(result["ok"])
        self.check_receipt(result["diagnostic_receipt"], "blocked",
                           "reviewer_preflight.identity")
        self.assertIsNone(result["diagnostic_receipt"]["run_id"])

    def test_malformed_step_metadata_still_returns_a_blocked_validation_receipt(self):
        manifest = dm.build_reviewer_manifest(self.report(failed=("exit_quality",)),
                                              "completed")
        manifest["steps"]["account_bills"]["rc"] = {"invalid": "exit code"}
        path = self.root / f"reviewer_ready_{DAY}.json"
        raw = json.dumps(manifest).encode("utf-8")
        path.write_bytes(raw)
        result = preflight.wait_for_manifest(path, DAY, 0, 0.05)
        self.assertFalse(result["ok"])
        self.check_receipt(result["diagnostic_receipt"], "blocked",
                           "reviewer_preflight.validate_manifest")
        self.assertIn("TypeError", result["diagnostic_receipt"]["failure_summary"])
        self.assertEqual(raw, path.read_bytes())

    def test_legacy_provisional_manifest_still_passes_without_archiving_or_backfill(self):
        manifest = dm.build_reviewer_manifest(self.report(failed=("exit_quality",)),
                                              "completed")
        del manifest["diagnostic_contract_version"]
        del manifest["diagnostic_receipt"]
        original = copy.deepcopy(manifest)
        result = preflight.validate_manifest(manifest, DAY)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(original, manifest)
        self.assertFalse((self.root / "diagnostics").exists())

    def test_stderr_is_bounded_redacted_and_not_replaced_with_stdout(self):
        text = ("old trace\n" * 1000
                + '\x1b[31mError: token="private token" OK-ACCESS-KEY=private-key '
                + "Authorization: Bearer private-bearer\x1b[0m")
        summary = diagnostic.compact_summary(text.encode("utf-8"))
        self.assertNotIn("private", summary)
        self.assertIn("[REDACTED]", summary)
        self.assertNotIn("\x1b", summary)
        self.assertLessEqual(len(summary), diagnostic.SUMMARY_LIMIT)
        self.assertNotIn("credential", diagnostic.compact_summary(
            "Authorization: Basic credential"))
        self.assertEqual(diagnostic.SUMMARY_LIMIT,
                         len(diagnostic.compact_summary("x" * 10000)))
        self.assertEqual("", diagnostic.failure("writer", {"tail": ["stdout"]})[
            "stderr_summary"])

    def test_later_validator_stop_cli_only_reads_stderr_and_emits_one_receipt(self):
        path = self.root / "stderr.txt"
        raw = b"ValueError: exact report window invalid\n"
        path.write_bytes(raw)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = diagnostic.main([
                "--business-date", DAY, "--run-id", "later-report-step",
                "--report-mode", "blocked", "--failed-step", "validate_daily_report",
                "--return-code", "2", "--stderr-file", str(path),
                "--reason", "report validation failed"])
        self.assertEqual(0, rc)
        self.assertEqual(1, len(stdout.getvalue().splitlines()))
        self.check_receipt(json.loads(stdout.getvalue()), "blocked", "validate_daily_report")
        self.assertEqual(raw, path.read_bytes())
        self.assertFalse((self.root / "diagnostics").exists())


if __name__ == "__main__":
    unittest.main()
