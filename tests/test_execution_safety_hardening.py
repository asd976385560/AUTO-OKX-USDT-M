# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
COLLECTORS = ROOT / "collectors"
for module_path in (CORE, COLLECTORS):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

import order_executor as oe  # noqa: E402
import risk_validator as rv  # noqa: E402
import trades_writer  # noqa: E402
from core import multitimeframe_gate as mtf_gate  # noqa: E402
from experience_contract import build_contract  # noqa: E402


_DEADLINE_PATCHER = None


def setUpModule() -> None:
    """Historical fixtures isolate executor branches; deadline has its own tests."""
    global _DEADLINE_PATCHER
    _DEADLINE_PATCHER = mock.patch.object(
        oe, "_cycle_side_effect_reject", return_value=None)
    _DEADLINE_PATCHER.start()


def tearDownModule() -> None:
    if _DEADLINE_PATCHER is not None:
        _DEADLINE_PATCHER.stop()


def _same_actor_timeline(*_args, **_kwargs) -> dict:
    """Deterministic fixture for tests that intentionally reach live OPEN gates."""
    return {
        "available": True,
        "handoff_detected": False,
        "analysis_epoch": 0,
        "current_epoch": 0,
        "actor_chain_hash": "fixture-same-actor",
    }


def _ready_multitimeframe(*_args, **_kwargs) -> dict:
    symbol = str(_args[1])
    cycle_id = str(_args[2])
    return {
        "contract_version": 1,
        "mode": "read_only",
        "ready": True,
        "status": "PASSED",
        "reject_reason": None,
        "timeframes": [],
        "evidence_contract": _market_evidence_contract(cycle_id, symbol),
        "production_database_writes": 0,
        "orders_placed": 0,
    }


def _ready_evidence_anchor(*_args, **_kwargs) -> dict:
    """Pass the independent execution-anchor gate in downstream unit tests.

    These cases deliberately exercise ledger, account-IMR and fill/protection
    branches after the MTF gates.  The MTF anchor itself has dedicated tests;
    mocking only readiness leaves these fixtures stopped at the newer,
    separate supplied/current evidence comparison.
    """
    return {
        "ok": True,
        "mode": "read_only",
        "evidence_anchor": "current_market_exact",
        "post_analysis_market_revision": False,
        "production_database_writes": 0,
        "orders_placed": 0,
    }


def _market_evidence_contract(cycle_id: str, symbol: str) -> dict:
    try:
        cycle = mtf_gate.parse_cycle_cst(cycle_id)
    except ValueError:
        # Legacy unit-test sentinels predate the canonical production cycle
        # contract.  Keep them isolated; production paths remain strict.
        cycle = mtf_gate.parse_cycle_cst("2026-08-12T18:30")
    values = {
        "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0,
        "v": 1000.0, "ma5": 100.0, "ma20": 99.0,
        "atr14": 2.0, "rsi14": 55.0, "macd_hist": 0.5,
    }
    return mtf_gate.seal_evidence_contract({
        "protocol": mtf_gate.EVIDENCE_PROTOCOL,
        "mode": "read_only",
        "symbol": symbol,
        "cycle_id": cycle_id,
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


def _multitimeframe_analysis(
    cycle_id: str,
    side: str = "long",
    symbol: str = "BTC-USDT-SWAP",
) -> dict:
    opposite = "short" if side == "long" else "long"
    return {
        "cycle_id": cycle_id,
        "required_timeframes": ["15m", "1H", "4H"],
        "timeframes": {
            "15m": {
                "direction": side,
                "evidence": ["isolated 15m closed-bar evidence"],
                "relative_rank": 2,
            },
            "1H": {
                "direction": opposite,
                "evidence": ["isolated 1H counter-evidence"],
                "relative_rank": 3,
            },
            "4H": {
                "direction": side,
                "evidence": ["isolated 4H selected evidence"],
                "relative_rank": 1,
            },
        },
        "selected_timeframe": "4H",
        "selected_direction": side,
        "selection_reason": "4H is relatively strongest in this fixture",
        "selection_method": (
            "relative_rank_1_among_15m_1H_4H_not_calibrated"
        ),
        "calibrated_confidence": None,
        "confidence_claim_allowed": False,
        "evidence_contract": _market_evidence_contract(cycle_id, symbol),
    }


def _valid_receipt_context(
    cycle_id: str,
    side: str = "long",
    symbol: str = "BTC-USDT-SWAP",
) -> dict:
    return {
        "cycle_id": cycle_id,
        "status": "ok",
        "decision": "traded",
        "decision_protocol": "decision_card_v1",
        "decision_card": {
            "direction_evidence": ["isolated execution contract test"],
            "opposing_evidence": ["no external order is sent"],
            "execution_conditions": {"status": "mocked exchange endpoints"},
            "invalidation_point": {"condition": "any contract mismatch"},
            "risk_reward": {"summary": "test-only"},
            "portfolio_impact": {"summary": "temporary isolated state"},
            "historical_experience": {
                "matched_wins": [],
                "matched_losses": [],
                "missed_opportunities": [],
                "usage": "none",
                "reason": "deterministic unit test",
            },
            "agent_judgement": "exercise the deterministic close contract",
            "reference_overrides": [],
            "multitimeframe_analysis": _multitimeframe_analysis(
                cycle_id, side, symbol),
        },
    }


def _empty_experience_contract(cycle_id: str, symbol: str,
                               side: str, regime: str = "range") -> dict:
    def summary(scope: str) -> dict:
        return {
            "scope": scope,
            "n": 0,
            "wins": 0,
            "losses": 0,
            "sufficient": False,
            "credibility": 0.0,
            "reason": "no_experiences",
        }

    return build_contract(
        {
            "symbol": symbol,
            "side": side,
            "regime": regime,
            "action": "open",
            "profile": "live",
            "as_of": cycle_id.replace("T", " ") + ":00",
            "min_sim": 0.5,
            "top_k": 8,
        },
        exact_setup=summary("same_symbol_side_action_regime"),
        same_symbol_similar=summary("same_symbol_similar"),
        cross_symbol_similar=summary("cross_symbol_similar"),
    )


class ExecutionIntentProfileGateTests(unittest.TestCase):
    @staticmethod
    def _reserve(path: Path, cycle: str, symbol: str,
                 *, profile: str = "live", side: str = "long"):
        request = {
            "profile": profile,
            "cycle_id": cycle,
            "symbol": symbol,
            "action": "open",
            "side": side,
            "intended_sz": 1.0,
            "lev": 5.0,
            "sl_trigger_px": 95.0,
            "mgn_mode": "cross",
        }
        return oe.ei.reserve(
            path, profile=profile, cycle_id=cycle, symbol=symbol,
            side=side, request=request, now_ts="2026-07-29 10:00:00")

    @staticmethod
    def _transition(path: Path, reserved: dict, cycle: str, symbol: str,
                    state: str, *, profile: str = "live",
                    side: str = "long"):
        kwargs = {
            "profile": profile, "cycle_id": cycle, "symbol": symbol,
            "side": side, "fingerprint": reserved["fingerprint"],
            "now_ts": "2026-07-29 10:01:00",
        }
        if state == "completed":
            oe.ei.mark_completed(
                path, receipt={"ok": True, "cycle_id": cycle}, **kwargs)
        elif state == "failed_clean":
            oe.ei.mark_failed_clean(path, error="confirmed_no_fill", **kwargs)
        else:
            raise AssertionError(state)

    def test_pending_other_symbol_blocks_before_all_exchange_io(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_path = root / "ledger.db"
            first = self._reserve(
                ledger_path, "CYCLE-PENDING", "BTC-USDT-SWAP")
            self.assertEqual(first["status"], "reserved")

            exchange_mocks = {
                name: mock.Mock(name=name)
                for name in (
                    "get_balance", "get_positions", "get_mark_price",
                    "set_leverage", "place_market_open", "place_algo_sl",
                )
            }
            repair_mock = mock.Mock()
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    oe.ox, "is_dryrun", return_value=False))
                stack.enter_context(mock.patch.object(
                    oe, "validate_receipt_context", return_value=[]))
                stack.enter_context(mock.patch.object(
                    oe.actor_att, "timeline_state",
                    side_effect=_same_actor_timeline))
                stack.enter_context(mock.patch.object(
                    oe, "_enqueue_repair", repair_mock))
                for name, method_mock in exchange_mocks.items():
                    stack.enter_context(mock.patch.object(
                        oe.ox, name, method_mock))
                result = oe.open_position(
                    "ETH-USDT-SWAP", "long", 1.0, 5.0, 95.0, "live",
                    db_root=root, cycle_id="CYCLE-NEW",
                    receipt_context={"cycle_id": "CYCLE-NEW"},
                )

            self.assertFalse(result["ok"])
            self.assertEqual(
                result["reject_reason"], "execution_intent_profile_blocked")
            self.assertEqual(
                result["blocking_intent"]["symbol"], "BTC-USDT-SWAP")
            self.assertEqual(result["pending_intent_count"], 1)
            for name, method_mock in exchange_mocks.items():
                with self.subTest(exchange_method=name):
                    method_mock.assert_not_called()
            repair_reason = repair_mock.call_args.args[3]
            self.assertIn("profile_pending_intent", repair_reason)
            self.assertIn("BTC-USDT-SWAP", repair_reason)

            con = sqlite3.connect(ledger_path)
            try:
                rows = con.execute(
                    "SELECT cycle_id,symbol,state FROM execution_intents "
                    "ORDER BY cycle_id").fetchall()
            finally:
                con.close()
            self.assertEqual(
                rows,
                [("CYCLE-PENDING", "BTC-USDT-SWAP", "reserved")],
            )

    def test_completed_and_failed_clean_do_not_block_profile(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.db"
            clean = self._reserve(path, "CLEAN", "BTC-USDT-SWAP")
            self._transition(
                path, clean, "CLEAN", "BTC-USDT-SWAP", "failed_clean")

            done = self._reserve(path, "DONE", "ETH-USDT-SWAP")
            self.assertEqual(done["status"], "reserved")
            self._transition(
                path, done, "DONE", "ETH-USDT-SWAP", "completed")

            next_intent = self._reserve(path, "NEXT", "SOL-USDT-SWAP")
            self.assertEqual(next_intent["status"], "reserved")

    def test_same_key_replay_and_failed_clean_reuse_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.db"
            done = self._reserve(path, "DONE", "BTC-USDT-SWAP")
            self._transition(
                path, done, "DONE", "BTC-USDT-SWAP", "completed")
            replay = self._reserve(path, "DONE", "BTC-USDT-SWAP")
            self.assertEqual(replay["status"], "replay")
            self.assertEqual(replay["receipt"]["cycle_id"], "DONE")

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.db"
            clean = self._reserve(path, "CLEAN", "BTC-USDT-SWAP")
            self._transition(
                path, clean, "CLEAN", "BTC-USDT-SWAP", "failed_clean")
            reused = self._reserve(path, "CLEAN", "BTC-USDT-SWAP")
            self.assertEqual(reused["status"], "reserved")
            self.assertTrue(reused["reused_failed_clean"])

    def test_reduce_intent_key_is_distinct_and_replays_by_action(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.db"
            request = {
                "profile": "live", "cycle_id": "REDUCE-CYCLE",
                "symbol": "BTC-USDT-SWAP", "action": "reduce",
                "side": "long", "reduce_sz": 2.0,
            }
            reserved = oe.ei.reserve(
                path, profile="live", cycle_id="REDUCE-CYCLE",
                symbol="BTC-USDT-SWAP", side="long", action="reduce",
                request=request, now_ts="2026-08-13 21:00:00")
            self.assertEqual(reserved["status"], "reserved")
            oe.ei.mark_completed(
                path, profile="live", cycle_id="REDUCE-CYCLE",
                symbol="BTC-USDT-SWAP", side="long", action="reduce",
                fingerprint=reserved["fingerprint"],
                now_ts="2026-08-13 21:01:00",
                receipt={"ok": True, "action_taken": "REDUCE"})
            replay = oe.ei.reserve(
                path, profile="live", cycle_id="REDUCE-CYCLE",
                symbol="BTC-USDT-SWAP", side="long", action="reduce",
                request=request, now_ts="2026-08-13 21:02:00")
            self.assertEqual(replay["status"], "replay")
            self.assertEqual(replay["receipt"]["action_taken"], "REDUCE")
            con = sqlite3.connect(path)
            try:
                row = con.execute(
                    "SELECT action,submitted_at,completed_at "
                    "FROM execution_intents WHERE cycle_id=?",
                    ("REDUCE-CYCLE",)).fetchone()
            finally:
                con.close()
            self.assertEqual(row[0], "reduce")
            # completed is itself proof of a submitted exchange side effect;
            # failure reports must never classify this row as pristine/clean.
            self.assertTrue(row[1])
            self.assertTrue(row[2])


class PretradeLedgerPositionGateTests(unittest.TestCase):
    @staticmethod
    def _create_trade_db(root: Path, rows, *, profile: str = "live"):
        path = root / f"{profile}_trades.db"
        con = sqlite3.connect(path)
        try:
            con.execute(
                "CREATE TABLE trades("
                "symbol TEXT,action TEXT,side TEXT,sz REAL)")
            con.executemany(
                "INSERT INTO trades(symbol,action,side,sz) VALUES(?,?,?,?)",
                rows,
            )
            con.commit()
        finally:
            con.close()
        return path

    @staticmethod
    def _intent_state(root: Path, cycle: str):
        con = sqlite3.connect(root / "ledger.db")
        try:
            row = con.execute(
                "SELECT state FROM execution_intents WHERE profile='live' "
                "AND cycle_id=?", (cycle,)).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def _run_open(self, root: Path, api_positions, *, cycle: str):
        repair_mock = mock.Mock()
        mark_mock = mock.Mock(return_value=None)
        order_mocks = {
            "set_leverage": mock.Mock(),
            "place_market_open": mock.Mock(),
            "place_algo_sl": mock.Mock(),
        }
        with ExitStack() as stack:
            # This helper exercises the original pretrade ledger gate.  Autoheal
            # has its own contract tests below and must not change these cases'
            # expected reject reason.
            stack.enter_context(mock.patch.dict(
                "os.environ", {"OKX_DISABLE_LEDGER_AUTOHEAL": "1"}, clear=False))
            stack.enter_context(mock.patch.object(
                oe.ox, "is_dryrun", return_value=False))
            stack.enter_context(mock.patch.object(
                oe, "validate_receipt_context", return_value=[]))
            stack.enter_context(mock.patch.object(
                oe.actor_att, "timeline_state",
                side_effect=_same_actor_timeline))
            stack.enter_context(mock.patch.object(
                oe, "check_multitimeframe_readiness",
                side_effect=_ready_multitimeframe))
            stack.enter_context(mock.patch.object(
                oe, "resolve_execution_evidence_anchor",
                side_effect=_ready_evidence_anchor))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_balance", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(
                oe.ac, "extract_settlement_capacity",
                return_value={
                    "ok": True, "total_equity": 1000.0,
                    "available_margin": 900.0, "settlement_ccy": "USDT",
                    "account_imr": 100.0,
                }))
            positions_mock = stack.enter_context(mock.patch.object(
                oe, "fetch_open_positions", return_value=api_positions))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_mark_price", mark_mock))
            for name, method_mock in order_mocks.items():
                stack.enter_context(mock.patch.object(
                    oe.ox, name, method_mock))
            stack.enter_context(mock.patch.object(
                oe, "_enqueue_repair", repair_mock))
            result = oe.open_position(
                "SOL-USDT-SWAP", "long", 1.0, 5.0, 90.0, "live",
                db_root=root, cycle_id=cycle,
                # 故意传与 API 不同的 caller 快照，闸门必须完全忽略它。
                open_positions=[
                    {"symbol": "CALLER-ONLY", "side": "short", "sz": 999.0}],
                receipt_context=_valid_receipt_context(
                    cycle, "long", "SOL-USDT-SWAP"),
            )
        return result, positions_mock, mark_mock, order_mocks, repair_mock

    def test_matching_full_set_reaches_following_mark_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trade_db = self._create_trade_db(root, [
                ("BTC-USDT-SWAP", "open", "long", 3.0),
                ("BTC-USDT-SWAP", "close", "long", 1.0),
                ("ETH-USDT-SWAP", "open", "short", 1.5),
                ("ETH-USDT-SWAP", "reduce", "short", 0.5),
                ("IGNORED-USDT-SWAP", "none", None, None),
            ])
            before_bytes = trade_db.read_bytes()
            result, positions_mock, mark_mock, order_mocks, repair_mock = (
                self._run_open(
                    root,
                    [
                        {"symbol": "BTC-USDT-SWAP", "side": "long", "sz": 2.0},
                        {"symbol": "ETH-USDT-SWAP", "side": "short", "sz": 1.0},
                    ],
                    cycle="MATCH",
                )
            )
            positions_mock.assert_called_once()
            mark_mock.assert_called_once()
            self.assertEqual(result["reject_reason"], "mark_px_fetch_failed")
            self.assertTrue(result["position_reconciliation"]["ok"])
            self.assertEqual(self._intent_state(root, "MATCH"), "failed_clean")
            for method_mock in order_mocks.values():
                method_mock.assert_not_called()
            repair_mock.assert_not_called()
            self.assertEqual(trade_db.read_bytes(), before_bytes)

    def test_db_more_and_exchange_more_both_fail_before_mark_or_order(self):
        cases = [
            (
                "DB-MORE",
                [("BTC-USDT-SWAP", "open", "long", 2.0)],
                [],
                "BTC-USDT-SWAP",
            ),
            (
                "EXCHANGE-MORE",
                [],
                [{"symbol": "ETH-USDT-SWAP", "side": "short", "sz": 3.0}],
                "ETH-USDT-SWAP",
            ),
        ]
        for cycle, rows, api_positions, expected_symbol in cases:
            with self.subTest(cycle=cycle), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                self._create_trade_db(root, rows)
                result, positions_mock, mark_mock, order_mocks, repair_mock = (
                    self._run_open(root, api_positions, cycle=cycle)
                )
                positions_mock.assert_called_once()
                mark_mock.assert_not_called()
                for method_mock in order_mocks.values():
                    method_mock.assert_not_called()
                self.assertEqual(
                    result["reject_reason"],
                    "pretrade_ledger_position_mismatch",
                )
                self.assertEqual(self._intent_state(root, cycle), "failed_clean")
                self.assertEqual(
                    result["position_reconciliation"]["diffs"][0]["symbol"],
                    expected_symbol,
                )
                repair_reason = repair_mock.call_args.args[3]
                self.assertIn("pretrade_ledger_position_mismatch", repair_reason)
                self.assertIn(expected_symbol, repair_reason)

    def test_autoheal_blocking_contract_stops_before_recheck_mark_risk_or_order(self):
        blocked = {
            "contract_version": 1,
            "request_id": "REQ-BLOCK",
            "profile": "live",
            "cycle": "AUTOHEAL-BLOCK",
            "db_root": "isolated",
            "status": "p0_blocked",
            "applied": False,
            "p0": True,
            "blocking": True,
            "findings": [{
                "kind": "NAKED-POSITION-P0", "sev": "P0",
                "symbol": "BTC-USDT-SWAP", "side": "long",
                "reason": "protective SL not confirmed",
            }],
            "healed": [],
            "needs_human": [],
            "rc": 4,
        }
        risk = mock.Mock()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(oe, "_try_autoheal_ledger",
                               return_value=blocked) as autoheal, \
             mock.patch.object(oe.rv, "validate", risk):
            root = Path(td)
            self._create_trade_db(root, [
                ("BTC-USDT-SWAP", "open", "long", 2.0),
            ])
            result, positions_mock, mark_mock, order_mocks, repair_mock = (
                self._run_open(root, [], cycle="AUTOHEAL-BLOCK"))
            intent_state = self._intent_state(root, "AUTOHEAL-BLOCK")

        positions_mock.assert_called_once()
        autoheal.assert_called_once()
        mark_mock.assert_not_called()
        risk.assert_not_called()
        for method_mock in order_mocks.values():
            method_mock.assert_not_called()
        self.assertEqual(
            result["reject_reason"], "pretrade_ledger_autoheal_blocked")
        self.assertTrue(result["p0"])
        self.assertEqual(
            result["position_reconciliation"]["autoheal"]["rc"], 4)
        self.assertEqual(intent_state, "failed_clean")
        repair_reason = repair_mock.call_args.args[3]
        self.assertIn("pretrade_ledger_autoheal_blocked", repair_reason)

    def test_missing_trade_db_fails_closed_before_mark_or_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result, positions_mock, mark_mock, order_mocks, repair_mock = (
                self._run_open(root, [], cycle="LEDGER-MISSING")
            )
            positions_mock.assert_called_once()
            mark_mock.assert_not_called()
            for method_mock in order_mocks.values():
                method_mock.assert_not_called()
            self.assertEqual(result["reject_reason"], "ledger_unavailable")
            self.assertEqual(
                self._intent_state(root, "LEDGER-MISSING"), "failed_clean")
            repair_reason = repair_mock.call_args.args[3]
            self.assertIn("pretrade_ledger_unavailable", repair_reason)
            self.assertIn("live_trades.db:missing", repair_reason)

    def test_trade_db_query_failure_is_ledger_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            con = sqlite3.connect(root / "live_trades.db")
            try:
                con.execute("CREATE TABLE not_trades(x INTEGER)")
                con.commit()
            finally:
                con.close()
            result, positions_mock, mark_mock, order_mocks, repair_mock = (
                self._run_open(root, [], cycle="LEDGER-BAD-SCHEMA")
            )
            positions_mock.assert_called_once()
            mark_mock.assert_not_called()
            for method_mock in order_mocks.values():
                method_mock.assert_not_called()
            self.assertEqual(result["reject_reason"], "ledger_unavailable")
            self.assertIn(
                "query_failed", result["position_reconciliation"]["error"])
            self.assertEqual(
                self._intent_state(root, "LEDGER-BAD-SCHEMA"), "failed_clean")
            self.assertIn(
                "query_failed", repair_mock.call_args.args[3])

    def test_ledger_position_compare_uses_numeric_tolerance(self):
        """0.1+0.2 != 0.3 的浮点累加不得被判成账仓不一致。

        原为 demo profile 用例（2026-08-06 demo 全量下线后改打 live）——容差本身
        与 profile 无关，是账本累加与交易所 sz 比对的通用契约。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._create_trade_db(
                root,
                [
                    ("BTC-USDT-SWAP", "open", "long", 0.1),
                    ("BTC-USDT-SWAP", "add", "long", 0.2),
                ],
            )
            result = oe._verify_pretrade_ledger_positions(
                "live", root,
                [{"symbol": "BTC-USDT-SWAP", "side": "long", "sz": 0.3}],
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["profile"], "live")
            self.assertEqual(result["ledger_groups"], 1)
            self.assertEqual(result["exchange_groups"], 1)


class LiveAccountImrGateTests(unittest.TestCase):
    @staticmethod
    def _rejected_risk(reason: str = "test_stop") -> dict:
        return {
            "approved": False,
            "approved_sz": 0.0,
            "clamped": False,
            "adjustments": [],
            "reject_reason": reason,
            "reject_detail": "stop before exchange writes",
            "math": {},
        }

    def _run_live(self, capacity: dict, *, caller_account_imr=0.0,
                  use_real_validator: bool = False):
        get_balance = mock.Mock(return_value={"ok": True, "data": [{}]})
        extract_capacity = mock.Mock(return_value=capacity)
        fetch_positions = mock.Mock(return_value=[])
        verify_positions = mock.Mock(return_value={
            "ok": True,
            "profile": "live",
            "ledger_groups": 0,
            "exchange_groups": 0,
            "diffs": [],
        })
        get_mark = mock.Mock(return_value=100.0)
        fetch_specs = mock.Mock(return_value={
            "ct_val": 1.0,
            "lot_sz": 0.1,
            "min_sz": 0.1,
            "source": "test",
            "spec_source": "test",
        })
        set_leverage = mock.Mock(return_value={"ok": True})
        place_market_open = mock.Mock()
        place_algo_sl = mock.Mock()
        validate_mock = mock.Mock(
            return_value=self._rejected_risk())

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                oe.ox, "is_dryrun", return_value=False))
            stack.enter_context(mock.patch.object(
                oe, "validate_receipt_context", return_value=[]))
            stack.enter_context(mock.patch.object(
                oe.actor_att, "timeline_state",
                side_effect=_same_actor_timeline))
            stack.enter_context(mock.patch.object(
                oe, "check_multitimeframe_readiness",
                side_effect=_ready_multitimeframe))
            stack.enter_context(mock.patch.object(
                oe, "resolve_execution_evidence_anchor",
                side_effect=_ready_evidence_anchor))
            stack.enter_context(mock.patch.object(
                oe.ei, "reserve",
                return_value={"status": "reserved", "fingerprint": "FP"}))
            stack.enter_context(mock.patch.object(oe.ei, "mark_failed_clean"))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_balance", get_balance))
            stack.enter_context(mock.patch.object(
                oe.ac, "extract_settlement_capacity", extract_capacity))
            stack.enter_context(mock.patch.object(
                oe, "fetch_open_positions", fetch_positions))
            stack.enter_context(mock.patch.object(
                oe, "_verify_pretrade_ledger_positions", verify_positions))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_mark_price", get_mark))
            stack.enter_context(mock.patch.object(
                oe, "fetch_instrument_specs", fetch_specs))
            if not use_real_validator:
                stack.enter_context(mock.patch.object(
                    oe.rv, "validate", validate_mock))
            stack.enter_context(mock.patch.object(
                oe.ox, "set_leverage", set_leverage))
            stack.enter_context(mock.patch.object(
                oe.ox, "place_market_open", place_market_open))
            stack.enter_context(mock.patch.object(
                oe.ox, "place_algo_sl", place_algo_sl))

            result = oe.open_position(
                "BTC-USDT-SWAP", "long", 0.1, 5.0, 95.0, "live",
                equity=1.0,
                available_margin=1.0,
                account_imr=caller_account_imr,
                cycle_id="IMR-GATE",
                receipt_context=_valid_receipt_context(
                    "IMR-GATE", "long", "BTC-USDT-SWAP"),
            )

        return {
            "result": result,
            "get_balance": get_balance,
            "extract_capacity": extract_capacity,
            "fetch_positions": fetch_positions,
            "verify_positions": verify_positions,
            "get_mark": get_mark,
            "fetch_specs": fetch_specs,
            "validate": validate_mock,
            "set_leverage": set_leverage,
            "place_market_open": place_market_open,
            "place_algo_sl": place_algo_sl,
        }

    def test_non_dryrun_live_uses_api_account_imr_and_ignores_caller(self):
        state = self._run_live({
            "ok": True,
            "total_equity": 100.0,
            "available_margin": 90.0,
            "settlement_ccy": "USDT",
            "source": "details.USDT.min(availBal,availEq)",
            "account_imr": 12.5,
        }, caller_account_imr=9999.0)

        state["get_balance"].assert_called_once_with("live")
        state["extract_capacity"].assert_called_once()
        self.assertEqual(
            state["validate"].call_args.kwargs["account_imr"], 12.5)
        self.assertEqual(state["result"]["capacity"]["account_imr"], 12.5)
        self.assertEqual(
            state["result"]["capacity"]["source"],
            "details.USDT.min(availBal,availEq)",
        )
        state["set_leverage"].assert_not_called()
        state["place_market_open"].assert_not_called()

    def test_missing_or_invalid_api_account_imr_fails_before_mark_and_writes(self):
        for value in (None, "nan", -1.0):
            with self.subTest(account_imr=value):
                state = self._run_live({
                    "ok": True,
                    "total_equity": 100.0,
                    "available_margin": 90.0,
                    "settlement_ccy": "USDT",
                    "account_imr": value,
                })

                self.assertFalse(state["result"]["ok"])
                self.assertEqual(
                    state["result"]["reject_reason"],
                    "account_imr_fetch_failed",
                )
                state["fetch_positions"].assert_not_called()
                state["verify_positions"].assert_not_called()
                state["get_mark"].assert_not_called()
                state["fetch_specs"].assert_not_called()
                state["validate"].assert_not_called()
                state["set_leverage"].assert_not_called()
                state["place_market_open"].assert_not_called()
                state["place_algo_sl"].assert_not_called()

    def test_projected_portfolio_imr_over_66_6pct_never_writes_exchange(self):
        state = self._run_live({
            "ok": True,
            "total_equity": 100.0,
            "available_margin": 90.0,
            "settlement_ccy": "USDT",
            "source": "details.USDT.min(availBal,availEq)",
            "account_imr": 66.7,
        }, caller_account_imr=0.0, use_real_validator=True)

        self.assertFalse(state["result"]["ok"])
        self.assertEqual(
            state["result"]["reject_reason"],
            "portfolio_margin_cap_exceeded",
        )
        self.assertAlmostEqual(
            state["result"]["risk"]["math"]["account_imr"], 66.7)
        self.assertGreater(
            state["result"]["risk"]["math"]["projected_portfolio_imr_ratio"],
            state["result"]["risk"]["math"]["max_portfolio_imr_ratio"],
        )
        state["set_leverage"].assert_not_called()
        state["place_market_open"].assert_not_called()
        state["place_algo_sl"].assert_not_called()


class StopLossDirectionTests(unittest.TestCase):
    def _validate(self, side: str, sl: float):
        return rv.validate(
            symbol="BTC-USDT-SWAP",
            side=side,
            intended_sz=1.0,
            lev=5.0,
            mark_px=100.0,
            ct_val=1.0,
            lot_sz=1.0,
            equity=1000.0,
            available_margin=1000.0,
            account_imr=0.0,
            open_positions=[],
            sl_trigger_px=sl,
        )

    def test_long_sl_must_be_below_mark(self):
        self.assertTrue(self._validate("long", 95.0)["approved"])
        for sl in (100.0, 105.0):
            with self.subTest(sl=sl):
                result = self._validate("long", sl)
                self.assertFalse(result["approved"])
                self.assertEqual(result["reject_reason"], "sl_direction_invalid")

    def test_short_sl_must_be_above_mark(self):
        self.assertTrue(self._validate("short", 105.0)["approved"])
        for sl in (100.0, 95.0):
            with self.subTest(sl=sl):
                result = self._validate("short", sl)
                self.assertFalse(result["approved"])
                self.assertEqual(result["reject_reason"], "sl_direction_invalid")


class StopLossReadbackTests(unittest.TestCase):
    @staticmethod
    def _algo(**overrides):
        row = {
            "instId": "BTC-USDT-SWAP",
            "algoId": "A-NEW",
            "slTriggerPx": "95",
            "posSide": "long",
            "side": "sell",
            "reduceOnly": "true",
            "state": "live",
            "cTime": "1000",
            "sz": "2",
            "linkedOrd": {"ordId": "O-NEW"},
        }
        row.update(overrides)
        return row

    def _verify(self, row, **kwargs):
        args = {
            "expected_sz": 2.0,
            "since_ms": 1000.0,
            "expected_ord_id": "O-NEW",
            "retries": 1,
        }
        args.update(kwargs)
        with mock.patch.object(oe.ox, "get_algo_orders", return_value=[row]):
            return oe._verify_sl_placed(
                "BTC-USDT-SWAP", "long", "live", 95.0, **args)

    def test_full_current_protective_order_passes(self):
        result = self._verify(self._algo())
        self.assertTrue(result["verified"])
        self.assertEqual(result["matched"]["algoId"], "A-NEW")

    def test_old_same_price_order_cannot_pass(self):
        result = self._verify(self._algo(cTime="999"))
        self.assertFalse(result["verified"])
        self.assertIn("created_before_request", result["found"][0]["errors"])

    def test_each_present_protective_field_must_match(self):
        mutations = {
            "posSide": {"posSide": "short"},
            "side": {"side": "buy"},
            "reduceOnly": {"reduceOnly": "false"},
            "state": {"state": "effective"},
            "size": {"sz": "1"},
            "price": {"slTriggerPx": "90"},
            "linked_order": {"linkedOrd": {"ordId": "O-OLD"}},
        }
        for label, mutation in mutations.items():
            with self.subTest(field=label):
                result = self._verify(self._algo(**mutation))
                self.assertFalse(result["verified"])

    def test_exact_new_algo_id_allows_missing_display_fields(self):
        # algoId comes from the just-accepted independent placement. Missing optional
        # display fields must not crash verification, while any present mismatch fails.
        row = {"algoId": "A-NEW", "slTriggerPx": "95"}
        result = self._verify(
            row, expected_algo_id="A-NEW", expected_ord_id=None)
        self.assertTrue(result["verified"])

        row["side"] = "buy"
        result = self._verify(
            row, expected_algo_id="A-NEW", expected_ord_id=None)
        self.assertFalse(result["verified"])

    def test_missing_identity_and_display_fields_fail_closed(self):
        result = self._verify(
            {"algoId": "A-NEW", "slTriggerPx": "95"},
            expected_ord_id=None)
        self.assertFalse(result["verified"])


class OpenFillTruthTests(unittest.TestCase):
    def test_live_open_fails_closed_when_actor_timeline_is_unavailable(self):
        balance_mock = mock.Mock()
        with (
            mock.patch.object(oe.ox, "is_dryrun", return_value=False),
            mock.patch.object(oe, "validate_receipt_context", return_value=[]),
            mock.patch.object(
                oe.actor_att, "timeline_state",
                return_value={"available": False,
                              "reason": "session_unresolvable"}),
            mock.patch.object(oe.ox, "get_balance", balance_mock),
        ):
            result = oe.open_position(
                "BTC-USDT-SWAP", "long", 1.0, 5.0, 95.0, "live",
                cycle_id="2026-08-10T08:00",
                receipt_context={"cycle_id": "2026-08-10T08:00"},
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reject_reason"], "actor_timeline_required")
        balance_mock.assert_not_called()

    def test_fills_use_last_authoritative_fill_time(self):
        aggregate = oe._avg_fill([
            {
                "fillSz": "1", "fillPx": "100", "fillPnl": "0",
                "fillTime": "1704067200000",
            },
            {
                "fillSz": "2", "fillPx": "101", "fillPnl": "0",
                "fillTime": "1704067201000",
            },
        ])
        self.assertEqual(aggregate["fill_ts"], "2024-01-01 08:00:01")
        self.assertEqual(aggregate["ts_source"], "fills.fillTime")

    def test_order_status_uses_terminal_update_time_not_creation_time(self):
        confirmed = oe._fill_from_order({
            "state": "filled", "accFillSz": "2", "avgPx": "100",
            "pnl": "0", "cTime": "1704067100000",
            "uTime": "1704067201000",
        })
        self.assertEqual(confirmed["fill_ts"], "2024-01-01 08:00:01")
        self.assertEqual(confirmed["ts_source"], "order_status.uTime")

        no_authoritative_time = oe._fill_from_order({
            "state": "filled", "accFillSz": "2", "avgPx": "100",
            "pnl": "0", "cTime": "1704067100000",
        })
        self.assertIsNone(no_authoritative_time["fill_ts"])
        self.assertIsNone(no_authoritative_time["ts_source"])

    def test_orders_history_uses_last_terminal_update_time(self):
        rows = [
            {
                "posSide": "long", "reduceOnly": "false",
                "cTime": "1704067200000", "uTime": "1704067201000",
                "accFillSz": "1", "avgPx": "100", "pnl": "0",
            },
            {
                "posSide": "long", "reduceOnly": "false",
                "cTime": "1704067200500", "uTime": "1704067202000",
                "accFillSz": "1", "avgPx": "102", "pnl": "0",
            },
        ]
        with mock.patch.object(oe.ox, "get_orders_history", return_value=rows):
            aggregate = oe._find_orders_since(
                "BTC-USDT-SWAP", "live", "long",
                1704067190000, reduce_only=False)
        self.assertEqual(aggregate["fill_ts"], "2024-01-01 08:00:02")
        self.assertEqual(aggregate["ts_source"], "orders_history.uTime")

    def test_only_exchange_fill_endpoints_are_confirmed_sources(self):
        fill = {"ok": True, "fill_px": 100.0, "fill_sz": 2.0}
        for source in ("fills", "order_status", "orders_history"):
            with self.subTest(source=source):
                self.assertEqual(
                    oe._validate_confirmed_open_fill(fill, source),
                    (True, None),
                )
        for source in ("approx_agg", "position_delta",
                       "position_confirmed_by_unwind", "unconfirmed"):
            with self.subTest(source=source):
                ok, error = oe._validate_confirmed_open_fill(fill, source)
                self.assertFalse(ok)
                self.assertTrue(error.startswith("untrusted_fill_source:"))

    def test_order_status_partial_fill_must_be_terminal(self):
        live = {
            "state": "live", "accFillSz": "2", "avgPx": "100", "pnl": "0"}
        canceled = dict(live, state="canceled")
        self.assertIsNone(oe._fill_from_order(live))
        confirmed = oe._fill_from_order(canceled)
        self.assertEqual(confirmed["fill_sz"], 2.0)
        self.assertTrue(confirmed["partial"])

    def test_partial_fill_drives_accounting_and_approved_size_is_audit_only(self):
        accounting = oe._open_fill_accounting(
            {"ok": True, "fill_px": 100.0, "fill_sz": 2.0},
            approved_sz=5.0,
            mark_px=99.0,
            ct_val=0.01,
            effective_lev=5.0,
        )
        self.assertEqual(accounting["sz"], 2.0)
        self.assertEqual(accounting["approved_sz"], 5.0)
        self.assertAlmostEqual(accounting["notional"], 2.0)
        self.assertAlmostEqual(accounting["margin"], 0.4)
        self.assertTrue(accounting["partial_fill"])
        self.assertAlmostEqual(accounting["fill_ratio"], 0.4)

    def test_fill_larger_than_approved_is_not_accepted_as_current_order(self):
        fill = {"ok": True, "fill_px": 100.0, "fill_sz": 5.1}
        ok, error = oe._validate_confirmed_open_fill(
            fill, "fills", approved_sz=5.0)
        self.assertFalse(ok)
        self.assertEqual(error, "fill_sz_exceeds_approved")

    def test_live_open_requires_cycle_scoped_experience_contract_before_io(self):
        context = _valid_receipt_context(
            "2026-08-10T08:00", "short", "HYPE-USDT-SWAP")
        balance_mock = mock.Mock()
        with (
            mock.patch.object(oe.ox, "is_dryrun", return_value=False),
            mock.patch.object(oe.ox, "get_balance", balance_mock),
        ):
            result = oe.open_position(
                "HYPE-USDT-SWAP",
                "short",
                100.0,
                10.0,
                56.5,
                "live",
                cycle_id="2026-08-10T08:00",
                receipt_context=context,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reject_reason"], "receipt_context_invalid")
        self.assertIn("evidence_contract", result["reject_detail"])
        balance_mock.assert_not_called()

    def test_explicit_exit_mode_is_enforced_before_account_io(self):
        for mode, tp, expected in (
            ("fixed_tp", None, "fixed_tp_required"),
            ("dynamic_exit", 110.0, "tp_not_allowed_for_exit_mode"),
            ("no_fixed_tp", 110.0, "tp_not_allowed_for_exit_mode"),
        ):
            with self.subTest(mode=mode):
                context = _valid_receipt_context("CYCLE-EXIT-MODE")
                context["decision_card"]["risk_reward"].update({
                    "exit_mode": mode, "target": 110.0,
                })
                balance_mock = mock.Mock()
                with (
                    mock.patch.object(oe.ox, "is_dryrun", return_value=True),
                    mock.patch.object(
                        oe, "validate_receipt_context", return_value=[]),
                    mock.patch.object(oe.ox, "get_balance", balance_mock),
                ):
                    result = oe.open_position(
                        "BTC-USDT-SWAP", "long", 2.0, 5.0, 95.0, "live",
                        cycle_id="CYCLE-EXIT-MODE",
                        receipt_context=context,
                        tp_trigger_px=tp,
                    )
                self.assertFalse(result["ok"])
                self.assertEqual(result["reject_reason"], expected)
                balance_mock.assert_not_called()

        invalid = _valid_receipt_context("CYCLE-EXIT-INVALID")
        invalid["decision_card"]["risk_reward"]["exit_mode"] = "auto_magic"
        errors = oe.validate_receipt_context(
            invalid, cycle_id="CYCLE-EXIT-INVALID", required=True)
        self.assertTrue(any("exit_mode" in item for item in errors), errors)

    def test_cycle_scoped_experience_contract_passes_pretrade_context(self):
        cycle = "2026-08-10T08:00"
        context = _valid_receipt_context(
            cycle, "short", "HYPE-USDT-SWAP")
        context["regime"] = "range"
        context["decision_card"]["historical_experience"][
            "evidence_contract"
        ] = _empty_experience_contract(
            cycle, "HYPE-USDT-SWAP", "short", "range")

        self.assertEqual(
            oe.validate_receipt_context(
                context,
                cycle_id=cycle,
                expected_symbol="HYPE-USDT-SWAP",
                expected_side="short",
                expected_regime="range",
                require_experience=True,
            ),
            [],
        )

    def test_independent_algo_requires_readback_and_partial_fill_is_returned(self):
        risk_result = {
            "approved": True,
            "approved_sz": 5.0,
            "clamped": False,
            "adjustments": [],
            "math": {"effective_lev": 5.0},
        }
        verify_mock = mock.Mock(
            return_value={"verified": True, "found": [], "matched": {}})
        tp_verify_mock = mock.Mock(
            return_value={"verified": False, "found": []})
        tp_place_mock = mock.Mock(return_value={
            "ok": True, "data": [{"algoId": "A-TP"}],
        })
        journal_mock = mock.Mock()
        repair_mock = mock.Mock()
        close_mock = mock.Mock()
        open_mock = mock.Mock(return_value={
            "ok": True, "sl_attached": False, "tp_attached": False,
            "data": [{"ordId": "O-NEW"}],
        })
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(oe.ox, "is_dryrun", return_value=False))
            stack.enter_context(mock.patch.object(
                oe, "validate_receipt_context", return_value=[]))
            stack.enter_context(mock.patch.object(
                oe.actor_att, "timeline_state",
                side_effect=_same_actor_timeline))
            stack.enter_context(mock.patch.object(
                oe, "check_multitimeframe_readiness",
                side_effect=_ready_multitimeframe))
            stack.enter_context(mock.patch.object(
                oe, "resolve_execution_evidence_anchor",
                side_effect=_ready_evidence_anchor))
            stack.enter_context(mock.patch.object(
                oe.ei, "reserve",
                return_value={"status": "reserved", "fingerprint": "FP"}))
            stack.enter_context(mock.patch.object(oe.ei, "mark_submitting"))
            stack.enter_context(mock.patch.object(oe.ei, "mark_submitted"))
            stack.enter_context(mock.patch.object(oe.ei, "mark_completed"))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_balance", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(
                oe.ac, "extract_settlement_capacity",
                return_value={
                    "ok": True, "total_equity": 1000.0,
                    "available_margin": 900.0, "settlement_ccy": "USDT",
                    "account_imr": 100.0,
                }))
            stack.enter_context(mock.patch.object(
                oe, "fetch_open_positions", return_value=[]))
            stack.enter_context(mock.patch.object(
                oe, "_verify_pretrade_ledger_positions",
                return_value={
                    "ok": True, "profile": "live",
                    "ledger_groups": 0, "exchange_groups": 0, "diffs": [],
                }))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_mark_price", return_value=100.0))
            stack.enter_context(mock.patch.object(
                oe, "fetch_instrument_specs",
                return_value={
                    "ct_val": 0.01, "lot_sz": 1.0,
                    "source": "test", "spec_source": "test",
                }))
            stack.enter_context(mock.patch.object(
                oe.rv, "validate", return_value=risk_result))
            stack.enter_context(mock.patch.object(
                oe.ox, "set_leverage", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(
                oe.ox, "place_market_open", open_mock))
            stack.enter_context(mock.patch.object(
                oe.ox, "place_algo_sl",
                return_value={
                    "ok": True, "data": [{"algoId": "A-SL"}],
                }))
            stack.enter_context(mock.patch.object(
                oe.ox, "place_algo_tp", tp_place_mock))
            stack.enter_context(mock.patch.object(
                oe, "_verify_sl_placed", verify_mock))
            stack.enter_context(mock.patch.object(
                oe, "_verify_tp_placed", tp_verify_mock))
            stack.enter_context(mock.patch.object(
                oe, "_read_fills",
                return_value={
                    "ok": True, "fill_px": 101.0, "fill_sz": 2.0,
                    "pnl": 0.0, "n": 1,
                    "fill_ts": "2024-01-01 08:00:01",
                    "ts_source": "fills.fillTime",
                }))
            stack.enter_context(mock.patch.object(
                oe, "_journal_fill", journal_mock))
            stack.enter_context(mock.patch.object(
                oe, "_enqueue_repair", repair_mock))
            stack.enter_context(mock.patch.object(
                oe, "close_position", close_mock))

            context = _valid_receipt_context("CYCLE-1")
            context["decision_card"]["risk_reward"]["target"] = 110.0
            result = oe.open_position(
                "BTC-USDT-SWAP", "long", 5.0, 5.0, 95.0, "live",
                cycle_id="CYCLE-1", receipt_context=context,
                tp_trigger_px=110.0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["sl_mode"], "algo")
        self.assertTrue(result["sl_verified"])
        self.assertEqual(result["tp_warning"], "tp_unsecured")
        self.assertFalse(result["tp_verified"])
        self.assertEqual(result["tp_algo_id"], "A-TP")
        tp_place_mock.assert_called_once_with(
            "BTC-USDT-SWAP", "long", 2.0, 110.0, "live",
            mgn_mode="cross")
        self.assertIsNone(open_mock.call_args.kwargs["tp_trigger_px"])
        close_mock.assert_not_called()
        self.assertIn(
            "tp_unsecured_after_open",
            [call.args[3] for call in repair_mock.call_args_list],
        )
        trade = result["trades"][0]
        self.assertEqual(trade["sz"], 2.0)
        self.assertEqual(trade["fill_sz"], 2.0)
        self.assertEqual(trade["approved_sz"], 5.0)
        self.assertTrue(trade["partial_fill"])
        self.assertEqual(trade["fill_source"], "fills")
        self.assertEqual(trade["fill_ts"], "2024-01-01 08:00:01")
        self.assertEqual(trade["ts_source"], "fills.fillTime")
        receipt = {
            **_valid_receipt_context("CYCLE-1"),
            **result,
        }
        self.assertEqual(trades_writer.validate(receipt), [])
        self.assertTrue(journal_mock.called)
        self.assertEqual(verify_mock.call_args.kwargs["expected_algo_id"], "A-SL")

    def test_fixed_tp_uses_independent_algo_and_exact_id_readback(self):
        risk_result = {
            "approved": True, "approved_sz": 2.0, "clamped": False,
            "adjustments": [], "math": {"effective_lev": 5.0},
        }
        open_mock = mock.Mock(return_value={
            "ok": True, "sl_attached": True, "tp_attached": False,
            "data": [{"ordId": "OPEN-TP"}],
        })
        tp_place = mock.Mock(return_value={
            "ok": True, "data": [{"algoId": "TP-EXACT"}],
        })
        tp_verify = mock.Mock(return_value={
            "verified": True, "matched": {"algoId": "TP-EXACT"},
            "found": [],
        })
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                oe.ox, "is_dryrun", return_value=False))
            stack.enter_context(mock.patch.object(
                oe, "validate_receipt_context", return_value=[]))
            stack.enter_context(mock.patch.object(
                oe.actor_att, "timeline_state",
                side_effect=_same_actor_timeline))
            stack.enter_context(mock.patch.object(
                oe, "check_multitimeframe_readiness",
                side_effect=_ready_multitimeframe))
            stack.enter_context(mock.patch.object(
                oe, "resolve_execution_evidence_anchor",
                side_effect=_ready_evidence_anchor))
            stack.enter_context(mock.patch.object(
                oe.ei, "reserve", return_value={
                    "status": "reserved", "fingerprint": "FP-TP"}))
            for name in ("mark_submitting", "mark_submitted", "mark_completed"):
                stack.enter_context(mock.patch.object(oe.ei, name))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_balance", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(
                oe.ac, "extract_settlement_capacity", return_value={
                    "ok": True, "total_equity": 1000.0,
                    "available_margin": 900.0, "settlement_ccy": "USDT",
                    "account_imr": 100.0,
                }))
            stack.enter_context(mock.patch.object(
                oe, "fetch_open_positions", return_value=[]))
            stack.enter_context(mock.patch.object(
                oe, "_verify_pretrade_ledger_positions", return_value={
                    "ok": True, "profile": "live", "ledger_groups": 0,
                    "exchange_groups": 0, "diffs": [],
                }))
            stack.enter_context(mock.patch.object(
                oe.ox, "get_mark_price", return_value=100.0))
            stack.enter_context(mock.patch.object(
                oe, "fetch_instrument_specs", return_value={
                    "ct_val": 0.01, "lot_sz": 1.0, "spec_source": "test",
                }))
            stack.enter_context(mock.patch.object(
                oe.rv, "validate", return_value=risk_result))
            stack.enter_context(mock.patch.object(
                oe.ox, "set_leverage", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(
                oe.ox, "place_market_open", open_mock))
            stack.enter_context(mock.patch.object(
                oe, "_verify_sl_placed", return_value={"verified": True}))
            stack.enter_context(mock.patch.object(
                oe.ox, "place_algo_tp", tp_place))
            stack.enter_context(mock.patch.object(
                oe, "_verify_tp_placed", tp_verify))
            stack.enter_context(mock.patch.object(
                oe, "_read_fills", return_value={
                    "ok": True, "fill_px": 100.5, "fill_sz": 2.0,
                    "pnl": 0.0, "n": 1,
                    "fill_ts": "2026-08-13 22:00:01",
                    "ts_source": "fills.fillTime",
                }))
            stack.enter_context(mock.patch.object(oe, "_journal_fill"))
            context = _valid_receipt_context("CYCLE-FIXED-TP")
            context["decision_card"]["risk_reward"].update({
                "exit_mode": "fixed_tp", "target": 110.0,
            })
            result = oe.open_position(
                "BTC-USDT-SWAP", "long", 2.0, 5.0, 95.0, "live",
                cycle_id="CYCLE-FIXED-TP", receipt_context=context,
                tp_trigger_px=110.0,
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["tp_verified"])
        self.assertEqual(result["tp_mode"], "independent_algo")
        self.assertEqual(result["tp_algo_id"], "TP-EXACT")
        self.assertIsNone(open_mock.call_args.kwargs["tp_trigger_px"])
        tp_verify.assert_called_once()
        self.assertEqual(
            tp_verify.call_args.kwargs["expected_algo_id"], "TP-EXACT")


class ReduceReceiptContractTests(unittest.TestCase):
    @staticmethod
    def _run_reduce(*, placed=None, fill=None, reduce_sz=2.0):
        cycle = "CYCLE-REDUCE"
        context = _valid_receipt_context(cycle)
        position = {
            "symbol": "BTC-USDT-SWAP", "side": "long", "sz": 5.0,
        }
        post = {
            "symbol": "BTC-USDT-SWAP", "side": "long", "sz": 3.0,
        }
        order_mock = mock.Mock(return_value=placed or {
            "ok": True, "data": [{"ordId": "REDUCE-1"}],
        })
        close_fallback = mock.Mock()
        adjust_mock = mock.Mock(return_value={
            "ok": True, "action_taken": "ADJUST_PROTECTION",
            "path": "amend", "p0": False,
        })
        journal_mock = mock.Mock()
        uncertain_mock = mock.Mock()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                oe.ox, "is_dryrun", return_value=False))
            stack.enter_context(mock.patch.object(
                oe.ei, "reserve", return_value={
                    "status": "reserved", "fingerprint": "REDUCE-FP",
                }))
            stack.enter_context(mock.patch.object(oe.ei, "mark_submitting"))
            stack.enter_context(mock.patch.object(oe.ei, "mark_submitted"))
            stack.enter_context(mock.patch.object(oe.ei, "mark_completed"))
            stack.enter_context(mock.patch.object(
                oe.ei, "mark_failed_clean"))
            stack.enter_context(mock.patch.object(
                oe.ei, "mark_uncertain", uncertain_mock))
            stack.enter_context(mock.patch.object(
                oe, "fetch_open_positions", side_effect=[[position], [post]]))
            stack.enter_context(mock.patch.object(
                oe, "fetch_instrument_specs", return_value={
                    "ct_val": 0.01, "lot_sz": 1.0, "min_sz": 1.0,
                }))
            stack.enter_context(mock.patch.object(
                oe.ox, "place_reduce_only_market", order_mock))
            stack.enter_context(mock.patch.object(
                oe.ox, "close_position_cli", close_fallback))
            stack.enter_context(mock.patch.object(
                oe, "_read_fills", return_value=fill or {
                    "ok": True, "fill_px": 101.5, "fill_sz": 2.0,
                    "pnl": 3.25, "n": 1,
                    "fill_ts": "2026-08-13 21:15:02",
                    "ts_source": "fills.fillTime",
                }))
            stack.enter_context(mock.patch.object(
                oe, "_confirm_order_filled", return_value=None))
            stack.enter_context(mock.patch.object(
                oe, "adjust_protection", adjust_mock))
            stack.enter_context(mock.patch.object(
                oe, "_journal_fill", journal_mock))
            stack.enter_context(mock.patch.object(oe, "_enqueue_repair"))
            stack.enter_context(mock.patch.object(oe.time, "sleep"))
            result = oe.reduce_position(
                "BTC-USDT-SWAP", "live", reduce_sz,
                pos_side="long", reasoning="agent partial exit",
                cycle_id=cycle, receipt_context=context,
            )
        return (
            result, order_mock, close_fallback, adjust_mock,
            journal_mock, uncertain_mock,
        )

    def test_confirmed_partial_reduce_is_writer_valid_and_resizes_stop(self):
        result, order, close_fallback, adjust, journal, uncertain = (
            self._run_reduce())
        self.assertTrue(result["ok"])
        self.assertEqual(result["action_taken"], "REDUCE")
        self.assertEqual(result["trades"][0]["action"], "reduce")
        self.assertEqual(result["trades"][0]["sz"], 2.0)
        self.assertEqual(result["post_position_sz"], 3.0)
        self.assertEqual(trades_writer.validate(result), [])
        order.assert_called_once()
        close_fallback.assert_not_called()
        uncertain.assert_not_called()
        adjust.assert_called_once()
        self.assertEqual(adjust.call_args.kwargs["pos_side"], "long")
        self.assertTrue(adjust.call_args.kwargs["resize_to_full_position"])
        journal.assert_called_once()

    def test_partial_reduce_equal_to_position_refuses_without_order(self):
        result, order, close_fallback, adjust, journal, _ = (
            self._run_reduce(reduce_sz=5.0))
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["reject_reason"],
            "partial_reduce_requires_less_than_position")
        order.assert_not_called()
        close_fallback.assert_not_called()
        adjust.assert_not_called()
        journal.assert_not_called()

    def test_ambiguous_reduce_never_falls_back_to_full_close(self):
        result, order, close_fallback, adjust, journal, uncertain = (
            self._run_reduce(placed={
                "ok": False, "sCode": None, "error": "transport timeout",
                "data": [],
            }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reject_reason"], "reduce_place_ambiguous")
        self.assertTrue(result["p0"])
        order.assert_called_once()
        close_fallback.assert_not_called()
        adjust.assert_not_called()
        journal.assert_not_called()
        uncertain.assert_called_once()


class CloseReceiptContractTests(unittest.TestCase):
    @staticmethod
    def _run_close(fill: dict, *, cycle: str = "CYCLE-CLOSE"):
        context = _valid_receipt_context(cycle)
        position = {
            "symbol": "BTC-USDT-SWAP",
            "side": "long",
            "sz": 5.0,
        }
        journal_mock = mock.Mock()
        repair_mock = mock.Mock()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                oe.ox, "is_dryrun", return_value=False))
            stack.enter_context(mock.patch.object(
                oe, "fetch_open_positions", side_effect=[[position], []]))
            stack.enter_context(mock.patch.object(
                oe.ox, "place_reduce_only_market",
                return_value={"ok": True, "data": [{"ordId": "CLOSE-1"}]}))
            stack.enter_context(mock.patch.object(
                oe, "_read_fills", return_value=fill))
            stack.enter_context(mock.patch.object(
                oe, "_confirm_order_filled", return_value=None))
            stack.enter_context(mock.patch.object(
                oe, "fetch_instrument_specs", return_value={"ct_val": 0.01}))
            stack.enter_context(mock.patch.object(
                oe, "_journal_fill", journal_mock))
            stack.enter_context(mock.patch.object(
                oe, "_enqueue_repair", repair_mock))
            stack.enter_context(mock.patch.object(oe.time, "sleep"))
            result = oe.close_position(
                "BTC-USDT-SWAP",
                "live",
                pos_side="long",
                reasoning="isolated close",
                cycle_id=cycle,
                receipt_context=context,
            )
        return result, context, journal_mock, repair_mock

    def test_invalid_context_blocks_before_any_okx_io(self):
        context = _valid_receipt_context("CYCLE-BAD-CONTEXT")
        context.pop("status")
        positions_mock = mock.Mock()
        order_mock = mock.Mock()
        with (
            mock.patch.object(oe.ox, "is_dryrun", return_value=False),
            mock.patch.object(oe, "fetch_open_positions", positions_mock),
            mock.patch.object(
                oe.ox, "place_reduce_only_market", order_mock),
        ):
            result = oe.close_position(
                "BTC-USDT-SWAP",
                "live",
                pos_side="long",
                cycle_id="CYCLE-BAD-CONTEXT",
                receipt_context=context,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reject_reason"], "receipt_context_invalid")
        self.assertIn("status", result["reject_detail"])
        positions_mock.assert_not_called()
        order_mock.assert_not_called()

    def test_confirmed_close_uses_authoritative_partial_fill_and_is_writer_valid(self):
        result, context, journal_mock, repair_mock = self._run_close({
            "ok": True,
            "fill_px": 101.5,
            "fill_sz": 2.0,
            "pnl": 3.25,
            "n": 1,
            "fill_ts": "2026-07-29 10:20:30",
            "ts_source": "fills.fillTime",
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["decision_protocol"], context["decision_protocol"])
        self.assertEqual(result["decision_card"], context["decision_card"])
        self.assertEqual(result["cycle_id"], "CYCLE-CLOSE")
        self.assertEqual(result["side"], "long")
        trade = result["trades"][0]
        self.assertEqual(trade["sz"], 2.0)
        self.assertEqual(trade["fill_sz"], 2.0)
        self.assertEqual(trade["requested_sz"], 5.0)
        self.assertEqual(trade["pre_position_sz"], 5.0)
        self.assertTrue(trade["partial_fill"])
        self.assertEqual(trade["fill_source"], "fills")
        self.assertEqual(trade["fill_ts"], "2026-07-29 10:20:30")
        self.assertEqual(trade["ts_source"], "fills.fillTime")
        self.assertEqual(trades_writer.validate(result), [])
        journal_mock.assert_called_once()
        repair_mock.assert_not_called()

    def test_missing_authoritative_fill_time_downgrades_without_fake_fill(self):
        result, _, journal_mock, repair_mock = self._run_close({
            "ok": True,
            "fill_px": 101.5,
            "fill_sz": 2.0,
            "pnl": 3.25,
            "n": 1,
            "fill_ts": None,
            "ts_source": None,
        })

        self.assertTrue(result["ok"])
        self.assertFalse(result["fills_ok"])
        trade = result["trades"][0]
        self.assertEqual(trade["fill_source"], "unconfirmed")
        self.assertEqual(trade["sz"], 5.0)
        self.assertIsNone(trade["fill_sz"])
        self.assertIsNone(trade["fill_px"])
        self.assertIsNone(trade["px"])
        self.assertIsNone(trade["pnl"])
        self.assertIsNone(trade["fill_ts"])
        self.assertIsNone(trade["ts_source"])
        self.assertEqual(trade["fill_contract_error"], "invalid_fill_ts")
        self.assertEqual(trades_writer.validate(result), [])
        journal_mock.assert_called_once()
        repair_reasons = [
            call.args[3] for call in repair_mock.call_args_list
        ]
        self.assertIn(
            "close_fill_contract_invalid:invalid_fill_ts", repair_reasons)

    def test_unconfirmed_close_never_fabricates_fill_size_or_financials(self):
        result, _, _, repair_mock = self._run_close({
            "ok": False,
            "fill_px": None,
            "fill_sz": None,
            "pnl": None,
            "n": 0,
            "fill_ts": None,
            "ts_source": None,
        })

        self.assertTrue(result["ok"])
        trade = result["trades"][0]
        self.assertEqual(trade["fill_source"], "unconfirmed")
        self.assertEqual(trade["requested_sz"], 5.0)
        self.assertEqual(trade["pre_position_sz"], 5.0)
        self.assertIsNone(trade["fill_sz"])
        self.assertIsNone(trade["fill_px"])
        self.assertIsNone(trade["pnl"])
        self.assertIsNone(trade["fill_ts"])
        self.assertEqual(trades_writer.validate(result), [])
        repair_reasons = [
            call.args[3] for call in repair_mock.call_args_list
        ]
        self.assertIn("close_pnl_unconfirmed", repair_reasons)


if __name__ == "__main__":
    unittest.main()
