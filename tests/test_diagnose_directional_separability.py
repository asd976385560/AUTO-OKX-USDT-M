from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diagnose_directional_separability as diagnostic  # noqa: E402


def _candidate_frame() -> pd.DataFrame:
    probabilities = {
        "15m_long": 0.90,
        "15m_short": 0.20,
        "1H_long": 0.80,
        "1H_short": 0.30,
        "4H_long": 0.70,
        "4H_short": 0.40,
    }
    rows = []
    for key, probability in probabilities.items():
        horizon, side = key.split("_", 1)
        rows.append({
            "obs_id": "BTC@fixture",
            "obs_ts": pd.Timestamp("2026-08-12T00:00:02Z"),
            "decision_ts": pd.Timestamp("2026-08-12T00:00:40Z"),
            "symbol": "BTC-USDT-SWAP",
            "horizon": horizon,
            "side": side,
            "candidate_key": key,
            "probability": probability,
            "success": int(key == "15m_long"),
            "signed_return_after_cost": 0.01 if key == "15m_long" else -0.01,
            "rule_direction": "long",
            "aligned_timeframes": 3,
            "taker_buy_centered": 0.2,
            "cvd_share": 0.3,
            "asset_class": "crypto",
        })
    return pd.DataFrame(rows)


class DirectionalSeparabilityTests(unittest.TestCase):
    def test_aggregate_separates_direction_margin_from_oracle(self) -> None:
        row = diagnostic._aggregate(_candidate_frame()).iloc[0]

        self.assertEqual(row["horizon"], "15m")
        self.assertEqual(row["side"], "long")
        self.assertAlmostEqual(row["top_vs_runner_up_margin"], 0.10)
        self.assertAlmostEqual(row["selected_vs_opposite_margin"], 0.70)
        self.assertEqual(row["selected_side_horizon_votes"], 3)
        self.assertTrue(row["selected_side_unanimous"])
        self.assertTrue(row["rule_agreement"])
        self.assertTrue(row["flow_agreement"])
        self.assertEqual(row["any_candidate_success"], 1)

    def test_cycle_local_coverage_does_not_select_globally(self) -> None:
        frame = pd.DataFrame({
            "cycle_id_utc": ["A"] * 4 + ["B"] * 2,
            "symbol": ["A1", "A2", "A3", "A4", "B1", "B2"],
            "top_probability": [0.1, 0.2, 0.3, 0.4, 0.8, 0.9],
        })
        policy = diagnostic.POLICIES[0]

        selected = diagnostic._select_cycle_local(frame, policy, 0.50)

        self.assertEqual(len(selected), 3)
        self.assertEqual(set(selected["symbol"]), {"A3", "A4", "B2"})

    def test_candidate_below_minimum_n_cannot_win_selection(self) -> None:
        rows = [
            {
                "policy": "probability_all",
                "n": 99,
                "precision": 1.0,
                "wilson_95_low": 0.96,
                "requested_cycle_local_coverage": 0.10,
            },
            {
                "policy": "global_margin_all",
                "n": 100,
                "precision": 0.60,
                "wilson_95_low": 0.50,
                "requested_cycle_local_coverage": 0.20,
            },
        ]

        chosen = diagnostic._choose(rows, minimum_n=100)

        self.assertEqual(chosen["policy"], "global_margin_all")


if __name__ == "__main__":
    unittest.main()
