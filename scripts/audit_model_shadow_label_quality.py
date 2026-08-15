#!/usr/bin/env python3
"""Independently audit frozen-model forward labels against raw market ticks.

The evaluator under audit is never imported.  This script reconstructs the
eligible artifacts, time anchors, executable prices, returns and aggregate
metrics from the frozen JSON inputs plus a read-only market database.  It only
writes an atomic quality receipt and cannot authorize production execution.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import _acceptance_thresholds as thresholds


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
    "after_cost_hit", "runner_up_probability", "top_vs_runner_up_margin",
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
BOOL_FIELDS = {
    "after_cost_hit", "selected_side_unanimous",
    "selected_is_highest_candidate_probability",
    "contract_statistics_available",
}
INT_FIELDS = {
    "selected_side_horizon_votes", "selected_probability_rank",
    "selected_margin_rank", "selected_cross_section_size",
    "candidate_probability_count",
}
PATH_FIELDS = {"source_file"}
KEY_FIELDS = (
    "model_id", "model_parameters_sha256", "cycle_id", "symbol",
)


def _parse_utc(value: Any) -> datetime:
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
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() == "true":
            return True
        if value.strip().lower() == "false":
            return False
    return None


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _load_artifacts(
    root: Path,
) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    latest: dict[tuple[str, str, str], tuple[Path, dict[str, Any]]] = {}
    unreadable: list[str] = []
    if not root.exists():
        return [], [f"missing_shadow_root:{root}"]
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            unreadable.append(f"{path}:{type(exc).__name__}")
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
            unreadable.append(f"{path}:missing_model_key")
            continue
        previous = latest.get(key)
        if previous is None or str(payload.get("generated_at_utc") or "") >= str(
            previous[1].get("generated_at_utc") or ""
        ):
            latest[key] = (path, payload)
    return [latest[key] for key in sorted(latest)], unreadable


def _first_tick(
    con: sqlite3.Connection,
    symbol: str,
    when: datetime,
    *,
    strict: bool,
    as_of: datetime,
    max_wait_minutes: int = 20,
) -> sqlite3.Row | None:
    upper = min(when + timedelta(minutes=max_wait_minutes), as_of)
    if upper < when:
        return None
    operator = ">" if strict else ">="
    return con.execute(
        "SELECT ts,last,bid,ask FROM tick_snapshots WHERE symbol=? "
        f"AND ts{operator}? AND ts<=? AND last IS NOT NULL AND last>0 "
        "ORDER BY ts LIMIT 1",
        (symbol, _iso(when), _iso(upper)),
    ).fetchone()


def _positive(row: sqlite3.Row, field: str) -> float | None:
    value = _optional_float(row[field])
    return value if value is not None and value > 0 else None


def _selected_cross_section_ranks(
    records: list[dict[str, Any]],
) -> dict[int, dict[str, int | None]]:
    """Independently reconstruct outcome-free frozen cross-section ranks."""
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
    for rank, item in enumerate(sorted(
        eligible,
        key=lambda item: (-float(item["probability"]), *identity(item)),
    ), start=1):
        result[int(item["index"])]["selected_probability_rank"] = rank
    for rank, item in enumerate(sorted(
        (item for item in eligible if item["margin"] is not None),
        key=lambda item: (-float(item["margin"]), *identity(item)),
    ), start=1):
        result[int(item["index"])]["selected_margin_rank"] = rank
    return result


def _expected_labels(
    artifacts: list[tuple[Path, dict[str, Any]]],
    market_db: Path,
    *,
    as_of: datetime,
    cost_bps: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[tuple[str, str], set[bool]]]:
    con = sqlite3.connect(
        market_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=20,
    )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    rows: list[dict[str, Any]] = []
    counters = Counter()
    model_gates: dict[tuple[str, str], set[bool]] = defaultdict(set)
    try:
        for source_path, payload in artifacts:
            model_key = (
                str(payload["model_id"]),
                str(payload["model_parameters_sha256"]),
            )
            model_gates[model_key].add(bool(
                (payload.get("offline_acceptance") or {}).get("gate_pass")
            ))
            generated = _parse_utc(payload["generated_at_utc"])
            if generated > as_of:
                continue
            artifact_records = list(payload.get("records") or [])
            cross_section_ranks = _selected_cross_section_ranks(
                artifact_records)
            for record_index, record in enumerate(artifact_records):
                if record.get("selected_for_forward_evaluation") is not True:
                    continue
                counters["selected_records"] += 1
                try:
                    probability = float(record["research_probability"])
                    if not math.isfinite(probability) or not 0 <= probability <= 1:
                        raise ValueError("probability")
                    side = str(record["side"])
                    horizon = str(record["horizon"])
                    symbol = str(record["symbol"])
                    if (
                        side not in {"long", "short"}
                        or horizon not in HORIZON_MINUTES
                        or not symbol
                    ):
                        raise ValueError("direction")
                    availability = max(
                        generated,
                        _parse_utc(record["feature_decision_ts_utc"]),
                        _parse_utc(record["signal_available_at_utc"]),
                    )
                except (KeyError, TypeError, ValueError):
                    counters["invalid_records"] += 1
                    continue
                entry = _first_tick(
                    con, symbol, availability, strict=True, as_of=as_of,
                )
                if entry is None:
                    counters["entry_missing_or_immature"] += 1
                    continue
                entry_ts = str(entry["ts"])
                entry_last = _positive(entry, "last")
                entry_bid = _positive(entry, "bid")
                entry_ask = _positive(entry, "ask")
                entry_field = "ask" if side == "long" else "bid"
                entry_exec = entry_ask if side == "long" else entry_bid
                if None in (entry_last, entry_bid, entry_ask, entry_exec):
                    counters["missing_executable_price_records"] += 1
                    continue
                if float(entry_ask) < float(entry_bid):
                    counters["crossed_executable_price_records"] += 1
                    continue
                entry_dt = _parse_utc(entry_ts)
                target = entry_dt + timedelta(minutes=HORIZON_MINUTES[horizon])
                if target > as_of:
                    counters["horizon_immature"] += 1
                    continue
                outcome = _first_tick(
                    con, symbol, target, strict=False, as_of=as_of,
                )
                if outcome is None:
                    counters["outcome_missing"] += 1
                    continue
                outcome_ts = str(outcome["ts"])
                outcome_last = _positive(outcome, "last")
                outcome_bid = _positive(outcome, "bid")
                outcome_ask = _positive(outcome, "ask")
                outcome_field = "bid" if side == "long" else "ask"
                outcome_exec = outcome_bid if side == "long" else outcome_ask
                if None in (outcome_last, outcome_bid, outcome_ask, outcome_exec):
                    counters["missing_executable_price_records"] += 1
                    continue
                if float(outcome_ask) < float(outcome_bid):
                    counters["crossed_executable_price_records"] += 1
                    continue
                direction = 1.0 if side == "long" else -1.0
                last_return = direction * (
                    float(outcome_last) / float(entry_last) - 1.0
                )
                executable_return = direction * (
                    float(outcome_exec) / float(entry_exec) - 1.0
                )
                after_cost = executable_return - cost_bps / 10_000.0
                ranking = record.get("ranking_diagnostics") or {}
                future = record.get("future_retraining_features") or {}
                selected_model = _optional_text(record.get("selected_model"))
                candidate_probabilities = _candidate_probabilities(record)
                candidate_probability_count = sum(
                    value is not None
                    for value in candidate_probabilities.values()
                )
                cross_section = cross_section_ranks.get(record_index) or {}
                rows.append({
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
                    "entry_price": entry_last,
                    "entry_last": entry_last,
                    "entry_executable": entry_exec,
                    "entry_price_source": entry_field,
                    "outcome_tick_ts_utc": outcome_ts,
                    "outcome_price": outcome_last,
                    "outcome_last": outcome_last,
                    "outcome_executable": outcome_exec,
                    "outcome_price_source": outcome_field,
                    "signed_return": last_return,
                    "last_directional_return": last_return,
                    "executable_directional_return": executable_return,
                    "signed_return_after_cost": after_cost,
                    "after_cost_hit": after_cost > 0,
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
    return rows, dict(counters), model_gates


def _key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(field) or "") for field in KEY_FIELDS)


def _wilson(successes: int, total: int) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(
        (p * (1 - p) + z * z / (4 * total)) / total
    ) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _ece(rows: Iterable[dict[str, Any]], bins: int = 10) -> float | None:
    values = list(rows)
    if not values:
        return None
    total = len(values)
    error = 0.0
    for index in range(bins):
        bucket = [row for row in values if min(
            int(float(row["research_probability"]) * bins), bins - 1
        ) == index]
        if bucket:
            mean_probability = sum(
                float(row["research_probability"]) for row in bucket
            ) / len(bucket)
            mean_outcome = sum(
                bool(row["after_cost_hit"]) for row in bucket
            ) / len(bucket)
            error += len(bucket) / total * abs(mean_probability - mean_outcome)
    return error


def _metrics(
    rows: list[dict[str, Any]],
    *,
    offline_gate: bool,
    min_sample: int,
    min_days: int,
    min_cycles: int,
    target_precision: float,
    min_long_labels: int | None = None,
    min_short_labels: int | None = None,
) -> dict[str, Any]:
    n = len(rows)
    successes = sum(bool(row["after_cost_hit"]) for row in rows)
    precision = successes / n if n else None
    low, high = _wilson(successes, n)
    ece = _ece(rows)
    cycles = len({str(row["cycle_id"]) for row in rows})
    days = len({str(row["cycle_id"])[:10] for row in rows})
    side_counts = Counter(str(row["side"]) for row in rows)
    requirements = {
        "minimum_sample_met": n >= min_sample,
        "minimum_days_met": days >= min_days,
        "minimum_cycles_met": cycles >= min_cycles,
        "precision_at_least_target": (
            precision is not None and precision >= target_precision),
        "wilson_95_low_at_least_target": (
            low is not None and low >= target_precision),
        "ece_at_most_5pp": ece is not None and ece <= 0.05,
        "offline_gate_pass": offline_gate,
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
    forward = (
        measurable
        and requirements["precision_at_least_target"]
        and requirements["wilson_95_low_at_least_target"]
        and requirements["ece_at_most_5pp"]
    )
    status = (
        "NOT_MEASURABLE" if not measurable
        else "MET_FORWARD_SHADOW_REQUIRES_RISK_APPROVAL"
        if forward and offline_gate
        else "FORWARD_MET_BUT_OFFLINE_GATE_NOT_MET"
        if forward
        else "NOT_MET"
    )
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
        "distinct_cycles": cycles,
        "distinct_days": days,
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
    offline_gate: bool,
    min_sample: int,
    min_days: int,
    min_cycles: int,
    target_precision: float,
) -> dict[str, Any]:
    def scoped(subset: list[dict[str, Any]]) -> dict[str, Any]:
        return _metrics(
            subset,
            offline_gate=offline_gate,
            min_sample=min_sample,
            min_days=min_days,
            min_cycles=min_cycles,
            target_precision=target_precision,
        )

    by_side = [{
        "side": side,
        **scoped([row for row in rows if row.get("side") == side]),
    } for side in ("long", "short")]
    by_horizon_side = [{
        "horizon": horizon,
        "side": side,
        **scoped([
            row for row in rows
            if row.get("horizon") == horizon and row.get("side") == side
        ]),
    } for horizon in HORIZON_MINUTES for side in ("long", "short")]

    with_margin = [
        row for row in rows
        if row.get("selected_vs_opposite_margin") is not None
    ]
    by_votes = [{
        "selected_side_horizon_votes": votes,
        **scoped([
            row for row in with_margin
            if row.get("selected_side_horizon_votes") == votes
        ]),
    } for votes in (1, 2, 3)]
    by_margin = [{
        "selected_vs_opposite_margin_band": band,
        **scoped([
            row for row in with_margin
            if _margin_band(float(row["selected_vs_opposite_margin"])) == band
        ]),
    } for band in ("lt_2pp", "2_to_5pp", "5_to_10pp", "gte_10pp")]

    with_contract_flag = [
        row for row in rows
        if row.get("contract_statistics_available") is not None
    ]
    by_contract = [{
        "contract_statistics_available": available,
        **scoped([
            row for row in with_contract_flag
            if row["contract_statistics_available"] is available
        ]),
    } for available in (False, True)]

    asset_classes = sorted({
        str(row["asset_class"])
        for row in rows if row.get("asset_class") is not None
    })
    by_asset_class = [{
        "asset_class": asset_class,
        **scoped([
            row for row in rows if row.get("asset_class") == asset_class
        ]),
    } for asset_class in asset_classes]

    by_selected_model = [{
        "selected_model": model_key,
        **scoped([
            row for row in rows if row.get("selected_model") == model_key
        ]),
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
                **scoped([
                    row for row in subset if str(row["symbol"]) == symbol
                ]),
            } for symbol, count in ranked[:10]],
        }

    def top_k_metrics(rank_field: str) -> list[dict[str, Any]]:
        return [{
            "top_k": top_k,
            "rank_field": rank_field,
            **scoped([
                row for row in rows
                if row.get(rank_field) is not None
                and int(row[rank_field]) <= top_k
            ]),
        } for top_k in (1, 3, 5, 10)]

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
        "by_selected_margin_top_k": top_k_metrics("selected_margin_rank"),
        "selection_concentration": {
            "all_selected": concentration(rows),
            "selected_probability_top_1": concentration([
                row for row in rows
                if row.get("selected_probability_rank") == 1
            ]),
        },
    }


def _close(left: Any, right: Any, tolerance: float = 1e-12) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        a, b = float(left), float(right)
    except (TypeError, ValueError):
        return False
    return math.isfinite(a) and math.isfinite(b) and math.isclose(
        a, b, rel_tol=tolerance, abs_tol=tolerance,
    )


def _row_matches(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for field in LABEL_COLUMNS:
        wanted = expected.get(field)
        observed: Any = actual.get(field)
        if wanted is None:
            if observed not in (None, ""):
                mismatches.append(field)
        elif field in BOOL_FIELDS:
            if _optional_bool(observed) is not wanted:
                mismatches.append(field)
        elif field in INT_FIELDS:
            if _optional_int(observed) != wanted:
                mismatches.append(field)
        elif isinstance(wanted, float):
            if not _close(wanted, observed):
                mismatches.append(field)
        elif field in PATH_FIELDS:
            try:
                if Path(str(observed)).resolve() != Path(str(wanted)).resolve():
                    mismatches.append(field)
            except OSError:
                mismatches.append(field)
        elif str(observed) != str(wanted):
            mismatches.append(field)
    return mismatches


def _metrics_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    scalar = (
        "n_labeled", "successes_after_cost", "precision_after_cost",
        "wilson_95_low", "wilson_95_high", "ece",
        "mean_research_probability", "mean_signed_return",
        "mean_executable_directional_return", "mean_signed_return_after_cost",
        "distinct_cycles", "distinct_days",
    )
    for field in scalar:
        wanted, observed = expected.get(field), actual.get(field)
        if isinstance(wanted, (int, float)) and not isinstance(wanted, bool):
            if not _close(wanted, observed):
                return False
        elif wanted != observed:
            return False
    return all(
        expected.get(field) == actual.get(field)
        for field in ("side_counts", "horizon_counts", "requirements", "status",
                      "production_threshold_change_allowed")
    )


def _structured_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and set(expected) == set(actual)
            and all(_structured_match(expected[key], actual[key]) for key in expected)
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                _structured_match(wanted, observed)
                for wanted, observed in zip(expected, actual)
            )
        )
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)):
        return _close(expected, actual)
    return expected == actual


def audit(
    *,
    evaluation_path: Path,
    labels_path: Path,
    shadow_root: Path,
    market_db: Path,
) -> dict[str, Any]:
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    as_of = _parse_utc(evaluation["as_of_utc"])
    cost_bps = float(evaluation["round_trip_cost_bps"])
    contract = evaluation.get("acceptance_contract") or {}
    artifacts, unreadable = _load_artifacts(shadow_root)
    expected, counters, model_gates = _expected_labels(
        artifacts, market_db, as_of=as_of, cost_bps=cost_bps,
    )
    with labels_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        actual = list(reader)
        actual_columns = tuple(reader.fieldnames or ())

    expected_keys = [_key(row) for row in expected]
    actual_keys = [_key(row) for row in actual]
    expected_map = {_key(row): row for row in expected}
    actual_map = {_key(row): row for row in actual}
    duplicate_expected = sorted(
        [key for key, count in Counter(expected_keys).items() if count > 1]
    )
    duplicate_actual = sorted(
        [key for key, count in Counter(actual_keys).items() if count > 1]
    )
    row_mismatches: list[dict[str, Any]] = []
    for key in sorted(set(expected_map) & set(actual_map)):
        fields = _row_matches(expected_map[key], actual_map[key])
        if fields:
            row_mismatches.append({"key": list(key), "fields": fields})

    min_sample = int(contract.get("minimum_sample", 100))
    min_days = int(contract.get("minimum_days", 5))
    min_cycles = int(contract.get("minimum_distinct_cycles", 100))
    # 用被审计工件自报的 target_precision 重建（与 min_sample 等同款姿势），
    # 「不得弱化」由下方独立地板校验负责——地板本身来自预注册激活边界。
    declared_precision = _optional_float(contract.get("target_precision"))
    target_precision = (
        declared_precision if declared_precision is not None
        else thresholds.shadow_target_precision(as_of)
    )
    min_long_labels = int(contract.get("minimum_long_labels", 30))
    min_short_labels = int(contract.get("minimum_short_labels", 30))
    actual_models = {
        (str(item.get("model_id")), str(item.get("model_parameters_sha256"))): item
        for item in evaluation.get("models") or []
    }
    model_metric_mismatches: list[dict[str, Any]] = []
    gate_consistent = all(len(values) == 1 for values in model_gates.values())
    for model_key, gates in sorted(model_gates.items()):
        gate = next(iter(gates)) if len(gates) == 1 else False
        model_rows = [
            row for row in expected
            if (row["model_id"], row["model_parameters_sha256"]) == model_key
        ]
        result = actual_models.get(model_key)
        if result is None:
            model_metric_mismatches.append({
                "model": list(model_key), "scope": "missing_model",
            })
            continue
        expected_overall = _metrics(
            model_rows, offline_gate=gate, min_sample=min_sample,
            min_days=min_days, min_cycles=min_cycles,
            target_precision=target_precision,
            min_long_labels=min_long_labels,
            min_short_labels=min_short_labels,
        )
        if not _metrics_match(expected_overall, result.get("overall") or {}):
            model_metric_mismatches.append({
                "model": list(model_key), "scope": "overall",
            })
        by_horizon = {
            str(item.get("horizon")): item
            for item in result.get("by_horizon") or []
        }
        for horizon in HORIZON_MINUTES:
            wanted = _metrics(
                [row for row in model_rows if row["horizon"] == horizon],
                offline_gate=gate, min_sample=min_sample,
                min_days=min_days, min_cycles=min_cycles,
                target_precision=target_precision,
            )
            observed = by_horizon.get(horizon)
            if observed is None or not _metrics_match(wanted, observed):
                model_metric_mismatches.append({
                    "model": list(model_key), "scope": horizon,
                })
        by_day_items = result.get("by_day") or []
        by_day_keys = [str(item.get("day_cst")) for item in by_day_items]
        by_day = {
            str(item.get("day_cst")): item for item in by_day_items
        }
        expected_days = sorted({
            str(row["cycle_id"])[:10] for row in model_rows
        })
        if len(by_day_keys) != len(set(by_day_keys)):
            model_metric_mismatches.append({
                "model": list(model_key),
                "scope": "by_day_duplicate_keys",
            })
        if sorted(by_day) != expected_days:
            model_metric_mismatches.append({
                "model": list(model_key), "scope": "by_day_keys",
            })
        for day in expected_days:
            wanted = _metrics(
                [
                    row for row in model_rows
                    if str(row["cycle_id"])[:10] == day
                ],
                offline_gate=gate,
                min_sample=min_sample,
                min_days=min_days,
                min_cycles=min_cycles,
                target_precision=target_precision,
            )
            observed = by_day.get(day)
            if (
                observed is None
                or observed.get("diagnostic_only") is not True
                or not _metrics_match(wanted, observed)
            ):
                model_metric_mismatches.append({
                    "model": list(model_key),
                    "scope": f"by_day:{day}",
                })
        expected_diagnostics = _diagnostic_metrics(
            model_rows,
            offline_gate=gate,
            min_sample=min_sample,
            min_days=min_days,
            min_cycles=min_cycles,
            target_precision=target_precision,
        )
        if not _structured_match(
            expected_diagnostics, result.get("diagnostics") or {},
        ):
            model_metric_mismatches.append({
                "model": list(model_key), "scope": "diagnostics",
            })

    safety_checks = {
        "confidence_claim_disallowed": evaluation.get("confidence_claim_allowed") is False,
        "production_threshold_change_disallowed": (
            evaluation.get("production_threshold_change_allowed") is False),
        "production_execution_unauthorized": (
            evaluation.get("production_execution_authorized") is False),
        "production_mutation_absent": evaluation.get("production_mutation") is False,
        "orders_absent": evaluation.get("orders_placed") == 0,
    }
    generated_at = _parse_utc(evaluation["generated_at_utc"])
    minimum_wilson = _optional_float(
        contract.get("minimum_wilson_95_lower_bound"))
    maximum_ece = _optional_float(contract.get("maximum_ece"))
    # 地板=预注册激活边界解析出的值（边界前 0.90、边界起 0.80）。点精度与
    # Wilson 下界是孪生阈值，同一个地板同时管住两者。
    precision_floor = thresholds.shadow_target_precision(as_of)
    thresholds_not_weakened = (
        declared_precision is not None and declared_precision >= precision_floor
        and minimum_wilson is not None and minimum_wilson >= precision_floor
        and maximum_ece is not None and 0.0 <= maximum_ece <= 0.05
        and min_sample >= 100
        and min_days >= 5
        and min_cycles >= 100
        and min_long_labels >= 30
        and min_short_labels >= 30
    )
    checks = {
        "evaluation_schema_v2": evaluation.get("schema_version") == 2,
        "label_schema_v3": evaluation.get("label_schema_version") == 3,
        "evaluation_artifact_type_valid": evaluation.get("artifact_type")
        == "frozen_multitimeframe_model_shadow_evaluation",
        "shadow_root_matches": Path(str(evaluation.get("shadow_root"))).resolve()
        == shadow_root.resolve(),
        "artifacts_present": bool(artifacts),
        "artifact_files_readable": not unreadable,
        "artifact_count_matches": evaluation.get("artifacts_loaded")
        == len(artifacts),
        "offline_gate_consistent_per_frozen_model": gate_consistent,
        "evaluation_generated_not_before_as_of": generated_at >= as_of,
        "acceptance_thresholds_not_weakened": thresholds_not_weakened,
        "cost_hurdle_not_weakened": (
            math.isfinite(cost_bps)
            and cost_bps >= 20.0
            and _close(
                contract.get("cost_hurdle_bps_after_observed_spread"),
                cost_bps,
            )
        ),
        "execution_price_contract_exact": contract.get("execution_prices")
        == "long ask->bid; short bid->ask; no last fallback",
        "last_prices_diagnostic_only": evaluation.get(
            "last_price_fields_are_diagnostic_only") is True,
        "all_safety_flags_closed": all(safety_checks.values()),
        "selected_record_count_matches": evaluation.get("selected_records")
        == counters.get("selected_records", 0),
        "invalid_record_count_matches": evaluation.get("invalid_records")
        == counters.get("invalid_records", 0),
        "invalid_records_zero": counters.get("invalid_records", 0) == 0,
        "missing_executable_price_count_matches": evaluation.get(
            "missing_executable_price_records")
        == counters.get("missing_executable_price_records", 0),
        "missing_executable_prices_zero": counters.get(
            "missing_executable_price_records", 0) == 0,
        "crossed_price_count_matches": evaluation.get(
            "crossed_executable_price_records")
        == counters.get("crossed_executable_price_records", 0),
        "crossed_prices_zero": counters.get(
            "crossed_executable_price_records", 0) == 0,
        "label_columns_exact": actual_columns == LABEL_COLUMNS,
        "label_count_matches_evaluation": evaluation.get("labels_written")
        == len(actual),
        "label_count_matches_independent_reconstruction": len(actual)
        == len(expected),
        "reconstructed_keys_unique": not duplicate_expected,
        "label_keys_unique": not duplicate_actual,
        "label_key_set_exact": set(actual_keys) == set(expected_keys),
        "all_label_fields_match_raw_evidence": not row_mismatches,
        "model_key_set_exact": set(actual_models) == set(model_gates),
        "aggregate_metrics_match_labels": not model_metric_mismatches,
    }
    status = "PASSED" if all(checks.values()) else "NOT_MET"
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": 1,
        "artifact_type": "frozen_model_shadow_label_quality_audit",
        "generated_at_utc": _iso(datetime.now(UTC)),
        "mode": "read_only_business_databases",
        "intended_use": "quality gate for forward credibility research evidence",
        "grain": "one frozen model-cycle-symbol selected direction and horizon",
        "calibration_gate_migration": {
            **thresholds.shadow_migration_facts(as_of),
            "declared_target_precision": declared_precision,
            "precision_floor_in_force": precision_floor,
            "rebuilt_with_target_precision": target_precision,
        },
        "inputs": {
            "evaluation": str(evaluation_path.resolve()),
            "evaluation_sha256": _sha256(evaluation_path),
            "labels": str(labels_path.resolve()),
            "labels_sha256": _sha256(labels_path),
            "shadow_root": str(shadow_root.resolve()),
            "market_db": str(market_db.resolve()),
            "as_of_utc": _iso(as_of),
        },
        "row_profile": {
            "artifacts_loaded": len(artifacts),
            "selected_records": counters.get("selected_records", 0),
            "expected_mature_labels": len(expected),
            "observed_labels": len(actual),
            "duplicate_label_keys": len(duplicate_actual),
            "invalid_records": counters.get("invalid_records", 0),
            "entry_missing_or_immature": counters.get(
                "entry_missing_or_immature", 0),
            "horizon_immature": counters.get("horizon_immature", 0),
            "outcome_missing": counters.get("outcome_missing", 0),
            "missing_executable_price_records": counters.get(
                "missing_executable_price_records", 0),
            "crossed_executable_price_records": counters.get(
                "crossed_executable_price_records", 0),
        },
        "quality_rates": {
            "mature_label_completeness_rate": (
                len(set(actual_keys) & set(expected_keys)) / len(expected_keys)
                if expected_keys else None
            ),
            "label_duplicate_rate": (
                len(duplicate_actual) / len(actual) if actual else 0.0
            ),
            "field_reconstruction_match_rate": (
                (len(expected) - len(row_mismatches)) / len(expected)
                if expected else None
            ),
        },
        "safety_checks": safety_checks,
        "checks": checks,
        "failed_checks": failed,
        "evidence": {
            "unreadable_artifacts": unreadable[:20],
            "duplicate_expected_keys": [list(key) for key in duplicate_expected[:20]],
            "duplicate_actual_keys": [list(key) for key in duplicate_actual[:20]],
            "missing_label_keys": [
                list(key) for key in sorted(set(expected_keys) - set(actual_keys))[:20]
            ],
            "extra_label_keys": [
                list(key) for key in sorted(set(actual_keys) - set(expected_keys))[:20]
            ],
            "row_mismatches": row_mismatches[:20],
            "model_metric_mismatches": model_metric_mismatches[:20],
        },
        "status": status,
        "safe_for_credibility_research": status == "PASSED" and bool(actual),
        "confidence_claim_allowed": False,
        "production_threshold_change_allowed": False,
        "production_execution_authorized": False,
        "production_database_writes": 0,
        "production_mutation": False,
        "orders_placed": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = audit(
            evaluation_path=args.evaluation,
            labels_path=args.labels,
            shadow_root=args.shadow_root,
            market_db=args.market_db,
        )
        _atomic_json(args.json_out, payload)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError,
            json.JSONDecodeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "production_mutation": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "ok": payload["status"] == "PASSED",
        "status": payload["status"],
        "labels": payload["row_profile"]["observed_labels"],
        "failed_checks": payload["failed_checks"],
        "json_out": str(args.json_out.resolve()),
        "production_database_writes": 0,
        "production_mutation": False,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0 if payload["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
