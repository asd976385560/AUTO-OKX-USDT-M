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

import audit_weekly_trading_net_profit as audit  # noqa: E402
import daily_maintenance  # noqa: E402


class WeeklyTradingNetProfitAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.account = self.root / "account.db"
        self.transfer_receipts = self.root / "transfer-receipts"
        self.trading_receipts = self.root / "trading-receipts"
        self.transfer_receipts.mkdir()
        self.trading_receipts.mkdir()
        con = sqlite3.connect(self.account)
        con.execute("""
            CREATE TABLE account_bills(
              profile TEXT,bill_id TEXT,ts TEXT,inst_id TEXT,ccy TEXT,type TEXT,
              subtype TEXT,bal_change REAL,fee REAL,pnl REAL,interest REAL,
              ord_id TEXT,trade_id TEXT,exec_type TEXT,fetched_at TEXT,raw TEXT,
              PRIMARY KEY(profile,bill_id))
        """)
        con.commit()
        con.close()

    def tearDown(self):
        self.temp.cleanup()

    def add_bill(
        self,
        bill_id: str,
        ts: str,
        *,
        bill_type: str,
        subtype: str,
        bal_change: float,
        ccy: str = "USDT",
    ) -> None:
        con = sqlite3.connect(self.account)
        con.execute(
            "INSERT INTO account_bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "live", bill_id, ts, "BTC-USDT-SWAP", ccy, bill_type,
                subtype, bal_change, 0.0, 0.0, 0.0, "", "", "",
                "2026-01-13 09:00:00", "{}",
            ),
        )
        con.commit()
        con.close()

    def write_transfer_receipts(self, start: str, end: str) -> None:
        cursor = datetime.fromisoformat(start)
        stop = datetime.fromisoformat(end)
        while cursor < stop:
            next_end = min(cursor + timedelta(days=1), stop)
            payload = {
                "schema_version": 1,
                "artifact_type": "account_cash_flow_forward_receipt",
                "profile": "live",
                "endpoint": "/api/v5/account/bills",
                "bill_type": "1",
                "subtype_mapping": {"11": "transfer_in", "12": "transfer_out"},
                "status": "ok",
                "pagination_complete": True,
                "historical_backfill": False,
                "orders_placed": 0,
                "window_start_cst": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                "window_end_exclusive_cst": next_end.strftime("%Y-%m-%d %H:%M:%S"),
            }
            (self.transfer_receipts / f"receipt-{next_end:%Y-%m-%d}.json").write_text(
                json.dumps(payload), encoding="utf-8")
            cursor = next_end

    def write_trading_receipts(self, start: str, end: str) -> None:
        cursor = datetime.fromisoformat(start)
        stop = datetime.fromisoformat(end)
        while cursor < stop:
            next_end = min(cursor + timedelta(days=1), stop)
            payload = {
                "schema_version": 1,
                "artifact_type": "account_trading_bills_forward_receipt",
                "status": "ok",
                "profile": "live",
                "endpoint": "/api/v5/account/bills",
                "inst_type": "SWAP",
                "window_start_cst": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                "window_end_exclusive_cst": next_end.strftime(
                    "%Y-%m-%d %H:%M:%S"),
                "pagination_complete": True,
                "historical_backfill": False,
                "orders_placed": 0,
            }
            (self.trading_receipts / f"receipt-{next_end:%Y-%m-%d}.json").write_text(
                json.dumps(payload), encoding="utf-8")
            cursor = next_end

    def report(self, *, activation: str = "2026-01-05 08:00:00", weeks: int = 1):
        return audit.build_report(
            account_db=self.account,
            transfer_receipt_dir=self.transfer_receipts,
            trading_receipt_dir=self.trading_receipts,
            as_of="2026-01-13 12:00:00",
            activation_cst=activation,
            history_weeks=weeks,
        )

    def prepare_complete_week(self) -> None:
        self.write_transfer_receipts(
            "2026-01-05 08:00:00", "2026-01-12 08:00:00")
        self.write_trading_receipts(
            "2026-01-05 08:00:00", "2026-01-12 08:00:00")

    def test_daily_maintenance_wires_weekly_authority_before_diagnostic(self):
        names = [step[0] for step in daily_maintenance.STEPS]
        self.assertEqual(1, names.count("account_trading_bills_forward"))
        self.assertEqual(1, names.count("weekly_trading_net_profit"))
        self.assertLess(
            names.index("account_trading_bills_forward"),
            names.index("weekly_trading_net_profit"),
        )
        self.assertLess(
            names.index("weekly_trading_net_profit"),
            names.index("live_profitability"),
        )
        step = next(
            item for item in daily_maintenance.STEPS
            if item[0] == "weekly_trading_net_profit")
        self.assertIn("--activation-cst", step[1])
        self.assertIn("weekly-trading-net-profit-audit.json", " ".join(step[1]))
        self.assertNotIn(
            "weekly_trading_net_profit", daily_maintenance.REVIEWER_CRITICAL_STEPS)

    def test_positive_week_is_met_and_transfer_is_excluded(self):
        self.prepare_complete_week()
        self.add_bill(
            "trade", "2026-01-06 10:00:00",
            bill_type="2", subtype="5", bal_change=5.0)
        self.add_bill(
            "funding", "2026-01-07 00:00:00",
            bill_type="8", subtype="173", bal_change=-1.0)
        self.add_bill(
            "transfer", "2026-01-08 00:00:00",
            bill_type="1", subtype="11", bal_change=100.0)
        report = self.report()
        self.assertEqual("MET", report["status"])
        week = report["weeks"][0]
        self.assertEqual(4.0, week["trading_net_profit_usdt"])
        self.assertEqual(100.0, week["excluded_transfer_net_usdt"])
        self.assertEqual(2, week["trading_bill_rows"])
        self.assertEqual(1, week["excluded_transfer_rows"])
        self.assertTrue(week["evidence_complete"])
        self.assertFalse(report["diagnostics"]["win_rate_required"])

    def test_negative_and_zero_weeks_are_not_met(self):
        self.prepare_complete_week()
        self.add_bill(
            "loss", "2026-01-06 10:00:00",
            bill_type="2", subtype="5", bal_change=-0.01)
        self.assertEqual("NOT_MET", self.report()["status"])

        con = sqlite3.connect(self.account)
        con.execute("DELETE FROM account_bills")
        con.commit()
        con.close()
        report = self.report()
        self.assertEqual("NOT_MET", report["status"])
        self.assertEqual(0.0, report["weeks"][0]["trading_net_profit_usdt"])

    def test_missing_transfer_receipt_is_insufficient_evidence(self):
        self.write_trading_receipts(
            "2026-01-05 08:00:00", "2026-01-12 08:00:00")
        self.add_bill(
            "trade", "2026-01-06 10:00:00",
            bill_type="2", subtype="5", bal_change=5.0)
        report = self.report()
        self.assertEqual("INSUFFICIENT_EVIDENCE", report["status"])
        self.assertFalse(
            report["weeks"][0]["evidence_checks"]
            ["transfer_query_receipts_complete"])

    def test_incomplete_trading_pagination_is_not_complete_evidence(self):
        self.write_transfer_receipts(
            "2026-01-05 08:00:00", "2026-01-12 08:00:00")
        path = self.trading_receipts / "receipt-2026-01-06.json"
        payload = {
            "schema_version": 1,
            "artifact_type": "account_trading_bills_forward_receipt",
            "status": "ok", "profile": "live",
            "endpoint": "/api/v5/account/bills", "inst_type": "SWAP",
            "window_start_cst": "2026-01-05 08:00:00",
            "window_end_exclusive_cst": "2026-01-06 08:00:00",
            "pagination_complete": False, "historical_backfill": False,
            "orders_placed": 0,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.add_bill(
            "trade", "2026-01-06 10:00:00",
            bill_type="2", subtype="5", bal_change=5.0)
        self.assertEqual("INSUFFICIENT_EVIDENCE", self.report()["status"])

    def test_unknown_bill_type_blocks_week_claim(self):
        self.prepare_complete_week()
        self.add_bill(
            "trade", "2026-01-06 10:00:00",
            bill_type="2", subtype="5", bal_change=5.0)
        self.add_bill(
            "unknown", "2026-01-07 10:00:00",
            bill_type="99", subtype="1", bal_change=10.0)
        report = self.report()
        self.assertEqual("INSUFFICIENT_EVIDENCE", report["status"])
        self.assertEqual(
            ["unknown"],
            report["weeks"][0]["unsupported_or_unclassified_bill_ids"])

    def test_before_first_binding_week_is_pending_not_historical_met(self):
        self.prepare_complete_week()
        self.add_bill(
            "trade", "2026-01-06 10:00:00",
            bill_type="2", subtype="5", bal_change=5.0)
        report = self.report(activation="2026-01-19 08:00:00")
        self.assertEqual("PENDING_FORWARD_EVIDENCE", report["status"])
        self.assertEqual(
            "PRE_ACTIVATION_DIAGNOSTIC", report["weeks"][0]["status"])

    def test_every_eligible_complete_week_must_be_positive(self):
        self.write_transfer_receipts(
            "2025-12-29 08:00:00", "2026-01-12 08:00:00")
        self.write_trading_receipts(
            "2025-12-29 08:00:00", "2026-01-12 08:00:00")
        self.add_bill(
            "win", "2026-01-02 10:00:00",
            bill_type="2", subtype="5", bal_change=5.0)
        self.add_bill(
            "loss", "2026-01-06 10:00:00",
            bill_type="2", subtype="5", bal_change=-1.0)
        report = self.report(
            activation="2025-12-29 08:00:00", weeks=2)
        self.assertEqual("NOT_MET", report["status"])
        self.assertEqual(2, report["counts"]["eligible_complete_weeks"])
        self.assertEqual(1, report["counts"]["positive_eligible_weeks"])
        self.assertEqual(1, report["counts"]["nonpositive_eligible_weeks"])


if __name__ == "__main__":
    unittest.main()
