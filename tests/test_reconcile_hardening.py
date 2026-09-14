# -*- coding: utf-8 -*-
"""Isolated regressions for the 2026-07-27 reconciliation incident.

No exchange calls, production database writes, Agent launches, or QQ pushes.
"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "collectors", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import stage_runner  # noqa: E402
import collection_monitor  # noqa: E402
import check_trader_docs_sync  # noqa: E402
import daily_report_writer  # noqa: E402
import live_reconcile_monitor  # noqa: E402
import reconcile_exchange_closes  # noqa: E402
import reconcile_daily  # noqa: E402
import trade_report_stats  # noqa: E402
import trades_writer  # noqa: E402


TRADE_SCHEMA = """
CREATE TABLE trade_cycles(
  cycle_id TEXT PRIMARY KEY, ts TEXT NOT NULL, mode TEXT, decision TEXT,
  n_orders INTEGER DEFAULT 0, equity REAL, note TEXT, raw TEXT
);
CREATE TABLE trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, ts TEXT NOT NULL,
  symbol TEXT NOT NULL, action TEXT NOT NULL, side TEXT, sz REAL,
  fill_px REAL, lev REAL, margin REAL, notional REAL, score_total INTEGER,
  reasoning TEXT, deviation TEXT, degradation TEXT, pnl REAL, raw TEXT
);
"""


def _valid_card() -> dict:
    return {
        "direction_evidence": ["isolated test"],
        "opposing_evidence": ["counter"],
        "execution_conditions": {"status": "ready"},
        "invalidation_point": {"condition": "invalid"},
        "risk_reward": {"summary": "bounded"},
        "portfolio_impact": {"summary": "isolated"},
        "historical_experience": {
            "matched_wins": [],
            "matched_losses": [],
            "missed_opportunities": [],
            "usage": "none",
            "reason": "no comparable sample",
        },
        "agent_judgement": "isolated test",
        "reference_overrides": [],
    }


def _create_trade_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(TRADE_SCHEMA)
        con.commit()
    finally:
        con.close()


def _create_analysis_db(
    path: Path,
    cycle: str,
    status: str = "ok",
    ts: str = "2026-07-27 02:50:00",
) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE analysis_runs("
            "cycle_id TEXT PRIMARY KEY,status TEXT,ts TEXT,mode TEXT)")
        con.execute(
            "INSERT INTO analysis_runs VALUES(?,?,?,?)",
            (cycle, status, ts, "full"),
        )
        con.commit()
    finally:
        con.close()


def _create_ledger_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE execution_intents("
            "profile TEXT,cycle_id TEXT,symbol TEXT,action TEXT,side TEXT,"
            "request_fingerprint TEXT,request_json TEXT,state TEXT,"
            "reserved_at TEXT,updated_at TEXT,submitted_at TEXT,"
            "completed_at TEXT,ord_id TEXT,receipt_json TEXT,error TEXT,"
            "PRIMARY KEY(profile,cycle_id,symbol,action,side))")
        con.commit()
    finally:
        con.close()


class HistoricalReplayTests(unittest.TestCase):
    def test_late_hold_is_refused_without_writing_but_side_effects_stay_writable(self):
        cycle = "2026-08-15T09:15"
        hold = {
            "cycle_id": cycle,
            "ts": "2026-08-15 09:28:01",
            "mode": "live",
            "status": "ok",
            "decision": "hold",
            "action_taken": "HOLD",
            "n_orders": 0,
            "trades": [],
            "decision_protocol": "decision_card_v1",
            "decision_card": _valid_card(),
            "equity": 1000.0,
        }
        late = datetime(2026, 8, 15, 9, 28, 1,
                        tzinfo=trades_writer.CST)
        before = datetime(2026, 8, 15, 9, 27, 59,
                          tzinfo=trades_writer.CST)

        self.assertIsNone(
            trades_writer._late_no_side_effect_refusal(hold, now=before))
        refusal = trades_writer._late_no_side_effect_refusal(hold, now=late)
        self.assertEqual(
            "cycle_deadline_exceeded_no_side_effect_terminal",
            refusal["error"],
        )
        self.assertEqual(0, refusal["production_database_writes"])

        protection = {
            **hold,
            "action_taken": "ADJUST_PROTECTION",
            "protection_change": {"requested_sl": 9.0},
        }
        self.assertIsNone(
            trades_writer._late_no_side_effect_refusal(
                protection, now=late))
        uncertain_error = {**hold, "decision": "error", "status": "error"}
        self.assertIsNone(
            trades_writer._late_no_side_effect_refusal(
                uncertain_error, now=late))

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            with mock.patch.object(
                    trades_writer, "_now_cst_dt", return_value=late):
                result = trades_writer.commit_receipt(
                    hold, "live", db_path=db, nudge=False)
            con = sqlite3.connect(db)
            try:
                count = con.execute(
                    "SELECT COUNT(*) FROM trade_cycles").fetchone()[0]
            finally:
                con.close()
        self.assertTrue(result["refused"])
        self.assertEqual(0, count)

    def test_v4_on_time_business_terminal_can_be_persisted_after_870(self):
        cycle = "2026-08-21T18:00"
        hold = {
            "cycle_id": cycle,
            "mode": "live",
            "status": "ok",
            "decision": "hold",
            "action_taken": "HOLD",
            "n_orders": 0,
            "trades": [],
            "business_terminal": {
                "schema_version": 1,
                "cycle_id": cycle,
                "status": "completed",
                "completed_at_cst": "2026-08-21 18:14:29",
            },
        }
        persisted_late = datetime(
            2026, 8, 21, 18, 14, 50, tzinfo=trades_writer.CST)
        self.assertIsNone(trades_writer._late_no_side_effect_refusal(
            hold, now=persisted_late))
        hold["business_terminal"]["completed_at_cst"] = (
            "2026-08-21 18:14:30")
        refusal = trades_writer._late_no_side_effect_refusal(
            hold, now=persisted_late)
        self.assertEqual(
            "cycle_deadline_exceeded_no_side_effect_terminal",
            refusal["error"],
        )
    def test_commit_receipt_writes_in_same_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = {
                "cycle_id": "TEST-2026-07-27T02:45",
                "ts": "2026-07-27 02:53:23",
                "decision": "traded",
                "status": "ok",
                "decision_protocol": "decision_card_v1",
                "decision_card": _valid_card(),
                "equity": 1000.0,
                "trades": [{
                    "symbol": "WLD-USDT-SWAP", "action": "close",
                    "side": "short", "sz": 400, "fill_px": 0.3506,
                    "fill_sz": 400, "fill_source": "fills",
                    "fill_ts": "2026-07-27 02:53:20",
                    "ts_source": "fills.fillTime",
                    "lev": 10, "pnl": -2.28, "reasoning": "isolated test",
                    "raw": {"ordId": "TEST-ORDID"},
                }],
            }
            with mock.patch.object(
                    trades_writer, "write_experiences",
                    return_value={"exp": 1}):
                result = trades_writer.commit_receipt(
                    payload, "live", db_path=db, nudge=False)
            con = sqlite3.connect(db)
            try:
                row = con.execute(
                    "SELECT n_orders FROM trade_cycles WHERE cycle_id=?",
                    ("TEST-2026-07-27T02:45",),
                ).fetchone()
            finally:
                con.close()
        self.assertTrue(result["ok"])
        self.assertEqual(result["exp"], 1)
        self.assertEqual(row[0], 1)

    def test_maintenance_replay_does_not_backfill_current_equity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = {
                "cycle_id": "2026-07-27T02:45",
                "ts": "2026-07-27 02:53:23",
                "decision": "traded",
                "trades": [{
                    "symbol": "WLD-USDT-SWAP", "action": "close",
                    "side": "short", "sz": 400, "fill_px": 0.3506,
                    "lev": 10, "pnl": -2.28,
                    "raw": {"ordId": "3777879978121388032"},
                }],
                "_profile": "live",
            }
            with mock.patch.object(
                    trades_writer, "_equity_snapshot_fallback",
                    return_value=(1061.77774024826, "2026-07-27 10:00:47")):
                result = trades_writer.maintenance_write_trades(
                    payload,
                    db,
                    trusted_timestamp=payload["ts"],
                    preserve_equity_none=True,
                )
            self.assertTrue(result["ok"])
            con = sqlite3.connect(db)
            try:
                equity = con.execute(
                    "SELECT equity FROM trade_cycles WHERE cycle_id=?",
                    ("2026-07-27T02:45",),
                ).fetchone()[0]
            finally:
                con.close()
            self.assertIsNone(equity)


class ReconcileReportContextTests(unittest.TestCase):
    def test_large_terminal_raw_preserves_bounded_report_context(self):
        card = _valid_card()
        raw = {
            "cycle_id": "2026-08-15T04:00",
            "decision_protocol": "decision_card_v1",
            "decision_card": card,
            "live_facts": {
                "schema_version": 1,
                "source": "okx_private_api",
                "cycle_id": "2026-08-15T04:00",
                "profile": "live",
                "status": "ok",
                "as_of": "2026-08-15 04:09:03",
                "position_truth_verified": True,
                "balance": {
                    "totalEq": 900.89,
                    "current_portfolio_imr_ratio": 0.4365,
                },
                "positions": [{
                    "instId": "APR-USDT-SWAP", "posSide": "long",
                    "contracts": 31, "avgPx": 0.5442,
                }],
                "errors": [],
                "exchange": {"oversized_raw": "x" * 25000},
            },
        }

        context = reconcile_exchange_closes._report_business_context(
            raw, "2026-08-15T04:00")

        self.assertEqual(context["decision_protocol"], "decision_card_v1")
        self.assertEqual(context["decision_card"], card)
        self.assertTrue(context["business_context_preserved"])
        self.assertEqual(
            context["live_facts"]["balance"]
            ["current_portfolio_imr_ratio"],
            0.4365,
        )
        self.assertNotIn("exchange", context["live_facts"])
        self.assertLess(len(json.dumps(context)), 10000)

    def test_completed_terminal_is_preserved_but_partial_cannot_be_upgraded(self):
        cycle = "2026-09-03T07:45"
        terminal = {
            "schema_version": 1,
            "cycle_id": cycle,
            "status": "completed",
            "completed_at_cst": "2026-09-03 07:53:08",
            "clock_stop": "analysis_judgment_trade_completed_before_persistence",
        }
        raw = {
            "status": "ok",
            "batch_status": "completed",
            "batch_ok": True,
            "runner_in_progress": False,
            "business_terminal": terminal,
            "facts_hash": "f" * 64,
            "plan_sha256": "a" * 64,
            "position_action_plan_hash": "p" * 64,
        }

        kept = reconcile_exchange_closes._report_business_context(raw, cycle)
        self.assertEqual(terminal, kept["business_terminal"])
        self.assertTrue(
            kept["business_terminal_preserved_after_reconcile"])
        self.assertEqual("f" * 64, kept["facts_hash"])

        for changes in (
            {"batch_status": "partial"},
            {"runner_in_progress": True},
            {"business_terminal": {**terminal, "cycle_id": "other"}},
            {"business_terminal": {
                **terminal, "completed_at_cst": "not-a-time"}},
        ):
            with self.subTest(changes=changes):
                invalid = {**raw, **changes}
                context = reconcile_exchange_closes._report_business_context(
                    invalid, cycle)
                self.assertNotIn("business_terminal", context)
                self.assertNotIn(
                    "business_terminal_preserved_after_reconcile", context)


class TraderDocContractTests(unittest.TestCase):
    def test_live_forced_flow_requires_same_process_commit(self):
        text = """
## RUN_OUTPUT
交易回执喂 writer：回执写 tmp 后 trades_writer.py --json-file <tmp 回执文件>
## STOP
"""
        problems = check_trader_docs_sync.live_money_path_problems(text)
        self.assertTrue(any("缺同进程" in item for item in problems))
        self.assertTrue(any("仍要求成交后分步调用" in item for item in problems))

    def test_live_forced_flow_accepts_same_process_commit(self):
        text = """
## RUN_OUTPUT
所有动作不论是否包含 OPEN/ADD，都必须通过 live_position_action_runner.py，
并在同一个固定 Python 进程内调用 commit_receipt(receipt, "live")。
## STOP
"""
        self.assertEqual(
            check_trader_docs_sync.live_money_path_problems(text), [])


class StageBusinessOutputTests(unittest.TestCase):
    def test_live_business_output_settle_recovers_writer_race(self):
        initial = {
            "ok": False,
            "failure_kind": "business_output_missing",
            "checks": [
                {"db": "analysis.db", "table": "analysis_runs",
                 "found": True},
                {"db": "live_trades.db", "table": "trade_cycles",
                 "found": False},
            ],
        }
        recovered = {
            "ok": True,
            "checks": [
                {"db": "analysis.db", "table": "analysis_runs",
                 "found": True},
                {"db": "live_trades.db", "table": "trade_cycles",
                 "found": True},
            ],
        }
        clock = mock.Mock(side_effect=[0.0, 0.0, 0.0, 0.25])
        sleeper = mock.Mock()
        with mock.patch.object(
                stage_runner, "verify_business_output",
                return_value=recovered) as verify:
            result, evidence = stage_runner._settle_late_live_business_output(
                "2026-08-15T09:30",
                "unified",
                initial,
                monotonic_fn=clock,
                sleep_fn=sleeper,
            )

        self.assertIs(result, recovered)
        self.assertTrue(evidence["recovered"])
        self.assertEqual(1, evidence["attempts"])
        sleeper.assert_called_once_with(0.25)
        verify.assert_called_once_with(
            "live", "2026-08-15T09:30", "unified")

    def test_live_business_output_settle_does_not_wait_without_analysis(self):
        initial = {
            "ok": False,
            "failure_kind": "business_output_missing",
            "checks": [
                {"db": "analysis.db", "table": "analysis_runs",
                 "found": False},
                {"db": "live_trades.db", "table": "trade_cycles",
                 "found": False},
            ],
        }
        result, evidence = stage_runner._settle_late_live_business_output(
            "2026-08-15T09:30", "unified", initial)
        self.assertIs(result, initial)
        self.assertIsNone(evidence)

    def test_live_child_budget_is_anchored_to_cycle_plus_thirteen_minutes(self):
        now = datetime(
            2026, 8, 15, 3, 5, 0, tzinfo=stage_runner.CST)
        guarded = mock.Mock(return_value=(0, "", "", False))
        with mock.patch.object(stage_runner._proc, "run_guarded", guarded):
            result = stage_runner._run_stage_child(
                "live", "2026-08-15T03:00", ["agent"], now=now)

        self.assertEqual(0, result["returncode"])
        self.assertFalse(result["timed_out"])
        self.assertEqual("2026-08-15 03:13:00", result["absolute_deadline_at"])
        self.assertEqual(480.0, result["budget_seconds"])
        self.assertEqual(480.0, guarded.call_args.kwargs["timeout"])

    def test_live_child_is_not_started_with_insufficient_cycle_budget(self):
        now = datetime(
            2026, 8, 15, 3, 12, 30, tzinfo=stage_runner.CST)
        guarded = mock.Mock()
        with mock.patch.object(stage_runner._proc, "run_guarded", guarded):
            result = stage_runner._run_stage_child(
                "live", "2026-08-15T03:00", ["agent"], now=now)

        self.assertEqual(stage_runner._proc.RC_TIMEOUT, result["returncode"])
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["started"])
        self.assertEqual(30.0, result["budget_seconds"])
        guarded.assert_not_called()

    def test_analysis_deadline_observer_rejects_missing_and_late_authority(self):
        cycle = "2026-08-15T21:45"
        deadline = datetime(
            2026, 8, 15, 21, 54, 30, tzinfo=stage_runner.CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tmp_root = root / "tmp"
            db_root = root / "db"
            tmp_root.mkdir()
            db_root.mkdir()
            missing = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=db_root,
                now_fn=lambda: deadline.timestamp(),
                enforce_analysis_deadline=True,
            )
            self.assertEqual(
                "analysis_deadline_exceeded:no_timely_analysis",
                missing(),
            )

            _create_analysis_db(
                db_root / "analysis.db",
                cycle,
                ts="2026-08-15 21:54:30",
            )
            late = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=db_root,
                now_fn=lambda: deadline.timestamp(),
                enforce_analysis_deadline=True,
            )
            self.assertEqual(
                "analysis_deadline_exceeded:late_analysis",
                late(),
            )

    def test_facts_cannot_appear_without_timely_analysis(self):
        cycle = "2026-08-15T21:45"
        now = datetime(2026, 8, 15, 21, 53, 0,
                       tzinfo=stage_runner.CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tmp_root = root / "tmp"
            db_root = root / "db"
            tmp_root.mkdir()
            db_root.mkdir()
            (tmp_root / "live_facts_2026-08-15T21-45.json").write_text(
                json.dumps({"facts_hash": "f" * 64}), encoding="utf-8")
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=db_root,
                now_fn=lambda: now.timestamp(),
                enforce_analysis_deadline=True,
            )
            reason = observer()
        self.assertEqual(
            "analysis_deadline_exceeded:facts_without_timely_analysis",
            reason,
        )

    def test_timely_analysis_allows_facts_at_deadline(self):
        cycle = "2026-08-15T21:45"
        deadline = datetime(
            2026, 8, 15, 21, 54, 30, tzinfo=stage_runner.CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tmp_root = root / "tmp"
            db_root = root / "db"
            tmp_root.mkdir()
            db_root.mkdir()
            _create_analysis_db(
                db_root / "analysis.db",
                cycle,
                ts="2026-08-15 21:54:29",
            )
            (tmp_root / "live_facts_2026-08-15T21-45.json").write_text(
                json.dumps({"facts_hash": "f" * 64}), encoding="utf-8")
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=db_root,
                now_fn=lambda: deadline.timestamp(),
                enforce_analysis_deadline=True,
            )
            reason = observer()
        self.assertIsNone(reason)

    def test_business_check_rejects_analysis_written_at_deadline(self):
        cycle = "2026-08-15T21:45"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_analysis_db(
                root / "analysis.db",
                cycle,
                ts="2026-08-15 21:54:30",
            )
            _create_trade_db(root / "live_trades.db")
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trade_cycles"
                    "(cycle_id,ts,mode,decision,n_orders,equity,note,raw) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-15 21:55:00", "live", "hold", 0,
                     1000.0, "", json.dumps({"batch_status": "completed"})),
                )
                con.commit()
            finally:
                con.close()
            result = stage_runner.verify_business_output(
                "live", cycle, "unified", db_root=root)
        self.assertFalse(result["ok"])
        self.assertIn("analysis_deadline_exceeded", result["error"])

    def test_live_child_timeout_uses_process_tree_guard(self):
        now = datetime(
            2026, 8, 15, 3, 5, 0, tzinfo=stage_runner.CST)
        guarded = mock.Mock(return_value=(124, "", "", True))
        abort_result = {
            "requested": True,
            "status": "aborted",
            "terminal_confirmed": True,
        }
        with mock.patch.object(stage_runner._proc, "run_guarded", guarded), \
                mock.patch.object(
                    stage_runner,
                    "_abort_gateway_session",
                    return_value=abort_result,
                ) as abort_gateway:
            result = stage_runner._run_stage_child(
                "live", "2026-08-15T03:00", ["agent"], now=now)

        self.assertEqual(124, result["returncode"])
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["started"])
        self.assertIn("process tree terminated", result["error"])
        self.assertEqual(abort_result, result["gateway_abort"])
        abort_gateway.assert_called_once_with("live", "2026-08-15T03:00")

    def test_guarded_process_generic_exception_terminates_started_tree(self):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        proc.communicate.side_effect = [
            RuntimeError("broken pipe"),
            ("partial-out", "partial-err"),
        ]
        terminated: list[int] = []

        def terminate(started_proc):
            terminated.append(started_proc.pid)
            started_proc.poll.return_value = 1

        with mock.patch.object(
                stage_runner._proc.subprocess, "Popen", return_value=proc), \
                mock.patch.object(
                    stage_runner._proc, "terminate_process_tree",
                    side_effect=terminate):
            rc, out, err, timed_out = stage_runner._proc.run_guarded(
                ["agent"], timeout=30)

        self.assertEqual(stage_runner._proc.RC_GUARD_ERROR, rc)
        self.assertFalse(timed_out)
        self.assertEqual("partial-out", out)
        self.assertIn("RuntimeError: broken pipe", err)
        self.assertIn("process tree terminated", err)
        self.assertEqual([4321], terminated)

    def test_live_nonzero_child_publishes_stopping_then_aborts(self):
        now = datetime(
            2026, 8, 15, 3, 5, 0, tzinfo=stage_runner.CST)
        events: list[str] = []

        def publish(_payload):
            events.append("stopping")

        def abort(_stage, _cycle):
            events.append("abort")
            return {"terminal_confirmed": True, "status": "no-active-run"}

        with mock.patch.object(
                stage_runner._proc, "run_guarded",
                return_value=(1, "", "cli disconnected", False)), \
                mock.patch.object(
                    stage_runner, "_abort_gateway_session", side_effect=abort):
            result = stage_runner._run_stage_child(
                "live", "2026-08-15T03:00", ["agent"], now=now,
                terminal_callback=publish)

        self.assertEqual(1, result["returncode"])
        self.assertTrue(result["started"])
        self.assertEqual("no-active-run", result["gateway_abort"]["status"])
        self.assertEqual(["stopping", "abort"], events)

    def test_live_natural_zero_exit_does_not_abort_gateway(self):
        now = datetime(
            2026, 8, 15, 3, 5, 0, tzinfo=stage_runner.CST)
        with mock.patch.object(
                stage_runner._proc, "run_guarded",
                return_value=(0, "", "", False)), \
                mock.patch.object(
                    stage_runner, "_abort_gateway_session") as abort:
            result = stage_runner._run_stage_child(
                "live", "2026-08-15T03:00", ["agent"], now=now)

        self.assertEqual(0, result["returncode"])
        self.assertNotIn("gateway_abort", result)
        abort.assert_not_called()

    def test_live_stopping_is_published_before_gateway_abort(self):
        now = datetime(
            2026, 8, 15, 3, 5, 0, tzinfo=stage_runner.CST)
        observer = mock.Mock()
        observer.evidence = {"stop_reason": "business_terminal_committed"}
        events: list[str] = []

        def publish(_payload):
            events.append("stopping")

        def abort(_stage, _cycle):
            events.append("abort")
            return {"terminal_confirmed": True, "status": "no-active-run"}

        with mock.patch.object(
                stage_runner, "_LiveChildObserver", return_value=observer), \
                mock.patch.object(
                    stage_runner._proc,
                    "run_guarded",
                    return_value=(stage_runner._proc.RC_OBSERVED_STOP,
                                  "", "", False)), \
                mock.patch.object(
                    stage_runner, "_abort_gateway_session", side_effect=abort):
            result = stage_runner._run_stage_child(
                "live", "2026-08-15T03:00", ["agent"], now=now,
                terminal_callback=publish)

        self.assertEqual(0, result["returncode"])
        self.assertEqual(["stopping", "abort"], events)

    def test_live_observer_aborts_when_position_exit_has_no_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            position_exit = tmp_root / "position_exit_2026-08-15T03-00.json"
            position_exit.write_text("{}", encoding="utf-8")
            now = (
                position_exit.stat().st_mtime
                + stage_runner._LIVE_HANDOFF_AFTER_POSITION_EXIT_SECONDS + 1)
            observer = stage_runner._LiveChildObserver(
                "2026-08-15T03:00",
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: now,
            )
            reason = observer()
        self.assertEqual(
            "post_facts_runner_handoff_violation:no_plan", reason)
        self.assertEqual(reason, observer.evidence["stop_reason"])

    def test_live_observer_marks_missing_position_exit_not_applicable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            facts.write_text(json.dumps({
                "facts_hash": "f" * 64,
                "positions": [],
            }), encoding="utf-8")
            observer = stage_runner._LiveChildObserver(
                "2026-08-15T03:00",
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
            )
            reason = observer()
        self.assertIsNone(reason)
        self.assertFalse(observer.evidence["position_exit_exists"])
        self.assertFalse(observer.evidence["position_exit_required"])
        self.assertEqual(0, observer.evidence["position_exit_position_count"])
        self.assertEqual(
            "not_applicable", observer.evidence["position_exit_status"])

    def test_live_observer_marks_missing_required_position_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            facts.write_text(json.dumps({
                "facts_hash": "f" * 64,
                "positions": [{"instId": "BTC-USDT-SWAP"}],
            }), encoding="utf-8")
            observer = stage_runner._LiveChildObserver(
                "2026-08-15T03:00",
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
            )
            reason = observer()
        self.assertIsNone(reason)
        self.assertFalse(observer.evidence["position_exit_exists"])
        self.assertTrue(observer.evidence["position_exit_required"])
        self.assertEqual(1, observer.evidence["position_exit_position_count"])
        self.assertEqual(
            "missing_required", observer.evidence["position_exit_status"])

    def test_live_observer_accepts_bound_executing_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            marker = tmp_root / "live_runner_state_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            marker.write_text(json.dumps({
                "schema_version": stage_runner._LIVE_RUNNER_STATE_SCHEMA_VERSION,
                "cycle_id": "2026-08-15T03:00",
                "state": "executing",
                "facts_hash": "f" * 64,
                "plan_sha256": stage_runner.hashlib.sha256(
                    plan.read_bytes()).hexdigest(),
                "session_key": stage_runner._gateway_session_key(
                    "live", "2026-08-15T03:00"),
                "stage_runner_pid": 4321,
            }), encoding="utf-8")
            db_root = Path(tmp) / "db"
            db_root.mkdir()
            con = sqlite3.connect(db_root / "live_trades.db")
            con.execute(
                "CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY, raw TEXT)"
            )
            con.execute(
                "INSERT INTO trade_cycles(cycle_id,raw) VALUES (?,?)",
                (
                    "2026-08-15T03:00",
                    json.dumps({
                        "runner_in_progress": True,
                        "batch_status": "partial",
                        "position_action_plan_hash": stage_runner.hashlib.sha256(
                            json.dumps(
                                {"actions": []},
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode("utf-8")
                        ).hexdigest(),
                        "live_facts": {"facts_hash": "f" * 64},
                    }),
                ),
            )
            con.commit()
            con.close()
            observer = stage_runner._LiveChildObserver(
                "2026-08-15T03:00",
                tmp_root=tmp_root,
                db_root=db_root,
                expected_session_key=stage_runner._gateway_session_key(
                    "live", "2026-08-15T03:00"),
                expected_stage_runner_pid=4321,
            )
            reason = observer()
        self.assertIsNone(reason)
        self.assertEqual("executing", observer.evidence["runner_state_value"])
        self.assertTrue(
            observer.evidence["trade_cycle_state"]["runner_in_progress"])

    def test_live_observer_stops_after_final_commit_even_if_marker_executing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            marker = tmp_root / "live_runner_state_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            marker.write_text(json.dumps({
                "schema_version": stage_runner._LIVE_RUNNER_STATE_SCHEMA_VERSION,
                "cycle_id": "2026-08-15T03:00",
                "state": "executing",
                "facts_hash": "f" * 64,
                "plan_sha256": stage_runner.hashlib.sha256(
                    plan.read_bytes()).hexdigest(),
            }), encoding="utf-8")
            db_root = Path(tmp) / "db"
            db_root.mkdir()
            con = sqlite3.connect(db_root / "live_trades.db")
            con.execute(
                "CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY, raw TEXT)"
            )
            con.execute(
                "INSERT INTO trade_cycles(cycle_id,raw) VALUES (?,?)",
                (
                    "2026-08-15T03:00",
                    json.dumps({
                        "runner_in_progress": False,
                        "batch_status": "completed",
                        "position_action_plan_hash": stage_runner.hashlib.sha256(
                            json.dumps(
                                {"actions": []},
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode("utf-8")
                        ).hexdigest(),
                        "live_facts": {"facts_hash": "f" * 64},
                    }),
                ),
            )
            con.commit()
            con.close()
            observer = stage_runner._LiveChildObserver(
                "2026-08-15T03:00",
                tmp_root=tmp_root,
                db_root=db_root,
            )
            reason = observer()
        self.assertEqual("business_terminal_committed", reason)
        self.assertFalse(
            observer.evidence["trade_cycle_state"]["runner_in_progress"])
        self.assertTrue(observer.evidence["trade_cycle_state"]["bound_final"])

    def test_live_observer_rejects_final_commit_without_required_position_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            facts.write_text(json.dumps({
                "facts_hash": "f" * 64,
                "positions": [
                    {"instId": "BTC-USDT-SWAP", "posSide": "long"}
                ],
            }), encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            db_root = Path(tmp) / "db"
            db_root.mkdir()
            con = sqlite3.connect(db_root / "live_trades.db")
            con.execute(
                "CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY, raw TEXT)"
            )
            con.execute(
                "INSERT INTO trade_cycles(cycle_id,raw) VALUES (?,?)",
                (
                    "2026-08-15T03:00",
                    json.dumps({
                        "runner_in_progress": False,
                        "batch_status": "completed",
                        "position_action_plan_hash": stage_runner.hashlib.sha256(
                            json.dumps(
                                {"actions": []},
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode("utf-8")
                        ).hexdigest(),
                        "live_facts": {"facts_hash": "f" * 64},
                    }),
                ),
            )
            con.commit()
            con.close()
            observer = stage_runner._LiveChildObserver(
                "2026-08-15T03:00",
                tmp_root=tmp_root,
                db_root=db_root,
            )
            reason = observer()
        self.assertEqual(
            "post_facts_runner_handoff_violation:position_exit_missing",
            reason,
        )
        self.assertTrue(observer.evidence["position_exit_required"])
        self.assertEqual(
            "missing_required", observer.evidence["position_exit_status"])

    def test_live_observer_does_not_treat_reconcile_row_as_runner_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            marker = tmp_root / "live_runner_state_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            marker.write_text(json.dumps({
                "schema_version": stage_runner._LIVE_RUNNER_STATE_SCHEMA_VERSION,
                "cycle_id": "2026-08-15T03:00",
                "state": "executing",
                "facts_hash": "f" * 64,
                "plan_sha256": stage_runner.hashlib.sha256(
                    plan.read_bytes()).hexdigest(),
            }), encoding="utf-8")
            db_root = Path(tmp) / "db"
            db_root.mkdir()
            con = sqlite3.connect(db_root / "live_trades.db")
            con.execute(
                "CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY, raw TEXT)"
            )
            con.execute(
                "INSERT INTO trade_cycles(cycle_id,raw) VALUES (?,?)",
                (
                    "2026-08-15T03:00",
                    json.dumps({
                        "reconcile_source": "exchange_fills_unrecorded",
                        "cycle_ts_source": "trusted_internal_override",
                    }),
                ),
            )
            con.commit()
            con.close()
            observer = stage_runner._LiveChildObserver(
                "2026-08-15T03:00",
                tmp_root=tmp_root,
                db_root=db_root,
            )
            reason = observer()
        self.assertIsNone(reason)
        self.assertFalse(observer.evidence["trade_cycle_state"]["bound_final"])

    def test_live_observer_aborts_when_plan_never_starts_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            plan.write_text('{"actions":[]}', encoding="utf-8")
            now = plan.stat().st_mtime + 31
            observer = stage_runner._LiveChildObserver(
                "2026-08-15T03:00",
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: now,
            )
            reason = observer()
        self.assertEqual(
            "post_facts_runner_handoff_violation:no_valid_runner_marker",
            reason,
        )

    def test_live_observer_autostarts_fixed_runner_once_for_stable_plan(self):
        cycle = "2026-08-15T03:00"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            now = (
                plan.stat().st_mtime
                + stage_runner._LIVE_SUPERVISOR_RUNNER_AUTOSTART_SECONDS
                + 0.1
            )
            process = mock.Mock()
            process.pid = 2468
            process.poll.return_value = None
            launcher = mock.Mock(return_value=process)
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: now,
                auto_start_runner=True,
                runner_launch_fn=launcher,
            )

            # First identical read establishes completeness; the next poll
            # proves the generic write is stable and performs the one launch.
            self.assertIsNone(observer())
            launcher.assert_not_called()
            self.assertIsNone(observer())
            self.assertIsNone(observer())

        launcher.assert_called_once()
        launch = launcher.call_args.kwargs
        self.assertEqual(cycle, launch["cycle"])
        self.assertEqual(plan, launch["plan_file"])
        self.assertEqual(facts, launch["facts_file"])
        self.assertEqual(
            "running",
            observer.evidence["supervisor_runner_autostart"]
            ["launches"][0]["status"],
        )
        self.assertEqual(
            2468,
            observer.evidence["supervisor_runner_autostart"]
            ["launches"][0]["pid"],
        )

    def test_independent_runner_watcher_launches_without_observer_callback(self):
        cycle = "2026-08-15T03:00"

        class StopAfterTwoPolls:
            def __init__(self):
                self.waits = 0

            def is_set(self):
                return False

            def wait(self, _delay):
                self.waits += 1
                return self.waits >= 2

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            process = mock.Mock()
            process.pid = 1357
            process.poll.return_value = None
            launcher = mock.Mock(return_value=process)
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: (
                    plan.stat().st_mtime
                    + stage_runner._LIVE_SUPERVISOR_RUNNER_AUTOSTART_SECONDS
                    + 0.1
                ),
                auto_start_runner=True,
                runner_launch_fn=launcher,
            )

            stage_runner._run_live_runner_autostart_watch(
                observer, StopAfterTwoPolls(), poll_seconds=0.01)

        launcher.assert_called_once()
        watcher = observer.evidence["supervisor_runner_autostart"]["watcher"]
        self.assertEqual("stopped", watcher["status"])
        self.assertEqual(2, watcher["polls"])

    def test_live_observer_never_autostarts_partial_json(self):
        cycle = "2026-08-15T03:00"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":', encoding="utf-8")
            launcher = mock.Mock()
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: plan.stat().st_mtime + 30.001,
                auto_start_runner=True,
                runner_launch_fn=launcher,
            )

            reason = observer()

        launcher.assert_not_called()
        self.assertEqual(
            "post_facts_runner_handoff_violation:no_valid_runner_marker",
            reason,
        )

    def test_live_observer_autostarts_only_new_sha_after_preflight_rewrite(self):
        cycle = "2026-08-15T03:00"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            marker = tmp_root / "live_runner_state_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            clock = {
                "now": plan.stat().st_mtime
                + stage_runner._LIVE_SUPERVISOR_RUNNER_AUTOSTART_SECONDS
                + 0.1,
            }
            first_process = mock.Mock()
            first_process.pid = 2468
            first_process.poll.return_value = 0
            second_process = mock.Mock()
            second_process.pid = 2469
            second_process.poll.return_value = None
            launcher = mock.Mock(
                side_effect=[first_process, second_process])
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: clock["now"],
                auto_start_runner=True,
                runner_launch_fn=launcher,
            )
            self.assertIsNone(observer())
            self.assertIsNone(observer())
            old_sha = stage_runner.hashlib.sha256(
                plan.read_bytes()).hexdigest()
            marker.write_text(json.dumps({
                "schema_version": stage_runner._LIVE_RUNNER_STATE_SCHEMA_VERSION,
                "cycle_id": cycle,
                "state": "failed_preflight",
                "facts_hash": "f" * 64,
                "plan_sha256": old_sha,
                "preflight_attempts": 1,
            }), encoding="utf-8")

            # Re-reading identical bytes never starts another process.
            self.assertIsNone(observer())
            self.assertEqual(1, launcher.call_count)

            plan.write_text(
                '{"actions":[],"receipt_context":{}}', encoding="utf-8")
            clock["now"] = (
                plan.stat().st_mtime
                + stage_runner._LIVE_SUPERVISOR_RUNNER_AUTOSTART_SECONDS
                + 0.1
            )
            self.assertIsNone(observer())
            self.assertIsNone(observer())

        self.assertEqual(2, launcher.call_count)
        launches = observer.evidence["supervisor_runner_autostart"]["launches"]
        self.assertNotEqual(
            launches[0]["plan_sha256"], launches[1]["plan_sha256"])

    def test_live_observer_does_not_autostart_over_valid_agent_marker(self):
        cycle = "2026-08-15T03:00"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            marker = tmp_root / "live_runner_state_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            marker.write_text(json.dumps({
                "schema_version": stage_runner._LIVE_RUNNER_STATE_SCHEMA_VERSION,
                "cycle_id": cycle,
                "state": "executing",
                "facts_hash": "f" * 64,
                "plan_sha256": stage_runner.hashlib.sha256(
                    plan.read_bytes()).hexdigest(),
            }), encoding="utf-8")
            launcher = mock.Mock()
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: plan.stat().st_mtime + 20.0,
                auto_start_runner=True,
                runner_launch_fn=launcher,
            )

            reason = observer()

        self.assertIsNone(reason)
        launcher.assert_not_called()

    def test_live_observer_revokes_handoff_at_30_second_boundary(self):
        cycle = "2026-08-15T03:00"
        session_key = stage_runner._gateway_session_key("live", cycle)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: plan.stat().st_mtime + 30.001,
                expected_session_key=session_key,
                expected_stage_runner_pid=4321,
            )

            reason = observer()
            gate = json.loads(observer.handoff_path.read_text(encoding="utf-8"))
            expected_plan_sha256 = stage_runner.hashlib.sha256(
                plan.read_bytes()).hexdigest()

        self.assertEqual(
            "post_facts_runner_handoff_violation:no_valid_runner_marker",
            reason,
        )
        self.assertEqual("supervisor_revoked",
                         observer.evidence["handoff_arbitration"])
        self.assertEqual("revoked", gate["state"])
        self.assertEqual(cycle, gate["cycle_id"])
        self.assertEqual(session_key, gate["session_key"])
        self.assertEqual(4321, gate["stage_runner_pid"])
        self.assertEqual(expected_plan_sha256, gate["plan_sha256"])
        self.assertEqual("f" * 64, gate["facts_hash"])

    def test_live_observer_repolls_while_runner_owns_handoff_cas(self):
        cycle = "2026-08-15T03:00"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            plan.write_text('{"actions":[]}', encoding="utf-8")
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: plan.stat().st_mtime + 32.0,
            )
            with mock.patch.object(
                    stage_runner, "_try_handoff_lock", return_value=None):
                reason = observer()
                handoff_exists = observer.handoff_path.exists()

        self.assertIsNone(reason)
        self.assertEqual(
            "claim_in_progress", observer.evidence["handoff_arbitration"])
        self.assertFalse(handoff_exists)

    def test_live_observer_rejects_wrong_session_marker_before_revocation(self):
        cycle = "2026-08-15T03:00"
        session_key = stage_runner._gateway_session_key("live", cycle)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            marker = tmp_root / "live_runner_state_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            marker.write_text(json.dumps({
                "schema_version": stage_runner._LIVE_RUNNER_STATE_SCHEMA_VERSION,
                "cycle_id": cycle,
                "state": "started",
                "facts_hash": "f" * 64,
                "plan_sha256": stage_runner.hashlib.sha256(
                    plan.read_bytes()).hexdigest(),
                "session_key": "agent:okx-live-trader:live-wrong",
                "stage_runner_pid": 4321,
            }), encoding="utf-8")
            observer = stage_runner._LiveChildObserver(
                cycle,
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
                now_fn=lambda: plan.stat().st_mtime + 31.0,
                expected_session_key=session_key,
                expected_stage_runner_pid=4321,
            )

            reason = observer()

        self.assertEqual(
            "post_facts_runner_handoff_violation:no_valid_runner_marker",
            reason,
        )
        self.assertIn("session_key", observer.evidence["marker_error"])
        self.assertTrue(observer.evidence["handoff_revoked"])

    def test_live_observer_stops_after_runner_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            facts = tmp_root / "live_facts_2026-08-15T03-00.json"
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            marker = tmp_root / "live_runner_state_2026-08-15T03-00.json"
            facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                             encoding="utf-8")
            plan.write_text('{"actions":[]}', encoding="utf-8")
            marker.write_text(json.dumps({
                "schema_version": stage_runner._LIVE_RUNNER_STATE_SCHEMA_VERSION,
                "cycle_id": "2026-08-15T03:00",
                "state": "committed",
                "facts_hash": "f" * 64,
                "plan_sha256": stage_runner.hashlib.sha256(
                    plan.read_bytes()).hexdigest(),
            }), encoding="utf-8")
            observer = stage_runner._LiveChildObserver(
                "2026-08-15T03:00",
                tmp_root=tmp_root,
                db_root=Path(tmp) / "db",
            )
            reason = observer()
        self.assertEqual("runner_terminal:committed", reason)

    def test_live_child_maps_observed_business_terminal_to_success(self):
        now = datetime(
            2026, 8, 15, 3, 5, 0, tzinfo=stage_runner.CST)
        observer = mock.Mock()
        observer.evidence = {"stop_reason": "business_terminal_committed"}
        with mock.patch.object(
                stage_runner, "_LiveChildObserver", return_value=observer), \
                mock.patch.object(
                    stage_runner._proc,
                    "run_guarded",
                    return_value=(stage_runner._proc.RC_OBSERVED_STOP,
                                  "", "", False)), \
                mock.patch.object(
                    stage_runner,
                    "_abort_gateway_session",
                    return_value={"terminal_confirmed": True}):
            result = stage_runner._run_stage_child(
                "live", "2026-08-15T03:00", ["agent"], now=now)
        self.assertEqual(0, result["returncode"])
        self.assertEqual(
            "business_terminal_committed",
            result["observed_stop"]["stop_reason"],
        )

    def test_live_child_waits_for_supervisor_runner_after_agent_returns(self):
        now = datetime(
            2026, 8, 15, 3, 5, 0, tzinfo=stage_runner.CST)
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "position_plan.json"
            plan_path.write_text('{"actions":[]}', encoding="utf-8")
            observer = mock.Mock()
            observer.plan_path = plan_path
            observer.evidence = {
                "supervisor_runner_autostart": {
                    "enabled": True,
                    "launches": [],
                },
            }

            def observe():
                observer.evidence["stop_reason"] = (
                    "business_terminal_committed")
                return "business_terminal_committed"

            observer.side_effect = observe
            observer.shutdown_supervised_runner.return_value = None
            with mock.patch.object(
                    stage_runner, "_LiveChildObserver", return_value=observer), \
                    mock.patch.object(
                        stage_runner._proc,
                        "run_guarded",
                        return_value=(0, "", "", False)), \
                    mock.patch.object(
                        stage_runner,
                        "_abort_gateway_session",
                        return_value={"terminal_confirmed": True}):
                result = stage_runner._run_stage_child(
                    "live", "2026-08-15T03:00", ["agent"], now=now)

        self.assertEqual(0, result["returncode"])
        self.assertEqual(
            0, result["post_agent_handoff_wait"]["agent_returncode"])
        self.assertEqual(
            "business_terminal_committed",
            result["post_agent_handoff_wait"]["reason"],
        )
        self.assertEqual(
            "business_terminal_committed",
            result["observed_stop"]["stop_reason"],
        )

    def test_live_child_maps_observed_handoff_violation_to_failure(self):
        now = datetime(
            2026, 8, 15, 3, 5, 0, tzinfo=stage_runner.CST)
        observer = mock.Mock()
        observer.evidence = {
            "stop_reason": "post_facts_runner_handoff_violation:no_plan",
        }
        with mock.patch.object(
                stage_runner, "_LiveChildObserver", return_value=observer), \
                mock.patch.object(
                    stage_runner._proc,
                    "run_guarded",
                    return_value=(stage_runner._proc.RC_OBSERVED_STOP,
                                  "", "", False)), \
                mock.patch.object(
                    stage_runner,
                    "_abort_gateway_session",
                    return_value={"terminal_confirmed": True}):
            result = stage_runner._run_stage_child(
                "live", "2026-08-15T03:00", ["agent"], now=now)
        self.assertEqual(stage_runner._LIVE_HANDOFF_FAILURE_RC,
                         result["returncode"])
        self.assertEqual(
            "post_facts_runner_handoff_violation",
            result["failure_kind"],
        )

    def test_gateway_abort_uses_exact_isolated_session_key(self):
        proc = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "ok": True,
                "abortedRunId": "RUN-1",
                "status": "aborted",
            }),
            stderr="",
        )
        with mock.patch.object(
                stage_runner.subprocess, "run", return_value=proc) as run:
            result = stage_runner._abort_gateway_session(
                "live", "2026-08-15T05:00")

        self.assertTrue(result["terminal_confirmed"])
        self.assertEqual("aborted", result["status"])
        command = run.call_args.args[0]
        params = json.loads(command[command.index("--params") + 1])
        self.assertEqual(
            "agent:okx-live-trader:live-20260815-0500",
            params["key"],
        )
        self.assertIn("sessions.abort", command)

    def test_same_connection_abort_control_is_identity_bound(self):
        cycle = "2026-08-20T22:00"
        command = [
            str(stage_runner._OPENCLAW_NODE),
            "--stack-size=8192",
            str(stage_runner._OPENCLAW_AGENT_ADAPTER),
            "--openclaw-mjs",
            str(stage_runner._OPENCLAW_MJS),
            "--",
            "agent",
        ]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                stage_runner, "_STAGE_CONTROL_DIR", Path(tmp)):
            prepared, control = stage_runner._prepare_same_connection_abort(
                "live", cycle, command)
            self.assertIsNotNone(control)
            self.assertIn("--abort-control-file", prepared)
            self.assertLess(
                prepared.index("--control-id"), prepared.index("--"))
            requested = stage_runner._request_same_connection_abort(
                control, "live", cycle, "business_terminal_committed")
            payload = json.loads(
                Path(control["control_file"]).read_text(encoding="utf-8"))
            self.assertTrue(requested)
            self.assertEqual(control["control_id"], payload["control_id"])
            self.assertEqual("SIGTERM", payload["signal"])
            self.assertEqual(
                "agent:okx-live-trader:live-20260820-2200",
                payload["session_key"],
            )
            Path(control["receipt_file"]).write_text(json.dumps({
                "schema": stage_runner._SAME_CONNECTION_RECEIPT_SCHEMA,
                "wrapper_pid": 1234,
                "control_id": control["control_id"],
                "control_configured": True,
                "control_request_observed": True,
                "signal_delivered": True,
                "signal_delivery_listener_count": 1,
                "command_completed": False,
                "phase": "signal_exit",
                "exit_code": 143,
            }), encoding="utf-8")
            loaded = stage_runner._load_same_connection_receipt(control)
        self.assertTrue(loaded["receipt_valid"])
        self.assertTrue(loaded["receipt"]["signal_delivered"])

    @unittest.skipUnless(
        stage_runner._OPENCLAW_NODE.exists(), "Node runtime unavailable")
    def test_same_connection_adapter_exits_gracefully_in_isolation(self):
        command = [
            str(stage_runner._OPENCLAW_NODE),
            "--stack-size=8192",
            str(stage_runner._OPENCLAW_AGENT_ADAPTER),
            "--self-test",
            "--",
        ]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    stage_runner, "_STAGE_CONTROL_DIR", Path(tmp)), \
                mock.patch.dict(
                    os.environ,
                    {"OKX_AGENT_ADAPTER_SELF_TEST": "1"},
                    clear=False,
                ):
            prepared, control = stage_runner._prepare_same_connection_abort(
                "live", "2026-08-20T22:00", command)
            stop_report = {}
            rc, _out, err, timed_out = stage_runner._proc.run_guarded(
                prepared,
                timeout=10,
                observer=lambda: "business_terminal_committed",
                observer_poll_seconds=0.1,
                graceful_stop=lambda _proc, reason: (
                    stage_runner._request_same_connection_abort(
                        control,
                        "live",
                        "2026-08-20T22:00",
                        reason,
                    )
                ),
                graceful_stop_timeout=3,
                stop_report=stop_report,
            )
            loaded = stage_runner._load_same_connection_receipt(control)
        self.assertEqual(stage_runner._proc.RC_OBSERVED_STOP, rc)
        self.assertFalse(timed_out)
        self.assertIn("graceful stop completed rc=143", err)
        self.assertTrue(stop_report["graceful_completed"])
        self.assertFalse(stop_report["process_tree_terminated"])
        self.assertTrue(loaded["receipt_valid"])
        self.assertTrue(loaded["receipt"]["signal_delivered"])

    @unittest.skipUnless(
        stage_runner._OPENCLAW_NODE.exists(), "Node runtime unavailable")
    def test_same_connection_adapter_records_terminal_gateway_error(self):
        command = [
            str(stage_runner._OPENCLAW_NODE),
            "--stack-size=8192",
            str(stage_runner._OPENCLAW_AGENT_ADAPTER),
            "--self-test-terminal-error",
            "--",
        ]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    stage_runner, "_STAGE_CONTROL_DIR", Path(tmp)), \
                mock.patch.dict(
                    os.environ,
                    {"OKX_AGENT_ADAPTER_SELF_TEST": "1"},
                    clear=False,
                ):
            prepared, control = stage_runner._prepare_same_connection_abort(
                "live", "2026-08-21T01:00", command)
            rc, _out, _err, timed_out = stage_runner._proc.run_guarded(
                prepared,
                timeout=5,
            )
            loaded = stage_runner._load_same_connection_receipt(control)
        self.assertEqual(1, rc)
        self.assertFalse(timed_out)
        self.assertTrue(loaded["receipt_valid"])
        self.assertTrue(
            loaded["receipt"]["gateway_terminal_error_observed"])
        self.assertEqual(
            "all_models_failed",
            loaded["receipt"]["gateway_terminal_error_marker"],
        )
        self.assertTrue(
            stage_runner._receipt_proves_gateway_terminal_error(loaded))

    def test_nonzero_terminal_gateway_error_receipt_overrides_cleanup_race(self):
        now = datetime(
            2026, 8, 21, 1, 2, 0, tzinfo=stage_runner.CST)
        command = [
            str(stage_runner._OPENCLAW_NODE),
            "--stack-size=8192",
            str(stage_runner._OPENCLAW_AGENT_ADAPTER),
            "--openclaw-mjs",
            str(stage_runner._OPENCLAW_MJS),
            "--",
            "agent",
        ]

        def guarded(prepared, **_kwargs):
            control_id = prepared[prepared.index("--control-id") + 1]
            receipt_path = Path(
                prepared[prepared.index("--abort-receipt-file") + 1])
            receipt_path.write_text(json.dumps({
                "schema": stage_runner._SAME_CONNECTION_RECEIPT_SCHEMA,
                "wrapper_pid": 1234,
                "control_id": control_id,
                "control_configured": True,
                "control_request_observed": False,
                "signal_delivered": False,
                "signal_delivery_listener_count": 0,
                "command_completed": False,
                "gateway_terminal_error_observed": True,
                "gateway_terminal_error_marker": "all_models_failed",
                "phase": "process_exit",
                "exit_code": 1,
            }), encoding="utf-8")
            return 1, "", "models failed", False

        unauthorized = {
            "requested": True,
            "rpc": "sessions.abort",
            "status": "unauthorized",
            "terminal_confirmed": False,
        }
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    stage_runner, "_STAGE_CONTROL_DIR", Path(tmp)), \
                mock.patch.object(
                    stage_runner._proc, "run_guarded", side_effect=guarded), \
                mock.patch.object(
                    stage_runner, "_abort_gateway_session",
                    return_value=unauthorized):
            result = stage_runner._run_stage_child(
                "live", "2026-08-21T01:00", command, now=now)

        self.assertEqual(1, result["returncode"])
        self.assertTrue(result["gateway_abort"]["terminal_confirmed"])
        self.assertEqual(
            "gateway-terminal-error", result["gateway_abort"]["status"])
        self.assertEqual(
            "gateway_terminal_error", result["failure_kind"])
        self.assertEqual(
            "unauthorized",
            result["gateway_abort"]["cleanup_probe"]["status"],
        )

    def test_gateway_abort_accepts_no_active_run_as_terminal(self):
        proc = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "ok": True,
                "abortedRunId": None,
                "status": "no-active-run",
            }),
            stderr="",
        )
        with mock.patch.object(
                stage_runner.subprocess, "run", return_value=proc):
            result = stage_runner._abort_gateway_session(
                "live", "2026-08-15T05:00")

        self.assertTrue(result["terminal_confirmed"])
        self.assertEqual("no-active-run", result["status"])

    def test_gateway_abort_invalid_response_remains_unconfirmed(self):
        proc = mock.Mock(returncode=1, stdout="{}", stderr="rpc denied")
        with mock.patch.object(
                stage_runner.subprocess, "run", return_value=proc):
            result = stage_runner._abort_gateway_session(
                "live", "2026-08-15T05:00")

        self.assertFalse(result["terminal_confirmed"])
        self.assertEqual("invalid_response", result["status"])
        self.assertIn("rpc denied", result["error"])

    def test_gateway_abort_surfaces_unauthorized_and_skips_fallback(self):
        proc = mock.Mock(
            returncode=1,
            stdout=json.dumps({
                "ok": False,
                "error": {
                    "type": "gateway_request_error",
                    "code": "INVALID_REQUEST",
                    "message": "unauthorized",
                    "retryable": False,
                },
            }),
            stderr="",
        )
        with mock.patch.object(
                stage_runner.subprocess, "run", return_value=proc) as run:
            result = stage_runner._abort_gateway_session(
                "live", "2026-08-15T05:00")

        self.assertFalse(result["terminal_confirmed"])
        self.assertEqual("unauthorized", result["status"])
        self.assertEqual("unauthorized", result["error"])
        self.assertEqual("INVALID_REQUEST", result["gateway_error_code"])
        self.assertFalse(result["retryable"])
        self.assertEqual(
            "originating_connection_or_admin_scope",
            result["authorization_required"],
        )
        self.assertIn("same requester", result["fallback_skipped"])
        self.assertEqual(1, run.call_count)

    def test_gateway_abort_falls_back_once_to_same_session_chat_abort(self):
        primary = mock.Mock(
            returncode=1, stdout="{}", stderr="transient sessions failure")
        fallback = mock.Mock(
            returncode=0,
            stdout=json.dumps({"ok": True, "aborted": False, "runIds": []}),
            stderr="",
        )
        with mock.patch.object(
                stage_runner.subprocess, "run",
                side_effect=[primary, fallback]) as run:
            result = stage_runner._abort_gateway_session(
                "live", "2026-08-15T05:00")

        self.assertTrue(result["terminal_confirmed"])
        self.assertEqual("chat.abort", result["rpc"])
        self.assertEqual("no-active-run", result["status"])
        self.assertEqual(2, run.call_count)
        fallback_command = run.call_args_list[1].args[0]
        self.assertIn("chat.abort", fallback_command)
        params = json.loads(
            fallback_command[fallback_command.index("--params") + 1])
        self.assertEqual(
            "agent:okx-live-trader:live-20260815-0500",
            params["sessionKey"],
        )

    def test_non_live_stage_preserves_unbounded_child_contract(self):
        proc = mock.Mock(returncode=0)
        with mock.patch.object(
                stage_runner.subprocess, "run", return_value=proc) as run:
            result = stage_runner._run_stage_child(
                "push", "2026-08-15T03:00", ["push"])

        self.assertEqual({
            "returncode": 0,
            "timed_out": False,
            "started": True,
        }, result)
        run.assert_called_once()

    def test_forward_push_child_uses_remaining_shared_deadline(self):
        cycle = "2026-08-16T07:30"
        current = datetime(
            2026, 8, 16, 7, 42, 0, tzinfo=stage_runner.CST)
        guarded = mock.Mock(return_value=(0, "sent", "", False))
        with mock.patch.object(
                stage_runner._proc, "run_guarded", guarded), \
                mock.patch.object(stage_runner.subprocess, "run") as legacy:
            result = stage_runner._run_stage_child(
                "push", cycle, ["push"], now=current)

        self.assertEqual(0, result["returncode"])
        self.assertEqual(120.0, result["budget_seconds"])
        self.assertEqual("2026-08-16 07:44:00",
                         result["absolute_deadline_at"])
        self.assertEqual("2026-08-16T07:30:00+08:00",
                         result["deadline_activation_cst"])
        guarded.assert_called_once()
        self.assertEqual(120.0, guarded.call_args.kwargs["timeout"])
        legacy.assert_not_called()

    def test_forward_push_child_exact_cycle_plus_fourteen_does_not_start(self):
        cycle = "2026-08-16T07:30"
        exact_deadline = datetime(
            2026, 8, 16, 7, 44, 0, tzinfo=stage_runner.CST)
        with mock.patch.object(
                stage_runner._proc, "run_guarded") as guarded, \
                mock.patch.object(stage_runner.subprocess, "run") as legacy:
            result = stage_runner._run_stage_child(
                "push", cycle, ["push"], now=exact_deadline)

        self.assertEqual(stage_runner._proc.RC_TIMEOUT,
                         result["returncode"])
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["started"])
        guarded.assert_not_called()
        legacy.assert_not_called()

    def test_forward_post_push_reconcile_reuses_same_absolute_deadline(self):
        cycle = "2026-08-16T07:30"
        current = datetime(
            2026, 8, 16, 7, 43, 30, tzinfo=stage_runner.CST)
        guarded = mock.Mock(return_value=(0, '{"ok":true}', "", False))
        with mock.patch.object(stage_runner._proc, "run_guarded", guarded), \
                mock.patch.object(stage_runner.subprocess, "run") as legacy:
            result = stage_runner._run_post_push_monitor(
                cycle, "live", now=current)

        self.assertEqual(0, result["rc"])
        self.assertEqual(30.0, result["budget_seconds"])
        self.assertEqual("2026-08-16 07:44:00",
                         result["absolute_deadline_at"])
        self.assertFalse(result["deadline_exceeded"])
        guarded.assert_called_once()
        self.assertEqual(30.0, guarded.call_args.kwargs["timeout"])
        legacy.assert_not_called()

    def test_historical_post_push_reconcile_keeps_independent_timeout(self):
        proc = mock.Mock(returncode=0, stdout="clean", stderr="")
        with mock.patch.object(
                stage_runner.subprocess, "run", return_value=proc) as legacy, \
                mock.patch.object(stage_runner._proc, "run_guarded") as guarded:
            result = stage_runner._run_post_push_monitor(
                "2026-08-16T07:15", "live")

        self.assertEqual(0, result["rc"])
        self.assertEqual("clean", result["output"])
        self.assertEqual(240, legacy.call_args.kwargs["timeout"])
        guarded.assert_not_called()

    def test_forward_post_push_reconcile_keeps_legacy_240_second_cap(self):
        cycle = "2026-08-16T07:30"
        current = datetime(
            2026, 8, 16, 7, 35, 0, tzinfo=stage_runner.CST)
        guarded = mock.Mock(return_value=(0, '{"ok":true}', "", False))
        with mock.patch.object(stage_runner._proc, "run_guarded", guarded):
            result = stage_runner._run_post_push_monitor(
                cycle, "live", now=current)

        self.assertEqual(540.0, result["budget_seconds"])
        self.assertEqual(240.0, result["guard_timeout_seconds"])
        self.assertEqual(240.0, guarded.call_args.kwargs["timeout"])

    def test_monitor_240_second_cap_timeout_is_not_absolute_deadline(self):
        cycle = "2026-08-16T07:30"
        current = datetime(
            2026, 8, 16, 7, 35, 0, tzinfo=stage_runner.CST)
        guarded = mock.Mock(return_value=(
            stage_runner._proc.RC_TIMEOUT,
            "",
            "timeout; process tree terminated",
            True,
        ))
        with mock.patch.object(stage_runner._proc, "run_guarded", guarded):
            monitor = stage_runner._run_post_push_monitor(
                cycle, "live", now=current)

        self.assertTrue(monitor["timed_out"])
        self.assertFalse(monitor["deadline_exceeded"])
        self.assertIn("240s guard timeout", monitor["error"])
        live_ok = {"status": "succeeded", "returncode": 0}
        sla = stage_runner.build_complete_cycle_sla(
            cycle, monitor, live_status=live_ok)
        failure = stage_runner._forward_post_push_failure(
            cycle, "full", monitor, sla, live_ok)
        self.assertEqual("post_push_reconcile_failed",
                         failure["failure_kind"])
        self.assertEqual(stage_runner._POST_PUSH_RECONCILE_FAILURE_RC,
                         failure["returncode"])

    def test_forward_post_push_exact_deadline_fails_without_start(self):
        cycle = "2026-08-16T07:30"
        exact_deadline = datetime(
            2026, 8, 16, 7, 44, 0, tzinfo=stage_runner.CST)
        with mock.patch.object(
                stage_runner._proc, "run_guarded") as guarded, \
                mock.patch.object(stage_runner.subprocess, "run") as legacy:
            result = stage_runner._run_post_push_monitor(
                cycle, "live", now=exact_deadline)

        self.assertEqual(stage_runner._proc.RC_TIMEOUT, result["rc"])
        self.assertTrue(result["deadline_exceeded"])
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["started"])
        guarded.assert_not_called()
        legacy.assert_not_called()

    def test_v3_push_and_post_monitor_use_separate_same_slot_deadlines(self):
        cycle = "2026-08-20T18:00"
        current = datetime(
            2026, 8, 20, 18, 14, 0, tzinfo=stage_runner.CST)
        guarded = mock.Mock(return_value=(0, '{"ok":true}', "", False))
        with mock.patch.object(stage_runner._proc, "run_guarded", guarded):
            child = stage_runner._run_stage_child(
                "push", cycle, ["push"], now=current)
        self.assertEqual(30.0, child["budget_seconds"])

        monitor_now = datetime(
            2026, 8, 20, 18, 14, 30, tzinfo=stage_runner.CST)
        with mock.patch.object(
                stage_runner._proc, "run_guarded",
                return_value=(0, '{"ok":true}', "", False)) as monitor_guard:
            monitor = stage_runner._run_post_push_monitor(
                cycle, "live", now=monitor_now)
        self.assertEqual(30.0, monitor["budget_seconds"])
        self.assertEqual(30.0, monitor_guard.call_args.kwargs["timeout"])

    def test_post_push_completion_crossing_absolute_deadline_is_failure(self):
        cycle = "2026-08-16T07:30"
        current = datetime(
            2026, 8, 16, 7, 43, 30, tzinfo=stage_runner.CST)
        guarded = mock.Mock(return_value=(0, '{"ok":true}', "", False))
        with mock.patch.object(stage_runner._proc, "run_guarded", guarded), \
                mock.patch.object(
                    stage_runner.time, "monotonic", side_effect=[100.0, 130.0]):
            result = stage_runner._run_post_push_monitor(
                cycle, "live", now=current)

        self.assertFalse(result["timed_out"])
        self.assertTrue(result["deadline_exceeded"])
        self.assertEqual(30.0, result["guard_elapsed_seconds"])

    def test_forward_post_push_deadline_failure_keeps_c2c_alert(self):
        cycle = "2026-08-16T07:30"
        child = {
            "returncode": 0,
            "timed_out": False,
            "started": True,
            "budget_seconds": 60.0,
        }
        monitor = {
            "rc": stage_runner._proc.RC_TIMEOUT,
            "output": "",
            "timed_out": True,
            "started": True,
            "deadline_exceeded": True,
            "absolute_deadline_at": "2026-08-16 07:44:00",
            "error": "deadline",
        }
        alert = mock.Mock(return_value={"delivered": True, "rc": 0})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
                mock.patch.object(stage_runner, "_run_stage_child",
                                  return_value=child), \
                mock.patch.object(stage_runner, "verify_business_output",
                                  return_value={"ok": True, "checks": []}), \
                mock.patch.object(stage_runner, "_run_post_push_monitor",
                                  return_value=monitor), \
                mock.patch.object(stage_runner, "_send_failure_alert",
                                  alert), \
                mock.patch.object(sys, "argv", [
                    "stage_runner.py", "--stage", "push", "--cycle", cycle,
                    "--mode", "full", "--", "push",
                ]):
            (Path(tmp) / "live-2026-08-16T07-30.json").write_text(
                json.dumps({"status": "succeeded", "returncode": 0}),
                encoding="utf-8",
            )
            rc = stage_runner.main()
            payload = json.loads(
                (Path(tmp) / "push-2026-08-16T07-30.json").read_text(
                    encoding="utf-8"))

        self.assertEqual(stage_runner._proc.RC_TIMEOUT, rc)
        self.assertEqual("failed", payload["status"])
        self.assertEqual("cycle_deadline_exceeded", payload["failure_kind"])
        self.assertEqual(0, payload["child_returncode"])
        self.assertEqual(stage_runner._proc.RC_TIMEOUT,
                         payload["returncode"])
        self.assertEqual("post_live_reconcile",
                         payload["agent_terminal_evidence"][
                             "deadline_component"])
        alert.assert_called_once()
        self.assertEqual("push", alert.call_args.args[0])
        self.assertEqual(cycle, alert.call_args.args[1])
        self.assertEqual("cycle_deadline_exceeded",
                         alert.call_args.args[4]["failure_kind"])

    def test_forward_reconcile_contract_rejects_all_unclean_results(self):
        cycle = "2026-08-16T07:30"
        live_ok = {"status": "succeeded", "returncode": 0}
        cases = (
            ({"rc": 1, "output": ""}, "monitor_rc_nonzero"),
            ({"rc": 0, "output": "not-json"}, "monitor_output_invalid"),
            ({"rc": 0, "output": json.dumps({
                "ts": "2026-08-16 07:43:00",
                "ok": False,
                "issue": True,
            })}, "monitor_not_clean"),
        )
        for monitor, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                sla = stage_runner.build_complete_cycle_sla(
                    cycle, monitor, live_status=live_ok)
                self.assertEqual(expected_reason, sla["reason"])
                failure = stage_runner._forward_post_push_failure(
                    cycle, "full", monitor, sla, live_ok)
                self.assertEqual(
                    "post_push_reconcile_failed",
                    failure["failure_kind"],
                )
                self.assertEqual(
                    stage_runner._POST_PUSH_RECONCILE_FAILURE_RC,
                    failure["returncode"],
                )

    def test_forward_reconcile_timestamp_at_exact_deadline_is_failure(self):
        cycle = "2026-08-16T07:30"
        live_ok = {"status": "succeeded", "returncode": 0}
        monitor = {
            "rc": 0,
            "output": json.dumps({
                "ts": "2026-08-16 07:44:00",
                "cycle_id": cycle,
                "profile": "live",
                "ok": True,
                "issue": False,
                "markers": [],
            }),
        }
        sla = stage_runner.build_complete_cycle_sla(
            cycle, monitor, live_status=live_ok)
        self.assertEqual("met", sla["status"])
        self.assertEqual(840, sla["elapsed_seconds"])
        failure = stage_runner._forward_post_push_failure(
            cycle, "full", monitor, sla, live_ok)
        self.assertEqual("cycle_deadline_exceeded",
                         failure["failure_kind"])
        self.assertEqual(stage_runner._proc.RC_TIMEOUT,
                         failure["returncode"])

    def test_failed_live_failure_report_does_not_fail_push_again(self):
        cycle = "2026-08-16T07:30"
        live_failed = {
            "status": "failed",
            "returncode": stage_runner._proc.RC_TIMEOUT,
            "failure_kind": "cycle_deadline_exceeded",
        }
        monitor = {"rc": 1, "output": ""}
        sla = stage_runner.build_complete_cycle_sla(
            cycle, monitor, live_status=live_failed)
        self.assertEqual("incomplete", sla["status"])
        self.assertIsNone(stage_runner._forward_post_push_failure(
            cycle, "failure_report", monitor, sla, live_failed))

    @staticmethod
    def _business_error_push_fixture():
        cycle = "2026-08-22T09:45"
        barrier = {
            "required": True, "profile": "live", "cycle_id": cycle,
            "status": "ok", "rc": 0, "blocking": False, "p0": False,
            "contract_valid": True, "report_safe": True,
        }
        live_failed = {
            "stage": "live", "cycle_id": cycle, "status": "failed",
            "returncode": 86,
            "failure_kind": "business_verification_error",
            "finished_at": "2026-08-22 09:55:33",
            "profile_lease_released": True,
            "collection_gate": {
                "schema_version": 1, "status": "met", "cycle_id": cycle,
                "required_sources": ["fast"],
                "completed_at": "2026-08-22 09:46:24",
                "elapsed_seconds": 84, "time_threshold_seconds": None,
            },
            "business_check": {"ok": False},
            "report_reconcile_barrier": dict(barrier),
        }
        terminal = {
            "status": "failed", "returncode": 86,
            "finished_at": "2026-08-22 09:55:33",
            "profile_lease_released": True,
            "same_cycle_active_lease": False,
            "report_reconcile_barrier": dict(barrier),
        }
        attestation = {
            "decision": "error", "n_orders": 0, "trade_count": 0,
            "sha256": "a" * 64,
            "inter_report_exchange_required": True,
            "inter_report_exchange_schema_version": 2,
            "inter_report_fill_count": 0,
            "inter_report_sha256": "b" * 64,
            "inter_report_window_start_exclusive_cst":
                "2026-08-22 09:30:00",
            "inter_report_window_end_inclusive_cst":
                "2026-08-22 09:45:00",
            "live_stage_terminal": terminal,
        }
        report = {
            "cycle": cycle, "report_mode": "business_terminal",
            "ok": True, "send_status": "sent",
            "steps": {
                "build": {"ok": True, "action": "ERROR", "n_trades": 0},
                "business_attestation_pre_archive": attestation,
                "business_attestation_pre_send": json.loads(
                    json.dumps(attestation)),
                "send": {"rc": 0},
            },
        }
        monitor = {
            "rc": 0, "timed_out": False, "started": True,
            "deadline_exceeded": False,
            "output": json.dumps({
                "ts": "2026-08-22 09:56:00", "cycle_id": cycle,
                "profile": "live", "ok": True, "issue": False,
                "rc": 0, "markers": [],
            }),
        }
        return cycle, live_failed, report, monitor

    def test_delivered_business_error_report_is_not_a_second_push_failure(self):
        cycle, live_failed, report, monitor = (
            self._business_error_push_fixture())
        sla = stage_runner.build_complete_cycle_sla(
            cycle, monitor, live_status=live_failed)
        self.assertEqual("incomplete", sla["status"])
        self.assertEqual("live_stage_not_succeeded", sla["reason"])
        self.assertTrue(stage_runner._strict_business_error_push_report(
            cycle, report, live_failed))
        self.assertIsNone(stage_runner._forward_post_push_failure(
            cycle, "full", monitor, sla, live_failed, push_report=report))

    def test_business_error_push_exception_remains_fail_closed(self):
        cycle, live_failed, report, monitor = (
            self._business_error_push_fixture())
        sla = stage_runner.build_complete_cycle_sla(
            cycle, monitor, live_status=live_failed)

        bad_send = json.loads(json.dumps(report))
        bad_send["send_status"] = "failed"
        self.assertIsNotNone(stage_runner._forward_post_push_failure(
            cycle, "full", monitor, sla, live_failed,
            push_report=bad_send))

        drifted = json.loads(json.dumps(report))
        drifted["steps"]["business_attestation_pre_send"]["sha256"] = "c" * 64
        self.assertIsNotNone(stage_runner._forward_post_push_failure(
            cycle, "full", monitor, sla, live_failed,
            push_report=drifted))

        dirty_monitor = dict(monitor)
        dirty_monitor["rc"] = 1
        self.assertIsNotNone(stage_runner._forward_post_push_failure(
            cycle, "full", dirty_monitor, sla, live_failed,
            push_report=report))

        wrong_failure = dict(live_failed)
        wrong_failure["failure_kind"] = "gateway_terminal_error"
        self.assertIsNotNone(stage_runner._forward_post_push_failure(
            cycle, "full", monitor, sla, wrong_failure,
            push_report=report))

    def test_live_status_and_push_mode_exceptions_are_fail_closed(self):
        cycle = "2026-08-16T07:30"
        monitor = {
            "rc": 0,
            "output": json.dumps({
                "ts": "2026-08-16 07:43:00",
                "ok": True,
                "issue": False,
            }),
        }
        cases = (
            ("full", {}, "missing"),
            ("full", {"status": "succeeded", "returncode": "bad"},
             "malformed"),
            ("full", {"status": "running", "returncode": 0}, "running"),
            ("full", {"status": "failed", "returncode": 124},
             "full_failed"),
            ("failure_report", {"status": "succeeded", "returncode": 0},
             "failure_report_success_mismatch"),
            ("failure_report", {"status": "failed", "returncode": 0},
             "failure_report_failed_zero_rc"),
            ("failure_report", {"status": "succeeded", "returncode": 1},
             "failure_report_succeeded_nonzero_rc"),
            ("failure_report", {"returncode": 1},
             "failure_report_missing_status"),
            ("failure_report", {"status": "failed", "returncode": "bad"},
             "failure_report_malformed_rc"),
        )
        for mode, live_status, label in cases:
            with self.subTest(label=label):
                sla = stage_runner.build_complete_cycle_sla(
                    cycle, monitor, live_status=live_status)
                failure = stage_runner._forward_post_push_failure(
                    cycle, mode, monitor, sla, live_status)
                self.assertEqual("post_push_reconcile_failed",
                                 failure["failure_kind"])
                self.assertEqual(
                    stage_runner._POST_PUSH_RECONCILE_FAILURE_RC,
                    failure["returncode"],
                )

    def test_live_report_reconcile_barrier_reuses_validated_autoheal(self):
        cycle = "2026-08-14T19:00"
        producer = {
            "contract_version": 1,
            "request_id": "c" * 32,
            "profile": "live",
            "cycle": cycle,
            "db_root": str(stage_runner.DB_ROOT.resolve()),
            "status": "applied",
            "applied": True,
            "p0": False,
            "blocking": False,
            "findings": [],
            "healed": [{"kind": "GHOST-EXACT", "applied": True}],
            "needs_human": [],
            "rc": 0,
        }
        fake_trigger = mock.Mock()
        fake_trigger._autoheal_ledger.return_value = producer
        with mock.patch.dict(sys.modules, {"trigger_agent": fake_trigger}):
            result = stage_runner._run_live_report_reconcile_barrier(cycle)
        fake_trigger._autoheal_ledger.assert_called_once_with(
            "live", cycle, apply_enabled_override=False)
        self.assertTrue(result["contract_valid"])
        self.assertTrue(result["report_safe"])
        self.assertFalse(result["apply_authorized"])
        self.assertEqual(result["healed_count"], 1)

    def test_live_report_reconcile_barrier_blocks_unresolved_contract(self):
        cycle = "2026-08-14T19:00"
        producer = {
            "contract_version": 1,
            "request_id": "d" * 32,
            "profile": "live",
            "cycle": cycle,
            "db_root": str(stage_runner.DB_ROOT.resolve()),
            "status": "needs_human",
            "applied": False,
            "p0": False,
            "blocking": True,
            "findings": [{"kind": "GHOST-FUZZY"}],
            "healed": [],
            "needs_human": [{"kind": "GHOST-FUZZY"}],
            "rc": 1,
        }
        fake_trigger = mock.Mock()
        fake_trigger._autoheal_ledger.return_value = producer
        with mock.patch.dict(sys.modules, {"trigger_agent": fake_trigger}):
            result = stage_runner._run_live_report_reconcile_barrier(cycle)
        self.assertTrue(result["contract_valid"])
        self.assertFalse(result["report_safe"])
        self.assertTrue(result["blocking"])

    def test_failed_business_terminal_forces_report_barrier_read_only(self):
        cycle = "2026-08-15T15:30"
        producer = {
            "contract_version": 1,
            "request_id": "e" * 32,
            "profile": "live",
            "cycle": cycle,
            "db_root": str(stage_runner.DB_ROOT.resolve()),
            "status": "needs_human",
            "applied": False,
            "p0": False,
            "blocking": True,
            "findings": [{"kind": "GHOST-EXACT"}],
            "healed": [],
            "needs_human": [{"kind": "GHOST-EXACT"}],
            "rc": 1,
        }
        fake_trigger = mock.Mock()
        fake_trigger._autoheal_ledger.return_value = producer
        with mock.patch.dict(sys.modules, {"trigger_agent": fake_trigger}):
            result = stage_runner._run_live_report_reconcile_barrier(
                cycle, allow_apply=False)
        fake_trigger._autoheal_ledger.assert_called_once_with(
            "live", cycle, apply_enabled_override=False)
        self.assertFalse(result["apply_authorized"])
        self.assertFalse(result["report_safe"])

    def test_live_terminal_nudge_runs_only_after_lease_release(self):
        cycle = "2026-08-14T04:15"
        nudge = mock.Mock(return_value={"nudged": True, "reason": "ok"})
        with mock.patch.object(
                stage_runner, "_nudge_mod", mock.Mock(nudge=nudge)):
            skipped = stage_runner._nudge_after_live_release(cycle, False)
            sent = stage_runner._nudge_after_live_release(cycle, True)
        self.assertEqual(
            {"nudged": False, "reason": "profile_lease_not_released"},
            skipped,
        )
        self.assertTrue(sent["nudged"])
        nudge.assert_called_once_with(
            f"stage_runner:live_terminal:{cycle}")

    def test_live_terminal_nudge_failure_is_nonfatal(self):
        with mock.patch.object(
                stage_runner, "_nudge_mod",
                mock.Mock(nudge=mock.Mock(side_effect=RuntimeError("boom")))):
            result = stage_runner._nudge_after_live_release(
                "2026-08-14T04:15", True)
        self.assertFalse(result["nudged"])
        self.assertEqual("nudge_error: RuntimeError", result["reason"])

    def test_main_publishes_stopping_before_profile_lease_release(self):
        cycle = "2026-08-15T21:45"
        events: list[str] = []
        real_write = stage_runner._write_status

        def record_status(path, payload):
            events.append(f"status:{payload.get('status')}")
            real_write(path, payload)

        def child(_stage, _cycle, _command, *, now=None,
                  terminal_callback=None):
            self.assertIsNotNone(terminal_callback)
            terminal_callback({
                "child_returncode": 0,
                "child_timed_out": False,
                "observed_stop_reason": "business_terminal_committed",
            })
            return {
                "returncode": 0,
                "timed_out": False,
                "started": True,
                "budget_seconds": 300.0,
            }

        def release(*_args, **_kwargs):
            events.append("lease:release")
            return True

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
                mock.patch.object(stage_runner, "_write_status",
                                  side_effect=record_status), \
                mock.patch.object(stage_runner, "_run_stage_child",
                                  side_effect=child), \
                mock.patch.object(stage_runner, "verify_business_output",
                                  return_value={"ok": True, "checks": []}), \
                mock.patch.object(
                    stage_runner, "_run_live_report_reconcile_barrier",
                    return_value={"required": False, "report_safe": True}), \
                mock.patch.object(stage_runner.ledger,
                                  "release_profile_lease",
                                  side_effect=release), \
                mock.patch.object(
                    stage_runner, "_nudge_after_live_release",
                    return_value={"nudged": False, "reason": "isolated"}), \
                mock.patch.object(sys, "argv", [
                    "stage_runner.py", "--stage", "live", "--cycle", cycle,
                    "--mode", "unified", "--", "agent",
                ]):
            rc = stage_runner.main()

        self.assertEqual(0, rc)
        self.assertLess(
            events.index("status:stopping"), events.index("lease:release"))

    def test_main_reverifies_business_terminal_after_reconcile(self):
        cycle = "2026-09-03T07:45"
        initial = {
            "ok": True,
            "checks": [],
            "business_terminal": {
                "schema_version": 1,
                "cycle_id": cycle,
                "status": "completed",
                "completed_at_cst": "2026-09-03 07:53:08",
            },
        }
        broken = {
            "ok": False,
            "failure_kind": "business_verification_error",
            "error": "business_terminal proof missing",
            "checks": [],
        }

        def child(_stage, _cycle, _command, *, now=None,
                  terminal_callback=None):
            terminal_callback({
                "child_returncode": 0,
                "child_timed_out": False,
                "observed_stop_reason": "business_terminal_committed",
            })
            return {
                "returncode": 0,
                "timed_out": False,
                "started": True,
                "budget_seconds": 300.0,
            }

        verify = mock.Mock(side_effect=[initial, broken])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
                mock.patch.object(stage_runner, "_run_stage_child",
                                  side_effect=child), \
                mock.patch.object(stage_runner, "verify_business_output",
                                  verify), \
                mock.patch.object(
                    stage_runner, "_run_live_report_reconcile_barrier",
                    return_value={
                        "required": True, "status": "applied",
                        "report_safe": True,
                    }), \
                mock.patch.object(
                    stage_runner, "build_complete_cycle_sla",
                    return_value={
                        "status": "NOT_MET", "reason": "live_stage_failed",
                        "strict_cycle_pass": False,
                    }), \
                mock.patch.object(stage_runner.ledger,
                                  "release_profile_lease",
                                  return_value=True), \
                mock.patch.object(
                    stage_runner, "_nudge_after_live_release",
                    return_value={"nudged": False, "reason": "isolated"}), \
                mock.patch.object(
                    stage_runner, "_run_zero_open_watchdog",
                    return_value={
                        "observed_natural_slots": 1,
                        "strict_business_success_slots": 0,
                        "failed_natural_slots": 1,
                    }), \
                mock.patch.object(stage_runner, "_send_failure_alert",
                                  return_value={"sent": False}), \
                mock.patch.object(sys, "argv", [
                    "stage_runner.py", "--stage", "live", "--cycle", cycle,
                    "--mode", "unified", "--", "agent",
                ]):
            rc = stage_runner.main()
            status = json.loads((
                Path(tmp) / "live-2026-09-03T07-45.json"
            ).read_text(encoding="utf-8"))

        self.assertEqual(stage_runner._BUSINESS_FAILURE_RC, rc)
        self.assertEqual(2, verify.call_count)
        self.assertEqual(
            "post_reconcile_business_verification_error",
            status["failure_kind"],
        )
        self.assertEqual(broken, status["post_reconcile_business_check"])
        self.assertEqual("failed", status["status"])
        self.assertEqual(
            1, status["zero_open_watchdog"]["failed_natural_slots"])

    def test_main_missing_business_terminal_aborts_before_finalize_release(self):
        cycle = "2026-08-15T21:45"
        events: list[str] = []
        real_write = stage_runner._write_status
        missing = {
            "ok": False,
            "failure_kind": "business_output_missing",
            "checks": [
                {"db": "analysis.db", "table": "analysis_runs",
                 "found": False},
                {"db": "live_trades.db", "table": "trade_cycles",
                 "found": False},
            ],
        }

        def record_status(path, payload):
            events.append(f"status:{payload.get('status')}")
            real_write(path, payload)

        def child(_stage, _cycle, _command, *, now=None,
                  terminal_callback=None):
            terminal_callback({
                "child_returncode": 0,
                "child_timed_out": False,
                "observed_stop_reason": None,
            })
            return {
                "returncode": 0,
                "timed_out": False,
                "started": True,
                "budget_seconds": 300.0,
            }

        def abort(_stage, _cycle):
            events.append("abort")
            return {"terminal_confirmed": True, "status": "no-active-run"}

        def release(*_args, **_kwargs):
            events.append("lease:release")
            return True

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
                mock.patch.object(stage_runner, "_write_status",
                                  side_effect=record_status), \
                mock.patch.object(stage_runner, "_run_stage_child",
                                  side_effect=child), \
                mock.patch.object(stage_runner, "verify_business_output",
                                  return_value=missing), \
                mock.patch.object(
                    stage_runner, "_settle_late_live_business_output",
                    return_value=(missing, None)), \
                mock.patch.object(stage_runner, "_abort_gateway_session",
                                  side_effect=abort), \
                mock.patch.object(stage_runner,
                                  "detect_agent_terminal_failure",
                                  return_value=None), \
                mock.patch.object(
                    stage_runner, "_run_live_report_reconcile_barrier",
                    return_value={"required": False, "report_safe": True}), \
                mock.patch.object(stage_runner.ledger,
                                  "release_profile_lease",
                                  side_effect=release), \
                mock.patch.object(
                    stage_runner, "_nudge_after_live_release",
                    return_value={"nudged": False, "reason": "isolated"}), \
                mock.patch.object(stage_runner, "_send_failure_alert",
                                  return_value={"sent": False}), \
                mock.patch.object(sys, "argv", [
                    "stage_runner.py", "--stage", "live", "--cycle", cycle,
                    "--mode", "unified", "--", "agent",
                ]):
            rc = stage_runner.main()

        self.assertEqual(stage_runner._BUSINESS_FAILURE_RC, rc)
        self.assertLess(events.index("status:stopping"), events.index("abort"))
        self.assertLess(events.index("abort"), events.index("status:failed"))
        self.assertLess(events.index("status:failed"),
                        events.index("lease:release"))

    def test_agent_cli_protocol_failure_requires_blocked_error_terminal(self):
        failed = json.dumps({
            "status": "ok",
            "result": {
                "payloads": [{"text": "准备写 analysis"}],
                "meta": {
                    "replayInvalid": True,
                    "livenessState": "blocked",
                    "stopReason": "error",
                    "completion": {
                        "stopReason": "error",
                        "finishReason": "error",
                    },
                    "toolSummary": {"calls": 7},
                    "finalAssistantVisibleText": "准备写 analysis",
                },
            },
        })
        evidence = stage_runner._agent_cli_protocol_failure(failed)
        self.assertEqual("blocked", evidence["liveness_state"])
        self.assertEqual("error", evidence["stop_reason"])
        self.assertEqual(7, evidence["tool_calls"])
        self.assertEqual("准备写 analysis", evidence["final_assistant_text"])

        successful = json.dumps({
            "status": "ok",
            "result": {"meta": {
                "replayInvalid": True,
                "livenessState": "working",
                "stopReason": "stop",
                "completion": {"finishReason": "stop"},
            }},
        })
        self.assertIsNone(
            stage_runner._agent_cli_protocol_failure(successful))

    def test_main_agent_protocol_terminal_skips_writer_race_wait(self):
        cycle = "2026-08-15T21:45"
        missing = {
            "ok": False,
            "failure_kind": "business_output_missing",
            "checks": [{
                "db": "analysis.db",
                "table": "analysis_runs",
                "found": False,
            }],
        }
        protocol = {
            "schema_version": 1,
            "replay_invalid": True,
            "liveness_state": "blocked",
            "stop_reason": "error",
            "finish_reason": "error",
            "tool_calls": 7,
            "final_assistant_text": "准备一次写完整 analysis",
            "business_semantics": (
                "agent_stopped_before_required_writer_terminal"),
        }

        def child(_stage, _cycle, _command, *, now=None,
                  terminal_callback=None):
            terminal_callback({
                "child_returncode": 0,
                "child_timed_out": False,
                "observed_stop_reason": None,
            })
            return {
                "returncode": 0,
                "timed_out": False,
                "started": True,
                "budget_seconds": 300.0,
                "agent_protocol_evidence": protocol,
            }

        settle = mock.Mock()
        placeholder = mock.Mock(return_value={"written": True})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
                mock.patch.object(stage_runner, "_run_stage_child",
                                  side_effect=child), \
                mock.patch.object(stage_runner, "verify_business_output",
                                  return_value=missing), \
                mock.patch.object(
                    stage_runner, "_settle_late_live_business_output", settle), \
                mock.patch.object(stage_runner, "_abort_gateway_session",
                                  return_value={
                                      "terminal_confirmed": True,
                                      "status": "no-active-run",
                                  }), \
                mock.patch.object(
                    stage_runner, "_run_live_report_reconcile_barrier",
                    return_value={"required": False, "report_safe": True}), \
                mock.patch.object(stage_runner.ledger,
                                  "release_profile_lease", return_value=True), \
                mock.patch.object(
                    stage_runner, "_nudge_after_live_release",
                    return_value={"nudged": False, "reason": "isolated"}), \
                mock.patch.object(stage_runner, "_send_failure_alert",
                                  return_value={"sent": False}), \
                mock.patch("analyst_writer.commit_deadline_placeholder",
                           placeholder), \
                mock.patch.object(sys, "argv", [
                    "stage_runner.py", "--stage", "live", "--cycle", cycle,
                    "--mode", "unified", "--", "agent",
                ]):
            rc = stage_runner.main()
            status = json.loads((
                Path(tmp) / "live-2026-08-15T21-45.json"
            ).read_text(encoding="utf-8"))

        self.assertEqual(stage_runner._BUSINESS_FAILURE_RC, rc)
        settle.assert_not_called()
        placeholder.assert_called_once()
        self.assertEqual("agent_protocol_error", status["failure_kind"])
        self.assertTrue(status["analysis_placeholder_written"])
        self.assertFalse(status["business_output_settle"]["attempted"])
        self.assertEqual(
            protocol,
            status["agent_terminal_evidence"]["agent_protocol_evidence"],
        )

    pass

    def test_length_terminal_is_classified_without_model_chain_metadata(self):
        cycle = "2026-07-28T16:45"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = (
                root / "agents" / "okx-live-trader" / "sessions"
            )
            session_dir.mkdir(parents=True)
            session_id = "test-session"
            (session_dir / "sessions.json").write_text(
                json.dumps({
                    "agent:okx-live-trader:live-20260728-1645": {
                        "sessionId": session_id,
                        "model": "must-not-be-emitted",
                    }
                }),
                encoding="utf-8",
            )
            records = [
                {
                    "type": "trace.items",
                    "data": {
                        "messages": [{
                            "provider": "must-not-be-emitted",
                            "model": "must-not-be-emitted",
                            "stopReason": "length",
                            "usage": {"totalTokens": 117740},
                        }]
                    },
                },
                {
                    "type": "trace.artifacts",
                    "data": {
                        "terminalError": "non_deliverable_terminal_turn"
                    },
                },
            ]
            (session_dir / f"{session_id}.trajectory.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            result = stage_runner.detect_agent_terminal_failure(
                "live", cycle, root)

        self.assertEqual(result["failure_kind"], "model_output_length")
        self.assertEqual(result["stop_reason"], "length")
        self.assertEqual(result["total_tokens"], 117740)
        serialized = json.dumps(result).lower()
        self.assertNotIn("model", serialized.replace("model_output_length", ""))
        self.assertNotIn("provider", serialized)

    def test_empty_terminal_is_classified_without_model_chain_metadata(self):
        cycle = "2026-08-12T20:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = (
                root / "agents" / "okx-live-trader" / "sessions"
            )
            session_dir.mkdir(parents=True)
            session_id = "empty-output-session"
            (session_dir / "sessions.json").write_text(
                json.dumps({
                    "agent:okx-live-trader:live-20260812-2000": {
                        "sessionId": session_id,
                        "model": "must-not-be-emitted",
                    }
                }),
                encoding="utf-8",
            )
            (session_dir / f"{session_id}.trajectory.jsonl").write_text(
                json.dumps({"type": "session.ended"}) + "\n",
                encoding="utf-8",
            )
            records = [
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working"}],
                        "provider": "must-not-be-emitted",
                        "model": "must-not-be-emitted",
                        "usage": {"output": 4, "totalTokens": 10},
                        "stopReason": "toolUse",
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "provider": "must-not-be-emitted",
                        "model": "must-not-be-emitted",
                        "usage": {"output": 0, "totalTokens": 0},
                        "stopReason": "stop",
                    },
                },
            ]
            (session_dir / f"{session_id}.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            result = stage_runner.detect_agent_terminal_failure(
                "live", cycle, root)

        self.assertEqual(result, {
            "failure_kind": "model_empty_output",
            "stop_reason": "stop",
            "content_blocks": 0,
            "output_tokens": 0,
        })
        serialized = json.dumps(result).lower()
        self.assertNotIn("provider", serialized)
        self.assertNotIn("must-not-be-emitted", serialized)

    def test_idle_timeout_is_classified_without_model_chain_metadata(self):
        cycle = "2026-08-13T03:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = (
                root / "agents" / "okx-live-trader" / "sessions"
            )
            session_dir.mkdir(parents=True)
            session_id = "idle-timeout-session"
            (session_dir / "sessions.json").write_text(
                json.dumps({
                    "agent:okx-live-trader:live-20260813-0300": {
                        "sessionId": session_id,
                        "model": "must-not-be-emitted",
                        "provider": "must-not-be-emitted",
                    }
                }),
                encoding="utf-8",
            )
            trajectory = [
                {
                    "type": "model.completed",
                    "data": {
                        "timedOut": True,
                        "idleTimedOut": True,
                        "promptError": "must-not-be-emitted",
                        "model": "must-not-be-emitted",
                    },
                },
                {
                    "type": "model.fallback_step",
                    "data": {
                        "fallbackStepFromModel": "must-not-be-emitted",
                        "fallbackStepToModel": "must-not-be-emitted",
                    },
                },
                {
                    "type": "session.ended",
                    "data": {
                        "timedOut": True,
                        "idleTimedOut": True,
                        "externalAbort": True,
                    },
                },
            ]
            (session_dir / f"{session_id}.trajectory.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in trajectory),
                encoding="utf-8",
            )
            (session_dir / f"{session_id}.jsonl").write_text(
                json.dumps({
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "aborted",
                    },
                }) + "\n",
                encoding="utf-8",
            )

            result = stage_runner.detect_agent_terminal_failure(
                "live", cycle, root)

        self.assertEqual(result, {
            "failure_kind": "agent_idle_timeout",
            "timed_out": True,
            "idle_timed_out": True,
            "external_abort_observed": True,
            "fallback_observed": True,
            "timeout_terminal_records": 2,
        })
        serialized = json.dumps(result).lower()
        self.assertNotIn("provider", serialized)
        self.assertNotIn("must-not-be-emitted", serialized)

    def test_nonempty_normal_stop_is_not_empty_terminal(self):
        cycle = "2026-08-12T20:15"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = (
                root / "agents" / "okx-live-trader" / "sessions"
            )
            session_dir.mkdir(parents=True)
            session_id = "normal-session"
            (session_dir / "sessions.json").write_text(
                json.dumps({
                    "agent:okx-live-trader:live-20260812-2015": {
                        "sessionId": session_id,
                    }
                }),
                encoding="utf-8",
            )
            (session_dir / f"{session_id}.trajectory.jsonl").write_text(
                json.dumps({"type": "session.ended"}) + "\n",
                encoding="utf-8",
            )
            (session_dir / f"{session_id}.jsonl").write_text(
                json.dumps({
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                        "usage": {"output": 1},
                        "stopReason": "stop",
                    },
                }) + "\n",
                encoding="utf-8",
            )
            result = stage_runner.detect_agent_terminal_failure(
                "live", cycle, root)

        self.assertIsNone(result)

    def test_unified_live_requires_trade_cycle_after_ok_analysis(self):
        cycle = "2026-07-27T02:45"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_analysis_db(root / "analysis.db", cycle, "ok")
            _create_trade_db(root / "live_trades.db")
            missing = stage_runner.verify_business_output(
                "live", cycle, "unified", root)
            self.assertFalse(missing["ok"])
            self.assertEqual(
                missing["failure_kind"], "business_output_missing")

            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-07-27 02:53:37", "live", "traded",
                     2, None, "", "{}"),
                )
                con.commit()
            finally:
                con.close()
            complete = stage_runner.verify_business_output(
                "live", cycle, "unified", root)
            self.assertTrue(complete["ok"])

    def test_unified_stale_analysis_is_valid_no_trade_terminal(self):
        cycle = "2026-07-27T03:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_analysis_db(root / "analysis.db", cycle, "stale")
            result = stage_runner.verify_business_output(
                "live", cycle, "unified", root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["terminal"], "analysis_stale")

    def test_live_requires_trade_cycle(self):
        # 原用 demo stage 驱动；2026-08-06 demo 下线后改打 live（机制不变）。
        with tempfile.TemporaryDirectory() as tmp:
            _create_trade_db(Path(tmp) / "live_trades.db")
            result = stage_runner.verify_business_output(
                "live", "2026-07-27T02:45", "full", Path(tmp))
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["failure_kind"], "business_output_missing")

    def test_trade_error_or_inconsistent_order_count_is_not_success(self):
        cycle = "2026-07-27T04:00"
        for decision, n_orders in (("error", 0), ("traded", 0), ("hold", 1)):
            with self.subTest(decision=decision, n_orders=n_orders):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    _create_trade_db(root / "live_trades.db")
                    con = sqlite3.connect(root / "live_trades.db")
                    try:
                        con.execute(
                            "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                            (cycle, "2026-07-27 04:01:00", "live",
                             decision, n_orders, None, "", "{}"),
                        )
                        con.commit()
                    finally:
                        con.close()
                    result = stage_runner.verify_business_output(
                        "live", cycle, "full", root)
                self.assertFalse(result["ok"], result)
                self.assertEqual(
                    result["failure_kind"], "business_verification_error")

    def test_hold_zero_orders_is_valid_trade_terminal(self):
        cycle = "2026-07-27T04:15"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_trade_db(root / "live_trades.db")
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-07-27 04:16:00", "demo",
                     "hold", 0, None, "", "{}"),
                )
                con.commit()
            finally:
                con.close()
            result = stage_runner.verify_business_output(
                "live", cycle, "full", root)
        self.assertTrue(result["ok"], result)

    def test_v4_business_output_requires_and_returns_terminal_marker(self):
        cycle = "2026-08-21T18:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_analysis_db(
                root / "analysis.db", cycle, "ok", "2026-08-21 18:10:00")
            _create_trade_db(root / "live_trades.db")
            receipt = {
                "batch_status": "completed",
                "business_terminal": {
                    "schema_version": 1,
                    "cycle_id": cycle,
                    "status": "completed",
                    "completed_at_cst": "2026-08-21 18:14:29",
                },
            }
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-21 18:14:50", "live", "hold", 0,
                     None, "", json.dumps(receipt)),
                )
                con.commit()
            finally:
                con.close()
            result = stage_runner.verify_business_output(
                "live", cycle, "unified", root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            "2026-08-21 18:14:29",
            result["business_terminal"]["completed_at_cst"],
        )

    def test_partial_position_action_batch_is_not_success_terminal(self):
        cycle = "2026-08-15T15:15"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_trade_db(root / "live_trades.db")
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (
                        cycle, "2026-08-15 15:24:00", "live", "traded", 1,
                        None, "", json.dumps({"batch_status": "partial"}),
                    ),
                )
                con.commit()
            finally:
                con.close()
            result = stage_runner.verify_business_output(
                "live", cycle, "full", root)
        self.assertFalse(result["ok"], result)
        self.assertEqual(
            result["failure_kind"], "business_verification_error")
        self.assertIn("batch_status=partial", result["error"])


class MonitoringAndAlertTests(unittest.TestCase):
    def test_post_push_monitor_detects_rc0_over_closed(self):
        out = "[OVER_CLOSED] 1 组:\n  LTC-USDT-SWAP long net=-2.8\n"
        result = live_reconcile_monitor.evaluate(0, out)
        self.assertTrue(result["issue"])
        self.assertIn("LTC-USDT-SWAP long net=-2.8", result["findings"])

    def test_post_push_monitor_clean_result(self):
        result = live_reconcile_monitor.evaluate(
            0, "结论: 无幽灵仓（账本 ≤ 现仓）✓")
        self.assertTrue(result["ok"])
        self.assertFalse(result["issue"])

    def test_post_push_monitor_skips_active_live_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp)
            (status_dir / "live-2026-07-27T10-30.json").write_text(
                json.dumps({
                    "status": "running",
                    "cycle_id": "2026-07-27T10:30",
                    "started_at": live_reconcile_monitor.now_cst().strftime(
                        "%Y-%m-%d %H:%M:%S"),
                }),
                encoding="utf-8",
            )
            with mock.patch.object(
                    live_reconcile_monitor, "STAGE_STATUS_DIR", status_dir):
                result = live_reconcile_monitor.active_live_runner()
        self.assertEqual(result["cycle_id"], "2026-07-27T10:30")

    def test_post_push_monitor_supports_demo_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp)
            (status_dir / "demo-2026-07-27T10-45.json").write_text(
                json.dumps({
                    "status": "running",
                    "cycle_id": "2026-07-27T10:45",
                    "started_at": live_reconcile_monitor.now_cst().strftime(
                        "%Y-%m-%d %H:%M:%S"),
                }),
                encoding="utf-8",
            )
            with mock.patch.object(
                    live_reconcile_monitor, "STAGE_STATUS_DIR", status_dir):
                result = live_reconcile_monitor.active_runner("demo")
        self.assertEqual(result["cycle_id"], "2026-07-27T10:45")

    def test_business_missing_runner_maps_to_run_ok_no_db_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp)
            path = status_dir / "live-2026-07-27T02-45.json"
            path.write_text(json.dumps({
                "status": "failed",
                "failure_kind": "business_output_missing",
                "child_returncode": 0,
                "returncode": 86,
            }), encoding="utf-8")
            with mock.patch.object(
                    collection_monitor, "STAGE_STATUS_DIR", status_dir):
                result = collection_monitor._audit_attribution(
                    "live", "2026-07-27T02:45")
        self.assertEqual(result, "run-ok-no-db-row")

    def test_empty_output_runner_maps_to_run_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp)
            path = status_dir / "live-2026-08-12T20-00.json"
            path.write_text(json.dumps({
                "status": "failed",
                "failure_kind": "model_empty_output",
                "child_returncode": 0,
                "returncode": 86,
            }), encoding="utf-8")
            with mock.patch.object(
                    collection_monitor, "STAGE_STATUS_DIR", status_dir):
                result = collection_monitor._audit_attribution(
                    "live", "2026-08-12T20:00")
        self.assertEqual(result, "run-failed")

    def test_findings_keeps_over_closed_symbol_but_not_ghost_diagnostics(self):
        out = """
[OVER_CLOSED] 1 组:
  LTC-USDT-SWAP long net=-2.8

[GHOST-EXACT] WLD-USDT-SWAP short sz=400
  窗口起点 2026-07-25 12:27:51
"""
        findings = reconcile_daily._findings(out)
        self.assertIn("LTC-USDT-SWAP long net=-2.8", findings)
        self.assertIn("[GHOST-EXACT] WLD-USDT-SWAP", findings)
        self.assertNotIn("窗口起点", findings)

    def test_mixed_live_classification_mentions_manual_item(self):
        out = (
            "[OVER_CLOSED] 1 组:\n  LTC-USDT-SWAP long net=-2.8\n"
            "[GHOST-EXACT] WLD-USDT-SWAP short sz=400\n"
        )
        label = reconcile_daily._live_classification(1, out)
        self.assertIn("GHOST-EXACT 可补", label)
        self.assertIn("OVER_CLOSED 缺 open 需人工", label)


class DailyReportCorrectionTests(unittest.TestCase):
    def test_correction_preserves_identity_and_updates_both_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "account.db"
            con = sqlite3.connect(db)
            try:
                con.execute(
                    "CREATE TABLE daily_reports("
                    "trade_day_num INTEGER,ts TEXT,profile TEXT,"
                    "open_count INTEGER,close_count INTEGER,total_pnl REAL,"
                    "total_fees REAL,best_trade TEXT,worst_trade TEXT,"
                    "summary TEXT,lessons TEXT,raw TEXT,"
                    "PRIMARY KEY(ts,profile))")
                for profile, pnl in (("live", -1.862), ("demo", -14.554)):
                    con.execute(
                        "INSERT INTO daily_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (64, "2026-07-27 08:05:00", profile, 2, 5, pnl,
                         0.0, "old best", "old worst", "old", "old", "{}"),
                    )
                before = con.execute(
                    "SELECT rowid,trade_day_num,profile FROM daily_reports "
                    "ORDER BY rowid").fetchall()
                payload = {
                    "ts": "2026-07-27 08:05:00",
                    "live_open_count": 3, "live_close_count": 7,
                    "live_total_pnl": -9.2965,
                    "live_total_fees": 0.0,
                    "live_best_trade": "ALLO +3.2637",
                    "live_worst_trade": "BEAT -5.1542",
                    "demo_open_count": 3, "demo_close_count": 2,
                    "demo_total_pnl": -14.554,
                    "demo_total_fees": 0.0,
                    "demo_best_trade": "LTC -5.874",
                    "demo_worst_trade": "AAVE -8.680",
                    "summary": "corrected", "lessons": "corrected",
                    "raw": "{\"corrected\":true}",
                }
                result = daily_report_writer.correct_existing_daily(
                    con, payload, ["live", "demo"], True)
                con.commit()
                after = con.execute(
                    "SELECT rowid,trade_day_num,profile,open_count,close_count,"
                    "total_pnl FROM daily_reports ORDER BY rowid").fetchall()
            finally:
                con.close()
        self.assertTrue(result["applied"])
        self.assertEqual([row[:3] for row in after], before)
        self.assertEqual(after[0][3:], (3, 7, -9.2965))
        self.assertEqual(after[1][3:], (3, 2, -14.554))


class TradeReportFactTests(unittest.TestCase):
    def test_daily_window_is_fixed_trailing_24h(self):
        start, end = trade_report_stats.daily_window(
            "2026-07-31 08:05:00")
        self.assertEqual(start, "2026-07-30 08:00:00")
        self.assertEqual(end, "2026-07-31 08:00:00")

    def test_daily_window_ignores_report_ts_jitter(self):
        """报告 ts 抖动不得移动事实窗，否则相邻日报会缺口/重叠。"""
        expected = ("2026-07-30 08:00:00", "2026-07-31 08:00:00")
        for jittered in (
            "2026-07-31 08:00:00",
            "2026-07-31 08:05:00",
            "2026-07-31 08:06:00",
            "2026-07-31 08:36:16",
            "2026-07-31 23:59:00",
        ):
            with self.subTest(ts=jittered):
                self.assertEqual(
                    trade_report_stats.daily_window(jittered), expected)

    def test_daily_window_before_anchor_reports_last_complete_window(self):
        start, end = trade_report_stats.daily_window(
            "2026-07-31 07:30:00")
        self.assertEqual(start, "2026-07-29 08:00:00")
        self.assertEqual(end, "2026-07-30 08:00:00")

    def test_consecutive_daily_windows_tile_exactly(self):
        _, prev_end = trade_report_stats.daily_window("2026-07-30 08:12:00")
        next_start, _ = trade_report_stats.daily_window("2026-07-31 08:05:00")
        self.assertEqual(prev_end, next_start)

    def test_daily_prepare_uses_half_open_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_db = root / "live_trades.db"
            demo_db = root / "demo_trades.db"
            ledger_db = root / "ledger.db"
            _create_trade_db(live_db)
            _create_trade_db(demo_db)
            _create_ledger_db(ledger_db)
            con = sqlite3.connect(live_db)
            try:
                for cycle, ts in (
                    ("before", "2026-07-30 07:59:59"),
                    ("start", "2026-07-30 08:00:00"),
                    ("inside", "2026-07-31 07:59:59"),
                    ("end", "2026-07-31 08:00:00"),
                ):
                    con.execute(
                        "INSERT INTO trades("
                        "cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (cycle, ts, "BTC-USDT-SWAP", "open", "long",
                         1, 10, 0, '{"ok":true}'),
                    )
                con.commit()
            finally:
                con.close()
            with (
                mock.patch.object(
                    daily_report_writer, "LIVE_TRADES_DB", live_db),
                mock.patch.object(
                    daily_report_writer, "LEDGER_DB", ledger_db),
            ):
                prepared = daily_report_writer.prepare_daily_payload({
                    "ts": "2026-07-31 08:05:00",
                    "live_reconcile_status": "clean",
                    "live_reconcile_issue_count": 0,
                })

        self.assertEqual(prepared["period_start_ts"],
                         "2026-07-30 08:00:00")
        self.assertEqual(prepared["period_end_ts"],
                         "2026-07-31 08:00:00")
        self.assertTrue(prepared["period_end_exclusive"])
        self.assertEqual(prepared["live_open_count"], 2)

    def test_fill_counts_exclude_rejects_and_risk_rejects_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trade_db = root / "demo_trades.db"
            ledger_db = root / "ledger.db"
            _create_trade_db(trade_db)
            _create_ledger_db(ledger_db)
            con = sqlite3.connect(trade_db)
            try:
                rows = [
                    ("c1", "2026-07-27 01:00:00", "LTC-USDT-SWAP",
                     "open", "long", 3.0, 50.0, 0.0, '{"ok":true}'),
                    ("c2", "2026-07-27 02:00:00", "LTC-USDT-SWAP",
                     "close", "long", 3.0, 49.0, -3.0, '{"ok":true}'),
                    ("c3", "2026-07-27 03:00:00", "UNI-USDT-SWAP",
                     "open", "long", None, None, None,
                     '{"status":"rejected","ok":false}'),
                    ("c4", "2026-07-27 04:00:00", "AAVE-USDT-SWAP",
                     "open_long", "long", None, None, None,
                     '{"action_taken":"REJECT"}'),
                ]
                con.executemany(
                    "INSERT INTO trades("
                    "cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                con.commit()
            finally:
                con.close()
            con = sqlite3.connect(ledger_db)
            try:
                intents = [
                    ("demo", "c3", "UNI-USDT-SWAP", "open", "long",
                     "f1", "{}", "failed_clean", "2026-07-27 03:00:00",
                     "2026-07-27 03:00:01", None, None, None, None,
                     "risk_reject:available_margin_infeasible"),
                    ("demo", "c4", "AAVE-USDT-SWAP", "open", "long",
                     "f2", "{}", "failed_clean", "2026-07-27 04:00:00",
                     "2026-07-27 04:00:01", None, None, None, None,
                     "risk_reject:single_trade_margin_exceeded"),
                    ("demo", "c5", "AVAX-USDT-SWAP", "open", "long",
                     "f3", "{}", "failed_clean", "2026-07-27 05:00:00",
                     "2026-07-27 05:00:01", None, None, None, None,
                     "place_timeout_confirmed_no_fill"),
                ]
                con.executemany(
                    "INSERT INTO execution_intents VALUES("
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    intents,
                )
                con.commit()
            finally:
                con.close()

            stats = trade_report_stats.profile_statistics(
                "demo", trade_db, ledger_db,
                "2026-07-27 00:00:00", "2026-07-27 08:05:00")

        self.assertEqual(stats["open_count"], 1)
        self.assertEqual(stats["close_count"], 1)
        self.assertEqual(stats["realized_pnl"], -3.0)
        self.assertEqual(stats["excluded_rejected_rows"], 1)
        self.assertEqual(
            stats["risk_rejected_open_attempts"]["count"], 2)
        self.assertNotIn(
            "place_timeout_confirmed_no_fill",
            stats["risk_rejected_open_attempts"]["reasons"],
        )

    def test_writer_rejects_nonfills_but_allows_explicit_unconfirmed_close(self):
        rejected = {
            "cycle_id": "c1",
            "decision": "traded",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "decision_card": _valid_card(),
            "trades": [{
                "symbol": "UNI-USDT-SWAP", "action": "open",
                "side": "long", "status": "rejected", "ok": False,
            }],
        }
        errors = trades_writer.validate(rejected)
        self.assertTrue(any("不得写入成交表" in error for error in errors))

        invalid_label = {
            "cycle_id": "c2",
            "decision": "traded",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "decision_card": _valid_card(),
            "trades": [{
                "symbol": "UNI-USDT-SWAP", "action": "open_long",
                "side": "long",
            }],
        }
        self.assertTrue(any(
            "非成交动作" in error
            for error in trades_writer.validate(invalid_label)
        ))

        unconfirmed_close = {
            "cycle_id": "c3",
            "decision": "traded",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "decision_card": _valid_card(),
            "trades": [{
                "symbol": "UNI-USDT-SWAP", "action": "close",
                "side": "long", "sz": 10,
                "fill_px": None, "pnl": None,
                "fill_source": "unconfirmed",
            }],
        }
        self.assertEqual(trades_writer.validate(unconfirmed_close), [])

    def test_daily_prepare_corrects_counts_and_marks_pending_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_db = root / "live_trades.db"
            demo_db = root / "demo_trades.db"
            ledger_db = root / "ledger.db"
            _create_trade_db(live_db)
            _create_trade_db(demo_db)
            _create_ledger_db(ledger_db)
            for path, profile in ((live_db, "live"), (demo_db, "demo")):
                con = sqlite3.connect(path)
                try:
                    con.execute(
                        "INSERT INTO trades("
                        "cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"{profile}-1", "2026-07-27 02:00:00",
                         "LTC-USDT-SWAP", "open", "long", 2, 50,
                         0, '{"ok":true}'),
                    )
                    con.commit()
                finally:
                    con.close()
            con = sqlite3.connect(ledger_db)
            try:
                con.execute(
                    "INSERT INTO execution_intents VALUES("
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("live", "d-reject", "UNI-USDT-SWAP", "open",
                     "long", "f", "{}", "failed_clean",
                     "2026-07-27 03:00:00", "2026-07-27 03:00:01",
                     None, None, None, None,
                     "risk_reject:available_margin_infeasible"),
                )
                con.commit()
            finally:
                con.close()
            payload = {
                "ts": "2026-07-27 08:05:00",
                "live_open_count": 9,
                "live_close_count": 0,
                "live_total_pnl": 0,
                "live_reconcile_status": "pending",
                "live_reconcile_issue_count": 2,
                "raw": "{\"origin\":\"test\"}",
            }
            with (
                mock.patch.object(
                    daily_report_writer, "LIVE_TRADES_DB", live_db),
                mock.patch.object(
                    daily_report_writer, "LEDGER_DB", ledger_db),
            ):
                prepared = daily_report_writer.prepare_daily_payload(payload)

        self.assertEqual(prepared["live_open_count"], 1)
        self.assertEqual(prepared["live_risk_rejected_open_count"], 1)
        # demo_* 断言随 2026-08-06 demo 全量下线移除（prepare 只再统计 live）。
        self.assertNotIn("demo_open_count", prepared)
        self.assertEqual(prepared["report_status"], "provisional")
        self.assertIn("成交统计已按有效 fill 自动校正",
                      prepared["anomalies"])
        audit = json.loads(prepared["raw"])["report_audit"]
        self.assertEqual(audit["report_state"]["status"], "provisional")

    def test_weekly_window_is_previous_complete_monday_to_monday(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_db = root / "live_trades.db"
            demo_db = root / "demo_trades.db"
            ledger_db = root / "ledger.db"
            _create_trade_db(live_db)
            _create_trade_db(demo_db)
            _create_ledger_db(ledger_db)
            # 原夹具把成交写在 demo 库；2026-08-06 demo 下线后统计只看 live。
            con = sqlite3.connect(live_db)
            try:
                for cycle, ts in (
                    ("before", "2026-07-20 07:59:59"),
                    ("start", "2026-07-20 08:00:00"),
                    ("inside", "2026-07-27 07:59:59"),
                    ("end", "2026-07-27 08:00:00"),
                ):
                    con.execute(
                        "INSERT INTO trades("
                        "cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (cycle, ts, "AVAX-USDT-SWAP", "open", "long",
                         1, 10, 0, '{"ok":true}'),
                    )
                con.commit()
            finally:
                con.close()
            with (
                mock.patch.object(
                    daily_report_writer, "LIVE_TRADES_DB", live_db),
                mock.patch.object(
                    daily_report_writer, "LEDGER_DB", ledger_db),
            ):
                prepared = daily_report_writer.prepare_weekly_payload({
                    "week_start_ts": "2026-07-27 00:00:00",
                })

        self.assertEqual(prepared["period_start_ts"],
                         "2026-07-20 08:00:00")
        self.assertEqual(prepared["period_end_ts"],
                         "2026-07-27 08:00:00")
        self.assertEqual(prepared["live_open_count"], 2)

    def test_weekly_window_is_tiled_by_seven_daily_windows(self):
        """七份日报必须恰好平铺周报窗，否则日/周口径无法互相对账。"""
        week_start = "2026-07-27 00:00:00"
        w_start, w_end = trade_report_stats.weekly_window(week_start)
        edges = []
        cursor = datetime.strptime(w_end, "%Y-%m-%d %H:%M:%S")
        for _ in range(7):
            d_start, d_end = trade_report_stats.daily_window(
                cursor.strftime("%Y-%m-%d 08:05:00"))
            edges.append((d_start, d_end))
            cursor -= timedelta(days=1)
        edges.reverse()
        self.assertEqual(edges[0][0], w_start)
        self.assertEqual(edges[-1][1], w_end)
        for earlier, later in zip(edges, edges[1:]):
            self.assertEqual(earlier[1], later[0])
class CompleteCycleSlaTests(unittest.TestCase):
    @staticmethod
    def _monitor(ts: str) -> dict:
        return {
            "rc": 0,
            "output": json.dumps({
                "ts": ts,
                "cycle_id": "2026-08-14T23:30",
                "profile": "live",
                "ok": True,
                "issue": False,
                "rc": 0,
                "markers": [],
            }),
        }

    def test_strictly_under_fourteen_thirty_passes(self):
        result = stage_runner.build_complete_cycle_sla(
            "2026-08-14T23:30", self._monitor("2026-08-14 23:44:29"))
        self.assertEqual("met", result["status"])
        self.assertTrue(result["under_14m30"])
        self.assertEqual(869, result["elapsed_seconds"])

    def test_exactly_fourteen_thirty_is_late(self):
        result = stage_runner.build_complete_cycle_sla(
            "2026-08-14T23:30", self._monitor("2026-08-14 23:44:30"))
        self.assertEqual("late", result["status"])
        self.assertFalse(result["under_14m30"])
        self.assertEqual(870, result["elapsed_seconds"])

    def test_skipped_post_reconcile_is_incomplete(self):
        result = stage_runner.build_complete_cycle_sla(
            "2026-08-14T23:30",
            {"rc": 0, "output": json.dumps({
                "ok": True,
                "issue": False,
                "skipped": "live_runner_active",
            })},
        )
        self.assertEqual("incomplete", result["status"])
        self.assertFalse(result["complete"])

    def test_monitor_json_survives_trailing_warning_text(self):
        monitor = self._monitor("2026-08-14 23:43:59")
        monitor["output"] = "monitor preface\n" + monitor["output"] + (
            "\nwarning: optional notification unavailable"
        )
        result = stage_runner.build_complete_cycle_sla(
            "2026-08-14T23:30", monitor)
        self.assertEqual("met", result["status"])
        self.assertEqual(839, result["elapsed_seconds"])

    def test_failed_live_stage_never_counts_as_complete_cycle(self):
        result = stage_runner.build_complete_cycle_sla(
            "2026-08-14T23:30",
            self._monitor("2026-08-14 23:43:26"),
            live_status={
                "stage": "live",
                "cycle_id": "2026-08-14T23:30",
                "status": "failed",
                "returncode": 124,
                "failure_kind": "cycle_deadline_exceeded",
            },
        )
        self.assertEqual("incomplete", result["status"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["under_14m30"])
        self.assertEqual("live_stage_not_succeeded", result["reason"])
        self.assertEqual(
            "cycle_deadline_exceeded", result["live_failure_kind"])

    @staticmethod
    def _v3_live_status(cycle: str, finished_at: str) -> dict:
        return {
            "stage": "live",
            "cycle_id": cycle,
            "status": "succeeded",
            "returncode": 0,
            "report_reconcile_barrier": {
                "required": True,
                "profile": "live",
                "cycle_id": cycle,
                "status": "ok",
                "rc": 0,
                "contract_valid": True,
                "report_safe": True,
                "started_at": finished_at,
                "finished_at": finished_at,
            },
        }

    def test_v3_clock_stops_at_live_record_reconcile_barrier(self):
        cycle = "2026-08-20T18:00"
        live = self._v3_live_status(cycle, "2026-08-20 18:13:59")
        result = stage_runner.build_complete_cycle_sla(
            cycle, {"rc": 1, "output": "bad"}, live_status=live)
        self.assertEqual(
            "cycle_start_to_successful_live_record_reconcile",
            result["measurement"],
        )
        self.assertEqual(839, result["elapsed_seconds"])
        self.assertTrue(result["under_14m30"])
        self.assertTrue(result["strict_cycle_pass"])
        self.assertTrue(result["record_reconcile_gate"]["met"])

    def test_v3_exact_record_gate_is_a_strict_cycle_failure(self):
        cycle = "2026-08-20T18:00"
        live = self._v3_live_status(cycle, "2026-08-20 18:14:00")
        result = stage_runner.build_complete_cycle_sla(
            cycle, {}, live_status=live)
        self.assertEqual("late", result["status"])
        self.assertEqual(
            "record_reconcile_deadline_exceeded", result["reason"])
        self.assertTrue(result["under_14m30"])
        self.assertFalse(result["strict_cycle_pass"])

    def test_v3_post_push_monitor_remains_an_independent_safety_gate(self):
        cycle = "2026-08-20T18:00"
        live = self._v3_live_status(cycle, "2026-08-20 18:13:00")
        sla = stage_runner.build_complete_cycle_sla(
            cycle, {}, live_status=live)
        dirty = {"rc": 1, "output": ""}
        failure = stage_runner._forward_post_push_failure(
            cycle, "full", dirty, sla, live)
        self.assertEqual(
            "post_push_reconcile_failed", failure["failure_kind"])

    @staticmethod
    def _v4_live_status(cycle: str, completed_at: str) -> dict:
        return {
            "stage": "live",
            "cycle_id": cycle,
            "status": "succeeded",
            "returncode": 0,
            "collection_gate": {
                "schema_version": 1,
                "status": "met",
                "cycle_id": cycle,
                "required_sources": ["fast", "regime", "slow"],
                "completed_at": "2026-08-21 18:04:00",
                "elapsed_seconds": 240,
                "time_threshold_seconds": None,
            },
            "business_check": {
                "ok": True,
                "business_terminal": {
                    "schema_version": 1,
                    "cycle_id": cycle,
                    "status": "completed",
                    "completed_at_cst": completed_at,
                },
            },
            "report_reconcile_barrier": {
                "required": True,
                "profile": "live",
                "cycle_id": cycle,
                "status": "ok",
                "rc": 0,
                "contract_valid": True,
                "report_safe": True,
                "started_at": "2026-08-21 18:14:50",
                "finished_at": "2026-08-21 18:15:00",
            },
        }

    def test_v4_clock_stops_at_business_terminal_before_persistence(self):
        cycle = "2026-08-21T18:00"
        live = self._v4_live_status(cycle, "2026-08-21 18:14:29")
        result = stage_runner.build_complete_cycle_sla(
            cycle, {"rc": 1, "output": "ignored"}, live_status=live)
        self.assertEqual(
            "cycle_start_to_successful_analysis_judgment_trade_terminal",
            result["measurement"],
        )
        self.assertEqual(869, result["elapsed_seconds"])
        self.assertTrue(result["strict_cycle_pass"])
        self.assertTrue(result["business_terminal_gate"]["met"])
        self.assertFalse(
            result["post_business_processing"]["included_in_870_seconds"])

    def test_v4_exact_870_business_terminal_is_late(self):
        cycle = "2026-08-21T18:00"
        live = self._v4_live_status(cycle, "2026-08-21 18:14:30")
        result = stage_runner.build_complete_cycle_sla(
            cycle, {}, live_status=live)
        self.assertEqual("late", result["status"])
        self.assertEqual(870, result["elapsed_seconds"])
        self.assertFalse(result["strict_cycle_pass"])

    def test_v4_collection_gate_is_required(self):
        cycle = "2026-08-21T18:00"
        live = self._v4_live_status(cycle, "2026-08-21 18:10:00")
        live["collection_gate"]["status"] = "incomplete"
        result = stage_runner.build_complete_cycle_sla(
            cycle, {}, live_status=live)
        self.assertEqual("incomplete", result["status"])
        self.assertEqual("collection_gate_not_met", result["reason"])

    def test_v4_collection_gate_reads_required_sources(self):
        cycle = "2026-08-21T18:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.execute(
                    "CREATE TABLE collection_runs("
                    "cycle_id TEXT,source TEXT,status TEXT,ts TEXT)"
                )
                con.executemany(
                    "INSERT INTO collection_runs VALUES(?,?,?,?)",
                    [
                        (cycle, "fast", "ok", "2026-08-21 18:02:00"),
                        (cycle, "slow", "ok", "2026-08-21 18:04:00"),
                        (cycle, "regime", "ok", "2026-08-21 18:04:00"),
                    ],
                )
                con.commit()
            finally:
                con.close()
            result = stage_runner._collection_gate_contract(
                cycle, db_root=root)
        self.assertEqual("met", result["status"])
        self.assertEqual(240, result["elapsed_seconds"])
        self.assertIsNone(result["time_threshold_seconds"])

    def test_v4_collection_gate_retries_transient_read_error(self):
        cycle = "2026-08-21T18:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.execute(
                    "CREATE TABLE collection_runs("
                    "cycle_id TEXT,source TEXT,status TEXT,ts TEXT)"
                )
                con.executemany(
                    "INSERT INTO collection_runs VALUES(?,?,?,?)",
                    [
                        (cycle, "fast", "ok", "2026-08-21 18:02:00"),
                        (cycle, "slow", "ok", "2026-08-21 18:04:00"),
                        (cycle, "regime", "ok", "2026-08-21 18:04:00"),
                    ],
                )
                con.commit()
            finally:
                con.close()

            real_connect = stage_runner.ledger.connect
            attempts = 0

            def flaky_connect(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("disk I/O error")
                return real_connect(*args, **kwargs)

            with (
                mock.patch.object(
                    stage_runner.ledger, "connect", side_effect=flaky_connect
                ),
                mock.patch.object(stage_runner.time, "sleep") as sleep,
            ):
                result = stage_runner._collection_gate_contract(
                    cycle, db_root=root)

        self.assertEqual("met", result["status"])
        self.assertEqual(2, attempts)
        sleep.assert_called_once_with(0.05)


class CollectionFailurePushTerminalTests(unittest.TestCase):
    CYCLE = "2026-08-16T14:30"

    @classmethod
    def terminal(cls):
        finished = "2026-08-16 14:35:01"
        request_id = hashlib.sha256(
            f"collection-failure-report|{cls.CYCLE}|{finished}".encode("utf-8")
        ).hexdigest()[:32]
        return {
            "stage": "collection", "cycle_id": cls.CYCLE,
            "mode": "quarter", "status": "failed",
            "failure_kind": "collection_gate_failed",
            "child_returncode": 1, "returncode": 1,
            "started_at": "2026-08-16 14:30:00",
            "finished_at": finished,
            "profile_lease_released": True,
            "same_cycle_live_dispatched": False,
            "failed_steps": ["fast"],
            "missing_required_sources": ["fast"],
            "collection_latency_ms": 301447,
            "collection_receipt_sha256": "a" * 64,
            "production_database_writes": 0, "orders_placed": 0,
            "business_check": {"ok": True, "checks": [
                {"db": "analysis.db", "table": "analysis_runs", "found": False},
                {"db": "live_trades.db", "table": "trade_cycles", "found": False},
                {"db": "live_trades.db", "table": "trades", "found": False},
            ]},
            "report_reconcile_barrier": {
                "schema_version": 1, "required": True, "profile": "live",
                "cycle_id": cls.CYCLE, "contract_version": 1,
                "request_id": request_id,
                "status": "ok", "rc": 0, "applied": False,
                "blocking": False, "p0": False, "contract_valid": True,
                "report_safe": True,
                "started_at": finished,
                "finished_at": finished,
                "findings_count": 1, "healed_count": 0,
                "evidence_kind": "collection_terminal_and_execution_path_absence",
            },
        }

    @classmethod
    def monitor(cls):
        return {"rc": 0, "timed_out": False, "started": True,
                "deadline_exceeded": False,
                "output": json.dumps({
                    "ts": "2026-08-16 14:35:30", "cycle_id": cls.CYCLE,
                    "profile": "live", "ok": True, "issue": False,
                    "rc": 0, "markers": [],
                })}

    @classmethod
    def push_report(cls, terminal=None):
        return {
            "cycle": cls.CYCLE, "report_mode": "upstream_failure",
            "ok": True, "send_status": "sent",
            "steps": {"send": {"rc": 0}},
            "upstream_failure": terminal or cls.terminal(),
        }

    def test_canonical_collection_failure_is_incomplete_without_failing_push(self):
        terminal = self.terminal()
        monitor = self.monitor()
        sla = stage_runner.build_complete_cycle_sla(
            self.CYCLE, monitor, live_status={}, upstream_failure=terminal,
            live_status_absent=True)
        self.assertEqual("incomplete", sla["status"])
        self.assertEqual("upstream_collection_failed", sla["reason"])
        self.assertFalse(sla["complete"])
        self.assertIsNone(stage_runner._forward_post_push_failure(
            self.CYCLE, "failure_report", monitor, sla, {},
            upstream_failure=terminal, push_report=self.push_report(terminal),
            live_status_absent=True))

    def test_collection_exception_is_strict_and_monitor_fail_closed(self):
        terminal_mutations = (
            ("cycle_id", "2026-08-16T14:15"),
            ("returncode", 0), ("returncode", True),
            ("same_cycle_live_dispatched", True),
        )
        for key, value in terminal_mutations:
            terminal = self.terminal()
            terminal[key] = value
            sla = stage_runner.build_complete_cycle_sla(
                self.CYCLE, self.monitor(), live_status={},
                upstream_failure=terminal, live_status_absent=True)
            self.assertIsNotNone(stage_runner._forward_post_push_failure(
                self.CYCLE, "failure_report", self.monitor(), sla, {},
                upstream_failure=terminal,
                push_report=self.push_report(terminal),
                live_status_absent=True), (key, value))
        for mutation in (
            {"rc": 1},
            {"output": "not-json"},
            {"output": json.dumps({
                "ts": "2026-08-16 14:35:30", "cycle_id": self.CYCLE,
                "profile": "live", "ok": True, "issue": False,
                "rc": 0, "markers": ["dirty"],
            })},
        ):
            monitor = self.monitor()
            monitor.update(mutation)
            sla = stage_runner.build_complete_cycle_sla(
                self.CYCLE, monitor, live_status={},
                upstream_failure=self.terminal(), live_status_absent=True)
            self.assertIsNotNone(stage_runner._forward_post_push_failure(
                self.CYCLE, "failure_report", monitor, sla, {},
                upstream_failure=self.terminal(),
                push_report=self.push_report(), live_status_absent=True))

    def test_collection_terminal_field_hash_and_time_matrix(self):
        required_terminal = (
            "stage", "cycle_id", "mode", "status", "failure_kind",
            "child_returncode", "returncode", "started_at", "finished_at",
            "profile_lease_released", "same_cycle_live_dispatched",
            "failed_steps", "missing_required_sources",
            "collection_latency_ms", "collection_receipt_sha256",
            "production_database_writes", "orders_placed", "business_check",
            "report_reconcile_barrier",
        )
        for key in required_terminal:
            terminal = self.terminal()
            del terminal[key]
            self.assertFalse(stage_runner._strict_collection_failure_terminal(
                self.CYCLE, terminal), key)
        required_barrier = tuple(
            self.terminal()["report_reconcile_barrier"].keys())
        for key in required_barrier:
            terminal = self.terminal()
            del terminal["report_reconcile_barrier"][key]
            self.assertFalse(stage_runner._strict_collection_failure_terminal(
                self.CYCLE, terminal), f"barrier.{key}")
        for check_index in range(3):
            for key in ("db", "table", "found"):
                terminal = self.terminal()
                del terminal["business_check"]["checks"][check_index][key]
                self.assertFalse(
                    stage_runner._strict_collection_failure_terminal(
                        self.CYCLE, terminal),
                    f"business_check.checks[{check_index}].{key}")
        for key, value in (
            ("child_returncode", 2), ("returncode", 89),
            ("collection_latency_ms", 1800001),
            ("collection_receipt_sha256", "A" * 64),
            ("collection_receipt_sha256", "a" * 63),
            ("finished_at", "2026-08-16 14:35:02"),
        ):
            terminal = self.terminal()
            terminal[key] = value
            self.assertFalse(stage_runner._strict_collection_failure_terminal(
                self.CYCLE, terminal), (key, value))

    def test_collection_source_order_and_business_exactness(self):
        for key, value in (
            ("failed_steps", ["fast", "fast"]),
            ("failed_steps", ["slow", "fast"]),
            ("missing_required_sources", ["slow"]),
            ("missing_required_sources", ["fast", "fast"]),
        ):
            terminal = self.terminal()
            terminal[key] = value
            self.assertFalse(stage_runner._strict_collection_failure_terminal(
                self.CYCLE, terminal), (key, value))
        for mutation in ("found_int", "check_extra", "business_extra"):
            terminal = self.terminal()
            if mutation == "found_int":
                terminal["business_check"]["checks"][0]["found"] = 0
            elif mutation == "check_extra":
                terminal["business_check"]["checks"][0]["extra"] = False
            else:
                terminal["business_check"]["extra"] = False
            self.assertFalse(stage_runner._strict_collection_failure_terminal(
                self.CYCLE, terminal), mutation)

        hourly_cycle = "2026-08-16T15:00"
        hourly = self.terminal()
        hourly.update({
            "cycle_id": hourly_cycle,
            "mode": "hourly",
            "started_at": "2026-08-16 15:00:00",
            "finished_at": "2026-08-16 15:05:01",
            "failed_steps": ["fast", "regime"],
            "missing_required_sources": ["fast", "regime"],
        })
        barrier = hourly["report_reconcile_barrier"]
        barrier.update({
            "cycle_id": hourly_cycle,
            "started_at": hourly["finished_at"],
            "finished_at": hourly["finished_at"],
            "findings_count": 2,
            "request_id": hashlib.sha256(
                (f"collection-failure-report|{hourly_cycle}|"
                 f"{hourly['finished_at']}").encode("utf-8")
            ).hexdigest()[:32],
        })
        self.assertTrue(stage_runner._strict_collection_failure_terminal(
            hourly_cycle, hourly))
        hourly["missing_required_sources"] = ["regime", "fast"]
        self.assertFalse(stage_runner._strict_collection_failure_terminal(
            hourly_cycle, hourly))
        for key, value in (
            ("request_id", "b" * 32),
            ("started_at", "2026-08-16 14:35:00"),
            ("finished_at", "2026-08-16 14:35:02"),
        ):
            terminal = self.terminal()
            terminal["report_reconcile_barrier"][key] = value
            self.assertFalse(stage_runner._strict_collection_failure_terminal(
                self.CYCLE, terminal), (key, value))

    def test_collection_monitor_cycle_boundaries(self):
        for ts, accepted in (
            ("2026-08-16 14:29:59", False),
            ("2026-08-16 14:30:00", True),
            ("2026-08-16 14:43:59", True),
            ("2026-08-16 14:44:00", False),
        ):
            monitor = self.monitor()
            payload = json.loads(monitor["output"])
            payload["ts"] = ts
            monitor["output"] = json.dumps(payload)
            self.assertEqual(accepted,
                stage_runner._strict_clean_collection_monitor(
                    self.CYCLE, monitor), ts)
        for output in (None, {}, [], b"{}"):
            monitor = self.monitor()
            monitor["output"] = output
            self.assertFalse(stage_runner._strict_clean_collection_monitor(
                self.CYCLE, monitor), type(output).__name__)

    def test_upstream_failure_push_report_mutation_matrix(self):
        terminal = self.terminal()
        mutations = {
            "cycle": lambda report: report.update({"cycle": "wrong"}),
            "mode": lambda report: report.update({"report_mode": "full"}),
            "ok": lambda report: report.update({"ok": False}),
            "send_status": lambda report: report.update(
                {"send_status": "failed"}),
            "steps": lambda report: report.update({"steps": {}}),
            "send_rc_bool": lambda report: report["steps"]["send"].update(
                {"rc": True}),
            "send_rc_nonzero": lambda report: report["steps"]["send"].update(
                {"rc": 1}),
            "missing_terminal": lambda report: report.update(
                {"upstream_failure": None}),
            "receipt_mismatch": lambda report: report[
                "upstream_failure"].update(
                    {"collection_receipt_sha256": "b" * 64}),
        }
        for label, mutate in mutations.items():
            report = self.push_report(self.terminal())
            mutate(report)
            self.assertFalse(stage_runner._strict_upstream_failure_push_report(
                self.CYCLE, report, terminal), label)

    def test_main_canonical_collection_report_succeeds_without_second_alert(self):
        child = {"returncode": 0, "timed_out": False, "started": True,
                 "budget_seconds": 300.0, "push_report": self.push_report()}
        alert = mock.Mock(return_value={"rc": 0, "delivered": True})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
                mock.patch.object(stage_runner, "_run_stage_child",
                                  return_value=child), \
                mock.patch.object(stage_runner, "verify_business_output",
                                  return_value={"ok": True}), \
                mock.patch.object(stage_runner, "_run_post_push_monitor",
                                  return_value=self.monitor()), \
                mock.patch.object(stage_runner, "load_upstream_failure",
                                  return_value=self.terminal()), \
                mock.patch.object(stage_runner, "_send_failure_alert", alert), \
                mock.patch.object(sys, "argv", [
                    "stage_runner.py", "--stage", "push", "--cycle",
                    self.CYCLE, "--mode", "failure_report", "--", "push",
                ]):
            rc = stage_runner.main()
            status = json.loads((Path(tmp) /
                "push-2026-08-16T14-30.json").read_text(encoding="utf-8"))
        self.assertEqual(0, rc)
        self.assertEqual("succeeded", status["status"])
        self.assertEqual("upstream_collection_failed",
                         status["complete_cycle_sla"]["reason"])
        alert.assert_not_called()

    def test_main_rejects_damaged_live_status_and_full_mode_absence(self):
        child = {"returncode": 0, "timed_out": False, "started": True,
                 "budget_seconds": 300.0, "push_report": self.push_report()}
        cases = (
            ("{}", "failure_report"),
            ("not-json", "failure_report"),
            ("[]", "failure_report"),
            (json.dumps({"stage": "live"}), "failure_report"),
            (None, "full"),
        )
        for live_text, mode in cases:
            with self.subTest(live_text=live_text, mode=mode), \
                    tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
                    mock.patch.object(stage_runner, "_run_stage_child",
                                      return_value=child), \
                    mock.patch.object(stage_runner, "verify_business_output",
                                      return_value={"ok": True}), \
                    mock.patch.object(stage_runner, "_run_post_push_monitor",
                                      return_value=self.monitor()), \
                    mock.patch.object(stage_runner, "load_upstream_failure",
                                      return_value=self.terminal()), \
                    mock.patch.object(stage_runner, "_send_failure_alert",
                                      return_value={"rc": 0,
                                                    "delivered": True}), \
                    mock.patch.object(sys, "argv", [
                        "stage_runner.py", "--stage", "push", "--cycle",
                        self.CYCLE, "--mode", mode, "--", "push",
                    ]):
                if live_text is not None:
                    (Path(tmp) / "live-2026-08-16T14-30.json").write_text(
                        live_text, encoding="utf-8")
                rc = stage_runner.main()
                status = json.loads((Path(tmp) /
                    "push-2026-08-16T14-30.json").read_text(encoding="utf-8"))
            self.assertEqual(stage_runner._POST_PUSH_RECONCILE_FAILURE_RC, rc)
            self.assertEqual("failed", status["status"])

    def test_unreadable_live_status_is_not_absent(self):
        with mock.patch.object(Path, "read_text",
                               side_effect=PermissionError("denied")):
            status, absent = stage_runner._read_push_live_status(self.CYCLE)
        self.assertFalse(absent)
        self.assertTrue(status)
        self.assertIs(status["_status_evidence_valid"], False)
        self.assertEqual("PermissionError", status["_status_evidence_error"])




class LiveObserverPreflightRewriteTests(unittest.TestCase):
    """2026-08-24：failed_preflight 是可重试驻留态，超时才收口。"""

    CYCLE = "2026-08-15T03:00"

    def _seed(self, tmp, *, attempts=1):
        tmp_root = Path(tmp) / "tmp"
        tmp_root.mkdir()
        facts = tmp_root / "live_facts_2026-08-15T03-00.json"
        plan = tmp_root / "position_plan_2026-08-15T03-00.json"
        marker = tmp_root / "live_runner_state_2026-08-15T03-00.json"
        facts.write_text(json.dumps({"facts_hash": "f" * 64}),
                         encoding="utf-8")
        plan.write_text('{"actions":[]}', encoding="utf-8")
        marker.write_text(json.dumps({
            "schema_version": stage_runner._LIVE_RUNNER_STATE_SCHEMA_VERSION,
            "cycle_id": self.CYCLE,
            "state": "failed_preflight",
            "facts_hash": "f" * 64,
            "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
            "preflight_attempts": attempts,
            "error": "PlanError: receipt_context contract rejection",
        }), encoding="utf-8")
        return tmp_root, marker

    def test_failed_preflight_within_window_keeps_observing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root, marker = self._seed(tmp)
            observer = stage_runner._LiveChildObserver(
                self.CYCLE, tmp_root=tmp_root, db_root=Path(tmp) / "db",
                now_fn=lambda: marker.stat().st_mtime + 10.0)
            reason = observer()
            handoff_written = (
                tmp_root / "live_runner_handoff_2026-08-15T03-00.json"
            ).exists()
        self.assertIsNone(reason)
        self.assertEqual(1, observer.evidence["preflight_attempts"])
        self.assertIn("preflight_retry_age_seconds", observer.evidence)
        self.assertFalse(handoff_written)

    def test_failed_preflight_timeout_revokes_then_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root, marker = self._seed(tmp)
            observer = stage_runner._LiveChildObserver(
                self.CYCLE, tmp_root=tmp_root, db_root=Path(tmp) / "db",
                now_fn=lambda: (
                    marker.stat().st_mtime
                    + stage_runner._LIVE_PREFLIGHT_REWRITE_SECONDS + 1.0))
            reason = observer()
            handoff = json.loads(
                (tmp_root / "live_runner_handoff_2026-08-15T03-00.json")
                .read_text(encoding="utf-8"))
        self.assertEqual(
            "runner_terminal:failed_preflight_rewrite_timeout", reason)
        self.assertTrue(observer.evidence["handoff_revoked"])
        self.assertEqual("revoked", handoff["state"])
        self.assertEqual(
            "runner_terminal:failed_preflight_rewrite_timeout",
            handoff["reason"])

    def test_exhausted_failed_preflight_marker_stops_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root, marker = self._seed(tmp, attempts=2)
            observer = stage_runner._LiveChildObserver(
                self.CYCLE, tmp_root=tmp_root, db_root=Path(tmp) / "db",
                now_fn=lambda: marker.stat().st_mtime + 1.0)
            reason = observer()
        self.assertEqual("runner_terminal:failed_preflight", reason)

    def test_plan_rewrite_invalidates_stale_preflight_marker_binding(self):
        # 重写 plan 后旧 marker 因 plan_sha256 失配退出可重试驻留态，
        # 回到「plan 后 30 秒须起 runner」的既有闸（本例窗口内不判失败）。
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root, marker = self._seed(tmp)
            plan = tmp_root / "position_plan_2026-08-15T03-00.json"
            plan.write_text('{"actions":[],"v":2}', encoding="utf-8")
            observer = stage_runner._LiveChildObserver(
                self.CYCLE, tmp_root=tmp_root, db_root=Path(tmp) / "db",
                now_fn=lambda: plan.stat().st_mtime + 5.0)
            reason = observer()
        self.assertIsNone(reason)
        self.assertIn("plan_sha256", observer.evidence.get("marker_error", ""))


class FillsWindowCoverageTests(unittest.TestCase):
    """2026-09-11 SOXL：`swap fills` 无分页，首页之外的平仓不得被当成「窗口内没有成交」。

    CLI（1.4.4 与 1.4.6 实测一致）的 cmdSwapFills 只透传 instId/ordId/archive，
    after/before/begin/end/limit 一律丢弃且不报错——recent 页只覆盖近 3 天，
    archive 页被固定成 20 条。证不到覆盖就必须失败关闭（FUZZY），绝不放行猜测匹配。
    """

    SYMBOL = "SOXL-USDT-SWAP"
    KEY = (SYMBOL, "long")
    OPEN_TS = "2026-09-07 12:00:00"       # 窗口起点 = 该时刻 - 10min 缓冲
    CLOSE_TS = "2026-09-07 13:07:17"      # 真实平仓 ordId=3900878456908173312
    BEFORE_WINDOW_TS = "2026-09-07 11:00:00"
    RECENT_PAGE_TS = "2026-09-08 16:00:29"   # 实测 recent 首页最早一条
    ARCHIVE_PAGE_TS = "2026-09-11 13:43:16"  # 实测 archive 首页 20 条同一时刻

    @staticmethod
    def _ms(text):
        return int(reconcile_exchange_closes.parse_ts(text).timestamp() * 1000)

    def _fill(self, ord_id, ts_text, *, side="sell", sz="0.19",
              px="128.73", pnl="-0.5662", tag=""):
        return {"instId": self.SYMBOL, "ordId": ord_id,
                "tradeId": f"T-{ord_id}-{tag or ts_text}",
                "side": side, "posSide": "long", "fillSz": sz, "fillPx": px,
                "fillPnl": pnl, "fillTime": str(self._ms(ts_text))}

    def _open_legs(self, count, ts_text):
        """同标的开仓腿：占满页面却不提供任何平仓证据。"""
        return [self._fill(f"OPEN-{index}", ts_text, side="buy", pnl="0",
                           tag=str(index)) for index in range(count)]

    def _close_leg(self):
        return self._fill("CLOSE-SOXL", self.CLOSE_TS)

    def _ledger(self):
        return [{"id": 1, "cycle_id": "2026-09-07T12:00", "ts": self.OPEN_TS,
                 "symbol": self.SYMBOL, "action": "open", "side": "long",
                 "sz": 0.19, "fill_px": 130.0, "lev": 5.0, "pnl": 0.0,
                 "raw": "{}"}]

    def _api(self, recent, archive):
        def api(*args, **kwargs):
            if args[:2] == ("swap", "fills"):
                page = archive if "--archive" in args else recent
                return {"code": "0", "data": [dict(row) for row in page]}
            self.assertEqual(args[:2], ("swap", "get"))
            oid = args[args.index("--ordId") + 1]
            return {"code": "0", "data": [{
                "instId": self.SYMBOL, "ordId": oid, "side": "sell",
                "posSide": "long", "state": "filled", "accFillSz": "0.19",
                "avgPx": "128.73", "pnl": "-0.5662"}]}
        return api

    def _classify(self, api):
        with mock.patch.object(reconcile_exchange_closes, "okx_json",
                               side_effect=api):
            return reconcile_exchange_closes.classify(
                "live", {self.KEY: self._ledger()}, {self.KEY: 0.19}, {})

    def test_close_beyond_the_only_page_is_not_reported_as_an_empty_window(self):
        # 复刻 2026-09-11：recent 37 条（全在窗口起点之后）、archive 20 条撞上限，
        # 真平仓落在两页之外。旧实现据此报「窗口内无未销账的成交」。
        verdict = self._classify(self._api(
            self._open_legs(37, self.RECENT_PAGE_TS),
            self._open_legs(20, self.ARCHIVE_PAGE_TS)))

        self.assertEqual(verdict["exact"], [])
        self.assertEqual(len(verdict["fuzzy"]), 1)
        reason = verdict["fuzzy"][0][2]
        self.assertIn("close_fills_window_coverage_unproven", reason)
        self.assertNotIn("窗口内无未销账的成交", reason)
        # 截断是永久事实而非可重试预算，不得进 deferred 放宽自愈分批闸。
        self.assertEqual(verdict["deferred"], [])

    def test_full_archive_page_crossing_the_window_start_still_matches(self):
        # 撞上限但已越过窗口起点 ⇒ 覆盖已自证，正常判定不受影响。
        archive = [self._close_leg()] + self._open_legs(
            19, self.BEFORE_WINDOW_TS)
        verdict = self._classify(self._api([], archive))

        self.assertEqual(verdict["fuzzy"], [])
        self.assertEqual(len(verdict["exact"]), 1)
        self.assertEqual(
            [group["ordId"] for group in verdict["exact"][0][2]],
            ["CLOSE-SOXL"])

    def test_short_archive_page_proves_coverage_without_false_alarm(self):
        archive = [self._close_leg()] + self._open_legs(2, self.CLOSE_TS)
        verdict = self._classify(self._api([], archive))

        self.assertEqual(verdict["fuzzy"], [])
        self.assertEqual(len(verdict["exact"]), 1)
        self.assertEqual(verdict["exact"][0][1], 0.19)

    def test_exhausted_evidence_budgets_stay_fail_closed_and_typed(self):
        archive = [self._close_leg()] + self._open_legs(2, self.CLOSE_TS)
        for name, value, expected in (
            ("CLOSE_ORDER_LOOKUP_LIMIT", 0,
             "close_evidence_order_budget_exhausted"),
            ("CLOSE_EVIDENCE_BUDGET_SEC", 0,
             "close_evidence_time_budget_exhausted"),
        ):
            with self.subTest(budget=name), mock.patch.object(
                    reconcile_exchange_closes, name, value):
                verdict = self._classify(self._api([], archive))

            self.assertEqual(verdict["exact"], [])
            self.assertIn(expected, verdict["fuzzy"][0][2])
            self.assertEqual(len(verdict["deferred"]), 1)
            self.assertIn(verdict["deferred"][0][2],
                          reconcile_exchange_closes.CLOSE_DEFERRED_REASONS)
        self.assertIsNone(reconcile_exchange_closes._CLOSE_BUDGET.get())

    def test_open_leg_fetch_fails_closed_on_the_same_truncation(self):
        t0_ms = self._ms(self.OPEN_TS)
        truncated = self._api(self._open_legs(37, self.RECENT_PAGE_TS),
                              self._open_legs(20, self.ARCHIVE_PAGE_TS))
        with mock.patch.object(reconcile_exchange_closes, "okx_json",
                               side_effect=truncated):
            with self.assertRaisesRegex(RuntimeError, "证据不足"):
                reconcile_exchange_closes.fetch_open_fills(
                    "live", self.SYMBOL, "long", t0_ms)

        proven = self._api([], self._open_legs(3, self.RECENT_PAGE_TS))
        with mock.patch.object(reconcile_exchange_closes, "okx_json",
                               side_effect=proven):
            fills = reconcile_exchange_closes.fetch_open_fills(
                "live", self.SYMBOL, "long", t0_ms)
        self.assertEqual(len(fills), 3)

    def test_only_the_archive_source_can_prove_coverage_by_a_short_page(self):
        reaches = reconcile_exchange_closes._fills_page_reaches_window
        cap = reconcile_exchange_closes.ARCHIVE_FILLS_PAGE_CAP
        t0_ms = self._ms(self.OPEN_TS)
        newer = [self._fill("X", self.ARCHIVE_PAGE_TS)]
        crossing = [self._fill("Y", self.BEFORE_WINDOW_TS)]

        # recent 源受交易所侧 3 天硬窗约束：短页证明不了更早的成交。
        self.assertFalse(reaches(newer, t0_ms))
        self.assertTrue(reaches(newer, t0_ms, cap))
        self.assertFalse(reaches(self._open_legs(cap, self.ARCHIVE_PAGE_TS),
                                 t0_ms, cap))
        # 越过窗口起点则与页长、来源无关，恒为已覆盖。
        self.assertTrue(reaches(crossing, t0_ms))
        self.assertTrue(reaches(crossing, t0_ms, cap))
        # 脏行不得让判据崩掉，也不得被当成覆盖证据。
        self.assertFalse(reaches(["bad", {"fillTime": "not-a-number"}], t0_ms))


class EpochAnchorCoverageTests(unittest.TestCase):
    """2026-09-11 BCH：活动段首笔开仓整单在页内即自证覆盖，不再误判 GHOST-FUZZY。

    BCH 11:22 首开之前三天没有任何 BCH 成交，recent 页（28 条）永远越不过窗口
    起点；当天成交又把 archive 页（20 条）塞满。页面是该标的最新成交、新到旧连续，
    首笔开仓的全部成交都在页内，本段每一笔平仓就必然在页内。
    """

    SYMBOL = "BCH-USDT-SWAP"
    KEY = (SYMBOL, "short")
    WINDOW_START = "2026-09-11 11:12:26"   # 首笔开仓 11:22:26 - 10min 缓冲

    @staticmethod
    def _ms(text):
        return int(reconcile_exchange_closes.parse_ts(text).timestamp() * 1000)

    def _order(self, ord_id, ts_text, sizes, *, side, px, pnls=None):
        pnls = pnls or ["0"] * len(sizes)
        return [{"instId": self.SYMBOL, "ordId": ord_id,
                 "tradeId": f"T-{ord_id}-{index}", "side": side,
                 "posSide": "short", "fillSz": sz, "fillPx": px,
                 "fillPnl": pnl, "fillTime": str(self._ms(ts_text))}
                for index, (sz, pnl) in enumerate(zip(sizes, pnls))]

    def _pages(self):
        """复刻 09-11 21:30 实测：recent 28 条新到旧，archive 首页 20 条。"""
        close_e = self._order("CLOSE-E", "2026-09-11 20:59:15",
                              ["3.3"] * 7 + ["3.5"], side="buy", px="229.56",
                              pnls=["-1.0"] * 7 + ["-1.400515"])
        open_d = self._order("OPEN-D", "2026-09-11 20:38:15", ["17.7"],
                             side="sell", px="226.8")
        close_c = self._order("CLOSE-C", "2026-09-11 20:30:11", ["11.5"],
                              side="buy", px="219.7", pnls=["6.590515"])
        open_b = self._order("OPEN-B", "2026-09-11 17:38:14", ["1.15"] * 10,
                             side="sell", px="225.3")
        open_a = self._order("OPEN-A", "2026-09-11 11:22:26",
                             ["1.1"] * 7 + ["1.2"], side="sell", px="225.6")
        recent = close_e + open_d + close_c + open_b + open_a
        return recent, recent[:20]

    def _ledger(self, first_open_raw='{"ordId": "OPEN-A"}',
                old_close_ts="2026-08-16 21:55:12"):
        def row(row_id, ts_text, action, sz, px, pnl=0.0, raw="{}"):
            return {"id": row_id, "cycle_id": "2026-09-11T11:15", "ts": ts_text,
                    "symbol": self.SYMBOL, "action": action, "side": "short",
                    "sz": sz, "fill_px": px, "lev": 5.0, "pnl": pnl, "raw": raw}
        return [
            row(404, "2026-08-16 03:38:38", "open", 32.3, 203.5,
                raw='{"ordId": "OLD-OPEN"}'),
            row(415, old_close_ts, "close", 32.3, 204.4, -2.907,
                raw='{"ordId": "OLD-CLOSE"}'),
            row(1705, "2026-09-11 11:22:26", "open", 8.9, 225.6,
                raw=first_open_raw),
            row(1745, "2026-09-11 17:38:14", "open", 11.5, 225.3,
                raw='{"ordId": "OPEN-B"}'),
            row(1786, "2026-09-11 20:30:11", "close", 11.5, 219.7, 6.590515,
                raw='{"ord_ids": ["CLOSE-C"]}'),
            row(1788, "2026-09-11 20:38:15", "open", 17.7, 226.8,
                raw='{"ordId": "OPEN-D"}'),
        ]

    def _classify(self, ledger, recent, archive):
        def api(*args, **kwargs):
            if args[:2] == ("swap", "fills"):
                page = archive if "--archive" in args else recent
                return {"code": "0", "data": [dict(item) for item in page]}
            self.assertEqual(args[:2], ("swap", "get"))
            self.assertEqual(args[args.index("--ordId") + 1], "CLOSE-E")
            return {"code": "0", "data": [{
                "instId": self.SYMBOL, "ordId": "CLOSE-E", "side": "buy",
                "posSide": "short", "state": "filled", "accFillSz": "26.6",
                "avgPx": "229.56", "pnl": "-8.400515"}]}

        net = reconcile_exchange_closes.net_of(ledger)
        with mock.patch.object(reconcile_exchange_closes, "okx_json",
                               side_effect=api):
            return reconcile_exchange_closes.classify(
                "live", {self.KEY: ledger}, {self.KEY: net}, {})

    def _assert_unproven(self, verdict):
        self.assertEqual(verdict["exact"], [])
        self.assertEqual(len(verdict["fuzzy"]), 1)
        self.assertIn("close_fills_window_coverage_unproven",
                      verdict["fuzzy"][0][2])
        self.assertEqual(verdict["deferred"], [])

    def test_whole_first_open_on_the_page_proves_the_epoch_window(self):
        recent, archive = self._pages()
        self.assertEqual(len(recent), 28)

        verdict = self._classify(self._ledger(), recent, archive)

        self.assertEqual(verdict["fuzzy"], [])
        self.assertEqual(len(verdict["exact"]), 1)
        key, ghost_sz, matched, _ = verdict["exact"][0]
        self.assertEqual(key, self.KEY)
        self.assertAlmostEqual(ghost_sz, 26.6, places=6)
        self.assertEqual([group["ordId"] for group in matched], ["CLOSE-E"])
        self.assertAlmostEqual(matched[0]["pnl"], -8.400515, places=6)
        self.assertIsNone(reconcile_exchange_closes._CLOSE_EPOCH_ANCHOR.get())

    def test_first_open_cut_off_by_the_page_stays_fail_closed(self):
        recent, archive = self._pages()
        self._assert_unproven(
            self._classify(self._ledger(), recent[:-3], archive))

    def test_first_open_without_one_order_identity_stays_fail_closed(self):
        recent, archive = self._pages()
        for raw in ("{}", '{"ord_ids": ["OPEN-A", "OPEN-X"]}', "not-json"):
            with self.subTest(raw=raw):
                self._assert_unproven(
                    self._classify(self._ledger(raw), recent, archive))

    def test_unfenced_history_anchors_on_the_oldest_open_and_fails_closed(self):
        # 旧段平仓距新开仓 < 核销+缓冲窗：活动段切不出来，锚点退到 08-16 首开（不在页内）。
        recent, archive = self._pages()
        self._assert_unproven(self._classify(
            self._ledger(old_close_ts="2026-09-11 11:00:00"), recent, archive))

    def test_page_holds_the_anchor_only_with_every_fill_of_the_order(self):
        reaches = reconcile_exchange_closes._fills_page_reaches_window
        recent, _ = self._pages()
        t0_ms = self._ms(self.WINDOW_START)
        whole = ("OPEN-A", 8.9)

        self.assertFalse(reaches(recent, t0_ms))
        self.assertTrue(reaches(recent, t0_ms, anchor=whole))
        self.assertFalse(reaches(recent[:-1], t0_ms, anchor=whole))
        self.assertFalse(reaches(recent, t0_ms, anchor=("OPEN-A", 10.0)))
        self.assertFalse(reaches(recent, t0_ms, anchor=("OPEN-Z", 8.9)))
        dirty = recent[:-1] + [dict(recent[-1], fillSz="bad")]
        self.assertFalse(reaches(dirty, t0_ms, anchor=whole))


class OpenReceiptAnchorCoverageTests(unittest.TestCase):
    """开仓腿同样受首页截断：intent 订单整单（张数 == 已核回执）在页内即自证覆盖。

    新标的前三天没有成交，recent 页越不过 t0 = reserved_at - 10min；开仓单一拆
    20 笔以上又独占 archive 页（20 条）。旧判据下严格 T1 补开仓永远落 T3——
    2026-09-09 XPL 空 185 张一单 31 笔成交即此形态（普查：当时若漏记则永久 T3）。
    """

    SYMBOL = "XPL-USDT-SWAP"
    ORD = "3906131984870297600"
    RESERVED = "2026-09-09 08:36:35"
    FILLED = "2026-09-09 08:36:44"
    SIZES = ["6.0"] * 30 + ["5.0"]         # 31 笔合计 185 张
    TOTAL = 185.0

    @staticmethod
    def _ms(text):
        return int(reconcile_exchange_closes.parse_ts(text).timestamp() * 1000)

    def _order(self, ord_id, ts_text, sizes, *, side="sell", pnl="0"):
        return [{"instId": self.SYMBOL, "ordId": ord_id,
                 "tradeId": f"T-{ord_id}-{index}", "side": side,
                 "posSide": "short", "fillSz": sz, "fillPx": "0.0989",
                 "fillPnl": pnl, "fillTime": str(self._ms(ts_text))}
                for index, sz in enumerate(sizes)]

    def _pages(self):
        """recent：14:02 平仓 7 笔 + 开仓 31 笔（新到旧）；archive 首页为最新 20 条。"""
        recent = (self._order("CLOSE-XPL", "2026-09-09 14:02:57",
                              ["26.0"] * 6 + ["29.0"], side="buy", pnl="0.62")
                  + self._order(self.ORD, self.FILLED, self.SIZES))
        return recent, recent[:reconcile_exchange_closes.ARCHIVE_FILLS_PAGE_CAP]

    def _fetch(self, recent, archive, anchor, recent_error=None):
        def api(*args, **kwargs):
            self.assertEqual(args[:4], ("swap", "fills", "--instId", self.SYMBOL))
            if "--archive" in args:
                return {"code": "0", "data": [dict(row) for row in archive]}
            if recent_error is not None:
                raise recent_error
            return {"code": "0", "data": [dict(row) for row in recent]}

        t0_ms = self._ms(self.RESERVED) - (
            reconcile_exchange_closes.OPEN_TS_BUFFER_MIN * 60 * 1000)
        with mock.patch.object(reconcile_exchange_closes, "okx_json",
                               side_effect=api):
            return reconcile_exchange_closes.fetch_open_fills(
                "live", self.SYMBOL, "short", t0_ms, anchor=anchor)

    def test_whole_intent_order_on_the_page_proves_the_open_window(self):
        recent, archive = self._pages()
        self.assertEqual((len(recent), len(archive)), (38, 20))
        with self.assertRaisesRegex(RuntimeError, "证据不足"):
            self._fetch(recent, archive, None)

        fills = self._fetch(recent, archive, (self.ORD, self.TOTAL))

        self.assertEqual(len(fills), 31)
        self.assertEqual({row["ordId"] for row in fills}, {self.ORD})
        self.assertAlmostEqual(sum(float(row["fillSz"]) for row in fills),
                               self.TOTAL, places=6)

    def test_intent_order_not_whole_on_the_page_stays_fail_closed(self):
        recent, archive = self._pages()
        whole = (self.ORD, self.TOTAL)
        for label, page, anchor in (
                ("oldest fills cut off", recent[:-3], whole),
                ("receipt size disagrees", recent, (self.ORD, self.TOTAL + 1)),
                ("order not on the page", recent, ("OTHER-ORDER", self.TOTAL)),
                ("unreadable fill size",
                 recent[:-1] + [dict(recent[-1], fillSz="bad")], whole)):
            with self.subTest(label), \
                    self.assertRaisesRegex(RuntimeError, "证据不足"):
                self._fetch(page, archive, anchor)

    def test_archive_page_alone_can_carry_the_anchor(self):
        # recent 源失败时，撞满 20 条上限的 archive 首页整单含 intent 订单也算覆盖。
        archive = (self._order("CLOSE-XPL", "2026-09-09 14:02:57", ["9.0"] * 8,
                               side="buy", pnl="0.62")
                   + self._order(self.ORD, self.FILLED, ["6.0"] * 12))
        self.assertEqual(len(archive),
                         reconcile_exchange_closes.ARCHIVE_FILLS_PAGE_CAP)
        down = RuntimeError("recent source timeout")
        with self.assertRaisesRegex(RuntimeError, "证据不足"):
            self._fetch([], archive, None, recent_error=down)

        fills = self._fetch([], archive, (self.ORD, 72.0), recent_error=down)

        self.assertEqual(len(fills), 12)

    def test_receipt_anchor_needs_a_verified_receipt_of_that_exact_order(self):
        anchor = reconcile_exchange_closes.receipt_open_anchor
        trade = {"symbol": self.SYMBOL, "action": "open", "side": "short",
                 "ordId": self.ORD, "sz": 184.99999999999997,
                 "fill_sz": 184.99999999999997}
        intent = {"ord_id": self.ORD, "state": "completed",
                  "completed_receipt_verified": True, "receipt_trade": trade}

        ord_id, sz = anchor(intent)
        self.assertEqual(ord_id, self.ORD)
        self.assertAlmostEqual(sz, self.TOTAL, places=6)
        legacy = dict(intent, receipt_trade=dict(trade, ordId=None,
                                                 ord_id=self.ORD))
        self.assertEqual(anchor(legacy), (self.ORD, trade["sz"]))
        for label, bad in (
                ("no intent", None),
                ("in-flight intent without receipt",
                 {"ord_id": self.ORD, "state": "submitted"}),
                ("receipt not verified",
                 dict(intent, completed_receipt_verified=False)),
                ("receipt of another order",
                 dict(intent, receipt_trade=dict(trade, ordId="OTHER-ORDER"))),
                ("size missing", dict(intent, receipt_trade=dict(trade, sz=None))),
                ("size zero",
                 dict(intent, receipt_trade=dict(trade, sz=0, fill_sz=0))),
                ("size not finite",
                 dict(intent, receipt_trade=dict(trade, sz="nan", fill_sz="nan"))),
                ("filled size disagrees",
                 dict(intent, receipt_trade=dict(trade, fill_sz=150.0)))):
            with self.subTest(label):
                self.assertIsNone(anchor(bad))


if __name__ == "__main__":
    unittest.main()
