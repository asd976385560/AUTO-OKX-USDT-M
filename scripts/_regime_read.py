# -*- coding: utf-8 -*-
r"""regime.db 读取助手（2026-06-26；2026-07-31 核实迁移已收尾）。

当前生产权威只在 regime.db，market.db.cross_market 已 DROP。本助手仍保留只读
market.db fallback，只用于读取迁移前归档库或隔离夹具；它不会重建表、双写或把
fallback 重新引入生产口径。两库都存在时仍返回 ts 更新的一行。

只读、纯标准库。当前生产调用应返回 regime.db。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

CST = timezone(timedelta(hours=8))
_CST_FMT = "%Y-%m-%d %H:%M:%S"


def _latest(db_path: Path) -> Optional[dict]:
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            # ts 为干净 UTC ISO（'...Z'，字典序==时序；非 cycle_runs 的 TEXT 前缀陷阱），
            # 用 ts DESC 而非 rowid DESC：对乱序插入（如回填把旧行追加到高 rowid）也取真·最新。
            r = con.execute(
                "SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return dict(r) if r else None
        finally:
            con.close()
    except Exception:
        return None


def latest_cross_market(db_root) -> Optional[dict]:
    """返回 cross_market 最新一行（dict，含全部列）；regime.db vs market.db 取 ts 更新者。
    两库都空/不可读 → None。ts 为同格式 UTC ISO 字符串，可直接字典序比较。"""
    root = Path(db_root)
    reg = _latest(root / "regime.db")
    mkt = _latest(root / "market.db")
    if reg is None:
        return mkt
    if mkt is None:
        return reg
    return reg if str(reg.get("ts") or "") >= str(mkt.get("ts") or "") else mkt


def _regime_at(db_path: Path, utc_key: str) -> tuple[Optional[str], Optional[str]]:
    if not db_path.exists():
        return None, None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            # ts 为干净 UTC ISO（'...Z'），字典序==时序，可直接串比较取"不晚于该时刻"的最近一行。
            r = con.execute(
                "SELECT ts, regime FROM cross_market "
                "WHERE regime IS NOT NULL AND regime != '' AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (utc_key,),
            ).fetchone()
            return (r["regime"], r["ts"]) if r else (None, None)
        finally:
            con.close()
    except Exception:
        return None, None


def regime_at(db_root, ts_cst: str) -> tuple[Optional[str], Optional[str]]:
    """取 ``ts_cst`` 那一刻的 regime 事实标签（成交当时，非"现在"）。

    经验行要记的是下单当时的 regime；若回填时改取最新 regime，等于把后见之明写进
    历史样本，find_similar/历史基线都会被污染。故此处严格point-in-time：只取
    ``ts <= 该时刻`` 的最近一行，取不到就返回 None（宁可留 NULL，不猜）。

    ``ts_cst`` 为 trade_experiences.ts 口径的 CST(UTC+8) 字符串
    ``'YYYY-MM-DD HH:MM:SS'``；cross_market.ts 为 UTC ISO ``'...Z'``，此处显式换算。
    返回 ``(regime, matched_utc_ts)``；无匹配/不可读返回 ``(None, None)``。
    """
    if not ts_cst:
        return None, None
    try:
        naive = datetime.strptime(str(ts_cst).strip()[:19], _CST_FMT)
    except (TypeError, ValueError):
        return None, None
    utc_key = (
        naive.replace(tzinfo=CST)
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    root = Path(db_root)
    regime, matched = _regime_at(root / "regime.db", utc_key)
    if regime is None:
        # 迁移前归档库/隔离夹具兜底；生产 market.db.cross_market 已 DROP。
        regime, matched = _regime_at(root / "market.db", utc_key)
    return regime, matched


def latest_source(db_root) -> str:
    """诊断用：返回本次最新行实际来自 'regime.db' / 'market.db' / 'none'。"""
    root = Path(db_root)
    reg = _latest(root / "regime.db")
    mkt = _latest(root / "market.db")
    if reg is None and mkt is None:
        return "none"
    if reg is None:
        return "market.db"
    if mkt is None:
        return "regime.db"
    return "regime.db" if str(reg.get("ts") or "") >= str(mkt.get("ts") or "") else "market.db"
