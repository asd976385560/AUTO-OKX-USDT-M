"""Isolated repair_queue ownership regressions.

No production database, network, Agent, scheduler, or push side effects.
"""
from __future__ import annotations

import ast
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "collectors"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collection_monitor  # noqa: E402
import ledger_invariants  # noqa: E402


SCHEMA = """
CREATE TABLE repair_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  check_name TEXT NOT NULL,
  issue TEXT,
  fix_action TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('open','pending','closed','resolved')),
  created_utc TEXT,
  closed_at TEXT,
  closed_by TEXT,
  resolution TEXT
);
"""


class RepairQueueFamilyOwnershipTests(unittest.TestCase):
    def _connect(self, tmp: str) -> sqlite3.Connection:
        con = sqlite3.connect(Path(tmp) / "account.db")
        con.executescript(SCHEMA)
        return con

    @staticmethod
    def _insert(con: sqlite3.Connection, name: str) -> None:
        con.execute(
            "INSERT INTO repair_queue(ts,check_name,issue,status,created_utc) "
            "VALUES('2026-08-09 20:00:00',?,'old','pending',"
            "'2026-08-09 20:00:00')",
            (name,),
        )

    def test_monitor_does_not_close_experience_owned_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            con = self._connect(tmp)
            experience = (
                "ledger_invariant:experience_position:live:BTC-USDT-SWAP:long"
            )
            negative = (
                "ledger_invariant:negative_net:live:ETH-USDT-SWAP:short"
            )
            duplicate = (
                "ledger_invariant:duplicate_intent:live:cycle:X:long"
            )
            execution = (
                "ledger_invariant:execution_intent:live:cycle:Y:long"
            )
            for name in (experience, negative, duplicate):
                self._insert(con, name)
            con.commit()

            findings = [
                {
                    "check_name": negative,
                    "issue": "negative still active",
                    "fix_action": "review",
                },
                {
                    "check_name": execution,
                    "issue": "intent active",
                    "fix_action": "review",
                },
            ]
            con.execute("BEGIN IMMEDIATE")
            result = collection_monitor.sync_monitor_repair_queue(
                con, findings, "2026-08-09 21:00:00")
            con.commit()

            rows = {
                row[0]: row[1:]
                for row in con.execute(
                    "SELECT check_name,status,closed_by,resolution "
                    "FROM repair_queue ORDER BY id"
                )
            }
            self.assertEqual(rows[experience][0], "pending")
            self.assertEqual(rows[negative][0], "pending")
            self.assertEqual(rows[duplicate][0:2], (
                "closed", "collection_monitor"))
            self.assertEqual(rows[execution][0], "pending")
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(result["closed"], 1)

            con.execute("BEGIN IMMEDIATE")
            second = collection_monitor.sync_monitor_repair_queue(
                con, findings, "2026-08-09 21:01:00")
            con.commit()
            self.assertEqual(second["inserted"], 0)
            self.assertEqual(second["closed"], 0)
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM repair_queue WHERE check_name=?",
                    (execution,),
                ).fetchone()[0],
                1,
            )
            con.close()

    def test_prefix_mismatch_fails_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            con = self._connect(tmp)
            con.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "family mismatch"):
                ledger_invariants.sync_repair_queue(
                    con,
                    family_prefix="ledger_invariant:negative_net:",
                    findings=[{
                        "check_name": (
                            "ledger_invariant:execution_intent:live:c:s:long"
                        ),
                        "issue": "wrong family",
                    }],
                    ts="2026-08-09 21:00:00",
                )
            con.rollback()
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM repair_queue").fetchone()[0],
                0,
            )
            con.close()

    def test_jobb_sync_call_site_records_its_own_closer(self) -> None:
        """jobb 是关单量最大的调用方，其同步调用点必须显式标注归因。"""
        source = (ROOT / "scripts" / "jobb_live_account_check.py").read_text(
            encoding="utf-8")
        calls = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sync_repair_queue"
        ]
        self.assertEqual(len(calls), 1)
        kwargs = {
            kw.arg: getattr(kw.value, "value", None)
            for kw in calls[0].keywords if kw.arg
        }
        self.assertEqual(kwargs.get("closed_by"), "jobb_live_account_check")
        resolution = kwargs.get("resolution")
        self.assertIsInstance(resolution, str)
        self.assertTrue(resolution.strip())
        self.assertNotEqual(resolution, "invariant healed")

    def test_full_invariant_pass_records_its_own_closer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            con = self._connect(tmp)
            name = "ledger_invariant:experience_schema:live"
            self._insert(con, name)
            con.commit()
            con.execute("BEGIN IMMEDIATE")
            result = ledger_invariants.sync_repair_queue(
                con,
                family_prefix="ledger_invariant:",
                findings=[],
                ts="2026-08-09 21:00:00",
            )
            con.commit()
            row = con.execute(
                "SELECT status,closed_by FROM repair_queue WHERE check_name=?",
                (name,),
            ).fetchone()
            self.assertEqual(result, {"inserted": 0, "closed": 1})
            self.assertEqual(row, ("closed", "ledger_invariants"))
            con.close()


if __name__ == "__main__":
    unittest.main()
