# -*- coding: utf-8 -*-
"""Generate a current, read-only ledger audit snapshot.

The snapshot is rebuilt from SQLite ledgers, execution journals, latest
position snapshots, experience quantities, repair queue, and collected OKX
account bills.  Historical duplicate-execution incidents remain visible but do
not by themselves make the current repair state fail.
"""
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
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
LEDGERS = {"live": "live_trades.db", "demo": "demo_trades.db"}
DB_CHECKS = (
    "ledger.db", "account.db", "live_trades.db", "demo_trades.db",
    "analysis.db", "market.db", "regime.db",
)
EPS = 1e-7


def _now_dt() -> datetime:
    return datetime.now(CST)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%d %H:%M:%S")


def _open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    return con


def _dicts(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
        return dict(parsed) if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _trade_ordid(row: dict[str, Any]) -> str | None:
    raw = _json_obj(row.get("raw"))
    for key in ("ordId", "ord_id", "open_id"):
        value = raw.get(key)
        if value not in (None, "", 0):
            return str(value)
    return None


def _ledger_rows(db_root: Path, profile: str) -> list[dict[str, Any]]:
    with _open_ro(db_root / LEDGERS[profile]) as con:
        rows = _dicts(con.execute(
            "SELECT rowid AS ledger_rowid,cycle_id,ts,symbol,action,side,"
            "sz,fill_px,pnl,raw FROM trades ORDER BY rowid"
        ).fetchall())
    for row in rows:
        row["ordId"] = _trade_ordid(row)
    return rows


def _load_journal(db_root: Path, profile: str) -> tuple[list[dict[str, Any]], int]:
    path = db_root / "journal" / f"exec_{profile}.jsonl"
    rows: list[dict[str, Any]] = []
    corrupt = 0
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            corrupt += 1
            continue
        row["_line_no"] = line_no
        rows.append(row)
    return rows, corrupt


def _economic_candidates(
    trades: list[dict[str, Any]], rec: dict[str, Any],
) -> list[dict[str, Any]]:
    trade = rec.get("trade") or {}
    result = []
    for row in trades:
        if (
            row["cycle_id"] == rec.get("cycle_id")
            and row["symbol"] == trade.get("symbol")
            and str(row["action"] or "").lower()
            == str(trade.get("action") or "").lower()
            and str(row["side"] or "").lower()
            == str(trade.get("side") or "").lower()
        ):
            try:
                same_size = abs(float(row["sz"] or 0) - float(trade.get("sz") or 0)) < 1e-8
            except (TypeError, ValueError):
                same_size = False
            if same_size:
                result.append(row)
    return result


def _journal_section(
    db_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    audit: dict[str, Any] = {}
    duplicate_intents: list[dict[str, Any]] = []
    for profile in LEDGERS:
        journal, corrupt = _load_journal(db_root, profile)
        trades = _ledger_rows(db_root, profile)
        by_oid = {
            row["ordId"]: row for row in trades if row.get("ordId")
        }
        exact = 0
        economic_without_id = []
        conflicts = []
        absent = []
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for rec in journal:
            trade = rec.get("trade") or {}
            oid = str(trade.get("ordId") or trade.get("open_id") or "") or None
            key = (
                rec.get("cycle_id"), trade.get("symbol"),
                trade.get("action"), trade.get("side"),
            )
            groups[key].append({
                "line": rec["_line_no"], "ordId": oid,
                "sz": trade.get("sz"), "notional": trade.get("notional"),
                "margin": trade.get("margin"), "ts": rec.get("ts"),
            })
            if oid and oid in by_oid:
                exact += 1
                continue
            candidates = _economic_candidates(trades, rec)
            if not candidates:
                absent.append({
                    "journal_line": rec["_line_no"],
                    "cycle_id": rec.get("cycle_id"),
                    "symbol": trade.get("symbol"),
                    "action": trade.get("action"),
                    "side": trade.get("side"),
                    "sz": trade.get("sz"),
                    "journal_ordId": oid,
                })
                continue
            ledger_ids = [row.get("ordId") for row in candidates]
            item = {
                "journal_line": rec["_line_no"],
                "cycle_id": rec.get("cycle_id"),
                "symbol": trade.get("symbol"),
                "journal_ordId": oid,
                "ledger_ordIds": ledger_ids,
                "ledger_rowids": [row["ledger_rowid"] for row in candidates],
            }
            if oid is None:
                economic_without_id.append(item)
            elif oid in ledger_ids:
                exact += 1
            else:
                conflicts.append(item)
        for key, records in groups.items():
            distinct = {item["ordId"] for item in records if item["ordId"]}
            if len(distinct) > 1:
                duplicate_intents.append({
                    "profile": profile,
                    "cycle_id": key[0],
                    "symbol": key[1],
                    "action": key[2],
                    "side": key[3],
                    "records": records,
                })
        audit[profile] = {
            "journal_rows": len(journal),
            "corrupt_rows": corrupt,
            "exact_ordid_matches": exact,
            "economic_matches_without_journal_ordid": economic_without_id,
            "identity_conflicts": conflicts,
            "unaccounted_without_fingerprint_match": absent,
        }
    return audit, duplicate_intents


def _repeated_cases(
    db_root: Path,
    duplicate_intents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    live_rows = _ledger_rows(db_root, "live")
    with _open_ro(db_root / "account.db") as con:
        experiences = _dicts(con.execute(
            "SELECT id,cycle_id,profile,symbol,side,status,pnl_pct,raw "
            "FROM trade_experiences ORDER BY id"
        ).fetchall())
    cases = []
    for item in duplicate_intents:
        if item["profile"] != "live":
            continue
        ledger_matches = [
            {k: v for k, v in row.items() if k != "raw"}
            for row in live_rows
            if row["cycle_id"] == item["cycle_id"]
            and row["symbol"] == item["symbol"]
            and str(row["action"]).lower() == str(item["action"]).lower()
            and str(row["side"]).lower() == str(item["side"]).lower()
        ]
        experience_matches = []
        for row in experiences:
            if (
                row["profile"] == "live"
                and row["cycle_id"] == item["cycle_id"]
                and row["symbol"] == item["symbol"]
                and str(row["side"]).lower() == str(item["side"]).lower()
            ):
                raw = _json_obj(row["raw"])
                experience_matches.append({
                    "id": row["id"], "status": row["status"],
                    "pnl_pct": row["pnl_pct"],
                    "open_sz": raw.get("sz"),
                    "open_ordId": raw.get("ordId"),
                    "close_ordId": raw.get("closeOrdId"),
                })
        cases.append({
            **item,
            "ledger_rows": ledger_matches,
            "ledger_ordId_count": len({
                row["ordId"] for row in ledger_matches if row.get("ordId")
            }),
            "experience_rows": experience_matches,
        })
    exposure = {
        "incident_count": len(cases),
        "second_execution_notional_sum": round(sum(
            float(case["records"][1].get("notional") or 0)
            for case in cases if len(case["records"]) > 1
        ), 6),
        "second_execution_margin_sum": round(sum(
            float(case["records"][1].get("margin") or 0)
            for case in cases if len(case["records"]) > 1
        ), 6),
        "classification": "historical_incidents_not_current_repair_queue",
    }
    return cases, exposure


def _ledger_net(db_root: Path, profile: str) -> dict[str, float]:
    positions: dict[str, float] = defaultdict(float)
    for row in _ledger_rows(db_root, profile):
        key = f"{row['symbol']}|{str(row['side'] or '').lower()}"
        action = str(row["action"] or "").lower()
        size = abs(float(row["sz"] or 0))
        if action in ("open", "add"):
            positions[key] += size
        elif action in ("close", "reduce", "stop", "stop_loss", "sl"):
            positions[key] -= size
    return {
        key: round(value, 10) for key, value in sorted(positions.items())
        if abs(value) > EPS
    }


def _latest_positions(
    con: sqlite3.Connection, profile: str,
) -> tuple[str | None, dict[str, float]]:
    row = con.execute(
        "SELECT MAX(ts) FROM position_snapshots "
        "WHERE profile=? AND ts LIKE '20%'", (profile,),
    ).fetchone()
    ts = row[0] if row else None
    if not ts:
        return None, {}
    rows = con.execute(
        "SELECT symbol,side,sz FROM position_snapshots "
        "WHERE profile=? AND ts=? AND symbol<>'__FLAT__'",
        (profile, ts),
    ).fetchall()
    positions = {
        f"{row['symbol']}|{str(row['side'] or '').lower()}": abs(float(row["sz"] or 0))
        for row in rows if abs(float(row["sz"] or 0)) > EPS
    }
    return str(ts), positions


def _experience_status(db_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = {}
    with _open_ro(db_root / "account.db") as con:
        cols = {
            str(row["name"]) for row in con.execute(
                "PRAGMA table_info(trade_experiences)"
            ).fetchall()
        }
        if "remaining_sz" not in cols:
            return {
                profile: {"error": "remaining_sz missing"} for profile in LEDGERS
            }, []
        for profile in LEDGERS:
            snapshot_ts, actual = _latest_positions(con, profile)
            expected = {
                f"{row['symbol']}|{str(row['side'] or '').lower()}": float(
                    row["remaining"] or 0
                )
                for row in con.execute(
                    "SELECT symbol,side,"
                    "SUM(COALESCE(remaining_sz,0)) remaining "
                    "FROM trade_experiences WHERE profile=? AND action='open' "
                    "AND status IN ('open','expired') GROUP BY symbol,side",
                    (profile,),
                ).fetchall()
                if abs(float(row["remaining"] or 0)) > EPS
            }
            mismatch = {}
            for key in sorted(set(actual) | set(expected)):
                if abs(actual.get(key, 0.0) - expected.get(key, 0.0)) > max(
                    EPS, actual.get(key, 0.0) * 1e-7,
                ):
                    mismatch[key] = {
                        "position_snapshot_size": actual.get(key, 0.0),
                        "experience_remaining_size": expected.get(key, 0.0),
                    }
            result[profile] = {
                "position_snapshot_ts": snapshot_ts,
                "position_groups": len(actual),
                "experience_open_groups": len(expected),
                "missing_or_size_mismatch_groups": mismatch,
            }
        eth = _dicts(con.execute(
            "SELECT id,cycle_id,ts,status,pnl_pct,hold_hours,hit_1R,"
            "remaining_sz,raw FROM trade_experiences "
            "WHERE profile='live' AND symbol='ETH-USDT-SWAP' "
            "AND cycle_id='2026-07-26T00:00' ORDER BY id"
        ).fetchall())
    for row in eth:
        raw = _json_obj(row.pop("raw", None))
        row["open_ordId"] = raw.get("ordId")
        row["close_ordId"] = raw.get("closeOrdId")
    return result, eth


def _cycle_quality(
    db_root: Path, recent_since: str,
) -> dict[str, Any]:
    result = {}
    for profile, filename in LEDGERS.items():
        with _open_ro(db_root / filename) as con:
            mismatches = _dicts(con.execute(
                "SELECT c.cycle_id,c.ts,c.decision,c.n_orders,"
                "(SELECT COUNT(*) FROM trades t WHERE t.cycle_id=c.cycle_id) "
                "trade_rows FROM trade_cycles c "
                "WHERE CAST(COALESCE(c.n_orders,0) AS INTEGER)<>"
                "(SELECT COUNT(*) FROM trades t WHERE t.cycle_id=c.cycle_id) "
                "ORDER BY c.cycle_id"
            ).fetchall())
            recent = _dicts(con.execute(
                "SELECT rowid AS ledger_rowid,cycle_id,ts,symbol,action,side,"
                "sz,raw FROM trades WHERE ts>=? ORDER BY ts,rowid",
                (recent_since,),
            ).fetchall())
        missing = []
        for row in recent:
            row["ordId"] = _trade_ordid(row)
            if not row["ordId"]:
                row.pop("raw", None)
                missing.append(row)
        result[profile] = {
            "n_orders_mismatches": len(mismatches),
            "n_orders_mismatch_rows": mismatches,
            "recent_since": recent_since,
            "recent_trade_rows": len(recent),
            "recent_missing_ordId": len(missing),
            "recent_missing_ordId_rows": missing,
        }
    return result


def _repair_queue_and_intents(db_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    with _open_ro(db_root / "account.db") as con:
        status_counts = {
            row["status"]: row["n"] for row in con.execute(
                "SELECT status,COUNT(*) n FROM repair_queue GROUP BY status"
            ).fetchall()
        }
        active = _dicts(con.execute(
            "SELECT id,ts,check_name,status,issue FROM repair_queue "
            "WHERE status IN ('open','pending') ORDER BY id"
        ).fetchall())
    with _open_ro(db_root / "ledger.db") as con:
        table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='execution_intents'"
        ).fetchone()
        if table:
            states = {
                row["state"]: row["n"] for row in con.execute(
                    "SELECT state,COUNT(*) n FROM execution_intents GROUP BY state"
                ).fetchall()
            }
            unresolved = _dicts(con.execute(
                "SELECT profile,cycle_id,symbol,action,side,state,updated_at,"
                "ord_id,error FROM execution_intents "
                "WHERE state IN ('reserved','submitting','submitted','uncertain') "
                "ORDER BY updated_at"
            ).fetchall())
        else:
            states, unresolved = {}, []
    return (
        {"status_counts": status_counts, "open_or_pending": active},
        {"state_counts": states, "unresolved": unresolved},
    )


def _pnl_and_bill_windows(db_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pnl_windows = {}
    bill_coverage = {}
    with _open_ro(db_root / "account.db") as account:
        for profile in LEDGERS:
            span = account.execute(
                "SELECT MIN(ts),MAX(ts),COUNT(*),"
                "SUM(COALESCE(pnl,0)),SUM(COALESCE(fee,0)),"
                "SUM(COALESCE(bal_change,0)),"
                "SUM(CASE WHEN type='2' THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN type='8' THEN 1 ELSE 0 END),"
                "COUNT(DISTINCT CASE WHEN type='2' THEN NULLIF(ord_id,'') END) "
                "FROM account_bills WHERE profile=?", (profile,),
            ).fetchone()
            start, end, count, pnl, fee, bal, trade_rows, funding_rows, orders = span
            with _open_ro(db_root / LEDGERS[profile]) as ledger:
                ledger_span = (
                    ledger.execute(
                        "SELECT COUNT(*),SUM(COALESCE(pnl,0)) FROM trades "
                        "WHERE ts BETWEEN ? AND ?", (start, end),
                    ).fetchone()
                    if start and end else (0, 0)
                )
            pnl_windows[profile] = {
                "bill_window_start": start,
                "bill_window_end": end,
                "account_bill_rows": count,
                "bill_realized_pnl": round(float(pnl or 0), 6),
                "bill_fees": round(float(fee or 0), 6),
                "bill_balance_change": round(float(bal or 0), 6),
                "ledger_trade_rows_in_same_window": ledger_span[0],
                "ledger_gross_pnl_in_same_window": round(
                    float(ledger_span[1] or 0), 6,
                ),
                "interpretation": (
                    "OKX bills are authoritative for balance-changing events "
                    "within this collected window; no lifetime extrapolation."
                ),
            }
            bill_coverage[profile] = {
                "rows": count,
                "window_start": start,
                "window_end": end,
                "trade_bill_rows": int(trade_rows or 0),
                "funding_bill_rows": int(funding_rows or 0),
                "distinct_trade_orders": int(orders or 0),
            }
    return pnl_windows, bill_coverage


def _db_integrity(db_root: Path) -> dict[str, str]:
    result = {}
    for name in DB_CHECKS:
        with _open_ro(db_root / name) as con:
            result[name] = str(con.execute("PRAGMA quick_check").fetchone()[0])
    return result


def _blockers(snapshot: dict[str, Any]) -> list[str]:
    blockers = []
    if any(value != "ok" for value in snapshot["db_integrity"].values()):
        blockers.append("db_integrity")
    for profile, item in snapshot["journal_audit"].items():
        if item["corrupt_rows"]:
            blockers.append(f"journal_corrupt:{profile}")
        if item["identity_conflicts"]:
            blockers.append(f"journal_identity_conflict:{profile}")
        if item["unaccounted_without_fingerprint_match"]:
            blockers.append(f"journal_unaccounted:{profile}")
    for profile, rows in snapshot["negative_net_positions"].items():
        if rows:
            blockers.append(f"negative_net:{profile}")
    for profile, item in snapshot["experience_status"].items():
        if item.get("error") or item.get("missing_or_size_mismatch_groups"):
            blockers.append(f"experience:{profile}")
    for profile, item in snapshot["cycle_quality"].items():
        if item["n_orders_mismatches"]:
            blockers.append(f"n_orders:{profile}")
        if item["recent_missing_ordId"]:
            blockers.append(f"recent_ordId:{profile}")
    if snapshot["repair_queue"]["open_or_pending"]:
        blockers.append("repair_queue")
    if snapshot["execution_intents"]["unresolved"]:
        blockers.append("execution_intents")
    return blockers


def build_snapshot(db_root: Path, recent_days: int) -> dict[str, Any]:
    now = _now_dt()
    recent_since = (now - timedelta(days=max(1, recent_days))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    journal_audit, duplicate_intents = _journal_section(db_root)
    repeated, exposure = _repeated_cases(db_root, duplicate_intents)
    net = {profile: _ledger_net(db_root, profile) for profile in LEDGERS}
    negative = {
        profile: {key: value for key, value in values.items() if value < -EPS}
        for profile, values in net.items()
    }
    experience, eth_experience = _experience_status(db_root)
    repair_queue, intents = _repair_queue_and_intents(db_root)
    pnl_windows, bill_coverage = _pnl_and_bill_windows(db_root)
    snapshot = {
        "as_of_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending",
        "db_integrity": _db_integrity(db_root),
        "journal_audit": journal_audit,
        "duplicate_intents": duplicate_intents,
        "repeated_execution_cases": repeated,
        "duplicate_event_exposure": exposure,
        "negative_net_positions": negative,
        "nonzero_ledger_net": net,
        "experience_status": experience,
        "live_eth_experience": eth_experience,
        "cycle_quality": _cycle_quality(db_root, recent_since),
        "repair_queue": repair_queue,
        "execution_intents": intents,
        "account_bill_coverage": bill_coverage,
        "pnl_windows": pnl_windows,
        "sources": {
            "ledgers": [
                str((db_root / name).resolve()) for name in DB_CHECKS
            ],
            "journals": [
                str((db_root / "journal" / f"exec_{profile}.jsonl").resolve())
                for profile in LEDGERS
            ],
            "grain": (
                "trade rows at ledger row grain; journal rows at execution-event "
                "grain; account bills at OKX billId grain; positions at latest "
                "profile snapshot grain"
            ),
        },
        "limitations": [
            "Historical duplicate executions are preserved as incidents, not reclassified as current orders.",
            "Account-bill PnL applies only to the explicit collected windows.",
            "Current position truth uses the latest stored OKX position snapshot and records its timestamp.",
        ],
    }
    blockers = _blockers(snapshot)
    snapshot["current_blockers"] = blockers
    snapshot["status"] = (
        "action_required" if blockers else (
            "ok_with_historical_incidents"
            if repeated else "ok"
        )
    )
    return snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate current ledger audit snapshot")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument(
        "--out",
        default=_project_path('reports', 'quality', 'ledger_audit_snapshot_20260726.json'),
    )
    ap.add_argument("--recent-days", type=int, default=7)
    ap.add_argument("--backup-dir")
    args = ap.parse_args()
    db_root = Path(args.db_root)
    out = Path(args.out)
    snapshot = build_snapshot(db_root, args.recent_days)
    backup = None
    if out.exists():
        if not args.backup_dir:
            raise RuntimeError("existing snapshot requires --backup-dir")
        stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
        backup = Path(args.backup_dir) / (
            f"{out.stem}.pre-regeneration-{stamp}{out.suffix}"
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, backup)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "ok": True,
        "out": str(out),
        "backup": str(backup) if backup else None,
        "status": snapshot["status"],
        "current_blockers": snapshot["current_blockers"],
        "as_of_cst": snapshot["as_of_cst"],
    }, ensure_ascii=False, indent=2))
    return 0 if not snapshot["current_blockers"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
