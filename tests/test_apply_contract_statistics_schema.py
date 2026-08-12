from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import apply_contract_statistics_schema as migration  # noqa: E402


class ContractStatisticsSchemaTests(unittest.TestCase):
    def test_dry_run_and_backup_required_apply_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "market.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE marker(value TEXT)")
            con.execute("INSERT INTO marker VALUES('preserved')")
            con.commit()
            con.close()

            output = io.StringIO()
            with redirect_stdout(output):
                rc = migration.main(["--db", str(db_path)])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(output.getvalue())["action"], "plan-only")
            con = sqlite3.connect(db_path)
            self.assertFalse(con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (migration.TABLE,),
            ).fetchone())
            con.close()

            output = io.StringIO()
            with redirect_stdout(output):
                rc = migration.main(["--db", str(db_path), "--apply"])
            self.assertEqual(rc, 2)
            self.assertIn("backup-dir", json.loads(output.getvalue())["error"])

            backup_dir = root / "backups"
            output = io.StringIO()
            with redirect_stdout(output):
                rc = migration.main([
                    "--db", str(db_path), "--apply",
                    "--backup-dir", str(backup_dir),
                ])
            result = json.loads(output.getvalue())
            self.assertEqual(rc, 0)
            self.assertEqual(result["action"], "applied")
            self.assertTrue(Path(result["backup"]).exists())

            con = sqlite3.connect(db_path)
            columns = tuple(
                row[1] for row in con.execute(
                    f"PRAGMA table_info({migration.TABLE})"))
            marker = con.execute("SELECT value FROM marker").fetchone()[0]
            check = con.execute("PRAGMA quick_check").fetchone()[0]
            con.close()
            self.assertEqual(columns, migration.REQUIRED_COLUMNS)
            self.assertEqual(marker, "preserved")
            self.assertEqual(check, "ok")


if __name__ == "__main__":
    unittest.main()
