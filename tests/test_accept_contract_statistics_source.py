from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import accept_contract_statistics_source as acceptance  # noqa: E402
import apply_contract_statistics_schema as schema  # noqa: E402
import collect_market_features as collector  # noqa: E402


class ContractStatisticsAcceptanceTests(unittest.TestCase):
    def test_main_rejects_non_quarter_cycle_before_source_or_target_access(self) -> None:
        with (
            mock.patch.object(acceptance, "_universe") as universe,
            mock.patch("builtins.print") as output,
        ):
            rc = acceptance.main([
                "--source-db", "missing.db",
                "--target-db", "unused.db",
                "--json-out", "unused.json",
                "--cycle-id", "2026-08-12T10:35",
            ])
        self.assertEqual(rc, 2)
        universe.assert_not_called()
        self.assertIn("15m boundary", output.call_args.args[0])

    def test_audit_checks_exact_universe_and_ratio_algebra(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute(schema.DDL)
        rows = [
            (
                "2026-08-11T20:00:00Z", "2026-08-11T20:01:00Z",
                "2026-08-12T04:15", symbol, "15m",
                100.0, 10.0, 1000.0, 40.0, 60.0, 0.6, "{}",
                collector.CONTRACT_STATS_SOURCE,
            )
            for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
        ]
        collector.write_contract_statistics_rows(con, rows)
        con.commit()

        passed = acceptance._audit(
            con,
            ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            "2026-08-12T04:15",
        )
        self.assertEqual(passed["status"], "PASSED")
        self.assertEqual(passed["coverage_rate"], 1.0)
        self.assertEqual(passed["direct_coverage_rate"], 1.0)
        self.assertEqual(passed["carry_forward_rate"], 0.0)
        self.assertEqual(passed["method_counts"], {"rubik_common_bucket": 2})

        con.execute(
            "UPDATE market_contract_statistics SET taker_buy_ratio=0.5 "
            "WHERE symbol='ETH-USDT-SWAP'")
        con.commit()
        failed = acceptance._audit(
            con,
            ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
            "2026-08-12T04:15",
        )
        con.close()
        self.assertEqual(failed["status"], "NOT_MET")
        self.assertIn("SOL-USDT-SWAP", failed["missing_symbols"])
        self.assertIn("ETH-USDT-SWAP:taker_ratio", failed["invalid_rows"])

    def test_latest_prior_rows_is_read_only_one_per_symbol_and_excludes_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "market.db"
            con = sqlite3.connect(db_path)
            con.execute(schema.DDL)
            rows = [
                (
                    "2026-08-11T19:45:00Z", "2026-08-11T20:01:00Z",
                    "2026-08-12T04:00", "BTC-USDT-SWAP", "15m",
                    90.0, 9.0, 900.0, 40.0, 60.0, 0.6, "{}",
                    collector.CONTRACT_STATS_SOURCE,
                ),
                (
                    "2026-08-11T20:00:00Z", "2026-08-11T20:16:00Z",
                    "2026-08-12T04:15", "BTC-USDT-SWAP", "15m",
                    100.0, 10.0, 1000.0, 40.0, 60.0, 0.6, "{}",
                    collector.CONTRACT_STATS_SOURCE,
                ),
                (
                    "2026-08-11T20:15:00Z", "2026-08-11T20:31:00Z",
                    "2026-08-12T04:30", "BTC-USDT-SWAP", "15m",
                    110.0, 11.0, 1100.0, 40.0, 60.0, 0.6, "{}",
                    collector.CONTRACT_STATS_SOURCE,
                ),
                (
                    "2026-08-11T20:15:00Z", "2026-08-11T20:32:00Z",
                    "2026-08-12T04:31", "ETH-USDT-SWAP", "15m",
                    200.0, 20.0, 2000.0, 40.0, 60.0, 0.6,
                    '{"method":"official_previous_batch_carry_forward"}',
                    collector.CONTRACT_STATS_SOURCE,
                ),
                (
                    "2026-08-11T20:00:00Z", "2026-08-11T20:16:00Z",
                    "2026-08-12T04:15", "ETH-USDT-SWAP", "15m",
                    200.0, 20.0, 2000.0, 40.0, 60.0, 0.6, "{}",
                    collector.CONTRACT_STATS_SOURCE,
                ),
            ]
            collector.write_contract_statistics_rows(con, rows)
            con.commit()
            before = con.execute(
                "SELECT COUNT(*) FROM market_contract_statistics").fetchone()[0]
            con.close()

            selected = acceptance._latest_prior_rows(
                db_path,
                ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                "2026-08-12T04:30",
            )

            self.assertEqual(len(selected), 2)
            self.assertEqual(
                {row[3]: row[2] for row in selected},
                {
                    "BTC-USDT-SWAP": "2026-08-12T04:15",
                    "ETH-USDT-SWAP": "2026-08-12T04:15",
                },
            )
            con = sqlite3.connect(db_path)
            after = con.execute(
                "SELECT COUNT(*) FROM market_contract_statistics").fetchone()[0]
            con.close()
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
