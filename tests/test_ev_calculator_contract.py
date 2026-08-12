# -*- coding: utf-8 -*-
"""Wave1 序5 —— RR/净 EV 计算器与决策卡算术一致性（终稿 T2）。

验收锚：DOT 2026-08-10T11:15 卡回放——entry 0.8025/target 0.76/stop 0.85 的
几何 gross_rr≈0.895，卡字段 rr=1.0 必须拒写；负 EV 凭结构化 ev_override 继续；
历史平均收益不得代替本笔 EV（p_win 只认具名 scope 的 wins/n）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ev_calculator import (  # noqa: E402
    build_ev_check,
    evidence_p_win,
    wilson_ci95,
)


def _contract(exact_n=0, exact_wins=0, same_n=0, same_wins=0,
              cross_n=0, cross_wins=0):
    def s(scope, n, wins):
        return {"scope": scope, "n": n, "wins": wins, "losses": n - wins}
    return {
        "protocol": "experience_evidence_v1",
        "summaries": {
            "exact_setup": s("same_symbol_side_action_regime",
                             exact_n, exact_wins),
            "same_symbol_similar": s("same_symbol_similar", same_n, same_wins),
            "cross_symbol_similar": s("cross_symbol_similar",
                                      cross_n, cross_wins),
        },
    }


def _dot_card(rr_field=1.0, override=None, contract=None):
    rr = {"entry": 0.8025, "target": 0.76, "stop": 0.85, "rr": rr_field}
    if override is not None:
        rr["ev_override"] = override
    return {
        "risk_reward": rr,
        "historical_experience": {
            "evidence_contract": contract if contract is not None
            else _contract(cross_n=104, cross_wins=41),  # 39.42% WR
        },
    }


class EvCalculatorTests(unittest.TestCase):
    def test_dot_replay_rr_field_conflict_rejected(self):
        """T2：DOT 卡 rr=1.0 vs 几何 0.895 → 拒写。"""
        _, errors = build_ev_check(_dot_card(rr_field=1.0), "short")
        self.assertTrue(any("rr=1.0" in e and "矛盾" in e for e in errors),
                        errors)

    def test_dot_replay_negative_ev_requires_override(self):
        """T2：rr 修正后（0.9），39.42% 胜率下 EV 为负 → 必须带 ev_override。"""
        block, errors = build_ev_check(_dot_card(rr_field=0.9), "short")
        self.assertTrue(any("ev_override" in e for e in errors), errors)

    def test_dot_replay_override_allows_negative_ev(self):
        """负 EV + 合规 override → 放行（判断自由），并算出 claim_ev_r。"""
        block, errors = build_ev_check(
            _dot_card(rr_field=0.9, override={
                "reason": "ETF 撤回催化未被定价，短端结构一致",
                "p_win_claim": 0.58,
            }), "short")
        self.assertEqual(errors, [], errors)
        self.assertEqual(block["status"], "computed")
        self.assertLess(block["ev_r"], 0)
        self.assertTrue(block["override_present"])
        self.assertIsNotNone(block["claim_ev_r"])
        # 基线数字与人工核验一致：gross_rr≈0.895、盈亏平衡≈54.5%（净口径略高）
        self.assertAlmostEqual(block["gross_rr"], 0.8947, places=3)
        self.assertGreater(block["breakeven_p"], 0.53)

    def test_indeterminate_when_no_scope_has_enough_samples(self):
        """全 scope n<5 → indeterminate：无 EV 要求（n=1 也照给的原则不变）。"""
        block, errors = build_ev_check(
            _dot_card(rr_field=0.9, contract=_contract(
                exact_n=1, exact_wins=1, same_n=2, same_wins=0,
                cross_n=4, cross_wins=2)), "short")
        self.assertEqual(errors, [], errors)
        self.assertEqual(block["status"], "indeterminate")
        self.assertIsNone(block["ev_r"])

    def test_geometry_invalid_rejected(self):
        """short 的 target 高于 entry → 几何非法拒写。"""
        card = _dot_card()
        card["risk_reward"]["target"] = 0.86
        card["risk_reward"]["stop"] = 0.85
        _, errors = build_ev_check(card, "short")
        self.assertTrue(any("几何非法" in e for e in errors), errors)

    def test_missing_numbers_rejected(self):
        card = {"risk_reward": {"entry": 0.8, "note": "target 见文字"},
                "historical_experience": {}}
        _, errors = build_ev_check(card, "short")
        self.assertTrue(any("entry/stop/target" in e for e in errors), errors)

    def test_scope_priority_exact_over_cross(self):
        """p_win 取首个 n≥5 scope：exact 优先于 cross。"""
        info = evidence_p_win(_contract(
            exact_n=6, exact_wins=4, cross_n=104, cross_wins=41))
        self.assertEqual(info["p_scope"], "exact_setup")
        self.assertAlmostEqual(info["p_win"], 4 / 6, places=6)
        lo, hi = info["p_ci95"]
        self.assertLess(lo, 4 / 6)
        self.assertGreater(hi, 4 / 6)

    def test_wilson_interval_sane(self):
        lo, hi = wilson_ci95(41, 104)
        self.assertGreater(lo, 0.30)
        self.assertLess(hi, 0.50)

    def test_positive_ev_no_override_needed(self):
        """正 EV：无 override 要求，ev_check 正常产出。"""
        card = {
            "risk_reward": {"entry": 100.0, "stop": 98.0, "target": 106.0,
                            "rr": 3.0},
            "historical_experience": {
                "evidence_contract": _contract(same_n=10, same_wins=6)},
        }
        block, errors = build_ev_check(card, "long")
        self.assertEqual(errors, [], errors)
        self.assertGreater(block["ev_r"], 0)
        self.assertFalse(block["override_present"])


if __name__ == "__main__":
    unittest.main()
