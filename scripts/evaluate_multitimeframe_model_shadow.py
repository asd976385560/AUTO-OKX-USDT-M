#!/usr/bin/env python3
"""Evaluate frozen model-shadow artifacts with actual post-generation prices.

Only artifacts created after the model freeze boundary and explicitly marked as
forward evidence are eligible.  Entry is the first ticker strictly after the
maximum of feature-decision time, artifact generation time, and the record's
declared signal-availability time.  The script is read-only with respect to all
business databases and cannot authorize a threshold or order change.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

import _acceptance_thresholds as thresholds
import offline_multitimeframe_calibration as calibration


UTC = timezone.utc
HORIZON_MINUTES = {"15m": 15, "1H": 60, "4H": 240}
CANDIDATE_MODEL_KEYS = tuple(
    f"{horizon}_{side}"
    for horizon in HORIZON_MINUTES
    for side in ("long", "short")
)
CANDIDATE_PROBABILITY_FIELDS = {
    key: f"candidate_probability_{key}" for key in CANDIDATE_MODEL_KEYS
}
LABEL_COLUMNS = (
    "model_id", "model_parameters_sha256", "cycle_id", "symbol", "side",
    "horizon", "research_probability", "selected_probability_rank",
    "selected_margin_rank", "selected_cross_section_size",
    "signal_available_at_utc",
    "entry_tick_ts_utc", "entry_price", "entry_last", "entry_executable",
    "entry_price_source", "outcome_tick_ts_utc", "outcome_price",
    "outcome_last", "outcome_executable", "outcome_price_source",
    "signed_return", "last_directional_return",
    "executable_directional_return", "signed_return_after_cost",
    "after_cost_hit",
    "runner_up_probability", "top_vs_runner_up_margin",
    "opposite_side_same_horizon_probability", "selected_vs_opposite_margin",
    "selected_side_horizon_votes", "selected_side_unanimous",
    "selected_side_mean_margin", "selected_side_min_margin",
    "selected_model", "candidate_probability_count",
    "selected_is_highest_candidate_probability",
    *CANDIDATE_PROBABILITY_FIELDS.values(),
    "contract_statistics_available", "contract_statistics_source_ts_utc",
    "contract_statistics_available_at_utc", "contract_oi_log_usd",
    "contract_oi_log_change_15m", "contract_taker_total_log_usd",
    "contract_taker_buy_centered", "contract_oi_taker_interaction",
    "asset_class",
    "source_file",
)


def _parse_utc(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _candidate_probabilities(record: dict[str, Any]) -> dict[str, float | None]:
    raw = record.get("all_research_probabilities")
    source = raw if isinstance(raw, dict) else {}
    output: dict[str, float | None] = {}
    for key in CANDIDATE_MODEL_KEYS:
        probability = _optional_float(source.get(key))
        output[key] = (
            probability
            if probability is not None and 0.0 <= probability <= 1.0
            else None
        )
    return output


def _highest_candidate_check(
    selected_model: str | None,
    selected_probability: float,
    probabilities: dict[str, float | None],
) -> bool | None:
    valid = {
        key: value for key, value in probabilities.items()
        if value is not None
    }
    if len(valid) != len(CANDIDATE_MODEL_KEYS) or selected_model not in valid:
        return None
    return (
        math.isclose(
            selected_probability, float(valid[selected_model]),
            rel_tol=1e-12, abs_tol=1e-12,
        )
        and math.isclose(
            selected_probability, max(float(value) for value in valid.values()),
            rel_tol=1e-12, abs_tol=1e-12,
        )
    )


def _selected_cross_section_ranks(
    records: list[dict[str, Any]],
) -> dict[int, dict[str, int | None]]:
    """Rank selected records without using any outcome information.

    Ranks are computed from the complete selected cross-section in one frozen
    artifact, so they remain stable while 15m/1H/4H outcomes mature at
    different times.  Deterministic identity fields break exact ties.
    """
    eligible: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if record.get("selected_for_forward_evaluation") is not True:
            continue
        probability = _optional_float(record.get("research_probability"))
        symbol = str(record.get("symbol") or "")
        if probability is None or not 0.0 <= probability <= 1.0 or not symbol:
            continue
        ranking = record.get("ranking_diagnostics") or {}
        eligible.append({
            "index": index,
            "probability": probability,
            "margin": _optional_float(
                ranking.get("selected_vs_opposite_margin")),
            "symbol": symbol,
            "side": str(record.get("side") or ""),
            "horizon": str(record.get("horizon") or ""),
        })
    size = len(eligible)
    result: dict[int, dict[str, int | None]] = {
        int(item["index"]): {
            "selected_probability_rank": None,
            "selected_margin_rank": None,
            "selected_cross_section_size": size,
        }
        for item in eligible
    }
    identity = lambda item: (  # noqa: E731
        str(item["symbol"]), str(item["side"]),
        str(item["horizon"]), int(item["index"]),
    )
    probability_order = sorted(
        eligible,
        key=lambda item: (-float(item["probability"]), *identity(item)),
    )
    for rank, item in enumerate(probability_order, start=1):
        result[int(item["index"])]["selected_probability_rank"] = rank
    margin_order = sorted(
        (item for item in eligible if item["margin"] is not None),
        key=lambda item: (-float(item["margin"]), *identity(item)),
    )
    for rank, item in enumerate(margin_order, start=1):
        result[int(item["index"])]["selected_margin_rank"] = rank
    return result


def _ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(str(path))
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _load_artifacts(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    latest: dict[tuple[str, str, str], tuple[Path, dict[str, Any]]] = {}
    if not root.exists():
        return []
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("artifact_type") != "frozen_multitimeframe_model_shadow":
            continue
        if payload.get("forward_evidence_eligible") is not True:
            continue
        if payload.get("status") != "ready_for_forward_shadow":
            continue
        key = (
            str(payload.get("model_id") or ""),
            str(payload.get("model_parameters_sha256") or ""),
            str(payload.get("cycle_id") or ""),
        )
        if not all(key):
            continue
        previous = latest.get(key)
        if previous is None or str(payload.get("generated_at_utc") or "") >= str(
            previous[1].get("generated_at_utc") or ""
        ):
            latest[key] = (path, payload)
    return [latest[key] for key in sorted(latest)]


def _first_tick(
    con: sqlite3.Connection,
    symbol: str,
    when: datetime,
    *,
    strict: bool,
    max_wait_minutes: int = 20,
    not_after: datetime | None = None,
) -> sqlite3.Row | None:
    operator = ">" if strict else ">="
    upper_bound = when + timedelta(minutes=max_wait_minutes)
    if not_after is not None:
        upper_bound = min(upper_bound, not_after)
    if upper_bound < when:
        return None
    row = con.execute(
        f"SELECT ts,last,bid,ask FROM tick_snapshots WHERE symbol=? AND ts{operator}? "
        "AND ts<=? AND last IS NOT NULL AND last>0 ORDER BY ts LIMIT 1",
        (symbol, _iso(when), _iso(upper_bound)),
    ).fetchone()
    return row


def _positive_price(row: sqlite3.Row, field: str) -> float | None:
    value = _optional_float(row[field])
    return value if value is not None and value > 0 else None


def _wilson(successes: int, total: int) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    z = 1.959963984540054
    p = successes / total
    denominator = 1.0 + z * z / total
    center = (p + z * z / (2.0 * total)) / denominator
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _ece(probabilities: Iterable[float], outcomes: Iterable[bool], bins: int = 10) -> float | None:
    probability = np.asarray(list(probabilities), dtype=float)
    observed = np.asarray(list(outcomes), dtype=float)
    if len(probability) == 0:
        return None
    indices = np.minimum((np.clip(probability, 0.0, 1.0) * bins).astype(int), bins - 1)
    error = 0.0
    for idx in range(bins):
        mask = indices == idx
        if mask.any():
            error += float(mask.mean()) * abs(float(probability[mask].mean()) - float(observed[mask].mean()))
    return error


def _metrics(
    rows: list[dict[str, Any]],
    *,
    offline_gate_pass: bool,
    min_sample: int,
    min_days: int,
    min_cycles: int,
    target_precision: float,
    min_long_labels: int | None = None,
    min_short_labels: int | None = None,
) -> dict[str, Any]:
    n = len(rows)
    successes = sum(bool(row["after_cost_hit"]) for row in rows)
    low, high = _wilson(successes, n)
    precision = successes / n if n else None
    ece = _ece(
        (float(row["research_probability"]) for row in rows),
        (bool(row["after_cost_hit"]) for row in rows),
    )
    distinct_cycles = len({str(row["cycle_id"]) for row in rows})
    distinct_days = len({str(row["cycle_id"])[:10] for row in rows})
    side_counts = Counter(str(row["side"]) for row in rows)
    requirements = {
        "minimum_sample_met": n >= min_sample,
        "minimum_days_met": distinct_days >= min_days,
        "minimum_cycles_met": distinct_cycles >= min_cycles,
        "precision_at_least_target": (
            precision is not None and precision >= target_precision),
        "wilson_95_low_at_least_target": (
            low is not None and low >= target_precision),
        "ece_at_most_5pp": ece is not None and ece <= 0.05,
        "offline_gate_pass": offline_gate_pass,
    }
    measurable_keys = [
        "minimum_sample_met", "minimum_days_met", "minimum_cycles_met",
    ]
    if min_long_labels is not None:
        requirements["minimum_long_labels_met"] = (
            side_counts.get("long", 0) >= min_long_labels)
        measurable_keys.append("minimum_long_labels_met")
    if min_short_labels is not None:
        requirements["minimum_short_labels_met"] = (
            side_counts.get("short", 0) >= min_short_labels)
        measurable_keys.append("minimum_short_labels_met")
    measurable = all(requirements[key] for key in measurable_keys)
    forward_pass = (
        measurable
        and requirements["precision_at_least_target"]
        and requirements["wilson_95_low_at_least_target"]
        and requirements["ece_at_most_5pp"]
    )
    if not measurable:
        status = "NOT_MEASURABLE"
    elif forward_pass and offline_gate_pass:
        status = "MET_FORWARD_SHADOW_REQUIRES_RISK_APPROVAL"
    elif forward_pass:
        status = "FORWARD_MET_BUT_OFFLINE_GATE_NOT_MET"
    else:
        status = "NOT_MET"
    return {
        "n_labeled": n,
        "successes_after_cost": successes,
        "precision_after_cost": precision,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "ece": ece,
        "mean_research_probability": (
            sum(float(row["research_probability"]) for row in rows) / n
            if n else None
        ),
        "mean_signed_return": (
            sum(float(row["signed_return"]) for row in rows) / n if n else None
        ),
        "mean_executable_directional_return": (
            sum(float(row["executable_directional_return"]) for row in rows) / n
            if n else None
        ),
        "mean_signed_return_after_cost": (
            sum(float(row["signed_return_after_cost"]) for row in rows) / n
            if n else None
        ),
        "distinct_cycles": distinct_cycles,
        "distinct_days": distinct_days,
        "side_counts": dict(sorted(side_counts.items())),
        "horizon_counts": dict(sorted(Counter(str(row["horizon"]) for row in rows).items())),
        "requirements": requirements,
        "status": status,
        "production_threshold_change_allowed": False,
    }


def _margin_band(value: float) -> str:
    if value < 0.02:
        return "lt_2pp"
    if value < 0.05:
        return "2_to_5pp"
    if value < 0.10:
        return "5_to_10pp"
    return "gte_10pp"


def _diagnostic_metrics(
    rows: list[dict[str, Any]],
    *,
    offline_gate_pass: bool,
    min_sample: int,
    min_days: int,
    min_cycles: int,
    target_precision: float,
) -> dict[str, Any]:
    by_side = []
    for side in ("long", "short"):
        subset = [row for row in rows if row.get("side") == side]
        by_side.append({
            "side": side,
            **_metrics(
                subset,
                offline_gate_pass=offline_gate_pass,
                min_sample=min_sample,
                min_days=min_days,
                min_cycles=min_cycles,
                target_precision=target_precision,
            ),
        })

    by_horizon_side = []
    for horizon in HORIZON_MINUTES:
        for side in ("long", "short"):
            subset = [
                row for row in rows
                if row.get("horizon") == horizon and row.get("side") == side
            ]
            by_horizon_side.append({
                "horizon": horizon,
                "side": side,
                **_metrics(
                    subset,
                    offline_gate_pass=offline_gate_pass,
                    min_sample=min_sample,
                    min_days=min_days,
                    min_cycles=min_cycles,
                    target_precision=target_precision,
                ),
            })

    with_margin = [
        row for row in rows
        if row.get("selected_vs_opposite_margin") is not None
    ]
    by_votes = []
    for votes in (1, 2, 3):
        subset = [
            row for row in with_margin
            if row.get("selected_side_horizon_votes") == votes
        ]
        by_votes.append({
            "selected_side_horizon_votes": votes,
            **_metrics(
                subset,
                offline_gate_pass=offline_gate_pass,
                min_sample=min_sample,
                min_days=min_days,
                min_cycles=min_cycles,
                target_precision=target_precision,
            ),
        })

    by_margin = []
    for band in ("lt_2pp", "2_to_5pp", "5_to_10pp", "gte_10pp"):
        subset = [
            row for row in with_margin
            if _margin_band(float(row["selected_vs_opposite_margin"])) == band
        ]
        by_margin.append({
            "selected_vs_opposite_margin_band": band,
            **_metrics(
                subset,
                offline_gate_pass=offline_gate_pass,
                min_sample=min_sample,
                min_days=min_days,
                min_cycles=min_cycles,
                target_precision=target_precision,
            ),
        })

    with_contract_flag = [
        row for row in rows
        if row.get("contract_statistics_available") is not None
    ]
    by_contract = []
    for available in (False, True):
        subset = [
            row for row in with_contract_flag
            if row["contract_statistics_available"] is available
        ]
        by_contract.append({
            "contract_statistics_available": available,
            **_metrics(
                subset,
                offline_gate_pass=offline_gate_pass,
                min_sample=min_sample,
                min_days=min_days,
                min_cycles=min_cycles,
                target_precision=target_precision,
            ),
        })

    asset_classes = sorted({
        str(row["asset_class"])
        for row in rows if row.get("asset_class") is not None
    })
    by_asset_class = [{
        "asset_class": asset_class,
        **_metrics(
            [row for row in rows if row.get("asset_class") == asset_class],
            offline_gate_pass=offline_gate_pass,
            min_sample=min_sample,
            min_days=min_days,
            min_cycles=min_cycles,
            target_precision=target_precision,
        ),
    } for asset_class in asset_classes]

    by_selected_model = [{
        "selected_model": model_key,
        **_metrics(
            [row for row in rows if row.get("selected_model") == model_key],
            offline_gate_pass=offline_gate_pass,
            min_sample=min_sample,
            min_days=min_days,
            min_cycles=min_cycles,
            target_precision=target_precision,
        ),
    } for model_key in CANDIDATE_MODEL_KEYS]

    candidate_vector_rows = [
        row for row in rows
        if row.get("candidate_probability_count") == len(CANDIDATE_MODEL_KEYS)
    ]
    highest_check_rows = [
        row for row in rows
        if row.get("selected_is_highest_candidate_probability") is not None
    ]
    highest_check_passes = sum(
        row["selected_is_highest_candidate_probability"] is True
        for row in highest_check_rows
    )

    def concentration(subset: list[dict[str, Any]]) -> dict[str, Any]:
        counts = Counter(str(row["symbol"]) for row in subset)
        total = len(subset)
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return {
            "n_selected": total,
            "distinct_symbols": len(counts),
            "maximum_symbol_share": (
                ranked[0][1] / total if total and ranked else None),
            "symbol_selection_hhi": (
                sum((count / total) ** 2 for count in counts.values())
                if total else None
            ),
            "top_symbols_by_selection_count": [{
                "symbol": symbol,
                "selection_count": count,
                "selection_share": count / total,
                **_metrics(
                    [row for row in subset if str(row["symbol"]) == symbol],
                    offline_gate_pass=offline_gate_pass,
                    min_sample=min_sample,
                    min_days=min_days,
                    min_cycles=min_cycles,
                    target_precision=target_precision,
                ),
            } for symbol, count in ranked[:10]],
        }

    def top_k_metrics(rank_field: str) -> list[dict[str, Any]]:
        output = []
        for top_k in (1, 3, 5, 10):
            subset = [
                row for row in rows
                if row.get(rank_field) is not None
                and int(row[rank_field]) <= top_k
            ]
            output.append({
                "top_k": top_k,
                "rank_field": rank_field,
                **_metrics(
                    subset,
                    offline_gate_pass=offline_gate_pass,
                    min_sample=min_sample,
                    min_days=min_days,
                    min_cycles=min_cycles,
                    target_precision=target_precision,
                ),
            })
        return output

    return {
        "ranking_diagnostic_rows": len(with_margin),
        "ranking_diagnostic_coverage_rate": (
            len(with_margin) / len(rows) if rows else None),
        "contract_feature_flag_rows": len(with_contract_flag),
        "contract_feature_flag_coverage_rate": (
            len(with_contract_flag) / len(rows) if rows else None),
        "candidate_probability_vector_rows": len(candidate_vector_rows),
        "candidate_probability_vector_coverage_rate": (
            len(candidate_vector_rows) / len(rows) if rows else None),
        "highest_candidate_selection_check_rows": len(highest_check_rows),
        "highest_candidate_selection_pass_rows": highest_check_passes,
        "highest_candidate_selection_pass_rate": (
            highest_check_passes / len(highest_check_rows)
            if highest_check_rows else None
        ),
        "by_side": by_side,
        "by_horizon_side": by_horizon_side,
        "by_asset_class": by_asset_class,
        "by_selected_model": by_selected_model,
        "by_selected_side_horizon_votes": by_votes,
        "by_selected_vs_opposite_margin_band": by_margin,
        "by_contract_statistics_availability": by_contract,
        "by_selected_probability_top_k": top_k_metrics(
            "selected_probability_rank"),
        "by_selected_margin_top_k": top_k_metrics(
            "selected_margin_rank"),
        "selection_concentration": {
            "all_selected": concentration(rows),
            "selected_probability_top_1": concentration([
                row for row in rows
                if row.get("selected_probability_rank") == 1
            ]),
        },
    }


def evaluate(
    *,
    shadow_root: Path,
    market_db: Path,
    as_of_utc: datetime,
    cost_bps: float = 20.0,
    min_sample: int = 100,
    min_days: int = 5,
    min_cycles: int = 100,
    min_long_labels: int = 30,
    min_short_labels: int = 30,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    artifacts = _load_artifacts(shadow_root)
    con = _ro(market_db)
    labels: list[dict[str, Any]] = []
    selected_records = 0
    invalid_records = 0
    missing_executable_price_records = 0
    crossed_executable_price_records = 0
    model_offline_gate: dict[tuple[str, str], bool] = {}
    try:
        for source_path, payload in artifacts:
            model_key = (
                str(payload["model_id"]),
                str(payload["model_parameters_sha256"]),
            )
            model_offline_gate[model_key] = bool(
                (payload.get("offline_acceptance") or {}).get("gate_pass")
            )
            generated = _parse_utc(str(payload["generated_at_utc"]))
            if generated > as_of_utc:
                continue
            artifact_records = list(payload.get("records") or [])
            cross_section_ranks = _selected_cross_section_ranks(
                artifact_records)
            for record_index, record in enumerate(artifact_records):
                if record.get("selected_for_forward_evaluation") is not True:
                    continue
                selected_records += 1
                try:
                    probability = float(record["research_probability"])
                    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                        raise ValueError("invalid probability")
                    side = str(record["side"])
                    horizon = str(record["horizon"])
                    if side not in {"long", "short"} or horizon not in HORIZON_MINUTES:
                        raise ValueError("invalid side or horizon")
                    availability = max(
                        generated,
                        _parse_utc(str(record["feature_decision_ts_utc"])),
                        _parse_utc(str(record["signal_available_at_utc"])),
                    )
                    symbol = str(record["symbol"])
                except (KeyError, TypeError, ValueError):
                    invalid_records += 1
                    continue
                entry = _first_tick(
                    con, symbol, availability, strict=True,
                    not_after=as_of_utc,
                )
                if entry is None:
                    continue
                entry_ts = str(entry["ts"])
                entry_last = _positive_price(entry, "last")
                entry_field = "ask" if side == "long" else "bid"
                entry_executable = _positive_price(entry, entry_field)
                entry_bid = _positive_price(entry, "bid")
                entry_ask = _positive_price(entry, "ask")
                if None in (entry_last, entry_executable, entry_bid, entry_ask):
                    missing_executable_price_records += 1
                    continue
                if float(entry_ask) < float(entry_bid):
                    crossed_executable_price_records += 1
                    continue
                entry_dt = _parse_utc(entry_ts)
                target = entry_dt + timedelta(minutes=HORIZON_MINUTES[horizon])
                if target > as_of_utc:
                    continue
                outcome = _first_tick(
                    con, symbol, target, strict=False,
                    not_after=as_of_utc,
                )
                if outcome is None:
                    continue
                outcome_ts = str(outcome["ts"])
                outcome_last = _positive_price(outcome, "last")
                outcome_field = "bid" if side == "long" else "ask"
                outcome_executable = _positive_price(outcome, outcome_field)
                outcome_bid = _positive_price(outcome, "bid")
                outcome_ask = _positive_price(outcome, "ask")
                if None in (
                    outcome_last, outcome_executable, outcome_bid, outcome_ask,
                ):
                    missing_executable_price_records += 1
                    continue
                if float(outcome_ask) < float(outcome_bid):
                    crossed_executable_price_records += 1
                    continue
                direction = 1.0 if side == "long" else -1.0
                last_directional_return = direction * (
                    float(outcome_last) / float(entry_last) - 1.0
                )
                executable_directional_return = direction * (
                    float(outcome_executable) / float(entry_executable) - 1.0
                )
                signed_return_after_cost = (
                    executable_directional_return - cost_bps / 10_000.0
                )
                ranking = record.get("ranking_diagnostics") or {}
                future = record.get("future_retraining_features") or {}
                selected_model = _optional_text(record.get("selected_model"))
                candidate_probabilities = _candidate_probabilities(record)
                candidate_probability_count = sum(
                    value is not None
                    for value in candidate_probabilities.values()
                )
                cross_section = cross_section_ranks.get(record_index) or {}
                labels.append({
                    "model_id": model_key[0],
                    "model_parameters_sha256": model_key[1],
                    "cycle_id": str(payload["cycle_id"]),
                    "symbol": symbol,
                    "side": side,
                    "horizon": horizon,
                    "research_probability": probability,
                    "selected_probability_rank": cross_section.get(
                        "selected_probability_rank"),
                    "selected_margin_rank": cross_section.get(
                        "selected_margin_rank"),
                    "selected_cross_section_size": cross_section.get(
                        "selected_cross_section_size"),
                    "signal_available_at_utc": _iso(availability),
                    "entry_tick_ts_utc": entry_ts,
                    # Legacy last-price fields remain for diagnostic continuity.
                    "entry_price": entry_last,
                    "entry_last": entry_last,
                    "entry_executable": entry_executable,
                    "entry_price_source": entry_field,
                    "outcome_tick_ts_utc": outcome_ts,
                    "outcome_price": outcome_last,
                    "outcome_last": outcome_last,
                    "outcome_executable": outcome_executable,
                    "outcome_price_source": outcome_field,
                    "signed_return": last_directional_return,
                    "last_directional_return": last_directional_return,
                    "executable_directional_return": executable_directional_return,
                    "signed_return_after_cost": signed_return_after_cost,
                    "after_cost_hit": signed_return_after_cost > 0.0,
                    "runner_up_probability": _optional_float(
                        ranking.get("runner_up_probability")),
                    "top_vs_runner_up_margin": _optional_float(
                        ranking.get("top_vs_runner_up_margin")),
                    "opposite_side_same_horizon_probability": _optional_float(
                        ranking.get("opposite_side_same_horizon_probability")),
                    "selected_vs_opposite_margin": _optional_float(
                        ranking.get("selected_vs_opposite_margin")),
                    "selected_side_horizon_votes": _optional_int(
                        ranking.get("selected_side_horizon_votes")),
                    "selected_side_unanimous": _optional_bool(
                        ranking.get("selected_side_unanimous")),
                    "selected_side_mean_margin": _optional_float(
                        ranking.get("selected_side_mean_margin")),
                    "selected_side_min_margin": _optional_float(
                        ranking.get("selected_side_min_margin")),
                    "selected_model": selected_model,
                    "candidate_probability_count": candidate_probability_count,
                    "selected_is_highest_candidate_probability": (
                        _highest_candidate_check(
                            selected_model, probability,
                            candidate_probabilities,
                        )),
                    **{
                        CANDIDATE_PROBABILITY_FIELDS[key]: value
                        for key, value in candidate_probabilities.items()
                    },
                    "contract_statistics_available": _optional_bool(
                        future.get("contract_statistics_available")),
                    "contract_statistics_source_ts_utc": future.get(
                        "contract_statistics_source_ts_utc"),
                    "contract_statistics_available_at_utc": future.get(
                        "contract_statistics_available_at_utc"),
                    "contract_oi_log_usd": _optional_float(
                        future.get("contract_oi_log_usd")),
                    "contract_oi_log_change_15m": _optional_float(
                        future.get("contract_oi_log_change_15m")),
                    "contract_taker_total_log_usd": _optional_float(
                        future.get("contract_taker_total_log_usd")),
                    "contract_taker_buy_centered": _optional_float(
                        future.get("contract_taker_buy_centered")),
                    "contract_oi_taker_interaction": _optional_float(
                        future.get("contract_oi_taker_interaction")),
                    "asset_class": _optional_text(record.get("asset_class")),
                    "source_file": str(source_path.resolve()),
                })
    finally:
        con.close()

    # 前向校准门数值：按预注册激活边界解析（边界前 0.90、边界起 0.80）；点精度
    # 与 Wilson 95% 下界共用同一数值，永远同步移动。
    target_precision = thresholds.shadow_target_precision(as_of_utc)
    model_results: list[dict[str, Any]] = []
    by_model: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in labels:
        by_model[(row["model_id"], row["model_parameters_sha256"])].append(row)
    for model_key in sorted(model_offline_gate):
        rows = by_model.get(model_key, [])
        overall = _metrics(
            rows,
            offline_gate_pass=model_offline_gate[model_key],
            min_sample=min_sample,
            min_days=min_days,
            min_cycles=min_cycles,
            target_precision=target_precision,
            min_long_labels=min_long_labels,
            min_short_labels=min_short_labels,
        )
        horizon_results = []
        for horizon in HORIZON_MINUTES:
            subset = [row for row in rows if row["horizon"] == horizon]
            horizon_results.append({
                "horizon": horizon,
                **_metrics(
                    subset,
                    offline_gate_pass=model_offline_gate[model_key],
                    min_sample=min_sample,
                    min_days=min_days,
                    min_cycles=min_cycles,
                    target_precision=target_precision,
                ),
            })
        day_results = []
        for day in sorted({str(row["cycle_id"])[:10] for row in rows}):
            subset = [
                row for row in rows
                if str(row["cycle_id"])[:10] == day
            ]
            day_results.append({
                "day_cst": day,
                "diagnostic_only": True,
                **_metrics(
                    subset,
                    offline_gate_pass=model_offline_gate[model_key],
                    min_sample=min_sample,
                    min_days=min_days,
                    min_cycles=min_cycles,
                    target_precision=target_precision,
                ),
            })
        model_results.append({
            "model_id": model_key[0],
            "model_parameters_sha256": model_key[1],
            "overall": overall,
            "by_horizon": horizon_results,
            "by_day": day_results,
            "diagnostics": _diagnostic_metrics(
                rows,
                offline_gate_pass=model_offline_gate[model_key],
                min_sample=min_sample,
                min_days=min_days,
                min_cycles=min_cycles,
                target_precision=target_precision,
            ),
        })

    payload = {
        "schema_version": 2,
        "label_schema_version": 3,
        "artifact_type": "frozen_multitimeframe_model_shadow_evaluation",
        "generated_at_utc": _iso(datetime.now(UTC)),
        "as_of_utc": _iso(as_of_utc),
        "shadow_root": str(shadow_root.resolve()),
        "artifacts_loaded": len(artifacts),
        "selected_records": selected_records,
        "invalid_records": invalid_records,
        "missing_executable_price_records": missing_executable_price_records,
        "crossed_executable_price_records": crossed_executable_price_records,
        "labels_written": len(labels),
        "round_trip_cost_bps": cost_bps,
        "acceptance_contract": {
            "target_precision": target_precision,
            "minimum_wilson_95_lower_bound": target_precision,
            "target_precision_migration": (
                thresholds.shadow_migration_facts(as_of_utc)),
            "maximum_ece": 0.05,
            "minimum_sample": min_sample,
            "minimum_days": min_days,
            "minimum_distinct_cycles": min_cycles,
            "minimum_long_labels": min_long_labels,
            "minimum_short_labels": min_short_labels,
            "cycle_diversity_semantics": (
                "distinct scheduled signal cycles; overlapping horizon windows "
                "are not claimed to be statistically independent"
            ),
            "entry": "first ticker strictly after actual signal availability",
            "outcome": "first ticker at or after entry plus selected horizon",
            "execution_prices": "long ask->bid; short bid->ask; no last fallback",
            "cost_hurdle_bps_after_observed_spread": cost_bps,
        },
        "last_price_fields_are_diagnostic_only": True,
        "feature_diagnostic_contract": {
            "source": "frozen point-in-time shadow snapshots only",
            "outcome_free_slices": (
                "asset class, availability, and fixed sign bands"),
            "temporal_slice": (
                "day_cst is preregistered diagnostic-only stability evidence; "
                "it never changes the overall production gate"),
            "production_gate": False,
        },
        "models": model_results,
        "confidence_claim_allowed": False,
        "production_threshold_change_allowed": False,
        "production_execution_authorized": False,
        "production_mutation": False,
        "orders_placed": 0,
    }
    return payload, labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--labels-out", type=Path)
    parser.add_argument("--as-of", help="ISO-8601 UTC; default now")
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--min-sample", type=int, default=100)
    parser.add_argument("--min-days", type=int, default=5)
    parser.add_argument("--min-cycles", type=int, default=100)
    parser.add_argument("--min-long-labels", type=int, default=30)
    parser.add_argument("--min-short-labels", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        if (
            args.cost_bps < 0
            or min(args.min_sample, args.min_days, args.min_cycles) <= 0
            or min(args.min_long_labels, args.min_short_labels) < 0
        ):
            raise ValueError("cost and acceptance thresholds are invalid")
        payload, labels = evaluate(
            shadow_root=args.shadow_root,
            market_db=args.market_db,
            as_of_utc=_parse_utc(args.as_of) if args.as_of else datetime.now(UTC),
            cost_bps=args.cost_bps,
            min_sample=args.min_sample,
            min_days=args.min_days,
            min_cycles=args.min_cycles,
            min_long_labels=args.min_long_labels,
            min_short_labels=args.min_short_labels,
        )
        calibration._atomic_json(args.json_out, payload)
        if args.labels_out:
            frame = pd.DataFrame(labels, columns=LABEL_COLUMNS)
            calibration._atomic_csv(args.labels_out, frame)
        print(json.dumps({
            "ok": True,
            "json_out": str(args.json_out.resolve()),
            "labels_out": str(args.labels_out.resolve()) if args.labels_out else None,
            "artifacts_loaded": payload["artifacts_loaded"],
            "labels_written": payload["labels_written"],
            "model_statuses": [item["overall"]["status"] for item in payload["models"]],
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
