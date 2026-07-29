# -*- coding: utf-8 -*-
"""Public migration CLI safety contracts.

The fixtures deliberately contain schemas which each migration could change.
Every assertion runs in an isolated temporary directory and never touches the
project's ``db/`` directory.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MIGRATION_GUARD = SCRIPTS / "migration_guard.py"

# Keep each script's historical target argument while standardising the safety
# flags.  This is also a concise inventory of the reviewed public migrations.
MIGRATIONS = {
    "apply_analysis_status_col.py": lambda root: ["--root", os.fspath(root)],
    "apply_data_enrichment_schema.py": lambda root: [
        "--db-root", os.fspath(root)
    ],
    "apply_decision_card_schema.py": lambda root: [
        "--db", os.fspath(root / "analysis.db")
    ],
    "apply_news_edge_schema.py": lambda root: [
        "--db-root", os.fspath(root)
    ],
    "apply_public_macro_schema.py": lambda root: [
        "--db-root", os.fspath(root)
    ],
    "apply_regime_split.py": lambda root: ["--db-root", os.fspath(root)],
    "apply_repair_queue_schema.py": lambda root: [
        "--db-root", os.fspath(root)
    ],
    "apply_trade_experiences_schema.py": lambda root: [
        "--db-root", os.fspath(root)
    ],
    "apply_ts_cst_migration.py": lambda root: [
        "--db-root", os.fspath(root)
    ],
}


def _executescript(path: Path, ddl: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(ddl)
        con.commit()
    finally:
        con.close()


def _fixture(root: Path) -> None:
    """Create one old-but-valid database set shared by all migration cases."""
    root.mkdir(parents=True, exist_ok=True)
    _executescript(
        root / "analysis.db",
        """
        CREATE TABLE analysis_runs (
            id INTEGER PRIMARY KEY,
            regime TEXT
        );
        INSERT INTO analysis_runs(id, regime) VALUES (1, 'trend');
        CREATE TABLE analysis_signals (
            id INTEGER PRIMARY KEY,
            symbol TEXT
        );
        INSERT INTO analysis_signals(id, symbol) VALUES (1, 'BTC-USDT-SWAP');
        """,
    )
    _executescript(
        root / "market.db",
        """
        CREATE TABLE cross_market (
            ts TEXT PRIMARY KEY,
            btc REAL
        );
        INSERT INTO cross_market(ts, btc)
        VALUES ('2026-07-01 00:00:00', 1.0);
        """,
    )
    _executescript(
        root / "regime.db",
        """
        CREATE TABLE cross_market (
            ts TEXT PRIMARY KEY,
            btc REAL
        );
        INSERT INTO cross_market(ts, btc)
        VALUES ('2026-06-30 00:00:00', 0.5);
        """,
    )
    _executescript(
        root / "news.db",
        """
        CREATE TABLE news_items (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL
        );
        INSERT INTO news_items(id, ts)
        VALUES (1, '2026-07-03T05:00:00Z');
        CREATE TABLE news_events_index (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL
        );
        INSERT INTO news_events_index(id, ts)
        VALUES (1, '2026-07-03T05:00:00Z');
        """,
    )
    _executescript(
        root / "account.db",
        """
        CREATE TABLE doc_versions (
            doc_path TEXT PRIMARY KEY,
            doc_version TEXT,
            last_updated TEXT,
            updated_by TEXT,
            change_summary TEXT
        );
        CREATE TABLE account_snapshots (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL
        );
        CREATE TABLE position_snapshots (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL
        );
        CREATE TABLE trade_events (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL
        );
        INSERT INTO account_snapshots(id, ts)
        VALUES (1, '2026-07-03T05:00:00Z');
        INSERT INTO position_snapshots(id, ts)
        VALUES (1, '2026-07-03T05:00:00Z');
        INSERT INTO trade_events(id, ts)
        VALUES (1, '2026-07-03T05:00:00Z');
        """,
    )


def _snapshot(root: Path) -> dict[str, str]:
    """Hash both database contents and the complete fixture file set."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, os.fspath(SCRIPTS / script), *args],
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


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "migration_guard_safety_test", MIGRATION_GUARD
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {MIGRATION_GUARD}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MigrationCliSafetyTests(unittest.TestCase):
    maxDiff = None

    def test_all_reviewed_migrations_expose_uniform_safety_flags(self):
        self.assertEqual(len(MIGRATIONS), 9)
        for script in MIGRATIONS:
            with self.subTest(script=script):
                result = _run(script, "--help")
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=f"{script}\nstdout={result.stdout}\nstderr={result.stderr}",
                )
                help_text = result.stdout + result.stderr
                for flag in ("--apply", "--backup-dir", "--dry-run"):
                    self.assertIn(flag, help_text, msg=f"{script}: missing {flag}")

    def test_default_and_explicit_dry_run_never_mutate_target(self):
        for script, target_args in MIGRATIONS.items():
            for explicit in (False, True):
                label = "explicit" if explicit else "default"
                with self.subTest(script=script, mode=label):
                    with tempfile.TemporaryDirectory() as temp:
                        db_root = Path(temp) / "db"
                        _fixture(db_root)
                        before = _snapshot(db_root)
                        args = target_args(db_root)
                        if explicit:
                            args = [*args, "--dry-run"]
                        result = _run(script, *args)
                        self.assertEqual(
                            result.returncode,
                            0,
                            msg=(
                                f"{script} {label} dry-run failed\n"
                                f"stdout={result.stdout}\nstderr={result.stderr}"
                            ),
                        )
                        self.assertEqual(
                            _snapshot(db_root),
                            before,
                            msg=f"{script} {label} dry-run mutated its target",
                        )

    def test_default_dry_run_does_not_create_missing_databases(self):
        for script, target_args in MIGRATIONS.items():
            with self.subTest(script=script):
                with tempfile.TemporaryDirectory() as temp:
                    db_root = Path(temp) / "empty-db-root"
                    db_root.mkdir()
                    before = _snapshot(db_root)
                    _run(script, *target_args(db_root))
                    self.assertEqual(
                        _snapshot(db_root),
                        before,
                        msg=f"{script} created a missing database in dry-run",
                    )

    def test_apply_without_backup_is_rejected_before_any_write(self):
        for script, target_args in MIGRATIONS.items():
            with self.subTest(script=script):
                with tempfile.TemporaryDirectory() as temp:
                    db_root = Path(temp) / "db"
                    _fixture(db_root)
                    before = _snapshot(db_root)
                    result = _run(script, *target_args(db_root), "--apply")
                    self.assertNotEqual(
                        result.returncode,
                        0,
                        msg=(
                            f"{script} accepted --apply without --backup-dir\n"
                            f"stdout={result.stdout}\nstderr={result.stderr}"
                        ),
                    )
                    self.assertEqual(
                        _snapshot(db_root),
                        before,
                        msg=f"{script} wrote before rejecting missing backup",
                    )

    def test_apply_succeeds_only_after_verified_backups_on_isolated_fixtures(self):
        for script, target_args in MIGRATIONS.items():
            with self.subTest(script=script):
                with tempfile.TemporaryDirectory() as temp:
                    temp_root = Path(temp)
                    db_root = temp_root / "db"
                    backup_dir = temp_root / "backups"
                    _fixture(db_root)
                    before = _snapshot(db_root)

                    result = _run(
                        script,
                        *target_args(db_root),
                        "--apply",
                        "--backup-dir",
                        os.fspath(backup_dir),
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        msg=(
                            f"{script} isolated apply failed\n"
                            f"stdout={result.stdout}\nstderr={result.stderr}"
                        ),
                    )
                    self.assertNotEqual(
                        _snapshot(db_root),
                        before,
                        msg=f"{script} reported success without changing fixture",
                    )
                    backups = sorted(backup_dir.glob("*.db"))
                    self.assertTrue(
                        backups,
                        msg=f"{script} applied without creating a backup",
                    )
                    for backup in backups:
                        con = sqlite3.connect(
                            f"{backup.resolve().as_uri()}?mode=ro", uri=True
                        )
                        try:
                            self.assertEqual(
                                con.execute(
                                    "PRAGMA integrity_check"
                                ).fetchone()[0],
                                "ok",
                                msg=f"{script}: invalid backup {backup.name}",
                            )
                        finally:
                            con.close()

    def test_backup_failure_aborts_before_any_target_write(self):
        for script, target_args in MIGRATIONS.items():
            with self.subTest(script=script):
                with tempfile.TemporaryDirectory() as temp:
                    temp_root = Path(temp)
                    db_root = temp_root / "db"
                    _fixture(db_root)
                    before = _snapshot(db_root)
                    invalid_backup_dir = temp_root / "not-a-directory"
                    invalid_backup_dir.write_text("sentinel", encoding="utf-8")

                    result = _run(
                        script,
                        *target_args(db_root),
                        "--apply",
                        "--backup-dir",
                        os.fspath(invalid_backup_dir),
                    )
                    self.assertNotEqual(
                        result.returncode,
                        0,
                        msg=f"{script} ignored a failed backup destination",
                    )
                    self.assertEqual(
                        _snapshot(db_root),
                        before,
                        msg=f"{script} touched its target before backup success",
                    )

    def test_shared_guard_backup_is_complete_and_does_not_mutate_source(self):
        guard = _load_guard()
        self.assertTrue(
            callable(getattr(guard, "backup_databases", None)),
            "migration_guard.backup_databases is the reviewed public helper",
        )
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            source = temp_root / "source.db"
            backup_dir = temp_root / "backups"
            _executescript(
                source,
                """
                CREATE TABLE sentinel (
                    id INTEGER PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO sentinel(id, value) VALUES (1, 'preserve-me');
                """,
            )
            source_before = hashlib.sha256(source.read_bytes()).hexdigest()

            guard.backup_databases([source], backup_dir, "unit-safety")

            self.assertEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(),
                source_before,
                "online backup mutated the source database",
            )
            backups = sorted(
                path for path in backup_dir.rglob("*") if path.is_file()
            )
            self.assertEqual(
                len(backups),
                1,
                msg=f"expected exactly one backup, found {backups}",
            )
            con = sqlite3.connect(
                f"{backups[0].resolve().as_uri()}?mode=ro", uri=True
            )
            try:
                self.assertEqual(
                    con.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(
                    con.execute(
                        "SELECT id, value FROM sentinel"
                    ).fetchall(),
                    [(1, "preserve-me")],
                )
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
