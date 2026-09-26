# -*- coding: utf-8 -*-
"""2026-09-26 对照 V3 risk::protection：止损距离 ≤ 0.8×(1/杠杆 − 维持保证金率)。

更远的止损在价格触及前就会被强平，止损单形同虚设；executor 必须拒单而不是放行。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import risk_validator as rv  # noqa: E402


def _validate(**kw):
    base = dict(
        symbol="X-USDT-SWAP", side="long", intended_sz=1.0, lev=10.0,
        mark_px=100.0, ct_val=1.0, lot_sz=0.001, equity=1000.0,
        open_positions=[], sl_trigger_px=97.0,
        available_margin=900.0, account_imr=10.0,
    )
    base.update(kw)
    return rv.validate(**base)


class MaxStopDistanceTests(unittest.TestCase):
    def test_formula_matches_v3(self):
        # lev 10, mmr 1% → 0.8×(0.1−0.01)=7.2%；lev 5 → 15.2%；lev 3 → 25.87%
        self.assertAlmostEqual(0.072, rv.max_sl_distance_pct(10, 0.01))
        self.assertAlmostEqual(0.152, rv.max_sl_distance_pct(5, 0.01))
        self.assertAlmostEqual(0.8 * (1 / 3 - 0.01), rv.max_sl_distance_pct(3))
        self.assertIsNone(rv.max_sl_distance_pct(0, 0.01))
        self.assertIsNone(rv.max_sl_distance_pct(10, 0.1))
        self.assertIsNone(rv.max_sl_distance_pct("x", 0.01))

    def test_stop_beyond_margin_distance_is_rejected(self):
        # 10x：8% 止损 > 7.2% → 拒；7% → 放行
        rejected = _validate(sl_trigger_px=92.0)
        self.assertFalse(rejected["approved"], rejected)
        self.assertEqual("sl_beyond_margin_distance", rejected["reject_reason"])
        self.assertAlmostEqual(0.072, rejected["math"]["max_sl_distance_pct"])
        self.assertEqual(rv.DEFAULT_MMR, rejected["math"]["mmr"])
        approved = _validate(sl_trigger_px=93.0)
        self.assertTrue(approved["approved"], approved)
        self.assertAlmostEqual(0.072, approved["math"]["max_sl_distance_pct"])

    def test_short_side_uses_the_same_distance(self):
        rejected = _validate(side="short", sl_trigger_px=108.0)
        self.assertEqual("sl_beyond_margin_distance", rejected["reject_reason"])
        self.assertTrue(_validate(side="short", sl_trigger_px=107.0)["approved"])

    def test_add_uses_existing_position_leverage(self):
        # 请求 3x（可扛 25.9%）但现仓 10x（可扛 7.2%）：按现仓杠杆核
        result = _validate(
            lev=3.0, sl_trigger_px=90.0,
            open_positions=[{"symbol": "X-USDT-SWAP", "side": "long",
                             "sz": 1.0, "lev": 10.0}],
        )
        self.assertEqual("sl_beyond_margin_distance", result["reject_reason"])
        self.assertEqual(10.0, result["math"]["effective_lev"])

    def test_exchange_tier_mmr_tightens_the_limit(self):
        # 10x、mmr 2% → 0.8×(0.1−0.02)=6.4%：7% 止损此时被拒
        result = _validate(sl_trigger_px=93.0, mmr=0.02)
        self.assertEqual("sl_beyond_margin_distance", result["reject_reason"])
        self.assertAlmostEqual(0.064, result["math"]["max_sl_distance_pct"])

    def test_invalid_mmr_fails_closed(self):
        for bad in ("nan", -0.01, 0.2, "x"):
            with self.subTest(mmr=bad):
                result = _validate(mmr=bad)
                self.assertFalse(result["approved"])
                self.assertEqual("bad_mmr", result["reject_reason"])

    def test_deviation_cap_still_wins_for_absurd_stops(self):
        # 40% 偏离先撞 30% 上限（疑填错标的），理由码保持 sl_deviation_exceeds
        result = _validate(lev=1.0, sl_trigger_px=60.0)
        self.assertEqual("sl_deviation_exceeds", result["reject_reason"])

    def test_no_stop_supplied_skips_the_rule(self):
        result = _validate(sl_trigger_px=None)
        self.assertTrue(result["approved"], result)
        self.assertIn("max_sl_distance_pct", result["math"])


if __name__ == "__main__":
    unittest.main()
