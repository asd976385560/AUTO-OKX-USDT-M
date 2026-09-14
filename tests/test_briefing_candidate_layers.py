# -*- coding: utf-8 -*-
"""briefing 候选分层（2026-08-14 低占用主动性批次）：纯函数回归。

背景：成熟候选排序（|三周期一致|→|chg24h|）天然只出「已走完」的晚期结构，
模型以「不追」wait——14 天决策分布 wait 74%/open 0.4% 的机制根因之一。
本文件钉住该批次两个最小可测核心：早期结构判定与连续 wait 计数；
分组配额与渲染由生产简报烟测覆盖，不在此重复拼装全库。
"""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

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


class OpportunityStateTests(unittest.TestCase):
    @staticmethod
    def _metrics(*, extended=False, short=False):
        close = 80.0 if short else 120.0
        ma20 = 100.0
        atr14 = 10.0 if extended else 40.0
        rsi14 = 25.0 if short and extended else 75.0 if extended else 50.0
        return {
            timeframe: {
                "close": close,
                "ma20": ma20,
                "atr14": atr14,
                "rsi14": rsi14,
                "macd_hist": -1.0 if short else 1.0,
            }
            for timeframe in decision_briefing.DECISION_TIMEFRAMES
        }

    def test_long_state_machine_and_non_directional(self):
        metrics = self._metrics()
        self.assertEqual("ENTRY_READY", decision_briefing.classify_opportunity_state(
            {"4H": 2, "1H": 2, "15m": 2}, metrics)[0])
        self.assertEqual("EXTENDED", decision_briefing.classify_opportunity_state(
            {"4H": 2, "1H": 2, "15m": 2},
            self._metrics(extended=True))[0])
        self.assertEqual("TRIGGERING", decision_briefing.classify_opportunity_state(
            {"4H": 2, "1H": 2, "15m": -2}, metrics)[0])
        self.assertEqual("EARLY_WATCH", decision_briefing.classify_opportunity_state(
            {"4H": 2, "1H": 0, "15m": -2}, metrics)[0])
        self.assertEqual("NON_DIRECTIONAL", decision_briefing.classify_opportunity_state(
            {"4H": 0, "1H": 2, "15m": 2}, metrics)[0])

    def test_short_state_machine_is_mirrored(self):
        self.assertEqual("ENTRY_READY", decision_briefing.classify_opportunity_state(
            {"4H": -2, "1H": -2, "15m": -2},
            self._metrics(short=True))[0])
        self.assertEqual("EXTENDED", decision_briefing.classify_opportunity_state(
            {"4H": -2, "1H": -2, "15m": -2},
            self._metrics(extended=True, short=True))[0])
        self.assertEqual("TRIGGERING", decision_briefing.classify_opportunity_state(
            {"4H": -2, "1H": 2, "15m": -2},
            self._metrics(short=True))[0])
        self.assertEqual("EARLY_WATCH", decision_briefing.classify_opportunity_state(
            {"4H": -2, "1H": 0, "15m": 2},
            self._metrics(short=True))[0])

    def test_first_seen_context_survives_state_transition(self):
        first = {
            "row": {"symbol": "AAA-USDT-SWAP", "last": 100.0},
            "opportunity_side": "long",
            "opportunity_state": "EARLY_WATCH",
            "timeframe_metrics": self._metrics(),
        }
        decision_briefing.enrich_opportunity_context(
            first, "2026-09-01T04:00", "range",
            tick_ts="2026-08-31T20:00:00Z",
            regime_source_ts="2026-08-31T19:00:00Z",
        )
        previous = {
            "symbol": "AAA-USDT-SWAP", "side": "long", **{
                key: first.get(key) for key in (
                    "state_version", "opportunity_id", "opportunity_state",
                    "first_seen_cycle", "first_seen_ts_utc", "first_seen_price",
                    "state_entered_cycle", "regime_first_seen",
                    "regime_first_seen_source_ts", "initial_invalidation",
                )
            },
        }
        ready = {
            "row": {"symbol": "AAA-USDT-SWAP", "last": 105.0},
            "opportunity_side": "long",
            "opportunity_state": "ENTRY_READY",
            "timeframe_metrics": self._metrics(),
        }
        decision_briefing.enrich_opportunity_context(
            ready, "2026-09-01T04:15", "trend_up", previous,
            tick_ts="2026-08-31T20:15:00Z",
            regime_source_ts="2026-08-31T20:00:00Z",
        )
        self.assertEqual(first["opportunity_id"], ready["opportunity_id"])
        self.assertEqual(first["first_seen_cycle"], ready["first_seen_cycle"])
        self.assertEqual(first["first_seen_price"], ready["first_seen_price"])
        self.assertEqual(first["initial_invalidation"], ready["initial_invalidation"])
        self.assertEqual("EARLY_WATCH->ENTRY_READY", ready["state_transition"])
        self.assertEqual("2026-09-01T04:15", ready["state_entered_cycle"])

    def test_state_version_change_starts_new_episode(self):
        candidate = {
            "row": {"symbol": "AAA-USDT-SWAP", "last": 101.0},
            "opportunity_side": "long",
            "opportunity_state": "ENTRY_READY",
            "timeframe_metrics": self._metrics(),
        }
        decision_briefing.enrich_opportunity_context(
            candidate, "2026-09-01T04:15", "range", {
                "symbol": "AAA-USDT-SWAP", "side": "long",
                "state_version": "old", "opportunity_id": "opp_" + "a" * 20,
                "opportunity_state": "EARLY_WATCH",
            }, tick_ts="2026-08-31T20:15:00Z")
        self.assertNotEqual("opp_" + "a" * 20, candidate["opportunity_id"])
        self.assertEqual("2026-09-01T04:15", candidate["first_seen_cycle"])

    def test_absolute_24h_change_is_not_a_rank_component(self):
        base = {
            "row": {"symbol": "AAA-USDT-SWAP", "candidate_oi_usd": 8_000_000,
                    "chg24h": 2.0},
            "opportunity_state": "ENTRY_READY",
            "trend_strength": {"score": 1.0},
            "entry_timing": {"timing_score": 0.8},
            "quote_vol": 9_000_000,
        }
        other = {**base, "row": {**base["row"], "chg24h": 20.0}}
        self.assertEqual(
            decision_briefing.candidate_rank_key(base),
            decision_briefing.candidate_rank_key(other),
        )

    def test_state_versions_are_forward_and_historical_artifacts_refuse(self):
        self.assertIsNone(decision_briefing.opportunity_state_version_for_cycle(
            "2026-09-01T01:15"))
        self.assertEqual(
            "opportunity_state_v1",
            decision_briefing.opportunity_state_version_for_cycle(
                "2026-09-01T01:30"),
        )
        self.assertEqual(
            "opportunity_state_v2",
            decision_briefing.opportunity_state_version_for_cycle(
                "2026-09-01T02:45"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "historical.*refused"):
                decision_briefing._render(
                    str(Path(temporary) / "db"), 5,
                    cycle_id="2026-09-01T02:45",
                    candidate_out_file=Path(temporary) / "manifest.json",
                    ready_pool_out_file=Path(temporary) / "pool.json",
                )


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


class CandidateSnapshotTests(unittest.TestCase):
    """2026-08-18：候选快照 JSONL 写读契约（错失池/连续候选轮数的确定性来源）。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        # 函数从 db_root 的上一级派生 logs/briefing —— 仿真 <PROJECT_ROOT>\db 结构。
        self.root = str(Path(self._tmp.name) / "db")
        Path(self.root).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    @staticmethod
    def _picked(symbol, bias, last=1.0, chg=2.0):
        return {"bias": bias, "row": {
            "symbol": symbol, "last": last, "chg24h": chg}}

    @staticmethod
    def _early(symbol, side, last=1.0, chg=-3.0):
        return {"early_side": side, "row": {
            "symbol": symbol, "last": last, "chg24h": chg}}

    def test_append_and_load_roundtrip_with_dedupe(self):
        cyc = "2026-08-19T10:15"
        decision_briefing.append_candidate_snapshot(
            self.root, cyc,
            [self._picked("AAA-USDT-SWAP", "偏多"),
             self._picked("MIX-USDT-SWAP", "混合")],
            [self._early("BBB-USDT-SWAP", "short")],
            "2026-08-19T02:15:00Z")
        # 同 cycle 第二行（analyst/trader 双预读）——消费端必须取首行。
        decision_briefing.append_candidate_snapshot(
            self.root, cyc,
            [self._picked("CCC-USDT-SWAP", "偏空")], [],
            "2026-08-19T02:15:00Z")
        cycles = decision_briefing.load_candidate_snapshots(
            self.root, ["2026-08-19"])
        self.assertEqual(list(cycles), [cyc])
        m = cycles[cyc]
        self.assertEqual(m["AAA-USDT-SWAP"]["side"], "long")
        self.assertEqual(m["AAA-USDT-SWAP"]["layer"], "mature")
        self.assertEqual(m["BBB-USDT-SWAP"]["side"], "short")
        self.assertEqual(m["BBB-USDT-SWAP"]["layer"], "early")
        self.assertNotIn("MIX-USDT-SWAP", m, "混合票无方向不得进快照")
        self.assertNotIn("CCC-USDT-SWAP", m, "同 cycle 重复行必须取首行")

    def test_empty_candidates_line_still_recorded(self):
        cyc = "2026-08-19T10:30"
        decision_briefing.append_candidate_snapshot(
            self.root, cyc, [], [], None)
        cycles = decision_briefing.load_candidate_snapshots(
            self.root, ["2026-08-19"])
        self.assertEqual(cycles, {cyc: {}})

    def test_exact_manifest_is_atomic_hashed_ordered_and_rotation_bound(self):
        cyc = "2026-08-19T10:45"
        manifest_path = Path(self._tmp.name) / "candidate-manifest.json"
        manifest = decision_briefing.append_candidate_snapshot(
            self.root,
            cyc,
            [self._picked("AAA-USDT-SWAP", "偏多")],
            [self._early("BBB-USDT-SWAP", "short")],
            "2026-08-19T02:45:00Z",
            candidate_out_file=manifest_path,
            dig_history={
                ("AAA-USDT-SWAP", "long"): {
                    "n": 3, "rejected_n": 3,
                    "last_cycle": "2026-08-19T10:30",
                    "last_decision": "reject",
                },
            },
        )
        persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted, manifest)
        self.assertEqual([1, 2], [
            item["ordinal"] for item in persisted["candidates"]])
        self.assertRegex(
            persisted["candidates"][0]["candidate_id"],
            r"^cand_[0-9a-f]{20}$",
        )
        self.assertNotEqual(
            persisted["candidates"][0]["candidate_id"],
            persisted["candidates"][1]["candidate_id"],
        )
        self.assertTrue(
            persisted["candidates"][0]["new_evidence_required"])
        self.assertIn("prior_evidence_hash", persisted["candidates"][0])
        self.assertFalse(persisted["candidates"][0]["rotation_due"])
        self.assertEqual(
            3, persisted["candidates"][0]["recent_rejections_6h"])
        self.assertTrue(persisted["candidates"][1]["rotation_due"])
        core = dict(persisted)
        supplied = core.pop("manifest_sha256")
        canonical = json.dumps(
            core, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), supplied)

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            decision_briefing.load_candidate_snapshots(
                self.root, ["2026-08-19"]), {})


class CandidateStreakV2Tests(unittest.TestCase):
    """连续候选轮数 v2：连续出现在快照层且期间无成交（2026-08-18 语义升级）。"""

    def setUp(self):
        import tempfile
        from datetime import datetime
        self._tmp = tempfile.TemporaryDirectory()
        self.root = str(Path(self._tmp.name) / "db")
        Path(self.root).mkdir(parents=True, exist_ok=True)
        self.today = datetime.now(
            decision_briefing.CST).strftime("%Y-%m-%d")

    def tearDown(self):
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _write(self, hhmm, symbols):
        # This class pins the retired candidate-streak-v2 contract itself.
        # Do not let wall-clock passage across a forward activation silently
        # switch the fixture to the current side-neutral closure manifest.
        with mock.patch.object(
            decision_briefing.thresholds,
            "minimal_decision_contract_active",
            return_value=False,
        ):
            decision_briefing.append_candidate_snapshot(
                self.root, f"{self.today}T{hhmm}",
                [], [CandidateSnapshotTests._early(s, "long") for s in symbols],
                None)

    def test_streak_counts_consecutive_presence(self):
        self._write("10:00", ["AAA-USDT-SWAP"])
        self._write("10:15", ["AAA-USDT-SWAP"])
        self._write("10:30", [])  # 出层一轮 → 连续性断
        self._write("10:45", ["AAA-USDT-SWAP"])
        out = decision_briefing.candidate_streak_v2(
            self.root, ["AAA-USDT-SWAP"])
        self.assertEqual(out["AAA-USDT-SWAP"], (1, "long"))

    def test_trade_resets_streak(self):
        self._write("10:00", ["AAA-USDT-SWAP"])
        self._write("10:15", ["AAA-USDT-SWAP"])
        self._write("10:30", ["AAA-USDT-SWAP"])
        out = decision_briefing.candidate_streak_v2(
            self.root, ["AAA-USDT-SWAP"],
            {"AAA-USDT-SWAP": f"{self.today}T10:15"})
        self.assertEqual(out["AAA-USDT-SWAP"], (1, "long"))

    def test_no_snapshots_returns_empty_not_legacy(self):
        self.assertEqual(
            decision_briefing.candidate_streak_v2(
                self.root, ["AAA-USDT-SWAP"]), {})


class LoadDigHistoryTests(unittest.TestCase):
    """2026-08-28 轮换注解数据面：近 6h 深挖历史聚合。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        con = sqlite3.connect(f"{self.root}\\analysis.db")
        con.execute("CREATE TABLE analysis_runs (cycle_id TEXT, raw TEXT)")
        self.con = con
        self.addCleanup(self.con.close)  # LIFO：先关连接再删临时目录（Win 文件锁）

    def _insert(self, cycle_id, entries, inner_as_string=False):
        inner = {"candidates_deep_dived": entries}
        raw = {"raw": json.dumps(inner) if inner_as_string else inner}
        self.con.execute(
            "INSERT INTO analysis_runs VALUES (?,?)",
            (cycle_id, json.dumps(raw)))
        self.con.commit()

    def test_aggregates_counts_and_latest_decision(self):
        now = datetime(2026, 8, 28, 12, 0)
        self._insert("2026-08-28T08:00", [
            {"instId": "AAA-USDT-SWAP", "side": "long",
             "decision": "reject"}])
        # inner 为 JSON 字符串的双编码形态也要能读（生产两种都出现过）
        self._insert("2026-08-28T10:00", [
            {"instId": "AAA-USDT-SWAP", "side": "long",
             "decision": "open_long"},
            {"instId": "BBB-USDT-SWAP", "side": "short",
             "decision": "reject"},
        ], inner_as_string=True)
        # 6h 窗外的历史不计
        self._insert("2026-08-28T05:00", [
            {"instId": "CCC-USDT-SWAP", "side": "long",
             "decision": "reject"}])
        # 坏行逐行跳过，不拖垮整体
        self.con.execute(
            "INSERT INTO analysis_runs VALUES ('2026-08-28T11:00','{bad')")
        self.con.commit()
        history = decision_briefing.load_dig_history(self.root, now=now)
        self.assertEqual(history[("AAA-USDT-SWAP", "long")]["n"], 2)
        self.assertEqual(history[("AAA-USDT-SWAP", "long")]["last_cycle"],
                         "2026-08-28T10:00")
        self.assertEqual(history[("AAA-USDT-SWAP", "long")]["last_decision"],
                         "open_long")
        self.assertEqual(history[("BBB-USDT-SWAP", "short")]["n"], 1)
        self.assertNotIn(("CCC-USDT-SWAP", "long"), history)

    def test_future_rows_and_opposite_side_never_pollute_as_of_history(self):
        now = datetime(2026, 8, 28, 12, 0)
        self._insert("2026-08-28T11:45", [{
            "instId": "AAA-USDT-SWAP", "side": "long",
            "decision": "reject", "evidence_hash": "a" * 64,
        }])
        self._insert("2026-08-28T11:45", [{
            "instId": "AAA-USDT-SWAP", "side": "short",
            "decision": "reject", "evidence_hash": "b" * 64,
        }])
        self._insert("2026-08-28T12:15", [{
            "instId": "AAA-USDT-SWAP", "side": "long",
            "decision": "reject", "evidence_hash": "c" * 64,
        }])
        history = decision_briefing.load_dig_history(self.root, now=now)
        self.assertEqual(1, history[("AAA-USDT-SWAP", "long")]["n"])
        self.assertEqual("a" * 64, history[
            ("AAA-USDT-SWAP", "long")]["last_evidence_hash"])
        self.assertEqual(1, history[("AAA-USDT-SWAP", "short")]["n"])

    def test_empty_table_returns_empty(self):
        self.assertEqual(
            decision_briefing.load_dig_history(
                self.root, now=datetime(2026, 8, 28, 12, 0)), {})


if __name__ == "__main__":
    unittest.main()
