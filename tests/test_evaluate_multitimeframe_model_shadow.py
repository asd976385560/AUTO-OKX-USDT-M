from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evaluate_multitimeframe_model_shadow as evaluator  # noqa: E402


UTC = timezone.utc


def _artifact(*, generated: str, forward: bool = True) -> dict:
    return {
        "artifact_type": "frozen_multitimeframe_model_shadow",
        "model_id": "fixture-model",
        "model_parameters_sha256": "a" * 64,
        "cycle_id": "2026-08-12T08:00",
        "generated_at_utc": generated,
        "forward_evidence_eligible": forward,
        "status": "ready_for_forward_shadow" if forward else "pre_freeze_reconstruction_not_forward_evidence",
        "offline_acceptance": {"gate_pass": False},
        "records": [
            {
                "symbol": "BTC-USDT-SWAP",
                "feature_decision_ts_utc": "2026-08-12T00:00:40Z",
                "signal_available_at_utc": generated,
                "side": "long",
                "horizon": "15m",
                "research_probability": 0.97,
                "selected_for_forward_evaluation": True,
                "ranking_diagnostics": {
                    "runner_up_probability": 0.65,
                    "top_vs_runner_up_margin": 0.32,
                    "opposite_side_same_horizon_probability": 0.30,
                    "selected_vs_opposite_margin": 0.67,
                    "selected_side_horizon_votes": 3,
                    "selected_side_unanimous": True,
                    "selected_side_mean_margin": 0.40,
                    "selected_side_min_margin": 0.20,
                },
                "future_retraining_features": {
                    "contract_statistics_available": True,
                    "contract_statistics_source_ts_utc": "2026-08-11T23:45:00Z",
                    "contract_statistics_available_at_utc": "2026-08-12T00:00:42Z",
                    "contract_oi_log_usd": 10.0,
                    "contract_oi_log_change_15m": 0.01,
                    "contract_taker_total_log_usd": 9.0,
                    "contract_taker_buy_centered": 0.10,
                    "contract_oi_taker_interaction": 0.001,
                },
            }
        ],
    }


class FrozenModelEvaluationTests(unittest.TestCase):
    def test_loader_excludes_pre_freeze_and_keeps_latest_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pre.json").write_text(
                json.dumps(_artifact(generated="2026-08-12T00:00:30Z", forward=False)),
                encoding="utf-8",
            )
            (root / "first.json").write_text(
                json.dumps(_artifact(generated="2026-08-12T00:00:45Z")),
                encoding="utf-8",
            )
            (root / "latest.json").write_text(
                json.dumps(_artifact(generated="2026-08-12T00:00:50Z")),
                encoding="utf-8",
            )

            loaded = evaluator._load_artifacts(root)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0][1]["generated_at_utc"], "2026-08-12T00:00:50Z")

    def test_entry_is_strictly_after_actual_artifact_availability(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shadow = root / "shadow"
            shadow.mkdir()
            (shadow / "cycle.json").write_text(
                json.dumps(_artifact(generated="2026-08-12T00:00:45Z")),
                encoding="utf-8",
            )
            market = root / "market.db"
            con = sqlite3.connect(market)
            con.execute(
                "CREATE TABLE tick_snapshots("
                "ts TEXT,symbol TEXT,last REAL,bid REAL,ask REAL)"
            )
            con.executemany(
                "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
                [
                    ("2026-08-12T00:00:30Z", "BTC-USDT-SWAP", 99.0, 98.9, 99.1),
                    ("2026-08-12T00:15:00Z", "BTC-USDT-SWAP", 100.0, 99.9, 100.1),
                    ("2026-08-12T00:30:00Z", "BTC-USDT-SWAP", 101.0, 100.9, 101.1),
                ],
            )
            con.commit()
            con.close()

            payload, labels = evaluator.evaluate(
                shadow_root=shadow,
                market_db=market,
                as_of_utc=datetime(2026, 8, 12, 0, 31, tzinfo=UTC),
                min_sample=1,
                min_days=1,
                min_cycles=1,
            )

        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["entry_tick_ts_utc"], "2026-08-12T00:15:00Z")
        self.assertEqual(labels[0]["outcome_tick_ts_utc"], "2026-08-12T00:30:00Z")
        self.assertEqual(labels[0]["entry_price_source"], "ask")
        self.assertEqual(labels[0]["outcome_price_source"], "bid")
        self.assertEqual(labels[0]["entry_executable"], 100.1)
        self.assertEqual(labels[0]["outcome_executable"], 100.9)
        self.assertAlmostEqual(
            labels[0]["signed_return_after_cost"], 100.9 / 100.1 - 1.002,
        )
        self.assertTrue(labels[0]["after_cost_hit"])
        self.assertEqual(labels[0]["selected_side_horizon_votes"], 3)
        self.assertAlmostEqual(labels[0]["selected_vs_opposite_margin"], 0.67)
        self.assertTrue(labels[0]["contract_statistics_available"])
        model = payload["models"][0]["overall"]
        self.assertEqual(model["status"], "NOT_MET")
        self.assertTrue(model["requirements"]["precision_at_least_90pct"])
        self.assertFalse(
            model["requirements"]["wilson_95_low_at_least_90pct"])
        diagnostics = payload["models"][0]["diagnostics"]
        self.assertEqual(diagnostics["ranking_diagnostic_rows"], 1)
        self.assertEqual(diagnostics["contract_feature_flag_rows"], 1)
        self.assertFalse(model["production_threshold_change_allowed"])
        self.assertFalse(payload["production_execution_authorized"])
        self.assertEqual(payload["orders_placed"], 0)
        self.assertEqual(payload["schema_version"], 2)
        self.assertTrue(payload["last_price_fields_are_diagnostic_only"])
        self.assertEqual(payload["missing_executable_price_records"], 0)

    def test_missing_bid_ask_never_falls_back_to_last(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shadow = root / "shadow"
            shadow.mkdir()
            (shadow / "cycle.json").write_text(
                json.dumps(_artifact(generated="2026-08-12T00:00:45Z")),
                encoding="utf-8",
            )
            market = root / "market.db"
            con = sqlite3.connect(market)
            con.execute(
                "CREATE TABLE tick_snapshots("
                "ts TEXT,symbol TEXT,last REAL,bid REAL,ask REAL)"
            )
            con.executemany(
                "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
                [
                    ("2026-08-12T00:15:00Z", "BTC-USDT-SWAP", 100.0, None, None),
                    ("2026-08-12T00:30:00Z", "BTC-USDT-SWAP", 101.0, 100.9, 101.1),
                ],
            )
            con.commit()
            con.close()

            payload, labels = evaluator.evaluate(
                shadow_root=shadow,
                market_db=market,
                as_of_utc=datetime(2026, 8, 12, 0, 31, tzinfo=UTC),
            )

        self.assertEqual(labels, [])
        self.assertEqual(payload["missing_executable_price_records"], 1)
        self.assertEqual(payload["models"][0]["overall"]["n_labeled"], 0)

    def test_outcome_query_never_reads_beyond_as_of(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shadow = root / "shadow"
            shadow.mkdir()
            (shadow / "cycle.json").write_text(
                json.dumps(_artifact(generated="2026-08-12T00:00:45Z")),
                encoding="utf-8",
            )
            market = root / "market.db"
            con = sqlite3.connect(market)
            con.execute(
                "CREATE TABLE tick_snapshots("
                "ts TEXT,symbol TEXT,last REAL,bid REAL,ask REAL)"
            )
            con.executemany(
                "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
                [
                    ("2026-08-12T00:15:00Z", "BTC-USDT-SWAP", 100.0, 99.9, 100.1),
                    ("2026-08-12T00:30:02Z", "BTC-USDT-SWAP", 101.0, 100.9, 101.1),
                ],
            )
            con.commit()
            con.close()

            early, early_labels = evaluator.evaluate(
                shadow_root=shadow,
                market_db=market,
                as_of_utc=datetime(2026, 8, 12, 0, 30, tzinfo=UTC),
            )
            mature, mature_labels = evaluator.evaluate(
                shadow_root=shadow,
                market_db=market,
                as_of_utc=datetime(2026, 8, 12, 0, 31, tzinfo=UTC),
            )

        self.assertEqual(early_labels, [])
        self.assertEqual(early["models"][0]["overall"]["n_labeled"], 0)
        self.assertEqual(len(mature_labels), 1)
        self.assertEqual(
            mature_labels[0]["outcome_tick_ts_utc"],
            "2026-08-12T00:30:02Z",
        )

    def test_default_sample_and_time_gates_remain_not_measurable(self) -> None:
        rows = [{
            "cycle_id": "2026-08-12T08:00",
            "side": "long",
            "horizon": "15m",
            "research_probability": 0.99,
            "after_cost_hit": True,
            "signed_return": 0.01,
            "executable_directional_return": 0.009,
            "signed_return_after_cost": 0.007,
        }]
        result = evaluator._metrics(
            rows,
            offline_gate_pass=False,
            min_sample=100,
            min_days=5,
            min_cycles=100,
        )
        self.assertEqual(result["status"], "NOT_MEASURABLE")
        self.assertFalse(result["requirements"]["minimum_sample_met"])
        self.assertFalse(result["requirements"]["minimum_days_met"])
        self.assertFalse(result["requirements"]["minimum_cycles_met"])


if __name__ == "__main__":
    unittest.main()
