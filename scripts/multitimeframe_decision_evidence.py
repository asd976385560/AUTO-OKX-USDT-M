# -*- coding: utf-8 -*-
"""Build read-only 15m/1H/4H evidence for OPEN or position-exit review.

Single-symbol mode preserves the OPEN evidence contract.  ``--facts-file``
adds a batch position-review mode: one local call reads every current position,
its original open plan when uniquely attributable, observed peak UPL, and exact
closed 15m/1H/4H evidence.  Review flags are attention prompts only; this tool
never decides, submits, or constrains a close/reduce/protection action.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from core.multitimeframe_gate import check_multitimeframe_readiness
from scripts.live_decision_facts import validate_facts


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
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


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("facts file must contain one JSON object")
    return payload


def _gaps(result: dict) -> list[dict]:
    return [
        {
            "timeframe": row.get("timeframe"),
            "classification": row.get("classification"),
            "raw_errors": row.get("raw_errors"),
            "indicator_errors": row.get("indicator_errors"),
        }
        for row in result.get("timeframes", [])
        if not row.get("ready")
    ]


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _open_plan_context(
    connection: sqlite3.Connection | None,
    position: dict,
) -> dict:
    symbol = str(position.get("instId") or "")
    side = str(position.get("posSide") or "").lower()
    base_qty = _number(position.get("base_qty"))
    current_contracts = _number(position.get("contracts"))
    mark_px = _number(position.get("markPx"))
    current_upl = _number(position.get("upl"))
    context: dict[str, Any] = {
        "status": "unavailable",
        "source": "account.db.trade_experiences+position_snapshots",
        "symbol": symbol,
        "side": side,
        "review_flags": [],
        "review_flags_are_non_binding": True,
        "automatic_exit_authorized": False,
    }
    if connection is None:
        context["reason"] = "account_db_unavailable"
        return context
    try:
        rows = connection.execute(
            "SELECT id,cycle_id,ts,open_sz,remaining_sz,raw "
            "FROM trade_experiences WHERE profile='live' AND status='open' "
            "AND symbol=? AND lower(side)=? AND COALESCE(remaining_sz,0)>0 "
            "ORDER BY ts,id",
            (symbol, side),
        ).fetchall()
    except sqlite3.Error as exc:
        context["reason"] = f"open_plan_query_failed:{type(exc).__name__}"
        return context
    context["open_leg_count"] = len(rows)
    if len(rows) != 1:
        context["reason"] = (
            "open_plan_missing" if not rows else "multiple_open_legs_not_aggregated"
        )
        return context

    row = rows[0]
    raw = _json_object(row["raw"])
    card = _json_object(raw.get("decision_card"))
    risk_reward = _json_object(card.get("risk_reward"))
    fill_px = _number(raw.get("fill_px"))
    initial_sl = _number(raw.get("sl_trigger_px"))
    target = _number(risk_reward.get("target"))
    exit_mode = str(risk_reward.get("exit_mode") or "").strip().lower()
    if not exit_mode:
        exit_mode = "legacy_unspecified"
    open_sz = _number(row["open_sz"])
    remaining_sz = _number(row["remaining_sz"])
    size_comparable = bool(
        open_sz is not None and remaining_sz is not None
        and current_contracts is not None
        and abs(open_sz - remaining_sz) <= 1e-8
        and abs(remaining_sz - current_contracts) <= 1e-8
    )
    initial_risk = None
    if (
        fill_px is not None and initial_sl is not None
        and base_qty is not None and size_comparable
    ):
        initial_risk = abs(fill_px - initial_sl) * base_qty
        if initial_risk <= 0:
            initial_risk = None
    current_r = (
        current_upl / initial_risk
        if current_upl is not None and initial_risk else None
    )
    protected_pnl = _number(position.get("pnl_at_stop_from_entry_usdt"))
    protected_r = (
        protected_pnl / initial_risk
        if protected_pnl is not None and initial_risk else None
    )
    target_reached = None
    if target is not None and mark_px is not None and side in {"long", "short"}:
        target_reached = mark_px >= target if side == "long" else mark_px <= target

    peak_ts = None
    peak_upl = None
    if size_comparable:
        try:
            peak = connection.execute(
                "SELECT ts,upl FROM position_snapshots "
                "WHERE profile='live' AND symbol=? AND ts>=? "
                "ORDER BY upl DESC,rowid DESC LIMIT 1",
                (symbol, str(row["ts"])),
            ).fetchone()
            if peak is not None:
                peak_ts = str(peak["ts"])
                peak_upl = _number(peak["upl"])
        except sqlite3.Error:
            peak_ts = None
            peak_upl = None
    giveback_usdt = None
    giveback_pct = None
    peak_r = None
    if peak_upl is not None and current_upl is not None and peak_upl > 0:
        giveback_usdt = max(0.0, peak_upl - current_upl)
        giveback_pct = giveback_usdt / peak_upl * 100.0
    if peak_upl is not None and initial_risk:
        peak_r = peak_upl / initial_risk

    flags: list[str] = []
    if position.get("margin_return_review_at_or_above_50pct") is True:
        flags.append("official_margin_return_at_or_above_50pct")
    if target_reached is True:
        flags.append("original_target_reached")
    if current_r is not None and current_r >= 2:
        flags.append("current_profit_at_or_above_2r")
    elif current_r is not None and current_r >= 1:
        flags.append("current_profit_at_or_above_1r")
    if (
        peak_r is not None and peak_r >= 1
        and giveback_pct is not None and giveback_pct >= 25
    ):
        flags.append(
            "observed_peak_at_or_above_1r_and_giveback_at_or_above_25pct")
    if current_r is not None and current_r >= 1 and (
        protected_r is None or protected_r <= 0
    ):
        flags.append("profit_at_or_above_1r_not_locked_by_current_sl")
    if exit_mode == "legacy_unspecified":
        flags.append("legacy_exit_mode_requires_fresh_agent_choice")

    context.update({
        "status": "unique_open_leg",
        "experience_id": int(row["id"]),
        "open_cycle_id": str(row["cycle_id"]),
        "opened_at": str(row["ts"]),
        "fill_px": _rounded(fill_px, 10),
        "initial_sl": _rounded(initial_sl, 10),
        "original_target": _rounded(target, 10),
        "original_exit_mode": exit_mode,
        "target_semantics": (
            "explicit_exit_mode" if exit_mode != "legacy_unspecified"
            else "legacy_target_not_known_to_be_exchange_tp"
        ),
        "target_reached": target_reached,
        "open_sz": _rounded(open_sz, 10),
        "remaining_sz": _rounded(remaining_sz, 10),
        "current_contracts": _rounded(current_contracts, 10),
        "path_size_comparable": size_comparable,
        "initial_risk_usdt": _rounded(initial_risk),
        "current_r_gross": _rounded(current_r),
        "protected_r_at_current_sl_gross": _rounded(protected_r),
        "observed_peak_upl_usdt": _rounded(peak_upl),
        "observed_peak_upl_at": peak_ts,
        "observed_peak_r_gross": _rounded(peak_r),
        "giveback_from_observed_peak_usdt": _rounded(giveback_usdt),
        "giveback_from_observed_peak_pct": _rounded(giveback_pct),
        "review_flags": flags,
    })
    return context


def build_position_exit_batch(
    db_root: Path,
    facts: dict,
    cycle_id: str,
) -> dict:
    validation_errors = validate_facts(
        facts, expected_cycle=cycle_id, expected_profile="live")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "mode": "read_only",
        "scope": "all_current_position_exit_review",
        "cycle_id": cycle_id,
        "facts_hash": facts.get("facts_hash"),
        "facts_validation_errors": validation_errors,
        "positions": [],
        "review_policy": {
            "evidence_only": True,
            "review_flags_are_non_binding": True,
            "automatic_exit_authorized": False,
            "agent_retains_choice": [
                "hold", "close", "reduce", "adjust_protection",
            ],
        },
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    if validation_errors:
        payload.update({"ok": False, "status": "FACTS_INVALID"})
        payload["evidence_hash"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        return payload

    account_db = db_root / "account.db"
    connection: sqlite3.Connection | None = None
    if account_db.is_file():
        try:
            connection = sqlite3.connect(
                f"file:{account_db.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=5,
            )
            connection.row_factory = sqlite3.Row
        except sqlite3.Error:
            connection = None
    try:
        for raw_position in facts.get("positions") or []:
            if not isinstance(raw_position, dict):
                continue
            symbol = str(raw_position.get("instId") or "")
            mtf = check_multitimeframe_readiness(db_root, symbol, cycle_id)
            payload["positions"].append({
                "symbol": symbol,
                "side": raw_position.get("posSide"),
                "current": {
                    key: raw_position.get(key)
                    for key in (
                        "avgPx", "markPx", "position_age_hours", "upl",
                        "upl_pct_initial_margin",
                        "signed_price_return_pct_from_entry",
                        "margin_return_review_at_or_above_50pct",
                        "pnl_at_stop_from_entry_usdt",
                        "secured_profit_at_stop_usdt",
                        "profit_retention_at_stop_pct_of_current_upl",
                        "giveback_to_stop_pct_of_current_upl",
                    )
                },
                "open_plan_and_path": _open_plan_context(
                    connection, raw_position),
                "multitimeframe_ready": bool(mtf.get("ready")),
                "multitimeframe_status": mtf.get("status"),
                "evidence_contract": mtf.get("evidence_contract"),
                "gaps": _gaps(mtf),
                "error": mtf.get("error"),
            })
    finally:
        if connection is not None:
            connection.close()
    all_ready = all(
        row.get("multitimeframe_ready") is True
        for row in payload["positions"]
    )
    payload["ok"] = all_ready
    payload["status"] = "PASSED" if all_ready else "PARTIAL_MTF"
    payload["position_count"] = len(payload["positions"])
    payload["multitimeframe_ready_count"] = sum(
        1 for row in payload["positions"]
        if row.get("multitimeframe_ready") is True)
    payload["evidence_hash"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-root", type=Path, default=Path(r".\db"))
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--symbol")
    scope.add_argument("--facts-file", type=Path)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--out-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.facts_file is not None:
        try:
            payload = build_position_exit_batch(
                args.db_root, _load_json(args.facts_file), args.cycle_id)
        except Exception as exc:  # noqa: BLE001
            payload = {
                "ok": False,
                "status": "ERROR",
                "mode": "read_only",
                "scope": "all_current_position_exit_review",
                "cycle_id": args.cycle_id,
                "error": f"{type(exc).__name__}:{exc}",
                "production_database_writes": 0,
                "orders_placed": 0,
            }
        _atomic_json(args.out_file, payload)
        print(json.dumps({
            "ok": bool(payload.get("ok")),
            "status": payload.get("status"),
            "scope": payload.get("scope"),
            "cycle_id": args.cycle_id,
            "position_count": payload.get("position_count", 0),
            "multitimeframe_ready_count": payload.get(
                "multitimeframe_ready_count", 0),
            "evidence_hash": payload.get("evidence_hash"),
            "out_file": str(args.out_file),
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 0 if payload.get("ok") else 2
    result = check_multitimeframe_readiness(
        args.db_root, args.symbol, args.cycle_id)
    payload = {
        "ok": bool(result.get("ready")),
        "status": result.get("status"),
        "symbol": args.symbol,
        "cycle_id": args.cycle_id,
        "evidence_contract": result.get("evidence_contract"),
        "gaps": _gaps(result),
        "error": result.get("error"),
        "mode": "read_only",
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    _atomic_json(args.out_file, payload)
    print(json.dumps({
        "ok": payload["ok"],
        "status": payload["status"],
        "symbol": args.symbol,
        "cycle_id": args.cycle_id,
        "evidence_hash": (
            (payload.get("evidence_contract") or {}).get("evidence_hash")
        ),
        "out_file": str(args.out_file),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
