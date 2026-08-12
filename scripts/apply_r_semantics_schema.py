# -*- coding: utf-8 -*-
r"""apply_r_semantics_schema.py — 1R 假语义冻结迁移（Wave 0 序 1，主人 2026-08-10 拍板）。

背景（判断优化终稿 reports/quality/judgment_optimization_plan_20260810.md 缺陷 #6）：
`trade_experiences.hit_1R` 实现是 `pnl_pct > 0`，与胜率完全等价，却以"达 1R"之名
进入日报/周报/经验提示；`missed_opportunities.would_hit_1R` 又是另一套固定 ±2%
代理口径，两套语义同名混用。本迁移把名与实对齐：

  account.db.trade_experiences:
    hit_1R  →  is_gross_profit_close   （1=毛利>0 | 0=毛利<=0 | NULL=未平/未知）
    新增 ever_hit_1r INTEGER            （NULL=无价格路径证据；Wave2 MFE 埋点前恒 NULL，
                                         禁止用 0 冒充未知）
  lessons.db.missed_opportunities:
    would_hit_1R  →  would_hit_1r_fixed2pct （固定 ±2% 代理口径；与真实计划止损无关）
    历史行中 notes 声明 risk_pct≠2.000 的 10 行（8 行卡值 2.3~4.9 + 2 行单位 bug
    0.025/0.026）不合新列名口径：值置 NULL，原值与 risk_pct 追加进 notes 留审计。

SQLite RENAME COLUMN 会保留 DDL 行内旧注释（"1=达 1R"谎言残留），故按
apply_repair_queue_status_check_schema.py 的成熟模式整表重建：备份（backup API +
quick_check）→ 单事务建新表（新注释）→ 整表拷贝 → drop/rename → 重建索引。
AUTOINCREMENT 的 sqlite_sequence 随显式 id 拷贝与 RENAME 自动跟随。

默认 dry-run 只打印计划与现状；--apply 必须配 --backup-dir。幂等：新列已存在时
直接报告 already-applied。迁移后须运行 scripts/export_schema.py 刷新 db/schema.sql。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TE_NEW_DDL = """CREATE TABLE trade_experiences_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id           TEXT NOT NULL,
    ts                 TEXT NOT NULL,        -- UTC+8 'YYYY-MM-DD HH:MM:SS'
    profile            TEXT NOT NULL,        -- 'live' | 'demo'
    symbol             TEXT NOT NULL,
    side               TEXT,                 -- long | short
    action             TEXT,                 -- open | close | add | reduce
    regime             TEXT,
    regime_stale       INTEGER DEFAULT 0,
    score_total        INTEGER,
    confidence         REAL,
    playbook_ref       TEXT,
    hypothesis_id      TEXT,
    market_snapshot    TEXT,                 -- JSON：regime/支撑阻力/衍生品极值/新闻摘要
    experience_vector  TEXT,                 -- JSON 数组：判定向量（算相似用）
    pnl_pct            REAL,                 -- 平仓时填；毛口径（不含手续费/资金费）
    hold_hours         REAL,
    is_gross_profit_close INTEGER,           -- 1=毛利>0 | 0=毛利<=0 | NULL=未平/未知；2026-08-10 由 hit_1R 更名（旧名谎称"达1R"，实义只是毛利为正）
    status             TEXT DEFAULT 'open',  -- open | closed
    raw                TEXT,                 -- 完整回执 JSON（事实正本）
    experience_summary TEXT,                 -- L2 异步：确定性教训摘要
    experience_summary_version INTEGER,      -- 摘要生成协议；v2 禁止回灌历史自由文本/伪 1R 语义
    open_sz REAL, remaining_sz REAL, realized_pnl REAL NOT NULL DEFAULT 0, close_count INTEGER NOT NULL DEFAULT 0, closed_at TEXT,
    ever_hit_1r        INTEGER               -- 1=持仓期间曾达+1R | 0=有路径证据确证未达 | NULL=无价格路径证据（2026-08-10 新增；Wave2 MFE 埋点前恒 NULL）
)"""

TE_COLS = ("id, cycle_id, ts, profile, symbol, side, action, regime, "
           "regime_stale, score_total, confidence, playbook_ref, hypothesis_id, "
           "market_snapshot, experience_vector, pnl_pct, hold_hours, "
           "is_gross_profit_close, status, raw, experience_summary, open_sz, "
           "remaining_sz, realized_pnl, close_count, closed_at")

TE_OLD_COLS = TE_COLS.replace("is_gross_profit_close", "hit_1R")

TE_INDEXES = (
    "CREATE INDEX idx_experience_open_qty ON trade_experiences(profile,symbol,side,status,ts)",
    "CREATE INDEX idx_trade_exp_cycle ON trade_experiences(cycle_id)",
    "CREATE INDEX idx_trade_exp_prs ON trade_experiences(profile, regime, side)",
    "CREATE INDEX idx_trade_exp_sym_ts ON trade_experiences(symbol, ts)",
)

MO_NEW_DDL = """CREATE TABLE missed_opportunities_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    score           INTEGER NOT NULL,
    regime          TEXT,
    direction_hint  TEXT,
    actual_4h_pct   REAL,
    would_hit_1r_fixed2pct INTEGER,   -- 固定±2%代理口径"本可达1R"；2026-08-10 由 would_hit_1R 更名，与真实计划止损口径无关
    notes           TEXT,
    reviewed_utc    TEXT NOT NULL,
    decision_card   TEXT
)"""

MO_COLS = ("id, ts, symbol, score, regime, direction_hint, actual_4h_pct, "
           "would_hit_1r_fixed2pct, notes, reviewed_utc, decision_card")

MO_OLD_COLS = MO_COLS.replace("would_hit_1r_fixed2pct", "would_hit_1R")

MO_INDEXES = (
    "CREATE INDEX idx_missed_symbol_ts ON missed_opportunities(symbol, ts)",
    "CREATE INDEX idx_missed_ts ON missed_opportunities(ts)",
)

RISK_RE = re.compile(r"risk_pct=([\d.]+)")


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})")}


def backup(con: sqlite3.Connection, bdir: Path, name: str, tag: str) -> Path:
    path = bdir / f"{name}.bak_r-semantics_{tag}"
    bak = sqlite3.connect(str(path))
    with bak:
        con.backup(bak)
    qc = bak.execute("PRAGMA quick_check").fetchone()[0]
    bak.close()
    if qc != "ok":
        raise RuntimeError(f"备份 quick_check={qc}（{path}），中止")
    return path


def rebuild(con: sqlite3.Connection, old: str, new_ddl: str,
            copy_cols_new: str, copy_cols_old: str,
            indexes: tuple[str, ...]) -> int:
    n_before = con.execute(f"SELECT COUNT(*) FROM {old}").fetchone()[0]
    con.execute("BEGIN IMMEDIATE")
    con.execute(new_ddl)
    con.execute(
        f"INSERT INTO {old}_new({copy_cols_new}) "
        f"SELECT {copy_cols_old} FROM {old}")
    n_copied = con.execute(f"SELECT COUNT(*) FROM {old}_new").fetchone()[0]
    if n_copied != n_before:
        con.rollback()
        raise RuntimeError(f"{old} 拷贝行数不符 {n_copied}!={n_before}，已回滚")
    con.execute(f"DROP TABLE {old}")
    con.execute(f"ALTER TABLE {old}_new RENAME TO {old}")
    for idx in indexes:
        con.execute(idx)
    con.commit()
    return n_before


def legacy_risk_rows(con: sqlite3.Connection, col: str) -> list[dict]:
    """notes 声明 risk_pct≠2.000 的行：值不合 fixed2pct 口径。"""
    out = []
    for rid, notes, val in con.execute(
            f"SELECT id, notes, {col} FROM missed_opportunities "
            "WHERE notes LIKE '%risk_pct=%'"):
        m = RISK_RE.search(notes or "")
        if m and m.group(1) != "2.000" and val is not None:
            out.append({"id": rid, "risk_pct": m.group(1), "old_value": val})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="1R 假语义冻结迁移（默认 dry-run）")
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backup-dir", default=None,
                    help="--apply 必填：备份 account.db/lessons.db 的目录")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        ap.error("--apply and --dry-run are mutually exclusive")
    root = Path(args.db_root)
    acc_path, les_path = root / "account.db", root / "lessons.db"
    for p in (acc_path, les_path):
        if not p.exists():
            print(json.dumps({"ok": False, "error": f"库不存在: {p}"}))
            return 2

    acc = sqlite3.connect(str(acc_path), timeout=15)
    les = sqlite3.connect(str(les_path), timeout=15)
    for con in (acc, les):
        con.execute("PRAGMA busy_timeout=10000")
    try:
        te_cols = columns(acc, "trade_experiences")
        mo_cols = columns(les, "missed_opportunities")
        te_base_done = (
            "is_gross_profit_close" in te_cols and "ever_hit_1r" in te_cols
        )
        summary_version_done = "experience_summary_version" in te_cols
        te_done = te_base_done and summary_version_done
        mo_done = "would_hit_1r_fixed2pct" in mo_cols
        legacy = legacy_risk_rows(
            les, "would_hit_1r_fixed2pct" if mo_done else "would_hit_1R")

        report = {
            "dry_run": not args.apply,
            "trade_experiences": {
                "already_applied": te_done,
                "r_semantics_applied": te_base_done,
                "summary_version_applied": summary_version_done,
                "rows": acc.execute(
                    "SELECT COUNT(*) FROM trade_experiences").fetchone()[0],
            },
            "missed_opportunities": {
                "already_applied": mo_done,
                "rows": les.execute(
                    "SELECT COUNT(*) FROM missed_opportunities").fetchone()[0],
                "legacy_risk_rows_to_null": legacy,
            },
        }
        if te_done and mo_done and not legacy:
            print(json.dumps({**report, "ok": True, "action": "none"},
                             ensure_ascii=False, indent=1))
            return 0
        if not args.apply:
            print(json.dumps({**report, "ok": True, "action": "plan-only"},
                             ensure_ascii=False, indent=1))
            return 0
        if not args.backup_dir:
            print(json.dumps({**report, "ok": False,
                              "error": "--apply 必须配 --backup-dir"},
                             ensure_ascii=False, indent=1))
            return 2

        bdir = Path(args.backup_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        applied: dict = {}

        if not te_base_done:
            applied["account_backup"] = str(backup(acc, bdir, "account.db", tag))
            applied["trade_experiences_rows"] = rebuild(
                acc, "trade_experiences", TE_NEW_DDL, TE_COLS, TE_OLD_COLS,
                TE_INDEXES)
        elif not summary_version_done:
            applied["account_backup"] = str(backup(acc, bdir, "account.db", tag))
            acc.execute(
                "ALTER TABLE trade_experiences "
                "ADD COLUMN experience_summary_version INTEGER"
            )
            acc.commit()
            applied["experience_summary_version_added"] = True

        if not mo_done:
            applied["lessons_backup"] = str(backup(les, bdir, "lessons.db", tag))
            applied["missed_opportunities_rows"] = rebuild(
                les, "missed_opportunities", MO_NEW_DDL, MO_COLS, MO_OLD_COLS,
                MO_INDEXES)

        # 重建后再置 NULL 不合口径的历史行（幂等：值已 NULL 的不再动）
        legacy = legacy_risk_rows(les, "would_hit_1r_fixed2pct")
        les.execute("BEGIN IMMEDIATE")
        for row in legacy:
            les.execute(
                "UPDATE missed_opportunities SET would_hit_1r_fixed2pct=NULL, "
                "notes = notes || ? WHERE id=?",
                (f"；[2026-08-10 r-semantics 迁移] 原 would_hit_1R="
                 f"{row['old_value']} 按 risk_pct={row['risk_pct']} 计算，"
                 "不合 fixed2pct 口径置 NULL", row["id"]))
        les.commit()
        applied["legacy_rows_nulled"] = len(legacy)

        qc_acc = acc.execute("PRAGMA quick_check").fetchone()[0]
        qc_les = les.execute("PRAGMA quick_check").fetchone()[0]
        ok = (qc_acc == "ok" and qc_les == "ok"
              and "is_gross_profit_close" in columns(acc, "trade_experiences")
              and "ever_hit_1r" in columns(acc, "trade_experiences")
              and "experience_summary_version" in columns(
                  acc, "trade_experiences")
              and "would_hit_1r_fixed2pct" in columns(
                  les, "missed_opportunities"))
        print(json.dumps({**report, "ok": ok, "action": "applied",
                          "applied": applied,
                          "quick_check": {"account": qc_acc, "lessons": qc_les}},
                         ensure_ascii=False, indent=1))
        return 0 if ok else 2
    finally:
        acc.close()
        les.close()


if __name__ == "__main__":
    raise SystemExit(main())
