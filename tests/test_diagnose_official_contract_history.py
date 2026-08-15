from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diagnose_official_contract_history as diagnostic  # noqa: E402


UTC = timezone.utc


def _history_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE open_interest(
          symbol TEXT,period TEXT,ts_ms INTEGER,oi_contracts REAL,
          oi_ccy REAL,oi_usd REAL,raw_json TEXT,fetched_at_utc TEXT,
          PRIMARY KEY(symbol,period,ts_ms));
        CREATE TABLE taker_volume(
          symbol TEXT,period TEXT,ts_ms INTEGER,sell_volume_usd REAL,
          buy_volume_usd REAL,unit TEXT,raw_json TEXT,fetched_at_utc TEXT,
          PRIMARY KEY(symbol,period,ts_ms));
        CREATE TABLE long_short_ratio(
          symbol TEXT,period TEXT,ts_ms INTEGER,
          account_long_short_ratio REAL,raw_json TEXT,fetched_at_utc TEXT,
          PRIMARY KEY(symbol,period,ts_ms));
        """
    )
    symbol = "BTC-USDT-SWAP"
    for hour, oi in ((23, 900.0), (0, 1000.0), (1, 1100.0)):
        date = "2026-07-31" if hour == 23 else "2026-08-01"
        ts = int(datetime.fromisoformat(
            f"{date}T{hour:02d}:00:00+00:00").timestamp() * 1000)
        con.execute(
            "INSERT INTO open_interest VALUES(?,?,?,?,?,?,?,?)",
            (symbol, "1H", ts, 10.0, 1.0, oi, "[]", "2026-08-13T00:00:00Z"),
        )
        con.execute(
            "INSERT INTO taker_volume VALUES(?,?,?,?,?,?,?,?)",
            (symbol, "1H", ts, 400.0, 600.0, "2", "[]", "2026-08-13T00:00:00Z"),
        )
        con.execute(
            "INSERT INTO long_short_ratio VALUES(?,?,?,?,?,?)",
            (symbol, "1H", ts, 1.5, "[]", "2026-08-13T00:00:00Z"),
        )
    con.commit()
    con.close()


class OfficialContractHistoryDiagnosticTests(unittest.TestCase):
    def test_cross_section_top_k_is_outcome_free_and_deterministic(self) -> None:
        frame = pd.DataFrame({
            "obs_ts": pd.to_datetime([
                "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
                "2026-08-01T01:00:00Z",
                "2026-08-01T01:00:00Z",
                "2026-08-01T01:00:00Z",
            ], utc=True),
            "symbol": ["B", "A", "C", "A", "B", "C"],
            "horizon": ["1H"] * 6,
            "side": ["long"] * 6,
            "probability": [0.9, 0.9, 0.8, 0.95, 0.7, 0.6],
            "success": [0, 1, 1, 1, 0, 0],
            "signed_return_after_cost": [-0.1, 0.1, 0.1, 0.1, -0.1, -0.1],
        })
        metrics = diagnostic.cross_section_top_k_metrics(frame, (1, 3))
        # The 00:00 tie is resolved by symbol, so A wins without inspecting
        # outcomes; A also wins the second cycle.
        self.assertEqual(metrics[0]["n"], 2)
        self.assertEqual(metrics[0]["successes"], 2)
        self.assertEqual(metrics[0]["precision"], 1.0)
        self.assertEqual(metrics[1]["n"], 6)

    def test_exact_previous_hour_requires_publication_before_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "history.db"
            _history_db(database)
            history = diagnostic.load_common_history(database)
        panel = pd.DataFrame({
            "obs_id": [1, 2],
            "obs_ts": pd.to_datetime([
                "2026-08-01T01:00:02Z", "2026-08-01T02:00:02Z",
            ], utc=True),
            "decision_ts": pd.to_datetime([
                "2026-08-01T01:04:00Z", "2026-08-01T02:04:00Z",
            ], utc=True),
            "entry_ts": pd.to_datetime([
                "2026-08-01T01:04:30Z", "2026-08-01T02:15:00Z",
            ], utc=True),
            "symbol": ["BTC-USDT-SWAP", "BTC-USDT-SWAP"],
            "split": ["train", "test"],
        })
        attached, audit = diagnostic.attach_exact_previous_hour(
            panel, history, publication_buffer_minutes=5)
        self.assertEqual(
            attached.loc[0, "official_contract_expected_source_ts"],
            pd.Timestamp("2026-08-01T00:00:00Z"),
        )
        self.assertEqual(
            attached.loc[1, "official_contract_expected_source_ts"],
            pd.Timestamp("2026-08-01T01:00:00Z"),
        )
        self.assertEqual(
            attached["official_contract_stats_available"].tolist(),
            [0.0, 1.0],
        )
        self.assertEqual(
            attached.loc[1, "official_augmented_decision_ts"],
            pd.Timestamp("2026-08-01T02:05:00Z"),
        )
        self.assertTrue(pd.isna(
            attached.loc[0, "official_contract_oi_log_change_1h"]))
        self.assertFalse(pd.isna(
            attached.loc[1, "official_contract_oi_log_change_1h"]))
        self.assertEqual(audit["exact_source_rows"], 2)
        self.assertEqual(audit["valid_rows"], 1)
        self.assertEqual(audit["by_split"]["test"]["coverage_rate"], 1.0)

    def test_manifest_validation_fails_closed_on_transport_failure(self) -> None:
        payload = {
            "artifact_type": "isolated_okx_official_contract_history",
            "status": "complete",
            "requests": {"transport_failed": 1, "invalid_rows": 0},
            "safety": {"production_database_writes": 0, "order_calls": 0},
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "transport failures"
            ):
                diagnostic.load_history_manifest(path)

    def test_production_db_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "outside the production db directory"
        ):
            diagnostic._outside_production_db(
                ROOT / "db" / "market.db", label="history database")


if __name__ == "__main__":
    unittest.main()
