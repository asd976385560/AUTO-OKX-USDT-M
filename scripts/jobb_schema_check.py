"""Quick schema checker"""
import sqlite3
from pathlib import Path

DB_DIR = Path(r"E:\OKX\db")

def check_schema(db_name):
    print(f"\n{'='*40}")
    print(f"DB: {db_name}")
    conn = sqlite3.connect(DB_DIR / db_name)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    for t in tables:
        print(f"\n  Table: {t}")
        try:
            cur2 = conn.execute(f"PRAGMA table_info({t})")
            for row in cur2.fetchall():
                print(f"    {row[1]} ({row[2]})")
        except Exception as e:
            print(f"    Error: {e}")
    conn.close()

for db in ["account.db", "market.db", "news.db", "lessons.db"]:
    check_schema(db)
