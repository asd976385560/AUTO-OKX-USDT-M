# -*- coding: utf-8 -*-
r"""V2.0 §8.5 L2 —— 交易经验「教训摘要」异步回填（account.db.trade_experiences.experience_summary）。

L1（结构化经验行）由 trades_writer→trade_experience_writer 同步写；L2 为已平仓经验补一句
**确定性事实摘要**（regime/方向/盈亏/是否达 1R/持仓时长/历史取舍），供复盘/检索时一眼看懂。
reviewer 每日复盘收尾段调用本脚本，不阻塞交易。
确定性生成（不依赖 LLM 多步可靠性，红线之鉴）；调用方如需可在此之上再润色，但本脚本保证列必被填。

只写 experience_summary 一列，仅对 status='closed' 且 summary 为空的行。dry-run 默认，--apply 才写。

用法：
  run_okx_python.ps1 scripts/experience_summary.py --db-root <PROJECT_ROOT>/db            # dry-run 列待填
  run_okx_python.ps1 scripts/experience_summary.py --db-root <PROJECT_ROOT>/db --apply    # 回填
"""
from __future__ import annotations

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
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _fmt(row: sqlite3.Row) -> str:
    """从经验行拼一句确定性事实摘要（ASCII 安全，便于嵌任意渠道）。"""
    regime = row["regime"] or "?"
    side = row["side"] or "?"
    pnl = row["pnl_pct"]
    hit = row["hit_1R"]
    hold = row["hold_hours"]
    parts = [f"{regime}/{side}"]
    if pnl is not None:
        parts.append(f"pnl{pnl:+.2f}%")
    parts.append("hit1R" if hit else "miss")
    if hold is not None:
        parts.append(f"hold{hold:.1f}h")
    try:
        raw = json.loads(row["raw"] or "{}")
        card = raw.get("decision_card") if isinstance(raw, dict) else None
        history = card.get("historical_experience") if isinstance(card, dict) else None
        if isinstance(history, dict):
            parts.append(f"history={history.get('usage') or 'none'}")
            if history.get("reason"):
                parts.append(str(history["reason"])[:80])
    except (json.JSONDecodeError, TypeError):
        pass
    # 一句教训倾向：盈/亏 + regime 是否吻合方向（仅事实，不预测）
    lesson = ""
    if pnl is not None:
        good = pnl > 0
        if regime == "trend_up" and side == "long" or regime == "trend_down" and side == "short":
            lesson = "顺势" + ("成功" if good else "失败")
        elif regime in ("trend_up", "trend_down"):
            lesson = "逆势" + ("侥幸" if good else "受损")
        elif regime == "range":
            lesson = "区间" + ("获利" if good else "亏损")
    if lesson:
        parts.append(lesson)
    return " ".join(parts)


def fill(db_root, apply: bool = False, limit: int = 500) -> dict:
    db = Path(db_root) / "account.db"
    if not db.exists():
        return {"ok": False, "error": f"account.db 不存在: {db}"}
    con = sqlite3.connect(str(db), timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
    con.row_factory = sqlite3.Row
    try:
        # 表不存在 → 安全跳过
        if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name='trade_experiences'").fetchone():
            return {"ok": True, "pending": 0, "filled": 0, "note": "no trade_experiences table"}
        rows = con.execute(
            "SELECT id, regime, side, pnl_pct, hit_1R, hold_hours, raw "
            "FROM trade_experiences WHERE status='closed' "
            "AND (experience_summary IS NULL OR experience_summary='') "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        previews = [{"id": r["id"], "summary": _fmt(r)} for r in rows]
        filled = 0
        if apply and rows:
            for r in rows:
                con.execute("UPDATE trade_experiences SET experience_summary=? WHERE id=?",
                            (_fmt(r), r["id"]))
                filled += 1
            con.commit()
        return {"ok": True, "pending": len(rows), "filled": filled,
                "preview": previews[:8]}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="L2 经验摘要确定性回填")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()
    out = fill(args.db_root, apply=args.apply, limit=args.limit)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
