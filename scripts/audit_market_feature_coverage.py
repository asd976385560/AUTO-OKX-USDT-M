#!/usr/bin/env python3
"""Audit dynamic order-book and recent-trade enrichment by natural slot.

Every expected slot keeps a fixed configured denominator.  The exact ranked
selection must be frozen in the same slot and its canonical SHA-256 is rebuilt
independently.  Missing/corrupt selection snapshots, missing or duplicate
feature rows, stale samples, invalid fields, and inconsistent derived values
remain in the denominator.

The production database is opened read-only.  Only the explicit JSON evidence
file is atomically replaced; this script never recollects, backfills, changes a
production threshold, dispatches, or places an order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import _acceptance_thresholds as thresholds


CST = timezone(timedelta(hours=8))
UTC = timezone.utc
SLOT_MINUTES = 15
DEFAULT_DB = Path(r".\db\market.db")
DEFAULT_OUTPUT = Path(
    r".\reports\quality\market-feature-coverage-audit.json")
DEFAULT_FORWARD_START = "2026-08-12T23:15:00+08:00"
DEFAULT_EXPECTED_SYMBOLS = 100
DEPTH_BPS = (10, 25, 50)
SLIPPAGE_USD = (100, 500, 1000)
OFFICIAL_SNAPSHOT_SOURCE = "okx_public_instruments_live_usdt_linear_swap"


def _parse_cst(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def _parse_utc(value: Any) -> datetime:
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _slot_floor(value: datetime) -> datetime:
    local = value.astimezone(CST)
    return local.replace(
        minute=(local.minute // SLOT_MINUTES) * SLOT_MINUTES,
        second=0,
        microsecond=0,
    )


def _ensure_slot_aligned(value: datetime, label: str) -> None:
    if value != _slot_floor(value):
        raise ValueError(f"{label} must align to a 15-minute CST slot")


def _completed_end_exclusive(as_of: datetime, grace_minutes: int) -> datetime:
    cutoff = as_of.astimezone(CST) - timedelta(minutes=grace_minutes)
    return _slot_floor(cutoff) + timedelta(minutes=SLOT_MINUTES)


def _expected_slots(start: datetime, end_exclusive: datetime) -> list[datetime]:
    _ensure_slot_aligned(start, "window start")
    _ensure_slot_aligned(end_exclusive, "window end")
    if end_exclusive < start:
        raise ValueError("window end must not precede start")
    output: list[datetime] = []
    current = start
    while current < end_exclusive:
        output.append(current)
        current += timedelta(minutes=SLOT_MINUTES)
    return output


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0


def _nonnegative(value: Any) -> bool:
    return _finite(value) and float(value) >= 0


def _close(left: Any, right: Any, *, abs_tol: float = 1e-8) -> bool:
    return (
        _finite(left)
        and _finite(right)
        and math.isclose(
            float(left), float(right), rel_tol=1e-8, abs_tol=abs_tol)
    )


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _selection_hash(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _selection_for_slot(
    connection: sqlite3.Connection,
    cycle_id: str,
    slot_utc: datetime,
    expected_symbols: int,
) -> tuple[list[str], dict[str, Any]]:
    required = {
        "market_feature_selection_runs", "market_feature_selection_rows",
    }
    if not required.issubset(_table_names(connection)):
        return [], {
            "status": "MISSING",
            "reasons": ["selection_snapshot_tables_missing"],
            "observed_row_count": 0,
        }
    header = connection.execute(
        "SELECT collected_ts_utc,selected_count,max_symbols,payload_sha256,"
        "complete,source FROM market_feature_selection_runs WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if header is None:
        return [], {
            "status": "MISSING",
            "reasons": ["selection_snapshot_header_missing"],
            "observed_row_count": 0,
        }
    raw_rows = connection.execute(
        "SELECT selection_rank,symbol FROM market_feature_selection_rows "
        "WHERE cycle_id=? ORDER BY selection_rank",
        (cycle_id,),
    ).fetchall()
    rows = [
        {
            "selection_rank": int(row["selection_rank"]),
            "symbol": str(row["symbol"] or "").strip().upper(),
        }
        for row in raw_rows
    ]
    observed_hash = _selection_hash(rows)
    reasons: list[str] = []
    header_count = int(header["selected_count"])
    header_max = int(header["max_symbols"])
    if int(header["complete"]) != 1:
        reasons.append("header_not_complete")
    if header_count != len(rows):
        reasons.append("header_row_count_mismatch")
    if header_count != expected_symbols or header_max != expected_symbols:
        reasons.append("configured_denominator_mismatch")
    if str(header["payload_sha256"]) != observed_hash:
        reasons.append("payload_sha256_mismatch")
    if str(header["source"]) != "dynamic_liquidity_oi_focus_rank_v1":
        reasons.append("selection_source_invalid")
    expected_ranks = list(range(1, expected_symbols + 1))
    if [row["selection_rank"] for row in rows] != expected_ranks:
        reasons.append("selection_ranks_not_contiguous")
    symbols = [row["symbol"] for row in rows]
    if len(symbols) != len(set(symbols)):
        reasons.append("selection_symbols_duplicate")
    if any(not symbol.endswith("-USDT-SWAP") for symbol in symbols):
        reasons.append("selection_symbol_invalid")
    collected_at: datetime | None = None
    try:
        collected_at = _parse_utc(header["collected_ts_utc"])
    except (TypeError, ValueError):
        reasons.append("collected_ts_invalid")
    if collected_at is not None and not (
        slot_utc <= collected_at < slot_utc + timedelta(minutes=SLOT_MINUTES)
    ):
        reasons.append("collected_ts_outside_slot")
    valid_symbols = [
        symbol for symbol in symbols
        if symbol.endswith("-USDT-SWAP")
    ][:expected_symbols]
    return valid_symbols, {
        "status": "PASSED" if not reasons else "NOT_MET",
        "reasons": reasons,
        "collected_ts_utc": header["collected_ts_utc"],
        "source": header["source"],
        "header_selected_count": header_count,
        "header_max_symbols": header_max,
        "observed_row_count": len(rows),
        "stored_payload_sha256": header["payload_sha256"],
        "observed_payload_sha256": observed_hash,
    }


def _json_levels(value: Any) -> list[list[Any]] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list):
        return None
    output: list[list[Any]] = []
    for level in parsed:
        if not isinstance(level, list) or len(level) < 2:
            return None
        if not _positive(level[0]) or not _positive(level[1]):
            return None
        output.append(level)
    return output


def _canonical_official_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "symbol": str(row["symbol"] or "").strip().upper(),
        "list_time_utc": row["list_time_utc"],
        "state": row["state"],
        "settle_ccy": row["settle_ccy"],
        "ct_type": row["ct_type"],
        "inst_category": row["inst_category"],
        "ct_val": row["ct_val"],
        "lot_sz": row["lot_sz"],
    }


def _official_snapshot_hash(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _official_ct_values_for_slot(
    connection: sqlite3.Connection,
    cycle_id: str,
    slot_utc: datetime,
) -> tuple[dict[str, float], dict[str, Any]]:
    required = {
        "official_instrument_snapshot_runs",
        "official_instrument_snapshot_rows",
    }
    if not required.issubset(_table_names(connection)):
        return {}, {
            "status": "MISSING",
            "reasons": ["official_snapshot_tables_missing"],
            "header_symbol_count": 0,
            "observed_row_count": 0,
            "valid_metadata_rows": 0,
            "metadata_coverage_rate": 0.0,
        }
    header = connection.execute(
        "SELECT collected_ts_utc,symbol_count,payload_sha256,complete,source "
        "FROM official_instrument_snapshot_runs WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if header is None:
        return {}, {
            "status": "MISSING",
            "reasons": ["official_snapshot_header_missing"],
            "header_symbol_count": 0,
            "observed_row_count": 0,
            "valid_metadata_rows": 0,
            "metadata_coverage_rate": 0.0,
        }
    raw_rows = connection.execute(
        "SELECT symbol,list_time_utc,state,settle_ccy,ct_type,inst_category,"
        "ct_val,lot_sz FROM official_instrument_snapshot_rows "
        "WHERE cycle_id=? ORDER BY symbol",
        (cycle_id,),
    ).fetchall()
    rows = [_canonical_official_row(row) for row in raw_rows]
    observed_hash = _official_snapshot_hash(rows)
    header_count = int(header["symbol_count"])
    reasons: list[str] = []
    if int(header["complete"]) != 1:
        reasons.append("header_not_complete")
    if header_count != len(rows):
        reasons.append("header_row_count_mismatch")
    if str(header["payload_sha256"]) != observed_hash:
        reasons.append("payload_sha256_mismatch")
    if str(header["source"]) != OFFICIAL_SNAPSHOT_SOURCE:
        reasons.append("official_snapshot_source_invalid")
    collected_at: datetime | None = None
    try:
        collected_at = _parse_utc(header["collected_ts_utc"])
    except (TypeError, ValueError):
        reasons.append("collected_ts_invalid")
    if collected_at is not None and not (
        slot_utc <= collected_at < slot_utc + timedelta(minutes=SLOT_MINUTES)
    ):
        reasons.append("collected_ts_outside_slot")
    if not rows:
        reasons.append("official_snapshot_empty")

    output: dict[str, float] = {}
    invalid_rows: list[dict[str, Any]] = []
    valid_metadata = 0
    for row in rows:
        symbol = str(row["symbol"])
        row_reasons: list[str] = []
        if not symbol.endswith("-USDT-SWAP"):
            row_reasons.append("symbol_not_usdt_swap")
        if str(row["state"] or "").lower() != "live":
            row_reasons.append("state_not_live")
        if str(row["settle_ccy"] or "").upper() != "USDT":
            row_reasons.append("settle_ccy_not_usdt")
        if str(row["ct_type"] or "").lower() != "linear":
            row_reasons.append("ct_type_not_linear")
        if not str(row["inst_category"] or "").strip():
            row_reasons.append("inst_category_missing")
        if not _positive(row["ct_val"]):
            row_reasons.append("ct_val_invalid")
        if not _positive(row["lot_sz"]):
            row_reasons.append("lot_sz_invalid")
        try:
            listing = _parse_utc(row["list_time_utc"])
            if listing > slot_utc + timedelta(minutes=SLOT_MINUTES):
                row_reasons.append("list_time_after_slot")
        except (TypeError, ValueError):
            row_reasons.append("list_time_invalid")
        if row_reasons:
            if len(invalid_rows) < 20:
                invalid_rows.append({
                    "symbol": symbol,
                    "reasons": row_reasons,
                })
        else:
            valid_metadata += 1
            output[symbol] = float(row["ct_val"])
    metadata_rate = valid_metadata / len(rows) if rows else 0.0
    evidence = {
        "status": "PASSED" if not reasons else "NOT_MET",
        "reasons": reasons,
        "collected_ts_utc": header["collected_ts_utc"],
        "source": header["source"],
        "header_symbol_count": header_count,
        "observed_row_count": len(rows),
        "stored_payload_sha256": header["payload_sha256"],
        "observed_payload_sha256": observed_hash,
        "valid_metadata_rows": valid_metadata,
        "invalid_metadata_rows": len(rows) - valid_metadata,
        "metadata_coverage_rate": round(metadata_rate, 6),
        "invalid_metadata_examples": invalid_rows,
    }
    # Header integrity establishes provenance for every row.  When it fails,
    # no ctVal from that snapshot may validate a derived USD feature.
    if reasons:
        output = {}
    return output, evidence


def _depth_usd(
    levels: list[list[Any]], mid: float, ct_val: float, side: str, bp: int
) -> float:
    if side == "bid":
        bound = mid * (1 - bp / 10000)
        chosen = (level for level in levels if float(level[0]) >= bound)
    else:
        bound = mid * (1 + bp / 10000)
        chosen = (level for level in levels if float(level[0]) <= bound)
    return sum(
        float(level[0]) * float(level[1]) * ct_val for level in chosen)


def _slippage_bps(
    levels: list[list[Any]], mid: float, ct_val: float, target_usd: float
) -> float | None:
    remaining = target_usd
    contracts = 0.0
    paid = 0.0
    for level in levels:
        px, qty = float(level[0]), float(level[1])
        level_usd = px * qty * ct_val
        take_usd = min(remaining, level_usd)
        take_contracts = take_usd / (px * ct_val)
        paid += take_contracts * px
        contracts += take_contracts
        remaining -= take_usd
        if remaining <= 1e-8:
            break
    if remaining > 1e-6 or contracts <= 0:
        return None
    return abs(paid / contracts - mid) / mid * 10000


def _within_slot(value: Any, slot_utc: datetime) -> bool:
    try:
        parsed = _parse_utc(value)
    except (TypeError, ValueError):
        return False
    return slot_utc <= parsed < slot_utc + timedelta(minutes=SLOT_MINUTES)


def _microstructure_errors(
    row: sqlite3.Row,
    slot_utc: datetime,
    ct_val: float | None,
) -> list[str]:
    errors: list[str] = []
    if not _within_slot(row["ts"], slot_utc):
        errors.append("row_ts_outside_slot")
    if not _within_slot(row["book_ts"], slot_utc):
        errors.append("book_ts_outside_slot")
    if int(row["depth_levels"] or 0) != 50:
        errors.append("depth_levels_not_50")
    if str(row["source"]) != "okx":
        errors.append("source_invalid")
    if ct_val is None or not _positive(ct_val):
        errors.append("official_ct_val_missing_or_invalid")
    bids = _json_levels(row["raw_bids"])
    asks = _json_levels(row["raw_asks"])
    if bids is None or len(bids) != 50:
        errors.append("raw_bids_invalid")
    if asks is None or len(asks) != 50:
        errors.append("raw_asks_invalid")
    if (
        bids is None or asks is None or len(bids) != 50 or len(asks) != 50
        or ct_val is None or not _positive(ct_val)
    ):
        return errors
    bid_prices = [float(level[0]) for level in bids]
    ask_prices = [float(level[0]) for level in asks]
    if any(left < right for left, right in zip(bid_prices, bid_prices[1:])):
        errors.append("bid_levels_not_descending")
    if any(left > right for left, right in zip(ask_prices, ask_prices[1:])):
        errors.append("ask_levels_not_ascending")
    best_bid, best_ask = bid_prices[0], ask_prices[0]
    if best_ask <= best_bid:
        errors.append("book_crossed_or_locked")
        return errors
    mid = (best_bid + best_ask) / 2
    spread = (best_ask - best_bid) / mid * 10000
    for label, observed, expected in (
        ("best_bid", row["best_bid"], best_bid),
        ("best_ask", row["best_ask"], best_ask),
        ("mid_px", row["mid_px"], mid),
        ("spread_bps", row["spread_bps"], spread),
    ):
        if not _close(observed, expected):
            errors.append(f"{label}_mismatch")
    for bp in DEPTH_BPS:
        bid_depth = _depth_usd(bids, mid, ct_val, "bid", bp)
        ask_depth = _depth_usd(asks, mid, ct_val, "ask", bp)
        if not _positive(bid_depth) or not _positive(ask_depth):
            errors.append(f"depth_{bp}bp_empty")
            continue
        for side, observed, expected in (
            ("bid", row[f"bid_depth_{bp}bp_usd"], bid_depth),
            ("ask", row[f"ask_depth_{bp}bp_usd"], ask_depth),
        ):
            if not _close(observed, expected, abs_tol=1e-5):
                errors.append(f"{side}_depth_{bp}bp_mismatch")
        imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        if not _close(row[f"imbalance_{bp}bp"], imbalance):
            errors.append(f"imbalance_{bp}bp_mismatch")
    for target in SLIPPAGE_USD:
        for side, levels, column in (
            ("buy", asks, f"buy_slippage_{target}usd_bps"),
            ("sell", bids, f"sell_slippage_{target}usd_bps"),
        ):
            expected = _slippage_bps(levels, mid, ct_val, target)
            if expected is None or not _close(row[column], expected):
                errors.append(f"{side}_slippage_{target}usd_invalid")
    if not _finite(row["seq_id"]):
        errors.append("seq_id_invalid")
    return errors


def _trade_flow_errors(
    row: sqlite3.Row,
    slot_utc: datetime,
    maximum_age_seconds: int,
) -> list[str]:
    errors: list[str] = []
    try:
        row_ts = _parse_utc(row["ts"])
        start = _parse_utc(row["sample_start"])
        end = _parse_utc(row["sample_end"])
    except (TypeError, ValueError):
        return ["sample_timestamp_invalid"]
    if not slot_utc <= row_ts < slot_utc + timedelta(minutes=SLOT_MINUTES):
        errors.append("row_ts_outside_slot")
    if start > end:
        errors.append("sample_time_reversed")
    if abs((row_ts - end).total_seconds()) > maximum_age_seconds:
        errors.append("sample_end_stale")
    expected_span = (end - start).total_seconds() * 1000
    # Stored endpoints are second precision while sample_span_ms preserves the
    # source millisecond values.  Their independently rebuilt spans can differ
    # by strictly less than one second solely because both endpoints truncate.
    if not _close(row["sample_span_ms"], expected_span, abs_tol=1000.0):
        errors.append("sample_span_mismatch")
    sample_count = int(row["sample_count"] or 0)
    if not 0 < sample_count <= 500:
        errors.append("sample_count_invalid")
    if str(row["source"]) != "okx_recent_trades":
        errors.append("source_invalid")
    for field in (
        "buy_qty_contracts", "sell_qty_contracts", "buy_notional_usd",
        "sell_notional_usd", "largest_trade_usd",
    ):
        if not _nonnegative(row[field]):
            errors.append(f"{field}_invalid")
    buy = float(row["buy_notional_usd"] or 0)
    sell = float(row["sell_notional_usd"] or 0)
    total = buy + sell
    if total <= 0:
        errors.append("sample_notional_empty")
    else:
        if not _close(row["taker_buy_ratio"], buy / total):
            errors.append("taker_buy_ratio_mismatch")
        if not _close(row["cvd_notional_usd"], buy - sell, abs_tol=1e-5):
            errors.append("cvd_notional_mismatch")
    try:
        raw = json.loads(str(row["raw_sample"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = None
    if not isinstance(raw, list) or not raw or len(raw) > 50:
        errors.append("raw_sample_invalid")
    elif len(raw) > sample_count:
        errors.append("raw_sample_exceeds_count")
    else:
        for item in raw:
            if (
                not isinstance(item, dict)
                or not _positive(item.get("px"))
                or not _positive(item.get("sz"))
                or item.get("side") not in ("buy", "sell")
            ):
                errors.append("raw_sample_trade_invalid")
                break
    return errors


def _rows_by_symbol(
    connection: sqlite3.Connection,
    table: str,
    cycle_id: str,
) -> tuple[dict[str, list[sqlite3.Row]], list[sqlite3.Row]]:
    rows = connection.execute(
        f"SELECT * FROM {table} WHERE cycle_id=? ORDER BY symbol,ts",
        (cycle_id,),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["symbol"]), []).append(row)
    return grouped, rows


def audit_market_feature_coverage(
    market_db: Path,
    *,
    as_of: datetime,
    forward_start: datetime,
    target_rate: float | None = None,
    minimum_slots: int = 96,
    expected_symbols_per_slot: int = DEFAULT_EXPECTED_SYMBOLS,
    grace_minutes: int = 5,
    maximum_trade_sample_age_seconds: int = 300,
) -> dict[str, Any]:
    # None = 按预注册激活边界解析（边界前 0.99、边界起 0.95）。
    if target_rate is None:
        target_rate = thresholds.coverage_target_rate(as_of)
    if not 0 < target_rate <= 1:
        raise ValueError("target_rate must be in (0,1]")
    if minimum_slots <= 0 or expected_symbols_per_slot <= 0:
        raise ValueError("minimum_slots and expected symbols must be positive")
    if not 0 <= grace_minutes < SLOT_MINUTES:
        raise ValueError("grace_minutes must be in [0,15)")
    if maximum_trade_sample_age_seconds < 0:
        raise ValueError("maximum trade sample age must be nonnegative")
    as_of = as_of.astimezone(CST)
    forward_start = forward_start.astimezone(CST)
    _ensure_slot_aligned(forward_start, "forward_start")
    end_exclusive = max(
        forward_start, _completed_end_exclusive(as_of, grace_minutes))
    slots = _expected_slots(forward_start, end_exclusive)
    denominator = len(slots) * expected_symbols_per_slot
    selection_passed = official_passed = 0
    official_metadata_valid = official_metadata_rows = 0
    micro_valid = flow_valid = combined_valid = 0
    slot_passed = 0
    slot_rows: list[dict[str, Any]] = []
    connection = _ro(market_db)
    try:
        tables = _table_names(connection)
        feature_tables_present = {
            "market_microstructure", "market_trade_flow",
        }.issubset(tables)
        for slot in slots:
            slot_utc = slot.astimezone(UTC)
            cycle_id = slot.strftime("%Y-%m-%dT%H:%M")
            symbols, selection = _selection_for_slot(
                connection, cycle_id, slot_utc, expected_symbols_per_slot)
            official_ct_values, official_snapshot = (
                _official_ct_values_for_slot(
                    connection, cycle_id, slot_utc))
            if selection["status"] == "PASSED":
                selection_passed += 1
            if official_snapshot["status"] == "PASSED":
                official_passed += 1
            official_metadata_valid += int(
                official_snapshot["valid_metadata_rows"])
            official_metadata_rows += int(
                official_snapshot["observed_row_count"])
            if feature_tables_present:
                micro_rows, all_micro = _rows_by_symbol(
                    connection, "market_microstructure", cycle_id)
                flow_rows, all_flow = _rows_by_symbol(
                    connection, "market_trade_flow", cycle_id)
            else:
                micro_rows, flow_rows, all_micro, all_flow = {}, {}, [], []
            micro_good: set[str] = set()
            flow_good: set[str] = set()
            invalid_examples: list[dict[str, Any]] = []
            for symbol in symbols:
                symbol_micro = micro_rows.get(symbol, [])
                symbol_flow = flow_rows.get(symbol, [])
                micro_errors = (
                    ["missing_or_duplicate_row"]
                    if len(symbol_micro) != 1
                    else _microstructure_errors(
                        symbol_micro[0], slot_utc,
                        official_ct_values.get(symbol),
                    )
                )
                flow_errors = (
                    ["missing_or_duplicate_row"]
                    if len(symbol_flow) != 1
                    else _trade_flow_errors(
                        symbol_flow[0], slot_utc,
                        maximum_trade_sample_age_seconds,
                    )
                )
                if not micro_errors:
                    micro_good.add(symbol)
                if not flow_errors:
                    flow_good.add(symbol)
                if (micro_errors or flow_errors) and len(invalid_examples) < 20:
                    invalid_examples.append({
                        "symbol": symbol,
                        "microstructure_errors": micro_errors,
                        "trade_flow_errors": flow_errors,
                    })
            slot_micro = len(micro_good)
            slot_flow = len(flow_good)
            slot_combined = len(micro_good & flow_good)
            micro_valid += slot_micro
            flow_valid += slot_flow
            combined_valid += slot_combined
            micro_rate = slot_micro / expected_symbols_per_slot
            flow_rate = slot_flow / expected_symbols_per_slot
            combined_rate = slot_combined / expected_symbols_per_slot
            passed = (
                selection["status"] == "PASSED"
                and official_snapshot["status"] == "PASSED"
                and official_snapshot["metadata_coverage_rate"] >= target_rate
                and micro_rate >= target_rate
                and flow_rate >= target_rate
                and combined_rate >= target_rate
            )
            if passed:
                slot_passed += 1
            selected_set = set(symbols)
            micro_set = set(micro_rows)
            flow_set = set(flow_rows)
            slot_rows.append({
                "cycle_id": cycle_id,
                "selection_snapshot": selection,
                "official_instrument_snapshot": official_snapshot,
                "fixed_denominator_symbols": expected_symbols_per_slot,
                "observed_selection_symbols": len(symbols),
                "microstructure_rows": len(all_micro),
                "trade_flow_rows": len(all_flow),
                "microstructure_valid_symbols": slot_micro,
                "trade_flow_valid_symbols": slot_flow,
                "combined_valid_symbols": slot_combined,
                "microstructure_coverage_rate": round(micro_rate, 6),
                "trade_flow_coverage_rate": round(flow_rate, 6),
                "combined_coverage_rate": round(combined_rate, 6),
                "missing_microstructure_symbols": len(selected_set - micro_set),
                "missing_trade_flow_symbols": len(selected_set - flow_set),
                "extra_microstructure_symbols": len(micro_set - selected_set),
                "extra_trade_flow_symbols": len(flow_set - selected_set),
                "status": "PASSED" if passed else "NOT_MET",
                "invalid_examples": invalid_examples,
            })
    finally:
        connection.close()
    expected_slots = len(slots)
    selection_rate = (
        selection_passed / expected_slots if expected_slots else 0.0)
    official_rate = (
        official_passed / expected_slots if expected_slots else 0.0)
    official_metadata_rate = (
        official_metadata_valid / official_metadata_rows
        if official_metadata_rows else 0.0)
    micro_rate = micro_valid / denominator if denominator else 0.0
    flow_rate = flow_valid / denominator if denominator else 0.0
    combined_rate = combined_valid / denominator if denominator else 0.0
    slot_rate = slot_passed / expected_slots if expected_slots else 0.0
    requirements = {
        "minimum_slots_met": expected_slots >= minimum_slots,
        "selection_snapshot_slot_rate_at_least_target": (
            selection_rate >= target_rate),
        "official_snapshot_slot_rate_at_least_target": (
            official_rate >= target_rate),
        "official_metadata_coverage_rate_at_least_target": (
            official_metadata_rate >= target_rate),
        "microstructure_coverage_rate_at_least_target": (
            micro_rate >= target_rate),
        "trade_flow_coverage_rate_at_least_target": flow_rate >= target_rate,
        "combined_coverage_rate_at_least_target": combined_rate >= target_rate,
        "slot_pass_rate_at_least_target": slot_rate >= target_rate,
    }
    if not requirements["minimum_slots_met"]:
        status = "INSUFFICIENT_EVIDENCE"
    elif all(requirements.values()):
        status = "PASSED"
    else:
        status = "NOT_MET"
    return {
        "schema_version": 1,
        "artifact_type": "scheduled_market_feature_coverage_audit",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "as_of_cst": as_of.isoformat(),
        "forward_start_cst": forward_start.isoformat(),
        "end_exclusive_cst": end_exclusive.isoformat(),
        "mode": "read_only",
        "target_rate": target_rate,
        "target_rate_migration": thresholds.coverage_migration_facts(as_of),
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics(
            {
                "selection_snapshot_slot_rate": selection_rate,
                "official_snapshot_slot_rate": official_rate,
                "official_metadata_coverage_rate": official_metadata_rate,
                "microstructure_coverage_rate": micro_rate,
                "trade_flow_coverage_rate": flow_rate,
                "combined_coverage_rate": combined_rate,
                "slot_pass_rate": slot_rate,
            },
            target_dependent=("slot_pass_rate",),
        ),
        "minimum_slots": minimum_slots,
        "expected_symbols_per_slot": expected_symbols_per_slot,
        "slot_grace_minutes": grace_minutes,
        "maximum_trade_sample_age_seconds": maximum_trade_sample_age_seconds,
        "contracts": {
            "selection_denominator": (
                "fixed configured symbol count per natural slot; exact ranked "
                "selection snapshot and canonical SHA-256 rebuilt independently"
            ),
            "official_instrument_snapshot": (
                "same-slot official live USDT linear SWAP metadata; source, "
                "count, canonical SHA-256, clock and row fields rebuilt and "
                "validated independently; corrupt provenance fails closed"
            ),
            "microstructure": (
                "50 raw bid/ask levels with independently rebuilt mid, spread, "
                "depth, imbalance and USD slippage"
            ),
            "trade_flow": (
                "recent trade sample clock, nonnegative aggregates, buy-ratio "
                "and CVD algebra, plus a valid capped raw sample"
            ),
            "missing_semantics": (
                "missing/corrupt selections, missing/duplicate rows, invalid "
                "fields and stale samples remain in the fixed denominator"
            ),
            "no_backfill": True,
        },
        "counts": {
            "expected_slots": expected_slots,
            "passed_selection_slots": selection_passed,
            "passed_official_snapshot_slots": official_passed,
            "official_metadata_rows": official_metadata_rows,
            "official_metadata_valid_rows": official_metadata_valid,
            "passed_slots": slot_passed,
            "failed_slots": expected_slots - slot_passed,
            "expected_symbol_rows": denominator,
            "microstructure_valid_symbol_rows": micro_valid,
            "trade_flow_valid_symbol_rows": flow_valid,
            "combined_valid_symbol_rows": combined_valid,
        },
        "rates": {
            "selection_snapshot_slot_rate": round(selection_rate, 6),
            "official_snapshot_slot_rate": round(official_rate, 6),
            "official_metadata_coverage_rate": round(
                official_metadata_rate, 6),
            "microstructure_coverage_rate": round(micro_rate, 6),
            "trade_flow_coverage_rate": round(flow_rate, 6),
            "combined_coverage_rate": round(combined_rate, 6),
            "slot_pass_rate": round(slot_rate, 6),
        },
        "requirements": requirements,
        "status": status,
        "slots": slot_rows,
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "production_execution_authorized": False,
        "orders_placed": 0,
    }


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--forward-start", default=DEFAULT_FORWARD_START)
    parser.add_argument(
        "--target-rate", type=float, default=None,
        help="default: resolved from the pre-registered activation boundary")
    parser.add_argument("--minimum-slots", type=int, default=96)
    parser.add_argument(
        "--expected-symbols-per-slot", type=int,
        default=DEFAULT_EXPECTED_SYMBOLS,
    )
    parser.add_argument("--grace-minutes", type=int, default=5)
    parser.add_argument(
        "--maximum-trade-sample-age-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        payload = audit_market_feature_coverage(
            args.market_db,
            as_of=_parse_cst(args.as_of) if args.as_of else datetime.now(CST),
            forward_start=_parse_cst(args.forward_start),
            target_rate=args.target_rate,
            minimum_slots=args.minimum_slots,
            expected_symbols_per_slot=args.expected_symbols_per_slot,
            grace_minutes=args.grace_minutes,
            maximum_trade_sample_age_seconds=(
                args.maximum_trade_sample_age_seconds),
        )
        _atomic_json(args.json_out, payload)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "ok": True,
        "output": str(args.json_out),
        "status": payload["status"],
        "expected_slots": payload["counts"]["expected_slots"],
        "expected_symbol_rows": payload["counts"]["expected_symbol_rows"],
        "microstructure_coverage_rate": payload["rates"][
            "microstructure_coverage_rate"],
        "trade_flow_coverage_rate": payload["rates"][
            "trade_flow_coverage_rate"],
        "combined_coverage_rate": payload["rates"][
            "combined_coverage_rate"],
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
