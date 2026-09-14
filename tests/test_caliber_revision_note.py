# -*- coding: utf-8 -*-
"""P0-1 ③｜口径修订说明段（主人拍板：不原地改写历史报告）。

2026-08-20 08:00 起头条已实现盈亏由「仅 close」改为「close + reduce」。已发布
的历史报告**保持原样不动** —— 原地改写会触发 revision 语义、需手写 UPDATE，
违反 writer 铁律，也会毁掉归档可复现性。代价是归档数字与新口径不可比，所以在
边界后的第一份日报里把受影响报告的旧值/新值/差额列出来，让这件事被写下来，
而不是等谁去发现。

本用例钉死四条：边界前不出现、边界后第一份出现、之后不再重复、无 reduce 不硬造。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import daily_report_writer as w  # noqa: E402

BOUNDARY = "2026-08-20 08:00:00"

_ACCOUNT_DDL = """
CREATE TABLE daily_reports (
    ts TEXT, profile TEXT, open_count INT, close_count INT,
    total_pnl REAL, total_fees REAL, best_trade TEXT, worst_trade TEXT,
    summary TEXT, lessons TEXT, raw TEXT, trade_day_num INT
);
CREATE TABLE weekly_reports (
    week_start_ts TEXT, profile TEXT, open_count INT, close_count INT,
    total_pnl REAL, win_rate REAL, avg_hold_hours REAL, margin_util_pct REAL,
    idle_ratio REAL, summary TEXT, lessons TEXT, raw TEXT, trade_week_num INT
);
"""

_TRADES_DDL = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY, cycle_id TEXT, ts TEXT, symbol TEXT,
    action TEXT, side TEXT, sz REAL, fill_px REAL, lev REAL, margin REAL,
    notional REAL, score_total REAL, reasoning TEXT, deviation TEXT,
    degradation TEXT, pnl REAL, raw TEXT
);
"""


class CaliberRevisionNoteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.account = root / "account.db"
        self.trades = root / "live_trades.db"
        with closing(sqlite3.connect(self.account)) as con:
            con.executescript(_ACCOUNT_DDL)
            con.commit()
        with closing(sqlite3.connect(self.trades)) as con:
            con.executescript(_TRADES_DDL)
            con.commit()

    def tearDown(self):
        self._tmp.cleanup()

    def _daily(self, ts: str, total_pnl: float):
        with closing(sqlite3.connect(self.account)) as con:
            con.execute(
                "INSERT INTO daily_reports(ts,profile,total_pnl) VALUES(?,?,?)",
                (ts, "live", total_pnl))
            con.commit()

    def _weekly(self, key: str, total_pnl: float, start: str, end: str):
        raw = json.dumps({"report_audit": {"trade_metrics": {"live": {
            "period_start_ts": start, "period_end_ts": end}}}},
            ensure_ascii=False)
        with closing(sqlite3.connect(self.account)) as con:
            con.execute(
                "INSERT INTO weekly_reports(week_start_ts,profile,total_pnl,raw)"
                " VALUES(?,?,?,?)", (key, "live", total_pnl, raw))
            con.commit()

    def _trade(self, ts: str, action: str, pnl: float):
        with closing(sqlite3.connect(self.trades)) as con:
            con.execute(
                "INSERT INTO trades(cycle_id,ts,symbol,action,pnl) "
                "VALUES(?,?,?,?,?)", ("c", ts, "BTC-USDT-SWAP", action, pnl))
            con.commit()

    def _block(self, report_ts: str) -> str:
        with mock.patch.object(w, "DB_PATH", self.account):
            return w._caliber_revision_block(report_ts)

    def test_before_boundary_renders_nothing(self):
        self._daily("2026-08-18 08:00:00", -8.4542)
        self._trade("2026-08-17 12:00:00", "reduce", 0.4624)
        self.assertEqual("", self._block("2026-08-19 08:00:00"))

    def test_first_report_after_boundary_lists_affected_reports(self):
        self._daily("2026-08-18 08:00:00", -8.4542)
        self._trade("2026-08-17 12:00:00", "reduce", 0.4624)
        self._trade("2026-08-17 13:00:00", "close", -8.4542)
        block = self._block(BOUNDARY)
        self.assertIn("口径修订说明", block)
        self.assertIn("日报 2026-08-18", block)
        self.assertIn("-8.4542", block)
        self.assertIn("-7.9918", block)
        self.assertIn("+0.4624", block)
        # 明说历史不改写 —— 这是主人拍板的那一半，不能只列数字。
        self.assertIn("保持原样不改", block)

    def test_sign_flip_is_called_out_explicitly(self):
        """08-16 那种由亏转盈的翻转必须显式标注，否则最容易被误读。"""
        self._daily("2026-08-16 08:00:00", -44.7149)
        self._trade("2026-08-15 20:00:00", "reduce", 72.6633)
        block = self._block(BOUNDARY)
        self.assertIn("符号翻转", block)
        self.assertIn("27.9484", block)

    def test_weekly_rows_use_the_window_recorded_in_raw(self):
        """周报的 week_start_ts 是报告键不是窗起点，必须读 raw 里的真实窗。"""
        self._weekly("2026-08-17 00:00:00", -186.9335,
                     "2026-08-10 08:00:00", "2026-08-17 08:00:00")
        self._trade("2026-08-16 09:00:00", "reduce", 82.0407)
        # 窗外的 reduce 不得混入。
        self._trade("2026-08-18 09:00:00", "reduce", 999.0)
        block = self._block(BOUNDARY)
        self.assertIn("周报 2026-08-17", block)
        self.assertIn("+82.0407", block)
        self.assertNotIn("999", block)

    def test_own_row_committed_before_render_does_not_suppress_note(self):
        """最关键的时序：`_commit_then_write_daily` 先 commit DB 行再渲 markdown。

        若「边界后是否已有日报」写成 `ts >= boundary` 的存在性判断，本轮自己
        那行就会把说明段吃掉 —— 说明永远不会出现，且**不会报错**，是最难发现
        的一类失效。判据必须是严格 `ts < report_ts`。
        """
        self._daily("2026-08-18 08:00:00", -8.4542)
        self._trade("2026-08-17 12:00:00", "reduce", 0.4624)
        self._daily(BOUNDARY, -1.0)          # 本轮自己的行，先于渲染落库
        self.assertIn("口径修订说明", self._block(BOUNDARY))

    def test_note_appears_only_once(self):
        """已有更早的边界后日报 ⇒ 说明已经发过，不再重复。"""
        self._daily("2026-08-18 08:00:00", -8.4542)
        self._trade("2026-08-17 12:00:00", "reduce", 0.4624)
        self._daily(BOUNDARY, -1.0)          # 边界后第一份已落库
        self.assertEqual("", self._block("2026-08-21 08:00:00"))

    def test_no_reduce_means_no_note(self):
        """没有 reduce 就没有口径差，不硬造一段空说明。"""
        self._daily("2026-08-18 08:00:00", -8.4542)
        self._trade("2026-08-17 12:00:00", "close", -8.4542)
        self.assertEqual("", self._block(BOUNDARY))

    def test_unreadable_database_degrades_to_empty(self):
        """块生成失败绝不能把整份日报炸掉。"""
        with mock.patch.object(w, "DB_PATH", Path("Z:/nope/account.db")):
            self.assertEqual("", w._caliber_revision_block(BOUNDARY))


if __name__ == "__main__":
    unittest.main()
