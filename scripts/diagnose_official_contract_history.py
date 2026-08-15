#!/usr/bin/env python3
"""Retrospective diagnostic for official 1H contract-statistics features.

This script joins an isolated OKX history database to an already generated
point-in-time research panel.  A row is eligible only when the exact previous
1H bucket would have cleared a conservative publication buffer before the
panel's recorded first post-decision entry.  The augmented decision clock is
moved to the later of the original decision and simulated official-data
availability.  It compares the frozen panel feature set with an augmented
feature set, writes research artifacts only, and never authorizes a production
threshold or order change.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

import offline_multitimeframe_calibration as calibration


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB_ROOT = (ROOT / "db").resolve()
HISTORY_FEATURES = (
    "official_contract_stats_available",
    "official_contract_oi_log_usd",
    "official_contract_oi_log_change_1h",
    "official_contract_taker_total_log_usd",
    "official_contract_taker_buy_centered",
    "official_contract_oi_taker_interaction",
    "official_contract_long_short_log",
)
HISTORY_4H_FEATURES = (
    "official_contract_4h_stats_available",
    "official_contract_4h_oi_log_usd",
    "official_contract_4h_oi_log_change",
    "official_contract_4h_taker_total_log_usd",
    "official_contract_4h_taker_buy_centered",
    "official_contract_4h_oi_taker_interaction",
    "official_contract_4h_long_short_log",
)
HISTORY_FEATURES_BY_PERIOD = {
    "1H": HISTORY_FEATURES,
    "4H": HISTORY_4H_FEATURES,
}
PERIOD_HOURS = {"1H": 1, "4H": 4}


def _period_feature_contract(period: str) -> tuple[tuple[str, ...], str]:
    try:
        features = HISTORY_FEATURES_BY_PERIOD[period]
        hours = PERIOD_HOURS[period]
    except KeyError as exc:
        raise ValueError(f"unsupported diagnostic history period: {period}") from exc
    prefix = "official_contract" if hours == 1 else f"official_contract_{hours}h"
    return features, prefix


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _outside_production_db(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == PRODUCTION_DB_ROOT or PRODUCTION_DB_ROOT in resolved.parents:
        raise ValueError(f"{label} must be outside the production db directory")
    return resolved


def _ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                payload, handle, ensure_ascii=False, indent=2,
                sort_keys=True, allow_nan=False,
            )
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
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            frame.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_history_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("artifact_type") != "isolated_okx_official_contract_history":
        raise ValueError("history manifest artifact_type is invalid")
    if payload.get("status") != "complete":
        raise ValueError("history manifest transport run is not complete")
    requests = payload.get("requests") or {}
    safety = payload.get("safety") or {}
    if requests.get("transport_failed") != 0:
        raise ValueError("history manifest contains transport failures")
    if requests.get("invalid_rows") != 0:
        raise ValueError("history manifest contains invalid rows")
    if safety.get("production_database_writes") != 0:
        raise ValueError("history manifest reports production database writes")
    if safety.get("order_calls") != 0:
        raise ValueError("history manifest reports order calls")
    return payload


def load_common_history(history_db: Path, period: str = "1H") -> pd.DataFrame:
    feature_names, _ = _period_feature_contract(period)
    con = _ro(history_db)
    try:
        frame = pd.read_sql_query(
            """
            SELECT oi.symbol,oi.period,oi.ts_ms,
                   oi.oi_contracts,oi.oi_ccy,oi.oi_usd,
                   tv.sell_volume_usd,tv.buy_volume_usd,
                   lr.account_long_short_ratio
            FROM open_interest oi
            JOIN taker_volume tv USING(symbol,period,ts_ms)
            JOIN long_short_ratio lr USING(symbol,period,ts_ms)
            WHERE oi.period=?
            ORDER BY oi.symbol,oi.ts_ms
            """,
            con,
            params=(period,),
        )
    finally:
        con.close()
    if frame.empty:
        raise RuntimeError("isolated history has no exact three-source rows")
    if frame.duplicated(["symbol", "period", "ts_ms"]).any():
        raise ValueError("isolated history common keys are duplicated")
    numeric_columns = [
        "oi_contracts", "oi_ccy", "oi_usd",
        "sell_volume_usd", "buy_volume_usd",
        "account_long_short_ratio",
    ]
    numeric = frame[numeric_columns].apply(pd.to_numeric, errors="coerce")
    finite = np.isfinite(numeric.to_numpy(dtype=float)).all(axis=1)
    nonnegative = numeric.drop(
        columns=["account_long_short_ratio"]).ge(0).all(axis=1)
    positive = numeric["account_long_short_ratio"].gt(0)
    if not bool((finite & nonnegative & positive).all()):
        raise ValueError("isolated history contains invalid common numerics")
    for column in numeric_columns:
        frame[column] = numeric[column]
    frame["source_ts"] = pd.to_datetime(
        frame["ts_ms"], unit="ms", utc=True, errors="coerce")
    if frame["source_ts"].isna().any():
        raise ValueError("isolated history contains invalid timestamps")
    frame.sort_values(["symbol", "ts_ms"], inplace=True)
    oi_log = np.log1p(frame["oi_usd"].clip(lower=0))
    frame[feature_names[1]] = oi_log
    frame[feature_names[2]] = oi_log.groupby(
        frame["symbol"]).diff()
    taker_total = frame["sell_volume_usd"] + frame["buy_volume_usd"]
    frame[feature_names[3]] = np.log1p(
        taker_total.clip(lower=0))
    frame[feature_names[4]] = (
        frame["buy_volume_usd"] / taker_total.replace(0, np.nan) - 0.5
    )
    frame[feature_names[5]] = (
        frame[feature_names[2]]
        * frame[feature_names[4]]
    )
    frame[feature_names[6]] = np.log(
        frame["account_long_short_ratio"])
    return frame


def load_panel(path: Path) -> pd.DataFrame:
    panel = pd.read_csv(path)
    required = {
        "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol",
        "asset_class", "split",
        *(
            f"{horizon}_{side}_{suffix}"
            for horizon in calibration.TIMEFRAMES
            for side in ("long", "short")
            for suffix in ("return", "success")
        ),
    }
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(
            "panel is not executable-price research evidence; missing: "
            + ",".join(missing))
    if panel["obs_id"].duplicated().any():
        raise ValueError("panel obs_id must be unique")
    for column in ("obs_ts", "decision_ts", "entry_ts"):
        raw = panel[column]
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")
        explicitly_present = raw.notna() & raw.astype(str).str.strip().ne("")
        if bool((explicitly_present & parsed.isna()).any()):
            raise ValueError(f"panel contains invalid {column}")
        if column != "entry_ts" and parsed.isna().any():
            raise ValueError(f"panel contains missing {column}")
        panel[column] = parsed
    allowed_splits = {"train", "calibration", "test", "purged"}
    if not set(panel["split"].dropna().unique()).issubset(allowed_splits):
        raise ValueError("panel contains an unknown split")
    return panel


def attach_exact_previous_period(
    panel: pd.DataFrame,
    history: pd.DataFrame,
    *,
    period: str,
    publication_buffer_minutes: int = 5,
    base_decision_column: str = "decision_ts",
    output_decision_column: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if publication_buffer_minutes < 0:
        raise ValueError("publication_buffer_minutes must be nonnegative")
    feature_names, prefix = _period_feature_contract(period)
    hours = PERIOD_HOURS[period]
    if base_decision_column not in panel.columns:
        raise ValueError(
            f"panel is missing base decision column: {base_decision_column}")
    output_decision_column = output_decision_column or (
        f"{prefix}_augmented_decision_ts")
    out = panel.copy()
    expected_source_column = f"{prefix}_expected_source_ts"
    expected_ms_column = f"{prefix}_expected_source_ts_ms"
    joined_source_column = f"{prefix}_source_ts"
    joined_ms_column = f"{prefix}_source_ts_ms"
    available_column = f"{prefix}_available_at"
    shift_column = f"{prefix}_decision_shift_seconds"
    expected_source = (
        out["obs_ts"].dt.floor(f"{hours}h") - pd.Timedelta(hours=hours))
    out[expected_source_column] = expected_source
    out[expected_ms_column] = (
        calibration._datetime_ns(expected_source) // 1_000_000)
    history_columns = [
        "symbol", "ts_ms", "source_ts",
        *feature_names[1:],
    ]
    history_for_join = history[history_columns].rename(columns={
        "ts_ms": joined_ms_column,
        "source_ts": joined_source_column,
    })
    joined = out.merge(
        history_for_join,
        how="left",
        left_on=["symbol", expected_ms_column],
        right_on=["symbol", joined_ms_column],
        validate="many_to_one",
    )
    available_at = (
        joined[joined_source_column]
        + pd.Timedelta(hours=hours)
        + pd.Timedelta(minutes=publication_buffer_minutes)
    )
    feature_core = joined[list(feature_names[1:])].notna().all(axis=1)
    exact_source = joined[joined_source_column].eq(
        joined[expected_source_column])
    entry_guard_ok = (
        joined["entry_ts"].isna()
        | available_at.lt(joined["entry_ts"])
    )
    timing_ok = (
        available_at.notna()
        & entry_guard_ok
    )
    valid = exact_source & feature_core & timing_ok
    joined[available_column] = available_at.where(valid)
    joined[feature_names[0]] = valid.astype(float)
    augmented_decision = pd.concat(
        [joined[base_decision_column], available_at.where(valid)], axis=1
    ).max(axis=1)
    joined[output_decision_column] = augmented_decision
    joined[shift_column] = (
        augmented_decision - joined[base_decision_column]
    ).dt.total_seconds()
    for column in feature_names[1:]:
        joined[column] = pd.to_numeric(
            joined[column], errors="coerce").where(valid)
    coverage_by_split = {}
    for split in ("train", "calibration", "test", "purged"):
        mask = joined["split"].eq(split)
        coverage_by_split[split] = {
            "rows": int(mask.sum()),
            "valid_rows": int((mask & valid).sum()),
            "coverage_rate": (
                float(valid.loc[mask].mean()) if mask.any() else None
            ),
        }
    audit = {
        "panel_rows": int(len(joined)),
        "panel_symbols": int(joined["symbol"].nunique()),
        "exact_source_rows": int(exact_source.sum()),
        "feature_complete_rows": int(feature_core.sum()),
        "timing_eligible_rows": int(timing_ok.sum()),
        "entry_guard_applicable_rows": int(joined["entry_ts"].notna().sum()),
        "entry_guard_failed_rows": int((
            joined["entry_ts"].notna() & ~entry_guard_ok
        ).sum()),
        "decision_shifted_rows": int((
            joined[shift_column] > 0
        ).sum()),
        "maximum_decision_shift_seconds": float(
            joined[shift_column].max()),
        "valid_rows": int(valid.sum()),
        "coverage_rate": float(valid.mean()),
        "publication_buffer_minutes": publication_buffer_minutes,
        "period": period,
        "source_rule": (
            f"exact previous UTC {period} bucket; bucket end plus fixed publication "
            "buffer may move the decision later but must remain before the "
            "recorded first post-decision entry"
        ),
        "by_split": coverage_by_split,
    }
    return joined, audit


def attach_exact_previous_hour(
    panel: pd.DataFrame,
    history: pd.DataFrame,
    *,
    publication_buffer_minutes: int = 5,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Backward-compatible 1H wrapper used by tests and single-period runs."""
    return attach_exact_previous_period(
        panel,
        history,
        period="1H",
        publication_buffer_minutes=publication_buffer_minutes,
        base_decision_column="decision_ts",
        output_decision_column="official_augmented_decision_ts",
    )


def _feature_names(panel: pd.DataFrame) -> tuple[str, ...]:
    ordered = (
        *calibration.CONTINUOUS_FEATURES,
        *calibration.ENRICHMENT_FEATURES,
    )
    missing = [name for name in ordered if name not in panel.columns]
    if missing:
        raise ValueError(
            "panel is missing frozen feature columns: " + ",".join(missing))
    return tuple(ordered)


def cross_section_top_k_metrics(
    frame: pd.DataFrame,
    top_values: Iterable[int] = (1, 3, 5, 10),
) -> list[dict[str, Any]]:
    """Evaluate fixed, outcome-free probability ranks within each cycle."""
    required = {
        "obs_ts", "symbol", "horizon", "side", "probability", "success",
        "signed_return_after_cost",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("top-k frame missing columns: " + ",".join(missing))
    ranked = frame.sort_values(
        ["obs_ts", "probability", "symbol", "horizon", "side"],
        ascending=[True, False, True, True, True],
        kind="mergesort",
    ).copy()
    ranked["cross_section_probability_rank"] = (
        ranked.groupby("obs_ts", sort=False).cumcount() + 1)
    output: list[dict[str, Any]] = []
    for raw_top in top_values:
        top = int(raw_top)
        if top <= 0:
            raise ValueError("top-k values must be positive")
        selected = ranked.loc[
            ranked["cross_section_probability_rank"].le(top)].copy()
        metrics = calibration._selection_metrics(selected, -1.0)
        metrics.pop("threshold", None)
        metrics["top_k"] = top
        output.append(metrics)
    return output


def fit_variant(
    panel: pd.DataFrame,
    *,
    continuous_features: Iterable[str],
    cost_bps: float,
    decision_time_column: str = "decision_ts",
) -> tuple[dict[str, Any], pd.DataFrame]:
    features = tuple(continuous_features)
    if decision_time_column not in panel.columns:
        raise ValueError(
            f"panel is missing decision time column: {decision_time_column}")
    train_mask = panel["split"].eq("train")
    calibration_split = panel["split"].eq("calibration")
    test_split = panel["split"].eq("test")
    if not train_mask.any() or not calibration_split.any() or not test_split.any():
        raise ValueError("panel train/calibration/test splits must all be nonempty")
    spec = calibration.fit_feature_spec(panel, train_mask, features)
    matrix = calibration.transform_features(panel, spec)
    candidates: dict[str, list[pd.DataFrame]] = {
        "calibration": [], "test": [],
    }
    task_audit: dict[str, Any] = {}
    for horizon in calibration.TIMEFRAMES:
        for side in ("long", "short"):
            label_column = f"{horizon}_{side}_success"
            return_column = f"{horizon}_{side}_return"
            label_available = panel[label_column].notna()
            y = panel[label_column].fillna(False).astype(float).to_numpy()
            task_train = train_mask & label_available
            task_calibration = calibration_split & label_available
            task_test = test_split & label_available
            weights = calibration.fit_ridge_logistic(
                matrix[task_train], y[task_train])
            raw_calibration = calibration._sigmoid(
                matrix[task_calibration] @ weights)
            platt = calibration.fit_platt(
                raw_calibration, y[task_calibration])
            key = f"{horizon}_{side}"
            task_audit[key] = {
                "train_n": int(task_train.sum()),
                "calibration_n": int(task_calibration.sum()),
                "test_n": int(task_test.sum()),
            }
            for split, mask in (
                ("calibration", task_calibration),
                ("test", task_test),
            ):
                probability = calibration.apply_platt(
                    calibration._sigmoid(matrix[mask] @ weights), platt)
                rows = panel.loc[
                    mask,
                    [
                        "obs_id", "obs_ts", "entry_ts",
                        "symbol", return_column,
                    ],
                ].copy()
                rows["decision_ts"] = panel.loc[
                    mask, decision_time_column].to_numpy()
                rows.rename(
                    columns={return_column: "forward_return"}, inplace=True)
                rows["horizon"] = horizon
                rows["side"] = side
                rows["probability"] = probability
                rows["success"] = y[mask]
                rows["signed_return_after_cost"] = (
                    rows["forward_return"] - cost_bps / 10_000.0)
                candidates[split].append(rows)
    calibration_best = calibration._best_per_observation(
        candidates["calibration"])
    test_best = calibration._best_per_observation(candidates["test"])
    threshold, calibration_selection = calibration._choose_threshold(
        calibration_best, min_n=100)
    holdout = calibration._selection_metrics(test_best, threshold)
    selected = test_best.loc[test_best["probability"].ge(threshold)].copy()
    metrics = {
        "feature_count": len(features),
        "transformed_feature_count": len(spec.feature_names),
        "task_rows": task_audit,
        "calibration_selection": calibration_selection,
        "holdout": holdout,
        "holdout_by_cross_section_probability_top_k": (
            cross_section_top_k_metrics(test_best)
        ),
        "threshold": threshold,
        "selected_holdout_rows": int(len(selected)),
    }
    return metrics, selected


def _delta(after: Any, before: Any) -> float | None:
    if after is None or before is None:
        return None
    values = (float(after), float(before))
    if not all(math.isfinite(value) for value in values):
        return None
    return values[0] - values[1]


def diagnose(
    *,
    panel_csv: Path,
    history_db: Path,
    history_manifest: Path,
    history_4h_db: Path | None = None,
    history_4h_manifest: Path | None = None,
    output_dir: Path,
    publication_buffer_minutes: int = 5,
    cost_bps: float = 20.0,
) -> dict[str, Any]:
    history_db = _outside_production_db(
        history_db, label="history database")
    output_dir = _outside_production_db(
        output_dir, label="output directory")
    source_manifest = load_history_manifest(history_manifest)
    if (history_4h_db is None) != (history_4h_manifest is None):
        raise ValueError(
            "history_4h_db and history_4h_manifest must be supplied together")
    panel = load_panel(panel_csv)
    history = load_common_history(history_db, period="1H")
    enriched, match_audit = attach_exact_previous_hour(
        panel,
        history,
        publication_buffer_minutes=publication_buffer_minutes,
    )
    source_4h_manifest: dict[str, Any] | None = None
    match_4h_audit: dict[str, Any] | None = None
    four_hour_only: dict[str, Any] | None = None
    four_hour_only_selected: pd.DataFrame | None = None
    augmented_decision_column = "official_augmented_decision_ts"
    augmented_history_features: tuple[str, ...] = HISTORY_FEATURES
    if history_4h_db is not None and history_4h_manifest is not None:
        history_4h_db = _outside_production_db(
            history_4h_db, label="4H history database")
        source_4h_manifest = load_history_manifest(history_4h_manifest)
        history_4h = load_common_history(history_4h_db, period="4H")
        enriched, match_4h_audit = attach_exact_previous_period(
            enriched,
            history_4h,
            period="4H",
            publication_buffer_minutes=publication_buffer_minutes,
            base_decision_column="official_augmented_decision_ts",
            output_decision_column=(
                "official_augmented_multiperiod_decision_ts"),
        )
        augmented_decision_column = (
            "official_augmented_multiperiod_decision_ts")
        enriched["official_augmented_4h_only_decision_ts"] = pd.concat(
            [
                enriched["decision_ts"],
                enriched["official_contract_4h_available_at"],
            ],
            axis=1,
        ).max(axis=1)
        augmented_history_features = (
            *HISTORY_FEATURES, *HISTORY_4H_FEATURES)
    baseline_features = _feature_names(enriched)
    augmented_features = (*baseline_features, *augmented_history_features)
    baseline, baseline_selected = fit_variant(
        enriched,
        continuous_features=baseline_features,
        cost_bps=cost_bps,
        decision_time_column="decision_ts",
    )
    augmented, augmented_selected = fit_variant(
        enriched,
        continuous_features=augmented_features,
        cost_bps=cost_bps,
        decision_time_column=augmented_decision_column,
    )
    if match_4h_audit is not None:
        four_hour_only, four_hour_only_selected = fit_variant(
            enriched,
            continuous_features=(*baseline_features, *HISTORY_4H_FEATURES),
            cost_bps=cost_bps,
            decision_time_column="official_augmented_4h_only_decision_ts",
        )
    baseline_holdout = baseline["holdout"]
    augmented_holdout = augmented["holdout"]
    retrospective_requirements = {
        "panel_1h_history_coverage_at_least_99pct": (
            match_audit["coverage_rate"] >= 0.99),
        "holdout_n_at_least_100": augmented_holdout["n"] >= 100,
        "holdout_precision_at_least_90pct": (
            (augmented_holdout["precision"] or 0.0) >= 0.90),
        "holdout_wilson_low_at_least_90pct": (
            (augmented_holdout["wilson_95_low"] or 0.0) >= 0.90),
        "holdout_ece_at_most_5pp": (
            augmented_holdout["ece"] is not None
            and augmented_holdout["ece"] <= 0.05
        ),
        "holdout_distinct_days_at_least_5": (
            augmented_holdout["distinct_days"] >= 5),
        "holdout_distinct_cycles_at_least_100": (
            augmented_holdout["distinct_cycles"] >= 100),
    }
    if match_4h_audit is not None:
        retrospective_requirements[
            "panel_4h_history_coverage_at_least_99pct"
        ] = match_4h_audit["coverage_rate"] >= 0.99
    metrics: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "official_contract_history_feature_diagnostic",
        "generated_at_utc": _iso(datetime.now(UTC)),
        "mode": "retrospective_research_only",
        "inputs": {
            "panel_csv": str(panel_csv.resolve()),
            "history_db": str(history_db),
            "history_manifest": str(history_manifest.resolve()),
            "history_run_id": source_manifest.get("run_id"),
            "history_4h_db": (
                str(history_4h_db) if history_4h_db is not None else None),
            "history_4h_manifest": (
                str(history_4h_manifest.resolve())
                if history_4h_manifest is not None else None
            ),
            "history_4h_run_id": (
                source_4h_manifest.get("run_id")
                if source_4h_manifest is not None else None
            ),
        },
        "point_in_time_simulation": {
            "periods": (
                ["1H", "4H"] if match_4h_audit is not None else ["1H"]),
            "publication_buffer_minutes": publication_buffer_minutes,
            "decision_clock": (
                "max(recorded decision, simulated official availability)"
            ),
            "entry_guard": (
                "shifted decision must remain before recorded first "
                "post-decision entry"
            ),
            "limitation": (
                "Historical endpoint values are joined retrospectively; "
                "simulated availability is not forward collection evidence."
            ),
        },
        "panel_history_match_1h": match_audit,
        "panel_history_match_4h": match_4h_audit,
        "baseline": baseline,
        "with_official_contract_history": augmented,
        "with_official_contract_history_4h_only": four_hour_only,
        "holdout_delta": {
            "n": int(augmented_holdout["n"] - baseline_holdout["n"]),
            "precision": _delta(
                augmented_holdout["precision"],
                baseline_holdout["precision"],
            ),
            "wilson_95_low": _delta(
                augmented_holdout["wilson_95_low"],
                baseline_holdout["wilson_95_low"],
            ),
            "ece": _delta(
                augmented_holdout["ece"], baseline_holdout["ece"]),
        },
        "holdout_delta_4h_only": (
            {
                "n": int(
                    four_hour_only["holdout"]["n"]
                    - baseline_holdout["n"]
                ),
                "precision": _delta(
                    four_hour_only["holdout"]["precision"],
                    baseline_holdout["precision"],
                ),
                "wilson_95_low": _delta(
                    four_hour_only["holdout"]["wilson_95_low"],
                    baseline_holdout["wilson_95_low"],
                ),
                "ece": _delta(
                    four_hour_only["holdout"]["ece"],
                    baseline_holdout["ece"],
                ),
            }
            if four_hour_only is not None else None
        ),
        "retrospective_requirements": retrospective_requirements,
        "retrospective_gate_pass": all(retrospective_requirements.values()),
        "credibility_90_forward_status": "NOT_PROVEN",
        "production_change_allowed": False,
        "production_change_block_reason": (
            "retrospective historical diagnostics cannot replace the strict "
            "frozen forward evidence gate"
        ),
        "safety": {
            "production_database_writes": 0,
            "production_threshold_changes": 0,
            "order_calls": 0,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(output_dir / "diagnostic.json", metrics)
    selected_columns = [
        "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol",
        "horizon", "side", "probability", "success",
        "forward_return", "signed_return_after_cost",
    ]
    _atomic_csv(
        output_dir / "holdout_selected_baseline.csv",
        baseline_selected[selected_columns].sort_values(["obs_ts", "symbol"]),
    )
    _atomic_csv(
        output_dir / "holdout_selected_with_official_history.csv",
        augmented_selected[selected_columns].sort_values(["obs_ts", "symbol"]),
    )
    if four_hour_only_selected is not None:
        _atomic_csv(
            output_dir / "holdout_selected_with_official_history_4h_only.csv",
            four_hour_only_selected[selected_columns].sort_values(
                ["obs_ts", "symbol"]),
        )
    match_columns = [
        "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol", "split",
        "official_contract_expected_source_ts",
        "official_contract_available_at",
        "official_augmented_decision_ts",
        "official_contract_decision_shift_seconds",
        "official_contract_stats_available",
    ]
    if match_4h_audit is not None:
        match_columns.extend([
            "official_contract_4h_expected_source_ts",
            "official_contract_4h_available_at",
            "official_augmented_multiperiod_decision_ts",
            "official_augmented_4h_only_decision_ts",
            "official_contract_4h_decision_shift_seconds",
            "official_contract_4h_stats_available",
        ])
    _atomic_csv(
        output_dir / "panel_history_match_audit.csv",
        enriched[match_columns].sort_values(["obs_ts", "symbol"]),
    )
    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel-csv", type=Path, required=True)
    parser.add_argument("--history-db", type=Path, required=True)
    parser.add_argument("--history-manifest", type=Path)
    parser.add_argument("--history-4h-db", type=Path)
    parser.add_argument("--history-4h-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--publication-buffer-minutes", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    history_manifest = (
        args.history_manifest
        if args.history_manifest is not None
        else args.history_db.with_suffix(".manifest.json")
    )
    history_4h_manifest = args.history_4h_manifest
    if args.history_4h_db is not None and history_4h_manifest is None:
        history_4h_manifest = args.history_4h_db.with_suffix(".manifest.json")
    metrics = diagnose(
        panel_csv=args.panel_csv,
        history_db=args.history_db,
        history_manifest=history_manifest,
        history_4h_db=args.history_4h_db,
        history_4h_manifest=history_4h_manifest,
        output_dir=args.output_dir,
        publication_buffer_minutes=args.publication_buffer_minutes,
        cost_bps=args.cost_bps,
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
