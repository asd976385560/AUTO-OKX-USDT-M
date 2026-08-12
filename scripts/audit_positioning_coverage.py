# -*- coding: utf-8 -*-
"""Audit latest official contract positioning batch against the live universe.

The audit is read-only.  It validates one exact ``collected_ts`` batch from
``market_positioning`` against the latest USDT linear SWAP ticker universe,
including missing/extra symbols, duplicates, ratio algebra and source times.
Only the explicit JSON evidence file is atomically replaced.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))
DEFAULT_MARKET_DB = Path(r"./db/market.db")
DEFAULT_OUTPUT = Path(
    r"./reports/quality/positioning-coverage-audit.json")
DEFAULT_SOURCE = "okx_rest_contract_long_short_ratio"


def _ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_ts(value: str) -> datetime:
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ratio_errors(row: sqlite3.Row) -> list[str]:
    errors: list[str] = []
    values: dict[str, float] = {}
    for name in ("long_ratio", "short_ratio", "long_short_ratio"):
        try:
            value = float(row[name])
        except (TypeError, ValueError):
            errors.append(f"{name}_missing_or_non_numeric")
            continue
        if not math.isfinite(value):
            errors.append(f"{name}_non_finite")
            continue
        values[name] = value
    if len(values) != 3:
        return errors
    if not 0 <= values["long_ratio"] <= 1:
        errors.append("long_ratio_out_of_range")
    if not 0 <= values["short_ratio"] <= 1:
        errors.append("short_ratio_out_of_range")
    if values["long_short_ratio"] < 0:
        errors.append("long_short_ratio_negative")
    if abs(values["long_ratio"] + values["short_ratio"] - 1.0) > 1e-6:
        errors.append("account_shares_do_not_sum_to_one")
    if values["short_ratio"] > 0:
        derived = values["long_ratio"] / values["short_ratio"]
        tolerance = max(1e-6, abs(values["long_short_ratio"]) * 1e-6)
        if abs(derived - values["long_short_ratio"]) > tolerance:
            errors.append("long_short_ratio_derivation_mismatch")
    return errors


def audit_positioning_coverage(
    market_db: Path,
    *,
    minimum_rate: float = 0.99,
    source: str = DEFAULT_SOURCE,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not 0 < minimum_rate <= 1:
        raise ValueError("minimum_rate must be in (0,1]")
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    connection = _ro(market_db)
    try:
        latest_tick = connection.execute(
            "SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
        universe_rows = connection.execute(
            "SELECT DISTINCT symbol FROM tick_snapshots "
            "WHERE ts=? AND symbol LIKE '%-USDT-SWAP' ORDER BY symbol",
            (latest_tick,),
        ).fetchall()
        universe = {str(row[0]) for row in universe_rows}
        latest_collected = connection.execute(
            "SELECT MAX(collected_ts) FROM market_positioning "
            "WHERE source=? AND timeframe='1H'",
            (source,),
        ).fetchone()[0]
        rows = [] if latest_collected is None else connection.execute(
            "SELECT ts,collected_ts,cycle_id,symbol,timeframe,long_ratio,"
            "short_ratio,long_short_ratio,source FROM market_positioning "
            "WHERE source=? AND timeframe='1H' AND collected_ts=? "
            "ORDER BY symbol",
            (source, latest_collected),
        ).fetchall()
    finally:
        connection.close()

    symbol_counts: dict[str, int] = {}
    invalid: list[dict[str, Any]] = []
    source_times: list[datetime] = []
    for row in rows:
        symbol = str(row["symbol"])
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        row_errors = _ratio_errors(row)
        try:
            source_times.append(_parse_ts(str(row["ts"])))
        except (TypeError, ValueError):
            row_errors.append("source_ts_invalid")
        if row_errors:
            invalid.append({"symbol": symbol, "errors": row_errors})

    observed = set(symbol_counts)
    duplicate_symbols = sorted(
        symbol for symbol, count in symbol_counts.items() if count != 1)
    invalid_symbols = {item["symbol"] for item in invalid}
    missing = sorted(universe - observed)
    extra = sorted(observed - universe)
    valid_symbols = (
        (universe & observed) - invalid_symbols - set(duplicate_symbols)
    )
    denominator = len(universe)
    coverage_rate = len(valid_symbols) / denominator if denominator else 0.0
    if latest_collected is None:
        status = "NO_DATA"
    elif (
        coverage_rate >= minimum_rate
        and not invalid
        and not duplicate_symbols
        and not extra
    ):
        status = "PASSED"
    else:
        status = "NOT_MET"

    min_source = min(source_times) if source_times else None
    max_source = max(source_times) if source_times else None
    return {
        "schema_version": 1,
        "artifact_type": "positioning_coverage_audit",
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_cst": now_utc.astimezone(CST).isoformat(),
        "mode": "read_only",
        "source": source,
        "timeframe": "1H",
        "latest_ticker_ts": latest_tick,
        "latest_batch_collected_ts": latest_collected,
        "source_ts_min": min_source.isoformat() if min_source else None,
        "source_ts_max": max_source.isoformat() if max_source else None,
        "maximum_source_age_minutes": (
            (now_utc - max_source).total_seconds() / 60.0
            if max_source else None
        ),
        "universe_symbols": denominator,
        "batch_rows": len(rows),
        "observed_unique_symbols": len(observed),
        "valid_symbols": len(valid_symbols),
        "coverage_rate": round(coverage_rate, 6),
        "minimum_rate": minimum_rate,
        "missing_symbols": missing,
        "extra_symbols": extra,
        "duplicate_symbols": duplicate_symbols,
        "invalid_rows": invalid,
        "status": status,
        "contracts": {
            "universe": "latest ticker symbols ending -USDT-SWAP",
            "availability_clock": "network batch completion collected_ts",
            "long_short_ratio": "account-count ratio, not position notional",
        },
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
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    args = parser.parse_args(argv)
    try:
        payload = audit_positioning_coverage(
            args.market_db,
            minimum_rate=args.minimum_rate,
            source=args.source,
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
        "coverage_rate": payload["coverage_rate"],
        "valid_symbols": payload["valid_symbols"],
        "universe_symbols": payload["universe_symbols"],
        "batch_rows": payload["batch_rows"],
        "json_out": str(args.json_out),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
