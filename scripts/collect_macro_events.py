# -*- coding: utf-8 -*-
"""经济日历采集（未来窗 + actual 回查窗），写 regime.db.macro_events。

2026-08-19 D1：旧版只查 [now, now+7d] 纯未来窗，发布后从不回查 —— 实测
``macro_events`` 77 行里 ``actual`` **0 行有值**、``forecast`` 68 行有值。
一个「事件敏感」的系统只知道几点有 CPI、不知道 CPI 打了多少，surprise
（actual-forecast）这个 15m 级最强可交易冲量完全采不到。本版加一个
[now-3d, now] 的回查窗，并把 importance 门从 3 放到 2（PPI/初请/PMI 等
对加密有实证冲击的次级事件）。写库改走 public_macro.upsert_macro_events
（只补不抹），不再手写 INSERT OR REPLACE。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, _public_project_path('collectors'))
import ledger  # noqa: E402

from _okxcli import okx_json
from public_macro import upsert_macro_events

CST = timezone(timedelta(hours=8))


def fmt_ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, CST).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def _fetch_window(importance: int, start: datetime, end: datetime) -> list:
    """一个 [start, end] 窗的日历页（--before=窗起、--after=窗止，同既有实参语义）。"""
    payload = okx_json(
        "news", "economic-calendar",
        "--importance", str(importance),
        "--before", str(int(start.timestamp() * 1000)),
        "--after", str(int(end.timestamp() * 1000)),
        "--limit", "100",
        timeout_sec=45,
    )
    return payload if isinstance(payload, list) else (payload.get("data") or [])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="经济日历采集（未来窗 + actual 回查窗）")
    ap.add_argument("--db-root", default=_public_project_path('db'))
    ap.add_argument("--days", type=int, default=7)
    # D1：3 降到 2 —— importance=2 含 PPI/初请/PMI 等对加密有实证冲击的次级
    # 事件；只扩证据面，不构成任何自动闸。
    ap.add_argument("--importance", type=int, default=2)
    # 回查窗：事件发布后 actual 才出现，纯未来窗永远采不到（实证 actual 0/77）。
    ap.add_argument("--lookback-days", type=int, default=3)
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    windows = [
        ("forward", now, now + timedelta(days=max(1, args.days))),
        ("lookback", now - timedelta(days=max(1, args.lookback_days)), now),
    ]
    items: list = []
    per_window: dict = {}
    for name, start, end in windows:
        try:
            got = _fetch_window(args.importance, start, end)
        except Exception as exc:   # 单窗失败不吞掉另一窗（回查是补充非关键路径）
            print(f"[collect_macro_events][WARN] {name} 窗失败: {exc}",
                  file=sys.stderr, flush=True)
            got = []
        per_window[name] = len(got)
        items.extend(got)
    fetched_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for item in items:
        event_ts = fmt_ms(item.get("date"))
        calendar_id = str(item.get("calendarId") or "")
        event = str(item.get("event") or "").strip()
        if not calendar_id or not event_ts or not event:
            continue
        rows.append({
            "calendar_id": calendar_id, "event_ts": event_ts,
            "region": item.get("region"), "category": item.get("category"),
            "event": event, "importance": int(item.get("importance") or 0),
            "forecast": item.get("forecast"), "previous": item.get("previous"),
            "actual": item.get("actual"), "unit": item.get("unit"),
            "ref_date": fmt_ms(item.get("refDate")),
            "updated_at": fmt_ms(item.get("uTime")), "fetched_at": fetched_at,
            "source": "okx_economic_calendar", "raw": item,
        })

    con = ledger.connect(Path(args.db_root) / "regime.db")
    try:
        # D1：唯一硬化写入口，只补不抹（禁手写 INSERT OR REPLACE —— 那会让
        # 未来窗刷新把回查窗已采到的 actual 冲回 NULL）。
        written = upsert_macro_events(con, rows)
        con.commit()
    finally:
        con.close()
    print(json.dumps({
        "ok": True, "fetched": len(items), "written": written,
        "window_days": args.days, "lookback_days": args.lookback_days,
        "windows": per_window, "importance": args.importance,
        "with_actual": sum(
            1 for r in rows if str(r.get("actual") or "").strip()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
