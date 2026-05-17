"""
JobB Cycle Write - Corrected
"""
import sqlite3, json
from datetime import datetime, timezone

ROOT = r"E:\OKX\db"
NOW_UTC = "2026-05-17T04:05:00Z"
CYCLE_ID = "JobB-15min-20260517T040500Z"
NET_WORTH = 1000.0  # placeholder account equity
REGIME = "low_vol"

# Current DOGE position data
DOGE_SIDE = "short"
DOGE_SZ = 1.0  # placeholder position size
DOGE_ENTRY = 0.1  # placeholder entry price
DOGE_CURRENT = 0.1  # placeholder mark price
DOGE_MARGIN = DOGE_SZ * DOGE_CURRENT * 1000 / 3.0
DOGE_MARGIN_PCT = DOGE_MARGIN / NET_WORTH * 100  # 9.17%
DOGE_SL = 0.11
DOGE_SL_DIST = (DOGE_SL - DOGE_CURRENT) / DOGE_CURRENT * 100  # 2.09%
DOGE_MAX_LOSS = DOGE_SZ * (DOGE_SL - DOGE_CURRENT) * 1000  # ~$5.84
DOGE_1H_RSI = 44.3   # calculated from live candles
DOGE_4H_RSI = 40.2  # calculated from live candles
DOGE_15M_RSI = 57.3  # from prior

ACTION = "HOLD_SHORT"
DEGRADATION = "data_stale_60min"
REASON = ("DOGE SHORT hold: 1H RSI=44.3 (valid SHORT zone 35-45) + price below 1H MA20(0.11024) and 4H MA20(0.11181). "
          "lower highs intact. 4H RSI=40.2 in valid range. "
          "Data stale >60min → no new positions. Existing SL <REDACTED_STOP_PRICE> intact. margin <REDACTED_MARGIN_PCT> < 10%. "
          "OKX live equity=<REDACTED_ACCOUNT_EQUITY>, DOGE short frozen=<REDACTED_MARGIN>. "
          "no second-entry SHORT with better risk-reward than DOGE in low_vol regime.")

# ===== 1. cycle_runs =====
conn = sqlite3.connect(f"{ROOT}\\account.db")
conn.execute("""
    INSERT OR REPLACE INTO cycle_runs (ts_start, ts_end, job_id, profile, state_before, state_after, error)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", (CYCLE_ID, NOW_UTC, "JobB", "live", "HOLD_SHORT", "HOLD_SHORT", None))
conn.commit()
print("[OK] cycle_runs written")

# ===== 2. trade_events =====
upl = round(DOGE_SZ * (DOGE_CURRENT - DOGE_ENTRY) * 1000, 2)
trade_row = (
    NOW_UTC, "live", "DOGE-USDT-SWAP", ACTION, DOGE_SIDE, DOGE_SZ, DOGE_CURRENT,
    26, REASON, None, DEGRADATION, "main", upl,
    json.dumps({
        "regime": REGIME, "net_worth": NET_WORTH,
        "doge_short": {
            "sz": DOGE_SZ, "entry": DOGE_ENTRY, "current": DOGE_CURRENT,
            "lev": 3.0, "upl": upl, "stop": DOGE_SL, "margin": round(DOGE_MARGIN, 2),
            "margin_pct": round(DOGE_MARGIN_PCT, 2),
            "1h_rsi": DOGE_1H_RSI, "4h_rsi": DOGE_4H_RSI, "15m_rsi": DOGE_15M_RSI,
            "sl_dist_pct": round(DOGE_SL_DIST, 2), "max_loss_usd": round(DOGE_MAX_LOSS, 2)
        },
        "action": ACTION, "reason": REASON, "degradation": DEGRADATION
    }, ensure_ascii=False)
)
conn.execute("""
    INSERT INTO trade_events (ts, profile, symbol, action, side, sz, fill_px, score_total, ai_reasoning, ai_deviation, degradation, channel, pnl, raw)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", trade_row)
conn.commit()
print("[OK] trade_events written")

# ===== 3. system_state =====
conn.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
             ("state", "HOLD_SHORT", NOW_UTC))
conn.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
             ("last_job_b_run", NOW_UTC, NOW_UTC))
conn.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
             ("last_jobb_run", NOW_UTC, NOW_UTC))
conn.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
             ("last_jobb_decision", ACTION, NOW_UTC))
conn.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
             ("jobb_cycle_id", CYCLE_ID, NOW_UTC))
conn.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
             ("last_jobb_regime", REGIME, NOW_UTC))
conn.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
             ("net_worth", str(NET_WORTH), NOW_UTC))
conn.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
             ("position_note", json.dumps({
                 "symbol": "DOGE-USDT-SWAP", "side": "short", "sz": DOGE_SZ,
                 "entry": DOGE_ENTRY, "stop": DOGE_SL, "margin_pct": round(DOGE_MARGIN_PCT, 1),
                 "lev": 3.0, "reason": REASON[:150]
             }), NOW_UTC))
conn.commit()
print("[OK] system_state written")
conn.close()

# ===== 4. Scoring history for DOGE =====
conn2 = sqlite3.connect(f"{ROOT}\\account.db")
conn2.execute("""
    INSERT OR REPLACE INTO scoring_history (ts, symbol, dim1, dim2, dim3, dim4, dim5, total, action, ai_reasoning, regime, side, open_fill_px)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (NOW_UTC, "DOGE-USDT-SWAP", 9, 4, 5, 4, 5, 27, ACTION,
      "DOGE 1H RSI=44.3 valid SHORT zone 35-45; 4H RSI=40.2 valid; price below MA20s; stale data → hold only",
      REGIME, DOGE_SIDE, None))
conn2.commit()
conn2.close()
print("[OK] DOGE scoring_history written")

# ===== 5. Hypotheses to lessons.db =====
conn_l = sqlite3.connect(f"{ROOT}\\lessons.db")
hypotheses = [
    {
        "id": "hyp-20260517-01",
        "summary": "Data stale >60min: only HOLD/REDUCE/CLOSE allowed, no new opens",
        "falsifiable": "Stale data leads to worse trade outcomes if new positions are opened",
        "confidence": "high",
    },
    {
        "id": "hyp-20260517-02",
        "summary": "DOGE 1H RSI 44.3 + 4H RSI 40.2 + below MA20s = valid SHORT hold in low_vol regime",
        "falsifiable": "DOGE SHORT hold is profitable or break-even in current conditions",
        "confidence": "medium",
    },
]
for h in hypotheses:
    conn_l.execute("""
        INSERT INTO missed_opportunities (ts, symbol, score, regime, direction_hint, notes, reviewed_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (NOW_UTC, "DOGE-USDT-SWAP", 27, REGIME, "short",
          f"hypothesis={h['id']}: {h['summary']}", NOW_UTC))
conn_l.commit()
conn_l.close()
print(f"[OK] {len(hypotheses)} hypotheses written")

print("\n[DONE] JobB cycle write complete")
print(f"Cycle: {CYCLE_ID}")
print(f"Action: {ACTION}")
print("Equity: <REDACTED_ACCOUNT_EQUITY>")
print("DOGE short: <REDACTED_POSITION_DETAILS>")
print(f"SL: {DOGE_SL} ({DOGE_SL_DIST:.1f}% above current), margin={DOGE_MARGIN_PCT:.1f}%")
