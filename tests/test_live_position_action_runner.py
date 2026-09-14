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
# The production wrapper seeds <PROJECT_ROOT>\scripts on sys.path.  This test is also
# run from an isolated staging tree, so force the paired supervisor module from
# the same tree instead of accepting a previously cached production import.
sys.modules.pop("stage_runner", None)
sys.path.insert(0, str(ROOT / "scripts"))
import stage_runner  # noqa: E402


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


def _position_exit_evidence(facts: dict) -> dict:
    payload = {
        "schema_version": 1,
        "mode": "read_only",
        "scope": "all_current_position_exit_review",
        "cycle_id": CYCLE,
        "facts_hash": facts["facts_hash"],
        "positions": [
            {"symbol": row["instId"], "side": row["posSide"]}
            for row in facts["positions"]
        ],
        "production_database_writes": 0,
        "orders_placed": 0,
        "ok": True,
        "status": "PASSED",
        "position_count": len(facts["positions"]),
    }
    payload["evidence_hash"] = runner._canonical_hash(payload)
    return payload


def _plan(actions: list[dict]) -> dict:
    return {
        "cycle_id": CYCLE,
        "receipt_context": {
            "cycle_id": CYCLE,
            "mode": "live",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "decision_card": _card(),
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

    def test_missing_required_position_exit_fails_before_side_effects(self) -> None:
        facts = _facts()
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3]:
            root = Path(tmp)
            receipt = root / "receipt.json"
            evidence = root / f"position_exit_{CYCLE.replace(':', '-')}.json"
            with self.assertRaisesRegex(
                    runner.PlanError, "position_exit_evidence_missing"):
                runner.execute_position_plan(
                    _plan([]), facts, cycle_id=CYCLE, db_root=root,
                    receipt_file=receipt, position_exit_file=evidence,
                    nudge=False,
                )
            marker = json.loads((
                root / f"live_runner_state_{CYCLE.replace(':', '-')}.json"
            ).read_text(encoding="utf-8"))
        self.assertEqual("failed_preflight", marker["state"])
        self.assertEqual(1, marker["preflight_attempts"])
        self.assertIn("position_exit_evidence_missing", marker["error"])

    def test_position_exit_evidence_requires_exact_facts_binding(self) -> None:
        facts = _facts()
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "position_exit.json"
            payload = _position_exit_evidence(facts)
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            runner._validate_position_exit_evidence(
                facts, cycle_id=CYCLE, evidence_file=evidence)
            payload["facts_hash"] = "0" * 64
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                    runner.PlanError, "position_exit_evidence_invalid"):
                runner._validate_position_exit_evidence(
                    facts, cycle_id=CYCLE, evidence_file=evidence)

    def test_position_exit_not_required_when_no_positions(self) -> None:
        facts = _facts()
        facts["positions"] = []
        with tempfile.TemporaryDirectory() as tmp:
            runner._validate_position_exit_evidence(
                facts,
                cycle_id=CYCLE,
                evidence_file=Path(tmp) / "missing.json",
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

    def test_v4_business_terminal_is_stamped_before_writer_commit(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "thesis invalid",
        }])
        captured: dict = {}

        def commit(receipt, _profile, **_kwargs):
            captured.update(receipt)
            return {"ok": True, "written": 1}

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], mock.patch.object(
                    runner.thresholds,
                    "complete_cycle_uses_business_terminal_stop",
                    return_value=True,
                ), mock.patch.object(runner.oe, "close_position", return_value={
                    "profile": "live", "ok": True,
                    "action_taken": "CLOSE", "side": "long", "p0": False,
                    "trades": [_trade("BTC-USDT-SWAP", "long")],
                }), mock.patch.object(
                    runner.tw, "commit_receipt", side_effect=commit):
            result = runner.execute_position_plan(
                plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False)

        terminal = result["receipt"]["business_terminal"]
        self.assertEqual(1, terminal["schema_version"])
        self.assertEqual(CYCLE, terminal["cycle_id"])
        self.assertEqual("completed", terminal["status"])
        self.assertFalse(terminal["persistence_completed"])
        self.assertEqual(terminal, captured["business_terminal"])

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

    def test_open_replaces_retyped_cycle_card_before_context_validation(self) -> None:
        canonical = _open_card(judgement="OPEN SOL canonical")
        retyped = _card()
        retyped["agent_judgement"] = (
            "BTC-USDT-SWAP HOLD：保护有效；SOL-USDT-SWAP OPEN"
        )
        retyped["position_reviews"] = [{
            "instId": "BTC-USDT-SWAP",
            "action": "HOLD",
            "reasoning": "保护有效",
        }]
        retyped["historical_experience"]["evidence_contract"] = {
            "protocol": "experience_evidence_v2",
            "summaries": {"cross_symbol_similar": {"n": 46}},
            "evidence_hash": "stale-copy",
        }
        plan = _plan([{
            "action": "OPEN",
            "symbol": "SOL-USDT-SWAP",
            "side": "long",
            "target_stop_risk_pct_equity": 0.01,
            "lev": 5,
        }])
        plan["receipt_context"]["decision_card"] = retyped

        validated_cards = []

        def validate_context(context, **_kwargs):
            validated_cards.append(context["decision_card"])
            return []

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner, "validate_facts", return_value=[]), \
                mock.patch.object(
                    runner.oe, "validate_receipt_context",
                    side_effect=validate_context,
                ), \
                mock.patch.object(
                    runner, "_load_analysis_signal", return_value={
                        "action": "open_long",
                        "side": "long",
                        "reasoning": "canonical reasoning",
                        "decision_card": canonical,
                    },
                ) as load_signal:
            context, actions = runner.preflight_plan(
                plan,
                _facts(),
                cycle_id=CYCLE,
                db_root=Path(tmp),
            )

        expected_cycle_card = dict(canonical)
        expected_cycle_card["agent_judgement"] = retyped["agent_judgement"]
        expected_cycle_card["position_reviews"] = retyped["position_reviews"]
        self.assertEqual(expected_cycle_card, context["decision_card"])
        self.assertEqual(canonical, actions[0]["_decision_card"])
        self.assertEqual(retyped, plan["receipt_context"]["decision_card"])
        self.assertEqual(expected_cycle_card, validated_cards[0])
        self.assertEqual(canonical, validated_cards[1])
        self.assertEqual(2, load_signal.call_count)

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
        self.assertEqual(
            state["schema_version"], runner.RUNNER_STATE_SCHEMA_VERSION)
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

    def test_adjust_rejects_open_style_sl_field_before_executor(self) -> None:
        plan = _plan([{
            "action": "ADJUST_PROTECTION",
            "symbol": "ETH-USDT-SWAP",
            "pos_side": "short",
            "sl_trigger_px": 105,
            "reasoning": "wrong field name must fail closed",
        }])
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.oe, "adjust_protection") as adjusted:
            with self.assertRaisesRegex(
                    runner.PlanError, "未知字段: sl_trigger_px"):
                runner.execute_position_plan(
                    plan, _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                    receipt_file=Path(tmp) / "receipt.json", nudge=False)
        adjusted.assert_not_called()

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
                "schema_version": runner.RUNNER_STATE_SCHEMA_VERSION,
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

    def test_legacy_v1_marker_fails_closed_before_executor(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "legacy marker fence",
        }])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "state.json"
            state_file.write_text(json.dumps({
                "schema_version": 1,
                "cycle_id": CYCLE,
                "state": "started",
                "facts_hash": "f" * 64,
                "plan_sha256": "a" * 64,
            }), encoding="utf-8")
            with mock.patch.object(runner.oe, "close_position") as close:
                with self.assertRaisesRegex(
                        runner.PlanError, "legacy/invalid schema"):
                    runner.execute_position_plan(
                        plan, _facts(), cycle_id=CYCLE, db_root=root,
                        receipt_file=root / "receipt.json", nudge=False,
                        state_file=state_file, plan_sha256="a" * 64)
            close.assert_not_called()

    def test_malformed_handoff_gate_fails_closed_before_executor(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "malformed gate fence",
        }])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "state.json"
            handoff = runner._default_handoff_state_file(state_file, CYCLE)
            handoff.write_text("{", encoding="utf-8")

            def guard(_cycle):
                return {"cycle_id": CYCLE, "stage_runner_pid": 12345}

            with mock.patch.object(runner.oe, "close_position") as close:
                with self.assertRaisesRegex(
                        runner.PlanError, "handoff gate 不可校验"):
                    runner.execute_position_plan(
                        plan, _facts(), cycle_id=CYCLE, db_root=root,
                        receipt_file=root / "receipt.json", nudge=False,
                        state_file=state_file, plan_sha256="a" * 64,
                        runtime_guard=guard)
            self.assertFalse(state_file.exists())
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

    def test_runtime_guard_non_object_fails_closed_before_marker(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "authority shape fence",
        }])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_file = root / "state.json"
            with mock.patch.object(runner.oe, "close_position") as close:
                with self.assertRaisesRegex(
                        runner.PlanError, "authority object"):
                    runner.execute_position_plan(
                        plan, _facts(), cycle_id=CYCLE, db_root=root,
                        receipt_file=root / "receipt.json", nudge=False,
                        state_file=state_file, plan_sha256="a" * 64,
                        runtime_guard=lambda _cycle: None)
            self.assertFalse(state_file.exists())
            close.assert_not_called()

    def test_cycle_and_production_tmp_paths_are_canonical(self) -> None:
        with self.assertRaisesRegex(runner.PlanError, "15 分钟自然槽"):
            runner._validated_cycle_id("2026-08-15T15:17")
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "position_plan.json"
            with self.assertRaisesRegex(runner.PlanError, "受控 tmp"):
                runner._require_direct_tmp_path(outside, "plan-file")

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

    def test_supervisor_revocation_fences_still_running_late_runner(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "must lose revoked handoff",
        }])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_dir, db_root = _create_runtime_authority(root)
            facts = _facts()
            state_file = root / "live_runner_state_2026-08-15T15-15.json"
            handoff_file = runner._default_handoff_state_file(
                state_file, CYCLE)
            handoff_file.write_text(json.dumps({
                "schema_version": runner.HANDOFF_GATE_SCHEMA_VERSION,
                "cycle_id": CYCLE,
                "state": "revoked",
                "reason": (
                    "post_facts_runner_handoff_violation:"
                    "no_valid_runner_marker"
                ),
                "session_key": runner._gateway_session_key(CYCLE),
                "stage_runner_pid": 12345,
                "facts_hash": "f" * 64,
                "plan_sha256": "a" * 64,
            }), encoding="utf-8")

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
                        runner.PlanError, "supervisor 原子撤销"):
                    runner.execute_position_plan(
                        plan,
                        facts,
                        cycle_id=CYCLE,
                        db_root=db_root,
                        receipt_file=root / "receipt.json",
                        nudge=False,
                        state_file=state_file,
                        plan_sha256="a" * 64,
                        runtime_guard=guard,
                    )

            # Stage status and lease deliberately remain valid here.  The
            # durable CAS fence alone must prevent marker/executor side effects.
            stage = json.loads(
                (status_dir / "live-2026-08-15T15-15.json").read_text(
                    encoding="utf-8"))
            self.assertEqual("running", stage["status"])
            self.assertFalse(state_file.exists())
            close.assert_not_called()

    def test_runner_marker_preserves_cycle_session_and_stage_binding(self) -> None:
        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(
                    runner.tw, "commit_receipt", return_value={"ok": True}
                ):
            root = Path(tmp)
            status_dir, db_root = _create_runtime_authority(root)
            state_file = root / "state.json"

            def guard(cycle):
                return runner.validate_live_runtime_authority(
                    cycle,
                    db_root=db_root,
                    status_dir=status_dir,
                    now=datetime(2026, 8, 15, 15, 20,
                                 tzinfo=runner.CST),
                )

            result = runner.execute_position_plan(
                _plan([]),
                _facts(),
                cycle_id=CYCLE,
                db_root=db_root,
                receipt_file=root / "receipt.json",
                nudge=False,
                state_file=state_file,
                plan_sha256="a" * 64,
                runtime_guard=guard,
            )
            marker = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertTrue(result["committed"])
        self.assertEqual("committed", marker["state"])
        self.assertEqual(CYCLE, marker["cycle_id"])
        self.assertEqual(
            runner._gateway_session_key(CYCLE), marker["session_key"])
        self.assertEqual(12345, marker["stage_runner_pid"])

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

    def test_final_handoff_lock_is_held_through_executor_call(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "linearized admission",
        }])
        patches = self._patch_validation()
        observer_blocked = []
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.tw, "commit_receipt",
                                  return_value={"ok": True}):
            root = Path(tmp)
            state_file = root / "state.json"
            handoff_lock = runner._default_handoff_lock_file(state_file, CYCLE)

            def close(*_args, **_kwargs):
                with self.assertRaisesRegex(runner.PlanError, "进程锁"):
                    with runner._runner_cycle_lock(handoff_lock, CYCLE):
                        self.fail("observer acquired handoff during executor")
                observer_blocked.append(True)
                return {
                    "ok": True, "action_taken": "CLOSE", "p0": False,
                    "trades": [_trade("BTC-USDT-SWAP", "long")],
                }

            with mock.patch.object(
                    runner.oe, "close_position", side_effect=close):
                result = runner.execute_position_plan(
                    plan, _facts(), cycle_id=CYCLE, db_root=root,
                    receipt_file=root / "receipt.json", nudge=False,
                    state_file=state_file, plan_sha256="a" * 64,
                    runtime_guard=lambda cycle: {
                        "cycle_id": cycle, "stage_runner_pid": 12345})
            with runner._runner_cycle_lock(handoff_lock, CYCLE):
                pass
        self.assertEqual([True], observer_blocked)
        self.assertTrue(result["committed"])

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
            return {"cycle_id": CYCLE, "stage_runner_pid": 12345}

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

    def test_deadline_flip_at_true_executor_boundary_blocks_call(self) -> None:
        plan = _plan([{
            "action": "CLOSE", "symbol": "BTC-USDT-SWAP",
            "pos_side": "long", "reasoning": "deadline admission fence",
        }])
        calls = 0

        def guard(_cycle):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise runner.PlanError("cycle_deadline_exceeded: flipped")
            return {"cycle_id": CYCLE, "stage_runner_pid": 12345}

        patches = self._patch_validation()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3], \
                mock.patch.object(runner.oe, "close_position") as close:
            root = Path(tmp)
            state_file = root / "state.json"
            with self.assertRaisesRegex(
                    runner.PlanError, "cycle_deadline_exceeded"):
                runner.execute_position_plan(
                    plan, _facts(), cycle_id=CYCLE, db_root=root,
                    receipt_file=root / "receipt.json", nudge=False,
                    state_file=state_file, plan_sha256="a" * 64,
                    runtime_guard=guard)
            marker = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(3, calls)
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

    def test_two_process_handoff_cas_runner_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            safe_cycle = CYCLE.replace(":", "-")
            facts_file = tmp_root / f"live_facts_{safe_cycle}.json"
            plan_file = tmp_root / f"position_plan_{safe_cycle}.json"
            state_file = tmp_root / f"live_runner_state_{safe_cycle}.json"
            receipt_file = tmp_root / "receipt.json"
            facts_file.write_text(
                json.dumps({"facts_hash": "f" * 64}), encoding="utf-8")
            plan_file.write_text('{"actions":[]}', encoding="utf-8")
            plan_sha = runner.hashlib.sha256(plan_file.read_bytes()).hexdigest()
            child = (
                "import json,sys\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "import live_position_action_runner as r\n"
                "def guard(cycle):\n"
                " print('CLAIMING', flush=True)\n"
                " sys.stdin.readline()\n"
                " return {'cycle_id':cycle,'stage_runner_pid':12345}\n"
                "try:\n"
                " r.execute_position_plan({'actions':[]},"
                " {'facts_hash':'f'*64}, cycle_id=sys.argv[1],"
                " db_root=r.Path(sys.argv[2]), receipt_file=r.Path(sys.argv[3]),"
                " state_file=r.Path(sys.argv[4]), plan_sha256=sys.argv[5],"
                " runtime_guard=guard, nudge=False)\n"
                "except Exception:\n"
                " print('EXPECTED_TERMINAL', flush=True)\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", child, CYCLE, str(tmp_root),
                 str(receipt_file), str(state_file), plan_sha],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                creationflags=(0x08000000 if sys.platform == "win32" else 0),
            )
            try:
                self.assertEqual("CLAIMING", proc.stdout.readline().strip())
                observer = stage_runner._LiveChildObserver(
                    CYCLE, tmp_root=tmp_root, db_root=Path(tmp) / "db",
                    now_fn=lambda: plan_file.stat().st_mtime + 31,
                    expected_session_key=runner._gateway_session_key(CYCLE),
                    expected_stage_runner_pid=12345,
                )
                self.assertIsNone(observer())
                self.assertEqual(
                    "claim_in_progress",
                    observer.evidence["handoff_arbitration"],
                )
                self.assertFalse(observer.handoff_path.exists())
                proc.stdin.write("\n")
                proc.stdin.flush()
                self.assertEqual(proc.wait(timeout=10), 0, proc.stderr.read())
                # 2026-08-24：首次预检拒不再直接终态——runner 留下
                # failed_preflight 驻留 marker，观察者在重写窗口内继续等待，
                # 窗口耗尽才由 supervisor 撤销交接并收口。
                self.assertIsNone(observer())
                self.assertEqual(
                    "failed_preflight",
                    observer.evidence["runner_state_value"])
                late = stage_runner._LiveChildObserver(
                    CYCLE, tmp_root=tmp_root, db_root=Path(tmp) / "db",
                    now_fn=lambda: (
                        state_file.stat().st_mtime
                        + stage_runner._LIVE_PREFLIGHT_REWRITE_SECONDS + 1),
                    expected_session_key=runner._gateway_session_key(CYCLE),
                    expected_stage_runner_pid=12345,
                )
                self.assertEqual(
                    "runner_terminal:failed_preflight_rewrite_timeout",
                    late())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()

    def test_two_process_handoff_cas_supervisor_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "tmp"
            tmp_root.mkdir()
            safe_cycle = CYCLE.replace(":", "-")
            facts_file = tmp_root / f"live_facts_{safe_cycle}.json"
            plan_file = tmp_root / f"position_plan_{safe_cycle}.json"
            state_file = tmp_root / f"live_runner_state_{safe_cycle}.json"
            facts_file.write_text(
                json.dumps({"facts_hash": "f" * 64}), encoding="utf-8")
            plan_file.write_text('{"actions":[]}', encoding="utf-8")
            plan_sha = runner.hashlib.sha256(plan_file.read_bytes()).hexdigest()
            observer = stage_runner._LiveChildObserver(
                CYCLE, tmp_root=tmp_root, db_root=Path(tmp) / "db",
                now_fn=lambda: plan_file.stat().st_mtime + 31,
                expected_session_key=runner._gateway_session_key(CYCLE),
                expected_stage_runner_pid=12345,
            )
            self.assertEqual(
                "post_facts_runner_handoff_violation:no_valid_runner_marker",
                observer(),
            )
            child = (
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "import live_position_action_runner as r\n"
                "def guard(cycle):\n"
                " return {'cycle_id':cycle,'stage_runner_pid':12345}\n"
                "try:\n"
                " r.execute_position_plan({'actions':[]},"
                " {'facts_hash':'f'*64}, cycle_id=sys.argv[1],"
                " db_root=r.Path(sys.argv[2]), receipt_file=r.Path(sys.argv[3]),"
                " state_file=r.Path(sys.argv[4]), plan_sha256=sys.argv[5],"
                " runtime_guard=guard, nudge=False)\n"
                "except r.PlanError:\n"
                " print('BLOCKED')\n"
                "else:\n"
                " raise SystemExit(7)\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", child, CYCLE, str(tmp_root),
                 str(tmp_root / "receipt.json"), str(state_file), plan_sha],
                capture_output=True, text=True, timeout=10,
                creationflags=(0x08000000 if sys.platform == "win32" else 0),
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("BLOCKED", completed.stdout.strip())
            self.assertFalse(state_file.exists())

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

    def test_receipt_context_omits_equity_and_injects_canonical_fact(self) -> None:
        plan = _plan([])
        with mock.patch.object(
            runner.oe, "validate_receipt_context", return_value=[]
        ):
            context = runner._normalize_context(plan, _facts(), CYCLE)
        self.assertEqual(1000.0, context["equity"])
        self.assertNotIn("equity", plan["receipt_context"])

    def test_runner_owned_terminal_context_fields_are_ignored(self) -> None:
        plan = _plan([])
        plan["receipt_context"].update({
            "decision": "hold",
            "action_taken": "HOLD",
            "n_orders": 999,
            "trades": [{"fabricated": True}],
            "errors": ["fabricated"],
            "ok": True,
        })
        with mock.patch.object(
            runner.oe, "validate_receipt_context", return_value=[]
        ):
            context = runner._normalize_context(plan, _facts(), CYCLE)

        self.assertFalse(runner.TERMINAL_CONTEXT_KEYS & set(context))
        self.assertEqual("hold", plan["receipt_context"]["decision"])
        self.assertEqual(999, plan["receipt_context"]["n_orders"])

    def test_equity_mismatch_keeps_specific_contract_error(self) -> None:
        plan = _plan([])
        plan["receipt_context"]["equity"] = 999.0
        with self.assertRaisesRegex(
            runner.PlanError, "receipt_context.equity 与 live_facts 不一致"
        ):
            runner._normalize_context(plan, _facts(), CYCLE)

    def test_nonnumeric_equity_reports_number_error(self) -> None:
        plan = _plan([])
        plan["receipt_context"]["equity"] = "not-a-number"
        with self.assertRaisesRegex(
            runner.PlanError, "receipt_context.equity 必须是有效数字"
        ):
            runner._normalize_context(plan, _facts(), CYCLE)

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


class PreflightRewriteRetryTests(unittest.TestCase):
    """2026-08-24：预检拒（零副作用）允许且只允许一次整文件 plan 重写。"""

    def _writer_patches(self):
        # 保留真实 oe.validate_receipt_context —— 本组测试恰恰要验证真实
        # 预检契约的拒绝/放行；只 mock facts 校验与 writer 落库面。
        return (
            mock.patch.object(runner, "validate_facts", return_value=[]),
            mock.patch.object(runner.tw, "validate", return_value=[]),
            mock.patch.object(
                runner.tw, "validate_strict_live_receipt", return_value=[]
            ),
            mock.patch.object(
                runner.tw, "commit_receipt", return_value={"ok": True}
            ),
        )

    @staticmethod
    def _bad_plan() -> dict:
        plan = _plan([])
        plan["receipt_context"]["decision_card"] = None
        return plan

    @staticmethod
    def _marker(state_file: Path) -> dict:
        return json.loads(state_file.read_text(encoding="utf-8"))

    def test_preflight_rejection_parks_marker_then_one_rewrite_commits(
            self) -> None:
        patches = self._writer_patches()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3]:
            state_file = Path(tmp) / "state.json"
            with self.assertRaisesRegex(
                    runner.PlanError, "decision_card 必须是 dict"):
                runner.execute_position_plan(
                    self._bad_plan(), _facts(), cycle_id=CYCLE,
                    db_root=Path(tmp), receipt_file=Path(tmp) / "receipt.json",
                    nudge=False, state_file=state_file,
                    plan_sha256="a" * 64)
            marker = self._marker(state_file)
            self.assertEqual("failed_preflight", marker["state"])
            self.assertEqual(1, marker["preflight_attempts"])
            self.assertIn("预检失败", marker["error"])

            result = runner.execute_position_plan(
                _plan([]), _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False,
                state_file=state_file, plan_sha256="b" * 64)
            marker = self._marker(state_file)
        self.assertTrue(result["ok"])
        self.assertEqual(result["receipt"]["action_taken"], "HOLD")
        self.assertEqual("committed", marker["state"])
        self.assertEqual(2, marker["preflight_attempts"])

    def test_second_preflight_rejection_is_sticky_terminal(self) -> None:
        patches = self._writer_patches()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3]:
            state_file = Path(tmp) / "state.json"
            with self.assertRaisesRegex(runner.PlanError, "预检失败"):
                runner.execute_position_plan(
                    self._bad_plan(), _facts(), cycle_id=CYCLE,
                    db_root=Path(tmp), receipt_file=Path(tmp) / "receipt.json",
                    nudge=False, state_file=state_file,
                    plan_sha256="a" * 64)
            with self.assertRaisesRegex(runner.PlanError, "预检失败"):
                runner.execute_position_plan(
                    self._bad_plan(), _facts(), cycle_id=CYCLE,
                    db_root=Path(tmp), receipt_file=Path(tmp) / "receipt.json",
                    nudge=False, state_file=state_file,
                    plan_sha256="c" * 64)
            marker = self._marker(state_file)
            self.assertEqual("failed", marker["state"])
            self.assertEqual(2, marker["preflight_attempts"])
            with self.assertRaisesRegex(runner.PlanError, "拒绝重复执行"):
                runner.execute_position_plan(
                    _plan([]), _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                    receipt_file=Path(tmp) / "receipt.json", nudge=False,
                    state_file=state_file, plan_sha256="b" * 64)
            self.assertEqual("failed", self._marker(state_file)["state"])

    def test_same_plan_late_duplicate_does_not_consume_rewrite(self) -> None:
        patches = self._writer_patches()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3]:
            state_file = Path(tmp) / "state.json"
            with self.assertRaisesRegex(runner.PlanError, "预检失败"):
                runner.execute_position_plan(
                    self._bad_plan(), _facts(), cycle_id=CYCLE,
                    db_root=Path(tmp), receipt_file=Path(tmp) / "receipt.json",
                    nudge=False, state_file=state_file,
                    plan_sha256="a" * 64)

            with self.assertRaisesRegex(
                    runner.PlanError, "同一 plan 已预检失败") as raised:
                runner.execute_position_plan(
                    _plan([]), _facts(), cycle_id=CYCLE,
                    db_root=Path(tmp), receipt_file=Path(tmp) / "receipt.json",
                    nudge=False, state_file=state_file,
                    plan_sha256="a" * 64)

            marker = self._marker(state_file)
        self.assertIn("首次错误=", str(raised.exception))
        self.assertEqual("failed_preflight", marker["state"])
        self.assertEqual(1, marker["preflight_attempts"])

    def test_preflight_rewrite_requires_same_facts_identity(self) -> None:
        patches = self._writer_patches()
        with tempfile.TemporaryDirectory() as tmp, patches[0], patches[1], \
                patches[2], patches[3]:
            state_file = Path(tmp) / "state.json"
            with self.assertRaisesRegex(runner.PlanError, "预检失败"):
                runner.execute_position_plan(
                    self._bad_plan(), _facts(), cycle_id=CYCLE,
                    db_root=Path(tmp), receipt_file=Path(tmp) / "receipt.json",
                    nudge=False, state_file=state_file,
                    plan_sha256="a" * 64)
            regenerated = _facts()
            regenerated["facts_hash"] = "e" * 64
            with self.assertRaisesRegex(
                    runner.PlanError, "facts_hash 已变化"):
                runner.execute_position_plan(
                    _plan([]), regenerated, cycle_id=CYCLE, db_root=Path(tmp),
                    receipt_file=Path(tmp) / "receipt.json", nudge=False,
                    state_file=state_file, plan_sha256="b" * 64)
            marker = self._marker(state_file)
            self.assertEqual("failed_preflight", marker["state"])
            self.assertEqual(1, marker["preflight_attempts"])

    def test_post_preflight_writer_refusal_stays_sticky_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner, "validate_facts", return_value=[]), \
                mock.patch.object(runner.tw, "validate", return_value=[]), \
                mock.patch.object(
                    runner.tw, "validate_strict_live_receipt",
                    return_value=[]), \
                mock.patch.object(
                    runner.tw, "commit_receipt",
                    return_value={"ok": False, "error": "refused"}):
            state_file = Path(tmp) / "state.json"
            result = runner.execute_position_plan(
                _plan([]), _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                receipt_file=Path(tmp) / "receipt.json", nudge=False,
                state_file=state_file, plan_sha256="a" * 64)
            self.assertFalse(result["committed"])
            marker = self._marker(state_file)
            self.assertEqual("failed", marker["state"])
            with self.assertRaisesRegex(runner.PlanError, "拒绝重复执行"):
                runner.execute_position_plan(
                    _plan([]), _facts(), cycle_id=CYCLE, db_root=Path(tmp),
                    receipt_file=Path(tmp) / "receipt.json", nudge=False,
                    state_file=state_file, plan_sha256="b" * 64)

    def test_preflight_attempt_cap_twin_constant_matches_supervisor(
            self) -> None:
        self.assertEqual(
            runner.PREFLIGHT_MAX_ATTEMPTS,
            stage_runner._LIVE_PREFLIGHT_MAX_ATTEMPTS)

    def test_atomic_json_retries_windows_share_violation_without_reexecuting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            payload = {"cycle_id": CYCLE, "state": "committed"}
            real_replace = runner.os.replace
            calls = []

            def flaky_replace(source, destination):
                calls.append((source, destination))
                if len(calls) < 3:
                    raise PermissionError(13, "simulated Windows share violation")
                return real_replace(source, destination)

            with (
                mock.patch.object(runner.os, "replace", side_effect=flaky_replace),
                mock.patch.object(
                    runner, "ATOMIC_REPLACE_RETRY_DELAYS_SECONDS", (0.0, 0.0)),
            ):
                runner._atomic_write_json(target, payload)

            self.assertEqual(3, len(calls))
            self.assertEqual(payload, json.loads(target.read_text(encoding="utf-8")))
            self.assertEqual([], list(target.parent.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
