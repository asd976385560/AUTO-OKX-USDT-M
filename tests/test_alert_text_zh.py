# -*- coding: utf-8 -*-
"""告警/战报异常段中文化文本契约（2026-08-21 批次）。

只验展示文本：kind/status 码保留原文加中文括注、异常原文保留英文、
dedupe 身份键不受译文影响。全程 mock subprocess/run_step，不真发 QQ。
"""


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COLLECTORS = ROOT / "collectors"
for _p in (SCRIPTS, COLLECTORS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fast_collect  # noqa: E402
import render_push_report  # noqa: E402
import stage_runner  # noqa: E402
import trigger_agent  # noqa: E402


def _send_stage_alert(failure_detail):
    proc = SimpleNamespace(returncode=0, stdout="", stderr="")
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
            mock.patch.object(stage_runner.subprocess, "run",
                              return_value=proc):
        stage_runner._send_failure_alert(
            "push", "2026-08-21T19:00", 86,
            Path(tmp) / "push-status.json", failure_detail)
        files = list(Path(tmp).glob("alert-*.txt"))
        assert len(files) == 1, files
        return files[0].read_text(encoding="utf-8")


class StageFailureAlertZhTests(unittest.TestCase):
    def test_known_kind_gets_summary_line_and_raw_json_kept(self):
        body = _send_stage_alert({
            "failure_kind": "cycle_deadline_exceeded",
            "stop_reason": "push absolute cycle deadline reached",
        })
        self.assertIn(
            "· 失败类别：cycle_deadline_exceeded（周期截止超时硬止）\n", body)
        self.assertIn("· 业务后置校验（原始JSON，程序字段保留英文）：", body)
        self.assertIn('"failure_kind":"cycle_deadline_exceeded"', body)
        self.assertIn(
            '"stop_reason":"push absolute cycle deadline reached"', body)

    def test_unknown_kind_degrades_to_code_only(self):
        body = _send_stage_alert({"failure_kind": "brand_new_kind"})
        self.assertIn("· 失败类别：brand_new_kind\n", body)
        self.assertNotIn("（）", body)

    def test_missing_kind_keeps_json_line_without_summary(self):
        body = _send_stage_alert({"note": "no kind here"})
        self.assertNotIn("失败类别", body)
        self.assertIn("· 业务后置校验（原始JSON，程序字段保留英文）：", body)
        self.assertIn('"note":"no kind here"', body)


class StageFailureCauseLineTests(unittest.TestCase):
    """2026-08-22 主人反馈「不知道什么错误」后加的直接原因行。"""

    def test_handoff_violation_surfaces_marker_and_runner_error(self):
        with tempfile.TemporaryDirectory() as aux:
            runner_state = Path(aux) / "live_runner_state_x.json"
            runner_state.write_text(
                '{"state": "failed", '
                '"error": "PlanError: receipt_context.equity 必须是有效数字"}',
                encoding="utf-8")
            body = _send_stage_alert({
                "failure_kind": "post_facts_runner_handoff_violation",
                "observed_stop": {
                    "stop_reason":
                        "post_facts_runner_handoff_violation:"
                        "no_valid_runner_marker",
                    "marker_error": "mismatch:plan_sha256",
                    "analysis_state": {"exists": True, "timely": True},
                    "runner_state": str(runner_state),
                },
            })
        self.assertIn(
            "· 直接原因：post_facts_runner_handoff_violation:"
            "no_valid_runner_marker（runner 未留下有效执行凭证）", body)
        self.assertIn(
            "· 凭证校验：mismatch:plan_sha256"
            "（计划文件与交接凭证哈希不符，凭证签发后计划被改写）", body)
        self.assertIn(
            "· runner 落败原因（原文）：PlanError: "
            "receipt_context.equity 必须是有效数字", body)
        self.assertNotIn("分析产物", body)

    def test_analysis_deadline_surfaces_missing_analysis(self):
        body = _send_stage_alert({
            "failure_kind": "analysis_deadline_exceeded",
            "observed_stop": {
                "stop_reason":
                    "analysis_deadline_exceeded:no_timely_analysis",
                "analysis_state": {"exists": False},
            },
        })
        self.assertIn(
            "· 直接原因：analysis_deadline_exceeded:no_timely_analysis"
            "（分析未按时产出）", body)
        self.assertIn("· 分析产物：本槽分析从未产出", body)

    def test_push_post_reconcile_reason_glossed(self):
        body = _send_stage_alert({
            "failure_kind": "post_push_reconcile_failed",
            "post_reconcile_reason": "live_stage_not_succeeded",
        })
        self.assertIn(
            "· 直接原因：live_stage_not_succeeded"
            "（本槽 live 业务未成功；消息送达结果单独核验）", body)

    def test_missing_runner_state_file_never_breaks_alert(self):
        body = _send_stage_alert({
            "failure_kind": "post_facts_runner_handoff_violation",
            "observed_stop": {
                "stop_reason": "post_facts_runner_handoff_violation:weird_new",
                "runner_state": _public_project_path('tmp', 'definitely_missing_state.json'),
            },
        })
        self.assertIn(
            "· 直接原因：post_facts_runner_handoff_violation:weird_new\n",
            body)
        self.assertNotIn("runner 落败原因", body)
        self.assertIn("· 业务后置校验（原始JSON，程序字段保留英文）：", body)


class AutohealAlertZhTests(unittest.TestCase):
    def _capture_alert(self, findings):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(trigger_agent.subprocess, "run",
                               side_effect=fake_run):
            ok = trigger_agent._send_autoheal_p0_alert(
                "live", "2026-08-21T19:00", findings)
        self.assertTrue(ok)
        argv = captured["argv"]
        message = argv[argv.index("--message") + 1]
        dedupe = argv[argv.index("--dedupe-key") + 1]
        return message, dedupe

    def test_kinds_glossed_and_dedupe_identity_untouched(self):
        message, dedupe = self._capture_alert([
            {"kind": "GHOST-EXACT",
             "symbol": "BTC-USDT-SWAP", "side": "long"},
            {"kind": "NAKED-POSITION-P0",
             "symbol": "ETH-USDT-SWAP", "side": "short"},
        ])
        self.assertIn("GHOST-EXACT(幽灵仓·fills精确可补)", message)
        self.assertIn("NAKED-POSITION-P0(现仓缺有效保护止损)", message)
        self.assertIn("BTC-USDT-SWAP/long", message)
        self.assertTrue(
            dedupe.startswith("autoheal-p0:live:2026-08-21T19:00:"), dedupe)

    def test_unregistered_kind_passes_through_bare(self):
        message, _ = self._capture_alert([
            {"kind": "WEIRD-NEW", "symbol": "BTC-USDT-SWAP", "side": "long"},
        ])
        self.assertIn("WEIRD-NEW", message)
        self.assertNotIn("WEIRD-NEW(", message)


class FastCollectAlertZhTests(unittest.TestCase):
    def test_content_file_uses_chinese_labels_and_keeps_raw_detail(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(fast_collect, "ROOT", Path(tmp)), \
                mock.patch.object(fast_collect, "run_step",
                                  return_value={"ok": True}):
            fast_collect._send_failure_alert(
                "2026-08-21T19:00",
                "RuntimeError: official ticker fetch failed", 12345)
            body = (Path(tmp) / "tmp"
                    / "fast_collect_failure_2026-08-21T19-00.txt"
                    ).read_text(encoding="utf-8")
        self.assertIn("耗时=12.3s", body)
        self.assertIn(
            "失败明细（异常/步骤stderr原文，保留英文）="
            "RuntimeError: official ticker fetch failed", body)
        self.assertNotIn("latency=", body)
        self.assertNotIn("detail=", body)

    def test_missing_detail_renders_chinese_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(fast_collect, "ROOT", Path(tmp)), \
                mock.patch.object(fast_collect, "run_step",
                                  return_value={"ok": True}):
            fast_collect._send_failure_alert("2026-08-21T19:15", None, 500)
            body = (Path(tmp) / "tmp"
                    / "fast_collect_failure_2026-08-21T19-15.txt"
                    ).read_text(encoding="utf-8")
        self.assertIn("=未知", body)
        self.assertNotIn("unknown", body)


class RenderExceptionStatusZhTests(unittest.TestCase):
    def test_filter_first_then_display_mapping(self):
        # 顺序契约：_is_runtime_fault 在 payload 层按英文关键词分流，
        # 中文映射只发生在 format_exceptions 展示层。
        items = [
            {"name": "fast", "status": "failed", "detail": "RuntimeError: x"},
            {"name": "slow", "status": "degraded", "detail": "-"},
            {"name": "regime", "status": "stale(age=120s)", "detail": "-"},
            {"name": "monitor", "level": "P0", "detail": "timeout hit"},
        ]
        faults, decisions = render_push_report.filter_exceptions(items)
        self.assertEqual(len(faults), 4, (faults, decisions))
        self.assertEqual(decisions, [])
        rendered = render_push_report.format_exceptions(faults)
        self.assertIn("fast [失败] RuntimeError: x", rendered)
        self.assertIn("slow [降级] -", rendered)
        self.assertIn("regime [过期(age=120s)] -", rendered)
        self.assertIn("monitor [P0] timeout hit", rendered)
        self.assertNotIn("[failed]", rendered)
        self.assertNotIn("[degraded]", rendered)

    def test_empty_exceptions_still_render_none_marker(self):
        self.assertEqual(render_push_report.format_exceptions([]), "无")


if __name__ == "__main__":
    unittest.main()
