# -*- coding: utf-8 -*-
"""Job B 全币种扫描 + 五维评分"""
import sqlite3, json, sys, os, math
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
ROOT = 'E:/OKX/db'
PROFILE = 'live'
NET_WORTH = 1000.0  # placeholder account equity

def query(db, sql, params=()):
    path = os.path.join(ROOT, db)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ─────────────────────────────────────────
# 1. 从 instruments 获取 ctVal（真实合约面值）
# ─────────────────────────────────────────
print("=== LOADING INSTRUMENTS ===")
inst_rows = query('market.db',
    "SELECT symbol, ts FROM tick_snapshots WHERE ts=(SELECT MAX(ts) FROM tick_snapshots)")
symbols = [r['symbol'] for r in inst_rows]
print(f"Total symbols in tick_snapshots: {len(symbols)}")

# 读取 OKX instruments ctVal（从本地缓存或网络）
# instruments ctVal 缓存表
ctval_map = {}
try:
    inst_data = query('market.db',
        "SELECT instId, ctVal, lotSz FROM instruments_cache LIMIT 9999")
    for r in inst_data:
        sym = r['instId']
        ctval_map[sym] = {'ctVal': r['ctVal'], 'lotSz': r['lotSz']}
    print(f"Loaded {len(ctval_map)} instruments from cache")
except Exception as e:
    print(f"No instruments_cache: {e}")

# 如果缓存为空，从已知数据构建（常见币种 ctVal 硬编码）
# ctVal 数据是 static 的，可以预填充
FALLBACK_CTVAL = {
    'DOGE-USDT-SWAP': 1000,
    'BTC-USDT-SWAP': 0.01,
    'ETH-USDT-SWAP': 0.01,
    'SOL-USDT-SWAP': 0.1,
    'XRP-USDT-SWAP': 10,
    'BNB-USDT-SWAP': 0.1,
    'ADA-USDT-SWAP': 100,
    'AVAX-USDT-SWAP': 1,
    'DOT-USDT-SWAP': 1,
    'LINK-USDT-SWAP': 1,
    'MATIC-USDT-SWAP': 10,
    'UNI-USDT-SWAP': 1,
    'ATOM-USDT-SWAP': 1,
    'LTC-USDT-SWAP': 0.1,
    'FIL-USDT-SWAP': 1,
    'ICP-USDT-SWAP': 0.1,
    'APT-USDT-SWAP': 0.1,
    'ARBU-USDT-SWAP': 0.1,
    'WLD-USDT-SWAP': 10,
    'OP-USDT-SWAP': 1,
    'INJ-USDT-SWAP': 0.1,
    'SUI-USDT-SWAP': 1,
    'TIA-USDT-SWAP': 0.1,
    'SEI-USDT-SWAP': 1,
    'NOT-USDT-SWAP': 10000,
    'ACT-USDT-SWAP': 10000,
    'MERL-USDT-SWAP': 100,
    'CORE-USDT-SWAP': 10,
    'BERA-USDT-SWAP': 10,
    'W-USDT-SWAP': 10000,
    'GALA-USDT-SWAP': 10000,
    'STRK-USDT-SWAP': 10,
    'CHZ-USDT-SWAP': 100,
    'EDEN-USDT-SWAP': 100,
    'GMT-USDT-SWAP': 100,
    'GPS-USDT-SWAP': 100,
    'PI-USDT-SWAP': 10,
    'LAYER-USDT-SWAP': 10,
    'LUNA-USDT-SWAP': 10,
    'YGG-USDT-SWAP': 10,
    'ZBT-USDT-SWAP': 10,
    'HYPE-USDT-SWAP': 0.01,
    'RIVER-USDT-SWAP': 0.1,
    'SAHARA-USDT-SWAP': 100,
    'PROS-USDT-SWAP': 0.1,
    'BABY-USDT-SWAP': 1000,
    'RECALL-USDT-SWAP': 100,
    'ZEC-USDT-SWAP': 0.01,
    'PIPPIN-USDT-SWAP': 100,
    'WLFI-USDT-SWAP': 100,
    'BASED-USDT-SWAP': 100,
    'AXS-USDT-SWAP': 0.1,
    'CRV-USDT-SWAP': 100,
    'YB-USDT-SWAP': 100,
    'WIF-USDT-SWAP': 10,
    'COAI-USDT-SWAP': 1,
    'AEVO-USDT-SWAP': 10,
    'ORDI-USDT-SWAP': 0.01,
    'HMSTR-USDT-SWAP': 10000,
    'AIXBT-USDT-SWAP': 1000,
    'XAG-USDT-SWAP': 1,
    'TON-USDT-SWAP': 1,
    'FARTCOIN-USDT-SWAP': 10,
    'IP-USDT-SWAP': 1,
    'DYDX-USDT-SWAP': 10,
    'APE-USDT-SWAP': 10,
    'TRUMP-USDT-SWAP': 10,
    'BIO-USDT-SWAP': 10000,
    'MON-USDT-SWAP': 0.01,
    'VANA-USDT-SWAP': 10,
    'SSV-USDT-SWAP': 0.01,
    'SOPH-USDT-SWAP': 0.1,
    'RVN-USDT-SWAP': 100,
    'PENDLE-USDT-SWAP': 0.1,
    'METIS-USDT-SWAP': 0.01,
    'MASK-USDT-SWAP': 1,
    'IOTA-USDT-SWAP': 10,
    'ICX-USDT-SWAP': 10,
    'CHIP-USDT-SWAP': 100,
    'BLUR-USDT-SWAP': 100,
    'API3-USDT-SWAP': 1,
    'FOGO-USDT-SWAP': 100,
    'ENJ-USDT-SWAP': 100,
    'SNDK-USDT-SWAP': 0.1,
    'LAB-USDT-SWAP': 0.1,
    'LLY-USDT-SWAP': 1,
    'MU-USDT-SWAP': 0.1,
    'BEAT-USDT-SWAP': 10,
    'WDC-USDT-SWAP': 100,
    'TRUTH-USDT-SWAP': 100,
    'LIGHT-USDT-SWAP': 100,
    'DRAM-USDT-SWAP': 100,
    'SNDK-USDT-SWAP': 1,
    'PIVERSE-USDT-SWAP': 100,
    'BAT-USDT-SWAP': 100,
    'CBRS-USDT-SWAP': 100,
    'LLY-USDT-SWAP': 10,
    'SNDK-USDT-SWAP': 10,
    'LITE-USDT-SWAP': 0.1,
    'AIAGENT-USDT-SWAP': 100,
    'AIXBT-USDT-SWAP': 100,
}
# 从 tick_snapshots 获取价格
ticks = query('market.db',
    "SELECT * FROM tick_snapshots WHERE ts=(SELECT MAX(ts) FROM tick_snapshots)")
tick_map = {r['symbol']: r for r in ticks}

# funding rates
derivs = query('market.db',
    "SELECT * FROM derivatives WHERE ts=(SELECT MAX(ts) FROM derivatives)")
deriv_map = {r['symbol']: r for r in derivs}

# cross market
cm = query('market.db', "SELECT * FROM cross_market ORDER BY ts DESC LIMIT 1")
regime = cm[0]['regime'] if cm else 'unknown'
dxy = cm[0]['dxy'] if cm else 118.0
vix = cm[0]['vix'] if cm else 17.0

# 1H kline for BTC dominance altcoins
# 从 kline_cache 读取 BTC 1H
btc_ticks = query('market.db',
    "SELECT * FROM tick_snapshots WHERE ts=(SELECT MAX(ts) FROM tick_snapshots) AND symbol='BTC-USDT-SWAP'")
btc_last = btc_ticks[0]['last'] if btc_ticks else None

# 新闻情绪
news_items = query('news.db', "SELECT * FROM news_items ORDER BY ts DESC LIMIT 50")
news_map = {}
for n in news_items:
    sym = n.get('symbol')
    if sym:
        if sym not in news_map:
            news_map[sym] = []
        news_map[sym].append(n)

# 持仓
positions = query('account.db',
    "SELECT * FROM position_snapshots WHERE profile='live' AND ts=(SELECT MAX(ts) FROM position_snapshots WHERE profile='live')")
pos_map = {}
for p in positions:
    pos_map[p['symbol']] = p

# ─────────────────────────────────────────
# 2. ctVal 过滤 + 五维评分
# ─────────────────────────────────────────
MAX_MARGIN_PCT = 0.10  # 10% 硬上限
LEVERAGE = 10  # 标准杠杆用于计算
MARGIN_BUDGET = NET_WORTH * MAX_MARGIN_PCT  # $101.6

scored = []

for sym, tick in tick_map.items():
    last = tick.get('last') or 0
    if not last or last <= 0:
        continue

    # ctVal
    ctVal = ctval_map.get(sym, {}).get('ctVal') or FALLBACK_CTVAL.get(sym) or 1
    if isinstance(ctVal, str) or ctVal == 0:
        ctVal = 1

    # 每张保证金（10x杠杆标准）
    margin_per_contract = last * ctVal / LEVERAGE

    # 排除：1张保证金 > 10%净值
    if margin_per_contract > MARGIN_BUDGET:
        continue

    # 排除：已有同向持仓达上限
    pos = pos_map.get(sym)
    existing_margin = 0
    if pos and pos.get('sz', 0) > 0:
        lev_existing = pos.get('lev', 3)
        existing_margin = pos['sz'] * last * ctVal / lev_existing
        if existing_margin >= MARGIN_BUDGET:
            continue  # 已有最大暴露

    # 可行性计算：最大张数
    max_contracts = int(MARGIN_BUDGET / margin_per_contract)

    # ── 五维评分 ──
    # D1: 技术面 (0-10)
    dim1 = 5

    # D2: 结构量价 (0-10)
    dim2 = 5

    # D3: 新闻事件 (0-10)
    dim3 = 5

    # D4: 跨市场联动 (0-10)
    dim4 = 5

    # D5: 资金与情绪 (0-10)
    dim5 = 5

    # ── 技术面细化 ──
    # 用 kline_cache 读该币 1H
    klines_1h = query('market.db',
        "SELECT * FROM kline_cache WHERE symbol=? AND tf='1H' ORDER BY ts DESC LIMIT 20",
        (sym,))
    if klines_1h and len(klines_1h) >= 5:
        closes = [k['c'] for k in klines_1h]
        highs = [k['h'] for k in klines_1h]
        lows = [k['l'] for k in klines_1h]

        # MA20
        ma20 = sum(closes[:20]) / min(len(closes), 20) if len(closes) >= 20 else sum(closes)/len(closes)
        ma5 = sum(closes[:5]) / 5

        # RSI(14)
        gains = []
        losses = []
        for i in range(1, min(15, len(closes))):
            diff = closes[i-1] - closes[i]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))
        avg_gain = sum(gains) / min(14, len(gains)) if gains else 0.001
        avg_loss = sum(losses) / min(14, len(losses)) if losses else 0.001
        rs = avg_gain / avg_loss if avg_loss > 0 else 999
        rsi = 100 - (100 / (1 + rs))

        # 趋势：价格 vs MA20
        price_vs_ma20 = last / ma20 - 1  # pct diff

        if price_vs_ma20 < -0.02:  # 显著低于MA20
            dim1 += 2
        elif price_vs_ma20 < -0.005:
            dim1 += 1

        # RSI 高位做空
        if 35 <= rsi <= 45:
            dim1 += 1  # 最佳做空区间
        elif rsi > 65:
            dim1 += 2  # 超买
        elif rsi < 35:
            dim1 -= 1

        # 1H 下跌趋势
        if ma5 < ma20:
            dim1 += 1
    else:
        rsi = 50
        ma20 = last

    # ── 结构量价细化 ──
    vol24h = tick.get('vol24h', 0) or 0
    vol_rank = min(vol24h / 100000000, 10)  # 归一化
    dim2 += min(int(vol_rank / 2), 2)  # 成交量权重

    # 流动性：小ctVal高价格 = 差
    if ctVal > 1000:
        dim2 -= 1  # 大ctVal意味着最小每张保证金高

    # ── 新闻事件 ──
    sym_news = news_map.get(sym, [])
    if sym_news:
        latest = sym_news[0]
        sent = latest.get('sentiment', 0) or 0
        level = latest.get('level', 'C')
        if level == 'A':
            dim3 += 3 if sent > 0 else -3
        elif level == 'B':
            dim3 += 2 if sent > 0 else -2
        else:
            dim3 += 1 if sent > 0 else -1

    # ── 跨市场联动 ──
    # BTC 相关性：高DXY做空风险资产
    if dxy > 115:
        dim4 -= 1  # 强势美元对风险资产不利
    if vix > 20:
        dim4 -= 1  # 高VIX增加风险
    if regime == 'low_vol':
        dim4 -= 1  # 低波动率限制趋势交易

    # ── 资金与情绪 ──
    deriv = deriv_map.get(sym, {})
    fr = deriv.get('funding_rate', 0) or 0
    if fr < -0.0001:  # 负资金费率利于空头
        dim5 += 1
    elif fr > 0.0001:
        dim5 -= 1

    # 扣分：市值太小、流动性差
    if vol24h < 10000000:
        dim5 -= 1

    # Clamp
    dim1 = max(0, min(10, dim1))
    dim2 = max(0, min(10, dim2))
    dim3 = max(0, min(10, dim3))
    dim4 = max(0, min(10, dim4))
    dim5 = max(0, min(10, dim5))

    total = dim1 + dim2 + dim3 + dim4 + dim5
    margin_1合约 = margin_per_contract

    scored.append({
        'symbol': sym,
        'last': last,
        'ctVal': ctVal,
        'margin_per_contract': round(margin_per_contract, 4),
        'max_contracts': max_contracts,
        'vol24h': vol24h,
        'funding_rate': fr,
        'rsi_1h': round(rsi, 1),
        'ma20_1h': round(ma20, 6) if 'ma20' in dir() else None,
        'dim1': dim1, 'dim2': dim2, 'dim3': dim3, 'dim4': dim4, 'dim5': dim5,
        'total': total,
        'margin_budget': MARGIN_BUDGET,
    })

# 排序
scored.sort(key=lambda x: x['total'], reverse=True)

print(f"\n=== FEASIBLE COINS: {len(scored)} ===")
print(f"Regime: {regime}, DXY: {dxy}, VIX: {vix}, Net worth: ${NET_WORTH}")
print(f"Margin budget (10%): ${MARGIN_BUDGET:.2f}")

print("\n=== TOP 20 COINS BY SCORE ===")
for i, c in enumerate(scored[:20]):
    pos_info = pos_map.get(c['symbol'])
    pos_side = pos_info['side'] if pos_info else 'none'
    pos_sz = pos_info['sz'] if pos_info else 0
    print(f"{i+1:2d}. {c['symbol']:25s} score={c['total']:2d} "
          f"(T{c['dim1']}/S{c['dim2']}/N{c['dim3']}/M{c['dim4']}/F{c['dim5']}) "
          f"price={c['last']:.6f} ctVal={c['ctVal']} "
          f"$/contract=${c['margin_per_contract']:.2f} "
          f"max@10x={c['max_contracts']} "
          f"funding={c['funding_rate']:.5f} "
          f"RSI={c['rsi_1h']} "
          f"pos={pos_side}:{pos_sz}")

# 深度分析 TOP 3
print("\n=== DEEP ANALYSIS: TOP 3 ===")
for i, c in enumerate(scored[:3]):
    print(f"\n--- #{i+1}: {c['symbol']} (score={c['total']}) ---")
    print(f"  Price: {c['last']}")
    print(f"  ctVal: {c['ctVal']}")
    print(f"  Margin/contract @10x: ${c['margin_per_contract']:.2f}")
    print(f"  Max contracts: {c['max_contracts']}")
    print(f"  1H RSI: {c['rsi_1h']}")
    print(f"  Funding: {c['funding_rate']:.5f}")
    print(f"  24h Vol: {c['vol24h']:,.0f}")
    klines = query('market.db',
        "SELECT * FROM kline_cache WHERE symbol=? AND tf='1H' ORDER BY ts DESC LIMIT 5",
        (c['symbol'],))
    for k in klines:
        print(f"  1H: O={k['o']} H={k['h']} L={k['l']} C={k['c']} V={k['v']:.0f} RSI={k.get('rsi14','?')} MA20={k.get('ma20','?')}")

# DOGE position
print("\n=== DOGE POSITION CHECK ===")
doge_pos = pos_map.get('DOGE-USDT-SWAP')
doge_tick = tick_map.get('DOGE-USDT-SWAP')
doge_ctVal = FALLBACK_CTVAL.get('DOGE-USDT-SWAP', 1000)
doge_margin = (doge_pos['sz'] * doge_tick['last'] * doge_ctVal / doge_pos['lev']) if doge_pos and doge_tick else 0
print(f"DOGE position: {doge_pos}")
print(f"DOGE price: {doge_tick['last'] if doge_tick else 'N/A'}")
print(f"DOGE ctVal: {doge_ctVal}")
print(f"DOGE margin: ${doge_margin:.2f} ({doge_margin/NET_WORTH*100:.2f}% of net worth)")
print(f"DOGE max safe size: {int(NET_WORTH * MAX_MARGIN_PCT / (doge_tick['last'] * doge_ctVal / 3)) if doge_tick else 'N/A'} @3x")
