# -*- coding: utf-8 -*-
"""Isolated regressions for the 2026-07-27 reconciliation incident.

No exchange calls, production database writes, Agent launches, or QQ pushes.
"""
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
for path in (ROOT / "collectors", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collection_monitor  # noqa: E402
import check_trader_docs_sync  # noqa: E402
import daily_report_writer  # noqa: E402
import live_reconcile_monitor  # noqa: E402
import reconcile_exchange_closes  # noqa: E402
import reconcile_daily  # noqa: E402
import stage_runner  # noqa: E402
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
并在同一个临时 Python 进程内调用 commit_receipt(receipt, "live")。
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
            now = position_exit.stat().st_mtime + 181
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
                "schema_version": 1,
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
                "schema_version": 1,
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
                "schema_version": 1,
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
                "schema_version": 1,
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

    def test_live_report_reconcile_barrier_reuses_read_only_autoheal(self):
        cycle = "2026-08-14T19:00"
        producer = {
            "contract_version": 1,
            "request_id": "c" * 32,
            "profile": "live",
            "cycle": cycle,
            "db_root": str(stage_runner.DB_ROOT.resolve()),
            "status": "ok",
            "applied": False,
            "p0": False,
            "blocking": False,
            "findings": [],
            "healed": [],
            "needs_human": [],
            "rc": 0,
        }
        fake_trigger = mock.Mock()
        fake_trigger._autoheal_ledger.return_value = producer
        with mock.patch.dict(sys.modules, {"trigger_agent": fake_trigger}):
            result = stage_runner._run_live_report_reconcile_barrier(cycle)
        fake_trigger._autoheal_ledger.assert_called_once_with(
            "live", cycle, db_root=stage_runner.DB_ROOT.resolve())
        self.assertTrue(result["contract_valid"])
        self.assertTrue(result["report_safe"])
        self.assertFalse(result["apply_authorized"])
        self.assertEqual(result["healed_count"], 0)

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
            "live", cycle, db_root=stage_runner.DB_ROOT.resolve())
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


if __name__ == "__main__":
    unittest.main()
