#!/usr/bin/env python3
"""Audit realised 15m/1H/4H quality of production analysis open signals.

This is a read-only evidence job.  It joins ``analysis_signals`` to the
terminal ``analysis_runs`` row, anchors entry to the first market snapshot
strictly after analysis completion, and evaluates 15m/1H/4H outcomes from the
entry clock.  Long entries/exits use ask/bid and shorts use bid/ask when those
quotes exist, then a further configurable round-trip hurdle is applied.

The retrospective discovery window may select one global horizon.  The later
window evaluates that frozen choice, but is still labelled retrospective; only
future artifacts created after an explicit freeze can become independent
forward evidence.  This script never writes a production database, changes a
threshold, dispatches a stage, or places an order.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import statistics
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


CST = timezone(timedelta(hours=8))
UTC = timezone.utc
HORIZONS = {"15m": timedelta(minutes=15), "1H": timedelta(hours=1),
            "4H": timedelta(hours=4)}
DEFAULT_ANALYSIS_DB = Path(r"./db/analysis.db")
DEFAULT_MARKET_DB = Path(r"./db/market.db")
DEFAULT_JSON = Path(
    r"./reports/quality/analysis-signal-forward-evaluation.json")
DEFAULT_LABELS = Path(
    r"./reports/quality/analysis-signal-forward-labels.csv")
DEFAULT_EVALUATION_START = "2026-08-05T00:00:00+08:00"
DEFAULT_CURRENT_PROTOCOL_START = "2026-08-10T00:00:00+08:00"


def _ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=20000")
    return con


def _parse_time(value: str, *, naive_zone: timezone = CST) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_zone)
    return parsed.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_cst(value: datetime) -> str:
    return value.astimezone(CST).isoformat()


def _wilson(successes: int, total: int,
            z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(
        (p * (1 - p) + z * z / (4 * total)) / total
    ) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


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


def _atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "cycle_id", "symbol", "action", "side", "regime",
        "analysis_completed_at_cst", "decision_ts_utc", "horizon",
        "outcome_status", "entry_ts_utc", "entry_last", "entry_executable",
        "entry_price_source", "exit_ts_utc", "exit_last", "exit_executable",
        "exit_price_source", "last_directional_return",
        "executable_directional_return", "signed_return_after_cost",
        "after_cost_hit", "self_reported_confidence", "planned_rr",
        "ev_p_win", "ev_p_n", "ev_ci_low", "ev_r",
        "direction_evidence_count", "opposing_evidence_count",
        "instrument_regime", "global_instrument_regime_match",
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


def _read_signal_rows(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT s.cycle_id,s.symbol,s.action,s.side,s.confidence,"
        "s.decision_card,r.ts AS analysis_completed_at_cst,r.mode,r.regime,"
        "r.regime_stale,r.missing_sources "
        "FROM analysis_signals s JOIN analysis_runs r "
        "ON r.cycle_id=s.cycle_id "
        "WHERE r.status='ok' AND s.action IN ('open_long','open_short') "
        "ORDER BY s.cycle_id,s.symbol"
    ).fetchall()
    return [dict(row) for row in rows]


def _card_features(raw: Any) -> dict[str, Any]:
    try:
        card = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        card = {}
    if not isinstance(card, dict):
        card = {}
    ev = card.get("ev_check") if isinstance(card.get("ev_check"), dict) else {}
    risk_reward = (
        card.get("risk_reward")
        if isinstance(card.get("risk_reward"), dict) else {}
    )
    regime_scope = (
        card.get("regime_scope")
        if isinstance(card.get("regime_scope"), dict) else {}
    )
    direction = card.get("direction_evidence")
    opposition = card.get("opposing_evidence")
    ci = ev.get("p_ci95")
    ci_low = (
        float(ci[0]) if isinstance(ci, list) and len(ci) >= 2
        and isinstance(ci[0], (int, float)) else None
    )
    return {
        "direction_evidence_count": len(direction) if isinstance(direction, list) else 0,
        "opposing_evidence_count": len(opposition) if isinstance(opposition, list) else 0,
        "planned_rr": _finite_or_none(risk_reward.get("rr")),
        "ev_p_win": _finite_or_none(ev.get("p_win")),
        "ev_p_n": int(ev["p_n"]) if isinstance(ev.get("p_n"), int) else None,
        "ev_ci_low": ci_low,
        "ev_r": _finite_or_none(ev.get("ev_r")),
        "instrument_regime": regime_scope.get("instrument_regime"),
    }


def _finite_or_none(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _snapshot(
    con: sqlite3.Connection,
    symbol: str,
    *,
    after: datetime,
    inclusive: bool,
    max_delay_minutes: int,
) -> sqlite3.Row | None:
    operator = ">=" if inclusive else ">"
    return con.execute(
        "SELECT ts,last,bid,ask FROM tick_snapshots WHERE symbol=? "
        f"AND ts{operator}? AND ts<=? ORDER BY ts LIMIT 1",
        (
            symbol,
            _iso_utc(after),
            _iso_utc(after + timedelta(minutes=max_delay_minutes)),
        ),
    ).fetchone()


def _price(row: sqlite3.Row, field: str, fallback: str = "last") -> tuple[float | None, str | None]:
    primary = _finite_or_none(row[field])
    if primary is not None and primary > 0:
        return primary, field
    secondary = _finite_or_none(row[fallback])
    if secondary is not None and secondary > 0:
        return secondary, fallback
    return None, None


def label_signal(
    market: sqlite3.Connection,
    signal: dict[str, Any],
    *,
    cost_bps: float,
    max_delay_minutes: int,
    market_max_ts: datetime,
) -> list[dict[str, Any]]:
    side = str(signal.get("side") or (
        "long" if signal.get("action") == "open_long" else "short"
    ))
    if side not in {"long", "short"}:
        raise ValueError(f"invalid signal side: {side!r}")
    decision = _parse_time(str(signal["analysis_completed_at_cst"]))
    entry = _snapshot(
        market, str(signal["symbol"]), after=decision, inclusive=False,
        max_delay_minutes=max_delay_minutes,
    )
    features = _card_features(signal.get("decision_card"))
    base = {
        "cycle_id": str(signal["cycle_id"]),
        "symbol": str(signal["symbol"]),
        "action": str(signal["action"]),
        "side": side,
        "regime": signal.get("regime"),
        "analysis_completed_at_cst": str(signal["analysis_completed_at_cst"]),
        "decision_ts_utc": _iso_utc(decision),
        "decision_date_cst": decision.astimezone(CST).date().isoformat(),
        "self_reported_confidence": _finite_or_none(signal.get("confidence")),
        **features,
    }
    instrument_regime = features.get("instrument_regime")
    base["global_instrument_regime_match"] = (
        bool(instrument_regime == signal.get("regime"))
        if instrument_regime not in (None, "", "not_available")
        and signal.get("regime") not in (None, "") else None
    )
    output: list[dict[str, Any]] = []
    if entry is None:
        for horizon in HORIZONS:
            output.append({**base, "horizon": horizon,
                           "outcome_status": "entry_missing",
                           "after_cost_hit": None})
        return output

    entry_time = _parse_time(str(entry["ts"]), naive_zone=UTC)
    entry_last, _ = _price(entry, "last")
    entry_field = "ask" if side == "long" else "bid"
    entry_exec, entry_source = _price(entry, entry_field)
    for horizon, duration in HORIZONS.items():
        target = entry_time + duration
        row = {
            **base,
            "horizon": horizon,
            "entry_ts_utc": _iso_utc(entry_time),
            "entry_last": entry_last,
            "entry_executable": entry_exec,
            "entry_price_source": entry_source,
            "after_cost_hit": None,
        }
        exit_row = _snapshot(
            market, str(signal["symbol"]), after=target, inclusive=True,
            max_delay_minutes=max_delay_minutes,
        )
        if exit_row is None:
            row["outcome_status"] = (
                "immature" if market_max_ts < target else "exit_missing"
            )
            output.append(row)
            continue
        exit_time = _parse_time(str(exit_row["ts"]), naive_zone=UTC)
        exit_last, _ = _price(exit_row, "last")
        exit_field = "bid" if side == "long" else "ask"
        exit_exec, exit_source = _price(exit_row, exit_field)
        if None in (entry_last, entry_exec, exit_last, exit_exec):
            row["outcome_status"] = "price_missing"
            output.append(row)
            continue
        direction = 1.0 if side == "long" else -1.0
        last_return = direction * (float(exit_last) / float(entry_last) - 1.0)
        executable_return = direction * (
            float(exit_exec) / float(entry_exec) - 1.0)
        after_cost = executable_return - cost_bps / 10_000.0
        row.update({
            "outcome_status": "matured",
            "exit_ts_utc": _iso_utc(exit_time),
            "exit_last": exit_last,
            "exit_executable": exit_exec,
            "exit_price_source": exit_source,
            "last_directional_return": last_return,
            "executable_directional_return": executable_return,
            "signed_return_after_cost": after_cost,
            "after_cost_hit": bool(after_cost > 0),
        })
        output.append(row)
    return output


def _metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    selected = [row for row in rows if row.get("outcome_status") == "matured"]
    successes = sum(bool(row["after_cost_hit"]) for row in selected)
    total = len(selected)
    low, high = _wilson(successes, total)
    returns = [float(row["signed_return_after_cost"]) for row in selected]
    return {
        "n": total,
        "successes": successes,
        "precision_after_cost": successes / total if total else None,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "distinct_cycles": len({row["cycle_id"] for row in selected}),
        "distinct_days": len({row["decision_date_cst"] for row in selected}),
        "distinct_symbols": len({row["symbol"] for row in selected}),
        "long_n": sum(row["side"] == "long" for row in selected),
        "short_n": sum(row["side"] == "short" for row in selected),
        "mean_signed_return_after_cost": (
            statistics.fmean(returns) if returns else None
        ),
        "median_signed_return_after_cost": (
            statistics.median(returns) if returns else None
        ),
    }


def select_horizon_from_discovery(
    rows: list[dict[str, Any]],
    *,
    minimum_n: int,
    minimum_days: int,
    minimum_cycles: int,
) -> dict[str, Any]:
    candidates = []
    for horizon in HORIZONS:
        metrics = _metrics(row for row in rows if row["horizon"] == horizon)
        metrics["horizon"] = horizon
        metrics["eligible"] = (
            metrics["n"] >= minimum_n
            and metrics["distinct_days"] >= minimum_days
            and metrics["distinct_cycles"] >= minimum_cycles
        )
        candidates.append(metrics)
    eligible = [item for item in candidates if item["eligible"]]
    pool = eligible or candidates
    chosen = max(
        pool,
        key=lambda item: (
            item["eligible"],
            item["wilson_95_low"] if item["wilson_95_low"] is not None else -1.0,
            item["precision_after_cost"]
            if item["precision_after_cost"] is not None else -1.0,
            item["n"],
            -list(HORIZONS).index(item["horizon"]),
        ),
    )
    return {
        "selection_source": "retrospective_discovery_only",
        "selection_rule": "highest Wilson 95% lower bound after sample/time gates",
        "selected_horizon": chosen["horizon"],
        "selection_status": "eligible_choice" if eligible else "no_eligible_choice",
        "candidates": candidates,
    }


def _confidence_diagnostic(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for horizon in HORIZONS:
        for threshold in (0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90):
            subset = [
                row for row in rows
                if row["horizon"] == horizon
                and row.get("self_reported_confidence") is not None
                and float(row["self_reported_confidence"]) >= threshold
            ]
            item = _metrics(subset)
            item.update({"horizon": horizon, "threshold": threshold})
            output.append(item)
    return output


def audit(
    *,
    analysis_db: Path,
    market_db: Path,
    evaluation_start: datetime,
    current_protocol_start: datetime,
    cost_bps: float = 20.0,
    max_delay_minutes: int = 20,
    minimum_discovery_n: int = 30,
    minimum_discovery_days: int = 7,
    minimum_discovery_cycles: int = 20,
    minimum_evaluation_n: int = 100,
    minimum_evaluation_days: int = 5,
    minimum_evaluation_cycles: int = 100,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if cost_bps < 0 or max_delay_minutes <= 0:
        raise ValueError("cost_bps and max_delay_minutes must be non-negative/positive")
    analysis = _ro(analysis_db)
    market = _ro(market_db)
    try:
        signals = _read_signal_rows(analysis)
        max_value = market.execute(
            "SELECT MAX(ts) AS max_ts FROM tick_snapshots"
        ).fetchone()["max_ts"]
        if not max_value:
            raise ValueError("tick_snapshots has no maximum timestamp")
        market_max = _parse_time(str(max_value), naive_zone=UTC)
        labels = [
            label
            for signal in signals
            for label in label_signal(
                market, signal, cost_bps=cost_bps,
                max_delay_minutes=max_delay_minutes,
                market_max_ts=market_max,
            )
        ]
    finally:
        market.close()
        analysis.close()

    evaluation_start = evaluation_start.astimezone(UTC)
    current_protocol_start = current_protocol_start.astimezone(UTC)
    discovery = [
        row for row in labels
        if _parse_time(row["decision_ts_utc"], naive_zone=UTC) < evaluation_start
    ]
    evaluation = [
        row for row in labels
        if _parse_time(row["decision_ts_utc"], naive_zone=UTC) >= evaluation_start
    ]
    current_protocol = [
        row for row in labels
        if _parse_time(row["decision_ts_utc"], naive_zone=UTC)
        >= current_protocol_start
    ]
    horizon_selection = select_horizon_from_discovery(
        discovery,
        minimum_n=minimum_discovery_n,
        minimum_days=minimum_discovery_days,
        minimum_cycles=minimum_discovery_cycles,
    )
    selected_horizon = str(horizon_selection["selected_horizon"])
    selected_evaluation = _metrics(
        row for row in evaluation if row["horizon"] == selected_horizon)
    requirements = {
        "evaluation_n_at_least_minimum": (
            selected_evaluation["n"] >= minimum_evaluation_n),
        "evaluation_days_at_least_minimum": (
            selected_evaluation["distinct_days"] >= minimum_evaluation_days),
        "evaluation_cycles_at_least_minimum": (
            selected_evaluation["distinct_cycles"] >= minimum_evaluation_cycles),
        "precision_after_cost_at_least_90pct": (
            (selected_evaluation["precision_after_cost"] or 0.0) >= 0.90),
        "calibrated_probability_available": False,
        "independent_future_window": False,
    }
    sample_ready = all(requirements[name] for name in (
        "evaluation_n_at_least_minimum",
        "evaluation_days_at_least_minimum",
        "evaluation_cycles_at_least_minimum",
    ))
    if not sample_ready:
        status = "INSUFFICIENT_EVIDENCE"
    elif not requirements["precision_after_cost_at_least_90pct"]:
        status = "NOT_MET"
    else:
        status = "DIRECTIONAL_PRECISION_ONLY_NOT_CALIBRATED_OR_FORWARD"

    missing_counts = Counter(row["outcome_status"] for row in labels)
    by_horizon = {
        horizon: _metrics(row for row in labels if row["horizon"] == horizon)
        for horizon in HORIZONS
    }
    by_side_horizon = {
        f"{side}_{horizon}": _metrics(
            row for row in labels
            if row["side"] == side and row["horizon"] == horizon
        )
        for side in ("long", "short") for horizon in HORIZONS
    }
    payload = {
        "schema_version": 1,
        "artifact_type": "analysis_signal_forward_quality_audit",
        "generated_at_cst": datetime.now(CST).isoformat(),
        "mode": "read_only_research_and_monitoring",
        "grain": "one production analysis open signal x one fixed horizon",
        "signal_population": {
            "actions": ["open_long", "open_short"],
            "terminal_analysis_status": "ok",
            "signals": len(signals),
            "label_rows": len(labels),
            "market_max_ts_utc": _iso_utc(market_max),
        },
        "outcome_contract": {
            "decision_clock": "analysis_runs.ts interpreted as Asia/Shanghai when naive",
            "entry": (
                "first tick strictly after analysis completion, within "
                f"{max_delay_minutes} minutes"
            ),
            "exit": (
                "first tick at or after entry+horizon, within "
                f"{max_delay_minutes} minutes"
            ),
            "execution_prices": "long ask->bid; short bid->ask; last fallback recorded",
            "cost_hurdle_bps_after_spread": cost_bps,
            "success": "executable directional return minus hurdle > 0",
            "horizons": list(HORIZONS),
        },
        "windows": {
            "discovery_end_exclusive_utc": _iso_utc(evaluation_start),
            "evaluation_start_utc": _iso_utc(evaluation_start),
            "current_protocol_start_utc": _iso_utc(current_protocol_start),
            "evaluation_evidence_class": "retrospective_not_independent_forward",
        },
        "data_quality": {
            "outcome_status_counts": dict(sorted(missing_counts.items())),
            "self_reported_confidence_semantics": (
                "legacy optional uncalibrated field; never a production confidence claim"
            ),
            "protocol_drift": (
                "signals span multiple historical decision protocols; current-protocol "
                "window is reported separately and may be small"
            ),
        },
        "all_history_by_horizon": by_horizon,
        "all_history_by_side_horizon": by_side_horizon,
        "discovery_horizon_selection": horizon_selection,
        "retrospective_evaluation": {
            "selected_horizon": selected_horizon,
            **selected_evaluation,
        },
        "current_protocol_by_horizon": {
            horizon: _metrics(
                row for row in current_protocol if row["horizon"] == horizon)
            for horizon in HORIZONS
        },
        "legacy_self_reported_confidence_diagnostic_discovery_only": (
            _confidence_diagnostic(discovery)
        ),
        "acceptance": {
            "target_precision": 0.90,
            "minimum_evaluation_n": minimum_evaluation_n,
            "minimum_evaluation_days": minimum_evaluation_days,
            "minimum_evaluation_cycles": minimum_evaluation_cycles,
            "requirements": requirements,
            "status": status,
            "confidence_90_status": "NOT_PROVEN",
        },
        "limitations": [
            "retrospective signal outcomes are not an untouched future window",
            "historical signals span multiple decision protocols",
            "analysis_signals.confidence is optional, legacy, and not calibrated",
            "fixed-horizon directional quality is not realised portfolio PnL",
            "20bp is a standardised hurdle after observed spread, not fill-by-fill fees",
        ],
        "production_mutation": False,
        "production_threshold_change_allowed": False,
        "stage_dispatch_triggered": False,
        "orders_placed": 0,
    }
    return payload, labels


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    parser.add_argument("--market-db", type=Path, default=DEFAULT_MARKET_DB)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--labels-out", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--evaluation-start", default=DEFAULT_EVALUATION_START)
    parser.add_argument(
        "--current-protocol-start", default=DEFAULT_CURRENT_PROTOCOL_START)
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--max-delay-minutes", type=int, default=20)
    parser.add_argument("--minimum-discovery-n", type=int, default=30)
    parser.add_argument("--minimum-discovery-days", type=int, default=7)
    parser.add_argument("--minimum-discovery-cycles", type=int, default=20)
    parser.add_argument("--minimum-evaluation-n", type=int, default=100)
    parser.add_argument("--minimum-evaluation-days", type=int, default=5)
    parser.add_argument("--minimum-evaluation-cycles", type=int, default=100)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload, labels = audit(
            analysis_db=args.analysis_db,
            market_db=args.market_db,
            evaluation_start=_parse_time(args.evaluation_start),
            current_protocol_start=_parse_time(args.current_protocol_start),
            cost_bps=args.cost_bps,
            max_delay_minutes=args.max_delay_minutes,
            minimum_discovery_n=args.minimum_discovery_n,
            minimum_discovery_days=args.minimum_discovery_days,
            minimum_discovery_cycles=args.minimum_discovery_cycles,
            minimum_evaluation_n=args.minimum_evaluation_n,
            minimum_evaluation_days=args.minimum_evaluation_days,
            minimum_evaluation_cycles=args.minimum_evaluation_cycles,
        )
        _atomic_json(args.json_out, payload)
        _atomic_csv(args.labels_out, labels)
    except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_mutation": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    selected = payload["retrospective_evaluation"]
    print(json.dumps({
        "ok": True,
        "json_out": str(args.json_out),
        "labels_out": str(args.labels_out),
        "signals": payload["signal_population"]["signals"],
        "selected_horizon": selected["selected_horizon"],
        "evaluation_n": selected["n"],
        "evaluation_precision_after_cost": selected["precision_after_cost"],
        "confidence_90_status": payload["acceptance"]["confidence_90_status"],
        "production_mutation": False,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
