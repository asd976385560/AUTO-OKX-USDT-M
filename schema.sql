-- OKX 自主交易系统 SQLite 三库 schema
-- 位置：E:\OKX\db\{market.db, news.db, account.db}
-- 写入运行时：Python 3 (sqlite3 标准库)
-- 设计原则：
--   1. 所有时间戳 ts 字段统一存 ISO8601 UTC 字符串，例如 "2026-04-18T07:30:00Z"
--   2. PRIMARY KEY 选取 (ts, symbol[, tf])，天然按时间索引并防重
--   3. 单表内幂等：同一 (PK) 行用 INSERT OR REPLACE
--   4. 外键不开启（SQLite 默认关闭），跨库不强制；news_events_index 仅做软引用
--   5. 字段宁可冗余，不强制 JOIN，便于决策侧 SELECT 一次拿齐窗口数据

-- ============================================================
-- market.db ：行情 / K线 / 跨市场
-- ============================================================

CREATE TABLE IF NOT EXISTS tick_snapshots (
    ts          TEXT NOT NULL,            -- ISO8601 UTC
    symbol      TEXT NOT NULL,            -- 例如 BTC-USDT-SWAP
    last        REAL,                     -- 最新成交价
    bid         REAL,
    ask         REAL,
    vol24h      REAL,                     -- 24h 成交量（合约张数或 USDT 视来源而定，写入端固定一种）
    fundingRate REAL,                     -- 当前资金费率（小数，例如 0.0001）
    oi          REAL,                     -- open interest（USDT）
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_tick_symbol_ts ON tick_snapshots(symbol, ts);

CREATE TABLE IF NOT EXISTS kline_cache (
    ts          TEXT NOT NULL,            -- 该 K 线的开盘 UTC 时间
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
    ts            TEXT PRIMARY KEY,       -- 单点跨市场快照
    dxy           REAL,                   -- 美元指数
    gold          REAL,                   -- 现货金 / 期金
    vix           REAL,                   -- 恐慌指数
    spx           REAL,                   -- 标普 500
    btc_etf_flow  REAL,                   -- BTC 现货 ETF 净流入 (USD)
    dxy_d1        REAL,                   -- DXY 较上一观测值的日变动（小数）
    vix_d1        REAL,                   -- VIX 较上一观测值的日变动（小数）
    defillama_tvl_total REAL,             -- DefiLlama 当前总 TVL (USD)
    regime        TEXT,                   -- trend_up/trend_down/range/low_vol（由 collect_data 计算；NULL=未启用 regime 检测）
    btc_dominance REAL,                   -- CoinGecko /global: data.market_cap_percentage.btc（百分比，0-100）
    total_mcap_usd REAL,                  -- CoinGecko /global: data.total_market_cap.usd（全市场总市值 USD）
    total_volume_24h_usd REAL             -- CoinGecko /global: data.total_volume.usd（全市场 24h 总成交 USD）
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

-- ============================================================
-- news.db ：新闻 / 事件
-- ============================================================

CREATE TABLE IF NOT EXISTS news_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,            -- 新闻发布 UTC
    source      TEXT,                     -- mx-search / coingecko / okx-announcement / 手动
    hash        TEXT,                     -- 幂等去重 hash（同一新闻/币种稳定）
    level       TEXT NOT NULL CHECK(level IN ('A','B','C')),  -- A=重大利空/利好  B=显著  C=普通
    symbol      TEXT,                     -- 关联币种；跨市场或宏观留 NULL
    title       TEXT NOT NULL,
    url         TEXT,
    sentiment   REAL,                     -- -1.0 ~ +1.0
    raw         TEXT                      -- 原始 JSON / 摘要文本
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_items(ts);
CREATE INDEX IF NOT EXISTS idx_news_symbol_ts ON news_items(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_news_level_ts ON news_items(level, ts);

CREATE TABLE IF NOT EXISTS news_events_index (
    symbol      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    news_id     INTEGER NOT NULL,         -- 软引用 news_items.id
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

-- ============================================================
-- account.db ：账户 / 持仓 / 评分 / 轮次审计
-- ============================================================

CREATE TABLE IF NOT EXISTS account_snapshots (
    ts         TEXT NOT NULL,
    profile    TEXT NOT NULL DEFAULT 'live',  -- 'live' 或 'demo'，隔离实盘与演练样本
    totalEq    REAL,                      -- 账户总权益 USDT
    availBal   REAL,                      -- 可用 USDT
    upl        REAL,                      -- 未实现盈亏
    daily_pnl  REAL,
    week_pnl   REAL,
    month_pnl  REAL,
    PRIMARY KEY (ts, profile)
);
CREATE INDEX IF NOT EXISTS idx_acct_profile_ts ON account_snapshots(profile, ts);

CREATE TABLE IF NOT EXISTS position_snapshots (
    ts            TEXT NOT NULL,
    profile       TEXT NOT NULL DEFAULT 'live',  -- 'live' 或 'demo'
    symbol        TEXT NOT NULL,
    side          TEXT CHECK(side IN ('long','short')),
    sz            REAL,                   -- 张数
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
    dim1          INTEGER,                -- 技术趋势信号 0-10
    dim2          INTEGER,                -- 结构位置与量价 0-10
    dim3          INTEGER,                -- 新闻事件信号  0-10
    dim4          INTEGER,                -- 跨市场联动    0-10
    dim5          INTEGER,                -- 资金与情绪    0-10
    total         INTEGER,                -- 0-50
    action        TEXT,                   -- IDLE / WATCH / OPEN_LONG / OPEN_SHORT / ADD / CLOSE / PAUSE
    ai_reasoning  TEXT,                   -- 小灵裁量摘要（≤500 字）
    regime        TEXT,                   -- trend_up/trend_down/range/low_vol（NULL=未启用 regime）
    side          TEXT,                   -- long/short（来自开仓方向；NULL=平仓/观望）
    open_fill_px  REAL,                   -- 实仓成交均价（开仓记录用）
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_score_symbol_ts ON scoring_history(symbol, ts);

CREATE TABLE IF NOT EXISTS cycle_runs (
    ts_start      TEXT PRIMARY KEY,
    ts_end        TEXT,
    job_id        TEXT,                   -- 'collect' | 'decide' | 'daily-update' ...
    profile       TEXT,                   -- 'live' 或 'demo'
    state_before  TEXT,
    state_after   TEXT,
    error         TEXT                    -- NULL 表示成功；否则记主要错误
);
CREATE INDEX IF NOT EXISTS idx_cycle_jobid_ts ON cycle_runs(job_id, ts_start);

-- ============================================================
-- lessons.db —— 自省与学习层（独立 DB；只由 Job C / self_review.py 写）
-- 路径：E:\OKX\db\lessons.db
-- 硬约束：
--   1. 该 DB 仅服务于"自我分析 / 学习 / 提升"，绝不能被 Job A/B 写入
--   2. 不得存储或放宽任何风控硬上限（单笔仓位/单次开仓保证金 ≤10% 净值、杠杆 ≤10x、同侧暴露 ≤60%、并发 ≤6 仓、profile=live）
--   3. self_review.py 故障不得阻断 Job A/B 主链路
-- ============================================================

-- 信号表现表：每个 (symbol, dimension, window_days) 一行，滚动统计胜率与平均收益
CREATE TABLE IF NOT EXISTS signal_perf (
    symbol        TEXT NOT NULL,          -- 如 BTC-USDT-SWAP
    dimension     TEXT NOT NULL,          -- dim1..dim5（对齐 scoring_history 五维）
    window_days   INTEGER NOT NULL,       -- 7 / 30
    win_rate      REAL,                   -- 0.0-1.0
    sample_n      INTEGER,                -- 该窗口内样本数
    avg_return    REAL,                   -- 平均收益（已平仓的归一化收益）
    updated_utc   TEXT NOT NULL,          -- ISO8601
    PRIMARY KEY (symbol, dimension, window_days)
);
CREATE INDEX IF NOT EXISTS idx_signal_perf_updated ON signal_perf(updated_utc);

-- 错判模式表：高分亏损 / 低分本可盈利等模式累积频次
CREATE TABLE IF NOT EXISTS error_patterns (
    pattern_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name      TEXT NOT NULL,      -- 如 "high_score_loss"
    trigger_condition TEXT,               -- 触发条件描述（自然语言）
    post_behavior     TEXT,               -- 后续表现描述
    hit_count         INTEGER DEFAULT 1,
    last_seen_utc     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_error_patterns_name ON error_patterns(pattern_name);

-- 参数建议表：小灵的"自我提升"建议清单；状态由主人在对话里裁定
-- 注意：suggestion 字段绝不能涉及风控硬上限；只允许评分权重、阈值、节奏等软参数
CREATE TABLE IF NOT EXISTS param_suggestions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion        TEXT NOT NULL,      -- 简述（≤200 字）
    current_value     TEXT,
    suggested_value   TEXT,
    evidence          TEXT,               -- 支撑证据（统计 / 案例引用）
    status            TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
    created_utc       TEXT NOT NULL,
    decided_utc       TEXT
);
CREATE INDEX IF NOT EXISTS idx_param_suggestions_status ON param_suggestions(status);

-- 错失机会表：擦边分（30-37）但未开仓的反事实回看
CREATE TABLE IF NOT EXISTS missed_opportunities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,         -- 当时评分时间 ISO8601 UTC
    symbol          TEXT NOT NULL,
    score           INTEGER NOT NULL,      -- 当时总分
    regime          TEXT,                  -- 当时 regime
    direction_hint  TEXT,                  -- long/short（基于 dim1+dim4）
    actual_4h_pct   REAL,                  -- 后续 4 小时 |最大顺向幅度|（百分比）
    would_hit_1R    INTEGER,               -- 0/1 是否会触发 1R
    notes           TEXT,
    reviewed_utc    TEXT NOT NULL          -- self_review 写入时刻
);
CREATE INDEX IF NOT EXISTS idx_missed_ts ON missed_opportunities(ts);
CREATE INDEX IF NOT EXISTS idx_missed_symbol_ts ON missed_opportunities(symbol, ts);

-- 周度活跃度表：每周日 self_review 写入
CREATE TABLE IF NOT EXISTS weekly_activity (
    week_start_utc    TEXT PRIMARY KEY,    -- 周一 00:00 UTC
    open_count        INTEGER DEFAULT 0,   -- 本周开仓数（含 main/exploration/scalping）
    close_count       INTEGER DEFAULT 0,
    avg_hold_hours    REAL,
    margin_util_pct   REAL,                -- 保证金利用率均值
    idle_ratio        REAL,                -- IDLE 状态轮次占比
    over_conservative INTEGER DEFAULT 0,   -- 0/1 过度保守标记（连 2 周 open_count<3 且 idle>0.7）
    notes             TEXT,
    updated_utc       TEXT NOT NULL
);
