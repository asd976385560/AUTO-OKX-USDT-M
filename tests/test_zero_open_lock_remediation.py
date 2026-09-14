# -*- coding: utf-8 -*-
"""零开仓自锁整改（2026-08-20）——A/C/D/E 五项的可测核心。

背景事实（实测，非推断）：2026-08-17 整改部署后连续 207 个 `status='ok'` 轮
`raw.signals` 全为空数组，最后一笔 OPEN 停在 2026-08-17 13:26。把 Agent 当轮实际
否决的候选（XPL 2026-08-20T10:15）喂回 EV 计算，得 p_win=40.68% / net_rr=1.86 /
ev_r=+0.163——**EV 闸当时并不会拦它**。真正的两处结构缺陷是：

  ① 新鲜一级源催化被 briefing 的 `severity IN ('critical','high')` 过滤挡在视野外
     （近 3 天同口径 20 条里 17 条 severity='low'）；
  ② `ev_check` 只在 writer 落卡时才算，而写卡被拒会锁死本轮 → 「EV 符号不确定」
     变成纯下行风险，理性策略是不写。

本文件钉住修复后的纯函数语义，不重复拼装全库（渲染由生产简报烟测覆盖）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _p in (ROOT, SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import find_similar_experience as fse  # noqa: E402
from collectors import news_writer  # noqa: E402
from core import policy_epochs  # noqa: E402


def _contract(n, wins, scope="cross_symbol_similar"):
    return {"summaries": {scope: {"scope": scope, "n": n, "wins": wins}}}


class EvPreviewTests(unittest.TestCase):
    """A —— 写卡前就能看到 EV 符号。"""

    def test_matches_the_real_rejected_candidate(self):
        # XPL 2026-08-20T10:15：3×ATR 止损 4.079%、RR2 目标、cross scope 24/59。
        preview = fse.build_ev_preview(
            0.08179, 0.078454, 0.088462, "long", _contract(59, 24))
        self.assertEqual(preview["status"], "computed")
        self.assertAlmostEqual(preview["p_win"], 0.4068, places=4)
        self.assertAlmostEqual(preview["net_rr"], 1.8598, places=3)
        self.assertAlmostEqual(preview["breakeven_p"], 0.3497, places=3)
        self.assertGreater(preview["ev_r"], 0)
        self.assertFalse(preview["needs_override"])

    def test_negative_ev_is_flagged_before_the_card_is_written(self):
        preview = fse.build_ev_preview(
            0.08179, 0.078454, 0.088462, "long", _contract(59, 12))
        self.assertLess(preview["ev_r"], 0)
        self.assertTrue(preview["needs_override"])
        self.assertTrue(preview["would_block_write_without_override"])

    def test_insufficient_sample_is_indeterminate_not_negative(self):
        preview = fse.build_ev_preview(
            0.08179, 0.078454, 0.088462, "long", _contract(4, 1))
        self.assertEqual(preview["status"], "indeterminate")
        self.assertIsNone(preview["ev_r"])
        self.assertFalse(preview["needs_override"])

    def test_friction_share_exposes_the_narrow_stop_penalty(self):
        # 窄止损（约 0.77%）被 0.2% 摩擦吃掉的比例应显著高于宽止损。
        narrow = fse.build_ev_preview(
            100.0, 99.23, 101.7, "long", _contract(59, 24))
        wide = fse.build_ev_preview(
            100.0, 96.0, 108.0, "long", _contract(59, 24))
        self.assertGreater(narrow["friction_share_of_risk"],
                           wide["friction_share_of_risk"])
        self.assertGreater(narrow["breakeven_p"], wide["breakeven_p"])

    def test_preview_never_claims_to_be_the_canonical_block(self):
        preview = fse.build_ev_preview(
            0.08179, 0.078454, 0.088462, "long", _contract(59, 24))
        self.assertEqual(preview["version"], fse.EV_PREVIEW_VERSION)
        self.assertIn("禁止复制进决策卡", preview["note"])

    def test_missing_prices_degrade_without_raising(self):
        preview = fse.build_ev_preview(None, None, None, "long", _contract(59, 24))
        self.assertEqual(preview["status"], "unavailable")


class RelativeEventDateTests(unittest.TestCase):
    """C —— 相对日期层：币圈标题绝大多数不写显式日期。"""

    def test_english_and_chinese_day_anchors(self):
        ref = "2026-08-20 09:47:13"
        for text, expected in (
            ("Bitcoin ETFs had a net inflow yesterday", "2026-08-19"),
            ("Today, the US Bitcoin ETF had a net outflow", "2026-08-20"),
            ("Hyperliquid earned $4.4 million in the past 24 hours", "2026-08-20"),
            ("昨日美国比特币现货 ETF 净流入", "2026-08-19"),
            ("今日以太坊现货 ETF 净流出", "2026-08-20"),
            ("过去 24 小时全网爆仓 2.9 亿美元", "2026-08-20"),
        ):
            self.assertEqual(
                news_writer.extract_relative_event_date(text, ref), expected,
                msg=text)

    def test_vague_windows_are_refused(self):
        ref = "2026-08-20 09:47:13"
        for text in ("recently the market rallied", "近期比特币走强",
                     "this week the ETF flows turned", "本周资金面转向"):
            self.assertIsNone(
                news_writer.extract_relative_event_date(text, ref), msg=text)

    def test_title_relative_beats_body_explicit_date(self):
        # 正文里的日期常是预测目标日/解锁日；标题的 today 说的才是本条的事件日。
        occurred, source = news_writer.extract_event_date_with_source(
            "Bitcoin breaks above $69,000 today",
            {"summary": "Standard Chartered sees $100,000 by 2026-09-09"},
            "2026-08-20 05:46:59")
        self.assertEqual(occurred, "2026-08-20")
        self.assertEqual(source, "relative_title")

    def test_explicit_title_date_still_wins(self):
        occurred, source = news_writer.extract_event_date_with_source(
            "Upgrade shipped on 2026-08-18, rollout continues today", {},
            "2026-08-20 05:46:59")
        self.assertEqual(occurred, "2026-08-18")
        self.assertEqual(source, "extracted_title")

    def test_relative_layer_never_produces_a_future_date(self):
        ref = "2026-08-20 09:47:13"
        for text in ("today", "yesterday", "in the past 24 hours", "今日"):
            got = news_writer.extract_relative_event_date(text, ref)
            self.assertLessEqual(got, "2026-08-20", msg=text)

    def test_source_tag_keeps_relative_separable_for_audit(self):
        occurred, source = news_writer.extract_event_date_with_source(
            "Whale withdrew 10,300 ETH in the past 24 hours", {},
            "2026-08-18 12:00:00")
        self.assertEqual(occurred, "2026-08-18")
        self.assertTrue(source.startswith("relative_"))


class PolicyEpochTests(unittest.TestCase):
    """D —— 已撤回策略纪元只退出 EV 先验，不退出任何验收口径。"""

    def test_withdrawn_probe_window_boundaries(self):
        self.assertEqual(policy_epochs.policy_epoch("2026-08-13T23:45"),
                         policy_epochs.EPOCH_BASELINE)
        self.assertEqual(policy_epochs.policy_epoch("2026-08-14T00:00"),
                         policy_epochs.EPOCH_WITHDRAWN_PROBE)
        self.assertEqual(policy_epochs.policy_epoch("2026-08-17T16:29"),
                         policy_epochs.EPOCH_WITHDRAWN_PROBE)
        # 右开：撤回部署时刻起回到 baseline
        self.assertEqual(policy_epochs.policy_epoch("2026-08-17T16:30"),
                         policy_epochs.EPOCH_BASELINE)

    def test_unparsable_cycle_stays_in_the_prior(self):
        # 身份不明的样本宁可留在先验里，也不静默剔除。
        self.assertEqual(policy_epochs.policy_epoch("not-a-cycle"),
                         policy_epochs.EPOCH_UNKNOWN)
        self.assertTrue(policy_epochs.is_ev_prior_eligible("not-a-cycle"))
        self.assertTrue(policy_epochs.is_ev_prior_eligible(None))

    def test_eligibility_tracks_the_single_registry(self):
        self.assertFalse(policy_epochs.is_ev_prior_eligible("2026-08-15T10:15"))
        self.assertTrue(policy_epochs.is_ev_prior_eligible("2026-08-19T10:15"))
        self.assertIn(policy_epochs.EPOCH_WITHDRAWN_PROBE,
                      policy_epochs.EXCLUDED_FROM_EV_PRIOR)


class PolicyEpochExternalisationTests(unittest.TestCase):
    """D —— 排除必须可被独立复核，禁止静默剔除失败样本。"""

    class _Row(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

    def _row(self, rid, cycle_id, pnl):
        return self._Row(id=rid, cycle_id=cycle_id, pnl_pct=pnl)

    def test_excluded_samples_are_counted_and_identified(self):
        block = fse.build_policy_epoch_filter([
            self._row(1, "2026-08-15T10:15", -1.2),
            self._row(2, "2026-08-16T04:00", 0.8),
            self._row(3, "2026-08-14T00:45", -2.0),
        ], include_all_epochs=False)
        bucket = block["excluded_by_epoch"][policy_epochs.EPOCH_WITHDRAWN_PROBE]
        self.assertEqual(block["excluded_total"], 3)
        self.assertEqual((bucket["n"], bucket["wins"], bucket["losses"]),
                         (3, 1, 2))
        self.assertEqual(bucket["sample_ids"], [1, 2, 3])

    def test_scope_note_pins_it_to_ev_prior_only(self):
        block = fse.build_policy_epoch_filter([], include_all_epochs=False)
        self.assertEqual(block["excluded_total"], 0)
        self.assertIn("EV 先验", block["scope"])
        self.assertIn("全量", block["scope"])


class RoleContractClauseTests(unittest.TestCase):
    """E —— 角色契约（2026-08-28 判断门槛降级后）：参考基线成文且准入制不复活。

    历史：本类原钉 2026-08-20 路径二四条件与探针条款取代关系；2026-08-28 主人
    拍板把判断类开仓门槛全面降级为参考（硬限制只保留确定性风控闸与数据真实性
    契约），断言随契约同步改钉新形态。
    """

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "agents" / "live_trader.md").read_text(
            encoding="utf-8")

    def test_admission_paths_are_retired_not_just_softened(self):
        self.assertIn("all_market_lightweight_open_v1", self.text)
        self.assertIn("OPEN不再要求六/九项展示卡", self.text)
        self.assertNotIn("否则一律不开", self.text)
        self.assertNotIn("两条路径都不成立则不开", self.text)
        self.assertNotIn("rr>=2.5", self.text)

    def test_negative_ev_disclosure_survives_the_demotion(self):
        self.assertNotIn("risk_reward.ev_override", self.text)
        self.assertNotIn("accepts_negative_ev", self.text)
        self.assertIn("cost_adjusted_ev", self.text)

    def test_hard_limits_are_enumerated_as_the_only_gates(self):
        # 「硬限制只有那几条」必须落成文字：确定性风控闸逐项可见。
        self.assertIn("钱路硬闸保持不变", self.text)
        for token in ("66.6%", "MAX_SINGLE_ORDER_IMR_RATIO=0.15",
                      "MAX_SINGLE_ORDER_RISK_PCT_EQUITY=0.05", "杠杆≤10x"):
            self.assertIn(token, self.text, msg=token)

    def test_wide_atr_cap_keeps_the_floor_evidence_visible(self):
        self.assertNotIn("stop_below_3x_atr_1h", self.text)
        self.assertNotIn("stop_capped_wide_atr", self.text)
        self.assertNotIn("77% 打满止损", self.text)

    def test_zero_open_is_still_a_legal_terminal_state(self):
        self.assertIn("不设置最低开仓数", self.text)
        self.assertIn("每个已review side-neutral候选只需形成OPEN", self.text)
        self.assertIn("`reject` 并写reason", self.text)


if __name__ == "__main__":
    unittest.main()
