# -*- coding: utf-8 -*-
"""Safety contracts for the stage-profile-lease migration."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "collectors"))
import ledger  # noqa: E402


SCRIPT = ROOT / "scripts" / "apply_stage_profile_lease_schema.py"
EXPECTED_COLUMNS = ("profile", "cycle_id", "acquired_at", "expires_at")
EXPECTED_SCHEMA = (
    ("profile", "TEXT", 0, 1),
    ("cycle_id", "TEXT", 1, 0),
    ("acquired_at", "TEXT", 1, 0),
    ("expires_at", "TEXT", 1, 0),
)


def _create_ledger(path: Path, *, incompatible: bool = False,
                   weak_constraints: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE sentinel (
                id INTEGER PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO sentinel(id, value) VALUES (1, 'preserve-me');
            """
        )
        if weak_constraints:
            con.execute(
                "CREATE TABLE stage_profile_leases ("
                "profile TEXT, cycle_id TEXT, acquired_at TEXT, expires_at TEXT)"
            )
        elif incompatible:
            con.execute(
                "CREATE TABLE stage_profile_leases (profile TEXT PRIMARY KEY)"
            )
        con.commit()
    finally:
        con.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, os.fspath(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _table_columns(path: Path) -> tuple[str, ...]:
    con = sqlite3.connect(path)
    try:
        return tuple(
            row[1]
            for row in con.execute(
                "PRAGMA table_info(stage_profile_leases)"
            )
        )
    finally:
        con.close()


def _table_schema(path: Path) -> tuple[tuple[str, str, int, int], ...]:
    con = sqlite3.connect(path)
    try:
        return tuple(
            (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in con.execute(
                "PRAGMA table_info(stage_profile_leases)"
            )
        )
    finally:
        con.close()


class StageProfileLeaseMigrationTests(unittest.TestCase):
    maxDiff = None

    def test_cli_uses_public_guard_and_has_no_private_root(self):
        result = _run("--help")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for flag in ("--apply", "--dry-run", "--backup-dir", "--db-root"):
            self.assertIn(flag, result.stdout + result.stderr)
        self.assertNotRegex(
            SCRIPT.read_text(encoding="utf-8"), r"(?i)\b[A-Z]:[\\/]"
        )

    def test_default_and_explicit_dry_run_are_byte_read_only(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                with tempfile.TemporaryDirectory() as temp:
                    db_root = Path(temp) / "db"
                    ledger = db_root / "ledger.db"
                    _create_ledger(ledger)
                    before = _sha256(ledger)
                    args = ["--db-root", os.fspath(db_root)]
                    if explicit:
                        args.append("--dry-run")

                    result = _run(*args)

                    self.assertEqual(
                        result.returncode,
                        0,
                        msg=f"stdout={result.stdout}\nstderr={result.stderr}",
                    )
                    self.assertEqual(_sha256(ledger), before)
                    self.assertEqual(_table_columns(ledger), ())
                    report = json.loads(result.stdout)
                    self.assertTrue(report["dry_run"])
                    self.assertTrue(report["would_create"])

    def test_dry_run_does_not_create_a_missing_database(self):
        with tempfile.TemporaryDirectory() as temp:
            db_root = Path(temp) / "empty-db"
            db_root.mkdir()
            ledger = db_root / "ledger.db"

            for explicit in (False, True):
                args = ["--db-root", os.fspath(db_root)]
                if explicit:
                    args.append("--dry-run")
                result = _run(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(ledger.exists())

    def test_apply_without_backup_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            db_root = Path(temp) / "db"
            ledger = db_root / "ledger.db"
            _create_ledger(ledger)
            before = _sha256(ledger)

            result = _run("--db-root", os.fspath(db_root), "--apply")

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(_sha256(ledger), before)
            self.assertEqual(_table_columns(ledger), ())

    def test_runtime_init_refuses_unmigrated_existing_db_without_write(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger_path = Path(temp) / "ledger.db"
            _create_ledger(ledger_path)
            before = _sha256(ledger_path)

            with self.assertRaisesRegex(
                RuntimeError, "apply_stage_profile_lease_schema"
            ):
                ledger.init_ledger(ledger_path)

            self.assertEqual(_sha256(ledger_path), before)
            self.assertEqual(_table_columns(ledger_path), ())

    def test_backup_failure_aborts_before_target_write(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            db_root = temp_root / "db"
            ledger = db_root / "ledger.db"
            _create_ledger(ledger)
            before = _sha256(ledger)
            invalid_backup_dir = temp_root / "not-a-directory"
            invalid_backup_dir.write_text("sentinel", encoding="utf-8")

            result = _run(
                "--db-root",
                os.fspath(db_root),
                "--apply",
                "--backup-dir",
                os.fspath(invalid_backup_dir),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(_sha256(ledger), before)
            self.assertEqual(_table_columns(ledger), ())

    def test_apply_creates_verified_backup_then_only_target_table(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            db_root = temp_root / "db"
            backup_dir = temp_root / "backups"
            ledger = db_root / "ledger.db"
            _create_ledger(ledger)

            result = _run(
                "--db-root",
                os.fspath(db_root),
                "--apply",
                "--backup-dir",
                os.fspath(backup_dir),
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout={result.stdout}\nstderr={result.stderr}",
            )
            report = json.loads(result.stdout)
            self.assertTrue(report["applied"])
            self.assertTrue(report["changed"])
            self.assertEqual(tuple(report["columns"]), EXPECTED_COLUMNS)
            self.assertEqual(
                tuple(tuple(row) for row in report["schema"]), EXPECTED_SCHEMA
            )
            self.assertEqual(_table_columns(ledger), EXPECTED_COLUMNS)
            self.assertEqual(_table_schema(ledger), EXPECTED_SCHEMA)

            backups = sorted(backup_dir.glob("*.db"))
            self.assertEqual(len(backups), 1)
            backup = backups[0]
            self.assertEqual(_table_columns(backup), ())
            con = sqlite3.connect(backup)
            try:
                self.assertEqual(
                    con.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(
                    con.execute("SELECT id, value FROM sentinel").fetchall(),
                    [(1, "preserve-me")],
                )
            finally:
                con.close()

            con = sqlite3.connect(ledger)
            try:
                objects = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertEqual(objects, {"sentinel", "stage_profile_leases"})
                self.assertEqual(
                    con.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
            finally:
                con.close()

    def test_reapply_is_idempotent_and_incompatible_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            db_root = temp_root / "db"
            ledger = db_root / "ledger.db"
            _create_ledger(ledger)

            first = _run(
                "--db-root",
                os.fspath(db_root),
                "--apply",
                "--backup-dir",
                os.fspath(temp_root / "backup-1"),
            )
            second = _run(
                "--db-root",
                os.fspath(db_root),
                "--apply",
                "--backup-dir",
                os.fspath(temp_root / "backup-2"),
            )

            self.assertEqual(first.returncode, 0, msg=first.stderr)
            self.assertEqual(second.returncode, 0, msg=second.stderr)
            self.assertFalse(json.loads(second.stdout)["changed"])
            self.assertEqual(_table_columns(ledger), EXPECTED_COLUMNS)

        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            db_root = temp_root / "db"
            ledger = db_root / "ledger.db"
            _create_ledger(ledger, incompatible=True)
            before = _sha256(ledger)

            dry_run = _run("--db-root", os.fspath(db_root))
            self.assertNotEqual(dry_run.returncode, 0)
            self.assertEqual(_sha256(ledger), before)

            result = _run(
                "--db-root",
                os.fspath(db_root),
                "--apply",
                "--backup-dir",
                os.fspath(temp_root / "backups"),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(_sha256(ledger), before)
            self.assertEqual(_table_columns(ledger), ("profile",))

    def test_same_columns_without_pk_or_not_null_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            db_root = temp_root / "db"
            ledger_path = db_root / "ledger.db"
            _create_ledger(ledger_path, weak_constraints=True)
            before = _sha256(ledger_path)

            dry_run = _run("--db-root", os.fspath(db_root))
            self.assertNotEqual(dry_run.returncode, 0)
            self.assertEqual(_sha256(ledger_path), before)
            self.assertEqual(_table_columns(ledger_path), EXPECTED_COLUMNS)
            self.assertNotEqual(_table_schema(ledger_path), EXPECTED_SCHEMA)

            with self.assertRaisesRegex(RuntimeError, "incompatible"):
                ledger.init_ledger(ledger_path)
            self.assertEqual(_sha256(ledger_path), before)

            applied = _run(
                "--db-root", os.fspath(db_root), "--apply",
                "--backup-dir", os.fspath(temp_root / "backups"),
            )
            self.assertNotEqual(applied.returncode, 0)
            self.assertEqual(_sha256(ledger_path), before)
            self.assertNotEqual(_table_schema(ledger_path), EXPECTED_SCHEMA)


if __name__ == "__main__":
    unittest.main()
