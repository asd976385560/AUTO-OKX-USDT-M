# -*- coding: utf-8 -*-
"""P0-5 5b｜降级战报（有业务终态但 live 报告对账屏障未就绪）。

修复的是「08-05~08-18 缺 111 条战报」里最刺眼的一类：21 轮 analysis=ok、
trade_cycles 终态齐、live 闩锁已取，**push 闩锁为 0** —— dispatcher 在
`live_report_barrier_ready` 为假时既不派也不留痕，有话可说却一个字没发。

5b 的契约是**加法不是减法**：
  * 内容仍是完整业务事实（成交/持仓/风控/指纹照旧必填），降的是**裁决效力**；
  * 第 2 行强制横幅 + 执行段 `report_barrier=not_ready` 双留痕；
  * `--degraded-report` 只是**意图**，push_pipeline 发送前独立复核屏障：
    仍未就绪才真降级，已就绪则**升级**回完整业务报告走原终态硬闸。
    这条不对称是本路径不会沦为绕开业务终态凭证之后门的原因。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import push_pipeline  # noqa: E402
import render_push_report  # noqa: E402
import validate_push_format as vpf  # noqa: E402

CYCLE = "2026-08-19T20:15"


class DegradedBarrierStateTests(unittest.TestCase):
    """意图 → 事实的那一次独立复核。"""

    def test_missing_barrier_authorizes_degradation(self):
        with mock.patch.object(
                push_pipeline, "load_live_report_barrier", return_value=None):
            state = push_pipeline.degraded_barrier_state(CYCLE)
        self.assertTrue(state["degraded"])
        self.assertEqual("report_barrier_not_ready", state["reason"])

    def test_ready_barrier_revokes_degradation(self):
        """屏障在 dispatcher 派发到 push 真正开跑之间就绪 → 必须升级回正常报告。"""
        with mock.patch.object(
                push_pipeline, "load_live_report_barrier",
                return_value={"schema_version": 1, "required": True}):
            state = push_pipeline.degraded_barrier_state(CYCLE)
        self.assertFalse(state["degraded"])
        self.assertEqual("report_barrier_ready", state["reason"])

    def test_probe_exception_is_treated_as_not_ready(self):
        """探测本身炸了 = 证明不了就绪 → 按未就绪处理，不当作就绪放行。"""
        with mock.patch.object(
                push_pipeline, "load_live_report_barrier",
                side_effect=OSError("status dir unreadable")):
            state = push_pipeline.degraded_barrier_state(CYCLE)
        self.assertTrue(state["degraded"])
        self.assertEqual("barrier_probe_error", state["reason"])

    def test_degraded_and_upstream_failure_are_mutually_exclusive(self):
        """两者前提互斥（无业务周期行 vs 有终态但缺凭证），同传即拒。"""
        rep = push_pipeline.run(
            CYCLE, str(ROOT / "db"), no_send=True,
            upstream_failure_report=True, degraded_report=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(
            "upstream_failure_and_degraded_are_mutually_exclusive",
            rep["fatal"])
        self.assertEqual("invalid", rep["report_mode"])

    def test_old_direct_cycle_is_probe_no_send_and_does_not_touch_production_log(self):
        before = push_pipeline.RUNLOG.stat().st_size if push_pipeline.RUNLOG.exists() else None
        rep = push_pipeline.run(
            CYCLE,
            str(ROOT / "db"),
            no_send=False,
            upstream_failure_report=True,
            degraded_report=True,
        )
        self.assertEqual("probe", rep["execution_context"])
        self.assertFalse(rep["natural_production_evidence"])
        after = push_pipeline.RUNLOG.stat().st_size if push_pipeline.RUNLOG.exists() else None
        self.assertEqual(before, after)

    def test_explicit_production_rejects_old_cycle_before_side_effects(self):
        with self.assertRaisesRegex(ValueError, "outside the current natural window"):
            push_pipeline.run(
                CYCLE,
                str(ROOT / "db"),
                no_send=False,
                execution_context="production",
            )


class DegradedRenderTests(unittest.TestCase):
    """横幅与执行段标记必须由渲染器确定性产出。"""

    def _payload(self, degraded: bool):
        payload = {
            "cycle_id": CYCLE,
            "cycle_count": 1,
            "cycle_duration_s": 10,
            "hhmm": "20:15",
            "action_taken": "HOLD",
            "symbol": "BTC",
            "assets": {"live": {
                "equity": 1000, "availBal": 900, "pnl": 0, "positions": 0}},
            "positions": [],
            "risk": {
                "current_portfolio_imr_ratio": 0.1,
                "max_portfolio_imr_ratio": 0.666,
                "portfolio_imr_ratio_unit": "fraction",
                "lev": 10, "side_pct": 0, "position_count": 0,
                "status": "PASS",
            },
            "market": {
                "btc": 100, "btc_chg24h": 0, "eth": 50, "eth_chg24h": 0,
                "regime": "range", "dxy": 100,
            },
            "decision": {
                "summary": "HOLD",
                "reason": "no executable candidate",
                "decision_protocol": "decision_card_v1",
                "decision_card": {},
            },
            "execution": {"result": "HOLD", "db_rows_live": 0},
            "business_report_attestation": {
                "trade_count": 0, "sha256": "a" * 64},
            "timeline": {"next_hh01_min": 60, "next_review_time": "20:20"},
            "exceptions": [],
        }
        if degraded:
            payload["degraded_report"] = {
                "schema_version": 1,
                "reason": "report_barrier_not_ready",
                "detail": "barrier missing or unsafe",
                "declared_at_cst": "2026-08-19 20:24:00",
                "note": "live 终态凭证未就绪",
            }
        return payload

    def _render(self, degraded: bool) -> str:
        patches = [
            mock.patch.object(render_push_report, name, return_value=None)
            for name in (
                "authoritative_cycle_count", "authoritative_cycle_duration",
                "authoritative_equity", "authoritative_cum_pnl",
                "authoritative_position_count",
            )
        ]
        for patch in patches:
            patch.start()
        try:
            return render_push_report.render(self._payload(degraded))["content"]
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_degraded_payload_renders_banner_on_second_line(self):
        content = self._render(True)
        lines = [ln for ln in content.splitlines() if ln.strip()]
        # 横幅必须紧贴头行 —— 藏在末尾等于没打。
        self.assertIn(vpf.DEGRADED_REPORT_BANNER, lines[1])
        self.assertIn(vpf.DEGRADED_REPORT_CAVEAT, lines[1])
        self.assertIn(vpf.DEGRADED_REPORT_EXEC_TOKEN, content)
        # 事实段一个都不许少：降的是裁决效力，不是内容完整度。
        self.assertIn("账实成交=0笔", content)
        self.assertIn("业务指纹=" + "a" * 64, content)

    def test_normal_payload_renders_no_degraded_marker(self):
        content = self._render(False)
        self.assertNotIn(vpf.DEGRADED_REPORT_BANNER, content)
        self.assertNotIn(vpf.DEGRADED_REPORT_EXEC_TOKEN, content)
        self.assertIn("账实成交=0笔", content)


class DegradedValidatorContractTests(unittest.TestCase):
    """校验器只做单向断言：声称降级 ⇒ 必须留痕。"""

    HEAD = (
        "【20:15】第1轮 / ⏱10s / live / HOLD BTC\n"
        "{banner}Agent自主裁决 | HOLD\n"
        "\n📊 资产\n🟢 实盘：资金 $1000 | 累计收益 0 USDT | 0仓\n"
        "\n💼 持仓详情\n空仓\n\n🛡 风控\nPASS\n\n🌍 行情\nBTC $1 | ETH $1\n"
        "\n🎯 Agent裁决\nHOLD\n\n🧩 三周期判断\n非OPEN/ADD，本轮不适用 "
        "校准可信度=未通过 可信度声明=禁止\n\n🧭 六项决策卡\n-\n"
        "\n📚 历史经验\n-\n\n⚙️ 执行\nHOLD\n{exec_line}\n"
        "\n⏰ 时间线\n下次HH:00: 1min\n\n⚠️ 异常\n无\n"
    )

    def _content(self, *, banner: bool, exec_token: bool) -> str:
        return self.HEAD.format(
            banner=(f"{vpf.DEGRADED_REPORT_BANNER}"
                    f"({vpf.DEGRADED_REPORT_CAVEAT}) | " if banner else ""),
            exec_line=(vpf.DEGRADED_REPORT_EXEC_TOKEN if exec_token else "-"),
        )

    def test_expected_degraded_without_banner_fails_closed(self):
        res = vpf.validate(self._content(banner=False, exec_token=False),
                           cycle_id=CYCLE, expect_degraded=True)
        self.assertFalse(res["ok"])
        self.assertIn("降级战报横幅", res["missing_fields"])
        self.assertFalse(res["degraded_report_declared"])

    def test_banner_without_execution_token_fails_closed(self):
        res = vpf.validate(self._content(banner=True, exec_token=False),
                           cycle_id=CYCLE, expect_degraded=True)
        self.assertFalse(res["ok"])
        self.assertIn("降级屏障标记", res["missing_fields"])

    def test_full_degraded_contract_passes(self):
        res = vpf.validate(self._content(banner=True, exec_token=True),
                           cycle_id=CYCLE, expect_degraded=True)
        self.assertTrue(res["degraded_report_declared"])
        self.assertNotIn("降级战报横幅", res["missing_fields"])
        self.assertNotIn("降级屏障标记", res["missing_fields"])

    def test_normal_report_is_untouched_by_the_new_contract(self):
        """未声称降级的正文不因 5b 多出任何必填项。"""
        content = self._content(banner=False, exec_token=False)
        base = vpf.validate(content, cycle_id=CYCLE)
        self.assertFalse(base["degraded_report_declared"])
        self.assertNotIn("降级战报横幅", base["missing_fields"])
        self.assertNotIn("降级屏障标记", base["missing_fields"])

    def test_banner_is_enforced_even_when_not_expected(self):
        """独立复核归档内容时，带横幅却没执行段标记同样算坏 —— 半截降级声明
        比不声明更容易误读。"""
        res = vpf.validate(self._content(banner=True, exec_token=False),
                           cycle_id=CYCLE)
        self.assertIn("降级屏障标记", res["missing_fields"])


if __name__ == "__main__":
    unittest.main()
