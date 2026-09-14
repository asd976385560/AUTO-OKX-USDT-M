# -*- coding: utf-8 -*-
"""告警推送分流契约（2026-08-04）。

主人拍板：**告警走 C2C 私聊，业务播报留群聊**，两条通道不得混。
本文件钉住路由，防止日后有人把告警改回群、或把播报误发进告警私聊。
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import qq_push_raw  # noqa: E402

# 告警调用方 → 必须带 --alert；播报调用方 → 必须不带
ALERT_CALLERS = (
    "stage_runner.py",
    "collection_monitor.py",
    "live_reconcile_monitor.py",
    "reconcile_daily.py",
)
BROADCAST_CALLERS = ("push_pipeline.py",)


class AlertTargetContractTests(unittest.TestCase):
    def test_unconfigured_public_routes_are_empty(self):
        for name in ("OKX_QQ_TARGET", "OKX_QQ_ALERT_TARGET", "OKX_QQ_REPORT_TARGET"):
            with patch.dict(qq_push_raw.os.environ, {name: ""}):
                self.assertNotIn('PUBLIC_', qq_push_raw.os.environ[name])

    def test_unconfigured_delivery_fails_before_transport(self):
        with patch.object(qq_push_raw.subprocess, "run") as run:
            ok, error = qq_push_raw.push("fixture", "")
        self.assertFalse(ok)
        self.assertIn("not configured", error)
        run.assert_not_called()


class AlertCallerWiringTests(unittest.TestCase):
    def _source(self, name: str) -> str:
        return (SCRIPTS / name).read_text(encoding="utf-8")

    def test_alert_callers_pass_alert_flag(self):
        for name in ALERT_CALLERS:
            src = self._source(name)
            self.assertIn("qq_push.py", src, f"{name} 不再调用 qq_push？")
            self.assertIn('"--alert"', src,
                          f"{name} 的告警推送缺 --alert，会误发进业务播报群")

    def test_broadcast_callers_do_not_use_alert_flag(self):
        for name in BROADCAST_CALLERS:
            src = self._source(name)
            self.assertNotIn('"--alert"', src,
                             f"{name} 是业务播报，不得发进告警私聊")

    def test_fast_collect_failure_uses_explicit_c2c_alert(self):
        src = (ROOT / "collectors" / "fast_collect.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("qq_push.py", src)
        self.assertIn('"--alert"', src)
        self.assertIn("fast-collect:{cycle}", src)


class AlertDedupeIsolationTests(unittest.TestCase):
    def test_dedupe_target_differs_between_alert_and_broadcast(self):
        """target 参与 dedupe basis：同内容发群与发告警私聊必须互不去重，
        否则切换路由后首条告警会被历史键吞掉（静默失败）。"""
        import qq_push

        orig = sys.argv[:]
        try:
            sys.argv = ["qq_push.py", "--content-file", "x"]
            _, _, _, plain = qq_push._dedupe_key("same content")
            sys.argv = ["qq_push.py", "--content-file", "x", "--alert"]
            _, _, _, alert = qq_push._dedupe_key("same content")
        finally:
            sys.argv = orig

        self.assertEqual(plain, "default")
        self.assertEqual(alert, "alert")
        self.assertNotEqual(plain, alert)

    def test_wrapper_does_not_strip_alert_flag(self):
        """_strip_wrapper_args 只准剥 --dedupe-key；剥掉 --alert 会让告警回落群聊。"""
        import qq_push

        orig = sys.argv[:]
        try:
            sys.argv = ["qq_push.py", "--alert", "--dedupe-key", "k",
                        "--content-file", "f"]
            qq_push._strip_wrapper_args()
            self.assertIn("--alert", sys.argv)
            self.assertNotIn("--dedupe-key", sys.argv)
        finally:
            sys.argv = orig


class ReportTargetContractTests(unittest.TestCase):
    def setUp(self):
        for name, value in {"DEFAULT_TARGET": ":".join(('group', 'TEST_GROUP')), "ALERT_TARGET": ":".join(('c2c', 'TEST_ALERT')), "REPORT_TARGET": ":".join(('c2c', 'TEST_REPORT')), "_NODE": "test-node", "_MJS": "test-openclaw"}.items():
            guard = patch.object(qq_push_raw, name, value)
            guard.start()
            self.addCleanup(guard.stop)

    """日/周/月报告改 C2C 私聊（2026-08-26 主人拍板）；15m 战报仍走群聊。

    路由在 qq_push wrapper 按 reviewer dedupe-key 确定性判定并注入 --report，
    调用方（reviewer agent）与业务播报完全不变。
    """

    def test_report_target_is_c2c_and_distinct_from_group(self):
        self.assertTrue(qq_push_raw.REPORT_TARGET.startswith("c2c:"))
        self.assertNotEqual(qq_push_raw.REPORT_TARGET, qq_push_raw.DEFAULT_TARGET)
        self.assertRegex(qq_push_raw.REPORT_TARGET, r"^c2c:[A-Za-z0-9_-]+$")

    def test_reviewer_dedupe_keys_route_to_report_target(self):
        import qq_push

        orig = sys.argv[:]
        try:
            for kind in ("daily", "weekly", "monthly"):
                sys.argv = ["qq_push.py", "--content-file", "x",
                            "--dedupe-key", f"reviewer:2026-08-27:{kind}"]
                _, _, _, target = qq_push._dedupe_key("c")
                self.assertEqual(target, "report", kind)
        finally:
            sys.argv = orig

    def test_cycle_broadcast_keys_stay_default_group(self):
        import qq_push

        orig = sys.argv[:]
        try:
            sys.argv = ["qq_push.py", "--content-file", "x",
                        "--dedupe-key", "push:2026-08-26T14:00"]
            _, _, _, target = qq_push._dedupe_key("c")
        finally:
            sys.argv = orig
        self.assertEqual(target, "default")

    def test_explicit_target_overrides_report_routing(self):
        import qq_push

        orig = sys.argv[:]
        try:
            sys.argv = ["qq_push.py", "--content-file", "x",
                        "--dedupe-key", "reviewer:2026-08-27:daily",
                        "--target", "group:XYZ"]
            _, _, _, target = qq_push._dedupe_key("c")
        finally:
            sys.argv = orig
        self.assertEqual(target, "group:XYZ")

    def test_raw_report_flag_resolves_to_report_target(self):
        import contextlib
        import io

        orig = sys.argv[:]
        buf = io.StringIO()
        try:
            sys.argv = ["qq_push_raw.py", "--report", "--dry-run",
                        "--message", "t"]
            with contextlib.redirect_stdout(buf):
                rc = qq_push_raw.main()
        finally:
            sys.argv = orig
        self.assertEqual(rc, 0)
        self.assertIn(qq_push_raw.REPORT_TARGET, buf.getvalue())

    def test_wrapper_does_not_strip_report_flag(self):
        import qq_push

        orig = sys.argv[:]
        try:
            sys.argv = ["qq_push.py", "--report", "--dedupe-key", "k",
                        "--content-file", "f"]
            qq_push._strip_wrapper_args()
            self.assertIn("--report", sys.argv)
            self.assertNotIn("--dedupe-key", sys.argv)
        finally:
            sys.argv = orig

    def test_wrapper_injects_report_flag_before_raw(self):
        """wrapper 判定 target=report 后必须把 --report 传给 raw；
        只改身份不改路由会让报告静默落回群聊。"""
        src = (SCRIPTS / "qq_push.py").read_text(encoding="utf-8")
        self.assertIn('sys.argv.append("--report")', src)


class PushTransportRetryTests(unittest.TestCase):
    def setUp(self):
        for name, value in {"DEFAULT_TARGET": ":".join(('group', 'TEST_GROUP')), "ALERT_TARGET": ":".join(('c2c', 'TEST_ALERT')), "REPORT_TARGET": ":".join(('c2c', 'TEST_REPORT')), "_NODE": "test-node", "_MJS": "test-openclaw"}.items():
            guard = patch.object(qq_push_raw, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        legacy = patch.dict(qq_push_raw.os.environ, {"OKX_QQ_TRANSPORT": "cli"})
        legacy.start()
        self.addCleanup(legacy.stop)

    _PRE_SUBMIT_TLS = (
        "OutboundDeliveryError: Network error getting access_token: fetch failed | "
        "Client network socket disconnected before secure TLS connection was established"
    )

    @staticmethod
    def _proc(returncode: int, stdout: str = "", stderr: str = ""):
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def test_exact_pre_submit_token_tls_failure_retries_once_and_recovers(self):
        failed = self._proc(1, stderr=self._PRE_SUBMIT_TLS)
        sent = self._proc(0, stdout='{"messageId":"receipt-1"}')
        with patch.object(qq_push_raw.subprocess, "run", side_effect=[failed, sent]) as run_mock, \
                patch.object(qq_push_raw.time, "sleep") as sleep_mock:
            ok, output = qq_push_raw.push("payload", timeout=60)

        self.assertTrue(ok)
        self.assertEqual(run_mock.call_count, 2)
        self.assertLessEqual(
            run_mock.call_args_list[0].kwargs["timeout"],
            qq_push_raw._MAX_DELIVERY_BUDGET_SECONDS,
        )
        self.assertEqual(run_mock.call_args_list[1].kwargs["timeout"], 25.0)
        sleep_mock.assert_called_once_with(0.5)
        self.assertIn("bounded retry", output)
        self.assertIn('"messageId"', output)

    def test_exact_pre_submit_token_tls_failure_has_only_one_retry(self):
        failed = self._proc(1, stderr=self._PRE_SUBMIT_TLS)
        with patch.object(
            qq_push_raw.subprocess,
            "run",
            side_effect=[failed, failed],
        ) as run_mock, patch.object(qq_push_raw.time, "sleep"):
            ok, output = qq_push_raw.push("payload")

        self.assertFalse(ok)
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(output.count("bounded retry"), 1)

    def test_unknown_failure_is_not_retried(self):
        failed = self._proc(1, stderr="remote closed connection after request")
        with patch.object(qq_push_raw.subprocess, "run", return_value=failed) as run_mock, \
                patch.object(qq_push_raw.time, "sleep") as sleep_mock:
            ok, _ = qq_push_raw.push("payload")

        self.assertFalse(ok)
        run_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_receipt_marker_makes_tls_failure_non_retryable(self):
        ambiguous = self._proc(
            1,
            stdout='{"messageId":"receipt-ambiguous"}',
            stderr=self._PRE_SUBMIT_TLS,
        )
        with patch.object(qq_push_raw.subprocess, "run", return_value=ambiguous) as run_mock, \
                patch.object(qq_push_raw.time, "sleep") as sleep_mock:
            ok, _ = qq_push_raw.push("payload")

        self.assertFalse(ok)
        run_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_timeout_is_not_retried(self):
        with patch.object(
            qq_push_raw.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["node"], timeout=60),
        ) as run_mock, patch.object(qq_push_raw.time, "sleep") as sleep_mock:
            ok, output = qq_push_raw.push("payload", timeout=60)

        self.assertFalse(ok)
        run_mock.assert_called_once()
        sleep_mock.assert_not_called()
        self.assertIn("timeout after 55", output)
        self.assertIn(qq_push_raw.UNCERTAIN_DELIVERY_MARKER, output)

    def test_timeout_main_returns_uncertain_delivery_exit_code(self):
        argv = ["qq_push_raw.py", "--message", "payload"]
        with patch.object(sys, "argv", argv), patch.object(
            qq_push_raw,
            "push",
            return_value=(False, qq_push_raw.UNCERTAIN_DELIVERY_MARKER),
        ), redirect_stdout(io.StringIO()) as stdout:
            rc = qq_push_raw.main()
        self.assertEqual(qq_push_raw.UNCERTAIN_DELIVERY_EXIT_CODE, rc)
        self.assertIn("PUSH UNCERTAIN", stdout.getvalue())

    def test_fast_collect_alert_uses_short_inner_and_outer_budgets(self):
        source = (ROOT / "collectors" / "fast_collect.py").read_text(
            encoding="utf-8"
        )
        start = source.index("def _send_failure_alert")
        end = source.index("\ndef main()", start)
        helper = source[start:end]
        self.assertIn('"--timeout",\n            "25"', helper)
        self.assertIn("timeout=30", helper)


if __name__ == "__main__":
    unittest.main()
