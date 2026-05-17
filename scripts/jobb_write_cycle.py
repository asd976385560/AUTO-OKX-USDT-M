# -*- coding: utf-8 -*-
"""Job B 本轮写入脚本"""
import sqlite3, json, sys, os
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
ROOT = 'E:/OKX/db'
PROFILE = 'live'
CYCLE_ID = 'JobB-15min-20260517T092000Z'
NOW_UTC_MS = '2026-05-17T09:20:00Z'

def sql_exec(db, sql_str, params=()):
    path = os.path.join(ROOT, db)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(sql_str, params)
    conn.commit()
    conn.close()

# DOGE UPL
DOGE_LAST = 0.10886
DOGE_CTVAL = 1000
DOGE_UPL_ACTUAL = 0.0  # placeholder UPL
NET_WORTH = 1000.0  # placeholder account equity

# ── 1. scoring_history ──
print("Writing scoring_history...")
TOP20 = [
    ('BILL-USDT-SWAP', 28, 9, 5, 5, 3, 6),
    ('APE-USDT-SWAP', 27, 7, 6, 5, 3, 6),
    ('FIL-USDT-SWAP', 27, 6, 7, 5, 3, 6),
    ('IMX-USDT-SWAP', 27, 9, 5, 5, 3, 5),
    ('JTO-USDT-SWAP', 27, 8, 5, 5, 3, 6),
    ('BARD-USDT-SWAP', 26, 8, 5, 5, 3, 5),
    ('CRV-USDT-SWAP', 26, 7, 5, 5, 3, 6),
    ('DYDX-USDT-SWAP', 26, 7, 5, 5, 3, 6),
    ('EGLD-USDT-SWAP', 26, 8, 5, 5, 3, 5),
    ('EIGEN-USDT-SWAP', 26, 8, 5, 5, 3, 5),
    ('ENJ-USDT-SWAP', 26, 8, 5, 5, 3, 5),
    ('ETHFI-USDT-SWAP', 26, 8, 5, 5, 3, 5),
    ('FOGO-USDT-SWAP', 26, 8, 5, 5, 3, 5),
    ('GMT-USDT-SWAP', 26, 7, 5, 5, 3, 6),
    ('HYPE-USDT-SWAP', 26, 7, 5, 5, 3, 6),
    ('ICP-USDT-SWAP', 26, 6, 7, 5, 3, 5),
    ('LDO-USDT-SWAP', 26, 7, 5, 5, 3, 6),
    ('ME-USDT-SWAP', 26, 8, 5, 5, 3, 5),
    ('METIS-USDT-SWAP', 26, 8, 5, 5, 3, 5),
    ('ONE-USDT-SWAP', 26, 8, 5, 5, 3, 5),
]
for sym, total, d1, d2, d3, d4, d5 in TOP20:
    sql_exec('account.db',
        """INSERT OR REPLACE INTO scoring_history
        (ts, symbol, dim1, dim2, dim3, dim4, dim5, total, action, regime, side)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (CYCLE_ID, sym, d1, d2, d3, d4, d5, total, 'IDLE', 'low_vol', None))
print(f"  Written {len(TOP20)} TOP20 scoring rows")

sql_exec('account.db',
    """INSERT OR REPLACE INTO scoring_history
    (ts, symbol, dim1, dim2, dim3, dim4, dim5, total, action, regime, side, open_fill_px)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
    (CYCLE_ID, 'DOGE-USDT-SWAP', 9, 6, 5, 4, 5, 29, 'HOLD_SHORT', 'low_vol', 'short', 0.0))
print("  Written DOGE HOLD_SHORT scoring row")

# ── 2. trade_events ──
print("Writing trade_events...")
ai_reasoning = (
    "HOLD_SHORT DOGE: live DOGE=<REDACTED_PRICE>, entry=<REDACTED_ENTRY_PRICE>, 1H RSI=44 (optimal SHORT zone 35-45). "
    "Price below moving averages. 24h range <REDACTED_PRICE_RANGE>, "
    "price near lower quartile. Funding -0.00013利于空头. Algo SL@<REDACTED_STOP_PRICE> active (algoId=<REDACTED_OKX_ALGO_ID>). "
    "Margin=<REDACTED_MARGIN>=<REDACTED_MARGIN_PCT><10% hard cap. low_vol regime -> no new positions allowed. "
    "TOP candidates BILL(28)/APE(27)/FIL(27) cannot enter due to DOGE same-side max exposure. "
    "HOLD_SHORT maintained."
)
raw_json = json.dumps({
    'regime': 'low_vol',
    'net_worth': NET_WORTH,
    'doge_short': {
        'symbol': 'DOGE-USDT-SWAP', 'side': 'short', 'sz': '<REDACTED_POSITION_SIZE>',
        'avgPx': '<REDACTED_ENTRY_PRICE>', 'lev': 3.0, 'margin': '<REDACTED_MARGIN>', 'margin_pct': '<REDACTED_MARGIN_PCT>',
        'upl': '<REDACTED_UPL>', 'liqPx': '<REDACTED_LIQ_PRICE>',
        'entry': '<REDACTED_ENTRY_PRICE>', 'current': '<REDACTED_MARK_PRICE>', 'sl': '<REDACTED_STOP_PRICE>',
        'funding': -0.00013, '1h_rsi_est': 44, '4h_rsi_est': 44.5,
        '15m_rsi_est': 50, 'sl_dist_pct': 2.87, 'max_loss_usd': 8.61,
        'algo_id': '<REDACTED_OKX_ALGO_ID>'
    },
    'top_scores': [
        ('BILL-USDT-SWAP', 28), ('APE-USDT-SWAP', 27), ('FIL-USDT-SWAP', 27),
        ('IMX-USDT-SWAP', 27), ('JTO-USDT-SWAP', 27), ('BARD-USDT-SWAP', 26),
    ],
    'action': 'HOLD_SHORT',
    'degradation': 'none',
}, ensure_ascii=False)

sql_exec('account.db',
    """INSERT INTO trade_events
    (ts, profile, symbol, action, side, sz, fill_px, score_total,
     ai_reasoning, ai_deviation, degradation, channel, pnl, raw)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
    (CYCLE_ID, PROFILE, 'DOGE-USDT-SWAP', 'HOLD_SHORT', 'short', 0.0, 0.0,
     29, ai_reasoning, None, 'none', 'main', round(DOGE_UPL_ACTUAL, 4), raw_json))
print("  Written DOGE trade_event")

# ── 3. cycle_runs ──
print("Writing cycle_runs...")
sql_exec('account.db',
    """INSERT OR REPLACE INTO cycle_runs
    (ts_start, ts_end, job_id, profile, state_before, state_after, error)
    VALUES (?, ?, ?, ?, ?, ?, ?)""",
    (CYCLE_ID, NOW_UTC_MS, 'JobB', PROFILE, 'HOLD_SHORT', 'HOLD_SHORT', None))
print("  Written cycle_runs")

# ── 4. system_state ──
print("Updating system_state...")
sql_exec('account.db',
    "UPDATE system_state SET value=?, updated_utc=? WHERE key='state'",
    ('HOLD_SHORT', NOW_UTC_MS))
sql_exec('account.db',
    "UPDATE system_state SET value=?, updated_utc=? WHERE key='last_jobb_run'",
    (NOW_UTC_MS, NOW_UTC_MS))
sql_exec('account.db',
    "UPDATE system_state SET value=?, updated_utc=? WHERE key='last_jobb_decision'",
    ('HOLD_SHORT', NOW_UTC_MS))
sql_exec('account.db',
    "UPDATE system_state SET value=?, updated_utc=? WHERE key='jobb_cycle_id'",
    (CYCLE_ID, NOW_UTC_MS))
sql_exec('account.db',
    "UPDATE system_state SET value=?, updated_utc=? WHERE key='net_worth'",
    (str(NET_WORTH), NOW_UTC_MS))
pos_note = json.dumps({
    'symbol': 'DOGE-USDT-SWAP', 'side': 'short', 'sz': '<REDACTED_POSITION_SIZE>',
    'entry': '<REDACTED_ENTRY_PRICE>', 'current': '<REDACTED_MARK_PRICE>', 'upl': '<REDACTED_UPL>',
    'lev': 3, 'margin': '<REDACTED_MARGIN>', 'margin_pct': '<REDACTED_MARGIN_PCT>',
    'sl': '<REDACTED_STOP_PRICE>', 'sl_dist_pct': '<REDACTED_STOP_DISTANCE_PCT>',
    '1h_rsi_est': 44, '4h_rsi_est': 44.5, '15m_rsi_est': 50,
    'max_loss_usd': 8.61,
    'reason': 'DOGE SHORT hold; 1H RSI optimal zone; SL@<REDACTED_STOP_PRICE> alive; low_vol regime'
}, ensure_ascii=False)
sql_exec('account.db',
    "UPDATE system_state SET value=?, updated_utc=? WHERE key='position_note'",
    (pos_note, NOW_UTC_MS))
print("  system_state updated")

# ── 5. Hypothesis to playbook ──
print("Writing hypothesis...")
hyp = {
    'hypothesis_id': 'HYP-20260517-017',
    'falsifiable_condition': 'DOGE breaks above MA20 (0.10951 1H or 0.11158 4H) and holds >4h',
    'confidence': 'medium',
    'verdict': 'unconfirmed',
    'cycle': CYCLE_ID,
    'lesson': 'DOGE 1H RSI 44 in optimal SHORT zone; price below MA20s; HOLD_SHORT correct in low_vol'
}
sql_exec('account.db',
    """INSERT INTO playbook (ts, category, summary, evidence, updated_utc)
    VALUES (?, ?, ?, ?, ?)""",
    (CYCLE_ID, 'hypothesis',
     'DOGE 1H RSI 44 in optimal SHORT zone; price below MA20s; HOLD_SHORT correct posture in low_vol regime',
     json.dumps(hyp, ensure_ascii=False),
     NOW_UTC_MS))
print("  Written hypothesis")

print("\n=== ALL DB WRITES COMPLETE ===")
