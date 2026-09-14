"""Read-only pre-send validator for weekly and monthly reviewer Markdown.

The producer and this validator deliberately keep separate calendar-window
implementations.  Trade/fill classification is shared through
``trade_report_stats`` just like the daily validator.  This script never
writes a database, report, repair queue, or external message.
"""
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
from datetime import timedelta
from pathlib import Path
from typing import Any

import trade_report_stats
import _acceptance_thresholds as thresholds

MISSED_OPPORTUNITY_OUTCOME_HOURS = 4
# 2026-08-19 P0-1 激活边界（只向前）：周期事实窗起点达到该边界后，头条
# total_pnl 的口径才是 close + reduce（total_realized_pnl）。跨边界周期保持
# 期初在边界前时的旧口径；这与 producer 的 period_start_ts 判定一致，避免
# 周报键已过边界、但其七日事实窗仍从边界前开始时产生假冲突。
TOTAL_REALIZED_PNL_REQUIRED_FROM = "2026-08-20"
# 2026-08-19 P0-3 激活边界：自该周报键起，「周窗必须被 7 份日报铺满」升为
# error（W10 实测 7 窗只有 4 份日报，差额 -127.05 USDT 无人发现）。
# 边界留 5 天给 2026-08-14/08-15/08-17 三份日报补跑。
WEEKLY_DAILY_COVERAGE_REQUIRED_FROM = "2026-08-24"
MISSED_EVIDENCE_STATUSES = {
    "COMPLETE", "SOURCE_LAG", "NO_DATA", "ERROR",
}
MISSED_EVIDENCE_HASH_KEYS = {
    "contract_sha256",
    "schema_sha256",
    "snapshot_input_sha256",
    "trade_exclusion_sha256",
    "outcome_input_sha256",
    "expected_keys_sha256",
    "observed_results_sha256",
}


def _missing_daily_windows(
    account_db: Path, expected_start: str, expected_end: str
) -> list[str] | None:
    """返回周窗内缺失的日报窗（按日报 ts 的日期部分标识）；无表返回 None。

    日报窗为 [D-1 08:00, D 08:00)，报告 ts 的日期即窗尾日期；历史 ts 形态混杂
    （'2026-05-17' / '2026-05-31T00:00:00Z' / '2026-08-04 08:08:04'），故只按
    substr(ts,1,10) 匹配日期 —— 用 ts=? 精确匹配会把所有周报误判成缺失。
    """
    start = trade_report_stats.parse_cst(expected_start)
    end = trade_report_stats.parse_cst(expected_end)
    wanted: list[str] = []
    cursor = start + timedelta(days=1)
    while cursor <= end:
        wanted.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    con = _open_ro(account_db)
    try:
        if not con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='daily_reports'").fetchone():
            # 库里根本没有 daily_reports（旧库/隔离测试夹具）→ 无法判定覆盖，
            # 返回 None 让调用方标 unknown，而不是把所有日窗都报成缺失。
            return None
        present = {
            str(r[0]) for r in con.execute(
                "SELECT DISTINCT substr(ts,1,10) FROM daily_reports "
                "WHERE profile='live'")
        }
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return [day for day in wanted if day not in present]


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


def _validate_missed_opportunity_contract(
    *,
    period_start: str,
    period_end: str,
    embedded_metrics: object,
    lessons_db: Path,
    live_trades_db: Path,
    market_db: Path,
    briefing_dir: Path,
) -> dict:
    """Independently rebuild and compare the forward evidence contract.

    The legacy naked COUNT path remains untouched before the preregistered
    report-period-end boundary.  After activation, absence or drift of the
    embedded contract is an artifact error; a correctly represented
    SOURCE_LAG/NO_DATA/ERROR contract remains a valid draft but is never
    release eligible.
    """
    active = bool(
        thresholds.missed_opportunity_evidence_contract_active(period_end))
    result = {
        "active": active,
        "status": None,
        "artifact_errors": [],
        "release_blockers": [],
        "checks": [],
        "contract": None,
    }
    if not active:
        return result

    metrics = embedded_metrics if isinstance(embedded_metrics, dict) else {}
    stored = metrics.get("evidence_contract")
    if not isinstance(stored, dict):
        result["status"] = "ERROR"
        result["artifact_errors"].append(
            "missed_evidence: active report missing evidence_contract")
        return result
    try:
        rebuilt = trade_report_stats.missed_opportunity_evidence_contract(
            report_start_ts=period_start,
            report_end_ts=period_end,
            lessons_db=Path(lessons_db),
            live_trades_db=Path(live_trades_db),
            market_db=Path(market_db),
            briefing_dir=Path(briefing_dir),
            contract_activation_cst=(
                thresholds.MISSED_OPPORTUNITY_EVIDENCE_ACTIVATION_CST),
        )
    except Exception as exc:  # fail-closed validator boundary
        result["status"] = "ERROR"
        result["artifact_errors"].append(
            "missed_evidence: independent rebuild failed: "
            f"{type(exc).__name__}: {exc}")
        return result
    result["contract"] = rebuilt
    if not isinstance(rebuilt, dict):
        result["status"] = "ERROR"
        result["artifact_errors"].append(
            "missed_evidence: rebuilt contract is not an object")
        return result

    status = str(rebuilt.get("status") or "ERROR")
    result["status"] = status
    required_top = {
        "schema_version", "artifact_type", "contract_activation_cst",
        "contract_active", "status", "release_eligible", "count",
        "report_window", "candidate_window", "source_coverage",
        "outcome_coverage", "hashes", "self_sha256",
    }
    if not required_top.issubset(rebuilt):
        result["artifact_errors"].append(
            "missed_evidence: rebuilt contract fields missing")
    if status not in MISSED_EVIDENCE_STATUSES:
        result["artifact_errors"].append(
            f"missed_evidence: invalid status {status!r}")
    hashes = rebuilt.get("hashes")
    if not isinstance(hashes, dict) or not MISSED_EVIDENCE_HASH_KEYS.issubset(
            hashes):
        result["artifact_errors"].append(
            "missed_evidence: rebuilt contract hashes incomplete")
    if stored != rebuilt:
        result["artifact_errors"].append(
            "missed_evidence: embedded contract differs from independent rebuild")

    report_window = rebuilt.get("report_window") or {}
    candidate_window = rebuilt.get("candidate_window") or {}
    if report_window != {
        "start_ts": period_start,
        "end_ts": period_end,
        "end_exclusive": True,
    }:
        result["artifact_errors"].append(
            "missed_evidence: report window differs")
    if any((
        metrics.get("candidate_window_start_ts")
        != candidate_window.get("start_ts"),
        metrics.get("candidate_window_end_ts")
        != candidate_window.get("end_ts"),
        metrics.get("candidate_window_end_exclusive")
        is not candidate_window.get("end_exclusive"),
        metrics.get("outcome_horizon_hours")
        != candidate_window.get("outcome_horizon_hours"),
        metrics.get("required_15m_bars")
        != candidate_window.get("required_15m_bars"),
    )):
        result["artifact_errors"].append(
            "missed_evidence: outer window differs from evidence contract")

    count = rebuilt.get("count")
    release_eligible = rebuilt.get("release_eligible") is True
    if status == "COMPLETE":
        if (not isinstance(count, int) or isinstance(count, bool)
                or not release_eligible):
            result["artifact_errors"].append(
                "missed_evidence: COMPLETE requires integer count and release eligibility")
        if metrics.get("count") != count:
            result["artifact_errors"].append(
                "missed_evidence: outer count differs from COMPLETE contract")
    else:
        if count is not None or release_eligible:
            result["artifact_errors"].append(
                f"missed_evidence: {status} must use count=null and release_eligible=false")
        if metrics.get("count") is not None:
            result["artifact_errors"].append(
                "missed_evidence: non-COMPLETE outer count must be null")
        result["release_blockers"].append(
            f"release: missed-opportunity evidence status {status}")
    if not result["artifact_errors"]:
        result["checks"].append("missed_opportunity_evidence_contract")
    return result


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


def _expected_missed_candidate_window(
    report_start: str,
    report_end: str,
) -> tuple[str, str]:
    shift = timedelta(hours=MISSED_OPPORTUNITY_OUTCOME_HOURS)
    start = trade_report_stats.parse_cst(report_start) - shift
    end = trade_report_stats.parse_cst(report_end) - shift
    return (
        start.strftime(trade_report_stats.TS_FMT),
        end.strftime(trade_report_stats.TS_FMT),
    )


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
    market_db: Path | None = None,
    briefing_dir: Path | None = None,
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
    root = Path(account_db).parent
    lessons_db = Path(lessons_db) if lessons_db is not None else root / "lessons.db"
    market_db = Path(market_db) if market_db is not None else root / "market.db"
    briefing_dir = (
        Path(briefing_dir)
        if briefing_dir is not None
        else root.parent / "logs" / "briefing"
    )
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
            "artifact_valid": False,
            "send_allowed": False,
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

    matured_match = re.search(
        r"已完整成熟4小时的错失机会记录[： :]\s*(\d+)\s*条"
        r"（候选窗口 \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\s*"
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\)",
        content,
    )
    legacy_match = re.search(
        r"本窗口错失机会记录[： :]\s*(\d+)\s*条", content)
    embedded_missed = audit.get("missed_opportunity_metrics") or {}
    evidence = _validate_missed_opportunity_contract(
        period_start=expected_start,
        period_end=expected_end,
        embedded_metrics=embedded_missed,
        lessons_db=lessons_db,
        live_trades_db=live_trades_db,
        market_db=market_db,
        briefing_dir=briefing_dir,
    )
    evidence_status = evidence["status"]
    release_blockers = list(evidence["release_blockers"])
    errors.extend(evidence["artifact_errors"])
    checks.extend(evidence["checks"])
    if evidence["active"]:
        contract = evidence.get("contract") or {}
        candidate = contract.get("candidate_window") or {}
        machine = re.search(
            r"(?m)^> missed_opportunity_evidence_contract: "
            r"status=(COMPLETE|SOURCE_LAG|NO_DATA|ERROR) "
            r"release_eligible=(true|false) count=(\d+|N/A) "
            r"candidate_window=\[([^,]+),([^\)]+)\) "
            r"self_sha256=([0-9a-fA-F]{64})$",
            content,
        )
        if machine is None:
            errors.append(
                "missed_evidence: machine-readable contract line missing")
        else:
            shown_count = (
                int(machine.group(3))
                if machine.group(3) != "N/A" else None)
            if any((
                machine.group(1) != evidence_status,
                (machine.group(2) == "true")
                is not (contract.get("release_eligible") is True),
                shown_count != contract.get("count"),
                machine.group(4) != candidate.get("start_ts"),
                machine.group(5) != candidate.get("end_ts"),
                machine.group(6).lower()
                != str(contract.get("self_sha256") or "").lower(),
            )):
                errors.append(
                    "missed_evidence: markdown contract line differs")
        if evidence_status == "COMPLETE":
            if not matured_match:
                errors.append(
                    "facts: COMPLETE evidence requires mature missed-opportunity count")
            else:
                if (matured_match.group(2), matured_match.group(3)) != (
                        candidate.get("start_ts"), candidate.get("end_ts")):
                    errors.append(
                        "facts: mature missed-opportunity window differs from contract")
                if int(matured_match.group(1)) != contract.get("count"):
                    errors.append(
                        "facts: markdown missed-opportunity count differs from contract")
            if "草稿" in content:
                errors.append("release: COMPLETE report must not be labelled draft")
        else:
            if matured_match or legacy_match:
                errors.append(
                    "facts: non-COMPLETE draft must not publish a naked count")
            if "草稿" not in content:
                errors.append(
                    f"release: {evidence_status or 'ERROR'} report lacks draft label")
    else:
        # Preserve the historical naked-COUNT semantics exactly before the
        # preregistered report-period-end activation boundary.
        if matured_match:
            missed_start, missed_end = _expected_missed_candidate_window(
                expected_start, expected_end)
            if (matured_match.group(2), matured_match.group(3)) != (
                    missed_start, missed_end):
                errors.append("facts: mature missed-opportunity window differs")
            missed_match = matured_match
        else:
            missed_start, missed_end = expected_start, expected_end
            missed_match = legacy_match
        missed = _missed_count(lessons_db, missed_start, missed_end)
        if missed is not None:
            if not missed_match:
                errors.append("facts: deterministic missed-opportunity count missing")
            elif int(missed_match.group(1)) != missed:
                errors.append(
                    "facts: missed-opportunity count "
                    f"{missed_match.group(1)}!={missed}")
        if matured_match:
            shown_missed = int(matured_match.group(1))
            stored_missed = embedded_missed.get("count")
            recovered_missed_count = (
                stored_missed is None
                and missed is not None
                and shown_missed == missed
            )
            if not all((
                embedded_missed.get("candidate_window_start_ts") == missed_start,
                embedded_missed.get("candidate_window_end_ts") == missed_end,
                embedded_missed.get("candidate_window_end_exclusive") is True,
                embedded_missed.get("outcome_horizon_hours")
                == MISSED_OPPORTUNITY_OUTCOME_HOURS,
                embedded_missed.get("required_15m_bars") == 16,
                stored_missed == shown_missed or recovered_missed_count,
            )):
                errors.append(
                    "facts: embedded mature missed-opportunity metrics differ")
            elif recovered_missed_count:
                checks.append(
                    "missed_opportunity_count_recovered_from_authoritative_ledger")

    # P0-1：边界后头条口径 = close + reduce；边界前保持仅 close。
    _pnl_key = ("total_realized_pnl"
                if str(expected_start) >= TOTAL_REALIZED_PNL_REQUIRED_FROM
                else "realized_pnl")
    if kind == "weekly":
        # P0-3：周报窗必须被 7 份日报铺满，否则日/周口径无法互相对账
        # （W10 实测 7 窗只有 4 份，差额 -127.05 USDT 无人发现）。
        missing_days = _missing_daily_windows(
            account_db, expected_start, expected_end)
        if missing_days is None:
            checks.append("weekly_daily_coverage_unknown")
        elif missing_days:
            message = ("coverage: 周窗未被日报铺满，缺 "
                       + ",".join(missing_days))
            if str(key) >= WEEKLY_DAILY_COVERAGE_REQUIRED_FROM:
                errors.append(message)
            else:
                checks.append("weekly_daily_coverage_legacy_gap")
        else:
            checks.append("weekly_daily_coverage")
        if not all((
            row["open_count"] == facts["open_count"],
            row["close_count"] == facts["close_count"],
            _same(row["total_pnl"], facts[_pnl_key]),
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
            _same(row["total_pnl"], facts[_pnl_key]),
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

    artifact_valid = not errors
    send_allowed = artifact_valid and not release_blockers
    if artifact_valid:
        checks.extend(["structure", "report_audit", "authoritative_facts"])
    return {
        "ok": artifact_valid and send_allowed,
        "artifact_valid": artifact_valid,
        "send_allowed": send_allowed,
        "evidence_status": evidence_status,
        "kind": kind,
        "report_key": key,
        "period_start_ts": expected_start,
        "period_end_ts": expected_end,
        "errors": errors + release_blockers,
        "release_blockers": release_blockers,
        "checks": sorted(set(checks)),
        "auto_send": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="read-only weekly/monthly report pre-send validator")
    parser.add_argument("--kind", choices=("weekly", "monthly"), required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--db-root", default=_public_project_path('db'))
    parser.add_argument("--account-db")
    parser.add_argument("--live-trades-db")
    parser.add_argument("--ledger-db")
    parser.add_argument("--lessons-db")
    parser.add_argument("--market-db")
    parser.add_argument("--briefing-dir")
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
        "market_db": Path(args.market_db) if args.market_db else root / "market.db",
        "briefing_dir": (
            Path(args.briefing_dir)
            if args.briefing_dir else root.parent / "logs" / "briefing"),
    }
    missing = [
        str(path) for name, path in paths.items()
        if name not in {"briefing_dir", "market_db"} and not path.exists()
    ]
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
