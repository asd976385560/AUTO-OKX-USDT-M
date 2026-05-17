import sqlite3
from datetime import datetime, timezone, timedelta
UTC8 = timezone(timedelta(hours=8))
now_utc8 = datetime.now(UTC8)
now_str = now_utc8.strftime("%Y-%m-%dT%H:%M:%SZ")

ac = sqlite3.connect(r'E:\OKX\db\account.db')

# Check current state before updating
cur = ac.execute("SELECT value FROM system_state WHERE key='state'")
old_state = cur.fetchone()
print(f"Current state: {old_state[0] if old_state else 'NOT SET'}")

# Update state to HOLD_SHORT (we have DOGE SHORT active)
ac.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES ('state', 'HOLD_SHORT', ?)", [now_str])
print(f"Updated state -> HOLD_SHORT at {now_str}")

# Also update last_job_b_run to match last_jobb_run
ac.execute("INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES ('last_job_b_run', '2026-05-16T14:21:27Z', ?)", [now_str])
print("Fixed last_job_b_run -> 2026-05-16T14:21:27Z")

ac.commit()

# Verify
cur = ac.execute("SELECT key, value FROM system_state WHERE key IN ('state', 'last_jobb_run', 'last_job_b_run', 'jobb_cycle_id')")
for r in cur.fetchall():
    print(f"  {r[0]}: {r[1]}")
ac.close()
print("Done.")
