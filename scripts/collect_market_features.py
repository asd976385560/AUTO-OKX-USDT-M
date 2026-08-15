# -*- coding: utf-8 -*-
"""采集盘口、逐笔成交与全宇宙多空账户比影子特征。

覆盖范围：
  BTC/ETH/SOL + 按正确合约面值计算且同时满足成交额/OI门槛的标的
  + focus.md关注币 + 最新快照按美元成交额动态补足。
生产默认100个币，当前可覆盖全部可交易性合格池；不是给全市场逐币抓深度。
多空账户比单独走官方 Trading Statistics REST，在 :00/:30 覆盖最新全部 USDT
线性 SWAP，不受盘口/逐笔动态集合上限约束；上游观测粒度仍为1H。

本脚本只生成影子特征，不改变分析评分和交易风控。
历史默认保留30天；每日04:45槽由本脚本自己的写连接裁剪，避免50档原始JSON无界增长。
多空账户比每小时 :00/:30 槽采全宇宙；覆盖低于99%或任一真实上游来源
在采集完成时超过90分钟均显式降级，但不阻断盘口/逐笔主路径。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _okx_http import (
    fetch_candles_batch_sync,
    fetch_contract_long_short_ratios_batch_sync,
    fetch_contract_open_interest_history_batch_sync,
    fetch_contract_taker_volumes_batch_sync,
    fetch_open_interest_all_sync,
    fetch_orderbooks_batch_sync,
    fetch_recent_trades_batch_sync,
)

CST = timezone(timedelta(hours=8))
ROOT = Path(r".")
BASE_SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
DEPTH_BPS = (10, 25, 50)
SLIPPAGE_USD = (100, 500, 1000)
DEFAULT_RETENTION_DAYS = 30
DEFAULT_POSITIONING_SYMBOLS = 500
DEFAULT_POSITIONING_WORKERS = 6
DEFAULT_CONTRACT_STATS_SYMBOLS = 500
DEFAULT_MAX_SYMBOLS = 100
POSITIONING_BATCH_TIMEOUT_S = 30.0
POSITIONING_MAXIMUM_SOURCE_AGE_S = 5_400.0
POSITIONING_PRIMARY_KEY = ("cycle_id", "symbol", "timeframe", "source")
CONTRACT_STATS_BATCH_TIMEOUT_S = 35.0
CONTRACT_STATS_RETRY_TIMEOUT_S = 12.0
CONTRACT_STATS_FALLBACK_TIMEOUT_S = 7.0
CONTRACT_STATS_FALLBACK_OI_MAX_AGE_S = 120.0
CONTRACT_STATS_PRIMARY_MAX_AGE_S = 5_400.0
CONTRACT_STATS_CARRY_PREFLIGHT_MARGIN_S = 90.0
CONTRACT_STATS_SYSTEMIC_FAILURE_MIN_SYMBOLS = 100
CONTRACT_STATS_SYSTEMIC_FAILURE_RATIO = 0.5
CONTRACT_STATS_SOURCE = "okx_rest_contract_oi_taker_15m"
CONTRACT_STATS_CARRY_METHOD = "official_previous_batch_carry_forward"
CONTRACT_STATS_DIRECT_METHODS = frozenset({
    "rubik_common_bucket",
    "official_public_oi_trades_candle_reconciled_fallback",
})
CONTRACT_STATS_MINIMUM_COVERAGE = 0.99
MARKET_FEATURE_MINIMUM_COVERAGE = 0.99
POSITIONING_MINIMUM_COVERAGE = 0.99
MIN_QUOTE_VOL_USD = 5_000_000.0
MIN_OI_USD = 5_000_000.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cycle_id_now() -> str:
    now = datetime.now(CST)
    minute = (now.minute // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def ms_to_iso(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def focus_symbols(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    part = text.split("## 关注币种", 1)
    if len(part) < 2:
        return []
    body = part[1].split("\n## ", 1)[0]
    out = []
    for line in body.splitlines():
        if not line.strip().startswith(("-", "*")):
            continue
        for token in re.findall(r"\b[A-Z][A-Z0-9]{1,14}\b", line.upper()):
            sym = token if token.endswith("-USDT-SWAP") else f"{token}-USDT-SWAP"
            if sym not in out:
                out.append(sym)
    return out


def select_symbols(
    con: sqlite3.Connection,
    max_symbols: int,
    focus_path: Path,
) -> list[str]:
    latest = con.execute("SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
    latest_derivatives = con.execute("SELECT MAX(ts) FROM derivatives").fetchone()[0]
    rows = con.execute(
        "SELECT t.symbol,t.last,t.vol24h,COALESCE(i.ctVal,0) ctVal,d.oi_usd "
        "FROM tick_snapshots t LEFT JOIN instruments_cache i ON i.instId=t.symbol "
        "LEFT JOIN derivatives d ON d.symbol=t.symbol AND d.ts=? "
        "WHERE t.ts=? AND t.last IS NOT NULL AND t.vol24h IS NOT NULL",
        (latest_derivatives, latest),
    ).fetchall()
    ranked = sorted(
        rows,
        key=lambda r: (r[1] or 0) * (r[2] or 0) * (r[3] or 0),
        reverse=True,
    )
    eligible = [
        r for r in ranked
        if (r[1] or 0) * (r[2] or 0) * (r[3] or 0) >= MIN_QUOTE_VOL_USD
        and (r[4] or 0) >= MIN_OI_USD
    ]
    live = {r[0] for r in rows}
    selected = []
    for symbol in (
        *BASE_SYMBOLS,
        *(r[0] for r in eligible),
        *focus_symbols(focus_path),
        *(r[0] for r in ranked),
    ):
        if symbol in live and symbol not in selected:
            selected.append(symbol)
        if len(selected) >= max_symbols:
            break
    return selected


def _feature_selection_payload(symbols: list[str]) -> list[dict]:
    return [
        {"selection_rank": rank, "symbol": str(symbol).strip().upper()}
        for rank, symbol in enumerate(symbols, start=1)
    ]


def _feature_selection_hash(rows: list[dict]) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def freeze_market_feature_selection(
    con: sqlite3.Connection,
    symbols: list[str],
    *,
    cycle_id: str,
    collected_ts_utc: str,
    max_symbols: int,
) -> dict:
    """Freeze the exact dynamic enrichment denominator once per slot."""
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:(00|15|30|45)", cycle_id
    ):
        raise ValueError("cycle_id must align to a 15-minute boundary")
    rows = _feature_selection_payload(symbols)
    normalized = [row["symbol"] for row in rows]
    if not rows or any(not symbol for symbol in normalized):
        raise ValueError("market feature selection is empty or invalid")
    if len(normalized) != len(set(normalized)):
        raise ValueError("market feature selection contains duplicate symbols")
    if max_symbols <= 0 or len(rows) != max_symbols:
        raise ValueError(
            "market feature selection must exactly match max_symbols: "
            f"selected={len(rows)} max_symbols={max_symbols}"
        )
    payload_hash = _feature_selection_hash(rows)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS market_feature_selection_runs(
            cycle_id         TEXT PRIMARY KEY,
            collected_ts_utc TEXT NOT NULL,
            selected_count   INTEGER NOT NULL,
            max_symbols      INTEGER NOT NULL,
            payload_sha256   TEXT NOT NULL,
            complete         INTEGER NOT NULL CHECK(complete IN (0,1)),
            source           TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS market_feature_selection_rows(
            cycle_id      TEXT NOT NULL,
            selection_rank INTEGER NOT NULL,
            symbol        TEXT NOT NULL,
            PRIMARY KEY(cycle_id,symbol),
            UNIQUE(cycle_id,selection_rank),
            FOREIGN KEY(cycle_id)
              REFERENCES market_feature_selection_runs(cycle_id)
        );
        CREATE INDEX IF NOT EXISTS idx_market_feature_selection_symbol
          ON market_feature_selection_rows(symbol,cycle_id);
    """)
    existing = con.execute(
        "SELECT selected_count,max_symbols,payload_sha256,complete "
        "FROM market_feature_selection_runs WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if existing is not None:
        stored_rows = [
            {"selection_rank": int(row[0]), "symbol": str(row[1])}
            for row in con.execute(
                "SELECT selection_rank,symbol "
                "FROM market_feature_selection_rows WHERE cycle_id=? "
                "ORDER BY selection_rank",
                (cycle_id,),
            ).fetchall()
        ]
        stored_observed_hash = _feature_selection_hash(stored_rows)
        identical = (
            int(existing[3]) == 1
            and int(existing[0]) == len(rows)
            and int(existing[1]) == max_symbols
            and str(existing[2]) == stored_observed_hash
            and str(existing[2]) == payload_hash
        )
        return {
            "status": "reused" if identical else "conflict",
            "cycle_id": cycle_id,
            "selected_count": len(rows),
            "max_symbols": max_symbols,
            "payload_sha256": payload_hash,
            "stored_selected_count": int(existing[0]),
            "stored_max_symbols": int(existing[1]),
            "stored_payload_sha256": str(existing[2]),
            "stored_observed_payload_sha256": stored_observed_hash,
            "complete": identical,
        }
    try:
        con.execute("SAVEPOINT freeze_market_feature_selection")
        con.execute(
            "INSERT INTO market_feature_selection_runs VALUES(?,?,?,?,?,?,?)",
            (
                cycle_id, collected_ts_utc, len(rows), max_symbols,
                payload_hash, 1, "dynamic_liquidity_oi_focus_rank_v1",
            ),
        )
        con.executemany(
            "INSERT INTO market_feature_selection_rows VALUES(?,?,?)",
            [
                (cycle_id, row["selection_rank"], row["symbol"])
                for row in rows
            ],
        )
        con.execute("RELEASE SAVEPOINT freeze_market_feature_selection")
        con.commit()
    except Exception:
        con.execute("ROLLBACK TO SAVEPOINT freeze_market_feature_selection")
        con.execute("RELEASE SAVEPOINT freeze_market_feature_selection")
        raise
    return {
        "status": "inserted",
        "cycle_id": cycle_id,
        "selected_count": len(rows),
        "max_symbols": max_symbols,
        "payload_sha256": payload_hash,
        "complete": True,
    }


def market_feature_batch_passed(
    *,
    selected_count: int,
    microstructure_rows: int,
    trade_flow_rows: int,
    minimum_coverage: float = MARKET_FEATURE_MINIMUM_COVERAGE,
) -> bool:
    if selected_count <= 0 or not 0 < minimum_coverage <= 1:
        return False
    return (
        microstructure_rows / selected_count >= minimum_coverage
        and trade_flow_rows / selected_count >= minimum_coverage
    )


def positioning_batch_passed(
    *,
    selected_count: int,
    positioning_rows: int,
    minimum_coverage: float = POSITIONING_MINIMUM_COVERAGE,
) -> bool:
    """Require one explicit whole-universe quality gate for positioning.

    A partial official REST batch is still written for observability, but it is
    never reported as a successful enrichment step merely because at least one
    symbol returned.
    """
    if selected_count <= 0 or not 0 < minimum_coverage <= 1:
        return False
    return positioning_rows / selected_count >= minimum_coverage


def positioning_source_freshness(
    rows: list[tuple],
    maximum_age_seconds: float = POSITIONING_MAXIMUM_SOURCE_AGE_S,
) -> dict[str, object]:
    """Validate real upstream timestamps at batch availability time.

    ``collected_ts`` only proves that a request completed.  It must never make
    an old upstream 1H observation look fresh.
    """
    if maximum_age_seconds <= 0:
        raise ValueError("maximum_age_seconds must be positive")
    ages: list[float] = []
    invalid_symbols: list[str] = []
    for row in rows:
        symbol = str(row[3]) if len(row) > 3 else "<unknown>"
        try:
            source_at = datetime.fromisoformat(
                str(row[0]).replace("Z", "+00:00"))
            collected_at = datetime.fromisoformat(
                str(row[1]).replace("Z", "+00:00"))
            if source_at.tzinfo is None:
                source_at = source_at.replace(tzinfo=timezone.utc)
            if collected_at.tzinfo is None:
                collected_at = collected_at.replace(tzinfo=timezone.utc)
            age_seconds = (
                collected_at.astimezone(timezone.utc)
                - source_at.astimezone(timezone.utc)
            ).total_seconds()
            ages.append(age_seconds)
            if age_seconds < -60.0 or age_seconds > maximum_age_seconds:
                invalid_symbols.append(symbol)
        except (TypeError, ValueError, IndexError):
            invalid_symbols.append(symbol)
    maximum_age = max(ages) if ages else None
    return {
        "passed": bool(rows) and not invalid_symbols,
        "maximum_source_age_minutes": (
            maximum_age / 60.0 if maximum_age is not None else None),
        "invalid_symbol_count": len(invalid_symbols),
        "invalid_symbol_examples": invalid_symbols[:20],
    }


def select_positioning_symbols(
    con: sqlite3.Connection,
    max_symbols: int,
) -> list[str]:
    """返回最新快照中的全部 USDT 线性 SWAP，按名义成交额排序。

    market.db 的活跃交易宇宙本身就是 USDT-M 线性 SWAP；后缀过滤避免未来
    其它产品误入。与 ``select_symbols`` 不同，这里不应用成交额/OI门槛。
    """
    latest = con.execute("SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
    if not latest:
        return []
    rows = con.execute(
        "SELECT t.symbol,t.last,t.vol24h,COALESCE(i.ctVal,0) ctVal "
        "FROM tick_snapshots t "
        "LEFT JOIN instruments_cache i ON i.instId=t.symbol "
        "WHERE t.ts=? AND t.symbol LIKE '%-USDT-SWAP'",
        (latest,),
    ).fetchall()
    ranked = sorted(
        rows,
        key=lambda row: (
            (row[1] or 0) * (row[2] or 0) * (row[3] or 0),
            row[0],
        ),
        reverse=True,
    )
    live = {row[0] for row in rows}
    output: list[str] = []
    for symbol in (*BASE_SYMBOLS, *(row[0] for row in ranked)):
        if symbol in live and symbol not in output:
            output.append(symbol)
        if len(output) >= max_symbols:
            break
    return output


def contract_values_for_symbols(
    con: sqlite3.Connection,
    symbols: list[str],
) -> dict[str, float]:
    """Load positive contract values for exact USD trade-notional conversion."""
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    return {
        str(row[0]): float(row[1])
        for row in con.execute(
            "SELECT instId,ctVal FROM instruments_cache "
            f"WHERE instId IN ({placeholders})",
            symbols,
        ).fetchall()
        if row[1] is not None and float(row[1]) > 0
    }


def contract_statistics_batch_passed(
    *,
    availability_coverage_rate: float,
    direct_coverage_rate: float,
    written_rows: int,
    completed_rows: int,
    minimum_coverage: float = CONTRACT_STATS_MINIMUM_COVERAGE,
) -> bool:
    """The 99% gate requires fresh direct data, not merely carried values."""
    return (
        availability_coverage_rate >= minimum_coverage
        and direct_coverage_rate >= minimum_coverage
        and written_rows == completed_rows
    )


def depth_usd(levels: list, mid: float, ct_val: float, side: str, bp: int) -> float:
    if not mid or not ct_val:
        return 0.0
    if side == "bid":
        bound = mid * (1 - bp / 10000)
        eligible = (lvl for lvl in levels if to_float(lvl[0]) is not None and float(lvl[0]) >= bound)
    else:
        bound = mid * (1 + bp / 10000)
        eligible = (lvl for lvl in levels if to_float(lvl[0]) is not None and float(lvl[0]) <= bound)
    total = 0.0
    for lvl in eligible:
        px, qty = to_float(lvl[0]), to_float(lvl[1])
        if px is not None and qty is not None:
            total += px * qty * ct_val
    return total


def estimate_slippage_bps(levels: list, mid: float, ct_val: float, target_usd: float) -> float | None:
    if not mid or not ct_val or target_usd <= 0:
        return None
    remaining = target_usd
    contracts = 0.0
    paid = 0.0
    for lvl in levels:
        px, qty = to_float(lvl[0]), to_float(lvl[1])
        if not px or not qty:
            continue
        level_usd = px * qty * ct_val
        take_usd = min(remaining, level_usd)
        take_contracts = take_usd / (px * ct_val)
        paid += take_contracts * px
        contracts += take_contracts
        remaining -= take_usd
        if remaining <= 1e-8:
            break
    if remaining > 1e-6 or contracts <= 0:
        return None
    avg_px = paid / contracts
    return abs(avg_px - mid) / mid * 10000


def book_features(book: dict, ct_val: float, cycle_id: str, collected_ts: str, symbol: str) -> tuple:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = to_float(bids[0][0]) if bids else None
    best_ask = to_float(asks[0][0]) if asks else None
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
    spread = (best_ask - best_bid) / mid * 10000 if mid else None
    depths = {}
    imbalances = {}
    for bp in DEPTH_BPS:
        bd = depth_usd(bids, mid, ct_val, "bid", bp) if mid else 0.0
        ad = depth_usd(asks, mid, ct_val, "ask", bp) if mid else 0.0
        depths[bp] = (bd, ad)
        imbalances[bp] = (bd - ad) / (bd + ad) if bd + ad else None
    slip = {}
    for target in SLIPPAGE_USD:
        slip[target] = (
            estimate_slippage_bps(asks, mid, ct_val, target) if mid else None,
            estimate_slippage_bps(bids, mid, ct_val, target) if mid else None,
        )
    return (
        collected_ts, cycle_id, symbol, min(50, len(bids), len(asks)),
        best_bid, best_ask, mid, spread,
        depths[10][0], depths[10][1], depths[25][0], depths[25][1],
        depths[50][0], depths[50][1],
        imbalances[10], imbalances[25], imbalances[50],
        slip[100][0], slip[100][1], slip[500][0], slip[500][1],
        slip[1000][0], slip[1000][1],
        ms_to_iso(book.get("ts")), book.get("seqId"),
        json.dumps(bids[:50], ensure_ascii=False),
        json.dumps(asks[:50], ensure_ascii=False), "okx",
    )


def flow_features(trades: list, ct_val: float, cycle_id: str, collected_ts: str, symbol: str) -> tuple:
    buy_qty = sell_qty = buy_usd = sell_usd = largest = 0.0
    timestamps = []
    for trade in trades:
        qty, px = to_float(trade.get("sz")), to_float(trade.get("px"))
        if qty is None or px is None:
            continue
        notion = qty * px * ct_val
        largest = max(largest, notion)
        if trade.get("side") == "buy":
            buy_qty += qty
            buy_usd += notion
        elif trade.get("side") == "sell":
            sell_qty += qty
            sell_usd += notion
        try:
            timestamps.append(int(trade.get("ts")))
        except (TypeError, ValueError):
            pass
    total = buy_usd + sell_usd
    start_ms = min(timestamps) if timestamps else None
    end_ms = max(timestamps) if timestamps else None
    return (
        collected_ts, cycle_id, symbol, len(trades),
        ms_to_iso(start_ms), ms_to_iso(end_ms),
        (end_ms - start_ms) if start_ms is not None and end_ms is not None else None,
        buy_qty, sell_qty, buy_usd, sell_usd,
        buy_usd / total if total else None, buy_usd - sell_usd, largest,
        json.dumps(trades[:50], ensure_ascii=False), "okx_recent_trades",
    )


def positioning_due(cycle_id: str, mode: str) -> bool:
    """auto 在:00/:30采；always供隔离验证，off明确关闭。"""
    if mode == "off":
        return False
    if mode == "always":
        return True
    return bool(re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:(00|30)", cycle_id))


def contract_statistics_due(cycle_id: str, mode: str) -> bool:
    """auto 对每个标准15分钟周期采；off/always 供灰度与隔离验收。"""
    if mode == "off":
        return False
    if mode == "always":
        return True
    return bool(re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:(00|15|30|45)", cycle_id))


def positioning_row(payload, cycle_id: str, collected_ts: str,
                    requested_symbol: str) -> tuple:
    """解析历史 OKX CLI 回执；仅保留兼容测试/旧工件读取。"""
    roots = payload if isinstance(payload, list) else [payload]
    candidates: list[tuple[int, str, dict, dict]] = []
    for root in roots:
        if not isinstance(root, dict):
            continue
        for block in root.get("data") or []:
            if not isinstance(block, dict):
                continue
            symbol = str(block.get("instId") or requested_symbol)
            records = (
                ((block.get("timeframes") or {}).get("1H") or {})
                .get("indicators") or {}
            ).get(
                "TOPLONGSHORT", []
            )
            for rec in records or []:
                if not isinstance(rec, dict):
                    continue
                try:
                    ts_ms = int(rec.get("ts"))
                except (TypeError, ValueError):
                    continue
                values = rec.get("values") or {}
                candidates.append((ts_ms, symbol, values, rec))
    if not candidates:
        raise ValueError("TOPLONGSHORT 1H 数据为空")
    ts_ms, symbol, values, raw_record = max(candidates, key=lambda item: item[0])
    long_ratio = to_float(values.get("longRatio"))
    short_ratio = to_float(values.get("shortRatio"))
    ratio = to_float(values.get("longShortRatio"))
    if long_ratio is None or short_ratio is None or ratio is None:
        raise ValueError("TOPLONGSHORT 比例字段缺失")
    source_ts = ms_to_iso(ts_ms)
    if not source_ts:
        raise ValueError("TOPLONGSHORT 时间戳非法")
    return (
        source_ts, collected_ts, cycle_id, symbol, "1H",
        long_ratio, short_ratio, ratio,
        json.dumps(raw_record, ensure_ascii=False),
        "okx_cli_top_long_short",
    )


def rest_positioning_row(
    payload,
    cycle_id: str,
    collected_ts: str,
    requested_symbol: str,
) -> tuple:
    """解析官方合约账户多空比 ``[ts, long/short]`` 最新行。"""
    candidates: list[tuple[int, float, list]] = []
    for record in payload or []:
        if not isinstance(record, list) or len(record) < 2:
            continue
        try:
            ts_ms = int(record[0])
            ratio = float(record[1])
        except (TypeError, ValueError):
            continue
        if ts_ms <= 0 or ratio < 0 or not math.isfinite(ratio):
            continue
        candidates.append((ts_ms, ratio, record))
    if not candidates:
        raise ValueError("contract long/short ratio 1H 数据为空")
    ts_ms, ratio, raw_record = max(candidates, key=lambda item: item[0])
    source_ts = ms_to_iso(ts_ms)
    if not source_ts:
        raise ValueError("contract long/short ratio 时间戳非法")
    short_ratio = 1.0 / (1.0 + ratio)
    long_ratio = ratio * short_ratio
    return (
        source_ts, collected_ts, cycle_id, requested_symbol, "1H",
        long_ratio, short_ratio, ratio,
        json.dumps({
            "row": raw_record,
            "derivation": "long=ratio/(1+ratio);short=1/(1+ratio)",
        }, ensure_ascii=False),
        "okx_rest_contract_long_short_ratio",
    )


def fetch_positioning_rows(
    symbols: list[str],
    cycle_id: str,
    collected_ts: str | None = None,
    workers: int | None = None,
) -> tuple[list[tuple], list[str]]:
    """官方 REST 并发采全宇宙1H账户多空比，单币失败隔离。"""
    del workers  # 历史调用兼容；并发度由 _okx_http 的统计端点专用配置控制。
    rows: list[tuple] = []
    errors: list[str] = []
    payloads = fetch_contract_long_short_ratios_batch_sync(
        symbols,
        period="1H",
        limit=1,
        batch_timeout_s=POSITIONING_BATCH_TIMEOUT_S,
    )
    # availability 必须是网络批次完成后，而不是发请求前。
    available_at = collected_ts or utc_now_iso()
    for symbol in symbols:
        try:
            rows.append(rest_positioning_row(
                payloads.get(symbol) or [], cycle_id, available_at, symbol,
            ))
        except Exception as exc:  # noqa: BLE001 - 单币失败隔离
            errors.append(f"{symbol}:positioning:{type(exc).__name__}:{exc}")
    rows.sort(key=lambda row: row[3])
    return rows, errors


def _contract_open_interest_records(
    payload,
) -> dict[int, tuple[float, float, float, list]]:
    candidates: dict[int, tuple[float, float, float, list]] = {}
    for record in payload or []:
        if not isinstance(record, list) or len(record) < 4:
            continue
        try:
            ts_ms = int(record[0])
            oi_contracts = float(record[1])
            oi_ccy = float(record[2])
            oi_usd = float(record[3])
        except (TypeError, ValueError):
            continue
        values = (oi_contracts, oi_ccy, oi_usd)
        if (
            ts_ms <= 0
            or any(value < 0 or not math.isfinite(value) for value in values)
        ):
            continue
        candidates[ts_ms] = (*values, record)
    if not candidates:
        raise ValueError("contract open interest 15m 数据为空")
    return candidates


def _contract_taker_volume_records(
    payload,
) -> dict[int, tuple[float, float, list]]:
    candidates: dict[int, tuple[float, float, list]] = {}
    for record in payload or []:
        if not isinstance(record, list) or len(record) < 3:
            continue
        try:
            ts_ms = int(record[0])
            sell_usd = float(record[1])
            buy_usd = float(record[2])
        except (TypeError, ValueError):
            continue
        if (
            ts_ms <= 0
            or sell_usd < 0 or buy_usd < 0
            or not math.isfinite(sell_usd) or not math.isfinite(buy_usd)
        ):
            continue
        candidates[ts_ms] = (sell_usd, buy_usd, record)
    if not candidates:
        raise ValueError("contract taker volume 15m 数据为空")
    return candidates


def contract_statistics_row(
    open_interest_payload,
    taker_volume_payload,
    cycle_id: str,
    collected_ts: str,
    requested_symbol: str,
) -> tuple:
    """严格合并同一闭合15m桶的官方合约OI与主动买卖量。"""
    open_interest = _contract_open_interest_records(open_interest_payload)
    taker_volume = _contract_taker_volume_records(taker_volume_payload)
    common_timestamps = set(open_interest) & set(taker_volume)
    if not common_timestamps:
        raise ValueError(
            "contract statistics source timestamp mismatch: no common bucket")
    source_ts_ms = max(common_timestamps)
    oi_contracts, oi_ccy, oi_usd, oi_raw = open_interest[source_ts_ms]
    sell_usd, buy_usd, taker_raw = taker_volume[source_ts_ms]
    source_ts = ms_to_iso(source_ts_ms)
    if not source_ts:
        raise ValueError("contract statistics 时间戳非法")
    total = sell_usd + buy_usd
    buy_ratio = buy_usd / total if total > 0 else None
    return (
        source_ts, collected_ts, cycle_id, requested_symbol, "15m",
        oi_contracts, oi_ccy, oi_usd,
        sell_usd, buy_usd, buy_ratio,
        json.dumps({
            "open_interest_row": oi_raw,
            "taker_volume_row": taker_raw,
            "taker_unit": "USD",
            "taker_row_order": "[ts,sell_volume,buy_volume]",
        }, ensure_ascii=False),
        CONTRACT_STATS_SOURCE,
    )


def contract_statistics_bucket_window_ms(cycle_id: str) -> tuple[int, int]:
    """Return the exact UTC millisecond window closed by a CST cycle."""
    cycle = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    if cycle.minute % 15 != 0:
        raise ValueError("contract statistics cycle must be a 15m boundary")
    end_ms = int(cycle.astimezone(timezone.utc).timestamp() * 1000)
    return end_ms - 15 * 60 * 1000, end_ms


def contract_statistics_row_needs_fallback(
    row: tuple,
    collected_ts: str,
) -> bool:
    """Use the same 90-minute source-lag gate as strict coverage audit."""
    try:
        source = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        available = datetime.fromisoformat(
            str(collected_ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if source.tzinfo is None:
        source = source.replace(tzinfo=timezone.utc)
    if available.tzinfo is None:
        available = available.replace(tzinfo=timezone.utc)
    lag = (available - source).total_seconds()
    return lag < 0 or lag > CONTRACT_STATS_PRIMARY_MAX_AGE_S


def contract_statistics_fallback_row(
    current_open_interest: dict,
    recent_trades_payload: list,
    recent_trade_outcome: dict,
    candle_payload: list,
    cycle_id: str,
    collected_ts: str,
    requested_symbol: str,
    contract_value: float,
) -> tuple:
    """Build an official-data fallback only when three sources reconcile.

    The fallback uses current official public OI and exact taker-side trades for
    the most recently closed 15-minute bucket.  The trade contract total must
    equal the confirmed official candle volume, otherwise it fails closed.
    """
    if not recent_trade_outcome.get("ok"):
        raise ValueError("recent trades transport failed")
    if not isinstance(current_open_interest, dict) or not current_open_interest:
        raise ValueError("current open interest data missing")
    if (
        current_open_interest.get("instId")
        and current_open_interest.get("instId") != requested_symbol
    ):
        raise ValueError("current open interest symbol mismatch")
    try:
        ct_val = float(contract_value)
        oi_source_ms = int(current_open_interest["ts"])
        oi_contracts = float(current_open_interest["oi"])
        oi_ccy = float(current_open_interest["oiCcy"])
        oi_usd = float(current_open_interest["oiUsd"])
        available = datetime.fromisoformat(
            str(collected_ts).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("current open interest contract invalid") from exc
    oi_values = (oi_contracts, oi_ccy, oi_usd)
    if (
        ct_val <= 0
        or not math.isfinite(ct_val)
        or any(value < 0 or not math.isfinite(value) for value in oi_values)
    ):
        raise ValueError("current open interest or contract value invalid")
    if available.tzinfo is None:
        available = available.replace(tzinfo=timezone.utc)
    available_ms = int(available.astimezone(timezone.utc).timestamp() * 1000)
    oi_age_s = (available_ms - oi_source_ms) / 1000
    if oi_age_s < -5 or oi_age_s > CONTRACT_STATS_FALLBACK_OI_MAX_AGE_S:
        raise ValueError("current open interest source time invalid")

    bucket_start_ms, bucket_end_ms = contract_statistics_bucket_window_ms(
        cycle_id)
    candle = None
    for item in candle_payload or []:
        if not isinstance(item, list) or len(item) < 9:
            continue
        try:
            if int(item[0]) == bucket_start_ms:
                candle = item
                break
        except (TypeError, ValueError):
            continue
    if candle is None or str(candle[8]) != "1":
        raise ValueError("confirmed fallback candle missing")
    try:
        candle_contract_volume = float(candle[5])
    except (TypeError, ValueError) as exc:
        raise ValueError("fallback candle volume invalid") from exc
    if (
        candle_contract_volume < 0
        or not math.isfinite(candle_contract_volume)
    ):
        raise ValueError("fallback candle volume invalid")

    target_trades: list[dict] = []
    for trade in recent_trades_payload or []:
        if not isinstance(trade, dict):
            continue
        try:
            trade_ms = int(trade.get("ts"))
        except (TypeError, ValueError):
            continue
        if bucket_start_ms <= trade_ms < bucket_end_ms:
            target_trades.append(trade)
    trade_ids: list[str] = []
    buy_usd = sell_usd = trade_contract_volume = 0.0
    for trade in target_trades:
        trade_id = str(trade.get("tradeId") or "")
        side = str(trade.get("side") or "")
        try:
            size = float(trade["sz"])
            price = float(trade["px"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("fallback trade contract invalid") from exc
        if (
            not trade_id
            or side not in {"buy", "sell"}
            or size <= 0
            or price <= 0
            or not math.isfinite(size)
            or not math.isfinite(price)
        ):
            raise ValueError("fallback trade contract invalid")
        trade_ids.append(trade_id)
        trade_contract_volume += size
        notional = size * price * ct_val
        if side == "buy":
            buy_usd += notional
        else:
            sell_usd += notional
    if len(trade_ids) != len(set(trade_ids)):
        raise ValueError("duplicate fallback trades")
    if not math.isclose(
        trade_contract_volume,
        candle_contract_volume,
        rel_tol=1e-9,
        abs_tol=1e-10,
    ):
        raise ValueError("fallback trades do not reconcile to candle volume")
    total = buy_usd + sell_usd
    buy_ratio = buy_usd / total if total > 0 else None
    source_ts = ms_to_iso(bucket_start_ms)
    if not source_ts:
        raise ValueError("fallback bucket time invalid")
    return (
        source_ts, collected_ts, cycle_id, requested_symbol, "15m",
        oi_contracts, oi_ccy, oi_usd,
        sell_usd, buy_usd, buy_ratio,
        json.dumps({
            "method": "official_public_oi_trades_candle_reconciled_fallback",
            "open_interest_row": current_open_interest,
            "open_interest_source_ts": ms_to_iso(oi_source_ms),
            "closed_candle_row": candle,
            "reconciled_trade_rows": target_trades,
            "trade_row_count": len(target_trades),
            "trade_contract_volume": trade_contract_volume,
            "candle_contract_volume": candle_contract_volume,
            "trade_volume_reconciled": True,
            "taker_unit": "USD",
            "taker_side_semantics": "official market trade aggressor side",
        }, ensure_ascii=False),
        CONTRACT_STATS_SOURCE,
    )


def contract_statistics_row_method(row: tuple | sqlite3.Row) -> str:
    """Return an explicit method while preserving legacy direct-row semantics."""
    try:
        payload = json.loads(str(row[11]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "invalid_raw_json"
    if not isinstance(payload, dict):
        return "invalid_raw_json"
    return str(payload.get("method") or "rubik_common_bucket")


def contract_statistics_row_issues(
    row: tuple | sqlite3.Row,
    *,
    available_at: str,
    expected_symbol: str | None = None,
    maximum_source_lag_seconds: float = CONTRACT_STATS_PRIMARY_MAX_AGE_S,
) -> list[str]:
    """Validate a row for availability use without hiding its real source age."""
    issues: list[str] = []
    try:
        source_time = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        visible_time = datetime.fromisoformat(
            str(available_at).replace("Z", "+00:00"))
        prior_visible_time = datetime.fromisoformat(
            str(row[1]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ["invalid_time"]
    if source_time.tzinfo is None:
        source_time = source_time.replace(tzinfo=timezone.utc)
    if visible_time.tzinfo is None:
        visible_time = visible_time.replace(tzinfo=timezone.utc)
    if prior_visible_time.tzinfo is None:
        prior_visible_time = prior_visible_time.replace(tzinfo=timezone.utc)
    source_time = source_time.astimezone(timezone.utc)
    visible_time = visible_time.astimezone(timezone.utc)
    prior_visible_time = prior_visible_time.astimezone(timezone.utc)
    source_age = (visible_time - source_time).total_seconds()
    if source_age < 0:
        issues.append("future_source_time")
    elif source_age > maximum_source_lag_seconds:
        issues.append("stale_source_time")
    if prior_visible_time > visible_time:
        issues.append("future_prior_availability")
    if str(row[4]) != "15m":
        issues.append("invalid_timeframe")
    if str(row[12]) != CONTRACT_STATS_SOURCE:
        issues.append("invalid_source")
    if expected_symbol is not None and str(row[3]) != expected_symbol:
        issues.append("symbol_mismatch")
    try:
        numeric = [float(row[index]) for index in (5, 6, 7, 8, 9)]
    except (TypeError, ValueError):
        numeric = []
        issues.append("invalid_numeric")
    if numeric and any(value < 0 or not math.isfinite(value) for value in numeric):
        issues.append("invalid_numeric")
    if numeric:
        total = numeric[3] + numeric[4]
        ratio = to_float(row[10])
        if total > 0:
            expected_ratio = numeric[4] / total
            if (
                ratio is None
                or not math.isfinite(ratio)
                or abs(ratio - expected_ratio) > 1e-12
            ):
                issues.append("taker_ratio_algebra")
        elif row[10] is not None:
            issues.append("zero_volume_ratio")
    if contract_statistics_row_method(row) == "invalid_raw_json":
        issues.append("invalid_raw_json")
    return issues


def latest_valid_direct_contract_statistics_rows(
    con: sqlite3.Connection,
    symbols: list[str],
    cycle_id: str,
    *,
    available_at: str,
) -> tuple[dict[str, sqlite3.Row], dict[str, list[str]]]:
    """Return fresh one-hop official origins and newest invalid diagnostics."""
    expected = list(dict.fromkeys(str(symbol) for symbol in symbols if symbol))
    if not expected:
        return {}, {}
    placeholders = ",".join("?" for _ in expected)
    prior_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        candidates = con.execute(
            "SELECT ts,collected_ts,cycle_id,symbol,timeframe,"
            "oi_contracts,oi_ccy,oi_usd,taker_sell_usd,taker_buy_usd,"
            "taker_buy_ratio,raw,source "
            "FROM market_contract_statistics "
            "WHERE source=? AND timeframe='15m' AND cycle_id<>? "
            f"AND symbol IN ({placeholders}) "
            "ORDER BY symbol,collected_ts DESC,cycle_id DESC",
            (CONTRACT_STATS_SOURCE, cycle_id, *expected),
        ).fetchall()
    finally:
        con.row_factory = prior_factory
    valid: dict[str, sqlite3.Row] = {}
    invalid: dict[str, list[str]] = {}
    for row in candidates:
        symbol = str(row["symbol"])
        if symbol in valid:
            continue
        if contract_statistics_row_method(row) not in CONTRACT_STATS_DIRECT_METHODS:
            continue
        issues = contract_statistics_row_issues(
            row, available_at=available_at, expected_symbol=symbol,
        )
        if issues:
            invalid.setdefault(symbol, issues)
            continue
        valid[symbol] = row
    return valid, invalid


def contract_statistics_carry_preflight_time() -> str:
    """Conservatively require a prior origin to survive the whole fetch budget."""
    return (
        datetime.now(timezone.utc)
        + timedelta(seconds=CONTRACT_STATS_CARRY_PREFLIGHT_MARGIN_S)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def complete_contract_statistics_with_previous_batch(
    con: sqlite3.Connection,
    rows: list[tuple],
    symbols: list[str],
    cycle_id: str,
    *,
    available_at: str | None = None,
) -> tuple[list[tuple], dict[str, object], list[str]]:
    """Complete a batch with bounded, transparent prior official observations.

    Direct or reconciled current rows always win.  A missing/invalid symbol may
    reuse only its latest row from the same official source while the original
    ``ts`` remains no more than 90 minutes old.  The carry row keeps that real
    source timestamp and is explicitly excluded from model features elsewhere.
    """
    batch_available_at = available_at or utc_now_iso()
    expected = list(dict.fromkeys(str(symbol) for symbol in symbols if symbol))
    expected_set = set(expected)
    valid_current: dict[str, tuple] = {}
    invalid_current: dict[str, list[str]] = {}
    for row in rows:
        symbol = str(row[3])
        if symbol not in expected_set:
            continue
        issues = contract_statistics_row_issues(
            row, available_at=batch_available_at, expected_symbol=symbol,
        )
        if contract_statistics_row_method(row) == CONTRACT_STATS_CARRY_METHOD:
            issues.append("current_batch_carry_input_disallowed")
        if issues:
            invalid_current[symbol] = issues
            continue
        normalized = list(row)
        normalized[1] = batch_available_at
        valid_current[symbol] = tuple(normalized)

    missing = [symbol for symbol in expected if symbol not in valid_current]
    previous: dict[str, sqlite3.Row] = {}
    previous_invalid: dict[str, list[str]] = {}
    if missing:
        previous, previous_invalid = latest_valid_direct_contract_statistics_rows(
            con, missing, cycle_id, available_at=batch_available_at)

    carry_rows: dict[str, tuple] = {}
    carry_errors: list[str] = []
    for symbol in missing:
        row = previous.get(symbol)
        if row is None:
            issues = previous_invalid.get(symbol)
            if issues:
                carry_errors.append(
                    f"{symbol}:carry_forward:previous_row_invalid:"
                    f"{','.join(issues)}")
            else:
                carry_errors.append(
                    f"{symbol}:carry_forward:previous_row_missing")
            continue
        prior_raw = json.loads(str(row["raw"]))
        prior_method = contract_statistics_row_method(row)
        source_time = datetime.fromisoformat(
            str(row["ts"]).replace("Z", "+00:00"))
        visible_time = datetime.fromisoformat(
            batch_available_at.replace("Z", "+00:00"))
        if source_time.tzinfo is None:
            source_time = source_time.replace(tzinfo=timezone.utc)
        if visible_time.tzinfo is None:
            visible_time = visible_time.replace(tzinfo=timezone.utc)
        age_seconds = (
            visible_time.astimezone(timezone.utc)
            - source_time.astimezone(timezone.utc)
        ).total_seconds()
        value_contract = {
            "ts": str(row["ts"]),
            "symbol": symbol,
            "oi_contracts": float(row["oi_contracts"]),
            "oi_ccy": float(row["oi_ccy"]),
            "oi_usd": float(row["oi_usd"]),
            "taker_sell_usd": float(row["taker_sell_usd"]),
            "taker_buy_usd": float(row["taker_buy_usd"]),
            "taker_buy_ratio": (
                float(row["taker_buy_ratio"])
                if row["taker_buy_ratio"] is not None else None
            ),
        }
        raw = {
            "method": CONTRACT_STATS_CARRY_METHOD,
            "semantics": (
                "availability continuity only; not a new 15m observation; "
                "excluded from model features"
            ),
            "carried_from_cycle_id": str(row["cycle_id"]),
            "carried_from_collected_ts": str(row["collected_ts"]),
            "origin_cycle_id": str(row["cycle_id"]),
            "origin_collected_ts": str(row["collected_ts"]),
            "origin_method": prior_method,
            "carry_count": 1,
            "source_age_seconds": age_seconds,
            "value_contract_sha256": hashlib.sha256(
                json.dumps(
                    value_contract, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "prior_raw_sha256": hashlib.sha256(
                str(row["raw"]).encode("utf-8")
            ).hexdigest(),
        }
        carry_rows[symbol] = (
            str(row["ts"]), batch_available_at, cycle_id, symbol, "15m",
            float(row["oi_contracts"]), float(row["oi_ccy"]),
            float(row["oi_usd"]), float(row["taker_sell_usd"]),
            float(row["taker_buy_usd"]),
            (float(row["taker_buy_ratio"])
             if row["taker_buy_ratio"] is not None else None),
            json.dumps(raw, ensure_ascii=False), CONTRACT_STATS_SOURCE,
        )

    completed = [
        valid_current.get(symbol) or carry_rows.get(symbol)
        for symbol in expected
    ]
    completed = [row for row in completed if row is not None]
    direct_count = len(valid_current)
    carry_count = len(carry_rows)
    quality: dict[str, object] = {
        "selected_symbols": len(expected),
        "direct_valid_symbols": direct_count,
        "carried_forward_symbols": carry_count,
        "available_symbols": len(completed),
        "direct_coverage_rate": direct_count / len(expected) if expected else 0.0,
        "carry_forward_rate": carry_count / len(expected) if expected else 0.0,
        "availability_coverage_rate": (
            len(completed) / len(expected) if expected else 0.0),
        "invalid_current_symbols": invalid_current,
        "carry_forward_max_source_age_seconds": (
            CONTRACT_STATS_PRIMARY_MAX_AGE_S),
        "carry_forward_excluded_from_model_features": True,
        "batch_available_at": batch_available_at,
    }
    return completed, quality, carry_errors


def fetch_contract_statistics_rows(
    symbols: list[str],
    cycle_id: str,
    collected_ts: str | None = None,
    contract_values: dict[str, float] | None = None,
    carryable_symbols: set[str] | None = None,
) -> tuple[list[tuple], list[str]]:
    """Fetch official 15m statistics with a reconciled public-data fallback."""

    def fetch_payloads(
        selected: list[str], timeout_s: float, *, request_retries: int,
    ) -> tuple[dict, dict, list[str]]:
        batch_errors: list[str] = []
        oi_outcomes: dict[str, dict] = {}
        taker_outcomes: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            oi_future = executor.submit(
                fetch_contract_open_interest_history_batch_sync,
                selected,
                "15m",
                8,
                timeout_s,
                request_retries=request_retries,
                outcomes=oi_outcomes,
            )
            taker_future = executor.submit(
                fetch_contract_taker_volumes_batch_sync,
                selected,
                "15m",
                "2",
                8,
                timeout_s,
                request_retries=request_retries,
                outcomes=taker_outcomes,
            )
            try:
                open_interest_result = oi_future.result()
            except Exception as exc:  # noqa: BLE001 - 来源整体失败外显
                open_interest_result = {}
                batch_errors.append(
                    f"contract_open_interest:{type(exc).__name__}:{exc}")
            try:
                taker_volume_result = taker_future.result()
            except Exception as exc:  # noqa: BLE001 - 来源整体失败外显
                taker_volume_result = {}
                batch_errors.append(
                    f"contract_taker_volume:{type(exc).__name__}:{exc}")
        for label, outcomes in (
            ("contract_open_interest_transport", oi_outcomes),
            ("contract_taker_volume_transport", taker_outcomes),
        ):
            failures = [
                (symbol, outcome) for symbol, outcome in outcomes.items()
                if not bool(outcome.get("ok"))
            ]
            if failures:
                kinds: dict[str, int] = {}
                for _symbol, outcome in failures:
                    kind = str(outcome.get("error_type") or "unknown")
                    kinds[kind] = kinds.get(kind, 0) + 1
                sample_symbol, sample = failures[0]
                batch_errors.append(
                    f"{label}:failed={len(failures)}/{len(selected)}:"
                    f"kinds={json.dumps(kinds, sort_keys=True)}:"
                    f"sample={sample_symbol}:{str(sample.get('error') or '')[:180]}"
                )
        return open_interest_result, taker_volume_result, batch_errors

    open_interest, taker_volume, errors = fetch_payloads(
        symbols, CONTRACT_STATS_BATCH_TIMEOUT_S, request_retries=0)
    provisional_time = collected_ts or utc_now_iso()
    retry_symbols: list[str] = []
    for symbol in symbols:
        try:
            contract_statistics_row(
                open_interest.get(symbol) or [],
                taker_volume.get(symbol) or [],
                cycle_id,
                provisional_time,
                symbol,
            )
        except Exception:  # noqa: BLE001 - 精确失败集进入一次重试
            retry_symbols.append(symbol)
    # 系统性传输失败也对全部失败币保留一次12秒有界直采重试。上一批可续用
    # 只能保障运行连续性，不能为了省请求牺牲模型可用直采率。严格三源fallback
    # 仍在下方只优先不可续用币，避免故障期扩大额外请求扇出。
    retry_fetch_symbols = retry_symbols
    if retry_fetch_symbols:
        retry_oi, retry_taker, retry_errors = fetch_payloads(
            retry_fetch_symbols, CONTRACT_STATS_RETRY_TIMEOUT_S,
            request_retries=1)
        errors.extend(retry_errors)
        for symbol in retry_fetch_symbols:
            open_interest[symbol] = [
                *(retry_oi.get(symbol) or []),
                *(open_interest.get(symbol) or []),
            ]
            taker_volume[symbol] = [
                *(retry_taker.get(symbol) or []),
                *(taker_volume.get(symbol) or []),
            ]
    provisional_rows: dict[str, tuple] = {}
    for symbol in symbols:
        try:
            provisional_rows[symbol] = contract_statistics_row(
                open_interest.get(symbol) or [],
                taker_volume.get(symbol) or [],
                cycle_id,
                provisional_time,
                symbol,
            )
        except Exception:  # noqa: BLE001 - 单币失败进入严格回退
            pass

    fallback_symbols = [
        symbol for symbol in symbols
        if symbol not in provisional_rows
        or contract_statistics_row_needs_fallback(
            provisional_rows[symbol], provisional_time)
    ]
    systemic_primary_failure = (
        len(symbols) >= CONTRACT_STATS_SYSTEMIC_FAILURE_MIN_SYMBOLS
        and len(fallback_symbols) / len(symbols)
        >= CONTRACT_STATS_SYSTEMIC_FAILURE_RATIO
    )
    fallback_fetch_symbols = fallback_symbols
    if systemic_primary_failure and carryable_symbols is not None:
        fallback_fetch_symbols = [
            symbol for symbol in fallback_symbols
            if symbol not in carryable_symbols
        ]
        errors.append(
            "contract_statistics_systemic_primary_failure:"
            f"failed={len(fallback_symbols)}/{len(symbols)}:"
            f"strict_fallback_prioritized={len(fallback_fetch_symbols)}:"
            f"fresh_direct_origins={len(carryable_symbols)}"
        )
    fallback_rows: dict[str, tuple] = {}
    fallback_failures: dict[str, Exception] = {}
    available_at = collected_ts or utc_now_iso()
    if fallback_fetch_symbols and contract_values:
        trade_outcomes: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            current_oi_future = executor.submit(
                fetch_open_interest_all_sync,
                "SWAP",
                CONTRACT_STATS_FALLBACK_TIMEOUT_S,
            )
            recent_trades_future = executor.submit(
                fetch_recent_trades_batch_sync,
                fallback_fetch_symbols,
                500,
                CONTRACT_STATS_FALLBACK_TIMEOUT_S,
                outcomes=trade_outcomes,
            )
            candles_future = executor.submit(
                fetch_candles_batch_sync,
                fallback_fetch_symbols,
                "15m",
                4,
                CONTRACT_STATS_FALLBACK_TIMEOUT_S,
            )
            try:
                current_oi = current_oi_future.result()
            except Exception as exc:  # noqa: BLE001 - 来源整体失败外显
                current_oi = {}
                errors.append(
                    f"contract_fallback_open_interest:"
                    f"{type(exc).__name__}:{exc}")
            try:
                recent_trades = recent_trades_future.result()
            except Exception as exc:  # noqa: BLE001 - 来源整体失败外显
                recent_trades = {}
                errors.append(
                    f"contract_fallback_trades:{type(exc).__name__}:{exc}")
            try:
                fallback_candles = candles_future.result()
            except Exception as exc:  # noqa: BLE001 - 来源整体失败外显
                fallback_candles = {}
                errors.append(
                    f"contract_fallback_candles:{type(exc).__name__}:{exc}")
        available_at = collected_ts or utc_now_iso()
        for symbol in fallback_fetch_symbols:
            try:
                fallback_rows[symbol] = contract_statistics_fallback_row(
                    current_oi.get(symbol) or {},
                    recent_trades.get(symbol) or [],
                    trade_outcomes.get(symbol) or {},
                    fallback_candles.get(symbol) or [],
                    cycle_id,
                    available_at,
                    symbol,
                    (contract_values or {}).get(symbol),
                )
            except Exception as exc:  # noqa: BLE001 - 单币失败关闭
                fallback_failures[symbol] = exc

    rows: list[tuple] = []
    for symbol in symbols:
        if symbol in fallback_rows:
            rows.append(fallback_rows[symbol])
            continue
        try:
            primary = contract_statistics_row(
                open_interest.get(symbol) or [],
                taker_volume.get(symbol) or [],
                cycle_id,
                available_at,
                symbol,
            )
            rows.append(primary)
            if (
                contract_statistics_row_needs_fallback(primary, available_at)
                and contract_values
            ):
                fallback_error = fallback_failures.get(symbol)
                errors.append(
                    f"{symbol}:contract_statistics_fallback:"
                    f"{type(fallback_error).__name__ if fallback_error else 'Unavailable'}:"
                    f"{fallback_error or 'latest closed bucket not recovered'}"
                )
        except Exception as exc:  # noqa: BLE001 - 单币失败隔离
            fallback_error = fallback_failures.get(symbol)
            detail = (
                f";fallback={type(fallback_error).__name__}:{fallback_error}"
                if fallback_error else ""
            )
            errors.append(
                f"{symbol}:contract_statistics:{type(exc).__name__}:{exc}"
                f"{detail}")
    rows.sort(key=lambda row: row[3])
    return rows, errors


def write_positioning_rows(con: sqlite3.Connection, rows: list[tuple]) -> int:
    if not rows:
        return 0
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='market_positioning'"
    ).fetchone()
    if not exists:
        raise RuntimeError("market_positioning table missing")
    table_info = con.execute("PRAGMA table_info(market_positioning)").fetchall()
    actual_primary_key = tuple(
        str(row[1])
        for row in sorted(
            (row for row in table_info if int(row[5]) > 0),
            key=lambda row: int(row[5]),
        )
    )
    if actual_primary_key != POSITIONING_PRIMARY_KEY:
        raise RuntimeError(
            "market_positioning unsafe primary key "
            f"{actual_primary_key}; expected {POSITIONING_PRIMARY_KEY}; "
            "run apply_positioning_batch_key_schema.py"
        )
    con.executemany(
        "INSERT INTO market_positioning "
        "(ts,collected_ts,cycle_id,symbol,timeframe,long_ratio,short_ratio,"
        "long_short_ratio,raw,source) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(cycle_id,symbol,timeframe,source) DO NOTHING",
        rows,
    )
    for expected in rows:
        stored = con.execute(
            "SELECT ts,collected_ts,cycle_id,symbol,timeframe,long_ratio,"
            "short_ratio,long_short_ratio,raw,source "
            "FROM market_positioning WHERE cycle_id=? AND symbol=? "
            "AND timeframe=? AND source=?",
            (expected[2], expected[3], expected[4], expected[9]),
        ).fetchone()
        if stored is None or tuple(stored) != tuple(expected):
            raise RuntimeError(
                "market_positioning immutable batch conflict: "
                f"cycle={expected[2]} symbol={expected[3]} "
                f"timeframe={expected[4]} source={expected[9]}"
            )
    return len(rows)


def write_contract_statistics_rows(
    con: sqlite3.Connection,
    rows: list[tuple],
) -> int:
    if not rows:
        return 0
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='market_contract_statistics'"
    ).fetchone()
    if not exists:
        raise RuntimeError("market_contract_statistics table missing")
    con.executemany(
        "INSERT OR REPLACE INTO market_contract_statistics "
        "(ts,collected_ts,cycle_id,symbol,timeframe,oi_contracts,oi_ccy,"
        "oi_usd,taker_sell_usd,taker_buy_usd,taker_buy_ratio,raw,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def prune_feature_history(con: sqlite3.Connection, retention_days: int) -> dict[str, int]:
    """由本表权威writer裁剪历史；新表ts固定为UTC-Z，可直接交给SQLite datetime解析。"""
    if not 7 <= retention_days <= 365:
        raise ValueError("retention_days必须在7..365之间")
    out = {}
    for table in (
        "market_microstructure", "market_trade_flow", "market_positioning",
        "market_contract_statistics",
    ):
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        cur = con.execute(
            f"DELETE FROM {table} WHERE datetime(ts) < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        out[table] = max(0, cur.rowcount)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="50档订单簿与逐笔成交影子采集")
    ap.add_argument("--db-root", default=str(ROOT / "db"))
    ap.add_argument("--focus-file", default=str(ROOT / "focus.md"))
    ap.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS)
    ap.add_argument("--depth", type=int, default=50)
    ap.add_argument("--trades-limit", type=int, default=500)
    ap.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    ap.add_argument("--cycle", default=None)
    ap.add_argument(
        "--positioning", choices=("auto", "always", "off"), default="auto",
        help="OKX官方REST多空账户比：auto=:00/:30，always=本轮，off=关闭",
    )
    ap.add_argument(
        "--positioning-max-symbols", type=int, default=DEFAULT_POSITIONING_SYMBOLS
    )
    ap.add_argument(
        "--positioning-workers", type=int, default=DEFAULT_POSITIONING_WORKERS
    )
    ap.add_argument(
        "--positioning-only", action="store_true",
        help="仅采多空账户比，不抓/写盘口和逐笔；仅用于隔离验证",
    )
    ap.add_argument(
        "--contract-stats", choices=("auto", "always", "off"), default="off",
        help="OKX官方15m合约OI+主动买卖量；默认off，迁移验收后由fast显式启用",
    )
    ap.add_argument(
        "--contract-stats-max-symbols", type=int,
        default=DEFAULT_CONTRACT_STATS_SYMBOLS,
    )
    ap.add_argument(
        "--contract-stats-only", action="store_true",
        help="仅采15m合约OI+主动买卖量；用于隔离全量验收",
    )
    args = ap.parse_args()
    if args.depth != 50:
        print(json.dumps({"ok": False, "error": "生产深度固定为50档"}, ensure_ascii=False))
        return 2

    db_path = Path(args.db_root) / "market.db"
    con = sqlite3.connect(str(db_path), timeout=20)
    try:
        feature_limit = max(3, min(args.max_symbols, 150))
        symbols = select_symbols(con, feature_limit, Path(args.focus_file))
        collected_ts = utc_now_iso()
        cycle = args.cycle or cycle_id_now()
        positioning_limit = max(3, min(args.positioning_max_symbols, 1000))
        positioning_symbols = select_positioning_symbols(
            con, positioning_limit,
        )
        contract_stats_limit = max(
            3, min(args.contract_stats_max_symbols, 1000))
        contract_stats_symbols = select_positioning_symbols(
            con, contract_stats_limit,
        )
        contract_stats_values = contract_values_for_symbols(
            con, contract_stats_symbols)
        if args.positioning_only and args.contract_stats_only:
            print(json.dumps({
                "ok": False,
                "error": "positioning-only与contract-stats-only不能同时启用",
            }, ensure_ascii=False))
            return 2
        if args.positioning_only:
            positioning_rows, errors = fetch_positioning_rows(
                positioning_symbols, cycle, workers=args.positioning_workers,
            )
            wrote = write_positioning_rows(con, positioning_rows)
            con.commit()
            selected_count = len(positioning_symbols)
            coverage_rate = wrote / selected_count if selected_count else 0.0
            coverage_passed = positioning_batch_passed(
                selected_count=selected_count,
                positioning_rows=wrote,
            )
            source_freshness = positioning_source_freshness(positioning_rows)
            quality_passed = (
                coverage_passed and bool(source_freshness["passed"])
            )
            print(json.dumps({
                "ok": quality_passed,
                "degraded": not quality_passed,
                "cycle": cycle,
                "selected": positioning_symbols,
                "wrote": {"positioning": wrote},
                "minimum_positioning_coverage": POSITIONING_MINIMUM_COVERAGE,
                "positioning_coverage_rate": coverage_rate,
                "maximum_positioning_source_age_minutes": (
                    POSITIONING_MAXIMUM_SOURCE_AGE_S / 60.0),
                "positioning_source_freshness": source_freshness,
                "positioning_due": True,
                "positioning_only": True,
                "positioning_transport": "okx_official_rest",
                "errors": errors[:20],
            }, ensure_ascii=False))
            return 0 if quality_passed else 1
        if args.contract_stats_only:
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='market_contract_statistics'"
            ).fetchone()
            if not exists:
                print(json.dumps({
                    "ok": False,
                    "error": "market_contract_statistics table missing",
                    "production_database_writes": 0,
                }, ensure_ascii=False))
                return 2
            carryable_rows, _ = latest_valid_direct_contract_statistics_rows(
                con,
                contract_stats_symbols,
                cycle,
                available_at=contract_statistics_carry_preflight_time(),
            )
            fetched_contract_stats_rows, errors = fetch_contract_statistics_rows(
                contract_stats_symbols,
                cycle,
                contract_values=contract_stats_values,
                carryable_symbols=set(carryable_rows),
            )
            contract_stats_rows, contract_stats_quality, carry_errors = (
                complete_contract_statistics_with_previous_batch(
                    con,
                    fetched_contract_stats_rows,
                    contract_stats_symbols,
                    cycle,
                    available_at=utc_now_iso(),
                )
            )
            errors.extend(carry_errors)
            wrote = write_contract_statistics_rows(con, contract_stats_rows)
            con.commit()
            coverage_rate = float(
                contract_stats_quality["availability_coverage_rate"])
            direct_coverage_rate = float(
                contract_stats_quality["direct_coverage_rate"])
            carried = int(
                contract_stats_quality["carried_forward_symbols"])
            # 用户硬门槛是99%，且模型只使用本轮直接官方观测；因此状态必须
            # 同时绑定 direct 与 availability。少于1%的新上市/暂未出历史桶标的
            # 仍保留逐币 warning/error 和独立审计，但不能把已达99%的整轮误标
            # degraded。carry-forward 只提供运行可用性，绝不替代 direct 门槛。
            passed = contract_statistics_batch_passed(
                availability_coverage_rate=coverage_rate,
                direct_coverage_rate=direct_coverage_rate,
                written_rows=wrote,
                completed_rows=len(contract_stats_rows),
            )
            degraded = not passed
            warnings = []
            if carried:
                warnings.append(
                    "contract statistics used bounded official previous-batch "
                    f"carry-forward for {carried}/{len(contract_stats_symbols)} "
                    "symbols; excluded from model features"
                )
            if carry_errors:
                warnings.append(
                    f"contract statistics carry-forward unresolved="
                    f"{len(carry_errors)}"
                )
            print(json.dumps({
                "ok": passed,
                "degraded": degraded,
                "warnings": warnings,
                "cycle": cycle,
                "selected": contract_stats_symbols,
                "wrote": {"contract_statistics": wrote},
                "coverage_rate": coverage_rate,
                "direct_coverage_rate": direct_coverage_rate,
                "carry_forward_rate": contract_stats_quality[
                    "carry_forward_rate"],
                "carried_forward_symbols": carried,
                "contract_statistics_quality": contract_stats_quality,
                "minimum_coverage": CONTRACT_STATS_MINIMUM_COVERAGE,
                "contract_statistics_due": True,
                "contract_statistics_only": True,
                "contract_statistics_transport": "okx_official_rest",
                "errors": errors[:20],
            }, ensure_ascii=False))
            return 0 if passed else 1

        try:
            selection_snapshot = freeze_market_feature_selection(
                con,
                symbols,
                cycle_id=cycle,
                collected_ts_utc=collected_ts,
                max_symbols=feature_limit,
            )
        except (sqlite3.Error, ValueError) as selection_exc:
            print(json.dumps({
                "ok": False,
                "degraded": True,
                "cycle": cycle,
                "error": (
                    "market_feature_selection_snapshot_failed: "
                    f"{type(selection_exc).__name__}: {selection_exc}"
                ),
            }, ensure_ascii=False))
            return 2
        if selection_snapshot["status"] == "conflict":
            print(json.dumps({
                "ok": False,
                "degraded": True,
                "cycle": cycle,
                "error": "market_feature_selection_snapshot_conflict",
                "selection_snapshot": selection_snapshot,
            }, ensure_ascii=False))
            return 1

        specs = {
            r[0]: float(r[1] or 0)
            for r in con.execute(
                "SELECT instId,ctVal FROM instruments_cache WHERE instId IN (%s)"
                % ",".join("?" for _ in symbols), symbols
            ).fetchall()
        } if symbols else {}
        do_positioning = positioning_due(cycle, args.positioning)
        do_contract_stats = contract_statistics_due(
            cycle, args.contract_stats)
        contract_stats_carryable: set[str] | None = None
        preflight_errors: list[str] = []
        if do_contract_stats:
            contract_stats_table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='market_contract_statistics'"
            ).fetchone()
            if not contract_stats_table:
                do_contract_stats = False
                preflight_errors.append(
                    "market_contract_statistics table missing; source disabled")
            else:
                carryable_rows, _ = (
                    latest_valid_direct_contract_statistics_rows(
                        con,
                        contract_stats_symbols,
                        cycle,
                        available_at=contract_statistics_carry_preflight_time(),
                    )
                )
                contract_stats_carryable = set(carryable_rows)
        worker_count = 2 + int(do_positioning) + int(do_contract_stats)
        with ThreadPoolExecutor(max_workers=worker_count) as ex:
            books_future = ex.submit(fetch_orderbooks_batch_sync, symbols, 50)
            trades_future = ex.submit(fetch_recent_trades_batch_sync, symbols, args.trades_limit)
            positioning_future = (
                ex.submit(
                    fetch_positioning_rows,
                    positioning_symbols,
                    cycle,
                    None,
                    args.positioning_workers,
                )
                if do_positioning else None
            )
            contract_stats_future = (
                ex.submit(
                    fetch_contract_statistics_rows,
                    contract_stats_symbols,
                    cycle,
                    None,
                    contract_stats_values,
                    contract_stats_carryable,
                )
                if do_contract_stats else None
            )
            books = books_future.result()
            trades = trades_future.result()
            if positioning_future is not None:
                positioning_rows, errors = positioning_future.result()
            else:
                positioning_rows, errors = [], []
            if contract_stats_future is not None:
                contract_stats_rows, contract_stats_errors = (
                    contract_stats_future.result())
                errors.extend(contract_stats_errors)
            else:
                contract_stats_rows = []
            errors.extend(preflight_errors)

        book_rows = []
        flow_rows = []
        for symbol in symbols:
            ct_val = specs.get(symbol, 0)
            book = books.get(symbol) or {}
            trade_rows = trades.get(symbol) or []
            if ct_val <= 0:
                errors.append(f"{symbol}:ctVal_missing")
                continue
            if book.get("bids") and book.get("asks"):
                book_rows.append(book_features(book, ct_val, cycle, collected_ts, symbol))
            else:
                errors.append(f"{symbol}:book_empty")
            if trade_rows:
                flow_rows.append(flow_features(trade_rows, ct_val, cycle, collected_ts, symbol))
            else:
                errors.append(f"{symbol}:trades_empty")

        con.executemany(
            "INSERT OR REPLACE INTO market_microstructure VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            book_rows,
        )
        con.executemany(
            "INSERT OR REPLACE INTO market_trade_flow VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            flow_rows,
        )
        if positioning_rows:
            try:
                write_positioning_rows(con, positioning_rows)
            except RuntimeError as exc:
                errors.append(str(exc))
                positioning_rows = []
        contract_stats_quality: dict[str, object] | None = None
        if do_contract_stats:
            try:
                (
                    contract_stats_rows,
                    contract_stats_quality,
                    carry_errors,
                ) = complete_contract_statistics_with_previous_batch(
                    con,
                    contract_stats_rows,
                    contract_stats_symbols,
                    cycle,
                    available_at=utc_now_iso(),
                )
                errors.extend(carry_errors)
                write_contract_statistics_rows(con, contract_stats_rows)
            except RuntimeError as exc:
                errors.append(str(exc))
                contract_stats_rows = []
        pruned = {}
        if cycle.endswith("T04:45"):
            pruned = prune_feature_history(con, args.retention_days)
        con.commit()
        selected_count = len(symbols)
        microstructure_coverage_rate = (
            len(book_rows) / selected_count if selected_count else 0.0
        )
        trade_flow_coverage_rate = (
            len(flow_rows) / selected_count if selected_count else 0.0
        )
        feature_batch_passed = market_feature_batch_passed(
            selected_count=selected_count,
            microstructure_rows=len(book_rows),
            trade_flow_rows=len(flow_rows),
        )
        positioning_selected_count = (
            len(positioning_symbols) if do_positioning else 0
        )
        positioning_coverage_rate = (
            len(positioning_rows) / positioning_selected_count
            if positioning_selected_count else None
        )
        positioning_source_quality = (
            positioning_source_freshness(positioning_rows)
            if do_positioning else None
        )
        positioning_quality_passed = (
            not do_positioning
            or (
                positioning_batch_passed(
                    selected_count=positioning_selected_count,
                    positioning_rows=len(positioning_rows),
                )
                and bool(positioning_source_quality["passed"])
            )
        )
        overall_batch_passed = (
            feature_batch_passed and positioning_quality_passed
        )
        out = {
            "ok": overall_batch_passed,
            "degraded": not overall_batch_passed,
            "cycle": cycle,
            "selected": symbols,
            "selection_snapshot": selection_snapshot,
            "wrote": {
                "microstructure": len(book_rows),
                "trade_flow": len(flow_rows),
                "positioning": len(positioning_rows),
                "contract_statistics": len(contract_stats_rows),
            },
            "minimum_feature_coverage": MARKET_FEATURE_MINIMUM_COVERAGE,
            "microstructure_coverage_rate": microstructure_coverage_rate,
            "trade_flow_coverage_rate": trade_flow_coverage_rate,
            "minimum_positioning_coverage": POSITIONING_MINIMUM_COVERAGE,
            "positioning_coverage_rate": positioning_coverage_rate,
            "maximum_positioning_source_age_minutes": (
                POSITIONING_MAXIMUM_SOURCE_AGE_S / 60.0),
            "positioning_source_freshness": positioning_source_quality,
            "positioning_quality_passed": positioning_quality_passed,
            "positioning_due": do_positioning,
            "positioning_selected": positioning_selected_count,
            "positioning_transport": "okx_official_rest",
            "contract_statistics_due": do_contract_stats,
            "contract_statistics_selected": (
                len(contract_stats_symbols) if do_contract_stats else 0),
            "contract_statistics_transport": "okx_official_rest",
            "contract_statistics_quality": contract_stats_quality,
            "depth_levels": 50,
            "retention_days": args.retention_days,
            "pruned": pruned,
            "errors": errors[:20],
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0 if overall_batch_passed else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
