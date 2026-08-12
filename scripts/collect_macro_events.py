# -*- coding: utf-8 -*-
"""采集未来7天高重要度经济日历，写 regime.db.macro_events。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, r"./collectors")
import ledger  # noqa: E402

from _okxcli import okx_json

CST = timezone(timedelta(hours=8))


def fmt_ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, CST).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="未来高重要度经济日历采集")
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--importance", type=int, default=3)
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    before_ms = str(int(now.timestamp() * 1000))
    after_ms = str(int((now + timedelta(days=max(1, args.days))).timestamp() * 1000))
    payload = okx_json(
        "news", "economic-calendar",
        "--importance", str(args.importance),
        "--before", before_ms,
        "--after", after_ms,
        "--limit", "100",
        timeout_sec=45,
    )
    items = payload if isinstance(payload, list) else (payload.get("data") or [])
    fetched_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for item in items:
        event_ts = fmt_ms(item.get("date"))
        calendar_id = str(item.get("calendarId") or "")
        event = str(item.get("event") or "").strip()
        if not calendar_id or not event_ts or not event:
            continue
        rows.append((
            calendar_id, event_ts, item.get("region"), item.get("category"), event,
            int(item.get("importance") or 0), item.get("forecast"), item.get("previous"),
            item.get("actual"), item.get("unit"), fmt_ms(item.get("refDate")),
            fmt_ms(item.get("uTime")), fetched_at, "okx_economic_calendar",
            json.dumps(item, ensure_ascii=False),
        ))

    con = ledger.connect(Path(args.db_root) / "regime.db")
    try:
        con.executemany(
            "INSERT OR REPLACE INTO macro_events "
            "(calendar_id,event_ts,region,category,event,importance,forecast,previous,"
            "actual,unit,ref_date,updated_at,fetched_at,source,raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()
    finally:
        con.close()
    print(json.dumps({
        "ok": True, "fetched": len(items), "written": len(rows),
        "window_days": args.days, "importance": args.importance,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
