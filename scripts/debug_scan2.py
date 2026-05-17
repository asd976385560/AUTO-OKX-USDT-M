import sqlite3
conn=sqlite3.connect(r'E:\OKX\db\market.db')
cur=conn.cursor()
cur.execute("""
SELECT k.symbol, k.tf, k.c, k.ma5, k.ma20, k.atr14, k.rsi14
FROM kline_cache k
INNER JOIN (
    SELECT symbol, tf, MAX(ts) as max_ts
    FROM kline_cache
    WHERE tf IN ('15m','1H','4H')
    GROUP BY symbol, tf
) sub ON k.symbol=sub.symbol AND k.tf=sub.tf AND k.ts=sub.max_ts
""")
klines_raw = cur.fetchall()
klines = {}
for sym, tf, c, ma5, ma20, atr14, rsi14 in klines_raw:
    if sym not in klines:
        klines[sym] = {}
    klines[sym][tf] = {"c": c, "ma5": ma5, "ma20": ma20, "atr14": atr14, "rsi14": rsi14}

conn2=sqlite3.connect(r'E:\OKX\db\market.db')
cur2=conn2.cursor()
cur2.execute("""
    SELECT symbol, last, vol24h, fundingRate
    FROM tick_snapshots
    WHERE ts = (SELECT MAX(ts) FROM tick_snapshots)
    AND symbol = 'DOGE-USDT-SWAP'
""")
doge = cur2.fetchone()
print("DOGE ticker:", doge)

NET_WORTH = 1000.0  # placeholder account equity
sym = "DOGE-USDT-SWAP"
doge_last = doge[1]
ct = 1000.0
mp = doge_last * ct / 10
print(f"doge_last={doge_last}, ct={ct}, mp={mp}, threshold={NET_WORTH*0.10}")
print(f"mp > threshold: {mp > NET_WORTH * 0.10}")

kl1h = klines.get(sym, {}).get("1H", {})
kl4h = klines.get(sym, {}).get("4H", {})
print(f"DOGE 1H kline: {kl1h}")
print(f"DOGE 4H kline: {kl4h}")
