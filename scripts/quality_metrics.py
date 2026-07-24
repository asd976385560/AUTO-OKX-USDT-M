# -*- coding: utf-8 -*-
"""
quality_metrics.py — 日频质量指标生成器（Phase 0）

07:55 跑完，产出 reports/quality/quality_metrics_YYYY-MM-DD.json
reviewer 08:05 开场读这个文件做数据驱动复盘。

核心指标：
  1. 必需源 fresh 达标率
  2. decision_card_v1 结构合法率
  3. skip/stale 占比
  4. action 分布
  5. 每币种信号频次
  6. 历史经验取舍分布
  7. demo 可评估单占比
  8. demo↔live 同向率
  9. 已平仓 R 结果
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

if _project_path() not in sys.path:
    sys.path.insert(0, _project_path())
from core.decision_card import validate_card  # noqa: E402

CST = timezone(timedelta(hours=8))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_ROOT = Path(_project_path('db'))
REPORT_DIR = Path(_project_path('reports', 'quality'))
WINDOW_DAYS = 14


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def connect(db_name: str) -> sqlite3.Connection:
    path = DB_ROOT / db_name
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def window_start(days: int = WINDOW_DAYS) -> str:
    dt = datetime.now(CST) - timedelta(days=days)
    return dt.strftime("%Y-%m-%d")


def cycle_window_clause(col: str = "cycle_id", days: int = WINDOW_DAYS) -> tuple[str, list]:
    ws = window_start(days)
    return f"{col} >= ?", [ws]


# ── 1. 必需源 fresh 达标率 ──────────────────────────────────────────
def metric_source_health() -> dict:
    con = connect("ledger.db")
    if con is None:
        return {"error": "ledger.db not found"}
    ws = window_start()
    try:
        rows = con.execute(
            "SELECT source, status, COUNT(*) as n FROM collection_runs "
            "WHERE ts >= ? GROUP BY source, status",
            (ws,),
        ).fetchall()
    finally:
        con.close()

    by_source: dict[str, dict] = defaultdict(lambda: {"ok": 0, "degraded": 0, "error": 0, "total": 0})
    for r in rows:
        s = r["source"]
        st = r["status"] or "unknown"
        if st in ("ok", "degraded", "error"):
            by_source[s][st] += r["n"]
            by_source[s]["total"] += r["n"]
        else:
            by_source[s]["total"] += r["n"]

    result = {}
    for s, counts in sorted(by_source.items()):
        t = counts["total"] or 1
        result[s] = {
            "ok_pct": round(counts["ok"] / t * 100, 1),
            "degraded_pct": round(counts["degraded"] / t * 100, 1),
            "error_pct": round(counts["error"] / t * 100, 1),
            "total_runs": counts["total"],
        }
    return result


# ── 2-6. analysis 指标 ──────────────────────────────────────────────
def metric_analysis() -> dict:
    con = connect("analysis.db")
    if con is None:
        return {"error": "analysis.db not found"}
    ws = window_start()

    try:
        # runs
        runs = con.execute(
            "SELECT cycle_id, status FROM analysis_runs WHERE cycle_id >= ?",
            (ws,),
        ).fetchall()
        total_runs = len(runs)
        status_counts = Counter(r["status"] or "unknown" for r in runs)
        skip_stale = status_counts.get("skipped", 0) + status_counts.get("stale", 0)
        skip_stale_pct = round(skip_stale / total_runs * 100, 1) if total_runs else 0

        # signals
        signals = con.execute(
            "SELECT symbol, action, side, decision_card "
            "FROM analysis_signals WHERE cycle_id >= ?",
            (ws,),
        ).fetchall()
        total_signals = len(signals)

        # 结构合法率
        valid = 0
        card_rows = 0
        legacy_rows = 0
        action_dist = Counter()
        symbol_freq = Counter()
        history_usage = Counter()
        for s in signals:
            action_ok = s["action"] is not None
            symbol_ok = s["symbol"] is not None and str(s["symbol"]).endswith("-USDT-SWAP")
            try:
                card = json.loads(s["decision_card"]) if s["decision_card"] else None
            except (json.JSONDecodeError, TypeError):
                card = None
            if isinstance(card, dict):
                card_rows += 1
                if not validate_card(card) and action_ok and symbol_ok:
                    valid += 1
                history = card.get("historical_experience") or {}
                history_usage[str(history.get("usage") or "missing")] += 1
            else:
                legacy_rows += 1
            action_dist[s["action"] or "null"] += 1
            symbol_freq[s["symbol"] or "null"] += 1

        valid_pct = round(valid / card_rows * 100, 1) if card_rows else 0
        action_pct = {k: round(v / total_signals * 100, 1) for k, v in action_dist.most_common()} if total_signals else {}
        symbol_top = dict(symbol_freq.most_common(15))

        return {
            "total_runs": total_runs,
            "total_signals": total_signals,
            "status_counts": dict(status_counts),
            "skip_stale_pct": skip_stale_pct,
            "structure_valid_pct": valid_pct,
            "decision_card_rows": card_rows,
            "legacy_score_rows": legacy_rows,
            "action_distribution": action_pct,
            "symbol_frequency_top15": symbol_top,
            "history_usage_distribution": dict(history_usage),
        }
    finally:
        con.close()


# ── 7. demo 可评估单占比 ────────────────────────────────────────────
def metric_demo_evaluable() -> dict:
    con = connect("demo_trades.db")
    if con is None:
        return {"error": "demo_trades.db not found"}
    ws = window_start()
    try:
        trades = con.execute(
            "SELECT action, fill_px, pnl FROM trades WHERE cycle_id >= ?",
            (ws,),
        ).fetchall()
        total = len(trades)
        if total == 0:
            return {"total_trades": 0, "evaluable_pct": 0}
        evaluable = sum(
            1 for t in trades
            if t["action"] in ("open", "close")
            and t["fill_px"] is not None
        )
        return {
            "total_trades": total,
            "evaluable": evaluable,
            "evaluable_pct": round(evaluable / total * 100, 1),
        }
    finally:
        con.close()


# ── 8. demo↔live 同向率 ─────────────────────────────────────────────
def metric_demo_live_agreement() -> dict:
    ws = window_start()
    live_con = connect("live_trades.db")
    demo_con = connect("demo_trades.db")
    if live_con is None or demo_con is None:
        return {"error": "trades db not found"}

    try:
        live_cycles = {
            r["cycle_id"]: (r["decision"] or "unknown")
            for r in live_con.execute(
                "SELECT cycle_id, decision FROM trade_cycles WHERE cycle_id >= ?",
                (ws,),
            ).fetchall()
        }
        demo_cycles = {
            r["cycle_id"]: (r["decision"] or "unknown")
            for r in demo_con.execute(
                "SELECT cycle_id, decision FROM trade_cycles WHERE cycle_id >= ?",
                (ws,),
            ).fetchall()
        }
    finally:
        live_con.close()
        demo_con.close()

    common = set(live_cycles.keys()) & set(demo_cycles.keys())
    if not common:
        return {"common_cycles": 0, "agreement_pct": 0}
    agree = sum(1 for c in common if live_cycles[c] == demo_cycles[c])
    return {
        "common_cycles": len(common),
        "agreement": agree,
        "agreement_pct": round(agree / len(common) * 100, 1),
        "disagreement_examples": [
            {"cycle_id": c, "live": live_cycles[c], "demo": demo_cycles[c]}
            for c in sorted(common, reverse=True)[:10]
            if live_cycles[c] != demo_cycles[c]
        ],
    }


# ── 9. 已平仓 R 结果 ────────────────────────────────────────────────
def metric_closed_r() -> dict:
    con = connect("account.db")
    if con is None:
        return {"error": "account.db not found"}
    ws = window_start()
    try:
        rows = con.execute(
            "SELECT profile, symbol, pnl_pct, hit_1R, status "
            "FROM trade_experiences WHERE ts >= ? AND status='closed'",
            (ws,),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return {"total_closed": 0}

    by_profile = defaultdict(lambda: {"n": 0, "hit_1R": 0, "avg_pnl_pct": 0, "wins": 0})
    for r in rows:
        p = r["profile"] or "unknown"
        by_profile[p]["n"] += 1
        if r["hit_1R"]:
            by_profile[p]["hit_1R"] += 1
        pnl = r["pnl_pct"] or 0
        by_profile[p]["avg_pnl_pct"] += pnl
        if pnl > 0:
            by_profile[p]["wins"] += 1

    result = {}
    for p, d in by_profile.items():
        n = d["n"] or 1
        result[p] = {
            "n": d["n"],
            "hit_1R": d["hit_1R"],
            "hit_1R_pct": round(d["hit_1R"] / n * 100, 1),
            "win_rate": round(d["wins"] / n * 100, 1),
            "avg_pnl_pct": round(d["avg_pnl_pct"] / n, 2),
        }
    return result


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "ts": now_cst(),
        "window_days": WINDOW_DAYS,
        "window_start": window_start(),
        "metrics": {
            "source_health": metric_source_health(),
            "analysis": metric_analysis(),
            "demo_evaluable": metric_demo_evaluable(),
            "demo_live_agreement": metric_demo_live_agreement(),
            "closed_r_results": metric_closed_r(),
        },
    }

    out = REPORT_DIR / f"quality_metrics_{today_str()}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n--- written to {out} ---", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
