import sys
import hashlib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_report_completeness  # noqa: E402


class ReportCompletenessAuditTests(unittest.TestCase):
    def test_missing_day_is_part_of_denominator(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reports = root / "reports"
            reports.mkdir()
            for name in ("account.db", "live_trades.db", "ledger.db"):
                (root / name).touch()
            (reports / "daily-2026-08-01.md").write_text(
                "fixture", encoding="utf-8")
            (reports / "daily-2026-08-03.md").write_text(
                "fixture", encoding="utf-8")

            def validator(**kwargs):
                valid = kwargs["report_path"].name.endswith("08-01.md")
                return {
                    "ok": valid,
                    "report_ts": "2026-08-01 08:05:00",
                    "errors": [] if valid else ["audit: drift"],
                    "checks": ["structure"],
                }

            result = audit_report_completeness.audit_daily_reports(
                start=date(2026, 8, 1),
                end=date(2026, 8, 3),
                reports_dir=reports,
                account_db=root / "account.db",
                live_trades_db=root / "live_trades.db",
                ledger_db=root / "ledger.db",
                validator=validator,
                evaluated_at="2026-08-12 01:00:00",
            )

        self.assertEqual(result["expected"], 3)
        self.assertEqual(result["existing"], 2)
        self.assertEqual(result["valid"], 1)
        self.assertAlmostEqual(result["completeness_rate"], 1 / 3)
        self.assertEqual(result["status"], "NOT_MET")
        self.assertIn("missing", result["rows"][1]["errors"][0])
        self.assertFalse(result["auto_send"])
        self.assertFalse(result["database_write"])

    def test_target_passes_only_at_or_above_99_percent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reports = root / "reports"
            reports.mkdir()
            (reports / "daily-2026-08-01.md").touch()
            result = audit_report_completeness.audit_daily_reports(
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
                reports_dir=reports,
                account_db=root / "account.db",
                live_trades_db=root / "live_trades.db",
                ledger_db=root / "ledger.db",
                validator=lambda **_: {"ok": True},
            )
        self.assertEqual(result["completeness_rate"], 1.0)
        self.assertEqual(result["status"], "PASSED")

    def test_reversed_window_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "end date"):
            audit_report_completeness.audit_daily_reports(
                start=date(2026, 8, 2),
                end=date(2026, 8, 1),
                reports_dir=Path("reports"),
                account_db=Path("account.db"),
                live_trades_db=Path("live.db"),
                ledger_db=Path("ledger.db"),
            )

    def test_forward_window_is_pre_registered_and_requires_30_days(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reports = root / "reports"
            reports.mkdir()
            required = {
                "reports_dir": reports,
                "account_db": root / "account.db",
                "live_trades_db": root / "live.db",
                "ledger_db": root / "ledger.db",
                "validator": lambda **_: {"ok": True},
            }
            empty = audit_report_completeness.audit_forward_daily_reports(
                start=date(2026, 8, 13),
                end=date(2026, 8, 12),
                minimum_days=30,
                **required,
            )
            for offset in range(30):
                day = date(2026, 8, 13) + timedelta(days=offset)
                (reports / f"daily-{day.isoformat()}.md").touch()
            complete = audit_report_completeness.audit_forward_daily_reports(
                start=date(2026, 8, 13),
                end=date(2026, 9, 11),
                minimum_days=30,
                **required,
            )
        self.assertEqual(empty["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(empty["expected"], 0)
        self.assertEqual(complete["status"], "PASSED")
        self.assertEqual(complete["completeness_rate"], 1.0)

    def test_one_invalid_day_fails_legacy_99_percent_forward_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reports = root / "reports"
            reports.mkdir()
            for offset in range(30):
                day = date(2026, 8, 13) + timedelta(days=offset)
                (reports / f"daily-{day.isoformat()}.md").touch()
            result = audit_report_completeness.audit_forward_daily_reports(
                start=date(2026, 8, 13),
                end=date(2026, 9, 11),
                minimum_days=30,
                reports_dir=reports,
                account_db=root / "account.db",
                live_trades_db=root / "live.db",
                ledger_db=root / "ledger.db",
                evaluated_at="2026-08-15T19:59:59+08:00",
                validator=lambda **kwargs: {
                    "ok": not kwargs["report_path"].name.endswith("08-20.md")
                },
            )
        self.assertEqual(result["valid"], 29)
        self.assertEqual(result["status"], "NOT_MET")

    def test_delivery_requires_matching_artifact_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reports = root / "reports"
            reports.mkdir()
            report = reports / "daily-2026-08-13.md"
            report.write_text("canonical", encoding="utf-8")
            identity = "reviewer:2026-08-13:daily"
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            common = {
                "start": date(2026, 8, 13),
                "end": date(2026, 8, 13),
                "reports_dir": reports,
                "account_db": root / "account.db",
                "live_trades_db": root / "live.db",
                "ledger_db": root / "ledger.db",
                "marked_identities": {identity},
                "validator": lambda **_: {"ok": True},
            }
            passed = audit_report_completeness.audit_daily_reports(
                delivery_hashes={identity: {digest}}, **common)
            failed = audit_report_completeness.audit_daily_reports(
                delivery_hashes={identity: {"0" * 64}}, **common)
        self.assertEqual(passed["delivery_status"], "PASSED")
        self.assertEqual(passed["audit_status"], "PASSED")
        self.assertEqual(failed["delivery_status"], "NOT_MET")
        self.assertEqual(failed["audit_status"], "NOT_MET")


if __name__ == "__main__":
    unittest.main()
