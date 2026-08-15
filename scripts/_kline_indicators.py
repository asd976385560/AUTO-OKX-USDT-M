# -*- coding: utf-8 -*-
r"""_kline_indicators.py — kline_cache 扩展指标共享库（BOLL20/OBV，2026-08-13）。

规格书「技术指标」维度补全：MA/EMA/MACD/RSI/ATR 已由两个采集器的
compute_indicators 实装（ma5/ma20 即 EMA 口径，MACD 内含 EMA12/26），本模块补
布林带 BOLL(20,2) 与 OBV，供 `collect_data.py`（15m）与 `collect_slow.py`
（1H/4H/1D/1W/1M）统一调用——单一实现防两处口径漂移。

口径（确定性，非 LLM）：
  - BOLL: mid=SMA20（经典布林口径，刻意不用 ma20 列的 EMA 值）；
    up/dn = mid ± 2×总体标准差(ddof=0)。窗口不足或含缺值 → None（宁缺勿假）。
  - OBV: 抓取窗口内累计（每批 limit=60 根，窗口起点计 0）。绝对值无跨批意义，
    **只可作方向/背离参考**；close/volume 缺值时该根不改变累计并输出 None。

migration-aware 写入（对齐 news_writer 先例）：`kline_insert_plan(con)` 检查
kline_cache 是否已有扩展列（apply_kline_indicator_schema.py 迁移后出现）。
迁移未跑时采集器按旧 13 列写入（安全、零行为变化），跑后自动启用 17 列。
扩展列**不进入**多周期 OPEN 证据契约与既有 99% 审计口径（那些是预注册前向门，
本模块只加参考证据）。零模型名（红线①）。
"""
from __future__ import annotations

import math
import sqlite3
from typing import Sequence

BOLL_PERIOD = 20
BOLL_K = 2.0

# 迁移新增列（顺序即 INSERT 顺序）；改动须同步 apply_kline_indicator_schema.py。
EXTENDED_COLUMNS: tuple[str, ...] = (
    "boll20_mid", "boll20_up", "boll20_dn", "obv",
)

_BASE_COLUMNS: tuple[str, ...] = (
    "ts", "symbol", "tf", "o", "h", "l", "c", "v",
    "ma5", "ma20", "atr14", "rsi14", "macd_hist",
)


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def boll_series(
    closes: Sequence[float | None],
    period: int = BOLL_PERIOD,
    k: float = BOLL_K,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """经典布林带：SMA(period) ± k×总体标准差。窗口不足/含缺值 → None。"""
    n = len(closes)
    mid: list[float | None] = [None] * n
    up: list[float | None] = [None] * n
    dn: list[float | None] = [None] * n
    if period <= 1:
        return mid, up, dn
    for i in range(period - 1, n):
        window = [_finite(c) for c in closes[i + 1 - period: i + 1]]
        if any(w is None for w in window):
            continue
        mean = sum(window) / period
        variance = sum((w - mean) ** 2 for w in window) / period
        std = math.sqrt(variance)
        mid[i] = mean
        up[i] = mean + k * std
        dn[i] = mean - k * std
    return mid, up, dn


def obv_series(
    closes: Sequence[float | None],
    volumes: Sequence[float | None],
) -> list[float | None]:
    """窗口内累计 OBV：close 升 +v、降 -v、平/缺值不变；缺值根输出 None。

    窗口起点计 0（首根有 close 即输出 0.0）；绝对值跨批不可比，仅方向参考。
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    running = 0.0
    prev_close: float | None = None
    for i in range(n):
        close = _finite(closes[i])
        volume = _finite(volumes[i]) if i < len(volumes) else None
        if close is None:
            out[i] = None
            continue
        if prev_close is not None and volume is not None:
            if close > prev_close:
                running += volume
            elif close < prev_close:
                running -= volume
        out[i] = running
        prev_close = close
    return out


def extend_with_boll_obv(candles: list[dict]) -> list[dict]:
    """就地为 candle dict 列表补 boll20_mid/up/dn 与 obv 键（None 安全）。"""
    if not candles:
        return candles
    closes = [c.get("c") for c in candles]
    volumes = [c.get("v") for c in candles]
    mid, up, dn = boll_series(closes)
    obv = obv_series(closes, volumes)
    for i, candle in enumerate(candles):
        candle["boll20_mid"] = mid[i]
        candle["boll20_up"] = up[i]
        candle["boll20_dn"] = dn[i]
        candle["obv"] = obv[i]
    return candles


def kline_insert_plan(con: sqlite3.Connection) -> dict:
    """检查 kline_cache 扩展列是否就绪，返回统一 INSERT 计划。

    返回 {extended: bool, sql: str, columns: tuple}；extended=False 表示迁移
    未跑（apply_kline_indicator_schema.py），采集器按旧列写入并可打一条 WARN。
    PRAGMA 失败（连接异常/测试替身）时 fail-safe 回退旧列——后续 executemany
    会如实暴露真正的连接问题，不在此处吞。
    """
    try:
        cols = {
            str(r[1]) for r in con.execute("PRAGMA table_info(kline_cache)")
        }
    except Exception:
        cols = set()
    extended = all(c in cols for c in EXTENDED_COLUMNS)
    columns = _BASE_COLUMNS + EXTENDED_COLUMNS if extended else _BASE_COLUMNS
    sql = (
        "INSERT OR REPLACE INTO kline_cache ("
        + ", ".join(columns)
        + ") VALUES (" + ",".join("?" for _ in columns) + ")"
    )
    return {"extended": extended, "sql": sql, "columns": columns}


def extended_row_tail(item: dict) -> tuple:
    """按 EXTENDED_COLUMNS 顺序取 candle dict 的扩展值（缺键=None）。"""
    return tuple(item.get(col) for col in EXTENDED_COLUMNS)
