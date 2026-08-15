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
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import _acceptance_thresholds as thresholds
import validate_daily_report


CST = timezone(timedelta(hours=8))
# 闸门数值由预注册激活边界解析（边界前 0.99、边界起 0.95）；常量只做迁移登记，
# 判定一律走 thresholds.coverage_target_rate(evaluated_at)。
TARGET_RATE = thresholds.COVERAGE_TARGET_RATE
LEGACY_TARGET_RATE = thresholds.COVERAGE_LEGACY_TARGET_RATE
DEFAULT_FORWARD_START = date(2026, 8, 13)
DEFAULT_FORWARD_MINIMUM_DAYS = 30
REVIEWER_DAILY_IDENTITY = re.compile(
    r"^reviewer:(\d{4}-\d{2}-\d{2}):daily(?::[\w.-]+)?$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


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


def _delivery_evidence(
    path: Path,
) -> tuple[dict[str, set[str]], set[str], dict]:
    claims: dict[tuple[str, str], str] = {}
    sent_hashes: dict[str, set[str]] = {}
    marked_identities: set[str] = set()
    total = malformed = 0
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
            raw_key = str(event.get("dedupe_key") or "")
            match = REVIEWER_DAILY_IDENTITY.fullmatch(raw_key)
            if match is None:
                continue
            identity = f"reviewer:{match.group(1)}:daily"
            target = str(event.get("target") or "default")
            claim_key = (raw_key, target)
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
            marked_identities.add(identity)
            claimed_hash = claims.get(claim_key)
            if claimed_hash:
                sent_hashes.setdefault(identity, set()).add(claimed_hash)
    return sent_hashes, marked_identities, {
        "path": str(path),
        "nonblank_lines": total,
        "malformed_lines": malformed,
        "successful_daily_identities": len(marked_identities),
        "successful_daily_identities_with_claim_hash": len(sent_hashes),
        "integrity_status": "PASSED" if malformed == 0 else "NOT_MET",
    }


def audit_daily_reports(
    *,
    start: date,
    end: date,
    reports_dir: Path,
    account_db: Path,
    live_trades_db: Path,
    ledger_db: Path,
    delivery_hashes: dict[str, set[str]] | None = None,
    marked_identities: set[str] | None = None,
    delivery_integrity: bool = True,
    validator: Callable[..., dict] = validate_daily_report.validate_report,
    evaluated_at: str | None = None,
) -> dict:
    """Return one auditable row per expected day and a strict coverage gate.

    闸门数值按预注册激活边界解析：边界前 0.99、边界起 0.95；老口径达成率仍
    在 ``legacy_target_diagnostics`` 里外显，长期信息不丢。
    """
    evaluated_cst = (
        thresholds.parse_cst(evaluated_at) if evaluated_at
        else datetime.now(CST)
    )
    target_rate = thresholds.coverage_target_rate(evaluated_cst)
    rows = []
    for day in _days_inclusive(start, end):
        path = reports_dir / f"daily-{day.isoformat()}.md"
        identity = f"reviewer:{day.isoformat()}:daily"
        if not path.exists():
            row = {
                "date": day.isoformat(),
                "path": str(path),
                "exists": False,
                "valid": False,
                "errors": ["artifact: missing daily report"],
                "checks": [],
            }
        else:
            try:
                result = validator(
                    report_path=path,
                    account_db=account_db,
                    live_trades_db=live_trades_db,
                    ledger_db=ledger_db,
                )
                row = {
                    "date": day.isoformat(),
                    "path": str(path),
                    "exists": True,
                    "valid": bool(result.get("ok")),
                    "report_ts": result.get("report_ts"),
                    "errors": list(result.get("errors") or []),
                    "checks": list(result.get("checks") or []),
                    "auto_send": False,
                }
            except Exception as exc:  # one bad artifact must not hide the rest
                row = {
                    "date": day.isoformat(),
                    "path": str(path),
                    "exists": True,
                    "valid": False,
                    "errors": [f"validator: {type(exc).__name__}: {exc}"],
                    "checks": [],
                    "auto_send": False,
                }
        artifact_hash = None
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            artifact_hash = hashlib.sha256(
                content.encode("utf-8", errors="replace")).hexdigest()
        identity_sent = bool(
            marked_identities is not None and identity in marked_identities)
        delivered = bool(
            artifact_hash
            and delivery_hashes is not None
            and artifact_hash in delivery_hashes.get(identity, set())
        )
        row.update({
            "dedupe_identity": identity,
            "artifact_sha256": artifact_hash,
            "identity_marked_sent": identity_sent,
            "delivery_confirmed": delivered,
            "delivered_report_complete": bool(row["valid"] and delivered),
        })
        rows.append(row)

    expected = len(rows)
    existing = sum(bool(row["exists"]) for row in rows)
    valid = sum(bool(row["valid"]) for row in rows)
    rate = valid / expected if expected else 0.0
    delivery_audited = delivery_hashes is not None and marked_identities is not None
    delivered = sum(bool(row["delivery_confirmed"]) for row in rows)
    delivered_complete = sum(
        bool(row["delivered_report_complete"]) for row in rows)
    delivery_rate = delivered / expected if expected else 0.0
    delivered_complete_rate = delivered_complete / expected if expected else 0.0
    report_status = "PASSED" if rate >= target_rate else "NOT_MET"
    if not delivery_audited:
        delivery_status = "NOT_EVALUATED"
        overall_status = report_status
    elif (
        delivery_integrity
        and delivery_rate >= target_rate
        and delivered_complete_rate >= target_rate
    ):
        delivery_status = "PASSED"
        overall_status = (
            "PASSED" if report_status == "PASSED" else "NOT_MET")
    else:
        delivery_status = "NOT_MET"
        overall_status = "NOT_MET"
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
        "target_rate": target_rate,
        "target_rate_migration": thresholds.coverage_migration_facts(
            evaluated_cst),
        "legacy_target_diagnostics": thresholds.legacy_rate_diagnostics({
            "completeness_rate": rate,
            "delivery_confirmation_rate": delivery_rate,
            "delivered_report_completeness_rate": delivered_complete_rate,
        }),
        "expected": expected,
        "existing": existing,
        "valid": valid,
        "invalid": expected - valid,
        "completeness_rate": rate,
        "status": report_status,
        "delivery_audited": delivery_audited,
        "delivery_confirmed": delivered,
        "delivery_unconfirmed": expected - delivered,
        "delivered_report_complete": delivered_complete,
        "delivery_confirmation_rate": delivery_rate,
        "delivered_report_completeness_rate": delivered_complete_rate,
        "delivery_status": delivery_status,
        "audit_status": overall_status,
        "rows": rows,
        "auto_send": False,
        "database_write": False,
        "production_order_authorized": False,
    }


def audit_forward_daily_reports(
    *,
    start: date,
    end: date,
    minimum_days: int,
    reports_dir: Path,
    account_db: Path,
    live_trades_db: Path,
    ledger_db: Path,
    delivery_hashes: dict[str, set[str]] | None = None,
    marked_identities: set[str] | None = None,
    delivery_integrity: bool = True,
    validator: Callable[..., dict] = validate_daily_report.validate_report,
    evaluated_at: str | None = None,
) -> dict:
    """Audit a pre-registered repair-era window without hiding old debt."""
    if minimum_days <= 0:
        raise ValueError("minimum forward days must be positive")
    if end < start:
        return {
            "start_date": start.isoformat(),
            "end_date": None,
            "end_inclusive": True,
            "minimum_days": minimum_days,
            "expected": 0,
            "existing": 0,
            "valid": 0,
            "invalid": 0,
            "completeness_rate": 0.0,
            "delivery_confirmation_rate": 0.0,
            "delivered_report_completeness_rate": 0.0,
            "status": "INSUFFICIENT_EVIDENCE",
            "rows": [],
        }
    audited = audit_daily_reports(
        start=start,
        end=end,
        reports_dir=reports_dir,
        account_db=account_db,
        live_trades_db=live_trades_db,
        ledger_db=ledger_db,
        delivery_hashes=delivery_hashes,
        marked_identities=marked_identities,
        delivery_integrity=delivery_integrity,
        validator=validator,
        evaluated_at=evaluated_at,
    )
    expected = int(audited["expected"])
    rate = float(audited["completeness_rate"])
    if expected < minimum_days:
        status = "INSUFFICIENT_EVIDENCE"
    elif audited["audit_status"] == "PASSED":
        status = "PASSED"
    else:
        status = "NOT_MET"
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "end_inclusive": True,
        "minimum_days": minimum_days,
        "expected": expected,
        "existing": int(audited["existing"]),
        "valid": int(audited["valid"]),
        "invalid": int(audited["invalid"]),
        "completeness_rate": rate,
        "delivery_confirmed": int(audited["delivery_confirmed"]),
        "delivery_unconfirmed": int(audited["delivery_unconfirmed"]),
        "delivered_report_complete": int(
            audited["delivered_report_complete"]),
        "delivery_confirmation_rate": float(
            audited["delivery_confirmation_rate"]),
        "delivered_report_completeness_rate": float(
            audited["delivered_report_completeness_rate"]),
        "delivery_status": audited["delivery_status"],
        "status": status,
        "rows": audited["rows"],
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
        "--forward-start",
        type=_parse_day,
        default=DEFAULT_FORWARD_START,
        help="pre-registered first repair-era daily artifact",
    )
    parser.add_argument(
        "--forward-minimum-days",
        type=int,
        default=DEFAULT_FORWARD_MINIMUM_DAYS,
    )
    parser.add_argument(
        "--reports-dir", default=r".\reports\daily-reports")
    parser.add_argument("--account-db", default=r".\db\account.db")
    parser.add_argument(
        "--live-trades-db", default=r".\db\live_trades.db")
    parser.add_argument("--ledger-db", default=r".\db\ledger.db")
    parser.add_argument(
        "--event-log", default=r".\logs\push\qq_push_dedupe.jsonl")
    parser.add_argument(
        "--json-out",
        default=r".\reports\quality\daily-report-completeness.json",
    )
    args = parser.parse_args(argv)
    if args.start is None and args.end is not None:
        parser.error("--end requires --start")
    if args.days is not None and args.days <= 0:
        parser.error("--days must be positive")
    if args.forward_minimum_days <= 0:
        parser.error("--forward-minimum-days must be positive")
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
    event_log = Path(args.event_log)
    missing = [str(path) for path in (*required.values(), event_log)
               if not path.exists()]
    if missing:
        print(json.dumps({
            "ok": False,
            "error": "missing input",
            "paths": missing,
        }, ensure_ascii=False), file=sys.stderr)
        return 2

    delivery_hashes, marked_identities, delivery_evidence = (
        _delivery_evidence(event_log))
    delivery_integrity = delivery_evidence["integrity_status"] == "PASSED"
    evaluated_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    result = audit_daily_reports(
        start=start,
        end=end,
        evaluated_at=evaluated_at,
        delivery_hashes=delivery_hashes,
        marked_identities=marked_identities,
        delivery_integrity=delivery_integrity,
        **required,
    )
    forward = audit_forward_daily_reports(
        start=args.forward_start,
        end=end,
        minimum_days=args.forward_minimum_days,
        evaluated_at=evaluated_at,
        delivery_hashes=delivery_hashes,
        marked_identities=marked_identities,
        delivery_integrity=delivery_integrity,
        **required,
    )
    result["rolling_status"] = result["status"]
    result["rolling_audit_status"] = result["audit_status"]
    result["delivery_evidence"] = delivery_evidence
    result["forward_after_remediation"] = forward
    result["overall_status"] = (
        "PENDING_FORWARD_EVIDENCE"
        if forward["status"] == "INSUFFICIENT_EVIDENCE"
        else forward["status"]
    )
    if args.json_out:
        _atomic_write_json(Path(args.json_out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
