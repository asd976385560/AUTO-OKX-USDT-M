"""
oi_24h_change_report.py
Compute 24h OI change from latest snapshot in market.db derivatives
Handles mixed format: 'YYYY-MM-DD HH:MM:SS' (UTC+8) vs 'YYYY-MM-DDTHH:MM:SSZ' (ISO UTC).
Outputs TOP3 by |change%|.
"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import json
import sys
from datetime import datetime, timezone, timedelta

from _db_ro import connect_ro

DB_PATH = _project_path('db', 'market.db')


def parse_ts(s: str) -> datetime | None:
    """Parse both UTC+8 string and ISO UTC string into UTC+8 datetime."""
    if not s:
        return None
    try:
        if "T" in s:
            # ISO UTC: 2026-06-07T19:16:56Z
            dt_utc = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return dt_utc.astimezone(timezone(timedelta(hours=8)))
        else:
            # UTC+8 string: 2026-06-07 16:19:09
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8)))
    except Exception:
        return None


def main() -> int:
    con = connect_ro(DB_PATH, timeout=10)  # 只读 mode=ro（2026-07-03）
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='derivatives'")
    if not cur.fetchone():
        print(json.dumps({"error": "no derivatives table"}, ensure_ascii=False))
        return 1

    # latest snapshot ts
    cur = con.execute("SELECT MAX(ts) FROM derivatives")
    latest_ts_raw = cur.fetchone()[0]
    if not latest_ts_raw:
        print(json.dumps({"error": "no data"}, ensure_ascii=False))
        return 1

    latest_dt = parse_ts(latest_ts_raw)
    if not latest_dt:
        print(json.dumps({"error": f"cannot parse latest ts: {latest_ts_raw}"}, ensure_ascii=False))
        return 1
    target = latest_dt - timedelta(hours=24)

    # get all snapshots, parse ts, group by (symbol, parsed_dt)
    cur = con.execute(
        "SELECT symbol, ts, oi_usd FROM derivatives WHERE oi_usd IS NOT NULL ORDER BY symbol, ts"
    )
    by_sym: dict[str, list[tuple[datetime, float]]] = {}
    for sym, ts, oi in cur.fetchall():
        dt = parse_ts(ts)
        if dt is None or oi is None:
            continue
        by_sym.setdefault(sym, []).append((dt, float(oi)))

    out_rows = []
    for sym, lst in by_sym.items():
        if len(lst) < 2:
            continue
        # latest for this sym
        own_latest_dt, latest_oi = lst[-1]
        # use EARLIEST available snapshot as baseline (24h data not always present)
        prev_dt, prev_oi = lst[0]
        lookback_h = (own_latest_dt - prev_dt).total_seconds() / 3600.0
        if prev_oi == 0:
            continue
        chg_pct = (latest_oi - prev_oi) / prev_oi * 100
        out_rows.append({
            "symbol": sym,
            "latest_ts": own_latest_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "prev_ts": prev_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "lookback_h": round(lookback_h, 2),
            "oi_usd": round(latest_oi, 2),
            "oi_baseline": round(prev_oi, 2),
            "oi_change_pct": round(chg_pct, 3),
            "abs_chg": abs(chg_pct),
        })

    out_rows.sort(key=lambda x: x["abs_chg"], reverse=True)
    print(json.dumps({
        "latest_ts_raw": latest_ts_raw,
        "target_24h_ago": target.strftime("%Y-%m-%d %H:%M:%S"),
        "rows_scanned": sum(len(v) for v in by_sym.values()),
        "symbols_with_24h_data": len(out_rows),
        "oi_24h_change_top3": out_rows[:3],
    }, ensure_ascii=False, indent=2))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
