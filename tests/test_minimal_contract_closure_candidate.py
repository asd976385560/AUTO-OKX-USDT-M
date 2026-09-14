# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for value in (ROOT, ROOT / "scripts", ROOT / "core"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from scripts import _acceptance_thresholds as thresholds
from scripts import decision_briefing
from scripts import multitimeframe_decision_evidence as evidence
from core import candidate_quality_contract as quality
from collectors import analyst_writer


CYCLE = "2026-09-02T19:00"
ACTIVATION = "2026-09-02T19:00:00+08:00"


def candidate(index: int, *, qv: float, oi: float, chg: float,
              funding: float) -> dict:
    symbol = f"C{index:03d}-USDT-SWAP"
    return {
        "row": {
            "symbol": symbol,
            "last": 10.0 + index,
            "chg24h": chg,
            "funding_rate": funding,
            "candidate_oi_usd": oi,
            "candidate_oi_source": "derivatives_current",
        },
        "bias": "方向待定",
        "opportunity_side": None,
        "eligible_sides": ["long", "short"],
        "opportunity_state": "SIDE_NEUTRAL",
        "state_version": "side_neutral_review_v1",
        "rank_version": "side_neutral_opportunity_rotation_v2",
        "quote_vol": qv,
        "dig_history": {},
    }


class MinimalClosureCandidateTests(unittest.TestCase):
    def policy(self):
        return (
            mock.patch.object(
                thresholds, "MINIMAL_DECISION_CONTRACT_ACTIVATION_CST",
                ACTIVATION),
            mock.patch.object(
                thresholds, "MINIMAL_CONTRACT_CLOSURE_ACTIVATION_CST",
                ACTIVATION),
        )

    def artifacts(self, root: Path, count: int = 20):
        rows = [
            candidate(i, qv=float(i), oi=float(i), chg=float(i % 7),
                      funding=float(i) / 100_000)
            for i in range(1, count + 1)
        ]
        ordered, review = decision_briefing.rank_side_neutral_opportunities(
            rows, cycle_id=CYCLE, micro_map={}, review_limit=8)
        safe_cycle = CYCLE.replace(":", "-")
        ready_path = root / f"briefing-ready-pool-{safe_cycle}.json"
        manifest_path = root / f"briefing-candidates-{safe_cycle}.json"
        ready = decision_briefing.write_ready_pool_artifact(
            cycle_id=CYCLE, tick_ts="2026-09-02T11:00:00Z",
            ranked=ordered, picked=review, early=[],
            manifest_ordered=ordered, review_slice=review,
            out_file=ready_path, previous_ready={})
        manifest = decision_briefing.append_candidate_snapshot(
            root, CYCLE, review, [], "2026-09-02T11:00:00Z",
            candidate_out_file=manifest_path,
            ready_pool_reference=ready,
            manifest_ordered=ordered, review_slice=review)
        bundle = evidence.build_candidate_evidence_bundle(
            root, manifest_path, CYCLE)
        (root / f"candidate-bundle-{CYCLE.replace(':', '-')}.json").write_text(
            json.dumps(bundle), encoding="utf-8")
        return ordered, review, manifest, bundle

    def test_opportunity_rank_keeps_full_pool_and_hybrid_review(self):
        rows = [
            candidate(i, qv=float(i * 1_000_000),
                      oi=float(i * 500_000), chg=float(i),
                      funding=float(i) / 10_000)
            for i in range(1, 13)
        ]
        ordered, review = decision_briefing.rank_side_neutral_opportunities(
            rows, cycle_id=CYCLE, micro_map={}, review_limit=8,
            rotation_slots=2)
        self.assertEqual(12, len(ordered))
        self.assertEqual(8, len(review))
        self.assertEqual(6, sum(
            item["selection_reason"] == "opportunity_priority"
            for item in review))
        self.assertEqual(2, sum(
            item["selection_reason"] == "rotation_coverage"
            for item in review))
        self.assertEqual(
            "side_neutral_opportunity_rotation_v2",
            ordered[0]["rank_version"])
        self.assertTrue(all(
            item["opportunity_score_components"]
            ["hard_liquidity_or_oi_gate_used"] is False
            for item in ordered))

    def test_held_symbol_is_observation_in_closure_but_legacy_still_excludes(self):
        symbol = "C001-USDT-SWAP"
        self.assertEqual(
            (True, False),
            decision_briefing.held_candidate_disposition(
                symbol, {symbol}, closure_policy=True),
        )
        self.assertEqual(
            (True, True),
            decision_briefing.held_candidate_disposition(
                symbol, {symbol}, closure_policy=False),
        )
        row = candidate(
            1, qv=1_000_000.0, oi=500_000.0, chg=1.0,
            funding=0.0001)
        row["currently_held_observation"] = True
        row["row"]["currently_held_observation"] = True
        with tempfile.TemporaryDirectory() as tmp:
            minimal_patch, closure_patch = self.policy()
            with minimal_patch, closure_patch:
                manifest = decision_briefing.append_candidate_snapshot(
                    Path(tmp), CYCLE, [row], [], "2026-09-02T11:00:00Z",
                    candidate_out_file=(Path(tmp) / "manifest.json"),
                    manifest_ordered=[row], review_slice=[row])
        self.assertEqual(1, manifest["candidate_count"])
        self.assertIs(
            manifest["candidates"][0]["currently_held_observation"], True)
        self.assertEqual([symbol], manifest["review_symbols"])

    def test_full_manifest_open_outside_slice_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            minimal_patch, closure_patch = self.policy()
            with minimal_patch, closure_patch:
                root = Path(tmp)
                _ordered, review, manifest, _bundle = self.artifacts(root)
                outside = next(
                    row for row in manifest["candidates"]
                    if row["symbol"] not in {
                        item["row"]["symbol"] for item in review})
                raw = {
                    "candidates_deep_dived_v2": [{
                        "symbol": outside["symbol"],
                        "decision": "provisional_open",
                        "reason": "full-manifest opportunity selected",
                    }],
                    "candidate_coverage": {"dynamic_limit": 1},
                }
                signal = {
                    "symbol": outside["symbol"], "action": "open_long",
                    "side": "long",
                }
                canonical, safe, result = quality.normalize_candidate_quality(
                    cycle_id=CYCLE, raw=raw, signals=[signal],
                    phase="consume", evidence_root=root)
        self.assertEqual([signal], safe)
        self.assertEqual([], result["policy_blocking_errors"])
        row = canonical["candidates_deep_dived_v2"][0]
        self.assertTrue(row["quality_valid"])
        self.assertIsNone(row["candidate_id"])
        self.assertIsNone(row["review_hash"])

    def test_open_reasons_reject_retired_authority_but_allow_soft_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            minimal_patch, closure_patch = self.policy()
            with minimal_patch, closure_patch:
                root = Path(tmp)
                _ordered, review, _manifest, _bundle = self.artifacts(root)
                symbol = review[0]["row"]["symbol"]

                def evaluate(entry_reason, signal_reasoning):
                    return quality.normalize_candidate_quality(
                        cycle_id=CYCLE,
                        raw={
                            "candidates_deep_dived_v2": [{
                                "symbol": symbol,
                                "decision": "provisional_open",
                                "reason": entry_reason,
                            }],
                            "candidate_coverage": {"dynamic_limit": 1},
                        },
                        signals=[{
                            "symbol": symbol,
                            "action": "open_long",
                            "side": "long",
                            "reasoning": signal_reasoning,
                        }],
                        phase="consume",
                        evidence_root=root,
                    )[2]

                entry_block = evaluate(
                    "ENTRY_READY授权开仓", "点差与价格失效条件支持")
                signal_block = evaluate(
                    "点差与价格失效条件支持", "4H方向确认后开仓")
                soft_open = evaluate(
                    "成交额与OI仅作排序观察", "OI和成交额支持执行观察")
        self.assertTrue(any(
            "retired_opportunity_authority_forbidden" in error
            for error in entry_block["policy_blocking_errors"]))
        self.assertTrue(any(
            "retired_timeframe_reason_forbidden" in error
            for error in signal_block["policy_blocking_errors"]))
        self.assertEqual([], soft_open["policy_blocking_errors"])

    def test_analyst_writer_blocks_retired_open_signal_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            minimal_patch, closure_patch = self.policy()
            with minimal_patch, closure_patch:
                root = Path(tmp)
                _ordered, review, _manifest, _bundle = self.artifacts(root)
                symbol = review[0]["row"]["symbol"]
                original_paths = quality.candidate_evidence_paths

                def temp_paths(cycle_id, *, root=None):
                    return evidence.candidate_evidence_paths(
                        cycle_id, root=Path(tmp))

                receipt = {
                    "cycle_id": CYCLE,
                    "ts": "2026-09-02 19:01:00",
                    "mode": "full",
                    "status": "ok",
                    "decision_protocol": "minimal_decision_v2",
                    "regime": "range",
                    "regime_stale": 0,
                    "market_summary": {
                        name: {} for name in (
                            "macro", "news", "tech", "sentiment", "quant")
                    },
                    "missing_sources": [],
                    "signals": [{
                        "symbol": symbol,
                        "action": "open_long",
                        "side": "long",
                        "reasoning": "MTF未确认但仍按4H方向开仓",
                        "entry_hint": 10.0,
                        "stop_hint": 9.0,
                        "tp_hint": 12.0,
                        "exit_mode": "fixed_tp",
                    }],
                    "raw": {
                        "candidates_deep_dived_v2": [{
                            "symbol": symbol,
                            "decision": "provisional_open",
                            "reason": "点差与价格失效条件支持",
                        }],
                        "candidate_coverage": {"dynamic_limit": 1},
                    },
                }
                with mock.patch.object(
                    quality, "candidate_evidence_paths",
                    side_effect=temp_paths,
                ):
                    errors = analyst_writer.validate_receipt(receipt)
                    missing_ts_receipt = json.loads(json.dumps(receipt))
                    missing_ts_receipt.pop("ts")
                    missing_ts_errors = analyst_writer.validate_receipt(
                        missing_ts_receipt)
                    onds_soft_receipt = json.loads(json.dumps(receipt))
                    onds_soft_receipt["signals"] = []
                    onds_soft_receipt["raw"]["candidates_deep_dived_v2"] = [{
                        "symbol": symbol,
                        "decision": "reject",
                        "reason": (
                            "ONDS成交额/OI偏低、微观N/A且无催化，暂不开仓"),
                    }]
                    onds_soft_errors = analyst_writer.validate_receipt(
                        onds_soft_receipt)
                quality.candidate_evidence_paths = original_paths
        self.assertTrue(any(
            "candidate_policy: signal[0]:"
            "retired_timeframe_reason_forbidden" in error
            for error in errors), errors)
        self.assertIn("缺少必填字段: ts", missing_ts_errors)
        self.assertTrue(any(
            "soft_observation_cannot_be_sole_reject_reason" in error
            for error in onds_soft_errors), onds_soft_errors)

    def test_retired_and_soft_only_reject_reasons_fail(self):
        self.assertIn(
            "retired_timeframe_reason_forbidden",
            quality.closure_reject_reason_errors("无MTF确认，缺乏三周期授权"))
        self.assertIn(
            "soft_observation_cannot_be_sole_reject_reason",
            quality.closure_reject_reason_errors("成交额低且OI低，无催化"))
        self.assertEqual(
            [], quality.closure_reject_reason_errors(
                "成交额偏低且订单簿点差12bp、滑点10bp，执行成本不可接受"))

    def test_retired_timeframe_variants_cannot_bypass_unicode_boundaries(self):
        retired_reasons = (
            "MTF未确认",
            "MTF_not_ready",
            "4H方向未确认",
            "1H structure conflict",
            "15m_signal_not_aligned",
            "15m/1H/4H方向不一致",
            "缺1H确认",
            "4H_not_ready",
            "1H RSI与价格结构冲突",
            "高低周期尚未共振",
            "多周期确认不足",
            "4小时级别结构冲突",
            "multi-timeframe confirmation missing",
            "higher timeframe conflict",
            "timeframe unavailable",
            "HTF未确认",
        )
        for reason in retired_reasons:
            with self.subTest(reason=reason):
                self.assertIn(
                    "retired_timeframe_reason_forbidden",
                    quality.closure_reject_reason_errors(reason),
                )
        for observation in (
            "24h动量偏弱",
            "ATR1H=0.12",
            "近15分钟成交增加",
            "新开不足1h，upl=+0.73",
            "持仓1h后仍未触及止损",
            "仓龄4h，保护单仍有效",
        ):
            with self.subTest(observation=observation):
                self.assertNotIn(
                    "retired_timeframe_reason_forbidden",
                    quality.closure_reject_reason_errors(observation),
                )

    def test_retired_state_authority_focuses_on_candidate_context(self):
        retired_reasons = (
            "ENTRY_READY授权开仓",
            "EXTENDED候选暂不进入",
            "TRIGGERING state permits entry",
            "EARLY_WATCH作为否决依据",
            "成熟候选自动授权",
            "早期结构不允许开仓",
            "mature candidate authorization",
            "early-stage setup is not eligible",
            "candidate is mature",
        )
        for reason in retired_reasons:
            with self.subTest(reason=reason):
                self.assertIn(
                    "retired_opportunity_authority_forbidden",
                    quality.closure_retired_authority_reason_errors(reason),
                )
        for ordinary in (
            "entered early in the session after price invalidation",
            "early today the spread widened to 30bp",
            "mature market infrastructure reduced slippage",
        ):
            with self.subTest(ordinary=ordinary):
                self.assertNotIn(
                    "retired_opportunity_authority_forbidden",
                    quality.closure_retired_authority_reason_errors(ordinary),
                )

    def test_soft_only_synonyms_fail_but_execution_and_hard_gates_pass(self):
        soft_only_reasons = (
            "成交额偏低且量能不足",
            "OI不足，未平仓量太少",
            "流动性较差且没有催化",
            "无事件驱动，暂不开仓",
            "已有17仓，当前仓位较多",
            "已有十七仓，风险预算紧张",
            "IMR 56%，保证金空间有限但未触硬闸",
            "IMR 56%，未触发硬闸",
            "IMR 60%，尚未达到66.6%上限",
            "IMR低于66.6%硬上限",
            "low volume and low open interest",
            "liquidity low and no catalyst",
            "too many existing positions",
            "margin budget tight and risk headroom limited",
            "portfolio crowded but below the hard cap",
            "IMR does not exceed the hard cap",
            "margin usage remains under the 66.6% limit",
        )
        for reason in soft_only_reasons:
            with self.subTest(reason=reason):
                self.assertIn(
                    "soft_observation_cannot_be_sole_reject_reason",
                    quality.closure_reject_reason_errors(reason),
                )

        independently_reproducible = (
            "成交额偏低，但实时点差35bp、预计滑点28bp，执行成本不可接受",
            "流动性偏弱且订单簿深度仅200USDT",
            "low liquidity; order book depth is only 200 USDT",
            "low volume but spread=40bp and slippage=31bp",
            "价格已跌破失效位，price invalidation confirmed",
            "projected portfolio IMR=68% > 66.6% hard cap",
            "预计组合IMR 68% 超过66.6%",
            "single-order IMR=16% > 15% hard limit",
            "单笔IMR=16% > 15%",
            "止损风险=5.4% > 5%硬上限",
            "止损风险5.2%>5%",
            "facts.status=blocking，禁止新增风险",
            "账户不可验证，fail closed",
            "账仓不一致，禁止新增风险",
            "SL缺失，硬闸拒绝",
        )
        for reason in independently_reproducible:
            with self.subTest(reason=reason):
                self.assertNotIn(
                    "soft_observation_cannot_be_sole_reject_reason",
                    quality.closure_reject_reason_errors(reason),
                )


if __name__ == "__main__":
    unittest.main()
