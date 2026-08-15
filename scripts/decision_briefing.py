# -*- coding: utf-8 -*-
"""P4 决策简报预处理器（T1，2026-06-12）。

五库汇总 → 紧凑标准简报（~2-3KB），P4 起手一次调用替代多次临场自查。
任何子段失败只标注 N/A，不中断（决策不能因简报缺段而停）。

用法:
  pwsh -NoProfile -File .\\scripts\\run_okx_python.ps1 .\\scripts\\decision_briefing.py [--db-root .\\db] [--top 5] [--out-file .\\tmp\\briefing_<stage>.md]

--out-file（2026-07-15）：stdout 照常输出（契约不变），同时把全文写入 UTF-8 文件。
agent exec 环境是 cp936 pwsh——对本脚本输出接管道/捕获（`| tail`/`| Select-Object`/`2>&1 |`）
会被按 GBK 解码坏成 `鍐崇瓥...`；需复读/截断一律 --out-file + read，禁再接管道。
"""
import argparse
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import exit_quality  # noqa: E402
from core.risk_validator import MAX_PORTFOLIO_IMR_RATIO  # noqa: E402
from core.multitimeframe_gate import (  # noqa: E402
    MINIMUM_BARS_FOR_FULL_INDICATORS,
    validate_kline_row,
)

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
_CALIBRATION_USAGE_ORDER = ("adopt", "partial", "ignore", "none", "unknown")
_CALIBRATION_HOLD_ORDER = ("<4h", "4-24h", "24-48h", ">=48h", "unknown")


def connect(db_root, name):
    con = sqlite3.connect(f"file:{db_root}\\{name}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


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
        parts = [f"spread={spread:.2f}bp", f"imb={imbalance:+.2f}"]
        if flow_fresh and taker_buy is not None:
            parts.append(f"buy={taker_buy:.0%}")
        if flow_fresh and cvd is not None:
            parts.append(f"CVD=${cvd / 1_000:+.0f}K")
        if flow_fresh and flow_sample_count is not None:
            parts.append(
                f"flowN/span={int(flow_sample_count)}/"
                f"{flow_span_ms / 1_000:.0f}s")
        elif taker_buy is not None or cvd is not None:
            parts.append("flow=N/A(stale_span)")
        if buy_slippage is not None and sell_slippage is not None:
            parts.append(
                f"slipB/S={buy_slippage:.2f}/{sell_slippage:.2f}bp")
        micro_text = "µ(" + " ".join(parts) + ")"
    else:
        micro_text = "µ=N/A"

    account_ratio = (
        finite(positioning_row, "long_short_ratio", minimum=0.0)
        if positioning_batch_passed else None
    )
    positioning_available = account_ratio is not None
    positioning_text = (
        f"acctL/S={account_ratio:.2f}"
        if positioning_available else "acctL/S=N/A"
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
    if any(value <= 0 for value in values):
        return None
    return values[0] * values[1] * values[2]


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
        items.append({
            "pnl_pct": pnl,
            "realized_pnl": realized_pnl,
            "usage": usage,
            "regime_side": (
                f"{_row_get(row, 'regime', 'unknown')}/"
                f"{_row_get(row, 'side', 'unknown')}"
            ),
            "hold_bucket": hold_bucket,
            "asset_class": asset_class,
        })

    return {
        "sample_n": len(items),
        "history_usage": _calibration_group_stats(
            items, "usage", _CALIBRATION_USAGE_ORDER),
        "regime_side": _calibration_group_stats(items, "regime_side"),
        "hold_bucket": _calibration_group_stats(
            items, "hold_bucket", _CALIBRATION_HOLD_ORDER),
        "asset_class": _calibration_group_stats(items, "asset_class"),
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
            "SELECT symbol,chg24h,last,vol24h FROM tick_snapshots "
            "WHERE ts=? AND chg24h IS NOT NULL", (tick_ts,)
        ).fetchall()
        liquid = [r for r in ticks
                  if (r["last"] or 0) * (r["vol24h"] or 0) >= MIN_QUOTE_VOL_USD]
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


def _render(root, top):
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
        print(f"  regime=**{r['regime']}** @ {fmt_age(r['ts'])} 前（事实标签，仅作决策参考）")
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
            + " | Fear&Greed "
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
            events = reg.execute(
                "SELECT event_ts,region,event,forecast,previous FROM macro_events "
                "WHERE importance=3 AND datetime(event_ts)>=datetime('now','+8 hours') "
                "ORDER BY datetime(event_ts) LIMIT 5"
            ).fetchall()
            if events:
                print("  未来高重要度事件:")
                for e in events:
                    print(f"    {e['event_ts']} {e['region']} {e['event']} "
                          f"(forecast={e['forecast'] or '-'} prev={e['previous'] or '-'})")
        except Exception:
            pass
    safe(s_macro)

    # ── 2. 行情纵览（chg24h 已落列） ─────────────────
    section(f"行情 Top{top}（流动性≥${MIN_QUOTE_VOL_USD/1e6:.0f}M）")

    def s_ticks():
        ts = mkt.execute("SELECT MAX(ts) AS m FROM tick_snapshots").fetchone()["m"]
        rows = mkt.execute(
            "SELECT symbol,last,chg24h,vol24h FROM tick_snapshots WHERE ts=? AND chg24h IS NOT NULL",
            (ts,),
        ).fetchall()
        liq = [r for r in rows if (r["vol24h"] or 0) * (r["last"] or 0) >= MIN_QUOTE_VOL_USD]
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
    safe(s_tech)

    # ── 2.6 高流动性可交易候选（双流动性闸 + 多周期结构） ──
    section(
        f"高流动性可交易候选（成交额≥${MIN_QUOTE_VOL_USD/1e6:.0f}M "
        f"且 OI≥${MIN_OI_USD/1e6:.0f}M；成熟趋势+早期结构两组）"
    )

    def s_tradeable_candidates():
        tick_ts = mkt.execute("SELECT MAX(ts) AS m FROM tick_snapshots").fetchone()["m"]
        deriv_ts = mkt.execute("SELECT MAX(ts) AS m FROM derivatives").fetchone()["m"]
        if not tick_ts or not deriv_ts:
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

        rows = mkt.execute(
            "SELECT t.symbol,t.last,t.chg24h,t.vol24h,i.ctVal,d.oi_usd,d.funding_rate "
            "FROM tick_snapshots t JOIN derivatives d ON d.symbol=t.symbol "
            "JOIN instruments_cache i ON i.instId=t.symbol "
            "WHERE t.ts=? AND d.ts=? AND t.chg24h IS NOT NULL "
            "AND i.ctVal IS NOT NULL AND i.ctVal>0",
            (tick_ts, deriv_ts),
        ).fetchall()

        ranked = []
        for r in rows:
            quote_vol = quote_volume_usd(r["last"], r["vol24h"], r["ctVal"])
            if (
                quote_vol is None
                or quote_vol < MIN_QUOTE_VOL_USD
                or (r["oi_usd"] or 0) < MIN_OI_USD
            ):
                continue
            if r["symbol"] in held:
                continue
            parts, votes, all_ready = [], {}, True
            for tf in DECISION_TIMEFRAMES:
                k, ready = closed_kline_readiness(
                    mkt, r["symbol"], tf, tick_ts)
                if not ready:
                    parts.append(f"{tf}:N/A")
                    all_ready = False
                    continue
                above = k["ma20"] is not None and k["c"] > k["ma20"]
                macd_up = k["macd_hist"] is not None and k["macd_hist"] > 0
                votes[tf] = (1 if above else -1) + (1 if macd_up else -1)
                trend = "↑MA" if above else "↓MA"
                rsi = f"R{k['rsi14']:.0f}" if k["rsi14"] is not None else "R?"
                macd = "M+" if macd_up else "M-"
                parts.append(f"{tf}{trend}/{rsi}/{macd}")
            if not all_ready:
                continue
            trend_vote = sum(votes.values())
            bias = "偏多" if trend_vote >= 2 else ("偏空" if trend_vote <= -2 else "混合")
            # 先看多周期一致性，再看实际波动；成交额/OI 仅用于同分时稳定排序。
            rank_key = (
                abs(trend_vote),
                min(abs(r["chg24h"] or 0), 20),
                min(quote_vol / MIN_QUOTE_VOL_USD, 100),
                min((r["oi_usd"] or 0) / MIN_OI_USD, 100),
            )
            ranked.append({
                "row": r,
                "parts": parts,
                "trend_vote": trend_vote,
                "votes": votes,
                "bias": bias,
                "quote_vol": quote_vol,
                "rank_key": rank_key,
            })

        ranked.sort(key=lambda x: x["rank_key"], reverse=True)
        bullish = [x for x in ranked if x["trend_vote"] >= 2]
        bearish = [x for x in ranked if x["trend_vote"] <= -2]
        picked = bullish[:TRADEABLE_CANDIDATE_COUNT // 2]
        picked += bearish[:TRADEABLE_CANDIDATE_COUNT // 2]
        seen = {x["row"]["symbol"] for x in picked}
        for x in ranked:
            if len(picked) >= TRADEABLE_CANDIDATE_COUNT:
                break
            if x["row"]["symbol"] not in seen:
                picked.append(x)
                seen.add(x["row"]["symbol"])
        picked.sort(key=lambda x: x["rank_key"], reverse=True)

        # 早期结构组（2026-08-14）：成熟组排序天然只出「已走完」的晚期结构，
        # 模型有正当理由「不追」→ 统计上就是 wait 74%。本组专门呈现 4H 满票
        # 立向、15m 未同向（回调/整理未走完）的候选，给「还没走完」的结构一个
        # 不被晚期结构挤掉的独立配额；仍只是证据，非下单指令。
        early_pool = []
        for x in ranked:
            if x["row"]["symbol"] in seen:
                continue
            e_side = early_structure_side(x["votes"])
            if e_side is None:
                continue
            agree_1h = (x["votes"].get("1H") or 0) * x["votes"]["4H"] > 0
            x["early_side"] = e_side
            x["early_key"] = (
                1 if agree_1h else 0,
                min(x["quote_vol"] / MIN_QUOTE_VOL_USD, 100),
                min((x["row"]["oi_usd"] or 0) / MIN_OI_USD, 100),
            )
            early_pool.append(x)
        early_pool.sort(key=lambda x: x["early_key"], reverse=True)
        e_long = [x for x in early_pool if x["early_side"] == "long"]
        e_short = [x for x in early_pool if x["early_side"] == "short"]
        early = e_long[:EARLY_STRUCTURE_CANDIDATE_COUNT // 2]
        early += e_short[:EARLY_STRUCTURE_CANDIDATE_COUNT // 2]
        e_seen = {x["row"]["symbol"] for x in early}
        for x in early_pool:
            if len(early) >= EARLY_STRUCTURE_CANDIDATE_COUNT:
                break
            if x["row"]["symbol"] not in e_seen:
                early.append(x)
                e_seen.add(x["row"]["symbol"])
        early.sort(key=lambda x: x["early_key"], reverse=True)

        if not picked and not early:
            print("  无符合双流动性闸且技术数据完整的候选")
            return

        # 连续 wait 轮数：每 cycle 独立 session、无跨轮记忆——「已连等 N 轮」
        # 必须由简报外显，新 session 才看得见自己在拖延。
        streaks = {}
        try:
            ana = connect(root, "analysis.db")
            try:
                for x in picked + early:
                    sym = x["row"]["symbol"]
                    streak, w_side = consecutive_wait_streak(ana, sym)
                    if streak >= WAIT_STREAK_MIN_DISPLAY:
                        streaks[sym] = (streak, w_side)
            finally:
                ana.close()
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

        def cand_line(x, bias_label):
            r = x["row"]
            funding = (
                f"{r['funding_rate']*100:+.4f}%"
                if r["funding_rate"] is not None else "N/A"
            )
            st = streaks.get(r["symbol"])
            streak_s = f" 连wait{st[0]}轮({st[1] or '?'})" if st else ""
            soft = soft_evidence.get(
                str(r["symbol"]), candidate_soft_evidence())
            return (
                f"  {r['symbol'].split('-')[0]} {bias_label} "
                f"chg{r['chg24h']:+.1f}% vol${x['quote_vol']/1e6:.0f}M "
                f"OI${r['oi_usd']/1e6:.0f}M funding={funding}{streak_s} | "
                + " ".join(x["parts"])
                + f" | {soft['text']}"
            )

        print("  「成熟趋势」三周期已对齐（结构成熟度最高，是否追高自辨）:")
        if picked:
            for x in picked:
                print(cand_line(x, x["bias"]))
        else:
            print("    无")
        print("  「早期结构」4H 立向、15m 未同向（回调/整理未走完）:")
        if early:
            for x in early:
                print(cand_line(
                    x, "早多" if x["early_side"] == "long" else "早空"))
        else:
            print("    无（本轮无 4H 立向且短周期回调中的候选）")

        micro_covered = sum(
            bool(item["micro_available"]) for item in soft_evidence.values())
        positioning_covered = sum(
            bool(item["positioning_available"])
            for item in soft_evidence.values()
        )
        print(
            "  候选软证据覆盖: "
            f"micro={micro_covered}/{len(candidate_symbols)}"
            f"@{fmt_age(micro_ts) if micro_ts else 'N/A'}前，"
            f"官方账户L/S={positioning_covered}/{len(candidate_symbols)}"
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
            "  （两组仅候选来源不同，均非下单指令；连wait轮数=该标的最近连续"
            " wait 的分析轮数，自查是否在无新证据下重复拖延。统一 live 仍须"
            "结合新闻、历史相似度、风险回报和组合暴露自主决断）"
        )
    safe(s_tradeable_candidates)

    # ── 3. 衍生品极值 ────────────────────────────────
    section("衍生品极值（资金费 8h）")

    def s_deriv():
        ts = mkt.execute("SELECT MAX(ts) AS m FROM derivatives").fetchone()["m"]
        rows = mkt.execute(
            "SELECT symbol,funding_rate,premium,oi,oi_usd FROM derivatives WHERE ts=? AND funding_rate IS NOT NULL",
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
            print(f"  {r['symbol'].split('-')[0]} funding {r['funding_rate']*100:+.4f}%（年化{ann:+.0f}%）{oi_s}{delta_s}")
        print(f"  （{len(rows)} 币 @ {fmt_age(ts)} 前；极端正费率=多头拥挤，反之亦然）")
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
            flow = (f"buy={r['taker_buy_ratio']:.0%} CVD=${r['cvd_notional_usd']/1e3:+.0f}K "
                    f"n={r['sample_count']}/{span_s:.0f}s"
                    if r["taker_buy_ratio"] is not None else "flow=N/A")
            slip = (f"{r['buy_slippage_500usd_bps']:.2f}/{r['sell_slippage_500usd_bps']:.2f}bp"
                    if r["buy_slippage_500usd_bps"] is not None
                    and r["sell_slippage_500usd_bps"] is not None else "N/A")
            print(f"  {r['symbol'].split('-')[0]} spread={r['spread_bps']:.2f}bp "
                  f"depth±25bp=${depth25/1e3:.0f}K imbalance={r['imbalance_25bp']:+.2f} "
                  f"slip$500(buy/sell)={slip} {flow}")
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
                print(f"    {tag} 组合观察: 0 仓 | gross=0 | 保证金≈0")
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
                warns.append("gross≥3x")
            if len(rows) >= 2 and same >= 0.80:
                warns.append("同向≥80%")
            if len(rows) >= 2 and largest >= 0.60:
                warns.append("单仓≥60%gross")
            warn_s = f" | ⚠️ {','.join(warns)}" if warns else ""
            print(
                f"    {tag} 组合观察: {len(rows)}仓 | gross=${gross:.2f}/{gross_x} | "
                f"逐仓保证金求和≈${margin:.2f}/{margin_pct} | net={net_x} | "
                f"同向={same:.0%} | 最大仓={largest:.0%}gross{warn_s}"
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
                print(f"  {c['profile']} 交易所账单24h: n={c['n']} fee={c['fee']:+.4f} "
                      f"funding={c['funding_cashflow']:+.4f} netΔ={c['net_change']:+.4f} USDT")
        except Exception:
            pass
    safe(s_pos)

    # ── 6. playbook 候选（真实战绩警示） ──────────────
    section("playbook（评估再用，战绩为准）")

    def s_play():
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
            eq_block = exit_quality.compute(
                account_db=Path(root) / "account.db",
                live_trades_db=Path(root) / "live_trades.db",
                report_start_ts=(
                    datetime.now(exit_quality.CST) - timedelta(days=7)
                ).strftime("%Y-%m-%d %H:%M:%S"),
                report_end_ts=datetime.now(exit_quality.CST).strftime(
                    "%Y-%m-%d %H:%M:%S"),
            )
            gb = eq_block["peak_giveback"]
            mr = eq_block["margin_return_review"]
            rate = mr["explicit_review_rate"]
            print(
                "  退出质量7d（后验窗已成熟部分；仅参考，不设自动闸）: "
                f"曾达1R {gb['reached_1r']} 笔 / 平仓仍≥1R "
                f"{gb['closed_at_or_above_1r']} 笔，错失止盈池 "
                f"{gb['missed_take_profit_pool_size']} 笔；峰值≥"
                f"{gb['profitable_peak_threshold_r']}R 的中位回吐 "
                f"{gb['profitable_peak_giveback_median_r']}R；"
                f"保证金收益率≥{mr['threshold_fraction']:.0%} 被标记 "
                f"{mr['flagged_position_cycles']} 次、显式复核率 "
                + ("无样本" if rate is None else f"{rate:.0%}")
                + f"，处置 {mr['disposition_counts'] or '无'}"
            )
            for item in gb["missed_take_profit_pool"][:3]:
                print(
                    f"    · {item['symbol']} {item['side']} 峰值 "
                    f"{item['peak_r']}R → 平仓 {item['realized_r_net']}R"
                    f"（出口 {item['exit_category']}）"
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
    safe(s_experience)

    # ── 7. 历史表现基线（不映射评分/置信度档位） ───────
    section("历史表现基线（30 天真实成交；仅参考）")

    def s_calib():
        exp_cols = {str(r[1]) for r in acc.execute(
            "PRAGMA table_info(trade_experiences)").fetchall()}
        close_clock = "COALESCE(closed_at,ts)" if "closed_at" in exp_cols else "ts"
        vector_sql = (
            "experience_vector" if "experience_vector" in exp_cols
            else "NULL AS experience_vector"
        )
        cutoff = (datetime.now(CST) - timedelta(days=30)).strftime(
            "%Y-%m-%d %H:%M:%S")
        rows = acc.execute(
            "SELECT cycle_id,symbol,side,regime,pnl_pct,realized_pnl,"
            "hold_hours,raw,"
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
    safe(s_calib)

    # ── 8. lessons 回灌（T6） ────────────────────────
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
            print("  近 30 天无有效错误模式")
        for r in eps:
            print(f"  错误模式[{r['hit_count']}次] {r['pattern_name']}: {str(r['trigger_condition'])[:48]}")
        # ts LIKE '202%' 只读取 ISO 日期行，排除非日期标识。
        mo = les.execute(
            "SELECT COUNT(*) AS c, ROUND(AVG(actual_4h_pct),2) AS avg4h, "
            "SUM(CASE WHEN would_hit_1r_fixed2pct=1 THEN 1 ELSE 0 END) AS hit1r "
            "FROM missed_opportunities WHERE ts LIKE '202%' AND ts>=datetime('now','-7 days')"
        ).fetchone()
        if mo and mo["c"]:
            print(f"  错过机会 7天: {mo['c']} 笔，均 4h 走幅 {mo['avg4h']}%，"
                  f"其中 {mo['hit1r']} 笔按固定±2%代理口径本可达 1R（非真实计划止损）")
        les.close()
    safe(s_lessons)

    # L5 (2026-06-14): 简报自足断言——减重查靠让查询无必要，而非靠 skill 文字禁止（agent 会偷懒）。
    print("\n➤ 本简报已含本轮决策所需的主要库内数据（宏观/行情/技术面/衍生品/情绪/关键新闻/持仓/playbook/历史表现/教训）。")
    print("  常规轮据此即可决策，**无需再调 sqlite3/query_state 重查这些**（关键新闻节为空=已查过无要闻，勿再查 news.db）；")
    print("  拟执行标的必须补查 find_similar_experience（正/负/错失三类）；此外仅在候选历史、消失仓 fills 或 N/A 段需要补查。")
    mkt.close()
    acc.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default=r".\db")
    ap.add_argument("--top", type=int, default=5)
    # 2026-07-15：exec(cp936 pwsh) 对 stdout 接管道会把中文 GBK 坏码——agent 需复读/截断时
    # 用 --out-file 落 UTF-8 文件后 read（文件通道绕开 shell 解码）。stdout 行为不变（纯加法）。
    ap.add_argument("--out-file", default=None)
    args = ap.parse_args()
    if not args.out_file:
        _render(args.db_root, args.top)
        return
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    render_err = None
    try:
        with redirect_stdout(buf):
            _render(args.db_root, args.top)
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
