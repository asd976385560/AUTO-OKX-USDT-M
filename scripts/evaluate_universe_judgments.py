# -*- coding: utf-8 -*-
"""Evaluate full-universe shadow judgments without look-ahead.

Entry is the first ticker snapshot at or after the judgment artifact's actual
``generated_at_utc``.  Outcomes use the first snapshot at or after 15m/1h/4h
from that entry.  This avoids using a price that existed before the judgment.

The evaluator is read-only with respect to business databases and never turns
an alignment score into a confidence probability.  Until an independently
calibrated model passes the offline gate and is separately wired into future
snapshots, the 90% credibility gate remains NOT MEASURABLE regardless of
descriptive hit rates.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc
CST = timezone(timedelta(hours=8), "Asia/Shanghai")
HORIZONS = {"15m": 15, "1h": 60, "4h": 240}
DEFAULT_COST_BPS = 20.0
DEFAULT_MIN_SAMPLE = 100


def _parse_utc(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(str(path))
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            tmp = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "cycle_id", "generated_at_utc", "symbol", "judgment",
        "uncalibrated_alignment_score", "entry_tick_ts_utc", "entry_price",
        "horizon", "outcome_tick_ts_utc", "outcome_price",
        "signed_return", "after_cost_hit", "source_file",
    ]
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            tmp = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: row.get(key) for key in fields} for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _first_tick_at_or_after(
    market: sqlite3.Connection,
    symbol: str,
    when_utc: datetime,
    *,
    max_wait_minutes: int = 20,
) -> tuple[str, float] | None:
    start = _iso_utc(when_utc)
    end = _iso_utc(when_utc + timedelta(minutes=max_wait_minutes))
    row = market.execute(
        "SELECT ts,last FROM tick_snapshots WHERE symbol=? AND ts>=? AND ts<=? "
        "AND last IS NOT NULL AND last>0 ORDER BY ts LIMIT 1",
        (symbol, start, end),
    ).fetchone()
    return (str(row["ts"]), float(row["last"])) if row else None


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _load_snapshots(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not root.exists():
        return []
    by_cycle: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("artifact_type") != "full_universe_shadow_judgment":
            continue
        cycle = str(payload.get("cycle_id") or path)
        previous = by_cycle.get(cycle)
        if previous is None or str(payload.get("generated_at_utc") or "") >= str(
            previous[1].get("generated_at_utc") or ""
        ):
            by_cycle[cycle] = (path, payload)
    return [by_cycle[key] for key in sorted(by_cycle)]


def evaluate(
    snapshot_root: Path,
    market_db: Path,
    *,
    as_of_utc: datetime,
    cost_bps: float = DEFAULT_COST_BPS,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snapshots = _load_snapshots(snapshot_root)
    eligible_snapshots: list[tuple[Path, dict[str, Any], datetime]] = []
    clock_exclusions = {
        "missing_generated_at_utc": 0,
        "invalid_generated_at_utc": 0,
        "future_generated_at_utc": 0,
        "cycle_day_mismatch": 0,
    }
    for source_path, snapshot in snapshots:
        generated_text = snapshot.get("generated_at_utc")
        if not generated_text:
            clock_exclusions["missing_generated_at_utc"] += 1
            continue
        try:
            generated = _parse_utc(str(generated_text))
        except (TypeError, ValueError):
            clock_exclusions["invalid_generated_at_utc"] += 1
            continue
        if generated > as_of_utc:
            clock_exclusions["future_generated_at_utc"] += 1
            continue
        cycle = str(snapshot.get("cycle_id") or "")
        if cycle[:10] != generated.astimezone(CST).date().isoformat():
            clock_exclusions["cycle_day_mismatch"] += 1
            continue
        eligible_snapshots.append((source_path, snapshot, generated))

    daily: dict[str, dict[str, Any]] = {}
    for _source_path, snapshot, _generated in eligible_snapshots:
        cycle = str(snapshot.get("cycle_id") or "")
        day = cycle[:10]
        if len(day) != 10:
            continue
        item = daily.setdefault(day, {
            "date": day,
            "snapshots": 0,
            "judgment_records": 0,
            "judgment_counts": {
                "long_bias": 0,
                "short_bias": 0,
                "wait_data": 0,
                "wait_mixed": 0,
                "other": 0,
            },
            "symbols": set(),
            "minimum_records_target": 993,
            "minimum_snapshots_target": 3,
            "minimum_unique_symbols_target": 300,
        })
        records = snapshot.get("records") or []
        targets = snapshot.get("targets") or {}
        item["snapshots"] += 1
        item["judgment_records"] += len(records)
        for record in records:
            judgment = str(record.get("judgment") or "")
            count_key = (
                judgment if judgment in item["judgment_counts"] else "other"
            )
            item["judgment_counts"][count_key] += 1
        item["symbols"].update(
            str(record.get("symbol")) for record in records if record.get("symbol")
        )
        item["minimum_records_target"] = int(
            targets.get("minimum_snapshot_rows_for_plus_50pct_daily_signal_target")
            or item["minimum_records_target"]
        )
        item["minimum_snapshots_target"] = int(
            targets.get("minimum_full_snapshots_per_day_for_plus_50pct_target")
            or item["minimum_snapshots_target"]
        )
        item["minimum_unique_symbols_target"] = int(
            targets.get("minimum_unique_symbols_screened_daily")
            or item["minimum_unique_symbols_target"]
        )

    daily_rows: list[dict[str, Any]] = []
    for day in sorted(daily):
        item = daily[day]
        unique_symbols = len(item.pop("symbols"))
        judgment_counts = item["judgment_counts"]
        long_bias_records = int(judgment_counts["long_bias"])
        short_bias_records = int(judgment_counts["short_bias"])
        directional_judgment_records = long_bias_records + short_bias_records
        checks = {
            "minimum_records_met": (
                item["judgment_records"] >= item["minimum_records_target"]
            ),
            "minimum_snapshots_met": (
                item["snapshots"] >= item["minimum_snapshots_target"]
            ),
            "minimum_unique_symbols_met": (
                unique_symbols >= item["minimum_unique_symbols_target"]
            ),
        }
        directional_checks = {
            "minimum_directional_records_met": (
                directional_judgment_records >= item["minimum_records_target"]
            ),
            "minimum_snapshots_met": checks["minimum_snapshots_met"],
            "minimum_unique_symbols_met": checks["minimum_unique_symbols_met"],
        }
        daily_rows.append({
            **item,
            "unique_symbols": unique_symbols,
            "directional_judgment_records": directional_judgment_records,
            "long_bias_records": long_bias_records,
            "short_bias_records": short_bias_records,
            "minimum_directional_records_target": item["minimum_records_target"],
            "directional_judgment_share": (
                directional_judgment_records / item["judgment_records"]
                if item["judgment_records"] else None
            ),
            "both_directional_sides_observed": bool(
                long_bias_records and short_bias_records
            ),
            "minimum_directional_records_met": directional_checks[
                "minimum_directional_records_met"
            ],
            "directional_daily_target_met": all(directional_checks.values()),
            **checks,
            "daily_target_met": all(checks.values()),
        })

    as_of_cst_day = as_of_utc.astimezone(CST).date().isoformat()
    completed_day_rows = [
        row for row in daily_rows if str(row["date"]) < as_of_cst_day
    ]
    current_partial_day = next(
        (row for row in daily_rows if row["date"] == as_of_cst_day), None
    )

    market = _ro(market_db)
    labels: list[dict[str, Any]] = []
    candidate_records = 0
    try:
        for source_path, payload, generated in eligible_snapshots:
            generated_text = payload.get("generated_at_utc")
            for record in payload.get("records") or []:
                if record.get("execution_readiness") != "shadow_candidate":
                    continue
                judgment = str(record.get("judgment") or "")
                if judgment not in {"long_bias", "short_bias"}:
                    continue
                symbol = str(record.get("symbol") or "")
                if not symbol:
                    continue
                candidate_records += 1
                entry = _first_tick_at_or_after(market, symbol, generated)
                if entry is None:
                    continue
                entry_ts, entry_price = entry
                entry_dt = _parse_utc(entry_ts)
                direction = 1.0 if judgment == "long_bias" else -1.0
                for horizon, minutes in HORIZONS.items():
                    if entry_dt + timedelta(minutes=minutes) > as_of_utc:
                        continue
                    outcome = _first_tick_at_or_after(
                        market, symbol, entry_dt + timedelta(minutes=minutes)
                    )
                    if outcome is None:
                        continue
                    outcome_ts, outcome_price = outcome
                    signed_return = direction * (outcome_price / entry_price - 1.0)
                    labels.append({
                        "cycle_id": payload.get("cycle_id"),
                        "generated_at_utc": generated_text,
                        "symbol": symbol,
                        "judgment": judgment,
                        "uncalibrated_alignment_score": record.get("uncalibrated_alignment_score"),
                        "entry_tick_ts_utc": entry_ts,
                        "entry_price": entry_price,
                        "horizon": horizon,
                        "outcome_tick_ts_utc": outcome_ts,
                        "outcome_price": outcome_price,
                        "signed_return": signed_return,
                        "after_cost_hit": signed_return > cost_bps / 10_000.0,
                        "source_file": str(source_path.resolve()),
                    })
    finally:
        market.close()

    by_horizon: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for label in labels:
        by_horizon[label["horizon"]].append(label)
    horizon_metrics = []
    for horizon in HORIZONS:
        rows = by_horizon[horizon]
        n = len(rows)
        raw_hits = sum(row["signed_return"] > 0 for row in rows)
        cost_hits = sum(bool(row["after_cost_hit"]) for row in rows)
        lower, upper = _wilson(cost_hits, n)
        horizon_metrics.append({
            "horizon": horizon,
            "n_labeled": n,
            "distinct_cycles": len({row["cycle_id"] for row in rows}),
            "raw_direction_precision_pct": round(100 * raw_hits / n, 3) if n else None,
            "after_cost_precision_pct": round(100 * cost_hits / n, 3) if n else None,
            "after_cost_wilson95_low_pct": round(100 * lower, 3) if lower is not None else None,
            "after_cost_wilson95_high_pct": round(100 * upper, 3) if upper is not None else None,
            "mean_signed_return_pct": round(
                100 * sum(row["signed_return"] for row in rows) / n, 4
            ) if n else None,
            "minimum_sample_met": n >= min_sample,
        })

    measurable = False  # no independent probability calibration exists yet
    payload = {
        "schema_version": 1,
        "artifact_type": "full_universe_shadow_judgment_evaluation",
        "generated_at_utc": _iso_utc(datetime.now(UTC)),
        "as_of_utc": _iso_utc(as_of_utc),
        "snapshot_root": str(snapshot_root.resolve()),
        "snapshots_loaded": len(snapshots),
        "snapshots_eligible_as_of": len(eligible_snapshots),
        "snapshot_clock_quality": {
            "as_of_utc": _iso_utc(as_of_utc),
            "day_boundary_timezone": "Asia/Shanghai (UTC+08:00)",
            "excluded": clock_exclusions,
            "status": (
                "PASSED" if not any(clock_exclusions.values()) else "DEGRADED"
            ),
        },
        "daily_throughput": {
            "metric": "auditable full-universe shadow judgment records",
            "directional_metric": (
                "long_bias plus short_bias records; wait and unknown records "
                "remain visible but are excluded"
            ),
            "days": daily_rows,
            "latest_day": daily_rows[-1] if daily_rows else None,
            "latest_completed_day": (
                completed_day_rows[-1] if completed_day_rows else None
            ),
            "current_partial_day": current_partial_day,
            "real_fills_are_not_a_throughput_target": True,
        },
        "shadow_candidate_records": candidate_records,
        "labels_written": len(labels),
        "round_trip_cost_bps": cost_bps,
        "minimum_sample_per_horizon": min_sample,
        "horizons": horizon_metrics,
        "confidence_calibration": {
            "available": False,
            "method": None,
            "holdout_cycles": 0,
            "expected_calibration_error_pct_points": None,
            "reason": (
                "snapshot alignment score is not a probability; offline calibrated candidates "
                "have not passed acceptance and are not wired into these judgments"
            ),
        },
        "credibility_gate": {
            "target_precision_pct": 90.0,
            "minimum_sample": min_sample,
            "maximum_calibration_error_pct_points": 5.0,
            "status": "NOT_MEASURABLE" if not measurable else "NOT_MET",
            "production_threshold_change_allowed": False,
        },
        "production_mutation": False,
        "orders_placed": 0,
    }
    return payload, labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate full-universe shadow judgments")
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--labels-out", type=Path)
    parser.add_argument("--as-of", help="ISO-8601 UTC; default now")
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-sample", type=int, default=DEFAULT_MIN_SAMPLE)
    args = parser.parse_args(argv)
    try:
        if args.cost_bps < 0 or args.min_sample <= 0:
            raise ValueError("cost-bps must be >=0 and min-sample must be >0")
        as_of = _parse_utc(args.as_of) if args.as_of else datetime.now(UTC)
        payload, labels = evaluate(
            args.snapshot_root,
            args.market_db,
            as_of_utc=as_of,
            cost_bps=args.cost_bps,
            min_sample=args.min_sample,
        )
        _atomic_json(args.json_out, payload)
        if args.labels_out:
            _atomic_csv(args.labels_out, labels)
        print(json.dumps({
            "ok": True,
            "json_out": str(args.json_out.resolve()),
            "labels_out": str(args.labels_out.resolve()) if args.labels_out else None,
            "snapshots_loaded": payload["snapshots_loaded"],
            "labels_written": payload["labels_written"],
            "credibility_status": payload["credibility_gate"]["status"],
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
