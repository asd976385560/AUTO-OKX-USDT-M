# -*- coding: utf-8 -*-
"""关键步降级：exit_quality 失败不再打死整份日报（2026-08-20）。

**实证代价**：经验 289（LINK，开于 08-11、持有 7 天平于 08-18）的决策卡缺
`exit_mode` → `exit_quality` 判 blocked → rc=2 → 关键步被拒 → `ready=False` →
`report_mode=blocked` → **2026-08-19 日报整份不存在**。

而日报的核心事实（成交笔数/PnL/手续费/持仓）来自 `reconcile` + `account_bills`
+ `trade_report_stats`，与退出质量分析彼此独立。既有设计里本来就有优雅降级通道
（`reconcile` rc=1 → provisional），`exit_quality` 却是全有全无 —— 本用例钉死
它并进同一条通道后的语义。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import daily_maintenance as dm  # noqa: E402

AFTER = "2026-08-21"     # 降级边界当日及之后
BEFORE = "2026-08-19"    # 边界前（289 真实出事那天）


def _report(business_date: str, *, failed: tuple[str, ...] = (),
            reconcile_rc: int = 0) -> dict:
    steps = {}
    for name in dm.REVIEWER_CRITICAL_STEPS:
        bad = name in failed
        steps[name] = {
            "rc": (2 if bad else (reconcile_rc if name == "reconcile" else 0)),
            "accepted": not bad and not (name == "reconcile" and reconcile_rc == 2),
            "completed": True,
            "completed_at": f"{business_date} 08:00:00",
        }
    if reconcile_rc == 1:
        steps["reconcile"]["accepted"] = True
    return {
        "business_date": business_date,
        "run_id": "r1",
        "started_at": f"{business_date} 07:55:00",
        "critical_steps_completed_at": f"{business_date} 08:00:00",
        "completed_at": f"{business_date} 08:01:00",
        "ok": True,
        "steps": steps,
    }


class ManifestDegradationTests(unittest.TestCase):
    def test_all_green_is_final_candidate(self):
        m = dm.build_reviewer_manifest(_report(AFTER), "completed")
        self.assertTrue(m["ready"])
        self.assertEqual("final_candidate", m["report_mode"])
        self.assertEqual([], m["degraded_critical_steps"])
        self.assertFalse(m["provisional_required"])

    def test_exit_quality_failure_degrades_instead_of_blocking(self):
        """本补丁的全部意义：报告仍然存在，只是标为临时。"""
        m = dm.build_reviewer_manifest(
            _report(AFTER, failed=("exit_quality",)), "completed")
        self.assertTrue(m["ready"], "exit_quality 失败不得再清掉 ready")
        self.assertEqual("provisional", m["report_mode"])
        self.assertEqual(["exit_quality"], m["degraded_critical_steps"])
        self.assertIn("critical_step_degraded:exit_quality",
                      m["provisional_reasons"])

    def test_failure_stays_visible_not_swallowed(self):
        """降级不是掩盖：该步的 rc 与 accepted 照旧如实记录。"""
        m = dm.build_reviewer_manifest(
            _report(AFTER, failed=("exit_quality",)), "completed")
        self.assertEqual(2, m["steps"]["exit_quality"]["rc"])
        self.assertFalse(m["steps"]["exit_quality"]["accepted"])
        self.assertIn("exit_quality", m["critical_steps"])

    def test_non_degradable_critical_step_still_blocks(self):
        """reconcile/account_bills 等仍是「有没有报告」的开关，不受本补丁影响。"""
        for name in ("reconcile", "account_bills",
                     "missed_opportunities", "ledger_invariants",
                     "quality_metrics"):
            with self.subTest(step=name):
                m = dm.build_reviewer_manifest(
                    _report(AFTER, failed=(name,)), "completed")
                self.assertFalse(m["ready"])
                self.assertEqual("blocked", m["report_mode"])

    def test_reconcile_unresolved_path_is_unchanged(self):
        m = dm.build_reviewer_manifest(
            _report(AFTER, reconcile_rc=1), "completed")
        self.assertTrue(m["ready"])
        self.assertEqual("provisional", m["report_mode"])
        self.assertIn("live_reconcile_unresolved", m["provisional_reasons"])
        self.assertEqual([], m["degraded_critical_steps"])

    def test_both_triggers_are_distinguishable(self):
        """对账未清零与退出质量段不可用是两件事，复盘正文写法也不同。"""
        m = dm.build_reviewer_manifest(
            _report(AFTER, failed=("exit_quality",), reconcile_rc=1),
            "completed")
        self.assertEqual("provisional", m["report_mode"])
        self.assertEqual(
            ["live_reconcile_unresolved",
             "critical_step_degraded:exit_quality"],
            m["provisional_reasons"])

    def test_boundary_is_forward_only(self):
        """边界前保持原判定 —— 不反向重解释 2026-08-19 那份 manifest。"""
        m = dm.build_reviewer_manifest(
            _report(BEFORE, failed=("exit_quality",)), "completed")
        self.assertFalse(m["ready"])
        self.assertEqual("blocked", m["report_mode"])
        self.assertEqual([], m["degraded_critical_steps"])
        self.assertEqual("2026-08-21", m["provisional_degrade_from"])

    def test_incomplete_run_is_still_not_ready(self):
        """维护整轮没跑完时，无论降级与否都不是 ready。"""
        m = dm.build_reviewer_manifest(_report(AFTER), "running")
        self.assertFalse(m["ready"])
        self.assertEqual("blocked", m["report_mode"])

    def test_model_business_status_is_separate_from_process_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "model.json"
            artifact.write_text(json.dumps({
                "models": [{"overall": {"status": "NOT_MET"}}],
            }), encoding="utf-8")
            with mock.patch.dict(dm.BUSINESS_STATUS_ARTIFACTS, {
                "frozen_model_shadow_evaluation": (
                    artifact, "model_statuses"),
            }, clear=False):
                result = dm._business_status_result(
                    "frozen_model_shadow_evaluation")
        self.assertTrue(result["valid"])
        self.assertEqual("NOT_MET", result["status"])
        self.assertEqual(["NOT_MET"], result["model_statuses"])


if __name__ == "__main__":
    unittest.main()
