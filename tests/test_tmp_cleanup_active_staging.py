# -*- coding: utf-8 -*-
"""Active staging lifecycle guard for tmp_cleanup (filesystem-only fixtures)."""
from __future__ import annotations

import os
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from threading import Barrier
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tmp_cleanup  # noqa: E402


def _create_account_db(
    db_root: Path,
    *,
    last_run: datetime | None = None,
    raw_status: str = "completed",
) -> Path:
    db_root.mkdir(parents=True, exist_ok=True)
    account_db = db_root / "account.db"
    conn = sqlite3.connect(str(account_db))
    try:
        conn.execute(tmp_cleanup._TMP_CLEANUP_RUNS_DDL)
        if last_run is not None:
            conn.execute(
                "INSERT INTO tmp_cleanup_runs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    last_run.isoformat(), 0, 1, 0, 0, 0, 0, None,
                    json.dumps({"status": raw_status}),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return account_db


def _cleanup_rows(account_db: Path) -> list[tuple]:
    conn = sqlite3.connect(str(account_db))
    try:
        return conn.execute(
            "SELECT run_utc,dry_run,scanned,kept_recent,skipped,archived,"
            "bytes_archived,archive_dir,raw_json FROM tmp_cleanup_runs "
            "ORDER BY run_utc"
        ).fetchall()
    finally:
        conn.close()


def _write_keep_marker(
    directory: Path,
    now_utc: datetime,
    *,
    expires_in: timedelta = timedelta(days=7),
) -> Path:
    marker = directory / tmp_cleanup.TMP_PROTECT_SENTINEL
    cst = timezone(timedelta(hours=8))
    expires = (now_utc + expires_in).astimezone(cst)
    created = (
        now_utc - timedelta(hours=1)
        if expires_in > timedelta(0)
        else now_utc + expires_in - timedelta(hours=1)
    ).astimezone(cst)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reason": "fixture evidence pending owner decision",
                "owner": "test",
                "created_at_cst": created.isoformat(),
                "expires_at_cst": expires.isoformat(),
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    return marker


def _age(path: Path, now_utc: datetime, days: float = 10) -> None:
    old_ts = now_utc.timestamp() - days * 86400
    os.utime(path, (old_ts, old_ts))


class RecentStagingSubtreeTests(unittest.TestCase):
    def test_old_copied_file_is_protected_by_recent_staging_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            staging = tmp_root / "goal-v21-staging-20260816-0142"
            staging.mkdir()
            copied = staging / "old-source.py"
            copied.write_text("pass\n", encoding="utf-8")
            now_ts = time.time()
            ten_days_ago = now_ts - 10 * 86400
            os.utime(copied, (ten_days_ago, ten_days_ago))

            self.assertTrue(
                tmp_cleanup.is_recent_staging_subtree(
                    copied, tmp_root, now_ts, 3 * 86400
                )
            )

    def test_staging_exemption_expires_with_normal_keep_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            staging = tmp_root / "goal-v21-staging-20260812-0000"
            staging.mkdir()
            copied = staging / "old-source.py"
            copied.write_text("pass\n", encoding="utf-8")
            now_ts = time.time()
            four_days_ago = now_ts - 4 * 86400
            os.utime(staging, (four_days_ago, four_days_ago))

            self.assertFalse(
                tmp_cleanup.is_recent_staging_subtree(
                    copied, tmp_root, now_ts, 3 * 86400
                )
            )

    def test_non_staging_active_directory_does_not_gain_blanket_exemption(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            rolling = tmp_root / "push_pipeline"
            rolling.mkdir()
            old_file = rolling / "old-payload.json"
            old_file.write_text("{}\n", encoding="utf-8")

            self.assertFalse(
                tmp_cleanup.is_recent_staging_subtree(
                    old_file, tmp_root, time.time(), 3 * 86400
                )
            )

    def test_explicit_marker_protects_only_its_subtree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            protected = tmp_root / "owner-decision-pending"
            protected.mkdir()
            marker = protected / tmp_cleanup.TMP_PROTECT_SENTINEL
            now_utc = datetime.now(timezone.utc)
            marker.write_text(
                "{\n"
                '  "schema_version": 1,\n'
                '  "reason": "pending owner decision",\n'
                '  "owner": "test",\n'
                f'  "created_at_cst": "{now_utc.isoformat()}",\n'
                f'  "expires_at_cst": "{(now_utc + timedelta(days=7)).isoformat()}"\n'
                "}\n",
                encoding="utf-8",
            )
            nested = protected / "large-evidence.db"
            nested.write_bytes(b"evidence")
            unrelated = tmp_root / "unrelated.db"
            unrelated.write_bytes(b"scratch")

            self.assertTrue(
                tmp_cleanup.is_explicitly_protected_tmp_subtree(
                    nested, tmp_root, now_utc
                )
            )
            self.assertTrue(
                tmp_cleanup.is_explicitly_protected_tmp_subtree(
                    marker, tmp_root, now_utc
                )
            )
            self.assertFalse(
                tmp_cleanup.is_explicitly_protected_tmp_subtree(
                    unrelated, tmp_root, now_utc
                )
            )

    def test_expired_or_malformed_marker_does_not_protect(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            protected = tmp_root / "expired"
            protected.mkdir()
            marker = protected / tmp_cleanup.TMP_PROTECT_SENTINEL
            target = protected / "old.db"
            target.write_bytes(b"old")
            now_utc = datetime(2026, 8, 16, tzinfo=timezone.utc)
            marker.write_text(
                '{"schema_version":1,"reason":"expired","owner":"test",'
                '"created_at_cst":"2026-08-10T00:00:00+00:00",'
                '"expires_at_cst":"2026-08-11T00:00:00+00:00"}\n',
                encoding="utf-8",
            )
            status, _, _ = tmp_cleanup.tmp_protection_status(
                target, tmp_root, now_utc
            )
            self.assertEqual("expired", status)
            self.assertFalse(
                tmp_cleanup.is_explicitly_protected_tmp_subtree(
                    target, tmp_root, now_utc
                )
            )

            marker.write_text("not-json\n", encoding="utf-8")
            status, _, _ = tmp_cleanup.tmp_protection_status(
                target, tmp_root, now_utc
            )
            self.assertEqual("invalid", status)
            self.assertFalse(
                tmp_cleanup.is_explicitly_protected_tmp_subtree(
                    target, tmp_root, now_utc
                )
            )

    def test_recent_apply_makes_three_day_reviewer_call_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            okx_root = Path(tmp)
            tmp_root = okx_root / "tmp"
            db_root = okx_root / "db"
            tmp_root.mkdir()
            old_file = tmp_root / "old-payload.json"
            old_file.write_text("{}\n", encoding="utf-8")

            now_utc = datetime(2026, 8, 16, 0, 5, tzinfo=timezone.utc)
            _age(old_file, now_utc)
            previous = now_utc - timedelta(days=3) + timedelta(minutes=6)
            account_db = _create_account_db(db_root, last_run=previous)
            before = _cleanup_rows(account_db)

            argv = [
                "tmp_cleanup.py", "--okx-root", str(okx_root), "--apply",
                "--minimum-interval-days", "3", "--keep-days", "1",
                "--archive-days", "1", "--hard-delete-tmp-days", "1",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                tmp_cleanup, "utc_now", return_value=now_utc
            ):
                self.assertEqual(0, tmp_cleanup.main())

            self.assertTrue(old_file.is_file())
            self.assertFalse((tmp_root / "archive").exists())
            self.assertEqual(before, _cleanup_rows(account_db))

    def test_interval_becomes_due_after_three_full_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp)
            conn = sqlite3.connect(str(db_root / "account.db"))
            try:
                conn.execute(
                    "CREATE TABLE tmp_cleanup_runs "
                    "(run_utc TEXT PRIMARY KEY, dry_run INTEGER NOT NULL)"
                )
                last = datetime(2026, 8, 13, tzinfo=timezone.utc)
                conn.execute(
                    "INSERT INTO tmp_cleanup_runs VALUES (?, 0)",
                    (last.isoformat(),),
                )
                conn.commit()
            finally:
                conn.close()

            due, actual = tmp_cleanup.cleanup_interval_due(
                db_root,
                last + timedelta(days=3),
                3,
            )
            self.assertTrue(due)
            self.assertEqual(last, actual)

    def test_two_concurrent_instances_only_one_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp)
            account_db = _create_account_db(db_root)
            now_utc = datetime(2026, 8, 16, 0, 5, tzinfo=timezone.utc)
            barrier = Barrier(2)

            def claim() -> dict[str, object]:
                barrier.wait(timeout=2)
                return tmp_cleanup.claim_cleanup_run(db_root, now_utc, 3)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: claim(), range(2)))

            self.assertEqual(1, sum(item["claimed"] is True for item in results))
            self.assertEqual(1, len(_cleanup_rows(account_db)))

    def test_missing_cadence_db_fails_before_filesystem_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            okx_root = Path(tmp)
            tmp_root = okx_root / "tmp"
            (okx_root / "db").mkdir()
            tmp_root.mkdir()
            old_file = tmp_root / "old.json"
            old_file.write_text("{}\n", encoding="utf-8")
            now_utc = datetime(2026, 8, 16, 0, 5, tzinfo=timezone.utc)
            _age(old_file, now_utc)
            argv = [
                "tmp_cleanup.py", "--okx-root", str(okx_root), "--apply",
                "--minimum-interval-days", "3", "--keep-days", "1",
                "--archive-days", "1", "--hard-delete-tmp-days", "1",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                tmp_cleanup, "utc_now", return_value=now_utc
            ):
                self.assertEqual(2, tmp_cleanup.main())
            self.assertTrue(old_file.is_file())
            self.assertFalse((tmp_root / "archive").exists())

    def test_finalize_failure_leaves_claim_blocking_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            okx_root = Path(tmp)
            tmp_root = okx_root / "tmp"
            tmp_root.mkdir()
            account_db = _create_account_db(okx_root / "db")
            old_file = tmp_root / "old.json"
            old_file.write_text("{}\n", encoding="utf-8")
            now_utc = datetime(2026, 8, 16, 0, 5, tzinfo=timezone.utc)
            _age(old_file, now_utc)
            argv = [
                "tmp_cleanup.py", "--okx-root", str(okx_root), "--apply",
                "--minimum-interval-days", "3", "--keep-days", "1",
                "--archive-days", "1", "--hard-delete-tmp-days", "1",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                tmp_cleanup, "utc_now", return_value=now_utc
            ), mock.patch.object(
                tmp_cleanup, "finalize_cleanup_claim",
                side_effect=sqlite3.OperationalError("finish denied"),
            ):
                self.assertEqual(2, tmp_cleanup.main())
            self.assertFalse(old_file.exists())
            rows = _cleanup_rows(account_db)
            self.assertEqual(1, len(rows))
            self.assertEqual("claimed", json.loads(rows[0][-1])["status"])
            retry = tmp_cleanup.claim_cleanup_run(
                okx_root / "db", now_utc + timedelta(days=1), 3
            )
            self.assertFalse(retry["claimed"])

    def test_marker_protects_subtree_during_main_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            okx_root = Path(tmp)
            tmp_root = okx_root / "tmp"
            protected = tmp_root / "owner-decision-pending"
            protected.mkdir(parents=True)
            account_db = _create_account_db(okx_root / "db")
            now_utc = datetime(2026, 8, 16, 0, 5, tzinfo=timezone.utc)
            marker = protected / tmp_cleanup.TMP_PROTECT_SENTINEL
            marker.write_text(json.dumps({
                "schema_version": 1,
                "reason": "pending owner decision",
                "owner": "test",
                "created_at_cst": (now_utc - timedelta(hours=1)).isoformat(),
                "expires_at_cst": (now_utc + timedelta(days=7)).isoformat(),
            }), encoding="utf-8")
            evidence = protected / "market.db"
            evidence.write_bytes(b"evidence")
            disposable = tmp_root / "old.json"
            disposable.write_text("{}\n", encoding="utf-8")
            _age(evidence, now_utc)
            _age(disposable, now_utc)
            argv = [
                "tmp_cleanup.py", "--okx-root", str(okx_root), "--apply",
                "--minimum-interval-days", "3", "--keep-days", "1",
                "--archive-days", "1", "--hard-delete-tmp-days", "1",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                tmp_cleanup, "utc_now", return_value=now_utc
            ), redirect_stdout(StringIO()):
                self.assertEqual(0, tmp_cleanup.main())
            self.assertTrue(marker.is_file())
            self.assertTrue(evidence.is_file())
            self.assertFalse(disposable.exists())
            raw = json.loads(_cleanup_rows(account_db)[0][-1])
            self.assertEqual("completed", raw["status"])
            self.assertGreaterEqual(raw["kept_protected"], 2)
            self.assertEqual(1, raw["tmp_hard_deleted"])

    def test_cadence_noop_surfaces_shadow_without_mutation_or_db_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            okx_root = Path(tmp)
            tmp_root = okx_root / "tmp"
            tmp_root.mkdir()
            now_utc = datetime(2026, 8, 16, 0, 5, tzinfo=timezone.utc)
            account_db = _create_account_db(
                okx_root / "db", last_run=now_utc - timedelta(days=1)
            )
            shadow = tmp_root / "bisect.py"
            shadow.write_text("raise RuntimeError\n", encoding="utf-8")
            before = _cleanup_rows(account_db)
            argv = [
                "tmp_cleanup.py", "--okx-root", str(okx_root), "--apply",
                "--minimum-interval-days", "3",
            ]
            output = StringIO()
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                tmp_cleanup, "utc_now", return_value=now_utc
            ), redirect_stdout(output):
                self.assertEqual(2, tmp_cleanup.main())
            self.assertTrue(shadow.is_file())
            self.assertEqual(before, _cleanup_rows(account_db))
            self.assertFalse((tmp_root / "archive").exists())
            self.assertIn("bisect.py", output.getvalue())

    def test_hard_delete_failure_is_not_counted_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            okx_root = Path(tmp)
            tmp_root = okx_root / "tmp"
            tmp_root.mkdir()
            account_db = _create_account_db(okx_root / "db")
            now_utc = datetime(2026, 8, 16, 0, 5, tzinfo=timezone.utc)
            old_file = tmp_root / "old.json"
            old_file.write_text("{}\n", encoding="utf-8")
            _age(old_file, now_utc)
            argv = [
                "tmp_cleanup.py", "--okx-root", str(okx_root), "--apply",
                "--minimum-interval-days", "3", "--keep-days", "1",
                "--archive-days", "1", "--hard-delete-tmp-days", "1",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                tmp_cleanup, "utc_now", return_value=now_utc
            ), mock.patch.object(
                Path, "unlink", side_effect=PermissionError("denied")
            ), redirect_stdout(StringIO()):
                self.assertEqual(2, tmp_cleanup.main())
            self.assertTrue(old_file.is_file())
            raw = json.loads(_cleanup_rows(account_db)[0][-1])
            self.assertEqual("completed_with_errors", raw["status"])
            self.assertEqual(0, raw["tmp_hard_deleted"])
            self.assertEqual(1, raw["tmp_hard_delete_failed"])

    def test_archive_purge_failure_is_not_counted_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            okx_root = Path(tmp)
            archive = okx_root / "tmp" / "archive" / "old-run"
            archive.mkdir(parents=True)
            account_db = _create_account_db(okx_root / "db")
            now_utc = datetime(2026, 8, 16, 0, 5, tzinfo=timezone.utc)
            old_file = archive / "old.json"
            old_file.write_text("{}\n", encoding="utf-8")
            _age(old_file, now_utc, days=40)
            _age(archive, now_utc, days=40)
            argv = [
                "tmp_cleanup.py", "--okx-root", str(okx_root), "--apply",
                "--minimum-interval-days", "3", "--purge-archive",
                "--archive-keep-days", "30",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                tmp_cleanup, "utc_now", return_value=now_utc
            ), mock.patch.object(
                tmp_cleanup.shutil, "rmtree", side_effect=OSError("denied")
            ), redirect_stdout(StringIO()):
                self.assertEqual(2, tmp_cleanup.main())
            self.assertTrue(archive.is_dir())
            raw = json.loads(_cleanup_rows(account_db)[0][-1])
            self.assertEqual("completed_with_errors", raw["status"])
            self.assertEqual(0, raw["archive_purged"])
            self.assertEqual(1, raw["archive_purge_failed"])


class ArchiveMarkerProtectionTests(unittest.TestCase):
    def test_valid_nested_marker_protects_only_its_archive_purge_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            archive_root = tmp_root / "archive"
            protected = archive_root / "daily-a"
            nested = protected / "evidence"
            expired_dir = protected / "z-expired"
            invalid_dir = protected / "zz-invalid"
            unrelated = archive_root / "daily-b"
            whitelisted = archive_root / "precutover-snapshot"
            for directory in (
                nested, expired_dir, invalid_dir, unrelated, whitelisted
            ):
                directory.mkdir(parents=True)
            now_utc = datetime(2026, 8, 16, tzinfo=timezone.utc)
            valid_marker = _write_keep_marker(nested, now_utc)
            expired_marker = _write_keep_marker(
                expired_dir, now_utc, expires_in=timedelta(hours=-2)
            )
            invalid_marker = invalid_dir / tmp_cleanup.TMP_PROTECT_SENTINEL
            invalid_marker.write_text("not-json\n", encoding="utf-8")
            protected_file = nested / "evidence.json"
            unrelated_file = unrelated / "old.json"
            protected_file.write_text("{}\n", encoding="utf-8")
            unrelated_file.write_text("{}\n", encoding="utf-8")
            for path in (
                expired_marker, invalid_marker, protected_file, unrelated_file
            ):
                _age(path, now_utc, days=40)

            stats = tmp_cleanup.CleanupStats()
            moves: list[dict[str, object]] = []
            tmp_cleanup.purge_archive(
                tmp_root, now_utc.timestamp(), 30 * 86400, False, stats, moves
            )

            self.assertTrue(protected.is_dir())
            self.assertFalse(unrelated.exists())
            self.assertTrue(whitelisted.is_dir())
            self.assertEqual(2, stats.archive_kept_protected)
            self.assertEqual(1, stats.archive_purged)
            self.assertEqual(1, stats.archive_marker_expired)
            self.assertEqual(1, stats.archive_marker_invalid)
            marker_move = next(
                move for move in moves
                if move.get("kind") == "archive_protected_marker"
            )
            self.assertEqual(str(valid_marker), marker_move["marker"])

    def test_expired_and_malformed_markers_do_not_protect(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            archive_root = tmp_root / "archive"
            expired = archive_root / "expired-daily"
            malformed = archive_root / "malformed-daily"
            expired.mkdir(parents=True)
            malformed.mkdir(parents=True)
            now_utc = datetime(2026, 8, 16, tzinfo=timezone.utc)
            expired_marker = _write_keep_marker(
                expired, now_utc, expires_in=timedelta(hours=-2)
            )
            malformed_marker = malformed / tmp_cleanup.TMP_PROTECT_SENTINEL
            malformed_marker.write_text("not-json\n", encoding="utf-8")
            for directory, marker in (
                (expired, expired_marker),
                (malformed, malformed_marker),
            ):
                payload = directory / "old.json"
                payload.write_text("{}\n", encoding="utf-8")
                _age(payload, now_utc, days=40)
                _age(marker, now_utc, days=40)

            stats = tmp_cleanup.CleanupStats()
            moves: list[dict[str, object]] = []
            tmp_cleanup.purge_archive(
                tmp_root, now_utc.timestamp(), 30 * 86400, False, stats, moves
            )

            self.assertFalse(expired.exists())
            self.assertFalse(malformed.exists())
            self.assertEqual(2, stats.archive_purged)
            self.assertEqual(1, stats.archive_marker_expired)
            self.assertEqual(1, stats.archive_marker_invalid)

    def test_marker_at_archive_root_cannot_protect_every_archive_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            archive_root = tmp_root / "archive"
            aged = archive_root / "ordinary-daily"
            aged.mkdir(parents=True)
            now_utc = datetime(2026, 8, 16, tzinfo=timezone.utc)
            _write_keep_marker(archive_root, now_utc)
            payload = aged / "old.json"
            payload.write_text("{}\n", encoding="utf-8")
            _age(payload, now_utc, days=40)

            stats = tmp_cleanup.CleanupStats()
            tmp_cleanup.purge_archive(
                tmp_root, now_utc.timestamp(), 30 * 86400, False, stats, []
            )

            self.assertFalse(aged.exists())
            self.assertEqual(0, stats.archive_kept_protected)
            self.assertEqual(1, stats.archive_purged)


if __name__ == "__main__":
    unittest.main()
