from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fetch_official_contract_history as history  # noqa: E402


UTC = timezone.utc


class OfficialContractHistoryTests(unittest.TestCase):
    def test_windows_are_bounded_below_api_page_limit(self) -> None:
        start = datetime(2026, 8, 1, tzinfo=UTC)
        end = datetime(2026, 8, 10, tzinfo=UTC)
        windows = history.build_windows(
            start, end, period="1H", window_bars=96)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0], (
            start, datetime(2026, 8, 5, tzinfo=UTC)))
        self.assertEqual(windows[-1][1], end)
        with self.assertRaisesRegex(ValueError, "between 1 and 99"):
            history.build_windows(
                start, end, period="1H", window_bars=100)

    def test_symbol_loader_deduplicates_panel_and_explicit_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            panel = Path(temp) / "panel.csv"
            with panel.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["symbol", "x"])
                writer.writeheader()
                writer.writerows([
                    {"symbol": "ETH-USDT-SWAP", "x": 1},
                    {"symbol": "BTC-USDT-SWAP", "x": 2},
                    {"symbol": "ETH-USDT-SWAP", "x": 3},
                ])
            symbols = history.load_symbols(
                symbols=["BTC-USDT-SWAP"], panel_csv=panel)
        self.assertEqual(symbols, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

    def test_fetch_writes_only_isolated_exact_common_rows(self) -> None:
        symbol = "BTC-USDT-SWAP"
        start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
        timestamps = [
            int(datetime(2026, 8, 1, hour, tzinfo=UTC).timestamp() * 1000)
            for hour in range(3)
        ]
        seen_kwargs: dict[str, dict] = {}

        def oi_fetch(symbols, **kwargs):
            seen_kwargs["oi"] = kwargs
            kwargs["outcomes"].update({s: {"ok": True} for s in symbols})
            return {
                s: [
                    *[[str(ts), "10", "1", "1000"] for ts in timestamps],
                    [str(int(end.timestamp() * 1000)), "11", "1", "1100"],
                ]
                for s in symbols
            }

        def taker_fetch(symbols, **kwargs):
            seen_kwargs["taker"] = kwargs
            kwargs["outcomes"].update({s: {"ok": True} for s in symbols})
            return {
                s: [
                    *[[str(ts), "400", "600"] for ts in timestamps],
                    [str(int(end.timestamp() * 1000)), "500", "500"],
                ]
                for s in symbols
            }

        def ratio_fetch(symbols, **kwargs):
            seen_kwargs["ratio"] = kwargs
            kwargs["outcomes"].update({s: {"ok": True} for s in symbols})
            return {
                s: [
                    *[[str(ts), "1.5"] for ts in timestamps],
                    [str(int(end.timestamp() * 1000)), "1.4"],
                ]
                for s in symbols
            }

        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(
                history,
                "fetch_contract_open_interest_history_batch_sync",
                side_effect=oi_fetch,
            ),
            mock.patch.object(
                history,
                "fetch_contract_taker_volumes_batch_sync",
                side_effect=taker_fetch,
            ),
            mock.patch.object(
                history,
                "fetch_contract_long_short_ratios_batch_sync",
                side_effect=ratio_fetch,
            ),
        ):
            output = Path(temp) / "research" / "history.db"
            manifest = history.fetch_history(
                output_db=output,
                symbols=[symbol],
                start=start,
                end=end,
                period="1H",
                window_bars=3,
                now_utc=lambda: datetime(2026, 8, 13, tzinfo=UTC),
            )
            saved_manifest = json.loads(
                output.with_suffix(".manifest.json").read_text(encoding="utf-8"))
            con = sqlite3.connect(output)
            try:
                counts = [
                    con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "open_interest", "taker_volume", "long_short_ratio",
                    )
                ]
                audit = con.execute(
                    "SELECT COUNT(*),SUM(transport_ok),SUM(invalid_rows) "
                    "FROM request_audit"
                ).fetchone()
            finally:
                con.close()

        self.assertEqual(counts, [3, 3, 3])
        self.assertEqual(audit, (3, 3, 0))
        self.assertEqual(manifest["rows"]["common"], 3)
        self.assertEqual(manifest["requests"]["invalid_rows"], 0)
        self.assertEqual(
            manifest["requests"]["outside_window_rows_filtered"], 3)
        self.assertTrue(manifest["research_gate"][
            "ready_for_retrospective_feature_diagnostics"])
        self.assertEqual(saved_manifest["run_id"], manifest["run_id"])
        self.assertEqual(
            seen_kwargs["oi"]["begin_ms"], timestamps[0] - 1)
        self.assertEqual(
            seen_kwargs["oi"]["end_ms"],
            int(end.timestamp() * 1000),
        )
        self.assertEqual(seen_kwargs["taker"]["unit"], "2")

    def test_transport_failure_is_preserved_and_gate_stays_closed(self) -> None:
        symbol = "BTC-USDT-SWAP"
        start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 8, 1, 1, 0, tzinfo=UTC)
        ts = int(start.timestamp() * 1000)

        def good_oi(symbols, **kwargs):
            kwargs["outcomes"][symbol] = {"ok": True}
            return {symbol: [[str(ts), "10", "1", "1000"]]}

        def bad_taker(symbols, **kwargs):
            kwargs["outcomes"][symbol] = {
                "ok": False,
                "error_type": "RuntimeError",
                "error": "network unavailable",
            }
            return {symbol: []}

        def good_ratio(symbols, **kwargs):
            kwargs["outcomes"][symbol] = {"ok": True}
            return {symbol: [[str(ts), "1.2"]]}

        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(
                history,
                "fetch_contract_open_interest_history_batch_sync",
                side_effect=good_oi,
            ),
            mock.patch.object(
                history,
                "fetch_contract_taker_volumes_batch_sync",
                side_effect=bad_taker,
            ),
            mock.patch.object(
                history,
                "fetch_contract_long_short_ratios_batch_sync",
                side_effect=good_ratio,
            ),
        ):
            manifest = history.fetch_history(
                output_db=Path(temp) / "history.db",
                symbols=[symbol],
                start=start,
                end=end,
                now_utc=lambda: datetime(2026, 8, 13, tzinfo=UTC),
            )
        self.assertEqual(manifest["status"], "partial_transport_failure")
        self.assertEqual(manifest["requests"]["transport_failed"], 1)
        self.assertFalse(manifest["research_gate"][
            "ready_for_retrospective_feature_diagnostics"])

    def test_production_db_target_is_rejected_before_network(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "outside the production db directory"
        ):
            history.fetch_history(
                output_db=ROOT / "db" / "must-not-write.db",
                symbols=["BTC-USDT-SWAP"],
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 2, tzinfo=UTC),
                now_utc=lambda: datetime(2026, 8, 13, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
