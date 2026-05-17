import sqlite3
conn = sqlite3.connect(r'E:\OKX\db\news.db')
c = conn.cursor()
c.execute("SELECT ts, source, symbol, title, sentiment FROM news_items ORDER BY ts DESC LIMIT 15")
rows = c.fetchall()
for r in rows:
    print(f'{r[0]} | {r[1]} | {r[2]} | sent={r[4]} | {r[3][:80]}')
conn.close()
