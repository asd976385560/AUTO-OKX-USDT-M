# -*- coding: utf-8 -*-
r"""Strict forward audit for complete-cycle latency and coverage quality.

The SLA clock starts at the natural UTC+8 cycle boundary.  Its forward-only
stop point is resolved per cycle: V4 stops at the analysis/judgment/trade
business terminal, while older registered generations retain their historical
record-reconcile or post-Push monitor stop.  The pass condition is strictly
``elapsed_seconds < 870``.  Missing, failed, skipped, malformed, or
exactly-870-second cycles remain in the planned denominator and fail closed.

Candidate breadth is diagnostic rather than an order quota: the audit counts
valid per-cycle MTF evidence artifacts and final open cards, but never requires
a minimum trade or long/short mix.  The fixed forward baseline begins only
after both the hourly critical-path optimization and the 2..3 candidate/runtime
contract were deployed.  The candidate target range widened to 3..5 and then
3..8 on 2026-08-27 (see ``candidate_observation.target_deep_dive_range``);
cycles before those boundaries legitimately show fewer deep dives.

Coverage diagnostics keep the SLA denominator unchanged.  They independently
surface per-slot collection-universe receipts, persisted candidate attrition,
complete final decision cards, and expected-versus-explicit position reviews.

Reads: stage-status JSON, collection JSONL, analysis.db, live_trades.db and
tmp/mtf evidence.
Writes: one atomic quality JSON only.  No network, order, dispatch, retry, or
business-database mutation.
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

ROOT = Path(_public_project_path())
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import stage_runner  # noqa: E402
import _acceptance_thresholds as thresholds  # noqa: E402
import audit_candidate_bundle_rollout as candidate_rollout_audit  # noqa: E402
from core.decision_card import (  # noqa: E402
    is_lightweight_open_card,
    validate_card,
)
from _audit_artifact_context import resolve_audit_output  # noqa: E402

CST = timezone(timedelta(hours=8))
DEFAULT_FORWARD_START = "2026-08-15T00:00:00+08:00"
DEFAULT_STATUS_DIR = ROOT / "logs" / "stage-status"
DEFAULT_ANALYSIS_DB = ROOT / "db" / "analysis.db"
DEFAULT_LIVE_TRADES_DB = ROOT / "db" / "live_trades.db"
DEFAULT_MTF_DIR = ROOT / "tmp"
DEFAULT_COLLECT_LOG_DIR = ROOT / "logs" / "collect"
DEFAULT_BRIEFING_LOG_DIR = ROOT / "logs" / "briefing"
DEFAULT_TRIGGER_LOG_DIR = ROOT / "logs" / "trigger"
DEFAULT_CANDIDATE_EVIDENCE_DIR = ROOT / "logs" / "candidate-evidence"
DEFAULT_JSON_OUT = ROOT / "reports" / "quality" / "complete-cycle-sla-audit.json"
DEFAULT_OPENCLAW_DB = Path(
    '<USER_HOME>\\.openclaw\\state\\openclaw.sqlite'.replace('<USER_HOME>', str(__import__('pathlib').Path.home())))
TIER1_CLOSED_EVIDENCE = (
    ROOT / "reports" / "quality"
    / "candidate-expansion-runtime-diagnostic-20260829-0054"
    / "complete-cycle-sla-audit.json"
)
TIER1_CLOSED_EVIDENCE_SHA256 = (
    "a03ac84a31a06f3627d5521c41eaf02f0d70d004bcd1b708be5bb7b20f92f485")
SLOT = timedelta(minutes=15)


def parse_cst(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def floor_slot(value: datetime) -> datetime:
    value = value.astimezone(CST)
    minute = (value.minute // 15) * 15
    return value.replace(minute=minute, second=0, microsecond=0)


def cycle_id(value: datetime) -> str:
    return value.astimezone(CST).strftime("%Y-%m-%dT%H:%M")


def planned_cycles(start: datetime, as_of: datetime, finality_seconds: int) -> list[str]:
    start_slot = floor_slot(start)
    if start != start_slot:
        raise ValueError("forward_start must be an exact 15-minute boundary")
    mature_through = floor_slot(as_of - timedelta(seconds=finality_seconds))
    if mature_through < start_slot:
        return []
    values = []
    cursor = start_slot
    while cursor <= mature_through:
        values.append(cycle_id(cursor))
        cursor += SLOT
    return values


def load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _frozen_prior_tier_result(
    prior: dict,
    *,
    forward_start: datetime,
    as_of: datetime,
) -> dict | None:
    """Read the immutable closed Tier-1 result when the full window is in scope.

    Flat stage-status retention is shorter than the closed Tier-1 window.  A
    later scan must surface missing retained files diagnostically, but must not
    rejudge the already closed 512-slot result.  Narrow/custom scans continue
    to report only their own rows.
    """
    if int(prior.get("tier") or 0) != thresholds.SLA_PASS_RATE_TIER_INDEX:
        return None
    activation = thresholds.parse_cst(prior["activation_cst"])
    end = thresholds.parse_cst(prior["end_exclusive_cst"])
    if forward_start > activation or as_of < end:
        return None
    try:
        raw = TIER1_CLOSED_EVIDENCE.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != TIER1_CLOSED_EVIDENCE_SHA256:
            return None
        payload = json.loads(raw.decode("utf-8"))
        tiers = (((payload.get("strict_sla") or {}).get("pass_rate_tier")
                  or {}).get("prior_tiers") or [])
        row = next(
            (item for item in tiers if isinstance(item, dict)
             and item.get("tier") == thresholds.SLA_PASS_RATE_TIER_INDEX),
            None,
        )
        if not isinstance(row, dict):
            return None
        expected = {
            "planned_cycles": 512,
            "strict_cycle_passes": 448,
            "strict_pass_rate": 0.875,
            "status": "MET",
        }
        if any(row.get(key) != value for key, value in expected.items()):
            return None
        return {
            **expected,
            "frozen_closed_result": True,
            "closure_evidence_path": str(TIER1_CLOSED_EVIDENCE),
            "closure_evidence_sha256": actual_hash,
            "closure_generated_at": payload.get("generated_at"),
            "historical_rejudgement": False,
        }
    except (OSError, ValueError, TypeError):
        return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _cycle_batches(cycles: list[str], size: int = 500) -> list[list[str]]:
    return [cycles[index:index + size] for index in range(0, len(cycles), size)]


_FINAL_CARD_KEYS = {
    "direction_evidence", "opposing_evidence", "execution_conditions",
    "invalidation_point", "risk_reward", "portfolio_impact",
    "historical_experience", "agent_judgement", "reference_overrides",
}


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _complete_final_card(value: object, action: object = None) -> bool:
    card = _json_dict(value)
    if is_lightweight_open_card(card):
        return not validate_card(card)
    if not _FINAL_CARD_KEYS.issubset(card):
        return False
    if not isinstance(card.get("direction_evidence"), list) or not card[
        "direction_evidence"
    ]:
        return False
    if not isinstance(card.get("opposing_evidence"), list) or not card[
        "opposing_evidence"
    ]:
        return False
    risk_reward = card.get("risk_reward")
    if not isinstance(risk_reward, dict):
        return False
    if not all(_finite_number(risk_reward.get(key)) for key in (
        "entry", "stop", "target", "rr",
    )):
        return False
    if str(action or "") in {"open_long", "open_short"} and str(
        risk_reward.get("exit_mode") or ""
    ) not in {"fixed_tp", "dynamic_exit", "no_fixed_tp"}:
        return False
    history = card.get("historical_experience")
    if not isinstance(history, dict):
        return False
    if not all(isinstance(history.get(key), list) for key in (
        "matched_wins", "matched_losses", "missed_opportunities",
    )):
        return False
    if str(history.get("usage") or "") not in {
        "adopt", "partial", "ignore", "none",
    }:
        return False
    if not str(history.get("reason") or "").strip():
        return False
    if not isinstance(card.get("reference_overrides"), list):
        return False
    return all(str(card.get(key) or "").strip() for key in (
        "execution_conditions", "invalidation_point", "portfolio_impact",
        "agent_judgement",
    ))


def _candidate_pool_count(raw: dict, shortfall: dict) -> int | None:
    values = [shortfall.get("observed_candidate_count")]
    for key in (
        "named_open_candidate_count", "named_open_candidates_in_briefing",
        "briefing_open_candidate_count",
    ):
        values.append(raw.get(key))
    for value in values:
        parsed = _nonnegative_int(value)
        if parsed is not None:
            return parsed
    scanner = raw.get("scanner_funnel")
    if isinstance(scanner, dict):
        for key in (
            "named_open_candidates", "open_candidates", "candidate_count",
        ):
            parsed = _nonnegative_int(scanner.get(key))
            if parsed is not None:
                return parsed
    return None


def _attrition_classification(reason: object) -> str:
    text = str(reason or "").strip().lower()
    if not text:
        return "unavailable"
    if any(token in text for token in (
        "剩余", "预算", "时间", "时限", "迟起", "deadline", "budget", "time",
    )):
        return "remaining_budget"
    if any(token in text for token in (
        "证据", "缺失", "不完整", "evidence", "missing", "incomplete",
    )):
        return "evidence_insufficient"
    return "other"


def analysis_coverage_observations(
    analysis_db: Path, cycles: list[str],
) -> dict[str, dict]:
    """Read persisted candidate and final-card facts without writing the DB."""
    if not cycles or not analysis_db.exists():
        return {}
    uri = analysis_db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    observations: dict[str, dict] = {}
    try:
        if _table_exists(connection, "analysis_runs"):
            for batch in _cycle_batches(cycles):
                placeholders = ",".join("?" for _ in batch)
                for row in connection.execute(
                    "SELECT cycle_id,status,raw FROM analysis_runs "
                    f"WHERE cycle_id IN ({placeholders})",
                    batch,
                ):
                    cycle = str(row["cycle_id"])
                    minimal_policy = (
                        thresholds.minimal_decision_contract_active(cycle))
                    outer = _json_dict(row["raw"])
                    raw = outer.get("raw")
                    raw = raw if isinstance(raw, dict) else outer
                    entries = raw.get(
                        "candidates_deep_dived_v2"
                        if minimal_policy else "candidates_deep_dived")
                    entries = entries if isinstance(entries, list) else []
                    valid_entries = []
                    rejected = 0
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        symbol = str(
                            entry.get("instId") or entry.get("symbol") or ""
                        ).strip().upper()
                        evidence_hash = str(entry.get("evidence_hash") or "").strip()
                        decision = str(entry.get("decision") or "").strip().lower()
                        reason = str(entry.get("reason") or "").strip()
                        if (
                            symbol
                            and (
                                minimal_policy
                                or re.fullmatch(
                                    r"[0-9a-fA-F]{64}", evidence_hash))
                            and decision
                            and reason
                        ):
                            valid_entries.append({
                                "instId": symbol,
                                "evidence_hash": evidence_hash,
                                "decision": decision,
                            })
                            if decision in {"reject", "rejected", "drop", "dropped"}:
                                rejected += 1
                    shortfall = raw.get("candidate_evidence_shortfall")
                    shortfall = shortfall if isinstance(shortfall, dict) else {}
                    pool_count = _candidate_pool_count(raw, shortfall)
                    valid_count = len(valid_entries)
                    not_deep = (
                        max(pool_count - valid_count, 0)
                        if pool_count is not None else None
                    )
                    classification = _attrition_classification(
                        shortfall.get("reason"))
                    observations[cycle] = {
                        "available": True,
                        "analysis_status": str(row["status"] or ""),
                        "policy": (
                            thresholds.MINIMAL_DECISION_CONTRACT_POLICY
                            if minimal_policy else "legacy"),
                        "three_period_judgment_required": not minimal_policy,
                        "six_field_decision_card_required": not minimal_policy,
                        "persisted_deep_dive_entries": len(entries),
                        "valid_persisted_deep_dives": valid_count,
                        "valid_candidate_reviews": (
                            valid_count if minimal_policy else None),
                        "invalid_persisted_deep_dives": len(entries) - valid_count,
                        "persisted_deep_dive_symbols": sorted({
                            item["instId"] for item in valid_entries
                        }),
                        "deep_dive_rejected_candidates": rejected,
                        "candidate_pool_count": pool_count,
                        "not_deep_dived_candidate_count": not_deep,
                        "candidate_attrition_classification": classification,
                        "remaining_budget_limited_candidates": (
                            not_deep if classification == "remaining_budget" else 0
                            if not_deep is not None else None
                        ),
                        "evidence_insufficient_candidates": (
                            not_deep if classification == "evidence_insufficient" else 0
                            if not_deep is not None else None
                        ),
                        "unclassified_not_deep_dived_candidates": (
                            not_deep if classification in {"other", "unavailable"} else 0
                            if not_deep is not None else None
                        ),
                    }
        if _table_exists(connection, "analysis_signals"):
            for batch in _cycle_batches(cycles):
                placeholders = ",".join("?" for _ in batch)
                query = (
                    "SELECT cycle_id,symbol,action,decision_card "
                    "FROM analysis_signals "
                    f"WHERE cycle_id IN ({placeholders}) "
                    "AND action IN ('open_long','open_short')"
                )
                try:
                    signal_rows = connection.execute(query, batch)
                except sqlite3.OperationalError:
                    # Minimal historical/test schemas may not have these columns.
                    continue
                for row in signal_rows:
                    cycle = str(row["cycle_id"])
                    current = observations.setdefault(cycle, {
                        "available": False,
                        "analysis_status": None,
                        "persisted_deep_dive_entries": 0,
                        "valid_persisted_deep_dives": 0,
                        "invalid_persisted_deep_dives": 0,
                        "persisted_deep_dive_symbols": [],
                        "deep_dive_rejected_candidates": 0,
                        "candidate_pool_count": None,
                        "not_deep_dived_candidate_count": None,
                        "candidate_attrition_classification": "unavailable",
                        "remaining_budget_limited_candidates": None,
                        "evidence_insufficient_candidates": None,
                        "unclassified_not_deep_dived_candidates": None,
                    })
                    current["final_open_card_count"] = (
                        int(current.get("final_open_card_count") or 0) + 1)
                    if _complete_final_card(row["decision_card"], row["action"]):
                        current["complete_final_open_card_count"] = (
                            int(current.get("complete_final_open_card_count") or 0) + 1)
        for value in observations.values():
            value.setdefault("final_open_card_count", 0)
            value.setdefault("complete_final_open_card_count", 0)
    finally:
        connection.close()
    return observations


def collection_universe_observations(
    collect_log_dir: Path | None, cycles: list[str],
) -> dict[str, dict]:
    """Extract bounded per-cycle universe receipts from retained collect logs."""
    if not cycles or collect_log_dir is None or not collect_log_dir.exists():
        return {}
    wanted = set(cycles)
    paths = {
        collect_log_dir / f"collect_cycle_{cycle[:10].replace('-', '')}.jsonl"
        for cycle in cycles
    }
    observations: dict[str, dict] = {}
    for path in sorted(paths):
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            cycle = str(payload.get("cycle") or "") if isinstance(payload, dict) else ""
            if cycle not in wanted or payload.get("duplicate_skip"):
                continue
            fast = next((
                step for step in payload.get("steps") or []
                if isinstance(step, dict) and step.get("name") == "fast"
            ), None)
            data_quality = fast.get("data_quality") if isinstance(fast, dict) else None
            if not isinstance(data_quality, dict):
                continue
            expected = _nonnegative_int(data_quality.get("expected"))
            tickers = _nonnegative_int(data_quality.get("tickers"))
            candle_transport = data_quality.get("candle_transport")
            candle_transport = (
                candle_transport if isinstance(candle_transport, dict) else {}
            )
            candles = _nonnegative_int(candle_transport.get("usable_symbols"))
            if candles is None and expected is not None:
                coverage = data_quality.get("candle_coverage")
                if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
                    candles = max(0, min(expected, int(round(expected * float(coverage)))))
            alignment = data_quality.get("contract_official_universe_alignment")
            alignment = alignment if isinstance(alignment, dict) else {}
            configured = _nonnegative_int(alignment.get("primary_selected_symbols"))
            observations[cycle] = {
                "available": True,
                "configured_universe_symbols": configured,
                "expected_symbols": expected,
                "ticker_symbols": tickers,
                "candle_symbols": candles,
                "ticker_coverage": _rate(tickers or 0, expected or 0),
                "candle_coverage": _rate(candles or 0, expected or 0),
                "collection_ok": payload.get("ok") is True,
            }
    return observations


def briefing_candidate_observations(
    briefing_log_dir: Path | None, cycles: list[str],
) -> dict[str, dict]:
    """Read the deterministic per-cycle mature/early candidate pool."""
    if not cycles or briefing_log_dir is None or not briefing_log_dir.exists():
        return {}
    wanted = set(cycles)
    paths = {
        briefing_log_dir / f"candidates-{cycle[:10].replace('-', '')}.jsonl"
        for cycle in cycles
    }
    observations: dict[str, dict] = {}
    for path in sorted(paths):
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            cycle = str(payload.get("cycle_id") or "")
            if (
                cycle not in wanted
                or payload.get("schema") != "briefing_candidates_v1"
                or not isinstance(payload.get("candidates"), list)
            ):
                continue
            valid = []
            minimal_policy = thresholds.minimal_decision_contract_active(cycle)
            for candidate in payload["candidates"]:
                if not isinstance(candidate, dict):
                    continue
                layer = str(candidate.get("layer") or "").strip().lower()
                symbol = str(candidate.get("symbol") or "").strip().upper()
                side = str(candidate.get("side") or "").strip().lower()
                if minimal_policy:
                    if (
                        layer != "all_market"
                        or not symbol
                        or candidate.get("side") is not None
                        or candidate.get("eligible_sides") != ["long", "short"]
                    ):
                        continue
                    side = "side_neutral"
                else:
                    if layer not in {"mature", "early"} or not symbol:
                        continue
                    if side not in {"long", "short"}:
                        continue
                valid.append((layer, symbol, side))
            observations[cycle] = {
                "available": True,
                "schema": "briefing_candidates_v1",
                "policy": (
                    thresholds.MINIMAL_DECISION_CONTRACT_POLICY
                    if minimal_policy else "legacy"),
                "three_period_judgment_required": not minimal_policy,
                "candidate_pool_count": len(valid),
                "candidate_pool_symbols": sorted({item[1] for item in valid}),
                "mature_candidates": sum(item[0] == "mature" for item in valid),
                "early_candidates": sum(item[0] == "early" for item in valid),
                "side_neutral_candidates": sum(
                    item[0] == "all_market" for item in valid),
                "invalid_candidate_entries": len(payload["candidates"]) - len(valid),
                "written_at_cst": payload.get("written_at_cst"),
            }
    return observations


_ALL_MODELS_FAILED_RE = re.compile(
    r"All models failed \((?P<count>\d+)\): (?P<details>.+)$")


def _model_error_kind(error: str) -> str:
    text = str(error or "").lower()
    if "authentication" in text or "invalid api key" in text:
        return "authentication_error"
    if "429" in text or "quota" in text or "monthly usage" in text:
        return "quota_exceeded"
    if "idle timeout" in text:
        return "idle_timeout"
    if any(token in text for token in (
        "connection error", "request timed out", "terminated", "timeout",
    )):
        return "connection_or_request_timeout"
    return "other"


def parse_model_failure_line(line: str) -> dict | None:
    match = _ALL_MODELS_FAILED_RE.search(str(line or "").strip())
    if match is None:
        return None
    attempts = []
    for item in match.group("details").split(" | "):
        model, separator, error = item.partition(": ")
        if not separator or not model.strip() or not error.strip():
            continue
        attempts.append({
            "model": model.strip(),
            "error_kind": _model_error_kind(error),
            "error_summary": error.strip()[:300],
        })
    expected = int(match.group("count"))
    return {
        "available": bool(attempts),
        "declared_model_count": expected,
        "parsed_model_count": len(attempts),
        "complete": len(attempts) == expected,
        "attempts": attempts,
    }


def model_failure_observations(
    trigger_log_dir: Path | None, cycles: list[str],
) -> dict[str, dict]:
    if not cycles or trigger_log_dir is None or not trigger_log_dir.exists():
        return {}
    observations: dict[str, dict] = {}
    for cycle in cycles:
        path = trigger_log_dir / (
            f"live-{cycle[:10].replace('-', '')}-{cycle[-5:].replace(':', '')}.log"
        )
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        parsed = next((
            value for line in lines
            if (value := parse_model_failure_line(line)) is not None
        ), None)
        if parsed is not None:
            observations[cycle] = parsed
    return observations


def candidate_funnel_observation(
    briefing: dict, analysis: dict, independent_mtf_symbols: list[str],
) -> dict:
    briefing_available = briefing.get("available") is True
    analysis_available = analysis.get("available") is True
    briefing_pool = (
        briefing.get("candidate_pool_count") if briefing_available else None
    )
    persisted_pool = analysis.get("candidate_pool_count")
    if isinstance(briefing_pool, int):
        pool = briefing_pool
        source = "briefing_candidates_v1"
    elif isinstance(persisted_pool, int):
        pool = persisted_pool
        source = "analysis_raw_fallback"
    else:
        return {
            "available": False,
            "reason": "candidate_pool_denominator_unavailable",
            "briefing_available": briefing_available,
            "analysis_available": analysis_available,
        }
    independent_symbols = {
        str(symbol).strip().upper()
        for symbol in independent_mtf_symbols
        if str(symbol).strip()
    }
    persisted_symbols = {
        str(symbol).strip().upper()
        for symbol in analysis.get("persisted_deep_dive_symbols") or []
        if str(symbol).strip()
    } if analysis_available else set()
    observed_symbols = independent_symbols | persisted_symbols
    briefing_symbols = {
        str(symbol).strip().upper()
        for symbol in briefing.get("candidate_pool_symbols") or []
        if str(symbol).strip()
    } if briefing_available else set()
    outside_pool = (
        observed_symbols - briefing_symbols if briefing_available else set()
    )
    if briefing_available:
        observed_symbols &= briefing_symbols
    deep = len(observed_symbols)
    not_deep = max(pool - deep, 0)
    if not analysis_available:
        classification = "analysis_unavailable"
    else:
        classification = str(
            analysis.get("candidate_attrition_classification") or "unavailable"
        )
    return {
        "available": True,
        "candidate_pool_source": source,
        "candidate_pool_count": pool,
        "briefing_candidate_pool_count": briefing_pool,
        "persisted_candidate_pool_count": persisted_pool,
        "candidate_pool_source_match": (
            briefing_pool == persisted_pool
            if isinstance(briefing_pool, int) and isinstance(persisted_pool, int)
            else None
        ),
        "observed_deep_dive_count": deep,
        "observed_deep_dive_symbols": sorted(observed_symbols),
        "independently_validated_mtf_count": len(independent_symbols),
        "valid_persisted_deep_dive_count": len(persisted_symbols),
        "deep_dive_source_parity": (
            independent_symbols == persisted_symbols
            if independent_symbols and persisted_symbols else None
        ),
        "deep_dive_outside_briefing_pool_count": len(outside_pool),
        "deep_dive_outside_briefing_pool_symbols": sorted(outside_pool),
        "not_deep_dived_candidate_count": not_deep,
        "deep_dive_coverage_rate": _rate(deep, pool),
        "candidate_attrition_classification": classification,
        "remaining_budget_limited_candidates": (
            not_deep if classification == "remaining_budget" else 0
        ),
        "evidence_insufficient_candidates": (
            not_deep if classification == "evidence_insufficient" else 0
        ),
        "analysis_unavailable_candidates": (
            not_deep if classification == "analysis_unavailable" else 0
        ),
        "unclassified_not_deep_dived_candidates": (
            not_deep
            if classification not in {
                "remaining_budget", "evidence_insufficient", "analysis_unavailable",
            }
            else 0
        ),
        "invalid_briefing_candidate_entries": int(
            briefing.get("invalid_candidate_entries") or 0
        ) if briefing_available else None,
    }


_REVIEW_ACTIONS = {
    "hold", "close", "reduce", "adjust", "adjust_protection", "add", "open",
}


def _review_item_symbol(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    request = value.get("request")
    if isinstance(request, dict):
        nested = _review_item_symbol(request)
        if nested:
            return nested
    return str(
        value.get("instId") or value.get("symbol")
        or value.get("instrument") or ""
    ).strip().upper()


def _review_item_action(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    request = value.get("request")
    if isinstance(request, dict):
        nested = _review_item_action(request)
        if nested:
            return nested
    return str(
        value.get("action") or value.get("type")
        or value.get("requested_action") or ""
    ).strip().lower()


def _review_item_reason(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("reason", "reasoning", "conclusion", "agent_judgement"):
        reason = str(value.get(key) or "").strip()
        if reason:
            return reason
    request = value.get("request")
    return _review_item_reason(request) if isinstance(request, dict) else ""


def _normalized_review_action(value: object) -> str:
    action = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "adjustprotection": "adjust_protection",
        "partial_close": "reduce",
        "reduce_position": "reduce",
        "close_position": "close",
        "open_long": "open",
        "open_short": "open",
    }
    return aliases.get(action, action)


def _mentions_exact_symbol(text: str, symbol: str) -> bool:
    return bool(symbol and re.search(
        rf"(?<![A-Z0-9]){re.escape(symbol.upper())}(?![A-Z0-9])",
        str(text or "").upper(),
    ))


def _explicit_position_review(payload: dict, symbol: str, cycle: str) -> bool:
    card = payload.get("decision_card")
    card = card if isinstance(card, dict) else {}
    for candidate in (payload.get("position_reviews"), card.get("position_reviews")):
        rows = list(candidate.values()) if isinstance(candidate, dict) else candidate
        if not isinstance(rows, (list, tuple)):
            continue
        for item in rows:
            if _review_item_symbol(item) != symbol:
                continue
            if (
                _normalized_review_action(_review_item_action(item)) in _REVIEW_ACTIONS
                and _review_item_reason(item)
            ):
                return True
    judgement = str(card.get("agent_judgement") or "")
    for clause in re.split(r"[\n；;。，,]+", judgement):
        if not _mentions_exact_symbol(clause, symbol):
            continue
        if re.search(
            r"(?<![A-Z])(?:HOLD|CLOSE|REDUCE|ADJUST(?:_|\s+)PROTECTION|ADD|OPEN)"
            r"(?![A-Z])",
            clause.upper(),
        ):
            return True
    if thresholds.structured_position_actions_count_as_review(cycle):
        for key in (
            "requested_position_actions", "position_action_results",
            "position_action_failures",
        ):
            for item in payload.get(key) or []:
                if _review_item_symbol(item) != symbol:
                    continue
                if (
                    _normalized_review_action(_review_item_action(item))
                    in _REVIEW_ACTIONS
                    and _review_item_reason(item)
                ):
                    return True
    return False


def position_review_observations(
    live_trades_db: Path | None, cycles: list[str],
) -> dict[str, dict]:
    """Read exact-cycle live facts and independently classify explicit reviews."""
    if not cycles or live_trades_db is None or not live_trades_db.exists():
        return {}
    uri = live_trades_db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    observations: dict[str, dict] = {}
    try:
        if not _table_exists(connection, "trade_cycles"):
            return {}
        for batch in _cycle_batches(cycles):
            placeholders = ",".join("?" for _ in batch)
            for row in connection.execute(
                "SELECT cycle_id,mode,raw FROM trade_cycles "
                f"WHERE cycle_id IN ({placeholders})",
                batch,
            ):
                cycle = str(row["cycle_id"])
                payload = _json_dict(row["raw"])
                facts = payload.get("live_facts")
                facts = facts if isinstance(facts, dict) else {}
                positions = facts.get("positions")
                valid_facts = (
                    str(row["mode"] or "") == "live"
                    and facts.get("cycle_id") == cycle
                    and facts.get("profile") == "live"
                    and facts.get("status") == "ok"
                    and isinstance(positions, list)
                )
                if not valid_facts:
                    observations[cycle] = {
                        "available": False,
                        "reason": "exact_live_facts_unavailable",
                    }
                    continue
                symbols = sorted({
                    str(item.get("instId") or item.get("symbol") or "").strip().upper()
                    for item in positions
                    if isinstance(item, dict)
                    and str(item.get("instId") or item.get("symbol") or "").strip()
                })
                reviewed = sorted(
                    symbol for symbol in symbols
                    if _explicit_position_review(payload, symbol, cycle)
                )
                observations[cycle] = {
                    "available": True,
                    "registered_semantic_active": (
                        thresholds.structured_position_actions_count_as_review(cycle)
                    ),
                    "positions_expected_review": len(symbols),
                    "position_symbols": symbols,
                    "positions_explicitly_reviewed": len(reviewed),
                    "explicitly_reviewed_symbols": reviewed,
                    "explicit_review_rate": _rate(len(reviewed), len(symbols)),
                }
    finally:
        connection.close()
    return observations


def open_card_counts(analysis_db: Path, cycles: list[str]) -> dict[str, int]:
    if not cycles or not analysis_db.exists():
        return {}
    uri = analysis_db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        placeholders = ",".join("?" for _ in cycles)
        rows = connection.execute(
            "SELECT cycle_id,COUNT(*) FROM analysis_signals "
            f"WHERE cycle_id IN ({placeholders}) "
            "AND action IN ('open_long','open_short') GROUP BY cycle_id",
            cycles,
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]): int(row[1]) for row in rows}


def valid_mtf_evidence(mtf_dir: Path, cycle: str) -> list[str]:
    safe_cycle = cycle.replace(":", "-")
    symbols: set[str] = set()
    for path in mtf_dir.glob(f"mtf_{safe_cycle}_*.json"):
        value = load_json(path)
        if (
            not value
            or value.get("ok") is not True
            or value.get("status") != "PASSED"
            or value.get("cycle_id") != cycle
            or value.get("production_database_writes") != 0
            or value.get("orders_placed") != 0
        ):
            continue
        symbol = str(value.get("symbol") or "").strip()
        contract = value.get("evidence_contract")
        if not symbol or not isinstance(contract, dict):
            continue
        if contract.get("cycle_id") != cycle or contract.get("symbol") != symbol:
            continue
        if contract.get("protocol") != "multitimeframe_market_evidence_v1":
            continue
        if contract.get("required_timeframes") != ["15m", "1H", "4H"]:
            continue
        timeframes = contract.get("timeframes")
        if (
            not isinstance(timeframes, dict)
            or set(timeframes) != {"15m", "1H", "4H"}
            or not all(
                isinstance(timeframes.get(name), dict)
                and timeframes[name].get("ready") is True
                for name in ("15m", "1H", "4H")
            )
            or not str(contract.get("evidence_hash") or "").strip()
        ):
            continue
        symbols.add(symbol)
    return sorted(symbols)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _pass_rate_recovery_projection(
    *,
    passes: int,
    planned: int,
    target_rate: float | None,
    minimum_slots: int,
) -> dict:
    """Describe the best-case path back to the registered tier.

    The projection never removes an observed failure.  It assumes every added
    mature natural cycle is a strict pass, so it is a mathematical lower bound
    on additional evidence rather than a forecast, quota, or threshold change.
    """
    if passes < 0 or planned < 0 or passes > planned:
        raise ValueError("invalid pass-rate counts")
    if minimum_slots < 0:
        raise ValueError("minimum_slots must be non-negative")

    baseline_cycles = max(planned, minimum_slots)
    additional_to_baseline = baseline_cycles - planned
    baseline_passes = passes + additional_to_baseline
    baseline_rate = _rate(baseline_passes, baseline_cycles)

    target = None if target_rate is None else Fraction(str(target_rate))
    if target is not None and not 0 <= target <= 1:
        raise ValueError("target_rate must be between zero and one")

    if target is None:
        additional_to_target = None
        finite_recovery_possible = None
        target_reachable_at_baseline = None
    elif target == 0:
        additional_to_target = 0
        finite_recovery_possible = True
        target_reachable_at_baseline = baseline_rate is not None
    elif planned == 0:
        additional_to_target = 1
        finite_recovery_possible = True
        target_reachable_at_baseline = (
            baseline_rate is not None
            and Fraction(baseline_passes, baseline_cycles) >= target
        )
    elif Fraction(passes, planned) >= target:
        additional_to_target = 0
        finite_recovery_possible = True
        target_reachable_at_baseline = (
            baseline_rate is not None
            and Fraction(baseline_passes, baseline_cycles) >= target
        )
    elif target == 1:
        additional_to_target = None
        finite_recovery_possible = False
        target_reachable_at_baseline = False
    else:
        required = (target * planned - passes) / (1 - target)
        additional_to_target = max(
            0,
            (required.numerator + required.denominator - 1)
            // required.denominator,
        )
        finite_recovery_possible = True
        target_reachable_at_baseline = (
            baseline_rate is not None
            and Fraction(baseline_passes, baseline_cycles) >= target
        )

    earliest_cycles = (
        planned + additional_to_target
        if additional_to_target is not None
        else None
    )
    earliest_passes = (
        passes + additional_to_target
        if additional_to_target is not None
        else None
    )
    return {
        "diagnostic_only": True,
        "assumption": (
            "Every additional mature natural cycle is a strict pass; all "
            "observed failures remain in the denominator. This is not a "
            "forecast, trade quota, threshold change, or authorization."
        ),
        "additional_all_pass_cycles_to_minimum_slots": additional_to_baseline,
        "best_case_at_minimum_or_current_denominator": {
            "planned_cycles": baseline_cycles,
            "strict_cycle_passes": baseline_passes,
            "strict_pass_rate": baseline_rate,
            "target_reachable": target_reachable_at_baseline,
        },
        "minimum_additional_all_pass_cycles_to_target": additional_to_target,
        "earliest_total_cycles_at_target_if_no_more_failures": earliest_cycles,
        "earliest_total_passes_at_target_if_no_more_failures": earliest_passes,
        "finite_recovery_possible": finite_recovery_possible,
    }


def _strict_cycle_pass(sla: object) -> bool:
    if not isinstance(sla, dict):
        return False
    if "strict_cycle_pass" in sla:
        return sla.get("strict_cycle_pass") is True
    return sla.get("under_14m30") is True


def _live_failure_kind(live: object) -> str | None:
    """Return a bounded diagnostic kind without changing SLA pass semantics."""
    if not isinstance(live, dict):
        return None
    explicit = str(live.get("failure_kind") or "").strip()
    if explicit:
        return explicit
    if live.get("status") != "failed":
        return None
    gateway = live.get("gateway_abort")
    same_connection = live.get("same_connection_abort")
    if not isinstance(gateway, dict) or not isinstance(same_connection, dict):
        return None
    receipt = same_connection.get("receipt")
    if not isinstance(receipt, dict):
        return None
    identity_bound_terminal = (
        gateway.get("status") == "gateway-terminal-error"
        and gateway.get("terminal_confirmed") is True
        and gateway.get("verification_source")
        == "identity_bound_wrapper_receipt"
        and same_connection.get("receipt_valid") is True
        and receipt.get("gateway_terminal_error_observed") is True
        and receipt.get("gateway_terminal_error_marker") == "all_models_failed"
        and isinstance(receipt.get("exit_code"), int)
        and receipt.get("exit_code") != 0
    )
    return "gateway_terminal_error" if identity_bound_terminal else None


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def _cycle_started_offset_seconds(cycle: str, started_at: object) -> int | None:
    """Return stage start offset from the natural UTC+8 cycle boundary.

    ``live.started_at`` is the first production-stage timestamp available in
    the stage status.  The offset therefore measures the combined delay from
    required-input readiness, dispatch and scheduler hand-off; it must not be
    described as collection time alone.
    """
    if not str(started_at or "").strip():
        return None
    try:
        started = parse_cst(str(started_at))
        cycle_start = parse_cst(cycle)
    except (TypeError, ValueError):
        return None
    return int((started - cycle_start).total_seconds())


def _whole_seconds_from_milliseconds(value: object) -> int | None:
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds < 0:
        return None
    return int(round(milliseconds / 1000.0))


def _seconds_summary(values: list[int]) -> dict:
    clean = [int(value) for value in values if isinstance(value, int)]
    return {
        "observations": len(clean),
        "average_seconds": (
            round(sum(clean) / len(clean), 3) if clean else None),
        "p50_seconds": _percentile(clean, 0.50),
        "p90_seconds": _percentile(clean, 0.90),
        "max_seconds": max(clean) if clean else None,
    }


def _slot_group_summary(label: str, rows: list[dict]) -> dict:
    planned = len(rows)
    live_offsets = [
        row["live_start_offset_seconds"] for row in rows
        if isinstance(row.get("live_start_offset_seconds"), int)
    ]
    child_budgets = [
        row["live_child_budget_seconds"] for row in rows
        if isinstance(row.get("live_child_budget_seconds"), int)
    ]
    live_runtimes = [
        row["live_runtime_seconds"] for row in rows
        if isinstance(row.get("live_runtime_seconds"), int)
    ]
    elapsed = [
        row["complete_cycle_sla"].get("elapsed_seconds") for row in rows
        if isinstance(row.get("complete_cycle_sla"), dict)
        and isinstance(row["complete_cycle_sla"].get("elapsed_seconds"), int)
    ]
    deep_counts = [int(row.get("mtf_deep_dive_count") or 0) for row in rows]
    final_counts = [int(row.get("final_open_card_count") or 0) for row in rows]
    complete = sum(bool(row["complete_cycle_sla"].get("complete")) for row in rows)
    under_threshold = sum(
        bool(row["complete_cycle_sla"].get("under_14m30")) for row in rows)
    passed = sum(
        _strict_cycle_pass(row.get("complete_cycle_sla")) for row in rows)
    return {
        "slot": label,
        "tier": "hourly" if label == ":00" else "quarter",
        "planned_cycles": planned,
        "live_started_cycles": len(live_offsets),
        "complete_cycles": complete,
        "strictly_under_14m30": under_threshold,
        "strict_cycle_passes": passed,
        "failures": planned - passed,
        "strict_pass_rate": _rate(passed, planned),
        "failure_taxonomy": {
            "sla_reason_counts": dict(sorted(Counter(
                str((row.get("complete_cycle_sla") or {}).get("reason") or
                    "unspecified")
                for row in rows
                if not _strict_cycle_pass(row.get("complete_cycle_sla"))
            ).items())),
            "live_failure_kind_counts": dict(sorted(Counter(
                str(row.get("live_failure_kind") or "unspecified_failed")
                for row in rows
                if row.get("live_status") == "failed"
            ).items())),
        },
        "live_start_offset": _seconds_summary(live_offsets),
        "live_child_budget": _seconds_summary(child_budgets),
        "live_runtime": _seconds_summary(live_runtimes),
        "complete_cycle_elapsed": _seconds_summary(elapsed),
        "candidate_observation": {
            "cycles_with_2_or_3_deep_dives": sum(
                2 <= value <= 3 for value in deep_counts),
            "deep_dive_distribution": dict(
                sorted(Counter(deep_counts).items())),
            "average_deep_dives": (
                round(sum(deep_counts) / planned, 4) if planned else None),
            "final_open_card_distribution": dict(
                sorted(Counter(final_counts).items())),
        },
    }


def _coverage_pair_summary(
    observations: list[dict], numerator_field: str, denominator_field: str,
) -> dict:
    pairs = [
        (item.get(numerator_field), item.get(denominator_field))
        for item in observations
        if isinstance(item.get(numerator_field), int)
        and isinstance(item.get(denominator_field), int)
    ]
    numerator = sum(pair[0] for pair in pairs)
    denominator = sum(pair[1] for pair in pairs)
    return {
        "observed_cycles": len(pairs),
        "numerator": numerator,
        "denominator": denominator,
        "rate": _rate(numerator, denominator),
    }


def _coverage_quality_group(label: str, rows: list[dict]) -> dict:
    minimal_rows = [
        row for row in rows
        if thresholds.minimal_decision_contract_active(
            str(row.get("cycle_id") or ""))]
    legacy_rows = [row for row in rows if row not in minimal_rows]
    collection = [
        row["collection_universe"] for row in rows
        if isinstance(row.get("collection_universe"), dict)
        and row["collection_universe"].get("available") is True
    ]
    analysis = [
        row["analysis_coverage"] for row in rows
        if isinstance(row.get("analysis_coverage"), dict)
        and row["analysis_coverage"].get("available") is True
    ]
    candidate_funnels = [
        row["candidate_funnel"] for row in rows
        if isinstance(row.get("candidate_funnel"), dict)
        and row["candidate_funnel"].get("available") is True
    ]
    positions = [
        row["position_review"] for row in rows
        if isinstance(row.get("position_review"), dict)
        and row["position_review"].get("available") is True
        and row["position_review"].get("registered_semantic_active") is True
    ]
    pre_activation_positions = [
        row["position_review"] for row in rows
        if isinstance(row.get("position_review"), dict)
        and row["position_review"].get("available") is True
        and row["position_review"].get("registered_semantic_active") is False
    ]
    candidate_pool = [
        item for item in analysis
        if isinstance(item.get("candidate_pool_count"), int)
    ]
    expected_reviews = sum(
        int(item.get("positions_expected_review") or 0) for item in positions)
    explicit_reviews = sum(
        int(item.get("positions_explicitly_reviewed") or 0) for item in positions)
    return {
        "slot": label,
        "planned_cycles": len(rows),
        "collection_universe": {
            "observed_cycles": len(collection),
            "missing_cycles": len(rows) - len(collection),
            "configured_universe_symbols": sum(
                int(item.get("configured_universe_symbols") or 0)
                for item in collection
            ),
            "ticker_coverage": _coverage_pair_summary(
                collection, "ticker_symbols", "expected_symbols"),
            "candle_coverage": _coverage_pair_summary(
                collection, "candle_symbols", "expected_symbols"),
        },
        "analysis_and_candidate_coverage": {
            "persisted_analysis_cycles": len(analysis),
            "missing_persisted_analysis_cycles": len(rows) - len(analysis),
            "independently_validated_mtf_evidence": sum(
                int(row.get("mtf_deep_dive_count") or 0) for row in rows),
            "three_period_judgment_applicable_cycles": len(legacy_rows),
            "three_period_judgment_retired_cycles": len(minimal_rows),
            "side_neutral_candidate_reviews": sum(
                int(row.get("candidate_review_count") or 0)
                for row in minimal_rows),
            "valid_persisted_deep_dives": sum(
                int(item.get("valid_persisted_deep_dives") or 0)
                for item in analysis
            ),
            "invalid_persisted_deep_dives": sum(
                int(item.get("invalid_persisted_deep_dives") or 0)
                for item in analysis
            ),
            "deep_dive_rejected_candidates": sum(
                int(item.get("deep_dive_rejected_candidates") or 0)
                for item in analysis
            ),
            "final_open_cards": sum(
                int(item.get("final_open_card_count") or 0) for item in analysis),
            "complete_final_open_cards": sum(
                int(item.get("complete_final_open_card_count") or 0)
                for item in analysis
            ),
            "six_field_decision_card_applicable_cycles": len(legacy_rows),
            "six_field_decision_card_retired_cycles": len(minimal_rows),
            "minimal_missing_six_field_card_is_failure": False,
            "candidate_pool_observed_cycles": len(candidate_pool),
            "observed_candidate_pool": sum(
                int(item["candidate_pool_count"]) for item in candidate_pool),
            "not_deep_dived_candidates": sum(
                int(item.get("not_deep_dived_candidate_count") or 0)
                for item in candidate_pool
            ),
            "remaining_budget_limited_candidates": sum(
                int(item.get("remaining_budget_limited_candidates") or 0)
                for item in candidate_pool
            ),
            "evidence_insufficient_candidates": sum(
                int(item.get("evidence_insufficient_candidates") or 0)
                for item in candidate_pool
            ),
            "unclassified_not_deep_dived_candidates": sum(
                int(item.get("unclassified_not_deep_dived_candidates") or 0)
                for item in candidate_pool
            ),
        },
        "candidate_funnel": {
            "observed_cycles": len(candidate_funnels),
            "missing_cycles": len(rows) - len(candidate_funnels),
            "briefing_source_cycles": sum(
                item.get("candidate_pool_source") == "briefing_candidates_v1"
                for item in candidate_funnels
            ),
            "analysis_raw_fallback_cycles": sum(
                item.get("candidate_pool_source") == "analysis_raw_fallback"
                for item in candidate_funnels
            ),
            "candidate_pool": sum(
                int(item.get("candidate_pool_count") or 0)
                for item in candidate_funnels
            ),
            "observed_deep_dives": sum(
                int(item.get("observed_deep_dive_count") or 0)
                for item in candidate_funnels
            ),
            "independently_validated_mtf_evidence": sum(
                int(item.get("independently_validated_mtf_count") or 0)
                for item in candidate_funnels
            ),
            "valid_persisted_deep_dive_receipts": sum(
                int(item.get("valid_persisted_deep_dive_count") or 0)
                for item in candidate_funnels
            ),
            "not_deep_dived_candidates": sum(
                int(item.get("not_deep_dived_candidate_count") or 0)
                for item in candidate_funnels
            ),
            "deep_dive_coverage_rate": _rate(
                sum(int(item.get("observed_deep_dive_count") or 0)
                    for item in candidate_funnels),
                sum(int(item.get("candidate_pool_count") or 0)
                    for item in candidate_funnels),
            ),
            "remaining_budget_limited_candidates": sum(
                int(item.get("remaining_budget_limited_candidates") or 0)
                for item in candidate_funnels
            ),
            "evidence_insufficient_candidates": sum(
                int(item.get("evidence_insufficient_candidates") or 0)
                for item in candidate_funnels
            ),
            "analysis_unavailable_candidates": sum(
                int(item.get("analysis_unavailable_candidates") or 0)
                for item in candidate_funnels
            ),
            "unclassified_not_deep_dived_candidates": sum(
                int(item.get("unclassified_not_deep_dived_candidates") or 0)
                for item in candidate_funnels
            ),
            "source_mismatch_cycles": sum(
                item.get("candidate_pool_source_match") is False
                for item in candidate_funnels
            ),
            "deep_dive_source_mismatch_cycles": sum(
                item.get("deep_dive_source_parity") is False
                for item in candidate_funnels
            ),
            "deep_dives_outside_briefing_pool": sum(
                int(item.get("deep_dive_outside_briefing_pool_count") or 0)
                for item in candidate_funnels
            ),
            "invalid_briefing_candidate_entries": sum(
                int(item.get("invalid_briefing_candidate_entries") or 0)
                for item in candidate_funnels
            ),
        },
        "position_review": {
            "activation_cst": (
                thresholds.STRUCTURED_POSITION_REVIEW_ACTIVATION_CST),
            "exact_live_facts_cycles": len(positions),
            "active_planned_cycles": sum(
                thresholds.structured_position_actions_count_as_review(
                    str(row.get("cycle_id") or ""))
                for row in rows
            ),
            "missing_exact_live_facts_cycles": sum(
                thresholds.structured_position_actions_count_as_review(
                    str(row.get("cycle_id") or ""))
                and not (
                    isinstance(row.get("position_review"), dict)
                    and row["position_review"].get("available") is True
                )
                for row in rows
            ),
            "cycles_with_positions": sum(
                int(item.get("positions_expected_review") or 0) > 0
                for item in positions
            ),
            "positions_expected_review": expected_reviews,
            "positions_explicitly_reviewed": explicit_reviews,
            "explicit_review_rate": _rate(explicit_reviews, expected_reviews),
            "zero_denominator_status": (
                "NO_OPEN_POSITIONS_IN_OBSERVED_CYCLES"
                if positions and expected_reviews == 0 else None
            ),
            "historical_rejudgement": False,
            "pre_activation_diagnostic": {
                "exact_live_facts_cycles": len(pre_activation_positions),
                "positions_expected_review": sum(
                    int(item.get("positions_expected_review") or 0)
                    for item in pre_activation_positions
                ),
                "positions_explicitly_reviewed_under_historical_semantics": sum(
                    int(item.get("positions_explicitly_reviewed") or 0)
                    for item in pre_activation_positions
                ),
            },
        },
    }


def coverage_and_quality_observation(rows: list[dict]) -> dict:
    labels = (":00", ":15", ":30", ":45")
    by_slot = [
        _coverage_quality_group(
            label,
            [row for row in rows if row.get("slot_minute") == label[1:]],
        )
        for label in labels
    ]
    active_start = thresholds.parse_cst(
        thresholds.SLA_V4_PROCESS_SCOPE_ACTIVATION_CST)
    active_rows = [
        row for row in rows
        if thresholds.parse_cst(str(row.get("cycle_id") or "")) >= active_start
    ]
    pre_activation_rows = [
        row for row in rows
        if thresholds.parse_cst(str(row.get("cycle_id") or "")) < active_start
    ]
    minimal_rows = [
        row for row in rows
        if thresholds.minimal_decision_contract_active(
            str(row.get("cycle_id") or ""))]
    legacy_contract_rows = [row for row in rows if row not in minimal_rows]
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "strict_sla_effect": "none",
        "definition": {
            "configured_universe": (
                "per-cycle fast collector transport receipt; retained-log "
                "coverage is reported explicitly and missing logs are never zero-filled"
            ),
            "independently_validated_mtf_evidence": (
                "exact-cycle 15m/1H/4H evidence artifact passing this audit's "
                "independent structural checks; applicable only before the "
                "minimal-policy boundary"
            ),
            "persisted_deep_dive": (
                "analysis_runs.raw.raw.candidates_deep_dived entry with exact "
                "instId, 64-hex evidence_hash, decision and non-empty reason"
            ),
            "candidate_attrition": (
                "candidate-pool denominator comes from the deterministic "
                "briefing_candidates_v1 log when available, otherwise from an "
                "explicit analysis raw denominator; unavailable is not zero-filled"
            ),
            "explicit_position_review": (
                "exact-cycle live_facts position plus same complete instId and "
                "recognized action with a non-empty reason in a structured "
                "review/action or action-bearing agent_judgement clause"
            ),
            "trade_count_quota": False,
            "minimum_open_count": None,
            "minimal_policy_missing_mtf_or_six_field_card_is_failure": False,
        },
        "decision_contract_epochs": {
            "minimal_policy_activation_cst": (
                thresholds.MINIMAL_DECISION_CONTRACT_ACTIVATION_CST),
            "historical_rejudgement": False,
            "legacy_contract": _coverage_quality_group(
                "legacy decision contract", legacy_contract_rows),
            "minimal_policy": _coverage_quality_group(
                "minimal decision contract", minimal_rows),
        },
        "overall": _coverage_quality_group("all", rows),
        "active_v4_process_scope": {
            "activation_cst": thresholds.SLA_V4_PROCESS_SCOPE_ACTIVATION_CST,
            "historical_rejudgement": False,
            "summary": _coverage_quality_group("V4 active", active_rows),
            "by_slot": [
                _coverage_quality_group(
                    label,
                    [
                        row for row in active_rows
                        if row.get("slot_minute") == label[1:]
                    ],
                )
                for label in labels
            ],
        },
        "pre_activation_diagnostic": {
            "retained_in_scan": True,
            "rejudged_under_v4_scope": False,
            "summary": _coverage_quality_group(
                "pre-V4 diagnostic", pre_activation_rows),
            "by_slot": [
                _coverage_quality_group(
                    label,
                    [
                        row for row in pre_activation_rows
                        if row.get("slot_minute") == label[1:]
                    ],
                )
                for label in labels
            ],
        },
        "by_slot": by_slot,
    }


def _model_failure_group(label: str, rows: list[dict]) -> dict:
    gateway_rows = [
        row for row in rows
        if row.get("live_failure_kind") == "gateway_terminal_error"
    ]
    observations = [
        row["model_failure_observation"] for row in gateway_rows
        if isinstance(row.get("model_failure_observation"), dict)
        and row["model_failure_observation"].get("available") is True
    ]
    attempts = [
        attempt
        for observation in observations
        for attempt in observation.get("attempts") or []
        if isinstance(attempt, dict)
    ]
    return {
        "slot": label,
        "gateway_terminal_error_cycles": len(gateway_rows),
        "cycles_with_provider_detail": len(observations),
        "cycles_missing_provider_detail": len(gateway_rows) - len(observations),
        "complete_provider_parse_cycles": sum(
            observation.get("complete") is True for observation in observations
        ),
        "provider_attempts": len(attempts),
        "attempts_by_model": dict(sorted(Counter(
            str(attempt.get("model") or "unknown") for attempt in attempts
        ).items())),
        "attempts_by_error_kind": dict(sorted(Counter(
            str(attempt.get("error_kind") or "other") for attempt in attempts
        ).items())),
        "cycles": [
            {
                "cycle_id": row.get("cycle_id"),
                "attempts": row["model_failure_observation"].get("attempts"),
            }
            for row in gateway_rows
            if isinstance(row.get("model_failure_observation"), dict)
            and row["model_failure_observation"].get("available") is True
        ],
    }


def model_failure_observation(rows: list[dict]) -> dict:
    active_start = thresholds.parse_cst(
        thresholds.SLA_V4_PROCESS_SCOPE_ACTIVATION_CST)
    active_rows = [
        row for row in rows
        if thresholds.parse_cst(str(row.get("cycle_id") or "")) >= active_start
    ]
    labels = (":00", ":15", ":30", ":45")
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "strict_sla_effect": "none",
        "definition": (
            "provider/model attempts parsed from the exact-cycle live trigger "
            "FallbackSummaryError; no provider calls are made by this audit"
        ),
        "overall": _model_failure_group("all", rows),
        "active_v4_process_scope": {
            "activation_cst": thresholds.SLA_V4_PROCESS_SCOPE_ACTIVATION_CST,
            "summary": _model_failure_group("V4 active", active_rows),
            "by_slot": [
                _model_failure_group(
                    label,
                    [
                        row for row in active_rows
                        if row.get("slot_minute") == label[1:]
                    ],
                )
                for label in labels
            ],
        },
    }


def _difference(left: object, right: object, *, digits: int = 3) -> float | None:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    return round(float(left) - float(right), digits)


def slot_observation(rows: list[dict]) -> dict:
    """Describe time/candidate allocation for :00/:15/:30/:45 separately."""
    labels = (":00", ":15", ":30", ":45")
    by_slot = []
    for label in labels:
        minute = label[1:]
        grouped = [row for row in rows if row.get("slot_minute") == minute]
        by_slot.append(_slot_group_summary(label, grouped))

    hourly = by_slot[0]
    quarter_rows = [row for row in rows if row.get("slot_minute") != "00"]
    pooled_quarter = _slot_group_summary(":15/:30/:45 pooled", quarter_rows)
    hourly_start = hourly["live_start_offset"]["average_seconds"]
    quarter_start = pooled_quarter["live_start_offset"]["average_seconds"]
    hourly_deep = hourly["candidate_observation"]["average_deep_dives"]
    quarter_deep = pooled_quarter["candidate_observation"]["average_deep_dives"]
    pass_gap = _difference(
        hourly.get("strict_pass_rate"),
        pooled_quarter.get("strict_pass_rate"),
        digits=6,
    )
    return {
        "definition": {
            "slot_population": "all planned natural 15-minute cycles",
            "hourly_slot": ":00",
            "quarter_slots": [":15", ":30", ":45"],
            "live_start_offset": (
                "live.started_at minus natural cycle boundary; includes "
                "required-input readiness, dispatcher and scheduler delay, "
                "so it is not collection-only latency"
            ),
            "candidate_depth": (
                "valid exact-cycle 15m/1H/4H evidence artifacts; diagnostic "
                "only, never a trade or direction quota"
            ),
        },
        "by_slot": by_slot,
        "hourly_vs_pooled_quarter": {
            "hourly": hourly,
            "pooled_quarter": pooled_quarter,
            "average_live_start_offset_delta_seconds": _difference(
                hourly_start, quarter_start),
            "strict_pass_rate_gap_percentage_points": (
                round(pass_gap * 100.0, 3) if pass_gap is not None else None),
            "average_deep_dive_delta": _difference(hourly_deep, quarter_deep),
        },
    }


def _exact_daily_cron_slot(expression: object) -> tuple[int, int] | None:
    """Return ``(hour, minute)`` for a simple exact daily cron expression."""
    parts = str(expression or "").strip().split()
    if len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    minute, hour = int(parts[0]), int(parts[1])
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        return None
    if parts[2:] != ["*", "*", "*"]:
        return None
    return hour, minute


def _milliseconds_to_cst(value: object) -> str | None:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return None
    if milliseconds <= 0:
        return None
    return datetime.fromtimestamp(
        milliseconds / 1000.0, tz=CST).isoformat()


def openclaw_contention_observation(
    openclaw_db: Path | None,
    cycle_rows: list[dict],
    *,
    run_sample_size: int = 14,
) -> dict:
    """Observe long exact-time OpenClaw jobs without assigning unique causality.

    A task whose median duration is at least the 15-minute business cadence is
    guaranteed to overlap a later natural cycle when it shares the production
    gateway.  This is diagnostic evidence only: it does not change the strict SLA
    numerator, infer that the job is the sole cause, or authorize a cron mutation.
    """
    base = {
        "status": "UNAVAILABLE",
        "source": str(openclaw_db) if openclaw_db else None,
        "source_mode": "sqlite_mode_ro",
        "run_sample_size": int(run_sample_size),
        "trading_cadence_seconds": int(SLOT.total_seconds()),
        "causality": "scheduled_overlap_is_not_unique_causal_proof",
        "strict_sla_effect": "diagnostic_only",
        "schedule_change_authorization": "USER_APPROVAL_REQUIRED",
        "jobs": [],
    }
    if openclaw_db is None or not openclaw_db.exists():
        base["reason"] = "openclaw_db_missing"
        return base
    uri = openclaw_db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        current_storage = "cron_run_receipts" in tables
        if "cron_jobs" not in tables or not (
                current_storage or "cron_run_logs" in tables):
            base["reason"] = "cron_tables_missing"
            return base
        if current_storage:
            jobs = connection.execute(
                "SELECT store_key,job_id,name,"
                "json_extract(job_json,'$.schedule.expr') AS schedule_expr,"
                "json_extract(job_json,'$.schedule.tz') AS schedule_tz,payload_kind,"
                "json_extract(job_json,'$.payload.timeoutSeconds') AS payload_timeout_seconds,"
                "json_extract(state_json,'$.runningAtMs') AS running_at_ms,"
                "json_extract(state_json,'$.lastRunAtMs') AS last_run_at_ms,"
                "json_extract(state_json,'$.lastRunStatus') AS last_run_status,"
                "json_extract(state_json,'$.lastDurationMs') AS last_duration_ms "
                "FROM cron_jobs WHERE enabled=1 "
                "AND json_extract(job_json,'$.schedule.kind')='cron' ORDER BY name,job_id"
            ).fetchall()
        else:
            jobs = connection.execute(
                "SELECT store_key,job_id,name,schedule_expr,schedule_tz,"
                "payload_kind,payload_timeout_seconds,running_at_ms,last_run_at_ms,"
                "last_run_status,last_duration_ms FROM cron_jobs "
                "WHERE enabled=1 AND schedule_kind='cron' ORDER BY name,job_id"
            ).fetchall()
        observations = []
        for job in jobs:
            exact_slot = _exact_daily_cron_slot(job["schedule_expr"])
            if exact_slot is None:
                continue
            durations = [
                int(row[0]) / 1000.0
                for row in connection.execute(
                    ("SELECT finished_at_ms-started_at_ms FROM cron_run_receipts "
                     "WHERE store_key=? AND job_id=? AND status='ok' "
                     "AND finished_at_ms>=started_at_ms "
                     "ORDER BY finished_at_ms DESC LIMIT ?" if current_storage else
                    "SELECT duration_ms FROM cron_run_logs "
                    "WHERE store_key=? AND job_id=? AND status='ok' "
                    "AND duration_ms IS NOT NULL AND duration_ms>=0 "
                    "ORDER BY seq DESC LIMIT ?"),
                    (job["store_key"], job["job_id"], int(run_sample_size)),
                ).fetchall()
            ]
            if not durations:
                continue
            median_seconds = round(float(statistics.median(durations)), 3)
            hour, minute = exact_slot
            same_clock = []
            for row in cycle_rows:
                try:
                    stamp = parse_cst(str(row.get("cycle_id") or ""))
                except (TypeError, ValueError):
                    continue
                if stamp.hour == hour and stamp.minute == minute:
                    same_clock.append(row)
            same_clock_passes = sum(
                _strict_cycle_pass(row.get("complete_cycle_sla"))
                for row in same_clock
            )
            same_clock_failures = len(same_clock) - same_clock_passes
            guaranteed_overlap = median_seconds >= SLOT.total_seconds()
            observations.append({
                "job_id": str(job["job_id"]),
                "name": str(job["name"]),
                "schedule_expr": str(job["schedule_expr"]),
                "schedule_tz": job["schedule_tz"],
                "exact_daily_slot_cst": f"{hour:02d}:{minute:02d}",
                "payload_kind": job["payload_kind"],
                "payload_timeout_seconds": job["payload_timeout_seconds"],
                "successful_duration_samples": len(durations),
                "duration_seconds": {
                    "minimum": round(min(durations), 3),
                    "median": median_seconds,
                    "maximum": round(max(durations), 3),
                    "latest": round(durations[0], 3),
                },
                "median_longer_than_trading_cadence": guaranteed_overlap,
                "guaranteed_to_overlap_a_later_cycle_on_same_gateway": (
                    guaranteed_overlap),
                "currently_running_since_cst": _milliseconds_to_cst(
                    job["running_at_ms"]),
                "last_run_at_cst": _milliseconds_to_cst(job["last_run_at_ms"]),
                "last_run_status": job["last_run_status"],
                "same_clock_cycle_summary": {
                    "planned_cycles": len(same_clock),
                    "strictly_under_14m30": same_clock_passes,
                    "failures": same_clock_failures,
                    "strict_pass_rate": _rate(
                        same_clock_passes, len(same_clock)),
                    "cycle_ids": [row.get("cycle_id") for row in same_clock],
                },
                "risk_classification": (
                    "HIGH_CONFIDENCE_SCHEDULED_GATEWAY_OVERLAP"
                    if guaranteed_overlap and job["payload_kind"] == "agentTurn"
                    else (
                        "LONG_SCHEDULED_OVERLAP"
                        if guaranteed_overlap
                        else "BELOW_CADENCE_DIAGNOSTIC"
                    )
                ),
            })
    finally:
        connection.close()
    base["status"] = "OBSERVED"
    base["jobs"] = observations
    high_risk = [
        row for row in observations
        if row["risk_classification"]
        == "HIGH_CONFIDENCE_SCHEDULED_GATEWAY_OVERLAP"
    ]
    base["high_risk_job_count"] = len(high_risk)
    base["high_risk_job_ids"] = [row["job_id"] for row in high_risk]
    base["recommendation"] = (
        "A task whose median duration exceeds the 15-minute cadence cannot be "
        "made non-overlapping by changing only its cron minute on the same "
        "gateway; disabling or isolating it requires explicit user approval."
    )
    return base


def audit_complete_cycle_sla(
    *,
    forward_start: datetime,
    as_of: datetime,
    finality_seconds: int,
    minimum_slots: int,
    status_dir: Path,
    analysis_db: Path,
    mtf_dir: Path,
    openclaw_db: Path | None = None,
    live_trades_db: Path | None = None,
    collect_log_dir: Path | None = None,
    briefing_log_dir: Path | None = None,
    trigger_log_dir: Path | None = None,
    candidate_evidence_dir: Path | None = None,
) -> dict:
    cycles = planned_cycles(forward_start, as_of, finality_seconds)
    cards = open_card_counts(analysis_db, cycles)
    analysis_observations = analysis_coverage_observations(analysis_db, cycles)
    collection_observations = collection_universe_observations(
        collect_log_dir, cycles)
    briefing_observations = briefing_candidate_observations(
        briefing_log_dir, cycles)
    model_observations = model_failure_observations(trigger_log_dir, cycles)
    position_observations = position_review_observations(live_trades_db, cycles)
    rows = []
    elapsed_values: list[int] = []
    complete_count = under_threshold_count = strict_pass_count = 0
    deep_counts: list[int] = []
    final_counts: list[int] = []

    for cycle in cycles:
        safe_cycle = cycle.replace(":", "-")
        live = load_json(status_dir / f"live-{safe_cycle}.json")
        push = load_json(status_dir / f"push-{safe_cycle}.json")
        # Always rebuild from the primary live + post-reconcile evidence.  Older
        # push status files may contain a pre-hardening SLA object that counted
        # a successfully delivered failure report even though live itself had
        # failed; that cached derived field is not authoritative.
        if isinstance(push, dict):
            sla = stage_runner.build_complete_cycle_sla(
                cycle,
                push.get("post_live_reconcile"),
                live_status=live if isinstance(live, dict) else {},
            )
        else:
            sla = stage_runner.build_complete_cycle_sla(
                cycle,
                {},
                live_status=live if isinstance(live, dict) else {},
            )
        # A current collection-failure report is built only after stage_runner
        # independently validates the canonical collection terminal, proves
        # the live execution path absent, and completes a clean monitor.  The
        # generic rebuild above deliberately ignores cached success fields,
        # but without this narrow diagnostic carry-forward it mislabels that
        # proven failure as ``collection_gate_missing``.  Preserve only the
        # failure *kind*; cached complete/pass/latency values remain unusable.
        cached_sla = push.get("complete_cycle_sla") if isinstance(push, dict) else None
        monitor = push.get("post_live_reconcile") if isinstance(push, dict) else None
        if (
            isinstance(push, dict)
            and push.get("stage") == "push"
            and push.get("cycle_id") == cycle
            and push.get("mode") == "failure_report"
            and push.get("status") == "succeeded"
            and push.get("returncode") == 0
            and isinstance(monitor, dict)
            and monitor.get("rc") == 0
            and monitor.get("timed_out") is False
            and isinstance(cached_sla, dict)
            and cached_sla.get("schema_version") == sla.get("schema_version")
            and cached_sla.get("measurement") == sla.get("measurement")
            and cached_sla.get("threshold_seconds") == sla.get("threshold_seconds")
            and cached_sla.get("comparison") == sla.get("comparison")
            and cached_sla.get("complete") is False
            and cached_sla.get("under_14m30") is False
            and cached_sla.get("strict_cycle_pass") is False
            and cached_sla.get("status") == "incomplete"
            and cached_sla.get("reason") == "upstream_collection_failed"
            and cached_sla.get("upstream_failure_kind") == "collection_gate_failed"
            and sla.get("complete") is False
            and sla.get("strict_cycle_pass") is False
            and sla.get("reason") in {
                "collection_gate_missing", "live_stage_status_missing",
            }
        ):
            sla = {
                **sla,
                "status": "incomplete",
                "reason": "upstream_collection_failed",
                "upstream_failure_kind": "collection_gate_failed",
            }

        complete = bool(sla.get("complete"))
        under = bool(sla.get("under_14m30"))
        strict_pass = _strict_cycle_pass(sla)
        if complete:
            complete_count += 1
        if under:
            under_threshold_count += 1
        if strict_pass:
            strict_pass_count += 1
        elapsed = sla.get("elapsed_seconds")
        if isinstance(elapsed, int):
            elapsed_values.append(elapsed)

        analysis_coverage = analysis_observations.get(cycle, {
            "available": False,
            "reason": "persisted_analysis_unavailable",
        })
        if thresholds.decision_restriction_removal_active(cycle):
            review_symbols = list(
                analysis_coverage.get("persisted_deep_dive_symbols") or [])
            deep_count = int(
                analysis_coverage.get("valid_persisted_deep_dives") or 0)
            mtf_symbols = (
                [] if thresholds.minimal_decision_contract_active(cycle)
                else review_symbols)
        else:
            mtf_symbols = valid_mtf_evidence(mtf_dir, cycle)
            review_symbols = mtf_symbols
            deep_count = len(mtf_symbols)
        final_count = int(cards.get(cycle, 0))
        collection_universe = collection_observations.get(cycle, {
            "available": False,
            "reason": "retained_collect_receipt_unavailable",
        })
        position_review = position_observations.get(cycle, {
            "available": False,
            "reason": "exact_live_facts_unavailable",
        })
        briefing_candidates = briefing_observations.get(cycle, {
            "available": False,
            "reason": "briefing_candidate_receipt_unavailable",
        })
        candidate_funnel = candidate_funnel_observation(
            briefing_candidates, analysis_coverage, mtf_symbols)
        model_failure = model_observations.get(cycle, {
            "available": False,
            "reason": "exact_cycle_provider_failure_detail_unavailable",
        })
        live_started_offset = _cycle_started_offset_seconds(
            cycle, live.get("started_at") if isinstance(live, dict) else None)
        live_runtime = _whole_seconds_from_milliseconds(
            live.get("duration_ms") if isinstance(live, dict) else None)
        child_budget = None
        if isinstance(live, dict):
            try:
                raw_child_budget = live.get("child_budget_seconds")
                if raw_child_budget is not None and float(raw_child_budget) >= 0:
                    child_budget = int(round(float(raw_child_budget)))
            except (TypeError, ValueError):
                child_budget = None
        deep_counts.append(deep_count)
        final_counts.append(final_count)
        rows.append({
            "cycle_id": cycle,
            "slot_minute": cycle[-2:],
            "tier": "hourly" if cycle.endswith(":00") else "quarter",
            "live_status": live.get("status") if isinstance(live, dict) else "missing",
            "live_failure_kind": _live_failure_kind(live),
            "push_status": push.get("status") if isinstance(push, dict) else "missing",
            "live_start_offset_seconds": live_started_offset,
            "live_child_budget_seconds": child_budget,
            "live_runtime_seconds": live_runtime,
            "complete_cycle_sla": sla,
            "decision_policy": (
                thresholds.MINIMAL_DECISION_CONTRACT_POLICY
                if thresholds.minimal_decision_contract_active(cycle)
                else "legacy"),
            "three_period_judgment_required": (
                thresholds.three_period_judgment_required(cycle)),
            "six_field_decision_card_required": (
                thresholds.six_field_decision_card_required(cycle)),
            "mtf_deep_dive_count": (
                None if thresholds.minimal_decision_contract_active(cycle)
                else deep_count),
            "mtf_deep_dive_symbols": (
                None if thresholds.minimal_decision_contract_active(cycle)
                else mtf_symbols),
            "candidate_review_count": (
                deep_count if thresholds.minimal_decision_contract_active(cycle)
                else None),
            "candidate_review_symbols": (
                review_symbols
                if thresholds.minimal_decision_contract_active(cycle) else None),
            "final_open_card_count": final_count,
            "collection_universe": collection_universe,
            "analysis_coverage": analysis_coverage,
            "briefing_candidates": briefing_candidates,
            "candidate_funnel": candidate_funnel,
            "model_failure_observation": model_failure,
            "position_review": position_review,
        })

    planned = len(cycles)
    failures = planned - strict_pass_count
    if failures:
        overall_status = "NOT_MET"
    elif planned < minimum_slots:
        overall_status = "PENDING_FORWARD_EVIDENCE"
    else:
        overall_status = "MET"

    registration = thresholds.sla_v3_registration_facts(as_of)
    tier_registration = registration["pass_rate_tier"]
    tier_activation_text = thresholds.sla_pass_rate_window_activation_cst(as_of)
    tier_activation = thresholds.parse_cst(tier_activation_text)
    raw_tier_rows = [
        row for row in rows
        if thresholds.parse_cst(str(row.get("cycle_id") or ""))
        >= tier_activation
    ]
    registered_exceptions = list(
        tier_registration.get("acceptance_exceptions") or [])
    exception_by_cycle = {
        str(item.get("cycle_id") or ""): item
        for item in registered_exceptions
        if isinstance(item, dict) and item.get("cycle_id")
    }
    tier_rows = []
    excluded_tier_rows = []
    for row in raw_tier_rows:
        cycle_id = str(row.get("cycle_id") or "")
        exception = exception_by_cycle.get(cycle_id)
        if exception is None:
            row["pass_rate_acceptance"] = {
                "included": True,
                "scope": "active_tier_acceptance_denominator",
            }
            tier_rows.append(row)
            continue
        row["pass_rate_acceptance"] = {
            "included": False,
            "scope": exception.get("scope"),
            "reason": exception.get("reason"),
            "decision_cst": exception.get("decision_cst"),
            "raw_strict_cycle_pass": _strict_cycle_pass(
                row.get("complete_cycle_sla")),
            "raw_cycle_fact_preserved": True,
        }
        excluded_tier_rows.append(row)
    pre_tier_rows = [
        row for row in rows
        if thresholds.parse_cst(str(row.get("cycle_id") or ""))
        < tier_activation
    ]
    raw_tier_planned = len(raw_tier_rows)
    raw_tier_passes = sum(
        _strict_cycle_pass(row.get("complete_cycle_sla"))
        for row in raw_tier_rows)
    raw_tier_rate = _rate(raw_tier_passes, raw_tier_planned)
    tier_planned = len(tier_rows)
    tier_passes = sum(
        _strict_cycle_pass(row.get("complete_cycle_sla")) for row in tier_rows)
    tier_rate = _rate(tier_passes, tier_planned)
    tier_target = thresholds.sla_pass_rate_target(as_of)
    tier_minimum_slots = thresholds.sla_pass_rate_minimum_slots(as_of)
    if tier_target is None:
        tier_status = "NOT_ACTIVE"
    elif tier_planned < tier_minimum_slots:
        tier_status = "PENDING_FORWARD_EVIDENCE"
    elif tier_rate is not None and tier_rate >= tier_target:
        tier_status = "MET"
    else:
        tier_status = "NOT_MET"
    if tier_target is None:
        raw_tier_status = "NOT_ACTIVE"
    elif raw_tier_planned < tier_minimum_slots:
        raw_tier_status = "PENDING_FORWARD_EVIDENCE"
    elif raw_tier_rate is not None and raw_tier_rate >= tier_target:
        raw_tier_status = "MET"
    else:
        raw_tier_status = "NOT_MET"
    tier_recovery = _pass_rate_recovery_projection(
        passes=tier_passes,
        planned=tier_planned,
        target_rate=tier_target,
        minimum_slots=tier_minimum_slots,
    )
    prior_tiers = []
    for prior in tier_registration.get("prior_tier_registrations") or []:
        prior_activation = thresholds.parse_cst(prior["activation_cst"])
        prior_end = thresholds.parse_cst(prior["end_exclusive_cst"])
        prior_rows = [
            row for row in rows
            if prior_activation
            <= thresholds.parse_cst(str(row.get("cycle_id") or ""))
            < prior_end
        ]
        prior_planned = len(prior_rows)
        prior_passes = sum(
            _strict_cycle_pass(row.get("complete_cycle_sla"))
            for row in prior_rows
        )
        prior_rate = _rate(prior_passes, prior_planned)
        prior_minimum = int(prior["minimum_slots"])
        prior_target = float(prior["target_rate"])
        if prior_planned < prior_minimum:
            prior_status = "INSUFFICIENT_EVIDENCE"
        elif prior_rate is not None and prior_rate >= prior_target:
            prior_status = "MET"
        else:
            prior_status = "NOT_MET"
        frozen = _frozen_prior_tier_result(
            prior, forward_start=forward_start, as_of=as_of)
        if frozen is not None:
            prior_tiers.append({
                **prior,
                **frozen,
                "retained_in_scan": True,
                "rejudged_under_current_scope": False,
                "current_retention_diagnostic": {
                    "planned_cycles": prior_planned,
                    "strict_cycle_passes": prior_passes,
                    "strict_pass_rate": prior_rate,
                    "status_under_incomplete_flat_retention": prior_status,
                    "acceptance_effect": "none_closed_result_is_frozen",
                },
            })
        else:
            prior_tiers.append({
                **prior,
                "planned_cycles": prior_planned,
                "strict_cycle_passes": prior_passes,
                "strict_pass_rate": prior_rate,
                "status": prior_status,
                "retained_in_scan": True,
                "rejudged_under_current_scope": False,
                "frozen_closed_result": False,
            })

    contention = openclaw_contention_observation(openclaw_db, rows)
    candidate_rollout = candidate_rollout_audit.audit_candidate_bundle_rollout(
        as_of=as_of,
        evidence_dir=(candidate_evidence_dir or DEFAULT_CANDIDATE_EVIDENCE_DIR),
        analysis_db=analysis_db,
        status_dir=status_dir,
        finality_seconds=finality_seconds,
    )
    failure_reason_counts = Counter(
        str((row.get("complete_cycle_sla") or {}).get("reason") or
            "unspecified")
        for row in rows
        if not _strict_cycle_pass(row.get("complete_cycle_sla"))
    )
    live_failure_kind_counts = Counter(
        str(row.get("live_failure_kind") or "unspecified_failed")
        for row in rows
        if row.get("live_status") == "failed"
    )
    return {
        "schema_version": 4,
        "generated_at": as_of.strftime("%Y-%m-%d %H:%M:%S%z"),
        "forward_window": {
            "start_cst": forward_start.isoformat(),
            "mature_end_inclusive": cycles[-1] if cycles else None,
            "finality_seconds": finality_seconds,
            "minimum_slots": minimum_slots,
            "baseline_fixed": True,
        },
        "definition": {
            "clock_start": "natural_cycle_boundary_cst",
            "clock_stop": "per_cycle_registered_measurement",
            "clock_stop_effective_at_as_of": registration["clock_stop"],
            "threshold_seconds": thresholds.COMPLETE_CYCLE_SLA_SECONDS,
            "comparison": "<",
            "missing_failed_skipped_or_exactly_870": "fail",
            "measurement_migration": registration,
        },
        "strict_sla": {
            "planned_cycles": planned,
            "complete_cycles": complete_count,
            "strictly_under_14m30": under_threshold_count,
            "strict_cycle_passes": strict_pass_count,
            "failures": failures,
            "completion_rate": _rate(complete_count, planned),
            "strict_pass_rate": _rate(strict_pass_count, planned),
            "p50_seconds": _percentile(elapsed_values, 0.50),
            "p90_seconds": _percentile(elapsed_values, 0.90),
            "max_seconds": max(elapsed_values) if elapsed_values else None,
            "failure_taxonomy": {
                "sla_reason_counts": dict(sorted(
                    failure_reason_counts.items())),
                "live_failure_kind_counts": dict(sorted(
                    live_failure_kind_counts.items())),
                "interpretation": (
                    "Raw counts preserve every failed or missing planned cycle, "
                    "including exact cycles that a later user decision excludes "
                    "only from the active tier acceptance denominator."
                ),
            },
            "status": tier_status if tier_target is not None else overall_status,
            "zero_failure_diagnostic_status": overall_status,
            "pass_rate_tier": {
                "activation_cst": tier_activation_text,
                "tier": thresholds.sla_pass_rate_tier_index(as_of),
                "target_rate": tier_target,
                "minimum_slots": tier_minimum_slots,
                "planned_cycles": tier_planned,
                "strict_cycle_passes": tier_passes,
                "strict_pass_rate": tier_rate,
                "status": tier_status,
                "next_tier_registered": bool(
                    tier_registration.get("next_tier_registered")),
                "next_tier": tier_registration.get("next_tier"),
                "prior_tiers": prior_tiers,
                "acceptance_exceptions": registered_exceptions,
                "excluded_cycle_count": len(excluded_tier_rows),
                "excluded_cycles_observed": [{
                    "cycle_id": row.get("cycle_id"),
                    "raw_strict_cycle_pass": _strict_cycle_pass(
                        row.get("complete_cycle_sla")),
                    "live_status": row.get("live_status"),
                    "live_failure_kind": row.get("live_failure_kind"),
                    "raw_sla_reason": (
                        (row.get("complete_cycle_sla") or {}).get("reason")),
                    "exception": exception_by_cycle.get(
                        str(row.get("cycle_id") or "")),
                } for row in excluded_tier_rows],
                "raw_including_exceptions": {
                    "planned_cycles": raw_tier_planned,
                    "strict_cycle_passes": raw_tier_passes,
                    "strict_pass_rate": raw_tier_rate,
                    "status": raw_tier_status,
                    "diagnostic_only": True,
                },
                "historical_rejudgement": bool(
                    tier_registration.get("historical_rejudgement")),
                "historical_rejudgement_scope": list(
                    tier_registration.get("historical_rejudgement_scope") or []),
                "raw_cycle_facts_preserved": True,
                "pre_activation_diagnostic": {
                    "planned_cycles": len(pre_tier_rows),
                    "strict_cycle_passes": sum(
                        _strict_cycle_pass(row.get("complete_cycle_sla"))
                        for row in pre_tier_rows),
                    "strict_pass_rate": _rate(
                        sum(_strict_cycle_pass(
                            row.get("complete_cycle_sla"))
                            for row in pre_tier_rows),
                        len(pre_tier_rows),
                    ),
                    "retained_in_scan": True,
                    "rejudged_under_current_scope": False,
                },
                "recovery_projection": tier_recovery,
            },
        },
        "candidate_observation": {
            "target_deep_dive_range": [3, 8],
            "target_deep_dive_range_activated_cst": "2026-08-27T23:45:00+08:00",
            "legacy_target_deep_dive_ranges": [[2, 3], [3, 5]],
            "target_is_not_a_trade_quota": True,
            "cycles_within_target_range": sum(3 <= value <= 8 for value in deep_counts),
            "deep_dive_distribution": dict(sorted(Counter(deep_counts).items())),
            "maximum_deep_dives": max(deep_counts) if deep_counts else None,
            "average_deep_dives": (
                round(sum(deep_counts) / len(deep_counts), 4) if deep_counts else None),
            "final_open_card_distribution": dict(sorted(Counter(final_counts).items())),
            "maximum_final_open_cards": max(final_counts) if final_counts else None,
            "no_minimum_final_open_cards": True,
            "no_direction_quota": True,
            "minimal_policy": {
                "activation_cst": (
                    thresholds.MINIMAL_DECISION_CONTRACT_ACTIVATION_CST),
                "three_period_or_four_state_target_applied": False,
                "six_field_decision_card_target_applied": False,
                "candidate_review_distribution": dict(sorted(Counter(
                    int(row.get("candidate_review_count") or 0)
                    for row in rows
                    if thresholds.minimal_decision_contract_active(
                        str(row.get("cycle_id") or ""))
                ).items())),
                "historical_rejudgement": False,
            },
        },
        "slot_observation": slot_observation(rows),
        "coverage_and_quality_observation": coverage_and_quality_observation(rows),
        "candidate_bundle_rollout": candidate_rollout,
        "model_failure_observation": model_failure_observation(rows),
        "openclaw_contention_observation": contention,
        "cycles": rows,
        "safety": {
            "business_databases_read_only": True,
            "collection_logs_read_only": True,
            "briefing_logs_read_only": True,
            "trigger_logs_read_only": True,
            "network_calls": 0,
            "orders": 0,
            "dispatches": 0,
            "repairs_or_retries": 0,
            "openclaw_state_db_read_only": True,
            "cron_changes": 0,
        },
    }


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="strict complete-cycle SLA forward audit")
    parser.add_argument("--forward-start", default=DEFAULT_FORWARD_START)
    parser.add_argument("--as-of")
    parser.add_argument("--finality-seconds", type=int, default=900)
    parser.add_argument("--minimum-slots", type=int, default=96)
    parser.add_argument("--status-dir", default=str(DEFAULT_STATUS_DIR))
    parser.add_argument("--analysis-db", default=str(DEFAULT_ANALYSIS_DB))
    parser.add_argument("--live-trades-db", default=str(DEFAULT_LIVE_TRADES_DB))
    parser.add_argument("--mtf-dir", default=str(DEFAULT_MTF_DIR))
    parser.add_argument("--collect-log-dir", default=str(DEFAULT_COLLECT_LOG_DIR))
    parser.add_argument(
        "--briefing-log-dir", default=str(DEFAULT_BRIEFING_LOG_DIR))
    parser.add_argument("--trigger-log-dir", default=str(DEFAULT_TRIGGER_LOG_DIR))
    parser.add_argument(
        "--candidate-evidence-dir",
        default=str(DEFAULT_CANDIDATE_EVIDENCE_DIR),
    )
    parser.add_argument("--openclaw-db", default=str(DEFAULT_OPENCLAW_DB))
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT))
    parser.add_argument("--execution-context", choices=("production", "test", "probe"))
    parser.add_argument("--artifact-root")
    args = parser.parse_args(argv)
    try:
        output, context, context_fields = resolve_audit_output(
            args.json_out,
            tool_name="audit_complete_cycle_sla",
            execution_context=args.execution_context,
            artifact_root=args.artifact_root,
        )
        result = audit_complete_cycle_sla(
            forward_start=parse_cst(args.forward_start),
            as_of=parse_cst(args.as_of) if args.as_of else datetime.now(CST),
            finality_seconds=args.finality_seconds,
            minimum_slots=args.minimum_slots,
            status_dir=Path(args.status_dir),
            analysis_db=Path(args.analysis_db),
            mtf_dir=Path(args.mtf_dir),
            openclaw_db=Path(args.openclaw_db),
            live_trades_db=Path(args.live_trades_db),
            collect_log_dir=Path(args.collect_log_dir),
            briefing_log_dir=Path(args.briefing_log_dir),
            trigger_log_dir=Path(args.trigger_log_dir),
            candidate_evidence_dir=Path(args.candidate_evidence_dir),
        )
        result["artifact_context"] = context_fields
        atomic_write_json(output, result)
    except Exception as exc:  # noqa: BLE001 - CLI must emit a bounded cause
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({
        "ok": True,
        "artifact": str(output),
        "execution_context": context,
        "strict_sla": result["strict_sla"],
        "candidate_observation": result["candidate_observation"],
        "slot_observation": result["slot_observation"],
        "coverage_and_quality_observation": (
            result["coverage_and_quality_observation"]),
        "candidate_bundle_rollout": result["candidate_bundle_rollout"],
        "model_failure_observation": result["model_failure_observation"],
        "openclaw_contention_observation": (
            result["openclaw_contention_observation"]),
        "forward_window": result["forward_window"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
