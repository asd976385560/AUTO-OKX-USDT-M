# -*- coding: utf-8 -*-
"""Read-only validator for reviewer daily Markdown before external delivery.

Checks the report structure, reconciliation state, risk-reject counts, embedded
``raw.report_audit`` contract, revision/resend-review state, and authoritative
trade/intent facts for the report-time window.  It never writes repair_queue,
changes a report, or sends a message.
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


REQUIRED_MARKERS = (
    "# 📊 小灵日报 ",
    "## 💰 资产",
    "## 📈 持仓",
    "## 🎯 交易",
    "### 🟢 实盘",
    # "### 🟡 模拟盘" 随 2026-08-06 demo 全量下线移除
    "## ⚠️ 异常 / 🛠 自修",
    "## 🌍 市场",
    "## 🧠 教训",
    "## 详细 summary",
)
# "demo" 项**刻意保留**：demo 下线前生成的 54 份历史日报仍带 "### 🟡 模拟盘"
# 段，重新校验旧报告时要按标签定位截断（见 _section 的截断注释）。它只是个
# 解析用的标签表，不代表 demo 还在跑。
PROFILE_LABELS = {"live": "🟢 实盘", "demo": "🟡 模拟盘"}

# 2026-08-13 规格书四段（市场总览/全市场扫描/数据完善率/次日关注）——
# 预注册激活边界起才要求，历史归档不反向加责（对齐 push 三周期段先例）。
SPEC_SECTIONS_ACTIVATION_TS = "2026-08-14 00:00:00"
# 退出质量段：预注册激活边界起的日报必须带；历史归档不反向加责。
EXIT_QUALITY_ACTIVATION_TS = "2026-08-16 08:00:00"
# 与 exit_quality/错失开仓池同款后验窗长度，独立声明防止单边改动。
EXIT_QUALITY_OUTCOME_HOURS = 4
SPEC_SECTION_MARKERS = (
    "## 🛰 全市场扫描",
    "## 📡 数据完善率",
    "### 市场总览（writer 权威回读）",
    "## 🔭 次日关注",
)
_FOCUS_PLACEHOLDER = "未填写"
FINAL_RECONCILE = {"clean", "ok", "cleared", "final"}
MISSED_OPPORTUNITY_OUTCOME_HOURS = 4


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


def _section_body(content: str, marker: str) -> str | None:
    """返回 marker 段正文（到下一个同级 '## ' 或 '---' 为止）；缺段返回 None。"""
    if marker not in content:
        return None
    tail = content.split(marker, 1)[1]
    for stop in ("\n## ", "\n---"):
        idx = tail.find(stop)
        if idx != -1:
            tail = tail[:idx]
    return tail


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


def _expected_missed_candidate_window(
    report_start: str,
    report_end: str,
) -> tuple[str, str]:
    """Independently derive the continuous window with mature 4H outcomes."""
    shift = timedelta(hours=MISSED_OPPORTUNITY_OUTCOME_HOURS)
    start = trade_report_stats.parse_cst(report_start) - shift
    end = trade_report_stats.parse_cst(report_end) - shift
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _expected_exit_quality_window(
    report_start: str,
    report_end: str,
) -> tuple[str, str]:
    """独立推导退出质量候选窗（与错失开仓池同款后验窗前移）。"""
    shift = timedelta(hours=EXIT_QUALITY_OUTCOME_HOURS)
    start = trade_report_stats.parse_cst(report_start) - shift
    end = trade_report_stats.parse_cst(report_end) - shift
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _independent_exit_quality_counts(
    account_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, int] | None:
    """独立重算「已成熟平仓」与「错失止盈池」两个可对账数字。"""
    if not Path(account_db).exists():
        return None
    con = _open_readonly(Path(account_db))
    try:
        rows = con.execute(
            "SELECT mfe_r,realized_r_net,path_coverage FROM trade_experiences "
            "WHERE status='closed' AND closed_at IS NOT NULL "
            "AND datetime(closed_at)>=datetime(?) "
            "AND datetime(closed_at)<datetime(?)",
            (candidate_start, candidate_end),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    closed = len(rows)
    pool = 0
    for row in rows:
        peak, realized, coverage = row[0], row[1], row[2]
        if peak is None or realized is None or coverage is None:
            continue
        try:
            ratio = float(str(coverage).rsplit(":", 1)[-1])
        except ValueError:
            continue
        if ratio < 0.9:
            continue
        if float(peak) >= 1.0 and float(realized) < 1.0:
            pool += 1
    return {"closed_rows": closed, "missed_take_profit_pool_size": pool}



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
    # 历史日报仍可能带 "### 🟡 模拟盘" 段（demo 下线前生成）；截断保证旧报告
    # 的 live 段解析结果不被后面的 demo 数字污染。
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

    # 2026-08-13 规格书四段：激活边界起硬性要求；历史归档不反向加责。
    if report_ts >= SPEC_SECTIONS_ACTIVATION_TS:
        spec_errors = 0
        for marker in SPEC_SECTION_MARKERS:
            if marker not in content:
                errors.append(f"spec-sections: missing marker {marker}")
                spec_errors += 1
        focus_body = _section_body(content, "## 🔭 次日关注")
        if focus_body is not None:
            focus_clean = focus_body.strip()
            if not focus_clean or _FOCUS_PLACEHOLDER in focus_clean:
                errors.append(
                    "spec-sections: focus_next_day must be filled by reviewer")
                spec_errors += 1
        completeness_body = _section_body(content, "## 📡 数据完善率")
        if (completeness_body is not None
                and "数据完善率不可用" in completeness_body
                and ledger_db.exists()):
            errors.append(
                "spec-sections: completeness block unavailable while "
                "ledger.db exists")
            spec_errors += 1
        if not spec_errors:
            checks.append("spec_sections_v1")
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

    # 2026-08-06 demo 全量下线：日报只剩 live 一段，双盘断言全部降为单盘。
    markdown_metrics = {
        profile: _parse_profile_metrics(
            _trading_profile_block(content, profile))
        for profile in ("live",)
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
    # 2026-08-06 demo 全量下线：只要求 live 行存在，多余 profile 一律忽略。
    # 刻意不写成 `set(by_profile) == {"live"}`——那在过渡期是**收紧**：demo 行清除
    # 之前生成的历史日报（54 份）都会当场校验失败。放宽后新旧两种形态都通过。
    if "live" not in by_profile:
        errors.append("database: report ts must have a live row")
        return {"ok": False, "errors": errors, "checks": checks}
    by_profile = {"live": by_profile["live"]}

    audit_by_profile: dict[str, dict] = {}
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

    # live↔demo 的 revision 一致性比对随 demo 下线移除（只剩一盘，无从比对）。

    if len(audit_by_profile) == 1:
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
        # live↔demo 的 report_state 一致性比对同上，随 demo 下线移除。
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
    for profile, trade_db in (("live", live_trades_db),):
        authoritative[profile] = trade_report_stats.profile_statistics(
            profile,
            trade_db,
            ledger_db,
            start_ts,
            end_ts,
            end_exclusive=True,
        )

    for profile in ("live",):
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

    # 2026-08-10 Wave0-2：方向计数与错失机会的独立复核（刻意不复用 writer 的
    # lint 实现）。仅对含新格式确定性事实行的报告启用——历史报告不回溯拒绝。
    live_facts = authoritative["live"]
    sides = live_facts.get("close_side_breakdown") or {}
    side_line = re.search(r"平仓方向:\s*多\s*(\d+)\s*/\s*空\s*(\d+)", content)
    if side_line:
        expect_long = (sides.get("long") or {}).get("close_count")
        expect_short = (sides.get("short") or {}).get("close_count")
        if (int(side_line.group(1)) != expect_long
                or int(side_line.group(2)) != expect_short):
            errors.append(
                "side_counts: markdown 平仓方向与 live_trades.db 不符 "
                f"(markdown 多{side_line.group(1)}/空{side_line.group(2)}, "
                f"账本 多{expect_long}/空{expect_short})")
        else:
            checks.append("side_counts")
    matured_missed_line = re.search(
        r"已完整成熟4小时的错失机会记录:\s*(\d+)\s*条"
        r"（候选窗口 \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\s*"
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\)，UTC\+8",
        content,
    )
    legacy_missed_line = re.search(
        r"本窗口错失机会记录:\s*(\d+)\s*条", content)
    missed_line = matured_missed_line or legacy_missed_line
    if missed_line:
        lessons_db = Path(account_db).parent / "lessons.db"
        actual_missed = None
        if matured_missed_line:
            candidate_start, candidate_end = _expected_missed_candidate_window(
                start_ts, end_ts)
            if (
                matured_missed_line.group(2),
                matured_missed_line.group(3),
            ) != (candidate_start, candidate_end):
                errors.append(
                    "missed_opps: markdown mature candidate window differs")
        else:
            # Historical format deliberately keeps its original full report
            # window semantics; it is not silently reclassified as matured.
            candidate_start, candidate_end = start_ts, end_ts
        if lessons_db.exists():
            lcon = _open_readonly(lessons_db)
            try:
                if lcon.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='missed_opportunities'").fetchone():
                    actual_missed = int(lcon.execute(
                        "SELECT COUNT(*) FROM missed_opportunities "
                        "WHERE ts LIKE '202%' AND datetime(ts)>=datetime(?) "
                        "AND datetime(ts)<datetime(?)",
                        (candidate_start, candidate_end)).fetchone()[0])
            finally:
                lcon.close()
        if actual_missed is not None:
            if int(missed_line.group(1)) != actual_missed:
                errors.append(
                    "missed_opps: markdown 错失机会计数与 lessons.db 不符 "
                    f"(markdown {missed_line.group(1)}, 库 {actual_missed})")
            elif actual_missed > 0 and re.search(
                    r"无错失机会|错失机会\s*[:：]?\s*0\s*(?:条|笔|个)?", content):
                errors.append(
                    "missed_opps: 文字段声称无错失机会，"
                    f"lessons.db 本窗口实有 {actual_missed} 条")
            else:
                checks.append("missed_opps")
        if matured_missed_line:
            embedded_missed = (
                audit_by_profile.get("live", {}).get(
                    "missed_opportunity_metrics") or {}
            )
            expected_embedded = (
                embedded_missed.get("candidate_window_start_ts")
                == candidate_start,
                embedded_missed.get("candidate_window_end_ts")
                == candidate_end,
                embedded_missed.get("candidate_window_end_exclusive") is True,
                embedded_missed.get("outcome_horizon_hours")
                == MISSED_OPPORTUNITY_OUTCOME_HOURS,
                embedded_missed.get("required_15m_bars") == 16,
                embedded_missed.get("count") == int(matured_missed_line.group(1)),
            )
            if not all(expected_embedded):
                errors.append(
                    "missed_opps: embedded mature outcome metrics differ")


    # ---- 退出质量段（激活边界起硬性要求，历史归档不反向加责）----
    exit_quality_header = "## 🚪 退出质量"
    if report_ts >= EXIT_QUALITY_ACTIVATION_TS:
        if exit_quality_header not in content:
            errors.append("exit_quality: missing section marker")
        else:
            checks.append("exit_quality_section")
    if exit_quality_header in content:
        window_line = re.search(
            r"候选窗口 \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\s*"
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\)，UTC\+8；报告窗整体前移 "
            r"(\d+) 小时",
            content,
        )
        matured_line = re.search(
            r"已成熟平仓:\s*(\d+)\s*笔", content)
        pool_line = re.search(
            r"错失止盈池:\s*(\d+)\s*笔", content)
        if not (window_line and matured_line and pool_line):
            errors.append("exit_quality: section is present but unparseable")
        else:
            expected_start, expected_end = _expected_exit_quality_window(
                start_ts, end_ts)
            if (window_line.group(1), window_line.group(2)) != (
                    expected_start, expected_end):
                errors.append(
                    "exit_quality: candidate window differs from the "
                    "independently derived matured window")
            elif int(window_line.group(3)) != EXIT_QUALITY_OUTCOME_HOURS:
                errors.append("exit_quality: outcome horizon differs")
            else:
                actual = _independent_exit_quality_counts(
                    account_db, expected_start, expected_end)
                if actual is None:
                    checks.append("exit_quality_window")
                elif int(matured_line.group(1)) != actual["closed_rows"]:
                    errors.append(
                        "exit_quality: markdown 已成熟平仓数与 account.db 不符 "
                        f"(markdown {matured_line.group(1)}, "
                        f"库 {actual['closed_rows']})")
                elif int(pool_line.group(1)) != actual[
                        "missed_take_profit_pool_size"]:
                    errors.append(
                        "exit_quality: markdown 错失止盈池与 account.db 不符 "
                        f"(markdown {pool_line.group(1)}, "
                        f"库 {actual['missed_take_profit_pool_size']})")
                else:
                    checks.append("exit_quality")

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
        "profiles_checked": len(by_profile),
        "auto_send": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="read-only reviewer daily report validator")
    parser.add_argument("--file", required=True)
    parser.add_argument("--db-root", default=r".\db")
    parser.add_argument("--account-db")
    parser.add_argument("--live-trades-db")
    # `--demo-trades-db` 随 2026-08-06 demo 全量下线删除：函数体早已不读它，
    # 只是签名和 CLI 里还挂着一个指向已删库的路径。
    parser.add_argument("--ledger-db")
    args = parser.parse_args()
    root = Path(args.db_root)
    paths = {
        "report_path": Path(args.file),
        "account_db": Path(args.account_db) if args.account_db else root / "account.db",
        "live_trades_db": (
            Path(args.live_trades_db)
            if args.live_trades_db else root / "live_trades.db"),
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
