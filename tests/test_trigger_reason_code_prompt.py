# -*- coding: utf-8 -*-
"""Regression coverage for the candidate reason-code prompt contract."""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import sys
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = ROOT / "collectors"
SCRIPTS = ROOT / "scripts"
for path in (COLLECTORS, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import trigger_agent  # noqa: E402


class TriggerReasonCodePromptTests(unittest.TestCase):
    def test_shadow_and_consume_prompts_publish_exact_lexical_contract(self):
        base = {
            "status": "PASSED",
            "candidate_count": 16,
            "screened_count": 16,
            "ready_count": 6,
            "briefing_sha256": "a" * 64,
            "bundle_sha256": "b" * 64,
            "bundle_path": (
                _public_project_path('logs', 'candidate-evidence', 'candidate-bundle.json')),
        }
        now = datetime(2026, 8, 31, 15, 1, tzinfo=trigger_agent.CST)
        expected = "完整匹配[a-z0-9][a-z0-9_]{0,63}"
        forbidden = "禁止点号、百分号和连字符"
        numeric = "数值1.27必须写成1_27"

        for phase in ("shadow", "consume"):
            with self.subTest(phase=phase):
                message = trigger_agent._unified_live_message(
                    "2026-08-31T15:00",
                    "briefing marker",
                    now=now,
                    candidate_bundle={**base, "phase": phase},
                )
                self.assertIn(expected, message)
                self.assertIn(forbidden, message)
                self.assertIn(numeric, message)


if __name__ == "__main__":
    unittest.main()
