# -*- coding: utf-8 -*-
"""Audit exact closed 15m/1H/4H coverage against the live swap universe.

The audit keeps every symbol in the denominator and separates two contracts:

* source data completeness: an exact expected closed OHLCV bar exists and is
  valid for the symbol/timeframe;
* analysis readiness: the same bar also has the full indicator set required by
  the current multi-timeframe decision logic.

Newly listed contracts with fewer than the 34 bars required for MACD are
reported as ``insufficient_history``.  They are never silently excluded and no
indicator is fabricated.  The database is opened read-only; only the explicit
JSON evidence file is atomically replaced.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.multitimeframe_gate import (  # noqa: E402
    INDICATOR_FIELDS,
    MINIMUM_BARS_FOR_FULL_INDICATORS,
    RAW_FIELDS,
    TIMEFRAME_SECONDS,
    validate_kline_row,
)


CST = timezone(timedelta(hours=8))
UTC = timezone.utc
DEFAULT_MARKET_DB = Path(r"./db/market.db")
DEFAULT_OUTPUT = Path(
    r"./reports/quality/multitimeframe-coverage-audit.json")
def _parse_ts(value: str) -> datetime:
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def expected_closed_bar_start(evaluation_utc: datetime, timeframe: str) -> str:
    """Return the exact UTC opening timestamp of the latest fully closed bar."""
    seconds = TIMEFRAME_SECONDS[timeframe]
    epoch = int(evaluation_utc.astimezone(UTC).timestamp())
    closed_start_epoch = (epoch // seconds) * seconds - seconds
    return _iso_utc(datetime.fromtimestamp(closed_start_epoch, tz=UTC))


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _raw_errors(row: sqlite3.Row | None) -> list[str]:
    return list(validate_kline_row(row)["raw_errors"])


def _indicator_errors(row: sqlite3.Row | None) -> list[str]:
    return list(validate_kline_row(row)["indicator_errors"])


def _rows_for_exact_bar(
    connection: sqlite3.Connection,
    symbols: list[str],
    timeframe: str,
    bar_ts: str,
) -> dict[str, sqlite3.Row]:
    placeholders = ",".join("?" for _ in symbols)
    rows = connection.execute(
        "SELECT symbol,ts,o,h,l,c,v,ma5,ma20,atr14,rsi14,macd_hist "
        f"FROM kline_cache WHERE tf=? AND ts=? AND symbol IN ({placeholders})",
        (timeframe, bar_ts, *symbols),
    ).fetchall()
    return {str(row["symbol"]): row for row in rows}


def _history_counts(
    connection: sqlite3.Connection,
    symbols: list[str],
    timeframe: str,
    bar_ts: str,
) -> dict[str, int]:
    placeholders = ",".join("?" for _ in symbols)
    rows = connection.execute(
        "SELECT symbol,COUNT(*) AS n FROM kline_cache "
        f"WHERE tf=? AND ts<=? AND symbol IN ({placeholders}) GROUP BY symbol",
        (timeframe, bar_ts, *symbols),
    ).fetchall()
    return {str(row["symbol"]): int(row["n"]) for row in rows}


def audit_multitimeframe_coverage(
    market_db: Path,
    *,
    minimum_rate: float = 0.99,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not 0 < minimum_rate <= 1:
        raise ValueError("minimum_rate must be in (0,1]")
    evaluation_utc = (now or datetime.now(UTC)).astimezone(UTC)
    evaluation_iso = _iso_utc(evaluation_utc)
    connection = _ro(market_db)
    try:
        latest_tick = connection.execute(
            "SELECT MAX(ts) FROM tick_snapshots WHERE ts<=?", (evaluation_iso,)
        ).fetchone()[0]
        if latest_tick is None:
            raise ValueError(f"no ticker snapshot at or before {evaluation_iso}")
        symbols = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT symbol FROM tick_snapshots "
                "WHERE ts=? AND symbol LIKE '%-USDT-SWAP' ORDER BY symbol",
                (latest_tick,),
            ).fetchall()
        ]
        if not symbols:
            raise ValueError(f"empty USDT linear SWAP universe at {latest_tick}")

        timeframes: list[dict[str, Any]] = []
        for timeframe in TIMEFRAME_SECONDS:
            expected_ts = expected_closed_bar_start(evaluation_utc, timeframe)
            rows = _rows_for_exact_bar(
                connection, symbols, timeframe, expected_ts)
            history = _history_counts(
                connection, symbols, timeframe, expected_ts)
            raw_valid: list[str] = []
            ready: list[str] = []
            gaps: list[dict[str, Any]] = []
            for symbol in symbols:
                row = rows.get(symbol)
                raw_errors = _raw_errors(row)
                indicator_errors = _indicator_errors(row)
                bars_seen = history.get(symbol, 0)
                if not raw_errors:
                    raw_valid.append(symbol)
                if (
                    not raw_errors
                    and not indicator_errors
                    and bars_seen >= MINIMUM_BARS_FOR_FULL_INDICATORS
                ):
                    ready.append(symbol)
                    continue
                if raw_errors:
                    classification = "source_data_invalid"
                elif bars_seen < MINIMUM_BARS_FOR_FULL_INDICATORS:
                    classification = "insufficient_history"
                else:
                    classification = "indicator_invalid"
                gaps.append({
                    "symbol": symbol,
                    "classification": classification,
                    "bars_seen": bars_seen,
                    "raw_errors": raw_errors,
                    "indicator_errors": indicator_errors,
                })

            denominator = len(symbols)
            raw_rate = len(raw_valid) / denominator
            ready_rate = len(ready) / denominator
            timeframes.append({
                "timeframe": timeframe,
                "expected_closed_bar_ts": expected_ts,
                "universe_symbols": denominator,
                "observed_exact_bar_rows": len(rows),
                "raw_ohlcv_valid_symbols": len(raw_valid),
                "raw_ohlcv_coverage_rate": round(raw_rate, 6),
                "raw_ohlcv_status": (
                    "PASSED" if raw_rate >= minimum_rate else "NOT_MET"),
                "analysis_ready_symbols": len(ready),
                "analysis_ready_rate": round(ready_rate, 6),
                "analysis_ready_status": (
                    "PASSED" if ready_rate >= minimum_rate else "NOT_MET"),
                "gap_counts": {
                    classification: sum(
                        1 for gap in gaps
                        if gap["classification"] == classification)
                    for classification in (
                        "source_data_invalid",
                        "insufficient_history",
                        "indicator_invalid",
                    )
                },
                "gaps": gaps,
            })
    finally:
        connection.close()

    data_passed = all(
        row["raw_ohlcv_status"] == "PASSED" for row in timeframes)
    readiness_passed = all(
        row["analysis_ready_status"] == "PASSED" for row in timeframes)
    return {
        "schema_version": 1,
        "artifact_type": "multitimeframe_closed_bar_coverage_audit",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "generated_at_cst": datetime.now(UTC).astimezone(CST).isoformat(),
        "evaluation_at_utc": evaluation_utc.isoformat(),
        "mode": "read_only",
        "latest_ticker_ts": latest_tick,
        "universe_symbols": len(symbols),
        "minimum_rate": minimum_rate,
        "minimum_bars_for_full_indicators": MINIMUM_BARS_FOR_FULL_INDICATORS,
        "contracts": {
            "universe": "latest ticker symbols ending -USDT-SWAP",
            "bar_selection": "exact latest fully closed UTC-aligned bar",
            "source_data_completeness": "valid exact OHLCV row / full universe",
            "analysis_readiness": (
                "valid exact OHLCV plus MA5/MA20/ATR14/RSI14/MACD histogram "
                "/ full universe"),
            "new_listing_semantics": (
                "insufficient history remains in denominator; indicators are "
                "never fabricated"),
        },
        "timeframes": timeframes,
        "data_completeness_status": "PASSED" if data_passed else "NOT_MET",
        "analysis_readiness_status": (
            "PASSED" if readiness_passed else "NOT_MET"),
        "status": "PASSED" if data_passed and readiness_passed else "NOT_MET",
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
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
    parser.add_argument("--market-db", type=Path, default=DEFAULT_MARKET_DB)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-rate", type=float, default=0.99)
    parser.add_argument("--as-of", help="ISO-8601 evaluation time; default now")
    args = parser.parse_args(argv)
    try:
        payload = audit_multitimeframe_coverage(
            args.market_db,
            minimum_rate=args.minimum_rate,
            now=_parse_ts(args.as_of) if args.as_of else None,
        )
        _atomic_json(args.json_out, payload)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "ok": True,
        "status": payload["status"],
        "data_completeness_status": payload["data_completeness_status"],
        "analysis_readiness_status": payload["analysis_readiness_status"],
        "universe_symbols": payload["universe_symbols"],
        "timeframes": [
            {
                "timeframe": row["timeframe"],
                "raw_ohlcv_coverage_rate": row["raw_ohlcv_coverage_rate"],
                "analysis_ready_rate": row["analysis_ready_rate"],
            }
            for row in payload["timeframes"]
        ],
        "json_out": str(args.json_out),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
