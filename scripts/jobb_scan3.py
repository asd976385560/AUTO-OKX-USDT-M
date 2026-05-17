"""
Job B Full Coin Scan - 2026-05-17T06:20:00Z
Uses DB tick_snapshots + live DOGE overlay
"""
import subprocess, json, sqlite3, math

RUN_TS = "2026-05-17T06:20:00Z"
NET_WORTH = 1000.0  # placeholder account equity
REGIME = "low_vol"
DXY = 118.04
VIX = 17.26

# Live DOGE data
LIVE_DOGE_PRICE = 0.0  # redacted live price placeholder
LIVE_DOGE_24H_OPEN = 0.11356  # from OKX ticker output
LIVE_DOGE_24H_HIGH = 0.11375
LIVE_DOGE_24H_LOW = 0.10773
LIVE_DOGE_CHG = ((LIVE_DOGE_PRICE - LIVE_DOGE_24H_OPEN) / LIVE_DOGE_24H_OPEN * 100)

# ---- 1. Load tick_snapshots from DB ----
db_mkt = 'E:/OKX/db/market.db'
conn = sqlite3.connect(db_mkt)
c = conn.cursor()

c.execute('SELECT MAX(ts) FROM tick_snapshots')
max_ts = c.fetchone()[0]
print(f"DB tick_snapshots max ts: {max_ts}")

c.execute('''
SELECT ts, symbol, last, bid, ask, vol24h, fundingRate
FROM tick_snapshots
WHERE ts = ?
ORDER BY vol24h DESC
''', (max_ts,))
rows = c.fetchall()
t_cols = [d[0] for d in c.description]
tickers_db = {}
for row in rows:
    d = dict(zip(t_cols, row))
    sym = d['symbol']
    if sym not in tickers_db:
        tickers_db[sym] = d

print(f"Loaded {len(tickers_db)} tickers from DB")

# ---- 2. Load cross_market ----
c.execute('SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1')
cm = c.fetchone()
cm_cols = [d[0] for d in c.description]
cm_data = dict(zip(cm_cols, cm)) if cm else {}
regime_db = cm_data.get('regime', 'low_vol')
dxy_db = cm_data.get('dxy', DXY)
vix_db = cm_data.get('vix', VIX)
print(f"DB regime={regime_db}, DXY={dxy_db}, VIX={vix_db}")
conn.close()

# ---- 3. Fetch live DOGE funding rate ----
print("\nFetching live DOGE funding rate...")
doge_fr = None
result = subprocess.run(
    ["pwsh", "-NoProfile", "-Command", 
     "okx market funding-rate DOGE-USDT-SWAP 2>&1"],
    capture_output=True, text=True, timeout=15
)
for line in result.stdout.split("\n"):
    line = line.strip()
    if line.startswith("fundingRate"):
        try:
            doge_fr = float(line.split()[1])
            print(f"DOGE live funding rate: {doge_fr}")
        except:
            pass

# ---- 4. Replace DOGE with live data ----
if "DOGE-USDT-SWAP" in tickers_db:
    tickers_db["DOGE-USDT-SWAP"]["last"] = LIVE_DOGE_PRICE
    tickers_db["DOGE-USDT-SWAP"]["vol24h"] = 3892276.15  # from live ticker
    if doge_fr is not None:
        tickers_db["DOGE-USDT-SWAP"]["fundingRate"] = doge_fr

# ---- 5. ctVal map (OKX actual values) ----
CTVAL_MAP = {
    "DOGE-USDT-SWAP": 1000,
    "SHIB-USDT-SWAP": 1000000,
    "NOT-USDT-SWAP": 10000,
    "WIF-USDT-SWAP": 10,
    "PEPE-USDT-SWAP": 1000000,
    "BONK-USDT-SWAP": 100,
    "FLOKI-USDT-SWAP": 100,
    "IMASU-USDT-SWAP": 100,
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
    "NEIRO-USDT-SWAP": 100000,
    "PNUT-USDT-SWAP": 100,
    "MEME-USDT-SWAP": 1000000,
    "TRUMP-USDT-SWAP": 1,
    "FARTCOIN-USDT-SWAP": 100,
    "GPS-USDT-SWAP": 100,
    "LAYER-USDT-SWAP": 100,
    "CHIP-USDT-SWAP": 100,
    "ACT-USDT-SWAP": 100,
    "SPK-USDT-SWAP": 100,
    "BIO-USDT-SWAP": 100,
    "MERL-USDT-SWAP": 100,
    "BERA-USDT-SWAP": 100,
    "APE-USDT-SWAP": 100,
    "RUNE-USDT-SWAP": 100,
    "GALA-USDT-SWAP": 1000000,
    "CORE-USDT-SWAP": 100,
    "STRK-USDT-SWAP": 100,
    "CHZ-USDT-SWAP": 100,
    "EDEN-USDT-SWAP": 100,
    "PI-USDT-SWAP": 100,
    "YGG-USDT-SWAP": 100,
    "ZBT-USDT-SWAP": 100,
    "RIVER-USDT-SWAP": 0.1,
    "PROS-USDT-SWAP": 0.1,
    "BABY-USDT-SWAP": 100,
    "LUNA-USDT-SWAP": 100,
    "RECALL-USDT-SWAP": 100,
    "ZEC-USDT-SWAP": 0.01,
    "PIPPIN-USDT-SWAP": 100,
    "BASED-USDT-SWAP": 100,
    "WLFI-USDT-SWAP": 100,
    "AEVO-USDT-SWAP": 100,
    "AXS-USDT-SWAP": 10,
    "CRV-USDT-SWAP": 1000,
    "YB-USDT-SWAP": 100,
    "HMSTR-USDT-SWAP": 100000,
    "ORDI-USDT-SWAP": 0.01,
    "COAI-USDT-SWAP": 1,
    "AIXBT-USDT-SWAP": 100,
    "TON-USDT-SWAP": 100,
    "IP-USDT-SWAP": 0.1,
    "DYDX-USDT-SWAP": 100,
    "BILL-USDT-SWAP": 100,
    "LIGHT-USDT-SWAP": 100,
    "CFX-USDT-SWAP": 100,
    "PENGU-USDT-SWAP": 100,
    "DASH-USDT-SWAP": 0.1,
    "MON-USDT-SWAP": 100,
    "ANIME-USDT-SWAP": 100,
    "OKB-USDT-SWAP": 1,
    "JTO-USDT-SWAP": 10,
    "ASTER-USDT-SWAP": 1,
    "EIGEN-USDT-SWAP": 100,
    "BICO-USDT-SWAP": 100,
    "AR-USDT-SWAP": 0.1,
    "AVNT-USDT-SWAP": 100,
    "IMX-USDT-SWAP": 100,
    "ENA-USDT-SWAP": 100,
    "GIGGLE-USDT-SWAP": 0.01,
    "ZEN-USDT-SWAP": 1,
    "SAGA-USDT-SWAP": 10,
    "STX-USDT-SWAP": 10,
    "KAIA-USDT-SWAP": 100,
    "ZRX-USDT-SWAP": 100,
    "MASK-USDT-SWAP": 10,
    "ENS-USDT-SWAP": 10,
    "RENDER-USDT-SWAP": 0.1,
    "GRASS-USDT-SWAP": 100,
    "PORTAL-USDT-SWAP": 100,
    "ALT-USDT-SWAP": 100,
    "SYRUP-USDT-SWAP": 100,
    "DOGS-USDT-SWAP": 10000,
    "CLANKER-USDT-SWAP": 100,
    "SLERF-USDT-SWAP": 100,
    "SUI-USDT-SWAP": 1,
    "CRO-USDT-SWAP": 1000,
    "SUSHI-USDT-SWAP": 10,
    "RAVE-USDT-SWAP": 100,
    "XLM-USDT-SWAP": 100,
    "GPS-USDT-SWAP": 100,
    "EDGE-USDT-SWAP": 10,
    "TRUST-USDT-SWAP": 100,
    "ACU-USDT-SWAP": 100,
    "WDC-USDT-SWAP": 0.01,
    "ZKP-USDT-SWAP": 100,
    "LITE-USDT-SWAP": 0.1,
    "MSTR-USDT-SWAP": 0.01,
    "ENJ-USDT-SWAP": 100,
    "RAY-USDT-SWAP": 10,
    "OL-USDT-SWAP": 100,
    "VANA-USDT-SWAP": 100,
    "CHAMP-USDT-SWAP": 100,
    "LISTA-USDT-SWAP": 100,
    "REZ-USDT-SWAP": 100,
    "BSW-USDT-SWAP": 100,
    "AERO-USDT-SWAP": 100,
    "LISTA-USDT-SWAP": 100,
    "XLD-USDT-SWAP": 100,
    "KOMA-USDT-SWAP": 100,
    "DOGE-USDT-SWAP": 1000,
    "RENDER-USDT-SWAP": 0.1,
    "KAIA-USDT-SWAP": 100,
    "GRASS-USDT-SWAP": 100,
    "PORTAL-USDT-SWAP": 100,
    "PUFFER-USDT-SWAP": 100,
    "ATH-USDT-SWAP": 10,
    "WHALE-USDT-SWAP": 100,
    "ATHENA-USDT-SWAP": 100,
    "MEW-USDT-SWAP": 100,
    "FWOG-USDT-SWAP": 100,
    "NEXA-USDT-SWAP": 100,
}

def get_ctval(symbol):
    return CTVAL_MAP.get(symbol, 1)

# ---- 6. Scoring ----
def five_dim_score(symbol, t, regime, dxy, vix, chg_override=None):
    p = t.get("last", 0) or 0
    vol = t.get("vol24h", 0) or 0
    fr = t.get("fundingRate", 0) or 0
    
    # 24h change: use override for DOGE, otherwise estimate
    if chg_override is not None:
        chg = chg_override
    elif fr != 0:
        # Funding rate can give hint: positive funding = bulls paying = price up
        chg = fr * 100 * 8  # rough estimate: 8 periods in 24h
        chg = max(-20, min(20, chg))
    else:
        chg = 0.0
    
    # D1 - Technical (0-10)
    tech = 5
    if chg < -5:
        tech += 2
    elif chg < -3:
        tech += 1
    # Volume filter
    if vol > 5e7:
        tech += 0.5
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
    if vol < 1e5:
        struct -= 1
    struct = min(10, max(0, struct))
    
    # D3 - Funding / Flow (0-10)
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
        "total": total,
        "chg_24h_est": round(chg, 2)
    }

# ---- 7. ctVal filter ----
def ctval_filter(symbol, price, ctval, net_worth):
    lev = 10
    margin_per_contract = price * ctval / lev
    max_margin = net_worth * 0.10
    if margin_per_contract > max_margin:
        return False, margin_per_contract, 0
    max_contracts = int(max_margin / margin_per_contract)
    return True, margin_per_contract, max_contracts

# ---- 8. Full scan ----
print("\n=== FULL COIN SCAN ===")
results = []
for symbol, t in tickers_db.items():
    p = t.get("last")
    if not p or p <= 0:
        continue
    ctval = get_ctval(symbol)
    
    passes, margin_per_contract, max_contracts = ctval_filter(symbol, p, ctval, NET_WORTH)
    if not passes:
        continue
    
    chg_override = LIVE_DOGE_CHG if symbol == "DOGE-USDT-SWAP" else None
    score = five_dim_score(symbol, t, REGIME, DXY, VIX, chg_override)
    
    results.append({
        "symbol": symbol,
        "price": p,
        "ctval": ctval,
        "margin_per_contract_10x": round(margin_per_contract, 4),
        "max_contracts": max_contracts,
        "vol24h": t.get("vol24h", 0),
        "funding_rate": t.get("fundingRate", 0),
        **score
    })

results.sort(key=lambda x: x["total"], reverse=True)

print(f"\nTotal feasible coins after ctVal filter: {len(results)}")
print(f"\n{'Rank':<4} {'Symbol':<22} {'Price':<12} {'Chg%':<8} {'ctVal':<10} {'$/Ct':<8} {'MaxCt':<6} {'D1':<4} {'D2':<4} {'D3':<4} {'D4':<4} {'D5':<4} {'Total':<6}")
for i, r in enumerate(results[:40], 1):
    print(f"{i:<4} {r['symbol']:<22} {r['price']:<12.6f} {r['chg_24h_est']:<8.2f} {r['ctval']:<10.4f} {r['margin_per_contract_10x']:<8.2f} {r['max_contracts']:<6} {r['dim1_tech']:<4} {r['dim2_struct']:<4} {r['dim3_funding']:<4} {r['dim4_cross']:<4} {r['dim5_sent']:<4} {r['total']:<6}")

top20 = results[:20]
top3 = results[:3]

print(f"\n=== TOP 20 SCORES ===")
for i, r in enumerate(top20, 1):
    fr_val = r['funding_rate']
    fr_str = f"{fr_val:.4f}" if fr_val else "N/A"
    print(f"{i:2d}. {r['symbol']}: total={r['total']} | T={r['dim1_tech']} S={r['dim2_struct']} F={r['dim3_funding']} C={r['dim4_cross']} M={r['dim5_sent']} | chg={r['chg_24h_est']:+.2f}% | funding={fr_str} | $/ct=${r['margin_per_contract_10x']:.2f} | max={r['max_contracts']}cts")

print(f"\n=== TOP 3 DEEP ANALYSIS ===")
for i, r in enumerate(top3, 1):
    print(f"\n{i}. {r['symbol']}: price={r['price']}, total={r['total']}")
    print(f"   ctVal={r['ctval']}, $/ct@10x=${r['margin_per_contract_10x']:.2f}, max_contracts={r['max_contracts']}")
    print(f"   chg={r['chg_24h_est']:+.2f}%, vol24h={r['vol24h']:.0f}")
    print(f"   funding={r['funding_rate']:.6f}")
    print(f"   D1={r['dim1_tech']} D2={r['dim2_struct']} D3={r['dim3_funding']} D4={r['dim4_cross']} D5={r['dim5_sent']}")

print("\n=== SCAN COMPLETE ===")

with open("E:/OKX/scripts/jobb_scan_results.json", "w") as f:
    json.dump({
        "run_ts": RUN_TS,
        "net_worth": NET_WORTH,
        "regime": REGIME,
        "dxy": DXY,
        "vix": VIX,
        "live_doge_price": LIVE_DOGE_PRICE,
        "live_doge_chg": LIVE_DOGE_CHG,
        "all_results": results,
        "top20": top20,
        "top3": top3
    }, f, indent=2)
print("Results saved to jobb_scan_results.json")
