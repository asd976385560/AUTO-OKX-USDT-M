"""Write all scored coins to scoring_history from tmp_scan.json"""
import sqlite3, json
from pathlib import Path

DB = Path(r"E:\OKX\db")
TS = "2026-05-16T21:36Z"  # collect_data timestamp
REGIME = "low_vol"
SIDE = "short"  # for DOGE only

conn = sqlite3.connect(str(DB / "account.db"))

# Load tmp_scan.json
scan_file = DB / "tmp_scan.json"
if not scan_file.exists():
    print("[WARN] tmp_scan.json not found, skipping bulk score write")
    conn.close()
    exit(0)

with open(scan_file) as f:
    data = json.load(f)

top20 = data.get("top20", [])
doge_data = data.get("doge_position", {})

# Write top 20 scores
written = 0
for coin in top20:
    sym = coin["symbol"]
    total = coin["total"]
    action = "HOLD_SHORT" if sym == "DOGE-USDT-SWAP" else "IDLE"
    side = "short" if sym == "DOGE-USDT-SWAP" else None
    entry = doge_data.get("avgPx") if sym == "DOGE-USDT-SWAP" else None
    
    reasoning = (
        f"{sym}: 1H RSI={coin.get('rsi15','?')[:5]}, funding={coin.get('funding','?')}, "
        f"score={total}, below MA20={coin.get('vs_ma20_pct','?')}% "
        f"→ {action}. low_vol regime."
    )
    
    conn.execute("""
        INSERT OR IGNORE INTO scoring_history 
        (ts, symbol, dim1, dim2, dim3, dim4, dim5, total, action, ai_reasoning, regime, side, open_fill_px)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (TS, sym,
          coin.get("dim1", 0), coin.get("dim2", 0), coin.get("dim3", 5),
          coin.get("dim4", 0), coin.get("dim5", 0),
          total, action, reasoning, REGIME, side, entry))
    written += 1

conn.commit()
conn.close()
print(f"[OK] Wrote {written} coin scores to scoring_history")
