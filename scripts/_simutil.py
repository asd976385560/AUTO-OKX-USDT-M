# -*- coding: utf-8 -*-
r"""V2.0 §8.5 —— 经验相似度工具（重建 2026-06-26）。

原件于 2026-06-25 ~22:40 随 _okxcli.py 一并丢失（见 memory
v2-incident-20260625-missing-modules）。本重建严格对齐两处消费者的事实契约：
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
from typing import Any, Iterable, Mapping, Optional, Sequence

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


def _finite(v: Any) -> Optional[float]:
    """有限 float；None/bool/空串/NaN/inf/不可解析 → None。

    2026-09-26 对照 V3 similarity 的 ``near`` 有限性门：非有限数字不再以
    NaN 混进贴近度求和，而是按"该特征缺失"处理（只降低覆盖率）。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        out = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def side_token(v: Any) -> Optional[str]:
    """side 归一：long/buy → ``long``，short/sell → ``short``，其余 None。"""
    s = _s(v)
    if "long" in s or s == "buy":
        return "long"
    if "short" in s or s == "sell":
        return "short"
    return None


def action_token(v: Any) -> Optional[str]:
    """action 归一：含 open → ``open``，含 close → ``close``，其余 None。"""
    s = _s(v)
    if "open" in s:
        return "open"
    if "close" in s:
        return "close"
    return None


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
    """余弦相似度；任一零向量或长度不匹配 -> 0.0（安全，不抛）。

    [LEGACY v1] 2026-08-10 Wave2 序9 起相似度改用 similarity_v2；本函数与
    10 维向量仅保留用于旧向量追溯（终稿放行条件：旧向量可追溯），不再参与
    生产匹配。v1 的致命缺陷：有效维只有方向/regime 家族/开平/md5 symbol 桶，
    同 side+regime+action 的任意两标的余弦≈1.0（HYPE↔BZ 伪近邻实锤）。
    """
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


# ---------------------------------------------------------------------------
# 相似度 v2（2026-08-10 Wave2 序9，特征集按终稿方案清单）
# ---------------------------------------------------------------------------
SIMILARITY_VERSION_V2 = "similarity_v2"
SIMILARITY_VERSION_V3 = "similarity_v3_strict_24h"
SIMILARITY_VERSION = SIMILARITY_VERSION_V3
FEATURE_EPOCH_V3 = "experience_features_v3_strict_24h"

# 数值特征 → 贴近度尺度（exp(-|q-r|/scale)）；尺度=该特征"半衰差"数量级，
# 定死为常量（禁 registry/LLM 调参，与风控常量同纪律）。
V2_NUMERIC_SCALES = {
    "stop_distance_pct": 0.02,   # 止损距离：差 2% → e^-1
    "planned_rr": 0.6,           # 计划盈亏比
    "funding_rate": 0.0005,      # 资金费率（8h）
    "vol_24h_pct": 0.025,        # 24h 高低幅
}
V2_TREND_KEYS = ("trend_1h", "trend_4h")  # -1|0|1；相等=1 否则 0
V2_HARD_GATES = ("asset_class", "side", "action")


def experience_features_v2(d: Mapping[str, Any]) -> dict[str, Any]:
    """把（拟）经验规整成 v2 特征 dict（不做任何 I/O；市场态字段由调用方
    经 experience_features 派生器补齐，缺失=None 如实留空）。"""
    return {
        "v": 2,
        "asset_class": _s(d.get("asset_class")) or None,
        "side": side_token(d.get("side")),
        "action": action_token(d.get("action")),
        "regime": _s(d.get("regime")) or None,
        "stop_distance_pct": d.get("stop_distance_pct"),
        "planned_rr": d.get("planned_rr"),
        "funding_rate": d.get("funding_rate"),
        "vol_24h_pct": d.get("vol_24h_pct"),
        "trend_1h": d.get("trend_1h"),
        "trend_4h": d.get("trend_4h"),
    }


def experience_features_v3(d: Mapping[str, Any]) -> dict[str, Any]:
    """Forward-only v3 feature payload with an explicit non-mixable epoch."""
    out = experience_features_v2(d)
    out["v"] = 3
    out["feature_epoch"] = FEATURE_EPOCH_V3
    return out


def similarity_v2(qf: Mapping[str, Any], rf: Mapping[str, Any]) -> float:
    """v2 相似度 ∈ [0,1]：硬门（资产类别/方向/动作不同=0）+ 数值特征贴近度
    × 覆盖惩罚。regime 家族只在双方均为 crypto 时计入（BTC 口径对股票/商品
    型标的无解释力，终稿序11）。确定性、无 I/O。"""
    for key in V2_HARD_GATES:
        q, r = qf.get(key), rf.get(key)
        if not q or not r or q != r:
            return 0.0
    scores: list[float] = []
    for key, scale in V2_NUMERIC_SCALES.items():
        # 非有限值（NaN/inf/bool/坏字符串）视为缺失：只降覆盖率，不污染求和。
        q, r = _finite(qf.get(key)), _finite(rf.get(key))
        if q is None or r is None:
            continue
        scores.append(math.exp(-abs(q - r) / scale))
    for key in V2_TREND_KEYS:
        q, r = qf.get(key), rf.get(key)
        if q is None or r is None:
            continue
        scores.append(1.0 if q == r else 0.0)
    if qf.get("asset_class") == "crypto":
        q, r = qf.get("regime"), rf.get("regime")
        if q and r:
            scores.append(1.0 if q == r else 0.3)
    total_soft = len(V2_NUMERIC_SCALES) + len(V2_TREND_KEYS) + (
        1 if qf.get("asset_class") == "crypto" else 0)
    if not scores:
        # 只过了硬门、无任何软特征可比：给保底相似度，覆盖=0 的惩罚显式化
        return 0.30
    coverage = len(scores) / total_soft
    return round(
        (sum(scores) / len(scores)) * (0.5 + 0.5 * math.sqrt(coverage)), 4)


def similarity_v3(qf: Mapping[str, Any], rf: Mapping[str, Any]) -> float:
    """Compare only exact v3 epochs; v2/v3 feature spaces never mix silently."""
    if qf.get("v") != 3 or rf.get("v") != 3:
        return 0.0
    if (
        qf.get("feature_epoch") != FEATURE_EPOCH_V3
        or rf.get("feature_epoch") != FEATURE_EPOCH_V3
    ):
        return 0.0
    return similarity_v2(qf, rf)


# ---------------------------------------------------------------------------
# 2026-09-26 对照 V3 similarity 模块补齐的统计与版本判定工具（纯函数、无 I/O）
# ---------------------------------------------------------------------------
STORED_VECTOR_CLASSES = (
    "v1_or_legacy", "v2_frozen", "v3_forward", "v3_epoch_mismatch", "invalid",
)


def wilson_lo95(wins: int, n: int) -> float:
    """胜率的 Wilson 95% 置信下界（对小样本不撒谎：3/3 不能算"必胜"）。"""
    try:
        wins_i = int(wins)
        n_i = int(n)
    except (TypeError, ValueError):
        return 0.0
    if n_i <= 0 or wins_i < 0:
        return 0.0
    wins_i = min(wins_i, n_i)
    z = 1.959963984540054
    p = wins_i / n_i
    denom = 1.0 + z * z / n_i
    centre = p + z * z / (2.0 * n_i)
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n_i)) / n_i)
    return min(1.0, max(0.0, (centre - spread) / denom))


def similarity_weighted(
    pairs: Iterable[tuple[Any, Any]],
) -> tuple[Optional[float], Optional[float]]:
    """按贴近度加权的 (胜率, 平均结果)；输入 (sim, outcome) 对。

    权重 = sim（非有限或 ≤0 的对跳过）；总权重 ≤ 0 → (None, None)。
    """
    weight_sum = 0.0
    win_weight = 0.0
    outcome_sum = 0.0
    for sim, outcome in pairs:
        w = _finite(sim)
        o = _finite(outcome)
        if w is None or o is None or w <= 0.0:
            continue
        weight_sum += w
        outcome_sum += w * o
        if o > 0.0:
            win_weight += w
    if weight_sum <= 0.0:
        return None, None
    return win_weight / weight_sum, outcome_sum / weight_sum


def stored_v3_features(stored: Any) -> Optional[dict[str, Any]]:
    """已存 experience_vector（解析后的 JSON）→ 前向 v3 特征 dict；非 exact epoch → None。

    外层与内层 features 都必须是 v==3 且携带 FEATURE_EPOCH_V3；v1（list）、
    v2、坏行、epoch 不符一律拒绝——finder 与版本分布报告共用本判定，避免两处漂移。
    """
    if not (
        isinstance(stored, dict)
        and stored.get("v") == 3
        and stored.get("feature_epoch") == FEATURE_EPOCH_V3
    ):
        return None
    feats = stored.get("features")
    if (
        isinstance(feats, dict)
        and feats.get("v") == 3
        and feats.get("feature_epoch") == FEATURE_EPOCH_V3
    ):
        return feats
    return None


def classify_stored_vector(stored: Any) -> str:
    """已存 experience_vector（解析后的 JSON）→ STORED_VECTOR_CLASSES 之一。"""
    if stored is None or isinstance(stored, list):
        return "v1_or_legacy"
    if not isinstance(stored, dict):
        return "invalid"
    version = stored.get("v")
    if version == 3:
        return ("v3_forward" if stored_v3_features(stored) is not None
                else "v3_epoch_mismatch")
    if version == 2:
        return "v2_frozen"
    return "invalid"
