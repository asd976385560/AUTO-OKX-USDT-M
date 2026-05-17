import sqlite3
conn = sqlite3.connect(r'E:\OKX\db\market.db')
c = conn.cursor()

# Get DOGE 1H klines for ATR calculation
c.execute("""
SELECT ts, o, h, l, c, v, atr14, rsi14 
FROM kline_cache 
WHERE symbol='DOGE-USDT-SWAP' AND tf='1H' 
ORDER BY ts DESC LIMIT 20
""")
rows = c.fetchall()
print('DOGE 1H klines (recent 20):')
for r in rows[:10]:
    print(f'  {r[0]} | O={r[1]:.5f} H={r[2]:.5f} L={r[3]:.5f} C={r[4]:.5f} V={r[5]:.0f} | ATR14={r[6]:.6f} RSI14={r[7]:.2f}')

# Get DOGE 15m klines
c.execute("""
SELECT ts, o, h, l, c, v, atr14, rsi14 
FROM kline_cache 
WHERE symbol='DOGE-USDT-SWAP' AND tf='15m' 
ORDER BY ts DESC LIMIT 10
""")
rows15 = c.fetchall()
print('\nDOGE 15m klines (recent 10):')
for r in rows15[:5]:
    print(f'  {r[0]} | O={r[1]:.5f} H={r[2]:.5f} L={r[3]:.5f} C={r[4]:.5f} | ATR14={r[6]:.6f} RSI14={r[7]:.2f}')

# Get latest cross_market
c.execute("SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1")
cm = c.fetchone()
print(f'\nCross market: {cm}')

conn.close()
