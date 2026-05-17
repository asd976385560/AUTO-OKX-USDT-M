"""JobB Cycle Write - Current fresh cycle"""
import sqlite3, json
from datetime import datetime, timezone

ROOT = r"E:\OKX\db"

# Current time
NOW_UTC = "2026-05-16T21:36Z"
CYCLE_ID = "JobB-15min-20260517T053500Z"

# Account data (live OKX)
NET_WORTH = 1000.0  # placeholder account equity
EQUITY = NET_WORTH

# DOGE position
DOGE_SIDE = "short"
DOGE_SZ = 1.0  # placeholder position size
DOGE_ENTRY = 0.1  # placeholder entry price
DOGE_CURRENT = 0.1  # placeholder mark price
DOGE_MARGIN = DOGE_SZ * DOGE_CURRENT * 1000 / 3.0  # lev=3, ctVal=1000
DOGE_MARGIN_PCT = DOGE_MARGIN / NET_WORTH * 100
DOGE_SL = 0.11
DOGE_SL_DIST = (DOGE_SL - DOGE_CURRENT) / DOGE_CURRENT * 100
DOGE_MAX_LOSS = DOGE_SZ * (DOGE_SL - DOGE_CURRENT) * 1000
DOGE_1H_RSI = 38.5
DOGE_4H_RSI = 41.5
DOGE_15M_RSI = 57.4

ACTION = "HOLD_SHORT"
DEGRADATION = "data_fresh"  # collect_data just ran successfully
REGIME = "low_vol"
upl = round(DOGE_SZ * (DOGE_CURRENT - DOGE_ENTRY) * 1000, 2)

REASON = (
    f"DOGE SHORT hold: 1H RSI={DOGE_1H_RSI} (optimal SHORT zone 35-42) + "
    f"price below 1H MA20(0.11011) and 4H MA20(0.11158). "
    f"lower highs intact <REDACTED_PRICE_RANGE>. "
    f"4H RSI={DOGE_4H_RSI} in valid range. "
    f"SL@<REDACTED_STOP_PRICE> active (algo <REDACTED_OKX_ALGO_ID>). "
    f"margin={round(DOGE_MARGIN,2)}=({round(DOGE_MARGIN_PCT,1)}%)<10%. "
    f"low_vol regime → no new positions. "
    f"fresh data OK. HOLD_SHORT."
)

# 5-dim scores for DOGE
D1 = 9  # technical: 1H RSI=38.5 in optimal SHORT zone
D2 = 6  # structure: below MA20s, lower highs intact
D3 = 5  # news/events: neutral
D4 = 4  # cross-market: DXY=118 strong, BTC weak
D5 = 5  # funding/sentiment: funding=+0.0001 slightly unfavorable
DOGE_TOTAL = D1+D2+D3+D4+D5  # 29

conn = sqlite3.connect(f"{ROOT}\\account.db")

# 1. cycle_runs - INSERT OR REPLACE by ts_start
conn.execute("""
    INSERT OR REPLACE INTO cycle_runs (ts_start, ts_end, job_id, profile, state_before, state_after, error)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", (CYCLE_ID, NOW_UTC, "JobB", "live", "HOLD_SHORT", "HOLD_SHORT", None))

# 2. trade_events
trade_raw = json.dumps({
    "regime": REGIME, "net_worth": NET_WORTH,
    "doge_short": {
        "sz": DOGE_SZ, "entry": DOGE_ENTRY, "current": DOGE_CURRENT,
        "lev": 3.0, "upl": upl, "stop": DOGE_SL, "margin": round(DOGE_MARGIN, 2),
        "margin_pct": round(DOGE_MARGIN_PCT, 2),
        "1h_rsi": DOGE_1H_RSI, "4h_rsi": DOGE_4H_RSI, "15m_rsi": DOGE_15M_RSI,
        "sl_dist_pct": round(DOGE_SL_DIST, 2), "max_loss_usd": round(DOGE_MAX_LOSS, 2),
        "algo_id": "<REDACTED_OKX_ALGO_ID>"
    },
    "top_scores": [
        ("W-USDT-SWAP", 40), ("ETHW-USDT-SWAP", 39), ("ATH-USDT-SWAP", 38),
        ("SPK-USDT-SWAP", 38), ("ACT-USDT-SWAP", 37), ("APT-USDT-SWAP", 37),
        ("BONK-USDT-SWAP", 37), ("EDGE-USDT-SWAP", 37)
    ],
    "action": ACTION, "reason": REASON, "degradation": DEGRADATION
}, ensure_ascii=False)

conn.execute("""
    INSERT INTO trade_events (ts, profile, symbol, action, side, sz, fill_px, score_total, ai_reasoning, ai_deviation, degradation, channel, pnl, raw)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (NOW_UTC, "live", "DOGE-USDT-SWAP", ACTION, DOGE_SIDE, DOGE_SZ, DOGE_CURRENT,
      DOGE_TOTAL, REASON, None, DEGRADATION, "main", upl, trade_raw))

# 3. scoring_history - DOGE
conn.execute("""
    INSERT OR REPLACE INTO scoring_history (ts, symbol, dim1, dim2, dim3, dim4, dim5, total, action, ai_reasoning, regime, side, open_fill_px)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (NOW_UTC, "DOGE-USDT-SWAP", D1, D2, D3, D4, D5, DOGE_TOTAL, ACTION, REASON, REGIME, DOGE_SIDE, DOGE_ENTRY))

# 4. system_state updates
conn.execute("""
    INSERT OR REPLACE INTO system_state (key, value, updated_utc)
    VALUES (?, ?, ?)
""", ("state", "HOLD_SHORT", NOW_UTC))
conn.execute("""
    INSERT OR REPLACE INTO system_state (key, value, updated_utc)
    VALUES (?, ?, ?)
""", ("net_worth", str(round(NET_WORTH, 2)), NOW_UTC))
conn.execute("""
    INSERT OR REPLACE INTO system_state (key, value, updated_utc)
    VALUES (?, ?, ?)
""", ("last_jobb_run", CYCLE_ID.replace("JobB-15min-", ""), NOW_UTC))
conn.execute("""
    INSERT OR REPLACE INTO system_state (key, value, updated_utc)
    VALUES (?, ?, ?)
""", ("last_jobb_decision", ACTION, NOW_UTC))
conn.execute("""
    INSERT OR REPLACE INTO system_state (key, value, updated_utc)
    VALUES (?, ?, ?)
""", ("last_jobb_regime", REGIME, NOW_UTC))
conn.execute("""
    INSERT OR REPLACE INTO system_state (key, value, updated_utc)
    VALUES (?, ?, ?)
""", ("jobb_cycle_id", CYCLE_ID, NOW_UTC))
conn.execute("""
    INSERT OR REPLACE INTO system_state (key, value, updated_utc)
    VALUES (?, ?, ?)
""", ("position_note", json.dumps({
    "symbol": "DOGE-USDT-SWAP", "side": "short", "sz": DOGE_SZ,
    "entry": DOGE_ENTRY, "current": DOGE_CURRENT, "upl": upl,
    "lev": 3, "margin": round(DOGE_MARGIN, 2), "margin_pct": round(DOGE_MARGIN_PCT, 2),
    "sl": DOGE_SL, "sl_dist_pct": round(DOGE_SL_DIST, 2),
    "1h_rsi_est": round(DOGE_1H_RSI), "4h_rsi_est": round(DOGE_4H_RSI),
    "reason": "DOGE SHORT hold; RSI optimal zone; SL active; low_vol regime"
}, ensure_ascii=False), NOW_UTC))

conn.commit()
conn.close()

# 5. Write hypotheses
conn2 = sqlite3.connect(f"{ROOT}\\lessons.db")

hypotheses = [
    ("HYP-20260517-011", "DOGE RSI bounce from 38.5 to 55+ reverses back lower in low_vol regime with DXY>117", "medium-high", "DOGE breaks <REDACTED_STOP_PRICE> or RSI stays >55 for 4h"),
    ("HYP-20260517-012", "Fresh data (collect_data <15min old) confirms DOGE RSI zone accuracy; stale data suppresses valid signals", "medium-high", "Next stale data cycle shows different DOGE RSI zone"),
    ("HYP-20260517-013", "W-USDT score=40 is highest this cycle; if entered would be correct direction but low_vol regime favors existing positions", "medium", "W continues lower OR low_vol regime forces IDLE"),
    ("HYP-20260517-014", "DOGE short position (3x, 8.17% margin) is max safe exposure; adding second coin would breach 10% single-trade limit", "high", "Second coin entry causes margin >10% of net worth"),
    ("HYP-20260517-015", "Algo SL placed @ <REDACTED_STOP_PRICE> covers 1.5xATR(1H)+MA20 structure; position size redacted", "medium-high", "DOGE hits SL, max_loss=<REDACTED_MAX_LOSS> confirmed"),
]

for h_id, h_text, conf, falsifiable in hypotheses:
    conn2.execute("""
        INSERT INTO missed_opportunities (ts, symbol, score, regime, direction_hint, actual_4h_pct, would_hit_1R, notes, reviewed_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (NOW_UTC, "DOGE-USDT-SWAP", DOGE_TOTAL, REGIME, "short", None, 0,
          f"hypothesis_id={h_id} confidence={conf} falsifiable={falsifiable}", NOW_UTC))
    conn2.execute("""
        INSERT INTO error_patterns (pattern_name, trigger_condition, post_behavior, hit_count, last_seen_utc)
        VALUES (?, ?, ?, ?, ?)
    """, (f"hypothesis:{h_id}", h_text, conf, 1, NOW_UTC))

conn2.commit()
conn2.close()

print("[OK] All DB writes complete")
print(f"Cycle: {CYCLE_ID}")
print(f"Action: {ACTION}")
print("Equity: <REDACTED_ACCOUNT_EQUITY>")
print("DOGE: <REDACTED_POSITION_DETAILS>")
print(f"SL: {DOGE_SL} ({round(DOGE_SL_DIST,2)}% away), margin={round(DOGE_MARGIN,2)} ({round(DOGE_MARGIN_PCT,1)}%)")
print(f"Max loss: ${round(DOGE_MAX_LOSS,2)}")
print(f"Regime: {REGIME}")
print(f"1H RSI: {DOGE_1H_RSI}, 4H RSI: {DOGE_4H_RSI}, 15m RSI: {DOGE_15M_RSI}")
