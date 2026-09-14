# -*- coding: utf-8 -*-
"""P4 决策简报预处理器（T1，2026-06-12）。

五库汇总 → 紧凑标准简报（~2-3KB），P4 起手一次调用替代多次临场自查。
任何子段失败只标注 N/A，不中断（决策不能因简报缺段而停）。

用法:
  pwsh -NoProfile -File <PROJECT_ROOT>\\scripts\\run_okx_python.ps1 <PROJECT_ROOT>\\scripts\\decision_briefing.py [--db-root <PROJECT_ROOT>\\db] [--top 5] [--out-file <PROJECT_ROOT>\\tmp\\briefing_<stage>.md]

--out-file（2026-07-15）：stdout 照常输出（契约不变），同时把全文写入 UTF-8 文件。
agent exec 环境是 cp936 pwsh——对本脚本输出接管道/捕获（`| tail`/`| Select-Object`/`2>&1 |`）
会被按 GBK 解码坏成 `鍐崇瓥...`；需复读/截断一律 --out-file + read，禁再接管道。
"""


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

import argparse
from bisect import bisect_right
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.risk_validator import MAX_PORTFOLIO_IMR_RATIO  # noqa: E402
from core.multitimeframe_gate import (  # noqa: E402
    MINIMUM_BARS_FOR_FULL_INDICATORS,
    validate_kline_row,
)
from collect_market_features import (  # noqa: E402
    CONTRACT_STATS_DIRECT_METHODS,
    CONTRACT_STATS_PRIMARY_MAX_AGE_S,
    CONTRACT_STATS_SOURCE,
    contract_statistics_row_issues,
    contract_statistics_row_method,
)
from scripts import _acceptance_thresholds as thresholds  # noqa: E402

CST = timezone(timedelta(hours=8))
MIN_QUOTE_VOL_USD = 5_000_000  # 流动性下限：过滤微盘噪音
MIN_OI_USD = 5_000_000         # 可交易候选 OI 下限：过滤成交额虚高但盘口承载不足
TRADEABLE_CANDIDATE_COUNT = 8
EARLY_STRUCTURE_CANDIDATE_COUNT = 8  # 早期结构组：4H 立向、15m 未同向（2026-08-14）
WAIT_STREAK_MIN_DISPLAY = 2    # 连续 wait 轮数达到该值才展示（1 轮是噪音）
WAIT_STREAK_LOOKBACK = 96      # 每标的回看最近 96 条信号（≈24h）判连等
DECISION_TIMEFRAMES = ("15m", "1H", "4H")
POSITIONING_SOURCE = "okx_rest_contract_long_short_ratio"
POSITIONING_MINIMUM_COVERAGE = 0.99
POSITIONING_MAXIMUM_SOURCE_AGE_MINUTES = 90.0
CANDIDATE_MICRO_MAXIMUM_AGE_MINUTES = 30.0
CANDIDATE_FLOW_MAXIMUM_SPAN_MINUTES = 30.0
TIMEFRAME_SECONDS = {"15m": 15 * 60, "1H": 60 * 60, "4H": 4 * 60 * 60}
DXY_OBSERVATION_WINDOW = 20    # DTWEXBGS 是周频；按 source_as_of 取真实观测，不取 carry-forward 日历行
DXY_MIN_OBSERVATIONS = 3       # 仅保证可描述离散度；样本量会原样展示给 Agent 自主权衡
DXY_CARRY_STALE_DAYS = 3       # 本地连续 carry-forward 达该天数后不再输出 zone 档位
PLAYBOOK_HYPOTHESIS_TTL_DAYS = 14
PLAYBOOK_OTHER_TTL_DAYS = 30
REGIME_TOKENS = ("trend_up", "trend_down", "range")
READY_POOL_SCHEMA = "briefing_ready_pool_v1"
READY_POOL_SCHEMA_SIDE_NEUTRAL = "briefing_ready_pool_v2_side_neutral"
OPPORTUNITY_STATE_VERSION = "opportunity_state_v2"
RELAXED_OPPORTUNITY_STATE_VERSION = "opportunity_state_v3_all_timeframes"
SIDE_NEUTRAL_STATE_VERSION = "side_neutral_review_v1"
CANDIDATE_RANK_VERSION = "trend_entry_timing_v1_no_abs_chg24h"
RELAXED_CANDIDATE_RANK_VERSION = "all_market_state_v3_no_liq_oi_gate"
SIDE_NEUTRAL_RANK_VERSION = "side_neutral_round_robin_v1"
SIDE_NEUTRAL_OPPORTUNITY_RANK_VERSION = (
    "side_neutral_opportunity_rotation_v2")
SIDE_NEUTRAL_ROTATION_SLOTS = 2
ENTRY_EXTENSION_ATR_THRESHOLD = 1.5
ENTRY_EXTENSION_RSI_LONG = 70.0
ENTRY_EXTENSION_RSI_SHORT = 30.0
OPPORTUNITY_STATES = (
    "ENTRY_READY", "TRIGGERING", "EARLY_WATCH", "EXTENDED",
    "NON_DIRECTIONAL",
)
OPPORTUNITY_STATE_PRIORITY = {
    "ENTRY_READY": 4,
    "TRIGGERING": 3,
    "EARLY_WATCH": 2,
    "EXTENDED": 1,
    "NON_DIRECTIONAL": 0,
}


def opportunity_state_version_for_cycle(cycle_id):
    if not cycle_id:
        return OPPORTUNITY_STATE_VERSION
    if thresholds.minimal_decision_contract_active(str(cycle_id)):
        return SIDE_NEUTRAL_STATE_VERSION
    if thresholds.decision_restriction_removal_active(str(cycle_id)):
        return RELAXED_OPPORTUNITY_STATE_VERSION
    moment = thresholds.parse_cst(str(cycle_id))
    if moment >= thresholds.parse_cst(
            thresholds.CANDIDATE_OPPORTUNITY_STATE_V2_ACTIVATION_CST):
        return "opportunity_state_v2"
    if moment >= thresholds.parse_cst(
            thresholds.CANDIDATE_OPPORTUNITY_STATE_ACTIVATION_CST):
        return "opportunity_state_v1"
    return None


def _candidate_price_text(value) -> str:
    """Render the candidate's absolute quote without rounding small prices to zero."""
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return "last=N/A"
    if isinstance(value, bool) or not math.isfinite(price) or price <= 0:
        return "last=N/A"
    return f"last={format(Decimal(str(price)), 'f')} USDT"


def _percentile_map(values: dict[str, float | None]) -> dict[str, float]:
    """Return deterministic 0..1 percentile scores; missing stays zero."""
    ordered = sorted(
        float(value) for value in values.values()
        if value is not None and math.isfinite(float(value)))
    if not ordered:
        return {key: 0.0 for key in values}
    denominator = max(len(ordered), 1)
    return {
        key: (
            bisect_right(ordered, float(value)) / denominator
            if value is not None and math.isfinite(float(value)) else 0.0)
        for key, value in values.items()
    }


def held_candidate_disposition(
    symbol: str,
    held_symbols,
    *,
    closure_policy: bool,
) -> tuple[bool, bool]:
    """Return ``(observed_held, exclude_from_manifest)`` for one symbol.

    Historical epochs preserve the prior new-position pool behaviour.  The
    owner-approved closure epoch keeps every otherwise tradable symbol in the
    full manifest; an existing position is observation context for an Agent
    OPEN/ADD choice, never an admission gate.
    """
    lookup = (
        held_symbols
        if isinstance(held_symbols, (set, frozenset))
        else set(held_symbols or ()))
    held = str(symbol) in lookup
    return held, bool(held and not closure_policy)


def rank_side_neutral_opportunities(
    candidates,
    *,
    cycle_id,
    micro_map=None,
    review_limit=8,
    rotation_slots=SIDE_NEUTRAL_ROTATION_SLOTS,
):
    """Rank the full side-neutral universe without creating a trade gate.

    Every candidate remains in the manifest.  Most review capacity follows a
    direction-free opportunity/execute-observability score; a small rotating
    share preserves coverage.  Volume, OI, catalyst absence, current position
    count, and unused IMR are never eligibility thresholds here.
    """
    rows = list(candidates or [])
    if not rows:
        return [], []
    micro_map = micro_map if isinstance(micro_map, dict) else {}

    def finite(value, default=None):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    def mvalue(row, key):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None

    by_symbol = {str(item["row"]["symbol"]): item for item in rows}
    turnover = {
        symbol: max(finite(item.get("quote_vol"), 0.0), 0.0)
        for symbol, item in by_symbol.items()
    }
    oi = {
        symbol: max(
            finite(item["row"].get("candidate_oi_usd"), 0.0), 0.0)
        for symbol, item in by_symbol.items()
    }
    dislocation = {
        symbol: min(abs(finite(item["row"].get("chg24h"), 0.0)), 30.0)
        for symbol, item in by_symbol.items()
    }
    funding = {
        symbol: min(
            abs(finite(item["row"].get("funding_rate"), 0.0)), 0.01)
        for symbol, item in by_symbol.items()
    }
    turnover_pct = _percentile_map(turnover)
    oi_pct = _percentile_map(oi)
    dislocation_pct = _percentile_map(dislocation)
    funding_pct = _percentile_map(funding)

    for symbol, item in by_symbol.items():
        micro = micro_map.get(symbol)
        spread = finite(mvalue(micro, "spread_bps"))
        buy_slip = finite(mvalue(micro, "buy_slippage_500usd_bps"))
        sell_slip = finite(mvalue(micro, "sell_slippage_500usd_bps"))
        imbalance = finite(mvalue(micro, "imbalance_25bp"))
        taker_buy = finite(mvalue(micro, "taker_buy_ratio"))
        cvd = finite(mvalue(micro, "cvd_notional_usd"))
        micro_available = spread is not None and imbalance is not None
        if micro_available:
            slip_values = [
                value for value in (buy_slip, sell_slip)
                if value is not None]
            average_slip = (
                sum(slip_values) / len(slip_values)
                if slip_values else spread)
            spread_quality = 1.0 / (1.0 + max(spread, 0.0) / 5.0)
            slip_quality = 1.0 / (1.0 + max(average_slip, 0.0) / 5.0)
            micro_execution = (spread_quality + slip_quality) / 2.0
            signals = [min(abs(imbalance), 1.0)]
            if taker_buy is not None:
                signals.append(min(abs(taker_buy - 0.5) * 2.0, 1.0))
            if cvd is not None:
                signals.append(abs(cvd) / (abs(cvd) + 50_000.0))
            micro_signal = sum(signals) / len(signals)
        else:
            micro_execution = 0.0
            micro_signal = 0.0
        history = item.get("dig_history") or {}
        prior_reviews = max(int(history.get("n") or 0), 0)
        rotation_score = 1.0 / (1.0 + prior_reviews)
        execution_observability = (
            0.45 * turnover_pct[symbol]
            + 0.35 * oi_pct[symbol]
            + 0.20 * micro_execution)
        components = {
            "execution_observability": round(execution_observability, 6),
            "price_dislocation": round(dislocation_pct[symbol], 6),
            "funding_dislocation": round(funding_pct[symbol], 6),
            "micro_signal": round(micro_signal, 6),
            "rotation": round(rotation_score, 6),
            "timeframe_judgment_used": False,
            "hard_liquidity_or_oi_gate_used": False,
        }
        score = (
            0.35 * execution_observability
            + 0.25 * dislocation_pct[symbol]
            + 0.15 * funding_pct[symbol]
            + 0.15 * micro_signal
            + 0.10 * rotation_score)
        item["opportunity_score"] = round(score, 8)
        item["opportunity_score_components"] = components
        item["selection_reason"] = "not_selected_this_slot"
        item["rank_version"] = SIDE_NEUTRAL_OPPORTUNITY_RANK_VERSION
        item["rank_key"] = (score, symbol)

    ordered = sorted(
        rows,
        key=lambda item: (
            float(item.get("opportunity_score") or 0.0),
            str(item["row"]["symbol"])),
        reverse=True,
    )
    limit = min(max(int(review_limit or 0), 0), len(ordered))
    rotation_n = min(max(int(rotation_slots or 0), 0), limit)
    priority_n = max(limit - rotation_n, 0)
    reviewed = list(ordered[:priority_n])
    selected_symbols = {str(item["row"]["symbol"]) for item in reviewed}
    alphabetical = sorted(ordered, key=lambda item: str(item["row"]["symbol"]))
    slot = int(
        datetime.strptime(str(cycle_id), "%Y-%m-%dT%H:%M")
        .replace(tzinfo=CST).timestamp() // (15 * 60))
    offset = (slot * max(rotation_n, 1)) % len(alphabetical)
    rotated = alphabetical[offset:] + alphabetical[:offset]
    for item in rotated:
        symbol = str(item["row"]["symbol"])
        if symbol in selected_symbols:
            continue
        reviewed.append(item)
        selected_symbols.add(symbol)
        if len(reviewed) >= limit:
            break
    priority_symbols = {
        str(item["row"]["symbol"]) for item in ordered[:priority_n]}
    for ordinal, item in enumerate(reviewed, start=1):
        symbol = str(item["row"]["symbol"])
        item["selection_reason"] = (
            "opportunity_priority" if symbol in priority_symbols
            else "rotation_coverage")
        item["review_ordinal"] = ordinal
    return ordered, reviewed
EXIT_QUALITY_SCHEMA_VERSION = 2
EXIT_QUALITY_METHOD_VERSION = "exit_quality_v2_forward_frozen"
EXIT_QUALITY_REPORT_ACTIVATION_TS = "2026-08-16 08:00:00"
# 2026-08-19 G1：净 R 口径起用 v2；此处是**消费/校验**侧，接受 v1|v2，
# 边界前归档的 v1 工件继续通过（历史不反向加责）。
EXIT_QUALITY_PEAK_METHOD_VERSION = "peak_giveback_forward_v2"
EXIT_QUALITY_PEAK_METHOD_VERSIONS_ACCEPTED = (
    "peak_giveback_forward_v1", "peak_giveback_forward_v2")
EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE = "2026-08-15T14:45"
EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD = (
    "authoritative_exit_fill_market_16x15m_v1")
_CALIBRATION_USAGE_ORDER = ("adopt", "partial", "ignore", "none", "unknown")
_CALIBRATION_HOLD_ORDER = ("<4h", "4-24h", "24-48h", ">=48h", "unknown")
# 2026-08-17 亏损复盘增补两组描述性分层（只描述过去，不形成阈值）：
#   开仓时段（UTC+8 四小时桶）——复盘中 00–11h 开仓 48 笔仅 3 胜、12–19h 22 笔 6 胜；
#   regime×顺逆势——side 与交易时冻结的 trend_1h/trend_4h 同向=顺势、反向=逆势、其余=混合。
_CALIBRATION_HOUR_ORDER = (
    "00-03h", "04-07h", "08-11h", "12-15h", "16-19h", "20-23h", "unknown")
_CALIBRATION_ALIGNMENT_LABELS = ("顺势", "逆势", "混合", "未知")


def _open_hour_bucket(ts_value) -> str:
    """UTC+8 open-time hour → 4h bucket label; anything unparsable → unknown."""
    text = str(ts_value or "").strip()
    if len(text) < 13:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CST)  # historical V2 naive timestamps are CST
        hour = parsed.astimezone(CST).hour
    except (ValueError, OverflowError):
        return "unknown"
    start = (hour // 4) * 4
    return f"{start:02d}-{start + 3:02d}h"


def _trend_alignment(side, trend_1h, trend_4h) -> str:
    """Side vs. frozen 1H/4H trend flags (+1/-1/0) → 顺势/逆势/混合/未知."""
    direction = {"long": 1, "short": -1}.get(str(side or "").strip().lower())
    try:
        t1 = int(trend_1h)
        t4 = int(trend_4h)
    except (TypeError, ValueError):
        return "未知"
    if direction is None:
        return "未知"
    if t1 == direction and t4 == direction:
        return "顺势"
    if t1 == -direction and t4 == -direction:
        return "逆势"
    return "混合"


def connect(db_root, name):
    con = sqlite3.connect(f"file:{db_root}\\{name}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _frozen_exit_quality_history(db_root: str | Path, days: int = 7) -> list[dict]:
    """Read only ready-bound daily artifacts; never recompute from trade DBs."""
    project_root = Path(db_root).resolve().parent
    quality_dir = Path(os.environ.get(
        "OKX_QUALITY_REPORT_DIR", str(project_root / "reports" / "quality")))
    ready_dir = Path(os.environ.get(
        "OKX_REVIEWER_READY_DIR", str(quality_dir)))
    artifacts: list[dict] = []
    for path in sorted(quality_dir.glob("exit_quality_????-??-??.json"), reverse=True):
        if len(artifacts) >= days:
            break
        business_date = path.stem.removeprefix("exit_quality_")
        manifest_path = ready_dir / f"reviewer_ready_{business_date}.json"
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        step = (manifest.get("steps") or {}).get("exit_quality") if isinstance(
            manifest, dict) else None
        bound = step.get("artifact") if isinstance(step, dict) else None
        peak = payload.get("peak_giveback") if isinstance(payload, dict) else None
        margin = (
            payload.get("margin_return_review")
            if isinstance(payload, dict) else None)
        missed = (
            payload.get("missed_take_profit")
            if isinstance(payload, dict) else None)
        candidate = (
            payload.get("candidate_window")
            if isinstance(payload, dict) else None)
        candidate_start = str(
            candidate.get("start_ts") or "") if isinstance(candidate, dict) else ""
        candidate_end = str(
            candidate.get("end_ts") or "") if isinstance(candidate, dict) else ""
        peak_effective_start = max(
            candidate_start, EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS)
        expected_peak_status = (
            "PENDING" if peak_effective_start >= candidate_end else "COMPLETE")
        try:
            generated_at = datetime.strptime(
                str(payload.get("generated_at") or "")[:19],
                "%Y-%m-%d %H:%M:%S")
            report_end = datetime.strptime(
                f"{business_date} 08:00:00", "%Y-%m-%d %H:%M:%S")
        except (AttributeError, ValueError):
            generated_at = report_end = None
        if not (
            isinstance(payload, dict)
            and payload.get("schema_version") == EXIT_QUALITY_SCHEMA_VERSION
            and payload.get("method_version") == EXIT_QUALITY_METHOD_VERSION
            and payload.get("business_date") == business_date
            and generated_at is not None and report_end is not None
            and generated_at >= report_end
            and payload.get("report_activation_cst")
            == EXIT_QUALITY_REPORT_ACTIVATION_TS
            and payload.get("margin_fact_activation_cycle")
            == EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE
            and payload.get("counterfactual_activation_cst")
            == EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS
            and isinstance(peak, dict)
            and peak.get("method_version")
            in EXIT_QUALITY_PEAK_METHOD_VERSIONS_ACCEPTED
            and peak.get("fact_activation_cst")
            == EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS
            and candidate_start and candidate_end
            and peak.get("status") == expected_peak_status
            and (
                (peak.get("effective_window") or {}).get("start_ts"),
                (peak.get("effective_window") or {}).get("end_ts"),
                (peak.get("effective_window") or {}).get("end_exclusive"),
            ) == (peak_effective_start, candidate_end, True)
            and isinstance(margin, dict)
            and "requested_unconfirmed" in (
                margin.get("disposition_counts") or {})
            and isinstance(missed, dict)
            and missed.get("evidence_method_version")
            == EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD
            and missed.get("upstream_status") == "READY"
            and isinstance(missed.get("pool_size"), int)
            and missed.get("pool_size")
            == (missed.get("classification_counts") or {}).get(
                "missed_take_profit")
            and isinstance(manifest, dict)
            and manifest.get("business_date") == business_date
            and manifest.get("state") == "ready"
            and manifest.get("ready") is True
            and isinstance(step, dict)
            and step.get("accepted") is True
            and isinstance(bound, dict)
            and str(bound.get("path") or "") == str(path)
            and str(bound.get("sha256") or "").lower()
            == hashlib.sha256(raw).hexdigest()
            and bound.get("size_bytes") == len(raw)
        ):
            continue
        artifacts.append(payload)
    return artifacts


def _summarize_frozen_exit_quality(artifacts: list[dict]) -> dict | None:
    if not artifacts:
        return None
    summary = {
        "days": len(artifacts),
        "first_business_date": min(str(row["business_date"]) for row in artifacts),
        "last_business_date": max(str(row["business_date"]) for row in artifacts),
        "reached_1r": 0,
        "closed_at_or_above_1r": 0,
        "profit_giveback_cases": 0,
        "peak_source_closed_rows": 0,
        "peak_excluded_non_live_rows": 0,
        "peak_excluded_non_open_rows": 0,
        "peak_status": "UNKNOWN",
        "flagged": 0,
        "reviewed": 0,
        "unknown_margin_facts": 0,
        "total_margin_facts": 0,
        "margin_source_candidate_cycles": 0,
        "margin_excluded_non_live_cycles": 0,
        "margin_excluded_non_open_positions": 0,
        "dispositions": {
            key: 0 for key in (
                "hold", "close", "reduce", "adjust", "add", "open",
                "attempted_failed", "requested_unconfirmed")
        },
        "action_layers": {
            layer: {key: 0 for key in (
                "close", "reduce", "adjust", "add", "open")}
            for layer in ("requested", "succeeded", "fills", "failed")
        },
        "missed_take_profit_status": "UNKNOWN",
        "missed_take_profit_pool_size": 0,
        "missed_take_profit_classified_count": 0,
        "missed_take_profit_unknown_pool_days": 0,
        "missed_source_closed_rows": 0,
        "missed_excluded_profile_count": 0,
        "missed_excluded_fallback_count": 0,
    }
    peak_statuses = set()
    missed_statuses = set()
    for artifact in artifacts:
        peak = artifact.get("peak_giveback") or {}
        margin = artifact.get("margin_return_review") or {}
        missed = artifact.get("missed_take_profit") or {}
        summary["reached_1r"] += int(peak.get("reached_1r") or 0)
        summary["closed_at_or_above_1r"] += int(
            peak.get("closed_at_or_above_1r") or 0)
        summary["profit_giveback_cases"] += int(
            peak.get("profit_giveback_case_count") or 0)
        summary["peak_source_closed_rows"] += int(
            peak.get("source_closed_rows") or 0)
        summary["peak_excluded_non_live_rows"] += int(
            peak.get("excluded_non_live_rows") or 0)
        summary["peak_excluded_non_open_rows"] += int(
            peak.get("excluded_non_open_rows") or 0)
        peak_statuses.add(str(peak.get("status") or "UNKNOWN"))
        summary["flagged"] += int(margin.get("flagged_position_cycles") or 0)
        summary["reviewed"] += int(margin.get("explicitly_reviewed") or 0)
        summary["unknown_margin_facts"] += int(
            margin.get("unknown_fact_position_cycles") or 0)
        summary["total_margin_facts"] += int(
            margin.get("total_position_cycles") or 0)
        summary["margin_source_candidate_cycles"] += int(
            margin.get("source_candidate_cycle_rows") or 0)
        summary["margin_excluded_non_live_cycles"] += int(
            margin.get("excluded_non_live_cycle_rows") or 0)
        summary["margin_excluded_non_open_positions"] += int(
            margin.get("excluded_non_open_position_rows") or 0)
        for key in summary["dispositions"]:
            summary["dispositions"][key] += int(
                (margin.get("disposition_counts") or {}).get(key) or 0)
        for layer, counts in summary["action_layers"].items():
            source_counts = (margin.get("action_layer_counts") or {}).get(
                layer) or {}
            for key in counts:
                counts[key] += int(source_counts.get(key) or 0)
        missed_statuses.add(str(missed.get("status") or "UNKNOWN"))
        pool_size = missed.get("pool_size")
        if isinstance(pool_size, int):
            summary["missed_take_profit_pool_size"] += pool_size
        else:
            summary["missed_take_profit_unknown_pool_days"] += 1
        summary["missed_take_profit_classified_count"] += int(
            (missed.get("classification_counts") or {}).get(
                "missed_take_profit") or 0)
        summary["missed_source_closed_rows"] += int(
            missed.get("source_closed_rows") or 0)
        summary["missed_excluded_profile_count"] += int(
            missed.get("excluded_profile_count") or 0)
        summary["missed_excluded_fallback_count"] += int(
            missed.get("excluded_fallback_count") or 0)
    summary["missed_take_profit_status"] = (
        next(iter(missed_statuses)) if len(missed_statuses) == 1 else "MIXED")
    summary["peak_status"] = (
        next(iter(peak_statuses)) if len(peak_statuses) == 1 else "MIXED")
    summary["review_rate"] = (
        summary["reviewed"] / summary["flagged"]
        if summary["flagged"] else None)
    summary["margin_fact_coverage"] = (
        (summary["total_margin_facts"] - summary["unknown_margin_facts"])
        / summary["total_margin_facts"]
        if summary["total_margin_facts"] else None)
    latest = max(artifacts, key=lambda row: str(row["business_date"]))
    summary["latest_peak_giveback_median_r"] = (
        latest.get("peak_giveback") or {}).get(
            "profitable_peak_giveback_median_r")
    return summary


def age_min(ts_utc_iso):
    try:
        t = datetime.fromisoformat(str(ts_utc_iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            # C3（2026-07-03）：无时区后缀的 ts 按项目约定是 CST 'YYYY-MM-DD HH:MM:SS'
            # （account_snapshots/position_snapshots/news_items 等写方已切 CST）；
            # 旧实现按 UTC 解会把新鲜 CST 行算成 -480min。带 Z 的（market.db 等）走上面分支不变。
            t = t.replace(tzinfo=CST)
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    except Exception:
        return None


def fmt_age(ts):
    a = age_min(ts)
    return f"{a:.0f}m" if a is not None else "?"


def early_structure_side(votes):
    """4H 满票立向且 15m 未同向 → 'long'|'short'|None（早期结构判定）。

    2026-08-14：成熟候选排序（|三周期一致|→|chg24h|）天然偏晚期结构，模型有
    正当理由「不追」（2026-08-13 实测 8 候选中 4 个因「位置过热/太晚」被 wait）。
    本判定挑出 4H 方向已立、15m 回调/整理未走完的候选；只改证据呈现，
    不构成任何自动闸或下单指令。votes[tf] ∈ {-2, 0, 2}。
    """
    v4 = votes.get("4H")
    v15 = votes.get("15m")
    if v4 is None or v15 is None:
        return None
    if v4 >= 2 and v15 <= 0:
        return "long"
    if v4 <= -2 and v15 >= 0:
        return "short"
    return None


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def entry_timing_snapshot(timeframe_metrics, side):
    """Separate entry timing from trend strength; this is ranking evidence, not a gate."""
    direction = 1.0 if side == "long" else -1.0 if side == "short" else 0.0
    rows = {}
    extension_reasons = []
    extension_values = []
    for timeframe in ("15m", "1H"):
        metric = dict((timeframe_metrics or {}).get(timeframe) or {})
        close = _finite_number(metric.get("close"))
        ma20 = _finite_number(metric.get("ma20"))
        atr14 = _finite_number(metric.get("atr14"))
        rsi14 = _finite_number(metric.get("rsi14"))
        extension_atr = None
        if direction and close is not None and ma20 is not None and atr14 and atr14 > 0:
            extension_atr = direction * (close - ma20) / atr14
            extension_values.append(max(extension_atr, 0.0))
            if extension_atr >= ENTRY_EXTENSION_ATR_THRESHOLD:
                extension_reasons.append(
                    f"{timeframe}_directional_ma_distance_gte_"
                    f"{ENTRY_EXTENSION_ATR_THRESHOLD:g}atr")
        if side == "long" and rsi14 is not None and rsi14 >= ENTRY_EXTENSION_RSI_LONG:
            extension_reasons.append(f"{timeframe}_rsi_gte_{ENTRY_EXTENSION_RSI_LONG:g}")
        if side == "short" and rsi14 is not None and rsi14 <= ENTRY_EXTENSION_RSI_SHORT:
            extension_reasons.append(f"{timeframe}_rsi_lte_{ENTRY_EXTENSION_RSI_SHORT:g}")
        rows[timeframe] = {
            "directional_ma_distance_atr": (
                round(extension_atr, 6) if extension_atr is not None else None),
            "rsi14": round(rsi14, 4) if rsi14 is not None else None,
        }
    max_extension = max(extension_values) if extension_values else None
    timing_score = (
        max(0.0, 1.0 - min(max(max_extension, 0.0), 3.0) / 3.0)
        if max_extension is not None else 0.0
    )
    return {
        "version": CANDIDATE_RANK_VERSION,
        "extended": bool(extension_reasons),
        "extension_reasons": extension_reasons,
        "maximum_directional_ma_distance_atr": (
            round(max_extension, 6) if max_extension is not None else None),
        "timing_score": round(timing_score, 6),
        "timeframes": rows,
        "decision_consumes_as_hard_gate": False,
    }


def classify_opportunity_state(
    votes, timeframe_metrics, *, require_four_hour_direction=True,
):
    """Classify one directional episode without conflating setup maturity and timing."""
    v4 = (votes or {}).get("4H")
    if require_four_hour_direction:
        side = "long" if v4 == 2 else "short" if v4 == -2 else None
    else:
        weighted = sum(
            float((votes or {}).get(timeframe) or 0) / 2.0 * weight
            for timeframe, weight in (("15m", 0.25), ("1H", 0.35), ("4H", 0.40))
        )
        if abs(weighted) <= 1e-12:
            # Deterministic tie break favours the decision horizon, not 4H.
            tie_vote = next(
                (
                    (votes or {}).get(timeframe)
                    for timeframe in ("1H", "15m", "4H")
                    if (votes or {}).get(timeframe) in {-2, 2}
                ),
                None,
            )
            side = "long" if tie_vote == 2 else "short" if tie_vote == -2 else None
        else:
            side = "long" if weighted > 0 else "short"
    if side is None:
        return "NON_DIRECTIONAL", None, entry_timing_snapshot(
            timeframe_metrics, None)
    same_vote = 2 if side == "long" else -2
    timing = entry_timing_snapshot(timeframe_metrics, side)
    lower_votes = ((votes or {}).get("15m"), (votes or {}).get("1H"))
    alignment_votes = (
        lower_votes if require_four_hour_direction else
        ((votes or {}).get("15m"), (votes or {}).get("1H"),
         (votes or {}).get("4H")))
    if all(value == same_vote for value in alignment_votes):
        if timing["extended"]:
            return "EXTENDED", side, timing
        return "ENTRY_READY", side, timing
    if any(value == same_vote for value in alignment_votes):
        return "TRIGGERING", side, timing
    return "EARLY_WATCH", side, timing


def trend_strength_snapshot(votes, side):
    same_vote = 2 if side == "long" else -2 if side == "short" else None
    if same_vote is None:
        return {
            "same_direction_timeframes": 0,
            "opposite_direction_timeframes": 0,
            "score": 0.0,
        }
    values = [(votes or {}).get(tf) for tf in DECISION_TIMEFRAMES]
    same = sum(value == same_vote for value in values)
    opposite = sum(value == -same_vote for value in values)
    direction = 1.0 if side == "long" else -1.0
    weighted = sum(
        direction * float((votes or {}).get(timeframe) or 0) / 2.0 * weight
        for timeframe, weight in (("15m", 0.25), ("1H", 0.35), ("4H", 0.40))
    )
    return {
        "same_direction_timeframes": same,
        "opposite_direction_timeframes": opposite,
        "score": round(weighted, 6),
        "weights": {"15m": 0.25, "1H": 0.35, "4H": 0.40},
        "calibrated": False,
    }


def opportunity_id(symbol, side, first_seen_cycle, state_version=None):
    version = state_version or OPPORTUNITY_STATE_VERSION
    parts = [str(symbol).upper(), str(side).lower(), str(first_seen_cycle)]
    if version != "opportunity_state_v1":
        parts.insert(0, version)
    raw = "|".join(parts)
    return "opp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _invalidation_reference(timeframe_metrics, side, state_version=None):
    if state_version == RELAXED_OPPORTUNITY_STATE_VERSION:
        return {
            "timeframe": "weighted_15m_1H_4H",
            "reference": "weighted_direction_score_and_dominant_timeframe",
            "level": None,
            "condition": (
                "weighted direction score crosses zero or the dominant timeframe reverses"
                if side in {"long", "short"} else
                "weighted direction remains unresolved"
            ),
        }
    metric = dict((timeframe_metrics or {}).get("4H") or {})
    ma20 = _finite_number(metric.get("ma20"))
    return {
        "timeframe": "4H",
        "reference": "ma20_and_macd_hist_direction",
        "level": round(ma20, 12) if ma20 is not None else None,
        "condition": (
            "4H close at_or_below MA20 or MACD histogram non_positive"
            if side == "long" else
            "4H close at_or_above MA20 or MACD histogram non_negative"
            if side == "short" else
            "4H direction not established"
        ),
    }


def enrich_opportunity_context(
    candidate,
    cycle_id,
    regime,
    previous=None,
    *,
    tick_ts=None,
    regime_source_ts=None,
    state_version=None,
):
    """Carry one exact symbol+side episode across its state transitions."""
    previous = dict(previous or {})
    side = candidate.get("opportunity_side")
    version = state_version or OPPORTUNITY_STATE_VERSION
    same_episode = (
        side in {"long", "short"}
        and previous.get("state_version") == version
        and previous.get("side") == side
        and previous.get("symbol") == candidate["row"]["symbol"]
        and previous.get("opportunity_id")
    )
    current_invalidation = _invalidation_reference(
        candidate.get("timeframe_metrics"), side, version)
    if same_episode:
        first_seen_cycle = previous.get("first_seen_cycle") or cycle_id
        first_seen_ts_utc = previous.get("first_seen_ts_utc") or tick_ts
        first_seen_price = previous.get("first_seen_price")
        regime_first_seen = previous.get("regime_first_seen")
        regime_first_seen_source_ts = previous.get(
            "regime_first_seen_source_ts")
        initial_invalidation = (
            previous.get("initial_invalidation") or current_invalidation)
        state_entered_cycle = (
            previous.get("state_entered_cycle")
            if previous.get("opportunity_state")
            == candidate.get("opportunity_state") else cycle_id)
        identifier = previous.get("opportunity_id")
        previous_state = previous.get("opportunity_state")
    else:
        first_seen_cycle = cycle_id
        first_seen_ts_utc = tick_ts
        first_seen_price = candidate["row"].get("last")
        regime_first_seen = regime
        regime_first_seen_source_ts = regime_source_ts
        initial_invalidation = current_invalidation
        state_entered_cycle = cycle_id
        identifier = (
            opportunity_id(
                candidate["row"]["symbol"], side, cycle_id, version)
            if side in {"long", "short"} else None)
        previous_state = None
    current_state = candidate.get("opportunity_state")
    candidate.update({
        "state_version": version,
        "opportunity_id": identifier,
        "first_seen_cycle": first_seen_cycle,
        "first_seen_ts_utc": first_seen_ts_utc,
        "first_seen_price": first_seen_price,
        "state_entered_cycle": state_entered_cycle,
        "previous_state": previous_state,
        "state_transition": (
            f"{previous_state}->{current_state}"
            if previous_state and previous_state != current_state
            else f"NEW->{current_state}" if not previous_state
            else f"{current_state}->{current_state}"
        ),
        "regime_first_seen": regime_first_seen,
        "regime_first_seen_source_ts": regime_first_seen_source_ts,
        "regime_current": regime,
        "regime_current_source_ts": regime_source_ts,
        "initial_invalidation": initial_invalidation,
        "current_invalidation": current_invalidation,
        "terminated_opportunity": (
            {
                "opportunity_id": previous.get("opportunity_id"),
                "side": previous.get("side"),
                "previous_state": previous.get("opportunity_state"),
                "invalidated_at_cycle": cycle_id,
                "invalidated_at_ts_utc": tick_ts,
                "reason": (
                    "side_reversal" if side in {"long", "short"}
                    and previous.get("side") in {"long", "short"}
                    and previous.get("side") != side else
                    "four_hour_direction_lost"
                ),
            }
            if previous.get("opportunity_id") and not same_episode else None
        ),
    })
    return candidate


def candidate_rank_key(candidate, history=None, *, relaxed_policy=False):
    """Rank trend and entry timing separately; 24h absolute change is excluded."""
    history = dict(history or {})
    state = str(candidate.get("opportunity_state") or "NON_DIRECTIONAL")
    trend = dict(candidate.get("trend_strength") or {})
    timing = dict(candidate.get("entry_timing") or {})
    rotation_due = not bool(history)
    rejected_n = int(history.get("rejected_n") or 0)
    core = (
        OPPORTUNITY_STATE_PRIORITY.get(state, 0),
        1 if rotation_due else 0,
        -min(rejected_n, 3),
        float(trend.get("score") or 0.0),
        float(timing.get("timing_score") or 0.0),
    )
    if relaxed_policy:
        return (*core, str(candidate["row"].get("symbol") or ""))
    return (
        *core,
        min(float(candidate.get("quote_vol") or 0) / MIN_QUOTE_VOL_USD, 100),
        min(float(candidate["row"].get("candidate_oi_usd") or 0)
            / MIN_OI_USD, 100),
    )


def balanced_candidate_pick(candidates, count):
    """Preserve the existing long/short quota shape, then fill by the same rank."""
    ordered = sorted(candidates, key=lambda item: item["rank_key"], reverse=True)
    longs = [item for item in ordered if item.get("opportunity_side") == "long"]
    shorts = [item for item in ordered if item.get("opportunity_side") == "short"]
    picked = longs[:count // 2] + shorts[:count // 2]
    seen = {item["row"]["symbol"] for item in picked}
    for item in ordered:
        if len(picked) >= count:
            break
        if item["row"]["symbol"] not in seen:
            picked.append(item)
            seen.add(item["row"]["symbol"])
    return sorted(picked, key=lambda item: item["rank_key"], reverse=True)


def consecutive_wait_streak(ana, symbol, lookback=None):
    """该标的最近连续 action='wait' 的信号轮数与最新一轮 side；遇非 wait 即停。

    每 cycle 是独立 session、模型无跨轮记忆——「同一标的已连等 N 轮」必须由
    简报外显，新 session 才看得见自己在拖延。只读 analysis_signals；
    连接/查询异常由调用方兜（简报任何子段失败不中断）。
    """
    if lookback is None:
        lookback = WAIT_STREAK_LOOKBACK
    rows = ana.execute(
        "SELECT action, side FROM analysis_signals WHERE symbol=? "
        "ORDER BY cycle_id DESC LIMIT ?",
        (symbol, lookback),
    ).fetchall()
    streak, side = 0, None
    for row in rows:
        if str(row["action"] or "") != "wait":
            break
        if streak == 0:
            side = row["side"]
        streak += 1
    return streak, side


# ── 2026-08-18 候选快照 JSONL + 连续候选轮数 v2 ─────────────────────────
# 背景：08-15 吞吐契约后 analysis_signals 只落最终开仓短名单（无 wait 行）——
# 旧 consecutive_wait_streak 只会数到冻结的历史行（显示为当前状态=假数），
# 错失池对照组同时断源。两层候选本轮反正已算好，追加一行 JSONL 快照
# （纯文件追加、零 DB 写、fail-safe），三个消费者换到该确定性来源：
#   ① missed_opps_writer 的 briefing_layer_v1 源（对照组复活，前向边界
#      2026-08-19T08:00，见该文件）
#   ② 本文件「连续候选 N 轮未成交」（语义升级：系统事实，不依赖 agent 明示 wait）
#   ③ missed_opps_writer 断供监控（昨日快照缺失/空 → WARN）
# 仅 dispatcher 传 --cycle-id 时写；agent 手动补跑不带参数 → 不写，避免同
# cycle 多来源混写；同 cycle 两次派发预读（analyst/trader）产生的重复行由
# 消费端按 cycle_id 取首行去重。
BRIEFING_SNAPSHOT_SCHEMA = "briefing_candidates_v1"
BRIEFING_CANDIDATE_MANIFEST_SCHEMA = "briefing_candidate_manifest_v1"
BRIEFING_CANDIDATE_MANIFEST_SCHEMA_V2 = "briefing_candidate_manifest_v2"
BRIEFING_CANDIDATE_MANIFEST_SCHEMA_V3 = (
    "briefing_candidate_manifest_v3_side_neutral")


def briefing_candidate_id(
    cycle_id, ordinal, symbol, side, layer,
):
    """Stable exact-manifest identity; never infer identity from free text."""
    raw = "|".join((
        str(cycle_id), str(int(ordinal)), str(symbol).upper(),
        str(side).lower(), str(layer).lower(),
    ))
    return "cand_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _briefing_snapshot_dir(root):
    return Path(root).resolve().parent / "logs" / "briefing"


def snapshot_path_for_date(root, date_str):
    return _briefing_snapshot_dir(root) / (
        f"candidates-{str(date_str).replace('-', '')}.jsonl")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def append_candidate_snapshot(
    root, cycle_id, picked, early, tick_ts,
    candidate_out_file: str | Path | None = None,
    dig_history: dict | None = None,
    ready_pool_reference: dict | None = None,
    manifest_ordered: list | None = None,
    review_slice: list | None = None,
):
    """Write a bounded observation row plus the exact-cycle eligibility manifest."""
    side_neutral = thresholds.minimal_decision_contract_active(cycle_id)
    closure_neutral = (
        side_neutral and thresholds.minimal_contract_closure_active(cycle_id))
    cands = []
    recent = dig_history if isinstance(dig_history, dict) else {}
    ordered = (
        list(manifest_ordered)
        if manifest_ordered is not None else list(picked) + list(early))
    reviewed_symbols = {
        str(item["row"]["symbol"])
        for item in (
            list(review_slice) if review_slice is not None else ordered)
    }
    for x in ordered:
        r = x["row"]
        symbol = str(r["symbol"])
        if side_neutral:
            side = None
            layer = "all_market"
            side_histories = [
                recent.get((symbol, value)) or {}
                for value in ("long", "short")
            ]
            history = max(
                side_histories,
                key=lambda item: str(item.get("last_cycle") or ""),
                default={},
            )
        else:
            side = str(
                x.get("opportunity_side")
                or x.get("early_side")
                or {"偏多": "long", "偏空": "short"}.get(x.get("bias"))
                or "").lower()
            if side not in {"long", "short"}:
                continue
            layer = (
                "mature" if manifest_ordered is None and x in picked else
                "early" if manifest_ordered is None else
                "mature" if x.get("opportunity_state") in {
                    "ENTRY_READY", "EXTENDED"} else "early")
            history = recent.get((symbol, side)) or {}
        ordinal = len(cands) + 1
        candidate = {
            "ordinal": ordinal,
            "layer": layer, "symbol": symbol,
            "side": side, "last": r["last"],
            "eligible_sides": ["long", "short"] if side_neutral else [side],
            "chg24h": r["chg24h"],
            "rotation_due": not bool(history),
            "recent_deep_dives_6h": int(history.get("n") or 0),
            "recent_rejections_6h": int(history.get("rejected_n") or 0),
            "prior_evidence_hash": history.get("last_evidence_hash"),
            "new_evidence_required": int(history.get("rejected_n") or 0) >= 3,
            "opportunity_id": None if side_neutral else x.get("opportunity_id"),
            "opportunity_state": (
                "SIDE_NEUTRAL" if side_neutral else x.get("opportunity_state")),
            "state_version": (
                SIDE_NEUTRAL_STATE_VERSION if side_neutral
                else x.get("state_version")),
            "first_seen_cycle": x.get("first_seen_cycle"),
            "first_seen_ts_utc": x.get("first_seen_ts_utc"),
            "first_seen_price": x.get("first_seen_price"),
            "state_entered_cycle": x.get("state_entered_cycle"),
            "previous_state": x.get("previous_state"),
            "state_transition": x.get("state_transition"),
            "regime_first_seen": x.get("regime_first_seen"),
            "regime_first_seen_source_ts": x.get(
                "regime_first_seen_source_ts"),
            "regime_current": x.get("regime_current"),
            "regime_current_source_ts": x.get("regime_current_source_ts"),
            "initial_invalidation": x.get("initial_invalidation"),
            "current_invalidation": x.get("current_invalidation"),
            "trend_strength": None if side_neutral else x.get("trend_strength"),
            "entry_timing": None if side_neutral else x.get("entry_timing"),
            "ready_pool_rank": x.get("ready_pool_rank"),
            "opportunity_score": x.get("opportunity_score"),
            "opportunity_score_components": x.get(
                "opportunity_score_components"),
            "selection_reason": x.get("selection_reason"),
            "rank_version": (
                x.get("rank_version", SIDE_NEUTRAL_RANK_VERSION)
                if side_neutral
                else x.get("rank_version", CANDIDATE_RANK_VERSION)),
            "selected_for_review": symbol in reviewed_symbols,
        }
        if closure_neutral:
            candidate["currently_held_observation"] = bool(
                x.get("currently_held_observation")
                or r.get("currently_held_observation"))
        if not closure_neutral:
            candidate["candidate_id"] = briefing_candidate_id(
                cycle_id, ordinal, symbol, side or "any", layer)
        cands.append(candidate)
    bounded_snapshot_candidates = (
        [item for item in cands if item.get("selected_for_review") is True]
        if manifest_ordered is not None else cands)
    written_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    snapshot = {
        "schema": BRIEFING_SNAPSHOT_SCHEMA,
        "cycle_id": str(cycle_id),
        "tick_ts": tick_ts,
        "written_at_cst": written_at,
        "candidates": bounded_snapshot_candidates,
    }
    line = json.dumps(snapshot, ensure_ascii=False)
    path = snapshot_path_for_date(root, str(cycle_id)[:10])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    manifest_core = {
        "schema": (
            BRIEFING_CANDIDATE_MANIFEST_SCHEMA_V3
            if side_neutral else
            BRIEFING_CANDIDATE_MANIFEST_SCHEMA_V2
            if manifest_ordered is not None
            else BRIEFING_CANDIDATE_MANIFEST_SCHEMA),
        "identity_contract": (
            "symbol_review_v2_full_manifest_no_identity_gate"
            if closure_neutral else
            "symbol_review_v1_side_selected_by_agent"
            if side_neutral else "candidate_id_v1_exact_manifest_no_alias"),
        "cycle_id": str(cycle_id),
        "tick_ts": tick_ts,
        "written_at_cst": written_at,
        "candidate_count": len(cands),
        "review_slice_count": len(bounded_snapshot_candidates),
        "candidates": cands,
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    if closure_neutral:
        manifest_core["review_symbols"] = [
            item["symbol"] for item in bounded_snapshot_candidates]
    else:
        manifest_core["review_candidate_ids"] = [
            item["candidate_id"] for item in bounded_snapshot_candidates]
    if isinstance(ready_pool_reference, dict):
        manifest_core["ready_pool"] = {
            "schema": ready_pool_reference.get("schema"),
            "status": ready_pool_reference.get("status"),
            "path": ready_pool_reference.get("path"),
            "sha256": ready_pool_reference.get("sha256"),
            "ready_count": ready_pool_reference.get("ready_count"),
            "selected_count": ready_pool_reference.get("selected_count"),
            "error": ready_pool_reference.get("error"),
        }
    canonical = json.dumps(
        manifest_core, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    manifest = {
        **manifest_core,
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    if candidate_out_file is not None:
        _atomic_json(Path(candidate_out_file), manifest)
    return manifest


def _ready_pool_hash_core(payload):
    core = dict(payload)
    core.pop("ready_pool_sha256", None)
    return hashlib.sha256(json.dumps(
        core, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def load_previous_ready_pool(ready_pool_out_file, cycle_id):
    """Load only the immediately preceding natural slot; a gap starts a new episode."""
    if ready_pool_out_file is None:
        return {}
    try:
        current = datetime.strptime(str(cycle_id), "%Y-%m-%dT%H:%M")
        previous_cycle = (current - timedelta(minutes=15)).strftime(
            "%Y-%m-%dT%H:%M")
        path = Path(ready_pool_out_file).parent / (
            "briefing-ready-pool-"
            + previous_cycle.replace(":", "-") + ".json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") not in {
                READY_POOL_SCHEMA, READY_POOL_SCHEMA_SIDE_NEUTRAL}:
            return {}
        if payload.get("cycle_id") != previous_cycle:
            return {}
        if payload.get("ready_pool_sha256") != _ready_pool_hash_core(payload):
            return {}
        items = payload.get("items")
        if not isinstance(items, list):
            return {}
        return {
            str(item.get("symbol")): item
            for item in items
            if isinstance(item, dict) and item.get("symbol")
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def write_ready_pool_artifact(
    *,
    cycle_id,
    tick_ts,
    ranked,
    picked,
    early,
    manifest_ordered=None,
    review_slice=None,
    out_file,
    previous_ready=None,
):
    """Persist every ready row and separate eligibility from review capacity."""
    if out_file is None:
        return None
    if thresholds.minimal_decision_contract_active(cycle_id):
        closure_neutral = thresholds.minimal_contract_closure_active(cycle_id)
        side_neutral_rank_version = (
            SIDE_NEUTRAL_OPPORTUNITY_RANK_VERSION
            if closure_neutral else SIDE_NEUTRAL_RANK_VERSION)
        selected = list(manifest_ordered or ranked)
        reviewed = list(review_slice or selected)
        review_by_symbol = {
            str(item["row"]["symbol"]): index
            for index, item in enumerate(reviewed, start=1)
        }
        selected_by_symbol = {
            str(item["row"]["symbol"]): index
            for index, item in enumerate(selected, start=1)
        }
        items = []
        for pool_rank, candidate in enumerate(ranked, start=1):
            candidate["ready_pool_rank"] = pool_rank
            row = candidate["row"]
            symbol = str(row["symbol"])
            ready_pool_id = "ready_" + hashlib.sha256(
                f"{cycle_id}|{symbol}".encode("utf-8")).hexdigest()[:20]
            items.append({
                "ready_pool_id": ready_pool_id,
                "ready_pool_rank": pool_rank,
                "symbol": symbol,
                "side": None,
                "eligible_sides": ["long", "short"],
                "opportunity_id": None,
                "opportunity_state": "SIDE_NEUTRAL",
                "state_version": SIDE_NEUTRAL_STATE_VERSION,
                "last": row.get("last"),
                "chg24h_observation_only": row.get("chg24h"),
                "quote_volume_usd": (
                    round(float(candidate["quote_vol"]), 6)
                    if candidate.get("quote_vol") is not None else None),
                "oi_usd": (
                    round(float(row["candidate_oi_usd"]), 6)
                    if row.get("candidate_oi_usd") is not None else None),
                "oi_source": row.get("candidate_oi_source"),
                "votes": None,
                "timeframes": None,
                "trend_strength": None,
                "entry_timing": None,
                "rank_version": side_neutral_rank_version,
                "opportunity_score": candidate.get("opportunity_score"),
                "rank_components": (
                    candidate.get("opportunity_score_components")
                    if closure_neutral else {
                        "round_robin_order": pool_rank,
                        "timeframe_judgment_used": False,
                        "absolute_chg24h_used": False,
                    }
                ),
                "selection_reason": candidate.get("selection_reason"),
                "mature_eligible": None,
                "early_eligible": None,
                "selected_for_manifest": symbol in selected_by_symbol,
                "selected_for_review": symbol in review_by_symbol,
                "selected_ordinal": selected_by_symbol.get(symbol),
                "selected_layer": "all_market",
                "review_ordinal": review_by_symbol.get(symbol),
                "pipeline_disposition": "shortlisted",
                "pipeline_reason": "side_neutral_all_market_owner_policy",
                **({
                    "currently_held_observation": bool(
                        candidate.get("currently_held_observation")
                        or row.get("currently_held_observation")),
                } if closure_neutral else {}),
            })
        core = {
            "schema": READY_POOL_SCHEMA_SIDE_NEUTRAL,
            "state_version": SIDE_NEUTRAL_STATE_VERSION,
            "rank_version": side_neutral_rank_version,
            "mode": "read_only_observation",
            "cycle_id": str(cycle_id),
            "tick_ts": tick_ts,
            "written_at_cst": datetime.now(CST).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "ready_count": len(items),
            "selected_count": len(selected),
            "review_count": len(reviewed),
            "terminated_opportunity_count": 0,
            "terminated_opportunities": [],
            "items": items,
            "selection_expands_deep_dive_limit": False,
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        payload = {**core, "ready_pool_sha256": _ready_pool_hash_core(core)}
        _atomic_json(Path(out_file), payload)
        return {
            "schema": READY_POOL_SCHEMA_SIDE_NEUTRAL,
            "status": "PASSED",
            "path": str(Path(out_file)),
            "sha256": payload["ready_pool_sha256"],
            "ready_count": len(items),
            "selected_count": len(selected),
            "review_count": len(reviewed),
        }
    selected = (
        list(manifest_ordered)
        if manifest_ordered is not None else list(picked) + list(early))
    reviewed = (
        list(review_slice)
        if review_slice is not None else list(selected))
    review_by_symbol = {
        str(item["row"]["symbol"]): index
        for index, item in enumerate(reviewed, start=1)
    }
    selected_by_symbol = {
        str(item["row"]["symbol"]): {
            "ordinal": index,
            "layer": (
                "mature" if item.get("opportunity_state") in {
                    "ENTRY_READY", "EXTENDED"} else "early"),
        }
        for index, item in enumerate(selected, start=1)
    }
    items = []
    for pool_rank, candidate in enumerate(
            sorted(ranked, key=lambda item: item["rank_key"], reverse=True),
            start=1):
        candidate["ready_pool_rank"] = pool_rank
        row = candidate["row"]
        symbol = str(row["symbol"])
        state = str(candidate.get("opportunity_state") or "NON_DIRECTIONAL")
        selected_row = selected_by_symbol.get(symbol)
        if selected_row:
            disposition = "shortlisted"
            pipeline_reason = (
                "selected_all_directional_owner_policy"
                if manifest_ordered is not None else
                "selected_mature_quota" if selected_row["layer"] == "mature"
                else "selected_early_quota")
        elif state == "NON_DIRECTIONAL":
            disposition = "not_shortlisted"
            pipeline_reason = (
                "weighted_direction_unresolved"
                if manifest_ordered is not None else
                "four_hour_direction_not_established")
        elif state in {"ENTRY_READY", "EXTENDED"}:
            disposition = "not_shortlisted"
            pipeline_reason = "mature_quota_rank_cutoff"
        else:
            disposition = "not_shortlisted"
            pipeline_reason = "early_quota_rank_cutoff"
        history = dict(candidate.get("dig_history") or {})
        ready_pool_id = "ready_" + hashlib.sha256(
            f"{cycle_id}|{symbol}".encode("utf-8")).hexdigest()[:20]
        items.append({
            "ready_pool_id": ready_pool_id,
            "ready_pool_rank": pool_rank,
            "symbol": symbol,
            "side": candidate.get("opportunity_side"),
            "opportunity_id": candidate.get("opportunity_id"),
            "opportunity_state": state,
            "state_version": candidate.get("state_version"),
            "first_seen_cycle": candidate.get("first_seen_cycle"),
            "first_seen_ts_utc": candidate.get("first_seen_ts_utc"),
            "first_seen_price": candidate.get("first_seen_price"),
            "state_entered_cycle": candidate.get("state_entered_cycle"),
            "previous_state": candidate.get("previous_state"),
            "state_transition": candidate.get("state_transition"),
            "regime_first_seen": candidate.get("regime_first_seen"),
            "regime_first_seen_source_ts": candidate.get(
                "regime_first_seen_source_ts"),
            "regime_current": candidate.get("regime_current"),
            "regime_current_source_ts": candidate.get(
                "regime_current_source_ts"),
            "initial_invalidation": candidate.get("initial_invalidation"),
            "current_invalidation": candidate.get("current_invalidation"),
            "terminated_opportunity": candidate.get("terminated_opportunity"),
            "last": row.get("last"),
            "chg24h_observation_only": row.get("chg24h"),
            "quote_volume_usd": (
                round(float(candidate["quote_vol"]), 6)
                if candidate.get("quote_vol") is not None else None),
            "oi_usd": (
                round(float(row["candidate_oi_usd"]), 6)
                if row.get("candidate_oi_usd") is not None else None),
            "oi_source": row.get("candidate_oi_source"),
            "votes": candidate.get("votes"),
            "timeframes": candidate.get("timeframe_metrics"),
            "trend_strength": candidate.get("trend_strength"),
            "entry_timing": candidate.get("entry_timing"),
            "rank_version": candidate.get(
                "rank_version", CANDIDATE_RANK_VERSION),
            "rank_components": {
                "state_priority": OPPORTUNITY_STATE_PRIORITY.get(state, 0),
                "trend_strength_score": (
                    candidate.get("trend_strength") or {}).get("score"),
                "entry_timing_score": (
                    candidate.get("entry_timing") or {}).get("timing_score"),
                "rotation_due": not bool(history),
                "recent_rejections_6h": int(history.get("rejected_n") or 0),
                "absolute_chg24h_used": False,
            },
            "mature_eligible": state in {"ENTRY_READY", "EXTENDED"},
            "early_eligible": state in {"TRIGGERING", "EARLY_WATCH"},
            "selected_for_manifest": bool(selected_row),
            "selected_for_review": symbol in review_by_symbol,
            "selected_ordinal": (
                selected_row["ordinal"] if selected_row else None),
            "selected_layer": (
                selected_row["layer"] if selected_row else None),
            "review_ordinal": review_by_symbol.get(symbol),
            "pipeline_disposition": disposition,
            "pipeline_reason": pipeline_reason,
        })
    current_symbols = {str(item.get("symbol")) for item in items}
    terminated_opportunities = [
        item["terminated_opportunity"] for item in items
        if isinstance(item.get("terminated_opportunity"), dict)
    ]
    for symbol, previous in dict(previous_ready or {}).items():
        if symbol in current_symbols or not previous.get("opportunity_id"):
            continue
        terminated_opportunities.append({
            "opportunity_id": previous.get("opportunity_id"),
            "side": previous.get("side"),
            "previous_state": previous.get("opportunity_state"),
            "invalidated_at_cycle": cycle_id,
            "invalidated_at_ts_utc": tick_ts,
            "reason": "left_ready_pool",
        })
    state_versions = sorted({
        str(item.get("state_version")) for item in items
        if item.get("state_version")})
    core = {
        "schema": READY_POOL_SCHEMA,
        "state_version": state_versions[0] if len(state_versions) == 1 else None,
        "rank_version": (
            RELAXED_CANDIDATE_RANK_VERSION
            if manifest_ordered is not None else CANDIDATE_RANK_VERSION),
        "mode": "read_only_observation",
        "cycle_id": str(cycle_id),
        "tick_ts": tick_ts,
        "written_at_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "ready_count": len(items),
        "selected_count": len(selected),
        "review_count": len(reviewed),
        "terminated_opportunity_count": len(terminated_opportunities),
        "terminated_opportunities": terminated_opportunities,
        "items": items,
        "selection_expands_deep_dive_limit": False,
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    payload = {**core, "ready_pool_sha256": _ready_pool_hash_core(core)}
    _atomic_json(Path(out_file), payload)
    return {
        "schema": READY_POOL_SCHEMA,
        "status": "PASSED",
        "path": str(Path(out_file)),
        "sha256": payload["ready_pool_sha256"],
        "ready_count": len(items),
        "selected_count": len(selected),
        "review_count": len(reviewed),
    }


def load_candidate_snapshots(root, dates):
    """读多日快照 → 按 cycle_id 升序 {cycle_id: {symbol: {side,layer}}}；
    每 cycle 取首行（同 cycle 重复行去重）；坏行/缺文件静默跳过。"""
    cycles = {}
    for d in dates:
        path = snapshot_path_for_date(root, d)
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    cyc = str(obj.get("cycle_id") or "")
                    if not cyc or cyc in cycles:
                        continue
                    m = {}
                    for c in obj.get("candidates") or []:
                        sym = str((c or {}).get("symbol") or "")
                        side = (c or {}).get("side")
                        if sym and side in ("long", "short") and sym not in m:
                            m[sym] = {
                                "side": side,
                                "layer": (c or {}).get("layer"),
                            }
                    cycles[cyc] = m
        except OSError:
            continue
    return dict(sorted(cycles.items()))


def candidate_streak_v2(root, symbols, trades_last_cycle=None):
    """连续候选轮数 v2：symbol 连续出现在快照候选层且期间无成交的轮数。

    读今天+昨天快照（≈覆盖 WAIT_STREAK_LOOKBACK 24h）；从最新快照轮向前数
    连续出现；只数该 symbol 最近一次成交 cycle 之后（任何成交=已交互，
    拖延计数重起）。side 取最新一轮快照。无快照返回 {}（不回退旧假数）。"""
    now = datetime.now(CST)
    dates = [(now - timedelta(days=1)).strftime("%Y-%m-%d"),
             now.strftime("%Y-%m-%d")]
    cycles = load_candidate_snapshots(root, dates)
    if not cycles:
        return {}
    order = list(cycles.keys())[-WAIT_STREAK_LOOKBACK:]
    out = {}
    for sym in symbols:
        streak, side = 0, None
        last_trade = str((trades_last_cycle or {}).get(sym) or "")
        for cyc in reversed(order):
            entry = cycles[cyc].get(sym)
            if entry is None:
                break
            if last_trade and cyc <= last_trade:
                break
            if streak == 0:
                side = entry["side"]
            streak += 1
        if streak:
            out[sym] = (streak, side)
    return out


def load_dig_history(
    root,
    hours: int = 6,
    now: datetime | None = None,
    as_of_cycle: str | None = None,
) -> dict:
    """近 N 小时深挖历史：{(instId,side): facts}，严格截止本轮前。

    2026-08-28 轮换注解的数据面：读 analysis_runs.raw.candidates_deep_dived
    （2026-08-22 起每轮强制落盘）。方向反转是新机会，旧方向拒绝/hash不得继承。
    只标事实、不设否决线；单行解析失败逐行跳过，整体失败由调用方降级。"""
    if as_of_cycle:
        anchor = datetime.strptime(
            str(as_of_cycle), "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    else:
        anchor = now or datetime.now(CST)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=CST)
    cutoff = (anchor - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
    upper = anchor.strftime("%Y-%m-%dT%H:%M")
    ana = connect(root, "analysis.db")
    try:
        rows = ana.execute(
            "SELECT cycle_id, raw FROM analysis_runs "
            "WHERE cycle_id >= ? AND cycle_id < ?",
            (cutoff, upper),
        ).fetchall()
    finally:
        ana.close()
    history: dict = {}
    for row in rows:
        try:
            payload = json.loads(row["raw"])
            inner = payload.get("raw")
            if isinstance(inner, str):
                inner = json.loads(inner)
            entries = (
                (inner or {}).get("candidates_deep_dived_v2")
                or (inner or {}).get("candidates_deep_dived")
                or []
            )
        except Exception:
            continue
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            inst = str(ent.get("symbol") or ent.get("instId") or "")
            side = str(ent.get("side") or "").strip().lower()
            if not inst or side not in {"long", "short"}:
                continue
            rec = history.setdefault((inst, side), {
                "n": 0,
                "rejected_n": 0,
                "last_cycle": "",
                "last_decision": "",
                "last_evidence_hash": None,
            })
            rec["n"] += 1
            decision = str(ent.get("decision") or "").strip().lower()
            if decision in {"reject", "rejected", "drop", "dropped"}:
                rec["rejected_n"] += 1
            cid = str(row["cycle_id"])
            if cid >= rec["last_cycle"]:
                rec["last_cycle"] = cid
                rec["last_decision"] = decision
                evidence_hash = str(ent.get("evidence_hash") or "").strip()
                rec["last_evidence_hash"] = (
                    evidence_hash if len(evidence_hash) == 64 else None)
    return history


def positioning_evidence_quality(
    rows,
    expected_symbols,
    *,
    now: datetime | None = None,
    minimum_coverage: float = POSITIONING_MINIMUM_COVERAGE,
    maximum_source_age_minutes: float = (
        POSITIONING_MAXIMUM_SOURCE_AGE_MINUTES),
):
    """Fail closed before exposing positioning as decision evidence."""
    if not 0 < minimum_coverage <= 1:
        raise ValueError("minimum_coverage must be in (0,1]")
    if maximum_source_age_minutes <= 0:
        raise ValueError("maximum_source_age_minutes must be positive")
    evaluated = now or datetime.now(timezone.utc)
    if evaluated.tzinfo is None:
        evaluated = evaluated.replace(tzinfo=timezone.utc)
    evaluated = evaluated.astimezone(timezone.utc)
    expected = {str(symbol) for symbol in expected_symbols}
    counts: dict[str, int] = {}
    invalid: list[dict[str, object]] = []
    ages: list[float] = []
    for row in rows:
        symbol = str(row["symbol"])
        counts[symbol] = counts.get(symbol, 0) + 1
        reasons: list[str] = []
        try:
            source_at = datetime.fromisoformat(
                str(row["ts"]).replace("Z", "+00:00"))
            if source_at.tzinfo is None:
                source_at = source_at.replace(tzinfo=timezone.utc)
            age = (
                evaluated - source_at.astimezone(timezone.utc)
            ).total_seconds() / 60.0
            ages.append(age)
            if age < -1.0:
                reasons.append("source_ts_after_decision")
            elif age > maximum_source_age_minutes:
                reasons.append("source_ts_stale_for_decision")
        except (TypeError, ValueError):
            reasons.append("source_ts_invalid")
        try:
            long_ratio = float(row["long_ratio"])
            short_ratio = float(row["short_ratio"])
            long_short_ratio = float(row["long_short_ratio"])
            if not all(map(math.isfinite, (
                long_ratio, short_ratio, long_short_ratio
            ))):
                reasons.append("ratio_non_finite")
            elif (
                not 0 <= long_ratio <= 1
                or not 0 <= short_ratio <= 1
                or long_short_ratio < 0
                or abs(long_ratio + short_ratio - 1.0) > 1e-6
                or abs(long_ratio - long_short_ratio * short_ratio) > 1e-6
            ):
                reasons.append("ratio_algebra_invalid")
        except (TypeError, ValueError):
            reasons.append("ratio_invalid")
        if reasons:
            invalid.append({"symbol": symbol, "reasons": reasons})
    observed = set(counts)
    duplicates = sorted(
        symbol for symbol, count in counts.items() if count != 1)
    invalid_symbols = {str(item["symbol"]) for item in invalid}
    valid = (
        (expected & observed) - set(duplicates) - invalid_symbols
        if expected else set()
    )
    coverage = len(valid) / len(expected) if expected else 0.0
    extra = sorted(observed - expected) if expected else sorted(observed)
    passed = bool(expected) and (
        coverage >= minimum_coverage
        and not duplicates
        and not extra
        and not invalid
    )
    return {
        "status": "PASSED" if passed else "NOT_MET",
        "expected_symbols": len(expected),
        "observed_unique_symbols": len(observed),
        "valid_symbols": len(valid),
        "coverage_rate": coverage,
        "maximum_source_age_minutes": max(ages) if ages else None,
        "missing_symbols": sorted(expected - observed),
        "extra_symbols": extra,
        "duplicate_symbols": duplicates,
        "invalid_rows": invalid[:20],
        "invalid_row_count": len(invalid),
    }


def candidate_soft_evidence(
    micro_row=None,
    positioning_row=None,
    *,
    positioning_batch_passed=False,
):
    """Format candidate-specific soft evidence without creating a gate."""
    def value(row, key):
        if row is None:
            return None
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None

    def finite(row, key, *, minimum=None, maximum=None):
        try:
            number = float(value(row, key))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if minimum is not None and number < minimum:
            return None
        if maximum is not None and number > maximum:
            return None
        return number

    spread = finite(micro_row, "spread_bps", minimum=0.0)
    imbalance = finite(
        micro_row, "imbalance_25bp", minimum=-1.0, maximum=1.0)
    taker_buy = finite(
        micro_row, "taker_buy_ratio", minimum=0.0, maximum=1.0)
    cvd = finite(micro_row, "cvd_notional_usd")
    flow_span_ms = finite(micro_row, "sample_span_ms", minimum=0.0)
    flow_sample_count = finite(micro_row, "sample_count", minimum=0.0)
    flow_fresh = (
        flow_span_ms is not None
        and flow_span_ms <= CANDIDATE_FLOW_MAXIMUM_SPAN_MINUTES * 60_000
    )
    buy_slippage = finite(
        micro_row, "buy_slippage_500usd_bps", minimum=0.0)
    sell_slippage = finite(
        micro_row, "sell_slippage_500usd_bps", minimum=0.0)
    micro_available = spread is not None and imbalance is not None
    if micro_available:
        parts = [f"点差={spread:.2f}bp", f"失衡={imbalance:+.2f}"]
        if flow_fresh and taker_buy is not None:
            parts.append(f"买盘={taker_buy:.0%}")
        if flow_fresh and cvd is not None:
            parts.append(f"CVD=${cvd / 1_000:+.0f}K")
        if flow_fresh and flow_sample_count is not None:
            parts.append(
                f"流样本/跨度={int(flow_sample_count)}/"
                f"{flow_span_ms / 1_000:.0f}s")
        elif taker_buy is not None or cvd is not None:
            parts.append("流=N/A(样本跨度过期)")
        if buy_slippage is not None and sell_slippage is not None:
            parts.append(
                f"滑点买/卖={buy_slippage:.2f}/{sell_slippage:.2f}bp")
        micro_text = "µ(" + " ".join(parts) + ")"
    else:
        micro_text = "µ=N/A"

    account_ratio = (
        finite(positioning_row, "long_short_ratio", minimum=0.0)
        if positioning_batch_passed else None
    )
    positioning_available = account_ratio is not None
    positioning_text = (
        f"账户多空比={account_ratio:.2f}"
        if positioning_available else "账户多空比=N/A"
    )
    return {
        "text": f"{micro_text} {positioning_text}",
        "micro_available": micro_available,
        "positioning_available": positioning_available,
    }


def closed_bar_cutoff(evaluation_ts_utc: str, timeframe: str) -> str:
    """Latest candle open time guaranteed closed at evaluation_ts_utc."""
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported decision timeframe: {timeframe}")
    value = datetime.fromisoformat(str(evaluation_ts_utc).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = TIMEFRAME_SECONDS[timeframe]
    epoch = int(value.astimezone(timezone.utc).timestamp())
    closed_start_epoch = (epoch // seconds) * seconds - seconds
    return datetime.fromtimestamp(
        closed_start_epoch, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_closed_kline(
    con: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    evaluation_ts_utc: str,
) -> sqlite3.Row | None:
    """Read the exact latest candle that had closed at the decision snapshot."""
    return con.execute(
        "SELECT * FROM kline_cache "
        "WHERE symbol=? AND tf=? AND ts=? LIMIT 1",
        (symbol, timeframe, closed_bar_cutoff(evaluation_ts_utc, timeframe)),
    ).fetchone()


def closed_kline_readiness(
    con: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    evaluation_ts_utc: str,
) -> tuple[sqlite3.Row | None, bool]:
    """Use the same exact-bar and warm-up contract as the execution gate."""
    cutoff = closed_bar_cutoff(evaluation_ts_utc, timeframe)
    row = latest_closed_kline(con, symbol, timeframe, evaluation_ts_utc)
    validation = validate_kline_row(row)
    bars_seen = int(
        con.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM kline_cache "
            "WHERE symbol=? AND tf=? AND ts<=? LIMIT ?)",
            (
                symbol,
                timeframe,
                cutoff,
                MINIMUM_BARS_FOR_FULL_INDICATORS,
            ),
        ).fetchone()[0]
    )
    return (
        row,
        bool(
            validation["ready"]
            and bars_seen >= MINIMUM_BARS_FOR_FULL_INDICATORS
        ),
    )


def quote_volume_usd(last, contracts_24h, contract_value) -> float | None:
    """OKX linear-swap quote turnover: price * contracts * ctVal."""
    try:
        values = (float(last), float(contracts_24h), float(contract_value))
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(value) or value <= 0 for value in values):
        return None
    turnover = values[0] * values[1] * values[2]
    return turnover if math.isfinite(turnover) else None


def candidate_market_rows(
    con: sqlite3.Connection,
    tick_ts: str,
) -> list[sqlite3.Row]:
    """Anchor candidates to one tick; only same-timestamp derivatives may join."""
    return con.execute(
        "SELECT t.symbol,t.last,t.chg24h,t.vol24h,i.ctVal,"
        "d.oi_usd,d.funding_rate FROM tick_snapshots t "
        "LEFT JOIN derivatives d ON d.symbol=t.symbol AND d.ts=t.ts "
        "JOIN instruments_cache i ON i.instId=t.symbol "
        "WHERE t.ts=? AND t.chg24h IS NOT NULL "
        "AND i.ctVal IS NOT NULL AND i.ctVal>0",
        (tick_ts,),
    ).fetchall()


def current_cycle_contract_oi_evidence(
    con: sqlite3.Connection,
    cycle_id: str | None,
    expected_symbols,
    *,
    available_at: str | None = None,
) -> dict:
    """Read valid direct OI facts from the exact current contract-stat cycle.

    This is a read-only visibility fallback for the briefing when the current
    derivatives snapshot missed OI. It never accepts carry-forward rows and
    never changes the independent batch audit, including its single-timestamp
    requirement.
    """
    expected = {
        str(symbol) for symbol in expected_symbols if str(symbol).strip()
    }
    result = {
        "source": CONTRACT_STATS_SOURCE,
        "cycle_id": str(cycle_id or ""),
        "expected_symbols": len(expected),
        "valid_symbols": 0,
        "coverage_rate": 0.0,
        "collected_timestamp_count": 0,
        "maximum_source_age_minutes": None,
        "values": {},
        "invalid_reason_counts": {},
        "status": "UNAVAILABLE",
        "semantics": (
            "exact-cycle direct official rows only; carry-forward excluded; "
            "read-only evidence visibility; independent batch audit unchanged"
        ),
    }
    if not cycle_id or not expected:
        result["reason"] = "exact_cycle_or_expected_universe_missing"
        return result
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='market_contract_statistics'"
        ).fetchone()
        if not exists:
            result["reason"] = "market_contract_statistics_missing"
            return result
        rows = con.execute(
            "SELECT ts,collected_ts,cycle_id,symbol,timeframe,"
            "oi_contracts,oi_ccy,oi_usd,taker_sell_usd,taker_buy_usd,"
            "taker_buy_ratio,raw,source FROM market_contract_statistics "
            "WHERE cycle_id=? AND timeframe='15m' AND source=?",
            (str(cycle_id), CONTRACT_STATS_SOURCE),
        ).fetchall()
    except sqlite3.Error as exc:
        result["reason"] = f"sqlite_error:{type(exc).__name__}"
        return result

    visible_text = available_at or datetime.now(timezone.utc).isoformat()
    try:
        visible_time = datetime.fromisoformat(
            str(visible_text).replace("Z", "+00:00"))
        if visible_time.tzinfo is None:
            visible_time = visible_time.replace(tzinfo=timezone.utc)
        visible_time = visible_time.astimezone(timezone.utc)
    except (TypeError, ValueError):
        result["reason"] = "invalid_available_at"
        return result

    values: dict[str, float] = {}
    collected_timestamps: set[str] = set()
    source_ages: list[float] = []
    invalid_counts: dict[str, int] = {}

    def reject(reason: str) -> None:
        invalid_counts[reason] = invalid_counts.get(reason, 0) + 1

    for row in rows:
        symbol = str(row[3])
        if symbol not in expected:
            reject("unexpected_symbol")
            continue
        method = contract_statistics_row_method(row)
        if method not in CONTRACT_STATS_DIRECT_METHODS:
            reject(f"method:{method}")
            continue
        issues = contract_statistics_row_issues(
            row,
            available_at=visible_time.isoformat(),
            expected_symbol=symbol,
            maximum_source_lag_seconds=CONTRACT_STATS_PRIMARY_MAX_AGE_S,
        )
        if issues:
            for issue in issues:
                reject(issue)
            continue
        try:
            oi_usd = float(row[7])
            source_time = datetime.fromisoformat(
                str(row[0]).replace("Z", "+00:00"))
            if source_time.tzinfo is None:
                source_time = source_time.replace(tzinfo=timezone.utc)
            source_time = source_time.astimezone(timezone.utc)
        except (TypeError, ValueError):
            reject("invalid_oi_or_source_time")
            continue
        if not math.isfinite(oi_usd) or oi_usd <= 0:
            reject("nonpositive_oi_usd")
            continue
        values[symbol] = oi_usd
        collected_timestamps.add(str(row[1]))
        source_ages.append(
            max(0.0, (visible_time - source_time).total_seconds() / 60.0)
        )

    result.update({
        "valid_symbols": len(values),
        "coverage_rate": len(values) / len(expected),
        "collected_timestamp_count": len(collected_timestamps),
        "maximum_source_age_minutes": (
            max(source_ages) if source_ages else None),
        "values": values,
        "invalid_reason_counts": invalid_counts,
        "status": "AVAILABLE" if values else "UNAVAILABLE",
    })
    if not values:
        result["reason"] = "no_valid_direct_current_cycle_rows"
    return result


def resolve_candidate_oi_usd(
    derivative_oi_usd,
    symbol: str,
    contract_evidence: dict,
) -> tuple[float | None, str]:
    """Prefer instantaneous OI; otherwise use a validated same-cycle fact."""
    try:
        value = float(derivative_oi_usd)
    except (TypeError, ValueError):
        value = None
    if value is not None and math.isfinite(value) and value > 0:
        return value, "derivatives_current"
    fallback = (contract_evidence.get("values") or {}).get(str(symbol))
    try:
        value = float(fallback)
    except (TypeError, ValueError):
        value = None
    if value is not None and math.isfinite(value) and value > 0:
        return value, "contract_statistics_current_cycle_direct"
    return None, "unavailable"


def catalyst_freshness(event_day, now: datetime | None = None) -> tuple[str, str]:
    """Event-date freshness; observation time is deliberately not an input."""
    if not event_day:
        return "unknown", "事件年龄未知"
    try:
        day = datetime.strptime(str(event_day)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "unknown", "事件年龄未知"
    today = (now or datetime.now(CST)).astimezone(CST).date()
    age_days = (today - day).days
    if age_days < 0:
        return "scheduled", f"距已排期事件 {-age_days} 天"
    if age_days == 0:
        return "fresh", "事件日=今天"
    if age_days <= 2:
        return "recent", f"事件已发生 {age_days} 天"
    return "stale", f"事件已发生 {age_days} 天"


def section(title):
    print(f"\n## {title}")


def safe(fn):
    try:
        fn()
    except Exception as e:
        print(f"  N/A（{type(e).__name__}: {str(e)[:60]}）")


def _row_get(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


def _json_object(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


# 2026-08-19 F4：路径分档口径（trade_experiences 现成字段，此前一处都不用）。
# 实测近 30 天已平仓：MAE<0.2R n=40 胜率 57.5% +169.32U；MAE 0.8-1.0R n=30
# 胜率 6.7% -157.72U；MFE<0.2R n=54 胜率 9.3% -170.68U；MFE>=2R n=23 胜率
# 60.9% +139.06U。判别力远超既有 regime×方向/时段/资产类别三组切片。
_R_BUCKET_EDGES = ((0.2, "<0.2R"), (0.5, "0.2-0.5R"),
                   (0.8, "0.5-0.8R"), (1.0, "0.8-1.0R"))
_R_BUCKET_ORDER = ("<0.2R", "0.2-0.5R", "0.5-0.8R", "0.8-1.0R",
                   ">=1.0R", ">=2.0R", "unknown")


def _r_bucket(value, top_label=">=1.0R"):
    """把 R 化路径值分档；None/不可解析一律 'unknown'，绝不当 0。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if num != num:
        return "unknown"
    for edge, label in _R_BUCKET_EDGES:
        if num < edge:
            return label
    if top_label == ">=2.0R" and num >= 2.0:
        return ">=2.0R"
    return ">=1.0R"


# 2026-08-19 F5③：playbook 陈旧阈值。account.db.playbook 最后更新
# 2026-06-10（69 天前，249 条），而 trade_experiences.playbook_ref 全库仅
# 1/189 条非空 → stats_ready 恒 False，该段每轮固定输出约 10 行零信息，
# 还给 LLM 一个「有剧本可用」的错觉。超期即整段收起（函数与
# update_playbook_stats.py 原样保留，回填 playbook_ref 后自然复活）。
PLAYBOOK_MAX_AGE_DAYS = 30

# 段落实际渲染状态。末尾那句「本简报已含…」是给 Agent 的免查询承诺，
# 一旦某段自我声明不可用（如 playbook 陈旧收起），承诺里就不能再列它——
# 「声称含某段 + 该段说自己没内容」是 F5 要消灭的同一类自相矛盾。
_SECTION_STATE: dict[str, bool] = {}


def _calibration_group_stats(items, field, preferred_order=()):
    grouped = {}
    for item in items:
        grouped.setdefault(item[field], []).append(item)
    labels = [label for label in preferred_order if label in grouped]
    labels.extend(sorted(label for label in grouped if label not in labels))
    result = {}
    for label in labels:
        group_items = grouped[label]
        values = [item["pnl_pct"] for item in group_items]
        realized = [
            item["realized_pnl"] for item in group_items
            if item["realized_pnl"] is not None
        ]
        wins = sum(value > 0 for value in values)
        result[label] = {
            "n": len(values),
            "wins": wins,
            "losses": len(values) - wins,
            "win_rate_pct": wins / len(values) * 100,
            "avg_pnl_pct": sum(values) / len(values),
            "pnl_sum_pct": sum(values),
            "realized_pnl_n": len(realized),
            "realized_pnl_sum_usdt": sum(realized) if realized else None,
        }
    return result


def experience_calibration(rows, asset_classes=None, analysis_usage=None):
    """Build deterministic self-calibration facts from closed experiences.

    Historical narrative is never parsed for counts or decision type.  Usage is
    read only from the structured decision-card field; asset class prefers the
    frozen v2 experience vector and falls back to the current instrument map.
    """
    asset_classes = asset_classes or {}
    analysis_usage = analysis_usage or {}
    items = []
    asset_fallback = 0
    usage_source_counts = {
        "analysis_signal": 0,
        "trade_receipt": 0,
        "unknown": 0,
    }
    for row in rows:
        try:
            pnl = float(_row_get(row, "pnl_pct"))
        except (TypeError, ValueError):
            continue
        try:
            realized_pnl = float(_row_get(row, "realized_pnl"))
        except (TypeError, ValueError):
            realized_pnl = None
        raw = _json_object(_row_get(row, "raw"))
        card = raw.get("decision_card")
        card = card if isinstance(card, dict) else {}
        history = card.get("historical_experience")
        history = history if isinstance(history, dict) else {}
        usage_key = (
            str(_row_get(row, "cycle_id", "")),
            str(_row_get(row, "symbol", "")).upper(),
        )
        usage = str(analysis_usage.get(usage_key) or "").strip().lower()
        if usage in _CALIBRATION_USAGE_ORDER[:-1]:
            usage_source_counts["analysis_signal"] += 1
        else:
            usage = str(history.get("usage") or "").strip().lower()
            if usage in _CALIBRATION_USAGE_ORDER[:-1]:
                usage_source_counts["trade_receipt"] += 1
            else:
                usage = "unknown"
                usage_source_counts["unknown"] += 1
        if usage not in _CALIBRATION_USAGE_ORDER:
            usage = "unknown"

        hold = _row_get(row, "hold_hours")
        try:
            hold_value = float(hold)
        except (TypeError, ValueError):
            hold_value = None
        if hold_value is None or hold_value < 0:
            hold_bucket = "unknown"
        elif hold_value < 4:
            hold_bucket = "<4h"
        elif hold_value < 24:
            hold_bucket = "4-24h"
        elif hold_value < 48:
            hold_bucket = "24-48h"
        else:
            hold_bucket = ">=48h"

        vector = _json_object(_row_get(row, "experience_vector"))
        features = vector.get("features")
        features = features if isinstance(features, dict) else {}
        asset_class = str(features.get("asset_class") or "").strip()
        if not asset_class:
            asset_class = str(asset_classes.get(
                str(_row_get(row, "symbol", "")), "unknown") or "unknown")
            asset_fallback += 1
        regime_label = str(_row_get(row, "regime", "unknown") or "unknown")
        alignment = _trend_alignment(
            _row_get(row, "side"),
            features.get("trend_1h"), features.get("trend_4h"))
        items.append({
            "pnl_pct": pnl,
            "realized_pnl": realized_pnl,
            "usage": usage,
            # F4：入场质量/路径质量/出场通道 —— 全部来自已冻结字段，零新增采集。
            "entry_quality": _r_bucket(_row_get(row, "mae_r")),
            "path_quality": _r_bucket(_row_get(row, "mfe_r"), ">=2.0R"),
            "exit_channel": str(
                _row_get(row, "exit_category", "unknown") or "unknown"),
            "regime_side": (
                f"{regime_label}/"
                f"{_row_get(row, 'side', 'unknown')}"
            ),
            "hold_bucket": hold_bucket,
            "asset_class": asset_class,
            "open_hour_bucket": _open_hour_bucket(_row_get(row, "ts")),
            "regime_alignment": f"{regime_label}/{alignment}",
        })

    alignment_order = tuple(
        f"{regime}/{label}"
        for regime in sorted({item["regime_alignment"].rsplit("/", 1)[0]
                              for item in items})
        for label in _CALIBRATION_ALIGNMENT_LABELS
    )
    return {
        "sample_n": len(items),
        "history_usage": _calibration_group_stats(
            items, "usage", _CALIBRATION_USAGE_ORDER),
        "regime_side": _calibration_group_stats(items, "regime_side"),
        "regime_alignment": _calibration_group_stats(
            items, "regime_alignment", alignment_order),
        "open_hour_bucket": _calibration_group_stats(
            items, "open_hour_bucket", _CALIBRATION_HOUR_ORDER),
        "hold_bucket": _calibration_group_stats(
            items, "hold_bucket", _CALIBRATION_HOLD_ORDER),
        "asset_class": _calibration_group_stats(items, "asset_class"),
        # F4：三组路径切片（MAE=进场后最大逆行 / MFE=最大顺行 / 出场通道）。
        "entry_quality": _calibration_group_stats(
            items, "entry_quality", _R_BUCKET_ORDER),
        "path_quality": _calibration_group_stats(
            items, "path_quality", _R_BUCKET_ORDER),
        "exit_channel": _calibration_group_stats(items, "exit_channel"),
        "asset_class_current_map_fallback_n": asset_fallback,
        "history_usage_source_counts": usage_source_counts,
        "decision_driver_available": False,
        "actor_cohort_available": False,
    }


def _dxy_observation_rows(reg, limit: int = DXY_OBSERVATION_WINDOW):
    """返回按 FRED observation date 去重的 DTWEXBGS 真实观测。

    ``cross_market`` 是小时级快照，周频 DTWEXBGS 会被 carry-forward 成数百行。
    ``source_meta.dxy.source_as_of`` 才是 FRED 观测日期；没有该字段的旧行不能
    冒充真实观测进入 z-score。
    """
    source_date = "json_extract(source_meta,'$.dxy.source_as_of')"
    return reg.execute(
        f"SELECT {source_date} AS observation_date, dxy, MAX(ts) AS last_ts "
        "FROM cross_market WHERE dxy IS NOT NULL "
        f"AND {source_date} IS NOT NULL "
        "GROUP BY observation_date ORDER BY observation_date DESC LIMIT ?",
        (limit,),
    ).fetchall()


def _dxy_zone_state(current, observations, frozen_days: int) -> dict:
    """基于真实周观测给出软标签；carry-forward 过久时 fail-open 为 STALE。"""
    rows = [row for row in observations if _row_get(row, "dxy") is not None]
    state = {
        "status": "UNKNOWN",
        "z": None,
        "raw_std": None,
        "n": len(rows),
        "reason": "observation_sample_insufficient",
    }
    if frozen_days >= DXY_CARRY_STALE_DAYS:
        state.update(status="STALE", reason="carry_forward_stale")
        return state
    if len(rows) < DXY_MIN_OBSERVATIONS:
        return state
    values = [float(_row_get(row, "dxy")) for row in rows]
    if abs(values[0] - float(current)) > 1e-9:
        state["reason"] = "latest_observation_mismatch"
        return state
    mean = sum(values) / len(values)
    raw_std = (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5
    state["raw_std"] = raw_std
    if raw_std <= 0:
        state["reason"] = "zero_observation_variance"
        return state
    z = (float(current) - mean) / raw_std
    zone = "EXTREME" if z > 1.5 else ("ELEVATED" if z > 0.75 else "NORMAL")
    state.update(status=zone, z=z, reason="true_observation_zscore")
    return state


def _parse_playbook_time(row) -> datetime | None:
    """playbook.ts有历史垃圾格式；优先ts，失败后回退updated_utc。"""
    for key in ("ts", "updated_utc"):
        raw = str(_row_get(row, key, "") or "").strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(CST) if dt.tzinfo else dt.replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def _playbook_scope(row, known_bases: set[str]) -> tuple[set[str], set[str]]:
    """从旧文本中提取regime和目标币。首句优先，避免正文背景BTC/ETH造成误匹配。"""
    summary = str(_row_get(row, "summary", "") or "")
    evidence = str(_row_get(row, "evidence", "") or "")
    category = str(_row_get(row, "category", "") or "")
    full = f"{category} {summary} {evidence}".lower()
    regimes = {token for token in REGIME_TOKENS
               if re.search(rf"(?<![a-z_]){re.escape(token)}(?![a-z_])", full)}

    def symbols(text: str) -> set[str]:
        tokens = set(re.findall(r"(?<![A-Z0-9])([A-Z][A-Z0-9]{1,14})(?![A-Z0-9])",
                                text.upper()))
        return tokens & known_bases

    lead = re.split(r"[。\n；;]", summary, maxsplit=1)[0]
    scoped = symbols(lead)
    if not scoped:
        scoped = symbols(summary)
    return regimes, scoped


def select_playbook_matches(rows, current_regime: str | None,
                            context_bases: set[str], known_bases: set[str],
                            now: datetime | None = None,
                            limit: int = 6) -> tuple[list, dict]:
    """上下文匹配playbook；未验证条目按类别TTL过期，不改写历史实体。"""
    now = now or datetime.now(CST)
    selected = []
    stats = {"deprecated": 0, "expired": 0, "regime_mismatch": 0,
             "symbol_mismatch": 0, "eligible": 0}
    for row in rows:
        category = str(_row_get(row, "category", "") or "")
        if "deprecated" in category.lower():
            stats["deprecated"] += 1
            continue
        n = int(_row_get(row, "win_count", 0) or 0) + int(_row_get(row, "loss_count", 0) or 0)
        if n < 5:
            created = _parse_playbook_time(row)
            ttl = (PLAYBOOK_HYPOTHESIS_TTL_DAYS
                   if "hypothesis" in category.lower() else PLAYBOOK_OTHER_TTL_DAYS)
            if created is None or (now - created).total_seconds() > ttl * 86400:
                stats["expired"] += 1
                continue
        regimes, symbols = _playbook_scope(row, known_bases)
        if regimes and current_regime and current_regime not in regimes:
            stats["regime_mismatch"] += 1
            continue
        if symbols and not symbols.intersection(context_bases):
            stats["symbol_mismatch"] += 1
            continue
        relevance = (4 if symbols.intersection(context_bases) else 0) + \
                    (2 if current_regime and current_regime in regimes else 0) + \
                    min(n, 20) / 20
        selected.append((relevance, int(_row_get(row, "id", 0) or 0), row))
        stats["eligible"] += 1
    selected.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in selected[:limit]], stats


def _playbook_context(mkt: sqlite3.Connection, acc: sqlite3.Connection,
                      focus_file: Path, top: int) -> tuple[set[str], set[str]]:
    """当前品种集合=主流币+持仓+focus+涨跌榜+资金费极值。"""
    known = {
        str(r["instId"]).split("-")[0].upper()
        for r in mkt.execute("SELECT instId FROM instruments_cache WHERE instId LIKE '%-USDT-SWAP'")
    }
    context = {"BTC", "ETH", "SOL"} & known

    for profile in ("live",):
        latest = acc.execute(
            "SELECT ts FROM position_snapshots WHERE profile=? "
            "ORDER BY ts DESC,rowid DESC LIMIT 1",
            (profile,),
        ).fetchone()
        if latest:
            context.update(
                str(r["symbol"]).split("-")[0].upper()
                for r in acc.execute(
                    "SELECT symbol FROM position_snapshots WHERE profile=? AND ts=? "
                    "AND symbol!='__FLAT__'", (profile, latest["ts"])
                )
            )

    try:
        text = focus_file.read_text(encoding="utf-8").upper()
        context.update(set(re.findall(r"\b[A-Z][A-Z0-9]{1,14}\b", text)) & known)
    except (OSError, UnicodeError):
        pass

    tick_ts = mkt.execute("SELECT MAX(ts) m FROM tick_snapshots").fetchone()["m"]
    if tick_ts:
        ticks = mkt.execute(
            "SELECT t.symbol,t.chg24h,t.last,t.vol24h,i.ctVal FROM tick_snapshots t "
            "LEFT JOIN instruments_cache i ON i.instId=t.symbol "
            "WHERE t.ts=? AND t.chg24h IS NOT NULL", (tick_ts,)
        ).fetchall()
        liquid = [r for r in ticks
                  if (quote_volume_usd(r["last"], r["vol24h"], r["ctVal"]) or 0)
                  >= MIN_QUOTE_VOL_USD]
        movers = sorted(liquid, key=lambda r: r["chg24h"], reverse=True)[:top]
        movers += sorted(liquid, key=lambda r: r["chg24h"])[:top]
        context.update(str(r["symbol"]).split("-")[0].upper() for r in movers)

    deriv_ts = mkt.execute("SELECT MAX(ts) m FROM derivatives").fetchone()["m"]
    if deriv_ts:
        extremes = mkt.execute(
            "SELECT symbol FROM derivatives WHERE ts=? AND funding_rate IS NOT NULL "
            "ORDER BY ABS(funding_rate) DESC LIMIT ?", (deriv_ts, top)
        ).fetchall()
        context.update(str(r["symbol"]).split("-")[0].upper() for r in extremes)
    return context & known, known


def _btc_structure_line(mkt_con, dxy_d1) -> str:
    """当前 BTC 4H 结构态一行，与 regime 预报标签同框对照。

    regime 标签是取反的「未来24h均值回归预报」（regime_classifier 的
    btc_orientation=-1：结构越多头标签越偏 down），裸打标签曾让 agent 把
    上涨段的多头候选误判成「逆大势」。这里给出未取反的结构分作当前态；
    特征不足时如实报缺，不沿用、不推断。
    """
    try:
        from regime_classifier import classify_regime
        rows = mkt_con.execute(
            "SELECT ts,c,ma5,ma20,rsi14 FROM kline_cache "
            "WHERE symbol='BTC-USDT-SWAP' AND tf='4H' ORDER BY ts DESC LIMIT 7"
        ).fetchall()
        if len(rows) < 7 or not rows[0][1] or not rows[6][1]:
            return "  当前BTC 4H结构: 不可计算（4H 历史不足 7 根，如实缺失）"
        res = classify_regime(
            close=rows[0][1], ma5=rows[0][2], ma20=rows[0][3],
            rsi14=rows[0][4], return_24h=rows[0][1] / rows[6][1] - 1.0,
            dxy_d1=dxy_d1,
        )
        if not res.get("ok"):
            return (
                "  当前BTC 4H结构: 不可计算"
                f"（{res.get('reason') or '特征缺失'}，如实缺失）"
            )
        score = int(res["btc_structure_score"])
        word = ("上行" if score >= 2 else "偏上" if score == 1 else
                "中性" if score == 0 else "偏下" if score == -1 else "下行")
        f = res["features"]
        return (
            f"  当前BTC 4H结构: {word}(结构分{score:+d}/±4)"
            f" | 价格vsMA20 {f['price_vs_ma20']:+.2%}"
            f" | MA5vsMA20 {f['ma_spread']:+.2%}"
            f" | 24h动量 {f['return_24h']:+.2%}"
            f" | RSI {f['rsi14']:.1f}"
        )
    except Exception as exc:  # noqa: BLE001 - 展示行失败不拖垮简报
        return f"  当前BTC 4H结构: 计算失败（{type(exc).__name__}，如实报错）"


def _render(
    root,
    top,
    cycle_id=None,
    candidate_out_file=None,
    ready_pool_out_file=None,
):
    minimal_cycle = bool(
        cycle_id and thresholds.minimal_decision_contract_active(cycle_id))
    closure_cycle = bool(
        cycle_id and thresholds.minimal_contract_closure_active(cycle_id))
    if candidate_out_file is not None or ready_pool_out_file is not None:
        if not cycle_id or opportunity_state_version_for_cycle(cycle_id) is None:
            raise ValueError(
                "candidate artifact generation is unavailable before the "
                "forward activation boundary")
        observed = datetime.now(CST)
        current_slot = observed.replace(
            minute=(observed.minute // 15) * 15,
            second=0, microsecond=0,
        ).strftime("%Y-%m-%dT%H:%M")
        if str(cycle_id) != current_slot:
            raise ValueError(
                "historical candidate artifact regeneration is refused; "
                "existing facts remain immutable")
    now_cst = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    print(f"# 决策简报 @ {now_cst} (UTC+8)")

    mkt = connect(root, "market.db")
    acc = connect(root, "account.db")
    # V2.0 (2026-06-26) Option A: cross_market 已切 regime.db 单写；regime 相关读走 reg
    # （regime.db 不可用时回退 mkt）。其余行情段仍读 market.db。
    try:
        reg = connect(root, "regime.db")
    except Exception:
        reg = mkt

    # ── 1. 宏观 / regime ──────────────────────────────
    section("宏观 / regime")

    def s_macro():
        # V2.0 (2026-06-26): 当前 regime 改从 regime.db 优先读（market.db 按 ts 兜底）——见 _regime_read。
        # 24h regime 序列（下方变更检测）仍读 market.db（完整历史，regime.db 仅 seed+新双写行）。
        try:
            from _regime_read import latest_cross_market as _lcm
            r = _lcm(root)
        except Exception:
            r = reg.execute("SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1").fetchone()
        if not r:
            print("  无数据")
            return
        def d1(v):
            return "None" if v is None else f"{v:+.4f}"
        # 2026-08-26 语义修正（方案A）：只改展示语义并补当前结构态；
        # 标签本值、写库口径、find_similar_experience --regime 分桶均不变。
        print(
            f"  regime=**{r['regime']}** @ {fmt_age(r['ts'])} 前"
            + (
                "（只作宏观观察，不产生方向或周期判断）"
                if minimal_cycle else
                "（该标签=未来24h均值回归预报，BTC 4H 结构越多头越偏 down，"
                "**不是当前趋势方向**；当前趋势与顺逆势以各标的 1H/4H 旗标"
                "和 MTF 证据为准）"
            )
        )
        if not minimal_cycle:
            print(_btc_structure_line(
                mkt, r["dxy_d1"] if "dxy_d1" in r.keys() else None))
        print(
            f"  USD_BROAD(DTWEXBGS; legacy字段=dxy，非ICE DXY) "
            f"{r['dxy']}（d1 {d1(r['dxy_d1'])}） | "
            f"VIX {r['vix']}（d1 {d1(r['vix_d1'])}） | "
            f"SPX {r['spx']}（d1 {d1(r['spx_d1'])}）"
        )
        public_snapshot = {}
        try:
            from public_macro import latest_snapshot as _latest_public_macro
            public_snapshot = _latest_public_macro(reg)
        except Exception:
            pass
        dxy_calc_row = public_snapshot.get("dxy_calc_ecb") or {}
        dxy_calc_value = dxy_calc_row.get("value")
        dxy_calc_d1 = public_snapshot.get("dxy_calc_ecb_d1")
        if dxy_calc_value is None and "dxy_calc_ecb" in r.keys():
            dxy_calc_value = r["dxy_calc_ecb"]
            dxy_calc_d1 = r["dxy_calc_ecb_d1"]
        fear_row = public_snapshot.get("fear_greed") or {}
        fear_value = fear_row.get("value")
        fear_label = fear_row.get("label")
        if fear_value is None and "fear_greed" in r.keys():
            fear_value = r["fear_greed"]
            fear_label = r["fear_greed_label"]
        print(
            "  DXY_CALC_ECB "
            + (
                f"{dxy_calc_value:.3f}（d1 {d1(dxy_calc_d1)}；"
                f"as_of={dxy_calc_row.get('observation_date') or '?'}；非ICE官方报价）"
                if isinstance(dxy_calc_value, (int, float))
                else "未采到（ECB六币种按ICE公式复算，非ICE官方报价）"
            )
            + " | 恐贪指数 "
            + (
                f"{fear_value:.0f}/{fear_label or '?'} "
                f"(as_of={fear_row.get('observation_date') or '?'}, Alternative.me)"
                if isinstance(fear_value, (int, float))
                else "未采到"
            )
        )
        fed_row = public_snapshot.get("fed_funds") or {}
        fed_value = fed_row.get("value")
        print(
            "  美联储利率(DFF有效联邦基金利率) "
            + (
                f"{fed_value:.2f}%/年 (as_of={fed_row.get('observation_date') or '?'}, "
                "FRED官方；日频约1个工作日滞后)"
                if isinstance(fed_value, (int, float))
                else "未采到（FRED DFF，macro_observations）"
            )
        )
        # 兼容键 dxy_zone 实际基于 USD_BROAD(DTWEXBGS) 的真实 FRED 周观测。
        def _dxy_zone():
            if r["dxy"] is None:
                return
            observations = _dxy_observation_rows(reg)
            # 日历行只用于判断本地 carry-forward 了几天，绝不进入 z-score。
            days = reg.execute(
                "SELECT substr(ts,1,10) AS d, dxy, MAX(ts) AS _mt FROM cross_market "
                "WHERE dxy IS NOT NULL GROUP BY d ORDER BY d DESC LIMIT 20"
            ).fetchall()
            cur = r["dxy"]
            frozen = 0
            for x in days:
                if x["dxy"] == cur:
                    frozen += 1
                else:
                    break
            frozen_s = f"{frozen}" if frozen < len(days) else f"≥{frozen}"
            prev = observations[1] if len(observations) > 1 else None
            if prev is None:
                delta_s = "真实观测中无第二个取值"
            else:
                delta = cur - prev["dxy"]
                delta_s = (f"前值 {prev['dxy']}（as_of={prev['observation_date']}）→ 现值 {cur}，"
                           f"{delta:+.4f} = {delta / prev['dxy'] * 100:+.3f}%")
            state = _dxy_zone_state(cur, observations, frozen)
            newest_as_of = (_row_get(observations[0], "observation_date", "?")
                            if observations else "?")
            if state["status"] == "STALE":
                print(f"  dxy_zone=**STALE**（兼容键，实际=USD_BROAD/DTWEXBGS；"
                      f"FRED 周频值在本地已连续 carry-forward {frozen_s} 天，"
                      "不出 zone 档位）")
                print(f"    {delta_s}；真实观测 n={state['n']}，最新 as_of={newest_as_of}；"
                      "carry-forward 日历行不进入 z-score")
            elif state["status"] == "UNKNOWN":
                print(f"  dxy_zone=UNKNOWN（兼容键，实际=USD_BROAD/DTWEXBGS；"
                      f"真实观测 n={state['n']}，reason={state['reason']}，不出 zone 档位）")
                print(f"    {delta_s}；最新 as_of={newest_as_of}；"
                      "缺 source_as_of 的旧 carry-forward 行不冒充观测")
            else:
                print(f"  dxy_zone=**{state['status']}**（兼容键，实际=USD_BROAD/DTWEXBGS；"
                      f"真实周观测 n={state['n']}，z={state['z']:+.2f}，"
                      f"判据 z>1.5=EXTREME / z>0.75=ELEVATED；"
                      f"最新 as_of={newest_as_of}）")
                print(f"    {delta_s}；真实观测 std={state['raw_std']:.4f}；"
                      "carry-forward 日历行不进入 z-score")
            print("  ➤ zone 处置：EXTREME/ELEVATED/STALE 均只作为方向或反对证据；不自动减仓、"
                  "不决定仓位、不禁开。Agent 可采纳、部分采纳或忽略并说明理由。")
        _dxy_zone()
        mcap_chg = r["btc_mcap_chg_24h_usd"]
        etf_s = f"{mcap_chg/1e9:+.2f}B" if mcap_chg is not None else "None"
        dom = r["btc_dominance"]
        print(f"  BTC市值Δ24h(≠ETF净流) {etf_s} | BTC.D {f'{dom:.2f}' if dom is not None else '?'}% | TVL {r['defillama_tvl_total'] and round(r['defillama_tvl_total']/1e9,1)}B")
        true_etf = (
            r["btc_etf_net_flow_usd"]
            if "btc_etf_net_flow_usd" in r.keys()
            else None
        )
        confirmed_etf = public_snapshot.get("etf_confirmed") or {}
        provisional_etf = public_snapshot.get("etf_provisional") or {}
        if confirmed_etf.get("value") is not None:
            true_etf = confirmed_etf["value"]
            print(
                f"  BTC ETF真实净流: ${true_etf/1e6:+.1f}M "
                f"(as_of={confirmed_etf.get('observation_date')}; "
                "Farside+SoSoValue cross_checked)"
            )
        elif provisional_etf.get("value") is not None:
            print(
                f"  BTC ETF净流 provisional: "
                f"${provisional_etf['value']/1e6:+.1f}M "
                f"(as_of={provisional_etf.get('observation_date')}; "
                f"source={provisional_etf.get('source')}; 单源未进硬字段)"
            )
        else:
            print("  BTC ETF真实净流: 未采到（禁止用市值变化代理）")
        # 2026-08-19 D4：类型化陈旧闸 —— ETF >3 天、FRED >5 天直接标 STALE。
        # 陈旧不是「值不可用」，是「不得当作当下读数」；只外显，不自动降权。
        # 实测 btc_etf_net_flow_usd 的 as_of 卡在 2026-08-03（15 天）、值恒
        # 170,100,000，而 carried_forward 是空数组 —— 陈旧此前完全不可见。
        _today = datetime.now(CST).date()
        for _label, _col, _limit in (("ETF", "btc_etf_as_of", 3),
                                     ("DXY", "dxy_as_of", 5),
                                     ("VIX", "vix_as_of", 5),
                                     ("SPX", "spx_as_of", 5)):
            _as_of = r[_col] if _col in r.keys() else None
            if not _as_of:
                print(f"  ⚠️ {_label} 观测日 N/A（未采到 source_as_of，"
                      "不得当作当下读数）")
                continue
            try:
                _days = (_today - datetime.strptime(
                    str(_as_of)[:10], "%Y-%m-%d").date()).days
            except (TypeError, ValueError):
                _days = None
            if _days is not None and _days > _limit:
                print(f"  ⚠️ {_label} STALE: as_of={_as_of}（{_days}天 > "
                      f"{_limit}天阈值），按 N/A 处理，禁止当作当下读数")
        if "carried_forward" in r.keys() and r["carried_forward"] not in (None, "", "[]"):
            print(f"  ⚠️ 本轮沿用旧宏观值: {r['carried_forward']}")
        if r["dxy_d1"] is None or r["spx_d1"] is None:
            print("  ⚠️ 宏观缺值按降级语义处理：值缺失才权重=0，d1 缺失不降权")
        # K4 (2026-06-13): regime 切换提示——刚转向时警惕惯性持仓与旧 regime 不符
        seq = reg.execute(
            "SELECT ts, regime FROM cross_market WHERE ts >= datetime('now','-1 day') "
            "AND regime IS NOT NULL ORDER BY ts"
        ).fetchall()
        changes = [(seq[i]["ts"], seq[i - 1]["regime"], seq[i]["regime"])
                   for i in range(1, len(seq)) if seq[i]["regime"] != seq[i - 1]["regime"]]
        if changes:
            lts, frm, to = changes[-1]
            age = age_min(lts) or 9999
            if age < 180:  # L3 (2026-06-14): 仅 <3h 算"刚切换"，治 1021min 仍报惯性期的噪音
                print(f"  ⚡ regime {len(changes)}次切换/24h；最近 {frm}→{to} @ {fmt_age(lts)} 前"
                      f"（刚转向 <3h，警惕惯性持仓与新 regime 不符）")
            else:
                print(f"  regime {len(changes)}次切换/24h；现 {to}（距上次切换 {fmt_age(lts)}，惯性期已过）")
        elif seq:
            print(f"  regime 24h 稳定（{seq[-1]['regime']}）")

        # 高重要度事件只作风险窗口输入，不直接产生方向信号。
        try:
            # 2026-08-19 D1：窗口改 [CST now-2h, CST now+12h]。事件真正影响
            # 价格的是发布瞬间与其后 1-2h，而不是 8h 之后的日程表；已发生的
            # 直接给 actual vs forecast，未发生的给 T-XXm 倒计时。
            # event_ts 是 UTC+8 字符串，datetime('now','+8 hours') 即 CST now。
            events = reg.execute(
                "SELECT event_ts,region,event,importance,forecast,previous,actual "
                "FROM macro_events WHERE importance>=2 AND datetime(event_ts) "
                "BETWEEN datetime('now','+8 hours','-2 hours') "
                "AND datetime('now','+8 hours','+12 hours') "
                "ORDER BY datetime(event_ts) LIMIT 6"
            ).fetchall()
            if events:
                now_cst = datetime.now(CST)
                print("  事件窗（-2h ~ +12h，importance≥2）:")
                for e in events:
                    try:
                        ets = datetime.strptime(
                            str(e["event_ts"])[:19], "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=CST)
                        mins = (ets - now_cst).total_seconds() / 60.0
                        eta = (f"T{mins:+.0f}m" if abs(mins) < 600
                               else f"T{mins / 60:+.1f}h")
                    except (TypeError, ValueError):
                        eta = "T?"
                    act = str(e["actual"] or "").strip()
                    fc = str(e["forecast"] or "").strip()
                    # surprise 只做并列展示；单位/口径各异，禁在此做数值归一。
                    tail = (f"actual={act} vs fc={fc or '-'} "
                            f"prev={e['previous'] or '-'}" if act
                            else f"未发布 fc={fc or '-'} prev={e['previous'] or '-'}")
                    print(f"    [{eta}] imp{e['importance']} {e['event_ts']} "
                          f"{e['region']} {e['event']} | {tail}")
        except Exception:
            pass
    safe(s_macro)

    # ── 2. 行情纵览（chg24h 已落列） ─────────────────
    section(f"行情 Top{top}（流动性≥${MIN_QUOTE_VOL_USD/1e6:.0f}M）")

    def s_ticks():
        ts = mkt.execute("SELECT MAX(ts) AS m FROM tick_snapshots").fetchone()["m"]
        # 2026-08-19 D5：vol24h 是**张数**不是币量，成交额必须乘 ctVal
        # （唯一口径 = quote_volume_usd）。旧式 vol24h*last 让 BTC(ctVal=0.01)
        # 被低估 100× 而闸掉、多数山寨(ctVal>1)被高估而放进来，与 §2.6 候选池
        # 的口径互相矛盾。ctVal 缺失的新币按不合格处理（收紧是正确方向）。
        rows = mkt.execute(
            "SELECT t.symbol,t.last,t.chg24h,t.vol24h,i.ctVal FROM tick_snapshots t "
            "LEFT JOIN instruments_cache i ON i.instId=t.symbol "
            "WHERE t.ts=? AND t.chg24h IS NOT NULL",
            (ts,),
        ).fetchall()
        liq = []
        for r in rows:
            _qv = quote_volume_usd(r["last"], r["vol24h"], r["ctVal"])
            if _qv is not None and _qv >= MIN_QUOTE_VOL_USD:
                liq.append(r)
        for tag in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
            m = next((r for r in rows if r["symbol"] == tag), None)
            if m:
                print(f"  {tag.split('-')[0]} ${m['last']:,.0f} ({m['chg24h']:+.2f}%)", end="")
        print(f" | 快照 {len(rows)} 币 @ {fmt_age(ts)} 前")
        gain = sorted(liq, key=lambda r: r["chg24h"], reverse=True)[:top]
        lose = sorted(liq, key=lambda r: r["chg24h"])[:top]
        print("  涨: " + " ".join(f"{r['symbol'].split('-')[0]}{r['chg24h']:+.1f}%" for r in gain))
        print("  跌: " + " ".join(f"{r['symbol'].split('-')[0]}{r['chg24h']:+.1f}%" for r in lose))
    safe(s_ticks)

    # ── 2.5 技术面（BTC/ETH 多周期，K1a 替代 agent 自查 K 线） ──
    if not minimal_cycle:
        section("技术面（BTC/ETH 多周期）")

    def s_tech():
        evaluation_ts = mkt.execute(
            "SELECT MAX(ts) FROM tick_snapshots"
        ).fetchone()[0]
        if not evaluation_ts:
            print("  暂无行情快照")
            return
        for sym in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
            parts = []
            for tf in DECISION_TIMEFRAMES:
                k, ready = closed_kline_readiness(
                    mkt, sym, tf, evaluation_ts)
                if not ready:
                    parts.append(f"{tf}:N/A")
                    continue
                trend = "↑MA" if (k["ma20"] and k["c"] > k["ma20"]) else "↓MA"
                rsi = f"RSI{k['rsi14']:.0f}" if k["rsi14"] is not None else "RSI?"
                macd = "MACD+" if (k["macd_hist"] or 0) > 0 else "MACD-"
                # 2026-08-13 扩展指标（迁移后才有列/值；缺列/缺值不显示）
                keys = k.keys()
                boll = ""
                if ("boll20_up" in keys and k["boll20_up"] is not None
                        and k["boll20_dn"] is not None and k["c"] is not None):
                    if k["c"] > k["boll20_up"]:
                        boll = "/BOLL↑破上轨"
                    elif k["c"] < k["boll20_dn"]:
                        boll = "/BOLL↓破下轨"
                    else:
                        boll = "/BOLL内"
                obv = ""
                if "obv" in keys and k["obv"] is not None:
                    prev_rows = mkt.execute(
                        "SELECT obv FROM kline_cache WHERE symbol=? AND tf=? "
                        "AND ts<? AND obv IS NOT NULL "
                        "ORDER BY ts DESC LIMIT 5",
                        (sym, tf, k["ts"]),
                    ).fetchall()
                    if prev_rows:
                        delta = k["obv"] - prev_rows[-1]["obv"]
                        obv = ("/OBV↑" if delta > 0
                               else ("/OBV↓" if delta < 0 else "/OBV→"))
                parts.append(f"{tf} {trend}/{rsi}/{macd}{boll}{obv}")
            print(f"  {sym.split('-')[0]}: " + " | ".join(parts))
        print("  （↑/↓MA=价在MA20上/下；BOLL=收盘相对布林带(20,2)位置；"
              "OBV↑/↓=对比近5根已收盘的量能方向(窗口内累计,仅方向/背离参考)；"
              "扩展指标缺列=迁移未跑；候选山寨币历史相似度仍须单独 find_similar_history）")
    if not minimal_cycle:
        safe(s_tech)

    # ── 2.6 全市场候选（旧周期保留双流动性闸，新策略仅观察额/OI） ──
    if cycle_id and thresholds.minimal_decision_contract_active(cycle_id):
        section("全市场无方向候选（无三周期判断；Agent选择side）")
    elif cycle_id and thresholds.decision_restriction_removal_active(cycle_id):
        section("全市场可交易候选（成交额/OI仅观察；全量资格+有界review slice）")
    else:
        section(
            f"高流动性可交易候选（成交额≥${MIN_QUOTE_VOL_USD/1e6:.0f}M "
            f"且 OI≥${MIN_OI_USD/1e6:.0f}M；成熟趋势+早期结构两组）"
        )

    def s_tradeable_candidates():
        relaxed_policy = bool(
            cycle_id and thresholds.decision_restriction_removal_active(cycle_id))
        minimal_policy = bool(
            cycle_id and thresholds.minimal_decision_contract_active(cycle_id))
        closure_policy = bool(
            cycle_id and thresholds.minimal_contract_closure_active(cycle_id))
        tick_ts = mkt.execute("SELECT MAX(ts) AS m FROM tick_snapshots").fetchone()["m"]
        if not tick_ts:
            print("  暂无数据")
            return

        # 已有 live 仓由持仓管理段覆盖；这里专门给空余资金提供新的、可执行的标的池。
        held = set()
        pts = acc.execute(
            "SELECT ts FROM position_snapshots WHERE profile='live' "
            "ORDER BY ts DESC,rowid DESC LIMIT 1"
        ).fetchone()
        if pts:
            held = {
                str(r["symbol"])
                for r in acc.execute(
                    "SELECT symbol FROM position_snapshots "
                    "WHERE profile='live' AND ts=? AND symbol!='__FLAT__'",
                    (pts["ts"],),
                )
            }

        rows = candidate_market_rows(mkt, tick_ts)

        contract_oi = current_cycle_contract_oi_evidence(
            mkt,
            cycle_id,
            (r["symbol"] for r in rows),
            available_at=datetime.now(timezone.utc).isoformat(),
        )
        ranked = []
        # 2026-08-19 D5：候选漏斗计数。此前对不合格标的一律静默 continue ——
        # 「本轮只有 3 个候选」到底是市场没机会还是 K 线缓存没补齐，外部完全
        # 不可分。只计数、不改任何闸门。
        funnel = {
            "universe": len(rows), "liq_pass": 0, "oi_pass": 0,
            "oi_derivatives": 0, "oi_contract_stats_fallback": 0,
            "oi_unavailable": 0, "ready_pass": 0, "held_excluded": 0,
            "held_observed": 0,
        }
        for source_row in rows:
            r = dict(source_row)
            quote_vol = quote_volume_usd(r["last"], r["vol24h"], r["ctVal"])
            if not relaxed_policy and (
                    quote_vol is None or quote_vol < MIN_QUOTE_VOL_USD):
                continue
            quote_vol = float(quote_vol or 0.0)
            funnel["liq_pass"] += 1
            candidate_oi, candidate_oi_source = resolve_candidate_oi_usd(
                r["oi_usd"], r["symbol"], contract_oi)
            if candidate_oi_source == "derivatives_current":
                funnel["oi_derivatives"] += 1
            elif candidate_oi_source == "contract_statistics_current_cycle_direct":
                funnel["oi_contract_stats_fallback"] += 1
            else:
                funnel["oi_unavailable"] += 1
            if not relaxed_policy and (
                    candidate_oi is None or candidate_oi < MIN_OI_USD):
                continue
            if candidate_oi is None:
                candidate_oi = 0.0
                candidate_oi_source = "unavailable_observation"
            funnel["oi_pass"] += 1
            r["candidate_oi_usd"] = candidate_oi
            r["candidate_oi_source"] = candidate_oi_source
            is_held, exclude_held = held_candidate_disposition(
                r["symbol"], held, closure_policy=closure_policy)
            r["currently_held_observation"] = is_held
            if is_held:
                funnel["held_observed"] += 1
            if exclude_held:
                funnel["held_excluded"] += 1
                continue
            if minimal_policy:
                funnel["ready_pass"] += 1
                ranked.append({
                    "row": r,
                    "parts": [],
                    "timeframe_metrics": None,
                    "atr1h": None,
                    "atr1h_close": None,
                    "trend_vote": None,
                    "votes": None,
                    "bias": "方向待定",
                    "opportunity_state": "SIDE_NEUTRAL",
                    "opportunity_side": None,
                    "eligible_sides": ["long", "short"],
                    "entry_timing": None,
                    "trend_strength": None,
                    "quote_vol": quote_vol,
                    "currently_held_observation": is_held,
                    "rank_version": (
                        SIDE_NEUTRAL_OPPORTUNITY_RANK_VERSION
                        if closure_policy else SIDE_NEUTRAL_RANK_VERSION),
                })
                continue
            parts, votes, timeframe_metrics, all_ready = [], {}, {}, True
            # D5：1H atr14 是止损距硬约束（live_trader.md 第 6 条 ≥3×ATR）的
            # 唯一库内来源，而简报此前一个 atr 字都没有（grep -ci atr = 0）。
            atr1h = None
            atr1h_close = None
            for tf in DECISION_TIMEFRAMES:
                k, ready = closed_kline_readiness(
                    mkt, r["symbol"], tf, tick_ts)
                if not ready:
                    parts.append(f"{tf}:N/A")
                    all_ready = False
                    continue
                if tf == "1H":
                    atr1h = k["atr14"] if "atr14" in k.keys() else None
                    atr1h_close = k["c"]
                above = k["ma20"] is not None and k["c"] > k["ma20"]
                macd_up = k["macd_hist"] is not None and k["macd_hist"] > 0
                votes[tf] = (1 if above else -1) + (1 if macd_up else -1)
                timeframe_metrics[tf] = {
                    "ts": k["ts"],
                    "close": k["c"],
                    "ma20": k["ma20"],
                    "atr14": k["atr14"] if "atr14" in k.keys() else None,
                    "rsi14": k["rsi14"],
                    "macd_hist": k["macd_hist"],
                    "vote": votes[tf],
                }
                trend = "↑MA" if above else "↓MA"
                rsi = f"R{k['rsi14']:.0f}" if k["rsi14"] is not None else "R?"
                macd = "M+" if macd_up else "M-"
                parts.append(f"{tf}{trend}/{rsi}/{macd}")
            if not all_ready:
                continue
            funnel["ready_pass"] += 1
            trend_vote = sum(votes.values())
            state, opportunity_side, entry_timing = classify_opportunity_state(
                votes, timeframe_metrics,
                require_four_hour_direction=not relaxed_policy)
            bias = (
                "偏多" if opportunity_side == "long" else
                "偏空" if opportunity_side == "short" else "混合")
            ranked.append({
                "row": r,
                "parts": parts,
                "timeframe_metrics": timeframe_metrics,
                "atr1h": atr1h,
                "atr1h_close": atr1h_close,
                "trend_vote": trend_vote,
                "votes": votes,
                "bias": bias,
                "opportunity_state": state,
                "opportunity_side": opportunity_side,
                "entry_timing": entry_timing,
                "trend_strength": trend_strength_snapshot(
                    votes, opportunity_side),
                "quote_vol": quote_vol,
            })

        # 轮换事实必须与候选顺序一起冻结进 exact-cycle manifest；否则后续
        # 审计只能依赖渲染文本，无法证明“近6h未深挖至少覆盖1个”。
        try:
            dig_history = load_dig_history(root, as_of_cycle=cycle_id)
        except Exception:
            dig_history = {}

        current_regime = None
        current_regime_source_ts = None
        if cycle_id:
            try:
                from _regime_read import regime_at
                current_regime, current_regime_source_ts = regime_at(
                    root, str(cycle_id).replace("T", " ") + ":00")
            except Exception:
                current_regime = None
                current_regime_source_ts = None

        previous_ready = load_previous_ready_pool(
            ready_pool_out_file, cycle_id) if cycle_id else {}
        state_version = opportunity_state_version_for_cycle(cycle_id)
        closure_review_slice = None
        if minimal_policy:
            for candidate in ranked:
                symbol = str(candidate["row"]["symbol"])
                histories = [
                    dig_history.get((symbol, side)) or {}
                    for side in ("long", "short")
                ]
                candidate["dig_history"] = max(
                    histories,
                    key=lambda item: str(item.get("last_cycle") or ""),
                    default={},
                )
                candidate["state_version"] = SIDE_NEUTRAL_STATE_VERSION
            if closure_policy:
                rank_micro_map = {}
                try:
                    rank_micro_ts = mkt.execute(
                        "SELECT MAX(ts) AS m FROM market_microstructure"
                    ).fetchone()["m"]
                    if rank_micro_ts:
                        rank_micro_rows = mkt.execute(
                            "SELECT m.symbol,m.spread_bps,m.imbalance_25bp,"
                            "m.buy_slippage_500usd_bps,"
                            "m.sell_slippage_500usd_bps,"
                            "f.taker_buy_ratio,f.cvd_notional_usd "
                            "FROM market_microstructure m "
                            "LEFT JOIN market_trade_flow f "
                            "ON f.ts=m.ts AND f.symbol=m.symbol WHERE m.ts=?",
                            (rank_micro_ts,),
                        ).fetchall()
                        rank_micro_age = age_min(rank_micro_ts)
                        if (
                            rank_micro_age is not None
                            and -1.0 <= rank_micro_age
                            <= CANDIDATE_MICRO_MAXIMUM_AGE_MINUTES
                        ):
                            rank_micro_map = {
                                str(row["symbol"]): row
                                for row in rank_micro_rows}
                except (sqlite3.Error, TypeError, ValueError):
                    rank_micro_map = {}
                ranked, closure_review_slice = (
                    rank_side_neutral_opportunities(
                        ranked,
                        cycle_id=cycle_id,
                        micro_map=rank_micro_map,
                        review_limit=(
                            thresholds.RELAXED_DECISION_SLICE_MAXIMUM),
                    )
                )
            else:
                ranked.sort(key=lambda item: str(item["row"]["symbol"]))
                if ranked:
                    slot = int(
                        datetime.strptime(str(cycle_id), "%Y-%m-%dT%H:%M")
                        .replace(tzinfo=CST).timestamp() // (15 * 60)
                    )
                    offset = (
                        slot * thresholds.RELAXED_DECISION_SLICE_MAXIMUM
                    ) % len(ranked)
                    ranked = ranked[offset:] + ranked[:offset]
                for index, candidate in enumerate(ranked, start=1):
                    symbol = str(candidate["row"]["symbol"])
                    candidate["rank_key"] = (len(ranked) - index, symbol)
                    candidate["rank_version"] = SIDE_NEUTRAL_RANK_VERSION
        else:
            for candidate in ranked:
                symbol = str(candidate["row"]["symbol"])
                candidate["dig_history"] = dig_history.get((
                    symbol, candidate.get("opportunity_side"))) or {}
                enrich_opportunity_context(
                    candidate, cycle_id, current_regime,
                    previous_ready.get(symbol),
                    tick_ts=tick_ts,
                    regime_source_ts=current_regime_source_ts,
                    state_version=state_version)
                candidate["rank_key"] = candidate_rank_key(
                    candidate, candidate["dig_history"],
                    relaxed_policy=relaxed_policy)
                candidate["rank_version"] = (
                    RELAXED_CANDIDATE_RANK_VERSION
                    if relaxed_policy else CANDIDATE_RANK_VERSION)
                if candidate["opportunity_state"] in {
                        "TRIGGERING", "EARLY_WATCH"}:
                    candidate["early_side"] = candidate["opportunity_side"]

            ranked.sort(key=lambda x: x["rank_key"], reverse=True)
        mature_pool = [
            item for item in ranked
            if item["opportunity_state"] in {"ENTRY_READY", "EXTENDED"}
        ]
        early_pool = [
            item for item in ranked
            if item["opportunity_state"] in {"TRIGGERING", "EARLY_WATCH"}
        ]
        manifest_ordered = None
        review_slice = None
        if minimal_policy:
            manifest_ordered = list(ranked)
            review_slice = (
                list(closure_review_slice)
                if closure_policy and closure_review_slice is not None
                else manifest_ordered[
                    :thresholds.RELAXED_DECISION_SLICE_MAXIMUM]
            )
            picked = list(review_slice)
            early = []
        elif relaxed_policy:
            manifest_ordered = [
                item for item in ranked
                if item.get("opportunity_side") in {"long", "short"}
            ]
            review_slice = manifest_ordered[
                :thresholds.RELAXED_DECISION_SLICE_MAXIMUM]
            picked = [
                item for item in review_slice
                if item["opportunity_state"] in {"ENTRY_READY", "EXTENDED"}]
            early = [
                item for item in review_slice
                if item["opportunity_state"] in {"TRIGGERING", "EARLY_WATCH"}]
        else:
            picked = balanced_candidate_pick(
                mature_pool, TRADEABLE_CANDIDATE_COUNT)
            early = balanced_candidate_pick(
                early_pool, EARLY_STRUCTURE_CANDIDATE_COUNT)

        ready_pool_reference = None
        if cycle_id:
            try:
                ready_pool_reference = write_ready_pool_artifact(
                    cycle_id=cycle_id,
                    tick_ts=tick_ts,
                    ranked=ranked,
                    picked=picked,
                    early=early,
                    manifest_ordered=manifest_ordered,
                    review_slice=review_slice,
                    out_file=ready_pool_out_file,
                    previous_ready=previous_ready,
                )
            except Exception as pool_exc:  # noqa: BLE001 - evidence failure is visible
                ready_pool_reference = {
                    "schema": READY_POOL_SCHEMA,
                    "status": "ERROR",
                    "path": str(ready_pool_out_file or ""),
                    "sha256": None,
                    "ready_count": len(ranked),
                    "selected_count": len(picked) + len(early),
                    "review_count": len(picked) + len(early),
                    "error": f"{type(pool_exc).__name__}:{pool_exc}",
                }
                print(
                    f"[WARN] 全ready候选工件写入失败: {type(pool_exc).__name__}",
                    file=sys.stderr)

        # 候选快照 JSONL（2026-08-18）：错失池/连续候选轮数的确定性来源。
        # 两层皆空也写空 candidates 行（有效观察，供 streak 正确断链）；
        # 必须先于下方空候选 early-return。
        candidate_manifest = None
        if cycle_id:
            try:
                candidate_manifest = append_candidate_snapshot(
                    root, cycle_id, picked, early, tick_ts,
                    candidate_out_file=candidate_out_file,
                    dig_history=dig_history,
                    ready_pool_reference=ready_pool_reference,
                    manifest_ordered=manifest_ordered,
                    review_slice=review_slice)
            except Exception as snap_exc:  # noqa: BLE001 - 快照失败不碰简报
                print(
                    f"[WARN] 候选快照写入失败: {type(snap_exc).__name__}",
                    file=sys.stderr)

        if not picked and not early:
            print(
                "  无方向可判定且技术数据完整的review候选"
                if relaxed_policy else
                "  无符合双流动性闸且技术数据完整的候选")
            return

        # 连续候选轮数 v2（2026-08-18）：每 cycle 独立 session、无跨轮记忆——
        # 「已连续候选 N 轮未成交」必须由简报外显。08-15 吞吐契约后
        # analysis_signals 无 wait 行，旧口径只会数到冻结历史行 → 改读候选
        # 快照；快照尚无积累（部署初期）就不显示，不回退旧假数。
        streaks = {}
        try:
            if minimal_policy:
                raise LookupError("side-neutral candidates have no side streak")
            syms = [str(x["row"]["symbol"]) for x in picked + early]
            last_trades = {}
            if syms:
                try:
                    lt = connect(root, "live_trades.db")
                    try:
                        marks = ",".join("?" for _ in syms)
                        for r in lt.execute(
                                "SELECT symbol, MAX(cycle_id) AS c "
                                f"FROM trades WHERE symbol IN ({marks}) "
                                "GROUP BY symbol", syms):
                            last_trades[str(r["symbol"])] = str(r["c"] or "")
                    finally:
                        lt.close()
                except Exception:
                    last_trades = {}
                for sym, pair in candidate_streak_v2(
                        root, syms, last_trades).items():
                    if pair[0] >= WAIT_STREAK_MIN_DISPLAY:
                        streaks[sym] = pair
        except Exception:
            streaks = {}

        candidate_symbols = {
            str(x["row"]["symbol"]) for x in picked + early
        }
        micro_map = {}
        micro_ts = None
        try:
            micro_ts = mkt.execute(
                "SELECT MAX(ts) AS m FROM market_microstructure"
            ).fetchone()["m"]
            if micro_ts:
                micro_rows = mkt.execute(
                    "SELECT m.symbol,m.spread_bps,m.imbalance_25bp,"
                    "m.buy_slippage_500usd_bps,m.sell_slippage_500usd_bps,"
                    "f.taker_buy_ratio,f.cvd_notional_usd,"
                    "f.sample_count,f.sample_span_ms "
                    "FROM market_microstructure m "
                    "LEFT JOIN market_trade_flow f "
                    "ON f.ts=m.ts AND f.symbol=m.symbol WHERE m.ts=?",
                    (micro_ts,),
                ).fetchall()
                micro_age = age_min(micro_ts)
                if (
                    micro_age is not None
                    and -1.0 <= micro_age <= CANDIDATE_MICRO_MAXIMUM_AGE_MINUTES
                ):
                    micro_map = {
                        str(row["symbol"]): row for row in micro_rows
                        if str(row["symbol"]) in candidate_symbols
                    }
        except (sqlite3.Error, TypeError, ValueError):
            micro_map = {}
            micro_ts = None

        positioning_map = {}
        positioning_collected_ts = None
        positioning_batch_ok = False
        try:
            latest_positioning = mkt.execute(
                "SELECT collected_ts FROM market_positioning "
                "WHERE source=? AND timeframe='1H' "
                "ORDER BY datetime(collected_ts) DESC LIMIT 1",
                (POSITIONING_SOURCE,),
            ).fetchone()
            if latest_positioning:
                positioning_collected_ts = latest_positioning["collected_ts"]
                positioning_rows = mkt.execute(
                    "SELECT symbol,ts,long_ratio,short_ratio,long_short_ratio "
                    "FROM market_positioning WHERE collected_ts=? "
                    "AND source=? AND timeframe='1H' ORDER BY symbol",
                    (positioning_collected_ts, POSITIONING_SOURCE),
                ).fetchall()
                expected_symbols = {
                    str(row["symbol"])
                    for row in mkt.execute(
                        "SELECT DISTINCT symbol FROM tick_snapshots "
                        "WHERE ts=? AND symbol LIKE '%-USDT-SWAP'",
                        (tick_ts,),
                    ).fetchall()
                }
                positioning_batch_ok = positioning_evidence_quality(
                    positioning_rows, expected_symbols)["status"] == "PASSED"
                if positioning_batch_ok:
                    positioning_map = {
                        str(row["symbol"]): row for row in positioning_rows
                        if str(row["symbol"]) in candidate_symbols
                    }
        except (sqlite3.Error, TypeError, ValueError):
            positioning_map = {}
            positioning_collected_ts = None
            positioning_batch_ok = False

        soft_evidence = {
            symbol: candidate_soft_evidence(
                micro_map.get(symbol), positioning_map.get(symbol),
                positioning_batch_passed=positioning_batch_ok,
            )
            for symbol in candidate_symbols
        }

        # 2026-08-28 轮换注解复用上方已冻结进 manifest 的同一份近6h事实。
        candidate_id_map = {
            str(item.get("symbol")): str(item.get("candidate_id"))
            for item in ((candidate_manifest or {}).get("candidates") or [])
            if isinstance(item, dict) and item.get("candidate_id")
        }

        def cand_line(x, bias_label):
            r = x["row"]
            funding = (
                f"{r['funding_rate']*100:+.4f}%"
                if r["funding_rate"] is not None else "N/A"
            )
            st = streaks.get(r["symbol"])
            streak_s = f" 连续候选{st[0]}轮未成交({st[1] or '?'})" if st else ""
            # D5：确定性算术，不是下单指令。08-11 起 55 张 open 卡止损距/ATR
            # 中位仅 1.63×、53/55 <3×，而手册要求 ≥3×ATR14(1H) —— 要它守的
            # 规则此前没给它数据。缺 ATR 显 N/A，不猜。
            _atr, _close = x.get("atr1h"), x.get("atr1h_close")
            atr_s = (
                f" ATR1H={_atr:.4g}(={_atr / _close * 100:.2f}%)"
                f" 3×ATR止损距={_atr * 3 / _close * 100:.2f}%"
                if _atr and _close else " ATR1H=N/A")
            soft = soft_evidence.get(
                str(r["symbol"]), candidate_soft_evidence())
            oi_source = (
                "即时" if r["candidate_oi_source"] == "derivatives_current"
                else "同轮15m直采"
                if r["candidate_oi_source"] == (
                    "contract_statistics_current_cycle_direct")
                else "N/A"
            )
            turnover_s = (
                f"${x['quote_vol']/1e6:.0f}M"
                if x.get("quote_vol") else "N/A")
            oi_s = (
                f"${r['candidate_oi_usd']/1e6:.0f}M"
                if r.get("candidate_oi_usd") else "N/A")
            dh = dig_history.get((
                str(r["symbol"]), x.get("opportunity_side")))
            if dh:
                dig_s = (
                    f" [近6h已深挖{dh['n']}次·上次"
                    + ("开仓" if str(dh["last_decision"]).startswith("open")
                       else "淘汰")
                    + "]"
                )
            else:
                dig_s = " [近6h未深挖]"
            candidate_id = candidate_id_map.get(
                str(r["symbol"]), "candidate_id=N/A")
            if minimal_policy:
                prefix = (
                    f"[review#{x.get('review_ordinal') or 'N/A'}]"
                    if closure_policy else f"[{candidate_id}]")
                score_text = (
                    f" score={float(x.get('opportunity_score') or 0.0):.3f}"
                    f" pick={x.get('selection_reason') or 'N/A'}"
                    if closure_policy else "")
                held_text = (
                    " held=是(仅观察)"
                    if closure_policy and bool(
                        x.get("currently_held_observation")
                        or r.get("currently_held_observation"))
                    else " held=否" if closure_policy else ""
                )
                return (
                    f"  {prefix} {r['symbol']} side=Agent选择(long|short) "
                    f"{_candidate_price_text(r.get('last'))} "
                    f"涨跌{r['chg24h']:+.1f}% 额{turnover_s} "
                    f"OI{oi_s}({oi_source}) 资金费率={funding} "
                    f"| {soft['text']}{score_text}{held_text}"
                )
            state = str(x.get("opportunity_state") or "N/A")
            trend_score = (x.get("trend_strength") or {}).get("score")
            timing_score = (x.get("entry_timing") or {}).get("timing_score")
            return (
                f"  [{candidate_id}] {r['symbol']} {bias_label} "
                f"state={state} trend={trend_score} timing={timing_score} "
                f"涨跌{r['chg24h']:+.1f}% 额{turnover_s} "
                f"OI{oi_s}({oi_source}) "
                f"资金费率={funding}{streak_s}"
                f"{atr_s} | "
                + " ".join(x["parts"])
                + f" | {soft['text']}{dig_s}"
            )

        print(
            f"  候选漏斗: 本轮宇宙 {funnel['universe']} / "
            + ("成交额可观测 " if relaxed_policy else "液性闸过 ")
            + f"{funnel['liq_pass']} / "
            + ("OI可观测或N/A纳入 " if relaxed_policy else "OI闸过 ")
            + f"{funnel['oi_pass']}"
            f"（即时 {funnel['oi_derivatives']} / 同轮15m直采 "
            f"{funnel['oi_contract_stats_fallback']} / 无有效OI "
            f"{funnel['oi_unavailable']}） / "
            + (
                f"全市场资格 {funnel['ready_pass']} / 已持仓观察 "
                f"{funnel['held_observed']}（不排除，方向由Agent选择）"
                if closure_policy else
                f"无三周期门资格 {funnel['ready_pass']} / 持仓排除 "
                f"{funnel['held_excluded']}（方向由Agent选择）"
                if minimal_policy else
                f"三周期就绪 {funnel['ready_pass']} / 持仓排除 "
                f"{funnel['held_excluded']}（就绪落差=K线缓存未补齐，非市场无机会）"
            )
        )
        if relaxed_policy:
            print(
                "  全量资格manifest="
                f"{(candidate_manifest or {}).get('candidate_count', 0)} / "
                "本轮展示与完整决策slice="
                f"{(candidate_manifest or {}).get('review_slice_count', 0)}；"
                "slice上限只是既有deep耗时预算，不是准入或方向配额")
            if closure_policy:
                print(
                    "  review选择=机会优先+少量轮换；成交额/OI仅参与连续排序，"
                    "无催化、已有仓位数或未触顶IMR均不得单独reject；"
                    "full manifest内任何symbol均可OPEN。")
        if funnel["oi_contract_stats_fallback"]:
            print(
                "  OI替代证据: 同一 cycle 直接官方合约统计逐行校验通过 "
                f"{contract_oi['valid_symbols']}/{contract_oi['expected_symbols']}，"
                f"来源年龄最大 {contract_oi['maximum_source_age_minutes']:.1f}m，"
                f"collected_ts 批次数 {contract_oi['collected_timestamp_count']}；"
                "carry 行禁用，且不改变独立批次审计结论。"
            )
        print(
            "  「无方向review」不使用15m/1H/4H判断，最终side由Agent在轻量OPEN中选择:"
            if minimal_policy else
            "  「入场阶段」严格三周期同向：ENTRY_READY 优先，EXTENDED 降序观察:")
        if picked:
            for x in picked:
                print(cand_line(x, x["bias"]))
        else:
            print("    无")
        if not minimal_policy:
            print(
                "  「形成阶段」三周期加权方向：TRIGGERING 优先，EARLY_WATCH 继续观察:"
                if relaxed_policy else
                "  「形成阶段」4H 立向：TRIGGERING 优先，EARLY_WATCH 继续观察:")
            if early:
                for x in early:
                    print(cand_line(
                        x, "早多" if x["early_side"] == "long" else "早空"))
            else:
                print("    无（本轮无 4H 已立向但尚未严格三周期同向的候选）")

        # 2026-08-28 轮换菜单：给「近6h未深挖」候选一个一眼可选的清单。
        undug = list(dict.fromkeys(
            str(x["row"]["symbol"]) for x in list(picked) + list(early)
            if minimal_policy or (
                str(x["row"]["symbol"]), x.get("opportunity_side"))
            not in dig_history
        ))
        if undug:
            print(
                "  近6h未深挖候选（轮换菜单：本轮深挖集合至少含其中 1 个；"
                "排程事实，非开仓指令）: "
                + " ".join(s.split("-")[0] for s in undug)
            )
        else:
            print("  近6h未深挖候选: 无（本轮具名候选近 6h 均已深挖过）")

        micro_covered = sum(
            bool(item["micro_available"]) for item in soft_evidence.values())
        positioning_covered = sum(
            bool(item["positioning_available"])
            for item in soft_evidence.values()
        )
        print(
            "  候选软证据覆盖: "
            f"微观={micro_covered}/{len(candidate_symbols)}"
            f"@{fmt_age(micro_ts) if micro_ts else 'N/A'}前，"
            f"官方账户多空比={positioning_covered}/{len(candidate_symbols)}"
            f"@{fmt_age(positioning_collected_ts) if positioning_collected_ts else 'N/A'}前；"
            "缺失只记N/A，不作为否决或反向证据"
        )

        try:
            les = connect(root, "lessons.db")
            try:
                mo = les.execute(
                    "SELECT direction_hint AS d, COUNT(*) AS n, "
                    "SUM(would_hit_1r_fixed2pct) AS hit "
                    "FROM missed_opportunities WHERE ts LIKE '202%' "
                    "AND ts>=datetime('now','-7 days') GROUP BY direction_hint"
                ).fetchall()
            finally:
                les.close()
            mo_parts = []
            for r in mo:
                if r["d"] not in ("long", "short") or not r["n"]:
                    continue
                mo_parts.append(
                    f"{r['d']} {int(r['hit'] or 0)}/{r['n']}"
                    f"={(r['hit'] or 0) / r['n'] * 100:.0f}%"
                )
            if mo_parts:
                print("  错失池7d触及+2%率(fixed2pct路径盲代理，不校验先触SL): "
                      + " | ".join(mo_parts)
                      + "（提示方向机会不对称，非下单指令）")
        except Exception:
            pass
        print(
            "  （无三周期方向/state/layer；全市场symbol按轮换进入review，最终long|short由Agent选择；"
            "任何行情、新闻、历史与微观只作观察，真钱动作仍经facts/risk/executor硬闸）"
            if minimal_policy else
            "  （两组仅候选来源不同，均非下单指令；连续候选轮数=该标的连续"
            "出现在候选层且期间无任何成交的简报轮数（2026-08-18 起读候选快照"
            "，不再依赖 wait 信号落库）。该轮数只是系统事实，供权衡时间成本与"
            "证据新旧，不构成开仓或不开仓的规则（2026-08-28 起明确：判断维度"
            "无成文否决线）。统一 live 仍须结合新闻、历史相似度、风险回报和"
            "组合暴露自主决断；绝对24h涨跌不再获得排序奖励，趋势强度与入场时机"
            "分开外显；新策略下四态review项均须显式provisional_open或可复算量化veto，"
            "最终仍经facts/risk/executor硬闸，不等于盲目自动下单）"
        )
    safe(s_tradeable_candidates)

    # ── 3. 衍生品极值 ────────────────────────────────
    section("衍生品极值（资金费 8h）")

    def s_deriv():
        # D2：迁移可能尚未在本机跑过，先探列再决定 SELECT（旧库不报错）。
        _deriv_has_basis = "basis_bp" in {
            str(c[1]) for c in mkt.execute(
                "PRAGMA table_info(derivatives)").fetchall()}
        ts = mkt.execute("SELECT MAX(ts) AS m FROM derivatives").fetchone()["m"]
        rows = mkt.execute(
            # D2：basis_bp 按列存在性容错（迁移未跑的旧库降级为 NULL）。
            "SELECT symbol,funding_rate,premium,oi,oi_usd"
            + (",basis_bp" if _deriv_has_basis else ",NULL AS basis_bp")
            + " FROM derivatives WHERE ts=? AND funding_rate IS NOT NULL",
            (ts,),
        ).fetchall()
        ext = sorted(rows, key=lambda r: abs(r["funding_rate"]), reverse=True)[:top]
        for r in ext:
            ann = r["funding_rate"] * 3 * 365 * 100
            oi_s = f" OI ${r['oi_usd']/1e6:.0f}M" if r["oi_usd"] else ""
            delta_parts = []
            if r["oi_usd"]:
                for label, offset in (("1h", "-1 hour"), ("24h", "-24 hours")):
                    prev = mkt.execute(
                        "SELECT oi_usd FROM derivatives WHERE symbol=? AND oi_usd IS NOT NULL "
                        "AND datetime(ts)<=datetime(?,?) ORDER BY datetime(ts) DESC LIMIT 1",
                        (r["symbol"], ts, offset),
                    ).fetchone()
                    if prev and prev["oi_usd"]:
                        delta_parts.append(
                            f"{label} {(r['oi_usd']/prev['oi_usd']-1)*100:+.1f}%")
            delta_s = (" Δ" + "/".join(delta_parts)) if delta_parts else ""
            # D2：basis = mark − index，即时拥挤度读数。
            _basis = r["basis_bp"] if "basis_bp" in r.keys() else None
            basis_s = (f" basis={_basis:+.1f}bp"
                       if isinstance(_basis, (int, float)) else " basis=N/A")
            print(f"  {r['symbol'].split('-')[0]} 资金费率 {r['funding_rate']*100:+.4f}%（年化{ann:+.0f}%）{oi_s}{delta_s}{basis_s}")
        print(f"  （{len(rows)} 币 @ {fmt_age(ts)} 前；极端正费率=多头拥挤，反之亦然。"
              "资金费率(funding)是 8h 结算的滞后读数，basis(mark−index) 是同一拥挤度的"
              "即时读数；两者背离＝拥挤刚形成或刚出清）")
    safe(s_deriv)

    # ── 3b. 市场微观结构（影子特征） ─────────────────
    section("微观结构（50档参考特征，仅作决策证据）")

    def s_micro():
        ts = mkt.execute("SELECT MAX(ts) AS m FROM market_microstructure").fetchone()["m"]
        if not ts:
            print("  暂无数据")
            return
        rows = mkt.execute(
            "SELECT m.*,f.taker_buy_ratio,f.cvd_notional_usd,f.sample_count,f.sample_span_ms "
            "FROM market_microstructure m LEFT JOIN market_trade_flow f "
            "ON f.ts=m.ts AND f.symbol=m.symbol WHERE m.ts=? "
            "ORDER BY CASE m.symbol WHEN 'BTC-USDT-SWAP' THEN 0 "
            "WHEN 'ETH-USDT-SWAP' THEN 1 WHEN 'SOL-USDT-SWAP' THEN 2 ELSE 3 END "
            "LIMIT 8", (ts,)
        ).fetchall()
        for r in rows:
            depth25 = (r["bid_depth_25bp_usd"] or 0) + (r["ask_depth_25bp_usd"] or 0)
            span_s = (r["sample_span_ms"] or 0) / 1000
            flow = (f"买盘={r['taker_buy_ratio']:.0%} CVD=${r['cvd_notional_usd']/1e3:+.0f}K "
                    f"样本={r['sample_count']}/{span_s:.0f}s"
                    if r["taker_buy_ratio"] is not None else "流=N/A")
            slip = (f"{r['buy_slippage_500usd_bps']:.2f}/{r['sell_slippage_500usd_bps']:.2f}bp"
                    if r["buy_slippage_500usd_bps"] is not None
                    and r["sell_slippage_500usd_bps"] is not None else "N/A")
            print(f"  {r['symbol'].split('-')[0]} 点差={r['spread_bps']:.2f}bp "
                  f"深度±25bp=${depth25/1e3:.0f}K 失衡={r['imbalance_25bp']:+.2f} "
                  f"滑点$500(买/卖)={slip} {flow}")
        print(f"  @ {fmt_age(ts)} 前；逐笔流为最近最多500笔样本，样本跨度随成交活跃度变化")
    safe(s_micro)

    # ── 3c. OKX 官方REST多空账户比（影子软证据） ─────
    section("多空账户比（OKX官方REST全宇宙1H，软证据）")

    def s_positioning():
        latest = mkt.execute(
            "SELECT collected_ts FROM market_positioning "
            "WHERE source=? AND timeframe='1H' "
            "ORDER BY datetime(collected_ts) DESC LIMIT 1",
            (POSITIONING_SOURCE,),
        ).fetchone()
        if not latest:
            print("  暂无数据")
            return
        rows = mkt.execute(
            "SELECT symbol,ts,long_ratio,short_ratio,long_short_ratio "
            "FROM market_positioning WHERE collected_ts=? AND source=? "
            "AND timeframe='1H' "
            "ORDER BY CASE symbol WHEN 'BTC-USDT-SWAP' THEN 0 "
            "WHEN 'ETH-USDT-SWAP' THEN 1 WHEN 'SOL-USDT-SWAP' THEN 2 ELSE 3 END, "
            "symbol",
            (latest["collected_ts"], POSITIONING_SOURCE),
        ).fetchall()
        tick_ts = mkt.execute(
            "SELECT MAX(ts) AS m FROM tick_snapshots"
        ).fetchone()["m"]
        expected = {
            str(row["symbol"])
            for row in mkt.execute(
                "SELECT DISTINCT symbol FROM tick_snapshots "
                "WHERE ts=? AND symbol LIKE '%-USDT-SWAP'",
                (tick_ts,),
            ).fetchall()
        } if tick_ts else set()
        quality = positioning_evidence_quality(rows, expected)
        if quality["status"] != "PASSED":
            age = quality["maximum_source_age_minutes"]
            age_text = f"{age:.1f}m" if age is not None else "N/A"
            print(
                "  本轮不可用：真实源时效或全宇宙完整性未过闸 "
                f"({quality['valid_symbols']}/{quality['expected_symbols']}, "
                f"最老源年龄={age_text})；不作为方向证据"
            )
            return
        for r in rows[:8]:
            print(
                f"  {r['symbol'].split('-')[0]} long={r['long_ratio']:.0%} "
                f"short={r['short_ratio']:.0%} L/S={r['long_short_ratio']:.2f} "
                f"源时刻={r['ts']}"
            )
        print(
            f"  @ {fmt_age(latest['collected_ts'])} 前；账户数量比≠仓位金额，"
            "仅用于识别拥挤，不自动产生方向"
        )
    safe(s_positioning)

    # ── 4. 币种情绪（新闻+X 提及） ────────────────────
    section("情绪 Top（coin_sentiment）")

    def s_senti():
        news = connect(root, "news.db")
        ts = news.execute("SELECT MAX(ts) AS m FROM coin_sentiment").fetchone()["m"]
        rows = news.execute(
            "SELECT symbol,label,bullish_ratio,bearish_ratio,mention_cnt FROM coin_sentiment "
            "WHERE ts=? AND mention_cnt>=3 ORDER BY mention_cnt DESC LIMIT 12",
            (ts,),
        ).fetchall()
        bull = sorted(rows, key=lambda r: r["bullish_ratio"] or 0, reverse=True)[:3]
        bear = sorted(rows, key=lambda r: r["bearish_ratio"] or 0, reverse=True)[:3]
        print("  偏多: " + " ".join(f"{r['symbol']}({r['bullish_ratio']:.0%}/{r['mention_cnt']}提及)" for r in bull))
        print("  偏空: " + " ".join(f"{r['symbol']}({r['bearish_ratio']:.0%}/{r['mention_cnt']}提及)" for r in bear))
        # ts 混 UTC-Z 与 CST-space（2026-07-02 修）：裸比 datetime('now')(naive-UTC) 会让 CST
        # 行整日字典序入选 → 虚高。归一到 naive-UTC 再比。
        _tsn = "CASE WHEN ts LIKE '%Z' THEN datetime(ts) ELSE datetime(ts,'-8 hours') END"
        n2h = news.execute(f"SELECT COUNT(*) AS c FROM news_items WHERE {_tsn} >= datetime('now','-2 hours')").fetchone()["c"]
        print(f"  新闻流量 2h: {n2h} 条 @ 情绪快照 {fmt_age(ts)} 前")
        news.close()
    safe(s_senti)

    # ── 4b. 关键新闻：统一从简报读取，禁止每轮临场写 _critnews/_precheck 查询脚本 ──
    section("关键新闻（critical/high 6h）")

    def s_critnews():
        news = connect(root, "news.db")
        # ts 混 UTC-Z/CST（同 s_senti 口径）：归一 naive-UTC 再比；年龄直接 SQL 算，
        # 不走 fmt_age（其解析不认 Z 格式）。
        _tsn = "CASE WHEN ts LIKE '%Z' THEN datetime(ts) ELSE datetime(ts,'-8 hours') END"
        news_cols = {str(r[1]) for r in news.execute(
            "PRAGMA table_info(news_items)").fetchall()}
        has_layers = "first_seen_at" in news_cols
        if has_layers:
            # first_seen_at 只表示观察首见（重复采集不刷新）；催化新鲜度只由
            # event_occurred_at 决定，禁止把“刚观察到旧闻”写成“刚发生”。
            _fsn = ("CASE WHEN first_seen_at LIKE '%Z' THEN datetime(first_seen_at) "
                    "ELSE datetime(COALESCE(first_seen_at, ts),'-8 hours') END")
            rows = news.execute(
                f"SELECT id, severity, symbol, title, event_occurred_at, "
                f"event_time_confidence, source_grade, primary_source_url, "
                f"CAST((julianday('now') - julianday({_fsn})) * 1440 AS INTEGER) AS first_seen_age_m "
                f"FROM news_items WHERE severity IN ('critical','high') "
                f"AND {_tsn} >= datetime('now','-6 hours') "
                f"ORDER BY (severity='critical') DESC, id DESC LIMIT 8").fetchall()
        else:
            rows = news.execute(
                f"SELECT id, severity, symbol, title, event_time, "
                f"CAST((julianday('now') - julianday({_tsn})) * 1440 AS INTEGER) AS age_m "
                f"FROM news_items WHERE severity IN ('critical','high') "
                f"AND {_tsn} >= datetime('now','-6 hours') "
                f"ORDER BY (severity='critical') DESC, id DESC LIMIT 8").fetchall()
        if not rows:
            print("  （近 6h 无 critical/high 新闻——本节为空即代表已查过，无需再查 news.db）")
            news.close()
            return
        if has_layers:
            print("  （观察首见=系统首次看到该事件，绝不等于事件新鲜度；"
                  "催化时效只看事件日；事件日未知不得写成 fresh；"
                  "grade≠primary 表示未经一级源核实）")
        for r in rows:
            syms = [x[0] for x in news.execute(
                "SELECT DISTINCT symbol FROM news_events_index WHERE news_id=? LIMIT 6",
                (r["id"],))]
            sym_s = ("[" + ",".join(syms) + "] ") if syms else \
                (f"[{r['symbol']}] " if r["symbol"] else "")
            if has_layers:
                occurred = r["event_occurred_at"]
                conf = r["event_time_confidence"] or "unknown"
                evt = (f" 事件日:{occurred}" if occurred
                       else f" 事件日:未知({conf})")
                freshness, age_text = catalyst_freshness(occurred)
                grade = r["source_grade"] or "secondary"
                grade_s = f" 源级:{grade}"
                if r["primary_source_url"]:
                    grade_s += "(已附一级源)"
                elif grade != "primary":
                    grade_s += "(未经一级源核实)"
                print(f"  [news_id:{r['id']} {r['severity']}] "
                      f"{sym_s}{str(r['title'])[:70]}{evt} "
                      f"催化:{freshness}({age_text}){grade_s} "
                      f"观察首见:{r['first_seen_age_m']}m前")
            else:
                evt = f" 事件时刻:{r['event_time']}" if r["event_time"] else ""
                print(f"  [{r['severity']}] {sym_s}{str(r['title'])[:70]}{evt} @ {r['age_m']}m前")
        news.close()
    safe(s_critnews)

    # ── 4b-2. 新鲜一级源催化：按「源级+事件日」筛，刻意不套 severity ──
    # 2026-08-20：上节 4b 硬过滤 severity IN ('critical','high') LIMIT 8，而实测
    # 近 3 天唯一同时满足「source_grade=primary + 有 event_occurred_at + 事件日在
    # 48h 内 + 标的级」的 20 条里有 17 条是 severity='low'（ETF 单日净流、协议手续
    # 费销毁、鲸鱼地址等）。角色契约要求「负 EV 候选只有经一级源核实的新鲜催化才
    # 可 ev_override」，于是唯一能解开该条款的证据被 severity 过滤结构性挡在视野
    # 外——Agent 每轮如实写「无一级源核实的新鲜催化」，而它确实看不到。
    # 本节只按催化契约本身的三个条件筛，不改任何风控，也不产生授权。
    section("新鲜一级源催化（primary + 事件日 ≤48h，不套 severity）")

    def s_primary_catalysts():
        news = connect(root, "news.db")
        news_cols = {str(r[1]) for r in news.execute(
            "PRAGMA table_info(news_items)").fetchall()}
        need = {"event_occurred_at", "source_grade", "primary_source_url"}
        if not need.issubset(news_cols):
            print("  N/A（news_items 缺 event_occurred_at/source_grade/"
                  "primary_source_url，本节口径不可判定）")
            news.close()
            return
        _tsn = ("CASE WHEN ts LIKE '%Z' THEN datetime(ts) "
                "ELSE datetime(ts,'-8 hours') END")
        # 标的级是本节的用途所在（催化要能绑到某个候选），故只取带 symbol 或已
        # 进 news_events_index 的条目；无标的的宏观 primary 只报计数，不占版面，
        # 也不被静默丢弃。
        _base = (
            f"FROM news_items n "
            f"WHERE (n.source_grade='primary' "
            f"       OR (n.primary_source_url IS NOT NULL "
            f"           AND n.primary_source_url<>'')) "
            f"AND n.event_occurred_at IS NOT NULL AND n.event_occurred_at<>'' "
            f"AND date(substr(n.event_occurred_at,1,10)) "
            f"    BETWEEN date('now','+8 hours','-2 day') "
            f"        AND date('now','+8 hours') "
            f"AND (CASE WHEN n.ts LIKE '%Z' THEN datetime(n.ts) "
            f"     ELSE datetime(n.ts,'-8 hours') END) "
            f"    >= datetime('now','-48 hours') "
        )
        _tagged = (
            "AND ((n.symbol IS NOT NULL AND n.symbol<>'') "
            "     OR EXISTS (SELECT 1 FROM news_events_index x "
            "                WHERE x.news_id=n.id)) "
        )
        rows = news.execute(
            "SELECT n.id, n.severity, n.symbol, n.title, n.event_occurred_at, "
            "n.event_time_confidence, n.source_grade, n.primary_source_url "
            + _base + _tagged
            + "ORDER BY date(substr(n.event_occurred_at,1,10)) DESC, n.id DESC "
              "LIMIT 12").fetchall()
        untagged = news.execute(
            "SELECT COUNT(*) AS c " + _base
            + "AND NOT ((n.symbol IS NOT NULL AND n.symbol<>'') "
              "         OR EXISTS (SELECT 1 FROM news_events_index x "
              "                    WHERE x.news_id=n.id))").fetchone()["c"]
        if not rows:
            print(f"  （近 48h 无「标的级 + primary + 事件日 ≤2 天」条目；"
                  f"另有 {untagged} 条同口径但无标的的宏观 primary。"
                  f"本节为空即代表已按契约口径查过，无需再查 news.db）")
            news.close()
            return
        print("  （本节条目已满足『一级源核实 + 事件日已知且 ≤2 天 + 标的级』；"
              "是否构成本标的方向催化仍由 Agent 自行判断，本节不推断方向、"
              f"不产生授权。另有 {untagged} 条同口径无标的宏观 primary 未列）")
        for r in rows:
            syms = [x[0] for x in news.execute(
                "SELECT DISTINCT symbol FROM news_events_index "
                "WHERE news_id=? LIMIT 6", (r["id"],))]
            sym_s = ("[" + ",".join(syms) + "] ") if syms else \
                (f"[{r['symbol']}] " if r["symbol"] else "[无标的] ")
            freshness, age_text = catalyst_freshness(r["event_occurred_at"])
            grade = r["source_grade"] or "secondary"
            grade_s = f" 源级:{grade}"
            if r["primary_source_url"]:
                grade_s += "(已附一级源)"
            print(f"  [news_id:{r['id']} sev:{r['severity'] or '?'}] "
                  f"{sym_s}{str(r['title'])[:70]} "
                  f"事件日:{r['event_occurred_at']} "
                  f"催化:{freshness}({age_text}){grade_s}")
        news.close()
    safe(s_primary_catalysts)

    # ── 4c. OKX 无专用接口的数据：x_search 权威证据层 ──
    section("权威补充数据（x_search 证据层）")

    def s_authoritative_data():
        news = connect(root, "news.db")
        _tsn = (
            "CASE WHEN COALESCE(ingested_at,ts) LIKE '%Z' "
            "THEN datetime(COALESCE(ingested_at,ts)) "
            "ELSE datetime(COALESCE(ingested_at,ts),'-8 hours') END"
        )
        rows = news.execute(
            f"SELECT title,event_time,url,raw FROM news_items "
            f"WHERE source='x_search' "
            f"AND (tags LIKE '%authoritative_data%' OR tags LIKE '%\"fear_greed\"%') "
            f"AND {_tsn} >= datetime('now','-72 hours') "
            f"ORDER BY id DESC LIMIT 20"
        ).fetchall()
        shown: set[str] = set()
        for r in rows:
            try:
                raw = json.loads(r["raw"] or "{}")
            except (TypeError, json.JSONDecodeError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            metric = str(raw.get("metric") or "unknown")
            if metric in shown:
                continue
            shown.add(metric)
            status = str(raw.get("verification_status") or "unknown")
            as_of = raw.get("as_of") or r["event_time"] or "?"
            source_name = raw.get("source_name") or "未标来源"
            value = raw.get("value")
            unit = str(raw.get("unit") or "")
            if isinstance(value, (int, float)) and unit.upper() == "USD":
                value_s = f"${value / 1_000_000:+.1f}M"
            elif value is not None:
                value_s = f"{value} {unit}".rstrip()
            else:
                value_s = "数值待复核"
            print(
                f"  [{status}] {metric}={value_s} as_of={as_of} "
                f"src={source_name}｜{str(r['title'])[:60]}"
            )
            if len(shown) >= 5:
                break
        if not shown:
            print("  近72h暂无合格权威补充证据")
        print(
            "  ETF单源证据仅进入macro_observations provisional；只有同日"
            "Farside+SoSoValue一致才进入硬字段。其他pending/unknown不得当确认值"
        )
        news.close()
    safe(s_authoritative_data)

    # ── 5. 持仓与账户 ────────────────────────────────
    section("持仓 / 账户")

    def s_pos():
        def _portfolio_line(tag, rows, eq, basis="净值"):
            notionals = []
            margins = []
            sides = {"long": 0.0, "short": 0.0}
            for r in rows:
                try:
                    iv = mkt.execute(
                        "SELECT ctVal FROM instruments_cache WHERE instId=?",
                        (r["symbol"],),
                    ).fetchone()
                    notional = abs(r["sz"] * iv["ctVal"] * r["avgPx"])
                    notionals.append(notional)
                    side = str(r["side"] or "").lower()
                    if side in sides:
                        sides[side] += notional
                    if r["lev"]:
                        margins.append(notional / r["lev"])
                except Exception:
                    continue
            gross = sum(notionals)
            if not rows:
                print(f"    {tag} 组合观察: 0 仓 | 总敞口(gross)=0 | 保证金≈0")
                return
            gross_x = f"{gross / eq:.2f}x{basis}" if eq else f"{basis}N/A"
            margin = sum(margins)
            margin_pct = f"{margin / eq:.1%}{basis}" if eq else f"{basis}N/A"
            net = sides["long"] - sides["short"]
            net_x = f"{net / eq:+.2f}x{basis}" if eq else f"${net:+.2f}"
            same = max(sides.values()) / gross if gross else 0.0
            largest = max(notionals, default=0.0) / gross if gross else 0.0
            warns = []
            if eq and gross / eq >= 3.0:
                warns.append("总敞口≥3x")
            if len(rows) >= 2 and same >= 0.80:
                warns.append("同向≥80%")
            if len(rows) >= 2 and largest >= 0.60:
                warns.append("单仓≥60%总敞口")
            warn_s = f" | ⚠️ {','.join(warns)}" if warns else ""
            print(
                f"    {tag} 组合观察: {len(rows)}仓 | 总敞口(gross)=${gross:.2f}/{gross_x} | "
                f"逐仓保证金求和≈${margin:.2f}/{margin_pct} | 净敞口(net)={net_x} | "
                f"同向={same:.0%} | 最大仓={largest:.0%}·占总敞口{warn_s}"
            )
            if tag == "live":
                print(
                    "      Live OPEN/ADD硬闸以执行时同次OKX "
                    "account.balance.imr/totalEq加本单增量计算预计值，"
                    f"须≤{MAX_PORTFOLIO_IMR_RATIO:.1%}；"
                    "本段逐仓估算、mgnRatio、gross、net均不得替代。"
                )

        def _fmt_pos(tag, r, eq, basis="净值"):
            # 2026-07-15 主人要求：持仓行补「多/空 + 保证金 USD + 占净值%」（原只有张数难判风险）。
            # 保证金≈sz×ctVal×avgPx÷lev（与 risk_validator 同口径，ctVal 取 market.db.instruments_cache）。
            side_cn = {"long": "多", "short": "空"}.get(str(r["side"] or "").lower(), r["side"] or "?")
            m = ""
            try:
                iv = mkt.execute("SELECT ctVal FROM instruments_cache WHERE instId=?",
                                 (r["symbol"],)).fetchone()
                if iv and iv["ctVal"] and r["sz"] and r["avgPx"] and r["lev"]:
                    margin = r["sz"] * iv["ctVal"] * r["avgPx"] / r["lev"]
                    pct = f"/{margin / eq * 100:.1f}%{basis}" if eq else ""
                    m = f" 保证金≈${margin:.2f}{pct}"
            except Exception:
                pass
            print(f"    {tag} {r['symbol']} {side_cn} {r['sz']}张@{r['avgPx']} "
                  f"{r['lev']:g}x{m} upl={(r['upl'] or 0):+.2f}")

        a = acc.execute(
            "SELECT totalEq,availBal,upl,daily_pnl,ts FROM account_snapshots "
            "WHERE profile='live' ORDER BY ts DESC,rowid DESC LIMIT 1"
        ).fetchone()
        if a:
            avail = f"${a['availBal']:.2f}" if a["availBal"] is not None else "N/A"
            print(f"  🟢 live 资金 ${a['totalEq']:.2f} | 可用USDT {avail} | "
                  f"upl {a['upl'] or 0:+.2f} | 日内 {a['daily_pnl'] or 0:+.2f} "
                  f"@ {fmt_age(a['ts'])} 前")
        pts_row = acc.execute(
            "SELECT ts FROM position_snapshots WHERE profile='live' "
            "ORDER BY ts DESC,rowid DESC LIMIT 1"
        ).fetchone()
        pts = pts_row["ts"] if pts_row else None
        # F7（2026-07-06）：symbol='__FLAT__' 是空仓哨兵行（jobb 写方标记"该 ts 确认空仓"，
        # 非缺数据）——展示时过滤；哨兵批次 prs 为空 → 走下方 "live 0 仓" 分支。
        prs = acc.execute(
            "SELECT symbol,side,sz,avgPx,lev,upl FROM position_snapshots "
            "WHERE ts=? AND profile='live' AND symbol != '__FLAT__'", (pts,)
        ).fetchall() if pts else []
        live_fresh = [r for r in prs if True]
        if live_fresh and (age_min(pts) or 999) < 45:
            for r in live_fresh:
                _fmt_pos("live", r, a["totalEq"] if a else None)
        else:
            print("    live 0 仓")
        _portfolio_line(
            "live",
            live_fresh if (age_min(pts) or 999) < 45 else [],
            a["totalEq"] if a else None,
        )
        # demo 账户/持仓/同向集中度/待回填 pnl 四段随 2026-08-06 demo 全量下线移除。
        try:
            costs = acc.execute(
                "SELECT profile,COUNT(*) n,COALESCE(SUM(fee),0) fee,"
                "COALESCE(SUM(CASE WHEN type='8' THEN pnl ELSE 0 END),0) funding_cashflow,"
                "COALESCE(SUM(bal_change),0) net_change "
                "FROM account_bills WHERE datetime(ts)>=datetime('now','+8 hours','-1 day') "
                "AND profile='live' GROUP BY profile ORDER BY profile"
            ).fetchall()
            for c in costs:
                print(f"  {c['profile']} 交易所账单24h: 笔数={c['n']} 手续费={c['fee']:+.4f} "
                      f"资金费={c['funding_cashflow']:+.4f} 净变动={c['net_change']:+.4f} USDT")
        except Exception:
            pass
    safe(s_pos)

    # ── 6. playbook 候选（真实战绩警示） ──────────────
    section("playbook（评估再用，战绩为准）")

    def s_play():
        # 2026-08-19 F5③：playbook 陈旧即整段收起。数据停在 2026-06-10、
        # playbook_ref 覆盖 1/189 → 每轮约 10 行零信息，且给 LLM「有剧本可用」
        # 的错觉。只收起展示，select_playbook_matches/_playbook_context 与
        # update_playbook_stats.py 原样保留，回填 playbook_ref 后自然复活。
        pb_age = acc.execute(
            "SELECT MAX(updated_utc), "
            "CAST(julianday('now') - julianday(MAX(updated_utc)) AS INT) "
            "FROM playbook").fetchone()
        pb_last, pb_days = (pb_age[0], pb_age[1]) if pb_age else (None, None)
        if pb_days is not None and pb_days > PLAYBOOK_MAX_AGE_DAYS:
            print(f"  ⚠️ playbook 数据停滞于 {pb_last}（{pb_days} 天前 > "
                  f"{PLAYBOOK_MAX_AGE_DAYS} 天阈值），本轮不展示条目；"
                  "统计未刷新前它既不是证据也不是禁令")
            # 段收起了，末尾的自足断言就不能再自称「已含 playbook」——
            # 「声称含某段 + 该段说自己不可用」正是 F5 要消灭的那种自相矛盾。
            _SECTION_STATE["playbook_shown"] = False
            return
        rg = reg.execute(
            "SELECT regime FROM cross_market WHERE regime IS NOT NULL ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        current_regime = rg["regime"] if rg else None
        context, known = _playbook_context(mkt, acc, Path(root).parent / "focus.md", top)
        all_rows = acc.execute(
            "SELECT id,ts,category,summary,evidence,updated_utc,"
            "win_count,loss_count,win_rate,avg_pnl_pct FROM playbook"
        ).fetchall()
        matched, stats = select_playbook_matches(
            all_rows, current_regime, context, known, limit=9
        )
        source_marker = (
            Path(root).parent
            / "reports"
            / "quality"
            / "playbook_current_source_v1.json"
        )
        stats_ready = False
        if source_marker.exists():
            try:
                marker = json.loads(source_marker.read_text(encoding="utf-8"))
                stats_ready = (
                    marker.get("source")
                    == "account.db.trade_experiences.closed.playbook_ref"
                    and int(marker.get("attributed_experiences") or 0) > 0
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                stats_ready = False
        if stats_ready:
            proven = [
                r for r in matched
                if int(r["win_count"] or 0) + int(r["loss_count"] or 0) >= 5
            ][:6]
            fresh = [
                r for r in matched
                if int(r["win_count"] or 0) + int(r["loss_count"] or 0) < 5
            ][:3]
        else:
            # Numeric fields pre-date the current trade_experiences source cutover
            # and may include retired drill/trade_events facts.  Keep summaries as
            # unverified context but never present those numbers as current proof.
            proven = []
            fresh = matched[:9]
        context_s = ",".join(sorted(context)[:18])
        if len(context) > 18:
            context_s += f",…(+{len(context)-18})"
        print(f"  匹配上下文: regime={current_regime or 'N/A'} | symbols={context_s or 'N/A'}")
        if not stats_ready:
            print(
                "  ⚠️ 现役 playbook 统计尚未完成 current-source 初始化；"
                "旧 drill/trade_events 数值已禁用，仅展示未验证条目。"
            )
        for r in proven:
            n = int(r["win_count"] or 0) + int(r["loss_count"] or 0)
            wr = r["win_rate"] or 0
            if n >= 10 and wr < 0.30:
                print(f"  ⚠️ #{r['id']} n={n} wr={wr:.0%}·历史表现弱，供反向/修订参考 | {r['summary'][:40]}")
            elif wr < 0.35:
                print(f"  ⚠️ #{r['id']} n={n} wr={wr:.0%} avg={r['avg_pnl_pct']:+.1f}%·低胜率仅反向/时机参考 | {r['summary'][:40]}")
            else:
                print(f"  ✓ #{r['id']} n={n} wr={wr:.0%} avg={r['avg_pnl_pct']:+.1f}%·可用 | {r['summary'][:40]}")
        if fresh:
            print("  未验证匹配条目: "
                  + " | ".join(f"#{r['id']} {r['summary'][:36]}" for r in fresh))
        else:
            print("  未验证匹配条目: 无（过期或与当前regime/品种不匹配）")
        print(f"  筛选统计: total={len(all_rows)} eligible={stats['eligible']} "
              f"expired={stats['expired']} regime_mismatch={stats['regime_mismatch']} "
              f"symbol_mismatch={stats['symbol_mismatch']} deprecated={stats['deprecated']}；"
              f"未验证TTL hypothesis={PLAYBOOK_HYPOTHESIS_TTL_DAYS}d/other={PLAYBOOK_OTHER_TTL_DAYS}d")
    safe(s_play)

    # ── 6.5 历史经验（正反样本与错失机会；参考输入，不锁决策） ──
    if not closure_cycle:
        section("历史交易经验（正反样本+错失机会；仅参考，不设自动闸）")

    def s_experience():
        exp_cols = {str(r[1]) for r in acc.execute(
            "PRAGMA table_info(trade_experiences)").fetchall()}
        version_sql = (
            "experience_summary_version"
            if "experience_summary_version" in exp_cols
            else "NULL AS experience_summary_version"
        )
        rows = acc.execute(
            "SELECT cycle_id,ts,profile,symbol,side,regime,pnl_pct,hold_hours,"
            f"experience_summary,{version_sql} FROM trade_experiences "
            "WHERE status='closed' AND pnl_pct IS NOT NULL "
            "ORDER BY ts DESC,id DESC LIMIT 80"
        ).fetchall()

        def safe_lesson(row):
            summary = str(row["experience_summary"] or "").strip()
            version = row["experience_summary_version"]
            if (version == 2 and summary
                    and not re.search(r"(?i)\bhit[_ ]?1r\b", summary)):
                return summary[:70]
            regime = row["regime"] or "?"
            side = row["side"] or "?"
            hold = row["hold_hours"]
            hold_s = f" hold{float(hold):.1f}h" if hold is not None else ""
            return f"{regime}/{side} pnl{float(row['pnl_pct']):+.2f}%{hold_s}"
        wins = sorted(
            (r for r in rows if r["pnl_pct"] > 0),
            key=lambda r: r["pnl_pct"],
            reverse=True,
        )[:3]
        losses = sorted(
            (r for r in rows if r["pnl_pct"] <= 0),
            key=lambda r: r["pnl_pct"],
        )[:3]
        print(f"  已平仓参考池: n={len(rows)}；盈利样本="
              f"{sum(r['pnl_pct'] > 0 for r in rows)}；亏损样本="
              f"{sum(r['pnl_pct'] <= 0 for r in rows)}")
        print("  盈利样本预览（拟交易标的仍须按 symbol/side/regime 匹配）:")
        for r in wins:
            lesson = safe_lesson(r)
            print(f"    + {r['symbol']} {r['side']} {r['regime'] or '-'} "
                  f"{r['pnl_pct']:+.2f}% 持{r['hold_hours'] or '-'}h | {lesson}")
        if not wins:
            print("    无")
        print("  亏损样本预览（必须与盈利样本同等查看）:")
        for r in losses:
            lesson = safe_lesson(r)
            print(f"    - {r['symbol']} {r['side']} {r['regime'] or '-'} "
                  f"{r['pnl_pct']:+.2f}% 持{r['hold_hours'] or '-'}h | {lesson}")
        if not losses:
            print("    无")

        les = connect(root, "lessons.db")
        missed = les.execute(
            "SELECT ts,symbol,regime,direction_hint,actual_4h_pct,"
            "would_hit_1r_fixed2pct,notes "
            "FROM missed_opportunities WHERE ts LIKE '202%' "
            "ORDER BY ts DESC,id DESC LIMIT 5"
        ).fetchall()
        les.close()
        print("  错失机会样本（fixed2pct 为固定±2%代理口径，非真实计划止损）:")
        for r in missed:
            print(f"    · {r['symbol']} {r['direction_hint'] or '-'} "
                  f"4h={r['actual_4h_pct'] if r['actual_4h_pct'] is not None else '-'}% "
                  f"would_hit_fixed2pct={r['would_hit_1r_fixed2pct']} "
                  f"| {str(r['notes'] or '')[:60]}")
        if not missed:
            print("    无")
        try:
            eq = _summarize_frozen_exit_quality(
                _frozen_exit_quality_history(root, days=7))
            if eq is None:
                print("  退出质量: N/A（无 ready+SHA-256 绑定的前向冻结工件）")
            else:
                rate = eq["review_rate"]
                coverage = eq["margin_fact_coverage"]
                print(
                    f"  退出质量{eq['days']}d冻结工件"
                    f"（{eq['first_business_date']}..{eq['last_business_date']}；"
                    "仅参考，不设自动闸）: "
                    f"峰值回吐={eq['peak_status']}；"
                    f"曾达1R {eq['reached_1r']} 笔 / 平仓仍≥1R "
                    f"{eq['closed_at_or_above_1r']} 笔，持仓期利润回吐案例 "
                    f"{eq['profit_giveback_cases']} 笔；最新峰值中位回吐 "
                    f"{eq['latest_peak_giveback_median_r']}R；回吐源行 "
                    f"{eq['peak_source_closed_rows']}（排除非live "
                    f"{eq['peak_excluded_non_live_rows']}、非open "
                    f"{eq['peak_excluded_non_open_rows']}）；"
                    f"保证金收益率≥50% 被标记 {eq['flagged']} 次、显式复核率 "
                    + ("无样本" if rate is None else f"{rate:.0%}")
                    + "，事实字段覆盖 "
                    + ("无样本" if coverage is None else f"{coverage:.0%}")
                    + f"（未知 {eq['unknown_margin_facts']}/"
                    f"{eq['total_margin_facts']}），处置 {eq['dispositions']}；"
                    f"动作分层 {eq['action_layers']}；"
                    f"保证金复核源cycle {eq['margin_source_candidate_cycles']}"
                    f"（排除非live {eq['margin_excluded_non_live_cycles']}、"
                    f"非open仓位 {eq['margin_excluded_non_open_positions']}）；"
                    f"错失止盈反事实={eq['missed_take_profit_status']}，"
                    f"池规模={eq['missed_take_profit_pool_size']}、"
                    f"分类计数={eq['missed_take_profit_classified_count']}、"
                    f"池未知日={eq['missed_take_profit_unknown_pool_days']}，"
                    f"源关闭记录={eq['missed_source_closed_rows']}"
                    f"（排除非live {eq['missed_excluded_profile_count']}、"
                    f"fallback兜底 {eq['missed_excluded_fallback_count']}）"
                )
                print(
                    "    ➤ 错失止盈池由原 fixed_tp、权威最终成交及平仓后"
                    "16根15m路径构成；缺关键证据会阻断 ready，不以 UNKNOWN"
                    "或0兜底，也绝不拿持仓期MFE代替。"
                )
            print(
                "    ➤ 这是既往退出的后验统计，不是止盈指令：是否止盈、"
                "减仓还是继续持有仍由当前证据逐仓自主裁决。"
            )
        except Exception:
            pass

        print(
            "  ➤ 本段只是全局预览，严禁据此声称某标的直接 N胜/N负。"
            "每个拟执行标的必须以完整 instId + side + regime + action=open + "
            "profile=live + 固定 cycle --as-of 调 find_similar_experience.py；"
            "直接传本卡 --entry/--stop/--target，禁止自行换算百分比或 RR；"
            "把 evidence_contract 原样写入决策卡。数字只认 exact_setup/"
            "same_symbol_similar/cross_symbol_similar 具名 summary；matched_* 与 "
            "cross_symbol_* 是截断样例，禁止数数组或混栏。另自主注明 "
            "usage=adopt|partial|ignore|none 与理由。历史结果永不自动批准或否决。"
        )
    if not closure_cycle:
        safe(s_experience)

    # ── 7. 历史表现基线（不映射评分/置信度档位） ───────
    if not closure_cycle:
        section("历史表现基线（30 天真实成交；仅参考）")

    def s_calib():
        exp_cols = {str(r[1]) for r in acc.execute(
            "PRAGMA table_info(trade_experiences)").fetchall()}
        close_clock = "COALESCE(closed_at,ts)" if "closed_at" in exp_cols else "ts"
        vector_sql = (
            "experience_vector" if "experience_vector" in exp_cols
            else "NULL AS experience_vector"
        )
        # F4：路径列按存在性拼接，旧库缺列时降级为 NULL（分档落 'unknown'）。
        path_sql = "".join(
            (f"{col}," if col in exp_cols else f"NULL AS {col},")
            for col in ("mae_r", "mfe_r", "exit_category")
        )
        cutoff = (datetime.now(CST) - timedelta(days=30)).strftime(
            "%Y-%m-%d %H:%M:%S")
        rows = acc.execute(
            "SELECT cycle_id,ts,symbol,side,regime,pnl_pct,realized_pnl,"
            f"hold_hours,raw,{path_sql}"
            f"{vector_sql} FROM trade_experiences "
            "WHERE status='closed' AND pnl_pct IS NOT NULL "
            f"AND datetime({close_clock})>=datetime(?) "
            f"ORDER BY datetime({close_clock}),id",
            (cutoff,),
        ).fetchall()
        try:
            asset_classes = {
                str(r["symbol"]): str(r["asset_class"])
                for r in mkt.execute(
                    "SELECT symbol,asset_class FROM instrument_class"
                ).fetchall()
            }
        except sqlite3.Error:
            asset_classes = {}
        analysis_usage = {}
        ana = None
        try:
            ana = connect(root, "analysis.db")
            for signal in ana.execute(
                "SELECT cycle_id,symbol,decision_card FROM analysis_signals "
                "WHERE cycle_id>=? AND lower(action) IN "
                "('open','open_long','open_short','long','short','sell')",
                (cutoff.replace(" ", "T"),),
            ).fetchall():
                card = _json_object(signal["decision_card"])
                history = card.get("historical_experience")
                history = history if isinstance(history, dict) else {}
                usage = str(history.get("usage") or "").strip().lower()
                if usage in _CALIBRATION_USAGE_ORDER[:-1]:
                    analysis_usage[(
                        str(signal["cycle_id"]),
                        str(signal["symbol"]).upper(),
                    )] = usage
        except sqlite3.Error:
            analysis_usage = {}
        finally:
            if ana is not None:
                ana.close()
        calibration = experience_calibration(
            rows, asset_classes, analysis_usage)
        if not calibration["sample_n"]:
            print("  近 30 天无已平仓真实成交样本")

        def show(title, groups):
            print(f"  {title}:")
            if not groups:
                print("    无可用样本")
                return
            for label, item in groups.items():
                small = "（样本小仅参考）" if item["n"] < 10 else ""
                print(
                    f"    {label}: n={item['n']} "
                    f"{item['wins']}胜/{item['losses']}负 "
                    f"胜率{item['win_rate_pct']:.1f}% "
                    f"均收益{item['avg_pnl_pct']:+.2f}% "
                    + (
                        f"实盈亏Σ{item['realized_pnl_sum_usdt']:+.2f} USDT"
                        if item["realized_pnl_n"] == item["n"]
                        else "实盈亏Σ N/A（旧样本缺字段）"
                    )
                    + small
                )

        print(f"  确定性样本窗: 最近30天按 closed_at；n={calibration['sample_n']}")
        show("历史经验采纳方式", calibration["history_usage"])
        usage_sources = calibration["history_usage_source_counts"]
        print(
            "  采纳口径来源: "
            f"开仓决策卡 {usage_sources['analysis_signal']} / "
            f"成交回执 {usage_sources['trade_receipt']} / "
            f"旧版不可判定 {usage_sources['unknown']}；"
            "不可判定样本不冒充 none。"
        )
        show("regime×方向", calibration["regime_side"])
        show("regime×顺逆势（side 与交易时冻结的 1H/4H 趋势：同向=顺势/反向=逆势/其余=混合）",
             calibration["regime_alignment"])
        # F4：路径三组排在时段/持有时长之前 —— 它们的判别力最高。
        show("入场质量 MAE 分档（进场后最大逆行，1R=计划止损距）",
             calibration["entry_quality"])
        show("路径质量 MFE 分档（进场后最大顺行）", calibration["path_quality"])
        show("出场通道（exit_category；reconcile_backfill=非 Agent 主动平仓）",
             calibration["exit_channel"])
        show("开仓时段（UTC+8 四小时桶，按开仓 ts）", calibration["open_hour_bucket"])
        show("已平仓持有时长", calibration["hold_bucket"])
        show("资产类别", calibration["asset_class"])
        if calibration["asset_class_current_map_fallback_n"]:
            print(
                "  注：资产类别优先使用交易时冻结的 experience_vector；"
                f"{calibration['asset_class_current_map_fallback_n']} 条旧样本回退当前"
                " instrument_class 映射。"
            )
        print("  决策主因分层: N/A（历史卡未冻结 event/technical 结构字段，禁从自然语言倒推）")
        print("  actor cohort 分层: N/A（历史闭仓回执无可比较的不透明 cohort，禁猜测身份）")
        print("  ➤ 统计只描述过去，不能形成开仓门槛、仓位档位或否决规则。"
              "仓位由 Agent 结合六项卡自主决定，再由确定性安全闸校验。")
    if not closure_cycle:
        safe(s_calib)

    # ── 8. lessons 回灌（T6） ────────────────────────
    if not closure_cycle:
        section("教训回灌（lessons.db）")

    def s_lessons():
        les = connect(root, "lessons.db")
        # 2026-07-12（主人拍板）：加 retired=0 + last_seen 30 天过滤——旧按 hit_count 全量降序时，
        # 两条 2026-05-22/23 自证计数模式（82/80 次）永久霸榜、每轮灌压制话术（评分压低自证回路根因）。
        eps = les.execute(
            "SELECT pattern_name,trigger_condition,hit_count FROM error_patterns "
            "WHERE COALESCE(retired,0)=0 AND last_seen_utc>=datetime('now','-30 days') "
            "ORDER BY hit_count DESC LIMIT 3"
        ).fetchall()
        if not eps:
            # 2026-08-19 F5②：唯一写方 self_review.py 不在 daily_maintenance
            # 的 STEPS 里，error_patterns 停在 2026-06-08 —— 「近 30 天无有效
            # 错误模式」是伪信息（真相是写方停摆）。改为自证写方状态。
            last_seen = les.execute(
                "SELECT MAX(last_seen_utc) FROM error_patterns").fetchone()
            last_seen = str((last_seen or [None])[0] or "")
            if last_seen:
                print(f"  ⚠️ 错误模式段口径失效：写方最后写入 {last_seen}，"
                      "近 30 天窗内 0 条不代表「无错误模式」")
            else:
                print("  ⚠️ 错误模式段口径失效：error_patterns 表从无记录")
        for r in eps:
            print(f"  错误模式[{r['hit_count']}次] {r['pattern_name']}: {str(r['trigger_condition'])[:48]}")
        # ts LIKE '202%' 只读取 ISO 日期行，排除非日期标识。
        # 2026-08-19 F5①：错失池写方（missed_opps_writer）依赖 analysis_signals
        # 的 wait 行，而 08-15 吞吐契约后 unified 轮不再落 wait —— 实测
        # max(ts)=2026-08-14 05:45，「7 天」口径返回的 823 笔全部来自 08-12~08-14。
        # 陈旧数据冒充当期比没有数据更坏，故先查断流再决定要不要出数。
        src_max = les.execute(
            "SELECT MAX(ts) FROM missed_opportunities WHERE ts LIKE '202%'"
        ).fetchone()
        src_max = str((src_max or [None])[0] or "")
        stall = les.execute(
            "SELECT CASE WHEN ? < datetime('now','-36 hours') "
            "THEN 1 ELSE 0 END", (src_max,)).fetchone()
        stalled = bool(src_max) and bool(stall and stall[0])
        if not src_max:
            print("  错过机会 7天: 池内无任何记录（写方从未落库，非「无错失」）")
        elif stalled:
            print(f"  ⚠️ 错失机会池断供：最后一行 {src_max}，超过 36h 未更新；"
                  "本段 7 天口径失效，不出数（0 是盲区不是事实）")
        else:
            mo = les.execute(
                "SELECT COUNT(*) AS c, ROUND(AVG(actual_4h_pct),2) AS avg4h, "
                "SUM(CASE WHEN would_hit_1r_fixed2pct=1 THEN 1 ELSE 0 END) AS hit1r "
                "FROM missed_opportunities WHERE ts LIKE '202%' AND ts>=datetime('now','-7 days')"
            ).fetchone()
            if mo and mo["c"]:
                print(f"  错过机会 7天: {mo['c']} 笔，均 4h 走幅 {mo['avg4h']}%，"
                      f"其中 {mo['hit1r']} 笔按固定±2%代理口径本可达 1R（非真实计划止损）")
        les.close()
    if not closure_cycle:
        safe(s_lessons)

    # L5 (2026-06-14): 简报自足断言——减重查靠让查询无必要，而非靠 skill 文字禁止（agent 会偷懒）。
    _covered = ["宏观", "行情", "技术面", "衍生品", "情绪", "关键新闻", "持仓"]
    if _SECTION_STATE.get("playbook_shown", True):
        _covered.append("playbook")
    if not closure_cycle:
        _covered += ["历史表现", "教训"]
    print("\n➤ 本简报已含本轮决策所需的主要库内数据（"
          + "/".join(_covered) + "）。")
    print("  常规轮据此即可决策，**无需再调 sqlite3/query_state 重查这些**（关键新闻节为空=已查过无要闻，勿再查 news.db）；")
    if not closure_cycle:
        print("  拟执行标的必须补查 find_similar_experience（正/负/错失三类）；此外仅在候选历史、消失仓 fills 或 N/A 段需要补查。")
    else:
        print("  closure策略不读取MTF、不要求历史经验/六项卡；低成交额/OI、"
              "无催化、已有仓位数或未触顶IMR不得单独成为reject理由。")
    mkt.close()
    acc.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default=_public_project_path('db'))
    ap.add_argument("--top", type=int, default=5)
    # 2026-07-15：exec(cp936 pwsh) 对 stdout 接管道会把中文 GBK 坏码——agent 需复读/截断时
    # 用 --out-file 落 UTF-8 文件后 read（文件通道绕开 shell 解码）。stdout 行为不变（纯加法）。
    ap.add_argument("--out-file", default=None)
    # 2026-08-18：dispatcher 传本轮 cycle_id → 两层候选追加写 logs/briefing/
    # candidates-YYYYMMDD.jsonl（错失池 briefing_layer_v1 源 + 连续候选轮数
    # 的确定性来源）。不带参数（agent 手动补跑）不写快照，渲染行为不变。
    ap.add_argument("--cycle-id", default=None)
    ap.add_argument("--candidate-out-file", default=None)
    ap.add_argument("--ready-pool-out-file", default=None)
    args = ap.parse_args()
    if (args.candidate_out_file or args.ready_pool_out_file) and not args.cycle_id:
        ap.error("candidate artifact output requires --cycle-id")
    if not args.out_file:
        _render(
            args.db_root, args.top, cycle_id=args.cycle_id,
            candidate_out_file=args.candidate_out_file,
            ready_pool_out_file=args.ready_pool_out_file)
        return
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    render_err = None
    try:
        with redirect_stdout(buf):
            _render(
                args.db_root, args.top, cycle_id=args.cycle_id,
                candidate_out_file=args.candidate_out_file,
                ready_pool_out_file=args.ready_pool_out_file)
    except BaseException as e:  # 渲染中途炸也要把已产出的部分照常吐 stdout + 落盘（与无 --out-file 的渐进输出对齐）
        render_err = e
    text = buf.getvalue()
    sys.stdout.write(text)
    write_ok = True
    try:
        with open(args.out_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except Exception as e:
        write_ok = False
        print(f"[out-file] write failed: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
    if render_err is not None:
        raise render_err
    if not write_ok:
        sys.exit(3)


if __name__ == "__main__":
    main()
