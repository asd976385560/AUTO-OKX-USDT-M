from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import daily_maintenance  # noqa: E402


class P2GovernanceContractTests(unittest.TestCase):
    def test_daily_log_rotation_includes_stage_status_for_seven_days(self):
        step = next(item for item in daily_maintenance.STEPS if item[0] == "log_rotate")
        argv = step[1]
        self.assertIn("--apply", argv)
        self.assertEqual(argv[argv.index("--days") + 1], "7")
        self.assertEqual(
            argv[argv.index("--dirs") + 1],
            "trigger,push,stage-status",
        )

    def test_daily_maintenance_evaluates_shadow_judgments_after_quality_handoff(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("universe_judgment_evaluation", names)
        self.assertGreater(
            names.index("universe_judgment_evaluation"),
            names.index("quality_metrics"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "universe_judgment_evaluation"
        )
        self.assertIn("evaluate_universe_judgments.py", step[1][0])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_evaluates_frozen_model_forward_shadow(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("frozen_model_shadow_evaluation", names)
        self.assertGreater(
            names.index("frozen_model_shadow_evaluation"),
            names.index("universe_judgment_evaluation"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "frozen_model_shadow_evaluation"
        )
        self.assertIn("evaluate_multitimeframe_model_shadow.py", step[1][0])
        self.assertIn("--shadow-root", step[1])
        self.assertIn("--labels-out", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_independently_audits_frozen_model_labels(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("frozen_model_shadow_label_quality", names)
        self.assertGreater(
            names.index("frozen_model_shadow_label_quality"),
            names.index("frozen_model_shadow_evaluation"),
        )
        self.assertLess(
            names.index("frozen_model_shadow_label_quality"),
            names.index("analysis_signal_forward_quality"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "frozen_model_shadow_label_quality"
        )
        self.assertIn("audit_model_shadow_label_quality.py", step[1][0])
        self.assertIn("--evaluation", step[1])
        self.assertIn("--labels", step[1])
        self.assertIn("--market-db", step[1])
        self.assertNotIn("--apply", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_audits_report_completeness_read_only(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("daily_report_completeness", names)
        self.assertGreater(
            names.index("daily_report_completeness"),
            names.index("analysis_signal_forward_quality"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "daily_report_completeness"
        )
        self.assertIn("audit_report_completeness.py", step[1][0])
        self.assertEqual(
            step[1][step[1].index("--start") + 1], "2026-07-28")
        self.assertNotIn("--apply", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_audits_push_slots_and_delivery_read_only(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("push_completeness", names)
        self.assertGreater(
            names.index("push_completeness"),
            names.index("daily_report_completeness"),
        )
        self.assertLess(
            names.index("push_completeness"),
            names.index("fast_source_health"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "push_completeness"
        )
        self.assertIn("audit_push_completeness.py", step[1][0])
        self.assertEqual("14", step[1][step[1].index("--days") + 1])
        self.assertIn("--pipeline-log", step[1])
        self.assertIn("--event-log", step[1])
        self.assertIn("--dedupe-db", step[1])
        self.assertIn("--reports-dir", step[1])
        self.assertNotIn("--apply", step[1])
        self.assertNotIn("--send", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_audits_production_analysis_signal_outcomes(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("analysis_signal_forward_quality", names)
        self.assertGreater(
            names.index("analysis_signal_forward_quality"),
            names.index("frozen_model_shadow_label_quality"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "analysis_signal_forward_quality"
        )
        self.assertIn("audit_analysis_signal_forward_quality.py", step[1][0])
        self.assertIn("--analysis-db", step[1])
        self.assertIn("--market-db", step[1])
        self.assertIn("--labels-out", step[1])
        self.assertNotIn("--apply", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_audits_missing_fast_slots_read_only(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("fast_source_health", names)
        self.assertGreater(
            names.index("fast_source_health"),
            names.index("push_completeness"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "fast_source_health"
        )
        self.assertIn("audit_source_health.py", step[1][0])
        self.assertEqual(
            "2026-08-12T16:00:00+08:00",
            step[1][step[1].index("--forward-start") + 1],
        )
        self.assertNotIn("--apply", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_audits_full_universe_positioning_read_only(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("positioning_coverage", names)
        self.assertGreater(
            names.index("positioning_coverage"),
            names.index("fast_source_health"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "positioning_coverage"
        )
        self.assertIn("audit_positioning_coverage.py", step[1][0])
        self.assertIn("--market-db", step[1])
        self.assertNotIn("--apply", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_audits_news_sources_by_expected_slots(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("news_source_health", names)
        self.assertGreater(
            names.index("news_source_health"),
            names.index("fast_source_health"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "news_source_health"
        )
        self.assertIn("audit_news_source_health.py", step[1][0])
        self.assertEqual(
            "2026-08-12T16:15:00+08:00",
            step[1][step[1].index("--forward-start") + 1],
        )
        self.assertIn("--minimum-window-hours", step[1])
        self.assertNotIn("--apply", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_audits_contract_statistics_read_only(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("contract_statistics_coverage", names)
        self.assertGreater(
            names.index("contract_statistics_coverage"),
            names.index("asset_class_coverage"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "contract_statistics_coverage"
        )
        self.assertIn("audit_contract_statistics_coverage.py", step[1][0])
        self.assertEqual(
            "2026-08-12T16:00:00+08:00",
            step[1][step[1].index("--forward-start") + 1],
        )
        self.assertEqual(
            "96",
            step[1][step[1].index("--forward-minimum-slots") + 1],
        )
        self.assertNotIn("--apply", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_audits_asset_classes_read_only(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("asset_class_coverage", names)
        self.assertGreater(
            names.index("asset_class_coverage"),
            names.index("positioning_coverage"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "asset_class_coverage"
        )
        self.assertIn("audit_asset_class_coverage.py", step[1][0])
        self.assertIn("--market-db", step[1])
        self.assertIn("--minimum-rate", step[1])
        self.assertNotIn("--apply", step[1])
        self.assertEqual(step[3], (0,))

    def test_daily_maintenance_audits_closed_multitimeframe_coverage(self):
        names = [item[0] for item in daily_maintenance.STEPS]
        self.assertIn("multitimeframe_coverage", names)
        self.assertGreater(
            names.index("multitimeframe_coverage"),
            names.index("contract_statistics_coverage"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "multitimeframe_coverage"
        )
        self.assertIn("audit_multitimeframe_coverage.py", step[1][0])
        self.assertIn("--market-db", step[1])
        self.assertIn("--minimum-rate", step[1])
        self.assertNotIn("--apply", step[1])
        self.assertEqual(step[3], (0,))

    def test_news_scout_uses_direct_json_file_write_contract(self):
        text = (ROOT / "agents" / "news_scout.md").read_text(encoding="utf-8")
        self.assertIn("write path=<PROJECT_ROOT>/tmp/_xsearch_<cycle>.json", text)
        self.assertIn("文件写入工具直接写 `tmp/*.json`", text)
        self.assertNotIn("用 `tmp\\*.py` 经 wrapper 写", text)
        self.assertNotRegex(text, re.compile(r"(?mi)^\s*pwsh\b.*\s-Command\b"))
        self.assertNotRegex(text, re.compile(r"(?mi)^\s*Set-Content\b"))


if __name__ == "__main__":
    unittest.main()
