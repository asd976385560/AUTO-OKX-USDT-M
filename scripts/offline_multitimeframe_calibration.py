#!/usr/bin/env python3
"""Point-in-time 15m/1H/4H judgment calibration (research only).

The module is intentionally read-only with respect to production databases and
never calls an exchange or order path.  It builds an hourly, cross-sectional
research panel from information that was available at each observation time,
uses the *next* ticker snapshot as the entry anchor, applies a 20 bp round-trip
cost hurdle, and keeps a four-hour purge between train/calibration/test windows.

Only NumPy/Pandas are required so the analysis runs in the repository's pinned
Python environment.  Output files are research artifacts; no production
threshold or configuration is changed by this script.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


UTC = timezone.utc
TIMEFRAMES = ("15m", "1H", "4H")
TF_SECONDS = {"15m": 900, "1H": 3600, "4H": 14400}
TF_WEIGHTS = {"15m": 0.2, "1H": 0.3, "4H": 0.5}
REQUIRED_BAR_FIELDS = (
    "o", "h", "l", "c", "v", "ma5", "ma20", "atr14", "rsi14", "macd_hist"
)
CONTINUOUS_FEATURES = (
    "chg24h",
    "log_quote_volume_usd",
    "log_oi_usd",
    "funding_rate",
    "premium",
    "spread_bps",
    "hour_sin",
    "hour_cos",
    "news_events_6h_log1p",
    "news_events_24h_log1p",
    "official_okx_events_24h_log1p",
    "news_sentiment_24h_mean",
    "alignment_score",
    "aligned_timeframes",
    *tuple(
        f"{tf}_{name}"
        for tf in TIMEFRAMES
        for name in (
            "close_vs_ma20",
            "ma5_vs_ma20",
            "rsi_norm",
            "macd_over_atr",
            "atr_pct",
            "bar_return",
            "range_pct",
        )
    ),
)
ENRICHMENT_FEATURES = (
    "book_spread_bps",
    "log_bid_depth_10bp_usd",
    "log_ask_depth_10bp_usd",
    "log_bid_depth_25bp_usd",
    "log_ask_depth_25bp_usd",
    "log_bid_depth_50bp_usd",
    "log_ask_depth_50bp_usd",
    "imbalance_10bp",
    "imbalance_25bp",
    "imbalance_50bp",
    "slippage_mean_100usd_bps",
    "slippage_asymmetry_100usd_bps",
    "slippage_mean_500usd_bps",
    "slippage_asymmetry_500usd_bps",
    "slippage_mean_1000usd_bps",
    "slippage_asymmetry_1000usd_bps",
    "trade_sample_count_log1p",
    "trade_sample_span_seconds_log1p",
    "log_total_trade_notional_usd",
    "taker_buy_centered",
    "cvd_share",
    "largest_trade_share",
    "positioning_available",
    "positioning_long_short_log",
    "positioning_long_minus_short",
    "contract_stats_available",
    "contract_oi_log_usd",
    "contract_oi_log_change_15m",
    "contract_taker_total_log_usd",
    "contract_taker_buy_centered",
    "contract_oi_taker_interaction",
)


def _ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=20000")
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone())


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        frame.to_csv(tmp_name, index=False, encoding="utf-8")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _iso(value: pd.Timestamp) -> str:
    return value.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _datetime_ns(values: pd.Series) -> np.ndarray:
    """Return UTC epoch nanoseconds independent of Pandas storage resolution."""
    converted = pd.to_datetime(values, utc=True, errors="coerce")
    return converted.astype("datetime64[ns, UTC]").array.asi8


def _parse_news_time(value: Any) -> pd.Timestamp:
    """Treat naive news timestamps as CST; preserve explicit offsets."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return pd.NaT
    text = str(value).strip()
    if not text:
        return pd.NaT
    try:
        stamp = pd.Timestamp(text)
    except (TypeError, ValueError):
        return pd.NaT
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("Asia/Shanghai")
    return stamp.tz_convert("UTC")


def _normalise_news_symbol(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text or any(token in text for token in (",", " ", "/")):
        return None
    if text.endswith("-USDT-SWAP"):
        return text
    if text.endswith("-USDT"):
        return f"{text}-SWAP"
    if "-" not in text and 1 <= len(text) <= 20:
        return f"{text}-USDT-SWAP"
    return None


def _wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = successes / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _ece(probability: np.ndarray, outcome: np.ndarray, bins: int = 10) -> float | None:
    if probability.size == 0:
        return None
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = float(probability.size)
    value = 0.0
    for idx in range(bins):
        if idx == bins - 1:
            mask = (probability >= edges[idx]) & (probability <= edges[idx + 1])
        else:
            mask = (probability >= edges[idx]) & (probability < edges[idx + 1])
        if mask.any():
            value += mask.sum() / total * abs(float(outcome[mask].mean()) - float(probability[mask].mean()))
    return float(value)


def _sigmoid(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-values))


def fit_ridge_logistic(
    x: np.ndarray,
    y: np.ndarray,
    *,
    regularization: float = 0.1,
    iterations: int = 450,
    learning_rate: float = 0.12,
) -> np.ndarray:
    """Deterministic full-batch ridge logistic regression.

    The intercept is column zero and is not regularized.  A decaying step keeps
    the implementation stable without depending on external ML libraries.
    """
    if x.ndim != 2 or y.ndim != 1 or len(x) != len(y):
        raise ValueError("invalid logistic input shapes")
    if len(np.unique(y)) < 2:
        raise ValueError("logistic target requires both classes")
    weights = np.zeros(x.shape[1], dtype=float)
    n = float(len(y))
    for step in range(iterations):
        prediction = _sigmoid(x @ weights)
        gradient = (x.T @ (prediction - y)) / n
        penalty = weights.copy()
        penalty[0] = 0.0
        gradient += regularization * penalty / n
        rate = learning_rate / math.sqrt(1.0 + step / 80.0)
        next_weights = weights - rate * gradient
        if np.max(np.abs(next_weights - weights)) < 1e-8:
            weights = next_weights
            break
        weights = next_weights
    return weights


def fit_platt(raw_probability: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Fit a two-parameter Platt calibrator by damped Newton updates."""
    clipped = np.clip(raw_probability.astype(float), 1e-6, 1 - 1e-6)
    score = np.log(clipped / (1.0 - clipped))
    design = np.column_stack([np.ones(len(score)), score])
    params = np.array([math.log((float(y.mean()) + 1e-4) / (1.0 - float(y.mean()) + 1e-4)), 1.0])
    ridge = np.diag([1e-6, 1e-4])
    for _ in range(60):
        probability = _sigmoid(design @ params)
        weight = np.clip(probability * (1.0 - probability), 1e-6, None)
        gradient = design.T @ (probability - y) + ridge @ params
        hessian = design.T @ (design * weight[:, None]) + ridge
        try:
            delta = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            break
        next_params = params - np.clip(delta, -1.0, 1.0)
        if np.max(np.abs(next_params - params)) < 1e-8:
            params = next_params
            break
        params = next_params
    return float(params[0]), float(params[1])


def apply_platt(raw_probability: np.ndarray, params: tuple[float, float]) -> np.ndarray:
    clipped = np.clip(raw_probability.astype(float), 1e-6, 1 - 1e-6)
    score = np.log(clipped / (1.0 - clipped))
    return _sigmoid(params[0] + params[1] * score)


@dataclass(frozen=True)
class FeatureSpec:
    medians: dict[str, float]
    means: dict[str, float]
    scales: dict[str, float]
    continuous_features: tuple[str, ...]
    asset_classes: tuple[str, ...]
    feature_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "medians": self.medians,
            "means": self.means,
            "scales": self.scales,
            "continuous_features": list(self.continuous_features),
            "asset_classes": list(self.asset_classes),
            "feature_names": list(self.feature_names),
        }


def fit_feature_spec(
    frame: pd.DataFrame,
    train_mask: pd.Series,
    continuous_features: Iterable[str] = CONTINUOUS_FEATURES,
) -> FeatureSpec:
    train = frame.loc[train_mask]
    if train.empty:
        raise ValueError("empty training split")
    continuous_features = tuple(continuous_features)
    medians: dict[str, float] = {}
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for name in continuous_features:
        values = pd.to_numeric(train[name], errors="coerce").replace([np.inf, -np.inf], np.nan)
        median = float(values.median()) if values.notna().any() else 0.0
        filled = values.fillna(median)
        low, high = filled.quantile([0.005, 0.995]).tolist()
        clipped = filled.clip(low, high)
        mean = float(clipped.mean())
        scale = float(clipped.std(ddof=0))
        medians[name] = median
        means[name] = mean
        scales[name] = scale if math.isfinite(scale) and scale > 1e-9 else 1.0
    counts = train["asset_class"].fillna("unknown").astype(str).value_counts()
    asset_classes = tuple(sorted(counts[counts >= 20].index.tolist()))
    feature_names = ("intercept", *continuous_features, *tuple(f"asset_class={x}" for x in asset_classes), "asset_class=other")
    return FeatureSpec(medians, means, scales, continuous_features, asset_classes, feature_names)


def transform_features(frame: pd.DataFrame, spec: FeatureSpec) -> np.ndarray:
    columns: list[np.ndarray] = [np.ones(len(frame), dtype=float)]
    for name in spec.continuous_features:
        values = pd.to_numeric(frame[name], errors="coerce").replace([np.inf, -np.inf], np.nan)
        values = values.fillna(spec.medians[name]).to_numpy(dtype=float)
        values = np.clip((values - spec.means[name]) / spec.scales[name], -8.0, 8.0)
        columns.append(values)
    assets = frame["asset_class"].fillna("unknown").astype(str)
    for category in spec.asset_classes:
        columns.append((assets == category).to_numpy(dtype=float))
    columns.append((~assets.isin(spec.asset_classes)).to_numpy(dtype=float))
    return np.column_stack(columns)


def _load_observations(
    con: sqlite3.Connection,
    start: str,
    end: str,
    sample_minutes: int,
    min_quote_volume_usd: float,
    min_oi_usd: float,
) -> tuple[pd.DataFrame, dict[str, int]]:
    frame = pd.read_sql_query(
        """
        SELECT t.ts,t.symbol,t.last,t.bid,t.ask,t.vol24h,t.chg24h,
               d.funding_rate,d.premium,d.oi_usd,
               i.ctVal,ic.asset_class
        FROM tick_snapshots t
        JOIN derivatives d ON d.ts=t.ts AND d.symbol=t.symbol
        JOIN instruments_cache i ON i.instId=t.symbol
        JOIN instrument_class ic ON ic.symbol=t.symbol
        WHERE t.ts>=? AND t.ts<?
        ORDER BY t.ts,t.symbol
        """,
        con,
        params=(start, end),
    )
    frame["obs_ts"] = pd.to_datetime(frame.pop("ts"), utc=True, errors="coerce")
    initial = len(frame)
    if sample_minutes > 0:
        frame = frame.loc[
            frame["obs_ts"].dt.minute.mod(sample_minutes).eq(0)
            & frame["obs_ts"].dt.second.lt(10)
        ].copy()
    sampled = len(frame)
    frame["quote_volume_usd"] = frame["last"] * frame["vol24h"] * frame["ctVal"]
    valid = (
        frame[["last", "bid", "ask", "ctVal", "oi_usd", "quote_volume_usd"]].notna().all(axis=1)
        & frame[["last", "bid", "ask", "ctVal", "oi_usd", "quote_volume_usd"]].gt(0).all(axis=1)
    )
    valid_market = int(valid.sum())
    liquid = valid & frame["quote_volume_usd"].ge(min_quote_volume_usd) & frame["oi_usd"].ge(min_oi_usd)
    frame = frame.loc[liquid].copy().reset_index(drop=True)
    frame.insert(0, "obs_id", np.arange(len(frame), dtype=np.int64))
    return frame, {
        "joined_rows": initial,
        "sampled_rows": sampled,
        "valid_market_rows": valid_market,
        "liquidity_eligible_rows": len(frame),
    }


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    """Match the production collector's seeded EMA semantics."""
    output = np.full(len(values), np.nan, dtype=float)
    if len(values) < period:
        return output
    multiplier = 2.0 / (period + 1.0)
    current = float(np.mean(values[:period]))
    output[period - 1] = current
    for idx in range(period, len(values)):
        current = (float(values[idx]) - current) * multiplier + current
        output[idx] = current
    return output


def _derive_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    """Recompute point-in-time indicators from raw OHLC history.

    Historical rows written before the collector's indicator repair legitimately
    contain NULL derived fields.  Raw OHLCV is intact, so research recomputes the
    same EMA/Wilder indicators without altering market.db or inventing candles.
    """
    pieces: list[pd.DataFrame] = []
    for _, group in bars.groupby("symbol", sort=False):
        group = group.sort_values("bar_ts").copy()
        close = group["c"].to_numpy(dtype=float)
        high = group["h"].to_numpy(dtype=float)
        low = group["l"].to_numpy(dtype=float)
        n = len(group)
        ma5 = _ema(close, 5)
        ma20 = _ema(close, 20)
        ema12 = _ema(close, 12)
        ema26 = _ema(close, 26)
        macd = ema12 - ema26
        valid_macd = macd[np.isfinite(macd)]
        signal_valid = _ema(valid_macd, 9)
        signal = np.full(n, np.nan, dtype=float)
        valid_positions = np.flatnonzero(np.isfinite(macd))
        signal[valid_positions] = signal_valid
        macd_hist = macd - signal

        atr = np.full(n, np.nan, dtype=float)
        rsi = np.full(n, np.nan, dtype=float)
        if n >= 15:
            previous_close = close[:-1]
            true_range = np.maximum.reduce(
                [high[1:] - low[1:], np.abs(high[1:] - previous_close), np.abs(low[1:] - previous_close)]
            )
            atr_value = float(np.mean(true_range[:14]))
            atr[14] = atr_value
            for idx in range(14, len(true_range)):
                atr_value = (atr_value * 13.0 + float(true_range[idx])) / 14.0
                atr[idx + 1] = atr_value

            delta = np.diff(close)
            avg_gain = float(np.maximum(delta[:14], 0.0).mean())
            avg_loss = float(np.maximum(-delta[:14], 0.0).mean())
            for idx in range(14, len(delta) + 1):
                ratio = avg_gain / avg_loss if avg_loss != 0 else 100.0
                rsi[idx] = 100.0 - 100.0 / (1.0 + ratio)
                if idx < len(delta):
                    avg_gain = (avg_gain * 13.0 + max(float(delta[idx]), 0.0)) / 14.0
                    avg_loss = (avg_loss * 13.0 + max(float(-delta[idx]), 0.0)) / 14.0

        group["ma5"] = ma5
        group["ma20"] = ma20
        group["atr14"] = atr
        group["rsi14"] = rsi
        group["macd_hist"] = macd_hist
        pieces.append(group)
    if not pieces:
        return bars.assign(ma5=np.nan, ma20=np.nan, atr14=np.nan, rsi14=np.nan, macd_hist=np.nan)
    return pd.concat(pieces, ignore_index=True)


def _merge_closed_bars(con: sqlite3.Connection, observations: pd.DataFrame) -> pd.DataFrame:
    if observations.empty:
        return observations
    symbols = sorted(observations["symbol"].unique().tolist())
    placeholders = ",".join("?" for _ in symbols)
    # Four-hour MACD needs at least 34 prior bars.  Fourteen calendar days gives
    # 84 4H bars and a conservative warm-up for every timeframe.
    start = observations["obs_ts"].min() - pd.Timedelta(days=14)
    end = observations["obs_ts"].max()
    result = observations.copy()
    for timeframe in TIMEFRAMES:
        bars = pd.read_sql_query(
            f"""
            SELECT ts,symbol,o,h,l,c,v
            FROM kline_cache
            WHERE tf=? AND ts>=? AND ts<=? AND symbol IN ({placeholders})
            ORDER BY symbol,ts
            """,
            con,
            params=(timeframe, _iso(start), _iso(end), *symbols),
        )
        bars["bar_ts"] = pd.to_datetime(bars.pop("ts"), utc=True, errors="coerce")
        bars = bars.loc[
            bars[["o", "h", "l", "c", "v"]].notna().all(axis=1)
            & bars[["o", "h", "l", "c"]].gt(0).all(axis=1)
        ].copy()
        bars = _derive_indicators(bars)
        duration = pd.Timedelta(seconds=TF_SECONDS[timeframe])
        pieces: list[pd.DataFrame] = []
        by_symbol = {symbol: group.sort_values("bar_ts") for symbol, group in bars.groupby("symbol", sort=False)}
        for symbol, left in result[["obs_id", "symbol", "obs_ts"]].groupby("symbol", sort=False):
            right = by_symbol.get(symbol)
            left = left.sort_values("obs_ts").copy()
            left["closed_cutoff"] = left["obs_ts"] - duration
            if right is None or right.empty:
                merged = left.assign(**{field: np.nan for field in REQUIRED_BAR_FIELDS}, bar_ts=pd.NaT)
            else:
                merged = pd.merge_asof(
                    left,
                    right.drop(columns="symbol").sort_values("bar_ts"),
                    left_on="closed_cutoff",
                    right_on="bar_ts",
                    direction="backward",
                    allow_exact_matches=True,
                )
            keep = merged[["obs_id", "bar_ts", *REQUIRED_BAR_FIELDS]].copy()
            pieces.append(keep)
        merged_tf = pd.concat(pieces, ignore_index=True)
        rename = {"bar_ts": f"{timeframe}_bar_ts", **{field: f"{timeframe}_{field}" for field in REQUIRED_BAR_FIELDS}}
        merged_tf.rename(columns=rename, inplace=True)
        result = result.merge(merged_tf, on="obs_id", how="left", validate="one_to_one")
    return result


def _add_technical_features(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    tf_scores: list[np.ndarray] = []
    tf_signs: list[np.ndarray] = []
    for timeframe in TIMEFRAMES:
        prefix = f"{timeframe}_"
        close = out[prefix + "c"].astype(float)
        ma5 = out[prefix + "ma5"].astype(float)
        ma20 = out[prefix + "ma20"].astype(float)
        atr = out[prefix + "atr14"].astype(float)
        rsi = out[prefix + "rsi14"].astype(float)
        macd = out[prefix + "macd_hist"].astype(float)
        out[prefix + "close_vs_ma20"] = close / ma20 - 1.0
        out[prefix + "ma5_vs_ma20"] = ma5 / ma20 - 1.0
        out[prefix + "rsi_norm"] = (rsi - 50.0) / 50.0
        out[prefix + "macd_over_atr"] = macd / atr.replace(0, np.nan)
        out[prefix + "atr_pct"] = atr / close
        out[prefix + "bar_return"] = close / out[prefix + "o"].astype(float) - 1.0
        out[prefix + "range_pct"] = (out[prefix + "h"] - out[prefix + "l"]) / close
        rsi_vote = np.where(rsi >= 55, 1.0, np.where(rsi <= 45, -1.0, 0.0))
        score = (
            np.where(close > ma20, 1.0, -1.0)
            + np.where(ma5 > ma20, 1.0, -1.0)
            + np.where(macd > 0, 1.0, -1.0)
            + rsi_vote
        ) / 4.0
        tf_scores.append(score)
        tf_signs.append(np.sign(score))
    weighted = sum(TF_WEIGHTS[tf] * tf_scores[idx] for idx, tf in enumerate(TIMEFRAMES))
    weighted_sign = np.where(weighted >= 0.25, 1, np.where(weighted <= -0.25, -1, 0))
    signs = np.column_stack(tf_signs)
    out["alignment_score"] = weighted
    out["aligned_timeframes"] = (signs == weighted_sign[:, None]).sum(axis=1) * (weighted_sign != 0)
    out["rule_direction"] = np.where(
        (weighted_sign > 0) & (out["aligned_timeframes"] >= 2),
        "long",
        np.where((weighted_sign < 0) & (out["aligned_timeframes"] >= 2), "short", "wait"),
    )
    out["log_quote_volume_usd"] = np.log1p(out["quote_volume_usd"].clip(lower=0))
    out["log_oi_usd"] = np.log1p(out["oi_usd"].clip(lower=0))
    out["spread_bps"] = (out["ask"] - out["bid"]) / ((out["ask"] + out["bid"]) / 2.0) * 10000.0
    hour = out["obs_ts"].dt.hour + out["obs_ts"].dt.minute / 60.0
    out["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
    out["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
    return out


def _add_news_features(news_db: Path | None, frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    out = frame.copy()
    for name in (
        "news_events_6h_log1p",
        "news_events_24h_log1p",
        "official_okx_events_24h_log1p",
        "news_sentiment_24h_mean",
    ):
        out[name] = 0.0
    audit: dict[str, Any] = {
        "database_available": False,
        "events_loaded": 0,
        "events_matched_to_universe": 0,
        "zero_event_semantics": "valid_no_event_not_missing",
        "availability_clock": "first_seen_at_or_ingested_at_or_ts interpreted as CST when naive",
    }
    if news_db is None or not news_db.exists() or out.empty:
        return out, audit
    con = _ro(news_db)
    try:
        start_cst = (out["obs_ts"].min() - pd.Timedelta(hours=24)).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
        end_cst = out["obs_ts"].max().tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
        events = pd.read_sql_query(
            """
            SELECT COALESCE(first_seen_at,ingested_at,ts) available_at,
                   symbol,source,sentiment
            FROM news_items
            WHERE symbol IS NOT NULL AND trim(symbol)<>''
              AND datetime(COALESCE(first_seen_at,ingested_at,ts))>=datetime(?)
              AND datetime(COALESCE(first_seen_at,ingested_at,ts))<=datetime(?)
            """,
            con,
            params=(start_cst, end_cst),
        )
    finally:
        con.close()
    audit["database_available"] = True
    audit["events_loaded"] = int(len(events))
    if events.empty:
        return out, audit
    events["event_ts"] = events["available_at"].map(_parse_news_time)
    events["symbol"] = events["symbol"].map(_normalise_news_symbol)
    events = events.loc[events["event_ts"].notna() & events["symbol"].isin(out["symbol"].unique())].copy()
    audit["events_matched_to_universe"] = int(len(events))
    feature_rows: list[pd.DataFrame] = []
    event_groups = {symbol: group.sort_values("event_ts") for symbol, group in events.groupby("symbol", sort=False)}
    for symbol, observations in out[["obs_id", "symbol", "obs_ts"]].groupby("symbol", sort=False):
        observations = observations.sort_values("obs_ts").copy()
        group = event_groups.get(symbol)
        if group is None or group.empty:
            observations["news_events_6h_log1p"] = 0.0
            observations["news_events_24h_log1p"] = 0.0
            observations["official_okx_events_24h_log1p"] = 0.0
            observations["news_sentiment_24h_mean"] = 0.0
        else:
            event_ns = _datetime_ns(group["event_ts"])
            obs_ns = _datetime_ns(observations["obs_ts"])
            end_idx = np.searchsorted(event_ns, obs_ns, side="right")
            start6 = np.searchsorted(event_ns, obs_ns - int(pd.Timedelta(hours=6).value), side="left")
            start24 = np.searchsorted(event_ns, obs_ns - int(pd.Timedelta(hours=24).value), side="left")
            official = (group["source"].astype(str).to_numpy() == "okx_news").astype(float)
            sentiment = pd.to_numeric(group["sentiment"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
            official_prefix = np.concatenate([[0.0], np.cumsum(official)])
            sentiment_prefix = np.concatenate([[0.0], np.cumsum(sentiment)])
            count6 = end_idx - start6
            count24 = end_idx - start24
            official24 = official_prefix[end_idx] - official_prefix[start24]
            sentiment_sum = sentiment_prefix[end_idx] - sentiment_prefix[start24]
            observations["news_events_6h_log1p"] = np.log1p(count6)
            observations["news_events_24h_log1p"] = np.log1p(count24)
            observations["official_okx_events_24h_log1p"] = np.log1p(official24)
            observations["news_sentiment_24h_mean"] = np.divide(
                sentiment_sum,
                count24,
                out=np.zeros_like(sentiment_sum, dtype=float),
                where=count24 > 0,
            )
        feature_rows.append(observations.drop(columns=["symbol", "obs_ts"]))
    features = pd.concat(feature_rows, ignore_index=True)
    out = out.drop(columns=[name for name in features.columns if name != "obs_id"], errors="ignore").merge(
        features, on="obs_id", how="left", validate="one_to_one"
    )
    return out, audit


def _add_enrichment_features(
    con: sqlite3.Connection,
    frame: pd.DataFrame,
    max_delay_minutes: int = 10,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Attach only enrichment rows that were actually visible after each cycle.

    ``cycle_id`` is a Beijing-time scheduler identifier.  The feature rows must
    share that exact cycle and arrive no earlier than the market observation and
    no later than the configured delay.  The first row observed for a rerun is
    retained, which reconstructs the earliest information available to a live
    decision.  Positioning and official contract statistics are optional;
    book and trade-flow evidence are required.
    """
    out = frame.copy()
    audit: dict[str, Any] = {
        "enabled": True,
        "input_rows": int(len(out)),
        "max_delay_minutes": int(max_delay_minutes),
        "cycle_clock": "Asia/Shanghai exact YYYY-MM-DDTHH:MM",
        "availability_rule": "observation_ts <= collected_ts <= observation_ts + max_delay",
        "required_families": ["market_microstructure", "market_trade_flow"],
        "optional_families": [
            "market_positioning", "market_contract_statistics"],
    }
    if out.empty:
        out["enrichment_ready"] = pd.Series(dtype=bool)
        out["decision_ts"] = pd.Series(dtype="datetime64[ns, UTC]")
        audit.update({
            "microstructure_rows_loaded": 0,
            "trade_flow_rows_loaded": 0,
            "positioning_rows_loaded": 0,
            "contract_statistics_rows_loaded": 0,
            "microstructure_available_rows": 0,
            "trade_flow_available_rows": 0,
            "positioning_available_rows": 0,
            "contract_statistics_available_rows": 0,
            "enrichment_ready_rows": 0,
            "enrichment_ready_pct": 0.0,
        })
        return out, audit

    out["cycle_id"] = out["obs_ts"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%dT%H:%M")
    symbols = sorted(out["symbol"].unique().tolist())
    placeholders = ",".join("?" for _ in symbols)
    load_start = out["obs_ts"].min() - pd.Timedelta(minutes=1)
    # One scheduler cycle is at most an hour.  Loading the whole following hour
    # lets the audit distinguish late rows from genuinely missing rows.
    load_end = out["obs_ts"].max() + pd.Timedelta(hours=1)
    common_params = (_iso(load_start), _iso(load_end), *symbols)
    micro = pd.read_sql_query(
        f"""
        SELECT ts AS micro_available_at,cycle_id,symbol,
               spread_bps AS book_spread_bps,
               bid_depth_10bp_usd,ask_depth_10bp_usd,
               bid_depth_25bp_usd,ask_depth_25bp_usd,
               bid_depth_50bp_usd,ask_depth_50bp_usd,
               imbalance_10bp,imbalance_25bp,imbalance_50bp,
               buy_slippage_100usd_bps,sell_slippage_100usd_bps,
               buy_slippage_500usd_bps,sell_slippage_500usd_bps,
               buy_slippage_1000usd_bps,sell_slippage_1000usd_bps
        FROM market_microstructure
        WHERE ts>=? AND ts<? AND symbol IN ({placeholders})
        """,
        con,
        params=common_params,
    )
    flow = pd.read_sql_query(
        f"""
        SELECT ts AS flow_available_at,cycle_id,symbol,
               sample_count,sample_span_ms,buy_notional_usd,sell_notional_usd,
               taker_buy_ratio,cvd_notional_usd,largest_trade_usd
        FROM market_trade_flow
        WHERE ts>=? AND ts<? AND symbol IN ({placeholders})
        """,
        con,
        params=common_params,
    )
    positioning = pd.read_sql_query(
        f"""
        SELECT collected_ts AS positioning_available_at,cycle_id,symbol,
               long_ratio,short_ratio,long_short_ratio
        FROM market_positioning
        WHERE collected_ts>=? AND collected_ts<? AND timeframe='1H'
          AND symbol IN ({placeholders})
        """,
        con,
        params=common_params,
    )
    contract_columns = [
        "contract_stats_available_at", "cycle_id", "symbol",
        "contract_stats_source_ts", "contract_oi_contracts",
        "contract_oi_ccy", "contract_oi_usd", "contract_taker_sell_usd",
        "contract_taker_buy_usd", "contract_taker_buy_ratio",
        "contract_oi_log_change_15m_raw",
        "contract_stats_method",
    ]
    if _table_exists(con, "market_contract_statistics"):
        contract_statistics = pd.read_sql_query(
            f"""
            SELECT collected_ts AS contract_stats_available_at,
                   cycle_id,symbol,ts AS contract_stats_source_ts,
                   oi_contracts AS contract_oi_contracts,
                   oi_ccy AS contract_oi_ccy,
                   oi_usd AS contract_oi_usd,
                   taker_sell_usd AS contract_taker_sell_usd,
                   taker_buy_usd AS contract_taker_buy_usd,
                   taker_buy_ratio AS contract_taker_buy_ratio,
                   raw AS contract_stats_raw
            FROM market_contract_statistics
            WHERE collected_ts>=? AND collected_ts<? AND timeframe='15m'
              AND source='okx_rest_contract_oi_taker_15m'
              AND symbol IN ({placeholders})
            """,
            con,
            params=(
                _iso(load_start - pd.Timedelta(hours=2)),
                _iso(load_end),
                *symbols,
            ),
        )
        contract_statistics["contract_stats_source_ts"] = pd.to_datetime(
            contract_statistics["contract_stats_source_ts"],
            utc=True,
            errors="coerce",
        )
        contract_statistics["contract_stats_available_at"] = pd.to_datetime(
            contract_statistics["contract_stats_available_at"],
            utc=True,
            errors="coerce",
        )
        def _contract_method(value: Any) -> str:
            try:
                payload = json.loads(str(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                return "invalid_raw_json"
            if not isinstance(payload, dict):
                return "invalid_raw_json"
            return str(payload.get("method") or "rubik_common_bucket")

        contract_statistics["contract_stats_method"] = (
            contract_statistics["contract_stats_raw"].map(_contract_method)
        )
        contract_carry_rows = contract_statistics[
            "contract_stats_method"
        ].eq("official_previous_batch_carry_forward")
        audit["contract_statistics_carry_rows_excluded"] = int(
            contract_carry_rows.sum())
        contract_statistics = contract_statistics.loc[
            ~contract_carry_rows].copy()
        contract_statistics.sort_values(
            ["symbol", "contract_stats_source_ts", "contract_stats_available_at"],
            inplace=True,
        )
        oi_log = np.log1p(pd.to_numeric(
            contract_statistics["contract_oi_usd"], errors="coerce"
        ).clip(lower=0))
        contract_statistics["contract_oi_log_change_15m_raw"] = (
            oi_log.groupby(contract_statistics["symbol"]).diff())
        contract_statistics = contract_statistics[contract_columns]
    else:
        contract_statistics = pd.DataFrame(columns=contract_columns)
        audit["contract_statistics_carry_rows_excluded"] = 0
    audit.update({
        "microstructure_rows_loaded": int(len(micro)),
        "trade_flow_rows_loaded": int(len(flow)),
        "positioning_rows_loaded": int(len(positioning)),
        "contract_statistics_rows_loaded": int(len(contract_statistics)),
    })

    sources = (
        (micro, "micro_available_at"),
        (flow, "flow_available_at"),
        (positioning, "positioning_available_at"),
        (contract_statistics, "contract_stats_available_at"),
    )
    for source, time_col in sources:
        source[time_col] = pd.to_datetime(source[time_col], utc=True, errors="coerce")
        source.sort_values(time_col, inplace=True)
        source.drop_duplicates(["cycle_id", "symbol"], keep="first", inplace=True)
        out = out.merge(source, on=["cycle_id", "symbol"], how="left", validate="many_to_one")

    deadline = out["obs_ts"] + pd.Timedelta(minutes=max_delay_minutes)
    micro_matched = out["micro_available_at"].notna()
    flow_matched = out["flow_available_at"].notna()
    positioning_matched = out["positioning_available_at"].notna()
    contract_statistics_matched = out[
        "contract_stats_available_at"].notna()
    micro_time_ok = micro_matched & out["micro_available_at"].ge(out["obs_ts"]) & out["micro_available_at"].le(deadline)
    flow_time_ok = flow_matched & out["flow_available_at"].ge(out["obs_ts"]) & out["flow_available_at"].le(deadline)
    positioning_time_ok = (
        positioning_matched
        & out["positioning_available_at"].ge(out["obs_ts"])
        & out["positioning_available_at"].le(deadline)
    )
    contract_statistics_time_ok = (
        contract_statistics_matched
        & out["contract_stats_available_at"].ge(out["obs_ts"])
        & out["contract_stats_available_at"].le(deadline)
    )
    contract_source_lag = (
        out["contract_stats_available_at"]
        - pd.to_datetime(
            out["contract_stats_source_ts"], utc=True, errors="coerce")
    ).dt.total_seconds()
    contract_source_time_ok = contract_source_lag.between(0, 5_400)
    micro_core = (
        out[["book_spread_bps", "bid_depth_25bp_usd", "ask_depth_25bp_usd", "imbalance_25bp"]]
        .apply(pd.to_numeric, errors="coerce")
        .notna()
        .all(axis=1)
        & pd.to_numeric(out["book_spread_bps"], errors="coerce").ge(0)
        & pd.to_numeric(out["bid_depth_25bp_usd"], errors="coerce").gt(0)
        & pd.to_numeric(out["ask_depth_25bp_usd"], errors="coerce").gt(0)
    )
    total_trade_notional = (
        pd.to_numeric(out["buy_notional_usd"], errors="coerce")
        + pd.to_numeric(out["sell_notional_usd"], errors="coerce")
    )
    flow_core = (
        out[["sample_count", "buy_notional_usd", "sell_notional_usd", "taker_buy_ratio", "cvd_notional_usd"]]
        .apply(pd.to_numeric, errors="coerce")
        .notna()
        .all(axis=1)
        & pd.to_numeric(out["sample_count"], errors="coerce").gt(0)
        & total_trade_notional.gt(0)
    )
    micro_valid = micro_time_ok & micro_core
    flow_valid = flow_time_ok & flow_core
    positioning_valid = (
        positioning_time_ok
        & out[["long_ratio", "short_ratio", "long_short_ratio"]]
        .apply(pd.to_numeric, errors="coerce")
        .notna()
        .all(axis=1)
        & pd.to_numeric(out["long_short_ratio"], errors="coerce").gt(0)
    )
    contract_numeric = out[[
        "contract_oi_contracts", "contract_oi_ccy", "contract_oi_usd",
        "contract_taker_sell_usd", "contract_taker_buy_usd",
        "contract_taker_buy_ratio",
    ]].apply(pd.to_numeric, errors="coerce")
    contract_total_taker = (
        contract_numeric["contract_taker_sell_usd"]
        + contract_numeric["contract_taker_buy_usd"]
    )
    contract_ratio_expected = (
        contract_numeric["contract_taker_buy_usd"]
        / contract_total_taker.replace(0, np.nan)
    )
    contract_core = (
        contract_numeric.notna().all(axis=1)
        & contract_numeric.drop(
            columns=["contract_taker_buy_ratio"]).ge(0).all(axis=1)
        & contract_numeric["contract_taker_buy_ratio"].between(0, 1)
        & (
            contract_numeric["contract_taker_buy_ratio"]
            - contract_ratio_expected
        ).abs().le(1e-12)
    )
    contract_statistics_valid = (
        contract_statistics_time_ok
        & contract_source_time_ok
        & contract_core
    )
    out["enrichment_ready"] = micro_valid & flow_valid

    for depth in (10, 25, 50):
        for side in ("bid", "ask"):
            raw = pd.to_numeric(out[f"{side}_depth_{depth}bp_usd"], errors="coerce").where(micro_valid)
            out[f"log_{side}_depth_{depth}bp_usd"] = np.log1p(raw.clip(lower=0))
        out[f"imbalance_{depth}bp"] = pd.to_numeric(
            out[f"imbalance_{depth}bp"], errors="coerce"
        ).where(micro_valid)
    out["book_spread_bps"] = pd.to_numeric(out["book_spread_bps"], errors="coerce").where(micro_valid)
    for notional in (100, 500, 1000):
        buy = pd.to_numeric(out[f"buy_slippage_{notional}usd_bps"], errors="coerce").where(micro_valid)
        sell = pd.to_numeric(out[f"sell_slippage_{notional}usd_bps"], errors="coerce").where(micro_valid)
        out[f"slippage_mean_{notional}usd_bps"] = (buy + sell) / 2.0
        out[f"slippage_asymmetry_{notional}usd_bps"] = sell - buy

    sample_count = pd.to_numeric(out["sample_count"], errors="coerce").where(flow_valid)
    sample_span_seconds = pd.to_numeric(out["sample_span_ms"], errors="coerce").where(flow_valid) / 1000.0
    valid_total = total_trade_notional.where(flow_valid)
    out["trade_sample_count_log1p"] = np.log1p(sample_count.clip(lower=0))
    out["trade_sample_span_seconds_log1p"] = np.log1p(sample_span_seconds.clip(lower=0))
    out["log_total_trade_notional_usd"] = np.log1p(valid_total.clip(lower=0))
    out["taker_buy_centered"] = (
        pd.to_numeric(out["taker_buy_ratio"], errors="coerce").where(flow_valid) - 0.5
    )
    out["cvd_share"] = (
        pd.to_numeric(out["cvd_notional_usd"], errors="coerce").where(flow_valid)
        / valid_total.replace(0, np.nan)
    ).clip(-1.0, 1.0)
    out["largest_trade_share"] = (
        pd.to_numeric(out["largest_trade_usd"], errors="coerce").where(flow_valid)
        / valid_total.replace(0, np.nan)
    ).clip(lower=0.0, upper=1.0)

    out["positioning_available"] = positioning_valid.astype(float)
    out["positioning_long_short_log"] = np.log(
        pd.to_numeric(out["long_short_ratio"], errors="coerce").where(positioning_valid).clip(lower=1e-6)
    )
    out["positioning_long_minus_short"] = (
        pd.to_numeric(out["long_ratio"], errors="coerce")
        - pd.to_numeric(out["short_ratio"], errors="coerce")
    ).where(positioning_valid)

    out["contract_stats_available"] = contract_statistics_valid.astype(float)
    valid_contract_oi_usd = contract_numeric[
        "contract_oi_usd"].where(contract_statistics_valid)
    valid_contract_taker_total = contract_total_taker.where(
        contract_statistics_valid)
    out["contract_oi_log_usd"] = np.log1p(
        valid_contract_oi_usd.clip(lower=0))
    out["contract_oi_log_change_15m"] = pd.to_numeric(
        out["contract_oi_log_change_15m_raw"], errors="coerce"
    ).where(contract_statistics_valid)
    out["contract_taker_total_log_usd"] = np.log1p(
        valid_contract_taker_total.clip(lower=0))
    out["contract_taker_buy_centered"] = (
        contract_numeric["contract_taker_buy_ratio"].where(
            contract_statistics_valid) - 0.5
    )
    out["contract_oi_taker_interaction"] = (
        out["contract_oi_log_change_15m"]
        * out["contract_taker_buy_centered"]
    )

    availability = pd.DataFrame({
        "observation": out["obs_ts"],
        "microstructure": out["micro_available_at"].where(micro_valid),
        "trade_flow": out["flow_available_at"].where(flow_valid),
        "positioning": out["positioning_available_at"].where(positioning_valid),
        "contract_statistics": out["contract_stats_available_at"].where(
            contract_statistics_valid),
    })
    out["decision_ts"] = availability.max(axis=1)
    ready_delay = (out.loc[out["enrichment_ready"], "decision_ts"] - out.loc[out["enrichment_ready"], "obs_ts"]).dt.total_seconds()
    audit.update({
        "microstructure_matched_rows": int(micro_matched.sum()),
        "trade_flow_matched_rows": int(flow_matched.sum()),
        "positioning_matched_rows": int(positioning_matched.sum()),
        "contract_statistics_matched_rows": int(
            contract_statistics_matched.sum()),
        "microstructure_late_or_early_rows": int((micro_matched & ~micro_time_ok).sum()),
        "trade_flow_late_or_early_rows": int((flow_matched & ~flow_time_ok).sum()),
        "positioning_late_or_early_rows": int((positioning_matched & ~positioning_time_ok).sum()),
        "contract_statistics_late_or_early_rows": int((
            contract_statistics_matched & ~contract_statistics_time_ok).sum()),
        "contract_statistics_stale_source_rows": int((
            contract_statistics_matched & ~contract_source_time_ok).sum()),
        "microstructure_available_rows": int(micro_valid.sum()),
        "trade_flow_available_rows": int(flow_valid.sum()),
        "positioning_available_rows": int(positioning_valid.sum()),
        "contract_statistics_available_rows": int(
            contract_statistics_valid.sum()),
        "enrichment_ready_rows": int(out["enrichment_ready"].sum()),
        "enrichment_ready_pct": round(100.0 * float(out["enrichment_ready"].mean()), 3),
        "maximum_ready_decision_delay_seconds": float(ready_delay.max()) if not ready_delay.empty else None,
    })
    return out, audit


def _add_forward_labels(
    con: sqlite3.Connection,
    frame: pd.DataFrame,
    cost_bps: float,
    decision_time_col: str = "obs_ts",
    price_mode: str = "last",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if price_mode not in {"last", "executable"}:
        raise ValueError("price_mode must be 'last' or 'executable'")
    if frame.empty:
        return frame, {
            "entry_labeled_rows": 0,
            "entry_anchor": decision_time_col,
            "price_mode": price_mode,
        }
    if decision_time_col not in frame.columns:
        raise ValueError(f"missing decision time column: {decision_time_col}")
    symbols = sorted(frame["symbol"].unique().tolist())
    placeholders = ",".join("?" for _ in symbols)
    start = frame[decision_time_col].min()
    end = frame[decision_time_col].max() + pd.Timedelta(hours=5)
    ticks = pd.read_sql_query(
        f"""
        SELECT ts,symbol,last,bid,ask FROM tick_snapshots
        WHERE ts>=? AND ts<=? AND symbol IN ({placeholders})
        ORDER BY symbol,ts
        """,
        con,
        params=(_iso(start), _iso(end), *symbols),
    )
    ticks["tick_ts"] = pd.to_datetime(ticks.pop("ts"), utc=True, errors="coerce")
    out = frame.copy()
    cost_return = cost_bps / 10000.0
    label_rows: list[pd.DataFrame] = []
    tick_groups = {symbol: group.sort_values("tick_ts") for symbol, group in ticks.groupby("symbol", sort=False)}
    for symbol, observations in out[["obs_id", "symbol", decision_time_col]].groupby("symbol", sort=False):
        observations = observations.sort_values(decision_time_col).copy()
        series = tick_groups.get(symbol)
        if series is None or series.empty:
            continue
        tick_ns = _datetime_ns(series["tick_ts"])
        last_prices = pd.to_numeric(
            series["last"], errors="coerce").to_numpy(dtype=float)
        bid_prices = pd.to_numeric(
            series["bid"], errors="coerce").to_numpy(dtype=float)
        ask_prices = pd.to_numeric(
            series["ask"], errors="coerce").to_numpy(dtype=float)
        decision_ns = _datetime_ns(observations[decision_time_col])
        entry_idx = np.searchsorted(tick_ns, decision_ns, side="right")
        valid_entry = entry_idx < len(tick_ns)
        entry_idx_safe = np.minimum(entry_idx, len(tick_ns) - 1)
        entry_ns = tick_ns[entry_idx_safe]
        entry_last = last_prices[entry_idx_safe]
        entry_bid = bid_prices[entry_idx_safe]
        entry_ask = ask_prices[entry_idx_safe]
        valid_entry &= (
            entry_ns - decision_ns
        ) <= int(pd.Timedelta(minutes=20).value)
        valid_entry &= np.isfinite(entry_last) & (entry_last > 0)
        if price_mode == "executable":
            valid_entry &= (
                np.isfinite(entry_bid) & np.isfinite(entry_ask)
                & (entry_bid > 0) & (entry_ask > 0)
                & (entry_ask >= entry_bid)
            )
        labels = observations[["obs_id"]].copy()
        labels["entry_ts"] = pd.to_datetime(
            np.where(valid_entry, entry_ns, np.iinfo(np.int64).min),
            unit="ns",
            utc=True,
            errors="coerce",
        )
        labels["entry_price"] = np.where(valid_entry, entry_last, np.nan)
        labels["entry_last"] = np.where(valid_entry, entry_last, np.nan)
        labels["entry_bid"] = np.where(valid_entry, entry_bid, np.nan)
        labels["entry_ask"] = np.where(valid_entry, entry_ask, np.nan)
        for timeframe in TIMEFRAMES:
            target_ns = entry_ns + int(pd.Timedelta(seconds=TF_SECONDS[timeframe]).value)
            exit_idx = np.searchsorted(tick_ns, target_ns, side="left")
            valid_exit = valid_entry & (exit_idx < len(tick_ns))
            exit_idx_safe = np.minimum(exit_idx, len(tick_ns) - 1)
            exit_ns = tick_ns[exit_idx_safe]
            exit_last = last_prices[exit_idx_safe]
            exit_bid = bid_prices[exit_idx_safe]
            exit_ask = ask_prices[exit_idx_safe]
            valid_exit &= (exit_ns - target_ns) <= int(pd.Timedelta(minutes=20).value)
            valid_exit &= np.isfinite(exit_last) & (exit_last > 0)
            if price_mode == "executable":
                valid_exit &= (
                    np.isfinite(exit_bid) & np.isfinite(exit_ask)
                    & (exit_bid > 0) & (exit_ask > 0)
                    & (exit_ask >= exit_bid)
                )
            last_return = np.where(
                valid_exit, exit_last / entry_last - 1.0, np.nan)
            labels[f"{timeframe}_exit_ts"] = pd.to_datetime(
                np.where(valid_exit, exit_ns, np.iinfo(np.int64).min),
                unit="ns",
                utc=True,
                errors="coerce",
            )
            labels[f"{timeframe}_last_return"] = last_return
            if price_mode == "executable":
                long_directional = np.where(
                    valid_exit, exit_bid / entry_ask - 1.0, np.nan)
                short_directional = np.where(
                    valid_exit, 1.0 - exit_ask / entry_bid, np.nan)
                # Keep the legacy signed market-return column for diagnostics.
                # Candidate-specific executable returns are the label authority.
                labels[f"{timeframe}_return"] = last_return
                labels[f"{timeframe}_long_return"] = long_directional
                labels[f"{timeframe}_short_return"] = short_directional
                labels[f"{timeframe}_long_success"] = np.where(
                    np.isfinite(long_directional),
                    long_directional > cost_return, np.nan)
                labels[f"{timeframe}_short_success"] = np.where(
                    np.isfinite(short_directional),
                    short_directional > cost_return, np.nan)
            else:
                labels[f"{timeframe}_return"] = last_return
                labels[f"{timeframe}_long_return"] = last_return
                labels[f"{timeframe}_short_return"] = -last_return
                labels[f"{timeframe}_long_success"] = np.where(
                    np.isfinite(last_return), last_return > cost_return, np.nan)
                labels[f"{timeframe}_short_success"] = np.where(
                    np.isfinite(last_return), last_return < -cost_return, np.nan)
        label_rows.append(labels)
    labels = pd.concat(label_rows, ignore_index=True) if label_rows else pd.DataFrame(columns=["obs_id"])
    out = out.merge(labels, on="obs_id", how="left", validate="one_to_one")
    return out, {
        "entry_labeled_rows": int(out["entry_price"].notna().sum()),
        "entry_anchor": decision_time_col,
        "price_mode": price_mode,
        "execution_price_contract": (
            "long ask->bid; short bid->ask; no last fallback"
            if price_mode == "executable"
            else "last->last diagnostic"
        ),
    }


def _split_masks(frame: pd.DataFrame) -> tuple[dict[str, pd.Series], dict[str, str]]:
    last_day_end = frame["obs_ts"].max().floor("D") + pd.Timedelta(days=1)
    test_start = last_day_end - pd.Timedelta(days=7)
    calibration_start = test_start - pd.Timedelta(days=6)
    purge = pd.Timedelta(hours=4)
    masks = {
        "train": frame["obs_ts"] < calibration_start - purge,
        "calibration": (frame["obs_ts"] >= calibration_start) & (frame["obs_ts"] < test_start - purge),
        "test": frame["obs_ts"] >= test_start,
    }
    contract = {
        "train_end_exclusive": _iso(calibration_start - purge),
        "calibration_start": _iso(calibration_start),
        "calibration_end_exclusive": _iso(test_start - purge),
        "test_start": _iso(test_start),
        "test_end_observed": _iso(frame["obs_ts"].max()),
        "purge_hours": "4",
    }
    return masks, contract


def _selection_metrics(frame: pd.DataFrame, threshold: float) -> dict[str, Any]:
    selected = frame.loc[frame["probability"] >= threshold]
    n = len(selected)
    successes = int(selected["success"].sum()) if n else 0
    low, high = _wilson(successes, n)
    return {
        "threshold": float(threshold),
        "n": int(n),
        "successes": successes,
        "precision": float(successes / n) if n else None,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "ece": _ece(selected["probability"].to_numpy(dtype=float), selected["success"].to_numpy(dtype=float)) if n else None,
        "distinct_cycles": int(selected["obs_ts"].nunique()) if n else 0,
        "distinct_days": int(selected["obs_ts"].dt.floor("D").nunique()) if n else 0,
        "mean_probability": float(selected["probability"].mean()) if n else None,
        "mean_signed_return_after_cost": float(selected["signed_return_after_cost"].mean()) if n else None,
        "long_n": int((selected["side"] == "long").sum()) if n else 0,
        "short_n": int((selected["side"] == "short").sum()) if n else 0,
        "horizon_counts": {str(k): int(v) for k, v in selected["horizon"].value_counts().sort_index().items()} if n else {},
    }


def _choose_threshold(calibration: pd.DataFrame, min_n: int = 100) -> tuple[float, dict[str, Any]]:
    if len(calibration) < min_n:
        threshold = float(calibration["probability"].min()) if len(calibration) else 1.0
        metrics = _selection_metrics(calibration, threshold)
        metrics["selection_status"] = "insufficient_calibration_rows"
        return threshold, metrics
    probabilities = calibration["probability"].to_numpy(dtype=float)
    quantiles = np.linspace(0.50, 0.995, 200)
    candidates = sorted(set(float(x) for x in np.quantile(probabilities, quantiles)))
    evaluations = [_selection_metrics(calibration, threshold) for threshold in candidates]
    eligible = [item for item in evaluations if item["n"] >= min_n and item["distinct_days"] >= 4 and item["distinct_cycles"] >= 24]
    target = [item for item in eligible if (item["precision"] or 0.0) >= 0.90]
    if target:
        chosen = max(target, key=lambda item: (item["n"], item["precision"], item["threshold"]))
        status = "target_reached_on_calibration"
    elif eligible:
        chosen = max(eligible, key=lambda item: (item["precision"], item["n"], item["threshold"]))
        status = "target_not_reached_on_calibration"
    else:
        chosen = max(evaluations, key=lambda item: (item["n"] >= min_n, item["precision"] or 0.0, item["n"]))
        status = "calibration_diversity_gate_not_reached"
    chosen = dict(chosen)
    chosen["selection_status"] = status
    return float(chosen["threshold"]), chosen


def _best_per_observation(
    predictions: Iterable[pd.DataFrame],
    *,
    required_candidates: int = len(TIMEFRAMES) * 2,
) -> pd.DataFrame:
    combined = pd.concat(list(predictions), ignore_index=True)
    counts = combined.groupby("obs_id")["obs_id"].transform("size")
    combined = combined.loc[counts == required_candidates].copy()
    combined.sort_values(
        ["obs_id", "probability", "horizon", "side"],
        ascending=[True, False, True, True],
        inplace=True,
    )
    return combined.drop_duplicates("obs_id", keep="first").reset_index(drop=True)


def _research_panel(
    frame: pd.DataFrame,
    masks: dict[str, pd.Series],
    continuous_features: Iterable[str],
) -> pd.DataFrame:
    """Build an observation-grain point-in-time panel for later research.

    Labels and future returns remain explicit columns, separate from the named
    feature list.  Immature outcomes are retained as nulls so downstream
    diagnostics cannot silently change their denominator through right
    censoring.
    """
    metadata = [
        "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol",
        "asset_class", "rule_direction",
    ]
    features = list(dict.fromkeys(continuous_features))
    outcomes = [
        name
        for timeframe in TIMEFRAMES
        for name in (
            f"{timeframe}_return",
            f"{timeframe}_long_return",
            f"{timeframe}_short_return",
            f"{timeframe}_long_success",
            f"{timeframe}_short_success",
        )
        if name in frame.columns
    ]
    required = [*metadata, *features, *outcomes]
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError(
            "research panel missing columns: " + ",".join(missing))
    panel = frame[required].copy()
    if panel["obs_id"].duplicated().any():
        raise ValueError("research panel obs_id must be unique")
    panel["split"] = "purged"
    for name, mask in masks.items():
        if len(mask) != len(panel):
            raise ValueError(f"split mask length mismatch: {name}")
        panel.loc[mask.to_numpy(dtype=bool), "split"] = name
    return panel


def _rule_baseline(
    frame: pd.DataFrame,
    split_mask: pd.Series,
    cost_bps: float,
) -> dict[str, Any]:
    subset = frame.loc[split_mask & frame["rule_direction"].isin(["long", "short"])].copy()
    output: dict[str, Any] = {}
    cost = cost_bps / 10000.0
    for horizon in TIMEFRAMES:
        long_column = f"{horizon}_long_return"
        short_column = f"{horizon}_short_return"
        if long_column in subset.columns and short_column in subset.columns:
            selected_return = np.where(
                subset["rule_direction"].eq("long"),
                pd.to_numeric(subset[long_column], errors="coerce"),
                pd.to_numeric(subset[short_column], errors="coerce"),
            )
            available = subset.assign(
                _selected_directional_return=selected_return).loc[
                    lambda item: item["_selected_directional_return"].notna()
                ].copy()
            signed = available["_selected_directional_return"].to_numpy(
                dtype=float)
        else:
            available = subset.loc[
                subset[f"{horizon}_return"].notna()].copy()
            signed = np.where(
                available["rule_direction"].eq("long"),
                available[f"{horizon}_return"],
                -available[f"{horizon}_return"],
            )
        successes = int((signed > cost).sum())
        n = len(available)
        low, high = _wilson(successes, n)
        output[horizon] = {
            "n": int(n),
            "precision_after_cost": float(successes / n) if n else None,
            "wilson_95_low": low,
            "wilson_95_high": high,
            "mean_signed_return_after_cost": float(np.mean(signed - cost)) if n else None,
            "distinct_cycles": int(available["obs_ts"].nunique()) if n else 0,
            "distinct_days": int(available["obs_ts"].dt.floor("D").nunique()) if n else 0,
        }
    return output


def run_calibration(
    *,
    market_db: Path,
    news_db: Path | None,
    output_dir: Path,
    start: str,
    end: str,
    sample_minutes: int = 60,
    cost_bps: float = 20.0,
    min_quote_volume_usd: float = 5_000_000.0,
    min_oi_usd: float = 5_000_000.0,
    feature_set: str = "base",
    label_price_mode: str = "last",
) -> dict[str, Any]:
    if feature_set not in {"base", "enhanced"}:
        raise ValueError("feature_set must be 'base' or 'enhanced'")
    con = _ro(market_db)
    try:
        observations, load_audit = _load_observations(
            con, start, end, sample_minutes, min_quote_volume_usd, min_oi_usd
        )
        with_bars = _merge_closed_bars(con, observations)
        required = [f"{tf}_{field}" for tf in TIMEFRAMES for field in REQUIRED_BAR_FIELDS]
        ready_mask = with_bars[required].notna().all(axis=1)
        analysis_ready_rows = int(ready_mask.sum())
        frame = with_bars.loc[ready_mask].copy().reset_index(drop=True)
        frame = _add_technical_features(frame)
        frame, news_audit = _add_news_features(news_db, frame)
        if feature_set == "enhanced":
            frame, enrichment_audit = _add_enrichment_features(con, frame)
            frame = frame.loc[frame["enrichment_ready"]].copy().reset_index(drop=True)
            continuous_features = (*CONTINUOUS_FEATURES, *ENRICHMENT_FEATURES)
        else:
            frame["decision_ts"] = frame["obs_ts"]
            enrichment_audit = {
                "enabled": False,
                "input_rows": int(len(frame)),
                "enrichment_ready_rows": int(len(frame)),
                "enrichment_ready_pct": 100.0,
                "decision_clock": "observation timestamp",
            }
            continuous_features = CONTINUOUS_FEATURES
        frame, label_audit = _add_forward_labels(
            con, frame, cost_bps, decision_time_col="decision_ts",
            price_mode=label_price_mode,
        )
    finally:
        con.close()

    if frame.empty:
        raise RuntimeError("no analysis-ready observations")
    masks, split_contract = _split_masks(frame)
    spec = fit_feature_spec(frame, masks["train"], continuous_features)
    x = transform_features(frame, spec)
    research_panel = _research_panel(frame, masks, continuous_features)

    model_parameters: dict[str, Any] = {
        "feature_spec": spec.to_dict(),
        "models": {},
        "feature_set": feature_set,
        "research_only": True,
        "label_price_mode": label_price_mode,
    }
    predictions_by_split: dict[str, list[pd.DataFrame]] = {"calibration": [], "test": []}
    diagnostic_context_columns = [
        name for name in (
            "rule_direction", "alignment_score", "aligned_timeframes",
            "asset_class", "chg24h", "funding_rate", "premium",
            "quote_volume_usd", "oi_usd", "news_events_6h",
            "news_events_24h", "official_okx_events_24h",
            "book_spread_bps", "taker_buy_centered", "cvd_share",
            "positioning_available", "positioning_long_short_log",
        )
        if name in frame.columns
    ]
    model_audit: dict[str, Any] = {}
    for horizon in TIMEFRAMES:
        for side in ("long", "short"):
            label_name = f"{horizon}_{side}_success"
            label_available = frame[label_name].notna()
            train_mask = masks["train"] & label_available
            calibration_mask = masks["calibration"] & label_available
            test_mask = masks["test"] & label_available
            y = frame[label_name].fillna(False).astype(float).to_numpy()
            weights = fit_ridge_logistic(x[train_mask], y[train_mask])
            raw_cal = _sigmoid(x[calibration_mask] @ weights)
            platt = fit_platt(raw_cal, y[calibration_mask])
            key = f"{horizon}_{side}"
            model_parameters["models"][key] = {
                "weights": [float(value) for value in weights],
                "platt_intercept": platt[0],
                "platt_slope": platt[1],
            }
            model_audit[key] = {
                "train_n": int(train_mask.sum()),
                "train_positive_rate": float(y[train_mask].mean()),
                "calibration_n": int(calibration_mask.sum()),
                "calibration_positive_rate": float(y[calibration_mask].mean()),
                "test_n": int(test_mask.sum()),
                "test_positive_rate": float(y[test_mask].mean()) if test_mask.any() else None,
            }
            for split, mask in (("calibration", calibration_mask), ("test", test_mask)):
                raw = _sigmoid(x[mask] @ weights)
                probability = apply_platt(raw, platt)
                return_column = (
                    f"{horizon}_{side}_return"
                    if label_price_mode == "executable"
                    else f"{horizon}_return"
                )
                selected = frame.loc[
                    mask,
                    [
                        "obs_id", "obs_ts", "decision_ts", "entry_ts",
                        "symbol", *diagnostic_context_columns,
                        return_column,
                    ],
                ].copy()
                selected.rename(
                    columns={return_column: "forward_return"}, inplace=True)
                selected["horizon"] = horizon
                selected["side"] = side
                selected["probability"] = probability
                selected["success"] = y[mask]
                direction = 1.0 if side == "long" else -1.0
                selected["signed_return_after_cost"] = (
                    selected["forward_return"] - cost_bps / 10000.0
                    if label_price_mode == "executable"
                    else direction * selected["forward_return"] - cost_bps / 10000.0
                )
                predictions_by_split[split].append(selected)

    calibration_candidates = pd.concat(
        predictions_by_split["calibration"], ignore_index=True)
    test_candidates = pd.concat(
        predictions_by_split["test"], ignore_index=True)
    calibration_best = _best_per_observation(
        predictions_by_split["calibration"])
    test_best = _best_per_observation(predictions_by_split["test"])
    threshold, calibration_selection = _choose_threshold(calibration_best, min_n=100)
    holdout = _selection_metrics(test_best, threshold)
    selected_holdout = test_best.loc[test_best["probability"] >= threshold].copy()

    requirements = {
        "calibration_target_reached": calibration_selection["selection_status"] == "target_reached_on_calibration",
        "holdout_n_at_least_100": holdout["n"] >= 100,
        "holdout_precision_at_least_90pct": (holdout["precision"] or 0.0) >= 0.90,
        "holdout_ece_at_most_5pp": holdout["ece"] is not None and holdout["ece"] <= 0.05,
        "holdout_distinct_days_at_least_5": holdout["distinct_days"] >= 5,
        "holdout_distinct_cycles_at_least_100": holdout["distinct_cycles"] >= 100,
    }
    offline_gate_pass = all(requirements.values())
    metrics: dict[str, Any] = {
        "schema_version": 2,
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "read_only_research",
        "feature_set": feature_set,
        "order_calls": 0,
        "production_database_writes": 0,
        "window": {"start": start, "end_exclusive": end, "sample_minutes": sample_minutes},
        "cost_hurdle_bps": cost_bps,
        "label_price_mode": label_price_mode,
        "liquidity_gate": {
            "min_quote_volume_usd": min_quote_volume_usd,
            "min_oi_usd": min_oi_usd,
            "quote_volume_formula": "last * contracts_24h * ctVal",
        },
        "point_in_time_contract": {
            "kline": "bar_start + timeframe_duration <= observation timestamp",
            "enrichment": (
                "exact Asia/Shanghai cycle; first collected row between observation and observation+10m"
                if feature_set == "enhanced"
                else "not used by base feature set"
            ),
            "decision": "latest availability time among required features and valid optional positioning",
            "entry": "first ticker snapshot strictly after decision timestamp, max delay 20m",
            "exit": "first ticker snapshot at/after entry+horizon, max delay 20m",
            "news": "first_seen_at else ingested_at else ts; naive timestamps interpreted as Asia/Shanghai",
            "label": (
                "long ask->bid or short bid->ask directional return must exceed 20bp; no last fallback"
                if label_price_mode == "executable"
                else "last-price directional return must exceed 20bp round-trip hurdle"
            ),
            "selection": "highest Platt-calibrated probability across 3 horizons x 2 directions per symbol-cycle",
        },
        "dataset": {
            **load_audit,
            "analysis_ready_rows": analysis_ready_rows,
            "analysis_ready_pct_of_liquidity_eligible": round(100.0 * analysis_ready_rows / max(1, load_audit["liquidity_eligible_rows"]), 3),
            **label_audit,
            "symbols": int(frame["symbol"].nunique()),
            "cycles": int(frame["obs_ts"].nunique()),
            "rows": int(len(frame)),
            "news": news_audit,
            "enrichment": enrichment_audit,
        },
        "split_contract": split_contract,
        "split_rows": {name: int(mask.sum()) for name, mask in masks.items()},
        "model_audit": model_audit,
        "prediction_exports": {
            "calibration_candidate_rows": int(len(calibration_candidates)),
            "calibration_best_rows": int(len(calibration_best)),
            "holdout_candidate_rows": int(len(test_candidates)),
            "holdout_best_rows": int(len(test_best)),
            "required_candidates_per_observation": len(TIMEFRAMES) * 2,
            "research_panel_rows": int(len(research_panel)),
            "research_panel_columns": int(len(research_panel.columns)),
            "right_censoring_rule": (
                "best-per-observation requires every 3-horizon x 2-side label"
            ),
            "purpose": "research-only policy diagnostics; not production evidence",
        },
        "calibration_selection": calibration_selection,
        "holdout": holdout,
        "offline_acceptance_requirements": requirements,
        "offline_gate_pass": offline_gate_pass,
        "credibility_90_status": "MET_OFFLINE_HOLDOUT_REQUIRES_LIVE_CONFIRMATION" if offline_gate_pass else "NOT_MET",
        "production_threshold_change_allowed": False,
        "production_change_block_reason": (
            "offline gate passed but independent forward live-shadow confirmation and risk approval are still required"
            if offline_gate_pass
            else "calibration and/or untouched holdout did not satisfy the 90% precision, sample, diversity, and calibration gates"
        ),
        "current_alignment_rule_test_baseline": _rule_baseline(frame, masks["test"], cost_bps),
        "limitations": [
            "observational backtest; no causal claim",
            "hourly sampling and cross-sectional symbols are not fully independent",
            "20bp is a conservative standardized hurdle, not a fill-by-fill fee/slippage reconstruction",
            "enhanced history starts when point-in-time microstructure and trade-flow collection became available",
            "offline success can only nominate a live-shadow candidate; it cannot authorize orders",
        ],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(output_dir / "calibration_metrics.json", metrics)
    _atomic_json(output_dir / "model_parameters.json", model_parameters)
    output_columns = [
        "obs_id", "obs_ts", "decision_ts", "entry_ts", "symbol", "horizon", "side", "probability",
        "success", "forward_return", "signed_return_after_cost",
        *diagnostic_context_columns,
    ]
    _atomic_csv(output_dir / "holdout_selected_signals.csv", selected_holdout[output_columns])
    candidate_columns = output_columns
    _atomic_csv(
        output_dir / "calibration_candidate_predictions.csv",
        calibration_candidates[candidate_columns].sort_values(
            ["obs_ts", "symbol", "horizon", "side"]),
    )
    _atomic_csv(
        output_dir / "calibration_best_predictions.csv",
        calibration_best[candidate_columns].sort_values(
            ["obs_ts", "symbol"]),
    )
    _atomic_csv(
        output_dir / "holdout_candidate_predictions.csv",
        test_candidates[candidate_columns].sort_values(
            ["obs_ts", "symbol", "horizon", "side"]),
    )
    _atomic_csv(
        output_dir / "holdout_best_predictions.csv",
        test_best[candidate_columns].sort_values(["obs_ts", "symbol"]),
    )
    _atomic_csv(
        output_dir / "research_panel.csv",
        research_panel.sort_values(["obs_ts", "symbol"]),
    )
    split_audit = frame[["obs_id", "obs_ts", "symbol", "rule_direction", "alignment_score", "aligned_timeframes"]].copy()
    split_audit["split"] = "purged"
    for name, mask in masks.items():
        split_audit.loc[mask, "split"] = name
    _atomic_csv(output_dir / "dataset_split_audit.csv", split_audit)
    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-db", type=Path, default=Path(r"./db/market.db"))
    parser.add_argument("--news-db", type=Path, default=Path(r"./db/news.db"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start", default="2026-07-12T00:00:00Z")
    parser.add_argument("--end", default="2026-08-11T15:01:00Z")
    parser.add_argument("--sample-minutes", type=int, default=60)
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--min-quote-volume-usd", type=float, default=5_000_000.0)
    parser.add_argument("--min-oi-usd", type=float, default=5_000_000.0)
    parser.add_argument("--feature-set", choices=("base", "enhanced"), default="base")
    parser.add_argument(
        "--label-price-mode", choices=("last", "executable"), default="last",
        help="research label price contract; executable uses long ask->bid and short bid->ask",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    metrics = run_calibration(
        market_db=args.market_db,
        news_db=args.news_db,
        output_dir=args.output_dir,
        start=args.start,
        end=args.end,
        sample_minutes=args.sample_minutes,
        cost_bps=args.cost_bps,
        min_quote_volume_usd=args.min_quote_volume_usd,
        min_oi_usd=args.min_oi_usd,
        feature_set=args.feature_set,
        label_price_mode=args.label_price_mode,
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
