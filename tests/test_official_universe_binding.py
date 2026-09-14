from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "collectors", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collect_positioning_current as positioning  # noqa: E402
import fast_collect  # noqa: E402


CYCLE = "2026-08-18T01:00"
SOURCE = "okx_public_instruments_live_usdt_linear_swap"


def _snapshot_database(root: Path) -> Path:
    db_root = root / "db"
    db_root.mkdir(parents=True)
    path = db_root / "market.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE tick_snapshots("
        "ts TEXT,symbol TEXT,last REAL,vol24h REAL);"
        "CREATE TABLE instruments_cache(instId TEXT,ctVal REAL);"
        "CREATE TABLE official_instrument_snapshot_runs("
        "cycle_id TEXT PRIMARY KEY,collected_ts_utc TEXT,"
        "symbol_count INTEGER,payload_sha256 TEXT,complete INTEGER,source TEXT);"
        "CREATE TABLE official_instrument_snapshot_rows("
        "cycle_id TEXT,symbol TEXT,list_time_utc TEXT,state TEXT,"
        "settle_ccy TEXT,ct_type TEXT,inst_category TEXT,ct_val REAL,lot_sz REAL);"
    )
    connection.executemany(
        "INSERT INTO tick_snapshots VALUES(?,?,?,?)",
        [
            ("2026-08-17T17:00:02Z", "BTC-USDT-SWAP", 60_000.0, 10.0),
            ("2026-08-17T17:00:02Z", "TINY-USDT-SWAP", 1.0, 2.0),
        ],
    )
    connection.executemany(
        "INSERT INTO instruments_cache VALUES(?,?)",
        [("BTC-USDT-SWAP", 1.0), ("TINY-USDT-SWAP", 1.0)],
    )
    snapshot_rows = [
        {
            "symbol": symbol,
            "list_time_utc": "2026-08-17T17:00:00Z",
            "state": "live",
            "settle_ccy": "USDT",
            "ct_type": "linear",
            "inst_category": "1",
            "ct_val": 1.0,
            "lot_sz": 0.1,
        }
        for symbol in (
            "BTC-USDT-SWAP", "MOONSHOT-USDT-SWAP", "TINY-USDT-SWAP",
        )
    ]
    payload_sha256 = hashlib.sha256(json.dumps(
        snapshot_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO official_instrument_snapshot_runs VALUES(?,?,?,?,?,?)",
        (CYCLE, "2026-08-17T17:00:00Z", 3, payload_sha256, 1, SOURCE),
    )
    connection.executemany(
        "INSERT INTO official_instrument_snapshot_rows "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (
                CYCLE, symbol, "2026-08-17T17:00:00Z", "live",
                "USDT", "linear", "1", 1.0, 0.1,
            )
            for symbol in (row["symbol"] for row in snapshot_rows)
        ],
    )
    connection.commit()
    connection.close()
    return path


class OfficialUniverseBindingTests(unittest.TestCase):
    def test_positioning_uses_same_slot_official_universe_when_ticker_lags(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = _snapshot_database(Path(temporary))
            connection = sqlite3.connect(path)
            try:
                selected, binding = positioning.select_current_official_symbols(
                    connection, CYCLE, 10,
                )
            finally:
                connection.close()
        self.assertEqual(
            set(selected),
            {"BTC-USDT-SWAP", "MOONSHOT-USDT-SWAP", "TINY-USDT-SWAP"},
        )
        self.assertEqual(selected[0], "BTC-USDT-SWAP")
        self.assertEqual(
            binding["official_without_ticker_symbols"],
            ["MOONSHOT-USDT-SWAP"],
        )
        self.assertTrue(binding["exact_set_match"])
        self.assertFalse(binding["historical_fallback"])

    def test_positioning_fails_closed_without_same_slot_snapshot(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            "CREATE TABLE official_instrument_snapshot_runs("
            "cycle_id TEXT,symbol_count INTEGER,payload_sha256 TEXT,"
            "complete INTEGER,source TEXT);"
            "CREATE TABLE official_instrument_snapshot_rows("
            "cycle_id TEXT,symbol TEXT,state TEXT,settle_ccy TEXT,ct_type TEXT);"
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "snapshot missing"):
                positioning.select_current_official_symbols(
                    connection, CYCLE, 10,
                )
        finally:
            connection.close()

    def test_contract_alignment_detects_exact_missing_official_symbol(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = _snapshot_database(Path(temporary))
            db_root = path.parent
            collect_step = {
                "payload": {
                    "official_instrument_snapshot": {
                        "complete": True,
                        "symbol_count": 3,
                    }
                }
            }
            primary = {
                "payload": {
                    "selected": ["BTC-USDT-SWAP", "TINY-USDT-SWAP"]
                }
            }
            mismatch = fast_collect._contract_official_universe_alignment(
                db_root, CYCLE, collect_step, primary,
            )
            aligned = fast_collect._contract_official_universe_alignment(
                db_root,
                CYCLE,
                collect_step,
                {
                    "payload": {
                        "selected": [
                            "BTC-USDT-SWAP",
                            "MOONSHOT-USDT-SWAP",
                            "TINY-USDT-SWAP",
                        ]
                    }
                },
            )
        self.assertTrue(mismatch["required_recovery"])
        self.assertEqual(
            mismatch["missing_symbols"], ["MOONSHOT-USDT-SWAP"]
        )
        self.assertFalse(aligned["required_recovery"])
        self.assertEqual(aligned["reason"], "aligned")

    def test_recovery_is_authoritative_when_successful_primary_was_misscoped(self):
        base = [
            {"name": "collect_data", "ok": True, "payload": {}},
            {"name": "live_account_check", "ok": True, "payload": {}},
            {
                "name": "contract_statistics",
                "ok": True,
                "payload": {"degraded": False},
                "official_universe_alignment": {"required_recovery": True},
            },
            {"name": "market_features", "ok": True, "payload": {}},
        ]
        recovered = {
            "name": "contract_statistics_recovery",
            "ok": True,
            "payload": {"degraded": False},
        }
        failed = {
            "name": "contract_statistics_recovery",
            "ok": False,
            "payload": {"degraded": True},
        }
        self.assertEqual(
            fast_collect._collection_status([*base[:-1], recovered, base[-1]]),
            "ok",
        )
        self.assertEqual(
            fast_collect._collection_status([*base[:-1], failed, base[-1]]),
            "degraded",
        )
        self.assertEqual(fast_collect._collection_status(base), "degraded")

    def test_fast_main_runs_recovery_for_successful_but_misscoped_primary(self):
        calls: list[str] = []

        def fake_run(name, _script, _args, _timeout):
            calls.append(name)
            payload: dict = {}
            if name == "collect_data":
                payload = {
                    "degraded": False,
                    "official_instrument_snapshot": {
                        "complete": True,
                        "symbol_count": 3,
                    },
                    "quality": {
                        "expected": 3,
                        "tickers": 3,
                        "ticker_coverage": 1.0,
                        "candle_coverage": 2 / 3,
                        "candle_transport": {
                            "contract_version": 1,
                            "missing_symbols": 1,
                            "error_types": {"TimeoutError": 1},
                        },
                    },
                }
            elif name == "contract_statistics":
                payload = {
                    "degraded": False,
                    "selected": ["BTC-USDT-SWAP", "TINY-USDT-SWAP"],
                }
            elif name == "contract_statistics_recovery":
                payload = {
                    "degraded": False,
                    "final_direct_coverage_rate": 1.0,
                }
            return {
                "name": name,
                "ok": True,
                "rc": 0,
                "dur_s": 0.0,
                "payload": payload,
                "stderr_tail": "",
            }

        alignment = {
            "checked": True,
            "required_recovery": True,
            "reason": "same_slot_universe_mismatch",
            "official_symbols": 3,
            "primary_selected_symbols": 2,
            "missing_symbols": ["MOONSHOT-USDT-SWAP"],
            "extra_symbols": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            stream = io.StringIO()
            argv = [
                "fast_collect.py",
                "--db-root", str(Path(temporary) / "db"),
                "--cycle", "2026-08-18T01:15",
                "--no-universe-shadow",
                "--no-model-shadow",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(fast_collect, "run_step", side_effect=fake_run),
                mock.patch.object(
                    fast_collect,
                    "_contract_official_universe_alignment",
                    return_value=alignment,
                ),
                mock.patch.object(fast_collect.ledger, "init_ledger"),
                mock.patch.object(fast_collect.ledger, "record_collection"),
                mock.patch.object(fast_collect, "_nudge_mod", None),
                mock.patch.object(sys, "stdout", stream),
            ):
                self.assertEqual(fast_collect.main(), 0)
        self.assertIn("contract_statistics_recovery", calls)
        output = json.loads(stream.getvalue().strip().splitlines()[-1])
        self.assertEqual(output["status"], "ok")
        self.assertEqual(
            output["data_quality"]["candle_transport"]["missing_symbols"],
            1,
        )
        self.assertEqual(
            output["data_quality"][
                "contract_official_universe_alignment"
            ]["missing_symbol_count"],
            1,
        )
        self.assertTrue(
            output["data_quality"][
                "contract_official_universe_alignment"
            ]["required_recovery"]
        )
        self.assertTrue(any(
            "same-slot official universe" in warning
            for warning in output["warnings"]
        ))


if __name__ == "__main__":
    unittest.main()
