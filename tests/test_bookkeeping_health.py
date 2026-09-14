# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bookkeeping_health  # noqa: E402


class PipelineFreshnessTests(unittest.TestCase):
    def _root(self, analysis_ts: str) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        with closing(sqlite3.connect(root / "market.db")) as con:
            con.execute("CREATE TABLE tick_snapshots(ts TEXT)")
            con.execute("INSERT INTO tick_snapshots(ts) VALUES(?)",
                        ("2026-08-31T00:00:00Z",))
            con.commit()
        with closing(sqlite3.connect(root / "analysis.db")) as con:
            con.execute(
                "CREATE TABLE analysis_runs("
                "cycle_id TEXT, ts TEXT, status TEXT)")
            con.execute(
                "INSERT INTO analysis_runs(cycle_id,ts,status) VALUES(?,?,?)",
                ("2026-08-31T08:00", analysis_ts, "ok"))
            con.commit()
        return tmp, root

    def _run(self, root: Path) -> int:
        argv = ["bookkeeping_health.py", "--db-root", str(root),
                "--threshold-min", "30"]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                bookkeeping_health.main()
        return int(raised.exception.code)

    def test_healthy_collection_to_analysis_gap_passes_without_account_db(self):
        tmp, root = self._root("2026-08-31 08:05:00")
        try:
            self.assertEqual(0, self._run(root))
            self.assertFalse((root / "account.db").exists())
        finally:
            tmp.cleanup()

    def test_stale_analysis_fails(self):
        tmp, root = self._root("2026-08-31 08:45:00")
        try:
            self.assertEqual(1, self._run(root))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
