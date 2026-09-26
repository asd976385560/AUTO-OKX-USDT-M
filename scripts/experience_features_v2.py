# -*- coding: utf-8 -*-
r"""experience_features_v2.py — 经验特征派生（v2 冻结、v3 前向启用）。

派生（全部确定性，as-of 语义）：
  asset_class        core.asset_class 权威表
  stop_distance_pct  |fill_px - sl_trigger_px| / fill_px（行 raw）
  planned_rr         raw.decision_card.ev_check.gross_rr（Wave1 起）、
                     risk_reward entry/stop/target 几何重算（旧卡），或
                     open_execution_package_v1 三价几何重算（最小闭环执行包）
  funding_rate       market.db.derivatives 最近一条 ≤ as_of（4h 内，否则 None）
  vol_24h_pct        15m K 线 as_of 前 24h (max(h)-min(l))/last(c)
  trend_1h/4h        1H/4H K 线 as_of 时 MA20 vs MA50（+1/-1；bars<50=None）

现有 v2 向量按历史证据冻结，不回填、不重算。部署后 writer 仅新增带明确
``experience_features_v3_strict_24h`` epoch 的 v3；finder 也只在完全相同的
v3 epoch 内比较，禁止 v2/v3 静默混算。CLI 仅报告版本分布，--apply fail-closed。

2026-09-26 对照 V3 similarity 模块优化（特征语义与 epoch 不变）：
  - 24h 波幅窗按 15m 栅格锚定：as_of 先向下取整到所属 15m bar 起点，再要求
    恰好 96 根不重复 bar。此前 96 个期望时间戳直接从 as_of 逐 15 分钟倒推，
    writer 传入的成交时刻几乎不落在栅格上，vol_24h_pct 实际恒为 None；
    栅格对齐的 as_of 结果逐字节不变。
  - 时间只解析一次；数值一律有限性校验（bool/NaN/inf 不冒充数字）；
    K 线 h/l/c 全空时返回 None 而非抛 ValueError。
  - writer / finder / 本脚本共用同一份基础特征装配（experience_base /
    market_features / market_context / vector_payload），不再各自复制
    止损距离与计划 RR 逻辑；无决策卡的执行包也能给出 planned_rr。
  - derive_market_features 支持只取子集（instrument_context 仅需 trend_4h，
    不再为一个字段跑四条查询）。
  - 版本分布与 finder 的 exact v3 epoch 判定共用 _simutil.classify_stored_vector，
    v==3 但 epoch 不符的行单列 v3_epoch_mismatch，不再冒充前向可比。

2026-09-26 第二批（主人「差异按照 V3 的改」）：前向 v4 特征集 = V3 similarity 的
1H 指标（ATR% / RSI / EMA20-50-200 排列 / 4h·16h 收益 / 成交额 z）+ 止损距离 +
开仓 UTC 小时；writer 落 v4（另存 features_v3 供追溯），finder 在 v4 空间比较，
历史 v1/v2/v3 行由 features_v4_for_row 按行 ts 从 kline_cache 现算（确定性、
无前视）——V3 对 legacy 行也是"有什么特征比什么"，不再以 epoch 一刀切排除。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SCRIPTS = str(_ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import _simutil  # noqa: E402
# 按模块引用而非 from-import：测试可 patch core.asset_class.asset_class_of。
from core import asset_class as _asset_class  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
UTC_Z_FMT = "%Y-%m-%dT%H:%M:%SZ"
BAR_15M = timedelta(minutes=15)
VOL_WINDOW_BARS = 96                      # 24h / 15m
FUNDING_LOOKBACK = timedelta(hours=4)
TREND_FAST_BARS = 20
TREND_SLOW_BARS = 50
INDICATOR_1H_KEYS = (
    "atr_pct_1h", "rsi_1h", "ema_trend_1h", "ret_4h", "ret_16h", "vol_z_1h")
MARKET_FEATURE_KEYS = (
    "funding_rate", "vol_24h_pct", "trend_1h", "trend_4h") + INDICATOR_1H_KEYS
_TREND_TIMEFRAMES = (("1H", "trend_1h"), ("4H", "trend_4h"))


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def parse_as_of(value: Any) -> Optional[datetime]:
    """CST / 带偏移 / Z 结尾的 ISO 时间（或 datetime）→ aware UTC；解析失败 → None。"""
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw[-1:] in ("Z", "z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(timezone.utc)


def _utcz(dt: datetime) -> str:
    return dt.strftime(UTC_Z_FMT)


def _cst_to_utcz(ts_cst: Any) -> Optional[str]:
    dt = parse_as_of(ts_cst)
    return _utcz(dt) if dt is not None else None


def hour_utc(value: Any) -> Optional[int]:
    """as-of 时刻的 UTC 小时（0–23）；解析失败 → None。"""
    dt = parse_as_of(value)
    return dt.hour if dt is not None else None


def _floor_to_bar(dt: datetime, bar: timedelta = BAR_15M) -> datetime:
    """向下取整到所属 bar 起点（UTC 栅格）。"""
    seconds = int(bar.total_seconds())
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


def _finite(value: Any) -> Optional[float]:
    return _simutil._finite(value)


# ---------------------------------------------------------------------------
# 市场态（as-of，严格 ``(lower, as_of]`` 窗）
# ---------------------------------------------------------------------------
def _funding_rate(mcon: sqlite3.Connection, symbol: str,
                  as_of: datetime) -> Optional[float]:
    row = mcon.execute(
        "SELECT funding_rate FROM derivatives WHERE symbol=? AND ts<=? "
        "AND ts>? ORDER BY ts DESC LIMIT 1",
        (symbol, _utcz(as_of), _utcz(as_of - FUNDING_LOOKBACK))).fetchone()
    return _finite(row[0]) if row else None


def _vol_24h_pct(mcon: sqlite3.Connection, symbol: str,
                 as_of: datetime) -> Optional[float]:
    """恰好 96 根不重复 15m bar（锚定到 as_of 所属栅格点）才给数，否则 None。"""
    anchor = _floor_to_bar(as_of)
    lower = anchor - VOL_WINDOW_BARS * BAR_15M
    bars = mcon.execute(
        "SELECT ts, h, l, c FROM kline_cache WHERE symbol=? AND tf='15m' "
        "AND ts<=? AND ts>? ORDER BY ts",
        (symbol, _utcz(anchor), _utcz(lower))).fetchall()
    if len(bars) != VOL_WINDOW_BARS:
        return None
    expected = {
        _utcz(anchor - BAR_15M * offset) for offset in range(VOL_WINDOW_BARS)
    }
    if {str(bar[0]) for bar in bars} != expected:
        return None
    highs = [h for h in (_finite(bar[1]) for bar in bars) if h is not None]
    lows = [l for l in (_finite(bar[2]) for bar in bars) if l is not None]
    last_close = next(
        (c for c in (_finite(bar[3]) for bar in reversed(bars)) if c), None)
    if not highs or not lows or not last_close:
        return None
    hi, lo = max(highs), min(lows)
    if hi <= 0.0 or lo <= 0.0 or last_close <= 0.0:
        return None
    return round((hi - lo) / last_close, 6)


def _ma_trend(mcon: sqlite3.Connection, symbol: str, tf: str,
              as_of: datetime) -> Optional[int]:
    """MA20 vs MA50（收盘价，升序求和与历史实现逐字节一致）；不足 50 根 → None。"""
    rows = mcon.execute(
        "SELECT c FROM kline_cache WHERE symbol=? AND tf=? AND ts<=? "
        "ORDER BY ts DESC LIMIT ?",
        (symbol, tf, _utcz(as_of), TREND_SLOW_BARS)).fetchall()
    closes = [c for c in (_finite(row[0]) for row in rows) if c is not None]
    if len(closes) < TREND_SLOW_BARS:
        return None
    closes.reverse()
    fast = sum(closes[-TREND_FAST_BARS:]) / TREND_FAST_BARS
    slow = sum(closes[-TREND_SLOW_BARS:]) / TREND_SLOW_BARS
    return 1 if fast > slow else (-1 if fast < slow else 0)


def _indicators_1h(mcon: sqlite3.Connection, symbol: str,
                   as_of: datetime) -> dict[str, Any]:
    """as-of 之前（含）最多 500 根 1H K 线 → V3 同口径指标（见 _simutil.compute_indicators_1h）。"""
    params = (symbol, _utcz(as_of), _simutil.INDICATOR_1H_BARS)
    try:
        rows = mcon.execute(
            "SELECT h, l, c, v FROM kline_cache WHERE symbol=? AND tf='1H' "
            "AND ts<=? ORDER BY ts DESC LIMIT ?", params).fetchall()
    except sqlite3.OperationalError:
        # 精简库（旧迁移 / 隔离测试）没有成交额列：成交额 z 留空，其余照算。
        rows = [
            (h, l, c, None) for h, l, c in mcon.execute(
                "SELECT h, l, c FROM kline_cache WHERE symbol=? AND tf='1H' "
                "AND ts<=? ORDER BY ts DESC LIMIT ?", params).fetchall()
        ]
    rows.reverse()
    return _simutil.compute_indicators_1h(rows)


def derive_market_features(
    mcon: sqlite3.Connection,
    symbol: str,
    as_of_cst: Any,
    fields: Iterable[str] = MARKET_FEATURE_KEYS,
) -> dict[str, Any]:
    """funding / vol / trend / 1H 指标 as-of（严格 ``(lower, as_of]`` 窗）。

    ``fields`` 只取子集时仅执行对应查询；返回 dict 的键 = 请求的合法键
    （按 MARKET_FEATURE_KEYS 顺序），缺数据一律 None，如实留空。
    """
    wanted = {fields} if isinstance(fields, str) else set(fields)
    out: dict[str, Any] = {
        key: None for key in MARKET_FEATURE_KEYS if key in wanted}
    as_of = parse_as_of(as_of_cst)
    if as_of is None or not out:
        return out

    def guarded(compute):
        # 单个特征的表 / 列缺失（精简库、迁移未跑）只让该特征留空，不拖垮其它特征。
        try:
            return compute()
        except sqlite3.OperationalError:
            return None

    if "funding_rate" in out:
        out["funding_rate"] = guarded(lambda: _funding_rate(mcon, symbol, as_of))
    if "vol_24h_pct" in out:
        out["vol_24h_pct"] = guarded(lambda: _vol_24h_pct(mcon, symbol, as_of))
    for tf, key in _TREND_TIMEFRAMES:
        if key in out:
            out[key] = guarded(lambda tf=tf: _ma_trend(mcon, symbol, tf, as_of))
    if any(key in out for key in INDICATOR_1H_KEYS):
        indicators = guarded(lambda: _indicators_1h(mcon, symbol, as_of)) or {}
        for key in INDICATOR_1H_KEYS:
            if key in out:
                out[key] = indicators.get(key)
    return out


# ---------------------------------------------------------------------------
# 行/回执侧基础特征（纯函数，无 I/O）
# ---------------------------------------------------------------------------
def _geometry_rr(entry: Any, stop: Any, target: Any) -> Optional[float]:
    e, s, t = _finite(entry), _finite(stop), _finite(target)
    if e is None or s is None or t is None:
        return None
    risk = abs(e - s)
    if risk <= 0.0:
        return None
    return round(abs(t - e) / risk, 4)


def planned_rr_from_card(card: Any) -> Optional[float]:
    """决策卡 → 计划盈亏比：ev_check.gross_rr 优先，其次 risk_reward 三价几何重算。"""
    if not isinstance(card, dict):
        return None
    ev = card.get("ev_check")
    if isinstance(ev, dict):
        gross_rr = _finite(ev.get("gross_rr"))
        if gross_rr is not None:
            return gross_rr
    rr = card.get("risk_reward")
    if isinstance(rr, dict):
        return _geometry_rr(rr.get("entry"), rr.get("stop"), rr.get("target"))
    return None


# 兼容旧调用名。
_planned_rr_from_card = planned_rr_from_card


def planned_rr_from_trade(trade: Any) -> Optional[float]:
    """交易回执 → 计划盈亏比：决策卡优先；无卡时按 open_execution_package_v1 三价重算。"""
    if not isinstance(trade, dict):
        return None
    rr = planned_rr_from_card(trade.get("decision_card"))
    if rr is not None:
        return rr
    package = trade.get("open_execution_package")
    if isinstance(package, dict):
        return _geometry_rr(
            package.get("entry"), package.get("stop"), package.get("target"))
    return None


def stop_distance_pct(trade: Any) -> Optional[float]:
    """|fill_px − sl_trigger_px| / fill_px（fill_px 缺失时退回 px）；缺数据 → None。"""
    if not isinstance(trade, dict):
        return None
    fill_px = _finite(trade.get("fill_px")) or _finite(trade.get("px"))
    sl_px = _finite(trade.get("sl_trigger_px"))
    if fill_px is None or sl_px is None or fill_px <= 0.0 or sl_px <= 0.0:
        return None
    return round(abs(fill_px - sl_px) / fill_px, 6)


def experience_base(symbol: str, side: Any, action: Any, regime: Any,
                    trade: Any) -> dict[str, Any]:
    """基础特征骨架（不做 I/O）：市场态、资产类别与开仓小时由 market_context 补齐。"""
    distance = stop_distance_pct(trade)
    base: dict[str, Any] = {
        "symbol": symbol,
        "asset_class": None,
        "side": side, "action": action, "regime": regime,
        "stop_distance_pct": distance,
        "sl_pct": distance,
        "planned_rr": planned_rr_from_trade(trade),
        "opp_score": None,
        "hour_utc": None,
    }
    base.update({key: None for key in MARKET_FEATURE_KEYS})
    return base


def market_features(symbol: str, as_of_cst: Any, db_root: Any,
                    fields: Iterable[str] = MARKET_FEATURE_KEYS) -> dict[str, Any]:
    """打开 market.db（只读）派生市场态；库不存在 → 全 None。sqlite 错误向上抛。"""
    market_db = Path(db_root) / "market.db"
    if not market_db.exists():
        wanted = {fields} if isinstance(fields, str) else set(fields)
        return {key: None for key in MARKET_FEATURE_KEYS if key in wanted}
    mcon = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True, timeout=5)
    try:
        return derive_market_features(mcon, symbol, as_of_cst, fields)
    finally:
        mcon.close()


def market_context(symbol: str, as_of_cst: Any, db_root: Any,
                   fields: Iterable[str] = MARKET_FEATURE_KEYS) -> dict[str, Any]:
    """asset_class（权威表，未分类兜底 crypto）+ 开仓 UTC 小时 + 市场态；writer / finder 共用。"""
    out: dict[str, Any] = {
        "asset_class": _asset_class.asset_class_of(symbol, db_root),
        "hour_utc": hour_utc(as_of_cst),
    }
    out.update(market_features(symbol, as_of_cst, db_root, fields))
    return out


def vector_payload(base: Mapping[str, Any]) -> dict[str, Any]:
    """基础特征 → 落库 experience_vector 载荷：前向 v4（外层与内层均携带固定 epoch），
    另存 features_v3 供旧口径追溯。"""
    return {
        "v": 4,
        "feature_epoch": _simutil.FEATURE_EPOCH_V4,
        "features": _simutil.experience_features_v4(base),
        "features_v3": _simutil.experience_features_v3(base),
    }


def _stored_vector(row: Any) -> Any:
    raw = row["experience_vector"] if "experience_vector" in row.keys() else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def features_v4_for_row(row: sqlite3.Row, db_root: Any,
                        mcon: Optional[sqlite3.Connection] = None,
                        cache: Optional[dict[Any, dict[str, Any]]] = None
                        ) -> dict[str, Any]:
    """任意历史行 → 前向 v4 特征（V3 对 legacy 行的做法：有什么特征比什么）。

    已存 v4 行直接取；v1/v2/v3 行按行 ts 从 kline_cache 现算 1H 指标（确定性、
    无前视），止损距离 / 计划 RR / 资产类别优先取当年冻结值，缺则按 raw 重算。
    ``cache`` 按 (symbol, as-of 整点) 复用同轮已算过的指标。
    """
    stored = _stored_vector(row)
    feats = _simutil.stored_v4_features(stored)
    symbol = str(row["symbol"] or "")
    if feats is not None:
        feats = dict(feats)
        if feats.get("hour_utc") is None:
            feats["hour_utc"] = hour_utc(row["ts"])
        if not feats.get("symbol"):
            feats["symbol"] = symbol.upper() or None
        return feats
    prior: dict[str, Any] = {}
    if isinstance(stored, dict) and isinstance(stored.get("features"), dict):
        prior = stored["features"]
    base = experience_base(
        symbol, row["side"], row["action"], row["regime"], _row_raw(row["raw"]))
    base["asset_class"] = (
        prior.get("asset_class") or _asset_class.asset_class_of(symbol, db_root))
    for key in ("stop_distance_pct", "planned_rr", "funding_rate",
                "vol_24h_pct", "trend_1h", "trend_4h"):
        if base.get(key) is None and prior.get(key) is not None:
            base[key] = prior[key]
    base["sl_pct"] = base.get("stop_distance_pct")
    base["hour_utc"] = hour_utc(row["ts"])
    as_of = parse_as_of(row["ts"])
    if mcon is not None and as_of is not None:
        key = (symbol, as_of.strftime("%Y-%m-%dT%H"))
        indicators = cache.get(key) if cache is not None else None
        if indicators is None:
            try:
                indicators = _indicators_1h(mcon, symbol, as_of)
            except sqlite3.Error:
                indicators = {}
            if cache is not None:
                cache[key] = indicators
        for name in INDICATOR_1H_KEYS:
            base[name] = indicators.get(name)
    return _simutil.experience_features_v4(base)


def _row_raw(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def features_for_row(mcon: sqlite3.Connection, row: sqlite3.Row,
                     db_root: Path) -> dict[str, Any]:
    """一条 trade_experiences 行 → 按当前口径全量重算的前向 v4 特征 dict（不写库）。"""
    base = experience_base(
        row["symbol"], row["side"], row["action"], row["regime"],
        _row_raw(row["raw"]))
    base["asset_class"] = _asset_class.asset_class_of(row["symbol"], db_root)
    base["hour_utc"] = hour_utc(row["ts"])
    base.update(derive_market_features(mcon, row["symbol"], row["ts"]))
    return _simutil.experience_features_v4(base)


# ---------------------------------------------------------------------------
# 版本分布（历史冻结，--apply 恒拒绝）
# ---------------------------------------------------------------------------
def backfill(db_root: Path, apply: bool) -> dict[str, Any]:
    acon = sqlite3.connect(
        f"file:{db_root / 'account.db'}?mode=ro", uri=True, timeout=15)
    try:
        versions = {name: 0 for name in _simutil.STORED_VECTOR_CLASSES}
        total_rows = 0
        for (vector,) in acon.execute(
                "SELECT experience_vector FROM trade_experiences ORDER BY id"):
            total_rows += 1
            try:
                stored = json.loads(vector or "null")
            except (TypeError, json.JSONDecodeError):
                versions["invalid"] += 1
                continue
            versions[_simutil.classify_stored_vector(stored)] += 1
        out = {
            "ok": not apply,
            "dry_run": True,
            "historical_mutation": False,
            "total_rows": total_rows,
            "versions": versions,
            "feature_epoch": _simutil.FEATURE_EPOCH_V4,
        }
        if apply:
            out["error"] = "historical_feature_backfill_frozen"
        return out
    finally:
        acon.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="经验特征版本分布（v2冻结；--apply 恒拒绝）")
    ap.add_argument("--db-root", default=_public_project_path('db'))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    result = backfill(Path(args.db_root), args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
