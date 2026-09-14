from contextlib import closing
from datetime import datetime,timedelta,timezone
import copy,hashlib,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core import execution_intent as ei
CST=timezone(timedelta(hours=8));CYCLE='2026-09-13T07:30';NOW=datetime(2026,9,13,12,0,tzinfo=CST)
SYMBOL='CSOPSKHYNIX2L-USDT-SWAP';OID='open-one';BIRTH=str(int(datetime(2026,9,13,7,40,23,tzinfo=CST).timestamp()*1000))
class ProtectedOpenRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'db').mkdir();(self.root/'tmp').mkdir();(self.root/'logs/stage-status').mkdir(parents=True)
        self.path=self.root/'db/ledger.db';self.trade_path=self.root/'db/live_trades.db'
        self.clock=mock.patch.object(ei,'_clock_cst',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)
        request={'profile':'live','cycle_id':CYCLE,'symbol':SYMBOL,'action':'open','side':'short','expected_pre_position_exists':False,
                 'expected_pre_position_sz':0.0,'intended_sz':2.0,'sl_trigger_px':5.406,'tp_trigger_px':5.088}
        reserved=ei.reserve(self.path,profile='live',cycle_id=CYCLE,symbol=SYMBOL,side='short',request=request,now_ts='2026-09-13 07:40:20')
        kw={'profile':'live','cycle_id':CYCLE,'symbol':SYMBOL,'side':'short','fingerprint':reserved['fingerprint']}
        ei.mark_submitting(self.path,now_ts='2026-09-13 07:40:21',**kw)
        ei.mark_submitted(self.path,ord_id=OID,now_ts='2026-09-13 07:40:23',**kw)
        ei.mark_uncertain(self.path,ord_id=OID,error='naked_position_unwind_failed',now_ts='2026-09-13 07:40:40',**kw)
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('CREATE TABLE stage_profile_leases(profile,expires_at)');c.commit();c.row_factory=sqlite3.Row
            self.old=dict(c.execute('SELECT * FROM execution_intents').fetchone())
        self.failure={'cycle_id':CYCLE,'position_action_failures':[{'request':{'symbol':SYMBOL,'side':'short'},'result':{'reject_reason':'naked_position_unwind_failed','unwind':{'reject_reason':'receipt_context_invalid','reject_detail':'非 OPEN/ADD context 禁止携带 open_execution_package'}}}]}
        self.failed_path=self.root/'tmp/_receipt_live_2026-09-13T07-30.json'
        self.failed_path.write_text(json.dumps(self.failure),encoding='utf-8')
        (self.root/'logs/stage-status/live-2026-09-13T07-30.json').write_text(json.dumps({'cycle_id':CYCLE,'status':'failed','profile_lease_released':True}))
        with closing(sqlite3.connect(self.trade_path)) as c:
            c.execute('CREATE TABLE trades(id,cycle_id,ts,symbol,action,side,sz,fill_px,raw)')
            c.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)',(1,CYCLE,'2026-09-13 07:40:23',SYMBOL,'open','short',2,5.3,json.dumps({'ordId':OID,'ord_ids':[OID],'fill_source':'fills'})));c.commit()
        order={'instId':SYMBOL,'ordId':OID,'posSide':'short','side':'sell','state':'filled','reduceOnly':'false','accFillSz':'2','sz':'2','avgPx':'5.3','cTime':BIRTH,'uTime':BIRTH}
        position={'instId':SYMBOL,'posSide':'short','pos':'2','avgPx':'5.3','cTime':BIRTH,'posId':'position-one','markPx':'5.28'}
        algo={'instId':SYMBOL,'algoId':'stop-one','posSide':'short','side':'buy','state':'live','reduceOnly':'true','sz':'2','slTriggerPx':'5.4','cTime':BIRTH}
        self.proof={'read_started_at_ms':NOW.timestamp()*1000-5000,'verified_at_ms':NOW.timestamp()*1000,
                    'failed_receipt_sha256':hashlib.sha256(self.failed_path.read_bytes()).hexdigest(),'order':order,
                    'fills':[{'instId':SYMBOL,'ordId':OID,'posSide':'short','side':'sell','tradeId':'fill-one','fillSz':'2','fillPx':'5.3','fillTime':BIRTH}],
                    'positions_before':[copy.deepcopy(position)],'positions_after':[copy.deepcopy(position)],
                    'instrument':{'instId':SYMBOL,'instType':'SWAP','state':'live','tickSz':'0.01'},'algos':[algo]}

    def state(self):
        with closing(sqlite3.connect(self.path)) as c:return c.execute('SELECT state FROM execution_intents').fetchone()[0]
    def backup(self):
        dest=self.root/'backup.db'
        with closing(sqlite3.connect(self.path)) as source,closing(sqlite3.connect(dest)) as target:source.backup(target)
        return dest
    def test_dry_run_verifies_but_does_not_release(self):
        result=ei.reconcile_protected_open(self.path,expected_row=self.old,proof=self.proof)
        self.assertEqual(result['would_set_state'],'completed');self.assertEqual(self.state(),'uncertain')
    def test_exact_backup_cas_releases_only_the_intent_and_keeps_history(self):
        before=self.failed_path.read_bytes();trade_hash=hashlib.sha256(self.trade_path.read_bytes()).hexdigest()
        result=ei.reconcile_protected_open(self.path,expected_row=self.old,proof=self.proof,apply=True,backup_path=self.backup())
        self.assertEqual(result['state'],'completed');self.assertEqual(self.state(),'completed')
        self.assertEqual(before,self.failed_path.read_bytes());self.assertEqual(trade_hash,hashlib.sha256(self.trade_path.read_bytes()).hexdigest())
        with closing(sqlite3.connect(self.path)) as c:
            row=c.execute('SELECT error,receipt_json FROM execution_intents').fetchone()
            self.assertIn('naked_position_unwind_failed',row[0]);self.assertEqual(json.loads(row[1])['reconciliation']['previous_intent'],self.old)
    def test_rejects_incomplete_wrong_or_stale_evidence_without_writes(self):
        mutations=[lambda p:p.update(read_started_at_ms=NOW.timestamp()*1000-31000),
                   lambda p:p['order'].update(state='partially_filled'),lambda p:p['order'].update(ordId='different'),
                   lambda p:p['fills'].append(copy.deepcopy(p['fills'][0])),lambda p:p['fills'][0].update(fillSz='1'),
                   lambda p:p.update(algos=[]),lambda p:p['algos'][0].update(slTriggerPx='5.41'),
                   lambda p:p['algos'][0].update(reduceOnly='false'),lambda p:p['algos'][0].update(sz='1'),
                   lambda p:p['positions_after'][0].update(pos='3'),lambda p:p['positions_after'][0].update(posId='another'),
                   lambda p:p['instrument'].update(instId='wrong')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                proof=copy.deepcopy(self.proof);mutation(proof)
                with self.assertRaises(Exception):ei.reconcile_protected_open(self.path,expected_row=self.old,proof=proof,apply=True,backup_path=self.backup())
                self.assertEqual(self.state(),'uncertain')
    def test_exchange_unwind_uncertainty_is_not_eligible(self):
        self.failure['position_action_failures'][0]['result']['unwind']={'reject_reason':'close_failed','reject_detail':'timeout'}
        self.failed_path.write_text(json.dumps(self.failure));self.proof['failed_receipt_sha256']=hashlib.sha256(self.failed_path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):ei.reconcile_protected_open(self.path,expected_row=self.old,proof=self.proof)
    def test_backup_drift_active_lease_and_row_cas_are_enforced(self):
        backup=self.backup()
        with closing(sqlite3.connect(self.path)) as c:c.execute("INSERT INTO stage_profile_leases VALUES('live','2026-09-13 13:00:00')");c.commit()
        with self.assertRaises(ValueError):ei.reconcile_protected_open(self.path,expected_row=self.old,proof=self.proof,apply=True,backup_path=backup)
        with closing(sqlite3.connect(self.path)) as c:c.execute('DELETE FROM stage_profile_leases');c.execute("UPDATE execution_intents SET updated_at='2026-09-13 11:59:59'");c.commit()
        with self.assertRaises(ValueError):ei.reconcile_protected_open(self.path,expected_row=self.old,proof=self.proof,apply=True,backup_path=backup)
        self.assertEqual(self.state(),'uncertain')
    def test_ordinary_transition_still_rejects_uncertain_to_completed(self):
        with self.assertRaises(Exception):ei.mark_completed(self.path,profile='live',cycle_id=CYCLE,symbol=SYMBOL,side='short',fingerprint=self.old['request_fingerprint'],now_ts='2026-09-13 12:00:00')
        self.assertEqual(self.state(),'uncertain')
    def test_missing_trade_cannot_be_created_by_this_reconciler(self):
        with closing(sqlite3.connect(self.trade_path)) as c:c.execute('DELETE FROM trades');c.commit()
        with self.assertRaises(ValueError):ei.reconcile_protected_open(self.path,expected_row=self.old,proof=self.proof)
        self.assertEqual(self.state(),'uncertain')
if __name__=='__main__':unittest.main()
