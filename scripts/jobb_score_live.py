#!/usr/bin/env python3
"""JobB full coin scoring - compute 5-dim scores for all coins - LIVE RUN"""
import sys, os, json, sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_DIR = Path(r'E:\OKX\db')
EQUITY = 1000.0  # placeholder account equity
NET_WORTH = EQUITY
LEVERAGE = 10  # Max leverage for scoring
REGIME = 'low_vol'
NOW_UTC = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

# ctVal map (expanded)
CTVLS = {
    'BTC-USDT-SWAP': 0.01, 'ETH-USDT-SWAP': 0.01, 'SOL-USDT-SWAP': 0.1,
    'DOGE-USDT-SWAP': 1000, 'SHIB-USDT-SWAP': 1000000, 'NOT-USDT-SWAP': 100,
    'PEPE-USDT-SWAP': 100000, 'FLOKI-USDT-SWAP': 100000, 'WIF-USDT-SWAP': 1,
    'BONK-USDT-SWAP': 1000, 'NEIRO-USDT-SWAP': 1000000, 'TRUMP-USDT-SWAP': 0.1,
    'W-USDT-SWAP': 10, 'WLD-USDT-SWAP': 1, 'ICP-USDT-SWAP': 0.01,
    'FIL-USDT-SWAP': 0.1, 'BERA-USDT-SWAP': 0.1, 'GALA-USDT-SWAP': 100,
    'CORE-USDT-SWAP': 0.1, 'ACT-USDT-SWAP': 1, 'STRK-USDT-SWAP': 0.1,
    'CHZ-USDT-SWAP': 1, 'GPS-USDT-SWAP': 1, 'GMT-USDT-SWAP': 0.1,
    'SUI-USDT-SWAP': 0.1, 'EDEN-USDT-SWAP': 0.1, 'LAYER-USDT-SWAP': 0.1,
    'PI-USDT-SWAP': 0.01, 'YGG-USDT-SWAP': 0.1, 'ZBT-USDT-SWAP': 0.1,
    'HYPE-USDT-SWAP': 0.01, 'RIVER-USDT-SWAP': 0.001, 'BABY-USDT-SWAP': 1,
    'LUNA-USDT-SWAP': 0.1, 'MERL-USDT-SWAP': 1, 'PROS-USDT-SWAP': 0.01,
    'BIO-USDT-SWAP': 1, 'SAHARA-USDT-SWAP': 1, 'RECALL-USDT-SWAP': 0.1,
    'PENGU-USDT-SWAP': 100, 'APT-USDT-SWAP': 0.01, 'OP-USDT-SWAP': 0.1,
    'ARB-USDT-SWAP': 0.1, 'SEI-USDT-SWAP': 0.1, 'BLUR-USDT-SWAP': 1,
    'TIA-USDT-SWAP': 0.01, 'STX-USDT-SWAP': 0.01, 'SATS-USDT-SWAP': 100000,
    'ORDI-USDT-SWAP': 0.01, 'REZ-USDT-SWAP': 100, 'EIGEN-USDT-SWAP': 0.01,
    'ZK-USDT-SWAP': 0.1, 'ZRO-USDT-SWAP': 0.01, 'GRASS-USDT-SWAP': 0.1,
    'AI16Z-USDT-SWAP': 1, 'BNB-USDT-SWAP': 0.01, 'XRP-USDT-SWAP': 10,
    'ADA-USDT-SWAP': 10, 'AVAX-USDT-SWAP': 0.1, 'DOT-USDT-SWAP': 0.1,
    'LINK-USDT-SWAP': 0.1, 'MATIC-USDT-SWAP': 0.1, 'LTC-USDT-SWAP': 0.01,
    'UNI-USDT-SWAP': 0.01, 'ATOM-USDT-SWAP': 0.01, 'XLM-USDT-SWAP': 1,
    'ETC-USDT-SWAP': 0.1, 'NEAR-USDT-SWAP': 0.1, 'ALGO-USDT-SWAP': 1,
    'XMR-USDT-SWAP': 0.01, 'BCH-USDT-SWAP': 0.01, 'BSV-USDT-SWAP': 0.01,
    'AAVE-USDT-SWAP': 0.01, 'MKR-USDT-SWAP': 0.001, 'SNX-USDT-SWAP': 0.1,
    'CRV-USDT-SWAP': 10, 'LDO-USDT-SWAP': 0.1, 'RUNE-USDT-SWAP': 0.1,
    'INJ-USDT-SWAP': 0.01, 'FTM-USDT-SWAP': 10, 'IMX-USDT-SWAP': 0.1,
    'MANA-USDT-SWAP': 10, 'SAND-USDT-SWAP': 10, 'AXS-USDT-SWAP': 0.01,
    'THETA-USDT-SWAP': 0.1, 'ALGO-USDT-SWAP': 1, 'VET-USDT-SWAP': 10,
    'KAS-USDT-SWAP': 0.1, 'MINA-USDT-SWAP': 0.1, 'IOTX-USDT-SWAP': 10,
    'RNDR-USDT-SWAP': 0.1, 'OCEAN-USDT-SWAP': 0.1, 'COTI-USDT-SWAP': 10,
    'STORJ-USDT-SWAP': 0.1, 'SKL-USDT-SWAP': 1, 'KAVA-USDT-SWAP': 0.1,
    'COMP-USDT-SWAP': 0.01, 'DASH-USDT-SWAP': 0.01, 'EOS-USDT-SWAP': 1,
    'ZEC-USDT-SWAP': 0.01, 'ZIL-USDT-SWAP': 10, 'ZEN-USDT-SWAP': 0.01,
    'XTZ-USDT-SWAP': 0.1, 'ENJ-USDT-SWAP': 1, 'BAT-USDT-SWAP': 10,
    'HOT-USDT-SWAP': 100, '1INCH-USDT-SWAP': 1, 'LRC-USDT-SWAP': 1,
    'CHZ-USDT-SWAP': 1, 'ANRK-USDT-SWAP': 10, 'ACH-USDT-SWAP': 10,
    'RNDR-USDT-SWAP': 0.1, 'GODS-USDT-SWAP': 1, 'MAGIC-USDT-SWAP': 0.1,
    'GALA-USDT-SWAP': 100, 'PROM-USDT-SWAP': 0.01, 'GNS-USDT-SWAP': 0.1,
    'WAXL-USDT-SWAP': 1, 'HIFI-USDT-SWAP': 0.1, 'GODS-USDT-SWAP': 1,
    'PEAK-USDT-SWAP': 1, 'KLAY-USDT-SWAP': 0.1, 'AUDIO-USDT-SWAP': 1,
    'MASK-USDT-SWAP': 0.1, 'BLZ-USDT-SWAP': 1, 'C98-USDT-SWAP': 1,
    'STG-USDT-SWAP': 0.1, 'SUN-USDT-SWAP': 10, 'AR-USDT-SWAP': 0.01,
    'BOND-USDT-SWAP': 0.01, 'RAD-USDT-SWAP': 0.01, 'RIF-USDT-SWAP': 10,
    'LOKA-USDT-SWAP': 0.1, 'IRIS-USDT-SWAP': 10, 'KDA-USDT-SWAP': 1,
    'CSF-USDT-SWAP': 1, 'NTRN-USDT-SWAP': 0.01, 'BEAM-USDT-SWAP': 10,
    'XVG-USDT-SWAP': 100, 'ANKR-USDT-SWAP': 10, 'REQ-USDT-SWAP': 1,
    'GODS-USDT-SWAP': 1, 'ANC-USDT-SWAP': 1, 'FRAX-USDT-SWAP': 0.01,
    'CRV-USDT-SWAP': 10, 'LUNA2-USDT-SWAP': 0.1, 'SXP-USDT-SWAP': 0.1,
    'OGN-USDT-SWAP': 10, 'MTL-USDT-SWAP': 0.1, 'T-USDT-SWAP': 0.1,
    'ZEC-USDT-SWAP': 0.01, 'IOTA-USDT-SWAP': 1, 'NEO-USDT-SWAP': 0.01,
    'WAVES-USDT-SWAP': 0.01, 'ZRX-USDT-SWAP': 1, 'SFP-USDT-SWAP': 1,
    'CTK-USDT-SWAP': 1, 'COTI-USDT-SWAP': 10, 'SC-USDT-SWAP': 100,
    'ALPHA-USDT-SWAP': 1, 'BOND-USDT-SWAP': 0.01, 'TRB-USDT-SWAP': 0.01,
    'LINA-USDT-SWAP': 10, 'RSR-USDT-SWAP': 100, 'OGN-USDT-SWAP': 10,
    'CTSI-USDT-SWAP': 1, 'ROSE-USDT-SWAP': 10, 'IDEX-USDT-SWAP': 10,
    'UTK-USDT-SWAP': 1, 'DATA-USDT-SWAP': 1, 'WABI-USDT-SWAP': 1,
    'DENT-USDT-SWAP': 100, 'KEY-USDT-SWAP': 10, 'NKN-USDT-SWAP': 1,
    'ONG-USDT-SWAP': 1, 'FUEL-USDT-SWAP': 1, 'PHB-USDT-SWAP': 0.1,
    'XVS-USDT-SWAP': 0.1, 'VTHO-USDT-SWAP': 100, 'DREP-USDT-SWAP': 1,
    'FIRO-USDT-SWAP': 0.1, 'DGB-USDT-SWAP': 10, 'SCRT-USDT-SWAP': 0.1,
    'ARPA-USDT-SWAP': 10, 'HIVE-USDT-SWAP': 1, 'STPT-USDT-SWAP': 1,
    'BETA-USDT-SWAP': 1, 'TORN-USDT-SWAP': 0.01, 'ADS-USDT-SWAP': 1,
    'JST-USDT-SWAP': 10, 'BIT-USDT-SWAP': 0.1, 'REEF-USDT-SWAP': 100,
    'SWFTC-USDT-SWAP': 10, 'NULS-USDT-SWAP': 1, 'POWR-USDT-SWAP': 1,
    'QNT-USDT-SWAP': 0.01, 'GNO-USDT-SWAP': 0.001, 'FET-USDT-SWAP': 0.1,
    'OCEAN-USDT-SWAP': 0.1, 'LTO-USDT-SWAP': 1, 'ARK-USDT-SWAP': 0.01,
    'YGG-USDT-SWAP': 0.1, 'MXC-USDT-SWAP': 10, 'ZKS-USDT-SWAP': 0.1,
    'KEEP-USDT-SWAP': 1, 'NMR-USDT-SWAP': 0.01, 'PAXG-USDT-SWAP': 0.001,
    'KNC-USDT-SWAP': 1, 'LOCUS-USDT-SWAP': 1, 'CQT-USDT-SWAP': 1,
    'COS-USDT-SWAP': 10, 'PNT-USDT-SWAP': 1, 'MCO-USDT-SWAP': 0.01,
    'ZEC-USDT-SWAP': 0.01, 'AVA-USDT-SWAP': 0.1, 'APPC-USDT-SWAP': 1,
    'ADX-USDT-SWAP': 1, 'AION-USDT-SWAP': 1, 'EQLD-USDT-SWAP': 1,
    'MFT-USDT-SWAP': 100, 'AUTO-USDT-SWAP': 1, 'QLC-USDT-SWAP': 1,
    'VRA-USDT-SWAP': 10, 'FIC-USDT-SWAP': 1, 'PVT-USDT-SWAP': 1,
    'TIPS-USDT-SWAP': 1, 'XEM-USDT-SWAP': 1, 'AAG-USDT-SWAP': 1,
    'GSE-USDT-SWAP': 1, 'LOKI-USDT-SWAP': 1, 'DOCK-USDT-SWAP': 1,
    'EULER-USDT-SWAP': 0.01, 'DAO-USDT-SWAP': 1, 'SWAP-USDT-SWAP': 1,
    'TRAC-USDT-SWAP': 0.1, 'DAD-USDT-SWAP': 1, 'DIA-USDT-SWAP': 1,
    'FIDA-USDT-SWAP': 1, 'HIFI-USDT-SWAP': 0.1, 'HFT-USDT-SWAP': 0.1,
    'PORTAL-USDT-SWAP': 1, 'AXL-USDT-SWAP': 1, 'WLD-USDT-SWAP': 1,
    'DOGE-USDT-SWAP': 1000, 'HIGH-USDT-SWAP': 0.1, 'SPELL-USDT-SWAP': 100,
    'USTC-USDT-SWAP': 1, 'BTC-USDT-SWAP': 0.01, 'ETH-USDT-SWAP': 0.01,
}

SKIP_SYMBOLS = ['TSLA-USDT-SWAP','AAPL-USDT-SWAP','NVDA-USDT-SWAP','GOOGL-USDT-SWAP',
    'MSFT-USDT-SWAP','AMZN-USDT-SWAP','META-USDT-SWAP','NFLX-USDT-SWAP',
    'QQQ-USDT-SWAP','SPY-USDT-SWAP','IWM-USDT-SWAP','EWJ-USDT-SWAP',
    'XAG-USDT-SWAP','XAU-USDT-SWAP','XCU-USDT-SWAP','XLE-USDT-SWAP',
    'XPT-USDT-SWAP','XPD-USDT-SWAP','CRCL-USDT-SWAP','CRWV-USDT-SWAP',
    'CL-USDT-SWAP','NG-USDT-SWAP','GAS-USDT-SWAP','SPACEX-USDT-SWAP']

def get_ctval(symbol):
    return CTVLS.get(symbol, 1)

def query_db(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def main():
    # Get tick snapshots
    rows = query_db(DB_DIR / 'market.db',
        """SELECT t.* FROM tick_snapshots t
           WHERE t.ts = (SELECT MAX(ts) FROM tick_snapshots)""")
    ticks = {r['symbol']: r for r in rows}
    
    # Get derivatives
    rows = query_db(DB_DIR / 'market.db',
        """SELECT d.* FROM derivatives d
           WHERE d.ts = (SELECT MAX(ts) FROM derivatives)""")
    derivs = {r['symbol']: r for r in rows}
    
    # Get kline 15m
    rows = query_db(DB_DIR / 'market.db',
        """SELECT k.symbol, k.rsi14, k.ma5, k.ma20, k.atr14, k.c as close, k.o as open
           FROM kline_cache k
           WHERE k.tf='15m'
           AND k.ts = (SELECT MAX(ts) FROM kline_cache WHERE tf='15m' AND symbol=k.symbol)""")
    klines_15m = {r['symbol']: r for r in rows}
    
    # Get cross_market
    rows = query_db(DB_DIR / 'market.db', "SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1")
    cross = rows[0] if rows else {}
    
    results = []
    DOGE_last = None
    
    for symbol, tick in ticks.items():
        last = tick.get('last') or 0
        if not last or last <= 0:
            continue
        if symbol in SKIP_SYMBOLS:
            continue
        if 'USDT' not in symbol:
            continue
        if symbol in ['USDC-USDT-SWAP', 'STABLE-USDT-SWAP', 'USELESS-USDT-SWAP']:
            continue
        
        deriv = derivs.get(symbol, {})
        kline = klines_15m.get(symbol, {})
        
        funding = deriv.get('funding_rate') or 0
        vol24h = tick.get('vol24h') or 0
        rsi15 = kline.get('rsi14') or 50
        ma5 = kline.get('ma5') or last
        ma20 = kline.get('ma20') or last
        atr14 = kline.get('atr14') or 0
        
        ctval = get_ctval(symbol)
        margin_per_contract = last * ctval / LEVERAGE
        budget_10pct = NET_WORTH * 0.10
        feasible = margin_per_contract <= budget_10pct
        
        # 1. Technical (0-10)
        dim1 = 5
        if last < ma5 and last < ma20:
            dim1 += 3
        elif last < ma5 or last < ma20:
            dim1 += 1
        if rsi15 < 30:
            dim1 += 1
        elif rsi15 < 40:
            dim1 += 2
        elif rsi15 > 70:
            dim1 -= 2
        elif rsi15 > 60:
            dim1 -= 1
        dim1 = max(0, min(10, dim1))
        
        # 2. Structure/Volume-Price (0-10)
        dim2 = 5
        if last < ma20:
            dim2 += 2
        # Use tick open field if available, otherwise estimate from 24h change
        tick_open = tick.get('open')
        if tick_open and tick_open > 0:
            chg24 = (last - tick_open) / tick_open * 100
        else:
            chg24 = 0
        if chg24 < -3:
            dim2 += 1
        if vol24h > 5000000:
            dim2 += 1
        dim2 = max(0, min(10, dim2))
        
        # 3. News/Events (0-10)
        dim3 = 5
        
        # 4. Cross-Market (0-10)
        dim4 = 5
        dxy = cross.get('dxy') or 118
        if dxy > 118:
            dim4 += 2
        btc_dom = cross.get('btc_dominance') or 58
        if btc_dom > 58:
            dim4 += 1
        dim4 = max(0, min(10, dim4))
        
        # 5. Funding/Sentiment (0-10)
        dim5 = 5
        if funding < -0.0002:
            dim5 += 3
        elif funding < 0:
            dim5 += 1
        elif funding > 0.0002:
            dim5 -= 2
        if rsi15 < 35:
            dim5 += 1
        dim5 = max(0, min(10, dim5))
        
        total = dim1 + dim2 + dim3 + dim4 + dim5
        
        if symbol == 'DOGE-USDT-SWAP':
            DOGE_last = last
        
        results.append({
            'symbol': symbol,
            'last': last,
            'dim1': dim1, 'dim2': dim2, 'dim3': dim3, 'dim4': dim4, 'dim5': dim5,
            'total': total,
            'rsi15': round(rsi15, 1),
            'funding': round(funding, 6),
            'vol24h': vol24h,
            'margin_per_contract': round(margin_per_contract, 2),
            'feasible': feasible,
            'ctval': ctval,
            'vs_ma20_pct': round((last/ma20 - 1)*100, 2) if ma20 and ma20 > 0 else 0,
            'ma20': ma20,
            'atr14': atr14,
        })
    
    results.sort(key=lambda x: x['total'], reverse=True)
    feasible = [r for r in results if r['feasible']]
    
    print(f"Total coins scored: {len(results)} | Feasible: {len(feasible)}")
    print(f"REGIME: {REGIME} | CROSS: DXY={cross.get('dxy','N/A'):.2f} VIX={cross.get('vix','N/A')} BTC_DOM={cross.get('btc_dominance','N/A'):.1f}")
    print(f"\n{'Rank':<4} {'Symbol':<25} {'Last':>10} {'D1':>3} {'D2':>3} {'D3':>3} {'D4':>3} {'D5':>3} {'Tot':>4} {'RSI15':>6} {'Fundg':>8} {'$/Cont':>7} {'Feas':>5}")
    print("-" * 105)
    for i, r in enumerate(feasible[:25], 1):
        print(f"{i:<4} {r['symbol']:<25} {r['last']:>10.5f} {r['dim1']:>3} {r['dim2']:>3} {r['dim3']:>3} {r['dim4']:>3} {r['dim5']:>3} {r['total']:>4} {r['rsi15']:>6.1f} {r['funding']:>8.4f} {r['margin_per_contract']:>7.2f} {'YES' if r['feasible'] else 'NO':>5}")
    
    # Top non-feasible
    non_feas = [r for r in results if not r['feasible']]
    if non_feas:
        print(f"\n--- Non-feasible (too expensive per contract) ---")
        for i, r in enumerate(non_feas[:5], 1):
            print(f"  {r['symbol']}: last={r['last']} ctVal={r['ctval']} margin/contract=${r['margin_per_contract']:.2f} (budget $101.42)")
    
    # Output JSON
    output = {
        'timestamp': NOW_UTC,
        'regime': REGIME,
        'total_scored': len(results),
        'feasible_count': len(feasible),
        'cross_market': {
            'dxy': cross.get('dxy'),
            'vix': cross.get('vix'),
            'btc_dominance': cross.get('btc_dominance'),
            'regime': cross.get('regime'),
        },
        'doge_position': {
            'sz': '<REDACTED_POSITION_SIZE>', 'avgPx': '<REDACTED_ENTRY_PRICE>',
            'last': '<REDACTED_MARK_PRICE>', 'lev': 3,
            'margin': '<REDACTED_MARGIN>', 'margin_pct': '<REDACTED_MARGIN_PCT>',
            'sl': '<REDACTED_STOP_PRICE>',
        },
        'top20_feasible': feasible[:20],
        'all_feasible': feasible,
    }
    print("\n\n=== JSON_OUTPUT ===")
    print(json.dumps(output, ensure_ascii=False, default=str))

if __name__ == '__main__':
    main()
