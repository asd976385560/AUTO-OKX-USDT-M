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
    def test_reference_override_repairs_are_not_published(self):
        archive = (
            SCRIPTS / "archive" / "migrations"
            / "20260731-reference-overrides"
        )
        for name in (
            "_fix_live_ref_override.py",
            "_fix_reference_overrides.py",
        ):
            self.assertFalse((SCRIPTS / name).exists())
            self.assertFalse((archive / name).is_file())

    def test_daily_log_rotation_includes_stage_status_for_seven_days(self):
        step = next(item for item in daily_maintenance.STEPS if item[0] == "log_rotate")
        argv = step[1]
        self.assertIn("--apply", argv)
        self.assertEqual(argv[argv.index("--days") + 1], "7")
        self.assertEqual(
            argv[argv.index("--dirs") + 1],
            "trigger,push,stage-status",
        )

    def test_news_scout_uses_direct_json_file_write_contract(self):
        text = (ROOT / "agents" / "news_scout.md").read_text(encoding="utf-8")
        self.assertIn("write path=<PROJECT_ROOT>/tmp/_xsearch_<cycle>.json", text)
        self.assertIn("文件写入工具直接写 `tmp/*.json`", text)
        self.assertNotIn("用 `tmp\\*.py` 经 wrapper 写", text)
        self.assertNotRegex(text, re.compile(r"(?mi)^\s*pwsh\b.*\s-Command\b"))
        self.assertNotRegex(text, re.compile(r"(?mi)^\s*Set-Content\b"))


if __name__ == "__main__":
    unittest.main()
