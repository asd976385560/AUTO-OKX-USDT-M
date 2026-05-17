import sqlite3, json
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
WHERE k.symbol = 'DOGE-USDT-SWAP'
""")
rows=cur.fetchall()
print("DOGE klines:")
for r in rows:
    print(r)
