import sqlite3
ac = sqlite3.connect(r'E:\OKX\db\account.db')
cur = ac.execute("""
    SELECT p.symbol, p.side, p.sz, p.avgPx, p.lev, p.liqPx, p.upl, p.ts
    FROM position_snapshots p
    INNER JOIN (
        SELECT symbol, MAX(ts) as max_ts
        FROM position_snapshots
        WHERE profile='live'
        GROUP BY symbol
    ) latest ON p.symbol = latest.symbol AND p.ts = latest.max_ts
    WHERE p.profile='live'
""")
rows = cur.fetchall()
print('CURRENT live positions (latest snapshot per symbol):')
for r in rows:
    print(f'  {r}')
ac.close()
