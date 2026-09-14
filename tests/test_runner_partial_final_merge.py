# -*- coding: utf-8 -*-
"""Runner final receipts that recorded a failed action vs same-slot reconciles.

Reconstructs the ledger sequence behind the 2026-09-03..09-11 writer
``ambiguous_merge`` refusals of the runner's own final receipt
(logs/runner-handoff, 9 cycles): the runner persists an interim receipt, an
exchange-side close that filled inside the running 15-minute slot is
reconciled into the same cycle (trigger autoheal A / pretrade autoheal B via
``apply_reconcile``), and the final receipt carries one failed action
(``batch_status=partial``, ``batch_ok=False``, ``runner_in_progress=False``).
Maintenance rows come from the real ``apply_reconcile`` / ``apply_unrecorded``
so the stored shapes are the production shapes.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for value in (ROOT, ROOT / "core", ROOT / "scripts", ROOT / "collectors"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from core import decision_card  # noqa: E402
from collectors import trades_writer  # noqa: E402
from scripts import _acceptance_thresholds as thresholds  # noqa: E402
from scripts import reconcile_exchange_closes as reconcile  # noqa: E402


CYCLE = "2026-09-02T18:00"
BINDING = {
    "facts_hash": "a" * 64,
    "plan_sha256": "b" * 64,
    "position_action_plan_hash": "c" * 64,
}
FAILURE = "OPEN PEPE-USDT-SWAP/long: place_failed"
# reconcile_exchange_closes imports the writer as top-level ``trades_writer``
# while tests import ``collectors.trades_writer``: two module objects, one
# source.  Every patch below is applied to both.
WRITERS = tuple({
    id(module): module for module in (trades_writer, reconcile.trades_writer)
}.values())


def package() -> dict:
    return {
        "contract": decision_card.OPEN_EXECUTION_PACKAGE_CONTRACT,
        "entry": 10.0,
        "stop": 9.0,
        "target": 12.0,
        "exit_mode": "fixed_tp",
    }


def runner_trade(symbol: str, ord_id: str | None, *, sz: float = 1.0,
                 px: float = 10.0, hms: str = "18:01:00",
                 action: str = "open", side: str = "long") -> dict:
    trade = {
        "symbol": symbol,
        "action": action,
        "side": side,
        "sz": sz,
        "approved_sz": sz,
        "fill_sz": sz,
        "fill_px": px,
        "fill_source": "fills",
        "fill_ts": f"2026-09-02 {hms}",
        "ts_source": "fills.fillTime",
        "lev": 5.0,
        "margin": sz * px / 5.0,
        "notional": sz * px,
        "reasoning": "exchange-confirmed runner fill",
        "sl_trigger_px": 9.0,
        "sl_verified": True,
    }
    if ord_id is not None:
        trade["ordId"] = ord_id
    if action in {"open", "add"}:
        trade[decision_card.OPEN_EXECUTION_PACKAGE_KEY] = package()
    return trade


def runner_receipt(trades: list[dict], stage: str, **updates) -> dict:
    value = {
        "cycle_id": CYCLE,
        "mode": "live",
        "profile": "live",
        "status": "ok",
        "decision": "traded",
        "action_taken": "OPEN_LONG",
        "decision_protocol": decision_card.MINIMAL_DECISION_PROTOCOL,
        "reasoning": "deterministic runner batch",
        "regime": "range",
        "n_orders": len(trades),
        "trades": copy.deepcopy(trades),
        "errors": [],
        "ok": True,
        **BINDING,
    }
    if stage == "interim":
        value.update(batch_status="partial", batch_ok=True,
                     runner_in_progress=True)
    elif stage == "failed_final":
        # _aggregate_receipt with one failed action (production shape).
        value.update(
            batch_status="partial", batch_ok=False, runner_in_progress=False,
            errors=[FAILURE],
            position_action_failures=[{
                "request": {"action": "OPEN", "symbol": "PEPE-USDT-SWAP"},
                "problem": FAILURE,
            }],
        )
    elif stage == "completed_final":
        value.update(
            batch_status="completed", batch_ok=True, runner_in_progress=False,
            business_terminal={
                "schema_version": 1,
                "cycle_id": CYCLE,
                "status": "completed",
                "completed_at_cst": "2026-09-02 18:12:00",
            },
        )
    else:
        raise ValueError(stage)
    value.update(updates)
    return value


def exchange_fills(ord_id: str, sz: float, px: float, hms: str,
                   *, pnl: str = "0") -> list[dict]:
    fill_time = datetime.strptime(
        f"2026-09-02 {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=reconcile.CST)
    return [{
        "ordId": ord_id,
        "fillTime": str(int(fill_time.timestamp() * 1000)),
        "fillPx": str(px),
        "fillSz": str(sz),
        "fillPnl": pnl,
        "tradeId": f"TRADE-{ord_id}",
        "execType": "T",
    }]


def create_trade_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as con:
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


class RunnerPartialFinalMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self._fresh_db("live_trades.db")
        stack = ExitStack()
        self.addCleanup(stack.close)
        # Hermetic: nothing may reach <PROJECT_ROOT>\db (experience rows live in
        # account.db, whose default path is production).
        stack.enter_context(mock.patch.dict(os.environ, {
            "OKX_ACCOUNT_DB": str(self.root / "account.db"),
            "OKX_ANALYSIS_DB": str(self.root / "analysis.db"),
            "OKX_MARKET_DB": str(self.root / "market.db"),
        }))
        stack.enter_context(mock.patch.object(
            thresholds, "minimal_decision_contract_active", return_value=True))
        stack.enter_context(mock.patch.object(
            thresholds, "minimal_contract_closure_active", return_value=True))
        self.experience_batches: list[list[str]] = []
        for writer in WRITERS:
            stack.enter_context(mock.patch.object(
                writer, "_analysis_context_for_cycle", return_value={}))
            stack.enter_context(mock.patch.object(
                writer, "_ctval_for", return_value=1.0))
            stack.enter_context(mock.patch.object(
                writer, "write_experiences",
                side_effect=self._record_experiences))
        stack.enter_context(mock.patch.object(
            reconcile, "find_journal_close", return_value=None))

    # -- fixtures -------------------------------------------------------
    def _fresh_db(self, name: str) -> Path:
        path = self.root / name
        create_trade_db(path)
        self.db = path
        return path

    def _record_experiences(self, data, *_args, **_kwargs) -> dict:
        trades = list(data.get("trades") or [])
        self.experience_batches.append([t.get("symbol") for t in trades])
        return {"exp": len(trades)}

    def _ro(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def reconcile_close(self, symbol: str, side: str, sz: float, px: float,
                        ord_id: str, hms: str) -> dict:
        """Exchange-side close reconciled exactly as autoheal A/B does."""
        matched = reconcile.group_by_ord(
            exchange_fills(ord_id, sz, px, hms, pnl="1.25"))
        with closing(self._ro()) as ro:
            result = reconcile.apply_reconcile(
                self.db, "live", symbol, side, sz, matched, ro, open_lev=5.0)
        self.assertTrue(result["writer"]["ok"], result)
        # slot_cycle_id(close fill time) is the cycle the runner is running.
        self.assertEqual(CYCLE, result["cycle_id"])
        return result

    def heal_unrecorded_open(self, symbol: str, side: str, sz: float,
                             px: float, ord_id: str, hms: str,
                             *, intent_ord_id: str | None = None) -> dict:
        """Strict T1 UNRECORDED open written exactly as apply_unrecorded does."""
        matched = reconcile.group_by_ord(exchange_fills(ord_id, sz, px, hms))
        intent = {
            "cycle_id": CYCLE,
            "ord_id": intent_ord_id or ord_id,
            "state": "completed",
            "receipt_trade": {
                "action": "open", "sz": sz, "lev": 5.0, "ct_val": 1.0,
                "sl_trigger_px": 9.0, "sl_verified": True,
            },
        }
        with closing(self._ro()) as ro:
            result = reconcile.apply_unrecorded(
                self.db, "live", symbol, side, sz, matched, ro, lev=5.0,
                card=None, intent=intent, sl_probe={"has_sl": True})
        self.assertEqual("applied", result["status"], result)
        self.assertEqual(CYCLE, result["cycle_id"])
        return result

    def commit(self, trades: list[dict], stage: str, **updates) -> dict:
        return trades_writer.write_trades(
            runner_receipt(trades, stage, **updates), self.db)

    def assert_committed(self, result: dict) -> None:
        self.assertTrue(result.get("ok"), result)
        self.assertIsNone(result.get("refused"), result)

    def rows(self) -> list[dict]:
        with closing(sqlite3.connect(self.db)) as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(
                "SELECT symbol, action, side, sz, fill_px, raw FROM trades "
                "WHERE cycle_id=? ORDER BY id", (CYCLE,))]

    def row_raw(self, symbol: str, action: str) -> dict:
        matches = [row for row in self.rows()
                   if (row["symbol"], row["action"]) == (symbol, action)]
        self.assertEqual(1, len(matches), matches)
        return json.loads(matches[0]["raw"])

    def header(self) -> dict:
        with closing(sqlite3.connect(self.db)) as con:
            decision, n_orders, note, raw = con.execute(
                "SELECT decision, n_orders, note, raw FROM trade_cycles "
                "WHERE cycle_id=?", (CYCLE,)).fetchone()
        return {"decision": decision, "n_orders": n_orders, "note": note,
                "raw": json.loads(raw)}

    def snapshot(self) -> tuple:
        with closing(sqlite3.connect(self.db)) as con:
            return (
                con.execute("SELECT * FROM trade_cycles").fetchall(),
                con.execute("SELECT * FROM trades ORDER BY id").fetchall(),
            )

    def _mutate_row_raw(self, symbol: str, action: str, change) -> None:
        with closing(sqlite3.connect(self.db)) as con:
            row_id, raw = con.execute(
                "SELECT id, raw FROM trades WHERE cycle_id=? AND symbol=? "
                "AND action=?", (CYCLE, symbol, action)).fetchone()
            raw = json.loads(raw)
            change(raw)
            con.execute("UPDATE trades SET raw=? WHERE id=?",
                        (json.dumps(raw), row_id))
            con.commit()

    # -- reconstructed production sequences -----------------------------
    def test_failed_final_after_midrun_reconcile_persists_last_fill(self):
        """09-07T11:45 SOXL shape: the fill after the last interim was lost."""
        a = runner_trade("X-USDT-SWAP", "ORDER-A")
        c = runner_trade("Z-USDT-SWAP", "ORDER-C", sz=2.0, hms="18:06:00")
        self.assert_committed(self.commit([a], "interim"))
        # Pretrade autoheal B heals an exchange stop that filled in-slot.
        self.reconcile_close("Y-USDT-SWAP", "long", 2.0, 20.0, "ORDER-B",
                             "18:05:00")
        self.assertTrue(
            self.header()["raw"]["runner_progression_preserved_after_reconcile"])

        result = self.commit([a, c], "failed_final")

        self.assert_committed(result)
        self.assertEqual(["Z-USDT-SWAP"],
                         [t["symbol"] for t in result["new_trades"]])
        self.assertEqual(
            [("X-USDT-SWAP", "open"), ("Y-USDT-SWAP", "close"),
             ("Z-USDT-SWAP", "open")],
            sorted((row["symbol"], row["action"]) for row in self.rows()))
        self.assertEqual("exchange_fills_reconcile",
                         self.row_raw("Y-USDT-SWAP", "close")["reconcile_source"])
        last_fill = self.row_raw("Z-USDT-SWAP", "open")
        self.assertEqual("ORDER-C", last_fill["ordId"])
        self.assertEqual(9.0, last_fill["sl_trigger_px"])
        self.assertEqual(
            package(), last_fill[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        header = self.header()
        self.assertEqual(("traded", 3), (header["decision"], header["n_orders"]))
        raw = header["raw"]
        self.assertEqual(("partial", False, False),
                         (raw["batch_status"], raw["batch_ok"],
                          raw["runner_in_progress"]))
        self.assertEqual([FAILURE], raw["errors"])
        self.assertTrue(raw["runner_partial_finalization_reconcile_merge"])
        self.assertNotIn("runner_finalization_reconcile_merge", raw)
        self.assertNotIn("runner_interim_reconcile_merge", raw)
        self.assertEqual(1, raw["merge_guard_kept_rows"])
        self.assertIn("runner partial finalization kept 1", header["note"])

    def test_failed_final_through_commit_receipt_feeds_only_new_fill(self):
        a = runner_trade("X-USDT-SWAP", "ORDER-A")
        c = runner_trade("Z-USDT-SWAP", "ORDER-C", sz=2.0, hms="18:06:00")
        self.assert_committed(self.commit([a], "interim"))
        self.reconcile_close("Y-USDT-SWAP", "long", 2.0, 20.0, "ORDER-B",
                             "18:05:00")
        self.experience_batches.clear()

        result = trades_writer.commit_receipt(
            runner_receipt([a, c], "failed_final"), "live",
            db_path=self.db, nudge=False)

        self.assert_committed(result)
        self.assertEqual([["Z-USDT-SWAP"]], self.experience_batches)

    def test_2030_sequence_prior_and_midrun_reconciles_keep_every_row(self):
        """09-11T20:30 shape: close before the runner, interims, close mid-run."""
        # Trigger autoheal A: exchange stop filled at slot start.
        self.reconcile_close("BCH-USDT-SWAP", "long", 11.5, 219.7,
                             "ORDER-B1", "18:00:11")
        a1 = runner_trade("BCH-USDT-SWAP", "ORDER-A1", sz=17.7, hms="18:08:15")
        a2 = runner_trade("TAO-USDT-SWAP", "ORDER-A2", sz=46.0, hms="18:09:06")
        first = self.commit([a1], "interim")
        self.assert_committed(first)
        self.assertEqual(1, self.header()["raw"]["merge_guard_kept_rows"])
        second = self.commit([a1, a2], "interim")
        self.assert_committed(second)
        self.assertTrue(self.header()["raw"]["runner_interim_reconcile_merge"])
        # Pretrade autoheal B before the failing PEPE open.
        self.reconcile_close("ETC-USDT-SWAP", "short", 2.08, 7.665,
                             "ORDER-B2", "18:09:29")

        result = self.commit([a1, a2], "failed_final")

        self.assert_committed(result)
        self.assertEqual([], result["new_trades"])
        self.assertEqual(
            [("BCH-USDT-SWAP", "close"), ("BCH-USDT-SWAP", "open"),
             ("ETC-USDT-SWAP", "close"), ("TAO-USDT-SWAP", "open")],
            sorted((row["symbol"], row["action"]) for row in self.rows()))
        raw = self.header()["raw"]
        self.assertEqual(4, raw["n_orders"])
        self.assertEqual(2, raw["merge_guard_kept_rows"])
        self.assertTrue(raw["runner_partial_finalization_reconcile_merge"])
        self.assertEqual([FAILURE], raw["errors"])

    def test_identity_bound_unrecorded_open_is_retained(self):
        a = runner_trade("X-USDT-SWAP", "ORDER-A")
        c = runner_trade("Z-USDT-SWAP", "ORDER-C", hms="18:06:00")
        self.assert_committed(self.commit([a], "interim"))
        # A fill of this cycle the runner did not record (failed action),
        # healed by strict T1 while the header is still the runner interim.
        self.heal_unrecorded_open("U-USDT-SWAP", "long", 3.0, 10.0,
                                  "ORDER-U", "18:04:00")

        result = self.commit([a, c], "failed_final")

        self.assert_committed(result)
        self.assertEqual(["Z-USDT-SWAP"],
                         [t["symbol"] for t in result["new_trades"]])
        healed = self.row_raw("U-USDT-SWAP", "open")
        self.assertEqual("exchange_fills_unrecorded", healed["reconcile_source"])
        self.assertEqual("ORDER-U", healed["ordId"])
        self.assertEqual(1, self.header()["raw"]["merge_guard_kept_rows"])

    def test_runner_row_supersedes_its_own_healed_open(self):
        """T1 healed the runner's own fill; the receipt restores its metadata."""
        a = runner_trade("X-USDT-SWAP", "ORDER-A")
        c = runner_trade("Z-USDT-SWAP", "ORDER-C", sz=2.0, hms="18:06:00")
        self.assert_committed(self.commit([a], "interim"))
        self.reconcile_close("Y-USDT-SWAP", "long", 2.0, 20.0, "ORDER-B",
                             "18:05:00")
        self.heal_unrecorded_open("Z-USDT-SWAP", "long", 2.0, 10.0,
                                  "ORDER-C", "18:06:00")

        result = self.commit([a, c], "failed_final")

        self.assert_committed(result)
        self.assertEqual([], result["new_trades"])
        self.assertEqual(3, len(self.rows()))
        restored = self.row_raw("Z-USDT-SWAP", "open")
        self.assertNotIn("reconcile_source", restored)
        self.assertEqual("ORDER-C", restored["ordId"])
        self.assertEqual(
            package(), restored[decision_card.OPEN_EXECUTION_PACKAGE_KEY])
        raw = self.header()["raw"]
        self.assertEqual(["ORDER-C"],
                         raw["merge_guard_superseded_reconcile_ord_ids"])
        self.assertEqual(1, raw["merge_guard_kept_rows"])

    def test_side_effect_salvage_final_uses_the_same_proof(self):
        a = runner_trade("X-USDT-SWAP", "ORDER-A")
        c = runner_trade("Z-USDT-SWAP", "ORDER-C", sz=2.0, hms="18:06:00")
        self.assert_committed(self.commit([a], "interim"))
        self.reconcile_close("Y-USDT-SWAP", "long", 2.0, 20.0, "ORDER-B",
                             "18:05:00")

        result = trades_writer.commit_side_effect_salvage(
            runner_receipt([a, c], "failed_final"), "live",
            validation_errors=["receipt contract drift"], db_path=self.db,
            _capability=trades_writer._SIDE_EFFECT_SALVAGE_CAPABILITY)

        self.assert_committed(result)
        self.assertTrue(result["quarantined"])
        self.assertEqual(3, len(self.rows()))
        raw = self.header()["raw"]
        self.assertEqual("error", raw["status"])
        self.assertEqual("confirmed_trade_receipt_contract_invalid",
                         raw["contract_quarantine"]["kind"])
        self.assertTrue(raw["runner_partial_finalization_reconcile_merge"])

    def test_chained_same_slot_heals_keep_runner_binding(self):
        """Pretrade B can heal several ghosts back to back (up to 3 a round).

        Each heal is its own maintenance write; the second one used to drop
        the runner binding because the first stored header lacked cycle_id,
        which refused even a completed final (09-08T21:30 lost its hashes).
        """
        a = runner_trade("X-USDT-SWAP", "ORDER-A")
        c = runner_trade("Z-USDT-SWAP", "ORDER-C", sz=2.0, hms="18:06:00")
        for final_stage in ("failed_final", "completed_final"):
            with self.subTest(final_stage):
                self._fresh_db(f"chain-{final_stage}.db")
                self.assert_committed(self.commit([a], "interim"))
                self.reconcile_close("Y-USDT-SWAP", "long", 2.0, 20.0,
                                     "ORDER-B1", "18:05:00")
                self.reconcile_close("V-USDT-SWAP", "short", 4.0, 30.0,
                                     "ORDER-B2", "18:05:30")
                self.heal_unrecorded_open("U-USDT-SWAP", "long", 3.0, 10.0,
                                          "ORDER-U", "18:05:40")
                raw = self.header()["raw"]
                self.assertEqual(CYCLE, raw["cycle_id"])
                self.assertTrue(
                    raw["runner_progression_preserved_after_reconcile"])
                self.assertEqual(BINDING["plan_sha256"], raw["plan_sha256"])

                result = self.commit([a, c], final_stage)

                self.assert_committed(result)
                self.assertEqual(["Z-USDT-SWAP"],
                                 [t["symbol"] for t in result["new_trades"]])
                self.assertEqual(5, len(self.rows()))
                self.assertEqual(3, self.header()["raw"]["merge_guard_kept_rows"])

    def test_preserved_runner_context_proves_itself_again(self):
        interim = runner_receipt([], "interim")
        context = reconcile._active_runner_progression_context(interim, CYCLE)
        self.assertEqual(CYCLE, context["cycle_id"])
        stored_header = {"reconcile_source": "exchange_fills_reconcile",
                         **context}
        self.assertEqual(
            context,
            reconcile._active_runner_progression_context(stored_header, CYCLE))
        self.assertEqual(
            {},
            reconcile._active_runner_progression_context(
                stored_header, "2026-09-02T18:15"))

    # -- still fail-closed ----------------------------------------------
    def test_ambiguous_failed_finals_remain_refused(self):
        a = runner_trade("X-USDT-SWAP", "ORDER-A")
        a2 = runner_trade("W-USDT-SWAP", "ORDER-A2", hms="18:02:00")
        c = runner_trade("Z-USDT-SWAP", "ORDER-C", sz=2.0, hms="18:06:00")

        def with_reconcile(prior):
            self.assert_committed(self.commit(prior, "interim"))
            self.reconcile_close("Y-USDT-SWAP", "long", 2.0, 20.0, "ORDER-B",
                                 "18:05:00")

        # Each case builds its history and returns the final commit, so the
        # snapshot below is taken immediately before the refused write.
        def not_superset():
            with_reconcile([a, a2])
            return lambda: self.commit([a, c], "failed_final")

        def binding_mismatch():
            with_reconcile([a])
            return lambda: self.commit(
                [a, c], "failed_final", plan_sha256="d" * 64)

        def second_terminal_receipt():
            with_reconcile([a])
            self.assert_committed(self.commit([a, c], "failed_final"))
            extra = runner_trade("V-USDT-SWAP", "ORDER-D", hms="18:07:00")
            return lambda: self.commit([a, c, extra], "failed_final")

        def hidden_duplicate_of_reconciled_close():
            with_reconcile([a])
            # Same exchange order as the reconciled close; only the rounded
            # fingerprint differs, so _rows_match alone would append it.
            dup = runner_trade("Y-USDT-SWAP", "ORDER-B", sz=2.0, px=20.0001,
                               hms="18:05:00", action="close")
            return lambda: self.commit([a, dup], "failed_final")

        def salvage_close_without_order_id():
            with_reconcile([a])
            close = runner_trade("Y-USDT-SWAP", None, sz=2.0, px=20.0001,
                                 hms="18:05:00", action="close")
            return lambda: trades_writer.commit_side_effect_salvage(
                runner_receipt([a, close], "failed_final"), "live",
                validation_errors=["receipt contract drift"], db_path=self.db,
                _capability=trades_writer._SIDE_EFFECT_SALVAGE_CAPABILITY)

        def healed(mutation=None, **heal):
            def build():
                self.assert_committed(self.commit([a], "interim"))
                self.heal_unrecorded_open(
                    "U-USDT-SWAP", "long", 3.0, 10.0, "ORDER-U", "18:04:00",
                    **heal)
                if mutation is not None:
                    self._mutate_row_raw("U-USDT-SWAP", "open", mutation)
                return lambda: self.commit([a, c], "failed_final")
            return build

        def legacy_ord_ids_only(raw):
            raw.pop("ordId")

        def other_cycle_intent(raw):
            raw["intent"]["cycle_id"] = "2026-09-02T17:45"

        def fills_do_not_explain_row(raw):
            raw["fills"][0]["sz"] = "2.5"

        def smaller_readback_than_healed_fill():
            self.assert_committed(self.commit([a], "interim"))
            self.reconcile_close("Y-USDT-SWAP", "long", 2.0, 20.0, "ORDER-B",
                                 "18:05:00")
            self.heal_unrecorded_open("Z-USDT-SWAP", "long", 2.0, 10.0,
                                      "ORDER-C", "18:06:00")
            smaller = runner_trade("Z-USDT-SWAP", "ORDER-C", sz=1.5,
                                   hms="18:06:00")
            return lambda: self.commit([a, smaller], "failed_final")

        cases = {
            "not a superset of prior runner rows": not_superset,
            "plan binding mismatch": binding_mismatch,
            "second terminal receipt": second_terminal_receipt,
            "hidden duplicate of reconciled close":
                hidden_duplicate_of_reconciled_close,
            "salvage close without order id":
                salvage_close_without_order_id,
            "legacy healed open (ord_ids only)": healed(legacy_ord_ids_only),
            "healed open bound to another intent order":
                healed(intent_ord_id="ORDER-OTHER"),
            "healed open from another cycle's intent":
                healed(other_cycle_intent),
            "healed fills do not explain the row":
                healed(fills_do_not_explain_row),
            "runner readback smaller than healed fill":
                smaller_readback_than_healed_fill,
        }
        for index, (label, build) in enumerate(cases.items()):
            with self.subTest(label):
                self._fresh_db(f"case-{index}.db")
                final_commit = build()
                before = self.snapshot()
                result = final_commit()
                self.assertFalse(result.get("ok"), (label, result))
                self.assertEqual("ambiguous_merge", result.get("refused"),
                                 (label, result))
                self.assertEqual(before, self.snapshot())

    def test_progression_kind_truth_table(self):
        interim_raw = runner_receipt([], "interim")
        failed = runner_receipt([], "failed_final")
        salvage = runner_receipt(
            [], "failed_final", status="error",
            contract_quarantine={
                "kind": "confirmed_trade_receipt_contract_invalid"})
        kind = trades_writer._runner_same_plan_progression_kind
        cases = [
            (interim_raw, runner_receipt([], "interim"), "interim"),
            (interim_raw, runner_receipt([], "completed_final"),
             "finalization"),
            (interim_raw, failed, "partial_finalization"),
            (interim_raw, salvage, "partial_finalization"),
            (interim_raw, {**failed, "status": "error"}, None),
            (interim_raw, {**salvage, "contract_quarantine": {
                "kind": "something_else"}}, None),
            (interim_raw, {**failed, "runner_in_progress": True}, None),
            (interim_raw, {**failed, "business_terminal": {
                "cycle_id": CYCLE, "status": "completed"}}, None),
            (interim_raw, {**failed, "batch_status": "completed"}, None),
            (interim_raw, {**failed, "batch_ok": None}, None),
            (failed, failed, None),
            (runner_receipt([], "completed_final"), failed, None),
            (interim_raw, {**failed, "facts_hash": "e" * 64}, None),
            ({**interim_raw, "plan_sha256": None},
             {**failed, "plan_sha256": None}, None),
        ]
        for index, (old_raw, incoming, expected) in enumerate(cases):
            with self.subTest(index=index, expected=expected):
                self.assertEqual(expected, kind(old_raw, incoming, CYCLE))


if __name__ == "__main__":
    unittest.main()
