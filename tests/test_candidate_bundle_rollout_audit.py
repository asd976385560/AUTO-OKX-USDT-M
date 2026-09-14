# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from scripts import _acceptance_thresholds as thresholds
from scripts import audit_candidate_bundle_rollout as audit


def _row(index: int, *, phase: str) -> dict:
    minute = ("00", "15", "30", "45")[index % 4]
    elapsed = {"00": 500, "15": 430, "30": 500, "45": 470}[minute]
    return {
        "cycle_id": f"2026-08-29T{index // 4:02d}:{minute}",
        "slot_minute": minute,
        "phase": phase,
        "runtime_status": "PASSED",
        "bundle_valid": True,
        "pool_count": 16,
        "screened_count": 16,
        "identity_cycle_order_hash_consistent": True,
        "bundle_elapsed_seconds": 1.0,
        "bundle_wall_elapsed_seconds": 1.5,
        "single_bundle_parity_numerator": 5,
        "single_bundle_parity_denominator": 5,
        "reported_deep_dives": 6,
        "quality_valid_deep_dives": 6,
        "dynamic_target_utilization": 1.0,
        "rotation_required": True,
        "rotation_satisfied": True,
        "strict_cycle_pass": True,
        "business_terminal_elapsed_seconds": elapsed,
        "new_candidate_bundle_failure_type": False,
        "manifest_side_neutral": True,
        "bundle_side_neutral": True,
        "review_slice_consistent": True,
        "retired_judgment_payload_absent": True,
    }


class CandidateBundleRolloutAuditTests(unittest.TestCase):
    def test_unregistered_boundary_does_not_rejudge_old_cycles(self):
        with mock.patch.object(
            thresholds, "CANDIDATE_BUNDLE_SHADOW_ACTIVATION_CST", None,
        ):
            result = audit.audit_candidate_bundle_rollout(
                as_of=datetime(2026, 8, 29, 9, 0, tzinfo=thresholds.CST))
        self.assertEqual("UNREGISTERED", result["status"])
        self.assertEqual([], result["cycles"])
        self.assertFalse(result["registration"]["historical_rejudgement"])

    def test_shadow_requires_all_24_slots_and_nonzero_contract_parity(self):
        rows = [_row(index, phase="shadow") for index in range(24)]
        summary = audit._shadow_summary(rows)
        self.assertEqual("READY_FOR_CONSUME", summary["status"])
        self.assertEqual(384, summary["screening"]["denominator"])
        self.assertEqual(1.0, summary["contract_parity"]["rate"])
        broken = [dict(row) for row in rows]
        broken[0]["single_bundle_parity_numerator"] = 4
        self.assertEqual("NOT_MET", audit._shadow_summary(broken)["status"])
        zero_parity = [dict(row) for row in rows]
        for row in zero_parity:
            row["single_bundle_parity_numerator"] = 0
            row["single_bundle_parity_denominator"] = 0
        self.assertFalse(
            audit._shadow_summary(zero_parity)["gates"]["contract_parity"])

    def test_consume_96_slots_pass_all_registered_guards(self):
        rows = [_row(index, phase="consume") for index in range(96)]
        summary = audit._consume_summary(rows)
        self.assertEqual("MET", summary["status"])
        self.assertFalse(summary["rollback_required"])
        self.assertEqual(1.0, summary["structure_valid_deep_dives"]["rate"])
        self.assertEqual(1.0, summary["strict_cycle_success"]["rate"])
        self.assertTrue(all(
            item["passed"]
            for item in summary["slot_business_terminal_p90"].values()))

    def test_new_candidate_bundle_failure_type_requires_rollback(self):
        rows = [_row(index, phase="consume") for index in range(10)]
        rows[3]["new_candidate_bundle_failure_type"] = True
        summary = audit._consume_summary(rows)
        self.assertEqual("ROLLBACK_REQUIRED", summary["status"])
        self.assertTrue(summary["rollback_required"])
        self.assertEqual(1, summary["new_candidate_bundle_failure_type_count"])

    def test_minimal_epoch_uses_three_and_twelve_slots_not_legacy_gates(self):
        basic = [_row(index, phase="minimal") for index in range(3)]
        result = audit._minimal_summary(basic)
        self.assertEqual("BASIC_CONFIRMED", result["status"])
        self.assertEqual(3, result["basic_confirmation_slots"])
        self.assertEqual(12, result["full_confirmation_slots"])
        self.assertFalse(result["legacy_mtf_state_card_gates_applied"])
        self.assertFalse(result["rollback_required"])

        full = [_row(index, phase="minimal") for index in range(12)]
        self.assertEqual("FULL_CONFIRMED", audit._minimal_summary(full)["status"])

    def test_minimal_epoch_fails_retired_payload_without_requiring_rollback(self):
        rows = [_row(index, phase="minimal") for index in range(3)]
        rows[1]["retired_judgment_payload_absent"] = False
        result = audit._minimal_summary(rows)
        self.assertEqual("NOT_MET", result["status"])
        self.assertFalse(result["rollback_required"])


if __name__ == "__main__":
    unittest.main()
