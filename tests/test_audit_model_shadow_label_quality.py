from __future__ import annotations

import csv
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

import audit_model_shadow_label_quality as auditor  # noqa: E402
import evaluate_multitimeframe_model_shadow as evaluator  # noqa: E402
from tests.test_evaluate_multitimeframe_model_shadow import _artifact  # noqa: E402


UTC = timezone.utc


def _fixture(root: Path) -> tuple[Path, Path, Path, Path]:
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
    evaluation = root / "evaluation.json"
    evaluation.write_text(json.dumps(payload), encoding="utf-8")
    labels_path = root / "labels.csv"
    with labels_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=evaluator.LABEL_COLUMNS)
        writer.writeheader()
        writer.writerows(labels)
    return evaluation, labels_path, shadow, market


class ModelShadowLabelQualityAuditTests(unittest.TestCase):
    def test_independent_reconstruction_passes_clean_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "PASSED")
        self.assertTrue(result["safe_for_credibility_research"])
        self.assertEqual(result["row_profile"]["observed_labels"], 1)
        self.assertTrue(all(result["checks"].values()))
        self.assertTrue(all(result["safety_checks"].values()))
        self.assertTrue(result["safety_checks"]["confidence_claim_disallowed"])
        self.assertFalse(result["production_execution_authorized"])
        self.assertEqual(result["orders_placed"], 0)

    def test_tampered_executable_price_and_return_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            with labels.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                fieldnames = reader.fieldnames
            rows[0]["entry_executable"] = "1.0"
            rows[0]["signed_return_after_cost"] = "99.0"
            with labels.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "NOT_MET")
        self.assertFalse(result["checks"]["all_label_fields_match_raw_evidence"])
        self.assertFalse(result["safe_for_credibility_research"])

    def test_duplicate_label_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            with labels.open("r", encoding="utf-8", newline="") as handle:
                text = handle.read()
            header, row = text.strip().splitlines()
            labels.write_text(f"{header}\n{row}\n{row}\n", encoding="utf-8")
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "NOT_MET")
        self.assertFalse(result["checks"]["label_keys_unique"])
        self.assertFalse(result["checks"]["label_count_matches_evaluation"])

    def test_weakened_acceptance_threshold_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            payload = json.loads(evaluation.read_text(encoding="utf-8"))
            payload["acceptance_contract"]["target_precision"] = 0.89
            evaluation.write_text(json.dumps(payload), encoding="utf-8")
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "NOT_MET")
        self.assertFalse(result["checks"]["acceptance_thresholds_not_weakened"])

    def test_tampered_side_diagnostic_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            payload = json.loads(evaluation.read_text(encoding="utf-8"))
            payload["models"][0]["diagnostics"]["by_side"][0][
                "precision_after_cost"
            ] = 0.0
            evaluation.write_text(json.dumps(payload), encoding="utf-8")
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "NOT_MET")
        self.assertFalse(result["checks"]["aggregate_metrics_match_labels"])

    def test_tampered_cross_section_rank_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            with labels.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                fieldnames = reader.fieldnames
            rows[0]["selected_probability_rank"] = "2"
            with labels.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "NOT_MET")
        self.assertFalse(result["checks"]["all_label_fields_match_raw_evidence"])

    def test_tampered_candidate_probability_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            with labels.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                fieldnames = reader.fieldnames
            rows[0]["candidate_probability_1H_long"] = "0.01"
            with labels.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "NOT_MET")
        self.assertFalse(result["checks"]["all_label_fields_match_raw_evidence"])

    def test_tampered_feature_diagnostic_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            payload = json.loads(evaluation.read_text(encoding="utf-8"))
            payload["models"][0]["diagnostics"]["by_asset_class"][0][
                "n_labeled"] = 99
            evaluation.write_text(json.dumps(payload), encoding="utf-8")
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "NOT_MET")
        self.assertFalse(result["checks"]["aggregate_metrics_match_labels"])

    def test_missing_preregistered_day_slice_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            payload = json.loads(evaluation.read_text(encoding="utf-8"))
            payload["models"][0].pop("by_day")
            evaluation.write_text(json.dumps(payload), encoding="utf-8")
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "NOT_MET")
        self.assertFalse(result["checks"]["aggregate_metrics_match_labels"])

    def test_duplicate_preregistered_day_slice_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            payload = json.loads(evaluation.read_text(encoding="utf-8"))
            payload["models"][0]["by_day"].append(
                dict(payload["models"][0]["by_day"][0]))
            evaluation.write_text(json.dumps(payload), encoding="utf-8")
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertEqual(result["status"], "NOT_MET")
        self.assertFalse(result["checks"]["aggregate_metrics_match_labels"])

    def test_stricter_acceptance_thresholds_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            payload = json.loads(evaluation.read_text(encoding="utf-8"))
            contract = payload["acceptance_contract"]
            contract["target_precision"] = 0.95
            contract["minimum_wilson_95_lower_bound"] = 0.95
            contract["maximum_ece"] = 0.03
            contract["minimum_sample"] = 200
            contract["minimum_days"] = 10
            contract["minimum_distinct_cycles"] = 200
            # Recompute the reported metrics under the stricter gates so only
            # the non-weakening contract itself is under test.
            def tighten_requirements(value):
                if isinstance(value, dict):
                    requirements = value.get("requirements")
                    if isinstance(requirements, dict):
                        requirements.update({
                            "minimum_sample_met": False,
                            "minimum_days_met": False,
                            "minimum_cycles_met": False,
                        })
                    for child in value.values():
                        tighten_requirements(child)
                elif isinstance(value, list):
                    for child in value:
                        tighten_requirements(child)

            for model in payload["models"]:
                model["overall"]["requirements"].update({
                    "minimum_sample_met": False,
                    "minimum_days_met": False,
                    "minimum_cycles_met": False,
                })
                for horizon in model["by_horizon"]:
                    horizon["requirements"].update({
                        "minimum_sample_met": False,
                        "minimum_days_met": False,
                        "minimum_cycles_met": False,
                    })
                for day in model["by_day"]:
                    day["requirements"].update({
                        "minimum_sample_met": False,
                        "minimum_days_met": False,
                        "minimum_cycles_met": False,
                    })
                tighten_requirements(model["diagnostics"])
            evaluation.write_text(json.dumps(payload), encoding="utf-8")
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertTrue(result["checks"]["acceptance_thresholds_not_weakened"])
        self.assertEqual(result["status"], "PASSED")

    def test_side_diversity_threshold_cannot_be_weakened(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evaluation, labels, shadow, market = _fixture(Path(temp))
            payload = json.loads(evaluation.read_text(encoding="utf-8"))
            payload["acceptance_contract"]["minimum_short_labels"] = 29
            evaluation.write_text(json.dumps(payload), encoding="utf-8")
            result = auditor.audit(
                evaluation_path=evaluation,
                labels_path=labels,
                shadow_root=shadow,
                market_db=market,
            )
        self.assertFalse(
            result["checks"]["acceptance_thresholds_not_weakened"])
        self.assertEqual(result["status"], "NOT_MET")


if __name__ == "__main__":
    unittest.main()
