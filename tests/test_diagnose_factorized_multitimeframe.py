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

import diagnose_factorized_multitimeframe as factorized  # noqa: E402


class FactorizedMultitimeframeDiagnosticTests(unittest.TestCase):
    def test_factor_targets_separate_move_and_conditional_direction(self) -> None:
        panel = pd.DataFrame({
            "15m_long_success": [1.0, 0.0, 0.0],
            "15m_short_success": [0.0, 1.0, 0.0],
            "1H_long_success": [0.0, 0.0, 1.0],
            "1H_short_success": [0.0, 0.0, 0.0],
            "4H_long_success": [0.0, 1.0, 0.0],
            "4H_short_success": [1.0, 0.0, 0.0],
        })

        move, conditional_long, labels = factorized._factor_targets(panel)

        np.testing.assert_array_equal(move[:, 0], [1.0, 1.0, 0.0])
        np.testing.assert_array_equal(
            conditional_long[:, 0], [1.0, 0.0, 0.0])
        np.testing.assert_array_equal(labels[0], [1, 0, 0, 0, 0, 1])

    def test_candidate_probabilities_are_coherent_with_move_probability(self) -> None:
        raw_move = np.full((4, 3), 0.6)
        raw_direction = np.array([
            [0.8, 0.3, 0.5],
            [0.2, 0.7, 0.4],
            [0.9, 0.1, 0.6],
            [0.4, 0.6, 0.2],
        ])
        identity = [(0.0, 1.0)] * 3

        candidates, move, _direction = factorized._candidate_probabilities(
            raw_move, raw_direction,
            {"move": identity, "direction": identity},
        )

        for horizon_index in range(3):
            np.testing.assert_allclose(
                candidates[:, 2 * horizon_index]
                + candidates[:, 2 * horizon_index + 1],
                move[:, horizon_index],
            )
        self.assertTrue((candidates >= 0).all())
        self.assertTrue((candidates <= 1).all())

    def test_direction_margin_confidence_rewards_clear_side_separation(self) -> None:
        candidates = np.array([
            [0.80, 0.05, 0.10, 0.09, 0.08, 0.07],
            [0.45, 0.44, 0.10, 0.09, 0.08, 0.07],
        ])
        chosen = np.argmax(candidates, axis=1)

        confidence = factorized._raw_selection_confidence(
            candidates, chosen, "direction_margin")

        self.assertGreater(float(confidence[0]), float(confidence[1]))

    def test_relative_features_are_point_in_time_and_finite_when_atr_is_zero(self) -> None:
        row: dict[str, list[float]] = {
            "taker_buy_centered": [0.2],
            "cvd_share": [-0.1],
        }
        for timeframe in factorized.TIMEFRAMES:
            row[f"{timeframe}_atr_pct"] = [0.0]
            row[f"{timeframe}_close_vs_ma20"] = [0.01]
            row[f"{timeframe}_ma5_vs_ma20"] = [0.02]
            row[f"{timeframe}_bar_return"] = [0.03]
            row[f"{timeframe}_macd_over_atr"] = [0.2]
            row[f"{timeframe}_rsi_norm"] = [0.1]

        transformed, names = factorized._add_declared_relative_features(
            pd.DataFrame(row))

        self.assertIn("declared_trend_mean", names)
        self.assertIn("declared_flow_consensus", names)
        self.assertFalse(np.isinf(
            transformed[names].to_numpy(dtype=float)).any())

    def test_label_price_mode_requires_complete_side_specific_contract(self) -> None:
        legacy = pd.DataFrame({"15m_return": [0.1]})
        executable = pd.DataFrame({
            f"{timeframe}_{side}_return": [0.1]
            for timeframe in factorized.TIMEFRAMES
            for side in factorized.SIDES
        })
        partial = pd.DataFrame({"15m_long_return": [0.1]})

        self.assertEqual(factorized._label_price_mode(legacy), "last")
        self.assertEqual(
            factorized._label_price_mode(executable), "executable")
        with self.assertRaisesRegex(ValueError, "partial"):
            factorized._label_price_mode(partial)


if __name__ == "__main__":
    unittest.main()
