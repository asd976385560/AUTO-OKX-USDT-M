from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import benchmark_model_shadow_evidence_scale as benchmark  # noqa: E402


class ModelShadowScaleBenchmarkTests(unittest.TestCase):
    def test_small_fixture_preserves_label_volume_and_safety(self) -> None:
        result = benchmark.benchmark(
            cycles=3,
            signals_per_cycle=6,
            evaluator_budget_seconds=30,
            auditor_budget_seconds=30,
        )
        self.assertEqual(result["labels_written"], 18)
        self.assertEqual(result["quality_status"], "PASSED")
        self.assertTrue(result["checks"]["label_volume_exact"])
        self.assertTrue(result["checks"]["label_keys_unique"])
        self.assertTrue(result["checks"]["field_reconstruction_exact"])
        self.assertTrue(result["checks"]["safety_flags_closed"])
        self.assertFalse(result["production_execution_authorized"])
        self.assertEqual(result["orders_placed"], 0)
        # Full benchmark requires 100 cycles; this unit fixture intentionally
        # fails only that volume gate while exercising the real job pair.
        self.assertFalse(result["checks"]["cycle_target_met"])
        self.assertEqual(result["status"], "NOT_MET")


if __name__ == "__main__":
    unittest.main()
