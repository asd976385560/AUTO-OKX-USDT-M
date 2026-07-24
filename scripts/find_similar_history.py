# -*- coding: utf-8 -*-
"""v7.0e.3 历史相似度匹配工具

用法：
  pwsh ... find_similar_history.py --symbol BTC-USDT-SWAP --tf 1D --forward-bars 7
  pwsh ... find_similar_history.py --top-n 10

算法（简化 fingerprint + 余弦相似度）：
  1. 取当前 (symbol, tf) 最新 K 行的 [rsi14, macd_hist, ma5, ma20, c]
  2. 从 kline_cache 查所有历史 K 行的同 5 维向量
  3. 算余弦相似度，取 top N
  4. 对每个匹配：查其后 [forward_bars] 根 K 线，算累计 return / max drawdown / 是否触底反弹
  5. 汇总：命中率、平均收益、最大回撤

返回 JSON 给 P4 决策用：
  {
    "current": {"rsi14": 18.5, "macd_hist": -1516, ...},
    "matches": [
      {"ts": "2024-XX-XX", "similarity": 0.92, "next_7d_return": -0.05, ...},
      ...
    ],
    "summary": {"avg_return": -0.02, "median_return": -0.01, "hit_rate": 0.4, ...}
  }
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import sys, json, math, argparse
from pathlib import Path
sys.path.insert(0, _project_path('Lib', 'site-packages'))
sys.stdout.reconfigure(encoding='utf-8')

from _db_ro import connect_ro


def market_db_path(db_root: str | None = None) -> str:
    return str(Path(db_root or _project_path('db')) / 'market.db')


def vec_norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def cosine(a: list[float], b: list[float]) -> float:
    na, nb = vec_norm(a), vec_norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def feature_vec(row: tuple) -> list[float] | None:
    """从 (rsi14, macd_hist, ma5, ma20, c, v) 提 5 维特征向量。None=跳过。"""
    rsi, macd, ma5, ma20, c, v = row
    if rsi is None or ma5 is None or ma20 is None or c is None:
        return None
    # 归一化：rsi 0-100 直接用；macd / c 是相对量；ma5/ma20 / c 是偏离度
    return [
        rsi / 100.0,                              # 0-1
        math.tanh(macd / max(abs(c) * 0.01, 1)) if macd is not None else 0.0,  # -1 to 1
        (ma5 / ma20 - 1.0) * 10 if ma20 else 0.0,  # 偏离度（0=持平）
        (c / ma20 - 1.0) * 10 if ma20 else 0.0,    # 价格 vs MA20
        math.log10(v + 1) / 10 if v else 0.0,      # 成交量对数
    ]


def find_similar(symbol: str, tf: str, top_n: int, forward_bars: int,
                 min_history_bars: int = 5, db_root: str | None = None) -> dict:
    con = connect_ro(market_db_path(db_root), timeout=30)  # 只读 mode=ro（2026-07-03）
    cur = con.cursor()
    # 1) 取当前最新行
    latest = cur.execute(
        "SELECT ts, o, h, l, c, v, rsi14, macd_hist, ma5, ma20 "
        "FROM kline_cache WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT 1",
        (symbol, tf),
    ).fetchone()
    if not latest:
        con.close()
        return {"error": f"no data for {symbol} {tf}"}
    cur_ts, o, h, l, c, v, rsi, macd, ma5, ma20 = latest
    cur_vec = feature_vec((rsi, macd, ma5, ma20, c, v))
    if cur_vec is None:
        con.close()
        return {"error": f"current row missing indicators for {symbol} {tf}"}
    # 2) 查所有历史行（跳过最近 min_history_bars 根避免和当前重叠）
    all_rows = cur.execute(
        "SELECT ts, c, v, rsi14, macd_hist, ma5, ma20 "
        "FROM kline_cache WHERE symbol=? AND tf=? AND rsi14 IS NOT NULL AND ma5 IS NOT NULL AND ma20 IS NOT NULL "
        "ORDER BY ts ASC",
        (symbol, tf),
    ).fetchall()
    if len(all_rows) < min_history_bars + forward_bars + 10:
        con.close()
        return {"error": f"insufficient history: {len(all_rows)} rows"}
    # 3) 算相似度（对每行）
    sims: list[tuple[float, str, float]] = []
    for row in all_rows:
        ts, c2, v2, r2, m2, ma5_2, ma20_2 = row
        vec = feature_vec((r2, m2, ma5_2, ma20_2, c2, v2))
        if vec is None:
            continue
        s = cosine(cur_vec, vec)
        sims.append((s, ts, c2))
    sims.sort(reverse=True)
    # 4) 对 top N 匹配，算后 forward_bars 根的 return
    ts_to_idx = {row[0]: i for i, row in enumerate(all_rows)}
    matches = []
    for sim, ts, c_match in sims[:top_n]:
        if ts not in ts_to_idx:
            continue
        idx = ts_to_idx[ts]
        # 拿后续 forward_bars 根
        forward = all_rows[idx + 1: idx + 1 + forward_bars]
        if len(forward) < forward_bars:
            continue
        c_start = c_match
        c_end = forward[-1][1]  # close of last forward bar
        cum_return = (c_end - c_start) / c_start if c_start else 0
        # max drawdown
        peak = c_start
        max_dd = 0.0
        for f in forward:
            if f[1] > peak:
                peak = f[1]
            dd = (peak - f[1]) / peak if peak else 0
            if dd > max_dd:
                max_dd = dd
        # max bounce
        trough = c_start
        max_bounce = 0.0
        for f in forward:
            if f[1] < trough:
                trough = f[1]
            bounce = (f[1] - trough) / trough if trough else 0
            if bounce > max_bounce:
                max_bounce = bounce
        matches.append({
            "ts": ts,
            "c_at_match": c_match,
            "similarity": round(sim, 4),
            "forward_bars": forward_bars,
            "cum_return": round(cum_return, 4),
            "max_drawdown": round(max_dd, 4),
            "max_bounce": round(max_bounce, 4),
        })
    # 5) 汇总
    if matches:
        avg_ret = sum(m["cum_return"] for m in matches) / len(matches)
        med_ret = sorted([m["cum_return"] for m in matches])[len(matches) // 2]
        hit_rate_pos = sum(1 for m in matches if m["cum_return"] > 0) / len(matches)
        avg_dd = sum(m["max_drawdown"] for m in matches) / len(matches)
        avg_bounce = sum(m["max_bounce"] for m in matches) / len(matches)
    else:
        avg_ret = med_ret = hit_rate_pos = avg_dd = avg_bounce = 0
    con.close()
    return {
        "current": {
            "ts": cur_ts,
            "c": c, "rsi14": rsi, "macd_hist": macd, "ma5": ma5, "ma20": ma20, "v": v,
        },
        "query": {"symbol": symbol, "tf": tf, "top_n": top_n, "forward_bars": forward_bars},
        "history_rows_scanned": len(all_rows),
        "matches": matches,
        "summary": {
            "n_matches": len(matches),
            "avg_return": round(avg_ret, 4),
            "median_return": round(med_ret, 4),
            "hit_rate_positive": round(hit_rate_pos, 4),
            "avg_max_drawdown": round(avg_dd, 4),
            "avg_max_bounce": round(avg_bounce, 4),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC-USDT-SWAP")
    ap.add_argument("--tf", default="1D")
    ap.add_argument("--top-n", "--top", dest="top_n", type=int, default=10)
    ap.add_argument("--forward-bars", type=int, default=7)
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()
    result = find_similar(args.symbol, args.tf, args.top_n, args.forward_bars, db_root=args.db_root)
    if args.pretty:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
