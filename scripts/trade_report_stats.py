# -*- coding: utf-8 -*-
"""Deterministic trade statistics for daily/weekly reviewer reports.

Facts are split deliberately:

* filled opens/closes come from ``live_trades.db``;
* risk-rejected open attempts come from ``ledger.db.execution_intents``;
* rejected or incomplete rows in ``trades`` never count as fills.

This module is read-only.  It can also be called as a CLI so the reviewer uses
the same facts before rendering QQ text and before invoking the report writer.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
# 日报事实窗锚点（CST）。cron 08:05 触发，窗口闭合在 08:00，留 5min 让数据落定。
# 固定锚点而非跟随报告 ts：报告 ts 由 agent 写入且历史上会漂，跟随会造成
# 相邻日报缺口/重叠。改锚点需同步 validate_daily_report._expected_daily_window
# （该处刻意保持独立实现，勿改为共享此常量）。
DAILY_ANCHOR_HOUR = 8
DAILY_ANCHOR_MINUTE = 0
PROFILE_DB = {
    "live": Path(r"./db/live_trades.db"),
}
LEDGER_DB = Path(r"./db/ledger.db")
FILL_ACTIONS = {"open", "close"}
POSITION_INCREASE_ACTIONS = {"open", "add"}
POSITION_DECREASE_ACTIONS = {"close", "reduce"}


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def now_cst() -> str:
    return datetime.now(CST).strftime(TS_FMT)


def parse_cst(value: str | datetime) -> datetime:
    """Parse supported project timestamps and return a CST-aware datetime."""
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
        text = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def fmt_ts(value: str | datetime) -> str:
    return parse_cst(value).strftime(TS_FMT)


def daily_window(as_of_ts: str | datetime) -> tuple[str, str]:
    """Return the fixed 24h reviewer window ``[前一日 08:00, 当日 08:00)``.

    The reviewer runs at 08:05 CST.  Anchoring the start at same-day midnight
    left every day's 08:05-24:00 trades outside all daily reports.  Anchoring
    on ``as_of_ts`` itself fixed the coverage hole but re-introduced drift:
    the report ts is agent-written and historically wandered (08:00 / 08:06 /
    08:12 / 08:36 ...), so one minute of jitter shifted the window and made
    consecutive reports gap or overlap.  Pinning both edges to 08:00 keeps the
    window deterministic and exactly tiling regardless of trigger jitter, and
    closes it 5 minutes before the run so the data has settled.
    """
    ref = parse_cst(as_of_ts)
    end = ref.replace(
        hour=DAILY_ANCHOR_HOUR, minute=DAILY_ANCHOR_MINUTE,
        second=0, microsecond=0)
    if ref < end:
        # Triggered before the anchor (manual re-run / early fire): report the
        # last complete window rather than a future-ending one.
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    return start.strftime(TS_FMT), end.strftime(TS_FMT)


def weekly_window(week_start_ts: str | datetime) -> tuple[str, str]:
    """Return ``[上周一 08:00, 本周一 08:00)`` for the given 本周一 report key.

    Anchored on the same 08:00 boundary as :func:`daily_window` so the seven
    daily windows of a week tile this interval exactly — a calendar-midnight
    weekly window would sit 8h out of phase and make the dailies unable to
    reconcile against the weekly.  ``week_start_ts`` stays the 本周一 00:00:00
    report key; only the fact window is anchored.
    """
    anchor = parse_cst(week_start_ts).replace(
        hour=DAILY_ANCHOR_HOUR, minute=DAILY_ANCHOR_MINUTE,
        second=0, microsecond=0)
    start = anchor - timedelta(days=7)
    return start.strftime(TS_FMT), anchor.strftime(TS_FMT)


def monthly_window(month_start_ts: str | datetime) -> tuple[str, str]:
    """Return the previous complete calendar month on the 08:00 fact anchor.

    ``month_start_ts`` is the current month's report key (day 1 at 00:00).
    Facts cover ``[previous month day 1 08:00, current month day 1 08:00)``
    so the interval is exactly tiled by the existing daily report windows.
    """
    report_month = parse_cst(month_start_ts)
    end = report_month.replace(
        day=1,
        hour=DAILY_ANCHOR_HOUR,
        minute=DAILY_ANCHOR_MINUTE,
        second=0,
        microsecond=0,
    )
    previous_month_last_day = end - timedelta(days=1)
    start = previous_month_last_day.replace(
        day=1,
        hour=DAILY_ANCHOR_HOUR,
        minute=DAILY_ANCHOR_MINUTE,
        second=0,
        microsecond=0,
    )
    return start.strftime(TS_FMT), end.strftime(TS_FMT)


def rolling_window(
    as_of_ts: str | datetime, days: int
) -> tuple[str, str]:
    if days <= 0:
        raise ValueError("days must be positive")
    end = parse_cst(as_of_ts)
    start = end - timedelta(days=days)
    return start.strftime(TS_FMT), end.strftime(TS_FMT)


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not Path(path).exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(
        f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10
    )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _json_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _explicitly_rejected(row: dict | sqlite3.Row) -> bool:
    """Return True only for explicit non-fill/reject evidence."""
    top = dict(row)
    raw = _json_dict(top.get("raw"))
    for obj in (top, raw):
        status = str(obj.get("status") or "").strip().lower()
        if status in {"rejected", "reject", "failed", "error"}:
            return True
        if obj.get("ok") is False:
            return True
        if obj.get("success") is False:
            return True
        if str(obj.get("action_taken") or "").strip().upper() == "REJECT":
            return True
        if str(obj.get("reject_reason") or "").strip():
            return True
    return False


def _trade_label(item: dict) -> str:
    symbol = str(item["symbol"]).replace("-USDT-SWAP", "")
    side = str(item.get("side") or "-")
    pnl = float(item["pnl"])
    clock = str(item["ts"])[11:16]
    return f"{symbol} {side} {pnl:+.4f} ({clock} close)"


def filled_trade_stats(
    db_path: Path,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = False,
) -> dict:
    """Count confirmed fill rows within a CST interval.

    A row is a reportable fill only when:
    ``action`` is exactly ``open``/``close``, ``sz`` and ``fill_px`` are
    positive, and neither the row nor its raw receipt explicitly says reject.
    """
    start = fmt_ts(start_ts)
    end = fmt_ts(end_ts)
    op = "<" if end_exclusive else "<="
    con = _connect_ro(Path(db_path))
    try:
        rows = con.execute(
            "SELECT id,cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw "
            "FROM trades WHERE datetime(ts)>=datetime(?) "
            f"AND datetime(ts){op}datetime(?) "
            "ORDER BY datetime(ts),id",
            (start, end),
        ).fetchall()
    finally:
        con.close()

    fills: list[dict] = []
    rejected_rows: list[int] = []
    incomplete_rows: list[int] = []
    non_fill_rows: list[int] = []
    for raw_row in rows:
        row = dict(raw_row)
        action = str(row.get("action") or "").strip().lower()
        if action not in FILL_ACTIONS:
            non_fill_rows.append(int(row["id"]))
            continue
        if _explicitly_rejected(row):
            rejected_rows.append(int(row["id"]))
            continue
        if not _positive(row.get("sz")) or not _positive(row.get("fill_px")):
            incomplete_rows.append(int(row["id"]))
            continue
        row["action"] = action
        fills.append(row)

    opens = [row for row in fills if row["action"] == "open"]
    closes = [row for row in fills if row["action"] == "close"]
    closed_with_pnl = [
        row for row in closes if row.get("pnl") is not None
    ]
    total_pnl = sum(float(row["pnl"]) for row in closed_with_pnl)
    best = max(closed_with_pnl, key=lambda row: float(row["pnl"]), default=None)
    worst = min(closed_with_pnl, key=lambda row: float(row["pnl"]), default=None)

    # 2026-08-10 Wave0-2：平仓方向分解进入权威事实——周报文字段曾把 10空/3多
    # 手写成 11空/2多、把 USDT 均值标成百分比；方向计数与均值此后只认这里。
    close_side_breakdown = {}
    for side_key in ("long", "short"):
        side_rows = [
            row for row in closed_with_pnl
            if str(row.get("side") or "").strip().lower() == side_key
        ]
        side_pnl = sum(float(row["pnl"]) for row in side_rows)
        side_wins = sum(float(row["pnl"]) > 0 for row in side_rows)
        close_side_breakdown[side_key] = {
            "close_count": len(side_rows),
            "win_count": side_wins,
            "win_rate_pct": (
                side_wins / len(side_rows) * 100 if side_rows else None
            ),
            "pnl_sum_usdt": side_pnl,
            "pnl_avg_usdt": side_pnl / len(side_rows) if side_rows else None,
            "pnl_unit": "USDT",
        }

    return {
        "source": str(Path(db_path)),
        "period_start_ts": start,
        "period_end_ts": end,
        "period_end_exclusive": bool(end_exclusive),
        "open_count": len(opens),
        "close_count": len(closes),
        "realized_pnl": total_pnl,
        "closed_with_pnl_count": len(closed_with_pnl),
        "win_count": sum(float(row["pnl"]) > 0 for row in closed_with_pnl),
        "win_rate_pct": (
            sum(float(row["pnl"]) > 0 for row in closed_with_pnl)
            / len(closed_with_pnl)
            * 100
            if closed_with_pnl
            else None
        ),
        "best_trade": _trade_label(best) if best else None,
        "worst_trade": _trade_label(worst) if worst else None,
        "close_side_breakdown": close_side_breakdown,
        "excluded_rejected_rows": len(rejected_rows),
        "excluded_rejected_row_ids": rejected_rows,
        "excluded_incomplete_rows": len(incomplete_rows),
        "excluded_incomplete_row_ids": incomplete_rows,
        "excluded_non_fill_rows": len(non_fill_rows),
        "fill_rows": [
            {
                "id": int(row["id"]),
                "cycle_id": row["cycle_id"],
                "ts": row["ts"],
                "symbol": row["symbol"],
                "action": row["action"],
                "side": row["side"],
                "sz": row["sz"],
                "fill_px": row["fill_px"],
                "pnl": row["pnl"],
            }
            for row in fills
        ],
    }


def realized_performance_stats(
    db_path: Path,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = True,
) -> dict:
    """Compute transparent monthly metrics from confirmed close-fill PnL.

    ``max_drawdown_usdt`` is the largest peak-to-trough loss on the cumulative
    realized-PnL curve, starting from zero. ``sharpe_approx`` is the annualized
    sample mean/stdev of 08:00-anchored daily realized PnL, including zero-PnL
    days and using ``sqrt(365)``.  It is ``None`` when variance is zero.
    These are reporting diagnostics, not account-equity or risk-gate inputs.
    """
    start = parse_cst(fmt_ts(start_ts))
    end = parse_cst(fmt_ts(end_ts))
    if end <= start:
        raise ValueError("performance interval must have end > start")
    seconds = (end - start).total_seconds()
    if seconds % 86400 != 0:
        raise ValueError("performance interval must tile whole 24h fact days")

    stats = filled_trade_stats(
        Path(db_path),
        start.strftime(TS_FMT),
        end.strftime(TS_FMT),
        end_exclusive=end_exclusive,
    )
    closes = [
        row for row in stats["fill_rows"]
        if row["action"] == "close" and row.get("pnl") is not None
    ]

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in closes:
        cumulative += float(row["pnl"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    day_count = int(seconds // 86400)
    daily_pnl = [0.0 for _ in range(day_count)]
    for row in closes:
        row_ts = parse_cst(str(row["ts"]))
        index = int((row_ts - start).total_seconds() // 86400)
        if 0 <= index < day_count:
            daily_pnl[index] += float(row["pnl"])

    sharpe = None
    if len(daily_pnl) >= 2:
        deviation = statistics.stdev(daily_pnl)
        if deviation > 1e-12:
            sharpe = statistics.mean(daily_pnl) / deviation * math.sqrt(365.0)

    return {
        "source": str(Path(db_path)),
        "period_start_ts": start.strftime(TS_FMT),
        "period_end_ts": end.strftime(TS_FMT),
        "period_end_exclusive": bool(end_exclusive),
        "realized_pnl": float(stats["realized_pnl"]),
        "close_count": int(stats["close_count"]),
        "closed_with_pnl_count": int(stats["closed_with_pnl_count"]),
        "max_drawdown_usdt": float(max_drawdown),
        "sharpe_approx": sharpe,
        "daily_anchor": "08:00 Asia/Shanghai",
        "daily_observations": day_count,
        "daily_realized_pnl": daily_pnl,
        "definitions": {
            "max_drawdown_usdt": (
                "peak-to-trough drawdown of cumulative confirmed close-fill "
                "realized PnL, starting at zero"
            ),
            "sharpe_approx": (
                "annualized mean/stdev of 08:00-anchored daily realized PnL; "
                "zero-PnL days included; sqrt(365); no risk-free adjustment"
            ),
        },
    }


def risk_rejected_open_attempts(
    ledger_path: Path,
    profile: str,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = False,
) -> dict:
    """Read risk-gate rejects, independently from filled trade rows."""
    start = fmt_ts(start_ts)
    end = fmt_ts(end_ts)
    op = "<" if end_exclusive else "<="
    con = _connect_ro(Path(ledger_path))
    try:
        rows = con.execute(
            "SELECT profile,cycle_id,symbol,action,side,state,reserved_at,"
            "updated_at,error FROM execution_intents "
            "WHERE profile=? AND action='open' AND state='failed_clean' "
            "AND error LIKE 'risk_reject:%' "
            "AND datetime(reserved_at)>=datetime(?) "
            f"AND datetime(reserved_at){op}datetime(?) "
            "ORDER BY datetime(reserved_at),symbol,side",
            (profile, start, end),
        ).fetchall()
    finally:
        con.close()

    items = []
    reasons: Counter[str] = Counter()
    for raw_row in rows:
        row = dict(raw_row)
        reason = str(row.get("error") or "").removeprefix("risk_reject:")
        reasons[reason or "unknown"] += 1
        items.append({
            "cycle_id": row["cycle_id"],
            "reserved_at": row["reserved_at"],
            "symbol": row["symbol"],
            "side": row["side"],
            "reason": reason or "unknown",
        })
    return {
        "source": str(Path(ledger_path)),
        "count": len(items),
        "reasons": dict(sorted(reasons.items())),
        "items": items,
    }


def open_position_avg_hold_hours(
    db_path: Path, as_of_ts: str
) -> float | None:
    """Approximate current ledger position age with FIFO lots.

    This is used only for the weekly turnover diagnostic.  It never replaces
    exchange/API position truth.
    """
    end = fmt_ts(as_of_ts)
    con = _connect_ro(Path(db_path))
    try:
        rows = con.execute(
            "SELECT id,ts,symbol,action,side,sz,fill_px,raw FROM trades "
            "WHERE datetime(ts)<=datetime(?) ORDER BY datetime(ts),id",
            (end,),
        ).fetchall()
    finally:
        con.close()

    lots: dict[tuple[str, str], list[list[Any]]] = defaultdict(list)
    for raw_row in rows:
        row = dict(raw_row)
        if _explicitly_rejected(row) or not _positive(row.get("sz")):
            continue
        action = str(row.get("action") or "").strip().lower()
        key = (str(row.get("symbol") or ""), str(row.get("side") or ""))
        qty = float(row["sz"])
        if action in POSITION_INCREASE_ACTIONS:
            if not _positive(row.get("fill_px")):
                continue
            lots[key].append([qty, parse_cst(str(row["ts"]))])
        elif action in POSITION_DECREASE_ACTIONS:
            remaining = qty
            while remaining > 1e-12 and lots[key]:
                take = min(remaining, float(lots[key][0][0]))
                lots[key][0][0] -= take
                remaining -= take
                if lots[key][0][0] <= 1e-12:
                    lots[key].pop(0)

    end_dt = parse_cst(end)
    weighted_hours = 0.0
    total_qty = 0.0
    for open_lots in lots.values():
        for qty, opened_at in open_lots:
            if qty <= 0:
                continue
            weighted_hours += qty * (end_dt - opened_at).total_seconds() / 3600
            total_qty += qty
    return weighted_hours / total_qty if total_qty > 0 else None


def closed_position_hold_stats(
    db_path: Path,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = True,
) -> dict:
    """Return FIFO holding time for confirmed close fills in the report window.

    Lots are reconstructed from all confirmed position-changing fills before
    ``end_ts`` so a position opened before the report window is still paired
    correctly.  Each fully matched ``close`` row contributes one observation;
    multiple FIFO lots consumed by that close are quantity-weighted first, then
    observations are averaged equally across close fills.  Any unmatched close
    makes the aggregate unknown instead of silently publishing a partial mean.
    """
    start = parse_cst(fmt_ts(start_ts))
    end = parse_cst(fmt_ts(end_ts))
    con = _connect_ro(Path(db_path))
    try:
        op = "<" if end_exclusive else "<="
        rows = con.execute(
            "SELECT id,ts,symbol,action,side,sz,fill_px,raw FROM trades "
            f"WHERE datetime(ts){op}datetime(?) ORDER BY datetime(ts),id",
            (end.strftime(TS_FMT),),
        ).fetchall()
    finally:
        con.close()

    lots: dict[tuple[str, str], list[list[Any]]] = defaultdict(list)
    samples: list[float] = []
    unmatched_close_row_ids: list[int] = []
    for raw_row in rows:
        row = dict(raw_row)
        if (_explicitly_rejected(row) or not _positive(row.get("sz"))
                or not _positive(row.get("fill_px"))):
            continue
        action = str(row.get("action") or "").strip().lower()
        if action not in POSITION_INCREASE_ACTIONS | POSITION_DECREASE_ACTIONS:
            continue
        row_ts = parse_cst(str(row["ts"]))
        key = (str(row.get("symbol") or ""), str(row.get("side") or ""))
        qty = float(row["sz"])
        if action in POSITION_INCREASE_ACTIONS:
            lots[key].append([qty, row_ts])
            continue

        remaining = qty
        matched = 0.0
        weighted_hours = 0.0
        while remaining > 1e-12 and lots[key]:
            lot_qty, opened_at = lots[key][0]
            take = min(remaining, float(lot_qty))
            weighted_hours += take * max(
                0.0, (row_ts - opened_at).total_seconds() / 3600.0)
            matched += take
            remaining -= take
            lots[key][0][0] -= take
            if lots[key][0][0] <= 1e-12:
                lots[key].pop(0)

        in_window = row_ts >= start and (
            row_ts < end if end_exclusive else row_ts <= end)
        if not in_window or action != "close":
            continue
        tolerance = max(1e-9, qty * 1e-9)
        if matched <= 0 or remaining > tolerance:
            unmatched_close_row_ids.append(int(row["id"]))
            continue
        samples.append(weighted_hours / matched)

    average = (
        sum(samples) / len(samples)
        if samples and not unmatched_close_row_ids else None
    )
    return {
        "closed_position_avg_hold_hours": average,
        "closed_position_hold_sample_count": len(samples),
        "closed_position_hold_unmatched_count": len(unmatched_close_row_ids),
        "closed_position_hold_unmatched_row_ids": unmatched_close_row_ids,
        "closed_position_hold_definition": (
            "FIFO quantity-weighted hours per confirmed close fill; "
            "arithmetic mean across fully matched close fills"
        ),
    }


def profile_statistics(
    profile: str,
    trade_db: Path,
    ledger_db: Path,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = False,
    include_avg_hold: bool = False,
) -> dict:
    fills = filled_trade_stats(
        trade_db, start_ts, end_ts, end_exclusive=end_exclusive
    )
    rejects = risk_rejected_open_attempts(
        ledger_db, profile, start_ts, end_ts, end_exclusive=end_exclusive
    )
    result = {
        **fills,
        "risk_rejected_open_attempts": rejects,
    }
    if include_avg_hold:
        result.update(closed_position_hold_stats(
            trade_db,
            start_ts,
            end_ts,
            end_exclusive=end_exclusive,
        ))
        # Keep the current-open age as a separately named turnover diagnostic;
        # it must never populate weekly_reports.avg_hold_hours.
        result["open_position_avg_hold_hours"] = (
            open_position_avg_hold_hours(trade_db, end_ts)
        )
    return result


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only authoritative fill/reject metrics for reports"
    )
    parser.add_argument("--profile", choices=("live", "both"),
                        default="both")
    parser.add_argument("--as-of", default=now_cst())
    parser.add_argument("--window", choices=("daily", "rolling", "explicit"),
                        default="daily")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--start-ts")
    parser.add_argument("--end-ts")
    parser.add_argument("--end-exclusive", action="store_true")
    parser.add_argument("--include-avg-hold", action="store_true")
    parser.add_argument(
        "--include-rows", action="store_true",
        help="包含逐笔 fill 明细；默认仅输出紧凑汇总",
    )
    parser.add_argument("--db-root", default=r"./db")
    args = parser.parse_args()

    as_of = fmt_ts(args.end_ts or args.as_of)
    if args.window == "daily":
        start, end = daily_window(as_of)
    elif args.window == "rolling":
        start, end = rolling_window(as_of, args.days)
    else:
        if not args.start_ts:
            parser.error("--window explicit requires --start-ts")
        start, end = fmt_ts(args.start_ts), as_of

    # Daily reviewer windows are adjacent half-open intervals.  The end must be
    # exclusive so an exact 08:05:00 fill cannot be counted in two reports.
    end_exclusive = bool(args.end_exclusive or args.window == "daily")
    root = Path(args.db_root)
    profiles = ("live",) if args.profile == "both" else (args.profile,)
    payload = {
        "as_of_ts": as_of,
        "period_start_ts": start,
        "period_end_ts": end,
        "period_end_exclusive": end_exclusive,
        "profiles": {},
    }
    for profile in profiles:
        stats = profile_statistics(
            profile,
            root / f"{profile}_trades.db",
            root / "ledger.db",
            start,
            end,
            end_exclusive=end_exclusive,
            include_avg_hold=args.include_avg_hold,
        )
        if not args.include_rows:
            stats.pop("fill_rows", None)
        payload["profiles"][profile] = stats
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
