import sqlite3
import json
import sys
from datetime import datetime, timezone

ROOT = r'E:\OKX\db'
NOW_UTC = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
CYCLE_ID = f"JobB-15min-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

net_worth = 1000.0  # placeholder account equity
doge_side = 'short'
doge_sz = 1.0  # placeholder position size
doge_entry = 0.1  # placeholder entry price
doge_current = 0.1  # placeholder mark price
doge_upl = 0.0  # placeholder UPL
doge_lev = 3.0
doge_margin = 0.0
doge_margin_pct = 9.08
doge_liqpx = 0.0
doge_sl = 0.11
doge_sl_dist = 0.0
doge_1h_rsi = 35.80
doge_4h_rsi = 40.20
doge_15m_rsi = 19.51
action = 'HOLD_SHORT'
degradation = 'none'
regime = 'low_vol'

d1_tech, d2_struct, d3_news, d4_cross, d5_fund = 6, 5, 5, 4, 6
doge_total = d1_tech + d2_struct + d3_news + d4_cross + d5_fund

ai_reasoning = f"""HOLD_SHORT DOGE: live DOGE=<REDACTED_PRICE>, entry=<REDACTED_ENTRY_PRICE>, UPL=<REDACTED_UPL>.
1H RSI=35.80(optimal SHORT zone 35-45). 4H RSI=40.20(valid short zone).
15m RSI=19.51(extremely oversold) - bounce risk but thesis intact.
Price below 1H MA20(0.10983) and 4H MA20(0.11133). Lower highs intact.
24h range <REDACTED_PRICE_RANGE>, DOGE near lower quartile.
BTC=77,780(-1.41%) < 78K, bearish signal for altcoins.
Funding=-0.0043%(利于空头). Premium=-0.028%(利于空头).
Algo SL@<REDACTED_STOP_PRICE> alive. Margin=<REDACTED_MARGIN> < 10% hard cap.
Leverage 3x<10x. Data fresh~4min. low_vol regime.
DOGE at max safe exposure (short side, ~9% margin). Cannot add SHORT.
HOLD_SHORT maintained."""

hypotheses = [
    ('HYP-20260517-041', 'DOGE 15m RSI 19-21极度超卖信号，将在未来1-2h内反弹至0.1086-0.1091区域', 'medium', 'DOGE未能反弹至0.1086以上或继续下跌至新低'),
    ('HYP-20260517-042', 'DOGE 4H RSI在40-45区间有效 SHORT zone，价格将受阻于4H MA20(0.11133)', 'medium-high', 'DOGE突破并站稳4H MA20(0.11133)上方'),
    ('HYP-20260517-043', 'BTC跌破78K是结构性熊市信号，DOGE将跟随走弱并测试0.105以下', 'medium', 'BTC反弹至78.5K以上并企稳'),
    ('HYP-20260517-044', 'low_vol regime + DXY>118 + VIX<20组合确认看空方向，应保持空头暴露', 'high', 'regime切换至neutral或DXY跌破117'),
    ('HYP-20260517-045', 'SIGN(-0.32%)/WLD(-0.04%)负资金费率是强做空信号，应优先关注做空机会', 'medium', '资金费率转正或中性'),
]

# === WRITE account.db ===
conn = sqlite3.connect(f'{ROOT}\\account.db')

conn.execute("""
    INSERT OR REPLACE INTO cycle_runs (ts_start, ts_end, job_id, profile, state_before, state_after, error)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", (CYCLE_ID, NOW_UTC, 'JobB', 'live', 'HOLD_SHORT', 'HOLD_SHORT', None))

raw_json = json.dumps({
    'regime': regime, 'net_worth': net_worth,
    'doge_short': {
        'symbol': 'DOGE-USDT-SWAP', 'side': doge_side, 'sz': doge_sz,
        'avgPx': doge_entry, 'lev': doge_lev, 'margin': doge_margin,
        'margin_pct': doge_margin_pct, 'upl': doge_upl, 'liqPx': doge_liqpx,
        'entry': doge_entry, 'current': doge_current, 'sl': doge_sl,
        'funding': -0.000043, '1h_rsi_est': doge_1h_rsi,
        '4h_rsi_est': doge_4h_rsi, '15m_rsi_est': doge_15m_rsi,
        'sl_dist_pct': doge_sl_dist, 'max_loss_usd': 8.2,
        'algo_id': '<REDACTED_OKX_ALGO_ID>'
    },
    'action': action, 'degradation': degradation
})

conn.execute("""
    INSERT INTO trade_events (ts, profile, symbol, action, side, sz, fill_px, score_total, ai_reasoning, ai_deviation, degradation, channel, pnl, raw)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (CYCLE_ID, 'live', 'DOGE-USDT-SWAP', action, doge_side, doge_sz, doge_entry,
      doge_total, ai_reasoning, None, degradation, 'main', doge_upl, raw_json))

conn.execute("""
    INSERT INTO scoring_history (ts, symbol, dim1, dim2, dim3, dim4, dim5, total, action, ai_reasoning, regime, side, open_fill_px)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (CYCLE_ID, 'DOGE-USDT-SWAP', d1_tech, d2_struct, d3_news, d4_cross, d5_fund,
      doge_total, action, ai_reasoning, regime, doge_side, doge_entry))

position_note = json.dumps({
    'symbol': 'DOGE-USDT-SWAP', 'side': doge_side, 'sz': doge_sz,
    'entry': doge_entry, 'current': doge_current, 'upl': doge_upl,
    'lev': doge_lev, 'margin': doge_margin, 'margin_pct': doge_margin_pct,
    'sl': doge_sl, 'sl_dist_pct': doge_sl_dist,
    '1h_rsi_est': doge_1h_rsi, '4h_rsi_est': doge_4h_rsi, '15m_rsi_est': doge_15m_rsi,
    'max_loss_usd': 8.2,
    'reason': 'DOGE SHORT hold; 15m RSI extremely oversold bounce risk; SL@<REDACTED_STOP_PRICE> alive; low_vol regime'
})

for key, value in [
    ('state', 'HOLD_SHORT'),
    ('last_jobb_run', NOW_UTC),
    ('last_jobb_regime', regime),
    ('net_worth', str(net_worth)),
    ('position_note', position_note),
]:
    conn.execute("""
        INSERT OR REPLACE INTO system_state (key, value, updated_utc)
        VALUES (?, ?, ?)
    """, (key, value, NOW_UTC))

conn.commit()
conn.close()

# === WRITE lessons.db ===
conn2 = sqlite3.connect(f'{ROOT}\\lessons.db')
for h in hypotheses:
    evidence = json.dumps({'hypothesis_id': h[0], 'cycle': CYCLE_ID, 'status': 'unconfirmed'})
    conn2.execute("""
        INSERT INTO param_suggestions (suggestion, current_value, suggested_value, evidence, status, created_utc, decided_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (h[1], h[2], h[3], evidence, 'pending', NOW_UTC, None))
conn2.commit()
conn2.close()

print(f"DB write complete. Cycle: {CYCLE_ID}")
print(f"Action: {action} DOGE | Net worth: <REDACTED_ACCOUNT_EQUITY> | UPL: <REDACTED_UPL>")
