# -*- coding: utf-8 -*-
"""规则遵守度审计的激活边界与零样本语义（2026-08-20）。

背景：审计曾把 `2026-08-17T00:00`（部署日零点）当作 3×ATR 与探针 fixed_tp 两条规则
的激活边界，但这两条规则是当天 16:30 那批才写进文档的——
`reports/quality/backups/loss-remediation-20260817-1630/` 下三份 before 备份里都不含
对应条款。于是当天 01:30–13:15 的 8+5 张卡被按尚不存在的规则判成违规，与本项目
「边界只向前生效、历史不重算不重判」直接冲突。

修正边界后立刻暴露第二个坑：这两条规则的激活期恰好落在 2026-08-17T15:15 之后的
零开仓窗内，`evaluated=0`，而原来的收口逻辑是「无违规即 PASS」——审计会对一条从未
被检验过的规则报绿灯。第三个坑：扫描起点绑在其中一条边界上，抬高边界会把边界前的
卡直接滤出查询，pre_activation 诊断跟着消失，等于把不合口径的历史删掉而非重新归类。

本文件把三件事都钉死。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _p in (ROOT, SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import audit_rule_compliance as arc  # noqa: E402


def _card(entry, stop, atr, *, exit_mode="dynamic_exit", rr=2.0,
          overrides=None, ev_r=None, claim_ev_r=None):
    card = {
        "risk_reward": {"entry": entry, "stop": stop, "target": None,
                        "rr": rr, "exit_mode": exit_mode},
        "multitimeframe_analysis": {
            "evidence_contract": {
                "timeframes": {"1H": {"values": {"atr14": atr}}}}},
        "reference_overrides": list(overrides or []),
    }
    if ev_r is not None:
        card["ev_check"] = {"ev_r": ev_r, "claim_ev_r": claim_ev_r}
        card["risk_reward"]["ev_override"] = {"reason": "t", "p_win_claim": 0.5}
    return json.dumps(card, ensure_ascii=False)


class _Fixture:
    """最小 analysis.db + live_trades.db，只放审计真正读的两张表。"""

    def __init__(self, rows, probe_rows=()):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        con = sqlite3.connect(root / "analysis.db")
        con.execute("CREATE TABLE analysis_signals (cycle_id TEXT, symbol TEXT,"
                    " action TEXT, decision_card TEXT)")
        con.executemany("INSERT INTO analysis_signals VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()
        con = sqlite3.connect(root / "live_trades.db")
        con.execute("CREATE TABLE trade_cycles (cycle_id TEXT, raw TEXT)")
        con.executemany("INSERT INTO trade_cycles VALUES (?,?)", probe_rows)
        con.commit()
        con.close()
        self.root = root

    def run(self, since=None):
        return arc.audit(self.root, since or arc.AUDIT_SCAN_START_CYCLE,
                         ROOT / "agents" / "live_trader.md")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.dir.cleanup()


class ActivationBoundaryTests(unittest.TestCase):
    def test_boundaries_sit_after_the_batch_that_created_the_rules(self):
        # 规则文本首次出现于 2026-08-17 16:30 那批；边界必须严格晚于它。
        self.assertGreater(arc.STOP_ATR_ACTIVATION_CYCLE, "2026-08-17T16:30")
        self.assertGreater(arc.PROBE_FIXED_TP_ACTIVATION_CYCLE,
                           "2026-08-17T16:30")

    def test_pre_boundary_card_is_diagnostic_not_violation(self):
        # 08-17T13:15：止损仅 1.5×ATR 且无 override token——规则当时还不存在。
        with _Fixture([("2026-08-17T13:15", "HYPE-USDT-SWAP", "open_long",
                        _card(100.0, 98.5, 1.0))]) as fx:
            out = fx.run()
        self.assertEqual(out["stop_distance_vs_atr_1h"]["violation_count"], 0)
        rules = [d["rule"] for d in out["pre_activation_diagnostics"]]
        self.assertIn("stop_atr", rules)

    def test_post_boundary_card_is_still_a_violation(self):
        with _Fixture([("2026-08-18T09:00", "HYPE-USDT-SWAP", "open_long",
                        _card(100.0, 98.5, 1.0))]) as fx:
            out = fx.run()
        self.assertEqual(out["stop_distance_vs_atr_1h"]["violation_count"], 1)
        self.assertEqual(out["overall_status"], "VIOLATIONS_FOUND")

    def test_declared_override_still_clears_the_floor(self):
        with _Fixture([("2026-08-18T09:00", "HYPE-USDT-SWAP", "open_long",
                        _card(100.0, 98.5, 1.0,
                              overrides=["stop_below_3x_atr_1h"]))]) as fx:
            out = fx.run()
        self.assertEqual(out["stop_distance_vs_atr_1h"]["violation_count"], 0)
        self.assertEqual(out["stop_distance_vs_atr_1h"]["declared_overrides"], 1)


class ScanWindowTests(unittest.TestCase):
    def test_scan_starts_at_the_earliest_boundary(self):
        # 扫描起点若绑在某一条边界上，抬高该边界会把历史直接滤掉而非重新归类。
        self.assertEqual(arc.AUDIT_SCAN_START_CYCLE, min(
            arc.STOP_ATR_ACTIVATION_CYCLE,
            arc.PROBE_FIXED_TP_ACTIVATION_CYCLE,
            arc.EV_OVERRIDE_ACTIVATION_CYCLE))

    def test_raising_a_boundary_reclassifies_rather_than_hides(self):
        with _Fixture([("2026-08-17T13:15", "HYPE-USDT-SWAP", "open_long",
                        _card(100.0, 98.5, 1.0))]) as fx:
            out = fx.run()
        self.assertEqual(out["open_cards_scanned"], 1)
        self.assertGreaterEqual(out["pre_activation_diagnostics_count"], 1)


class ZeroSampleHonestyTests(unittest.TestCase):
    def test_no_samples_is_pending_not_pass(self):
        with _Fixture([]) as fx:
            out = fx.run()
        self.assertEqual(out["overall_status"], "PENDING_FORWARD_EVIDENCE")
        self.assertEqual(out["evaluated_total"], 0)
        for key in ("stop_distance_vs_atr_1h", "probe_fixed_tp", "ev_override"):
            self.assertEqual(out[key]["status"], "PENDING_FORWARD_EVIDENCE")
            self.assertIn("不得读作合规", out[key]["status_reason"])

    def test_pass_lists_the_rules_it_does_not_cover(self):
        # ev_override 边界更早，有样本；另两条无样本必须单列，不被 PASS 顺带带过。
        with _Fixture([("2026-08-17T05:00", "WLD-USDT-SWAP", "open_long",
                        _card(100.0, 96.0, 1.4, ev_r=-0.01,
                              claim_ev_r=0.07))]) as fx:
            out = fx.run()
        self.assertEqual(out["overall_status"], "PASS")
        self.assertIn("stop_distance_vs_atr_1h",
                      out["rules_pending_forward_evidence"])
        self.assertIn("probe_fixed_tp", out["rules_pending_forward_evidence"])
        self.assertIn("不得读作合规", out["overall_status_note"])


class RetirementBoundaryTests(unittest.TestCase):
    """2026-08-28 判断门槛退役：边界后只观察不判违规；历史判定不动。

    与激活边界同一约定「边界只向前生效」，方向相反：退役边界取晚了会把已按
    新契约（无门槛）行事的卡按已废规则判违规——所以边界必须钉在降级批次的
    部署槽上，且退役后的卡要留在 post_retirement_diagnostics 里可查，
    不能从扫描里静默消失。
    """

    def test_boundary_is_registered_on_the_demotion_batch(self):
        # 降级批次部署于 2026-08-28 上午；边界在当天、且不早于部署开始。
        self.assertTrue(arc.RULES_RETIRED_CYCLE.startswith("2026-08-28T"))
        self.assertGreaterEqual(arc.RULES_RETIRED_CYCLE, "2026-08-28T11:00")

    def test_post_retirement_card_is_observed_not_judged(self):
        # 同一张 1.5×ATR 无 override 的卡：边界前是违规（见
        # test_post_boundary_card_is_still_a_violation），边界后只观察。
        with _Fixture([("2026-08-29T09:00", "HYPE-USDT-SWAP", "open_long",
                        _card(100.0, 98.5, 1.0))]) as fx:
            out = fx.run()
        self.assertEqual(out["stop_distance_vs_atr_1h"]["violation_count"], 0)
        self.assertEqual(out["total_violations"], 0)
        rules = [d["rule"] for d in out["post_retirement_diagnostics"]]
        self.assertIn("stop_atr", rules)
        self.assertEqual(out["post_retirement_diagnostics_count"], 1)

    def test_post_retirement_negative_ev_card_is_not_a_violation(self):
        # 退役后自认 claim_ev_r<=0 也不再判 accepts_negative_ev 违规——
        # 判断权已还给 Agent；writer 的如实标注继续兜数据真实性。
        with _Fixture([("2026-08-29T09:00", "WLD-USDT-SWAP", "open_long",
                        _card(100.0, 96.0, 1.4, ev_r=-0.01,
                              claim_ev_r=-0.02))]) as fx:
            out = fx.run()
        self.assertEqual(out["ev_override"]["violation_count"], 0)
        rules = [d["rule"] for d in out["post_retirement_diagnostics"]]
        self.assertIn("ev_override", rules)

    def test_pre_retirement_history_is_still_judged(self):
        # 退役不重判历史：边界前的违规判定原样保留。
        with _Fixture([("2026-08-18T09:00", "HYPE-USDT-SWAP", "open_long",
                        _card(100.0, 98.5, 1.0))]) as fx:
            out = fx.run()
        self.assertEqual(out["stop_distance_vs_atr_1h"]["violation_count"], 1)
        self.assertEqual(out["overall_status"], "VIOLATIONS_FOUND")


if __name__ == "__main__":
    unittest.main()
