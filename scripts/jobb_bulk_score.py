"""Write top 20 coin scores to scoring_history - from jobb_score.py fresh run"""
import sqlite3, json
from pathlib import Path

DB = Path(r"E:\OKX\db")
TS = "2026-05-16T21:36Z"
REGIME = "low_vol"

conn = sqlite3.connect(str(DB / "account.db"))

# From jobb_score.py fresh run (276 coins scored, top 20 from stdout)
# Full list of all feasible coins with 5-dim scores
coins = [
    ("W-USDT-SWAP",       10, 8, 5, 8, 9,  40, "below MA20, funding=-0.0005, RSI15=34.8"),
    ("ETHW-USDT-SWAP",    10, 8, 5, 8, 8,  39, "below MA20, funding=-0.0003, RSI15=39.1"),
    ("ATH-USDT-SWAP",     10, 7, 5, 8, 8,  38, "below MA20, funding=-0.0002, RSI15=38.7"),
    ("SPK-USDT-SWAP",      9, 7, 5, 8, 9,  38, "below MA20, funding=-0.0003, RSI15=19.7"),
    ("ACT-USDT-SWAP",     10, 8, 5, 8, 6,  37, "below MA20, funding=+0.0001, RSI15=33.3"),
    ("APT-USDT-SWAP",      9, 8, 5, 8, 7,  37, "below MA20, funding~-0, RSI15=18.7"),
    ("BONK-USDT-SWAP",    10, 8, 5, 8, 6,  37, "below MA20, funding=-0.0001, RSI15=38.7"),
    ("EDGE-USDT-SWAP",    10, 7, 5, 8, 7,  37, "below MA20, funding=-0.0001, RSI15=30.6"),
    ("FOGO-USDT-SWAP",     9, 8, 5, 8, 7,  37, "below MA20, funding=-0.0001, RSI15=20.0"),
    ("LAYER-USDT-SWAP",   10, 8, 5, 8, 6,  37, "below MA20, funding=+0.0001, RSI15=34.2"),
    ("LDO-USDT-SWAP",      9, 8, 5, 8, 7,  37, "below MA20, funding=-0.0001, RSI15=30.0"),
    ("LINEA-USDT-SWAP",   10, 8, 5, 8, 6,  37, "below MA20, funding~0, RSI15=32.1"),
    ("MAGIC-USDT-SWAP",    8, 8, 5, 8, 8,  37, "below MA20, funding=-0.0002, RSI15=37.1"),
    ("UB-USDT-SWAP",      10, 8, 5, 8, 6,  37, "below MA20, funding=+0.0001, RSI15=35.0"),
    ("VIRTUAL-USDT-SWAP", 10, 8, 5, 8, 6,  37, "below MA20, funding~-0, RSI15=35.6"),
    ("WLD-USDT-SWAP",      8, 8, 5, 8, 8,  37, "below MA20, funding=-0.0006, RSI15=40.5"),
    ("ARB-USDT-SWAP",      9, 8, 5, 8, 6,  36, "below MA20, funding~0, RSI15=30.0"),
    ("ASTER-USDT-SWAP",   10, 8, 5, 8, 5,  36, "below MA20, funding=+0.0001, RSI15=35.4"),
    ("AZTEC-USDT-SWAP",   10, 7, 5, 8, 6,  36, "below MA20, funding=+0.0001, RSI15=31.2"),
    ("BLUR-USDT-SWAP",    10, 8, 5, 8, 5,  36, "below MA20, funding=+0.0001, RSI15=35.2"),
    # DOGE is special - has actual position
    ("DOGE-USDT-SWAP",     9, 6, 5, 4, 5,  29, "HOLD_SHORT: 1H RSI=38.5 optimal SHORT zone, below MA20s, 4H RSI=41.5 valid"),
    # Additional coins from top 25
    ("CFX-USDT-SWAP",     10, 8, 5, 8, 5,  36, "below MA20, funding~0, RSI15=39.2"),
    ("CHIP-USDT-SWAP",     8, 7, 5, 8, 8,  36, "below MA20, funding=-0.0003, RSI15=39.0"),
    ("EIGEN-USDT-SWAP",    9, 8, 5, 8, 6,  36, "below MA20, funding=+0.0001, RSI15=24.2"),
    ("ETHFI-USDT-SWAP",    9, 8, 5, 8, 6,  36, "below MA20, funding=+0.0001, RSI15=23.8"),
]

# DOGE special fields
DOGE_ENTRY = 0.0  # redacted position entry
DOGE_SIDE = "short"
DOGE_ACTION = "HOLD_SHORT"

written = 0
for coin in coins:
    sym, d1, d2, d3, d4, d5, total, reasoning = coin
    action = DOGE_ACTION if sym == "DOGE-USDT-SWAP" else "IDLE"
    side = DOGE_SIDE if sym == "DOGE-USDT-SWAP" else None
    entry = DOGE_ENTRY if sym == "DOGE-USDT-SWAP" else None
    
    conn.execute("""
        INSERT OR REPLACE INTO scoring_history 
        (ts, symbol, dim1, dim2, dim3, dim4, dim5, total, action, ai_reasoning, regime, side, open_fill_px)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (TS, sym, d1, d2, d3, d4, d5, total, action, reasoning, REGIME, side, entry))
    written += 1

conn.commit()
conn.close()
print(f"[OK] Wrote {written} coin scores to scoring_history")
