# -*- coding: utf-8 -*-
"""Read-only exact-slot watchdog for prolonged successful zero-OPEN analysis.

The evaluator never writes business databases, dispatches work, retries a cycle,
or calls an executor.  It may publish one immutable quality JSON artifact; the
stage runner separately owns any deduplicated operational alert.
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scripts import _acceptance_thresholds as thresholds


SCHEMA = "zero_open_watchdog_v1"
SCHEMA_V2 = "zero_open_watchdog_v2"
OPEN_ACTIONS = {"open_long", "open_short"}
DEFAULT_ANALYSIS_DB = Path(_public_project_path('db', 'analysis.db'))
DEFAULT_LIVE_TRADES_DB = Path(_public_project_path('db', 'live_trades.db'))
DEFAULT_STAGE_STATUS_DIR = Path(_public_project_path('logs', 'stage-status'))
DEFAULT_ARTIFACT_DIR = Path(os.environ.get(
    "OKX_ZERO_OPEN_WATCHDOG_DIR", _public_project_path('reports', 'quality')))


def _cycle(value: str) -> datetime:
    parsed = datetime.strptime(str(value), "%Y-%m-%dT%H:%M")
    if parsed.minute not in {0, 15, 30, 45}:
        raise ValueError("cycle must be an exact 15-minute natural slot")
    return parsed


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _raw_open_count(raw: Any) -> int | None:
    payload = _json_object(raw)
    signals = payload.get("signals")
    if not isinstance(signals, list):
        return None
    return sum(
        isinstance(signal, dict)
        and str(signal.get("action") or "").strip().lower() in OPEN_ACTIONS
        for signal in signals
    )


def _inner_raw(raw: Any) -> dict:
    payload = _json_object(raw)
    return _json_object(payload.get("raw"))


def _closure_business_terminal_errors(
    cycle_id: str,
    *,
    stage_status_dir: Path,
    live_trades: sqlite3.Connection | None,
) -> list[str]:
    """Return exact reasons a closure slot is not a strict business success."""
    errors: list[str] = []
    stage_path = Path(stage_status_dir) / (
        f"live-{str(cycle_id).replace(':', '-')}.json")
    try:
        stage = json.loads(stage_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stage = {}
        errors.append("live_stage_status_missing")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        stage = {}
        errors.append("live_stage_status_unreadable")
    if stage:
        if stage.get("stage") != "live" or stage.get("cycle_id") != cycle_id:
            errors.append("live_stage_identity_mismatch")
        if str(stage.get("status") or "").lower() != "succeeded":
            errors.append(
                f"live_stage_status={stage.get('status') or 'missing'}")
        if stage.get("returncode") != 0:
            errors.append("live_stage_returncode_nonzero")
        business = stage.get("business_check")
        if not isinstance(business, dict) or business.get("ok") is not True:
            errors.append("business_check_not_ok")
        else:
            terminal = business.get("business_terminal")
            if (
                not isinstance(terminal, dict)
                or terminal.get("cycle_id") != cycle_id
                or terminal.get("status") != "completed"
            ):
                errors.append("business_terminal_invalid")
        gate = stage.get("business_terminal_gate")
        if (
            not isinstance(gate, dict)
            or gate.get("status") != "met"
            or gate.get("strict_cycle_pass") is not True
        ):
            errors.append("business_terminal_gate_not_met")

    if live_trades is None:
        errors.append("live_trades_db_unavailable")
        return list(dict.fromkeys(errors))
    try:
        row = live_trades.execute(
            "SELECT decision,n_orders,raw FROM trade_cycles "
            "WHERE cycle_id=? LIMIT 1",
            (cycle_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
        errors.append("trade_cycle_query_failed")
    if row is None:
        errors.append("trade_cycle_missing")
        return list(dict.fromkeys(errors))
    decision = str(row["decision"] or "").strip().lower()
    try:
        n_orders = int(row["n_orders"])
    except (TypeError, ValueError):
        n_orders = -1
    if not (
        (decision == "traded" and n_orders > 0)
        or (decision in {"hold", "skip"} and n_orders == 0)
    ):
        errors.append("trade_cycle_terminal_invalid")
    receipt = _json_object(row["raw"])
    if (
        receipt.get("status") != "ok"
        or receipt.get("batch_status") != "completed"
        or receipt.get("batch_ok") is not True
    ):
        errors.append("trade_cycle_receipt_not_completed")
    terminal = receipt.get("business_terminal")
    if (
        not isinstance(terminal, dict)
        or terminal.get("cycle_id") != cycle_id
        or terminal.get("status") != "completed"
    ):
        errors.append("trade_cycle_business_terminal_invalid")
    return list(dict.fromkeys(errors))


def evaluate_zero_open_watchdog(
    analysis_db: Path,
    cycle_id: str,
    *,
    activation_cycle: str | None = None,
    threshold_slots: int | None = None,
    stage_status_dir: Path | None = None,
    live_trades_db: Path | None = None,
) -> dict:
    activation = activation_cycle or thresholds.zero_open_watchdog_activation_cycle(
        cycle_id)
    threshold = int(
        threshold_slots or thresholds.zero_open_watchdog_threshold_slots(
            cycle_id))
    current = _cycle(cycle_id)
    activation_dt = _cycle(activation)
    relaxed_policy = (
        thresholds.decision_restriction_removal_active(cycle_id)
        and activation == thresholds.zero_open_watchdog_activation_cycle(cycle_id)
    )
    closure_policy = thresholds.minimal_contract_closure_active(cycle_id)
    schema = SCHEMA_V2 if relaxed_policy else SCHEMA
    base = {
        "schema": schema,
        "cycle_id": cycle_id,
        "activation_cycle": activation,
        "threshold_successful_slots": threshold,
        "business_database_writes": 0,
        "writer_authority": False,
        "executor_authority": False,
        "dispatch_authority": False,
        "retry_authority": False,
        "scheduler_authority": False,
        "auto_order_authority": False,
        "policy_epoch": (
            thresholds.MINIMAL_CONTRACT_CLOSURE_POLICY
            if closure_policy
            else thresholds.MINIMAL_DECISION_CONTRACT_POLICY
            if thresholds.minimal_decision_contract_active(cycle_id)
            else thresholds.DECISION_RESTRICTION_REMOVAL_POLICY
            if relaxed_policy else "legacy_zero_open_v1"),
    }
    if current < activation_dt:
        inactive = {
            **base,
            "status": "INACTIVE",
            "successful_zero_open_slots": 0,
            "accepted_open_signals": 0,
            "source_consistent": None,
            "alert_required": False,
        }
        if closure_policy:
            inactive.update({
                "strict_business_terminal_required": True,
                "observed_natural_slots": 0,
                "strict_business_success_slots": 0,
                "failed_natural_slots": 0,
                "failure_details": [],
            })
        return inactive
    if threshold <= 0:
        raise ValueError("threshold_slots must be positive")

    connection = sqlite3.connect(
        f"file:{Path(analysis_db).resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    accepted: list[sqlite3.Row] = []
    observed_natural_slots = 0
    strict_business_success_slots = 0
    failed_natural_slots = 0
    failure_details: list[dict[str, Any]] = []
    source_consistent = True
    stop_reason = "activation_boundary"
    inconsistency = None
    observed_open_signals = 0
    live_trades: sqlite3.Connection | None = None
    if closure_policy:
        resolved_live_db = Path(
            live_trades_db or Path(analysis_db).with_name("live_trades.db"))
        try:
            live_trades = sqlite3.connect(
                f"file:{resolved_live_db.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=5,
            )
            live_trades.row_factory = sqlite3.Row
        except sqlite3.Error:
            live_trades = None
    try:
        offset = 0
        while True:
            expected_dt = current - timedelta(minutes=15 * offset)
            if expected_dt < activation_dt:
                break
            expected = expected_dt.strftime("%Y-%m-%dT%H:%M")
            observed_natural_slots += 1
            row = connection.execute(
                "SELECT cycle_id,status,mode,raw FROM analysis_runs "
                "WHERE cycle_id=?",
                (expected,),
            ).fetchone()
            if row is None:
                stop_reason = "missing_natural_slot"
                if closure_policy:
                    failed_natural_slots += 1
                    failure_details.append({
                        "cycle_id": expected,
                        "reasons": ["analysis_run_missing"],
                    })
                break
            table_open_count = int(connection.execute(
                "SELECT COUNT(*) FROM analysis_signals WHERE cycle_id=? "
                "AND lower(action) IN ('open_long','open_short')",
                (expected,),
            ).fetchone()[0])
            raw_open_count = _raw_open_count(row["raw"])
            if raw_open_count is None or raw_open_count != table_open_count:
                source_consistent = False
                stop_reason = "raw_table_open_count_mismatch"
                inconsistency = {
                    "cycle_id": expected,
                    "raw_open_count": raw_open_count,
                    "table_open_count": table_open_count,
                }
                if closure_policy:
                    failed_natural_slots += 1
                    failure_details.append({
                        "cycle_id": expected,
                        "reasons": ["analysis_signal_source_inconsistent"],
                    })
                break
            if str(row["status"] or "").lower() != "ok":
                stop_reason = "analysis_status_not_ok"
                if closure_policy:
                    failed_natural_slots += 1
                    failure_details.append({
                        "cycle_id": expected,
                        "reasons": ["analysis_status_not_ok"],
                    })
                break
            if str(row["mode"] or "").lower() != "full":
                stop_reason = "analysis_mode_not_full"
                if closure_policy:
                    failed_natural_slots += 1
                    failure_details.append({
                        "cycle_id": expected,
                        "reasons": ["analysis_mode_not_full"],
                    })
                break
            if closure_policy:
                strict_errors = _closure_business_terminal_errors(
                    expected,
                    stage_status_dir=Path(
                        stage_status_dir or DEFAULT_STAGE_STATUS_DIR),
                    live_trades=live_trades,
                )
                if strict_errors:
                    stop_reason = "strict_business_terminal_not_met"
                    failed_natural_slots += 1
                    failure_details.append({
                        "cycle_id": expected,
                        "reasons": strict_errors,
                    })
                    break
                strict_business_success_slots += 1
            if table_open_count:
                observed_open_signals = table_open_count
                stop_reason = "accepted_open_signal_observed"
                break
            accepted.append(row)
            offset += 1
    finally:
        connection.close()
        if live_trades is not None:
            live_trades.close()

    reason_counts: Counter[str] = Counter()
    veto_counts: Counter[str] = Counter()
    shadow_counterfactuals = 0
    for row in accepted:
        inner = _inner_raw(row["raw"])
        if relaxed_policy:
            entries = inner.get("candidates_deep_dived_v2")
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    veto = _json_object(entry.get("primary_disqualifier"))
                    code = str(
                        veto.get("kind") or entry.get("reason_code") or "").strip()
                    if code:
                        veto_counts[code] += 1
        else:
            coverage = _json_object(inner.get("candidate_coverage"))
            rejection_counts = coverage.get("rejection_reason_family_counts")
            if not isinstance(rejection_counts, dict):
                rejection_counts = coverage.get("reason_family_counts")
            for family, count in _json_object(rejection_counts).items():
                try:
                    reason_counts[str(family)] += int(count)
                except (TypeError, ValueError):
                    continue
        shadow = _json_object(inner.get("side_regime_soft_veto_shadow"))
        rows = shadow.get("counterfactuals")
        if isinstance(rows, list):
            shadow_counterfactuals += len(rows)

    streak = len(accepted)
    start_cycle = (
        accepted[-1]["cycle_id"] if accepted else None)
    if not source_consistent:
        status = "SOURCE_INCONSISTENT"
    elif streak >= threshold:
        status = "ALERT_CONDITION_OBSERVED"
    else:
        status = "BELOW_THRESHOLD"
    dedupe_key = (
        f"zero-open-watchdog:{'v2' if relaxed_policy else 'v1'}:"
        f"{activation}:{start_cycle}"
        if status == "ALERT_CONDITION_OBSERVED" and start_cycle else None)
    result = {
        **base,
        "status": status,
        "streak_start_cycle": start_cycle,
        "streak_end_cycle": cycle_id if accepted else None,
        "successful_zero_open_slots": streak,
        "accepted_open_signals": (
            observed_open_signals if source_consistent else None),
        "source_consistent": source_consistent,
        "stop_reason": stop_reason,
        "source_inconsistency": inconsistency,
        "reason_family_counts": dict(sorted(reason_counts.items())),
        "veto_code_counts": dict(sorted(veto_counts.items())),
        "soft_veto_shadow_counterfactual_count": shadow_counterfactuals,
        "counterfactual_semantics": (
            "reason distribution and continue-review shadow only; not would-open, "
            "not fill, and not profit"),
        "alert_required": status == "ALERT_CONDITION_OBSERVED",
        "alert_dedupe_key": dedupe_key,
    }
    if closure_policy:
        result.update({
            "strict_business_terminal_required": True,
            "observed_natural_slots": observed_natural_slots,
            "strict_business_success_slots": strict_business_success_slots,
            "failed_natural_slots": failed_natural_slots,
            "failure_details": failure_details,
        })
    return result


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_watchdog_artifact(
    result: dict,
    output_dir: Path = DEFAULT_ARTIFACT_DIR,
) -> Path:
    safe_cycle = str(result.get("cycle_id") or "unknown").replace(":", "-")
    path = Path(output_dir) / f"zero-open-watchdog-{safe_cycle}.json"
    _atomic_json(path, result)
    return path


def render_alert(result: dict) -> str:
    reasons = result.get("reason_family_counts") or {}
    top = sorted(reasons.items(), key=lambda item: (-int(item[1]), item[0]))[:5]
    top_text = ", ".join(f"{name}={count}" for name, count in top) or "无结构化理由"
    return (
        "⚠️ OKX 连续零OPEN观察条件达到 [P1]\n"
        f"· exact自然槽={result.get('successful_zero_open_slots')}/"
        f"{result.get('threshold_successful_slots')} "
        f"window={result.get('streak_start_cycle')}..{result.get('streak_end_cycle')}\n"
        f"· 候选拒绝理由Top: {top_text}\n"
        "· 本告警只要求人工复核；不下单、不补派、不重试、不改变风控或调度。\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--activation-cycle", default=None)
    parser.add_argument("--threshold-slots", type=int, default=None)
    parser.add_argument("--stage-status-dir", type=Path,
                        default=DEFAULT_STAGE_STATUS_DIR)
    parser.add_argument("--live-trades-db", type=Path,
                        default=DEFAULT_LIVE_TRADES_DB)
    parser.add_argument("--write-artifact", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    args = parser.parse_args()
    result = evaluate_zero_open_watchdog(
        args.analysis_db,
        args.cycle_id,
        activation_cycle=args.activation_cycle,
        threshold_slots=args.threshold_slots,
        stage_status_dir=args.stage_status_dir,
        live_trades_db=args.live_trades_db,
    )
    if args.write_artifact:
        result["artifact_path"] = str(publish_watchdog_artifact(
            result, args.output_dir))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result.get("status") == "SOURCE_INCONSISTENT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
