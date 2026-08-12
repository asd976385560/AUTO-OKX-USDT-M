#!/usr/bin/env python3
"""Research a factorized 15m/1H/4H side-and-horizon selector.

The six-candidate task mixes two different questions: whether a horizon moves
far enough to clear the input panel's declared 20 bp hurdle, and which side
that move takes.  A panel with side-specific returns uses executable long
ask-to-bid and short bid-to-ask labels; legacy panels remain last-price-only
diagnostics.
This diagnostic models those questions separately and multiplies the two
train-only estimates into coherent long/short candidate probabilities.

Model fitting uses only the original training split.  The existing calibration
period is subdivided chronologically, with four-hour purges, into model
selection, threshold selection, and untouched internal confirmation windows.
The already-inspected historical holdout is retrospective only.  Outputs are
research artifacts; this module never writes a production database, changes a
production threshold, authorizes execution, or places an order.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import diagnose_selective_multitimeframe as baseline
import offline_multitimeframe_calibration as calibration


ROOT = Path(
    os.environ.get("OKX_ROOT") or Path(__file__).resolve().parents[1]
).resolve()
DEFAULT_INPUT = (
    ROOT / "reports" / "quality"
    / "goal-selective-multitimeframe-v1-20260812" / "research_panel.csv"
)
DEFAULT_OUTPUT = (
    ROOT / "reports" / "quality"
    / "goal-factorized-multitimeframe-v1-20260812"
)
TIMEFRAMES = baseline.TIMEFRAMES
SIDES = baseline.SIDES
CANDIDATE_KEYS = baseline._candidate_keys()
DEPTHS = (1, 2)
ROUNDS = (40, 80)
CONFIDENCE_TYPES = (
    "joint_probability",
    "top_margin",
    "direction_margin",
)


def _label_price_mode(panel: pd.DataFrame) -> str:
    side_columns = {
        f"{timeframe}_{side}_return"
        for timeframe in TIMEFRAMES for side in SIDES
    }
    present = side_columns & set(panel.columns)
    if present and present != side_columns:
        raise ValueError("partial side-specific return contract")
    return "executable" if present == side_columns else "last"


def _safe_divide(
    numerator: pd.Series,
    denominator: pd.Series,
) -> pd.Series:
    denom = pd.to_numeric(denominator, errors="coerce").replace(0, np.nan)
    return pd.to_numeric(numerator, errors="coerce") / denom


def _add_declared_relative_features(panel: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Add a small, fixed family of horizon-relative point-in-time features."""
    out = panel.copy()
    added: list[str] = []
    trend_components: list[pd.Series] = []
    for timeframe in TIMEFRAMES:
        atr = out[f"{timeframe}_atr_pct"]
        ma_distance = _safe_divide(
            out[f"{timeframe}_close_vs_ma20"], atr)
        return_over_atr = _safe_divide(
            out[f"{timeframe}_bar_return"], atr)
        ma_name = f"{timeframe}_close_vs_ma20_over_atr"
        return_name = f"{timeframe}_bar_return_over_atr"
        out[ma_name] = ma_distance.clip(-20.0, 20.0)
        out[return_name] = return_over_atr.clip(-20.0, 20.0)
        added.extend((ma_name, return_name))

        trend = (
            np.tanh(out[ma_name].fillna(0.0))
            + np.tanh(_safe_divide(
                out[f"{timeframe}_ma5_vs_ma20"], atr).fillna(0.0))
            + np.tanh(out[f"{timeframe}_macd_over_atr"].fillna(0.0))
            + out[f"{timeframe}_rsi_norm"].fillna(0.0).clip(-1.0, 1.0)
        ) / 4.0
        name = f"{timeframe}_declared_trend_score"
        out[name] = trend
        trend_components.append(out[name])
        added.append(name)

    trend_frame = pd.concat(trend_components, axis=1)
    out["declared_trend_mean"] = trend_frame.mean(axis=1)
    out["declared_trend_dispersion"] = trend_frame.max(axis=1) - trend_frame.min(axis=1)
    out["declared_trend_same_sign_count"] = (
        np.sign(trend_frame).eq(
            np.sign(trend_frame.mean(axis=1)), axis=0
        ).sum(axis=1)
        * trend_frame.mean(axis=1).ne(0)
    ).astype(float)
    added.extend((
        "declared_trend_mean",
        "declared_trend_dispersion",
        "declared_trend_same_sign_count",
    ))

    flow_columns = [
        name for name in (
            "taker_buy_centered", "cvd_share", "imbalance_10bp",
            "imbalance_25bp", "imbalance_50bp",
            "contract_taker_buy_centered",
        )
        if name in out.columns
    ]
    if flow_columns:
        out["declared_flow_consensus"] = (
            out[flow_columns].apply(pd.to_numeric, errors="coerce")
            .clip(-1.0, 1.0).mean(axis=1)
        )
        added.append("declared_flow_consensus")
    return out, added


def _factor_targets(
    panel: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return move, conditional-long, and candidate-success matrices."""
    labels = np.column_stack([
        panel[f"{timeframe}_{side}_success"].to_numpy(dtype=float)
        for timeframe, side in CANDIDATE_KEYS
    ])
    move = np.column_stack([
        (
            panel[f"{timeframe}_long_success"].to_numpy(dtype=float)
            + panel[f"{timeframe}_short_success"].to_numpy(dtype=float)
        )
        for timeframe in TIMEFRAMES
    ])
    conditional_long = np.column_stack([
        panel[f"{timeframe}_long_success"].to_numpy(dtype=float)
        for timeframe in TIMEFRAMES
    ])
    return move, conditional_long, labels


def _utility_matrix(panel: pd.DataFrame) -> np.ndarray:
    utilities = np.empty((len(panel), len(CANDIDATE_KEYS)), dtype=float)
    for index, (timeframe, side) in enumerate(CANDIDATE_KEYS):
        directional, _side_specific = baseline._candidate_forward_return(
            panel, timeframe, side)
        utilities[:, index] = directional - baseline.COST_HURDLE
    return utilities


def _fit_factor_models(
    bins: np.ndarray,
    move: np.ndarray,
    conditional_long: np.ndarray,
    *,
    depth: int,
) -> dict[str, list[baseline.GBDTModel]]:
    move_models: list[baseline.GBDTModel] = []
    direction_models: list[baseline.GBDTModel] = []
    for horizon_index, _timeframe in enumerate(TIMEFRAMES):
        move_y = move[:, horizon_index]
        move_models.append(baseline._fit_gbdt(
            bins, move_y, depth=depth, rounds=max(ROUNDS),
            learning_rate=0.08, min_leaf=140, l2=20.0,
        ))
        movers = move_y > 0.5
        if int(movers.sum()) < 100 or len(np.unique(
            conditional_long[movers, horizon_index]
        )) < 2:
            raise ValueError(
                f"insufficient two-sided mover labels for {TIMEFRAMES[horizon_index]}"
            )
        direction_models.append(baseline._fit_gbdt(
            bins[movers], conditional_long[movers, horizon_index],
            depth=depth, rounds=max(ROUNDS), learning_rate=0.08,
            min_leaf=80, l2=20.0,
        ))
    return {"move": move_models, "direction": direction_models}


def _raw_factor_probabilities(
    models: dict[str, list[baseline.GBDTModel]],
    bins: np.ndarray,
    *,
    rounds: int,
) -> tuple[np.ndarray, np.ndarray]:
    move = np.column_stack([
        calibration._sigmoid(model.raw_score(bins, rounds=rounds))
        for model in models["move"]
    ])
    direction = np.column_stack([
        calibration._sigmoid(model.raw_score(bins, rounds=rounds))
        for model in models["direction"]
    ])
    return move, direction


def _fit_factor_calibrators(
    raw_move: np.ndarray,
    raw_direction: np.ndarray,
    move_y: np.ndarray,
    conditional_long_y: np.ndarray,
) -> dict[str, list[tuple[float, float]]]:
    move_params: list[tuple[float, float]] = []
    direction_params: list[tuple[float, float]] = []
    for horizon_index, _timeframe in enumerate(TIMEFRAMES):
        move_params.append(calibration.fit_platt(
            raw_move[:, horizon_index], move_y[:, horizon_index]))
        movers = move_y[:, horizon_index] > 0.5
        direction_params.append(calibration.fit_platt(
            raw_direction[movers, horizon_index],
            conditional_long_y[movers, horizon_index],
        ))
    return {"move": move_params, "direction": direction_params}


def _candidate_probabilities(
    raw_move: np.ndarray,
    raw_direction: np.ndarray,
    calibrators: dict[str, list[tuple[float, float]]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    move_probability = np.column_stack([
        calibration.apply_platt(
            raw_move[:, index], calibrators["move"][index])
        for index in range(len(TIMEFRAMES))
    ])
    long_given_move = np.column_stack([
        calibration.apply_platt(
            raw_direction[:, index], calibrators["direction"][index])
        for index in range(len(TIMEFRAMES))
    ])
    candidates = np.empty((len(raw_move), len(CANDIDATE_KEYS)), dtype=float)
    for index, (timeframe, side) in enumerate(CANDIDATE_KEYS):
        horizon_index = TIMEFRAMES.index(timeframe)
        direction_probability = (
            long_given_move[:, horizon_index]
            if side == "long"
            else 1.0 - long_given_move[:, horizon_index]
        )
        candidates[:, index] = (
            move_probability[:, horizon_index] * direction_probability)
    return candidates, move_probability, long_given_move


def _raw_selection_confidence(
    candidates: np.ndarray,
    chosen: np.ndarray,
    confidence_type: str,
) -> np.ndarray:
    rows = np.arange(len(candidates), dtype=int)
    top = candidates[rows, chosen]
    ordered = np.sort(candidates, axis=1)
    if confidence_type == "joint_probability":
        return top
    if confidence_type == "top_margin":
        return calibration._sigmoid(8.0 * (ordered[:, -1] - ordered[:, -2]))
    if confidence_type == "direction_margin":
        opposite = chosen + np.where(chosen % 2 == 0, 1, -1)
        return calibration._sigmoid(
            8.0 * (top - candidates[rows, opposite]))
    raise ValueError(f"unknown confidence type: {confidence_type}")


def _selected_frame(
    panel: pd.DataFrame,
    labels: np.ndarray,
    utilities: np.ndarray,
    candidates: np.ndarray,
    move_probability: np.ndarray,
    long_given_move: np.ndarray,
    confidence_type: str,
) -> pd.DataFrame:
    chosen = np.argmax(candidates, axis=1)
    rows = np.arange(len(panel), dtype=int)
    horizon = np.asarray([key[0] for key in CANDIDATE_KEYS], dtype=object)
    side = np.asarray([key[1] for key in CANDIDATE_KEYS], dtype=object)
    output = panel[[
        "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol", "split",
    ]].copy().reset_index(drop=True)
    output["horizon"] = horizon[chosen]
    output["side"] = side[chosen]
    output["success"] = labels[rows, chosen]
    output["signed_return_after_cost"] = utilities[rows, chosen]
    output["raw_confidence"] = _raw_selection_confidence(
        candidates, chosen, confidence_type)
    output["joint_probability"] = candidates[rows, chosen]
    output["move_probability"] = move_probability[
        rows, np.asarray([TIMEFRAMES.index(value) for value in horizon[chosen]])]
    output["long_given_move_probability"] = long_given_move[
        rows, np.asarray([TIMEFRAMES.index(value) for value in horizon[chosen]])]
    output["top_vs_runner_up_margin"] = (
        np.sort(candidates, axis=1)[:, -1] - np.sort(candidates, axis=1)[:, -2]
    )
    opposite = chosen + np.where(chosen % 2 == 0, 1, -1)
    output["selected_vs_opposite_margin"] = (
        candidates[rows, chosen] - candidates[rows, opposite])
    return output


def _safe_platt(
    raw_probability: np.ndarray,
    outcome: np.ndarray,
) -> tuple[float, float]:
    if len(np.unique(outcome)) < 2:
        mean = float(np.clip(outcome.mean(), 1e-6, 1.0 - 1e-6))
        return math.log(mean / (1.0 - mean)), 0.0
    return calibration.fit_platt(raw_probability, outcome)


def _apply_selection_calibrator(
    frame: pd.DataFrame,
    params: tuple[float, float],
) -> pd.DataFrame:
    output = frame.copy()
    output["probability"] = calibration.apply_platt(
        output["raw_confidence"].to_numpy(dtype=float), params)
    return output


def _candidate_metrics(
    *,
    name: str,
    depth: int,
    rounds: int,
    confidence_type: str,
    selected: pd.DataFrame,
    selection_calibrator: tuple[float, float],
) -> dict[str, Any]:
    calibrated = _apply_selection_calibrator(selected, selection_calibrator)
    fixed = baseline._fixed_coverage_metrics(calibrated)
    top20 = next(row for row in fixed if row["requested_coverage"] == 0.20)
    outcome = calibrated["success"].to_numpy(dtype=float)
    probability = calibrated["probability"].to_numpy(dtype=float)
    return {
        "model": name,
        "depth": depth,
        "rounds": rounds,
        "confidence_type": confidence_type,
        "top_candidate_precision": float(outcome.mean()),
        "top20_precision": top20["precision"],
        "top20_wilson_low": top20["wilson_95_low"],
        "top20_n": top20["n"],
        "selected_brier": float(np.mean((probability - outcome) ** 2)),
        "fixed_coverage": fixed,
        "selection_platt_intercept": selection_calibrator[0],
        "selection_platt_slope": selection_calibrator[1],
    }


def _label_profile(
    panel: pd.DataFrame,
    move: np.ndarray,
    conditional_long: np.ndarray,
    named_masks: dict[str, np.ndarray],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, mask in named_masks.items():
        horizons: dict[str, Any] = {}
        for index, timeframe in enumerate(TIMEFRAMES):
            mover = move[mask, index] > 0.5
            horizons[timeframe] = {
                "observations": int(mask.sum()),
                "move_rate": float(move[mask, index].mean()),
                "movers": int(mover.sum()),
                "long_share_among_movers": (
                    float(conditional_long[mask, index][mover].mean())
                    if mover.any() else None
                ),
            }
        result[name] = {
            "distinct_cycles": int(panel.loc[mask, "obs_ts"].nunique()),
            "distinct_days": int(
                panel.loc[mask, "obs_ts"].dt.floor("D").nunique()),
            "horizons": horizons,
        }
    return result


def _notebook_payload(json_name: str, candidates_name: str) -> dict[str, Any]:
    return {
        "cells": [
            {
                "cell_type": "markdown", "metadata": {},
                "source": [
                    "# Factorized multi-timeframe diagnostic\n",
                    "Research-only. The historical holdout is retrospective and no output authorizes trading.\n",
                ],
            },
            {
                "cell_type": "code", "execution_count": None,
                "metadata": {}, "outputs": [],
                "source": [
                    "import json, pandas as pd\n",
                    f"diagnostic = json.loads(open('{json_name}', encoding='utf-8').read())\n",
                    f"candidates = pd.read_csv('{candidates_name}')\n",
                    "diagnostic['evaluation'], diagnostic['acceptance']\n",
                ],
            },
            {
                "cell_type": "code", "execution_count": None,
                "metadata": {}, "outputs": [],
                "source": [
                    "candidates.sort_values(['top20_wilson_low','top20_precision'], ascending=False).head(20)\n",
                ],
            },
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def diagnose(
    panel_path: Path,
    *,
    minimum_n: int = 100,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    panel = baseline._load_panel(panel_path)
    label_price_mode = _label_price_mode(panel)
    original_features = baseline._feature_columns(panel)
    panel, relative_features = _add_declared_relative_features(panel)
    feature_columns = [*original_features, *relative_features]
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
    move, conditional_long, labels = _factor_targets(usable)
    utilities = _utility_matrix(usable)
    id_sets = {
        "model_selection": set(
            panel.loc[tuning & complete, "obs_id"].tolist()),
        "threshold_selection": set(
            panel.loc[threshold_mask & complete, "obs_id"].tolist()),
        "internal_confirmation": set(
            panel.loc[confirmation & complete, "obs_id"].tolist()),
        "historical_holdout": set(
            panel.loc[panel["split"].eq("test") & complete, "obs_id"].tolist()),
    }
    named_masks = {
        name: usable["obs_id"].isin(ids).to_numpy(dtype=bool)
        for name, ids in id_sets.items()
    }
    train_mask = usable["split"].eq("train").to_numpy(dtype=bool)
    profile_masks = {"train": train_mask, **named_masks}

    train_assets = (
        usable.loc[train_mask, "asset_class"].fillna("unknown")
        .astype(str).value_counts()
    )
    asset_classes = tuple(sorted(train_assets[train_assets >= 20].index))
    _unused_candidates, candidate_features = baseline._expand_candidates(
        usable, feature_columns, asset_classes)
    # Factorized models are observation-grain.  Directional transforms are
    # represented by the conditional side model rather than candidate rows.
    observation_features = list(dict.fromkeys([
        *feature_columns,
        *[f"asset_class={asset}" for asset in asset_classes],
        "asset_class=other",
    ]))
    design = usable[[
        "obs_id", "asset_class", *feature_columns,
    ]].copy()
    for asset in asset_classes:
        design[f"asset_class={asset}"] = (
            design["asset_class"].astype(str).eq(asset).astype(float))
    design["asset_class=other"] = (
        ~design["asset_class"].astype(str).isin(asset_classes)).astype(float)
    spec = baseline._fit_bin_spec(
        design.loc[train_mask], observation_features)
    bins = baseline._transform_bins(design, spec)

    fitted_by_depth = {
        depth: _fit_factor_models(
            bins[train_mask], move[train_mask], conditional_long[train_mask],
            depth=depth,
        )
        for depth in DEPTHS
    }

    tuning_mask = named_masks["model_selection"]
    tuning_panel = usable.loc[tuning_mask].reset_index(drop=True)
    candidate_rows: list[dict[str, Any]] = []
    fitted: dict[str, dict[str, Any]] = {}
    for depth in DEPTHS:
        models = fitted_by_depth[depth]
        for rounds in ROUNDS:
            raw_move, raw_direction = _raw_factor_probabilities(
                models, bins[tuning_mask], rounds=rounds)
            factor_calibrators = _fit_factor_calibrators(
                raw_move, raw_direction,
                move[tuning_mask], conditional_long[tuning_mask],
            )
            candidates, move_probability, long_given_move = (
                _candidate_probabilities(
                    raw_move, raw_direction, factor_calibrators))
            for confidence_type in CONFIDENCE_TYPES:
                selected = _selected_frame(
                    tuning_panel, labels[tuning_mask], utilities[tuning_mask],
                    candidates, move_probability, long_given_move,
                    confidence_type,
                )
                selection_calibrator = _safe_platt(
                    selected["raw_confidence"].to_numpy(dtype=float),
                    selected["success"].to_numpy(dtype=float),
                )
                name = (
                    f"factorized_depth{depth}_rounds{rounds}_"
                    f"{confidence_type}"
                )
                candidate_rows.append(_candidate_metrics(
                    name=name, depth=depth, rounds=rounds,
                    confidence_type=confidence_type, selected=selected,
                    selection_calibrator=selection_calibrator,
                ))
                fitted[name] = {
                    "models": models,
                    "rounds": rounds,
                    "factor_calibrators": factor_calibrators,
                    "selection_calibrator": selection_calibrator,
                    "confidence_type": confidence_type,
                    "depth": depth,
                }

    chosen_row = max(candidate_rows, key=lambda row: (
        row["top20_wilson_low"], row["top20_precision"],
        row["top_candidate_precision"], -row["depth"], -row["rounds"],
        row["model"],
    ))
    chosen = fitted[str(chosen_row["model"])]
    selected_by_split: dict[str, pd.DataFrame] = {}
    for name, mask in named_masks.items():
        raw_move, raw_direction = _raw_factor_probabilities(
            chosen["models"], bins[mask], rounds=chosen["rounds"])
        candidates, move_probability, long_given_move = (
            _candidate_probabilities(
                raw_move, raw_direction, chosen["factor_calibrators"]))
        selected = _selected_frame(
            usable.loc[mask].reset_index(drop=True),
            labels[mask], utilities[mask], candidates,
            move_probability, long_given_move,
            str(chosen["confidence_type"]),
        )
        selected_by_split[name] = _apply_selection_calibrator(
            selected, chosen["selection_calibrator"])

    threshold_choice, threshold_curve = baseline._choose_threshold(
        selected_by_split["threshold_selection"], minimum_n=minimum_n)
    threshold = float(threshold_choice["threshold"])
    split_metrics = {
        name: baseline._selection_metrics(selected, threshold)
        for name, selected in selected_by_split.items()
    }
    confirmation_metrics = split_metrics["internal_confirmation"]
    holdout_metrics = split_metrics["historical_holdout"]
    requirements = {
        "threshold_window_reached_90pct": (
            threshold_choice["selection_status"]
            == "target_reached_on_threshold_window"),
        "confirmation_point_precision_at_least_90pct": (
            (confirmation_metrics["precision"] or 0.0) >= 0.90),
        "confirmation_wilson_95_low_at_least_90pct": (
            (confirmation_metrics["wilson_95_low"] or 0.0) >= 0.90),
        "confirmation_n_at_least_100": confirmation_metrics["n"] >= minimum_n,
        "confirmation_ece_at_most_5pp": (
            confirmation_metrics["ece"] is not None
            and confirmation_metrics["ece"] <= 0.05),
        "confirmation_days_at_least_2": (
            confirmation_metrics["distinct_days"] >= 2),
        "confirmation_cycles_at_least_20": (
            confirmation_metrics["distinct_cycles"] >= 20),
        "historical_holdout_point_precision_at_least_90pct": (
            (holdout_metrics["precision"] or 0.0) >= 0.90),
        "historical_holdout_wilson_95_low_at_least_90pct": (
            (holdout_metrics["wilson_95_low"] or 0.0) >= 0.90),
        "historical_holdout_n_at_least_100": holdout_metrics["n"] >= minimum_n,
        "historical_holdout_ece_at_most_5pp": (
            holdout_metrics["ece"] is not None
            and holdout_metrics["ece"] <= 0.05),
        "historical_holdout_days_at_least_5": (
            holdout_metrics["distinct_days"] >= 5),
        "historical_holdout_cycles_at_least_100": (
            holdout_metrics["distinct_cycles"] >= 100),
        "independent_future_shadow_window": False,
    }
    internal_names = (
        "threshold_window_reached_90pct",
        "confirmation_point_precision_at_least_90pct",
        "confirmation_wilson_95_low_at_least_90pct",
        "confirmation_n_at_least_100",
        "confirmation_ece_at_most_5pp",
        "confirmation_days_at_least_2",
        "confirmation_cycles_at_least_20",
    )
    internal_candidate = all(requirements[name] for name in internal_names)

    payload = {
        "schema_version": 1,
        "artifact_type": "factorized_multitimeframe_diagnostic",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_research",
        "input_panel": str(panel_path.resolve()),
        "data_quality": quality,
        "point_in_time_contract": {
            "features": "train-only median binning; no outcome column in features",
            "candidate_set": "exactly 3 horizons x 2 directions per complete observation",
            "factorization": (
                "P(candidate success) = P(input-label directional return "
                "clears 20bp) x P(direction | hurdle-clearing move)"),
            "label_price_mode": label_price_mode,
            "historical_label_semantics": (
                "long ask-to-bid and short bid-to-ask with no last fallback"
                if label_price_mode == "executable"
                else "last-price diagnostic only; it is not executable-price acceptance evidence"
            ),
            "forward_acceptance_semantics": (
                "only frozen future shadow labels using long ask-to-bid or "
                "short bid-to-ask plus the 20bp hurdle can prove 90 percent"),
            "nested_calibration": nested_contract,
            "historical_holdout": "already inspected; retrospective diagnostic only",
            "cost_hurdle_bps": baseline.COST_HURDLE * 10_000,
        },
        "feature_contract": {
            "original_point_in_time_features": len(original_features),
            "declared_relative_features": relative_features,
            "total_observation_features": len(observation_features),
            "train_only_bin_spec": True,
            "unused_candidate_expansion_feature_count": len(candidate_features),
        },
        "label_profile": _label_profile(
            usable, move, conditional_long, profile_masks),
        "model_family": {
            "algorithm": (
                "separate deterministic histogram boosted classifiers for "
                "hurdle-clearing movement and conditional direction"),
            "external_ml_dependencies": 0,
            "candidate_models": candidate_rows,
            "selection_rule": (
                "highest model-selection top-20pct Wilson lower bound"),
            "selected_model": chosen_row["model"],
            "candidate_family_size": len(candidate_rows),
            "multiple_testing_warning": (
                "small predeclared exploratory family; untouched internal "
                "confirmation must carry the conclusion"),
        },
        "threshold_selection": threshold_choice,
        "evaluation": split_metrics,
        "acceptance": {
            "target_precision": 0.90,
            "target_wilson_95_lower_bound": 0.90,
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
            "observational research; no causal claim",
            *(
                []
                if label_price_mode == "executable"
                else ["historical panel labels use last prices and cannot satisfy the executable-price acceptance gate"]
            ),
            "hourly cross-sectional observations are not statistically independent",
            "model family and selection calibrators use only model-selection data after train fitting",
            "historical holdout was inspected before this diagnostic and is retrospective only",
            "a newly frozen future-only shadow window remains mandatory",
        ],
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "production_execution_authorized": False,
        "orders_placed": 0,
    }
    holdout_selected = selected_by_split["historical_holdout"].loc[
        selected_by_split["historical_holdout"]["probability"].ge(threshold)
    ].copy()
    return payload, pd.DataFrame(candidate_rows), threshold_curve, holdout_selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-n", type=int, default=100)
    args = parser.parse_args(argv)
    if args.minimum_n <= 0:
        raise SystemExit("--minimum-n must be positive")
    try:
        payload, candidates, curve, holdout = diagnose(
            args.panel, minimum_n=args.minimum_n)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = args.output_dir / "factorized_diagnostic.json"
        candidates_path = args.output_dir / "factorized_model_candidates.csv"
        curve_path = args.output_dir / "factorized_threshold_curve.csv"
        holdout_path = args.output_dir / "historical_holdout_selected.csv"
        notebook_path = args.output_dir / "analysis.ipynb"
        baseline._atomic_json(json_path, payload)
        baseline._atomic_csv(candidates_path, candidates)
        baseline._atomic_csv(curve_path, curve)
        baseline._atomic_csv(holdout_path, holdout)
        baseline._atomic_json(
            notebook_path,
            _notebook_payload(json_path.name, candidates_path.name),
        )
        confirmation = payload["evaluation"]["internal_confirmation"]
        historical = payload["evaluation"]["historical_holdout"]
        print(json.dumps({
            "ok": True,
            "json_out": str(json_path.resolve()),
            "selected_model": payload["model_family"]["selected_model"],
            "internal_confirmation_n": confirmation["n"],
            "internal_confirmation_precision": confirmation["precision"],
            "internal_confirmation_wilson_95_low": confirmation["wilson_95_low"],
            "historical_holdout_n": historical["n"],
            "historical_holdout_precision": historical["precision"],
            "confidence_90_status": payload["acceptance"]["confidence_90_status"],
            "production_mutation": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_mutation": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
