# -*- coding: utf-8 -*-
"""Durable execution-intent idempotency for order side effects.

The order executor reserves one logical side effect before any exchange write.
A completed intent returns its stored receipt on an identical retry; in-flight
or ambiguous intents are fail-closed and require reconciliation instead of a
second order.  ``action='open'`` remains the backward-compatible default.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


TERMINAL_STATES = frozenset({"completed", "failed_clean"})
RESERVED_OPEN_EXPIRY_FROM = "2026-09-05T13:30"
EXPIRED_RESERVATION_ERROR = "expired_before_exchange_submission"
_CST = timezone(timedelta(hours=8))


def _clock_cst() -> datetime:
    return datetime.now(_CST)


def _cycle_time(value: str) -> datetime | None:
    try:
        stamp = datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=_CST)
        return stamp if stamp.minute % 15 == 0 and stamp.strftime("%Y-%m-%dT%H:%M") == value else None
    except (TypeError, ValueError):
        return None


def _expire_reserved_opens(con, profile: str, before_cycle: str) -> list[dict]:
    """CAS-close expired pre-submit reservations in the current transaction.

    Real submission requires reserved->submitting to commit first. If this CAS
    wins, the old executor's submit transition fails and no order can be sent.
    Submitting/submitted/uncertain records and old storage epochs remain blocked.
    """
    requested = _cycle_time(before_cycle)
    now = _clock_cst()
    if requested is None or requested > now or before_cycle < RESERVED_OPEN_EXPIRY_FROM:
        return []
    released = []
    for row in con.execute(
            "SELECT * FROM execution_intents WHERE profile=? AND action='open' "
            "AND state='reserved' AND submitted_at IS NULL AND ord_id IS NULL "
            "AND cycle_id>=? AND cycle_id<?",
            (profile, RESERVED_OPEN_EXPIRY_FROM, before_cycle)).fetchall():
        cycle = _cycle_time(row["cycle_id"])
        try:
            reserved = datetime.strptime(row["reserved_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST)
        except (TypeError, ValueError):
            continue
        if cycle is None or not cycle <= reserved < cycle + timedelta(minutes=15) or now < cycle + timedelta(minutes=15):
            continue
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        receipt = json.dumps({"ok": False, "cycle_id": row["cycle_id"],
                              "action_taken": "REJECT", "n_orders": 0,
                              "reject_reason": EXPIRED_RESERVATION_ERROR}, sort_keys=True)
        changed = con.execute(
            "UPDATE execution_intents SET state='failed_clean',updated_at=?,completed_at=?,"
            "receipt_json=?,error=? WHERE profile=? AND cycle_id=? AND symbol=? AND action='open' "
            "AND side=? AND request_fingerprint=? AND state='reserved' "
            "AND submitted_at IS NULL AND ord_id IS NULL",
            (stamp,stamp,receipt,EXPIRED_RESERVATION_ERROR,row["profile"],row["cycle_id"],
             row["symbol"],row["side"],row["request_fingerprint"])).rowcount
        if changed:
            released.append({k:row[k] for k in ("profile","cycle_id","symbol","side","request_fingerprint")})
    return released


def release_expired_reserved_opens(path: Path, *, profile: str, before_cycle: str) -> list[dict]:
    """Maintenance entry for the same strictly pre-submission CAS recovery."""
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        released = _expire_reserved_opens(con, profile, before_cycle)
        con.commit()
        return released
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

# Forward-only state graph.  ``completed`` retains the legacy direct
# reserved->completed path used by deterministic synchronous drills, while all
# transitions reject terminal-state rollback and out-of-order rewrites.
ALLOWED_PRIOR_STATES = {
    "submitting": frozenset({"reserved"}),
    "submitted": frozenset({"submitting"}),
    "completed": frozenset({"reserved", "submitting", "submitted"}),
    "failed_clean": frozenset({"reserved", "submitting", "submitted"}),
    "uncertain": frozenset({"submitting", "submitted"}),
}


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


def _key(
    profile: str,
    cycle_id: str,
    symbol: str,
    side: str,
    action: str = "open",
) -> tuple[str, ...]:
    return (profile, cycle_id, symbol, action, side)


def _pending_profile_intents(
    con: sqlite3.Connection,
    profile: str,
) -> list[sqlite3.Row]:
    """同一 profile 的全局未决 intent；未知状态也按未决 fail-closed。"""
    return con.execute(
        "SELECT profile,cycle_id,symbol,action,side,state,updated_at,ord_id,error "
        "FROM execution_intents WHERE profile=? "
        "AND state NOT IN ('completed','failed_clean') "
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
    action: str = "open",
) -> dict[str, Any]:
    """Reserve one action or return a stored receipt / fail-closed conflict."""
    fingerprint, request_json = canonical_request(request)
    con = _connect(path)
    try:
        con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        _expire_reserved_opens(con, profile, cycle_id)
        row = con.execute(
            "SELECT * FROM execution_intents WHERE profile=? AND cycle_id=? "
            "AND symbol=? AND action=? AND side=?",
            _key(profile, cycle_id, symbol, side, action),
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
                (*_key(profile, cycle_id, symbol, side, action), fingerprint,
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
            if row["error"] == EXPIRED_RESERVATION_ERROR:
                con.commit()
                return {"status": "blocked", "state": state,
                        "reason": EXPIRED_RESERVATION_ERROR, "ord_id": row["ord_id"]}
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
                 *_key(profile, cycle_id, symbol, side, action)),
            )
            con.commit()
            return {"status": "reserved", "fingerprint": fingerprint,
                    "reused_failed_clean": True}

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
    action: str = "open",
    ord_id: Optional[str] = None,
    receipt: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    allowed_prior = ALLOWED_PRIOR_STATES.get(state)
    if not allowed_prior:
        raise ValueError(f"unsupported execution intent state: {state!r}")
    receipt_json = None
    if receipt is not None:
        receipt_json = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, allow_nan=False)
    # Some synchronous exchange mutations go directly from ``submitting`` to
    # ``completed`` after readback.  A completed intent must still prove that
    # an exchange side effect was submitted (protection algo amendments are
    # the main example).
    submitted_at = (
        now_ts if state in ("submitted", "uncertain", "completed") else None
    )
    completed_at = now_ts if state == "completed" else None
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in sorted(allowed_prior))
        cur = con.execute(
            "UPDATE execution_intents SET state=?,updated_at=?,"
            "submitted_at=COALESCE(?,submitted_at),"
            "completed_at=COALESCE(?,completed_at),"
            "ord_id=COALESCE(?,ord_id),"
            "receipt_json=COALESCE(?,receipt_json),error=? "
            "WHERE profile=? AND cycle_id=? AND symbol=? AND action=? AND side=? "
            "AND request_fingerprint=? "
            f"AND state IN ({placeholders})",
            (state, now_ts, submitted_at, completed_at, ord_id, receipt_json,
             error, *_key(profile, cycle_id, symbol, side, action), fingerprint,
             *sorted(allowed_prior)),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"execution intent transition lost: {profile}/{cycle_id}/"
                f"{symbol}/{action}/{side} -> {state}")
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


def mark_uncertain(path: Path, **kwargs: Any) -> None:
    _transition(path, state="uncertain", **kwargs)




def _reconcile_booked_protected_open(
    path: Path, *, expected_row: dict, proof: dict,
    apply: bool = False, backup_path: Path | None = None,
    submitted_recovery: bool = False, prelaunch_cycle: str | None = None,
) -> dict:
    """Owner CAS of a booked/filled/protected OPEN after local UNWIND rejection.

    The normal state graph is unchanged. This never accepts an uncertain
    exchange close, an ADD, a partial fill, or a different position epoch.
    It writes only this exact execution-intent row, never trade/cycle rows.
    """
    from contextlib import closing
    from decimal import Decimal

    def require(ok, reason):
        if not ok: raise ValueError(reason)
    def number(value):
        n = Decimal(str(value)); require(n.is_finite(), "nonfinite number")
        return n
    def same(a, b):
        return abs(number(a)-number(b)) <= max(Decimal("0.000000001"),abs(number(b))*Decimal("0.000000001"))
    old = dict(expected_row)
    require(old.get("profile")=="live" and old.get("action")=="open" and old.get("ord_id"), "unsupported recovery identity")
    if submitted_recovery:
        require(old.get("state")=="submitted" and old.get("error") is None and old.get("receipt_json") is None,
                "only an identified submitted OPEN without a terminal receipt is eligible")
    else:
        require(old.get("state")=="uncertain" and old.get("error")=="naked_position_unwind_failed", "unsupported recovery state")
    request = json.loads(old["request_json"])
    require(canonical_request(request)[0]==old["request_fingerprint"], "request fingerprint mismatch")
    require(all(request.get(k)==old.get(k) for k in ("profile","cycle_id","symbol","action","side")), "request identity mismatch")
    require(request.get("expected_pre_position_exists") is False and request.get("expected_pre_position_sz")==0,
            "only a new OPEN from confirmed flat is eligible")
    now = _clock_cst(); now_ms=now.timestamp()*1000
    require(0 <= now_ms-float(proof.get("read_started_at_ms",0)) <= 30000
            and 0 <= now_ms-float(proof.get("verified_at_ms",0)) <= 30000, "stale proof")
    cycle = _cycle_time(old["cycle_id"])
    require(cycle is not None and now >= cycle+timedelta(minutes=15), "cycle still active")
    if submitted_recovery:
        submitted=datetime.strptime(str(old.get("submitted_at")),"%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST)
        require(now >= submitted+timedelta(minutes=15), "submitted OPEN is still within its settling window")
    root = Path(path).resolve().parent.parent
    if not submitted_recovery:
        failed_path = root/"tmp"/("_receipt_live_"+old["cycle_id"].replace(":","-")+".json")
        raw_failed = failed_path.read_bytes()
        require(hashlib.sha256(raw_failed).hexdigest()==proof.get("failed_receipt_sha256"), "failure receipt changed")
        failure = json.loads(raw_failed)
        failures = [x for x in failure.get("position_action_failures",[])
                    if isinstance(x,dict) and (x.get("request") or {}).get("symbol")==old["symbol"]
                    and (x.get("request") or {}).get("side")==old["side"]]
        require(failure.get("cycle_id")==old["cycle_id"] and len(failures)==1, "one exact historical failure required")
        failed_result=failures[0].get("result") or {}; unwind=failed_result.get("unwind") or {}
        require(failed_result.get("reject_reason")=="naked_position_unwind_failed"
                and unwind.get("reject_reason")=="receipt_context_invalid"
                and "非 OPEN/ADD context 禁止携带 open_execution_package" in str(unwind.get("reject_detail") or ""),
                "UNWIND must be proven locally rejected before exchange I/O")
    stage=json.loads((root/"logs/stage-status"/("live-"+old["cycle_id"].replace(":","-")+".json")).read_text(encoding="utf-8"))
    require(stage.get("cycle_id")==old["cycle_id"] and stage.get("status")=="failed"
            and stage.get("profile_lease_released") is True, "original failed stage not released")
    symbol, side, oid = old["symbol"], old["side"], str(old["ord_id"])
    order=proof.get("order") or {}
    require(order.get("instId")==symbol and str(order.get("ordId"))==oid and order.get("posSide")==side
            and order.get("side")==("buy" if side=="long" else "sell") and order.get("state")=="filled"
            and str(order.get("reduceOnly")).lower()=="false" and same(order.get("accFillSz"),request["intended_sz"])
            and same(order.get("sz"),request["intended_sz"]), "exact fully filled OPEN required")
    fills=proof.get("fills");require(isinstance(fills,list) and fills, "exact fills required")
    ids=set();quantity=Decimal(0);quote=Decimal(0)
    for f in fills:
        fid=str(f.get("tradeId") or "");size=number(f["fillSz"]);price=number(f["fillPx"]);stamp=number(f["fillTime"])
        require(f.get("instId")==symbol and str(f.get("ordId"))==oid and f.get("posSide")==side
                and f.get("side")==order["side"] and fid and fid not in ids and size>0 and price>0
                and number(order["cTime"])<=stamp<=number(order["uTime"]), "fill identity/time/economics mismatch")
        ids.add(fid);quantity+=size;quote+=size*price
    require(same(quantity,order["accFillSz"]) and same(quote/quantity,order["avgPx"]), "fill aggregate mismatch")
    positions=[]
    for key in ("positions_before","positions_after"):
        rows=proof.get(key);require(isinstance(rows,list), "two position readbacks required")
        matches=[p for p in rows if p.get("instId")==symbol and p.get("posSide")==side and number(p.get("pos") or 0)!=0]
        require(len(matches)==1, "position absent or ambiguous")
        p=matches[0]
        require(same(p["pos"],quantity) and same(p["avgPx"],order["avgPx"]) and p.get("posId")
                and abs(number(p["cTime"])-number(order["uTime"]))<=2000, "different position epoch/size/price")
        positions.append(p)
    require(all(positions[0].get(k)==positions[1].get(k) for k in ("posId","cTime","pos","avgPx")), "position changed")
    instrument=proof.get("instrument") or {}
    require(instrument.get("instId")==symbol and instrument.get("state")=="live" and instrument.get("instType")=="SWAP", "instrument identity missing")
    tick=number(instrument["tickSz"]);require(tick>0, "invalid tick")
    algos=proof.get("algos");require(isinstance(algos,list), "complete protection readback required")
    if submitted_recovery:
        from collections import Counter
        require(max(Counter(a.get("ordType") for a in algos).values(),default=0)<100,"protection pagination incomplete")
    valid=[]
    for a in algos:
        if a.get("instId")!=symbol or a.get("posSide")!=side or not a.get("slTriggerPx"):continue
        sl=number(a["slTriggerPx"]);wanted=number(request["sl_trigger_px"])
        tighter=(wanted<=sl if side=="long" else sl<=wanted)
        direction=(sl<number(positions[-1]["markPx"]) if side=="long" else sl>number(positions[-1]["markPx"]))
        require(a.get("state")=="live" and str(a.get("reduceOnly")).lower()=="true"
                and a.get("side")==("sell" if side=="long" else "buy") and same(a["sz"],quantity)
                and number(a["cTime"])>=number(order["cTime"]) and sl%tick==0 and tighter
                and abs(sl-wanted)<tick and direction and a.get("algoId"), "stop not exact/full/valid or looser than requested")
        valid.append(a)
    require(valid and len({str(a["algoId"]) for a in valid})==len(valid), "full protective SL required")
    if submitted_recovery: require(len(valid)==1,"one unique full protective SL required")
    with closing(sqlite3.connect((Path(path).resolve().parent/"live_trades.db").as_uri()+"?mode=ro",uri=True,timeout=3)) as con:
        con.row_factory=sqlite3.Row;con.execute("PRAGMA query_only=ON");recorded=[]
        for r in con.execute("SELECT * FROM trades WHERE cycle_id=? AND symbol=? AND action='open' AND side=?",(old["cycle_id"],symbol,side)):
            raw=json.loads(r["raw"] or "{}");order_ids={str(x) for x in raw.get("ord_ids",[]) if x}
            for key in ("ordId","ord_id","exchange_order_id"):
                if raw.get(key):order_ids.add(str(raw[key]))
            if oid in order_ids:
                require(order_ids=={oid} and same(r["sz"],quantity) and same(r["fill_px"],order["avgPx"]), "ledger conflicts with exchange")
                recorded.append(dict(r))
        require(len(recorded)==1, "one already-booked exact OPEN required")
    trade={**json.loads(recorded[0]["raw"]),**{k:recorded[0][k] for k in ("cycle_id","ts","symbol","action","side","sz","fill_px")}}
    receipt={"ok":True,"profile":"live","mode":"live","status":"ok","cycle_id":old["cycle_id"],
             "symbol":symbol,"side":side,"action_taken":"OPEN_LONG" if side=="long" else "OPEN_SHORT",
             "decision":"traded","n_orders":1,"ord_id":oid,"trades":[trade],"sl_verified":True,
             "reconciliation":{"kind":"owner_verified_submitted_booked_open" if submitted_recovery else "owner_verified_filled_protected_open","previous_intent":old,
               "ledger_trade_id":recorded[0]["id"],"proof":proof,"historical_failed_cycle_rewritten":False,"trade_rows_written":0}}
    if submitted_recovery:
        wanted_tp=request.get("tp_trigger_px")
        tp=[a for a in algos if a.get("instId")==symbol and a.get("posSide")==side
            and a.get("state")=="live" and str(a.get("reduceOnly")).lower()=="true"
            and a.get("side")==("sell" if side=="long" else "buy") and a.get("tpTriggerPx")
            and wanted_tp is not None and same(a["tpTriggerPx"],wanted_tp) and same(a.get("sz"),quantity)]
        receipt["tp_verified"]=bool(tp)
        if wanted_tp is not None and not tp: receipt["protection_warning"]="original_tp_not_verified"
    encoded=json.dumps(receipt,ensure_ascii=False,sort_keys=True,allow_nan=False)
    answer={"ok":True,"ord_id":oid,"filled_size":float(quantity),"sl_ids":[a["algoId"] for a in valid],
            "trade_rows_written":0,"exchange_orders_sent":0,"dry_run":not apply}
    if not apply:return {**answer,"would_set_state":"completed"}
    require(backup_path is not None and Path(backup_path).is_file() and Path(backup_path).resolve()!=Path(path).resolve(), "separate verified backup required")
    key=_key(old["profile"],old["cycle_id"],symbol,side,old["action"])
    with closing(sqlite3.connect(Path(backup_path).resolve().as_uri()+"?mode=ro",uri=True,timeout=3)) as con:
        con.row_factory=sqlite3.Row
        saved=con.execute("SELECT * FROM execution_intents WHERE profile=? AND cycle_id=? AND symbol=? AND action=? AND side=?",key).fetchone()
        require(saved is not None and dict(saved)==old, "backup intent mismatch")
    con=_connect(Path(path))
    try:
        con.execute("BEGIN IMMEDIATE")
        actual=con.execute("SELECT * FROM execution_intents WHERE profile=? AND cycle_id=? AND symbol=? AND action=? AND side=?",key).fetchone()
        require(actual is not None and dict(actual)==old, "intent changed before CAS")
        stamp=_clock_cst().strftime("%Y-%m-%d %H:%M:%S")
        leases=con.execute("SELECT cycle_id FROM stage_profile_leases WHERE profile='live' AND expires_at>?" if submitted_recovery else
                           "SELECT 1 FROM stage_profile_leases WHERE profile='live' AND expires_at>?",(stamp,)).fetchall()
        if leases:
            prelaunch_time=_cycle_time(prelaunch_cycle) if prelaunch_cycle else None
            require(submitted_recovery and len(leases)==1 and str(leases[0]["cycle_id"])==prelaunch_cycle
                    and prelaunch_time is not None and cycle<prelaunch_time<=_clock_cst()<prelaunch_time+timedelta(minutes=15)
                    and not (root/"logs/stage-status"/("live-"+prelaunch_cycle.replace(":","-")+".json")).exists(),
                    "live profile active outside its own prelaunch recovery")
        if submitted_recovery:
            require(con.execute("SELECT COUNT(*) FROM execution_intents WHERE profile='live' AND (state IS NULL OR state NOT IN ('completed','failed_clean'))").fetchone()[0]==1,
                    "another unresolved execution intent exists")
        require(_clock_cst().timestamp()*1000-float(proof["read_started_at_ms"])<=30000, "proof expired before CAS")
        count=con.execute("UPDATE execution_intents SET state='completed',updated_at=?,completed_at=?,receipt_json=?,error=? "
             "WHERE profile=? AND cycle_id=? AND symbol=? AND action=? AND side=? AND state=? "
             "AND request_fingerprint=? AND ord_id=? AND updated_at=?",
             (stamp,stamp,encoded,"reconciled_submitted_booked_open" if submitted_recovery else "reconciled_protected_open:"+old["error"],*key,old["state"],old["request_fingerprint"],oid,old["updated_at"])).rowcount
        require(count==1, "protected OPEN reconciliation lost CAS")
        con.commit()
    except Exception:
        con.rollback();raise
    finally:con.close()
    return {**answer,"state":"completed"}


def reconcile_protected_open(path: Path, *, expected_row: dict, proof: dict,
                             apply: bool = False, backup_path: Path | None = None) -> dict:
    return _reconcile_booked_protected_open(path, expected_row=expected_row, proof=proof,
        apply=apply, backup_path=backup_path)


def reconcile_submitted_open(path: Path, *, expected_row: dict, proof: dict,
                             apply: bool = False, backup_path: Path | None = None,
                             prelaunch_cycle: str | None = None) -> dict:
    """Finalize one old fully filled, already booked and uniquely protected OPEN.

    No exchange order or trade row is written. Other states, changed epochs,
    partial fills and missing protection remain blocked. An occupied profile is
    accepted only for its own prelaunch, before its Live stage file exists.
    """
    return _reconcile_booked_protected_open(path, expected_row=expected_row, proof=proof,
        apply=apply, backup_path=backup_path, submitted_recovery=True, prelaunch_cycle=prelaunch_cycle)


def reconcile_triggered_protection(
    path: Path, *, expected_row: dict[str, Any], proof: dict[str, Any],
    fills: list[dict[str, Any]], apply: bool = False, backup_path: Path | None = None,
) -> dict[str, Any]:
    """CAS-resolve one independently confirmed protection outcome; no trade writes.

    This operator writer does not alter the ordinary state graph. The exact
    uncertain row, a freshly verified triggered algo/filled child/flat side,
    exact fills, and an already-recorded matching close must all agree.
    """
    from core.protection_terminal import validate_triggered_flat, _same, _number
    from contextlib import closing

    if not Path(path).is_file():
        raise FileNotFoundError(path)
    old = dict(expected_row)
    if not (old.get("profile") == "live"
            and old.get("action") == "adjust_protection"
            and old.get("state") == "uncertain" and old.get("ord_id")):
        raise ValueError("not an uncertain identified protection intent")
    request = json.loads(old["request_json"])
    if any(request.get(k) != old.get(k) for k in ("profile","cycle_id","symbol","action","side")):
        raise ValueError("request key does not match intent key")
    fingerprint, _ = canonical_request(request)
    if fingerprint != old["request_fingerprint"]:
        raise ValueError("request fingerprint mismatch")
    now = _clock_cst()
    now_ms = now.timestamp()*1000
    verified_at = _number(proof.get("verified_at_ms"))
    if not (proof.get("verified") is True and proof.get("positions_confirmed_flat") is True
            and isinstance(proof.get("position_rows"), list)
            and verified_at is not None and 0 <= now_ms-verified_at <= 30000):
        raise ValueError("stale or incomplete terminal proof")
    since_ms = datetime.strptime(old["reserved_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST).timestamp()*1000
    checked = validate_triggered_flat(
        symbol=old["symbol"], side=old["side"], algo_id=str(old["ord_id"]),
        expected_sz=request["target_sz"], expected_sl=request["target_sl"],
        expected_tp=request.get("target_tp"), since_ms=since_ms,
        algo=proof.get("algo"), order=proof.get("order"), positions=proof["position_rows"], now_ms=now_ms)
    if checked.get("verified") is not True:
        raise ValueError("terminal proof mismatch: "+str(checked))
    child_id = checked["child_order_id"]
    if not fills or not all(isinstance(f, dict) for f in fills):
        raise ValueError("exact fills required")
    ids, quantity, notional = set(), 0.0, 0.0
    for fill in fills:
        trade_id = str(fill.get("tradeId") or "")
        size, price, stamp = map(_number, (fill.get("fillSz"), fill.get("fillPx"), fill.get("fillTime")))
        if not (str(fill.get("ordId")) == child_id and fill.get("instId") == old["symbol"]
                and fill.get("posSide") == old["side"]
                and fill.get("side") == ("sell" if old["side"] == "long" else "buy")
                and trade_id and trade_id not in ids and size is not None and size>0
                and price is not None and price>0 and stamp is not None
                and checked["trigger_time_ms"] <= stamp <= float(checked["order"]["uTime"])):
            raise ValueError("fill identity, time or economics mismatch")
        ids.add(trade_id);quantity += size;notional += size*price
    if not _same(quantity, checked["closed_sz"]) or not _same(notional/quantity, checked["fill_px"]):
        raise ValueError("fill aggregate mismatch")
    trade_db = Path(path).parent/"live_trades.db"
    if not trade_db.is_file():
        raise FileNotFoundError(trade_db)
    fill_clock = datetime.fromtimestamp(checked["fill_time_ms"]/1000, _CST)
    lower = (fill_clock-timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")
    upper = (fill_clock+timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(trade_db.resolve().as_uri()+"?mode=ro", uri=True, timeout=3)) as trades:
        trades.row_factory = sqlite3.Row
        recorded = []
        for row in trades.execute(
                "SELECT id,ts,sz,fill_px,raw FROM trades WHERE symbol=? AND side=? "
                "AND action='close' AND ts>=? AND ts<=?",
                (old["symbol"],old["side"],lower,upper)):
            raw = json.loads(row["raw"] or "{}")
            order_ids = set(str(x) for x in (raw.get("ord_ids") or []) if x)
            for key in ("ordId", "ord_id", "exchange_order_id"):
                if raw.get(key):order_ids.add(str(raw[key]))
            if child_id in order_ids:
                if order_ids != {child_id} or not _same(row["sz"], quantity) or not _same(row["fill_px"], checked["fill_px"]):
                    raise ValueError("recorded close conflicts with exchange")
                recorded.append({k:row[k] for k in ("id","ts","sz","fill_px")})
        if len(recorded) != 1:
            raise ValueError("one already-recorded exact close required")
    receipt = {"ok": True, "status": "ok", "profile": "live", "mode": "live",
               "cycle_id": old["cycle_id"], "symbol": old["symbol"],
               "action_taken": "ADJUST_PROTECTION", "decision": "hold",
               "n_orders": 0, "trades": [], "errors": [],
               "protection_state": {"ok": True, "naked": False, "position_flat": True},
               "protection_terminal": checked,
               "reconciliation": {"kind": "triggered_protection_confirmed",
                                  "previous_intent": {k:old.get(k) for k in (
                                      "state","error","updated_at","submitted_at","ord_id","receipt_json")},
                                  "recorded_close": recorded[0], "fill_count": len(fills),
                                  "historical_business_terminal_rewritten": False}}
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, allow_nan=False)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    key = _key(old["profile"], old["cycle_id"], old["symbol"], old["side"], old["action"])
    if not apply:
        return {"ok": True, "dry_run": True, "state": "uncertain", "would_set_state": "completed",
                "algo_id": old["ord_id"], "child_order_id": child_id,
                "recorded_close": recorded[0], "trade_rows_written": 0, "exchange_orders_sent": 0}
    if backup_path is None or not Path(backup_path).is_file() or Path(backup_path).resolve() == Path(path).resolve():
        raise ValueError("verified separate backup is required for reconciliation")
    with closing(sqlite3.connect(Path(backup_path).resolve().as_uri()+"?mode=ro", uri=True, timeout=3)) as backup:
        backup.row_factory = sqlite3.Row
        saved = backup.execute("SELECT * FROM execution_intents WHERE profile=? AND cycle_id=? AND symbol=? AND action=? AND side=?",key).fetchone()
        if saved is None or dict(saved) != old:
            raise ValueError("backup intent does not match expected row")
    con = _connect(Path(path))
    try:
        con.execute("BEGIN IMMEDIATE")
        actual = con.execute("SELECT * FROM execution_intents WHERE profile=? AND cycle_id=? AND symbol=? AND action=? AND side=?",key).fetchone()
        if actual is None or dict(actual) != old:
            raise RuntimeError("intent changed since independent verification")
        if con.execute("SELECT 1 FROM stage_profile_leases WHERE profile=? AND expires_at>? LIMIT 1",("live",stamp)).fetchone():
            raise RuntimeError("live profile is active; retry read-only verification after release")
        if _clock_cst().timestamp()*1000-_number(proof["verified_at_ms"]) > 30000:
            raise RuntimeError("terminal proof expired before CAS")
        changed = con.execute(
            "UPDATE execution_intents SET state='completed',updated_at=?,completed_at=?,receipt_json=?,error=? "
            "WHERE profile=? AND cycle_id=? AND symbol=? AND action=? AND side=? "
            "AND request_fingerprint=? AND state='uncertain' AND ord_id=? AND updated_at=?",
            (stamp,stamp,encoded,"reconciled_triggered_protection:"+str(old.get("error") or ""),
             *key,old["request_fingerprint"],old["ord_id"],old["updated_at"])).rowcount
        if changed != 1:
            raise RuntimeError("protection intent reconciliation lost CAS")
        con.commit()
    except Exception:
        con.rollback();raise
    finally:
        con.close()
    return {"ok": True, "state": "completed", "algo_id": old["ord_id"],
            "child_order_id": child_id, "recorded_close": recorded[0],
            "trade_rows_written": 0, "exchange_orders_sent": 0}
