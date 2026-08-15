import copy
import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from refresh_goal_acceptance_report import (  # noqa: E402
    main as refresh_main,
    refresh_asset_class_coverage,
    refresh_multitimeframe_coverage,
    refresh_news_source_health,
    refresh_positioning_coverage,
    refresh_report_completeness,
    refresh_runtime_evidence,
)


def coverage_artifact() -> dict:
    return {
        "surface": "report",
        "manifest": {
            "title": ". 四项目标实施与前向验收（old）",
            "generatedAt": "old",
            "sources": [{
                "id": "coverage_evidence",
                "query": {"tables_used": ["baseline.json"]},
            }],
            "charts": [{"id": "coverage_chart", "subtitle": "baseline"}],
            "tables": [],
            "blocks": [
                {"id": "title", "body": "# old"},
                {"id": "data_section", "body": "## old\n\nold"},
                {"id": "coverage_block", "type": "chart"},
            ],
        },
        "snapshot": {"datasets": {
            "headline": [{}],
            "coverage": [
                {
                    "data_family": timeframe,
                    "valid_symbols": 1,
                    "universe": 100,
                    "coverage_rate": 0.01,
                    "target_rate": 0.99,
                    "status": "未达标",
                }
                for timeframe in ("15m", "1H", "4H")
            ],
            "gates": [{"goal": "关键数据完善率", "current": "old"}],
            "fast_source_health": [{
                "usable_rate": 0.95,
                "forward_usable_rate": 1.0,
                "forward_expected_slots": 4,
                "forward_minimum_slots": 96,
            }],
        }},
    }


def report_artifact() -> dict:
    return {
        "surface": "report",
        "manifest": {
            "title": ". 四项目标实施与前向验收（old）",
            "generatedAt": "2026-08-12T00:00:00Z",
            "sources": [{"id": "report_quality", "query": {}}],
            "cards": [{"id": "push_card", "metrics": []}],
            "blocks": [
                {"id": "title", "body": "# old"},
                {"id": "executive_summary", "body": "old"},
                {"id": "reports_section", "body": "old"},
                {"id": "gates_section", "body": "old"},
            ],
        },
        "snapshot": {
            "generatedAt": "2026-08-12T00:00:00Z",
            "datasets": {
                "headline": [{}],
                "report_quality": [
                    {"artifact_family": "Push 结构校验"},
                    {"artifact_family": "Push 投递确认"},
                    {"artifact_family": "日报历史校验"},
                    {"artifact_family": "最新周报校验"},
                ],
                "gates": [{"goal": "报告与推送完整度"}],
            },
        },
    }


def daily_report_audit() -> dict:
    return {
        "evaluated_at_cst": "2026-08-12 17:00:00",
        "expected": 1,
        "valid": 1,
        "invalid": 0,
        "completeness_rate": 1.0,
        "target_rate": 0.99,
        "status": "PASSED",
        "window": {
            "start_date": "2026-08-12",
            "end_date": "2026-08-12",
        },
        "auto_send": False,
        "database_write": False,
        "production_order_authorized": False,
    }


def push_report_audit(*, complete: int = 96) -> dict:
    expected = 96
    rate = complete / expected
    status = "PASSED" if rate >= 0.99 else "NOT_MET"
    payload = {
        "artifact_type": "push_report_and_delivery_completeness_audit",
        "evaluated_at_cst": "2026-08-12 17:01:00",
        "mode": "read_only_business_data",
        "as_of_cst": "2026-08-12T17:01:00+08:00",
        "forward_start_cst": "2026-08-12T16:00:00+08:00",
        "slot_finality_grace_minutes": 45,
        "window": {
            "start_date": "2026-08-11",
            "end_date": "2026-08-11",
            "completed_calendar_days": True,
            "days": 1,
            "schedule_minutes": 15,
            "expected_slots": expected,
        },
        "target_rate": 0.99,
        "counts": {
            "expected_slots": expected,
            "pipeline_present": complete,
            "missing_pipeline_slots": expected - complete,
            "pipeline_attempts": complete,
            "duplicate_pipeline_attempts": 0,
            "archive_attempts_checked": complete,
            "report_complete": complete,
            "report_incomplete": expected - complete,
            "delivery_confirmed": complete,
            "delivery_unconfirmed": expected - complete,
            "delivered_report_complete": complete,
            "delivered_report_incomplete": expected - complete,
            "failure_slots": expected - complete,
        },
        "rates": {
            "pipeline_presence_rate": rate,
            "report_completeness_rate": rate,
            "delivery_confirmation_rate": rate,
            "delivered_report_completeness_rate": rate,
        },
        "statuses": {
            "report_completeness_status": status,
            "delivered_report_completeness_status": status,
            "overall_status": status,
        },
        "status": status,
        "daily": [{
            "date": "2026-08-11",
            "expected_slots": expected,
            "pipeline_present": complete,
            "missing_pipeline_slots": expected - complete,
            "report_complete": complete,
            "delivery_confirmed": complete,
            "delivered_report_complete": complete,
            "report_completeness_rate": rate,
            "delivered_report_completeness_rate": rate,
        }],
        "failure_rows": [
            {
                "cycle": f"2026-08-11T{slot // 4:02d}:{(slot % 4) * 15:02d}",
                "pipeline_attempts": 0,
                "report_complete": False,
                "delivery_confirmed": False,
                "delivered_report_complete": False,
                "reasons": ["missing_pipeline_slot"],
                "attempt_failures": [],
            }
            for slot in range(complete, expected)
        ],
        "safety": {
            "auto_resend": False,
            "historical_backfill": False,
            "production_database_writes": 0,
            "production_report_mutation": False,
            "production_threshold_change_allowed": False,
            "production_order_authorized": False,
            "orders_placed": 0,
        },
    }
    forward_complete = 2
    payload["forward_after_remediation"] = {
        "start_cst": "2026-08-12T16:00:00+08:00",
        "end_exclusive_cst": "2026-08-12T16:30:00+08:00",
        "target_rate": 0.99,
        "minimum_slots": 96,
        "counts": {
            "expected_slots": 2,
            "pipeline_present": forward_complete,
            "missing_pipeline_slots": 0,
            "pipeline_attempts": forward_complete,
            "duplicate_pipeline_attempts": 0,
            "archive_attempts_checked": forward_complete,
            "report_complete": forward_complete,
            "report_incomplete": 0,
            "delivery_confirmed": forward_complete,
            "delivery_unconfirmed": 0,
            "delivered_report_complete": forward_complete,
            "delivered_report_incomplete": 0,
            "failure_slots": 0,
        },
        "rates": {
            "pipeline_presence_rate": 1.0,
            "report_completeness_rate": 1.0,
            "delivery_confirmation_rate": 1.0,
            "delivered_report_completeness_rate": 1.0,
        },
        "statuses": {
            "report_completeness_status": "INSUFFICIENT_EVIDENCE",
            "delivered_report_completeness_status": "INSUFFICIENT_EVIDENCE",
            "overall_status": "INSUFFICIENT_EVIDENCE",
        },
        "status": "INSUFFICIENT_EVIDENCE",
        "daily": [{
            "date": "2026-08-12",
            "expected_slots": 2,
            "pipeline_present": 2,
            "missing_pipeline_slots": 0,
            "report_complete": 2,
            "delivery_confirmed": 2,
            "delivered_report_complete": 2,
            "report_completeness_rate": 1.0,
            "delivered_report_completeness_rate": 1.0,
        }],
        "failure_rows": [],
    }
    payload["overall_status"] = "PENDING_FORWARD_EVIDENCE"
    return payload


def positioning_audit() -> dict:
    def window(*, schedule: int, minimum: int) -> dict:
        slot = {
            "cycle_id": "2026-08-13T03:00",
            "official_instrument_snapshot": {
                "status": "PASSED",
                "metadata_coverage_rate": 1.0,
            },
            "expected_symbols": 3,
            "valid_symbols": 3,
            "coverage_rate": 1.0,
            "batch_reasons": [],
            "duplicate_symbols": [],
            "extra_symbols": [],
            "invalid_row_count": 0,
            "status": "PASSED",
        }
        return {
            "start_cst": "2026-08-13T03:00:00+08:00",
            "schedule_minutes": schedule,
            "minimum_slots": minimum,
            "target_rate": 0.99,
            "expected_slots": 1,
            "passed_slots": 1,
            "expected_symbol_rows": 3,
            "valid_symbol_rows": 3,
            "slot_pass_rate": 1.0,
            "symbol_coverage_rate": 1.0,
            "official_snapshot_slot_rate": 1.0,
            "requirements": {
                "minimum_slots_met": False,
                "slot_pass_rate_at_least_target": True,
                "symbol_coverage_rate_at_least_target": True,
                "official_snapshot_slot_rate_at_least_target": True,
            },
            "status": "INSUFFICIENT_EVIDENCE",
            "slots": [slot],
        }

    return {
        "artifact_type": "positioning_coverage_audit",
        "source": "okx_rest_contract_long_short_ratio",
        "generated_at_utc": "2026-08-12T19:06:00Z",
        "latest_batch_collected_ts": "2026-08-12T19:01:16Z",
        "minimum_rate": 0.99,
        "coverage_rate": 1.0,
        "valid_symbols": 3,
        "universe_symbols": 3,
        "missing_symbols": [],
        "extra_symbols": [],
        "duplicate_symbols": [],
        "invalid_rows": [],
        "status": "PASSED",
        "mode": "read_only",
        "production_database_writes": 0,
        "production_threshold_change_allowed": False,
        "orders_placed": 0,
        "storage_contract": {
            "expected_primary_key": [
                "cycle_id", "symbol", "timeframe", "source"],
            "actual_primary_key": [
                "cycle_id", "symbol", "timeframe", "source"],
            "cross_cycle_upstream_ts_reuse_supported": True,
            "status": "PASSED",
        },
        "forward_after_remediation": window(schedule=60, minimum=24),
        "decision_availability_forward": window(
            schedule=15, minimum=96),
        "overall_status": "PENDING_FORWARD_EVIDENCE",
    }


class GoalAcceptanceRefreshSafetyTests(unittest.TestCase):
    def test_positioning_latest_batch_never_substitutes_for_forward_windows(self):
        natural = positioning_audit()
        isolated = copy.deepcopy(natural)
        result = refresh_positioning_coverage(
            coverage_artifact(),
            natural,
            isolated,
            natural_relative_path="positioning.json",
            isolated_relative_path="positioning-isolated.json",
        )
        coverage = next(
            row for row in result["snapshot"]["datasets"]["coverage"]
            if row["data_family"] == "official_positioning_1H"
        )
        headline = result["snapshot"]["datasets"]["headline"][0]
        self.assertEqual("达标", coverage["status"])
        self.assertEqual("未达标", coverage["acceptance_status"])
        self.assertEqual(
            "PENDING_FORWARD_EVIDENCE", coverage["overall_status"])
        self.assertEqual(1, headline["positioning_hourly_forward_expected_slots"])
        self.assertEqual(
            1, headline["positioning_availability_forward_expected_slots"])
        data_block = next(
            block for block in result["manifest"]["blocks"]
            if block.get("id") == "data_section"
        )
        self.assertIn("1/24槽", data_block["body"])

        tampered = copy.deepcopy(natural)
        tampered["overall_status"] = "PASSED"
        with self.assertRaisesRegex(ValueError, "overall status"):
            refresh_positioning_coverage(
                coverage_artifact(), tampered, isolated,
                natural_relative_path="positioning.json",
                isolated_relative_path="positioning-isolated.json",
            )
        tampered = copy.deepcopy(natural)
        tampered["storage_contract"]["actual_primary_key"][0] = "ts"
        with self.assertRaisesRegex(ValueError, "storage contract"):
            refresh_positioning_coverage(
                coverage_artifact(), tampered, isolated,
                natural_relative_path="positioning.json",
                isolated_relative_path="positioning-isolated.json",
            )
        tampered = copy.deepcopy(natural)
        tampered["forward_after_remediation"]["passed_slots"] = 0
        with self.assertRaisesRegex(ValueError, "aggregate counts"):
            refresh_positioning_coverage(
                coverage_artifact(), tampered, isolated,
                natural_relative_path="positioning.json",
                isolated_relative_path="positioning-isolated.json",
            )

    def test_cli_dry_run_never_calls_artifact_writer(self):
        artifact = report_artifact()
        audit = daily_report_audit()
        push_audit = push_report_audit()
        with self.subTest("dry-run"):
            with (
                mock.patch("pathlib.Path.read_text") as read_text,
                mock.patch(
                    "refresh_goal_acceptance_report._atomic_write_json"
                ) as writer,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                read_text.side_effect = [
                    json.dumps(artifact, ensure_ascii=False),
                    json.dumps(audit, ensure_ascii=False),
                    json.dumps(push_audit, ensure_ascii=False),
                ]
                status = refresh_main([
                    "--artifact", "artifact.json",
                    "--audit", "audit.json",
                    "--audit-relative-path", "audit.json",
                    "--push-audit", "push.json",
                    "--push-audit-relative-path", "push.json",
                    "--dry-run",
                ])
            self.assertEqual(0, status)
            writer.assert_not_called()
            summary = json.loads(stdout.getvalue())
            self.assertFalse(summary["artifact_written"])
            self.assertEqual(1.0, summary["push_report_rate"])
            self.assertEqual(
                "PENDING_FORWARD_EVIDENCE", summary["push_overall_status"])
            self.assertIsNone(summary["contract_statistics_overall_status"])

    def test_report_refresh_keeps_push_below_99_as_not_met_and_rejects_tampering(self):
        daily = daily_report_audit()
        push = push_report_audit(complete=90)
        result = refresh_report_completeness(
            report_artifact(),
            daily,
            push,
            audit_relative_path="daily.json",
            push_audit_relative_path="push.json",
        )
        headline = result["snapshot"]["datasets"]["headline"][0]
        gate = result["snapshot"]["datasets"]["gates"][0]
        rows = {
            row["artifact_family"]: row
            for row in result["snapshot"]["datasets"]["report_quality"]
        }
        self.assertEqual("未达标", gate["status"])
        self.assertFalse(headline["report_and_push_gate_passed"])
        self.assertEqual(90, rows["Push 报告完整性"]["numerator"])
        self.assertEqual(96, rows["Push 报告完整性"]["denominator"])
        self.assertEqual("达标", rows["日报历史校验"]["status"])
        reports = next(
            block for block in result["manifest"]["blocks"]
            if block.get("id") == "reports_section"
        )["body"]
        self.assertIn("93.750%", reports)
        self.assertIn("不历史补推", reports)

        tampered_rate = copy.deepcopy(push)
        tampered_rate["rates"]["report_completeness_rate"] = 1.0
        with self.assertRaisesRegex(ValueError, "rate disagrees"):
            refresh_report_completeness(
                report_artifact(), daily, tampered_rate,
                audit_relative_path="daily.json",
                push_audit_relative_path="push.json",
            )

        tampered_safety = copy.deepcopy(push)
        tampered_safety["safety"]["auto_resend"] = True
        with self.assertRaisesRegex(ValueError, "unsafe field"):
            refresh_report_completeness(
                report_artifact(), daily, tampered_safety,
                audit_relative_path="daily.json",
                push_audit_relative_path="push.json",
            )

    def test_news_refresh_recomputes_publishers_and_rejects_rate_tampering(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "title": ". 四项目标实施与前向验收（old）",
                "sources": [],
                "tables": [],
                "blocks": [{"id": "data_section", "body": "old"}],
            },
            "snapshot": {"datasets": {
                "headline": [{}],
                "gates": [{"goal": "关键数据完善率", "current": "old"}],
            }},
        }

        def source_row(source: str, role: str) -> dict:
            return {
                "source": source,
                "role": role,
                "endpoint": "https://publisher.example/feed",
                "schedule_minutes": 15,
                "start_cst": "2026-08-12T16:15:00+08:00",
                "end_exclusive_cst": "2026-08-12T17:30:00+08:00",
                "expected_slots": 5,
                "observed_rows": 5,
                "missing_slots": 0,
                "complete_slots": 5,
                "degraded_or_failed_slots": 0,
                "strict_complete_rate": 1.0,
                "available_rate": 1.0,
                "target_rate": 0.99,
                "minimum_slots": 96,
                "status": "INSUFFICIENT_EVIDENCE",
                "raw_status_counts": {"ok": 5},
                "exception_count": 0,
            }

        rows = [
            source_row("okx_news", "official_required"),
            source_row("rss_en", "required"),
            *[
                source_row(f"rss:{publisher}", "required_subsource")
                for publisher in (
                    "bitcoinist", "coindesk", "cointelegraph", "cryptoslate",
                    "decrypt", "theblock",
                )
            ],
            source_row("panews", "optional"),
        ]
        audit = {
            "artifact_type": "scheduled_news_source_health_audit",
            "generated_at_cst": "2026-08-12T17:24:51+08:00",
            "as_of_cst": "2026-08-12T17:24:50+08:00",
            "forward_start_cst": "2026-08-12T16:15:00+08:00",
            "target_rate": 0.99,
            "minimum_window_hours": 24,
            "forward_after_remediation": {
                "critical_status": "INSUFFICIENT_EVIDENCE",
                "all_sources_status": "INSUFFICIENT_EVIDENCE",
                "sources": rows,
            },
            "overall_status": "PENDING_FORWARD_EVIDENCE",
            "production_mutation": False,
            "collector_retry_triggered": False,
            "stage_dispatch_triggered": False,
            "orders_placed": 0,
            "production_execution_authorized": False,
        }
        result = refresh_news_source_health(
            artifact, audit,
            audit_relative_path="news-source-health-audit.json",
        )
        section = next(
            block for block in result["manifest"]["blocks"]
            if block.get("id") == "news_source_section"
        )["body"]
        self.assertIn("PANews官方RSS主路径", section)
        self.assertIn("5/5=100.000%", section)
        self.assertEqual(
            "2026-08-12T16:15:00+08:00",
            result["snapshot"]["datasets"]["headline"][0][
                "news_forward_start_cst"
            ],
        )

        tampered = copy.deepcopy(audit)
        tampered["forward_after_remediation"]["sources"][2][
            "complete_slots"
        ] = 4
        with self.assertRaisesRegex(ValueError, "evidence disagrees"):
            refresh_news_source_health(
                copy.deepcopy(artifact), tampered,
                audit_relative_path="tampered.json",
            )

    def test_asset_class_refresh_recomputes_counts_and_rejects_tampering(self):
        audit = {
            "artifact_type": "official_asset_class_coverage_audit",
            "generated_at_cst": "2026-08-12T16:51:32+08:00",
            "mode": "read_only",
            "official_universe_symbols": 100,
            "local_rows": 100,
            "row_coverage_rate": 1.0,
            "official_compatible_symbols": 100,
            "official_compatibility_rate": 1.0,
            "missing_symbols": [],
            "mismatches": [],
            "unsupported_official_symbols": [],
            "minimum_rate": 0.99,
            "checks": {
                "row_coverage_at_least_target": True,
                "official_compatibility_at_least_target": True,
                "no_unsupported_official_categories": True,
            },
            "status": "PASSED",
            "production_database_writes": 0,
            "collector_triggered": False,
            "orders_placed": 0,
        }
        result = refresh_asset_class_coverage(
            coverage_artifact(), audit,
            audit_relative_path="asset-class-coverage-audit.json",
        )
        rows = result["snapshot"]["datasets"]["asset_class_coverage"]
        self.assertEqual([1.0, 1.0], [row["coverage_rate"] for row in rows])
        self.assertIn(
            "OKX官方instCategory兼容率100/100=100.000%",
            result["manifest"]["blocks"][1]["body"],
        )

        tampered = copy.deepcopy(audit)
        tampered["mismatches"] = [{"symbol": "BAD-USDT-SWAP"}]
        with self.assertRaisesRegex(ValueError, "mismatches disagree"):
            refresh_asset_class_coverage(
                coverage_artifact(), tampered,
                audit_relative_path="tampered.json",
            )

    def test_multitimeframe_refresh_reports_non_100_rates_and_gap_classes(self):
        def row(
            timeframe: str,
            raw_valid: int,
            ready: int,
            gaps: list[dict],
        ) -> dict:
            counts = {
                classification: sum(
                    gap["classification"] == classification for gap in gaps
                )
                for classification in (
                    "source_data_invalid",
                    "insufficient_history",
                    "indicator_invalid",
                )
            }
            return {
                "timeframe": timeframe,
                "expected_closed_bar_ts": "2026-08-12T08:00:00Z",
                "universe_symbols": 100,
                "observed_exact_bar_rows": raw_valid,
                "raw_ohlcv_valid_symbols": raw_valid,
                "raw_ohlcv_coverage_rate": raw_valid / 100,
                "raw_ohlcv_status": "PASSED" if raw_valid >= 99 else "NOT_MET",
                "analysis_ready_symbols": ready,
                "analysis_ready_rate": ready / 100,
                "analysis_ready_status": "PASSED" if ready >= 99 else "NOT_MET",
                "gap_counts": counts,
                "gaps": gaps,
            }

        audit = {
            "artifact_type": "multitimeframe_closed_bar_coverage_audit",
            "generated_at_cst": "2026-08-12T16:52:13+08:00",
            "evaluation_at_utc": "2026-08-12T08:52:12+00:00",
            "mode": "read_only",
            "universe_symbols": 100,
            "minimum_rate": 0.99,
            "timeframes": [
                row("15m", 100, 99, [{
                    "symbol": "NEW15-USDT-SWAP",
                    "classification": "insufficient_history",
                }]),
                row("1H", 99, 99, [{
                    "symbol": "NO1H-USDT-SWAP",
                    "classification": "source_data_invalid",
                }]),
                row("4H", 99, 98, [
                    {
                        "symbol": "NO4H-USDT-SWAP",
                        "classification": "source_data_invalid",
                    },
                    {
                        "symbol": "NEW4H-USDT-SWAP",
                        "classification": "insufficient_history",
                    },
                ]),
            ],
            "data_completeness_status": "PASSED",
            "analysis_readiness_status": "NOT_MET",
            "status": "NOT_MET",
            "production_database_writes": 0,
            "production_threshold_change_allowed": False,
            "orders_placed": 0,
        }
        result = refresh_multitimeframe_coverage(
            coverage_artifact(), audit,
            audit_relative_path="multitimeframe-coverage-audit.json",
        )
        subtitle = result["manifest"]["charts"][0]["subtitle"]
        body = result["manifest"]["blocks"][1]["body"]
        self.assertIn("15m 100.000%", subtitle)
        self.assertIn("1H 99.000%", subtitle)
        self.assertIn("4H 99.000%", subtitle)
        self.assertIn("NO4H-USDT-SWAP", body)
        self.assertIn("NEW4H-USDT-SWAP", body)
        self.assertNotIn("均为100%", body)

        tampered = copy.deepcopy(audit)
        tampered["timeframes"][2]["raw_ohlcv_coverage_rate"] = 1.0
        with self.assertRaisesRegex(ValueError, "counts or rates disagree"):
            refresh_multitimeframe_coverage(
                coverage_artifact(), tampered,
                audit_relative_path="tampered.json",
            )

    def test_runtime_refresh_recomputes_daily_gate_and_uses_dynamic_universe(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "title": ". 四项目标实施与前向验收（2026-08-12 17:30）",
                "generatedAt": "2026-08-12T09:30:00Z",
                "sources": [],
                "charts": [{"id": "throughput_chart"}],
                "cards": [{"id": "throughput_card"}],
                "tables": [],
                "blocks": [
                    {"id": "title", "body": "# old"},
                    {"id": "throughput_section", "body": "old"},
                    {"id": "throughput_block", "type": "chart"},
                ],
            },
            "snapshot": {"generatedAt": "2026-08-12T09:30:00Z", "datasets": {
                "headline": [{"shadow_capacity_per_day": 1}],
                "throughput": [{"target_or_capacity": "observed"}],
                "gates": [{"goal": "300+币与判断量+50%"}],
            }},
        }
        evaluation = {
            "artifact_type": "full_universe_shadow_judgment_evaluation",
            "generated_at_utc": "2026-08-12T08:11:24Z",
            "snapshots_loaded": 3,
            "daily_throughput": {"latest_day": {
                "date": "2026-08-12",
                "snapshots": 3,
                "judgment_records": 1283,
                "minimum_records_target": 993,
                "minimum_snapshots_target": 3,
                "minimum_unique_symbols_target": 300,
                "unique_symbols": 429,
                "minimum_records_met": True,
                "minimum_snapshots_met": True,
                "minimum_unique_symbols_met": True,
                "daily_target_met": True,
            }},
            "horizons": [],
            "production_mutation": False,
            "orders_placed": 0,
        }
        model = {
            "artifact_type": "frozen_multitimeframe_model_shadow",
            "cycle_id": "2026-08-12T08:00",
            "generated_at_utc": "2026-08-12T00:01:58Z",
            "status": "ready_for_forward_shadow",
            "forward_evidence_eligible": True,
            "metrics": {
                "scored_symbols": 61,
                "selected_signals": 54,
                "side_counts": {"long": 48, "short": 6},
            },
            "data_audit": {
                "scoring_ready_rows": 61,
                "enrichment": {"contract_statistics_available_rows": 61},
                "frozen_feature_clock": {},
            },
            "confidence_claim_allowed": False,
            "production_execution_authorized": False,
            "production_threshold_change_allowed": False,
            "production_mutation": False,
            "orders_placed": 0,
        }
        result = refresh_runtime_evidence(
            artifact, evaluation, model,
            evaluation_relative_path="universe-shadow-evaluation.json",
            model_relative_path="model-shadow.json",
        )
        section = next(
            block for block in result["manifest"]["blocks"]
            if block.get("id") == "throughput_section"
        )["body"]
        self.assertIn("429个交易对", section)
        self.assertIn("已同时达到", section)
        self.assertIn("1,287条/日", section)
        self.assertNotIn("尚未同时达到", section)
        self.assertEqual(
            1287,
            result["snapshot"]["datasets"]["headline"][0][
                "shadow_capacity_per_day"
            ],
        )
        self.assertEqual(
            "达标", result["snapshot"]["datasets"]["gates"][0]["status"])
        self.assertEqual(
            "2026-08-12T09:30:00Z", result["manifest"]["generatedAt"])
        self.assertEqual(
            "2026-08-12T09:30:00Z", result["snapshot"]["generatedAt"])
        self.assertTrue(
            result["manifest"]["title"].endswith("（2026-08-12 17:30）")
        )

        tampered = copy.deepcopy(evaluation)
        tampered["daily_throughput"]["latest_day"]["daily_target_met"] = False
        with self.assertRaisesRegex(ValueError, "status disagrees"):
            refresh_runtime_evidence(
                copy.deepcopy(artifact), tampered, model,
                evaluation_relative_path="tampered.json",
                model_relative_path="model-shadow.json",
            )


if __name__ == "__main__":
    unittest.main()
