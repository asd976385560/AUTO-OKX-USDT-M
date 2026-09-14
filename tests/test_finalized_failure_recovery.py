from contextlib import closing
from pathlib import Path
import hashlib,json,sqlite3,tempfile,unittest
from unittest.mock import patch
from scripts import live_reconcile_monitor as m

C='2026-09-13T23:15'
class FinalizedFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'tmp').mkdir();(self.root/'db').mkdir();(self.root/'status').mkdir()
        self.plan=b'{"cycle_id":"2026-09-13T23:15","profile":"live"}'
        self.ph=hashlib.sha256(self.plan).hexdigest();self.fh='a'*64
        self.runner={'cycle_id':C,'state':'committed','plan_sha256':self.ph,'facts_hash':self.fh}
        self.receipt={'cycle_id':C,'profile':'live','runner_in_progress':False,'batch_status':'partial','ok':True,'plan_sha256':self.ph,'facts_hash':self.fh}
        self.facts={'cycle_id':C,'profile':'live','facts_hash':self.fh}
        self.status={'cycle_id':C,'status':'failed','returncode':86,'failure_kind':'business_verification_error','profile_lease_released':True}
        self.write_files()
        with closing(sqlite3.connect(self.root/'db/live_trades.db')) as c:
            c.execute('CREATE TABLE trade_cycles(cycle_id TEXT,raw TEXT)');c.execute('INSERT INTO trade_cycles VALUES(?,?)',(C,json.dumps(self.receipt)));c.commit()
        with closing(sqlite3.connect(self.root/'db/ledger.db')) as c:
            c.execute('CREATE TABLE execution_intents(profile TEXT,state TEXT)');c.commit()
        self.patches=[patch.object(m,'ROOT',self.root),patch.object(m,'DB_ROOT',self.root/'db'),patch.object(m,'STAGE_STATUS_DIR',self.root/'status'),patch.object(m,'active_runner',return_value=None)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    def write_files(self):
        slug=C.replace(':','-')
        for prefix,value in [('live_runner_state',self.runner),('_receipt_live',self.receipt),('live_facts',self.facts)]:
            (self.root/'tmp'/f'{prefix}_{slug}.json').write_text(json.dumps(value),encoding='utf-8')
        (self.root/'tmp'/f'position_plan_{slug}.json').write_bytes(self.plan)
        (self.root/'status'/f'live-{slug}.json').write_text(json.dumps(self.status),encoding='utf-8')
    def test_committed_failed_business_can_reconcile_but_is_not_rejudged(self):
        before=(self.root/'status'/f'live-{C.replace(":","-")}.json').read_bytes()
        self.assertTrue(m._recovery_live_ready(C))
        self.assertEqual(before,(self.root/'status'/f'live-{C.replace(":","-")}.json').read_bytes())
        self.assertEqual(self.status['status'],'failed')
    def test_running_failed_or_unfinished_runner_cannot_reconcile(self):
        for state in ('executing','failed','prepared'):
            self.runner['state']=state;self.write_files();self.assertFalse(m._recovery_live_ready(C))
        self.runner['state']='committed';self.receipt['runner_in_progress']=True;self.write_files();self.assertFalse(m._recovery_live_ready(C))
    def test_plan_and_facts_tampering_rejected(self):
        self.plan+=b' ';self.write_files();self.assertFalse(m._recovery_live_ready(C))
        self.plan=self.plan[:-1];self.facts['facts_hash']='b'*64;self.write_files();self.assertFalse(m._recovery_live_ready(C))
    def test_cross_cycle_or_profile_rejected(self):
        self.receipt['cycle_id']='2026-09-13T23:00';self.write_files();self.assertFalse(m._recovery_live_ready(C))
        self.receipt['cycle_id']=C;self.facts['profile']='demo';self.write_files();self.assertFalse(m._recovery_live_ready(C))
    def test_database_commit_must_match_terminal_receipt(self):
        with closing(sqlite3.connect(self.root/'db/live_trades.db')) as c:
            c.execute('UPDATE trade_cycles SET raw=?',(json.dumps({**self.receipt,'runner_in_progress':True}),));c.commit()
        self.assertFalse(m._recovery_live_ready(C))
        with closing(sqlite3.connect(self.root/'db/live_trades.db')) as c:c.execute('DELETE FROM trade_cycles');c.commit()
        self.assertFalse(m._recovery_live_ready(C))
    def test_live_lease_and_unresolved_intent_still_block(self):
        self.status['profile_lease_released']=False;self.write_files();self.assertFalse(m._recovery_live_ready(C))
        self.status['profile_lease_released']=True;self.write_files()
        with closing(sqlite3.connect(self.root/'db/ledger.db')) as c:c.execute("INSERT INTO execution_intents VALUES('live','submitted')");c.commit()
        self.assertFalse(m._recovery_live_ready(C))
    def test_transport_failure_and_legacy_cycle_remain_blocked(self):
        self.status['failure_kind']='model_output_length';self.write_files();self.assertFalse(m._recovery_live_ready(C))
        self.status['failure_kind']='business_verification_error';self.status['returncode']=87;self.write_files();self.assertFalse(m._recovery_live_ready(C))
        self.assertFalse(m._finalized_failed_writer('2026-09-13T23:00',{**self.status,'returncode':86}))
    def test_missing_or_malformed_artifact_is_not_finality_proof(self):
        path=self.root/'tmp'/f'live_runner_state_{C.replace(":","-")}.json';path.write_text('{')
        self.assertFalse(m._recovery_live_ready(C));path.unlink();self.assertFalse(m._recovery_live_ready(C))

if __name__=='__main__':unittest.main()
