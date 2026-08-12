# -*- coding: utf-8 -*-
"""Cycle-scoped deterministic news snapshot for writer and actor handoff checks."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
VERSION = "news_context_v1"


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text[:-1] + "+00:00").astimezone(CST)
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=CST) if parsed.tzinfo is None else parsed.astimezone(CST)
    except ValueError:
        return None


def _as_of(value: str) -> datetime:
    parsed = _parse_ts(value.replace("T", " ") + (":00" if len(value) == 16 else ""))
    if parsed is None:
        raise ValueError(f"news_context as_of 非法: {value!r}")
    return parsed


def _freshness(event_day: Any, as_of: datetime) -> str:
    try:
        day = datetime.strptime(str(event_day)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "unknown"
    age = (as_of.date() - day).days
    if age < 0:
        return "scheduled"
    if age == 0:
        return "fresh"
    if age <= 2:
        return "recent"
    return "stale"


def build_news_context(db_root: str | Path, as_of: str,
                       window_hours: int = 6) -> dict[str, Any]:
    root = Path(db_root)
    db = root / "news.db"
    at = _as_of(str(as_of))
    start = at - timedelta(hours=window_hours)
    items: list[dict[str, Any]] = []
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            cols = {str(r[1]) for r in con.execute("PRAGMA table_info(news_items)")}
            required = {
                "event_occurred_at", "event_time_confidence", "first_seen_at",
                "source_grade", "event_key", "news_time_version",
            }
            if required.issubset(cols):
                rows = con.execute(
                    "SELECT id,ts,ingested_at,severity,symbol,title,"
                    "event_occurred_at,event_time_confidence,first_seen_at,"
                    "source_grade,primary_source_url,event_key,news_time_version "
                    "FROM news_items WHERE severity IN ('critical','high') "
                    "ORDER BY id"
                ).fetchall()
                for row in rows:
                    observed = _parse_ts(row["ingested_at"] or row["ts"])
                    if observed is None or observed < start or observed > at:
                        continue
                    title_hash = hashlib.sha256(
                        str(row["title"] or "").encode("utf-8")
                    ).hexdigest()[:12]
                    items.append({
                        "id": int(row["id"]),
                        "severity": row["severity"],
                        "symbol": row["symbol"],
                        "title_hash": title_hash,
                        "event_occurred_at": row["event_occurred_at"],
                        "event_time_confidence": row["event_time_confidence"],
                        "catalyst_freshness": _freshness(
                            row["event_occurred_at"], at),
                        "first_seen_at": row["first_seen_at"],
                        "source_grade": row["source_grade"],
                        "primary_source_url": row["primary_source_url"],
                        "event_key": row["event_key"],
                        "news_time_version": row["news_time_version"],
                    })
        finally:
            con.close()
    body = {
        "version": VERSION,
        "as_of": at.strftime("%Y-%m-%d %H:%M:%S"),
        "window_hours": int(window_hours),
        "items": items,
    }
    body["context_hash"] = hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return body
