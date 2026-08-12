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

import offline_multitimeframe_calibration as calibration


UTC = timezone.utc
HORIZON_MINUTES = {"15m": 15, "1H": 60, "4H": 240}
LABEL_COLUMNS = (
    "model_id", "model_parameters_sha256", "cycle_id", "symbol", "side",
    "horizon", "research_probability", "signal_available_at_utc",
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
    "contract_statistics_available", "contract_statistics_source_ts_utc",
    "contract_statistics_available_at_utc", "contract_oi_log_usd",
    "contract_oi_log_change_15m", "contract_taker_total_log_usd",
    "contract_taker_buy_centered", "contract_oi_taker_interaction",
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
    requirements = {
        "minimum_sample_met": n >= min_sample,
        "minimum_days_met": distinct_days >= min_days,
        "minimum_cycles_met": distinct_cycles >= min_cycles,
        "precision_at_least_90pct": precision is not None and precision >= 0.90,
        "wilson_95_low_at_least_90pct": low is not None and low >= 0.90,
        "ece_at_most_5pp": ece is not None and ece <= 0.05,
        "offline_gate_pass": offline_gate_pass,
    }
    measurable = all(requirements[key] for key in (
        "minimum_sample_met", "minimum_days_met", "minimum_cycles_met"
    ))
    forward_pass = (
        measurable
        and requirements["precision_at_least_90pct"]
        and requirements["wilson_95_low_at_least_90pct"]
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
        "side_counts": dict(sorted(Counter(str(row["side"]) for row in rows).items())),
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
) -> dict[str, Any]:
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
            ),
        })
    return {
        "ranking_diagnostic_rows": len(with_margin),
        "ranking_diagnostic_coverage_rate": (
            len(with_margin) / len(rows) if rows else None),
        "contract_feature_flag_rows": len(with_contract_flag),
        "contract_feature_flag_coverage_rate": (
            len(with_contract_flag) / len(rows) if rows else None),
        "by_selected_side_horizon_votes": by_votes,
        "by_selected_vs_opposite_margin_band": by_margin,
        "by_contract_statistics_availability": by_contract,
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
            for record in payload.get("records") or []:
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
                labels.append({
                    "model_id": model_key[0],
                    "model_parameters_sha256": model_key[1],
                    "cycle_id": str(payload["cycle_id"]),
                    "symbol": symbol,
                    "side": side,
                    "horizon": horizon,
                    "research_probability": probability,
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
                    "source_file": str(source_path.resolve()),
                })
    finally:
        con.close()

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
                ),
            })
        model_results.append({
            "model_id": model_key[0],
            "model_parameters_sha256": model_key[1],
            "overall": overall,
            "by_horizon": horizon_results,
            "diagnostics": _diagnostic_metrics(
                rows,
                offline_gate_pass=model_offline_gate[model_key],
                min_sample=min_sample,
                min_days=min_days,
                min_cycles=min_cycles,
            ),
        })

    payload = {
        "schema_version": 2,
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
            "target_precision": 0.90,
            "minimum_wilson_95_lower_bound": 0.90,
            "maximum_ece": 0.05,
            "minimum_sample": min_sample,
            "minimum_days": min_days,
            "minimum_distinct_cycles": min_cycles,
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
    args = parser.parse_args(argv)
    try:
        if args.cost_bps < 0 or min(args.min_sample, args.min_days, args.min_cycles) <= 0:
            raise ValueError("cost and acceptance thresholds are invalid")
        payload, labels = evaluate(
            shadow_root=args.shadow_root,
            market_db=args.market_db,
            as_of_utc=_parse_utc(args.as_of) if args.as_of else datetime.now(UTC),
            cost_bps=args.cost_bps,
            min_sample=args.min_sample,
            min_days=args.min_days,
            min_cycles=args.min_cycles,
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
