# -*- coding: utf-8 -*-
"""离线回测引擎 v1 试点（T10，2026-06-12）。

用 kline_cache 已落库 K 线 + 预计算指标（ma5/ma20/atr14/rsi14/macd_hist）离线验证
playbook 规则——把"等实盘 n≥10"的周/月级验证提速到分钟级。

规则 JSON（scripts/backtest_rules/*.json）:
  {"name":"...", "direction":"long|short",
   "entry":{"all":["macd_hist < 0","c < ma20"]},      # 全部满足才入场
   "exit":{"any":["macd_hist > 0","bars_held >= 12"]}, # 任一满足即离场
   "stop_atr":2.0, "tp_atr":3.0}                       # SL/TP = entry ∓/± n*atr14
条件文法（安全解析，无 eval）: "<字段|数字> <op> <字段|数字>"，op∈{<,>,<=,>=}
字段: o h l c v ma5 ma20 atr14 rsi14 macd_hist bars_held

成本: 单边 fee 0.05% + 滑点 0.02%（开平各一次）。
结果只打印/可注记 playbook（--annotate-playbook ID --apply 把回测战绩追加进 evidence，
**绝不写 win_count/loss_count 等实盘统计列**——实盘统计只属于 update_playbook_stats）。

用法:
  ... run_okx_python.ps1 scripts/backtest_engine.py --rule scripts/backtest_rules/trend_down_short_v1.json
      [--symbols top:22|BTC-USDT-SWAP,ETH-USDT-SWAP] [--tf 4H] [--workers 8]
      [--annotate-playbook 354 --apply]
"""
import argparse
import json
import re
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

FIELDS = {"o", "h", "l", "c", "v", "ma5", "ma20", "atr14", "rsi14", "macd_hist", "bars_held"}
OPS = {"<": lambda a, b: a < b, ">": lambda a, b: a > b,
       "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}
COST_PCT = (0.05 + 0.02) * 2 / 100  # 开+平 各 fee+滑点


def parse_cond(s):
    m = re.fullmatch(r"\s*([\w.]+)\s*(<=|>=|<|>)\s*([\w.+-]+)\s*", s)
    if not m:
        raise ValueError(f"非法条件: {s!r}")
    lhs, op, rhs = m.groups()
    if lhs not in FIELDS:
        raise ValueError(f"未知字段: {lhs}")
    rhs_f = None
    if rhs not in FIELDS:
        rhs_f = float(rhs)
    return lhs, OPS[op], rhs, rhs_f


def conds_ok(conds, bar, mode):
    vals = []
    for lhs, fn, rhs, rhs_f in conds:
        a = bar.get(lhs)
        b = rhs_f if rhs_f is not None else bar.get(rhs)
        if a is None or b is None:
            vals.append(False)
            continue
        vals.append(fn(a, b))
    return all(vals) if mode == "all" else any(vals)


def run_symbol(args_tuple):
    db_root, symbol, tf, rule = args_tuple
    con = sqlite3.connect(f"file:{db_root}\\market.db?mode=ro", uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    bars = [dict(r) for r in con.execute(
        "SELECT ts,o,h,l,c,v,ma5,ma20,atr14,rsi14,macd_hist FROM kline_cache "
        "WHERE symbol=? AND tf=? ORDER BY ts", (symbol, tf))]
    con.close()
    if len(bars) < 40:
        return {"symbol": symbol, "skipped": f"bars={len(bars)}<40"}

    entry_c = [parse_cond(x) for x in rule["entry"]["all"]]
    exit_c = [parse_cond(x) for x in rule.get("exit", {}).get("any", [])]
    short = rule["direction"] == "short"
    stop_atr, tp_atr = rule.get("stop_atr"), rule.get("tp_atr")

    trades, pos = [], None
    for i in range(21, len(bars)):  # 留 ma20 暖机
        bar = dict(bars[i])
        if pos:
            bar["bars_held"] = i - pos["i"]
            px_exit = None
            # 先判 SL/TP（用当根 h/l 近似盘中触发）
            if stop_atr and pos["sl"] is not None:
                if (short and bar["h"] >= pos["sl"]) or (not short and bar["l"] <= pos["sl"]):
                    px_exit = pos["sl"]
            if px_exit is None and tp_atr and pos["tp"] is not None:
                if (short and bar["l"] <= pos["tp"]) or (not short and bar["h"] >= pos["tp"]):
                    px_exit = pos["tp"]
            if px_exit is None and exit_c and conds_ok(exit_c, bar, "any"):
                px_exit = bar["c"]
            if px_exit is not None:
                raw = (pos["px"] - px_exit) / pos["px"] if short else (px_exit - pos["px"]) / pos["px"]
                trades.append(raw - COST_PCT)
                pos = None
            continue
        bar["bars_held"] = 0
        if conds_ok(entry_c, bar, "all"):
            atr = bar.get("atr14")
            px = bar["c"]
            sl = tp = None
            if atr:
                if stop_atr:
                    sl = px + stop_atr * atr if short else px - stop_atr * atr
                if tp_atr:
                    tp = px - tp_atr * atr if short else px + tp_atr * atr
            pos = {"i": i, "px": px, "sl": sl, "tp": tp}

    if not trades:
        return {"symbol": symbol, "n": 0}
    wins = sum(1 for t in trades if t > 0)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= (1 + t)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    gp = sum(t for t in trades if t > 0)
    gl = -sum(t for t in trades if t <= 0)
    return {"symbol": symbol, "n": len(trades), "wr": wins / len(trades),
            "avg_pct": sum(trades) / len(trades) * 100, "total_pct": (eq - 1) * 100,
            "mdd_pct": mdd * 100, "pf": (gp / gl) if gl > 0 else float("inf")}


def pick_symbols(db_root, spec):
    if not spec.startswith("top:"):
        return [s.strip() for s in spec.split(",") if s.strip()]
    n = int(spec.split(":")[1])
    con = sqlite3.connect(f"file:{db_root}\\market.db?mode=ro", uri=True, timeout=20)
    ts = con.execute("SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
    rows = con.execute(
        "SELECT symbol, vol24h*last AS qv FROM tick_snapshots WHERE ts=? AND last IS NOT NULL "
        "ORDER BY qv DESC LIMIT ?", (ts, n)).fetchall()
    con.close()
    syms = [r[0] for r in rows]
    for must in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
        if must not in syms:
            syms.insert(0, must)
    return syms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", required=True)
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--symbols", default="top:22")
    ap.add_argument("--tf", default="4H")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--annotate-playbook", type=int)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rule = json.load(open(args.rule, encoding="utf-8"))
    syms = pick_symbols(args.db_root, args.symbols)
    print(f"== 回测 {rule['name']} ({rule['direction']}) tf={args.tf} 币数={len(syms)} ==")

    tasks = [(args.db_root, s, args.tf, rule) for s in syms]
    if args.workers > 1 and len(tasks) > 3:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(run_symbol, tasks))
    else:
        results = [run_symbol(t) for t in tasks]

    all_t_n, agg_w = 0, 0.0
    valid = []
    for r in sorted(results, key=lambda x: -(x.get("total_pct") or -999)):
        if r.get("skipped"):
            continue
        if r["n"] == 0:
            continue
        valid.append(r)
        all_t_n += r["n"]
        agg_w += r["wr"] * r["n"]
        print(f"  {r['symbol'].split('-')[0]:<8} n={r['n']:<3} wr={r['wr']:.0%} "
              f"avg={r['avg_pct']:+.2f}% total={r['total_pct']:+.1f}% mdd={r['mdd_pct']:.1f}% pf={r['pf']:.2f}")
    if not valid:
        print("  无有效交易")
        return
    wr_all = agg_w / all_t_n
    avg_total = sum(r["total_pct"] for r in valid) / len(valid)
    summary = (f"n={all_t_n} wr={wr_all:.0%} 平均累计收益/币={avg_total:+.1f}% "
               f"覆盖 {len(valid)}/{len(syms)} 币 tf={args.tf}")
    print(f"\n汇总: {summary}")

    if args.annotate_playbook:
        note = f"[backtest {datetime.now(timezone.utc).strftime('%Y%m%d')}: {rule['name']} {summary}]"
        if args.apply:
            con = sqlite3.connect(f"{args.db_root}\\account.db", timeout=15)
            con.execute("UPDATE playbook SET evidence=COALESCE(evidence,'')||' '||? WHERE id=?",
                        (note, args.annotate_playbook))
            con.commit()
            con.close()
            print(f"→ 已注记 playbook #{args.annotate_playbook}（evidence 追加，实盘统计列不动）")
        else:
            print(f"→ dry-run 注记预览 #{args.annotate_playbook}: {note}")


if __name__ == "__main__":
    main()
