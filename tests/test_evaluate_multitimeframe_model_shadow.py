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
                "asset_class": "major",
                "feature_decision_ts_utc": "2026-08-12T00:00:40Z",
                "signal_available_at_utc": generated,
                "side": "long",
                "horizon": "15m",
                "research_probability": 0.97,
                "selected_model": "15m_long",
                "all_research_probabilities": {
                    "15m_long": 0.97,
                    "15m_short": 0.30,
                    "1H_long": 0.65,
                    "1H_short": 0.35,
                    "4H_long": 0.60,
                    "4H_short": 0.40,
                },
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
    def test_recursive_root_keeps_two_frozen_models_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shadow = root / "shadow"
            (shadow / "base" / "2026-08-12").mkdir(parents=True)
            (shadow / "second-model" / "cycles").mkdir(parents=True)
            first = _artifact(generated="2026-08-12T00:00:45Z")
            second = _artifact(generated="2026-08-12T00:00:50Z")
            second["model_id"] = "fixture-second-model"
            second["model_parameters_sha256"] = "b" * 64
            (shadow / "base" / "2026-08-12" / "cycle.json").write_text(
                json.dumps(first), encoding="utf-8")
            (shadow / "second-model" / "cycles" / "cycle.json").write_text(
                json.dumps(second), encoding="utf-8")
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

        self.assertEqual(payload["artifacts_loaded"], 2)
        self.assertEqual(len(labels), 2)
        self.assertEqual(len(payload["models"]), 2)
        self.assertEqual(
            {row["model_id"] for row in labels},
            {"fixture-model", "fixture-second-model"},
        )

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
        self.assertEqual(labels[0]["selected_probability_rank"], 1)
        self.assertEqual(labels[0]["selected_margin_rank"], 1)
        self.assertEqual(labels[0]["selected_cross_section_size"], 1)
        self.assertEqual(labels[0]["selected_side_horizon_votes"], 3)
        self.assertAlmostEqual(labels[0]["selected_vs_opposite_margin"], 0.67)
        self.assertTrue(labels[0]["contract_statistics_available"])
        self.assertEqual(labels[0]["selected_model"], "15m_long")
        self.assertEqual(labels[0]["candidate_probability_count"], 6)
        self.assertTrue(
            labels[0]["selected_is_highest_candidate_probability"])
        self.assertAlmostEqual(
            labels[0]["candidate_probability_4H_short"], 0.40)
        self.assertEqual(labels[0]["asset_class"], "major")
        model = payload["models"][0]["overall"]
        self.assertEqual(model["status"], "NOT_MEASURABLE")
        self.assertFalse(model["requirements"]["minimum_long_labels_met"])
        self.assertFalse(model["requirements"]["minimum_short_labels_met"])
        self.assertTrue(model["requirements"]["precision_at_least_target"])
        self.assertFalse(
            model["requirements"]["wilson_95_low_at_least_target"])
        by_day = payload["models"][0]["by_day"]
        self.assertEqual(len(by_day), 1)
        self.assertEqual(by_day[0]["day_cst"], "2026-08-12")
        self.assertTrue(by_day[0]["diagnostic_only"])
        self.assertEqual(by_day[0]["n_labeled"], 1)
        diagnostics = payload["models"][0]["diagnostics"]
        self.assertEqual(diagnostics["ranking_diagnostic_rows"], 1)
        self.assertEqual(diagnostics["contract_feature_flag_rows"], 1)
        self.assertEqual(diagnostics["candidate_probability_vector_rows"], 1)
        self.assertEqual(
            diagnostics["highest_candidate_selection_pass_rate"], 1.0)
        self.assertEqual(
            diagnostics["by_selected_model"][0]["n_labeled"], 1)
        self.assertEqual(
            diagnostics["selection_concentration"]["all_selected"][
                "distinct_symbols"], 1)
        self.assertEqual(
            diagnostics["by_asset_class"][0]["asset_class"], "major")
        by_side = {row["side"]: row for row in diagnostics["by_side"]}
        self.assertEqual(by_side["long"]["n_labeled"], 1)
        self.assertEqual(by_side["short"]["n_labeled"], 0)
        by_horizon_side = {
            (row["horizon"], row["side"]): row
            for row in diagnostics["by_horizon_side"]
        }
        self.assertEqual(by_horizon_side[("15m", "long")]["n_labeled"], 1)
        self.assertEqual(by_horizon_side[("4H", "short")]["n_labeled"], 0)
        probability_top = {
            row["top_k"]: row
            for row in diagnostics["by_selected_probability_top_k"]
        }
        margin_top = {
            row["top_k"]: row
            for row in diagnostics["by_selected_margin_top_k"]
        }
        self.assertEqual(probability_top[1]["n_labeled"], 1)
        self.assertEqual(margin_top[1]["n_labeled"], 1)
        self.assertEqual(
            probability_top[1]["rank_field"],
            "selected_probability_rank",
        )
        self.assertFalse(model["production_threshold_change_allowed"])
        self.assertFalse(payload["production_execution_authorized"])
        self.assertEqual(payload["orders_placed"], 0)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["label_schema_version"], 3)
        self.assertTrue(payload["last_price_fields_are_diagnostic_only"])
        self.assertIn(
            "preregistered diagnostic-only",
            payload["feature_diagnostic_contract"]["temporal_slice"],
        )
        self.assertEqual(payload["missing_executable_price_records"], 0)

    def test_legacy_shadow_without_optional_asset_class_stays_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shadow = root / "shadow"
            shadow.mkdir()
            artifact = _artifact(generated="2026-08-12T00:00:45Z")
            record = artifact["records"][0]
            record.pop("asset_class")
            (shadow / "cycle.json").write_text(
                json.dumps(artifact), encoding="utf-8")
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

        self.assertIsNone(labels[0]["asset_class"])
        diagnostics = payload["models"][0]["diagnostics"]
        self.assertEqual(diagnostics["by_asset_class"], [])

    def test_candidate_vector_exposes_non_highest_selection(self) -> None:
        record = _artifact(generated="2026-08-12T00:00:45Z")["records"][0]
        record["all_research_probabilities"]["1H_long"] = 0.99
        probabilities = evaluator._candidate_probabilities(record)
        self.assertFalse(evaluator._highest_candidate_check(
            record["selected_model"], record["research_probability"],
            probabilities,
        ))

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
            target_precision=0.90,
        )
        self.assertEqual(result["status"], "NOT_MEASURABLE")
        self.assertFalse(result["requirements"]["minimum_sample_met"])
        self.assertFalse(result["requirements"]["minimum_days_met"])
        self.assertFalse(result["requirements"]["minimum_cycles_met"])

    def test_overall_gate_requires_minimum_labels_on_both_sides(self) -> None:
        rows = [
            {
                "cycle_id": f"2026-08-{1 + index // 24:02d}T{index % 24:02d}:00",
                "side": "long",
                "horizon": "15m",
                "research_probability": 0.99,
                "after_cost_hit": True,
                "signed_return": 0.01,
                "executable_directional_return": 0.009,
                "signed_return_after_cost": 0.007,
            }
            for index in range(120)
        ]
        result = evaluator._metrics(
            rows,
            offline_gate_pass=False,
            min_sample=100,
            min_days=5,
            min_cycles=100,
            target_precision=0.90,
            min_long_labels=30,
            min_short_labels=30,
        )
        self.assertTrue(result["requirements"]["minimum_long_labels_met"])
        self.assertFalse(result["requirements"]["minimum_short_labels_met"])
        self.assertEqual(result["status"], "NOT_MEASURABLE")

    def test_cross_section_ranks_use_all_selected_records_and_stable_ties(self) -> None:
        records = [
            {
                "symbol": "B-USDT-SWAP", "side": "long", "horizon": "4H",
                "research_probability": 0.7,
                "selected_for_forward_evaluation": True,
                "ranking_diagnostics": {"selected_vs_opposite_margin": 0.1},
            },
            {
                "symbol": "A-USDT-SWAP", "side": "short", "horizon": "1H",
                "research_probability": 0.7,
                "selected_for_forward_evaluation": True,
                "ranking_diagnostics": {"selected_vs_opposite_margin": 0.2},
            },
            {
                "symbol": "C-USDT-SWAP", "side": "long", "horizon": "15m",
                "research_probability": 0.9,
                "selected_for_forward_evaluation": False,
                "ranking_diagnostics": {"selected_vs_opposite_margin": 0.9},
            },
        ]
        ranks = evaluator._selected_cross_section_ranks(records)
        self.assertEqual(ranks[1]["selected_probability_rank"], 1)
        self.assertEqual(ranks[0]["selected_probability_rank"], 2)
        self.assertEqual(ranks[1]["selected_margin_rank"], 1)
        self.assertEqual(ranks[0]["selected_margin_rank"], 2)
        self.assertEqual(ranks[0]["selected_cross_section_size"], 2)
        self.assertNotIn(2, ranks)


if __name__ == "__main__":
    unittest.main()
