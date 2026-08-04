# -*- coding: utf-8 -*-
"""Isolated report-pipeline regressions; production DBs and push are untouched."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_push_payload  # noqa: E402
import daily_report_writer  # noqa: E402
import validate_daily_report  # noqa: E402


TRADE_SCHEMA = """
CREATE TABLE trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, ts TEXT NOT NULL,
  symbol TEXT NOT NULL, action TEXT NOT NULL, side TEXT, sz REAL,
  fill_px REAL, lev REAL, margin REAL, notional REAL, score_total INTEGER,
  reasoning TEXT, deviation TEXT, degradation TEXT, pnl REAL, raw TEXT
);
"""

LEDGER_SCHEMA = """
CREATE TABLE execution_intents(
  profile TEXT,cycle_id TEXT,symbol TEXT,action TEXT,side TEXT,
  request_fingerprint TEXT,request_json TEXT,state TEXT,
  reserved_at TEXT,updated_at TEXT,submitted_at TEXT,
  completed_at TEXT,ord_id TEXT,receipt_json TEXT,error TEXT,
  PRIMARY KEY(profile,cycle_id,symbol,action,side)
);
"""

DAILY_SCHEMA = """
CREATE TABLE daily_reports(
  ts TEXT NOT NULL,profile TEXT NOT NULL,open_count INTEGER,
  close_count INTEGER,total_pnl REAL,total_fees REAL,best_trade TEXT,
  worst_trade TEXT,summary TEXT,lessons TEXT,raw TEXT,trade_day_num INTEGER,
  PRIMARY KEY(ts,profile)
);
"""

WEEKLY_SCHEMA = """
CREATE TABLE weekly_reports(
  week_start_ts TEXT NOT NULL,profile TEXT NOT NULL,open_count INTEGER,
  close_count INTEGER,total_pnl REAL,win_rate REAL,avg_hold_hours REAL,
  margin_util_pct REAL,idle_ratio REAL,summary TEXT,lessons TEXT,raw TEXT,
  trade_week_num INTEGER,PRIMARY KEY(week_start_ts,profile)
);
"""


def _create_db(path: Path, schema: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(schema)
        con.commit()
    finally:
        con.close()


class ReportRevisionTests(unittest.TestCase):
    def test_correction_marks_manual_resend_review_without_auto_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "account.db"
            _create_db(db, DAILY_SCHEMA)
            initial_raw = json.dumps({
                "report_audit": {
                    "version": 1,
                    "period_kind": "daily",
                    "revision": {
                        "schema_version": 1,
                        "number": 1,
                        "kind": "initial",
                        "corrected": False,
                        "resend_review_required": False,
                        "resend_status": "not_requested",
                        "auto_resend": False,
                    },
                }
            })
            con = sqlite3.connect(db)
            try:
                for profile in ("live", "demo"):
                    con.execute(
                        "INSERT INTO daily_reports VALUES("
                        "?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("2026-07-28 08:05:00", profile, 0, 0, 0.0,
                         0.0, None, None, "old", "old", initial_raw, 65),
                    )
                con.commit()
                payload = {
                    "ts": "2026-07-28 08:05:00",
                    "summary": "corrected",
                    "lessons": "corrected",
                    "raw": initial_raw,
                    "live_open_count": 0,
                    "live_close_count": 1,
                    "live_total_pnl": -1.0,
                    "demo_open_count": 0,
                    "demo_close_count": 0,
                    "demo_total_pnl": 0.0,
                }
                result = daily_report_writer.correct_existing_daily(
                    con, payload, ["live", "demo"], False)
            finally:
                con.close()

        revision = json.loads(
            result["targets"][0]["fields"]["raw"]
        )["report_audit"]["revision"]
        self.assertEqual(revision["number"], 2)
        self.assertEqual(revision["kind"], "corrected")
        self.assertTrue(revision["resend_review_required"])
        self.assertEqual(revision["resend_status"], "review_required")
        self.assertFalse(revision["auto_resend"])


class DailyArtifactTests(unittest.TestCase):
    def test_account_bill_window_is_half_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "account.db"
            con = sqlite3.connect(db)
            try:
                con.execute(
                    "CREATE TABLE account_bills("
                    "profile TEXT,type TEXT,ts TEXT,"
                    "bal_change REAL,fee REAL,pnl REAL)")
                con.executemany(
                    "INSERT INTO account_bills VALUES(?,?,?,?,?,?)",
                    [
                        ("live", "2", "2026-07-30 08:04:59", 100, 0, 0),
                        ("live", "2", "2026-07-30 08:05:00", 1, 0, 1),
                        ("live", "8", "2026-07-31 08:04:59", 2, 0, 2),
                        ("live", "2", "2026-07-31 08:05:00", 100, 0, 0),
                    ],
                )
                con.commit()
            finally:
                con.close()

            result = daily_report_writer._account_bill_net_for_window(
                db,
                "live",
                "2026-07-30 08:05:00",
                "2026-07-31 08:05:00",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["net"], 3.0)
        self.assertTrue(result["period_end_exclusive"])

    def test_daily_markdown_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "daily"
            missing_db = root / "missing.db"
            payload = {
                "ts": "2026-07-28 08:05:00",
                "live_equity": 100.0,
                "live_realized_pnl": 0.0,
                "live_positions_summary": "无",
                "live_open_count": 0,
                "live_close_count": 0,
                "live_total_pnl": 0.0,
                "live_total_fees": 0.0,
                "demo_equity": 100.0,
                "demo_realized_pnl": 0.0,
                "demo_positions_summary": "无",
                "demo_open_count": 0,
                "demo_close_count": 0,
                "demo_total_pnl": 0.0,
                "demo_total_fees": 0.0,
                "summary": "summary",
                "lessons": "lessons",
                "raw": json.dumps({
                    "report_audit": {
                        "revision": {
                            "number": 1,
                            "kind": "initial",
                            "resend_review_required": False,
                        }
                    }
                }),
            }
            with (
                mock.patch.object(
                    daily_report_writer, "REPORTS_DIR", out_dir),
                mock.patch.object(
                    daily_report_writer, "DB_PATH", missing_db),
            ):
                path = Path(daily_report_writer.write_markdown(payload, True))

            self.assertEqual(path.name, "daily-2026-07-28.md")
            self.assertIn(
                "report_revision: 1",
                path.read_text(encoding="utf-8"),
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn(
                "统计窗口: [2026-07-27 08:00:00, "
                "2026-07-28 08:00:00)",
                content,
            )
            self.assertIn("本复盘周期成交开仓", content)
            self.assertEqual(list(out_dir.glob("*.tmp")), [])

    def test_commit_precedes_markdown_and_file_failure_keeps_db_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE facts(value TEXT)")
            con.execute("INSERT INTO facts VALUES('committed')")
            with mock.patch.object(
                daily_report_writer,
                "write_markdown",
                side_effect=RuntimeError("simulated file failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "file failure"):
                    daily_report_writer._commit_then_write_daily(
                        con, {"ts": "2026-07-28 08:05:00"}, True)
            con.close()
            check = sqlite3.connect(db)
            try:
                self.assertEqual(
                    check.execute("SELECT value FROM facts").fetchone()[0],
                    "committed",
                )
            finally:
                check.close()


class WeeklyArtifactTests(unittest.TestCase):
    def test_weekly_markdown_is_atomic_and_uses_percent_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "weekly"
            payload = {
                "week_start_ts": "2026-07-27 00:00:00",
                "period_start_ts": "2026-07-20 00:00:00",
                "period_end_ts": "2026-07-27 00:00:00",
                "trade_week_num": 7,
                "live_reconcile_status": "clean",
                "live_reconcile_issue_count": 0,
                "live_open_count": 2,
                "live_close_count": 1,
                "live_total_pnl": 1.25,
                "live_win_rate": 50.0,
                "live_risk_rejected_open_summary": "0 笔",
                "demo_open_count": 1,
                "demo_close_count": 1,
                "demo_total_pnl": -0.5,
                "demo_win_rate": 0.0,
                "demo_risk_rejected_open_summary": "1 笔（已分离）",
                "summary": "summary",
                "lessons": "lessons",
            }
            with mock.patch.object(
                    daily_report_writer, "WEEKLY_REPORTS_DIR", out_dir):
                path = Path(daily_report_writer.write_weekly_markdown(
                    payload, True))

            self.assertEqual(path.name, "weekly-2026-07-27.md")
            content = path.read_text(encoding="utf-8")
            self.assertIn("[2026-07-20 00:00:00, 2026-07-27 00:00:00)", content)
            self.assertIn("胜率单位：百分数（0–100）", content)
            self.assertIn("50.00%", content)
            self.assertEqual(list(out_dir.glob("*.tmp")), [])

    def test_commit_precedes_markdown_and_file_failure_keeps_db_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE facts(value TEXT)")
            con.execute("INSERT INTO facts VALUES('committed')")
            with mock.patch.object(
                daily_report_writer,
                "write_weekly_markdown",
                side_effect=RuntimeError("simulated file failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "file failure"):
                    daily_report_writer._commit_then_write_weekly(
                        con, {"week_start_ts": "2026-07-27 00:00:00"}, True)
            con.close()
            check = sqlite3.connect(db)
            try:
                self.assertEqual(
                    check.execute("SELECT value FROM facts").fetchone()[0],
                    "committed",
                )
            finally:
                check.close()

    def test_readonly_weekly_backfill_does_not_change_database_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "account.db"
            out_dir = root / "weekly"
            _create_db(db, WEEKLY_SCHEMA)
            con = sqlite3.connect(db)
            try:
                for profile in ("live", "demo"):
                    con.execute(
                        "INSERT INTO weekly_reports("
                        "week_start_ts,profile,open_count,close_count,total_pnl,"
                        "win_rate,summary,lessons,raw,trade_week_num)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("2026-07-27 00:00:00", profile, 1, 1, 0.5,
                         0.5, "summary", "lessons", "{}", 7),
                    )
                con.commit()
            finally:
                con.close()
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            ro = sqlite3.connect(
                f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
            try:
                payload = daily_report_writer.load_existing_weekly_payload(
                    ro, "2026-07-27 00:00:00")
            finally:
                ro.close()
            with mock.patch.object(
                    daily_report_writer, "WEEKLY_REPORTS_DIR", out_dir):
                daily_report_writer.write_weekly_markdown(payload, True)
            after = hashlib.sha256(db.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            ro = sqlite3.connect(
                f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
            try:
                self.assertEqual(ro.execute(
                    "PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                ro.close()
            self.assertTrue(
                (out_dir / "weekly-2026-07-27.md").exists())


class WeeklyMigrationBoundaryTests(unittest.TestCase):
    def test_one_off_weekly_migration_is_not_published(self):
        script = ROOT / "scripts" / "migrate_weekly_win_rate_pct.py"
        self.assertFalse(script.exists())
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/scripts/migrate_weekly_win_rate_pct.py", gitignore)


class DailyValidatorTests(unittest.TestCase):
    def test_validator_checks_report_time_facts_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = root / "account.db"
            live = root / "live_trades.db"
            demo = root / "demo_trades.db"
            ledger = root / "ledger.db"
            report = root / "daily-2026-07-28.md"
            _create_db(account, DAILY_SCHEMA)
            _create_db(live, TRADE_SCHEMA)
            _create_db(demo, TRADE_SCHEMA)
            _create_db(ledger, LEDGER_SCHEMA)
            con = sqlite3.connect(live)
            try:
                con.executemany(
                    "INSERT INTO trades("
                    "cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        ("l1", "2026-07-28 01:00:00", "BTC-USDT-SWAP",
                         "open", "long", 1, 10, 0, '{"ok":true}'),
                        ("l2", "2026-07-28 02:00:00", "BTC-USDT-SWAP",
                         "close", "long", 1, 11, 1.0, '{"ok":true}'),
                    ],
                )
                con.commit()
            finally:
                con.close()
            con = sqlite3.connect(ledger)
            try:
                con.execute(
                    "INSERT INTO execution_intents VALUES("
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("demo", "d1", "ETH-USDT-SWAP", "open", "long",
                     "f", "{}", "failed_clean", "2026-07-28 03:00:00",
                     "2026-07-28 03:00:01", None, None, None, None,
                     "risk_reject:test"),
                )
                con.commit()
            finally:
                con.close()

            with (
                mock.patch.object(daily_report_writer, "LIVE_TRADES_DB", live),
                mock.patch.object(daily_report_writer, "DEMO_TRADES_DB", demo),
                mock.patch.object(daily_report_writer, "LEDGER_DB", ledger),
            ):
                prepared = daily_report_writer.prepare_daily_payload({
                    "ts": "2026-07-28 08:05:00",
                    "live_reconcile_status": "clean",
                    "live_reconcile_issue_count": 0,
                    "summary": "ok",
                    "lessons": "ok",
                    "raw": "{}",
                })
            con = sqlite3.connect(account)
            try:
                for index, profile in enumerate(("live", "demo"), start=1):
                    fields = daily_report_writer._daily_fields(
                        prepared, profile)
                    con.execute(
                        "INSERT INTO daily_reports VALUES("
                        "?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            fields["ts"], profile, fields["open_count"],
                            fields["close_count"], fields["total_pnl"],
                            fields["total_fees"], fields["best_trade"],
                            fields["worst_trade"], fields["summary"],
                            fields["lessons"], fields["raw"], 65,
                        ),
                    )
                con.commit()
            finally:
                con.close()

            report.write_text(
                """# 📊 小灵日报 2026-07-28
> ts: 2026-07-28 08:05:00
> 统计窗口: [2026-07-27 08:00:00, 2026-07-28 08:00:00)，UTC+8（固定24小时）
> **报告状态：最终报告｜live 对账已清零**
> report_revision: 1 | revision_kind: initial | resend_review_required: false | auto_resend: false
## 💰 资产
### 🟢 实盘
### 🟡 模拟盘
## 📈 持仓
### 🟢 实盘
### 🟡 模拟盘
## 🎯 交易
### 🟢 实盘
- 本复盘周期成交开仓: 1 笔
- 本复盘周期成交平仓: 1 笔
- 开仓尝试被风控拒绝: 0 笔
- 净 PnL: $1.00
### 🟡 模拟盘
- 本复盘周期成交开仓: 0 笔
- 本复盘周期成交平仓: 0 笔
- 开仓尝试被风控拒绝: 1 笔
- 净 PnL: $0.00
## ⚠️ 异常 / 🛠 自修
无
## 🌍 市场
无
## 🧠 教训
无
## 详细 summary
ok
""",
                encoding="utf-8",
            )
            result = validate_daily_report.validate_report(
                report, account, live, demo, ledger)
            self.assertTrue(result["ok"], result["errors"])
            self.assertIn("risk_reject", result["checks"])
            self.assertIn("revision", result["checks"])
            self.assertIn("daily_window_24h", result["checks"])


class PushMacroSummaryTests(unittest.TestCase):
    def test_current_usd_broad_schema_does_not_render_missing(self):
        value = build_push_payload._usd_broad_summary({
            "dxy_broad_usd_trade_weighted": 120.71,
            "dxy_zone": "ELEVATED",
        })
        self.assertEqual(value, "USD_BROAD 120.71 ELEVATED")
        self.assertNotIn(" -", value)

    def test_analysis_dxy_broad_aliases_are_supported(self):
        value = build_push_payload._usd_broad_summary({
            "dxy_broad_value": 120.71,
            "dxy_broad_zone": "ELEVATED",
            "dxy_broad_d1": -0.08,
        })
        self.assertEqual(value, "USD_BROAD 120.71 ELEVATED")

    def test_error_decision_is_never_rendered_as_hold(self):
        self.assertEqual(build_push_payload._map_decision("error"), "ERROR")
        self.assertEqual(
            build_push_payload._map_decision("degraded"), "DEGRADED")
        self.assertEqual(
            build_push_payload._map_decision("unexpected"), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
