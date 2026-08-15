# -*- coding: utf-8 -*-
"""Fail-closed 15m/1H/4H readiness gate for live OPEN/ADD.

The gate reads ``market.db`` in SQLite read-only mode.  For the dispatched
Beijing-time cycle it requires the exact latest fully closed candle for every
decision timeframe, valid OHLCV, and the complete indicator set consumed by
the trading analysis.  Stale candles, partial/new-listing warm-up data, and
invalid values never count as ready.

This module has no exchange, order, or database-write capability.  CLOSE and
REDUCE paths deliberately do not call it.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


CST = timezone(timedelta(hours=8))
UTC = timezone.utc
TIMEFRAME_SECONDS = {"15m": 15 * 60, "1H": 60 * 60, "4H": 4 * 60 * 60}
RAW_FIELDS = ("o", "h", "l", "c", "v")
INDICATOR_FIELDS = ("ma5", "ma20", "atr14", "rsi14", "macd_hist")
MINIMUM_BARS_FOR_FULL_INDICATORS = 34
EVIDENCE_PROTOCOL = "multitimeframe_market_evidence_v1"
EVIDENCE_FIELDS = RAW_FIELDS + INDICATOR_FIELDS


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _field(row: Mapping[str, Any] | sqlite3.Row | None, name: str) -> Any:
    if row is None:
        return None
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return None


def validate_kline_row(
    row: Mapping[str, Any] | sqlite3.Row | None,
) -> dict[str, Any]:
    """Validate one exact candle without inventing missing fields."""
    if row is None:
        return {
            "raw_errors": ["missing_closed_bar"],
            "indicator_errors": [
                f"{field}_missing_or_non_finite" for field in INDICATOR_FIELDS
            ],
            "raw_valid": False,
            "indicators_valid": False,
            "ready": False,
        }

    raw_errors: list[str] = []
    raw_values: dict[str, float] = {}
    for field in RAW_FIELDS:
        value = _finite(_field(row, field))
        if value is None:
            raw_errors.append(f"{field}_missing_or_non_finite")
        else:
            raw_values[field] = value
    for field in ("o", "h", "l", "c"):
        if field in raw_values and raw_values[field] <= 0:
            raw_errors.append(f"{field}_not_positive")
    if "v" in raw_values and raw_values["v"] < 0:
        raw_errors.append("v_negative")
    if all(field in raw_values for field in ("o", "h", "l", "c")):
        if raw_values["h"] < max(
            raw_values["o"], raw_values["c"], raw_values["l"]
        ):
            raw_errors.append("high_cross_field_invalid")
        if raw_values["l"] > min(
            raw_values["o"], raw_values["c"], raw_values["h"]
        ):
            raw_errors.append("low_cross_field_invalid")

    indicator_errors: list[str] = []
    indicator_values: dict[str, float] = {}
    for field in INDICATOR_FIELDS:
        value = _finite(_field(row, field))
        if value is None:
            indicator_errors.append(f"{field}_missing_or_non_finite")
        else:
            indicator_values[field] = value
    for field in ("ma5", "ma20"):
        if field in indicator_values and indicator_values[field] <= 0:
            indicator_errors.append(f"{field}_not_positive")
    if "atr14" in indicator_values and indicator_values["atr14"] < 0:
        indicator_errors.append("atr14_negative")
    if (
        "rsi14" in indicator_values
        and not 0 <= indicator_values["rsi14"] <= 100
    ):
        indicator_errors.append("rsi14_out_of_range")

    return {
        "raw_errors": raw_errors,
        "indicator_errors": indicator_errors,
        "raw_valid": not raw_errors,
        "indicators_valid": not indicator_errors,
        "ready": not raw_errors and not indicator_errors,
    }


def parse_cycle_cst(cycle_id: str) -> datetime:
    """Parse a canonical 15-minute Beijing-time dispatch cycle."""
    try:
        parsed = datetime.strptime(str(cycle_id), "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "cycle_id must use canonical YYYY-MM-DDTHH:MM Beijing time"
        ) from exc
    if parsed.minute % 15 != 0:
        raise ValueError("cycle_id must be on a 15-minute boundary")
    return parsed.replace(tzinfo=CST)


def expected_closed_bar_start(cycle_cst: datetime, timeframe: str) -> str:
    """Exact UTC candle start that closed at or before ``cycle_cst``."""
    seconds = TIMEFRAME_SECONDS[timeframe]
    epoch = int(cycle_cst.astimezone(UTC).timestamp())
    start_epoch = (epoch // seconds) * seconds - seconds
    return datetime.fromtimestamp(start_epoch, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def seal_evidence_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return a canonical SHA-256 sealed evidence contract."""
    payload = dict(contract)
    payload.pop("evidence_hash", None)
    payload["evidence_hash"] = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return payload


def validate_evidence_contract(
    contract: Any,
    *,
    expected_symbol: str | None = None,
    expected_cycle: str | None = None,
) -> list[str]:
    """Self-validate exact point-in-time market evidence for an OPEN card."""
    if not isinstance(contract, dict):
        return ["evidence_contract must be a dict"]
    errors: list[str] = []
    if contract.get("protocol") != EVIDENCE_PROTOCOL:
        errors.append(f"protocol must be {EVIDENCE_PROTOCOL}")
    symbol = str(contract.get("symbol") or "")
    cycle_id = str(contract.get("cycle_id") or "")
    if not symbol:
        errors.append("symbol is required")
    elif expected_symbol is not None and symbol != str(expected_symbol):
        errors.append(
            f"symbol={symbol!r} differs from expected {expected_symbol!r}"
        )
    if not cycle_id:
        errors.append("cycle_id is required")
    elif expected_cycle is not None and cycle_id != str(expected_cycle):
        errors.append(
            f"cycle_id={cycle_id!r} differs from expected {expected_cycle!r}"
        )
    if contract.get("required_timeframes") != list(TIMEFRAME_SECONDS):
        errors.append("required_timeframes must be exactly 15m/1H/4H")
    if (
        contract.get("minimum_bars_for_full_indicators")
        != MINIMUM_BARS_FOR_FULL_INDICATORS
    ):
        errors.append(
            "minimum_bars_for_full_indicators must match the execution gate"
        )
    if contract.get("mode") != "read_only":
        errors.append("mode must be read_only")
    if contract.get("production_database_writes") != 0:
        errors.append("production_database_writes must be 0")
    if contract.get("orders_placed") != 0:
        errors.append("orders_placed must be 0")

    try:
        cycle_cst = parse_cycle_cst(cycle_id)
    except ValueError as exc:
        errors.append(f"cycle invalid: {exc}")
        cycle_cst = None

    timeframes = contract.get("timeframes")
    if not isinstance(timeframes, dict):
        errors.append("timeframes must be a dict")
        timeframes = {}
    elif set(timeframes) != set(TIMEFRAME_SECONDS):
        errors.append("timeframes must contain exactly 15m/1H/4H")
    for timeframe in TIMEFRAME_SECONDS:
        row = timeframes.get(timeframe)
        if not isinstance(row, dict):
            errors.append(f"timeframes.{timeframe} must be a dict")
            continue
        expected_ts = (
            expected_closed_bar_start(cycle_cst, timeframe)
            if cycle_cst is not None else None
        )
        if expected_ts is not None and row.get(
            "expected_closed_bar_ts"
        ) != expected_ts:
            errors.append(
                f"timeframes.{timeframe}.expected_closed_bar_ts mismatch"
            )
        if row.get("observed_bar_ts") != row.get("expected_closed_bar_ts"):
            errors.append(f"timeframes.{timeframe} exact closed bar missing")
        bars_seen = row.get("bars_seen")
        if (
            isinstance(bars_seen, bool)
            or not isinstance(bars_seen, int)
            or bars_seen < MINIMUM_BARS_FOR_FULL_INDICATORS
        ):
            errors.append(f"timeframes.{timeframe}.bars_seen is insufficient")
        if row.get("ready") is not True:
            errors.append(f"timeframes.{timeframe}.ready must be true")
        values = row.get("values")
        if not isinstance(values, dict) or set(values) != set(EVIDENCE_FIELDS):
            errors.append(
                f"timeframes.{timeframe}.values must contain exact OHLCV+indicator fields"
            )
            continue
        reconstructed = {"ts": row.get("observed_bar_ts"), **values}
        validation = validate_kline_row(reconstructed)
        if not validation["ready"]:
            errors.append(
                f"timeframes.{timeframe}.values are invalid: "
                + ",".join(
                    validation["raw_errors"] + validation["indicator_errors"]
                )
            )

    supplied_hash = contract.get("evidence_hash")
    core = dict(contract)
    core.pop("evidence_hash", None)
    try:
        expected_hash = hashlib.sha256(
            _canonical_json(core).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError) as exc:
        errors.append(f"contract is not canonical JSON: {exc}")
    else:
        if supplied_hash != expected_hash:
            errors.append("evidence_hash mismatch")
    return errors


def load_persisted_analysis_evidence(
    db_root: str | Path,
    symbol: str,
    cycle_id: str,
) -> dict[str, Any]:
    """Load the writer-validated OPEN evidence anchor without writing.

    ``analyst_writer`` compares this evidence contract with ``market.db``
    immediately before committing the signal.  A later collection can revise
    the same already-closed official candle and therefore recompute indicators.
    The persisted signal is the immutable hand-off authority for that race; it
    is never a substitute for the current readiness check.
    """
    result: dict[str, Any] = {
        "status": "NOT_FOUND",
        "mode": "read_only",
        "symbol": str(symbol),
        "cycle_id": str(cycle_id),
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    analysis_db = Path(db_root) / "analysis.db"
    if not analysis_db.is_file():
        result["error"] = "analysis_db_missing"
        return result
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{analysis_db.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT action,side,decision_card FROM analysis_signals "
            "WHERE cycle_id=? AND symbol=? LIMIT 1",
            (str(cycle_id), str(symbol)),
        ).fetchone()
    except sqlite3.Error as exc:
        result["status"] = "UNREADABLE"
        result["error"] = (
            f"analysis_db_unreadable:{type(exc).__name__}:{exc}"
        )
        return result
    finally:
        if connection is not None:
            connection.close()
    if row is None:
        return result
    result["action"] = str(row["action"] or "").strip().lower()
    result["side"] = str(row["side"] or "").strip().lower()
    if result["action"] not in {"open_long", "open_short"}:
        result["status"] = "INVALID"
        result["error"] = "persisted_signal_not_open"
        return result
    expected_side = "long" if result["action"] == "open_long" else "short"
    if result["side"] != expected_side:
        result["status"] = "INVALID"
        result["error"] = "persisted_signal_side_mismatch"
        return result
    try:
        card = json.loads(row["decision_card"] or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        result["status"] = "INVALID"
        result["error"] = "persisted_decision_card_invalid_json"
        return result
    block = card.get("multitimeframe_analysis") \
        if isinstance(card, dict) else None
    contract = block.get("evidence_contract") \
        if isinstance(block, dict) else None
    errors = validate_evidence_contract(
        contract,
        expected_symbol=str(symbol),
        expected_cycle=str(cycle_id),
    )
    if errors:
        result["status"] = "INVALID"
        result["error"] = "persisted_evidence_invalid"
        result["validation_errors"] = errors
        return result
    result["status"] = "VALID"
    result["evidence_contract"] = contract
    result["evidence_hash"] = contract.get("evidence_hash")
    return result


def resolve_execution_evidence_anchor(
    db_root: str | Path,
    symbol: str,
    cycle_id: str,
    expected_side: str,
    supplied_contract: Any,
    current_contract: Any,
) -> dict[str, Any]:
    """Resolve exact-current or post-analysis-revision evidence safely.

    The normal path remains an exact supplied/current match.  A mismatch is
    accepted only when the supplied contract exactly matches the independently
    persisted, writer-validated signal for the same cycle and symbol.  Callers
    must still prove the current three timeframes are ready before invoking
    this helper.
    """
    supplied_errors = validate_evidence_contract(
        supplied_contract,
        expected_symbol=str(symbol),
        expected_cycle=str(cycle_id),
    )
    current_errors = validate_evidence_contract(
        current_contract,
        expected_symbol=str(symbol),
        expected_cycle=str(cycle_id),
    )
    result: dict[str, Any] = {
        "ok": False,
        "mode": "read_only",
        "symbol": str(symbol),
        "cycle_id": str(cycle_id),
        "expected_side": str(expected_side).strip().lower(),
        "supplied_evidence_hash": (
            supplied_contract.get("evidence_hash")
            if isinstance(supplied_contract, dict) else None
        ),
        "current_evidence_hash": (
            current_contract.get("evidence_hash")
            if isinstance(current_contract, dict) else None
        ),
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    if supplied_errors or current_errors:
        result["reason"] = "invalid_evidence_contract"
        result["supplied_validation_errors"] = supplied_errors
        result["current_validation_errors"] = current_errors
        return result
    if supplied_contract == current_contract:
        result.update({
            "ok": True,
            "evidence_anchor": "current_market_exact",
            "post_analysis_market_revision": False,
        })
        return result
    persisted = load_persisted_analysis_evidence(
        db_root, symbol, cycle_id)
    result["persisted_status"] = persisted.get("status")
    result["persisted_evidence_hash"] = persisted.get("evidence_hash")
    if (
        persisted.get("status") == "VALID"
        and persisted.get("side") == str(expected_side).strip().lower()
        and supplied_contract == persisted.get("evidence_contract")
    ):
        result.update({
            "ok": True,
            "evidence_anchor": "analysis_db_writer_validated",
            "post_analysis_market_revision": True,
        })
        return result
    result["reason"] = "supplied_evidence_not_persisted_anchor"
    result["persisted_side"] = persisted.get("side")
    if persisted.get("error"):
        result["persisted_error"] = persisted.get("error")
    return result


def check_multitimeframe_readiness(
    db_root: str | Path,
    symbol: str,
    cycle_id: str,
) -> dict[str, Any]:
    """Return auditable readiness evidence; never writes any database."""
    result: dict[str, Any] = {
        "contract_version": 1,
        "mode": "read_only",
        "symbol": str(symbol),
        "cycle_id": str(cycle_id),
        "required_timeframes": list(TIMEFRAME_SECONDS),
        "minimum_bars_for_full_indicators": (
            MINIMUM_BARS_FOR_FULL_INDICATORS
        ),
        "timeframes": [],
        "ready": False,
        "status": "NOT_READY",
        "reject_reason": "multitimeframe_data_not_ready",
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    try:
        cycle_cst = parse_cycle_cst(cycle_id)
    except ValueError as exc:
        result["error"] = f"cycle_invalid:{exc}"
        return result
    result["evaluation_at_cst"] = cycle_cst.isoformat()

    market_db = Path(db_root) / "market.db"
    if not market_db.is_file():
        result["error"] = "market_db_missing"
        return result

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{market_db.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        for timeframe in TIMEFRAME_SECONDS:
            expected_ts = expected_closed_bar_start(cycle_cst, timeframe)
            row = connection.execute(
                "SELECT ts,o,h,l,c,v,ma5,ma20,atr14,rsi14,macd_hist "
                "FROM kline_cache WHERE symbol=? AND tf=? AND ts=? "
                "LIMIT 1",
                (symbol, timeframe, expected_ts),
            ).fetchone()
            bars_seen = int(
                connection.execute(
                    "SELECT COUNT(*) FROM kline_cache "
                    "WHERE symbol=? AND tf=? AND ts<=?",
                    (symbol, timeframe, expected_ts),
                ).fetchone()[0]
            )
            validation = validate_kline_row(row)
            if validation["raw_errors"]:
                classification = "source_data_invalid"
            elif bars_seen < MINIMUM_BARS_FOR_FULL_INDICATORS:
                classification = "insufficient_history"
            elif validation["indicator_errors"]:
                classification = "indicator_invalid"
            else:
                classification = "ready"
            timeframe_ready = (
                validation["ready"]
                and bars_seen >= MINIMUM_BARS_FOR_FULL_INDICATORS
            )
            result["timeframes"].append(
                {
                    "timeframe": timeframe,
                    "expected_closed_bar_ts": expected_ts,
                    "observed_bar_ts": _field(row, "ts"),
                    "bars_seen": bars_seen,
                    "classification": classification,
                    **validation,
                    "ready": timeframe_ready,
                    "values": {
                        field: _finite(_field(row, field))
                        for field in EVIDENCE_FIELDS
                    },
                }
            )
    except sqlite3.Error as exc:
        result["error"] = f"market_db_unreadable:{type(exc).__name__}:{exc}"
        return result
    finally:
        if connection is not None:
            connection.close()

    result["ready"] = (
        len(result["timeframes"]) == len(TIMEFRAME_SECONDS)
        and all(row["ready"] for row in result["timeframes"])
    )
    result["status"] = "PASSED" if result["ready"] else "NOT_READY"
    result["reject_reason"] = (
        None if result["ready"] else "multitimeframe_data_not_ready"
    )
    result["evidence_contract"] = seal_evidence_contract({
        "protocol": EVIDENCE_PROTOCOL,
        "mode": "read_only",
        "symbol": str(symbol),
        "cycle_id": str(cycle_id),
        "required_timeframes": list(TIMEFRAME_SECONDS),
        "minimum_bars_for_full_indicators": (
            MINIMUM_BARS_FOR_FULL_INDICATORS
        ),
        "timeframes": {
            row["timeframe"]: {
                "expected_closed_bar_ts": row["expected_closed_bar_ts"],
                "observed_bar_ts": row["observed_bar_ts"],
                "bars_seen": row["bars_seen"],
                "ready": row["ready"],
                "values": row["values"],
            }
            for row in result["timeframes"]
        },
        "production_database_writes": 0,
        "orders_placed": 0,
    })
    return result
