from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


update_playbook_stats = load_module(
    "update_playbook_stats_current_test",
    SCRIPTS / "update_playbook_stats.py",
)
trade_experience_writer = load_module(
    "trade_experience_writer_current_test",
    SCRIPTS / "trade_experience_writer.py",
)


def make_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE playbook(
          id INTEGER PRIMARY KEY,
          summary TEXT,
          evidence_count INTEGER,
          win_count INTEGER,
          loss_count INTEGER,
          win_rate REAL,
          avg_pnl_pct REAL,
          last_validated_cycle INTEGER
        );
        CREATE TABLE cycle_runs(cycle_count INTEGER);
        CREATE TABLE trade_experiences(
          id INTEGER PRIMARY KEY,
          profile TEXT,
          playbook_ref TEXT,
          pnl_pct REAL,
          status TEXT,
          closed_at TEXT,
          ts TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO playbook VALUES(?,?,?,?,?,?,?,?)",
        [
            (1, "one", 99, 80, 19, 0.8, 12.0, 1),
            (2, "two", 5, 4, 1, 0.8, 3.0, 1),
        ],
    )
    con.execute("INSERT INTO cycle_runs VALUES(42)")
    con.executemany(
        "INSERT INTO trade_experiences VALUES(?,?,?,?,?,?,?)",
        [
            (10, "live", "[1]", 10.0, "closed", "x", "x"),
            (11, "demo", "playbook #1", -5.0, "closed", "x", "x"),
            (12, "demo", "[2]", 20.0, "open", None, "x"),
            (13, "live", "playbook #99", 1.0, "closed", "x", "x"),
        ],
    )
    con.commit()
    return con


class PlaybookCurrentFactTests(unittest.TestCase):
    def test_parser_does_not_treat_unrelated_numbers_as_refs(self):
        self.assertEqual(
            update_playbook_stats.parse_playbook_ids(
                "cycle 20260729 used playbook #7"),
            {7},
        )
        self.assertEqual(
            update_playbook_stats.parse_playbook_ids("[1, 2, 2]"),
            {1, 2},
        )
        self.assertEqual(
            update_playbook_stats.parse_playbook_ids("cycle 12345"),
            set(),
        )

    def test_current_experiences_replace_legacy_numeric_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = make_db(Path(tmp) / "account.db")
            plan = update_playbook_stats.build_plan(con)
            by_id = {row["id"]: row for row in plan["updates"]}
            self.assertEqual(by_id[1]["target"]["evidence_count"], 2)
            self.assertEqual(by_id[1]["target"]["win_count"], 1)
            self.assertEqual(by_id[1]["target"]["loss_count"], 1)
            self.assertEqual(by_id[1]["target"]["win_rate"], 0.5)
            self.assertEqual(by_id[1]["target"]["avg_pnl_pct"], 2.5)
            self.assertEqual(by_id[2]["target"]["evidence_count"], 0)
            self.assertEqual(plan["invalid_refs"][0]["unknown_ids"], [99])

            update_playbook_stats.apply_plan(con, plan)
            second = update_playbook_stats.build_plan(con)
            self.assertEqual(second["changed_playbooks"], 0)
            con.close()

    def test_baseline_is_explicit_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "account.db"
            con = make_db(db_path)
            plan = update_playbook_stats.build_plan(con)
            baseline = root / "baseline.json"
            update_playbook_stats.write_baseline(baseline, db_path, plan)
            payload = json.loads(baseline.read_text(encoding="utf-8"))
            self.assertEqual(payload["rows"][0]["evidence_count"], 99)
            with self.assertRaises(FileExistsError):
                update_playbook_stats.write_baseline(
                    baseline, db_path, plan)
            con.close()

    def test_decision_card_refs_propagate_to_experience(self):
        data = {
            "decision_card": {
                "historical_experience": {
                    "profitable": [
                        {"playbook_ref": "playbook #4"},
                        {"playbook_ref": 2},
                    ],
                    "unprofitable": [{"playbook_ref": "[4, 7]"}],
                    "missed_opportunities": [],
                }
            }
        }
        value = trade_experience_writer._canonical_playbook_ref(data, {})
        self.assertEqual(json.loads(value), [2, 4, 7])


if __name__ == "__main__":
    unittest.main()
