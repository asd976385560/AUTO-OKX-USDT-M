# -*- coding: utf-8 -*-
"""加仓成交后自动收敛并扩到全仓止损（2026-08-13 阶段4 接线）。

主人 2026-08-13 授权：「加仓后自动扩到全仓」。阶段3 真单已证实**加仓必然留下
第二张分档止损**——`open_position` 每笔成交都自挂 `approved_sz` 大小的 reduceOnly SL，
而 `_verify_sl_placed` 要求 `cTime >= 本次请求时刻`，命中的必是新单。不接线就长期
停在「两张分档单」：总覆盖达全仓（非裸仓），但不满足终局唯一契约，且此后任何
**不带** `consolidate_extra_sl` 的改单都会被 `duplicate_sl_before_change` 硬拒。

本文件守四条：
  1. 只有加仓（开仓前同侧已有仓）才触发，全新开仓绝不触发；
  2. 触发时参数必须与 `reduce_position` 的 post_reduce_resize 对称
     （`resize_to_full_position` + `consolidate_extra_sl` + 显式 `pos_side`）；
  3. 扩仓失败**绝不反向抹掉已确认的成交**——回执仍 ok，成交行原样保留，
     失败只外显 + 入 repair_queue；
  4. 失败到裸仓级别（`naked_after_change` / `no_sl_to_preserve`）必须把 p0 顶到回执上。
"""
from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "lib"),
           str(ROOT / "scripts"), str(ROOT / "collectors"),
           str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import order_executor as oe  # noqa: E402
from test_execution_safety_hardening import (  # noqa: E402
    _ready_multitimeframe,
    _same_actor_timeline,
    _valid_receipt_context,
)


_DEADLINE_PATCHER = None


def setUpModule() -> None:
    """Historical fixtures isolate add/SL sync; deadline has dedicated tests."""
    global _DEADLINE_PATCHER
    _DEADLINE_PATCHER = mock.patch.object(
        oe, "_cycle_side_effect_reject", return_value=None)
    _DEADLINE_PATCHER.start()


def tearDownModule() -> None:
    if _DEADLINE_PATCHER is not None:
        _DEADLINE_PATCHER.stop()


CYCLE = "2026-08-13T22:45"
SYMBOL = "DOGE-USDT-SWAP"


def _held(sz: float) -> list[dict]:
    """开仓前的同标的同侧现仓（sz=0 表示全新开仓）。"""
    if sz <= 0:
        return []
    return [{"symbol": SYMBOL, "side": "long", "sz": sz,
             "posId": "POS-1", "cTime": "1000",
             "lev": 5.0, "avgPx": 0.0702}]


def _run_open(pre_sz: float, *, adjust_result: dict | None = None,
              dryrun: bool = False, sl: float | None = 0.0677,
              expected_pre_position_exists: bool | None = None,
              expected_pre_position_sz: float | None = None,
              expected_pre_position_pos_id: str | None = None,
              expected_pre_position_c_time: str | None = None,
              position_reads: list[list[dict]] | None = None,
              io_mocks: dict | None = None):
    """跑一次 open_position，返回 (回执, adjust_protection 的 mock, repair 的 mock)。"""
    adjust_mock = mock.Mock(return_value=adjust_result or {
        "ok": True, "action_taken": "ADJUST_PROTECTION",
        "path": "amend_consolidate", "applied": {"sl": 0.0677, "sz": 2.0},
        "consolidated_from": ["A-NEW"],
        "protection_state": {"live_sl_count": 1, "naked": False},
    })
    repair_mock = mock.Mock()
    journal_mock = mock.Mock()
    set_leverage_mock = mock.Mock(return_value={"ok": True})
    place_market_mock = mock.Mock(return_value={
        "ok": True, "sl_attached": True, "tp_attached": False,
        "data": [{"ordId": "O-ADD"}],
    })
    if io_mocks is not None:
        io_mocks.update({
            "set_leverage": set_leverage_mock,
            "place_market_open": place_market_mock,
        })
    risk_result = {"approved": True, "approved_sz": 1.0, "clamped": False,
                   "adjustments": [], "math": {"effective_lev": 5.0}}
    with ExitStack() as stack:
        p = stack.enter_context
        p(mock.patch.object(oe.ox, "is_dryrun", return_value=dryrun))
        p(mock.patch.object(oe, "validate_receipt_context", return_value=[]))
        p(mock.patch.object(oe.actor_att, "timeline_state",
                            side_effect=_same_actor_timeline))
        p(mock.patch.object(oe, "check_multitimeframe_readiness",
                            side_effect=_ready_multitimeframe))
        p(mock.patch.object(oe.ei, "reserve",
                            return_value={"status": "reserved", "fingerprint": "FP"}))
        for name in ("mark_submitting", "mark_submitted", "mark_completed"):
            p(mock.patch.object(oe.ei, name))
        p(mock.patch.object(oe.ox, "get_balance", return_value={"ok": True}))
        p(mock.patch.object(oe.ac, "extract_settlement_capacity",
                            return_value={"ok": True, "total_equity": 1000.0,
                                          "available_margin": 900.0,
                                          "settlement_ccy": "USDT",
                                          "account_imr": 100.0}))
        positions_patch = mock.patch.object(
            oe, "fetch_open_positions",
            side_effect=position_reads if position_reads is not None else None,
            return_value=_held(pre_sz),
        )
        p(positions_patch)
        p(mock.patch.object(oe, "_verify_pretrade_ledger_positions",
                            return_value={"ok": True, "profile": "live",
                                          "ledger_groups": 0,
                                          "exchange_groups": 0, "diffs": []}))
        p(mock.patch.object(oe.ox, "get_mark_price", return_value=0.0702))
        p(mock.patch.object(oe, "fetch_instrument_specs",
                            return_value={"ct_val": 1000.0, "lot_sz": 0.01,
                                          "source": "test", "spec_source": "test"}))
        p(mock.patch.object(oe.rv, "validate", return_value=risk_result))
        p(mock.patch.object(oe.ox, "set_leverage", set_leverage_mock))
        p(mock.patch.object(oe.ox, "place_market_open", place_market_mock))
        p(mock.patch.object(oe.ox, "place_algo_sl",
                            return_value={"ok": True, "data": [{"algoId": "A-SL"}]}))
        p(mock.patch.object(oe, "_verify_sl_placed",
                            return_value={"verified": True, "found": [], "matched": {}}))
        p(mock.patch.object(oe, "_read_fills",
                            return_value={"ok": True, "fill_px": 0.07013,
                                          "fill_sz": 1.0, "pnl": 0.0, "n": 1,
                                          "fill_ts": "2026-08-13 22:54:56",
                                          "ts_source": "fills.fillTime"}))
        p(mock.patch.object(oe, "_journal_fill", journal_mock))
        p(mock.patch.object(oe, "_enqueue_repair", repair_mock))
        p(mock.patch.object(oe, "close_position", mock.Mock()))
        p(mock.patch.object(oe, "adjust_protection", adjust_mock))

        result = oe.open_position(
            SYMBOL, "long", 1.0, 5.0, sl, "live", cycle_id=CYCLE,
            receipt_context=_valid_receipt_context(CYCLE, "long", SYMBOL),
            expected_pre_position_exists=expected_pre_position_exists,
            expected_pre_position_sz=expected_pre_position_sz,
            expected_pre_position_pos_id=expected_pre_position_pos_id,
            expected_pre_position_c_time=expected_pre_position_c_time,
        )
    return result, adjust_mock, repair_mock, journal_mock


class AddTriggersProtectionSyncTests(unittest.TestCase):
    def test_expected_add_that_became_flat_rejects_before_order_io(self) -> None:
        io_mocks: dict = {}
        result, adjust, _repair, _journal = _run_open(
            pre_sz=0.0,
            expected_pre_position_exists=True,
            expected_pre_position_sz=1.0,
            expected_pre_position_pos_id="POS-1",
            expected_pre_position_c_time="1000",
            io_mocks=io_mocks,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reject_reason"],
                         "pre_position_semantics_changed")
        io_mocks["set_leverage"].assert_not_called()
        io_mocks["place_market_open"].assert_not_called()
        adjust.assert_not_called()

    def test_expected_open_that_became_add_rejects_before_order_io(self) -> None:
        io_mocks: dict = {}
        result, adjust, _repair, _journal = _run_open(
            pre_sz=1.0,
            expected_pre_position_exists=False,
            expected_pre_position_sz=0.0,
            io_mocks=io_mocks,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reject_reason"],
                         "pre_position_semantics_changed")
        io_mocks["set_leverage"].assert_not_called()
        io_mocks["place_market_open"].assert_not_called()
        adjust.assert_not_called()

    def test_expected_add_drift_after_preflight_rejects_at_order_boundary(self) -> None:
        io_mocks: dict = {}
        result, adjust, _repair, _journal = _run_open(
            pre_sz=1.0,
            expected_pre_position_exists=True,
            expected_pre_position_sz=1.0,
            expected_pre_position_pos_id="POS-1",
            expected_pre_position_c_time="1000",
            position_reads=[_held(1.0), []],
            io_mocks=io_mocks,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reject_reason"],
                         "pre_position_semantics_changed")
        io_mocks["place_market_open"].assert_not_called()
        adjust.assert_not_called()

    def test_expected_open_drift_after_preflight_rejects_at_order_boundary(self) -> None:
        io_mocks: dict = {}
        result, adjust, _repair, _journal = _run_open(
            pre_sz=0.0,
            expected_pre_position_exists=False,
            expected_pre_position_sz=0.0,
            position_reads=[[], _held(1.0)],
            io_mocks=io_mocks,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reject_reason"],
                         "pre_position_semantics_changed")
        io_mocks["place_market_open"].assert_not_called()
        adjust.assert_not_called()

    def test_add_syncs_protection_with_consolidation(self) -> None:
        result, adjust, _repair, _journal = _run_open(pre_sz=1.0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["is_add"])
        self.assertEqual(result["pre_position_sz"], 1.0)
        adjust.assert_called_once()
        args, kwargs = adjust.call_args
        self.assertEqual(args[:2], (SYMBOL, "live"))
        # 与 reduce_position 的 post_reduce_resize 对称
        self.assertEqual(kwargs["pos_side"], "long")
        self.assertTrue(kwargs["resize_to_full_position"])
        self.assertTrue(kwargs["consolidate_extra_sl"])
        self.assertEqual(kwargs["reason_code"], "post_add_resize")
        self.assertEqual(kwargs["cycle_id"], CYCLE)
        self.assertEqual(result["protection_sync"]["path"], "amend_consolidate")

    def test_fresh_open_never_touches_protection(self) -> None:
        """全新开仓只有一张 SL，收敛无意义；多跑一次改单纯属加风险。"""
        result, adjust, _repair, _journal = _run_open(pre_sz=0.0)
        self.assertTrue(result["ok"])
        self.assertFalse(result["is_add"])
        adjust.assert_not_called()
        self.assertIsNone(result["protection_sync"])

    def test_open_without_stop_loss_never_calls_adjust(self) -> None:
        """无 SL 时 adjust_protection 只会返回 no_sl_to_preserve，不必去撞它。"""
        _result, adjust, _repair, _journal = _run_open(pre_sz=1.0, sl=None)
        adjust.assert_not_called()

    def test_dryrun_plans_without_calling_the_exchange(self) -> None:
        result, adjust, _repair, _journal = _run_open(pre_sz=1.0, dryrun=True)
        adjust.assert_not_called()
        self.assertTrue(result["protection_sync"]["dryrun"])
        self.assertEqual(result["protection_sync"]["planned_full_sz"], 2.0)


class SyncFailureNeverUnwindsTheFillTests(unittest.TestCase):
    """本类是这条接线的安全核心：扩仓是成交**之后**的补强，
    它失败绝不能反过来否定已经发生的成交，更不能去撤止损。"""

    FAIL = {"ok": False, "action_taken": "REJECT",
            "reject_reason": "consolidate_survivor_unconfirmed",
            "reject_detail": "幸存单回读未达全仓", "p0": False,
            "path": "amend_consolidate"}

    def test_failure_keeps_the_receipt_ok_and_the_trade_intact(self) -> None:
        result, _adjust, repair, journal = _run_open(
            pre_sz=1.0, adjust_result=self.FAIL)
        self.assertTrue(result["ok"])                    # 成交是既成事实
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["fill_sz"], 1.0)
        journal.assert_called()                          # 成交已留痕
        self.assertFalse(result["protection_sync"]["ok"])
        self.assertEqual(result["protection_sync"]["reject_reason"],
                         "consolidate_survivor_unconfirmed")
        self.assertFalse(result["p0"])                   # 分档全覆盖，非裸仓
        reasons = [c.args[3] for c in repair.call_args_list]
        self.assertTrue(
            any(str(r).startswith("add_protection_sync_failed:") for r in reasons),
            reasons)

    def test_naked_after_change_is_escalated_to_p0(self) -> None:
        naked = dict(self.FAIL, reject_reason="naked_after_change", p0=True)
        result, _adjust, _repair, _journal = _run_open(
            pre_sz=1.0, adjust_result=naked)
        self.assertTrue(result["ok"])
        self.assertTrue(result["p0"])

    def test_no_sl_to_preserve_is_also_p0(self) -> None:
        gone = dict(self.FAIL, reject_reason="no_sl_to_preserve")
        result, _adjust, _repair, _journal = _run_open(
            pre_sz=1.0, adjust_result=gone)
        self.assertTrue(result["p0"])

    def test_close_is_never_called_to_undo_a_failed_sync(self) -> None:
        """扩仓失败不得触发 unwind——那会把一笔正确的加仓平掉。"""
        close_mock = mock.Mock()
        with mock.patch.object(oe, "close_position", close_mock):
            result, _adjust, _repair, _journal = _run_open(
                pre_sz=1.0, adjust_result=self.FAIL)
        self.assertTrue(result["ok"])
        close_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
