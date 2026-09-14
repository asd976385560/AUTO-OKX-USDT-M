# -*- coding: utf-8 -*-
from contextlib import closing
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from scripts import live_decision_facts as facts
from scripts import stage_runner

class ParallelFactsTests(unittest.TestCase):
    def client(self, count=8):
        from tests.test_live_decision_facts import _raw_inputs
        now=1789194000000
        positions,balance,instruments,algos=_raw_inputs(now)
        class Client:
            def __init__(self):self.lock=threading.Lock();self.active=0;self.peak=0;self.instrument_calls=[];self.algo_calls=[]
            def get_positions(self,profile):
                return {"ok":True,"data":[dict(positions[0],instId=f"S{i}-USDT-SWAP") for i in range(count)]}
            def get_balance(self,profile):return {"ok":True,"data":copy.deepcopy(balance)}
            def get_instrument(self,symbol,profile):
                with self.lock:self.active+=1;self.peak=max(self.peak,self.active);self.instrument_calls.append(symbol)
                time.sleep(0.025)
                with self.lock:self.active-=1
                template=next(iter(instruments.values()))
                return dict(template,instId=symbol)
            def get_algo_orders(self,symbol,profile):
                self.algo_calls.append(symbol)
                template=next(iter(algos.values()))
                return [dict(row,instId=symbol) for row in template]
        return Client(),now

    def test_symbol_reads_overlap_with_a_fixed_worker_bound(self):
        client,now=self.client(12)
        result=facts.build_facts("2026-09-12T14:15",as_of_ms=now,client=client)
        self.assertGreaterEqual(client.peak,2)
        self.assertLessEqual(client.peak,facts.FACTS_READ_WORKERS)
        self.assertEqual(len(client.instrument_calls),12)
        self.assertEqual(len(client.algo_calls),12)
        self.assertEqual(len(result["positions"]),12)
        self.assertEqual(facts.validate_facts(result,expected_cycle="2026-09-12T14:15",now_ms=now),[])

    def test_hedge_rows_share_symbol_reads(self):
        client,now=self.client(1)
        get=client.get_positions
        client.get_positions=lambda p:{"ok":True,"data":get(p)["data"]*2}
        facts.build_facts("2026-09-12T14:15",as_of_ms=now,client=client)
        self.assertEqual(client.instrument_calls,["S0-USDT-SWAP"])
        self.assertEqual(client.algo_calls,["S0-USDT-SWAP"])

    def test_failed_read_keeps_missing_data_blocking(self):
        client,now=self.client(2)
        client.get_instrument=mock.Mock(side_effect=RuntimeError("read failed"))
        result=facts.build_facts("2026-09-12T14:15",as_of_ms=now,client=client)
        self.assertEqual(result["status"],"blocking")
        self.assertFalse(result["action_policy"]["open_add_allowed_by_facts"])
        self.assertTrue(any("instrument_query_failed" in s for s in result["errors"]))

    def test_each_read_is_bounded_by_remaining_budget(self):
        raw=mock.Mock();raw._call.return_value={"ok":True,"data":[]}
        client=facts._BoundedFactsClient(raw,100)
        with mock.patch.object(facts.time,"monotonic",side_effect=[90,99]):client.get_balance("live")
        self.assertEqual(raw._call.call_args.kwargs["timeout_sec"],5)
        self.assertEqual(raw._call.call_args.kwargs["retries"],1)

    def test_expired_or_late_reads_never_supply_facts(self):
        raw=mock.Mock();raw._call.return_value={"ok":True,"data":[]}
        client=facts._BoundedFactsClient(raw,100)
        with mock.patch.object(facts.time,"monotonic",return_value=101),self.assertRaises(TimeoutError):client.get_balance("live")
        raw._call.assert_not_called()
        with mock.patch.object(facts.time,"monotonic",side_effect=[90,101]),self.assertRaises(TimeoutError):client.get_balance("live")

    def test_global_budget_exhaustion_does_not_publish_empty_hold_facts(self):
        raw=mock.Mock();client=facts._BoundedFactsClient(raw,100)
        with mock.patch.object(facts.time,"monotonic",return_value=101),self.assertRaises(TimeoutError):
            facts.build_facts("2026-09-12T14:15",client=client)
        raw._call.assert_not_called()

class FactProcessTimeoutTests(unittest.TestCase):
    def test_timeout_uses_tree_guard_and_retains_diagnostics(self):
        def timeout(command,**kwargs):
            self.assertEqual(kwargs["timeout"],120)
            kwargs["stop_report"].update(process_tree_terminated=True,trigger="timeout")
            return 124,"partial-summary","bounded timeout",True
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with mock.patch.object(stage_runner._proc,"run_guarded",side_effect=timeout) as guard:
                result=stage_runner._prepare_deterministic_live_inputs(cycle="2026-09-12T14:15",facts_file=root/"facts.json",position_exit_file=root/"exit.json",decision_view_file=root/"view.json",db_root=root,log_file=root/"run.log")
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"],"facts_process_failed")
            guard.assert_called_once()
            record=json.loads((root/"run.log").read_text(encoding="utf-8"))
            self.assertTrue(record["timed_out"])
            self.assertTrue(record["process_stop"]["process_tree_terminated"])
            self.assertEqual(record["stdout"],"partial-summary")
            self.assertFalse((root/"facts.json").exists())

if __name__=="__main__":unittest.main()
