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
  7. 已平仓 R 结果
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

if r"." not in sys.path:
    sys.path.insert(0, r".")
from core.decision_card import validate_card  # noqa: E402

CST = timezone(timedelta(hours=8))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_ROOT = Path(os.environ.get("OKX_DB_ROOT", r"./db"))
REPORT_DIR = Path(os.environ.get(
    "OKX_QUALITY_REPORT_DIR", r"./reports/quality"))
WINDOW_DAYS = 14
FAILURE_STATUSES = frozenset({
    "error",
    "failed",
    "fail",
    "timeout",
    "timed_out",
})


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


def normalize_run_status(value: object) -> str:
    """Map equivalent terminal failures to one reporting bucket.

    The raw status remains available in ``raw_status_counts``.  Unknown and
    partial states are deliberately not reclassified as failures without an
    explicit producer contract.
    """
    status = str(value or "unknown").strip().lower()
    if (
        status in FAILURE_STATUSES
        or status.startswith(("error:", "error_"))
        or status.startswith(("failed:", "failed_"))
        or status.startswith(("timeout:", "timeout_"))
    ):
        return "failure"
    if status in {"ok", "degraded"}:
        return status
    return "other"


def _atomic_write_json(path: Path, value: dict) -> None:
    """Publish one complete JSON snapshot; readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


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

    by_source: dict[str, dict] = defaultdict(
        lambda: {
            "ok": 0,
            "degraded": 0,
            "failure": 0,
            "other": 0,
            "total": 0,
            "raw_status_counts": Counter(),
        }
    )
    for r in rows:
        s = r["source"]
        raw_status = str(r["status"] or "unknown").strip().lower()
        bucket = normalize_run_status(raw_status)
        by_source[s][bucket] += r["n"]
        by_source[s]["raw_status_counts"][raw_status] += r["n"]
        by_source[s]["total"] += r["n"]

    result = {}
    for s, counts in sorted(by_source.items()):
        t = counts["total"] or 1
        failure_pct = round(counts["failure"] / t * 100, 1)
        result[s] = {
            "ok_pct": round(counts["ok"] / t * 100, 1),
            "degraded_pct": round(counts["degraded"] / t * 100, 1),
            "failure_pct": failure_pct,
            "failure_runs": counts["failure"],
            "other_runs": counts["other"],
            "total_runs": counts["total"],
            "raw_status_counts": dict(sorted(
                counts["raw_status_counts"].items())),
            # Backward-compatible alias.  It now has the same unified
            # failed/error/timeout denominator as ``failure_pct``.
            "error_pct": failure_pct,
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


# 指标 7（demo 可评估单占比）与 8（demo↔live 同向率）随 2026-08-06 demo 全量
# 下线移除。后者曾按 decision 字面相等计算，被双 HOLD 长期撑在 85% 上下；
# 08-06 改成 active-only 后 14d 实测掉到 24.8%，随即整体下线。

# ── 7. 已平仓 R 结果 ────────────────────────────────────────────────
def metric_closed_r() -> dict:
    con = connect("account.db")
    if con is None:
        return {"error": "account.db not found"}
    ws = window_start()
    try:
        # 2026-08-06 demo 全量下线：只统计 live。历史 demo 经验行在数据清理前
        # 仍留在表里，不加这个过滤会让指标继续冒出一个不再更新的 demo 桶。
        rows = con.execute(
            "SELECT profile, symbol, pnl_pct, status "
            "FROM trade_experiences "
            "WHERE ts >= ? AND status='closed' AND profile='live'",
            (ws,),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return {"total_closed": 0}

    # 2026-08-10 r-semantics：旧 hit_1R/hit_1R_pct 键下线——该字段实为 pnl>0，
    # 与 win_rate 恒等，双键并存曾让报告把胜率误读成"1R 触达率"。真 1R 触达
    # 待 ever_hit_1r（需 MFE 路径证据，Wave2）后另立指标。
    by_profile = defaultdict(lambda: {"n": 0, "avg_pnl_pct": 0, "wins": 0})
    for r in rows:
        p = r["profile"] or "unknown"
        by_profile[p]["n"] += 1
        pnl = r["pnl_pct"] or 0
        by_profile[p]["avg_pnl_pct"] += pnl
        if pnl > 0:
            by_profile[p]["wins"] += 1

    result = {}
    for p, d in by_profile.items():
        n = d["n"] or 1
        result[p] = {
            "n": d["n"],
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
            "closed_r_results": metric_closed_r(),
        },
    }

    out = REPORT_DIR / f"quality_metrics_{today_str()}.json"
    _atomic_write_json(out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n--- written to {out} ---", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
