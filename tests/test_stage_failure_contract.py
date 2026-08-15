# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COLLECTORS = ROOT / "collectors"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))

import stage_failure_contract as contract  # noqa: E402
import push_pipeline  # noqa: E402
import trigger_agent  # noqa: E402


CST = timezone(timedelta(hours=8))


class StageFailureContractTests(unittest.TestCase):
    def _write(self, root: Path, cycle: str, **updates) -> None:
        payload = {
            "stage": "live",
            "cycle_id": cycle,
            "mode": "unified",
            "status": "failed",
            "started_at": "2026-08-13 04:05:00",
            "finished_at": "2026-08-13 04:12:00",
            "child_returncode": 1,
            "returncode": 1,
            "failure_kind": "agent_idle_timeout",
            "profile_lease_released": True,
            "provider": "must-not-be-emitted",
            "model": "must-not-be-emitted",
            "prompt": "must-not-be-emitted",
        }
        payload.update(updates)
        contract.status_path(cycle, root).write_text(
            json.dumps(payload), encoding="utf-8")

    def test_future_terminal_failure_is_redacted(self):
        cycle = "2026-08-13T04:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, cycle)
            result = contract.load_live_failure(
                cycle,
                status_dir=root,
                now=datetime(2026, 8, 13, 4, 13, tzinfo=CST),
            )
        self.assertEqual(result["failure_kind"], "agent_idle_timeout")
        self.assertEqual(result["production_database_writes"], 0)
        self.assertEqual(result["orders_placed"], 0)
        serialized = json.dumps(result).lower()
        for forbidden in ("provider", "model", "prompt", "must-not-be-emitted"):
            self.assertNotIn(forbidden, serialized)

    def test_history_running_or_unreleased_is_rejected(self):
        now = datetime(2026, 8, 13, 4, 13, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "2026-08-13T03:45")
            self.assertIsNone(contract.load_live_failure(
                "2026-08-13T03:45", status_dir=root, now=now))

            cycle = "2026-08-13T04:00"
            self._write(root, cycle, status="running")
            self.assertIsNone(contract.load_live_failure(
                cycle, status_dir=root, now=now))
            self._write(root, cycle, profile_lease_released=False)
            self.assertIsNone(contract.load_live_failure(
                cycle, status_dir=root, now=now))

    def test_mismatched_identity_and_future_terminal_are_rejected(self):
        cycle = "2026-08-13T04:00"
        now = datetime(2026, 8, 13, 4, 13, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, cycle, cycle_id="2026-08-13T04:15")
            self.assertIsNone(contract.load_live_failure(
                cycle, status_dir=root, now=now))
            self._write(
                root, cycle,
                started_at="2026-08-13 04:14:00",
                finished_at="2026-08-13 04:20:00",
            )
            self.assertIsNone(contract.load_live_failure(
                cycle, status_dir=root, now=now))

    def test_terminal_started_before_cycle_is_rejected(self):
        cycle = "2026-08-13T04:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root,
                cycle,
                started_at="2026-08-13 03:59:59",
                finished_at="2026-08-13 04:01:00",
            )
            self.assertIsNone(contract.load_live_failure(
                cycle,
                status_dir=root,
                now=datetime(2026, 8, 13, 4, 2, tzinfo=CST),
            ))

    def test_future_report_barrier_requires_safe_post_agent_contract(self):
        cycle = "2026-08-14T19:00"
        barrier = {
            "schema_version": 1,
            "required": True,
            "profile": "live",
            "cycle_id": cycle,
            "contract_version": 1,
            "request_id": "a" * 32,
            "status": "applied",
            "rc": 0,
            "applied": True,
            "blocking": False,
            "p0": False,
            "contract_valid": True,
            "report_safe": True,
            "started_at": "2026-08-14 19:08:01",
            "finished_at": "2026-08-14 19:08:03",
            "findings_count": 0,
            "healed_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root,
                cycle,
                status="succeeded",
                started_at="2026-08-14 19:01:00",
                finished_at="2026-08-14 19:08:00",
                child_returncode=0,
                returncode=0,
                profile_lease_released=True,
                report_reconcile_barrier=barrier,
            )
            result = contract.load_live_report_barrier(
                cycle, status_dir=root)
            self.assertTrue(result["report_safe"])
            self.assertTrue(result["applied"])

            unsafe = {**barrier, "status": "needs_human", "rc": 1,
                      "blocking": True, "report_safe": False}
            self._write(
                root,
                cycle,
                status="succeeded",
                started_at="2026-08-14 19:01:00",
                finished_at="2026-08-14 19:08:00",
                child_returncode=0,
                returncode=0,
                profile_lease_released=True,
                report_reconcile_barrier=unsafe,
            )
            self.assertIsNone(contract.load_live_report_barrier(
                cycle, status_dir=root))

    def test_future_failure_report_waits_for_report_barrier(self):
        cycle = "2026-08-14T19:00"
        now = datetime(2026, 8, 14, 19, 12, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root,
                cycle,
                started_at="2026-08-14 19:01:00",
                finished_at="2026-08-14 19:08:00",
            )
            self.assertIsNone(contract.load_live_failure(
                cycle, status_dir=root, now=now))

            barrier = {
                "schema_version": 1, "required": True,
                "profile": "live", "cycle_id": cycle,
                "contract_version": 1, "request_id": "b" * 32,
                "status": "ok", "rc": 0, "applied": False,
                "blocking": False, "p0": False,
                "contract_valid": True, "report_safe": True,
                "started_at": "2026-08-14 19:08:01",
                "finished_at": "2026-08-14 19:08:02",
                "findings_count": 0, "healed_count": 0,
            }
            self._write(
                root,
                cycle,
                started_at="2026-08-14 19:01:00",
                finished_at="2026-08-14 19:08:00",
                report_reconcile_barrier=barrier,
            )
            result = contract.load_live_failure(
                cycle, status_dir=root, now=now)
            self.assertTrue(
                result["report_reconcile_barrier"]["report_safe"])

    def _collection_fixture(
        self,
        root: Path,
        cycle: str,
        *,
        ready: bool = False,
        live_dispatched: bool = False,
    ) -> tuple[Path, Path]:
        db_root = root / "db"
        log_root = root / "collect"
        db_root.mkdir()
        log_root.mkdir()
        connection = sqlite3.connect(db_root / "ledger.db")
        try:
            connection.executescript(
                "CREATE TABLE collection_runs(cycle_id TEXT,source TEXT,status TEXT);"
                "CREATE TABLE stage_dispatch(cycle_id TEXT,stage TEXT);"
                "CREATE TABLE stage_profile_leases(profile TEXT,cycle_id TEXT);"
            )
            if ready:
                connection.execute(
                    "INSERT INTO collection_runs VALUES(?,?,?)",
                    (cycle, "fast", "ok"),
                )
            if live_dispatched:
                connection.execute(
                    "INSERT INTO stage_dispatch VALUES(?,?)", (cycle, "live"))
            connection.commit()
        finally:
            connection.close()
        connection = sqlite3.connect(db_root / "analysis.db")
        try:
            connection.execute("CREATE TABLE analysis_runs(cycle_id TEXT)")
            connection.commit()
        finally:
            connection.close()
        connection = sqlite3.connect(db_root / "live_trades.db")
        try:
            connection.executescript(
                "CREATE TABLE trade_cycles(cycle_id TEXT);"
                "CREATE TABLE trades(cycle_id TEXT);"
            )
            connection.commit()
        finally:
            connection.close()
        payload = {
            "ok": False,
            "tier": "quarter",
            "cycle": cycle,
            "ts": "2026-08-15 13:15:01",
            "latency_ms": 120000,
            "failed": ["fast"],
            "warnings": ["provider secret must stay redacted"],
            "steps": [{
                "name": "fast", "ok": False, "rc": 1,
                "error": "raw TLS failure must stay redacted",
            }],
        }
        (log_root / "collect_cycle_20260815.jsonl").write_text(
            json.dumps(payload) + "\n", encoding="utf-8")
        return db_root, log_root

    def test_collection_failure_requires_gate_and_execution_path_absence(self):
        cycle = "2026-08-15T13:15"
        now = datetime(2026, 8, 15, 13, 20, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_root, log_root = self._collection_fixture(root, cycle)
            result = contract.load_collection_failure(
                cycle,
                db_root=db_root,
                collect_log_dir=log_root,
                now=now,
            )
        self.assertEqual(result["stage"], "collection")
        self.assertEqual(result["failure_kind"], "collection_gate_failed")
        self.assertEqual(result["missing_required_sources"], ["fast"])
        self.assertTrue(result["report_reconcile_barrier"]["report_safe"])
        serialized = json.dumps(result).lower()
        self.assertNotIn("raw tls", serialized)
        self.assertNotIn("provider secret", serialized)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_root, log_root = self._collection_fixture(
                root, cycle, ready=True)
            self.assertIsNone(contract.load_collection_failure(
                cycle, db_root=db_root, collect_log_dir=log_root, now=now))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_root, log_root = self._collection_fixture(
                root, cycle, live_dispatched=True)
            self.assertIsNone(contract.load_collection_failure(
                cycle, db_root=db_root, collect_log_dir=log_root, now=now))

    def test_collection_failure_is_future_only_and_single_terminal(self):
        cycle = "2026-08-15T13:15"
        now = datetime(2026, 8, 15, 13, 20, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_root, log_root = self._collection_fixture(root, cycle)
            self.assertIsNone(contract.load_collection_failure(
                cycle,
                db_root=db_root,
                collect_log_dir=log_root,
                now=now,
                activation_cycle="2026-08-15T13:30",
            ))
            path = log_root / "collect_cycle_20260815.jsonl"
            path.write_text(
                path.read_text(encoding="utf-8")
                + path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self.assertIsNone(contract.load_collection_failure(
                cycle, db_root=db_root, collect_log_dir=log_root, now=now))


class FailureReportPlumbingTests(unittest.TestCase):
    CYCLE = "2026-08-13T04:00"

    def test_trigger_passes_only_failure_report_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            with (
                mock.patch.object(trigger_agent, "LOG_DIR", log_dir),
                mock.patch.dict(os.environ, {"OKX_TRIGGER_DRYRUN": "1"}),
            ):
                trigger_agent._fire_push_script(
                    self.CYCLE, mode="failure_report")
            text = (log_dir / "push-20260813-0400.log").read_text(
                encoding="utf-8")
        self.assertIn("--upstream-failure-report", text)
        self.assertNotIn("failure_kind", text)

    def test_pipeline_revalidates_failure_before_builder(self):
        failure = {
            "stage": "live", "cycle_id": self.CYCLE,
            "status": "failed", "returncode": 1,
            "profile_lease_released": True,
        }
        captured = {}

        class Builder:
            @staticmethod
            def build(db_root, cycle, *, upstream_failure=None):
                captured["db_root"] = db_root
                captured["cycle"] = cycle
                captured["failure"] = upstream_failure
                raise RuntimeError("stop after contract assertion")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(push_pipeline, "WORK", root / "work"),
                mock.patch.object(push_pipeline, "REPORT_DIR", root / "reports"),
                mock.patch.object(push_pipeline, "RUNLOG", root / "runlog.jsonl"),
                mock.patch.object(
                    push_pipeline, "require_upstream_failure", return_value=failure,
                ) as require,
                mock.patch.object(push_pipeline, "_load_build", return_value=Builder),
                mock.patch.object(
                    push_pipeline, "_finish", side_effect=lambda rep: rep),
            ):
                result = push_pipeline.run(
                    self.CYCLE,
                    str(root / "db"),
                    no_send=True,
                    upstream_failure_report=True,
                )
        require.assert_called_once_with(
            self.CYCLE,
            db_root=str(root / "db"),
            status_dir=push_pipeline.STAGE_STATUS_DIR,
        )
        self.assertEqual(captured["failure"], failure)
        self.assertEqual(result["report_mode"], "upstream_failure")
        self.assertEqual(result["fatal"], "build_failed")


if __name__ == "__main__":
    unittest.main()
