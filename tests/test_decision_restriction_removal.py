

def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

# -*- coding: utf-8 -*-
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for value in (ROOT, ROOT / "scripts", ROOT / "collectors", ROOT / "core"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from scripts import _acceptance_thresholds as thresholds
from scripts import decision_briefing
from scripts import multitimeframe_decision_evidence as evidence
from core import candidate_quality_contract as quality
from core import decision_card
from core import multitimeframe_gate as mtf_gate
from core import order_executor
from scripts import render_push_report
from scripts import validate_push_format
from scripts import zero_open_watchdog
import analyst_writer
import trigger_agent
import trades_writer


ACTIVATION = "2026-09-01T22:45:00+08:00"
CYCLE = "2026-09-01T22:45"


def candidate(index: int, state: str = "ENTRY_READY") -> dict:
    symbol = f"X{index:03d}-USDT-SWAP"
    side = "long" if index % 2 else "short"
    return {
        "row": {"symbol": symbol, "last": 10.0 + index, "chg24h": 0.0},
        "bias": "偏多" if side == "long" else "偏空",
        "opportunity_side": side,
        "opportunity_state": state,
        "state_version": "opportunity_state_v3_all_timeframes",
        "opportunity_id": f"opp_{index:020x}"[-24:],
        "first_seen_cycle": CYCLE,
        "rank_version": "all_market_state_v3_no_liq_oi_gate",
        "rank_key": (4, 1, 0, 1.0, 1.0, symbol),
        "trend_strength": {"score": 1.0},
        "entry_timing": {"timing_score": 1.0},
        "dig_history": {},
    }


class DecisionRestrictionRemovalTests(unittest.TestCase):
    def policy(self):
        return mock.patch.object(
            thresholds, "DECISION_RESTRICTION_REMOVAL_ACTIVATION_CST", ACTIVATION)

    def make_manifest(
        self, root: Path, count: int = 40, state: str = "ENTRY_READY",
    ) -> Path:
        rows = [candidate(index, state=state) for index in range(1, count + 1)]
        path = root / f"briefing-candidates-{CYCLE.replace(':', '-')}.json"
        decision_briefing.append_candidate_snapshot(
            root,
            CYCLE,
            [],
            [],
            "2026-09-01T14:45:00Z",
            candidate_out_file=path,
            manifest_ordered=rows,
            review_slice=rows[:8],
        )
        return path

    def test_forward_boundary_changes_only_new_cycles(self):
        with self.policy():
            self.assertFalse(thresholds.decision_restriction_removal_active(
                "2026-09-01T22:30"))
            self.assertTrue(thresholds.candidate_exact_identity_enforced(
                "2026-09-01T22:30"))
            self.assertEqual("shadow", thresholds.candidate_bundle_phase(
                "2026-09-01T22:30"))
            self.assertTrue(thresholds.decision_restriction_removal_active(CYCLE))
            self.assertFalse(thresholds.candidate_exact_identity_enforced(CYCLE))
            self.assertEqual("consume", thresholds.candidate_bundle_phase(CYCLE))
            self.assertFalse(thresholds.open_multitimeframe_contract_required(CYCLE))
            self.assertEqual(2048, thresholds.candidate_manifest_maximum(CYCLE))
            self.assertEqual(CYCLE, thresholds.zero_open_watchdog_activation_cycle(CYCLE))
            self.assertEqual(3, thresholds.RELAXED_BASIC_CONFIRMATION_SLOTS)
            self.assertEqual(12, thresholds.zero_open_watchdog_threshold_slots(CYCLE))

    def test_four_hour_is_not_required_for_direction(self):
        votes = {"15m": 2, "1H": 2, "4H": 0}
        old = decision_briefing.classify_opportunity_state(
            votes, {}, require_four_hour_direction=True)
        new = decision_briefing.classify_opportunity_state(
            votes, {}, require_four_hour_direction=False)
        self.assertEqual(("NON_DIRECTIONAL", None), old[:2])
        self.assertEqual(("TRIGGERING", "long"), new[:2])

    def test_v2_manifest_and_bundle_support_more_than_sixteen(self):
        with tempfile.TemporaryDirectory() as tmp, self.policy():
            root = Path(tmp)
            manifest_path = self.make_manifest(root, 40)
            manifest, _ = evidence.load_candidate_manifest(manifest_path, CYCLE)
            self.assertEqual(40, manifest["candidate_count"])
            self.assertEqual(8, manifest["review_slice_count"])

            def result(symbol):
                contract = mtf_gate.seal_evidence_contract({
                    "protocol": "multitimeframe_market_evidence_v1",
                    "cycle_id": CYCLE,
                    "symbol": symbol,
                })
                return {"ready": True, "status": "PASSED",
                        "evidence_contract": contract}

            symbols = [row["symbol"] for row in manifest["candidates"]]
            with mock.patch.object(
                    evidence, "check_multitimeframe_readiness_batch",
                    return_value=[result(symbol) for symbol in symbols]):
                bundle = evidence.build_candidate_evidence_bundle(
                    ROOT / "db", manifest_path, CYCLE)
            self.assertEqual(40, bundle["manifest_count"])
            self.assertEqual(40, bundle["screened_count"])
            self.assertEqual(8, bundle["decision_slice_count"])
            self.assertEqual(8, len(bundle["items"]))
            self.assertEqual([], evidence.validate_candidate_evidence_bundle(
                bundle, expected_cycle=CYCLE,
                expected_manifest_path=manifest_path))

    def test_lightweight_open_card_has_no_history_identity_or_mtf(self):
        payload = {
            "cycle_id": CYCLE,
            "ts": "2026-09-01 22:46:00",
            "mode": "full",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "regime": "range",
            "regime_stale": False,
            "market_summary": {
                key: {} for key in ("macro", "news", "tech", "sentiment", "quant")},
            "missing_sources": [],
            "signals": [{
                "symbol": "SOL-USDT-SWAP",
                "action": "open_long",
                "side": "long",
                "entry_hint": 100.0,
                "stop_hint": 97.0,
                "tp_hint": 106.0,
                "exit_mode": "fixed_tp",
                "reasoning": "provisional open after all-market review",
            }],
            "raw": {
                "candidates_deep_dived_v2": [],
                "candidate_coverage": {
                    "dynamic_limit": 0, "dynamic_target": 0,
                    "stop_reason": "target_reached"},
            },
        }
        with self.policy():
            normalized = analyst_writer.normalize_receipt(payload)
            card = normalized["signals"][0]["decision_card"]
            self.assertEqual(
                decision_card.LIGHTWEIGHT_OPEN_CONTRACT, card["contract"])
            self.assertNotIn("historical_experience", card)
            self.assertNotIn("multitimeframe_analysis", card)
            self.assertEqual([], decision_card.validate_card(card))
            self.assertEqual([], analyst_writer.validate_receipt(payload))

    def test_more_than_three_open_signals_are_allowed(self):
        signals = []
        for index in range(4):
            signals.append({
                "symbol": f"S{index}-USDT-SWAP",
                "action": "open_long", "side": "long",
                "entry_hint": 100.0 + index, "stop_hint": 97.0 + index,
                "tp_hint": 106.0 + index, "exit_mode": "no_fixed_tp",
                "reasoning": f"qualified provisional open {index}",
            })
        payload = {
            "cycle_id": CYCLE, "ts": "2026-09-01 22:46:00",
            "mode": "full", "status": "ok",
            "decision_protocol": "decision_card_v1",
            "regime": "range", "regime_stale": False,
            "market_summary": {
                key: {} for key in ("macro", "news", "tech", "sentiment", "quant")},
            "missing_sources": [], "signals": signals,
            "raw": {"candidates_deep_dived_v2": [],
                    "candidate_coverage": {"dynamic_limit": 0,
                                           "dynamic_target": 0,
                                           "stop_reason": "target_reached"}},
        }
        with self.policy():
            normalized = analyst_writer.normalize_receipt(payload)
            self.assertEqual(4, len(normalized["signals"]))
            self.assertEqual([], analyst_writer.validate_receipt(payload))

    def test_relaxed_quality_never_filters_open(self):
        signal = {"symbol": "SOL-USDT-SWAP", "action": "open_long",
                  "side": "long"}
        with tempfile.TemporaryDirectory() as tmp, self.policy():
            raw, signals, result = quality.normalize_candidate_quality(
                cycle_id=CYCLE,
                raw={"candidates_deep_dived_v2": [],
                     "candidate_coverage": {
                         "dynamic_limit": 0, "dynamic_target": 0,
                         "stop_reason": "target_reached"}},
                signals=[signal], phase="consume", evidence_root=Path(tmp))
        self.assertEqual([signal], signals)
        self.assertFalse(raw["candidate_identity"]["active"])
        self.assertFalse(raw["candidate_quality"]["open_filter_active"])
        self.assertEqual([], result["rejected_open_signals"])

    def test_executor_context_accepts_light_card_without_mtf_or_history(self):
        card = {
            "contract": decision_card.LIGHTWEIGHT_OPEN_CONTRACT,
            "side": "long",
            "reasoning": "all-market provisional open",
            "risk_reward": {
                "entry": 100.0, "stop": 97.0, "target": 106.0,
                "exit_mode": "fixed_tp",
            },
        }
        context = {
            "cycle_id": CYCLE,
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "regime": "range",
            "decision_card": card,
        }
        with self.policy():
            self.assertEqual([], order_executor.validate_receipt_context(
                context, cycle_id=CYCLE, required=True,
                expected_symbol="SOL-USDT-SWAP", expected_side="long",
                expected_regime="range", require_experience=True))

    def test_open_executor_does_not_call_mtf_gate_after_boundary(self):
        card = {
            "contract": decision_card.LIGHTWEIGHT_OPEN_CONTRACT,
            "side": "long", "reasoning": "exercise post-removal executor",
            "risk_reward": {"entry": 100.0, "stop": 97.0,
                            "target": 106.0, "exit_mode": "no_fixed_tp"},
        }
        context = {"cycle_id": CYCLE, "status": "ok",
                   "decision_protocol": "decision_card_v1",
                   "regime": "range", "decision_card": card}
        with self.policy(), \
                mock.patch.object(order_executor.ox, "is_dryrun", return_value=True), \
                mock.patch.object(
                    order_executor, "check_multitimeframe_readiness",
                    side_effect=AssertionError("MTF gate must not run")) as readiness, \
                mock.patch.object(
                    order_executor, "resolve_execution_evidence_anchor",
                    side_effect=AssertionError("MTF anchor must not run")) as anchor, \
                mock.patch.object(
                    order_executor, "fetch_instrument_specs", return_value={}):
            result = order_executor.open_position(
                "SOL-USDT-SWAP", "long", 1.0, 5.0, 97.0, "live",
                mark_px=100.0, equity=1000.0, open_positions=[],
                available_margin=900.0, account_imr=0.0,
                cycle_id=CYCLE, receipt_context=context)
        self.assertFalse(result["ok"])
        self.assertEqual("instrument_unknown", result["reject_reason"])
        readiness.assert_not_called()
        anchor.assert_not_called()

    def test_trades_writer_accepts_lightweight_open_receipt(self):
        card = {
            "contract": decision_card.LIGHTWEIGHT_OPEN_CONTRACT,
            "side": "long", "reasoning": "confirmed lightweight open",
            "risk_reward": {"entry": 100.0, "stop": 97.0,
                            "target": 106.0, "exit_mode": "fixed_tp"},
        }
        payload = {
            "cycle_id": CYCLE, "ts": "2026-09-01 22:46:30",
            "mode": "live", "status": "ok", "decision": "traded",
            "action_taken": "OPEN_LONG", "n_orders": 1, "equity": 1000.0,
            "regime": "range", "decision_protocol": "decision_card_v1",
            "decision_card": card, "errors": [], "ok": True,
            "trades": [{
                "symbol": "SOL-USDT-SWAP", "action": "open",
                "side": "long", "sz": 1.0, "fill_sz": 1.0,
                "fill_px": 100.0, "approved_sz": 1.0, "lev": 5.0,
                "margin": 20.0, "notional": 100.0,
                "reasoning": "confirmed lightweight open",
                "fill_source": "fills", "fill_ts": "2026-09-01 22:46:29",
                "ts_source": "fills.fillTime", "decision_card": card,
                "raw": {"ok": True, "sl_verified": True},
            }],
        }
        with self.policy():
            self.assertEqual([], trades_writer.validate(payload))

    def test_push_no_longer_requires_mtf_section_after_boundary(self):
        with self.policy():
            section = render_push_report.format_multitimeframe_analysis(
                "OPEN_LONG", {}, cycle_id=CYCLE,
                fallback_symbol="SOL-USDT-SWAP")
            self.assertIn("三周期合同=已按主人批准", section)
            result = validate_push_format.validate(
                "第1轮 OPEN_LONG SOL\n" + section, cycle_id=CYCLE)
            self.assertFalse(result["multitimeframe_contract_required"])
            self.assertFalse(any(
                "三周期" in error for error in result["errors"]))

    def test_new_prompt_removes_old_open_restrictions(self):
        bundle = {
            "phase": "consume", "status": "PASSED",
            "manifest_valid": True,
            "manifest_count": 438, "candidate_count": 8,
            "decision_slice_count": 8, "screened_count": 438,
            "ready_count": 8, "full_ready_count": 438,
            "bundle_path": _public_project_path('tmp', 'full.json'),
            "decision_slice_path": _public_project_path('tmp', 'slice.json'),
            "manifest_path": _public_project_path('tmp', 'manifest.json'),
            "ready_pool_path": _public_project_path('tmp', 'ready.json'),
            "ready_pool_status": "PASSED",
        }
        with self.policy():
            message = trigger_agent._unified_live_message(
                CYCLE, "briefing marker", candidate_bundle=bundle,
                now=datetime(2026, 9, 1, 22, 46, tzinfo=trigger_agent.CST))
        self.assertIn("全市场轻量OPEN合同", message)
        self.assertIn('decision slice=<PROJECT_ROOT>/tmp/slice.json'.replace('<PROJECT_ROOT>', _public_project_path()).replace('\\', '/'), message.replace('\\', '/'))
        self.assertIn("禁止读取MEMORY.md", message)
        self.assertNotIn("允许读取 MEMORY.md", message)
        self.assertNotIn("只允许 0..3", message)
        self.assertNotIn("confidence_claim_allowed=false", message)
        self.assertNotIn("--candidate-id <cand_...>", message)
        self.assertIn("receipt_context.decision_card", message)
        self.assertIn("decision_protocol准确字段名", message)
        self.assertIn("--decision-view-file", message)
        self.assertIn("只读取精简decision-view", message)
        self.assertIn("禁止把完整大文件加载进模型上下文", message)
        self.assertIn("source_status必须为PASSED", message)
        self.assertIn("position_count与现仓一致", message)

    def test_watchdog_restarts_at_new_policy_epoch(self):
        with tempfile.TemporaryDirectory() as tmp, self.policy():
            db = Path(tmp) / "analysis.db"
            connection = sqlite3.connect(db)
            connection.executescript(
                "CREATE TABLE analysis_runs (cycle_id TEXT PRIMARY KEY,"
                "status TEXT,mode TEXT,raw TEXT);"
                "CREATE TABLE analysis_signals (cycle_id TEXT,symbol TEXT,action TEXT);"
            )
            for cycle in ("2026-09-01T22:30", CYCLE, "2026-09-01T23:00"):
                raw = {"signals": [], "raw": {"candidates_deep_dived_v2": [{
                    "reason_code": "quantified_cost_veto",
                    "primary_disqualifier": {"kind": "cost_or_liquidity"},
                }]}}
                connection.execute(
                    "INSERT INTO analysis_runs VALUES (?,?,?,?)",
                    (cycle, "ok", "full", json.dumps(raw)))
            connection.commit()
            connection.close()
            result = zero_open_watchdog.evaluate_zero_open_watchdog(
                db, "2026-09-01T23:00")
        self.assertEqual("zero_open_watchdog_v2", result["schema"])
        self.assertEqual(CYCLE, result["activation_cycle"])
        self.assertEqual(2, result["successful_zero_open_slots"])
        self.assertEqual(12, result["threshold_successful_slots"])
        self.assertEqual(2, result["veto_code_counts"]["cost_or_liquidity"])

    def test_entry_ready_requires_open_or_quantified_veto(self):
        with tempfile.TemporaryDirectory() as tmp, self.policy():
            root = Path(tmp)
            self.make_manifest(root, 1)
            base_entry = {
                "symbol": "X001-USDT-SWAP", "side": "long", "layer": "mature",
                "decision": "reject", "supporting_evidence": ["aligned"],
                "opposing_evidence": ["weak catalyst"],
                "invalidation_condition": "stop", "reason_code": "weak_catalyst",
                "reason": "no fresh catalyst",
            }
            raw = {"candidates_deep_dived_v2": [base_entry],
                   "candidate_coverage": {
                       "dynamic_limit": 1, "dynamic_target": 1,
                       "stop_reason": "target_reached"}}
            _, _, failed = quality.normalize_candidate_quality(
                cycle_id=CYCLE, raw=raw, signals=[], phase="manifest_only",
                evidence_root=root)
            self.assertTrue(any(
                "entry_ready_primary_disqualifier" in item
                for item in failed["policy_blocking_errors"]))

            allowed = json.loads(json.dumps(base_entry))
            allowed["primary_disqualifier"] = {
                "kind": "cost_or_liquidity", "metric": "exit_slippage_bps",
                "observed_value": 18.0, "operator": ">",
                "boundary_value": 10.0, "source_path": "decision_slice.book",
            }
            allowed["reason_family"] = "FREE_FORM_OWNER_REASON"
            raw["candidates_deep_dived_v2"] = [allowed]
            _, _, passed = quality.normalize_candidate_quality(
                cycle_id=CYCLE, raw=raw, signals=[], phase="manifest_only",
                evidence_root=root)
            self.assertEqual([], passed["policy_blocking_errors"])
            canonical, _, _ = quality.normalize_candidate_quality(
                cycle_id=CYCLE, raw=raw, signals=[], phase="manifest_only",
                evidence_root=root)
            entry = canonical["candidates_deep_dived_v2"][0]
            self.assertEqual("FREE_FORM_OWNER_REASON", entry["reason_family"])
            self.assertFalse(entry["reason_family_normalized"])

    def test_all_four_states_require_open_or_quantified_veto(self):
        states = ("ENTRY_READY", "EXTENDED", "TRIGGERING", "EARLY_WATCH")
        for state in states:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp, \
                    self.policy():
                root = Path(tmp)
                self.make_manifest(root, 1, state=state)
                base = {
                    "symbol": "X001-USDT-SWAP", "side": "long",
                    "layer": (
                        "early" if state in {"TRIGGERING", "EARLY_WATCH"}
                        else "mature"),
                    "decision": "reject",
                    "supporting_evidence": ["directional"],
                    "opposing_evidence": ["execution cost"],
                    "invalidation_condition": "stop",
                    "reason_code": "execution_cost", "reason": "quantified",
                }
                raw = {
                    "candidates_deep_dived_v2": [base],
                    "candidate_coverage": {
                        "dynamic_limit": 1, "dynamic_target": 1,
                        "stop_reason": "target_reached"},
                }
                _, _, missing = quality.normalize_candidate_quality(
                    cycle_id=CYCLE, raw=raw, signals=[],
                    phase="manifest_only", evidence_root=root)
                self.assertTrue(missing["policy_blocking_errors"])

                vetoed = json.loads(json.dumps(base))
                vetoed["primary_disqualifier"] = {
                    "kind": "cost_or_liquidity", "metric": "spread_bps",
                    "observed_value": 18.0, "operator": ">",
                    "boundary_value": 10.0,
                    "source_path": "decision_slice.book.spread_bps",
                }
                raw["candidates_deep_dived_v2"] = [vetoed]
                _, _, allowed_veto = quality.normalize_candidate_quality(
                    cycle_id=CYCLE, raw=raw, signals=[],
                    phase="manifest_only", evidence_root=root)
                self.assertEqual([], allowed_veto["policy_blocking_errors"])

                opened = json.loads(json.dumps(base))
                opened["decision"] = "provisional_open"
                raw["candidates_deep_dived_v2"] = [opened]
                _, _, allowed_open = quality.normalize_candidate_quality(
                    cycle_id=CYCLE, raw=raw,
                    signals=[{"symbol": "X001-USDT-SWAP",
                              "action": "open_long", "side": "long"}],
                    phase="manifest_only", evidence_root=root)
                self.assertEqual([], allowed_open["policy_blocking_errors"])

    def test_removed_volume_and_oi_thresholds_cannot_return_as_veto(self):
        with tempfile.TemporaryDirectory() as tmp, self.policy():
            root = Path(tmp)
            self.make_manifest(root, 1)
            for metric in (
                    "quote_volume_usd", "turnover_usd", "vol24h",
                    "closed_15m_volume_contracts", "oi", "oi_usd",
                    "open interest"):
                with self.subTest(metric=metric):
                    entry = {
                        "symbol": "X001-USDT-SWAP", "side": "long",
                        "layer": "mature", "decision": "reject",
                        "supporting_evidence": ["directional"],
                        "opposing_evidence": ["old threshold"],
                        "invalidation_condition": "stop",
                        "reason_code": "old_threshold", "reason": "old gate",
                        "primary_disqualifier": {
                            "kind": "cost_or_liquidity", "metric": metric,
                            "observed_value": 1.0, "operator": "<",
                            "boundary_value": 5_000_000.0,
                            "source_path": f"decision_slice.{metric}",
                        },
                    }
                    raw = {
                        "candidates_deep_dived_v2": [entry],
                        "candidate_coverage": {
                            "dynamic_limit": 1, "dynamic_target": 1,
                            "stop_reason": "target_reached"},
                    }
                    _, _, result = quality.normalize_candidate_quality(
                        cycle_id=CYCLE, raw=raw, signals=[],
                        phase="manifest_only", evidence_root=root)
                    self.assertTrue(any(
                        "removed_market_threshold" in error
                        for error in result["policy_blocking_errors"]))

    def test_degraded_bundle_uses_preloaded_review_without_old_gates(self):
        bundle = {
            "phase": "consume", "status": "DEGRADED",
            "manifest_valid": True, "manifest_count": 435,
            "candidate_count": 0, "screened_count": 0,
            "ready_count": 0, "full_ready_count": 438,
            "decision_slice_path": _public_project_path('tmp', 'does-not-exist.json'),
            "ready_pool_path": _public_project_path('tmp', 'ready.json'),
            "error": "timeout",
        }
        with self.policy():
            message = trigger_agent._unified_live_message(
                CYCLE, "preloaded briefing review rows", candidate_bundle=bundle,
                now=datetime(2026, 9, 1, 22, 46,
                             tzinfo=trigger_agent.CST))
        self.assertIn("预读briefing", message)
        self.assertNotIn("--candidate-id <cand_...>", message)
        self.assertNotIn("多周期证据命令固定", message)
        self.assertNotIn("本轮全市场候选只读取有界决策slice", message)


if __name__ == "__main__":
    unittest.main()
