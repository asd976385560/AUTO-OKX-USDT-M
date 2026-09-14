# -*- coding: utf-8 -*-
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

from collectors import trigger_agent
from core import candidate_bundle_runtime
from scripts import multitimeframe_decision_evidence as position_evidence
from scripts import stage_runner
from scripts import live_decision_facts
from scripts import live_position_action_runner as runner


CYCLE = "2026-09-02T12:00"
CST = timezone(timedelta(hours=8))


def _sealed(payload: dict, field: str) -> dict:
    result = dict(payload)
    result[field] = hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return result


class ClosurePromptTests(unittest.TestCase):
    def test_closure_prompt_uses_stage_owned_facts_and_filters_old_clauses(self):
        brief = (
            "可用市场事实\n"
            "必须补查 find_similar 并写 evidence_contract\n"
            "必须用MTF和三周期，再填写六项 decision_card\n"
            "candidate_id=cand_deadbeef 必须精确匹配\n"
            "4H方向未确认，暂不开仓\n"
            "1H结构冲突，暂不开仓\n"
            "15m信号未对齐，暂不开仓\n"
            "高低周期尚未共振，暂不开仓\n"
            "多周期确认不足，暂不开仓\n"
            "成交额低且OI低，暂不开仓\n"
            "成交不活跃，暂不开仓\n"
            "已有十七仓，暂不开仓\n"
            "无事件驱动，暂不开仓\n"
            "成交额/OI仅参与连续排序，无催化、已有仓位数或未触顶IMR均不得单独reject\n"
            "候选B 额$12M OI$3M 只作观察\n"
            "候选A可供本轮review"
        )
        with mock.patch.object(
            trigger_agent.thresholds,
            "minimal_contract_closure_active",
            return_value=True,
        ):
            message = trigger_agent._unified_live_message(
                CYCLE,
                brief,
                candidate_bundle={
                    "manifest_count": 427,
                    "decision_slice_count": 8,
                    "manifest_path": _public_project_path('tmp', 'full.json'),
                    "decision_slice_path": _public_project_path('tmp', 'review.json'),
                },
            )
        self.assertIn("stage_runner监督进程自动且只执行一次", message)
        self.assertIn("live_input_handoff_2026-09-02T12-00.json", message)
        self.assertIn("live_decision_facts.py", message)
        self.assertIn('"ts": "2026-09-02 12:00:00"', message)
        self.assertIn('"candidate_coverage"', message)
        self.assertIn("analysis_receipt_2026-09-02T12-00.json", message)
        self.assertIn("analyst_writer.py --input-file", message)
        self.assertIn("只按返回的精确error整文件修正一次", message)
        self.assertIn("必须写decision=provisional_open", message)
        self.assertIn("微观N/A换词偷渡reject", message)
        self.assertIn("候选A可供本轮review", message)
        self.assertIn("成交额/OI仅参与连续排序", message)
        self.assertIn("候选B 额$12M OI$3M 只作观察", message)
        for retired in (
            "find_similar", "evidence_contract", "MTF", "三周期", "六项",
            "decision_card", "ENTRY_READY", "exact candidate", "candidate_id",
            "4H方向", "1H结构", "15m信号", "高低周期", "多周期",
            "成交额低且OI低", "成交不活跃", "已有十七仓，暂不开仓",
            "无事件驱动，暂不开仓",
        ):
            self.assertNotIn(retired, message)


class ClosurePositionEvidenceTests(unittest.TestCase):
    def test_closure_live_facts_do_not_publish_retired_timeframe_basis(self):
        stamp = int(datetime(2026, 9, 2, 12, 5, tzinfo=CST).timestamp() * 1000)
        positions = [{
            "instId": "ETH-USDT-SWAP", "posSide": "long", "pos": "1",
            "avgPx": "100", "markPx": "101", "lever": "5",
            "mgnMode": "cross", "posId": "P1", "cTime": str(stamp - 3600000),
            "upl": "1", "uplRatio": "0.05", "imr": "20",
        }]
        balance = [{
            "totalEq": "1000",
            "details": [{"ccy": "USDT", "availEq": "980", "imr": "20",
                         "mmr": "1", "upl": "1"}],
        }]
        instruments = {"ETH-USDT-SWAP": {
            "instId": "ETH-USDT-SWAP", "ctVal": "1",
        }}
        algos = {"ETH-USDT-SWAP": [{
            "instId": "ETH-USDT-SWAP", "algoId": "A1",
            "slTriggerPx": "95", "slTriggerPxType": "mark",
            "posSide": "long", "side": "sell", "reduceOnly": "true",
            "state": "live", "sz": "1",
        }]}
        with mock.patch.object(
            live_decision_facts.thresholds,
            "minimal_contract_closure_active",
            return_value=True,
        ):
            payload = live_decision_facts.derive_facts(
                CYCLE, "live", positions, balance, instruments, algos,
                as_of_ms=stamp,
            )
            self.assertEqual([], live_decision_facts.validate_facts(payload))
        basis = payload["position_profit_review_policy"]["decision_basis"]
        self.assertEqual(
            "current_exchange_facts_plus_original_exit_plan_"
            "and_portfolio_opportunity_cost",
            basis,
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        for retired in ("15m", "1H", "4H", "MTF", "timeframe"):
            self.assertNotIn(retired, serialized)

    def test_error_view_is_always_v2_without_timeframes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            facts_file = root / "facts.json"
            out_file = root / "position.json"
            view_file = root / "view.json"
            facts_file.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                position_evidence.thresholds,
                "minimal_contract_closure_active",
                return_value=True,
            ), mock.patch.object(
                position_evidence,
                "build_position_exit_batch",
                side_effect=RuntimeError("forced"),
            ):
                rc = position_evidence.main([
                    "--db-root", str(root),
                    "--facts-file", str(facts_file),
                    "--cycle-id", CYCLE,
                    "--out-file", str(out_file),
                    "--decision-view-file", str(view_file),
                ])
            self.assertEqual(2, rc)
            payload = json.loads(out_file.read_text(encoding="utf-8"))
            view = json.loads(view_file.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["schema_version"])
            self.assertIs(payload["timeframe_judgment_used"], False)
            self.assertEqual(
                "position_exit_decision_view_v2_no_timeframes",
                view["schema"],
            )
            self.assertIs(view["timeframe_judgment_used"], False)
            self.assertTrue(all(
                "timeframes" not in row for row in view["positions"]
            ))

    def test_legacy_single_item_clis_are_noop_without_gate_calls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(
                position_evidence.thresholds,
                "minimal_contract_closure_active",
                return_value=True,
            ), mock.patch.object(
                position_evidence,
                "check_multitimeframe_readiness",
                side_effect=AssertionError("retired gate called"),
            ), mock.patch.object(
                position_evidence,
                "resolve_manifest_candidate",
                side_effect=AssertionError("identity gate called"),
            ):
                symbol_out = root / "symbol.json"
                self.assertEqual(0, position_evidence.main([
                    "--db-root", str(root),
                    "--symbol", "BTC-USDT-SWAP",
                    "--cycle-id", CYCLE,
                    "--out-file", str(symbol_out),
                ]))
                id_out = root / "id.json"
                self.assertEqual(0, position_evidence.main([
                    "--db-root", str(root),
                    "--candidate-id", "obsolete-id",
                    "--cycle-id", CYCLE,
                    "--out-file", str(id_out),
                ]))
            for path in (symbol_out, id_out):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual("NOT_REQUIRED", payload["status"])
                self.assertIs(payload["identity_enforced"], False)
                self.assertIs(payload["timeframe_judgment_used"], False)
                self.assertNotIn("evidence_contract", payload)

    def test_closure_manifest_does_not_validate_candidate_id(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "manifest.json"
            core = {
                "schema": "briefing_candidate_manifest_v3_side_neutral",
                "identity_contract": (
                    "symbol_review_v2_full_manifest_no_identity_gate"),
                "cycle_id": CYCLE,
                "candidate_count": 2,
                "review_slice_count": 1,
                "review_symbols": ["ETH-USDT-SWAP"],
                "candidates": [{
                    "ordinal": 1,
                    "candidate_id": "not-an-exact-id",
                    "symbol": "BTC-USDT-SWAP",
                    "side": None,
                    "eligible_sides": ["long", "short"],
                    "layer": "all_market",
                    "rotation_due": False,
                    "recent_deep_dives_6h": 0,
                    "recent_rejections_6h": 0,
                }, {
                    "ordinal": 2,
                    "candidate_id": "not-an-exact-id",
                    "symbol": "ETH-USDT-SWAP",
                    "side": None,
                    "eligible_sides": ["long", "short"],
                    "layer": "all_market",
                    "rotation_due": False,
                    "recent_deep_dives_6h": 0,
                    "recent_rejections_6h": 0,
                }],
            }
            payload = _sealed(core, "manifest_sha256")
            path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                position_evidence.thresholds,
                "minimal_contract_closure_active",
                return_value=True,
            ), mock.patch.object(
                position_evidence,
                "check_multitimeframe_readiness_batch",
                side_effect=AssertionError("retired batch gate called"),
            ):
                loaded, _ = position_evidence.load_candidate_manifest(
                    path, CYCLE)
                bundle = position_evidence.build_candidate_evidence_bundle(
                    Path(td), path, CYCLE)
            self.assertEqual(payload, loaded)
            self.assertTrue(bundle["ok"])
            self.assertIs(bundle["timeframe_judgment_used"], False)
            self.assertEqual(
                ["ETH-USDT-SWAP"], bundle["review_symbols"])
            self.assertEqual("ETH-USDT-SWAP", bundle["items"][0]["symbol"])
            self.assertEqual(2, bundle["items"][0]["manifest_ordinal"])
            self.assertNotIn("candidate_id", bundle["items"][0])
            self.assertTrue(all(
                "candidate_id" not in row
                for row in bundle["screening_index"]
            ))
            with mock.patch.object(
                position_evidence.thresholds,
                "minimal_contract_closure_active",
                return_value=True,
            ):
                self.assertEqual(
                    [], position_evidence.validate_candidate_evidence_bundle(
                        bundle,
                        expected_cycle=CYCLE,
                        expected_manifest_path=path,
                    ))

    def test_runtime_slice_carries_only_symbol_review_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {
                "manifest_sha256": "a" * 64,
                "identity_contract": (
                    "symbol_review_v2_full_manifest_no_identity_gate"),
                "review_symbols": ["ETH-USDT-SWAP"],
                "candidate_count": 2,
            }
            bundle = {
                "candidate_count": 1,
                "manifest_count": 2,
                "screened_count": 2,
                "ready_count": 2,
                "not_ready_count": 0,
                "decision_slice_count": 1,
                "decision_ready_count": 1,
                "bundle_sha256": "b" * 64,
                "elapsed_seconds": 0.01,
                "review_identity_contract": (
                    "symbol_review_v2_full_manifest_no_identity_gate"),
                "review_symbols": ["ETH-USDT-SWAP"],
                "items": [{
                    "ordinal": 1,
                    "manifest_ordinal": 2,
                    "symbol": "ETH-USDT-SWAP",
                    "review_identity": "symbol",
                }],
            }
            process = mock.Mock(returncode=0, stdout="{}", stderr="")
            with mock.patch.object(
                candidate_bundle_runtime,
                "load_candidate_manifest",
                return_value=(manifest, "c" * 64),
            ), mock.patch.object(
                candidate_bundle_runtime,
                "load_candidate_evidence_bundle",
                return_value=bundle,
            ), mock.patch.object(
                candidate_bundle_runtime.subprocess,
                "run",
                return_value=process,
            ), mock.patch.object(
                candidate_bundle_runtime.thresholds,
                "minimal_contract_closure_active",
                return_value=True,
            ), mock.patch.object(
                candidate_bundle_runtime.thresholds,
                "minimal_decision_contract_active",
                return_value=True,
            ):
                status = candidate_bundle_runtime.prepare_candidate_bundle(
                    cycle_id=CYCLE,
                    db_root=root,
                    phase="consume",
                    timeout_seconds=12,
                    evidence_root=root,
                )
            self.assertEqual("PASSED", status["status"])
            slice_path = position_evidence.candidate_evidence_paths(
                CYCLE, root=root)["decision_slice"]
            decision_slice = json.loads(
                slice_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "candidate_decision_slice_v3_symbol_review",
                decision_slice["schema"],
            )
            self.assertEqual(
                ["ETH-USDT-SWAP"], decision_slice["review_symbols"])
            self.assertNotIn("candidate_id", decision_slice["items"][0])


class DeterministicFactsHandoffTests(unittest.TestCase):
    def _runner_artifacts(self, root: Path, *, status: str = "ready"):
        safe = CYCLE.replace(":", "-")
        facts_path = root / f"live_facts_{safe}.json"
        view_path = root / f"position_exit_view_{safe}.json"
        handoff_path = root / f"live_input_handoff_{safe}.json"
        facts = _sealed({
            "schema_version": 1,
            "source": "okx_private_api",
            "cycle_id": CYCLE,
            "profile": "live",
            "status": "ok",
            "errors": [],
            "positions": [],
        }, "facts_hash")
        view = _sealed({
            "schema": "position_exit_decision_view_v2_no_timeframes",
            "cycle_id": CYCLE,
            "facts_hash": facts["facts_hash"],
            "source_evidence_hash": "e" * 64,
            "source_status": "PASSED",
            "position_count": 0,
            "timeframe_judgment_used": False,
        }, "view_hash")
        handoff = {
            "schema_version": 1,
            "cycle_id": CYCLE,
            "status": status,
            "detail_status": "ready" if status == "ready" else status,
            "facts_file": str(facts_path),
            "decision_view_file": str(view_path),
            "facts_hash": facts["facts_hash"],
            "facts_status": "ok",
            "decision_view_hash": view["view_hash"],
            "position_count": 0,
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        facts_path.write_text(json.dumps(facts), encoding="utf-8")
        view_path.write_text(json.dumps(view), encoding="utf-8")
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        plan = {
            "cycle_id": CYCLE,
            "receipt_context": {
                "cycle_id": CYCLE,
                "status": "ok",
                "mode": "live",
                "decision_protocol": "minimal_decision_v2",
                "reasoning": "stage-bound HOLD",
                "regime": "range",
            },
            "actions": [],
        }
        return plan, facts, view, handoff, facts_path, view_path, handoff_path

    def test_runner_requires_ready_stage_owned_handoff_before_plan_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan, facts, _view, _handoff, facts_path, view_path, handoff_path = \
                self._runner_artifacts(root)
            handoff_path.unlink()
            state_path = root / "state.json"
            with mock.patch.object(
                    runner.thresholds, "minimal_contract_closure_active",
                    return_value=True), mock.patch.object(
                    runner, "preflight_plan") as preflight, mock.patch.object(
                    runner, "_call_executor") as executor:
                with self.assertRaisesRegex(
                        runner.PlanError, "stage_input_handoff_missing"):
                    runner.execute_position_plan(
                        plan, facts, cycle_id=CYCLE, db_root=root,
                        receipt_file=root / "receipt.json",
                        facts_file=facts_path,
                        decision_view_file=view_path,
                        live_input_handoff_file=handoff_path,
                        state_file=state_path, nudge=False)
            self.assertFalse(state_path.exists())
            preflight.assert_not_called()
            executor.assert_not_called()

    def test_runner_rejects_nested_retired_structures_before_executor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan, facts, _view, _handoff, facts_path, view_path, handoff_path = \
                self._runner_artifacts(root)
            plan["receipt_context"]["position_reviews"] = [{
                "reason": "ordinary price review",
                "legacy": {
                    "decision_card": {
                        "contract": "lightweight_open_v1",
                        "risk_reward": {"entry": 10, "stop": 9, "target": 12},
                    },
                    "candidate_id": "cand_obsolete",
                    "review_hash": "f" * 64,
                    "opportunity_state": "ENTRY_READY",
                },
            }]
            state_path = root / "state.json"
            with mock.patch.object(
                    runner.thresholds, "minimal_contract_closure_active",
                    return_value=True), mock.patch.object(
                    runner, "validate_facts", return_value=[]), \
                    mock.patch.object(runner, "preflight_plan") as preflight, \
                    mock.patch.object(runner, "_call_executor") as executor:
                with self.assertRaisesRegex(
                        runner.PlanError, "退役机器结构"):
                    runner.execute_position_plan(
                        plan, facts, cycle_id=CYCLE, db_root=root,
                        receipt_file=root / "receipt.json",
                        facts_file=facts_path,
                        decision_view_file=view_path,
                        live_input_handoff_file=handoff_path,
                        state_file=state_path, plan_sha256="a" * 64,
                        nudge=False)
            self.assertFalse(state_path.exists())
            preflight.assert_not_called()
            executor.assert_not_called()

    def test_cli_checks_stage_handoff_before_reading_agent_plan(self):
        with mock.patch.object(
                runner.thresholds, "minimal_contract_closure_active",
                return_value=True), mock.patch.object(
                runner, "_require_direct_tmp_path",
                side_effect=lambda path, _label: Path(path)), \
                mock.patch.object(
                    runner, "_precheck_stage_owned_live_input_handoff",
                    side_effect=runner.PlanError("forced not ready")), \
                mock.patch.object(runner, "_read_json_with_sha") as read_plan:
            rc = runner.main([
                "--cycle-id", CYCLE,
                "--plan-file", _public_project_path('tmp', 'position_plan_2026-09-02T12-00.json'),
                "--facts-file", _public_project_path('tmp', 'live_facts_2026-09-02T12-00.json'),
                "--receipt-file", _public_project_path('tmp', 'receipt.json'),
            ])
        self.assertEqual(2, rc)
        read_plan.assert_not_called()

    def test_closure_execution_reasons_reject_retired_authority_terms(self):
        cases = (
            ("context", "MTF未确认但继续HOLD"),
            ("review", "4H方向确认后退出"),
            ("action", "ENTRY_READY状态授权平仓"),
            ("state", "成熟候选状态允许调整保护"),
        )
        for label, reason in cases:
            with self.subTest(label=label):
                plan = {
                    "receipt_context": {
                        "reasoning": "正常组合复核",
                        "position_reviews": [{"reason": "价格失效复核"}],
                    },
                    "actions": [{
                        "action": "CLOSE", "reasoning": "价格失效",
                    }],
                }
                if label == "context":
                    plan["receipt_context"]["reasoning"] = reason
                elif label == "review":
                    plan["receipt_context"]["position_reviews"][0]["reason"] = reason
                else:
                    plan["actions"][0]["reasoning"] = reason
                with self.assertRaisesRegex(
                        runner.PlanError, "禁止使用已删除"):
                    runner._validate_closure_plan_reasoning(plan)

        allowed = {
            "receipt_context": {
                "reasoning": "组合风险与价格失效点复核完成",
                "position_reviews": [{
                    "reason": "止损保护完整，当前价格未触失效点"}],
            },
            "actions": [{
                "action": "REDUCE", "reasoning": "降低止损风险敞口"}],
        }
        runner._validate_closure_plan_reasoning(allowed)

    def test_runner_defensively_rejects_retired_canonical_open_reason(self):
        package = {
            "contract": "open_execution_package_v1",
            "entry": 10.0, "stop": 9.0, "target": 12.0,
            "exit_mode": "fixed_tp",
        }
        plan = {
            "cycle_id": CYCLE,
            "receipt_context": {
                "cycle_id": CYCLE, "status": "ok", "mode": "live",
                "decision_protocol": "minimal_decision_v2",
                "reasoning": "正常组合复核", "regime": "range",
            },
            "actions": [{
                "action": "OPEN", "symbol": "X-USDT-SWAP",
                "side": "long", "target_stop_risk_pct_equity": 0.01,
                "lev": 5.0,
            }],
        }
        facts = {
            "cycle_id": CYCLE, "profile": "live", "status": "ok",
            "positions": [], "balance": {"totalEq": 1000.0},
            "action_policy": {
                "position_truth_verified": True,
                "allowed_executor_actions": ["open"],
            },
        }
        with mock.patch.object(
                runner.thresholds, "minimal_contract_closure_active",
                return_value=True), mock.patch.object(
                runner.thresholds, "minimal_decision_contract_active",
                return_value=True), mock.patch.object(
                runner, "validate_facts", return_value=[]), \
                mock.patch.object(
                    runner.oe, "validate_receipt_context", return_value=[]), \
                mock.patch.object(runner, "_load_analysis_signal", return_value={
                    "action": "open_long", "side": "long",
                    "reasoning": "4H方向确认并由ENTRY_READY授权开仓",
                    "open_execution_package": package,
                }):
            with self.assertRaisesRegex(
                    runner.PlanError, "canonical_analysis_reasoning"):
                runner.preflight_plan(
                    plan, facts, cycle_id=CYCLE, db_root=Path("E:/unused"))

    def test_runner_rejects_preparing_failed_and_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for status in ("preparing", "failed"):
                with self.subTest(status=status):
                    plan, facts, _view, _handoff, facts_path, view_path, handoff_path = \
                        self._runner_artifacts(root, status=status)
                    with self.assertRaisesRegex(
                            runner.PlanError, "stage_input_handoff_not_ready"):
                        runner._validate_stage_owned_live_input_binding(
                            plan, facts, cycle_id=CYCLE,
                            plan_sha256="a" * 64,
                            facts_file=facts_path,
                            decision_view_file=view_path,
                            handoff_file=handoff_path)
            for label in (
                "facts_hash", "decision_view_hash", "position_count",
                "cycle_id",
            ):
                with self.subTest(label=label):
                    _plan, _facts, _view, handoff, facts_path, view_path, handoff_path = \
                        self._runner_artifacts(root)
                    mutate_target = {
                        "facts_hash": lambda: handoff.update(facts_hash="0" * 64),
                        "decision_view_hash": lambda: handoff.update(
                            decision_view_hash="1" * 64),
                        "position_count": lambda: handoff.update(position_count=1),
                        "cycle_id": lambda: handoff.update(
                            cycle_id="2026-09-02T12:15"),
                    }[label]
                    mutate_target()
                    handoff_path.write_text(
                        json.dumps(handoff), encoding="utf-8")
                    with mock.patch.object(
                            runner, "validate_facts", return_value=[]):
                        with self.assertRaises(runner.PlanError):
                            runner._validate_stage_owned_live_input_binding(
                                _plan, _facts, cycle_id=CYCLE,
                                plan_sha256="a" * 64,
                                facts_file=facts_path,
                                decision_view_file=view_path,
                                handoff_file=handoff_path)

    def test_runner_rechecks_handoff_after_preflight_to_close_replacement_race(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan, facts, view, handoff, facts_path, view_path, handoff_path = \
                self._runner_artifacts(root)
            state_path = root / "state.json"

            def raced_preflight(*_args, **_kwargs):
                raced = dict(view)
                raced["position_count"] = 1
                raced = _sealed({
                    key: value for key, value in raced.items()
                    if key != "view_hash"
                }, "view_hash")
                view_path.write_text(json.dumps(raced), encoding="utf-8")
                return (dict(plan["receipt_context"]), [{
                    "action": "CLOSE", "symbol": "X-USDT-SWAP",
                    "pos_side": "long", "reasoning": "race probe",
                }])

            with mock.patch.object(
                    runner.thresholds, "minimal_contract_closure_active",
                    return_value=True), mock.patch.object(
                    runner, "validate_facts", return_value=[]), \
                    mock.patch.object(
                        runner, "preflight_plan", side_effect=raced_preflight), \
                    mock.patch.object(
                        runner, "_validate_position_exit_evidence"), \
                    mock.patch.object(runner, "_call_executor") as executor:
                with self.assertRaisesRegex(
                        runner.PlanError, "stage_input_binding_invalid"):
                    runner.execute_position_plan(
                        plan, facts, cycle_id=CYCLE, db_root=root,
                        receipt_file=root / "receipt.json",
                        facts_file=facts_path,
                        decision_view_file=view_path,
                        live_input_handoff_file=handoff_path,
                        state_file=state_path, plan_sha256="a" * 64,
                        nudge=False)
            executor.assert_not_called()

    def test_prepare_validates_facts_position_and_view_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            facts_path = root / "facts.json"
            position_path = root / "position.json"
            view_path = root / "view.json"
            facts = _sealed({
                "schema_version": 1,
                "source": "okx_private_api",
                "cycle_id": CYCLE,
                "profile": "live",
                "status": "blocking",
                "errors": ["private_source_incomplete"],
                "positions": [],
            }, "facts_hash")
            position = _sealed({
                "schema_version": 2,
                "cycle_id": CYCLE,
                "facts_hash": facts["facts_hash"],
                "status": "PASSED",
                "position_count": 0,
                "timeframe_judgment_used": False,
            }, "evidence_hash")
            view = _sealed({
                "schema": "position_exit_decision_view_v2_no_timeframes",
                "cycle_id": CYCLE,
                "facts_hash": facts["facts_hash"],
                "source_evidence_hash": position["evidence_hash"],
                "source_status": "PASSED",
                "position_count": 0,
                "timeframe_judgment_used": False,
            }, "view_hash")

            def fake_run(command, **_kwargs):
                if stage_runner.LIVE_DECISION_FACTS.as_posix() in command:
                    facts_path.write_text(json.dumps(facts), encoding="utf-8")
                    return subprocess.CompletedProcess(command, 2, "{}", "")
                else:
                    position_path.write_text(
                        json.dumps(position), encoding="utf-8")
                    view_path.write_text(json.dumps(view), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "{}", "")

            def fake_guarded(command, **kwargs):
                completed = fake_run(command, **kwargs)
                return completed.returncode, completed.stdout, completed.stderr, False

            with mock.patch.object(
                stage_runner._proc, "run_guarded", side_effect=fake_guarded):
                result = stage_runner._prepare_deterministic_live_inputs(
                    cycle=CYCLE,
                    facts_file=facts_path,
                    position_exit_file=position_path,
                    decision_view_file=view_path,
                    db_root=root,
                    log_file=root / "input.log",
                )
            self.assertTrue(result["ok"])
            self.assertEqual("ready", result["status"])
            self.assertEqual("blocking", result["facts_status"])
            self.assertEqual(0, result["position_count"])
            self.assertEqual(0, result["orders_placed"])

    def test_observer_prepares_once_and_failure_stops_before_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_root = root / "db"
            tmp_root = root / "tmp"
            db_root.mkdir()
            tmp_root.mkdir()
            con = sqlite3.connect(db_root / "analysis.db")
            con.execute(
                "CREATE TABLE analysis_runs(cycle_id TEXT,status TEXT,ts TEXT)")
            con.execute(
                "INSERT INTO analysis_runs VALUES(?,?,?)",
                (CYCLE, "ok", "2026-09-02 12:01:00"),
            )
            con.commit()
            con.close()
            calls = []

            def fail_prepare(**kwargs):
                calls.append(kwargs)
                return {
                    "ok": False,
                    "status": "facts_validation_failed",
                    "error": "forced",
                }

            observer = stage_runner._LiveChildObserver(
                CYCLE,
                tmp_root=tmp_root,
                db_root=db_root,
                now_fn=lambda: datetime(
                    2026, 9, 2, 12, 2, tzinfo=CST).timestamp(),
                enforce_analysis_deadline=True,
                auto_prepare_live_inputs=True,
                live_input_prepare_fn=fail_prepare,
            )
            observer.poll_deterministic_live_inputs()
            observer.poll_deterministic_live_inputs()
            self.assertEqual(1, len(calls))
            handoff = json.loads(
                observer.live_input_status_path.read_text(encoding="utf-8"))
            self.assertEqual("failed", handoff["status"])
            self.assertNotIn("positions", handoff)
            reason = observer()
            self.assertEqual(
                "deterministic_live_input_failed:facts_validation_failed",
                reason,
            )
            self.assertFalse(observer.plan_path.exists())


if __name__ == "__main__":
    unittest.main()
