import sqlite3
ac = sqlite3.connect(r'E:\OKX\db\account.db')
rows = ac.execute("SELECT key, value, updated_utc FROM system_state ORDER BY key").fetchall()
print('system_state:')
for r in rows:
    print(f'  {r[0]}: {r[1]} ({r[2]})')
ac.close()
