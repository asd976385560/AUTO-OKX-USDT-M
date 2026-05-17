import sqlite3
from datetime import datetime, timezone, timedelta
UTC8 = timezone(timedelta(hours=8))
now_utc8 = datetime.now(UTC8)
now_str = now_utc8.strftime("%Y-%m-%dT%H:%M:%SZ")

ld = sqlite3.connect(r'E:\OKX\db\lessons.db')

# Log the bug found in jobb_full_scan.py
cur = ld.execute("SELECT pattern_name FROM error_patterns WHERE pattern_name='position_snapshot_dup_read'")
existing = cur.fetchone()
if existing:
    ld.execute("UPDATE error_patterns SET hit_count=hit_count+1, last_seen_utc=? WHERE pattern_name='position_snapshot_dup_read'", [now_str])
    print("Updated existing error_pattern hit_count")
else:
    ld.execute("""
        INSERT INTO error_patterns (pattern_name, trigger_condition, post_behavior, hit_count, last_seen_utc)
        VALUES ('position_snapshot_dup_read', 
                'position_snapshots has multiple ts entries per symbol (PK=ts,profile,symbol). Full scan reads ORDER BY ts DESC LIMIT 10 without deduplication.',
                'position count inflated, state mis-reported as 0 positions when DOGE SHORT active, state→IDLE instead of HOLD_SHORT',
                1, ?)
    """, [now_str])
    print("Inserted new error_pattern")

ld.commit()
ld.close()
print("Done.")
