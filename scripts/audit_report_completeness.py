# -*- coding: utf-8 -*-
"""Audit daily report completeness with the canonical deterministic validator.

This is a read-only monitoring surface.  A report counts as complete only when
its expected file exists and ``validate_daily_report`` accepts its structure,
fixed 24-hour window, database facts, reconciliation state, revision metadata,
and risk-reject facts.  A failed 99% gate is data, not a process failure: the
script exits 0 after a successful audit and records ``NOT_MET`` in the JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import validate_daily_report


CST = timezone(timedelta(hours=8))
TARGET_RATE = 0.99


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def _days_inclusive(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end date must not be earlier than start date")
    return [start + timedelta(days=offset)
            for offset in range((end - start).days + 1)]


def audit_daily_reports(
    *,
    start: date,
    end: date,
    reports_dir: Path,
    account_db: Path,
    live_trades_db: Path,
    ledger_db: Path,
    validator: Callable[..., dict] = validate_daily_report.validate_report,
    evaluated_at: str | None = None,
) -> dict:
    """Return one auditable row per expected day and a strict 99% gate."""
    rows = []
    for day in _days_inclusive(start, end):
        path = reports_dir / f"daily-{day.isoformat()}.md"
        if not path.exists():
            rows.append({
                "date": day.isoformat(),
                "path": str(path),
                "exists": False,
                "valid": False,
                "errors": ["artifact: missing daily report"],
                "checks": [],
            })
            continue
        try:
            result = validator(
                report_path=path,
                account_db=account_db,
                live_trades_db=live_trades_db,
                ledger_db=ledger_db,
            )
            rows.append({
                "date": day.isoformat(),
                "path": str(path),
                "exists": True,
                "valid": bool(result.get("ok")),
                "report_ts": result.get("report_ts"),
                "errors": list(result.get("errors") or []),
                "checks": list(result.get("checks") or []),
                "auto_send": False,
            })
        except Exception as exc:  # keep one bad artifact from hiding the rest
            rows.append({
                "date": day.isoformat(),
                "path": str(path),
                "exists": True,
                "valid": False,
                "errors": [f"validator: {type(exc).__name__}: {exc}"],
                "checks": [],
                "auto_send": False,
            })

    expected = len(rows)
    existing = sum(bool(row["exists"]) for row in rows)
    valid = sum(bool(row["valid"]) for row in rows)
    rate = valid / expected if expected else 0.0
    return {
        "schema_version": 1,
        "evaluated_at_cst": evaluated_at or datetime.now(CST).strftime(
            "%Y-%m-%d %H:%M:%S"),
        "window": {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "end_inclusive": True,
            "expected_days": expected,
        },
        "metric_definition": (
            "complete = expected daily artifact exists and passes the canonical "
            "deterministic daily report validator"
        ),
        "target_rate": TARGET_RATE,
        "expected": expected,
        "existing": existing,
        "valid": valid,
        "invalid": expected - valid,
        "completeness_rate": rate,
        "status": "PASSED" if rate >= TARGET_RATE else "NOT_MET",
        "rows": rows,
        "auto_send": False,
        "database_write": False,
        "production_order_authorized": False,
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
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
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="read-only rolling daily-report completeness audit")
    dates = parser.add_mutually_exclusive_group()
    dates.add_argument(
        "--days", type=int, default=30,
        help="completed calendar days ending yesterday (default: 30)")
    dates.add_argument("--start", type=_parse_day)
    parser.add_argument("--end", type=_parse_day)
    parser.add_argument(
        "--reports-dir", default=r"./reports/daily-reports")
    parser.add_argument("--account-db", default=r"./db/account.db")
    parser.add_argument(
        "--live-trades-db", default=r"./db/live_trades.db")
    parser.add_argument("--ledger-db", default=r"./db/ledger.db")
    parser.add_argument(
        "--json-out",
        default=r"./reports/quality/daily-report-completeness.json",
    )
    args = parser.parse_args(argv)
    if args.start is None and args.end is not None:
        parser.error("--end requires --start")
    if args.days is not None and args.days <= 0:
        parser.error("--days must be positive")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.start is not None:
        start = args.start
        end = args.end or (datetime.now(CST).date() - timedelta(days=1))
    else:
        end = datetime.now(CST).date() - timedelta(days=1)
        start = end - timedelta(days=args.days - 1)

    required = {
        "reports_dir": Path(args.reports_dir),
        "account_db": Path(args.account_db),
        "live_trades_db": Path(args.live_trades_db),
        "ledger_db": Path(args.ledger_db),
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        print(json.dumps({
            "ok": False,
            "error": "missing input",
            "paths": missing,
        }, ensure_ascii=False), file=sys.stderr)
        return 2

    result = audit_daily_reports(start=start, end=end, **required)
    if args.json_out:
        _atomic_write_json(Path(args.json_out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
