import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else r'E:\OKX\db\account.db'
query = sys.argv[2] if len(sys.argv) > 2 else 'SELECT * FROM system_state ORDER BY updated_utc DESC LIMIT 5'

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute(query)
cols = [desc[0] for desc in cur.description]
print(cols)
for row in cur.fetchall():
    print(dict(row))
conn.close()
