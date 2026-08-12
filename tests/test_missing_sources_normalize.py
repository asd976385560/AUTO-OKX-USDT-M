# -*- coding: utf-8 -*-
"""missing_sources 标签归一契约（2026-08-05）。

背景：该字段由 Agent 自由文本写入，实测同一含义出现两种拼写
（`dxy_zone_stale_carryforward` 9 轮 / `dxy_zone_stale_carry_forward` 3 轮），
按 key 聚合的统计会把同一件事算成两件。writer 侧只归一**已知别名**。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("collectors", "scripts"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import analyst_writer  # noqa: E402

CANON = "dxy_zone_stale_carry_forward"


class MissingSourcesNormalizeTests(unittest.TestCase):
    def test_known_aliases_map_to_canonical(self):
        for alias in ("dxy_zone_stale_carryforward",
                      "dxy_zone_stale_carry-forward",
                      "dxy_stale_carryforward"):
            self.assertEqual(
                analyst_writer.normalize_missing_sources([alias]), [CANON],
                f"{alias} 未归一")

    def test_canonical_form_is_unchanged(self):
        self.assertEqual(analyst_writer.normalize_missing_sources([CANON]), [CANON])

    def test_unknown_labels_are_left_alone(self):
        """只归一已登记别名——不得擅自改写 Agent 报的其它缺源标签。"""
        src = ["x_search", "defillama_tvl_total", "某个新源"]
        self.assertEqual(analyst_writer.normalize_missing_sources(src), src)

    def test_dedupe_after_normalization_preserves_order(self):
        """两种拼写同时出现时归一后去重，且保持首次出现顺序。"""
        src = ["defillama_tvl_total", "dxy_zone_stale_carryforward",
               "dxy_zone_stale_carry_forward"]
        self.assertEqual(
            analyst_writer.normalize_missing_sources(src),
            ["defillama_tvl_total", CANON])

    def test_none_and_empty_pass_through(self):
        """None / [] 语义等价（无缺源），不得被改写成对方。"""
        self.assertIsNone(analyst_writer.normalize_missing_sources(None))
        self.assertEqual(analyst_writer.normalize_missing_sources([]), [])

    def test_non_list_and_non_str_items_survive(self):
        self.assertEqual(analyst_writer.normalize_missing_sources("x_search"),
                         "x_search")
        src = [{"src": "x_search", "age": 900}]
        self.assertEqual(analyst_writer.normalize_missing_sources(src), src)

    def test_alias_table_targets_are_all_canonical_snake_case(self):
        """别名表的目标值本身必须是小写 snake_case，否则等于换个地方分叉。"""
        for target in analyst_writer.MISSING_SOURCE_ALIASES.values():
            self.assertEqual(target, target.lower())
            self.assertNotIn("-", target)
            self.assertNotIn(" ", target)
            self.assertNotIn(target, analyst_writer.MISSING_SOURCE_ALIASES,
                             f"{target} 既是别名又是目标，会造成映射歧义")


if __name__ == "__main__":
    unittest.main()
