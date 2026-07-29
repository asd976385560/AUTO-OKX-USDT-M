# -*- coding: utf-8 -*-
"""记录 Agent 未执行机会的后验表现，供后续决策卡参考。

背景：该表曾停更，导致压制策略缺少对照组——
「不开仓」的机会成本无人量化，压制经验只能自证。本脚本按日回填：

  取指定日 decision_card_v1 中 action=wait/hold 且有方向的候选；
  无 decision_card 的兼容记录仍按 total/confidence 阈值读取，
  剔除同 cycle 同 symbol 已真实成交的行，按 15m kline 计算其后 4h 实际走幅与是否可达 1R
  （1R=按系统惯例 -2% SL 的对称目标 +2%；side 缺失按 long 惯例并在 notes 标注），
  幂等写入 missed_opportunities（同 ts+symbol 已存在则跳过）。

调度：reviewer 每日复盘（08:05）跑 `--date yesterday`；也可手动补历史日。
写库纪律：lessons.db writer=复盘链路，本脚本是该链路的确定性组件。
ts 写 CST 'YYYY-MM-DD HH:MM:SS'（禁 JobB-/UTC-Z 混入——该表 ts 已有历史混格式之痛）。

用法：
  pwsh -NoProfile -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/scripts/missed_opps_writer.py --date 2026-07-11 [--dry-run]
  --date yesterday（默认）
"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

TOTAL_MIN = 45
CONF_MIN = 0.40
R_PCT = 2.0  # 兼容记录无失效距离时的后验评估兜底


def _utcz_from_cst(cst_str: str) -> str:
    dt = datetime.strptime(cst_str, "%Y-%m-%d %H:%M:%S") - timedelta(hours=8)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="yesterday", help="CST 日期 YYYY-MM-DD 或 'yesterday'")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    day = (
        (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if args.date == "yesterday" else args.date
    )
    root = args.db_root

    ana = sqlite3.connect(f"file:{root}\\analysis.db?mode=ro", uri=True)
    mkt = sqlite3.connect(f"file:{root}\\market.db?mode=ro", uri=True)
    exe = {}  # (cycle_id, symbol) 已成交集合
    for db in ("live_trades", "demo_trades"):
        con = sqlite3.connect(f"file:{root}\\{db}.db?mode=ro", uri=True)
        for cyc, sym in con.execute(
            "SELECT cycle_id, symbol FROM trades WHERE cycle_id LIKE ?", (day + "%",)
        ):
            exe[(cyc, sym)] = True
        con.close()

    cands = ana.execute(
        "SELECT cycle_id, symbol, total, confidence, side, decision_card, regime.regime "
        "FROM analysis_signals "
        "JOIN (SELECT cycle_id AS c2, regime FROM analysis_runs) regime ON regime.c2 = analysis_signals.cycle_id "
        "WHERE analysis_signals.cycle_id LIKE ? AND action IN ('wait','hold')",
        (day + "%",),
    ).fetchall()
    ana.close()

    les = sqlite3.connect(f"{root}\\lessons.db")
    les.execute("PRAGMA busy_timeout=5000")
    written = skipped = nodata = 0
    try:
        selected = 0
        for cyc, sym, total, conf, side, card_raw, regime in cands:
            try:
                card = json.loads(card_raw) if card_raw else None
            except (json.JSONDecodeError, TypeError):
                card = None
            is_card = isinstance(card, dict)
            if is_card:
                # 没有可检验方向的纯持仓观察不记为错失机会。
                if side not in ("long", "short"):
                    continue
            elif not (
                (total is not None and total >= TOTAL_MIN)
                or (conf is not None and conf >= CONF_MIN)
            ):
                continue
            selected += 1
            if (cyc, sym) in exe:
                continue
            slot_cst = cyc.replace("T", " ") + ":00"  # 'YYYY-MM-DDTHH:MM' -> CST ts
            if les.execute(
                "SELECT 1 FROM missed_opportunities WHERE ts=? AND symbol=?", (slot_cst, sym)
            ).fetchone():
                skipped += 1
                continue
            t0 = _utcz_from_cst(slot_cst)
            t4 = _utcz_from_cst(
                (datetime.strptime(slot_cst, "%Y-%m-%d %H:%M:%S") + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
            )
            rows = mkt.execute(
                "SELECT ts, o, h, l, c FROM kline_cache WHERE symbol=? AND tf='15m' "
                "AND ts>=? AND ts<? ORDER BY ts",
                (sym, t0, t4),
            ).fetchall()
            if len(rows) < 8:  # 4h 窗至少要 8 根 15m 才算数据可用
                nodata += 1
                continue
            px0 = rows[0][1]
            close4 = rows[-1][4]
            hi = max(r[2] for r in rows)
            lo = min(r[3] for r in rows)
            direction = side if side in ("long", "short") else "long"
            risk_pct = R_PCT
            if is_card:
                try:
                    rr = card.get("risk_reward") or {}
                    candidate_risk = rr.get("risk_pct") if isinstance(rr, dict) else None
                    if candidate_risk is not None and float(candidate_risk) > 0:
                        risk_pct = float(candidate_risk)
                except (TypeError, ValueError):
                    risk_pct = R_PCT
            if direction == "short":
                actual = (px0 - close4) / px0 * 100.0
                hit_1r = 1 if (px0 - lo) / px0 * 100.0 >= risk_pct else 0
            else:
                actual = (close4 - px0) / px0 * 100.0
                hit_1r = 1 if (hi - px0) / px0 * 100.0 >= risk_pct else 0
            note = (
                f"cycle={cyc} 未执行候选；risk_pct={risk_pct:.3f}"
                + ("；decision_card_v1" if is_card else f"；兼容格式 total={total} conf={conf}")
                + ("；side 缺失按 long 惯例" if side not in ("long", "short") else "")
            )
            if not args.dry_run:
                les.execute(
                    "INSERT INTO missed_opportunities"
                    "(ts, symbol, score, regime, direction_hint, actual_4h_pct, "
                    "would_hit_1R, notes, reviewed_utc, decision_card)"
                    "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)",
                    (slot_cst, sym, total if total is not None else 0, regime,
                     direction, round(actual, 3), hit_1r, note, card_raw),
                )
            written += 1
        if not args.dry_run:
            les.commit()
    finally:
        les.close()
        mkt.close()

    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}ok date={day} candidates={selected} written={written} "
          f"dup_skipped={skipped} no_kline={nodata}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
