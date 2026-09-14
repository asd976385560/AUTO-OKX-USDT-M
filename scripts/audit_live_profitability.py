# -*- coding: utf-8 -*-
"""Read-only lifecycle, win-rate, and account-economics diagnostic.

The audit deliberately uses a *position lifecycle* as the strategy grain.  A
lifecycle starts when one ``symbol + side`` position moves from zero to a
positive quantity, merges later OPEN/ADD fills, merges REDUCE/CLOSE fills, and
finishes only when the ledger quantity returns to zero.  Close-fill row counts
are therefore never substituted for completed-position counts.

For every completed lifecycle the audit cross-checks the live trade ledger,
its raw exchange receipt/order identifiers, completed execution intent for
Agent-initiated increases, and OKX account-bill rows.  Net lifecycle PnL is
rebuilt from order bill ``bal_change`` plus in-position funding bills; the
ledger's gross close PnL remains a diagnostic only.  Maintenance microtests are
excluded from strategy win rate but their real bills remain in account-level
net profit and equity reconciliation.

All SQLite connections are read-only.  The only optional write is the explicit
``--json-out`` evidence file, replaced atomically.  The script never refreshes
bills, changes a threshold, dispatches, repairs, or places an order.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from core import policy_epochs


CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
INCREASE_ACTIONS = {"open", "add"}
DECREASE_ACTIONS = {"close", "reduce"}
SUPPORTED_ACCOUNT_BILL_TYPES = {"1", "2", "8"}
TRADING_ACCOUNT_BILL_TYPES = {"2", "8"}
EXTERNAL_CASH_FLOW_BILL_TYPE = "1"
FUNDING_BILL_TYPE = "8"
CASH_FLOW_FORWARD_ACTIVATION_CST = "2026-08-18 00:00:00"
CASH_FLOW_SUBTYPES = {"11": "transfer_in", "12": "transfer_out"}
QUANTITY_EPSILON = 1e-8
MONEY_TOLERANCE = 0.05


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


def connect_ro(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def positive(value: Any) -> float | None:
    number = finite_number(value)
    return number if number is not None and number > 0 else None


def normalize_side(value: Any) -> str:
    side = str(value or "").strip().lower()
    if side in {"long", "buy", "open_long"}:
        return "long"
    if side in {"short", "sell", "open_short"}:
        return "short"
    return side


def explicitly_rejected(row: dict[str, Any]) -> bool:
    raw = json_dict(row.get("raw"))
    for obj in (row, raw):
        status = str(obj.get("status") or "").strip().lower()
        if status in {"rejected", "reject", "failed", "error"}:
            return True
        if obj.get("ok") is False or obj.get("success") is False:
            return True
        if str(obj.get("reject_reason") or "").strip():
            return True
    return False


def order_ids_from_raw(raw: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("ordId", "ord_id", "open_id", "close_ordId"):
        value = raw.get(key)
        if value not in (None, "", 0, "0"):
            values.add(str(value))
    for key in ("ord_ids", "order_ids"):
        items = raw.get(key)
        if isinstance(items, list):
            for value in items:
                if value not in (None, "", 0, "0"):
                    values.add(str(value))
    fills = raw.get("fills")
    if isinstance(fills, list):
        for item in fills:
            if not isinstance(item, dict):
                continue
            value = item.get("ordId") or item.get("ord_id")
            if value not in (None, "", 0, "0"):
                values.add(str(value))
    return values


def maintenance_fill(raw: dict[str, Any]) -> bool:
    if raw.get("maintenance_test") is True or raw.get("microtest") is True:
        return True
    reason = str(raw.get("reason") or "").strip().casefold()
    if "microtest" in reason:
        return True
    chinese_markers = ("维护微单", "测试微单", "回归微单", "链路回归")
    return any(marker in reason for marker in chinese_markers)


def direct_exchange_receipt(raw: dict[str, Any]) -> bool:
    fill_source = str(raw.get("fill_source") or "").strip().lower()
    if fill_source in {"fills", "exchange_fills", "order_fills"}:
        return True
    fills = raw.get("fills")
    if isinstance(fills, list) and fills:
        return True
    return (
        str(raw.get("reconcile_source") or "").strip()
        in {"exchange_fills_reconcile", "exact_exchange_reconcile"}
        and isinstance(fills, list)
        and bool(fills)
    )


def wilson_interval(successes: int, total: int) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    z = 1.959963984540054
    p = successes / total
    denominator = 1.0 + z * z / total
    center = (p + z * z / (2.0 * total)) / denominator
    half = (
        z
        * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
        / denominator
    )
    return max(0.0, center - half), min(1.0, center + half)


def max_drawdown(values: Iterable[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        cumulative += float(value)
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def policy_epoch_diagnostic(
    rows: list[dict[str, Any]],
    *,
    minimum_closed_lifecycles: int,
) -> dict[str, Any]:
    """Describe current-policy outcomes without applying a win-rate target."""
    probe_start = parse_cst(policy_epochs.PROBE_CLAUSE_START_CST)
    current_start = parse_cst(policy_epochs.PROBE_CLAUSE_END_CST)
    buckets = {
        "pre_withdrawn_probe": [],
        policy_epochs.EPOCH_WITHDRAWN_PROBE: [],
        "current_policy_after_withdrawal": [],
        policy_epochs.EPOCH_UNKNOWN: [],
    }
    for row in rows:
        cycle_id = str(row.get("start_cycle_id") or "").strip()
        try:
            moment = parse_cst(cycle_id)
        except (TypeError, ValueError):
            buckets[policy_epochs.EPOCH_UNKNOWN].append(row)
            continue
        epoch = policy_epochs.policy_epoch(cycle_id)
        if epoch == policy_epochs.EPOCH_WITHDRAWN_PROBE:
            buckets[policy_epochs.EPOCH_WITHDRAWN_PROBE].append(row)
        elif moment >= current_start:
            buckets["current_policy_after_withdrawal"].append(row)
        elif moment < probe_start:
            buckets["pre_withdrawn_probe"].append(row)
        else:
            buckets[policy_epochs.EPOCH_UNKNOWN].append(row)

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        wins = sum(row["net_pnl_after_direct_costs"] > 0 for row in items)
        losses = sum(row["net_pnl_after_direct_costs"] < 0 for row in items)
        flat = len(items) - wins - losses
        rate = wins / len(items) if items else None
        low, high = wilson_interval(wins, len(items))
        net = sum(row["net_pnl_after_direct_costs"] for row in items)
        return {
            "verified_sample_n": len(items),
            "wins": wins,
            "losses": losses,
            "flat": flat,
            "win_rate": rate,
            "wilson_95_low": low,
            "wilson_95_high": high,
            "net_pnl_after_direct_costs": net,
        }

    summaries = {name: summarize(items) for name, items in buckets.items()}
    current = summaries["current_policy_after_withdrawal"]
    if current["verified_sample_n"] < minimum_closed_lifecycles:
        sample_status = "NOT_MEASURABLE"
    else:
        sample_status = "MEASURABLE"
    return {
        "current_policy_start_cst": fmt_ts(current_start),
        "boundary_source": "core.policy_epochs.PROBE_CLAUSE_END_CST",
        "minimum_closed_lifecycles": minimum_closed_lifecycles,
        "cohorts": summaries,
        "current_policy_sample_status": sample_status,
        "scope": (
            "diagnostic only; no win-rate target is applied; every lifecycle "
            "remains in full strategy, account-economics and report denominators"
        ),
        "historical_rejudgement": False,
        "acceptance_denominator_unchanged": True,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_snapshots(con: sqlite3.Connection, as_of: str, window_days: int) -> dict[str, Any]:
    bill_max_row = con.execute(
        "SELECT MAX(ts) AS max_ts, MAX(fetched_at) AS max_fetched_at "
        "FROM account_bills WHERE profile='live' AND type IN ('2','8')"
    ).fetchone()
    bill_max = str(bill_max_row["max_ts"] or "") if bill_max_row else ""
    bill_fetched = (
        str(bill_max_row["max_fetched_at"] or "") if bill_max_row else "")
    if not bill_max or not bill_fetched:
        raise ValueError("live account bills are empty")
    # A successful empty incremental fetch is still authoritative evidence that
    # no newer bill exists.  The evidence cutoff is therefore fetched_at, not
    # the business timestamp of the latest non-empty bill row.
    end_cap = min(parse_cst(as_of), parse_cst(bill_fetched))
    end_row = con.execute(
        "SELECT ts,totalEq,availBal,upl FROM account_snapshots "
        "WHERE profile='live' AND datetime(ts)<=datetime(?) "
        "AND totalEq IS NOT NULL ORDER BY datetime(ts) DESC LIMIT 1",
        (fmt_ts(end_cap),),
    ).fetchone()
    if end_row is None:
        raise ValueError("no live account snapshot at or before bill evidence cutoff")
    end_ts = parse_cst(str(end_row["ts"]))
    target_start = end_ts - timedelta(days=window_days)
    start_row = con.execute(
        "SELECT ts,totalEq,availBal,upl FROM account_snapshots "
        "WHERE profile='live' AND datetime(ts)<=datetime(?) "
        "AND totalEq IS NOT NULL ORDER BY datetime(ts) DESC LIMIT 1",
        (fmt_ts(target_start),),
    ).fetchone()
    if start_row is None:
        raise ValueError("account snapshot history does not cover the requested window")
    latest_row = con.execute(
        "SELECT ts,totalEq,availBal,upl FROM account_snapshots "
        "WHERE profile='live' AND datetime(ts)<=datetime(?) "
        "AND totalEq IS NOT NULL ORDER BY datetime(ts) DESC LIMIT 1",
        (fmt_ts(as_of),),
    ).fetchone()

    def snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "ts": str(row["ts"]),
            "totalEq": finite_number(row["totalEq"]),
            "availBal": finite_number(row["availBal"]),
            "upl": finite_number(row["upl"]),
        }

    return {
        "bill_latest_event_ts": bill_max,
        "bill_evidence_fetched_at": bill_fetched,
        "requested_as_of_cst": fmt_ts(as_of),
        "evidence_lag_seconds": max(
            0.0, (parse_cst(as_of) - parse_cst(bill_fetched)).total_seconds()),
        "start": snapshot(start_row),
        "end": snapshot(end_row),
        "latest_account": snapshot(latest_row),
    }


def load_trade_rows(con: sqlite3.Connection, end_ts: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT id,cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw "
        "FROM trades WHERE datetime(ts)<=datetime(?) "
        "ORDER BY datetime(ts),id",
        (end_ts,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_lifecycles(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    active: dict[tuple[str, str], dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    for source in rows:
        row = dict(source)
        action = str(row.get("action") or "").strip().lower()
        if action not in INCREASE_ACTIONS | DECREASE_ACTIONS:
            ignored.append({"id": row.get("id"), "action": action})
            continue
        quantity = positive(row.get("sz"))
        fill_px = positive(row.get("fill_px"))
        if quantity is None or fill_px is None or explicitly_rejected(row):
            anomalies.append({
                "kind": "invalid_or_rejected_fill_row",
                "id": row.get("id"),
                "ts": row.get("ts"),
                "symbol": row.get("symbol"),
                "action": action,
            })
            continue
        symbol = str(row.get("symbol") or "").strip()
        side = normalize_side(row.get("side"))
        if not symbol or side not in {"long", "short"}:
            anomalies.append({
                "kind": "invalid_symbol_or_side",
                "id": row.get("id"),
                "ts": row.get("ts"),
                "symbol": symbol,
                "side": side,
            })
            continue
        row["action"] = action
        row["side"] = side
        row["raw_obj"] = json_dict(row.get("raw"))
        row["order_ids"] = sorted(order_ids_from_raw(row["raw_obj"]))
        key = (symbol, side)

        if action in INCREASE_ACTIONS:
            lifecycle = active.get(key)
            if lifecycle is None:
                lifecycle = {
                    "symbol": symbol,
                    "side": side,
                    "started_at": str(row["ts"]),
                    "start_cycle_id": row.get("cycle_id"),
                    "start_trade_id": int(row["id"]),
                    "quantity": 0.0,
                    "fills": [],
                    "maintenance": False,
                    "quantity_anomaly": False,
                }
                active[key] = lifecycle
            lifecycle["quantity"] += quantity
            lifecycle["fills"].append(row)
            if maintenance_fill(row["raw_obj"]):
                lifecycle["maintenance"] = True
            continue

        lifecycle = active.get(key)
        if lifecycle is None:
            anomalies.append({
                "kind": "decrease_without_open_lifecycle",
                "id": row.get("id"),
                "ts": row.get("ts"),
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
            })
            continue
        if quantity > lifecycle["quantity"] + QUANTITY_EPSILON:
            lifecycle["quantity_anomaly"] = True
            anomalies.append({
                "kind": "decrease_exceeds_ledger_quantity",
                "id": row.get("id"),
                "ts": row.get("ts"),
                "symbol": symbol,
                "side": side,
                "decrease": quantity,
                "ledger_quantity_before": lifecycle["quantity"],
            })
        lifecycle["fills"].append(row)
        lifecycle["quantity"] = max(0.0, lifecycle["quantity"] - quantity)
        if lifecycle["quantity"] <= QUANTITY_EPSILON:
            lifecycle["quantity"] = 0.0
            lifecycle["closed_at"] = str(row["ts"])
            lifecycle["end_cycle_id"] = row.get("cycle_id")
            lifecycle["end_trade_id"] = int(row["id"])
            lifecycle["lifecycle_id"] = (
                f"{symbol}|{side}|{lifecycle['start_trade_id']}|{row['id']}")
            completed.append(lifecycle)
            del active[key]

    open_lifecycles = []
    for lifecycle in active.values():
        open_lifecycles.append({
            "symbol": lifecycle["symbol"],
            "side": lifecycle["side"],
            "started_at": lifecycle["started_at"],
            "start_trade_id": lifecycle["start_trade_id"],
            "remaining_quantity": lifecycle["quantity"],
            "fill_count": len(lifecycle["fills"]),
        })
    return completed, anomalies, open_lifecycles


def load_bills(con: sqlite3.Connection, end_ts: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT profile,bill_id,ts,inst_id,ccy,type,subtype,bal_change,fee,"
        "pnl,interest,ord_id,trade_id,exec_type,fetched_at,raw FROM account_bills "
        "WHERE profile='live' AND datetime(ts)<=datetime(?) "
        "ORDER BY datetime(ts),bill_id",
        (end_ts,),
    ).fetchall()
    return [dict(row) for row in rows]


def cash_flow_receipt_coverage(
    receipt_dir: Path | None,
    start_ts: str,
    end_ts: str,
    *,
    forward_start: str = CASH_FLOW_FORWARD_ACTIVATION_CST,
) -> dict[str, Any]:
    """Verify that dated type=1 query receipts continuously cover the window."""
    start = parse_cst(start_ts)
    end = parse_cst(end_ts)
    activation = parse_cst(forward_start)
    directory = Path(receipt_dir) if receipt_dir is not None else None
    intervals: list[tuple[datetime, datetime, str]] = []
    invalid: list[dict[str, str]] = []
    if directory is not None and directory.is_dir():
        for path in sorted(directory.glob("receipt-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("root_not_object")
                if payload.get("status") == "pre_activation":
                    pre_end = parse_cst(payload["window_end_exclusive_cst"])
                    if pre_end <= activation:
                        continue
                interval_start = parse_cst(payload["window_start_cst"])
                interval_end = parse_cst(payload["window_end_exclusive_cst"])
                if interval_end <= start or interval_start >= end:
                    continue
                checks = {
                    "schema": payload.get("schema_version") == 1,
                    "artifact": payload.get("artifact_type")
                    == "account_cash_flow_forward_receipt",
                    "profile": payload.get("profile") == "live",
                    "endpoint": payload.get("endpoint")
                    == "/api/v5/account/bills",
                    "bill_type": str(payload.get("bill_type") or "")
                    == EXTERNAL_CASH_FLOW_BILL_TYPE,
                    "subtypes": payload.get("subtype_mapping")
                    == CASH_FLOW_SUBTYPES,
                    "forward_start": fmt_ts(payload.get("forward_start_cst"))
                    == fmt_ts(activation),
                    "status": payload.get("status") == "ok",
                    "pagination": payload.get("pagination_complete") is True,
                    "no_backfill": payload.get("historical_backfill") is False,
                    "no_orders": payload.get("orders_placed") == 0,
                }
                failed = [name for name, ok in checks.items() if not ok]
                if failed:
                    raise ValueError("contract:" + ",".join(failed))
                duration = (interval_end - interval_start).total_seconds()
                if duration <= 0 or duration > 86400:
                    raise ValueError("window_duration_invalid")
                intervals.append((interval_start, interval_end, str(path)))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                    KeyError, TypeError, ValueError) as exc:
                invalid.append({
                    "path": str(path),
                    "error": f"{type(exc).__name__}: {exc}",
                })

    eligible = start >= activation
    cursor = start
    gaps: list[dict[str, str]] = []
    if eligible:
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
    complete = eligible and not invalid and not gaps and cursor >= end
    return {
        "receipt_dir": str(directory) if directory is not None else None,
        "forward_start_cst": fmt_ts(activation),
        "window_start_cst": fmt_ts(start),
        "window_end_cst": fmt_ts(end),
        "window_is_fully_forward": eligible,
        "valid_receipts": len(intervals),
        "invalid_receipts": invalid,
        "covered_through_cst": fmt_ts(min(cursor, end)) if eligible else None,
        "gaps": gaps,
        "complete": complete,
    }


def load_intents(con: sqlite3.Connection, end_ts: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT profile,cycle_id,symbol,action,side,state,reserved_at,"
        "submitted_at,completed_at,ord_id,error FROM execution_intents "
        "WHERE profile='live' AND datetime(reserved_at)<=datetime(?)",
        (end_ts,),
    ).fetchall()
    return [dict(row) for row in rows]


def trade_intent_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return the exact immutable identity shared by a fill and its intent."""
    return (
        str(row.get("cycle_id") or "").strip(),
        str(row.get("symbol") or "").strip(),
        str(row.get("action") or "").strip().lower(),
        normalize_side(row.get("side")),
    )


def ambiguous_funding_lifecycle_ids(
    lifecycles: list[dict[str, Any]],
    funding_by_symbol: dict[str, list[dict[str, Any]]],
) -> set[str]:
    """Find opposite-side overlaps that make directionless funding ambiguous."""
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lifecycle in lifecycles:
        by_symbol[lifecycle["symbol"]].append(lifecycle)
    ambiguous: set[str] = set()
    for symbol, rows in by_symbol.items():
        funding = funding_by_symbol.get(symbol, [])
        if not funding:
            continue
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                if left["side"] == right["side"]:
                    continue
                overlap_start = max(
                    parse_cst(left["started_at"]),
                    parse_cst(right["started_at"]),
                )
                overlap_end = min(
                    parse_cst(left["closed_at"]),
                    parse_cst(right["closed_at"]),
                )
                if overlap_start >= overlap_end:
                    continue
                if any(
                    overlap_start < parse_cst(str(item["ts"])) <= overlap_end
                    for item in funding
                ):
                    ambiguous.add(left["lifecycle_id"])
                    ambiguous.add(right["lifecycle_id"])
    return ambiguous


def verify_lifecycle(
    lifecycle: dict[str, Any],
    bills_by_order: dict[str, list[dict[str, Any]]],
    funding_by_symbol: dict[str, list[dict[str, Any]]],
    intents_by_order: dict[str, list[dict[str, Any]]],
    intents_by_trade_identity: dict[
        tuple[str, str, str, str], list[dict[str, Any]]],
    *,
    funding_ambiguous: bool = False,
) -> dict[str, Any]:
    issues: list[str] = []
    order_ids: set[str] = set()
    order_id_recoveries: list[dict[str, Any]] = []
    gross_close_pnl = 0.0
    for fill in lifecycle["fills"]:
        ids = set(fill.get("order_ids") or [])
        if not ids:
            identity = trade_intent_identity(fill)
            matching_intents = [
                intent
                for intent in intents_by_trade_identity.get(identity, [])
                if str(intent.get("state") or "").strip() == "completed"
                and str(intent.get("ord_id") or "").strip()
            ]
            if len(matching_intents) == 1:
                recovered_order_id = str(
                    matching_intents[0]["ord_id"]).strip()
                ids.add(recovered_order_id)
                order_id_recoveries.append({
                    "trade_id": int(fill["id"]),
                    "cycle_id": identity[0],
                    "symbol": identity[1],
                    "action": identity[2],
                    "side": identity[3],
                    "order_id": recovered_order_id,
                    "source": "exact_completed_execution_intent",
                })
            else:
                issues.append(f"trade_id={fill['id']}:order_id_missing")
        order_ids.update(ids)
        action = str(fill["action"])
        raw = fill["raw_obj"]
        if action in INCREASE_ACTIONS:
            completed_intent = any(
                str(intent.get("state") or "") == "completed"
                for order_id in ids
                for intent in intents_by_order.get(order_id, [])
            )
            if not completed_intent and not direct_exchange_receipt(raw):
                issues.append(
                    f"trade_id={fill['id']}:intent_or_exchange_receipt_missing")
        elif ids and not any(
            str(intent.get("state") or "") == "completed"
            for order_id in ids
            for intent in intents_by_order.get(order_id, [])
        ):
            if not direct_exchange_receipt(raw):
                issues.append(
                    f"trade_id={fill['id']}:intent_or_exchange_receipt_missing")
        if action in DECREASE_ACTIONS:
            pnl = finite_number(fill.get("pnl"))
            if pnl is None:
                issues.append(f"trade_id={fill['id']}:close_pnl_missing")
            else:
                gross_close_pnl += pnl

    order_bills: list[dict[str, Any]] = []
    missing_bill_orders: list[str] = []
    for order_id in sorted(order_ids):
        matching = [
            row for row in bills_by_order.get(order_id, [])
            if str(row.get("inst_id") or "") == lifecycle["symbol"]
            and str(row.get("type") or "") == "2"
        ]
        if not matching:
            missing_bill_orders.append(order_id)
        order_bills.extend(matching)
    if missing_bill_orders:
        issues.append("account_bill_missing:" + ",".join(missing_bill_orders))

    start = parse_cst(lifecycle["started_at"])
    end = parse_cst(lifecycle["closed_at"])
    funding_rows = [
        row for row in funding_by_symbol.get(lifecycle["symbol"], [])
        if start < parse_cst(str(row["ts"])) <= end
    ]
    if funding_ambiguous and funding_rows:
        issues.append("funding_attribution_ambiguous_opposite_side_overlap")
    order_net = sum(finite_number(row.get("bal_change")) or 0.0 for row in order_bills)
    order_fees = sum(finite_number(row.get("fee")) or 0.0 for row in order_bills)
    order_realized = sum(finite_number(row.get("pnl")) or 0.0 for row in order_bills)
    funding_net = sum(finite_number(row.get("bal_change")) or 0.0 for row in funding_rows)
    net_pnl = order_net + funding_net

    return {
        "lifecycle_id": lifecycle["lifecycle_id"],
        "symbol": lifecycle["symbol"],
        "side": lifecycle["side"],
        "start_cycle_id": lifecycle.get("start_cycle_id"),
        "end_cycle_id": lifecycle.get("end_cycle_id"),
        "started_at": lifecycle["started_at"],
        "closed_at": lifecycle["closed_at"],
        "maintenance": bool(lifecycle["maintenance"]),
        "fill_count": len(lifecycle["fills"]),
        "order_ids": sorted(order_ids),
        "order_id_recoveries": order_id_recoveries,
        "order_bill_rows": len(order_bills),
        "funding_bill_rows": len(funding_rows),
        "gross_close_pnl_ledger": gross_close_pnl,
        "order_bill_realized_pnl": order_realized,
        "order_fees": order_fees,
        "funding_net": funding_net,
        "net_pnl_after_direct_costs": net_pnl,
        "verified": not issues and not lifecycle.get("quantity_anomaly"),
        "verification_issues": issues,
    }


def build_report(
    *,
    account_db: Path,
    trades_db: Path,
    ledger_db: Path,
    cash_flow_receipt_dir: Path | None = None,
    cash_flow_forward_start: str = CASH_FLOW_FORWARD_ACTIVATION_CST,
    as_of: str,
    window_days: int = 30,
    minimum_closed_lifecycles: int = 100,
) -> dict[str, Any]:
    if window_days < 30:
        raise ValueError("window_days must be at least 30")
    if minimum_closed_lifecycles < 1:
        raise ValueError("minimum_closed_lifecycles must be positive")

    account = connect_ro(account_db)
    trades = connect_ro(trades_db)
    ledger = connect_ro(ledger_db)
    try:
        snapshot_window = load_snapshots(account, as_of, window_days)
        start_snapshot = snapshot_window["start"]
        end_snapshot = snapshot_window["end"]
        start_ts = str(start_snapshot["ts"])
        end_ts = str(end_snapshot["ts"])
        cash_flow_coverage = cash_flow_receipt_coverage(
            cash_flow_receipt_dir,
            start_ts,
            end_ts,
            forward_start=cash_flow_forward_start,
        )
        trade_rows = load_trade_rows(trades, end_ts)
        bills = load_bills(account, end_ts)
        intents = load_intents(ledger, end_ts)
    finally:
        account.close()
        trades.close()
        ledger.close()

    completed, anomalies, open_lifecycles = build_lifecycles(trade_rows)
    window_completed = [
        lifecycle for lifecycle in completed
        if parse_cst(start_ts) < parse_cst(lifecycle["closed_at"]) <= parse_cst(end_ts)
    ]
    window_anomalies = [
        item for item in anomalies
        if item.get("ts") and parse_cst(start_ts) < parse_cst(str(item["ts"])) <= parse_cst(end_ts)
    ]

    bills_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    funding_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bills:
        order_id = str(row.get("ord_id") or "").strip()
        if order_id:
            bills_by_order[order_id].append(row)
        if str(row.get("type") or "") == FUNDING_BILL_TYPE:
            funding_by_symbol[str(row.get("inst_id") or "")].append(row)
    intents_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    intents_by_trade_identity: dict[
        tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in intents:
        order_id = str(row.get("ord_id") or "").strip()
        if order_id:
            intents_by_order[order_id].append(row)
        intents_by_trade_identity[trade_intent_identity(row)].append(row)
    ambiguous_funding_ids = ambiguous_funding_lifecycle_ids(
        window_completed, funding_by_symbol)

    verified_rows = [
        verify_lifecycle(
            lifecycle,
            bills_by_order,
            funding_by_symbol,
            intents_by_order,
            intents_by_trade_identity,
            funding_ambiguous=(
                lifecycle["lifecycle_id"] in ambiguous_funding_ids),
        )
        for lifecycle in window_completed
    ]
    strategy_candidates = [row for row in verified_rows if not row["maintenance"]]
    strategy_verified = [row for row in strategy_candidates if row["verified"]]
    maintenance_rows = [row for row in verified_rows if row["maintenance"]]
    unverified = [row for row in strategy_candidates if not row["verified"]]
    lifecycle_bill_evidence_complete = (
        bool(verified_rows) and all(row["verified"] for row in verified_rows))

    wins = sum(row["net_pnl_after_direct_costs"] > 0 for row in strategy_verified)
    losses = sum(row["net_pnl_after_direct_costs"] < 0 for row in strategy_verified)
    flat = len(strategy_verified) - wins - losses
    win_rate = wins / len(strategy_verified) if strategy_verified else None
    wilson_low, wilson_high = wilson_interval(wins, len(strategy_verified))
    positive_pnl = sum(
        row["net_pnl_after_direct_costs"]
        for row in strategy_verified if row["net_pnl_after_direct_costs"] > 0)
    negative_pnl = sum(
        row["net_pnl_after_direct_costs"]
        for row in strategy_verified if row["net_pnl_after_direct_costs"] < 0)
    profit_factor = positive_pnl / abs(negative_pnl) if negative_pnl < 0 else None
    sorted_strategy = sorted(strategy_verified, key=lambda row: row["closed_at"])
    all_strategy_count = len(strategy_candidates)
    best_case_win_rate = (
        (wins + len(unverified)) / all_strategy_count
        if all_strategy_count else None)
    worst_case_win_rate = (
        wins / all_strategy_count if all_strategy_count else None)

    window_bills = [
        row for row in bills
        if parse_cst(start_ts) < parse_cst(str(row["ts"])) <= parse_cst(end_ts)
    ]
    bill_type_counts = Counter(str(row.get("type") or "") for row in window_bills)
    currency_counts = Counter(str(row.get("ccy") or "") for row in window_bills)
    unsupported_bill_types = sorted(set(bill_type_counts) - SUPPORTED_ACCOUNT_BILL_TYPES)
    non_usdt_currencies = sorted(ccy for ccy in currency_counts if ccy != "USDT")
    bill_type_classification_complete = not unsupported_bill_types and not non_usdt_currencies
    trading_bill_net_observed = sum(
        finite_number(row.get("bal_change")) or 0.0
        for row in window_bills
        if str(row.get("type") or "") in TRADING_ACCOUNT_BILL_TYPES
        and str(row.get("ccy") or "") == "USDT"
    )
    external_cash_flow_net_observed = sum(
        finite_number(row.get("bal_change")) or 0.0
        for row in window_bills
        if str(row.get("type") or "") == EXTERNAL_CASH_FLOW_BILL_TYPE
        and str(row.get("ccy") or "") == "USDT"
    )
    trade_bill_net = sum(
        finite_number(row.get("bal_change")) or 0.0
        for row in window_bills if str(row.get("type") or "") == "2"
    )
    funding_bill_net = sum(
        finite_number(row.get("bal_change")) or 0.0
        for row in window_bills if str(row.get("type") or "") == "8"
    )

    start_equity = finite_number(start_snapshot["totalEq"])
    end_equity = finite_number(end_snapshot["totalEq"])
    start_upl = finite_number(start_snapshot["upl"]) or 0.0
    end_upl = finite_number(end_snapshot["upl"]) or 0.0
    equity_delta = (
        end_equity - start_equity
        if start_equity is not None and end_equity is not None else None)
    account_bill_net = (
        trading_bill_net_observed if lifecycle_bill_evidence_complete else None)
    observed_expected_raw_equity_delta = (
        trading_bill_net_observed
        + external_cash_flow_net_observed
        + (end_upl - start_upl))
    observed_unexplained_equity_movement = (
        equity_delta - observed_expected_raw_equity_delta
        if equity_delta is not None else None)
    expected_adjusted_equity_delta = (
        account_bill_net + (end_upl - start_upl)
        if account_bill_net is not None else None)
    external_cash_flow_evidence_complete = (
        bill_type_classification_complete
        and lifecycle_bill_evidence_complete
        and cash_flow_coverage["complete"]
        and observed_unexplained_equity_movement is not None
        and abs(observed_unexplained_equity_movement) <= MONEY_TOLERANCE)
    # Use only observed type=1 transfer bills backed by continuous forward
    # query receipts.  Never infer a transfer from the equity residual.
    external_cash_flow_adjustment = (
        external_cash_flow_net_observed
        if external_cash_flow_evidence_complete else None)
    adjusted_equity_delta = (
        equity_delta - external_cash_flow_adjustment
        if equity_delta is not None and external_cash_flow_adjustment is not None
        else None)
    equity_reconciliation_gap = (
        adjusted_equity_delta - expected_adjusted_equity_delta
        if adjusted_equity_delta is not None
        and expected_adjusted_equity_delta is not None else None)

    snapshot_con = connect_ro(account_db)
    try:
        equity_rows = snapshot_con.execute(
            "SELECT ts,totalEq FROM account_snapshots WHERE profile='live' "
            "AND datetime(ts)>datetime(?) AND datetime(ts)<=datetime(?) "
            "AND totalEq IS NOT NULL ORDER BY datetime(ts)",
            (start_ts, end_ts),
        ).fetchall()
    finally:
        snapshot_con.close()
    equity_values = [float(row["totalEq"]) for row in equity_rows]
    equity_peak = equity_values[0] if equity_values else None
    equity_max_drawdown = 0.0
    if equity_peak is not None:
        for value in equity_values:
            equity_peak = max(equity_peak, value)
            equity_max_drawdown = max(equity_max_drawdown, equity_peak - value)

    duration_days = (
        parse_cst(end_ts) - parse_cst(start_ts)).total_seconds() / 86400.0
    verification_rate = (
        len(strategy_verified) / len(strategy_candidates)
        if strategy_candidates else None)
    evidence_requirements = {
        "window_at_least_30_days": duration_days >= 30.0,
        "minimum_verified_lifecycles_met": (
            len(strategy_verified) >= minimum_closed_lifecycles),
        "all_strategy_lifecycles_verified": (
            bool(strategy_candidates) and len(strategy_verified) == len(strategy_candidates)),
        "window_ledger_integrity_clean": not window_anomalies,
        "account_bill_lifecycle_coverage_complete": lifecycle_bill_evidence_complete,
        "bill_type_classification_complete": bill_type_classification_complete,
        "cash_flow_forward_receipt_coverage_complete": (
            cash_flow_coverage["complete"]),
        "external_cash_flow_evidence_complete": external_cash_flow_evidence_complete,
        "equity_reconciles_to_bills_and_unrealized": (
            equity_reconciliation_gap is not None
            and abs(equity_reconciliation_gap) <= MONEY_TOLERANCE),
    }
    diagnostic_signs = {
        "account_net_profit_positive": (
            account_bill_net is not None and account_bill_net > 0),
        "cash_flow_adjusted_equity_delta_positive": (
            adjusted_equity_delta is not None and adjusted_equity_delta > 0),
    }
    if not all(evidence_requirements.values()):
        diagnostic_evidence_status = "INSUFFICIENT_EVIDENCE"
    else:
        diagnostic_evidence_status = "COMPLETE"

    return {
        "schema_version": 1,
        "artifact_type": "live_profitability_lifecycle_diagnostic",
        "generated_at_cst": fmt_ts(now_cst()),
        "mode": "read_only_business_databases",
        "status": "DIAGNOSTIC_ONLY",
        "diagnostic_evidence_status": diagnostic_evidence_status,
        "goal_10_acceptance_authority": {
            "artifact": "weekly-trading-net-profit-audit.json",
            "script": "audit_weekly_trading_net_profit.py",
            "win_rate_required": False,
            "minimum_lifecycle_count_required": False,
            "account_equity_delta_required": False,
        },
        "definition": {
            "lifecycle_grain": (
                "one symbol+side position from ledger quantity zero through "
                "OPEN/ADD and REDUCE/CLOSE back to zero"),
            "strategy_win": "verified lifecycle net PnL after order fees and in-position funding > 0",
            "flat_trade": "net PnL equals 0 and is not a win",
            "maintenance_semantics": (
                "excluded from strategy win rate; real bills remain in account net profit"),
            "account_net_profit": (
                "sum of OKX account bill bal_change for supported trade/funding "
                "bill types, only claimable when every closed lifecycle is bill-verified"),
            "bill_history_limit": (
                "the collector persists current pages; lifecycle order-id coverage "
                "is required so missing historical pages cannot look like zero cost"),
            "equity_adjustment": (
                "end totalEq - start totalEq - observed type=1 transfers backed "
                "by continuous forward receipts; unrealized PnL is shown "
                "separately and reconciled"),
            "cash_flow_forward_activation_cst": fmt_ts(
                cash_flow_forward_start),
        },
        "diagnostic_configuration": {
            "acceptance_effect": "none_diagnostic_only",
            "minimum_window_days": window_days,
            "minimum_verified_closed_lifecycles": minimum_closed_lifecycles,
        },
        "window": {
            "requested_as_of_cst": snapshot_window["requested_as_of_cst"],
            "evidence_cutoff_cst": end_ts,
            "bill_latest_event_ts": snapshot_window["bill_latest_event_ts"],
            "bill_evidence_fetched_at": snapshot_window["bill_evidence_fetched_at"],
            "bill_evidence_lag_seconds": snapshot_window["evidence_lag_seconds"],
            "start_exclusive_cst": start_ts,
            "end_inclusive_cst": end_ts,
            "duration_days": duration_days,
            "baseline_fixed": True,
        },
        "lifecycle_profile": {
            "completed_in_window": len(window_completed),
            "strategy_candidates": len(strategy_candidates),
            "maintenance_lifecycles": len(maintenance_rows),
            "verified_strategy_lifecycles": len(strategy_verified),
            "unverified_strategy_lifecycles": len(unverified),
            "verification_rate": verification_rate,
            "open_lifecycles_at_cutoff": len(open_lifecycles),
            "window_ledger_anomalies": len(window_anomalies),
            "funding_attribution_ambiguous_lifecycles": len(
                ambiguous_funding_ids),
            "order_ids_recovered_from_exact_completed_intents": sum(
                len(row["order_id_recoveries"]) for row in verified_rows),
            "lifecycles_with_recovered_order_ids": sum(
                bool(row["order_id_recoveries"]) for row in verified_rows),
        },
        "strategy_performance": {
            "verified_sample_n": len(strategy_verified),
            "wins": wins,
            "losses": losses,
            "flat": flat,
            "win_rate": win_rate,
            "wilson_95_low": wilson_low,
            "wilson_95_high": wilson_high,
            "net_pnl_after_direct_costs": sum(
                row["net_pnl_after_direct_costs"] for row in strategy_verified),
            "gross_profit": positive_pnl,
            "gross_loss": negative_pnl,
            "profit_factor": profit_factor,
            "profit_factor_infinite": negative_pnl == 0 and positive_pnl > 0,
            "max_drawdown_usdt": max_drawdown(
                row["net_pnl_after_direct_costs"] for row in sorted_strategy),
            "unverified_outcome_sensitivity": {
                "all_unverified_assumed_losses_win_rate": worst_case_win_rate,
                "all_unverified_assumed_wins_win_rate": best_case_win_rate,
            },
        },
        "policy_epoch_diagnostic": policy_epoch_diagnostic(
            strategy_verified,
            minimum_closed_lifecycles=minimum_closed_lifecycles,
        ),
        "maintenance_real_cost": {
            "verified_lifecycles": sum(row["verified"] for row in maintenance_rows),
            "unverified_lifecycles": sum(not row["verified"] for row in maintenance_rows),
            "verified_net_pnl_after_direct_costs": sum(
                row["net_pnl_after_direct_costs"]
                for row in maintenance_rows if row["verified"]),
            "included_in_account_net_profit": True,
        },
        "account_economics": {
            "start_snapshot": start_snapshot,
            "end_snapshot": end_snapshot,
            "latest_account_snapshot": snapshot_window["latest_account"],
            "account_bill_rows": len(window_bills),
            "bill_type_counts": dict(sorted(bill_type_counts.items())),
            "currency_counts": dict(sorted(currency_counts.items())),
            "unsupported_or_unclassified_bill_types": unsupported_bill_types,
            "non_usdt_currencies": non_usdt_currencies,
            "bill_type_classification_complete": bill_type_classification_complete,
            "cash_flow_forward_coverage": cash_flow_coverage,
            "external_cash_flow_evidence_complete": external_cash_flow_evidence_complete,
            "trade_bill_net": trade_bill_net,
            "funding_bill_net": funding_bill_net,
            "account_net_profit_observed_in_local_bill_rows": (
                trading_bill_net_observed),
            "account_bill_lifecycle_coverage_complete": lifecycle_bill_evidence_complete,
            "account_net_profit": account_bill_net,
            "external_cash_flow_net_observed": external_cash_flow_net_observed,
            "external_cash_flow_adjustment": external_cash_flow_adjustment,
            "raw_equity_delta": equity_delta,
            "cash_flow_adjusted_equity_delta": adjusted_equity_delta,
            "start_unrealized_pnl": start_upl,
            "end_unrealized_pnl": end_upl,
            "unrealized_pnl_delta": end_upl - start_upl,
            "expected_raw_equity_delta_from_observed_bills_flows_plus_upl": (
                observed_expected_raw_equity_delta),
            "observed_unexplained_equity_movement": (
                observed_unexplained_equity_movement),
            "expected_adjusted_equity_delta_from_bills_plus_upl": (
                expected_adjusted_equity_delta),
            "equity_reconciliation_gap": equity_reconciliation_gap,
            "equity_max_drawdown_usdt": equity_max_drawdown,
        },
        "requirements": {"evidence": evidence_requirements},
        "diagnostic_signs": diagnostic_signs,
        "unverified_examples": [
            {
                "lifecycle_id": row["lifecycle_id"],
                "symbol": row["symbol"],
                "side": row["side"],
                "started_at": row["started_at"],
                "closed_at": row["closed_at"],
                "issues": row["verification_issues"],
            }
            for row in unverified[:25]
        ],
        "ledger_anomaly_examples": window_anomalies[:25],
        "safety": {
            "business_databases_read_only": True,
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
        description="read-only live lifecycle profitability diagnostic")
    parser.add_argument("--account-db", default=str(root / "db" / "account.db"))
    parser.add_argument("--trades-db", default=str(root / "db" / "live_trades.db"))
    parser.add_argument("--ledger-db", default=str(root / "db" / "ledger.db"))
    parser.add_argument(
        "--cash-flow-receipt-dir",
        default=str(root / "reports" / "quality" / "account-cash-flow-forward"))
    parser.add_argument(
        "--cash-flow-forward-start",
        default=CASH_FLOW_FORWARD_ACTIVATION_CST)
    parser.add_argument("--as-of", default=fmt_ts(now_cst()))
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--minimum-closed-lifecycles", type=int, default=100)
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    try:
        report = build_report(
            account_db=Path(args.account_db),
            trades_db=Path(args.trades_db),
            ledger_db=Path(args.ledger_db),
            cash_flow_receipt_dir=Path(args.cash_flow_receipt_dir),
            cash_flow_forward_start=args.cash_flow_forward_start,
            as_of=args.as_of,
            window_days=args.window_days,
            minimum_closed_lifecycles=args.minimum_closed_lifecycles,
        )
        if args.json_out:
            atomic_write_json(Path(args.json_out), report)
        print(json.dumps({
            "ok": True,
            "status": report["status"],
            "diagnostic_evidence_status": report["diagnostic_evidence_status"],
            "window": report["window"],
            "lifecycle_profile": report["lifecycle_profile"],
            "strategy_performance": report["strategy_performance"],
            "account_economics": report["account_economics"],
            "json_out": args.json_out,
            "orders_placed": 0,
            "production_database_writes": 0,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI must produce one clear failure
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "orders_placed": 0,
            "production_database_writes": 0,
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
