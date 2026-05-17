"""
Job B Full Coin Scan - 2026-05-17T06:20:00Z
Live OKX data + scoring - FIXED
"""
import subprocess, json, sqlite3, math, re
from datetime import datetime, timezone

RUN_TS = "2026-05-17T06:20:00Z"
NET_WORTH = 1000.0  # placeholder account equity
REGIME = "low_vol"
DXY = 118.04
VIX = 17.26

# ---- 1. Fetch all USDT-M SWAP tickers from OKX live ----
print("Fetching all USDT-M SWAP tickers from OKX live...")
result = subprocess.run(
    ["pwsh", "-NoProfile", "-Command", "okx market tickers SWAP 2>&1"],
    capture_output=True, text=True, timeout=90
)
raw = result.stdout
lines = raw.strip().split("\n")

# Parse tickers
tickers = {}
header_found = False
for line in lines:
    line = line.strip()
    if not line or "----" in line or line.startswith("Environment:"):
        if "last" in line.lower():
            header_found = True
        continue
    if not header_found:
        continue
    # instId, last, 24h high, 24h low, 24h vol
    parts = line.split()
    if len(parts) < 5:
        continue
    inst_id = parts[0]
    try:
        last = float(parts[1])
        high24h = float(parts[2])
        low24h = float(parts[3])
        vol24h = float(parts[4])
        chg_pct = ((last - low24h) / low24h * 100) if low24h > 0 else 0.0
    except:
        continue
    tickers[inst_id] = {
        "last": last,
        "high24h": high24h,
        "low24h": low24h,
        "vol24h": vol24h,
        "change_24h_pct": chg_pct,
        "symbol": inst_id
    }

print(f"Parsed {len(tickers)} tickers")

# ---- 2. Load funding rates from DB (stale but usable) ----
db_mkt = 'E:/OKX/db/market.db'
conn = sqlite3.connect(db_mkt)
c = conn.cursor()
c.execute('SELECT symbol, funding_rate FROM derivatives ORDER BY ts DESC')
funding_rows = c.fetchall()
funding_rates = {}
for row in funding_rows:
    if row[0] not in funding_rates:
        funding_rates[row[0]] = row[1]
conn.close()
print(f"Loaded {len(funding_rates)} funding rates from DB")

# ---- 3. ctVal lookup ----
CTVAL_MAP = {
    "DOGE-USDT-SWAP": 1000,
    "SHIB-USDT-SWAP": 1000000,
    "NOT-USDT-SWAP": 10000,
    "WIF-USDT-SWAP": 10,
    "PEPE-USDT-SWAP": 1000000,
    "BONK-USDT-SWAP": 100,
    "FLOKI-USDT-SWAP": 100,
    "IMASU-USDT-SWAP": 100,
    "BIT-USDT-SWAP": 10,
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.01,
    "SOL-USDT-SWAP": 0.1,
    "XRP-USDT-SWAP": 100,
    "ADA-USDT-SWAP": 100,
    "AVAX-USDT-SWAP": 1,
    "DOT-USDT-SWAP": 1,
    "MATIC-USDT-SWAP": 1,
    "LINK-USDT-SWAP": 1,
    "UNI-USDT-SWAP": 1,
    "ATOM-USDT-SWAP": 1,
    "LTC-USDT-SWAP": 0.1,
    "BCH-USDT-SWAP": 0.01,
    "APT-USDT-SWAP": 0.1,
    "ARB-USDT-SWAP": 1,
    "OP-USDT-SWAP": 1,
    "SUI-USDT-SWAP": 1,
    "TIA-USDT-SWAP": 0.1,
    "INJ-USDT-SWAP": 0.1,
    "WLD-USDT-SWAP": 10,
    "W-USDT-SWAP": 1000,
    "FIL-USDT-SWAP": 1,
    "ICP-USDT-SWAP": 0.1,
    "HYPE-USDT-SWAP": 0.01,
    "XAG-USDT-SWAP": 1000,
    "XAU-USDT-SWAP": 0.01,
    "DOGE-USDT-SWAP": 1000,
    "NEIRO-USDT-SWAP": 100000,
    "PNUT-USDT-SWAP": 100,
    "MEME-USDT-SWAP": 1000000,
    "TRUMP-USDT-SWAP": 1,
    "MELANIA-USDT-SWAP": 1,
    "AI16Z-USDT-SWAP": 100,
    "GIGA-USDT-SWAP": 100,
    "FARTCOIN-USDT-SWAP": 100,
    "PNUT-USDT-SWAP": 100,
}

def get_ctval(symbol):
    if symbol in CTVAL_MAP:
        return CTVAL_MAP[symbol]
    return 1  # Default

# ---- 4. Scoring function ----
def five_dim_score(symbol, t, funding, regime, dxy, vix, net_worth):
    p = t.get("last", 0) or 0
    chg = t.get("change_24h_pct", 0) or 0
    vol = t.get("vol24h", 0) or 0
    high24h = t.get("high24h", p * 1.02) or p * 1.02
    low24h = t.get("low24h", p * 0.98) or p * 0.98
    
    # D1 - Technical (0-10): momentum + position in 24h range
    tech = 5
    if chg < -5:
        tech += 2
    elif chg < -3:
        tech += 1
    # Near 24h lows = oversold = better for SHORT
    if high24h > low24h and p > 0:
        pct_from_low = (p - low24h) / (high24h - low24h) * 100
        if pct_from_low < 20:
            tech += 2  # Deep oversold
        elif pct_from_low < 35:
            tech += 1  # Mild oversold
        elif pct_from_low > 80:
            tech -= 1  # Near 24h high
    tech = min(10, max(0, tech))
    
    # D2 - Structure / Volume (0-10)
    struct = 5
    if abs(chg) > 5:
        struct += 3
    elif abs(chg) > 3:
        struct += 2
    elif abs(chg) > 1:
        struct += 1
    if vol > 5e7:
        struct += 1
    if vol > 1e8:
        struct += 1
    struct = min(10, max(0, struct))
    
    # D3 - Funding / Flow (0-10)
    fr = funding or 0
    fund_score = 5
    if fr < -0.0003:
        fund_score += 3
    elif fr < -0.0001:
        fund_score += 2
    elif fr < 0:
        fund_score += 1
    elif fr > 0.0003:
        fund_score -= 3
    elif fr > 0.0001:
        fund_score -= 2
    else:
        fund_score -= 1
    fund_score = min(10, max(0, fund_score))
    
    # D4 - Cross Market (0-10)
    cross = 6
    if regime == "low_vol":
        cross -= 1
    if dxy > 117.5:
        cross -= 1
    if vix > 20:
        cross -= 1
    cross = min(10, max(0, cross))
    
    # D5 - Sentiment / Momentum (0-10)
    sent = 5
    if chg < -5:
        sent += 2
    elif chg < -2:
        sent += 1
    elif chg > 5:
        sent -= 2
    elif chg > 2:
        sent -= 1
    sent = min(10, max(0, sent))
    
    total = tech + struct + fund_score + cross + sent
    
    return {
        "dim1_tech": tech,
        "dim2_struct": struct,
        "dim3_funding": fund_score,
        "dim4_cross": cross,
        "dim5_sent": sent,
        "total": total
    }

# ---- 5. ctVal filter ----
def ctval_filter(symbol, price, ctval, net_worth):
    lev = 10
    margin_per_contract = price * ctval / lev
    max_margin = net_worth * 0.10
    if margin_per_contract > max_margin:
        return False, margin_per_contract, 0
    max_contracts = int(max_margin / margin_per_contract)
    return True, margin_per_contract, max_contracts

# ---- 6. Full scan ----
print("\n=== FULL COIN SCAN ===")
results = []
for symbol, t in tickers.items():
    p = t.get("last")
    if not p or p <= 0:
        continue
    ctval = get_ctval(symbol)
    fr = funding_rates.get(symbol, 0)
    
    passes, margin_per_contract, max_cts = ctval_filter(symbol, p, ctval, NET_WORTH)
    if not passes:
        continue
    
    score = five_dim_score(symbol, t, fr, REGIME, DXY, VIX, NET_WORTH)
    results.append({
        "symbol": symbol,
        "price": p,
        "ctval": ctval,
        "margin_per_contract_10x": round(margin_per_contract, 4),
        "max_contracts": max_cts,
        "change_24h_pct": round(t.get("change_24h_pct", 0), 2),
        "vol24h": t.get("vol24h", 0),
        "funding_rate": fr,
        **score
    })

results.sort(key=lambda x: x["total"], reverse=True)

print(f"\nTotal feasible coins after ctVal filter: {len(results)}")
print(f"\n{'Rank':<4} {'Symbol':<22} {'Price':<12} {'Chg%':<8} {'ctVal':<12} {'$/Ct':<8} {'MaxCt':<6} {'D1':<4} {'D2':<4} {'D3':<4} {'D4':<4} {'D5':<4} {'Total':<6}")
for i, r in enumerate(results[:35], 1):
    print(f"{i:<4} {r['symbol']:<22} {r['price']:<12.6f} {r['change_24h_pct']:<8.2f} {r['ctval']:<12.4f} {r['margin_per_contract_10x']:<8.2f} {r['max_contracts']:<6} {r['dim1_tech']:<4} {r['dim2_struct']:<4} {r['dim3_funding']:<4} {r['dim4_cross']:<4} {r['dim5_sent']:<4} {r['total']:<6}")

top20 = results[:20]
top3 = results[:3]

print(f"\n=== TOP 20 ===")
for i, r in enumerate(top20, 1):
    fr_val = r['funding_rate']
    fr_str = f"{fr_val:.4f}" if fr_val else "N/A"
    print(f"{i:2d}. {r['symbol']}: score={r['total']} | D1={r['dim1_tech']} D2={r['dim2_struct']} D3={r['dim3_funding']} D4={r['dim4_cross']} D5={r['dim5_sent']} | 24h={r['change_24h_pct']:+.2f}% | funding={fr_str} | $/ct={r['margin_per_contract_10x']:.2f}")

print(f"\n=== TOP 3 DEEP ANALYSIS ===")
for i, r in enumerate(top3, 1):
    print(f"\n{i}. {r['symbol']}: price={r['price']}, score={r['total']}")
    print(f"   ctVal={r['ctval']}, $/ct@10x=${r['margin_per_contract_10x']:.2f}, max_contracts={r['max_contracts']}")
    print(f"   24h change={r['change_24h_pct']:+.2f}%, vol24h={r['vol24h']:.0f}")
    print(f"   funding={r['funding_rate']:.6f}")

print("\n=== SCAN COMPLETE ===")

with open("E:/OKX/scripts/jobb_scan_results.json", "w") as f:
    json.dump({
        "run_ts": RUN_TS,
        "net_worth": NET_WORTH,
        "regime": REGIME,
        "dxy": DXY,
        "vix": VIX,
        "all_results": results,
        "top20": top20,
        "top3": top3
    }, f, indent=2)
print("Results saved")
