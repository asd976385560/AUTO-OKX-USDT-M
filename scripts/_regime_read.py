# -*- coding: utf-8 -*-
r"""regime.db 迁移读取助手（2026-06-26）。

迁移收尾：collect_slow 现双写 cross_market 到 regime.db + market.db。本助手让所有
reader 统一「regime.db 优先、market.db 按 ts 兜底」取最新 cross_market 行——
- 双写转正后两库最新行一致 → 读 regime.db；
- regime.db 缺/旧（迁移过渡窗、或某轮双写失败）→ 自动回退 market.db（仍是完整 superset）。
过渡安全：永远返回两库中 ts 更新的一行，绝不退回 06-21 旧 seed。

只读、纯标准库。market.db 后续若彻底停写 cross_market（owner 决策），本助手自动只认 regime.db。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


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
