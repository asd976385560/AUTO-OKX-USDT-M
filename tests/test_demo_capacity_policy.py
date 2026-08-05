# -*- coding: utf-8 -*-
"""Demo 开仓容量与 profile 风控分流回归。

全部交易所 I/O 和执行意图写入均使用 mock；本测试不会读取生产数据库、不会下单。
"""
from __future__ import annotations

import math
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
for module_path in (ROOT, CORE):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

import account_capacity as ac  # noqa: E402
import order_executor as oe  # noqa: E402
import risk_validator as rv  # noqa: E402


def _valid_receipt_context(cycle_id: str) -> dict:
    return {
        "cycle_id": cycle_id,
        "status": "ok",
        "decision": "traded",
        "decision_protocol": "decision_card_v1",
        "decision_card": {
            "direction_evidence": ["isolated demo capacity regression"],
            "opposing_evidence": ["all exchange writes are mocked"],
            "execution_conditions": {"status": "mocked endpoints"},
            "invalidation_point": {"condition": "contract mismatch"},
            "risk_reward": {"summary": "test-only"},
            "portfolio_impact": {"summary": "isolated temporary state"},
            "historical_experience": {
                "matched_wins": [],
                "matched_losses": [],
                "missed_opportunities": [],
                "usage": "none",
                "reason": "deterministic unit test",
            },
            "agent_judgement": "exercise demo capacity policy",
            "reference_overrides": [],
        },
    }


class OkxMaxSizeAdapterTests(unittest.TestCase):
    def test_demo_max_size_uses_profile_scoped_account_endpoint(self):
        response = {
            "code": "0",
            "msg": "",
            "data": [{
                "instId": "BTC-USDT-SWAP",
                "maxBuy": "2.91",
                "maxSell": "2.97",
            }],
        }
        with mock.patch.object(oe.ox, "okx_json", return_value=response) as cli:
            payload = oe.ox.get_max_size(
                "BTC-USDT-SWAP", "cross", "demo")

        self.assertTrue(payload["ok"])
        args, kwargs = cli.call_args
        self.assertEqual(
            args,
            (
                "account", "max-size",
                "--instId", "BTC-USDT-SWAP",
                "--tdMode", "cross",
            ),
        )
        self.assertEqual(kwargs["global_args"], ["--profile", "demo"])

    def test_directional_max_size_selects_buy_for_long_and_sell_for_short(self):
        payload = {
            "ok": True,
            "data": [{
                "instId": "BTC-USDT-SWAP",
                "maxBuy": "2.91",
                "maxSell": "2.97",
            }],
        }
        long_capacity = ac.extract_directional_max_size(
            payload, "BTC-USDT-SWAP", "long")
        short_capacity = ac.extract_directional_max_size(
            payload, "BTC-USDT-SWAP", "short")

        self.assertTrue(long_capacity["ok"])
        self.assertTrue(short_capacity["ok"])
        self.assertEqual(long_capacity["max_size"], 2.91)
        self.assertEqual(short_capacity["max_size"], 2.97)
        self.assertIn("maxBuy", long_capacity["source"])
        self.assertIn("maxSell", short_capacity["source"])

    def test_zero_is_authoritative_but_bad_or_missing_selected_side_fails(self):
        zero = ac.extract_directional_max_size(
            {
                "ok": True,
                "data": [{
                    "instId": "BTC-USDT-SWAP",
                    "maxBuy": "0",
                    "maxSell": "1",
                }],
            },
            "BTC-USDT-SWAP",
            "long",
        )
        self.assertTrue(zero["ok"])
        self.assertEqual(zero["max_size"], 0.0)

        bad_values = (None, "", "-0.1", "nan", "inf", object())
        for value in bad_values:
            with self.subTest(value=repr(value)):
                result = ac.extract_directional_max_size(
                    {
                        "ok": True,
                        "data": [{
                            "instId": "BTC-USDT-SWAP",
                            "maxBuy": value,
                            "maxSell": "99",
                        }],
                    },
                    "BTC-USDT-SWAP",
                    "long",
                )
                self.assertFalse(result["ok"])
                self.assertIsNone(result["max_size"])
                self.assertTrue(result["error"])

    def test_wrong_instrument_or_failed_payload_never_supplies_capacity(self):
        cases = (
            {"ok": False, "error": "endpoint unavailable", "data": []},
            {
                "ok": True,
                "data": [{
                    "instId": "ETH-USDT-SWAP",
                    "maxBuy": "9",
                    "maxSell": "9",
                }],
            },
            {"ok": True, "data": "not-a-list"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                result = ac.extract_directional_max_size(
                    payload, "BTC-USDT-SWAP", "long")
                self.assertFalse(result["ok"])
                self.assertIsNone(result["max_size"])


class DemoRiskPolicyTests(unittest.TestCase):
    @staticmethod
    def _demo_validate(**overrides):
        kwargs = {
            "symbol": "BTC-USDT-SWAP",
            "side": "long",
            "intended_sz": 0.1,
            "lev": 5.0,
            "mark_px": 100.0,
            "ct_val": 1.0,
            "lot_sz": 0.1,
            # 旧规则会把 0.1 张上调至 10 张；Demo 新规则不得使用该净值下限。
            "equity": 100_000.0,
            # 旧 98% 可用保证金规则也不得参与 Demo approve/reject/clamp。
            "available_margin": 0.0,
            "open_positions": [],
            "sl_trigger_px": 95.0,
            "profile": "demo",
            "exchange_max_size": 100.0,
            "min_order_size": 0.1,
        }
        kwargs.update(overrides)
        return rv.validate(**kwargs)

    def test_demo_below_old_one_percent_floor_is_not_raised(self):
        result = self._demo_validate()
        self.assertTrue(result["approved"])
        self.assertEqual(result["approved_sz"], 0.1)
        self.assertFalse(result["clamped"])
        self.assertNotIn(
            "名义 1% 下限", "；".join(result.get("adjustments") or []))

    def test_demo_above_old_twenty_percent_and_ninety_eight_percent_caps_passes(self):
        result = self._demo_validate(
            intended_sz=20.0,
            lot_sz=1.0,
            min_order_size=1.0,
            exchange_max_size=20.0,
            equity=1_000.0,
            available_margin=1.0,
        )
        self.assertTrue(result["approved"])
        self.assertEqual(result["approved_sz"], 20.0)
        self.assertFalse(result["clamped"])

    def test_demo_only_clamps_to_exchange_physical_ceiling(self):
        result = self._demo_validate(
            intended_sz=30.0,
            lot_sz=0.1,
            min_order_size=0.1,
            exchange_max_size=20.13,
            equity=1_000.0,
            available_margin=1.0,
        )
        self.assertTrue(result["approved"])
        self.assertEqual(result["approved_sz"], 20.1)
        self.assertTrue(result["clamped"])
        adjustment_text = "；".join(result.get("adjustments") or [])
        self.assertTrue(
            "exchange" in adjustment_text.lower() or "交易所" in adjustment_text,
            adjustment_text,
        )
        self.assertNotIn("20% 上限", adjustment_text)
        self.assertNotIn("可用保证金上限", adjustment_text)

    def test_demo_lot_rounding_is_not_an_equity_floor(self):
        result = self._demo_validate(
            intended_sz=0.149,
            lot_sz=0.01,
            min_order_size=0.01,
            exchange_max_size=1.0,
        )
        self.assertTrue(result["approved"])
        self.assertEqual(result["approved_sz"], 0.14)
        self.assertTrue(result["clamped"])

    def test_demo_requires_finite_nonnegative_exchange_capacity(self):
        for capacity in (None, -1.0, math.nan, math.inf):
            with self.subTest(capacity=capacity):
                result = self._demo_validate(exchange_max_size=capacity)
                self.assertFalse(result["approved"])
                self.assertIn(
                    "exchange_max_size",
                    str(result.get("reject_reason") or ""),
                )

    def test_demo_rejects_impossible_exchange_minimum_and_maximum_range(self):
        result = self._demo_validate(
            intended_sz=1.0,
            min_order_size=2.0,
            exchange_max_size=1.0,
        )
        self.assertFalse(result["approved"])
        self.assertIsNone(result["approved_sz"])

    def test_live_keeps_equity_floor_and_available_margin_policy(self):
        oversized = rv.validate(
            symbol="BTC-USDT-SWAP",
            side="long",
            intended_sz=20.0,
            lev=5.0,
            mark_px=100.0,
            ct_val=1.0,
            lot_sz=1.0,
            equity=1_000.0,
            available_margin=1_000.0,
            account_imr=0.0,
            open_positions=[],
            sl_trigger_px=95.0,
            profile="live",
            # Demo-only physical ceiling must not silently replace Live policy.
            exchange_max_size=1.0,
            min_order_size=None,
        )
        self.assertTrue(oversized["approved"])
        self.assertEqual(oversized["approved_sz"], 20.0)
        self.assertFalse(oversized["clamped"])

        undersized = rv.validate(
            symbol="BTC-USDT-SWAP",
            side="long",
            intended_sz=0.1,
            lev=5.0,
            mark_px=100.0,
            ct_val=1.0,
            lot_sz=0.1,
            equity=10_000.0,
            available_margin=10_000.0,
            account_imr=0.0,
            open_positions=[],
            sl_trigger_px=95.0,
            profile="live",
            exchange_max_size=None,
            min_order_size=None,
        )
        self.assertTrue(undersized["approved"])
        self.assertEqual(undersized["approved_sz"], 1.0)
        self.assertTrue(undersized["clamped"])


class DemoOpenPositionCapacityFlowTests(unittest.TestCase):
    def _run_demo_open(
        self,
        *,
        side: str = "long",
        intended_sz: float = 1.0,
        lev: float = 5.0,
        sl_trigger_px: float = 95.0,
        existing_positions=None,
        max_size_payload=None,
        set_leverage_ok: bool = True,
        specs=None,
        position_check=None,
    ):
        symbol = "BTC-USDT-SWAP"
        cycle = "2026-07-29T18:00"
        positions = list(existing_positions or [])
        max_size_payload = max_size_payload or {
            "ok": True,
            "data": [{
                "instId": symbol,
                "maxBuy": "5",
                "maxSell": "4",
            }],
        }
        specs = specs or {
            "ct_val": 1.0,
            "lot_sz": 1.0,
            "min_sz": 1.0,
            "source": "demo_test",
            "spec_source": "demo_test",
        }
        position_check = position_check or {
            "ok": True,
            "profile": "demo",
            "ledger_groups": len(positions),
            "exchange_groups": len(positions),
            "diffs": [],
        }

        events: list[str] = []
        validate_kwargs: dict = {}
        original_validate = rv.validate

        def set_leverage(*args, **kwargs):
            events.append("set_leverage")
            if set_leverage_ok:
                return {"ok": True, "sCode": "0", "data": []}
            return {
                "ok": False,
                "sCode": "51000",
                "sMsg": "isolated set leverage failure",
                "data": [],
            }

        def get_max_size(*args, **kwargs):
            events.append("get_max_size")
            return max_size_payload

        def validate(*args, **kwargs):
            events.append("validate")
            validate_kwargs.update(kwargs)
            return original_validate(*args, **kwargs)

        def place_market_open(*args, **kwargs):
            events.append("place_market_open")
            return {
                "ok": True,
                "sCode": "0",
                "sMsg": "",
                "sl_attached": True,
                "data": [{"ordId": "DEMO-ORDER-1", "sCode": "0"}],
            }

        intent_mocks = {
            "mark_submitting": mock.Mock(),
            "mark_submitted": mock.Mock(),
            "mark_completed": mock.Mock(),
            "mark_failed_clean": mock.Mock(),
            "mark_uncertain": mock.Mock(),
        }
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                oe.ox, "is_dryrun", return_value=False))
            stack.enter_context(mock.patch.object(
                oe.ei, "reserve",
                return_value={"status": "reserved", "fingerprint": "FP-DEMO"}))
            for name, method_mock in intent_mocks.items():
                stack.enter_context(mock.patch.object(
                    oe.ei, name, method_mock))
            balance_mock = stack.enter_context(mock.patch.object(
                oe.ox,
                "get_balance",
                return_value={
                    "ok": True,
                    "data": [{
                        "totalEq": "1000",
                        # Demo 容量不得依赖 Live 的 details.USDT 计算公式。
                        "details": [],
                    }],
                },
            ))
            stack.enter_context(mock.patch.object(
                oe, "fetch_open_positions", return_value=positions))
            stack.enter_context(mock.patch.object(
                oe, "_verify_pretrade_ledger_positions",
                return_value=position_check))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_mark_price", return_value=100.0))
            stack.enter_context(mock.patch.object(
                oe, "fetch_instrument_specs", return_value=specs))
            stack.enter_context(mock.patch.object(
                oe.ox, "set_leverage", side_effect=set_leverage))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_max_size",
                side_effect=get_max_size,
                create=True,
            ))
            stack.enter_context(mock.patch.object(
                oe.rv, "validate", side_effect=validate))
            place_mock = stack.enter_context(mock.patch.object(
                oe.ox, "place_market_open",
                side_effect=place_market_open))
            stack.enter_context(mock.patch.object(
                oe, "_verify_sl_placed",
                return_value={"verified": True, "found": [], "matched": {}}))
            stack.enter_context(mock.patch.object(
                oe, "_read_fills",
                return_value={
                    "ok": True,
                    "fill_px": 100.0,
                    "fill_sz": intended_sz,
                    "pnl": 0.0,
                    "n": 1,
                    "fill_ts": "2026-07-29 18:00:01",
                    "ts_source": "fills.fillTime",
                }))
            stack.enter_context(mock.patch.object(
                oe, "_journal_fill"))
            stack.enter_context(mock.patch.object(
                oe, "_enqueue_repair"))

            result = oe.open_position(
                symbol=symbol,
                side=side,
                intended_sz=intended_sz,
                lev=lev,
                sl_trigger_px=sl_trigger_px,
                profile="demo",
                mgn_mode="cross",
                # caller 注入必须被非 dry-run 执行路径忽略。
                equity=999_999.0,
                available_margin=999_999.0,
                open_positions=[{
                    "symbol": "CALLER-ONLY",
                    "side": "short",
                    "sz": 999.0,
                }],
                db_root=Path(td),
                cycle_id=cycle,
                receipt_context=_valid_receipt_context(cycle),
            )

        return {
            "result": result,
            "events": events,
            "validate_kwargs": validate_kwargs,
            "place_mock": place_mock,
            "balance_mock": balance_mock,
            **intent_mocks,
        }

    def test_new_demo_open_sets_leverage_then_reads_capacity_then_validates_and_places(self):
        state = self._run_demo_open()
        result = state["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(
            state["events"],
            [
                "validate",  # 公共安全预检，必须先于修改杠杆
                "set_leverage", "get_max_size",
                "validate",  # 带交易所 max-size 的完整定仓
                "place_market_open",
            ],
        )
        self.assertEqual(
            state["validate_kwargs"]["exchange_max_size"], 5.0)
        self.assertEqual(state["validate_kwargs"]["min_order_size"], 1.0)
        self.assertEqual(state["validate_kwargs"]["profile"], "demo")
        self.assertEqual(state["result"]["capacity"]["max_size"], 5.0)
        self.assertNotEqual(
            state["result"]["capacity"].get("source"), "caller_dryrun")
        state["balance_mock"].assert_not_called()

    def test_demo_set_leverage_failure_is_fail_closed_before_capacity_and_order(self):
        state = self._run_demo_open(set_leverage_ok=False)
        self.assertFalse(state["result"]["ok"])
        self.assertEqual(
            state["result"]["reject_reason"], "set_leverage_failed")
        self.assertEqual(state["events"], ["validate", "set_leverage"])
        state["place_mock"].assert_not_called()
        state["mark_failed_clean"].assert_called_once()

    def test_demo_capacity_failure_is_failed_clean_before_validate_and_order(self):
        state = self._run_demo_open(max_size_payload={
            "ok": False,
            "error": "isolated max-size failure",
            "data": [],
        })
        self.assertFalse(state["result"]["ok"])
        self.assertEqual(
            state["events"], ["validate", "set_leverage", "get_max_size"])
        self.assertIn(
            "max_size", str(state["result"].get("reject_reason") or ""))
        state["place_mock"].assert_not_called()
        state["mark_failed_clean"].assert_called_once()

    def test_demo_missing_directional_capacity_never_falls_back_to_balance(self):
        state = self._run_demo_open(max_size_payload={
            "ok": True,
            "data": [{
                "instId": "BTC-USDT-SWAP",
                "maxSell": "4",
            }],
        })
        self.assertFalse(state["result"]["ok"])
        self.assertEqual(
            state["events"], ["validate", "set_leverage", "get_max_size"])
        state["place_mock"].assert_not_called()
        state["mark_failed_clean"].assert_called_once()

    def test_demo_add_uses_existing_leverage_without_set_leverage(self):
        state = self._run_demo_open(existing_positions=[{
            "symbol": "BTC-USDT-SWAP",
            "side": "long",
            "sz": 2.0,
            "notional": 200.0,
            "lev": 3.0,
            "avgPx": 100.0,
        }])
        self.assertTrue(state["result"]["ok"])
        self.assertEqual(
            state["events"],
            ["validate", "get_max_size", "validate", "place_market_open"],
        )
        self.assertEqual(
            state["validate_kwargs"]["open_positions"][0]["lev"], 3.0)
        self.assertEqual(
            state["result"]["risk"]["math"]["effective_lev"], 3.0)

    def test_demo_common_precheck_blocks_invalid_requests_before_mutating_leverage(self):
        cases = (
            {
                "label": "leverage",
                "kwargs": {"lev": 11.0},
                "reason": "leverage_exceeds",
            },
            {
                "label": "sl_direction",
                "kwargs": {"sl_trigger_px": 100.0},
                "reason": "sl_direction_invalid",
            },
            {
                "label": "sl_distance",
                "kwargs": {"sl_trigger_px": 50.0},
                "reason": "sl_deviation_exceeds",
            },
            {
                "label": "instrument_specs",
                "kwargs": {
                    "specs": {
                        "ct_val": None,
                        "lot_sz": 1.0,
                        "min_sz": 1.0,
                        "source": "demo_fetch_failed",
                        "spec_source": "demo_fetch_failed",
                    },
                },
                "reason": "instrument_unknown",
            },
        )
        for case in cases:
            with self.subTest(case=case["label"]):
                state = self._run_demo_open(**case["kwargs"])
                self.assertFalse(state["result"]["ok"])
                self.assertEqual(
                    state["result"]["reject_reason"], case["reason"])
                self.assertEqual(state["events"], ["validate"])
                state["place_mock"].assert_not_called()
                state["mark_failed_clean"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
