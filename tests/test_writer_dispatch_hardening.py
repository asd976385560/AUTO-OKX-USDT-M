from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest import mock

from collectors import analyst_writer, trades_writer
from core import dispatcher
from core import multitimeframe_gate as mtf_gate
from core.experience_contract import build_contract, setup_from_prices
from core.instrument_context import build_instrument_context


CST = timezone(timedelta(hours=8))


class AnalysisValidationBudgetTests(unittest.TestCase):
    def test_analysis_deadline_is_strict_at_cycle_plus_nine_thirty(self):
        cycle = "2026-08-15T21:45"
        before = datetime(2026, 8, 15, 21, 54, 29, tzinfo=CST)
        boundary = datetime(2026, 8, 15, 21, 54, 30, tzinfo=CST)
        self.assertIsNone(
            analyst_writer.analysis_deadline_refusal(cycle, now=before))
        refused = analyst_writer.analysis_deadline_refusal(
            cycle, now=boundary)
        self.assertEqual("analysis_deadline_exceeded", refused["error"])
        self.assertEqual(0, refused["production_database_writes"])
        self.assertIsNone(analyst_writer.analysis_deadline_refusal(
            "2026-08-15T21:30", now=boundary))

    def test_two_failures_block_cycle_deterministically(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    analyst_writer, "VALIDATION_STATE_DIR", Path(tmp)):
            cycle = "2026-08-11T18:30"
            first = analyst_writer._record_validation_failure(
                cycle, "hash-1", ["first error"])
            self.assertEqual(first["failed_attempts"], 1)
            self.assertFalse(first["blocked"])
            error, _ = analyst_writer._validation_guard(
                cycle, "hash-2", validate_only=True)
            self.assertIsNone(error)

            second = analyst_writer._record_validation_failure(
                cycle, "hash-2", ["second error"])
            self.assertEqual(second["failed_attempts"], 2)
            self.assertTrue(second["blocked"])
            for validate_only in (True, False):
                error, state = analyst_writer._validation_guard(
                    cycle, "hash-3", validate_only=validate_only)
                self.assertIn("budget exhausted", error)
                self.assertTrue(state["blocked"])

    def test_formal_write_must_match_validated_payload(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    analyst_writer, "VALIDATION_STATE_DIR", Path(tmp)):
            cycle = "2026-08-11T18:45"
            state = analyst_writer._record_validation_success(cycle, "same")
            self.assertEqual(state["validated_payload_sha256"], "same")
            error, _ = analyst_writer._validation_guard(
                cycle, "same", validate_only=False)
            self.assertIsNone(error)
            error, _ = analyst_writer._validation_guard(
                cycle, "changed", validate_only=False)
            self.assertIn("不一致", error)
            error, _ = analyst_writer._validation_guard(
                cycle, "changed", validate_only=True)
            self.assertIn("禁止通过后再次改写", error)


def _market_evidence_contract(cycle_id: str, symbol: str) -> dict:
    cycle = mtf_gate.parse_cycle_cst(cycle_id)
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
    cycle_id: str, side: str, symbol: str,
) -> dict:
    return {
        "cycle_id": cycle_id,
        "required_timeframes": ["15m", "1H", "4H"],
        "timeframes": {
            "15m": {"direction": side, "evidence": ["15m"],
                    "relative_rank": 2},
            "1H": {"direction": "neutral", "evidence": ["1H"],
                   "relative_rank": 3},
            "4H": {"direction": side, "evidence": ["4H"],
                   "relative_rank": 1},
        },
        "selected_timeframe": "4H",
        "selected_direction": side,
        "selection_reason": "isolated relative selection",
        "selection_method": (
            "relative_rank_1_among_15m_1H_4H_not_calibrated"),
        "calibrated_confidence": None,
        "confidence_claim_allowed": False,
        "evidence_contract": _market_evidence_contract(cycle_id, symbol),
    }


def _valid_card(label: str = "isolated test") -> dict:
    return {
        "direction_evidence": [label],
        "opposing_evidence": ["counter"],
        "execution_conditions": {"status": "ready"},
        "invalidation_point": {"condition": "invalidated"},
        # Wave1 序5 起 open 卡须含数值 entry/stop/target（writer 重算 RR/EV）
        "risk_reward": {"entry": 100.0, "stop": 97.0, "target": 106.0,
                        "rr": 2.0, "summary": "bounded",
                        "exit_mode": "fixed_tp"},
        "portfolio_impact": {"summary": "isolated"},
        "historical_experience": {
            "matched_wins": [],
            "matched_losses": [],
            "missed_opportunities": [],
            "usage": "none",
            "reason": "no comparable sample",
        },
        "agent_judgement": label,
        "reference_overrides": [],
    }


def _experience_contract(cycle_id: str, symbol: str, side: str,
                         regime: str = "", db_root: Path | None = None) -> dict:
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

    setup = {"stop_distance_pct": 0.03, "planned_rr": 2.0}
    setup["setup_hash"] = hashlib.sha256(json.dumps(
        setup, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    query = {
            "symbol": symbol,
            "side": side,
            "regime": regime,
            "action": "open",
            "profile": "live",
            "as_of": cycle_id.replace("T", " ") + ":00",
            "min_sim": 0.5,
            "top_k": 8,
            "setup": setup,
            "instrument_context": build_instrument_context(
                symbol, regime, cycle_id, db_root or Path(".")),
        }
    return build_contract(
        query,
        exact_setup=summary("same_symbol_side_action_regime"),
        same_symbol_similar=summary("same_symbol_similar"),
        cross_symbol_similar=summary("cross_symbol_similar"),
    )


class CanonicalSetupGeometryTests(unittest.TestCase):
    def test_writer_accepts_same_price_geometry_sealed_by_tool(self):
        setup = setup_from_prices(0.0698, 0.0705, 0.0685)
        contract = {"query": {"setup": setup}}
        card = {
            "risk_reward": {
                "entry": 0.0698,
                "stop": 0.0705,
                "target": 0.0685,
            }
        }

        self.assertEqual(
            analyst_writer._validate_setup_contract(card, contract), [])

    def test_writer_still_rejects_percent_unit_or_tampered_hash(self):
        setup = setup_from_prices(0.0698, 0.0705, 0.0685)
        setup["stop_distance_pct"] = 1.002865
        contract = {"query": {"setup": setup}}
        card = {
            "risk_reward": {
                "entry": 0.0698,
                "stop": 0.0705,
                "target": 0.0685,
            }
        }

        errors = analyst_writer._validate_setup_contract(card, contract)
        self.assertTrue(any("stop_distance_pct" in error for error in errors))

        setup = setup_from_prices(0.0698, 0.0705, 0.0685)
        setup["setup_hash"] = "tampered"
        errors = analyst_writer._validate_setup_contract(
            card, {"query": {"setup": setup}})
        self.assertTrue(any("setup_hash" in error for error in errors))


def _card_with_contract(cycle_id: str, symbol: str, side: str,
                        regime: str = "", db_root: Path | None = None) -> dict:
    card = _valid_card()
    card["multitimeframe_analysis"] = _multitimeframe_analysis(
        cycle_id, side, symbol)
    card["historical_experience"]["evidence_contract"] = (
        _experience_contract(cycle_id, symbol, side, regime, db_root)
    )
    return card


def _create_analysis_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.executescript(
            """
            CREATE TABLE analysis_runs(
                cycle_id TEXT PRIMARY KEY,
                ts TEXT,
                mode TEXT,
                regime TEXT,
                regime_stale INTEGER,
                market_summary TEXT,
                missing_sources TEXT,
                raw TEXT,
                status TEXT
            );
            CREATE TABLE analysis_signals(
                cycle_id TEXT,
                symbol TEXT,
                dim1 REAL,
                dim2 REAL,
                dim3 REAL,
                dim4 REAL,
                dim5 REAL,
                total REAL,
                action TEXT,
                side TEXT,
                confidence REAL,
                entry_hint REAL,
                stop_hint REAL,
                tp_hint REAL,
                reasoning TEXT,
                decision_card TEXT,
                raw TEXT
            );
            """
        )


def _create_market_db(path: Path, cycle_id: str,
                      symbol: str = "BTC-USDT-SWAP") -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute(
            "CREATE TABLE derivatives("
            "ts TEXT,symbol TEXT,funding_rate REAL)"
        )
        con.execute(
            "CREATE TABLE kline_cache("
            "ts TEXT,symbol TEXT,tf TEXT,o REAL,h REAL,l REAL,c REAL,v REAL,"
            "ma5 REAL,ma20 REAL,atr14 REAL,rsi14 REAL,macd_hist REAL,"
            "PRIMARY KEY(ts,symbol,tf))"
        )
        cycle = mtf_gate.parse_cycle_cst(cycle_id)
        for timeframe, seconds in mtf_gate.TIMEFRAME_SECONDS.items():
            expected = datetime.fromisoformat(
                mtf_gate.expected_closed_bar_start(cycle, timeframe).replace(
                    "Z", "+00:00"))
            rows = []
            for offset in range(
                    mtf_gate.MINIMUM_BARS_FOR_FULL_INDICATORS - 1, -1, -1):
                ts = (expected - timedelta(seconds=seconds * offset)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                rows.append((
                    ts, symbol, timeframe,
                    100.0, 102.0, 99.0, 101.0, 1000.0,
                    100.0, 99.0, 2.0, 55.0, 0.5,
                ))
            con.executemany(
                "INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        con.commit()


def _create_trade_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.executescript(
            """
            CREATE TABLE trade_cycles(
                cycle_id TEXT PRIMARY KEY,
                ts TEXT,
                mode TEXT,
                decision TEXT,
                n_orders INTEGER,
                equity REAL,
                note TEXT,
                raw TEXT
            );
            CREATE TABLE trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT,
                ts TEXT,
                symbol TEXT,
                action TEXT,
                side TEXT,
                sz REAL,
                fill_px REAL,
                lev REAL,
                margin REAL,
                notional REAL,
                score_total REAL,
                reasoning TEXT,
                deviation TEXT,
                degradation TEXT,
                pnl REAL,
                raw TEXT
            );
            """
        )


def _write_trade_cycle(path: Path, cycle: str, decision: str,
                       n_orders: int) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute(
            "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
            (cycle, "2026-07-29 12:01:00", "full", decision, n_orders,
             None, "", "{}"),
        )
        con.commit()


class AnalystWriterHardeningTests(unittest.TestCase):
    def _open_payload(self, root: Path, *, side: object = "long") -> dict:
        cycle = "2026-07-29T14:00"
        symbol = "BTC-USDT-SWAP"
        _create_market_db(root / "market.db", cycle)
        return {
            "cycle_id": cycle,
            "ts": "2026-07-29 14:01:00",
            "mode": "full",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "regime": "",
            "market_summary": {
                name: {} for name in analyst_writer.MARKET_SUMMARY_SECTIONS
            },
            "signals": [{
                "symbol": symbol,
                "action": "open_long",
                "side": side,
                "decision_card": _card_with_contract(
                    cycle, symbol, "long", db_root=root),
            }],
        }

    def test_missing_open_side_is_losslessly_inferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._open_payload(root)
            del payload["signals"][0]["side"]
            normalized = analyst_writer.normalize_receipt(payload)
            with mock.patch.object(
                    analyst_writer, "DB_PATH", root / "analysis.db"):
                errors = analyst_writer.validate_receipt(
                    payload, db_root=root)
        self.assertEqual("long", normalized["signals"][0]["side"])
        self.assertEqual([], errors)

    @staticmethod
    def _deadline_refusal(cycle_id: str) -> dict:
        return {
            "ok": False,
            "refused": True,
            "error": "analysis_deadline_exceeded",
            "cycle_id": cycle_id,
            "deadline_at": "2026-08-15 23:09:30",
            "checked_at": "2026-08-15 23:09:30",
            "production_database_writes": 0,
        }

    def test_lock_wait_crossing_deadline_refuses_before_first_business_write(
            self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "analysis.db"
            _create_analysis_db(db)
            cycle = "2026-08-15T23:00"
            payload = {
                "cycle_id": cycle,
                "ts": "2026-08-15 23:01:00",
                "mode": "full",
                "status": "skipped",
                "decision_protocol": "decision_card_v1",
                "market_summary": None,
                "signals": [],
            }
            begin_started = Event()
            checks = 0

            def deadline_side_effect(_cycle_id):
                nonlocal checks
                checks += 1
                # Entry + post-validation checks are timely.  The third check
                # runs only after BEGIN IMMEDIATE has waited for this test's
                # real SQLite writer lock and must observe the crossed cutoff.
                return (None if checks < 3
                        else self._deadline_refusal(cycle))

            class _BeginNotifyingConnection:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, sql, parameters=()):
                    if str(sql).strip().upper() == "BEGIN IMMEDIATE":
                        begin_started.set()
                    return self._inner.execute(sql, parameters)

                def __getattr__(self, name):
                    return getattr(self._inner, name)

            def notifying_connect(write=False, db_path=None):
                self.assertTrue(write)
                self.assertEqual(Path(db_path), db)
                inner = sqlite3.connect(db, timeout=5)
                inner.row_factory = sqlite3.Row
                inner.execute("PRAGMA busy_timeout=5000")
                return _BeginNotifyingConnection(inner)

            with closing(sqlite3.connect(db, timeout=5)) as blocker:
                blocker.execute("PRAGMA journal_mode=WAL")
                blocker.execute("BEGIN IMMEDIATE")
                with mock.patch.object(analyst_writer, "DB_PATH", db), \
                        mock.patch.object(
                            analyst_writer, "connect",
                            side_effect=notifying_connect), \
                        mock.patch.object(
                            analyst_writer, "analysis_deadline_refusal",
                            side_effect=deadline_side_effect), \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(
                        analyst_writer.write_analysis, payload)
                    self.assertTrue(begin_started.wait(timeout=2))
                    self.assertFalse(future.done())
                    blocker.rollback()
                    result = future.result(timeout=5)

            self.assertEqual("analysis_deadline_exceeded", result["error"])
            self.assertEqual(0, result["production_database_writes"])
            self.assertEqual(3, checks)
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(
                    0, con.execute(
                        "SELECT COUNT(*) FROM analysis_runs").fetchone()[0])
                self.assertEqual(
                    0, con.execute(
                        "SELECT COUNT(*) FROM analysis_signals").fetchone()[0])

    def test_deadline_crossed_before_commit_rolls_back_run_and_signals(
            self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "analysis.db"
            _create_analysis_db(db)
            cycle = "2026-08-15T23:00"
            with closing(sqlite3.connect(db)) as con:
                con.execute(
                    "INSERT INTO analysis_runs"
                    "(cycle_id,ts,mode,raw,status) VALUES(?,?,?,?,?)",
                    (cycle, "old-ts", "full", '{"old":true}', "error"),
                )
                con.execute(
                    "INSERT INTO analysis_signals"
                    "(cycle_id,symbol,action,side) VALUES(?,?,?,?)",
                    (cycle, "OLD-USDT-SWAP", "wait", None),
                )
                con.commit()
            payload = {
                "cycle_id": cycle,
                "ts": "2026-08-15 23:01:00",
                "mode": "full",
                "status": "skipped",
                "decision_protocol": "decision_card_v1",
                "market_summary": None,
                "signals": [],
            }
            checks = 0

            def deadline_side_effect(_cycle_id):
                nonlocal checks
                checks += 1
                # Entry, post-validation and post-lock checks pass.  The
                # fourth check is immediately before COMMIT, after both run
                # replacement and signal deletion have executed in txn.
                return (None if checks < 4
                        else self._deadline_refusal(cycle))

            with mock.patch.object(analyst_writer, "DB_PATH", db), \
                    mock.patch.object(
                        analyst_writer, "analysis_deadline_refusal",
                        side_effect=deadline_side_effect):
                result = analyst_writer.write_analysis(payload)

            self.assertEqual("analysis_deadline_exceeded", result["error"])
            self.assertEqual(0, result["production_database_writes"])
            self.assertEqual(4, checks)
            with closing(sqlite3.connect(db)) as con:
                run = con.execute(
                    "SELECT ts,raw,status FROM analysis_runs WHERE cycle_id=?",
                    (cycle,),
                ).fetchone()
                signal = con.execute(
                    "SELECT symbol,action FROM analysis_signals "
                    "WHERE cycle_id=?",
                    (cycle,),
                ).fetchone()
            self.assertEqual(("old-ts", '{"old":true}', "error"), run)
            self.assertEqual(("OLD-USDT-SWAP", "wait"), signal)

    def test_explicit_open_side_conflict_remains_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._open_payload(root, side="short")
            normalized = analyst_writer.normalize_receipt(payload)
            with mock.patch.object(
                    analyst_writer, "DB_PATH", root / "analysis.db"):
                errors = analyst_writer.validate_receipt(
                    payload, db_root=root)
        self.assertEqual("short", normalized["signals"][0]["side"])
        self.assertTrue(any("不一致" in item for item in errors), errors)

    def test_nonempty_mtf_evidence_string_becomes_one_item_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._open_payload(root)
            row = payload["signals"][0]["decision_card"][
                "multitimeframe_analysis"]["timeframes"]["15m"]
            row["evidence"] = " one exact reason "
            normalized = analyst_writer.normalize_receipt(payload)
            with mock.patch.object(
                    analyst_writer, "DB_PATH", root / "analysis.db"):
                errors = analyst_writer.validate_receipt(
                    payload, db_root=root)
        evidence = normalized["signals"][0]["decision_card"][
            "multitimeframe_analysis"]["timeframes"]["15m"]["evidence"]
        self.assertEqual(["one exact reason"], evidence)
        self.assertEqual([], errors)

    def test_empty_or_object_mtf_evidence_remains_invalid(self) -> None:
        for invalid in ("", {"reason": "not a list"}):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                payload = self._open_payload(root)
                payload["signals"][0]["decision_card"][
                    "multitimeframe_analysis"]["timeframes"]["15m"][
                        "evidence"] = invalid
                normalized = analyst_writer.normalize_receipt(payload)
                with mock.patch.object(
                        analyst_writer, "DB_PATH", root / "analysis.db"):
                    errors = analyst_writer.validate_receipt(
                        payload, db_root=root)
                retained = normalized["signals"][0]["decision_card"][
                    "multitimeframe_analysis"]["timeframes"]["15m"][
                        "evidence"]
            self.assertEqual(invalid, retained)
            self.assertTrue(any("evidence" in item for item in errors), errors)

    def test_price_hints_are_numeric_and_hold_cannot_claim_stop_price(self):
        base = {
            "cycle_id": "2026-07-29T12:00",
            "ts": "2026-07-29 12:01:00",
            "mode": "full",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "market_summary": {
                name: {} for name in analyst_writer.MARKET_SUMMARY_SECTIONS
            },
        }
        string_price = {
            **base,
            "signals": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "open_long",
                "side": "long",
                "entry_hint": "60000附近",
                "stop_hint": 59000.0,
                "tp_hint": 62000.0,
                "decision_card": _valid_card(),
            }],
        }
        errors = analyst_writer.validate_receipt(string_price)
        self.assertTrue(
            any("entry_hint" in item and "正有限数值" in item
                for item in errors),
            errors,
        )

        hold_with_claimed_stop = {
            **base,
            "signals": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "hold",
                "side": None,
                "entry_hint": None,
                "stop_hint": 59000.0,
                "tp_hint": None,
                "decision_card": _valid_card(),
            }],
        }
        errors = analyst_writer.validate_receipt(hold_with_claimed_stop)
        self.assertTrue(
            any("价格提示必须全为 null" in item for item in errors),
            errors,
        )

    def test_action_side_contract_is_fail_closed(self) -> None:
        base = {
            "cycle_id": "2026-07-29T12:00",
            "ts": "2026-07-29 12:01:00",
            "mode": "full",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "market_summary": {
                name: {} for name in analyst_writer.MARKET_SUMMARY_SECTIONS
            },
        }
        bad = {
            **base,
            "signals": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "open_long",
                "side": "short",
                "decision_card": _valid_card(),
            }],
        }
        errors = analyst_writer.validate_receipt(bad)
        self.assertTrue(any("不一致" in item for item in errors), errors)

        missing_evidence = {
            **base,
            "signals": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "open_long",
                "side": "long",
                "decision_card": _valid_card(),
            }],
        }
        errors = analyst_writer.validate_receipt(missing_evidence)
        self.assertTrue(
            any("evidence_contract" in item for item in errors), errors)

        unknown = {
            **base,
            "signals": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "maybe",
                "side": None,
                "decision_card": _valid_card(),
            }],
        }
        errors = analyst_writer.validate_receipt(unknown)
        self.assertTrue(any("action 不支持" in item for item in errors), errors)

        # 持仓管理动作是 Agent 的正式裁决；必须绑定明确持仓方向，但不套
        # OPEN/ADD 的多周期/历史 EV 进入闸。
        for action in ("reduce", "adjust_protection"):
            with self.subTest(action=action):
                signal = {
                    **base,
                    "signals": [{
                        "symbol": "BTC-USDT-SWAP",
                        "action": action,
                        "side": "long",
                        "stop_hint": 95.0 if action == "adjust_protection" else None,
                        "tp_hint": 110.0 if action == "adjust_protection" else None,
                        "decision_card": _valid_card(action),
                    }],
                }
                self.assertEqual(
                    analyst_writer.validate_receipt(signal), [])

        bad_reduce = {
            **base,
            "signals": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "reduce",
                "side": None,
                "decision_card": _valid_card("bad reduce"),
            }],
        }
        self.assertTrue(any(
            "不一致" in item
            for item in analyst_writer.validate_receipt(bad_reduce)))

        # hold = 持有既有仓位，恒无方向。
        hold_with_side = {
            **base,
            "signals": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "hold",
                "side": "long",
                "decision_card": _valid_card(),
            }],
        }
        errors = analyst_writer.validate_receipt(hold_with_side)
        self.assertTrue(any("不一致" in item for item in errors), errors)

        # wait = 可选方向；三种取值都必须放行，否则错失机会对照组断供。
        for wait_side in (None, "long", "short"):
            with self.subTest(wait_side=wait_side):
                wait_signal = {
                    **base,
                    "signals": [{
                        "symbol": "BTC-USDT-SWAP",
                        "action": "wait",
                        "side": wait_side,
                        "decision_card": _valid_card(),
                    }],
                }
                self.assertEqual(
                    analyst_writer.validate_receipt(wait_signal), [])

        # wait 的方向仍受域约束，垃圾值（历史上出现过 '-'/'n/a'）必须拒。
        wait_garbage = {
            **base,
            "signals": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "wait",
                "side": "n/a",
                "decision_card": _valid_card(),
            }],
        }
        errors = analyst_writer.validate_receipt(wait_garbage)
        self.assertTrue(any("不一致" in item for item in errors), errors)

        missing_status = dict(base)
        missing_status.pop("status")
        errors = analyst_writer.validate_receipt(missing_status)
        self.assertTrue(any("status" in item for item in errors), errors)

        missing_protocol = dict(base)
        missing_protocol.pop("decision_protocol")
        errors = analyst_writer.validate_receipt(missing_protocol)
        self.assertTrue(
            any("decision_protocol" in item for item in errors), errors)

        stale_with_signal = {
            **base,
            "status": "stale",
            "market_summary": None,
            "signals": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "hold",
                "side": None,
                "decision_card": _valid_card(),
            }],
        }
        errors = analyst_writer.validate_receipt(stale_with_signal)
        self.assertTrue(
            any("signals 必须为空" in item for item in errors), errors)

    def test_writer_owns_analysis_timestamp_and_keeps_reported_ts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "analysis.db"
            _create_analysis_db(db)
            payload = {
                "cycle_id": "2026-07-29T12:00",
                "ts": "2001-01-01 00:00:00",
                "mode": "full",
                "status": "skipped",
                "decision_protocol": "decision_card_v1",
                "market_summary": None,
                "signals": [],
                "raw": {"reason": "gate"},
            }
            with mock.patch.object(analyst_writer, "DB_PATH", db):
                result = analyst_writer.write_analysis(payload)
            self.assertTrue(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                ts, raw_text = con.execute(
                    "SELECT ts,raw FROM analysis_runs WHERE cycle_id=?",
                    (payload["cycle_id"],),
                ).fetchone()
            self.assertNotEqual(ts, payload["ts"])
            persisted = json.loads(raw_text)
            self.assertEqual(persisted["reported_ts"], payload["ts"])
            self.assertEqual(persisted["decision_protocol"], "decision_card_v1")
            self.assertEqual(persisted["status"], "skipped")
            self.assertEqual(persisted["signals"], [])
            self.assertEqual(persisted["raw"], {"reason": "gate"})
            self.assertEqual(persisted["raw_schema_version"], 2)

    def test_direct_write_cannot_bypass_validation_and_labels_are_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "analysis.db"
            _create_analysis_db(db)
            bad = {
                "cycle_id": "2026-07-29T12:15",
                "ts": "2026-07-29 12:16:00",
                "mode": "full",
                "status": "ok",
                "decision_protocol": "decision_card_v1",
                "market_summary": {
                    name: {} for name in analyst_writer.MARKET_SUMMARY_SECTIONS
                },
                "signals": [{
                    "symbol": "BTC-USDT-SWAP",
                    "action": "open_long",
                    "side": "short",
                    "decision_card": _valid_card(),
                }],
            }
            with mock.patch.object(analyst_writer, "DB_PATH", db):
                refused = analyst_writer.write_analysis(bad)
            self.assertFalse(refused["ok"], refused)

            _create_market_db(
                db.parent / "market.db", "2026-07-29T12:30")
            good = {
                **bad,
                "cycle_id": "2026-07-29T12:30",
                "status": "OK",
                "decision_protocol": "DECISION_CARD_V1",
                "signals": [{
                    "symbol": "BTC-USDT-SWAP",
                    "action": "OPEN_LONG",
                    "side": "LONG",
                    "decision_card": _card_with_contract(
                        "2026-07-29T12:30",
                        "BTC-USDT-SWAP",
                        "long",
                        db_root=db.parent,
                    ),
                    "raw": "agent short text",
                }],
            }
            with mock.patch.object(analyst_writer, "DB_PATH", db):
                written = analyst_writer.write_analysis(good)
            self.assertTrue(written["ok"], written)
            with closing(sqlite3.connect(db)) as con:
                status = con.execute(
                    "SELECT status FROM analysis_runs WHERE cycle_id=?",
                    (good["cycle_id"],),
                ).fetchone()[0]
                action, side, signal_raw = con.execute(
                    "SELECT action,side,raw FROM analysis_signals WHERE cycle_id=?",
                    (good["cycle_id"],),
                ).fetchone()
            self.assertEqual((status, action, side), ("ok", "open_long", "long"))
            signal_raw_obj = json.loads(signal_raw)
            self.assertEqual(signal_raw_obj["schema_version"], 1)
            self.assertEqual(signal_raw_obj["source"], "analyst_writer")
            self.assertEqual(signal_raw_obj["input_kind"], "str")
            self.assertEqual(signal_raw_obj["payload"], "agent short text")
            self.assertEqual(
                signal_raw_obj["canonical_signal"]["symbol"],
                "BTC-USDT-SWAP",
            )

    def test_open_evidence_resealed_after_tamper_still_fails_market_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycle_id = "2026-07-29T13:00"
            _create_market_db(root / "market.db", cycle_id)
            card = _card_with_contract(
                cycle_id, "BTC-USDT-SWAP", "long", db_root=root)
            contract = card["multitimeframe_analysis"]["evidence_contract"]
            contract["timeframes"]["4H"]["values"]["rsi14"] = 70.0
            card["multitimeframe_analysis"]["evidence_contract"] = (
                mtf_gate.seal_evidence_contract(contract)
            )
            payload = {
                "cycle_id": cycle_id,
                "ts": "2026-07-29 13:01:00",
                "mode": "full",
                "status": "ok",
                "decision_protocol": "decision_card_v1",
                "regime": "",
                "market_summary": {
                    name: {} for name in analyst_writer.MARKET_SUMMARY_SECTIONS
                },
                "signals": [{
                    "symbol": "BTC-USDT-SWAP",
                    "action": "open_long",
                    "side": "long",
                    "decision_card": card,
                }],
            }

            with mock.patch.object(
                    analyst_writer, "DB_PATH", root / "analysis.db"):
                errors = analyst_writer.validate_receipt(
                    payload, db_root=root)

            self.assertTrue(
                any("与 market.db 本 cycle" in item for item in errors),
                errors,
            )


class TradesWriterHardeningTests(unittest.TestCase):
    def _payload(self, cycle_id: str) -> dict:
        return {
            "cycle_id": cycle_id,
            "ts": "2001-01-01 00:00:00",
            "decision": "traded",
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "decision_card": _valid_card(),
            "equity": 1000.0,
            "_profile": "live",
            "raw": {"source": "test"},
            "trades": [{
                "symbol": "BTC-USDT-SWAP",
                "action": "open",
                "side": "long",
                "sz": 1.0,
                "fill_sz": 1.0,
                "approved_sz": 2.0,
                "fill_px": 60000.0,
                "lev": 5.0,
                "ct_val": 0.01,
                "fill_source": "fills",
                "fill_ts": "2026-07-29 12:00:05",
                "ts_source": "fills.fillTime",
                "reasoning": "test",
                "pnl": 0.0,
            }],
        }

    def test_exchange_fill_ts_wins_and_caller_ts_is_only_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:00")
            with mock.patch.object(
                trades_writer, "now_cst",
                return_value="2026-07-29 12:01:00",
            ):
                result = trades_writer.write_trades(payload, db)
            self.assertTrue(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                cycle_ts, cycle_raw = con.execute(
                    "SELECT ts,raw FROM trade_cycles").fetchone()
                trade_ts, trade_raw = con.execute(
                    "SELECT ts,raw FROM trades").fetchone()
            self.assertEqual(cycle_ts, "2026-07-29 12:01:00")
            self.assertEqual(trade_ts, "2026-07-29 12:00:05")
            self.assertEqual(
                json.loads(cycle_raw)["reported_ts"],
                "2001-01-01 00:00:00",
            )
            self.assertEqual(
                json.loads(trade_raw)["ts_source"],
                "fills.fillTime",
            )

    def test_trade_level_decision_card_wins_over_top_level_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:05")
            top_card = _valid_card("top-level fallback")
            trade_card = _valid_card("symbol-specific canonical card")
            payload["decision_card"] = top_card
            payload["trades"][0]["decision_card"] = trade_card

            with mock.patch.object(
                trades_writer, "now_cst",
                return_value="2026-07-29 12:06:00",
            ):
                result = trades_writer.write_trades(payload, db)

            self.assertTrue(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                trade_raw = con.execute("SELECT raw FROM trades").fetchone()[0]
            persisted = json.loads(trade_raw)
            self.assertEqual(persisted["decision_card"], trade_card)
            self.assertNotEqual(persisted["decision_card"], top_card)

    def test_first_write_duplicate_trade_identity_is_refused_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:07")
            payload["trades"] = [
                dict(payload["trades"][0]),
                dict(payload["trades"][0]),
            ]
            with mock.patch.object(
                trades_writer, "_analysis_context_for_cycle", return_value={}
            ), mock.patch.object(
                trades_writer, "_equity_snapshot_fallback", return_value=None
            ):
                result = trades_writer.write_trades(payload, db)

            self.assertFalse(result["ok"], result)
            self.assertEqual(result["refused"], "duplicate_trade_identity")
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM trade_cycles").fetchone()[0], 0)
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM trades").fetchone()[0], 0)

    def test_concurrent_disjoint_same_cycle_commits_merge_without_lost_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            cycle = "2026-07-29T12:08"
            left = self._payload(cycle)
            right = self._payload(cycle)
            left["trades"][0]["ordId"] = "ORDER-LEFT"
            right["trades"][0].update({
                "symbol": "ETH-USDT-SWAP",
                "fill_px": 3000.0,
                "ordId": "ORDER-RIGHT",
            })

            def commit(payload):
                return trades_writer.write_trades(payload, db)

            with mock.patch.object(
                trades_writer, "_analysis_context_for_cycle", return_value={}
            ), mock.patch.object(
                trades_writer, "_equity_snapshot_fallback", return_value=None
            ), ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(commit, payload) for payload in (left, right)]
                results = [future.result(timeout=15) for future in futures]

            self.assertTrue(all(result["ok"] for result in results), results)
            with closing(sqlite3.connect(db)) as con:
                symbols = [row[0] for row in con.execute(
                    "SELECT symbol FROM trades ORDER BY symbol").fetchall()]
                n_orders = con.execute(
                    "SELECT n_orders FROM trade_cycles WHERE cycle_id=?",
                    (cycle,),
                ).fetchone()[0]
            self.assertEqual(symbols, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
            self.assertEqual(n_orders, 2)

    def test_structured_raw_compaction_preserves_full_live_facts(self) -> None:
        facts = {"facts_hash": "f" * 64, "authority_blob": "F" * 70000}
        oversized = {
            "cycle_id": "2026-07-29T12:09",
            "live_facts": facts,
            "decision_card": _valid_card(),
            "trades": [],
            "position_action_results": [{
                "request": {"action": "CLOSE", "symbol": "BTC-USDT-SWAP"},
                "result": {"trades": [], "debug": "R" * 60000},
            }],
            "position_action_failures": [{
                "request": {"action": "OPEN", "symbol": "ETH-USDT-SWAP"},
                "problem": "contract error",
                "result": {"trades": [], "debug": "E" * 60000},
            }],
        }

        encoded = trades_writer._bounded_json(
            oversized, 100000, "isolated trade_cycles.raw"
        )
        persisted = json.loads(encoded)
        self.assertEqual(persisted["live_facts"], facts)
        self.assertTrue(persisted["raw_structurally_truncated"])
        self.assertIn("row_sha256", persisted["position_action_results"][0])
        self.assertIn("row_sha256", persisted["position_action_failures"][0])

    def test_oversized_authority_preserves_terminal_and_plan_binding_keys(self) -> None:
        facts = {"facts_hash": "f" * 64, "authority_blob": "F" * 120000}
        oversized = {
            "schema_version": 1,
            "cycle_id": "2026-07-29T12:09",
            "status": "error",
            "decision": "traded",
            "batch_status": "partial",
            "batch_ok": False,
            "runner_in_progress": False,
            "n_orders": 1,
            "facts_hash": "f" * 64,
            "position_action_plan_hash": "p" * 64,
            "live_facts": facts,
            "decision_card": _valid_card(),
            "trades": [{"ordId": "ORDER-1", "symbol": "BTC-USDT-SWAP"}],
            "debug": "D" * 50000,
        }

        encoded = trades_writer._bounded_json(
            oversized, 100000, "isolated trade_cycles.raw"
        )
        persisted = json.loads(encoded)
        self.assertEqual(persisted["live_facts"], facts)
        self.assertEqual(persisted["batch_status"], "partial")
        self.assertIs(persisted["runner_in_progress"], False)
        self.assertEqual(persisted["position_action_plan_hash"], "p" * 64)
        self.assertEqual(persisted["status"], "error")
        self.assertEqual(persisted["n_orders"], 1)

    def test_salvage_direct_ordid_uses_fill_size_and_writer_time_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:10")
            trade = payload["trades"][0]
            trade.update({"ordId": "ORDER-1", "sz": 9.0, "fill_sz": 1.25})
            trade.pop("fill_ts")
            trade.pop("ts_source")
            with mock.patch.object(
                trades_writer, "_analysis_context_for_cycle", return_value={}
            ), mock.patch.object(
                trades_writer, "_equity_snapshot_fallback", return_value=None
            ), mock.patch.object(
                trades_writer, "now_cst", return_value="2026-07-29 12:10:30"
            ):
                result = trades_writer.commit_side_effect_salvage(
                    payload,
                    "live",
                    validation_errors=["missing fill_ts"],
                    db_path=db,
                    _capability=trades_writer._SIDE_EFFECT_SALVAGE_CAPABILITY,
                )

            self.assertTrue(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                sz, trade_raw = con.execute(
                    "SELECT sz,raw FROM trades").fetchone()
                cycle_raw = con.execute(
                    "SELECT raw FROM trade_cycles").fetchone()[0]
            self.assertEqual(sz, 1.25)
            self.assertEqual(
                json.loads(trade_raw)["ts_source"], "writer_commit_fallback"
            )
            quarantine = json.loads(cycle_raw)["contract_quarantine"]
            self.assertEqual(quarantine["timestamp_fallbacks"][0]["ordId"],
                             "ORDER-1")
            self.assertEqual(
                quarantine["size_mismatches"][0]["authoritative_fill_sz"], 1.25
            )

    def test_salvage_rejects_algoid_as_fill_identity_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:11")
            payload["trades"][0].update({"algoId": "SL-ALGO-ONLY"})
            result = trades_writer.commit_side_effect_salvage(
                payload,
                "live",
                validation_errors=["missing direct order identity"],
                db_path=db,
                _capability=trades_writer._SIDE_EFFECT_SALVAGE_CAPABILITY,
            )
            self.assertFalse(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM trade_cycles").fetchone()[0], 0)
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM trades").fetchone()[0], 0)

    def test_salvage_partial_candidates_are_audited_and_not_marked_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:12")
            trusted = dict(payload["trades"][0], ordId="ORDER-GOOD")
            rejected = dict(payload["trades"][0], symbol="ETH-USDT-SWAP",
                            fill_source="planned", ordId="ORDER-FAKE")
            payload["trades"] = [trusted, rejected]
            with mock.patch.object(
                trades_writer, "_analysis_context_for_cycle", return_value={}
            ), mock.patch.object(
                trades_writer, "_equity_snapshot_fallback", return_value=None
            ):
                result = trades_writer.commit_side_effect_salvage(
                    payload,
                    "live",
                    validation_errors=["mixed contract errors"],
                    db_path=db,
                    _capability=trades_writer._SIDE_EFFECT_SALVAGE_CAPABILITY,
                )

            self.assertFalse(result["ok"], result)
            self.assertTrue(result["partial_persisted"], result)
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM trades").fetchone()[0], 1)
                cycle_raw = json.loads(con.execute(
                    "SELECT raw FROM trade_cycles").fetchone()[0])
            quarantine = cycle_raw["contract_quarantine"]
            self.assertEqual(quarantine["candidate_count"], 2)
            self.assertEqual(quarantine["side_effect_trades_preserved"], 1)
            self.assertEqual(quarantine["rejected_count"], 1)
            self.assertEqual(len(quarantine["rejected_candidates"][0]["trade_sha256"]),
                             64)

    def test_salvage_ordidless_close_rejects_untrusted_time_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:13")
            payload["trades"][0].update({
                "action": "close",
                "fill_ts": "2026-07-29 12:13:05",
                "ts_source": "caller.claimed_time",
            })
            result = trades_writer.commit_side_effect_salvage(
                payload,
                "live",
                validation_errors=["close identity contract invalid"],
                db_path=db,
                _capability=trades_writer._SIDE_EFFECT_SALVAGE_CAPABILITY,
            )
            self.assertFalse(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM trade_cycles").fetchone()[0], 0)
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM trades").fetchone()[0], 0)

    def test_salvage_ordidless_close_rejects_fill_outside_cycle_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:13")
            payload["trades"][0].update({
                "action": "close",
                "fill_ts": "2026-07-29 12:27:00",
                "ts_source": "fills.fillTime",
            })
            result = trades_writer.commit_side_effect_salvage(
                payload,
                "live",
                validation_errors=["close identity contract invalid"],
                db_path=db,
                _capability=trades_writer._SIDE_EFFECT_SALVAGE_CAPABILITY,
            )
            self.assertFalse(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM trade_cycles").fetchone()[0], 0)
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM trades").fetchone()[0], 0)

    def test_salvage_refused_write_is_not_reported_partial_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:13")
            trusted = dict(payload["trades"][0], ordId="ORDER-GOOD")
            rejected = dict(payload["trades"][0], symbol="ETH-USDT-SWAP",
                            fill_source="planned", ordId="ORDER-FAKE")
            payload["trades"] = [trusted, rejected]
            with mock.patch.object(
                trades_writer, "_write_trades",
                return_value={
                    "ok": False,
                    "refused": "ambiguous_merge",
                    "error": "ambiguous_merge",
                },
            ):
                result = trades_writer.commit_side_effect_salvage(
                    payload,
                    "live",
                    validation_errors=["mixed contract errors"],
                    db_path=db,
                    _capability=trades_writer._SIDE_EFFECT_SALVAGE_CAPABILITY,
                )
            self.assertFalse(result["ok"], result)
            self.assertFalse(result["partial_persisted"], result)
            self.assertEqual(result["preserved_count"], 0)
            self.assertEqual(result["accepted_candidate_count"], 1)
            self.assertEqual(result["error"], "ambiguous_merge")

    def test_missing_fill_ts_uses_explicit_writer_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:15")
            payload["trades"] = [{
                "symbol": "BTC-USDT-SWAP",
                "action": "close",
                "side": "long",
                "sz": 1.0,
                "fill_px": None,
                "pnl": None,
                "fill_source": "unconfirmed",
                "reasoning": "pending reconciliation",
            }]
            with mock.patch.object(
                trades_writer, "now_cst",
                return_value="2026-07-29 12:16:00",
            ):
                result = trades_writer.write_trades(payload, db)
            self.assertTrue(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                trade_ts, trade_raw = con.execute(
                    "SELECT ts,raw FROM trades").fetchone()
            self.assertEqual(trade_ts, "2026-07-29 12:16:00")
            self.assertEqual(
                json.loads(trade_raw)["ts_source"],
                "writer_commit_fallback",
            )

    def test_internal_maintenance_uses_separate_capability_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:30")
            payload.pop("status")
            payload.pop("decision_protocol")
            payload.pop("decision_card")
            payload["trades"][0].pop("fill_ts")
            payload["trades"][0].pop("ts_source")
            payload["trades"][0].pop("fill_sz")
            result = trades_writer.maintenance_write_trades(
                payload,
                db,
                trusted_timestamp="2026-07-20 03:04:05",
            )
            self.assertTrue(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                cycle_ts, cycle_raw = con.execute(
                    "SELECT ts,raw FROM trade_cycles").fetchone()
                trade_ts, trade_raw = con.execute(
                    "SELECT ts,raw FROM trades").fetchone()
            self.assertEqual(cycle_ts, "2026-07-20 03:04:05")
            self.assertEqual(trade_ts, "2026-07-20 03:04:05")
            self.assertEqual(
                json.loads(cycle_raw)["cycle_ts_source"],
                "trusted_internal_override",
            )
            self.assertEqual(
                json.loads(trade_raw)["ts_source"],
                "trusted_internal_override",
            )

            with self.assertRaises(TypeError):
                trades_writer.write_trades(
                    payload,
                    db,
                    trusted_timestamp="2000-01-01 00:00:00",
                )

    def test_receipt_fields_cannot_enable_maintenance_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T12:35")
            payload["equity"] = None
            payload["ts"] = "2000-01-01 00:00:00"
            payload["_maintenance_preserve_equity_none"] = True
            payload["_trusted_timestamp"] = "2000-01-01 00:00:00"
            with (
                mock.patch.object(
                    trades_writer,
                    "now_cst",
                    return_value="2026-07-29 12:36:00",
                ),
                mock.patch.object(
                    trades_writer,
                    "_equity_snapshot_fallback",
                    return_value=(777.0, "2026-07-29 12:35:30"),
                ),
            ):
                result = trades_writer.write_trades(payload, db)
            self.assertTrue(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                cycle_ts, equity = con.execute(
                    "SELECT ts,equity FROM trade_cycles").fetchone()
            self.assertEqual(cycle_ts, "2026-07-29 12:36:00")
            self.assertEqual(equity, 777.0)

    def test_current_hold_preserves_same_slot_reconciled_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            cycle = trades_writer.RECONCILE_ANALYSIS_TERMINAL_FROM
            maintenance = self._payload(cycle)
            maintenance.pop("status")
            maintenance.pop("decision_protocol")
            maintenance.pop("decision_card")
            maintenance["raw"] = {
                "reconcile_source": "exchange_fills_exact",
                "symbol": "MU-USDT-SWAP",
            }
            maintenance["trades"] = [{
                "symbol": "MU-USDT-SWAP",
                "action": "close",
                "side": "long",
                "sz": 0.5,
                "fill_px": 954.99,
                "lev": 10.0,
                "pnl": -4.21,
                "reasoning": "exchange stop reconciliation",
            }]
            first = trades_writer.maintenance_write_trades(
                maintenance, db,
                trusted_timestamp="2026-08-14 03:45:04",
                preserve_equity_none=True,
            )
            self.assertTrue(first["ok"], first)

            current = {
                "cycle_id": cycle,
                "decision": "hold",
                "status": "ok",
                "decision_protocol": "decision_card_v1",
                "decision_card": _valid_card("scheduled Agent HOLD"),
                "action_taken": "HOLD",
                "n_orders": 0,
                "equity": 920.0,
                "_profile": "live",
                "raw": {"source": "scheduled_agent"},
                "trades": [],
            }
            second = trades_writer.write_trades(current, db)
            self.assertTrue(second["ok"], second)
            self.assertFalse(second.get("refused"), second)
            with closing(sqlite3.connect(db)) as con:
                con.row_factory = sqlite3.Row
                header = con.execute(
                    "SELECT decision,n_orders,equity,raw FROM trade_cycles"
                ).fetchone()
                rows = con.execute(
                    "SELECT symbol,action,side,sz,pnl FROM trades"
                ).fetchall()
            self.assertEqual((header["decision"], header["n_orders"]),
                             ("traded", 1))
            self.assertEqual(header["equity"], 920.0)
            raw = json.loads(header["raw"])
            self.assertEqual(raw["status"], "ok")
            self.assertEqual(raw["action_taken"], "HOLD")
            self.assertTrue(raw["reconciled_close_preserved"])
            self.assertEqual(
                [(row["symbol"], row["action"], row["side"], row["sz"],
                  row["pnl"]) for row in rows],
                [("MU-USDT-SWAP", "close", "long", 0.5, -4.21)],
            )

    def test_confirmed_fill_contract_and_failure_receipts_are_closed(self) -> None:
        payload = self._payload("2026-07-29T13:00")
        for field, expected in (
            ("fill_source", "fill_source 必填"),
            ("fill_sz", "fill_sz 必须"),
            ("fill_ts", "fill_ts 必须"),
            ("ts_source", "ts_source 必填"),
        ):
            broken = json.loads(json.dumps(payload))
            broken["trades"][0].pop(field)
            errors = trades_writer.validate(broken)
            self.assertTrue(
                any(expected in item for item in errors),
                (field, errors),
            )

        open_unconfirmed = json.loads(json.dumps(payload))
        open_unconfirmed["trades"][0].update({
            "fill_source": "unconfirmed",
            "fill_px": None,
            "pnl": None,
            "fill_ts": None,
        })
        errors = trades_writer.validate(open_unconfirmed)
        self.assertTrue(
            any("禁止 fill_source=unconfirmed" in item for item in errors),
            errors,
        )

        close_unconfirmed = json.loads(json.dumps(payload))
        close_unconfirmed["trades"][0].update({
            "action": "close",
            "fill_source": "unconfirmed",
            "fill_px": 60000.0,
            "pnl": 1.0,
            "fill_ts": None,
        })
        errors = trades_writer.validate(close_unconfirmed)
        self.assertTrue(any("fill_sz 必须为 null" in item for item in errors), errors)
        self.assertTrue(any("fill_px 必须为 null" in item for item in errors), errors)
        self.assertTrue(any("pnl 必须为 null" in item for item in errors), errors)

        failed = json.loads(json.dumps(payload))
        failed["status"] = "error"
        errors = trades_writer.validate(failed)
        self.assertTrue(any("trades 必须为空" in item for item in errors), errors)

        contradictory = json.loads(json.dumps(payload))
        contradictory["status"] = "ok"
        contradictory["decision"] = "skip"
        contradictory["trades"] = []
        contradictory["n_orders"] = 2
        errors = trades_writer.validate(contradictory)
        self.assertTrue(any("status=ok" in item for item in errors), errors)
        self.assertTrue(any("n_orders" in item for item in errors), errors)

    def test_fill_time_normalizes_across_cst_midnight_and_rejects_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live_trades.db"
            _create_trade_db(db)
            payload = self._payload("2026-07-29T00:00")
            payload["trades"][0]["fill_ts"] = "2026-07-28T16:00:05Z"
            result = trades_writer.write_trades(payload, db)
            self.assertTrue(result["ok"], result)
            with closing(sqlite3.connect(db)) as con:
                trade_ts = con.execute("SELECT ts FROM trades").fetchone()[0]
            self.assertEqual(trade_ts, "2026-07-29 00:00:05")

        invalid = self._payload("2026-07-29T00:15")
        invalid["trades"][0]["fill_ts"] = "2026-02-30 25:61:00"
        errors = trades_writer.validate(invalid)
        self.assertTrue(any("fill_ts 必须" in item for item in errors), errors)

    def test_size_and_decision_ambiguity_are_rejected(self) -> None:
        payload = self._payload("2026-07-29T12:45")
        payload["decision"] = "whatever"
        payload["trades"][0]["fill_sz"] = 0.5
        errors = trades_writer.validate(payload)
        self.assertTrue(any("decision 不支持" in item for item in errors), errors)
        self.assertTrue(any("必须等于权威 fill_sz" in item for item in errors), errors)

    def test_cli_explicitly_rejects_legacy_quick_write(self) -> None:
        argv = [
            "trades_writer.py",
            "--profile",
            "live",
            "--cycle-id",
            "2026-07-29T13:15",
            "--decision",
            "hold",
        ]
        with (
            mock.patch.object(trades_writer.sys, "argv", argv),
            mock.patch("builtins.print") as output,
        ):
            rc = trades_writer.main()
        self.assertEqual(rc, 1)
        payload = json.loads(output.call_args.args[0])
        self.assertFalse(payload["ok"])
        self.assertIn("legacy quick-write 已禁用", payload["error"])


class DispatcherHardeningTests(unittest.TestCase):
    def test_future_push_waits_for_post_agent_report_barrier(self):
        cycle = "2026-08-14T19:00"
        now = datetime(2026, 8, 14, 19, 12, tzinfo=CST)
        analysis = {
            "mode": "full", "status": "ok", "ts": "2026-08-14 19:01:00"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            _write_trade_cycle(root / "live_trades.db", cycle, "hold", 0)
            self.assertTrue(dispatcher.ledger.try_stage(
                ledger_path, cycle, "live"))
            fired: list = []
            with (
                mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis),
                mock.patch.object(
                    dispatcher, "live_report_barrier_ready",
                    return_value=False),
            ):
                out = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *a, **k: fired.append(a) or "card")
            self.assertFalse(fired, (fired, out))
            self.assertFalse(dispatcher.ledger.stage_dispatched(
                ledger_path, cycle, "push"))

            with (
                mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis),
                mock.patch.object(
                    dispatcher, "live_report_barrier_ready",
                    return_value=True),
            ):
                out = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *a, **k: fired.append(a) or "card")
            self.assertEqual([("push", cycle, "full")], fired, out)

    def test_profile_lease_serializes_cycles_and_owner_only_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            dispatcher.ledger.init_ledger(path)
            at = datetime(2026, 7, 29, 12, 0, tzinfo=CST)
            self.assertTrue(dispatcher.ledger.try_profile_lease(
                path, "live", "2026-07-29T12:00", now=at))
            self.assertFalse(dispatcher.ledger.try_profile_lease(
                path, "live", "2026-07-29T12:00", now=at))
            self.assertFalse(dispatcher.ledger.try_profile_lease(
                path, "live", "2026-07-29T12:15", now=at))
            self.assertFalse(dispatcher.ledger.release_profile_lease(
                path, "live", "2026-07-29T12:15"))
            self.assertTrue(dispatcher.ledger.release_profile_lease(
                path, "live", "2026-07-29T12:00"))
            self.assertTrue(dispatcher.ledger.try_profile_lease(
                path, "live", "2026-07-29T12:15", now=at))

    def test_trade_written_rejects_error_and_fake_traded_terminal(self):
        cycle = "2026-07-29T12:00"
        for decision, n_orders, expected in (
            ("error", 0, False),
            ("traded", 0, False),
            ("hold", 0, True),
            ("traded", 1, True),
        ):
            with self.subTest(decision=decision, n_orders=n_orders):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    _create_trade_db(root / "live_trades.db")
                    with closing(sqlite3.connect(
                            root / "live_trades.db")) as con:
                        con.execute(
                            "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                            (cycle, "2026-07-29 12:01:00", "live",
                             decision, n_orders, None, "", "{}"),
                        )
                        con.commit()
                    self.assertEqual(
                        dispatcher.trade_written(root, "live", cycle),
                        expected,
                    )

    def test_business_error_reportable_is_forward_only_and_zero_side_effect(self):
        def prepare(root: Path, cycle: str, *, intent_state: str = "failed_clean"):
            _create_trade_db(root / "live_trades.db")
            raw = {
                "cycle_id": cycle,
                "status": "error",
                "decision": "error",
                "n_orders": 0,
                "action_taken": "REJECT",
                "trades": [],
                "errors": ["clean reject"],
                "reject_reason": "multitimeframe_context_mismatch",
            }
            with closing(sqlite3.connect(root / "live_trades.db")) as con:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-14 02:16:00", "live", "error",
                     0, None, "", json.dumps(raw)),
                )
                con.commit()
            with closing(sqlite3.connect(root / "ledger.db")) as con:
                con.execute(
                    "CREATE TABLE execution_intents("
                    "profile TEXT,cycle_id TEXT,state TEXT,ord_id TEXT,"
                    "submitted_at TEXT,completed_at TEXT)"
                )
                con.execute(
                    "INSERT INTO execution_intents VALUES(?,?,?,?,?,?)",
                    ("live", cycle, intent_state, None, None, None),
                )
                con.commit()

        for cycle, expected in (
            ("2026-08-14T02:00", False),
            (dispatcher.BUSINESS_ERROR_REPORTABLE_FROM, True),
        ):
            with self.subTest(cycle=cycle), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                prepare(root, cycle)
                self.assertEqual(
                    dispatcher.trade_error_reportable(root, "live", cycle),
                    expected,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycle = dispatcher.BUSINESS_ERROR_REPORTABLE_FROM
            prepare(root, cycle, intent_state="submitted")
            self.assertFalse(
                dispatcher.trade_error_reportable(root, "live", cycle))

    def test_forward_business_error_dispatches_full_report(self):
        cycle = dispatcher.BUSINESS_ERROR_REPORTABLE_FROM
        now = datetime(2026, 8, 14, 2, 35, tzinfo=CST)
        analysis = {
            "mode": "full", "status": "ok", "ts": "2026-08-14 02:16:00"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            raw = {
                "cycle_id": cycle, "status": "error", "decision": "error",
                "n_orders": 0, "action_taken": "REJECT", "trades": [],
                "errors": ["clean reject"], "reject_reason": "data_not_ready",
            }
            with closing(sqlite3.connect(root / "live_trades.db")) as con:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-14 02:16:00", "live", "error",
                     0, None, "", json.dumps(raw)),
                )
                con.commit()
            # Keep the fixture compatible with both a base ledger that already
            # owns the table and older isolated schemas that create it lazily.
            with closing(sqlite3.connect(ledger_path)) as con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS execution_intents("
                    "profile TEXT,cycle_id TEXT,state TEXT,ord_id TEXT,"
                    "submitted_at TEXT,completed_at TEXT)"
                )
                con.commit()
            fired: list = []
            with mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis):
                out = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *a, **k: fired.append(a) or "card")
            self.assertEqual([("push", cycle, "full")], fired, (fired, out))

    def test_reconciled_close_does_not_replace_scheduled_unified_analysis(self):
        cycle = dispatcher.RECONCILE_ANALYSIS_TERMINAL_FROM
        now = datetime(2026, 8, 14, 4, 6, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            raw = {
                "reconcile_source": "exchange_fills_exact",
                "cycle_ts_source": "trusted_internal_override",
                "symbol": "MU-USDT-SWAP",
                "close_ts": "2026-08-14 03:45:04",
            }
            with closing(sqlite3.connect(root / "live_trades.db")) as con:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-14 03:45:04", "live", "traded",
                     1, None, "reconcile close", json.dumps(raw)),
                )
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                    "fill_px,lev,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-14 03:45:04", "MU-USDT-SWAP",
                     "close", "long", 0.5, 954.99, 10.0, -4.21, "{}"),
                )
                con.commit()

            self.assertTrue(dispatcher.trade_written(root, "live", cycle))
            self.assertTrue(dispatcher.trade_terminal_requires_analysis(
                root, "live", cycle))
            fired: list = []
            with mock.patch.object(
                    dispatcher, "_collection_ready", return_value=True):
                output = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *args, **kwargs: fired.append(args) or "card",
                )
            self.assertEqual([("live", cycle, "unified")], fired, output)
            self.assertFalse(dispatcher.ledger.stage_dispatched(
                ledger_path, cycle, "push"))

    def test_current_agent_trade_terminal_still_pushes_without_analysis_row(self):
        cycle = "2026-08-14T04:00"
        now = datetime(2026, 8, 14, 4, 10, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            raw = {
                "status": "ok",
                "decision": "traded",
                "decision_protocol": "decision_card_v1",
            }
            with closing(sqlite3.connect(root / "live_trades.db")) as con:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-14 04:09:00", "live", "traded",
                     1, 900.0, "agent open", json.dumps(raw)),
                )
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                    "fill_px,lev,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-14 04:09:00", "SOXL-USDT-SWAP",
                     "open", "long", 3.0, 145.3, 10.0, 0.0, "{}"),
                )
                con.commit()

            self.assertFalse(dispatcher.trade_terminal_requires_analysis(
                root, "live", cycle))
            fired: list = []
            output = dispatcher.dispatch_cycle(
                root, ledger_path, cycle, now=now,
                fire_fn=lambda *args, **kwargs: fired.append(args) or "card",
            )
            self.assertEqual([("push", cycle, "full")], fired, output)

    def test_same_cycle_live_lease_defers_push_until_runner_releases(self):
        cycle = dispatcher.RECONCILE_ANALYSIS_TERMINAL_FROM
        now = datetime(2026, 8, 14, 4, 10, tzinfo=CST)
        analysis = {
            "mode": "full", "status": "ok", "ts": "2026-08-14 04:08:00"}
        # CI globally enables trigger dry-run, which intentionally never writes
        # stage latches.  This case verifies the persistent lease/latch path
        # with an injected fire function and isolated databases.
        with mock.patch.dict(
                os.environ, {"OKX_TRIGGER_DRYRUN": "0"}, clear=False), \
                tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            raw = {
                "reconcile_source": "exchange_fills_exact",
                "cycle_ts_source": "trusted_internal_override",
                "symbol": "SOXL-USDT-SWAP",
            }
            with closing(sqlite3.connect(root / "live_trades.db")) as con:
                con.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-14 04:01:09", "live", "traded",
                     1, None, "reconcile close", json.dumps(raw)),
                )
                con.execute(
                    "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,"
                    "fill_px,lev,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cycle, "2026-08-14 04:01:09", "SOXL-USDT-SWAP",
                     "close", "long", 3.0, 143.91, 10.0, -4.17, "{}"),
                )
                con.commit()
            self.assertTrue(dispatcher.ledger.try_stage(
                ledger_path, cycle, "live"))
            self.assertTrue(dispatcher.ledger.try_profile_lease(
                ledger_path, "live", cycle, now=now))
            self.assertTrue(dispatcher.live_cycle_in_progress(
                ledger_path, cycle, now=now))

            fired: list = []
            with mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis):
                output = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *args, **kwargs: fired.append(args) or "card",
                )
            self.assertFalse(fired, (fired, output))
            self.assertFalse(dispatcher.ledger.stage_dispatched(
                ledger_path, cycle, "push"))

            self.assertTrue(dispatcher.ledger.release_profile_lease(
                ledger_path, "live", cycle))
            self.assertFalse(dispatcher.live_cycle_in_progress(
                ledger_path, cycle, now=now))
            with mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis):
                output = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *args, **kwargs: fired.append(args) or "card",
                )
            self.assertEqual([("push", cycle, "full")], fired, output)
            self.assertTrue(dispatcher.ledger.stage_dispatched(
                ledger_path, cycle, "push"))

    def test_push_fires_on_live_alone_without_demo(self):
        """2026-08-06 解耦：push 是纯汇报、一分钱不碰，不得被 demo 账本卡住
        （8-05 一个 demo 幽灵仓让 demo+push 死锁 2h14m）。demo 停派后恒无该行，
        所以既不阻断也不告警——告警会变成每轮一条的纯噪音。"""
        cycle = "2026-07-29T12:00"
        # analysis 已过 max_age：trader 不会被追起，只剩 push 闸可评估
        now = datetime(2026, 7, 29, 12, 40, tzinfo=CST)
        analysis = {"mode": "full", "status": "ok", "ts": "2026-07-29 12:01:00"}
        # CI keeps trigger dry-run enabled globally for safety.  This test
        # specifically verifies the persistent stage latch, so exercise the
        # non-dry-run dispatcher with an injected fire function and temp DBs.
        with mock.patch.dict(
                os.environ, {"OKX_TRIGGER_DRYRUN": "0"}, clear=False), \
                tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            _create_trade_db(root / "demo_trades.db")
            _write_trade_cycle(root / "live_trades.db", cycle, "hold", 0)

            def run() -> tuple[list, list]:
                fired: list = []
                with mock.patch.object(
                        dispatcher, "analysis_row", return_value=analysis):
                    output = dispatcher.dispatch_cycle(
                        root, ledger_path, cycle, now=now,
                        fire_fn=lambda *a, **k: fired.append(a) or "card")
                return [a[0] for a in fired], output

            stages, out = run()
            self.assertIn("push", stages, (stages, out))
            self.assertFalse([m for m in out if "未落库" in m], out)

            # 同 cycle 再扫：push 闩锁不重派
            stages2, _ = run()
            self.assertNotIn("push", stages2, stages2)

    def test_demo_stage_is_never_auto_dispatched(self):
        """demo 自 2026-08-06 停自动派发：新鲜 analysis 也只起 live，不起 demo。
        需要跑 demo 时走 trigger_agent.py --stage demo 手动起。"""
        cycle = "2026-07-29T12:00"
        now = datetime(2026, 7, 29, 12, 5, tzinfo=CST)   # analysis 新鲜
        analysis = {"mode": "full", "status": "ok", "ts": "2026-07-29 12:04:00"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            _create_trade_db(root / "demo_trades.db")
            fired: list = []
            with mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis):
                out = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *a, **k: fired.append(a) or "card")
            stages = [a[0] for a in fired]
            self.assertNotIn("demo", stages, (stages, out))
            self.assertIn("live", stages, (stages, out))   # 回滚轮补派 full live 不变

    def test_push_still_requires_live_trade_cycles(self):
        """解耦只放开 demo；live 未落库仍然不许 push（没有实盘事实可汇报）。"""
        cycle = "2026-07-29T12:00"
        now = datetime(2026, 7, 29, 12, 40, tzinfo=CST)
        analysis = {"mode": "full", "status": "ok", "ts": "2026-07-29 12:01:00"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            _create_trade_db(root / "demo_trades.db")
            _write_trade_cycle(root / "demo_trades.db", cycle, "hold", 0)
            fired: list = []
            with mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis):
                out = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *a, **k: fired.append(a) or "card")
            self.assertFalse(fired, (fired, out))

    def test_future_terminal_live_failure_fires_wait_report_only(self):
        cycle = "2026-08-13T04:00"
        now = datetime(2026, 8, 13, 4, 20, tzinfo=CST)
        analysis = {
            "mode": "full", "status": "ok", "ts": "2026-08-13 04:01:00"}
        terminal = {
            "stage": "live", "cycle_id": cycle, "status": "failed",
            "failure_kind": "agent_idle_timeout",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            fired: list = []
            with (
                mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis),
                mock.patch.object(
                    dispatcher, "live_failure_terminal",
                    return_value=terminal) as failure_probe,
            ):
                out = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *a, **k: fired.append(a) or "card")
            self.assertEqual(
                [("push", cycle, "failure_report")], fired, (fired, out))
            self.assertFalse(dispatcher.trade_written(root, "live", cycle))
            failure_probe.assert_called_with(
                cycle, now=now, db_root=root)

    def test_valid_trade_terminal_precedes_failure_report(self):
        cycle = "2026-08-13T04:00"
        now = datetime(2026, 8, 13, 4, 20, tzinfo=CST)
        analysis = {
            "mode": "full", "status": "ok", "ts": "2026-08-13 04:01:00"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            _write_trade_cycle(root / "live_trades.db", cycle, "hold", 0)
            fired: list = []
            with (
                mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis),
                mock.patch.object(
                    dispatcher, "live_failure_terminal",
                    return_value={"status": "failed"}),
            ):
                out = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *a, **k: fired.append(a) or "card")
            self.assertEqual([("push", cycle, "full")], fired, (fired, out))

    def test_partial_trade_cycle_blocks_failure_report_without_taking_push_latch(self):
        cycle = "2026-08-13T04:00"
        now = datetime(2026, 8, 13, 4, 20, tzinfo=CST)
        analysis = {
            "mode": "full", "status": "ok", "ts": "2026-08-13 04:01:00"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.db"
            dispatcher.ledger.init_ledger(ledger_path)
            _create_trade_db(root / "live_trades.db")
            _write_trade_cycle(root / "live_trades.db", cycle, "unknown", 0)
            fired: list = []
            with (
                mock.patch.object(
                    dispatcher, "analysis_row", return_value=analysis),
                mock.patch.object(
                    dispatcher, "live_failure_terminal",
                    return_value={"status": "failed"}),
            ):
                out = dispatcher.dispatch_cycle(
                    root, ledger_path, cycle, now=now,
                    fire_fn=lambda *a, **k: fired.append(a) or "card")
            self.assertFalse(fired, (fired, out))
            self.assertTrue(dispatcher.trade_cycle_present(
                root, "live", cycle))
            self.assertFalse(dispatcher.ledger.stage_dispatched(
                ledger_path, cycle, "push"))

    def test_unknown_analysis_status_never_dispatches_downstream(self) -> None:
        fired: list[tuple] = []
        now = datetime(2026, 7, 29, 12, 5, tzinfo=CST)
        with (
            mock.patch.object(
                dispatcher,
                "analysis_row",
                return_value={
                    "mode": "full",
                    "status": "failed",
                    "ts": "2026-07-29 12:04:00",
                },
            ),
            mock.patch.object(
                dispatcher.ledger,
                "stage_dispatched",
                return_value=False,
            ),
            mock.patch.object(
                dispatcher.ledger,
                "try_stage",
                return_value=True,
            ),
        ):
            output = dispatcher.dispatch_cycle(
                Path("."),
                Path("ledger.db"),
                "2026-07-29T12:00",
                now=now,
                fire_fn=lambda *args, **kwargs: fired.append((args, kwargs)),
            )
        self.assertFalse(fired)
        self.assertTrue(any("status=failed" in item for item in output), output)

    def test_missing_status_or_noncurrent_mode_never_dispatches(self) -> None:
        now = datetime(2026, 7, 29, 12, 5, tzinfo=CST)
        for row in (
            {"mode": "full", "status": None, "ts": "2026-07-29 12:04:00"},
            {"mode": None, "status": "ok", "ts": "2026-07-29 12:04:00"},
            {"mode": "legacy", "status": "ok", "ts": "2026-07-29 12:04:00"},
        ):
            fired: list[tuple] = []
            with (
                mock.patch.object(dispatcher, "analysis_row", return_value=row),
                mock.patch.object(
                    dispatcher.ledger, "stage_dispatched", return_value=False),
                mock.patch.object(
                    dispatcher.ledger, "try_stage", return_value=True),
            ):
                output = dispatcher.dispatch_cycle(
                    Path("."),
                    Path("ledger.db"),
                    "2026-07-29T12:00",
                    now=now,
                    fire_fn=lambda *args, **kwargs: fired.append((args, kwargs)),
                )
            self.assertFalse(fired, (row, output))


if __name__ == "__main__":
    unittest.main()
