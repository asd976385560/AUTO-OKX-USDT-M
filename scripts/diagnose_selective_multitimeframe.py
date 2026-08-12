#!/usr/bin/env python3
"""Research a nonlinear, selective 15m/1H/4H decision policy.

The input is the observation-grain point-in-time panel exported by
``offline_multitimeframe_calibration.py``.  The script uses only NumPy/Pandas
and a deterministic histogram gradient-boosted tree implementation.  It keeps
the original train/calibration/historical-holdout split, subdivides the
calibration window chronologically with four-hour purges, and separates model
selection, threshold selection, and internal confirmation.

The historical holdout has already been inspected by earlier research and is
therefore reported only as a retrospective diagnostic.  No result from this
script can change a production threshold, authorize an order, or write a
production database.
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
from typing import Any, Iterable

import numpy as np
import pandas as pd

import offline_multitimeframe_calibration as calibration


TIMEFRAMES = ("15m", "1H", "4H")
SIDES = ("long", "short")
COST_HURDLE = 0.002
DEFAULT_INPUT = Path(
    r"./reports/quality/goal-selective-multitimeframe-v1-20260812"
)
DEFAULT_OUTPUT = DEFAULT_INPUT / "selective_diagnostic.json"
DEFAULT_MODEL = DEFAULT_INPUT / "selective_model.json"
DEFAULT_CANDIDATES = DEFAULT_INPUT / "selective_model_candidates.csv"
DEFAULT_CURVE = DEFAULT_INPUT / "selective_threshold_curve.csv"
DEFAULT_SELECTED = DEFAULT_INPUT / "historical_holdout_selected.csv"

META_COLUMNS = {
    "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol",
    "asset_class", "rule_direction", "split",
}
DIRECTIONAL_FEATURES = (
    "chg24h", "funding_rate", "premium", "alignment_score",
    "15m_close_vs_ma20", "15m_ma5_vs_ma20", "15m_rsi_norm",
    "15m_macd_over_atr", "15m_bar_return",
    "1H_close_vs_ma20", "1H_ma5_vs_ma20", "1H_rsi_norm",
    "1H_macd_over_atr", "1H_bar_return",
    "4H_close_vs_ma20", "4H_ma5_vs_ma20", "4H_rsi_norm",
    "4H_macd_over_atr", "4H_bar_return",
    "imbalance_10bp", "imbalance_25bp", "imbalance_50bp",
    "slippage_asymmetry_500usd_bps", "taker_buy_centered",
    "cvd_share", "positioning_long_short_log",
    "positioning_long_minus_short",
    "contract_oi_log_change_15m", "contract_taker_buy_centered",
    "contract_oi_taker_interaction",
)


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


def _outcome_columns() -> list[str]:
    return [
        name
        for timeframe in TIMEFRAMES
        for name in (
            f"{timeframe}_return",
            f"{timeframe}_long_return",
            f"{timeframe}_short_return",
            f"{timeframe}_long_success",
            f"{timeframe}_short_success",
        )
    ]


def _required_outcome_columns() -> list[str]:
    return [
        name
        for timeframe in TIMEFRAMES
        for name in (
            f"{timeframe}_return",
            f"{timeframe}_long_success",
            f"{timeframe}_short_success",
        )
    ]


def _candidate_forward_return(
    panel: pd.DataFrame,
    timeframe: str,
    side: str,
) -> tuple[np.ndarray, bool]:
    side_specific = f"{timeframe}_{side}_return"
    if side_specific in panel.columns:
        return panel[side_specific].to_numpy(dtype=float), True
    direction = 1.0 if side == "long" else -1.0
    return (
        direction * panel[f"{timeframe}_return"].to_numpy(dtype=float),
        False,
    )


def _feature_columns(panel: pd.DataFrame) -> list[str]:
    outcomes = set(_outcome_columns())
    return [
        name for name in panel.columns
        if name not in META_COLUMNS and name not in outcomes
    ]


def _load_panel(path: Path) -> pd.DataFrame:
    panel = pd.read_csv(path)
    required = META_COLUMNS | set(_required_outcome_columns())
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError("research panel missing columns: " + ",".join(missing))
    for name in ("obs_ts", "decision_ts", "entry_ts"):
        panel[name] = pd.to_datetime(panel[name], utc=True, errors="raise")
    numeric_columns = [
        *_feature_columns(panel),
        *[name for name in _outcome_columns() if name in panel.columns],
    ]
    for name in numeric_columns:
        panel[name] = pd.to_numeric(panel[name], errors="coerce")
    if panel["obs_id"].duplicated().any():
        raise ValueError("research panel obs_id is not unique")
    allowed = {"train", "calibration", "test", "purged"}
    unexpected = sorted(set(panel["split"].astype(str)) - allowed)
    if unexpected:
        raise ValueError("unexpected split values: " + ",".join(unexpected))
    return panel


def _complete_outcome_mask(panel: pd.DataFrame) -> pd.Series:
    labels = [
        f"{timeframe}_{side}_success"
        for timeframe in TIMEFRAMES for side in SIDES
    ]
    return panel[labels].notna().all(axis=1)


def _calibration_subsplits(
    panel: pd.DataFrame,
    *,
    purge_hours: int = 4,
) -> tuple[pd.Series, pd.Series, pd.Series, dict[str, Any]]:
    calibration_rows = panel.loc[panel["split"].eq("calibration")]
    times = np.array(sorted(calibration_rows["obs_ts"].unique()))
    if len(times) < 12:
        raise ValueError("calibration window has too few distinct cycles")
    first_boundary = pd.Timestamp(times[len(times) // 2])
    second_boundary = pd.Timestamp(times[(3 * len(times)) // 4])
    purge = pd.Timedelta(hours=purge_hours)
    obs = panel["obs_ts"]
    base = panel["split"].eq("calibration")
    tuning = base & obs.lt(first_boundary - purge)
    threshold = base & obs.ge(first_boundary) & obs.lt(second_boundary - purge)
    confirmation = base & obs.ge(second_boundary)
    if not tuning.any() or not threshold.any() or not confirmation.any():
        raise ValueError("empty nested calibration subwindow")
    return tuning, threshold, confirmation, {
        "method": "chronological 50/25/25 with four-hour purges",
        "purge_hours": purge_hours,
        "model_selection_end_exclusive": (
            first_boundary - purge).isoformat(),
        "threshold_selection_start": first_boundary.isoformat(),
        "threshold_selection_end_exclusive": (
            second_boundary - purge).isoformat(),
        "confirmation_start": second_boundary.isoformat(),
        "discarded_purge_rows": int(
            (base & ~(tuning | threshold | confirmation)).sum()),
    }


def _quality_audit(
    panel: pd.DataFrame,
    feature_columns: list[str],
    complete: pd.Series,
    nested_masks: tuple[pd.Series, pd.Series, pd.Series],
) -> dict[str, Any]:
    tuning, threshold, confirmation = nested_masks
    clock_violations = int((
        panel["obs_ts"].gt(panel["decision_ts"])
        | panel["decision_ts"].ge(panel["entry_ts"])
    ).sum())
    split_ranges: dict[str, Any] = {}
    named_masks = {
        "train": panel["split"].eq("train"),
        "model_selection": tuning,
        "threshold_selection": threshold,
        "internal_confirmation": confirmation,
        "historical_holdout": panel["split"].eq("test"),
    }
    for name, mask in named_masks.items():
        rows = panel.loc[mask]
        split_ranges[name] = {
            "rows": int(len(rows)),
            "complete_outcome_rows": int((mask & complete).sum()),
            "start_utc": rows["obs_ts"].min().isoformat() if len(rows) else None,
            "end_utc": rows["obs_ts"].max().isoformat() if len(rows) else None,
            "distinct_cycles": int(rows["obs_ts"].nunique()),
            "distinct_days": int(rows["obs_ts"].dt.floor("D").nunique()),
        }
    missing_rates = panel[feature_columns].isna().mean().sort_values(
        ascending=False)
    constant_train = []
    train = panel.loc[panel["split"].eq("train")]
    for name in feature_columns:
        if train[name].dropna().nunique() <= 1:
            constant_train.append(name)
    critical = {
        "duplicate_obs_id": int(panel["obs_id"].duplicated().sum()),
        "clock_violations": clock_violations,
        "unknown_split_rows": int(
            (~panel["split"].isin(["train", "calibration", "test", "purged"])).sum()),
        "label_columns_in_features": sorted(
            set(feature_columns) & set(_outcome_columns())),
    }
    passed = (
        critical["duplicate_obs_id"] == 0
        and critical["clock_violations"] == 0
        and critical["unknown_split_rows"] == 0
        and not critical["label_columns_in_features"]
    )
    return {
        "status": "PASSED" if passed else "NOT_MET",
        "grain": "one symbol x one hourly observation",
        "rows": int(len(panel)),
        "unique_observations": int(panel["obs_id"].nunique()),
        "feature_columns": len(feature_columns),
        "complete_six_candidate_rows": int(complete.sum()),
        "complete_six_candidate_rate": float(complete.mean()),
        "critical_checks": critical,
        "split_ranges": split_ranges,
        "highest_feature_missing_rates": [
            {"feature": str(name), "missing_rate": float(rate)}
            for name, rate in missing_rates.head(10).items()
        ],
        "constant_train_features": constant_train,
        "missing_value_contract": "train median imputation before binning",
    }


def _expand_candidates(
    panel: pd.DataFrame,
    feature_columns: list[str],
    asset_classes: Iterable[str],
) -> tuple[pd.DataFrame, list[str]]:
    pieces = []
    assets = tuple(asset_classes)
    for timeframe in TIMEFRAMES:
        for side in SIDES:
            piece = panel[[
                "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol",
                "asset_class", "rule_direction", "split", *feature_columns,
            ]].copy()
            piece["horizon"] = timeframe
            piece["side"] = side
            piece["success"] = panel[
                f"{timeframe}_{side}_success"].to_numpy(dtype=float)
            directional_return, _side_specific = _candidate_forward_return(
                panel, timeframe, side)
            piece["signed_return_after_cost"] = (
                directional_return - COST_HURDLE)
            piece["candidate_is_long"] = 1.0 if side == "long" else 0.0
            for candidate_tf in TIMEFRAMES:
                piece[f"candidate_horizon={candidate_tf}"] = (
                    1.0 if timeframe == candidate_tf else 0.0)
            rule = piece["rule_direction"].astype(str)
            piece["candidate_rule_agreement"] = rule.eq(side).astype(float)
            piece["candidate_rule_opposition"] = (
                rule.isin(SIDES) & ~rule.eq(side)).astype(float)
            for name in DIRECTIONAL_FEATURES:
                if name in piece.columns:
                    piece[f"directional__{name}"] = direction * piece[name]
            for asset in assets:
                piece[f"asset_class={asset}"] = (
                    piece["asset_class"].astype(str).eq(asset).astype(float))
            piece["asset_class=other"] = (
                ~piece["asset_class"].astype(str).isin(assets)).astype(float)
            pieces.append(piece)
    candidates = pd.concat(pieces, ignore_index=True)
    candidate_features = [
        *feature_columns,
        "candidate_is_long",
        *[f"candidate_horizon={timeframe}" for timeframe in TIMEFRAMES],
        "candidate_rule_agreement", "candidate_rule_opposition",
        *[
            f"directional__{name}" for name in DIRECTIONAL_FEATURES
            if name in feature_columns
        ],
        *[f"asset_class={asset}" for asset in assets],
        "asset_class=other",
    ]
    return candidates, candidate_features


@dataclass(frozen=True)
class BinSpec:
    medians: dict[str, float]
    edges: dict[str, list[float]]
    feature_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "medians": self.medians,
            "edges": self.edges,
            "feature_names": list(self.feature_names),
        }


def _fit_bin_spec(
    frame: pd.DataFrame,
    feature_names: list[str],
    *,
    max_bins: int = 24,
) -> BinSpec:
    medians: dict[str, float] = {}
    edges: dict[str, list[float]] = {}
    quantiles = np.linspace(0, 1, max_bins + 1)[1:-1]
    for name in feature_names:
        values = pd.to_numeric(frame[name], errors="coerce").replace(
            [np.inf, -np.inf], np.nan)
        median = float(values.median()) if values.notna().any() else 0.0
        filled = values.fillna(median).to_numpy(dtype=float)
        cut = np.unique(np.quantile(filled, quantiles)) if len(filled) else np.array([])
        medians[name] = median
        edges[name] = [float(value) for value in cut if math.isfinite(value)]
    return BinSpec(medians, edges, tuple(feature_names))


def _transform_bins(frame: pd.DataFrame, spec: BinSpec) -> np.ndarray:
    output = np.empty((len(frame), len(spec.feature_names)), dtype=np.uint8)
    for index, name in enumerate(spec.feature_names):
        values = pd.to_numeric(frame[name], errors="coerce").replace(
            [np.inf, -np.inf], np.nan).fillna(spec.medians[name])
        output[:, index] = np.searchsorted(
            np.asarray(spec.edges[name], dtype=float),
            values.to_numpy(dtype=float), side="right",
        ).astype(np.uint8)
    return output


@dataclass
class TreeNode:
    value: float
    feature: int | None = None
    threshold: int | None = None
    left: "TreeNode | None" = None
    right: "TreeNode | None" = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"value": self.value}
        if self.feature is not None:
            payload.update({
                "feature": self.feature,
                "threshold": self.threshold,
                "left": self.left.to_dict() if self.left else None,
                "right": self.right.to_dict() if self.right else None,
            })
        return payload


def _leaf_value(
    gradient: np.ndarray,
    hessian: np.ndarray,
    indices: np.ndarray,
    l2: float,
) -> float:
    if not len(indices):
        return 0.0
    value = -float(gradient[indices].sum()) / (
        float(hessian[indices].sum()) + l2)
    return float(np.clip(value, -5.0, 5.0))


def _best_split(
    bins: np.ndarray,
    gradient: np.ndarray,
    hessian: np.ndarray,
    indices: np.ndarray,
    *,
    min_leaf: int,
    l2: float,
) -> tuple[int, int, float] | None:
    if len(indices) < 2 * min_leaf:
        return None
    total_g = float(gradient[indices].sum())
    total_h = float(hessian[indices].sum())
    parent = total_g * total_g / (total_h + l2)
    best: tuple[int, int, float] | None = None
    for feature in range(bins.shape[1]):
        values = bins[indices, feature]
        bin_count = int(values.max()) + 1 if len(values) else 0
        if bin_count <= 1:
            continue
        counts = np.bincount(values, minlength=bin_count)
        grad = np.bincount(
            values, weights=gradient[indices], minlength=bin_count)
        hess = np.bincount(
            values, weights=hessian[indices], minlength=bin_count)
        left_n = np.cumsum(counts)[:-1]
        right_n = len(indices) - left_n
        valid = (left_n >= min_leaf) & (right_n >= min_leaf)
        if not valid.any():
            continue
        left_g = np.cumsum(grad)[:-1]
        left_h = np.cumsum(hess)[:-1]
        right_g = total_g - left_g
        right_h = total_h - left_h
        gains = 0.5 * (
            left_g * left_g / (left_h + l2)
            + right_g * right_g / (right_h + l2)
            - parent
        )
        gains[~valid] = -np.inf
        threshold = int(np.argmax(gains))
        gain = float(gains[threshold])
        candidate = (feature, threshold, gain)
        if best is None or (gain, -feature, -threshold) > (
            best[2], -best[0], -best[1]
        ):
            best = candidate
    if best is None or not math.isfinite(best[2]) or best[2] <= 1e-10:
        return None
    return best


def _fit_tree(
    bins: np.ndarray,
    gradient: np.ndarray,
    hessian: np.ndarray,
    indices: np.ndarray,
    *,
    depth: int,
    min_leaf: int,
    l2: float,
) -> TreeNode:
    value = _leaf_value(gradient, hessian, indices, l2)
    if depth <= 0:
        return TreeNode(value=value)
    split = _best_split(
        bins, gradient, hessian, indices,
        min_leaf=min_leaf, l2=l2,
    )
    if split is None:
        return TreeNode(value=value)
    feature, threshold, _gain = split
    left_mask = bins[indices, feature] <= threshold
    left_indices = indices[left_mask]
    right_indices = indices[~left_mask]
    return TreeNode(
        value=value,
        feature=feature,
        threshold=threshold,
        left=_fit_tree(
            bins, gradient, hessian, left_indices,
            depth=depth - 1, min_leaf=min_leaf, l2=l2,
        ),
        right=_fit_tree(
            bins, gradient, hessian, right_indices,
            depth=depth - 1, min_leaf=min_leaf, l2=l2,
        ),
    )


def _predict_tree(node: TreeNode, bins: np.ndarray) -> np.ndarray:
    output = np.full(len(bins), node.value, dtype=float)
    if node.feature is None or node.left is None or node.right is None:
        return output
    left_mask = bins[:, node.feature] <= int(node.threshold)
    if left_mask.any():
        output[left_mask] = _predict_tree(node.left, bins[left_mask])
    if (~left_mask).any():
        output[~left_mask] = _predict_tree(node.right, bins[~left_mask])
    return output


@dataclass
class GBDTModel:
    base_score: float
    learning_rate: float
    depth: int
    min_leaf: int
    l2: float
    trees: list[TreeNode]

    def raw_score(self, bins: np.ndarray, rounds: int | None = None) -> np.ndarray:
        output = np.full(len(bins), self.base_score, dtype=float)
        selected = self.trees if rounds is None else self.trees[:rounds]
        for tree in selected:
            output += self.learning_rate * _predict_tree(tree, bins)
        return output


def _fit_gbdt(
    bins: np.ndarray,
    outcome: np.ndarray,
    *,
    depth: int,
    rounds: int = 80,
    learning_rate: float = 0.08,
    min_leaf: int = 180,
    l2: float = 20.0,
) -> GBDTModel:
    mean = float(np.clip(outcome.mean(), 1e-6, 1 - 1e-6))
    base = math.log(mean / (1.0 - mean))
    score = np.full(len(outcome), base, dtype=float)
    trees: list[TreeNode] = []
    indices = np.arange(len(outcome), dtype=np.int64)
    for _ in range(rounds):
        probability = calibration._sigmoid(score)
        gradient = probability - outcome
        hessian = np.clip(probability * (1.0 - probability), 1e-6, None)
        tree = _fit_tree(
            bins, gradient, hessian, indices,
            depth=depth, min_leaf=min_leaf, l2=l2,
        )
        trees.append(tree)
        score += learning_rate * _predict_tree(tree, bins)
    return GBDTModel(base, learning_rate, depth, min_leaf, l2, trees)


def _candidate_keys() -> tuple[tuple[str, str], ...]:
    return tuple(
        (timeframe, side)
        for timeframe in TIMEFRAMES
        for side in SIDES
    )


def _candidate_key_name(key: tuple[str, str]) -> str:
    return f"{key[0]}__{key[1]}"


def _candidate_mask(
    frame: pd.DataFrame,
    key: tuple[str, str],
) -> np.ndarray:
    return (
        frame["horizon"].eq(key[0]) & frame["side"].eq(key[1])
    ).to_numpy(dtype=bool)


def _fit_per_candidate_models(
    frame: pd.DataFrame,
    bins: np.ndarray,
    outcome: np.ndarray,
    *,
    depth: int,
) -> dict[str, GBDTModel]:
    models: dict[str, GBDTModel] = {}
    for key in _candidate_keys():
        mask = _candidate_mask(frame, key)
        if not mask.any():
            raise ValueError(
                f"training candidates missing {_candidate_key_name(key)}")
        models[_candidate_key_name(key)] = _fit_gbdt(
            bins[mask], outcome[mask], depth=depth)
    return models


def _fit_per_candidate_calibrators(
    frame: pd.DataFrame,
    bins: np.ndarray,
    models: dict[str, GBDTModel],
    outcome: np.ndarray,
    *,
    rounds: int,
) -> tuple[np.ndarray, dict[str, tuple[float, float]]]:
    probability = np.empty(len(frame), dtype=float)
    calibrators: dict[str, tuple[float, float]] = {}
    for key in _candidate_keys():
        name = _candidate_key_name(key)
        mask = _candidate_mask(frame, key)
        raw = calibration._sigmoid(
            models[name].raw_score(bins[mask], rounds=rounds))
        params = calibration.fit_platt(raw, outcome[mask])
        calibrators[name] = params
        probability[mask] = calibration.apply_platt(raw, params)
    return probability, calibrators


def _apply_per_candidate_models(
    frame: pd.DataFrame,
    bins: np.ndarray,
    models: dict[str, GBDTModel],
    calibrators: dict[str, tuple[float, float]],
    *,
    rounds: int,
) -> np.ndarray:
    probability = np.empty(len(frame), dtype=float)
    for key in _candidate_keys():
        name = _candidate_key_name(key)
        mask = _candidate_mask(frame, key)
        raw = calibration._sigmoid(
            models[name].raw_score(bins[mask], rounds=rounds))
        probability[mask] = calibration.apply_platt(
            raw, calibrators[name])
    return probability


def _best_candidates(
    candidate_rows: pd.DataFrame,
    probability: np.ndarray,
) -> pd.DataFrame:
    selected = candidate_rows[[
        "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol",
        "horizon", "side", "success", "signed_return_after_cost",
    ]].copy()
    selected["probability"] = probability
    counts = selected.groupby("obs_id")["obs_id"].transform("size")
    selected = selected.loc[counts.eq(6)].copy()
    selected.sort_values(
        ["obs_id", "probability", "horizon", "side"],
        ascending=[True, False, True, True], inplace=True,
    )
    return selected.drop_duplicates("obs_id", keep="first").reset_index(drop=True)


def _selection_metrics(
    best: pd.DataFrame,
    threshold: float,
) -> dict[str, Any]:
    selected = best.loc[best["probability"].ge(threshold)].copy()
    n = int(len(selected))
    successes = int(selected["success"].sum()) if n else 0
    low, high = calibration._wilson(successes, n)
    return {
        "threshold": float(threshold),
        "n": n,
        "successes": successes,
        "precision": float(successes / n) if n else None,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "ece": calibration._ece(
            selected["probability"].to_numpy(dtype=float),
            selected["success"].to_numpy(dtype=float),
        ) if n else None,
        "coverage_rate": float(n / len(best)) if len(best) else 0.0,
        "distinct_days": int(
            selected["obs_ts"].dt.floor("D").nunique()) if n else 0,
        "distinct_cycles": int(selected["obs_ts"].nunique()) if n else 0,
        "distinct_symbols": int(selected["symbol"].nunique()) if n else 0,
        "mean_signed_return_after_cost": float(
            selected["signed_return_after_cost"].mean()) if n else None,
        "long_n": int(selected["side"].eq("long").sum()) if n else 0,
        "short_n": int(selected["side"].eq("short").sum()) if n else 0,
        "horizon_counts": {
            str(key): int(value)
            for key, value in selected["horizon"].value_counts().items()
        } if n else {},
    }


def _fixed_coverage_metrics(best: pd.DataFrame) -> list[dict[str, Any]]:
    ranked = best.sort_values("probability", ascending=False).reset_index(drop=True)
    output = []
    for coverage in (0.10, 0.20, 0.50, 1.00):
        count = max(1, int(math.ceil(len(ranked) * coverage)))
        subset = ranked.iloc[:count]
        threshold = float(subset["probability"].min())
        output.append({"requested_coverage": coverage,
                       **_selection_metrics(best, threshold)})
    return output


def _choose_threshold(
    best: pd.DataFrame,
    *,
    minimum_n: int = 100,
    target_precision: float = 0.90,
) -> tuple[dict[str, Any], pd.DataFrame]:
    ranked = best.sort_values(
        ["probability", "obs_id"], ascending=[False, True]).reset_index(drop=True)
    successes = ranked["success"].to_numpy(dtype=float)
    cumulative = np.cumsum(successes)
    rows = []
    for count in range(minimum_n, len(ranked) + 1):
        if count < len(ranked) and math.isclose(
            float(ranked.loc[count - 1, "probability"]),
            float(ranked.loc[count, "probability"]),
            rel_tol=0.0, abs_tol=1e-15,
        ):
            continue
        subset = ranked.iloc[:count]
        success_count = int(cumulative[count - 1])
        precision = success_count / count
        low, high = calibration._wilson(success_count, count)
        rows.append({
            "n": count,
            "successes": success_count,
            "precision": precision,
            "wilson_95_low": low,
            "wilson_95_high": high,
            "threshold": float(subset["probability"].min()),
            "distinct_days": int(subset["obs_ts"].dt.floor("D").nunique()),
            "distinct_cycles": int(subset["obs_ts"].nunique()),
            "coverage_rate": count / len(ranked),
        })
    if not rows:
        raise ValueError("threshold window cannot satisfy minimum_n")
    eligible = [
        row for row in rows
        if row["distinct_days"] >= 2 and row["distinct_cycles"] >= 20
    ]
    target = [row for row in eligible if row["precision"] >= target_precision]
    if target:
        chosen = max(target, key=lambda row: (
            row["n"], row["wilson_95_low"], row["threshold"]))
        status = "target_reached_on_threshold_window"
    else:
        pool = eligible or rows
        chosen = max(pool, key=lambda row: (
            row["wilson_95_low"], row["precision"], row["n"]))
        status = "target_not_reached_on_threshold_window"
    chosen = dict(chosen)
    chosen["selection_status"] = status
    return chosen, pd.DataFrame(rows)


def _oracle_selected(
    candidates: pd.DataFrame,
    best: pd.DataFrame,
    threshold: float,
) -> dict[str, Any]:
    selected_ids = set(best.loc[
        best["probability"].ge(threshold), "obs_id"].tolist())
    selected = candidates.loc[candidates["obs_id"].isin(selected_ids)]
    grouped = selected.groupby("obs_id")
    complete = grouped.size().eq(6)
    success = grouped["success"].max().reindex(complete[complete].index)
    return {
        "selected_complete_observations": int(len(success)),
        "any_candidate_success_rate": float(success.mean()) if len(success) else None,
    }


def _model_candidate_row(
    name: str,
    family: str,
    depth: int,
    rounds: int,
    best: pd.DataFrame,
    calibrated_probability: np.ndarray,
    outcome: np.ndarray,
    calibrators: dict[str, tuple[float, float]],
) -> dict[str, Any]:
    fixed = _fixed_coverage_metrics(best)
    top20 = next(row for row in fixed if row["requested_coverage"] == 0.20)
    return {
        "model": name,
        "family": family,
        "depth": depth,
        "rounds": rounds,
        "candidate_brier": float(np.mean(
            (calibrated_probability - outcome) ** 2)),
        "top_candidate_precision": float(best["success"].mean()),
        "top20_precision": top20["precision"],
        "top20_wilson_low": top20["wilson_95_low"],
        "top20_n": top20["n"],
        "calibrators": {
            key: {"intercept": params[0], "slope": params[1]}
            for key, params in calibrators.items()
        },
        "fixed_coverage": fixed,
    }


def diagnose(
    panel_path: Path,
    *,
    minimum_n: int = 100,
) -> tuple[
    dict[str, Any], dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame,
]:
    panel = _load_panel(panel_path)
    feature_columns = _feature_columns(panel)
    complete = _complete_outcome_mask(panel)
    tuning, threshold_mask, confirmation, nested_contract = (
        _calibration_subsplits(panel))
    quality = _quality_audit(
        panel, feature_columns, complete,
        (tuning, threshold_mask, confirmation),
    )
    if quality["status"] != "PASSED":
        raise ValueError("research panel failed critical data-quality checks")
    usable = panel.loc[complete].copy().reset_index(drop=True)
    train_assets = (
        usable.loc[usable["split"].eq("train"), "asset_class"]
        .fillna("unknown").astype(str).value_counts()
    )
    asset_classes = tuple(sorted(train_assets[train_assets >= 20].index))
    candidates, candidate_features = _expand_candidates(
        usable, feature_columns, asset_classes)
    train_candidates = candidates.loc[candidates["split"].eq("train")].copy()
    spec = _fit_bin_spec(train_candidates, candidate_features)

    masks = {
        "model_selection": candidates["obs_id"].isin(
            panel.loc[tuning & complete, "obs_id"]),
        "threshold_selection": candidates["obs_id"].isin(
            panel.loc[threshold_mask & complete, "obs_id"]),
        "internal_confirmation": candidates["obs_id"].isin(
            panel.loc[confirmation & complete, "obs_id"]),
        "historical_holdout": candidates["split"].eq("test"),
    }
    bins_by_split = {
        name: _transform_bins(candidates.loc[mask], spec)
        for name, mask in masks.items()
    }
    train_bins = _transform_bins(train_candidates, spec)
    train_y = train_candidates["success"].to_numpy(dtype=float)
    shared_models = {
        depth: _fit_gbdt(train_bins, train_y, depth=depth)
        for depth in (1, 2)
    }

    candidate_rows: list[dict[str, Any]] = []
    fitted: dict[str, dict[str, Any]] = {}
    tuning_candidates = candidates.loc[masks["model_selection"]].reset_index(drop=True)
    tuning_y = tuning_candidates["success"].to_numpy(dtype=float)
    for depth, model in shared_models.items():
        for rounds in (40, 80):
            raw_score = model.raw_score(
                bins_by_split["model_selection"], rounds=rounds)
            raw_probability = calibration._sigmoid(raw_score)
            platt = calibration.fit_platt(raw_probability, tuning_y)
            probability = calibration.apply_platt(raw_probability, platt)
            best = _best_candidates(tuning_candidates, probability)
            name = f"shared_hist_gbdt_depth{depth}_rounds{rounds}"
            row = _model_candidate_row(
                name, "shared", depth, rounds, best, probability, tuning_y,
                {"shared": platt})
            candidate_rows.append(row)
            fitted[name] = {
                "family": "shared",
                "models": {"shared": model},
                "rounds": rounds,
                "calibrators": {"shared": platt},
            }

    per_candidate_models = {
        depth: _fit_per_candidate_models(
            train_candidates, train_bins, train_y, depth=depth)
        for depth in (1, 2)
    }
    for depth, models in per_candidate_models.items():
        for rounds in (40, 80):
            probability, calibrators = _fit_per_candidate_calibrators(
                tuning_candidates,
                bins_by_split["model_selection"],
                models,
                tuning_y,
                rounds=rounds,
            )
            best = _best_candidates(tuning_candidates, probability)
            name = f"per_candidate_hist_gbdt_depth{depth}_rounds{rounds}"
            row = _model_candidate_row(
                name, "per_candidate", depth, rounds, best, probability,
                tuning_y, calibrators)
            candidate_rows.append(row)
            fitted[name] = {
                "family": "per_candidate",
                "models": models,
                "rounds": rounds,
                "calibrators": calibrators,
            }
    chosen = max(candidate_rows, key=lambda row: (
        row["top20_wilson_low"], row["top20_precision"],
        row["top_candidate_precision"], row["family"] == "shared",
        -row["depth"], -row["rounds"],
    ))
    selected_fit = fitted[chosen["model"]]
    family = str(selected_fit["family"])
    selected_models: dict[str, GBDTModel] = selected_fit["models"]
    rounds = int(selected_fit["rounds"])
    selected_calibrators: dict[str, tuple[float, float]] = (
        selected_fit["calibrators"])

    best_by_split: dict[str, pd.DataFrame] = {}
    for name, mask in masks.items():
        subset = candidates.loc[mask].reset_index(drop=True)
        if family == "shared":
            raw = calibration._sigmoid(
                selected_models["shared"].raw_score(
                    bins_by_split[name], rounds=rounds))
            probability = calibration.apply_platt(
                raw, selected_calibrators["shared"])
        else:
            probability = _apply_per_candidate_models(
                subset,
                bins_by_split[name],
                selected_models,
                selected_calibrators,
                rounds=rounds,
            )
        best_by_split[name] = _best_candidates(subset, probability)

    threshold_choice, threshold_curve = _choose_threshold(
        best_by_split["threshold_selection"], minimum_n=minimum_n)
    threshold = float(threshold_choice["threshold"])
    split_metrics = {
        name: _selection_metrics(best, threshold)
        for name, best in best_by_split.items()
    }
    oracles = {
        name: _oracle_selected(
            candidates.loc[masks[name]], best_by_split[name], threshold)
        for name in masks
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
    internal_candidate = all(
        requirements[name] for name in (
            "threshold_window_reached_90pct",
            "confirmation_precision_at_least_90pct",
            "confirmation_n_at_least_100",
            "confirmation_ece_at_most_5pp",
        )
    )
    payload = {
        "schema_version": 1,
        "artifact_type": "selective_multitimeframe_diagnostic",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_research",
        "input_panel": str(panel_path),
        "data_quality": quality,
        "point_in_time_contract": {
            "features": "exported observation-grain panel; no outcome columns in features",
            "candidate_set": "exactly 3 horizons x 2 directions per complete observation",
            "nested_calibration": nested_contract,
            "historical_holdout": "already inspected; retrospective diagnostic only",
            "cost_hurdle_bps": COST_HURDLE * 10_000,
        },
        "model_family": {
            "algorithm": "deterministic histogram gradient-boosted Newton trees",
            "external_ml_dependencies": 0,
            "candidate_features": len(candidate_features),
            "asset_classes_from_train_only": list(asset_classes),
            "candidate_models": candidate_rows,
            "selection_rule": "highest model-selection top-20pct Wilson lower bound",
            "selected_model": chosen["model"],
            "selected_family": family,
            "selected_rounds": rounds,
            "selected_depth": int(chosen["depth"]),
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
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "orders_placed": 0,
    }
    model_payload = {
        "schema_version": 1,
        "research_only": True,
        "selected_model": chosen["model"],
        "family": family,
        "rounds": rounds,
        "depth": int(chosen["depth"]),
        "threshold": threshold,
        "bin_spec": spec.to_dict(),
        "models": {
            name: {
                "learning_rate": selected_model.learning_rate,
                "min_leaf": selected_model.min_leaf,
                "l2": selected_model.l2,
                "base_score": selected_model.base_score,
                "platt_intercept": selected_calibrators[name][0],
                "platt_slope": selected_calibrators[name][1],
                "trees": [
                    tree.to_dict()
                    for tree in selected_model.trees[:rounds]
                ],
            }
            for name, selected_model in selected_models.items()
        },
        "production_threshold_change_allowed": False,
    }
    holdout_selected = best_by_split["historical_holdout"].loc[
        best_by_split["historical_holdout"]["probability"].ge(threshold)
    ].copy()
    return (
        payload,
        model_payload,
        pd.DataFrame(candidate_rows),
        threshold_curve,
        holdout_selected,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--panel", type=Path, default=DEFAULT_INPUT / "research_panel.csv")
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--candidates-out", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--curve-out", type=Path, default=DEFAULT_CURVE)
    parser.add_argument("--selected-out", type=Path, default=DEFAULT_SELECTED)
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
    except (OSError, ValueError, KeyError, pd.errors.ParserError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "ok": True,
        "json_out": str(args.json_out),
        "selected_model": payload["model_family"]["selected_model"],
        "threshold_status": payload["threshold_selection"]["selection_status"],
        "confirmation_precision": payload["evaluation"]["internal_confirmation"]["precision"],
        "historical_holdout_precision": payload["evaluation"]["historical_holdout"]["precision"],
        "acceptance": payload["acceptance"]["internal_candidate_status"],
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
