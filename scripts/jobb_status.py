#!/usr/bin/env python3
"""Quick status check for JobB"""
import sys
import os
import json
import sqlite3
from pathlib import Path

DB_DIR = Path(r'E:\OKX\db')

def query_db(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# System state
rows = query_db(DB_DIR / 'account.db', "SELECT key, value, updated_utc FROM system_state")
for r in rows:
    print(f"SYSTEM_STATE {r['key']} = {r['value']} (updated {r['updated_utc']})")

# Latest ticks time
rows = query_db(DB_DIR / 'market.db', "SELECT MAX(ts) as max_ts FROM tick_snapshots")
print(f"\nLatest tick_snapshots ts: {rows[0]['max_ts']}")

# Latest kline time
rows = query_db(DB_DIR / 'market.db', "SELECT MAX(ts) as max_ts, tf FROM kline_cache WHERE tf='1H' GROUP BY tf")
print(f"Latest kline_cache 1H ts: {rows[0]['max_ts'] if rows else 'N/A'}")

# Latest account snapshot
rows = query_db(DB_DIR / 'account.db', "SELECT * FROM account_snapshots WHERE profile='live' ORDER BY ts DESC LIMIT 1")
print(f"\nLatest account snapshot: {rows[0] if rows else 'N/A'}")

# Latest cross_market
rows = query_db(DB_DIR / 'market.db', "SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1")
print(f"\nLatest cross_market: {rows[0] if rows else 'N/A'}")

# Latest cycle run
rows = query_db(DB_DIR / 'account.db', "SELECT * FROM cycle_runs ORDER BY ts_start DESC LIMIT 5")
print(f"\nRecent cycle_runs:")
for r in rows:
    print(f"  {r['ts_start']} -> {r['ts_end']} | job={r['job_id']} | error={r['error']}")
