# -*- coding: utf-8 -*-
"""Read-only ledger invariants plus deduplicated repair-queue synchronization."""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
LEDGERS = {"live": "live_trades.db"}
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
    # 自愈/对账补的行只带 ord_ids 列表；恰好一个元素时就是该行的订单身份，
    # 让同组内「不同订单」的重复意图也能被识别（2026-09-11）。注意：同一订单
    # 被记两行在 duplicate_execution_findings 里仍看不见（它要求 ≥2 个不同
    # ordId），那条防线在 writer 合并闸与 apply_unrecorded 的写前查重。
    ids = obj.get("ord_ids")
    if isinstance(ids, (list, tuple)) and len(ids) == 1 and ids[0] not in (None, "", 0):
        return str(ids[0])
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


def latest_trade_timestamps(
    db_root: Path,
    profile: str,
) -> dict[tuple[str, str], str]:
    """Latest ledger side effect per position key, using authoritative trade ts."""
    con = _open_ro(db_root / LEDGERS[profile])
    try:
        rows = con.execute(
            "SELECT symbol,LOWER(COALESCE(side,'')),MAX(ts) "
            "FROM trades WHERE action IN "
            "('open','add','close','reduce','stop','stop_loss','sl') "
            "GROUP BY symbol,LOWER(COALESCE(side,''))"
        ).fetchall()
    finally:
        con.close()
    return {
        (str(row[0]), str(row[1])): str(row[2])
        for row in rows if row[2]
    }


def _cst_timestamp(raw: object) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=CST)
    return value.astimezone(CST)


def experience_position_findings(
    account: sqlite3.Connection,
    profile: str,
    actual: dict[tuple[str, str], float],
    *,
    snapshot_ts: str | None = None,
    latest_trade_ts: dict[tuple[str, str], str] | None = None,
    deferred: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    expected = experience_remaining(account, profile)
    trade_times = latest_trade_ts or {}
    snapshot_at = _cst_timestamp(snapshot_ts)
    findings = []
    for symbol, side in sorted(set(expected) | set(actual)):
        key = (symbol, side)
        trade_ts = trade_times.get(key)
        trade_at = _cst_timestamp(trade_ts)
        if (
            snapshot_at is not None
            and trade_at is not None
            and trade_at > snapshot_at
        ):
            if deferred is not None:
                deferred.append({
                    "kind": "experience_position_check_deferred",
                    "profile": profile,
                    "symbol": symbol,
                    "side": side,
                    "snapshot_ts": snapshot_ts,
                    "latest_trade_ts": trade_ts,
                    "reason": "position_snapshot_precedes_latest_trade",
                })
            continue
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


# 2026-09-11：experience_position 族里 UNRECORDED 方向（经验剩余 < OKX 实仓）的行，
# 会在交易所仓位后来被平掉时“平凡愈合”：经验与实仓同时归零，账本却从未记录过这笔
# 持仓（2026-09-03 ENS 空 284 张、09-07 SOXL 因此整笔漏记）。这类行只有在经验库出现
# 发现时刻附近的同 symbol/side 开仓记录后才准自动关单；否则保持 pending，并在 issue
# 末尾追加一次说明，让漏记保持可见。GHOST 方向与其它族不受影响。
_EXPERIENCE_ISSUE_RE = re.compile(r"经验剩余=([-+0-9.eE]+)，OKX 实仓=([-+0-9.eE]+)")
UNRECORDED_OPEN_LOOKBACK = timedelta(minutes=60)
UNRECORDED_OPEN_GRACE = timedelta(minutes=5)
UNRECORDED_VANISHED_NOTE = (
    "｜UNRECORDED 未闭环：OKX 仓位已消失，而经验库在发现时刻前后没有这笔开仓，"
    "不是自愈；先按 ordId 查 trades——缺则按 fills 回填整笔"
    "（repair_verified_journal_open 补 open + reconcile_exchange_closes --ordid "
    "补 close），已在则只补经验行；处理完人工关单")


def hold_unrecorded_vanished(
    account: sqlite3.Connection, row: dict[str, Any]
) -> str | None:
    """``sync_repair_queue`` hold for vanished UNRECORDED experience rows.

    Returns the note to append (the row stays pending) or ``None`` (normal
    close).  Unparseable rows and read failures return ``None`` so a caller
    such as the live account snapshot keeps its previous behavior.
    """
    parts = str(row.get("check_name") or "").split(":")
    if len(parts) != 5 or parts[1] != "experience_position":
        return None
    match = _EXPERIENCE_ISSUE_RE.search(str(row.get("issue") or ""))
    since = _cst_timestamp(row.get("ts"))
    if match is None or since is None:
        return None
    try:
        experience_then = float(match.group(1))
        actual_then = float(match.group(2))
    except ValueError:
        return None
    if not experience_then + _EPS < actual_then:
        return None
    profile, symbol, side = parts[2], parts[3], parts[4].lower()
    try:
        recorded = account.execute(
            "SELECT 1 FROM trade_experiences WHERE profile=? AND symbol=? "
            "AND LOWER(side)=? AND action='open' AND ts>=? AND ts<=? LIMIT 1",
            (profile, symbol, side,
             (since - UNRECORDED_OPEN_LOOKBACK).strftime("%Y-%m-%d %H:%M:%S"),
             (since + UNRECORDED_OPEN_GRACE).strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if recorded else UNRECORDED_VANISHED_NOTE


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
    closed_by: str = "ledger_invariants",
    resolution: str = "invariant healed",
    hold=None,
) -> dict[str, int]:
    """Upsert one explicitly owned repair-queue family.

    ``findings`` must belong to ``family_prefix``.  Rejecting a mixed batch is
    deliberate: a narrow caller must never insert another owner's finding or
    close another owner's still-active row.  ``substr`` is used instead of
    ``LIKE`` because invariant names contain ``_`` (a SQL LIKE wildcard).

    ``hold(account, row)`` may return a note for a row that would be
    closed; the row then stays pending with the note appended once (see
    ``hold_unrecorded_vanished``).  Without ``hold`` nothing changes.
    """
    family_prefix = str(family_prefix or "")
    closed_by = str(closed_by or "").strip()
    resolution = str(resolution or "").strip()
    if not family_prefix:
        raise ValueError("family_prefix must be non-empty")
    if not closed_by:
        raise ValueError("closed_by must be non-empty")
    if not resolution:
        raise ValueError("resolution must be non-empty")

    active: dict[str, dict[str, Any]] = {}
    for finding in findings:
        check_name = str(finding.get("check_name") or "")
        if not check_name.startswith(family_prefix):
            raise ValueError(
                "repair_queue family mismatch: "
                f"prefix={family_prefix!r} check_name={check_name!r}"
            )
        active[check_name] = finding
    rows = account.execute(
        "SELECT id,check_name,issue,ts FROM repair_queue "
        "WHERE substr(check_name,1,?)=? "
        "AND status IN ('open','pending')",
        (len(family_prefix), family_prefix),).fetchall()
    existing = {str(r[1]): int(r[0]) for r in rows}
    details = {
        str(r[1]): {"id": int(r[0]), "check_name": str(r[1]),
                    "issue": r[2], "ts": r[3]}
        for r in rows}
    inserted = closed = held = 0
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
        note = hold(account, details[check_name]) if hold is not None else None
        if note:
            issue = str(details[check_name].get("issue") or "")
            if note not in issue:
                account.execute(
                    "UPDATE repair_queue SET issue=? "
                    "WHERE id=? AND status IN ('open','pending')",
                    (issue + note, row_id))
            held += 1
            continue
        account.execute(
            "UPDATE repair_queue SET status='closed',closed_at=?,"
            "closed_by=?,resolution=? "
            "WHERE id=? AND status IN ('open','pending')",
            (ts, closed_by, resolution, row_id))
        if account.execute("SELECT changes()").fetchone()[0]:
            closed += 1
    result = {"inserted": inserted, "closed": closed}
    if hold is not None:
        result["held"] = held
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default=_public_project_path('db'))
    # `both` 是 demo 时代的遗留写法，**刻意继续接受**：daily_maintenance、
    # reviewer 角色文档和历史运维记录里都写着 `--profile both`，改成拒绝会让
    # 每日维护当场 rc≠0。这里把它降级为 live 的别名。
    ap.add_argument("--profile", choices=("live", "both"), default="both")
    ap.add_argument("--window-min", type=int, default=90)
    ap.add_argument("--intent-stale-min", type=int, default=15)
    ap.add_argument("--apply-repair-queue", action="store_true")
    ap.add_argument("--compact", action="store_true")
    args = ap.parse_args()
    db_root = Path(args.db_root)
    # 2026-08-06 demo 全量下线：无论传 live 还是 both，都只扫 live。
    # 这行原本是 `("live","demo") if both`——Phase 2 只收窄了 choices 没动这里，
    # 于是 demo_trades.db 一删，daily_maintenance 每日那条 `--profile both`
    # 就会在 _open_ro 上抛 `unable to open database file`（已实测复现）。
    profiles = ("live",)
    now = datetime.now(CST)
    since = (now - timedelta(minutes=max(1, args.window_min))).strftime(
        "%Y-%m-%d %H:%M:%S")
    findings = []
    deferred_checks: list[dict[str, Any]] = []
    snapshot_ts = {}
    account = sqlite3.connect(str(db_root / "account.db"), timeout=8)
    for profile in profiles:
        findings.extend(trade_net_findings(db_root, profile))
        findings.extend(duplicate_execution_findings(db_root, profile, since))
        findings.extend(execution_intent_findings(
            db_root, profile, now, args.intent_stale_min))
        ts, actual = latest_snapshot_positions(account, profile)
        snapshot_ts[profile] = ts
        if ts is None:
            findings.append({
                "kind": "position_snapshot_missing",
                "profile": profile,
                "check_name": (
                    f"ledger_invariant:position_snapshot:{profile}"),
                "issue": f"[{profile}] 无可用交易所持仓快照，无法核验经验数量",
                "fix_action": "等待下一次自然账户采集；禁止把缺快照解释成零仓",
            })
            continue
        try:
            findings.extend(experience_position_findings(
                account,
                profile,
                actual,
                snapshot_ts=ts,
                latest_trade_ts=latest_trade_timestamps(db_root, profile),
                deferred=deferred_checks,
            ))
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
                findings=findings, ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                hold=hold_unrecorded_vanished)
            account.commit()
        except Exception:
            account.rollback()
            raise
        finally:
            account.close()
    payload = {
        "ok": True, "since": since, "findings": findings,
        "latest_position_snapshot": snapshot_ts,
        "deferred_checks": deferred_checks,
        "repair_queue": queue,
    }
    print(json.dumps(
        payload, ensure_ascii=False,
        separators=(",", ":") if args.compact else None,
        indent=None if args.compact else 2,
    ))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
