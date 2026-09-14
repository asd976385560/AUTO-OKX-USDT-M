# -*- coding: utf-8 -*-
"""Read-only validator for reviewer daily Markdown before external delivery.

Checks the report structure, reconciliation state, risk-reject counts, embedded
``raw.report_audit`` contract, revision/resend-review state, and authoritative
trade/intent facts for the report-time window.  It never writes repair_queue,
changes a report, or sends a message.
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import trade_report_stats
import _acceptance_thresholds as thresholds
from core.decision_card import (
    OPEN_EXECUTION_PACKAGE_KEY,
    is_open_execution_package,
)


CST = timezone(timedelta(hours=8))

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
EXIT_QUALITY_SCHEMA_VERSION = 2
EXIT_QUALITY_METHOD_VERSION = "exit_quality_v2_forward_frozen"
# 2026-08-20：退出质量段的降级契约（与 daily_maintenance.
# PROVISIONAL_ON_FAILURE_STEPS、daily_report_writer.
# EXIT_QUALITY_DEGRADED_STEP 三处同名同义）。该步失败时报告改判
# provisional 并把本段如实留空，validator 必须认这种形态 —— 否则
# maintenance 说「可以发」、writer 也渲染出来了，却卡在校验，等于没改。
EXIT_QUALITY_DEGRADED_STEP = "exit_quality"
REVIEWER_READY_DIR = Path(os.environ.get(
    "OKX_REVIEWER_READY_DIR", _public_project_path('reports', 'quality')))
EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE = "2026-08-15T14:45"
EXIT_QUALITY_MISSED_TP_METHOD = (
    "post_exit_counterfactual_16x15m_v1")
EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_PEAK_ACTIVATION_TS = "2026-08-16 08:00:00"
# 2026-08-19 G1：净 R 口径起用 v2。独立重算方与生产方必须同版本，
# 但消费/校验方一律接受 {v1, v2} —— 边界前归档的 v1 工件不重算不重判。
EXIT_QUALITY_PEAK_METHOD = "peak_giveback_forward_v2"
EXIT_QUALITY_PEAK_METHOD_ACCEPTED = (
    "peak_giveback_forward_v1", "peak_giveback_forward_v2")
EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD = (
    "authoritative_exit_fill_market_16x15m_v1")
EXIT_QUALITY_REQUIRED_15M_BARS = 16
EXIT_QUALITY_BUCKETS = (0.0, 0.25, 0.5, 1.0, 2.0)
# 2026-08-19 P0-2 激活边界（只向前）：自此报告 ts 起，「有平仓必须有手续费事实」
# 为硬性 error。31 份历史零费日报（07-25~08-05 连续 12 份等）不反向加责。
FEES_RECONCILIATION_REQUIRED_FROM = "2026-08-20 08:00:00"
# 2026-08-19 P0-1 激活边界（只向前）：自此报告 ts 起，头条 total_pnl 的口径是
# close + reduce（total_realized_pnl）。边界前的历史报告仍按仅 close 的
# realized_pnl 复核——它们当时就是那么算的，不反向判错。
TOTAL_REALIZED_PNL_REQUIRED_FROM = "2026-08-20 08:00:00"
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
# 2026-08-19 G1 后补激活边界：与 exit_quality.PEAK_GIVEBACK_NET_R_ACTIVATION_CST
# （"2026-08-20T08:00:00+08:00"）同一时刻，换成本文件 report_ts 的
# "YYYY-MM-DD HH:MM:SS" 口径以便直接串比较。
# 为什么需要：path_metrics v2→v3 把 trade_experiences 的
# mfe_r/mae_r/initial_risk_usdt 按**净风险**全量重算，而
# _independent_peak_giveback 是直接读这些列做独立重算 —— 回填后，边界前
# 已发布、当时 valid 的日报会突然对不上（2026-08-19 实证：08-18 由 valid
# 变为 bucket differs 0.5-1R / 1-2R / >=2R）。原常量只挡住 exit_quality.py
# 选方法版本，挡不到这条读列路径。故边界前的报告以**冻结工件**为准
# （那才是当时的事实），独立重算只对边界后的报告做防篡改交叉验证。
PEAK_GIVEBACK_NET_R_ACTIVATION_TS = "2026-08-20 08:00:00"
EXIT_QUALITY_MINIMUM_COVERAGE = 0.9
EXIT_QUALITY_PROFITABLE_PEAK_R = 0.5
EXIT_QUALITY_ONE_R = 1.0
EXIT_QUALITY_MARGIN_THRESHOLD = 0.5
EXIT_QUALITY_DISPOSITIONS = (
    "hold", "close", "reduce", "adjust", "add", "open",
    "attempted_failed", "requested_unconfirmed")
EXIT_QUALITY_ACTION_LAYERS = ("close", "reduce", "adjust", "add", "open")
EXIT_QUALITY_DISPOSITION_PRIORITY = (
    "close", "reduce", "add", "open", "adjust")
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


def _daily_window_continuity(
    previous_end: object,
    current_start: str,
) -> tuple[list[str], list[str], list[str]]:
    """Validate non-overlap without making one missing report contagious.

    Every report independently proves an exact trailing 24-hour window, while
    the completeness audit keeps missing calendar days in its denominator.  A
    whole-day gap therefore remains explicit historical debt but must not make
    every later valid report fail forever.  Overlap and sub-day misalignment
    remain hard errors.
    """
    if previous_end in (None, ""):
        return [], [], []
    try:
        previous = trade_report_stats.parse_cst(str(previous_end))
        current = trade_report_stats.parse_cst(current_start)
    except (TypeError, ValueError):
        return ["window: previous daily report end is invalid"], [], []
    delta = current - previous
    if delta == timedelta(0):
        return [], [], ["daily_window_continuity"]
    if delta < timedelta(0):
        return ["window: overlap with previous daily report"], [], []
    if delta.total_seconds() % timedelta(days=1).total_seconds() != 0:
        return ["window: gap is not aligned to whole report days"], [], []
    gap_days = int(delta / timedelta(days=1))
    return (
        [],
        [
            "window: preserved missing daily report gap "
            f"({gap_days} day{'s' if gap_days != 1 else ''})"
        ],
        ["daily_window_gap_preserved"],
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


def _exit_coverage_ratio(value: object) -> float | None:
    """Independent parser: real producer values include bare full/none."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text == "none":
        return None
    if text == "full":
        return 1.0
    if ":" in text:
        prefix, tail = text.rsplit(":", 1)
        try:
            ratio = float(tail)
        except ValueError:
            return None
        return ratio if (
            (prefix == "full" or prefix.startswith("partial"))
            and math.isfinite(ratio) and 0.0 <= ratio <= 1.0
        ) else None
    try:
        ratio = float(text)
    except ValueError:
        return None
    return ratio if math.isfinite(ratio) and 0.0 <= ratio <= 1.0 else None


def _exit_finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _exit_empty_buckets() -> dict[str, int]:
    labels = [f"<{EXIT_QUALITY_BUCKETS[0]:g}R"]
    labels.extend(
        f"{EXIT_QUALITY_BUCKETS[index - 1]:g}-"
        f"{EXIT_QUALITY_BUCKETS[index]:g}R"
        for index in range(1, len(EXIT_QUALITY_BUCKETS)))
    labels.append(f">={EXIT_QUALITY_BUCKETS[-1]:g}R")
    return {label: 0 for label in labels}


def _exit_bucket_label(giveback: float) -> str:
    for index, edge in enumerate(EXIT_QUALITY_BUCKETS):
        if giveback < edge:
            return (
                f"<{edge:g}R" if index == 0
                else f"{EXIT_QUALITY_BUCKETS[index - 1]:g}-{edge:g}R")
    return f">={EXIT_QUALITY_BUCKETS[-1]:g}R"


def _exit_median(values: list[float]) -> float | None:
    ranked = sorted(values)
    if not ranked:
        return None
    middle = len(ranked) // 2
    if len(ranked) % 2:
        return ranked[middle]
    return round((ranked[middle - 1] + ranked[middle]) / 2, 4)


def _independent_peak_giveback(
    account_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    effective_start = max(
        trade_report_stats.parse_cst(candidate_start).strftime(
            "%Y-%m-%d %H:%M:%S"),
        EXIT_QUALITY_PEAK_ACTIVATION_TS,
    )
    candidate_end = trade_report_stats.parse_cst(candidate_end).strftime(
        "%Y-%m-%d %H:%M:%S")
    con = _open_readonly(account_db)
    try:
        source_counts = con.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN COALESCE(profile,'')!='live' THEN 1 ELSE 0 END) "
            "AS non_live,"
            "SUM(CASE WHEN profile='live' "
            "AND COALESCE(action,'')!='open' THEN 1 ELSE 0 END) "
            "AS non_open FROM trade_experiences WHERE status='closed' "
            "AND closed_at IS NOT NULL AND closed_at>=? AND closed_at<?",
            (candidate_start, candidate_end),
        ).fetchone()
        candidate_count = int(con.execute(
            "SELECT COUNT(*) FROM trade_experiences "
            "WHERE profile='live' AND action='open' "
            "AND status='closed' AND closed_at IS NOT NULL "
            "AND closed_at>=? AND closed_at<?",
            (candidate_start, candidate_end),
        ).fetchone()[0])
        rows = con.execute(
            "SELECT symbol,side,closed_at,mfe_r,mae_r,realized_r_net,"
            "ever_hit_1r,close_at_1r,exit_category,path_coverage "
            "FROM trade_experiences WHERE profile='live' "
            "AND action='open' AND status='closed' "
            "AND closed_at IS NOT NULL AND closed_at>=? AND closed_at<? "
            "ORDER BY closed_at,id",
            (effective_start, candidate_end),
        ).fetchall() if effective_start < candidate_end else []
    finally:
        con.close()
    buckets = _exit_empty_buckets()
    profitable_buckets = _exit_empty_buckets()
    measured: list[float] = []
    profitable: list[float] = []
    retentions: list[float] = []
    cases: list[dict[str, Any]] = []
    unknown = reached = held = disagreements = 0
    for row in rows:
        coverage = _exit_coverage_ratio(row["path_coverage"])
        peak = _exit_finite_float(row["mfe_r"])
        realized = _exit_finite_float(row["realized_r_net"])
        if (peak is None or realized is None or coverage is None
                or coverage < EXIT_QUALITY_MINIMUM_COVERAGE):
            unknown += 1
            continue
        giveback = round(peak - realized, 4)
        measured.append(giveback)
        buckets[_exit_bucket_label(giveback)] += 1
        if peak >= EXIT_QUALITY_PROFITABLE_PEAK_R:
            profitable.append(giveback)
            profitable_buckets[_exit_bucket_label(giveback)] += 1
            retentions.append(round(realized / peak, 4))
        hit = peak >= EXIT_QUALITY_ONE_R
        if hit:
            reached += 1
            if realized >= EXIT_QUALITY_ONE_R:
                held += 1
            else:
                cases.append({
                    "symbol": str(row["symbol"]),
                    "side": str(row["side"]),
                    "closed_at": str(row["closed_at"]),
                    "peak_r": round(peak, 4),
                    "realized_r_net": round(realized, 4),
                    "giveback_r": giveback,
                    "exit_category": (
                        str(row["exit_category"])
                        if row["exit_category"] is not None else None),
                })
        if row["ever_hit_1r"] is not None and bool(row["ever_hit_1r"]) != hit:
            disagreements += 1
    return {
        "method_version": EXIT_QUALITY_PEAK_METHOD,
        "fact_activation_cst": EXIT_QUALITY_PEAK_ACTIVATION_TS,
        "status": "PENDING" if effective_start >= candidate_end else "COMPLETE",
        "effective_window": {
            "start_ts": effective_start,
            "end_ts": candidate_end,
            "end_exclusive": True,
        },
        "source_closed_rows": int(source_counts["total"] or 0),
        "excluded_non_live_rows": int(source_counts["non_live"] or 0),
        "excluded_non_open_rows": int(source_counts["non_open"] or 0),
        "candidate_closed_rows": candidate_count,
        "pre_activation_excluded_rows": candidate_count - len(rows),
        "closed_rows": len(rows),
        "measured_rows": len(measured),
        "unknown_path_rows": unknown,
        "path_coverage_rate": (
            round(len(measured) / len(rows), 6) if rows else None),
        "giveback_buckets_r": buckets,
        "giveback_median_r": _exit_median(measured),
        "giveback_max_r": max(measured) if measured else None,
        "profitable_peak_rows": len(profitable),
        "profitable_peak_threshold_r": EXIT_QUALITY_PROFITABLE_PEAK_R,
        "profitable_peak_giveback_buckets_r": profitable_buckets,
        "profitable_peak_giveback_median_r": _exit_median(profitable),
        "peak_retention_median": _exit_median(retentions),
        "reached_1r": reached,
        "closed_at_or_above_1r": held,
        "profit_giveback_case_count": len(cases),
        "profit_giveback_cases": cases,
        "ever_hit_1r_flag_disagreements": disagreements,
        "semantics": (
            "live closed action=open experiences only; giveback_r = in-position "
            "mfe_r - realized_r_net; full=1.0; none/missing/coverage<0.9 "
            "are unknown and excluded; non-live/non-open rows are audited outside "
            "the denominator"),
    }


def _exit_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        parts: list[str] = []
        for child in value.values():
            parts.extend(_exit_strings(child))
        return parts
    if isinstance(value, list):
        parts = []
        for child in value:
            parts.extend(_exit_strings(child))
        return parts
    return []


def _exit_item_symbol(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    request = item.get("request")
    if isinstance(request, dict):
        nested = _exit_item_symbol(request)
        if nested:
            return nested
    return str(item.get("instId") or item.get("symbol") or
               item.get("instrument") or "").upper()


def _exit_item_action(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    request = item.get("request")
    if isinstance(request, dict):
        nested = _exit_item_action(request)
        if nested:
            return nested
    return str(item.get("action") or item.get("type") or
               item.get("requested_action") or "").strip().lower()


def _exit_result_success(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("ok") is True or item.get("success") is True:
        return True
    if str(item.get("status") or "").lower() in {
            "ok", "success", "succeeded", "completed", "applied", "filled"}:
        return True
    nested = item.get("result")
    if isinstance(nested, dict) and _exit_result_success(nested):
        return True
    return item.get("problem") is None and bool(
        item.get("trade_identities") or item.get("algo_identities"))


def _exit_result_failed(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("ok") is False or item.get("success") is False:
        return True
    if str(item.get("status") or "").strip().lower() in {
            "error", "failed", "failure", "rejected", "blocked", "cancelled",
            "canceled", "timeout"}:
        return True
    if item.get("problem") not in (None, "") or item.get("error") not in (None, ""):
        return True
    nested = item.get("result")
    return isinstance(nested, dict) and _exit_result_failed(nested)


def _exit_action_class(action: str) -> str | None:
    value = str(action or "").lower()
    if value == "hold":
        return "hold"
    if value == "add" or "add_position" in value:
        return "add"
    if value in {"open", "open_long", "open_short"}:
        return "open"
    if "close" in value or value in {"exit", "flatten"}:
        return "close"
    if "reduce" in value or "partial" in value:
        return "reduce"
    if any(token in value for token in (
            "adjust", "protection", "amend", "resize")):
        return "adjust"
    return None


def _exit_mentions(text: str, symbol: str) -> bool:
    upper = text.upper()
    symbol_upper = symbol.upper()
    return bool(symbol_upper and re.search(
        rf"(?<![A-Z0-9]){re.escape(symbol_upper)}(?![A-Z0-9])", upper))


def _exit_item_review_reason(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("reason", "reasoning", "conclusion", "agent_judgement"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    request = item.get("request")
    return _exit_item_review_reason(request) if isinstance(request, dict) else ""


def _exit_structured_position_action_review(
    raw: dict[str, Any], symbol: str,
) -> bool:
    allowed = set(EXIT_QUALITY_ACTION_LAYERS) | {"hold"}
    for key in (
        "requested_position_actions",
        "position_action_results",
        "position_action_failures",
    ):
        for item in raw.get(key) or []:
            if not isinstance(item, dict) or _exit_item_symbol(item) != symbol:
                continue
            action = _exit_action_class(_exit_item_action(item))
            if action in allowed and _exit_item_review_reason(item):
                return True
    return False


def _exit_explicit_review(
    raw: dict[str, Any], symbol: str, *, cycle_id: str,
) -> bool:
    card = raw.get("decision_card")
    candidates = [raw.get("position_reviews")]
    if isinstance(card, dict):
        candidates.append(card.get("position_reviews"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            rows = list(candidate.values())
        elif isinstance(candidate, (list, tuple)):
            rows = candidate
        else:
            continue
        for item in rows:
            if not isinstance(item, dict) or _exit_item_symbol(item) != symbol:
                continue
            action = _exit_action_class(_exit_item_action(item))
            conclusion = str(item.get("conclusion") or
                             item.get("agent_judgement") or
                             item.get("reasoning") or "").strip()
            if action in set(EXIT_QUALITY_ACTION_LAYERS) | {"hold"} and conclusion:
                return True
    judgement = str(card.get("agent_judgement") or "") if isinstance(
        card, dict) else ""
    for clause in re.split(r"[\n；;。，,]+", judgement):
        if _exit_mentions(clause, symbol) and re.search(
            r"(?<![A-Z])(?:HOLD|CLOSE|REDUCE|ADJUST(?:_|\s+)PROTECTION|ADD|OPEN)"
            r"(?![A-Z])", clause.upper(),
        ):
            return True
    if (
        thresholds.structured_position_actions_count_as_review(cycle_id)
        and _exit_structured_position_action_review(raw, symbol)
    ):
        return True
    return False


def _exit_action_layers(
    raw: dict[str, Any], symbol: str, fill_actions: set[str],
) -> dict[str, list[str]]:
    requests = [item for item in (raw.get("requested_position_actions") or [])
                if isinstance(item, dict) and _exit_item_symbol(item) == symbol]
    results = [item for item in (raw.get("position_action_results") or [])
               if isinstance(item, dict) and _exit_item_symbol(item) == symbol]
    failures = [item for item in (raw.get("position_action_failures") or [])
                if isinstance(item, dict) and _exit_item_symbol(item) == symbol]

    def classified(rows: list[dict[str, Any]]) -> list[str]:
        return sorted({action for action in (
            _exit_action_class(_exit_item_action(item)) for item in rows)
            if action in EXIT_QUALITY_ACTION_LAYERS})

    successful = [item for item in results if _exit_result_success(item)]
    failed = list(failures) + [
        item for item in results if _exit_result_failed(item)]
    return {
        "requested": classified(requests),
        "succeeded": classified(successful),
        "fills": sorted({action for action in (
            _exit_action_class(item) for item in fill_actions)
            if action in EXIT_QUALITY_ACTION_LAYERS}),
        "failed": classified(failed),
    }


def _exit_disposition(
    raw: dict[str, Any], symbol: str, fill_actions: set[str]
) -> str:
    layers = _exit_action_layers(raw, symbol, fill_actions)
    fill_dispositions = {
        kind for kind in (
            _exit_action_class(action) for action in fill_actions)
        if kind in EXIT_QUALITY_DISPOSITION_PRIORITY
    }
    for kind in EXIT_QUALITY_DISPOSITION_PRIORITY:
        if kind in fill_dispositions:
            return kind
    succeeded = set(layers["succeeded"])
    for kind in EXIT_QUALITY_DISPOSITION_PRIORITY:
        if kind in succeeded:
            return kind
    if layers["failed"]:
        return "attempted_failed"
    has_unconfirmed = any(
        isinstance(item, dict) and _exit_item_symbol(item) == symbol
        for key in ("requested_position_actions", "position_action_results")
        for item in (raw.get(key) or [])
    )
    if has_unconfirmed:
        return "requested_unconfirmed"
    return "hold"


def _independent_margin_review(
    live_trades_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    start_cycle = trade_report_stats.parse_cst(candidate_start).strftime(
        "%Y-%m-%dT%H:%M")
    end_cycle = trade_report_stats.parse_cst(candidate_end).strftime(
        "%Y-%m-%dT%H:%M")
    effective_start = max(start_cycle, EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE)
    con = _open_readonly(live_trades_db)
    try:
        source_candidate_count = int(con.execute(
            "SELECT COUNT(*) FROM trade_cycles WHERE cycle_id>=? AND cycle_id<?",
            (start_cycle, end_cycle)).fetchone()[0])
        candidate_count = int(con.execute(
            "SELECT COUNT(*) FROM trade_cycles WHERE mode='live' "
            "AND cycle_id>=? AND cycle_id<?",
            (start_cycle, end_cycle)).fetchone()[0])
        cycles = con.execute(
            "SELECT cycle_id,raw FROM trade_cycles WHERE mode='live' "
            "AND cycle_id>=? AND cycle_id<? ORDER BY cycle_id",
            (effective_start, end_cycle),
        ).fetchall() if effective_start < end_cycle else []
        actions: dict[tuple[str, str], set[str]] = {}
        ids = [str(row["cycle_id"]) for row in cycles]
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            fill_rows = con.execute(
                "SELECT cycle_id,symbol,action FROM trades WHERE cycle_id IN ("
                f"{placeholders})", chunk,
            ).fetchall() if chunk else []
            for fill in fill_rows:
                key = (str(fill["cycle_id"]), str(fill["symbol"]).upper())
                actions.setdefault(key, set()).add(
                    str(fill["action"] or "").lower())
    finally:
        con.close()

    dispositions = {key: 0 for key in EXIT_QUALITY_DISPOSITIONS}
    action_layer_counts = {
        layer: {key: 0 for key in EXIT_QUALITY_ACTION_LAYERS}
        for layer in ("requested", "succeeded", "fills", "failed")
    }
    unreadable = unknown_cycles = total = unknown = disagreements = 0
    excluded_non_open_positions = 0
    unflagged = flagged = reviewed = structured_action_reviewed = 0
    structured_semantics_active_flagged = legacy_semantics_flagged = 0
    items: list[dict[str, Any]] = []
    for row in cycles:
        try:
            raw = json.loads(row["raw"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            unreadable += 1
            continue
        if not isinstance(raw, dict):
            unreadable += 1
            continue
        live_facts = raw.get("live_facts")
        positions = live_facts.get("positions") if isinstance(
            live_facts, dict) else None
        if not isinstance(positions, list):
            unknown_cycles += 1
            continue
        cycle_id = str(row["cycle_id"])
        for position in positions:
            if not isinstance(position, dict):
                continue
            symbol = str(position.get("instId") or
                         position.get("symbol") or "").upper()
            if not symbol:
                continue
            contracts = _exit_finite_float(position.get("contracts"))
            if contracts is None or contracts <= 0:
                excluded_non_open_positions += 1
                continue
            total += 1
            present = "margin_return_review_at_or_above_50pct" in position
            upl = _exit_finite_float(position.get("upl_ratio_initial_margin"))
            upl_known = upl is not None
            if not present or not upl_known:
                unknown += 1
                continue
            flag = position.get("margin_return_review_at_or_above_50pct") is True
            recomputed = bool(upl is not None and
                              upl >= EXIT_QUALITY_MARGIN_THRESHOLD)
            if flag != recomputed:
                disagreements += 1
                unknown += 1
                continue
            if not flag:
                unflagged += 1
                continue
            flagged += 1
            structured_semantics_active = (
                thresholds.structured_position_actions_count_as_review(cycle_id))
            if structured_semantics_active:
                structured_semantics_active_flagged += 1
            else:
                legacy_semantics_flagged += 1
            structured_review = bool(
                structured_semantics_active
                and _exit_structured_position_action_review(raw, symbol)
            )
            explicit = _exit_explicit_review(
                raw, symbol, cycle_id=cycle_id)
            if explicit:
                reviewed += 1
            if structured_review:
                structured_action_reviewed += 1
            layers = _exit_action_layers(
                raw, symbol, actions.get((cycle_id, symbol), set()))
            for layer, layer_actions in layers.items():
                for action in layer_actions:
                    action_layer_counts[layer][action] += 1
            disposition = _exit_disposition(
                raw, symbol, actions.get((cycle_id, symbol), set()))
            dispositions[disposition] += 1
            items.append({
                "cycle_id": cycle_id,
                "symbol": symbol,
                "upl_ratio_initial_margin": round(float(upl), 8),
                "explicitly_reviewed": explicit,
                "structured_action_reviewed": structured_review,
                "structured_semantics_active": structured_semantics_active,
                "disposition": disposition,
                "action_layers": layers,
            })
    known = flagged + unflagged
    return {
        "threshold_fraction": EXIT_QUALITY_MARGIN_THRESHOLD,
        "fact_activation_cycle": EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE,
        "candidate_cycle_window": {
            "start_cycle": start_cycle,
            "end_cycle": end_cycle,
            "end_exclusive": True,
        },
        "effective_cycle_window": {
            "start_cycle": effective_start,
            "end_cycle": end_cycle,
            "end_exclusive": True,
        },
        "candidate_cycle_rows": candidate_count,
        "source_candidate_cycle_rows": source_candidate_count,
        "excluded_non_live_cycle_rows": source_candidate_count - candidate_count,
        "eligible_cycle_rows": len(cycles),
        "pre_activation_excluded_cycle_rows": candidate_count - len(cycles),
        "unreadable_cycle_rows": unreadable,
        "unknown_position_list_cycle_rows": unknown_cycles,
        "excluded_non_open_position_rows": excluded_non_open_positions,
        "total_position_cycles": total,
        "fact_observed_position_cycles": known,
        "unknown_fact_position_cycles": unknown,
        "fact_coverage_rate": round(known / total, 6) if total else None,
        "flag_value_disagreements": disagreements,
        "unflagged_position_cycles": unflagged,
        "flagged_position_cycles": flagged,
        "explicitly_reviewed": reviewed,
        "structured_action_reviewed": structured_action_reviewed,
        "structured_semantics_active_flagged": (
            structured_semantics_active_flagged),
        "legacy_semantics_flagged": legacy_semantics_flagged,
        "explicit_review_rate": round(reviewed / flagged, 6) if flagged else None,
        "explicit_review_semantics_migration": (
            thresholds.structured_position_review_migration_facts(end_cycle)),
        "disposition_counts": dispositions,
        "action_layer_counts": action_layer_counts,
        "items": items,
        "semantics": (
            "mode=live cycles are selected by cycle_id then joined to trades; only "
            "positive-contract open positions after activation with both a numeric "
            "margin fact and an "
            "agreeing flag enter coverage, and only flagged positions enter "
            "the review denominator; explicit review requires a same-symbol "
            "structured review or an action-bearing agent_judgement clause; "
            "from the registered boundary, a same-full-symbol structured "
            "position action with a recognized action and non-empty reason also "
            "counts, while older cycles retain the prior semantic; "
            "requested/succeeded/fill/failed actions remain separate, requested-only "
            "is requested_unconfirmed rather than failed/HOLD, and unknown is never "
            "treated as below 50%"),
    }


def _exit_ceil_15m(value) -> Any:
    minute = (value.minute // 15) * 15
    floor = value.replace(minute=minute, second=0, microsecond=0)
    return floor if value == floor else floor + timedelta(minutes=15)


def _v_parse_utc(value: object) -> datetime:
    text = str(value or "").strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _v_utc_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _v_raw(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _v_ord_ids(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"ordId", "ord_id"} and child not in (None, "", 0):
                found.add(str(child))
            elif key == "ord_ids" and isinstance(child, list):
                found.update(
                    str(item) for item in child if item not in (None, "", 0))
            elif isinstance(child, (dict, list)):
                found.update(_v_ord_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_v_ord_ids(child))
    return found


def _v_snapshot_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _v_original_plan(
    row: sqlite3.Row,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    raw = _v_raw(row["raw"])
    if raw is None:
        return None, "blocked", ["trade_experience_raw_unreadable"]
    package = raw.get(OPEN_EXECUTION_PACKAGE_KEY)
    if package is not None:
        if not is_open_execution_package(package):
            return None, "blocked", ["original_open_execution_package_invalid"]
        rr = {
            "entry": package["entry"],
            "stop": package["stop"],
            "target": package["target"],
            "exit_mode": package["exit_mode"],
        }
    else:
        card = raw.get("decision_card")
        rr = card.get("risk_reward") if isinstance(card, dict) else None
    if not isinstance(rr, dict):
        return None, "blocked", ["original_plan_risk_reward_missing"]
    mode = str(rr.get("exit_mode") or "").strip().lower()
    if mode in {"dynamic_exit", "no_fixed_tp"}:
        return {"exit_mode": mode, "target_px": rr.get("target")}, \
            "not_applicable", [f"exit_mode_{mode}"]
    if mode != "fixed_tp":
        return None, "blocked", ["original_plan_exit_mode_invalid_or_missing"]
    try:
        target = float(rr.get("target"))
    except (TypeError, ValueError, OverflowError):
        return None, "blocked", ["original_plan_fixed_tp_target_missing"]
    if not math.isfinite(target):
        return None, "blocked", ["original_plan_fixed_tp_target_not_finite"]
    if target <= 0:
        return None, "blocked", ["original_plan_fixed_tp_target_missing"]
    if str(raw.get("symbol") or raw.get("instId") or "").upper() != str(
            row["symbol"]).upper():
        return None, "blocked", ["original_plan_symbol_identity_mismatch"]
    if str(raw.get("side") or raw.get("pos_side") or "").lower() != str(
            row["side"]).lower():
        return None, "blocked", ["original_plan_side_identity_mismatch"]
    return {"exit_mode": mode, "target_px": target}, "eligible", []


def _v_exit_fill(
    row: sqlite3.Row,
    live: sqlite3.Connection,
    trade_cache: dict[tuple[str, str], list[sqlite3.Row]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    raw = _v_raw(row["raw"])
    events = raw.get("close_events") if isinstance(raw, dict) else None
    if not isinstance(events, list) or not events:
        return None, ["authoritative_close_event_missing"]
    closed_at = trade_report_stats.parse_cst(row["closed_at"]).strftime(
        "%Y-%m-%d %H:%M:%S")
    event = events[-1]
    if not isinstance(event, dict):
        return None, ["final_close_event_invalid"]
    try:
        ts = trade_report_stats.parse_cst(event.get("ts")).strftime(
            "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None, ["final_close_event_ts_invalid"]
    if ts != closed_at:
        return None, ["final_close_event_closed_at_mismatch"]
    cycle_id = str(event.get("cycle_id") or "").strip()
    # 2026-09-12：与 exit_quality 同口径——多单聚合平仓的权威身份是 ordId 集合。
    ord_identity = {str(event.get("ordId") or "").strip()} if str(
        event.get("ordId") or "").strip() else {
        str(x) for x in (event.get("ord_ids") or []) if x not in (None, "", 0)}
    if not ord_identity:
        return None, ["final_close_event_ord_id_missing"]
    ord_id = ",".join(sorted(ord_identity))
    try:
        event_px = float(event.get("fill_px"))
    except (TypeError, ValueError, OverflowError):
        return None, ["final_close_event_fill_px_missing"]
    if not math.isfinite(event_px):
        return None, ["final_close_event_fill_px_not_finite"]
    event_sz = _exit_finite_float(event.get("sz"))
    if not cycle_id or event_px <= 0 or event_sz is None or event_sz <= 0:
        return None, ["final_close_event_identity_incomplete"]
    cache_key = (cycle_id, str(row["symbol"]))
    trade_rows = trade_cache.get(cache_key) if trade_cache is not None else None
    if trade_rows is None:
        trade_rows = live.execute(
            "SELECT id,cycle_id,ts,symbol,action,side,sz,fill_px,raw FROM trades "
            "WHERE cycle_id=? AND symbol=? ORDER BY id",
            cache_key,
        ).fetchall()
        if trade_cache is not None:
            trade_cache[cache_key] = trade_rows
    matches = []
    for trade in trade_rows:
        action = str(trade["action"] or "").lower()
        if not any(token in action for token in (
                "close", "reduce", "stop", "take_profit", "tp")):
            continue
        if str(trade["side"] or "").lower() != str(row["side"]).lower():
            continue
        raw_trade = _v_raw(trade["raw"]) or {}
        authoritative_ts = trade["ts"]
        if (
            raw_trade.get("reconcile_source") in {
                "exchange_fills_reconcile", "execution_journal_recovery"}
            and raw_trade.get("ts_source") == "trusted_internal_override"
            and ord_identity <= _v_ord_ids(raw_trade)
            and raw_trade.get("close_ts")
        ):
            authoritative_ts = raw_trade["close_ts"]
        try:
            ts = trade_report_stats.parse_cst(authoritative_ts).strftime(
                "%Y-%m-%d %H:%M:%S")
            px = float(trade["fill_px"])
            trade_sz = float(trade["sz"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(px) or not math.isfinite(trade_sz) or px <= 0 or trade_sz <= 0:
            continue
        if ts != closed_at or abs(px - event_px) > max(1e-10, abs(event_px) * 1e-10):
            continue
        if not ord_identity <= _v_ord_ids(raw_trade):
            continue
        matches.append(trade)
    if len(matches) != 1:
        return None, ["authoritative_live_trade_not_unique_or_missing"]
    trade = matches[0]
    ids = sorted(_v_ord_ids(_v_raw(trade["raw"]) or {}))
    return {
        "trade_row_id": int(trade["id"]),
        "cycle_id": str(trade["cycle_id"]),
        "ord_id": ord_id,
        "ts": closed_at,
        "px": float(trade["fill_px"]),
        "trade_fill_sz": float(trade["sz"]),
        "experience_consumed_sz": float(event_sz),
        "action": str(trade["action"]).lower(),
    }, []


def _v_bars(
    row: sqlite3.Row,
    fill: dict[str, Any],
    market: sqlite3.Connection,
    bar_cache: dict[tuple[str, str, str], list[sqlite3.Row]] | None = None,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    exit_ts = trade_report_stats.parse_cst(fill["ts"])
    first = _exit_ceil_15m(exit_ts).astimezone(timezone.utc)
    end = first + timedelta(minutes=15 * EXIT_QUALITY_REQUIRED_15M_BARS)
    cache_key = (str(row["symbol"]), _v_utc_z(first), _v_utc_z(end))
    rows = bar_cache.get(cache_key) if bar_cache is not None else None
    if rows is None:
        rows = market.execute(
            "SELECT ts,o,h,l,c FROM kline_cache WHERE symbol=? AND tf='15m' "
            "AND ts>=? AND ts<? ORDER BY ts",
            cache_key,
        ).fetchall()
        if bar_cache is not None:
            bar_cache[cache_key] = rows
    if len(rows) != EXIT_QUALITY_REQUIRED_15M_BARS:
        return None, ["post_exit_bar_count_not_exactly_16"]
    bars = []
    for index, bar in enumerate(rows):
        try:
            ts = _v_parse_utc(bar["ts"])
            numbers = {key: float(bar[key]) for key in ("o", "h", "l", "c")}
        except (TypeError, ValueError, OverflowError):
            return None, ["post_exit_bar_invalid"]
        if not all(math.isfinite(number) for number in numbers.values()):
            return None, ["post_exit_bar_non_finite"]
        value = {"ts": _v_utc_z(ts), **numbers}
        if ts != first + timedelta(minutes=15 * index):
            return None, ["post_exit_bars_not_exact_contiguous_window"]
        if not (
            value["o"] > 0 and value["h"] > 0 and value["l"] > 0
            and value["c"] > 0
            and value["h"] >= max(value["o"], value["l"], value["c"])
            and value["l"] <= min(value["o"], value["h"], value["c"])
        ):
            return None, ["post_exit_bar_invalid"]
        bars.append(value)
    return bars, []


def _v_snapshot(
    row: sqlite3.Row,
    live: sqlite3.Connection,
    market: sqlite3.Connection,
    trade_cache: dict[tuple[str, str], list[sqlite3.Row]] | None = None,
    bar_cache: dict[tuple[str, str, str], list[sqlite3.Row]] | None = None,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    plan, state, reasons = _v_original_plan(row)
    if state != "eligible":
        return None, state, reasons
    fill, reasons = _v_exit_fill(row, live, trade_cache)
    if fill is None:
        return None, "blocked", reasons
    bars, reasons = _v_bars(row, fill, market, bar_cache)
    if bars is None:
        return None, "blocked", reasons
    return {
        "method_version": EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD,
        "experience_identity": {
            "experience_id": int(row["id"]),
            "symbol": str(row["symbol"]),
            "side": str(row["side"]).lower(),
            "closed_at": trade_report_stats.parse_cst(row["closed_at"]).strftime(
                "%Y-%m-%d %H:%M:%S"),
        },
        "original_plan": plan,
        "exit_fill": fill,
        "bars_15m": bars,
    }, "eligible", []


def _independent_missed_take_profit_from_connections(
    con: sqlite3.Connection,
    live: sqlite3.Connection,
    market: sqlite3.Connection,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    columns = {str(row[1]) for row in con.execute(
        "PRAGMA table_info(trade_experiences)").fetchall()}
    raw_expr = "raw" if "raw" in columns else "NULL AS raw"
    source_counts = con.execute(
        "SELECT COUNT(*) AS total,"
        "SUM(CASE WHEN COALESCE(profile,'')!='live' THEN 1 ELSE 0 END) "
        "AS non_live,"
        "SUM(CASE WHEN profile='live' "
        "AND COALESCE(action,'')!='open' THEN 1 ELSE 0 END) "
        "AS fallback FROM trade_experiences WHERE status='closed' "
        "AND closed_at IS NOT NULL AND closed_at>=? AND closed_at<?",
        (candidate_start, candidate_end),
    ).fetchone()
    rows = con.execute(
        "SELECT id,profile,symbol,side,closed_at," + raw_expr + " "
        "FROM trade_experiences WHERE profile='live' "
        "AND action='open' AND status='closed' "
        "AND closed_at IS NOT NULL AND closed_at>=? AND closed_at<? "
        "ORDER BY closed_at,id",
        (candidate_start, candidate_end),
    ).fetchall()
    pool: list[dict[str, Any]] = []
    evaluated_items: list[dict[str, Any]] = []
    blocking_items: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    pre_activation = not_applicable = fixed_tp_candidates = 0
    trade_cache: dict[tuple[str, str], list[sqlite3.Row]] = {}
    bar_cache: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        if trade_report_stats.parse_cst(row["closed_at"]) < \
                trade_report_stats.parse_cst(
                    EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS):
            pre_activation += 1
            continue
        snapshot, state, reasons = _v_snapshot(
            row, live, market, trade_cache, bar_cache)
        if state == "not_applicable":
            not_applicable += 1
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            continue
        fixed_tp_candidates += 1
        if snapshot is None:
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            blocking_items.append({
                "experience_id": int(row["id"]), "symbol": str(row["symbol"]),
                "side": str(row["side"]), "closed_at": str(row["closed_at"]),
                "reasons": sorted(set(reasons)),
            })
            continue
        side = str(row["side"] or "").lower()
        plan = snapshot["original_plan"]
        fill = snapshot["exit_fill"]
        target = float(plan["target_px"])
        exit_px = float(fill["px"])
        bars = snapshot["bars_15m"]
        if side == "long":
            before = exit_px < target
            reached = any(float(bar["h"]) >= target for bar in bars)
        elif side == "short":
            before = exit_px > target
            reached = any(float(bar["l"]) <= target for bar in bars)
        else:
            reason_counts["side_invalid"] = reason_counts.get("side_invalid", 0) + 1
            blocking_items.append({
                "experience_id": int(row["id"]), "symbol": str(row["symbol"]),
                "side": side, "closed_at": str(row["closed_at"]),
                "reasons": ["side_invalid"],
            })
            continue
        missed = before and reached
        item = {
            "experience_id": int(row["id"]),
            "symbol": str(row["symbol"]),
            "side": side,
            "closed_at": str(row["closed_at"]),
            "exit_mode": plan["exit_mode"],
            "target_px": target,
            "exit_px": exit_px,
            "post_exit_target_reached": reached,
            "classification": "missed_take_profit" if missed else "not_missed",
            "source_snapshot_sha256": _v_snapshot_hash(snapshot),
            "source_snapshot": snapshot,
        }
        evaluated_items.append(item)
        if missed:
            pool.append(item)
    evaluated = len(evaluated_items)
    post_activation = len(rows) - pre_activation
    unknown = len(blocking_items)
    upstream_status = "BLOCKED" if blocking_items else "READY"
    status = (
        "BLOCKED" if blocking_items else
        "NOT_ACTIVATED_WINDOW" if post_activation == 0 else
        "NOT_APPLICABLE" if fixed_tp_candidates == 0 else "COMPLETE"
    )
    return {
        "method_version": EXIT_QUALITY_MISSED_TP_METHOD,
        "evidence_method_version": EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD,
        "counterfactual_activation_cst": (
            EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS),
        "upstream_status": upstream_status,
        "status": status,
        "required_15m_bars": EXIT_QUALITY_REQUIRED_15M_BARS,
        "outcome_horizon_hours": EXIT_QUALITY_OUTCOME_HOURS,
        "source_closed_rows": int(source_counts["total"] or 0),
        "excluded_profile_count": int(source_counts["non_live"] or 0),
        "excluded_fallback_count": int(source_counts["fallback"] or 0),
        "candidate_exits": post_activation,
        "pre_activation_excluded_exits": pre_activation,
        "not_applicable_exits": not_applicable,
        "fixed_tp_candidate_exits": fixed_tp_candidates,
        "eligible_exits": evaluated,
        "evaluated_exits": evaluated,
        "unknown_exits": unknown,
        "coverage_rate": (
            round(evaluated / fixed_tp_candidates, 6)
            if fixed_tp_candidates else None),
        "classification_counts": {
            "missed_take_profit": len(pool),
            "not_missed": evaluated - len(pool),
            "not_applicable": not_applicable,
            "unknown": unknown,
            "pre_activation_excluded": pre_activation,
            "excluded_profile": int(source_counts["non_live"] or 0),
            "excluded_fallback": int(source_counts["fallback"] or 0),
        },
        "unknown_reason_counts": dict(sorted(reason_counts.items())),
        "unknown_reasons_multi_label": True,
        "pool_size": None if blocking_items else len(pool),
        "items": pool,
        "evaluated_items": evaluated_items,
        "blocking_items": blocking_items,
        "eligibility_contract": (
            "forward-only profile=live action=open closed experience with canonical "
            "fixed_tp plan, append-final close event carrying ordId independently "
            "matched to exactly one live_trades row, and exactly 16 contiguous "
            "fully post-exit market.db 15m OHLC bars; SHA-256 is recomputed over "
            "identity+plan+fill+bars"),
        "reason": (
            "dynamic_exit/no_fixed_tp are not applicable; missing fixed-TP plan, "
            "authoritative fill, or exact bars blocks publication instead of "
            "becoming a fabricated zero; in-position MFE is never substituted"),
    }


def _independent_missed_take_profit(
    account_db: Path,
    live_trades_db: Path,
    market_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    """Open all three authorities read-only and always release every handle."""
    connections: list[sqlite3.Connection] = []
    try:
        for path in (account_db, live_trades_db, market_db):
            connections.append(_open_readonly(path))
        return _independent_missed_take_profit_from_connections(
            connections[0], connections[1], connections[2],
            candidate_start, candidate_end)
    finally:
        for connection in reversed(connections):
            connection.close()


def _independent_exit_quality(
    account_db: Path,
    live_trades_db: Path,
    market_db: Path,
    report_start: str,
    report_end: str,
) -> dict[str, Any]:
    candidate_start, candidate_end = _expected_exit_quality_window(
        report_start, report_end)
    peak = _independent_peak_giveback(
        account_db, candidate_start, candidate_end)
    return {
        "schema_version": EXIT_QUALITY_SCHEMA_VERSION,
        "version": EXIT_QUALITY_SCHEMA_VERSION,
        "method_version": EXIT_QUALITY_METHOD_VERSION,
        "business_date": report_end[:10],
        "report_activation_cst": EXIT_QUALITY_ACTIVATION_TS,
        "margin_fact_activation_cycle": EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE,
        "counterfactual_activation_cst": (
            EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS),
        "outcome_horizon_hours": EXIT_QUALITY_OUTCOME_HOURS,
        "report_window": {
            "start_ts": report_start,
            "end_ts": report_end,
            "end_exclusive": True,
        },
        "candidate_window": {
            "start_ts": candidate_start,
            "end_ts": candidate_end,
            "end_exclusive": True,
        },
        "peak_giveback": peak,
        "margin_return_review": _independent_margin_review(
            live_trades_db, report_start, report_end),
        "missed_take_profit": _independent_missed_take_profit(
            account_db, live_trades_db, market_db,
            candidate_start, candidate_end),
        "safety": {
            "production_database_writes": 0,
            "cycles_replayed": 0,
            "window_extended": False,
            "orders_placed": 0,
        },
    }


def _exit_quality_degraded(business_date: str) -> bool:
    """当日 ready manifest 是否确实记录了 exit_quality 步未被接受。

    只读 manifest 原件独立判断，不信报告自称 —— 报告说「本段不可用」必须能在
    维护记录里对上，否则任何一份漏渲染退出质量段的报告都能靠这条蒙混过关。
    """
    path = REVIEWER_READY_DIR / f"reviewer_ready_{business_date}.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    if manifest.get("business_date") != business_date:
        return False
    if EXIT_QUALITY_DEGRADED_STEP in (
            manifest.get("degraded_critical_steps") or []):
        return True
    step = (manifest.get("steps") or {}).get(EXIT_QUALITY_DEGRADED_STEP)
    return isinstance(step, dict) and step.get("accepted") is not True


def _verify_frozen_exit_artifact(
    embedded: object,
) -> tuple[list[str], dict[str, Any] | None]:
    """Verify artifact bytes and ready binding independently of the producer."""
    if not isinstance(embedded, dict):
        return ["exit_quality: embedded frozen block missing"], None
    proof = embedded.get("frozen_artifact")
    if not isinstance(proof, dict):
        return ["exit_quality: frozen artifact proof missing"], None
    path_text = str(proof.get("path") or "")
    sha = str(proof.get("sha256") or "").lower()
    manifest_text = str(proof.get("ready_manifest") or "")
    if not path_text or not sha or not manifest_text:
        return ["exit_quality: frozen artifact proof incomplete"], None
    errors: list[str] = []
    try:
        artifact_path = Path(path_text)
        raw = artifact_path.read_bytes()
        artifact = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"exit_quality: artifact unreadable: {type(exc).__name__}"], None
    if hashlib.sha256(raw).hexdigest() != sha:
        errors.append("exit_quality: artifact hash differs")
    if proof.get("size_bytes") != len(raw):
        errors.append("exit_quality: artifact size differs")
    embedded_payload = dict(embedded)
    embedded_payload.pop("frozen_artifact", None)
    if artifact != embedded_payload:
        errors.append("exit_quality: embedded block differs from frozen artifact")
    try:
        manifest = json.loads(Path(manifest_text).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return errors + [
            f"exit_quality: ready manifest unreadable: {type(exc).__name__}"
        ], artifact if isinstance(artifact, dict) else None
    step = (manifest.get("steps") or {}).get("exit_quality") if isinstance(
        manifest, dict) else None
    manifest_artifact = step.get("artifact") if isinstance(step, dict) else None
    if not (
        isinstance(manifest, dict)
        and manifest.get("business_date") == (
            artifact.get("business_date") if isinstance(artifact, dict) else None)
        and manifest.get("state") == "ready"
        and manifest.get("ready") is True
        and isinstance(step, dict)
        and step.get("accepted") is True
        and isinstance(manifest_artifact, dict)
        and str(manifest_artifact.get("path") or "") == path_text
        and str(manifest_artifact.get("sha256") or "").lower() == sha
        and manifest_artifact.get("size_bytes") == len(raw)
    ):
        errors.append("exit_quality: ready manifest binding differs")
    return errors, artifact if isinstance(artifact, dict) else None



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


def _historical_frozen_exit_is_authority(
    report_ts: str,
    *,
    now: datetime | None = None,
) -> bool:
    """A completed prior business date is governed by its frozen artifact."""
    try:
        report_date = datetime.strptime(
            str(report_ts), "%Y-%m-%d %H:%M:%S").date()
    except (TypeError, ValueError):
        return False
    current = now or datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    else:
        current = current.astimezone(CST)
    return report_date < current.date()


def _exit_action_layer_text(layer: str, counts: object) -> str:
    values = counts if isinstance(counts, dict) else {}
    return f"{layer}=" + ",".join(
        f"{action}:{int(values.get(action, 0) or 0)}"
        for action in EXIT_QUALITY_ACTION_LAYERS
    )


def _missed_opportunity_count(
    lessons_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> int:
    """Return the authoritative count or fail closed on missing evidence."""
    path = Path(lessons_db)
    if not path.is_file():
        raise ValueError(f"lessons database missing: {path}")
    con = _open_readonly(path)
    try:
        table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='missed_opportunities'"
        ).fetchone()
        if table is None:
            raise ValueError(
                "lessons database missing missed_opportunities table")
        return int(con.execute(
            "SELECT COUNT(*) FROM missed_opportunities "
            "WHERE ts LIKE '202%' AND datetime(ts)>=datetime(?) "
            "AND datetime(ts)<datetime(?)",
            (candidate_start, candidate_end),
        ).fetchone()[0])
    finally:
        con.close()


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
    """Independently rebuild the forward report evidence contract."""
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
    except Exception as exc:
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
    if rebuilt.get("report_window") != {
        "start_ts": period_start,
        "end_ts": period_end,
        "end_exclusive": True,
    }:
        result["artifact_errors"].append(
            "missed_evidence: report window differs")
    candidate = rebuilt.get("candidate_window") or {}
    if any((
        metrics.get("candidate_window_start_ts") != candidate.get("start_ts"),
        metrics.get("candidate_window_end_ts") != candidate.get("end_ts"),
        metrics.get("candidate_window_end_exclusive")
        is not candidate.get("end_exclusive"),
        metrics.get("outcome_horizon_hours")
        != candidate.get("outcome_horizon_hours"),
        metrics.get("required_15m_bars")
        != candidate.get("required_15m_bars"),
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


def validate_report(
    report_path: Path,
    account_db: Path,
    live_trades_db: Path,
    ledger_db: Path,
    market_db: Path | None = None,
    lessons_db: Path | None = None,
    briefing_dir: Path | None = None,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
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
        return {
            "ok": False, "artifact_valid": False,
            "send_allowed": False,
            "errors": errors, "warnings": warnings,
            "checks": checks,
        }
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
    db_root = Path(account_db).parent
    market_db = (
        Path(market_db) if market_db is not None else db_root / "market.db")
    lessons_db = (
        Path(lessons_db) if lessons_db is not None else db_root / "lessons.db")
    briefing_dir = (
        Path(briefing_dir)
        if briefing_dir is not None
        else db_root.parent / "logs" / "briefing")
    release_blockers: list[str] = []
    evidence_status: str | None = None
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
            "SELECT ts,profile,open_count,close_count,total_pnl,"
            "total_fees,raw "
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
        return {
            "ok": False, "artifact_valid": False,
            "send_allowed": False,
            "errors": errors, "warnings": warnings,
            "checks": checks,
        }
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
        embedded_missed = (
            audit_by_profile["live"].get(
                "missed_opportunity_metrics") or {})
        embedded_contract = embedded_missed.get("evidence_contract")
        embedded_evidence_status = (
            str(embedded_contract.get("status") or "ERROR")
            if isinstance(embedded_contract, dict) else None)
        is_evidence_draft = (
            thresholds.missed_opportunity_evidence_contract_active(end_ts)
            and status == "evidence_draft"
            and embedded_evidence_status in {
                "SOURCE_LAG", "NO_DATA", "ERROR"})
        if not (is_final or is_provisional or is_evidence_draft):
            errors.append("reconciliation: state/status combination invalid")
        if is_final and "最终报告" not in content:
            errors.append("reconciliation: final audit lacks final banner")
        if is_provisional and "临时报告" not in content:
            errors.append(
                "reconciliation: provisional audit lacks provisional banner")
        if is_evidence_draft and "证据草稿" not in content:
            errors.append(
                "reconciliation: evidence draft lacks draft banner")
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
        continuity_errors, continuity_warnings, continuity_checks = (
            _daily_window_continuity(previous_end, start_ts)
        )
        errors.extend(continuity_errors)
        warnings.extend(continuity_warnings)
        checks.extend(continuity_checks)

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
        # P0-1：边界后头条口径 = close + reduce；边界前保持仅 close。
        if str(report_ts) >= TOTAL_REALIZED_PNL_REQUIRED_FROM:
            _pnl_key, _embedded_pnl_key = (
                "total_realized_pnl", "total_realized_pnl")
        else:
            _pnl_key, _embedded_pnl_key = "realized_pnl", "realized_pnl"
        comparisons = (
            row["open_count"] == embedded.get("open_count")
            == facts["open_count"],
            row["close_count"] == embedded.get("close_count")
            == facts["close_count"],
            _same_number(row["total_pnl"], embedded.get(_embedded_pnl_key))
            and _same_number(row["total_pnl"], facts[_pnl_key]),
            embedded.get("period_start_ts") == start_ts,
            embedded.get("period_end_ts") == end_ts,
            embedded.get("period_end_exclusive") is True,
        )
        if not all(comparisons):
            errors.append(f"audit: {profile} report-time facts differ")
        # P0-2：有平仓却零手续费 = 事实缺失，不是「这天真没手续费」。
        # 带预注册激活边界，历史零费日报不反向加责。
        if str(report_ts) >= FEES_RECONCILIATION_REQUIRED_FROM:
            _fees = row["total_fees"]
            if int(row["close_count"] or 0) > 0 and (
                    _fees is None or abs(float(_fees)) < 1e-9):
                errors.append(
                    f"fees: {profile} close_count>0 但 total_fees=0/NULL；"
                    "必须来自 account_bills 同窗 SUM(fee)")
            else:
                checks.append("fees_nonzero_with_closes")
            _fees_audit = (audit.get("fees_reconciliation") or {}).get(profile)
            if not isinstance(_fees_audit, dict):
                errors.append(f"fees: {profile} report_audit 缺 fees_reconciliation")
            elif _fees_audit.get("source") not in (
                    "account_bills.sum_abs_fee", "unavailable"):
                errors.append(
                    f"fees: {profile} fees_source 非法 "
                    f"({_fees_audit.get('source')})")
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
    embedded_missed = (
        audit_by_profile.get("live", {}).get(
            "missed_opportunity_metrics") or {})
    evidence = _validate_missed_opportunity_contract(
        period_start=start_ts,
        period_end=end_ts,
        embedded_metrics=embedded_missed,
        lessons_db=lessons_db,
        live_trades_db=live_trades_db,
        market_db=market_db,
        briefing_dir=briefing_dir,
    )
    evidence_status = evidence["status"]
    release_blockers.extend(evidence["release_blockers"])
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
            if not matured_missed_line:
                errors.append(
                    "missed_opps: COMPLETE evidence requires mature count")
            else:
                if (
                    matured_missed_line.group(2),
                    matured_missed_line.group(3),
                ) != (candidate.get("start_ts"), candidate.get("end_ts")):
                    errors.append(
                        "missed_opps: markdown candidate window differs from contract")
                if int(matured_missed_line.group(1)) != contract.get("count"):
                    errors.append(
                        "missed_opps: markdown count differs from contract")
            if "证据草稿" in content:
                errors.append(
                    "release: COMPLETE report must not be labelled draft")
        else:
            if missed_line:
                errors.append(
                    "missed_opps: non-COMPLETE draft must not publish naked count")
            if "证据草稿" not in content:
                errors.append(
                    f"release: {evidence_status or 'ERROR'} report lacks draft label")
    elif missed_line:
        # Preserve the complete pre-activation COUNT behavior without
        # reinterpreting historical reports through the new contract.
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
            candidate_start, candidate_end = start_ts, end_ts
        try:
            actual_missed = _missed_opportunity_count(
                lessons_db, candidate_start, candidate_end)
        except (OSError, sqlite3.Error, ValueError) as exc:
            errors.append(f"missed_opps: {exc}")
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
            expected_embedded = (
                embedded_missed.get("candidate_window_start_ts")
                == candidate_start,
                embedded_missed.get("candidate_window_end_ts")
                == candidate_end,
                embedded_missed.get("candidate_window_end_exclusive") is True,
                embedded_missed.get("outcome_horizon_hours")
                == MISSED_OPPORTUNITY_OUTCOME_HOURS,
                embedded_missed.get("required_15m_bars") == 16,
                embedded_missed.get("count")
                == int(matured_missed_line.group(1)),
            )
            if not all(expected_embedded):
                errors.append(
                    "missed_opps: embedded mature outcome metrics differ")


    # ---- 退出质量段（激活边界起硬性要求，历史归档不反向加责）----
    exit_quality_header = "## 🚪 退出质量"
    if report_ts >= EXIT_QUALITY_ACTIVATION_TS:
        if market_db is None:
            errors.append("exit_quality: market_db missing")
            return {
                "ok": False,
                "artifact_valid": False,
                "send_allowed": False,
                "report_ts": report_ts,
                "errors": errors,
                "warnings": warnings,
                "checks": sorted(set(checks)),
                "profiles_checked": len(by_profile),
                "auto_send": False,
            }
        if exit_quality_header not in content:
            errors.append("exit_quality: missing section marker")
        embedded_exit = (
            audit_by_profile.get("live", {}).get("exit_quality"))
        # 降级形态：报告自证本段为 null + 维护记录确实有该步未被接受。
        # 两个条件都要满足才放行；只缺其一都按原样报错（缺段不等于降级）。
        if embedded_exit is None and _exit_quality_degraded(report_ts[:10]):
            checks.append("exit_quality_degraded_section_accepted")
            proof_errors, frozen_exit = [], None
            skip_exit_quality_comparison = True
        else:
            proof_errors, frozen_exit = _verify_frozen_exit_artifact(
                embedded_exit)
            skip_exit_quality_comparison = False
        errors.extend(proof_errors)
        expected_exit = _independent_exit_quality(
            account_db, live_trades_db, market_db, start_ts, end_ts)
        # 边界前：peak_giveback 改以冻结工件为准（见 PEAK_GIVEBACK_NET_R_
        # ACTIVATION_TS 注释）。没有冻结工件就退回独立重算 —— 无当时真值可依，
        # 宁可报错也不放行。margin_return_review / missed_take_profit 不读
        # 路径指标列，不受 v3 影响，仍走独立重算。
        if (report_ts < PEAK_GIVEBACK_NET_R_ACTIVATION_TS
                and isinstance(frozen_exit, dict)
                and isinstance(frozen_exit.get("peak_giveback"), dict)):
            expected_exit = dict(expected_exit)
            expected_exit["peak_giveback"] = frozen_exit["peak_giveback"]
            checks.append("exit_quality_peak_pre_activation_frozen")
        if frozen_exit is not None:
            generated_at = frozen_exit.get("generated_at")
            actual_without_generated = dict(frozen_exit)
            actual_without_generated.pop("generated_at", None)
            historical_frozen_authority = bool(
                not proof_errors
                and _historical_frozen_exit_is_authority(report_ts)
            )
            if not isinstance(generated_at, str) or not generated_at.strip():
                errors.append("exit_quality: generated_at missing")
            else:
                try:
                    if trade_report_stats.parse_cst(
                            generated_at) < trade_report_stats.parse_cst(end_ts):
                        errors.append(
                            "exit_quality: generated_at precedes report window close")
                except (TypeError, ValueError):
                    errors.append("exit_quality: generated_at invalid")
            if actual_without_generated != expected_exit:
                if historical_frozen_authority:
                    warnings.append(
                        "exit_quality: current-source reconstruction differs "
                        "from immutable historical artifact; frozen facts retained"
                    )
                else:
                    errors.append(
                        "exit_quality: frozen all-field reconstruction differs")
            if historical_frozen_authority:
                expected_exit = actual_without_generated
                checks.append("exit_quality_historical_frozen_authority")
        if not proof_errors and frozen_exit is not None:
            checks.append("exit_quality_frozen_artifact")

        # Markdown is a view of the independently rebuilt facts.  Verify every
        # rendered category plus the forward/unknown coverage statements.
        peak = expected_exit["peak_giveback"]
        margin = expected_exit["margin_return_review"]
        missed = expected_exit["missed_take_profit"]
        rendered_fragments = (
            f"候选窗口 [{expected_exit['candidate_window']['start_ts']}, "
            f"{expected_exit['candidate_window']['end_ts']})",
            f"峰值回吐: {peak['status']}",
            f"峰值有效窗 [{peak['effective_window']['start_ts']}, "
            f"{peak['effective_window']['end_ts']})",
            f"候选open平仓 {peak['candidate_closed_rows']} 笔、激活前排除 "
            f"{peak['pre_activation_excluded_rows']} 笔",
            f"已成熟平仓: {peak['closed_rows']} 笔",
            f"源关闭记录 {peak['source_closed_rows']} 笔、排除非live "
            f"{peak['excluded_non_live_rows']} 笔、排除非open "
            f"{peak['excluded_non_open_rows']} 笔",
            f"路径可测 {peak['measured_rows']} 笔",
            f"覆盖不足按未知计 {peak['unknown_path_rows']} 笔",
            f"持仓期利润回吐案例: {peak['profit_giveback_case_count']} 笔",
            f"错失止盈池: {missed['status']}",
            f"evidence={missed['evidence_method_version']}",
            f"候选{missed['candidate_exits']}、已评估"
            f"{missed['evaluated_exits']}、未知{missed['unknown_exits']}",
            f"pool={missed['pool_size']}",
            f"未知原因={missed['unknown_reason_counts']}",
            f"激活前排除={missed['pre_activation_excluded_exits']}",
            f"非固定TP不适用={missed['not_applicable_exits']}",
            f"排除非live={missed['excluded_profile_count']}、"
            f"fallback={missed['excluded_fallback_count']}",
            f"[{margin['effective_cycle_window']['start_cycle']}, "
            f"{margin['effective_cycle_window']['end_cycle']})",
            f"激活前排除 {margin['pre_activation_excluded_cycle_rows']} 个cycle",
            f"源cycle {margin['source_candidate_cycle_rows']}、排除非live "
            f"{margin['excluded_non_live_cycle_rows']}、排除非open仓位 "
            f"{margin['excluded_non_open_position_rows']}",
            f"字段可观测 {margin['fact_observed_position_cycles']}/"
            f"{margin['total_position_cycles']}，未知 "
            f"{margin['unknown_fact_position_cycles']}",
            f"被标记仓位-周期: {margin['flagged_position_cycles']} 次",
            f"决策卡显式复核 {margin['explicitly_reviewed']} 次",
        )
        if skip_exit_quality_comparison:
            # 本段如实不可用：没有数就没什么可逐字段比对的。改校验留空段的
            # 确定性措辞仍在 —— 否则「降级」会退化成「随便渲染什么都行」。
            rendered_fragments = ()
            if "退出质量统计不可用" not in content:
                errors.append(
                    "exit_quality: degraded section must state 退出质量统计不可用")
            if "不以 0 冒充" not in content:
                errors.append(
                    "exit_quality: degraded section must keep the "
                    "no-zero-substitution statement")
        for fragment in rendered_fragments:
            if fragment not in content:
                errors.append(
                    "exit_quality: markdown differs at " + fragment)
        for name, count in (
                {} if skip_exit_quality_comparison
                else margin["disposition_counts"]).items():
            if f"{name} {count}次" not in content:
                errors.append(
                    f"exit_quality: markdown disposition differs for {name}")
        action_layers = (
            {} if skip_exit_quality_comparison
            else margin["action_layer_counts"])
        for layer in ("requested", "succeeded", "fills", "failed"):
            if layer not in action_layers:
                continue
            rendered = _exit_action_layer_text(
                layer, action_layers.get(layer))
            if rendered not in content:
                errors.append(
                    f"exit_quality: markdown action layer differs for {layer}")
        for label, count in (
                {} if skip_exit_quality_comparison
                else peak["profitable_peak_giveback_buckets_r"]).items():
            if f"{label} {count}笔" not in content:
                errors.append(
                    f"exit_quality: markdown bucket differs for {label}")
        if not any(error.startswith("exit_quality:") for error in errors):
            checks.extend(("exit_quality_section", "exit_quality"))
    elif exit_quality_header in content:
        # A historical archive may already contain older prose.  The v2 gate
        # deliberately does not reinterpret or fail it before activation.
        checks.append("exit_quality_historical_not_rejudged")

    if not any(error.startswith("risk_reject:") for error in errors):
        checks.append("risk_reject")
    if not any(error.startswith("revision:") for error in errors):
        checks.append("revision")
    if not any(error.startswith("audit:") for error in errors):
        checks.append("authoritative_report_time_facts")
    artifact_valid = not errors
    send_allowed = artifact_valid and not release_blockers
    return {
        "ok": artifact_valid and send_allowed,
        "artifact_valid": artifact_valid,
        "send_allowed": send_allowed,
        "evidence_status": evidence_status,
        "report_ts": report_ts,
        "errors": errors + release_blockers,
        "release_blockers": release_blockers,
        "warnings": warnings,
        "checks": sorted(set(checks)),
        "profiles_checked": len(by_profile),
        "auto_send": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="read-only reviewer daily report validator")
    parser.add_argument("--file", required=True)
    parser.add_argument("--db-root", default=_public_project_path('db'))
    parser.add_argument("--account-db")
    parser.add_argument("--live-trades-db")
    parser.add_argument("--market-db")
    parser.add_argument("--lessons-db")
    parser.add_argument("--briefing-dir")
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
        "market_db": (
            Path(args.market_db) if args.market_db else root / "market.db"),
        "ledger_db": (
            Path(args.ledger_db)
            if args.ledger_db else root / "ledger.db"),
        "lessons_db": (
            Path(args.lessons_db)
            if args.lessons_db else root / "lessons.db"),
        "briefing_dir": (
            Path(args.briefing_dir)
            if args.briefing_dir else root.parent / "logs" / "briefing"),
    }
    # lessons.db is validated fail-closed only when the report contains a
    # missed-opportunity fact.  Keep it out of this unconditional preflight so
    # legacy reports without that section remain inspectable.
    missing = [
        str(path) for name, path in paths.items()
        if name not in {"lessons_db", "briefing_dir"} and not path.exists()
    ]
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
