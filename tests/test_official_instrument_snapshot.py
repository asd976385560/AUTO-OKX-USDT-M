from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import collect_data  # noqa: E402


def _instrument(symbol: str, *, ct_val: str = "0.01") -> dict:
    return {
        "instId": symbol,
        "listTime": "1786523400000",
        "state": "live",
        "settleCcy": "USDT",
        "ctType": "linear",
        "instCategory": "1",
        "ctVal": ct_val,
        "lotSz": "0.1",
    }


class OfficialInstrumentSnapshotTests(unittest.TestCase):
    def test_discovery_failure_returns_empty_and_preserves_root_cause(self) -> None:
        with mock.patch.object(
            collect_data,
            "_fetch_all_swap_instruments",
            side_effect=TimeoutError("official instruments timed out"),
        ):
            instruments, error = collect_data.discover_live_swap_instruments([])
        self.assertEqual(instruments, [])
        self.assertEqual(
            error, "TimeoutError: official instruments timed out")

    def test_insert_reuse_and_conflict_never_overwrite_first_snapshot(self) -> None:
        connection = sqlite3.connect(":memory:")
        first = [_instrument("BTC-USDT-SWAP"), _instrument("ETH-USDT-SWAP")]

        inserted = collect_data.freeze_official_instrument_snapshot(
            connection,
            first,
            cycle_id="2026-08-12T22:45",
            collected_ts_utc="2026-08-12T14:45:01Z",
        )
        reused = collect_data.freeze_official_instrument_snapshot(
            connection,
            list(reversed(first)),
            cycle_id="2026-08-12T22:45",
            collected_ts_utc="2026-08-12T14:45:30Z",
        )
        conflict = collect_data.freeze_official_instrument_snapshot(
            connection,
            [*first, _instrument("SOL-USDT-SWAP")],
            cycle_id="2026-08-12T22:45",
            collected_ts_utc="2026-08-12T14:45:45Z",
        )
        header = connection.execute(
            "SELECT symbol_count,payload_sha256,complete "
            "FROM official_instrument_snapshot_runs"
        ).fetchone()
        symbols = [
            row[0] for row in connection.execute(
                "SELECT symbol FROM official_instrument_snapshot_rows "
                "ORDER BY symbol"
            )
        ]
        connection.close()

        self.assertEqual(inserted["status"], "inserted")
        self.assertEqual(reused["status"], "reused")
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(header[0], 2)
        self.assertEqual(header[1], inserted["payload_sha256"])
        self.assertEqual(header[2], 1)
        self.assertEqual(symbols, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

    def test_invalid_or_duplicate_snapshot_fails_before_writing_header(self) -> None:
        connection = sqlite3.connect(":memory:")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            collect_data.freeze_official_instrument_snapshot(
                connection,
                [_instrument("BTC-USDT-SWAP"), _instrument("BTC-USDT-SWAP")],
                cycle_id="2026-08-12T22:45",
                collected_ts_utc="2026-08-12T14:45:01Z",
            )
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='official_instrument_snapshot_runs'"
        ).fetchone()
        connection.close()

        self.assertIsNone(table)

    def test_tampered_stored_rows_are_a_conflict_not_a_reuse(self) -> None:
        connection = sqlite3.connect(":memory:")
        first = [_instrument("BTC-USDT-SWAP"), _instrument("ETH-USDT-SWAP")]
        inserted = collect_data.freeze_official_instrument_snapshot(
            connection,
            first,
            cycle_id="2026-08-12T22:45",
            collected_ts_utc="2026-08-12T14:45:01Z",
        )
        connection.execute(
            "UPDATE official_instrument_snapshot_rows SET lot_sz=0.2 "
            "WHERE symbol='ETH-USDT-SWAP'"
        )
        connection.commit()

        result = collect_data.freeze_official_instrument_snapshot(
            connection,
            first,
            cycle_id="2026-08-12T22:45",
            collected_ts_utc="2026-08-12T14:45:30Z",
        )
        stored_lot = connection.execute(
            "SELECT lot_sz FROM official_instrument_snapshot_rows "
            "WHERE symbol='ETH-USDT-SWAP'"
        ).fetchone()[0]
        connection.close()

        self.assertEqual(result["status"], "conflict")
        self.assertNotEqual(
            result["stored_payload_sha256"],
            result["stored_observed_payload_sha256"],
        )
        self.assertEqual(result["stored_payload_sha256"],
                         inserted["payload_sha256"])
        self.assertEqual(stored_lot, 0.2)


if __name__ == "__main__":
    unittest.main()
