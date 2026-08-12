from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diagnose_selective_multitimeframe as diagnostic  # noqa: E402
import offline_multitimeframe_calibration as calibration  # noqa: E402


class SelectiveMultitimeframeDiagnosticTests(unittest.TestCase):
    def test_feature_columns_exclude_metadata_and_all_outcomes(self) -> None:
        panel = pd.DataFrame({
            **{name: [0] for name in diagnostic.META_COLUMNS},
            **{name: [0.0] for name in diagnostic._outcome_columns()},
            "safe_feature": [1.0],
        })

        self.assertEqual(diagnostic._feature_columns(panel), ["safe_feature"])

    def test_candidate_return_prefers_side_specific_executable_label(self) -> None:
        panel = pd.DataFrame({
            "15m_return": [0.90],
            "15m_long_return": [0.01],
            "15m_short_return": [-0.03],
        })

        long_return, side_specific = diagnostic._candidate_forward_return(
            panel, "15m", "long")
        short_return, _ = diagnostic._candidate_forward_return(
            panel, "15m", "short")

        self.assertTrue(side_specific)
        self.assertAlmostEqual(float(long_return[0]), 0.01)
        self.assertAlmostEqual(float(short_return[0]), -0.03)

    def test_old_last_price_panel_does_not_require_optional_side_returns(self) -> None:
        panel = pd.DataFrame({
            **{name: [0] for name in diagnostic.META_COLUMNS},
            **{
                name: [0.0]
                for name in diagnostic._required_outcome_columns()
            },
            "safe_feature": [1.0],
        })

        self.assertEqual(diagnostic._feature_columns(panel), ["safe_feature"])

    def test_nested_calibration_windows_are_disjoint_and_four_hour_purged(
        self,
    ) -> None:
        times = pd.date_range(
            "2026-08-01T00:00:00Z", periods=24, freq="h")
        panel = pd.DataFrame({"obs_ts": times, "split": "calibration"})

        tuning, threshold, confirmation, contract = (
            diagnostic._calibration_subsplits(panel))

        self.assertFalse(bool((tuning & threshold).any()))
        self.assertFalse(bool((threshold & confirmation).any()))
        self.assertFalse(bool((tuning & confirmation).any()))
        self.assertGreaterEqual(
            panel.loc[threshold, "obs_ts"].min()
            - panel.loc[tuning, "obs_ts"].max(),
            pd.Timedelta(hours=4),
        )
        self.assertGreaterEqual(
            panel.loc[confirmation, "obs_ts"].min()
            - panel.loc[threshold, "obs_ts"].max(),
            pd.Timedelta(hours=4),
        )
        self.assertEqual(contract["purge_hours"], 4)
        self.assertGreater(contract["discarded_purge_rows"], 0)

    def test_histogram_gbdt_learns_simple_threshold_deterministically(self) -> None:
        values = np.tile(np.arange(8, dtype=np.uint8), 100)
        bins = np.column_stack([values, np.zeros(len(values), dtype=np.uint8)])
        outcome = (values >= 4).astype(float)

        first = diagnostic._fit_gbdt(
            bins, outcome, depth=1, rounds=30,
            learning_rate=0.10, min_leaf=20, l2=1.0,
        )
        second = diagnostic._fit_gbdt(
            bins, outcome, depth=1, rounds=30,
            learning_rate=0.10, min_leaf=20, l2=1.0,
        )
        first_probability = calibration._sigmoid(first.raw_score(bins))
        second_probability = calibration._sigmoid(second.raw_score(bins))

        np.testing.assert_allclose(first_probability, second_probability)
        self.assertGreater(
            float(first_probability[values >= 4].mean()),
            float(first_probability[values < 4].mean()) + 0.70,
        )

    def test_threshold_choice_requires_minimum_sample_and_finds_target(self) -> None:
        count = 200
        success = np.zeros(count, dtype=float)
        success[:90] = 1.0
        best = pd.DataFrame({
            "obs_id": np.arange(count),
            "obs_ts": pd.date_range(
                "2026-08-01T00:00:00Z", periods=count, freq="h"),
            "probability": np.linspace(0.999, 0.001, count),
            "success": success,
        })

        chosen, curve = diagnostic._choose_threshold(best, minimum_n=100)

        self.assertEqual(
            chosen["selection_status"],
            "target_reached_on_threshold_window",
        )
        self.assertEqual(chosen["n"], 100)
        self.assertAlmostEqual(chosen["precision"], 0.90)
        self.assertEqual(int(curve["n"].min()), 100)


if __name__ == "__main__":
    unittest.main()
