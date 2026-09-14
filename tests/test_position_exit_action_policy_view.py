# -*- coding: utf-8 -*-
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from scripts import multitimeframe_decision_evidence as evidence

class ActionPolicyProjectionTests(unittest.TestCase):
    def test_validated_permissions_and_margin_limit_reach_compact_view(self):
        facts={"cycle_id":"2026-09-12T08:15","facts_hash":"a"*64,"positions":[],
               "action_policy":{"position_truth_verified":True,"allowed_executor_actions":["close","reduce","adjust_protection"],"open_add_allowed_by_facts":False},
               "balance":{"current_portfolio_imr_ratio":0.6664193444,"max_portfolio_imr_ratio":0.666,"portfolio_imr_ratio_unit":"fraction","headroom_before_cap_usdt":-0.28840253,"portfolio_margin_state":"at_or_over_cap","new_open_current_ratio_gate":False}}
        with tempfile.TemporaryDirectory() as folder,mock.patch.object(evidence,"validate_facts",return_value=[]):
            payload=evidence.build_position_exit_batch(Path(folder),facts,facts["cycle_id"])
        view=evidence.build_position_exit_decision_view(payload)
        self.assertEqual(view["action_policy"],facts["action_policy"])
        self.assertFalse(view["action_policy"]["open_add_allowed_by_facts"])
        self.assertNotIn("open",view["action_policy"]["allowed_executor_actions"])
        self.assertEqual(view["account_risk_context"]["current_portfolio_imr_ratio"],0.6664193444)
        self.assertLess(list(view).index("action_policy"),list(view).index("positions"))
        self.assertFalse(view["decision_authority"])
        self.assertFalse(view["runner_authority"])
        self.assertEqual(view["orders_placed"],0)
        self.assertEqual(view["production_database_writes"],0)
        unhashed=dict(view);supplied=unhashed.pop("view_hash")
        self.assertEqual(supplied,evidence._canonical_sha256(unhashed))
        view["action_policy"]["allowed_executor_actions"].append("open")
        self.assertNotIn("open",payload["action_policy"]["allowed_executor_actions"])
        self.assertNotIn("open",facts["action_policy"]["allowed_executor_actions"])

    def test_invalid_facts_do_not_publish_action_permissions(self):
        facts={"facts_hash":"a"*64,"positions":[],"action_policy":{"allowed_executor_actions":["open"]}}
        with tempfile.TemporaryDirectory() as folder,mock.patch.object(evidence,"validate_facts",return_value=["facts_hash_mismatch"]):
            payload=evidence.build_position_exit_batch(Path(folder),facts,"2026-09-12T08:15")
        view=evidence.build_position_exit_decision_view(payload)
        self.assertEqual(view["source_status"],"FACTS_INVALID")
        self.assertNotIn("action_policy",view)

    def test_legacy_payload_without_policy_keeps_old_view_shape(self):
        payload={"cycle_id":"2026-08-15T14:15","facts_hash":"a"*64,"evidence_hash":"b"*64,"status":"PASSED","position_count":0,"positions":[],"timeframe_judgment_used":False}
        view=evidence.build_position_exit_decision_view(payload)
        self.assertNotIn("action_policy",view)
        self.assertNotIn("execution_instruction",view)
        self.assertNotIn("account_risk_context",view)

if __name__=="__main__":unittest.main()
