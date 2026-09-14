# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_complete_cycle_sla as audit  # noqa: E402


class CompleteCycleForwardAuditTests(unittest.TestCase):
    def test_minimal_policy_quality_summary_retires_mtf_and_six_card_gates(self):
        rows = [{
            "cycle_id": "2026-09-02T12:00",
            "slot_minute": "00",
            "mtf_deep_dive_count": None,
            "candidate_review_count": 3,
        }]
        with mock.patch.object(
            audit.thresholds,
            "MINIMAL_DECISION_CONTRACT_ACTIVATION_CST",
            "2026-09-02T12:00:00+08:00",
        ):
            summary = audit._coverage_quality_group("minimal", rows)
        quality = summary["analysis_and_candidate_coverage"]
        self.assertEqual(0, quality["three_period_judgment_applicable_cycles"])
        self.assertEqual(1, quality["three_period_judgment_retired_cycles"])
        self.assertEqual(3, quality["side_neutral_candidate_reviews"])
        self.assertEqual(1, quality["six_field_decision_card_retired_cycles"])
        self.assertFalse(quality["minimal_missing_six_field_card_is_failure"])

    def test_closed_tier_result_is_frozen_across_flat_status_retention(self):
        prior = audit.thresholds.sla_pass_rate_prior_tier_registrations(
            "2026-08-29T11:00:00+08:00")[0]
        payload = {
            "generated_at": "2026-08-29 00:53:50+0800",
            "strict_sla": {"pass_rate_tier": {"prior_tiers": [{
                "tier": 1,
                "planned_cycles": 512,
                "strict_cycle_passes": 448,
                "strict_pass_rate": 0.875,
                "status": "MET",
            }]}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "closure.json"
            raw = json.dumps(payload, sort_keys=True).encode("utf-8")
            path.write_bytes(raw)
            with (
                mock.patch.object(audit, "TIER1_CLOSED_EVIDENCE", path),
                mock.patch.object(
                    audit,
                    "TIER1_CLOSED_EVIDENCE_SHA256",
                    hashlib.sha256(raw).hexdigest(),
                ),
            ):
                frozen = audit._frozen_prior_tier_result(
                    prior,
                    forward_start=audit.parse_cst(
                        "2026-08-15T00:00:00+08:00"),
                    as_of=audit.parse_cst("2026-08-29T11:00:00+08:00"),
                )
                narrow = audit._frozen_prior_tier_result(
                    prior,
                    forward_start=audit.parse_cst(
                        "2026-08-27T01:45:00+08:00"),
                    as_of=audit.parse_cst("2026-08-29T11:00:00+08:00"),
                )
        self.assertIsNotNone(frozen)
        self.assertEqual(448, frozen["strict_cycle_passes"])
        self.assertEqual("MET", frozen["status"])
        self.assertTrue(frozen["frozen_closed_result"])
        self.assertIsNone(narrow)

    def test_recovery_projection_keeps_failures_and_does_not_move_tier(self):
        projection = audit._pass_rate_recovery_projection(
            passes=42,
            planned=66,
            target_rate=0.80,
            minimum_slots=96,
        )

        self.assertTrue(projection["diagnostic_only"])
        self.assertEqual(
            30, projection["additional_all_pass_cycles_to_minimum_slots"])
        self.assertEqual({
            "planned_cycles": 96,
            "strict_cycle_passes": 72,
            "strict_pass_rate": 0.75,
            "target_reachable": False,
        }, projection["best_case_at_minimum_or_current_denominator"])
        self.assertEqual(
            54, projection["minimum_additional_all_pass_cycles_to_target"])
        self.assertEqual(
            120,
            projection["earliest_total_cycles_at_target_if_no_more_failures"],
        )
        self.assertEqual(
            96,
            projection["earliest_total_passes_at_target_if_no_more_failures"],
        )
        self.assertTrue(projection["finite_recovery_possible"])

    @staticmethod
    def _write_status(
        root: Path,
        cycle: str,
        completed_at: str,
        *,
        started_at: str | None = None,
        duration_ms: int | None = None,
        child_budget_seconds: float | None = None,
        v4: bool = False,
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
        if v4:
            live["collection_gate"] = {
                "schema_version": 1,
                "status": "met",
                "cycle_id": cycle,
                "required_sources": ["fast"],
                "completed_at": completed_at,
                "elapsed_seconds": 60,
                "time_threshold_seconds": None,
            }
            live["business_check"] = {
                "ok": True,
                "business_terminal": {
                    "schema_version": 1,
                    "cycle_id": cycle,
                    "status": "completed",
                    "completed_at_cst": completed_at,
                },
            }
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

    @staticmethod
    def _create_openclaw_cron_db(path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript("""
                CREATE TABLE cron_jobs(
                    store_key TEXT,job_id TEXT,name TEXT,enabled INTEGER,
                    schedule_kind TEXT,schedule_expr TEXT,schedule_tz TEXT,
                    payload_kind TEXT,payload_timeout_seconds INTEGER,
                    running_at_ms INTEGER,last_run_at_ms INTEGER,
                    last_run_status TEXT,last_duration_ms INTEGER
                );
                CREATE TABLE cron_run_logs(
                    store_key TEXT,job_id TEXT,seq INTEGER,status TEXT,
                    duration_ms INTEGER
                );
            """)
            connection.execute(
                "INSERT INTO cron_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "default", "dreaming", "Memory Dreaming Promotion", 1,
                    "cron", "0 3 * * *", None, "agentTurn", 3600,
                    None, 1_786_993_200_000, "ok", 1_200_000,
                ),
            )
            connection.executemany(
                "INSERT INTO cron_run_logs VALUES(?,?,?,?,?)",
                [
                    ("default", "dreaming", seq, "ok", duration)
                    for seq, duration in enumerate(
                        (1_020_000, 1_080_000, 1_140_000, 1_200_000), 1)
                ],
            )
            connection.commit()
        finally:
            connection.close()

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

    def test_registered_first_tier_uses_only_post_activation_cycles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status"
            mtf = root / "mtf"
            status.mkdir()
            mtf.mkdir()
            analysis = root / "analysis.db"
            self._create_analysis(analysis)
            cycle = "2026-08-20T18:00"
            safe = cycle.replace(":", "-")
            (status / f"live-{safe}.json").write_text(json.dumps({
                "stage": "live",
                "cycle_id": cycle,
                "status": "succeeded",
                "returncode": 0,
                "report_reconcile_barrier": {
                    "required": True,
                    "profile": "live",
                    "cycle_id": cycle,
                    "status": "ok",
                    "rc": 0,
                    "contract_valid": True,
                    "report_safe": True,
                    "started_at": "2026-08-20 18:13:00",
                    "finished_at": "2026-08-20 18:13:01",
                },
            }), encoding="utf-8")
            result = audit.audit_complete_cycle_sla(
                forward_start=audit.parse_cst(
                    "2026-08-20T18:00:00+08:00"),
                as_of=audit.parse_cst("2026-08-20T18:29:59+08:00"),
                finality_seconds=900,
                minimum_slots=96,
                status_dir=status,
                analysis_db=analysis,
                mtf_dir=mtf,
            )
        tier = result["strict_sla"]["pass_rate_tier"]
        self.assertEqual(0.80, tier["target_rate"])
        self.assertEqual(1, tier["planned_cycles"])
        self.assertEqual(1.0, tier["strict_pass_rate"])
        self.assertEqual("PENDING_FORWARD_EVIDENCE", tier["status"])
        self.assertFalse(tier["next_tier_registered"])

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
        taxonomy = result["strict_sla"]["failure_taxonomy"]
        self.assertEqual(
            {"live_stage_not_succeeded": 1},
            taxonomy["sla_reason_counts"],
        )
        self.assertEqual(
            {"cycle_deadline_exceeded": 1},
            taxonomy["live_failure_kind_counts"],
        )

    def test_tier2_uses_new_window_and_retains_tier1_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status"
            mtf = root / "mtf"
            status.mkdir()
            mtf.mkdir()
            analysis = root / "analysis.db"
            self._create_analysis(analysis)
            for cycle, completed_at in (
                ("2026-08-27T01:45", "2026-08-27 01:50:00"),
                ("2026-08-27T02:00", "2026-08-27 02:05:00"),
                ("2026-08-27T02:15", "2026-08-27 02:20:00"),
            ):
                self._write_status(status, cycle, completed_at, v4=True)

            result = audit.audit_complete_cycle_sla(
                forward_start=audit.parse_cst(
                    "2026-08-27T01:45:00+08:00"),
                as_of=audit.parse_cst("2026-08-27T02:44:59+08:00"),
                finality_seconds=900,
                minimum_slots=96,
                status_dir=status,
                analysis_db=analysis,
                mtf_dir=mtf,
            )

        tier = result["strict_sla"]["pass_rate_tier"]
        self.assertEqual(2, tier["tier"])
        self.assertEqual("2026-08-27T02:00:00+08:00", tier["activation_cst"])
        self.assertEqual(0.90, tier["target_rate"])
        self.assertEqual(192, tier["minimum_slots"])
        self.assertEqual(2, tier["planned_cycles"])
        self.assertEqual(2, tier["strict_cycle_passes"])
        self.assertEqual(1.0, tier["strict_pass_rate"])
        self.assertEqual("PENDING_FORWARD_EVIDENCE", tier["status"])
        self.assertFalse(tier["next_tier_registered"])
        self.assertEqual(1, len(tier["prior_tiers"]))
        prior = tier["prior_tiers"][0]
        self.assertEqual(1, prior["tier"])
        self.assertEqual(1, prior["planned_cycles"])
        self.assertEqual(1, prior["strict_cycle_passes"])
        self.assertEqual("INSUFFICIENT_EVIDENCE", prior["status"])
        self.assertFalse(prior["historical_rejudgement"])

    def test_tier2_exact_0300_exception_changes_only_acceptance_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status"
            mtf = root / "mtf"
            status.mkdir()
            mtf.mkdir()
            analysis = root / "analysis.db"
            self._create_analysis(analysis)
            start = datetime(2026, 8, 27, 2, 0, tzinfo=audit.CST)
            for index in range(24):
                slot = start + timedelta(minutes=15 * index)
                cycle = slot.strftime("%Y-%m-%dT%H:%M")
                if cycle == "2026-08-27T03:00":
                    safe = cycle.replace(":", "-")
                    (status / f"live-{safe}.json").write_text(json.dumps({
                        "stage": "live",
                        "cycle_id": cycle,
                        "status": "failed",
                        "returncode": 88,
                        "failure_kind": "analysis_deadline_exceeded",
                        "collection_gate": {
                            "schema_version": 1,
                            "status": "met",
                            "cycle_id": cycle,
                            "required_sources": ["fast"],
                            "completed_at": "2026-08-27 03:03:00",
                            "elapsed_seconds": 180,
                            "time_threshold_seconds": None,
                        },
                    }), encoding="utf-8")
                    continue
                completed_at = (slot + timedelta(minutes=5)).strftime(
                    "%Y-%m-%d %H:%M:%S")
                self._write_status(
                    status, cycle, completed_at, v4=True)

            result = audit.audit_complete_cycle_sla(
                forward_start=audit.parse_cst(
                    "2026-08-27T02:00:00+08:00"),
                as_of=audit.parse_cst("2026-08-27T08:04:13+08:00"),
                finality_seconds=900,
                minimum_slots=96,
                status_dir=status,
                analysis_db=analysis,
                mtf_dir=mtf,
            )

        strict = result["strict_sla"]
        self.assertEqual(24, strict["planned_cycles"])
        self.assertEqual(23, strict["strict_cycle_passes"])
        self.assertEqual(1, strict["failures"])
        self.assertEqual(
            {"live_stage_not_succeeded": 1},
            strict["failure_taxonomy"]["sla_reason_counts"],
        )
        self.assertEqual(
            {"analysis_deadline_exceeded": 1},
            strict["failure_taxonomy"]["live_failure_kind_counts"],
        )

        tier = strict["pass_rate_tier"]
        self.assertEqual(23, tier["planned_cycles"])
        self.assertEqual(23, tier["strict_cycle_passes"])
        self.assertEqual(1.0, tier["strict_pass_rate"])
        self.assertEqual(1, tier["excluded_cycle_count"])
        self.assertEqual(
            ["2026-08-27T03:00"],
            [row["cycle_id"] for row in tier["excluded_cycles_observed"]],
        )
        self.assertEqual(24, tier["raw_including_exceptions"]["planned_cycles"])
        self.assertEqual(
            23, tier["raw_including_exceptions"]["strict_cycle_passes"])
        self.assertEqual(
            0.958333,
            tier["raw_including_exceptions"]["strict_pass_rate"],
        )
        self.assertTrue(tier["historical_rejudgement"])
        self.assertTrue(tier["raw_cycle_facts_preserved"])
        excluded = next(
            row for row in result["cycles"]
            if row["cycle_id"] == "2026-08-27T03:00"
        )
        self.assertFalse(excluded["pass_rate_acceptance"]["included"])
        self.assertFalse(excluded["complete_cycle_sla"]["strict_cycle_pass"])

    def test_proven_collection_failure_is_not_mislabeled_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status"
            mtf = root / "mtf"
            status.mkdir()
            mtf.mkdir()
            analysis = root / "analysis.db"
            self._create_analysis(analysis)
            cycle = "2026-08-21T18:00"
            safe = cycle.replace(":", "-")
            (status / f"push-{safe}.json").write_text(json.dumps({
                "stage": "push",
                "cycle_id": cycle,
                "mode": "failure_report",
                "status": "succeeded",
                "returncode": 0,
                "post_live_reconcile": {
                    "rc": 0,
                    "timed_out": False,
                },
                "complete_cycle_sla": {
                    "schema_version": 4,
                    "measurement": (
                        "cycle_start_to_successful_analysis_judgment_trade_terminal"
                    ),
                    "threshold_seconds": 870,
                    "comparison": "<",
                    "complete": False,
                    "under_14m30": False,
                    "strict_cycle_pass": False,
                    "status": "incomplete",
                    "reason": "upstream_collection_failed",
                    "upstream_failure_kind": "collection_gate_failed",
                },
            }), encoding="utf-8")

            result = audit.audit_complete_cycle_sla(
                forward_start=audit.parse_cst(
                    "2026-08-21T18:00:00+08:00"),
                as_of=audit.parse_cst("2026-08-21T18:29:59+08:00"),
                finality_seconds=900,
                minimum_slots=96,
                status_dir=status,
                analysis_db=analysis,
                mtf_dir=mtf,
            )

        row = result["cycles"][0]["complete_cycle_sla"]
        self.assertEqual("upstream_collection_failed", row["reason"])
        self.assertEqual("collection_gate_failed", row["upstream_failure_kind"])
        self.assertFalse(row["strict_cycle_pass"])
        self.assertEqual(
            {"upstream_collection_failed": 1},
            result["strict_sla"]["failure_taxonomy"]["sla_reason_counts"],
        )

    def test_identity_bound_terminal_model_error_has_specific_taxonomy(self):
        live = {
            "status": "failed",
            "gateway_abort": {
                "status": "gateway-terminal-error",
                "terminal_confirmed": True,
                "verification_source": "identity_bound_wrapper_receipt",
            },
            "same_connection_abort": {
                "receipt_valid": True,
                "receipt": {
                    "gateway_terminal_error_observed": True,
                    "gateway_terminal_error_marker": "all_models_failed",
                    "exit_code": 1,
                },
            },
        }
        self.assertEqual(
            "gateway_terminal_error", audit._live_failure_kind(live))
        live["gateway_abort"]["terminal_confirmed"] = False
        self.assertIsNone(audit._live_failure_kind(live))

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
        self.assertEqual(
            {}, by_slot[":00"]["failure_taxonomy"]["sla_reason_counts"])

    def test_openclaw_long_agent_cron_is_diagnostic_not_sla_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            openclaw_db = root / "openclaw.sqlite"
            self._create_openclaw_cron_db(openclaw_db)
            rows = [
                {
                    "cycle_id": "2026-08-16T03:00",
                    "complete_cycle_sla": {
                        "complete": False,
                        "under_14m30": False,
                    },
                },
                {
                    "cycle_id": "2026-08-17T03:00",
                    "complete_cycle_sla": {
                        "complete": False,
                        "under_14m30": False,
                    },
                },
            ]

            result = audit.openclaw_contention_observation(
                openclaw_db, rows, run_sample_size=14)

        self.assertEqual("OBSERVED", result["status"])
        self.assertEqual(1, result["high_risk_job_count"])
        job = result["jobs"][0]
        self.assertEqual("03:00", job["exact_daily_slot_cst"])
        self.assertEqual(1110.0, job["duration_seconds"]["median"])
        self.assertTrue(job["median_longer_than_trading_cadence"])
        self.assertEqual(
            "HIGH_CONFIDENCE_SCHEDULED_GATEWAY_OVERLAP",
            job["risk_classification"],
        )
        self.assertEqual(2, job["same_clock_cycle_summary"]["failures"])
        self.assertEqual(0.0, job["same_clock_cycle_summary"]["strict_pass_rate"])
        self.assertEqual("diagnostic_only", result["strict_sla_effect"])
        self.assertEqual(
            "scheduled_overlap_is_not_unique_causal_proof",
            result["causality"],
        )

    def test_openclaw_contention_missing_db_is_non_blocking_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = audit.openclaw_contention_observation(
                Path(temporary) / "missing.sqlite", [])
        self.assertEqual("UNAVAILABLE", result["status"])
        self.assertEqual("openclaw_db_missing", result["reason"])
        self.assertEqual([], result["jobs"])

    def test_openclaw_receipts_and_json_schedule_are_observed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'state.sqlite'
            con=sqlite3.connect(path)
            try:
                con.executescript('''
                    CREATE TABLE cron_jobs(store_key TEXT,job_id TEXT,name TEXT,enabled INTEGER,payload_kind TEXT,job_json TEXT,state_json TEXT);
                    CREATE TABLE cron_run_receipts(store_key TEXT,job_id TEXT,status TEXT,started_at_ms INTEGER,finished_at_ms INTEGER);
                ''')
                con.execute('INSERT INTO cron_jobs VALUES(?,?,?,?,?,?,?)',('s','j','Memory Dreaming Promotion',1,'agentTurn',json.dumps({'schedule':{'kind':'cron','expr':'0 3 * * *','tz':'Asia/Shanghai'}}),json.dumps({'lastRunStatus':'ok','lastRunAtMs':1000})))
                con.execute("INSERT INTO cron_run_receipts VALUES('s','j','ok',1000,1033000)")
                con.commit()
            finally:
                con.close()
            result=audit.openclaw_contention_observation(path,[{'cycle_id':'2026-09-05T03:00','complete_cycle_sla':{'strict_cycle_pass':False}}])
        self.assertEqual(result['status'],'OBSERVED')
        self.assertEqual(result['jobs'][0]['duration_seconds']['median'],1032.0)
        self.assertEqual(result['jobs'][0]['same_clock_cycle_summary']['failures'],1)
        self.assertEqual(result['strict_sla_effect'],'diagnostic_only')

    def test_coverage_and_quality_observation_uses_real_denominators(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status"
            mtf = root / "mtf"
            collect_logs = root / "collect"
            briefing_logs = root / "briefing"
            status.mkdir()
            mtf.mkdir()
            collect_logs.mkdir()
            briefing_logs.mkdir()
            analysis = root / "analysis.db"
            live_trades = root / "live_trades.db"
            cycle = "2026-08-15T00:15"

            self._write_status(
                status, cycle, "2026-08-15 00:25:00",
                started_at="2026-08-15 00:17:00",
                duration_ms=480_000,
                child_budget_seconds=660,
            )
            self._write_mtf(mtf, cycle, "BTC-USDT-SWAP")

            complete_card = {
                "direction_evidence": ["direction"],
                "opposing_evidence": ["opposition"],
                "execution_conditions": "condition",
                "invalidation_point": "invalidation",
                "risk_reward": {
                    "entry": 100, "stop": 90, "target": 120, "rr": 2.0,
                    "exit_mode": "fixed_tp",
                },
                "portfolio_impact": "bounded",
                "historical_experience": {
                    "matched_wins": [],
                    "matched_losses": [],
                    "missed_opportunities": [],
                    "usage": "none",
                    "reason": "no comparable sample",
                },
                "agent_judgement": "open only if conditions remain valid",
                "reference_overrides": [],
            }
            connection = sqlite3.connect(analysis)
            try:
                connection.execute(
                    "CREATE TABLE analysis_runs("
                    "cycle_id TEXT PRIMARY KEY,status TEXT,raw TEXT)"
                )
                connection.execute(
                    "CREATE TABLE analysis_signals("
                    "cycle_id TEXT,symbol TEXT,action TEXT,decision_card TEXT)"
                )
                connection.execute(
                    "INSERT INTO analysis_runs VALUES(?,?,?)",
                    (
                        cycle,
                        "ok",
                        json.dumps({"raw": {
                            "candidates_deep_dived": [
                                {
                                    "instId": "BTC-USDT-SWAP",
                                    "evidence_hash": "a" * 64,
                                    "decision": "reject",
                                    "reason": "evidence does not support entry",
                                },
                                {
                                    "instId": "ETH-USDT-SWAP",
                                    "evidence_hash": "b" * 64,
                                    "decision": "reject",
                                    "reason": "structure is incomplete",
                                },
                                {"instId": "BAD-USDT-SWAP"},
                            ],
                            "candidate_evidence_shortfall": {
                                "observed_candidate_count": 4,
                                "reason": "remaining budget cannot finish more evidence",
                            },
                        }}),
                    ),
                )
                connection.execute(
                    "INSERT INTO analysis_signals VALUES(?,?,?,?)",
                    (
                        cycle, "SOL-USDT-SWAP", "open_long",
                        json.dumps(complete_card),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            connection = sqlite3.connect(live_trades)
            try:
                connection.execute(
                    "CREATE TABLE trade_cycles("
                    "cycle_id TEXT PRIMARY KEY,mode TEXT,raw TEXT)"
                )
                connection.execute(
                    "INSERT INTO trade_cycles VALUES(?,?,?)",
                    (
                        cycle,
                        "live",
                        json.dumps({
                            "live_facts": {
                                "cycle_id": cycle,
                                "profile": "live",
                                "status": "ok",
                                "positions": [
                                    {"instId": "BTC-USDT-SWAP", "sz": "1"},
                                    {"instId": "ETH-USDT-SWAP", "sz": "2"},
                                ],
                            },
                            "decision_card": {
                                "agent_judgement": (
                                    "BTC-USDT-SWAP HOLD because protection is valid; "
                                    "ETH-USDT-SWAP CLOSE because thesis failed"
                                ),
                            },
                        }),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            (collect_logs / "collect_cycle_20260815.jsonl").write_text(
                json.dumps({
                    "cycle": cycle,
                    "ok": True,
                    "steps": [{
                        "name": "fast",
                        "data_quality": {
                            "expected": 4,
                            "tickers": 4,
                            "candle_transport": {"usable_symbols": 3},
                            "contract_official_universe_alignment": {
                                "primary_selected_symbols": 4,
                            },
                        },
                    }],
                }) + "\n",
                encoding="utf-8",
            )
            (briefing_logs / "candidates-20260815.jsonl").write_text(
                json.dumps({
                    "schema": "briefing_candidates_v1",
                    "cycle_id": cycle,
                    "written_at_cst": "2026-08-15 00:16:00",
                    "candidates": [
                        {"layer": "mature", "symbol": "BTC-USDT-SWAP", "side": "long"},
                        {"layer": "mature", "symbol": "ETH-USDT-SWAP", "side": "short"},
                        {"layer": "early", "symbol": "SOL-USDT-SWAP", "side": "long"},
                        {"layer": "early", "symbol": "XRP-USDT-SWAP", "side": "long"},
                    ],
                }) + "\n",
                encoding="utf-8",
            )

            result = audit.audit_complete_cycle_sla(
                forward_start=audit.parse_cst("2026-08-15T00:15:00+08:00"),
                as_of=audit.parse_cst("2026-08-15T00:44:59+08:00"),
                finality_seconds=900,
                minimum_slots=1,
                status_dir=status,
                analysis_db=analysis,
                mtf_dir=mtf,
                live_trades_db=live_trades,
                collect_log_dir=collect_logs,
                briefing_log_dir=briefing_logs,
            )

        observation = result["coverage_and_quality_observation"]
        self.assertTrue(observation["diagnostic_only"])
        self.assertEqual("none", observation["strict_sla_effect"])
        overall = observation["overall"]
        self.assertEqual(
            1.0, overall["collection_universe"]["ticker_coverage"]["rate"])
        self.assertEqual(
            0.75, overall["collection_universe"]["candle_coverage"]["rate"])
        analysis_summary = overall["analysis_and_candidate_coverage"]
        self.assertEqual(1, analysis_summary["independently_validated_mtf_evidence"])
        self.assertEqual(2, analysis_summary["valid_persisted_deep_dives"])
        self.assertEqual(1, analysis_summary["invalid_persisted_deep_dives"])
        self.assertEqual(1, analysis_summary["complete_final_open_cards"])
        self.assertEqual(4, analysis_summary["observed_candidate_pool"])
        self.assertEqual(2, analysis_summary["not_deep_dived_candidates"])
        self.assertEqual(2, analysis_summary["remaining_budget_limited_candidates"])
        funnel = overall["candidate_funnel"]
        self.assertEqual(1, funnel["observed_cycles"])
        self.assertEqual(1, funnel["briefing_source_cycles"])
        self.assertEqual(4, funnel["candidate_pool"])
        self.assertEqual(2, funnel["observed_deep_dives"])
        self.assertEqual(1, funnel["independently_validated_mtf_evidence"])
        self.assertEqual(2, funnel["valid_persisted_deep_dive_receipts"])
        self.assertEqual(0.5, funnel["deep_dive_coverage_rate"])
        self.assertEqual(2, funnel["remaining_budget_limited_candidates"])
        self.assertEqual(0, funnel["source_mismatch_cycles"])
        self.assertEqual(1, funnel["deep_dive_source_mismatch_cycles"])
        position_summary = overall["position_review"]
        self.assertEqual(0, position_summary["positions_expected_review"])
        self.assertEqual(0, position_summary["positions_explicitly_reviewed"])
        self.assertIsNone(position_summary["explicit_review_rate"])
        pre_review = position_summary["pre_activation_diagnostic"]
        self.assertEqual(2, pre_review["positions_expected_review"])
        self.assertEqual(
            2,
            pre_review[
                "positions_explicitly_reviewed_under_historical_semantics"
            ],
        )
        by_slot = {item["slot"]: item for item in observation["by_slot"]}
        self.assertEqual(1, by_slot[":15"]["planned_cycles"])
        active_by_slot = {
            item["slot"]: item
            for item in observation["active_v4_process_scope"]["by_slot"]
        }
        pre_by_slot = {
            item["slot"]: item
            for item in observation["pre_activation_diagnostic"]["by_slot"]
        }
        self.assertEqual(0, active_by_slot[":15"]["planned_cycles"])
        self.assertEqual(1, pre_by_slot[":15"]["planned_cycles"])

    def test_structured_position_review_is_forward_only_and_reasoned(self):
        payload = {
            "decision_card": {"agent_judgement": ""},
            "requested_position_actions": [{
                "action": "HOLD",
                "symbol": "BTC-USDT-SWAP",
                "reasoning": "保护有效，继续持有",
            }],
        }
        self.assertFalse(audit._explicit_position_review(
            payload, "BTC-USDT-SWAP", "2026-08-20T17:45"))
        self.assertTrue(audit._explicit_position_review(
            payload, "BTC-USDT-SWAP", "2026-08-20T18:00"))
        payload["requested_position_actions"][0]["reasoning"] = ""
        self.assertFalse(audit._explicit_position_review(
            payload, "BTC-USDT-SWAP", "2026-08-20T18:00"))

    def test_final_open_count_is_not_candidate_pool_denominator(self):
        self.assertIsNone(audit._candidate_pool_count(
            {"open_candidate_count": 0}, {}))
        self.assertEqual(16, audit._candidate_pool_count(
            {"named_open_candidates_in_briefing": 16}, {}))

    def test_model_failure_observation_separates_provider_causes(self):
        parsed = audit.parse_model_failure_line(
            "GatewayClientRequestError: FallbackSummaryError: All models "
            "failed (3): xai/grok: Connection error. (timeout) | "
            "minimax/M3: LLM idle timeout (120s): no response from model "
            "(timeout) | glm/5.3: 429 monthly usage quota exceeded"
        )
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["complete"])
        self.assertEqual(3, parsed["parsed_model_count"])
        self.assertEqual(
            [
                "connection_or_request_timeout",
                "idle_timeout",
                "quota_exceeded",
            ],
            [item["error_kind"] for item in parsed["attempts"]],
        )
        observation = audit.model_failure_observation([{
            "cycle_id": "2026-08-21T18:00",
            "slot_minute": "00",
            "live_failure_kind": "gateway_terminal_error",
            "model_failure_observation": parsed,
        }])
        active = observation["active_v4_process_scope"]["summary"]
        self.assertEqual(1, active["gateway_terminal_error_cycles"])
        self.assertEqual(3, active["provider_attempts"])
        self.assertEqual({
            "connection_or_request_timeout": 1,
            "idle_timeout": 1,
            "quota_exceeded": 1,
        }, active["attempts_by_error_kind"])


if __name__ == "__main__":
    unittest.main()
