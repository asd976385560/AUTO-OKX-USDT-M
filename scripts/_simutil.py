# -*- coding: utf-8 -*-
r"""V2.0 §8.5 —— 经验相似度工具。

本模块严格对齐两处公开消费者的调用契约：
  - find_similar_experience.py / trade_experience_writer.py 调
    `experience_vector(dict)` -> 定长 10 维 list[float]；`cosine(a, b)` -> float。
  - 入参 dict 键（消费者实际传）：symbol / side / regime / action /
    regime_stale（旧 score_total 键仅兼容，不再参与相似度）。

设计要点
--------
- **确定性**：同一逻辑输入恒产同一向量（符号哈希用 hashlib，非 Python salted hash）。
  写入端（writer 存 vector）与查询端（find_similar 现算 query_vec）共用本函数 →
  同空间可比。兼容行保留 10 维布局；相似度计算显式忽略评分兼容维。
- **10 维兼容布局**（第 7 维冻结为 0，避免重写历史向量）：
    0 side_long      1 side_short
    2 regime_dir(+up/-down/0)   3 regime_range   4 regime_extreme
    5 action_open    6 action_close
    7 legacy_score_disabled(恒0)  8 regime_stale(0/1)
    9 symbol 桶([0,1) 稳定哈希)
- 零模型名（红线 #1）；纯标准库，无网络。
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Mapping, Sequence

VEC_DIM = 10


def _s(v: Any) -> str:
    return ("" if v is None else str(v)).strip().lower()


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _symbol_bucket(symbol: str) -> float:
    """稳定哈希 symbol -> [0,1)；hashlib 保证跨进程/跨运行一致（Python hash() 加盐不可用）。"""
    s = _s(symbol)
    if not s:
        return 0.0
    h = hashlib.md5(s.encode("utf-8")).hexdigest()
    return (int(h[:8], 16) % 10_000) / 10_000.0


def experience_vector(d: Mapping[str, Any]) -> list[float]:
    """把一条（拟）经验编码成定长 10 维向量。缺键容错（.get + 默认）。"""
    side = _s(d.get("side"))
    regime = _s(d.get("regime"))
    action = _s(d.get("action"))

    # side：long/buy vs short/sell
    side_long = 1.0 if ("long" in side or side == "buy") else 0.0
    side_short = 1.0 if ("short" in side or side == "sell") else 0.0

    # regime 家族（兼容两套词汇：collect_slow 写 trend_up/down/range；analyst 写 risk_on/off）
    if any(k in regime for k in ("up", "bull", "risk_on", "risk-on")):
        regime_dir = 1.0
    elif any(k in regime for k in ("down", "bear", "risk_off", "risk-off")):
        regime_dir = -1.0
    else:
        regime_dir = 0.0
    regime_range = 1.0 if any(k in regime for k in ("range", "consol", "side")) else 0.0
    regime_extreme = 1.0 if any(k in regime for k in ("extreme", "volat", "panic")) else 0.0

    # action 家族
    action_open = 1.0 if "open" in action else 0.0
    action_close = 1.0 if "close" in action else 0.0

    # 标量
    # 2026-07-23：评分不再是决策协议。保留维度位置只为兼容已存 10 维向量，
    # 实际相似度在 cosine 中忽略该维。
    score_norm = 0.0
    regime_stale = 1.0 if _f(d.get("regime_stale")) >= 1.0 else 0.0
    sym = _symbol_bucket(d.get("symbol"))

    return [
        side_long, side_short,
        regime_dir, regime_range, regime_extreme,
        action_open, action_close,
        score_norm, regime_stale, sym,
    ]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度；任一零向量或长度不匹配 -> 0.0（安全，不抛）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for idx, (x, y) in enumerate(zip(a, b)):
        if idx == 7:
            continue
        try:
            fx = float(x)
            fy = float(y)
        except (TypeError, ValueError):
            return 0.0
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
