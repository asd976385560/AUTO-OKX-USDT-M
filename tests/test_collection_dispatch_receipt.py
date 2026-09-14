# -*- coding: utf-8 -*-
"""A successful collector must retain a blocked dispatch handoff.

All databases and logs are temporary; subprocesses are intercepted. The real
pause/dry-run/isolation gates are exercised without invoking a dispatcher.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collectors"))
import _dispatch_nudge as nudge
import collect_cycle
import fast_collect


class CollectionDispatchReceiptTests(unittest.TestCase):
    CYCLE = "2026-09-04T11:30"

    @staticmethod
    def success_step(name, *_args, **_kwargs):
        return {"name": name, "ok": True, "rc": 0, "dur_s": 0.01,
                "payload": {}, "stderr_tail": ""}

    def test_successful_collection_retains_paused_or_unreadable_dispatch(self):
        for cron_state in ("disabled", "missing_database", "missing_job"):
            with self.subTest(cron_state=cron_state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                db_root = root / "db"
                state_db = root / "cron.sqlite"
                if cron_state != "missing_database":
                    with closing(sqlite3.connect(state_db)) as con, con:
                        con.execute("CREATE TABLE cron_jobs(name TEXT, enabled INTEGER)")
                        if cron_state == "disabled":
                            con.execute("INSERT INTO cron_jobs VALUES('okx-dispatcher',0)")
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch.object(nudge, "_STATE_DB", str(state_db)),
                    mock.patch.object(nudge, "_DB_ROOT", str(db_root)),
                    mock.patch.object(fast_collect, "_nudge_mod", nudge),
                    mock.patch.object(fast_collect, "run_step", side_effect=self.success_step),
                    mock.patch.object(sys, "argv", ["fast_collect.py", "--db-root", str(db_root), "--cycle", self.CYCLE]),
                    mock.patch.object(nudge.subprocess, "Popen") as spawn,
                    redirect_stdout(stdout),
                ):
                    rc = fast_collect.main()
                result = json.loads(stdout.getvalue())
                self.assertEqual(0, rc)
                self.assertTrue(result["ok"])
                self.assertEqual({"nudged": False, "reason": "cron_disabled_or_unreadable"}, result["dispatch"])
                spawn.assert_not_called()
                with closing(sqlite3.connect((db_root / "ledger.db").as_uri() + "?mode=ro", uri=True)) as con:
                    self.assertIn(con.execute("SELECT status FROM collection_runs WHERE cycle_id=? AND source='fast'", (self.CYCLE,)).fetchone()[0], ("ok", "degraded"))
                    self.assertEqual(0, con.execute("SELECT count(*) FROM stage_dispatch").fetchone()[0])
                self.assertEqual(cron_state != "missing_database", state_db.exists())

    def test_guard_refusals_never_spawn(self):
        cases = [
            ({"OKX_TRIGGER_DRYRUN": "0"}, False, True, ["ok"], "dryrun_env"),
            ({"OKX_DISPATCH_NUDGE": "0"}, False, True, ["ok"], "disabled"),
            ({}, True, True, ["ok"], "dry_collect"),
            ({}, False, False, ["ok"], "non_production_db_root"),
            ({}, False, True, ["error"], "no_done_status"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for env, dry, same_root, statuses, reason in cases:
                with self.subTest(reason=reason), mock.patch.dict(os.environ, env, clear=True), mock.patch.object(nudge, "_DB_ROOT", str(root)), mock.patch.object(nudge.subprocess, "Popen") as spawn:
                    result = nudge.nudge_from_collector("test", root if same_root else root / "other", statuses, dry_collect=dry)
                    self.assertEqual({"nudged": False, "reason": reason}, result)
                    spawn.assert_not_called()

    def test_quarter_and_hourly_logs_keep_blocked_handoff_without_recollection(self):
        for tier, cycle in (("quarter", self.CYCLE), ("hourly", "2026-09-04T12:00")):
            with self.subTest(tier=tier), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                refusal = {"nudged": False, "reason": "cron_disabled_or_unreadable"}

                def fake_step(name, *_args, **_kwargs):
                    step = self.success_step(name)
                    if name == "fast":
                        step["payload"] = {"status": "ok", "dispatch": refusal}
                    if name == "slow":
                        step["payload"] = {"status_slow": "ok", "status_regime": "ok", "dispatch": {"nudged": False, "reason": "deferred_to_collect_cycle"}}
                    return step

                argv = ["collect_cycle.py", "--tier", tier, "--db-root", str(root / "db"), "--guard-dir", str(root / "guards"), "--log-dir", str(root / "logs")]
                with (
                    mock.patch.object(collect_cycle.ledger, "cycle_id_for", return_value=cycle),
                    mock.patch.object(collect_cycle, "_is_production_db_root", return_value=True),
                    mock.patch.object(collect_cycle, "run_step", side_effect=fake_step) as steps,
                    mock.patch.object(collect_cycle, "_nudge_mod", mock.Mock(nudge_from_collector=mock.Mock(return_value=refusal))),
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(nudge.subprocess, "Popen") as spawn,
                    redirect_stdout(io.StringIO()) as stdout,
                ):
                    self.assertEqual(0, collect_cycle.main())
                    result = json.loads(stdout.getvalue())
                    self.assertTrue(result["ok"])
                    self.assertEqual([], result["failed"])
                    self.assertTrue(any("cron_disabled_or_unreadable" in w for w in result["warnings"]))
                    if tier == "hourly":
                        self.assertTrue(any("hourly" in w and "cron_disabled_or_unreadable" in w for w in result["warnings"]))
                    log = next((root / "logs").glob("*.jsonl"))
                    record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
                    self.assertEqual(refusal, record["steps"][0]["dispatch"])
                    self.assertEqual(result["warnings"], record["warnings"])
                    # Collection success still dedupes: the fallback must not
                    # collect or nudge again merely because dispatch was paused.
                    steps.reset_mock()
                    stdout.seek(0)
                    stdout.truncate()
                    self.assertEqual(0, collect_cycle.main())
                    self.assertEqual("duplicate_completed", json.loads(stdout.getvalue())["duplicate_skip"])
                    steps.assert_not_called()
                    spawn.assert_not_called()

    def test_dispatch_warning_handles_success_missing_receipt_and_spawn_failure(self):
        self.assertEqual([], collect_cycle._dispatch_warnings({"nudged": True, "reason": "ok"}, "fast"))
        for receipt in (None, {"nudged": False, "reason": "spawn_failed: blocked"}, {"nudged": False, "reason": "module_unavailable"}):
            with self.subTest(receipt=receipt):
                warning = collect_cycle._dispatch_warnings(receipt, "fast")
                self.assertEqual(1, len(warning))
                self.assertIn("unconfirmed", warning[0])
                self.assertIn("no retry", warning[0])


if __name__ == "__main__":
    unittest.main()
