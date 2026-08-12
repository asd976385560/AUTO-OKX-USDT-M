"""Read-only pre-send validator for weekly and monthly reviewer Markdown.

The producer and this validator deliberately keep separate calendar-window
implementations.  Trade/fill classification is shared through
``trade_report_stats`` just like the daily validator.  This script never
writes a database, report, repair queue, or external message.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import trade_report_stats


def _open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA query_only=ON")
    return con


def _json_obj(value: Any) -> dict:
    try:
        decoded = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _same(left: Any, right: Any, tolerance: float = 1e-8) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def _number(value: str) -> float | None:
    text = str(value).strip()
    if text == "—":
        return None
    return float(text)


def _extract_header(content: str) -> tuple[str, tuple[str, str]]:
    key_match = re.search(
        r"(?m)^> 报告键：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
        content,
    )
    window_match = re.search(
        r"(?m)^> 统计窗口：\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}), "
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\)，UTC\+8$",
        content,
    )
    if not key_match or not window_match:
        raise ValueError("missing canonical report key/window header")
    return key_match.group(1), (window_match.group(1), window_match.group(2))


def _expected_window(kind: str, key: str) -> tuple[str, str]:
    """Independently restate the Monday/month-day-1 08:00 contract."""
    ref = trade_report_stats.parse_cst(key)
    if any((ref.hour, ref.minute, ref.second, ref.microsecond)):
        raise ValueError("period report key must be 00:00:00")
    if kind == "weekly":
        if ref.weekday() != 0:
            raise ValueError("weekly report key must be Monday")
        end = ref.replace(hour=8)
        start = end - timedelta(days=7)
    else:
        if ref.day != 1:
            raise ValueError("monthly report key must be month day 1")
        end = ref.replace(day=1, hour=8)
        start = (end - timedelta(days=1)).replace(day=1, hour=8)
    return start.strftime(trade_report_stats.TS_FMT), end.strftime(
        trade_report_stats.TS_FMT)


def _parse_weekly_row(content: str) -> dict:
    match = re.search(
        r"(?m)^\| 实盘 \| (\d+) \| (\d+) \| ([^|]+) \| "
        r"([^|]+)% \| ([^|]+) \| ([^|]+) \|$",
        content,
    )
    if not match:
        raise ValueError("weekly live metrics row missing")
    reject = re.match(r"\s*(\d+)\s*笔", match.group(5))
    if not reject:
        raise ValueError("weekly risk-reject count missing")
    return {
        "open_count": int(match.group(1)),
        "close_count": int(match.group(2)),
        "total_pnl": _number(match.group(3)),
        "win_rate": _number(match.group(4)),
        "risk_reject_count": int(reject.group(1)),
        "avg_hold_hours": _number(match.group(6)),
    }


def _parse_monthly_row(content: str) -> dict:
    match = re.search(
        r"(?m)^\| 实盘 \| (\d+) \| (\d+) \| ([^|]+) \| "
        r"([^|]+) \| ([^|]+) \| ([^|]+) \|$",
        content,
    )
    if not match:
        raise ValueError("monthly live metrics row missing")
    reject = re.match(r"\s*(\d+)\s*笔", match.group(6))
    if not reject:
        raise ValueError("monthly risk-reject count missing")
    return {
        "open_count": int(match.group(1)),
        "close_count": int(match.group(2)),
        "total_pnl": _number(match.group(3)),
        "max_drawdown": _number(match.group(4)),
        "sharpe_approx": _number(match.group(5)),
        "risk_reject_count": int(reject.group(1)),
    }


def _parse_side_rows(content: str) -> dict[str, dict[str, Any]]:
    """Parse the fixed direction table; units are explicit and never inferred."""
    out: dict[str, dict[str, Any]] = {}
    for label, side in (("多", "long"), ("空", "short")):
        match = re.search(
            rf"(?m)^\| {label} \| (\d+) \| (\d+) \| ([^|]+) \| "
            rf"([^|]+) \| ([^|]+) \|$",
            content,
        )
        if not match:
            raise ValueError(f"direction detail row missing: {label}")
        wr_text = match.group(3).strip()
        if wr_text != "—" and not wr_text.endswith("%"):
            raise ValueError(f"direction win-rate unit missing: {label}")
        out[side] = {
            "close_count": int(match.group(1)),
            "win_count": int(match.group(2)),
            "win_rate_pct": _number(wr_text[:-1] if wr_text.endswith("%") else wr_text),
            "pnl_sum_usdt": _number(match.group(4)),
            "pnl_avg_usdt": _number(match.group(5)),
        }
    return out


def _missed_count(path: Path | None, start: str, end: str) -> int | None:
    if path is None or not path.exists():
        return None
    con = _open_ro(path)
    try:
        if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='missed_opportunities'"
        ).fetchone():
            return None
        return int(con.execute(
            "SELECT COUNT(*) FROM missed_opportunities "
            "WHERE ts LIKE '202%' AND datetime(ts)>=datetime(?) "
            "AND datetime(ts)<datetime(?)",
            (start, end),
        ).fetchone()[0])
    finally:
        con.close()


def validate_report(
    *,
    kind: str,
    report_path: Path,
    account_db: Path,
    live_trades_db: Path,
    ledger_db: Path,
    lessons_db: Path | None = None,
) -> dict:
    if kind not in {"weekly", "monthly"}:
        raise ValueError("kind must be weekly or monthly")
    content = report_path.read_text(encoding="utf-8")
    errors: list[str] = []
    checks: list[str] = []
    title = "# 小灵周报 " if kind == "weekly" else "# 小灵月报 "
    if title not in content or "## 成交与绩效" not in content:
        errors.append("structure: required title/section missing")
    if "报告状态：" not in content:
        errors.append("structure: report status missing")
    if re.search(r"(?i)\bhit[_ ]?1r\b", content):
        errors.append("semantics: legacy hit_1R/hit1R token is forbidden")

    key, markdown_window = _extract_header(content)
    expected_start, expected_end = _expected_window(kind, key)
    if markdown_window != (expected_start, expected_end):
        errors.append("window: markdown period differs from fixed contract")
    else:
        checks.append(f"{kind}_window")

    markdown = (
        _parse_weekly_row(content)
        if kind == "weekly" else _parse_monthly_row(content)
    )
    side_markdown = _parse_side_rows(content)
    table = "weekly_reports" if kind == "weekly" else "monthly_reports"
    key_col = "week_start_ts" if kind == "weekly" else "month_start_ts"
    columns = (
        "open_count,close_count,total_pnl,win_rate,avg_hold_hours,raw"
        if kind == "weekly"
        else "total_pnl,max_drawdown,sharpe_approx,raw"
    )
    con = _open_ro(account_db)
    try:
        rows = con.execute(
            f"SELECT {columns} FROM {table} WHERE {key_col}=? "
            "AND profile='live'",
            (key,),
        ).fetchall()
    finally:
        con.close()
    if len(rows) != 1:
        errors.append(
            f"database: expected one live {table} row, got {len(rows)}")
        return {
            "ok": False,
            "kind": kind,
            "report_key": key,
            "errors": errors,
            "checks": checks,
            "auto_send": False,
        }
    row = rows[0]
    raw = _json_obj(row["raw"])
    audit = raw.get("report_audit")
    if not isinstance(audit, dict) or audit.get("period_kind") != kind:
        errors.append("audit: report_audit missing or wrong period_kind")
        audit = {}
    embedded = (audit.get("trade_metrics") or {}).get("live")
    if not isinstance(embedded, dict):
        errors.append("audit: embedded live trade metrics missing")
        embedded = {}
    facts = trade_report_stats.profile_statistics(
        "live",
        live_trades_db,
        ledger_db,
        expected_start,
        expected_end,
        end_exclusive=True,
        include_avg_hold=(kind == "weekly"),
    )
    reject_count = facts["risk_rejected_open_attempts"]["count"]
    side_facts = facts.get("close_side_breakdown") or {}
    common = (
        markdown["open_count"] == facts["open_count"],
        markdown["close_count"] == facts["close_count"],
        _same(markdown["total_pnl"], facts["realized_pnl"], 5e-5),
        markdown["risk_reject_count"] == reject_count,
        embedded.get("period_start_ts") == expected_start,
        embedded.get("period_end_ts") == expected_end,
        embedded.get("period_end_exclusive") is True,
        embedded.get("open_count") == facts["open_count"],
        embedded.get("close_count") == facts["close_count"],
        _same(embedded.get("realized_pnl"), facts["realized_pnl"]),
        ((embedded.get("risk_rejected_open_attempts") or {}).get("count")
         == reject_count),
    )
    if not all(common):
        errors.append("facts: markdown/audit differs from authoritative ledgers")
    for side in ("long", "short"):
        shown = side_markdown.get(side) or {}
        expected_side = side_facts.get(side) or {}
        embedded_side = (embedded.get("close_side_breakdown") or {}).get(side) or {}
        if not all((
            shown.get("close_count") == expected_side.get("close_count"),
            shown.get("win_count") == expected_side.get("win_count"),
            _same(shown.get("win_rate_pct"), expected_side.get("win_rate_pct"), 5e-3),
            _same(shown.get("pnl_sum_usdt"), expected_side.get("pnl_sum_usdt"), 5e-5),
            _same(shown.get("pnl_avg_usdt"), expected_side.get("pnl_avg_usdt"), 5e-5),
            embedded_side.get("close_count") == expected_side.get("close_count"),
            embedded_side.get("win_count") == expected_side.get("win_count"),
            _same(embedded_side.get("win_rate_pct"), expected_side.get("win_rate_pct")),
            _same(embedded_side.get("pnl_sum_usdt"), expected_side.get("pnl_sum_usdt")),
            _same(embedded_side.get("pnl_avg_usdt"), expected_side.get("pnl_avg_usdt")),
        )):
            errors.append(f"facts: {side} direction detail differs")

    missed = _missed_count(lessons_db, expected_start, expected_end)
    if missed is not None:
        match = re.search(r"本窗口错失机会记录[： :]\s*(\d+)\s*条", content)
        if not match:
            errors.append("facts: deterministic missed-opportunity count missing")
        elif int(match.group(1)) != missed:
            errors.append(
                f"facts: missed-opportunity count {match.group(1)}!={missed}")

    if kind == "weekly":
        if not all((
            row["open_count"] == facts["open_count"],
            row["close_count"] == facts["close_count"],
            _same(row["total_pnl"], facts["realized_pnl"]),
            _same(row["win_rate"], facts["win_rate_pct"]),
            _same(markdown["win_rate"], facts["win_rate_pct"], 5e-3),
            _same(
                row["avg_hold_hours"],
                facts.get("closed_position_avg_hold_hours"),
            ),
            _same(
                markdown["avg_hold_hours"],
                facts.get("closed_position_avg_hold_hours"),
                5e-3,
            ),
        )):
            errors.append("database: weekly stored metrics differ")
    else:
        performance = trade_report_stats.realized_performance_stats(
            live_trades_db,
            expected_start,
            expected_end,
            end_exclusive=True,
        )
        embedded_perf = (audit.get("performance_metrics") or {}).get("live")
        if not isinstance(embedded_perf, dict):
            errors.append("audit: monthly performance metrics missing")
            embedded_perf = {}
        if not all((
            _same(row["total_pnl"], facts["realized_pnl"]),
            _same(row["max_drawdown"], performance["max_drawdown_usdt"]),
            _same(row["sharpe_approx"], performance["sharpe_approx"]),
            _same(
                markdown["max_drawdown"],
                performance["max_drawdown_usdt"],
                5e-5,
            ),
            _same(
                markdown["sharpe_approx"],
                performance["sharpe_approx"],
                5e-5,
            ),
            _same(
                embedded_perf.get("max_drawdown_usdt"),
                performance["max_drawdown_usdt"],
            ),
            _same(
                embedded_perf.get("sharpe_approx"),
                performance["sharpe_approx"],
            ),
        )):
            errors.append("database: monthly stored performance differs")

    if not errors:
        checks.extend(["structure", "report_audit", "authoritative_facts"])
    return {
        "ok": not errors,
        "kind": kind,
        "report_key": key,
        "period_start_ts": expected_start,
        "period_end_ts": expected_end,
        "errors": errors,
        "checks": sorted(set(checks)),
        "auto_send": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="read-only weekly/monthly report pre-send validator")
    parser.add_argument("--kind", choices=("weekly", "monthly"), required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--db-root", default=r"./db")
    parser.add_argument("--account-db")
    parser.add_argument("--live-trades-db")
    parser.add_argument("--ledger-db")
    parser.add_argument("--lessons-db")
    args = parser.parse_args()
    root = Path(args.db_root)
    paths = {
        "report_path": Path(args.file),
        "account_db": Path(args.account_db) if args.account_db else root / "account.db",
        "live_trades_db": (
            Path(args.live_trades_db)
            if args.live_trades_db else root / "live_trades.db"
        ),
        "ledger_db": Path(args.ledger_db) if args.ledger_db else root / "ledger.db",
        "lessons_db": Path(args.lessons_db) if args.lessons_db else root / "lessons.db",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        print(json.dumps(
            {"ok": False, "error": "missing input", "paths": missing},
            ensure_ascii=False,
        ), file=sys.stderr)
        return 2
    try:
        result = validate_report(kind=args.kind, **paths)
    except Exception as exc:
        print(json.dumps(
            {"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
