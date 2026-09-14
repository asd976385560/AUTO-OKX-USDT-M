# -*- coding: utf-8 -*-
"""口径迁移契约：预注册激活边界只向前生效，且孪生阈值必须同步移动。

covers V2.1 §1/§3（完善率与完整度 99%→95%）与 §2（前向校准门 90%→80%）。
黑名单三审计（多周期/资产分类/合约统计）与消费端引用必须原地不动。
"""
import ast
import inspect
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _acceptance_thresholds as thresholds  # noqa: E402
import audit_asset_class_coverage  # noqa: E402
import audit_contract_statistics_coverage  # noqa: E402
import audit_market_feature_coverage  # noqa: E402
import audit_market_field_coverage  # noqa: E402
import audit_multitimeframe_coverage  # noqa: E402
import audit_news_source_health  # noqa: E402
import audit_periodic_report_completeness  # noqa: E402
import audit_positioning_coverage  # noqa: E402
import audit_push_completeness  # noqa: E402
import audit_report_completeness  # noqa: E402
import audit_model_shadow_label_quality as auditor  # noqa: E402
import audit_source_health  # noqa: E402
import evaluate_multitimeframe_model_shadow as evaluator  # noqa: E402
from refresh_goal_acceptance_report import (  # noqa: E402
    _validated_coverage_target,
)


def _literal_assignment(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name
                   for target in targets):
                matches.append(ast.literal_eval(node.value))
    if len(matches) != 1:
        raise AssertionError(f"expected one assignment for {name}, got {matches}")
    return matches[0]


def _cli_default(module, option: str):
    path = Path(module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and any(
                isinstance(arg, ast.Constant) and arg.value == option
                for arg in node.args
            )
        ):
            continue
        defaults = [kw.value for kw in node.keywords if kw.arg == "default"]
        if len(defaults) == 1:
            matches.append(ast.literal_eval(defaults[0]))
    if len(matches) != 1:
        raise AssertionError(
            f"expected one CLI default for {option}, got {matches}")
    return matches[0]


BEFORE = "2026-08-15T19:59:59+08:00"
AT = "2026-08-15T20:00:00+08:00"
AFTER = "2026-08-16T00:00:00+08:00"
V3_BEFORE = "2026-08-20T17:59:59+08:00"
V3_AT = "2026-08-20T18:00:00+08:00"
V4_BEFORE = "2026-08-21T17:59:59+08:00"
V4_AT = "2026-08-21T18:00:00+08:00"
LIVENESS_BEFORE = "2026-08-21T18:59:59+08:00"
LIVENESS_AT = "2026-08-21T19:00:00+08:00"
WEEKLY_PROFIT_DECISION = "2026-08-26T14:26:06+08:00"
WEEKLY_PROFIT_ACTIVATION = "2026-08-31T08:00:00+08:00"
MISSED_EVIDENCE_DECISION = "2026-08-31T19:32:24+08:00"
MISSED_EVIDENCE_ACTIVATION = "2026-09-01T08:00:00+08:00"
SLA_TIER2_BEFORE_DECISION = "2026-08-27T01:09:05+08:00"
SLA_TIER2_DECISION = "2026-08-27T01:09:06+08:00"
SLA_TIER2_BEFORE_ACTIVATION = "2026-08-27T01:59:59+08:00"
SLA_TIER2_ACTIVATION = "2026-08-27T02:00:00+08:00"
SLA_TIER2_EXCEPTION_BEFORE = "2026-08-27T08:04:12+08:00"
SLA_TIER2_EXCEPTION_DECISION = "2026-08-27T08:04:13+08:00"


class ActivationBoundaryTests(unittest.TestCase):
    def test_missed_opportunity_evidence_contract_is_forward_only(self):
        self.assertEqual(
            MISSED_EVIDENCE_DECISION,
            thresholds.MISSED_OPPORTUNITY_EVIDENCE_DECISION_RECORDED_CST,
        )
        self.assertEqual(
            MISSED_EVIDENCE_ACTIVATION,
            thresholds.MISSED_OPPORTUNITY_EVIDENCE_ACTIVATION_CST,
        )
        self.assertFalse(
            thresholds.missed_opportunity_evidence_contract_active(
                "2026-09-01 07:59:59"))
        self.assertTrue(
            thresholds.missed_opportunity_evidence_contract_active(
                "2026-09-01 08:00:00"))
        facts = thresholds.missed_opportunity_evidence_registration_facts(
            "2026-09-01 08:00:00")
        self.assertEqual(MISSED_EVIDENCE_DECISION, facts["decision_recorded"])
        self.assertEqual(MISSED_EVIDENCE_ACTIVATION, facts["activation_cst"])
        self.assertEqual(">=", facts["comparison"])
        self.assertTrue(facts["active"])
        self.assertFalse(facts["legacy_reports_rejudged"])

    def test_weekly_profit_gate_starts_at_first_complete_future_week(self):
        decision = thresholds.parse_cst(WEEKLY_PROFIT_DECISION)
        activation = thresholds.parse_cst(WEEKLY_PROFIT_ACTIVATION)
        self.assertEqual(
            WEEKLY_PROFIT_DECISION,
            thresholds.WEEKLY_TRADING_NET_PROFIT_DECISION_CST,
        )
        self.assertEqual(
            WEEKLY_PROFIT_ACTIVATION,
            thresholds.WEEKLY_TRADING_NET_PROFIT_ACTIVATION_CST,
        )
        self.assertGreater(activation, decision)
        self.assertEqual(0, activation.weekday())
        self.assertEqual((8, 0, 0), (
            activation.hour, activation.minute, activation.second))
        self.assertEqual(0.0, thresholds.WEEKLY_TRADING_NET_PROFIT_TARGET_USDT)
        self.assertEqual(">", thresholds.WEEKLY_TRADING_NET_PROFIT_COMPARISON)

    def test_coverage_gate_is_forward_only(self):
        self.assertEqual(0.99, thresholds.coverage_target_rate(BEFORE))
        self.assertEqual(0.95, thresholds.coverage_target_rate(AT))
        self.assertEqual(0.95, thresholds.coverage_target_rate(AFTER))

    def test_calibration_gate_is_forward_only(self):
        self.assertEqual(0.90, thresholds.shadow_target_precision(BEFORE))
        self.assertEqual(0.80, thresholds.shadow_target_precision(AT))
        self.assertEqual(0.80, thresholds.shadow_target_precision(AFTER))

    def test_boundary_is_registered_once_for_both_migrations(self):
        # 两条迁移同批部署，边界必须是同一时刻；改一个忘了另一个会在这里红。
        self.assertEqual(
            thresholds.COVERAGE_TARGET_ACTIVATION_CST,
            thresholds.SHADOW_CALIBRATION_ACTIVATION_CST,
        )

    def test_migration_facts_carry_both_calibers(self):
        facts = thresholds.coverage_migration_facts(AT)
        self.assertTrue(facts["activated"])
        self.assertEqual(0.99, facts["legacy_target_rate"])
        self.assertEqual(0.95, facts["effective_target_rate"])
        self.assertFalse(
            thresholds.coverage_migration_facts(BEFORE)["activated"])

    def test_legacy_diagnostics_exclude_target_dependent_rates(self):
        diagnostics = thresholds.legacy_rate_diagnostics(
            {"complete_rate": 0.995, "slot_pass_rate": 1.0},
            target_dependent=("slot_pass_rate",),
        )
        self.assertEqual(
            {"complete_rate": True},
            diagnostics["rates_at_least_legacy_target"],
        )
        self.assertEqual(
            ["slot_pass_rate"],
            diagnostics["target_dependent_rates_excluded"],
        )
        self.assertTrue(diagnostics["diagnostic_only"])

    def test_v3_sla_gates_are_forward_only(self):
        self.assertEqual(
            570, thresholds.sla_analysis_deadline_seconds(V3_BEFORE))
        self.assertEqual(600, thresholds.sla_analysis_deadline_seconds(V3_AT))
        self.assertEqual(
            780, thresholds.sla_business_terminal_deadline_seconds(V3_BEFORE))
        self.assertEqual(
            780, thresholds.sla_business_terminal_deadline_seconds(V3_AT))
        self.assertEqual(
            840, thresholds.sla_record_reconcile_deadline_seconds(V3_BEFORE))
        self.assertEqual(
            840, thresholds.sla_record_reconcile_deadline_seconds(V3_AT))
        self.assertEqual(
            840, thresholds.push_same_slot_max_age_seconds(V3_BEFORE))
        self.assertEqual(
            870, thresholds.push_same_slot_max_age_seconds(V3_AT))
        self.assertEqual(
            840, thresholds.post_push_monitor_deadline_seconds(V3_BEFORE))
        self.assertEqual(
            900, thresholds.post_push_monitor_deadline_seconds(V3_AT))

    def test_v3_pass_tier_push_latency_and_review_semantics_share_boundary(self):
        self.assertIsNone(thresholds.sla_pass_rate_target(V3_BEFORE))
        self.assertEqual(0.80, thresholds.sla_pass_rate_target(V3_AT))
        self.assertIsNone(
            thresholds.push_delivery_latency_target_seconds(V3_BEFORE))
        self.assertEqual(
            30, thresholds.push_delivery_latency_target_seconds(V3_AT))
        self.assertFalse(
            thresholds.structured_position_actions_count_as_review(V3_BEFORE))
        self.assertTrue(
            thresholds.structured_position_actions_count_as_review(V3_AT))
        self.assertEqual(
            thresholds.SLA_V3_REGISTRATION_ACTIVATION_CST,
            thresholds.SLA_PASS_RATE_TIER_ACTIVATION_CST,
        )
        self.assertEqual(
            thresholds.SLA_V3_REGISTRATION_ACTIVATION_CST,
            thresholds.PUSH_DELIVERY_LATENCY_ACTIVATION_CST,
        )
        self.assertEqual(
            thresholds.SLA_V3_REGISTRATION_ACTIVATION_CST,
            thresholds.STRUCTURED_POSITION_REVIEW_ACTIVATION_CST,
        )

    def test_v3_registration_facts_do_not_preregister_next_tier(self):
        facts = thresholds.sla_v3_registration_facts(V3_AT)
        self.assertTrue(facts["activated"])
        self.assertEqual(
            "successful_live_report_reconcile_barrier_finished_at",
            facts["clock_stop"],
        )
        self.assertEqual(0.80, facts["pass_rate_tier"]["target_rate"])
        self.assertFalse(facts["pass_rate_tier"]["next_tier_registered"])
        push = thresholds.push_latency_registration_facts(V3_AT)
        self.assertEqual("<=", push["comparison"])
        self.assertFalse(push["historical_rejudgement"])

    def test_sla_tier2_registration_is_forward_only(self):
        self.assertEqual(
            SLA_TIER2_DECISION,
            thresholds.SLA_PASS_RATE_TIER_2_DECISION_CST,
        )
        self.assertEqual(
            SLA_TIER2_ACTIVATION,
            thresholds.SLA_PASS_RATE_TIER_2_ACTIVATION_CST,
        )
        self.assertGreater(
            thresholds.parse_cst(SLA_TIER2_ACTIVATION),
            thresholds.parse_cst(SLA_TIER2_DECISION),
        )
        self.assertEqual(0.80, thresholds.sla_pass_rate_target(
            SLA_TIER2_BEFORE_ACTIVATION))
        self.assertEqual(1, thresholds.sla_pass_rate_tier_index(
            SLA_TIER2_BEFORE_ACTIVATION))
        self.assertEqual(96, thresholds.sla_pass_rate_minimum_slots(
            SLA_TIER2_BEFORE_ACTIVATION))
        self.assertEqual(
            thresholds.SLA_V4_PASS_RATE_TIER_ACTIVATION_CST,
            thresholds.sla_pass_rate_window_activation_cst(
                SLA_TIER2_BEFORE_ACTIVATION),
        )

        self.assertIsNone(thresholds.sla_pass_rate_next_tier_registration(
            SLA_TIER2_BEFORE_DECISION))
        pending = thresholds.sla_pass_rate_next_tier_registration(
            SLA_TIER2_DECISION)
        self.assertIsNotNone(pending)
        self.assertEqual(2, pending["tier"])
        self.assertEqual(0.90, pending["target_rate"])
        self.assertEqual(192, pending["minimum_slots"])
        before_facts = thresholds.sla_v3_registration_facts(
            SLA_TIER2_DECISION)
        self.assertTrue(
            before_facts["pass_rate_tier"]["next_tier_registered"])
        self.assertEqual(
            "REGISTERED_PENDING_ACTIVATION",
            before_facts["pass_rate_tier"]["next_tier"]["status"],
        )

        self.assertEqual(0.90, thresholds.sla_pass_rate_target(
            SLA_TIER2_ACTIVATION))
        self.assertEqual(2, thresholds.sla_pass_rate_tier_index(
            SLA_TIER2_ACTIVATION))
        self.assertEqual(192, thresholds.sla_pass_rate_minimum_slots(
            SLA_TIER2_ACTIVATION))
        self.assertEqual(
            SLA_TIER2_ACTIVATION,
            thresholds.sla_pass_rate_window_activation_cst(
                SLA_TIER2_ACTIVATION),
        )
        active_facts = thresholds.sla_v3_registration_facts(
            SLA_TIER2_ACTIVATION)
        active_tier = active_facts["pass_rate_tier"]
        self.assertEqual(2, active_tier["tier"])
        self.assertEqual(0.90, active_tier["target_rate"])
        self.assertEqual(192, active_tier["minimum_slots"])
        self.assertFalse(active_tier["next_tier_registered"])
        self.assertIsNone(active_tier["next_tier"])
        self.assertEqual(1, len(active_tier["prior_tier_registrations"]))
        self.assertEqual(
            thresholds.SLA_PASS_RATE_TIER_2_ACTIVATION_CST,
            active_tier["prior_tier_registrations"][0][
                "end_exclusive_cst"],
        )
        self.assertFalse(active_tier["historical_rejudgement"])
        self.assertEqual(
            870, thresholds.sla_business_terminal_deadline_seconds(
                SLA_TIER2_ACTIVATION))

    def test_tier2_exact_0300_user_exception_is_one_off_and_auditable(self):
        self.assertEqual([], thresholds.sla_pass_rate_acceptance_exceptions(
            SLA_TIER2_EXCEPTION_BEFORE))
        exceptions = thresholds.sla_pass_rate_acceptance_exceptions(
            SLA_TIER2_EXCEPTION_DECISION)
        self.assertEqual(1, len(exceptions))
        exception = exceptions[0]
        self.assertEqual("2026-08-27T03:00", exception["cycle_id"])
        self.assertEqual(2, exception["tier"])
        self.assertTrue(exception["user_approved"])
        self.assertTrue(exception["post_observation"])
        self.assertTrue(exception["raw_cycle_fact_preserved"])
        self.assertFalse(exception["recurring"])
        self.assertEqual(
            exception,
            thresholds.sla_pass_rate_acceptance_exception_for_cycle(
                "2026-08-27T03:00", SLA_TIER2_EXCEPTION_DECISION),
        )
        self.assertIsNone(
            thresholds.sla_pass_rate_acceptance_exception_for_cycle(
                "2026-08-28T03:00", SLA_TIER2_EXCEPTION_DECISION))

        registration = thresholds.sla_v3_registration_facts(
            SLA_TIER2_EXCEPTION_DECISION)
        facts = registration["pass_rate_tier"]
        self.assertEqual(exceptions, facts["acceptance_exceptions"])
        self.assertTrue(facts["historical_rejudgement"])
        self.assertEqual(
            ["2026-08-27T03:00"], facts["historical_rejudgement_scope"])
        self.assertTrue(facts["raw_cycle_facts_preserved"])
        self.assertIn("raw cycle facts stay preserved", registration["semantics"])

    def test_v4_two_gate_scope_stops_before_persistence(self):
        self.assertFalse(
            thresholds.complete_cycle_uses_business_terminal_stop(V4_BEFORE))
        self.assertTrue(
            thresholds.complete_cycle_uses_business_terminal_stop(V4_AT))
        self.assertFalse(
            thresholds.complete_cycle_uses_record_reconcile_stop(V4_AT))
        self.assertEqual(
            870, thresholds.sla_analysis_deadline_seconds(V4_AT))
        self.assertEqual(
            870, thresholds.sla_business_terminal_deadline_seconds(V4_AT))
        self.assertEqual(
            900, thresholds.sla_record_reconcile_deadline_seconds(V4_AT))
        self.assertEqual(
            930, thresholds.push_same_slot_max_age_seconds(V4_AT))
        self.assertEqual(
            960, thresholds.post_push_monitor_deadline_seconds(V4_AT))
        facts = thresholds.sla_v3_registration_facts(V4_AT)
        self.assertEqual(
            "successful_analysis_judgment_trade_terminal_at",
            facts["clock_stop"],
        )
        self.assertEqual([
            "required_collection_sources_completed",
            "analysis_judgment_trade_completed",
        ], facts["stage_gates"])
        self.assertIn("business_database_commit", facts["excluded_from_870_seconds"])
        self.assertIn("push_delivery", facts["excluded_from_870_seconds"])
        self.assertEqual(
            thresholds.SLA_V4_PROCESS_SCOPE_ACTIVATION_CST,
            thresholds.sla_pass_rate_window_activation_cst(V4_AT),
        )
        self.assertEqual(
            thresholds.SLA_PASS_RATE_TIER_ACTIVATION_CST,
            thresholds.sla_pass_rate_window_activation_cst(V4_BEFORE),
        )

    def test_critical_output_zero_streak_registration_is_forward_only(self):
        self.assertIsNone(
            thresholds.critical_output_zero_streak_threshold_slots(
                LIVENESS_BEFORE))
        self.assertEqual(
            2,
            thresholds.critical_output_zero_streak_threshold_slots(
                LIVENESS_AT),
        )
        before = thresholds.critical_output_zero_streak_registration_facts(
            LIVENESS_BEFORE)
        active = thresholds.critical_output_zero_streak_registration_facts(
            LIVENESS_AT)
        self.assertEqual("REGISTERED_FORWARD_ONLY", before["status"])
        self.assertEqual(2, before["alert_threshold_slots"])
        self.assertIsNone(before["effective_alert_threshold_slots"])
        self.assertFalse(before["activated"])
        self.assertEqual(2, active["effective_alert_threshold_slots"])
        self.assertTrue(active["activated"])
        self.assertFalse(active["historical_rejudgement"])
        self.assertFalse(active["external_alert_wiring"])
        self.assertFalse(active["scheduler_authority"])
        self.assertFalse(active["trading_authority"])

    def test_candidate_bundle_has_separate_forward_shadow_and_consume_edges(self):
        registered = thresholds.candidate_bundle_registration_facts(
            "2026-08-29T09:45")
        self.assertEqual("REGISTERED_FORWARD_ONLY", registered["status"])
        self.assertEqual("off", registered["phase"])
        self.assertEqual(
            "2026-08-29T10:00:00+08:00",
            registered["shadow_activation_cst"],
        )
        self.assertIsNone(registered["consume_activation_cst"])
        self.assertEqual(24, registered["shadow_minimum_natural_slots"])
        self.assertEqual(96, registered["consume_minimum_natural_slots"])
        self.assertFalse(registered["scheduler_changed"])
        self.assertFalse(registered["model_or_provider_changed"])
        self.assertEqual(
            "shadow", thresholds.candidate_bundle_phase(
                "2026-08-29T10:00"))
        with (
            mock.patch.object(
                thresholds, "CANDIDATE_BUNDLE_SHADOW_ACTIVATION_CST",
                "2026-08-29T10:00:00+08:00"),
            mock.patch.object(
                thresholds, "CANDIDATE_BUNDLE_CONSUME_ACTIVATION_CST",
                "2026-08-29T16:00:00+08:00"),
            mock.patch.object(
                thresholds, "CANDIDATE_BUNDLE_CONSUME_END_CST",
                "2026-08-30T16:00:00+08:00"),
        ):
            self.assertEqual(
                "off", thresholds.candidate_bundle_phase(
                    "2026-08-29T09:45"))
            self.assertEqual(
                "shadow", thresholds.candidate_bundle_phase(
                    "2026-08-29T10:00"))
            self.assertEqual(
                "consume", thresholds.candidate_bundle_phase(
                    "2026-08-29T16:00"))
            self.assertEqual(
                "rollback", thresholds.candidate_bundle_phase(
                    "2026-08-30T16:00"))

    def test_zero_open_funnel_repairs_are_forward_only(self):
        self.assertEqual(
            "2026-09-01T01:30:00+08:00",
            thresholds.CANDIDATE_OPPORTUNITY_STATE_ACTIVATION_CST)
        self.assertEqual(
            "2026-09-01T02:45:00+08:00",
            thresholds.CANDIDATE_OPPORTUNITY_STATE_V2_ACTIVATION_CST)
        self.assertFalse(thresholds.candidate_exact_identity_enforced(
            "2026-09-01T03:45"))
        self.assertTrue(thresholds.candidate_exact_identity_enforced(
            "2026-09-01T04:00"))
        self.assertFalse(thresholds.side_regime_soft_veto_shadow_active(
            "2026-09-01T03:45"))
        self.assertTrue(thresholds.side_regime_soft_veto_shadow_active(
            "2026-09-01T04:00"))
        self.assertFalse(thresholds.zero_open_watchdog_active(
            "2026-09-01T03:45"))
        self.assertTrue(thresholds.zero_open_watchdog_active(
            "2026-09-01T04:00"))
        self.assertEqual(96, thresholds.ZERO_OPEN_WATCHDOG_THRESHOLD_SLOTS)

    def test_goal_refresh_validates_target_and_full_migration_facts(self):
        for moment in (BEFORE, AT):
            expected = thresholds.coverage_target_rate(moment)
            payload = {
                "target_rate": expected,
                "target_rate_migration": thresholds.coverage_migration_facts(
                    moment),
            }
            with self.subTest(moment=moment):
                self.assertEqual(
                    expected,
                    _validated_coverage_target(
                        payload,
                        as_of=moment,
                        target_field="target_rate",
                        migration_field="target_rate_migration",
                        label="fixture",
                    ),
                )

        lowered = {
            "target_rate": thresholds.coverage_target_rate(AT) - 0.01,
            "target_rate_migration": thresholds.coverage_migration_facts(AT),
        }
        with self.assertRaisesRegex(ValueError, "effective target"):
            _validated_coverage_target(
                lowered,
                as_of=AT,
                target_field="target_rate",
                migration_field="target_rate_migration",
                label="fixture",
            )

        tampered_facts = {
            "target_rate": thresholds.coverage_target_rate(AT),
            "target_rate_migration": thresholds.coverage_migration_facts(AT),
        }
        tampered_facts["target_rate_migration"]["activated"] = False
        with self.assertRaisesRegex(ValueError, "migration facts"):
            _validated_coverage_target(
                tampered_facts,
                as_of=AT,
                target_field="target_rate",
                migration_field="target_rate_migration",
                label="fixture",
            )


class MigratedAuditDefaultsTests(unittest.TestCase):
    """闸门默认值必须是「按边界解析」而不是任何硬编码数字。"""

    CASES = (
        (audit_source_health.audit_source_health, "target_rate"),
        (audit_news_source_health.audit_news_source_health, "target_rate"),
        (audit_market_field_coverage.audit_market_field_coverage,
         "target_rate"),
        (audit_market_feature_coverage.audit_market_feature_coverage,
         "target_rate"),
        (audit_positioning_coverage.audit_positioning_coverage,
         "minimum_rate"),
        (audit_positioning_coverage.audit_positioning_forward_coverage,
         "target_rate"),
        (audit_positioning_coverage.audit_positioning_decision_availability,
         "target_rate"),
        (audit_positioning_coverage.audit_positioning_collection_receipts,
         "target_rate"),
        (audit_contract_statistics_coverage.audit_contract_statistics,
         "forward_target_rate"),
    )

    def test_every_migrated_audit_resolves_its_default(self):
        for function, parameter in self.CASES:
            with self.subTest(function=function.__name__):
                default = inspect.signature(
                    function).parameters[parameter].default
                self.assertIsNone(default)

    def test_module_constants_carry_the_migrated_value(self):
        for module in (
            audit_push_completeness,
            audit_periodic_report_completeness,
            audit_report_completeness,
        ):
            with self.subTest(module=module.__name__):
                self.assertEqual(0.95, module.TARGET_RATE)
                self.assertEqual(0.99, module.LEGACY_TARGET_RATE)

    def test_contract_statistics_forward_cli_resolves_migrated_target(self):
        self.assertIsNone(_cli_default(
            audit_contract_statistics_coverage, "--forward-target-rate"))


class BlacklistUntouchedTests(unittest.TestCase):
    """黑名单三审计挂在影子标签/信号验收链，本批不迁移。"""

    def test_blacklisted_audits_keep_99_percent(self):
        cases = (
            (audit_multitimeframe_coverage.audit_multitimeframe_coverage,
             "minimum_rate"),
            (audit_asset_class_coverage.audit_asset_class_coverage,
             "minimum_rate"),
            (audit_contract_statistics_coverage.audit_contract_statistics,
             "minimum_coverage"),
        )
        for function, parameter in cases:
            with self.subTest(function=function.__name__, parameter=parameter):
                signature = inspect.signature(function)
                self.assertIn(parameter, signature.parameters)
                self.assertEqual(0.99, signature.parameters[parameter].default)

    def test_blacklisted_cli_defaults_keep_99_percent(self):
        for module, option in (
            (audit_multitimeframe_coverage, "--minimum-rate"),
            (audit_asset_class_coverage, "--minimum-rate"),
            (audit_contract_statistics_coverage, "--minimum-coverage"),
        ):
            with self.subTest(module=module.__name__, option=option):
                self.assertEqual(0.99, _cli_default(module, option))

    def test_consumer_side_99_percent_references_do_not_drift(self):
        cases = (
            (SCRIPTS / "decision_briefing.py",
             "POSITIONING_MINIMUM_COVERAGE"),
            (SCRIPTS / "collect_market_features.py",
             "CONTRACT_STATS_MINIMUM_COVERAGE"),
            (SCRIPTS / "collect_market_features.py",
             "MARKET_FEATURE_MINIMUM_COVERAGE"),
            (SCRIPTS / "collect_market_features.py",
             "POSITIONING_MINIMUM_COVERAGE"),
            (SCRIPTS / "recover_contract_statistics_current.py",
             "MINIMUM_DIRECT_RATE"),
        )
        for path, name in cases:
            with self.subTest(path=path.name, name=name):
                self.assertEqual(0.99, _literal_assignment(path, name))


class SourceHealthGateMigrationTests(unittest.TestCase):
    """端到端：同一批数据，边界前后判定不同，且证据自证用了哪个口径。"""

    def _ledger(self, root: Path, cycles: list[str], bad: int) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        path = root / "ledger.db"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE collection_runs("
            "cycle_id TEXT,source TEXT,status TEXT,ts TEXT,rows INTEGER,"
            "latency_ms INTEGER,err TEXT,PRIMARY KEY(cycle_id,source))"
        )
        rows = []
        for index, cycle in enumerate(cycles):
            status = "error" if index < bad else "ok"
            rows.append(
                (cycle, "fast", status, f"{cycle[:10]} 00:00:00", 1, 1, None))
        connection.executemany(
            "INSERT INTO collection_runs VALUES(?,?,?,?,?,?,?)", rows)
        connection.commit()
        connection.close()
        return path

    def _audit(self, ledger: Path, as_of: str, start: str) -> dict:
        return audit_source_health.audit_source_health(
            ledger_db=ledger,
            as_of=audit_source_health._parse_cst(as_of),
            forward_start=audit_source_health._parse_cst(start),
            rolling_days=1,
            forward_minimum_slots=100,
            grace_minutes=5,
        )

    def _window(self, start: str, slots: int = 100):
        begin = audit_source_health._parse_cst(start)
        end = begin + audit_source_health.timedelta(minutes=15 * slots)
        return begin, end, audit_source_health._expected_cycles(begin, end)

    def test_97_percent_fails_before_and_passes_after_the_boundary(self):
        # 同样是 97/100 的严格完整率：边界前按 0.99 判 NOT_MET，边界后按
        # 0.95 判 PASSED，且两次都自证用的是哪个口径。
        before_start = "2026-08-14T00:00:00+08:00"
        after_start = "2026-08-15T20:00:00+08:00"
        _, _, before_cycles = self._window(before_start)
        _, _, after_cycles = self._window(after_start)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before_ledger = self._ledger(root / "a", before_cycles, bad=3)
            after_ledger = self._ledger(root / "b", after_cycles, bad=3)
            before = self._audit(
                before_ledger, "2026-08-15T01:00:00+08:00", before_start)
            after = self._audit(
                after_ledger, "2026-08-16T21:00:00+08:00", after_start)

        self.assertEqual(0.99, before["target_rate"])
        self.assertEqual(0.97, before["forward_after_remediation"]["complete_rate"])
        self.assertEqual(
            "NOT_MET", before["forward_after_remediation"]["status"])
        self.assertFalse(before["target_rate_migration"]["activated"])

        self.assertEqual(0.95, after["target_rate"])
        self.assertEqual(0.97, after["forward_after_remediation"]["complete_rate"])
        self.assertEqual(
            "PASSED", after["forward_after_remediation"]["status"])
        self.assertTrue(after["target_rate_migration"]["activated"])
        # 老口径达成率仍外显：0.97 达不到 0.99，诊断列如实为 False。
        diagnostics = after["legacy_target_diagnostics"][
            "forward_after_remediation"]
        self.assertEqual(0.99, diagnostics["legacy_target_rate"])
        self.assertFalse(
            diagnostics["rates_at_least_legacy_target"]["complete_rate"])

    def test_explicit_target_rate_still_wins(self):
        start = "2026-08-15T20:00:00+08:00"
        _, _, cycles = self._window(start)
        with tempfile.TemporaryDirectory() as temp:
            ledger = self._ledger(Path(temp) / "c", cycles, bad=3)
            result = audit_source_health.audit_source_health(
                ledger_db=ledger,
                as_of=audit_source_health._parse_cst(
                    "2026-08-16T21:00:00+08:00"),
                forward_start=audit_source_health._parse_cst(start),
                rolling_days=1,
                target_rate=0.99,
                forward_minimum_slots=100,
                grace_minutes=5,
            )
        self.assertEqual(0.99, result["target_rate"])
        self.assertEqual(
            "NOT_MET", result["forward_after_remediation"]["status"])


class CalibrationTwinThresholdTests(unittest.TestCase):
    """点精度与 Wilson 95% 下界必须共用同一个数值，永远同步移动。"""

    def _rows(self, n: int, hits: int) -> list[dict]:
        return [
            {
                "cycle_id": f"2026-08-1{index % 5}T00:{index % 4 * 15:02d}",
                "side": "long" if index % 2 else "short",
                "horizon": "15m",
                "research_probability": 0.9,
                "after_cost_hit": index < hits,
                "signed_return": 0.01,
                "executable_directional_return": 0.009,
                "signed_return_after_cost": 0.007,
            }
            for index in range(n)
        ]

    def test_point_precision_follows_the_declared_target(self):
        rows = self._rows(100, 85)
        at_80 = auditor._metrics(
            rows, offline_gate=True, min_sample=100, min_days=5,
            min_cycles=100, target_precision=0.80)
        at_90 = auditor._metrics(
            rows, offline_gate=True, min_sample=100, min_days=5,
            min_cycles=100, target_precision=0.90)
        self.assertTrue(at_80["requirements"]["precision_at_least_target"])
        self.assertFalse(at_90["requirements"]["precision_at_least_target"])

    def test_no_legacy_90pct_requirement_key_survives(self):
        for module in (auditor, evaluator):
            source = Path(module.__file__).read_bytes().decode("utf-8")
            with self.subTest(module=module.__name__):
                self.assertNotIn('"precision_at_least_90pct"', source)
                self.assertNotIn('"wilson_95_low_at_least_90pct"', source)

    def test_active_trade_contract_does_not_restate_obsolete_90pct_gate(self):
        trader = (ROOT / "agents" / "live_trader.md").read_text(
            encoding="utf-8")
        decision_card = (ROOT / "core" / "decision_card.py").read_text(
            encoding="utf-8")
        self.assertNotIn("独立前瞻验证通过 90%", trader)
        self.assertNotIn("独立90%门", decision_card)
        self.assertIn("all_market_lightweight_open_v1", trader)
        self.assertIn("OPEN不再要求六/九项展示卡", trader)
        self.assertIn("当前独立前向门通过且主人", decision_card)

    def test_evaluator_and_audit_share_one_resolved_threshold(self):
        # 评估器自报的 target_precision 与审计的地板同源；边界前 0.90、
        # 边界后 0.80，只降一个就会在这里红。
        for moment, expected in ((BEFORE, 0.90), (AT, 0.80)):
            with self.subTest(moment=moment):
                self.assertEqual(
                    expected, thresholds.shadow_target_precision(moment))
        facts = thresholds.shadow_migration_facts(AT)
        self.assertEqual(0.90, facts["legacy_target_precision"])
        self.assertEqual(0.80, facts["effective_target_precision"])
        self.assertIn("REQUIRES_RISK_APPROVAL", facts["semantics"])


if __name__ == "__main__":
    unittest.main()
