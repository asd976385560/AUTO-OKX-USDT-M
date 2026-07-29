# -*- coding: utf-8 -*-
"""V2.0 §8.5 迁移 —— account.db.trade_experiences（交易经验库）。

现役 live/demo 成交均由 `collectors/trades_writer.py` 调用
`trade_experience_writer` 写入；与交易库采用独立事务。本脚本只建表 + 索引
（幂等），不写数据。

schema（§8.5）：
  基础：cycle_id/ts/profile/symbol/side/action/score_total/confidence
  市场快照：regime/regime_stale/market_snapshot(JSON)
  判定向量：experience_vector(JSON 数组，8–12 维混合)
  裁决（平仓时知）：pnl_pct/hold_hours/hit_1R
  关联：playbook_ref/hypothesis_id/status/raw(完整回执 JSON)
  L2 异步教训摘要：experience_summary（maintainer cron 落，不阻塞交易）
索引：(profile,regime,side)、(symbol,ts)。

用法：
  python apply_trade_experiences_schema.py --db-root <PROJECT_ROOT>\\db [--dry-run] [--verify]
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
import sys
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
import ledger  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DDL = """
CREATE TABLE IF NOT EXISTS trade_experiences (
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
    pnl_pct            REAL,                 -- 平仓时填
    hold_hours         REAL,
    hit_1R             INTEGER,              -- 1=达 1R | 0=否 | NULL=未平
    status             TEXT DEFAULT 'open',  -- open | closed | expired | superseded | orphaned
    open_sz            REAL,
    remaining_sz       REAL,
    realized_pnl       REAL NOT NULL DEFAULT 0,
    close_count        INTEGER NOT NULL DEFAULT 0,
    closed_at          TEXT,
    raw                TEXT,                 -- 完整回执 JSON（事实正本）
    experience_summary TEXT                  -- L2 异步：LLM 1-2 行教训（maintainer 落）
);
CREATE INDEX IF NOT EXISTS idx_trade_exp_prs ON trade_experiences(profile, regime, side);
CREATE INDEX IF NOT EXISTS idx_trade_exp_sym_ts ON trade_experiences(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_trade_exp_cycle ON trade_experiences(cycle_id);
CREATE INDEX IF NOT EXISTS idx_experience_open_qty
    ON trade_experiences(profile,symbol,side,status,ts);
"""


def existing_tables(con) -> set[str]:
    return {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def migrate(db_root: Path, dry_run: bool = False) -> dict:
    account = db_root / "account.db"
    if not account.exists():
        return {"ok": False, "error": f"account.db 不存在: {account}"}
    con = ledger.connect(account)
    try:
        had = "trade_experiences" in existing_tables(con)
        if dry_run:
            return {"ok": True, "dry_run": True, "already_exists": had}
        con.executescript(DDL)
        con.commit()
        return {"ok": True, "already_existed": had, "created": not had}
    finally:
        con.close()


def verify(db_root: Path) -> dict:
    con = ledger.connect(db_root / "account.db", readonly=True)
    try:
        has = "trade_experiences" in existing_tables(con)
        cols = [r["name"] for r in con.execute(
            "PRAGMA table_info(trade_experiences)")] if has else []
        idx = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='trade_experiences'")] if has else []
        return {"ok": has and len(cols) >= 18, "has_table": has,
                "n_cols": len(cols), "indexes": idx}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 trade_experiences 建表迁移")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    root = Path(args.db_root)

    res = migrate(root, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        return 1
    if args.verify and not args.dry_run:
        v = verify(root)
        print("-- verify --")
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0 if v["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
