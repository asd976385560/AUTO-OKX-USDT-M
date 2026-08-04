# -*- coding: utf-8 -*-
"""Durable execution-intent idempotency for order side effects.

The order executor reserves one logical OPEN before any exchange write.  A
completed intent returns its stored receipt on an identical retry; in-flight or
ambiguous intents are fail-closed and require reconciliation instead of a
second order.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional


TERMINAL_STATES = frozenset({"completed", "failed_clean", "reconciled"})


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_intents (
    profile             TEXT NOT NULL,
    cycle_id            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    action              TEXT NOT NULL,
    side                TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    request_json        TEXT NOT NULL,
    state               TEXT NOT NULL,
    reserved_at         TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    submitted_at        TEXT,
    completed_at        TEXT,
    ord_id              TEXT,
    receipt_json        TEXT,
    error               TEXT,
    PRIMARY KEY (profile, cycle_id, symbol, action, side)
);
CREATE INDEX IF NOT EXISTS idx_execution_intents_state
    ON execution_intents(state, updated_at);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=8)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


def ensure_schema(path: Path) -> None:
    con = _connect(path)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def canonical_request(payload: dict[str, Any]) -> tuple[str, str]:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), raw


def _key(profile: str, cycle_id: str, symbol: str, side: str) -> tuple[str, ...]:
    return (profile, cycle_id, symbol, "open", side)


def _pending_profile_intents(
    con: sqlite3.Connection,
    profile: str,
) -> list[sqlite3.Row]:
    """同一 profile 的全局未决 intent；未知状态也按未决 fail-closed。"""
    return con.execute(
        "SELECT profile,cycle_id,symbol,action,side,state,updated_at,ord_id,error "
        "FROM execution_intents WHERE profile=? "
        "AND state NOT IN ('completed','failed_clean','reconciled') "
        "ORDER BY updated_at,cycle_id,symbol,action,side",
        (profile,),
    ).fetchall()


def _blocking_result(rows: list[sqlite3.Row]) -> dict[str, Any]:
    first = rows[0]
    blocker = {key: first[key] for key in first.keys()}
    return {
        "status": "blocked",
        "state": str(first["state"]),
        "reason": "profile_pending_intent",
        "ord_id": first["ord_id"],
        "blocking_intent": blocker,
        "pending_count": len(rows),
    }


def reserve(
    path: Path,
    *,
    profile: str,
    cycle_id: str,
    symbol: str,
    side: str,
    request: dict[str, Any],
    now_ts: str,
) -> dict[str, Any]:
    """Reserve an OPEN or return a stored receipt / fail-closed conflict."""
    fingerprint, request_json = canonical_request(request)
    con = _connect(path)
    try:
        con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM execution_intents WHERE profile=? AND cycle_id=? "
            "AND symbol=? AND action=? AND side=?",
            _key(profile, cycle_id, symbol, side),
        ).fetchone()
        if row is None:
            pending = _pending_profile_intents(con, profile)
            if pending:
                con.commit()
                return _blocking_result(pending)
            con.execute(
                "INSERT INTO execution_intents "
                "(profile,cycle_id,symbol,action,side,request_fingerprint,"
                "request_json,state,reserved_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (*_key(profile, cycle_id, symbol, side), fingerprint,
                 request_json, "reserved", now_ts, now_ts),
            )
            con.commit()
            return {"status": "reserved", "fingerprint": fingerprint}

        state = str(row["state"])
        same = str(row["request_fingerprint"]) == fingerprint
        if state == "completed" and same and row["receipt_json"]:
            try:
                receipt = json.loads(row["receipt_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                con.rollback()
                return {
                    "status": "blocked",
                    "state": state,
                    "reason": f"stored_receipt_invalid:{type(exc).__name__}",
                    "ord_id": row["ord_id"],
                }
            con.commit()
            return {
                "status": "replay",
                "state": state,
                "fingerprint": fingerprint,
                "receipt": receipt,
                "ord_id": row["ord_id"],
            }

        if state == "failed_clean":
            # failed_clean 本身是 terminal，可复用；但其他标的只要有未决 intent，
            # 本次仍不得重新进入 reserved。
            pending = _pending_profile_intents(con, profile)
            if pending:
                con.commit()
                return _blocking_result(pending)
            con.execute(
                "UPDATE execution_intents SET request_fingerprint=?,"
                "request_json=?,state='reserved',reserved_at=?,updated_at=?,"
                "submitted_at=NULL,completed_at=NULL,ord_id=NULL,"
                "receipt_json=NULL,error=NULL "
                "WHERE profile=? AND cycle_id=? AND symbol=? AND action=? AND side=?",
                (fingerprint, request_json, now_ts, now_ts,
                 *_key(profile, cycle_id, symbol, side)),
            )
            con.commit()
            return {"status": "reserved", "fingerprint": fingerprint,
                    "reused_failed_clean": True}

        if state == "reconciled":
            # The exchange fill and ledger row were recovered after the original
            # executor lost its receipt.  This key must never place another order,
            # but it is terminal and therefore must not freeze the whole profile.
            pending = _pending_profile_intents(con, profile)
            if pending:
                con.commit()
                return _blocking_result(pending)
            con.commit()
            return {
                "status": "blocked",
                "state": state,
                "reason": "intent_reconciled",
                "ord_id": row["ord_id"],
                "same_fingerprint": same,
                "pending_count": 0,
            }

        con.commit()
        return {
            "status": "blocked",
            "state": state,
            "reason": "request_conflict" if not same else "intent_in_flight",
            "ord_id": row["ord_id"],
            "same_fingerprint": same,
            "blocking_intent": {
                "profile": row["profile"],
                "cycle_id": row["cycle_id"],
                "symbol": row["symbol"],
                "action": row["action"],
                "side": row["side"],
                "state": row["state"],
                "updated_at": row["updated_at"],
                "ord_id": row["ord_id"],
                "error": row["error"],
            },
            "pending_count": 1 if state not in TERMINAL_STATES else 0,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _transition(
    path: Path,
    *,
    profile: str,
    cycle_id: str,
    symbol: str,
    side: str,
    fingerprint: str,
    state: str,
    now_ts: str,
    ord_id: Optional[str] = None,
    receipt: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    receipt_json = None
    if receipt is not None:
        receipt_json = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, allow_nan=False)
    submitted_at = now_ts if state in ("submitted", "uncertain") else None
    completed_at = now_ts if state in ("completed", "reconciled") else None
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            "UPDATE execution_intents SET state=?,updated_at=?,"
            "submitted_at=COALESCE(?,submitted_at),"
            "completed_at=COALESCE(?,completed_at),"
            "ord_id=COALESCE(?,ord_id),"
            "receipt_json=COALESCE(?,receipt_json),error=? "
            "WHERE profile=? AND cycle_id=? AND symbol=? AND action=? AND side=? "
            "AND request_fingerprint=?",
            (state, now_ts, submitted_at, completed_at, ord_id, receipt_json,
             error, *_key(profile, cycle_id, symbol, side), fingerprint),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"execution intent transition lost: {profile}/{cycle_id}/"
                f"{symbol}/open/{side} -> {state}")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_submitting(path: Path, **kwargs: Any) -> None:
    _transition(path, state="submitting", **kwargs)


def mark_submitted(path: Path, **kwargs: Any) -> None:
    _transition(path, state="submitted", **kwargs)


def mark_completed(path: Path, **kwargs: Any) -> None:
    _transition(path, state="completed", **kwargs)


def mark_failed_clean(path: Path, **kwargs: Any) -> None:
    _transition(path, state="failed_clean", **kwargs)


def mark_reconciled(path: Path, **kwargs: Any) -> None:
    """Close an intent whose exchange fill and ledger row were reconciled later."""
    _transition(path, state="reconciled", **kwargs)


def mark_uncertain(path: Path, **kwargs: Any) -> None:
    _transition(path, state="uncertain", **kwargs)
