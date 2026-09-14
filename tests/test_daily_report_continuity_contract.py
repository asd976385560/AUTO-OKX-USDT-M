from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_daily_report as validator  # noqa: E402


class DailyReportContinuityContractTests(unittest.TestCase):
    def test_contiguous_windows_pass_the_strict_continuity_check(self):
        errors, warnings, checks = validator._daily_window_continuity(
            "2026-08-17 08:00:00", "2026-08-17 08:00:00",
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(checks, ["daily_window_continuity"])

    def test_whole_day_gap_is_preserved_without_poisoning_later_report(self):
        errors, warnings, checks = validator._daily_window_continuity(
            "2026-08-14 08:00:00", "2026-08-17 08:00:00",
        )
        self.assertEqual(errors, [])
        self.assertIn("3 days", warnings[0])
        self.assertEqual(checks, ["daily_window_gap_preserved"])

    def test_overlap_remains_a_hard_error(self):
        errors, warnings, checks = validator._daily_window_continuity(
            "2026-08-17 09:00:00", "2026-08-17 08:00:00",
        )
        self.assertEqual(errors, ["window: overlap with previous daily report"])
        self.assertEqual(warnings, [])
        self.assertEqual(checks, [])

    def test_sub_day_gap_remains_a_hard_error(self):
        errors, warnings, checks = validator._daily_window_continuity(
            "2026-08-16 20:00:00", "2026-08-17 08:00:00",
        )
        self.assertEqual(
            errors, ["window: gap is not aligned to whole report days"],
        )
        self.assertEqual(warnings, [])
        self.assertEqual(checks, [])


if __name__ == "__main__":
    unittest.main()
