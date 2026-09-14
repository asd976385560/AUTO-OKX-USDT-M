"""Demo-removal regressions for remaining maintenance/data entrypoints."""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "collectors"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collect_data  # noqa: E402


class DemoEntrypointRetirementTests(unittest.TestCase):
    def test_collect_data_demo_flag_fails_before_collection(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
                sys, "argv", ["collect_data.py", "--demo"]), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                collect_data.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--demo 已于 2026-08-06 下线", stderr.getvalue())

    pass

    pass


if __name__ == "__main__":
    unittest.main()
