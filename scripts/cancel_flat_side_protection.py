# -*- coding: utf-8 -*-
"""Owner-run sweep of reduceOnly protection left on flat symbol/posSides.

An exchange-side SL fill leaves the independent fixed TP live, and the next
position on that symbol/posSide inherits it (2026-09-12 00:00: 34 such rows;
UNI's 23:39 position inherited the 22:08 TP).  open_position cancels these
right before each fresh OPEN; this CLI clears the ones on sides that stay flat.

Default is a read-only report.  ``--apply`` cancels through
`order_executor.sweep_flat_side_protection`, which re-reads each symbol and the
positions before cancelling and never touches a side that holds a position on
the exchange or still shows one in the ledger.  Refuses (rc=3) while a live
runner stage is in flight.  The sentinel config/stale_protection_cleanup.off
disables --apply (and the pre-open cleanup).

usage:
  pwsh -NoProfile -File scripts/run_okx_python.ps1 scripts/cancel_flat_side_protection.py
      [--apply] [--lookback-days 14] [--max-symbols 8] [--max-cancels 12]
      [--json-out PATH]
exit: 0 ok | 1 sweep failed or incomplete | 2 ledger unreadable | 3 runner active
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for module_path in (ROOT, ROOT / "collectors", ROOT / "scripts"):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

from core import order_executor as oe  # noqa: E402
from live_reconcile_monitor import active_runner  # noqa: E402

CST = timezone(timedelta(hours=8))


def ledger_symbols(db_root: Path, lookback_days: float) -> list[str]:
    """Symbols traded in the lookback window (live_trades.db, read-only).

    The account-wide algo listing holds only the newest 100 rows per ordType;
    when it is full, the sweep lists these symbols one by one to reach older
    rows.
    """
    since = (datetime.now(CST) - timedelta(days=lookback_days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    path = (Path(db_root) / "live_trades.db").resolve()
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM trades WHERE ts >= ? ORDER BY symbol",
            (since,)).fetchall()
    finally:
        con.close()
    return [str(row[0]) for row in rows if row[0]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", choices=("live",), default="live")
    ap.add_argument("--db-root", default=str(oe.DEFAULT_DB_ROOT))
    ap.add_argument("--apply", action="store_true",
                    help="cancel the rows (default: read-only report)")
    ap.add_argument("--lookback-days", type=float, default=14.0)
    ap.add_argument("--max-symbols", type=int, default=8)
    ap.add_argument("--max-cancels", type=int, default=12)
    ap.add_argument("--json-out")
    args = ap.parse_args(argv)

    def emit(report: dict) -> None:
        text = json.dumps(report, ensure_ascii=False, indent=1)
        if args.json_out:
            Path(args.json_out).write_text(text, encoding="utf-8")
        print(text)

    active = active_runner(args.profile)
    if active:
        emit({"ok": False, "refused": f"{args.profile}_runner_active",
              "active": active})
        return 3
    try:
        symbols = ledger_symbols(Path(args.db_root), args.lookback_days)
    except (OSError, sqlite3.Error) as exc:
        emit({"ok": False,
              "error": f"ledger_unreadable:{type(exc).__name__}: {exc}"})
        return 2
    report = oe.sweep_flat_side_protection(
        args.profile, Path(args.db_root), apply=args.apply,
        symbols=tuple(symbols), max_symbols=args.max_symbols,
        max_cancels=args.max_cancels)
    report["ledger_symbols"] = len(symbols)
    report["lookback_days"] = args.lookback_days
    emit(report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
