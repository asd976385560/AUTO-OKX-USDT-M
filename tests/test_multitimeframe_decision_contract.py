# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from core.decision_card import validate_multitimeframe_analysis
from core import multitimeframe_gate as mtf_gate


CYCLE = "2026-08-12T18:30"


def card(side: str = "long") -> dict:
    cycle = mtf_gate.parse_cycle_cst(CYCLE)
    values = {
        "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0,
        "v": 1000.0, "ma5": 100.0, "ma20": 99.0,
        "atr14": 2.0, "rsi14": 55.0, "macd_hist": 0.5,
    }
    evidence_contract = mtf_gate.seal_evidence_contract({
        "protocol": mtf_gate.EVIDENCE_PROTOCOL,
        "mode": "read_only",
        "symbol": "BTC-USDT-SWAP",
        "cycle_id": CYCLE,
        "required_timeframes": list(mtf_gate.TIMEFRAME_SECONDS),
        "minimum_bars_for_full_indicators": (
            mtf_gate.MINIMUM_BARS_FOR_FULL_INDICATORS),
        "timeframes": {
            timeframe: {
                "expected_closed_bar_ts": (
                    mtf_gate.expected_closed_bar_start(cycle, timeframe)),
                "observed_bar_ts": (
                    mtf_gate.expected_closed_bar_start(cycle, timeframe)),
                "bars_seen": mtf_gate.MINIMUM_BARS_FOR_FULL_INDICATORS,
                "ready": True,
                "values": dict(values),
            }
            for timeframe in mtf_gate.TIMEFRAME_SECONDS
        },
        "production_database_writes": 0,
        "orders_placed": 0,
    })
    return {
        "multitimeframe_analysis": {
            "cycle_id": CYCLE,
            "required_timeframes": ["15m", "1H", "4H"],
            "timeframes": {
                "15m": {
                    "direction": "neutral",
                    "evidence": ["15m momentum is mixed"],
                    "relative_rank": 3,
                },
                "1H": {
                    "direction": side,
                    "evidence": ["1H structure supports the side"],
                    "relative_rank": 2,
                },
                "4H": {
                    "direction": side,
                    "evidence": ["4H structure is strongest"],
                    "relative_rank": 1,
                },
            },
            "selected_timeframe": "4H",
            "selected_direction": side,
            "selection_reason": "4H has the strongest relative evidence",
            "selection_method": (
                "relative_rank_1_among_15m_1H_4H_not_calibrated"),
            "calibrated_confidence": None,
            "confidence_claim_allowed": False,
            "evidence_contract": evidence_contract,
        }
    }


class MultitimeframeDecisionContractTests(unittest.TestCase):
    def test_complete_relative_selection_passes(self):
        self.assertEqual(
            validate_multitimeframe_analysis(
                card(), expected_cycle=CYCLE, expected_side="long"),
            [],
        )

    def test_all_three_timeframes_and_unique_ranks_are_required(self):
        value = card()
        del value["multitimeframe_analysis"]["timeframes"]["15m"]
        errors = validate_multitimeframe_analysis(
            value, expected_cycle=CYCLE, expected_side="long")
        self.assertTrue(any("15m/1H/4H" in item for item in errors), errors)

        value = card()
        value["multitimeframe_analysis"]["timeframes"]["1H"][
            "relative_rank"] = 1
        errors = validate_multitimeframe_analysis(
            value, expected_cycle=CYCLE, expected_side="long")
        self.assertTrue(any("恰为 1,2,3" in item for item in errors), errors)

    def test_selection_must_be_rank_one_and_match_open_side(self):
        value = card()
        value["multitimeframe_analysis"]["selected_timeframe"] = "1H"
        errors = validate_multitimeframe_analysis(
            value, expected_cycle=CYCLE, expected_side="long")
        self.assertTrue(any("relative_rank=1" in item for item in errors), errors)

        value = card("short")
        errors = validate_multitimeframe_analysis(
            value, expected_cycle=CYCLE, expected_side="long")
        self.assertTrue(any("OPEN/ADD side" in item for item in errors), errors)

    def test_numeric_confidence_claim_is_forbidden_until_gate_proven(self):
        value = card()
        value["multitimeframe_analysis"]["calibrated_confidence"] = 0.95
        value["multitimeframe_analysis"]["confidence_claim_allowed"] = True
        errors = validate_multitimeframe_analysis(
            value, expected_cycle=CYCLE, expected_side="long")
        self.assertTrue(any("必须为 null" in item for item in errors), errors)
        self.assertTrue(any("必须为 false" in item for item in errors), errors)

    def test_cycle_is_bound_to_dispatched_cycle(self):
        errors = validate_multitimeframe_analysis(
            card(), expected_cycle="2026-08-12T18:45", expected_side="long")
        self.assertTrue(any("与本轮" in item for item in errors), errors)

    def test_tampered_market_evidence_hash_is_rejected(self):
        value = card()
        value["multitimeframe_analysis"]["evidence_contract"][
            "timeframes"]["4H"]["values"]["rsi14"] = 70.0
        errors = validate_multitimeframe_analysis(
            value,
            expected_cycle=CYCLE,
            expected_side="long",
            expected_symbol="BTC-USDT-SWAP",
        )
        self.assertTrue(any("evidence_hash mismatch" in item for item in errors), errors)


if __name__ == "__main__":
    unittest.main()
