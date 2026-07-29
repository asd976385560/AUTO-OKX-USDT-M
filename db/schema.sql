-- OKX 永续合约自主交易系统 - 数据库 Schema
-- 导出时间: 2026-07-28 23:09:19 CST
-- 版本: V2.0
-- 数据库目录: <PROJECT_ROOT>\db\
-- 本文件供 AI 读取表结构使用，不要手动编辑（改 schema 后跑 export_schema.py 重生成）
-- 核心拆分库由 init_v20_dbs.py 初始化；增量变更走 apply_* 幂等迁移脚本

-- ============================================================
-- 数据库: market.db
-- ============================================================

CREATE TABLE data_source_quality (
    source_id          TEXT PRIMARY KEY,
    source_name        TEXT NOT NULL,
    source_url         TEXT,
    data_type          TEXT NOT NULL,
    quality_score      REAL,
    freshness_min      INTEGER,
    accuracy_verified  INTEGER DEFAULT 0,
    last_checked_utc   TEXT NOT NULL,
    notes              TEXT
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

CREATE TABLE instruments_cache (
            instId    TEXT PRIMARY KEY,
            ctVal     REAL,
            lotSz     REAL
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

CREATE TABLE macro_registry (
    metric_id        TEXT PRIMARY KEY,
    metric_name      TEXT NOT NULL,
    category         TEXT NOT NULL,
    source_name      TEXT,
    source_url       TEXT,
    enabled          INTEGER DEFAULT 1,
    recommended_freq TEXT,
    last_checked_utc TEXT,
    notes            TEXT
);

CREATE TABLE market_microstructure (
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

CREATE TABLE market_positioning (
    ts               TEXT NOT NULL,
    collected_ts     TEXT NOT NULL,
    cycle_id         TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    timeframe        TEXT NOT NULL DEFAULT '1H',
    long_ratio       REAL,
    short_ratio      REAL,
    long_short_ratio REAL,
    raw              TEXT,
    source           TEXT NOT NULL DEFAULT 'okx_cli_top_long_short',
    PRIMARY KEY (ts, symbol, timeframe)
);

CREATE TABLE market_trade_flow (
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

CREATE TABLE tick_snapshots (
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    last        REAL,
    bid         REAL,
    ask         REAL,
    vol24h      REAL,
    fundingRate REAL,
    oi          REAL, chg24h REAL,
    PRIMARY KEY (ts, symbol)
);

CREATE INDEX idx_derivatives_symbol_ts ON derivatives(symbol, ts);

CREATE INDEX idx_flow_cycle
    ON market_trade_flow(cycle_id);

CREATE INDEX idx_flow_symbol_ts
    ON market_trade_flow(symbol, ts);

CREATE INDEX idx_kline_symbol_tf_ts ON kline_cache(symbol, tf, ts);

CREATE INDEX idx_micro_cycle
    ON market_microstructure(cycle_id);

CREATE INDEX idx_micro_symbol_ts
    ON market_microstructure(symbol, ts);

CREATE INDEX idx_positioning_cycle
    ON market_positioning(cycle_id);

CREATE INDEX idx_positioning_symbol_ts
    ON market_positioning(symbol, ts);

CREATE INDEX idx_tick_symbol_ts ON tick_snapshots(symbol, ts);

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
, ingested_at TEXT, event_time TEXT, severity TEXT, tags TEXT);

CREATE INDEX idx_coin_sentiment_symbol_ts ON coin_sentiment(symbol, ts);

CREATE INDEX idx_news_event_time ON news_items(event_time);

CREATE UNIQUE INDEX idx_news_items_hash ON news_items(hash);

CREATE INDEX idx_news_level_ts ON news_items(level, ts);

CREATE INDEX idx_news_severity_ts ON news_items(severity, ts);

CREATE INDEX idx_news_symbol_ts ON news_items(symbol, ts);

CREATE INDEX idx_news_ts ON news_items(ts);

-- ============================================================
-- 数据库: account.db
-- ============================================================

CREATE TABLE account_bills (
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

CREATE TABLE cycle_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gap_start_cycle INTEGER NOT NULL,
                gap_end_cycle INTEGER NOT NULL,
                missing_count INTEGER NOT NULL,
                reason TEXT,
                detected_at TEXT NOT NULL,
                filled INTEGER DEFAULT 0
            );

CREATE TABLE cycle_runs (
    ts_start      TEXT PRIMARY KEY,
    ts_end        TEXT,
    job_id        TEXT,
    profile       TEXT,
    state_before  TEXT,
    state_after   TEXT,
    error         TEXT
, cycle_count INTEGER, cycle_start_time TEXT, cycle_end_time TEXT, cycle_duration_s INTEGER, initial_equity REAL, current_equity REAL, return_pct REAL, positions_snapshot TEXT, regime TEXT, action_taken TEXT, confidence REAL, session_baseline REAL, baseline_set_at TEXT, session_return_pct REAL, session_pnl REAL);

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
    raw             TEXT, trade_day_num INTEGER,
    PRIMARY KEY (ts, profile)
);

CREATE TABLE doc_versions (
    doc_path       TEXT PRIMARY KEY,
    doc_version    TEXT NOT NULL,
    last_updated   TEXT NOT NULL,
    updated_by     TEXT,
    change_summary TEXT
);

CREATE TABLE hypotheses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id TEXT, ts TEXT, hypothesis_id TEXT,
            hypothesis TEXT, falsifiable_condition TEXT,
            confidence TEXT, rationale TEXT,
            status TEXT DEFAULT 'open'
        );

CREATE TABLE monthly_reports (
    month_start_ts  TEXT NOT NULL,
    profile         TEXT NOT NULL DEFAULT 'live',
    total_pnl       REAL,
    max_drawdown    REAL,
    sharpe_approx   REAL,
    summary         TEXT,
    lessons         TEXT,
    raw             TEXT, trade_month_num INTEGER,
    PRIMARY KEY (month_start_ts, profile)
);

CREATE TABLE playbook (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    category        TEXT,
    summary         TEXT NOT NULL,
    evidence        TEXT,
    updated_utc     TEXT NOT NULL
, evidence_count INTEGER DEFAULT 0, win_count INTEGER DEFAULT 0, loss_count INTEGER DEFAULT 0, win_rate REAL, avg_pnl_pct REAL, last_validated_cycle INTEGER);

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

CREATE TABLE repair_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    issue       TEXT NOT NULL,
    fix_action  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    cycle_id    INTEGER,
    created_utc TEXT NOT NULL
, closed_at TEXT, closed_by TEXT, resolution TEXT);

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

CREATE TABLE system_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_utc     TEXT NOT NULL
);

CREATE TABLE tmp_cleanup_runs (
    run_utc          TEXT PRIMARY KEY,
    dry_run          INTEGER NOT NULL,
    scanned          INTEGER NOT NULL,
    kept_recent      INTEGER NOT NULL,
    skipped          INTEGER NOT NULL,
    archived         INTEGER NOT NULL,
    bytes_archived   INTEGER NOT NULL,
    archive_dir      TEXT,
    raw_json         TEXT
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

CREATE TABLE trade_experiences (
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
    status             TEXT DEFAULT 'open',  -- open | closed
    raw                TEXT,                 -- 完整回执 JSON（事实正本）
    experience_summary TEXT                  -- L2 异步：LLM 1-2 行教训（maintainer 落）
, open_sz REAL, remaining_sz REAL, realized_pnl REAL NOT NULL DEFAULT 0, close_count INTEGER NOT NULL DEFAULT 0, closed_at TEXT);

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
    raw             TEXT, trade_week_num INTEGER,
    PRIMARY KEY (week_start_ts, profile)
);

CREATE INDEX idx_account_bills_inst_ts
    ON account_bills(profile, inst_id, ts);

CREATE INDEX idx_account_bills_ts
    ON account_bills(profile, ts);

CREATE INDEX idx_acct_profile_ts ON account_snapshots(profile, ts);

CREATE INDEX idx_cycle_jobid_ts ON cycle_runs(job_id, ts_start);

CREATE INDEX idx_cycle_runs_baseline ON cycle_runs(baseline_set_at);

CREATE INDEX idx_dr_ts ON daily_reports(ts);

CREATE INDEX idx_experience_open_qty ON trade_experiences(profile,symbol,side,status,ts);

CREATE INDEX idx_playbook_category ON playbook(category);

CREATE INDEX idx_playbook_ts ON playbook(ts);

CREATE INDEX idx_pos_profile_ts ON position_snapshots(profile, ts);

CREATE INDEX idx_pos_symbol_ts ON position_snapshots(symbol, ts);

CREATE INDEX idx_repair_queue_check ON repair_queue(check_name);

CREATE INDEX idx_repair_queue_status ON repair_queue(status);

CREATE INDEX idx_repair_queue_ts ON repair_queue(ts);

CREATE INDEX idx_score_symbol_ts ON scoring_history(symbol, ts);

CREATE INDEX idx_sys_state_key ON system_state(key);

CREATE INDEX idx_te_action_ts ON trade_events(action, ts);

CREATE INDEX idx_te_symbol_ts ON trade_events(symbol, ts);

CREATE INDEX idx_te_ts ON trade_events(ts);

CREATE INDEX idx_trade_exp_cycle ON trade_experiences(cycle_id);

CREATE INDEX idx_trade_exp_prs ON trade_experiences(profile, regime, side);

CREATE INDEX idx_trade_exp_sym_ts ON trade_experiences(symbol, ts);

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
, outcome TEXT, outcome_note TEXT, retired INTEGER DEFAULT 0);

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
, decision_card TEXT);

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

CREATE INDEX idx_error_patterns_name ON error_patterns(pattern_name);

CREATE INDEX idx_missed_symbol_ts ON missed_opportunities(symbol, ts);

CREATE INDEX idx_missed_ts ON missed_opportunities(ts);

CREATE INDEX idx_param_suggestions_status ON param_suggestions(status);

CREATE INDEX idx_signal_perf_updated ON signal_perf(updated_utc);

-- ============================================================
-- 数据库: drill.db
-- ============================================================

CREATE TABLE drill_account_snapshots (
        ts TEXT PRIMARY KEY,
        total_eq REAL,
        avail_bal REAL,
        margin_used REAL,
        position_count INTEGER
    , profile TEXT DEFAULT 'demo');

CREATE TABLE drill_cycle_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_start TEXT NOT NULL,
        ts_end TEXT,
        drill_balance REAL,
        drill_margin_used REAL,
        open_positions INTEGER,
        trades_opened INTEGER,
        trades_closed INTEGER,
        error TEXT
    , profile TEXT DEFAULT 'demo');

CREATE TABLE drill_learnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        category TEXT,
        lesson TEXT NOT NULL,
        evidence_trade_ids TEXT,
        confidence TEXT,
        synced_to_lessons INTEGER DEFAULT 0
    , trade_id INTEGER);

CREATE TABLE drill_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT,
    entry_reason TEXT,
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    risk_pct REAL,
    score_total REAL,
    score_rsi REAL,
    regime TEXT,
    notes TEXT,
    status TEXT DEFAULT 'pending',
    created_utc TEXT NOT NULL
);

CREATE TABLE drill_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        sz REAL NOT NULL,
        lever INTEGER NOT NULL,
        mark_px REAL,
        ct_val REAL,
        margin REAL,
        notional REAL,
        entry_reason TEXT,
        stop_loss_px REAL,
        close_px REAL,
        close_ts TEXT,
        pnl REAL,
        pnl_pct REAL,
        status TEXT DEFAULT 'open',
        source_suggestion_id INTEGER
    , close_reason TEXT, profile TEXT DEFAULT 'demo', cycle_id INTEGER);

-- ============================================================
-- 数据库: regime.db
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
    total_volume_24h_usd REAL,
    gold_d1       REAL,
    spx_d1        REAL,
    btc_mcap_chg_24h_usd REAL GENERATED ALWAYS AS (btc_etf_flow) VIRTUAL
, btc_etf_net_flow_usd REAL, source_meta TEXT, carried_forward TEXT, dxy_calc_ecb REAL, dxy_calc_ecb_d1 REAL, fear_greed REAL, fear_greed_label TEXT);

CREATE TABLE macro_events (
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

CREATE TABLE macro_observations (
    metric           TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    source           TEXT NOT NULL,
    collected_at     TEXT NOT NULL,
    value            REAL,
    unit             TEXT,
    label            TEXT,
    status           TEXT NOT NULL,
    source_url       TEXT,
    raw              TEXT,
    PRIMARY KEY (metric, observation_date, source)
);

CREATE INDEX idx_macro_events_importance_ts
    ON macro_events(importance, event_ts);

CREATE INDEX idx_macro_events_ts
    ON macro_events(event_ts);

CREATE INDEX idx_macro_observations_metric_date
    ON macro_observations(metric, observation_date DESC);

CREATE INDEX idx_macro_observations_source_date
    ON macro_observations(source, observation_date DESC);

-- ============================================================
-- 数据库: analysis.db
-- ============================================================

CREATE TABLE analysis_runs (
    cycle_id        TEXT PRIMARY KEY,   -- '2026-06-18T14:00'
    ts              TEXT NOT NULL,      -- 完成时刻 UTC+8 'YYYY-MM-DD HH:MM:SS'
    mode            TEXT NOT NULL,      -- 'full' | 'partial'
    regime          TEXT,               -- 本轮 regime（partial 可能 carry-forward）
    regime_stale    INTEGER DEFAULT 0,  -- 1=沿用上一轮 regime（绝不静默空 regime）
    market_summary  TEXT,               -- 结构化市场综述
    missing_sources TEXT,               -- JSON：本轮缺的采集源（来自账本）
    raw             TEXT                 -- 完整结构化报告 JSON
, status TEXT DEFAULT 'ok');

CREATE TABLE analysis_signals (
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
    raw          TEXT, decision_card TEXT,
    PRIMARY KEY (cycle_id, symbol)
);

CREATE INDEX idx_analysis_signals_cycle ON analysis_signals(cycle_id);

-- ============================================================
-- 数据库: live_trades.db
-- ============================================================

CREATE TABLE trade_cycles (
    cycle_id     TEXT PRIMARY KEY,
    ts           TEXT NOT NULL,
    mode         TEXT,               -- live|demo（由 trades_writer 按 profile 写入）
    decision     TEXT,               -- traded|hold|skip|degraded
    n_orders     INTEGER DEFAULT 0,
    equity       REAL,
    note         TEXT,
    raw          TEXT
);

CREATE TABLE trades (
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
    degradation  TEXT,               -- 本轮降级标记（partial / 数据缺失）
    pnl          REAL,
    raw          TEXT
);

CREATE INDEX idx_trades_cycle ON trades(cycle_id);

CREATE INDEX idx_trades_symbol_ts ON trades(symbol, ts);

-- ============================================================
-- 数据库: demo_trades.db
-- ============================================================

CREATE TABLE trade_cycles (
    cycle_id     TEXT PRIMARY KEY,
    ts           TEXT NOT NULL,
    mode         TEXT,               -- live|demo（由 trades_writer 按 profile 写入）
    decision     TEXT,               -- traded|hold|skip|degraded
    n_orders     INTEGER DEFAULT 0,
    equity       REAL,
    note         TEXT,
    raw          TEXT
);

CREATE TABLE trades (
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
    degradation  TEXT,               -- 本轮降级标记（partial / 数据缺失）
    pnl          REAL,
    raw          TEXT
);

CREATE INDEX idx_trades_cycle ON trades(cycle_id);

CREATE INDEX idx_trades_symbol_ts ON trades(symbol, ts);

-- ============================================================
-- 数据库: ledger.db
-- ============================================================

CREATE TABLE collection_runs (
                cycle_id   TEXT NOT NULL,   -- 槽位归一时间戳 'YYYY-MM-DDTHH:MM'（UTC+8）
                source     TEXT NOT NULL,   -- 'fast' | 'slow' | 'regime' | 'x_search'
                status     TEXT NOT NULL,   -- 'ok' | 'degraded' | 'timeout' | 'error'
                ts         TEXT NOT NULL,   -- 实际完成时刻（UTC+8 'YYYY-MM-DD HH:MM:SS'）
                rows       INTEGER,
                latency_ms INTEGER,
                err        TEXT,
                PRIMARY KEY (cycle_id, source)
            );

CREATE TABLE execution_intents (
                profile             TEXT NOT NULL,
                cycle_id            TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                action              TEXT NOT NULL,
                side                TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                request_json        TEXT NOT NULL,
                state               TEXT NOT NULL,
                reserved_at         TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                submitted_at        TEXT,
                completed_at        TEXT,
                ord_id              TEXT,
                receipt_json        TEXT,
                error               TEXT,
                PRIMARY KEY (profile, cycle_id, symbol, action, side)
            );

CREATE TABLE stage_dispatch (
                cycle_id      TEXT NOT NULL,
                stage         TEXT NOT NULL,      -- 'live' | 'demo' | 'push'
                dispatched_at TEXT NOT NULL,
                card_id       TEXT,
                PRIMARY KEY (cycle_id, stage)
            );

CREATE INDEX idx_cr_cycle ON collection_runs(cycle_id);

CREATE INDEX idx_execution_intents_state
                ON execution_intents(state, updated_at);

CREATE INDEX idx_sd_cycle ON stage_dispatch(cycle_id);

-- ============================================================
-- 数据库: qq_push_dedupe.db
-- ============================================================

CREATE TABLE sent (k TEXT PRIMARY KEY, content_hash TEXT, status TEXT, first_seen TEXT, updated_at TEXT, preview TEXT);
