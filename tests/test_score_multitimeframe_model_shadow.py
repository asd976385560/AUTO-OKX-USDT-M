from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import offline_multitimeframe_calibration as calibration  # noqa: E402
import score_multitimeframe_model_shadow as scorer  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spec() -> calibration.FeatureSpec:
    features = (*calibration.CONTINUOUS_FEATURES, *calibration.ENRICHMENT_FEATURES)
    names = ("intercept", *features, "asset_class=crypto", "asset_class=other")
    return calibration.FeatureSpec(
        medians={name: 0.0 for name in features},
        means={name: 0.0 for name in features},
        scales={name: 1.0 for name in features},
        continuous_features=features,
        asset_classes=("crypto",),
        feature_names=names,
    )


def _model(spec: calibration.FeatureSpec) -> dict:
    width = len(spec.feature_names)
    models = {}
    for key in scorer.REQUIRED_MODEL_KEYS:
        models[key] = {
            "weights": [0.0] * width,
            "platt_intercept": 2.0 if key == "15m_long" else 0.0,
            "platt_slope": 1.0,
        }
    return {
        "feature_set": "enhanced",
        "research_only": True,
        "feature_spec": spec.to_dict(),
        "models": models,
    }


def _metrics() -> dict:
    return {
        "feature_set": "enhanced",
        "production_threshold_change_allowed": False,
        "offline_gate_pass": False,
        "cost_hurdle_bps": 20.0,
        "holdout": {"precision": 0.4, "n": 200, "ece": 0.1},
    }


class FrozenModelShadowTests(unittest.TestCase):
    def test_frozen_prefix_contract_remains_scoreable_after_optional_features(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            features = (
                *calibration.CONTINUOUS_FEATURES,
                *calibration.ENRICHMENT_FEATURES[:-6],
            )
            spec = calibration.FeatureSpec(
                medians={name: 0.0 for name in features},
                means={name: 0.0 for name in features},
                scales={name: 1.0 for name in features},
                continuous_features=features,
                asset_classes=("crypto",),
                feature_names=(
                    "intercept", *features,
                    "asset_class=crypto", "asset_class=other",
                ),
            )
            model_path = root / "model.json"
            metrics_path = root / "metrics.json"
            model_path.write_text(json.dumps(_model(spec)), encoding="utf-8")
            metrics_path.write_text(json.dumps(_metrics()), encoding="utf-8")
            manifest = {
                "artifact_type": "frozen_multitimeframe_research_model",
                "production_execution_authorized": False,
                "orders_allowed": False,
                "model_parameters_path": "model.json",
                "model_parameters_sha256": _sha(model_path),
                "calibration_metrics_path": "metrics.json",
                "calibration_metrics_sha256": _sha(metrics_path),
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            _manifest, _model_payload, _metrics_payload, loaded = (
                scorer._load_bundle(root, manifest_path))

            self.assertEqual(loaded.continuous_features, features)

    def test_bundle_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = _spec()
            model_path = root / "model.json"
            metrics_path = root / "metrics.json"
            model_path.write_text(json.dumps(_model(spec)), encoding="utf-8")
            metrics_path.write_text(json.dumps(_metrics()), encoding="utf-8")
            manifest = {
                "artifact_type": "frozen_multitimeframe_research_model",
                "production_execution_authorized": False,
                "orders_allowed": False,
                "model_parameters_path": "model.json",
                "model_parameters_sha256": _sha(model_path),
                "calibration_metrics_path": "metrics.json",
                "calibration_metrics_sha256": _sha(metrics_path),
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            model_path.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                scorer._load_bundle(root, manifest_path)

    def test_cycle_must_be_a_natural_hour(self) -> None:
        self.assertEqual(
            scorer._cycle_utc("2026-08-12T08:00"),
            pd.Timestamp("2026-08-12T00:00:00Z").to_pydatetime(),
        )
        self.assertEqual(
            scorer._cycle_utc("2026-08-12T09:00"),
            pd.Timestamp("2026-08-12T01:00:00Z").to_pydatetime(),
        )
        with self.assertRaisesRegex(ValueError, "scheduled CST"):
            scorer._cycle_utc("2026-08-12T08:15")

    def test_frozen_prefix_clock_ignores_new_optional_contract_source(self) -> None:
        prefix_features = (
            *calibration.CONTINUOUS_FEATURES,
            *calibration.ENRICHMENT_FEATURES[:-6],
        )
        prefix_spec = calibration.FeatureSpec(
            medians={name: 0.0 for name in prefix_features},
            means={name: 0.0 for name in prefix_features},
            scales={name: 1.0 for name in prefix_features},
            continuous_features=prefix_features,
            asset_classes=("crypto",),
            feature_names=(
                "intercept", *prefix_features,
                "asset_class=crypto", "asset_class=other",
            ),
        )
        frame = pd.DataFrame({
            "obs_ts": pd.to_datetime(["2026-08-12T00:00:02Z"], utc=True),
            "micro_available_at": pd.to_datetime(
                ["2026-08-12T00:00:30Z"], utc=True),
            "flow_available_at": pd.to_datetime(
                ["2026-08-12T00:00:40Z"], utc=True),
            "positioning_available": [0.0],
            "positioning_available_at": pd.to_datetime([None], utc=True),
            "contract_stats_available": [1.0],
            "contract_stats_available_at": pd.to_datetime(
                ["2026-08-12T00:09:00Z"], utc=True),
        })

        frozen, frozen_audit = scorer._apply_frozen_feature_clock(
            frame, prefix_spec)
        future, future_audit = scorer._apply_frozen_feature_clock(frame, _spec())

        self.assertEqual(
            frozen.loc[0, "decision_ts"],
            pd.Timestamp("2026-08-12T00:00:40Z"),
        )
        self.assertFalse(
            frozen_audit["contract_statistics_affects_frozen_clock"])
        self.assertEqual(
            future.loc[0, "decision_ts"],
            pd.Timestamp("2026-08-12T00:09:00Z"),
        )
        self.assertTrue(
            future_audit["contract_statistics_affects_frozen_clock"])

    def test_highest_probability_is_shadow_only_and_uses_generation_clock(self) -> None:
        spec = _spec()
        model = _model(spec)
        manifest = {
            "model_id": "fixture",
            "model_parameters_sha256": "a" * 64,
            "first_forward_cycle_utc": "2026-08-12T00:00:00Z",
            "selection_threshold": 0.8,
        }
        frame_data = {name: [0.0] for name in spec.continuous_features}
        frame_data.update({
            "symbol": ["BTC-USDT-SWAP"],
            "asset_class": ["crypto"],
            "obs_ts": pd.to_datetime(["2026-08-12T00:00:02Z"], utc=True),
            "decision_ts": pd.to_datetime(["2026-08-12T00:00:40Z"], utc=True),
            "quote_volume_usd": [10_000_000.0],
            "oi_usd": [20_000_000.0],
            "positioning_available": [0.0],
        })
        frame = pd.DataFrame(frame_data)
        with (
            mock.patch.object(
                scorer,
                "_load_bundle",
                return_value=(manifest, model, _metrics(), spec),
            ),
            mock.patch.object(
                scorer,
                "_build_frame",
                return_value=(frame, {"scoring_ready_rows": 1}),
            ),
        ):
            payload = scorer.score_cycle(
                root=ROOT,
                db_root=ROOT / "db",
                manifest_path=Path("unused"),
                cycle_id="2026-08-12T08:00",
            )

        record = payload["records"][0]
        self.assertEqual(record["selected_model"], "15m_long")
        self.assertGreater(record["research_probability"], 0.8)
        self.assertTrue(record["selected_for_forward_evaluation"])
        self.assertGreater(
            record["ranking_diagnostics"]["top_vs_runner_up_margin"], 0)
        self.assertEqual(
            record["ranking_diagnostics"]["selected_side_horizon_votes"], 1)
        self.assertFalse(
            record["ranking_diagnostics"]["selected_side_unanimous"])
        self.assertFalse(
            record["future_retraining_features"][
                "contract_statistics_available"])
        self.assertGreaterEqual(
            pd.Timestamp(record["signal_available_at_utc"]),
            pd.Timestamp(payload["generated_at_utc"]),
        )
        self.assertFalse(record["confidence_claim_allowed"])
        self.assertFalse(record["production_execution_authorized"])
        self.assertFalse(payload["production_threshold_change_allowed"])
        self.assertEqual(payload["orders_placed"], 0)

    def test_pre_freeze_cycle_is_rejected_without_explicit_reconstruction(self) -> None:
        spec = _spec()
        manifest = {
            "first_forward_cycle_utc": "2026-08-12T00:00:00Z",
        }
        with mock.patch.object(
            scorer,
            "_load_bundle",
            return_value=(manifest, _model(spec), _metrics(), spec),
        ):
            with self.assertRaisesRegex(ValueError, "predates frozen"):
                scorer.score_cycle(
                    root=ROOT,
                    db_root=ROOT / "db",
                    manifest_path=Path("unused"),
                    cycle_id="2026-08-12T00:00",
                )


if __name__ == "__main__":
    unittest.main()
