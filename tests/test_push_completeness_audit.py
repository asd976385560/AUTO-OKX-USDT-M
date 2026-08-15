import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
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
