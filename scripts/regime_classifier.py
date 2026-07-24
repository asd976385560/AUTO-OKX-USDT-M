# -*- coding: utf-8 -*-
"""BTC 主导、DXY 修正的三分类 regime 纯函数。

标签保持 trend_up / trend_down / range，避免破坏 V2.0 下游契约。分类只用于观察和
LLM 判断输入，不是交易硬闸。
"""
from __future__ import annotations

from typing import Any, Optional


DEFAULT_PARAMS = {
    "price_ma_threshold": 0.0075,
    "ma_spread_threshold": 0.005,
    "momentum_threshold": 0.015,
    "rsi_upper": 52.0,
    "rsi_lower": 48.0,
    "dxy_threshold": 0.002,
    "score_cutoff": 3,
    # -1 = 历史样本验证后的均值回归预期；+1 = 趋势延续预期。
    "btc_orientation": -1,
}


def classify_regime(
    *,
    close: Optional[float],
    ma5: Optional[float],
    ma20: Optional[float],
    rsi14: Optional[float],
    return_24h: Optional[float],
    dxy_d1: Optional[float],
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """返回 regime、方向分数和透明特征；核心 BTC 特征缺失时 ok=False。

    DXY 只是一个修正票，不再单独决定加密市场 regime。
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    required = (close, ma5, ma20, rsi14, return_24h)
    if any(v is None for v in required) or not close or not ma20:
        return {
            "ok": False,
            "regime": None,
            "score": None,
            "reason": "btc_4h_features_missing",
        }

    close = float(close)
    ma5 = float(ma5)
    ma20 = float(ma20)
    rsi14 = float(rsi14)
    return_24h = float(return_24h)
    price_vs_ma20 = close / ma20 - 1.0
    ma_spread = ma5 / ma20 - 1.0
    btc_score = 0
    votes: list[str] = []

    def vote(value: float, threshold: float, name: str) -> None:
        nonlocal btc_score
        if value >= threshold:
            btc_score += 1
            votes.append(f"{name}=up")
        elif value <= -threshold:
            btc_score -= 1
            votes.append(f"{name}=down")
        else:
            votes.append(f"{name}=neutral")

    vote(price_vs_ma20, float(p["price_ma_threshold"]), "price_ma20")
    vote(ma_spread, float(p["ma_spread_threshold"]), "ma5_ma20")
    vote(return_24h, float(p["momentum_threshold"]), "ret24h")

    if rsi14 >= float(p["rsi_upper"]):
        btc_score += 1
        votes.append("rsi=up")
    elif rsi14 <= float(p["rsi_lower"]):
        btc_score -= 1
        votes.append("rsi=down")
    else:
        votes.append("rsi=neutral")

    score = int(p["btc_orientation"]) * btc_score
    if dxy_d1 is not None:
        dxy_d1 = float(dxy_d1)
        if dxy_d1 >= float(p["dxy_threshold"]):
            score -= 1
            votes.append("dxy=down")
        elif dxy_d1 <= -float(p["dxy_threshold"]):
            score += 1
            votes.append("dxy=up")
        else:
            votes.append("dxy=neutral")
    else:
        votes.append("dxy=missing")

    cutoff = int(p["score_cutoff"])
    regime = "trend_up" if score >= cutoff else "trend_down" if score <= -cutoff else "range"
    return {
        "ok": True,
        "regime": regime,
        "score": score,
        "btc_structure_score": btc_score,
        "votes": votes,
        "features": {
            "close": close,
            "ma5": ma5,
            "ma20": ma20,
            "rsi14": rsi14,
            "return_24h": return_24h,
            "price_vs_ma20": price_vs_ma20,
            "ma_spread": ma_spread,
            "dxy_d1": dxy_d1,
        },
        "params": p,
    }
