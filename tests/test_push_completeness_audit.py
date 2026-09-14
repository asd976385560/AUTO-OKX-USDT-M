import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_push_completeness  # noqa: E402


class PushCompletenessAuditTests(unittest.TestCase):
    CST = timezone(timedelta(hours=8))
    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports" / "agents"
        reports.mkdir(parents=True)
        stage_status = root / "stage-status"
        stage_status.mkdir()
        pipeline = root / "pipeline.jsonl"
        events = root / "events.jsonl"
        dedupe = root / "dedupe.db"
        pipeline.write_text("", encoding="utf-8")
        events.write_text("", encoding="utf-8")
        connection = sqlite3.connect(dedupe)
        try:
            connection.execute(
                "CREATE TABLE sent ("
                "k TEXT PRIMARY KEY, content_hash TEXT, status TEXT, "
                "first_seen TEXT, updated_at TEXT, preview TEXT)"
            )
            connection.commit()
        finally:
            connection.close()
        return {
            "reports_dir": reports,
            "pipeline_log": pipeline,
            "event_log": events,
            "dedupe_db": dedupe,
            "stage_status_dir": stage_status,
        }

    def _archive_attempt(
        self,
        fixture: dict[str, Path],
        *,
        cycle: str = "2026-08-01T00:00",
        suffix: str = "good",
        hard_check: bool = True,
        logged_validation_rc: int = 0,
    ) -> tuple[dict, str]:
        content = f"【{cycle[11:16]}】fixture report {suffix}\nvalid body"
        archived = f"# fixture\n\n{content}"
        path = fixture["reports_dir"] / f"v2-push-{suffix}.md"
        path.write_text(archived, encoding="utf-8")
        row = {
            "cycle": cycle,
            "ts": f"{cycle[:10]} {cycle[11:]}:30",
            "steps": {
                "build": {"ok": True},
                "render": {"rc": 0},
                "validate": {
                    "rc": logged_validation_rc,
                    "errors": [],
                    "missing": [],
                },
                "archive": {
                    "rc": 0,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "degraded": None,
                    "hard_check": hard_check,
                },
            },
        }
        return row, hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _write_pipeline(self, path: Path, rows: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )

    def _receipt(
        self,
        fixture: dict[str, Path],
        *,
        cycle: str,
        content_hash: str,
        status: str = "sent",
    ) -> None:
        key = hashlib.sha256(
            f"default|push:{cycle}".encode("utf-8")
        ).hexdigest()
        connection = sqlite3.connect(fixture["dedupe_db"])
        try:
            connection.execute(
                "INSERT INTO sent VALUES (?,?,?,?,?,?)",
                (key, content_hash, status, "fixture", "fixture", "fixture"),
            )
            connection.commit()
        finally:
            connection.close()

    def _audit(self, fixture: dict[str, Path]) -> dict:
        return audit_push_completeness.audit_push_completeness(
            start=date(2026, 8, 1),
            end=date(2026, 8, 1),
            archive_validator=lambda text: {"ok": "valid body" in text},
            evaluated_at="2026-08-12 18:00:00",
            **fixture,
        )

    def test_missing_planned_slots_remain_in_denominator(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            good, content_hash = self._archive_attempt(fixture)
            bad, _ = self._archive_attempt(
                fixture, suffix="bad", logged_validation_rc=1
            )
            self._write_pipeline(fixture["pipeline_log"], [bad, good])
            self._receipt(
                fixture,
                cycle="2026-08-01T00:00",
                content_hash=content_hash,
            )
            result = self._audit(fixture)

        self.assertEqual(96, result["counts"]["expected_slots"])
        self.assertEqual(1, result["counts"]["pipeline_present"])
        self.assertEqual(95, result["counts"]["missing_pipeline_slots"])
        self.assertEqual(2, result["counts"]["pipeline_attempts"])
        self.assertEqual(1, result["counts"]["duplicate_pipeline_attempts"])
        self.assertEqual(1, result["counts"]["report_complete"])
        self.assertEqual(1, result["counts"]["delivery_confirmed"])
        self.assertEqual(1, result["counts"]["delivered_report_complete"])
        self.assertAlmostEqual(
            1 / 96,
            result["rates"]["delivered_report_completeness_rate"],
        )
        self.assertEqual("NOT_MET", result["status"])
        self.assertEqual(95, result["daily"][0]["missing_pipeline_slots"])
        self.assertFalse(result["safety"]["auto_resend"])
        self.assertEqual(0, result["safety"]["production_database_writes"])

    def test_nonproduction_and_stale_legacy_probe_rows_are_not_pipeline_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pipeline.jsonl"
            cycle = "2026-08-01T00:00"
            rows = [
                {
                    "cycle": cycle,
                    "ts": "2026-08-01 00:05:00",
                    "execution_context": "test",
                    "natural_production_evidence": False,
                },
                {
                    "cycle": cycle,
                    "ts": "2026-08-02 12:00:00",
                },
                {
                    "cycle": cycle,
                    "ts": "2026-08-01 00:10:00",
                    "execution_context": "production",
                    "natural_production_evidence": True,
                },
            ]
            self._write_pipeline(path, rows)
            attempts, diagnostics = audit_push_completeness._pipeline_attempts(
                path, {cycle})
        self.assertEqual(1, len(attempts[cycle]))
        self.assertEqual(1, diagnostics["nonproduction_context_rows"])
        self.assertEqual(1, diagnostics["stale_legacy_probe_rows"])

    def test_delivery_hash_must_match_an_independently_valid_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            good, _ = self._archive_attempt(fixture)
            self._write_pipeline(fixture["pipeline_log"], [good])
            self._receipt(
                fixture,
                cycle="2026-08-01T00:00",
                content_hash="0" * 64,
            )
            result = self._audit(fixture)

        self.assertEqual(1, result["counts"]["report_complete"])
        self.assertEqual(1, result["counts"]["delivery_confirmed"])
        self.assertEqual(0, result["counts"]["delivered_report_complete"])
        row = next(
            item for item in result["failure_rows"]
            if item["cycle"] == "2026-08-01T00:00"
        )
        self.assertIn(
            "delivered_content_hash_not_in_valid_archives", row["reasons"]
        )

    def test_pending_and_unproven_duplicate_skip_are_not_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            good, content_hash = self._archive_attempt(fixture)
            self._write_pipeline(fixture["pipeline_log"], [good])
            self._receipt(
                fixture,
                cycle="2026-08-01T00:00",
                content_hash=content_hash,
                status="pending",
            )
            fixture["event_log"].write_text(
                json.dumps({
                    "event": "duplicate_skip",
                    "dedupe_key": "push:2026-08-01T00:00",
                    "target": "default",
                    "key": "fixture",
                    "content_hash": content_hash,
                }) + "\n",
                encoding="utf-8",
            )
            result = self._audit(fixture)

        self.assertEqual(0, result["counts"]["delivery_confirmed"])
        self.assertEqual(0, result["counts"]["delivered_report_complete"])

    def test_archive_outside_production_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self._fixture(root)
            good, content_hash = self._archive_attempt(fixture)
            outside = root / "outside.md"
            outside.write_text("# fixture\n\n【00:00】valid body", encoding="utf-8")
            good["steps"]["archive"].update({
                "path": str(outside),
                "bytes": outside.stat().st_size,
            })
            self._write_pipeline(fixture["pipeline_log"], [good])
            self._receipt(
                fixture,
                cycle="2026-08-01T00:00",
                content_hash=content_hash,
            )
            result = self._audit(fixture)

        self.assertEqual(0, result["counts"]["report_complete"])
        failure = result["failure_rows"][0]["attempt_failures"][0]
        self.assertIn(
            "archive is outside the production reports directory",
            failure["reasons"],
        )

    def test_future_business_attestations_are_required_and_must_agree(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            cycle = "2026-08-14T07:00"
            row, _ = self._archive_attempt(fixture, cycle=cycle)
            missing = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertFalse(missing["complete"])
            self.assertIn(
                "business attestation pre-archive missing or failed",
                missing["reasons"],
            )

            terminal = {
                "status": "succeeded",
                "returncode": 0,
                "finished_at": "2026-08-14 07:08:00",
                "profile_lease_released": True,
                "same_cycle_active_lease": False,
            }
            attestation = {
                "ok": True,
                "required": True,
                "mode": "business_terminal",
                "decision": "hold",
                "n_orders": 0,
                "trade_count": 0,
                "sha256": "a" * 64,
                "live_stage_terminal": terminal,
            }
            row["steps"]["business_attestation_pre_archive"] = attestation
            row["steps"]["business_attestation_pre_send"] = dict(attestation)
            complete = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertTrue(complete["complete"], complete)

            row["steps"]["business_attestation_pre_send"] = {
                **attestation, "sha256": "b" * 64,
            }
            drifted = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertFalse(drifted["complete"])
            self.assertIn(
                "business attestation sha256 drifted before send",
                drifted["reasons"],
            )

    def test_future_failure_attestation_requires_full_intent_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            cycle = "2026-08-14T07:00"
            row, _ = self._archive_attempt(fixture, cycle=cycle)
            terminal = {
                "status": "failed",
                "returncode": 86,
                "finished_at": "2026-08-14 07:08:00",
                "profile_lease_released": True,
                "same_cycle_active_lease": False,
            }
            body = {
                "schema_version": 1,
                "profile": "live",
                "cycle_id": cycle,
                "terminal": "absent",
                "trade_count": 0,
                "failure_kind": "business_output_missing",
                "intent_rows": 1,
                "failed_clean_rows": 1,
                "unsafe_rows": 0,
            }
            canonical = json.dumps(
                body, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            attestation = {
                "ok": True,
                "required": True,
                "mode": "upstream_failure",
                **body,
                "sha256": hashlib.sha256(
                    canonical.encode("utf-8")).hexdigest(),
                "live_stage_terminal": terminal,
            }
            row["steps"]["business_attestation_pre_archive"] = attestation
            row["steps"]["business_attestation_pre_send"] = dict(attestation)
            complete = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertTrue(complete["complete"], complete)

            row["steps"]["business_attestation_pre_send"] = {
                **attestation,
                "unsafe_rows": 1,
            }
            drifted = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertFalse(drifted["complete"])
            self.assertIn(
                "failure attestation unsafe_rows drifted before send",
                drifted["reasons"],
            )

    def test_inter_report_exchange_attestation_is_required_and_stable(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            cycle = "2026-08-15T08:00"
            row, _ = self._archive_attempt(fixture, cycle=cycle)
            barrier = {
                "required": True,
                "report_safe": True,
                "status": "ok",
                "rc": 0,
                "blocking": False,
            }
            terminal = {
                "status": "succeeded",
                "returncode": 0,
                "finished_at": "2026-08-15 08:08:00",
                "profile_lease_released": True,
                "same_cycle_active_lease": False,
                "report_reconcile_barrier": barrier,
            }
            attestation = {
                "ok": True,
                "required": True,
                "mode": "business_terminal",
                "decision": "hold",
                "n_orders": 0,
                "trade_count": 0,
                "sha256": "a" * 64,
                "live_stage_terminal": terminal,
            }
            row["steps"]["business_attestation_pre_archive"] = attestation
            row["steps"]["business_attestation_pre_send"] = dict(attestation)
            missing = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertFalse(missing["complete"])
            self.assertIn(
                "inter-report exchange attestation pre-archive missing",
                missing["reasons"],
            )

            interval = {
                "inter_report_exchange_required": True,
                "inter_report_exchange_schema_version": 1,
                "inter_report_fill_count": 1,
                "inter_report_sha256": "b" * 64,
                "inter_report_window_start_exclusive_cst": (
                    "2026-08-15 07:45:00"),
                "inter_report_window_end_inclusive_cst": (
                    "2026-08-15 08:00:00"),
            }
            pre_archive = {**attestation, **interval}
            row["steps"]["business_attestation_pre_archive"] = pre_archive
            row["steps"]["business_attestation_pre_send"] = dict(pre_archive)
            complete = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertTrue(complete["complete"], complete)

            schema_v2 = {
                **pre_archive,
                "inter_report_exchange_schema_version": 2,
            }
            row["steps"]["business_attestation_pre_archive"] = schema_v2
            row["steps"]["business_attestation_pre_send"] = dict(schema_v2)
            complete_v2 = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertTrue(complete_v2["complete"], complete_v2)

            unknown_schema = {
                **pre_archive,
                "inter_report_exchange_schema_version": 3,
            }
            row["steps"]["business_attestation_pre_archive"] = unknown_schema
            row["steps"]["business_attestation_pre_send"] = dict(
                unknown_schema)
            rejected_schema = (
                audit_push_completeness._validate_archive_attempt(
                    row,
                    cycle=cycle,
                    reports_dir=fixture["reports_dir"],
                    validator=lambda text: {"ok": "valid body" in text},
                )
            )
            self.assertFalse(rejected_schema["complete"])
            self.assertIn(
                "inter-report exchange attestation summary invalid",
                rejected_schema["reasons"],
            )

            row["steps"]["business_attestation_pre_archive"] = pre_archive
            row["steps"]["business_attestation_pre_send"] = {
                **pre_archive,
                "inter_report_fill_count": 2,
            }
            drifted = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertFalse(drifted["complete"])
            self.assertIn(
                "inter-report exchange attestation "
                "inter_report_fill_count drifted before send",
                drifted["reasons"],
            )

    def test_post_agent_report_barrier_is_required_from_fixed_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            cycle = "2026-08-14T19:00"
            row, _ = self._archive_attempt(fixture, cycle=cycle)
            terminal = {
                "status": "succeeded",
                "returncode": 0,
                "finished_at": "2026-08-14 19:08:00",
                "profile_lease_released": True,
                "same_cycle_active_lease": False,
            }
            attestation = {
                "ok": True,
                "required": True,
                "mode": "business_terminal",
                "decision": "hold",
                "n_orders": 0,
                "trade_count": 0,
                "sha256": "a" * 64,
                "live_stage_terminal": terminal,
            }
            row["steps"]["business_attestation_pre_archive"] = attestation
            row["steps"]["business_attestation_pre_send"] = dict(attestation)
            missing = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertFalse(missing["complete"])
            self.assertIn(
                "report reconcile barrier pre-archive incomplete",
                missing["reasons"],
            )

            barrier = {
                "required": True,
                "report_safe": True,
                "status": "ok",
                "rc": 0,
                "blocking": False,
            }
            attestation["live_stage_terminal"] = {
                **terminal,
                "report_reconcile_barrier": barrier,
            }
            row["steps"]["business_attestation_pre_archive"] = attestation
            row["steps"]["business_attestation_pre_send"] = dict(attestation)
            complete = audit_push_completeness._validate_archive_attempt(
                row,
                cycle=cycle,
                reports_dir=fixture["reports_dir"],
                validator=lambda text: {"ok": "valid body" in text},
            )
            self.assertTrue(complete["complete"], complete)

    def test_forward_window_stays_insufficient_before_96_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            result = audit_push_completeness.audit_push_completeness(
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
                forward_start=datetime(
                    2026, 8, 12, 16, 0, tzinfo=self.CST),
                as_of=datetime(2026, 8, 12, 17, 1, tzinfo=self.CST),
                finality_grace_minutes=45,
                forward_minimum_slots=96,
                archive_validator=lambda _: {"ok": True},
                **fixture,
            )

        forward = result["forward_after_remediation"]
        self.assertEqual(2, forward["counts"]["expected_slots"])
        self.assertEqual("INSUFFICIENT_EVIDENCE", forward["status"])
        self.assertEqual("PENDING_FORWARD_EVIDENCE", result["overall_status"])

    def test_forward_can_pass_only_after_96_exact_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            rows = []
            day = datetime(2026, 8, 1, 0, 0, tzinfo=self.CST)
            forward_start = datetime(2026, 8, 12, 16, 0, tzinfo=self.CST)
            cycles = [day + timedelta(minutes=15 * index) for index in range(96)]
            cycles += [
                forward_start + timedelta(minutes=15 * index)
                for index in range(96)
            ]
            for index, instant in enumerate(cycles):
                cycle = instant.strftime("%Y-%m-%dT%H:%M")
                row, content_hash = self._archive_attempt(
                    fixture,
                    cycle=cycle,
                    suffix=f"slot-{index}",
                )
                rows.append(row)
                self._receipt(
                    fixture, cycle=cycle, content_hash=content_hash)
            self._write_pipeline(fixture["pipeline_log"], rows)
            result = audit_push_completeness.audit_push_completeness(
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
                forward_start=forward_start,
                as_of=datetime(2026, 8, 13, 16, 30, tzinfo=self.CST),
                finality_grace_minutes=45,
                forward_minimum_slots=96,
                archive_validator=lambda text: {"ok": "valid body" in text},
                **fixture,
            )

        self.assertEqual("PASSED", result["status"])
        self.assertEqual(
            96,
            result["forward_after_remediation"]["counts"]["expected_slots"],
        )
        self.assertEqual(
            "PASSED", result["forward_after_remediation"]["status"])
        self.assertEqual("PASSED", result["overall_status"])

    def test_registered_push_latency_uses_record_barrier_to_sent_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            status_dir = Path(temp)
            cycles = ["2026-08-20T18:00", "2026-08-20T18:15"]
            for cycle, finished in zip(
                cycles,
                ("2026-08-20 18:10:00", "2026-08-20 18:25:00"),
            ):
                (status_dir / f"live-{cycle.replace(':', '-')}.json").write_text(
                    json.dumps({
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
                            "finished_at": finished,
                        },
                    }),
                    encoding="utf-8",
                )
            result = audit_push_completeness._push_delivery_latency(
                expected_cycles=cycles,
                delivered_at={
                    cycles[0]: datetime(
                        2026, 8, 20, 18, 10, 30, tzinfo=self.CST),
                    cycles[1]: datetime(
                        2026, 8, 20, 18, 25, 31, tzinfo=self.CST),
                },
                stage_status_dir=status_dir,
                as_of=datetime(2026, 8, 20, 19, 0, tzinfo=self.CST),
            )
        self.assertEqual(30, result["registration"]["target_seconds"])
        self.assertEqual(2, result["counts"]["measured_deliveries"])
        self.assertEqual(1, result["counts"]["timely_deliveries"])
        self.assertEqual(0.5, result["rates"]["timely_delivery_rate"])
        self.assertEqual(31, result["latency_seconds"]["p95"])
        self.assertEqual(
            {"delivery_latency_above_target": 1},
            result["failure_reason_counts"],
        )
        self.assertEqual("PENDING_FORWARD_EVIDENCE", result["status"])
        projection = result["recovery_projection"]
        self.assertTrue(projection["diagnostic_only"])
        self.assertEqual(
            94,
            projection["additional_all_timely_cycles_to_minimum_slots"],
        )
        self.assertEqual(
            18,
            projection["minimum_additional_all_timely_cycles_to_target"],
        )
        self.assertEqual(
            20,
            projection["earliest_total_slots_at_target_if_no_more_failures"],
        )

    def test_rotated_stage_status_uses_two_matching_pipeline_attestations(self):
        cycle = "2026-08-20T18:00"
        finished = "2026-08-20 18:10:00"

        def attestation():
            return {
                "ok": True,
                "required": True,
                "mode": "business_terminal",
                "live_stage_terminal": {
                    "status": "succeeded",
                    "returncode": 0,
                    "finished_at": "2026-08-20 18:09:58",
                    "profile_lease_released": True,
                    "same_cycle_active_lease": False,
                    "report_reconcile_barrier": {
                        "required": True,
                        "profile": "live",
                        "cycle_id": cycle,
                        "status": "ok",
                        "rc": 0,
                        "blocking": False,
                        "contract_valid": True,
                        "report_safe": True,
                        "finished_at": finished,
                    },
                },
            }

        with tempfile.TemporaryDirectory() as temp:
            result = audit_push_completeness._push_delivery_latency(
                expected_cycles=[cycle],
                delivered_at={
                    cycle: datetime(
                        2026, 8, 20, 18, 10, 20, tzinfo=self.CST),
                },
                stage_status_dir=Path(temp),
                as_of=datetime(2026, 8, 20, 19, 0, tzinfo=self.CST),
                pipeline_attempts={cycle: [{
                    "cycle": cycle,
                    "ok": True,
                    "send_status": "sent",
                    "steps": {
                        "business_attestation_pre_archive": attestation(),
                        "business_attestation_pre_send": attestation(),
                    },
                }]},
            )

        self.assertEqual(1, result["counts"]["measured_deliveries"])
        self.assertEqual(1, result["counts"]["timely_deliveries"])
        self.assertEqual(
            {"pipeline_attestation_fallback": 1},
            result["anchor_source_counts"],
        )
        self.assertEqual({}, result["failure_reason_counts"])

    def test_failed_live_with_clean_reconcile_is_not_timely_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            status_dir = Path(temp)
            cycle = "2026-08-20T18:00"
            (status_dir / f"live-{cycle.replace(':', '-')}.json").write_text(
                json.dumps({
                    "cycle_id": cycle,
                    "status": "failed",
                    "returncode": 1,
                    "report_reconcile_barrier": {
                        "required": True,
                        "profile": "live",
                        "cycle_id": cycle,
                        "status": "ok",
                        "rc": 0,
                        "contract_valid": True,
                        "report_safe": True,
                        "finished_at": "2026-08-20 18:10:00",
                    },
                }),
                encoding="utf-8",
            )
            result = audit_push_completeness._push_delivery_latency(
                expected_cycles=[cycle],
                delivered_at={
                    cycle: datetime(
                        2026, 8, 20, 18, 10, 20, tzinfo=self.CST),
                },
                stage_status_dir=status_dir,
                as_of=datetime(2026, 8, 20, 19, 0, tzinfo=self.CST),
            )

        self.assertEqual(0, result["counts"]["measured_deliveries"])
        self.assertEqual(0, result["counts"]["timely_deliveries"])
        self.assertEqual(1, result["counts"]["failures"])
        self.assertEqual(
            {"live_stage_not_succeeded": 1},
            result["failure_reason_counts"],
        )
        self.assertEqual(
            "live_stage_not_succeeded", result["failure_rows"][0]["reason"])

    def test_push_latency_recovery_projection_keeps_failed_slots(self):
        projection = audit_push_completeness._pass_rate_recovery_projection(
            passes=57,
            planned=65,
            target_rate=0.95,
            minimum_slots=96,
        )

        self.assertEqual(
            31,
            projection["additional_all_timely_cycles_to_minimum_slots"],
        )
        self.assertEqual({
            "expected_slots": 96,
            "timely_deliveries": 88,
            "timely_delivery_rate": 0.916667,
            "target_reachable": False,
        }, projection["best_case_at_minimum_or_current_denominator"])
        self.assertEqual(
            95,
            projection["minimum_additional_all_timely_cycles_to_target"],
        )
        self.assertEqual(
            160,
            projection["earliest_total_slots_at_target_if_no_more_failures"],
        )
        self.assertEqual(
            152,
            projection[
                "earliest_timely_deliveries_at_target_if_no_more_failures"],
        )

    def test_push_completeness_recovery_projection_uses_exact_fraction(self):
        projection = (
            audit_push_completeness
            ._delivered_complete_recovery_projection(
                passes=1426,
                planned=1506,
                target_rate=0.95,
                minimum_slots=96,
            )
        )

        self.assertEqual(
            94,
            projection[
                "minimum_additional_all_success_cycles_to_target"],
        )
        self.assertEqual(
            1600,
            projection["earliest_total_slots_at_target_if_no_more_failures"],
        )
        self.assertEqual(
            1520,
            projection[
                "earliest_delivered_complete_at_target_if_no_more_failures"],
        )

    def test_pending_registered_latency_prevents_overall_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = self._fixture(Path(temp))
            passed = {
                "rates": {}, "counts": {}, "statuses": {}, "daily": [],
                "failure_rows": [], "status": "PASSED",
            }
            pending_latency = {
                "status": "PENDING_FORWARD_EVIDENCE",
                "registration": {}, "window": {}, "counts": {},
                "rates": {}, "latency_seconds": {},
                "failure_reason_counts": {}, "failure_rows": [],
            }
            with mock.patch.object(
                    audit_push_completeness, "_summarize_cycles",
                    return_value=passed), mock.patch.object(
                    audit_push_completeness, "_push_delivery_latency",
                    return_value=pending_latency):
                result = audit_push_completeness.audit_push_completeness(
                    start=date(2026, 8, 20),
                    end=date(2026, 8, 20),
                    as_of=datetime(2026, 8, 21, 0, 0, tzinfo=self.CST),
                    archive_validator=lambda text: {"ok": True},
                    **fixture,
                )
        self.assertEqual("PASSED", result["status"])
        self.assertEqual(
            "PENDING_FORWARD_EVIDENCE",
            result["delivery_latency"]["status"],
        )
        self.assertEqual(
            "PENDING_FORWARD_EVIDENCE", result["overall_status"])

    def test_reversed_window_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "end date"):
            audit_push_completeness.audit_push_completeness(
                start=date(2026, 8, 2),
                end=date(2026, 8, 1),
                pipeline_log=Path("pipeline.jsonl"),
                event_log=Path("events.jsonl"),
                dedupe_db=Path("dedupe.db"),
                reports_dir=Path("reports"),
            )


if __name__ == "__main__":
    unittest.main()
