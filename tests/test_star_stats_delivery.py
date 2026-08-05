from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "update-star-stats.yml"
DATA_BRANCH = "star-statistics"
RAW_CHART = (
    "https://raw.githubusercontent.com/asd976385560/AUTO-OKX-USDT-M/"
    f"{DATA_BRANCH}/docs/assets/star-history.svg"
)
DATA_URL = (
    "https://github.com/asd976385560/AUTO-OKX-USDT-M/blob/"
    f"{DATA_BRANCH}/docs/data/star-history.json"
)


class StarStatisticsDeliveryTests(unittest.TestCase):
    def test_workflow_writes_only_the_dedicated_data_branch(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn(f"STAR_STATS_BRANCH: {DATA_BRANCH}", text)
        self.assertIn("ref: main", text)
        self.assertIn("path: source", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("ref: ${{ env.STAR_STATS_BRANCH }}", text)
        self.assertIn("path: stats", text)
        self.assertIn("python source/scripts/update_star_stats.py", text)
        self.assertIn(
            "--json-output stats/docs/data/star-history.json", text
        )
        self.assertIn(
            "--svg-output stats/docs/assets/star-history.svg", text
        )
        self.assertIn('git push origin "HEAD:${STAR_STATS_BRANCH}"', text)
        self.assertNotRegex(text, re.compile(r"(?m)^\s*git push\s*$"))
        self.assertNotIn("git push origin main", text)

    def test_public_docs_read_generated_artifacts_from_the_data_branch(self):
        for relative in ("README.md", "README.en.md", "docs/README.md"):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(RAW_CHART, text)
                self.assertIn(DATA_URL, text)


if __name__ == "__main__":
    unittest.main()
