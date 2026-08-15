# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_complete_cycle_sla as audit  # noqa: E402


class CompleteCycleForwardAuditTests(unittest.TestCase):
    @staticmethod
    def _write_status(
        root: Path,
        cycle: str,
        completed_at: str,
        *,
        started_at: str | None = None,
        duration_ms: int | None = None,
        child_budget_seconds: float | None = None,
    ) -> None:
        safe = cycle.replace(":", "-")
        live = {
            "stage": "live", "cycle_id": cycle, "status": "succeeded",
            "returncode": 0,
        }
        if started_at is not None:
            live["started_at"] = started_at
        if duration_ms is not None:
            live["duration_ms"] = duration_ms
        if child_budget_seconds is not None:
            live["child_budget_seconds"] = child_budget_seconds
        (root / f"live-{safe}.json").write_text(
            json.dumps(live), encoding="utf-8")
        monitor = {
            "rc": 0,
            "output": json.dumps({
                "ts": completed_at,
                "cycle_id": cycle,
                "profile": "live",
                "ok": True,
                "issue": False,
            }),
        }
        (root / f"push-{safe}.json").write_text(json.dumps({
            "stage": "push", "cycle_id": cycle, "status": "succeeded",
            "post_live_reconcile": monitor,
        }), encoding="utf-8")

    @staticmethod
    def _create_analysis(path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE analysis_signals(cycle_id TEXT,action TEXT)")
            connection.execute(
                "INSERT INTO analysis_signals VALUES(?,?)",
                ("2026-08-15T00:00", "open_long"),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _write_mtf(root: Path, cycle: str, symbol: str) -> None:
        safe = cycle.replace(":", "-")
        (root / f"mtf_{safe}_{symbol}.json").write_text(json.dumps({
            "ok": True,
            "status": "PASSED",
            "cycle_id": cycle,
            "symbol": symbol,
            "production_database_writes": 0,
            "orders_placed": 0,
            "evidence_contract": {
                "protocol": "multitimeframe_market_evidence_v1",
                "cycle_id": cycle,
                "symbol": symbol,
                "required_timeframes": ["15m", "1H", "4H"],
                "timeframes": {
                    "15m": {"ready": True},
                    "1H": {"ready": True},
                    "4H": {"ready": True},
                },
                "evidence_hash": "a" * 64,
            },
        }), encoding="utf-8")

    def test_planned_denominator_keeps_exactly_fourteen_thirty_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status"
            mtf = root / "mtf"
            status.mkdir()
            mtf.mkdir()
            analysis = root / "analysis.db"
            self._create_analysis(analysis)
            self._write_status(
                status, "2026-08-15T00:00", "2026-08-15 00:13:59")
            self._write_status(
                status, "2026-08-15T00:15", "2026-08-15 00:29:30")
            self._write_mtf(mtf, "2026-08-15T00:00", "BTC-USDT-SWAP")
            self._write_mtf(mtf, "2026-08-15T00:00", "ETH-USDT-SWAP")

            result = audit.audit_complete_cycle_sla(
                forward_start=audit.parse_cst("2026-08-15T00:00:00+08:00"),
                as_of=audit.parse_cst("2026-08-15T00:44:59+08:00"),
                finality_seconds=900,
                minimum_slots=96,
                status_dir=status,
                analysis_db=analysis,
                mtf_dir=mtf,
            )

        self.assertEqual(2, result["strict_sla"]["planned_cycles"])
        self.assertEqual(1, result["strict_sla"]["strictly_under_14m30"])
        self.assertEqual(1, result["strict_sla"]["failures"])
        self.assertEqual("NOT_MET", result["strict_sla"]["status"])
        self.assertEqual(
            "late", result["cycles"][1]["complete_cycle_sla"]["status"])
        self.assertEqual(2, result["cycles"][0]["mtf_deep_dive_count"])
        self.assertEqual(1, result["cycles"][0]["final_open_card_count"])

    def test_clean_short_window_remains_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status"
            mtf = root / "mtf"
            status.mkdir()
            mtf.mkdir()
            analysis = root / "analysis.db"
            self._create_analysis(analysis)
            self._write_status(
                status, "2026-08-15T00:00", "2026-08-15 00:13:59")
            result = audit.audit_complete_cycle_sla(
                forward_start=audit.parse_cst("2026-08-15T00:00:00+08:00"),
                as_of=audit.parse_cst("2026-08-15T00:29:59+08:00"),
                finality_seconds=900,
                minimum_slots=96,
                status_dir=status,
                analysis_db=analysis,
                mtf_dir=mtf,
            )
        self.assertEqual("PENDING_FORWARD_EVIDENCE", result["strict_sla"]["status"])
        self.assertEqual(1.0, result["strict_sla"]["strict_pass_rate"])

    def test_cached_met_sla_cannot_override_failed_live_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status"
            mtf = root / "mtf"
            status.mkdir()
            mtf.mkdir()
            analysis = root / "analysis.db"
            self._create_analysis(analysis)
            cycle = "2026-08-15T00:00"
            safe = cycle.replace(":", "-")
            (status / f"live-{safe}.json").write_text(json.dumps({
                "stage": "live",
                "cycle_id": cycle,
                "status": "failed",
                "returncode": 124,
                "failure_kind": "cycle_deadline_exceeded",
            }), encoding="utf-8")
            monitor = {
                "rc": 0,
                "output": json.dumps({
                    "ts": "2026-08-15 00:13:26",
                    "cycle_id": cycle,
                    "profile": "live",
                    "ok": True,
                    "issue": False,
                }),
            }
            (status / f"push-{safe}.json").write_text(json.dumps({
                "stage": "push",
                "cycle_id": cycle,
                "status": "succeeded",
                "post_live_reconcile": monitor,
                "complete_cycle_sla": {
                    "complete": True,
                    "under_14m30": True,
                    "status": "met",
                    "elapsed_seconds": 806,
                },
            }), encoding="utf-8")

            result = audit.audit_complete_cycle_sla(
                forward_start=audit.parse_cst("2026-08-15T00:00:00+08:00"),
                as_of=audit.parse_cst("2026-08-15T00:29:59+08:00"),
                finality_seconds=900,
                minimum_slots=96,
                status_dir=status,
                analysis_db=analysis,
                mtf_dir=mtf,
            )

        row = result["cycles"][0]["complete_cycle_sla"]
        self.assertEqual("incomplete", row["status"])
        self.assertFalse(row["complete"])
        self.assertEqual("live_stage_not_succeeded", row["reason"])
        self.assertEqual(0, result["strict_sla"]["complete_cycles"])

    def test_slot_observation_separates_hourly_and_later_round_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status"
            mtf = root / "mtf"
            status.mkdir()
            mtf.mkdir()
            analysis = root / "analysis.db"
            self._create_analysis(analysis)
            fixtures = [
                ("2026-08-15T00:00", "2026-08-15 00:05:00", 300, 480),
                ("2026-08-15T00:15", "2026-08-15 00:17:00", 120, 660),
                ("2026-08-15T00:30", "2026-08-15 00:31:30", 90, 690),
                ("2026-08-15T00:45", "2026-08-15 00:46:30", 90, 690),
            ]
            for cycle, started_at, offset, budget in fixtures:
                cycle_start = audit.parse_cst(cycle)
                completed_at = (
                    cycle_start.replace(tzinfo=None)
                    + audit.timedelta(seconds=offset + 300)
                ).strftime("%Y-%m-%d %H:%M:%S")
                self._write_status(
                    status,
                    cycle,
                    completed_at,
                    started_at=started_at,
                    duration_ms=300_000,
                    child_budget_seconds=budget,
                )
                depth = 2 if cycle.endswith(":00") else 3
                for index in range(depth):
                    self._write_mtf(
                        mtf, cycle, f"S{index}-{cycle[-2:]}-USDT-SWAP")

            result = audit.audit_complete_cycle_sla(
                forward_start=audit.parse_cst("2026-08-15T00:00:00+08:00"),
                as_of=audit.parse_cst("2026-08-15T01:14:59+08:00"),
                finality_seconds=900,
                minimum_slots=4,
                status_dir=status,
                analysis_db=analysis,
                mtf_dir=mtf,
            )

        observation = result["slot_observation"]
        by_slot = {row["slot"]: row for row in observation["by_slot"]}
        self.assertEqual(300, by_slot[":00"]["live_start_offset"]["average_seconds"])
        self.assertEqual(120, by_slot[":15"]["live_start_offset"]["average_seconds"])
        self.assertEqual(2.0, by_slot[":00"]["candidate_observation"]["average_deep_dives"])
        self.assertEqual(3.0, by_slot[":45"]["candidate_observation"]["average_deep_dives"])
        comparison = observation["hourly_vs_pooled_quarter"]
        self.assertEqual(200, comparison["average_live_start_offset_delta_seconds"])
        self.assertEqual(-1.0, comparison["average_deep_dive_delta"])
        self.assertEqual(0.0, comparison["strict_pass_rate_gap_percentage_points"])
        self.assertEqual("00", result["cycles"][0]["slot_minute"])
        self.assertEqual("hourly", result["cycles"][0]["tier"])


if __name__ == "__main__":
    unittest.main()
