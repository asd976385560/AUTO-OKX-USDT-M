# -*- coding: utf-8 -*-
"""公开版告警推送分流契约（占位/合成目标）。"""
from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

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
    BROADCAST_TARGET = "group:PUBLIC_GROUP_OPENID"
    ALERT_TARGET = "c2c:PUBLIC_ALERT_OPENID"

    def test_targets_come_only_from_the_selected_environment_variable(self):
        env = {
            "OKX_QQ_TARGET": self.BROADCAST_TARGET,
            "OKX_QQ_ALERT_TARGET": self.ALERT_TARGET,
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                qq_push_raw._resolve_target(False),
                (self.BROADCAST_TARGET, "OKX_QQ_TARGET"),
            )
            self.assertEqual(
                qq_push_raw._resolve_target(True),
                (self.ALERT_TARGET, "OKX_QQ_ALERT_TARGET"),
            )

    def test_missing_or_crossed_route_target_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(qq_push_raw._resolve_target(False)[0], "")
            self.assertEqual(qq_push_raw._resolve_target(True)[0], "")
        with mock.patch.dict(
            os.environ,
            {
                "OKX_QQ_TARGET": self.ALERT_TARGET,
                "OKX_QQ_ALERT_TARGET": self.BROADCAST_TARGET,
            },
            clear=True,
        ):
            self.assertEqual(qq_push_raw._resolve_target(False)[0], "")
            self.assertEqual(qq_push_raw._resolve_target(True)[0], "")

    def test_cli_missing_target_returns_input_error(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            sys, "argv", ["qq_push_raw.py", "--message", "synthetic", "--dry-run"]
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(qq_push_raw.main(), 2)

    def test_alert_dry_run_uses_alert_env_without_printing_target(self):
        with mock.patch.dict(
            os.environ, {"OKX_QQ_ALERT_TARGET": self.ALERT_TARGET}, clear=True
        ), mock.patch.object(
            sys,
            "argv",
            ["qq_push_raw.py", "--message", "synthetic", "--alert", "--dry-run"],
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(qq_push_raw.main(), 0)
        self.assertIn("OKX_QQ_ALERT_TARGET", output.getvalue())
        self.assertNotIn(self.ALERT_TARGET, output.getvalue())

    def test_cli_target_override_is_rejected(self):
        with mock.patch.object(
            sys,
            "argv",
            ["qq_push_raw.py", "--message", "synthetic", "--target", self.ALERT_TARGET],
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                qq_push_raw.main()
        self.assertEqual(raised.exception.code, 2)

    def test_runtime_paths_have_no_host_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(qq_push_raw._resolve_runtime(), ("", "", "OKX_NODE_BIN"))
        with mock.patch.dict(os.environ, {"OKX_NODE_BIN": "node-bin"}, clear=True):
            self.assertEqual(qq_push_raw._resolve_runtime(), ("", "", "OKX_OPENCLAW_MJS"))
        with mock.patch.dict(
            os.environ,
            {"OKX_NODE_BIN": "node-bin", "OKX_OPENCLAW_MJS": "openclaw-entrypoint"},
            clear=True,
        ):
            self.assertEqual(
                qq_push_raw._resolve_runtime(),
                ("node-bin", "openclaw-entrypoint", ""),
            )

    def test_public_source_contains_no_literal_target_or_host_fallback(self):
        source = (SCRIPTS / "qq_push_raw.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?:group|c2c):[0-9A-Fa-f]{16,}")
        self.assertNotRegex(source, r"[A-Za-z]:\\")


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

    def test_wrapper_strips_private_args_but_keeps_alert_flag(self):
        """wrapper 私参不可喂 raw；--alert 必须保留，避免告警回落群聊。"""
        import qq_push

        orig = sys.argv[:]
        try:
            sys.argv = ["qq_push.py", "--alert", "--dedupe-key", "k",
                        "--db-root", "isolated-db",
                        "--content-file", "f"]
            qq_push._strip_wrapper_args()
            self.assertIn("--alert", sys.argv)
            self.assertNotIn("--dedupe-key", sys.argv)
            self.assertNotIn("--db-root", sys.argv)
        finally:
            sys.argv = orig


if __name__ == "__main__":
    unittest.main()
