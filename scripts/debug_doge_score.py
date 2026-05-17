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

doge_kl = klines.get("DOGE-USDT-SWAP", {})
print("DOGE klines:", doge_kl)

kl1h = doge_kl.get("1H", {})
kl4h = doge_kl.get("4H", {})
kl15 = doge_kl.get("15m", {})

d = 0.0  # redacted live/position price placeholder
rsi1h = kl1h.get("rsi14", 50)
rsi4h = kl4h.get("rsi14", 50)
ma20_1h = kl1h.get("ma20", d)
print(f"d={d}, ma20_1h={ma20_1h}, rsi1h={rsi1h}")
print(f"d < ma20_1h: {d < ma20_1h}")
print(f"35 <= rsi1h <= 45: {35 <= rsi1h <= 45}")

d1 = 0
if d < ma20_1h: d1 += 3
print(f"after ma20_1h check: d1={d1}")
