# -*- coding: utf-8 -*-
"""briefing 候选分层（2026-08-14 低占用主动性批次）：纯函数回归。

背景：成熟候选排序（|三周期一致|→|chg24h|）天然只出「已走完」的晚期结构，
模型以「不追」wait——14 天决策分布 wait 74%/open 0.4% 的机制根因之一。
本文件钉住该批次两个最小可测核心：早期结构判定与连续 wait 计数；
分组配额与渲染由生产简报烟测覆盖，不在此重复拼装全库。
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import decision_briefing  # noqa: E402


class EarlyStructureSideTests(unittest.TestCase):
    def test_long_pullback_variants(self):
        # 4H 满票多 + 15m 未同向（0=混合 / -2=反向）→ 早多
        self.assertEqual(
            decision_briefing.early_structure_side(
                {"4H": 2, "1H": 2, "15m": 0}), "long")
        self.assertEqual(
            decision_briefing.early_structure_side(
                {"4H": 2, "1H": -2, "15m": -2}), "long")

    def test_short_pullback_variants(self):
        self.assertEqual(
            decision_briefing.early_structure_side(
                {"4H": -2, "1H": 0, "15m": 0}), "short")
        self.assertEqual(
            decision_briefing.early_structure_side(
                {"4H": -2, "1H": 2, "15m": 2}), "short")

    def test_fully_aligned_is_not_early(self):
        # 三周期全对齐=成熟结构，归成熟组，不得重复进早期组。
        self.assertIsNone(decision_briefing.early_structure_side(
            {"4H": 2, "1H": 2, "15m": 2}))
        self.assertIsNone(decision_briefing.early_structure_side(
            {"4H": -2, "1H": -2, "15m": -2}))

    def test_weak_or_missing_4h_is_not_early(self):
        # 4H 未满票立向（0=混合）或缺周期票 → 不判早期结构。
        self.assertIsNone(decision_briefing.early_structure_side(
            {"4H": 0, "1H": 2, "15m": -2}))
        self.assertIsNone(decision_briefing.early_structure_side({"15m": 0}))
        self.assertIsNone(decision_briefing.early_structure_side({}))


class ConsecutiveWaitStreakTests(unittest.TestCase):
    def _con(self, rows):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE analysis_signals ("
            "cycle_id TEXT, symbol TEXT, action TEXT, side TEXT)")
        con.executemany(
            "INSERT INTO analysis_signals VALUES (?,?,?,?)", rows)
        return con

    def test_streak_counts_until_first_non_wait(self):
        con = self._con([
            ("2026-08-13T10:00", "AAA-USDT-SWAP", "hold", None),
            ("2026-08-13T10:15", "AAA-USDT-SWAP", "wait", "short"),
            ("2026-08-13T10:30", "AAA-USDT-SWAP", "wait", "long"),
        ])
        streak, side = decision_briefing.consecutive_wait_streak(
            con, "AAA-USDT-SWAP")
        self.assertEqual(streak, 2)
        self.assertEqual(side, "long")  # side 取最新一轮的

    def test_latest_non_wait_means_zero(self):
        con = self._con([
            ("2026-08-13T10:00", "AAA-USDT-SWAP", "wait", "long"),
            ("2026-08-13T10:15", "AAA-USDT-SWAP", "open_long", "long"),
        ])
        streak, side = decision_briefing.consecutive_wait_streak(
            con, "AAA-USDT-SWAP")
        self.assertEqual(streak, 0)
        self.assertIsNone(side)

    def test_other_symbols_and_empty_table_ignored(self):
        con = self._con([
            ("2026-08-13T10:15", "BBB-USDT-SWAP", "wait", "long"),
        ])
        streak, side = decision_briefing.consecutive_wait_streak(
            con, "AAA-USDT-SWAP")
        self.assertEqual(streak, 0)
        self.assertIsNone(side)

    def test_lookback_caps_scan(self):
        rows = [
            (f"2026-08-13T{i:02d}:00", "AAA-USDT-SWAP", "wait", "long")
            for i in range(20)
        ]
        con = self._con(rows)
        streak, _ = decision_briefing.consecutive_wait_streak(
            con, "AAA-USDT-SWAP", lookback=5)
        self.assertEqual(streak, 5)

    def test_null_action_rows_break_streak(self):
        con = self._con([
            ("2026-08-13T10:00", "AAA-USDT-SWAP", "wait", "long"),
            ("2026-08-13T10:15", "AAA-USDT-SWAP", None, None),
            ("2026-08-13T10:30", "AAA-USDT-SWAP", "wait", "short"),
        ])
        streak, side = decision_briefing.consecutive_wait_streak(
            con, "AAA-USDT-SWAP")
        self.assertEqual(streak, 1)
        self.assertEqual(side, "short")


if __name__ == "__main__":
    unittest.main()
