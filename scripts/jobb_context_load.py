
import sqlite3, json, sys
from datetime import datetime, timezone, timedelta

UTC8 = timezone(timedelta(hours=8))
now_utc8 = datetime.now(UTC8)
now_str = now_utc8.strftime("%Y-%m-%dT%H:%M:%SZ")

DB_DIR = r"E:\OKX\db"
MARKET_DB = DB_DIR + r"\market.db"
NEWS_DB = DB_DIR + r"\news.db"
ACCOUNT_DB = DB_DIR + r"\account.db"
LESSONS_DB = DB_DIR + r"\lessons.db"

def read_db(path):
    if not path.exists():
        return None
    return sqlite3.connect(str(path))

def fmt_ts(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z','+00:00'))
        dt_utc8 = dt.astimezone(UTC8)
        return dt_utc8.strftime("%m-%d %H:%M")
    except:
        return ts_str

def q(db, sql, args=()):
    cur = db.execute(sql, args)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]

print("="*60)
print(f"JobB Context Load @ {now_str}")
print("="*60)

# ---- system_state ----
ac = sqlite3.connect(ACCOUNT_DB)
ss = q(ac, "SELECT key, value, updated_utc FROM system_state")
print("\n[system_state]")
for s in ss:
    print(f"  {s['key']}: {s['value']} (updated {s['updated_utc']})")

# ---- account snapshots ----
print("\n[account_snapshots - latest 3]")
snap = q(ac, "SELECT * FROM account_snapshots WHERE profile='live' ORDER BY ts DESC LIMIT 3")
for s in snap:
    age_mins = (datetime.now(UTC8) - datetime.fromisoformat(s['ts'].replace('Z','+00:00')).astimezone(UTC8)).total_seconds()/60
    print(f"  ts={fmt_ts(s['ts'])} age={age_mins:.0f}min | totalEq={s['totalEq']} | availBal={s['availBal']} | daily_pnl={s['daily_pnl']} | upl={s['upl']}")

# ---- position snapshots ----
print("\n[position_snapshots - live]")
pos = q(ac, "SELECT * FROM position_snapshots WHERE profile='live' ORDER BY ts DESC LIMIT 20")
if pos:
    pos_by_sym = {}
    for p in pos:
        sym = p['symbol']
        if sym not in pos_by_sym:
            pos_by_sym[sym] = p
    for sym, p in pos_by_sym.items():
        age_mins = (datetime.now(UTC8) - datetime.fromisoformat(p['ts'].replace('Z','+00:00')).astimezone(UTC8)).total_seconds()/60
        print(f"  {sym}: side={p['side']}, sz={p['sz']}, avgPx={p['avgPx']}, lev={p['lev']}, liqPx={p['liqPx']}, upl={p['upl']:.2f} (age={age_mins:.0f}min)")
else:
    print("  (no live positions)")

# ---- trade_events recent ----
print("\n[trade_events - latest 10]")
te = q(ac, "SELECT ts, symbol, action, side, sz, fill_px, pnl FROM trade_events WHERE profile='live' ORDER BY ts DESC LIMIT 10")
for t in te:
    print(f"  {fmt_ts(t['ts'])} | {t['symbol']} | {t['action']} {t['side']} sz={t['sz']} @ {t['fill_px']} | pnl={t['pnl']}")

# ---- scoring_history latest per symbol ----
print("\n[scoring_history - latest per symbol]")
sh = q(ac, """
    SELECT * FROM scoring_history s1
    WHERE ts = (SELECT MAX(ts) FROM scoring_history s2 WHERE s2.symbol = s1.symbol AND s2.ts <= ?)
    AND ts >= ?
    ORDER BY total DESC LIMIT 30
""", [now_str, (datetime.now(UTC8) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")])
for s in sh:
    print(f"  {s['symbol']}: dim1={s['dim1']} dim2={s['dim2']} dim3={s['dim3']} dim4={s['dim4']} dim5={s['dim5']} total={s['total']} | {s['action']} {s['side']} | {fmt_ts(s['ts'])}")

# ---- cycle_runs ----
print("\n[cycle_runs - latest 5]")
cr = q(ac, "SELECT * FROM cycle_runs ORDER BY ts_start DESC LIMIT 5")
for c in cr:
    print(f"  {c['ts_start']} | {c['job_id']} | {c['state_before']} -> {c['state_after']} | err={c['error']}")

ac.close()

# ---- market.db cross_market ----
mc = sqlite3.connect(MARKET_DB)
print("\n[cross_market - latest]")
cm = q(mc, "SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1")
if cm:
    c = cm[0]
    print(f"  ts={fmt_ts(c['ts'])} | regime={c['regime']} | dxy={c['dxy']} | gold={c['gold']} | vix={c['vix']} | spx={c['spx']} | btc_dominance={c['btc_dominance']} | total_mcap={c['total_mcap_usd']} | defillama_tvl={c['defillama_tvl_total']}")
    age_c = (datetime.now(UTC8) - datetime.fromisoformat(c['ts'].replace('Z','+00:00')).astimezone(UTC8)).total_seconds()/60
    print(f"  age={age_c:.0f}min")
mc.close()

# ---- market.db tick_snapshots (latest) ----
mc = sqlite3.connect(MARKET_DB)
print("\n[tick_snapshots - count]")
cnt = q(mc, "SELECT COUNT(DISTINCT symbol) as cnt FROM tick_snapshots WHERE ts >= ?", [(datetime.now(UTC8) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")])
print(f"  symbols with data in last 20min: {cnt[0]['cnt']}")

# latest tick data
print("\n[tick_snapshots - sample BTC/ETH/SOL/DOGE]")
syms = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'DOGE-USDT-SWAP']
for sym in syms:
    t = q(mc, "SELECT * FROM tick_snapshots WHERE symbol=? ORDER BY ts DESC LIMIT 1", [sym])
    if t:
        t=t[0]
        age_t = (datetime.now(UTC8) - datetime.fromisoformat(t['ts'].replace('Z','+00:00')).astimezone(UTC8)).total_seconds()/60
        print(f"  {sym}: last={t['last']} bid={t['bid']} ask={t['ask']} vol24h={t['vol24h']} fundingRate={t['fundingRate']} (age={age_t:.0f}min)")
    else:
        print(f"  {sym}: NO DATA")
mc.close()

# ---- news.db latest ----
nn = sqlite3.connect(NEWS_DB)
print("\n[news_items - latest 5]")
news = q(nn, "SELECT ts, source, level, symbol, title, sentiment FROM news_items ORDER BY ts DESC LIMIT 5")
for n in news:
    print(f"  [{n['level']}] {fmt_ts(n['ts'])} | {n['source']} | {n['symbol'] or 'N/A'} | {n['title'][:60]} | sent={n['sentiment']}")
nn.close()

# ---- lessons.db ----
ld = sqlite3.connect(LESSONS_DB)
print("\n[lessons.db - param_suggestions pending]")
ps = q(ld, "SELECT * FROM param_suggestions WHERE status='pending' ORDER BY created_utc DESC LIMIT 10")
for p in ps:
    print(f"  [{p['id']}] {p['suggestion']} | curr={p['current_value']} sugg={p['suggested_value']} | {p['evidence'][:60] if p['evidence'] else ''}")

print("\n[lessons.db - error_patterns recent]")
ep = q(ld, "SELECT * FROM error_patterns ORDER BY last_seen_utc DESC LIMIT 5")
for e in ep:
    print(f"  {e['pattern_name']}: {e['trigger_condition']} (hits={e['hit_count']})")

print("\n[lessons.db - missed_opportunities recent]")
mo = q(ld, "SELECT * FROM missed_opportunities ORDER BY ts DESC LIMIT 5")
for m in mo:
    print(f"  {fmt_ts(m['ts'])} | {m['symbol']} score={m['score']} dir={m['direction_hint']} would_hit_1R={m['would_hit_1R']} | {m['notes'][:60] if m['notes'] else ''}")
ld.close()

print("\n" + "="*60)
