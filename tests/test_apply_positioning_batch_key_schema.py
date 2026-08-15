from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import apply_positioning_batch_key_schema as migration  # noqa: E402
import collect_market_features  # noqa: E402


LEGACY_DDL = """
CREATE TABLE market_positioning (
    ts TEXT NOT NULL,
    collected_ts TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT '1H',
    long_ratio REAL,
    short_ratio REAL,
    long_short_ratio REAL,
    raw TEXT,
    source TEXT NOT NULL DEFAULT 'okx_cli_top_long_short',
    PRIMARY KEY (ts, symbol, timeframe)
);
CREATE INDEX idx_positioning_cycle ON market_positioning(cycle_id);
CREATE INDEX idx_positioning_symbol_ts ON market_positioning(symbol, ts);
"""


def _row(
    cycle_id: str,
    *,
    ts: str = "2026-08-12T18:00:00Z",
    long_ratio: float = 0.6,
) -> tuple:
    short_ratio = 1.0 - long_ratio
    return (
        ts,
        "2026-08-12T18:31:00Z",
        cycle_id,
        "BTC-USDT-SWAP",
        "1H",
        long_ratio,
        short_ratio,
        long_ratio / short_ratio,
        "{}",
        "okx_rest_contract_long_short_ratio",
    )


class PositioningBatchKeySchemaTests(unittest.TestCase):
    def _legacy_db(self, root: Path, rows: list[tuple]) -> Path:
        path = root / "market.db"
        connection = sqlite3.connect(path)
        connection.executescript(LEGACY_DDL)
        connection.executemany(
            "INSERT INTO market_positioning VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        connection.commit()
        connection.close()
        return path

    def test_plan_backup_migration_and_cross_cycle_immutability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = _row("2026-08-13T02:30")
            db_path = self._legacy_db(root, [original])

            connection = sqlite3.connect(db_path)
            with self.assertRaisesRegex(RuntimeError, "unsafe primary key"):
                collect_market_features.write_positioning_rows(
                    connection, [_row("2026-08-13T03:00")])
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM market_positioning").fetchone()[0],
            )
            connection.close()

            output = io.StringIO()
            with redirect_stdout(output):
                rc = migration.main(["--db", str(db_path)])
            planned = json.loads(output.getvalue())
            self.assertEqual(0, rc)
            self.assertEqual("plan-only", planned["action"])
            self.assertEqual(0, planned["target_key_duplicates"])
            self.assertFalse(planned["historical_backfill"])

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                migration.main(["--db", str(db_path), "--apply"])
            self.assertEqual(2, caught.exception.code)
            self.assertIn("backup-dir", stderr.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                rc = migration.main([
                    "--db", str(db_path),
                    "--apply",
                    "--backup-dir", str(root / "backups"),
                ])
            applied = json.loads(output.getvalue())
            self.assertEqual(0, rc)
            self.assertEqual("applied", applied["action"])
            self.assertTrue(applied["validation"]["passed"])
            backup = Path(applied["backup"])
            self.assertTrue(backup.is_file())

            backup_connection = sqlite3.connect(backup)
            backup_info = backup_connection.execute(
                "PRAGMA table_info(market_positioning)").fetchall()
            backup_pk = tuple(
                row[1] for row in sorted(
                    (row for row in backup_info if row[5]),
                    key=lambda row: row[5],
                )
            )
            backup_rows = backup_connection.execute(
                "SELECT COUNT(*) FROM market_positioning").fetchone()[0]
            backup_connection.close()
            self.assertEqual(migration.LEGACY_PRIMARY_KEY, backup_pk)
            self.assertEqual(1, backup_rows)

            same_source_timestamp = _row("2026-08-13T03:00")
            connection = sqlite3.connect(db_path)
            wrote = collect_market_features.write_positioning_rows(
                connection, [same_source_timestamp])
            connection.commit()
            self.assertEqual(1, wrote)
            stored = connection.execute(
                "SELECT cycle_id,ts FROM market_positioning ORDER BY cycle_id"
            ).fetchall()
            self.assertEqual([
                ("2026-08-13T02:30", "2026-08-12T18:00:00Z"),
                ("2026-08-13T03:00", "2026-08-12T18:00:00Z"),
            ], stored)
            with self.assertRaisesRegex(RuntimeError, "immutable batch conflict"):
                collect_market_features.write_positioning_rows(
                    connection,
                    [_row("2026-08-13T03:00", long_ratio=0.7)],
                )
            connection.close()

            output = io.StringIO()
            with redirect_stdout(output):
                rc = migration.main([
                    "--db", str(db_path), "--apply",
                    "--backup-dir", str(root / "backups"),
                ])
            repeated = json.loads(output.getvalue())
            self.assertEqual(0, rc)
            self.assertEqual("none", repeated["action"])
            self.assertTrue(repeated["validation"]["passed"])

    def test_target_key_duplicates_fail_closed_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = _row("2026-08-13T02:30", ts="2026-08-12T17:00:00Z")
            second = _row("2026-08-13T02:30", ts="2026-08-12T18:00:00Z")
            db_path = self._legacy_db(root, [first, second])
            output = io.StringIO()
            with redirect_stdout(output):
                rc = migration.main([
                    "--db", str(db_path),
                    "--apply",
                    "--backup-dir", str(root / "backups"),
                ])
            result = json.loads(output.getvalue())
            self.assertEqual(2, rc)
            self.assertEqual(1, result["target_key_duplicates"])
            self.assertIn("pre-migration", result["error"])
            self.assertFalse((root / "backups").exists())


if __name__ == "__main__":
    unittest.main()
