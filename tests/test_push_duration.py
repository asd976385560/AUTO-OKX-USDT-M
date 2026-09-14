# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from scripts import _push_duration as duration
import render_push_report as render
import validate_push_format as validator


class PushDurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "db"
        self.db.mkdir()
        self.cycle = "2026-09-06T20:15"
        self.create_fixture()

    def create_fixture(self, seconds=458):
        start = duration.thresholds.parse_cst(self.cycle)
        fmt = lambda secs: (start + timedelta(seconds=secs)).strftime(
            "%Y-%m-%d %H:%M:%S")
        self.terminal = {
            "schema_version": 1, "cycle_id": self.cycle,
            "status": "completed", "completed_at_cst": fmt(seconds),
            "clock_stop": duration.CLOCK_STOP,
        }
        self.raw = {"status": "ok", "batch_status": "completed",
                    "runner_in_progress": False,
                    "business_terminal": self.terminal}
        self.live = {
            "stage": "live", "cycle_id": self.cycle, "status": "succeeded",
            "returncode": 0, "finished_at": fmt(seconds + 12),
            "collection_gate": {"cycle_id": self.cycle, "status": "met",
                                "completed_at": fmt(min(64, seconds))},
            "business_check": {"ok": True, "business_terminal": self.terminal},
        }
        self.status_path = self.root / "logs/stage-status" / (
            "live-" + self.cycle.replace(":", "-") + ".json")
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_status(self.live)
        with closing(sqlite3.connect(self.db / "live_trades.db")) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS trade_cycles "
                        "(cycle_id TEXT PRIMARY KEY, mode TEXT, decision TEXT, raw TEXT)")
            con.execute("INSERT OR REPLACE INTO trade_cycles VALUES (?,?,?,?)",
                        (self.cycle, "live", "hold", json.dumps(self.raw)))
        with closing(sqlite3.connect(self.db / "analysis.db")) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS analysis_runs "
                        "(cycle_id TEXT PRIMARY KEY, ts TEXT, status TEXT)")
            con.execute("INSERT OR REPLACE INTO analysis_runs VALUES (?,?,?)",
                        (self.cycle, fmt(min(255, seconds)), "ok"))

    def write_status(self, value):
        self.status_path.write_text(json.dumps(value), encoding="utf-8")

    def write_raw(self, value):
        with closing(sqlite3.connect(self.db / "live_trades.db")) as con, con:
            con.execute("UPDATE trade_cycles SET raw=? WHERE cycle_id=?",
                        (json.dumps(value), self.cycle))

    def payload(self):
        return {
            "cycle_id": self.cycle, "hhmm": self.cycle[-5:],
            "cycle_count": 6398, "cycle_duration_s": 0,
            "action_taken": "HOLD", "symbol": "ZEC",
            "assets": {"live": {"equity": 1000, "availBal": 800,
                                  "pnl": 0, "positions": 0}},
            "positions": [],
            "risk": {"current_portfolio_imr_ratio": 0.1,
                     "max_portfolio_imr_ratio": 0.666,
                     "portfolio_imr_ratio_unit": "fraction", "lev": 5,
                     "side_pct": 0, "position_count": 0, "status": "PASS"},
            "market": {"btc": 65000, "btc_chg24h": 0, "eth": 3500,
                       "eth_chg24h": 0, "regime": "range", "dxy": 120},
            "decision": {"summary": "HOLD", "reason": "无新增操作",
                         "decision_protocol": "minimal_decision_v2"},
            "execution": {"result": "HOLD", "db_rows_live": 0},
            "business_report_attestation": {"trade_count": 0, "sha256": "a" * 64},
            "inter_report_exchange_attestation": {
                "fill_count": 0, "excluded_count": 0, "sha256": "b" * 64},
            "timeline": {"next_hh01_min": 45, "next_review_time": "08:05"},
            "exceptions": [],
        }

    def render_context(self):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(render, "DB_ROOT", str(self.db)))
        for name in ("authoritative_cycle_count", "authoritative_equity",
                     "authoritative_cum_pnl", "authoritative_position_count",
                     "authoritative_cycle_duration"):
            stack.enter_context(mock.patch.object(render, name, return_value=None))
        stack.enter_context(mock.patch.object(duration,
            "BUSINESS_DURATION_REQUIRED_FROM", "2026-09-06T00:00"))
        return stack

    def test_exact_6398_is_458_seconds(self):
        evidence = duration.read_business_duration(self.db, self.cycle)
        self.assertEqual("known", evidence["status"], evidence)
        self.assertEqual(458, evidence["elapsed_seconds"])
        self.assertTrue(evidence["completed_at_cst"].endswith("+08:00"))

    def test_render_same_across_local_timezones_and_later_replay(self):
        headers = []
        for local in (datetime(2026, 9, 6, 7, 22, 54),
                      datetime(2026, 9, 6, 20, 22, 54),
                      datetime(2026, 9, 7, 12, 0, 0)):
            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return local.replace(tzinfo=tz) if tz else local
            with self.render_context(), mock.patch.object(render, "datetime", Clock):
                headers.append(render.render(self.payload())["content"].splitlines()[0])
        self.assertEqual(1, len(set(headers)))
        self.assertIn("第6398轮 / ⏱458s / live / HOLD ZEC", headers[0])

    def test_utc_z_and_explicit_offset_proofs_are_equivalent(self):
        for offset in (timezone.utc, timezone(timedelta(hours=-5))):
            live = copy.deepcopy(self.live)
            converted = duration.thresholds.parse_cst(
                self.terminal["completed_at_cst"]).astimezone(offset).isoformat()
            live["business_check"]["business_terminal"]["completed_at_cst"] = (
                converted.replace("+00:00", "Z"))
            self.write_status(live)
            self.assertEqual(458, duration.read_business_duration(
                self.db, self.cycle)["elapsed_seconds"])

    def test_cross_midnight_and_late_completion_keep_actual_duration(self):
        self.cycle = "2026-09-06T23:45"
        self.create_fixture(seconds=1201)
        self.assertEqual(1201, duration.read_business_duration(
            self.db, self.cycle)["elapsed_seconds"])

    def test_missing_evidence_does_not_create_database_or_use_production(self):
        missing = self.root / "missing" / "db"
        with mock.patch.object(duration, "_read_row") as read:
            result = duration.read_business_duration(missing, self.cycle)
        self.assertEqual("unknown", result["status"])
        read.assert_not_called()
        self.assertFalse(missing.exists())

    def test_failed_or_mismatched_stage_is_unknown(self):
        for key, value in (("cycle_id", "2026-09-06T20:00"),
                           ("stage", "push"), ("status", "failed"),
                           ("returncode", 1)):
            with self.subTest(key=key):
                live = copy.deepcopy(self.live)
                live[key] = value
                self.write_status(live)
                self.assertEqual("unknown", duration.read_business_duration(
                    self.db, self.cycle)["status"])

    def test_partial_or_unconfirmed_trade_is_unknown(self):
        for key, value in (("status", "error"), ("batch_status", "partial"),
                           ("runner_in_progress", True)):
            with self.subTest(key=key):
                raw = copy.deepcopy(self.raw)
                raw[key] = value
                self.write_raw(raw)
                self.assertEqual("unknown", duration.read_business_duration(
                    self.db, self.cycle)["status"])

    def test_missing_malformed_and_wrong_cycle_terminal_are_unknown(self):
        for key, value in (("schema_version", True), ("status", "failed"),
                           ("cycle_id", "2026-09-06T20:00"),
                           ("clock_stop", "push_sent"),
                           ("completed_at_cst", "not-a-date"),
                           ("completed_at_cst", "2026-09-06 20:14:00")):
            with self.subTest(key=key, value=value):
                live = copy.deepcopy(self.live)
                live["business_check"]["business_terminal"][key] = value
                self.write_status(live)
                self.assertEqual("unknown", duration.read_business_duration(
                    self.db, self.cycle)["status"])
        self.write_status({**self.live, "business_check": {"ok": True}})
        self.assertEqual("unknown", duration.read_business_duration(
            self.db, self.cycle)["status"])

    def test_live_receipt_and_ledger_must_agree(self):
        raw = copy.deepcopy(self.raw)
        raw["business_terminal"]["completed_at_cst"] = "2026-09-06 20:22:39"
        self.write_raw(raw)
        self.assertEqual("business_terminal_mismatch", duration.read_business_duration(
            self.db, self.cycle)["reason"])

    def test_collection_and_analysis_order_must_be_valid(self):
        live = copy.deepcopy(self.live)
        live["collection_gate"]["completed_at"] = "2026-09-06 20:23:00"
        self.write_status(live)
        self.assertEqual("business_time_order_invalid", duration.read_business_duration(
            self.db, self.cycle)["reason"])
        self.write_status(self.live)
        with closing(sqlite3.connect(self.db / "analysis.db")) as con, con:
            con.execute("UPDATE analysis_runs SET ts='2026-09-06 20:23:00'")
        self.assertEqual("analysis_time_order_invalid", duration.read_business_duration(
            self.db, self.cycle)["reason"])

    def test_damaged_json_and_database_fail_to_unknown(self):
        self.status_path.write_text("{bad-json", encoding="utf-8")
        self.assertEqual("unknown", duration.read_business_duration(
            self.db, self.cycle)["status"])
        self.write_status(self.live)
        (self.db / "live_trades.db").write_bytes(b"not-sqlite")
        self.assertEqual("unknown", duration.read_business_duration(
            self.db, self.cycle)["status"])

    def test_render_validate_round_trip_and_fake_zero_rejected(self):
        with self.render_context():
            content = render.render(self.payload())["content"]
            result = validator.validate(content, cycle_id=self.cycle, db_root=self.db)
            bad = validator.validate(content.replace("⏱458s", "⏱0s"),
                                     cycle_id=self.cycle, db_root=self.db)
        self.assertTrue(result["ok"], result)
        self.assertFalse(bad["ok"])
        self.assertIn("标题耗时与同轮业务完成证据不一致", bad["errors"])

    def test_unknown_keeps_failure_report_sendable_with_reason(self):
        self.write_status({**self.live, "status": "failed"})
        with self.render_context():
            content = render.render(self.payload())["content"]
            result = validator.validate(content, cycle_id=self.cycle, db_root=self.db)
        self.assertIn("⏱未知 /", content)
        self.assertIn("本轮耗时未知：", content)
        self.assertNotIn("⏱0s", content)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["warnings"])

    def test_numeric_time_cannot_be_claimed_when_evidence_unreadable(self):
        with self.render_context():
            content = render.render(self.payload())["content"]
            self.write_status({**self.live, "status": "failed"})
            result = validator.validate(content, cycle_id=self.cycle, db_root=self.db)
        self.assertFalse(result["ok"])

    def test_explicit_unknown_remains_valid_if_evidence_arrives_later(self):
        self.write_status({**self.live, "status": "failed"})
        with self.render_context():
            content = render.render(self.payload())["content"]
            self.write_status(self.live)
            result = validator.validate(content, cycle_id=self.cycle, db_root=self.db)
        self.assertTrue(result["ok"], result)

    def test_pre_activation_archive_not_rejudged(self):
        with self.render_context():
            content = render.render(self.payload())["content"]
        with mock.patch.object(duration, "read_business_duration") as reader:
            result = validator.validate(content.replace("⏱458s", "⏱0s"),
                                        cycle_id=self.cycle, db_root=self.db)
        reader.assert_not_called()
        self.assertTrue(result["ok"], result)

    def test_activation_boundary_is_forward_only(self):
        self.assertFalse(duration.duration_contract_active("2026-09-06T21:15"))
        self.assertTrue(duration.duration_contract_active("2026-09-06T21:30"))
        for invalid in (None, "invalid", "2026-99-06T21:30"):
            self.assertFalse(duration.duration_contract_active(invalid))

    def test_fused_cli_passes_isolated_database_root_to_real_validator(self):
        out = self.root / "content.txt"
        argv = ["render_push_report.py", "--json", "{}", "--out-file", str(out),
                "--db-root", str(self.db), "--validate-cycle-id", self.cycle,
                "--validate-no-repair-queue"]
        with self.render_context(), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(render, "load_payload", return_value=self.payload()), \
                mock.patch.object(validator, "write_repair_queue") as repair, \
                mock.patch.object(validator, "close_healed_push_format") as close, \
                mock.patch.object(render, "LEDGER_DB", "unused"), \
                mock.patch.object(render, "ACCOUNT_DB", "unused"), \
                redirect_stdout(io.StringIO()) as stdout:
            rc = render.main()
        receipt = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual(0, rc, receipt)
        self.assertEqual(458, receipt["validation"]["cycle_duration_evidence"]["elapsed_seconds"])
        repair.assert_not_called()
        close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
