#!/usr/bin/env python3
"""Audit canonical weekly/monthly report generation and confirmed delivery.

Expected Monday and month-day-1 boundaries stay in the denominator.  A report
is complete only when the canonical Markdown exists and the independent
periodic validator accepts it; delivery requires a matching successful
``reviewer:<date>:<kind>`` mark in the QQ dedupe event log.  Historical debt and
pre-registered repair-era evidence are reported separately.  Production data
is opened read-only and only the explicit JSON evidence file is replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

import _acceptance_thresholds as thresholds
import validate_periodic_report


CST = timezone(timedelta(hours=8))
# 闸门数值由预注册激活边界解析（边界前 0.99、边界起 0.95）；常量只做迁移登记，
# 判定一律走 thresholds.coverage_target_rate(as_of)。前向分层起点不受影响。
TARGET_RATE = thresholds.COVERAGE_TARGET_RATE
LEGACY_TARGET_RATE = thresholds.COVERAGE_LEGACY_TARGET_RATE
DEFAULT_OUTPUT = Path(
    r".\reports\quality\periodic-report-completeness-audit.json")
DEFAULT_WEEKLY_START = date(2026, 8, 3)
DEFAULT_MONTHLY_START = date(2026, 8, 1)
DEFAULT_FORWARD_WEEKLY_START = date(2026, 8, 17)
DEFAULT_FORWARD_MONTHLY_START = date(2026, 9, 1)
DEFAULT_FORWARD_WEEKLY_MINIMUM = 12
DEFAULT_FORWARD_MONTHLY_MINIMUM = 6
REVIEWER_IDENTITY = re.compile(
    r"^reviewer:(\d{4}-\d{2}-\d{2}):(daily|weekly|monthly)(?::[\w.-]+)?$"
)
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD") from exc


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


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _boundaries(kind: str, start: date, as_of: datetime) -> list[date]:
    as_of = as_of.astimezone(CST)
    if kind == "weekly":
        if start.weekday() != 0:
            raise ValueError("weekly start must be Monday")
        step: Callable[[date], date] = lambda value: value + timedelta(days=7)
    elif kind == "monthly":
        if start.day != 1:
            raise ValueError("monthly start must be month day 1")
        step = _next_month
    else:
        raise ValueError("kind must be weekly or monthly")
    output: list[date] = []
    current = start
    while datetime.combine(current, time(8), CST) <= as_of:
        output.append(current)
        current = step(current)
    return output


def _delivery_evidence(
    path: Path,
) -> tuple[dict[str, set[str]], set[str], dict]:
    claims: dict[tuple[str, str], str] = {}
    sent_hashes: dict[str, set[str]] = {}
    marked_identities: set[str] = set()
    malformed = total = 0
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line in handle:
            if not line.strip():
                continue
            total += 1
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                malformed += 1
                continue
            if not isinstance(event, dict):
                malformed += 1
                continue
            key = str(event.get("dedupe_key") or "")
            match = REVIEWER_IDENTITY.fullmatch(key)
            if match is None:
                continue
            day, kind = match.groups()
            base = f"reviewer:{day}:{kind}"
            target = str(event.get("target") or "default")
            claim_key = (key, target)
            if event.get("event") == "claim":
                content_hash = str(event.get("content_hash") or "")
                if SHA256.fullmatch(content_hash):
                    claims[claim_key] = content_hash.lower()
                continue
            if event.get("event") != "mark" or event.get("status") != "sent":
                continue
            try:
                successful = int(event.get("exit_code")) == 0
            except (TypeError, ValueError):
                successful = False
            if not successful:
                continue
            marked_identities.add(base)
            claimed_hash = claims.get(claim_key)
            if claimed_hash:
                sent_hashes.setdefault(base, set()).add(claimed_hash)
    return sent_hashes, marked_identities, {
        "path": str(path),
        "nonblank_lines": total,
        "malformed_lines": malformed,
        "successful_reviewer_identities": len(marked_identities),
        "successful_reviewer_identities_with_claim_hash": len(sent_hashes),
        "integrity_status": "PASSED" if malformed == 0 else "NOT_MET",
    }


def _rows(
    *,
    kind: str,
    boundaries: list[date],
    reports_dir: Path,
    account_db: Path,
    live_trades_db: Path,
    ledger_db: Path,
    lessons_db: Path,
    sent_hashes: dict[str, set[str]],
    marked_identities: set[str],
    validator: Callable[..., dict],
) -> list[dict]:
    output: list[dict] = []
    for boundary in boundaries:
        day = boundary.isoformat()
        path = reports_dir / f"{kind}-{day}.md"
        identity = f"reviewer:{day}:{kind}"
        valid = False
        errors: list[str] = []
        checks: list[str] = []
        observed_key = None
        if not path.is_file():
            errors.append("artifact: missing canonical periodic report")
        else:
            try:
                result = validator(
                    kind=kind,
                    report_path=path,
                    account_db=account_db,
                    live_trades_db=live_trades_db,
                    ledger_db=ledger_db,
                    lessons_db=lessons_db,
                )
                valid = bool(result.get("ok"))
                errors = [str(item) for item in result.get("errors") or []]
                checks = [str(item) for item in result.get("checks") or []]
                observed_key = result.get("report_key")
            except Exception as exc:
                errors.append(f"validator: {type(exc).__name__}: {exc}")
        file_hash = None
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            file_hash = hashlib.sha256(
                content.encode("utf-8", errors="replace")).hexdigest()
        identity_sent = identity in marked_identities
        delivered = bool(
            file_hash and file_hash in sent_hashes.get(identity, set()))
        output.append({
            "date": day,
            "kind": kind,
            "path": str(path),
            "exists": path.is_file(),
            "valid": valid,
            "errors": errors,
            "checks": checks,
            "report_key": observed_key,
            "dedupe_identity": identity,
            "artifact_sha256": file_hash,
            "identity_marked_sent": identity_sent,
            "delivery_confirmed": delivered,
            "delivered_report_complete": valid and delivered,
        })
    return output


def _surface(
    rows: list[dict],
    *,
    minimum: int | None,
    delivery_integrity: bool,
    target_rate: float,
) -> dict:
    expected = len(rows)
    existing = sum(bool(row["exists"]) for row in rows)
    valid = sum(bool(row["valid"]) for row in rows)
    delivered = sum(bool(row["delivery_confirmed"]) for row in rows)
    complete_deliveries = sum(
        bool(row["delivered_report_complete"]) for row in rows)
    report_rate = valid / expected if expected else 0.0
    delivery_rate = delivered / expected if expected else 0.0
    complete_delivery_rate = complete_deliveries / expected if expected else 0.0
    enough = expected > 0 and (minimum is None or expected >= minimum)
    if not enough:
        status = "INSUFFICIENT_EVIDENCE"
    elif (
        delivery_integrity
        and report_rate >= target_rate
        and delivery_rate >= target_rate
        and complete_delivery_rate >= target_rate
    ):
        status = "PASSED"
    else:
        status = "NOT_MET"
    return {
        "minimum_expected": minimum,
        "expected": expected,
        "existing": existing,
        "valid": valid,
        "invalid": expected - valid,
        "delivery_confirmed": delivered,
        "delivery_unconfirmed": expected - delivered,
        "delivered_report_complete": complete_deliveries,
        "report_completeness_rate": report_rate,
        "delivery_confirmation_rate": delivery_rate,
        "delivered_report_completeness_rate": complete_delivery_rate,
        "target_rate": target_rate,
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics({
            "report_completeness_rate": report_rate,
            "delivery_confirmation_rate": delivery_rate,
            "delivered_report_completeness_rate": complete_delivery_rate,
        }),
        "status": status,
        "rows": rows,
    }


def _combined_status(surfaces: list[dict], *, pending_label: bool) -> str:
    statuses = [str(surface["status"]) for surface in surfaces]
    if "NOT_MET" in statuses:
        return "NOT_MET"
    if statuses and all(status == "PASSED" for status in statuses):
        return "PASSED"
    return "PENDING_FORWARD_EVIDENCE" if pending_label else "INSUFFICIENT_EVIDENCE"


def audit_periodic_reports(
    *,
    as_of: datetime,
    weekly_start: date,
    monthly_start: date,
    forward_weekly_start: date,
    forward_monthly_start: date,
    forward_weekly_minimum: int,
    forward_monthly_minimum: int,
    weekly_dir: Path,
    monthly_dir: Path,
    account_db: Path,
    live_trades_db: Path,
    ledger_db: Path,
    lessons_db: Path,
    event_log: Path,
    validator: Callable[..., dict] = validate_periodic_report.validate_report,
) -> dict:
    sent_hashes, marked_identities, delivery_source = _delivery_evidence(
        event_log)
    integrity = delivery_source["integrity_status"] == "PASSED"
    target_rate = thresholds.coverage_target_rate(as_of)

    def build(kind: str, start: date, minimum: int | None) -> dict:
        boundaries = _boundaries(kind, start, as_of)
        return _surface(
            _rows(
                kind=kind,
                boundaries=boundaries,
                reports_dir=weekly_dir if kind == "weekly" else monthly_dir,
                account_db=account_db,
                live_trades_db=live_trades_db,
                ledger_db=ledger_db,
                lessons_db=lessons_db,
                sent_hashes=sent_hashes,
                marked_identities=marked_identities,
                validator=validator,
            ),
            minimum=minimum,
            delivery_integrity=integrity,
            target_rate=target_rate,
        )

    historical_weekly = build("weekly", weekly_start, None)
    historical_monthly = build("monthly", monthly_start, None)
    forward_weekly = build(
        "weekly", forward_weekly_start, forward_weekly_minimum)
    forward_monthly = build(
        "monthly", forward_monthly_start, forward_monthly_minimum)
    historical = {
        "weekly": historical_weekly,
        "monthly": historical_monthly,
        "status": _combined_status(
            [historical_weekly, historical_monthly], pending_label=False),
    }
    forward = {
        "weekly_start": forward_weekly_start.isoformat(),
        "monthly_start": forward_monthly_start.isoformat(),
        "weekly": forward_weekly,
        "monthly": forward_monthly,
        "status": _combined_status(
            [forward_weekly, forward_monthly], pending_label=True),
    }
    return {
        "schema_version": 1,
        "artifact_type": "periodic_report_and_delivery_completeness_audit",
        "generated_at_cst": datetime.now(CST).isoformat(),
        "as_of_cst": as_of.astimezone(CST).isoformat(),
        "mode": "read_only_business_data",
        "target_rate": target_rate,
        "target_rate_migration": thresholds.coverage_migration_facts(as_of),
        "metric_definition": (
            "complete = canonical weekly/monthly Markdown passes the "
            "independent validator; delivered complete additionally requires "
            "a matching successful reviewer dedupe identity"
        ),
        "delivery_evidence": delivery_source,
        "historical": historical,
        "forward_after_remediation": forward,
        "overall_status": forward["status"],
        "safety": {
            "auto_resend": False,
            "historical_backfill": False,
            "production_database_writes": 0,
            "production_report_mutation": False,
            "production_order_authorized": False,
            "orders_placed": 0,
        },
    }


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
    parser.add_argument("--as-of")
    parser.add_argument("--weekly-start", type=_parse_day,
                        default=DEFAULT_WEEKLY_START)
    parser.add_argument("--monthly-start", type=_parse_day,
                        default=DEFAULT_MONTHLY_START)
    parser.add_argument("--forward-weekly-start", type=_parse_day,
                        default=DEFAULT_FORWARD_WEEKLY_START)
    parser.add_argument("--forward-monthly-start", type=_parse_day,
                        default=DEFAULT_FORWARD_MONTHLY_START)
    parser.add_argument("--forward-weekly-minimum", type=int,
                        default=DEFAULT_FORWARD_WEEKLY_MINIMUM)
    parser.add_argument("--forward-monthly-minimum", type=int,
                        default=DEFAULT_FORWARD_MONTHLY_MINIMUM)
    parser.add_argument("--weekly-dir", type=Path,
                        default=Path(r".\reports\weekly"))
    parser.add_argument("--monthly-dir", type=Path,
                        default=Path(r".\reports\monthly"))
    parser.add_argument("--account-db", type=Path,
                        default=Path(r".\db\account.db"))
    parser.add_argument("--live-trades-db", type=Path,
                        default=Path(r".\db\live_trades.db"))
    parser.add_argument("--ledger-db", type=Path,
                        default=Path(r".\db\ledger.db"))
    parser.add_argument("--lessons-db", type=Path,
                        default=Path(r".\db\lessons.db"))
    parser.add_argument("--event-log", type=Path,
                        default=Path(r".\logs\push\qq_push_dedupe.jsonl"))
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.forward_weekly_minimum <= 0 or args.forward_monthly_minimum <= 0:
        parser.error("forward minimums must be positive")
    required = (
        args.weekly_dir, args.monthly_dir, args.account_db,
        args.live_trades_db, args.ledger_db, args.lessons_db, args.event_log,
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print(json.dumps({"ok": False, "error": "missing input",
                          "paths": missing}, ensure_ascii=False))
        return 2
    try:
        payload = audit_periodic_reports(
            as_of=_parse_cst(args.as_of) if args.as_of else datetime.now(CST),
            weekly_start=args.weekly_start,
            monthly_start=args.monthly_start,
            forward_weekly_start=args.forward_weekly_start,
            forward_monthly_start=args.forward_monthly_start,
            forward_weekly_minimum=args.forward_weekly_minimum,
            forward_monthly_minimum=args.forward_monthly_minimum,
            weekly_dir=args.weekly_dir,
            monthly_dir=args.monthly_dir,
            account_db=args.account_db,
            live_trades_db=args.live_trades_db,
            ledger_db=args.ledger_db,
            lessons_db=args.lessons_db,
            event_log=args.event_log,
        )
        _atomic_json(args.json_out, payload)
    except (OSError, ValueError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "ok": True,
        "historical_status": payload["historical"]["status"],
        "forward_status": payload["forward_after_remediation"]["status"],
        "overall_status": payload["overall_status"],
        "json_out": str(args.json_out),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
