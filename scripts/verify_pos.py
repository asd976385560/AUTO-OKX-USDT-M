import sqlite3
conn=sqlite3.connect(r'E:\OKX\db\account.db')
cur=conn.cursor()
cur.execute("SELECT symbol,side,sz,fill_px,action FROM trade_events WHERE symbol='DOGE-USDT-SWAP' ORDER BY id DESC LIMIT 5")
for r in cur.fetchall():
    print(r)
cur.execute("SELECT key,value,updated_utc FROM system_state WHERE key IN ('state','last_jobb_decision','position_note')")
for r in cur.fetchall():
    print(r)
cur.execute("SELECT COUNT(*) FROM scoring_history WHERE ts='2026-05-16T19:35:00Z'")
print("scoring rows this cycle:", cur.fetchone()[0])
