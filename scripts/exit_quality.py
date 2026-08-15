# -*- coding: utf-8 -*-
"""退出质量：峰值回吐分布、≥50% 保证金收益率复核率、错失止盈池。

只读 `account.db.trade_experiences`（路径指标权威）与
`live_trades.db.trade_cycles/trades`（复核证据与实际处置）。本模块不写任何
业务库、不重跑周期、不补采、不下单。

**后验窗固化**（对照 `lessons.db.missed_opportunities` 错失开仓池的既有方法
论）：平仓那一刻的 `mfe_r/realized_r_net` 由 15m K 线路径懒计算得出，刚平的
仓可能还没等到路径 K 线闭合。因此统计窗整体前移 ``OUTCOME_HORIZON_HOURS``，
只统计结果已经完整成熟的那一段；窗口由报告窗确定性推导，Reviewer 不重跑、
不扩窗、不改口径。路径覆盖不足的行按「未知」计，不冒充 0。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"

# 与错失开仓池同一后验窗长度：4H 结果成熟。
OUTCOME_HORIZON_HOURS = 4
# 峰值回吐分布的固定分桶（单位 R，左闭右开；最后一档到正无穷）。
GIVEBACK_BUCKETS = (0.0, 0.25, 0.5, 1.0, 2.0)
# 「曾达 1R」判据：路径峰值 ≥ 1R；平仓 ≥ 1R 才算把这段利润拿住。
ONE_R = 1.0
# 「确有浮盈峰值」判据：低于这个峰值谈「回吐」没有意义（那是亏损，不是回吐）。
PROFITABLE_PEAK_R = 0.5
# 路径覆盖不足就不参与分布，只计未知——宁缺勿假。
MINIMUM_PATH_COVERAGE = 0.9
# 复核注意线与 live_decision_facts 同源：50% 保证金收益率。
MARGIN_REVIEW_THRESHOLD = 0.5


def parse_cst(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("T", " ")
        parsed = datetime.strptime(text[:19], TS_FMT)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def candidate_window(start_ts: str, end_ts: str) -> tuple[str, str]:
    """报告窗整体前移一个后验窗，得到结果已成熟的候选窗（左闭右开）。"""
    shift = timedelta(hours=OUTCOME_HORIZON_HOURS)
    return (
        (parse_cst(start_ts) - shift).strftime(TS_FMT),
        (parse_cst(end_ts) - shift).strftime(TS_FMT),
    )


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _coverage_ratio(value: object) -> float | None:
    """`path_coverage` 形如 ``partial_boundary:1.00``/``full:0.93``。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    tail = text.rsplit(":", 1)[-1]
    try:
        return float(tail)
    except ValueError:
        return None


def _bucket_label(giveback: float) -> str:
    edges = GIVEBACK_BUCKETS
    for index, edge in enumerate(edges):
        if giveback < edge:
            return f"<{edge:g}R" if index == 0 else f"{edges[index - 1]:g}-{edge:g}R"
    return f">={edges[-1]:g}R"


def _empty_buckets() -> dict[str, int]:
    labels = [f"<{GIVEBACK_BUCKETS[0]:g}R"]
    for index in range(1, len(GIVEBACK_BUCKETS)):
        labels.append(
            f"{GIVEBACK_BUCKETS[index - 1]:g}-{GIVEBACK_BUCKETS[index]:g}R")
    labels.append(f">={GIVEBACK_BUCKETS[-1]:g}R")
    return {label: 0 for label in labels}


def peak_giveback(
    account_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    """浮盈峰值回吐分布 + 错失止盈池（曾达 1R 却没在 1R 以上平仓）。"""
    connection = _ro(account_db)
    try:
        rows = connection.execute(
            "SELECT symbol,side,closed_at,mfe_r,mae_r,realized_r_net,"
            "ever_hit_1r,close_at_1r,exit_category,path_coverage "
            "FROM trade_experiences "
            "WHERE status='closed' AND closed_at IS NOT NULL "
            "AND datetime(closed_at)>=datetime(?) "
            "AND datetime(closed_at)<datetime(?) "
            "ORDER BY closed_at",
            (candidate_start, candidate_end),
        ).fetchall()
    finally:
        connection.close()

    buckets = _empty_buckets()
    profitable_buckets = _empty_buckets()
    measured: list[float] = []
    profitable_givebacks: list[float] = []
    retentions: list[float] = []
    unknown = 0
    pool: list[dict[str, Any]] = []
    reached_one_r = 0
    held_to_one_r = 0
    flag_disagreements = 0

    for row in rows:
        mfe = row["mfe_r"]
        realized = row["realized_r_net"]
        coverage = _coverage_ratio(row["path_coverage"])
        if (
            mfe is None
            or realized is None
            or coverage is None
            or coverage < MINIMUM_PATH_COVERAGE
        ):
            unknown += 1
            continue
        mfe = float(mfe)
        realized = float(realized)
        giveback = round(mfe - realized, 4)
        measured.append(giveback)
        buckets[_bucket_label(giveback)] += 1
        if mfe >= PROFITABLE_PEAK_R:
            # 只有确实攒出过浮盈峰值的仓，才谈得上「回吐」。
            profitable_givebacks.append(giveback)
            profitable_buckets[_bucket_label(giveback)] += 1
            retentions.append(round(realized / mfe, 4))
        hit = mfe >= ONE_R
        if hit:
            reached_one_r += 1
            if realized >= ONE_R:
                held_to_one_r += 1
            else:
                pool.append({
                    "symbol": str(row["symbol"]),
                    "side": str(row["side"]),
                    "closed_at": str(row["closed_at"]),
                    "peak_r": round(mfe, 4),
                    "realized_r_net": round(realized, 4),
                    "giveback_r": giveback,
                    "exit_category": (
                        str(row["exit_category"])
                        if row["exit_category"] is not None else None),
                })
        if bool(row["ever_hit_1r"]) != hit:
            flag_disagreements += 1

    def _median(values: list[float]) -> float | None:
        ranked = sorted(values)
        if not ranked:
            return None
        middle = len(ranked) // 2
        if len(ranked) % 2:
            return ranked[middle]
        return round((ranked[middle - 1] + ranked[middle]) / 2, 4)

    ordered = sorted(measured)
    median = (
        None if not ordered
        else ordered[len(ordered) // 2] if len(ordered) % 2
        else round((ordered[len(ordered) // 2 - 1]
                    + ordered[len(ordered) // 2]) / 2, 4)
    )
    return {
        "closed_rows": len(rows),
        "measured_rows": len(measured),
        "unknown_path_rows": unknown,
        "giveback_buckets_r": buckets,
        "giveback_median_r": median,
        "giveback_max_r": max(ordered) if ordered else None,
        "profitable_peak_rows": len(profitable_givebacks),
        "profitable_peak_threshold_r": PROFITABLE_PEAK_R,
        "profitable_peak_giveback_buckets_r": profitable_buckets,
        "profitable_peak_giveback_median_r": _median(profitable_givebacks),
        "peak_retention_median": _median(retentions),
        "reached_1r": reached_one_r,
        "closed_at_or_above_1r": held_to_one_r,
        "missed_take_profit_pool_size": len(pool),
        "missed_take_profit_pool": pool,
        "ever_hit_1r_flag_disagreements": flag_disagreements,
        "semantics": (
            "giveback_r = mfe_r - realized_r_net; rows without matured or "
            "sufficient 15m path coverage are unknown, never counted as zero"
        ),
    }


def _decision_text(payload: dict[str, Any]) -> str:
    card = payload.get("decision_card")
    if not isinstance(card, dict):
        return ""
    parts = []
    for key in (
        "agent_judgement", "execution_conditions", "invalidation_point",
        "portfolio_impact",
    ):
        value = card.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def margin_return_review(
    live_trades_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    """≥50% 保证金收益率仓位的显式复核率与处置分布。

    分母=窗口内每个「周期 × 被标记仓位」；显式复核=该标的出现在同周期决策卡
    正文里（复核结论必须可审计）；处置取同周期账本实际成交动作，无成交即 HOLD。
    """
    connection = _ro(live_trades_db)
    try:
        cycles = connection.execute(
            "SELECT cycle_id,ts,raw FROM trade_cycles "
            "WHERE ts IS NOT NULL AND datetime(ts)>=datetime(?) "
            "AND datetime(ts)<datetime(?) ORDER BY cycle_id",
            (candidate_start, candidate_end),
        ).fetchall()
        fills = connection.execute(
            "SELECT cycle_id,symbol,action FROM trades "
            "WHERE ts IS NOT NULL AND datetime(ts)>=datetime(?) "
            "AND datetime(ts)<datetime(?)",
            (candidate_start, candidate_end),
        ).fetchall()
    finally:
        connection.close()

    actions: dict[tuple[str, str], set[str]] = {}
    for row in fills:
        key = (str(row["cycle_id"]), str(row["symbol"]))
        actions.setdefault(key, set()).add(str(row["action"]).lower())

    flagged = 0
    reviewed = 0
    unreadable_cycles = 0
    dispositions: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for row in cycles:
        try:
            payload = json.loads(row["raw"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            unreadable_cycles += 1
            continue
        if not isinstance(payload, dict):
            unreadable_cycles += 1
            continue
        positions = ((payload.get("live_facts") or {}).get("positions")
                     if isinstance(payload.get("live_facts"), dict) else None)
        if not isinstance(positions, list):
            continue
        text = _decision_text(payload)
        cycle_id = str(row["cycle_id"])
        for position in positions:
            if not isinstance(position, dict):
                continue
            if not position.get("margin_return_review_at_or_above_50pct"):
                continue
            symbol = str(position.get("instId") or position.get("symbol") or "")
            if not symbol:
                continue
            flagged += 1
            base = symbol.split("-", 1)[0]
            explicit = bool(text) and (symbol in text or base in text)
            if explicit:
                reviewed += 1
            taken = actions.get((cycle_id, symbol)) or set()
            if "close" in taken:
                disposition = "close"
            elif "reduce" in taken:
                disposition = "reduce"
            elif taken:
                disposition = sorted(taken)[0]
            else:
                disposition = "hold"
            dispositions[disposition] = dispositions.get(disposition, 0) + 1
            items.append({
                "cycle_id": cycle_id,
                "symbol": symbol,
                "upl_ratio_initial_margin": position.get(
                    "upl_ratio_initial_margin"),
                "explicitly_reviewed": explicit,
                "disposition": disposition,
            })

    return {
        "threshold_fraction": MARGIN_REVIEW_THRESHOLD,
        "flagged_position_cycles": flagged,
        "explicitly_reviewed": reviewed,
        "explicit_review_rate": (
            round(reviewed / flagged, 6) if flagged else None),
        "disposition_counts": dict(sorted(dispositions.items())),
        "unreadable_cycle_rows": unreadable_cycles,
        "items": items,
        "semantics": (
            "denominator = one row per cycle x flagged position; explicit "
            "review = the instrument is named in that cycle decision card; "
            "disposition comes from the ledger fills, absent fills = hold"
        ),
    }


def compute(
    *,
    account_db: Path,
    live_trades_db: Path,
    report_start_ts: str,
    report_end_ts: str,
) -> dict[str, Any]:
    """Return the whole exit-quality block for one report window."""
    candidate_start, candidate_end = candidate_window(
        report_start_ts, report_end_ts)
    return {
        "version": 1,
        "outcome_horizon_hours": OUTCOME_HORIZON_HOURS,
        "report_window": {"start_ts": report_start_ts,
                          "end_ts": report_end_ts},
        "candidate_window": {"start_ts": candidate_start,
                             "end_ts": candidate_end},
        "peak_giveback": peak_giveback(
            account_db, candidate_start, candidate_end),
        "margin_return_review": margin_return_review(
            live_trades_db, candidate_start, candidate_end),
        "safety": {
            "production_database_writes": 0,
            "cycles_replayed": 0,
            "window_extended": False,
            "orders_placed": 0,
        },
    }
