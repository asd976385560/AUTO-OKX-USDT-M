# -*- coding: utf-8 -*-
"""Read-only validator for reviewer daily Markdown before external delivery.

Checks the report structure, reconciliation state, risk-reject counts, embedded
``raw.report_audit`` contract, revision/resend-review state, and authoritative
trade/intent facts for the report-time window.  It never writes repair_queue,
changes a report, or sends a message.
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
import re
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import trade_report_stats


REQUIRED_MARKERS = (
    "# 📊 小灵日报 ",
    "## 💰 资产",
    "## 📈 持仓",
    "## 🎯 交易",
    "### 🟢 实盘",
    "### 🟡 模拟盘",
    "## ⚠️ 异常 / 🛠 自修",
    "## 🌍 市场",
    "## 🧠 教训",
    "## 详细 summary",
)
PROFILE_LABELS = {"live": "🟢 实盘", "demo": "🟡 模拟盘"}
FINAL_RECONCILE = {"clean", "ok", "cleared", "final"}


def _open_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _json_object(value: Any) -> dict:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _extract_report_ts(content: str) -> str | None:
    match = re.search(
        r"(?m)^>\s*ts:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\b",
        content,
    )
    return match.group(1) if match else None


def _expected_daily_window(report_ts: str) -> tuple[str, str]:
    """Independently derive the fixed ``[前一日 08:00, 当日 08:00)`` contract.

    Deliberately re-states the anchor instead of importing it from
    trade_report_stats: this validator exists to catch the producer drifting
    from the contract, so sharing the constant would make the check tautological.
    """
    ref = trade_report_stats.parse_cst(report_ts)
    end = ref.replace(hour=8, minute=0, second=0, microsecond=0)
    if ref < end:
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _extract_period_window(content: str) -> tuple[str, str] | None:
    match = re.search(
        r"(?m)^>\s*统计窗口:\s*\["
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\s*"
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
        r"\)，UTC\+8（固定24小时）\s*$",
        content,
    )
    return (match.group(1), match.group(2)) if match else None


def _extract_revision_line(content: str) -> dict | None:
    match = re.search(
        r"(?m)^>\s*report_revision:\s*(\d+)\s*\|\s*"
        r"revision_kind:\s*([a-z_]+)\s*\|\s*"
        r"resend_review_required:\s*(true|false)\s*\|\s*"
        r"auto_resend:\s*(true|false)\s*$",
        content,
        re.IGNORECASE,
    )
    if not match:
        return None
    return {
        "number": int(match.group(1)),
        "kind": match.group(2).lower(),
        "resend_review_required": match.group(3).lower() == "true",
        "auto_resend": match.group(4).lower() == "true",
    }


def _trading_profile_block(content: str, profile: str) -> str | None:
    if "## 🎯 交易" not in content:
        return None
    trading = content.split("## 🎯 交易", 1)[1]
    trading = trading.split("## ⚠️", 1)[0]
    label = PROFILE_LABELS[profile]
    marker = f"### {label}"
    if marker not in trading:
        return None
    block = trading.split(marker, 1)[1]
    if profile == "live" and "### 🟡 模拟盘" in block:
        block = block.split("### 🟡 模拟盘", 1)[0]
    return block


def _parse_profile_metrics(block: str | None) -> dict | None:
    if not block:
        return None
    patterns = {
        "open_count": r"本复盘周期成交开仓:\s*(\d+)\s*笔",
        "close_count": r"本复盘周期成交平仓:\s*(\d+)\s*笔",
        "risk_reject_count": r"开仓尝试被风控拒绝:\s*(\d+)\s*笔",
        "total_pnl": r"净 PnL:\s*\$?\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))",
    }
    values = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, block)
        if not match:
            return None
        values[key] = (
            float(match.group(1)) if key == "total_pnl"
            else int(match.group(1))
        )
    return values


def _same_number(left: Any, right: Any, digits: int = 8) -> bool:
    try:
        return round(float(left), digits) == round(float(right), digits)
    except (TypeError, ValueError):
        return False


def validate_report(
    report_path: Path,
    account_db: Path,
    live_trades_db: Path,
    demo_trades_db: Path,
    ledger_db: Path,
) -> dict:
    errors: list[str] = []
    checks: list[str] = []
    content = report_path.read_text(encoding="utf-8")

    for marker in REQUIRED_MARKERS:
        if marker not in content:
            errors.append(f"structure: missing marker {marker}")
    if not errors:
        checks.append("structure")

    report_ts = _extract_report_ts(content)
    if not report_ts:
        errors.append("structure: missing canonical report ts")
        return {"ok": False, "errors": errors, "checks": checks}
    if report_ts[:10] not in content.splitlines()[0]:
        errors.append("structure: title date differs from report ts")
    start_ts, end_ts = _expected_daily_window(report_ts)
    markdown_window = _extract_period_window(content)
    if markdown_window is None:
        errors.append("window: missing fixed 24h period line")
    elif markdown_window != (start_ts, end_ts):
        errors.append("window: markdown period is not trailing 24h")
    else:
        checks.append("daily_window_24h")

    revision_line = _extract_revision_line(content)
    if revision_line is None:
        errors.append("revision: missing machine-readable revision line")

    markdown_metrics = {
        profile: _parse_profile_metrics(
            _trading_profile_block(content, profile))
        for profile in ("live", "demo")
    }
    for profile, metrics in markdown_metrics.items():
        if metrics is None:
            errors.append(f"structure: incomplete {profile} trade metrics")

    previous_row = None
    con = _open_readonly(account_db)
    try:
        rows = con.execute(
            "SELECT ts,profile,open_count,close_count,total_pnl,raw "
            "FROM daily_reports WHERE ts=? ORDER BY profile",
            (report_ts,),
        ).fetchall()
        previous_row = con.execute(
            "SELECT ts,raw FROM daily_reports "
            "WHERE profile='live' AND ts<? ORDER BY ts DESC LIMIT 1",
            (report_ts,),
        ).fetchone()
    finally:
        con.close()
    by_profile = {str(row["profile"]): row for row in rows}
    if set(by_profile) != {"live", "demo"} or len(rows) != 2:
        errors.append("database: report ts must have exactly live+demo rows")
        return {"ok": False, "errors": errors, "checks": checks}

    audit_by_profile: dict[str, dict] = {}
    revision_by_profile: dict[str, dict] = {}
    for profile, row in by_profile.items():
        raw = _json_object(row["raw"])
        audit = raw.get("report_audit")
        if not isinstance(audit, dict):
            errors.append(f"audit: {profile} report_audit missing")
            continue
        audit_by_profile[profile] = audit
        if audit.get("version") != 1 or audit.get("period_kind") != "daily":
            errors.append(f"audit: {profile} audit version/period invalid")
        state = audit.get("report_state")
        metrics_all = audit.get("trade_metrics")
        revision = audit.get("revision")
        if not isinstance(state, dict):
            errors.append(f"reconciliation: {profile} report_state missing")
        if not isinstance(metrics_all, dict):
            errors.append(f"audit: {profile} trade_metrics missing")
        if not isinstance(revision, dict):
            errors.append(f"revision: {profile} revision missing")
        else:
            revision_by_profile[profile] = revision
            required_revision = {
                "number", "kind", "corrected", "resend_review_required",
                "resend_status", "auto_resend",
            }
            if not required_revision.issubset(revision):
                errors.append(f"revision: {profile} revision fields missing")
            if revision.get("auto_resend") is not False:
                errors.append(f"revision: {profile} auto_resend must be false")
            if revision.get("kind") == "corrected" and not revision.get(
                    "resend_review_required"):
                errors.append(
                    f"revision: {profile} correction requires resend review")
            if revision_line and any((
                revision_line["number"] != revision.get("number"),
                revision_line["kind"] != revision.get("kind"),
                revision_line["resend_review_required"] != bool(
                    revision.get("resend_review_required")),
                revision_line["auto_resend"] != bool(
                    revision.get("auto_resend")),
            )):
                errors.append(
                    f"revision: {profile} markdown/audit state differs")

    if len(revision_by_profile) == 2 and (
        revision_by_profile["live"] != revision_by_profile["demo"]
    ):
        errors.append("revision: live/demo revision state differs")

    if len(audit_by_profile) == 2:
        checks.append("report_audit")
        reference_state = audit_by_profile["live"].get("report_state") or {}
        status = str(reference_state.get("status") or "").lower()
        reconcile = str(
            reference_state.get("live_reconcile_status") or "").lower()
        try:
            issue_count = int(
                reference_state.get("live_reconcile_issue_count") or 0)
        except (TypeError, ValueError):
            issue_count = -1
        is_final = (
            status == "final"
            and reconcile in FINAL_RECONCILE
            and issue_count == 0
        )
        is_provisional = status == "provisional" and not is_final
        if not (is_final or is_provisional):
            errors.append("reconciliation: state/status combination invalid")
        if is_final and "最终报告" not in content:
            errors.append("reconciliation: final audit lacks final banner")
        if is_provisional and "临时报告" not in content:
            errors.append(
                "reconciliation: provisional audit lacks provisional banner")
        other_state = audit_by_profile["demo"].get("report_state") or {}
        if other_state != reference_state:
            errors.append("reconciliation: live/demo report_state differs")
        if not any(error.startswith("reconciliation:") for error in errors):
            checks.append("reconciliation")

    if previous_row is not None:
        previous_audit = _json_object(previous_row["raw"]).get(
            "report_audit") or {}
        previous_metrics = (
            previous_audit.get("trade_metrics") or {}
        ).get("live") or {}
        previous_end = previous_metrics.get("period_end_ts")
        if previous_end and previous_end != start_ts:
            errors.append(
                "window: gap or overlap with previous daily report")
        elif previous_end == start_ts:
            checks.append("daily_window_continuity")

    authoritative = {}
    for profile, trade_db in (
        ("live", live_trades_db), ("demo", demo_trades_db)
    ):
        authoritative[profile] = trade_report_stats.profile_statistics(
            profile,
            trade_db,
            ledger_db,
            start_ts,
            end_ts,
            end_exclusive=True,
        )

    for profile in ("live", "demo"):
        row = by_profile[profile]
        audit = audit_by_profile.get(profile) or {}
        metrics_all = audit.get("trade_metrics") or {}
        embedded = metrics_all.get(profile)
        if not isinstance(embedded, dict):
            errors.append(f"audit: {profile} embedded metrics missing")
            continue
        facts = authoritative[profile]
        markdown = markdown_metrics.get(profile)
        comparisons = (
            row["open_count"] == embedded.get("open_count")
            == facts["open_count"],
            row["close_count"] == embedded.get("close_count")
            == facts["close_count"],
            _same_number(row["total_pnl"], embedded.get("realized_pnl"))
            and _same_number(row["total_pnl"], facts["realized_pnl"]),
            embedded.get("period_start_ts") == start_ts,
            embedded.get("period_end_ts") == end_ts,
            embedded.get("period_end_exclusive") is True,
        )
        if not all(comparisons):
            errors.append(f"audit: {profile} report-time facts differ")
        reject_count = (
            embedded.get("risk_rejected_open_attempts") or {}
        ).get("count")
        if reject_count != facts["risk_rejected_open_attempts"]["count"]:
            errors.append(f"risk_reject: {profile} audit differs from ledger")
        if markdown is not None:
            if (
                markdown["open_count"] != row["open_count"]
                or markdown["close_count"] != row["close_count"]
                or round(markdown["total_pnl"], 2)
                != round(float(row["total_pnl"] or 0), 2)
            ):
                errors.append(f"structure: {profile} markdown metrics differ")
            if markdown["risk_reject_count"] != reject_count:
                errors.append(
                    f"risk_reject: {profile} markdown count differs")

    if not any(error.startswith("risk_reject:") for error in errors):
        checks.append("risk_reject")
    if not any(error.startswith("revision:") for error in errors):
        checks.append("revision")
    if not any(error.startswith("audit:") for error in errors):
        checks.append("authoritative_report_time_facts")
    return {
        "ok": not errors,
        "report_ts": report_ts,
        "errors": errors,
        "checks": sorted(set(checks)),
        "profiles_checked": 2,
        "auto_send": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="read-only reviewer daily report validator")
    parser.add_argument("--file", required=True)
    parser.add_argument("--db-root", default=_project_path('db'))
    parser.add_argument("--account-db")
    parser.add_argument("--live-trades-db")
    parser.add_argument("--demo-trades-db")
    parser.add_argument("--ledger-db")
    args = parser.parse_args()
    root = Path(args.db_root)
    paths = {
        "report_path": Path(args.file),
        "account_db": Path(args.account_db) if args.account_db else root / "account.db",
        "live_trades_db": (
            Path(args.live_trades_db)
            if args.live_trades_db else root / "live_trades.db"),
        "demo_trades_db": (
            Path(args.demo_trades_db)
            if args.demo_trades_db else root / "demo_trades.db"),
        "ledger_db": (
            Path(args.ledger_db)
            if args.ledger_db else root / "ledger.db"),
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        print(json.dumps(
            {"ok": False, "error": "missing input", "paths": missing},
            ensure_ascii=False,
        ), file=sys.stderr)
        return 2
    try:
        result = validate_report(**paths)
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
