# -*- coding: utf-8 -*-
"""Wave1 序8 —— 证据摘要冻结（终稿 T3）。

验收锚：HYPE 2026-08-10 事故形态——开仓卡 reason 写 "direct n=0" 而卡内自带
同标的亏损样本、平仓卡 summary n=5/列 3 条/说 4 窗口三处不一致——在 v2 契约下
必须机械不可再现：计数与 sample_ids 同源哈希冻结、卡内样本行必须携带契约内
experience_id、close 卡自动生成相对 open 基线的 new/removed/count_delta。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.experience_contract import (  # noqa: E402
    PROTOCOL,
    build_contract,
    validate_contract,
)
from collectors import analyst_writer  # noqa: E402


def _summary(scope, ids, wins):
    n = len(ids)
    return {"scope": scope, "n": n, "wins": wins, "losses": n - wins,
            "sample_ids": list(ids)}


def _contract(same_ids=(), same_wins=0, cross_ids=(), cross_wins=0,
              exact_ids=(), exact_wins=0, symbol="HYPE-USDT-SWAP",
              side="short", as_of="2026-08-10 08:00:00"):
    return build_contract(
        {"symbol": symbol, "side": side, "regime": "range", "action": "open",
         "profile": "live", "as_of": as_of, "min_sim": 0.5, "top_k": 8},
        exact_setup=_summary("same_symbol_side_action_regime",
                             exact_ids, exact_wins),
        same_symbol_similar=_summary("same_symbol_similar", same_ids, same_wins),
        cross_symbol_similar=_summary("cross_symbol_similar",
                                      cross_ids, cross_wins),
    )


class ContractV2Tests(unittest.TestCase):
    def test_protocol_is_v2_and_ids_are_hash_frozen(self):
        contract = _contract(same_ids=(11, 12), same_wins=1)
        self.assertEqual(contract["protocol"], PROTOCOL)
        self.assertEqual(validate_contract(contract), [])
        # 篡改 sample_ids → hash 校验失败（计数无法脱离样本手写）
        tampered = json.loads(json.dumps(contract))
        tampered["summaries"]["same_symbol_similar"]["sample_ids"] = [11]
        tampered["summaries"]["same_symbol_similar"]["n"] = 1
        tampered["summaries"]["same_symbol_similar"]["losses"] = 0
        errors = validate_contract(tampered)
        self.assertTrue(any("evidence_hash" in e for e in errors), errors)

    def test_ids_count_mismatch_rejected(self):
        contract = _contract(same_ids=(11, 12), same_wins=1)
        bad = json.loads(json.dumps(contract))
        bad["summaries"]["same_symbol_similar"]["n"] = 3
        bad["summaries"]["same_symbol_similar"]["losses"] = 2
        errors = validate_contract(bad)
        self.assertTrue(
            any("sample_ids" in e or "evidence_hash" in e for e in errors))


class SampleMembershipTests(unittest.TestCase):
    def test_historical_numeric_prose_is_rejected(self):
        card = {
            "historical_experience": {"reason": "direct n=0, later 0W/5L"},
            "agent_judgement": "follow the current setup",
            "direction_evidence": ["price structure"],
            "opposing_evidence": ["history WR=20%"],
        }
        errors = analyst_writer._validate_history_numeric_prose(card)
        self.assertGreaterEqual(len(errors), 2)
        self.assertTrue(all("scope_counts" in item for item in errors), errors)

    def test_setup_contract_must_match_card_geometry(self):
        card = {"risk_reward": {
            "entry": 100.0, "stop": 95.0, "target": 110.0, "rr": 2.0,
        }}
        missing = _contract()
        errors = analyst_writer._validate_setup_contract(card, missing)
        self.assertTrue(any("query.setup 缺失" in item for item in errors), errors)

    def test_hype_shape_same_symbol_row_without_id_rejected(self):
        """T3：卡内同标的亏损样本行缺 experience_id → 拒。"""
        contract = _contract(same_ids=(21,), same_wins=0)
        history = {
            "matched_wins": [],
            "matched_losses": [{"sim": 0.99, "pnl_pct": -1.06,
                                "note": "direct HYPE short loss"}],
            "evidence_contract": contract,
        }
        errors = analyst_writer._validate_sample_membership(history, contract)
        self.assertTrue(any("缺 experience_id" in e for e in errors), errors)

    def test_foreign_id_rejected(self):
        contract = _contract(same_ids=(21,), same_wins=0)
        history = {
            "matched_losses": [{"experience_id": 999, "pnl_pct": -1.0}],
            "evidence_contract": contract,
        }
        errors = analyst_writer._validate_sample_membership(history, contract)
        self.assertTrue(any("999" in e and "sample_ids" in e for e in errors),
                        errors)

    def test_legit_rows_pass(self):
        contract = _contract(same_ids=(21, 22), same_wins=1,
                             cross_ids=(31,), cross_wins=1)
        history = {
            "matched_wins": [{"experience_id": 22, "pnl_pct": 2.0}],
            "matched_losses": [{"experience_id": 21, "pnl_pct": -1.0}],
            "cross_symbol_wins": [{"experience_id": 31, "pnl_pct": 1.0}],
            "evidence_contract": contract,
        }
        self.assertEqual(
            analyst_writer._validate_sample_membership(history, contract), [])


class EvidenceDeltaTests(unittest.TestCase):
    def _mk_db(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE analysis_signals("
            "cycle_id TEXT, symbol TEXT, action TEXT, side TEXT, "
            "decision_card TEXT)")
        return con

    def test_close_delta_traces_new_samples(self):
        """T3：口径翻转（open 时 n=1 → close 时 n=3）必须指出新增样本 id。"""
        con = self._mk_db()
        open_contract = _contract(same_ids=(21,), same_wins=0)
        open_card = {"historical_experience": {
            "evidence_contract": open_contract}}
        con.execute(
            "INSERT INTO analysis_signals VALUES(?,?,?,?,?)",
            ("2026-08-10T08:00", "HYPE-USDT-SWAP", "open_short", "short",
             json.dumps(open_card)))
        close_contract = _contract(
            same_ids=(21, 35, 36), same_wins=0,
            as_of="2026-08-10 11:00:00")
        delta = analyst_writer._evidence_delta(
            con, "2026-08-10T11:00", "HYPE-USDT-SWAP", "short",
            close_contract)
        self.assertEqual(delta["status"], "ok", delta)
        self.assertEqual(delta["baseline_cycle"], "2026-08-10T08:00")
        scope = delta["per_scope"]["same_symbol_similar"]
        self.assertEqual(scope["new_ids"], [35, 36])
        self.assertEqual(scope["removed_ids"], [])
        self.assertEqual(scope["count_delta"], 2)

    def test_no_baseline_is_honest(self):
        con = self._mk_db()
        delta = analyst_writer._evidence_delta(
            con, "2026-08-10T11:00", "HYPE-USDT-SWAP", "short",
            _contract(same_ids=(1,)))
        self.assertEqual(delta["status"], "baseline_unavailable")

    def test_v1_baseline_marked_legacy(self):
        con = self._mk_db()
        v1_card = {"historical_experience": {"evidence_contract": {
            "protocol": "experience_evidence_v1",
            "summaries": {"same_symbol_similar": {"n": 1}}}}}
        con.execute(
            "INSERT INTO analysis_signals VALUES(?,?,?,?,?)",
            ("2026-08-10T08:00", "HYPE-USDT-SWAP", "open_short", "short",
             json.dumps(v1_card)))
        delta = analyst_writer._evidence_delta(
            con, "2026-08-10T11:00", "HYPE-USDT-SWAP", "short",
            _contract(same_ids=(1,)))
        self.assertEqual(delta["status"], "baseline_unavailable")
        self.assertIn("v1", delta["reason"])


if __name__ == "__main__":
    unittest.main()
