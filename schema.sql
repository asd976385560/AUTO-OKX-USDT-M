-- OKX 永续合约自主交易系统 - 数据库 Schema
-- 导出时间: 2026-05-16 21:56
-- 数据库目录: E:\OKX\db\
-- 本文件供 AI 读取表结构使用，不要手动编辑
-- 实际建表由 init_okx2.py 完成

-- ============================================================
-- 数据库: market.db
-- ============================================================

CREATE TABLE cross_market (
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

CREATE TABLE derivatives (
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

CREATE TABLE kline_cache (
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

CREATE TABLE tick_snapshots (
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

-- ============================================================
-- 数据库: news.db
-- ============================================================

CREATE TABLE coin_sentiment (
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

CREATE TABLE news_events_index (
    symbol      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    news_id     INTEGER NOT NULL,
    PRIMARY KEY (symbol, ts, news_id)
);

CREATE TABLE news_items (
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

CREATE TABLE sqlite_sequence(name,seq);

-- ============================================================
-- 数据库: account.db
-- ============================================================

CREATE TABLE account_snapshots (
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

CREATE TABLE cycle_runs (
    ts_start      TEXT PRIMARY KEY,
    ts_end        TEXT,
    job_id        TEXT,
    profile       TEXT,
    state_before  TEXT,
    state_after   TEXT,
    error         TEXT
);

CREATE TABLE daily_reports (
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

CREATE TABLE monthly_reports (
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

CREATE TABLE playbook (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    category        TEXT,
    summary         TEXT NOT NULL,
    evidence        TEXT,
    updated_utc     TEXT NOT NULL
);

CREATE TABLE position_snapshots (
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

CREATE TABLE quarterly_reports (
    quarter_start_ts TEXT NOT NULL,
    profile          TEXT NOT NULL DEFAULT 'live',
    total_pnl        REAL,
    summary          TEXT,
    lessons          TEXT,
    raw              TEXT,
    PRIMARY KEY (quarter_start_ts, profile)
);

CREATE TABLE records (
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

CREATE TABLE scoring_history (
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

CREATE TABLE sqlite_sequence(name,seq);

CREATE TABLE system_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_utc     TEXT NOT NULL
);

CREATE TABLE trade_events (
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

CREATE TABLE weekly_reports (
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

CREATE TABLE yearly_reports (
    year_start_ts    TEXT NOT NULL,
    profile          TEXT NOT NULL DEFAULT 'live',
    total_pnl        REAL,
    summary          TEXT,
    lessons          TEXT,
    raw              TEXT,
    PRIMARY KEY (year_start_ts, profile)
);

-- ============================================================
-- 数据库: lessons.db
-- ============================================================

CREATE TABLE error_patterns (
    pattern_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name      TEXT NOT NULL,
    trigger_condition TEXT,
    post_behavior     TEXT,
    hit_count         INTEGER DEFAULT 1,
    last_seen_utc     TEXT NOT NULL
);

CREATE TABLE missed_opportunities (
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

CREATE TABLE param_suggestions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion        TEXT NOT NULL,
    current_value     TEXT,
    suggested_value   TEXT,
    evidence          TEXT,
    status            TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
    created_utc       TEXT NOT NULL,
    decided_utc       TEXT
);

CREATE TABLE signal_perf (
    symbol        TEXT NOT NULL,
    dimension     TEXT NOT NULL,
    window_days   INTEGER NOT NULL,
    win_rate      REAL,
    sample_n      INTEGER,
    avg_return    REAL,
    updated_utc   TEXT NOT NULL,
    PRIMARY KEY (symbol, dimension, window_days)
);

CREATE TABLE sqlite_sequence(name,seq);

CREATE TABLE weekly_activity (
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
