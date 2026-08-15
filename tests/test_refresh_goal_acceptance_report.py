import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from refresh_goal_acceptance_report import (  # noqa: E402
    refresh_contract_statistics_coverage,
    refresh_credibility_evidence,
    refresh_directional_separability,
    refresh_news_source_health,
    refresh_multitimeframe_coverage,
    refresh_asset_class_coverage,
    refresh_positioning_coverage,
    refresh_ranking_credibility,
    refresh_report_completeness,
    refresh_runtime_evidence,
    refresh_selective_credibility,
    refresh_source_health,
)


CST = timezone(timedelta(hours=8))
# 与 refresh_goal_acceptance_report 中的预注册常量同源，改一处即两处同改。
PUSH_FORWARD_START_CST = "2026-08-12T16:00:00+08:00"
PUSH_FINALITY_GRACE_MINUTES = 45


def push_completeness_audit(
    *,
    days: int = 14,
    target_rate: float = 0.99,
    evaluated_at_cst: str = "2026-08-13 17:00:00",
    as_of_cst: str = "2026-08-13T16:45:00+08:00",
) -> dict:
    """构造一份自洽的 Push 完整度审计工件（全达标口径）。

    `refresh_report_completeness` 自 2026-08-12 起把 Push 审计作为**必填**第三位
    参数，并对窗口/计数/速率/状态/安全标志/逐日行/失败行/前向证据交叉复算。这里
    按同一套恒等式生成，只留 days / target_rate / as_of 少量旋钮——避免测试里手抄
    几十个互相牵制的常量（手抄必然随生产口径演进而腐坏，本文件此前正是如此）。
    """
    expected = days * 96
    start_date = datetime(2026, 7, 31, tzinfo=CST)
    daily_rows = [
        {
            "date": (start_date + timedelta(days=index)).strftime("%Y-%m-%d"),
            "expected_slots": 96,
            "pipeline_present": 96,
            "missing_pipeline_slots": 0,
            "report_complete": 96,
            "delivered_report_complete": 96,
            "report_completeness_rate": 1.0,
            "delivered_report_completeness_rate": 1.0,
        }
        for index in range(days)
    ]
    end_date = (start_date + timedelta(days=days - 1)).strftime("%Y-%m-%d")

    # 前向窗：起点是预注册常量；终点 = as_of 减 45 分钟宽限后向下对齐 15 分钟再 +1 槽。
    forward_start = datetime.fromisoformat(
        PUSH_FORWARD_START_CST).astimezone(CST)
    as_of_dt = datetime.fromisoformat(as_of_cst).astimezone(CST)
    graced = as_of_dt - timedelta(minutes=PUSH_FINALITY_GRACE_MINUTES)
    forward_end = max(
        forward_start,
        graced.replace(
            minute=graced.minute // 15 * 15, second=0, microsecond=0,
        ) + timedelta(minutes=15),
    )
    forward_expected = int(
        (forward_end - forward_start).total_seconds() // (15 * 60))
    forward_status = (
        "PASSED" if forward_expected >= 96 else "INSUFFICIENT_EVIDENCE")
    overall = (
        "PASSED" if forward_status == "PASSED" else "PENDING_FORWARD_EVIDENCE")
    full_counts = {
        "expected_slots": expected,
        "pipeline_present": expected,
        "missing_pipeline_slots": 0,
        "pipeline_attempts": expected,
        "duplicate_pipeline_attempts": 0,
        "archive_attempts_checked": expected,
        "report_complete": expected,
        "report_incomplete": 0,
        "delivery_confirmed": expected,
        "delivery_unconfirmed": 0,
        "delivered_report_complete": expected,
        "delivered_report_incomplete": 0,
        "failure_slots": 0,
    }
    perfect_rates = {
        "pipeline_presence_rate": 1.0,
        "report_completeness_rate": 1.0,
        "delivery_confirmation_rate": 1.0,
        "delivered_report_completeness_rate": 1.0,
    }
    return {
        "artifact_type": "push_report_and_delivery_completeness_audit",
        "mode": "read_only_business_data",
        "evaluated_at_cst": evaluated_at_cst,
        "as_of_cst": as_of_cst,
        "target_rate": target_rate,
        "forward_start_cst": PUSH_FORWARD_START_CST,
        "slot_finality_grace_minutes": PUSH_FINALITY_GRACE_MINUTES,
        "window": {
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date,
            "end_inclusive": True,
            "completed_calendar_days": True,
            "days": days,
            "schedule_minutes": 15,
            "expected_slots": expected,
        },
        "counts": dict(full_counts),
        "rates": dict(perfect_rates),
        "statuses": {
            "report_completeness_status": "PASSED",
            "delivered_report_completeness_status": "PASSED",
            "overall_status": "PASSED",
        },
        "status": "PASSED",
        "overall_status": overall,
        "daily": daily_rows,
        "failure_rows": [],
        "safety": {
            "auto_resend": False,
            "historical_backfill": False,
            "production_database_writes": 0,
            "production_report_mutation": False,
            "production_threshold_change_allowed": False,
            "production_order_authorized": False,
            "orders_placed": 0,
        },
        "forward_after_remediation": {
            "start_cst": forward_start.isoformat(),
            "end_exclusive_cst": forward_end.isoformat(),
            "target_rate": target_rate,
            "minimum_slots": 96,
            "counts": {
                **full_counts,
                "expected_slots": forward_expected,
                "pipeline_present": forward_expected,
                "pipeline_attempts": forward_expected,
                "archive_attempts_checked": forward_expected,
                "report_complete": forward_expected,
                "delivery_confirmed": forward_expected,
                "delivered_report_complete": forward_expected,
            },
            "rates": dict(perfect_rates),
            "statuses": {
                "report_completeness_status": forward_status,
                "delivered_report_completeness_status": forward_status,
                "overall_status": forward_status,
            },
            "status": forward_status,
            "daily": [{
                "date": forward_start.strftime("%Y-%m-%d"),
                "expected_slots": forward_expected,
                "pipeline_present": forward_expected,
                "report_complete": forward_expected,
                "delivered_report_complete": forward_expected,
            }],
            "failure_rows": [],
        },
    }


class RefreshGoalAcceptanceReportTests(unittest.TestCase):
    def test_refreshes_natural_runtime_without_authorizing_production(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "title": ". 四项目标实施与前向验收（old）",
                "generatedAt": "old",
                "sources": [{"id": "runtime", "query": {}}],
                "charts": [{"id": "throughput_chart"}],
                "cards": [{"id": "throughput_card", "metrics": []}],
                "tables": [],
                "blocks": [
                    {"id": "title", "body": "# old"},
                    {"id": "throughput_section", "body": "old"},
                    {"id": "throughput_block", "type": "chart"},
                    {"id": "credibility_section", "body": "old"},
                ],
            },
            "snapshot": {"datasets": {
                "headline": [{}],
                "throughput": [{
                    "target_or_capacity": "observed",
                    "judgments_per_day": 427,
                }],
                "gates": [{"goal": "300+币与判断量+50%"}],
            }},
        }
        evaluation = {
            "artifact_type": "full_universe_shadow_judgment_evaluation",
            "generated_at_utc": "2026-08-12T00:03:28Z",
            "snapshots_loaded": 2,
            "daily_throughput": {"latest_day": {
                "date": "2026-08-12",
                "snapshots": 2,
                "judgment_records": 854,
                "minimum_records_target": 993,
                "minimum_snapshots_target": 3,
                "minimum_unique_symbols_target": 300,
                "unique_symbols": 427,
                "daily_target_met": False,
            }},
            "horizons": [{
                "horizon": "15m",
                "n_labeled": 51,
                "after_cost_precision_pct": 23.529,
            }],
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
                "enrichment": {
                    "contract_statistics_available_rows": 61,
                    "maximum_ready_decision_delay_seconds": 103,
                },
                "frozen_feature_clock": {
                    "maximum_decision_delay_seconds": 56,
                },
            },
            "confidence_claim_allowed": False,
            "production_execution_authorized": False,
            "production_threshold_change_allowed": False,
            "production_mutation": False,
            "orders_placed": 0,
        }
        result = refresh_runtime_evidence(
            artifact,
            evaluation,
            model,
            evaluation_relative_path="universe-shadow-evaluation.json",
            model_relative_path="model-shadow-08.json",
        )
        headline = result["snapshot"]["datasets"]["headline"][0]
        self.assertEqual(854, headline["shadow_records_observed_today"])
        self.assertEqual(54, headline["model_forward_selected_signals"])
        self.assertFalse(headline["model_forward_confidence_claim_allowed"])
        observed = result["snapshot"]["datasets"]["throughput"][0]
        self.assertEqual(2, observed["schedule_runs_per_day"])
        self.assertIn("854/993", result["manifest"]["charts"][0]["subtitle"])
        self.assertEqual(
            ["model_forward_section", "model_shadow_forward_block"],
            [block["id"] for block in result["manifest"]["blocks"][3:5]],
        )
        self.assertEqual(
            "待连续验收", result["snapshot"]["datasets"]["gates"][0]["status"])

        unsafe = copy.deepcopy(model)
        unsafe["production_execution_authorized"] = True
        with self.assertRaises(ValueError):
            refresh_runtime_evidence(
                copy.deepcopy(artifact),
                evaluation,
                unsafe,
                evaluation_relative_path="eval.json",
                model_relative_path="model.json",
            )

    def test_refreshes_closed_multitimeframe_coverage_without_excluding_new_listings(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "title": ". 四项目标实施与前向验收（old）",
                "generatedAt": "old",
                "sources": [{"id": "coverage_evidence", "query": {}}],
                "tables": [],
                "charts": [{"id": "coverage_chart"}],
                "blocks": [
                    {"id": "title", "body": "# old"},
                    {"id": "data_section", "body": "## old\n\nold detail"},
                    {"id": "coverage_block", "type": "chart"},
                ],
            },
            "snapshot": {
                "datasets": {
                    "headline": [{}],
                    "coverage": [
                        {"data_family": timeframe, "valid_symbols": 1,
                         "universe": 3, "coverage_rate": 1 / 3,
                         "target_rate": 0.99, "status": "未达标"}
                        for timeframe in ("15m", "1H", "4H")
                    ],
                    "gates": [{"goal": "关键数据完善率"}],
                    "fast_source_health": [{
                        "usable_rate": 0.97,
                        "forward_expected_slots": 20,
                        "forward_minimum_slots": 96,
                    }],
                }
            },
        }
        rows = []
        for timeframe, ready in (("15m", 3), ("1H", 3), ("4H", 2)):
            rows.append({
                "timeframe": timeframe,
                "expected_closed_bar_ts": "2026-08-12T00:00:00Z",
                "universe_symbols": 3,
                "observed_exact_bar_rows": 3,
                "raw_ohlcv_valid_symbols": 3,
                "raw_ohlcv_coverage_rate": 1.0,
                "raw_ohlcv_status": "PASSED",
                "analysis_ready_symbols": ready,
                "analysis_ready_rate": ready / 3,
                "analysis_ready_status": "PASSED" if ready == 3 else "NOT_MET",
                "gap_counts": {
                    "source_data_invalid": 0,
                    "insufficient_history": 3 - ready,
                    "indicator_invalid": 0,
                },
                "gaps": ([{
                    "symbol": "NEW-USDT-SWAP",
                    "classification": "insufficient_history",
                }] if ready < 3 else []),
            })
        audit = {
            "artifact_type": "multitimeframe_closed_bar_coverage_audit",
            "generated_at_cst": "2026-08-12T06:35:00+08:00",
            "evaluation_at_utc": "2026-08-11T22:35:00+00:00",
            "mode": "read_only",
            "universe_symbols": 3,
            "minimum_rate": 0.99,
            "data_completeness_status": "PASSED",
            "analysis_readiness_status": "NOT_MET",
            "status": "NOT_MET",
            "production_database_writes": 0,
            "production_threshold_change_allowed": False,
            "orders_placed": 0,
            "timeframes": rows,
        }
        result = refresh_multitimeframe_coverage(
            artifact, audit,
            audit_relative_path="multitimeframe-coverage-audit.json",
        )
        coverage_4h = next(
            row for row in result["snapshot"]["datasets"]["coverage"]
            if row["data_family"] == "4H")
        self.assertEqual(2 / 3, coverage_4h["coverage_rate"])
        self.assertEqual(1.0, coverage_4h["raw_ohlcv_coverage_rate"])
        table = next(
            table for table in result["manifest"]["tables"]
            if table["id"] == "multitimeframe_coverage_table")
        self.assertEqual("analysis_ready_rate", table["defaultSort"]["field"])
        self.assertIn("NEW-USDT-SWAP", result["manifest"]["blocks"][1]["body"])
        self.assertEqual("未达标", result["snapshot"]["datasets"]["gates"][0]["status"])

    def test_refreshes_groupwise_ranking_without_authorizing_production(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "sources": [{
                    "id": "credibility_evidence",
                    "query": {"tables_used": ["baseline.json"]},
                }],
                "cards": [{"id": "credibility_card", "metrics": []}],
                "charts": [{"id": "credibility_chart"}],
                "blocks": [{"id": "credibility_section", "body": "old"}],
            },
            "snapshot": {"datasets": {
                "headline": [{
                    "production_signal_precision": 0.4545,
                    "production_signal_n": 11,
                    "shared_confirmation_precision": 0.3538,
                    "independent_confirmation_precision": 0.4021,
                }],
                "credibility": [],
                "credibility_primary": [],
                "gates": [{"goal": "分析可信度"}],
            }},
        }
        result_template = {
            "precision": 0.3661,
            "n": 579,
            "wilson_95_low": 0.3279,
            "wilson_95_high": 0.4062,
            "ece": 0.0934,
            "distinct_days": 2,
            "distinct_cycles": 32,
            "mean_signed_return_after_cost": -0.0051,
        }
        holdout = dict(result_template)
        holdout.update({
            "precision": 0.4412,
            "n": 2092,
            "ece": 0.0168,
            "distinct_days": 7,
            "distinct_cycles": 147,
            "mean_signed_return_after_cost": -0.0021,
        })
        ranking = {
            "artifact_type": "groupwise_multitimeframe_ranking_diagnostic",
            "generated_at_utc": "2026-08-12T05:15:31+08:00",
            "input_panel": "research_panel.csv",
            "model_family": {"selected_model": "listwise_test_model"},
            "threshold_selection": {"precision": 0.4703, "n": 438},
            "evaluation": {
                "internal_confirmation": result_template,
                "historical_holdout": holdout,
            },
            "selected_subset_oracle_diagnostic": {
                "internal_confirmation": {
                    "any_candidate_success_rate": 0.9672,
                },
            },
            "label_profile": {
                "historical_holdout": {
                    "any_candidate_success_rate": 0.8956,
                },
            },
            "production_threshold_change_allowed": False,
            "acceptance": {
                "target_precision": 0.9,
                "confidence_90_status": "NOT_PROVEN",
                "production_status": "NO_CHANGE_ALLOWED",
            },
        }
        result = refresh_ranking_credibility(
            artifact,
            ranking,
            ranking_relative_path="ranking_diagnostic.json",
        )
        headline = result["snapshot"]["datasets"]["headline"][0]
        self.assertAlmostEqual(0.3661, headline["ranking_confirmation_precision"])
        self.assertEqual(579, headline["ranking_confirmation_n"])
        methods = [
            row["method"]
            for row in result["snapshot"]["datasets"]["credibility_primary"]
        ]
        self.assertEqual(
            ["组内排序 内部确认", "组内排序 历史留出"], methods)
        self.assertEqual(
            "未达标", result["snapshot"]["datasets"]["gates"][0]["status"])
        self.assertIn(
            "没有修改生产阈值",
            result["manifest"]["blocks"][0]["body"],
        )

    def test_refreshes_directional_gap_without_authorizing_production(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "sources": [{
                    "id": "credibility_evidence",
                    "query": {"tables_used": ["ranking.json"]},
                }],
                "charts": [{"id": "credibility_chart"}],
                "blocks": [{
                    "id": "credibility_section",
                    "body": "## existing\n\nexisting evidence",
                }],
            },
            "snapshot": {"datasets": {
                "headline": [{}],
                "credibility": [],
                "credibility_primary": [],
                "gates": [{
                    "goal": "分析可信度",
                    "current": "组内排序36.6%",
                }],
            }},
        }
        confirmation = {
            "precision": 0.4587,
            "n": 617,
            "wilson_95_low": 0.4197,
            "wilson_95_high": 0.4981,
            "distinct_days": 2,
            "distinct_cycles": 32,
            "mean_signed_return_after_cost": -0.0005,
            "selected_subset_any_candidate_success_rate": 0.9562,
            "capture_rate_when_any_candidate_succeeds": 0.4797,
        }
        holdout = dict(confirmation)
        holdout.update({
            "precision": 0.4066,
            "n": 2747,
            "wilson_95_low": 0.3884,
            "wilson_95_high": 0.4251,
            "distinct_days": 7,
            "distinct_cycles": 147,
            "mean_signed_return_after_cost": -0.0024,
            "selected_subset_any_candidate_success_rate": 0.9115,
            "capture_rate_when_any_candidate_succeeds": 0.4461,
        })
        diagnostic = {
            "artifact_type": "directional_separability_diagnostic",
            "generated_at_utc": "2026-08-12T07:20:00Z",
            "selected_policy": {"policy": "mean_direction_margin_all"},
            "evaluation": {
                "internal_confirmation": confirmation,
                "historical_holdout": holdout,
            },
            "root_cause": {
                "historical_holdout_oracle_any_candidate_success_rate": 0.8956,
                "historical_holdout_ranking_gap": 0.4890,
            },
            "acceptance": {
                "confidence_90_status": "NOT_PROVEN",
                "production_status": "NO_CHANGE_ALLOWED",
            },
            "production_threshold_change_allowed": False,
        }

        result = refresh_directional_separability(
            artifact,
            diagnostic,
            diagnostic_relative_path="directional_separability.json",
        )

        headline = result["snapshot"]["datasets"]["headline"][0]
        self.assertAlmostEqual(
            headline["directional_confirmation_precision"], 0.4587)
        self.assertAlmostEqual(
            headline["directional_holdout_ranking_gap"], 0.4890)
        methods = [
            row["method"]
            for row in result["snapshot"]["datasets"]["credibility_primary"]
        ]
        self.assertEqual(
            ["方向间距 内部确认", "方向间距 历史留出"], methods)
        self.assertEqual(
            result["snapshot"]["datasets"]["gates"][0]["status"],
            "未达标",
        )
        self.assertIn("波动", result["manifest"]["blocks"][0]["body"])
        self.assertTrue(any(
            source["id"] == "directional_separability"
            for source in result["manifest"]["sources"]
        ))

    def test_refreshes_news_children_with_strict_forward_denominator(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "sources": [],
                "tables": [],
                "blocks": [{"id": "data_section", "body": "old"}],
            },
            "snapshot": {"datasets": {
                "headline": [{}],
                "gates": [{"goal": "关键数据完善率", "current": "4H 98.6%"}],
            }},
        }

        # 生产端逐行复算：minimum_slots=ceil(窗口小时×60/间隔)、raw_status_counts
        # 与 observed/complete/available 互锁、exception_count=expected-complete、
        # target_rate 与 start_cst 必须与审计头一致。这里按同一套恒等式生成，
        # 只留 complete 一个旋钮，避免手抄常量再次腐坏。
        forward_start = "2026-08-12T05:30:00+08:00"
        target_rate = 0.99
        minimum_window_hours = 24

        def source_row(source, role, complete, status):
            interval = 15
            expected_slots = 1
            observed = 1
            degraded_or_failed = observed - complete
            return {
                "source": source,
                "role": role,
                "schedule_minutes": interval,
                "start_cst": forward_start,
                "target_rate": target_rate,
                "minimum_slots": minimum_window_hours * 60 // interval,
                "expected_slots": expected_slots,
                "observed_rows": observed,
                "complete_slots": complete,
                "missing_slots": expected_slots - observed,
                "degraded_or_failed_slots": degraded_or_failed,
                "raw_status_counts": {
                    "ok": complete, "degraded": degraded_or_failed,
                },
                "strict_complete_rate": complete / expected_slots,
                "available_rate": observed / expected_slots,
                "exception_count": expected_slots - complete,
                "status": status,
            }

        # 关键源集合由生产端钉死，缺一即 "critical source set incomplete"。
        critical_rows = [
            source_row("okx_news", "official_required", 1,
                       "INSUFFICIENT_EVIDENCE"),
            source_row("rss_en", "required", 1, "INSUFFICIENT_EVIDENCE"),
        ] + [
            source_row(f"rss:{name}", "required_subsource", 1,
                       "INSUFFICIENT_EVIDENCE")
            for name in ("bitcoinist", "coindesk", "cointelegraph",
                         "cryptoslate", "decrypt", "theblock")
        ]
        audit = {
            "artifact_type": "scheduled_news_source_health_audit",
            "generated_at_cst": "2026-08-12T05:37:59+08:00",
            "forward_start_cst": forward_start,
            "minimum_window_hours": minimum_window_hours,
            "target_rate": target_rate,
            # 五个安全标志为必填（生产端拒绝会改库/触发重采/触发派发/授权执行的证据）。
            "production_mutation": False,
            "collector_retry_triggered": False,
            "stage_dispatch_triggered": False,
            "production_execution_authorized": False,
            "orders_placed": 0,
            "overall_status": "PENDING_FORWARD_EVIDENCE",
            "forward_after_remediation": {
                "critical_status": "INSUFFICIENT_EVIDENCE",
                "all_sources_status": "INSUFFICIENT_EVIDENCE",
                "sources": critical_rows + [
                    source_row("panews", "optional", 0,
                               "INSUFFICIENT_EVIDENCE"),
                ],
            },
        }
        result = refresh_news_source_health(
            artifact,
            audit,
            audit_relative_path="news-source-health-audit.json",
        )
        headline = result["snapshot"]["datasets"]["headline"][0]
        # 关键源 8 行全 complete；可选 panews 未 complete 只进「全部源」分母。
        self.assertEqual(8, headline["news_forward_complete_source_slots"])
        self.assertEqual(8, headline["news_forward_expected_source_slots"])
        self.assertEqual(8, headline["news_forward_all_complete_source_slots"])
        self.assertEqual(9, headline["news_forward_all_expected_source_slots"])
        self.assertEqual(
            "INSUFFICIENT_EVIDENCE", headline["news_forward_critical_status"])
        table = result["manifest"]["tables"][0]
        self.assertEqual("news_source_health", table["dataset"])
        section = next(
            block for block in result["manifest"]["blocks"]
            if block.get("id") == "news_source_section"
        )
        self.assertIn("至少24小时", section["body"])
        self.assertEqual(
            "未达标", result["snapshot"]["datasets"]["gates"][0]["status"])

    def test_refreshes_corrected_credibility_and_production_signal(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "sources": [{"id": "calibration"}],
                "cards": [{"id": "credibility_card"}],
                "charts": [{"id": "credibility_chart"}],
                "blocks": [{"id": "credibility_section", "body": "old"}],
            },
            "snapshot": {"datasets": {
                "headline": [{}],
                "credibility": [],
                "gates": [{"goal": "分析可信度"}],
            }},
        }
        rule = {
            "n": 200,
            "precision_after_cost": 0.3,
            "wilson_95_low": 0.24,
            "wilson_95_high": 0.36,
            "distinct_days": 7,
            "distinct_cycles": 120,
            "mean_signed_return_after_cost": -0.002,
        }
        calibration = {
            "schema_version": 2,
            "generated_at_utc": "2026-08-11T18:26:38Z",
            "holdout": {
                "precision": 0.3939, "n": 3176,
                "wilson_95_low": 0.377, "wilson_95_high": 0.411,
                "ece": 0.0655, "distinct_days": 7,
                "distinct_cycles": 146,
                "mean_signed_return_after_cost": -0.0022,
            },
            "current_alignment_rule_test_baseline": {
                "15m": dict(rule), "1H": dict(rule), "4H": dict(rule),
            },
        }
        selected_holdout = {
            "precision": 0.3984, "n": 1541,
            "wilson_95_low": 0.3743, "wilson_95_high": 0.4231,
            "ece": 0.0592, "distinct_days": 7,
            "distinct_cycles": 146,
            "mean_signed_return_after_cost": -0.0021,
        }
        policy = {
            "artifact_type": "multitimeframe_policy_diagnostic",
            "generated_at_utc": "2026-08-11T18:34:01Z",
            "selected_policy": {
                "policy": "flow_agreement",
                "historical_holdout": selected_holdout,
            },
            "oracle_ranking_diagnostic": {"historical_holdout": {
                "complete_observations": 5362,
                "oracle_any_candidate_success_rate": 0.8948,
                "capture_rate_when_any_candidate_succeeds": 0.4452,
            }},
            "acceptance": {"target_precision": 0.9},
        }
        signal = {
            "artifact_type": "analysis_signal_forward_quality_audit",
            "generated_at_cst": "2026-08-12T02:19:34+08:00",
            "retrospective_evaluation": {
                "precision_after_cost": 0.4545, "n": 11,
                "wilson_95_low": 0.2127, "wilson_95_high": 0.7199,
                "distinct_days": 6, "distinct_cycles": 11,
                "mean_signed_return_after_cost": -0.0038,
            },
        }
        result = refresh_credibility_evidence(
            artifact, calibration, policy, signal,
            calibration_relative_path="calibration.json",
            policy_relative_path="policy.json",
            signal_relative_path="signal.json",
        )
        headline = result["snapshot"]["datasets"]["headline"][0]
        self.assertEqual(11, headline["production_signal_n"])
        self.assertEqual(1541, headline["policy_holdout_n"])
        methods = [
            row["method"] for row in result["snapshot"]["datasets"]["credibility"]
        ]
        self.assertIn("固定候选族事后神谕（不可交易）", methods)
        gate = result["snapshot"]["datasets"]["gates"][0]
        self.assertEqual("未达标", gate["status"])
        self.assertEqual(
            "credibility_evidence", result["manifest"]["cards"][0]["sourceId"])

    def test_refreshes_positioning_without_conflating_isolated_and_natural(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "sources": [{"id": "official_okx"}],
                "tables": [],
                "charts": [{"id": "coverage_chart"}],
                "blocks": [
                    {"id": "data_section", "body": "old"},
                    {"id": "coverage_block", "type": "chart"},
                ],
            },
            "snapshot": {"datasets": {
                "headline": [{}],
                "coverage": [{
                    "data_family": "4H", "coverage_rate": 0.98595,
                    "valid_symbols": 421, "universe": 427,
                }],
                "gates": [{"goal": "关键数据完善率"}],
                "fast_source_health": [{
                    "usable_rate": 0.970982,
                    "forward_usable_rate": 1.0,
                    "forward_expected_slots": 5,
                    "forward_minimum_slots": 96,
                }],
            }},
        }
        natural = {
            "artifact_type": "positioning_coverage_audit",
            "source": "okx_rest_contract_long_short_ratio",
            "generated_at_utc": "2026-08-11T19:01:46Z",
            "latest_batch_collected_ts": "2026-08-11T19:01:08Z",
            "minimum_rate": 0.99, "coverage_rate": 426 / 427,
            "valid_symbols": 426, "universe_symbols": 427,
            "missing_symbols": ["APLD-USDT-SWAP"], "status": "PASSED",
        }
        isolated = dict(natural)
        isolated.update({
            "latest_batch_collected_ts": "2026-08-11T18:46:40Z",
            "coverage_rate": 1.0, "valid_symbols": 427,
            "missing_symbols": [],
            "duplicate_symbols": [],
            "extra_symbols": [],
        })
        result = refresh_positioning_coverage(
            artifact, natural, isolated,
            natural_relative_path="positioning.json",
            isolated_relative_path="positioning-isolated.json",
        )
        rows = result["snapshot"]["datasets"]["positioning_coverage"]
        self.assertEqual([427, 426], [row["valid_symbols"] for row in rows])
        self.assertEqual("APLD-USDT-SWAP", rows[1]["missing"])
        coverage = next(
            row for row in result["snapshot"]["datasets"]["coverage"]
            if row["data_family"] == "official_positioning_1H"
        )
        self.assertEqual("达标", coverage["status"])
        self.assertEqual(
            "https://www.okx.com/docs-v5/en/",
            result["manifest"]["sources"][0]["href"],
        )

    def test_refreshes_selective_models_with_confirmation_before_holdout(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "sources": [{"id": "credibility_evidence", "query": {}}],
                "cards": [{"id": "credibility_card"}],
                "charts": [{"id": "credibility_chart"}],
                "blocks": [{"id": "credibility_section", "body": "old"}],
            },
            "snapshot": {"datasets": {
                "headline": [{
                    "production_signal_precision": 0.4545,
                    "production_signal_n": 11,
                }],
                "credibility": [
                    {
                        "method": "增强模型（右截尾修正）",
                        "precision_after_cost": 0.3955, "n": 3206,
                        "wilson_low": 0.3787, "wilson_high": 0.4125,
                        "ece": 0.06, "days": 7, "cycles": 147,
                        "mean_signed_return_after_cost": -0.002,
                        "result_type": "enhanced_historical_holdout",
                        "evidence_class": "historical_diagnostic",
                    },
                    {
                        "method": "实际生产LLM信号 4H",
                        "precision_after_cost": 0.4545, "n": 11,
                        "wilson_low": 0.21, "wilson_high": 0.72,
                        "ece": None, "days": 6, "cycles": 11,
                        "mean_signed_return_after_cost": -0.003,
                        "result_type": "production_signal_retrospective",
                        "evidence_class": "retrospective_not_independent_forward",
                    },
                    {
                        "method": "生产可信度目标",
                        "precision_after_cost": 0.9, "n": 100,
                        "wilson_low": None, "wilson_high": None,
                        "ece": 0.05, "days": 5, "cycles": 100,
                        "mean_signed_return_after_cost": None,
                        "result_type": "acceptance_target",
                        "evidence_class": "target",
                    },
                ],
                "gates": [{"goal": "分析可信度"}],
            }},
        }

        def diagnostic(model: str, confirmation: float, holdout: float) -> dict:
            def result(precision: float, n: int, ece: float) -> dict:
                return {
                    "precision": precision, "n": n,
                    "wilson_95_low": precision - 0.03,
                    "wilson_95_high": precision + 0.03,
                    "ece": ece, "distinct_days": 7,
                    "distinct_cycles": 147,
                    "mean_signed_return_after_cost": -0.002,
                }
            return {
                "artifact_type": "selective_multitimeframe_diagnostic",
                "generated_at_utc": "2026-08-12T03:44:32+08:00",
                "input_panel": "reports/quality/research_panel.csv",
                "model_family": {"selected_model": model},
                "threshold_selection": {
                    "precision": 0.55, "n": 230,
                },
                "evaluation": {
                    "internal_confirmation": result(confirmation, 424, 0.15),
                    "historical_holdout": result(holdout, 1366, 0.01),
                },
                "acceptance": {
                    "target_precision": 0.9,
                    "production_status": "NO_CHANGE_ALLOWED",
                    "confidence_90_status": "NOT_PROVEN",
                },
            }

        shared = diagnostic("shared", 0.3538, 0.5117)
        independent = diagnostic("independent", 0.4021, 0.4254)
        result = refresh_selective_credibility(
            artifact, shared, independent,
            shared_relative_path="shared.json",
            independent_relative_path="independent.json",
        )
        headline = result["snapshot"]["datasets"]["headline"][0]
        self.assertEqual(0.3538, headline["shared_confirmation_precision"])
        self.assertEqual(0.4021, headline["independent_confirmation_precision"])
        self.assertEqual(
            "credibility_primary",
            result["manifest"]["charts"][0]["dataset"],
        )
        self.assertEqual("未达标", result["snapshot"]["datasets"]["gates"][0]["status"])
        self.assertIn(
            "内部确认窗降至35.380%",
            result["manifest"]["blocks"][0]["body"],
        )

    def test_refreshes_contract_statistics_with_failed_then_passed_natural_cycle(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "sources": [{"id": "official_okx"}],
                "tables": [],
                "charts": [{"id": "coverage_chart"}],
                "blocks": [
                    {"id": "data_section", "body": "old"},
                    {"id": "coverage_block", "type": "chart"},
                    {"id": "positioning_coverage_block", "type": "table"},
                ],
            },
            "snapshot": {"datasets": {
                "headline": [{}],
                "coverage": [
                    {"data_family": "4H", "coverage_rate": 0.98595,
                     "valid_symbols": 421, "universe": 427,
                     "target_rate": 0.99, "status": "未达标"},
                    {"data_family": "official_positioning_1H",
                     "coverage_rate": 426 / 427,
                     "valid_symbols": 426, "universe": 427,
                     "target_rate": 0.99, "status": "达标"},
                ],
                "fast_source_health": [{
                    "usable_rate": 0.970982,
                    "forward_expected_slots": 12,
                    "forward_minimum_slots": 96,
                }],
                "gates": [{"goal": "关键数据完善率"}],
            }},
        }
        natural = {
            "artifact_type": "contract_statistics_coverage_audit",
            "generated_at_utc": "2026-08-11T20:31:56Z",
            "source": "okx_rest_contract_oi_taker_15m",
            "latest_cycle_id": "2026-08-12T04:30",
            "minimum_coverage": 0.99,
            "maximum_source_lag_seconds": 5400,
            "universe_symbols": 427,
            "valid_symbols": 427,
            "coverage_rate": 1.0,
            "direct_valid_symbols": 420,
            "direct_coverage_rate": 420 / 427,
            "carried_forward_valid_symbols": 7,
            "carry_forward_rate": 7 / 427,
            "method_counts": {
                "rubik_common_bucket": 404,
                "official_public_oi_trades_candle_reconciled_fallback": 16,
                "official_previous_batch_carry_forward": 7,
            },
            "valid_method_counts": {
                "rubik_common_bucket": 404,
                "official_public_oi_trades_candle_reconciled_fallback": 16,
                "official_previous_batch_carry_forward": 7,
            },
            "carry_forward_semantics": (
                "availability continuity within 90m; excluded from model features; "
                "not counted as direct current-batch collection"
            ),
            "missing_symbols": [],
            "duplicate_symbols": [],
            "extra_symbols": [],
            "source_lag_seconds": {"min": 991.0, "max": 4591.0},
            "availability_checks": {
                "coverage_at_least_target": True,
                "single_collected_timestamp": True,
                "no_duplicates": True,
                "no_extra_symbols": True,
                "universe_nonempty": True,
            },
            "analysis_ready_checks": {
                "direct_coverage_at_least_target": False,
                "single_collected_timestamp": True,
                "no_duplicates": True,
                "no_extra_symbols": True,
                "universe_nonempty": True,
            },
            "availability_status": "PASSED",
            "analysis_ready_status": "NOT_MET",
            "status": "NOT_MET",
            "overall_status": "NOT_MET",
            "production_database_writes": 0,
            "orders_placed": 0,
            "forward_after_remediation": {
                "start_cst": "2026-08-12T04:15:00+08:00",
                "end_exclusive_cst": "2026-08-12T05:00:00+08:00",
                "expected_slots": 3,
                "observed_slots": 3,
                "missing_slots": 0,
                "passed_slots": 3,
                "failed_slots": 0,
                "slot_pass_rate": 1.0,
                "analysis_ready_slots": 3,
                "analysis_not_ready_slots": 0,
                "analysis_ready_slot_pass_rate": 1.0,
                "expected_symbol_rows": 1281,
                "valid_symbol_rows": 1281,
                "availability_coverage_rate": 1.0,
                "direct_valid_symbol_rows": 1281,
                "direct_coverage_rate": 1.0,
                "carried_forward_valid_symbol_rows": 0,
                "carry_forward_rate": 0.0,
                "target_rate": 0.99,
                "minimum_slots": 96,
                "status": "INSUFFICIENT_EVIDENCE",
                "analysis_ready_status": "INSUFFICIENT_EVIDENCE",
                "missing_slot_semantics": "unavailable_and_in_denominator",
                "slots": [
                    {
                        "cycle_id": f"2026-08-12T04:{minute}",
                        "universe_symbols": 427,
                        "batch_rows": 427,
                        "valid_symbols": 427,
                        "availability_coverage_rate": 1.0,
                        "direct_valid_symbols": 427,
                        "direct_coverage_rate": 1.0,
                        "carried_forward_valid_symbols": 0,
                        "carry_forward_rate": 0.0,
                        "duplicate_symbols": 0,
                        "extra_symbols": 0,
                        "single_collected_timestamp": True,
                        "status": "PASSED",
                        "analysis_ready_status": "PASSED",
                    }
                    for minute in ("15", "30", "45")
                ],
            },
            "recent_batches": [
                {
                    "cycle_id": "2026-08-12T04:30",
                    "observed_universe_symbols": 427,
                    "observed_coverage_rate": 1.0,
                    "missing_symbols": [],
                    "single_collected_timestamp": True,
                },
                {
                    "cycle_id": "2026-08-12T04:15",
                    "observed_universe_symbols": 364,
                    "observed_coverage_rate": 364 / 427,
                    "missing_symbols": [f"S{i}" for i in range(63)],
                    "single_collected_timestamp": True,
                },
            ],
        }
        isolated = {
            "artifact_type": "contract_statistics_isolated_acceptance",
            "generated_at_utc": "2026-08-11T20:05:50Z",
            "source": "okx_rest_contract_oi_taker_15m",
            "cycle_id": "2026-08-12T04:00",
            "audit": {
                "coverage_rate": 1.0, "valid_symbols": 427,
                "universe_symbols": 427, "missing_symbols": [],
                "direct_valid_symbols": 408,
                "direct_coverage_rate": 408 / 427,
                "carried_forward_valid_symbols": 19,
                "carry_forward_rate": 19 / 427,
                "method_counts": {
                    "rubik_common_bucket": 408,
                    "official_previous_batch_carry_forward": 19,
                },
                "valid_method_counts": {
                    "rubik_common_bucket": 408,
                    "official_previous_batch_carry_forward": 19,
                },
                "carry_forward_semantics": (
                    "availability continuity within 90m; excluded from model features; "
                    "not counted as direct current-batch collection"
                ),
                "checks": {
                    "coverage_at_least_99pct": True,
                    "no_extra_symbols": True,
                    "no_duplicate_symbols": True,
                    "valid_values_and_times": True,
                    "single_collected_timestamp": True,
                    "sqlite_quick_check": True,
                },
                "status": "PASSED",
            },
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        result = refresh_contract_statistics_coverage(
            copy.deepcopy(artifact), natural, isolated,
            natural_relative_path="contract-natural.json",
            isolated_relative_path="contract-isolated.json",
        )
        rows = result["snapshot"]["datasets"]["contract_statistics_coverage"]
        self.assertEqual(
            [427, 1281, 364, 427],
            [row["valid_symbols"] for row in rows],
        )
        self.assertEqual("INSUFFICIENT_EVIDENCE", rows[1]["status"])
        self.assertEqual("OBSERVED_NOT_MET", rows[2]["status"])
        coverage = next(
            row for row in result["snapshot"]["datasets"]["coverage"]
            if row["data_family"] == "official_contract_stats_15m"
        )
        self.assertEqual(420 / 427, coverage["coverage_rate"])
        self.assertEqual(1.0, coverage["availability_coverage_rate"])
        self.assertEqual(420 / 427, coverage["direct_coverage_rate"])
        self.assertEqual(7 / 427, coverage["carry_forward_rate"])
        self.assertEqual(1.0, coverage["forward_direct_coverage_rate"])
        self.assertEqual(3, coverage["forward_expected_slots"])
        self.assertEqual("未达标", coverage["status"])
        self.assertEqual(420 / 427, rows[-1]["direct_coverage_rate"])
        self.assertEqual(7 / 427, rows[-1]["carry_forward_rate"])
        self.assertEqual("coverage_evidence", result["manifest"]["charts"][0]["sourceId"])

        tampered = copy.deepcopy(natural)
        tampered["forward_after_remediation"]["slots"][0][
            "direct_valid_symbols"] = 426
        with self.assertRaises(ValueError):
            refresh_contract_statistics_coverage(
                copy.deepcopy(artifact), tampered, isolated,
                natural_relative_path="contract-natural.json",
                isolated_relative_path="contract-isolated.json",
            )
        self.assertEqual("未达标", result["snapshot"]["datasets"]["gates"][0]["status"])

    def test_contract_statistics_report_rejects_missing_direct_carry_split(self):
        artifact = {
            "surface": "report",
            "manifest": {"sources": [], "tables": [], "charts": [], "blocks": []},
            "snapshot": {"datasets": {}},
        }
        # 只读安全标志在拆分字段之前先被校验；不带就会提前抛"not read-only"，
        # 把本用例真正要钉的 "split fields missing" 遮掉。
        natural = {
            "artifact_type": "contract_statistics_coverage_audit",
            "source": "okx_rest_contract_oi_taker_15m",
            "coverage_rate": 1.0,
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        isolated = {
            "artifact_type": "contract_statistics_isolated_acceptance",
            "source": "okx_rest_contract_oi_taker_15m",
            "audit": {"coverage_rate": 1.0},
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        with self.assertRaisesRegex(ValueError, "split fields missing"):
            refresh_contract_statistics_coverage(
                artifact,
                natural,
                isolated,
                natural_relative_path="natural.json",
                isolated_relative_path="isolated.json",
            )

    def test_contract_statistics_report_rejects_non_quarter_isolated_cycle(self):
        split = {
            "universe_symbols": 2,
            "valid_symbols": 2,
            "coverage_rate": 1.0,
            "direct_valid_symbols": 1,
            "direct_coverage_rate": 0.5,
            "carried_forward_valid_symbols": 1,
            "carry_forward_rate": 0.5,
            "method_counts": {
                "rubik_common_bucket": 1,
                "official_previous_batch_carry_forward": 1,
            },
            "valid_method_counts": {
                "rubik_common_bucket": 1,
                "official_previous_batch_carry_forward": 1,
            },
            "carry_forward_semantics": (
                "availability continuity within 90m; excluded from model features; "
                "not counted as direct current-batch collection"
            ),
        }
        artifact = {
            "surface": "report",
            "manifest": {"sources": [], "tables": [], "charts": [], "blocks": []},
            "snapshot": {"datasets": {}},
        }
        natural = {
            **split,
            "artifact_type": "contract_statistics_coverage_audit",
            "source": "okx_rest_contract_oi_taker_15m",
            "latest_cycle_id": "2026-08-12T10:30",
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        isolated = {
            "artifact_type": "contract_statistics_isolated_acceptance",
            "source": "okx_rest_contract_oi_taker_15m",
            "cycle_id": "2026-08-12T10:35",
            "audit": dict(split),
            "production_database_writes": 0,
            "orders_placed": 0,
        }
        with self.assertRaisesRegex(ValueError, "not a 15m boundary"):
            refresh_contract_statistics_coverage(
                artifact,
                natural,
                isolated,
                natural_relative_path="natural.json",
                isolated_relative_path="isolated.json",
            )

    def test_refreshes_daily_gate_without_changing_other_gates(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "title": ". 四项目标实施与前向验收（old）",
                "generatedAt": "old",
                "sources": [{"id": "report_quality", "query": {}}],
                "cards": [{
                    "id": "push_card", "metrics": [], "description": "old"}],
                "blocks": [
                    {"id": "title", "body": "# old"},
                    {"id": "executive_summary", "body": (
                        "最新日报也已在备份和dry-run后受控修正，Push结构与"
                        "投递近14日均高于99%。历史日报有效率仍为66.667%。")},
                    {"id": "reports_section", "body": "old"},
                    {"id": "gates_section", "body": "old"},
                ],
            },
            "snapshot": {
                "generatedAt": "old",
                "datasets": {
                    "headline": [{}],
                    # Push 两行与日报行同为必需：生产端要求各恰好一行可更新，
                    # 兼容旧名（Push 结构校验 / Push 投递确认）并统一改写为新名。
                    "report_quality": [
                        {
                            "artifact_family": "Push 结构校验",
                            "completeness_rate": 0.5,
                        },
                        {
                            "artifact_family": "Push 投递确认",
                            "completeness_rate": 0.5,
                        },
                        {
                            "artifact_family": "日报历史校验",
                            "completeness_rate": 0.5,
                        },
                    ],
                    "gates": [
                        {"goal": "关键数据完善率", "status": "未达标"},
                        {"goal": "报告与推送完整度", "status": "部分达标"},
                    ],
                },
            },
        }
        audit = {
            "evaluated_at_cst": "2026-08-12 01:17:00",
            "window": {"start_date": "2026-07-28", "end_date": "2026-08-11"},
            "expected": 15,
            "valid": 15,
            "completeness_rate": 1.0,
            "target_rate": 0.99,
            # 日报审计的三个安全标志同为必填（生产端拒绝会写库/自动外发/授权下单的证据）。
            "auto_send": False,
            "database_write": False,
            "production_order_authorized": False,
        }
        before_other = copy.deepcopy(
            artifact["snapshot"]["datasets"]["gates"][0])
        result = refresh_report_completeness(
            artifact,
            audit,
            push_completeness_audit(),
            audit_relative_path="reports/quality/audit.json",
            push_audit_relative_path="reports/quality/push-audit.json",
        )
        rows = {
            row["artifact_family"]: row
            for row in result["snapshot"]["datasets"]["report_quality"]
        }
        daily = rows["日报历史校验"]
        report_gate = result["snapshot"]["datasets"]["gates"][1]
        self.assertEqual(daily["numerator"], 15)
        self.assertEqual(daily["status"], "达标")
        self.assertEqual(report_gate["status"], "达标")
        # Push 两行按新命名就地改写，且与审计工件计数一致。
        self.assertEqual(rows["Push 报告完整性"]["numerator"], 14 * 96)
        self.assertEqual(rows["Push 精确送达"]["denominator"], 14 * 96)
        self.assertEqual(rows["Push 报告完整性"]["status"], "达标")
        self.assertEqual(rows["Push 精确送达"]["status"], "达标")
        self.assertEqual(
            result["snapshot"]["datasets"]["gates"][0], before_other)
        self.assertIn(
            "daily_report_validation_rate",
            [item["field"] for item in result["manifest"]["cards"][0]["metrics"]],
        )
        self.assertEqual(
            f"# {result['manifest']['title']}",
            result["manifest"]["blocks"][0]["body"],
        )

    def test_refreshes_fast_with_scheduled_slot_denominator(self):
        artifact = {
            "surface": "report",
            "manifest": {
                "title": ". 四项目标实施与前向验收（old）",
                "generatedAt": "old",
                "sources": [],
                "cards": [{
                    "id": "coverage_card", "description": "old", "metrics": []}],
                "tables": [{
                    "id": "source_health_table",
                    "dataset": "source_health",
                    "sourceId": "baseline",
                    "columns": [],
                }],
                "blocks": [
                    {"id": "title", "body": "# old"},
                    {"id": "executive_summary", "body": (
                        "最低关键数据覆盖仍为98.595%。")},
                    {"id": "data_section", "body": "old"},
                    {"id": "source_health_block", "type": "table"},
                    {"id": "gates_section", "body": "old"},
                    {"id": "methods", "body": "old"},
                ],
            },
            "snapshot": {
                "generatedAt": "old",
                "datasets": {
                    "headline": [{"minimum_coverage_rate": 0.98595}],
                    "coverage": [
                        {"data_family": "ticker", "coverage_rate": 1.0},
                        {"data_family": "4H", "coverage_rate": 0.98595},
                    ],
                    "source_health": [
                        {"source": "fast", "usable_rate": 0.982},
                        {"source": "news", "usable_rate": 1.0},
                    ],
                    "gates": [{
                        "goal": "关键数据完善率", "status": "未达标"}],
                },
            },
        }
        audit = {
            "artifact_type": "scheduled_source_health_audit",
            "generated_at_cst": "2026-08-12T01:50:10+08:00",
            "as_of_cst": "2026-08-12T01:50:10+08:00",
            "target_rate": 0.99,
            "overall_status": "PENDING_FORWARD_EVIDENCE",
            "rolling": {
                "start_cst": "2026-07-29T02:00:00+08:00",
                "end_exclusive_cst": "2026-08-12T02:00:00+08:00",
                "expected_slots": 1344,
                "observed_rows": 1331,
                "missing_slots": 13,
                "complete_slots": 1296,
                "complete_rate": 0.964286,
                "available_slots": 1305,
                "available_rate": 0.970982,
                "raw_status_counts": {"ok": 1296, "degraded": 9, "error": 26},
            },
            "forward_after_remediation": {
                "start_cst": "2026-08-12T01:45:00+08:00",
                "end_exclusive_cst": "2026-08-12T02:00:00+08:00",
                "expected_slots": 1,
                "minimum_slots": 96,
                "complete_slots": 1,
                "complete_rate": 1.0,
                "available_slots": 1,
                "available_rate": 1.0,
                "status": "INSUFFICIENT_EVIDENCE",
            },
        }
        result = refresh_source_health(
            artifact,
            audit,
            audit_relative_path="reports/quality/source-health-audit.json",
        )
        headline = result["snapshot"]["datasets"]["headline"][0]
        self.assertEqual(0.964286, headline["minimum_coverage_rate"])
        self.assertEqual(
            ["news"],
            [row["source"] for row in result["snapshot"]["datasets"]["source_health"]],
        )
        fast = result["snapshot"]["datasets"]["fast_source_health"][0]
        self.assertEqual(1344, fast["runs"])
        self.assertEqual(13, fast["missing_slots"])
        self.assertEqual("未达标", fast["status"])
        self.assertIn(
            "fast_source_health_table",
            [table["id"] for table in result["manifest"]["tables"]],
        )
        self.assertEqual("source_health", result["manifest"]["sources"][-1]["id"])
        strict_table = next(
            table for table in result["manifest"]["tables"]
            if table["id"] == "fast_source_health_table"
        )
        self.assertEqual(
            {"field": "complete_rate", "direction": "asc"},
            strict_table["defaultSort"],
        )
        self.assertEqual(
            f"# {result['manifest']['title']}",
            result["manifest"]["blocks"][0]["body"],
        )

        rerun = refresh_source_health(
            result,
            audit,
            audit_relative_path="reports/quality/source-health-audit.json",
        )
        self.assertEqual(
            ["fast"],
            [row["source"] for row in rerun["snapshot"]["datasets"]
             ["fast_source_health"]],
        )
        self.assertEqual(
            1,
            sum(
                table["id"] == "fast_source_health_table"
                for table in rerun["manifest"]["tables"]
            ),
        )


if __name__ == "__main__":
    unittest.main()
