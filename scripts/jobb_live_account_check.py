# -*- coding: utf-8 -*-
"""双盘账户/持仓快照采集器（live/demo account sanity check）。

V2.0 起由 collectors/fast_collect.py 每轮调用（live 一次 + `--profile demo` 一次，
2026-07-03 C4b）：查 OKX 账户余额 + SWAP 持仓，写成本轮新鲜快照，防止 account.db
旧行被当真相。

写库：account_snapshots / position_snapshots（含 B13 消失仓对账补 trade_events 行；
F7 2026-07-06：空仓批次写 symbol='__FLAT__' 哨兵行标记"确认空仓"，见 FLAT_SENTINEL）；
system_state 键按 profile 派生——`{profile}_totalEq` / `{profile}_availBal` /
`{profile}_position_count` / `last_{profile}_account_check`
（即 live_totalEq / demo_totalEq / … camelCase 键）。

Read-only against OKX; writes the fresh snapshot to account.db for audit.
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
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from _okxcli import okx_json
import ledger_invariants as li

CST = timezone(timedelta(hours=8))

# 空仓 flat 哨兵：空仓批次写一行 symbol=FLAT_SENTINEL、sz=0、side/价格列 NULL，
# 表示“该 ts 确认空仓”。消费方据此区分“0 仓”与“没数据”，展示/求和必须过滤哨兵。
FLAT_SENTINEL = "__FLAT__"


def cst_now_str() -> str:
    """C3（2026-07-03）UTC-Z 写方统一：account_snapshots/position_snapshots/trade_events 的 ts
    一律 CST 'YYYY-MM-DD HH:MM:SS'（消 ts 混 Z/CST 测量地雷；历史 Z 行由
    apply_ts_cst_migration.py 幂等迁移）。"""
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return [payload]
    return []


def normalize_profile_label(profile: str | None) -> str:
    if not profile:
        return "live"
    return "demo" if "demo" in str(profile).lower() else "live"


def signed_size(item: dict[str, Any]) -> float:
    pos = to_float(item.get("pos") or item.get("sz")) or 0.0
    pos_side = str(item.get("posSide") or "").lower()
    if pos_side == "short" and pos > 0:
        return -pos
    return pos


def position_side(item: dict[str, Any]) -> str:
    pos_side = str(item.get("posSide") or "").lower()
    if pos_side in {"long", "short"}:
        return pos_side
    return "short" if signed_size(item) < 0 else "long"


def _trade_db_path(db_root: Path, profile_label: str) -> Path:
    return db_root / ("demo_trades.db" if profile_label == "demo" else "live_trades.db")


def _main_ledger_close(db_root: Path, profile_label: str, symbol: str,
                       prev_ts: str, until_ts: str | None = None,
                       expected_sz: float | None = None,
                       expected_side: str | None = None) -> dict[str, Any] | None:
    """Return a matching authoritative close already present in the V2 trade ledger.

    `trade_events` is only a compatibility/event table.  Normal V2 closes are written
    to live/demo_trades.db, so using trade_events alone creates duplicate
    ``auto-vanished`` rows.  Read the authoritative ledger read-only and keep the
    match deliberately narrow by symbol/time/size.
    """
    trade_db = _trade_db_path(db_root, profile_label)
    if not trade_db.exists():
        return None
    try:
        uri = trade_db.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        try:
            sql = (
                "SELECT id,cycle_id,ts,symbol,action,side,sz FROM trades "
                "WHERE symbol=? AND lower(action) IN ('close','stop_loss') AND ts>=?"
            )
            params: list[Any] = [symbol, prev_ts]
            if until_ts:
                sql += " AND ts<=?"
                params.append(until_ts)
            sql += " ORDER BY id DESC"
            for row in con.execute(sql, params):
                if (expected_side is not None
                        and str(row["side"] or "").lower()
                        != str(expected_side).lower()):
                    continue
                if expected_sz is not None:
                    got = to_float(row["sz"])
                    if got is None or abs(abs(got) - abs(expected_sz)) > 1e-8:
                        continue
                return dict(row)
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        # Fail-open for the compatibility reconciliation only: inability to read
        # the secondary ledger must not block account snapshots.
        return None
    return None


def _journal_close(db_root: Path, profile_label: str, symbol: str,
                   prev_ts: str, until_ts: str,
                   expected_sz: float | None = None,
                   expected_side: str | None = None) -> dict[str, Any] | None:
    """Return a matching confirmed close from the execution journal.

    The journal is written immediately after fill confirmation and normally lands
    before trades_writer.  Checking it closes the few-minute race where the OKX
    position has disappeared but the authoritative SQLite trade row is not written
    yet.  Malformed/torn lines are ignored; the account snapshot remains available.
    """
    path = db_root / "journal" / f"exec_{profile_label}.jsonl"
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        item_ts = str(item.get("ts") or "")
        if not item_ts or item_ts < prev_ts:
            # Journal is append ordered; once older than the previous snapshot no
            # still-earlier row can be relevant.
            break
        if item_ts > until_ts:
            continue
        trade = item.get("trade") if isinstance(item.get("trade"), dict) else {}
        if str(trade.get("symbol") or "") != symbol:
            continue
        if str(trade.get("action") or "").lower() not in {"close", "stop_loss"}:
            continue
        if (expected_side is not None
                and str(trade.get("side") or "").lower()
                != str(expected_side).lower()):
            continue
        if expected_sz is not None:
            got = to_float(trade.get("sz"))
            if got is None or abs(abs(got) - abs(expected_sz)) > 1e-8:
                continue
        return {
            "cycle_id": item.get("cycle_id"),
            "ts": item_ts,
            "action": trade.get("action"),
            "symbol": symbol,
            "sz": trade.get("sz"),
            "ordId": trade.get("ordId"),
        }
    return None


def reconcile_vanished_positions(con: sqlite3.Connection, ts: str, profile_label: str,
                                 open_positions: list[dict[str, Any]],
                                 db_root: Path | None = None) -> list[dict[str, Any]]:
    """B13 治本（2026-06-11 全流程验证）：仓位消失但无记账 → 自动补 reconcile 行。

    06-10 现场：2 张 ETH short 在断链窗口（16:30）被平，live 实际空仓数小时，
    无任何 trade_events 记录，账实静默漂移直到全流程验证才发现。
    本函数每轮对比上一批 position_snapshots 与本次 OKX 实仓：
    上批有、本次没有、且其间 trade_events / V2 主账本 / execution journal 均无
    该 symbol 的 CLOSE/STOP_LOSS
    → INSERT 一条 reconcile CLOSE 行（pnl=NULL 待 fills 对账补，不污染 SUM）+ 返回告警。
    """
    prev_ts_row = con.execute(
        "SELECT MAX(ts) FROM position_snapshots WHERE profile=? AND ts LIKE '20%' AND ts < ?",
        (profile_label, ts),
    ).fetchone()
    prev_ts = prev_ts_row[0] if prev_ts_row else None
    if not prev_ts:
        return []
    # F7：过滤空仓哨兵行——上批为 FLAT_SENTINEL 批次时 prev_rows 为空 → 无仓可对账（正确语义），
    # 否则哨兵会被当"消失仓位"误补一条 symbol='__FLAT__' 的 reconcile CLOSE 行。
    prev_rows = con.execute(
        "SELECT symbol, side, sz, avgPx FROM position_snapshots "
        "WHERE ts=? AND profile=? AND symbol != ?",
        (prev_ts, profile_label, FLAT_SENTINEL),
    ).fetchall()
    if not prev_rows:
        return []
    cur_keys = {(item.get("instId"), position_side(item)) for item in open_positions}
    vanished: list[dict[str, Any]] = []
    for sym, side, sz, avg_px in prev_rows:
        if not sym or (sym, side) in cur_keys:
            continue
        ev = con.execute(
            "SELECT 1 FROM trade_events WHERE profile=? AND symbol=? "
            "AND action IN ('CLOSE','STOP_LOSS') AND ts >= ? LIMIT 1",
            (profile_label, sym, prev_ts),
        ).fetchone()
        if ev:
            continue
        # V2 normal closes do not write trade_events.  First consult the
        # authoritative trade ledger; then the execution journal to cover the race
        # between exchange fill and trades_writer commit.
        if db_root is not None:
            main_close = _main_ledger_close(
                db_root, profile_label, sym, prev_ts, ts, to_float(sz), str(side)
            )
            journal_close = _journal_close(
                db_root, profile_label, sym, prev_ts, ts, to_float(sz), str(side)
            )
            if main_close or journal_close:
                continue
        raw = {
            "reconcile": "auto-vanished",
            "prev_snapshot_ts": prev_ts,
            "prev_side": side,
            "prev_sz": sz,
            "prev_avg_px": avg_px,
            "note": "P1.1 账实对账: 仓位自上批快照后消失且无平仓事件, 自动补记; "
                    "pnl=NULL 待 fills 对账补 (okx swap fills --instId ...)",
        }
        close_side = "buy" if side == "short" else "sell"
        con.execute(
            "INSERT INTO trade_events (ts, profile, symbol, action, side, sz, fill_px, pnl, channel, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, profile_label, sym, "CLOSE", close_side, sz, None, None, profile_label,
             json.dumps(raw, ensure_ascii=False)),
        )
        vanished.append({"symbol": sym, "side": side, "sz": sz, "prev_ts": prev_ts})
    return vanished


def find_superseded_auto_vanished(db_root: Path, since: str | None = None,
                                  writer_lag_minutes: int = 30) -> list[dict[str, Any]]:
    """Find compatibility CLOSE rows superseded by an authoritative V2 close.

    A normal executor close can disappear from the exchange position endpoint a few
    minutes before trades_writer commits.  Historical ``auto-vanished`` rows from
    that race are false accounting events.  Match only exact symbol/size closes in
    the narrow previous-snapshot -> event+writer-lag window.
    """
    account_db = db_root / "account.db"
    if not account_db.exists():
        return []
    uri = account_db.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT id,ts,profile,symbol,action,side,sz,raw FROM trade_events "
            "WHERE action='CLOSE' AND raw LIKE '%\"auto-vanished\"%'"
        )
        params: list[Any] = []
        if since:
            sql += " AND ts>=?"
            params.append(since)
        sql += " ORDER BY id"
        event_rows = list(con.execute(sql, params))
    finally:
        con.close()

    matches: list[dict[str, Any]] = []
    for event in event_rows:
        try:
            raw = json.loads(event["raw"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if raw.get("reconcile") != "auto-vanished":
            continue
        prev_ts = str(raw.get("prev_snapshot_ts") or "")
        if not prev_ts:
            continue
        try:
            until_ts = (
                datetime.strptime(str(event["ts"]), "%Y-%m-%d %H:%M:%S")
                + timedelta(minutes=writer_lag_minutes)
            ).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        main_close = _main_ledger_close(
            db_root,
            str(event["profile"]),
            str(event["symbol"]),
            prev_ts,
            until_ts,
            to_float(event["sz"]),
            str(raw.get("prev_side") or "") or None,
        )
        if not main_close:
            continue
        matches.append({
            "event_id": event["id"],
            "event_ts": event["ts"],
            "profile": event["profile"],
            "symbol": event["symbol"],
            "sz": event["sz"],
            "prev_snapshot_ts": prev_ts,
            "trade_id": main_close.get("id"),
            "trade_cycle_id": main_close.get("cycle_id"),
            "trade_ts": main_close.get("ts"),
        })
    return matches


def repair_superseded_auto_vanished(db_root: Path, since: str | None,
                                    apply: bool, backup_dir: Path | None = None) -> dict[str, Any]:
    """Dry-run/apply repair while preserving an auditable superseded row."""
    candidates = find_superseded_auto_vanished(db_root, since=since)
    result: dict[str, Any] = {
        "ok": True, "apply": apply, "candidates": candidates,
        "candidate_count": len(candidates), "updated": 0, "backup": None,
    }
    if not apply or not candidates:
        return result

    account_db = db_root / "account.db"
    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    archive = backup_dir or (db_root.parent / "tmp" / "archive" /
                             f"{stamp}-auto-vanished-repair")
    archive.mkdir(parents=True, exist_ok=True)
    backup_path = archive / "account.db.before"

    src = sqlite3.connect(account_db.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"backup integrity_check={integrity}")
    finally:
        dst.close()
        src.close()
    result["backup"] = str(backup_path)

    by_id = {int(item["event_id"]): item for item in candidates}
    con = sqlite3.connect(account_db)
    try:
        con.execute("BEGIN IMMEDIATE")
        updated = 0
        for event_id, evidence in by_id.items():
            row = con.execute(
                "SELECT action,raw FROM trade_events WHERE id=?", (event_id,)
            ).fetchone()
            if not row or row[0] != "CLOSE":
                raise RuntimeError(f"event {event_id} changed during repair")
            raw = json.loads(row[1] or "{}")
            if raw.get("reconcile") != "auto-vanished":
                raise RuntimeError(f"event {event_id} is no longer auto-vanished")
            raw.update({
                "reconcile": "auto-vanished-superseded",
                "original_action": "CLOSE",
                "superseded_at": cst_now_str(),
                "superseded_by": {
                    "db": f"{evidence['profile']}_trades.db",
                    "trade_id": evidence["trade_id"],
                    "cycle_id": evidence["trade_cycle_id"],
                    "trade_ts": evidence["trade_ts"],
                },
            })
            cur = con.execute(
                "UPDATE trade_events SET action='RECONCILE_SUPERSEDED',raw=? "
                "WHERE id=? AND action='CLOSE'",
                (json.dumps(raw, ensure_ascii=False), event_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"event {event_id} update lost race")
            updated += 1
        con.commit()
        result["updated"] = updated
        result["integrity_check"] = con.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return result


def collect_live_account(profile: str, db_root: Path) -> dict[str, Any]:
    profile_label = normalize_profile_label(profile)
    cli_global_args = ["--profile", profile]
    # C3（2026-07-03）：本轮快照/对账事件全部用同一 CST ts（含 reconcile trade_events 行、
    # position_snapshots DELETE+INSERT、system_state.updated_utc——单函数单 ts，整体自洽）。
    ts = cst_now_str()

    balance_rows = normalize_rows(okx_json("account", "balance", global_args=cli_global_args))
    positions_raw = normalize_rows(okx_json("account", "positions", "--instType", "SWAP", global_args=cli_global_args))

    balance = balance_rows[0] if balance_rows else {}
    details = balance.get("details") or []
    usdt_row = next((row for row in details if isinstance(row, dict) and row.get("ccy") == "USDT"), {})
    upl = to_float(balance.get("upl"))
    if upl is None:
        upl = sum(to_float(row.get("upl")) or 0.0 for row in positions_raw)

    open_positions: list[dict[str, Any]] = []
    for item in positions_raw:
        size_abs = abs(signed_size(item))
        if size_abs <= 0:
            continue
        open_positions.append(item)

    db_root.mkdir(parents=True, exist_ok=True)
    account_db = db_root / "account.db"
    con = sqlite3.connect(str(account_db))
    try:
        con.execute(
            "INSERT OR REPLACE INTO account_snapshots "
            "(ts, profile, totalEq, availBal, upl, daily_pnl, week_pnl, month_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                profile_label,
                to_float(balance.get("totalEq")),
                to_float(usdt_row.get("availBal") or usdt_row.get("availEq")),
                upl,
                None,
                None,
                None,
            ),
        )
        # B13（2026-06-11）：写新快照前先做账实对账——消失仓位无记账则补 reconcile 行
        vanished = reconcile_vanished_positions(
            con, ts, profile_label, open_positions, db_root=db_root
        )

        # The fresh ts/profile row is authoritative for this cycle; delete first so
        # a flat account is represented by zero rows at this timestamp.
        con.execute("DELETE FROM position_snapshots WHERE ts = ? AND profile = ?", (ts, profile_label))
        rows = []
        for item in open_positions:
            rows.append(
                (
                    ts,
                    profile_label,
                    item.get("instId"),
                    position_side(item),
                    abs(signed_size(item)),
                    to_float(item.get("avgPx") or item.get("markPx")),
                    to_float(item.get("lever")),
                    to_float(item.get("liqPx")),
                    to_float(item.get("upl")),
                    to_float(item.get("mgnRatio")),
                )
            )
        if not rows:
            # F7：空仓批次写 FLAT_SENTINEL 哨兵行（sz=0；side=NULL 过表 CHECK(side IN
            # ('long','short'))；其余数值列 NULL），复用同一 INSERT——保证每轮必有本 ts 批次，
            # 消费方按"最新批次"读时能区分"确认空仓"与"没数据"。
            rows.append((ts, profile_label, FLAT_SENTINEL, None, 0.0,
                         None, None, None, None, None))
        con.executemany(
            "INSERT OR REPLACE INTO position_snapshots "
            "(ts, profile, symbol, side, sz, avgPx, lev, liqPx, upl, marginRatio) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        system_rows = {
            f"last_{profile_label}_account_check": ts,
            # F7：持仓数以真实仓位为准（哨兵行不计数；非空路径 len(open_positions)==len(rows) 不变）
            f"{profile_label}_position_count": str(len(open_positions)),
            f"{profile_label}_totalEq": str(to_float(balance.get("totalEq"))),
            f"{profile_label}_availBal": str(to_float(usdt_row.get("availBal") or usdt_row.get("availEq"))),
        }
        for key, value in system_rows.items():
            con.execute(
                "INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
                (key, value, ts),
            )
        # 数量化经验账必须与本轮 OKX 实仓一致。命中只开/续
        # repair_queue 元数据，不改经验或订单；恢复一致后自动闭单。
        actual_positions = {
            (str(item.get("instId")), position_side(item)):
                abs(signed_size(item))
            for item in open_positions
        }
        try:
            experience_findings = li.experience_position_findings(
                con, profile_label, actual_positions)
            experience_queue = li.sync_repair_queue(
                con,
                family_prefix=(
                    f"ledger_invariant:experience_position:{profile_label}:"),
                findings=experience_findings,
                ts=ts)
            experience_audit = {
                "findings": experience_findings,
                "repair_queue": experience_queue,
            }
        except RuntimeError as exc:
            # 迁移部署窗口内保持快采可用；迁移完成后下一轮自然启用。
            experience_audit = {"skipped": str(exc)}
        con.commit()
    finally:
        con.close()

    return {
        "ok": True,
        "ts": ts,
        "profile": profile_label,
        "totalEq": to_float(balance.get("totalEq")),
        "availBal": to_float(usdt_row.get("availBal") or usdt_row.get("availEq")),
        "upl": upl,
        "positionCount": len(open_positions),
        # B13：非空即 WARN——agent 必须在本轮异常段上报并跑 fills 对账补 pnl
        "reconcile_vanished": vanished,
        "experience_reconcile": experience_audit,
        "positions": [
            {
                "instId": item.get("instId"),
                "side": position_side(item),
                "sz": abs(signed_size(item)),
                "avgPx": to_float(item.get("avgPx") or item.get("markPx")),
                "lever": to_float(item.get("lever")),
                "liqPx": to_float(item.get("liqPx")),
                "upl": to_float(item.get("upl")),
                "mgnRatio": to_float(item.get("mgnRatio")),
            }
            for item in open_positions
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="JobB mandatory live account/position check")
    parser.add_argument("--profile", default="live")
    parser.add_argument("--db-root", default=_project_path('db'))
    parser.add_argument("--repair-auto-vanished", action="store_true",
                        help="dry-run 查找已被 V2 主账本平仓取代的伪 auto-vanished 事件")
    parser.add_argument("--since", default=None,
                        help="--repair-auto-vanished 的最早 CST ts")
    parser.add_argument("--apply", action="store_true",
                        help="实际标记伪事件为 RECONCILE_SUPERSEDED；默认只 dry-run")
    parser.add_argument("--backup-dir", default=None,
                        help="修复 apply 前 SQLite 一致性备份目录")
    args = parser.parse_args()

    if args.repair_auto_vanished:
        try:
            result = repair_superseded_auto_vanished(
                Path(args.db_root), args.since, args.apply,
                Path(args.backup_dir) if args.backup_dir else None,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    try:
        result = collect_live_account(args.profile, Path(args.db_root))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
