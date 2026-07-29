# -*- coding: utf-8 -*-
"""Read-only ledger invariants plus deduplicated repair-queue synchronization."""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
LEDGERS = {"live": "live_trades.db", "demo": "demo_trades.db"}
_EPS = 1e-7


def _json(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
        return dict(value) if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _ordid(raw) -> str | None:
    obj = _json(raw)
    for key in ("ordId", "ord_id", "open_id"):
        value = obj.get(key)
        if value not in (None, "", 0):
            return str(value)
    return None


def _open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=8)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def trade_net_findings(db_root: Path, profile: str) -> list[dict[str, Any]]:
    path = db_root / LEDGERS[profile]
    con = _open_ro(path)
    try:
        rows = con.execute(
            "SELECT symbol,side,action,sz FROM trades").fetchall()
    finally:
        con.close()
    nets: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (str(row["symbol"]), str(row["side"] or "").lower())
        action = str(row["action"] or "").lower()
        size = abs(float(row["sz"] or 0))
        sign = 1 if action in ("open", "add") else (
            -1 if action in ("close", "reduce", "stop", "stop_loss", "sl")
            else 0)
        nets[key] = nets.get(key, 0.0) + sign * size
    findings = []
    for (symbol, side), net in sorted(nets.items()):
        if net < -_EPS:
            findings.append({
                "kind": "negative_trade_net", "profile": profile,
                "symbol": symbol, "side": side, "ledger_net": net,
                "check_name": (
                    f"ledger_invariant:negative_net:{profile}:{symbol}:{side}"),
                "issue": (
                    f"[{profile}] {symbol} {side} 主账净张数={net:g}<0，"
                    "存在漏记开仓或多记平仓"),
                "fix_action": "只读核对 OKX orders/fills + exec journal；禁止自动重放订单",
            })
    return findings


def duplicate_execution_findings(
    db_root: Path, profile: str, since_ts: str
) -> list[dict[str, Any]]:
    path = db_root / LEDGERS[profile]
    con = _open_ro(path)
    try:
        rows = con.execute(
            "SELECT rowid AS ledger_rowid,cycle_id,ts,symbol,action,side,sz,fill_px,raw "
            "FROM trades WHERE ts>=? AND action IN ('open','add') "
            "ORDER BY ts,rowid", (since_ts,)).fetchall()
    finally:
        con.close()
    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for row in rows:
        key = (
            str(row["cycle_id"]), str(row["symbol"]),
            str(row["action"]), str(row["side"] or "").lower())
        groups.setdefault(key, []).append({
            "rowid": row["ledger_rowid"], "ordId": _ordid(row["raw"]),
            "sz": row["sz"], "fill_px": row["fill_px"], "ts": row["ts"],
        })
    findings = []
    for (cycle, symbol, action, side), items in sorted(groups.items()):
        ord_ids = sorted({i["ordId"] for i in items if i["ordId"]})
        if len(items) <= 1 or len(ord_ids) <= 1:
            continue
        findings.append({
            "kind": "duplicate_execution_intent", "profile": profile,
            "cycle_id": cycle, "symbol": symbol, "side": side,
            "action": action, "rows": items, "ord_ids": ord_ids,
            "check_name": (
                f"ledger_invariant:duplicate_intent:{profile}:"
                f"{cycle}:{symbol}:{side}"),
            "issue": (
                f"[{profile}] {cycle} {symbol} {side} 同一执行意图出现 "
                f"{len(ord_ids)} 个 ordId"),
            "fix_action": "核对 execution_intents/journal/OKX；禁止自动下单或重放",
        })
    return findings


def execution_intent_findings(
    db_root: Path,
    profile: str,
    now: datetime,
    stale_min: int = 15,
) -> list[dict[str, Any]]:
    """Find ambiguous or stale OPEN intents that must never be auto-retried."""
    path = db_root / "ledger.db"
    if not path.exists():
        return []
    con = _open_ro(path)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='execution_intents'").fetchone()
        if not exists:
            return []
        rows = con.execute(
            "SELECT profile,cycle_id,symbol,action,side,state,updated_at,"
            "ord_id,receipt_json,error FROM execution_intents WHERE profile=? "
            "ORDER BY updated_at",
            (profile,),
        ).fetchall()
    finally:
        con.close()

    stale_after = now - timedelta(minutes=max(1, stale_min))
    valid_states = {
        "reserved", "submitting", "submitted", "completed",
        "uncertain", "failed_clean",
    }
    findings: list[dict[str, Any]] = []
    for row in rows:
        state = str(row["state"] or "")
        updated_at = str(row["updated_at"] or "")
        problem = None
        kind = None
        if state == "uncertain":
            kind = "execution_intent_uncertain"
            problem = "执行结果含糊"
        elif state in {"reserved", "submitting", "submitted"}:
            try:
                updated_dt = datetime.fromisoformat(updated_at)
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=CST)
                else:
                    updated_dt = updated_dt.astimezone(CST)
            except (TypeError, ValueError):
                updated_dt = None
            if updated_dt is None:
                kind = "execution_intent_stale"
                problem = f"执行意图 {state} 的更新时间非法"
            elif updated_dt <= stale_after:
                kind = "execution_intent_stale"
                problem = f"执行意图停留 {state} 超过 {max(1, stale_min)} 分钟"
        elif state == "completed":
            raw_receipt = row["receipt_json"]
            try:
                parsed = json.loads(raw_receipt) if raw_receipt else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if not isinstance(parsed, dict):
                kind = "execution_intent_receipt_invalid"
                problem = "completed 意图缺少有效缓存回执"
        elif state not in valid_states:
            kind = "execution_intent_state_invalid"
            problem = f"未知执行意图状态 {state!r}"
        if not problem:
            continue

        cycle_id = str(row["cycle_id"])
        symbol = str(row["symbol"])
        side = str(row["side"])
        findings.append({
            "kind": kind,
            "profile": profile,
            "cycle_id": cycle_id,
            "symbol": symbol,
            "side": side,
            "action": str(row["action"]),
            "state": state,
            "updated_at": updated_at,
            "ord_id": row["ord_id"],
            "error": row["error"],
            "check_name": (
                f"ledger_invariant:execution_intent:{profile}:"
                f"{cycle_id}:{symbol}:{side}"
            ),
            "issue": (
                f"[{profile}] {cycle_id} {symbol} {side} {problem}；"
                "必须先核对 OKX orders/fills、execution journal 与主账"
            ),
            "fix_action": (
                "禁止自动重下；人工确认交易所事实后再闭环 intent/主账/repair_queue"
            ),
        })
    return findings


def experience_remaining(
    account: sqlite3.Connection, profile: str
) -> dict[tuple[str, str], float]:
    cols = {str(r[1]) for r in account.execute(
        "PRAGMA table_info(trade_experiences)")}
    if "remaining_sz" not in cols:
        raise RuntimeError("trade_experiences.remaining_sz missing")
    rows = account.execute(
        "SELECT symbol,side,COALESCE(SUM(COALESCE(remaining_sz,0)),0) "
        "FROM trade_experiences WHERE profile=? AND action='open' "
        "AND status IN ('open','expired') GROUP BY symbol,side",
        (profile,)).fetchall()
    return {
        (str(r[0]), str(r[1] or "").lower()): float(r[2] or 0)
        for r in rows if abs(float(r[2] or 0)) > _EPS
    }


def experience_position_findings(
    account: sqlite3.Connection,
    profile: str,
    actual: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    expected = experience_remaining(account, profile)
    findings = []
    for symbol, side in sorted(set(expected) | set(actual)):
        exp_sz = float(expected.get((symbol, side), 0.0))
        actual_sz = float(actual.get((symbol, side), 0.0))
        tolerance = max(_EPS, actual_sz * 1e-7)
        if abs(exp_sz - actual_sz) <= tolerance:
            continue
        findings.append({
            "kind": "experience_position_mismatch", "profile": profile,
            "symbol": symbol, "side": side,
            "experience_remaining": exp_sz, "actual_position": actual_sz,
            "check_name": (
                f"ledger_invariant:experience_position:{profile}:{symbol}:{side}"),
            "issue": (
                f"[{profile}] {symbol} {side} 经验剩余={exp_sz:g}，"
                f"OKX 实仓={actual_sz:g}"),
            "fix_action": "以主账流水重建数量生命周期，再与 OKX 实仓复核；禁止改订单",
        })
    return findings


def latest_snapshot_positions(
    account: sqlite3.Connection, profile: str
) -> tuple[str | None, dict[tuple[str, str], float]]:
    row = account.execute(
        "SELECT MAX(ts) FROM position_snapshots WHERE profile=? AND ts LIKE '20%'",
        (profile,)).fetchone()
    ts = row[0] if row else None
    if not ts:
        return None, {}
    rows = account.execute(
        "SELECT symbol,side,sz FROM position_snapshots "
        "WHERE profile=? AND ts=? AND symbol<>'__FLAT__'",
        (profile, ts)).fetchall()
    return str(ts), {
        (str(r[0]), str(r[1] or "").lower()): abs(float(r[2] or 0))
        for r in rows if abs(float(r[2] or 0)) > _EPS
    }


def sync_repair_queue(
    account: sqlite3.Connection,
    *,
    family_prefix: str,
    findings: list[dict[str, Any]],
    ts: str,
) -> dict[str, int]:
    """Upsert active invariant findings and close healed queue entries."""
    active = {str(f["check_name"]): f for f in findings}
    rows = account.execute(
        "SELECT id,check_name FROM repair_queue WHERE check_name LIKE ? "
        "AND status IN ('open','pending')",
        (f"{family_prefix}%",)).fetchall()
    existing = {str(r[1]): int(r[0]) for r in rows}
    inserted = closed = 0
    for check_name, finding in active.items():
        if check_name in existing:
            continue
        account.execute(
            "INSERT INTO repair_queue "
            "(ts,check_name,issue,fix_action,status,created_utc) "
            "VALUES (?,?,?,?,?,?)",
            (ts, check_name, finding["issue"], finding.get("fix_action"),
             "pending", ts))
        inserted += 1
    for check_name, row_id in existing.items():
        if check_name in active:
            continue
        account.execute(
            "UPDATE repair_queue SET status='closed',closed_at=?,"
            "closed_by='ledger_invariants',resolution='invariant healed' "
            "WHERE id=?", (ts, row_id))
        closed += 1
    return {"inserted": inserted, "closed": closed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--profile", choices=("live", "demo", "both"), default="both")
    ap.add_argument("--window-min", type=int, default=90)
    ap.add_argument("--intent-stale-min", type=int, default=15)
    ap.add_argument("--apply-repair-queue", action="store_true")
    ap.add_argument("--compact", action="store_true")
    args = ap.parse_args()
    db_root = Path(args.db_root)
    profiles = ("live", "demo") if args.profile == "both" else (args.profile,)
    now = datetime.now(CST)
    since = (now - timedelta(minutes=max(1, args.window_min))).strftime(
        "%Y-%m-%d %H:%M:%S")
    findings = []
    snapshot_ts = {}
    account = sqlite3.connect(str(db_root / "account.db"), timeout=8)
    for profile in profiles:
        findings.extend(trade_net_findings(db_root, profile))
        findings.extend(duplicate_execution_findings(db_root, profile, since))
        findings.extend(execution_intent_findings(
            db_root, profile, now, args.intent_stale_min))
        ts, actual = latest_snapshot_positions(account, profile)
        snapshot_ts[profile] = ts
        try:
            findings.extend(experience_position_findings(
                account, profile, actual))
        except RuntimeError as exc:
            findings.append({
                "kind": "experience_schema_missing", "profile": profile,
                "check_name": (
                    f"ledger_invariant:experience_schema:{profile}"),
                "issue": f"[{profile}] 经验数量字段不可用: {exc}",
                "fix_action": "先执行 migrate_ledger_integrity.py dry-run/apply",
            })
    account.close()
    queue = None
    if args.apply_repair_queue:
        account = sqlite3.connect(str(db_root / "account.db"), timeout=8)
        try:
            account.execute("BEGIN IMMEDIATE")
            queue = sync_repair_queue(
                account, family_prefix="ledger_invariant:",
                findings=findings, ts=now.strftime("%Y-%m-%d %H:%M:%S"))
            account.commit()
        except Exception:
            account.rollback()
            raise
        finally:
            account.close()
    payload = {
        "ok": True, "since": since, "findings": findings,
        "latest_position_snapshot": snapshot_ts, "repair_queue": queue,
    }
    print(json.dumps(
        payload, ensure_ascii=False,
        separators=(",", ":") if args.compact else None,
        indent=None if args.compact else 2,
    ))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
