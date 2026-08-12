from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from collectors import analyst_writer, trades_writer
from core import dispatcher
from core import multitimeframe_gate as mtf_gate
from core.experience_contract import build_contract
from core.instrument_context import build_instrument_context


CST = timezone(timedelta(hours=8))


class AnalysisValidationBudgetTests(unittest.TestCase):
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
                        "rr": 2.0, "summary": "bounded"},
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
                errors = analyst_writer.validate_receipt(payload)

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
