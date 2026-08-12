# -*- coding: utf-8 -*-
"""Build read-only, SHA-256-bound 15m/1H/4H evidence for one OPEN candidate."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from core.multitimeframe_gate import check_multitimeframe_readiness


def _atomic_json(path: Path, payload: dict) -> None:
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
    parser.add_argument("--db-root", type=Path, default=Path(r"./db"))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--out-file", type=Path, required=True)
    args = parser.parse_args(argv)
    result = check_multitimeframe_readiness(
        args.db_root, args.symbol, args.cycle_id)
    payload = {
        "ok": bool(result.get("ready")),
        "status": result.get("status"),
        "symbol": args.symbol,
        "cycle_id": args.cycle_id,
        "evidence_contract": result.get("evidence_contract"),
        "gaps": [
            {
                "timeframe": row.get("timeframe"),
                "classification": row.get("classification"),
                "raw_errors": row.get("raw_errors"),
                "indicator_errors": row.get("indicator_errors"),
            }
            for row in result.get("timeframes", [])
            if not row.get("ready")
        ],
        "error": result.get("error"),
        "mode": "read_only",
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    _atomic_json(args.out_file, payload)
    print(json.dumps({
        "ok": payload["ok"],
        "status": payload["status"],
        "symbol": args.symbol,
        "cycle_id": args.cycle_id,
        "evidence_hash": (
            (payload.get("evidence_contract") or {}).get("evidence_hash")
        ),
        "out_file": str(args.out_file),
        "production_database_writes": 0,
        "orders_placed": 0,
    }, ensure_ascii=False))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
