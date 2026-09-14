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
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
CLOSED_BAR_PROOF_METHOD = "immediate_successor_bar_present"
WS_CONFIRMED_BAR_PROOF_METHOD = "ws_confirmed_candle_exact_match"
WS_CACHE_REQUIRED_COLUMNS = {
    "inst_id",
    "timeframe",
    "ts_ms",
    "open",
    "high",
    "low",
    "close",
    "volume_quote",
    "confirm",
    "bar_end_ms",
    "received_at",
    "conn_epoch",
    "source",
}


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


def immediate_successor_bar_start(bar_start: str, timeframe: str) -> str:
    """Return the exact next candle start used to prove the prior bar closed.

    ``kline_cache`` predates OKX's ``confirm`` field and therefore cannot prove
    closure from the expected row alone: a row fetched at its opening time may
    still be forming.  The exact immediate successor can only be present after
    a later batch observed the timeframe boundary; that same atomic batch also
    refreshes the preceding row.  Missing proof fails closed without rewriting
    historical cache rows or requiring a database migration.
    """
    try:
        parsed = datetime.fromisoformat(
            str(bar_start).replace("Z", "+00:00")
        ).astimezone(UTC)
        seconds = TIMEFRAME_SECONDS[timeframe]
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("invalid bar_start or timeframe") from exc
    return (parsed + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _iso_utc_to_ms(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid UTC timestamp") from exc
    return int(parsed.timestamp() * 1000)


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _effective_source_mode(config: Mapping[str, Any]) -> str:
    valid_modes = {"shadow", "dual_read", "ws_first", "rest_only"}
    configured = str(config.get("mode") or "rest_only")
    if configured not in valid_modes:
        return "rest_only"
    pending = str(config.get("pending_mode") or "")
    boundary = config.get("activation_boundary")
    if pending not in valid_modes or not boundary:
        return configured
    try:
        parsed = datetime.fromisoformat(
            str(boundary).replace("Z", "+00:00")
        )
    except ValueError:
        return "rest_only"
    if parsed.tzinfo is None:
        return "rest_only"
    return pending if datetime.now(UTC) >= parsed.astimezone(UTC) else configured


def _source_config_path(db_root: Path) -> Path:
    explicit = os.environ.get("OKX_MARKET_SOURCE_CONFIG")
    if explicit:
        return Path(explicit)
    local = db_root / "ws_market_source.json"
    if local.is_file():
        return local
    project_root = Path(os.environ.get("OKX_ROOT") or db_root.parent)
    return project_root / "config" / "ws_market_source.json"


def open_ws_confirmation_connection(
    db_root: str | Path,
) -> tuple[sqlite3.Connection | None, dict[str, Any]]:
    """Open the immutable WS candle proof store only for active ws_first mode.

    Missing, malformed, or non-ws_first configuration returns no connection and
    leaves the legacy successor proof authoritative.  The helper never creates
    a database and never falls back to a writable SQLite connection.
    """
    root = Path(db_root)
    metadata: dict[str, Any] = {
        "source_mode": "rest_only",
        "available": False,
        "cache_db": "ws_market_cache.db",
    }
    config_path = _source_config_path(root)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        metadata["reason"] = f"source_config_unavailable:{type(exc).__name__}"
        return None, metadata
    if not isinstance(config, dict):
        metadata["reason"] = "source_config_not_object"
        return None, metadata
    mode = _effective_source_mode(config)
    metadata["source_mode"] = mode
    if mode != "ws_first":
        metadata["reason"] = f"source_mode_{mode}"
        return None, metadata

    configured_cache = config.get("cache_db")
    cache_path = Path(
        os.environ.get("OKX_WS_CACHE_DB")
        or str(configured_cache or (root / "ws_market_cache.db"))
    )
    if not cache_path.is_absolute():
        cache_path = config_path.parent.parent / cache_path
    metadata["cache_db"] = cache_path.name
    if not cache_path.is_file():
        metadata["reason"] = "ws_cache_missing"
        return None, metadata
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{cache_path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candles'"
        ).fetchone()
        if table is None:
            raise sqlite3.DatabaseError("candles table missing")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(candles)")
        }
        missing = sorted(WS_CACHE_REQUIRED_COLUMNS - columns)
        if missing:
            raise sqlite3.DatabaseError(
                "candles columns missing:" + ",".join(missing)
            )
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        metadata["reason"] = f"ws_cache_unreadable:{type(exc).__name__}"
        return None, metadata
    metadata["available"] = True
    metadata["reason"] = "ok"
    return connection, metadata


def _ws_confirmed_candle_proof(
    connection: sqlite3.Connection | None,
    context: Mapping[str, Any],
    *,
    symbol: str,
    timeframe: str,
    expected_ts: str,
    expected_successor_ts: str,
    market_row: Mapping[str, Any] | sqlite3.Row | None,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "source_mode": str(context.get("source_mode") or "rest_only"),
        "cache_db": str(context.get("cache_db") or "ws_market_cache.db"),
        "expected_candle_ts": expected_ts,
        "observed_candle_ts": None,
        "timeframe": timeframe,
        "confirm": None,
        "source": None,
        "expected_bar_end_ms": _iso_utc_to_ms(expected_successor_ts),
        "observed_bar_end_ms": None,
        "market_row_match": False,
        "compared_fields": list(RAW_FIELDS),
        "mismatched_fields": [],
        "received_at": None,
        "conn_epoch_present": False,
        "proven": False,
        "reason": str(context.get("reason") or "ws_confirmation_unavailable"),
    }
    if connection is None or context.get("available") is not True:
        return proof
    expected_ms = _iso_utc_to_ms(expected_ts)
    try:
        row = connection.execute(
            "SELECT ts_ms,open,high,low,close,volume_quote,confirm,"
            "bar_end_ms,received_at,conn_epoch,source FROM candles "
            "WHERE inst_id=? AND timeframe=? AND ts_ms=? LIMIT 1",
            (str(symbol), timeframe, expected_ms),
        ).fetchone()
    except sqlite3.Error as exc:
        proof["reason"] = f"ws_confirmation_query_failed:{type(exc).__name__}"
        return proof
    if row is None:
        proof["reason"] = "ws_confirmed_candle_missing"
        return proof

    observed_ms = int(row["ts_ms"])
    proof.update({
        "observed_candle_ts": datetime.fromtimestamp(
            observed_ms / 1000, tz=UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "confirm": int(row["confirm"]),
        "source": str(row["source"] or ""),
        "observed_bar_end_ms": int(row["bar_end_ms"]),
        "received_at": str(row["received_at"] or ""),
        "conn_epoch_present": bool(str(row["conn_epoch"] or "").strip()),
    })
    mapping = {
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume_quote",
    }
    mismatches: list[str] = []
    for market_field, ws_field in mapping.items():
        market_value = _decimal(_field(market_row, market_field))
        ws_value = _decimal(row[ws_field])
        if market_value is None or ws_value is None or market_value != ws_value:
            mismatches.append(market_field)
    proof["mismatched_fields"] = mismatches
    proof["market_row_match"] = not mismatches and market_row is not None
    proof["proven"] = bool(
        proof["source_mode"] == "ws_first"
        and proof["observed_candle_ts"] == expected_ts
        and proof["confirm"] == 1
        and proof["source"] == "ws"
        and proof["observed_bar_end_ms"] == proof["expected_bar_end_ms"]
        and proof["received_at"]
        and proof["conn_epoch_present"]
        and proof["market_row_match"]
    )
    if proof["proven"]:
        proof["reason"] = "ok"
    elif mismatches:
        proof["reason"] = "market_ws_ohlcv_mismatch"
    elif proof["source"] != "ws":
        proof["reason"] = "ws_candle_source_not_raw_ws"
    elif proof["confirm"] != 1:
        proof["reason"] = "ws_candle_not_confirmed"
    elif proof["observed_bar_end_ms"] != proof["expected_bar_end_ms"]:
        proof["reason"] = "ws_candle_bar_end_mismatch"
    elif not proof["received_at"] or not proof["conn_epoch_present"]:
        proof["reason"] = "ws_candle_provenance_incomplete"
    else:
        proof["reason"] = "ws_confirmation_invalid"
    return proof


def build_closed_bar_proof(
    *,
    observed_successor_ts: str | None,
    expected_successor_ts: str,
    ws_connection: sqlite3.Connection | None,
    ws_context: Mapping[str, Any],
    symbol: str,
    timeframe: str,
    expected_ts: str,
    market_row: Mapping[str, Any] | sqlite3.Row | None,
) -> dict[str, Any]:
    if observed_successor_ts == expected_successor_ts:
        return {
            "method": CLOSED_BAR_PROOF_METHOD,
            "expected_successor_bar_ts": expected_successor_ts,
            "observed_successor_bar_ts": observed_successor_ts,
            "proven": True,
        }
    ws_confirmation = _ws_confirmed_candle_proof(
        ws_connection,
        ws_context,
        symbol=symbol,
        timeframe=timeframe,
        expected_ts=expected_ts,
        expected_successor_ts=expected_successor_ts,
        market_row=market_row,
    )
    if ws_confirmation["proven"]:
        return {
            "method": WS_CONFIRMED_BAR_PROOF_METHOD,
            "expected_successor_bar_ts": expected_successor_ts,
            "observed_successor_bar_ts": None,
            "ws_confirmation": ws_confirmation,
            "proven": True,
        }
    proof = {
        "method": CLOSED_BAR_PROOF_METHOD,
        "expected_successor_bar_ts": expected_successor_ts,
        "observed_successor_bar_ts": observed_successor_ts,
        "proven": False,
    }
    if ws_context.get("source_mode") == "ws_first":
        proof["ws_confirmation"] = ws_confirmation
    return proof


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
        # Legacy v1 contracts did not carry a closure proof.  They remain
        # structurally readable for historical evidence, while every contract
        # generated by current code includes this sealed block and must prove
        # that the immediate successor bar was observed.
        proof = row.get("closed_bar_proof")
        if proof is not None:
            if not isinstance(proof, dict):
                errors.append(
                    f"timeframes.{timeframe}.closed_bar_proof must be a dict"
                )
            else:
                expected_successor = (
                    immediate_successor_bar_start(expected_ts, timeframe)
                    if expected_ts is not None else None
                )
                method = proof.get("method")
                if proof.get("expected_successor_bar_ts") != expected_successor:
                    errors.append(
                        f"timeframes.{timeframe}.closed_bar_proof expected successor mismatch"
                    )
                if method == CLOSED_BAR_PROOF_METHOD:
                    if proof.get("observed_successor_bar_ts") != expected_successor:
                        errors.append(
                            f"timeframes.{timeframe}.closed_bar_proof successor missing"
                        )
                elif method == WS_CONFIRMED_BAR_PROOF_METHOD:
                    if proof.get("observed_successor_bar_ts") is not None:
                        errors.append(
                            f"timeframes.{timeframe}.closed_bar_proof successor must be absent for ws proof"
                        )
                    confirmation = proof.get("ws_confirmation")
                    prefix = f"timeframes.{timeframe}.closed_bar_proof.ws_confirmation"
                    if not isinstance(confirmation, dict):
                        errors.append(f"{prefix} must be a dict")
                    else:
                        expected_bar_end_ms = (
                            _iso_utc_to_ms(expected_successor)
                            if expected_successor is not None else None
                        )
                        expected_confirmation = {
                            "source_mode": "ws_first",
                            "expected_candle_ts": expected_ts,
                            "observed_candle_ts": expected_ts,
                            "timeframe": timeframe,
                            "confirm": 1,
                            "source": "ws",
                            "expected_bar_end_ms": expected_bar_end_ms,
                            "observed_bar_end_ms": expected_bar_end_ms,
                            "market_row_match": True,
                            "compared_fields": list(RAW_FIELDS),
                            "mismatched_fields": [],
                            "conn_epoch_present": True,
                            "proven": True,
                            "reason": "ok",
                        }
                        for field, expected_value in expected_confirmation.items():
                            if confirmation.get(field) != expected_value:
                                errors.append(f"{prefix}.{field} invalid")
                        if not str(confirmation.get("received_at") or "").strip():
                            errors.append(f"{prefix}.received_at is required")
                else:
                    errors.append(
                        f"timeframes.{timeframe}.closed_bar_proof.method invalid"
                    )
                if proof.get("proven") is not True:
                    errors.append(
                        f"timeframes.{timeframe}.closed_bar_proof must be proven"
                    )
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
    ws_connection: sqlite3.Connection | None = None
    ws_context: dict[str, Any] = {
        "source_mode": "rest_only",
        "available": False,
        "cache_db": "ws_market_cache.db",
        "reason": "not_checked",
    }
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
    *,
    _market_connection: sqlite3.Connection | None = None,
    _ws_connection: sqlite3.Connection | None = None,
    _ws_context: dict[str, Any] | None = None,
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

    connection: sqlite3.Connection | None = _market_connection
    ws_connection: sqlite3.Connection | None = _ws_connection
    owns_market_connection = connection is None
    # Batch callers may intentionally provide a shared WS proof context even
    # when the optional WS SQLite file is absent.  Treat that as an explicit
    # shared dependency so the single-symbol helper does not reopen the same
    # missing connection once per candidate.
    ws_context_supplied = _ws_context is not None
    owns_ws_connection = ws_connection is None and not ws_context_supplied
    try:
        if connection is None:
            connection = sqlite3.connect(
                f"file:{market_db.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=5,
            )
            connection.row_factory = sqlite3.Row
        if ws_connection is None and not ws_context_supplied:
            ws_connection, ws_context = open_ws_confirmation_connection(db_root)
        else:
            ws_context = dict(_ws_context or {})
        result["closed_bar_proof_context"] = dict(ws_context)
        for timeframe in TIMEFRAME_SECONDS:
            expected_ts = expected_closed_bar_start(cycle_cst, timeframe)
            expected_successor_ts = immediate_successor_bar_start(
                expected_ts, timeframe)
            row = connection.execute(
                "SELECT ts,o,h,l,c,v,ma5,ma20,atr14,rsi14,macd_hist "
                "FROM kline_cache WHERE symbol=? AND tf=? AND ts=? "
                "LIMIT 1",
                (symbol, timeframe, expected_ts),
            ).fetchone()
            successor_row = connection.execute(
                "SELECT ts FROM kline_cache WHERE symbol=? AND tf=? AND ts=? "
                "LIMIT 1",
                (symbol, timeframe, expected_successor_ts),
            ).fetchone()
            observed_successor_ts = _field(successor_row, "ts")
            closed_bar_proof = build_closed_bar_proof(
                observed_successor_ts=observed_successor_ts,
                expected_successor_ts=expected_successor_ts,
                ws_connection=ws_connection,
                ws_context=ws_context,
                symbol=str(symbol),
                timeframe=timeframe,
                expected_ts=expected_ts,
                market_row=row,
            )
            closed_bar_proven = closed_bar_proof["proven"] is True
            bars_seen = int(
                connection.execute(
                    "SELECT COUNT(*) FROM kline_cache "
                    "WHERE symbol=? AND tf=? AND ts<=?",
                    (symbol, timeframe, expected_ts),
                ).fetchone()[0]
            )
            validation = validate_kline_row(row)
            raw_errors = list(validation["raw_errors"])
            if not closed_bar_proven:
                raw_errors.append(
                    "closed_state_unproven_no_successor_bar"
                )
            validation = {
                **validation,
                "raw_errors": raw_errors,
                "raw_valid": not raw_errors,
                "ready": not raw_errors and not validation["indicator_errors"],
            }
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
                    "closed_bar_proof": closed_bar_proof,
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
        if owns_market_connection and connection is not None:
            connection.close()
        if owns_ws_connection and ws_connection is not None:
            ws_connection.close()

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
                "closed_bar_proof": row["closed_bar_proof"],
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


def check_multitimeframe_readiness_batch(
    db_root: str | Path,
    symbols: list[str] | tuple[str, ...],
    cycle_id: str,
) -> list[dict[str, Any]]:
    """Build exact-cycle evidence for many symbols with shared read connections.

    The result order exactly follows ``symbols``.  This is a read-only throughput
    helper; the single-symbol contract and all readiness semantics remain owned
    by :func:`check_multitimeframe_readiness`.
    """
    normalized = [str(symbol).strip().upper() for symbol in symbols]
    if not normalized:
        return []
    market_db = Path(db_root) / "market.db"
    if not market_db.is_file():
        raise FileNotFoundError(f"market_db_missing:{market_db}")
    connection: sqlite3.Connection | None = None
    ws_connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{market_db.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        ws_connection, ws_context = open_ws_confirmation_connection(db_root)
        return [
            check_multitimeframe_readiness(
                db_root,
                symbol,
                cycle_id,
                _market_connection=connection,
                _ws_connection=ws_connection,
                _ws_context=ws_context,
            )
            for symbol in normalized
        ]
    finally:
        if connection is not None:
            connection.close()
        if ws_connection is not None:
            ws_connection.close()
