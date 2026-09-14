from contextlib import closing
from datetime import datetime,timezone,timedelta
import copy,json,sqlite3,unittest
from unittest.mock import patch
from core import execution_intent as ei
from core import open_intent_recovery as recovery
import test_protected_open_reconciliation as fixtures

CST=timezone(timedelta(hours=8));NOW=datetime(2026,9,14,11,16,tzinfo=CST);NEW='2026-09-14T11:15'
class SubmittedOpenRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ProtectedOpenRecoveryTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;ei._clock_cst.return_value=NOW
        with closing(sqlite3.connect(f.path)) as c:
            c.execute('ALTER TABLE stage_profile_leases ADD COLUMN cycle_id')
            c.execute("UPDATE execution_intents SET state='submitted',error=NULL,receipt_json=NULL")
            c.commit();c.row_factory=sqlite3.Row;f.old=dict(c.execute('SELECT * FROM execution_intents').fetchone())
        f.proof.update(read_started_at_ms=NOW.timestamp()*1000,verified_at_ms=NOW.timestamp()*1000)
        f.proof['algos'][0]['ordType']='conditional'
    def run_owner(self,*,apply=False,proof=None,prelaunch=None):
        f=self.fixture
        return ei.reconcile_submitted_open(f.path,expected_row=f.old,proof=proof or f.proof,apply=apply,backup_path=f.backup() if apply else None,prelaunch_cycle=prelaunch)
    def test_readonly_proof_does_not_release(self):
        self.assertEqual('completed',self.run_owner()['would_set_state']);self.assertEqual('submitted',self.fixture.state())
    def test_cas_completes_only_intent_and_preserves_failed_history(self):
        f=self.fixture;before=f.failed_path.read_bytes();result=self.run_owner(apply=True)
        self.assertEqual('completed',result['state']);self.assertEqual(before,f.failed_path.read_bytes());self.assertEqual(0,result['trade_rows_written'])
        with closing(sqlite3.connect(f.path)) as c:receipt=json.loads(c.execute('SELECT receipt_json FROM execution_intents').fetchone()[0])
        self.assertFalse(receipt['tp_verified']);self.assertEqual('original_tp_not_verified',receipt['protection_warning'])
    def test_filled_booked_and_unique_sl_are_all_required(self):
        for mutation in (lambda p:p['order'].update(state='partially_filled'),lambda p:p['fills'][0].update(fillSz='1'),lambda p:p.update(algos=[]),lambda p:p['algos'].append(copy.deepcopy(p['algos'][0])),lambda p:p['positions_after'][0].update(posId='OTHER')):
            proof=copy.deepcopy(self.fixture.proof);mutation(proof)
            with self.assertRaises(Exception):self.run_owner(apply=True,proof=proof)
            self.assertEqual('submitted',self.fixture.state())
    def test_missing_booked_open_or_changed_intent_is_rejected(self):
        f=self.fixture
        with closing(sqlite3.connect(f.trade_path)) as c:c.execute('DELETE FROM trades');c.commit()
        with self.assertRaises(ValueError):self.run_owner(apply=True)
    def test_prelaunch_may_use_only_its_own_not_started_cycle_lease(self):
        f=self.fixture
        with closing(sqlite3.connect(f.path)) as c:c.execute('INSERT INTO stage_profile_leases VALUES(?,?,?)',('live','2026-09-14 12:00:00',NEW));c.commit()
        with self.assertRaises(ValueError):self.run_owner(apply=True)
        result=self.run_owner(apply=True,prelaunch=NEW);self.assertEqual('completed',result['state'])
    def test_started_live_or_foreign_lease_still_blocks(self):
        f=self.fixture
        with closing(sqlite3.connect(f.path)) as c:c.execute('INSERT INTO stage_profile_leases VALUES(?,?,?)',('live','2026-09-14 12:00:00',NEW));c.commit()
        (f.root/'logs/stage-status'/f'live-{NEW.replace(":","-")}.json').write_text('{}')
        with self.assertRaises(ValueError):self.run_owner(apply=True,prelaunch=NEW)
    def test_prelaunch_orchestrator_uses_owner_and_retains_commit_on_later_warning(self):
        f=self.fixture
        with patch.object(recovery,'probe_open',return_value=f.proof):
            result=recovery.recover_booked_submitted_open(f.path.parent,NEW,apply=True)
        self.assertEqual('completed',result['status']);self.assertEqual('completed',f.state())
        self.assertEqual(0,result['exchange_orders_sent']);self.assertTrue(result.get('post_commit_warning'))
    def test_dry_run_and_current_live_do_not_query_exchange(self):
        f=self.fixture
        with patch.object(recovery,'probe_open') as read:
            recovery.recover_booked_submitted_open(f.path.parent,NEW,apply=False)
            (f.root/'logs/stage-status'/f'live-{NEW.replace(":","-")}.json').write_text('{}')
            recovery.recover_booked_submitted_open(f.path.parent,NEW,apply=True)
        read.assert_not_called();self.assertEqual('submitted',f.state())
if __name__=='__main__':unittest.main()
