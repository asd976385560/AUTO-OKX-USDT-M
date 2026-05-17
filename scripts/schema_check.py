import sqlite3
conn = sqlite3.connect('E:/OKX/db/market.db')
c = conn.cursor()
c.execute('PRAGMA table_info(tick_snapshots)')
print("tick_snapshots columns:", [r for r in c.fetchall()])
c.execute('SELECT * FROM tick_snapshots LIMIT 1')
print("tick_snapshots sample row keys:", [d[0] for d in c.description])
c.execute('SELECT ts, symbol, last, bid, ask, vol24h FROM tick_snapshots WHERE ts = (SELECT MAX(ts) FROM tick_snapshots) LIMIT 5')
for r in c.fetchall():
    print(r)
conn.close()
