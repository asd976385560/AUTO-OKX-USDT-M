# -*- coding: utf-8 -*-
"""持仓行「到 SL 锁盈」三态契约回归（2026-08-20）。

背景：旧实现只在 `secured_profit_at_stop_usdt > 0` 时输出锁盈段，`else` 一律空
串。而上游 `live_decision_facts` 的 `secured = max(0.0, pnl_at_stop)` 会把「止损
仍在成本之下」压成 `0.0`，于是三种完全不同的状态渲染成同一个「什么都没有」：

  ① 未保本（止损在开仓价下方）—— 确定事实，且是风控要害；
  ② 上游没算出到 SL 盈亏（缺 markPx/base_qty/触发价）—— 未知；
  ③ 恰好保本。

实测 2026-08-20T16:15 战报：XRP 浮盈 +$5.11，但到 SL 需较现价再亏 $13.62（=当前
浮盈的 266.7%，吐光还倒亏），该行却与同屏已锁盈 $40.61 的 HYPE 一样看不出差别。
缺值必须说缺值，未保本必须说未保本——本测试锁住这个区分。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import render_push_report as rpr  # noqa: E402


def _position(**overrides):
    base = {
        "profile": "live", "symbol": "X-USDT-SWAP", "side": "多",
        "size": "1.0", "avg_price": "1.0", "leverage": "5",
        "sl_pct": "3.4", "sl_buffer_pct": 5.3,
        "margin_usd": 51.34, "margin_pct": 5.6,
        "upl": "1.0", "upl_pct_initial_margin": 10.0,
    }
    base.update(overrides)
    return base


class StopLockStateRenderingTests(unittest.TestCase):

    def test_secured_profit_keeps_existing_wording(self):
        """已锁盈分支必须逐字保持原样——本次只补未锁盈/未知，不改已有口径。"""
        line = rpr.format_position(_position(
            symbol="HYPE-USDT-SWAP", size="45.0", avg_price="58.9765",
            upl="57.29", sl_pct="15.3", sl_buffer_pct=5.2,
            secured_profit_at_stop_usdt=40.6056,
            pnl_at_stop_from_entry_usdt=40.6056,
            additional_loss_to_stop_from_mark_usdt=16.686,
            giveback_to_stop_pct_of_current_upl=29.1247))
        self.assertIn("SL锁盈≈$40.61（到SL将回吐当前浮盈29.1%）", line)
        self.assertNotIn("SL未锁盈", line)

    def test_stop_below_entry_is_stated_not_omitted(self):
        """未保本必须显式写出，且带上「较现价再亏多少 / 占当前浮盈几成」。"""
        line = rpr.format_position(_position(
            symbol="XRP-USDT-SWAP", size="2.27", avg_price="1.1084",
            upl="5.11",
            secured_profit_at_stop_usdt=0.0,
            pnl_at_stop_from_entry_usdt=-8.5125,
            additional_loss_to_stop_from_mark_usdt=13.62,
            giveback_to_stop_pct_of_current_upl=266.6667))
        self.assertIn("SL未锁盈", line)
        self.assertIn("到SL从开仓价亏≈$8.51", line)
        self.assertIn("较现价再亏≈$13.62", line)
        # >100% 表示吐光当前浮盈后还要倒亏，数字必须如实给出、不得截断到 100。
        self.assertIn("为当前浮盈的266.7%", line)
        self.assertNotIn("SL锁盈≈", line)

    def test_exact_breakeven_does_not_say_lose_zero(self):
        """恰好保本单独措辞——「亏≈$0.00」自相矛盾。"""
        line = rpr.format_position(_position(
            secured_profit_at_stop_usdt=0.0,
            pnl_at_stop_from_entry_usdt=0.0,
            additional_loss_to_stop_from_mark_usdt=2.0,
            giveback_to_stop_pct_of_current_upl=40.0))
        self.assertIn("SL恰好保本(到SL不赚不亏", line)
        self.assertNotIn("亏≈$0.00", line)

    def test_missing_upstream_values_report_unknown_not_empty(self):
        """有止损但上游没给到 SL 盈亏：报未知，不得冒充「无锁盈」，更不得渲染成空。"""
        line = rpr.format_position(_position(
            secured_profit_at_stop_usdt=None,
            pnl_at_stop_from_entry_usdt=None))
        self.assertIn("SL锁盈未知(上游未提供到SL盈亏)", line)

    def test_absent_stop_does_not_add_redundant_unknown(self):
        """无止损时 sl_txt 已说明状态，锁盈段保持静默，避免重复噪音。"""
        line = rpr.format_position(_position(
            sl_pct="", sl_buffer_pct=None, sl_state="absent",
            secured_profit_at_stop_usdt=None,
            pnl_at_stop_from_entry_usdt=None))
        self.assertIn("SL未挂(交易所已确认无止损单)", line)
        self.assertNotIn("SL锁盈未知", line)
        self.assertNotIn("SL未锁盈", line)

    def test_bool_is_not_accepted_as_number(self):
        """payload 若把布尔塞进数值字段，不得被当成 0/1 参与判断。"""
        line = rpr.format_position(_position(
            secured_profit_at_stop_usdt=True,
            pnl_at_stop_from_entry_usdt=False))
        self.assertIn("SL锁盈未知(上游未提供到SL盈亏)", line)


if __name__ == "__main__":
    unittest.main()
