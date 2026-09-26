# -*- coding: utf-8 -*-
"""missed_opps_writer 的 briefing_layer_v1 第二来源（2026-08-18 批次）。

钉住三个最小可测核心：
  1. _briefing_layer_first_seen：窗口过滤 + 激活边界过滤 + 每日每
     (symbol, direction) 首现口径；
  2. _evaluate_outcome：与主循环镜像的 4h 后验计算（16 根 15m 精确连续、
     固定 ±2% 代理口径）；
  3. 激活边界常量与 daily_report_writer 侧注明边界同源（防单边改动）。
main() 端到端依赖 Windows 路径拼接（f"{root}\\analysis.db"），不在跨平台
单测内拼装；成交排除/幂等由生产链路与日报独立计数复核覆盖。
"""
import json
import sqlite3
import sys
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import decision_briefing  # noqa: E402
import missed_opps_writer  # noqa: E402

CST = timezone(timedelta(hours=8))


class BriefingLayerFirstSeenTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = str(Path(self._tmp.name) / "db")
        Path(self.root).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _snap(self, cyc, entries):
        decision_briefing.append_candidate_snapshot(
            self.root, cyc, [],
            [{"early_side": side, "row": {
                "symbol": sym, "last": 1.0, "chg24h": 0.0}}
             for sym, side in entries],
            None)

    def test_window_activation_and_first_seen(self):
        act = missed_opps_writer.BRIEFING_SOURCE_ACTIVATION_CYC
        act_day = act[:10]
        # 边界前一轮：必须被激活边界排除（不反向加责）。
        self._snap(f"{act_day}T07:45", [("AAA-USDT-SWAP", "long")])
        # 边界内：AAA long 首现 08:00，08:15 重复出现不再记；
        # 同 symbol 反向（short）是另一组，可另记首现。
        self._snap(f"{act_day}T08:00", [("AAA-USDT-SWAP", "long")])
        self._snap(f"{act_day}T08:15", [
            ("AAA-USDT-SWAP", "long"), ("BBB-USDT-SWAP", "short")])
        self._snap(f"{act_day}T08:30", [("AAA-USDT-SWAP", "short")])
        cycles, first_seen = missed_opps_writer._briefing_layer_first_seen(
            self.root, f"{act_day}T04:00", f"{act_day}T09:00")
        self.assertEqual(cycles, 3, "边界前的 07:45 不得计入观察轮")
        self.assertEqual(
            first_seen[("AAA-USDT-SWAP", "long")][0], f"{act_day}T08:00")
        self.assertEqual(
            first_seen[("BBB-USDT-SWAP", "short")][0], f"{act_day}T08:15")
        self.assertEqual(
            first_seen[("AAA-USDT-SWAP", "short")][0], f"{act_day}T08:30")
        self.assertEqual(len(first_seen), 3)

    def test_empty_snapshot_dir_reports_zero_cycles(self):
        act_day = missed_opps_writer.BRIEFING_SOURCE_ACTIVATION_CYC[:10]
        cycles, first_seen = missed_opps_writer._briefing_layer_first_seen(
            self.root, f"{act_day}T04:00", f"{act_day}T09:00")
        self.assertEqual((cycles, first_seen), (0, {}))

    def test_side_neutral_snapshot_uses_only_same_cycle_analysis_direction(self):
        first_cycle = missed_opps_writer.BRIEFING_SIDE_NEUTRAL_ACTIVATION_CYC
        first_dt = datetime.strptime(first_cycle, "%Y-%m-%dT%H:%M")
        second_cycle = (first_dt + timedelta(minutes=15)).strftime(
            "%Y-%m-%dT%H:%M")
        day = first_cycle[:10]
        briefing = Path(self.root).parent / "logs" / "briefing"
        briefing.mkdir(parents=True, exist_ok=True)
        payloads = []
        for cycle, symbol in (
            (first_cycle, "AAA-USDT-SWAP"),
            (second_cycle, "BBB-USDT-SWAP"),
        ):
            payloads.append(json.dumps({
                "schema": "briefing_candidates_v1",
                "cycle_id": cycle,
                "candidates": [{
                    "ordinal": 1,
                    "layer": "all_market",
                    "symbol": symbol,
                    "side": None,
                    "eligible_sides": ["long", "short"],
                    "opportunity_state": "SIDE_NEUTRAL",
                    "selected_for_review": True,
                }],
            }))
        (briefing / f"candidates-{day.replace('-', '')}.jsonl").write_text(
            "\n".join(payloads) + "\n", encoding="utf-8")

        con = sqlite3.connect(Path(self.root) / "analysis.db")
        try:
            con.execute(
                "CREATE TABLE analysis_runs(cycle_id TEXT,status TEXT)")
            con.execute(
                "CREATE TABLE analysis_signals("
                "cycle_id TEXT,symbol TEXT,action TEXT,side TEXT)")
            con.execute(
                "INSERT INTO analysis_runs VALUES(?,?)",
                (first_cycle, "ok"))
            con.execute(
                "INSERT INTO analysis_signals VALUES(?,?,?,?)",
                (first_cycle, "AAA-USDT-SWAP", "open_short", "short"))
            con.commit()
        finally:
            con.close()

        cycles, first_seen = missed_opps_writer._briefing_layer_first_seen(
            self.root, first_cycle,
            (first_dt + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"))

        self.assertEqual(2, cycles)
        self.assertEqual(
            (first_cycle, "all_market",
             missed_opps_writer.BRIEFING_SIDE_NEUTRAL_SOURCE_TAG),
            first_seen[("AAA-USDT-SWAP", "short")],
        )
        self.assertNotIn(("AAA-USDT-SWAP", "long"), first_seen)
        self.assertFalse(any(key[0] == "BBB-USDT-SWAP" for key in first_seen))


class EvaluateOutcomeTests(unittest.TestCase):
    def _mkt(self, bars):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE kline_cache "
            "(symbol TEXT, tf TEXT, ts TEXT, o REAL, h REAL, l REAL, c REAL)")
        con.executemany(
            "INSERT INTO kline_cache VALUES ('AAA-USDT-SWAP','15m',?,?,?,?,?)",
            bars)
        return con

    @staticmethod
    def _bars(start_cst, n=16, o=100.0, h=103.0, low=99.5, c=101.0):
        start = (datetime.strptime(start_cst, "%Y-%m-%d %H:%M:%S")
                 - timedelta(hours=8))
        out = []
        for i in range(n):
            ts = (start + timedelta(minutes=15 * i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            out.append((ts, o, h, low, c))
        return out

    def test_long_outcome_and_fixed2pct_hit(self):
        slot = "2026-08-19 08:00:00"
        con = self._mkt(self._bars(slot))  # 高点 103 → +3% ≥ 2% 命中
        ok, actual, hit = missed_opps_writer._evaluate_outcome(
            con, "AAA-USDT-SWAP", slot, "long")
        self.assertTrue(ok)
        self.assertAlmostEqual(actual, 1.0, places=6)  # (101-100)/100
        self.assertEqual(hit, 1)

    def test_short_outcome_no_hit(self):
        slot = "2026-08-19 08:00:00"
        con = self._mkt(self._bars(slot, low=99.5))  # 最低 -0.5% < 2%
        ok, actual, hit = missed_opps_writer._evaluate_outcome(
            con, "AAA-USDT-SWAP", slot, "short")
        self.assertTrue(ok)
        self.assertAlmostEqual(actual, -1.0, places=6)
        self.assertEqual(hit, 0)

    def test_incomplete_bars_fail_closed(self):
        slot = "2026-08-19 08:00:00"
        con = self._mkt(self._bars(slot, n=15))
        ok, actual, hit = missed_opps_writer._evaluate_outcome(
            con, "AAA-USDT-SWAP", slot, "long")
        self.assertFalse(ok)
        self.assertIsNone(actual)


class ActivationBoundarySyncTests(unittest.TestCase):
    def test_writer_and_daily_report_share_boundary(self):
        import daily_report_writer
        self.assertEqual(
            missed_opps_writer.BRIEFING_SOURCE_ACTIVATION_CYC.replace(
                "T", " ") + ":00",
            daily_report_writer.MISSED_BRIEFING_SOURCE_ACTIVATION_TS)


class Sim2rAtrTests(unittest.TestCase):
    """实盘口径模拟（2026-09-26 对照 V3）：止损 clamp(1×ATR, 3%, 6%)、TP +5%、24h 先触。"""

    SLOT = "2026-08-20 10:00:00"

    def _mkt(self, bars_15m, atr14=1.0, atr_close=100.0, with_atr=True):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE kline_cache (symbol TEXT, tf TEXT, ts TEXT, "
            "o REAL, h REAL, l REAL, c REAL, atr14 REAL)")
        con.executemany(
            "INSERT INTO kline_cache VALUES "
            "('AAA-USDT-SWAP','15m',?,?,?,?,?,NULL)", bars_15m)
        if with_atr:
            atr_ts = (datetime.strptime(self.SLOT, "%Y-%m-%d %H:%M:%S")
                      - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
            con.execute(
                "INSERT INTO kline_cache VALUES "
                "('AAA-USDT-SWAP','1H',?,?,?,?,?,?)",
                (atr_ts, atr_close, atr_close, atr_close, atr_close, atr14))
        return con

    @staticmethod
    def _bars(slot_cst, n, o=100.0, h=100.5, low=99.5, c=100.0):
        start = (datetime.strptime(slot_cst, "%Y-%m-%d %H:%M:%S")
                 - timedelta(hours=8))
        return [
            ((start + timedelta(minutes=15 * i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"), o, h, low, c)
            for i in range(n)
        ]

    def test_sl_rule_matches_v3(self):
        self.assertEqual(4.0, missed_opps_writer.sim_sl_pct(None))
        self.assertEqual(3.0, missed_opps_writer.sim_sl_pct(1.0))
        self.assertEqual(4.5, missed_opps_writer.sim_sl_pct(4.5))
        self.assertEqual(6.0, missed_opps_writer.sim_sl_pct(80.0))
        self.assertEqual(4.0, missed_opps_writer.sim_sl_pct("nan"))
        self.assertEqual("ambiguous", missed_opps_writer.sim_outcome(3, 3))
        self.assertEqual("hit_tp", missed_opps_writer.sim_outcome(2, 5))
        self.assertEqual("hit_sl", missed_opps_writer.sim_outcome(None, 5))
        self.assertEqual("neither", missed_opps_writer.sim_outcome(None, None))

    def test_atr_uses_true_range_of_last_14_closed_1h_bars(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE kline_cache (symbol TEXT, tf TEXT, ts TEXT, "
            "o REAL, h REAL, l REAL, c REAL, atr14 REAL)")
        t0 = datetime(2026, 8, 20, 2, 0)   # SLOT in UTC
        rows = []
        for index in range(15):
            ts = (t0 - timedelta(hours=15 - index)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append(("AAA-USDT-SWAP", "1H", ts, 100.0, 102.0, 98.0, 100.0, 9.0))
        con.executemany("INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?)", rows)
        # 14 根 TR 均为 4 → ATR% = 4%（存列 atr14=9 不参与）
        self.assertAlmostEqual(
            4.0,
            missed_opps_writer._atr_pct_1h(
                con, "AAA-USDT-SWAP", "2026-08-20T02:00:00Z", 100.0))
        con.execute("DELETE FROM kline_cache WHERE ts<?",
                    ("2026-08-19T20:00:00Z",))
        # 不足 15 根 → 退回存列 atr14=9 → 9%
        self.assertAlmostEqual(
            9.0,
            missed_opps_writer._atr_pct_1h(
                con, "AAA-USDT-SWAP", "2026-08-20T02:00:00Z", 100.0))
        con.close()

    def test_atr_excludes_the_1h_bar_still_open_at_the_slot(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE kline_cache (symbol TEXT, tf TEXT, ts TEXT, "
            "o REAL, h REAL, l REAL, c REAL, atr14 REAL)")
        hour = datetime(2026, 8, 20, 2, 0)   # 槽位 02:15Z 所在的 1H 开盘时刻
        rows = []
        for index in range(15):
            ts = (hour - timedelta(hours=15 - index)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append(("AAA-USDT-SWAP", "1H", ts, 100.0, 102.0, 98.0, 100.0, 9.0))
        # 02:00Z 这根在 02:15Z 时还没收盘：它的巨幅波动与存列 atr14 都不得进入
        rows.append(("AAA-USDT-SWAP", "1H", "2026-08-20T02:00:00Z",
                     100.0, 150.0, 50.0, 100.0, 99.0))
        con.executemany("INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?)", rows)
        self.assertAlmostEqual(
            4.0,
            missed_opps_writer._atr_pct_1h(
                con, "AAA-USDT-SWAP", "2026-08-20T02:15:00Z", 100.0))
        # 03:00Z 时它已收盘：TR=100 进入 14 根均值
        self.assertAlmostEqual(
            (13 * 4.0 + 100.0) / 14.0,
            missed_opps_writer._atr_pct_1h(
                con, "AAA-USDT-SWAP", "2026-08-20T03:00:00Z", 100.0))
        con.execute("DELETE FROM kline_cache WHERE ts<?",
                    ("2026-08-19T20:00:00Z",))
        # 退回存列时同样只看已收盘那根（01:00Z 的 9），不是 02:00Z 的 99
        self.assertAlmostEqual(
            9.0,
            missed_opps_writer._atr_pct_1h(
                con, "AAA-USDT-SWAP", "2026-08-20T02:15:00Z", 100.0))
        con.close()

    def test_long_hit_tp_first(self):
        # 入场 100、ATR=1 → 1% 抬到 3% 下限（97）、TP 固定 5%（105）
        bars = self._bars(self.SLOT, 96)
        bars[10] = (bars[10][0], 100.0, 106.5, 99.8, 106.0)
        stop, tp, outcome, touch = missed_opps_writer._evaluate_sim(
            self._mkt(bars), "AAA-USDT-SWAP", self.SLOT, "long")
        self.assertAlmostEqual(stop, 3.0)
        self.assertAlmostEqual(tp, 5.0)
        self.assertEqual(outcome, "hit_tp")
        self.assertEqual(touch, "2026-08-20 12:30:00")

    def test_long_hit_sl_before_later_tp(self):
        bars = self._bars(self.SLOT, 96)
        bars[5] = (bars[5][0], 100.0, 100.5, 96.8, 97.0)
        bars[20] = (bars[20][0], 100.0, 107.0, 99.8, 106.5)
        _s, _t, outcome, _touch = missed_opps_writer._evaluate_sim(
            self._mkt(bars), "AAA-USDT-SWAP", self.SLOT, "long")
        self.assertEqual(outcome, "hit_sl")

    def test_same_bar_both_touches_is_ambiguous_not_a_loss(self):
        bars = self._bars(self.SLOT, 96)
        bars[3] = (bars[3][0], 100.0, 106.5, 96.5, 100.0)
        _s, _t, outcome, touch = missed_opps_writer._evaluate_sim(
            self._mkt(bars), "AAA-USDT-SWAP", self.SLOT, "long")
        self.assertEqual(outcome, "ambiguous")
        self.assertEqual(touch, "2026-08-20 10:45:00")

    def test_short_direction_mirrors(self):
        bars = self._bars(self.SLOT, 96)
        bars[7] = (bars[7][0], 100.0, 100.2, 93.5, 94.0)  # 空头 TP=95
        _s, _t, outcome, _touch = missed_opps_writer._evaluate_sim(
            self._mkt(bars), "AAA-USDT-SWAP", self.SLOT, "short")
        self.assertEqual(outcome, "hit_tp")

    def test_missing_atr_uses_default_four_percent(self):
        bars = self._bars(self.SLOT, 96)
        stop, tp, outcome, touch = missed_opps_writer._evaluate_sim(
            self._mkt(bars, with_atr=False), "AAA-USDT-SWAP", self.SLOT,
            "long")
        self.assertAlmostEqual(stop, 4.0)
        self.assertAlmostEqual(tp, 5.0)
        self.assertEqual(outcome, "neither")

    def test_missing_entry_bar_is_no_data(self):
        stop, tp, outcome, touch = missed_opps_writer._evaluate_sim(
            self._mkt([]), "AAA-USDT-SWAP", self.SLOT, "long")
        self.assertIsNone(stop)
        self.assertEqual(outcome, "no_data")

    def test_untouched_with_incomplete_window_is_no_data(self):
        bars = self._bars(self.SLOT, 20)  # 24h 应有 96 根，覆盖不足
        _s, _t, outcome, _touch = missed_opps_writer._evaluate_sim(
            self._mkt(bars), "AAA-USDT-SWAP", self.SLOT, "long")
        self.assertEqual(outcome, "no_data")

    def test_absurd_atr_is_clamped_to_six_percent(self):
        bars = self._bars(self.SLOT, 96)
        stop, _t, outcome, touch = missed_opps_writer._evaluate_sim(
            self._mkt(bars, atr14=80.0), "AAA-USDT-SWAP", self.SLOT, "long")
        self.assertAlmostEqual(stop, 6.0)
        self.assertEqual(outcome, "neither")
        self.assertIsNone(touch)

    def test_untouched_full_window_is_neither(self):
        bars = self._bars(self.SLOT, 96)
        _s, _t, outcome, touch = missed_opps_writer._evaluate_sim(
            self._mkt(bars), "AAA-USDT-SWAP", self.SLOT, "long")
        self.assertEqual(outcome, "neither")
        self.assertIsNone(touch)


class MatureSimBackfillTests(unittest.TestCase):
    def _les(self, rows):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE missed_opportunities (id INTEGER PRIMARY KEY, "
            "ts TEXT, symbol TEXT, direction_hint TEXT)")
        missed_opps_writer._ensure_sim_columns(con)
        con.executemany(
            "INSERT INTO missed_opportunities (ts, symbol, direction_hint) "
            "VALUES (?,?,?)", rows)
        return con
    def test_backfills_matured_rows_and_skips_young(self):
        slot = Sim2rAtrTests.SLOT
        bars = Sim2rAtrTests._bars(slot, 96)
        bars[10] = (bars[10][0], 100.0, 106.5, 99.8, 106.0)
        mkt = Sim2rAtrTests()._mkt(bars)
        les = self._les([
            (slot, "AAA-USDT-SWAP", "long"),          # 已成熟 → 回补
            ("2026-08-21 09:50:00", "AAA-USDT-SWAP", "long"),  # 未成熟
        ])
        now = datetime(2026, 8, 21, 12, 0)  # slot+26h；第二行仅 +2h
        n_eval, n_done = missed_opps_writer._mature_sim_backfill(
            les, mkt, now_cst=now)
        self.assertEqual((n_eval, n_done), (1, 1))
        got = les.execute(
            "SELECT sim_outcome_24h, sim_stop_pct, sim_rule FROM missed_opportunities "
            "ORDER BY id").fetchall()
        self.assertEqual(got[0][0], "hit_tp")
        self.assertAlmostEqual(got[0][1], 3.0)
        self.assertEqual(got[0][2], missed_opps_writer.SIM_RULE)
        self.assertIsNone(got[1][0])
        self.assertIsNone(got[1][2])

    def test_permanent_no_entry_bar_rows_not_rescanned(self):
        slot = Sim2rAtrTests.SLOT
        mkt = Sim2rAtrTests()._mkt([])  # 入场 15m K 线永久缺失
        les = self._les([(slot, "AAA-USDT-SWAP", "long")])
        now = datetime(2026, 8, 21, 12, 0)
        first = missed_opps_writer._mature_sim_backfill(les, mkt, now_cst=now)
        second = missed_opps_writer._mature_sim_backfill(les, mkt, now_cst=now)
        self.assertEqual(first, (1, 0))
        self.assertEqual(second, (0, 0))

    def test_old_rule_pending_rows_are_re_evaluated_once_and_conclusions_frozen(self):
        slot = Sim2rAtrTests.SLOT
        bars = Sim2rAtrTests._bars(slot, 96)
        mkt = Sim2rAtrTests()._mkt(bars)
        les = self._les([(slot, "AAA-USDT-SWAP", "long"),
                         (slot, "BBB-USDT-SWAP", "long")])
        # 第一行：旧口径挂起（no_data、rule 为空）→ 按当前口径重算；
        # 第二行：旧口径已有结论（hit_sl）→ 冻结不动
        les.execute(
            "UPDATE missed_opportunities SET sim_outcome_24h='no_data', "
            "sim_stop_pct=3.0 WHERE id=1")
        les.execute(
            "UPDATE missed_opportunities SET sim_outcome_24h='hit_sl', "
            "sim_stop_pct=9.0, sim_tp_pct=18.0 WHERE id=2")
        now = datetime(2026, 8, 21, 12, 0)
        self.assertEqual(
            (1, 1), missed_opps_writer._mature_sim_backfill(les, mkt, now_cst=now))
        rows = les.execute(
            "SELECT sim_outcome_24h, sim_tp_pct, sim_rule FROM missed_opportunities "
            "ORDER BY id").fetchall()
        self.assertEqual(("neither", 5.0, missed_opps_writer.SIM_RULE), rows[0])
        self.assertEqual(("hit_sl", 18.0, None), rows[1])


class DryRunSchemaSafetyTests(unittest.TestCase):
    def test_dry_run_opens_read_only_and_never_adds_sim_columns(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lessons.db"
            con = sqlite3.connect(path)
            con.execute(
                "CREATE TABLE missed_opportunities("
                "id INTEGER PRIMARY KEY,ts TEXT,symbol TEXT)"
            )
            con.commit()
            con.close()
            before = path.read_bytes()
            with mock.patch.object(
                    missed_opps_writer, "_ensure_sim_columns") as ensure:
                ro = missed_opps_writer._open_lessons_database(
                    path, dry_run=True)
                try:
                    columns = [
                        row[1] for row in ro.execute(
                            "PRAGMA table_info(missed_opportunities)")
                    ]
                    with self.assertRaises(sqlite3.OperationalError):
                        ro.execute(
                            "ALTER TABLE missed_opportunities "
                            "ADD COLUMN forbidden TEXT")
                finally:
                    ro.close()
            ensure.assert_not_called()
            self.assertEqual(["id", "ts", "symbol"], columns)
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
