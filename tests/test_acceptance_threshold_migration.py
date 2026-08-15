# -*- coding: utf-8 -*-
"""口径迁移契约：预注册激活边界只向前生效，且孪生阈值必须同步移动。

covers V2.1 §1/§3（完善率与完整度 99%→95%）与 §2（前向校准门 90%→80%）。
黑名单三审计（多周期/资产分类/合约统计）与消费端引用必须原地不动。
"""
import inspect
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


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


BEFORE = "2026-08-15T19:59:59+08:00"
AT = "2026-08-15T20:00:00+08:00"
AFTER = "2026-08-16T00:00:00+08:00"


class ActivationBoundaryTests(unittest.TestCase):
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


class BlacklistUntouchedTests(unittest.TestCase):
    """黑名单三审计挂在影子标签/信号验收链，本批不迁移。"""

    def test_blacklisted_audits_keep_99_percent(self):
        cases = (
            (audit_multitimeframe_coverage, "audit_multitimeframe_coverage"),
            (audit_asset_class_coverage, "audit_asset_class_coverage"),
            (audit_contract_statistics_coverage,
             "audit_contract_statistics_coverage"),
        )
        for module, name in cases:
            function = getattr(module, name, None)
            if function is None:
                continue
            with self.subTest(module=name):
                signature = inspect.signature(function)
                for parameter in ("target_rate", "minimum_rate"):
                    if parameter in signature.parameters:
                        self.assertEqual(
                            0.99, signature.parameters[parameter].default)


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
