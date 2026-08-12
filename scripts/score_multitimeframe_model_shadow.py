#!/usr/bin/env python3
"""Score one frozen 15m/1H/4H research model cycle without trading.

The scorer reconstructs exactly the feature contract used by
``offline_multitimeframe_calibration.py``.  It verifies SHA-256 hashes for the
frozen model and calibration metrics, requires a cycle after the declared
freeze boundary for forward evidence, and anchors signal availability to the
actual artifact generation time.  Output is a research shadow artifact only:
the offline candidate did not pass acceptance, so probabilities may be used
for evaluation but never as a production confidence claim or order authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import offline_multitimeframe_calibration as calibration


ROOT = Path(r".")
UTC = timezone.utc
CST = timezone(timedelta(hours=8))
DEFAULT_MANIFEST = (
    ROOT / "reports" / "quality" / "goal-multitimeframe-model-v2-20260812"
    / "frozen_model_manifest.json"
)
REQUIRED_MODEL_KEYS = tuple(
    f"{horizon}_{side}"
    for horizon in calibration.TIMEFRAMES
    for side in ("long", "short")
)
POSITIONING_FEATURES = frozenset({
    "positioning_available",
    "positioning_long_short_log",
    "positioning_long_minus_short",
})
CONTRACT_STATISTICS_FEATURES = frozenset({
    name for name in calibration.ENRICHMENT_FEATURES
    if name.startswith("contract_")
})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _parse_utc(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cycle_utc(cycle_id: str) -> datetime:
    parsed = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    if parsed.minute != 0:
        raise ValueError("cycle_id must be a scheduled CST hourly cycle")
    return parsed.astimezone(UTC)


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_utc(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    try:
        return calibration._iso(pd.Timestamp(value))
    except (TypeError, ValueError):
        return None


def _apply_frozen_feature_clock(
    frame: pd.DataFrame,
    spec: calibration.FeatureSpec,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Anchor availability only to sources used by the frozen feature spec.

    ``offline_multitimeframe_calibration._add_enrichment_features`` deliberately
    tracks every currently available optional family for future retraining.  A
    frozen prefix model must not silently acquire a later decision clock when a
    new optional family is appended after freeze.  Required microstructure and
    trade-flow sources always participate; positioning/contract statistics do
    so only when the frozen feature vector actually contains those features.
    """
    out = frame.copy()
    required = ("obs_ts", "micro_available_at", "flow_available_at")
    missing = [column for column in required if column not in out.columns]
    if missing:
        raise ValueError(f"frozen availability clock missing columns: {missing}")

    active_features = set(spec.continuous_features)
    availability: dict[str, pd.Series] = {
        "observation": pd.to_datetime(out["obs_ts"], utc=True, errors="coerce"),
        "microstructure": pd.to_datetime(
            out["micro_available_at"], utc=True, errors="coerce"),
        "trade_flow": pd.to_datetime(
            out["flow_available_at"], utc=True, errors="coerce"),
    }
    if active_features & POSITIONING_FEATURES:
        missing_positioning = [
            column for column in ("positioning_available", "positioning_available_at")
            if column not in out.columns
        ]
        if missing_positioning:
            raise ValueError(
                "frozen positioning feature lacks availability fields: "
                f"{missing_positioning}")
        valid = pd.to_numeric(
            out["positioning_available"], errors="coerce").fillna(0).gt(0)
        availability["positioning"] = pd.to_datetime(
            out["positioning_available_at"], utc=True, errors="coerce").where(valid)
    if active_features & CONTRACT_STATISTICS_FEATURES:
        missing_contract = [
            column for column in (
                "contract_stats_available", "contract_stats_available_at")
            if column not in out.columns
        ]
        if missing_contract:
            raise ValueError(
                "frozen contract feature lacks availability fields: "
                f"{missing_contract}")
        valid = pd.to_numeric(
            out["contract_stats_available"], errors="coerce").fillna(0).gt(0)
        availability["contract_statistics"] = pd.to_datetime(
            out["contract_stats_available_at"], utc=True, errors="coerce").where(valid)

    availability_frame = pd.DataFrame(availability, index=out.index)
    required_sources = ["observation", "microstructure", "trade_flow"]
    if availability_frame[required_sources].isna().any(axis=None):
        raise ValueError("required frozen feature availability timestamp is invalid")
    out["decision_ts"] = availability_frame.max(axis=1)
    delays = (out["decision_ts"] - availability_frame["observation"]).dt.total_seconds()
    return out, {
        "active_sources": list(availability),
        "contract_statistics_affects_frozen_clock": (
            "contract_statistics" in availability),
        "maximum_decision_delay_seconds": (
            float(delays.max()) if len(delays) else None),
    }


def _ranking_diagnostics(
    probabilities: np.ndarray,
    best_index: int,
) -> dict[str, Any]:
    best_key = REQUIRED_MODEL_KEYS[best_index]
    horizon, side = best_key.split("_", 1)
    opposite = "short" if side == "long" else "long"
    index_by_key = {key: index for index, key in enumerate(REQUIRED_MODEL_KEYS)}
    ordered = np.sort(np.asarray(probabilities, dtype=float))[::-1]
    side_edges = []
    for candidate_horizon in calibration.TIMEFRAMES:
        chosen = float(probabilities[index_by_key[f"{candidate_horizon}_{side}"]])
        other = float(probabilities[index_by_key[f"{candidate_horizon}_{opposite}"]])
        side_edges.append(chosen - other)
    opposite_probability = float(
        probabilities[index_by_key[f"{horizon}_{opposite}"]])
    return {
        "runner_up_probability": float(ordered[1]),
        "top_vs_runner_up_margin": float(ordered[0] - ordered[1]),
        "opposite_side_same_horizon_probability": opposite_probability,
        "selected_vs_opposite_margin": float(
            probabilities[best_index] - opposite_probability),
        "selected_side_horizon_votes": int(sum(edge > 0 for edge in side_edges)),
        "selected_side_unanimous": bool(all(edge > 0 for edge in side_edges)),
        "selected_side_mean_margin": float(np.mean(side_edges)),
        "selected_side_min_margin": float(np.min(side_edges)),
    }


def _future_feature_snapshot(row: pd.Series) -> dict[str, Any]:
    return {
        "contract_statistics_available": bool(
            (_finite_float(row.get("contract_stats_available")) or 0.0) > 0),
        "contract_statistics_source_ts_utc": _timestamp_utc(
            row.get("contract_stats_source_ts")),
        "contract_statistics_available_at_utc": _timestamp_utc(
            row.get("contract_stats_available_at")),
        "contract_oi_log_usd": _finite_float(row.get("contract_oi_log_usd")),
        "contract_oi_log_change_15m": _finite_float(
            row.get("contract_oi_log_change_15m")),
        "contract_taker_total_log_usd": _finite_float(
            row.get("contract_taker_total_log_usd")),
        "contract_taker_buy_centered": _finite_float(
            row.get("contract_taker_buy_centered")),
        "contract_oi_taker_interaction": _finite_float(
            row.get("contract_oi_taker_interaction")),
    }


def _load_bundle(
    root: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], calibration.FeatureSpec]:
    manifest = _read_json(manifest_path)
    if manifest.get("artifact_type") != "frozen_multitimeframe_research_model":
        raise ValueError("invalid frozen model manifest artifact_type")
    if manifest.get("production_execution_authorized") is not False:
        raise ValueError("frozen manifest must explicitly deny production execution")
    if manifest.get("orders_allowed") is not False:
        raise ValueError("frozen manifest must explicitly deny orders")
    model_path = _resolve(root, str(manifest["model_parameters_path"]))
    metrics_path = _resolve(root, str(manifest["calibration_metrics_path"]))
    if _sha256(model_path) != str(manifest["model_parameters_sha256"]):
        raise ValueError("model parameter SHA-256 mismatch")
    if _sha256(metrics_path) != str(manifest["calibration_metrics_sha256"]):
        raise ValueError("calibration metrics SHA-256 mismatch")
    model = _read_json(model_path)
    metrics = _read_json(metrics_path)
    if model.get("research_only") is not True:
        raise ValueError("model must be research_only")
    if model.get("feature_set") != "enhanced" or metrics.get("feature_set") != "enhanced":
        raise ValueError("only the frozen enhanced feature contract is supported")
    if metrics.get("production_threshold_change_allowed") is not False:
        raise ValueError("metrics must deny production threshold changes")
    if set(model.get("models") or {}) != set(REQUIRED_MODEL_KEYS):
        raise ValueError("frozen model must contain exactly six horizon/side models")
    raw_spec = model.get("feature_spec") or {}
    spec = calibration.FeatureSpec(
        medians={str(k): float(v) for k, v in (raw_spec.get("medians") or {}).items()},
        means={str(k): float(v) for k, v in (raw_spec.get("means") or {}).items()},
        scales={str(k): float(v) for k, v in (raw_spec.get("scales") or {}).items()},
        continuous_features=tuple(str(x) for x in raw_spec.get("continuous_features") or []),
        asset_classes=tuple(str(x) for x in raw_spec.get("asset_classes") or []),
        feature_names=tuple(str(x) for x in raw_spec.get("feature_names") or []),
    )
    base = tuple(calibration.CONTINUOUS_FEATURES)
    enrichment = tuple(calibration.ENRICHMENT_FEATURES)
    frozen = spec.continuous_features
    frozen_enrichment = frozen[len(base):]
    if (
        frozen[:len(base)] != base
        or frozen_enrichment != enrichment[:len(frozen_enrichment)]
    ):
        raise ValueError(
            "frozen continuous feature order does not match current scorer")
    return manifest, model, metrics, spec


def _build_frame(
    db_root: Path,
    cycle_utc: datetime,
    spec: calibration.FeatureSpec,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    start = calibration._iso(pd.Timestamp(cycle_utc))
    end = calibration._iso(pd.Timestamp(cycle_utc + timedelta(minutes=1)))
    market = calibration._ro(db_root / "market.db")
    try:
        observations, load_audit = calibration._load_observations(
            market,
            start,
            end,
            sample_minutes=60,
            min_quote_volume_usd=5_000_000.0,
            min_oi_usd=5_000_000.0,
        )
        with_bars = calibration._merge_closed_bars(market, observations)
        required = [
            f"{timeframe}_{field}"
            for timeframe in calibration.TIMEFRAMES
            for field in calibration.REQUIRED_BAR_FIELDS
        ]
        ready_mask = with_bars[required].notna().all(axis=1)
        analysis_ready_rows = int(ready_mask.sum())
        frame = with_bars.loc[ready_mask].copy().reset_index(drop=True)
        frame = calibration._add_technical_features(frame)
        news_path = db_root / "news.db"
        frame, news_audit = calibration._add_news_features(
            news_path if news_path.exists() else None,
            frame,
        )
        frame, enrichment_audit = calibration._add_enrichment_features(market, frame)
        frame = frame.loc[frame["enrichment_ready"]].copy().reset_index(drop=True)
        frame, frozen_clock_audit = _apply_frozen_feature_clock(frame, spec)
    finally:
        market.close()
    return frame, {
        **load_audit,
        "analysis_ready_rows": analysis_ready_rows,
        "news": news_audit,
        "enrichment": enrichment_audit,
        "frozen_feature_clock": frozen_clock_audit,
        "scoring_ready_rows": int(len(frame)),
    }


def score_cycle(
    *,
    root: Path,
    db_root: Path,
    manifest_path: Path,
    cycle_id: str,
    allow_pre_freeze_reconstruction: bool = False,
) -> dict[str, Any]:
    cycle_start = _cycle_utc(cycle_id)
    manifest, model, metrics, spec = _load_bundle(root, manifest_path)
    first_forward = _parse_utc(str(manifest["first_forward_cycle_utc"]))
    forward_eligible = cycle_start >= first_forward
    if not forward_eligible and not allow_pre_freeze_reconstruction:
        raise ValueError(
            f"cycle predates frozen forward boundary {manifest['first_forward_cycle_utc']}"
        )
    frame, data_audit = _build_frame(db_root, cycle_start, spec)
    generated = datetime.now(UTC)
    threshold = float(manifest["selection_threshold"])
    if frame.empty:
        probabilities = np.empty((0, len(REQUIRED_MODEL_KEYS)), dtype=float)
    else:
        matrix = calibration.transform_features(frame, spec)
        columns: list[np.ndarray] = []
        for key in REQUIRED_MODEL_KEYS:
            parameters = model["models"][key]
            weights = np.asarray(parameters["weights"], dtype=float)
            if len(weights) != matrix.shape[1]:
                raise ValueError(f"weight length mismatch for {key}")
            raw = calibration._sigmoid(matrix @ weights)
            columns.append(calibration.apply_platt(
                raw,
                (float(parameters["platt_intercept"]), float(parameters["platt_slope"])),
            ))
        probabilities = np.column_stack(columns)

    records: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        best_index = int(np.argmax(probabilities[idx]))
        best_key = REQUIRED_MODEL_KEYS[best_index]
        horizon, side = best_key.split("_", 1)
        probability = float(probabilities[idx, best_index])
        ranking = _ranking_diagnostics(probabilities[idx], best_index)
        decision_ts = pd.Timestamp(row["decision_ts"]).to_pydatetime().astimezone(UTC)
        signal_available = max(decision_ts, generated)
        records.append({
            "symbol": str(row["symbol"]),
            "observation_ts_utc": calibration._iso(pd.Timestamp(row["obs_ts"])),
            "feature_decision_ts_utc": calibration._iso(pd.Timestamp(row["decision_ts"])),
            "signal_available_at_utc": signal_available.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "asset_class": str(row.get("asset_class") or "unknown"),
            "selected_model": best_key,
            "horizon": horizon,
            "side": side,
            "research_probability": probability,
            "frozen_selection_threshold": threshold,
            "selected_for_forward_evaluation": probability >= threshold,
            "all_research_probabilities": {
                key: float(probabilities[idx, column])
                for column, key in enumerate(REQUIRED_MODEL_KEYS)
            },
            "ranking_diagnostics": ranking,
            "future_retraining_features": _future_feature_snapshot(row),
            "quote_volume_24h_usd": float(row["quote_volume_usd"]),
            "open_interest_usd": float(row["oi_usd"]),
            "positioning_available": bool(float(row["positioning_available"]) > 0),
            "confidence_claim_allowed": False,
            "production_execution_authorized": False,
        })

    selected = [record for record in records if record["selected_for_forward_evaluation"]]
    status = (
        "ready_for_forward_shadow"
        if forward_eligible and records
        else "pre_freeze_reconstruction_not_forward_evidence"
        if records
        else "degraded_no_scoring_rows"
    )
    return {
        "schema_version": 1,
        "artifact_type": "frozen_multitimeframe_model_shadow",
        "model_id": manifest["model_id"],
        "model_parameters_sha256": manifest["model_parameters_sha256"],
        "cycle_id": cycle_id,
        "cycle_start_utc": cycle_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_utc": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forward_evidence_eligible": forward_eligible,
        "status": status,
        "method": {
            "feature_set": "enhanced",
            "candidate_models": list(REQUIRED_MODEL_KEYS),
            "selection": "highest Platt research probability across 3 horizons x 2 sides",
            "availability_anchor": "max(feature decision timestamp, artifact generated timestamp)",
            "cost_hurdle_bps": float(metrics["cost_hurdle_bps"]),
            "frozen_selection_threshold": threshold,
        },
        "offline_acceptance": {
            "gate_pass": bool(metrics["offline_gate_pass"]),
            "holdout_precision": metrics["holdout"]["precision"],
            "holdout_n": metrics["holdout"]["n"],
            "holdout_ece": metrics["holdout"]["ece"],
        },
        "data_audit": data_audit,
        "metrics": {
            "scored_symbols": len(records),
            "selected_signals": len(selected),
            "side_counts": dict(sorted(Counter(record["side"] for record in selected).items())),
            "horizon_counts": dict(sorted(Counter(record["horizon"] for record in selected).items())),
        },
        "records": records,
        "confidence_claim_allowed": False,
        "production_threshold_change_allowed": False,
        "production_execution_authorized": False,
        "production_mutation": False,
        "orders_placed": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db-root", type=Path, default=ROOT / "db")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--allow-pre-freeze-reconstruction", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = score_cycle(
            root=args.root,
            db_root=args.db_root,
            manifest_path=args.manifest,
            cycle_id=args.cycle_id,
            allow_pre_freeze_reconstruction=args.allow_pre_freeze_reconstruction,
        )
        calibration._atomic_json(args.json_out, payload)
        print(json.dumps({
            "ok": bool(payload["records"]),
            "status": payload["status"],
            "json_out": str(args.json_out.resolve()),
            "cycle_id": payload["cycle_id"],
            "forward_evidence_eligible": payload["forward_evidence_eligible"],
            "scored_symbols": payload["metrics"]["scored_symbols"],
            "selected_signals": payload["metrics"]["selected_signals"],
            "confidence_claim_allowed": False,
            "production_mutation": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 0 if payload["records"] else 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "ok": False,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "json_out": str(args.json_out),
            "confidence_claim_allowed": False,
            "production_mutation": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
