# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import render_push_report  # noqa: E402
import validate_push_format  # noqa: E402


class PushRenderValidateFusionTests(unittest.TestCase):
    CONTENT = "第1轮\n📊 资产\n" + ("完整战报正文" * 80)

    def _run(self, validation: dict) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "content.txt"
            argv = [
                "render_push_report.py",
                "--json", "{}",
                "--out-file", str(output),
                "--validate-cycle-id", "2026-08-30T01:15",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    render_push_report, "load_payload", return_value={}),
                mock.patch.object(
                    render_push_report,
                    "render",
                    return_value={
                        "ok": True,
                        "title": "fixture",
                        "content": self.CONTENT,
                    },
                ),
                mock.patch.object(
                    validate_push_format, "validate", return_value=validation),
                mock.patch.object(
                    validate_push_format, "write_repair_queue") as repair,
                mock.patch.object(
                    validate_push_format, "close_healed_push_format") as close,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                rc = render_push_report.main()
            receipt = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual(self.CONTENT, output.read_text(encoding="utf-8"))
            if validation["ok"]:
                close.assert_called_once_with()
                repair.assert_not_called()
            else:
                repair.assert_called_once()
                close.assert_not_called()
            return rc, receipt

    def test_fused_success_preserves_canonical_validation_receipt(self):
        validation = {
            "ok": True,
            "errors": [],
            "warnings": [],
            "missing_fields": [],
            "char_count": len(self.CONTENT),
        }
        rc, receipt = self._run(validation)
        self.assertEqual(0, rc)
        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["render_ok"])
        self.assertTrue(receipt["validation_fused"])
        self.assertEqual(validation, receipt["validation"])

    def test_fused_validation_failure_writes_repair_and_returns_one(self):
        validation = {
            "ok": False,
            "errors": ["missing execution section"],
            "warnings": [],
            "missing_fields": ["execution"],
            "char_count": len(self.CONTENT),
        }
        rc, receipt = self._run(validation)
        self.assertEqual(1, rc)
        self.assertFalse(receipt["ok"])
        self.assertTrue(receipt["render_ok"])
        self.assertEqual(validation, receipt["validation"])


if __name__ == "__main__":
    unittest.main()
