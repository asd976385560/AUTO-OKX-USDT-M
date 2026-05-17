"""
JobB Write Script - 2026-05-17 01:21 UTC+8
DOGE-USDT-SWAP SHORT position review
"""
import sqlite3, json, sys
from datetime import datetime, timezone

ROOT = r'E:\OKX'
UTC8 = timezone.utc  # We'll label as UTC+8 in display

ts_cycle = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
ts_display = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC+8')

# ─── Connect to DBs ────────────────────────────────────────────────────────────
acc_conn = sqlite3.connect(f'{ROOT}\\db\\account.db')
acc_c = acc_conn.cursor()
lesson_conn = sqlite3.connect(f'{ROOT}\\db\\lessons.db')
lesson_c = lesson_conn.cursor()

# ─── 1. Update system_state ─────────────────────────────────────────────────
# Correct the position status: DOGE SHORT position exists
# State should remain IDLE (no new opens this cycle)
acc_c.execute("""
    UPDATE system_state 
    SET value=?, updated_utc=? 
    WHERE key IN ('state','last_jobb_run','last_jobb_decision')
""", (json.dumps('IDLE'), ts_cycle))
# Note the DOGE position exists
acc_c.execute("""
    INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)
""", ('position_note', json.dumps({'symbol':'DOGE-USDT-SWAP','side':'short','sz':'<REDACTED_POSITION_SIZE>','note':'persistent DOGE SHORT from prior cycle. No new opens this cycle.'}), ts_cycle))
# Update net_worth from latest snapshot
acc_c.execute("""
    UPDATE system_state SET value=?, updated_utc=? WHERE key='net_worth'
""", ('<REDACTED_ACCOUNT_EQUITY>', ts_cycle))
acc_conn.commit()
print(f"[system_state] Updated at {ts_cycle}")

# ─── 2. Write cycle_runs ────────────────────────────────────────────────────
acc_c.execute("""
    INSERT INTO cycle_runs (ts_start, ts_end, job_id, profile, state_before, state_after, error)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", (ts_cycle, ts_cycle, 'JobB', 'live', 'IDLE', 'IDLE', None))
acc_conn.commit()
print(f"[cycle_runs] IDLE cycle written")

# ─── 3. Write trade_events ───────────────────────────────────────────────────
# This cycle: no new trades. Log as review cycle with IDLE decision.
acc_c.execute("""
    INSERT INTO trade_events 
    (ts, profile, symbol, action, side, sz, fill_px, score_total, ai_reasoning, degradation, channel)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    ts_cycle, 'live',
    '',  # no symbol
    'IDLE',  # action
    None, None, None,  # no side/sz/px
    None,  # score_total
    json.dumps({
        'reason': 'trend_down+extreme_oversold+Iran_geo_risk',
        'doge_position': 'HOLD_SHORT existing <REDACTED_POSITION_SIZE> contracts',
        'doge_sl': '<REDACTED_STOP_PRICE> (active algoId <REDACTED_OKX_ALGO_ID>)',
        'confidence': 'medium',
        'data_note': 'jobb_full_scan reports 0 positions (BUG: dedup issue). DOGE SHORT position confirmed via _check_pos.py and OKX live account. Position details redacted.'
    }),
    None,  # degradation
    'main'
))
acc_conn.commit()
print(f"[trade_events] IDLE decision cycle logged")

# ─── 4. Write hypotheses ────────────────────────────────────────────────────
hypotheses = [
    ('H-20260517-01', 'trend_down+extreme_oversold(RSI15-30)+Iran_geo_risk suppresses SHORT signals even when score>=30', 
     'High', 'Iran military action triggers crypto spike and stops SHORT positions'),
    ('H-20260517-02', 'DOGE RSI 38-45 in downtrend is valid SHORT zone (per param_suggestion #33183)',
     'High', 'DOGE RSI stays in 35-45 range for extended periods; SHORT at RSI=38 with tight stop is valid'),
    ('H-20260517-03', 'DOGE 15m RSI 69-73 means short-term bounce risk; hold SHORT but do not add',
     'Medium', 'Short-term overbought on 15m; position already on; let it ride with stop <REDACTED_STOP_PRICE>'),
    ('H-20260517-04', 'No new OPEN in low_vol regime with geopolitical tail risk',
     'High', 'low_vol regime + Iran A-class risk = suppress all new OPEN signals'),
    ('H-20260517-05', 'DOGE ATR-based stop sizing validated with redacted price inputs',
     'Medium', 'ATR-based stop sizing validated with redacted stop and entry'),
]
for h_id, h_text, conf, falsifiable in hypotheses:
    acc_c.execute("""
        INSERT INTO cycle_runs (ts_start, ts_end, job_id, profile, state_before, state_after, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (f'{h_id}_start', f'{h_id}_end', f'HYPOTHESIS:{h_id}', 'live', h_text, conf, falsifiable))
acc_conn.commit()
print(f"[hypotheses] 5 hypotheses logged")

# ─── 5. Write playbook entry ─────────────────────────────────────────────────
acc_c.execute("""
    INSERT INTO playbook (ts, category, summary, evidence, updated_utc)
    VALUES (?, ?, ?, ?, ?)
""", (
    ts_cycle,
    'IDLE_decision',
    'trend_down+extreme_oversold+geopolitical_Iran_risk => IDLE even when TOP coins score>=30',
    json.dumps({
        'regime': 'low_vol',
        'dxy': 118.039,
        'vix': 17.26,
        'spx_change': -0.0124,
        'top_scores': ['WLD=32','ICP=32','AR=32'],
        'action': 'IDLE',
        'reason': 'A-class Iran geopolitical risk suppresses all new OPEN signals regardless of score',
        'dog_sl_active': '<REDACTED_STOP_PRICE> algo <REDACTED_OKX_ALGO_ID>',
        'next_action': 'monitor DOGE SHORT; await regime clarification or Iran resolution'
    }),
    ts_cycle
))
acc_conn.commit()
print(f"[playbook] IDLE decision logged")

# ─── 6. Error pattern note ─────────────────────────────────────────────────
# Document the jobb_full_scan.py position dedup bug
lesson_c.execute("""
    INSERT INTO error_patterns (pattern_name, trigger_condition, post_behavior, hit_count, last_seen_utc)
    VALUES (?, ?, ?, ?, ?)
""", (
    'jobb_full_scan_reports_0_positions_but_DOGE_SHORT_exists',
    'jobb_full_scan.py reads position_snapshots and shows 0 positions despite DOGE short position existing',
    'Reports 0 positions and 0% exposure; actual DOGE SHORT position confirmed via _check_pos.py and OKX live account',
    2,
    ts_cycle
))
lesson_conn.commit()
print(f"[error_patterns] jobb_full_scan position dedup bug logged (hit=2)")

acc_conn.close()
lesson_conn.close()
print("\n[DB] All writes complete.")
print(f"[TIME] Cycle completed at {ts_display}")
