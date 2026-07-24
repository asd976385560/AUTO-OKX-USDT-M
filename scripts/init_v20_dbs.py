# -*- coding: utf-8 -*-
"""V2.0 数据库初始化：按业务域建独立库 + WAL，结构上杜绝并发写冲突。

| 库            | 唯一 writer   | 读者          | 本脚本动作                         |
|:--------------|:--------------|:--------------|:-----------------------------------|
| regime.db     | 慢采脚本      | 分析员        | 建 cross_market（schema 同现 market.db）|
| analysis.db   | 分析员        | 双 trader     | 建 analysis_runs + analysis_signals |
| live_trades.db| 实盘 trader   | 复盘          | 建 trades + trade_cycles            |
| demo_trades.db| 模拟 trader   | 复盘          | 建 trades + trade_cycles（同构）    |
| ledger.db     | 各采集器      | 全体          | 委托 ledger.init_ledger             |

幂等（CREATE IF NOT EXISTS）。WAL/busy_timeout 复用 ledger.connect（单一来源）。

**只建表，不搬数据**：cross_market 从 market.db → regime.db 的行迁移是独立的、需主人
确认的生产步（见 docs/团队架构 切换清单），不在本脚本内做，避免误碰生产。

用法：
    python init_v20_dbs.py --root <PROJECT_ROOT>\\db          # 生产（建空库，不动现有库）
    python init_v20_dbs.py --root <tmp> --verify       # tmp 验证 schema + journal_mode
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import sys
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
import ledger  # noqa: E402  复用 connect()/init_ledger()（WAL 单一来源）

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# regime.db：cross_market 表 schema 严格对齐现 market.db（schema.sql v7.0e.5），
# 含那列 GENERATED 虚拟列，保证迁移行时列完全兼容。
DDL_REGIME = """
CREATE TABLE IF NOT EXISTS cross_market (
    ts            TEXT PRIMARY KEY,
    dxy           REAL,
    gold          REAL,
    vix           REAL,
    spx           REAL,
    btc_etf_flow  REAL,
    dxy_d1        REAL,
    vix_d1        REAL,
    defillama_tvl_total REAL,
    regime        TEXT,
    btc_dominance REAL,
    total_mcap_usd REAL,
    total_volume_24h_usd REAL,
    gold_d1       REAL,
    spx_d1        REAL,
    btc_mcap_chg_24h_usd REAL GENERATED ALWAYS AS (btc_etf_flow) VIRTUAL
);
"""

# analysis.db：统一 live agent 产出的结构化市场报告与自主决策卡。
DDL_ANALYSIS = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    cycle_id        TEXT PRIMARY KEY,   -- '2026-06-18T14:00'
    ts              TEXT NOT NULL,      -- 完成时刻 UTC+8 'YYYY-MM-DD HH:MM:SS'
    mode            TEXT NOT NULL,      -- 'full'
    regime          TEXT,               -- 本轮 regime
    regime_stale    INTEGER DEFAULT 0,  -- 1=沿用上一轮 regime（绝不静默空 regime）
    market_summary  TEXT,               -- 结构化市场综述
    missing_sources TEXT,               -- JSON：本轮缺的采集源（来自账本）
    status          TEXT DEFAULT 'ok',  -- 'ok'|'skipped'|'stale'|'error'（gate/降级状态，可 SELECT 过滤）
    raw             TEXT                 -- 完整结构化报告 JSON
);

CREATE TABLE IF NOT EXISTS analysis_signals (
    cycle_id     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    dim1 INTEGER, dim2 INTEGER, dim3 INTEGER, dim4 INTEGER, dim5 INTEGER,
    total        INTEGER,
    action       TEXT,               -- open_long|open_short|hold|close|wait
    side         TEXT,               -- long|short|null
    confidence   REAL,
    entry_hint   REAL,
    stop_hint    REAL,
    tp_hint      REAL,
    reasoning    TEXT,
    decision_card TEXT,             -- decision_card_v1 JSON；评分列仅作 schema 兼容
    raw          TEXT,
    PRIMARY KEY (cycle_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_analysis_signals_cycle ON analysis_signals(cycle_id);
"""

# live_trades.db / demo_trades.db：同构。每个 trader 是自己库的唯一 writer。
DDL_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id     TEXT,
    ts           TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    action       TEXT NOT NULL,      -- open|close|add|reduce|...
    side         TEXT,               -- long|short
    sz           REAL,
    fill_px      REAL,
    lev          REAL,
    margin       REAL,
    notional     REAL,
    score_total  INTEGER,
    reasoning    TEXT,
    deviation    TEXT,               -- 偏离分析建议的说明（若有）
    degradation  TEXT,               -- 本轮降级标记（数据缺失等）
    pnl          REAL,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_cycle ON trades(cycle_id);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades(symbol, ts);

-- 每轮决策留痕：即使没下单也写一行（复盘"为什么没动"），不静默。
CREATE TABLE IF NOT EXISTS trade_cycles (
    cycle_id     TEXT PRIMARY KEY,
    ts           TEXT NOT NULL,
    mode         TEXT,               -- live|demo（由 trades_writer 按 profile 写入）
    decision     TEXT,               -- traded|hold|skip|degraded
    n_orders     INTEGER DEFAULT 0,
    equity       REAL,
    note         TEXT,
    raw          TEXT
);
"""

TARGETS = [
    ("regime.db", DDL_REGIME),
    ("analysis.db", DDL_ANALYSIS),
    ("live_trades.db", DDL_TRADES),
    ("demo_trades.db", DDL_TRADES),
]


def init_db(path: Path, ddl: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = ledger.connect(path)            # WAL + busy_timeout + synchronous=NORMAL
    try:
        con.executescript(ddl)
        con.commit()
    finally:
        con.close()


def list_tables(path: Path) -> list[str]:
    con = ledger.connect(path, readonly=True)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [r["name"] for r in rows]
    finally:
        con.close()


def journal_mode(path: Path) -> str:
    con = ledger.connect(path, readonly=True)
    try:
        return con.execute("PRAGMA journal_mode;").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 数据库初始化（只建表不搬数据）")
    ap.add_argument("--root", default=_project_path('db'))
    ap.add_argument("--verify", action="store_true", help="建完打印 schema + journal_mode")
    args = ap.parse_args()
    root = Path(args.root)

    for fname, ddl in TARGETS:
        init_db(root / fname, ddl)
        print(f"init: {fname}")
    ledger.init_ledger(root / "ledger.db")
    print("init: ledger.db (collection_runs + stage_dispatch)")

    if args.verify:
        print("\n-- verify --")
        for fname, _ in TARGETS + [("ledger.db", None)]:
            p = root / fname
            jm = journal_mode(p)
            print(f"{fname:16s} journal={jm:5s} tables={list_tables(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
