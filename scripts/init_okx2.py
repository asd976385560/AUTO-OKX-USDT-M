# -*- coding: utf-8 -*-
"""
init_okx2.py - OKX v2.0 trade system first-run initialization

Usage:
    pwsh -Command "python scripts/init_okx2.py --root <ROOT> --db-dir <DB_DIR>"

What it does:
    1. Validate config.md required placeholders are filled
    2. Create all required directories
    3. Initialize 4 SQLite databases (market / news / account / lessons)
    4. Create all tables and indexes
    5. Write init audit record

Idempotent: safe to re-run, will not destroy existing data.
"""

from __future__ import annotations

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        if _s.encoding and _s.encoding.lower() != "utf-8":
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# -- Directories -----------------------------------------------------------
# All report subdirs go under reports/ to keep root clean

REQUIRED_DIRS = [
    "db",
    "scripts",
    "reports",
    "reports/daily",
    "reports/weekly",
    "reports/monthly",
    "reports/quarterly",
    "reports/yearly",
    "reports/records",
    "reports/trade-events",
    "reports/self-reviews",
]

# -- SQL: market.db --------------------------------------------------------

MARKET_SQL = """
CREATE TABLE IF NOT EXISTS tick_snapshots (
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    last        REAL,
    bid         REAL,
    ask         REAL,
    vol24h      REAL,
    fundingRate REAL,
    oi          REAL,
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_tick_symbol_ts ON tick_snapshots(symbol, ts);

CREATE TABLE IF NOT EXISTS kline_cache (
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    tf          TEXT NOT NULL CHECK(tf IN ('15m','1H','4H','1D','1W','1M')),
    o           REAL, h REAL, l REAL, c REAL, v REAL,
    ma5         REAL,
    ma20        REAL,
    atr14       REAL,
    rsi14       REAL,
    macd_hist   REAL,
    PRIMARY KEY (ts, symbol, tf)
);
CREATE INDEX IF NOT EXISTS idx_kline_symbol_tf_ts ON kline_cache(symbol, tf, ts);

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
    total_volume_24h_usd REAL
);

CREATE TABLE IF NOT EXISTS derivatives (
    ts                TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    funding_rate      REAL,
    funding_time      TEXT,
    next_funding_time TEXT,
    premium           REAL,
    oi                REAL,
    oi_ccy            REAL,
    oi_usd            REAL,
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_derivatives_symbol_ts ON derivatives(symbol, ts);
"""

# -- SQL: news.db ----------------------------------------------------------

NEWS_SQL = """
CREATE TABLE IF NOT EXISTS news_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    source      TEXT,
    hash        TEXT,
    level       TEXT NOT NULL CHECK(level IN ('A','B','C')),
    symbol      TEXT,
    title       TEXT NOT NULL,
    url         TEXT,
    sentiment   REAL,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_items(ts);
CREATE INDEX IF NOT EXISTS idx_news_symbol_ts ON news_items(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_news_level_ts ON news_items(level, ts);

CREATE TABLE IF NOT EXISTS news_events_index (
    symbol      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    news_id     INTEGER NOT NULL,
    PRIMARY KEY (symbol, ts, news_id)
);

CREATE TABLE IF NOT EXISTS coin_sentiment (
    ts                TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    period            TEXT NOT NULL,
    label             TEXT,
    bullish_ratio     REAL,
    bearish_ratio     REAL,
    bullish_cnt       INTEGER,
    bearish_cnt       INTEGER,
    neutral_cnt       INTEGER,
    mention_cnt       INTEGER,
    news_mention_cnt  INTEGER,
    x_mention_cnt     INTEGER,
    raw               TEXT,
    PRIMARY KEY (ts, symbol, period)
);
CREATE INDEX IF NOT EXISTS idx_coin_sentiment_symbol_ts ON coin_sentiment(symbol, ts);
"""

# -- SQL: account.db -------------------------------------------------------

ACCOUNT_SQL = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    ts         TEXT NOT NULL,
    profile    TEXT NOT NULL DEFAULT 'live',
    totalEq    REAL,
    availBal   REAL,
    upl        REAL,
    daily_pnl  REAL,
    week_pnl   REAL,
    month_pnl  REAL,
    PRIMARY KEY (ts, profile)
);
CREATE INDEX IF NOT EXISTS idx_acct_profile_ts ON account_snapshots(profile, ts);

CREATE TABLE IF NOT EXISTS position_snapshots (
    ts            TEXT NOT NULL,
    profile       TEXT NOT NULL DEFAULT 'live',
    symbol        TEXT NOT NULL,
    side          TEXT CHECK(side IN ('long','short')),
    sz            REAL,
    avgPx         REAL,
    lev           REAL,
    liqPx         REAL,
    upl           REAL,
    marginRatio   REAL,
    PRIMARY KEY (ts, profile, symbol)
);
CREATE INDEX IF NOT EXISTS idx_pos_symbol_ts ON position_snapshots(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_pos_profile_ts ON position_snapshots(profile, ts);

CREATE TABLE IF NOT EXISTS scoring_history (
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    dim1          INTEGER,
    dim2          INTEGER,
    dim3          INTEGER,
    dim4          INTEGER,
    dim5          INTEGER,
    total         INTEGER,
    action        TEXT,
    ai_reasoning  TEXT,
    regime        TEXT,
    side          TEXT,
    open_fill_px  REAL,
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_score_symbol_ts ON scoring_history(symbol, ts);

CREATE TABLE IF NOT EXISTS cycle_runs (
    ts_start      TEXT PRIMARY KEY,
    ts_end        TEXT,
    job_id        TEXT,
    profile       TEXT,
    state_before  TEXT,
    state_after   TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_cycle_jobid_ts ON cycle_runs(job_id, ts_start);

CREATE TABLE IF NOT EXISTS trade_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    profile         TEXT NOT NULL DEFAULT 'live',
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,
    side            TEXT,
    sz              REAL,
    fill_px         REAL,
    score_total     INTEGER,
    ai_reasoning    TEXT,
    ai_deviation    TEXT,
    degradation     TEXT,
    channel         TEXT DEFAULT 'main',
    pnl             REAL,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_te_ts ON trade_events(ts);
CREATE INDEX IF NOT EXISTS idx_te_symbol_ts ON trade_events(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_te_action_ts ON trade_events(action, ts);

CREATE TABLE IF NOT EXISTS daily_reports (
    ts              TEXT NOT NULL,
    profile         TEXT NOT NULL DEFAULT 'live',
    open_count      INTEGER DEFAULT 0,
    close_count     INTEGER DEFAULT 0,
    total_pnl       REAL,
    total_fees      REAL,
    best_trade      TEXT,
    worst_trade     TEXT,
    summary         TEXT,
    lessons         TEXT,
    raw             TEXT,
    PRIMARY KEY (ts, profile)
);
CREATE INDEX IF NOT EXISTS idx_dr_ts ON daily_reports(ts);

CREATE TABLE IF NOT EXISTS weekly_reports (
    week_start_ts   TEXT NOT NULL,
    profile         TEXT NOT NULL DEFAULT 'live',
    open_count      INTEGER DEFAULT 0,
    close_count     INTEGER DEFAULT 0,
    total_pnl       REAL,
    win_rate        REAL,
    avg_hold_hours  REAL,
    margin_util_pct REAL,
    idle_ratio      REAL,
    summary         TEXT,
    lessons         TEXT,
    raw             TEXT,
    PRIMARY KEY (week_start_ts, profile)
);

CREATE TABLE IF NOT EXISTS monthly_reports (
    month_start_ts  TEXT NOT NULL,
    profile         TEXT NOT NULL DEFAULT 'live',
    total_pnl       REAL,
    max_drawdown    REAL,
    sharpe_approx   REAL,
    summary         TEXT,
    lessons         TEXT,
    raw             TEXT,
    PRIMARY KEY (month_start_ts, profile)
);

CREATE TABLE IF NOT EXISTS quarterly_reports (
    quarter_start_ts TEXT NOT NULL,
    profile          TEXT NOT NULL DEFAULT 'live',
    total_pnl        REAL,
    summary          TEXT,
    lessons          TEXT,
    raw              TEXT,
    PRIMARY KEY (quarter_start_ts, profile)
);

CREATE TABLE IF NOT EXISTS yearly_reports (
    year_start_ts    TEXT NOT NULL,
    profile          TEXT NOT NULL DEFAULT 'live',
    total_pnl        REAL,
    summary          TEXT,
    lessons          TEXT,
    raw              TEXT,
    PRIMARY KEY (year_start_ts, profile)
);

CREATE TABLE IF NOT EXISTS records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    profile         TEXT NOT NULL DEFAULT 'live',
    plan_id         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_px        REAL,
    exit_px         REAL,
    sz              REAL,
    leverage        REAL,
    pnl             REAL,
    pnl_pct         REAL,
    hold_hours      REAL,
    ai_reasoning    TEXT,
    ai_deviation    TEXT,
    degradation     TEXT,
    channel         TEXT DEFAULT 'main',
    score_total     INTEGER,
    execution_check TEXT,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_ts ON records(ts);
CREATE INDEX IF NOT EXISTS idx_records_symbol_ts ON records(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_records_plan ON records(plan_id);

CREATE TABLE IF NOT EXISTS system_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_utc     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sys_state_key ON system_state(key);

CREATE TABLE IF NOT EXISTS playbook (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    category        TEXT,
    summary         TEXT NOT NULL,
    evidence        TEXT,
    updated_utc     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playbook_ts ON playbook(ts);
CREATE INDEX IF NOT EXISTS idx_playbook_category ON playbook(category);
"""

# -- SQL: lessons.db -------------------------------------------------------

LESSONS_SQL = """
CREATE TABLE IF NOT EXISTS signal_perf (
    symbol        TEXT NOT NULL,
    dimension     TEXT NOT NULL,
    window_days   INTEGER NOT NULL,
    win_rate      REAL,
    sample_n      INTEGER,
    avg_return    REAL,
    updated_utc   TEXT NOT NULL,
    PRIMARY KEY (symbol, dimension, window_days)
);
CREATE INDEX IF NOT EXISTS idx_signal_perf_updated ON signal_perf(updated_utc);

CREATE TABLE IF NOT EXISTS error_patterns (
    pattern_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name      TEXT NOT NULL,
    trigger_condition TEXT,
    post_behavior     TEXT,
    hit_count         INTEGER DEFAULT 1,
    last_seen_utc     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_error_patterns_name ON error_patterns(pattern_name);

CREATE TABLE IF NOT EXISTS param_suggestions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion        TEXT NOT NULL,
    current_value     TEXT,
    suggested_value   TEXT,
    evidence          TEXT,
    status            TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
    created_utc       TEXT NOT NULL,
    decided_utc       TEXT
);
CREATE INDEX IF NOT EXISTS idx_param_suggestions_status ON param_suggestions(status);

CREATE TABLE IF NOT EXISTS missed_opportunities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    score           INTEGER NOT NULL,
    regime          TEXT,
    direction_hint  TEXT,
    actual_4h_pct   REAL,
    would_hit_1R    INTEGER,
    notes           TEXT,
    reviewed_utc    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_missed_ts ON missed_opportunities(ts);
CREATE INDEX IF NOT EXISTS idx_missed_symbol_ts ON missed_opportunities(symbol, ts);

CREATE TABLE IF NOT EXISTS weekly_activity (
    week_start_utc    TEXT PRIMARY KEY,
    open_count        INTEGER DEFAULT 0,
    close_count       INTEGER DEFAULT 0,
    avg_hold_hours    REAL,
    margin_util_pct   REAL,
    idle_ratio        REAL,
    over_conservative INTEGER DEFAULT 0,
    notes             TEXT,
    updated_utc       TEXT NOT NULL
);
"""

# -- Config validation -----------------------------------------------------




def validate_config(config_path: Path) -> list[str]:
    """Check config.md for unfilled placeholders. Returns list of errors."""
    errors = []
    if not config_path.exists():
        errors.append(f"[FATAL] config.md not found at: {config_path}")
        return errors

    content = config_path.read_text(encoding="utf-8")

    # Check for __FILL__ placeholders
    fill_pattern = re.compile(r"\|.*`__FILL__`.*\|")
    for match in fill_pattern.finditer(content):
        # Extract the key name from the row
        row = match.group()
        key_match = re.search(r"\|\s*([^|]+?)\s*\|", row)
        if key_match:
            key = key_match.group(1).strip()
            errors.append(f"[REQUIRED] {key} - must be filled in config.md")

    # Also check standalone __FILL__ not in table
    standalone = re.findall(r"`__FILL__`", content)
    table_fills = len(fill_pattern.findall(content))
    if len(standalone) > table_fills:
        errors.append("[REQUIRED] Found __FILL__ placeholders outside table rows in config.md")

    # Check critical paths
    root_match = re.search(r"项目根目录\s*\|\s*`([^`]+)`", content)
    db_match = re.search(r"数据库目录\s*\|\s*`([^`]+)`", content)
    if not root_match or root_match.group(1) == "__FILL__":
        errors.append("[FATAL] PROJECT_ROOT not set in config.md")
    if not db_match or db_match.group(1) == "__FILL__":
        errors.append("[FATAL] DB_DIR not set in config.md")

    return errors


# -- Main logic ------------------------------------------------------------

DB_MAP = {
    "market.db": MARKET_SQL,
    "news.db": NEWS_SQL,
    "account.db": ACCOUNT_SQL,
    "lessons.db": LESSONS_SQL,
}


def init(root: Path, db_dir: Path, skip_config_check: bool = False) -> None:
    # 0. Config validation
    if not skip_config_check:
        config_path = root / "config.md"
        errors = validate_config(config_path)
        if errors:
            print("\n[CONFIG VALIDATION FAILED]")
            print("Please fix the following in config.md before initializing:\n")
            for e in errors:
                print(f"  {e}")
            print("\nRun with --skip-config-check to bypass (not recommended).")
            sys.exit(1)
        else:
            print("[OK] config.md validation passed")

    # 1. Create directories
    for d in REQUIRED_DIRS:
        target = root / d
        target.mkdir(parents=True, exist_ok=True)
        print(f"  [DIR]  {target}")

    db_dir.mkdir(parents=True, exist_ok=True)

    # 2. Initialize databases
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for db_name, sql in DB_MAP.items():
        db_path = db_dir / db_name
        conn = sqlite3.connect(str(db_path))
        conn.executescript(sql)
        # cycle_runs only in account.db
        if db_name == "account.db":
            conn.execute(
                "INSERT OR REPLACE INTO cycle_runs (ts_start, job_id, profile, state_before, state_after) "
                "VALUES (?, 'init_okx2', 'live', NULL, 'INITIALIZED')",
                (now,),
            )
            # Initialize system_state with default values
            defaults = {
                "state": "IDLE",
                "profile": "live",
                "initialized_utc": now,
                "idle_consecutive_low_count": "0",
                "confidence_adjust_enabled": "true",
                "dynamic_threshold_enabled": "true",
            }
            for k, v in defaults.items():
                conn.execute(
                    "INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
                    (k, v, now),
                )
        conn.commit()
        conn.close()
        size = db_path.stat().st_size
        print(f"  [DB]   {db_path} ({size:,} bytes)")

    # 3. Create playbook.md placeholder (kept for backwards compat, primary is DB)
    playbook = root / "playbook.md"
    if not playbook.exists():
        playbook.write_text(
            "# Playbook\n\n> Last 30 experience entries, maintained by self_review.py\n> Primary storage: account.db.playbook\n\n---\n",
            encoding="utf-8",
        )
        print(f"  [FILE] {playbook}")

    print(f"\n[DONE] Init complete at {now} UTC")
    print(f"       Databases: {db_dir}")
    print(f"       Tables created: 26 (market:4, news:3, account:14, lessons:5)")
    print(f"\n[NEXT] Set up cron jobs as described in config.md")


def main() -> None:
    parser = argparse.ArgumentParser(description="OKX v2.0 init script")
    parser.add_argument(
        "--root",
        required=True,
        help="Project root dir (e.g. E:\\OKX)",
    )
    parser.add_argument(
        "--db-dir",
        required=True,
        help="Database dir (e.g. E:\\OKX\\db)",
    )
    parser.add_argument(
        "--skip-config-check",
        action="store_true",
        help="Skip config.md validation (not recommended)",
    )
    args = parser.parse_args()

    root = Path(args.root)
    db_dir = Path(args.db_dir)

    print("[OKX v2.0] Init start")
    print(f"   root: {root}")
    print(f"   db:   {db_dir}\n")

    init(root, db_dir, skip_config_check=args.skip_config_check)


if __name__ == "__main__":
    main()
