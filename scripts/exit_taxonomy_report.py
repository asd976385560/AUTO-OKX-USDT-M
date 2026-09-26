# -*- coding: utf-8 -*-
r"""exit_taxonomy_report.py — 平仓出口互斥分类报告（Wave 0 序 3，只读）。

背景（reports/quality/judgment_optimization_plan_20260810.md Wave 0 序 3）：
2026-07 以来的平仓从未有过覆盖全量的出口归因——"84 裁量 + 20 止损"旧统计
窗口错配（104/133），且存在一笔实现 -5.25R 的离群与 16 条无法恢复初始止损的
样本无解释。本脚本对窗口内全部 live 平仓行做**互斥**分类并产出 r_source
覆盖状态，供 T5/T6 验收与 Wave 3 出口政策回放使用。

分类判定（2026-09-26 对照 V3 experience::journey 改为按价位实证，优先级从上到下）：
  imr_forced_reduce          reasoning 含 IMR 破闸/硬闸去风险（V2 维护类退出）
  concentration_derisk       reasoning 含集中度/Concentration 减仓（V2 维护类退出）
  交易所侧成交（reconcile 兜底回填 / 算法单）——取离成交价最近且 ≤1.5% 的候选价位：
    tp_hit                   止盈价位（开仓 tp_trigger_px / 执行包 target / 卡 target）
    sl_hit                   开仓止损；移动过的止损仍在亏损一侧也记 sl_hit
    breakeven_stop           移动过的止损在开仓价 ±0.4% 内
    trail_stop               移动过的止损在盈利一侧
    sl_hit_inferred /        都对不上（成交价缺失或离任何价位 >1.5%）时按净盈亏正负兜底，
    tp_hit_inferred          是推断不是实证，后缀外显、统计分开看
    reconcile_backfill       连净盈亏都没有，无法推断
  Agent 主动平仓——按开仓时给的论点失效价（invalidation_px）拆分：
    manual_close_invalidated        平仓前最近一根已收盘 15m 收盘价已越过失效价
    manual_close_discretionary      给了失效价但收盘价还没越过（裁量平仓）
    manual_close_unverified         给了失效价但没有可核的已收盘 K 线
    manual_close_no_invalidation_px 开仓时没给失效价（只能靠文字论点，不做机器判定）
  历史行仍可能带旧口径 sl_algo_fill / discretionary_sl_cite / discretionary_manual，
  统计侧按字符串原样分组，不重判。
r_source（初始风险可复原性）：
  sl_from_open_raw      同 symbol+side FIFO 配对的 open.raw.sl_trigger_px 可用
  missing               配不到带 SL 的 open（跨窗口开仓/旧格式），realized_r 未知

realized_r = pnl / (open_notional × |open_fill - sl| / open_fill)；仅在
r_source=sl_from_open_raw 时计算，其余输出 null（未知不冒充 0）。

只读：不写任何库。--out-dir 落 reports/quality/exit_taxonomy_<date>.{json,md}。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import collections
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# V3 experience::journey::classify_algo_exit_ex 同口径：成交价离候选价位 ≤1.5% 才算
# 实证（止损按市价成交有滑点）；移动过的止损在开仓价 ±0.4% 内 = 保本止损。
ALGO_MATCH_TOL = 0.015
BREAKEVEN_TOL = 0.004
CST = timezone(timedelta(hours=8))
MAINTENANCE_CATEGORIES = ("imr_forced_reduce", "concentration_derisk")
ALGO_CATEGORIES = ("tp_hit", "sl_hit", "breakeven_stop", "trail_stop")
MANUAL_CATEGORIES = (
    "manual_close_invalidated", "manual_close_discretionary",
    "manual_close_unverified", "manual_close_no_invalidation_px",
)


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _load(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def classify_algo_exit(side: Any, entry_px: Any, exit_px: Any, init_sl: Any,
                       moved_sl: Any, tps: Any, net: Any) -> tuple[str, bool]:
    """交易所侧成交细分（V3 classify_algo_exit_ex 移植）→ (类别, 是否推断)。

    候选价位：止盈（开仓 TP / 执行包 target / 卡 target）、开仓止损、之后移动过的
    最后一个止损。取离成交价最近且 ≤1.5% 的那个；平手时先出现的优先（止盈 >
    开仓止损 > 移动止损）。都对不上按净盈亏正负兜底并标记推断。
    """
    net_value = _finite(net)
    fallback = "sl_hit" if (net_value is not None and net_value < 0) else "tp_hit"
    px = _finite(exit_px)
    if px is None or px <= 0:
        return fallback, True
    candidates: list[tuple[Any, str]] = [(t, "tp") for t in (tps or ())]
    candidates.extend([(init_sl, "init_sl"), (moved_sl, "moved_sl")])
    best: Optional[tuple[float, str, float]] = None
    for level, kind in candidates:
        value = _finite(level)
        if value is None or value <= 0:
            continue
        dist = abs(px - value) / value
        if best is None or dist < best[0]:
            best = (dist, kind, value)
    if best is None or best[0] > ALGO_MATCH_TOL:
        return fallback, True
    _dist, kind, level = best
    if kind == "tp":
        return "tp_hit", False
    if kind == "init_sl":
        return "sl_hit", False
    entry = _finite(entry_px)
    side_token = str(side or "").lower()
    if entry is not None and entry > 0:
        if abs(level - entry) / entry <= BREAKEVEN_TOL:
            return "breakeven_stop", False
        if (side_token == "long" and level > entry) or (
                side_token == "short" and level < entry):
            return "trail_stop", False
    return "sl_hit", False


def classify_manual_close(side: Any, invalidation_px: Any,
                          last_close_px: Any) -> str:
    """Agent 主动平仓细分（V3 classify_exit 的 manual_close 分支移植）。"""
    inv = _finite(invalidation_px)
    close = _finite(last_close_px)
    if inv is None or inv <= 0:
        return "manual_close_no_invalidation_px"
    if close is None or close <= 0:
        return "manual_close_unverified"
    crossed = close <= inv if str(side or "").lower() == "long" else close >= inv
    return "manual_close_invalidated" if crossed else "manual_close_discretionary"


def is_exchange_side_close(reason: Any, raw: Any) -> bool:
    """成交是否来自交易所侧（对账兜底回填 / 算法单），而非 Agent 主动指令。"""
    text = str(reason or "")
    lowered = text.lower()
    if isinstance(raw, dict):
        if raw.get("reconcile_source") or raw.get("reconcile") is True:
            return True
        source = str(raw.get("source") or raw.get("fill_source") or "").lower()
        if "reconcile" in source:
            return True
    return ("reconcile_exchange_close" in lowered or text.startswith("RECON-")
            or "reconcile_source" in lowered)


def invalidation_price(open_raw: Any) -> Optional[float]:
    """开仓回执里的论点失效价：raw.invalidation_px 或决策卡 invalidation_point 的数值键。"""
    if not isinstance(open_raw, dict):
        return None
    direct = _finite(open_raw.get("invalidation_px"))
    if direct is not None and direct > 0:
        return direct
    for container in (open_raw.get("open_execution_package"),
                      open_raw.get("decision_card")):
        if not isinstance(container, dict):
            continue
        direct = _finite(container.get("invalidation_px"))
        if direct is not None and direct > 0:
            return direct
        point = container.get("invalidation_point")
        if isinstance(point, dict):
            for key in ("price", "px", "level", "trigger_px", "invalidation_px"):
                value = _finite(point.get(key))
                if value is not None and value > 0:
                    return value
        elif point is not None:
            value = _finite(point)
            if value is not None and value > 0:
                return value
    return None


def take_profit_levels(open_raw: Any) -> list[float]:
    """开仓回执里的止盈候选价位（去重、保序）：tp_trigger_px、执行包 target、卡 target。"""
    if not isinstance(open_raw, dict):
        return []
    values: list[float] = []
    sources = [open_raw.get("tp_trigger_px")]
    package = open_raw.get("open_execution_package")
    if isinstance(package, dict):
        sources.append(package.get("target"))
    card = open_raw.get("decision_card")
    if isinstance(card, dict):
        rr = card.get("risk_reward")
        if isinstance(rr, dict):
            sources.append(rr.get("target"))
    for candidate in sources:
        value = _finite(candidate)
        if value is not None and value > 0 and value not in values:
            values.append(value)
    return values


def _cst_to_utc(ts_cst: Any) -> Optional[datetime]:
    try:
        parsed = datetime.strptime(str(ts_cst)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=CST).astimezone(timezone.utc)


def last_closed_15m_close(mcon: Any, symbol: str, close_ts_cst: Any) -> Optional[float]:
    """平仓前最近一根**已收盘** 15m K 线的收盘价（K 线 ts 是开盘时刻，收盘 = ts+15m）。"""
    if mcon is None:
        return None
    exit_at = _cst_to_utc(close_ts_cst)
    if exit_at is None:
        return None
    latest_open = (exit_at - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        row = mcon.execute(
            "SELECT c FROM kline_cache WHERE symbol=? AND tf='15m' AND ts<=? "
            "ORDER BY ts DESC LIMIT 1", (symbol, latest_open)).fetchone()
    except sqlite3.Error:
        return None
    return _finite(row[0]) if row else None


def last_adjusted_sl(db_root: Any, symbol: str, side: Any, open_ts_cst: Any,
                     close_ts_cst: Any) -> Optional[float]:
    """持仓期内最后一次 ADJUST_PROTECTION 回执落地的止损价（live_trades.db.trade_cycles）。"""
    if db_root is None:
        return None
    path = Path(db_root) / "live_trades.db"
    if not path.exists():
        return None
    side_token = str(side or "").lower()
    latest: Optional[tuple[str, float]] = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT ts, raw FROM trade_cycles WHERE ts>? AND ts<=? "
                "AND raw LIKE '%ADJUST_PROTECTION%' ORDER BY ts",
                (str(open_ts_cst or ""), str(close_ts_cst or ""))).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    for ts, raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("action_taken") or "").upper() != "ADJUST_PROTECTION":
            continue
        if str(payload.get("symbol") or "") != symbol:
            continue
        if str(payload.get("pos_side") or "").lower() != side_token:
            continue
        applied = payload.get("applied")
        value = _finite(applied.get("sl")) if isinstance(applied, dict) else None
        if value is None or value <= 0:
            continue
        if latest is None or str(ts) >= latest[0]:
            latest = (str(ts), value)
    return latest[1] if latest else None


def exit_context(open_raw: Any, close_trade: Any, *, symbol: str, side: Any,
                 open_ts: Any, close_ts: Any, realized_pnl: Any = None,
                 db_root: Any = None, mcon: Any = None) -> dict[str, Any]:
    """为 classify_exit 装配全部实证输入（缺什么留 None，绝不猜）。"""
    open_raw = open_raw if isinstance(open_raw, dict) else {}
    close_trade = close_trade if isinstance(close_trade, dict) else {}
    events = open_raw.get("close_events")
    last_event = events[-1] if isinstance(events, list) and events and isinstance(
        events[-1], dict) else {}
    reason = str(close_trade.get("reason") or close_trade.get("reasoning")
                 or last_event.get("reason") or last_event.get("reasoning") or "")
    exit_px = _finite(close_trade.get("fill_px")) or _finite(close_trade.get("px")) \
        or _finite(last_event.get("fill_px"))
    raw_for_source = close_trade if close_trade else last_event
    return {
        "reason": reason,
        "raw": raw_for_source,
        "sl_px": _finite(open_raw.get("sl_trigger_px")),
        "fill_px": exit_px,
        "side": str(side or "").lower(),
        "entry_px": _finite(open_raw.get("fill_px")) or _finite(open_raw.get("px")),
        "moved_sl": last_adjusted_sl(db_root, symbol, side, open_ts, close_ts),
        "tps": take_profit_levels(open_raw),
        "net": _finite(realized_pnl),
        "invalidation_px": invalidation_price(open_raw),
        "last_close_px": last_closed_15m_close(mcon, symbol, close_ts),
    }


def classify_exit(reason: str, raw: Any, sl_px: Any, fill_px: Any, *,
                  side: Any = None, entry_px: Any = None, moved_sl: Any = None,
                  tps: Any = (), net: Any = None, invalidation_px: Any = None,
                  last_close_px: Any = None) -> str:
    """互斥出口类别（见模块 docstring）；旧四参调用仍可用，只是实证输入更少。"""
    text = reason or ""
    lowered = text.lower()
    if ("imr" in lowered and ("0.66" in lowered or "硬闸" in text or "破" in text)) \
            or "imr_forced" in lowered:
        return "imr_forced_reduce"
    if "concentration" in lowered or "集中度" in text:
        return "concentration_derisk"
    if is_exchange_side_close(text, raw):
        category, inferred = classify_algo_exit(
            side, entry_px, fill_px, sl_px, moved_sl, tps, net)
        if not inferred:
            return category
        return "reconcile_backfill" if _finite(net) is None else f"{category}_inferred"
    return classify_manual_close(side, invalidation_px, last_close_px)


# 旧名兼容：apply_path_metrics_schema / trade_experience_writer 以 classify 别名导入。
classify = classify_exit


def main() -> int:
    ap = argparse.ArgumentParser(description="平仓出口互斥分类（只读）")
    ap.add_argument("--db-root", default=_public_project_path('db'))
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
                "tps": take_profit_levels(raw),
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
        cat = classify_exit(
            row["reasoning"] or "", raw, sl_px, row["fill_px"],
            side=row["side"],
            entry_px=opener["fill_px"] if opener else None,
            tps=opener.get("tps", []) if opener else [],
            net=row["pnl"],
        )
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
