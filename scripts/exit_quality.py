# -*- coding: utf-8 -*-
"""Build the frozen, forward-only daily exit-quality evidence artifact.

The producer reads ``account.db.trade_experiences`` and
``live_trades.db.trade_cycles/trades`` in SQLite read-only mode. It never
writes a business database, replays a cycle, or places an order. One daily
JSON artifact is published with an atomic write-once contract; downstream
reporting and briefing consumers read that artifact instead of recomputing.

Missed take profit is reconstructed only for forward, canonical ``fixed_tp``
open experiences.  The evidence binds the original plan, one authoritative
final fill, and exactly 16 fully post-exit 15m bars.  Missing authority blocks
publication instead of being inferred from MFE or fabricated as zero.
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
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import _acceptance_thresholds as thresholds
from core.decision_card import (
    OPEN_EXECUTION_PACKAGE_KEY,
    is_open_execution_package,
)


CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
SCHEMA_VERSION = 2
METHOD_VERSION = "exit_quality_v2_forward_frozen"
REPORT_ACTIVATION_TS = "2026-08-16 08:00:00"
MARGIN_FACT_ACTIVATION_CYCLE = "2026-08-15T14:45"
COUNTERFACTUAL_ACTIVATION_TS = "2026-08-16 08:00:00"
# 2026-08-20：exit_mode 成为 open_* 必填的实际生效时刻（按开仓日实证：
# 2026-08-13 及之前 100% 缺失、08-14 起 100% 具备，转折干净）。
# COUNTERFACTUAL_ACTIVATION_TS 是按 **closed_at** 判定的前向边界，而它索取
# 的 exit_mode 是**开仓时刻**写进卡的 —— 横跨边界的持仓（开仓早于要求、
# 平仓晚于边界）会掉进「索取一个当时合法地不存在的字段」的缝里。实证：
# 经验 289 开于 08-11 16:27、持有 7 天平于 08-18 10:44，缺 exit_mode 被判
# blocked → exit_quality rc=2 → 关键步被拒 → **2026-08-19 日报整份不存在**。
# 故 legacy 判据必须看开仓时刻，不能看平仓时刻。
EXIT_MODE_MANDATE_FROM = "2026-08-14 00:00:00"
PEAK_GIVEBACK_ACTIVATION_TS = "2026-08-16 08:00:00"
# 2026-08-19 G1：v2 = 净 R 口径（path_metric_version>=3）。边界前已发日报按
# v1 数字归档，不重算不重判；边界后新报告用 v2，两版都在 payload 自证。
PEAK_GIVEBACK_METHOD_VERSION = "peak_giveback_forward_v2"
PEAK_GIVEBACK_NET_R_ACTIVATION_CST = "2026-08-20T08:00:00+08:00"
OUTCOME_HORIZON_HOURS = 4
REQUIRED_COUNTERFACTUAL_15M_BARS = 16
MISSED_TAKE_PROFIT_METHOD_VERSION = (
    "post_exit_counterfactual_16x15m_v1")
COUNTERFACTUAL_EVIDENCE_METHOD_VERSION = (
    "authoritative_exit_fill_market_16x15m_v1")
GIVEBACK_BUCKETS = (0.0, 0.25, 0.5, 1.0, 2.0)
ONE_R = 1.0
PROFITABLE_PEAK_R = 0.5
MINIMUM_PATH_COVERAGE = 0.9
MARGIN_REVIEW_THRESHOLD = 0.5
DISPOSITION_KEYS = (
    "hold", "close", "reduce", "adjust", "add", "open",
    "attempted_failed", "requested_unconfirmed")
ACTION_LAYER_KEYS = ("close", "reduce", "adjust", "add", "open")
DISPOSITION_PRIORITY = ("close", "reduce", "add", "open", "adjust")
DEFAULT_DB_ROOT = Path(os.environ.get("OKX_DB_ROOT", _public_project_path('db')))
DEFAULT_QUALITY_DIR = Path(os.environ.get(
    "OKX_QUALITY_REPORT_DIR", _public_project_path('reports', 'quality')))
DEFAULT_WINDOW_CLOSE_WAIT_SECONDS = 600


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
    """Shift a report window by the fixed outcome horizon (left-closed)."""
    shift = timedelta(hours=OUTCOME_HORIZON_HOURS)
    return (
        (parse_cst(start_ts) - shift).strftime(TS_FMT),
        (parse_cst(end_ts) - shift).strftime(TS_FMT),
    )


def daily_report_window(as_of: str | datetime) -> tuple[str, str]:
    """Return the last complete ``[08:00, 08:00)`` CST report window."""
    reference = parse_cst(as_of)
    end = reference.replace(hour=8, minute=0, second=0, microsecond=0)
    if reference < end:
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    return start.strftime(TS_FMT), end.strftime(TS_FMT)


def default_daily_as_of() -> str:
    """Daily maintenance starts at 07:55 for the report ending today 08:00."""
    now = datetime.now(CST)
    return now.replace(hour=8, minute=5, second=0, microsecond=0).strftime(
        TS_FMT)


def _cycle_boundary(value: str) -> str:
    return parse_cst(value).strftime("%Y-%m-%dT%H:%M")


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _coverage_ratio(value: object) -> float | None:
    """Parse the real path contract: ``full`` is 1.0 and ``none`` unknown."""
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
        if (
            (prefix == "full" or prefix.startswith("partial"))
            and math.isfinite(ratio) and 0.0 <= ratio <= 1.0
        ):
            return ratio
        return None
    try:
        ratio = float(text)
    except ValueError:
        return None
    return ratio if math.isfinite(ratio) and 0.0 <= ratio <= 1.0 else None


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bucket_label(giveback: float) -> str:
    for index, edge in enumerate(GIVEBACK_BUCKETS):
        if giveback < edge:
            return (
                f"<{edge:g}R" if index == 0
                else f"{GIVEBACK_BUCKETS[index - 1]:g}-{edge:g}R")
    return f">={GIVEBACK_BUCKETS[-1]:g}R"


def _empty_buckets() -> dict[str, int]:
    labels = [f"<{GIVEBACK_BUCKETS[0]:g}R"]
    labels.extend(
        f"{GIVEBACK_BUCKETS[index - 1]:g}-{GIVEBACK_BUCKETS[index]:g}R"
        for index in range(1, len(GIVEBACK_BUCKETS)))
    labels.append(f">={GIVEBACK_BUCKETS[-1]:g}R")
    return {label: 0 for label in labels}


def _median(values: list[float]) -> float | None:
    ranked = sorted(values)
    if not ranked:
        return None
    middle = len(ranked) // 2
    if len(ranked) % 2:
        return ranked[middle]
    return round((ranked[middle - 1] + ranked[middle]) / 2, 4)


def peak_giveback(
    account_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    """Measure in-position peak giveback; insufficient paths stay unknown."""
    effective_start = max(
        parse_cst(candidate_start).strftime(TS_FMT),
        PEAK_GIVEBACK_ACTIVATION_TS,
    )
    candidate_end = parse_cst(candidate_end).strftime(TS_FMT)
    connection = _ro(account_db)
    try:
        source_counts = connection.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN COALESCE(profile,'')!='live' THEN 1 ELSE 0 END) "
            "AS non_live,"
            "SUM(CASE WHEN profile='live' "
            "AND COALESCE(action,'')!='open' THEN 1 ELSE 0 END) "
            "AS non_open "
            "FROM trade_experiences WHERE status='closed' "
            "AND closed_at IS NOT NULL AND closed_at>=? AND closed_at<?",
            (candidate_start, candidate_end),
        ).fetchone()
        candidate_count = int(connection.execute(
            "SELECT COUNT(*) FROM trade_experiences "
            "WHERE profile='live' AND action='open' "
            "AND status='closed' AND closed_at IS NOT NULL "
            "AND closed_at>=? AND closed_at<?",
            (candidate_start, candidate_end),
        ).fetchone()[0])
        rows = connection.execute(
            "SELECT symbol,side,closed_at,mfe_r,mae_r,realized_r_net,"
            "ever_hit_1r,close_at_1r,exit_category,path_coverage "
            "FROM trade_experiences "
            "WHERE profile='live' AND action='open' "
            "AND status='closed' AND closed_at IS NOT NULL "
            "AND closed_at>=? AND closed_at<? ORDER BY closed_at,id",
            (effective_start, candidate_end),
        ).fetchall() if effective_start < candidate_end else []
    finally:
        connection.close()

    buckets = _empty_buckets()
    profitable_buckets = _empty_buckets()
    measured: list[float] = []
    profitable_givebacks: list[float] = []
    retentions: list[float] = []
    cases: list[dict[str, Any]] = []
    unknown = reached_one_r = held_to_one_r = flag_disagreements = 0

    for row in rows:
        coverage = _coverage_ratio(row["path_coverage"])
        mfe = _finite_float(row["mfe_r"])
        realized = _finite_float(row["realized_r_net"])
        if (
            mfe is None or realized is None or coverage is None
            or coverage < MINIMUM_PATH_COVERAGE
        ):
            unknown += 1
            continue
        giveback = round(mfe - realized, 4)
        measured.append(giveback)
        buckets[_bucket_label(giveback)] += 1
        if mfe >= PROFITABLE_PEAK_R:
            profitable_givebacks.append(giveback)
            profitable_buckets[_bucket_label(giveback)] += 1
            retentions.append(round(realized / mfe, 4))
        hit = mfe >= ONE_R
        if hit:
            reached_one_r += 1
            if realized >= ONE_R:
                held_to_one_r += 1
            else:
                cases.append({
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
        if row["ever_hit_1r"] is not None and bool(row["ever_hit_1r"]) != hit:
            flag_disagreements += 1

    return {
        "method_version": PEAK_GIVEBACK_METHOD_VERSION,
        "fact_activation_cst": PEAK_GIVEBACK_ACTIVATION_TS,
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
        "giveback_median_r": _median(measured),
        "giveback_max_r": max(measured) if measured else None,
        "profitable_peak_rows": len(profitable_givebacks),
        "profitable_peak_threshold_r": PROFITABLE_PEAK_R,
        "profitable_peak_giveback_buckets_r": profitable_buckets,
        "profitable_peak_giveback_median_r": _median(profitable_givebacks),
        "peak_retention_median": _median(retentions),
        "reached_1r": reached_one_r,
        "closed_at_or_above_1r": held_to_one_r,
        "profit_giveback_case_count": len(cases),
        "profit_giveback_cases": cases,
        "ever_hit_1r_flag_disagreements": flag_disagreements,
        "semantics": (
            "live closed action=open experiences only; giveback_r = in-position "
            "mfe_r - realized_r_net; full=1.0; none/missing/coverage<0.9 "
            "are unknown and excluded; non-live/non-open rows are audited outside "
            "the denominator"),
    }


def _ceil_15m(value: datetime) -> datetime:
    minute = (value.minute // 15) * 15
    floor = value.replace(minute=minute, second=0, microsecond=0)
    return floor if value == floor else floor + timedelta(minutes=15)


def _parse_utc(value: object) -> datetime:
    text = str(value or "").strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_snapshot_sha256(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _event_ord_identity(event: dict[str, Any]) -> set[str]:
    """close_event 的权威身份：单一 ordId，或多单聚合平仓的 ord_ids 集合。"""
    single = str(event.get("ordId") or "").strip()
    if single:
        return {single}
    return {
        str(item) for item in (event.get("ord_ids") or [])
        if item not in (None, "", 0)
    }


def _canonical_identity(identity: set[str]) -> str:
    """身份集合的稳定字符串形式（单元素即其本身），供证据字段原样留痕。"""
    return ",".join(sorted(identity))


def _ord_ids(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"ordId", "ord_id"} and child not in (None, "", 0):
                found.add(str(child))
            elif key == "ord_ids" and isinstance(child, list):
                found.update(
                    str(item) for item in child if item not in (None, "", 0))
            elif isinstance(child, (dict, list)):
                found.update(_ord_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_ord_ids(child))
    return found


def _raw_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _opened_before_exit_mode_mandate(row: sqlite3.Row) -> bool:
    """该经验的**开仓时刻**是否早于 exit_mode 成为 open_* 必填的那一刻。

    判据刻意看开仓时刻而非平仓时刻：字段是开仓时写进卡的，用平仓时刻判 legacy
    会把「开仓早于要求、平仓晚于边界」的横跨持仓错判成数据坏（实证：经验 289
    开于 08-11、持有 7 天、平于 08-18，因此卡掉了 2026-08-19 整份日报）。

    取不到开仓时刻时返回 False —— 宁可 fail-closed 也不凭空豁免。
    """
    try:
        keys = set(row.keys())
    except AttributeError:
        return False
    if "ts" not in keys:
        return False
    raw_ts = row["ts"]
    if raw_ts is None:
        return False
    try:
        return parse_cst(str(raw_ts)) < parse_cst(EXIT_MODE_MANDATE_FROM)
    except (TypeError, ValueError):
        return False


def _original_plan(
    row: sqlite3.Row,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    raw = _raw_object(row["raw"])
    if raw is None:
        return None, "blocked", ["trade_experience_raw_unreadable"]
    package = raw.get(OPEN_EXECUTION_PACKAGE_KEY)
    if package is not None:
        if not is_open_execution_package(package):
            return None, "blocked", ["original_open_execution_package_invalid"]
        risk_reward = {
            "entry": package["entry"],
            "stop": package["stop"],
            "target": package["target"],
            "exit_mode": package["exit_mode"],
        }
    else:
        card = raw.get("decision_card")
        risk_reward = (
            card.get("risk_reward") if isinstance(card, dict) else None)
    if not isinstance(risk_reward, dict):
        return None, "blocked", ["original_plan_risk_reward_missing"]
    exit_mode = str(risk_reward.get("exit_mode") or "").strip().lower()
    if exit_mode in {"dynamic_exit", "no_fixed_tp"}:
        return {
            "exit_mode": exit_mode,
            "target_px": risk_reward.get("target"),
        }, "not_applicable", [f"exit_mode_{exit_mode}"]
    if not exit_mode and _opened_before_exit_mode_mandate(row):
        # 2026-08-20：**缺键**且开仓早于强制日 = 当时合法地没有这个字段，
        # 不是数据坏。判 not_applicable（与 legacy_unspecified 同族语义），
        # 不再让一张七天前的老卡把整份日报 fail-closed 掉。
        # 只放行「缺键」；取值存在但非法仍是真契约违规，照旧 blocked。
        return None, "not_applicable", ["exit_mode_legacy_before_mandate"]
    if exit_mode != "fixed_tp":
        return None, "blocked", ["original_plan_exit_mode_invalid_or_missing"]
    try:
        target = float(risk_reward.get("target"))
    except (TypeError, ValueError, OverflowError):
        return None, "blocked", ["original_plan_fixed_tp_target_missing"]
    if not math.isfinite(target):
        return None, "blocked", ["original_plan_fixed_tp_target_not_finite"]
    if target <= 0:
        return None, "blocked", ["original_plan_fixed_tp_target_missing"]
    raw_symbol = str(raw.get("symbol") or raw.get("instId") or "").upper()
    raw_side = str(raw.get("side") or raw.get("pos_side") or "").lower()
    if raw_symbol != str(row["symbol"]).upper():
        return None, "blocked", ["original_plan_symbol_identity_mismatch"]
    if raw_side != str(row["side"]).lower():
        return None, "blocked", ["original_plan_side_identity_mismatch"]
    return {
        "exit_mode": exit_mode,
        "target_px": target,
    }, "eligible", []


def _authoritative_exit_fill(
    row: sqlite3.Row,
    live_connection: sqlite3.Connection,
    trade_cache: dict[tuple[str, str], list[sqlite3.Row]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    raw = _raw_object(row["raw"])
    events = raw.get("close_events") if isinstance(raw, dict) else None
    if not isinstance(events, list) or not events:
        return None, ["authoritative_close_event_missing"]
    closed_at = parse_cst(row["closed_at"]).strftime(TS_FMT)
    # close_events is append-only in the single writer.  Multiple partial
    # closes may share one wall-clock second, so identity comes from the final
    # append, not from an invalid "exactly one event at this second" rule.
    event = events[-1]
    if not isinstance(event, dict):
        return None, ["final_close_event_invalid"]
    try:
        event_ts = parse_cst(event.get("ts")).strftime(TS_FMT)
    except (TypeError, ValueError):
        return None, ["final_close_event_ts_invalid"]
    if event_ts != closed_at:
        return None, ["final_close_event_closed_at_mismatch"]
    cycle_id = str(event.get("cycle_id") or "").strip()
    # 2026-09-12：交易所用多张单平掉同一仓位时，对账回填出的聚合 close 行没有
    # 单一 ordId，权威身份是那组 ordId 的集合。集合同样可核验——仍要求它是
    # trades 行身份集合的子集，且 trades 行仍须唯一；不挑选、不伪造单一 id。
    event_ord_identity = _event_ord_identity(event)
    if not event_ord_identity:
        return None, ["final_close_event_ord_id_missing"]
    event_ord_id = _canonical_identity(event_ord_identity)
    try:
        event_px = float(event.get("fill_px"))
    except (TypeError, ValueError, OverflowError):
        return None, ["final_close_event_fill_px_missing"]
    if not math.isfinite(event_px):
        return None, ["final_close_event_fill_px_not_finite"]
    event_sz = _finite_float(event.get("sz"))
    if (
        not cycle_id or event_px <= 0 or event_sz is None or event_sz <= 0
    ):
        return None, ["final_close_event_identity_incomplete"]
    cache_key = (cycle_id, str(row["symbol"]))
    trade_rows = trade_cache.get(cache_key) if trade_cache is not None else None
    if trade_rows is None:
        trade_rows = live_connection.execute(
            "SELECT id,cycle_id,ts,symbol,action,side,sz,fill_px,raw "
            "FROM trades WHERE cycle_id=? AND symbol=? ORDER BY id",
            cache_key,
        ).fetchall()
        if trade_cache is not None:
            trade_cache[cache_key] = trade_rows
    matches: list[sqlite3.Row] = []
    for trade in trade_rows:
        action = str(trade["action"] or "").lower()
        if not any(token in action for token in (
                "close", "reduce", "stop", "take_profit", "tp")):
            continue
        if str(trade["side"] or "").lower() != str(row["side"]).lower():
            continue
        trade_raw = _raw_object(trade["raw"]) or {}
        authoritative_ts = trade["ts"]
        if (
            trade_raw.get("reconcile_source") in {
                "exchange_fills_reconcile", "execution_journal_recovery"}
            and trade_raw.get("ts_source") == "trusted_internal_override"
            and event_ord_id in _ord_ids(trade_raw)
            and trade_raw.get("close_ts")
        ):
            authoritative_ts = trade_raw["close_ts"]
        try:
            trade_ts = parse_cst(authoritative_ts).strftime(TS_FMT)
        except (TypeError, ValueError):
            continue
        trade_px = _finite_float(trade["fill_px"])
        trade_sz = _finite_float(trade["sz"])
        if trade_px is None or trade_px <= 0 or trade_sz is None or trade_sz <= 0:
            continue
        if trade_ts != closed_at or abs(trade_px - event_px) > max(
                1e-10, abs(event_px) * 1e-10):
            continue
        if not event_ord_identity <= _ord_ids(trade_raw):
            continue
        matches.append(trade)
    if len(matches) != 1:
        return None, ["authoritative_live_trade_not_unique_or_missing"]
    trade = matches[0]
    trade_raw = _raw_object(trade["raw"]) or {}
    ord_ids = sorted(_ord_ids(trade_raw))
    if not event_ord_identity <= set(ord_ids):
        return None, ["authoritative_order_identity_mismatch"]
    return {
        "trade_row_id": int(trade["id"]),
        "cycle_id": str(trade["cycle_id"]),
        "ord_id": event_ord_id,
        "ts": closed_at,
        "px": float(trade["fill_px"]),
        "trade_fill_sz": float(trade["sz"]),
        "experience_consumed_sz": float(event_sz),
        "action": str(trade["action"]).lower(),
    }, []


def _post_exit_bars(
    row: sqlite3.Row,
    exit_fill: dict[str, Any],
    market_connection: sqlite3.Connection,
    bar_cache: dict[tuple[str, str, str], list[sqlite3.Row]] | None = None,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    exit_ts = parse_cst(exit_fill["ts"])
    first_cst = _ceil_15m(exit_ts)
    first_utc = first_cst.astimezone(timezone.utc)
    end_utc = first_utc + timedelta(
        minutes=15 * REQUIRED_COUNTERFACTUAL_15M_BARS)
    cache_key = (str(row["symbol"]), _utc_z(first_utc), _utc_z(end_utc))
    rows = bar_cache.get(cache_key) if bar_cache is not None else None
    if rows is None:
        rows = market_connection.execute(
            "SELECT ts,o,h,l,c FROM kline_cache WHERE symbol=? AND tf='15m' "
            "AND ts>=? AND ts<? ORDER BY ts",
            cache_key,
        ).fetchall()
        if bar_cache is not None:
            bar_cache[cache_key] = rows
    if len(rows) != REQUIRED_COUNTERFACTUAL_15M_BARS:
        return None, ["post_exit_bar_count_not_exactly_16"]
    expected = [
        first_utc + timedelta(minutes=15 * index)
        for index in range(REQUIRED_COUNTERFACTUAL_15M_BARS)
    ]
    bars: list[dict[str, Any]] = []
    for index, bar in enumerate(rows):
        try:
            ts = _parse_utc(bar["ts"])
        except (TypeError, ValueError, OverflowError):
            return None, ["post_exit_bar_invalid"]
        try:
            raw_numbers = {
                key: float(bar[key]) for key in ("o", "h", "l", "c")}
        except (TypeError, ValueError, OverflowError):
            return None, ["post_exit_bar_invalid"]
        if not all(math.isfinite(value) for value in raw_numbers.values()):
            return None, ["post_exit_bar_non_finite"]
        numbers = raw_numbers
        values = {"ts": _utc_z(ts), **numbers}
        if ts != expected[index]:
            return None, ["post_exit_bars_not_exact_contiguous_window"]
        if not (
            values["o"] > 0 and values["h"] > 0 and values["l"] > 0
            and values["c"] > 0
            and values["h"] >= max(values["o"], values["l"], values["c"])
            and values["l"] <= min(values["o"], values["h"], values["c"])
        ):
            return None, ["post_exit_bar_invalid"]
        bars.append(values)
    return bars, []


def _counterfactual_snapshot(
    row: sqlite3.Row,
    live_connection: sqlite3.Connection,
    market_connection: sqlite3.Connection,
    trade_cache: dict[tuple[str, str], list[sqlite3.Row]] | None = None,
    bar_cache: dict[tuple[str, str, str], list[sqlite3.Row]] | None = None,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    plan, state, reasons = _original_plan(row)
    if state != "eligible":
        return None, state, reasons
    exit_fill, fill_reasons = _authoritative_exit_fill(
        row, live_connection, trade_cache)
    if exit_fill is None:
        return None, "blocked", fill_reasons
    bars, bar_reasons = _post_exit_bars(
        row, exit_fill, market_connection, bar_cache)
    if bars is None:
        return None, "blocked", bar_reasons
    snapshot = {
        "method_version": COUNTERFACTUAL_EVIDENCE_METHOD_VERSION,
        "experience_identity": {
            "experience_id": int(row["id"]),
            "symbol": str(row["symbol"]),
            "side": str(row["side"]).lower(),
            "closed_at": parse_cst(row["closed_at"]).strftime(TS_FMT),
        },
        "original_plan": plan,
        "exit_fill": exit_fill,
        "bars_15m": bars,
    }
    return snapshot, "eligible", []


def _missed_take_profit_from_connections(
    account_connection: sqlite3.Connection,
    live_connection: sqlite3.Connection,
    market_connection: sqlite3.Connection,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    """Build the true post-exit counterfactual from authoritative sources."""
    columns = {str(row[1]) for row in account_connection.execute(
        "PRAGMA table_info(trade_experiences)").fetchall()}
    raw_expr = "raw" if "raw" in columns else "NULL AS raw"
    # ts（开仓时刻）是 legacy exit_mode 判据的唯一依据。隔离夹具与早期
    # 库未必有这一列 —— 直接 SELECT 会抛 OperationalError，既把整步打成
    # rc=2，又让 compute() 已开的连接漏掉（Windows 上表现为 tempdir 清理
    # WinError 32）。缺列时选 NULL，判据侧自然 fail-closed。
    ts_expr = "ts" if "ts" in columns else "NULL AS ts"
    source_counts = account_connection.execute(
        "SELECT COUNT(*) AS total,"
        "SUM(CASE WHEN COALESCE(profile,'')!='live' THEN 1 ELSE 0 END) "
        "AS non_live,"
        "SUM(CASE WHEN profile='live' "
        "AND COALESCE(action,'')!='open' THEN 1 ELSE 0 END) "
        "AS fallback "
        "FROM trade_experiences WHERE status='closed' "
        "AND closed_at IS NOT NULL AND closed_at>=? AND closed_at<?",
        (candidate_start, candidate_end),
    ).fetchone()
    rows = account_connection.execute(
        "SELECT id,profile,symbol,side," + ts_expr + ",closed_at,"
        + raw_expr + " "
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
        if parse_cst(row["closed_at"]) < parse_cst(COUNTERFACTUAL_ACTIVATION_TS):
            pre_activation += 1
            continue
        snapshot, state, reasons = _counterfactual_snapshot(
            row, live_connection, market_connection, trade_cache, bar_cache)
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
                "experience_id": int(row["id"]),
                "symbol": str(row["symbol"]),
                "side": str(row["side"]),
                "closed_at": str(row["closed_at"]),
                "reasons": sorted(set(reasons)),
            })
            continue
        side = str(row["side"] or "").lower()
        plan = snapshot["original_plan"]
        fill = snapshot["exit_fill"]
        bars = snapshot["bars_15m"]
        target = float(plan["target_px"])
        exit_px = float(fill["px"])
        if side == "long":
            exited_before_target = exit_px < target
            reached_after_exit = any(
                float(bar["h"]) >= target for bar in bars)
        elif side == "short":
            exited_before_target = exit_px > target
            reached_after_exit = any(
                float(bar["l"]) <= target for bar in bars)
        else:
            reason_counts["side_invalid"] = reason_counts.get(
                "side_invalid", 0) + 1
            blocking_items.append({
                "experience_id": int(row["id"]),
                "symbol": str(row["symbol"]),
                "side": side,
                "closed_at": str(row["closed_at"]),
                "reasons": ["side_invalid"],
            })
            continue
        missed = exited_before_target and reached_after_exit
        snapshot_hash = _canonical_snapshot_sha256(snapshot)
        item = {
            "experience_id": int(row["id"]),
            "symbol": str(row["symbol"]),
            "side": side,
            "closed_at": str(row["closed_at"]),
            "exit_mode": plan["exit_mode"],
            "target_px": target,
            "exit_px": exit_px,
            "post_exit_target_reached": reached_after_exit,
            "classification": "missed_take_profit" if missed else "not_missed",
            "source_snapshot_sha256": snapshot_hash,
            "source_snapshot": snapshot,
        }
        evaluated_items.append(item)
        if missed:
            pool.append(item)
    evaluated = len(evaluated_items)
    post_activation = len(rows) - pre_activation
    unknown = len(blocking_items)
    upstream_status = "BLOCKED" if blocking_items else "READY"
    if blocking_items:
        status = "BLOCKED"
    elif post_activation == 0:
        status = "NOT_ACTIVATED_WINDOW"
    elif fixed_tp_candidates == 0:
        status = "NOT_APPLICABLE"
    else:
        status = "COMPLETE"
    return {
        "method_version": MISSED_TAKE_PROFIT_METHOD_VERSION,
        "evidence_method_version": COUNTERFACTUAL_EVIDENCE_METHOD_VERSION,
        "counterfactual_activation_cst": COUNTERFACTUAL_ACTIVATION_TS,
        "upstream_status": upstream_status,
        "status": status,
        "required_15m_bars": REQUIRED_COUNTERFACTUAL_15M_BARS,
        "outcome_horizon_hours": OUTCOME_HORIZON_HOURS,
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


def missed_take_profit(
    account_db: Path,
    live_trades_db: Path,
    market_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    """Open every source read-only and close it on every success/error path."""
    connections: list[sqlite3.Connection] = []
    try:
        for path in (account_db, live_trades_db, market_db):
            connections.append(_ro(path))
        return _missed_take_profit_from_connections(
            connections[0], connections[1], connections[2],
            candidate_start, candidate_end)
    finally:
        for connection in reversed(connections):
            connection.close()


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        parts: list[str] = []
        for child in value.values():
            parts.extend(_strings(child))
        return parts
    if isinstance(value, list):
        parts = []
        for child in value:
            parts.extend(_strings(child))
        return parts
    return []


def _mentions_symbol(text: str, symbol: str) -> bool:
    upper = text.upper()
    symbol_upper = symbol.upper()
    return bool(symbol_upper and re.search(
        rf"(?<![A-Z0-9]){re.escape(symbol_upper)}(?![A-Z0-9])", upper))


def _explicit_position_review(
    payload: dict[str, Any], symbol: str, *, cycle_id: str,
) -> bool:
    """Require a same-position review record or an action-bearing judgement."""
    candidates = [payload.get("position_reviews")]
    card = payload.get("decision_card")
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
            if not isinstance(item, dict) or _item_symbol(item) != symbol.upper():
                continue
            action = _disposition_for_action(_item_action(item))
            conclusion = str(
                item.get("conclusion") or item.get("agent_judgement")
                or item.get("reasoning") or "").strip()
            if action in set(ACTION_LAYER_KEYS) | {"hold"} and conclusion:
                return True
    judgement = (
        str(card.get("agent_judgement") or "") if isinstance(card, dict)
        else "")
    for clause in re.split(r"[\n；;。，,]+", judgement):
        if not _mentions_symbol(clause, symbol):
            continue
        if re.search(
            r"(?<![A-Z])(?:HOLD|CLOSE|REDUCE|ADJUST(?:_|\s+)PROTECTION|ADD|OPEN)"
            r"(?![A-Z])",
            clause.upper(),
        ):
            return True
    if (
        thresholds.structured_position_actions_count_as_review(cycle_id)
        and _structured_position_action_review(payload, symbol)
    ):
        return True
    return False


def _item_symbol(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    nested = value.get("request")
    if isinstance(nested, dict):
        nested_symbol = _item_symbol(nested)
        if nested_symbol:
            return nested_symbol
    return str(
        value.get("instId") or value.get("symbol")
        or value.get("instrument") or "").upper()


def _item_action(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    nested = value.get("request")
    if isinstance(nested, dict):
        nested_action = _item_action(nested)
        if nested_action:
            return nested_action
    return str(
        value.get("action") or value.get("type")
        or value.get("requested_action") or "").strip().lower()


def _item_review_reason(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("reason", "reasoning", "conclusion", "agent_judgement"):
        reason = str(value.get(key) or "").strip()
        if reason:
            return reason
    nested = value.get("request")
    return _item_review_reason(nested) if isinstance(nested, dict) else ""


def _structured_position_action_review(
    payload: dict[str, Any], symbol: str,
) -> bool:
    """Require exact symbol, recognized action and an Agent-authored reason."""
    allowed = set(ACTION_LAYER_KEYS) | {"hold"}
    for key in (
        "requested_position_actions",
        "position_action_results",
        "position_action_failures",
    ):
        for item in payload.get(key) or []:
            if not isinstance(item, dict) or _item_symbol(item) != symbol.upper():
                continue
            action = _disposition_for_action(_item_action(item))
            if action in allowed and _item_review_reason(item):
                return True
    return False


def _successful_result(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("ok") is True or value.get("success") is True:
        return True
    if str(value.get("status") or "").lower() in {
        "ok", "success", "succeeded", "completed", "applied", "filled"
    }:
        return True
    nested = value.get("result")
    if isinstance(nested, dict) and _successful_result(nested):
        return True
    return value.get("problem") is None and bool(
        value.get("trade_identities") or value.get("algo_identities"))


def _failed_result(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("ok") is False or value.get("success") is False:
        return True
    if str(value.get("status") or "").strip().lower() in {
        "error", "failed", "failure", "rejected", "blocked", "cancelled",
        "canceled", "timeout",
    }:
        return True
    if value.get("problem") not in (None, "") or value.get("error") not in (None, ""):
        return True
    nested = value.get("result")
    return isinstance(nested, dict) and _failed_result(nested)


def _disposition_for_action(action: str) -> str | None:
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


def _action_layers(
    payload: dict[str, Any], symbol: str, fill_actions: set[str],
) -> dict[str, list[str]]:
    matching_requests = [
        item for item in (payload.get("requested_position_actions") or [])
        if isinstance(item, dict) and _item_symbol(item) == symbol.upper()
    ]
    matching_results = [
        item for item in (payload.get("position_action_results") or [])
        if isinstance(item, dict) and _item_symbol(item) == symbol.upper()
    ]
    matching_failures = [
        item for item in (payload.get("position_action_failures") or [])
        if isinstance(item, dict) and _item_symbol(item) == symbol.upper()
    ]

    def classified(rows: list[dict[str, Any]]) -> list[str]:
        return sorted({
            action for action in (
                _disposition_for_action(_item_action(item)) for item in rows)
            if action in ACTION_LAYER_KEYS
        })

    successful_rows = [
        item for item in matching_results if _successful_result(item)]
    failed_rows = list(matching_failures) + [
        item for item in matching_results if _failed_result(item)]
    return {
        "requested": classified(matching_requests),
        "succeeded": classified(successful_rows),
        "fills": sorted({
            action for action in (
                _disposition_for_action(item) for item in fill_actions)
            if action in ACTION_LAYER_KEYS
        }),
        "failed": classified(failed_rows),
    }


def _cycle_disposition(
    payload: dict[str, Any],
    symbol: str,
    fill_actions: set[str],
) -> str:
    """Separate successful actions, failed attempts, and a true empty HOLD."""
    layers = _action_layers(payload, symbol, fill_actions)
    fill_dispositions = {
        mapped for mapped in (
            _disposition_for_action(action) for action in fill_actions)
        if mapped in DISPOSITION_PRIORITY
    }
    for mapped in DISPOSITION_PRIORITY:
        if mapped in fill_dispositions:
            return mapped
    succeeded = set(layers["succeeded"])
    for mapped in DISPOSITION_PRIORITY:
        if mapped in succeeded:
            return mapped
    if layers["failed"]:
        return "attempted_failed"
    has_unconfirmed = any(
        isinstance(item, dict) and _item_symbol(item) == symbol.upper()
        for key in ("requested_position_actions", "position_action_results")
        for item in (payload.get(key) or [])
    )
    if has_unconfirmed:
        return "requested_unconfirmed"
    return "hold"


def _fills_for_cycles(
    connection: sqlite3.Connection,
    cycle_ids: list[str],
) -> dict[tuple[str, str], set[str]]:
    actions: dict[tuple[str, str], set[str]] = {}
    for offset in range(0, len(cycle_ids), 500):
        chunk = cycle_ids[offset:offset + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            "SELECT cycle_id,symbol,action FROM trades WHERE cycle_id IN ("
            f"{placeholders})", chunk).fetchall()
        for row in rows:
            key = (str(row["cycle_id"]), str(row["symbol"]).upper())
            actions.setdefault(key, set()).add(str(row["action"] or "").lower())
    return actions


def margin_return_review(
    live_trades_db: Path,
    candidate_start: str,
    candidate_end: str,
) -> dict[str, Any]:
    """Audit >=50% margin-return review only after its real fact boundary."""
    candidate_start_cycle = _cycle_boundary(candidate_start)
    candidate_end_cycle = _cycle_boundary(candidate_end)
    effective_start_cycle = max(
        candidate_start_cycle, MARGIN_FACT_ACTIVATION_CYCLE)
    connection = _ro(live_trades_db)
    try:
        source_candidate_count = int(connection.execute(
            "SELECT COUNT(*) FROM trade_cycles WHERE cycle_id>=? AND cycle_id<?",
            (candidate_start_cycle, candidate_end_cycle),
        ).fetchone()[0])
        candidate_count = int(connection.execute(
            "SELECT COUNT(*) FROM trade_cycles WHERE mode='live' "
            "AND cycle_id>=? AND cycle_id<?",
            (candidate_start_cycle, candidate_end_cycle),
        ).fetchone()[0])
        cycles = connection.execute(
            "SELECT cycle_id,raw FROM trade_cycles "
            "WHERE mode='live' AND cycle_id>=? AND cycle_id<? "
            "ORDER BY cycle_id",
            (effective_start_cycle, candidate_end_cycle),
        ).fetchall() if effective_start_cycle < candidate_end_cycle else []
        cycle_ids = [str(row["cycle_id"]) for row in cycles]
        fills = _fills_for_cycles(connection, cycle_ids)
    finally:
        connection.close()

    dispositions = {key: 0 for key in DISPOSITION_KEYS}
    action_layer_counts = {
        layer: {key: 0 for key in ACTION_LAYER_KEYS}
        for layer in ("requested", "succeeded", "fills", "failed")
    }
    flagged = reviewed = unreadable_cycles = unknown_cycles = 0
    structured_action_reviewed = 0
    structured_semantics_active_flagged = 0
    legacy_semantics_flagged = 0
    observed_unflagged = unknown_positions = disagreements = 0
    excluded_non_open_positions = 0
    total_position_cycles = 0
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
        live_facts = payload.get("live_facts")
        positions = (
            live_facts.get("positions") if isinstance(live_facts, dict)
            else None)
        if not isinstance(positions, list):
            unknown_cycles += 1
            continue
        cycle_id = str(row["cycle_id"])
        for position in positions:
            if not isinstance(position, dict):
                continue
            symbol = str(
                position.get("instId") or position.get("symbol") or "").upper()
            if not symbol:
                continue
            contracts = _finite_float(position.get("contracts"))
            if contracts is None or contracts <= 0:
                excluded_non_open_positions += 1
                continue
            total_position_cycles += 1
            flag_present = "margin_return_review_at_or_above_50pct" in position
            upl_value = position.get("upl_ratio_initial_margin")
            upl = _finite_float(upl_value)
            upl_known = upl is not None
            if not flag_present or not upl_known:
                unknown_positions += 1
                continue
            flag = position.get("margin_return_review_at_or_above_50pct") is True
            recomputed = bool(upl is not None and upl >= MARGIN_REVIEW_THRESHOLD)
            if flag != recomputed:
                disagreements += 1
                unknown_positions += 1
                continue
            if not flag:
                observed_unflagged += 1
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
                and _structured_position_action_review(payload, symbol)
            )
            explicit = _explicit_position_review(
                payload, symbol, cycle_id=cycle_id)
            if explicit:
                reviewed += 1
            if structured_review:
                structured_action_reviewed += 1
            layers = _action_layers(
                payload, symbol, fills.get((cycle_id, symbol), set()))
            for layer, actions in layers.items():
                for action in actions:
                    action_layer_counts[layer][action] += 1
            disposition = _cycle_disposition(
                payload, symbol, fills.get((cycle_id, symbol), set()))
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

    known_positions = flagged + observed_unflagged
    return {
        "threshold_fraction": MARGIN_REVIEW_THRESHOLD,
        "fact_activation_cycle": MARGIN_FACT_ACTIVATION_CYCLE,
        "candidate_cycle_window": {
            "start_cycle": candidate_start_cycle,
            "end_cycle": candidate_end_cycle,
            "end_exclusive": True,
        },
        "effective_cycle_window": {
            "start_cycle": effective_start_cycle,
            "end_cycle": candidate_end_cycle,
            "end_exclusive": True,
        },
        "candidate_cycle_rows": candidate_count,
        "source_candidate_cycle_rows": source_candidate_count,
        "excluded_non_live_cycle_rows": source_candidate_count - candidate_count,
        "eligible_cycle_rows": len(cycles),
        "pre_activation_excluded_cycle_rows": candidate_count - len(cycles),
        "unreadable_cycle_rows": unreadable_cycles,
        "unknown_position_list_cycle_rows": unknown_cycles,
        "excluded_non_open_position_rows": excluded_non_open_positions,
        "total_position_cycles": total_position_cycles,
        "fact_observed_position_cycles": known_positions,
        "unknown_fact_position_cycles": unknown_positions,
        "fact_coverage_rate": (
            round(known_positions / total_position_cycles, 6)
            if total_position_cycles else None),
        "flag_value_disagreements": disagreements,
        "unflagged_position_cycles": observed_unflagged,
        "flagged_position_cycles": flagged,
        "explicitly_reviewed": reviewed,
        "structured_action_reviewed": structured_action_reviewed,
        "structured_semantics_active_flagged": (
            structured_semantics_active_flagged),
        "legacy_semantics_flagged": legacy_semantics_flagged,
        "explicit_review_rate": round(reviewed / flagged, 6) if flagged else None,
        "explicit_review_semantics_migration": (
            thresholds.structured_position_review_migration_facts(
                candidate_end_cycle)),
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


def compute(
    *,
    account_db: Path,
    live_trades_db: Path,
    market_db: Path,
    report_start_ts: str,
    report_end_ts: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Compute one immutable daily artifact payload from read-only sources."""
    report_start = parse_cst(report_start_ts).strftime(TS_FMT)
    report_end = parse_cst(report_end_ts).strftime(TS_FMT)
    candidate_start, candidate_end = candidate_window(report_start, report_end)
    peak = peak_giveback(account_db, candidate_start, candidate_end)
    return {
        "schema_version": SCHEMA_VERSION,
        "version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "business_date": report_end[:10],
        "generated_at": generated_at or datetime.now(CST).strftime(TS_FMT),
        "report_activation_cst": REPORT_ACTIVATION_TS,
        "margin_fact_activation_cycle": MARGIN_FACT_ACTIVATION_CYCLE,
        "counterfactual_activation_cst": COUNTERFACTUAL_ACTIVATION_TS,
        "outcome_horizon_hours": OUTCOME_HORIZON_HOURS,
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
        "margin_return_review": margin_return_review(
            live_trades_db, report_start, report_end),
        "missed_take_profit": missed_take_profit(
            account_db, live_trades_db, market_db,
            candidate_start, candidate_end),
        "safety": {
            "production_database_writes": 0,
            "cycles_replayed": 0,
            "window_extended": False,
            "orders_placed": 0,
        },
    }


def frozen_artifact_errors(
    payload: object,
    *,
    business_date: str,
    report_start_ts: str | None = None,
    report_end_ts: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["artifact root must be an object"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version differs")
    if payload.get("method_version") != METHOD_VERSION:
        errors.append("method_version differs")
    if payload.get("business_date") != business_date:
        errors.append("business_date differs")
    if payload.get("report_activation_cst") != REPORT_ACTIVATION_TS:
        errors.append("report activation differs")
    if payload.get("margin_fact_activation_cycle") != MARGIN_FACT_ACTIVATION_CYCLE:
        errors.append("margin fact activation differs")
    if payload.get("counterfactual_activation_cst") != COUNTERFACTUAL_ACTIVATION_TS:
        errors.append("counterfactual activation differs")
    report_window = payload.get("report_window") or {}
    if report_start_ts is not None and report_window.get("start_ts") != report_start_ts:
        errors.append("report window start differs")
    if report_end_ts is not None and report_window.get("end_ts") != report_end_ts:
        errors.append("report window end differs")
    frozen_end = report_end_ts or report_window.get("end_ts")
    try:
        generated = parse_cst(payload.get("generated_at"))
        if frozen_end and generated < parse_cst(frozen_end):
            errors.append("generated_at precedes closed report window")
    except (TypeError, ValueError):
        errors.append("generated_at invalid")
    candidate_start = report_start_ts or report_window.get("start_ts")
    candidate_end = report_end_ts or report_window.get("end_ts")
    if candidate_start and candidate_end:
        expected_candidate = candidate_window(candidate_start, candidate_end)
        candidate = payload.get("candidate_window") or {}
        if (candidate.get("start_ts"), candidate.get("end_ts")) != expected_candidate:
            errors.append("candidate window differs")
        peak = payload.get("peak_giveback") or {}
        expected_peak_start = max(
            expected_candidate[0], PEAK_GIVEBACK_ACTIVATION_TS)
        expected_peak_status = (
            "PENDING" if expected_peak_start >= expected_candidate[1]
            else "COMPLETE")
        if not (
            isinstance(peak, dict)
            and peak.get("method_version") == PEAK_GIVEBACK_METHOD_VERSION
            and peak.get("fact_activation_cst") == PEAK_GIVEBACK_ACTIVATION_TS
            and peak.get("status") == expected_peak_status
            and (peak.get("effective_window") or {}).get("start_ts")
            == expected_peak_start
            and (peak.get("effective_window") or {}).get("end_ts")
            == expected_candidate[1]
            and (peak.get("effective_window") or {}).get("end_exclusive") is True
        ):
            errors.append("peak giveback forward contract differs")
    missed = payload.get("missed_take_profit")
    if not isinstance(missed, dict) or missed.get(
            "method_version") != MISSED_TAKE_PROFIT_METHOD_VERSION:
        errors.append("missed take profit method missing")
    elif (
        missed.get("evidence_method_version")
        != COUNTERFACTUAL_EVIDENCE_METHOD_VERSION
        or missed.get("counterfactual_activation_cst")
        != COUNTERFACTUAL_ACTIVATION_TS
        or missed.get("upstream_status") != "READY"
    ):
        errors.append("missed take profit upstream contract differs")
    safety = payload.get("safety") or {}
    if safety != {
        "production_database_writes": 0,
        "cycles_replayed": 0,
        "window_extended": False,
        "orders_placed": 0,
    }:
        errors.append("safety contract differs")
    return errors


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8")


def atomic_write_once_json(path: Path, payload: dict[str, Any]) -> tuple[bytes, bool]:
    """Atomically publish once; an existing artifact is never replaced."""
    path = Path(path)
    if path.exists():
        return path.read_bytes(), False
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        # A second compliant maintenance process may overlap the first. Wait
        # only for the bounded atomic publication; a stale lock still fails
        # closed and never grants permission to replace the destination.
        for _ in range(50):
            if path.exists():
                return path.read_bytes(), False
            time.sleep(0.1)
        raise RuntimeError(f"artifact writer lock exists: {lock_path}") from exc
    temp_path: Path | None = None
    try:
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.close(lock_fd)
        if path.exists():
            return path.read_bytes(), False
        raw = _json_bytes(payload)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Same-directory hard-link creation is atomic and fails when the
            # destination already exists. Unlike os.replace(), this is a true
            # write-once publish primitive with no TOCTOU overwrite window.
            os.link(temp_path, path)
        except FileExistsError:
            return path.read_bytes(), False
        return raw, True
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def _load_existing(
    path: Path,
    *,
    business_date: str,
    report_start: str,
    report_end: str,
) -> bytes:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    errors = frozen_artifact_errors(
        payload,
        business_date=business_date,
        report_start_ts=report_start,
        report_end_ts=report_end,
    )
    if errors:
        raise ValueError("existing artifact invalid: " + "; ".join(errors))
    return raw


def _wait_for_closed_report_window(
    report_end: str,
    maximum_wait_seconds: int,
) -> None:
    """Never freeze a business window before its right-open boundary closes."""
    deadline = parse_cst(report_end)
    remaining = (deadline - datetime.now(CST)).total_seconds()
    if remaining <= 0:
        return
    if remaining > max(0, int(maximum_wait_seconds)):
        raise RuntimeError(
            "report window is still open; refusing future-window freeze "
            f"(remaining_seconds={remaining:.3f})")
    while True:
        remaining = (deadline - datetime.now(CST)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="publish one frozen read-only exit-quality artifact")
    parser.add_argument("--as-of", default=default_daily_as_of())
    parser.add_argument("--db-root", default=str(DEFAULT_DB_ROOT))
    parser.add_argument("--account-db")
    parser.add_argument("--live-trades-db")
    parser.add_argument("--market-db")
    parser.add_argument("--quality-dir", default=str(DEFAULT_QUALITY_DIR))
    parser.add_argument("--out-file")
    parser.add_argument(
        "--window-close-wait-seconds", type=int,
        default=DEFAULT_WINDOW_CLOSE_WAIT_SECONDS,
    )
    args = parser.parse_args(argv)

    try:
        report_start, report_end = daily_report_window(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    business_date = report_end[:10]
    if report_end < REPORT_ACTIVATION_TS:
        print(json.dumps({
            "status": "not_activated",
            "business_date": business_date,
            "report_activation_cst": REPORT_ACTIVATION_TS,
        }, ensure_ascii=False))
        return 0

    root = Path(args.db_root)
    account_db = Path(args.account_db) if args.account_db else root / "account.db"
    live_db = (
        Path(args.live_trades_db) if args.live_trades_db
        else root / "live_trades.db")
    market_db = Path(args.market_db) if args.market_db else root / "market.db"
    output = (
        Path(args.out_file) if args.out_file else
        Path(args.quality_dir) / f"exit_quality_{business_date}.json")
    try:
        _wait_for_closed_report_window(
            report_end, args.window_close_wait_seconds)
        if output.exists():
            raw = _load_existing(
                output,
                business_date=business_date,
                report_start=report_start,
                report_end=report_end,
            )
            created = False
        else:
            for path in (account_db, live_db, market_db):
                if not path.exists():
                    raise FileNotFoundError(path)
            payload = compute(
                account_db=account_db,
                live_trades_db=live_db,
                market_db=market_db,
                report_start_ts=report_start,
                report_end_ts=report_end,
            )
            missed = payload.get("missed_take_profit") or {}
            if missed.get("upstream_status") != "READY":
                raise RuntimeError(
                    "missed_take_profit upstream blocked: "
                    + json.dumps({
                        "status": missed.get("status"),
                        "unknown_reason_counts": missed.get(
                            "unknown_reason_counts"),
                        "blocking_items": missed.get("blocking_items"),
                    }, ensure_ascii=False, sort_keys=True))
            contract_errors = frozen_artifact_errors(
                payload,
                business_date=business_date,
                report_start_ts=report_start,
                report_end_ts=report_end,
            )
            if contract_errors:
                raise RuntimeError(
                    "computed artifact contract invalid: "
                    + "; ".join(contract_errors))
            raw, created = atomic_write_once_json(output, payload)
            if not created:
                raw = _load_existing(
                    output,
                    business_date=business_date,
                    report_start=report_start,
                    report_end=report_end,
                )
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(json.dumps({
            "status": "blocked",
            "business_date": business_date,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False))
        return 2

    print(json.dumps({
        "status": "created" if created else "already_frozen",
        "business_date": business_date,
        "path": str(output),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "database_writes": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
