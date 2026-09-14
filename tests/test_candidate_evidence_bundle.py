# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import multitimeframe_gate as gate
from core import candidate_quality_contract as quality
from core import candidate_bundle_runtime as runtime
from scripts import multitimeframe_decision_evidence as evidence


CYCLE = "2026-08-29T08:30"


def _manifest(path: Path, candidates: list[dict]) -> dict:
    core = {
        "schema": evidence.CANDIDATE_MANIFEST_SCHEMA,
        "cycle_id": CYCLE,
        "tick_ts": "2026-08-29T00:30:00Z",
        "written_at_cst": "2026-08-29 08:31:00",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    payload = {
        **core,
        "manifest_sha256": evidence._canonical_sha256(core),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _candidate(ordinal: int, symbol: str, *, rotation_due: bool) -> dict:
    return {
        "ordinal": ordinal,
        "layer": "mature" if ordinal == 1 else "early",
        "symbol": symbol,
        "side": "long" if ordinal == 1 else "short",
        "last": float(ordinal),
        "chg24h": float(ordinal),
        "rotation_due": rotation_due,
        "recent_deep_dives_6h": 0 if rotation_due else 3,
        "recent_rejections_6h": 0 if rotation_due else 2,
    }


def _result(symbol: str, *, ready: bool) -> dict:
    contract = gate.seal_evidence_contract({
        "protocol": gate.EVIDENCE_PROTOCOL,
        "mode": "read_only",
        "symbol": symbol,
        "cycle_id": CYCLE,
        "required_timeframes": ["15m", "1H", "4H"],
        "minimum_bars_for_full_indicators": 34,
        "timeframes": {},
        "production_database_writes": 0,
        "orders_placed": 0,
    })
    return {
        "ready": ready,
        "status": "PASSED" if ready else "NOT_READY",
        "timeframes": [],
        "evidence_contract": contract,
        "production_database_writes": 0,
        "orders_placed": 0,
    }


class CandidateEvidenceBundleTests(unittest.TestCase):
    @staticmethod
    def _install_bundle(root: Path, candidates: list[dict], results: list[dict]):
        paths = evidence.candidate_evidence_paths(CYCLE, root=root)
        paths["manifest"].parent.mkdir(parents=True, exist_ok=True)
        _manifest(paths["manifest"], candidates)
        with mock.patch.object(
            evidence, "check_multitimeframe_readiness_batch",
            return_value=results,
        ):
            bundle = evidence.build_candidate_evidence_bundle(
                root, paths["manifest"], CYCLE)
        evidence._atomic_json(paths["bundle"], bundle)
        return paths, bundle

    def test_ready_and_not_ready_candidates_form_one_valid_ordered_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            candidates = [
                _candidate(1, "AAA-USDT-SWAP", rotation_due=False),
                _candidate(2, "BBB-USDT-SWAP", rotation_due=True),
            ]
            manifest = _manifest(manifest_path, candidates)
            with mock.patch.object(
                evidence,
                "check_multitimeframe_readiness_batch",
                return_value=[
                    _result("AAA-USDT-SWAP", ready=True),
                    _result("BBB-USDT-SWAP", ready=False),
                ],
            ) as batch:
                bundle = evidence.build_candidate_evidence_bundle(
                    root, manifest_path, CYCLE)
            batch.assert_called_once_with(
                root, ["AAA-USDT-SWAP", "BBB-USDT-SWAP"], CYCLE)
            self.assertTrue(bundle["ok"])
            self.assertEqual(2, bundle["screened_count"])
            self.assertEqual(1, bundle["ready_count"])
            self.assertEqual(manifest["manifest_sha256"],
                             bundle["candidate_manifest_sha256"])
            self.assertEqual([], evidence.validate_candidate_evidence_bundle(
                bundle,
                expected_cycle=CYCLE,
                expected_manifest_path=manifest_path,
            ))

    def test_manifest_rejects_duplicate_invalid_or_wrong_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "manifest.json"
            duplicate = [
                _candidate(1, "AAA-USDT-SWAP", rotation_due=True),
                _candidate(2, "AAA-USDT-SWAP", rotation_due=True),
            ]
            _manifest(path, duplicate)
            with self.assertRaisesRegex(ValueError, "duplicated"):
                evidence.load_candidate_manifest(path, CYCLE)
            payload = _manifest(
                path, [_candidate(1, "AAA-USDT-SWAP", rotation_due=True)])
            payload["cycle_id"] = "2026-08-29T08:45"
            core = dict(payload)
            core.pop("manifest_sha256", None)
            payload["manifest_sha256"] = evidence._canonical_sha256(core)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cycle mismatch"):
                evidence.load_candidate_manifest(path, CYCLE)

    def test_single_candidate_mode_resolves_symbol_only_from_exact_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "manifest.json"
            candidate = _candidate(
                1, "SKHY-USDT-SWAP", rotation_due=True)
            candidate.update({
                "candidate_id": "cand_" + "1" * 20,
                "prior_evidence_hash": None,
                "new_evidence_required": False,
            })
            _manifest(path, [candidate])
            with mock.patch.object(
                evidence, "check_multitimeframe_readiness",
                return_value=_result("SKHY-USDT-SWAP", ready=True),
            ) as check:
                payload = evidence.build_candidate_bound_evidence(
                    root, path, candidate["candidate_id"], CYCLE)
            check.assert_called_once_with(root, "SKHY-USDT-SWAP", CYCLE)
            self.assertEqual("SKHY-USDT-SWAP", payload["symbol"])
            self.assertEqual(candidate["candidate_id"], payload["candidate_id"])

            with mock.patch.object(
                    evidence, "check_multitimeframe_readiness") as check:
                with self.assertRaisesRegex(ValueError, "exactly once"):
                    evidence.build_candidate_bound_evidence(
                        root, path, "cand_" + "2" * 20, CYCLE)
            check.assert_not_called()

    def test_bundle_hash_and_manifest_order_tampering_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "manifest.json"
            _manifest(path, [
                _candidate(1, "AAA-USDT-SWAP", rotation_due=True)])
            with mock.patch.object(
                evidence,
                "check_multitimeframe_readiness_batch",
                return_value=[_result("AAA-USDT-SWAP", ready=True)],
            ):
                bundle = evidence.build_candidate_evidence_bundle(
                    root, path, CYCLE)
            bundle["items"][0]["side"] = "short"
            core = dict(bundle)
            core.pop("bundle_sha256", None)
            bundle["bundle_sha256"] = evidence._canonical_sha256(core)
            errors = evidence.validate_candidate_evidence_bundle(
                bundle, expected_cycle=CYCLE, expected_manifest_path=path)
            self.assertTrue(any("manifest side mismatch" in item
                                for item in errors), errors)

    def test_atomic_output_has_no_temporary_residue(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bundle.json"
            evidence._atomic_json(path, {"ok": True})
            self.assertEqual({"ok": True}, json.loads(
                path.read_text(encoding="utf-8")))
            self.assertEqual([], list(path.parent.glob("*.tmp")))

    def test_shadow_measures_invalid_structure_without_filtering_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = _candidate(
                1, "AAA-USDT-SWAP", rotation_due=False)
            paths, bundle = self._install_bundle(
                root, [candidate], [_result("AAA-USDT-SWAP", ready=True)])
            contract_hash = bundle["items"][0]["evidence_hash"]
            signal = {
                "symbol": "AAA-USDT-SWAP",
                "action": "open_long",
                "decision_card": {"multitimeframe_analysis": {
                    "evidence_contract": {"evidence_hash": contract_hash}}},
            }
            raw, signals, result = quality.normalize_candidate_quality(
                cycle_id=CYCLE,
                raw={
                    "candidate_screening": {
                        "bundle_path": str(paths["bundle"]).replace("\\", "/"),
                        "bundle_status": "shadow",
                    },
                    "candidates_deep_dived_v2": [{
                        "symbol": "AAA-USDT-SWAP",
                        "side": "long",
                        "layer": "mature",
                        "evidence_hash": contract_hash,
                        "decision": "selected",
                        "reason_code": "selected_open",
                        "reason": "结构尚可",
                    }],
                    "candidate_coverage": {
                        "dynamic_limit": 1,
                        "dynamic_target": 1,
                        "stop_reason": "target_reached",
                    },
                },
                signals=[signal],
                phase="shadow",
                evidence_root=root,
            )
            self.assertEqual([signal], signals)
            self.assertEqual("NOT_MET", result["status"])
            self.assertFalse(raw["candidate_quality"]["position_exit_path_blocked"])

    def test_forward_identity_gate_filters_wrong_open_but_retains_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = _candidate(
                1, "SKHY-USDT-SWAP", rotation_due=False)
            candidate.update({
                "candidate_id": "cand_" + "3" * 20,
                "prior_evidence_hash": None,
                "new_evidence_required": False,
            })
            _paths, bundle = self._install_bundle(
                root, [candidate], [_result("SKHY-USDT-SWAP", ready=True)])
            contract_hash = bundle["items"][0]["evidence_hash"]
            wrong_open = {
                "symbol": "SKHYNIX-USDT-SWAP",
                "action": "open_long",
                "decision_card": {"multitimeframe_analysis": {
                    "evidence_contract": {"evidence_hash": contract_hash}}},
            }
            close = {
                "symbol": "HELD-USDT-SWAP", "action": "close", "side": "long"}
            with mock.patch.object(
                    quality.thresholds, "candidate_exact_identity_enforced",
                    return_value=True):
                raw, signals, result = quality.normalize_candidate_quality(
                    cycle_id=CYCLE,
                    raw={
                        "candidates_deep_dived_v2": [{
                            "candidate_id": candidate["candidate_id"],
                            "symbol": "SKHYNIX-USDT-SWAP",
                            "side": "long", "layer": "mature",
                            "evidence_hash": contract_hash,
                            "decision": "selected",
                            "supporting_evidence": ["4H"],
                            "opposing_evidence": ["cost"],
                            "invalidation_condition": "1H close",
                            "reason_code": "selected_open",
                            "reason_family": "OTHER_AGENT_JUDGMENT",
                            "reason": "selected",
                        }],
                        "candidate_coverage": {
                            "dynamic_limit": 1, "dynamic_target": 1,
                            "stop_reason": "target_reached",
                        },
                    },
                    signals=[wrong_open, close],
                    phase="shadow",
                    evidence_root=root,
                )
            self.assertEqual([close], signals)
            self.assertEqual(1, len(result["identity_rejected_open_signals"]))
            self.assertTrue(raw["candidate_identity"]["active"])

    def test_manifest_only_identity_gate_keeps_exact_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = _candidate(
                1, "AAA-USDT-SWAP", rotation_due=False)
            candidate.update({
                "candidate_id": "cand_" + "6" * 20,
                "prior_evidence_hash": None,
                "new_evidence_required": False,
                "opportunity_id": "opp_" + "7" * 20,
            })
            _paths, bundle = self._install_bundle(
                root, [candidate], [_result("AAA-USDT-SWAP", ready=True)])
            contract_hash = bundle["items"][0]["evidence_hash"]
            signal = {
                "candidate_id": candidate["candidate_id"],
                "symbol": "AAA-USDT-SWAP", "action": "open_long",
                "decision_card": {"multitimeframe_analysis": {
                    "evidence_contract": {"evidence_hash": contract_hash}}},
            }
            with mock.patch.object(
                    quality.thresholds, "candidate_exact_identity_enforced",
                    return_value=True):
                raw, signals, result = quality.normalize_candidate_quality(
                    cycle_id=CYCLE,
                    raw={
                        "candidates_deep_dived_v2": [{
                            "candidate_id": candidate["candidate_id"],
                            "symbol": "AAA-USDT-SWAP", "side": "long",
                            "layer": "mature", "evidence_hash": contract_hash,
                            "decision": "selected",
                            "supporting_evidence": ["4H"],
                            "opposing_evidence": ["cost"],
                            "invalidation_condition": "1H close",
                            "reason_code": "selected_open",
                            "reason_family": "OTHER_AGENT_JUDGMENT",
                            "reason": "selected",
                        }],
                        "candidate_coverage": {
                            "dynamic_limit": 1, "dynamic_target": 1,
                            "stop_reason": "target_reached",
                        },
                    },
                    signals=[signal], phase="manifest_only",
                    evidence_root=root,
                )
            self.assertEqual(1, len(signals))
            self.assertEqual(candidate["candidate_id"], signals[0]["candidate_id"])
            self.assertEqual(candidate["opportunity_id"], signals[0]["opportunity_id"])
            self.assertEqual([], result["identity_rejected_open_signals"])
            self.assertEqual(
                "NOT_APPLICABLE", raw["candidate_screening"]["bundle_status"])

    def test_consume_filters_only_invalid_open_and_retains_non_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = _candidate(
                1, "AAA-USDT-SWAP", rotation_due=False)
            paths, bundle = self._install_bundle(
                root, [candidate], [_result("AAA-USDT-SWAP", ready=True)])
            contract_hash = bundle["items"][0]["evidence_hash"]
            open_signal = {
                "symbol": "AAA-USDT-SWAP",
                "action": "open_long",
                "decision_card": {"multitimeframe_analysis": {
                    "evidence_contract": {"evidence_hash": contract_hash}}},
            }
            close_signal = {
                "symbol": "HELD-USDT-SWAP",
                "action": "close",
                "side": "long",
            }
            _, signals, result = quality.normalize_candidate_quality(
                cycle_id=CYCLE,
                raw={
                    "candidates_deep_dived_v2": [],
                    "candidate_coverage": {
                        "dynamic_limit": 1,
                        "dynamic_target": 1,
                        "stop_reason": "candidate_shortfall",
                    },
                },
                signals=[open_signal, close_signal],
                phase="consume",
                evidence_root=root,
            )
            self.assertEqual([close_signal], signals)
            self.assertEqual("quality_valid_deep_dive_missing",
                             result["rejected_open_signals"][0]["reason"])

    def test_consume_valid_complete_structure_keeps_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = _candidate(
                1, "AAA-USDT-SWAP", rotation_due=False)
            paths, bundle = self._install_bundle(
                root, [candidate], [_result("AAA-USDT-SWAP", ready=True)])
            contract_hash = bundle["items"][0]["evidence_hash"]
            signal = {
                "symbol": "AAA-USDT-SWAP",
                "action": "open_long",
                "decision_card": {"multitimeframe_analysis": {
                    "evidence_contract": {"evidence_hash": contract_hash}}},
            }
            raw, signals, result = quality.normalize_candidate_quality(
                cycle_id=CYCLE,
                raw={
                    "candidate_screening": {
                        "bundle_path": str(paths["bundle"]).replace("\\", "/"),
                        "bundle_status": "shadow",
                    },
                    "candidates_deep_dived_v2": [{
                        "symbol": "AAA-USDT-SWAP",
                        "side": "long",
                        "layer": "mature",
                        "evidence_hash": contract_hash,
                        "decision": "selected",
                        "supporting_evidence": ["4H结构支持"],
                        "opposing_evidence": ["15m动量转弱"],
                        "invalidation_condition": {
                            "timeframe": "1H", "condition": "收盘跌破结构位"},
                        "reason_code": "selected_open",
                        "reason": "正反证据完整且失效条件明确",
                    }],
                    "candidate_coverage": {
                        "dynamic_limit": 1,
                        "dynamic_target": 1,
                        "stop_reason": "target_reached",
                    },
                },
                signals=[signal],
                phase="consume",
                evidence_root=root,
            )
            self.assertEqual([signal], signals)
            self.assertEqual("MET", result["status"])
            self.assertEqual(
                "PASSED", raw["candidate_screening"]["bundle_status"])
            self.assertEqual(1, raw["candidate_coverage"]["quality_valid_count"])

    def test_candidate_id_and_reason_family_bind_exact_manifest_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = _candidate(
                1, "AAA-USDT-SWAP", rotation_due=False)
            candidate.update({
                "candidate_id": "cand_" + "a" * 20,
                "prior_evidence_hash": None,
                "new_evidence_required": False,
            })
            paths, bundle = self._install_bundle(
                root, [candidate], [_result("AAA-USDT-SWAP", ready=True)])
            contract_hash = bundle["items"][0]["evidence_hash"]
            raw, _signals, result = quality.normalize_candidate_quality(
                cycle_id=CYCLE,
                raw={
                    "candidates_deep_dived_v2": [{
                        "candidate_id": candidate["candidate_id"],
                        "symbol": "AAA-USDT-SWAP",
                        "side": "long",
                        "layer": "mature",
                        "evidence_hash": contract_hash,
                        "decision": "reject",
                        "supporting_evidence": ["4H结构支持"],
                        "opposing_evidence": ["15m反向"],
                        "invalidation_condition": "1H收盘失效",
                        "reason_code": "htf_not_aligned",
                        "reason_family": "LOWER_TIMEFRAME_AGAINST",
                        "reason": "多周期尚未一致",
                    }],
                    "candidate_coverage": {
                        "dynamic_limit": 1,
                        "dynamic_target": 1,
                        "stop_reason": "target_reached",
                    },
                },
                signals=[],
                phase="shadow",
                evidence_root=root,
            )
            self.assertEqual("MET", result["status"])
            self.assertEqual(
                candidate["candidate_id"],
                raw["candidates_deep_dived_v2"][0]["candidate_id"],
            )
            self.assertEqual(
                "MTF_CONFLICT",
                raw["candidates_deep_dived_v2"][0]["reason_family"],
            )
            self.assertEqual(
                "LOWER_TIMEFRAME_AGAINST",
                raw["candidates_deep_dived_v2"][0]["reported_reason_family"],
            )
            self.assertTrue(
                raw["candidates_deep_dived_v2"][0]["reason_family_normalized"])

    def test_long_early_lower_timeframe_rejection_enters_shadow_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = _candidate(
                1, "AAA-USDT-SWAP", rotation_due=False)
            candidate.update({
                "layer": "early",
                "candidate_id": "cand_" + "4" * 20,
                "prior_evidence_hash": None,
                "new_evidence_required": False,
                "opportunity_id": "opp_" + "5" * 20,
                "opportunity_state": "EARLY_WATCH",
                "regime_first_seen": "range",
                "regime_current": "range",
            })
            result_item = _result("AAA-USDT-SWAP", ready=True)
            contract = dict(result_item["evidence_contract"])
            contract.pop("evidence_hash", None)
            contract["timeframes"] = {"15m": {
                "observed_bar_ts": "2026-08-29T00:15:00Z",
                "values": {"c": 1.25},
            }}
            result_item["evidence_contract"] = gate.seal_evidence_contract(contract)
            _paths, bundle = self._install_bundle(root, [candidate], [result_item])
            contract_hash = bundle["items"][0]["evidence_hash"]
            with (
                mock.patch.object(
                    quality.thresholds, "candidate_exact_identity_enforced",
                    return_value=False),
                mock.patch.object(
                    quality.thresholds, "side_regime_soft_veto_shadow_active",
                    return_value=True),
            ):
                raw, signals, _result_value = quality.normalize_candidate_quality(
                    cycle_id=CYCLE,
                    raw={
                        "candidates_deep_dived_v2": [{
                            "candidate_id": candidate["candidate_id"],
                            "symbol": "AAA-USDT-SWAP", "side": "long",
                            "layer": "early", "evidence_hash": contract_hash,
                            "decision": "reject",
                            "supporting_evidence": ["4H direction"],
                            "opposing_evidence": ["15m against"],
                            "invalidation_condition": "4H direction lost",
                            "reason_code": "15m_not_aligned",
                            "reason_family": "LOWER_TIMEFRAME_AGAINST",
                            "reason": "15m lower timeframe not aligned",
                        }],
                        "candidate_coverage": {
                            "dynamic_limit": 1, "dynamic_target": 1,
                            "stop_reason": "target_reached",
                        },
                    },
                    signals=[], phase="shadow", evidence_root=root,
                )
            self.assertEqual([], signals)
            shadow = raw["side_regime_soft_veto_shadow"]
            self.assertEqual(1, len(shadow["counterfactuals"]))
            self.assertEqual(
                "continue_review_only",
                shadow["counterfactuals"][0]["counterfactual_disposition"])
            self.assertFalse(shadow["signals_mutated"])
            self.assertFalse(shadow["order_authority"])
            self.assertEqual(
                candidate["opportunity_id"],
                raw["candidates_deep_dived_v2"][0]["opportunity_id"])

            watch_raw = json.loads(json.dumps({
                "candidates_deep_dived_v2": [{
                    "candidate_id": candidate["candidate_id"],
                    "symbol": "AAA-USDT-SWAP", "side": "long",
                    "layer": "early", "evidence_hash": contract_hash,
                    "decision": "watch",
                    "supporting_evidence": ["4H direction"],
                    "opposing_evidence": ["15m against"],
                    "invalidation_condition": "4H direction lost",
                    "reason_code": "15m_not_aligned",
                    "reason_family": "LOWER_TIMEFRAME_AGAINST",
                    "reason": "15m lower timeframe not aligned",
                }],
                "candidate_coverage": {
                    "dynamic_limit": 1, "dynamic_target": 1,
                    "stop_reason": "target_reached",
                },
            }))
            with mock.patch.object(
                    quality.thresholds, "side_regime_soft_veto_shadow_active",
                    return_value=True):
                watch_result, _signals, _quality = quality.normalize_candidate_quality(
                    cycle_id=CYCLE, raw=watch_raw, signals=[], phase="shadow",
                    evidence_root=root,
                )
            self.assertEqual(
                [], watch_result["side_regime_soft_veto_shadow"]["counterfactuals"])
            self.assertEqual(
                1, watch_result["side_regime_soft_veto_shadow"]
                ["excluded_counts"]["non_rejection_decision"])
            self.assertEqual(
                {"LOWER_TIMEFRAME_AGAINST": 1},
                watch_result["candidate_coverage"]["watch_reason_family_counts"])

    def test_repeated_candidate_requires_deterministically_changed_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_item = _result("AAA-USDT-SWAP", ready=True)
            current_hash = result_item["evidence_contract"]["evidence_hash"]
            candidate = _candidate(
                1, "AAA-USDT-SWAP", rotation_due=False)
            candidate.update({
                "candidate_id": "cand_" + "b" * 20,
                "prior_evidence_hash": current_hash,
                "new_evidence_required": True,
                "recent_deep_dives_6h": 3,
                "recent_rejections_6h": 3,
            })
            self._install_bundle(root, [candidate], [result_item])
            _raw, _signals, quality_result = quality.normalize_candidate_quality(
                cycle_id=CYCLE,
                raw={
                    "candidates_deep_dived_v2": [{
                        "candidate_id": candidate["candidate_id"],
                        "symbol": "AAA-USDT-SWAP",
                        "side": "long",
                        "layer": "mature",
                        "evidence_hash": current_hash,
                        "decision": "reject",
                        "supporting_evidence": ["4H结构支持"],
                        "opposing_evidence": ["15m反向"],
                        "invalidation_condition": "1H收盘失效",
                        "reason_code": "repeat_reject_no_new_break",
                        "reason_family": "REPEAT_NO_NEW_EVIDENCE",
                        "reason": "证据未发生变化",
                    }],
                    "candidate_coverage": {
                        "dynamic_limit": 1,
                        "dynamic_target": 1,
                        "stop_reason": "target_reached",
                    },
                },
                signals=[],
                phase="shadow",
                evidence_root=root,
            )
            self.assertEqual("NOT_MET", quality_result["status"])
            self.assertIn(
                "new_evidence_hash_unchanged",
                quality_result["coverage"]["invalid_entries"][0]["errors"],
            )

    def test_runtime_off_does_not_start_batch_or_write_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(runtime.subprocess, "run") as run:
                status = runtime.prepare_candidate_bundle(
                    cycle_id=CYCLE,
                    db_root=root,
                    phase="off",
                    timeout_seconds=12,
                    evidence_root=root,
                )
            run.assert_not_called()
            self.assertEqual("OFF", status["status"])
            self.assertFalse(evidence.candidate_evidence_paths(
                CYCLE, root=root)["status"].exists())

    def test_runtime_passed_receipt_binds_counts_hashes_and_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = {"manifest_sha256": "a" * 64}
            bundle = {
                "candidate_count": 16,
                "screened_count": 16,
                "ready_count": 12,
                "not_ready_count": 4,
                "bundle_sha256": "b" * 64,
                "elapsed_seconds": 0.75,
            }
            process = mock.Mock(returncode=0, stdout='{"ok":true}', stderr="")
            with (
                mock.patch.object(
                    runtime, "load_candidate_manifest",
                    return_value=(manifest, "c" * 64)),
                mock.patch.object(
                    runtime, "load_candidate_evidence_bundle",
                    return_value=bundle),
                mock.patch.object(
                    runtime.subprocess, "run", return_value=process) as run,
            ):
                status = runtime.prepare_candidate_bundle(
                    cycle_id=CYCLE,
                    db_root=root,
                    phase="shadow",
                    timeout_seconds=12,
                    evidence_root=root,
                )
            self.assertEqual("PASSED", status["status"])
            self.assertTrue(status["fallback_required"])
            self.assertEqual(16, status["screened_count"])
            self.assertEqual("a" * 64, status["briefing_sha256"])
            self.assertEqual("b" * 64, status["bundle_sha256"])
            self.assertEqual(12, run.call_args.kwargs["timeout"])
            persisted = json.loads(evidence.candidate_evidence_paths(
                CYCLE, root=root)["status"].read_text(encoding="utf-8"))
            self.assertEqual("PASSED", persisted["status"])

    def test_runtime_timeout_degrades_to_legacy_without_throwing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timeout = runtime.subprocess.TimeoutExpired(
                cmd=["python"], timeout=12, output="partial", stderr="late")
            with (
                mock.patch.object(
                    runtime, "load_candidate_manifest",
                    return_value=({"manifest_sha256": "a" * 64}, "c" * 64)),
                mock.patch.object(runtime.subprocess, "run", side_effect=timeout),
            ):
                status = runtime.prepare_candidate_bundle(
                    cycle_id=CYCLE,
                    db_root=root,
                    phase="consume",
                    timeout_seconds=12,
                    evidence_root=root,
                )
            self.assertEqual("DEGRADED", status["status"])
            self.assertTrue(status["fallback_required"])
            self.assertFalse(status["decision_consumes_bundle"])
            self.assertTrue(status["manifest_valid"])
            self.assertEqual("a" * 64, status["briefing_sha256"])
            self.assertIn("TimeoutExpired", status["error"])


if __name__ == "__main__":
    unittest.main()
