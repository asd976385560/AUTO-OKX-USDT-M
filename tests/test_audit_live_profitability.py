# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_live_profitability as audit  # noqa: E402
import daily_maintenance  # noqa: E402


class LiveProfitabilityAuditTests(unittest.TestCase):
    def test_daily_maintenance_wiring_is_noncritical(self):
        names = [step[0] for step in daily_maintenance.STEPS]
        self.assertEqual(1, names.count("live_profitability"))
        self.assertEqual(1, names.count("account_cash_flows"))
        self.assertGreater(
            names.index("live_profitability"), names.index("quality_metrics"))
        self.assertGreater(
            names.index("live_profitability"), names.index("account_cash_flows"))
        self.assertNotIn(
            "live_profitability", daily_maintenance.REVIEWER_CRITICAL_STEPS)
        self.assertNotIn(
            "account_cash_flows", daily_maintenance.REVIEWER_CRITICAL_STEPS)
        cash_step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "account_cash_flows")
        self.assertIn("--cash-flows-forward", cash_step[1])
        self.assertIn("--receipt-dir", cash_step[1])
        self.assertIn("--cash-flow-wait-for-close-seconds", cash_step[1])
        self.assertEqual(420, cash_step[2])
        self.assertEqual((0,), cash_step[3])
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "live_profitability")
        argv = step[1]
        self.assertEqual(
            str(SCRIPTS / "audit_live_profitability.py"), argv[0])
        self.assertIn("--json-out", argv)
        output = argv[argv.index("--json-out") + 1]
        self.assertTrue(output.endswith("live-profitability-audit.json"))
        self.assertIn("--cash-flow-receipt-dir", argv)
        self.assertNotIn("--target-win-rate", argv)
        self.assertEqual((0,), step[3])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.account = self.root / "account.db"
        self.trades = self.root / "live_trades.db"
        self.ledger = self.root / "ledger.db"
        self.receipts = self.root / "cash-flow-receipts"
        self.receipts.mkdir()
        self._create_schema()
        self._write_cash_flow_receipts()

    def tearDown(self):
        self.temp.cleanup()

    def _create_schema(self):
        con = sqlite3.connect(self.account)
        con.executescript("""
        CREATE TABLE account_snapshots(
          ts TEXT, profile TEXT, totalEq REAL, availBal REAL, upl REAL,
          PRIMARY KEY(ts,profile));
        CREATE TABLE account_bills(
          profile TEXT,bill_id TEXT,ts TEXT,inst_id TEXT,ccy TEXT,type TEXT,
          subtype TEXT,bal_change REAL,fee REAL,pnl REAL,interest REAL,
          ord_id TEXT,trade_id TEXT,exec_type TEXT,fetched_at TEXT,raw TEXT,
          PRIMARY KEY(profile,bill_id));
        """)
        con.executemany(
            "INSERT INTO account_snapshots VALUES (?,?,?,?,?)",
            [
                ("2026-01-01 00:00:00", "live", 1000.0, 1000.0, 0.0),
                ("2026-02-01 00:00:00", "live", 1010.0, 1000.0, 1.0),
                ("2026-02-01 01:00:00", "live", 1011.0, 1000.0, 2.0),
            ],
        )
        con.commit()
        con.close()

        con = sqlite3.connect(self.trades)
        con.execute("""
        CREATE TABLE trades(
          id INTEGER PRIMARY KEY,cycle_id TEXT,ts TEXT,symbol TEXT,action TEXT,
          side TEXT,sz REAL,fill_px REAL,pnl REAL,raw TEXT)
        """)
        con.commit()
        con.close()

        con = sqlite3.connect(self.ledger)
        con.execute("""
        CREATE TABLE execution_intents(
          profile TEXT,cycle_id TEXT,symbol TEXT,action TEXT,side TEXT,state TEXT,
          reserved_at TEXT,submitted_at TEXT,completed_at TEXT,ord_id TEXT,error TEXT)
        """)
        con.commit()
        con.close()

    def _write_cash_flow_receipts(self):
        activation = datetime(2026, 1, 1, 0, 0, 0)
        cursor = activation
        first_end = datetime(2026, 1, 1, 8, 0, 0)
        intervals = [(cursor, first_end)]
        cursor = first_end
        final_end = datetime(2026, 2, 1, 8, 0, 0)
        while cursor < final_end:
            end = min(cursor + timedelta(days=1), final_end)
            intervals.append((cursor, end))
            cursor = end
        for start, end in intervals:
            payload = {
                "schema_version": 1,
                "artifact_type": "account_cash_flow_forward_receipt",
                "profile": "live",
                "endpoint": "/api/v5/account/bills",
                "bill_type": "1",
                "subtype_mapping": {"11": "transfer_in", "12": "transfer_out"},
                "forward_start_cst": "2026-01-01 00:00:00",
                "status": "ok",
                "pagination_complete": True,
                "historical_backfill": False,
                "orders_placed": 0,
                "window_start_cst": start.strftime("%Y-%m-%d %H:%M:%S"),
                "window_end_exclusive_cst": end.strftime("%Y-%m-%d %H:%M:%S"),
            }
            path = self.receipts / f"receipt-{end:%Y-%m-%d}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _add_lifecycle(
        self, *, number: int, symbol: str, close_net: float,
        close_pnl: float, reason: str = "strategy",
        close_has_bill: bool = True,
    ):
        open_id = f"open-{number}"
        close_id = f"close-{number}"
        start = f"2026-01-{number + 1:02d} 00:00:00"
        end = f"2026-01-{number + 1:02d} 12:00:00"
        con = sqlite3.connect(self.trades)
        con.executemany(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    number * 10 + 1, f"2026-01-{number + 1:02d}T00:00", start,
                    symbol, "open", "long", 2.0, 10.0, 0.0,
                    json.dumps({
                        "ordId": open_id, "fill_source": "fills",
                        "reason": reason,
                    }),
                ),
                (
                    number * 10 + 2, f"2026-01-{number + 1:02d}T12:00", end,
                    symbol, "close", "long", 2.0, 11.0, close_pnl,
                    json.dumps({
                        "reconcile_source": "exchange_fills_reconcile",
                        "ord_ids": [close_id],
                        "fills": [{"ordId": close_id}],
                    }),
                ),
            ],
        )
        con.commit()
        con.close()

        con = sqlite3.connect(self.ledger)
        con.execute(
            "INSERT INTO execution_intents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "live", f"2026-01-{number + 1:02d}T00:00", symbol, "open",
                "long", "completed", start, start, start, open_id, None,
            ),
        )
        con.commit()
        con.close()

        con = sqlite3.connect(self.account)
        con.execute(
            "INSERT INTO account_bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "live", f"bill-open-{number}", start, symbol, "USDT", "2", "3",
                -1.0, -1.0, 0.0, 0.0, open_id, f"trade-open-{number}", "T",
                "2026-02-01 01:00:00", "{}",
            ),
        )
        if close_has_bill:
            con.execute(
                "INSERT INTO account_bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "live", f"bill-close-{number}", end, symbol, "USDT", "2", "5",
                    close_net + 1.0, -1.0, close_pnl, 0.0, close_id,
                    f"trade-close-{number}", "T", "2026-02-01 01:00:00", "{}",
                ),
            )
        con.commit()
        con.close()

    def _report(self, **overrides):
        values = {
            "account_db": self.account,
            "trades_db": self.trades,
            "ledger_db": self.ledger,
            "cash_flow_receipt_dir": self.receipts,
            "cash_flow_forward_start": "2026-01-01 00:00:00",
            "as_of": "2026-02-01 01:00:00",
            "window_days": 30,
            "minimum_closed_lifecycles": 1,
        }
        values.update(overrides)
        return audit.build_report(**values)

    def test_lifecycle_uses_bills_and_reconciles_equity(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        report = self._report()
        self.assertEqual("DIAGNOSTIC_ONLY", report["status"])
        self.assertEqual("COMPLETE", report["diagnostic_evidence_status"])
        self.assertNotIn("legacy_lifecycle_status", report)
        self.assertNotIn("minimum_win_rate", report["diagnostic_configuration"])
        self.assertNotIn("targets", report["requirements"])
        self.assertEqual(1, report["lifecycle_profile"]["completed_in_window"])
        self.assertEqual(1, report["strategy_performance"]["wins"])
        self.assertEqual(1.0, report["strategy_performance"]["win_rate"])
        self.assertEqual(
            1.0,
            report["strategy_performance"]["unverified_outcome_sensitivity"]
            ["all_unverified_assumed_wins_win_rate"],
        )
        self.assertAlmostEqual(9.0, report["account_economics"]["account_net_profit"])
        self.assertAlmostEqual(
            11.0, report["account_economics"]["cash_flow_adjusted_equity_delta"])
        self.assertAlmostEqual(0.0, report["account_economics"]["equity_reconciliation_gap"])
        diagnostic = report["policy_epoch_diagnostic"]
        self.assertEqual(
            1,
            diagnostic["cohorts"]["pre_withdrawn_probe"]["verified_sample_n"],
        )
        self.assertEqual(
            0,
            diagnostic["cohorts"]["current_policy_after_withdrawal"]
            ["verified_sample_n"],
        )
        self.assertEqual(
            "NOT_MEASURABLE", diagnostic["current_policy_sample_status"])
        self.assertNotIn("target_win_rate", diagnostic)
        self.assertTrue(diagnostic["acceptance_denominator_unchanged"])

    def test_policy_epoch_diagnostic_never_filters_full_rows(self):
        rows = [
            {
                "start_cycle_id": "2026-08-15T10:15",
                "net_pnl_after_direct_costs": -2.0,
            },
            {
                "start_cycle_id": "2026-08-18T10:15",
                "net_pnl_after_direct_costs": 3.0,
            },
        ]
        result = audit.policy_epoch_diagnostic(
            rows, minimum_closed_lifecycles=2)
        self.assertEqual(
            1,
            result["cohorts"][audit.policy_epochs.EPOCH_WITHDRAWN_PROBE]
            ["verified_sample_n"],
        )
        self.assertEqual(
            1,
            result["cohorts"]["current_policy_after_withdrawal"]
            ["verified_sample_n"],
        )
        self.assertEqual(
            len(rows),
            sum(item["verified_sample_n"] for item in result["cohorts"].values()),
        )
        self.assertEqual(
            "NOT_MEASURABLE", result["current_policy_sample_status"])
        self.assertNotIn("target_win_rate", result)

    def test_policy_epoch_sample_status_has_no_win_rate_threshold(self):
        rows = [{
            "start_cycle_id": "2026-08-18T10:15",
            "net_pnl_after_direct_costs": -3.0,
        }]
        result = audit.policy_epoch_diagnostic(
            rows, minimum_closed_lifecycles=1)
        self.assertEqual(
            "MEASURABLE", result["current_policy_sample_status"])
        self.assertEqual(
            0.0,
            result["cohorts"]["current_policy_after_withdrawal"]["win_rate"],
        )
        self.assertNotIn("target_win_rate", result)

    def test_missing_close_bill_blocks_verification(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0,
            close_pnl=10.0, close_has_bill=False)
        report = self._report()
        self.assertEqual(
            "INSUFFICIENT_EVIDENCE", report["diagnostic_evidence_status"])
        self.assertEqual(1, report["lifecycle_profile"]["unverified_strategy_lifecycles"])
        self.assertIn(
            "account_bill_missing:close-1",
            report["unverified_examples"][0]["issues"],
        )
        self.assertNotIn(
            "target_reachable_if_all_unverified_are_wins",
            report["strategy_performance"]["unverified_outcome_sensitivity"],
        )

    def test_legacy_direct_fill_receipt_does_not_require_intent(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        con = sqlite3.connect(self.ledger)
        con.execute("DELETE FROM execution_intents")
        con.commit()
        con.close()
        report = self._report()
        self.assertEqual("COMPLETE", report["diagnostic_evidence_status"])
        self.assertEqual(1, report["lifecycle_profile"]["verified_strategy_lifecycles"])

    def test_truncated_fill_recovers_order_id_from_exact_completed_intent(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        con = sqlite3.connect(self.trades)
        con.execute(
            "UPDATE trades SET raw=? WHERE id=11",
            (json.dumps({
                "raw_structurally_truncated": True,
                "raw_truncated_fields": [
                    {"field": "ordId", "sha256": "opaque"},
                    {"field": "fill_source", "sha256": "opaque"},
                ],
            }),),
        )
        con.commit()
        con.close()

        report = self._report()
        profile = report["lifecycle_profile"]
        self.assertEqual(1, profile["verified_strategy_lifecycles"])
        self.assertEqual(0, profile["unverified_strategy_lifecycles"])
        self.assertEqual(
            1, profile["order_ids_recovered_from_exact_completed_intents"])
        self.assertEqual(1, profile["lifecycles_with_recovered_order_ids"])

    def test_truncated_fill_does_not_use_non_exact_intent(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        con = sqlite3.connect(self.trades)
        con.execute(
            "UPDATE trades SET raw=? WHERE id=11",
            (json.dumps({"raw_structurally_truncated": True}),),
        )
        con.commit()
        con.close()
        con = sqlite3.connect(self.ledger)
        con.execute("UPDATE execution_intents SET action='add'")
        con.commit()
        con.close()

        report = self._report()
        self.assertEqual(
            1, report["lifecycle_profile"]["unverified_strategy_lifecycles"])
        self.assertIn(
            "trade_id=11:order_id_missing",
            report["unverified_examples"][0]["issues"],
        )

    def test_add_and_reduce_merge_into_one_lifecycle(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        con = sqlite3.connect(self.trades)
        con.execute("UPDATE trades SET sz=1.0 WHERE id=12")
        con.executemany(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (13, "2026-01-02T06:00", "2026-01-02 06:00:00",
                 "BTC-USDT-SWAP", "add", "long", 1.0, 10.0, 0.0,
                 json.dumps({"ordId": "add-1", "fill_source": "fills"})),
                (14, "2026-01-02T18:00", "2026-01-02 18:00:00",
                 "BTC-USDT-SWAP", "reduce", "long", 2.0, 11.0, 0.0,
                 json.dumps({
                     "reconcile_source": "exchange_fills_reconcile",
                     "ord_ids": ["reduce-1"], "fills": [{"ordId": "reduce-1"}],
                 })),
            ],
        )
        con.commit()
        con.close()
        con = sqlite3.connect(self.ledger)
        con.execute(
            "INSERT INTO execution_intents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("live", "2026-01-02T06:00", "BTC-USDT-SWAP", "add", "long",
             "completed", "2026-01-02 06:00:00", "2026-01-02 06:00:00",
             "2026-01-02 06:00:00", "add-1", None),
        )
        con.commit()
        con.close()
        con = sqlite3.connect(self.account)
        con.executemany(
            "INSERT INTO account_bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("live", "bill-add", "2026-01-02 06:00:00", "BTC-USDT-SWAP",
                 "USDT", "2", "3", -0.1, -0.1, 0.0, 0.0, "add-1", "ta", "T",
                 "2026-02-01 01:00:00", "{}"),
                ("live", "bill-reduce", "2026-01-02 18:00:00", "BTC-USDT-SWAP",
                 "USDT", "2", "5", 0.1, -0.1, 0.2, 0.0, "reduce-1", "tr", "T",
                 "2026-02-01 01:00:00", "{}"),
            ],
        )
        con.commit()
        con.close()
        report = self._report()
        self.assertEqual("COMPLETE", report["diagnostic_evidence_status"])
        self.assertEqual(1, report["lifecycle_profile"]["completed_in_window"])
        self.assertEqual(1, report["lifecycle_profile"]["verified_strategy_lifecycles"])
        self.assertEqual(0, report["lifecycle_profile"]["unverified_strategy_lifecycles"])
        self.assertAlmostEqual(
            9.0, report["strategy_performance"]["net_pnl_after_direct_costs"])

    def test_maintenance_is_excluded_from_strategy_but_cost_remains(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0,
            close_pnl=10.0, reason="MICROTEST live chain regression")
        report = self._report()
        self.assertEqual(
            "INSUFFICIENT_EVIDENCE", report["diagnostic_evidence_status"])
        self.assertEqual(0, report["lifecycle_profile"]["strategy_candidates"])
        self.assertEqual(1, report["lifecycle_profile"]["maintenance_lifecycles"])
        self.assertAlmostEqual(9.0, report["account_economics"]["account_net_profit"])
        self.assertTrue(report["maintenance_real_cost"]["included_in_account_net_profit"])

    def test_unknown_bill_type_blocks_cash_flow_adjustment(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        con = sqlite3.connect(self.account)
        con.execute(
            "INSERT INTO account_bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("live", "unknown", "2026-01-15 00:00:00", "", "USDT", "99", "1",
             50.0, 0.0, 0.0, 0.0, "", "", "", "2026-02-01 01:00:00", "{}"),
        )
        con.commit()
        con.close()
        report = self._report()
        self.assertEqual(
            "INSUFFICIENT_EVIDENCE", report["diagnostic_evidence_status"])
        self.assertEqual(
            ["99"], report["account_economics"]["unsupported_or_unclassified_bill_types"])
        self.assertFalse(
            report["account_economics"]["external_cash_flow_evidence_complete"])
        self.assertIsNone(
            report["account_economics"]["cash_flow_adjusted_equity_delta"])

    def test_unexplained_equity_jump_is_not_called_adjusted_profit(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        con = sqlite3.connect(self.account)
        con.execute(
            "UPDATE account_snapshots SET totalEq=1511.0 "
            "WHERE ts='2026-02-01 01:00:00' AND profile='live'")
        con.commit()
        con.close()
        report = self._report()
        self.assertEqual(
            "INSUFFICIENT_EVIDENCE", report["diagnostic_evidence_status"])
        self.assertFalse(
            report["account_economics"]["external_cash_flow_evidence_complete"])
        self.assertIsNone(
            report["account_economics"]["cash_flow_adjusted_equity_delta"])
        self.assertAlmostEqual(
            500.0,
            report["account_economics"]["observed_unexplained_equity_movement"])

    def test_missing_cash_flow_receipt_blocks_adjusted_equity(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        (self.receipts / "receipt-2026-01-15.json").unlink()
        report = self._report()
        coverage = report["account_economics"]["cash_flow_forward_coverage"]
        self.assertFalse(coverage["complete"])
        self.assertTrue(coverage["gaps"])
        self.assertEqual(
            "INSUFFICIENT_EVIDENCE", report["diagnostic_evidence_status"])
        self.assertIsNone(
            report["account_economics"]["cash_flow_adjusted_equity_delta"])

    def test_irrelevant_old_receipt_does_not_poison_current_window(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        (self.receipts / "receipt-2025-12-01.json").write_text(
            json.dumps({
                "window_start_cst": "2025-11-30 08:00:00",
                "window_end_exclusive_cst": "2025-12-01 08:00:00",
            }),
            encoding="utf-8",
        )
        report = self._report()
        self.assertEqual("COMPLETE", report["diagnostic_evidence_status"])
        self.assertEqual(
            [], report["account_economics"]["cash_flow_forward_coverage"]
            ["invalid_receipts"])

    def test_observed_transfer_is_excluded_from_adjusted_equity(self):
        self._add_lifecycle(
            number=1, symbol="BTC-USDT-SWAP", close_net=9.0, close_pnl=10.0)
        con = sqlite3.connect(self.account)
        con.execute(
            "INSERT INTO account_bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("live", "transfer-in", "2026-01-15 00:00:00", "", "USDT",
             "1", "11", 100.0, 0.0, 0.0, 0.0, "", "", "",
             "2026-02-02 01:00:00", "{}"),
        )
        con.execute(
            "UPDATE account_snapshots SET totalEq=1111.0 "
            "WHERE ts='2026-02-01 01:00:00' AND profile='live'")
        con.commit()
        con.close()
        report = self._report()
        economics = report["account_economics"]
        self.assertEqual("COMPLETE", report["diagnostic_evidence_status"])
        self.assertEqual(
            "2026-02-01 01:00:00", report["window"]["evidence_cutoff_cst"])
        self.assertTrue(economics["cash_flow_forward_coverage"]["complete"])
        self.assertAlmostEqual(100.0, economics["external_cash_flow_net_observed"])
        self.assertAlmostEqual(100.0, economics["external_cash_flow_adjustment"])
        self.assertAlmostEqual(11.0, economics["cash_flow_adjusted_equity_delta"])
        self.assertAlmostEqual(0.0, economics["equity_reconciliation_gap"])

    def test_opposite_side_overlap_blocks_directionless_funding(self):
        lifecycles = [
            {
                "lifecycle_id": "long",
                "symbol": "BTC-USDT-SWAP",
                "side": "long",
                "started_at": "2026-01-02 00:00:00",
                "closed_at": "2026-01-03 00:00:00",
            },
            {
                "lifecycle_id": "short",
                "symbol": "BTC-USDT-SWAP",
                "side": "short",
                "started_at": "2026-01-02 06:00:00",
                "closed_at": "2026-01-03 06:00:00",
            },
        ]
        funding = {
            "BTC-USDT-SWAP": [
                {"ts": "2026-01-02 08:00:00", "bal_change": -1.0}
            ]
        }
        self.assertEqual(
            {"long", "short"},
            audit.ambiguous_funding_lifecycle_ids(lifecycles, funding),
        )


if __name__ == "__main__":
    unittest.main()
