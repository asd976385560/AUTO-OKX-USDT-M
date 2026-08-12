#!/usr/bin/env python3
"""Summarize the fixed 16-cycle offline model A/B/C replay.

The script is analysis-only.  It reads immutable replay JSONL, the pre-authored
Codex C judgments, production analysis signals, and 15-minute market bars via
read-only SQLite connections.  It never imports or invokes trading code.

Canonicalization is deliberately outcome-blind: for each model/cycle pair the
earliest successful response by ``started_utc`` is selected.  Later retries or
duplicate successes are retained only for stability diagnostics.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = ROOT / "reports" / "model_ab_replay_20260731"
DEFAULT_MARKET_DB = ROOT / "db" / "market.db"
DEFAULT_ANALYSIS_DB = ROOT / "db" / "analysis.db"
ARMS = {
    "old_glm_5_2": "A 旧模型 GLM-5.2",
    "new_minimax_m3": "B 新模型 MiniMax-M3",
    "codex_c": "C 独立判断 Codex",
    "production_actual": "实际生产 MiniMax-M3",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL {path}:{line_no}: {exc}") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _model_rows(report_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(report_dir.glob("replay*.jsonl")):
        for row in _read_jsonl(path):
            row = dict(row)
            row["source_file"] = path.name
            rows.append(row)
    return rows


def _canonicalize(
    rows: list[dict[str, Any]], cycles: list[str]
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        label = row.get("model_label")
        cycle = row.get("cycle_id")
        if label in {"old_glm_5_2", "new_minimax_m3"} and cycle in cycles:
            grouped[(str(label), str(cycle))].append(row)

    canonical: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_pairs = 0
    open_count_disagreements = 0
    successful_attempts = 0
    error_attempts = 0
    pair_diagnostics: list[dict[str, Any]] = []
    for key, attempts in sorted(grouped.items()):
        successful = sorted(
            (row for row in attempts if row.get("status") == "ok"),
            key=lambda row: str(row.get("started_utc", "")),
        )
        errors = [row for row in attempts if row.get("status") != "ok"]
        successful_attempts += len(successful)
        error_attempts += len(errors)
        if successful:
            canonical[key] = successful[0]
        open_counts = [
            sum(
                str(item.get("action", "")).startswith("open_")
                for item in row.get("decision", {}).get("decisions", [])
                if isinstance(item, dict)
            )
            for row in successful
        ]
        if len(successful) > 1:
            duplicate_pairs += 1
            if len(set(open_counts)) > 1:
                open_count_disagreements += 1
        pair_diagnostics.append(
            {
                "model_label": key[0],
                "cycle_id": key[1],
                "attempts": len(attempts),
                "successes": len(successful),
                "errors": len(errors),
                "successful_open_counts": open_counts,
                "canonical_started_utc": successful[0].get("started_utc") if successful else None,
                "canonical_source_file": successful[0].get("source_file") if successful else None,
            }
        )

    missing = [
        {"model_label": label, "cycle_id": cycle}
        for label in ("old_glm_5_2", "new_minimax_m3")
        for cycle in cycles
        if (label, cycle) not in canonical
    ]
    observed_stochastic_bounds: dict[str, dict[str, Any]] = {}
    for label in ("old_glm_5_2", "new_minimax_m3"):
        flags_by_cycle: dict[str, list[bool]] = {}
        for pair in pair_diagnostics:
            if pair["model_label"] != label or not pair["successful_open_counts"]:
                continue
            flags_by_cycle[pair["cycle_id"]] = [
                count > 0 for count in pair["successful_open_counts"]
            ]
        forced_open = sum(all(flags) for flags in flags_by_cycle.values())
        possible_open = sum(any(flags) for flags in flags_by_cycle.values())
        mean_attempt_probability = statistics.mean(
            sum(flags) / len(flags) for flags in flags_by_cycle.values()
        )
        observed_stochastic_bounds[label] = {
            "cycles_with_success": len(flags_by_cycle),
            "all_successes_open_cycles": forced_open,
            "any_success_open_cycles": possible_open,
            "observed_open_cycle_rate_range_pct": [
                round(forced_open / len(cycles) * 100.0, 3),
                round(possible_open / len(cycles) * 100.0, 3),
            ],
            "mean_within_cycle_open_probability_pct": round(
                mean_attempt_probability * 100.0, 3
            ),
        }
    diagnostics = {
        "attempts": len(rows),
        "successful_attempts": successful_attempts,
        "error_attempts": error_attempts,
        "duplicate_success_pairs": duplicate_pairs,
        "duplicate_open_count_disagreements": open_count_disagreements,
        "observed_stochastic_bounds": observed_stochastic_bounds,
        "missing_canonical_pairs": missing,
        "pairs": pair_diagnostics,
    }
    return canonical, diagnostics


def _decision_rows(
    canonical: dict[tuple[str, str], dict[str, Any]],
    c_payload: dict[str, Any],
    cycles: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label in ("old_glm_5_2", "new_minimax_m3"):
        for cycle in cycles:
            payload = canonical[(label, cycle)]["decision"]
            for item in payload.get("decisions", []):
                action = str(item.get("action", "wait"))
                rows.append(
                    {
                        "arm": label,
                        "arm_name": ARMS[label],
                        "cycle_id": cycle,
                        "symbol": item.get("symbol"),
                        "action": action,
                        "side": item.get("side")
                        or ("long" if action == "open_long" else "short" if action == "open_short" else None),
                        "is_open": int(action.startswith("open_")),
                        "reason": item.get("reason"),
                        "invalidation": item.get("invalidation"),
                    }
                )
    c_by_cycle = {row["cycle_id"]: row for row in c_payload["decisions"]}
    for cycle in cycles:
        for item in c_by_cycle[cycle].get("opens", []):
            action = str(item["action"])
            rows.append(
                {
                    "arm": "codex_c",
                    "arm_name": ARMS["codex_c"],
                    "cycle_id": cycle,
                    "symbol": item["symbol"],
                    "action": action,
                    "side": "long" if action == "open_long" else "short",
                    "is_open": 1,
                    "reason": item.get("reason"),
                    "invalidation": item.get("invalidation"),
                }
            )
    return rows


def _production_counts(conn: sqlite3.Connection, cycles: list[str]) -> dict[str, int]:
    placeholders = ",".join("?" for _ in cycles)
    sql = (
        "SELECT cycle_id, SUM(CASE WHEN action IN ('open_long','open_short') "
        "THEN 1 ELSE 0 END) AS opens FROM analysis_signals "
        f"WHERE cycle_id IN ({placeholders}) GROUP BY cycle_id"
    )
    found = {str(row["cycle_id"]): int(row["opens"] or 0) for row in conn.execute(sql, cycles)}
    return {cycle: found.get(cycle, 0) for cycle in cycles}


def _next_bar(
    conn: sqlite3.Connection, symbol: str, after_utc: datetime
) -> sqlite3.Row | None:
    after = after_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    latest = (after_utc + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(
        "SELECT ts,o FROM kline_cache WHERE symbol=? AND tf='15m' "
        "AND ts>? AND ts<=? ORDER BY ts LIMIT 1",
        (symbol, after, latest),
    ).fetchone()


def _bar_at_or_after(
    conn: sqlite3.Connection, symbol: str, target_utc: datetime
) -> sqlite3.Row | None:
    target = target_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    latest = (target_utc + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(
        "SELECT ts,o FROM kline_cache WHERE symbol=? AND tf='15m' "
        "AND ts>=? AND ts<=? ORDER BY ts LIMIT 1",
        (symbol, target, latest),
    ).fetchone()


def _attach_outcomes(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cst = ZoneInfo("Asia/Shanghai")
    outcome_rows: list[dict[str, Any]] = []
    for row in rows:
        if not row["is_open"]:
            continue
        cycle_cst = datetime.fromisoformat(row["cycle_id"]).replace(tzinfo=cst)
        cycle_utc = cycle_cst.astimezone(timezone.utc)
        entry = _next_bar(conn, str(row["symbol"]), cycle_utc)
        result = dict(row)
        result.update(
            {
                "entry_ts_utc": None,
                "entry_price": None,
                "exit_ts_utc": None,
                "exit_price": None,
                "directional_return_4h_pct": None,
                "directional_win": None,
            }
        )
        if entry is not None and entry["o"] not in (None, 0):
            entry_dt = datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            exit_bar = _bar_at_or_after(conn, str(row["symbol"]), entry_dt + timedelta(hours=4))
            if exit_bar is not None and exit_bar["o"] is not None:
                raw = (float(exit_bar["o"]) / float(entry["o"]) - 1.0) * 100.0
                directional = raw if row["side"] == "long" else -raw
                result.update(
                    {
                        "entry_ts_utc": entry["ts"],
                        "entry_price": float(entry["o"]),
                        "exit_ts_utc": exit_bar["ts"],
                        "exit_price": float(exit_bar["o"]),
                        "directional_return_4h_pct": round(directional, 6),
                        "directional_win": int(directional > 0),
                    }
                )
        outcome_rows.append(result)
    return outcome_rows


def _exact_paired_pvalue(a_open: dict[str, int], b_open: dict[str, int]) -> dict[str, Any]:
    a_only = sum(bool(a_open[c]) and not bool(b_open[c]) for c in a_open)
    b_only = sum(bool(b_open[c]) and not bool(a_open[c]) for c in a_open)
    discordant = a_only + b_only
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(math.comb(discordant, k) for k in range(0, min(a_only, b_only) + 1)) / (2**discordant)
        p_value = min(1.0, 2.0 * tail)
    return {"a_only_open_cycles": a_only, "b_only_open_cycles": b_only, "discordant_cycles": discordant, "exact_two_sided_p": p_value}


def _summaries(
    cycles: list[str], decision_rows: list[dict[str, Any]], outcome_rows: list[dict[str, Any]], production: dict[str, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, int]]]:
    open_counts: dict[str, dict[str, int]] = {
        arm: {cycle: 0 for cycle in cycles}
        for arm in ("old_glm_5_2", "new_minimax_m3", "codex_c", "production_actual")
    }
    for row in decision_rows:
        if row["is_open"]:
            open_counts[row["arm"]][row["cycle_id"]] += 1
    open_counts["production_actual"].update(production)

    outcome_by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in outcome_rows:
        outcome_by_arm[row["arm"]].append(row)

    summaries: list[dict[str, Any]] = []
    for arm in open_counts:
        returns = [
            float(row["directional_return_4h_pct"])
            for row in outcome_by_arm.get(arm, [])
            if row["directional_return_4h_pct"] is not None
        ]
        open_signals = sum(open_counts[arm].values())
        open_cycles = sum(count > 0 for count in open_counts[arm].values())
        summaries.append(
            {
                "arm": arm,
                "arm_name": ARMS[arm],
                "cycles": len(cycles),
                "open_cycles": open_cycles,
                "open_cycle_rate_pct": round(open_cycles / len(cycles) * 100.0, 3),
                "open_signals": open_signals,
                "outcomes_covered": len(returns),
                "outcome_coverage_pct": round(len(returns) / open_signals * 100.0, 3) if open_signals else None,
                "directional_hit_rate_pct": round(sum(value > 0 for value in returns) / len(returns) * 100.0, 3) if returns else None,
                "mean_directional_return_4h_pct": round(statistics.mean(returns), 6) if returns else None,
                "median_directional_return_4h_pct": round(statistics.median(returns), 6) if returns else None,
            }
        )

    cycle_rows = [
        {
            "cycle_id": cycle,
            "arm": arm,
            "arm_name": ARMS[arm],
            "open_count": open_counts[arm][cycle],
            "open_cycle": int(open_counts[arm][cycle] > 0),
        }
        for cycle in cycles
        for arm in open_counts
    ]
    return summaries, cycle_rows, open_counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--market-db", type=Path, default=DEFAULT_MARKET_DB)
    parser.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    args = parser.parse_args()

    c_path = args.report_dir / "codex_c_judgement.json"
    c_payload = json.loads(c_path.read_text(encoding="utf-8"))
    if not c_payload.get("authored_before_future_outcome_query"):
        raise ValueError("C judgment was not frozen before outcome lookup")
    cycles = [str(row["cycle_id"]) for row in c_payload["decisions"]]
    if len(cycles) != 16 or len(set(cycles)) != 16:
        raise ValueError("expected the fixed 16 unique cycles")

    replay_rows = _model_rows(args.report_dir)
    canonical, diagnostics = _canonicalize(replay_rows, cycles)
    if diagnostics["missing_canonical_pairs"]:
        raise RuntimeError(
            f"missing successful replay results: {diagnostics['missing_canonical_pairs']}"
        )
    decision_rows = _decision_rows(canonical, c_payload, cycles)

    with _open_db(args.analysis_db) as analysis_conn:
        production = _production_counts(analysis_conn, cycles)
    with _open_db(args.market_db) as market_conn:
        outcome_rows = _attach_outcomes(market_conn, decision_rows)
    arm_summaries, cycle_rows, open_counts = _summaries(
        cycles, decision_rows, outcome_rows, production
    )

    summary_map = {row["arm"]: row for row in arm_summaries}
    old_rate = summary_map["old_glm_5_2"]["open_cycle_rate_pct"]
    new_rate = summary_map["new_minimax_m3"]["open_cycle_rate_pct"]
    paired = _exact_paired_pvalue(open_counts["old_glm_5_2"], open_counts["new_minimax_m3"])
    summary = {
        "schema": "offline_model_ab_c_analysis_v1",
        "cohort": {
            "description": c_payload["cohort"],
            "cycles": cycles,
            "briefing_dxy_zone": "NORMAL in all 16 archived briefings",
        },
        "canonicalization": "Earliest successful started_utc for each model/cycle; outcome blind.",
        "outcome_method": "First 15m bar strictly after the cycle as entry; bar open 4h later as exit; long uses raw return, short uses negated raw return.",
        "arms": arm_summaries,
        "model_switch_effect": {
            "old_open_cycle_rate_pct": old_rate,
            "new_open_cycle_rate_pct": new_rate,
            "new_minus_old_percentage_points": round(new_rate - old_rate, 3),
            "relative_change_pct": round((new_rate / old_rate - 1.0) * 100.0, 3) if old_rate else None,
            "paired_cycle_test": paired,
        },
        "replay_stability": diagnostics,
        "production_actual_open_signals": sum(production.values()),
        "limitations": [
            "Sixteen cycles from one four-hour market window are a small, autocorrelated sample.",
            "Provider sampling is stochastic; duplicate successes can disagree and are reported separately.",
            "Four-hour directional return measures decision direction, not executable PnL, sizing, fees, slippage, stops, or portfolio path.",
            "C judgments repeatedly select the same symbols and are counterfactual reviews, not a feasible compounding portfolio simulation.",
        ],
    }

    canonical_export = [
        {
            "model_label": label,
            "cycle_id": cycle,
            "started_utc": row.get("started_utc"),
            "source_file": row.get("source_file"),
            "decision": row.get("decision"),
        }
        for (label, cycle), row in sorted(canonical.items())
    ]
    _write_json(args.report_dir / "canonical_decisions.json", canonical_export)
    _write_json(args.report_dir / "summary.json", summary)
    _write_json(args.report_dir / "replay_stability.json", diagnostics)
    _write_csv(
        args.report_dir / "decision_rows.csv",
        decision_rows,
        ["arm", "arm_name", "cycle_id", "symbol", "action", "side", "is_open", "reason", "invalidation"],
    )
    _write_csv(
        args.report_dir / "outcome_rows.csv",
        outcome_rows,
        [
            "arm", "arm_name", "cycle_id", "symbol", "action", "side", "entry_ts_utc", "entry_price",
            "exit_ts_utc", "exit_price", "directional_return_4h_pct", "directional_win", "reason", "invalidation",
        ],
    )
    _write_csv(
        args.report_dir / "arm_summary.csv",
        arm_summaries,
        list(arm_summaries[0]),
    )
    _write_csv(
        args.report_dir / "cycle_open_counts.csv",
        cycle_rows,
        ["cycle_id", "arm", "arm_name", "open_count", "open_cycle"],
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
