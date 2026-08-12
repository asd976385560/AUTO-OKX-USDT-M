from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_script_lifecycle.py"
MANIFEST = ROOT / "scripts" / "lifecycle.json"


def load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_script_lifecycle_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


checker = load_checker()


class ScriptLifecycleContractTests(unittest.TestCase):
    def test_current_manifest_covers_every_top_level_script_once(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        errors = checker.validate_manifest(
            manifest, checker.tracked_scripts(ROOT / "scripts"))
        self.assertEqual(errors, [])

    def test_missing_script_and_retired_top_level_are_rejected(self):
        manifest = {
            "version": 1,
            "groups": [{
                "name": "retired",
                "status": "retired",
                "invocation": "none",
                "write_scope": "none",
                "default_mode": "none",
                "replacement": "new.py",
                "last_verified": "2026-07-29",
                "paths": ["old.py"],
            }],
        }
        errors = checker.validate_manifest(
            manifest, {"old.py", "untracked.py"})
        self.assertTrue(any("不得继续留在" in item for item in errors))
        self.assertTrue(any("未登记脚本" in item for item in errors))

    def test_retired_cross_market_powershell_query_stays_absent(self):
        stale_query = ROOT / "scripts" / "query_cross_market.ps1"
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        registered = {
            path
            for group in manifest["groups"]
            for path in group["paths"]
        }
        self.assertFalse(stale_query.exists())
        self.assertNotIn(stale_query.name, registered)

if __name__ == "__main__":
    unittest.main()
