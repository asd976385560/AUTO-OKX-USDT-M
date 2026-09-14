# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
COLLECTORS = ROOT / "collectors"
for module_path in (ROOT, CORE, COLLECTORS):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

import multitimeframe_gate as gate  # noqa: E402
import order_executor as oe  # noqa: E402


_DEADLINE_PATCHER = None


def setUpModule() -> None:
    """Historical fixtures isolate MTF behavior; deadline has dedicated tests."""
    global _DEADLINE_PATCHER
    _DEADLINE_PATCHER = mock.patch.object(
        oe, "_cycle_side_effect_reject", return_value=None)
    _DEADLINE_PATCHER.start()


def tearDownModule() -> None:
    if _DEADLINE_PATCHER is not None:
        _DEADLINE_PATCHER.stop()


CYCLE = "2026-08-12T18:30"


def _valid_row(ts: str, symbol: str, timeframe: str) -> tuple:
    return (
        ts, symbol, timeframe,
        100.0, 102.0, 99.0, 101.0, 1000.0,
        100.0, 99.0, 2.0, 55.0, 0.5,
    )


class MultitimeframeReadinessTests(unittest.TestCase):
    @staticmethod
    def _create_market_db(root: Path, *, bars: int = 34) -> Path:
        path = root / "market.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE kline_cache("
                "ts TEXT,symbol TEXT,tf TEXT,o REAL,h REAL,l REAL,c REAL,"
                "v REAL,ma5 REAL,ma20 REAL,atr14 REAL,rsi14 REAL,"
                "macd_hist REAL,PRIMARY KEY(ts,symbol,tf))"
            )
            cycle = gate.parse_cycle_cst(CYCLE)
            for timeframe, seconds in gate.TIMEFRAME_SECONDS.items():
                expected = datetime.fromisoformat(
                    gate.expected_closed_bar_start(cycle, timeframe)
                    .replace("Z", "+00:00")
                )
                rows = [
                    _valid_row(
                        (expected - timedelta(seconds=seconds * offset))
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "BTC-USDT-SWAP",
                        timeframe,
                    )
                    for offset in range(bars - 1, -1, -1)
                ]
                rows.append(
                    _valid_row(
                        (expected + timedelta(seconds=seconds))
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "BTC-USDT-SWAP",
                        timeframe,
                    )
                )
                connection.executemany(
                    "INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
            connection.commit()
        finally:
            connection.close()
        return path

    @staticmethod
    def _create_ws_confirmation_cache(
        root: Path,
        *,
        mode: str = "ws_first",
        mismatch_timeframe: str | None = None,
    ) -> Path:
        cache = root / "ws_market_cache.db"
        connection = sqlite3.connect(cache)
        try:
            connection.execute(
                "CREATE TABLE candles("
                "inst_id TEXT,timeframe TEXT,ts_ms INTEGER,"
                "open TEXT,high TEXT,low TEXT,close TEXT,"
                "volume TEXT,volume_ccy TEXT,volume_quote TEXT,"
                "confirm INTEGER,bar_end_ms INTEGER,close_latency_ms INTEGER,"
                "received_at TEXT,conn_epoch TEXT,source TEXT,"
                "PRIMARY KEY(inst_id,timeframe,ts_ms))"
            )
            cycle = gate.parse_cycle_cst(CYCLE)
            for timeframe in gate.TIMEFRAME_SECONDS:
                expected = gate.expected_closed_bar_start(cycle, timeframe)
                successor = gate.immediate_successor_bar_start(
                    expected, timeframe)
                close = "999.0" if timeframe == mismatch_timeframe else "101.0"
                connection.execute(
                    "INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "BTC-USDT-SWAP",
                        timeframe,
                        gate._iso_utc_to_ms(expected),
                        "100.0", "102.0", "99.0", close,
                        "1000.0", "1000.0", "1000.0", 1,
                        gate._iso_utc_to_ms(successor), 5,
                        "2026-08-12T10:30:05Z", "epoch-1", "ws",
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        (root / "ws_market_source.json").write_text(
            json.dumps({
                "schema_version": 1,
                "mode": mode,
                "pending_mode": None,
                "cache_db": str(cache),
            }),
            encoding="utf-8",
        )
        return cache

    @staticmethod
    def _delete_successors(market_db: Path) -> None:
        connection = sqlite3.connect(market_db)
        try:
            cycle = gate.parse_cycle_cst(CYCLE)
            for timeframe in gate.TIMEFRAME_SECONDS:
                expected = gate.expected_closed_bar_start(cycle, timeframe)
                successor = gate.immediate_successor_bar_start(
                    expected, timeframe)
                connection.execute(
                    "DELETE FROM kline_cache WHERE symbol=? AND tf=? AND ts=?",
                    ("BTC-USDT-SWAP", timeframe, successor),
                )
            connection.commit()
        finally:
            connection.close()

    def test_exact_three_timeframes_with_full_history_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market_db = self._create_market_db(root)
            before = market_db.read_bytes()

            result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)

            self.assertTrue(result["ready"])
            self.assertEqual(result["status"], "PASSED")
            self.assertEqual(
                [row["expected_closed_bar_ts"] for row in result["timeframes"]],
                [
                    "2026-08-12T10:15:00Z",
                    "2026-08-12T09:00:00Z",
                    "2026-08-12T04:00:00Z",
                ],
            )
            self.assertTrue(all(row["ready"] for row in result["timeframes"]))
            self.assertEqual(result["production_database_writes"], 0)
            self.assertEqual(result["orders_placed"], 0)
            self.assertEqual(market_db.read_bytes(), before)

    def test_batch_reuses_connections_and_preserves_single_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._create_market_db(root)
            single = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)
            with mock.patch.object(
                gate,
                "open_ws_confirmation_connection",
                wraps=gate.open_ws_confirmation_connection,
            ) as open_ws:
                batch = gate.check_multitimeframe_readiness_batch(
                    root,
                    ["BTC-USDT-SWAP", "BTC-USDT-SWAP"],
                    CYCLE,
                )
            self.assertEqual(open_ws.call_count, 1)
            self.assertEqual(len(batch), 2)
            self.assertEqual(
                batch[0]["evidence_contract"], single["evidence_contract"])
            self.assertEqual(
                batch[1]["evidence_contract"], single["evidence_contract"])

    def test_missing_exact_bar_fails_even_when_an_older_bar_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market_db = self._create_market_db(root)
            connection = sqlite3.connect(market_db)
            try:
                connection.execute(
                    "DELETE FROM kline_cache WHERE symbol=? AND tf='4H' AND ts=?",
                    ("BTC-USDT-SWAP", "2026-08-12T04:00:00Z"),
                )
                connection.commit()
            finally:
                connection.close()

            result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)
            four_hour = next(
                row for row in result["timeframes"]
                if row["timeframe"] == "4H"
            )
            self.assertFalse(result["ready"])
            self.assertEqual(four_hour["classification"], "source_data_invalid")
            self.assertIn("missing_closed_bar", four_hour["raw_errors"])

    def test_forming_snapshot_without_successor_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market_db = self._create_market_db(root)
            connection = sqlite3.connect(market_db)
            try:
                connection.execute(
                    "DELETE FROM kline_cache WHERE symbol=? AND tf='1H' AND ts=?",
                    ("BTC-USDT-SWAP", "2026-08-12T10:00:00Z"),
                )
                connection.commit()
            finally:
                connection.close()

            result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)
            one_hour = next(
                row for row in result["timeframes"]
                if row["timeframe"] == "1H"
            )

            self.assertFalse(result["ready"])
            self.assertEqual(
                one_hour["observed_bar_ts"], "2026-08-12T09:00:00Z")
            self.assertFalse(one_hour["closed_bar_proof"]["proven"])
            self.assertIn(
                "closed_state_unproven_no_successor_bar",
                one_hour["raw_errors"],
            )

    def test_ws_first_confirmed_exact_candle_proves_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market_db = self._create_market_db(root)
            self._delete_successors(market_db)
            self._create_ws_confirmation_cache(root)

            result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)

            self.assertTrue(result["ready"])
            self.assertEqual("PASSED", result["status"])
            self.assertEqual("ws_first", result[
                "closed_bar_proof_context"]["source_mode"])
            for row in result["timeframes"]:
                proof = row["closed_bar_proof"]
                self.assertEqual(
                    gate.WS_CONFIRMED_BAR_PROOF_METHOD, proof["method"])
                self.assertTrue(proof["proven"])
                self.assertTrue(proof["ws_confirmation"]["market_row_match"])
                self.assertEqual([], proof["ws_confirmation"][
                    "mismatched_fields"])
            self.assertEqual([], gate.validate_evidence_contract(
                result["evidence_contract"],
                expected_symbol="BTC-USDT-SWAP",
                expected_cycle=CYCLE,
            ))

    def test_ws_first_confirmation_requires_exact_market_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market_db = self._create_market_db(root)
            self._delete_successors(market_db)
            self._create_ws_confirmation_cache(
                root, mismatch_timeframe="1H")

            result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)
            one_hour = next(
                row for row in result["timeframes"]
                if row["timeframe"] == "1H"
            )

            self.assertFalse(result["ready"])
            self.assertFalse(one_hour["closed_bar_proof"]["proven"])
            confirmation = one_hour["closed_bar_proof"]["ws_confirmation"]
            self.assertEqual("market_ws_ohlcv_mismatch", confirmation["reason"])
            self.assertEqual(["c"], confirmation["mismatched_fields"])

    def test_non_ws_first_mode_keeps_successor_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market_db = self._create_market_db(root)
            self._delete_successors(market_db)
            self._create_ws_confirmation_cache(root, mode="rest_only")

            result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)

            self.assertFalse(result["ready"])
            self.assertTrue(all(
                row["closed_bar_proof"]["method"]
                == gate.CLOSED_BAR_PROOF_METHOD
                for row in result["timeframes"]
            ))

    def test_fabricated_indicators_cannot_bypass_history_warmup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._create_market_db(root, bars=10)

            result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)

            self.assertFalse(result["ready"])
            self.assertTrue(all(
                row["classification"] == "insufficient_history"
                for row in result["timeframes"]
            ))
            self.assertTrue(all(not row["ready"] for row in result["timeframes"]))

    def test_invalid_indicator_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market_db = self._create_market_db(root)
            connection = sqlite3.connect(market_db)
            try:
                connection.execute(
                    "UPDATE kline_cache SET rsi14=101 WHERE symbol=? "
                    "AND tf='1H' AND ts=?",
                    ("BTC-USDT-SWAP", "2026-08-12T09:00:00Z"),
                )
                connection.commit()
            finally:
                connection.close()

            result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)
            one_hour = next(
                row for row in result["timeframes"]
                if row["timeframe"] == "1H"
            )
            self.assertFalse(result["ready"])
            self.assertEqual(one_hour["classification"], "indicator_invalid")
            self.assertIn("rsi14_out_of_range", one_hour["indicator_errors"])

    def test_evidence_contract_is_self_validating_and_bound_to_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._create_market_db(root)
            result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)
            contract = result["evidence_contract"]
            self.assertEqual(
                gate.validate_evidence_contract(
                    contract,
                    expected_symbol="BTC-USDT-SWAP",
                    expected_cycle=CYCLE,
                ),
                [],
            )
            contract["timeframes"]["15m"]["values"]["c"] = 999.0
            errors = gate.validate_evidence_contract(
                contract,
                expected_symbol="BTC-USDT-SWAP",
                expected_cycle=CYCLE,
            )
            self.assertIn("evidence_hash mismatch", errors)

    def test_missing_database_and_noncanonical_cycle_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)
            invalid = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", "TEST-NOT-A-CYCLE")

            self.assertFalse(missing["ready"])
            self.assertEqual(missing["error"], "market_db_missing")
            self.assertFalse(invalid["ready"])
            self.assertTrue(invalid["error"].startswith("cycle_invalid:"))

    def test_post_analysis_market_revision_uses_persisted_writer_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market_db = self._create_market_db(root)
            original = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)["evidence_contract"]
            analysis_db = root / "analysis.db"
            connection = sqlite3.connect(analysis_db)
            try:
                connection.execute(
                    "CREATE TABLE analysis_signals("
                    "cycle_id TEXT,symbol TEXT,action TEXT,side TEXT,"
                    "decision_card TEXT)"
                )
                connection.execute(
                    "INSERT INTO analysis_signals VALUES(?,?,?,?,?)",
                    (
                        CYCLE, "BTC-USDT-SWAP", "open_long", "long",
                        json.dumps({"multitimeframe_analysis": {
                            "evidence_contract": original}}),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            connection = sqlite3.connect(market_db)
            try:
                connection.execute(
                    "UPDATE kline_cache SET ma20=98.5,rsi14=56.0 "
                    "WHERE symbol=? AND tf='15m' AND ts=?",
                    ("BTC-USDT-SWAP", "2026-08-12T10:15:00Z"),
                )
                connection.commit()
            finally:
                connection.close()
            current_result = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)
            current = current_result["evidence_contract"]

            resolved = gate.resolve_execution_evidence_anchor(
                root, "BTC-USDT-SWAP", CYCLE, "long", original, current)

            self.assertTrue(current_result["ready"])
            self.assertNotEqual(original, current)
            self.assertTrue(resolved["ok"], resolved)
            self.assertTrue(resolved["post_analysis_market_revision"])
            self.assertEqual(
                resolved["evidence_anchor"], "analysis_db_writer_validated")
            self.assertEqual(
                resolved["persisted_evidence_hash"],
                original["evidence_hash"],
            )

    def test_revision_exception_rejects_missing_tampered_or_wrong_side_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._create_market_db(root)
            original = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)["evidence_contract"]
            changed = copy.deepcopy(original)
            changed["timeframes"]["15m"]["values"]["rsi14"] = 56.0
            changed = gate.seal_evidence_contract(changed)

            missing = gate.resolve_execution_evidence_anchor(
                root, "BTC-USDT-SWAP", CYCLE, "long", original, changed)
            tampered = copy.deepcopy(original)
            tampered["timeframes"]["4H"]["values"]["rsi14"] = 70.0
            invalid = gate.resolve_execution_evidence_anchor(
                root, "BTC-USDT-SWAP", CYCLE, "long", tampered, changed)

            analysis_db = root / "analysis.db"
            connection = sqlite3.connect(analysis_db)
            try:
                connection.execute(
                    "CREATE TABLE analysis_signals("
                    "cycle_id TEXT,symbol TEXT,action TEXT,side TEXT,"
                    "decision_card TEXT)"
                )
                connection.execute(
                    "INSERT INTO analysis_signals VALUES(?,?,?,?,?)",
                    (
                        CYCLE, "BTC-USDT-SWAP", "open_short", "short",
                        json.dumps({"multitimeframe_analysis": {
                            "evidence_contract": original}}),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            wrong_side = gate.resolve_execution_evidence_anchor(
                root, "BTC-USDT-SWAP", CYCLE, "long", original, changed)

            self.assertFalse(missing["ok"], missing)
            self.assertFalse(invalid["ok"], invalid)
            self.assertFalse(wrong_side["ok"], wrong_side)
            self.assertEqual(wrong_side["persisted_side"], "short")


class OpenExecutionGateTests(unittest.TestCase):
    def test_open_rejects_before_exchange_io_when_market_data_not_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            balance = mock.Mock()
            place = mock.Mock()
            with (
                mock.patch.object(oe.ox, "is_dryrun", return_value=False),
                mock.patch.object(oe, "validate_receipt_context", return_value=[]),
                mock.patch.object(
                    oe.actor_att,
                    "timeline_state",
                    return_value={
                        "available": True,
                        "handoff_detected": False,
                        "analysis_epoch": 0,
                        "current_epoch": 0,
                        "actor_chain_hash": "isolated",
                    },
                ),
                mock.patch.object(oe.ox, "get_balance", balance),
                mock.patch.object(oe.ox, "place_market_open", place),
            ):
                result = oe.open_position(
                    "BTC-USDT-SWAP",
                    "long",
                    1.0,
                    5.0,
                    95.0,
                    "live",
                    db_root=root,
                    cycle_id=CYCLE,
                    receipt_context={"cycle_id": CYCLE},
                )

            self.assertFalse(result["ok"])
            self.assertEqual(
                result["reject_reason"], "multitimeframe_data_not_ready")
            self.assertEqual(
                result["multitimeframe_readiness"]["error"],
                "market_db_missing",
            )
            balance.assert_not_called()
            place.assert_not_called()

    def test_close_never_invokes_open_readiness_gate(self):
        readiness = mock.Mock(side_effect=AssertionError("OPEN gate called"))
        with (
            mock.patch.object(oe.ox, "is_dryrun", return_value=False),
            mock.patch.object(oe, "validate_receipt_context", return_value=[]),
            mock.patch.object(oe, "check_multitimeframe_readiness", readiness),
            mock.patch.object(oe, "fetch_open_positions", return_value=[]),
        ):
            result = oe.close_position(
                "BTC-USDT-SWAP",
                "live",
                pos_side="long",
                db_root=Path("isolated"),
                cycle_id=CYCLE,
                receipt_context={"cycle_id": CYCLE},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["note"], "no_open_position")
        readiness.assert_not_called()

    def test_open_rejects_tampered_card_before_exchange_io(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            MultitimeframeReadinessTests._create_market_db(root)
            actual = gate.check_multitimeframe_readiness(
                root, "BTC-USDT-SWAP", CYCLE)
            supplied = copy.deepcopy(actual["evidence_contract"])
            supplied["timeframes"]["4H"]["values"]["rsi14"] = 70.0
            context = {
                "cycle_id": CYCLE,
                "decision_card": {
                    "multitimeframe_analysis": {
                        "evidence_contract": supplied,
                    }
                },
            }
            balance = mock.Mock()
            with (
                mock.patch.object(oe.ox, "is_dryrun", return_value=False),
                mock.patch.object(oe, "validate_receipt_context", return_value=[]),
                mock.patch.object(
                    oe.actor_att,
                    "timeline_state",
                    return_value={
                        "available": True,
                        "handoff_detected": False,
                        "analysis_epoch": 0,
                        "current_epoch": 0,
                        "actor_chain_hash": "isolated",
                    },
                ),
                mock.patch.object(
                    oe.ei, "reserve",
                    return_value={"status": "reserved", "fingerprint": "FP"},
                ),
                mock.patch.object(oe.ei, "mark_failed_clean"),
                mock.patch.object(oe.ox, "get_balance", balance),
            ):
                result = oe.open_position(
                    "BTC-USDT-SWAP",
                    "long",
                    1.0,
                    5.0,
                    95.0,
                    "live",
                    db_root=root,
                    cycle_id=CYCLE,
                    receipt_context=context,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(
                result["reject_reason"], "multitimeframe_context_mismatch")
            balance.assert_not_called()


if __name__ == "__main__":
    unittest.main()
