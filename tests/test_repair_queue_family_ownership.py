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

    def test_experience_position_check_defers_newer_trade_than_snapshot(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE trade_experiences("
            "profile TEXT,action TEXT,status TEXT,symbol TEXT,side TEXT,"
            "remaining_sz REAL)"
        )
        con.execute(
            "INSERT INTO trade_experiences VALUES"
            "('live','open','open','HYPE-USDT-SWAP','long',23)"
        )
        deferred = []
        findings = ledger_invariants.experience_position_findings(
            con,
            "live",
            {("HYPE-USDT-SWAP", "long"): 45.0},
            snapshot_ts="2026-08-20 23:30:39",
            latest_trade_ts={
                ("HYPE-USDT-SWAP", "long"): "2026-08-20 23:35:51",
            },
            deferred=deferred,
        )
        self.assertEqual(findings, [])
        self.assertEqual(len(deferred), 1)
        self.assertEqual(
            deferred[0]["reason"],
            "position_snapshot_precedes_latest_trade",
        )
        con.close()

    def test_experience_position_mismatch_remains_after_fresh_snapshot(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE trade_experiences("
            "profile TEXT,action TEXT,status TEXT,symbol TEXT,side TEXT,"
            "remaining_sz REAL)"
        )
        con.execute(
            "INSERT INTO trade_experiences VALUES"
            "('live','open','open','HYPE-USDT-SWAP','long',23)"
        )
        deferred = []
        findings = ledger_invariants.experience_position_findings(
            con,
            "live",
            {("HYPE-USDT-SWAP", "long"): 45.0},
            snapshot_ts="2026-08-20 23:40:00",
            latest_trade_ts={
                ("HYPE-USDT-SWAP", "long"): "2026-08-20 23:35:51",
            },
            deferred=deferred,
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "experience_position_mismatch")
        self.assertEqual(deferred, [])
        con.close()

    # 2026-09-11：UNRECORDED 方向（经验剩余 < 实仓）的行在交易所仓位消失后
    # 不得被当作"自愈"关掉（09-03 ENS、09-07 SOXL 因此整笔漏记）。
    ENS_ROW = "ledger_invariant:experience_position:live:ENS-USDT-SWAP:short"

    def _experience_queue(self, tmp: str) -> sqlite3.Connection:
        con = self._connect(tmp)
        con.execute(
            "CREATE TABLE trade_experiences("
            "profile TEXT,ts TEXT,symbol TEXT,side TEXT,action TEXT,"
            "status TEXT,remaining_sz REAL)"
        )
        con.execute(
            "INSERT INTO repair_queue(ts,check_name,issue,status,created_utc) "
            "VALUES('2026-09-03 19:15:07',?,?,'pending','2026-09-03 19:15:07')",
            (self.ENS_ROW,
             "[live] ENS-USDT-SWAP short 经验剩余=0，OKX 实仓=284"),
        )
        con.commit()
        return con

    def _sync_experience(self, con: sqlite3.Connection, ts: str) -> dict:
        con.execute("BEGIN IMMEDIATE")
        result = ledger_invariants.sync_repair_queue(
            con,
            family_prefix="ledger_invariant:experience_position:live:",
            findings=[],
            ts=ts,
            closed_by="jobb_live_account_check",
            resolution="jobb live account check invariant healed",
            hold=ledger_invariants.hold_unrecorded_vanished,
        )
        con.commit()
        return result

    def test_vanished_unrecorded_row_stays_pending_with_one_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = self._experience_queue(tmp)
            for ts in ("2026-09-03 23:00:00", "2026-09-03 23:15:00"):
                self.assertEqual(
                    self._sync_experience(con, ts),
                    {"inserted": 0, "closed": 0, "held": 1},
                )
            status, issue = con.execute(
                "SELECT status,issue FROM repair_queue WHERE check_name=?",
                (self.ENS_ROW,),
            ).fetchone()
            self.assertEqual(status, "pending")
            self.assertEqual(
                issue.count(ledger_invariants.UNRECORDED_VANISHED_NOTE), 1)
            con.close()

    def test_unrecorded_row_closes_once_the_open_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = self._experience_queue(tmp)
            con.execute(
                "INSERT INTO trade_experiences VALUES"
                "('live','2026-09-03 19:10:35','ENS-USDT-SWAP','short',"
                "'open','closed',0)"
            )
            con.commit()
            self.assertEqual(
                self._sync_experience(con, "2026-09-11 15:05:00"),
                {"inserted": 0, "closed": 1, "held": 0},
            )
            self.assertEqual(
                con.execute(
                    "SELECT status FROM repair_queue WHERE check_name=?",
                    (self.ENS_ROW,),
                ).fetchone()[0],
                "closed",
            )
            con.close()

    def test_later_unrelated_open_does_not_release_the_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = self._experience_queue(tmp)
            con.execute(
                "INSERT INTO trade_experiences VALUES"
                "('live','2026-09-10 10:00:00','ENS-USDT-SWAP','short',"
                "'open','open',50)"
            )
            con.commit()
            self.assertEqual(
                self._sync_experience(con, "2026-09-11 15:05:00"),
                {"inserted": 0, "closed": 0, "held": 1},
            )
            con.close()

    def test_ghost_direction_and_other_families_close_normally(self):
        ghost = "ledger_invariant:experience_position:live:ARB-USDT-SWAP:long"
        other = "ledger_invariant:negative_net:live:ETH-USDT-SWAP:short"
        with tempfile.TemporaryDirectory() as tmp:
            con = self._connect(tmp)
            con.execute(
                "CREATE TABLE trade_experiences("
                "profile TEXT,ts TEXT,symbol TEXT,side TEXT,action TEXT,"
                "status TEXT,remaining_sz REAL)"
            )
            for name, issue in (
                (ghost, "[live] ARB-USDT-SWAP long 经验剩余=243，OKX 实仓=0"),
                (other, "negative net"),
            ):
                con.execute(
                    "INSERT INTO repair_queue(ts,check_name,issue,status,"
                    "created_utc) VALUES('2026-09-10 21:15:06',?,?,"
                    "'pending','2026-09-10 21:15:06')",
                    (name, issue),
                )
            con.commit()
            con.execute("BEGIN IMMEDIATE")
            result = ledger_invariants.sync_repair_queue(
                con,
                family_prefix="ledger_invariant:",
                findings=[],
                ts="2026-09-11 15:05:00",
                hold=ledger_invariants.hold_unrecorded_vanished,
            )
            con.commit()
            self.assertEqual(result, {"inserted": 0, "closed": 2, "held": 0})
            con.close()

    def test_both_sync_call_sites_pass_the_unrecorded_hold(self):
        for rel in ("scripts/jobb_live_account_check.py",
                    "scripts/ledger_invariants.py"):
            source = (ROOT / rel).read_text(encoding="utf-8")
            calls = [
                node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", None))
                == "sync_repair_queue"
            ]
            self.assertEqual(len(calls), 1, rel)
            hold = {kw.arg: kw.value for kw in calls[0].keywords}.get("hold")
            self.assertIsNotNone(hold, rel)
            self.assertEqual(
                getattr(hold, "attr", getattr(hold, "id", None)),
                "hold_unrecorded_vanished",
                rel,
            )


if __name__ == "__main__":
    unittest.main()
