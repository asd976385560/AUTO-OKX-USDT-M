# -*- coding: utf-8 -*-
"""D3｜gold_d1 自身序列 24h 口径（2026-08-19）。

旧口径 ``_fetch_gold_etf_d1`` 查妙想「518880 黄金 ETF」——人民币计价、A 股
时段、且是**盘中至今涨幅**，与 ``gold`` 列的 CoinGecko tether-gold(XAUT/USD,
7×24) 根本不是同一个标的（2026-08-13 当日同一根 gold 序列上 d1 从 +0.0104
摆到 -0.0075，A 股收盘后又冻结 17 小时）。新口径改为纯函数
``gold_d1_from_history``：网络无关、可单测。

本用例钉死三条语义（方案 D3「连锁」节要求）：正常 24h 锚点、锚点缺失→None、
锚点是 carry-forward→None（绝不伪造 0，否则"假平静"会被读成真实低波动）。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "collectors"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collect_slow  # noqa: E402

# 被测函数按位置读 (ts, gold, carried_forward)，建表列序必须一致。
_DDL = """
CREATE TABLE cross_market (
    ts TEXT PRIMARY KEY,
    gold REAL,
    carried_forward TEXT
)
"""


class GoldD1SelfSeriesTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(_DDL)

    def tearDown(self):
        self.con.close()

    def _insert(self, offsets, gold, carried="[]"):
        """按相对当前时刻的 SQLite 时间修饰符插一行锚点。

        ``offsets`` 是修饰符序列（或单个字符串）。SQLite 的 strftime 要求每个
        修饰符各占一个参数——把 '-24 hours +30 minutes' 拼成一个参数会整体
        解析失败返回 NULL，锚点行静默消失。
        """
        if isinstance(offsets, str):
            offsets = (offsets,)
        holes = ",".join("?" for _ in offsets)
        self.con.execute(
            "INSERT INTO cross_market(ts, gold, carried_forward) VALUES ("
            f"strftime('%Y-%m-%dT%H:%M:%SZ','now',{holes}), ?, ?)",
            (*offsets, gold, carried),
        )
        self.con.commit()

    def test_normal_24h_anchor_returns_rolling_return(self):
        """窗内有真实观测锚点 → d1=(now-prev)/prev。"""
        self._insert("-24 hours", 4000.0)
        got = collect_slow.gold_d1_from_history(self.con, 4040.0)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, 0.01, places=9)

    def test_negative_return_is_signed_not_absolute(self):
        self._insert("-24 hours", 4000.0)
        got = collect_slow.gold_d1_from_history(self.con, 3960.0)
        self.assertAlmostEqual(got, -0.01, places=9)

    def test_missing_anchor_returns_none_not_zero(self):
        """整表为空 → None。0 会被读成「24h 无变化」，是伪造事实。"""
        self.assertIsNone(collect_slow.gold_d1_from_history(self.con, 4040.0))

    def test_anchor_outside_tolerance_returns_none(self):
        """锚点落在 ±90min 容差外（这里 -20h）→ 不将就，返回 None。"""
        self._insert("-20 hours", 4000.0)
        self.assertIsNone(collect_slow.gold_d1_from_history(self.con, 4040.0))

    def test_carried_forward_anchor_is_skipped(self):
        """锚点是 carry-forward 值 → 跳过；无其它候选则 None。

        沿用值与当前值往往逐字相同，不跳过会让 d1 恒 0（假平静）。
        """
        self._insert("-24 hours", 4040.0, carried='["gold", "vix"]')
        self.assertIsNone(collect_slow.gold_d1_from_history(self.con, 4040.0))

    def test_carried_forward_anchor_yields_to_real_observation(self):
        """窗内同时有 carry-forward 行与真实观测行 → 取真实观测那条。"""
        self._insert("-24 hours", 4040.0, carried='["gold"]')
        self._insert(("-24 hours", "+30 minutes"), 4000.0)
        got = collect_slow.gold_d1_from_history(self.con, 4040.0)
        self.assertAlmostEqual(got, 0.01, places=9)

    def test_carried_forward_of_other_metric_does_not_block(self):
        """carry-forward 里没有 gold（只有 vix）→ 该锚点的 gold 仍是真实观测。"""
        self._insert("-24 hours", 4000.0, carried='["vix", "spx"]')
        got = collect_slow.gold_d1_from_history(self.con, 4040.0)
        self.assertAlmostEqual(got, 0.01, places=9)

    def test_none_or_nonpositive_current_price_returns_none(self):
        self._insert("-24 hours", 4000.0)
        self.assertIsNone(collect_slow.gold_d1_from_history(self.con, None))
        self.assertIsNone(collect_slow.gold_d1_from_history(self.con, 0.0))
        self.assertIsNone(collect_slow.gold_d1_from_history(self.con, -1.0))

    def test_zero_anchor_price_does_not_divide_by_zero(self):
        self._insert("-24 hours", 0.0)
        self.assertIsNone(collect_slow.gold_d1_from_history(self.con, 4040.0))

    def test_missing_table_returns_none_instead_of_raising(self):
        """sqlite3.Error 一律吞成 None —— 慢采不得因 d1 计算炸掉整轮。"""
        bare = sqlite3.connect(":memory:")
        try:
            self.assertIsNone(collect_slow.gold_d1_from_history(bare, 4040.0))
        finally:
            bare.close()

    def test_legacy_mx_data_fetcher_is_gone(self):
        """旧 518880 取数函数必须已删除（方案 D3 明确要求整函数删）。"""
        self.assertFalse(hasattr(collect_slow, "_fetch_gold_etf_d1"))


if __name__ == "__main__":
    unittest.main()
