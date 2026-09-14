# -*- coding: utf-8 -*-
"""carry-forward 时观测日必须一起沿用；BTC 市值振幅告警按占比判定（2026-08-20）。

**as_of 沿用**：D4 原意「沿用值不冒充新观测」是对的，实现却写了 None。写**今天**
的日期才叫冒充；写**原观测日**是实话 —— 那个值就是那次观测被再次端出来，观测日
不因重发而变未知。写 None 把已知陈旧度丢掉，而且恰好丢在源头失效那一轮
（carry-forward 只在拉取失败时发生），最该看见陈旧度时反而看不见。
实证：2026-08-19T14:02:13Z 行 `carried_forward=["dxy"]`、`dxy=118.9028`（08-14
那次观测），`dxy_as_of` 与 `source_meta.dxy.source_as_of` 双双为 null。

**市值告警**：同日 query_state 报「>5% BTC 市值，疑数据质量」。独立核对 BTC 1H
K 线 64,330→69,736 = **24h 真涨 8.40%**，折算 ~+105.9B 与库内 +108.9B 吻合 ——
数据是对的，阈值误报。绝对美元阈值会随市值漂（6e10 在 1.2T 是 5%、1.4T 只剩
4.3%），2026-06-11 已因同样原因抬过一次；改用占比才不随行情漂。
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class AsOfCarryForwardTests(unittest.TestCase):
    SRC = (SCRIPTS / "collect_slow.py").read_text(encoding="utf-8")

    def test_previous_observation_dates_are_selected(self):
        """沿用观测日的前提：上一行取值时把 *_as_of 一并取出（同源）。"""
        self.assertIn("dxy_as_of, vix_as_of, spx_as_of, gold_as_of", self.SRC)
        self.assertIn("prev_as_of = tuple(prev_vals[6:10])", self.SRC)
        self.assertIn(").fetchone() or (None,) * 10", self.SRC)

    def test_carry_forward_no_longer_writes_none(self):
        """旧实现是 `None if "dxy" in carried_forward else ...`，必须已退役。"""
        for metric in ("dxy", "vix", "spx", "gold"):
            with self.subTest(metric=metric):
                self.assertNotIn(
                    f'(None if "{metric}" in carried_forward else', self.SRC,
                    f"{metric} 的 as_of 仍在 carry-forward 时被写成 None")

    def test_carry_forward_reuses_previous_as_of(self):
        for idx, metric in enumerate(("dxy", "vix", "spx", "gold")):
            with self.subTest(metric=metric):
                self.assertIn(
                    f'(prev_as_of[{idx}] if "{metric}" in carried_forward',
                    self.SRC)

    def test_source_meta_as_of_matches_the_column(self):
        """source_meta 里的 source_as_of 与新列必须同口径，不能一个有一个 null。"""
        self.assertIn(
            'prev_as_of[0] if "dxy" in carried_forward', self.SRC)
        self.assertNotIn('"source_as_of": dxy_obs_date,', self.SRC)


class BtcMcapAmplitudeCheckTests(unittest.TestCase):
    SRC = (SCRIPTS / "query_state.py").read_text(encoding="utf-8")

    def test_check_uses_share_of_btc_mcap_not_absolute_usd(self):
        self.assertIn("btc_mcap_usd", self.SRC)
        self.assertIn("total_mcap_usd", self.SRC)
        self.assertIn("btc_dominance", self.SRC)
        self.assertIn("pct > 0.25", self.SRC)

    def test_dominance_percent_and_fraction_are_both_handled(self):
        """库内 dominance 存的是百分数（56.67）；按小数用会差 100 倍。"""
        self.assertIn("if share > 1:", self.SRC)
        self.assertIn("share /= 100.0", self.SRC)

    def test_absolute_threshold_survives_only_as_fallback(self):
        """拿不到 dominance/total 时仍要检，不能因缺字段静默放行。"""
        tree = ast.parse(self.SRC)
        consts = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, float)
        }
        self.assertIn(6e10, consts, "回退用的绝对阈值不应被删掉")
        self.assertIn("BTC 市值不可得", self.SRC)

    def test_eight_percent_move_no_longer_alerts(self):
        """2026-08-20 的真实数字：+108.9B / BTC 市值 ~1.40T = 7.8%，不得告警。"""
        total_mcap, dominance, chg = 2467643468062.433, 56.6661001245372, 1.088e11
        share = dominance / 100.0 if dominance > 1 else dominance
        btc_mcap = total_mcap * share
        pct = abs(chg) / btc_mcap
        self.assertLess(pct, 0.25, f"实测占比 {pct:.1%} 不应触发告警")
        self.assertGreater(chg, 6e10, "该值确实超过旧的绝对阈值（正是误报来源）")


if __name__ == "__main__":
    unittest.main()
