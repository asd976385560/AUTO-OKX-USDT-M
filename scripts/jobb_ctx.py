# -*- coding: utf-8 -*-
import sqlite3, json, sys, os
sys.stdout.reconfigure(encoding='utf-8')

ROOT = 'E:/OKX/db'

def query(db, sql, params=()):
    path = os.path.join(ROOT, db)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# 1. System State
print("=== SYSTEM STATE ===")
state = query('account.db', "SELECT key, value FROM system_state")
for r in state:
    print(json.dumps({r['key']: r['value']}, ensure_ascii=False))

# 2. Account Snapshot
print("\n=== ACCOUNT SNAPSHOT ===")
accts = query('account.db', "SELECT * FROM account_snapshots WHERE profile='live' ORDER BY ts DESC LIMIT 1")
for r in accts: print(json.dumps(r, ensure_ascii=False, default=str))

# 3. Positions
print("\n=== POSITIONS ===")
poss = query('account.db', "SELECT * FROM position_snapshots WHERE profile='live' ORDER BY ts DESC LIMIT 10")
for r in poss: print(json.dumps(r, ensure_ascii=False, default=str))

# 4. Cross Market
print("\n=== CROSS MARKET ===")
cm = query('market.db', "SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1")
for r in cm: print(json.dumps(r, ensure_ascii=False, default=str))

# 5. Tick Snapshots (most recent, top by vol)
print("\n=== TOP TICK SNAPSHOTS ===")
ticks = query('market.db', "SELECT * FROM tick_snapshots WHERE ts=(SELECT MAX(ts) FROM tick_snapshots) ORDER BY vol24h DESC LIMIT 50")
for r in ticks: print(json.dumps(r, ensure_ascii=False, default=str))

# 6. Derivatives (funding rates)
print("\n=== DERIVATIVES (funding) ===")
derivs = query('market.db', "SELECT * FROM derivatives WHERE ts=(SELECT MAX(ts) FROM derivatives) ORDER BY funding_rate DESC LIMIT 20")
for r in derivs: print(json.dumps(r, ensure_ascii=False, default=str))

# 7. News
print("\n=== NEWS (latest 10) ===")
news = query('news.db', "SELECT * FROM news_items ORDER BY ts DESC LIMIT 10")
for r in news: print(json.dumps(r, ensure_ascii=False, default=str))

# 8. Scoring History (recent)
print("\n=== SCORING HISTORY (recent) ===")
sc = query('account.db', "SELECT * FROM scoring_history ORDER BY ts DESC LIMIT 20")
for r in sc: print(json.dumps(r, ensure_ascii=False, default=str))

# 9. Trade Events (recent)
print("\n=== TRADE EVENTS (recent) ===")
te = query('account.db', "SELECT * FROM trade_events ORDER BY ts DESC LIMIT 20")
for r in te: print(json.dumps(r, ensure_ascii=False, default=str))

# 10. Playbook (recent)
print("\n=== PLAYBOOK (recent 5) ===")
pb = query('account.db', "SELECT * FROM playbook ORDER BY ts DESC LIMIT 5")
for r in pb: print(json.dumps(r, ensure_ascii=False, default=str))

# 11. Lessons - error patterns
print("\n=== ERROR PATTERNS ===")
ep = query('lessons.db', "SELECT * FROM error_patterns ORDER BY hit_count DESC LIMIT 5")
for r in ep: print(json.dumps(r, ensure_ascii=False, default=str))

# 12. Hypotheses
print("\n=== HYPOTHESES (active) ===")
hyp = query('account.db', "SELECT * FROM cycle_runs WHERE ts_start > datetime('now', '-3 days') ORDER BY ts_start DESC LIMIT 10")
for r in hyp: print(json.dumps(r, ensure_ascii=False, default=str))
