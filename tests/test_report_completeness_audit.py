import sys
import tempfile
import unittest
from datetime import date
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


if __name__ == "__main__":
    unittest.main()
