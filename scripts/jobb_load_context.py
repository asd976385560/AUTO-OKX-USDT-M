"""
Job B Context Loader v2 - Correct Schema
"""
import sqlite3, json
from pathlib import Path

ROOT = Path(r"E:\OKX")
DB_DIR = ROOT / "db"

def g(db, sql, args=()):
    conn = sqlite3.connect(DB_DIR / db)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(sql, args)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def g1(db, sql, args=()):
    rows = g(db, sql, args)
    return rows[0] if rows else None

def main():
    # system_state is key-value
    ss_rows = g("account.db", "SELECT * FROM system_state")
    ss = {r['key']: r['value'] for r in ss_rows}
    print("=== SYSTEM STATE ===")
    print(json.dumps(ss, ensure_ascii=False, default=str))
    
    print("\n=== ACCOUNT SNAPSHOT ===")
    acct = g1("account.db", "SELECT * FROM account_snapshots ORDER BY ts DESC LIMIT 1")
    print(json.dumps(acct, ensure_ascii=False, default=str) if acct else "NONE")
    
    print("\n=== POSITIONS ===")
    for p in g("account.db", "SELECT * FROM position_snapshots ORDER BY ts DESC LIMIT 20"):
        print(json.dumps(p, ensure_ascii=False, default=str))
    
    print("\n=== CROSS MARKET ===")
    cm = g1("market.db", "SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1")
    print(json.dumps(cm, ensure_ascii=False, default=str) if cm else "NONE")
    
    print("\n=== NEWS (latest 20) ===")
    for n in g("news.db", "SELECT * FROM news_items ORDER BY ts DESC LIMIT 20"):
        print(json.dumps(n, ensure_ascii=False, default=str))
    
    print("\n=== DERIVATIVES (latest per symbol, top 20 by vol) ===")
    derivs = g("market.db", "SELECT * FROM derivatives ORDER BY ts DESC LIMIT 2000")
    by_sym = {}
    for d in derivs:
        sym = d.get("symbol")
        if sym and sym not in by_sym:
            by_sym[sym] = d
    for sym, d in list(by_sym.items())[:20]:
        print(f"{sym}: fundingRate={d.get('funding_rate')}, nextFundingTime={d.get('next_funding_time')}, oi={d.get('oi_usd')}")
    
    print("\n=== LATEST TRADES (20) ===")
    for t in g("account.db", "SELECT * FROM trade_events ORDER BY ts DESC LIMIT 20"):
        print(json.dumps(t, ensure_ascii=False, default=str))
    
    print("\n=== PLAYBOOK (latest 10) ===")
    for p in g("account.db", "SELECT * FROM playbook ORDER BY ts DESC LIMIT 10"):
        print(json.dumps(p, ensure_ascii=False, default=str))
    
    print("\n=== SCORING HISTORY (latest 20) ===")
    for s in g("account.db", "SELECT * FROM scoring_history ORDER BY ts DESC LIMIT 20"):
        print(json.dumps(s, ensure_ascii=False, default=str))
    
    print("\n=== LESSONS ===")
    for table in ["signal_perf", "error_patterns", "param_suggestions", "missed_opportunities"]:
        print(f"\n-- {table} --")
        for r in g("lessons.db", f"SELECT * FROM {table} ORDER BY updated_utc DESC LIMIT 10"):
            print(json.dumps(r, ensure_ascii=False, default=str))
    
    print("\n=== CYCLE RUNS (10) ===")
    for c in g("account.db", "SELECT * FROM cycle_runs ORDER BY ts_start DESC LIMIT 10"):
        print(json.dumps(c, ensure_ascii=False, default=str))
    
    print("\n=== RECORDS (10) ===")
    for r in g("account.db", "SELECT * FROM records ORDER BY ts DESC LIMIT 10"):
        print(json.dumps(r, ensure_ascii=False, default=str))
    
    print("\n=== TICK SNAPSHOT (unique symbols latest, sample 40) ===")
    ticks = g("market.db", "SELECT * FROM tick_snapshots ORDER BY ts DESC LIMIT 5000")
    by_sym = {}
    for t in ticks:
        sym = t.get("symbol")
        if sym and sym not in by_sym:
            by_sym[sym] = t
    for sym, t in list(by_sym.items())[:40]:
        print(f"{sym}: last={t.get('last')}, bid={t.get('bid')}, ask={t.get('ask')}, vol24h={t.get('vol24h')}, fundingRate={t.get('fundingRate')}")

if __name__ == "__main__":
    main()
