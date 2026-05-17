#!/usr/bin/env python3
"""JobB context loader - reads all DB state for decision making"""
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

def main():
    out = {}
    
    # 1. system_state
    rows = query_db(DB_DIR / 'account.db', 
        "SELECT key, value, updated_utc FROM system_state")
    out['system_state'] = {r['key']: r['value'] for r in rows}
    
    # 2. account snapshots (latest)
    rows = query_db(DB_DIR / 'account.db',
        "SELECT * FROM account_snapshots WHERE profile='live' ORDER BY ts DESC LIMIT 1")
    out['account'] = rows[0] if rows else None
    
    # 3. position snapshots (latest)
    rows = query_db(DB_DIR / 'account.db',
        "SELECT * FROM position_snapshots WHERE profile='live' ORDER BY ts DESC")
    out['positions'] = rows
    
    # 4. tick_snapshots (latest)
    rows = query_db(DB_DIR / 'market.db',
        "SELECT * FROM tick_snapshots WHERE ts = (SELECT MAX(ts) FROM tick_snapshots)")
    out['ticks'] = rows
    
    # 5. cross_market (latest)
    rows = query_db(DB_DIR / 'market.db',
        "SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1")
    out['cross_market'] = rows[0] if rows else None
    
    # 6. derivatives (latest)
    rows = query_db(DB_DIR / 'market.db',
        "SELECT * FROM derivatives WHERE ts = (SELECT MAX(ts) FROM derivatives)")
    out['derivatives'] = rows
    
    # 7. news (latest 20)
    rows = query_db(DB_DIR / 'news.db',
        "SELECT * FROM news_items ORDER BY ts DESC LIMIT 20")
    out['news'] = rows
    
    # 8. scoring_history (latest cycle)
    rows = query_db(DB_DIR / 'account.db',
        "SELECT * FROM scoring_history ORDER BY ts DESC LIMIT 100")
    out['scoring_history'] = rows
    
    # 9. trade_events (recent 20)
    rows = query_db(DB_DIR / 'account.db',
        "SELECT * FROM trade_events WHERE profile='live' ORDER BY ts DESC LIMIT 20")
    out['trade_events'] = rows
    
    # 10. playbook (recent)
    rows = query_db(DB_DIR / 'account.db',
        "SELECT * FROM playbook ORDER BY ts DESC LIMIT 5")
    out['playbook'] = rows
    
    # 11. lessons
    for tbl in ['signal_perf', 'error_patterns', 'param_suggestions', 'missed_opportunities']:
        rows = query_db(DB_DIR / 'lessons.db',
            f"SELECT * FROM {tbl} ORDER BY ROWID DESC LIMIT 20")
        out[f'lessons_{tbl}'] = rows
    
    # 12. kline_cache (latest for key symbols)
    rows = query_db(DB_DIR / 'market.db',
        """SELECT k.* FROM kline_cache k
           WHERE k.ts = (SELECT MAX(ts) FROM kline_cache WHERE symbol=k.symbol AND tf='15m')
           AND k.tf = '15m'""")
    out['klines_15m'] = rows
    
    # 13. cycle_runs (recent)
    rows = query_db(DB_DIR / 'account.db',
        "SELECT * FROM cycle_runs ORDER BY ts_start DESC LIMIT 10")
    out['cycle_runs'] = rows
    
    print(json.dumps(out, ensure_ascii=False, default=str))

if __name__ == '__main__':
    main()
