from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_news_source_health as audit  # noqa: E402
import audit_source_health as scheduled  # noqa: E402


class NewsSourceHealthAuditTests(unittest.TestCase):
    def test_overall_status_uses_all_enabled_sources(self) -> None:
        self.assertEqual("PASSED", audit._overall_status("PASSED"))
        self.assertEqual(
            "PENDING_FORWARD_EVIDENCE",
            audit._overall_status("INSUFFICIENT_EVIDENCE"),
        )
        self.assertEqual("NOT_MET", audit._overall_status("NOT_MET"))

    def test_due_cycles_honor_hourly_schedule(self) -> None:
        start = datetime(2026, 8, 12, 0, 0, tzinfo=scheduled.CST)
        end = datetime(2026, 8, 12, 2, 0, tzinfo=scheduled.CST)

        cycles = audit._due_cycles(start, end, 60)

        self.assertEqual(cycles, ["2026-08-12T00:00", "2026-08-12T01:00"])

    def test_strict_completeness_rejects_degraded_and_missing(self) -> None:
        spec = audit.NewsSourceSpec(
            source="rss_en", interval_minutes=15, role="required",
            endpoint="test", historical_eligible=True)
        start = datetime(2026, 8, 12, 0, 0, tzinfo=scheduled.CST)
        end = datetime(2026, 8, 12, 1, 0, tzinfo=scheduled.CST)
        records = {
            "2026-08-12T00:00": {"status": "ok"},
            "2026-08-12T00:15": {"status": "degraded", "err": "partial"},
            "2026-08-12T00:30": {"status": "ok"},
        }

        result = audit._summarize_source(
            spec, start=start, end_exclusive=end, records=records,
            target_rate=0.99, minimum_slots=4)

        self.assertEqual(result["expected_slots"], 4)
        self.assertEqual(result["complete_slots"], 2)
        self.assertEqual(result["missing_slots"], 1)
        self.assertEqual(result["strict_complete_rate"], 0.5)
        self.assertEqual(result["available_rate"], 0.75)
        self.assertEqual(result["status"], "NOT_MET")

    def test_short_forward_window_is_insufficient_not_passed(self) -> None:
        spec = audit.NewsSourceSpec(
            source="rss:cryptoslate", interval_minutes=15,
            role="required_subsource", endpoint="test",
            historical_eligible=False)
        start = datetime(2026, 8, 12, 0, 0, tzinfo=scheduled.CST)
        end = datetime(2026, 8, 12, 0, 30, tzinfo=scheduled.CST)
        records = {
            "2026-08-12T00:00": {"status": "ok"},
            "2026-08-12T00:15": {"status": "ok"},
        }

        result = audit._summarize_source(
            spec, start=start, end_exclusive=end, records=records,
            target_rate=0.99, minimum_slots=96)

        self.assertEqual(result["strict_complete_rate"], 1.0)
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
