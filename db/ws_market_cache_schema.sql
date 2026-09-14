PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS cache_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    inst_id       TEXT PRIMARY KEY,
    state         TEXT,
    settle_ccy    TEXT,
    ct_type       TEXT,
    exchange_ts   INTEGER,
    received_at   TEXT NOT NULL,
    source        TEXT NOT NULL,
    payload_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS latest_market (
    channel       TEXT NOT NULL,
    inst_id       TEXT NOT NULL,
    exchange_ts   INTEGER,
    received_at   TEXT NOT NULL,
    conn_epoch    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    PRIMARY KEY (channel, inst_id)
);

CREATE TABLE IF NOT EXISTS candles (
    inst_id       TEXT NOT NULL,
    timeframe     TEXT NOT NULL CHECK (
        timeframe IN ('15m','1H','4H','1D','1W','1M')
    ),
    ts_ms         INTEGER NOT NULL,
    open          TEXT,
    high          TEXT,
    low           TEXT,
    close         TEXT,
    volume        TEXT,
    volume_ccy    TEXT,
    volume_quote  TEXT,
    confirm       INTEGER NOT NULL CHECK (confirm = 1),
    bar_end_ms    INTEGER,
    close_latency_ms INTEGER,
    received_at   TEXT NOT NULL,
    conn_epoch    TEXT NOT NULL,
    source        TEXT NOT NULL,
    PRIMARY KEY (inst_id, timeframe, ts_ms)
);

CREATE INDEX IF NOT EXISTS idx_ws_candles_tf_ts
    ON candles(timeframe, ts_ms);

CREATE TABLE IF NOT EXISTS books (
    inst_id       TEXT PRIMARY KEY,
    exchange_ts   INTEGER,
    seq_id        INTEGER,
    checksum      INTEGER,
    valid         INTEGER NOT NULL CHECK (valid IN (0,1)),
    received_at   TEXT NOT NULL,
    conn_epoch    TEXT NOT NULL,
    bids_json     TEXT NOT NULL,
    asks_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    inst_id       TEXT NOT NULL,
    trade_id      TEXT NOT NULL,
    exchange_ts   INTEGER NOT NULL,
    received_at   TEXT NOT NULL,
    conn_epoch    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    PRIMARY KEY (inst_id, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_ws_trades_inst_ts
    ON trades(inst_id, exchange_ts DESC);

CREATE TABLE IF NOT EXISTS connection_health (
    group_id          TEXT PRIMARY KEY,
    endpoint          TEXT NOT NULL,
    channels_json     TEXT NOT NULL,
    status            TEXT NOT NULL,
    conn_epoch        TEXT NOT NULL,
    connected_at      TEXT,
    disconnected_at   TEXT,
    last_message_at   TEXT,
    last_pong_at      TEXT,
    expected_args     INTEGER NOT NULL DEFAULT 0,
    acked_args        INTEGER NOT NULL DEFAULT 0,
    reconnect_count   INTEGER NOT NULL DEFAULT 0,
    recovery_ms       INTEGER,
    last_error        TEXT,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    group_id      TEXT NOT NULL,
    arg_key       TEXT NOT NULL,
    channel       TEXT NOT NULL,
    inst_id       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (group_id, arg_key)
);

CREATE INDEX IF NOT EXISTS idx_ws_subscriptions_channel
    ON subscriptions(channel, inst_id, status);

CREATE TABLE IF NOT EXISTS stream_completeness (
    channel       TEXT NOT NULL,
    inst_id       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('complete','incomplete')),
    reason        TEXT,
    conn_epoch    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (channel, inst_id)
);

CREATE INDEX IF NOT EXISTS idx_ws_stream_completeness_status
    ON stream_completeness(channel, status);

CREATE TABLE IF NOT EXISTS connection_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    group_id      TEXT NOT NULL,
    conn_epoch    TEXT NOT NULL,
    event         TEXT NOT NULL,
    duration_ms   INTEGER,
    detail        TEXT
);

CREATE INDEX IF NOT EXISTS idx_ws_connection_events_ts
    ON connection_events(ts);

CREATE TABLE IF NOT EXISTS service_samples (
    ts                TEXT PRIMARY KEY,
    pid               INTEGER NOT NULL,
    rss_bytes         INTEGER,
    cpu_cores         REAL,
    pending_latest    INTEGER NOT NULL,
    pending_candles   INTEGER NOT NULL,
    pending_books     INTEGER NOT NULL,
    pending_trades    INTEGER NOT NULL,
    required_drops    INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO cache_meta(key, value, updated_at)
VALUES ('schema_version', '1', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
