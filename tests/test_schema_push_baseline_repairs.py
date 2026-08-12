# -*- coding: utf-8 -*-
"""Schema、推送前置归档和 OpenClaw 基线收口的独立回归。"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import export_schema  # noqa: E402
import push_pipeline  # noqa: E402
import qq_push  # noqa: E402
import schema_drift_check  # noqa: E402


QQ_SCHEMA = """
CREATE TABLE sent (
    k TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','sent','failed')),
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    preview TEXT
);
CREATE INDEX idx_sent_status_updated ON sent(status, updated_at);
"""


def _create_db(path: Path, ddl: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(ddl)
        con.commit()
    finally:
        con.close()


class SchemaContractTests(unittest.TestCase):
    def test_export_catalog_formally_includes_qq_dedupe(self):
        self.assertIn("qq_push_dedupe.db", export_schema.DBS)
        self.assertIn("qq_push_dedupe.db", schema_drift_check.DBS)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_db(root / "qq_push_dedupe.db", QQ_SCHEMA)
            schema_path = root / "schema.sql"
            export_schema.export_schema(
                root,
                schema_path,
                db_names=("qq_push_dedupe.db",),
                exported_at="2026-07-28 00:00:00",
            )
            text = schema_path.read_text(encoding="utf-8")
            self.assertIn("-- 数据库: qq_push_dedupe.db", text)
            self.assertIn("CREATE TABLE sent", text)
            self.assertIn("CREATE INDEX idx_sent_status_updated", text)

    def test_full_table_constraints_and_indexes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_db(root / "qq_push_dedupe.db", QQ_SCHEMA)
            schema_path = root / "schema.sql"
            export_schema.export_schema(
                root,
                schema_path,
                db_names=("qq_push_dedupe.db",),
                exported_at="2026-07-28 00:00:00",
            )
            with mock.patch.object(
                schema_drift_check, "DBS", ("qq_push_dedupe.db",)
            ):
                declared = schema_drift_check.parse_schema_sql(schema_path)
                live = schema_drift_check.read_live_schema(root)
                self.assertEqual(
                    schema_drift_check.collect_drifts(declared, live), []
                )

    def test_constraint_and_index_definition_drift_are_both_detected(self):
        drifted_schema = """
CREATE TABLE sent (
    k TEXT PRIMARY KEY,
    content_hash TEXT,
    status TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    preview TEXT
);
CREATE INDEX idx_sent_status_updated ON sent(updated_at, status);
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_db(root / "qq_push_dedupe.db", drifted_schema)
            schema_path = root / "schema.sql"
            schema_path.write_text(
                "-- 数据库: qq_push_dedupe.db\n" + QQ_SCHEMA,
                encoding="utf-8",
            )
            with mock.patch.object(
                schema_drift_check, "DBS", ("qq_push_dedupe.db",)
            ):
                drifts = schema_drift_check.collect_drifts(
                    schema_drift_check.parse_schema_sql(schema_path),
                    schema_drift_check.read_live_schema(root),
                )
            self.assertTrue(any("完整表定义" in item for item in drifts))
            self.assertTrue(any("完整索引定义" in item for item in drifts))

    def test_qq_connection_explicitly_uses_normal_synchronous(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(qq_push, "DB", Path(tmp) / "dedupe.db"):
                con = qq_push._connect()
                try:
                    self.assertEqual(
                        con.execute("PRAGMA synchronous").fetchone()[0], 1
                    )
                    self.assertEqual(
                        con.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                        "wal",
                    )
                finally:
                    con.close()


class PushArchiveOrderingTests(unittest.TestCase):
    CONTENT = "第1轮\n📊 资产\n" + ("完整归档正文" * 80)

    def _run_pipeline(
        self,
        root: Path,
        *,
        archive_rc: int = 0,
        send_rc: int = 0,
        send_exception: Exception | None = None,
    ) -> tuple[dict, list[str], Path]:
        calls: list[str] = []
        archive_path = root / "archive.md"

        def fake_run(script: str, args: list, stdin_text: str | None = None):
            name = Path(script).name
            calls.append(name)
            if name == "render_push_report.py":
                content_path = Path(args[args.index("--out-file") + 1])
                content_path.write_text(self.CONTENT, encoding="utf-8")
                return 0, json.dumps({"bytes": len(self.CONTENT), "title": "test"}), ""
            if name == "validate_push_format.py":
                return 0, json.dumps({"char_count": len(self.CONTENT)}), ""
            if name == "push_archive.py":
                archive_path.write_text(
                    "# archived\n\n" + self.CONTENT, encoding="utf-8"
                )
                result = {
                    "ok": True,
                    "path": str(archive_path),
                    "bytes": archive_path.stat().st_size,
                    "degraded": archive_rc != 0,
                }
                return archive_rc, json.dumps(result), "degraded" if archive_rc else ""
            if name == "qq_push.py":
                if send_exception is not None:
                    raise send_exception
                return send_rc, "", "send failed" if send_rc else ""
            if name == "system_state_writer.py":
                return 0, "{}", ""
            raise AssertionError(f"unexpected script: {name}")

        builder = types.SimpleNamespace(build=lambda _root, _cycle: {"trades": {}})
        with (
            mock.patch.object(push_pipeline, "WORK", root / "work"),
            mock.patch.object(push_pipeline, "REPORT_DIR", root / "reports"),
            mock.patch.object(push_pipeline, "RUNLOG", root / "runlog.jsonl"),
            mock.patch.object(push_pipeline, "_load_build", return_value=builder),
            mock.patch.object(push_pipeline, "_run", side_effect=fake_run),
            mock.patch.object(push_pipeline, "_finish", side_effect=lambda rep: rep),
        ):
            result = push_pipeline.run(
                "2026-07-28T12:00", str(root / "db"), no_send=False
            )
        return result, calls, archive_path

    def test_archive_hard_check_completes_before_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls, archive_path = self._run_pipeline(Path(tmp))
            self.assertLess(
                calls.index("push_archive.py"), calls.index("qq_push.py")
            )
            self.assertTrue(result["steps"]["archive"]["hard_check"])
            self.assertTrue(archive_path.read_text(encoding="utf-8").endswith(self.CONTENT))

    def test_archive_hard_check_failure_blocks_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls, _ = self._run_pipeline(Path(tmp), archive_rc=2)
            self.assertNotIn("qq_push.py", calls)
            self.assertEqual(result["fatal"], "archive_hard_check_failed")
            self.assertFalse(result["ok"])

    def test_send_failure_keeps_completed_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls, archive_path = self._run_pipeline(
                Path(tmp), send_rc=1
            )
            self.assertIn("qq_push.py", calls)
            self.assertEqual(result["send_status"], "failed")
            self.assertFalse(result["ok"])
            self.assertEqual(result["fatal"], "send_failed")
            self.assertTrue(result["steps"]["archive"]["hard_check"])
            self.assertTrue(archive_path.exists())
            self.assertTrue(archive_path.read_text(encoding="utf-8").endswith(self.CONTENT))

    def test_send_exception_keeps_archive_and_records_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls, archive_path = self._run_pipeline(
                Path(tmp), send_exception=TimeoutError("simulated")
            )
            self.assertIn("qq_push.py", calls)
            self.assertEqual(result["send_status"], "failed")
            self.assertFalse(result["ok"])
            self.assertEqual(result["fatal"], "send_failed")
            self.assertIn("TimeoutError", result["steps"]["send"]["err"])
            self.assertTrue(archive_path.exists())


if __name__ == "__main__":
    unittest.main()
