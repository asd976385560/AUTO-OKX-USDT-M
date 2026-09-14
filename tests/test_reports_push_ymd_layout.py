# -*- coding: utf-8 -*-
"""Isolated forward-layout tests; production files, DBs and send paths untouched."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _artifact_paths as artifact_paths  # noqa: E402
import push_archive  # noqa: E402
import push_pipeline  # noqa: E402
import reports_rotate  # noqa: E402


FIXED_NOW_TS = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc).timestamp()
FIXED_OLD_TS = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc).timestamp()


class ArtifactPathTests(unittest.TestCase):
    def test_preregistered_boundary_is_forward_only(self):
        root = Path("X:/reports/push")
        before = artifact_paths.forward_artifact_path(
            root, "2026-08-17T23:45", "before.json")
        active = artifact_paths.forward_artifact_path(
            root, "2026-08-18T00:00", "active.json")
        self.assertEqual(root / "before.json", before)
        self.assertEqual(
            root / "2026" / "08" / "18" / "active.json", active)
        self.assertEqual(
            "legacy_flat", artifact_paths.layout_for_cycle("2026-08-17T23:45"))
        self.assertEqual(
            "dated_ymd", artifact_paths.layout_for_cycle("2026-08-18T00:00"))

    def test_invalid_cycle_and_traversal_fail_closed(self):
        with self.assertRaises(ValueError):
            artifact_paths.forward_artifact_path(
                Path("X:/reports/push"), "2026-08-18T00:01", "bad.json")
        with self.assertRaises(ValueError):
            artifact_paths.forward_artifact_path(
                Path("X:/reports/push"), "2026-08-18T00:00", "../bad.json")
        with self.assertRaises(ValueError):
            list(artifact_paths.iter_legacy_and_ymd_files(Path("X:/"), "**/*.json"))

    @unittest.skipUnless(os.name == "nt", "Windows junction semantics required")
    def test_reader_rejects_outside_root_junction_before_descent(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as outside,
        ):
            report_dir = Path(temp) / "reports" / "push"
            report_dir.mkdir(parents=True)
            outside_year = Path(outside) / "outside-year"
            rogue = outside_year / "08" / "18" / "rogue.json"
            rogue.parent.mkdir(parents=True)
            rogue.write_text("must-not-be-read", encoding="utf-8")
            junction = report_dir / "2026"
            comspec = os.environ.get("ComSpec", "cmd.exe")
            made = subprocess.run(
                [comspec, "/d", "/c", "mklink", "/J", str(junction), str(outside_year)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, made.returncode, made.stdout + made.stderr)
            try:
                attrs = getattr(os.lstat(junction), "st_file_attributes", 0)
                self.assertTrue(
                    attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                )
                self.assertEqual(
                    [],
                    list(artifact_paths.iter_legacy_and_ymd_files(
                        report_dir, "*.json")),
                )
                self.assertTrue(rogue.is_file())
            finally:
                os.rmdir(junction)


class PushPipelineLayoutTests(unittest.TestCase):
    def _patched_output_roots(self, root: Path):
        return (
            mock.patch.object(push_pipeline, "WORK", root / "work"),
            mock.patch.object(push_pipeline, "REPORT_DIR", root / "reports" / "push"),
            mock.patch.object(push_pipeline, "RUNLOG", root / "logs" / "pipeline.jsonl"),
        )

    @staticmethod
    def _make_junction(link: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        link.parent.mkdir(parents=True, exist_ok=True)
        comspec = os.environ.get("ComSpec", "cmd.exe")
        made = subprocess.run(
            [comspec, "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if made.returncode != 0:
            raise AssertionError(made.stdout + made.stderr)

    def _assert_directory_junction_rejected(self, unsafe_stage: str) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = Path(temp)
            report_dir = root / "reports" / "push"
            outside_target = Path(outside) / f"{unsafe_stage}-target"
            locations = {
                "root": report_dir,
                "year": report_dir / "2026",
                "month": report_dir / "2026" / "08",
                "day": report_dir / "2026" / "08" / "18",
            }
            outside_suffixes = {
                "root": ("2026", "08", "18"),
                "year": ("08", "18"),
                "month": ("18",),
                "day": (),
            }
            junction = locations[unsafe_stage]
            self._make_junction(junction, outside_target)
            filename = "pipeline-2026-08-18-0000.json"
            outside_report = outside_target.joinpath(
                *outside_suffixes[unsafe_stage], filename)
            try:
                patches = self._patched_output_roots(root)
                with patches[0], patches[1], patches[2]:
                    result = push_pipeline._finish({
                        "cycle": "2026-08-18T00:00",
                        "ok": True,
                        "steps": {},
                    })
                self.assertFalse(outside_report.exists())
                storage = result["artifact_storage"]
                self.assertEqual("rejected", storage["write_status"])
                self.assertEqual("unsafe_artifact_path", storage["error_code"])
                self.assertEqual(unsafe_stage, storage["unsafe_stage"])
                self.assertEqual("reparse_point", storage["unsafe_reason"])
                rows = [json.loads(line) for line in (
                    root / "logs" / "pipeline.jsonl"
                ).read_text(encoding="utf-8").splitlines()]
                self.assertEqual(storage, rows[-1]["artifact_storage"])
            finally:
                os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt", "Windows junction semantics required")
    def test_writer_rejects_root_junction(self):
        self._assert_directory_junction_rejected("root")

    @unittest.skipUnless(os.name == "nt", "Windows junction semantics required")
    def test_writer_rejects_real_year_junction(self):
        self._assert_directory_junction_rejected("year")

    @unittest.skipUnless(os.name == "nt", "Windows junction semantics required")
    def test_writer_rejects_month_junction(self):
        self._assert_directory_junction_rejected("month")

    @unittest.skipUnless(os.name == "nt", "Windows junction semantics required")
    def test_writer_rejects_day_junction(self):
        self._assert_directory_junction_rejected("day")

    @unittest.skipUnless(os.name == "nt", "Windows reparse semantics required")
    def test_writer_rejects_file_reparse_without_touching_target(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = Path(temp)
            report_dir = root / "reports" / "push"
            dated_dir = report_dir / "2026" / "08" / "18"
            dated_dir.mkdir(parents=True)
            outside_file = Path(outside) / "outside.json"
            outside_file.write_text("sentinel", encoding="utf-8")
            report_link = dated_dir / "pipeline-2026-08-18-0000.json"
            try:
                os.symlink(outside_file, report_link)
            except OSError:
                comspec = os.environ.get("ComSpec", "cmd.exe")
                made = subprocess.run(
                    [comspec, "/d", "/c", "mklink", str(report_link), str(outside_file)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, made.returncode, made.stdout + made.stderr)
            try:
                patches = self._patched_output_roots(root)
                with patches[0], patches[1], patches[2]:
                    result = push_pipeline._finish({
                        "cycle": "2026-08-18T00:00",
                        "ok": True,
                        "steps": {},
                    })
                self.assertEqual("sentinel", outside_file.read_text(encoding="utf-8"))
                storage = result["artifact_storage"]
                self.assertEqual("rejected", storage["write_status"])
                self.assertEqual("file", storage["unsafe_stage"])
                self.assertEqual("reparse_point", storage["unsafe_reason"])
                rows = [json.loads(line) for line in (
                    root / "logs" / "pipeline.jsonl"
                ).read_text(encoding="utf-8").splitlines()]
                self.assertEqual(storage, rows[-1]["artifact_storage"])
            finally:
                report_link.unlink()

    def test_finish_keeps_pre_activation_flat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "reports").mkdir()
            patches = self._patched_output_roots(root)
            with patches[0], patches[1], patches[2]:
                result = push_pipeline._finish({
                    "cycle": "2026-08-17T23:45", "ok": True, "steps": {}})
            expected = root / "reports" / "push" / "pipeline-2026-08-17-2345.json"
            self.assertTrue(expected.is_file())
            self.assertEqual("legacy_flat", result["artifact_storage"]["layout"])
            self.assertFalse(result["artifact_storage"]["historical_files_moved"])

    def test_successful_pipeline_writes_future_cycle_to_ymd(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "reports").mkdir()
            archive_path = root / "dev-archive.md"
            content = "第1轮\n📊 资产\n" + ("完整报告正文" * 100)

            def fake_run(script: str, args: list, stdin_text: str | None = None):
                name = Path(script).name
                if name == "render_push_report.py":
                    out_file = Path(args[args.index("--out-file") + 1])
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    out_file.write_text(content, encoding="utf-8")
                    return 0, json.dumps({
                        "ok": True,
                        "render_ok": True,
                        "bytes": out_file.stat().st_size,
                        "title": "fixture",
                        "validation_fused": True,
                        "validation": {
                            "ok": True,
                            "errors": [],
                            "missing_fields": [],
                            "char_count": len(content),
                        },
                    }), ""
                if name == "push_archive.py":
                    source = Path(push_pipeline.WORK / "content-2026-08-18-0000.txt")
                    archive_path.write_text("# fixture\n\n" + source.read_text(
                        encoding="utf-8"), encoding="utf-8")
                    return 0, json.dumps({
                        "ok": True,
                        "path": str(archive_path),
                        "bytes": archive_path.stat().st_size,
                        "degraded": False,
                    }), ""
                raise AssertionError(f"unexpected child: {name}")

            builder = types.SimpleNamespace(build=lambda _root, _cycle: {
                "action_taken": "HOLD", "symbol": None, "trades": {"live": []},
            })
            patches = self._patched_output_roots(root)
            with (
                patches[0], patches[1], patches[2],
                mock.patch.object(push_pipeline, "_load_build", return_value=builder),
                mock.patch.object(push_pipeline, "_run", side_effect=fake_run),
                mock.patch.object(
                    push_pipeline, "_verify_business_attestation",
                    return_value={"ok": True, "required": True}),
            ):
                result = push_pipeline.run(
                    "2026-08-18T00:00", str(root / "db"), no_send=True)

            expected = (
                root / "reports" / "push" / "2026" / "08" / "18"
                / "pipeline-2026-08-18-0000.json"
            )
            self.assertTrue(result["ok"])
            self.assertTrue(expected.is_file())
            stored = json.loads(expected.read_text(encoding="utf-8"))
            self.assertEqual("dated_ymd", stored["artifact_storage"]["layout"])
            self.assertEqual(str(expected), stored["artifact_storage"]["path"])
            run_rows = [json.loads(line) for line in (
                root / "logs" / "pipeline.jsonl").read_text(
                    encoding="utf-8").splitlines()]
            self.assertEqual(str(expected), run_rows[-1]["artifact_storage"]["path"])


class ReportsRotateCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _make_old(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
        os.utime(path, (FIXED_OLD_TS, FIXED_OLD_TS))
        return path

    def test_rotate_reads_flat_and_strict_ymd_but_not_arbitrary_subtrees(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "reports" / "push"
            archive_dir = root / "reports" / "archive"
            legacy = self._make_old(report_dir / "legacy.json")
            dated = self._make_old(
                report_dir / "2026" / "06" / "01" / "dated.json")
            unrelated = self._make_old(report_dir / "scratch" / "rogue.json")
            with (
                mock.patch.object(
                    reports_rotate, "TARGETS", [("push", report_dir, "*.json")]),
                mock.patch.object(reports_rotate, "ARCHIVE_DIR", archive_dir),
                mock.patch.object(reports_rotate.time, "time", return_value=FIXED_NOW_TS),
            ):
                result = reports_rotate.rotate(days=30, apply=True)

            self.assertEqual(2, result["groups"]["push"]["candidates"])
            self.assertEqual(2, result["groups"]["push"]["archived_deleted"])
            self.assertFalse(legacy.exists())
            self.assertFalse(dated.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(0, result["groups"]["push"]["layout_migration_moves"])
            self.assertTrue(
                result["groups"]["push"]["existing_retention_policy_unchanged"])

            zip_paths = sorted(archive_dir.glob("push-*.zip"))
            self.assertEqual(2, len(zip_paths))
            self.assertEqual(
                ["push-202605.zip", "push-202606.zip"],
                [path.name for path in zip_paths],
            )
            members: set[str] = set()
            for zip_path in zip_paths:
                with zipfile.ZipFile(zip_path, "r") as archive:
                    members.update(archive.namelist())
            self.assertIn("legacy.json", members)
            self.assertIn("2026/06/01/dated.json", members)

    def test_agents_rotation_remains_strictly_root_flat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "reports" / "agents"
            archive_dir = root / "reports" / "archive"
            legacy = self._make_old(report_dir / "legacy.md")
            nested = self._make_old(
                report_dir / "2026" / "06" / "01" / "out-of-scope.md")
            with (
                mock.patch.object(
                    reports_rotate, "TARGETS", [("agents", report_dir, "*.md")]),
                mock.patch.object(reports_rotate, "ARCHIVE_DIR", archive_dir),
                mock.patch.object(reports_rotate.time, "time", return_value=FIXED_NOW_TS),
            ):
                result = reports_rotate.rotate(days=30, apply=True)

            group = result["groups"]["agents"]
            self.assertEqual(1, group["candidates"])
            self.assertEqual(1, group["archived_deleted"])
            self.assertEqual(["legacy_flat"], group["layouts_read"])
            self.assertFalse(legacy.exists())
            self.assertTrue(nested.exists())
            zip_path = archive_dir / "agents-202605.zip"
            with zipfile.ZipFile(zip_path, "r") as archive:
                self.assertEqual(["legacy.md"], archive.namelist())

    def test_existing_same_member_must_match_before_source_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "reports" / "push"
            archive_dir = root / "reports" / "archive"
            source = self._make_old(report_dir / "legacy.json")
            archive_dir.mkdir(parents=True)
            zip_path = archive_dir / "push-202605.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("legacy.json", source.read_bytes())
            with (
                mock.patch.object(
                    reports_rotate, "TARGETS", [
                        ("push", report_dir, "*.json")]),
                mock.patch.object(reports_rotate, "ARCHIVE_DIR", archive_dir),
                mock.patch.object(
                    reports_rotate.time, "time", return_value=FIXED_NOW_TS),
            ):
                result = reports_rotate.rotate(days=30, apply=True)
            self.assertEqual(1, result["groups"]["push"]["archived_deleted"])
            self.assertFalse(source.exists())
            with zipfile.ZipFile(zip_path, "r") as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(b"legacy.json", archive.read("legacy.json"))

    def test_conflicting_same_member_preserves_source_and_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "reports" / "push"
            archive_dir = root / "reports" / "archive"
            source = self._make_old(report_dir / "legacy.json")
            archive_dir.mkdir(parents=True)
            zip_path = archive_dir / "push-202605.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("legacy.json", b"older-different-content")
            before = zip_path.read_bytes()
            with (
                mock.patch.object(
                    reports_rotate, "TARGETS", [
                        ("push", report_dir, "*.json")]),
                mock.patch.object(reports_rotate, "ARCHIVE_DIR", archive_dir),
                mock.patch.object(
                    reports_rotate.time, "time", return_value=FIXED_NOW_TS),
            ):
                with self.assertRaisesRegex(RuntimeError, "conflicts"):
                    reports_rotate.rotate(days=30, apply=True)
            self.assertTrue(source.exists())
            self.assertEqual(before, zip_path.read_bytes())

    def test_atomic_replace_failure_never_deletes_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "reports" / "push"
            archive_dir = root / "reports" / "archive"
            source = self._make_old(report_dir / "legacy.json")
            with (
                mock.patch.object(
                    reports_rotate, "TARGETS", [
                        ("push", report_dir, "*.json")]),
                mock.patch.object(reports_rotate, "ARCHIVE_DIR", archive_dir),
                mock.patch.object(
                    reports_rotate.time, "time", return_value=FIXED_NOW_TS),
                mock.patch.object(
                    reports_rotate.os, "replace",
                    side_effect=OSError("replace denied")),
            ):
                with self.assertRaisesRegex(OSError, "replace denied"):
                    reports_rotate.rotate(days=30, apply=True)
            self.assertTrue(source.exists())
            self.assertFalse((archive_dir / "push-202605.zip").exists())
            self.assertEqual([], list(archive_dir.glob("*.tmp")))

    def test_corrupt_existing_zip_never_deletes_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "reports" / "push"
            archive_dir = root / "reports" / "archive"
            source = self._make_old(report_dir / "legacy.json")
            archive_dir.mkdir(parents=True)
            zip_path = archive_dir / "push-202605.zip"
            zip_path.write_bytes(b"not-a-zip")
            before = zip_path.read_bytes()
            with (
                mock.patch.object(
                    reports_rotate, "TARGETS", [
                        ("push", report_dir, "*.json")]),
                mock.patch.object(reports_rotate, "ARCHIVE_DIR", archive_dir),
                mock.patch.object(
                    reports_rotate.time, "time", return_value=FIXED_NOW_TS),
            ):
                with self.assertRaises(zipfile.BadZipFile):
                    reports_rotate.rotate(days=30, apply=True)
            self.assertTrue(source.exists())
            self.assertEqual(before, zip_path.read_bytes())


class PushArchiveAtomicTests(unittest.TestCase):
    @staticmethod
    def _payload(content: str) -> str:
        return json.dumps({
            "content": content,
            "ts": "2026-08-30 12:34:56",
            "cycle_count": 1,
        }, ensure_ascii=False)

    def test_invalid_t4_never_overwrites_canonical_or_latest(self):
        with tempfile.TemporaryDirectory() as temp:
            reports = Path(temp) / "reports"
            reports.mkdir()
            latest = reports / "v2-push-latest.md"
            canonical = reports / "v2-push-20260830-123456.md"
            latest.write_text("previous", encoding="utf-8")
            canonical.write_text("previous canonical", encoding="utf-8")
            with mock.patch.object(sys, "argv", [
                "push_archive.py", "--reports-dir", str(reports),
                "--json", self._payload("too short"),
            ]):
                with self.assertRaises(SystemExit) as raised:
                    push_archive.main()
            self.assertEqual(2, raised.exception.code)
            self.assertEqual("previous", latest.read_text(encoding="utf-8"))
            self.assertEqual(
                "previous canonical", canonical.read_text(encoding="utf-8"))

    def test_valid_content_atomically_updates_archive_and_latest(self):
        content = "第1轮\n📊 资产\n" + ("有效正文" * 100)
        with tempfile.TemporaryDirectory() as temp:
            reports = Path(temp) / "reports"
            with mock.patch.object(sys, "argv", [
                "push_archive.py", "--reports-dir", str(reports),
                "--json", self._payload(content),
            ]):
                with self.assertRaises(SystemExit) as raised:
                    push_archive.main()
            self.assertEqual(0, raised.exception.code)
            archived = reports / "v2-push-20260830-123456.md"
            latest = reports / "v2-push-latest.md"
            self.assertTrue(archived.read_text(encoding="utf-8").endswith(content))
            self.assertEqual(archived.read_bytes(), latest.read_bytes())
            self.assertEqual([], list(reports.glob("*.tmp")))

    def test_replace_failure_preserves_previous_latest_and_cleans_temp(self):
        content = "第1轮\n📊 资产\n" + ("有效正文" * 100)
        with tempfile.TemporaryDirectory() as temp:
            reports = Path(temp) / "reports"
            reports.mkdir()
            latest = reports / "v2-push-latest.md"
            latest.write_text("previous", encoding="utf-8")
            with (
                mock.patch.object(sys, "argv", [
                    "push_archive.py", "--reports-dir", str(reports),
                    "--json", self._payload(content),
                ]),
                mock.patch.object(
                    push_archive.os, "replace",
                    side_effect=OSError("replace denied")),
            ):
                with self.assertRaises(SystemExit) as raised:
                    push_archive.main()
            self.assertEqual(2, raised.exception.code)
            self.assertEqual("previous", latest.read_text(encoding="utf-8"))
            self.assertFalse(
                (reports / "v2-push-20260830-123456.md").exists())
            self.assertEqual([], list(reports.glob("*.tmp")))

    def test_latest_replace_failure_keeps_complete_timestamp_archive(self):
        content = "第1轮\n📊 资产\n" + ("有效正文" * 100)
        with tempfile.TemporaryDirectory() as temp:
            reports = Path(temp) / "reports"
            reports.mkdir()
            latest = reports / "v2-push-latest.md"
            latest.write_text("previous", encoding="utf-8")
            real_replace = os.replace

            def replace(source, destination):
                if Path(destination) == latest:
                    raise OSError("latest replace denied")
                return real_replace(source, destination)

            with (
                mock.patch.object(sys, "argv", [
                    "push_archive.py", "--reports-dir", str(reports),
                    "--json", self._payload(content),
                ]),
                mock.patch.object(push_archive.os, "replace", side_effect=replace),
            ):
                with self.assertRaises(SystemExit) as raised:
                    push_archive.main()
            self.assertEqual(2, raised.exception.code)
            archived = reports / "v2-push-20260830-123456.md"
            self.assertTrue(archived.read_text(encoding="utf-8").endswith(content))
            self.assertEqual("previous", latest.read_text(encoding="utf-8"))
            self.assertEqual([], list(reports.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
