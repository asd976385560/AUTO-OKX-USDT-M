# -*- coding: utf-8 -*-
"""Report business-fidelity gates; all databases are isolated fixtures."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_push_payload  # noqa: E402
import push_pipeline  # noqa: E402
import render_push_report  # noqa: E402
import validate_push_format  # noqa: E402


CYCLE = "2026-08-14T07:00"
TRADE_SCHEMA = """
CREATE TABLE trade_cycles(
  cycle_id TEXT PRIMARY KEY,ts TEXT,mode TEXT,decision TEXT,
  n_orders INTEGER,equity REAL,note TEXT,raw TEXT
);
CREATE TABLE trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,cycle_id TEXT,ts TEXT,
  symbol TEXT,action TEXT,side TEXT,sz REAL,fill_px REAL,pnl REAL,raw TEXT
);
"""
LEDGER_SCHEMA = """
CREATE TABLE stage_profile_leases(
  profile TEXT PRIMARY KEY,stage TEXT,cycle_id TEXT,
  acquired_at TEXT,expires_at TEXT
);
CREATE TABLE execution_intents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile TEXT,cycle_id TEXT,state TEXT,ord_id TEXT,
  submitted_at TEXT,completed_at TEXT
);
"""


class PushBusinessFidelityTests(unittest.TestCase):
    def _root(self, tmp: str, *, active_lease: bool = False) -> Path:
        root = Path(tmp)
        con = sqlite3.connect(root / "live_trades.db")
        try:
            con.executescript(TRADE_SCHEMA)
            con.execute(
                "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                (CYCLE, "2026-08-14 07:02:00", "live", "traded", 1,
                 1000.0, "", "{}"),
            )
            con.execute(
                "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,fill_px,pnl) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (CYCLE, "2026-08-14 07:01:00", "BTC-USDT-SWAP",
                 "open", "long", 1.0, 100.0, 0.0),
            )
            con.commit()
        finally:
            con.close()

        con = sqlite3.connect(root / "ledger.db")
        try:
            con.executescript(LEDGER_SCHEMA)
            if active_lease:
                con.execute(
                    "INSERT INTO stage_profile_leases VALUES(?,?,?,?,?)",
                    ("live", "live", CYCLE, "2026-08-14 07:00:01",
                     "2026-08-14 07:30:01"),
                )
            con.commit()
        finally:
            con.close()
        return root

    @staticmethod
    def _status(root: Path) -> Path:
        status_dir = root / "status"
        status_dir.mkdir()
        (status_dir / "live-2026-08-14T07-00.json").write_text(
            json.dumps({
                "stage": "live",
                "cycle_id": CYCLE,
                "mode": "unified",
                "status": "succeeded",
                "started_at": "2026-08-14 07:00:01",
                "finished_at": "2026-08-14 07:04:00",
                "returncode": 0,
                "profile_lease_released": True,
            }),
            encoding="utf-8",
        )
        return status_dir

    @staticmethod
    def _payload(root: Path) -> dict:
        con = sqlite3.connect(root / "live_trades.db")
        con.row_factory = sqlite3.Row
        try:
            tc = dict(con.execute(
                "SELECT decision,n_orders FROM trade_cycles WHERE cycle_id=?",
                (CYCLE,),
            ).fetchone())
            trades = [dict(row) for row in con.execute(
                "SELECT id,ts,symbol,action,side,sz,fill_px,pnl FROM trades "
                "WHERE cycle_id=? ORDER BY id",
                (CYCLE,),
            )]
        finally:
            con.close()
        return {
            "business_report_attestation": (
                build_push_payload._business_report_attestation(
                    CYCLE, tc, trades)),
            "inter_report_exchange_attestation": (
                build_push_payload._inter_report_exchange_attestation(
                    str(root), CYCLE)),
        }

    def test_released_unchanged_terminal_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            status_dir = self._status(root)
            with mock.patch.object(
                    push_pipeline, "STAGE_STATUS_DIR", status_dir):
                result = push_pipeline._verify_business_attestation(
                    self._payload(root), str(root), CYCLE,
                    upstream_failure_report=False,
                )
        self.assertTrue(result["ok"])
        self.assertEqual(result["trade_count"], 1)
        self.assertFalse(
            result["live_stage_terminal"]["same_cycle_active_lease"])

    def test_late_fill_after_build_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            status_dir = self._status(root)
            payload = self._payload(root)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,fill_px,pnl) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (CYCLE, "2026-08-14 07:04:01", "ETH-USDT-SWAP",
                     "open", "short", 2.0, 50.0, 0.0),
                )
                con.execute(
                    "UPDATE trade_cycles SET n_orders=2 WHERE cycle_id=?",
                    (CYCLE,),
                )
                con.commit()
            finally:
                con.close()
            with (
                mock.patch.object(
                    push_pipeline, "STAGE_STATUS_DIR", status_dir),
                self.assertRaisesRegex(ValueError, "changed after build"),
            ):
                push_pipeline._verify_business_attestation(
                    payload, str(root), CYCLE,
                    upstream_failure_report=False,
                )

    def test_late_inter_report_exchange_fill_after_build_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            status_dir = self._status(root)
            payload = self._payload(root)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                    "fill_px,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("2026-08-14T06:45", "2026-08-14 06:59:59",
                     "ETH-USDT-SWAP", "close", "short", 2.0, 50.0,
                     3.25, json.dumps({
                         "reconcile_source": "exchange_fills_reconcile",
                         "ord_ids": ["ord-late"],
                     })),
                )
                con.commit()
            finally:
                con.close()
            with (
                mock.patch.object(
                    push_pipeline, "STAGE_STATUS_DIR", status_dir),
                mock.patch.object(
                    push_pipeline,
                    "INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM",
                    CYCLE,
                ),
                self.assertRaisesRegex(
                    ValueError, "inter-report exchange fill set changed"),
            ):
                push_pipeline._verify_business_attestation(
                    payload, str(root), CYCLE,
                    upstream_failure_report=False,
                )

    def test_late_direct_exchange_fill_after_build_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            status_dir = self._status(root)
            payload = self._payload(root)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                    "fill_px,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("2026-08-14T06:45", "2026-08-14 06:59:59",
                     "ETH-USDT-SWAP", "open", "long", 2.0, 50.0,
                     0.0, json.dumps({
                         "fill_source": "fills",
                         "ts_source": "fills.fillTime",
                         "ordId": "ord-direct-late",
                     })),
                )
                con.commit()
            finally:
                con.close()
            with (
                mock.patch.object(
                    push_pipeline, "STAGE_STATUS_DIR", status_dir),
                mock.patch.object(
                    push_pipeline,
                    "INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM",
                    CYCLE,
                ),
                self.assertRaisesRegex(
                    ValueError, "inter-report exchange fill set changed"),
            ):
                push_pipeline._verify_business_attestation(
                    payload, str(root), CYCLE,
                    upstream_failure_report=False,
                )

    def test_active_same_cycle_lease_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, active_lease=True)
            status_dir = self._status(root)
            with (
                mock.patch.object(
                    push_pipeline, "STAGE_STATUS_DIR", status_dir),
                self.assertRaisesRegex(ValueError, "lease still exists"),
            ):
                push_pipeline._verify_business_attestation(
                    self._payload(root), str(root), CYCLE,
                    upstream_failure_report=False,
                )

    def test_terminal_wait_bridges_release_status_write_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            status_dir = root / "status"
            status_dir.mkdir()
            path = status_dir / "live-2026-08-14T07-00.json"
            path.write_text(json.dumps({
                "stage": "live", "cycle_id": CYCLE,
                "mode": "unified", "status": "succeeded",
                "started_at": "2026-08-14 07:00:01",
                "finished_at": "2026-08-14 07:04:00",
                "returncode": 0,
                "profile_lease_released": False,
            }), encoding="utf-8")

            def finish_status():
                time.sleep(0.05)
                path.write_text(json.dumps({
                    "stage": "live", "cycle_id": CYCLE,
                    "mode": "unified", "status": "succeeded",
                    "started_at": "2026-08-14 07:00:01",
                    "finished_at": "2026-08-14 07:04:00",
                    "returncode": 0,
                    "profile_lease_released": True,
                }), encoding="utf-8")

            worker = threading.Thread(target=finish_status)
            worker.start()
            try:
                with mock.patch.object(
                        push_pipeline, "STAGE_STATUS_DIR", status_dir):
                    result = push_pipeline._verify_business_attestation(
                        self._payload(root), str(root), CYCLE,
                        upstream_failure_report=False,
                        terminal_wait_seconds=0.5,
                    )
            finally:
                worker.join()
        self.assertTrue(result["ok"])

    def test_failure_report_zero_business_truth_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.executescript(TRADE_SCHEMA)
                con.commit()
            finally:
                con.close()
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.executescript(LEDGER_SCHEMA)
                con.commit()
            finally:
                con.close()
            status_dir = root / "status"
            status_dir.mkdir()
            (status_dir / "live-2026-08-14T07-00.json").write_text(
                json.dumps({
                    "stage": "live", "cycle_id": CYCLE,
                    "mode": "unified", "status": "failed",
                    "started_at": "2026-08-14 07:00:01",
                    "finished_at": "2026-08-14 07:04:00",
                    "child_returncode": 1,
                    "returncode": 86,
                    "failure_kind": "business_output_missing",
                    "profile_lease_released": True,
                }),
                encoding="utf-8",
            )
            context = {
                "failure_kind": "business_output_missing",
            }
            payload = {
                "business_report_attestation": (
                    build_push_payload._failure_report_attestation(
                        CYCLE,
                        context,
                        {
                            "intent_rows": 0,
                            "failed_clean_rows": 0,
                            "unsafe_rows": 0,
                        },
                    )
                ),
            }
            with mock.patch.object(
                    push_pipeline, "STAGE_STATUS_DIR", status_dir):
                result = push_pipeline._verify_business_attestation(
                    payload, str(root), CYCLE,
                    upstream_failure_report=True,
                )
        self.assertTrue(result["ok"])
        self.assertEqual(result["terminal"], "absent")
        self.assertEqual(result["unsafe_rows"], 0)
        self.assertRegex(result["sha256"], r"^[0-9a-f]{64}$")

    def test_collection_failure_report_uses_execution_path_absence_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.executescript(TRADE_SCHEMA)
                con.commit()
            finally:
                con.close()
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.executescript(LEDGER_SCHEMA)
                con.commit()
            finally:
                con.close()
            barrier = {
                "schema_version": 1,
                "required": True,
                "profile": "live",
                "cycle_id": CYCLE,
                "status": "ok",
                "rc": 0,
                "blocking": False,
                "report_safe": True,
            }
            context = {
                "stage": "collection",
                "cycle_id": CYCLE,
                "status": "failed",
                "failure_kind": "collection_gate_failed",
                "finished_at": "2026-08-14 07:03:00",
                "report_reconcile_barrier": barrier,
            }
            payload = {
                "upstream_failure": context,
                "business_report_attestation": (
                    build_push_payload._failure_report_attestation(
                        CYCLE,
                        context,
                        {
                            "intent_rows": 0,
                            "failed_clean_rows": 0,
                            "unsafe_rows": 0,
                        },
                    )
                ),
            }
            with mock.patch.object(
                push_pipeline,
                "require_upstream_failure",
                return_value=context,
            ):
                result = push_pipeline._verify_business_attestation(
                    payload,
                    str(root),
                    CYCLE,
                    upstream_failure_report=True,
                )
                drifted_payload = dict(payload)
                drifted_context = dict(context)
                drifted_context["collection_receipt_sha256"] = "0" * 64
                drifted_payload["upstream_failure"] = drifted_context
                with self.assertRaisesRegex(
                    ValueError,
                    "collection failure receipt or gate proof drifted",
                ):
                    push_pipeline._verify_business_attestation(
                        drifted_payload,
                        str(root),
                        CYCLE,
                        upstream_failure_report=True,
                    )

                late_context = dict(context)
                late_context["missing_required_sources"] = ["fast", "slow"]
                with mock.patch.object(
                    push_pipeline,
                    "require_upstream_failure",
                    side_effect=[context, late_context],
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "drifted during terminal verification",
                    ):
                        push_pipeline._verify_business_attestation(
                            payload,
                            str(root),
                            CYCLE,
                            upstream_failure_report=True,
                        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "upstream_failure")
        self.assertEqual(result["live_stage_terminal"]["stage"], "collection")
        self.assertTrue(
            result["live_stage_terminal"]["report_reconcile_barrier"]
            ["report_safe"])

    def test_failure_report_late_completed_adjust_intent_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.executescript(TRADE_SCHEMA)
                con.commit()
            finally:
                con.close()
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.executescript(LEDGER_SCHEMA)
                con.commit()
            finally:
                con.close()
            status_dir = root / "status"
            status_dir.mkdir()
            (status_dir / "live-2026-08-14T07-00.json").write_text(
                json.dumps({
                    "stage": "live", "cycle_id": CYCLE,
                    "mode": "unified", "status": "failed",
                    "started_at": "2026-08-14 07:00:01",
                    "finished_at": "2026-08-14 07:04:00",
                    "child_returncode": 1, "returncode": 86,
                    "failure_kind": "business_output_missing",
                    "profile_lease_released": True,
                }),
                encoding="utf-8",
            )
            payload = {"business_report_attestation": (
                build_push_payload._failure_report_attestation(
                    CYCLE,
                    {"failure_kind": "business_output_missing"},
                    {
                        "intent_rows": 0,
                        "failed_clean_rows": 0,
                        "unsafe_rows": 0,
                    },
                )
            )}
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.execute(
                    "INSERT INTO execution_intents"
                    "(profile,cycle_id,state,ord_id,submitted_at,completed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    ("live", CYCLE, "completed", "algo-1",
                     "2026-08-14 07:03:00", "2026-08-14 07:03:02"),
                )
                con.commit()
            finally:
                con.close()
            with (
                mock.patch.object(
                    push_pipeline, "STAGE_STATUS_DIR", status_dir),
                self.assertRaisesRegex(
                    ValueError, "non-clean execution intents"),
            ):
                push_pipeline._verify_business_attestation(
                    payload, str(root), CYCLE,
                    upstream_failure_report=True,
                )

    def test_failure_report_late_terminal_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            status_dir = self._status(root)
            (status_dir / "live-2026-08-14T07-00.json").write_text(
                json.dumps({
                    "stage": "live", "cycle_id": CYCLE,
                    "mode": "unified", "status": "failed",
                    "started_at": "2026-08-14 07:00:01",
                    "finished_at": "2026-08-14 07:04:00",
                    "child_returncode": 1, "returncode": 86,
                    "failure_kind": "business_output_missing",
                    "profile_lease_released": True,
                }),
                encoding="utf-8",
            )
            payload = {"business_report_attestation": {
                "terminal": "absent", "trade_count": 0,
            }}
            with (
                mock.patch.object(
                    push_pipeline, "STAGE_STATUS_DIR", status_dir),
                self.assertRaisesRegex(ValueError, "late business terminal"),
            ):
                push_pipeline._verify_business_attestation(
                    payload, str(root), CYCLE,
                    upstream_failure_report=True,
                )

    def test_future_report_requires_visible_count_and_fingerprint(self):
        payload = {
            "cycle_id": CYCLE,
            "cycle_count": 1,
            "cycle_duration_s": 10,
            "hhmm": "07:00",
            "action_taken": "HOLD",
            "symbol": "BTC",
            "assets": {"live": {
                "equity": 1000, "availBal": 900, "pnl": 0,
                "positions": 0,
            }},
            "positions": [],
            "risk": {
                "current_portfolio_imr_ratio": 0.1,
                "max_portfolio_imr_ratio": 0.666,
                "portfolio_imr_ratio_unit": "fraction",
                "lev": 10, "side_pct": 0, "position_count": 0,
                "status": "PASS",
            },
            "market": {
                "btc": 100, "btc_chg24h": 0,
                "eth": 50, "eth_chg24h": 0,
                "regime": "range", "dxy": 100,
            },
            "decision": {
                "summary": "HOLD with complete business truth",
                "reason": "no executable candidate",
                "decision_protocol": "decision_card_v1",
                "decision_card": {},
            },
            "execution": {"result": "HOLD", "db_rows_live": 0},
            "business_report_attestation": {
                "trade_count": 0,
                "sha256": "a" * 64,
            },
            "timeline": {"next_hh01_min": 60, "next_review_time": "08:05"},
            "exceptions": [],
        }
        patches = (
            mock.patch.object(
                render_push_report, "authoritative_cycle_count",
                return_value=None),
            mock.patch.object(
                render_push_report, "authoritative_cycle_duration",
                return_value=None),
            mock.patch.object(
                render_push_report, "authoritative_equity", return_value=None),
            mock.patch.object(
                render_push_report, "authoritative_cum_pnl", return_value=None),
            mock.patch.object(
                render_push_report, "authoritative_position_count",
                return_value=None),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            content = render_push_report.render(payload)["content"]
            payload["cycle_id"] = "2026-08-15T08:00"
            payload["hhmm"] = "08:00"
            payload["inter_report_exchange_attestation"] = {
                "schema_version": 1,
                "profile": "live",
                "cycle_id": "2026-08-15T08:00",
                "window_start_exclusive_cst": "2026-08-15 07:45:00",
                "window_end_inclusive_cst": "2026-08-15 08:00:00",
                "fill_count": 1,
                "fills": [{
                    "symbol": "BCH-USDT-SWAP",
                    "action": "close",
                    "side": "short",
                    "sz": 36.0,
                    "fill_px": 204.1,
                    "pnl": 6.84,
                    "ord_ids": ["3833488461226856449"],
                }],
                "sha256": "c" * 64,
            }
            interval_content = render_push_report.render(payload)["content"]
        result = validate_push_format.validate(content, cycle_id=CYCLE)
        self.assertTrue(result["ok"], result)
        self.assertIn("账实成交=0笔", content)
        self.assertIn("业务指纹=" + "a" * 64, content)

        missing = content.replace(" | 业务指纹=" + "a" * 64, "")
        rejected = validate_push_format.validate(missing, cycle_id=CYCLE)
        self.assertFalse(rejected["ok"])
        self.assertIn("业务指纹", rejected["missing_fields"])

        interval_result = validate_push_format.validate(
            interval_content, cycle_id="2026-08-15T08:00")
        self.assertTrue(interval_result["ok"], interval_result)
        self.assertIn("报告间交易所成交=1笔", interval_content)
        self.assertIn("CLOSE SHORT BCH-USDT-SWAP 36.0@204.1", interval_content)
        self.assertIn("ordId=3833488461226856449", interval_content)
        self.assertIn("区间指纹=" + "c" * 64, interval_content)

        missing_interval = interval_content.replace(
            " | 区间指纹=" + "c" * 64, "")
        interval_rejected = validate_push_format.validate(
            missing_interval, cycle_id="2026-08-15T08:00")
        self.assertFalse(interval_rejected["ok"])
        self.assertIn("区间指纹", interval_rejected["missing_fields"])

    def test_long_report_preserves_full_business_fingerprint(self):
        payload = {
            "cycle_id": CYCLE, "cycle_count": 1,
            "cycle_duration_s": 10, "hhmm": "07:00",
            "action_taken": "HOLD", "symbol": "BTC",
            "assets": {"live": {
                "equity": 1000, "availBal": 900, "pnl": 0,
                "positions": 0,
            }},
            "positions": [],
            "risk": {
                "current_portfolio_imr_ratio": 0.1,
                "max_portfolio_imr_ratio": 0.666,
                "portfolio_imr_ratio_unit": "fraction",
                "lev": 10, "side_pct": 0, "position_count": 0,
                "status": "PASS",
            },
            "market": {
                "btc": 100, "btc_chg24h": 0,
                "eth": 50, "eth_chg24h": 0,
                "regime": "range", "dxy": 100,
            },
            "decision": {
                "summary": "x" * 2000,
                "reason": "y" * 8000,
                "decision_protocol": "decision_card_v1",
                "decision_card": {},
            },
            "execution": {"result": "z" * 1000, "db_rows_live": 0},
            "business_report_attestation": {
                "trade_count": 0, "sha256": "b" * 64,
            },
            "timeline": {"next_hh01_min": 60, "next_review_time": "08:05"},
            "exceptions": [],
        }
        patches = (
            mock.patch.object(render_push_report, "authoritative_cycle_count", return_value=None),
            mock.patch.object(render_push_report, "authoritative_cycle_duration", return_value=None),
            mock.patch.object(render_push_report, "authoritative_equity", return_value=None),
            mock.patch.object(render_push_report, "authoritative_cum_pnl", return_value=None),
            mock.patch.object(render_push_report, "authoritative_position_count", return_value=None),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            content = render_push_report.render(payload)["content"]
        result = validate_push_format.validate(content, cycle_id=CYCLE)
        self.assertTrue(result["ok"], result)
        # 2026-08-14 起渲染端全量输出：超长内容不压缩、指纹与长决策依据原样保留，
        # 整条交 QQ 外发（本地不截断、不分段；QQ 侧自行分段展示）。
        self.assertIn("y" * 8000, content)
        self.assertIn("业务指纹=" + "b" * 64, content)
        self.assertNotIn("推送过长", content)
        self.assertNotIn("详情见归档", content)

    def test_reconciled_exchange_fill_keeps_agent_decision_banner(self):
        card = {
            "direction_evidence": ["facts verified"],
            "opposing_evidence": ["no new open"],
            "execution_conditions": {"hold": "keep live SL"},
            "invalidation_point": {"position": "live SL"},
            "risk_reward": {"exit_mode": "no_fixed_tp"},
            "portfolio_impact": {"after": "one protected close"},
            "historical_experience": {
                "matched_wins": [], "matched_losses": [],
                "missed_opportunities": [], "usage": "none",
                "reason": "no new open",
            },
            "agent_judgement": "HOLD before the exchange fill",
            "reference_overrides": [],
        }
        payload = {
            "cycle_id": CYCLE, "cycle_count": 1,
            "cycle_duration_s": 10, "hhmm": "07:00",
            "action_taken": "CLOSE", "symbol": "APR",
            "assets": {"live": {
                "equity": 1000, "availBal": 900, "pnl": -8.22,
                "positions": 0,
            }},
            "positions": [],
            "risk": {
                "current_portfolio_imr_ratio": 0.1,
                "max_portfolio_imr_ratio": 0.666,
                "portfolio_imr_ratio_unit": "fraction",
                "lev": 10, "side_pct": 0, "position_count": 0,
                "status": "PASS",
            },
            "market": {
                "btc": 100, "btc_chg24h": 0,
                "eth": 50, "eth_chg24h": 0,
                "regime": "range", "dxy": 100,
            },
            "decision": {
                "summary": "exchange close after HOLD",
                "reason": "existing exchange protection filled",
                "origin": "exchange_reconcile_after_business_terminal",
                "decision_protocol": "decision_card_v1",
                "decision_card": card,
            },
            "execution": {"result": "CLOSE APR", "db_rows_live": 1},
            "business_report_attestation": {
                "trade_count": 1, "sha256": "c" * 64,
            },
            "trades": {"live": [{
                "symbol": "APR-USDT-SWAP", "action": "close",
                "side": "long", "sz": 31, "fill_px": 0.5177,
                "pnl": -8.22,
            }]},
            "timeline": {"next_hh01_min": 60, "next_review_time": "08:05"},
            "exceptions": [],
        }
        patches = (
            mock.patch.object(render_push_report, "authoritative_cycle_count", return_value=None),
            mock.patch.object(render_push_report, "authoritative_cycle_duration", return_value=None),
            mock.patch.object(render_push_report, "authoritative_equity", return_value=None),
            mock.patch.object(render_push_report, "authoritative_cum_pnl", return_value=None),
            mock.patch.object(render_push_report, "authoritative_position_count", return_value=None),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            content = render_push_report.render(payload)["content"]

        self.assertIn("Agent裁决后交易所成交", content)
        self.assertIn("🧭 六项决策卡", content)
        self.assertNotIn("旧轮次无 decision_card_v1", content)


if __name__ == "__main__":
    unittest.main()
