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

import diagnose_multitimeframe_ranking as ranking  # noqa: E402


class MultitimeframeRankingDiagnosticTests(unittest.TestCase):
    def test_label_price_mode_requires_complete_side_specific_contract(self) -> None:
        legacy = pd.DataFrame({"15m_return": [0.1]})
        executable = pd.DataFrame({
            f"{timeframe}_{side}_return": [0.1]
            for timeframe, side in ranking.CANDIDATE_KEYS
        })
        partial = pd.DataFrame({"15m_long_return": [0.1]})

        self.assertEqual(ranking._label_price_mode(legacy), "last")
        self.assertEqual(ranking._label_price_mode(executable), "executable")
        with self.assertRaisesRegex(ValueError, "partial"):
            ranking._label_price_mode(partial)

    def test_softmax_rows_sum_to_one(self) -> None:
        scores = np.array([
            [1000.0, 999.0, -1000.0, 0.0, 1.0, 2.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        ])

        probability = ranking._softmax(scores)

        np.testing.assert_allclose(probability.sum(axis=1), 1.0)
        self.assertTrue(np.isfinite(probability).all())

    def test_uniform_positive_target_shares_mass_across_successes(self) -> None:
        labels = np.array([
            [1.0, 0.0, 1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        ])
        utilities = np.zeros_like(labels)

        targets, active = ranking._listwise_targets(
            labels, utilities, "uniform_positive")

        np.testing.assert_allclose(
            targets[0], [0.5, 0.0, 0.5, 0.0, 0.0, 0.0])
        np.testing.assert_array_equal(active, [True, False])

    def test_best_positive_utility_never_selects_failed_candidate(self) -> None:
        labels = np.array([[1.0, 0.0, 1.0, 0.0, 0.0, 0.0]])
        utilities = np.array([[0.01, 5.0, 0.03, 4.0, 3.0, 2.0]])

        targets, active = ranking._listwise_targets(
            labels, utilities, "best_positive_utility")

        self.assertTrue(bool(active[0]))
        self.assertEqual(int(np.argmax(targets[0])), 2)
        self.assertEqual(float(targets[0].sum()), 1.0)

    def test_listwise_model_learns_feature_dependent_candidate(self) -> None:
        count = 400
        signal = np.tile(np.array([-1.0, 1.0]), count // 2)
        x = np.column_stack([np.ones(count), signal])
        targets = np.zeros((count, 6), dtype=float)
        targets[signal < 0, 0] = 1.0
        targets[signal > 0, 1] = 1.0
        active = np.ones(count, dtype=bool)

        weights = ranking._fit_listwise_softmax(
            x, targets, active, regularization=0.001,
            iterations=240, learning_rate=0.04)
        chosen = np.argmax(x @ weights, axis=1)

        self.assertGreater(float((chosen[signal < 0] == 0).mean()), 0.99)
        self.assertGreater(float((chosen[signal > 0] == 1).mean()), 0.99)

    def test_return_candidate_scores_are_directionally_symmetric(self) -> None:
        x = np.array([[1.0]])
        weights = np.array([[0.01, -0.02, 0.03]])
        scale = np.ones(3)

        scores = ranking._return_candidate_scores(x, weights, scale)

        for index in range(0, 6, 2):
            self.assertAlmostEqual(
                float(scores[0, index] + scores[0, index + 1]),
                -2.0 * ranking.baseline.COST_HURDLE,
            )

    def test_six_candidate_return_scores_preserve_executable_asymmetry(self) -> None:
        x = np.array([[1.0]])
        weights = np.array([[0.01, -0.03, 0.02, -0.04, 0.05, -0.06]])
        scale = np.ones(6)

        scores = ranking._return_candidate_scores(x, weights, scale)

        np.testing.assert_allclose(scores, weights)

    def test_residual_scale_details_preserve_all_six_candidates(self) -> None:
        details = ranking._residual_scale_details(
            np.arange(1.0, 7.0, dtype=float))

        self.assertEqual(len(details), 6)
        self.assertEqual(details["residual_scale_15m_long"], 1.0)
        self.assertEqual(details["residual_scale_4H_short"], 6.0)


if __name__ == "__main__":
    unittest.main()
