#!/usr/bin/env python3
"""Diagnose groupwise 15m/1H/4H candidate ranking, research only.

The input is the point-in-time observation panel exported by
``offline_multitimeframe_calibration.py``.  Every complete observation has six
candidates (three horizons by two sides).  This script deliberately optimizes
the within-observation ranking rather than treating the six rows as unrelated
binary examples.

All feature fitting is train-only.  The existing calibration period is split
chronologically into model-selection, threshold-selection, and internal-
confirmation windows with four-hour purges.  The already-inspected historical
holdout is reported only as a retrospective diagnostic.  This script cannot
write a production database, alter thresholds, place orders, or authorize
production execution.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import diagnose_selective_multitimeframe as baseline
import offline_multitimeframe_calibration as calibration


DEFAULT_DIR = Path(
    r"./reports/quality/goal-selective-multitimeframe-v1-20260812"
)
DEFAULT_PANEL = DEFAULT_DIR / "research_panel.csv"
DEFAULT_JSON = DEFAULT_DIR / "ranking_diagnostic.json"
DEFAULT_MODEL = DEFAULT_DIR / "ranking_model.json"
DEFAULT_CANDIDATES = DEFAULT_DIR / "ranking_model_candidates.csv"
DEFAULT_CURVE = DEFAULT_DIR / "ranking_threshold_curve.csv"
DEFAULT_SELECTED = DEFAULT_DIR / "ranking_historical_holdout_selected.csv"
DEFAULT_NOTEBOOK = DEFAULT_DIR / "ranking_analysis.ipynb"

CANDIDATE_KEYS = baseline._candidate_keys()
TARGET_TYPES = ("uniform_positive", "best_positive_utility")
CONFIDENCE_TYPES = ("top_softmax", "top_margin")
LISTWISE_L2 = (0.0001, 0.001, 0.01)
RIDGE_L2 = (0.1, 1.0, 10.0, 100.0)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            frame.to_csv(handle, index=False, lineterminator="\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class RankingFeatureSpec:
    numeric_features: tuple[str, ...]
    medians: dict[str, float]
    lower: dict[str, float]
    upper: dict[str, float]
    means: dict[str, float]
    scales: dict[str, float]
    asset_classes: tuple[str, ...]
    rule_directions: tuple[str, ...]
    design_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "numeric_features": list(self.numeric_features),
            "medians": self.medians,
            "lower": self.lower,
            "upper": self.upper,
            "means": self.means,
            "scales": self.scales,
            "asset_classes": list(self.asset_classes),
            "rule_directions": list(self.rule_directions),
            "design_names": list(self.design_names),
        }


def _fit_feature_spec(
    train: pd.DataFrame,
    feature_columns: list[str],
) -> RankingFeatureSpec:
    if train.empty:
        raise ValueError("empty train split")
    numeric_features: list[str] = []
    medians: dict[str, float] = {}
    lower: dict[str, float] = {}
    upper: dict[str, float] = {}
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for name in feature_columns:
        values = pd.to_numeric(train[name], errors="coerce").replace(
            [np.inf, -np.inf], np.nan)
        if not values.notna().any():
            continue
        median = float(values.median())
        filled = values.fillna(median)
        low, high = (float(value) for value in filled.quantile(
            [0.005, 0.995]).tolist())
        clipped = filled.clip(low, high)
        mean = float(clipped.mean())
        scale = float(clipped.std(ddof=0))
        if not math.isfinite(scale) or scale <= 1e-9:
            continue
        numeric_features.append(name)
        medians[name] = median
        lower[name] = low
        upper[name] = high
        means[name] = mean
        scales[name] = scale
    asset_counts = train["asset_class"].fillna("unknown").astype(str).value_counts()
    assets = tuple(sorted(asset_counts[asset_counts >= 20].index.tolist()))
    rule_counts = train["rule_direction"].fillna("unknown").astype(str).value_counts()
    rules = tuple(sorted(rule_counts[rule_counts >= 20].index.tolist()))
    design_names = (
        "intercept",
        *numeric_features,
        *(f"asset_class={name}" for name in assets),
        "asset_class=other",
        *(f"rule_direction={name}" for name in rules),
        "rule_direction=other",
    )
    return RankingFeatureSpec(
        tuple(numeric_features), medians, lower, upper, means, scales,
        assets, rules, tuple(design_names),
    )


def _transform_features(
    frame: pd.DataFrame,
    spec: RankingFeatureSpec,
) -> np.ndarray:
    columns: list[np.ndarray] = [np.ones(len(frame), dtype=float)]
    for name in spec.numeric_features:
        values = pd.to_numeric(frame[name], errors="coerce").replace(
            [np.inf, -np.inf], np.nan).fillna(spec.medians[name])
        values = values.clip(spec.lower[name], spec.upper[name])
        standardized = (
            values.to_numpy(dtype=float) - spec.means[name]
        ) / spec.scales[name]
        columns.append(np.clip(standardized, -8.0, 8.0))
    assets = frame["asset_class"].fillna("unknown").astype(str)
    for name in spec.asset_classes:
        columns.append(assets.eq(name).to_numpy(dtype=float))
    columns.append((~assets.isin(spec.asset_classes)).to_numpy(dtype=float))
    rules = frame["rule_direction"].fillna("unknown").astype(str)
    for name in spec.rule_directions:
        columns.append(rules.eq(name).to_numpy(dtype=float))
    columns.append((~rules.isin(spec.rule_directions)).to_numpy(dtype=float))
    output = np.column_stack(columns)
    if output.shape[1] != len(spec.design_names):
        raise ValueError("feature design width mismatch")
    return output


def _label_matrices(
    panel: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    labels = np.column_stack([
        panel[f"{timeframe}_{side}_success"].to_numpy(dtype=float)
        for timeframe, side in CANDIDATE_KEYS
    ])
    utilities = np.empty_like(labels)
    for index, (timeframe, side) in enumerate(CANDIDATE_KEYS):
        horizon_index = baseline.TIMEFRAMES.index(timeframe)
        directional, _side_specific = baseline._candidate_forward_return(
            panel, timeframe, side)
        utilities[:, index] = directional - baseline.COST_HURDLE
    side_specific = all(
        f"{timeframe}_{side}_return" in panel.columns
        for timeframe, side in CANDIDATE_KEYS
    )
    returns = (
        utilities.copy()
        if side_specific
        else np.column_stack([
            panel[f"{timeframe}_return"].to_numpy(dtype=float)
            for timeframe in baseline.TIMEFRAMES
        ])
    )
    return labels, returns, utilities


def _softmax(scores: np.ndarray) -> np.ndarray:
    shifted = scores - np.max(scores, axis=1, keepdims=True)
    exponential = np.exp(np.clip(shifted, -60.0, 0.0))
    return exponential / exponential.sum(axis=1, keepdims=True)


def _listwise_targets(
    labels: np.ndarray,
    utilities: np.ndarray,
    target_type: str,
) -> tuple[np.ndarray, np.ndarray]:
    if target_type not in TARGET_TYPES:
        raise ValueError(f"unknown target_type: {target_type}")
    active = labels.sum(axis=1) > 0
    targets = np.zeros_like(labels, dtype=float)
    if target_type == "uniform_positive":
        targets[active] = labels[active] / labels[active].sum(
            axis=1, keepdims=True)
    else:
        safe_utility = np.where(labels > 0, utilities, -np.inf)
        winner = np.argmax(safe_utility[active], axis=1)
        targets[np.flatnonzero(active), winner] = 1.0
    return targets, active


def _fit_listwise_softmax(
    x: np.ndarray,
    targets: np.ndarray,
    active: np.ndarray,
    *,
    regularization: float,
    iterations: int = 360,
    learning_rate: float = 0.035,
) -> np.ndarray:
    if x.ndim != 2 or targets.ndim != 2 or len(x) != len(targets):
        raise ValueError("invalid listwise input shapes")
    if targets.shape[1] != len(CANDIDATE_KEYS):
        raise ValueError("listwise target candidate width mismatch")
    if int(active.sum()) < 100:
        raise ValueError("too few active listwise rows")
    active_x = x[active]
    active_targets = targets[active]
    weights = np.zeros((x.shape[1], targets.shape[1]), dtype=float)
    first = np.zeros_like(weights)
    second = np.zeros_like(weights)
    beta1 = 0.9
    beta2 = 0.999
    epsilon = 1e-8
    n = float(len(active_x))
    for step in range(1, iterations + 1):
        probability = _softmax(active_x @ weights)
        gradient = active_x.T @ (probability - active_targets) / n
        penalty = weights.copy()
        penalty[0, :] = 0.0
        gradient += regularization * penalty
        first = beta1 * first + (1.0 - beta1) * gradient
        second = beta2 * second + (1.0 - beta2) * (gradient * gradient)
        corrected_first = first / (1.0 - beta1 ** step)
        corrected_second = second / (1.0 - beta2 ** step)
        next_weights = weights - learning_rate * corrected_first / (
            np.sqrt(corrected_second) + epsilon)
        # Remove the unidentifiable common score shift deterministically.
        next_weights -= next_weights.mean(axis=1, keepdims=True)
        if float(np.max(np.abs(next_weights - weights))) < 1e-8:
            weights = next_weights
            break
        weights = next_weights
    return weights


def _fit_return_ridge(
    x: np.ndarray,
    returns: np.ndarray,
    *,
    regularization: float,
) -> tuple[np.ndarray, np.ndarray]:
    penalty = np.eye(x.shape[1], dtype=float) * regularization
    penalty[0, 0] = 0.0
    try:
        weights = np.linalg.solve(x.T @ x + penalty, x.T @ returns)
    except np.linalg.LinAlgError:
        weights = np.linalg.pinv(x.T @ x + penalty) @ x.T @ returns
    residual = returns - x @ weights
    scale = np.sqrt(np.mean(residual * residual, axis=0))
    scale = np.where(np.isfinite(scale) & (scale > 1e-6), scale, 1.0)
    return weights, scale


def _return_candidate_scores(
    x: np.ndarray,
    weights: np.ndarray,
    residual_scale: np.ndarray,
) -> np.ndarray:
    prediction = x @ weights
    if prediction.shape[1] == len(CANDIDATE_KEYS):
        if len(residual_scale) != len(CANDIDATE_KEYS):
            raise ValueError("six-candidate residual scale mismatch")
        return prediction / residual_scale
    if prediction.shape[1] != len(baseline.TIMEFRAMES):
        raise ValueError("return model must predict three horizons or six candidates")
    scores = np.empty((len(x), len(CANDIDATE_KEYS)), dtype=float)
    for index, (timeframe, side) in enumerate(CANDIDATE_KEYS):
        horizon_index = baseline.TIMEFRAMES.index(timeframe)
        direction = 1.0 if side == "long" else -1.0
        scores[:, index] = (
            direction * prediction[:, horizon_index] - baseline.COST_HURDLE
        ) / residual_scale[horizon_index]
    return scores


def _raw_confidence(
    scores: np.ndarray,
    confidence_type: str,
) -> np.ndarray:
    probability = _softmax(scores)
    ordered = np.sort(scores, axis=1)
    if confidence_type == "top_softmax":
        return probability.max(axis=1)
    if confidence_type == "top_margin":
        return calibration._sigmoid(ordered[:, -1] - ordered[:, -2])
    raise ValueError(f"unknown confidence_type: {confidence_type}")


def _selected_frame(
    panel: pd.DataFrame,
    labels: np.ndarray,
    utilities: np.ndarray,
    scores: np.ndarray,
    raw_confidence: np.ndarray,
) -> pd.DataFrame:
    chosen = np.argmax(scores, axis=1)
    horizon = np.asarray([key[0] for key in CANDIDATE_KEYS], dtype=object)
    side = np.asarray([key[1] for key in CANDIDATE_KEYS], dtype=object)
    frame = panel[[
        "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol", "split",
    ]].copy().reset_index(drop=True)
    rows = np.arange(len(panel), dtype=int)
    frame["horizon"] = horizon[chosen]
    frame["side"] = side[chosen]
    frame["success"] = labels[rows, chosen]
    frame["signed_return_after_cost"] = utilities[rows, chosen]
    frame["raw_confidence"] = raw_confidence
    frame["score_margin"] = (
        np.sort(scores, axis=1)[:, -1] - np.sort(scores, axis=1)[:, -2]
    )
    return frame


def _safe_platt(
    raw_probability: np.ndarray,
    outcome: np.ndarray,
) -> tuple[float, float]:
    mean = float(np.clip(outcome.mean(), 1e-6, 1 - 1e-6))
    if len(np.unique(outcome)) < 2:
        return math.log(mean / (1.0 - mean)), 0.0
    return calibration.fit_platt(raw_probability, outcome)


def _calibrated_selection(
    selected: pd.DataFrame,
    calibrator: tuple[float, float],
) -> pd.DataFrame:
    output = selected.copy()
    output["probability"] = calibration.apply_platt(
        output["raw_confidence"].to_numpy(dtype=float), calibrator)
    return output


def _profile_labels(
    panel: pd.DataFrame,
    labels: np.ndarray,
    named_masks: dict[str, np.ndarray],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for split_name, mask in named_masks.items():
        subset = labels[mask]
        successful_candidates = subset.sum(axis=1).astype(int)
        distribution = pd.Series(successful_candidates).value_counts().sort_index()
        output[split_name] = {
            "observations": int(len(subset)),
            "any_candidate_success_rate": float(
                (successful_candidates > 0).mean()) if len(subset) else None,
            "no_candidate_success_rate": float(
                (successful_candidates == 0).mean()) if len(subset) else None,
            "successful_candidate_count_distribution": {
                str(int(key)): int(value) for key, value in distribution.items()
            },
            "candidate_success_rates": {
                baseline._candidate_key_name(key): float(subset[:, index].mean())
                for index, key in enumerate(CANDIDATE_KEYS)
            },
            "distinct_cycles": int(panel.loc[mask, "obs_ts"].nunique()),
            "distinct_days": int(
                panel.loc[mask, "obs_ts"].dt.floor("D").nunique()),
        }
    return output


def _model_candidate_metrics(
    *,
    name: str,
    family: str,
    details: dict[str, Any],
    selected: pd.DataFrame,
    calibrator: tuple[float, float],
) -> dict[str, Any]:
    calibrated = _calibrated_selection(selected, calibrator)
    fixed = baseline._fixed_coverage_metrics(calibrated)
    top20 = next(row for row in fixed if row["requested_coverage"] == 0.20)
    outcome = calibrated["success"].to_numpy(dtype=float)
    probability = calibrated["probability"].to_numpy(dtype=float)
    return {
        "model": name,
        "family": family,
        **details,
        "selected_brier": float(np.mean((probability - outcome) ** 2)),
        "top_candidate_precision": float(outcome.mean()),
        "top20_precision": top20["precision"],
        "top20_wilson_low": top20["wilson_95_low"],
        "top20_n": top20["n"],
        "platt_intercept": calibrator[0],
        "platt_slope": calibrator[1],
        "fixed_coverage": fixed,
    }


def _notebook_payload(
    diagnostic_path: Path,
    candidates_path: Path,
    selected_path: Path,
) -> dict[str, Any]:
    base = diagnostic_path.parent
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.14"},
        },
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 15m/1H/4H groupwise ranking diagnostic\n",
                    "\n",
                    "Research-only, point-in-time analysis. The historical holdout is already inspected; no cell authorizes production trading.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "from pathlib import Path\n",
                    "import json\n",
                    "import pandas as pd\n",
                    f"base = Path(r'{base}')\n",
                    f"diagnostic = json.loads((base / '{diagnostic_path.name}').read_text(encoding='utf-8'))\n",
                    f"candidates = pd.read_csv(base / '{candidates_path.name}')\n",
                    f"holdout_selected = pd.read_csv(base / '{selected_path.name}')\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "pd.DataFrame(diagnostic['model_family']['candidate_models']).sort_values('top20_wilson_low', ascending=False)[['model', 'family', 'top20_precision', 'top20_wilson_low', 'top20_n']]\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "pd.DataFrame(diagnostic['evaluation']).T[['n', 'precision', 'wilson_95_low', 'ece', 'coverage_rate', 'distinct_days', 'distinct_cycles']]\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "holdout_selected.groupby(['horizon', 'side']).agg(n=('success', 'size'), precision=('success', 'mean'), mean_after_cost=('signed_return_after_cost', 'mean')).reset_index()\n",
                ],
            },
        ],
    }


def diagnose(
    panel_path: Path,
    *,
    minimum_n: int = 100,
) -> tuple[
    dict[str, Any], dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame,
]:
    panel = baseline._load_panel(panel_path)
    feature_columns = baseline._feature_columns(panel)
    complete = baseline._complete_outcome_mask(panel)
    tuning, threshold_mask, confirmation, nested_contract = (
        baseline._calibration_subsplits(panel))
    quality = baseline._quality_audit(
        panel, feature_columns, complete,
        (tuning, threshold_mask, confirmation),
    )
    if quality["status"] != "PASSED":
        raise ValueError("research panel failed critical data-quality checks")

    usable = panel.loc[complete].copy().reset_index(drop=True)
    labels, returns, utilities = _label_matrices(usable)
    id_sets = {
        "model_selection": set(panel.loc[tuning & complete, "obs_id"].tolist()),
        "threshold_selection": set(
            panel.loc[threshold_mask & complete, "obs_id"].tolist()),
        "internal_confirmation": set(
            panel.loc[confirmation & complete, "obs_id"].tolist()),
        "historical_holdout": set(
            panel.loc[panel["split"].eq("test") & complete, "obs_id"].tolist()),
    }
    named_masks = {
        name: usable["obs_id"].isin(values).to_numpy(dtype=bool)
        for name, values in id_sets.items()
    }
    train_mask = usable["split"].eq("train").to_numpy(dtype=bool)
    named_masks_with_train = {"train": train_mask, **named_masks}
    spec = _fit_feature_spec(usable.loc[train_mask], feature_columns)
    x = _transform_features(usable, spec)

    fitted: dict[str, dict[str, Any]] = {}
    candidate_rows: list[dict[str, Any]] = []
    tuning_rows = usable.loc[named_masks["model_selection"]].reset_index(drop=True)
    tuning_labels = labels[named_masks["model_selection"]]
    tuning_utilities = utilities[named_masks["model_selection"]]

    for target_type in TARGET_TYPES:
        targets, active = _listwise_targets(
            labels[train_mask], utilities[train_mask], target_type)
        for regularization in LISTWISE_L2:
            weights = _fit_listwise_softmax(
                x[train_mask], targets, active,
                regularization=regularization,
            )
            tuning_scores = x[named_masks["model_selection"]] @ weights
            for confidence_type in CONFIDENCE_TYPES:
                raw = _raw_confidence(tuning_scores, confidence_type)
                selected = _selected_frame(
                    tuning_rows, tuning_labels, tuning_utilities,
                    tuning_scores, raw,
                )
                calibrator = _safe_platt(
                    raw, selected["success"].to_numpy(dtype=float))
                name = (
                    f"listwise_{target_type}_l2_{regularization:g}_"
                    f"{confidence_type}"
                )
                candidate_rows.append(_model_candidate_metrics(
                    name=name,
                    family="listwise_softmax",
                    details={
                        "target_type": target_type,
                        "regularization": regularization,
                        "confidence_type": confidence_type,
                        "active_train_rows": int(active.sum()),
                    },
                    selected=selected,
                    calibrator=calibrator,
                ))
                fitted[name] = {
                    "family": "listwise_softmax",
                    "weights": weights,
                    "confidence_type": confidence_type,
                    "calibrator": calibrator,
                    "target_type": target_type,
                    "regularization": regularization,
                }

    for regularization in RIDGE_L2:
        weights, residual_scale = _fit_return_ridge(
            x[train_mask], returns[train_mask],
            regularization=regularization,
        )
        tuning_scores = _return_candidate_scores(
            x[named_masks["model_selection"]], weights, residual_scale)
        for confidence_type in CONFIDENCE_TYPES:
            raw = _raw_confidence(tuning_scores, confidence_type)
            selected = _selected_frame(
                tuning_rows, tuning_labels, tuning_utilities,
                tuning_scores, raw,
            )
            calibrator = _safe_platt(
                raw, selected["success"].to_numpy(dtype=float))
            name = (
                f"return_ridge_l2_{regularization:g}_{confidence_type}"
            )
            candidate_rows.append(_model_candidate_metrics(
                name=name,
                family="return_ridge",
                details={
                    "regularization": regularization,
                    "confidence_type": confidence_type,
                    "residual_scale_15m": float(residual_scale[0]),
                    "residual_scale_1H": float(residual_scale[1]),
                    "residual_scale_4H": float(residual_scale[2]),
                },
                selected=selected,
                calibrator=calibrator,
            ))
            fitted[name] = {
                "family": "return_ridge",
                "weights": weights,
                "residual_scale": residual_scale,
                "confidence_type": confidence_type,
                "calibrator": calibrator,
                "regularization": regularization,
            }

    chosen_row = max(candidate_rows, key=lambda row: (
        row["top20_wilson_low"], row["top20_precision"],
        row["top_candidate_precision"],
        row["family"] == "listwise_softmax",
        -float(row["regularization"]), row["model"],
    ))
    chosen = fitted[str(chosen_row["model"])]

    selected_by_split: dict[str, pd.DataFrame] = {}
    for split_name, mask in named_masks.items():
        split_x = x[mask]
        if chosen["family"] == "listwise_softmax":
            scores = split_x @ chosen["weights"]
        else:
            scores = _return_candidate_scores(
                split_x, chosen["weights"], chosen["residual_scale"])
        raw = _raw_confidence(scores, str(chosen["confidence_type"]))
        selected = _selected_frame(
            usable.loc[mask].reset_index(drop=True),
            labels[mask], utilities[mask], scores, raw,
        )
        selected_by_split[split_name] = _calibrated_selection(
            selected, chosen["calibrator"])

    threshold_choice, threshold_curve = baseline._choose_threshold(
        selected_by_split["threshold_selection"], minimum_n=minimum_n)
    threshold = float(threshold_choice["threshold"])
    split_metrics = {
        name: baseline._selection_metrics(best, threshold)
        for name, best in selected_by_split.items()
    }
    oracles = {}
    for name, mask in named_masks.items():
        selected_ids = set(selected_by_split[name].loc[
            selected_by_split[name]["probability"].ge(threshold), "obs_id"
        ].tolist())
        selected_observation_mask = mask & usable["obs_id"].isin(
            selected_ids).to_numpy(dtype=bool)
        subset = labels[selected_observation_mask]
        oracles[name] = {
            "selected_complete_observations": int(len(subset)),
            "any_candidate_success_rate": float(
                (subset.max(axis=1) > 0).mean()) if len(subset) else None,
        }

    confirmation_metrics = split_metrics["internal_confirmation"]
    holdout_metrics = split_metrics["historical_holdout"]
    requirements = {
        "threshold_window_reached_90pct": (
            threshold_choice["selection_status"]
            == "target_reached_on_threshold_window"),
        "confirmation_precision_at_least_90pct": (
            (confirmation_metrics["precision"] or 0.0) >= 0.90),
        "confirmation_n_at_least_100": confirmation_metrics["n"] >= 100,
        "confirmation_ece_at_most_5pp": (
            confirmation_metrics["ece"] is not None
            and confirmation_metrics["ece"] <= 0.05),
        "historical_holdout_precision_at_least_90pct": (
            (holdout_metrics["precision"] or 0.0) >= 0.90),
        "historical_holdout_n_at_least_100": holdout_metrics["n"] >= 100,
        "historical_holdout_ece_at_most_5pp": (
            holdout_metrics["ece"] is not None
            and holdout_metrics["ece"] <= 0.05),
        "historical_holdout_days_at_least_5": (
            holdout_metrics["distinct_days"] >= 5),
        "historical_holdout_cycles_at_least_100": (
            holdout_metrics["distinct_cycles"] >= 100),
        "independent_future_shadow_window": False,
    }
    internal_candidate = all(requirements[name] for name in (
        "threshold_window_reached_90pct",
        "confirmation_precision_at_least_90pct",
        "confirmation_n_at_least_100",
        "confirmation_ece_at_most_5pp",
    ))
    label_profile = _profile_labels(
        usable, labels, named_masks_with_train)
    payload = {
        "schema_version": 1,
        "artifact_type": "groupwise_multitimeframe_ranking_diagnostic",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_research",
        "input_panel": str(panel_path),
        "data_quality": quality,
        "point_in_time_contract": {
            "features": "train-only imputation, winsorization, and standardization; outcomes excluded",
            "candidate_set": "exactly 3 horizons x 2 directions per complete observation",
            "nested_calibration": nested_contract,
            "historical_holdout": "already inspected; retrospective diagnostic only",
            "cost_hurdle_bps": baseline.COST_HURDLE * 10_000,
        },
        "label_profile": label_profile,
        "oracle_interpretation": (
            "Any-candidate success is mechanically high because both directions "
            "are offered at three horizons; it is an upper bound, not evidence "
            "that the successful side or horizon is predictable."
        ),
        "feature_spec": {
            "design_width": len(spec.design_names),
            "numeric_feature_count": len(spec.numeric_features),
            "dropped_constant_or_empty_features": sorted(
                set(feature_columns) - set(spec.numeric_features)),
            "train_only": True,
        },
        "model_family": {
            "algorithms": [
                "multi-positive listwise softmax regression",
                "multi-output ridge return regression",
            ],
            "external_ml_dependencies": 0,
            "candidate_models": candidate_rows,
            "selection_rule": "highest model-selection top-20pct Wilson lower bound",
            "selected_model": chosen_row["model"],
            "selected_family": chosen_row["family"],
            "selected_confidence_type": chosen_row["confidence_type"],
        },
        "threshold_selection": threshold_choice,
        "evaluation": split_metrics,
        "selected_subset_oracle_diagnostic": oracles,
        "acceptance": {
            "target_precision": 0.90,
            "minimum_n": minimum_n,
            "maximum_ece": 0.05,
            "requirements": requirements,
            "internal_candidate_status": (
                "ELIGIBLE_TO_FREEZE_FOR_FUTURE_SHADOW"
                if internal_candidate else "NOT_ELIGIBLE"),
            "confidence_90_status": "NOT_PROVEN",
            "production_status": "NO_CHANGE_ALLOWED",
        },
        "limitations": [
            "observational backtest; no causal claim",
            "hourly cross-sectional observations are not independent",
            "the model-selection window also fits Platt calibration",
            "the historical holdout was already inspected before this diagnostic",
            "an independent future live-shadow window is mandatory before any production consideration",
        ],
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "orders_placed": 0,
    }
    model_payload = {
        "schema_version": 1,
        "research_only": True,
        "selected_model": chosen_row["model"],
        "family": chosen["family"],
        "confidence_type": chosen["confidence_type"],
        "threshold": threshold,
        "platt_intercept": chosen["calibrator"][0],
        "platt_slope": chosen["calibrator"][1],
        "feature_spec": spec.to_dict(),
        "weights": chosen["weights"].tolist(),
        "residual_scale": (
            chosen["residual_scale"].tolist()
            if chosen["family"] == "return_ridge" else None),
        "candidate_keys": [
            {"horizon": horizon, "side": side}
            for horizon, side in CANDIDATE_KEYS
        ],
        "production_threshold_change_allowed": False,
    }
    holdout_selected = selected_by_split["historical_holdout"].loc[
        selected_by_split["historical_holdout"]["probability"].ge(threshold)
    ].copy()
    return (
        payload, model_payload, pd.DataFrame(candidate_rows),
        threshold_curve, holdout_selected,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--candidates-out", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--curve-out", type=Path, default=DEFAULT_CURVE)
    parser.add_argument("--selected-out", type=Path, default=DEFAULT_SELECTED)
    parser.add_argument("--notebook-out", type=Path, default=DEFAULT_NOTEBOOK)
    parser.add_argument("--minimum-n", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        payload, model, candidates, curve, selected = diagnose(
            args.panel, minimum_n=args.minimum_n)
        _atomic_json(args.json_out, payload)
        _atomic_json(args.model_out, model)
        _atomic_csv(args.candidates_out, candidates)
        _atomic_csv(args.curve_out, curve)
        _atomic_csv(args.selected_out, selected)
        _atomic_json(args.notebook_out, _notebook_payload(
            args.json_out, args.candidates_out, args.selected_out))
    except (OSError, ValueError, KeyError, pd.errors.ParserError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    confirmation = payload["evaluation"]["internal_confirmation"]
    holdout = payload["evaluation"]["historical_holdout"]
    print(json.dumps({
        "ok": True,
        "json_out": str(args.json_out),
        "selected_model": payload["model_family"]["selected_model"],
        "threshold_status": payload["threshold_selection"]["selection_status"],
        "confirmation_precision": confirmation["precision"],
        "confirmation_n": confirmation["n"],
        "confirmation_ece": confirmation["ece"],
        "historical_holdout_precision": holdout["precision"],
        "confidence_90_status": payload["acceptance"]["confidence_90_status"],
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
