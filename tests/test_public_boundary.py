# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_public_boundary.py"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("check_public_boundary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner()


class PublicBoundaryTests(unittest.TestCase):
    def test_current_tracked_tree_is_clean(self):
        self.assertEqual(scanner.scan_repository(ROOT), [])

    def test_sensitive_values_are_reported_without_copying_the_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = "123456789"
            private_ip = ".".join(("10", "20", "30", "40"))
            (root / "sample.py").write_text(
                f'alert_target = "c2c:{value}"\nendpoint = "{private_ip}"\n',
                encoding="utf-8",
            )
            findings = scanner.scan_files(root, ["sample.py"])

        self.assertEqual(
            {finding["rule"] for finding in findings},
            {"numeric_push_target", "concrete_qq_route", "private_ipv4"},
        )
        self.assertNotIn(value, str(findings))

    def test_placeholders_are_allowed_but_runtime_artifacts_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.example.md").write_text(
                "group:PUBLIC_GROUP_OPENID <PRIVATE_IP> <PROJECT_ROOT>",
                encoding="utf-8",
            )
            findings = scanner.scan_files(
                root, ["config.example.md", "logs/runtime.log"]
            )

        self.assertEqual(findings, [{
            "rule": "forbidden_runtime_artifact",
            "path": "logs/runtime.log",
            "line": None,
        }])


if __name__ == "__main__":
    unittest.main()
