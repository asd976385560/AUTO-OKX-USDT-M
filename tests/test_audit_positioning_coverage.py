from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_positioning_coverage as audit  # noqa: E402


def _database(root: Path, rows: list[tuple]) -> Path:
    path = root / "market.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT);
        CREATE TABLE market_positioning(
          ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,
          long_ratio REAL,short_ratio REAL,long_short_ratio REAL,source TEXT
        );
        """
    )
    tick_ts = "2026-08-11T19:00:02Z"
    connection.executemany(
        "INSERT INTO tick_snapshots VALUES(?,?)",
        [(tick_ts, symbol) for symbol in (
            "AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP")],
    )
    connection.executemany(
        "INSERT INTO market_positioning VALUES(?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return path


def _row(symbol: str, long_ratio: float = 0.6) -> tuple:
    short_ratio = 1.0 - long_ratio
    return (
        "2026-08-11T18:00:00Z", "2026-08-11T19:00:25Z",
        "2026-08-12T03:00", symbol, "1H", long_ratio, short_ratio,
        long_ratio / short_ratio, audit.DEFAULT_SOURCE,
    )


class PositioningCoverageAuditTests(unittest.TestCase):
    def test_complete_valid_batch_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _database(Path(tmp), [
                _row("AAA-USDT-SWAP"),
                _row("BBB-USDT-SWAP"),
                _row("CCC-USDT-SWAP"),
            ])
            result = audit.audit_positioning_coverage(
                path, now=datetime(2026, 8, 11, 20, tzinfo=timezone.utc))
        self.assertEqual("PASSED", result["status"])
        self.assertEqual(1.0, result["coverage_rate"])
        self.assertEqual([], result["invalid_rows"])

    def test_missing_symbol_is_in_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _database(Path(tmp), [
                _row("AAA-USDT-SWAP"), _row("BBB-USDT-SWAP")])
            result = audit.audit_positioning_coverage(
                path, now=datetime(2026, 8, 11, 20, tzinfo=timezone.utc))
        self.assertEqual("NOT_MET", result["status"])
        self.assertEqual(["CCC-USDT-SWAP"], result["missing_symbols"])
        self.assertEqual(0.666667, result["coverage_rate"])

    def test_invalid_ratio_cannot_count_as_covered(self) -> None:
        bad = list(_row("CCC-USDT-SWAP"))
        bad[7] = 99.0
        with tempfile.TemporaryDirectory() as tmp:
            path = _database(Path(tmp), [
                _row("AAA-USDT-SWAP"), _row("BBB-USDT-SWAP"), tuple(bad)])
            result = audit.audit_positioning_coverage(
                path, now=datetime(2026, 8, 11, 20, tzinfo=timezone.utc))
        self.assertEqual("NOT_MET", result["status"])
        self.assertEqual(0.666667, result["coverage_rate"])
        self.assertIn(
            "long_short_ratio_derivation_mismatch",
            result["invalid_rows"][0]["errors"],
        )


if __name__ == "__main__":
    unittest.main()
