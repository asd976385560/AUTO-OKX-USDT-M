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

    def test_child_output_is_forced_to_utf8(self):
        with mock.patch.dict(
                collect_cycle.os.environ,
                {"PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"}):
            child_env = collect_cycle._python_child_env()
        self.assertEqual("utf-8", child_env["PYTHONIOENCODING"])
        self.assertEqual("1", child_env["PYTHONUTF8"])

    def test_news_mixed_zero_row_degraded_and_failed_is_full_outage(self):
        step = {
            "ok": True,
            "payload": {"sources": [
                {"id": "rss_en", "status": "degraded", "fetched": 0,
                 "err": "transport down"},
                {"id": "mx_search", "status": "failed", "fetched": 0,
                 "err": "quota down"},
                {"id": "hourly_only", "status": "skipped",
                 "why": "poll_interval_min=60"},
            ]},
        }
        ok, warnings = collect_cycle._news_verdict(step)
        self.assertFalse(ok)
        self.assertFalse(step["ok"])
        self.assertTrue(step["all_sources_failed"])
        self.assertEqual(1, step["outage_equivalent_degraded"])
        self.assertEqual(2, len(warnings))

    def test_news_natural_zero_events_and_cadence_skips_remain_ok(self):
        step = {
            "ok": True,
            "payload": {"sources": [
                {"id": "quiet", "status": "ok", "fetched": 0},
                {"id": "not_due", "status": "skipped",
                 "why": "poll_interval_min=60"},
            ]},
        }
        ok, warnings = collect_cycle._news_verdict(step)
        self.assertTrue(ok)
        self.assertEqual([], warnings)
        self.assertTrue(step["ok"])

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

    def test_dry_collect_rejects_production_root_without_touching_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "OKX"
            db_root = root / "db"
            db_root.mkdir(parents=True)
            ledger_path = db_root / "ledger.db"
            ledger_path.write_bytes(b"sentinel-ledger")
            argv = [
                "collect_cycle.py", "--tier", "quarter", "--dry-collect",
                "--db-root", str(db_root),
                "--guard-dir", str(root / "guards"),
                "--log-dir", str(root / "logs"),
            ]
            runner = mock.Mock(side_effect=self._success_step)
            with (
                mock.patch.object(collect_cycle, "ROOT", root),
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.CYCLE),
                mock.patch.object(collect_cycle, "run_step", runner),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(64, collect_cycle.main())
            runner.assert_not_called()
            self.assertEqual(b"sentinel-ledger", ledger_path.read_bytes())
            self.assertFalse((root / "logs").exists())

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

    def test_auto_tier_resolves_quarter_slot(self):
        """--tier auto（2026-08-26 兜底 cron 二合一）在 :15/:30/:45 槽自推 quarter。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = mock.Mock(side_effect=self._success_step)
            argv = [
                "collect_cycle.py", "--tier", "auto",
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

        self.assertEqual(0, return_code)
        self.assertEqual("quarter", result["tier"])
        self.assertEqual(self.CYCLE, result["cycle"])
        step_names = [call.args[0] for call in runner.call_args_list]
        self.assertNotIn("slow", step_names)

    def test_auto_tier_resolves_hourly_slot(self):
        """--tier auto 在 :00 槽自推 hourly（cycle_id_for 向下归槽，延迟触发不错档）。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = mock.Mock(side_effect=self._success_step)
            argv = [
                "collect_cycle.py", "--tier", "auto",
                "--db-root", str(root / "db"),
                "--guard-dir", str(root / "guards"),
                "--log-dir", str(root / "logs"),
            ]
            with (
                mock.patch.object(
                    collect_cycle.ledger, "cycle_id_for",
                    return_value=self.HOUR_CYCLE),
                mock.patch.object(collect_cycle, "run_step", runner),
                mock.patch.object(sys, "argv", argv),
                redirect_stdout(io.StringIO()) as output,
            ):
                return_code = collect_cycle.main()
            result = json.loads(output.getvalue())

        self.assertEqual(0, return_code)
        self.assertEqual("hourly", result["tier"])
        self.assertEqual(self.HOUR_CYCLE, result["cycle"])
        step_names = [call.args[0] for call in runner.call_args_list]
        self.assertIn("slow", step_names)

    def test_auto_tier_with_stale_explicit_cycle_still_rejects(self):
        """auto 只放宽层级推导；过期 --cycle 仍在联网/写盘前拒绝。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = mock.Mock()
            argv = [
                "collect_cycle.py", "--tier", "auto",
                "--cycle", "2026-08-13T02:45",
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
        self.assertIn("not the current natural slot", result["error"])
        runner.assert_not_called()
        self.assertFalse((root / "guards").exists())

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
        self.assertIn(
            "--defer-multitimeframe-coverage", observed_args["fast"])
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

    def test_post_slow_mtf_audit_publishes_only_after_ready_slow(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_root = str(Path(temporary) / "db")
            ready_slow = self._success_step("slow")
            ready_slow["payload"] = {
                "status_slow": "ok", "status_regime": "ok"}
            audit_result = {
                "name": "multitimeframe_coverage_audit",
                "ok": True,
                "rc": 0,
                "dur_s": 0.1,
                "payload": {
                    "status": "NOT_MET",
                    "data_completeness_status": "PASSED",
                    "analysis_readiness_status": "NOT_MET",
                },
                "stderr_tail": "",
            }
            with mock.patch.object(
                collect_cycle, "run_step", return_value=audit_result
            ) as runner:
                step = collect_cycle._post_slow_multitimeframe_coverage(
                    db_root, "2026-08-13T08:00", ready_slow)

            self.assertIsNotNone(step)
            self.assertTrue(step["diagnostic_only"])
            self.assertTrue(step["after_same_cycle_slow"])
            args = runner.call_args.args
            self.assertEqual("multitimeframe_coverage_audit", args[0])
            self.assertIn("--execution-context", args[2])
            self.assertIn("test", args[2])
            self.assertIn(
                str(Path(temporary) / "reports" / "quality" /
                    "multitimeframe-coverage-audit.json"),
                args[2],
            )

    def test_post_slow_mtf_audit_retains_canonical_when_slow_not_ready(self):
        slow = self._success_step("slow")
        slow["payload"] = {
            "status_slow": "degraded", "status_regime": "ok"}
        with mock.patch.object(collect_cycle, "run_step") as runner:
            step = collect_cycle._post_slow_multitimeframe_coverage(
                r"E:\isolated\db", "2026-08-13T16:00", slow)
        runner.assert_not_called()
        self.assertEqual("same_cycle_slow_not_complete", step["skipped"])
        self.assertTrue(step["canonical_receipt_retained"])
        self.assertTrue(step["diagnostic_only"])

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
            }, {
                "name": "slow", "ok": True, "rc": 0, "dur_s": 4.0,
                "payload": {
                    "status_slow": "degraded",
                    "status_regime": "ok",
                    "error": "collect_slow degraded: blocks=klines",
                    "collector_timing_s": {
                        "slow_klines": 3.0,
                        "macro_regime": 1.0,
                        "total": 4.0,
                    },
                    "collector_degraded": ["klines"],
                    "collector_degradation_details": {
                        "slow_kline_incomplete": ["1M:420/436"],
                    },
                    "collector_wrote": {"klines": 123},
                    "collector_symbols_count": 436,
                    "collector_position_priority_symbols": ["UNI-USDT-SWAP"],
                    "collector_warnings": ["1M:420/436"],
                    "unrelated_large_payload": {"must": "be trimmed"},
                },
            }],
        }
        slim = collect_cycle._slim_for_log(output)
        self.assertNotIn("payload", slim["steps"][0])
        self.assertEqual(slim["steps"][0]["data_quality"], receipt)
        self.assertNotIn("payload", slim["steps"][1])
        self.assertNotIn("data_quality", slim["steps"][1])
        self.assertNotIn("payload", slim["steps"][2])
        self.assertEqual(
            slim["steps"][2]["collector_timing_s"]["slow_klines"],
            3.0,
        )
        self.assertEqual("degraded", slim["steps"][2]["status_slow"])
        self.assertEqual(["klines"], slim["steps"][2]["collector_degraded"])
        self.assertEqual(
            ["1M:420/436"],
            slim["steps"][2]["collector_degradation_details"]
            ["slow_kline_incomplete"],
        )
        self.assertEqual(123, slim["steps"][2]["collector_wrote"]["klines"])
        self.assertEqual(436, slim["steps"][2]["collector_symbols_count"])
        self.assertEqual(
            ["UNI-USDT-SWAP"],
            slim["steps"][2]["collector_position_priority_symbols"],
        )
        self.assertEqual(["1M:420/436"], slim["steps"][2]["collector_warnings"])
        self.assertNotIn("unrelated_large_payload", slim["steps"][2])




if __name__ == "__main__":
    unittest.main()
