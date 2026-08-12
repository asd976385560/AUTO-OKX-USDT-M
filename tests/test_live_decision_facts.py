# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts", ROOT / "collectors"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts import (  # noqa: E402
    _okx_http,
    build_push_payload,
    collect_data,
    live_decision_facts as facts,
)
from collectors import trades_writer  # noqa: E402


def _raw_inputs(as_of_ms: int):
    ctime = as_of_ms - int(27.81 * 3_600_000)
    positions = [{
        "instId": "ETH-USDT-SWAP",
        "posSide": "long",
        "pos": "2",
        "avgPx": "1917.66",
        "markPx": "1929.01",
        "lever": "10",
        "mgnMode": "cross",
        "posId": "P1",
        "cTime": str(ctime),
        "upl": "2.27",
        "imr": "38.5774",
    }]
    balance = [{
        "totalEq": "1013.76",
        "details": [{
            "ccy": "USDT",
            "availEq": "975.18",
            "imr": "38.5774",
            "mmr": "1.54",
            "upl": "2.27",
        }],
    }]
    instruments = {"ETH-USDT-SWAP": {
        "instId": "ETH-USDT-SWAP", "ctVal": "0.1",
    }}
    algos = {"ETH-USDT-SWAP": [{
        "instId": "ETH-USDT-SWAP",
        "algoId": "A1",
        "slTriggerPx": "1888",
        "slTriggerPxType": "mark",
        "posSide": "long",
        "side": "sell",
        "reduceOnly": "true",
        "state": "live",
        "sz": "2",
    }]}
    return positions, balance, instruments, algos


class LiveDecisionFactsTests(unittest.TestCase):
    def _facts(self, as_of_ms: int | None = None):
        stamp = as_of_ms or int(time.time() * 1000)
        inputs = _raw_inputs(stamp)
        return facts.derive_facts(
            "2026-08-07T21:15", "live", *inputs, as_of_ms=stamp,
        )

    def test_contract_multiplier_age_sl_and_imr_are_deterministic(self):
        payload = self._facts()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(facts.validate_facts(payload), [])
        position = payload["positions"][0]
        self.assertEqual(position["contracts"], 2.0)
        self.assertEqual(position["ctVal"], 0.1)
        self.assertEqual(position["base_qty"], 0.2)
        self.assertAlmostEqual(position["position_age_hours"], 27.81, places=2)
        self.assertEqual(position["sl"]["trigger_px"], 1888.0)
        self.assertAlmostEqual(
            position["pnl_at_stop_from_entry_usdt"], -5.932, places=6,
        )
        self.assertAlmostEqual(
            position["additional_pnl_to_stop_from_mark_usdt"],
            -8.202,
            places=6,
        )
        self.assertAlmostEqual(
            payload["balance"]["current_portfolio_imr_ratio"],
            38.5774 / 1013.76,
            places=9,
        )
        self.assertEqual(
            payload["balance"]["max_portfolio_imr_ratio"], 0.666,
        )
        self.assertEqual(
            payload["balance"]["portfolio_margin_state"], "low_usage")
        self.assertEqual(
            payload["balance"]["portfolio_margin_label_cn"], "占用较低")
        self.assertAlmostEqual(
            payload["balance"]["portfolio_imr_cap_utilization"],
            (38.5774 / 1013.76) / 0.666,
            places=9,
        )
        self.assertEqual(
            payload["balance"]["single_order_budget_scope"],
            "next_incremental_open_or_add_order",
        )
        self.assertFalse(
            payload["balance"][
                "single_order_budget_reduced_by_existing_position"
            ]
        )

    def test_margin_wording_state_uses_cap_utilization(self):
        stamp = int(time.time() * 1000)
        positions, balance, instruments, algos = _raw_inputs(stamp)
        balance[0]["details"][0]["imr"] = "620"
        payload = facts.derive_facts(
            "2026-08-07T21:15", "live", positions, balance,
            instruments, algos, as_of_ms=stamp,
        )
        self.assertEqual(
            payload["balance"]["portfolio_margin_state"], "near_cap")
        self.assertEqual(
            payload["balance"]["portfolio_margin_label_cn"], "接近上限")

    def test_missing_full_size_live_sl_is_blocking(self):
        stamp = int(time.time() * 1000)
        positions, balance, instruments, _algos = _raw_inputs(stamp)
        payload = facts.derive_facts(
            "2026-08-07T21:15",
            "live",
            positions,
            balance,
            instruments,
            {"ETH-USDT-SWAP": []},
            as_of_ms=stamp,
        )
        self.assertEqual(payload["status"], "blocking")
        self.assertTrue(any("sl_missing" in item for item in payload["errors"]))
        allowed = payload["action_policy"]["allowed_executor_actions"]
        self.assertNotIn("open", allowed)
        self.assertNotIn("add", allowed)
        self.assertIn("close", allowed)
        self.assertIn("reduce", allowed)
        self.assertTrue(facts.validate_facts(payload, require_ok=True))

    def test_hash_and_raw_snapshot_recalculation_detect_tamper(self):
        payload = self._facts()
        tampered = copy.deepcopy(payload)
        tampered["positions"][0]["ctVal"] = 1.0
        errors = facts.validate_facts(tampered)
        self.assertTrue(any("facts_hash" in item for item in errors))
        self.assertTrue(any("推导结果不一致" in item for item in errors))

    def test_strict_receipt_rejects_schema_drift_and_stale_rules(self):
        payload = self._facts()
        receipt = {
            "cycle_id": payload["cycle_id"],
            "mode": "live",
            "status": "ok",
            "decision": "hold",
            "action_taken": "HOLD",
            "n_orders": 0,
            "equity": payload["balance"]["totalEq"],
            "regime": "range",
            "trades": [],
            "errors": [],
            "live_facts": payload,
        }
        self.assertEqual(
            trades_writer.validate_strict_live_receipt(receipt), [],
        )
        broken = copy.deepcopy(receipt)
        broken.pop("action_taken")
        broken["note"] = "同侧集中度 60% 硬上限；IMR 阈值 0.0666"
        errors = trades_writer.validate_strict_live_receipt(broken)
        self.assertTrue(any("action_taken" in item for item in errors))
        self.assertTrue(any("0.0666" in item for item in errors))
        self.assertTrue(any("60%" in item for item in errors))

    def test_push_loader_keeps_already_decoded_trade_card(self):
        card = {"agent_judgement": "upl=-0.058"}
        self.assertIs(build_push_payload._loads(card), card)
        self.assertEqual(
            build_push_payload._loads('{"agent_judgement":"upl=-0.058"}'),
            card,
        )


class HttpDeadlineTests(unittest.TestCase):
    def test_funding_any_and_fallback_share_one_deadline(self):
        client_cm = mock.MagicMock()
        client_cm.__enter__.return_value = mock.Mock()
        with (
            mock.patch.object(_okx_http, "_client", return_value=client_cm),
            mock.patch.object(
                _okx_http, "_get_data", side_effect=RuntimeError("ANY failed")
            ) as get_data,
            mock.patch.object(_okx_http, "_batch", return_value={}) as batch,
        ):
            _okx_http.fetch_funding_rates_batch_sync(
                ["BTC-USDT-SWAP"], batch_timeout_s=10,
            )
        deadline = get_data.call_args.kwargs["deadline"]
        self.assertIsNotNone(deadline)
        remaining = batch.call_args.kwargs["batch_timeout_s"]
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, 10)

    def test_ticker_writer_does_not_persist_empty_placeholder_rows(self):
        class FakeConnection:
            def __init__(self):
                self.batches = []

            def executemany(self, _sql, rows):
                self.batches.append(list(rows))

            def commit(self):
                return None

        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        connection = FakeConnection()
        with (
            mock.patch.object(collect_data, "SYMBOLS", symbols),
            mock.patch.object(
                collect_data,
                "fetch_tickers_all_sync",
                return_value=[
                    {"instId": "BTC-USDT-SWAP", "last": "65000"},
                    {"instId": "ETH-USDT-SWAP", "last": ""},
                ],
            ),
            mock.patch.object(
                collect_data,
                "fetch_funding_rates_batch_sync",
                return_value={
                    "BTC-USDT-SWAP": {"fundingRate": "0.0001"},
                    "ETH-USDT-SWAP": {},
                },
            ),
            mock.patch.object(
                collect_data, "fetch_open_interest_all_sync", return_value={}
            ),
        ):
            count, _snapshot, quality = collect_data.collect_tickers(
                connection, "2026-08-07T21:15:00+08:00", 10,
            )
        self.assertEqual(count, 1)
        self.assertEqual(quality["tickers"], 1)
        self.assertEqual(quality["funding"], 1)
        self.assertEqual(len(connection.batches[0]), 1)
        self.assertEqual(len(connection.batches[1]), 1)


if __name__ == "__main__":
    unittest.main()
