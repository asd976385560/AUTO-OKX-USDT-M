from __future__ import annotations

import json
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from collectors import _dispatch_nudge, analyst_writer, trades_writer


class WriterDbRootIsolationTests(unittest.TestCase):
    @staticmethod
    def _empty_trade_db(db_root: Path, profile: str = "demo") -> Path:
        db_root.mkdir(parents=True, exist_ok=True)
        db = db_root / f"{profile}_trades.db"
        con = sqlite3.connect(db)
        try:
            con.execute(
                "CREATE TABLE trades (cycle_id TEXT, symbol TEXT, action TEXT, "
                "side TEXT, sz REAL, fill_px REAL, pnl REAL, raw TEXT)"
            )
            con.commit()
        finally:
            con.close()
        return db

    def test_okx_db_root_routes_writer_primary_and_auxiliary_databases(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "OKX_DB_ROOT": tmp,
                # An explicit runtime root must remain the isolation boundary,
                # even if a legacy per-DB override is present.
                "OKX_ACCOUNT_DB": str(Path(tmp).parent / "wrong-account.db"),
                "OKX_ANALYSIS_DB": str(Path(tmp).parent / "wrong-analysis.db"),
            },
        ):
            root = Path(tmp).resolve()
            self.assertEqual(analyst_writer._runtime_db_root(), root)
            self.assertEqual(
                trades_writer._trade_db_path("live"), root / "live_trades.db"
            )
            self.assertEqual(
                trades_writer._trade_db_path("demo"), root / "demo_trades.db"
            )
            self.assertEqual(
                trades_writer._runtime_db_path(
                    "account.db", root, "OKX_ACCOUNT_DB"
                ),
                root / "account.db",
            )
            self.assertEqual(
                trades_writer._runtime_db_path(
                    "analysis.db", root, "OKX_ANALYSIS_DB"
                ),
                root / "analysis.db",
            )

    def test_custom_root_nudge_is_rejected_before_cron_or_spawn(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            _dispatch_nudge.os.environ,
            {"OKX_DISPATCH_NUDGE": "1"},
        ):
            _dispatch_nudge.os.environ.pop("OKX_TRIGGER_DRYRUN", None)
            with (
                mock.patch.object(
                    _dispatch_nudge, "_dispatcher_cron_enabled"
                ) as cron_enabled,
                mock.patch.object(_dispatch_nudge, "_default_spawn") as spawn,
            ):
                result = _dispatch_nudge.nudge(
                    "isolated-test", db_root=Path(tmp)
                )

        self.assertEqual(
            result, {"nudged": False, "reason": "non_production_db_root"}
        )
        cron_enabled.assert_not_called()
        spawn.assert_not_called()

    def test_analyst_cli_passes_custom_root_to_write_and_nudge(self):
        payload = {
            "cycle_id": "2026-08-04T12:00",
            "ts": "2026-08-04 12:01:00",
            "mode": "full",
            "status": "skipped",
            "decision_protocol": "decision_card_v1",
            "market_summary": None,
            "signals": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            argv = [
                "analyst_writer.py",
                "--input-file",
                str(receipt),
                "--db-root",
                str(root),
            ]
            with (
                mock.patch.object(analyst_writer.sys, "argv", argv),
                mock.patch.object(
                    analyst_writer,
                    "write_analysis",
                    return_value={"ok": True, "cycle_id": payload["cycle_id"]},
                ) as write_analysis,
                mock.patch.object(analyst_writer._nudge_mod, "nudge") as nudge,
                mock.patch("builtins.print"),
            ):
                rc = analyst_writer.main()

        self.assertEqual(rc, 0)
        write_analysis.assert_called_once_with(
            payload, db_path=root / "analysis.db"
        )
        nudge.assert_called_once_with("analyst_writer", db_root=root)

    def test_trades_commit_passes_target_root_to_aux_write_and_nudge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "live_trades.db"
            target.touch()
            with (
                mock.patch.object(
                    trades_writer,
                    "write_trades",
                    return_value={"ok": True, "writer_commit_at": "2026-08-04 12:00:00"},
                ),
                mock.patch.object(
                    trades_writer, "write_experiences", return_value={"exp": 0}
                ) as experiences,
                mock.patch.object(trades_writer._nudge_mod, "nudge") as nudge,
            ):
                result = trades_writer.commit_receipt(
                    {}, "live", db_path=target
                )

        self.assertTrue(result["ok"], result)
        experiences.assert_called_once_with(
            {"_profile": "live"},
            "live",
            "2026-08-04 12:00:00",
            db_root=root,
        )
        nudge.assert_called_once_with("trades_writer:live", db_root=root)

    def test_journal_dry_scan_reports_unwind_without_wal_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_root = root / "db"
            db = self._empty_trade_db(db_root)
            journal = root / "exec_demo.jsonl"
            journal.write_text(json.dumps({
                "profile": "demo",
                "cycle_id": "2026-08-04T12:00",
                "ts": "2026-08-04 12:01:00",
                "unwind": True,
                "trade": {
                    "ordId": "ORD-UNWIND",
                    "symbol": "TEST-USDT-SWAP",
                    "action": "close",
                    "side": "long",
                    "sz": "1",
                    "fill_px": "100",
                },
            }) + "\n", encoding="utf-8")
            before = db.read_bytes()
            args = SimpleNamespace(
                from_journal=str(journal), profile="demo",
                db_root=str(db_root), ordid=None, replay_dry_run=True,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                rc = trades_writer.replay_from_journal(args)

            payload = json.loads(output.getvalue())
            row = payload["plan"]["2026-08-04T12:00"][0]
            self.assertEqual(rc, 0)
            self.assertTrue(row["unwind"])
            self.assertEqual(row["ordId"], "ORD-UNWIND")
            self.assertEqual(db.read_bytes(), before)
            self.assertFalse(Path(str(db) + "-wal").exists())
            self.assertFalse(Path(str(db) + "-shm").exists())

    def test_journal_apply_without_ordid_is_hard_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_root = root / "db"
            self._empty_trade_db(db_root)
            journal = root / "exec_demo.jsonl"
            journal.write_text("{}\n", encoding="utf-8")
            args = SimpleNamespace(
                from_journal=str(journal), profile="demo",
                db_root=str(db_root), ordid=None, replay_dry_run=False,
            )
            output = io.StringIO()
            with redirect_stdout(output), mock.patch.object(
                trades_writer, "write_trades"
            ) as write_trades:
                rc = trades_writer.replay_from_journal(args)

            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 2)
            self.assertFalse(payload["ok"])
            self.assertIn("--ordid", payload["error"])
            write_trades.assert_not_called()

    def test_targeted_unwind_apply_returns_structured_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_root = root / "db"
            self._empty_trade_db(db_root)
            journal = root / "exec_demo.jsonl"
            journal.write_text(json.dumps({
                "profile": "demo",
                "cycle_id": "2026-08-04T12:00",
                "ts": "2026-08-04 12:01:00",
                "unwind": True,
                "trade": {
                    "ordId": "ORD-UNWIND",
                    "symbol": "TEST-USDT-SWAP",
                    "action": "close",
                    "side": "long",
                    "sz": "1",
                },
            }) + "\n", encoding="utf-8")
            args = SimpleNamespace(
                from_journal=str(journal), profile="demo",
                db_root=str(db_root), ordid="ORD-UNWIND",
                replay_dry_run=False,
            )
            output = io.StringIO()
            with redirect_stdout(output), mock.patch.object(
                trades_writer, "write_trades"
            ) as write_trades:
                rc = trades_writer.replay_from_journal(args)

            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 3)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["blocked_unwinds"][0]["ordId"], "ORD-UNWIND"
            )
            write_trades.assert_not_called()

    def test_targeted_apply_requires_unique_journal_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_root = root / "db"
            self._empty_trade_db(db_root)
            journal = root / "exec_demo.jsonl"
            record = {
                "profile": "demo",
                "cycle_id": "2026-08-04T12:00",
                "ts": "2026-08-04 12:01:00",
                "trade": {
                    "ordId": "ORD-DUPLICATE",
                    "symbol": "TEST-USDT-SWAP",
                    "action": "open",
                    "side": "long",
                    "sz": "1",
                },
            }
            journal.write_text(
                json.dumps(record) + "\n" + json.dumps(record) + "\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                from_journal=str(journal), profile="demo",
                db_root=str(db_root), ordid="ORD-DUPLICATE",
                replay_dry_run=False,
            )
            output = io.StringIO()
            with redirect_stdout(output), mock.patch.object(
                trades_writer, "write_trades"
            ) as write_trades:
                rc = trades_writer.replay_from_journal(args)

            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 3)
            self.assertEqual(payload["matched_records"], 2)
            self.assertIn("唯一命中", payload["error"])
            write_trades.assert_not_called()


if __name__ == "__main__":
    unittest.main()
