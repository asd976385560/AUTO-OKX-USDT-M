# -*- coding: utf-8 -*-
r"""exit_taxonomy_report.py — 平仓出口互斥分类报告（Wave 0 序 3，只读）。

背景（reports/quality/judgment_optimization_plan_20260810.md Wave 0 序 3）：
2026-07 以来的平仓从未有过覆盖全量的出口归因——"84 裁量 + 20 止损"旧统计
窗口错配（104/133），且存在一笔实现 -5.25R 的离群与 16 条无法恢复初始止损的
样本无解释。本脚本对窗口内全部 live 平仓行做**互斥**分类并产出 r_source
覆盖状态，供 T5/T6 验收与 Wave 3 出口政策回放使用。

分类判定（优先级从上到下，首个命中即归类）：
  imr_forced_reduce     reasoning 含 IMR 破闸/硬闸去风险
  concentration_derisk  reasoning 含集中度/Concentration 减仓
  sl_algo_fill          reconcile 兜底回填且成交价落在开仓 SL 触发价 ±0.6% 内
                        （交易所侧 SL 算法单成交，由对账器记账）
  reconcile_backfill    reconcile 兜底回填但成交价不贴 SL（交易所侧其它成交）
  discretionary_sl_cite reasoning 明示按 SL/止损逻辑主动平（非算法单触发）
  discretionary_manual  其余主动裁量平仓
r_source（初始风险可复原性）：
  sl_from_open_raw      同 symbol+side FIFO 配对的 open.raw.sl_trigger_px 可用
  missing               配不到带 SL 的 open（跨窗口开仓/旧格式），realized_r 未知

realized_r = pnl / (open_notional × |open_fill - sl| / open_fill)；仅在
r_source=sl_from_open_raw 时计算，其余输出 null（未知不冒充 0）。

只读：不写任何库。--out-dir 落 reports/quality/exit_taxonomy_<date>.{json,md}。
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SL_MATCH_TOL = 0.006  # 成交价贴 SL 触发价的相对容差


def _load(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def classify(reason: str, raw: dict, sl_px: float | None,
             fill_px: float | None) -> str:
    r = reason or ""
    rl = r.lower()
    if ("imr" in rl and ("0.66" in rl or "硬闸" in r or "破" in r)) or \
            "imr_forced" in rl:
        return "imr_forced_reduce"
    if "concentration" in rl or "集中度" in r:
        return "concentration_derisk"
    is_reconcile = ("reconcile_source" in raw
                    or "reconcile_exchange_close" in rl
                    or r.startswith("RECON-"))
    if is_reconcile:
        if sl_px and fill_px and abs(fill_px - sl_px) / sl_px <= SL_MATCH_TOL:
            return "sl_algo_fill"
        return "reconcile_backfill"
    if "sl" in rl.split() or "止损" in r or "SL" in r:
        return "discretionary_sl_cite"
    return "discretionary_manual"


def main() -> int:
    ap = argparse.ArgumentParser(description="平仓出口互斥分类（只读）")
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--since", default="2026-07-01")
    ap.add_argument("--until", default="2099-01-01")
    ap.add_argument("--out-dir", default=None,
                    help="写 reports/quality/exit_taxonomy_<tag>.{json,md}；缺省只打印")
    args = ap.parse_args()

    con = sqlite3.connect(
        f"file:{Path(args.db_root) / 'live_trades.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(
        "SELECT id, ts, symbol, side, action, sz, fill_px, notional, pnl, "
        "reasoning, raw FROM trades WHERE ts>=? AND ts<? ORDER BY ts",
        (args.since, args.until)))
    con.close()

    # FIFO 配对 open（同 symbol+side），取初始 SL 与开仓名义
    open_q: dict[tuple, collections.deque] = collections.defaultdict(
        collections.deque)
    results = []
    for row in rows:
        key = (row["symbol"], row["side"])
        raw = _load(row["raw"])
        if row["action"] == "open":
            open_q[key].append({
                "fill_px": row["fill_px"],
                "notional": row["notional"],
                "sl": raw.get("sl_trigger_px"),
            })
            continue
        if not str(row["action"]).startswith("close") or row["pnl"] is None:
            continue
        opener = open_q[key].popleft() if open_q[key] else None
        sl_px = None
        r_source = "missing"
        initial_risk = None
        realized_r = None
        if opener and opener["sl"] and opener["fill_px"] and opener["notional"]:
            try:
                sl_px = float(opener["sl"])
                stop_dist = abs(opener["fill_px"] - sl_px) / opener["fill_px"]
                if stop_dist > 0:
                    initial_risk = opener["notional"] * stop_dist
                    realized_r = row["pnl"] / initial_risk
                    r_source = "sl_from_open_raw"
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        cat = classify(row["reasoning"] or "", raw, sl_px, row["fill_px"])
        results.append({
            "trade_id": row["id"],
            "ts": row["ts"],
            "symbol": row["symbol"],
            "side": row["side"],
            "pnl": round(row["pnl"], 4),
            "category": cat,
            "r_source": r_source,
            "initial_risk_usdt": round(initial_risk, 2) if initial_risk else None,
            "realized_r": round(realized_r, 3) if realized_r is not None else None,
            "reason_head": (row["reasoning"] or "")[:80],
        })

    agg = collections.defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for x in results:
        agg[x["category"]]["n"] += 1
        agg[x["category"]]["pnl"] += x["pnl"]
    outliers = [x for x in results
                if x["realized_r"] is not None and x["realized_r"] <= -2.0]
    missing = [x for x in results if x["r_source"] == "missing"]

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": [args.since, args.until],
        "total_closes": len(results),
        "by_category": {k: {"n": v["n"], "pnl": round(v["pnl"], 2)}
                        for k, v in sorted(agg.items(),
                                           key=lambda i: -i[1]["n"])},
        "r_source_coverage": {
            "sl_from_open_raw": len(results) - len(missing),
            "missing": len(missing),
        },
        "r_outliers_le_minus2": outliers,
        "missing_sl_rows": [
            {k: x[k] for k in ("trade_id", "ts", "symbol", "side", "pnl")}
            for x in missing],
        "rows": results,
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d")
        jpath = out / f"exit_taxonomy_{tag}.json"
        jpath.write_text(text, encoding="utf-8")
        lines = [
            f"# 平仓出口互斥分类 {tag}",
            "",
            f"> 窗口 [{args.since}, {args.until})，live 平仓 {len(results)} 笔，"
            "全量互斥分类；只读生成 by exit_taxonomy_report.py",
            "",
            "| 出口类别 | n | PnL 合计 |",
            "|---|---:|---:|",
        ]
        for k, v in report["by_category"].items():
            lines.append(f"| {k} | {v['n']} | {v['pnl']:+.2f} |")
        lines += [
            "",
            f"r_source 覆盖：sl_from_open_raw="
            f"{report['r_source_coverage']['sl_from_open_raw']}，"
            f"missing={report['r_source_coverage']['missing']}"
            "（missing=窗口内配不到带 SL 的 open，realized_r 未知不冒充 0）",
            "",
            "## 实现 R ≤ -2 离群",
            "",
        ]
        if outliers:
            lines.append("| ts | symbol | side | pnl | realized_r | 类别 | 原因头 |")
            lines.append("|---|---|---|---:|---:|---|---|")
            for x in outliers:
                lines.append(
                    f"| {x['ts'][:16]} | {x['symbol'].replace('-USDT-SWAP', '')} "
                    f"| {x['side']} | {x['pnl']:+.2f} | {x['realized_r']} "
                    f"| {x['category']} | {x['reason_head'][:50]} |")
        else:
            lines.append("无")
        (out / f"exit_taxonomy_{tag}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "json": str(jpath),
                          "md": str(out / f'exit_taxonomy_{tag}.md'),
                          "total": len(results),
                          "by_category": report["by_category"],
                          "r_source_coverage": report["r_source_coverage"]},
                         ensure_ascii=False, indent=1))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
