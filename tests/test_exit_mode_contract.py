# -*- coding: utf-8 -*-
"""exit_mode 三方口径收口（2026-08-20）。

**起因**：经验 289（LINK 多）开于 2026-08-11 16:27、持有 7 天平于 08-18 10:44，
其决策卡 `risk_reward` 无 `exit_mode` 键 → `exit_quality` 判 blocked → rc=2 →
关键步被拒 → **2026-08-19 日报整份不存在**。

**它不是 Agent 违规**：`tmp/archive/source-snapshot-20260811-161206`
（2026-08-11 16:12:06 的快照）里 live_trader.md 还没有 exit_mode 强制条款，比
289 开仓早 15 分钟。按开仓日统计也印证：08-13 及之前 100% 缺失、08-14 起 100%
具备（39 张卡零缺失）。

**分叉的三层**（本用例逐层钉死）：
  1. `agents/live_trader.md`：open_* 必须写 exit_mode（强制）
  2. `core.decision_card.validate_card`：**有才校验值**，缺键放行 —— 规则只活在
     `analyst_writer` 的本地检查里，共用本函数的另三个写方全不知道
  3. `exit_quality._original_plan`：缺键 → blocked → 一路 fail-closed 到日报

修法：①把存在性检查提进共享校验器（opt-in，`ledger_autoheal` 复放老卡时不启用）；
②`exit_quality` 的 legacy 判据从 **closed_at** 改看 **开仓时刻** —— 字段是开仓时
写进卡的，用平仓时刻判会把横跨边界的持仓错判成数据坏。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import exit_quality  # noqa: E402
from core.decision_card import validate_card  # noqa: E402


def _card(exit_mode=..., **overrides):
    """一张其余字段齐备的卡；exit_mode 用 ... 表示「整个键不存在」。"""
    risk_reward = {"entry": 8.47, "stop": 8.25, "target": 8.89, "rr": 1.92}
    if exit_mode is not ...:
        risk_reward["exit_mode"] = exit_mode
    card = {
        "direction_evidence": ["a"],
        "opposing_evidence": ["b"],
        "execution_conditions": "c",
        "invalidation_point": "d",
        "risk_reward": risk_reward,
        "portfolio_impact": "e",
        "historical_experience": {
            "matched_wins": [], "matched_losses": [],
            "missed_opportunities": [], "usage": "none", "reason": "r",
        },
        "agent_judgement": "j",
        "reference_overrides": [],
    }
    card.update(overrides)
    return card


def _exit_mode_errors(errors):
    return [e for e in errors if "exit_mode" in e]


class SharedValidatorExitModeTests(unittest.TestCase):
    """①：存在性检查进共享校验器，且必须是 opt-in。"""

    def test_missing_key_passes_by_default(self):
        """默认不变 —— `ledger_autoheal` 复放 2026-08-14 前的老卡必须仍能过。

        无条件必填会让补账对老仓直接失败，把交易冻在那里，比漏检更糟。
        """
        self.assertEqual([], _exit_mode_errors(validate_card(_card())))

    def test_missing_key_rejected_when_required(self):
        errors = _exit_mode_errors(
            validate_card(_card(), require_exit_mode=True))
        self.assertEqual(1, len(errors), errors)
        self.assertIn("缺失", errors[0])

    def test_valid_value_accepted_when_required(self):
        for mode in ("fixed_tp", "dynamic_exit", "no_fixed_tp"):
            with self.subTest(mode=mode):
                self.assertEqual([], _exit_mode_errors(validate_card(
                    _card(mode), require_exit_mode=True)))

    def test_invalid_value_rejected_regardless_of_flag(self):
        """取值非法与缺键是两回事：前者任何时候都是错。"""
        for flag in (False, True):
            with self.subTest(require=flag):
                errors = _exit_mode_errors(validate_card(
                    _card("trailing"), require_exit_mode=flag))
                self.assertEqual(1, len(errors), errors)
                self.assertIn("必须是", errors[0])

    def test_missing_and_invalid_produce_distinguishable_messages(self):
        """「没写」和「写错」的排查方向不同，报错必须能分辨。"""
        missing = _exit_mode_errors(
            validate_card(_card(), require_exit_mode=True))[0]
        invalid = _exit_mode_errors(
            validate_card(_card("trailing"), require_exit_mode=True))[0]
        self.assertNotEqual(missing, invalid)

    def test_analyst_writer_no_longer_hardcodes_its_own_copy(self):
        """本地第二份实现必须已退役，否则规则又分叉回两处。"""
        src = (ROOT / "collectors" / "analyst_writer.py").read_text(
            encoding="utf-8")
        self.assertNotIn("exit_mode 开仓时必须显式为", src)
        self.assertIn("require_exit_mode=_is_open", src)

    def test_order_executor_gates_on_a_preregistered_forward_boundary(self):
        from core import order_executor
        self.assertEqual(
            "2026-08-21T00:00", order_executor.EXIT_MODE_REQUIRED_FROM_CYCLE)


class LegacyExitModeBoundaryTests(unittest.TestCase):
    """②：legacy 判据必须看开仓时刻，不是平仓时刻。"""

    def setUp(self):
        self._cons: list[sqlite3.Connection] = []

    def tearDown(self):
        for con in self._cons:
            con.close()

    def _con(self, ddl: str) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(ddl)
        self._cons.append(con)
        return con

    def _row(self, ts, exit_mode=...):
        import json
        con = self._con(
            "CREATE TABLE t(id INT, ts TEXT, closed_at TEXT, raw TEXT)")
        con.execute(
            "INSERT INTO t VALUES(?,?,?,?)",
            (289, ts, "2026-08-18 10:44:22",
             json.dumps({"decision_card": _card(exit_mode)},
                        ensure_ascii=False)))
        return con.execute("SELECT id,ts,closed_at,raw FROM t").fetchone()

    def test_opened_before_mandate_with_missing_key_is_not_applicable(self):
        """289 的真实形态：开仓早于强制日、平仓晚于反事实边界。"""
        plan, state, reasons = exit_quality._original_plan(
            self._row("2026-08-11 16:27:42"))
        self.assertEqual("not_applicable", state)
        self.assertIn("exit_mode_legacy_before_mandate", reasons)
        self.assertIsNone(plan)

    def test_opened_after_mandate_with_missing_key_still_blocks(self):
        """强制生效后再缺就是真缺口，不得被豁免掉。"""
        _, state, reasons = exit_quality._original_plan(
            self._row("2026-08-14 09:00:00"))
        self.assertEqual("blocked", state)
        self.assertIn("original_plan_exit_mode_invalid_or_missing", reasons)

    def test_legacy_row_with_invalid_value_still_blocks(self):
        """豁免只给「缺键」；写了个非法值是真数据坏，老卡也不放行。"""
        _, state, reasons = exit_quality._original_plan(
            self._row("2026-08-11 16:27:42", "trailing"))
        self.assertEqual("blocked", state)
        self.assertIn("original_plan_exit_mode_invalid_or_missing", reasons)

    def test_missing_open_timestamp_fails_closed(self):
        """取不到开仓时刻 ⇒ 证明不了是 legacy ⇒ 宁可 blocked，不凭空豁免。"""
        import json
        con = self._con("CREATE TABLE t(id INT, closed_at TEXT, raw TEXT)")
        con.execute("INSERT INTO t VALUES(?,?,?)", (
            289, "2026-08-18 10:44:22",
            json.dumps({"decision_card": _card()}, ensure_ascii=False)))
        row = con.execute("SELECT id,closed_at,raw FROM t").fetchone()
        self.assertFalse(exit_quality._opened_before_exit_mode_mandate(row))
        _, state, _ = exit_quality._original_plan(row)
        self.assertEqual("blocked", state)

    def test_null_open_timestamp_fails_closed(self):
        self.assertFalse(exit_quality._opened_before_exit_mode_mandate(
            self._row(None)))

    def test_boundary_is_the_empirically_observed_cutover(self):
        """08-13 及之前 100% 缺失、08-14 起 100% 具备 —— 边界即取该转折点。"""
        self.assertEqual(
            "2026-08-14 00:00:00", exit_quality.EXIT_MODE_MANDATE_FROM)
        self.assertTrue(exit_quality._opened_before_exit_mode_mandate(
            self._row("2026-08-13 23:59:59")))
        self.assertFalse(exit_quality._opened_before_exit_mode_mandate(
            self._row("2026-08-14 00:00:00")))

    def test_missed_take_profit_query_carries_open_timestamp(self):
        """判据靠 row['ts']，查询必须真的把它选出来，否则永远 fail-closed。"""
        src = (ROOT / "scripts" / "exit_quality.py").read_text(encoding="utf-8")
        self.assertIn('ts_expr = "ts" if "ts" in columns else "NULL AS ts"', src)
        self.assertIn('"SELECT id,profile,symbol,side," + ts_expr', src)

    def test_missing_ts_column_does_not_break_the_step(self):
        """老库/隔离夹具可能没有 ts 列。

        直接 SELECT 会抛 OperationalError：既把 exit_quality 打成 rc=2，又让
        compute() 已开的连接漏掉（Windows 上表现为 tempdir 清理 WinError 32）。
        缺列必须走 NULL，判据侧自然 fail-closed。
        """
        con = self._con(
            "CREATE TABLE trade_experiences("
            "id INT, profile TEXT, symbol TEXT, side TEXT, "
            "action TEXT, status TEXT, closed_at TEXT, raw TEXT)")
        columns = {str(r[1]) for r in con.execute(
            "PRAGMA table_info(trade_experiences)")}
        self.assertNotIn("ts", columns)
        ts_expr = "ts" if "ts" in columns else "NULL AS ts"
        row = con.execute(
            "SELECT id,profile,symbol,side," + ts_expr + ",closed_at,raw "
            "FROM trade_experiences").fetchone()
        self.assertIsNone(row)   # 查询本身必须能编译通过，不抛 OperationalError


if __name__ == "__main__":
    unittest.main()
