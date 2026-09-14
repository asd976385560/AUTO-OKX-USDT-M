# -*- coding: utf-8 -*-
import copy
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from core import execution_intent as ei
from core import protection_terminal as pt

SYMBOL="TEST-USDT-SWAP"
START=1789178005000
NOW=1789178020000
CST=timezone(timedelta(hours=8))

@contextmanager
def db(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        yield connection

def sample():
    algo={"algoId":"ALGO-NEW","instId":SYMBOL,"posSide":"long","side":"sell",
          "state":"effective","reduceOnly":"true","failCode":"0","sz":"14",
          "actualSz":"14","slTriggerPx":"0.00357","actualSide":"sl",
          "cTime":str(START+5000),"triggerTime":str(START+5439),"ordIdList":["ORDER-CHILD"]}
    order={"algoId":"ALGO-NEW","ordId":"ORDER-CHILD","instId":SYMBOL,"instType":"SWAP",
           "posSide":"long","side":"sell","reduceOnly":"true","state":"filled",
           "sz":"14","accFillSz":"14","avgPx":"0.003569",
           "fillTime":str(START+5439),"uTime":str(START+5440)}
    return dict(symbol=SYMBOL,side="long",algo_id="ALGO-NEW",expected_sz=14,
                expected_sl=0.00357,expected_tp=None,since_ms=START,
                algo=algo,order=order,positions=[],now_ms=NOW)

class TriggeredProtectionProofTests(unittest.TestCase):
    def test_439ms_trigger_is_a_proven_terminal(self):
        result=pt.validate_triggered_flat(**sample())
        self.assertTrue(result["verified"])
        self.assertEqual(result["closed_sz"],14)
        self.assertEqual(result["child_order_id"],"ORDER-CHILD")

    def test_identity_size_state_and_trigger_mismatches_fail_closed(self):
        variants=[("algo","algoId","OLD"),("algo","sz","13"),("algo","actualSz","13"),
                  ("algo","state","canceled"),("algo","reduceOnly","false"),
                  ("algo","slTriggerPx","0.00350"),("algo","cTime",str(START-1)),
                  ("algo","actualSide","tp"),("algo","ordIdList",["X","Y"]),
                  ("order","algoId","OLD"),("order","ordId","WRONG"),
                  ("order","accFillSz","13"),("order","state","partially_filled"),
                  ("order","reduceOnly","false"),("order","avgPx","NaN")]
        for obj,key,value in variants:
            with self.subTest(obj=obj,key=key,value=value):
                data=sample();data[obj][key]=value
                self.assertFalse(pt.validate_triggered_flat(**data)["verified"])

    def test_reopened_or_unreadable_position_is_not_flat(self):
        for pos in ({"instId":SYMBOL,"posSide":"long","pos":"1"},
                    {"symbol":SYMBOL,"side":"long","sz":"NaN"},
                    {"instId":SYMBOL,"posSide":"net","pos":"0"}):
            data=sample();data["positions"]=[pos]
            self.assertFalse(pt.validate_triggered_flat(**data)["verified"])

    def test_probe_is_three_bounded_read_commands(self):
        data=sample();calls=[]
        def reader(*args,**kw):
            calls.append((args,kw))
            return [data["algo"]] if args[:3]==("swap","algo","orders") else [data["order"]] if args[:2]==("swap","get") else []
        with mock.patch.object(pt.time,"time",return_value=NOW/1000):
            result=pt.probe_triggered_flat(**{k:v for k,v in data.items() if k not in ("algo","order","positions","now_ms")},profile="live",read_json=reader)
        self.assertTrue(result["verified"])
        self.assertEqual(len(calls),3)
        self.assertEqual([c[0][:2] for c in calls],[("swap","algo"),("swap","get"),("account","positions")])
        self.assertTrue(all(c[1]["retries"]==0 and c[1]["timeout_sec"]<=5 for c in calls))

    def test_read_error_is_not_an_empty_success(self):
        data=sample()
        def reader(*args,**kwargs):
            if args[:2]==("account","positions"):return {"code":"1","data":[]}
            return [data["algo"]] if args[:2]==("swap","algo") else [data["order"]]
        result=pt.probe_triggered_flat(**{k:v for k,v in data.items() if k not in ("algo","order","positions","now_ms")},profile="live",read_json=reader)
        self.assertFalse(result["verified"])

    def test_expired_probe_budget_is_not_accepted(self):
        data=sample()
        with mock.patch.object(pt.time,"monotonic",side_effect=[0,0,19]):
            result=pt.probe_triggered_flat(**{k:v for k,v in data.items() if k not in ("algo","order","positions","now_ms")},profile="live",read_json=lambda *a,**k:[data["algo"]])
        self.assertFalse(result["verified"])

class ProtectionIntentWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/"ledger.db"
        self.request={"profile":"live","cycle_id":"2026-09-12T09:45","symbol":SYMBOL,"action":"adjust_protection","side":"long","target_sz":14,"target_sl":0.00357,"target_tp":None}
        fp,raw=ei.canonical_request(self.request)
        self.old={"profile":"live","cycle_id":self.request["cycle_id"],"symbol":SYMBOL,"action":"adjust_protection","side":"long","request_fingerprint":fp,"request_json":raw,"state":"uncertain","reserved_at":datetime.fromtimestamp(START/1000,CST).strftime("%Y-%m-%d %H:%M:%S"),"updated_at":"2026-09-12 09:53:35","submitted_at":"2026-09-12 09:53:35","completed_at":None,"ord_id":"ALGO-NEW","receipt_json":None,"error":"new_protection_unverified"}
        with db(self.path) as c:
            c.executescript(ei.SCHEMA)
            c.execute("CREATE TABLE stage_profile_leases(profile TEXT,cycle_id TEXT,acquired_at TEXT,expires_at TEXT)")
            c.execute("INSERT INTO execution_intents ("+",".join(self.old)+") VALUES ("+",".join("?" for _ in self.old)+")",tuple(self.old.values()))
        when=datetime.fromtimestamp((START+5439)/1000,CST).strftime("%Y-%m-%d %H:%M:%S")
        with db(self.root/"live_trades.db") as c:
            c.execute("CREATE TABLE trades(id INTEGER,ts TEXT,symbol TEXT,side TEXT,action TEXT,sz REAL,fill_px REAL,raw TEXT)")
            c.execute("INSERT INTO trades VALUES(1,?,?,?,'close',14,0.003569,?)",(when,SYMBOL,"long",json.dumps({"ord_ids":["ORDER-CHILD"]})))
        self.backup=self.root/"backup.db"
        with db(self.path) as src,db(self.backup) as dest:src.backup(dest)
        self.proof=pt.validate_triggered_flat(**sample())
        self.fills=[{"ordId":"ORDER-CHILD","instId":SYMBOL,"posSide":"long","side":"sell","tradeId":str(i),"fillSz":str(sz),"fillPx":"0.003569","fillTime":str(START+5439)} for i,sz in enumerate((6,8),1)]
        self.clock=mock.patch.object(ei,"_clock_cst",return_value=datetime.fromtimestamp(NOW/1000,CST))
        self.clock.start();self.addCleanup(self.clock.stop)

    def read_row(self):
        with db(self.path) as c:
            c.row_factory=sqlite3.Row
            return dict(c.execute("SELECT * FROM execution_intents").fetchone())

    def call(self,**kwargs):
        return ei.reconcile_triggered_protection(self.path,expected_row=self.old,proof=self.proof,fills=self.fills,**kwargs)

    def test_default_is_read_only(self):
        self.assertTrue(self.call()["dry_run"])
        self.assertEqual(self.read_row(),self.old)

    def test_apply_changes_only_the_intent_and_keeps_failure_evidence(self):
        before=(self.root/"live_trades.db").read_bytes()
        self.assertTrue(self.call(apply=True,backup_path=self.backup)["ok"])
        row=self.read_row();self.assertEqual(row["state"],"completed")
        receipt=json.loads(row["receipt_json"])
        self.assertEqual(receipt["reconciliation"]["previous_intent"]["error"],"new_protection_unverified")
        self.assertFalse(receipt["reconciliation"]["historical_business_terminal_rewritten"])
        self.assertEqual((self.root/"live_trades.db").read_bytes(),before)

    def test_apply_requires_backup(self):
        with self.assertRaises(ValueError):self.call(apply=True)
        self.assertEqual(self.read_row(),self.old)

    def test_duplicate_or_incomplete_fills_cannot_resolve(self):
        self.fills.append(copy.deepcopy(self.fills[0]))
        with self.assertRaises(ValueError):self.call(apply=True,backup_path=self.backup)
        self.assertEqual(self.read_row(),self.old)

    def test_wrong_ledger_quantity_cannot_resolve(self):
        with db(self.root/"live_trades.db") as c:c.execute("UPDATE trades SET sz=13")
        with self.assertRaises(ValueError):self.call(apply=True,backup_path=self.backup)
        self.assertEqual(self.read_row(),self.old)

    def test_current_position_in_proof_cannot_be_ignored(self):
        self.proof["position_rows"]=[{"instId":SYMBOL,"posSide":"long","pos":"1"}]
        with self.assertRaises(ValueError):self.call(apply=True,backup_path=self.backup)
        self.assertEqual(self.read_row(),self.old)

    def test_active_lease_cannot_be_bypassed(self):
        with db(self.path) as c:c.execute("INSERT INTO stage_profile_leases VALUES('live','C','T','2099-01-01 00:00:00')")
        with self.assertRaises(RuntimeError):self.call(apply=True,backup_path=self.backup)
        self.assertEqual(self.read_row(),self.old)

    def test_concurrent_intent_change_loses_cas(self):
        with db(self.path) as c:c.execute("UPDATE execution_intents SET error='changed' ")
        with self.assertRaises(RuntimeError):self.call(apply=True,backup_path=self.backup)
        self.assertEqual(self.read_row()["state"],"uncertain")

    def test_stale_proof_cannot_resolve(self):
        self.proof["verified_at_ms"]=NOW-31000
        with self.assertRaises(ValueError):self.call(apply=True,backup_path=self.backup)
        self.assertEqual(self.read_row(),self.old)

    def test_generic_state_graph_still_rejects_uncertain_to_completed(self):
        with self.assertRaises(RuntimeError):
            ei.mark_completed(self.path,profile="live",cycle_id=self.old["cycle_id"],symbol=SYMBOL,side="long",action="adjust_protection",fingerprint=self.old["request_fingerprint"],now_ts="2026-09-12 09:53:40",receipt={"ok":True})
        self.assertEqual(self.read_row(),self.old)

class AdjustmentRuntimeTerminalTests(unittest.TestCase):
    def run_adjust(self, verified):
        from contextlib import ExitStack
        from tests import test_adjust_protection as original
        oe=original.oe
        harness=original._Harness([original._live_sl()]);harness.amend_ok=False
        with ExitStack() as stack:
            harness.apply(stack)
            stack.enter_context(mock.patch.object(oe,"_cycle_side_effect_reject",return_value=None))
            stack.enter_context(mock.patch.object(oe,"_enqueue_repair"))
            stack.enter_context(mock.patch.object(oe,"_verify_sl_placed",return_value={"verified":False,"found":[]}))
            probe=stack.enter_context(mock.patch.object(pt,"probe_triggered_flat",return_value={"verified":verified,"positions_confirmed_flat":verified,"algo_id":"NEW1"}))
            result=oe.adjust_protection("LINK-USDT-SWAP","live",cycle_id="2026-08-13T14:00",receipt_context=original._ctx(),new_sl_trigger_px=8.60)
        return result,harness,probe

    def test_triggered_new_stop_completes_intent_without_cancel_or_reorder(self):
        result,harness,probe=self.run_adjust(True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["protection_state"]["position_flat"])
        harness.intent_completed.assert_called_once();harness.intent_uncertain.assert_not_called()
        self.assertEqual(len(harness.place_calls),1);self.assertEqual(harness.cancel_calls,[])
        self.assertEqual(probe.call_args.kwargs["algo_id"],"NEW1")
        self.assertEqual(probe.call_args.kwargs["expected_sz"],100)
        self.assertEqual(probe.call_args.kwargs["expected_sl"],8.60)

    def test_unproven_terminal_remains_uncertain_and_preserves_receipt(self):
        result,harness,probe=self.run_adjust(False)
        self.assertFalse(result["ok"])
        harness.intent_completed.assert_not_called();harness.intent_uncertain.assert_called_once()
        self.assertEqual(harness.cancel_calls,[])
        saved=harness.intent_uncertain.call_args.kwargs["receipt"]
        self.assertEqual(saved["reject_reason"],"new_protection_unverified")
        self.assertEqual(result["new_algo_id"],"NEW1")

if __name__=="__main__":unittest.main()
