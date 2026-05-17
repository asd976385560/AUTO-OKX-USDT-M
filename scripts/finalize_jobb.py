import sqlite3, json
conn=sqlite3.connect(r'E:\OKX\db\account.db')
NOW_UTC = "2026-05-16T19:35:00Z"
# Update position_note
note = '{"symbol":"DOGE-USDT-SWAP","side":"short","sz":"<REDACTED_POSITION_SIZE>","entry":"<REDACTED_ENTRY_PRICE>","stop":"<REDACTED_STOP_PRICE>","margin_pct":"<REDACTED_MARGIN_PCT>","lev":3.0,"reason":"HOLD_SHORT position details redacted"}'
conn.execute("INSERT OR REPLACE INTO system_state (key,value,updated_utc) VALUES (?,?,?)",
             ("position_note", note, NOW_UTC))
conn.commit()
print("[OK] position_note updated")

# Write self-review entry
conn.execute("""
    INSERT INTO trade_events (ts, profile, symbol, action, side, sz, fill_px, score_total, ai_reasoning, channel, raw)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    NOW_UTC, "live", "DOGE-USDT-SWAP", "CYCLE_CHECK", "short", 0.0, 0.0,
    26,
    "Cycle check complete. HOLD_SHORT DOGE. All safety checks passed.",
    "main",
    json.dumps({"cycle": "JobB-15min-20260516T193500ZZ", "action": "HOLD_SHORT", "net_worth": "<REDACTED_ACCOUNT_EQUITY>"}, ensure_ascii=False)
))
conn.commit()
print("[OK] cycle check event written")
conn.close()
