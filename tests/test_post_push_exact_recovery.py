import contextlib,io,json,sqlite3,subprocess,sys,tempfile,time,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch,Mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from scripts import live_reconcile_monitor as m
from scripts import ledger_recovery
from collectors import ledger

CYCLE='2026-09-13T22:15'
NOW=datetime(2026,9,13,22,25,tzinfo=timezone(timedelta(hours=8)))
INITIAL={'rc':1,'markers':['[GHOST-EXACT]'],'issue':True}

def clean(verified=True):
    return {'status':'ok','rc':0,'blocking':False,'p0':False,
            'unrecorded_count':0,'findings':[],'applied':False,
            'recovery_chain':{'applied_any':True,'verified_after_write':verified,
                              'stop_reason':'converged_and_reverified'}}

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(m,'LOG_DIR',Path(self.tmp.name)))
        self.stack.enter_context(patch.object(m,'now_cst',return_value=NOW))
        self.stack.enter_context(patch.dict(m.os.environ,{
            'OKX_DISABLE_LEDGER_AUTOHEAL':'0','OKX_LEDGER_AUTOHEAL_APPLY':'1'}))
        self.ready=self.stack.enter_context(patch.object(m,'_recovery_live_ready',return_value=True))
        self.backups=self.stack.enter_context(patch.object(m,'_recovery_backups',return_value=[]))
        self.lease=self.stack.enter_context(patch.object(ledger,'try_profile_lease',return_value=True))
        self.release=self.stack.enter_context(patch.object(ledger,'release_profile_lease',return_value=True))
        self.chain=self.stack.enter_context(patch.object(ledger_recovery,'recover_in_budget',return_value=clean()))
    def run_recovery(self,initial=None,cycle=CYCLE,started=None):
        return m.recover_exact_after_push(cycle,initial or INITIAL,
                    monitor_started=started if started is not None else time.monotonic())
    def test_success_requires_verified_contract_and_releases_lease(self):
        result=self.run_recovery()
        self.assertTrue(result['recovered']);self.assertTrue(result['lease_released'])
        self.assertEqual(self.ready.call_count,2);self.backups.assert_called_once()
        self.assertLessEqual(self.chain.call_args.kwargs['timeout_sec'],175)
        self.release.assert_called_once()
    def test_mixed_or_legacy_never_writes(self):
        for markers,cycle in [(['[GHOST-EXACT]','[GHOST-FUZZY]'],CYCLE),(['[UNRECORDED]'],CYCLE),(['[GHOST-EXACT]'],'2026-09-13T22:00')]:
            self.assertFalse(self.run_recovery({'rc':1,'markers':markers},cycle)['recovered'])
        self.lease.assert_not_called();self.chain.assert_not_called()
    def test_kill_switches_preserved(self):
        for name,value in [('OKX_DISABLE_LEDGER_AUTOHEAL','1'),('OKX_LEDGER_AUTOHEAL_APPLY','0')]:
            with patch.dict(m.os.environ,{name:value}):self.assertFalse(self.run_recovery()['recovered'])
        self.lease.assert_not_called()
    def test_missing_terminal_or_busy_lease_never_writes(self):
        self.ready.return_value=False;self.assertFalse(self.run_recovery()['recovered'])
        self.ready.return_value=True;self.lease.return_value=False
        self.assertFalse(self.run_recovery()['recovered']);self.chain.assert_not_called()
        self.release.assert_not_called()
    def test_budget_expiry_before_or_after_backup_blocks(self):
        self.assertFalse(self.run_recovery(started=time.monotonic()-120)['recovered'])
        def expire(_):
            m.now_cst.return_value=NOW+timedelta(minutes=5)
            return []
        self.backups.side_effect=expire
        self.assertFalse(self.run_recovery()['recovered']);self.chain.assert_not_called()
        self.release.assert_called_once()
    def test_failed_verification_or_write_error_never_recovers(self):
        self.chain.return_value=clean(False)
        self.assertFalse(self.run_recovery()['recovered'])
        self.chain.side_effect=TimeoutError('owner exceeded remaining budget')
        r=self.run_recovery();self.assertFalse(r['recovered']);self.assertTrue(r['lease_released'])
        self.assertIn('TimeoutError',r['reason'])
    def test_release_error_is_visible_and_not_recovered(self):
        self.release.side_effect=OSError('lease DB unavailable')
        result=self.run_recovery();self.assertFalse(result['recovered'])
        self.assertIn('OSError',result['release_error'])
    def test_callbacks_make_write_then_readonly_owner_calls(self):
        def chain(run_once,*,verify_once,**kw):
            run_once(80);verify_once(20);return clean()
        self.chain.side_effect=chain
        with patch.object(m,'_recovery_once',return_value={}) as owner:
            self.assertTrue(self.run_recovery()['recovered'])
        self.assertEqual([c.kwargs['apply'] for c in owner.call_args_list],[True,False])

class EntryAndEvidenceTests(unittest.TestCase):
    def test_real_terminal_and_intent_proof(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);p=root/'live-2026-09-13T22-15.json'
            status={'cycle_id':CYCLE,'status':'succeeded','returncode':0,
                    'business_check':{'ok':True},'profile_lease_released':True}
            p.write_text(json.dumps(status),encoding='utf-8')
            with contextlib.closing(sqlite3.connect(root/'ledger.db')) as c:
                c.execute('CREATE TABLE execution_intents(profile TEXT,state TEXT)')
                c.execute("INSERT INTO execution_intents VALUES('live','completed')")
                c.commit()
            with patch.object(m,'STAGE_STATUS_DIR',root),patch.object(m,'DB_ROOT',root),patch.object(m,'active_runner',return_value=None):
                self.assertTrue(m._recovery_live_ready(CYCLE))
                with contextlib.closing(sqlite3.connect(root/'ledger.db')) as c:
                    c.execute("INSERT INTO execution_intents VALUES('live','submitted')");c.commit()
                self.assertFalse(m._recovery_live_ready(CYCLE))
                status['status']='failed';p.write_text(json.dumps(status),encoding='utf-8')
                self.assertFalse(m._recovery_live_ready(CYCLE))
    def test_owner_command_is_close_only_and_binds_contract(self):
        from collectors import trigger_agent
        with tempfile.TemporaryDirectory() as td,patch.object(m,'LOG_DIR',Path(td)),patch.object(m.subprocess,'run',return_value=Mock(returncode=0)) as run,patch.object(trigger_agent,'_read_autoheal_contract',return_value=clean()) as read:
            m._recovery_once(CYCLE,61,apply=True)
            args=run.call_args.args[0]
            self.assertIn('--apply',args);self.assertNotIn('--enable-unrecorded',args)
            self.assertEqual(run.call_args.kwargs['timeout'],61)
            self.assertEqual(read.call_args.kwargs['cycle_id'],CYCLE)
    def test_dry_run_never_recovers_or_sends(self):
        with patch.object(sys,'argv',['monitor','--cycle',CYCLE,'--autoheal-exact','--dry-run']),patch.object(m,'active_runner',return_value=None),patch.object(m,'run_reconcile',return_value=(1,'[GHOST-EXACT] X short sz=1')),patch.object(m,'recover_exact_after_push') as heal,patch.object(m.subprocess,'run') as send,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(),1);heal.assert_not_called();send.assert_not_called()
    def test_recovery_notice_is_separate_and_preserves_detection(self):
        with tempfile.TemporaryDirectory() as td,patch.object(m,'LOG_DIR',Path(td)),patch.object(sys,'argv',['monitor','--cycle',CYCLE,'--autoheal-exact']),patch.object(m,'active_runner',return_value=None),patch.object(m,'run_reconcile',return_value=(1,'[GHOST-EXACT] X short sz=1')),patch.object(m,'recover_exact_after_push',return_value={'recovered':True}),patch.object(m.subprocess,'run',return_value=Mock(returncode=0)) as send,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.main(),0)
            result=json.loads(output.getvalue())
            self.assertTrue(result['recovered']);self.assertFalse(result['issue'])
            self.assertEqual(result['markers'],['[GHOST-EXACT]'])
            self.assertIn('-recovered:',send.call_args.args[0][-1])
    def test_cold_script_entry_outside_project(self):
        with tempfile.TemporaryDirectory() as td:
            p=subprocess.run([sys.executable,'-I',m.__file__,'--help'],cwd=td,capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(p.returncode,0,p.stderr);self.assertIn('--autoheal-exact',p.stdout)

if __name__=='__main__':unittest.main()
