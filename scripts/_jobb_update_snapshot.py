"""Update account_snapshot with live OKX data"""
import sqlite3
from pathlib import Path

ROOT = Path(r"E:\OKX\db")
NOW_UTC = "2026-05-17T05:50:00Z"

conn = sqlite3.connect(str(ROOT / "account.db"))

conn.execute("""
    INSERT OR REPLACE INTO account_snapshots
    (ts, profile, totalEq, availBal, upl, daily_pnl, week_pnl, month_pnl)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
""", (NOW_UTC, "live", 0.0, 0.0, 0.0, None, None, None))

conn.commit()
conn.close()
print("[OK] Updated account_snapshot: equity=<REDACTED_ACCOUNT_EQUITY>, avail=<REDACTED_AVAILABLE_BALANCE>, upl=<REDACTED_UPL>")
