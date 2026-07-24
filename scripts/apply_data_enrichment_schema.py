# -*- coding: utf-8 -*-
"""市场微观结构、宏观事件与来源治理 schema 迁移（幂等）。

只建表/补列，不搬数据：
  market.db:
    - market_microstructure：50档订单簿及深度/倾斜/滑点特征
    - market_trade_flow：最近逐笔成交样本的主动买卖流特征
  regime.db:
    - macro_events：经济日历
    - cross_market 补真实 ETF 净流、来源元数据和沿用标记
  account.db:
    - account_bills：手续费、资金费、已实现盈亏等交易所账单
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
import ledger  # noqa: E402


MARKET_DDL = """
CREATE TABLE IF NOT EXISTS market_microstructure (
    ts                       TEXT NOT NULL,
    cycle_id                 TEXT NOT NULL,
    symbol                   TEXT NOT NULL,
    depth_levels             INTEGER NOT NULL,
    best_bid                 REAL,
    best_ask                 REAL,
    mid_px                   REAL,
    spread_bps               REAL,
    bid_depth_10bp_usd       REAL,
    ask_depth_10bp_usd       REAL,
    bid_depth_25bp_usd       REAL,
    ask_depth_25bp_usd       REAL,
    bid_depth_50bp_usd       REAL,
    ask_depth_50bp_usd       REAL,
    imbalance_10bp           REAL,
    imbalance_25bp           REAL,
    imbalance_50bp           REAL,
    buy_slippage_100usd_bps  REAL,
    sell_slippage_100usd_bps REAL,
    buy_slippage_500usd_bps  REAL,
    sell_slippage_500usd_bps REAL,
    buy_slippage_1000usd_bps REAL,
    sell_slippage_1000usd_bps REAL,
    book_ts                  TEXT,
    seq_id                   INTEGER,
    raw_bids                 TEXT,
    raw_asks                 TEXT,
    source                   TEXT NOT NULL DEFAULT 'okx',
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_micro_symbol_ts
    ON market_microstructure(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_micro_cycle
    ON market_microstructure(cycle_id);

CREATE TABLE IF NOT EXISTS market_trade_flow (
    ts                 TEXT NOT NULL,
    cycle_id           TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    sample_count       INTEGER NOT NULL,
    sample_start       TEXT,
    sample_end         TEXT,
    sample_span_ms     INTEGER,
    buy_qty_contracts  REAL,
    sell_qty_contracts REAL,
    buy_notional_usd   REAL,
    sell_notional_usd  REAL,
    taker_buy_ratio    REAL,
    cvd_notional_usd   REAL,
    largest_trade_usd  REAL,
    raw_sample         TEXT,
    source             TEXT NOT NULL DEFAULT 'okx_recent_trades',
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_flow_symbol_ts
    ON market_trade_flow(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_flow_cycle
    ON market_trade_flow(cycle_id);
"""


REGIME_DDL = """
CREATE TABLE IF NOT EXISTS macro_events (
    calendar_id TEXT PRIMARY KEY,
    event_ts    TEXT NOT NULL,
    region      TEXT,
    category    TEXT,
    event       TEXT NOT NULL,
    importance INTEGER,
    forecast    TEXT,
    previous    TEXT,
    actual      TEXT,
    unit        TEXT,
    ref_date    TEXT,
    updated_at  TEXT,
    fetched_at  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'okx_economic_calendar',
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_macro_events_ts
    ON macro_events(event_ts);
CREATE INDEX IF NOT EXISTS idx_macro_events_importance_ts
    ON macro_events(importance, event_ts);
"""

REGIME_COLS = {
    "btc_etf_net_flow_usd": "REAL",
    "source_meta": "TEXT",
    "carried_forward": "TEXT",
}

ACCOUNT_DDL = """
CREATE TABLE IF NOT EXISTS account_bills (
    profile     TEXT NOT NULL,
    bill_id     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    inst_id     TEXT,
    inst_type   TEXT,
    ccy         TEXT,
    type        TEXT,
    subtype     TEXT,
    bal_change  REAL,
    fee         REAL,
    pnl         REAL,
    interest    REAL,
    px          REAL,
    sz          REAL,
    ord_id      TEXT,
    trade_id    TEXT,
    exec_type   TEXT,
    fetched_at  TEXT NOT NULL,
    raw         TEXT,
    PRIMARY KEY (profile, bill_id)
);
CREATE INDEX IF NOT EXISTS idx_account_bills_ts
    ON account_bills(profile, ts);
CREATE INDEX IF NOT EXISTS idx_account_bills_inst_ts
    ON account_bills(profile, inst_id, ts);
"""


def columns(con, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def migrate(root: Path, dry_run: bool = False) -> dict:
    result = {"ok": True, "dry_run": dry_run, "market": {}, "regime": {}}
    market_path = root / "market.db"
    regime_path = root / "regime.db"
    account_path = root / "account.db"
    if not market_path.exists() or not regime_path.exists() or not account_path.exists():
        return {"ok": False, "error": "market.db、regime.db 或 account.db 不存在"}

    if dry_run:
        mcon = ledger.connect(market_path, readonly=True)
        rcon = ledger.connect(regime_path, readonly=True)
        try:
            result["market"]["tables"] = [
                r["name"] for r in mcon.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('market_microstructure','market_trade_flow')")
            ]
            have = columns(rcon, "cross_market")
            result["regime"]["columns_to_add"] = sorted(set(REGIME_COLS) - have)
        finally:
            mcon.close()
            rcon.close()
        return result

    mcon = ledger.connect(market_path)
    try:
        mcon.executescript(MARKET_DDL)
        mcon.commit()
        result["market"]["tables"] = ["market_microstructure", "market_trade_flow"]
    finally:
        mcon.close()

    rcon = ledger.connect(regime_path)
    try:
        rcon.executescript(REGIME_DDL)
        have = columns(rcon, "cross_market")
        added = []
        for name, typ in REGIME_COLS.items():
            if name not in have:
                rcon.execute(f"ALTER TABLE cross_market ADD COLUMN {name} {typ}")
                added.append(name)
        rcon.commit()
        result["regime"] = {"table": "macro_events", "columns_added": added}
    finally:
        rcon.close()

    acon = ledger.connect(account_path)
    try:
        acon.executescript(ACCOUNT_DDL)
        acon.commit()
        result["account"] = {"table": "account_bills"}
    finally:
        acon.close()
    return result


def verify(root: Path) -> dict:
    mcon = ledger.connect(root / "market.db", readonly=True)
    rcon = ledger.connect(root / "regime.db", readonly=True)
    acon = ledger.connect(root / "account.db", readonly=True)
    try:
        mtables = {
            r["name"] for r in mcon.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        rtables = {
            r["name"] for r in rcon.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing_cols = sorted(set(REGIME_COLS) - columns(rcon, "cross_market"))
        missing_tables = sorted(
            {"market_microstructure", "market_trade_flow"} - mtables
        )
        if "macro_events" not in rtables:
            missing_tables.append("macro_events")
        atables = {
            r["name"] for r in acon.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "account_bills" not in atables:
            missing_tables.append("account_bills")
        return {
            "ok": not missing_tables and not missing_cols,
            "missing_tables": missing_tables,
            "missing_columns": missing_cols,
        }
    finally:
        mcon.close()
        rcon.close()
        acon.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="数据增强 schema 迁移")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    root = Path(args.db_root)
    res = migrate(root, args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        return 1
    if args.verify and not args.dry_run:
        checked = verify(root)
        print(json.dumps({"verify": checked}, ensure_ascii=False, indent=2))
        return 0 if checked["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
