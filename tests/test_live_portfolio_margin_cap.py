# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import account_capacity as ac  # noqa: E402
import risk_validator as rv  # noqa: E402


class SettlementAccountImrTests(unittest.TestCase):
    @staticmethod
    def _payload(*, top_imr=None, detail_imr=None):
        row = {
            "totalEq": "1000",
            "details": [{
                "ccy": "USDT",
                "eq": "900",
                "availBal": "800",
                "availEq": "750",
            }],
        }
        if top_imr is not None:
            row["imr"] = top_imr
        if detail_imr is not None:
            row["details"][0]["imr"] = detail_imr
        return {"ok": True, "data": [row]}

    def test_top_level_account_imr_is_authoritative(self):
        capacity = ac.extract_settlement_capacity(
            self._payload(top_imr="123.45", detail_imr="456.78"),
            "USDT",
        )

        self.assertTrue(capacity["ok"])
        self.assertEqual(capacity["available_margin"], 750.0)
        self.assertEqual(capacity["account_imr"], 123.45)
        self.assertEqual(
            capacity["account_imr_source"], "account.balance.imr")

    def test_details_usdt_imr_is_the_only_fallback(self):
        capacity = ac.extract_settlement_capacity(
            self._payload(detail_imr="234.56"),
            "USDT",
        )

        self.assertTrue(capacity["ok"])
        self.assertEqual(capacity["account_imr"], 234.56)
        self.assertEqual(
            capacity["account_imr_source"],
            "account.balance.details.USDT.imr",
        )

    def test_invalid_authoritative_imr_fails_closed_without_detail_fallback(self):
        for invalid_top_imr in ("bad", "NaN", "-1"):
            with self.subTest(top_imr=invalid_top_imr):
                capacity = ac.extract_settlement_capacity(
                    self._payload(
                        top_imr=invalid_top_imr,
                        detail_imr="234.56",
                    ),
                    "USDT",
                )

                self.assertTrue(capacity["ok"])
                self.assertIsNone(capacity["account_imr"])
                self.assertEqual(
                    capacity["account_imr_source"], "account.balance.imr")
                self.assertEqual(
                    capacity["account_imr_error"], "account_imr_invalid")

    def test_missing_imr_is_not_inferred_from_total_equity_minus_available(self):
        capacity = ac.extract_settlement_capacity(self._payload(), "USDT")

        self.assertTrue(capacity["ok"])
        self.assertEqual(capacity["total_equity"], 1000.0)
        self.assertEqual(capacity["available_margin"], 750.0)
        self.assertIsNone(capacity["account_imr"])
        self.assertIsNone(capacity["account_imr_source"])
        self.assertNotEqual(
            capacity["account_imr"],
            capacity["total_equity"] - capacity["available_margin"],
        )


class LivePortfolioMarginCapTests(unittest.TestCase):
    @staticmethod
    def _live_validate(**overrides):
        kwargs = {
            "symbol": "BTC-USDT-SWAP",
            "side": "long",
            "intended_sz": 10.0,
            "lev": 5.0,
            "mark_px": 100.0,
            "ct_val": 1.0,
            "lot_sz": 1.0,
            "equity": 1000.0,
            "available_margin": 1000.0,
            "account_imr": 0.0,
            "open_positions": [],
            "sl_trigger_px": 95.0,
            "profile": "live",
        }
        kwargs.update(overrides)
        return rv.validate(**kwargs)

    def test_incremental_order_imr_above_15pct_is_clamped_below_portfolio_cap(self):
        # 2026-08-08 契约反转：本测试原名 *_above_20pct_is_allowed_*，断言
        # 「单笔 >20% 只要组合 ≤66.6% 就放行」——SNDK 单笔 60% 净值事故后，
        # 单笔增量保证金 >15% 改为按 lotSz 缩量（定仓预算 14.7%）。
        result = self._live_validate(
            intended_sz=25.0,
            account_imr=100.0,
        )

        self.assertTrue(result["approved"])
        self.assertEqual(result["approved_sz"], 7.0)
        self.assertTrue(result["clamped"])
        self.assertTrue(
            any("单笔保证金上限" in a for a in result["adjustments"]))
        math_box = result["math"]
        self.assertEqual(math_box["max_sz_single_order"], 7.0)
        self.assertEqual(math_box["incremental_order_imr"], 140.0)
        self.assertLessEqual(
            math_box["single_order_imr_ratio"],
            rv.MAX_SINGLE_ORDER_IMR_RATIO)
        self.assertEqual(math_box["projected_account_imr"], 240.0)
        self.assertAlmostEqual(
            math_box["projected_portfolio_imr_ratio"], 0.24)
        self.assertEqual(
            math_box["max_portfolio_imr_ratio"],
            rv.MAX_PORTFOLIO_IMR_RATIO,
        )

    def test_exactly_66_6pct_is_allowed(self):
        # 2026-08-08 构造调整：改用 7 张（增量 140 = 单笔预算内）+ 更高存量 IMR
        # 逼出精确 66.6% 边界；原 10 张构造会先撞单笔闸，测不到组合边界语义。
        result = self._live_validate(
            intended_sz=7.0,
            account_imr=526.0,
        )

        self.assertTrue(result["approved"])
        self.assertEqual(result["approved_sz"], 7.0)
        self.assertFalse(result["clamped"])
        self.assertEqual(result["math"]["projected_account_imr"], 666.0)
        self.assertAlmostEqual(
            result["math"]["projected_portfolio_imr_ratio"], 0.666)
        self.assertEqual(
            result["math"]["portfolio_imr_ratio_unit"], "fraction")
        self.assertEqual(
            result["math"]["portfolio_imr_source"], "account.balance.imr")

    def test_above_66_6pct_rejects_the_whole_order_without_clamp(self):
        # 2026-08-08 构造调整：同上改 7 张 + 存量 526.1，保持越界 0.6661 不变。
        result = self._live_validate(
            intended_sz=7.0,
            account_imr=526.1,
        )

        self.assertFalse(result["approved"])
        self.assertIsNone(result["approved_sz"])
        self.assertFalse(result["clamped"])
        self.assertEqual(
            result["reject_reason"], "portfolio_margin_cap_exceeded")
        self.assertEqual(result["math"]["incremental_order_imr"], 140.0)
        self.assertAlmostEqual(
            result["math"]["projected_portfolio_imr_ratio"], 0.6661)
        self.assertEqual(result["adjustments"], [])

    def test_new_order_is_rejected_when_account_is_already_at_cap(self):
        result = self._live_validate(
            intended_sz=1.0,
            account_imr=666.0,
        )

        self.assertFalse(result["approved"])
        self.assertEqual(
            result["reject_reason"], "portfolio_margin_cap_exceeded")
        self.assertAlmostEqual(
            result["math"]["current_portfolio_imr_ratio"], 0.666)
        self.assertAlmostEqual(
            result["math"]["projected_portfolio_imr_ratio"], 0.686)

    def test_available_margin_98pct_clamp_remains_effective(self):
        result = self._live_validate(
            intended_sz=10.0,
            available_margin=100.0,
            account_imr=0.0,
        )

        self.assertTrue(result["approved"])
        self.assertTrue(result["clamped"])
        self.assertEqual(result["approved_sz"], 4.0)
        self.assertEqual(result["math"]["available_margin_budget"], 98.0)
        self.assertEqual(result["math"]["max_sz_available"], 4.0)
        self.assertEqual(result["math"]["incremental_order_imr"], 80.0)
        self.assertIn(
            "当前可用保证金上限", "；".join(result["adjustments"]))

    def test_missing_or_invalid_live_account_imr_is_rejected(self):
        cases = [
            (None, "account_imr_missing"),
            ("bad", "bad_account_imr"),
            (math.nan, "bad_account_imr"),
            (math.inf, "bad_account_imr"),
            (-1.0, "bad_account_imr"),
        ]
        for account_imr, expected_reason in cases:
            with self.subTest(account_imr=account_imr):
                result = self._live_validate(account_imr=account_imr)
                self.assertFalse(result["approved"])
                self.assertEqual(result["reject_reason"], expected_reason)
                self.assertIsNone(result["approved_sz"])

    # `test_demo_ignores_imr_and_keeps_exchange_max_size_policy` 随 2026-08-06
    # demo 全量下线删除：它断言的 `sizing_policy=okx_demo_max_size_only` 分支
    # （绕过 IMR 闸、只按交易所 max-size 收敛）在 risk_validator 中已整块移除。

    def test_add_uses_existing_leverage_for_incremental_imr(self):
        result = self._live_validate(
            intended_sz=5.0,
            lev=10.0,
            account_imr=600.0,
            open_positions=[{
                "symbol": "BTC-USDT-SWAP",
                "side": "long",
                "sz": 1.0,
                "lev": 5.0,
            }],
        )

        self.assertFalse(result["approved"])
        self.assertEqual(
            result["reject_reason"], "portfolio_margin_cap_exceeded")
        self.assertEqual(result["math"]["requested_lev"], 10.0)
        self.assertEqual(result["math"]["effective_lev"], 5.0)
        self.assertEqual(result["math"]["per_contract_margin"], 20.0)
        self.assertEqual(result["math"]["incremental_order_imr"], 100.0)
        self.assertAlmostEqual(
            result["math"]["projected_portfolio_imr_ratio"], 0.70)
        self.assertIn(
            "加仓沿用现仓杠杆 5x", "；".join(result["adjustments"]))


if __name__ == "__main__":
    unittest.main()
