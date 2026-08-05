from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for sub in (ROOT / "collectors", ROOT / "scripts"):
    if str(sub) not in sys.path:
        sys.path.insert(0, str(sub))

from core import dispatcher  # noqa: E402
import collection_monitor  # noqa: E402
import cycle_contract  # noqa: E402
import live_reconcile_monitor  # noqa: E402
import push_pipeline  # noqa: E402
import stage_runner  # noqa: E402
import trigger_agent  # noqa: E402


INVALID_CYCLES = (
    "../2026-08-04T12:00",
    r"..\2026-08-04T12:00",
    "2026-02-30T12:00",
    "2026-08-04T24:00",
    "2026-08-04T12:60",
)


class CycleIdContractTests(unittest.TestCase):
    def test_non_slot_minute_tokens_match_all_producers_and_consumers(self):
        cycle = "2026-08-04T12:07"
        self.assertEqual(cycle_contract.validate_cycle_id(cycle), cycle)
        self.assertEqual(
            trigger_agent.session_key("live", cycle),
            stage_runner._stage_session_key("live", cycle),
        )
        self.assertEqual(
            trigger_agent.session_key("live", cycle),
            "live-20260804-1207",
        )
        self.assertEqual(
            trigger_agent._stage_status_path("live", cycle).name,
            stage_runner._status_path("live", cycle).name,
        )
        self.assertEqual(
            trigger_agent._stage_status_path("live", cycle).name,
            "live-2026-08-04T12-07.json",
        )
        self.assertEqual(
            cycle_contract.cycle_artifact_token(cycle),
            "2026-08-04-1207",
        )
        for value in (
            trigger_agent.session_key("live", cycle),
            trigger_agent._stage_status_path("live", cycle).name,
            cycle_contract.cycle_artifact_token(cycle),
        ):
            self.assertNotIn("/", value)
            self.assertNotIn("\\", value)
            self.assertNotIn("..", value)

    def test_strict_validator_rejects_traversal_and_impossible_timestamps(self):
        for cycle in INVALID_CYCLES:
            with self.subTest(cycle=cycle):
                with self.assertRaises(ValueError):
                    cycle_contract.validate_cycle_id(cycle)
                with self.assertRaises(ValueError):
                    cycle_contract.cycle_session_token(cycle)
                with self.assertRaises(ValueError):
                    cycle_contract.cycle_status_token(cycle)
                with self.assertRaises(ValueError):
                    cycle_contract.cycle_artifact_token(cycle)

    def test_trigger_rejects_every_invalid_cycle_before_logs_or_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "trigger"
            with mock.patch.object(trigger_agent, "LOG_DIR", log_dir), \
                 mock.patch.object(trigger_agent, "build_cmd") as build, \
                 mock.patch.object(trigger_agent, "_fire_push_script") as push, \
                 mock.patch.object(trigger_agent.subprocess, "Popen") as popen:
                for cycle in INVALID_CYCLES:
                    with self.subTest(stage="live", cycle=cycle):
                        with self.assertRaises(ValueError):
                            trigger_agent.fire("live", cycle)
                    with self.subTest(stage="push", cycle=cycle):
                        with self.assertRaises(ValueError):
                            trigger_agent.fire("push", cycle)
            self.assertFalse(log_dir.exists())
            build.assert_not_called()
            push.assert_not_called()
            popen.assert_not_called()

    def test_stage_cli_rejects_before_status_or_child_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp) / "stage-status"
            run = mock.Mock()
            with mock.patch.object(stage_runner, "STATUS_DIR", status_dir), \
                 mock.patch.object(stage_runner.subprocess, "run", run):
                for cycle in INVALID_CYCLES:
                    argv = [
                        "stage_runner.py", "--stage", "live",
                        "--cycle", cycle, "--", "child",
                    ]
                    with self.subTest(cycle=cycle), \
                         mock.patch.object(stage_runner.sys, "argv", argv), \
                         redirect_stderr(io.StringIO()), \
                         self.assertRaises(SystemExit):
                        stage_runner.main()
            self.assertFalse(status_dir.exists())
            run.assert_not_called()

    def test_push_pipeline_rejects_before_artifacts_or_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            load_build = mock.Mock()
            run_child = mock.Mock()
            with mock.patch.object(push_pipeline, "WORK", root / "work"), \
                 mock.patch.object(push_pipeline, "REPORT_DIR", root / "reports"), \
                 mock.patch.object(push_pipeline, "RUNLOG", root / "logs" / "runs.jsonl"), \
                 mock.patch.object(push_pipeline, "_load_build", load_build), \
                 mock.patch.object(push_pipeline, "_run", run_child):
                for cycle in INVALID_CYCLES:
                    with self.subTest(cycle=cycle), self.assertRaises(ValueError):
                        push_pipeline.run(cycle, str(root / "db"), no_send=True)
            self.assertFalse((root / "work").exists())
            self.assertFalse((root / "reports").exists())
            self.assertFalse((root / "logs").exists())
            load_build.assert_not_called()
            run_child.assert_not_called()

    def test_dispatcher_rejects_before_db_probe_or_latch(self):
        analysis = mock.Mock()
        dispatched = mock.Mock()
        leased = mock.Mock()
        latched = mock.Mock()
        fire = mock.Mock()
        with mock.patch.object(dispatcher, "analysis_row", analysis), \
             mock.patch.object(dispatcher.ledger, "stage_dispatched", dispatched), \
             mock.patch.object(dispatcher.ledger, "try_profile_lease", leased), \
             mock.patch.object(dispatcher.ledger, "try_stage", latched):
            for cycle in INVALID_CYCLES:
                with self.subTest(cycle=cycle), self.assertRaises(ValueError):
                    dispatcher.dispatch_cycle(
                        Path("db"), Path("db") / "ledger.db", cycle,
                        fire_fn=fire,
                    )
        analysis.assert_not_called()
        dispatched.assert_not_called()
        leased.assert_not_called()
        latched.assert_not_called()
        fire.assert_not_called()

    def test_monitor_consumers_reject_before_status_or_audit_reads(self):
        root_namespace = mock.Mock()
        connect = mock.Mock()
        with mock.patch.object(
            collection_monitor, "_root_namespace", root_namespace
        ), mock.patch.object(collection_monitor.sqlite3, "connect", connect):
            for cycle in INVALID_CYCLES:
                with self.subTest(cycle=cycle):
                    self.assertEqual(
                        collection_monitor._audit_attribution("live", cycle),
                        "audit-unavailable",
                    )
        root_namespace.assert_not_called()
        connect.assert_not_called()

    def test_live_reconcile_cli_rejects_before_runtime_probe(self):
        active = mock.Mock()
        reconcile = mock.Mock()
        for cycle in INVALID_CYCLES:
            argv = ["live_reconcile_monitor.py", "--cycle", cycle]
            with self.subTest(cycle=cycle), \
                 mock.patch.object(live_reconcile_monitor.sys, "argv", argv), \
                 mock.patch.object(live_reconcile_monitor, "active_runner", active), \
                 mock.patch.object(live_reconcile_monitor, "run_reconcile", reconcile), \
                 redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                live_reconcile_monitor.main()
        active.assert_not_called()
        reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
