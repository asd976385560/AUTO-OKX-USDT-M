import sqlite3, json

db_acc = 'E:/OKX/db/account.db'
db_mkt = 'E:/OKX/db/market.db'
db_news = 'E:/OKX/db/news.db'

def dict_row(c, row):
    return dict(zip([d[0] for d in c.description], row))

print("=" * 60)
print("CONTEXT LOAD — Job B start")
print("=" * 60)

# ---- account.db ----
conn = sqlite3.connect(db_acc)
c = conn.cursor()

# system_state - has key/value columns
c.execute('SELECT * FROM system_state ORDER BY updated_utc DESC LIMIT 10')
sss = c.fetchall()
print("\n=== SYSTEM_STATE ===")
for ss in sss:
    print(dict_row(c, ss))

c.execute('SELECT name FROM sqlite_master WHERE type="table"')
tables = c.fetchall()
print("\n=== ACCOUNT DB TABLES ===")
for t in tables:
    print(t[0])

c.execute('SELECT * FROM account_snapshots ORDER BY ts DESC LIMIT 1')
acc = c.fetchone()
print("\n=== ACCOUNT_SNAPSHOT ===")
print(dict_row(c, acc) if acc else "EMPTY")

c.execute('SELECT * FROM position_snapshots ORDER BY ts DESC LIMIT 20')
poss = c.fetchall()
print(f"\n=== POSITION_SNAPSHOTS ({len(poss)} rows) ===")
for p in poss:
    print(dict_row(c, p))

c.execute('SELECT * FROM cycle_runs ORDER BY ts_start DESC LIMIT 5')
crs = c.fetchall()
print(f"\n=== CYCLE_RUNS (last 5) ===")
for cr in crs:
    print(dict_row(c, cr))

c.execute('SELECT * FROM trade_events ORDER BY ts DESC LIMIT 15')
tes = c.fetchall()
print(f"\n=== TRADE_EVENTS (last 15) ===")
for te in tes:
    print(dict_row(c, te))

c.execute('SELECT * FROM playbook ORDER BY ts DESC LIMIT 5')
pb = c.fetchall()
print(f"\n=== PLAYBOOK (last 5) ===")
for p in pb:
    print(dict_row(c, p))

conn.close()

# ---- market.db ----
conn2 = sqlite3.connect(db_mkt)
c2 = conn2.cursor()

c2.execute('SELECT MAX(ts) FROM tick_snapshots')
max_ts_result = c2.fetchone()
max_ts = max_ts_result[0] if max_ts_result else None
print(f"\n=== TICK_SNAPSHOTS max_ts={max_ts} ===")
if max_ts:
    c2.execute('''
    SELECT ts, symbol, last, bid, ask, vol24h, fundingRate, oi
    FROM tick_snapshots
    WHERE ts = ?
    ORDER BY vol24h DESC
    LIMIT 80
    ''', (max_ts,))
    ticks = c2.fetchall()
    t_cols = [d[0] for d in c2.description]
    for r in ticks:
        print(dict_row(c2, r))
else:
    print("NO tick_snapshots data")

c2.execute('SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1')
cm = c2.fetchone()
print("\n=== CROSS_MARKET ===")
print(dict_row(c2, cm) if cm else "EMPTY")

c2.execute('SELECT * FROM derivatives WHERE ts = ? ORDER BY symbol LIMIT 50', (max_ts,))
drv = c2.fetchall()
print(f"\n=== DERIVATIVES (ts={max_ts}, top 50) ===")
for d in drv:
    print(dict_row(c2, d))

c2.execute('SELECT * FROM kline_cache WHERE tf = ? ORDER BY ts DESC LIMIT 5', ('15m',))
kl_top = c2.fetchall()
print(f"\n=== KLINE_CACHE 15m (latest 5 symbols by time) ===")
for k in kl_top:
    print(dict_row(c2, k))

conn2.close()

# ---- news.db ----
conn3 = sqlite3.connect(db_news)
c3 = conn3.cursor()

c3.execute('SELECT * FROM news_items ORDER BY ts DESC LIMIT 20')
news = c3.fetchall()
print(f"\n=== NEWS_ITEMS (last 20) ===")
for n in news:
    print(dict_row(c3, n))

c3.execute('SELECT * FROM coin_sentiment ORDER BY ts DESC LIMIT 20')
sent = c3.fetchall()
print(f"\n=== COIN_SENTIMENT (last 20) ===")
for s in sent:
    print(dict_row(c3, s))

conn3.close()

# ---- lessons.db ----
try:
    db_less = 'E:/OKX/db/lessons.db'
    conn4 = sqlite3.connect(db_less)
    c4 = conn4.cursor()
    c4.execute('SELECT * FROM error_patterns ORDER BY last_seen_utc DESC LIMIT 10')
    eps = c4.fetchall()
    print(f"\n=== ERROR_PATTERNS (last 10) ===")
    for e in eps:
        print(dict_row(c4, e))
    c4.execute('SELECT * FROM param_suggestions WHERE status = ? ORDER BY created_utc DESC LIMIT 10', ('pending',))
    ps = c4.fetchall()
    print(f"\n=== PARAM_SUGGESTIONS pending (last 10) ===")
    for p in ps:
        print(dict_row(c4, p))
    c4.execute('SELECT * FROM signal_perf ORDER BY updated_utc DESC LIMIT 20')
    sp = c4.fetchall()
    print(f"\n=== SIGNAL_PERF (last 20) ===")
    for s in sp:
        print(dict_row(c4, s))
    conn4.close()
except Exception as e:
    print(f"\n=== lessons.db not available: {e} ===")

print("\n=== END CONTEXT LOAD ===")
