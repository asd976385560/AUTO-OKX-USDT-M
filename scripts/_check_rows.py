import sqlite3
conn = sqlite3.connect(r'E:\OKX\db\market.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cur.fetchall()
for t in tables:
    cur.execute(f"SELECT COUNT(*) FROM {t[0]}")
    print(f"{t[0]}: {cur.fetchone()[0]} rows")
conn.close()
