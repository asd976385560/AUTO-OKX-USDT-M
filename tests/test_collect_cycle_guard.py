# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = ROOT / "collectors"
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))

import collect_cycle  # noqa: E402


class CollectCycleRunGuardTests(unittest.TestCase):
    CYCLE = "2026-08-13T03:15"
    HOUR_CYCLE = "2026-08-13T03:00"

    @staticmethod
    def _success_step(name, *_args, **_kwargs):
        payload = {"sources": []} if name == "news" else {}
        return {
            "name": name,
            "ok": True,
            "rc": 0,
            "dur_s": 0.01,
            "payload": payload,
            "stderr_tail": "",
        }

    def test_exact_cycle_lock_allows_only_one_live_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = collect_cycle._acquire_run_guard(
                root, "quarter", self.CYCLE, stale_after_seconds=900)
            self.assertEqual(first["status"], "acquired")
            second = collect_cycle._acquire_run_guard(
                root, "quarter", self.CYCLE, stale_after_seconds=900)
            self.assertEqual(second["status"], "duplicate_running")
            self.assertEqual(second["owner_pid"], os.getpid())
            collect_cycle._release_run_guard(first)
            third = collect_cycle._acquire_run_guard(
                root, "quarter", self.CYCLE, stale_after_seconds=900)
            self.assertEqual(third["status"], "acquired")
            collect_cycle._release_run_guard(third)

    def test_dead_expired_exact_cycle_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(exist_ok=True)
            lock, _receipt = collect_cycle._guard_paths(
                root, "quarter", self.CYCLE)
            lock.write_text(json.dumps({
                "schema_version": 1,
                "tier": "quarter",
                "cycle": self.CYCLE,
                "pid": 999999,
                "token": "dead",
            }), encoding="utf-8")
            old = time.time() - 1000
            os.utime(lock, (old, old))
            with mock.patch.object(
                    collect_cycle, "_pid_is_alive", return_value=False):
                result = collect_cycle._acquire_run_guard(
                    root, "quarter", self.CYCLE,
                    stale_after_seconds=900)
            self.assertEqual(result["status"], "acquired")
            collect_cycle._release_run_guard(result)

    def test_corrupt_expired_exact_cycle_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(exist_ok=True)
            lock, _receipt = collect_cycle._guard_paths(
                root, "quarter", self.CYCLE)
            lock.write_text("not-json", encoding="utf-8")
            old = time.time() - 1000
            os.utime(lock, (old, old))
            result = collect_cycle._acquire_run_guard(
                root, "quarter", self.CYCLE, stale_after_seconds=900)
            self.assertEqual(result["status"], "acquired")
            collect_cycle._release_run_guard(result)

    def test_success_receipt_makes_late_scheduler_run_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard_dir = root / "guards"
            log_dir = root / "logs"
            argv = [
                "collect_cycle.py", "--tier", "quarter",
                "--db-root", str(root / "db"),
                "--guard-dir", str(guard_dir),
                "--log-dir", str(log_dir),
            ]
            runner = mock.Mock(side_effect=self._success_step)
            with (
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.CYCLE),
                mock.patch.object(collect_cycle, "run_step", runner),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()) as first_stdout,
            ):
                first_rc = collect_cycle.main()
            self.assertEqual(first_rc, 0)
            first = json.loads(first_stdout.getvalue())
            self.assertTrue(first["ok"])
            self.assertEqual(runner.call_count, 2)

            runner.reset_mock()
            with (
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.CYCLE),
                mock.patch.object(collect_cycle, "run_step", runner),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()) as second_stdout,
            ):
                second_rc = collect_cycle.main()
            self.assertEqual(second_rc, 0)
            second = json.loads(second_stdout.getvalue())
            self.assertEqual(second["duplicate_skip"], "duplicate_completed")
            runner.assert_not_called()

    def test_dry_collect_never_creates_production_guard_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard_dir = root / "guards"
            argv = [
                "collect_cycle.py", "--tier", "quarter", "--dry-collect",
                "--db-root", str(root / "db"),
                "--guard-dir", str(guard_dir),
                "--log-dir", str(root / "logs"),
            ]
            with (
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.CYCLE),
                mock.patch.object(
                    collect_cycle, "run_step",
                    side_effect=self._success_step),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(collect_cycle.main(), 0)
            self.assertFalse(guard_dir.exists())

    def test_explicit_current_cycle_is_pinned_into_fast_step(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = mock.Mock(side_effect=self._success_step)
            argv = [
                "collect_cycle.py", "--tier", "quarter",
                "--cycle", self.CYCLE,
                "--db-root", str(root / "db"),
                "--guard-dir", str(root / "guards"),
                "--log-dir", str(root / "logs"),
            ]
            with (
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.CYCLE),
                mock.patch.object(collect_cycle, "run_step", runner),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()) as output,
            ):
                return_code = collect_cycle.main()

            result = json.loads(output.getvalue())
            fast_args = runner.call_args_list[0].args[2]

        self.assertEqual(0, return_code)
        self.assertEqual(self.CYCLE, result["cycle"])
        self.assertEqual(
            ["--db-root", str(root / "db"), "--cycle", self.CYCLE],
            fast_args,
        )

    def test_stale_explicit_cycle_rejects_before_guard_log_or_steps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = mock.Mock()
            guard = mock.Mock()
            argv = [
                "collect_cycle.py", "--tier", "quarter",
                "--cycle", "2026-08-13T03:00",
                "--db-root", str(root / "db"),
                "--guard-dir", str(root / "guards"),
                "--log-dir", str(root / "logs"),
            ]
            with (
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.CYCLE),
                mock.patch.object(collect_cycle, "_acquire_run_guard", guard),
                mock.patch.object(collect_cycle, "run_step", runner),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()) as output,
            ):
                return_code = collect_cycle.main()

            result = json.loads(output.getvalue())
            self.assertEqual(2, return_code)
            self.assertEqual(["natural_cycle_guard"], result["failed"])
            self.assertFalse(result["network_started"])
            self.assertEqual(0, result["database_writes"])
            self.assertFalse((root / "guards").exists())
            self.assertFalse((root / "logs").exists())
            guard.assert_not_called()
            runner.assert_not_called()

    def test_tier_mismatch_rejects_before_guard_log_or_steps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = mock.Mock()
            argv = [
                "collect_cycle.py", "--tier", "hourly",
                "--cycle", self.CYCLE,
                "--guard-dir", str(root / "guards"),
                "--log-dir", str(root / "logs"),
            ]
            with (
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.CYCLE),
                mock.patch.object(collect_cycle, "run_step", runner),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()) as output,
            ):
                return_code = collect_cycle.main()

            result = json.loads(output.getvalue())
            self.assertEqual(2, return_code)
            self.assertIn("tier does not match", result["error"])
            runner.assert_not_called()
            self.assertFalse((root / "guards").exists())
            self.assertFalse((root / "logs").exists())

    def test_fast_degraded_is_warning_without_changing_success_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            argv = [
                "collect_cycle.py", "--tier", "quarter",
                "--db-root", str(root / "db"),
                "--guard-dir", str(root / "guards"),
                "--log-dir", str(root / "logs"),
            ]

            def runner(name, *_args, **_kwargs):
                if name == "fast":
                    return {
                        "name": "fast", "ok": True, "rc": 0, "dur_s": 1.0,
                        "payload": {
                            "status": "degraded",
                            "warnings": ["official_positioning: rc=1"],
                        },
                        "stderr_tail": "",
                    }
                return self._success_step(name)

            with (
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.CYCLE),
                mock.patch.object(collect_cycle, "run_step", side_effect=runner),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()) as output,
            ):
                return_code = collect_cycle.main()
            result = json.loads(output.getvalue())
        self.assertEqual(0, return_code)
        self.assertTrue(result["ok"])
        self.assertEqual([], result["failed"])
        self.assertTrue(result["steps"][0]["degraded"])
        self.assertIn(
            "fast:degraded: official_positioning: rc=1",
            result["warnings"],
        )

    def test_hourly_runs_news_and_slow_in_parallel_then_nudges_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            argv = [
                "collect_cycle.py", "--tier", "hourly",
                "--db-root", str(root / "db"),
                "--guard-dir", str(root / "guards"),
                "--log-dir", str(root / "logs"),
            ]
            entered = {"news": threading.Event(), "slow": threading.Event()}
            observed_args = {}

            def runner(name, _script, step_args, _timeout):
                observed_args[name] = list(step_args)
                if name == "fast":
                    return self._success_step(name)
                entered[name].set()
                peer = "slow" if name == "news" else "news"
                if not entered[peer].wait(timeout=1):
                    return {
                        "name": name, "ok": False, "rc": 99,
                        "dur_s": 1.0, "stderr_tail": "ran sequentially",
                    }
                result = self._success_step(name)
                if name == "slow":
                    result["payload"] = {
                        "status_slow": "ok", "status_regime": "degraded",
                    }
                return result

            nudge = mock.Mock(return_value={"nudged": True, "reason": "ok"})
            fake_nudge_module = mock.Mock(nudge_from_collector=nudge)
            with (
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.HOUR_CYCLE),
                mock.patch.object(collect_cycle, "run_step", side_effect=runner),
                mock.patch.object(collect_cycle, "_nudge_mod", fake_nudge_module),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()) as output,
            ):
                return_code = collect_cycle.main()

            result = json.loads(output.getvalue())
            expected_db_root = str(root / "db")
        self.assertEqual(0, return_code)
        self.assertTrue(result["hourly_parallel_tail"])
        self.assertEqual(
            ["fast", "news", "slow"],
            [step["name"] for step in result["steps"]],
        )
        self.assertIn("--defer-dispatch-nudge", observed_args["slow"])
        self.assertIn(self.HOUR_CYCLE, observed_args["slow"])
        nudge.assert_called_once_with(
            "collect_cycle_hourly_complete",
            expected_db_root,
            ["ok", "degraded"],
            dry_collect=False,
        )
        self.assertEqual(
            {"nudged": True, "reason": "ok"},
            result["deferred_dispatch_nudge"],
        )

    def test_success_log_retains_only_compact_fast_data_quality(self):
        receipt = {
            "expected": 431,
            "tickers": 431,
            "ticker_coverage": 1.0,
            "ticker_transport": {
                "attempts": 2,
                "recovered_after_cold_retry": True,
                "historical_retry": False,
                "unbounded_retry": False,
            },
        }
        output = {
            "ok": True,
            "steps": [{
                "name": "fast", "ok": True, "rc": 0, "dur_s": 1.0,
                "payload": {
                    "data_quality": receipt,
                    "unrelated_large_payload": {"must": "be trimmed"},
                },
            }, {
                "name": "news", "ok": True, "rc": 0, "dur_s": 1.0,
                "payload": {"sources": ["trimmed"]},
            }],
        }
        slim = collect_cycle._slim_for_log(output)
        self.assertNotIn("payload", slim["steps"][0])
        self.assertEqual(slim["steps"][0]["data_quality"], receipt)
        self.assertNotIn("payload", slim["steps"][1])
        self.assertNotIn("data_quality", slim["steps"][1])
if __name__ == "__main__":
    unittest.main()
