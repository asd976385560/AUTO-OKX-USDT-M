import sqlite3
ac = sqlite3.connect(r'E:\OKX\db\account.db')
cur = ac.execute("SELECT symbol, side, sz, avgPx, lev, liqPx, upl, COUNT(*) as cnt FROM position_snapshots WHERE profile='live' GROUP BY symbol, side, sz, avgPx, lev, liqPx, upl")
rows = cur.fetchall()
print('UNIQUE live positions:')
for r in rows:
    print(f'  {r}')
print(f'Total unique position records: {len(rows)}')
