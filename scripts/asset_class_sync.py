# -*- coding: utf-8 -*-
"""Synchronize asset classes from OKX's official instrument metadata.

``instCategory`` is the exchange-owned category of the instrument base asset:
1=crypto, 3=stocks, 4=commodities, 5=forex, 6=bonds.  The current local
schema supports the first three categories.  Unknown or unsupported values are
reported and never guessed.

Manual classifications are immutable.  Existing curated index/ETF rows are
also kept when OKX reports the broader stock category, because the local class
is intentionally more granular.  All other non-manual contradictions are
corrected to the official category by the existing market.db writer.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


CST = timezone(timedelta(hours=8))
OFFICIAL_CATEGORY_TO_CLASS = {
    "1": "crypto",
    "3": "tokenized_stock",
    "4": "tokenized_commodity",
}
SUPPORTED_CLASSES = {
    "crypto",
    "tokenized_stock",
    "tokenized_commodity",
    "tokenized_index_etf",
}
DDL = """CREATE TABLE IF NOT EXISTS instrument_class (
    symbol      TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL CHECK (asset_class IN
        ('crypto','tokenized_stock','tokenized_commodity','tokenized_index_etf')),
    source      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)"""


def _desired_class(category: Any, existing_class: str | None) -> str | None:
    official = OFFICIAL_CATEGORY_TO_CLASS.get(str(category or "").strip())
    if official == "tokenized_stock" and existing_class == "tokenized_index_etf":
        return existing_class
    return official


def plan_asset_class_sync(
    connection: sqlite3.Connection,
    instruments: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build an auditable sync plan without mutating the database."""
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='instrument_class'"
    ).fetchone()
    existing = (
        {
            str(symbol): (str(asset_class), str(source))
            for symbol, asset_class, source in connection.execute(
                "SELECT symbol,asset_class,source FROM instrument_class"
            )
        }
        if has_table else {}
    )
    inserts: list[dict[str, str]] = []
    updates: list[dict[str, str]] = []
    manual_conflicts: list[dict[str, str]] = []
    unsupported: list[dict[str, str]] = []
    seen: set[str] = set()
    unchanged = 0

    for instrument in instruments:
        symbol = str(instrument.get("instId") or "").strip().upper()
        if not symbol.endswith("-USDT-SWAP") or symbol in seen:
            continue
        seen.add(symbol)
        category = str(instrument.get("instCategory") or "").strip()
        current_class, current_source = existing.get(symbol, (None, None))
        desired = _desired_class(category, current_class)
        if desired is None:
            unsupported.append({
                "symbol": symbol,
                "inst_category": category or "missing",
            })
            continue
        if desired not in SUPPORTED_CLASSES:
            raise ValueError(f"unsupported local asset class: {desired}")
        if current_class is None:
            inserts.append({
                "symbol": symbol,
                "asset_class": desired,
                "source": "official_inst_category",
                "inst_category": category,
            })
            continue
        if current_class == desired:
            unchanged += 1
            continue
        if current_source == "manual":
            manual_conflicts.append({
                "symbol": symbol,
                "manual_class": current_class,
                "official_class": desired,
                "inst_category": category,
            })
            continue
        updates.append({
            "symbol": symbol,
            "old_class": current_class,
            "old_source": current_source,
            "asset_class": desired,
            "source": "official_inst_category",
            "inst_category": category,
        })

    return {
        "official_instruments": len(seen),
        "insert_count": len(inserts),
        "update_count": len(updates),
        "unchanged_count": unchanged,
        "manual_conflict_count": len(manual_conflicts),
        "unsupported_count": len(unsupported),
        "inserts": inserts,
        "updates": updates,
        "manual_conflicts": manual_conflicts,
        "unsupported": unsupported,
    }


def sync_asset_classes(
    connection: sqlite3.Connection,
    instruments: Iterable[dict[str, Any]],
    *,
    apply: bool = False,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Plan or atomically apply official classifications on one connection."""
    rows = list(instruments)
    if apply:
        connection.execute(DDL)
    plan = plan_asset_class_sync(connection, rows)
    if not apply:
        return {**plan, "dry_run": True, "applied": False}

    stamp = updated_at or datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        connection.executemany(
            "INSERT OR IGNORE INTO instrument_class"
            "(symbol,asset_class,source,updated_at) VALUES (?,?,?,?)",
            [
                (row["symbol"], row["asset_class"], row["source"], stamp)
                for row in plan["inserts"]
            ],
        )
        connection.executemany(
            "UPDATE instrument_class SET asset_class=?,source=?,updated_at=? "
            "WHERE symbol=? AND source<>'manual'",
            [
                (row["asset_class"], row["source"], stamp, row["symbol"])
                for row in plan["updates"]
            ],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {**plan, "dry_run": False, "applied": True}
