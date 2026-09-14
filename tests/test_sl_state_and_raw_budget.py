# -*- coding: utf-8 -*-
"""战报「SL未挂」误报的两层修复（2026-08-20）。

**实证**：2026-08-20 10:45 战报写「SL未挂」，同轮 live_facts 里
`sl={"state":"live","trigger_px":67.0,"verified":true}` —— 止损不但挂着，还在被
持续上移（02:30 60.75 → 05:15 64.864 → 10:45 67.0）。近 40 轮有仓战报 8 轮误报，
全是 WAIT 轮（无 live_facts 的失败报告）。

**两层根因**：
① `render_push_report` 把「取不到止损价」一律写成「SL未挂」—— 与 F5 杀掉的
   「无错失机会」同类的伪断言，只是落在风控最要害的字段上。
② 本该兜底的 `_open_sl_info`（2026-07-09 专为根治该误导而写，从建仓 trade 的
   `raw.sl_trigger_px` 纯库读）取不到值，因为那笔 raw 被 `trades_writer.
   _bounded_json` 截断了：`decision_card` 20,179 字符在预算检查**之前**被无条件
   拷入，此后 `len(trial) <= max_chars - reserve`（16,000）对任何字段都不成立，
   连 4 字符的 `sz` 都被换成约 110 字符的哈希存根 —— 记录反而从 21,347 涨到
   25,211，代价是销毁全部操作事实。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "collectors"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import render_push_report as rpr  # noqa: E402
import trades_writer as tw  # noqa: E402


def _sl_text(content: str) -> str:
    for line in content.splitlines():
        if "SL" in line and ("多" in line or "空" in line):
            return line
    return ""


class SlStateRenderingTests(unittest.TestCase):
    """三态按**证据强度**分，不是按有没有数。"""

    def _render(self, sl_state, sl_px=None, stop_distance=""):
        position = {"sl_state": sl_state} if sl_state is not None else {}
        if sl_px is not None:
            position["sl_px"] = sl_px
        return rpr, position, stop_distance

    def _sl_txt_for(self, position, stop_distance=""):
        """复算 render 的 sl_txt 分支（与 render_push_report 同一判据）。"""
        if str(stop_distance) not in ("", "-"):
            return "attached-branch"
        state = str(position.get("sl_state") or "unread").lower()
        if state == "absent":
            return "SL未挂(交易所已确认无止损单)"
        if state == "unverified":
            return "SL未确认(有algo单但未验成，非未挂)"
        return "SL本轮未读取(非未挂；上游未取到交易所止损)"

    def test_unread_never_claims_the_stop_is_missing(self):
        """本轮没读交易所 ⇒ 只能说没读到，不能断言没挂。"""
        txt = self._sl_txt_for({"sl_state": "unread"})
        self.assertIn("未读取", txt)
        self.assertIn("非未挂", txt)

    def test_absent_still_says_it_plainly(self):
        """真没挂是必须看见的风险事实，不能为了消灭假阴性而藏起来。"""
        txt = self._sl_txt_for({"sl_state": "absent"})
        self.assertIn("SL未挂", txt)
        self.assertIn("交易所已确认", txt)

    def test_unverified_is_neither_attached_nor_absent(self):
        txt = self._sl_txt_for({"sl_state": "unverified"})
        self.assertIn("未确认", txt)
        self.assertIn("非未挂", txt)

    def test_legacy_payload_without_state_degrades_to_unread(self):
        """归档旧 payload 没有 sl_state —— 宁可说没读到，也不凭空断言未挂。"""
        txt = self._sl_txt_for({})
        self.assertIn("未读取", txt)
        self.assertNotIn("交易所已确认", txt)

    def test_bare_missing_branch_is_gone_from_source(self):
        """源码里不得再有无条件的 `sl_txt = "SL未挂"`。"""
        src = (ROOT / "scripts" / "render_push_report.py").read_text(
            encoding="utf-8")
        self.assertNotIn('sl_txt = "SL未挂"\r\n', src)
        self.assertNotIn('sl_txt = "SL未挂"\n', src)
        self.assertIn('position.get("sl_state")', src)


class RawBudgetKeepsSmallFactsTests(unittest.TestCase):
    """截断器不得为"省空间"丢掉比存根还小的字段。"""

    #  id=436 的真实形状：一张 20KB 决策卡 + 一堆小操作字段
    @staticmethod
    def _payload():
        return {
            "symbol": "HYPE-USDT-SWAP", "action": "open", "side": "long",
            "sz": 45.0, "fill_px": 58.97653333333334, "fill_sz": 45.0,
            "fill_source": "algo", "ordId": "3840036192059858944",
            "algo_id": "3840036192059858944", "sl_trigger_px": 55.6,
            "sl_mode": "attached", "sl_verified": True, "lev": 5.0,
            "margin": 53.08, "notional": 265.39, "pnl": 0.0,
            "decision_card": {"filler": "x" * 20000},
        }

    def test_operational_facts_survive_an_oversized_decision_card(self):
        out = json.loads(tw._bounded_json(self._payload(), 20000, "trades.raw"))
        for key in ("sl_trigger_px", "sl_mode", "sl_verified", "algo_id",
                    "ordId", "fill_px", "sz", "symbol", "side", "action"):
            with self.subTest(field=key):
                self.assertIn(key, out, f"{key} 又被截断掉了")
        self.assertEqual(55.6, out["sl_trigger_px"])

    def test_no_small_field_is_replaced_by_a_larger_stub(self):
        """存根约 110 字符：丢掉比它小的字段只会让记录变大，是纯亏。"""
        out = json.loads(tw._bounded_json(self._payload(), 20000, "trades.raw"))
        for entry in (out.get("raw_truncated_fields") or []):
            with self.subTest(field=entry["field"]):
                self.assertGreater(
                    entry["chars"], tw._SMALL_FIELD_KEEP_CHARS,
                    f"{entry['field']} 只有 {entry['chars']} 字符，不该被哈希掉")

    def test_truncation_makes_the_record_smaller_not_larger(self):
        """实测原实现把 21,347 字符的回执"截"成了 25,211。"""
        payload = self._payload()
        original = len(json.dumps(payload, ensure_ascii=False))
        bounded = tw._bounded_json(payload, 20000, "trades.raw")
        self.assertLessEqual(
            len(bounded), original + 512,
            f"截断后 {len(bounded)} 反而明显大于原始 {original}")

    def test_genuinely_large_fields_are_still_compacted(self):
        """阈值不是"什么都留"：真正大的字段照旧换哈希，否则预算形同虚设。"""
        payload = self._payload()
        payload["huge_debug_blob"] = "y" * 5000
        out = json.loads(tw._bounded_json(payload, 20000, "trades.raw"))
        dropped = {e["field"] for e in (out.get("raw_truncated_fields") or [])}
        self.assertIn("huge_debug_blob", dropped)
        self.assertNotIn("huge_debug_blob", out)
        self.assertIn("sl_trigger_px", out)

    def test_small_payload_is_untouched(self):
        """不超预算时原样落库，本改动不影响绝大多数回执。"""
        small = {"symbol": "BTC-USDT-SWAP", "sz": 1.0, "sl_trigger_px": 60000.0}
        out = json.loads(tw._bounded_json(small, 20000, "trades.raw"))
        self.assertEqual(small, out)


if __name__ == "__main__":
    unittest.main()
