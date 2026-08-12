# -*- coding: utf-8 -*-
"""Audit local asset classes against OKX official ``instCategory`` metadata.

The audit separates row completeness from semantic correctness.  It reads
market.db in read-only mode, fetches current public instrument metadata, and
atomically writes only the requested quality JSON.  It never repairs rows,
dispatches work, changes thresholds, or places orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from _okx_http import fetch_instruments_sync


CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "db" / "market.db"
DEFAULT_OUTPUT = ROOT / "reports" / "quality" / "asset-class-coverage-audit.json"
OFFICIAL_COMPATIBLE_CLASSES = {
    "1": frozenset({"crypto"}),
    "3": frozenset({"tokenized_stock", "tokenized_index_etf"}),
    "4": frozenset({"tokenized_commodity"}),
}


def _official_universe(instruments: Iterable[dict[str, Any]]) -> dict[str, str]:
    output: dict[str, str] = {}
    for row in instruments:
        symbol = str(row.get("instId") or "").strip().upper()
        if (
            row.get("instType") == "SWAP"
            and row.get("settleCcy") == "USDT"
            and row.get("ctType") == "linear"
            and row.get("state") == "live"
            and symbol.endswith("-USDT-SWAP")
        ):
            output[symbol] = str(row.get("instCategory") or "").strip()
    return output


def audit_asset_class_coverage(
    market_db: Path,
    instruments: Iterable[dict[str, Any]],
    *,
    minimum_rate: float = 0.99,
) -> dict[str, Any]:
    if not 0 < minimum_rate <= 1:
        raise ValueError("minimum_rate must be in (0,1]")
    official = _official_universe(instruments)
    if not official:
        raise ValueError("official live linear USDT SWAP universe is empty")
    connection = sqlite3.connect(
        f"file:{market_db.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='instrument_class'"
        ).fetchone()
        if not has_table:
            raise ValueError("instrument_class table missing")
        symbols = sorted(official)
        placeholders = ",".join("?" for _ in symbols)
        local = {
            str(row["symbol"]): dict(row)
            for row in connection.execute(
                "SELECT symbol,asset_class,source,updated_at "
                f"FROM instrument_class WHERE symbol IN ({placeholders})",
                symbols,
            )
        }
    finally:
        connection.close()

    missing = sorted(set(official) - set(local))
    unsupported = sorted(
        symbol for symbol, category in official.items()
        if category not in OFFICIAL_COMPATIBLE_CLASSES
    )
    mismatches: list[dict[str, Any]] = []
    compatible = 0
    for symbol, category in sorted(official.items()):
        row = local.get(symbol)
        if row is None:
            continue
        allowed = OFFICIAL_COMPATIBLE_CLASSES.get(category, frozenset())
        if str(row["asset_class"]) in allowed:
            compatible += 1
        else:
            mismatches.append({
                "symbol": symbol,
                "official_inst_category": category or "missing",
                "local_asset_class": row["asset_class"],
                "local_source": row["source"],
                "local_updated_at": row["updated_at"],
                "allowed_local_classes": sorted(allowed),
            })

    total = len(official)
    row_rate = len(local) / total
    compatible_rate = compatible / total
    category_digest = hashlib.sha256(
        json.dumps(
            sorted(official.items()),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    checks = {
        "row_coverage_at_least_target": row_rate >= minimum_rate,
        "official_compatibility_at_least_target": compatible_rate >= minimum_rate,
        "no_unsupported_official_categories": not unsupported,
    }
    return {
        "schema_version": 1,
        "artifact_type": "official_asset_class_coverage_audit",
        "generated_at_cst": datetime.now(CST).isoformat(),
        "mode": "read_only",
        "market_db": str(market_db),
        "official_source": "OKX GET /api/v5/public/instruments instCategory",
        "official_category_semantics": {
            "1": "crypto",
            "3": "stocks",
            "4": "commodities",
            "5": "forex_unsupported_local_schema",
            "6": "bonds_unsupported_local_schema",
        },
        "official_universe_symbols": total,
        "official_category_counts": dict(sorted(Counter(official.values()).items())),
        "official_symbol_category_sha256": category_digest,
        "local_rows": len(local),
        "row_coverage_rate": row_rate,
        "official_compatible_symbols": compatible,
        "official_compatibility_rate": compatible_rate,
        "local_class_counts": dict(sorted(Counter(
            str(row["asset_class"]) for row in local.values()).items())),
        "local_source_counts": dict(sorted(Counter(
            str(row["source"]) for row in local.values()).items())),
        "missing_symbols": missing,
        "mismatches": mismatches,
        "unsupported_official_symbols": unsupported,
        "minimum_rate": minimum_rate,
        "checks": checks,
        "status": "PASSED" if all(checks.values()) else "NOT_MET",
        "production_database_writes": 0,
        "collector_triggered": False,
        "orders_placed": 0,
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-rate", type=float, default=0.99)
    args = parser.parse_args(argv)
    try:
        payload = audit_asset_class_coverage(
            args.market_db,
            fetch_instruments_sync("SWAP"),
            minimum_rate=args.minimum_rate,
        )
        _atomic_json(args.json_out, payload)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "production_database_writes": 0,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "ok": payload["status"] == "PASSED",
        "status": payload["status"],
        "official_universe": payload["official_universe_symbols"],
        "row_coverage": payload["row_coverage_rate"],
        "official_compatibility": payload["official_compatibility_rate"],
        "json_out": str(args.json_out),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0 if payload["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
