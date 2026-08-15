# -*- coding: utf-8 -*-
"""`order_executor.adjust_protection` 契约回归（2026-08-13 阶段2）。

守住主人拍板的三条边界与一条不变量：
  1. 止损方向完全自主（可放宽），**不重算 5% 风险预算**——但保留 30% 偏离事故闸；
  2. 止损不可撤只能替换——**任何返回路径都不得让持仓变裸仓**；
  3. 加仓后可扩到全仓（amend --newSz）；
  4. amend 为主路径，失败才退「挂新→回读→撤旧」，且顺序不可颠倒。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "lib"),
           str(ROOT / "scripts"), str(ROOT / "collectors")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import order_executor as oe  # noqa: E402


_DEADLINE_PATCHER = None


def setUpModule() -> None:
    """Historical fixtures isolate protection logic; deadline has dedicated tests."""
    global _DEADLINE_PATCHER
    _DEADLINE_PATCHER = mock.patch.object(
        oe, "_cycle_side_effect_reject", return_value=None)
    _DEADLINE_PATCHER.start()


def tearDownModule() -> None:
    if _DEADLINE_PATCHER is not None:
        _DEADLINE_PATCHER.stop()


# 与 test_execution_safety_hardening 同形的完整六项卡（契约校验器要求齐全）
CARD = {
    "cycle_id": "2026-08-13T14:00",
    "status": "ok",
    "decision": "traded",
    "decision_protocol": "decision_card_v1",
    "decision_card": {
        "direction_evidence": ["isolated protection-amend contract test"],
        "opposing_evidence": ["no external order is sent"],
        "execution_conditions": {"status": "mocked exchange endpoints"},
        "invalidation_point": {"condition": "any contract mismatch"},
        "risk_reward": {"summary": "test-only"},
        "portfolio_impact": {"summary": "temporary isolated state"},
        "historical_experience": {
            "matched_wins": [], "matched_losses": [],
            "missed_opportunities": [], "usage": "none",
            "reason": "deterministic unit test",
        },
        "agent_judgement": "exercise the deterministic protection contract",
        "reference_overrides": [],
    },
}


def _ctx(**kw):
    out = dict(CARD)
    out.update(kw)
    return out


class ValidateProtectionChangeTests(unittest.TestCase):
    """纯函数闸：只挡「无意义或几乎必是填错」，不挡风险偏好。"""

    def test_long_sl_above_mark_is_rejected(self) -> None:
        errs = oe.validate_protection_change("long", 100.0, 101.0, None)
        self.assertTrue(any("下单即触发" in e for e in errs))

    def test_short_sl_below_mark_is_rejected(self) -> None:
        errs = oe.validate_protection_change("short", 100.0, 99.0, None)
        self.assertTrue(any("下单即触发" in e for e in errs))

    def test_loosening_sl_is_allowed_no_risk_recompute(self) -> None:
        """核心授权：放宽止损必须放行（不重算 5% 预算）。"""
        self.assertEqual(oe.validate_protection_change("long", 100.0, 75.0, None), [])
        self.assertEqual(oe.validate_protection_change("short", 100.0, 129.0, None), [])

    def test_tightening_sl_is_allowed(self) -> None:
        self.assertEqual(oe.validate_protection_change("long", 100.0, 99.5, None), [])

    def test_deviation_guard_still_bites(self) -> None:
        errs = oe.validate_protection_change("long", 100.0, 60.0, None)  # 40% 偏离
        self.assertTrue(any("疑填错价" in e for e in errs))

    def test_fat_finger_near_zero_sl_is_caught(self) -> None:
        """止损误设成 0.01：表面已保护实则永不触发，比裸仓更隐蔽。"""
        errs = oe.validate_protection_change("long", 100.0, 0.01, None)
        self.assertTrue(errs)

    def test_tp_direction_and_missing_mark(self) -> None:
        self.assertTrue(oe.validate_protection_change("long", 100.0, None, 99.0))
        self.assertEqual(oe.validate_protection_change("long", 100.0, None, 110.0), [])
        self.assertTrue(oe.validate_protection_change("long", None, 95.0, None))
        self.assertTrue(oe.validate_protection_change("net", 100.0, 95.0, None))


class _Harness:
    """替身：positions / mark / algo 读写全部可控，绝不触网。"""

    def __init__(self, sl_rows, full_sz=100.0, pos_side="long"):
        self.rows = list(sl_rows)
        self.full_sz = full_sz
        self.pos_side = pos_side
        self.amend_calls = []
        self.place_calls = []
        self.cancel_calls = []
        self.amend_ok = True
        self.amend_silent = False   # 回执 ok 但交易所侧其实没变（回读打脸）
        self.place_ok = True
        self.cancel_ok = True
        self.order = []
        self.intent_reserve = mock.Mock(return_value={
            "status": "reserved", "fingerprint": "adjust-intent-fp"})
        self.intent_submitting = mock.Mock()
        self.intent_completed = mock.Mock()
        self.intent_failed_clean = mock.Mock()
        self.intent_uncertain = mock.Mock()
        self.position_reads = None

    def positions(self, profile):
        if self.position_reads is not None:
            return self.position_reads.pop(0)
        return [{"symbol": "LINK-USDT-SWAP", "side": self.pos_side,
                 "sz": self.full_sz, "posId": "POS-1", "cTime": "1000",
                 "lev": 10.0, "avgPx": 8.4}]

    def mark(self, symbol, profile):
        return 8.70

    def algos(self, symbol, profile):
        return list(self.rows)

    def amend(self, inst_id, algo_id, profile, new_sl_trigger_px=None,
              new_tp_trigger_px=None, new_sz=None):
        self.amend_calls.append(
            {"algoId": algo_id, "sl": new_sl_trigger_px,
             "tp": new_tp_trigger_px, "sz": new_sz})
        self.order.append("amend")
        if not self.amend_ok:
            return {"ok": False, "sMsg": "51279 algo not found", "data": []}
        if self.amend_silent:
            return {"ok": True, "data": [{"algoId": algo_id}]}
        for r in self.rows:
            if r.get("algoId") == algo_id:
                if new_sl_trigger_px is not None:
                    r["slTriggerPx"] = new_sl_trigger_px
                if new_tp_trigger_px is not None:
                    r["tpTriggerPx"] = new_tp_trigger_px
                if new_sz is not None:
                    r["sz"] = new_sz
        return {"ok": True, "data": [{"algoId": algo_id}]}

    def place(self, inst_id, pos_side, sz, sl_trigger_px, profile, **kw):
        tp = kw.get("tp_trigger_px")
        self.place_calls.append({"sz": sz, "sl": sl_trigger_px, "tp": tp})
        self.order.append("place")
        if not self.place_ok:
            return {"ok": False, "sMsg": "51004 insufficient", "data": []}
        self.rows.append({
            "algoId": "NEW1", "slTriggerPx": sl_trigger_px, "tpTriggerPx": tp,
            "sz": sz, "cTime": 9e12, "state": "live",
            "posSide": pos_side, "side": "sell", "reduceOnly": "true",
            "instId": inst_id})
        return {"ok": True, "data": [{"algoId": "NEW1"}]}

    def cancel(self, inst_id, algo_id, profile):
        self.cancel_calls.append(algo_id)
        self.order.append("cancel")
        if not self.cancel_ok:
            return {"ok": False, "sMsg": "51400 cancel failed", "data": []}
        self.rows = [r for r in self.rows if r.get("algoId") != algo_id]
        return {"ok": True, "data": [{"algoId": algo_id}]}

    def apply(self, stack):
        stack.enter_context(mock.patch.object(oe, "fetch_open_positions", self.positions))
        stack.enter_context(mock.patch.object(oe.ox, "get_mark_price", self.mark))
        stack.enter_context(mock.patch.object(oe.ox, "get_algo_orders", self.algos))
        stack.enter_context(mock.patch.object(oe.ox, "amend_algo_protection", self.amend))
        stack.enter_context(mock.patch.object(oe.ox, "place_algo_sl", self.place))
        stack.enter_context(mock.patch.object(
            oe.ox, "place_algo_protection", self.place))
        stack.enter_context(mock.patch.object(oe.ox, "cancel_algo_order", self.cancel))
        stack.enter_context(mock.patch.object(oe.ox, "is_dryrun", lambda: False))
        # The exchange is fully isolated above; keep the durable intent store
        # isolated too so this contract test can never touch production DBs.
        stack.enter_context(mock.patch.object(
            oe.ei, "reserve", self.intent_reserve))
        stack.enter_context(mock.patch.object(
            oe.ei, "mark_submitting", self.intent_submitting))
        stack.enter_context(mock.patch.object(
            oe.ei, "mark_completed", self.intent_completed))
        stack.enter_context(mock.patch.object(
            oe.ei, "mark_failed_clean", self.intent_failed_clean))
        stack.enter_context(mock.patch.object(
            oe.ei, "mark_uncertain", self.intent_uncertain))
        stack.enter_context(mock.patch.object(oe, "_enqueue_repair", lambda *a, **k: None))
        stack.enter_context(mock.patch.object(oe.time, "sleep", lambda *_: None))


def _live_sl(algo_id="A1", px=8.25, sz=100.0):
    return {"algoId": algo_id, "slTriggerPx": px, "tpTriggerPx": None,
            "sz": sz, "cTime": 1.0, "state": "live", "posSide": "long",
            "side": "sell", "reduceOnly": "true", "instId": "LINK-USDT-SWAP"}


def _live_tp(algo_id="TP1", px=9.50, sz=100.0):
    return {"algoId": algo_id, "slTriggerPx": None, "tpTriggerPx": px,
            "sz": sz, "cTime": 1.0, "state": "live", "posSide": "long",
            "side": "sell", "reduceOnly": "true", "instId": "LINK-USDT-SWAP"}


class AdjustProtectionFlowTests(unittest.TestCase):
    def _run(self, harness, **kw):
        import contextlib
        with contextlib.ExitStack() as stack:
            harness.apply(stack)
            return oe.adjust_protection(
                "LINK-USDT-SWAP", "live", cycle_id="2026-08-13T14:00",
                receipt_context=_ctx(), **kw)

    def test_amend_is_primary_path(self) -> None:
        h = _Harness([_live_sl()])
        r = self._run(h, new_sl_trigger_px=8.60)
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], "amend")
        self.assertEqual(h.amend_calls[0]["sl"], 8.60)
        self.assertEqual(h.place_calls, [])       # 主路径不挂新单
        self.assertEqual(h.cancel_calls, [])      # 也不撤单
        self.assertEqual(r["previous"]["sl"], 8.25)
        # ADJUST_PROTECTION 是有交易所副作用的零成交正式动作；
        # executor 直接返回 writer 可接受的确定性终态，Agent 不再猜字段。
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["decision"], "hold")
        self.assertEqual(r["n_orders"], 0)
        self.assertEqual(r["mode"], "live")
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["intent_state"], "completed")
        self.assertEqual(h.intent_reserve.call_args.kwargs["action"],
                         "adjust_protection")
        h.intent_submitting.assert_called_once()
        h.intent_completed.assert_called_once()

    def test_custom_standalone_reason_cannot_bypass_execution_intent(self) -> None:
        h = _Harness([_live_sl()])
        r = self._run(
            h,
            new_sl_trigger_px=8.60,
            reason_code="agent_trailing_stop",
        )
        self.assertTrue(r["ok"])
        h.intent_reserve.assert_called_once()
        h.intent_submitting.assert_called_once()
        h.intent_completed.assert_called_once()

    def test_position_reopened_after_preflight_rejects_before_protection_write(self) -> None:
        h = _Harness([_live_sl()])
        h.position_reads = [
            [{"symbol": "LINK-USDT-SWAP", "side": "long", "sz": 100.0,
              "posId": "POS-1", "cTime": "1000", "lev": 10.0,
              "avgPx": 8.4}],
            [{"symbol": "LINK-USDT-SWAP", "side": "long", "sz": 100.0,
              "posId": "POS-2", "cTime": "2000", "lev": 10.0,
              "avgPx": 8.6}],
        ]
        r = self._run(
            h,
            new_sl_trigger_px=8.60,
            expected_pre_position_exists=True,
            expected_pre_position_sz=100.0,
            expected_pre_position_pos_id="POS-1",
            expected_pre_position_c_time="1000",
        )
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["reject_reason"],
                         "pre_position_fingerprint_changed")
        self.assertEqual(h.amend_calls, [])
        self.assertEqual(h.place_calls, [])
        self.assertEqual(h.cancel_calls, [])
        h.intent_failed_clean.assert_called_once()

    def test_parent_order_resize_reuses_parent_intent(self) -> None:
        h = _Harness([_live_sl()])
        r = self._run(
            h,
            new_sl_trigger_px=8.60,
            reason_code="post_add_resize",
        )
        self.assertTrue(r["ok"])
        h.intent_reserve.assert_not_called()
        h.intent_submitting.assert_not_called()
        h.intent_completed.assert_not_called()

    def test_moving_tp_cancels_old_independent_tp_only_after_confirmation(self) -> None:
        h = _Harness([_live_sl(), _live_tp()])
        r = self._run(h, new_tp_trigger_px=9.80)
        self.assertTrue(r["ok"])
        self.assertEqual(h.order, ["place", "cancel", "cancel"])
        self.assertEqual(h.place_calls[0]["tp"], 9.80)
        self.assertEqual(sorted(h.cancel_calls), ["A1", "TP1"])
        self.assertEqual(r["path"], "oco_replace")
        self.assertEqual(len(h.rows), 1)
        self.assertEqual(h.rows[0]["tpTriggerPx"], 9.80)

    def test_stale_tp_cancel_failure_is_a_non_naked_warning(self) -> None:
        h = _Harness([_live_sl(), _live_tp()])
        h.cancel_ok = False
        r = self._run(h, new_tp_trigger_px=9.80)
        self.assertFalse(r["ok"])
        self.assertFalse(r["p0"])
        self.assertEqual(r["reject_reason"], "protection_state_unconfirmed")
        self.assertTrue(r["protection_state"]["duplicate_sl"])

    def test_loosening_stop_is_executed(self) -> None:
        """完全自主：放宽止损照做（这是主人明确要的行为）。"""
        h = _Harness([_live_sl()])
        r = self._run(h, new_sl_trigger_px=7.50)
        self.assertTrue(r["ok"])
        self.assertEqual(h.amend_calls[0]["sl"], 7.50)

    def test_resize_to_full_position_after_add(self) -> None:
        h = _Harness([_live_sl(sz=100.0)], full_sz=180.0)
        r = self._run(h, resize_to_full_position=True)
        self.assertTrue(r["ok"])
        self.assertEqual(h.amend_calls[0]["sz"], 180.0)
        self.assertEqual(r["applied"]["sz"], 180.0)

    def test_size_drift_is_corrected_even_without_flag(self) -> None:
        """止损数量与现仓不符（加仓后没扩）→ 顺带纠正，不留部分保护。"""
        h = _Harness([_live_sl(sz=100.0)], full_sz=180.0)
        r = self._run(h, new_sl_trigger_px=8.60)
        self.assertTrue(r["ok"])
        self.assertEqual(h.amend_calls[0]["sz"], 180.0)

    def test_amend_failure_falls_back_to_place_then_cancel_in_order(self) -> None:
        h = _Harness([_live_sl()])
        h.amend_ok = False
        r = self._run(h, new_sl_trigger_px=8.60)
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], "replace_fallback")
        # 顺序不可颠倒：先 place 后 cancel（反过来中间就是裸仓窗口）
        self.assertEqual(h.order, ["amend", "place", "cancel"])
        self.assertEqual(h.cancel_calls, ["A1"])

    def test_sl_replace_fallback_preserves_existing_attached_tp_via_oco(self) -> None:
        old = _live_sl()
        old["tpTriggerPx"] = 9.50
        h = _Harness([old])
        h.amend_ok = False
        r = self._run(h, new_sl_trigger_px=8.60)
        self.assertTrue(r["ok"])
        self.assertEqual(h.place_calls[0]["tp"], 9.50)
        self.assertEqual(r["applied"]["tp"], 9.50)
        self.assertEqual(len(h.rows), 1)
        self.assertEqual(h.rows[0]["tpTriggerPx"], 9.50)

    def test_place_failure_keeps_old_sl_and_rejects_cleanly(self) -> None:
        h = _Harness([_live_sl()])
        h.amend_ok = False
        h.place_ok = False
        r = self._run(h, new_sl_trigger_px=8.60)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "protection_place_failed")
        self.assertFalse(r["p0"])                 # 旧单还在 → 非裸仓
        self.assertEqual(h.cancel_calls, [])      # 绝不在挂新失败后撤旧
        self.assertEqual(len(h.rows), 1)          # 原保护单原封不动
        h.intent_failed_clean.assert_called_once()
        h.intent_uncertain.assert_not_called()
        h.intent_completed.assert_not_called()

    def test_cancel_failure_leaves_two_stops_but_still_protected(self) -> None:
        h = _Harness([_live_sl()])
        h.amend_ok = False
        h.cancel_ok = False
        r = self._run(h, new_sl_trigger_px=8.60)
        # 两张全仓止损：先触发者平仓，另一张 reduceOnly 自动作废 → 非裸仓，
        # 但终局状态不唯一，必须如实报未确认而不是谎报成功。
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "protection_state_unconfirmed")
        self.assertFalse(r["protection_state"]["naked"])
        self.assertTrue(r["protection_state"]["duplicate_sl"])

    def test_duplicate_sl_before_change_is_refused(self) -> None:
        h = _Harness([_live_sl("A1"), _live_sl("A2", px=8.10)])
        r = self._run(h, new_sl_trigger_px=8.60)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "duplicate_sl_before_change")
        self.assertEqual(h.amend_calls, [])       # 状态不干净时不动手

    def test_never_creates_unprotected_position(self) -> None:
        """无止损可保留且未给新价 → 拒绝，绝不产生/留下裸仓。"""
        h = _Harness([])
        r = self._run(h, new_tp_trigger_px=9.5)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "no_sl_to_preserve")
        self.assertEqual(h.place_calls, [])

    def test_missing_position_is_rejected(self) -> None:
        h = _Harness([_live_sl()])
        h.positions = lambda profile: []
        r = self._run(h, new_sl_trigger_px=8.60)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "no_position")

    def test_positions_api_failure_fails_closed(self) -> None:
        h = _Harness([_live_sl()])

        def boom(profile):
            raise oe.PositionsUnavailable("api down")
        h.positions = boom
        r = self._run(h, new_sl_trigger_px=8.60)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "positions_unavailable")
        self.assertEqual(h.amend_calls, [])       # 拒绝盲改

    def test_invalid_direction_never_reaches_exchange(self) -> None:
        h = _Harness([_live_sl()])
        r = self._run(h, new_sl_trigger_px=9.99)  # 多头止损高于现价 8.70
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "protection_change_invalid")
        self.assertEqual(h.amend_calls, [])
        self.assertEqual(h.place_calls, [])


class ConsolidateAfterAddTests(unittest.TestCase):
    """加仓后「两张分档止损 → 一张全仓止损」的收敛路径（2026-08-13 阶段3）。

    加仓必然产生第二张止损：`open_position` 每笔成交都自挂 `approved_sz` 大小的
    reduceOnly 止损，且 `_verify_sl_placed` 要求 `cTime >= 本次请求时刻`——
    命中的必是新单。故「加仓后自动扩到全仓」只能走收敛，不能走普通 amend。

    本类守的核心不变量：**任一瞬间保护覆盖量 ≥ 现仓**，即撤单永远发生在
    「接替单已被交易所确认覆盖全仓」之后。
    """

    def _run(self, harness, **kw):
        import contextlib
        with contextlib.ExitStack() as stack:
            harness.apply(stack)
            return oe.adjust_protection(
                "LINK-USDT-SWAP", "live", cycle_id="2026-08-13T14:00",
                receipt_context=_ctx(), **kw)

    def _after_add(self, full_sz=200.0):
        """加仓后的真实现场：旧档 100 张 + 新档 100 张，现仓 200 张。

        cTime 刻意区分：加仓单必然晚于原开仓单（`_verify_sl_placed` 要求
        `cTime >= 请求时刻`），同量时幸存单应是更早那张。
        """
        old = _live_sl("OLD", px=8.25, sz=100.0)
        new = _live_sl("NEW", px=8.10, sz=100.0)
        old["cTime"], new["cTime"] = 1.0, 2.0
        return _Harness([old, new], full_sz=full_sz)

    def test_two_tranche_stops_merge_into_one_full_size_stop(self) -> None:
        h = self._after_add()
        r = self._run(h, resize_to_full_position=True,
                      consolidate_extra_sl=True)
        self.assertTrue(r["ok"], r.get("reject_detail"))
        self.assertEqual(r["path"], "amend_consolidate")
        self.assertEqual(r["applied"]["sz"], 200.0)
        self.assertEqual(len(h.rows), 1)
        self.assertEqual(h.rows[0]["sz"], 200.0)
        self.assertEqual(r["protection_state"]["live_sl_count"], 1)

    def test_amend_strictly_precedes_cancel(self) -> None:
        """顺序颠倒＝自造裸口窗；这是本路径唯一不可让步的点。"""
        h = self._after_add()
        self._run(h, resize_to_full_position=True, consolidate_extra_sl=True)
        self.assertEqual(h.order[0], "amend")
        self.assertNotIn("cancel", h.order[:1])
        self.assertEqual(h.order.count("cancel"), 1)
        self.assertLess(h.order.index("amend"), h.order.index("cancel"))
        # amend 必须一次就把幸存单扩到全仓，而不是先撤再补
        self.assertEqual(h.amend_calls[0]["sz"], 200.0)

    def test_survivor_is_largest_then_oldest_deterministically(self) -> None:
        h = _Harness([_live_sl("SMALL", px=8.2, sz=40.0),
                      _live_sl("BIG_NEW", px=8.2, sz=60.0),
                      _live_sl("BIG_OLD", px=8.2, sz=60.0)], full_sz=160.0)
        h.rows[1]["cTime"] = 50.0
        h.rows[2]["cTime"] = 10.0
        r = self._run(h, resize_to_full_position=True,
                      consolidate_extra_sl=True)
        self.assertTrue(r["ok"], r.get("reject_detail"))
        self.assertEqual(h.amend_calls[0]["algoId"], "BIG_OLD")
        self.assertEqual(sorted(h.cancel_calls), ["BIG_NEW", "SMALL"])
        self.assertEqual(sorted(r["consolidated_from"]), ["BIG_NEW", "SMALL"])

    def test_no_price_given_keeps_the_most_protective_one(self) -> None:
        """收敛不得隐式放松保护：多头取最高的那档触发价。"""
        h = self._after_add()
        r = self._run(h, resize_to_full_position=True,
                      consolidate_extra_sl=True)
        self.assertEqual(h.amend_calls[0]["sl"], 8.25)
        self.assertEqual(r["applied"]["sl"], 8.25)

    def test_short_position_keeps_the_lowest_stop(self) -> None:
        h = _Harness([_live_sl("A", px=9.10, sz=50.0),
                      _live_sl("B", px=9.40, sz=50.0)],
                     full_sz=100.0, pos_side="short")
        for row in h.rows:
            row["posSide"], row["side"] = "short", "buy"
        r = self._run(h, resize_to_full_position=True,
                      consolidate_extra_sl=True)
        self.assertTrue(r["ok"], r.get("reject_detail"))
        self.assertEqual(r["applied"]["sl"], 9.10)

    def test_stop_on_wrong_side_of_mark_is_not_adopted(self) -> None:
        """最保护的那档若已在现价错侧（下单即触发），退回幸存单原价。"""
        h = _Harness([_live_sl("OLD", px=8.25, sz=100.0),
                      _live_sl("BAD", px=8.99, sz=40.0)], full_sz=140.0)
        r = self._run(h, resize_to_full_position=True,
                      consolidate_extra_sl=True)
        self.assertTrue(r["ok"], r.get("reject_detail"))
        self.assertEqual(r["applied"]["sl"], 8.25)   # 不是 8.99（> mark 8.70）

    def test_cancel_is_skipped_when_survivor_not_confirmed_full(self) -> None:
        """amend 回执说成功但交易所回读没扩上 → 一张都不撤，干净拒绝。"""
        h = self._after_add()
        h.amend_silent = True
        r = self._run(h, resize_to_full_position=True,
                      consolidate_extra_sl=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "consolidate_survivor_unconfirmed")
        self.assertEqual(h.cancel_calls, [])          # ← 本测试的全部意义
        self.assertEqual(len(h.rows), 2)              # 分档全覆盖，非裸仓
        self.assertFalse(r["p0"])

    def test_amend_failure_places_full_stop_then_cancels_every_old_one(self) -> None:
        h = self._after_add()
        h.amend_ok = False
        r = self._run(h, resize_to_full_position=True,
                      consolidate_extra_sl=True)
        self.assertTrue(r["ok"], r.get("reject_detail"))
        self.assertEqual(r["path"], "replace_fallback")
        self.assertEqual(h.place_calls[0]["sz"], 200.0)
        self.assertLess(h.order.index("place"), h.order.index("cancel"))
        self.assertEqual(sorted(h.cancel_calls), ["NEW", "OLD"])
        self.assertEqual(len(h.rows), 1)

    def test_place_failure_leaves_both_tranche_stops_untouched(self) -> None:
        h = self._after_add()
        h.amend_ok = False
        h.place_ok = False
        r = self._run(h, resize_to_full_position=True,
                      consolidate_extra_sl=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "protection_place_failed")
        self.assertEqual(h.cancel_calls, [])
        self.assertEqual(len(h.rows), 2)
        self.assertFalse(r["p0"])

    def test_flag_defaults_off_so_manual_edits_still_refuse(self) -> None:
        """默认关：人工改单遇到残单仍必须硬拒，收敛只对加仓这一确定场景开。"""
        import inspect
        sig = inspect.signature(oe.adjust_protection)
        self.assertIs(sig.parameters["consolidate_extra_sl"].default, False)
        h = self._after_add()
        r = self._run(h, new_sl_trigger_px=8.60)
        self.assertEqual(r["reject_reason"], "duplicate_sl_before_change")
        self.assertEqual(h.amend_calls, [])
        self.assertEqual(h.cancel_calls, [])

    def test_dryrun_lists_cancel_plan_without_touching_exchange(self) -> None:
        import contextlib
        h = self._after_add()
        with contextlib.ExitStack() as stack:
            h.apply(stack)
            stack.enter_context(mock.patch.object(oe.ox, "is_dryrun", lambda: True))
            r = oe.adjust_protection(
                "LINK-USDT-SWAP", "live", resize_to_full_position=True,
                consolidate_extra_sl=True, cycle_id="2026-08-13T14:00",
                receipt_context=_ctx())
        self.assertTrue(r["dryrun"])
        self.assertEqual(r["planned"]["sz"], 200.0)
        self.assertEqual(r["planned"]["cancel_after"], ["NEW"])
        self.assertEqual(h.amend_calls, [])
        self.assertEqual(h.place_calls, [])
        self.assertEqual(h.cancel_calls, [])


class GuardrailTests(unittest.TestCase):
    def test_non_live_profile_raises(self) -> None:
        with self.assertRaises(ValueError):
            oe.adjust_protection("LINK-USDT-SWAP", "demo",
                                 new_sl_trigger_px=8.6)

    def test_dryrun_plans_without_any_mutation(self) -> None:
        import contextlib
        h = _Harness([_live_sl()])
        with contextlib.ExitStack() as stack:
            h.apply(stack)
            stack.enter_context(mock.patch.object(oe.ox, "is_dryrun", lambda: True))
            r = oe.adjust_protection("LINK-USDT-SWAP", "live",
                                     new_sl_trigger_px=8.60)
        self.assertTrue(r["ok"])
        self.assertTrue(r["dryrun"])
        self.assertEqual(r["planned"]["path"], "amend")
        self.assertEqual(h.amend_calls, [])
        self.assertEqual(h.place_calls, [])

    def test_live_call_requires_cycle_and_card(self) -> None:
        import contextlib
        h = _Harness([_live_sl()])
        with contextlib.ExitStack() as stack:
            h.apply(stack)
            r = oe.adjust_protection("LINK-USDT-SWAP", "live",
                                     new_sl_trigger_px=8.60)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reject_reason"], "receipt_context_invalid")

    def test_agent_contract_exposes_only_deterministic_entry(self) -> None:
        """Agent 现可调用确定性入口；dispatcher/briefing 不得绕过它直接改保护。"""
        trader = (ROOT / "agents/live_trader.md").read_text(encoding="utf-8")
        self.assertIn("live_position_action_runner.py", trader)
        self.assertIn("ADJUST_PROTECTION", trader)
        self.assertNotIn("adjust_protection(", trader)
        for rel in ("core/dispatcher.py", "scripts/decision_briefing.py"):
            text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
            calls = [ln for ln in text.splitlines()
                     if "adjust_protection(" in ln and "def " not in ln]
            self.assertEqual(calls, [], f"{rel} 出现确定性调度绕行")


if __name__ == "__main__":
    unittest.main()
