# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for value in (ROOT, ROOT / "core", ROOT / "scripts", ROOT / "collectors"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from core import decision_card
from core import order_executor
from collectors import trades_writer
from scripts import _acceptance_thresholds as thresholds
from scripts import build_push_payload
from scripts import live_position_action_runner as runner
from scripts import reconcile_exchange_closes as reconcile


CYCLE = "2026-09-02T18:00"


def package() -> dict:
    return {
        "contract": decision_card.OPEN_EXECUTION_PACKAGE_CONTRACT,
        "entry": 10.0,
        "stop": 9.0,
        "target": 12.0,
        "exit_mode": "fixed_tp",
    }


def legacy_analysis_card() -> dict:
    return {
        "contract": decision_card.LIGHTWEIGHT_OPEN_CONTRACT,
        "side": "long",
        "reasoning": "legacy analysis reasoning",
        "news_context": {"headline": "must not cross execution boundary"},
        "regime_scope": "legacy-extra",
        "risk_reward": {
            "entry": 10.0, "stop": 9.0, "target": 12.0,
            "exit_mode": "fixed_tp",
        },
    }


def trade() -> dict:
    return {
        "symbol": "X-USDT-SWAP",
        "action": "open",
        "side": "long",
        "sz": 1.0,
        "approved_sz": 1.0,
        "fill_sz": 1.0,
        "fill_px": 10.0,
        "fill_source": "fills",
        "fill_ts": "2026-09-02 18:01:00",
        "ts_source": "fills.fillTime",
        "lev": 5.0,
        "margin": 2.0,
        "notional": 10.0,
        "reasoning": "exchange-confirmed OPEN",
        decision_card.OPEN_EXECUTION_PACKAGE_KEY: package(),
    }


def receipt(*, top_package: bool = True) -> dict:
    value = {
        "cycle_id": CYCLE,
        "mode": "live",
        "profile": "live",
        "status": "ok",
        "decision": "traded",
        "action_taken": "OPEN_LONG",
        "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL,
        "reasoning": "OPEN X with deterministic risk package",
        "regime": "range",
        "n_orders": 1,
        "trades": [trade()],
        "errors": [],
        "ok": True,
    }
    if top_package:
        value[decision_card.OPEN_EXECUTION_PACKAGE_KEY] = package()
    return value


def create_trade_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE trade_cycles(
                cycle_id TEXT PRIMARY KEY, ts TEXT, mode TEXT,
                decision TEXT, n_orders INTEGER, equity REAL,
                note TEXT, raw TEXT
            );
            CREATE TABLE trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT, ts TEXT, symbol TEXT, action TEXT,
                side TEXT, sz REAL, fill_px REAL, lev REAL,
                margin REAL, notional REAL, score_total REAL,
                reasoning TEXT, deviation TEXT, degradation TEXT,
                pnl REAL, raw TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


class MinimalContractClosureExecutionPackageTests(unittest.TestCase):
    def policy(self):
        return (
            mock.patch.object(
                thresholds, "minimal_decision_contract_active",
                return_value=True),
            mock.patch.object(
                thresholds, "minimal_contract_closure_active",
                return_value=True),
        )

    def assert_exact_package(self, value: dict) -> None:
        self.assertEqual(
            decision_card.OPEN_EXECUTION_PACKAGE_FIELDS, set(value))
        self.assertNotIn("side", value)
        self.assertNotIn("reasoning", value)
        self.assertNotIn("risk_reward", value)
        self.assertNotIn("news_context", value)
        self.assertNotIn("regime_scope", value)

    def _seed_runner_partial_with_reconcile(
        self,
        db: Path,
        *,
        old_raw_updates: dict | None = None,
        reconcile_raw_updates: dict | None = None,
        ordinary_unmatched: bool = False,
    ) -> None:
        binding = {
            "facts_hash": "a" * 64,
            "plan_sha256": "b" * 64,
            "position_action_plan_hash": "c" * 64,
        }
        old_raw = {
            "status": "ok",
            "batch_status": "partial",
            "batch_ok": True,
            "runner_in_progress": True,
            "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL,
            **binding,
        }
        old_raw.update(old_raw_updates or {})
        reconcile_raw = {
            "reconcile_source": "exchange_fills_reconcile",
            "ts_source": "trusted_internal_override",
            "close_ts": "2026-09-02 18:00:15",
            "ord_ids": ["ORDER-RECONCILE"],
            "fills": [{
                "ts": "2026-09-02 18:00:15",
                "px": "20",
                "sz": "2",
                "pnl": "1.25",
                "ordId": "ORDER-RECONCILE",
                "tradeId": "TRADE-RECONCILE",
            }],
        }
        reconcile_raw.update(reconcile_raw_updates or {})
        rows = [
            (
                CYCLE, "2026-09-02 18:00:15", "Y-USDT-SWAP", "close",
                "long", 2.0, 20.0, 5.0, 8.0, 40.0, None,
                "exchange-reconciled close", None, None, 1.25,
                json.dumps(reconcile_raw),
            ),
            (
                CYCLE, "2026-09-02 18:01:00", "X-USDT-SWAP", "open",
                "long", 1.0, 10.0, 5.0, 2.0, 10.0, None,
                "runner interim fill", None, None, 0.0,
                json.dumps({
                    "ordId": "ORDER-RUNNER",
                    "ts_source": "fills.fillTime",
                    "fill_ts": "2026-09-02 18:01:00",
                    "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL,
                    decision_card.OPEN_EXECUTION_PACKAGE_KEY: package(),
                }),
            ),
        ]
        if ordinary_unmatched:
            rows.append((
                CYCLE, "2026-09-02 18:00:30", "Z-USDT-SWAP", "close",
                "short", 3.0, 30.0, 5.0, 18.0, 90.0, None,
                "ordinary unmatched row", None, None, -0.5,
                json.dumps({"ordId": "ORDER-ORDINARY"}),
            ))
        con = sqlite3.connect(db)
        try:
            con.execute(
                "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                (CYCLE, "2026-09-02 18:00:15", "live", "traded",
                 len(rows), 1000.0, "runner interim", json.dumps(old_raw)),
            )
            con.executemany(
                "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                "fill_px,lev,margin,notional,score_total,reasoning,"
                "deviation,degradation,pnl,raw) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            con.commit()
        finally:
            con.close()

    def _runner_final_receipt(self, **updates) -> dict:
        value = receipt(top_package=False)
        value["trades"][0]["ordId"] = "ORDER-RUNNER"
        value.update({
            "batch_status": "completed",
            "batch_ok": True,
            "runner_in_progress": False,
            "facts_hash": "a" * 64,
            "plan_sha256": "b" * 64,
            "position_action_plan_hash": "c" * 64,
            "business_terminal": {
                "schema_version": 1,
                "cycle_id": CYCLE,
                "status": "completed",
                "completed_at_cst": "2026-09-02 18:02:00",
            },
        })
        value.update(updates)
        return value

    def test_clean_hard_risk_reject_is_completed_no_order_outcome(self):
        action = {
            "action": "OPEN", "symbol": "ETH-USDT-SWAP", "side": "short",
        }
        rejected = {
            "ok": False,
            "action_taken": "REJECT",
            "p0": False,
            "reject_reason": "portfolio_margin_cap_exceeded",
            "reject_detail": "projected 70.75% > 66.6%",
            "trades": [],
            "position_reconciliation": {"ok": True},
            "risk": {
                "approved": False,
                "reject_reason": "portfolio_margin_cap_exceeded",
                "math": {"projected_portfolio_imr_ratio": 0.7075},
            },
        }
        with mock.patch.object(
            runner.thresholds,
            "minimal_contract_closure_active",
            return_value=True,
        ):
            self.assertTrue(runner._is_closure_clean_hard_reject(
                rejected, action, CYCLE))
        clean = dict(rejected)
        clean.update({
            "clean_hard_reject": True,
            "business_outcome": "completed_no_order_hard_reject",
        })
        aggregated = runner._aggregate_receipt(
            {
                "cycle_id": CYCLE,
                "mode": "live",
                "status": "ok",
                "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL,
                "reasoning": "hard gate decides",
                "position_reviews": [],
            },
            {"status": "ok"},
            plan_hash="a" * 64,
            requested=[action],
            successes=[{"request": action, "result": clean}],
            failures=[],
        )
        self.assertEqual("ok", aggregated["status"])
        self.assertEqual("hold", aggregated["decision"])
        self.assertEqual("HOLD", aggregated["action_taken"])
        self.assertEqual("completed", aggregated["batch_status"])
        self.assertTrue(aggregated["batch_ok"])
        self.assertEqual(0, aggregated["n_orders"])
        self.assertEqual([], aggregated["trades"])
        self.assertTrue(aggregated["position_action_results"][0][
            "result"]["clean_hard_reject"])

    def test_ambiguous_or_non_allowlisted_reject_remains_failure(self):
        action = {
            "action": "OPEN", "symbol": "ETH-USDT-SWAP", "side": "short",
        }
        base = {
            "ok": False, "action_taken": "REJECT", "p0": False,
            "trades": [], "position_reconciliation": {"ok": True},
            "risk": {"approved": False, "math": {}},
        }
        with mock.patch.object(
            runner.thresholds,
            "minimal_contract_closure_active",
            return_value=True,
        ):
            unsafe = dict(base)
            unsafe.update({
                "reject_reason": "place_ambiguous",
                "risk": {"approved": False,
                         "reject_reason": "place_ambiguous", "math": {}},
            })
            self.assertFalse(runner._is_closure_clean_hard_reject(
                unsafe, action, CYCLE))
            submitted = dict(base)
            submitted.update({
                "reject_reason": "portfolio_margin_cap_exceeded",
                "ordId": "should-never-exist",
                "risk": {"approved": False,
                         "reject_reason": "portfolio_margin_cap_exceeded",
                         "math": {}},
            })
            self.assertFalse(runner._is_closure_clean_hard_reject(
                submitted, action, CYCLE))

    def test_stale_stop_direction_is_clean_only_with_exact_pre_submit_proof(self):
        def rejected(side: str, mark: float, stop: float) -> dict:
            symbol = "MOVE-USDT-SWAP"
            return {
                "ok": False,
                "action_taken": "REJECT",
                "p0": False,
                "reject_reason": "sl_direction_invalid",
                "trades": [],
                "position_reconciliation": {"ok": True},
                "risk": {
                    "approved": False,
                    "reject_reason": "sl_direction_invalid",
                    "math": {
                        "symbol": symbol,
                        "side": side,
                        "mark_px": mark,
                        "sl_trigger_px": stop,
                    },
                },
            }

        cases = (
            ("long", 9.9, 10.0),
            ("short", 10.1, 10.0),
        )
        with mock.patch.object(
            runner.thresholds,
            "minimal_contract_closure_active",
            return_value=True,
        ):
            for side, mark, stop in cases:
                with self.subTest(side=side):
                    action = {
                        "action": "OPEN",
                        "symbol": "MOVE-USDT-SWAP",
                        "side": side,
                    }
                    result = rejected(side, mark, stop)
                    self.assertTrue(runner._is_closure_clean_hard_reject(
                        result, action, CYCLE))

                    valid_geometry = json.loads(json.dumps(result))
                    valid_geometry["risk"]["math"]["sl_trigger_px"] = (
                        mark - 0.1 if side == "long" else mark + 0.1)
                    self.assertFalse(runner._is_closure_clean_hard_reject(
                        valid_geometry, action, CYCLE))

                    submitted = json.loads(json.dumps(result))
                    submitted["ordId"] = "MUST-NOT-BE-CLEAN"
                    self.assertFalse(runner._is_closure_clean_hard_reject(
                        submitted, action, CYCLE))

                    mismatched = json.loads(json.dumps(result))
                    mismatched["risk"]["math"]["symbol"] = \
                        "OTHER-USDT-SWAP"
                    self.assertFalse(runner._is_closure_clean_hard_reject(
                        mismatched, action, CYCLE))

    def test_vanished_protection_target_is_clean_but_changed_position_is_not(self):
        action = {
            "action": "ADJUST_PROTECTION",
            "symbol": "GONE-USDT-SWAP",
            "pos_side": "long",
        }
        base = {
            "ok": False,
            "action_taken": "REJECT",
            "p0": False,
            "reject_reason": "pre_position_fingerprint_changed",
            "trades": [],
            "expected_pre_position": {
                "exists": True, "sz": 10.0, "posId": "P1", "cTime": "T1"},
            "actual_pre_position": None,
        }
        with mock.patch.object(
            runner.thresholds,
            "minimal_contract_closure_active",
            return_value=True,
        ):
            self.assertTrue(runner._is_closure_clean_hard_reject(
                base, action, CYCLE))
            changed = {
                **base,
                "actual_pre_position": {
                    "exists": True, "sz": 9.0, "posId": "P2"},
            }
            self.assertFalse(runner._is_closure_clean_hard_reject(
                changed, action, CYCLE))
            submitted = {**base, "ordId": "MUST-STOP"}
            self.assertFalse(runner._is_closure_clean_hard_reject(
                submitted, action, CYCLE))

    def test_runner_attaches_deterministic_takeover_proof_for_open(self):
        context = {"cycle_id": CYCLE}
        attestation = {
            "version": "actor_attestation_v3_lightweight_open",
            "cycle_id": CYCLE,
            "timeline": {"handoff_detected": True},
            "revalidation": {"all_ok": True},
            "attestation_hash": "sealed",
        }
        with mock.patch.object(
            runner.actor_att, "build_attestation",
            return_value=attestation,
        ) as build:
            runner._ensure_actor_attestation(
                context,
                {"action": "OPEN", "symbol": "X-USDT-SWAP"},
                CYCLE,
                Path("db"),
            )
        self.assertEqual(attestation, context["actor_attestation"])
        build.assert_called_once_with(CYCLE, db_root=Path("db"), stage="live")

        no_handoff = {"cycle_id": CYCLE}
        with mock.patch.object(
            runner.actor_att, "build_attestation",
            return_value={"timeline": {"handoff_detected": False}},
        ):
            runner._ensure_actor_attestation(
                no_handoff,
                {"action": "ADD", "symbol": "X-USDT-SWAP"},
                CYCLE,
                Path("db"),
            )
        self.assertNotIn("actor_attestation", no_handoff)

    def test_closure_open_signals_must_all_reach_plan_actions(self):
        required = {
            ("A-USDT-SWAP", "long"): "OPEN",
            ("B-USDT-SWAP", "short"): "ADD",
        }
        with self.assertRaisesRegex(
            runner.PlanError,
            "OPEN signals 必须逐项进入plan",
        ):
            runner._validate_closure_open_action_coverage(
                required,
                [{
                    "action": "OPEN", "symbol": "A-USDT-SWAP",
                    "side": "long",
                }],
            )
        runner._validate_closure_open_action_coverage(
            required,
            [{
                "action": "OPEN", "symbol": "A-USDT-SWAP", "side": "long",
            }, {
                "action": "ADD", "symbol": "B-USDT-SWAP", "side": "short",
            }],
        )
        with self.assertRaisesRegex(runner.PlanError, "extra_or_wrong"):
            runner._validate_closure_open_action_coverage(
                required,
                [{
                    "action": "OPEN", "symbol": "A-USDT-SWAP",
                    "side": "long",
                }, {
                    "action": "OPEN", "symbol": "B-USDT-SWAP",
                    "side": "short",
                }],
            )

    def test_same_position_allows_only_protection_plus_add_pair(self):
        key = ("ZEC-USDT-SWAP", "long")
        seen: dict[tuple[str, str], set[str]] = {}
        runner._record_position_action(seen, key, "ADJUST_PROTECTION")
        runner._record_position_action(seen, key, "ADD")
        self.assertEqual({"ADJUST_PROTECTION", "ADD"}, seen[key])

        for actions in (
            ("ADJUST_PROTECTION", "ADJUST_PROTECTION"),
            ("ADD", "ADD"),
            ("ADJUST_PROTECTION", "CLOSE"),
            ("REDUCE", "ADD"),
        ):
            with self.subTest(actions=actions):
                local: dict[tuple[str, str], set[str]] = {}
                runner._record_position_action(local, key, actions[0])
                with self.assertRaises(runner.PlanError):
                    runner._record_position_action(local, key, actions[1])
    def test_executor_accepts_machine_package_and_rejects_retired_card_key(self):
        context = {
            "cycle_id": CYCLE,
            "status": "ok",
            "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL,
            "reasoning": "OPEN X",
            decision_card.OPEN_EXECUTION_PACKAGE_KEY: package(),
        }
        p1, p2 = self.policy()
        with p1, p2:
            self.assertEqual([], order_executor.validate_receipt_context(
                context, cycle_id=CYCLE, expected_symbol="X-USDT-SWAP",
                expected_side="long"))
            invalid = {**context, "decision_card": package()}
            errors = order_executor.validate_receipt_context(
                invalid, cycle_id=CYCLE, expected_symbol="X-USDT-SWAP",
                expected_side="long")
            extra = {
                **context,
                decision_card.OPEN_EXECUTION_PACKAGE_KEY: {
                    **package(), "news_context": "legacy-extra"},
            }
            extra_errors = order_executor.validate_receipt_context(
                extra, cycle_id=CYCLE, expected_symbol="X-USDT-SWAP",
                expected_side="long")
            nested = {
                **context,
                "position_reviews": [{
                    "reason": "ordinary review",
                    "legacy": {
                        "decision_card": legacy_analysis_card(),
                        "review_hash": "f" * 64,
                    },
                }],
            }
            nested_errors = order_executor.validate_receipt_context(
                nested, cycle_id=CYCLE, expected_symbol="X-USDT-SWAP",
                expected_side="long")
            prose = {
                **context,
                "reasoning": (
                    "audit prose mentions decision_card, candidate_id and "
                    "lightweight_open_v1 without publishing machine keys"),
            }
            prose_errors = order_executor.validate_receipt_context(
                prose, cycle_id=CYCLE, expected_symbol="X-USDT-SWAP",
                expected_side="long")
        self.assertTrue(any("禁止携带顶层 decision_card" in x for x in errors))
        self.assertTrue(any("禁止额外字段" in x for x in extra_errors))
        self.assertTrue(any("退役机器结构" in x for x in nested_errors))
        self.assertEqual([], prose_errors)
        self.assert_exact_package(context[decision_card.OPEN_EXECUTION_PACKAGE_KEY])

    def test_closure_receipt_omits_mtf_audits_and_legacy_epoch_keeps_them(self):
        readiness = {"status": "REMOVED_BY_OWNER_POLICY"}
        anchor = {"status": "REMOVED_BY_OWNER_POLICY"}
        with mock.patch.object(
                order_executor.thresholds, "minimal_contract_closure_active",
                return_value=True):
            current = order_executor._multitimeframe_receipt_fields(
                CYCLE, readiness, anchor)
        with mock.patch.object(
                order_executor.thresholds, "minimal_contract_closure_active",
                return_value=False):
            legacy = order_executor._multitimeframe_receipt_fields(
                "2026-08-01T00:00", readiness, anchor)
        self.assertEqual({}, current)
        self.assertEqual(readiness, legacy["multitimeframe_readiness"])
        self.assertEqual(anchor, legacy["multitimeframe_evidence_anchor"])

    def test_writer_rejects_current_mtf_fields_and_scrubs_maintenance_raw(self):
        current = receipt(top_package=False)
        current["multitimeframe_readiness"] = {
            "status": "REMOVED_BY_OWNER_POLICY"}
        p1, p2 = self.policy()
        with p1, p2:
            errors = trades_writer.validate(current)
        self.assertTrue(any("禁止携带退役机器结构" in item for item in errors))

        nested = receipt(top_package=False)
        nested["position_reviews"] = [{
            "reason": "ordinary price review",
            "legacy": {
                "decision_card": legacy_analysis_card(),
                "candidate_id": "cand_obsolete",
                "opportunity_state": "ENTRY_READY",
            },
        }]
        p1, p2 = self.policy()
        with p1, p2:
            nested_errors = trades_writer.validate(nested)
        self.assertTrue(any(
            "禁止携带退役机器结构" in item for item in nested_errors))

        maintenance = receipt(top_package=False)
        maintenance["raw"] = {
            "keep": "yes",
            "multitimeframe_evidence_anchor": {"status": "removed"},
            "nested": {
                "timeframe_judgment_used": False,
                "decision_card": legacy_analysis_card(),
                "candidate_id": "cand_obsolete",
                "review_hash": "f" * 64,
                "opportunity_state": "ENTRY_READY",
                "open_execution_packages": [{
                    "symbol": "X-USDT-SWAP", "side": "long",
                    "open_execution_package": package(),
                }],
            },
        }
        maintenance["trades"][0]["raw"] = {
            "ordId": "closure-mtf-scrub",
            "multitimeframe_readiness": {"status": "removed"},
        }
        p1, p2 = self.policy()
        with tempfile.TemporaryDirectory() as tmp, p1, p2, \
                mock.patch.object(
                    trades_writer, "_analysis_context_for_cycle",
                    return_value={}):
            db = Path(tmp) / "live_trades.db"
            create_trade_db(db)
            result = trades_writer.maintenance_write_trades(
                maintenance, db,
                trusted_timestamp="2026-09-02 18:01:01",
                preserve_equity_none=True)
            self.assertTrue(result["ok"], result)
            con = sqlite3.connect(db)
            try:
                cycle_raw = json.loads(con.execute(
                    "SELECT raw FROM trade_cycles WHERE cycle_id=?", (CYCLE,)
                ).fetchone()[0])
                trade_raw = json.loads(con.execute(
                    "SELECT raw FROM trades WHERE cycle_id=?", (CYCLE,)
                ).fetchone()[0])
            finally:
                con.close()
        self.assertEqual([], trades_writer._closure_mtf_paths(cycle_raw))
        self.assertEqual([], trades_writer._closure_mtf_paths(trade_raw))
        self.assertEqual("yes", cycle_raw["keep"])
        self.assertEqual("closure-mtf-scrub", trade_raw["ordId"])

    def test_runner_action_context_and_writer_use_new_business_key(self):
        context = {
            "cycle_id": CYCLE,
            "status": "ok",
            "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL,
            "reasoning": "OPEN X",
            "regime": "range",
        }
        action = {
            "action": "OPEN",
            "_open_execution_package": package(),
        }
        p1, p2 = self.policy()
        with p1, p2, mock.patch.object(
                runner.thresholds, "minimal_contract_closure_active",
                return_value=True):
            action_context = runner._action_context(context, action)
            errors = trades_writer.validate(receipt())
        self.assertEqual([], errors)
        self.assertNotIn("decision_card", action_context)
        self.assertEqual(
            package(), action_context[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        self.assert_exact_package(
            action_context[decision_card.OPEN_EXECUTION_PACKAGE_KEY])

    def test_runner_converts_legacy_analysis_column_at_read_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "analysis.db")
            try:
                con.execute(
                    "CREATE TABLE analysis_signals(cycle_id TEXT,symbol TEXT,"
                    "action TEXT,side TEXT,reasoning TEXT,decision_card TEXT)")
                con.execute(
                    "INSERT INTO analysis_signals VALUES(?,?,?,?,?,?)",
                    (CYCLE, "X-USDT-SWAP", "open_long", "long",
                     "canonical analysis reasoning",
                     json.dumps(legacy_analysis_card())))
                con.commit()
            finally:
                con.close()
            with mock.patch.object(
                    runner.thresholds, "minimal_contract_closure_active",
                    return_value=True):
                signal = runner._load_analysis_signal(
                    root, CYCLE, "X-USDT-SWAP")
        self.assertNotIn("decision_card", signal)
        self.assertEqual("canonical analysis reasoning", signal["reasoning"])
        converted = signal[decision_card.OPEN_EXECUTION_PACKAGE_KEY]
        self.assertEqual(package(), converted)
        self.assert_exact_package(converted)

    def test_successful_open_is_not_relabelled_partial_by_result_validation(self):
        result = receipt()
        result.update({"action_taken": "OPEN_LONG", "is_add": False})
        action = {"action": "OPEN", "symbol": "X-USDT-SWAP", "side": "long"}
        p1, p2 = self.policy()
        with p1, p2, mock.patch.object(
                runner.tw, "validate_strict_live_receipt", return_value=[]), \
                mock.patch.object(
                    runner.tw, "validate_live_facts", return_value=[]):
            problem = runner._result_problem(result, action, {})
        self.assertIsNone(problem)
        aggregate = runner._aggregate_receipt(
            {
                "cycle_id": CYCLE,
                "status": "ok",
                "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL,
                "reasoning": "OPEN X",
            },
            {}, plan_hash="a" * 64, requested=[action],
            successes=[{"request": action, "result": result}], failures=[])
        self.assertEqual("completed", aggregate["batch_status"])
        self.assertTrue(aggregate["batch_ok"])
        self.assertNotIn("decision_card", aggregate)
        self.assertEqual(
            package(), aggregate["trades"][0][
                decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        self.assert_exact_package(
            aggregate["trades"][0][
                decision_card.OPEN_EXECUTION_PACKAGE_KEY])

    def test_legacy_trade_key_is_rejected_after_closure(self):
        payload = receipt(top_package=False)
        payload["trades"][0]["decision_card"] = \
            payload["trades"][0].pop(
                decision_card.OPEN_EXECUTION_PACKAGE_KEY)
        p1, p2 = self.policy()
        with p1, p2:
            errors = trades_writer.validate(payload)
        self.assertTrue(any("禁止使用 decision_card 键" in x for x in errors))
        self.assertTrue(any("open_execution_package" in x for x in errors))

    def test_trade_ledger_raw_and_push_reader_keep_only_new_key(self):
        payload = receipt(top_package=False)
        p1, p2 = self.policy()
        with tempfile.TemporaryDirectory() as tmp, p1, p2, \
                mock.patch.object(
                    trades_writer, "_analysis_context_for_cycle",
                    return_value={}):
            db = Path(tmp) / "live_trades.db"
            create_trade_db(db)
            result = trades_writer.maintenance_write_trades(
                payload, db,
                trusted_timestamp="2026-09-02 18:01:01",
                preserve_equity_none=True)
            self.assertTrue(result["ok"], result)
            con = sqlite3.connect(db)
            try:
                raw = json.loads(con.execute(
                    "SELECT raw FROM trades WHERE cycle_id=?", (CYCLE,)
                ).fetchone()[0])
            finally:
                con.close()
            decisions = build_push_payload._open_trade_decisions(
                [{**trade(), "raw": raw}], closure_policy=True)
        self.assertNotIn("decision_card", raw)
        self.assertEqual(
            package(), raw[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        self.assert_exact_package(raw[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        self.assertEqual(
            package(),
            decisions[0][decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        push_decision = build_push_payload._closure_execution_package_payload(
            package())
        self.assertEqual(
            {decision_card.OPEN_EXECUTION_PACKAGE_KEY}, set(push_decision))
        self.assertNotIn("open_execution_packages", push_decision)
        self.assert_exact_package(
            push_decision[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        self.assertEqual(
            {},
            build_push_payload._closure_execution_package_payload({
                **package(), "side": "long"}),
        )

    def test_closure_merge_keep_scrubs_only_raw_and_preserves_trade_facts(self):
        polluted_raw = {
            "ordId": "prior-order-1",
            "decision_card": legacy_analysis_card(),
            "nested": {
                "candidate_id": "cand_obsolete",
                "review_hash": "f" * 64,
                "multitimeframe_readiness": {"status": "removed"},
            },
            "keep": {"exchange": "confirmed"},
        }
        p1, p2 = self.policy()
        with tempfile.TemporaryDirectory() as tmp, p1, p2, \
                mock.patch.object(
                    trades_writer, "_analysis_context_for_cycle",
                    return_value={}):
            db = Path(tmp) / "live_trades.db"
            create_trade_db(db)
            con = sqlite3.connect(db)
            try:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (CYCLE, "2026-09-02 18:00:30", "live", "traded", 1,
                     1000.0, "prior", json.dumps({"status": "ok"})))
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                    "fill_px,lev,margin,notional,score_total,reasoning,"
                    "deviation,degradation,pnl,raw) VALUES(?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?,?,?)",
                    (CYCLE, "2026-09-02 18:00:30", "Y-USDT-SWAP", "open",
                     "long", 2.0, 20.0, 5.0, 8.0, 40.0, None,
                     "prior confirmed fill", None, None, 0.0,
                     json.dumps(polluted_raw)))
                con.commit()
            finally:
                con.close()
            incoming = receipt(top_package=False)
            incoming["trades"][0]["ordId"] = "new-order-2"
            result = trades_writer.maintenance_write_trades(
                incoming, db,
                trusted_timestamp="2026-09-02 18:01:01",
                preserve_equity_none=True)
            self.assertTrue(result["ok"], result)
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT symbol,action,side,sz,fill_px,lev,margin,notional,"
                    "reasoning,pnl,raw FROM trades WHERE cycle_id=? "
                    "ORDER BY symbol", (CYCLE,)).fetchall()
            finally:
                con.close()
        self.assertEqual(2, len(rows))
        prior = next(row for row in rows if row["symbol"] == "Y-USDT-SWAP")
        self.assertEqual(
            ("open", "long", 2.0, 20.0, 5.0, 8.0, 40.0,
             "prior confirmed fill", 0.0),
            tuple(prior[key] for key in (
                "action", "side", "sz", "fill_px", "lev", "margin",
                "notional", "reasoning", "pnl")))
        cleaned = json.loads(prior["raw"])
        self.assertEqual("prior-order-1", cleaned["ordId"])
        self.assertEqual({"exchange": "confirmed"}, cleaned["keep"])
        self.assertEqual([], trades_writer._closure_mtf_paths(cleaned))

    def test_runner_finalization_keeps_only_unmatched_reconciled_close(self):
        p1, p2 = self.policy()
        with tempfile.TemporaryDirectory() as tmp, p1, p2, \
                mock.patch.object(
                    trades_writer, "_analysis_context_for_cycle",
                    return_value={}):
            db = Path(tmp) / "live_trades.db"
            create_trade_db(db)
            self._seed_runner_partial_with_reconcile(db)

            result = trades_writer.write_trades(
                self._runner_final_receipt(), db)

            self.assertTrue(result["ok"], result)
            self.assertFalse(result.get("refused"), result)
            self.assertEqual([], result["new_trades"])
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                header = con.execute(
                    "SELECT decision,n_orders,raw FROM trade_cycles "
                    "WHERE cycle_id=?", (CYCLE,),
                ).fetchone()
                rows = con.execute(
                    "SELECT symbol,action,side,sz,fill_px,raw FROM trades "
                    "WHERE cycle_id=? ORDER BY symbol", (CYCLE,),
                ).fetchall()
            finally:
                con.close()

        self.assertEqual(("traded", 2),
                         (header["decision"], header["n_orders"]))
        raw = json.loads(header["raw"])
        self.assertEqual("completed", raw["batch_status"])
        self.assertIs(raw["runner_in_progress"], False)
        self.assertEqual(2, raw["n_orders"])
        self.assertTrue(raw["reconciled_close_preserved"])
        self.assertTrue(raw["runner_finalization_reconcile_merge"])
        self.assertEqual(1, raw["merge_guard_kept_rows"])
        self.assertEqual(
            [("X-USDT-SWAP", "open"), ("Y-USDT-SWAP", "close")],
            [(row["symbol"], row["action"]) for row in rows],
        )
        retained = json.loads(next(
            row["raw"] for row in rows if row["symbol"] == "Y-USDT-SWAP"))
        self.assertEqual(
            "exchange_fills_reconcile", retained["reconcile_source"])
        self.assertEqual(["ORDER-RECONCILE"], retained["ord_ids"])

    def test_runner_interim_progression_keeps_reconcile_and_adds_new_fill(self):
        p1, p2 = self.policy()
        with tempfile.TemporaryDirectory() as tmp, p1, p2, \
                mock.patch.object(
                    trades_writer, "_analysis_context_for_cycle",
                    return_value={}):
            db = Path(tmp) / "live_trades.db"
            create_trade_db(db)
            self._seed_runner_partial_with_reconcile(db)
            incoming = self._runner_final_receipt()
            incoming.update({
                "batch_status": "partial",
                "runner_in_progress": True,
            })
            incoming.pop("business_terminal")
            extra = trade()
            extra.update({
                "symbol": "Z-USDT-SWAP",
                "ordId": "ORDER-RUNNER-SECOND",
            })
            incoming["trades"].append(extra)
            incoming["n_orders"] = 2

            result = trades_writer.write_trades(incoming, db)

            self.assertTrue(result["ok"], result)
            self.assertFalse(result.get("refused"), result)
            self.assertEqual(
                ["Z-USDT-SWAP"],
                [item["symbol"] for item in result["new_trades"]],
            )
            con = sqlite3.connect(db)
            try:
                header = con.execute(
                    "SELECT n_orders,raw FROM trade_cycles WHERE cycle_id=?",
                    (CYCLE,),
                ).fetchone()
                symbols = [row[0] for row in con.execute(
                    "SELECT symbol FROM trades WHERE cycle_id=? "
                    "ORDER BY symbol", (CYCLE,)).fetchall()]
            finally:
                con.close()
        raw = json.loads(header[1])
        self.assertEqual(3, header[0])
        self.assertEqual(
            ["X-USDT-SWAP", "Y-USDT-SWAP", "Z-USDT-SWAP"], symbols)
        self.assertEqual("partial", raw["batch_status"])
        self.assertTrue(raw["runner_in_progress"])
        self.assertTrue(raw["runner_interim_reconcile_merge"])
        self.assertNotIn("runner_finalization_reconcile_merge", raw)

    def test_runner_interim_accepts_verified_exchange_fill_timestamp(self):
        """A real maintenance close carries fills.fillTime after normalization."""
        p1, p2 = self.policy()
        with tempfile.TemporaryDirectory() as tmp, p1, p2, \
                mock.patch.object(trades_writer, "_analysis_context_for_cycle", return_value={}):
            db = Path(tmp) / "live_trades.db"
            create_trade_db(db)
            self._seed_runner_partial_with_reconcile(db, reconcile_raw_updates={
                "ts_source": "fills.fillTime",
                "fill_ts": "2026-09-02 18:00:15",
            })
            incoming = self._runner_final_receipt()
            incoming.update(batch_status="partial", runner_in_progress=True)
            incoming.pop("business_terminal")
            extra = trade()
            extra.update(symbol="Z-USDT-SWAP", ordId="ORDER-RUNNER-SECOND")
            incoming["trades"].append(extra)
            incoming["n_orders"] = 2
            result = trades_writer.write_trades(incoming, db)
            self.assertTrue(result["ok"], result)
            self.assertEqual(["Z-USDT-SWAP"], [t["symbol"] for t in result["new_trades"]])
            with closing(sqlite3.connect(db)) as con:
                rows = con.execute("SELECT symbol,ts,sz,fill_px,raw FROM trades ORDER BY symbol").fetchall()
            self.assertEqual(3, len(rows))
            retained = next(r for r in rows if r[0] == "Y-USDT-SWAP")
            self.assertEqual(("2026-09-02 18:00:15", 2.0, 20.0), retained[1:4])
            self.assertEqual("fills.fillTime", json.loads(retained[4])["ts_source"])

    def test_runner_reconcile_fill_timestamp_conflicts_remain_blocked(self):
        cases = [
            {"fill_ts": None},
            {"fill_ts": "2026-09-02 18:00:16"},
            {"close_ts": "not-a-time"},
            {"ts_source": "writer_commit_fallback"},
            {"fills": [{"ts": "2026-09-02 18:00:15", "px": "20", "sz": "3", "ordId": "ORDER-RECONCILE"}]},
            {"fills": [{"ts": "2026-09-02 18:00:14", "px": "20", "sz": "2", "ordId": "ORDER-RECONCILE"}]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                p1, p2 = self.policy()
                with tempfile.TemporaryDirectory() as tmp, p1, p2, \
                        mock.patch.object(trades_writer, "_analysis_context_for_cycle", return_value={}):
                    db = Path(tmp) / "live_trades.db"
                    create_trade_db(db)
                    self._seed_runner_partial_with_reconcile(db, reconcile_raw_updates={
                        "ts_source": "fills.fillTime", "fill_ts": "2026-09-02 18:00:15", **changes,
                    })
                    result = trades_writer.write_trades(self._runner_final_receipt(), db)
                    self.assertEqual("ambiguous_merge", result.get("refused"))
                    with closing(sqlite3.connect(db)) as con:
                        self.assertEqual(2, con.execute("SELECT count(*) FROM trades").fetchone()[0])

    def test_reconcile_during_runner_preserves_progression_for_later_fill(self):
        """A maintenance close must not erase an active runner's binding.

        Production can observe this order: runner fill A -> exchange reconcile
        close B -> runner fill C.  The final runner receipt contains A+C while
        the database contains A+B.  Keeping the proven partial-runner context
        lets the writer retain B and append C without treating that overlap as
        an ambiguous resend.
        """
        p1, p2 = self.policy()
        with tempfile.TemporaryDirectory() as tmp, p1, p2, \
                mock.patch.object(
                    trades_writer, "_analysis_context_for_cycle",
                    return_value={}):
            db = Path(tmp) / "live_trades.db"
            create_trade_db(db)

            interim = self._runner_final_receipt()
            interim.update({
                "batch_status": "partial",
                "batch_ok": True,
                "runner_in_progress": True,
            })
            interim.pop("business_terminal")
            first = trades_writer.write_trades(interim, db)
            self.assertTrue(first["ok"], first)

            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                old_raw = json.loads(con.execute(
                    "SELECT raw FROM trade_cycles WHERE cycle_id=?", (CYCLE,),
                ).fetchone()[0])
                prior = dict(con.execute(
                    "SELECT ts,symbol,action,side,sz,fill_px,lev,margin,"
                    "notional,score_total,reasoning,deviation,degradation,pnl,raw "
                    "FROM trades WHERE cycle_id=?", (CYCLE,),
                ).fetchone())
            finally:
                con.close()

            reconcile_raw = {
                "reconcile_source": "exchange_fills_reconcile",
                "reconciled_at": "2026-09-02 18:01:15",
                "symbol": "Y-USDT-SWAP",
                "side": "long",
                "close_ts": "2026-09-02 18:01:15",
                "ord_ids": ["ORDER-RECONCILE"],
                "fills": [{
                    "ts": "2026-09-02 18:01:15",
                    "px": "20",
                    "sz": "2",
                    "pnl": "1.25",
                    "ordId": "ORDER-RECONCILE",
                    "tradeId": "TRADE-RECONCILE",
                }],
            }
            reconcile_raw.update(
                reconcile._report_business_context(old_raw, CYCLE))
            reconcile_raw.update({"decision": "traded", "n_orders": 2})
            reconciled_close = {
                "symbol": "Y-USDT-SWAP",
                "action": "close",
                "side": "long",
                "sz": 2.0,
                "fill_px": 20.0,
                "lev": 5.0,
                "margin": 8.0,
                "notional": 40.0,
                "reasoning": "exchange-reconciled close",
                "pnl": 1.25,
                "raw": {
                    "reconcile_source": "exchange_fills_reconcile",
                    "ts_source": "trusted_internal_override",
                    "close_ts": "2026-09-02 18:01:15",
                    "ord_ids": ["ORDER-RECONCILE"],
                    "fills": reconcile_raw["fills"],
                },
            }
            maintenance = {
                "cycle_id": CYCLE,
                "mode": "live",
                "decision": "traded",
                "n_orders": 2,
                "trades": [prior, reconciled_close],
                "raw": reconcile_raw,
                "_profile": "live",
            }
            merged = trades_writer.maintenance_write_trades(
                maintenance,
                db,
                trusted_timestamp="2026-09-02 18:01:15",
                preserve_equity_none=True,
            )
            self.assertTrue(merged["ok"], merged)

            final = self._runner_final_receipt()
            extra = trade()
            extra.update({
                "symbol": "Z-USDT-SWAP",
                "ordId": "ORDER-RUNNER-SECOND",
            })
            final["trades"].append(extra)
            final["n_orders"] = 2
            result = trades_writer.write_trades(final, db)

            self.assertTrue(result["ok"], result)
            self.assertFalse(result.get("refused"), result)
            self.assertEqual(
                ["Z-USDT-SWAP"],
                [item["symbol"] for item in result["new_trades"]],
            )
            con = sqlite3.connect(db)
            try:
                header = con.execute(
                    "SELECT n_orders,raw FROM trade_cycles WHERE cycle_id=?",
                    (CYCLE,),
                ).fetchone()
                rows = con.execute(
                    "SELECT symbol,action FROM trades WHERE cycle_id=? "
                    "ORDER BY symbol", (CYCLE,),
                ).fetchall()
            finally:
                con.close()

        raw = json.loads(header[1])
        self.assertEqual(3, header[0])
        self.assertEqual(
            [("X-USDT-SWAP", "open"),
             ("Y-USDT-SWAP", "close"),
             ("Z-USDT-SWAP", "open")],
            rows,
        )
        self.assertEqual("completed", raw["batch_status"])
        self.assertTrue(raw["runner_finalization_reconcile_merge"])

    def test_runner_finalization_partial_overlap_remains_fail_closed(self):
        cases = (
            ({"facts_hash": "d" * 64}, {}, False, "binding mismatch"),
            ({"batch_status": "completed", "runner_in_progress": False},
             {}, False, "old row already terminal"),
            ({}, {"ts_source": "writer_commit_fallback"}, False,
             "untrusted reconcile provenance"),
            ({}, {}, True, "ordinary unmatched row"),
        )
        for old_updates, reconcile_updates, ordinary, label in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "live_trades.db"
                create_trade_db(db)
                self._seed_runner_partial_with_reconcile(
                    db,
                    old_raw_updates=old_updates,
                    reconcile_raw_updates=reconcile_updates,
                    ordinary_unmatched=ordinary,
                )
                p1, p2 = self.policy()
                with p1, p2, mock.patch.object(
                        trades_writer, "_analysis_context_for_cycle",
                        return_value={}):
                    result = trades_writer.write_trades(
                        self._runner_final_receipt(), db)
                self.assertFalse(result["ok"], (label, result))
                self.assertEqual("ambiguous_merge", result["refused"])
                con = sqlite3.connect(db)
                try:
                    count = con.execute(
                        "SELECT COUNT(*) FROM trades WHERE cycle_id=?",
                        (CYCLE,),
                    ).fetchone()[0]
                    raw = json.loads(con.execute(
                        "SELECT raw FROM trade_cycles WHERE cycle_id=?",
                        (CYCLE,),
                    ).fetchone()[0])
                finally:
                    con.close()
                self.assertEqual(3 if ordinary else 2, count)
                self.assertEqual(
                    old_updates.get("batch_status", "partial"),
                    raw["batch_status"],
                )

    def test_unrecorded_reconcile_emits_new_key_but_keeps_history_hook(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY,ts TEXT,"
            "decision TEXT,n_orders INTEGER,equity REAL,note TEXT,raw TEXT)")
        con.execute(
            "CREATE TABLE trades(symbol TEXT,action TEXT,side TEXT,sz REAL,"
            "fill_px REAL,lev REAL,margin REAL,notional REAL,score_total REAL,"
            "reasoning TEXT,deviation TEXT,degradation TEXT,pnl REAL,"
            "raw TEXT,cycle_id TEXT,ts TEXT)")
        captured: dict = {}
        history: dict = {}

        def fake_write(data, *_args, **_kwargs):
            captured.update(data)
            return {"ok": True, "refused": None}

        def fake_history(data, *_args, **_kwargs):
            history.update(data)
            return {"exp": 1}

        matched = [{
            "ordId": "confirmed-open",
            "fills": [{
                "fillTime": "1788343260000", "fillPx": "10",
                "fillSz": "1", "ordId": "confirmed-open",
                "tradeId": "trade-1", "execType": "T",
            }],
        }]
        p1, p2 = self.policy()
        try:
            with p1, p2, \
                    mock.patch.object(
                        reconcile.trades_writer, "maintenance_write_trades",
                        side_effect=fake_write), \
                    mock.patch.object(
                        reconcile.trades_writer, "write_experiences",
                        side_effect=fake_history):
                reconcile.apply_unrecorded(
                    Path("unused.db"), "live", "X-USDT-SWAP", "long",
                    1.0, matched, con, lev=5.0, card=package(),
                    intent={"cycle_id": CYCLE, "ord_id": "confirmed-open"},
                    sl_probe={"has_sl": True})
        finally:
            con.close()
        row = captured["trades"][0]
        self.assertNotIn("decision_card", row)
        self.assertEqual(
            package(), row[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        self.assert_exact_package(row[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        self.assertEqual(row, history["trades"][0])

    def test_reconcile_report_context_preserves_package_without_legacy_card(self):
        raw = {
            "decision_protocol": "decision_card_v1",
            "decision_card": {"agent_judgement": "retired six-field card"},
            decision_card.OPEN_EXECUTION_PACKAGE_KEY: package(),
            "reasoning": "exchange reconciliation",
            "multitimeframe_readiness": {"status": "removed"},
            "nested": {"timeframe_judgment_used": False},
        }
        p1, p2 = self.policy()
        with p1, p2:
            context = reconcile._report_business_context(raw, CYCLE)
        self.assertEqual(
            decision_card.MINIMAL_DECISION_PROTOCOL,
            context["decision_protocol"])
        self.assertNotIn("decision_card", context)
        self.assertEqual(
            package(), context[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        self.assert_exact_package(
            context[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        self.assertEqual([], trades_writer._closure_mtf_paths(context))


if __name__ == "__main__":
    unittest.main()
