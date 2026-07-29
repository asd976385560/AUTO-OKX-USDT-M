# -*- coding: utf-8 -*-
"""Regression tests for writer ownership and fast-collection status."""
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
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))

import fast_collect  # noqa: E402

class WriterOwnershipTests(unittest.TestCase):
    def test_fast_path_has_one_snapshot_writer(self) -> None:
        fast = (ROOT / "collectors" / "fast_collect.py").read_text(encoding="utf-8")
        collect_data = (ROOT / "scripts" / "collect_data.py").read_text(
            encoding="utf-8"
        )
        demo_check = (ROOT / "scripts" / "demo_account_check.py").read_text(
            encoding="utf-8"
        )
        self_review = (ROOT / "scripts" / "self_review.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"demo_position_check"', fast)
        self.assertIn('"--profile", "demo"', fast)
        self.assertLess(
            fast.index("steps.append(demo_pos_step)"),
            fast.index("status = _collection_status(steps)"),
        )
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
        self.assertNotIn("INSERT INTO account_snapshots", demo_check)
        self.assertNotIn("UPDATE drill_trades", demo_check)
        self.assertNotIn("INSERT INTO drill_trades", demo_check)
        self.assertNotIn("INSERT OR REPLACE INTO daily_reports", self_review)
        self.assertNotIn("INSERT INTO missed_opportunities", self_review)


class FastCollectionStatusTests(unittest.TestCase):
    @staticmethod
    def _steps(**overrides: bool) -> list[dict]:
        defaults = {
            "collect_data": True,
            "market_features": True,
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
        self.assertEqual(
            "error",
            fast_collect._collection_status(
                self._steps(demo_position_check=False)
            ),
        )

    def test_only_enrichment_may_degrade(self) -> None:
        self.assertEqual(
            "degraded",
            fast_collect._collection_status(
                self._steps(market_features=False)
            ),
        )
        self.assertEqual(
            "ok",
            fast_collect._collection_status(
                self._steps(demo_account_check=False)
            ),
        )

    def test_main_records_error_when_demo_snapshot_writer_fails(self) -> None:
        def fake_run(name, _script, _args, _timeout):
            ok = name != "demo_position_check"
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
