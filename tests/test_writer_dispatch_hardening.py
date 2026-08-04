from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from collectors import analyst_writer, trades_writer
from core import dispatcher


CST = timezone(timedelta(hours=8))


def _valid_card(label: str = "isolated test") -> dict:
    return {
        "direction_evidence": [label],
        "opposing_evidence": ["counter"],
        "execution_conditions": {"status": "ready"},
        "invalidation_point": {"condition": "invalidated"},
        "risk_reward": {"summary": "bounded"},
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
            self.assertEqual(json.loads(raw_text)["reported_ts"], payload["ts"])

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

            good = {
                **bad,
                "cycle_id": "2026-07-29T12:30",
                "status": "OK",
                "decision_protocol": "DECISION_CARD_V1",
                "signals": [{
                    "symbol": "BTC-USDT-SWAP",
                    "action": "OPEN_LONG",
                    "side": "LONG",
                    "decision_card": _valid_card(),
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
                action, side = con.execute(
                    "SELECT action,side FROM analysis_signals WHERE cycle_id=?",
                    (good["cycle_id"],),
                ).fetchone()
            self.assertEqual((status, action, side), ("ok", "open_long", "long"))


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
    def test_public_runner_uses_current_python_and_propagates_db_root(self):
        trigger_source = Path(dispatcher.trigger_agent.__file__).read_text(
            encoding="utf-8"
        )
        self.assertNotIn("pythoncore-", trigger_source)
        self.assertIn(
            'os.environ.get("OKX_PYTHON_BIN", sys.executable)',
            trigger_source,
        )

        custom_root = Path("isolated") / "db"
        with mock.patch.object(dispatcher.trigger_agent, "_PYTHON_EXE",
                               sys.executable):
            command = dispatcher.trigger_agent._supervised_cmd(
                "live", "2026-07-29T12:00", "full", ["child"],
                db_root=custom_root,
            )
        self.assertEqual(command[0], sys.executable)
        self.assertIn("--db-root", command)
        self.assertEqual(
            command[command.index("--db-root") + 1],
            os.fspath(custom_root.resolve()),
        )

    def test_fire_passes_canonical_db_root_into_build_and_runner(self):
        selected_root = dispatcher.trigger_agent._CANONICAL_DB_ROOT
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(dispatcher.trigger_agent, "LOG_DIR", Path(tmp)), \
             mock.patch.object(dispatcher.trigger_agent, "build_cmd",
                               return_value=["child"]) as build, \
             mock.patch.object(dispatcher.trigger_agent, "_supervised_cmd",
                               return_value=["runner"]) as supervised, \
             mock.patch.object(dispatcher.trigger_agent.subprocess, "Popen",
                               return_value=mock.Mock()), \
             mock.patch.object(dispatcher.trigger_agent, "_probe_launch"):
            dispatcher.trigger_agent.fire(
                "live", "2026-07-29T12:00", "full", db_root=selected_root
            )

        build.assert_called_once_with(
            "live", "2026-07-29T12:00", "full", db_root=selected_root
        )
        supervised.assert_called_once_with(
            "live", "2026-07-29T12:00", "full", ["child"],
            db_root=selected_root,
        )

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
