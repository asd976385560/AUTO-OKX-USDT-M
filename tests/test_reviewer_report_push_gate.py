from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import qq_push  # noqa: E402
import validate_periodic_report  # noqa: E402


class ReviewerReportPushGateTests(unittest.TestCase):
    def test_non_reviewer_identity_is_unchanged(self):
        with mock.patch.object(sys, "argv", ["qq_push.py"]):
            self.assertIsNone(
                qq_push._validate_reviewer_report_before_push(
                    "ordinary", "push:2026-08-13T00:45"))

    def test_reviewer_report_requires_canonical_file(self):
        with mock.patch.object(sys, "argv", ["qq_push.py"]):
            with self.assertRaisesRegex(ValueError, "canonical report file"):
                qq_push._validate_reviewer_report_before_push(
                    '{"month_start_ts":"wrong"}',
                    "reviewer:2026-09-01:monthly",
                )

    def test_malformed_reviewer_identity_cannot_bypass_gate(self):
        with mock.patch.object(sys, "argv", ["qq_push.py"]):
            with self.assertRaisesRegex(ValueError, "reviewer dedupe identity"):
                qq_push._validate_reviewer_report_before_push(
                    "raw output", "reviewer:2026-09-01:summary")

    def test_validated_canonical_monthly_file_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "reports" / "monthly" / "monthly-2026-09-01.md"
            report.parent.mkdir(parents=True)
            report.write_text("# 小灵月报 2026-09-01\n", encoding="utf-8")
            argv = [
                "qq_push.py", "--content-file", str(report),
                "--dedupe-key", "reviewer:2026-09-01:monthly",
            ]
            with (
                mock.patch.object(qq_push, "ROOT", root),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    validate_periodic_report,
                    "validate_report",
                    return_value={
                        "ok": True,
                        "report_key": "2026-09-01 00:00:00",
                        "checks": ["authoritative_facts"],
                    },
                ),
            ):
                result = qq_push._validate_reviewer_report_before_push(
                    report.read_text(encoding="utf-8"),
                    "reviewer:2026-09-01:monthly",
                )
        self.assertEqual(result["kind"], "monthly")
        self.assertEqual(result["report_day"], "2026-09-01")

    def test_identity_date_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "reports" / "weekly" / "weekly-2026-08-17.md"
            report.parent.mkdir(parents=True)
            report.write_text("# 小灵周报 2026-08-17\n", encoding="utf-8")
            argv = ["qq_push.py", "--file", str(report)]
            with (
                mock.patch.object(qq_push, "ROOT", root),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    validate_periodic_report,
                    "validate_report",
                    return_value={
                        "ok": True,
                        "report_key": "2026-08-10 00:00:00",
                    },
                ),
            ):
                with self.assertRaisesRegex(ValueError, "differs"):
                    qq_push._validate_reviewer_report_before_push(
                        report.read_text(encoding="utf-8"),
                        "reviewer:2026-08-17:weekly",
                    )


if __name__ == "__main__":
    unittest.main()
