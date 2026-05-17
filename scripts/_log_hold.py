import sqlite3
from datetime import datetime, timezone, timedelta
UTC8 = timezone(timedelta(hours=8))
now_utc8 = datetime.now(UTC8)
now_str = now_utc8.strftime("%Y-%m-%dT%H:%M:%SZ")

ac = sqlite3.connect(r'E:\OKX\db\account.db')

# Write HOLD_SHORT DOGE trade_event for this cycle
ac.execute("""
    INSERT INTO trade_events (ts, profile, symbol, action, side, sz, fill_px, score_total, ai_reasoning, ai_deviation, degradation, channel, pnl, raw)
        VALUES (?, 'live', 'DOGE-USDT-SWAP', 'HOLD_SHORT', 'short', 0.0, 0.0, 0, 
            'HOLD_SHORT DOGE. Position details redacted. Regime forces IDLE on new positions. Existing short thesis intact.',
            'No deviation from scoring - DOGE top5 score but RSI extreme oversold suppresses new SHORT. Existing HOLD_SHORT maintained.',
            NULL, 'main', 0.0, NULL)
""", [now_str])
print(f"Wrote HOLD_SHORT DOGE trade_event at {now_str}")

ac.commit()

# Verify last 3 trade events
cur = ac.execute("SELECT ts, symbol, action, side, sz, fill_px, pnl FROM trade_events WHERE profile='live' ORDER BY ts DESC LIMIT 3")
for r in cur.fetchall():
    print(f"  {r}")
ac.close()
print("Done.")
