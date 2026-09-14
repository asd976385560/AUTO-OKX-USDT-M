# -*- coding: utf-8 -*-
"""Filesystem-only accounting and failure contract for log_rotate."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import log_rotate  # noqa: E402


def _old_log(log_root: Path, *, content: bytes = b"old-log") -> Path:
    target = log_root / "trigger" / "old.log"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    old_ts = time.time() - 10 * 86400
    os.utime(target, (old_ts, old_ts))
    return target


class LogRotateAccountingTests(unittest.TestCase):
    def test_apply_counts_deleted_bytes_only_after_successful_unlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_root = Path(tmp)
            target = _old_log(log_root, content=b"x" * 2_000_000)
            with mock.patch.object(log_rotate, "LOG_ROOT", log_root):
                result = log_rotate.rotate(["trigger"], 7, True)

            self.assertFalse(target.exists())
            self.assertEqual(1, result["candidates"])
            self.assertEqual(1, result["deleted"])
            self.assertEqual(0, result["delete_failed"])
            self.assertEqual(2.0, result["candidate_mb"])
            self.assertEqual(2.0, result["freed_mb"])

    def test_failed_unlink_is_structured_and_not_counted_as_deleted_or_freed(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_root = Path(tmp)
            target = _old_log(log_root, content=b"x" * 2_000_000)
            with mock.patch.object(log_rotate, "LOG_ROOT", log_root), mock.patch.object(
                Path, "unlink", side_effect=PermissionError("locked")
            ), redirect_stderr(StringIO()):
                result = log_rotate.rotate(["trigger"], 7, True)

            self.assertTrue(target.exists())
            self.assertEqual(1, result["candidates"])
            self.assertEqual(0, result["deleted"])
            self.assertEqual(0.0, result["freed_mb"])
            self.assertEqual(1, result["delete_failed"])
            self.assertEqual("PermissionError", result["failures"][0]["error_type"])
            self.assertEqual(str(target), result["failures"][0]["path"])

    def test_main_returns_nonzero_when_any_apply_unlink_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_root = Path(tmp)
            _old_log(log_root)
            stdout = StringIO()
            with mock.patch.object(log_rotate, "LOG_ROOT", log_root), mock.patch.object(
                Path, "unlink", side_effect=PermissionError("locked")
            ), mock.patch.object(
                sys, "argv", ["log_rotate.py", "--apply", "--dirs", "trigger"]
            ), redirect_stdout(stdout), redirect_stderr(StringIO()):
                rc = log_rotate.main()

            payload = json.loads(stdout.getvalue())
            self.assertEqual(2, rc)
            self.assertEqual(1, payload["delete_failed"])
            self.assertEqual(0, payload["deleted"])

    def test_dry_run_reports_candidates_without_claiming_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_root = Path(tmp)
            target = _old_log(log_root, content=b"x" * 2_000_000)
            with mock.patch.object(log_rotate, "LOG_ROOT", log_root):
                result = log_rotate.rotate(["trigger"], 7, False)

            self.assertTrue(target.exists())
            self.assertEqual(1, result["candidates"])
            self.assertEqual(2.0, result["candidate_mb"])
            self.assertEqual(0, result["deleted"])
            self.assertEqual(0.0, result["freed_mb"])


if __name__ == "__main__":
    unittest.main()
