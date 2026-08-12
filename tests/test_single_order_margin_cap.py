# -*- coding: utf-8 -*-
"""单笔增量保证金 15% 闸（2026-08-08）——validator/facts/budget 契约测试。

覆盖：SNDK 事故复现（60% → 按生产规格 lotSz=0.001 缩量 1.225 张）、小单不受扰、加仓沿用现仓杠杆口径、
最小单位/名义下限与单笔预算冲突的整笔拒绝、缩量后组合 66.6% 仍整单拒绝、
facts 动作表与 open_add gate 联动 + 单笔预算字段、position_budget 同口径。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts", ROOT / "collectors"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core import order_executor as oe  # noqa: E402
from core import risk_validator as rv  # noqa: E402
from scripts import live_decision_facts as facts  # noqa: E402
from scripts import render_push_report as rpr  # noqa: E402
from tests.test_live_decision_facts import _raw_inputs  # noqa: E402


def _validate(**over):
    """SNDK 2026-08-08T06:30 实况为基线。

    数字取自 live_trades/account 落账；ctVal=1.0、lotSz=0.001 为
    market.db.instruments_cache 生产实查值（2026-08-08 核实——早版测试误用
    lotSz=1.0，复现值 1 张与真实规格 1.225 张不符）。
    """
    kw = dict(
        symbol="SNDK-USDT-SWAP", side="short", intended_sz=5.0, lev=10.0,
        mark_px=1212.49, ct_val=1.0, lot_sz=0.001, equity=1011.04,
        open_positions=[], sl_trigger_px=None, profile="live",
        available_margin=973.34, account_imr=38.4,
    )
    kw.update(over)
    return rv.validate(**kw)


class SingleOrderCapValidatorTests(unittest.TestCase):
    def test_sndk_replay_with_production_specs(self):
        """事故复现（真实规格 lotSz=0.001）：5 张(60%净值) → 1.225 张(14.69%)。"""
        v = _validate()
        self.assertTrue(v["approved"])
        self.assertEqual(v["approved_sz"], 1.225)
        self.assertTrue(v["clamped"])
        self.assertTrue(any("单笔保证金上限" in a for a in v["adjustments"]))
        m = v["math"]
        self.assertEqual(m["max_sz_single_order"], 1.225)
        self.assertAlmostEqual(m["single_order_imr_ratio"], 0.146908, places=6)
        self.assertLess(m["single_order_imr_ratio"], rv.MAX_SINGLE_ORDER_IMR_RATIO)
        self.assertAlmostEqual(
            m["single_order_sizing_budget"],
            1011.04 * 0.15 * 0.98, places=6)

    def test_integer_lot_granularity_floors_to_whole_contract(self):
        """整数 lot 合成用例：lotSz=1 时同一预算只允许 1 整张（取整分支覆盖）。"""
        v = _validate(lot_sz=1.0)
        self.assertTrue(v["approved"])
        self.assertEqual(v["approved_sz"], 1.0)
        self.assertTrue(v["clamped"])
        self.assertEqual(v["math"]["max_sz_single_order"], 1.0)

    def test_small_order_untouched(self):
        v = _validate(intended_sz=1.0)
        self.assertTrue(v["approved"])
        self.assertEqual(v["approved_sz"], 1.0)
        self.assertFalse(v["clamped"])
        self.assertEqual(v["adjustments"], [])

    def test_add_uses_existing_position_leverage_for_cap(self):
        """加仓：现仓 5x（请求 10x 不生效）→ 每张保证金翻倍 → 单笔上限张数减半。"""
        v = _validate(
            symbol="X-USDT-SWAP", side="long", intended_sz=10.0, lev=10.0,
            mark_px=100.0, ct_val=1.0, lot_sz=1.0, equity=1000.0,
            available_margin=2000.0, account_imr=100.0,
            open_positions=[{
                "symbol": "X-USDT-SWAP", "side": "long", "lev": "5",
                "notional": 1000.0,
            }],
        )
        self.assertTrue(v["approved"])
        # budget=1000×0.147=147，每张保证金 100/5=20 → floor(147/20)=7
        self.assertEqual(v["approved_sz"], 7.0)
        self.assertEqual(v["math"]["effective_lev"], 5.0)
        self.assertTrue(any("单笔保证金上限" in a for a in v["adjustments"]))
        self.assertTrue(any("沿用现仓杠杆" in a for a in v["adjustments"]))

    def test_min_unit_exceeding_budget_rejects(self):
        """最小 1 lot 的保证金 > 单笔预算 → 整笔拒绝，不越权放行。"""
        v = _validate(
            symbol="EXP-USDT-SWAP", intended_sz=1.0, lev=1.0,
            mark_px=2000.0, ct_val=1.0, lot_sz=1.0, equity=1000.0,
            available_margin=3000.0, account_imr=0.0,
        )
        self.assertFalse(v["approved"])
        self.assertEqual(v["reject_reason"], "single_order_cap_infeasible")

    def test_notional_floor_conflict_rejects_not_clamps_up(self):
        """名义 1% 下限张数 > 单笔上限张数 → 拒绝，禁止向上抬升绕闸。

        构造值（lev=0.05）现实中不会出现，仅覆盖该拒绝分支。
        """
        v = _validate(
            symbol="EDGE-USDT-SWAP", intended_sz=5.0, lev=0.05,
            mark_px=1.0, ct_val=1.0, lot_sz=1.0, equity=1000.0,
            available_margin=5000.0, account_imr=0.0,
        )
        self.assertFalse(v["approved"])
        self.assertEqual(v["reject_reason"], "single_order_cap_infeasible")

    def test_portfolio_cap_still_whole_order_rejects_after_single_cap(self):
        """单笔闸内的订单撞 66.6% 组合闸 → 仍整笔拒绝，不缩量绕闸。"""
        v = _validate(
            symbol="Y-USDT-SWAP", side="long", intended_sz=8.0, lev=10.0,
            mark_px=100.0, ct_val=1.0, lot_sz=1.0, equity=1000.0,
            available_margin=2000.0, account_imr=600.0,
        )
        self.assertFalse(v["approved"])
        self.assertEqual(v["reject_reason"], "portfolio_margin_cap_exceeded")

    def test_available_clamp_can_bind_before_single_cap(self):
        """可用资金比单笔预算更紧时，先按可用资金缩，再按单笔预算缩。"""
        v = _validate(
            symbol="Z-USDT-SWAP", side="long", intended_sz=50.0, lev=10.0,
            mark_px=100.0, ct_val=1.0, lot_sz=1.0, equity=10000.0,
            available_margin=50.0, account_imr=0.0,
        )
        # 可用 50×0.98=49 → 4 张；单笔预算 1470 → 147 张（不束缚）
        self.assertTrue(v["approved"])
        self.assertEqual(v["approved_sz"], 4.0)
        self.assertTrue(any("可用保证金上限" in a for a in v["adjustments"]))
        self.assertFalse(any("单笔保证金上限" in a for a in v["adjustments"]))


class SingleOrderBudgetHelperTests(unittest.TestCase):
    def test_position_budget_reports_single_order_fields(self):
        b = rv.position_budget(
            mark_px=100.0, ct_val=1.0, lot_sz=1.0, equity=1000.0, lev=10.0,
            available_margin=2000.0, account_imr=0.0)
        self.assertTrue(b["ok"])
        self.assertEqual(b["max_sz_single_order"], 14.0)
        self.assertAlmostEqual(b["single_order_sizing_budget"], 147.0)
        self.assertEqual(
            b["max_single_order_imr_ratio"], rv.MAX_SINGLE_ORDER_IMR_RATIO)
        self.assertTrue(b["feasible"])


class PostFillAuditTests(unittest.TestCase):
    """executor 成交后单笔保证金复审（纯函数 _single_order_fill_audit）。"""

    def test_sndk_actual_fill_breaches(self):
        ratio, breached = oe._single_order_fill_audit(606.245, 1011.04)
        self.assertAlmostEqual(ratio, 0.5996, places=4)
        self.assertTrue(breached)

    def test_within_cap_not_breached(self):
        ratio, breached = oe._single_order_fill_audit(121.249, 1011.04)
        self.assertAlmostEqual(ratio, 0.1199, places=4)
        self.assertFalse(breached)

    def test_exact_boundary_not_breached(self):
        ratio, breached = oe._single_order_fill_audit(150.0, 1000.0)
        self.assertAlmostEqual(ratio, 0.15)
        self.assertFalse(breached)

    def test_just_above_boundary_breached(self):
        _, breached = oe._single_order_fill_audit(150.01, 1000.0)
        self.assertTrue(breached)

    def test_invalid_inputs_return_none_not_guess(self):
        for margin, equity in ((None, 1000.0), (100.0, None), (100.0, 0.0),
                               ("bad", 1000.0), (float("nan"), 1000.0),
                               (-1.0, 1000.0)):
            with self.subTest(margin=margin, equity=equity):
                ratio, breached = oe._single_order_fill_audit(margin, equity)
                self.assertIsNone(ratio)
                self.assertIsNone(breached)


class PushSegmentTests(unittest.TestCase):
    """推送风控行「本单保证金」段（render_push_report._single_order_risk_segment）。"""

    @staticmethod
    def _payload(**trade_over):
        trade = {"symbol": "SNDK-USDT-SWAP", "action": "open", "side": "short",
                 "sz": 5.0, "single_order_imr_ratio": 0.5996,
                 "max_single_order_imr_ratio": 0.15}
        trade.update(trade_over)
        return {"trades": {"live": [trade]}}

    def test_breach_marker_wins(self):
        seg = rpr._single_order_risk_segment(
            self._payload(single_order_cap_breached=True, risk_clamped=True))
        self.assertEqual(seg, " | 本单保证金 60.0%/限15%(滑点超限,已入修复队列)")

    def test_clamped_marker(self):
        seg = rpr._single_order_risk_segment(
            self._payload(single_order_imr_ratio=0.147, risk_clamped=True,
                          single_order_cap_breached=False))
        self.assertEqual(seg, " | 本单保证金 14.7%/限15%(已缩量)")

    def test_plain_open_no_marker(self):
        seg = rpr._single_order_risk_segment(
            self._payload(single_order_imr_ratio=0.05))
        self.assertEqual(seg, " | 本单保证金 5.0%/限15%")

    def test_hold_round_renders_nothing(self):
        self.assertEqual(
            rpr._single_order_risk_segment({"trades": {"live": []}}), "")

    def test_trade_without_audit_fields_renders_nothing(self):
        payload = {"trades": {"live": [{"symbol": "X", "action": "open",
                                        "side": "long", "sz": 1.0}]}}
        self.assertEqual(rpr._single_order_risk_segment(payload), "")

    def test_close_only_round_renders_nothing(self):
        seg = rpr._single_order_risk_segment(
            self._payload(action="close"))
        self.assertEqual(seg, "")


class FactsSingleOrderTests(unittest.TestCase):
    def _derive(self, balance_mutator=None):
        stamp = int(time.time() * 1000)
        positions, balance, instruments, algos = _raw_inputs(stamp)
        if balance_mutator:
            balance_mutator(balance)
        return facts.derive_facts(
            "2026-08-08T10:15", "live", positions, balance, instruments, algos,
            as_of_ms=stamp,
        )

    def test_balance_carries_single_order_budget(self):
        payload = self._derive()
        bal = payload["balance"]
        self.assertEqual(
            bal["max_single_order_imr_ratio"], rv.MAX_SINGLE_ORDER_IMR_RATIO)
        self.assertAlmostEqual(
            bal["single_order_margin_budget_usdt"],
            round(1013.76 * 0.15 * 0.98, 8))
        self.assertEqual(
            bal["single_order_budget_scope"],
            "next_incremental_open_or_add_order")
        self.assertFalse(
            bal["single_order_budget_reduced_by_existing_position"])
        policy = payload["action_policy"]
        self.assertTrue(policy["open_add_allowed_by_facts"])
        self.assertEqual(
            policy["allowed_executor_actions"],
            ["open", "add", "close", "reduce"])

    def test_actions_drop_open_add_when_ratio_gate_false(self):
        """组合 IMR 越 66.6% → open_add gate=False，动作表必须同步剔除 open/add。"""
        def _inflate_imr(balance):
            balance[0]["details"][0]["imr"] = "700"

        payload = self._derive(_inflate_imr)
        policy = payload["action_policy"]
        self.assertFalse(policy["open_add_allowed_by_facts"])
        self.assertEqual(
            policy["allowed_executor_actions"], ["close", "reduce"])
        self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()
