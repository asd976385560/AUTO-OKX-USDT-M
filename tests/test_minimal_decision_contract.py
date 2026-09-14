# -*- coding: utf-8 -*-
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
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
from core import order_executor
from collectors import trades_writer
from collectors import trigger_agent
from scripts import render_push_report
from scripts import validate_push_format
from scripts import live_position_action_runner as runner
from scripts import build_push_payload
from scripts import reconcile_exchange_closes as reconcile


CYCLE = "2026-09-02T12:00"
ACTIVATION = "2026-09-02T12:00:00+08:00"


def neutral_candidate(index: int) -> dict:
    symbol = f"N{index:03d}-USDT-SWAP"
    return {
        "row": {
            "symbol": symbol, "last": 10.0 + index, "chg24h": 0.0,
            "candidate_oi_usd": 0.0,
            "candidate_oi_source": "unavailable_observation",
        },
        "bias": "方向待定",
        "opportunity_side": None,
        "eligible_sides": ["long", "short"],
        "opportunity_state": "SIDE_NEUTRAL",
        "state_version": "side_neutral_review_v1",
        "rank_version": "side_neutral_round_robin_v1",
        "rank_key": (100 - index, symbol),
        "quote_vol": 0.0,
        "dig_history": {},
    }


def lightweight_open_card() -> dict:
    return {
        "contract": decision_card.LIGHTWEIGHT_OPEN_CONTRACT,
        "side": "long",
        "reasoning": "exchange-confirmed OPEN execution package",
        "risk_reward": {
            "entry": 10.0,
            "stop": 9.0,
            "target": 12.0,
            "exit_mode": "fixed_tp",
        },
    }


def create_trade_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE trade_cycles(
                cycle_id TEXT PRIMARY KEY, ts TEXT, mode TEXT,
                decision TEXT, n_orders INTEGER, equity REAL,
                note TEXT, raw TEXT
            );
            CREATE TABLE trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT, ts TEXT, symbol TEXT, action TEXT,
                side TEXT, sz REAL, fill_px REAL, lev REAL,
                margin REAL, notional REAL, score_total REAL,
                reasoning TEXT, deviation TEXT, degradation TEXT,
                pnl REAL, raw TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


class MinimalDecisionContractTests(unittest.TestCase):
    def policy(self):
        return mock.patch.object(
            thresholds, "MINIMAL_DECISION_CONTRACT_ACTIVATION_CST",
            ACTIVATION)

    def artifacts(self, root: Path, count: int = 20):
        rows = [neutral_candidate(index) for index in range(1, count + 1)]
        review = rows[:8]
        ready_path = root / f"briefing-ready-pool-{CYCLE.replace(':', '-')}.json"
        manifest_path = root / f"briefing-candidates-{CYCLE.replace(':', '-')}.json"
        ready_ref = decision_briefing.write_ready_pool_artifact(
            cycle_id=CYCLE, tick_ts="2026-09-02T04:00:00Z",
            ranked=rows, picked=review, early=[], manifest_ordered=rows,
            review_slice=review, out_file=ready_path, previous_ready={})
        manifest = decision_briefing.append_candidate_snapshot(
            root, CYCLE, review, [], "2026-09-02T04:00:00Z",
            candidate_out_file=manifest_path, ready_pool_reference=ready_ref,
            manifest_ordered=rows, review_slice=review)
        return rows, manifest_path, manifest

    def test_forward_boundary(self):
        with self.policy():
            self.assertFalse(thresholds.minimal_decision_contract_active(
                "2026-09-02T11:45"))
            self.assertTrue(thresholds.minimal_decision_contract_active(CYCLE))
            self.assertFalse(thresholds.three_period_judgment_required(CYCLE))
            self.assertFalse(thresholds.six_field_decision_card_required(CYCLE))

    def test_candidate_registration_switches_off_legacy_quality_gates(self):
        with self.policy():
            facts = thresholds.candidate_bundle_registration_facts(CYCLE)
        self.assertEqual("minimal", facts["phase"])
        self.assertEqual(
            thresholds.MINIMAL_DECISION_CONTRACT_POLICY,
            facts["decision_policy"])
        self.assertFalse(facts["three_period_judgment_required"])
        self.assertFalse(facts["four_state_judgment_required"])
        self.assertFalse(facts["six_field_decision_card_required"])
        self.assertIsNone(facts["deep_dive_range"])
        self.assertIsNone(facts["shadow_gates"])
        self.assertIsNone(facts["consume_gates"])
        self.assertEqual(
            12,
            facts["minimal_policy_gates"]["full_confirmation_natural_slots"])
        self.assertFalse(facts["consume_requires_completed_shadow_gate"])
        self.assertEqual(
            "none",
            facts["legacy_registration"][
                "acceptance_effect_after_minimal_activation"])

    def test_side_neutral_manifest_and_bundle_do_not_call_mtf(self):
        with tempfile.TemporaryDirectory() as tmp, self.policy():
            root = Path(tmp)
            _, manifest_path, manifest = self.artifacts(root)
            self.assertEqual(
                "briefing_candidate_manifest_v3_side_neutral",
                manifest["schema"])
            self.assertEqual(20, manifest["candidate_count"])
            self.assertEqual(8, manifest["review_slice_count"])
            self.assertTrue(all(
                row["side"] is None
                and row["eligible_sides"] == ["long", "short"]
                and row["layer"] == "all_market"
                and row["trend_strength"] is None
                and row["entry_timing"] is None
                for row in manifest["candidates"]))
            with mock.patch.object(
                    evidence, "check_multitimeframe_readiness_batch",
                    side_effect=AssertionError("MTF must not run")):
                bundle = evidence.build_candidate_evidence_bundle(
                    Path(tmp), manifest_path, CYCLE)
            self.assertEqual(
                "candidate_review_bundle_v3_side_neutral",
                bundle["artifact_type"])
            self.assertEqual(20, bundle["screened_count"])
            self.assertEqual(8, bundle["decision_slice_count"])
            self.assertFalse(bundle["timeframe_judgment_used"])
            self.assertTrue(all(
                "evidence_contract" not in row
                and row["eligible_sides"] == ["long", "short"]
                for row in bundle["items"]))
            self.assertEqual([], evidence.validate_candidate_evidence_bundle(
                bundle, expected_cycle=CYCLE,
                expected_manifest_path=manifest_path))

    def test_side_neutral_review_uses_signal_to_select_side(self):
        with tempfile.TemporaryDirectory() as tmp, self.policy():
            root = Path(tmp)
            _, manifest_path, manifest = self.artifacts(root, 1)
            bundle = evidence.build_candidate_evidence_bundle(
                Path(tmp), manifest_path, CYCLE)
            (root / f"candidate-bundle-{CYCLE.replace(':', '-')}.json").write_text(
                json.dumps(bundle), encoding="utf-8")
            raw = {
                "candidates_deep_dived_v2": [{
                    "symbol": "N001-USDT-SWAP",
                    "decision": "provisional_open",
                    "reason": "Agent chose short without timeframe gate",
                }],
                "candidate_coverage": {
                    "dynamic_limit": 1, "stop_reason": "target_reached"},
            }
            signal = {
                "symbol": "N001-USDT-SWAP", "action": "open_short",
                "side": "short",
            }
            canonical, signals, result = quality.normalize_candidate_quality(
                cycle_id=CYCLE, raw=raw, signals=[signal], phase="consume",
                evidence_root=root)
            self.assertEqual([signal], signals)
            self.assertEqual([], result["policy_blocking_errors"])
            row = canonical["candidates_deep_dived_v2"][0]
            self.assertEqual("short", row["selected_side"])
            self.assertFalse(row["timeframe_judgment_used"])

    def test_side_neutral_non_open_is_not_quality_filtered(self):
        with tempfile.TemporaryDirectory() as tmp, self.policy():
            root = Path(tmp)
            _, manifest_path, _ = self.artifacts(root, 1)
            bundle = evidence.build_candidate_evidence_bundle(
                Path(tmp), manifest_path, CYCLE)
            (root / f"candidate-bundle-{CYCLE.replace(':', '-')}.json").write_text(
                json.dumps(bundle), encoding="utf-8")
            raw = {
                "candidates_deep_dived_v2": [{
                    "symbol": "N001-USDT-SWAP", "decision": "veto",
                    "reason": "Agent declined without a retired veto card",
                }],
                "candidate_coverage": {
                    "dynamic_limit": 426, "stop_reason": "target_reached"},
            }
            canonical, signals, result = quality.normalize_candidate_quality(
                cycle_id=CYCLE, raw=raw, signals=[], phase="consume",
                evidence_root=root)
            self.assertEqual([], signals)
            self.assertEqual([], result["policy_blocking_errors"])
            self.assertEqual(
                "reject", canonical["candidates_deep_dived_v2"][0]["decision"])
            self.assertEqual(1, canonical["candidate_coverage"]["dynamic_limit"])

    def test_position_exit_passes_without_market_timeframes(self):
        facts = {
            "cycle_id": CYCLE, "profile": "live", "status": "ok",
            "facts_hash": "f" * 64,
            "positions": [{
                "instId": "N001-USDT-SWAP", "posSide": "long",
                "contracts": 1.0, "avgPx": 10.0, "markPx": 10.1,
            }],
        }
        with self.policy(), \
                mock.patch.object(evidence, "validate_facts", return_value=[]), \
                mock.patch.object(evidence, "_open_plan_context", return_value={
                    "status": "unique_open_leg", "review_flags": [],
                    "protection_floor": {"breach": False},
                }), \
                mock.patch.object(
                    evidence, "check_multitimeframe_readiness",
                    side_effect=AssertionError("position MTF must not run")):
            payload = evidence.build_position_exit_batch(
                Path("Z:/definitely-missing"), facts, CYCLE)
        self.assertTrue(payload["ok"])
        self.assertEqual("PASSED", payload["status"])
        self.assertFalse(payload["timeframe_judgment_used"])
        self.assertNotIn("multitimeframe_ready_count", payload)
        self.assertNotIn("evidence_contract", payload["positions"][0])

    def test_minimal_hold_context_has_no_decision_card(self):
        context = {
            "cycle_id": CYCLE, "status": "ok", "mode": "live",
            "regime": "range",
            "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL,
            "reasoning": "portfolio IMR leaves no capacity",
            "position_reviews": [],
        }
        with self.policy():
            self.assertEqual([], order_executor.validate_receipt_context(
                context, cycle_id=CYCLE, required=True))
            payload = {
                **context, "profile": "live", "decision": "hold",
                "action_taken": "HOLD", "n_orders": 0, "trades": [],
                "errors": [], "ok": True,
            }
            self.assertEqual([], trades_writer.validate(payload))
            plan = {"cycle_id": CYCLE, "receipt_context": context,
                    "actions": []}
            facts = {
                "cycle_id": CYCLE, "profile": "live", "status": "ok",
                "facts_hash": "f" * 64, "positions": [],
                "balance": {"totalEq": 1000.0},
                "action_policy": {
                    "position_truth_verified": True,
                    "allowed_executor_actions": [
                        "open", "add", "close", "reduce",
                        "adjust_protection"],
                },
            }
            with mock.patch.object(runner, "validate_facts", return_value=[]):
                canonical_context, actions = runner.preflight_plan(
                    plan, facts, cycle_id=CYCLE, db_root=Path(_public_project_path('db')))
            self.assertEqual([], actions)
            self.assertNotIn("decision_card", canonical_context)
            self.assertEqual(
                decision_card.MINIMAL_DECISION_PROTOCOL,
                canonical_context["decision_protocol"])

    def test_actual_prompt_has_no_three_period_or_six_card_contract(self):
        bundle = {
            "phase": "consume", "status": "PASSED",
            "manifest_valid": True, "manifest_count": 420,
            "candidate_count": 8, "decision_slice_count": 8,
            "screened_count": 420, "ready_count": 8,
            "full_ready_count": 420,
            "bundle_path": _public_project_path('tmp', 'bundle.json'),
            "decision_slice_path": _public_project_path('tmp', 'slice.json'),
            "manifest_path": _public_project_path('tmp', 'manifest.json'),
            "ready_pool_path": _public_project_path('tmp', 'ready.json'),
            "ready_pool_status": "PASSED",
        }
        with self.policy():
            message = trigger_agent._unified_live_message(
                CYCLE, "side-neutral briefing", candidate_bundle=bundle,
                now=datetime(2026, 9, 2, 12, 1,
                             tzinfo=trigger_agent.CST))
        self.assertIn("policy=no_three_period_no_six_card_v1", message)
        self.assertIn("decision_protocol=minimal_decision_v2", message)
        self.assertIn("eligible_sides=[long,short]", message)
        self.assertNotIn("严格三周期同向", message)
        self.assertNotIn("HOLD必须把完整卡", message)
        self.assertNotIn("ENTRY_READY", message)

    def test_new_push_omits_three_period_and_six_card_sections(self):
        payload = {
            "cycle_id": CYCLE, "cycle_count": 1, "cycle_duration_s": 10,
            "hhmm": "12:00", "action_taken": "HOLD", "symbol": "NONE",
            "assets": {"live": {
                "equity": 1000, "availBal": 800, "pnl": 0, "positions": 0}},
            "positions": [],
            "risk": {
                "current_portfolio_imr_ratio": 0.1,
                "max_portfolio_imr_ratio": 0.666,
                "portfolio_imr_ratio_unit": "fraction",
                "lev": 5, "side_pct": 0, "position_count": 0,
                "status": "PASS"},
            "market": {
                "btc": 65000, "btc_chg24h": 0, "eth": 3500,
                "eth_chg24h": 0, "regime": "range", "dxy": 120},
            "decision": {
                "summary": "minimal HOLD", "reason": "no action",
                "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL},
            "execution": {"result": "HOLD", "db_rows_live": 0},
            "timeline": {"next_hh01_min": 60, "next_review_time": "08:05"},
            "exceptions": [],
        }
        patches = (
            mock.patch.object(render_push_report, "authoritative_cycle_count",
                              return_value=None),
            mock.patch.object(render_push_report, "authoritative_cycle_duration",
                              return_value=None),
            mock.patch.object(render_push_report, "authoritative_equity",
                              return_value=None),
            mock.patch.object(render_push_report, "authoritative_cum_pnl",
                              return_value=None),
            mock.patch.object(render_push_report, "authoritative_position_count",
                              return_value=None),
        )
        with self.policy(), patches[0], patches[1], patches[2], patches[3], patches[4]:
            rendered = render_push_report.render(payload)
            result = validate_push_format.validate(
                rendered["content"], cycle_id=CYCLE)
        self.assertNotIn("🧩 三周期判断", rendered["content"])
        self.assertNotIn("🧭 六项决策卡", rendered["content"])
        self.assertNotIn("multitimeframe_analysis", rendered["sections"])
        self.assertFalse(any(
            "三周期" in error or "六项" in error
            for error in result["errors"]), result)

    def test_maintenance_scrubs_old_cycle_card_from_minimal_persistence(self):
        old_card = {
            "direction_evidence": ["legacy"],
            "opposing_evidence": ["legacy"],
            "execution_conditions": {"legacy": True},
            "invalidation_point": {"legacy": True},
            "risk_reward": {"legacy": True},
            "portfolio_impact": {"legacy": True},
            "historical_experience": {
                "matched_wins": [], "matched_losses": [],
                "missed_opportunities": [], "usage": "none",
                "reason": "legacy",
            },
            "agent_judgement": "legacy six-field card",
            "reference_overrides": [],
        }
        payload = {
            "cycle_id": CYCLE,
            "ts": "2026-09-02 12:02:00",
            "decision": "traded",
            "_profile": "live",
            "raw": {
                "decision_protocol": "decision_card_v1",
                "decision_card": old_card,
                "live_facts": {"status": "ok"},
            },
            "trades": [{
                "symbol": "N001-USDT-SWAP", "action": "close",
                "side": "long", "sz": 1.0, "fill_px": 10.0,
                "lev": 5.0, "margin": 2.0, "notional": 10.0,
                "pnl": 0.1, "reasoning": "exchange-confirmed close",
                "raw": {
                    "ordId": "confirmed-close",
                    "decision_protocol": "decision_card_v1",
                    "decision_card": old_card,
                },
            }],
        }
        with tempfile.TemporaryDirectory() as tmp, self.policy(), \
                mock.patch.object(
                    trades_writer, "_analysis_context_for_cycle",
                    return_value={}):
            db = Path(tmp) / "live_trades.db"
            create_trade_db(db)
            result = trades_writer.maintenance_write_trades(
                payload, db, trusted_timestamp=payload["ts"],
                preserve_equity_none=True)
            self.assertTrue(result["ok"], result)
            con = sqlite3.connect(db)
            try:
                cycle_raw = json.loads(con.execute(
                    "SELECT raw FROM trade_cycles WHERE cycle_id=?",
                    (CYCLE,)).fetchone()[0])
                trade_raw = json.loads(con.execute(
                    "SELECT raw FROM trades WHERE cycle_id=?",
                    (CYCLE,)).fetchone()[0])
            finally:
                con.close()
        self.assertEqual(
            decision_card.MINIMAL_DECISION_PROTOCOL,
            cycle_raw["decision_protocol"])
        self.assertNotIn("decision_card", cycle_raw)
        self.assertEqual(
            decision_card.MINIMAL_DECISION_PROTOCOL,
            trade_raw["decision_protocol"])
        self.assertNotIn("decision_card", trade_raw)
        self.assertEqual("confirmed-close", trade_raw["ordId"])

    def test_minimal_unrecorded_repair_keeps_open_package_off_cycle_top(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY,ts TEXT,"
            "decision TEXT,n_orders INTEGER,equity REAL,note TEXT,raw TEXT)")
        con.execute(
            "CREATE TABLE trades(symbol TEXT,action TEXT,side TEXT,sz REAL,"
            "fill_px REAL,lev REAL,margin REAL,notional REAL,score_total REAL,"
            "reasoning TEXT,deviation TEXT,degradation TEXT,pnl REAL,"
            "raw TEXT,cycle_id TEXT,ts TEXT)")
        captured = {}

        def fake_write(data, *_args, **_kwargs):
            captured.update(data)
            return {"ok": True, "refused": None}

        matched = [{
            "ordId": "confirmed-open",
            "fills": [{
                "fillTime": "1788312120000", "fillPx": "10",
                "fillSz": "1", "ordId": "confirmed-open",
                "tradeId": "trade-1", "execType": "T",
            }],
        }]
        try:
            with self.policy(), \
                    mock.patch.object(
                        reconcile.trades_writer, "maintenance_write_trades",
                        side_effect=fake_write), \
                    mock.patch.object(
                        reconcile.trades_writer, "write_experiences",
                        return_value={"exp": 1}):
                result = reconcile.apply_unrecorded(
                    Path("unused.db"), "live", "N001-USDT-SWAP", "long",
                    1.0, matched, con, lev=5.0,
                    card=lightweight_open_card(),
                    intent={"cycle_id": CYCLE, "ord_id": "confirmed-open"},
                    sl_probe={"has_sl": True})
        finally:
            con.close()
        self.assertEqual(CYCLE, result["cycle_id"])
        self.assertEqual(
            decision_card.MINIMAL_DECISION_PROTOCOL,
            captured["decision_protocol"])
        self.assertNotIn("decision_card", captured)
        self.assertEqual(
            decision_card.LIGHTWEIGHT_OPEN_CONTRACT,
            captured["trades"][0]["decision_card"]["contract"])
        self.assertEqual("confirmed-open", captured["raw"]["ord_ids"][0])
        self.assertTrue(captured["raw"]["sl_probe"]["has_sl"])

    def test_reconcile_context_and_push_payload_do_not_revive_removed_contracts(self):
        raw = {
            "decision_protocol": "decision_card_v1",
            "decision_card": {"agent_judgement": "legacy six-field card"},
            "reasoning": "facts-only position review",
            "position_reviews": [{"symbol": "N001-USDT-SWAP"}],
            "live_facts": {
                "status": "ok", "cycle_id": CYCLE,
                "balance": {"totalEq": 1000.0}, "positions": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmp, self.policy():
            context = reconcile._report_business_context(raw, CYCLE)
            payload = build_push_payload.build(tmp, CYCLE)
        self.assertEqual(
            decision_card.MINIMAL_DECISION_PROTOCOL,
            context["decision_protocol"])
        self.assertNotIn("decision_card", context)
        self.assertEqual(raw["position_reviews"], context["position_reviews"])
        decision = payload["decision"]
        self.assertEqual(
            decision_card.MINIMAL_DECISION_PROTOCOL,
            decision["decision_protocol"])
        self.assertNotIn("decision_card", decision)
        self.assertNotIn("multitimeframe_analysis", decision)
        self.assertNotIn("multitimeframe_analyses", decision)


if __name__ == "__main__":
    unittest.main()
