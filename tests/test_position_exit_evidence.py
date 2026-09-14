# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts import live_decision_facts as facts  # noqa: E402
from scripts import multitimeframe_decision_evidence as evidence  # noqa: E402


def _facts() -> dict:
    stamp = int(time.time() * 1000)
    positions = [{
        "instId": "LINK-USDT-SWAP",
        "posSide": "long",
        "pos": "100",
        "avgPx": "8.473822",
        "markPx": "9.461",
        "lever": "10",
        "mgnMode": "cross",
        "posId": "P1",
        "cTime": str(stamp - 93 * 3_600_000),
        "upl": "98.7178",
        "uplRatio": "1.1649737273",
        "imr": "94.61",
    }]
    balance = [{
        "totalEq": "913.72",
        "details": [{
            "ccy": "USDT", "availEq": "550", "imr": "94.61",
            "mmr": "10", "upl": "98.7178",
        }],
    }]
    instruments = {"LINK-USDT-SWAP": {
        "instId": "LINK-USDT-SWAP", "ctVal": "1",
    }}
    algos = {"LINK-USDT-SWAP": [{
        "instId": "LINK-USDT-SWAP", "algoId": "A1",
        "slTriggerPx": "9.2", "slTriggerPxType": "mark",
        "posSide": "long", "side": "sell", "reduceOnly": "true",
        "state": "live", "sz": "100",
    }]}
    return facts.derive_facts(
        "2026-08-15T14:15", "live", positions, balance,
        instruments, algos, as_of_ms=stamp,
    )


class PositionExitEvidenceTests(unittest.TestCase):
    def test_compact_decision_view_preserves_exit_decision_fields(self):
        payload = {
            "cycle_id": "2026-08-15T14:15",
            "facts_hash": "f" * 64,
            "evidence_hash": "e" * 64,
            "status": "PASSED",
            "position_count": 1,
            "protection_floor_breach_count": 0,
            "protection_floor_breaches": [],
            "review_policy": {"automatic_exit_authorized": False},
            "positions": [{
                "symbol": "LINK-USDT-SWAP", "side": "long",
                "current": {"avgPx": 8.4, "markPx": 9.4, "upl": 10.0},
                "open_plan_and_path": {
                    "status": "unique_open_leg", "reason": "exact",
                    "review_flags": [], "fill_px": 8.4, "initial_sl": 8.0,
                    "original_target": 9.8, "original_exit_mode": "fixed_tp",
                    "target_semantics": "explicit_exit_mode",
                    "target_reached": False, "remaining_sz": 100.0,
                    "path_basis": "full_size",
                    "initial_risk_current_size_usdt": 40.0,
                    "current_r_gross": 2.5, "observed_peak_upl_usdt": 110.0,
                    "observed_peak_upl_at": "2026-08-15 13:00:00",
                    "observed_peak_r_gross": 2.75,
                    "giveback_from_observed_peak_usdt": 10.0,
                    "giveback_from_observed_peak_pct": 9.09,
                    "giveback_pct_actionable": True,
                    "protection_floor": {
                        "status": "evaluated", "level": 2, "breach": False,
                        "live_sl_verified": True,
                    },
                },
                "multitimeframe_ready": True,
                "multitimeframe_status": "PASSED",
                "evidence_contract": {"timeframes": {
                    timeframe: {
                        "ready": True, "observed_bar_ts": "2026-08-15T00:00:00Z",
                        "closed_bar_proof": {
                            "proven": True,
                            "ws_confirmation": {"source": "ws"}},
                        "values": {"o": 9.0, "h": 9.5, "l": 8.9,
                                   "c": 9.4, "v": 1000.0, "ma5": 9.2,
                                   "ma20": 9.1, "atr14": 0.2,
                                   "rsi14": 60.0, "macd_hist": 0.1},
                    } for timeframe in ("15m", "1H", "4H")}},
                "gaps": [], "error": None,
            }],
        }
        view = evidence.build_position_exit_decision_view(payload)
        self.assertEqual("position_exit_decision_view_v1", view["schema"])
        self.assertEqual(payload["evidence_hash"], view["source_evidence_hash"])
        self.assertEqual(1, len(view["positions"]))
        row = view["positions"][0]
        self.assertEqual("unique_open_leg", row["path"]["status"])
        self.assertEqual(9.8, row["path"]["original_target"])
        self.assertEqual(2.75, row["path"]["observed_peak_r_gross"])
        self.assertTrue(row["timeframes"]["15m"]["closed_bar_proven"])
        self.assertEqual("above", row["timeframes"]["15m"]["price_vs_ma20"])
        self.assertNotIn("evidence_contract", row)
        self.assertEqual(64, len(view["view_hash"]))
        core = dict(view)
        supplied_hash = core.pop("view_hash")
        self.assertEqual(evidence._canonical_sha256(core), supplied_hash)

    def test_facts_file_waits_for_atomic_producer(self):
        payload = {"cycle_id": "2026-08-15T14:15"}
        with mock.patch.object(
            evidence, "_load_json",
            side_effect=[FileNotFoundError("not ready"), payload],
        ), mock.patch.object(
            evidence.time, "monotonic", side_effect=[0.0, 0.1],
        ), mock.patch.object(evidence.time, "sleep") as sleep:
            result = evidence._load_json_when_ready(
                Path("facts.json"), wait_seconds=1.0, poll_seconds=0.1)

        self.assertEqual(payload, result)
        sleep.assert_called_once_with(0.1)

    def test_facts_file_wait_is_bounded(self):
        with mock.patch.object(
            evidence, "_load_json",
            side_effect=FileNotFoundError("still missing"),
        ), mock.patch.object(
            evidence.time, "monotonic", side_effect=[0.0, 1.1],
        ), mock.patch.object(evidence.time, "sleep") as sleep:
            with self.assertRaises(FileNotFoundError):
                evidence._load_json_when_ready(
                    Path("facts.json"), wait_seconds=1.0, poll_seconds=0.1)

        sleep.assert_not_called()

    def _db_root(self, root: Path) -> None:
        connection = sqlite3.connect(root / "account.db")
        connection.executescript(
            "CREATE TABLE trade_experiences ("
            "id INTEGER PRIMARY KEY,cycle_id TEXT,ts TEXT,profile TEXT,"
            "symbol TEXT,side TEXT,status TEXT,open_sz REAL,"
            "remaining_sz REAL,raw TEXT);"
            "CREATE TABLE position_snapshots ("
            "ts TEXT,profile TEXT,symbol TEXT,upl REAL);"
        )
        connection.execute(
            "INSERT INTO trade_experiences VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                289, "2026-08-11T16:00", "2026-08-11 16:27:42",
                "live", "LINK-USDT-SWAP", "long", "open", 100, 100,
                json.dumps({
                    "fill_px": 8.473822,
                    "sl_trigger_px": 8.25,
                    "decision_card": {"risk_reward": {"target": 8.89}},
                }),
            ),
        )
        connection.executemany(
            "INSERT INTO position_snapshots VALUES (?,?,?,?)",
            [
                ("2026-08-15 11:00:34", "live", "LINK-USDT-SWAP", 124.3178),
                ("2026-08-15 14:15:36", "live", "LINK-USDT-SWAP", 98.0178),
            ],
        )
        connection.commit()
        connection.close()

    def test_batch_surfaces_target_r_peak_and_non_binding_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._db_root(root)
            mtf = {
                "ready": True,
                "status": "PASSED",
                "timeframes": [],
                "evidence_contract": {"evidence_hash": "fixed"},
            }
            with mock.patch.object(
                evidence, "check_multitimeframe_readiness", return_value=mtf,
            ):
                result = evidence.build_position_exit_batch(
                    root, _facts(), "2026-08-15T14:15")

        self.assertTrue(result["ok"])
        self.assertEqual(result["position_count"], 1)
        row = result["positions"][0]
        context = row["open_plan_and_path"]
        self.assertEqual(context["status"], "unique_open_leg")
        self.assertEqual(context["original_target"], 8.89)
        self.assertTrue(context["target_reached"])
        self.assertEqual(context["original_exit_mode"], "legacy_unspecified")
        self.assertAlmostEqual(context["initial_risk_usdt"], 22.3822, places=4)
        self.assertAlmostEqual(context["current_r_gross"], 4.4105, places=4)
        self.assertEqual(context["observed_peak_upl_usdt"], 124.3178)
        self.assertIn(
            "official_margin_return_at_or_above_50pct",
            context["review_flags"],
        )
        self.assertIn("original_target_reached", context["review_flags"])
        self.assertIn("current_profit_at_or_above_2r", context["review_flags"])
        self.assertIn(
            "legacy_exit_mode_requires_fresh_agent_choice",
            context["review_flags"],
        )
        self.assertTrue(context["review_flags_are_non_binding"])
        self.assertFalse(context["automatic_exit_authorized"])
        # 2026-08-17 soft protection floor: peak 5.55R, SL 9.2 locks 3.24R,
        # level-2 floor = max(1R, 50% of peak) = 2.78R -> satisfied, no breach.
        floor = context["protection_floor"]
        self.assertEqual(floor["policy"], "soft_review_flag_not_a_gate")
        self.assertEqual(floor["status"], "evaluated")
        self.assertEqual(floor["level"], 2)
        self.assertFalse(floor["breach"])
        self.assertAlmostEqual(floor["required_protected_r"], 2.7772, places=3)
        self.assertAlmostEqual(
            floor["protected_r_at_current_sl_gross"], 3.2444, places=3)
        self.assertEqual(floor["reason"], "floor_satisfied")
        self.assertTrue(context["giveback_pct_actionable"])
        self.assertEqual(result["protection_floor_breach_count"], 0)
        self.assertNotIn("protection_floor_breach_level_2", context["review_flags"])

    def test_tampered_facts_fail_before_market_or_account_reads(self):
        payload = copy.deepcopy(_facts())
        payload["positions"][0]["upl"] = 999
        with mock.patch.object(
            evidence, "check_multitimeframe_readiness",
        ) as readiness:
            result = evidence.build_position_exit_batch(
                Path("missing"), payload, "2026-08-15T14:15")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "FACTS_INVALID")
        readiness.assert_not_called()


def _crv_like_facts(*, live_sl: str, mark_px: str = "0.2610",
                    contracts: str = "5200") -> dict:
    """CRV-USDT-SWAP long 5200 @0.2638 (initial SL 0.258, risk 30.16 USDT).

    Reproduces the 2026-08-11..13 pattern: the position peaked near +139 USDT
    (+4.6R) while the exchange SL stayed at the original 0.258 and the trade
    was finally stopped at -1.2R.
    """
    stamp = int(time.time() * 1000)
    mark = float(mark_px)
    upl = (mark - 0.2638) * float(contracts)
    positions = [{
        "instId": "CRV-USDT-SWAP",
        "posSide": "long",
        "pos": contracts,
        "avgPx": "0.2638",
        "markPx": mark_px,
        "lever": "10",
        "mgnMode": "cross",
        "posId": "P2",
        "cTime": str(stamp - 50 * 3_600_000),
        "upl": f"{upl:.6f}",
        "uplRatio": f"{upl / (0.2638 * float(contracts) / 10):.6f}",
        "imr": f"{0.2638 * float(contracts) / 10:.4f}",
    }]
    balance = [{
        "totalEq": "950.0",
        "details": [{
            "ccy": "USDT", "availEq": "700", "imr": "137.18",
            "mmr": "10", "upl": f"{upl:.6f}",
        }],
    }]
    instruments = {"CRV-USDT-SWAP": {"instId": "CRV-USDT-SWAP", "ctVal": "1"}}
    algos = {"CRV-USDT-SWAP": [{
        "instId": "CRV-USDT-SWAP", "algoId": "A2",
        "slTriggerPx": live_sl, "slTriggerPxType": "mark",
        "posSide": "long", "side": "sell", "reduceOnly": "true",
        "state": "live", "sz": contracts,
    }]}
    return facts.derive_facts(
        "2026-08-13T11:00", "live", positions, balance,
        instruments, algos, as_of_ms=stamp,
    )


class ProtectionFloorTests(unittest.TestCase):
    """2026-08-17 soft profit-protection floor (review flag, never a gate)."""

    def _db_root(self, root: Path, *, peak_upl: float, remaining_sz: float = 5200,
                 open_ts: str = "2026-08-11 08:42:12",
                 snapshot_sz: bool = True) -> None:
        connection = sqlite3.connect(root / "account.db")
        connection.executescript(
            "CREATE TABLE trade_experiences ("
            "id INTEGER PRIMARY KEY,cycle_id TEXT,ts TEXT,profile TEXT,"
            "symbol TEXT,side TEXT,status TEXT,open_sz REAL,"
            "remaining_sz REAL,raw TEXT);"
            + (
                # production shape (schema.sql): ts,profile,symbol,side,sz,...,upl
                "CREATE TABLE position_snapshots ("
                "ts TEXT,profile TEXT,symbol TEXT,side TEXT,sz REAL,upl REAL);"
                if snapshot_sz else
                "CREATE TABLE position_snapshots ("
                "ts TEXT,profile TEXT,symbol TEXT,upl REAL);"
            )
        )
        connection.execute(
            "INSERT INTO trade_experiences VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                288, "2026-08-11T08:30", open_ts,
                "live", "CRV-USDT-SWAP", "long", "open", 5200, remaining_sz,
                json.dumps({
                    "fill_px": 0.2638,
                    "sl_trigger_px": 0.258,
                    "decision_card": {"risk_reward": {
                        "target": 0.275, "exit_mode": "dynamic_exit"}},
                }),
            ),
        )
        if snapshot_sz:
            # peak observed while the full 5200 contracts were still open
            connection.executemany(
                "INSERT INTO position_snapshots VALUES (?,?,?,?,?,?)",
                [
                    ("2026-08-12 15:45:36", "live", "CRV-USDT-SWAP", "long",
                     5200, peak_upl),
                    ("2026-08-13 11:00:34", "live", "CRV-USDT-SWAP", "long",
                     remaining_sz, -14.56 * remaining_sz / 5200),
                ],
            )
        else:
            connection.executemany(
                "INSERT INTO position_snapshots VALUES (?,?,?,?)",
                [
                    ("2026-08-12 15:45:36", "live", "CRV-USDT-SWAP", peak_upl),
                    ("2026-08-13 11:00:34", "live", "CRV-USDT-SWAP", -14.56),
                ],
            )
        connection.commit()
        connection.close()

    def _run(self, root: Path, payload: dict) -> dict:
        mtf = {"ready": True, "status": "PASSED", "timeframes": [],
               "evidence_contract": {"evidence_hash": "fixed"}}
        with mock.patch.object(
            evidence, "check_multitimeframe_readiness", return_value=mtf,
        ):
            return evidence.build_position_exit_batch(
                root, payload, "2026-08-13T11:00")

    def test_level2_breach_when_sl_never_trailed_from_initial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._db_root(root, peak_upl=139.44)  # +4.62R observed peak
            result = self._run(root, _crv_like_facts(live_sl="0.258"))
        context = result["positions"][0]["open_plan_and_path"]
        floor = context["protection_floor"]
        self.assertAlmostEqual(context["initial_risk_usdt"], 30.16, places=2)
        self.assertAlmostEqual(context["observed_peak_r_gross"], 4.6233, places=3)
        self.assertEqual(floor["status"], "evaluated")
        self.assertEqual(floor["level"], 2)
        self.assertTrue(floor["breach"])
        self.assertTrue(floor["live_sl_verified"])
        # required = max(1R, 50% of 4.62R) = 2.31R; current SL locks -1R.
        self.assertAlmostEqual(floor["required_protected_r"], 2.3116, places=3)
        self.assertAlmostEqual(
            floor["protected_r_at_current_sl_gross"], -1.0, places=3)
        self.assertAlmostEqual(floor["shortfall_r"], 3.3116, places=3)
        # suggested SL = 0.2638 + 2.3116 * 30.16 / 5200 = 0.27721
        self.assertAlmostEqual(floor["suggested_min_sl_px"], 0.27721, places=4)
        self.assertEqual(floor["reason"], "current_sl_below_floor")
        self.assertIn("protection_floor_breach_level_2", context["review_flags"])
        self.assertTrue(context["giveback_pct_actionable"])
        # surfaced at batch level for the Agent and in the review policy
        self.assertEqual(result["protection_floor_breach_count"], 1)
        breach = result["protection_floor_breaches"][0]
        self.assertEqual(breach["symbol"], "CRV-USDT-SWAP")
        self.assertEqual(breach["level"], 2)
        self.assertAlmostEqual(breach["suggested_min_sl_px"], 0.27721, places=4)
        # still evidence-only: nothing is authorized or blocked by this tool
        self.assertFalse(context["automatic_exit_authorized"])
        self.assertTrue(result["review_policy"]["review_flags_are_non_binding"])
        self.assertEqual(result["orders_placed"], 0)
        self.assertEqual(result["production_database_writes"], 0)

    def test_level2_satisfied_when_sl_trailed_to_half_of_peak(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._db_root(root, peak_upl=139.44)
            # SL 0.2775 locks (0.2775-0.2638)*5200 = 71.24 USDT = 2.36R >= 2.31R
            result = self._run(root, _crv_like_facts(
                live_sl="0.2775", mark_px="0.2790"))
        floor = result["positions"][0]["open_plan_and_path"]["protection_floor"]
        self.assertEqual(floor["level"], 2)
        self.assertFalse(floor["breach"])
        self.assertEqual(floor["reason"], "floor_satisfied")
        self.assertEqual(floor["shortfall_r"], 0.0)
        self.assertEqual(result["protection_floor_breach_count"], 0)

    def test_level1_requires_breakeven_plus_fee_buffer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._db_root(root, peak_upl=42.0)  # +1.39R -> level 1
            at_entry = self._run(root, _crv_like_facts(
                live_sl="0.2638", mark_px="0.2660"))
            above_buffer = self._run(root, _crv_like_facts(
                live_sl="0.2650", mark_px="0.2660"))
        floor_entry = at_entry["positions"][0]["open_plan_and_path"][
            "protection_floor"]
        self.assertEqual(floor_entry["level"], 1)
        # SL exactly at entry locks 0 < 0.2% * 1371.76 = 2.74 USDT -> breach
        self.assertTrue(floor_entry["breach"])
        self.assertAlmostEqual(
            floor_entry["required_locked_pnl_usdt"], 2.7435, places=3)
        self.assertIn(
            "protection_floor_breach_level_1",
            at_entry["positions"][0]["open_plan_and_path"]["review_flags"])
        floor_above = above_buffer["positions"][0]["open_plan_and_path"][
            "protection_floor"]
        self.assertEqual(floor_above["level"], 1)
        self.assertFalse(floor_above["breach"])  # locks 6.24 USDT > buffer

    def test_peak_below_1r_has_no_floor_and_giveback_not_actionable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._db_root(root, peak_upl=12.0)  # +0.40R
            result = self._run(root, _crv_like_facts(live_sl="0.258"))
        context = result["positions"][0]["open_plan_and_path"]
        floor = context["protection_floor"]
        self.assertEqual(floor["level"], 0)
        self.assertFalse(floor["breach"])
        self.assertEqual(floor["reason"], "peak_below_1r_no_floor_required")
        self.assertIsNone(floor["suggested_min_sl_px"])
        self.assertFalse(context["giveback_pct_actionable"])
        self.assertEqual(result["protection_floor_breach_count"], 0)

    def test_reduce_only_runner_is_restated_per_contract(self):
        # Half was reduced (5200 -> 2600). The floor must still work for the
        # runner: peak per contract 139.44/5200 restated to 2600 = 69.72 USDT,
        # initial risk of the current size 15.08 USDT, peak 4.62R unchanged.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._db_root(root, peak_upl=139.44, remaining_sz=2600)
            result = self._run(root, _crv_like_facts(
                live_sl="0.258", contracts="2600"))
        context = result["positions"][0]["open_plan_and_path"]
        floor = context["protection_floor"]
        self.assertEqual(context["status"], "unique_open_leg")
        self.assertFalse(context["path_size_comparable"])
        self.assertEqual(
            context["path_basis"], "per_contract_restated_to_current_size")
        self.assertAlmostEqual(context["initial_risk_usdt"], 30.16, places=2)
        self.assertAlmostEqual(
            context["initial_risk_current_size_usdt"], 15.08, places=2)
        self.assertAlmostEqual(context["observed_peak_upl_usdt"], 69.72, places=2)
        self.assertAlmostEqual(context["observed_peak_r_gross"], 4.6233, places=3)
        self.assertEqual(floor["status"], "evaluated")
        self.assertEqual(floor["level"], 2)
        self.assertTrue(floor["breach"])
        self.assertAlmostEqual(floor["required_protected_r"], 2.3116, places=3)
        # same price floor as the full-size case: R per contract is unchanged
        self.assertAlmostEqual(floor["suggested_min_sl_px"], 0.27721, places=4)
        self.assertEqual(result["protection_floor_breach_count"], 1)

    def test_reduce_path_without_snapshot_size_stays_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._db_root(root, peak_upl=139.44, remaining_sz=2600,
                          snapshot_sz=False)
            result = self._run(root, _crv_like_facts(
                live_sl="0.258", contracts="2600"))
        context = result["positions"][0]["open_plan_and_path"]
        floor = context["protection_floor"]
        self.assertIsNone(context["observed_peak_r_gross"])
        self.assertEqual(floor["status"], "unavailable")
        self.assertFalse(floor["breach"])
        self.assertEqual(floor["reason"], "peak_or_initial_risk_unavailable")
        self.assertEqual(result["protection_floor_breach_count"], 0)

    def test_multiple_open_legs_stay_not_comparable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._db_root(root, peak_upl=139.44)
            connection = sqlite3.connect(root / "account.db")
            connection.execute(
                "INSERT INTO trade_experiences VALUES (?,?,?,?,?,?,?,?,?,?)",
                (289, "2026-08-12T10:00", "2026-08-12 10:12:00", "live",
                 "CRV-USDT-SWAP", "long", "open", 1000, 1000,
                 json.dumps({"fill_px": 0.2700, "sl_trigger_px": 0.2600})),
            )
            connection.commit()
            connection.close()
            result = self._run(root, _crv_like_facts(
                live_sl="0.258", contracts="6200"))
        context = result["positions"][0]["open_plan_and_path"]
        self.assertEqual(context["status"], "unavailable")
        self.assertEqual(context["reason"], "multiple_open_legs_not_aggregated")
        self.assertNotIn("protection_floor", context)
        self.assertEqual(result["protection_floor_breach_count"], 0)


if __name__ == "__main__":
    unittest.main()
