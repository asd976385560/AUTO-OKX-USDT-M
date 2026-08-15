# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "collectors"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collect_market_features as collector  # noqa: E402
import fast_collect  # noqa: E402
import recover_contract_statistics_current as recovery  # noqa: E402


CST = timezone(timedelta(hours=8))
CYCLE = "2026-08-14T00:15"
NOW = datetime(2026, 8, 14, 0, 18, tzinfo=CST)


def _direct_row(symbol: str, cycle: str = CYCLE) -> tuple:
    return (
        "2026-08-13T16:00:00Z",
        "2026-08-13T16:16:00Z",
        cycle,
        symbol,
        "15m",
        100.0,
        10.0,
        1000.0,
        40.0,
        60.0,
        0.6,
        json.dumps({"method": "rubik_common_bucket"}),
        collector.CONTRACT_STATS_SOURCE,
    )


def _carry_row(symbol: str, cycle: str = CYCLE) -> tuple:
    row = list(_direct_row(symbol, cycle))
    row[0] = "2026-08-13T15:45:00Z"
    row[11] = json.dumps({
        "method": collector.CONTRACT_STATS_CARRY_METHOD,
        "source_age_seconds": 1860.0,
    })
    return tuple(row)


class CurrentContractStatisticsRecoveryTests(unittest.TestCase):
    def _db_root(self, temporary: str, symbols: list[str]) -> Path:
        db_root = Path(temporary) / "db"
        db_root.mkdir()
        con = sqlite3.connect(db_root / "market.db")
        con.executescript(
            """
            CREATE TABLE official_instrument_snapshot_runs(
              cycle_id TEXT PRIMARY KEY,collected_ts_utc TEXT NOT NULL,
              symbol_count INTEGER NOT NULL,payload_sha256 TEXT NOT NULL,
              complete INTEGER NOT NULL,source TEXT NOT NULL);
            CREATE TABLE official_instrument_snapshot_rows(
              cycle_id TEXT NOT NULL,symbol TEXT NOT NULL,list_time_utc TEXT,
              state TEXT,settle_ccy TEXT,ct_type TEXT,inst_category TEXT,
              ct_val REAL,lot_sz REAL,PRIMARY KEY(cycle_id,symbol));
            CREATE TABLE market_contract_statistics(
              ts TEXT NOT NULL,collected_ts TEXT NOT NULL,cycle_id TEXT NOT NULL,
              symbol TEXT NOT NULL,timeframe TEXT NOT NULL,oi_contracts REAL NOT NULL,
              oi_ccy REAL NOT NULL,oi_usd REAL NOT NULL,taker_sell_usd REAL NOT NULL,
              taker_buy_usd REAL NOT NULL,taker_buy_ratio REAL,raw TEXT NOT NULL,
              source TEXT NOT NULL,PRIMARY KEY(cycle_id,symbol,timeframe,source));
            """
        )
        con.execute(
            "INSERT INTO official_instrument_snapshot_runs VALUES(?,?,?,?,?,?)",
            (
                CYCLE,
                "2026-08-13T16:15:02Z",
                len(symbols),
                "hash",
                1,
                recovery.EXPECTED_SNAPSHOT_SOURCE,
            ),
        )
        con.executemany(
            "INSERT INTO official_instrument_snapshot_rows VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (CYCLE, symbol, None, "live", "USDT", "linear", "1", 1, 1)
                for symbol in symbols
            ],
        )
        con.commit()
        con.close()
        return db_root

    def test_rejects_historical_future_and_unaligned_cycles(self) -> None:
        with self.assertRaisesRegex(ValueError, "historical/future"):
            recovery.require_current_natural_cycle(
                "2026-08-14T00:00", now=NOW
            )
        with self.assertRaisesRegex(ValueError, "natural 15-minute"):
            recovery.require_current_natural_cycle(
                "2026-08-14T00:17", now=NOW
            )

    def test_requests_only_non_direct_symbols_and_replaces_carries(self) -> None:
        symbols = [f"S{i:03d}-USDT-SWAP" for i in range(100)]
        missing = symbols[-2:]
        with tempfile.TemporaryDirectory() as temporary:
            db_root = self._db_root(temporary, symbols)
            con = sqlite3.connect(db_root / "market.db")
            collector.write_contract_statistics_rows(
                con,
                [
                    *[_direct_row(symbol) for symbol in symbols[:-2]],
                    *[_carry_row(symbol) for symbol in missing],
                ],
            )
            con.commit()
            con.close()

            fetched = [_direct_row(symbol) for symbol in missing]
            with (
                mock.patch.object(
                    recovery, "_fetch_once", return_value=(
                        fetched,
                        [],
                        {
                            "open_interest_ok": 2,
                            "taker_volume_ok": 2,
                            "open_interest_failed": 0,
                            "taker_volume_failed": 0,
                        },
                    )
                ) as fetch,
                mock.patch.object(recovery.time, "sleep") as sleep,
                mock.patch.object(
                    recovery.collector,
                    "utc_now_iso",
                    return_value="2026-08-13T16:18:00Z",
                ),
            ):
                result = recovery.recover(
                    db_root, CYCLE, now=NOW, cooldown_seconds=3
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "recovered")
            self.assertEqual(result["attempted_symbols"], 2)
            self.assertEqual(result["recovered_symbols"], 2)
            self.assertEqual(result["final_direct_coverage_rate"], 1.0)
            self.assertEqual(fetch.call_args.args[0], missing)
            self.assertEqual(fetch.call_args.args[1], CYCLE)
            sleep.assert_called_once_with(3.0)
            self.assertEqual(
                result["recovery_contract"]["maximum_requests_per_symbol"], 2
            )
            self.assertFalse(
                result["recovery_contract"]["historical_retry"]
            )

            con = sqlite3.connect(db_root / "market.db")
            methods = {
                row[0]: json.loads(row[1]).get("method")
                for row in con.execute(
                    "SELECT symbol,raw FROM market_contract_statistics "
                    "WHERE cycle_id=?",
                    (CYCLE,),
                )
            }
            con.close()
            self.assertEqual(methods[missing[0]], "rubik_common_bucket")
            self.assertEqual(methods[missing[1]], "rubik_common_bucket")
            self.assertEqual(result["production_database_writes"], 0)
            self.assertEqual(result["isolated_database_writes"], 2)

    def test_incomplete_recovery_remains_degraded(self) -> None:
        symbols = [f"S{i:03d}-USDT-SWAP" for i in range(100)]
        with tempfile.TemporaryDirectory() as temporary:
            db_root = self._db_root(temporary, symbols)
            con = sqlite3.connect(db_root / "market.db")
            collector.write_contract_statistics_rows(
                con,
                [_direct_row(symbol) for symbol in symbols[:50]],
            )
            con.commit()
            con.close()
            with (
                mock.patch.object(
                    recovery,
                    "_fetch_once",
                    return_value=([], ["simulated"], {}),
                ),
                mock.patch.object(recovery.time, "sleep"),
                mock.patch.object(
                    recovery.collector,
                    "utc_now_iso",
                    return_value="2026-08-13T16:18:00Z",
                ),
            ):
                result = recovery.recover(
                    db_root, CYCLE, now=NOW, cooldown_seconds=0
                )
            self.assertFalse(result["ok"])
            self.assertTrue(result["degraded"])
            self.assertEqual(result["status"], "recovery_incomplete")
            self.assertEqual(result["final_direct_coverage_rate"], 0.5)

    def test_complete_cycle_performs_no_network_or_write(self) -> None:
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        with tempfile.TemporaryDirectory() as temporary:
            db_root = self._db_root(temporary, symbols)
            con = sqlite3.connect(db_root / "market.db")
            collector.write_contract_statistics_rows(
                con, [_direct_row(symbol) for symbol in symbols]
            )
            con.commit()
            con.close()
            with (
                mock.patch.object(recovery, "_fetch_once") as fetch,
                mock.patch.object(recovery.time, "sleep") as sleep,
                mock.patch.object(
                    recovery.collector,
                    "utc_now_iso",
                    return_value="2026-08-13T16:18:00Z",
                ),
            ):
                result = recovery.recover(db_root, CYCLE, now=NOW)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "already_complete")
            self.assertEqual(result["production_database_writes"], 0)
            fetch.assert_not_called()
            sleep.assert_not_called()

    def test_fast_status_uses_successful_recovery_but_not_failed_recovery(self) -> None:
        healthy = {
            "name": "collect_data", "ok": True, "payload": {}
        }
        account = {
            "name": "live_account_check", "ok": True, "payload": {}
        }
        features = {
            "name": "market_features", "ok": True, "payload": {}
        }
        failed_primary = {
            "name": "contract_statistics",
            "ok": False,
            "payload": {"degraded": True},
        }
        recovered = {
            "name": "contract_statistics_recovery",
            "ok": True,
            "payload": {
                "degraded": False,
                "final_direct_coverage_rate": 1.0,
            },
        }
        still_failed = {
            "name": "contract_statistics_recovery",
            "ok": False,
            "payload": {
                "degraded": True,
                "final_direct_coverage_rate": 0.98,
            },
        }
        self.assertEqual(
            fast_collect._collection_status([
                healthy, account, failed_primary, recovered, features,
            ]),
            "ok",
        )
        self.assertEqual(
            fast_collect._collection_status([
                healthy, account, failed_primary, still_failed, features,
            ]),
            "degraded",
        )

    def test_failed_primary_without_recovery_remains_degraded(self) -> None:
        steps = [
            {"name": "collect_data", "ok": True, "payload": {}},
            {"name": "live_account_check", "ok": True, "payload": {}},
            {
                "name": "contract_statistics",
                "ok": False,
                "payload": {"degraded": True},
            },
            {"name": "market_features", "ok": True, "payload": {}},
        ]
        self.assertEqual(fast_collect._collection_status(steps), "degraded")

    def test_fast_main_does_not_recover_when_core_market_failed(self) -> None:
        calls: list[str] = []

        def fake_run(name, _script, _args, _timeout):
            calls.append(name)
            ok = name not in {"collect_data", "contract_statistics"}
            return {
                "name": name,
                "ok": ok,
                "rc": 0 if ok else 1,
                "dur_s": 0.0,
                "payload": {},
                "stderr_tail": "" if ok else "simulated failure",
            }

        with tempfile.TemporaryDirectory() as temporary:
            argv = [
                "fast_collect.py",
                "--db-root", str(Path(temporary) / "db"),
                "--cycle", "2026-08-14T00:15",
                "--no-universe-shadow",
                "--no-model-shadow",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    fast_collect, "run_step", side_effect=fake_run
                ),
                mock.patch.object(fast_collect.ledger, "init_ledger"),
                mock.patch.object(fast_collect.ledger, "record_collection"),
                mock.patch.object(fast_collect, "_nudge_mod", None),
                mock.patch.object(sys, "stdout"),
            ):
                self.assertEqual(1, fast_collect.main())
        self.assertNotIn("contract_statistics_recovery", calls)

    def test_fast_main_uses_verified_recovery_and_keeps_primary_warning(self) -> None:
        calls: list[str] = []

        def fake_run(name, _script, _args, _timeout):
            calls.append(name)
            if name == "contract_statistics":
                return {
                    "name": name,
                    "ok": False,
                    "rc": 1,
                    "dur_s": 0.0,
                    "payload": {
                        "degraded": True,
                        "warnings": ["initial direct coverage below gate"],
                    },
                    "stderr_tail": "initial failure",
                }
            payload = {}
            if name == "contract_statistics_recovery":
                payload = {
                    "degraded": False,
                    "final_direct_coverage_rate": 1.0,
                    "wrote": {"contract_statistics_recovery": 2},
                }
            return {
                "name": name,
                "ok": True,
                "rc": 0,
                "dur_s": 0.0,
                "payload": payload,
                "stderr_tail": "",
            }

        with tempfile.TemporaryDirectory() as temporary:
            import io
            stream = io.StringIO()
            argv = [
                "fast_collect.py",
                "--db-root", str(Path(temporary) / "db"),
                "--cycle", "2026-08-14T00:15",
                "--no-universe-shadow",
                "--no-model-shadow",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    fast_collect, "run_step", side_effect=fake_run
                ),
                mock.patch.object(fast_collect.ledger, "init_ledger"),
                mock.patch.object(
                    fast_collect.ledger, "record_collection"
                ) as record,
                mock.patch.object(fast_collect, "_nudge_mod", None),
                mock.patch.object(sys, "stdout", stream),
            ):
                self.assertEqual(0, fast_collect.main())
        self.assertIn("contract_statistics_recovery", calls)
        self.assertEqual(record.call_args.args[3], "ok")
        output = json.loads(stream.getvalue().strip().splitlines()[-1])
        self.assertEqual(output["status"], "ok")
        self.assertTrue(any(
            "initial direct coverage below gate" in warning
            for warning in output["warnings"]
        ))
        primary = next(
            step for step in output["steps"]
            if step["name"] == "contract_statistics"
        )
        self.assertFalse(primary["ok"])

    def test_cold_fetch_has_one_attempt_per_endpoint_and_restores_workers(self) -> None:
        symbol = "BTC-USDT-SWAP"
        oi = {symbol: [["1786636800000", "100", "10", "1000"]]}
        taker = {symbol: [["1786636800000", "40", "60"]]}

        def oi_fetch(selected, _period, _limit, _timeout, **kwargs):
            self.assertEqual(selected, [symbol])
            self.assertEqual(kwargs["request_retries"], 0)
            kwargs["outcomes"][symbol] = {"ok": True}
            return oi

        def taker_fetch(selected, _period, _unit, _limit, _timeout, **kwargs):
            self.assertEqual(selected, [symbol])
            self.assertEqual(kwargs["request_retries"], 0)
            kwargs["outcomes"][symbol] = {"ok": True}
            return taker

        previous = recovery._okx_http._CONTRACT_STATS_WORKERS
        with (
            mock.patch.object(
                recovery._okx_http,
                "fetch_contract_open_interest_history_batch_sync",
                side_effect=oi_fetch,
            ),
            mock.patch.object(
                recovery._okx_http,
                "fetch_contract_taker_volumes_batch_sync",
                side_effect=taker_fetch,
            ),
        ):
            rows, errors, transport = recovery._fetch_once(
                [symbol],
                CYCLE,
                collected_ts="2026-08-13T16:18:00Z",
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(errors, [])
        self.assertEqual(transport["open_interest_ok"], 1)
        self.assertEqual(transport["taker_volume_ok"], 1)
        self.assertEqual(recovery._okx_http._CONTRACT_STATS_WORKERS, previous)


if __name__ == "__main__":
    unittest.main()
