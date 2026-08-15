# -*- coding: utf-8 -*-
"""Bounded recovery contract for the daily live account-bill collector."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COLLECTORS = ROOT / "collectors"
for path in (str(SCRIPTS), str(COLLECTORS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import collect_account_bills as bills  # noqa: E402


def _bill() -> dict:
    return {
        "billId": "bill-1",
        "ts": "1786740900000",
        "instId": "BTC-USDT-SWAP",
        "instType": "SWAP",
        "ccy": "USDT",
        "type": "2",
        "subType": "1",
        "balChg": "1.2",
        "fee": "-0.1",
        "pnl": "1.3",
    }


class AccountBillsRetryTests(unittest.TestCase):
    def test_first_attempt_success_does_not_sleep(self):
        stats = {}
        sleep = mock.Mock()
        with mock.patch.object(bills, "okx_json", return_value=[_bill()]) as fetch:
            rows = bills.collect("live", 100, retry_stats=stats, sleep_fn=sleep)

        self.assertEqual(len(rows), 1)
        self.assertEqual(fetch.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(stats["attempts"], 1)
        self.assertFalse(stats["recovered_after_cold_retry"])
        self.assertFalse(stats["historical_retry"])
        self.assertFalse(stats["unbounded_retry"])

    def test_transient_failure_recovers_once_after_cold_delay(self):
        stats = {}
        sleep = mock.Mock()
        with mock.patch.object(
            bills,
            "okx_json",
            side_effect=[RuntimeError("temporary transport"), [_bill()]],
        ) as fetch:
            rows = bills.collect("live", 100, retry_stats=stats, sleep_fn=sleep)

        self.assertEqual(len(rows), 1)
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(bills._COLD_RETRY_DELAY_SECONDS)
        self.assertEqual(stats["attempts"], 2)
        self.assertTrue(stats["recovered_after_cold_retry"])
        self.assertIn("temporary transport", stats["initial_error"])

    def test_second_failure_remains_blocking_and_never_loops(self):
        stats = {}
        sleep = mock.Mock()
        with mock.patch.object(
            bills,
            "okx_json",
            side_effect=[RuntimeError("first"), RuntimeError("second")],
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "second"):
                bills.collect("live", 100, retry_stats=stats, sleep_fn=sleep)

        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(bills._COLD_RETRY_DELAY_SECONDS)
        self.assertEqual(stats["attempts"], 2)
        self.assertFalse(stats["recovered_after_cold_retry"])
        self.assertFalse(stats["historical_retry"])
        self.assertFalse(stats["unbounded_retry"])

    def test_demo_profile_is_rejected_before_network(self):
        with mock.patch.object(bills, "okx_json") as fetch:
            with self.assertRaisesRegex(ValueError, "只支持 live"):
                bills.collect("demo", 100, sleep_fn=mock.Mock())
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
