# -*- coding: utf-8 -*-
"""Regression tests for public writer ownership and collection status."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COLLECTORS = ROOT / "collectors"
for _p in (SCRIPTS, COLLECTORS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fast_collect  # noqa: E402


class WriterOwnershipTests(unittest.TestCase):
    def test_fast_path_has_one_snapshot_writer(self) -> None:
        fast = (ROOT / "collectors" / "fast_collect.py").read_text(encoding="utf-8")
        collect_data = (ROOT / "scripts" / "collect_data.py").read_text(
            encoding="utf-8"
        )
        self_review = (ROOT / "scripts" / "self_review.py").read_text(
            encoding="utf-8"
        )

        # 唯一快照 writer 契约（live）：只有 jobb_live_account_check 写
        # account_snapshots / position_snapshots，collect_data 不得旁路写入。
        self.assertIn('"live_account_check"', fast)
        self.assertIn("jobb_live_account_check.py", fast)
        self.assertIn('"contract_statistics"', fast)
        self.assertIn('"--contract-stats-only"', fast)
        self.assertIn('"official_positioning"', fast)
        self.assertIn('collect_positioning_current.py', fast)
        # 2026-08-06 demo 全量下线：快采不得再**调用**任何 demo 步骤。
        # 断言针对调用形态而非文件名字样——注释里提历史沿革是允许的。
        self.assertNotIn('run_step("demo_position_check"', fast)
        self.assertNotIn('run_step("demo_account_check"', fast)
        self.assertNotIn('SCRIPTS / "demo_account_check.py"', fast)
        self.assertNotIn('"--profile", "demo"', fast)
        self.assertNotIn("--legacy-account-writes", fast)
        self.assertNotIn("--legacy-account-writes", collect_data)
        self.assertNotIn("account_con = open_db", collect_data)
        self.assertNotIn("def collect_account(", collect_data)
        self.assertNotIn("INSERT OR REPLACE INTO account_snapshots", collect_data)
        self.assertNotIn("INSERT OR REPLACE INTO position_snapshots", collect_data)
        self.assertNotIn(
            "update_state(state_path, ts_start, ticker_snapshot, "
            "cross_snapshot, account_snapshot",
            collect_data,
        )
        # demo_account_check.py 的三条「不得写库」断言随该脚本一并下线（Phase 2 删除）。
        self.assertNotIn("INSERT OR REPLACE INTO daily_reports", self_review)
        self.assertNotIn("INSERT INTO missed_opportunities", self_review)


class FastCollectionStatusTests(unittest.TestCase):
    @staticmethod
    def _steps(**overrides: bool) -> list[dict]:
        defaults = {
            "collect_data": True,
            "market_features": True,
            "contract_statistics": True,
            "official_positioning": True,
            "live_account_check": True,
            "demo_account_check": True,
            "demo_position_check": True,
        }
        defaults.update(overrides)
        return [
            {"name": name, "ok": ok}
            for name, ok in defaults.items()
        ]

    def test_required_account_writers_fail_closed(self) -> None:
        self.assertEqual(
            "error",
            fast_collect._collection_status(
                self._steps(live_account_check=False)
            ),
        )

    def test_demo_snapshot_is_no_longer_a_gate_source(self) -> None:
        """2026-08-06 demo 全量下线：demo 快照曾是采集 gate 必需源，一旦停写会让
        `_collection_ready()` 永久 False、unified live 再也不派发。解除后 demo
        侧任何状态都不得再影响 live 派发。"""
        self.assertEqual(
            "ok",
            fast_collect._collection_status(
                self._steps(demo_position_check=False, demo_account_check=False)
            ),
        )

    def test_ticker_below_goal_quality_marks_collect_data_degraded(self) -> None:
        steps = self._steps()
        for step in steps:
            if step["name"] == "collect_data":
                step["payload"] = {"degraded": True}
        self.assertEqual("degraded", fast_collect._collection_status(steps))

    def test_only_enrichment_may_degrade(self) -> None:
        self.assertEqual(
            "degraded",
            fast_collect._collection_status(
                self._steps(market_features=False)
            ),
        )
        self.assertEqual(
            "degraded",
            fast_collect._collection_status(
                self._steps(contract_statistics=False)
            ),
        )
        self.assertEqual(
            "degraded",
            fast_collect._collection_status(
                self._steps(official_positioning=False)
            ),
        )
        self.assertEqual(
            "ok",
            fast_collect._collection_status(
                self._steps(demo_account_check=False)
            ),
        )

    def test_collect_data_partial_payload_is_visible_as_degraded(self) -> None:
        steps = self._steps()
        for step in steps:
            if step["name"] == "collect_data":
                step["payload"] = {
                    "degraded": True,
                    "quality": {"candle_coverage": 0.91},
                }
        self.assertEqual("degraded", fast_collect._collection_status(steps))

    def test_contract_statistics_warning_does_not_override_passed_99_gate(self) -> None:
        steps = self._steps()
        for step in steps:
            if step["name"] == "contract_statistics":
                step["payload"] = {
                    "degraded": False,
                    "direct_coverage_rate": 427 / 429,
                    "coverage_rate": 427 / 429,
                    "warnings": [
                        "contract statistics carry-forward unresolved=2"
                    ],
                }
        self.assertEqual("ok", fast_collect._collection_status(steps))
        contract_step = next(
            step for step in steps if step["name"] == "contract_statistics")
        warning = fast_collect._step_degraded_warning(contract_step)
        self.assertIn("unresolved=2", warning)

    def test_positioning_degraded_warning_keeps_failure_counts(self) -> None:
        step = {
            "name": "official_positioning",
            "ok": False,
            "rc": 1,
            "payload": {
                "degraded": True,
                "positioning_coverage_rate": 237 / 431,
                "retry": {
                    "initial_invalid_symbols": 431,
                    "retry_recovered_symbols": 237,
                    "final_failed_symbols": 194,
                },
            },
        }
        warning = fast_collect._step_degraded_warning(step)
        self.assertIn("coverage=0.549884", warning)
        self.assertIn("final_failed=194", warning)

    def test_main_records_error_when_live_snapshot_writer_fails(self) -> None:
        # 原用 demo_position_check 打这条链；2026-08-06 demo 下线后它不再是必需源，
        # 改打 live_account_check——fail-closed 机制本身不变，仍须覆盖。
        def fake_run(name, _script, _args, _timeout):
            ok = name != "live_account_check"
            return {
                "name": name,
                "ok": ok,
                "rc": 0 if ok else 1,
                "dur_s": 0.0,
                "payload": {},
                "stderr_tail": "" if ok else "simulated writer failure",
            }

        with tempfile.TemporaryDirectory() as td:
            nudge = mock.Mock()
            argv = [
                "fast_collect.py",
                "--db-root",
                td,
                "--cycle",
                "TEST-FAIL-CLOSED",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(fast_collect, "run_step", side_effect=fake_run),
                mock.patch.object(fast_collect.ledger, "init_ledger"),
                mock.patch.object(
                    fast_collect.ledger, "record_collection"
                ) as record,
                mock.patch.object(fast_collect, "_nudge_mod", nudge),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(1, fast_collect.main())

            self.assertEqual("error", record.call_args.args[3])
            self.assertEqual(
                ["error"],
                nudge.nudge_from_collector.call_args.args[2],
            )


if __name__ == "__main__":
    unittest.main()
