# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from core import multitimeframe_gate as mtf_gate  # noqa: E402
import build_push_payload  # noqa: E402
import render_push_report  # noqa: E402
import validate_push_format  # noqa: E402


CYCLE = "2026-08-12T20:00"


def _card(symbol: str = "BTC-USDT-SWAP", side: str = "long") -> dict:
    cycle = mtf_gate.parse_cycle_cst(CYCLE)
    values = {
        "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0,
        "v": 1000.0, "ma5": 100.0, "ma20": 99.0,
        "atr14": 2.0, "rsi14": 55.0, "macd_hist": 0.5,
    }
    contract = mtf_gate.seal_evidence_contract({
        "protocol": mtf_gate.EVIDENCE_PROTOCOL,
        "mode": "read_only",
        "symbol": symbol,
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
        "direction_evidence": ["higher-timeframe structure supports long"],
        "opposing_evidence": ["15m momentum is mixed"],
        "execution_conditions": {"status": "exact closed bars ready"},
        "invalidation_point": {"condition": "4H structure breaks"},
        "risk_reward": {"summary": "bounded by stop"},
        "portfolio_impact": {"summary": "within deterministic limits"},
        "historical_experience": {
            "matched_wins": [],
            "matched_losses": [],
            "missed_opportunities": [],
            "usage": "none",
            "reason": "no matching sample overrides current evidence",
        },
        "agent_judgement": "select the strongest of three explicit analyses",
        "reference_overrides": [],
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
                    "evidence": [f"1H structure supports {side}"],
                    "relative_rank": 2,
                },
                "4H": {
                    "direction": side,
                    "evidence": [f"4H evidence is strongest for {side}"],
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
            "evidence_contract": contract,
        },
    }


def _payload(action: str = "OPEN_LONG", card: dict | None = None) -> dict:
    decision_card = copy.deepcopy(card if card is not None else _card())
    return {
        "cycle_id": CYCLE,
        "cycle_count": 1,
        "cycle_duration_s": 10,
        "hhmm": "20:00",
        "action_taken": action,
        "symbol": "BTC-USDT-SWAP",
        "assets": {
            "live": {
                "equity": 1000, "availBal": 800, "pnl": 1,
                "positions": 0,
            },
        },
        "positions": [],
        "risk": {
            "current_portfolio_imr_ratio": 0.1,
            "max_portfolio_imr_ratio": 0.666,
            "portfolio_imr_ratio_unit": "fraction",
            "lev": 5, "side_pct": 0, "position_count": 0,
            "status": "PASS",
        },
        "market": {
            "btc": 65000, "btc_chg24h": 1,
            "eth": 3500, "eth_chg24h": 1,
            "regime": "range", "dxy": 120,
        },
        "decision": {
            "summary": "three-timeframe decision fixture",
            "reason": "explicit market evidence fixture",
            "decision_protocol": "decision_card_v1",
            "decision_card": decision_card,
        },
        "execution": {"result": action, "db_rows_live": 0},
        "timeline": {"next_hh01_min": 60, "next_review_time": "08:05"},
        "exceptions": [],
    }


def _render(payload: dict) -> dict:
    patches = (
        mock.patch.object(
            render_push_report, "authoritative_cycle_count", return_value=None),
        mock.patch.object(
            render_push_report, "authoritative_cycle_duration", return_value=None),
        mock.patch.object(
            render_push_report, "authoritative_equity", return_value=None),
        mock.patch.object(
            render_push_report, "authoritative_cum_pnl", return_value=None),
        mock.patch.object(
            render_push_report, "authoritative_position_count", return_value=None),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        return render_push_report.render(payload)


class PushMultitimeframeReportTests(unittest.TestCase):
    def test_future_open_report_shows_and_validates_all_three_timeframes(self):
        rendered = _render(_payload())
        content = rendered["content"]
        result = validate_push_format.validate(content, cycle_id=CYCLE)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["multitimeframe_contract_required"])
        self.assertIn("OPEN/ADD覆盖=1/1", content)
        self.assertIn("交易对=BTC-USDT-SWAP side=long", content)
        for timeframe in ("15m", "1H", "4H"):
            self.assertIn(f"{timeframe} rank=", content)
        self.assertIn("选择=4H/long rank=1", content)
        self.assertIn("symbol=BTC-USDT-SWAP", content)
        self.assertIn("方法=三周期相对最优（非概率）", content)
        self.assertIn("校准可信度=未通过", content)
        self.assertIn("可信度声明=禁止", content)
        self.assertIn(
            _card()["multitimeframe_analysis"]["evidence_contract"][
                "evidence_hash"],
            content,
        )

    def test_future_open_missing_or_tampered_contract_is_fail_closed(self):
        missing = _payload(card={})
        missing_result = validate_push_format.validate(
            _render(missing)["content"], cycle_id=CYCLE)
        self.assertFalse(missing_result["ok"], missing_result)
        self.assertIn("15m/1H/4H判断", missing_result["missing_fields"])

        tampered_card = _card()
        tampered_card["multitimeframe_analysis"]["evidence_contract"][
            "timeframes"]["4H"]["values"]["rsi14"] = 99.0
        tampered_result = validate_push_format.validate(
            _render(_payload(card=tampered_card))["content"], cycle_id=CYCLE)
        self.assertFalse(tampered_result["ok"], tampered_result)

        wrong_symbol_card = _card()
        wrong_symbol_contract = wrong_symbol_card["multitimeframe_analysis"][
            "evidence_contract"]
        wrong_symbol_contract["symbol"] = "ETH-USDT-SWAP"
        wrong_symbol_card["multitimeframe_analysis"]["evidence_contract"] = (
            mtf_gate.seal_evidence_contract(wrong_symbol_contract))
        wrong_symbol_result = validate_push_format.validate(
            _render(_payload(card=wrong_symbol_card))["content"], cycle_id=CYCLE)
        self.assertFalse(wrong_symbol_result["ok"], wrong_symbol_result)

    def test_future_hold_is_explicitly_not_applicable(self):
        rendered = _render(_payload(action="HOLD", card={}))
        result = validate_push_format.validate(
            rendered["content"], cycle_id=CYCLE)
        self.assertTrue(result["ok"], result)
        self.assertIn("非OPEN/ADD，本轮不适用", rendered["content"])

    def test_upstream_failure_wait_report_is_explicit_and_valid(self):
        failure = {
            "failure_kind": "agent_idle_timeout",
        }
        payload = _payload(
            action="WAIT",
            card=build_push_payload._upstream_failure_card(failure),
        )
        payload["decision"]["origin"] = "system_failure_fallback"
        payload["decision"]["summary"] = (
            "上游失败，未形成交易业务终态；禁止下单，零新增风险")
        payload["decision"]["reason"] = (
            "Agent 未形成当轮判断；系统只生成失败闭环报告")
        payload["risk"]["status"] = "BLOCKED_UPSTREAM_FAILURE"
        payload["exceptions"] = [{
            "name": "live_trader",
            "status": "failed",
            "detail": "agent_idle_timeout；未补写 trade_cycles",
        }]

        rendered = _render(payload)
        result = validate_push_format.validate(
            rendered["content"], cycle_id=CYCLE)

        self.assertTrue(result["ok"], result)
        self.assertIn("系统失败闭环", rendered["content"])
        self.assertIn("Agent 未形成当轮判断", rendered["content"])
        self.assertIn("非OPEN/ADD，本轮不适用", rendered["content"])
        self.assertNotIn("Agent自主裁决 |", rendered["content"])

    def test_versioned_adjust_and_error_execution_audits(self):
        cycle = validate_push_format.EXECUTION_AUDIT_REQUIRED_FROM
        adjust = _payload(action="ADJUST", card={})
        adjust["cycle_id"] = cycle
        adjust["hhmm"] = "02:15"
        adjust["execution"]["result"] = (
            "ADJUST_PROTECTION LINK-USDT-SWAP no_fill path=amend sz=100 "
            "SL=8.5 TP=- algoId=A1 readback=verified/live "
            "exchange_side_effect=protection_only")
        adjust_result = validate_push_format.validate(
            _render(adjust)["content"], cycle_id=cycle)
        self.assertTrue(adjust_result["ok"], adjust_result)

        broken_adjust = copy.deepcopy(adjust)
        broken_adjust["execution"]["result"] = "ADJUST LINK fill=- stop=-"
        broken_result = validate_push_format.validate(
            _render(broken_adjust)["content"], cycle_id=cycle)
        self.assertFalse(broken_result["ok"], broken_result)
        self.assertIn("调保护回读状态", broken_result["missing_fields"])

        error = _payload(action="ERROR", card={})
        error["cycle_id"] = cycle
        error["hhmm"] = "02:15"
        error["execution"]["result"] = (
            "REJECT INTC no_fill orders=0 exchange_side_effect=none "
            "reason=data_not_ready detail=exact evidence missing")
        error_result = validate_push_format.validate(
            _render(error)["content"], cycle_id=cycle)
        self.assertTrue(error_result["ok"], error_result)

        historical = copy.deepcopy(broken_adjust)
        historical["cycle_id"] = "2026-08-14T02:00"
        historical_result = validate_push_format.validate(
            _render(historical)["content"], cycle_id="2026-08-14T02:00")
        self.assertTrue(historical_result["ok"], historical_result)

    def test_historical_archive_is_not_retroactively_failed(self):
        content = _render(_payload(action="HOLD", card={}))["content"]
        stripped = "\n".join(
            line for line in content.splitlines()
            if "🧩 三周期判断" not in line
            and "非OPEN/ADD，本轮不适用" not in line
        )
        old_result = validate_push_format.validate(
            stripped, cycle_id="2026-08-12T19:45")
        new_result = validate_push_format.validate(stripped, cycle_id=CYCLE)
        self.assertTrue(old_result["ok"], old_result)
        self.assertFalse(new_result["ok"], new_result)

    def test_compound_close_then_open_prefers_actual_open_trade_card(self):
        close_card = {"agent_judgement": "close card"}
        open_card = _card()
        rows = [
            {
                "symbol": "OLD-USDT-SWAP", "action": "close",
                "raw": {"decision_card": close_card},
            },
            {
                "symbol": "BTC-USDT-SWAP", "action": "open",
                "raw": {"decision_card": open_card},
            },
        ]
        self.assertEqual(
            open_card, build_push_payload._first_open_trade_card(rows))

    def test_multiple_open_symbols_are_each_reported_and_validated(self):
        btc_card = _card("BTC-USDT-SWAP", "long")
        eth_card = _card("ETH-USDT-SWAP", "long")
        payload = _payload(card=btc_card)
        payload["symbol"] = "BTC/ETH"
        payload["decision"]["multitimeframe_analyses"] = [
            {
                "symbol": "BTC-USDT-SWAP", "side": "long",
                "decision_card": btc_card, "conflicting_cards": False,
            },
            {
                "symbol": "ETH-USDT-SWAP", "side": "long",
                "decision_card": eth_card, "conflicting_cards": False,
            },
        ]
        rendered = _render(payload)
        result = validate_push_format.validate(
            rendered["content"], cycle_id=CYCLE)
        self.assertTrue(result["ok"], result)
        self.assertIn("OPEN/ADD覆盖=2/2", rendered["content"])
        self.assertIn("交易对=BTC-USDT-SWAP side=long", rendered["content"])
        self.assertIn("交易对=ETH-USDT-SWAP side=long", rendered["content"])

    def test_repeated_leg_merges_but_conflicting_cards_fail_closed(self):
        original = _card()
        rows = [
            {
                "symbol": "BTC-USDT-SWAP", "action": "open", "side": "long",
                "raw": {"decision_card": original},
            },
            {
                "symbol": "BTC-USDT-SWAP", "action": "add", "side": "long",
                "raw": {"decision_card": original},
            },
        ]
        merged = build_push_payload._open_trade_decisions(rows)
        self.assertEqual(1, len(merged))
        self.assertFalse(merged[0]["conflicting_cards"])

        conflict = copy.deepcopy(original)
        conflict["agent_judgement"] = "different frozen decision"
        rows[1]["raw"]["decision_card"] = conflict
        conflicting = build_push_payload._open_trade_decisions(rows)
        self.assertEqual(1, len(conflicting))
        self.assertTrue(conflicting[0]["conflicting_cards"])

        payload = _payload(card=original)
        payload["decision"]["multitimeframe_analyses"] = conflicting
        result = validate_push_format.validate(
            _render(payload)["content"], cycle_id=CYCLE)
        self.assertFalse(result["ok"], result)
        self.assertIn("OPEN/ADD全覆盖", result["missing_fields"])


if __name__ == "__main__":
    unittest.main()
