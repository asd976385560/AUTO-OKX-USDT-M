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


def _create_analysis_db(path: Path, cycle: str, status: str = "ok") -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE analysis_runs("
            "cycle_id TEXT PRIMARY KEY,status TEXT,ts TEXT,mode TEXT)")
        con.execute(
            "INSERT INTO analysis_runs VALUES(?,?,?,?)",
            (cycle, status, "2026-07-27 02:50:00", "full"),
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
成交轮必须在同一个临时 Python 进程内调用 commit_receipt(receipt, "live")；HOLD 才可分步使用 writer。
## STOP
"""
        self.assertEqual(
            check_trader_docs_sync.live_money_path_problems(text), [])


class StageBusinessOutputTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
