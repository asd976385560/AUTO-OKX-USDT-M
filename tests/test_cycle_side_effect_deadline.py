# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for module_path in (ROOT, ROOT / "core", ROOT / "collectors"):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

from core import order_executor as oe  # noqa: E402


_CST = timezone(timedelta(hours=8))
_CYCLE = "2026-08-15T05:00"
_LATE_REJECT = {
    "action_taken": "REJECT",
    "reject_reason": "cycle_side_effect_deadline_exceeded",
    "reject_detail": "isolated late-cycle fixture; no exchange write",
    "side_effect_deadline": {
        "cycle_id": _CYCLE,
        "deadline_at": "2026-08-15 05:13:00",
        "checked_at": "2026-08-15 05:15:00",
        "comparison": ">=",
    },
}


class CycleSideEffectDeadlineHelperTests(unittest.TestCase):
    def test_before_deadline_is_allowed(self) -> None:
        result = oe._cycle_side_effect_reject(
            _CYCLE,
            now=datetime(2026, 8, 15, 5, 12, 59, tzinfo=_CST),
        )
        self.assertIsNone(result)

    def test_exact_deadline_is_rejected(self) -> None:
        result = oe._cycle_side_effect_reject(
            _CYCLE,
            now=datetime(2026, 8, 15, 5, 13, 0, tzinfo=_CST),
        )
        self.assertEqual(
            result["reject_reason"],
            "cycle_side_effect_deadline_exceeded",
        )
        self.assertEqual(
            result["side_effect_deadline"]["deadline_at"],
            "2026-08-15 05:13:00",
        )

    def test_timezone_is_normalized_before_comparison(self) -> None:
        result = oe._cycle_side_effect_reject(
            _CYCLE,
            now=datetime(2026, 8, 14, 21, 13, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            result["side_effect_deadline"]["checked_at"],
            "2026-08-15 05:13:00",
        )

    def test_invalid_cycle_is_fail_closed(self) -> None:
        result = oe._cycle_side_effect_reject("not-a-cycle")
        self.assertEqual(result["reject_reason"], "cycle_id_invalid")


class LateCycleEntryPointTests(unittest.TestCase):
    def _patch_common(self):
        return (
            mock.patch.object(oe.ox, "is_dryrun", return_value=False),
            mock.patch.object(oe, "validate_receipt_context", return_value=[]),
            mock.patch.object(
                oe,
                "_cycle_side_effect_reject",
                return_value=dict(_LATE_REJECT),
            ),
        )

    def test_open_rejects_before_account_or_order_io(self) -> None:
        dryrun, context, deadline = self._patch_common()
        with dryrun, context, deadline, \
                mock.patch.object(oe, "fetch_open_positions") as positions, \
                mock.patch.object(oe.ox, "place_market_open") as place:
            result = oe.open_position(
                "BTC-USDT-SWAP",
                "long",
                1,
                2,
                90,
                "live",
                cycle_id=_CYCLE,
                receipt_context={"cycle_id": _CYCLE},
            )
        self.assertEqual(
            result["reject_reason"],
            "cycle_side_effect_deadline_exceeded",
        )
        positions.assert_not_called()
        place.assert_not_called()

    def test_close_rejects_before_position_or_order_io(self) -> None:
        dryrun, context, deadline = self._patch_common()
        with dryrun, context, deadline, \
                mock.patch.object(oe, "fetch_open_positions") as positions, \
                mock.patch.object(oe.ox, "place_reduce_only_market") as place:
            result = oe.close_position(
                "BTC-USDT-SWAP",
                "live",
                pos_side="long",
                cycle_id=_CYCLE,
                receipt_context={"cycle_id": _CYCLE},
            )
        self.assertEqual(
            result["reject_reason"],
            "cycle_side_effect_deadline_exceeded",
        )
        positions.assert_not_called()
        place.assert_not_called()

    def test_reduce_rejects_before_intent_or_order_io(self) -> None:
        dryrun, context, deadline = self._patch_common()
        with dryrun, context, deadline, \
                mock.patch.object(oe.ei, "reserve") as reserve, \
                mock.patch.object(oe.ox, "place_reduce_only_market") as place:
            result = oe.reduce_position(
                "BTC-USDT-SWAP",
                "live",
                1,
                pos_side="long",
                cycle_id=_CYCLE,
                receipt_context={"cycle_id": _CYCLE},
            )
        self.assertEqual(
            result["reject_reason"],
            "cycle_side_effect_deadline_exceeded",
        )
        reserve.assert_not_called()
        place.assert_not_called()

    def test_standalone_protection_rejects_before_exchange_io(self) -> None:
        dryrun, context, deadline = self._patch_common()
        with dryrun, context, deadline, \
                mock.patch.object(oe, "fetch_open_positions") as positions, \
                mock.patch.object(oe.ox, "amend_algo_protection") as amend:
            result = oe.adjust_protection(
                "BTC-USDT-SWAP",
                "live",
                pos_side="long",
                new_sl_trigger_px=90,
                cycle_id=_CYCLE,
                receipt_context={"cycle_id": _CYCLE},
            )
        self.assertEqual(
            result["reject_reason"],
            "cycle_side_effect_deadline_exceeded",
        )
        positions.assert_not_called()
        amend.assert_not_called()

    def test_emergency_unwind_bypasses_new_action_gate(self) -> None:
        dryrun, context, deadline = self._patch_common()
        with dryrun, context, deadline as deadline_mock, \
                mock.patch.object(
                    oe, "fetch_open_positions", return_value=[]
                ) as positions:
            result = oe.close_position(
                "BTC-USDT-SWAP",
                "live",
                pos_side="long",
                cycle_id=_CYCLE,
                _unwind=True,
                receipt_context={"cycle_id": _CYCLE},
            )
        self.assertTrue(result["ok"])
        positions.assert_called_once()
        deadline_mock.assert_not_called()

    def test_internal_protection_completion_bypasses_new_action_gate(self) -> None:
        dryrun, context, deadline = self._patch_common()
        with dryrun, context, deadline as deadline_mock, \
                mock.patch.object(
                    oe, "fetch_open_positions", return_value=[]
                ) as positions:
            result = oe.adjust_protection(
                "BTC-USDT-SWAP",
                "live",
                pos_side="long",
                new_sl_trigger_px=90,
                cycle_id=_CYCLE,
                reason_code="post_add_resize",
                receipt_context={"cycle_id": _CYCLE},
            )
        self.assertEqual(result["reject_reason"], "no_position")
        positions.assert_called_once()
        deadline_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
