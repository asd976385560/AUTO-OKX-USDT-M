# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import qq_push  # noqa: E402
import qq_push_raw  # noqa: E402


class QqGatewayTransportTests(unittest.TestCase):
    def setUp(self):
        for name in ("_NODE", "_MJS"):
            guard = mock.patch.object(qq_push_raw, name, "test-runtime")
            guard.start()
            self.addCleanup(guard.stop)

    def test_gateway_uses_stdin_complete_content_and_one_process(self):
        content = "完整简报\n" * 1000
        completed = mock.Mock(returncode=0, stdout='{"messageId":"receipt-1"}', stderr="")
        with mock.patch.dict(qq_push_raw.os.environ, {"OKX_QQ_TRANSPORT": "gateway"}), \
                mock.patch.object(qq_push_raw.subprocess, "run", return_value=completed) as run:
            ok, _ = qq_push_raw.push(content, ":".join(('group', 'TEST_GROUP')))
        self.assertTrue(ok)
        run.assert_called_once()
        self.assertEqual(qq_push_raw._GATEWAY_MJS, run.call_args.args[0][1])
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(content, payload["content"])
        self.assertEqual(":".join(('group', 'TEST_GROUP')), payload["target"])
        self.assertNotIn(content, run.call_args.args[0])

    def test_uncertain_gateway_outcome_never_falls_back_to_cli(self):
        completed = mock.Mock(returncode=3, stdout="", stderr='{"uncertainDelivery":true}')
        with mock.patch.dict(qq_push_raw.os.environ, {"OKX_QQ_TRANSPORT": "gateway"}), \
                mock.patch.object(qq_push_raw.subprocess, "run", return_value=completed) as run:
            ok, output = qq_push_raw.push("payload", ":".join(('c2c', 'TEST_USER')))
        self.assertFalse(ok)
        run.assert_called_once()
        self.assertIn(qq_push_raw.UNCERTAIN_DELIVERY_MARKER, output)


class QqPushUncertainDeliveryTests(unittest.TestCase):
    def test_main_exposes_each_non_send_claim_result(self):
        cases = (
            (qq_push.CLAIM_DUPLICATE_SENT, 0),
            (
                qq_push.CLAIM_DUPLICATE_UNCERTAIN,
                qq_push.UNCERTAIN_DELIVERY_EXIT_CODE,
            ),
            (
                qq_push.CLAIM_PENDING_IN_FLIGHT,
                qq_push.PENDING_IN_FLIGHT_EXIT_CODE,
            ),
            (
                qq_push.CLAIM_STALE_PENDING,
                qq_push.STALE_PENDING_EXIT_CODE,
            ),
        )
        for claim_result, expected_rc in cases:
            with self.subTest(claim_result=claim_result):
                with (
                    mock.patch.object(sys, "argv", [
                        "qq_push.py", "--message", "payload",
                        "--dedupe-key", "push:fixture",
                    ]),
                    mock.patch.object(
                        qq_push, "_read_content_once", return_value="payload"),
                    mock.patch.object(
                        qq_push, "_dedupe_key",
                        return_value=(
                            "key", "hash", "push:fixture", "default")),
                    mock.patch.object(
                        qq_push, "_claim", return_value=claim_result),
                    mock.patch.object(qq_push.runpy, "run_path") as raw,
                ):
                    self.assertEqual(expected_rc, qq_push.main())
                raw.assert_not_called()

    def test_wrapper_marks_exit_three_as_uncertain_delivery(self):
        with (
            mock.patch.object(sys, "argv", [
                "qq_push.py", "--message", "payload",
                "--dedupe-key", "push:fixture",
            ]),
            mock.patch.object(qq_push, "_read_content_once", return_value="payload"),
            mock.patch.object(
                qq_push, "_dedupe_key",
                return_value=("key", "hash", "push:fixture", "default"),
            ),
            mock.patch.object(
                qq_push, "_claim", return_value=qq_push.CLAIM_ACQUIRED),
            mock.patch.object(
                qq_push.runpy, "run_path", side_effect=SystemExit(3)),
            mock.patch.object(qq_push, "_mark") as mark,
        ):
            with self.assertRaises(SystemExit) as raised:
                qq_push.main()
        self.assertEqual(3, raised.exception.code)
        mark.assert_called_once_with(
            "key", "uncertain_delivery", "push:fixture", "default", 3)

    def test_uncertain_claim_is_terminal_and_blocks_manual_resend(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "dedupe.db"
            event_log = root / "events.jsonl"
            con = sqlite3.connect(db)
            con.execute(qq_push.SENT_TABLE_DDL)
            con.execute(
                "INSERT INTO sent(k,content_hash,status,first_seen,updated_at,preview) "
                "VALUES(?,?,?,?,?,?)",
                (
                    "key", "hash", "uncertain_delivery",
                    "2026-08-30 04:09:52", "2026-08-30 04:09:52", "fixture",
                ),
            )
            con.commit()
            con.close()
            with (
                mock.patch.object(qq_push, "DB", db),
                mock.patch.object(qq_push, "EVENT_LOG", event_log),
            ):
                claimed = qq_push._claim(
                    "key", "hash", "fixture", "push:fixture", "default")
            self.assertEqual(qq_push.CLAIM_DUPLICATE_UNCERTAIN, claimed)
            con = sqlite3.connect(db)
            status = con.execute(
                "SELECT status FROM sent WHERE k='key'").fetchone()[0]
            con.close()
            self.assertEqual("uncertain_delivery", status)

    def test_existing_claim_states_are_distinct_and_stale_is_not_reclaimed(self):
        cases = (
            ("sent", "2026-08-30 04:09:52", qq_push.CLAIM_DUPLICATE_SENT),
            (
                "uncertain_delivery", "2026-08-30 04:09:52",
                qq_push.CLAIM_DUPLICATE_UNCERTAIN,
            ),
            (
                "pending", "2026-08-30 04:09:52",
                qq_push.CLAIM_PENDING_IN_FLIGHT,
            ),
            (
                "pending", "2026-08-30 03:00:00",
                qq_push.CLAIM_STALE_PENDING,
            ),
        )
        for status, updated_at, expected in cases:
            with self.subTest(status=status, updated_at=updated_at):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    db = root / "dedupe.db"
                    con = sqlite3.connect(db)
                    con.execute(qq_push.SENT_TABLE_DDL)
                    con.execute(
                        "INSERT INTO sent(k,content_hash,status,first_seen,"
                        "updated_at,preview) VALUES(?,?,?,?,?,?)",
                        (
                            "key", "old-hash", status,
                            "2026-08-30 03:00:00", updated_at, "fixture",
                        ),
                    )
                    con.commit()
                    con.close()
                    with (
                        mock.patch.object(qq_push, "DB", db),
                        mock.patch.object(
                            qq_push, "EVENT_LOG", root / "events.jsonl"),
                        mock.patch.object(
                            qq_push, "_now",
                            return_value="2026-08-30 04:10:00"),
                    ):
                        observed = qq_push._claim(
                            "key", "new-hash", "new", "push:fixture",
                            "default",
                        )
                    self.assertEqual(expected, observed)
                    con = sqlite3.connect(db)
                    row = con.execute(
                        "SELECT content_hash,status,updated_at FROM sent "
                        "WHERE k='key'"
                    ).fetchone()
                    con.close()
                    self.assertEqual(("old-hash", status, updated_at), row)

    def test_mark_requires_one_row_and_reads_back_in_same_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "dedupe.db"
            con = sqlite3.connect(db)
            con.execute(qq_push.SENT_TABLE_DDL)
            con.execute(
                "INSERT INTO sent(k,status,updated_at) VALUES(?,?,?)",
                ("key", "pending", "2026-08-30 04:00:00"),
            )
            con.commit()
            con.close()
            with (
                mock.patch.object(qq_push, "DB", db),
                mock.patch.object(qq_push, "EVENT_LOG", root / "events.jsonl"),
                mock.patch.object(
                    qq_push, "_now", return_value="2026-08-30 04:10:00"),
            ):
                qq_push._mark(
                    "key", "sent", "push:fixture", "default", 0)
                with self.assertRaisesRegex(RuntimeError, "expected one row"):
                    qq_push._mark(
                        "missing", "sent", "push:fixture", "default", 0)
            con = sqlite3.connect(db)
            row = con.execute(
                "SELECT status,updated_at FROM sent WHERE k='key'"
            ).fetchone()
            con.close()
            self.assertEqual(("sent", "2026-08-30 04:10:00"), row)


if __name__ == "__main__":
    unittest.main()
