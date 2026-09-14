"""Bounded continuation of the existing exact-close recovery owner.

Each child remains an independently validated, at-most-three-group write.
Only a proven pure-close backlog with new committed order identities permits
another fresh read. No exchange writes, cycle replays, or authority overrides.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

ACTIVATION_CYCLE = "2026-09-13T18:15"
MAX_ATTEMPTS = 3
MIN_CONTINUATION_SECONDS = 45.0
CST = timezone(timedelta(hours=8))


def enabled(cycle: str | None) -> bool:
    try:
        value = datetime.strptime(str(cycle), "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return False
    return value.minute % 15 == 0 and str(cycle) >= ACTIVATION_CYCLE


def _committed_close_ids(result: dict) -> set[str] | None:
    findings = result.get("findings")
    healed = result.get("healed")
    backlog = result.get("backlog")
    if not (
        result.get("contract_version") == 1
        and result.get("status") == "needs_human"
        and type(result.get("rc")) is int and result["rc"] == 1
        and result.get("apply") is True and result.get("applied") is True
        and result.get("blocking") is True and result.get("p0") is False
        and result.get("unrecorded_count") == 0
        and isinstance(findings, list) and findings
        and all(isinstance(f, dict) and f.get("kind") == "AUTOHEAL-BACKLOG"
                and str(f.get("sev", "")).upper() != "P0" for f in findings)
        and isinstance(healed, list) and 1 <= len(healed) <= 3
        and isinstance(backlog, dict) and backlog.get("cap") == 3
        and type(backlog.get("remaining_exact_count")) is int
        and backlog["remaining_exact_count"] > 0
        and backlog.get("evidence_deferred_count") == 0
    ):
        return None
    ids: set[str] = set()
    for item in healed:
        if not isinstance(item, dict):
            return None
        try:
            size = float(item.get("sz"))
        except (ValueError, TypeError):
            return None
        order_ids = item.get("ord_ids")
        if not (
            item.get("kind") == "GHOST-EXACT" and item.get("applied") is True
            and item.get("side") in {"long", "short"}
            and isinstance(item.get("symbol"), str) and item["symbol"]
            and math.isfinite(size) and size > 0
            and isinstance(order_ids, list) and order_ids
            and all(re.fullmatch(r"[1-9][0-9]*", str(x)) for x in order_ids)
            and len({str(x) for x in order_ids}) == len(order_ids)
            and not ids.intersection(str(x) for x in order_ids)
        ):
            return None
        ids.update(str(x) for x in order_ids)
    return ids


def recover_in_budget(run_once: Callable[[float], dict], *, cycle: str,
                      verify_once: Callable[[float], dict] | None = None,
                      timeout_sec: float = 180.0,
                      clock: Callable[[], float] | None = None,
                      now: datetime | None = None) -> dict:
    """Return the final child contract plus separate chain evidence.

    Final child fields are never promoted: a remaining backlog/error still
    blocks. The caller must independently refresh positions and ledger before
    admitting a trade, including when another writer finished the work.
    """
    from scripts import _acceptance_thresholds as thresholds
    clock = clock or time.monotonic
    now = now or datetime.now(CST)
    stop_at = datetime.strptime(cycle, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    stop_at += timedelta(seconds=thresholds.sla_business_terminal_deadline_seconds(cycle))
    budget = max(0.0, min(float(timeout_sec), (stop_at-now).total_seconds()))
    started = clock(); deadline = started+budget
    attempts = []; seen_orders: set[str] = set(); seen_requests: set[str] = set()
    last: dict = {}; reason = "attempt_limit"
    for index in range(MAX_ATTEMPTS):
        remaining = max(0.0, deadline-clock())
        if index and remaining < MIN_CONTINUATION_SECONDS:
            reason = "remaining_budget_insufficient"; break
        last = run_once(remaining)
        attempts.append({k: last.get(k) for k in (
            "request_id", "status", "rc", "applied", "blocking", "p0", "healed")})
        if last.get("blocking") is False:
            reason = "converged"; break
        ids = _committed_close_ids(last)
        request = str(last.get("request_id") or "")
        if ids is None:
            reason = "not_a_committed_pure_close_backlog"; break
        if not request or request in seen_requests or ids.intersection(seen_orders):
            reason = "no_new_committed_progress"; break
        seen_requests.add(request); seen_orders.update(ids)
    applied_any = any(a.get("applied") is True for a in attempts)
    verified_after_write = False
    if (verify_once is not None and applied_any and last.get("blocking") is False
            and last.get("status") in {"ok", "applied"}):
        last = verify_once(max(0.0, deadline-clock()))
        attempts.append({**{k: last.get(k) for k in (
            "request_id", "status", "rc", "applied", "blocking", "p0", "healed")},
            "verification_only": True})
        verified_after_write = bool(
            last.get("apply") is False and last.get("applied") is False
            and last.get("status") == "ok" and last.get("rc") == 0
            and last.get("blocking") is False and last.get("p0") is False)
        reason = "converged_and_reverified" if verified_after_write else "post_write_verification_blocked"
    return {**last, "recovery_chain": {
        "schema_version": 1, "activation_cycle": ACTIVATION_CYCLE,
        "attempts": attempts, "max_mutating_attempts": MAX_ATTEMPTS,
        "max_verification_attempts": 1,
        "per_attempt_max_heals": 3, "budget_seconds": budget,
        "elapsed_seconds": round(clock()-started, 3), "stop_reason": reason,
        "applied_any": applied_any,
        "verified_after_write": verified_after_write,
        "fresh_request_per_attempt": True,
    }}
