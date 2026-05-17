import sqlite3, json

ROOT = r"E:\OKX\db"
NET_WORTH = 1000.0  # placeholder account equity
REGIME = "low_vol"

CTVAL = {
    "DOGE-USDT-SWAP": 1000, "BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.01,
    "FIL-USDT-SWAP": 0.01, "BLUR-USDT-SWAP": 0.1, "TRUMP-USDT-SWAP": 0.1,
    "RVN-USDT-SWAP": 10, "ATH-USDT-SWAP": 1, "SNX-USDT-SWAP": 0.01,
}

def get_ctval(sym):
    return CTVAL.get(sym, 1.0)

def margin_per_contract(price, sym, lev=10):
    return price * get_ctval(sym) / lev

conn_m = sqlite3.connect(f"{ROOT}\\market.db")
cur_m = conn_m.cursor()
cur_m.execute("""
SELECT k.symbol, k.tf, k.c, k.ma5, k.ma20, k.atr14, k.rsi14
FROM kline_cache k
INNER JOIN (
    SELECT symbol, tf, MAX(ts) as max_ts
    FROM kline_cache
    WHERE tf IN ('15m','1H','4H')
    GROUP BY symbol, tf
) sub ON k.symbol=sub.symbol AND k.tf=sub.tf AND k.ts=sub.max_ts
""")
klines_raw = cur_m.fetchall()
klines = {}
for sym, tf, c, ma5, ma20, atr14, rsi14 in klines_raw:
    if sym not in klines:
        klines[sym] = {}
    klines[sym][tf] = {"c": c, "ma5": ma5, "ma20": ma20, "atr14": atr14, "rsi14": rsi14}

cur_m.execute("""
    SELECT symbol, last, vol24h, fundingRate
    FROM tick_snapshots
    WHERE ts = (SELECT MAX(ts) FROM tick_snapshots)
""")
tickers = {r[0]: {"last": r[1], "vol24h": r[2], "fr": r[3]} for r in cur_m.fetchall()}

def score_coin(sym, t):
    d = t["last"]
    vol = t["vol24h"]
    fr = t["fr"] or 0.0
    kl15 = klines.get(sym, {}).get("15m", {})
    kl1h = klines.get(sym, {}).get("1H", {})
    kl4h = klines.get(sym, {}).get("4H", {})
    
    rsi15 = kl15.get("rsi14") or 50
    rsi1h = kl1h.get("rsi14") or 50
    rsi4h = kl4h.get("rsi14") or 50
    ma5_15 = kl15.get("ma5") or d
    ma20_15 = kl15.get("ma20") or d
    ma5_1h = kl1h.get("ma5") or d
    ma20_1h = kl1h.get("ma20") or d
    ma5_4h = kl4h.get("ma5") or d
    ma20_4h = kl4h.get("ma20") or d

    d1 = 0
    if d < ma20_1h: d1 += 3
    elif d < ma5_1h: d1 += 1
    if 35 <= rsi1h <= 45: d1 += 4
    elif rsi1h < 35: d1 += 1
    elif rsi1h > 55: d1 -= 2
    if d < ma20_4h: d1 += 2
    if rsi4h < 40: d1 += 1
    d1 = max(0, min(10, d1))
    
    d2 = 0
    if vol > 5e6: d2 += 3
    elif vol > 1e6: d2 += 2
    elif vol > 1e5: d2 += 1
    if fr < -0.0002: d2 += 4
    elif fr < -0.00005: d2 += 2
    elif fr > 0.0002: d2 -= 3
    if REGIME == "low_vol": d2 += 1
    d2 = max(0, min(10, d2))
    
    d3 = 5
    d4 = 4
    d5 = 5
    if fr < -0.0002: d5 += 3
    elif fr < -0.00005: d5 += 1
    elif fr > 0.0002: d5 -= 3
    d5 = max(0, min(10, d5))
    
    total = d1 + d2 + d3 + d4 + d5
    return {"dim1": d1, "dim2": d2, "dim3": d3, "dim4": d4, "dim5": d5, "total": total}

# Score specific coins
for sym in ["DOGE-USDT-SWAP", "FIL-USDT-SWAP", "BLUR-USDT-SWAP", "TRUMP-USDT-SWAP"]:
    t = tickers.get(sym)
    if not t:
        print(f"{sym}: NOT IN TICKERS")
        continue
    mp = margin_per_contract(t["last"], sym, 10)
    if mp > NET_WORTH * 0.10:
        print(f"{sym}: FILTERED OUT (mp={mp:.2f} > {NET_WORTH*0.10:.2f})")
        continue
    s = score_coin(sym, t)
    print(f"{sym}: D1={s['dim1']}, D2={s['dim2']}, D3={s['dim3']}, D4={s['dim4']}, D5={s['dim5']}, Total={s['total']}, last={t['last']}, fr={t['fr']}, vol={t['vol24h']}")
