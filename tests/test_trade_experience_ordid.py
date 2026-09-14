from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import trade_experience_writer as writer  # noqa: E402


class TradeExperienceOrderIdentityTests(unittest.TestCase):
    def test_direct_order_identity_remains_authoritative(self):
        self.assertEqual("direct-1", writer._trade_ordid({
            "ordId": "direct-1",
            "raw": {"ord_ids": ["nested-2"]},
        }))

    def test_unique_reconcile_identity_is_recovered_from_raw(self):
        trade = {
            "raw": json.dumps({
                "ord_ids": ["3849481331071012864"],
                "fills": [{"ordId": "3849481331071012864"}],
            })
        }
        self.assertEqual("3849481331071012864", writer._trade_ordid(trade))
        event = writer._close_event(
            trade, "2026-08-20T19:30", "2026-08-20 19:38:03",
            2.27, 18.0692)
        self.assertEqual("3849481331071012864", event["ordId"])

    def test_multiple_reconcile_order_ids_remain_ambiguous(self):
        trade = {"raw": {
            "ord_ids": ["a", "b"],
            "fills": [{"ordId": "a"}, {"ordId": "b"}],
        }}
        self.assertIsNone(writer._trade_ordid(trade))


if __name__ == "__main__":
    unittest.main()
