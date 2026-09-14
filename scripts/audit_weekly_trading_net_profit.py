# -*- coding: utf-8 -*-
"""Read-only weekly trading-net-profit acceptance audit.

Goal 10 has one acceptance KPI: every complete Beijing-time week must have
strictly positive trading net profit.  A week is ``[Monday 08:00, next Monday
08:00)`` so the seven canonical daily-report windows tile it exactly.  Net
trading profit is the sum of OKX account-bill ``bal_change`` for type 2
(trading) and type 8 (funding); fees are already included.  Type 1 transfers
are reported separately and excluded.

The script is read-only.  Its only optional write is an atomic JSON artifact.
It never refreshes bills, changes thresholds, dispatches, repairs, or trades.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from _acceptance_thresholds import (
    WEEKLY_TRADING_NET_PROFIT_ACTIVATION_CST,
    WEEKLY_TRADING_NET_PROFIT_COMPARISON,
    WEEKLY_TRADING_NET_PROFIT_DECISION_CST,
    WEEKLY_TRADING_NET_PROFIT_TARGET_USDT,
)


CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
WEEK_ANCHOR_HOUR = 8
TRADING_BILL_TYPES = {"2", "8"}
TRANSFER_BILL_TYPE = "1"
TRANSFER_SUBTYPES = {"11": "transfer_in", "12": "transfer_out"}
ALLOWED_BILL_TYPES = TRADING_BILL_TYPES | {TRANSFER_BILL_TYPE}


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_cst(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("timestamp is empty")
        if len(text) == 10:
            text += " 00:00:00"
        elif len(text) == 16:
            text += ":00"
        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00").replace(" ", "T", 1))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def fmt_ts(value: str | datetime) -> str:
    return parse_cst(value).strftime(TS_FMT)


def now_cst() -> datetime:
    return datetime.now(CST)


def decimal_value(value: Any) -> Decimal:
    try:
        number = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid money value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite money value: {value!r}")
    return number


def money_float(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.00000001")))


def connect_ro(path: Path | str) -> sqlite3.Connection:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(target)
    con = sqlite3.connect(
        f"file:{target.as_posix()}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def latest_complete_week_end(as_of: str | datetime) -> datetime:
    ref = parse_cst(as_of)
    monday = ref - timedelta(days=ref.weekday())
    anchor = monday.replace(
        hour=WEEK_ANCHOR_HOUR, minute=0, second=0, microsecond=0)
    if ref < anchor:
        anchor -= timedelta(days=7)
    return anchor


def weekly_windows(
    as_of: str | datetime,
    *,
    history_weeks: int,
) -> list[tuple[datetime, datetime]]:
    count = max(1, int(history_weeks))
    end = latest_complete_week_end(as_of)
    return [
        (end - timedelta(days=7 * offset),
         end - timedelta(days=7 * (offset - 1)))
        for offset in range(count, 0, -1)
    ]


def load_bills(account_db: Path, end_ts: datetime) -> list[dict[str, Any]]:
    con = connect_ro(account_db)
    try:
        rows = con.execute(
            "SELECT profile,bill_id,ts,inst_id,ccy,type,subtype,bal_change,"
            "fee,pnl,interest,ord_id,trade_id,exec_type,fetched_at,raw "
            "FROM account_bills WHERE profile='live' "
            "AND datetime(ts)<datetime(?) ORDER BY datetime(ts),bill_id",
            (fmt_ts(end_ts),),
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def _receipt_payloads(directory: Path | None) -> Iterable[tuple[Path, dict]]:
    if directory is None or not directory.is_dir():
        return []
    out: list[tuple[Path, dict]] = []
    for path in sorted(directory.glob("receipt-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            out.append((path, payload))
    return out


def transfer_receipt_coverage(
    receipt_dir: Path | None,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    intervals: list[tuple[datetime, datetime, str]] = []
    invalid: list[dict[str, str]] = []
    for path, payload in _receipt_payloads(receipt_dir):
        try:
            interval_start = parse_cst(payload["window_start_cst"])
            interval_end = parse_cst(payload["window_end_exclusive_cst"])
            if interval_end <= start or interval_start >= end:
                continue
            checks = {
                "schema": payload.get("schema_version") == 1,
                "artifact": payload.get("artifact_type")
                == "account_cash_flow_forward_receipt",
                "profile": payload.get("profile") == "live",
                "endpoint": payload.get("endpoint") == "/api/v5/account/bills",
                "bill_type": str(payload.get("bill_type") or "") == "1",
                "subtypes": payload.get("subtype_mapping") == TRANSFER_SUBTYPES,
                "status": payload.get("status") == "ok",
                "pagination": payload.get("pagination_complete") is True,
                "no_backfill": payload.get("historical_backfill") is False,
                "no_orders": payload.get("orders_placed") == 0,
            }
            failed = [name for name, ok in checks.items() if not ok]
            if failed:
                raise ValueError("contract:" + ",".join(failed))
            intervals.append((interval_start, interval_end, str(path)))
        except (KeyError, TypeError, ValueError) as exc:
            invalid.append({
                "path": str(path),
                "error": f"{type(exc).__name__}:{exc}",
            })

    cursor = start
    gaps: list[dict[str, str]] = []
    for interval_start, interval_end, _ in sorted(intervals):
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            gaps.append({
                "start_cst": fmt_ts(cursor),
                "end_exclusive_cst": fmt_ts(min(interval_start, end)),
            })
            break
        cursor = max(cursor, interval_end)
        if cursor >= end:
            break
    if cursor < end and not gaps:
        gaps.append({
            "start_cst": fmt_ts(cursor),
            "end_exclusive_cst": fmt_ts(end),
        })
    return {
        "receipt_dir": str(receipt_dir) if receipt_dir else None,
        "valid_receipts": len(intervals),
        "invalid_receipts": invalid,
        "gaps": gaps,
        "complete": not invalid and not gaps and cursor >= end,
    }


def trading_receipt_coverage(
    receipt_dir: Path | None,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    intervals: list[tuple[datetime, datetime, str]] = []
    invalid: list[dict[str, str]] = []
    for path, payload in _receipt_payloads(receipt_dir):
        try:
            interval_start = parse_cst(payload["window_start_cst"])
            interval_end = parse_cst(payload["window_end_exclusive_cst"])
            if interval_end <= start or interval_start >= end:
                continue
            checks = {
                "schema": payload.get("schema_version") == 1,
                "artifact": payload.get("artifact_type")
                == "account_trading_bills_forward_receipt",
                "profile": payload.get("profile") == "live",
                "endpoint": payload.get("endpoint") == "/api/v5/account/bills",
                "inst_type": payload.get("inst_type") == "SWAP",
                "status": payload.get("status") == "ok",
                "pagination": payload.get("pagination_complete") is True,
                "no_backfill": payload.get("historical_backfill") is False,
                "no_orders": payload.get("orders_placed") == 0,
            }
            failed = [name for name, ok in checks.items() if not ok]
            if failed:
                raise ValueError("contract:" + ",".join(failed))
            intervals.append((interval_start, interval_end, str(path)))
        except (KeyError, TypeError, ValueError) as exc:
            invalid.append({
                "path": str(path),
                "error": f"{type(exc).__name__}:{exc}",
            })

    cursor = start
    gaps: list[dict[str, str]] = []
    for interval_start, interval_end, _ in sorted(intervals):
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            gaps.append({
                "start_cst": fmt_ts(cursor),
                "end_exclusive_cst": fmt_ts(min(interval_start, end)),
            })
            break
        cursor = max(cursor, interval_end)
        if cursor >= end:
            break
    if cursor < end and not gaps:
        gaps.append({
            "start_cst": fmt_ts(cursor),
            "end_exclusive_cst": fmt_ts(end),
        })
    return {
        "receipt_dir": str(receipt_dir) if receipt_dir else None,
        "valid_receipts": len(intervals),
        "invalid_receipts": invalid,
        "gaps": gaps,
        "complete": not invalid and not gaps and cursor >= end,
    }


def evaluate_week(
    bills: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    activation: datetime,
    transfer_receipt_dir: Path | None,
    trading_receipt_dir: Path | None,
) -> dict[str, Any]:
    rows = [
        row for row in bills
        if start <= parse_cst(str(row.get("ts") or "")) < end
    ]
    bill_ids = [str(row.get("bill_id") or "") for row in rows]
    duplicates = sorted(
        bill_id for bill_id, count in Counter(bill_ids).items()
        if bill_id and count > 1)
    trading = [
        row for row in rows
        if str(row.get("type") or "") in TRADING_BILL_TYPES
        and str(row.get("ccy") or "") == "USDT"
    ]
    transfers = [
        row for row in rows
        if str(row.get("type") or "") == TRANSFER_BILL_TYPE
        and str(row.get("ccy") or "") == "USDT"
    ]
    unsupported = [
        str(row.get("bill_id") or "")
        for row in rows
        if (
            str(row.get("type") or "") not in ALLOWED_BILL_TYPES
            or str(row.get("ccy") or "") != "USDT"
            or (
                str(row.get("type") or "") == TRANSFER_BILL_TYPE
                and str(row.get("subtype") or "") not in TRANSFER_SUBTYPES
            )
        )
    ]
    trade_net = sum(
        (decimal_value(row.get("bal_change")) for row in trading), Decimal(0))
    order_net = sum(
        (decimal_value(row.get("bal_change")) for row in trading
         if str(row.get("type") or "") == "2"), Decimal(0))
    funding_net = sum(
        (decimal_value(row.get("bal_change")) for row in trading
         if str(row.get("type") or "") == "8"), Decimal(0))
    transfer_net = sum(
        (decimal_value(row.get("bal_change")) for row in transfers), Decimal(0))
    transfer_coverage = transfer_receipt_coverage(
        transfer_receipt_dir, start, end)
    trading_coverage = trading_receipt_coverage(
        trading_receipt_dir, start, end)
    evidence_checks = {
        "bill_ids_unique": not duplicates,
        "bill_types_and_currency_classified": not unsupported,
        "transfer_query_receipts_complete": transfer_coverage["complete"],
        "trading_bill_receipts_complete": trading_coverage["complete"],
    }
    evidence_complete = all(evidence_checks.values())
    eligible = start >= activation
    positive = trade_net > Decimal(str(WEEKLY_TRADING_NET_PROFIT_TARGET_USDT))
    if not eligible:
        status = "PRE_ACTIVATION_DIAGNOSTIC"
    elif not evidence_complete:
        status = "INSUFFICIENT_EVIDENCE"
    elif positive:
        status = "MET"
    else:
        status = "NOT_MET"
    return {
        "week_start_cst": fmt_ts(start),
        "week_end_exclusive_cst": fmt_ts(end),
        "eligible_for_acceptance": eligible,
        "status": status,
        "trading_net_profit_usdt": money_float(trade_net),
        "order_bill_net_usdt": money_float(order_net),
        "funding_bill_net_usdt": money_float(funding_net),
        "trading_bill_rows": len(trading),
        "excluded_transfer_net_usdt": money_float(transfer_net),
        "excluded_transfer_rows": len(transfers),
        "profit_strictly_positive": positive,
        "evidence_complete": evidence_complete,
        "evidence_checks": evidence_checks,
        "trading_receipt_coverage": trading_coverage,
        "transfer_receipt_coverage": transfer_coverage,
        "duplicate_bill_ids": duplicates,
        "unsupported_or_unclassified_bill_ids": unsupported[:25],
    }


def build_report(
    *,
    account_db: Path,
    transfer_receipt_dir: Path | None,
    trading_receipt_dir: Path | None,
    as_of: str,
    activation_cst: str = WEEKLY_TRADING_NET_PROFIT_ACTIVATION_CST,
    history_weeks: int = 8,
) -> dict[str, Any]:
    as_of_dt = parse_cst(as_of)
    activation = parse_cst(activation_cst)
    if activation.weekday() != 0 or (
        activation.hour, activation.minute, activation.second
    ) != (WEEK_ANCHOR_HOUR, 0, 0):
        raise ValueError("weekly activation must be Monday 08:00 CST")
    windows = weekly_windows(as_of_dt, history_weeks=history_weeks)
    bills = load_bills(Path(account_db), windows[-1][1])
    weeks = [
        evaluate_week(
            bills,
            start=start,
            end=end,
            activation=activation,
            transfer_receipt_dir=transfer_receipt_dir,
            trading_receipt_dir=trading_receipt_dir,
        )
        for start, end in windows
    ]
    eligible = [week for week in weeks if week["eligible_for_acceptance"]]
    if not eligible:
        status = "PENDING_FORWARD_EVIDENCE"
    elif any(week["status"] == "INSUFFICIENT_EVIDENCE" for week in eligible):
        status = "INSUFFICIENT_EVIDENCE"
    elif all(week["status"] == "MET" for week in eligible):
        status = "MET"
    else:
        status = "NOT_MET"

    current_week_start = latest_complete_week_end(as_of_dt)
    current_rows = load_bills(Path(account_db), as_of_dt)
    partial_rows = [
        row for row in current_rows
        if current_week_start <= parse_cst(str(row.get("ts") or "")) < as_of_dt
        and str(row.get("type") or "") in TRADING_BILL_TYPES
        and str(row.get("ccy") or "") == "USDT"
    ]
    partial_net = sum(
        (decimal_value(row.get("bal_change")) for row in partial_rows), Decimal(0))
    return {
        "schema_version": 1,
        "artifact_type": "weekly_trading_net_profit_acceptance_audit",
        "generated_at_cst": fmt_ts(now_cst()),
        "as_of_cst": fmt_ts(as_of_dt),
        "mode": "read_only_account_bills",
        "status": status,
        "definition": {
            "week_window": "[Monday 08:00 CST, next Monday 08:00 CST)",
            "trading_net_profit": (
                "SUM(account_bills.bal_change) for profile=live, ccy=USDT, "
                "type in (2 trading, 8 funding); fees are already included"),
            "transfer_exclusion": (
                "type=1 subtype=11/12 is reported separately and excluded"),
            "win_rate_role": "diagnostic_only_not_an_acceptance_requirement",
            "minimum_trade_count": None,
            "zero_profit_week": "NOT_MET because the comparison is strictly >0",
            "no_trade_pressure": (
                "a non-positive week never authorizes extra trades, leverage, "
                "risk, delayed stops, or hidden losses"),
        },
        "registration": {
            "decision_cst": WEEKLY_TRADING_NET_PROFIT_DECISION_CST,
            "activation_cst": fmt_ts(activation),
            "first_binding_week_end_exclusive_cst": fmt_ts(
                activation + timedelta(days=7)),
            "comparison": WEEKLY_TRADING_NET_PROFIT_COMPARISON,
            "target_usdt": WEEKLY_TRADING_NET_PROFIT_TARGET_USDT,
            "forward_only": True,
            "historical_rejudgement": False,
        },
        "counts": {
            "diagnostic_weeks": len(weeks),
            "eligible_complete_weeks": len(eligible),
            "positive_eligible_weeks": sum(
                week["status"] == "MET" for week in eligible),
            "nonpositive_eligible_weeks": sum(
                week["status"] == "NOT_MET" for week in eligible),
            "insufficient_evidence_weeks": sum(
                week["status"] == "INSUFFICIENT_EVIDENCE" for week in eligible),
        },
        "weeks": weeks,
        "latest_complete_week_diagnostic": weeks[-1],
        "current_partial_week": {
            "week_start_cst": fmt_ts(current_week_start),
            "planned_end_exclusive_cst": fmt_ts(
                current_week_start + timedelta(days=7)),
            "observed_through_cst": fmt_ts(as_of_dt),
            "trading_bill_rows": len(partial_rows),
            "trading_net_profit_to_date_usdt": money_float(partial_net),
            "acceptance_effect": "none_until_week_closes_and_evidence_completes",
        },
        "diagnostics": {
            "positive_week_count": sum(
                week["trading_net_profit_usdt"] > 0 for week in weeks),
            "nonpositive_week_count": sum(
                week["trading_net_profit_usdt"] <= 0 for week in weeks),
            "win_rate_required": False,
            "lifecycle_sample_minimum_required": False,
            "account_equity_delta_required": False,
        },
        "safety": {
            "business_database_read_only": True,
            "network_calls": 0,
            "orders_placed": 0,
            "dispatches": 0,
            "repairs_or_retries": 0,
            "production_threshold_changes": 0,
        },
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="read-only weekly trading net profit acceptance audit")
    parser.add_argument("--account-db", default=str(root / "db" / "account.db"))
    parser.add_argument(
        "--transfer-receipt-dir",
        default=str(root / "reports" / "quality" / "account-cash-flow-forward"))
    parser.add_argument(
        "--trading-receipt-dir",
        default=str(root / "reports" / "quality" / "account-trading-bills-forward"))
    parser.add_argument(
        "--activation-cst", default=WEEKLY_TRADING_NET_PROFIT_ACTIVATION_CST)
    parser.add_argument("--as-of", default=fmt_ts(now_cst()))
    parser.add_argument("--history-weeks", type=int, default=8)
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    try:
        report = build_report(
            account_db=Path(args.account_db),
            transfer_receipt_dir=Path(args.transfer_receipt_dir),
            trading_receipt_dir=Path(args.trading_receipt_dir),
            as_of=args.as_of,
            activation_cst=args.activation_cst,
            history_weeks=args.history_weeks,
        )
        if args.json_out:
            atomic_write_json(Path(args.json_out), report)
        print(json.dumps({
            "ok": True,
            "status": report["status"],
            "registration": report["registration"],
            "counts": report["counts"],
            "latest_complete_week_diagnostic": (
                report["latest_complete_week_diagnostic"]),
            "current_partial_week": report["current_partial_week"],
            "json_out": args.json_out,
            "orders_placed": 0,
            "production_database_writes": 0,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - one explicit CLI failure
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "orders_placed": 0,
            "production_database_writes": 0,
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
