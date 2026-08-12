#!/usr/bin/env python3
"""Diagnose whether a pre-specified high-confidence policy survives time split.

The input CSVs are research exports from
``offline_multitimeframe_calibration.py``.  Candidate policies are fixed in
code and ranked only on the calibration window.  Exactly one selected policy
is then summarized on the historical holdout.  Because that holdout has
already been inspected by the broader model workflow, even a strong result is
exploratory and must be frozen before a new future-only shadow window.

No production database, threshold, stage, or exchange path is accessed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

import offline_multitimeframe_calibration as calibration


DEFAULT_INPUT = Path(
    r"./reports/quality/goal-multitimeframe-policy-diagnostic-v2-20260812")
DEFAULT_OUTPUT = DEFAULT_INPUT / "policy_diagnostic.json"
DEFAULT_TABLE = DEFAULT_INPUT / "calibration_policy_candidates.csv"


@dataclass(frozen=True)
class Policy:
    name: str
    rationale: str
    predicate: Callable[[pd.DataFrame], pd.Series]


def _true(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=frame.index)


def _rule_agreement(frame: pd.DataFrame) -> pd.Series:
    return frame["rule_direction"].eq(frame["side"])


def _three_aligned(frame: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(frame["aligned_timeframes"], errors="coerce").eq(3)


def _flow_agreement(frame: pd.DataFrame) -> pd.Series:
    taker = pd.to_numeric(frame["taker_buy_centered"], errors="coerce")
    cvd = pd.to_numeric(frame["cvd_share"], errors="coerce")
    return (
        frame["side"].eq("long") & taker.gt(0) & cvd.gt(0)
    ) | (
        frame["side"].eq("short") & taker.lt(0) & cvd.lt(0)
    )


POLICIES = (
    Policy("all", "highest model probability without a structural filter", _true),
    Policy("15m_only", "selected candidate horizon is 15m",
           lambda frame: frame["horizon"].eq("15m")),
    Policy("1H_only", "selected candidate horizon is 1H",
           lambda frame: frame["horizon"].eq("1H")),
    Policy("4H_only", "selected candidate horizon is 4H",
           lambda frame: frame["horizon"].eq("4H")),
    Policy("long_only", "selected candidate direction is long",
           lambda frame: frame["side"].eq("long")),
    Policy("short_only", "selected candidate direction is short",
           lambda frame: frame["side"].eq("short")),
    Policy("rule_agreement", "model direction agrees with the deterministic multi-TF rule",
           _rule_agreement),
    Policy("three_tf", "all three deterministic timeframes align", _three_aligned),
    Policy("three_tf_rule_agreement",
           "three timeframes align and model direction agrees with them",
           lambda frame: _three_aligned(frame) & _rule_agreement(frame)),
    Policy("flow_agreement",
           "taker imbalance and CVD both agree with the selected direction",
           _flow_agreement),
    Policy("4H_rule_agreement",
           "selected horizon is 4H and model agrees with the multi-TF rule",
           lambda frame: frame["horizon"].eq("4H") & _rule_agreement(frame)),
    Policy("4H_three_tf_rule_agreement",
           "selected horizon is 4H with full deterministic alignment and agreement",
           lambda frame: (
               frame["horizon"].eq("4H")
               & _three_aligned(frame)
               & _rule_agreement(frame)
           )),
    Policy("4H_rule_flow_agreement",
           "4H model, deterministic direction and observed trade flow all agree",
           lambda frame: (
               frame["horizon"].eq("4H")
               & _rule_agreement(frame)
               & _flow_agreement(frame)
           )),
)


def _read(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        "obs_id", "obs_ts", "symbol", "horizon", "side", "probability",
        "success", "signed_return_after_cost", "rule_direction",
        "aligned_timeframes", "taker_buy_centered", "cvd_share",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{path.name} missing columns: {','.join(missing)}")
    frame["obs_ts"] = pd.to_datetime(frame["obs_ts"], utc=True, errors="raise")
    for name in (
        "probability", "success", "signed_return_after_cost",
        "aligned_timeframes", "taker_buy_centered", "cvd_share",
    ):
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    return frame


def _policy_frame(frame: pd.DataFrame, policy: Policy) -> pd.DataFrame:
    mask = policy.predicate(frame)
    return frame.loc[mask.fillna(False)].copy()


def calibration_candidates(
    frame: pd.DataFrame,
    *,
    minimum_n: int = 100,
) -> list[dict[str, Any]]:
    output = []
    for policy in POLICIES:
        subset = _policy_frame(frame, policy)
        threshold, metrics = calibration._choose_threshold(
            subset, min_n=minimum_n)
        item = {
            "policy": policy.name,
            "rationale": policy.rationale,
            **metrics,
            "threshold": threshold,
        }
        output.append(item)
    return output


def choose_policy(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        item for item in candidates
        if item["selection_status"] in {
            "target_reached_on_calibration",
            "target_not_reached_on_calibration",
        }
    ]
    pool = eligible or candidates
    target = [
        item for item in pool
        if item["selection_status"] == "target_reached_on_calibration"
    ]
    ranked = target or pool
    chosen = max(
        ranked,
        key=lambda item: (
            item["wilson_95_low"]
            if item["wilson_95_low"] is not None else -1.0,
            item["precision"] if item["precision"] is not None else -1.0,
            item["n"],
            -next(index for index, policy in enumerate(POLICIES)
                  if policy.name == item["policy"]),
        ),
    )
    return dict(chosen)


def _selected_metrics(frame: pd.DataFrame, chosen: dict[str, Any]) -> dict[str, Any]:
    policy = next(item for item in POLICIES if item.name == chosen["policy"])
    subset = _policy_frame(frame, policy)
    return calibration._selection_metrics(subset, float(chosen["threshold"]))


def _deciles(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    ranked = frame.copy()
    ranked["probability_decile"] = pd.qcut(
        ranked["probability"].rank(method="first"), 10,
        labels=False, duplicates="drop",
    )
    output = []
    for decile, subset in ranked.groupby("probability_decile", sort=True):
        metrics = calibration._selection_metrics(
            subset, float(subset["probability"].min()))
        output.append({
            "decile": int(decile) + 1,
            "probability_min": float(subset["probability"].min()),
            "probability_max": float(subset["probability"].max()),
            **metrics,
        })
    return output


def _oracle(candidate_frame: pd.DataFrame, best_frame: pd.DataFrame) -> dict[str, Any]:
    grouped = candidate_frame.groupby("obs_id", sort=False)
    any_success = grouped["success"].max()
    complete = grouped.size().eq(6)
    valid_ids = complete[complete].index
    oracle = any_success.loc[valid_ids]
    best = best_frame.loc[best_frame["obs_id"].isin(valid_ids)]
    best_indexed = best.set_index("obs_id")["success"]
    aligned = best_indexed.reindex(valid_ids).dropna()
    oracle_aligned = oracle.reindex(aligned.index)
    opportunities = int(oracle_aligned.sum())
    captured = int(((oracle_aligned > 0) & (aligned > 0)).sum())
    return {
        "complete_observations": int(len(aligned)),
        "oracle_any_candidate_success_rate": (
            float(oracle_aligned.mean()) if len(aligned) else None),
        "best_probability_success_rate": (
            float(aligned.mean()) if len(aligned) else None),
        "oracle_success_opportunities": opportunities,
        "successful_opportunities_captured_by_top_probability": captured,
        "capture_rate_when_any_candidate_succeeds": (
            captured / opportunities if opportunities else None),
        "interpretation": (
            "oracle is a non-tradable ceiling using future labels; the gap to the "
            "top-probability row measures ranking weakness, not an achievable claim"
        ),
    }


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


def _atomic_table(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "policy", "selection_status", "threshold", "n", "successes",
        "precision", "wilson_95_low", "wilson_95_high", "ece",
        "distinct_days", "distinct_cycles", "long_n", "short_n",
        "mean_signed_return_after_cost", "rationale",
    ]
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=columns,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def diagnose(input_dir: Path, minimum_n: int = 100) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    calibration_best = _read(input_dir / "calibration_best_predictions.csv")
    holdout_best = _read(input_dir / "holdout_best_predictions.csv")
    calibration_all = _read(input_dir / "calibration_candidate_predictions.csv")
    holdout_all = _read(input_dir / "holdout_candidate_predictions.csv")
    metrics_path = input_dir / "calibration_metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    candidates = calibration_candidates(calibration_best, minimum_n=minimum_n)
    chosen = choose_policy(candidates)
    holdout = _selected_metrics(holdout_best, chosen)
    requirements = {
        "calibration_precision_at_least_90pct": (
            (chosen.get("precision") or 0.0) >= 0.90),
        "calibration_n_at_least_100": chosen.get("n", 0) >= minimum_n,
        "holdout_precision_at_least_90pct": (
            (holdout.get("precision") or 0.0) >= 0.90),
        "holdout_n_at_least_100": holdout.get("n", 0) >= minimum_n,
        "holdout_ece_at_most_5pp": (
            holdout.get("ece") is not None and holdout["ece"] <= 0.05),
        "holdout_days_at_least_5": holdout.get("distinct_days", 0) >= 5,
        "holdout_cycles_at_least_100": (
            holdout.get("distinct_cycles", 0) >= 100),
        "independent_unseen_future_window": False,
    }
    statistical_rows_ready = all(requirements[name] for name in (
        "holdout_n_at_least_100", "holdout_days_at_least_5",
        "holdout_cycles_at_least_100",
    ))
    if not statistical_rows_ready:
        status = "INSUFFICIENT_HOLDOUT_DIVERSITY"
    elif not all(requirements[name] for name in (
        "calibration_precision_at_least_90pct",
        "holdout_precision_at_least_90pct",
        "holdout_ece_at_most_5pp",
    )):
        status = "NOT_MET"
    else:
        status = "EXPLORATORY_PASS_REQUIRES_NEW_FORWARD_WINDOW"
    payload = {
        "schema_version": 1,
        "artifact_type": "multitimeframe_policy_diagnostic",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_research",
        "input_dir": input_dir.as_posix(),
        "split_contract": metrics.get("split_contract"),
        "cost_hurdle_bps": metrics.get("cost_hurdle_bps"),
        "right_censoring_contract": metrics.get("prediction_exports", {}),
        "candidate_family": {
            "count": len(POLICIES),
            "selection_data": "calibration window only",
            "multiple_testing_warning": (
                "fixed small exploratory family; selected historical holdout cannot "
                "be reused as final forward proof"
            ),
        },
        "calibration_candidates": candidates,
        "selected_policy": {
            "policy": chosen["policy"],
            "rationale": chosen["rationale"],
            "threshold": chosen["threshold"],
            "calibration": {
                key: value for key, value in chosen.items()
                if key not in {"policy", "rationale"}
            },
            "historical_holdout": holdout,
        },
        "probability_deciles": {
            "calibration": _deciles(calibration_best),
            "historical_holdout": _deciles(holdout_best),
        },
        "oracle_ranking_diagnostic": {
            "calibration": _oracle(calibration_all, calibration_best),
            "historical_holdout": _oracle(holdout_all, holdout_best),
        },
        "acceptance": {
            "target_precision": 0.90,
            "requirements": requirements,
            "status": status,
            "confidence_90_status": "NOT_PROVEN",
        },
        "conclusion": (
            "no historical policy result can authorize production; freeze a candidate "
            "only if calibration/holdout diagnostics are credible, then require a new "
            "future-only shadow window"
        ),
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "orders_placed": 0,
    }
    return payload, candidates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--table-out", type=Path, default=None)
    parser.add_argument("--minimum-n", type=int, default=100)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_out = args.json_out or (args.input_dir / "policy_diagnostic.json")
    table_out = args.table_out or (
        args.input_dir / "calibration_policy_candidates.csv")
    try:
        payload, candidates = diagnose(args.input_dir, args.minimum_n)
        _atomic_json(json_out, payload)
        _atomic_table(table_out, candidates)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_threshold_change_allowed": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    selected = payload["selected_policy"]
    print(json.dumps({
        "ok": True,
        "json_out": str(json_out),
        "table_out": str(table_out),
        "selected_policy": selected["policy"],
        "threshold": selected["threshold"],
        "calibration_precision": selected["calibration"]["precision"],
        "holdout_precision": selected["historical_holdout"]["precision"],
        "holdout_n": selected["historical_holdout"]["n"],
        "confidence_90_status": payload["acceptance"]["confidence_90_status"],
        "production_threshold_change_allowed": False,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
