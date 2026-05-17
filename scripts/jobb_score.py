#!/usr/bin/env python3
"""JobB full coin scoring - compute 5-dim scores for all coins"""
import sys
import os
import json
import sqlite3
from pathlib import Path

DB_DIR = Path(r'E:\OKX\db')
EQUITY = 1000.0  # placeholder account equity
NET_WORTH = EQUITY
LEVERAGE = 10  # Max leverage for scoring
REGIME = 'low_vol'

# ctVal map for common coins (from OKX instruments)
CTVLS = {
    'BTC-USDT-SWAP': 0.01, 'ETH-USDT-SWAP': 0.01, 'SOL-USDT-SWAP': 0.1,
    'DOGE-USDT-SWAP': 1000, 'SHIB-USDT-SWAP': 1000000, 'NOT-USDT-SWAP': 10000,
    'PEPE-USDT-SWAP': 1000000, 'FLOKI-USDT-SWAP': 100000, 'WIF-USDT-SWAP': 10,
    'BONK-USDT-SWAP': 1000, 'NEIRO-USDT-SWAP': 1000000,
}

def get_ctval(symbol):
    return CTVLS.get(symbol, 1)  # default ctVal=1

def query_db(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def main():
    # Get all tick snapshots
    rows = query_db(DB_DIR / 'market.db',
        """SELECT t.* FROM tick_snapshots t
           WHERE t.ts = (SELECT MAX(ts) FROM tick_snapshots)""")
    ticks = {r['symbol']: r for r in rows}
    
    # Get derivatives (funding rates)
    rows = query_db(DB_DIR / 'market.db',
        """SELECT d.* FROM derivatives d
           WHERE d.ts = (SELECT MAX(ts) FROM derivatives)""")
    derivs = {r['symbol']: r for r in rows}
    
    # Get kline data (15m RSI/MA)
    rows = query_db(DB_DIR / 'market.db',
        """SELECT k.* FROM kline_cache k
           WHERE k.ts = (SELECT MAX(ts) FROM kline_cache WHERE symbol=k.symbol AND tf='15m')
           AND k.tf = '15m'""")
    klines = {r['symbol']: r for r in rows}
    
    # Get cross_market
    rows = query_db(DB_DIR / 'market.db', "SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1")
    cross = rows[0] if rows else {}
    
    # Score all coins
    results = []
    
    for symbol, tick in ticks.items():
        last = tick.get('last') or 0
        if not last or last <= 0:
            continue
        
        # Skip non-crypto tokens
        skip_symbols = ['TSLA-USDT-SWAP', 'AAPL-USDT-SWAP', 'NVDA-USDT-SWAP', 'GOOGL-USDT-SWAP',
                        'MSFT-USDT-SWAP', 'AMZN-USDT-SWAP', 'META-USDT-SWAP', 'NFLX-USDT-SWAP',
                        'QQQ-USDT-SWAP', 'SPY-USDT-SWAP', 'IWM-USDT-SWAP', 'EWJ-USDT-SWAP',
                        'EWY-USDT-SWAP', 'COST-USDT-SWAP', 'LLY-USDT-SWAP', 'AVGO-USDT-SWAP',
                        'MSTR-USDT-SWAP', 'PLTR-USDT-SWAP', 'ORCL-USDT-SWAP', 'INTC-USDT-SWAP',
                        'AMD-USDT-SWAP', 'MU-USDT-SWAP', 'TSM-USDT-SWAP', 'COIN-USDT-SWAP',
                        'ARKM-USDT-SWAP', 'HOOD-USDT-SWAP', 'HIMS-USDT-SWAP', 'ARM-USDT-SWAP',
                        'XAG-USDT-SWAP', 'XAU-USDT-SWAP', 'XCU-USDT-SWAP', 'XLE-USDT-SWAP',
                        'XPT-USDT-SWAP', 'XPD-USDT-SWAP', 'CRCL-USDT-SWAP', 'CRWV-USDT-SWAP',
                        'CL-USDT-SWAP', 'NG-USDT-SWAP', 'GAS-USDT-SWAP', 'SPACEX-USDT-SWAP']
        if symbol in skip_symbols:
            continue
        
        # Skip stablecoins
        if 'USDC' in symbol or 'USDT' not in symbol:
            continue
        if symbol in ['USDC-USDT-SWAP', 'STABLE-USDT-SWAP', 'USELESS-USDT-SWAP']:
            continue
            
        deriv = derivs.get(symbol, {})
        kline = klines.get(symbol, {})
        
        funding = deriv.get('funding_rate') or 0
        vol24h = tick.get('vol24h') or 0
        rsi15 = kline.get('rsi14') or 50
        ma5 = kline.get('ma5') or last
        ma20 = kline.get('ma20') or last
        
        ctval = get_ctval(symbol)
        
        # 1. Technical (0-10)
        dim1 = 5  # neutral
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
        chg24 = ((last - (tick.get('open') or last)) / (tick.get('open') or last) * 100) if tick.get('open') else 0
        if chg24 < -3:
            dim2 += 1
        if vol24h > 5000000:
            dim2 += 1
        dim2 = max(0, min(10, dim2))
        
        # 3. News/Events (0-10) - default neutral
        dim3 = 5
        
        # 4. Cross-Market (0-10)
        dim4 = 5
        dxy = cross.get('dxy') or 118
        if dxy > 118:
            dim4 += 2  # DXY strong = bearish for crypto
        btc_dom = cross.get('btc_dominance') or 58
        if btc_dom > 58:
            dim4 += 1  # BTC dominance up = alt pain
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
        
        # ctVal check
        margin_per_contract = last * ctval / LEVERAGE
        budget_10pct = NET_WORTH * 0.10
        if margin_per_contract > budget_10pct:
            feasible = False
        else:
            feasible = True
        
        results.append({
            'symbol': symbol,
            'last': last,
            'dim1': dim1, 'dim2': dim2, 'dim3': dim3, 'dim4': dim4, 'dim5': dim5,
            'total': total,
            'rsi15': rsi15,
            'funding': funding,
            'vol24h': vol24h,
            'margin_per_contract': round(margin_per_contract, 2),
            'feasible': feasible,
            'ctval': ctval,
            'vs_ma20_pct': round((last/ma20 - 1)*100, 2) if ma20 else 0,
        })
    
    # Sort by total score
    results.sort(key=lambda x: x['total'], reverse=True)
    
    print(f"Total coins scored: {len(results)}")
    print(f"\nTOP 25 COINS:")
    print(f"{'Rank':<4} {'Symbol':<25} {'Last':>10} {'D1':>3} {'D2':>3} {'D3':>3} {'D4':>3} {'D5':>3} {'Total':>5} {'RSI15':>6} {'Fundg':>8} {'$/Cont':>7} {'Feas':>5} {'vsMA20':>7}")
    print("-" * 115)
    for i, r in enumerate(results[:25], 1):
        print(f"{i:<4} {r['symbol']:<25} {r['last']:>10.5f} {r['dim1']:>3} {r['dim2']:>3} {r['dim3']:>3} {r['dim4']:>3} {r['dim5']:>3} {r['total']:>5} {r['rsi15']:>6.1f} {r['funding']:>8.4f} {r['margin_per_contract']:>7.2f} {'YES' if r['feasible'] else 'NO':>5} {r['vs_ma20_pct']:>7.1f}%")
    
    # Feasible coins count
    feasible = [r for r in results if r['feasible']]
    print(f"\nFeasible coins (ctVal check): {len(feasible)}")
    
    # Output JSON for further processing
    print("\n\n=== JSON_OUTPUT ===")
    print(json.dumps({
        'top20': results[:20],
        'feasible_count': len(feasible),
        'total_count': len(results),
        'regime': REGIME,
        'cross_market': dict(cross) if cross else {},
        'doge_position': {
            'sz': '<REDACTED_POSITION_SIZE>', 'avgPx': '<REDACTED_ENTRY_PRICE>',
            'last': '<REDACTED_MARK_PRICE>', 'upl': '<REDACTED_UPL>',
            'lev': 3, 'margin': '<REDACTED_MARGIN>',
            'margin_pct': '<REDACTED_MARGIN_PCT>',
            'sl': '<REDACTED_STOP_PRICE>', 'sl_dist_pct': '<REDACTED_STOP_DISTANCE_PCT>',
        }
    }, ensure_ascii=False, default=str))

if __name__ == '__main__':
    main()
