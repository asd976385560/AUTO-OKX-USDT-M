from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diagnose_multitimeframe_policy as diagnostic  # noqa: E402


def _frame(count: int = 200) -> pd.DataFrame:
    successes = int(count * 0.90)
    labels = np.r_[np.ones(successes), np.zeros(count - successes)]
    return pd.DataFrame({
        "obs_id": np.arange(count),
        "obs_ts": pd.date_range("2026-07-01", periods=count, freq="1h", tz="UTC"),
        "symbol": [f"S{i % 20}" for i in range(count)],
        "horizon": ["4H"] * count,
        "side": ["long"] * count,
        "probability": np.linspace(0.99, 0.50, count),
        "success": labels,
        "signed_return_after_cost": np.where(labels > 0, 0.01, -0.01),
        "rule_direction": ["long"] * count,
        "aligned_timeframes": [3] * count,
        "taker_buy_centered": [0.2] * count,
        "cvd_share": [0.1] * count,
    })


class MultitimeframePolicyDiagnosticTests(unittest.TestCase):
    def test_structural_filters_are_point_in_time_fields(self) -> None:
        frame = _frame(10)
        frame.loc[0, "side"] = "short"
        frame.loc[1, "aligned_timeframes"] = 2
        frame.loc[2, "cvd_share"] = -0.1
        self.assertEqual(int(diagnostic._rule_agreement(frame).sum()), 9)
        self.assertEqual(int(diagnostic._three_aligned(frame).sum()), 9)
        self.assertEqual(int(diagnostic._flow_agreement(frame).sum()), 8)

    def test_policy_choice_is_derived_without_holdout_input(self) -> None:
        candidates = diagnostic.calibration_candidates(_frame(), minimum_n=100)
        chosen = diagnostic.choose_policy(candidates)
        self.assertGreaterEqual(chosen["precision"], 0.90)
        self.assertGreaterEqual(chosen["n"], 100)
        self.assertEqual(
            "target_reached_on_calibration", chosen["selection_status"])

    def test_oracle_requires_all_six_candidates(self) -> None:
        best = pd.DataFrame({"obs_id": [1, 2], "success": [1.0, 0.0]})
        rows = []
        for obs_id, count in ((1, 6), (2, 5)):
            for index in range(count):
                rows.append({"obs_id": obs_id, "success": float(index == 0)})
        result = diagnostic._oracle(pd.DataFrame(rows), best)
        self.assertEqual(1, result["complete_observations"])
        self.assertEqual(1.0, result["oracle_any_candidate_success_rate"])

    def test_probability_deciles_are_strict_json_serializable(self) -> None:
        result = diagnostic._deciles(_frame())
        self.assertEqual(10, len(result))
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
