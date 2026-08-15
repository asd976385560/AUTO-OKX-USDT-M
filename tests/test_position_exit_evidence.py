# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts import live_decision_facts as facts  # noqa: E402
from scripts import multitimeframe_decision_evidence as evidence  # noqa: E402


def _facts() -> dict:
    stamp = int(time.time() * 1000)
    positions = [{
        "instId": "LINK-USDT-SWAP",
        "posSide": "long",
        "pos": "100",
        "avgPx": "8.473822",
        "markPx": "9.461",
        "lever": "10",
        "mgnMode": "cross",
        "posId": "P1",
        "cTime": str(stamp - 93 * 3_600_000),
        "upl": "98.7178",
        "uplRatio": "1.1649737273",
        "imr": "94.61",
    }]
    balance = [{
        "totalEq": "913.72",
        "details": [{
            "ccy": "USDT", "availEq": "550", "imr": "94.61",
            "mmr": "10", "upl": "98.7178",
        }],
    }]
    instruments = {"LINK-USDT-SWAP": {
        "instId": "LINK-USDT-SWAP", "ctVal": "1",
    }}
    algos = {"LINK-USDT-SWAP": [{
        "instId": "LINK-USDT-SWAP", "algoId": "A1",
        "slTriggerPx": "9.2", "slTriggerPxType": "mark",
        "posSide": "long", "side": "sell", "reduceOnly": "true",
        "state": "live", "sz": "100",
    }]}
    return facts.derive_facts(
        "2026-08-15T14:15", "live", positions, balance,
        instruments, algos, as_of_ms=stamp,
    )


class PositionExitEvidenceTests(unittest.TestCase):
    def _db_root(self, root: Path) -> None:
        connection = sqlite3.connect(root / "account.db")
        connection.executescript(
            "CREATE TABLE trade_experiences ("
            "id INTEGER PRIMARY KEY,cycle_id TEXT,ts TEXT,profile TEXT,"
            "symbol TEXT,side TEXT,status TEXT,open_sz REAL,"
            "remaining_sz REAL,raw TEXT);"
            "CREATE TABLE position_snapshots ("
            "ts TEXT,profile TEXT,symbol TEXT,upl REAL);"
        )
        connection.execute(
            "INSERT INTO trade_experiences VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                289, "2026-08-11T16:00", "2026-08-11 16:27:42",
                "live", "LINK-USDT-SWAP", "long", "open", 100, 100,
                json.dumps({
                    "fill_px": 8.473822,
                    "sl_trigger_px": 8.25,
                    "decision_card": {"risk_reward": {"target": 8.89}},
                }),
            ),
        )
        connection.executemany(
            "INSERT INTO position_snapshots VALUES (?,?,?,?)",
            [
                ("2026-08-15 11:00:34", "live", "LINK-USDT-SWAP", 124.3178),
                ("2026-08-15 14:15:36", "live", "LINK-USDT-SWAP", 98.0178),
            ],
        )
        connection.commit()
        connection.close()

    def test_batch_surfaces_target_r_peak_and_non_binding_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._db_root(root)
            mtf = {
                "ready": True,
                "status": "PASSED",
                "timeframes": [],
                "evidence_contract": {"evidence_hash": "fixed"},
            }
            with mock.patch.object(
                evidence, "check_multitimeframe_readiness", return_value=mtf,
            ):
                result = evidence.build_position_exit_batch(
                    root, _facts(), "2026-08-15T14:15")

        self.assertTrue(result["ok"])
        self.assertEqual(result["position_count"], 1)
        row = result["positions"][0]
        context = row["open_plan_and_path"]
        self.assertEqual(context["status"], "unique_open_leg")
        self.assertEqual(context["original_target"], 8.89)
        self.assertTrue(context["target_reached"])
        self.assertEqual(context["original_exit_mode"], "legacy_unspecified")
        self.assertAlmostEqual(context["initial_risk_usdt"], 22.3822, places=4)
        self.assertAlmostEqual(context["current_r_gross"], 4.4105, places=4)
        self.assertEqual(context["observed_peak_upl_usdt"], 124.3178)
        self.assertIn(
            "official_margin_return_at_or_above_50pct",
            context["review_flags"],
        )
        self.assertIn("original_target_reached", context["review_flags"])
        self.assertIn("current_profit_at_or_above_2r", context["review_flags"])
        self.assertIn(
            "legacy_exit_mode_requires_fresh_agent_choice",
            context["review_flags"],
        )
        self.assertTrue(context["review_flags_are_non_binding"])
        self.assertFalse(context["automatic_exit_authorized"])

    def test_tampered_facts_fail_before_market_or_account_reads(self):
        payload = copy.deepcopy(_facts())
        payload["positions"][0]["upl"] = 999
        with mock.patch.object(
            evidence, "check_multitimeframe_readiness",
        ) as readiness:
            result = evidence.build_position_exit_batch(
                Path("missing"), payload, "2026-08-15T14:15")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "FACTS_INVALID")
        readiness.assert_not_called()


if __name__ == "__main__":
    unittest.main()
