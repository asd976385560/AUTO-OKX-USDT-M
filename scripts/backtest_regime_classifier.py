# -*- coding: utf-8 -*-
"""Walk-forward-ish regime 参数筛选：前 70% 选参，后 30% 独立报告。

只读 market.db/regime.db；用每根 BTC 4H 已收盘特征预测未来 24h 收益。输出中同时给出
旧 DXY-only 规则，便于判断新分类是否真正提供方向区分。
"""
from __future__ import annotations

import argparse
import bisect
import itertools
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean, median

from regime_classifier import classify_regime


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()


def _rows(db_root: Path) -> list[dict]:
    market = sqlite3.connect(f"file:{(db_root / 'market.db').as_posix()}?mode=ro", uri=True)
    market.row_factory = sqlite3.Row
    regime = sqlite3.connect(f"file:{(db_root / 'regime.db').as_posix()}?mode=ro", uri=True)
    regime.row_factory = sqlite3.Row
    try:
        bars = market.execute(
            "SELECT ts,c,ma5,ma20,rsi14 FROM kline_cache "
            "WHERE symbol='BTC-USDT-SWAP' AND tf='4H' ORDER BY ts"
        ).fetchall()
        macros = regime.execute(
            "SELECT ts,dxy_d1 FROM cross_market WHERE dxy_d1 IS NOT NULL ORDER BY ts"
        ).fetchall()
    finally:
        market.close()
        regime.close()

    macro_times = [_epoch(r["ts"]) for r in macros]
    out = []
    horizon = 6
    for i in range(horizon, len(bars) - horizon):
        row = bars[i]
        prev = bars[i - horizon]
        future = bars[i + horizon]
        if any(row[k] is None for k in ("c", "ma5", "ma20", "rsi14")) or not prev["c"]:
            continue
        at = _epoch(row["ts"])
        mi = bisect.bisect_right(macro_times, at) - 1
        dxy_d1 = macros[mi]["dxy_d1"] if mi >= 0 else None
        out.append({
            "ts": row["ts"],
            "close": row["c"],
            "ma5": row["ma5"],
            "ma20": row["ma20"],
            "rsi14": row["rsi14"],
            "return_24h": row["c"] / prev["c"] - 1.0,
            "dxy_d1": dxy_d1,
            "forward_24h": future["c"] / row["c"] - 1.0,
        })
    return out


def _old_label(r: dict) -> str:
    dxy = r["dxy_d1"]
    if dxy is None:
        return "range"
    if dxy > 0.005:
        return "trend_down"
    if dxy < -0.005:
        return "trend_up"
    return "range"


def _summary(rows: list[dict], labeler) -> dict:
    grouped = {"trend_up": [], "trend_down": [], "range": []}
    for row in rows:
        grouped[labeler(row)].append(row["forward_24h"])
    total = len(rows)
    by_regime = {}
    for label, vals in grouped.items():
        hits = (
            sum(v > 0 for v in vals) if label == "trend_up"
            else sum(v < 0 for v in vals) if label == "trend_down"
            else sum(abs(v) < 0.02 for v in vals)
        )
        by_regime[label] = {
            "n": len(vals),
            "share": len(vals) / total if total else 0,
            "mean_forward_24h": mean(vals) if vals else None,
            "median_forward_24h": median(vals) if vals else None,
            "hit_rate": hits / len(vals) if vals else None,
        }
    directional = grouped["trend_up"] + [-v for v in grouped["trend_down"]]
    directional_hit = sum(v > 0 for v in directional) / len(directional) if directional else 0
    up_mean = mean(grouped["trend_up"]) if grouped["trend_up"] else 0
    down_mean = mean(grouped["trend_down"]) if grouped["trend_down"] else 0
    counts = Counter(labeler(r) for r in rows)
    max_share = max(counts.values(), default=0) / total if total else 1
    return {
        "n": total,
        "by_regime": by_regime,
        "directional_hit_rate": directional_hit,
        "up_minus_down_bps": (up_mean - down_mean) * 10000,
        "max_class_share": max_share,
    }


def _objective(summary: dict) -> float:
    classes = summary["by_regime"]
    # 至少 8% 样本/类，避免靠极少数极端点制造漂亮均值。
    if any(v["share"] < 0.08 for v in classes.values()):
        return -1e9
    return (
        summary["up_minus_down_bps"]
        + 100.0 * (summary["directional_hit_rate"] - 0.5)
        - 100.0 * max(0.0, summary["max_class_share"] - 0.70)
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="BTC 4H regime 历史回测（只读）")
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--json-out")
    args = ap.parse_args()
    rows = _rows(Path(args.db_root))
    if len(rows) < 100:
        raise SystemExit(f"样本不足: {len(rows)}")
    split = int(len(rows) * 0.70)
    train, holdout = rows[:split], rows[split:]

    candidates = itertools.product(
        (0.003, 0.005, 0.0075),
        (0.002, 0.003, 0.005),
        (0.010, 0.015, 0.020),
        ((52.0, 48.0), (55.0, 45.0), (58.0, 42.0)),
        (0.002, 0.003, 0.005),
        (2, 3),
        (-1, 1),
    )
    ranked = []
    for price_t, spread_t, mom_t, rsi_pair, dxy_t, cutoff, orientation in candidates:
        params = {
            "price_ma_threshold": price_t,
            "ma_spread_threshold": spread_t,
            "momentum_threshold": mom_t,
            "rsi_upper": rsi_pair[0],
            "rsi_lower": rsi_pair[1],
            "dxy_threshold": dxy_t,
            "score_cutoff": cutoff,
            "btc_orientation": orientation,
        }

        def label(row, p=params):
            return classify_regime(
                close=row["close"], ma5=row["ma5"], ma20=row["ma20"],
                rsi14=row["rsi14"], return_24h=row["return_24h"],
                dxy_d1=row["dxy_d1"], params=p,
            )["regime"]

        s = _summary(train, label)
        ranked.append((_objective(s), params, s))
    ranked.sort(key=lambda x: x[0], reverse=True)
    best_score, best_params, train_summary = ranked[0]

    def best_label(row):
        return classify_regime(
            close=row["close"], ma5=row["ma5"], ma20=row["ma20"],
            rsi14=row["rsi14"], return_24h=row["return_24h"],
            dxy_d1=row["dxy_d1"], params=best_params,
        )["regime"]

    report = {
        "method": "BTC 4H multi-factor, predict forward 24h; first 70% select, last 30% holdout",
        "period": {"first": rows[0]["ts"], "last": rows[-1]["ts"]},
        "samples": len(rows),
        "split_index": split,
        "selected_params": best_params,
        "training_objective": best_score,
        "training": train_summary,
        "holdout": _summary(holdout, best_label),
        "old_dxy_only_holdout": _summary(holdout, _old_label),
        "top5_training": [
            {"objective": score, "params": params, "summary": summary}
            for score, params, summary in ranked[:5]
        ],
        "note": "回测是分类辨识力验证，不代表可交易收益；regime 保持观察输入，不作硬闸。",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
