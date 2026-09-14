# -*- coding: utf-8 -*-
"""record_xsearch 的 cycle 归一必须按槽起点，而不是记账时刻（2026-09-13 主人拍板）。

回归背景：news-scout 槽 :10/:25/:40/:55，一轮跑过 5 分钟就越过下一刻钟，与下一槽同
cycle，record_collection 的 INSERT OR REPLACE 会把前一轮的账抹掉（10:10 槽的 degraded
被 10:25 槽的 ok 覆盖）。零模型名（红线 #1）。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = ROOT / "collectors"
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))

import ledger  # noqa: E402
import record_xsearch  # noqa: E402

CST = timezone(timedelta(hours=8))


class ScoutCycleIdTests(unittest.TestCase):
    def _cycle(self, hour: int, minute: int, second: int = 0, day: int = 13) -> str:
        return record_xsearch.scout_cycle_id(datetime(2026, 9, day, hour, minute, second, tzinfo=CST))

    def test_slot_anchor_boundaries(self):
        cases = {
            (23, 9, 59): "2026-09-13T22:45",   # :55 槽拖到下一小时开头，仍归上一刻钟
            (23, 10, 0): "2026-09-13T23:00",   # :10 槽起点
            (23, 15, 25): "2026-09-13T23:00",  # 实证：10:10 槽 5.5 分钟跑完，原先误归 T23:15
            (23, 24, 59): "2026-09-13T23:00",
            (23, 25, 0): "2026-09-13T23:15",   # :25 槽起点
            (23, 26, 20): "2026-09-13T23:15",  # 实证：10:25 槽 1.5 分钟跑完
            (23, 39, 59): "2026-09-13T23:15",
            (23, 40, 0): "2026-09-13T23:30",
            (23, 54, 59): "2026-09-13T23:30",
            (23, 55, 0): "2026-09-13T23:45",
            (23, 59, 59): "2026-09-13T23:45",
        }
        for (h, m, s), want in cases.items():
            with self.subTest(t=f"{h:02d}:{m:02d}:{s:02d}"):
                self.assertEqual(self._cycle(h, m, s), want)

    def test_day_rollover(self):
        self.assertEqual(self._cycle(0, 5, 0, day=14), "2026-09-13T23:45")

    def test_utc_input_is_converted_to_cst(self):
        got = record_xsearch.scout_cycle_id(datetime(2026, 9, 13, 15, 26, 20, tzinfo=timezone.utc))
        self.assertEqual(got, "2026-09-13T23:15")

    def test_naive_input_is_treated_as_cst(self):
        self.assertEqual(record_xsearch.scout_cycle_id(datetime(2026, 9, 13, 23, 15, 25)), "2026-09-13T23:00")

    def test_offset_matches_cron_slots(self):
        # 槽在每刻钟第 10 分钟；若 cron 改槽，这里和 SCOUT_SLOT_OFFSET_MIN 要一起改
        self.assertEqual(record_xsearch.SCOUT_SLOT_OFFSET_MIN, 10)

    def test_consecutive_slots_no_longer_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "ledger.db")
            ledger.init_ledger(db)

            def run(now: datetime, argv: list[str]) -> None:
                with mock.patch.object(record_xsearch, "_now_cst", return_value=now),                         mock.patch.object(sys, "argv", ["record_xsearch.py", "--db-root", tmp] + argv),                         redirect_stdout(io.StringIO()):
                    self.assertEqual(record_xsearch.main(), 0)

            run(datetime(2026, 9, 13, 23, 15, 25, tzinfo=CST),
                ["--status", "degraded", "--rows", "4", "--err", "x_search 2x 524"])
            run(datetime(2026, 9, 13, 23, 26, 20, tzinfo=CST), ["--status", "ok", "--rows", "1"])

            con = ledger.connect(db, readonly=True)
            try:
                rows = con.execute(
                    "SELECT cycle_id, status, rows FROM collection_runs WHERE source=? ORDER BY cycle_id",
                    (ledger.SRC_XSEARCH,),
                ).fetchall()
                rows = [tuple(r) for r in rows]  # ledger.connect 用 sqlite3.Row，比对前转元组
            finally:
                con.close()
            self.assertEqual(rows, [("2026-09-13T23:00", "degraded", 4), ("2026-09-13T23:15", "ok", 1)])


if __name__ == "__main__":
    unittest.main()
