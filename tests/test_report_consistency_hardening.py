# -*- coding: utf-8 -*-
"""Isolated consistency hardening tests; production DBs are never opened."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import daily_maintenance  # noqa: E402
import daily_report_writer  # noqa: E402
import judgment_quality_report  # noqa: E402
import quality_metrics  # noqa: E402
import reviewer_preflight  # noqa: E402


DAILY_SCHEMA = """
CREATE TABLE daily_reports(
  ts TEXT NOT NULL,profile TEXT NOT NULL,open_count INTEGER,
  close_count INTEGER,total_pnl REAL,total_fees REAL,best_trade TEXT,
  worst_trade TEXT,summary TEXT,lessons TEXT,raw TEXT,trade_day_num INTEGER,
  PRIMARY KEY(ts,profile)
);
"""


class FailureMetricTests(unittest.TestCase):
    def test_failed_error_and_timeout_share_failure_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "ledger.db"
            con = sqlite3.connect(db)
            try:
                con.execute(
                    "CREATE TABLE collection_runs("
                    "source TEXT,status TEXT,ts TEXT)")
                con.executemany(
                    "INSERT INTO collection_runs VALUES(?,?,?)",
                    [
                        ("news", "ok", "2099-01-01 00:00:00"),
                        ("news", "degraded", "2099-01-01 00:01:00"),
                        ("news", "failed", "2099-01-01 00:02:00"),
                        ("news", "error", "2099-01-01 00:03:00"),
                        ("news", "timeout", "2099-01-01 00:04:00"),
                    ],
                )
                con.commit()
            finally:
                con.close()

            with mock.patch.object(quality_metrics, "DB_ROOT", root):
                result = quality_metrics.metric_source_health()["news"]
            self.assertEqual(result["failure_runs"], 3)
            self.assertEqual(result["failure_pct"], 60.0)
            self.assertEqual(result["error_pct"], 60.0)
            self.assertEqual(
                result["raw_status_counts"],
                {"degraded": 1, "error": 1, "failed": 1,
                 "ok": 1, "timeout": 1},
            )


class WeeklyQualityMetricTests(unittest.TestCase):
    def test_four_metrics_keep_denominators_explicit(self):
        complete = {
            field: {"evidence": "present"}
            for field in judgment_quality_report.REQUIRED_DECISION_CARD_FIELDS
        }
        partial = dict(complete)
        partial["risk_reward"] = ""
        rows = [
            {"decision_card": None},
            {"decision_card": "not-json"},
            {"decision_card": json.dumps(complete)},
            {"decision_card": json.dumps(partial)},
        ]
        result = judgment_quality_report.decision_card_quality(rows)
        self.assertEqual(result["total_signals"], 4)
        self.assertEqual(result["decision_card_rows"], 2)
        self.assertEqual(result["complete_card_rows"], 1)
        self.assertEqual(result["decision_card_coverage_pct"], 50.0)
        self.assertEqual(result["within_card_completeness_pct"], 50.0)
        self.assertEqual(result["overall_completeness_pct"], 25.0)


class DailyRevisionBackfillTests(unittest.TestCase):
    def test_backfill_is_dry_run_first_metadata_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "account.db"
            report = root / "daily-2026-07-28.md"
            con = sqlite3.connect(db)
            try:
                con.executescript(DAILY_SCHEMA)
                raw = json.dumps({
                    "unchanged_fact": {"value": 42},
                    "report_audit": {
                        "version": 1,
                        "period_kind": "daily",
                        "report_state": {"status": "final"},
                        "trade_metrics": {"live": {}, "demo": {}},
                    },
                }, ensure_ascii=False)
                for profile in ("live", "demo"):
                    con.execute(
                        "INSERT INTO daily_reports VALUES("
                        "?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("2026-07-28 08:05:00", profile, 1, 2, 3.0,
                         0.1, "best", "worst", "summary", "lessons",
                         raw, 65),
                    )
                con.commit()
            finally:
                con.close()
            original_report = (
                "# 📊 小灵日报 2026-07-28\n"
                "> ts: 2026-07-28 08:05:00\n"
                "> **报告状态：最终报告｜live 对账已清零**\n"
                "\n## unchanged\nfact body\n"
            )
            report.write_text(original_report, encoding="utf-8")

            before_db = hashlib.sha256(db.read_bytes()).hexdigest()
            before_con = sqlite3.connect(db)
            try:
                before_rows = before_con.execute(
                    "SELECT rowid,ts,profile,open_count,close_count,total_pnl,"
                    "total_fees,best_trade,worst_trade,summary,lessons,"
                    "trade_day_num FROM daily_reports ORDER BY profile"
                ).fetchall()
            finally:
                before_con.close()
            con = sqlite3.connect(db)
            try:
                plan = daily_report_writer.plan_daily_revision_backfill(
                    con, "2026-07-28 08:05:00", report)
            finally:
                con.close()
            self.assertEqual(
                hashlib.sha256(db.read_bytes()).hexdigest(), before_db)
            self.assertEqual(
                report.read_text(encoding="utf-8"), original_report)
            self.assertEqual(len(plan["row_updates"]), 2)
            self.assertTrue(plan["markdown_change"])
            self.assertFalse(plan["revision"]["auto_resend"])

            report.write_text(
                original_report + "concurrent edit\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "并发校验失败"):
                daily_report_writer.apply_daily_revision_backfill_markdown(
                    plan)
            report.write_text(original_report, encoding="utf-8")

            backup = daily_report_writer.create_daily_revision_backup(
                db,
                report,
                root / "backups",
                "2026-07-28 08:05:00",
            )
            backup_db = sqlite3.connect(backup["database"])
            try:
                self.assertEqual(
                    backup_db.execute("PRAGMA quick_check").fetchone()[0],
                    "ok",
                )
            finally:
                backup_db.close()
            self.assertEqual(
                Path(backup["markdown"]).read_text(encoding="utf-8"),
                original_report,
            )

            con = sqlite3.connect(db)
            try:
                daily_report_writer.apply_daily_revision_backfill_db(
                    con, plan)
                con.commit()
            finally:
                con.close()
            daily_report_writer.apply_daily_revision_backfill_markdown(plan)

            con = sqlite3.connect(db)
            try:
                after_rows = con.execute(
                    "SELECT rowid,ts,profile,open_count,close_count,total_pnl,"
                    "total_fees,best_trade,worst_trade,summary,lessons,"
                    "trade_day_num FROM daily_reports ORDER BY profile"
                ).fetchall()
                raw_rows = con.execute(
                    "SELECT raw FROM daily_reports ORDER BY profile"
                ).fetchall()
            finally:
                con.close()
            self.assertEqual(before_rows, after_rows)
            for (stored_raw,) in raw_rows:
                stored = json.loads(stored_raw)
                self.assertEqual(stored["unchanged_fact"], {"value": 42})
                revision = stored["report_audit"]["revision"]
                self.assertEqual(revision["kind"], "initial")
                self.assertFalse(revision["auto_resend"])
                self.assertTrue(revision["metadata_backfill_only"])
            content = report.read_text(encoding="utf-8")
            self.assertIn("report_revision: 1", content)
            self.assertEqual(
                content.replace(
                    "> report_revision: 1 | revision_kind: initial | "
                    "resend_review_required: false | auto_resend: false\n",
                    "",
                ),
                original_report,
            )

            con = sqlite3.connect(db)
            try:
                second = daily_report_writer.plan_daily_revision_backfill(
                    con, "2026-07-28 08:05:00", report)
            finally:
                con.close()
            self.assertEqual(second["row_updates"], [])
            self.assertFalse(second["markdown_change"])


class ReviewerReadyTests(unittest.TestCase):
    def test_manifest_proves_artifact_and_forces_provisional_on_reconcile_rc1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quality_path = root / "quality_metrics_2026-07-29.json"
            quality_payload = {
                "ts": "2026-07-29 07:56:00",
                "metrics": {"source_health": {}},
            }
            quality_path.write_text(
                json.dumps(quality_payload), encoding="utf-8")
            raw = quality_path.read_bytes()
            artifact = {
                "path": str(quality_path),
                "valid": True,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
            report = {
                "business_date": "2026-07-29",
                "run_id": "20260729T075500.000000+0800",
                "started_at": "2026-07-29 07:55:00",
                "critical_steps_completed_at": "2026-07-29 07:57:00",
                "completed_at": "2026-07-29 07:57:00",
                "steps": {
                    "reconcile": {"rc": 1, "accepted": True},
                    "account_bills": {"rc": 0, "accepted": True},
                    "missed_opportunities": {"rc": 0, "accepted": True},
                    "quality_metrics": {
                        "rc": 0,
                        "accepted": True,
                        "artifact": artifact,
                    },
                },
            }
            with mock.patch.object(
                    daily_maintenance, "REVIEWER_READY_DIR", root):
                written = daily_maintenance.write_reviewer_manifest(
                    report, "completed")
            manifest = json.loads(
                Path(written["path"]).read_text(encoding="utf-8"))
            result = reviewer_preflight.validate_manifest(
                manifest, "2026-07-29")
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(result["report_mode"], "provisional")
            self.assertTrue(result["provisional_required"])
            self.assertFalse(result["auto_send"])

            quality_path.write_text("{}", encoding="utf-8")
            tampered = reviewer_preflight.validate_manifest(
                manifest, "2026-07-29")
            self.assertFalse(tampered["ok"])
            self.assertIn(
                "quality_metrics artifact hash differs", tampered["errors"])

    def test_daily_maintenance_publishes_ready_only_after_critical_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            business_date = daily_maintenance.now_cst()[:10]
            quality_path = root / f"quality_metrics_{business_date}.json"
            quality_path.write_text(json.dumps({
                "ts": f"{business_date} 07:56:00",
                "metrics": {"source_health": {}},
            }), encoding="utf-8")
            steps = [
                ("reconcile", ["reconcile.py"], 1, (0, 1)),
                ("account_bills", ["account_bills.py"], 1, (0,)),
                ("missed_opportunities", ["missed.py"], 1, (0,)),
                ("quality_metrics", ["quality_metrics.py"], 1, (0,)),
                ("noncritical", ["noncritical.py"], 1, (0,)),
            ]
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ok\n", stderr="")
            noncritical_failed = subprocess.CompletedProcess(
                args=[], returncode=9, stdout="", stderr="late failure")
            observed_handoff = {}

            def fake_run(command, **_kwargs):
                if "noncritical.py" in command:
                    manifest_path = (
                        root / f"reviewer_ready_{business_date}.json")
                    observed_handoff.update(json.loads(
                        manifest_path.read_text(encoding="utf-8")))
                    return noncritical_failed
                return completed

            with (
                mock.patch.object(daily_maintenance, "STEPS", steps),
                mock.patch.object(
                    daily_maintenance, "QUALITY_REPORT_DIR", root),
                mock.patch.object(
                    daily_maintenance, "REVIEWER_READY_DIR", root),
                mock.patch.object(
                    daily_maintenance.subprocess, "run",
                    side_effect=fake_run),
            ):
                rc = daily_maintenance.main([])
            self.assertEqual(rc, 1)
            self.assertTrue(observed_handoff["ready"])
            self.assertEqual(observed_handoff["state"], "ready")
            self.assertIsNone(
                observed_handoff["maintenance_completed_at"])
            manifest_path = (
                root / f"reviewer_ready_{business_date}.json")
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["ready"])
            self.assertEqual(manifest["state"], "ready")
            self.assertEqual(manifest["report_mode"], "final_candidate")
            self.assertFalse(manifest["maintenance_ok"])
            self.assertEqual(
                manifest["maintenance_steps"]["noncritical"]["rc"], 9)
            result = reviewer_preflight.wait_for_manifest(
                manifest_path, business_date, 0, 0.05)
            self.assertTrue(result["ok"], result["errors"])


if __name__ == "__main__":
    unittest.main()
