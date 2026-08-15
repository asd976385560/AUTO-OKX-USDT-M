# -*- coding: utf-8 -*-
"""Isolated report-pipeline regressions; production DBs and push are untouched."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _p in (SCRIPTS,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build_push_payload  # noqa: E402
import daily_report_writer  # noqa: E402
import missed_opps_writer  # noqa: E402
import render_push_report  # noqa: E402
import validate_daily_report  # noqa: E402
import validate_push_format  # noqa: E402


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


class MissedOpportunityMaturityTests(unittest.TestCase):
    def test_report_window_is_shifted_as_one_continuous_24h_window(self):
        self.assertEqual(
            ("2026-08-11 04:00:00", "2026-08-12 04:00:00"),
            missed_opps_writer._matured_candidate_window(
                "2026-08-11 08:00:00", "2026-08-12 08:00:00"),
        )
        self.assertEqual(
            ("2026-08-11 04:00:00", "2026-08-12 04:00:00"),
            daily_report_writer._missed_opps_candidate_window(
                "2026-08-11 08:00:00", "2026-08-12 08:00:00"),
        )
        self.assertEqual(
            ("2026-08-11 04:00:00", "2026-08-12 04:00:00"),
            validate_daily_report._expected_missed_candidate_window(
                "2026-08-11 08:00:00", "2026-08-12 08:00:00"),
        )

    def test_four_hour_outcome_requires_exact_16_quarter_hour_starts(self):
        start = "2026-08-11T00:00:00Z"
        rows = [
            (f"2026-08-11T{hour:02d}:{minute:02d}:00Z", 1, 1, 1, 1)
            for hour in range(4)
            for minute in (0, 15, 30, 45)
        ]
        self.assertTrue(
            missed_opps_writer._complete_four_hour_rows(rows, start))
        self.assertFalse(
            missed_opps_writer._complete_four_hour_rows(rows[:-1], start))
        gapped = list(rows)
        gapped[8] = ("2026-08-11T02:15:00Z", 1, 1, 1, 1)
        self.assertFalse(
            missed_opps_writer._complete_four_hour_rows(gapped, start))

    def test_four_hour_outcome_rejects_invalid_ohlc(self):
        start = "2026-08-11T00:00:00Z"
        rows = [
            (f"2026-08-11T{hour:02d}:{minute:02d}:00Z", 10, 11, 9, 10)
            for hour in range(4)
            for minute in (0, 15, 30, 45)
        ]
        invalid_high = list(rows)
        invalid_high[3] = (*invalid_high[3][:2], 8, 9, 10)
        self.assertFalse(missed_opps_writer._complete_four_hour_rows(
            invalid_high, start))
        nonfinite = list(rows)
        nonfinite[4] = (*nonfinite[4][:4], float("nan"))
        self.assertFalse(missed_opps_writer._complete_four_hour_rows(
            nonfinite, start))

    def test_writer_count_excludes_both_shifted_window_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            lessons = Path(tmp) / "lessons.db"
            con = sqlite3.connect(lessons)
            try:
                con.execute("CREATE TABLE missed_opportunities(ts TEXT)")
                con.executemany(
                    "INSERT INTO missed_opportunities VALUES(?)",
                    [
                        ("2026-08-11 03:45:00",),
                        ("2026-08-11 04:00:00",),
                        ("2026-08-12 03:45:00",),
                        ("2026-08-12 04:00:00",),
                    ],
                )
                con.commit()
            finally:
                con.close()
            with mock.patch.object(
                    daily_report_writer, "LESSONS_DB", lessons):
                self.assertEqual(
                    2,
                    daily_report_writer._missed_opps_window_count(
                        "2026-08-11 08:00:00",
                        "2026-08-12 08:00:00",
                    ),
                )


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
                # 2026-08-06 demo 全量下线：日报只落 live 一行
                for index, profile in enumerate(("live",), start=1):
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
## 📈 持仓
### 🟢 实盘
## 🎯 交易
### 🟢 实盘
- 本复盘周期成交开仓: 1 笔
- 本复盘周期成交平仓: 1 笔
- 开仓尝试被风控拒绝: 0 笔
- 净 PnL: $1.00
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
                report, account, live, ledger)
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


CYCLE_SCHEMA = """
CREATE TABLE trade_cycles(
  cycle_id TEXT PRIMARY KEY, ts TEXT, mode TEXT, decision TEXT,
  n_orders INTEGER, equity REAL, note TEXT, raw TEXT
);
"""


class PushSingleBookPayloadTests(unittest.TestCase):
    """2026-08-06 demo 全量下线后 push payload 只组 live 一本账。

    原 `PushDemoDecoupleTests` 锁的是「demo 缺行必须标 PENDING 而非 UNKNOWN」；
    demo 整体移除后，该契约的有效残余是 **live 缺行 vs live decision 不可解释**
    两种情况必须区分——前者是异常但可渲染，后者必须被 UNKNOWN 拦住整条推送。"""

    CYCLE = "2026-08-06T12:00"

    def _db_root(
        self, tmp: str, live_row: bool, *, cycle: str | None = None
    ) -> Path:
        root = Path(tmp)
        cycle = cycle or self.CYCLE
        con = sqlite3.connect(root / "live_trades.db")
        try:
            con.executescript(CYCLE_SCHEMA + TRADE_SCHEMA)
            if live_row:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-06 12:01:00", "full", "hold",
                     0, None, "", "{}"))
            con.commit()
        finally:
            con.close()
        return root

    def test_single_live_book_renders_as_live_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_push_payload.build(
                self._db_root(tmp, live_row=True), self.CYCLE)
        self.assertEqual(payload["action_taken"], "HOLD")
        self.assertIn("实盘", payload["summary"])
        self.assertNotIn("双盘", payload["summary"])
        self.assertNotIn("demo", payload["trades"])
        self.assertNotIn("demo", payload["assets"])
        self.assertEqual(payload["channel"], "live")
        self.assertNotIn("db_rows_demo", payload["execution"])

    def test_build_reports_exchange_fill_from_prior_business_cycle(self):
        report_cycle = "2026-08-06T12:15"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(
                tmp, live_row=True, cycle=report_cycle)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                    "fill_px,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?)",
                    (self.CYCLE, "2026-08-06 12:14:18", "BCH-USDT-SWAP",
                     "close", "short", 36.0, 204.1, 6.84,
                     json.dumps({
                         "reconcile_source": "exchange_fills_reconcile",
                         "ord_ids": ["3833488461226856449"],
                     })),
                )
                con.commit()
            finally:
                con.close()

            payload = build_push_payload.build(
                root, report_cycle,
                now=datetime(2026, 8, 6, 12, 16, tzinfo=timezone(
                    timedelta(hours=8))),
            )

        self.assertEqual(payload["action_taken"], "HOLD")
        self.assertEqual(
            payload["business_report_attestation"]["trade_count"], 0)
        interval = payload["inter_report_exchange_attestation"]
        self.assertEqual(interval["fill_count"], 1)
        self.assertEqual(
            interval["window_start_exclusive_cst"],
            "2026-08-06 12:00:00",
        )
        self.assertEqual(
            interval["window_end_inclusive_cst"],
            "2026-08-06 12:15:00",
        )
        self.assertEqual(interval["fills"][0]["symbol"], "BCH-USDT-SWAP")
        self.assertEqual(
            interval["fills"][0]["ord_ids"],
            ["3833488461226856449"],
        )

    def test_inter_report_exchange_window_is_half_open_and_deduplicated(self):
        report_cycle = "2026-08-06T12:15"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=True, cycle=report_cycle)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                rows = [
                    ("2026-08-06T12:00", "2026-08-06 12:00:00", "BOUNDARY-OLD", "exchange_fills_reconcile"),
                    ("2026-08-06T12:00", "2026-08-06 12:00:01", "INSIDE", "exchange_fills_reconcile"),
                    ("2026-08-06T12:00", "2026-08-06 12:15:00", "BOUNDARY-NOW", "execution_journal_recovery"),
                    (report_cycle, "2026-08-06 12:14:00", "CURRENT-CYCLE", "exchange_fills_reconcile"),
                    ("2026-08-06T12:00", "2026-08-06 12:14:00", "NOT-RECOVERED", "agent_execution"),
                ]
                for index, (cycle_id, ts, symbol, source) in enumerate(rows):
                    con.execute(
                        "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                        "fill_px,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?)",
                        (cycle_id, ts, symbol, "close", "long", 1.0,
                         10.0, 1.0, json.dumps({
                             "reconcile_source": source,
                             "ord_ids": [f"ord-{index}", f"ord-{index}"],
                         })),
                    )
                con.commit()
            finally:
                con.close()

            interval = build_push_payload._inter_report_exchange_attestation(
                str(root), report_cycle)

        self.assertEqual(interval["fill_count"], 2)
        self.assertEqual(
            [row["symbol"] for row in interval["fills"]],
            ["INSIDE", "BOUNDARY-NOW"],
        )
        self.assertEqual(interval["fills"][0]["ord_ids"], ["ord-1"])

    def test_inter_report_exchange_accepts_direct_fill_with_unique_ord_id(self):
        report_cycle = "2026-08-06T12:15"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=True, cycle=report_cycle)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                rows = [
                    (
                        "DIRECT-CONFIRMED",
                        {"fill_source": "fills",
                         "ts_source": "fills.fillTime",
                         "ordId": "ord-direct"},
                    ),
                    (
                        "DIRECT-NO-ORD",
                        {"fill_source": "fills",
                         "ts_source": "fills.fillTime"},
                    ),
                    (
                        "DIRECT-WRONG-TS",
                        {"fill_source": "fills",
                         "ts_source": "writer_commit",
                         "ordId": "ord-untrusted"},
                    ),
                ]
                for index, (symbol, raw) in enumerate(rows):
                    con.execute(
                        "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                        "fill_px,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?)",
                        (self.CYCLE, f"2026-08-06 12:14:1{index}", symbol,
                         "open", "long", 1.0, 10.0, 0.0,
                         json.dumps(raw)),
                    )
                con.commit()
            finally:
                con.close()

            interval = build_push_payload._inter_report_exchange_attestation(
                str(root), report_cycle)

        self.assertEqual(interval["fill_count"], 1)
        self.assertEqual(
            interval["fills"][0]["symbol"], "DIRECT-CONFIRMED")
        self.assertEqual(
            interval["fills"][0]["reconcile_source"], "fills")
        self.assertEqual(
            interval["fills"][0]["ord_ids"], ["ord-direct"])

    def test_position_projection_uses_half_open_window_and_marks_changes(self):
        baseline = [{
            "symbol": "ETH-USDT-SWAP", "side": "long", "sz": 2.0,
            "avgPx": 100.0, "lev": 10.0, "upl": 1.0,
        }]
        trades = [
            {"id": 1, "ts": "2026-08-06 12:00:59",
             "symbol": "ETH-USDT-SWAP", "action": "open",
             "side": "long", "sz": 1.0, "fill_px": 99.0, "lev": 10.0},
            {"id": 2, "ts": "2026-08-06 12:01:00",
             "symbol": "ETH-USDT-SWAP", "action": "open",
             "side": "long", "sz": 1.0, "fill_px": 99.0, "lev": 10.0},
            {"id": 3, "ts": "2026-08-06 12:02:00",
             "symbol": "BTC-USDT-SWAP", "action": "open",
             "side": "short", "sz": 3.0, "fill_px": 90.0, "lev": 10.0},
            {"id": 4, "ts": "2026-08-06 12:04:00",
             "symbol": "ETH-USDT-SWAP", "action": "close",
             "side": "long", "sz": 2.0, "fill_px": 101.0, "lev": 10.0},
        ]

        projected = build_push_payload._project_positions_through_trades(
            baseline, trades, "2026-08-06 12:01:00",
            as_of="2026-08-06 12:03:00")

        self.assertEqual(
            [(row["symbol"], row["sz"]) for row in projected],
            [("ETH-USDT-SWAP", 2.0), ("BTC-USDT-SWAP", 3.0)],
        )
        self.assertNotIn("_projected_after_baseline", projected[0])
        self.assertTrue(projected[1]["_projected_after_baseline"])
        self.assertTrue(projected[1]["_projected_open_after_baseline"])

    def test_build_projects_post_facts_open_into_positions_with_stop(self):
        cst = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=False)
            facts = {
                "cycle_id": self.CYCLE,
                "status": "ok",
                "as_of": "2026-08-06 12:01:00",
                "balance": {"totalEq": 1000.0},
                "positions": [{
                    "instId": "ETH-USDT-SWAP", "posSide": "long",
                    "contracts": 2.0, "avgPx": 100.0, "lever": 10.0,
                    "upl": 1.0, "markPx": 101.0,
                    "mark_notional_usdt": 202.0, "position_imr": 20.2,
                    "position_age_hours": 1.0,
                    "sl": {"verified": True, "trigger_px": 95.0},
                }],
            }
            cycle_raw = {
                "action_taken": "OPEN_SHORT",
                "live_facts": facts,
                "trades": [],
            }
            trade_raw = json.dumps({"sl_trigger_px": 92.0})
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (self.CYCLE, "2026-08-06 12:02:00", "live", "traded",
                     1, 1000.0, "", json.dumps(cycle_raw)),
                )
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                    "fill_px,lev,margin,notional,pnl,raw) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.CYCLE, "2026-08-06 12:02:00", "BTC-USDT-SWAP",
                     "open", "short", 3.0, 90.0, 10.0, 27.0, 270.0,
                     0.0, trade_raw),
                )
                con.commit()
            finally:
                con.close()

            payload = build_push_payload.build(
                root, self.CYCLE,
                now=datetime(2026, 8, 6, 12, 3, tzinfo=cst))

        positions = {row["symbol"]: row for row in payload["positions"]}
        self.assertEqual(set(positions), {"ETH-USDT-SWAP", "BTC-USDT-SWAP"})
        self.assertEqual(positions["ETH-USDT-SWAP"]["sz"], 2.0)
        self.assertEqual(positions["BTC-USDT-SWAP"]["sz"], 3.0)
        self.assertEqual(positions["BTC-USDT-SWAP"]["sl_px"], 92.0)
        self.assertEqual(payload["assets"]["live"]["positions"], 2)
        self.assertEqual(payload["risk"]["position_count"], 2)

    def test_reconciled_close_uses_preserved_business_context(self):
        cst = timezone(timedelta(hours=8))
        card = {
            "direction_evidence": ["verified live facts"],
            "opposing_evidence": ["no new open"],
            "execution_conditions": {"hold": "keep exchange SL"},
            "invalidation_point": {"APR": "SL 0.518"},
            "risk_reward": {"exit_mode": "no_fixed_tp"},
            "portfolio_impact": {"after": "protected close"},
            "historical_experience": {
                "matched_wins": [], "matched_losses": [],
                "missed_opportunities": [], "usage": "none",
                "reason": "no new open",
            },
            "agent_judgement": "HOLD before protective fill",
            "reference_overrides": [],
        }
        facts = {
            "cycle_id": self.CYCLE,
            "status": "ok",
            "as_of": "2026-08-06 12:01:00",
            "balance": {
                "totalEq": 1000.0,
                "current_portfolio_imr_ratio": 0.42,
                "max_portfolio_imr_ratio": 0.666,
            },
            "positions": [{
                "instId": "APR-USDT-SWAP", "posSide": "long",
                "contracts": 31.0, "avgPx": 0.5442, "lever": 10.0,
                "upl": -6.0, "markPx": 0.5226,
                "mark_notional_usdt": 16.2, "position_imr": 1.62,
                "position_age_hours": 3.2,
                "sl": {"verified": True, "trigger_px": 0.518},
            }],
        }
        cycle_raw = {
            "reconcile_source": "exchange_fills_reconcile",
            "business_context_preserved": True,
            "business_context_source_cycle_id": self.CYCLE,
            "decision_protocol": "decision_card_v1",
            "decision_card": card,
            "live_facts": facts,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=False)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (self.CYCLE, "2026-08-06 12:02:00", "live", "traded",
                     1, 1000.0, "exchange fill reconcile",
                     json.dumps(cycle_raw)),
                )
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                    "fill_px,lev,margin,notional,pnl,reasoning,raw) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.CYCLE, "2026-08-06 12:02:00", "APR-USDT-SWAP",
                     "close", "long", 31.0, 0.5177, 10.0, None, None,
                     -8.22, "exchange protective fill",
                     json.dumps({"reconcile_source":
                                 "exchange_fills_reconcile"})),
                )
                con.commit()
            finally:
                con.close()

            payload = build_push_payload.build(
                root, self.CYCLE,
                now=datetime(2026, 8, 6, 12, 3, tzinfo=cst))

        self.assertEqual(payload["action_taken"], "CLOSE")
        self.assertEqual(
            payload["decision"]["origin"],
            "exchange_reconcile_after_business_terminal",
        )
        self.assertEqual(payload["decision"]["decision_card"], card)
        self.assertEqual(
            payload["risk"]["current_portfolio_imr_ratio"], 0.42)
        self.assertEqual(payload["positions"], [])

    def test_missing_live_cycle_is_flagged_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_push_payload.build(
                self._db_root(tmp, live_row=False), self.CYCLE)
        self.assertIn(
            {"name": "live_trader", "status": "pending",
             "detail": "本轮 trade_cycles 未落库——push 闸要求 live 落库，"
                       "出现即为异常"},
            payload["exceptions"])

    def test_terminal_failure_builds_explicit_wait_report_without_trade_row(self):
        failure = {
            "stage": "live",
            "cycle_id": self.CYCLE,
            "mode": "unified",
            "status": "failed",
            "failure_kind": "agent_idle_timeout",
            "child_returncode": 1,
            "returncode": 1,
            "started_at": "2026-08-06 12:01:00",
            "finished_at": "2026-08-06 12:10:00",
            "profile_lease_released": True,
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=False)
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.execute(
                    "CREATE TABLE execution_intents("
                    "profile TEXT,cycle_id TEXT,state TEXT,ord_id TEXT,"
                    "submitted_at TEXT,completed_at TEXT)"
                )
                con.commit()
            finally:
                con.close()
            payload = build_push_payload.build(
                root,
                self.CYCLE,
                upstream_failure=failure,
            )
        self.assertEqual(payload["action_taken"], "WAIT")
        self.assertEqual(payload["report_mode"], "upstream_failure")
        self.assertEqual(
            payload["decision"]["origin"], "system_failure_fallback")
        self.assertIn(
            "Agent 未形成当轮判断",
            payload["decision"]["decision_card"]["agent_judgement"],
        )
        self.assertEqual(payload["risk"]["status"], "BLOCKED_UPSTREAM_FAILURE")
        self.assertEqual(payload["execution"]["db_rows_live"], 0)
        self.assertEqual(
            payload["execution_intent_safety"]["status"], "PASSED")
        self.assertEqual(payload["orders_placed"], 0)
        self.assertTrue(any(
            item.get("name") == "live_trader"
            and item.get("status") == "failed"
            for item in payload["exceptions"]
        ))

    def test_collection_failure_builds_wait_without_fake_agent_judgement(self):
        failure = {
            "stage": "collection",
            "cycle_id": self.CYCLE,
            "mode": "quarter",
            "status": "failed",
            "failure_kind": "collection_gate_failed",
            "child_returncode": 1,
            "returncode": 1,
            "started_at": "2026-08-06 12:00:01",
            "finished_at": "2026-08-06 12:02:01",
            "profile_lease_released": True,
            "same_cycle_live_dispatched": False,
            "missing_required_sources": ["fast"],
            "failed_steps": ["fast"],
            "collection_receipt_sha256": "a" * 64,
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=False)
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.execute(
                    "CREATE TABLE execution_intents("
                    "profile TEXT,cycle_id TEXT,state TEXT,ord_id TEXT,"
                    "submitted_at TEXT,completed_at TEXT)"
                )
                con.commit()
            finally:
                con.close()
            payload = build_push_payload.build(
                root, self.CYCLE, upstream_failure=failure)

            with (
                mock.patch.object(
                    render_push_report, "authoritative_cycle_count",
                    return_value=None),
                mock.patch.object(
                    render_push_report, "authoritative_cycle_duration",
                    return_value=None),
                mock.patch.object(
                    render_push_report, "authoritative_equity",
                    return_value=None),
                mock.patch.object(
                    render_push_report, "authoritative_cum_pnl",
                    return_value=None),
                mock.patch.object(
                    render_push_report, "authoritative_position_count",
                    return_value=None),
            ):
                content = render_push_report.render(payload)["content"]
            validation = validate_push_format.validate(
                content, cycle_id=self.CYCLE)

        self.assertEqual(payload["action_taken"], "WAIT")
        self.assertIn("采集闸失败", payload["summary"])
        self.assertIn(
            "Agent 未启动",
            payload["decision"]["decision_card"]["agent_judgement"],
        )
        self.assertFalse(any(
            item.get("name") == "live_trader"
            for item in payload["exceptions"]
        ))
        self.assertTrue(any(
            item.get("name") == "collection_gate"
            and item.get("status") == "failed"
            for item in payload["exceptions"]
        ))
        self.assertTrue(validation["ok"], validation)
        self.assertIn("采集闸失败", content)
        normal_missing_market = json.loads(json.dumps(payload))
        normal_missing_market.pop("report_mode", None)
        normal_missing_market.pop("upstream_failure", None)
        with self.assertRaises(SystemExit):
            render_push_report.validate_input(normal_missing_market)

    def test_failure_card_distinguishes_analysis_from_missing_trade_terminal(self):
        failure = {
            "failure_kind": "business_output_missing",
            "business_check": {"checks": [
                {"db": "analysis.db", "table": "analysis_runs",
                 "found": True},
                {"db": "live_trades.db", "table": "trade_cycles",
                 "found": False},
            ]},
        }

        card = build_push_payload._upstream_failure_card(failure)

        self.assertIn("analysis.db 已形成分析证据", card["direction_evidence"][0])
        self.assertIn("交易终态缺失", card["opposing_evidence"][0])
        self.assertIn("已形成分析，但未形成交易终态", card["agent_judgement"])
        self.assertNotIn("未形成当轮判断", card["agent_judgement"])

    def test_failure_report_refuses_existing_business_terminal(self):
        failure = {
            "stage": "live", "cycle_id": self.CYCLE,
            "status": "failed", "returncode": 1,
            "profile_lease_released": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "trade terminal exists"):
                build_push_payload.build(
                    self._db_root(tmp, live_row=True),
                    self.CYCLE,
                    upstream_failure=failure,
                )

    def test_failure_report_refuses_partial_business_terminal(self):
        failure = {
            "stage": "live", "cycle_id": self.CYCLE,
            "status": "failed", "returncode": 1,
            "profile_lease_released": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=True)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "UPDATE trade_cycles SET decision='unknown',n_orders=0 "
                    "WHERE cycle_id=?", (self.CYCLE,))
                con.commit()
            finally:
                con.close()
            with self.assertRaisesRegex(ValueError, "trade terminal exists"):
                build_push_payload.build(
                    root, self.CYCLE, upstream_failure=failure)

    def test_failure_report_blocks_submitted_execution_intent(self):
        failure = {
            "stage": "live", "cycle_id": self.CYCLE,
            "status": "failed", "returncode": 1,
            "profile_lease_released": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=False)
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.execute(
                    "CREATE TABLE execution_intents("
                    "profile TEXT,cycle_id TEXT,state TEXT,ord_id TEXT,"
                    "submitted_at TEXT,completed_at TEXT)"
                )
                con.execute(
                    "INSERT INTO execution_intents VALUES(?,?,?,?,?,?)",
                    ("live", self.CYCLE, "submitted", "ord-1",
                     "2026-08-06 12:05:00", None),
                )
                con.commit()
            finally:
                con.close()
            with self.assertRaisesRegex(
                    ValueError, "non-clean execution intents"):
                build_push_payload.build(
                    root, self.CYCLE, upstream_failure=failure)

    def test_failure_report_blocks_completed_timestamp_even_if_state_is_clean(self):
        failure = {
            "stage": "live", "cycle_id": self.CYCLE,
            "status": "failed", "returncode": 1,
            "profile_lease_released": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=False)
            con = sqlite3.connect(root / "ledger.db")
            try:
                con.execute(
                    "CREATE TABLE execution_intents("
                    "profile TEXT,cycle_id TEXT,state TEXT,ord_id TEXT,"
                    "submitted_at TEXT,completed_at TEXT)"
                )
                con.execute(
                    "INSERT INTO execution_intents VALUES(?,?,?,?,?,?)",
                    ("live", self.CYCLE, "failed_clean", None, None,
                     "2026-08-06 12:05:00"),
                )
                con.commit()
            finally:
                con.close()
            with self.assertRaisesRegex(
                    ValueError, "non-clean execution intents"):
                build_push_payload.build(
                    root, self.CYCLE, upstream_failure=failure)

    def test_present_row_with_unknown_decision_stays_unknown(self):
        """行在但动作不可解释仍须被拦——这条不因 demo 下线而放开。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._db_root(tmp, live_row=True)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute(
                    "UPDATE trade_cycles SET decision='weird' WHERE cycle_id=?",
                    (self.CYCLE,))
                con.commit()
            finally:
                con.close()
            payload = build_push_payload.build(root, self.CYCLE)
        self.assertEqual(payload["action_taken"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
