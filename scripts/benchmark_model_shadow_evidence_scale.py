#!/usr/bin/env python3
"""Isolated 100-cycle scale acceptance for frozen-shadow evidence jobs.

Creates deterministic synthetic artifacts and an isolated indexed market DB,
then runs the real evaluator and the independent auditor.  No production
database, report, threshold or order path is touched.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import audit_model_shadow_label_quality as auditor
import evaluate_multitimeframe_model_shadow as evaluator


UTC = timezone.utc


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _record(
    *,
    symbol: str,
    generated: datetime,
    side: str,
    horizon: str,
    probability: float,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "feature_decision_ts_utc": _iso(generated - timedelta(seconds=30)),
        "signal_available_at_utc": _iso(generated),
        "side": side,
        "horizon": horizon,
        "research_probability": probability,
        "selected_for_forward_evaluation": True,
        "ranking_diagnostics": {
            "runner_up_probability": max(0.0, probability - 0.08),
            "top_vs_runner_up_margin": 0.08,
            "opposite_side_same_horizon_probability": max(
                0.0, probability - 0.18),
            "selected_vs_opposite_margin": 0.18,
            "selected_side_horizon_votes": 2,
            "selected_side_unanimous": False,
            "selected_side_mean_margin": 0.11,
            "selected_side_min_margin": -0.02,
        },
        "future_retraining_features": {
            "contract_statistics_available": True,
            "contract_statistics_source_ts_utc": _iso(
                generated - timedelta(minutes=15)),
            "contract_statistics_available_at_utc": _iso(
                generated - timedelta(seconds=10)),
            "contract_oi_log_usd": 15.0,
            "contract_oi_log_change_15m": 0.01,
            "contract_taker_total_log_usd": 13.0,
            "contract_taker_buy_centered": 0.02,
            "contract_oi_taker_interaction": 0.0002,
        },
    }


def _build_fixture(
    root: Path,
    *,
    cycles: int,
    signals_per_cycle: int,
) -> tuple[Path, Path, datetime]:
    shadow = root / "shadow"
    shadow.mkdir(parents=True)
    market = root / "market.db"
    con = sqlite3.connect(market)
    con.execute(
        "CREATE TABLE tick_snapshots("
        "ts TEXT NOT NULL,symbol TEXT NOT NULL,last REAL,bid REAL,ask REAL,"
        "PRIMARY KEY(ts,symbol))"
    )
    con.execute("CREATE INDEX idx_tick_symbol_ts ON tick_snapshots(symbol,ts)")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    ticks: list[tuple[str, str, float, float, float]] = []
    horizons = (("15m", 15), ("1H", 60), ("4H", 240))
    for cycle_index in range(cycles):
        generated = start + timedelta(hours=8 * cycle_index, seconds=45)
        cycle_id = generated.strftime("%Y-%m-%dT%H:%M")
        records = []
        for signal_index in range(signals_per_cycle):
            symbol = f"S{signal_index:03d}-USDT-SWAP"
            side = "long" if (cycle_index + signal_index) % 2 == 0 else "short"
            horizon, minutes = horizons[signal_index % len(horizons)]
            probability = 0.55 + (signal_index % 20) / 100.0
            records.append(_record(
                symbol=symbol,
                generated=generated,
                side=side,
                horizon=horizon,
                probability=probability,
            ))
            entry = generated + timedelta(seconds=15)
            outcome = entry + timedelta(minutes=minutes)
            base = 1.0 + signal_index / 100.0 + cycle_index / 10000.0
            move = 0.004 if (cycle_index + signal_index) % 3 else -0.004
            outcome_last = base * (1 + move)
            ticks.extend([
                (_iso(entry), symbol, base, base * 0.9995, base * 1.0005),
                (_iso(outcome), symbol, outcome_last,
                 outcome_last * 0.9995, outcome_last * 1.0005),
            ])
        artifact = {
            "schema_version": 1,
            "artifact_type": "frozen_multitimeframe_model_shadow",
            "model_id": "scale-fixture-model",
            "model_parameters_sha256": "b" * 64,
            "cycle_id": cycle_id,
            "generated_at_utc": _iso(generated),
            "forward_evidence_eligible": True,
            "status": "ready_for_forward_shadow",
            "offline_acceptance": {"gate_pass": False},
            "records": records,
            "confidence_claim_allowed": False,
            "production_execution_authorized": False,
            "production_mutation": False,
            "orders_placed": 0,
        }
        path = shadow / cycle_id[:10] / (
            f"model-shadow-{cycle_id.replace(':', '-')}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact), encoding="utf-8")
    con.executemany(
        "INSERT OR REPLACE INTO tick_snapshots VALUES(?,?,?,?,?)", ticks,
    )
    con.commit()
    con.close()
    as_of = start + timedelta(hours=8 * (cycles - 1) + 5)
    return shadow, market, as_of


def benchmark(
    *,
    cycles: int,
    signals_per_cycle: int,
    evaluator_budget_seconds: float,
    auditor_budget_seconds: float,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        shadow, market, as_of = _build_fixture(
            root, cycles=cycles, signals_per_cycle=signals_per_cycle,
        )
        tracemalloc.start()
        started = time.perf_counter()
        evaluation, labels = evaluator.evaluate(
            shadow_root=shadow,
            market_db=market,
            as_of_utc=as_of,
        )
        evaluator_seconds = time.perf_counter() - started
        _, evaluator_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        evaluation_path = root / "evaluation.json"
        labels_path = root / "labels.csv"
        evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
        with labels_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=evaluator.LABEL_COLUMNS)
            writer.writeheader()
            writer.writerows(labels)
        tracemalloc.start()
        started = time.perf_counter()
        quality = auditor.audit(
            evaluation_path=evaluation_path,
            labels_path=labels_path,
            shadow_root=shadow,
            market_db=market,
        )
        auditor_seconds = time.perf_counter() - started
        _, auditor_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    expected_labels = cycles * signals_per_cycle
    checks = {
        "cycle_target_met": cycles >= 100,
        "label_volume_exact": len(labels) == expected_labels,
        "evaluation_within_step_budget": (
            evaluator_seconds <= evaluator_budget_seconds),
        "independent_audit_within_step_budget": (
            auditor_seconds <= auditor_budget_seconds),
        "quality_audit_passed": quality["status"] == "PASSED",
        "label_keys_unique": quality["checks"]["label_keys_unique"],
        "field_reconstruction_exact": quality["checks"]
        ["all_label_fields_match_raw_evidence"],
        "safety_flags_closed": quality["checks"]["all_safety_flags_closed"],
        "confidence_claim_disallowed": not evaluation["confidence_claim_allowed"],
        "production_execution_unauthorized": not evaluation[
            "production_execution_authorized"],
        "production_mutation_absent": not evaluation["production_mutation"],
        "orders_absent": evaluation["orders_placed"] == 0,
    }
    return {
        "schema_version": 1,
        "artifact_type": "frozen_model_shadow_scale_acceptance",
        "generated_at_utc": _iso(datetime.now(UTC)),
        "mode": "isolated_synthetic_no_production_database_access",
        "cycles": cycles,
        "signals_per_cycle": signals_per_cycle,
        "expected_labels": expected_labels,
        "labels_written": len(labels),
        "evaluator": {
            "duration_seconds": round(evaluator_seconds, 6),
            "budget_seconds": evaluator_budget_seconds,
            "peak_memory_mib": round(evaluator_peak / 1024 / 1024, 3),
        },
        "independent_auditor": {
            "duration_seconds": round(auditor_seconds, 6),
            "budget_seconds": auditor_budget_seconds,
            "peak_memory_mib": round(auditor_peak / 1024 / 1024, 3),
        },
        "quality_status": quality["status"],
        "quality_rates": quality["quality_rates"],
        "checks": checks,
        "status": "PASSED" if all(checks.values()) else "NOT_MET",
        "confidence_claim_allowed": False,
        "production_threshold_change_allowed": False,
        "production_execution_authorized": False,
        "production_database_writes": 0,
        "production_mutation": False,
        "orders_placed": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--signals-per-cycle", type=int, default=54)
    parser.add_argument("--evaluator-budget-seconds", type=float, default=15.0)
    parser.add_argument("--auditor-budget-seconds", type=float, default=20.0)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if min(args.cycles, args.signals_per_cycle) <= 0:
            raise ValueError("cycles and signals-per-cycle must be positive")
        payload = benchmark(
            cycles=args.cycles,
            signals_per_cycle=args.signals_per_cycle,
            evaluator_budget_seconds=args.evaluator_budget_seconds,
            auditor_budget_seconds=args.auditor_budget_seconds,
        )
        _atomic_json(args.json_out, payload)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
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
        "cycles": payload["cycles"],
        "labels": payload["labels_written"],
        "evaluator_seconds": payload["evaluator"]["duration_seconds"],
        "auditor_seconds": payload["independent_auditor"]["duration_seconds"],
        "failed_checks": [
            name for name, passed in payload["checks"].items() if not passed
        ],
        "json_out": str(args.json_out.resolve()),
        "production_database_writes": 0,
        "production_mutation": False,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0 if payload["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
