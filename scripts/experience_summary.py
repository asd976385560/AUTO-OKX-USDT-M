# -*- coding: utf-8 -*-
r"""V2.0 §8.5 L2 —— 交易经验「教训摘要」异步回填（account.db.trade_experiences.experience_summary）。

L1（结构化经验行）由 trades_writer→trade_experience_writer 同步写；L2 为已平仓经验补一句
**确定性事实摘要**（regime/方向/盈亏/毛利方向/持仓时长/历史取舍），供复盘/检索时一眼看懂。
2026-08-10 r-semantics：摘要不再出现 "hit1R"/"miss" 字样——旧字段 hit_1R 实为 pnl>0，
该措辞会让模型把毛利误读成 1R 触达；改用 gross_win/gross_loss。
reviewer 每日复盘收尾段调用本脚本，不阻塞交易。
确定性生成（不依赖 LLM 多步可靠性，红线之鉴）；调用方如需可在此之上再润色，但本脚本保证列必被填。

只写 experience_summary / experience_summary_version 两列。v2 摘要只包含结构化字段，
绝不把历史 decision_card 的自由文本重新喂回检索。dry-run 默认，--apply 才写。

用法：
  run_okx_python.ps1 scripts/experience_summary.py --db-root ./db            # dry-run 列待填
  run_okx_python.ps1 scripts/experience_summary.py --db-root ./db --apply    # 回填
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SUMMARY_VERSION = 2
_LEGACY_1R_RE = re.compile(r"(?i)\bhit[_ ]?1r\b")


def _fmt(row: sqlite3.Row) -> str:
    """从经验行拼一句确定性事实摘要（ASCII 安全，便于嵌任意渠道）。"""
    regime = row["regime"] or "?"
    side = row["side"] or "?"
    pnl = row["pnl_pct"]
    hit = row["is_gross_profit_close"]
    hold = row["hold_hours"]
    parts = [f"{regime}/{side}"]
    if pnl is not None:
        parts.append(f"pnl{pnl:+.2f}%")
    if hit is None:
        parts.append("gross_unknown")
    else:
        parts.append("gross_win" if int(hit) == 1 else "gross_loss")
    if hold is not None:
        parts.append(f"hold{hold:.1f}h")
    try:
        raw = json.loads(row["raw"] or "{}")
        card = raw.get("decision_card") if isinstance(raw, dict) else None
        history = card.get("historical_experience") if isinstance(card, dict) else None
        if isinstance(history, dict):
            parts.append(f"history={history.get('usage') or 'none'}")
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
    summary = " ".join(parts)
    # 双保险：v2 生成器永不应产生旧 token；若未来字段意外带入则 fail-safe 清除。
    return _LEGACY_1R_RE.sub("legacy_r_metric", summary)


def fill(db_root, apply: bool = False, limit: int = 500,
         refresh_labels: bool = False,
         refresh_all_closed: bool = False) -> dict:
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
        cols = {str(r[1]) for r in con.execute(
            "PRAGMA table_info(trade_experiences)")}
        if "experience_summary_version" not in cols:
            return {
                "ok": False,
                "error": (
                    "trade_experiences 缺 experience_summary_version；"
                    "先运行 apply_r_semantics_schema.py 迁移"
                ),
            }
        if refresh_all_closed:
            where = "1=1"
        elif refresh_labels:
            # SQL 只负责缩小集合，最终 token 保证由 v2 生成器承担。
            where = (
                "(COALESCE(experience_summary_version,0)<>? "
                "OR lower(COALESCE(experience_summary,'')) LIKE '%hit1r%' "
                "OR lower(COALESCE(experience_summary,'')) LIKE '%hit_1r%' "
                "OR lower(COALESCE(experience_summary,'')) LIKE '%hit 1r%' "
                "OR experience_summary LIKE '% miss %' "
                "OR experience_summary LIKE '% miss')"
            )
        else:
            where = (
                "(experience_summary IS NULL OR experience_summary='' "
                "OR COALESCE(experience_summary_version,0)<>?)"
            )
        params: tuple = () if refresh_all_closed else (SUMMARY_VERSION,)
        rows = con.execute(
            "SELECT id, regime, side, pnl_pct, is_gross_profit_close, "
            "hold_hours, raw "
            f"FROM trade_experiences WHERE status='closed' AND {where} "
            "ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        previews = [{"id": r["id"], "summary": _fmt(r)} for r in rows]
        filled = 0
        if apply and rows:
            for r in rows:
                con.execute(
                    "UPDATE trade_experiences SET experience_summary=?, "
                    "experience_summary_version=? WHERE id=?",
                    (_fmt(r), SUMMARY_VERSION, r["id"]),
                )
                filled += 1
            con.commit()
        return {"ok": True, "summary_version": SUMMARY_VERSION,
                "pending": len(rows), "filled": filled,
                "preview": previews[:8]}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="L2 经验摘要确定性回填")
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--refresh-labels", action="store_true",
                    help="一次性重算含旧 hit1R/miss 措辞的历史摘要（r-semantics 迁移配套）")
    ap.add_argument(
        "--refresh-all-closed", action="store_true",
        help="受控全量重算所有 closed 摘要（迁移/审计用）",
    )
    args = ap.parse_args()
    out = fill(args.db_root, apply=args.apply, limit=args.limit,
               refresh_labels=args.refresh_labels,
               refresh_all_closed=args.refresh_all_closed)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
