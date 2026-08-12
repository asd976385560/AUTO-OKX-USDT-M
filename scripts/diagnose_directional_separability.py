#!/usr/bin/env python3
"""Diagnose whether frozen candidate scores can rank trade direction.

The six 15m/1H/4H long/short candidates make the any-candidate oracle look
high whenever price moves enough in either direction.  This research-only
diagnostic separates that volatility opportunity from the deployable task:
choosing the correct side and horizon without future labels.

A small, predeclared family of probability-margin policies is selected only on
the model-selection window.  The chosen policy is then applied unchanged to
the threshold-selection, internal-confirmation, and already-inspected
historical-holdout windows.  No result authorizes production trading; a new
future-only shadow window is always required.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

import offline_multitimeframe_calibration as calibration


ROOT = Path(r".")
DEFAULT_INPUT = (
    ROOT / "reports" / "quality" / "goal-selective-multitimeframe-v1-20260812"
)
DEFAULT_OUTPUT = (
    ROOT / "reports" / "quality" / "goal-directional-separability-v1-20260812"
)
HORIZONS = ("15m", "1H", "4H")
SIDES = ("long", "short")
CANDIDATE_KEYS = frozenset(
    f"{horizon}_{side}" for horizon in HORIZONS for side in SIDES)
COVERAGES = (0.10, 0.20, 0.50, 1.00)


@dataclass(frozen=True)
class Policy:
    name: str
    score: str
    filter_name: str
    rationale: str


POLICIES = (
    Policy("probability_all", "top_probability", "all",
           "absolute top probability baseline"),
    Policy("global_margin_all", "top_vs_runner_up_margin", "all",
           "separation from the second-ranked candidate"),
    Policy("direction_margin_all", "selected_vs_opposite_margin", "all",
           "selected side versus the opposite side at the same horizon"),
    Policy("mean_direction_margin_all", "selected_side_mean_margin", "all",
           "mean selected-side edge across all three horizons"),
    Policy("direction_margin_unanimous", "selected_vs_opposite_margin", "unanimous",
           "same model side wins at all three horizons"),
    Policy("direction_margin_rule", "selected_vs_opposite_margin", "rule_agreement",
           "model side agrees with the deterministic multi-timeframe rule"),
    Policy("direction_margin_flow", "selected_vs_opposite_margin", "flow_agreement",
           "model side agrees with taker imbalance and CVD"),
    Policy(
        "direction_margin_unanimous_rule",
        "selected_vs_opposite_margin",
        "unanimous_rule_agreement",
        "all model horizons agree and deterministic rule agrees",
    ),
)


def _read_candidates(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    frame = pd.read_csv(path)
    required = {
        "obs_id", "obs_ts", "decision_ts", "symbol", "horizon", "side",
        "probability", "success", "signed_return_after_cost", "rule_direction",
        "aligned_timeframes", "taker_buy_centered", "cvd_share",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{path.name} missing columns: {','.join(missing)}")
    frame["obs_ts"] = pd.to_datetime(frame["obs_ts"], utc=True, errors="raise")
    frame["decision_ts"] = pd.to_datetime(
        frame["decision_ts"], utc=True, errors="raise")
    for column in (
        "probability", "success", "signed_return_after_cost",
        "aligned_timeframes", "taker_buy_centered", "cvd_share",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["candidate_key"] = frame["horizon"].astype(str) + "_" + frame["side"].astype(str)
    duplicate_rows = int(frame.duplicated(["obs_id", "candidate_key"]).sum())
    invalid_probability_rows = int((
        frame["probability"].isna()
        | ~frame["probability"].between(0.0, 1.0)
    ).sum())
    clock_violations = int(frame["decision_ts"].lt(frame["obs_ts"]).sum())
    valid_observations: list[str] = []
    for obs_id, group in frame.groupby("obs_id", sort=False):
        if (
            len(group) == 6
            and set(group["candidate_key"]) == CANDIDATE_KEYS
            and group[[
                "probability", "success", "signed_return_after_cost",
            ]].notna().all(axis=None)
        ):
            valid_observations.append(str(obs_id))
    total_observations = int(frame["obs_id"].nunique())
    complete = frame[frame["obs_id"].astype(str).isin(valid_observations)].copy()
    audit = {
        "file": str(path.resolve()),
        "candidate_rows": int(len(frame)),
        "observations": total_observations,
        "complete_six_candidate_observations": len(valid_observations),
        "complete_six_candidate_rate": (
            len(valid_observations) / total_observations
            if total_observations else None),
        "duplicate_observation_candidate_rows": duplicate_rows,
        "invalid_probability_rows": invalid_probability_rows,
        "clock_violations": clock_violations,
        "status": (
            "PASSED"
            if duplicate_rows == invalid_probability_rows == clock_violations == 0
            else "FAILED"
        ),
    }
    if audit["status"] != "PASSED":
        raise ValueError(f"candidate data quality failed: {audit}")
    return complete, audit


def _aggregate(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for obs_id, group in frame.groupby("obs_id", sort=False):
        indexed = group.set_index("candidate_key")
        if not indexed.index.is_unique:
            raise ValueError(f"duplicate candidate key in observation {obs_id}")
        ranked = group.sort_values(
            ["probability", "candidate_key"], ascending=[False, True])
        top = ranked.iloc[0]
        runner_up = ranked.iloc[1]
        side = str(top["side"])
        opposite = "short" if side == "long" else "long"
        horizon = str(top["horizon"])
        side_edges = [
            float(indexed.loc[f"{candidate_horizon}_{side}", "probability"])
            - float(indexed.loc[f"{candidate_horizon}_{opposite}", "probability"])
            for candidate_horizon in HORIZONS
        ]
        obs_ts = pd.Timestamp(top["obs_ts"])
        taker = float(top["taker_buy_centered"])
        cvd = float(top["cvd_share"])
        flow_agreement = (
            (side == "long" and taker > 0 and cvd > 0)
            or (side == "short" and taker < 0 and cvd < 0)
        )
        rows.append({
            "obs_id": str(obs_id),
            "obs_ts": obs_ts,
            "cycle_id_utc": obs_ts.floor("h").strftime("%Y-%m-%dT%H:00Z"),
            "symbol": str(top["symbol"]),
            "horizon": horizon,
            "side": side,
            "success": int(float(top["success"]) > 0),
            "signed_return_after_cost": float(top["signed_return_after_cost"]),
            "any_candidate_success": int(group["success"].max() > 0),
            "top_probability": float(top["probability"]),
            "runner_up_probability": float(runner_up["probability"]),
            "top_vs_runner_up_margin": (
                float(top["probability"]) - float(runner_up["probability"])),
            "opposite_side_same_horizon_probability": float(
                indexed.loc[f"{horizon}_{opposite}", "probability"]),
            "selected_vs_opposite_margin": (
                float(top["probability"])
                - float(indexed.loc[f"{horizon}_{opposite}", "probability"])),
            "selected_side_horizon_votes": int(sum(edge > 0 for edge in side_edges)),
            "selected_side_unanimous": bool(all(edge > 0 for edge in side_edges)),
            "selected_side_mean_margin": float(np.mean(side_edges)),
            "selected_side_min_margin": float(np.min(side_edges)),
            "rule_agreement": str(top["rule_direction"]) == side,
            "flow_agreement": bool(flow_agreement),
            "aligned_timeframes": int(float(top["aligned_timeframes"])),
            "asset_class": str(top.get("asset_class") or "unknown"),
        })
    return pd.DataFrame(rows)


def _filter(frame: pd.DataFrame, name: str) -> pd.Series:
    if name == "all":
        return pd.Series(True, index=frame.index)
    if name == "unanimous":
        return frame["selected_side_unanimous"].astype(bool)
    if name == "rule_agreement":
        return frame["rule_agreement"].astype(bool)
    if name == "flow_agreement":
        return frame["flow_agreement"].astype(bool)
    if name == "unanimous_rule_agreement":
        return (
            frame["selected_side_unanimous"].astype(bool)
            & frame["rule_agreement"].astype(bool)
        )
    raise ValueError(f"unknown filter: {name}")


def _select_cycle_local(
    frame: pd.DataFrame,
    policy: Policy,
    requested_coverage: float,
) -> pd.DataFrame:
    eligible = frame.loc[_filter(frame, policy.filter_name)].copy()
    if eligible.empty:
        return eligible
    selected: list[pd.DataFrame] = []
    for _cycle, group in eligible.groupby("cycle_id_utc", sort=True):
        count = max(1, int(math.ceil(len(group) * requested_coverage)))
        selected.append(group.sort_values(
            [policy.score, "symbol"], ascending=[False, True]).head(count))
    return pd.concat(selected, ignore_index=True) if selected else eligible.iloc[0:0]


def _metrics(selected: pd.DataFrame, population: int) -> dict[str, Any]:
    n = int(len(selected))
    successes = int(selected["success"].sum()) if n else 0
    precision = successes / n if n else None
    low, high = calibration._wilson(successes, n)
    oracle_opportunities = int(selected["any_candidate_success"].sum()) if n else 0
    return {
        "n": n,
        "successes": successes,
        "precision": precision,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "coverage_rate": n / population if population else None,
        "distinct_days": int(selected["obs_ts"].dt.date.nunique()) if n else 0,
        "distinct_cycles": int(selected["cycle_id_utc"].nunique()) if n else 0,
        "distinct_symbols": int(selected["symbol"].nunique()) if n else 0,
        "mean_signed_return_after_cost": (
            float(selected["signed_return_after_cost"].mean()) if n else None),
        "selected_subset_any_candidate_success_rate": (
            float(selected["any_candidate_success"].mean()) if n else None),
        "oracle_success_opportunities": oracle_opportunities,
        "captured_oracle_opportunities": successes,
        "capture_rate_when_any_candidate_succeeds": (
            successes / oracle_opportunities if oracle_opportunities else None),
        "side_counts": (
            {str(k): int(v) for k, v in selected["side"].value_counts().sort_index().items()}
            if n else {}),
        "horizon_counts": (
            {str(k): int(v) for k, v in selected["horizon"].value_counts().sort_index().items()}
            if n else {}),
    }


def _split_frame(
    frame: pd.DataFrame,
    ranges: dict[str, Any],
    split: str,
) -> pd.DataFrame:
    item = ranges[split]
    start = pd.Timestamp(item["start_utc"])
    end = pd.Timestamp(item["end_utc"])
    return frame.loc[frame["obs_ts"].between(start, end, inclusive="both")].copy()


def _candidate_rows(
    model_selection: pd.DataFrame,
    minimum_n: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for policy in POLICIES:
        for coverage in COVERAGES:
            selected = _select_cycle_local(model_selection, policy, coverage)
            metrics = _metrics(selected, len(model_selection))
            rows.append({
                "policy": policy.name,
                "score": policy.score,
                "filter": policy.filter_name,
                "rationale": policy.rationale,
                "requested_cycle_local_coverage": coverage,
                **metrics,
                "minimum_n_met": metrics["n"] >= minimum_n,
            })
    return rows


def _choose(rows: list[dict[str, Any]], minimum_n: int) -> dict[str, Any]:
    eligible = [row for row in rows if row["n"] >= minimum_n]
    if not eligible:
        raise ValueError("no directional policy meets minimum_n")
    order = {policy.name: index for index, policy in enumerate(POLICIES)}
    return max(eligible, key=lambda row: (
        row["wilson_95_low"] if row["wilson_95_low"] is not None else -1.0,
        row["precision"] if row["precision"] is not None else -1.0,
        row["n"],
        -order[row["policy"]],
        -COVERAGES.index(row["requested_cycle_local_coverage"]),
    ))


def _add_cross_window_diagnostics(
    rows: list[dict[str, Any]],
    windows: dict[str, pd.DataFrame],
) -> None:
    policies = {policy.name: policy for policy in POLICIES}
    for row in rows:
        policy = policies[str(row["policy"])]
        coverage = float(row["requested_cycle_local_coverage"])
        for split, observations in windows.items():
            metrics = _metrics(
                _select_cycle_local(observations, policy, coverage),
                len(observations),
            )
            for field in (
                "n", "successes", "precision", "wilson_95_low",
                "wilson_95_high", "coverage_rate",
                "mean_signed_return_after_cost",
            ):
                row[f"{split}_{field}"] = metrics[field]


def _posthoc_family_ceiling(
    rows: list[dict[str, Any]],
    split: str,
    minimum_n: int,
) -> dict[str, Any]:
    eligible = [
        row for row in rows if int(row[f"{split}_n"]) >= minimum_n
    ]
    if not eligible:
        return {"status": "NO_POLICY_AT_MINIMUM_N"}
    best = max(eligible, key=lambda row: (
        float(row[f"{split}_precision"]),
        float(row[f"{split}_wilson_95_low"]),
        int(row[f"{split}_n"]),
    ))
    return {
        "status": "POSTHOC_DIAGNOSTIC_ONLY",
        "policy": best["policy"],
        "score": best["score"],
        "filter": best["filter"],
        "requested_cycle_local_coverage": best[
            "requested_cycle_local_coverage"],
        "n": int(best[f"{split}_n"]),
        "precision": float(best[f"{split}_precision"]),
        "wilson_95_low": float(best[f"{split}_wilson_95_low"]),
        "mean_signed_return_after_cost": float(
            best[f"{split}_mean_signed_return_after_cost"]),
        "interpretation": (
            "maximum inspected precision inside the fixed family; it is not a "
            "valid selection result for this window"),
    }


def _label_profile(candidates: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {
        "observations": int(candidates["obs_id"].nunique()),
        "any_candidate_success_rate": float(
            candidates.groupby("obs_id")["success"].max().mean()),
        "candidate_success_rates": {},
    }
    for (horizon, side), group in candidates.groupby(["horizon", "side"], sort=True):
        result["candidate_success_rates"][f"{horizon}_{side}"] = float(
            group["success"].mean())
    return result


def _write_notebook(path: Path, json_path: Path, table_path: Path) -> None:
    payload = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# Directional separability diagnostic\n",
                    "Research-only. Historical windows are already inspected and cannot authorize trading.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "import json, pandas as pd\n",
                    f"payload = json.loads(open(r'{json_path.name}', encoding='utf-8').read())\n",
                    f"candidates = pd.read_csv(r'{table_path.name}')\n",
                    "payload['selected_policy'], payload['evaluation']\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "candidates.sort_values(['wilson_95_low','precision'], ascending=False).head(20)\n",
                ],
            },
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    calibration._atomic_json(path, payload)


def diagnose(
    input_dir: Path,
    *,
    minimum_n: int = 100,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    calibration_candidates, calibration_audit = _read_candidates(
        input_dir / "calibration_candidate_predictions.csv")
    holdout_candidates, holdout_audit = _read_candidates(
        input_dir / "holdout_candidate_predictions.csv")
    existing = json.loads(
        (input_dir / "selective_diagnostic.json").read_text(encoding="utf-8"))
    ranges = existing["data_quality"]["split_ranges"]

    calibration_observations = _aggregate(calibration_candidates)
    holdout_observations = _aggregate(holdout_candidates)
    split_observations = {
        split: _split_frame(calibration_observations, ranges, split)
        for split in (
            "model_selection", "threshold_selection", "internal_confirmation")
    }
    split_observations["historical_holdout"] = holdout_observations
    split_candidates = {
        split: _split_frame(calibration_candidates, ranges, split)
        for split in (
            "model_selection", "threshold_selection", "internal_confirmation")
    }
    split_candidates["historical_holdout"] = holdout_candidates

    candidate_rows = _candidate_rows(
        split_observations["model_selection"], minimum_n)
    _add_cross_window_diagnostics(candidate_rows, split_observations)
    chosen = _choose(candidate_rows, minimum_n)
    policy = next(item for item in POLICIES if item.name == chosen["policy"])
    evaluation: dict[str, Any] = {}
    selected_by_split: dict[str, pd.DataFrame] = {}
    for split, observations in split_observations.items():
        selected = _select_cycle_local(
            observations,
            policy,
            float(chosen["requested_cycle_local_coverage"]),
        )
        selected_by_split[split] = selected
        evaluation[split] = _metrics(selected, len(observations))

    profiles = {
        split: _label_profile(frame)
        for split, frame in split_candidates.items()
    }
    requirements = {
        "model_selection_wilson_95_low_at_least_90pct": (
            evaluation["model_selection"]["wilson_95_low"] or 0.0) >= 0.90,
        "threshold_selection_wilson_95_low_at_least_90pct": (
            evaluation["threshold_selection"]["wilson_95_low"] or 0.0) >= 0.90,
        "internal_confirmation_wilson_95_low_at_least_90pct": (
            evaluation["internal_confirmation"]["wilson_95_low"] or 0.0) >= 0.90,
        "internal_confirmation_n_at_least_minimum": (
            evaluation["internal_confirmation"]["n"] >= minimum_n),
        "historical_holdout_wilson_95_low_at_least_90pct": (
            evaluation["historical_holdout"]["wilson_95_low"] or 0.0) >= 0.90,
        "historical_holdout_n_at_least_minimum": (
            evaluation["historical_holdout"]["n"] >= minimum_n),
        "independent_future_shadow_window": False,
    }
    confirmed = all(
        requirements[key]
        for key in requirements
        if key != "independent_future_shadow_window"
    )
    payload = {
        "schema_version": 1,
        "artifact_type": "directional_separability_diagnostic",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "research_only_read_only",
        "diagnostic_question": (
            "Can observable probability margins choose the successful side/horizon, "
            "rather than merely identify volatile observations?"),
        "data_quality": {
            "status": (
                "PASSED" if calibration_audit["status"] == holdout_audit["status"] == "PASSED"
                else "FAILED"),
            "calibration": calibration_audit,
            "historical_holdout": holdout_audit,
            "grain": "one complete symbol-hour observation with exactly six candidates",
        },
        "selection_contract": {
            "selection_window": "model_selection only",
            "candidate_family_count": len(POLICIES) * len(COVERAGES),
            "policy_count": len(POLICIES),
            "cycle_local_coverages": list(COVERAGES),
            "minimum_n": minimum_n,
            "multiple_testing_warning": (
                "exploratory fixed family; confirmation and historical holdout are "
                "diagnostic only, never production authorization"),
        },
        "selected_policy": {
            key: chosen[key]
            for key in (
                "policy", "score", "filter", "rationale",
                "requested_cycle_local_coverage", "n", "precision",
                "wilson_95_low", "wilson_95_high",
            )
        },
        "evaluation": evaluation,
        "label_profile": profiles,
        "root_cause": {
            "interpretation": (
                "Any-candidate success primarily measures whether price moved enough "
                "in either direction; chosen-candidate precision measures the actual "
                "deployable ranking task."),
            "historical_holdout_oracle_any_candidate_success_rate": (
                profiles["historical_holdout"]["any_candidate_success_rate"]),
            "historical_holdout_selected_precision": (
                evaluation["historical_holdout"]["precision"]),
            "historical_holdout_ranking_gap": (
                profiles["historical_holdout"]["any_candidate_success_rate"]
                - (evaluation["historical_holdout"]["precision"] or 0.0)),
            "posthoc_fixed_family_precision_ceiling": {
                split: _posthoc_family_ceiling(
                    candidate_rows, split, minimum_n)
                for split in (
                    "model_selection", "threshold_selection",
                    "internal_confirmation", "historical_holdout",
                )
            },
        },
        "acceptance": {
            "target_wilson_95_lower_bound": 0.90,
            "requirements": requirements,
            "historical_candidate_status": (
                "INTERNALLY_CONFIRMED_RESEARCH_ONLY" if confirmed else "NOT_ELIGIBLE"),
            "confidence_90_status": "NOT_PROVEN",
            "production_status": "NO_CHANGE_ALLOWED",
        },
        "conclusion": (
            "A historical margin policy cannot authorize trading. Even an internally "
            "confirmed candidate requires a newly frozen future-only shadow window."),
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "production_execution_authorized": False,
        "orders_placed": 0,
    }
    table = pd.DataFrame(candidate_rows)
    holdout_selected = selected_by_split["historical_holdout"].copy()
    return payload, table, holdout_selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-n", type=int, default=100)
    args = parser.parse_args(argv)
    if args.minimum_n <= 0:
        raise SystemExit("--minimum-n must be positive")
    try:
        payload, table, holdout = diagnose(
            args.input_dir, minimum_n=args.minimum_n)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = args.output_dir / "directional_separability.json"
        table_path = args.output_dir / "model_selection_candidates.csv"
        holdout_path = args.output_dir / "historical_holdout_selected.csv"
        notebook_path = args.output_dir / "analysis.ipynb"
        calibration._atomic_json(json_path, payload)
        calibration._atomic_csv(table_path, table)
        calibration._atomic_csv(holdout_path, holdout)
        _write_notebook(notebook_path, json_path, table_path)
        selected = payload["selected_policy"]
        confirmation = payload["evaluation"]["internal_confirmation"]
        holdout_metrics = payload["evaluation"]["historical_holdout"]
        print(json.dumps({
            "ok": True,
            "json_out": str(json_path.resolve()),
            "candidate_table": str(table_path.resolve()),
            "notebook": str(notebook_path.resolve()),
            "selected_policy": selected["policy"],
            "internal_confirmation_precision": confirmation["precision"],
            "internal_confirmation_n": confirmation["n"],
            "historical_holdout_precision": holdout_metrics["precision"],
            "historical_holdout_n": holdout_metrics["n"],
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
