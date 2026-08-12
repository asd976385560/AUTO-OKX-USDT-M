# -*- coding: utf-8 -*-
"""Public read-only ledger-autoheal and machine-contract regressions."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "collectors", "core"):
    path = str(ROOT / sub)
    if path not in sys.path:
        sys.path.insert(0, path)

import ledger_autoheal  # noqa: E402
from core import order_executor  # noqa: E402


CST = timezone(timedelta(hours=8))
SYM = "TEST-USDT-SWAP"
CLOSE_MS = int(datetime(2026, 8, 4, 10, 30, tzinfo=CST).timestamp() * 1000)
TRADE_SCHEMA = """
CREATE TABLE trade_cycles(
  cycle_id TEXT PRIMARY KEY, ts TEXT NOT NULL, mode TEXT, decision TEXT,
  n_orders INTEGER DEFAULT 0, equity REAL, note TEXT, raw TEXT
);
CREATE TABLE trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, ts TEXT NOT NULL,
  symbol TEXT NOT NULL, action TEXT NOT NULL, side TEXT, sz REAL,
  fill_px REAL, lev REAL, margin REAL, notional REAL, score_total INTEGER,
  reasoning TEXT, deviation TEXT, degradation TEXT, pnl REAL, raw TEXT
);
"""


def _make_db(root: Path) -> Path:
    path = root / "live_trades.db"
    con = sqlite3.connect(path)
    try:
        con.executescript(TRADE_SCHEMA)
        con.execute(
            "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,fill_px,lev,pnl) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("2026-08-04T10:00", "2026-08-04 10:00:00", SYM,
             "open", "short", 1.0, 100.0, 5.0, 0.0),
        )
        con.commit()
    finally:
        con.close()
    return path


def _close_fill() -> list[dict]:
    return [{
        "ordId": "ORD-1", "fillSz": "1", "fillPx": "101",
        "fillPnl": "-1", "fillTime": str(CLOSE_MS),
    }]


def _rows(path: Path) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT action,side,sz,pnl FROM trades ORDER BY id"
        ).fetchall()
    finally:
        con.close()


class PublicReadOnlyProducerTests(unittest.TestCase):
    def _dry_run(self, root: Path) -> dict:
        with mock.patch.object(
                ledger_autoheal.rec, "venue_positions", return_value={}), \
             mock.patch.object(
                ledger_autoheal.rec, "fetch_reduce_fills",
                return_value=_close_fill()), \
             mock.patch.object(
                ledger_autoheal.rec, "fetch_open_fills", return_value=[]), \
             mock.patch.object(
                ledger_autoheal, "active_runner", return_value=None):
            return ledger_autoheal.autoheal(
                "live", root, False, 3, None, request_id="READ-ONLY"
            )

    def test_exact_candidate_is_reported_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = _make_db(root)
            before = _rows(db)
            result = self._dry_run(root)
            self.assertEqual(_rows(db), before)
            self.assertFalse(result["applied"])
            self.assertTrue(result["blocking"])
            self.assertEqual(result["rc"], 1)
            self.assertEqual(result["request_id"], "READ-ONLY")
            self.assertEqual(result["contract_version"], 1)
            self.assertTrue(any(
                item.get("kind") == "GHOST-EXACT"
                for item in result["healed"]
            ))

    def test_apply_and_unrecorded_flags_fail_before_any_probe_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = _make_db(root)
            before = _rows(db)
            for apply, unrecorded in ((True, False), (False, True), (True, True)):
                with self.subTest(apply=apply, unrecorded=unrecorded), \
                     mock.patch.object(
                         ledger_autoheal, "active_runner"
                     ) as active_runner:
                    result = ledger_autoheal.autoheal(
                        "live", root, apply, 3, None,
                        enable_unrecorded=unrecorded,
                        request_id="DENIED",
                    )
                active_runner.assert_not_called()
                self.assertEqual(result["rc"], 2)
                self.assertTrue(result["blocking"])
                self.assertFalse(result["applied"])
                self.assertIn("permanently read-only", result["error"])
                self.assertEqual(_rows(db), before)

    def test_cli_apply_is_structured_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = _make_db(root)
            before = _rows(db)
            output = io.StringIO()
            argv = [
                "ledger_autoheal.py", "--profile", "live",
                "--db-root", str(root), "--apply",
            ]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(output):
                rc = ledger_autoheal.main()
            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 2)
            self.assertEqual(payload["status"], "error")
            self.assertTrue(payload["blocking"])
            self.assertEqual(_rows(db), before)

    def test_atomic_json_output_matches_stdout_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_db(root)
            out_file = root / "result.json"
            output = io.StringIO()
            argv = [
                "ledger_autoheal.py", "--profile", "live",
                "--db-root", str(root), "--request-id", "ATOMIC",
                "--json-out", str(out_file),
            ]
            with mock.patch.object(
                    ledger_autoheal.rec, "venue_positions", return_value={}), \
                 mock.patch.object(
                    ledger_autoheal.rec, "fetch_reduce_fills",
                    return_value=_close_fill()), \
                 mock.patch.object(
                    ledger_autoheal.rec, "fetch_open_fills", return_value=[]), \
                 mock.patch.object(
                    ledger_autoheal, "active_runner", return_value=None), \
                 mock.patch.object(sys, "argv", argv), redirect_stdout(output):
                rc = ledger_autoheal.main()
            stdout_payload = json.loads(output.getvalue())
            file_payload = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(rc, 1)
            self.assertEqual(file_payload, stdout_payload)
            self.assertEqual(file_payload["request_id"], "ATOMIC")


class PublicReadOnlyClientTests(unittest.TestCase):
    @staticmethod
    def _arg(argv: list[str], name: str) -> str:
        return argv[argv.index(name) + 1]

    def _producer(self, argv: list[str], *, corrupt: bool = False):
        out_path = Path(self._arg(argv, "--json-out"))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if corrupt:
            out_path.write_text("{broken", encoding="utf-8")
            return mock.Mock(returncode=0, stdout="ignored", stderr="")
        payload = {
            "contract_version": 1,
            "request_id": self._arg(argv, "--request-id"),
            "profile": self._arg(argv, "--profile"),
            "cycle": self._arg(argv, "--self-cycle"),
            "db_root": str(Path(self._arg(argv, "--db-root")).resolve()),
            "status": "ok",
            "applied": False,
            "p0": False,
            "blocking": False,
            "findings": [],
            "healed": [],
            "needs_human": [],
            "rc": 0,
        }
        out_path.write_text(json.dumps(payload), encoding="utf-8")
        return mock.Mock(returncode=0, stdout="ignored", stderr="")

    def test_executor_never_forwards_write_flags_from_environment(self) -> None:
        captured: list[str] = []

        def run(argv, **_kwargs):
            captured[:] = argv
            return self._producer(argv)

        with mock.patch.dict(os.environ, {
                "OKX_LEDGER_AUTOHEAL_APPLY": "1",
                "OKX_LEDGER_AUTOHEAL_UNRECORDED": "1",
        }, clear=False), \
             mock.patch("subprocess.run", side_effect=run), \
             mock.patch("os.path.exists", return_value=True):
            result = order_executor._try_autoheal_ledger(
                "live", ROOT / "db", "2026-08-12T10:00"
            )
        self.assertFalse(result["blocking"])
        self.assertFalse(result["applied"])
        self.assertNotIn("--apply", captured)
        self.assertNotIn("--enable-unrecorded", captured)
        self.assertIn("--json-out", captured)

    def test_missing_or_corrupt_contract_fails_closed(self) -> None:
        with mock.patch("subprocess.run",
                        side_effect=lambda argv, **kw:
                        self._producer(argv, corrupt=True)), \
             mock.patch("os.path.exists", return_value=True):
            result = order_executor._try_autoheal_ledger(
                "live", ROOT / "db", "2026-08-12T10:00"
            )
        self.assertTrue(result["blocking"])
        self.assertEqual(result["status"], "contract_invalid")
        self.assertEqual(result["rc"], 2)


if __name__ == "__main__":
    unittest.main()
