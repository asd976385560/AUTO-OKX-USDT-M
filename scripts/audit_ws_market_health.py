# -*- coding: utf-8 -*-
"""审计 OKX 公共行情 WS 影子/双读运行事实。"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from _okx_market_source import load_source_config
from _audit_artifact_context import resolve_audit_output
from _okx_ws_cache import (
    CANDLE_RETENTION,
    CacheReader,
    candle_bar_end_ms,
    expected_closed_start_ms,
    ms_to_iso,
    utc_now_iso,
)

ROOT = Path(_public_project_path())
CST = timezone(timedelta(hours=8))


def percentile(values: Sequence[float], fraction: float) -> float | None:
    ordered = sorted(float(item) for item in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def dist(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": _rounded(percentile(values, 0.50)),
        "p95": _rounded(percentile(values, 0.95)),
        "p99": _rounded(percentile(values, 0.99)),
        "max": _rounded(max(values) if values else None),
    }


def _rounded(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def phase_interpretation(mode: str | None, status: str) -> str:
    """Return phase-aware operator guidance without changing any gate."""
    current = str(mode or "rest_only")
    common = "失败、缺失与差异全部保留在分母；本审计不自动改变数据源阶段。"
    if status == "NOT_MET":
        return f"{current}观察窗硬门未通过；按既有回滚/修复规则处理。{common}"
    if current == "shadow":
        prefix = (
            "shadow观察窗尚未成熟，禁止提前推进dual_read。"
            if status == "PENDING"
            else "shadow观察窗硬门已通过，但阶段推进仍须走原子管理入口。"
        )
    elif current == "dual_read":
        prefix = (
            "dual_read观察窗尚未成熟，禁止提前推进ws_first。"
            if status == "PENDING"
            else "dual_read观察窗硬门已通过，但阶段推进仍须走原子管理入口。"
        )
    elif current == "ws_first":
        prefix = (
            "ws_first前向观察窗尚未成熟；当前已运行ws_first，这不表示回退或重新等待切换。"
            if status == "PENDING"
            else "ws_first前向观察窗硬门已通过；这不替代完整周期SLA或其它业务验收。"
        )
    else:
        prefix = (
            "rest_only为当前回滚生产模式；WS证据只作诊断，不授权自动切源。"
        )
    return prefix + common


def exit_code_for_status(status: str) -> int:
    """Separate a valid business NOT_MET result from process/contract failure."""
    if status in {"PENDING", "PASSED"}:
        return 0
    if status == "NOT_MET":
        return 1
    return 2


def audit(
    cache_db: Path,
    market_db: Path,
    required_hours: int,
) -> dict[str, Any]:
    reader = CacheReader(cache_db)
    snapshot = reader.health_snapshot()
    config = load_source_config()
    cache = sqlite3.connect(
        f"file:{cache_db.as_posix()}?mode=ro", uri=True, timeout=10
    )
    cache.row_factory = sqlite3.Row
    market = sqlite3.connect(
        f"file:{market_db.as_posix()}?mode=ro", uri=True, timeout=20
    )
    market.row_factory = sqlite3.Row
    try:
        meta = {
            row["key"]: row["value"]
            for row in cache.execute("SELECT key,value FROM cache_meta")
        }
        service_started_at = meta.get("service_started_at")
        started, started_at, window_basis = _resolve_window_start(
            config, service_started_at
        )
        query_started_at = _sqlite_query_start(started)
        now = datetime.now(timezone.utc)
        elapsed_hours = (
            max(0.0, (now - started).total_seconds() / 3600) if started else 0.0
        )
        live_symbols = [
            str(row[0])
            for row in cache.execute(
                """
                SELECT inst_id FROM instruments
                WHERE state='live' AND settle_ccy='USDT' AND ct_type='linear'
                ORDER BY inst_id
                """
            ).fetchall()
        ]
        live_set = set(live_symbols)
        list_times = reader.instrument_list_times(live_symbols)
        connections = [dict(row) for row in cache.execute(
            "SELECT * FROM connection_health ORDER BY group_id"
        )]
        expected_args = sum(int(row["expected_args"] or 0) for row in connections)
        acked_args = sum(int(row["acked_args"] or 0) for row in connections)
        all_connected = bool(connections) and all(
            row["status"] == "connected"
            and int(row["expected_args"] or 0) == int(row["acked_args"] or 0)
            for row in connections
        )
        trade_expected = int(
            cache.execute(
                "SELECT count(DISTINCT inst_id) FROM subscriptions "
                "WHERE channel='trades-all' AND status='acked'"
            ).fetchone()[0]
        )
        trade_complete = int(
            cache.execute(
                """
                SELECT count(DISTINCT completeness.inst_id)
                FROM stream_completeness AS completeness
                JOIN subscriptions AS subscription
                  ON subscription.channel=completeness.channel
                 AND subscription.inst_id=completeness.inst_id
                 AND subscription.status='acked'
                WHERE completeness.channel='trades-all'
                  AND completeness.status='complete'
                """
            ).fetchone()[0]
        )
        latest_expected = int(
            cache.execute(
                "SELECT count(*) FROM subscriptions "
                "WHERE channel IN ('tickers','funding-rate','open-interest',"
                "'mark-price','index-tickers') AND status='acked'"
            ).fetchone()[0]
        )
        latest_current_epoch = int(
            cache.execute(
                """
                SELECT count(*)
                FROM subscriptions AS subscription
                JOIN connection_health AS health
                  ON health.group_id=subscription.group_id
                 AND health.status='connected'
                JOIN latest_market AS market
                  ON market.channel=subscription.channel
                 AND market.inst_id=subscription.inst_id
                 AND market.conn_epoch=health.conn_epoch
                WHERE subscription.channel IN (
                    'tickers','funding-rate','open-interest','mark-price','index-tickers'
                ) AND subscription.status='acked'
                """
            ).fetchone()[0]
        )

        candle_audits = {}
        total_mismatches = 0
        total_window_expected = 0
        total_window_ws = 0
        total_window_mismatches = 0
        combined_close_latencies: list[float] = []
        now_ms = int(now.timestamp() * 1000)
        comparison_cutoff_ms = now_ms - 10 * 60 * 1000
        for bar in ("15m", "1H", "4H", "1D", "1W", "1M"):
            bar_started, bar_retention_clamped = _effective_window_start(
                bar, started, now
            )
            bar_start_ms = (
                int(bar_started.timestamp() * 1000)
                if bar_started
                else now_ms
            )
            expected_ms = expected_closed_start_ms(bar)
            expected_iso = ms_to_iso(expected_ms)
            expected_end_ms = candle_bar_end_ms(bar, expected_ms)
            applicable_symbols = [
                symbol
                for symbol in live_symbols
                if list_times.get(symbol) is None
                or int(list_times[symbol]) < int(expected_end_ms)
            ]
            applicable_set = set(applicable_symbols)
            ws_rows = {
                str(row["inst_id"]): row
                for row in cache.execute(
                    """
                    SELECT inst_id,open,high,low,close,volume_quote,source,
                           close_latency_ms
                    FROM candles WHERE timeframe=? AND ts_ms=?
                    """,
                    (bar, expected_ms),
                ).fetchall()
                if str(row["inst_id"]) in applicable_set
            }
            market_rows = {
                str(row["symbol"]): row
                for row in market.execute(
                    """
                    SELECT symbol,o,h,l,c,v FROM kline_cache
                    WHERE tf=? AND ts=?
                    """,
                    (bar, expected_iso),
                ).fetchall()
                if str(row["symbol"]) in applicable_set
            }
            mismatch_samples = []
            compared = 0
            for symbol in applicable_symbols:
                left = ws_rows.get(symbol)
                right = market_rows.get(symbol)
                if left is None or right is None:
                    continue
                compared += 1
                ws_value = tuple(_text(left[key]) for key in (
                    "open", "high", "low", "close", "volume_quote"
                ))
                market_value = tuple(_text(right[key]) for key in (
                    "o", "h", "l", "c", "v"
                ))
                if ws_value != market_value:
                    mismatch_samples.append(
                        {"symbol": symbol, "cache": ws_value, "market": market_value}
                    )
            total_mismatches += len(mismatch_samples)
            raw_ws = sum(
                1 for row in ws_rows.values() if str(row["source"]) == "ws"
            )
            latencies = [
                float(row[0]) / 1000
                for row in cache.execute(
                    """
                    SELECT close_latency_ms FROM candles
                    WHERE timeframe=? AND source='ws' AND received_at>=?
                          AND close_latency_ms IS NOT NULL
                    """,
                    (bar, query_started_at or "9999-12-31T00:00:00Z"),
                ).fetchall()
            ]
            combined_close_latencies.extend(latencies)
            expected_closes = (
                _expected_close_count(bar, bar_started, now)
                if bar_started
                else 0
            )
            window_expected = (
                sum(
                    _expected_close_count(
                        bar,
                        max(
                            bar_started,
                            datetime.fromtimestamp(list_times[symbol] / 1000, timezone.utc),
                        )
                        if list_times.get(symbol) is not None
                        else bar_started,
                        now,
                    )
                    for symbol in live_symbols
                )
                if bar_started
                else 0
            )
            window_ws_rows = cache.execute(
                    """
                    SELECT inst_id,bar_end_ms FROM candles
                    WHERE timeframe=? AND source='ws'
                          AND bar_end_ms>? AND bar_end_ms<=?
                    """,
                    (bar, bar_start_ms, now_ms),
                ).fetchall()
            window_ws = sum(
                1
                for row in window_ws_rows
                if str(row["inst_id"]) in live_set
                and (
                    list_times.get(str(row["inst_id"])) is None
                    or int(list_times[str(row["inst_id"])]) < int(row["bar_end_ms"])
                )
            )
            total_window_expected += window_expected
            total_window_ws += window_ws

            window_rows = [
                row
                for row in cache.execute(
                """
                SELECT inst_id,ts_ms,open,high,low,close,volume_quote
                FROM candles
                WHERE timeframe=? AND source='ws'
                      AND bar_end_ms>? AND bar_end_ms<=?
                ORDER BY ts_ms,inst_id
                """,
                (bar, bar_start_ms, comparison_cutoff_ms),
                ).fetchall()
                if str(row["inst_id"]) in live_set
                and (
                    list_times.get(str(row["inst_id"])) is None
                    or int(list_times[str(row["inst_id"])])
                    < candle_bar_end_ms(bar, int(row["ts_ms"]))
                )
            ]
            market_window: dict[tuple[str, int], tuple[str | None, ...]] = {}
            if window_rows:
                minimum_iso = ms_to_iso(min(int(row["ts_ms"]) for row in window_rows))
                maximum_iso = ms_to_iso(max(int(row["ts_ms"]) for row in window_rows))
                market_window = {
                    (str(row["symbol"]), int(_iso_ms(row["ts"]) or -1)): tuple(
                        _text(row[key]) for key in ("o", "h", "l", "c", "v")
                    )
                    for row in market.execute(
                        """
                        SELECT symbol,ts,o,h,l,c,v FROM kline_cache
                        WHERE tf=? AND ts>=? AND ts<=?
                        """,
                        (bar, minimum_iso, maximum_iso),
                    ).fetchall()
                }
            window_mismatch_samples = []
            window_compared = 0
            for row in window_rows:
                key = (str(row["inst_id"]), int(row["ts_ms"]))
                ws_value = tuple(
                    _text(row[name])
                    for name in ("open", "high", "low", "close", "volume_quote")
                )
                market_value = market_window.get(key)
                if market_value is None or market_value != ws_value:
                    window_mismatch_samples.append(
                        {
                            "symbol": key[0],
                            "ts_ms": key[1],
                            "cache": ws_value,
                            "market": market_value,
                        }
                    )
                else:
                    window_compared += 1
            total_window_mismatches += len(window_mismatch_samples)
            candle_audits[bar] = {
                "expected_ts_ms": expected_ms,
                "expected_ts_utc": expected_iso,
                "denominator": len(applicable_symbols),
                "not_applicable_pre_listing": sorted(live_set - applicable_set),
                "cache_coverage": len(ws_rows),
                "raw_ws_coverage": raw_ws,
                "market_rest_coverage": len(market_rows),
                "compared": compared,
                "unresolved_mismatches": len(mismatch_samples),
                "mismatch_samples": mismatch_samples[:8],
                "close_latency_seconds": dist(latencies),
                "window_effective_started_at": (
                    bar_started.astimezone(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                    if bar_started
                    else None
                ),
                "window_retention_bars": CANDLE_RETENTION,
                "window_retention_clamped": bar_retention_clamped,
                "window_expected_closes": expected_closes,
                "window_expected_rows": window_expected,
                "window_raw_ws_rows": window_ws,
                "window_raw_ws_rate": (
                    round(window_ws / window_expected, 8)
                    if window_expected
                    else None
                ),
                "window_compared_after_10m_settlement": window_compared,
                "window_unresolved_mismatches": len(window_mismatch_samples),
                "window_mismatch_samples": window_mismatch_samples[:8],
            }

        samples = cache.execute(
            "SELECT * FROM service_samples WHERE ts>=? ORDER BY ts",
            (query_started_at or "9999-12-31T00:00:00Z",),
        ).fetchall()
        rss_mib = [float(row["rss_bytes"]) / 1024 / 1024 for row in samples if row["rss_bytes"]]
        cpu = [float(row["cpu_cores"]) for row in samples if row["cpu_cores"] is not None]
        drops = sum(int(row["required_drops"] or 0) for row in samples)
        event_rows = [
            dict(row)
            for row in cache.execute(
                "SELECT * FROM connection_events WHERE ts>=? ORDER BY id",
                (query_started_at or "9999-12-31T00:00:00Z",),
            ).fetchall()
        ]
        actual_recovery = _actual_recovery_seconds(event_rows)
        startup_connection = [
            float(row["duration_ms"]) / 1000
            for row in event_rows
            if row.get("event") == "ready" and row.get("duration_ms") is not None
        ][: max(1, len(connections))]
        recovery = actual_recovery or startup_connection
        planned_disconnects = sum(
            1
            for row in event_rows
            if row["event"] in {"planned_disconnect"}
            or (row["event"] == "disconnected" and row.get("detail") == "cancelled")
        )
        unplanned_disconnects = sum(
            1
            for row in event_rows
            if row["event"] == "disconnected" and row.get("detail") != "cancelled"
        )
        persisted_confirm0 = int(
            cache.execute("SELECT count(*) FROM candles WHERE confirm<>1").fetchone()[0]
        )
    finally:
        cache.close()
        market.close()

    current_gates = {
        "cache_quick_check": snapshot["quick_check"] == "ok",
        "service_heartbeat": bool(snapshot["health"]["healthy"]),
        "subscriptions_fully_acked": all_connected and expected_args == acked_args,
        "latest_state_current_epoch": (
            latest_expected > 0 and latest_current_epoch == latest_expected
        ),
        "trade_streams_complete": (
            trade_expected > 0 and trade_complete == trade_expected
        ),
        "persisted_confirm0_zero": persisted_confirm0 == 0,
        "required_drops_zero": drops == 0,
        "unresolved_ohlcv_mismatches_zero": (
            total_mismatches == 0 and total_window_mismatches == 0
        ),
        "raw_ws_coverage_gte_99pct": (
            total_window_expected > 0
            and total_window_ws / total_window_expected >= 0.99
        ),
        "candle_close_p99_lte_30s": (
            percentile(combined_close_latencies, 0.99) is not None
            and float(percentile(combined_close_latencies, 0.99)) <= 30
        ),
        "recovery_p99_lte_120s": (
            percentile(recovery, 0.99) is not None
            and float(percentile(recovery, 0.99)) <= 120
        ),
        "rss_p95_lte_512_mib": (
            percentile(rss_mib, 0.95) is not None
            and float(percentile(rss_mib, 0.95)) <= 512
        ),
        "cpu_p95_lte_2_cores": (
            percentile(cpu, 0.95) is not None
            and float(percentile(cpu, 0.95)) <= 2
        ),
    }
    mature = elapsed_hours >= required_hours
    status = (
        "PENDING"
        if not mature
        else ("PASSED" if all(current_gates.values()) else "NOT_MET")
    )
    return {
        "schema_version": 2,
        "generated_at": utc_now_iso(),
        "mode": config.get("mode"),
        "window": {
            "service_started_at": service_started_at,
            "phase_started_at": config.get("phase_started_at_cst"),
            "window_started_at": started_at,
            "window_basis": window_basis,
            "elapsed_hours": round(elapsed_hours, 4),
            "required_hours": required_hours,
            "mature": mature,
        },
        "status": status,
        "live_symbols": len(live_symbols),
        "subscriptions": {
            "expected": expected_args,
            "acked": acked_args,
            "connections": len(connections),
            "all_connected": all_connected,
            "trade_stream_expected": trade_expected,
            "trade_stream_complete": trade_complete,
            "latest_state_expected": latest_expected,
            "latest_state_current_epoch": latest_current_epoch,
        },
        "candles": candle_audits,
        "candle_window_summary": {
            "expected_rows": total_window_expected,
            "raw_ws_rows": total_window_ws,
            "raw_ws_rate": (
                round(total_window_ws / total_window_expected, 8)
                if total_window_expected
                else None
            ),
            "unresolved_mismatches_after_10m_settlement": total_window_mismatches,
            "combined_close_latency_seconds": dist(combined_close_latencies),
        },
        "resources": {
            "rss_mib": dist(rss_mib),
            "cpu_cores": dist(cpu),
            "required_drops_total": drops,
            "recovery_seconds": dist(recovery),
            "recovery_basis": (
                "unplanned_disconnect_to_ready"
                if actual_recovery
                else "initial_connection_to_ready"
            ),
            "startup_connection_seconds": dist(startup_connection),
            "actual_recovery_seconds": dist(actual_recovery),
            "planned_disconnects": planned_disconnects,
            "unplanned_disconnects": unplanned_disconnects,
        },
        "current_gates": current_gates,
        "persisted_confirm0": persisted_confirm0,
        "cache_snapshot": snapshot,
        "interpretation": phase_interpretation(config.get("mode"), status),
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _resolve_window_start(
    config: dict[str, Any], service_started_at: str | None
) -> tuple[datetime | None, str | None, str]:
    """Use the configured phase boundary so each maturity round gets its own window."""
    phase_started = _parse_iso(str(config.get("phase_started_at_cst") or ""))
    if phase_started is not None:
        normalized = phase_started.astimezone(timezone.utc)
        return normalized, normalized.isoformat().replace("+00:00", "Z"), (
            "config_phase_started_at_cst"
        )
    service_started = _parse_iso(service_started_at)
    if service_started is not None:
        normalized = service_started.astimezone(timezone.utc)
        return normalized, normalized.isoformat().replace("+00:00", "Z"), (
            "cache_service_started_at"
        )
    return None, None, "missing"


def _sqlite_query_start(started: datetime | None) -> str | None:
    """Return a conservative whole-second lower bound for second-resolution cache rows."""
    if started is None:
        return None
    normalized = started.astimezone(timezone.utc)
    if normalized.microsecond:
        normalized = (normalized + timedelta(seconds=1)).replace(microsecond=0)
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_ms(value: str | None) -> int | None:
    parsed = _parse_iso(value)
    return int(parsed.timestamp() * 1000) if parsed else None


def _retention_window_start(bar: str, now: datetime) -> datetime:
    """返回使 ``_expected_close_count(bar, start, now) == CANDLE_RETENTION`` 的起点。

    缓存对每个 (标的, 周期) 只保留最新 ``CANDLE_RETENTION`` 根收盘棒
    （见 ``_okx_ws_cache`` 的裁剪 DELETE），更早的行按设计被删除。
    """
    if bar in {"15m", "1H", "4H"}:
        seconds = {"15m": 900, "1H": 3600, "4H": 14400}[bar]
        return now - timedelta(seconds=CANDLE_RETENTION * seconds)
    local = now.astimezone(CST)
    if bar == "1D":
        return local - timedelta(days=CANDLE_RETENTION)
    if bar == "1W":
        return local - timedelta(weeks=CANDLE_RETENTION)
    if bar == "1M":
        index = local.year * 12 + local.month - CANDLE_RETENTION
        year, month_zero = divmod(index - 1, 12)
        return datetime(year, month_zero + 1, 1, tzinfo=CST)
    raise ValueError(f"unsupported bar: {bar}")


def _effective_window_start(
    bar: str,
    started: datetime | None,
    now: datetime,
) -> tuple[datetime | None, bool]:
    """覆盖率窗口起点按缓存保留视界钳制，返回 (起点, 是否被钳制)。

    保留视界内的失败、缺失与差异仍全部留在分母；这里只把「缓存按设计
    已裁剪的更早历史」移出期望值，避免固定锚点窗口超过保留期后覆盖率
    机械性衰减（15m 仅保留 120 根 = 30 小时，1H 为 120 小时，依此类推）。
    """
    if started is None:
        return None, False
    retention_start = _retention_window_start(bar, now)
    if retention_start > started:
        return retention_start, True
    return started, False


def _expected_close_count(
    bar: str,
    start: datetime,
    end: datetime,
) -> int:
    if end <= start:
        return 0
    if bar in {"15m", "1H", "4H"}:
        seconds = {"15m": 900, "1H": 3600, "4H": 14400}[bar]
        return max(0, int(end.timestamp() // seconds - start.timestamp() // seconds))
    start_local = start.astimezone(CST)
    end_local = end.astimezone(CST)
    if bar == "1D":
        return max(0, (end_local.date() - start_local.date()).days)
    if bar == "1W":
        start_monday = start_local.date() - timedelta(days=start_local.weekday())
        end_monday = end_local.date() - timedelta(days=end_local.weekday())
        return max(0, (end_monday - start_monday).days // 7)
    if bar == "1M":
        start_index = start_local.year * 12 + start_local.month
        end_index = end_local.year * 12 + end_local.month
        return max(0, end_index - start_index)
    raise ValueError(f"unsupported bar: {bar}")


def _actual_recovery_seconds(events: Sequence[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for index, row in enumerate(events):
        if row.get("event") != "disconnected" or row.get("detail") == "cancelled":
            continue
        disconnected = _parse_iso(str(row.get("ts") or ""))
        if disconnected is None:
            continue
        for later in events[index + 1 :]:
            if (
                later.get("group_id") == row.get("group_id")
                and later.get("event") == "ready"
            ):
                ready = _parse_iso(str(later.get("ts") or ""))
                if ready is not None:
                    values.append(max(0.0, (ready - disconnected).total_seconds()))
                break
    return values


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        # SQLite REAL 与 WS 文本统一到可复现的十进制表示。
        return format(float(value), ".15g")
    except (TypeError, ValueError):
        return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="审计OKX公共行情WS影子健康")
    parser.add_argument("--cache-db", default=str(ROOT / "db" / "ws_market_cache.db"))
    parser.add_argument("--market-db", default=str(ROOT / "db" / "market.db"))
    parser.add_argument("--required-hours", type=int, default=24)
    parser.add_argument("--json-out")
    parser.add_argument("--execution-context", choices=("production", "test", "probe"))
    parser.add_argument("--artifact-root")
    args = parser.parse_args()
    payload = audit(
        Path(args.cache_db), Path(args.market_db), max(1, args.required_hours)
    )
    if args.json_out:
        output, _context, context_fields = resolve_audit_output(
            args.json_out,
            tool_name="audit_ws_market_health",
            execution_context=args.execution_context,
            artifact_root=args.artifact_root,
        )
        payload["artifact_context"] = context_fields
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code_for_status(str(payload.get("status") or ""))


if __name__ == "__main__":
    raise SystemExit(main())
