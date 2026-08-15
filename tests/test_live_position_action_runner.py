# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for module_path in (ROOT, ROOT / "scripts", ROOT / "collectors", ROOT / "core"):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

import live_position_action_runner as runner  # noqa: E402


CYCLE = "2026-08-15T15:15"


def _card() -> dict:
    return {
        "direction_evidence": ["current position evidence"],
        "opposing_evidence": ["counter evidence"],
        "execution_conditions": "Agent-owned exit condition",
        "invalidation_point": "Agent-owned invalidation",
        "risk_reward": {"exit_mode": "dynamic_exit", "entry": 1,
                        "stop": 0.9, "target": 1.2, "rr": 2},
        "portfolio_impact": "reviewed all positions",
        "historical_experience": {
            "matched_wins": [], "matched_losses": [],
            "missed_opportunities": [], "usage": "none", "reason": "none",
        },
        "agent_judgement": "CLOSE BTC; HOLD ETH",
        "reference_overrides": [],
    }


def _open_card(*, judgement: str = "OPEN SOL", stop: float = 98.0) -> dict:
    card = _card()
    card["agent_judgement"] = judgement
    card["risk_reward"] = {
        "exit_mode": "fixed_tp",
        "entry": 100.0,
        "stop": stop,
        "target": 104.0,
        "rr": 2.0,
    }
    return card


def _facts(*, status: str = "ok") -> dict:
    return {
        "cycle_id": CYCLE,
        "profile": "live",
        "status": status,
        "errors": [] if status == "ok" else ["balance unavailable"],
        "balance": {"totalEq": 1000.0, "availEq": 800.0,
                    "account_imr": 100.0},
        "positions": [
            {"instId": "BTC-USDT-SWAP", "posSide": "long", "contracts": 3,
             "posId": "P-BTC", "cTime": 1001},
            {"instId": "ETH-USDT-SWAP", "posSide": "short", "contracts": 5,
             "posId": "P-ETH", "cTime": 1002},
        ],
        "action_policy": {
            "position_truth_verified": True,
            "allowed_executor_actions": [
                "open", "add", "close", "reduce", "adjust_protection"
            ],
        },
        "facts_hash": "f" * 64,
    }


def _plan(actions: list[dict]) -> dict:
    return {
        "cycle_id": CYCLE,
        "receipt_context": {
            "cycle_id": CYCLE,
            "mode": "live",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "decision_card": _card(),
            "equity": 1000.0,
            "regime": "range",
        },
        "actions": actions,
    }


def _trade(symbol: str, side: str, action: str = "close") -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "side": side,
        "sz": 1.0,
        "fill_sz": 1.0,
        "fill_px": 100.0,
        "fill_source": "fills",
        "fill_ts": "2026-08-15 15:20:00",
        "ts_source": "fills.fillTime",
    }


def _create_trade_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE trade_cycles(
                cycle_id TEXT PRIMARY KEY, ts TEXT, mode TEXT, decision TEXT,
                n_orders INTEGER, equity REAL, note TEXT, raw TEXT
            );
            CREATE TABLE trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, ts TEXT,
                symbol TEXT, action TEXT, side TEXT, sz REAL, fill_px REAL,
                lev REAL, margin REAL, notional REAL, score_total REAL,
                reasoning TEXT, deviation TEXT, degradation TEXT, pnl REAL,
                raw TEXT
            );
            """
        )
    finally:
        con.close()


def _create_runtime_authority(
    root: Path,
    *,
    cycle_id: str = CYCLE,
    stage_status: str = "running",
    with_lease: bool = True,
    analysis_status: str | None = "ok",
    analysis_ts: str | None = None,
) -> tuple[Path, Path]:
    status_dir = root / "stage-status"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / f"live-{cycle_id.replace(':', '-')}.json").write_text(
        json.dumps({
            "stage": "live",
            "cycle_id": cycle_id,
            "status": stage_status,
            "runner_pid": 12345,
        }),
        encoding="utf-8",
    )
    db_root = root / "db"
    db_root.mkdir(parents=True, exist_ok=True)
    cycle_start = datetime.strptime(
        cycle_id, "%Y-%m-%dT%H:%M").replace(tzinfo=runner.CST)
    con = sqlite3.connect(db_root / "ledger.db")
    try:
        con.execute(
            "CREATE TABLE stage_profile_leases("
            "profile TEXT PRIMARY KEY,cycle_id TEXT,acquired_at TEXT,"
            "expires_at TEXT)"
        )
        if with_lease:
            con.execute(
                "INSERT INTO stage_profile_leases VALUES(?,?,?,?)",
                ("live", cycle_id,
                 (cycle_start + timedelta(seconds=1)).strftime(
                     "%Y-%m-%d %H:%M:%S"),
                 (cycle_start + timedelta(hours=1)).strftime(
                     "%Y-%m-%d %H:%M:%S")),
            )
        con.commit()
    finally:
        con.close()
    con = sqlite3.connect(db_root / "analysis.db")
    try:
        con.execute(
            "CREATE TABLE analysis_runs("
            "cycle_id TEXT PRIMARY KEY,status TEXT,ts TEXT)"
        )
        if analysis_status is not None:
            con.execute(
                "INSERT INTO analysis_runs VALUES(?,?,?)",
                (cycle_id, analysis_status, analysis_ts or (
                    cycle_start + timedelta(minutes=5)).strftime(
                        "%Y-%m-%d %H:%M:%S")),
            )
        con.commit()
    finally:
        con.close()
    return status_dir, db_root


class LivePositionActionRunnerTests(unittest.TestCase):
    def _patch_validation(self):
        return (
            mock.patch.object(runner, "validate_facts", return_value=[]),
            mock.patch.object(
                runner.oe, "validate_receipt_context", return_value=[]
            ),
            mock.patch.object(runner.tw, "validate", return_value=[]),
            mock.patch.object(
                runner.tw, "validate_strict_live_receipt", return_value=[]
            ),
        )

    def test_two_closes_execute_then_commit_once_in_same_call(self) -> None:
        plan = _plan([
            {"action": "CLOSE", "symbol": "BTC-USDT-SWAP",
             "pos_side": "long", "reasoning": "thesis invalid"},
            {"action": "CLOSE", "symbol": "ETH-USDT-SWAP",
             "pos_side": "short", "reasoning": "giveback too large"},
        ])
        order: list[str] = []

        def close(symbol, profile, **kwargs):
            order.append(symbol)
            self.assertEqual(profile, "live")
            self.assertEqual(kwargs["cycle_id"], CYCLE)
            self.assertTrue(kwargs["expected_pre_position_exists"])
            self.assertEqual(kwargs["expected_pre_position_sz"],
                             3 if symbol.startswith("BTC") else 5)
            self.assertEqual(kwargs["expected_pre_position_pos_id"],
                             "P-BTC" if symbol.startswith("BTC") else "P-ETH")
            self.assertEqual(kwargs["expected_pre_position_c_time"],
                             1001 if symbol.startswith("BTC") else 1002)
            return {
                **kwargs["receipt_context"],
                "profile": "live", "ok": True, "action_taken": "CLOSE",
                "symbol": symbol, "side": kwargs["pos_side"], "p0": False,
                "trades": [_trade(symbol, kwargs["pos_side"])],
            }

        def commit(receipt, profile, **kwargs):
            order.append("commit")
            self.assertEqual(profile, "live")
            self.assertTrue(kwargs["require_live_facts"])
            self.assertEqual(receipt["n_orders"], 2)
            return {"ok": True, "written": 2}

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.oe, "close_position", side_effect=close), \
                mock.patch.object(runner.tw, "commit_receipt", side_effect=commit):
            receipt_file = Path(tmp) / "receipt.json"
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=receipt_file, nudge=False)
            persisted = json.loads(receipt_file.read_text(encoding="utf-8"))

        self.assertTrue(result["ok"])
        self.assertEqual(order, ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "commit"])
        receipt = result["receipt"]
        self.assertEqual(receipt["batch_status"], "completed")
        self.assertEqual(receipt["action_taken"], "CLOSE")
        self.assertEqual(len(receipt["position_action_results"]), 2)
        self.assertEqual(receipt["live_facts"], _facts())
        self.assertEqual(persisted, receipt)

    def test_open_uses_canonical_card_and_deterministic_contract_size(self) -> None:
        card = _open_card(judgement="OPEN SOL from canonical analysis")
        plan = _plan([{
            "action": "OPEN",
            "symbol": "SOL-USDT-SWAP",
            "side": "long",
            "target_stop_risk_pct_equity": 0.01,
            "lev": 5,
        }])

        def open_position(symbol, side, intended_sz, lev, sl_trigger_px,
                          **kwargs):
            self.assertEqual((symbol, side), ("SOL-USDT-SWAP", "long"))
            self.assertEqual(intended_sz, 4.5)
            self.assertEqual(lev, 5.0)
            self.assertEqual(sl_trigger_px, 98.0)
            self.assertEqual(kwargs["tp_trigger_px"], 104.0)
            self.assertEqual(kwargs["receipt_context"]["decision_card"], card)
            return {
                **kwargs["receipt_context"],
                "profile": "live",
                "ok": True,
                "action_taken": "OPEN_LONG",
                "symbol": symbol,
                "side": side,
                "is_add": False,
                "p0": False,
                "trades": [_trade(symbol, side, action="open")],
            }

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner, "_load_analysis_signal", return_value={
                    "action": "open_long", "side": "long",
                    "reasoning": "canonical analysis reasoning",
                    "decision_card": card,
                }), \
                mock.patch.object(runner.oe, "fetch_instrument_specs", return_value={
                    "ct_val": 1.0, "lot_sz": 0.1, "min_sz": 0.1,
                }), \
                mock.patch.object(runner.oe.ox, "get_mark_price", return_value=100.0), \
                mock.patch.object(
                    runner.oe, "open_position", side_effect=open_position
                ) as opened, \
                mock.patch.object(
                    runner.tw, "commit_receipt", return_value={"ok": True}
                ) as commit:
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["receipt"]["action_taken"], "OPEN")
        trade = result["receipt"]["trades"][0]
        self.assertEqual(trade["decision_card"], card)
        self.assertEqual(trade["decision_protocol"], "decision_card_v1")
        self.assertNotIn(
            "_decision_card",
            result["receipt"]["requested_position_actions"][0],
        )
        opened.assert_called_once()
        commit.assert_called_once()

    def test_add_uses_same_open_entrypoint_but_aggregates_as_add(self) -> None:
        card = _open_card(judgement="ADD BTC from canonical analysis")
        plan = _plan([{
            "action": "ADD",
            "symbol": "BTC-USDT-SWAP",
            "side": "long",
            "target_stop_risk_pct_equity": 0.005,
            "lev": 5,
        }])
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner, "_load_analysis_signal", return_value={
                    "action": "open_long", "side": "long",
                    "reasoning": "canonical add reasoning",
                    "decision_card": card,
                }), \
                mock.patch.object(runner.oe, "fetch_instrument_specs", return_value={
                    "ct_val": 1.0, "lot_sz": 0.1, "min_sz": 0.1,
                }), \
                mock.patch.object(runner.oe.ox, "get_mark_price", return_value=100.0), \
                mock.patch.object(runner.oe, "open_position", return_value={
                    "ok": True, "action_taken": "OPEN_LONG", "p0": False,
                    "is_add": True,
                    "trades": [_trade("BTC-USDT-SWAP", "long", action="open")],
                }) as opened, \
                mock.patch.object(
                    runner.tw, "commit_receipt", return_value={"ok": True}
                ):
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["receipt"]["action_taken"], "ADD")
        self.assertEqual(
            result["receipt"]["trades"][0]["decision_card"], card
        )
        opened.assert_called_once()

    def test_open_canonical_signal_mismatch_rejects_before_market_read(self) -> None:
        plan = _plan([{
            "action": "OPEN", "symbol": "SOL-USDT-SWAP", "side": "long",
            "target_stop_risk_pct_equity": 0.01, "lev": 5,
        }])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner, "validate_facts", return_value=[]), \
                mock.patch.object(
                    runner.oe, "validate_receipt_context", return_value=[]
                ), \
                mock.patch.object(runner, "_load_analysis_signal", return_value={
                    "action": "open_short", "side": "short", "reasoning": "x",
                    "decision_card": _open_card(),
                }), \
                mock.patch.object(runner.oe, "fetch_instrument_specs") as specs, \
                mock.patch.object(runner.oe.ox, "get_mark_price") as mark:
            with self.assertRaisesRegex(runner.PlanError, "canonical.*不一致"):
                runner.execute_position_plan(
                    plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                    receipt_file=Path(tmp) / "receipt.json", nudge=False)
        specs.assert_not_called()
        mark.assert_not_called()

    def test_runner_state_uses_facts_artifact_hash_and_raw_plan_sha(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "close",
        }])
        seen_states: list[str] = []
        real_write_state = runner._write_runner_state

        def capture_state(path, **kwargs):
            seen_states.append(kwargs["state"])
            return real_write_state(path, **kwargs)

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner, "_write_runner_state",
                                  side_effect=capture_state), \
                mock.patch.object(runner.oe, "close_position", return_value={
                    "ok": True, "action_taken": "CLOSE", "p0": False,
                    "trades": [_trade("BTC-USDT-SWAP", "long")],
                }), \
                mock.patch.object(
                    runner.tw, "commit_receipt", return_value={"ok": True}
                ):
            state_file = Path(tmp) / "state.json"
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False,
                plan_sha256="a" * 64, state_file=state_file)
            state = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertTrue(result["committed"])
        self.assertEqual(seen_states, ["started", "executing", "committed"])
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["cycle_id"], CYCLE)
        self.assertEqual(state["facts_hash"], "f" * 64)
        self.assertEqual(state["plan_sha256"], "a" * 64)

    def test_close_then_open_interim_commits_close_before_open(self) -> None:
        card = _open_card(judgement="OPEN SOL after persisted close")
        plan = _plan([
            {
                "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
                "pos_side": "long", "reasoning": "free risk first",
            },
            {
                "action": "OPEN", "symbol": "SOL-USDT-SWAP", "side": "long",
                "target_stop_risk_pct_equity": 0.01, "lev": 5,
            },
        ])
        order: list[str] = []

        def close_position(symbol, profile, **kwargs):
            order.append("close")
            return {
                "ok": True, "action_taken": "CLOSE", "p0": False,
                "trades": [_trade(symbol, "long", action="close")],
            }

        def open_position(symbol, side, intended_sz, lev, sl_trigger_px,
                          **kwargs):
            order.append("open")
            return {
                "ok": True, "action_taken": "OPEN_LONG", "p0": False,
                "is_add": False,
                "trades": [_trade(symbol, side, action="open")],
            }

        def commit(receipt, profile, **kwargs):
            if receipt.get("runner_in_progress"):
                order.append("interim")
                self.assertEqual(receipt["batch_status"], "partial")
                self.assertEqual(receipt["n_orders"], 1)
                self.assertFalse(kwargs["nudge"])
            else:
                order.append("final")
                self.assertEqual(receipt["n_orders"], 2)
            return {"ok": True}

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner, "_load_analysis_signal", return_value={
                    "action": "open_long", "side": "long", "reasoning": "open",
                    "decision_card": card,
                }), \
                mock.patch.object(runner.oe, "fetch_instrument_specs", return_value={
                    "ct_val": 1.0, "lot_sz": 0.1, "min_sz": 0.1,
                }), \
                mock.patch.object(runner.oe.ox, "get_mark_price", return_value=100.0), \
                mock.patch.object(runner.oe, "close_position",
                                  side_effect=close_position), \
                mock.patch.object(runner.oe, "open_position",
                                  side_effect=open_position), \
                mock.patch.object(runner.tw, "commit_receipt",
                                  side_effect=commit) as committed:
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)

        self.assertTrue(result["ok"])
        self.assertEqual(order, ["close", "interim", "open", "final"])
        self.assertEqual(result["receipt"]["action_taken"], "OPEN")
        self.assertEqual(committed.call_count, 2)

    def test_two_opens_interim_commit_first_fill_before_second_open(self) -> None:
        cards = {
            "SOL-USDT-SWAP": _open_card(judgement="OPEN SOL"),
            "AVAX-USDT-SWAP": _open_card(judgement="OPEN AVAX"),
        }
        plan = _plan([
            {
                "action": "OPEN", "symbol": symbol, "side": "long",
                "target_stop_risk_pct_equity": 0.005, "lev": 5,
            }
            for symbol in cards
        ])
        order: list[str] = []

        def load_signal(db_root, cycle_id, symbol):
            return {
                "action": "open_long", "side": "long",
                "reasoning": f"canonical {symbol}",
                "decision_card": cards[symbol],
            }

        def open_position(symbol, side, intended_sz, lev, sl_trigger_px,
                          **kwargs):
            order.append(symbol)
            self.assertEqual(
                kwargs["receipt_context"]["decision_card"], cards[symbol]
            )
            return {
                "ok": True, "action_taken": "OPEN_LONG", "p0": False,
                "is_add": False,
                "trades": [_trade(symbol, side, action="open")],
            }

        def commit(receipt, profile, **kwargs):
            order.append("interim" if receipt.get("runner_in_progress") else "final")
            return {"ok": True}

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner, "_load_analysis_signal",
                                  side_effect=load_signal), \
                mock.patch.object(runner.oe, "fetch_instrument_specs", return_value={
                    "ct_val": 1.0, "lot_sz": 0.1, "min_sz": 0.1,
                }), \
                mock.patch.object(runner.oe.ox, "get_mark_price", return_value=100.0), \
                mock.patch.object(runner.oe, "open_position",
                                  side_effect=open_position), \
                mock.patch.object(runner.tw, "commit_receipt",
                                  side_effect=commit) as committed:
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)

        self.assertTrue(result["ok"])
        self.assertEqual(order, [
            "SOL-USDT-SWAP", "interim", "AVAX-USDT-SWAP", "final",
        ])
        self.assertEqual(committed.call_count, 2)
        self.assertEqual(
            [row["decision_card"] for row in result["receipt"]["trades"]],
            [cards["SOL-USDT-SWAP"], cards["AVAX-USDT-SWAP"]],
        )

    def test_partial_batch_commits_confirmed_first_trade_and_stops(self) -> None:
        plan = _plan([
            {"action": "CLOSE", "symbol": "BTC-USDT-SWAP",
             "pos_side": "long", "reasoning": "close first"},
            {"action": "CLOSE", "symbol": "ETH-USDT-SWAP",
             "pos_side": "short", "reasoning": "close second"},
        ])
        calls = 0

        def close(symbol, profile, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    **kwargs["receipt_context"], "profile": "live", "ok": True,
                    "action_taken": "CLOSE", "symbol": symbol, "p0": False,
                    "trades": [_trade(symbol, "long")],
                }
            return {
                **kwargs["receipt_context"], "profile": "live", "ok": False,
                "action_taken": "REJECT", "symbol": symbol, "trades": [],
                "reject_reason": "cycle_side_effect_deadline_exceeded",
            }

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.oe, "close_position", side_effect=close), \
                mock.patch.object(
                    runner.tw, "commit_receipt", return_value={"ok": True}
                ) as commit:
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)

        self.assertFalse(result["ok"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["batch_status"], "partial")
        self.assertEqual(result["receipt"]["n_orders"], 1)
        self.assertEqual(len(result["receipt"]["position_action_failures"]), 1)
        commit.assert_called_once()

    def test_reduce_and_adjust_forward_exact_facts_position_fingerprint(self) -> None:
        plan = _plan([
            {
                "action": "REDUCE", "symbol": "BTC-USDT-SWAP",
                "pos_side": "long", "reduce_sz": 1,
                "reasoning": "reduce exposure",
            },
            {
                "action": "ADJUST_PROTECTION", "symbol": "ETH-USDT-SWAP",
                "pos_side": "short", "new_sl_trigger_px": 105,
                "new_tp_trigger_px": None,
                "resize_to_full_position": False,
                "consolidate_extra_sl": False,
                "reasoning": "tighten protection",
            },
        ])

        def reduce_position(symbol, profile, reduce_sz, **kwargs):
            self.assertEqual((symbol, profile, reduce_sz),
                             ("BTC-USDT-SWAP", "live", 1.0))
            self.assertTrue(kwargs["expected_pre_position_exists"])
            self.assertEqual(kwargs["expected_pre_position_sz"], 3.0)
            self.assertEqual(kwargs["expected_pre_position_pos_id"], "P-BTC")
            self.assertEqual(kwargs["expected_pre_position_c_time"], 1001)
            return {
                "ok": True, "action_taken": "REDUCE", "p0": False,
                "trades": [_trade(symbol, "long", action="reduce")],
            }

        def adjust_protection(symbol, profile, **kwargs):
            self.assertEqual((symbol, profile), ("ETH-USDT-SWAP", "live"))
            self.assertTrue(kwargs["expected_pre_position_exists"])
            self.assertEqual(kwargs["expected_pre_position_sz"], 5.0)
            self.assertEqual(kwargs["expected_pre_position_pos_id"], "P-ETH")
            self.assertEqual(kwargs["expected_pre_position_c_time"], 1002)
            return {
                "ok": True, "action_taken": "ADJUST_PROTECTION", "p0": False,
                "trades": [], "path": "amend",
                "protection_change": {"requested_sl": 105.0},
                "protection_state": {"ok": True},
                "applied": {"sl": 105.0},
            }

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.oe, "reduce_position",
                                  side_effect=reduce_position) as reduced, \
                mock.patch.object(runner.oe, "adjust_protection",
                                  side_effect=adjust_protection) as adjusted, \
                mock.patch.object(runner.tw, "commit_receipt",
                                  return_value={"ok": True}):
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)

        self.assertTrue(result["ok"], result)
        reduced.assert_called_once()
        adjusted.assert_called_once()

    def test_existing_failed_marker_refuses_rerun_before_executor(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "close",
        }])
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(json.dumps({
                "schema_version": 1,
                "cycle_id": CYCLE,
                "state": "failed",
                "facts_hash": "f" * 64,
                "plan_sha256": "a" * 64,
            }), encoding="utf-8")
            with mock.patch.object(runner.oe, "close_position") as close:
                with self.assertRaisesRegex(runner.PlanError, "failed.*拒绝重复"):
                    runner.execute_position_plan(
                        plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                        receipt_file=Path(tmp) / "receipt.json", nudge=False,
                        state_file=state_file, plan_sha256="a" * 64)
            close.assert_not_called()

    def test_production_authority_accepts_running_owned_unexpired_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_dir, db_root = _create_runtime_authority(Path(tmp))
            authority = runner.validate_live_runtime_authority(
                CYCLE,
                db_root=db_root,
                status_dir=status_dir,
                now=datetime(2026, 8, 15, 15, 20,
                             tzinfo=runner.CST),
            )
        self.assertEqual("running", authority["stage_status"])
        self.assertEqual(CYCLE, authority["cycle_id"])

    def test_production_authority_rejects_bad_analysis_before_marker_executor(
            self) -> None:
        protected_cycle = "2026-08-15T21:45"
        cases = (
            ("missing", None, None),
            ("skipped", "skipped", "2026-08-15 21:50:00"),
            ("late", "ok", "2026-08-15 21:54:30"),
        )
        for label, analysis_status, analysis_ts in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                status_dir, db_root = _create_runtime_authority(
                    root,
                    cycle_id=protected_cycle,
                    analysis_status=analysis_status,
                    analysis_ts=analysis_ts,
                )
                plan = _plan([{
                    "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
                    "pos_side": "long", "reasoning": "authority test",
                }])
                plan["cycle_id"] = protected_cycle
                plan["receipt_context"]["cycle_id"] = protected_cycle
                facts = _facts()
                facts["cycle_id"] = protected_cycle
                state_file = root / "state.json"

                def guard(cycle):
                    return runner.validate_live_runtime_authority(
                        cycle,
                        db_root=db_root,
                        status_dir=status_dir,
                        now=datetime(2026, 8, 15, 21, 55,
                                     tzinfo=runner.CST),
                    )

                with mock.patch.object(runner.oe, "close_position") as close:
                    with self.assertRaisesRegex(
                            runner.PlanError,
                            "analysis|analysis_deadline_exceeded"):
                        runner.execute_position_plan(
                            plan,
                            facts,
                            cycle_id=protected_cycle,
                            db_root=db_root,
                            receipt_file=root / "receipt.json",
                            nudge=False,
                            state_file=state_file,
                            plan_sha256="a" * 64,
                            runtime_guard=guard,
                        )
                self.assertFalse(state_file.exists())
                close.assert_not_called()

    def test_analysis_authority_is_rechecked_before_executor(self) -> None:
        protected_cycle = "2026-08-15T21:45"
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "analysis race fence",
        }])
        plan["cycle_id"] = protected_cycle
        plan["receipt_context"]["cycle_id"] = protected_cycle
        facts = _facts()
        facts["cycle_id"] = protected_cycle
        calls = 0
        patches = self._patch_validation()

        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3]:
            root = Path(tmp)
            status_dir, db_root = _create_runtime_authority(
                root,
                cycle_id=protected_cycle,
                analysis_status="ok",
                analysis_ts="2026-08-15 21:50:00",
            )
            state_file = root / "state.json"

            def guard(cycle):
                nonlocal calls
                calls += 1
                authority = runner.validate_live_runtime_authority(
                    cycle,
                    db_root=db_root,
                    status_dir=status_dir,
                    now=datetime(2026, 8, 15, 21, 51,
                                 tzinfo=runner.CST),
                )
                if calls == 1:
                    with closing(sqlite3.connect(
                            db_root / "analysis.db")) as con:
                        con.execute(
                            "UPDATE analysis_runs SET status='skipped' "
                            "WHERE cycle_id=?",
                            (protected_cycle,),
                        )
                        con.commit()
                return authority

            with mock.patch.object(runner.oe, "close_position") as close:
                with self.assertRaisesRegex(
                        runner.PlanError, "analysis status=skipped"):
                    runner.execute_position_plan(
                        plan,
                        facts,
                        cycle_id=protected_cycle,
                        db_root=db_root,
                        receipt_file=root / "receipt.json",
                        nudge=False,
                        state_file=state_file,
                        plan_sha256="a" * 64,
                        runtime_guard=guard,
                    )
            marker = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(2, calls)
        self.assertEqual("failed", marker["state"])
        close.assert_not_called()

    def test_late_runner_is_rejected_before_marker_and_executor(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "late close must not run",
        }])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_dir, db_root = _create_runtime_authority(
                root, stage_status="stopping", with_lease=False)
            state_file = root / "state.json"

            def guard(cycle):
                return runner.validate_live_runtime_authority(
                    cycle,
                    db_root=db_root,
                    status_dir=status_dir,
                    now=datetime(2026, 8, 15, 15, 20,
                                 tzinfo=runner.CST),
                )

            with mock.patch.object(runner.oe, "close_position") as close:
                with self.assertRaisesRegex(
                        runner.PlanError, "stopping.*晚到 runner"):
                    runner.execute_position_plan(
                        plan,
                        _facts(),
                        cycle_id=CYCLE,
                        db_root=db_root,
                        receipt_file=root / "receipt.json",
                        nudge=False,
                        state_file=state_file,
                        plan_sha256="a" * 64,
                        runtime_guard=guard,
                    )
            self.assertFalse(state_file.exists())
            close.assert_not_called()

    def test_runtime_authority_rejects_at_absolute_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_dir, db_root = _create_runtime_authority(Path(tmp))
            with self.assertRaisesRegex(
                    runner.PlanError, "cycle_deadline_exceeded"):
                runner.validate_live_runtime_authority(
                    CYCLE,
                    db_root=db_root,
                    status_dir=status_dir,
                    now=datetime(2026, 8, 15, 15, 28,
                                 tzinfo=runner.CST),
                )

    def test_runtime_authority_is_rechecked_before_executor(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "race fence",
        }])
        calls = 0

        def guard(_cycle):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise runner.PlanError("live stage status=stopping")

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.oe, "close_position") as close:
            root = Path(tmp)
            state_file = root / "state.json"
            with self.assertRaisesRegex(runner.PlanError, "stopping"):
                runner.execute_position_plan(
                    plan,
                    _facts(),
                    cycle_id=CYCLE,
                    db_root=root,
                    receipt_file=root / "receipt.json",
                    nudge=False,
                    state_file=state_file,
                    plan_sha256="a" * 64,
                    runtime_guard=guard,
                )
            marker = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(2, calls)
        self.assertEqual("failed", marker["state"])
        close.assert_not_called()

    def test_profile_lock_is_cross_process_and_kernel_released(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "live_runner.lock"
            child = (
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from pathlib import Path\n"
                "import live_position_action_runner as r\n"
                "with r._runner_cycle_lock(Path(sys.argv[1]), 'child'):\n"
                " print('LOCKED', flush=True)\n"
                " sys.stdin.readline()\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", child, str(lock_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(proc.stdout.readline().strip(), "LOCKED")
                with self.assertRaisesRegex(runner.PlanError, "进程锁"):
                    with runner._runner_cycle_lock(lock_path, CYCLE):
                        self.fail("second process unexpectedly acquired lock")
                assert proc.stdin is not None
                proc.stdin.write("\n")
                proc.stdin.flush()
                self.assertEqual(proc.wait(timeout=10), 0, proc.stderr.read())
                with runner._runner_cycle_lock(lock_path, CYCLE):
                    pass
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()

    def test_contract_problem_with_confirmed_trade_commits_failed_superset(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "close",
        }])
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.oe, "close_position", return_value={
                    "ok": True, "action_taken": "REDUCE", "p0": False,
                    "trades": [_trade("BTC-USDT-SWAP", "long")],
                }), \
                mock.patch.object(runner.tw, "commit_receipt",
                                  return_value={"ok": True}) as commit:
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)

        self.assertFalse(result["ok"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["receipt"]["n_orders"], 1)
        self.assertEqual(result["receipt"]["batch_status"], "partial")
        self.assertEqual(len(result["receipt"]["position_action_failures"]), 1)
        commit.assert_called_once()

    def test_persistent_trade_contract_error_routes_to_salvage(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "close",
        }])
        trade = _trade("BTC-USDT-SWAP", "long")
        trade["ordId"] = "CLOSE-CONFIRMED-1"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner, "validate_facts", return_value=[]), \
                mock.patch.object(
                    runner.oe, "validate_receipt_context", return_value=[]
                ), \
                mock.patch.object(
                    runner.tw, "validate", return_value=["persistent contract error"]
                ), \
                mock.patch.object(
                    runner.tw, "validate_strict_live_receipt", return_value=[]
                ), \
                mock.patch.object(runner.oe, "close_position", return_value={
                    "ok": True, "action_taken": "CLOSE", "p0": False,
                    "trades": [trade],
                }), \
                mock.patch.object(
                    runner.tw, "commit_side_effect_salvage",
                    return_value={"ok": True, "quarantined": True},
                ) as salvage, \
                mock.patch.object(runner.tw, "commit_receipt") as regular:
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)

        self.assertFalse(result["ok"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["receipt"]["n_orders"], 1)
        self.assertEqual(result["receipt"]["status"], "error")
        salvage.assert_called_once()
        regular.assert_not_called()

    def test_receipt_file_failure_after_fill_still_commits_ledger_and_fails_marker(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "close",
        }])
        real_write = runner._atomic_write_json
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp:
            receipt_file = Path(tmp) / "receipt.json"
            state_file = Path(tmp) / "state.json"
            db_path = Path(tmp) / "live_trades.db"
            _create_trade_db(db_path)

            def fail_receipt_only(path, payload):
                if Path(path) == receipt_file:
                    raise OSError("isolated receipt disk failure")
                return real_write(path, payload)

            with patches[0], patches[1], patches[2], patches[3], \
                    mock.patch.object(runner, "_atomic_write_json",
                                      side_effect=fail_receipt_only), \
                    mock.patch.object(runner.oe, "close_position", return_value={
                        "ok": True, "action_taken": "CLOSE", "p0": False,
                        "trades": [_trade("BTC-USDT-SWAP", "long")],
                    }), \
                    mock.patch.object(
                        runner.tw, "_analysis_context_for_cycle", return_value={}
                    ), \
                    mock.patch.object(
                        runner.tw, "write_experiences", return_value={"exp": 0}
                    ):
                result = runner.execute_position_plan(
                    plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                    receipt_file=receipt_file, nudge=False,
                    state_file=state_file, plan_sha256="a" * 64)

            marker = json.loads(state_file.read_text(encoding="utf-8"))
            con = sqlite3.connect(db_path)
            try:
                trade_count = con.execute(
                    "SELECT COUNT(*) FROM trades WHERE cycle_id=?", (CYCLE,)
                ).fetchone()[0]
                raw = json.loads(con.execute(
                    "SELECT raw FROM trade_cycles WHERE cycle_id=?", (CYCLE,)
                ).fetchone()[0])
            finally:
                con.close()
        self.assertFalse(result["ok"])
        self.assertTrue(result["committed"])
        self.assertIn("isolated receipt disk failure",
                      result["receipt_file_error"])
        self.assertEqual(marker["state"], "failed")
        self.assertEqual(trade_count, 1)
        self.assertIn("isolated receipt disk failure",
                      raw["receipt_file_warning"])

    def test_interim_receipt_file_failure_commits_close_but_blocks_later_open(self) -> None:
        card = _open_card(judgement="OPEN SOL only after interim")
        plan = _plan([
            {
                "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
                "pos_side": "long", "reasoning": "close first",
            },
            {
                "action": "OPEN", "symbol": "SOL-USDT-SWAP", "side": "long",
                "target_stop_risk_pct_equity": 0.01, "lev": 5,
            },
        ])
        real_write = runner._atomic_write_json
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp:
            receipt_file = Path(tmp) / "receipt.json"
            state_file = Path(tmp) / "state.json"
            committed_flags: list[bool] = []

            def fail_receipt_only(path, payload):
                if Path(path) == receipt_file:
                    raise OSError("interim audit unavailable")
                return real_write(path, payload)

            def commit(receipt, profile, **kwargs):
                committed_flags.append(bool(receipt.get("runner_in_progress")))
                return {"ok": True}

            with patches[0], patches[1], patches[2], patches[3], \
                    mock.patch.object(runner, "_load_analysis_signal", return_value={
                        "action": "open_long", "side": "long",
                        "reasoning": "canonical open", "decision_card": card,
                    }), \
                    mock.patch.object(runner, "_atomic_write_json",
                                      side_effect=fail_receipt_only), \
                    mock.patch.object(runner.oe, "close_position", return_value={
                        "ok": True, "action_taken": "CLOSE", "p0": False,
                        "trades": [_trade("BTC-USDT-SWAP", "long")],
                    }), \
                    mock.patch.object(runner.oe, "open_position") as opened, \
                    mock.patch.object(runner.tw, "commit_receipt",
                                      side_effect=commit):
                result = runner.execute_position_plan(
                    plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                    receipt_file=receipt_file, nudge=False,
                    state_file=state_file, plan_sha256="a" * 64)

            marker = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["committed"])
        self.assertEqual(committed_flags, [True, False])
        self.assertEqual(result["receipt"]["n_orders"], 1)
        self.assertEqual(marker["state"], "failed")
        opened.assert_not_called()

    def test_empty_action_list_commits_hold_without_executor(self) -> None:
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.oe, "close_position") as close, \
                mock.patch.object(runner.oe, "reduce_position") as reduce, \
                mock.patch.object(runner.oe, "adjust_protection") as adjust, \
                mock.patch.object(
                    runner.tw, "commit_receipt", return_value={"ok": True}
                ):
            result = runner.execute_position_plan(
                _plan([]), _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["receipt"]["action_taken"], "HOLD")
        self.assertEqual(result["receipt"]["n_orders"], 0)
        close.assert_not_called()
        reduce.assert_not_called()
        adjust.assert_not_called()

    def test_blocking_facts_without_authorized_action_commits_error_terminal(self) -> None:
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(
                    runner.tw, "commit_receipt", return_value={"ok": True}
                ):
            result = runner.execute_position_plan(
                _plan([]), _facts(status="blocking"), cycle_id=CYCLE,
                db_root=Path(tmp), receipt_file=Path(tmp) / "receipt.json",
                nudge=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["receipt"]["decision"], "error")
        self.assertEqual(result["receipt"]["action_taken"], "REJECT")

    def test_close_that_is_already_flat_becomes_auditable_hold(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "close if still present",
        }])
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.oe, "close_position", return_value={
                    "ok": True, "action_taken": "CLOSE", "trades": [],
                    "note": "no_open_position", "symbol": "BTC-USDT-SWAP",
                }), \
                mock.patch.object(
                    runner.tw, "commit_receipt", return_value={"ok": True}
                ):
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["receipt"]["action_taken"], "HOLD")
        self.assertEqual(len(result["receipt"]["position_action_results"]), 1)

    def test_reduce_equal_to_full_position_is_preflight_rejected(self) -> None:
        plan = _plan([{
            "action": "REDUCE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reduce_sz": 3,
            "reasoning": "would accidentally be a full close",
        }])
        with mock.patch.object(runner, "validate_facts", return_value=[]), \
                mock.patch.object(
                    runner.oe, "validate_receipt_context", return_value=[]
                ):
            with self.assertRaisesRegex(runner.PlanError, "严格小于"):
                runner.preflight_plan(plan, _facts(), cycle_id=CYCLE)

    def test_unknown_action_field_is_not_silently_ignored(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "close", "lever": 99,
        }])
        with mock.patch.object(runner, "validate_facts", return_value=[]), \
                mock.patch.object(
                    runner.oe, "validate_receipt_context", return_value=[]
                ):
            with self.assertRaisesRegex(runner.PlanError, "未知字段"):
                runner.preflight_plan(plan, _facts(), cycle_id=CYCLE)


if __name__ == "__main__":
    unittest.main()
