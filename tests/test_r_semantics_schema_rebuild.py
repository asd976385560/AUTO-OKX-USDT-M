# -*- coding: utf-8 -*-
"""apply_r_semantics_schema.rebuild 必须带上迁移 DDL 定稿后追加的列（sim_*、路径埋点）。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import apply_r_semantics_schema as migration  # noqa: E402


class RebuildPreservesExtraColumnsTests(unittest.TestCase):
    def test_missed_opportunities_rebuild_keeps_simulation_columns(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE missed_opportunities("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,symbol TEXT NOT NULL,"
            "score INTEGER NOT NULL,regime TEXT,direction_hint TEXT,actual_4h_pct REAL,"
            "would_hit_1R INTEGER,notes TEXT,reviewed_utc TEXT NOT NULL,decision_card TEXT,"
            "sim_stop_pct REAL,sim_tp_pct REAL,sim_outcome_24h TEXT,"
            "sim_first_touch_cst TEXT,sim_rule TEXT)")
        con.execute(
            "INSERT INTO missed_opportunities(ts,symbol,score,would_hit_1R,notes,"
            "reviewed_utc,sim_stop_pct,sim_tp_pct,sim_outcome_24h,sim_first_touch_cst,"
            "sim_rule) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-20 10:00:00", "AAA-USDT-SWAP", 0, 1, "n", "2026-08-21",
             3.0, 5.0, "hit_tp", "2026-08-20 12:30:00", "v3_clamp_1xatr1h_3to6_tp5_h24"))
        con.commit()  # rebuild 自己开 BEGIN IMMEDIATE，夹具不能留隐式事务
        copied = migration.rebuild(
            con, "missed_opportunities", migration.MO_NEW_DDL,
            migration.MO_COLS, migration.MO_OLD_COLS, migration.MO_INDEXES)
        self.assertEqual(1, copied)
        cols = migration.columns(con, "missed_opportunities")
        self.assertIn("would_hit_1r_fixed2pct", cols)
        self.assertNotIn("would_hit_1R", cols)
        for name in ("sim_stop_pct", "sim_tp_pct", "sim_outcome_24h",
                     "sim_first_touch_cst", "sim_rule"):
            self.assertIn(name, cols)
        row = con.execute(
            "SELECT would_hit_1r_fixed2pct,sim_stop_pct,sim_outcome_24h,sim_rule "
            "FROM missed_opportunities").fetchone()
        con.close()
        self.assertEqual((1, 3.0, "hit_tp", "v3_clamp_1xatr1h_3to6_tp5_h24"), row)

    def test_rebuild_without_extra_columns_is_unchanged(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE missed_opportunities("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,symbol TEXT NOT NULL,"
            "score INTEGER NOT NULL,regime TEXT,direction_hint TEXT,actual_4h_pct REAL,"
            "would_hit_1R INTEGER,notes TEXT,reviewed_utc TEXT NOT NULL,decision_card TEXT)")
        con.execute(
            "INSERT INTO missed_opportunities(ts,symbol,score,would_hit_1R,reviewed_utc) "
            "VALUES('2026-08-20 10:00:00','AAA-USDT-SWAP',0,0,'2026-08-21')")
        con.commit()
        self.assertEqual([], migration.extra_columns(
            con, "missed_opportunities", migration.MO_OLD_COLS,
            {"would_hit_1r_fixed2pct"}))
        copied = migration.rebuild(
            con, "missed_opportunities", migration.MO_NEW_DDL,
            migration.MO_COLS, migration.MO_OLD_COLS, migration.MO_INDEXES)
        self.assertEqual(1, copied)
        self.assertEqual(
            {"id", "ts", "symbol", "score", "regime", "direction_hint",
             "actual_4h_pct", "would_hit_1r_fixed2pct", "notes", "reviewed_utc",
             "decision_card"},
            migration.columns(con, "missed_opportunities"))
        con.close()


if __name__ == "__main__":
    unittest.main()
