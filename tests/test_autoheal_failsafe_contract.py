# -*- coding: utf-8 -*-
"""自愈 fail-safe 契约（2026-08-05 事故后拍板）。

公开版两条契约：
  1. 自动补账永久只读，继承任何写入环境变量也不得追加写开关；
  2. 派发层永不因诊断未清干净而阻断 stage —— 真正的防线是 order_executor 的
     pretrade 闸（只挡这一单），而 push 是纯汇报、任何情况下都不该被关掉
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for sub in ("collectors", "scripts", "core"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import trigger_agent  # noqa: E402


class PublicReadOnlyAutohealTests(unittest.TestCase):
    def _captured_argv(self, env: dict) -> list[str]:
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch("subprocess.run") as run, \
             mock.patch.object(trigger_agent, "_read_autoheal_contract",
                               return_value={"blocking": False, "status": "ok"}):
            run.return_value = mock.Mock(returncode=0, stdout="{}", stderr="")
            trigger_agent._autoheal_ledger("live", "2026-08-05T19:30")
            self.assertTrue(run.called, "未调用 ledger_autoheal")
            return list(run.call_args[0][0])

    def test_default_is_read_only(self):
        argv = self._captured_argv({})
        self.assertNotIn("--apply", argv)
        self.assertNotIn("--enable-unrecorded", argv)

    def test_write_environment_cannot_enable_apply(self):
        argv = self._captured_argv({
            "OKX_LEDGER_AUTOHEAL_APPLY": "1",
            "OKX_LEDGER_AUTOHEAL_UNRECORDED": "1",
        })
        self.assertNotIn("--apply", argv)
        self.assertNotIn("--enable-unrecorded", argv)


class DispatchNeverBlocksTests(unittest.TestCase):
    def setUp(self):
        """把 build_cmd 的落盘/读库挪进临时目录。

        契约钉的是"真实 build_cmd 在 blocking 下仍出命令"，所以整条代码路径必须真跑，
        不能拿 mock 把内部环节掏空。但 build_cmd 末尾必写 `LOG_DIR/msg-<key>.txt`，
        demo 分支的 `_trader_preload` 还会读 `_DB_ROOT` 下的库——不隔离则每跑一次测试
        就覆盖一份生产触发消息（2026-08-06 10:21 实测重写 msg-demo-20260805-1930.txt）。
        这里只挪路径，不改行为。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # _DB_ROOT 指向空目录：_ro_db 取不到库 → _trader_preload 各块走既有 fail-safe
        # 留缺块标记（open_* 因此为空，也就不会去拉 OKX 公共合约池——测试不打外网）。
        for name, value in (("LOG_DIR", self.tmp / "logs" / "trigger"),
                            ("_DB_ROOT", self.tmp / "db")):
            patcher = mock.patch.object(trigger_agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _assert_launch_cmd(self, cmd, why: str) -> None:
        """原断言（命令照出、不抛异常）+ 落盘留在沙箱内的守卫。

        守卫的意义：哪天 setUp 的 LOG_DIR patch 被摘掉，message-file 会指回
        <PROJECT_ROOT>\\logs\\trigger，这里立刻红，而不是又悄悄覆盖生产触发消息。
        """
        self.assertIsInstance(cmd, list)
        self.assertTrue(cmd, why)
        if "--message-file" in cmd:
            msg_file = Path(cmd[cmd.index("--message-file") + 1])
            self.assertTrue(
                msg_file.is_relative_to(self.tmp),
                f"触发消息写到了沙箱外（会覆盖生产日志）：{msg_file}")

    def test_blocking_autoheal_does_not_abort_stage(self):
        """核心回归：自愈报 blocking 时 build_cmd 仍须正常产出命令，不得抛异常。"""
        blocking = {
            "blocking": True, "status": "needs_human", "rc": 1, "p0": False,
            "findings": [{"kind": "GHOST-EXACT", "symbol": "CL-USDT-SWAP"}],
        }
        with mock.patch.object(trigger_agent, "_autoheal_ledger",
                               return_value=blocking), \
             mock.patch.object(trigger_agent, "_analyst_briefing",
                               return_value="brief"), \
             mock.patch.object(trigger_agent, "_briefing_for_traders",
                               return_value="brief"):
            cmd = trigger_agent.build_cmd("live", "2026-08-05T19:30", "full")

        self._assert_launch_cmd(cmd, "build_cmd 必须仍然产出起棒命令")

    def test_blocking_autoheal_does_not_abort_live_either(self):
        blocking = {"blocking": True, "status": "needs_human", "rc": 1,
                    "p0": True, "findings": [{"kind": "GHOST-FUZZY"}]}
        with mock.patch.object(trigger_agent, "_autoheal_ledger",
                               return_value=blocking), \
             mock.patch.object(trigger_agent, "_analyst_briefing",
                               return_value="brief"), \
             mock.patch.object(trigger_agent, "_briefing_for_traders",
                               return_value="brief"):
            cmd = trigger_agent.build_cmd("live", "2026-08-05T19:30", "unified")
        self._assert_launch_cmd(cmd, "live 同样不得因 blocking 停 stage")

    def test_pretrade_gate_still_blocks(self):
        """派发层放行的同时，下单路径的 fail-closed 必须保留——防线不能一起拆掉。"""
        src = (ROOT / "core" / "order_executor.py").read_text(encoding="utf-8")
        self.assertIn('reject_reason="pretrade_ledger_autoheal_blocked"', src)
        self.assertIn('if autoheal_result.get("blocking"):', src)


if __name__ == "__main__":
    unittest.main()
