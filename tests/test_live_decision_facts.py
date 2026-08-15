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
        "uplRatio": "0.05919",
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


def _verified_protection_change():
    """Representative non-dry executor receipt fields after exact readback."""
    return {
        "symbol": "ETH-USDT-SWAP",
        "pos_side": "long",
        "protection_change": {
            "reason_code": "agent_adjust",
            "requested_sl": 1895.0,
            "requested_tp": None,
            "resize_to_full_position": False,
        },
        "path": "amend",
        "protection_state": {
            "ok": True,
            "live_sl_count": 1,
            "matched_count": 1,
            "duplicate_sl": False,
            "naked": False,
        },
        "applied": {
            "sl": 1895.0,
            "tp": None,
            "sz": 2.0,
            "algoId": "A1",
        },
    }


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
        self.assertEqual(position["upl_ratio_source"],
                         "exchange.positions.uplRatio")
        self.assertAlmostEqual(
            position["upl_pct_initial_margin"], 5.919, places=3)
        self.assertFalse(
            position["margin_return_review_at_or_above_50pct"])
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

    def test_high_margin_return_is_review_attention_not_auto_exit(self):
        stamp = int(time.time() * 1000)
        positions, balance, instruments, algos = _raw_inputs(stamp)
        positions[0].update({
            "avgPx": "100", "markPx": "110", "upl": "2",
            "uplRatio": "1.0", "imr": "2",
        })
        instruments["ETH-USDT-SWAP"]["ctVal"] = "0.1"
        algos["ETH-USDT-SWAP"][0]["slTriggerPx"] = "105"
        payload = facts.derive_facts(
            "2026-08-07T21:15", "live", positions, balance,
            instruments, algos, as_of_ms=stamp,
        )
        position = payload["positions"][0]
        self.assertEqual(position["upl_pct_initial_margin"], 100.0)
        self.assertEqual(
            position["signed_price_return_pct_from_entry"], 10.0)
        self.assertTrue(
            position["margin_return_review_at_or_above_50pct"])
        self.assertEqual(position["secured_profit_at_stop_usdt"], 1.0)
        self.assertEqual(
            position["profit_retention_at_stop_pct_of_current_upl"], 50.0)
        self.assertEqual(
            position["giveback_to_stop_pct_of_current_upl"], 50.0)
        policy = payload["position_profit_review_policy"]
        self.assertTrue(policy["attention_flag_is_non_binding"])
        self.assertFalse(policy["automatic_close_authorized"])

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
        self.assertIn("adjust_protection", allowed)
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

    def test_adjust_protection_is_a_formal_zero_fill_action(self):
        payload = self._facts()
        receipt = {
            "cycle_id": payload["cycle_id"],
            "mode": "live",
            "status": "ok",
            "decision": "hold",
            "action_taken": "ADJUST_PROTECTION",
            "n_orders": 0,
            "equity": payload["balance"]["totalEq"],
            "regime": "range",
            "trades": [],
            "errors": [],
            "live_facts": payload,
            "decision_protocol": "decision_card_v1",
            **_verified_protection_change(),
            "decision_card": {
                "direction_evidence": ["position still valid"],
                "opposing_evidence": ["volatility expanded"],
                "execution_conditions": {"status": "move protection"},
                "invalidation_point": {"condition": "structure invalidates"},
                "risk_reward": {"summary": "agent-managed exit"},
                "portfolio_impact": {"summary": "quantity unchanged"},
                "historical_experience": {
                    "matched_wins": [], "matched_losses": [],
                    "missed_opportunities": [], "usage": "none",
                    "reason": "no comparable sample",
                },
                "agent_judgement": "loosen stop and keep position",
                "reference_overrides": [],
            },
        }
        self.assertEqual(
            trades_writer.validate_strict_live_receipt(receipt), [])
        self.assertEqual(trades_writer.validate(receipt), [])

    def test_blocking_facts_allow_only_whitelisted_position_actions(self):
        stamp = int(time.time() * 1000)
        positions, balance, instruments, _algos = _raw_inputs(stamp)
        blocking = facts.derive_facts(
            "2026-08-07T21:15", "live", positions, balance, instruments,
            {"ETH-USDT-SWAP": []}, as_of_ms=stamp,
        )
        self.assertEqual(blocking["status"], "blocking")
        common = {
            "cycle_id": blocking["cycle_id"], "mode": "live",
            "status": "ok", "decision": "hold", "n_orders": 0,
            "equity": blocking["balance"]["totalEq"], "regime": "range",
            "trades": [], "errors": [], "live_facts": blocking,
        }
        allowed = {
            **common, "action_taken": "ADJUST_PROTECTION",
            **_verified_protection_change(),
        }
        self.assertEqual(
            trades_writer.validate_strict_live_receipt(allowed), [])
        forbidden = {**common, "action_taken": "HOLD"}
        self.assertTrue(any(
            "blocking" in item
            for item in trades_writer.validate_strict_live_receipt(forbidden)))

    def test_position_action_must_be_in_ok_facts_whitelist(self):
        payload = self._facts()
        payload = copy.deepcopy(payload)
        payload["action_policy"]["allowed_executor_actions"] = ["open", "add"]
        payload["facts_hash"] = facts._hash_payload(payload)
        receipt = {
            "cycle_id": payload["cycle_id"], "mode": "live",
            "status": "ok", "decision": "hold", "n_orders": 0,
            "equity": payload["balance"]["totalEq"], "regime": "range",
            "trades": [], "errors": [], "live_facts": payload,
            "action_taken": "ADJUST_PROTECTION",
            **_verified_protection_change(),
        }
        errors = trades_writer.validate_strict_live_receipt(receipt)
        self.assertTrue(any("未获" in item and "adjust_protection" in item
                            for item in errors), errors)

    def test_push_action_preserves_protection_adjustment(self):
        self.assertEqual(
            build_push_payload._action_from_cycle(
                {
                    "action_taken": "ADJUST_PROTECTION",
                    **_verified_protection_change(),
                }, "hold"),
            "ADJUST",
        )

    def test_verified_adjustment_execution_exposes_readback_not_fake_fill(self):
        raw = {
            "action_taken": "ADJUST_PROTECTION",
            **_verified_protection_change(),
        }
        raw["protection_state"]["rows"] = [{"state": "live"}]

        execution = build_push_payload._verified_adjustment_execution(
            raw, "hold")

        self.assertIsNotNone(execution)
        self.assertEqual(execution["fill_px"], "no_fill")
        self.assertEqual(execution["stop_px"], 1895.0)
        for token in (
            "ADJUST_PROTECTION", "no_fill", "path=amend", "sz=2",
            "SL=1895", "algoId=A1", "readback=verified/live",
            "exchange_side_effect=protection_only",
        ):
            self.assertIn(token, execution["result"])
        self.assertIsNone(build_push_payload._verified_adjustment_execution(
            {"action_taken": "ADJUST_PROTECTION"}, "hold"))

    def test_unverified_or_legacy_adjust_is_reported_as_hold(self):
        self.assertEqual(
            build_push_payload._action_from_cycle(
                {"action_taken": "ADJUST_PROTECTION"}, "hold"),
            "HOLD",
        )
        self.assertEqual(
            build_push_payload._action_from_cycle(
                {"action_taken": "ADJUST"}, "hold"),
            "HOLD",
        )
        payload = self._facts()
        receipt = {
            "cycle_id": payload["cycle_id"], "mode": "live",
            "status": "ok", "decision": "hold", "n_orders": 0,
            "equity": payload["balance"]["totalEq"], "regime": "range",
            "trades": [], "errors": [], "live_facts": payload,
            "action_taken": "ADJUST",
        }
        errors = trades_writer.validate_strict_live_receipt(receipt)
        self.assertTrue(any("模糊 ADJUST" in item for item in errors), errors)

    def test_push_execution_preserves_all_verified_batch_adjustments(self):
        first = {
            "action_taken": "ADJUST_PROTECTION",
            **_verified_protection_change(),
            "symbol": "LINK-USDT-SWAP",
        }
        second = {
            "action_taken": "ADJUST_PROTECTION",
            **_verified_protection_change(),
            "symbol": "AAVE-USDT-SWAP",
        }
        second["applied"] = {**second["applied"], "sl": 91.2, "algoId": "A2"}
        raw = {
            "action_taken": "CLOSE",
            "protection_changes": [first, second],
        }

        execution = build_push_payload._verified_adjustment_execution(
            raw, "traded")

        self.assertIsNotNone(execution)
        self.assertIn("batch=2", execution["result"])
        self.assertIn("LINK-USDT-SWAP", execution["result"])
        self.assertIn("AAVE-USDT-SWAP", execution["result"])
        self.assertIn("SL=91.2", execution["result"])

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
                collect_data, "fetch_tickers_batch_sync", return_value={}
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
        # A returned instrument ID with no usable official price is incomplete
        # and receives bounded cold + single-ticker phases; it still must not
        # be stored when both current-cycle recovery sources remain empty.
        self.assertEqual(quality["ticker_transport"]["attempts"], 3)
        self.assertFalse(
            quality["ticker_transport"]["recovered_after_cold_retry"])

    def test_ticker_transport_cold_retries_with_new_fetch_phase(self):
        class FakeConnection:
            def __init__(self):
                self.batches = []

            def executemany(self, _sql, rows):
                self.batches.append(list(rows))

            def commit(self):
                return None

        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        recovered = [
            {"instId": "BTC-USDT-SWAP", "last": "65000"},
            {"instId": "ETH-USDT-SWAP", "last": "3500"},
        ]
        connection = FakeConnection()
        with (
            mock.patch.object(collect_data, "SYMBOLS", symbols),
            mock.patch.object(
                collect_data, "fetch_tickers_all_sync",
                side_effect=[RuntimeError("tls eof"), recovered],
            ) as fetch,
            mock.patch.object(
                collect_data, "fetch_funding_rates_batch_sync", return_value={}
            ),
            mock.patch.object(
                collect_data, "fetch_open_interest_all_sync", return_value={}
            ),
            mock.patch.object(collect_data.time, "sleep") as sleep,
        ):
            count, _snapshot, quality = collect_data.collect_tickers(
                connection, "2026-08-13T17:15:00+08:00", 135,
            )
        self.assertEqual(count, 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertLessEqual(
            fetch.call_args_list[0].args[0],
            collect_data.TICKER_INITIAL_TIMEOUT_SECONDS,
        )
        self.assertLessEqual(
            fetch.call_args_list[1].args[0],
            collect_data.TICKER_COLD_RETRY_TIMEOUT_SECONDS,
        )
        sleep.assert_called_once()
        transport = quality["ticker_transport"]
        self.assertEqual(transport["attempts"], 2)
        self.assertEqual(transport["initial_error_type"], "RuntimeError")
        self.assertTrue(transport["recovered_after_cold_retry"])
        self.assertFalse(transport["historical_retry"])
        self.assertFalse(transport["unbounded_retry"])
        self.assertIn("schannel_fallback_requested", transport)
        self.assertIn("schannel_fallback_successes", transport)

    def test_ticker_transport_keeps_better_partial_response(self):
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        first = [{"instId": "BTC-USDT-SWAP", "last": "65000"}]
        with (
            mock.patch.object(collect_data, "SYMBOLS", symbols),
            mock.patch.object(
                collect_data, "fetch_tickers_all_sync",
                side_effect=[first, RuntimeError("still down")],
            ),
            mock.patch.object(
                collect_data, "fetch_tickers_batch_sync", return_value={}
            ),
            mock.patch.object(collect_data.time, "sleep"),
        ):
            rows, transport = collect_data._fetch_tickers_with_cold_retry(
                time.monotonic() + 135,
            )
        self.assertEqual(rows, first)
        self.assertEqual(transport["selected_coverage_rate"], 0.5)
        self.assertEqual(transport["cold_retry_error_type"], "RuntimeError")
        self.assertFalse(transport["recovered_after_cold_retry"])
        self.assertEqual(transport["attempts"], 3)
        self.assertTrue(transport["single_ticker_fallback_requested"])

    def test_ticker_transport_single_ticker_fallback_recovers_outage(self):
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        fallback = {
            "BTC-USDT-SWAP": {
                "instId": "BTC-USDT-SWAP", "last": "65000",
            },
            "ETH-USDT-SWAP": {
                "instId": "ETH-USDT-SWAP", "last": "3500",
            },
        }
        with (
            mock.patch.object(collect_data, "SYMBOLS", symbols),
            mock.patch.object(
                collect_data, "fetch_tickers_all_sync",
                side_effect=[RuntimeError("first"), RuntimeError("second")],
            ),
            mock.patch.object(
                collect_data, "fetch_tickers_batch_sync",
                return_value=fallback,
            ) as batch,
            mock.patch.object(collect_data.time, "sleep"),
        ):
            rows, transport = collect_data._fetch_tickers_with_cold_retry(
                time.monotonic() + 135,
            )
        self.assertEqual({row["instId"] for row in rows}, set(symbols))
        self.assertEqual(transport["attempts"], 3)
        self.assertTrue(transport["single_ticker_fallback_requested"])
        self.assertEqual(transport["single_ticker_fallback_usable"], 2)
        self.assertTrue(
            transport["recovered_after_single_ticker_fallback"]
        )
        self.assertLessEqual(
            batch.call_args.kwargs["batch_timeout_s"],
            collect_data.TICKER_SINGLE_FALLBACK_TIMEOUT_SECONDS,
        )
        self.assertEqual(batch.call_args.args[0], symbols)

    def test_ticker_transport_combines_disjoint_current_responses(self):
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        first = [{"instId": "BTC-USDT-SWAP", "last": "65000"}]
        retry = [{"instId": "ETH-USDT-SWAP", "last": "3500"}]
        with (
            mock.patch.object(collect_data, "SYMBOLS", symbols),
            mock.patch.object(
                collect_data, "fetch_tickers_all_sync",
                side_effect=[first, retry],
            ),
            mock.patch.object(collect_data.time, "sleep"),
        ):
            rows, transport = collect_data._fetch_tickers_with_cold_retry(
                time.monotonic() + 135,
            )
        self.assertEqual(
            {row["instId"] for row in rows}, set(symbols))
        self.assertEqual(transport["initial_coverage_rate"], 0.5)
        self.assertEqual(transport["cold_retry_coverage_rate"], 0.5)
        self.assertEqual(transport["selected_coverage_rate"], 1.0)
        self.assertTrue(transport["recovered_after_cold_retry"])

    def test_ticker_transport_fails_closed_before_write_when_both_fail(self):
        with (
            mock.patch.object(collect_data, "SYMBOLS", ["BTC-USDT-SWAP"]),
            mock.patch.object(
                collect_data, "fetch_tickers_all_sync",
                side_effect=[RuntimeError("first"), RuntimeError("second")],
            ) as fetch,
            mock.patch.object(
                collect_data, "fetch_tickers_batch_sync", return_value={}
            ) as batch,
            mock.patch.object(collect_data.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                    RuntimeError, "failed after bounded cold retry"):
                collect_data._fetch_tickers_with_cold_retry(
                    time.monotonic() + 135,
                )
        self.assertEqual(fetch.call_count, 2)
        batch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
