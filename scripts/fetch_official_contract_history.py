#!/usr/bin/env python3
"""Fetch bounded OKX contract-statistics history into an isolated research DB.

The three public Trading Statistics endpoints are queried with explicit
``begin``/``end`` bounds and a maximum of 100 rows per request.  Outputs are
for retrospective research only: the script rejects every target under the
production ``db`` directory, never calls an order API, and never mutates a
production database or trading threshold.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from _okx_http import (
    fetch_contract_long_short_ratios_batch_sync,
    fetch_contract_open_interest_history_batch_sync,
    fetch_contract_taker_volumes_batch_sync,
)


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB_ROOT = (ROOT / "db").resolve()
PERIOD_SECONDS = {"15m": 900, "1H": 3_600, "4H": 14_400}
ENDPOINTS = (
    "open_interest",
    "taker_volume",
    "long_short_ratio",
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include an explicit timezone")
    return parsed.astimezone(UTC)


def _unix_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def _ensure_isolated_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.suffix.lower() != ".db":
        raise ValueError("output database must use a .db suffix")
    if resolved == PRODUCTION_DB_ROOT or PRODUCTION_DB_ROOT in resolved.parents:
        raise ValueError("output database must be outside the production db directory")
    return resolved


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
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
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _symbols_from_csv(path: Path, column: str = "symbol") -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or column not in reader.fieldnames:
            raise ValueError(f"CSV is missing required column: {column}")
        return [str(row.get(column) or "").strip() for row in reader]


def load_symbols(
    *,
    symbols: Iterable[str] = (),
    symbols_file: Path | None = None,
    panel_csv: Path | None = None,
    max_symbols: int | None = None,
) -> list[str]:
    values = [str(value).strip() for value in symbols]
    if symbols_file is not None:
        values.extend(
            line.strip()
            for line in symbols_file.read_text(encoding="utf-8-sig").splitlines()
        )
    if panel_csv is not None:
        values.extend(_symbols_from_csv(panel_csv))
    normalized = sorted({value for value in values if value})
    invalid = [
        symbol
        for symbol in normalized
        if not symbol.endswith("-SWAP") or len(symbol) > 80
    ]
    if invalid:
        raise ValueError("invalid SWAP symbols: " + ",".join(invalid[:10]))
    if max_symbols is not None:
        if max_symbols <= 0:
            raise ValueError("max_symbols must be positive")
        normalized = normalized[:max_symbols]
    if not normalized:
        raise ValueError("at least one SWAP symbol is required")
    return normalized


def build_windows(
    start: datetime,
    end: datetime,
    *,
    period: str,
    window_bars: int,
) -> list[tuple[datetime, datetime]]:
    if period not in PERIOD_SECONDS:
        raise ValueError(f"unsupported research period: {period}")
    if not 1 <= int(window_bars) <= 99:
        raise ValueError("window_bars must be between 1 and 99")
    if start >= end:
        raise ValueError("start must be earlier than end")
    width = timedelta(seconds=PERIOD_SECONDS[period] * int(window_bars))
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        window_end = min(end, cursor + width)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows


def _number(value: Any, *, nonnegative: bool = True) -> float:
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError("invalid numeric value")
    return result


def _rows_in_window(
    rows: Any,
    *,
    start_ms: int,
    end_ms: int,
    parser: Callable[[list[Any]], tuple[Any, ...]],
) -> tuple[list[tuple[Any, ...]], int, int, int]:
    if not isinstance(rows, list):
        return [], 0, 1, 0
    valid: list[tuple[Any, ...]] = []
    invalid = 0
    outside_window = 0
    seen: set[int] = set()
    for raw in rows:
        try:
            if not isinstance(raw, list):
                raise ValueError("row must be a list")
            parsed = parser(raw)
            ts_ms = int(parsed[0])
            if not start_ms <= ts_ms < end_ms:
                outside_window += 1
                continue
            if ts_ms in seen:
                raise ValueError("duplicated timestamp")
            seen.add(ts_ms)
            valid.append((*parsed, json.dumps(raw, ensure_ascii=False)))
        except (TypeError, ValueError, OverflowError):
            invalid += 1
    valid.sort(key=lambda row: int(row[0]))
    return valid, len(rows), invalid, outside_window


def _parse_oi(row: list[Any]) -> tuple[int, float, float, float]:
    if len(row) < 4:
        raise ValueError("open-interest row is short")
    return (
        int(row[0]),
        _number(row[1]),
        _number(row[2]),
        _number(row[3]),
    )


def _parse_taker(row: list[Any]) -> tuple[int, float, float]:
    if len(row) < 3:
        raise ValueError("taker-volume row is short")
    return int(row[0]), _number(row[1]), _number(row[2])


def _parse_ratio(row: list[Any]) -> tuple[int, float]:
    if len(row) < 2:
        raise ValueError("long-short-ratio row is short")
    value = _number(row[1], nonnegative=False)
    if value <= 0:
        raise ValueError("long-short ratio must be positive")
    return int(row[0]), value


def _initialize(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS history_runs(
            run_id TEXT PRIMARY KEY,
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT,
            period TEXT NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            symbol_count INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS open_interest(
            symbol TEXT NOT NULL,
            period TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            oi_contracts REAL NOT NULL,
            oi_ccy REAL NOT NULL,
            oi_usd REAL NOT NULL,
            raw_json TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            PRIMARY KEY(symbol,period,ts_ms)
        );
        CREATE TABLE IF NOT EXISTS taker_volume(
            symbol TEXT NOT NULL,
            period TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            sell_volume_usd REAL NOT NULL,
            buy_volume_usd REAL NOT NULL,
            unit TEXT NOT NULL CHECK(unit='2'),
            raw_json TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            PRIMARY KEY(symbol,period,ts_ms)
        );
        CREATE TABLE IF NOT EXISTS long_short_ratio(
            symbol TEXT NOT NULL,
            period TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            account_long_short_ratio REAL NOT NULL,
            raw_json TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            PRIMARY KEY(symbol,period,ts_ms)
        );
        CREATE TABLE IF NOT EXISTS request_audit(
            run_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            symbol TEXT NOT NULL,
            period TEXT NOT NULL,
            window_start_ms INTEGER NOT NULL,
            window_end_ms INTEGER NOT NULL,
            request_begin_ms INTEGER NOT NULL,
            request_end_ms INTEGER NOT NULL,
            transport_ok INTEGER NOT NULL CHECK(transport_ok IN (0,1)),
            raw_rows_received INTEGER NOT NULL,
            valid_rows_in_window INTEGER NOT NULL,
            invalid_rows INTEGER NOT NULL,
            filtered_outside_window_rows INTEGER NOT NULL,
            error_type TEXT,
            error TEXT,
            fetched_at_utc TEXT NOT NULL,
            PRIMARY KEY(run_id,endpoint,symbol,window_start_ms,window_end_ms),
            FOREIGN KEY(run_id) REFERENCES history_runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_oi_period_ts
          ON open_interest(period,ts_ms,symbol);
        CREATE INDEX IF NOT EXISTS idx_taker_period_ts
          ON taker_volume(period,ts_ms,symbol);
        CREATE INDEX IF NOT EXISTS idx_ratio_period_ts
          ON long_short_ratio(period,ts_ms,symbol);
        """
    )


def _write_source_rows(
    con: sqlite3.Connection,
    endpoint: str,
    symbol: str,
    period: str,
    rows: list[tuple[Any, ...]],
    fetched_at: str,
) -> None:
    if endpoint == "open_interest":
        con.executemany(
            "INSERT OR REPLACE INTO open_interest VALUES(?,?,?,?,?,?,?,?)",
            [
                (symbol, period, *row[:-1], row[-1], fetched_at)
                for row in rows
            ],
        )
    elif endpoint == "taker_volume":
        con.executemany(
            "INSERT OR REPLACE INTO taker_volume VALUES(?,?,?,?,?,?,?,?)",
            [
                (symbol, period, *row[:-1], "2", row[-1], fetched_at)
                for row in rows
            ],
        )
    elif endpoint == "long_short_ratio":
        con.executemany(
            "INSERT OR REPLACE INTO long_short_ratio VALUES(?,?,?,?,?,?)",
            [
                (symbol, period, *row[:-1], row[-1], fetched_at)
                for row in rows
            ],
        )
    else:
        raise ValueError(f"unknown endpoint: {endpoint}")


def _source_counts(
    con: sqlite3.Connection,
    *,
    period: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for endpoint, table in (
        ("open_interest", "open_interest"),
        ("taker_volume", "taker_volume"),
        ("long_short_ratio", "long_short_ratio"),
    ):
        counts[endpoint] = int(con.execute(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE period=? AND ts_ms>=? AND ts_ms<?",
            (period, start_ms, end_ms),
        ).fetchone()[0])
    counts["common"] = int(con.execute(
        "SELECT COUNT(*) FROM open_interest oi "
        "JOIN taker_volume tv USING(symbol,period,ts_ms) "
        "JOIN long_short_ratio lr USING(symbol,period,ts_ms) "
        "WHERE oi.period=? AND oi.ts_ms>=? AND oi.ts_ms<?",
        (period, start_ms, end_ms),
    ).fetchone()[0])
    return counts


def fetch_history(
    *,
    output_db: Path,
    symbols: list[str],
    start: datetime,
    end: datetime,
    period: str = "1H",
    window_bars: int = 96,
    batch_timeout_s: float = 180.0,
    request_retries: int = 2,
    manifest_output: Path | None = None,
    now_utc: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    output_db = _ensure_isolated_output(output_db)
    if not symbols:
        raise ValueError("symbols must not be empty")
    if batch_timeout_s <= 0:
        raise ValueError("batch_timeout_s must be positive")
    if request_retries < 0:
        raise ValueError("request_retries must be nonnegative")
    clock = now_utc or (lambda: datetime.now(UTC))
    started = clock().astimezone(UTC)
    if end > started + timedelta(minutes=1):
        raise ValueError("end must not be in the future")
    windows = build_windows(
        start, end, period=period, window_bars=window_bars)
    # Newest windows first: a bounded interrupted run still retains the most
    # relevant evidence and can resume through primary-key upserts.
    windows = list(reversed(windows))
    output_db.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = (
        manifest_output.resolve()
        if manifest_output is not None
        else output_db.with_suffix(".manifest.json")
    )
    run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    start_ms = _unix_ms(start)
    end_ms = _unix_ms(end)
    con = sqlite3.connect(output_db, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    _initialize(con)
    con.execute(
        "INSERT INTO history_runs VALUES(?,?,?,?,?,?,?,?)",
        (
            run_id, _iso(started), None, period, start_ms, end_ms,
            len(symbols), "running",
        ),
    )
    con.commit()

    fetchers = {
        "open_interest": (
            fetch_contract_open_interest_history_batch_sync, _parse_oi,
        ),
        "taker_volume": (
            fetch_contract_taker_volumes_batch_sync, _parse_taker,
        ),
        "long_short_ratio": (
            fetch_contract_long_short_ratios_batch_sync, _parse_ratio,
        ),
    }
    total_requests = 0
    failed_requests = 0
    invalid_rows_total = 0
    outside_window_rows_total = 0
    try:
        for window_start, window_end in windows:
            window_start_ms = _unix_ms(window_start)
            window_end_ms = _unix_ms(window_end)
            # API bounds are strict in their documented direction.  One
            # millisecond below the lower bound includes an exactly aligned
            # first bucket; local validation still enforces [start,end).
            request_begin_ms = max(0, window_start_ms - 1)
            request_end_ms = window_end_ms
            for endpoint in ENDPOINTS:
                fetcher, parser = fetchers[endpoint]
                outcomes: dict[str, dict] = {}
                kwargs: dict[str, Any] = {
                    "period": period,
                    "limit": 100,
                    "batch_timeout_s": batch_timeout_s,
                    "begin_ms": request_begin_ms,
                    "end_ms": request_end_ms,
                    "request_retries": request_retries,
                    "outcomes": outcomes,
                }
                if endpoint == "taker_volume":
                    kwargs["unit"] = "2"
                payloads = fetcher(symbols, **kwargs)
                fetched_at = _iso(clock().astimezone(UTC))
                for symbol in symbols:
                    outcome = outcomes.get(symbol) or {
                        "ok": False,
                        "error_type": "MissingOutcome",
                        "error": "batch did not return a transport outcome",
                    }
                    (
                        parsed,
                        raw_count,
                        invalid_count,
                        outside_window_count,
                    ) = _rows_in_window(
                        payloads.get(symbol, []),
                        start_ms=window_start_ms,
                        end_ms=window_end_ms,
                        parser=parser,
                    )
                    total_requests += 1
                    failed_requests += int(not bool(outcome.get("ok")))
                    invalid_rows_total += invalid_count
                    outside_window_rows_total += outside_window_count
                    _write_source_rows(
                        con, endpoint, symbol, period, parsed, fetched_at)
                    con.execute(
                        "INSERT INTO request_audit VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            run_id, endpoint, symbol, period,
                            window_start_ms, window_end_ms,
                            request_begin_ms, request_end_ms,
                            int(bool(outcome.get("ok"))),
                            raw_count, len(parsed), invalid_count,
                            outside_window_count,
                            outcome.get("error_type"),
                            str(outcome.get("error") or "")[:500] or None,
                            fetched_at,
                        ),
                    )
                con.commit()
        completed = clock().astimezone(UTC)
        status = "complete" if failed_requests == 0 else "partial_transport_failure"
        con.execute(
            "UPDATE history_runs SET completed_at_utc=?,status=? WHERE run_id=?",
            (_iso(completed), status, run_id),
        )
        con.commit()
        counts = _source_counts(
            con, period=period, start_ms=start_ms, end_ms=end_ms)
    except BaseException:
        completed = clock().astimezone(UTC)
        con.execute(
            "UPDATE history_runs SET completed_at_utc=?,status='failed' "
            "WHERE run_id=?",
            (_iso(completed), run_id),
        )
        con.commit()
        raise
    finally:
        con.close()

    period_ms = PERIOD_SECONDS[period] * 1000
    expected_bars = max(0, math.ceil((end_ms - start_ms) / period_ms))
    expected_rows = expected_bars * len(symbols)
    coverage = {
        key: (value / expected_rows if expected_rows else None)
        for key, value in counts.items()
    }
    transport_rate = (
        (total_requests - failed_requests) / total_requests
        if total_requests else 0.0
    )
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "artifact_type": "isolated_okx_official_contract_history",
        "run_id": run_id,
        "generated_at_utc": _iso(completed),
        "status": status,
        "mode": "retrospective_research_only",
        "source": "OKX public Trading Statistics REST",
        "official_endpoints": {
            "open_interest": "/api/v5/rubik/stat/contracts/open-interest-history",
            "taker_volume": "/api/v5/rubik/stat/taker-volume-contract",
            "long_short_ratio": (
                "/api/v5/rubik/stat/contracts/"
                "long-short-account-ratio-contract"
            ),
        },
        "request_contract": {
            "period": period,
            "start_utc": _iso(start),
            "end_exclusive_utc": _iso(end),
            "window_bars": window_bars,
            "maximum_rows_per_request": 100,
            "documented_latest_entry_limit": 1440,
            "taker_volume_unit": "2 (U/USD notional)",
            "bounded_begin_end_on_every_request": True,
        },
        "symbols": {
            "count": len(symbols),
            "values": symbols,
        },
        "requests": {
            "windows": len(windows),
            "total": total_requests,
            "transport_failed": failed_requests,
            "transport_success_rate": transport_rate,
            "invalid_rows": invalid_rows_total,
            "outside_window_rows_filtered": outside_window_rows_total,
        },
        "rows": counts,
        "listing_unadjusted_expected_grid": {
            "bars_per_symbol": expected_bars,
            "rows": expected_rows,
            "coverage_rate": coverage,
            "semantics": (
                "Denominator is every requested symbol-period bucket; newly "
                "listed instruments are not removed."
            ),
        },
        "research_gate": {
            "transport_complete": failed_requests == 0,
            "invalid_rows_zero": invalid_rows_total == 0,
            "common_source_coverage_at_least_99pct": (
                coverage.get("common") is not None
                and coverage["common"] >= 0.99
            ),
            "ready_for_retrospective_feature_diagnostics": (
                failed_requests == 0
                and invalid_rows_total == 0
                and coverage.get("common") is not None
                and coverage["common"] >= 0.99
            ),
            "not_forward_evidence": True,
            "does_not_authorize_production_change": True,
        },
        "safety": {
            "output_db": str(output_db),
            "production_database_writes": 0,
            "production_threshold_changes": 0,
            "order_calls": 0,
        },
    }
    _atomic_json(manifest_path, manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument(
        "--symbols",
        default="",
        help="comma-separated SWAP instrument IDs",
    )
    parser.add_argument("--symbols-file", type=Path)
    parser.add_argument("--panel-csv", type=Path)
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--period", choices=tuple(PERIOD_SECONDS), default="1H")
    parser.add_argument("--window-bars", type=int, default=96)
    parser.add_argument("--batch-timeout-s", type=float, default=180.0)
    parser.add_argument("--request-retries", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = load_symbols(
        symbols=(value for value in args.symbols.split(",")),
        symbols_file=args.symbols_file,
        panel_csv=args.panel_csv,
        max_symbols=args.max_symbols,
    )
    manifest = fetch_history(
        output_db=args.output_db,
        manifest_output=args.manifest_output,
        symbols=symbols,
        start=_parse_utc(args.start),
        end=_parse_utc(args.end),
        period=args.period,
        window_bars=args.window_bars,
        batch_timeout_s=args.batch_timeout_s,
        request_retries=args.request_retries,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
