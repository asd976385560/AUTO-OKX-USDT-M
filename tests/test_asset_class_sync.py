from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import asset_class_sync  # noqa: E402
import audit_asset_class_coverage  # noqa: E402
import apply_asset_class_schema  # noqa: E402
from core import asset_class  # noqa: E402


class AssetClassSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(asset_class_sync.DDL)
        self.connection.executemany(
            "INSERT INTO instrument_class VALUES(?,?,?,?)",
            [
                ("BTC-USDT-SWAP", "crypto", "default_crypto", "old"),
                ("BOT-USDT-SWAP", "crypto", "default_crypto", "old"),
                ("SPY-USDT-SWAP", "tokenized_index_etf", "curated", "old"),
                ("F-USDT-SWAP", "tokenized_stock", "curated", "old"),
                ("LOCKED-USDT-SWAP", "crypto", "manual", "old"),
            ],
        )
        self.connection.commit()
        self.instruments = [
            {"instId": "BTC-USDT-SWAP", "instCategory": "1"},
            {"instId": "BOT-USDT-SWAP", "instCategory": "3"},
            {"instId": "SPY-USDT-SWAP", "instCategory": "3"},
            {"instId": "F-USDT-SWAP", "instCategory": "1"},
            {"instId": "POPMART-USDT-SWAP", "instCategory": "3"},
            {"instId": "XAU-USDT-SWAP", "instCategory": "4"},
            {"instId": "LOCKED-USDT-SWAP", "instCategory": "3"},
            {"instId": "EUR-USDT-SWAP", "instCategory": "5"},
        ]

    def tearDown(self) -> None:
        self.connection.close()

    def test_dry_run_is_non_mutating_and_reports_all_actions(self) -> None:
        result = asset_class_sync.sync_asset_classes(
            self.connection, self.instruments, apply=False)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["insert_count"], 2)
        self.assertEqual(result["update_count"], 2)
        self.assertEqual(result["manual_conflict_count"], 1)
        self.assertEqual(result["unsupported_count"], 1)
        self.assertIsNone(self.connection.execute(
            "SELECT asset_class FROM instrument_class WHERE symbol=?",
            ("POPMART-USDT-SWAP",),
        ).fetchone())

    def test_apply_corrects_non_manual_and_preserves_granular_etf_and_manual(self) -> None:
        result = asset_class_sync.sync_asset_classes(
            self.connection,
            self.instruments,
            apply=True,
            updated_at="2026-08-12 16:45:00",
        )
        self.assertTrue(result["applied"])
        values = {
            symbol: (asset_class, source, updated_at)
            for symbol, asset_class, source, updated_at
            in self.connection.execute("SELECT * FROM instrument_class")
        }
        self.assertEqual(
            values["BOT-USDT-SWAP"][:2],
            ("tokenized_stock", "official_inst_category"),
        )
        self.assertEqual(
            values["F-USDT-SWAP"][:2],
            ("crypto", "official_inst_category"),
        )
        self.assertEqual(
            values["POPMART-USDT-SWAP"][:2],
            ("tokenized_stock", "official_inst_category"),
        )
        self.assertEqual(
            values["XAU-USDT-SWAP"][:2],
            ("tokenized_commodity", "official_inst_category"),
        )
        self.assertEqual(
            values["SPY-USDT-SWAP"][:2],
            ("tokenized_index_etf", "curated"),
        )
        self.assertEqual(
            values["LOCKED-USDT-SWAP"][:2],
            ("crypto", "manual"),
        )

        second = asset_class_sync.sync_asset_classes(
            self.connection, self.instruments, apply=True)
        self.assertEqual(second["insert_count"], 0)
        self.assertEqual(second["update_count"], 0)
        self.assertEqual(second["manual_conflict_count"], 1)

    def test_independent_audit_separates_missing_and_wrong_class(self) -> None:
        instruments = [
            {
                "instId": "BTC-USDT-SWAP", "instCategory": "1",
                "instType": "SWAP", "settleCcy": "USDT",
                "ctType": "linear", "state": "live",
            },
            {
                "instId": "BOT-USDT-SWAP", "instCategory": "3",
                "instType": "SWAP", "settleCcy": "USDT",
                "ctType": "linear", "state": "live",
            },
            {
                "instId": "XAU-USDT-SWAP", "instCategory": "4",
                "instType": "SWAP", "settleCcy": "USDT",
                "ctType": "linear", "state": "live",
            },
        ]
        # Use a temporary on-disk backup because the auditor deliberately opens
        # its own read-only connection.
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            target = sqlite3.connect(path)
            self.connection.backup(target)
            target.execute(
                "UPDATE instrument_class SET asset_class='crypto' "
                "WHERE symbol='BOT-USDT-SWAP'"
            )
            target.commit()
            target.close()
            payload = audit_asset_class_coverage.audit_asset_class_coverage(
                path, instruments, minimum_rate=0.99)

        self.assertEqual(payload["official_universe_symbols"], 3)
        self.assertEqual(payload["local_rows"], 2)
        self.assertEqual(payload["missing_symbols"], ["XAU-USDT-SWAP"])
        self.assertEqual(
            [row["symbol"] for row in payload["mismatches"]],
            ["BOT-USDT-SWAP"],
        )
        self.assertEqual(payload["status"], "NOT_MET")

    def test_initial_seed_matches_official_same_ticker_semantics(self) -> None:
        for symbol in (
            "BOT", "BSP", "CBRS", "NET", "POPMART", "RIOT", "XIAOMI",
        ):
            self.assertEqual(
                apply_asset_class_schema.classify_base(symbol),
                ("tokenized_stock", "curated"),
            )
        for symbol in ("BEAT", "BILL", "CVX", "F", "OPG", "ROBO", "SLX", "SPX"):
            self.assertEqual(
                apply_asset_class_schema.classify_base(symbol),
                ("crypto", "default_crypto"),
            )

    def test_reader_cache_refreshes_after_external_wal_commit(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.db"
            writer = sqlite3.connect(path)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(asset_class_sync.DDL)
            writer.execute(
                "INSERT INTO instrument_class VALUES(?,?,?,?)",
                ("BOT-USDT-SWAP", "crypto", "default_crypto", "old"),
            )
            writer.commit()
            asset_class._CACHE.clear()
            self.assertEqual(
                asset_class.classify("BOT", root),
                ("crypto", "default_crypto"),
            )
            writer.execute(
                "UPDATE instrument_class SET asset_class=?,source=?,updated_at=? "
                "WHERE symbol=?",
                (
                    "tokenized_stock", "official_inst_category",
                    "2026-08-12 16:45:05", "BOT-USDT-SWAP",
                ),
            )
            writer.commit()
            self.assertEqual(
                asset_class.classify("BOT", root),
                ("tokenized_stock", "official_inst_category"),
            )
            writer.close()
            asset_class._CACHE.clear()


if __name__ == "__main__":
    unittest.main()
