from contextlib import ExitStack,closing
from pathlib import Path
from datetime import datetime,timedelta,timezone
import json,sqlite3,sys,tempfile,unittest
from unittest.mock import patch
from scripts import stage_runner as sr
from core import dispatcher
from collectors import ledger

CYCLE='2026-09-14T11:15'
def initialize(path):
    with closing(sqlite3.connect(path)) as c:
        c.execute('CREATE TABLE stage_profile_leases(profile TEXT PRIMARY KEY,cycle_id TEXT,acquired_at TEXT,expires_at TEXT)')
        c.execute('CREATE TABLE stage_dispatch(cycle_id TEXT,stage TEXT,dispatched_at TEXT,card_id TEXT,UNIQUE(cycle_id,stage))')
        c.commit()
class AlertTradingIsolationTests(unittest.TestCase):
    def setUp(self):
        guard = __import__('unittest.mock', fromlist=['patch']).patch.dict(__import__('os').environ, {"OKX_TRIGGER_DRYRUN": "0"})
        guard.start()
        self.addCleanup(guard.stop)

    def test_report_alert_runs_after_release_and_next_dispatch_nudge(self):
        with tempfile.TemporaryDirectory() as td,ExitStack() as stack:
            root=Path(td);path=root/'ledger.db';initialize(path);self.assertTrue(ledger.try_profile_lease(path,'live',CYCLE))
            events=[]
            def child(*args,terminal_callback=None,**kwargs):
                terminal_callback({'child_returncode':0,'child_timed_out':False,'observed_stop_reason':'business_terminal_committed'})
                return {'returncode':0,'timed_out':False,'started':True,'budget_seconds':300}
            def barrier(*args,**kwargs):
                with closing(sqlite3.connect(path)) as c:self.assertEqual(CYCLE,c.execute("SELECT cycle_id FROM stage_profile_leases WHERE profile='live'").fetchone()[0])
                events.append('barrier_with_exclusive_writer');return {'required':True,'report_safe':False,'status':'error'}
            def nudge(*args):events.append('nudge');return {'nudged':True}
            def alert(*args):
                self.assertTrue(ledger.try_profile_lease(path,'live','next-cycle'))
                events.append('alert_after_next_trade_can_acquire_lease');return {'delivered':False}
            for name,value in [('STATUS_DIR',root),('DB_ROOT',root)]:stack.enter_context(patch.object(sr,name,value))
            stack.enter_context(patch.object(sr,'_run_stage_child',side_effect=child))
            stack.enter_context(patch.object(sr,'verify_business_output',return_value={'ok':True,'checks':[]}))
            stack.enter_context(patch.object(sr,'_run_live_report_reconcile_barrier',side_effect=barrier))
            stack.enter_context(patch.object(sr,'_nudge_after_live_release',side_effect=nudge))
            stack.enter_context(patch.object(sr,'_send_report_barrier_alert',side_effect=alert))
            stack.enter_context(patch.object(sr,'build_complete_cycle_sla',return_value={'status':'met','strict_cycle_pass':True}))
            stack.enter_context(patch.object(sr,'_run_zero_open_watchdog',return_value={'status':'isolated'}))
            stack.enter_context(patch.object(sys,'argv',['stage_runner.py','--stage','live','--cycle',CYCLE,'--mode','unified','--','isolated']))
            self.assertEqual(0,sr.main())
            self.assertEqual(events,['barrier_with_exclusive_writer','nudge','alert_after_next_trade_can_acquire_lease'])
    def test_previous_live_and_push_failures_do_not_veto_new_live(self):
        with tempfile.TemporaryDirectory() as td,ExitStack() as stack:
            root=Path(td);path=root/'ledger.db';previous='2026-09-14T10:45'
            initialize(path)
            for stage in ('live','push'):(root/f'{stage}-{previous.replace(":","-")}.json').write_text(json.dumps({'status':'failed','cycle_id':previous}))
            stack.enter_context(patch.object(dispatcher,'STAGE_STATUS_DIR',root))
            stack.enter_context(patch.object(dispatcher,'analysis_row',return_value=None))
            stack.enter_context(patch.object(dispatcher.ledger,'gate_collection_fresh',return_value={'status':'ok'}))
            candidate,_,_=dispatcher._unified_live_candidate(root,path,[CYCLE],now=datetime(2026,9,14,11,17,tzinfo=timezone(timedelta(hours=8))))
            self.assertEqual(CYCLE,candidate)
            calls=[];result=[]
            dispatcher._fire_stage(path,CYCLE,'live','unified',lambda *args:calls.append(args) or 'isolated',result)
            self.assertEqual(1,len(calls));self.assertTrue(result[0].startswith('fired live'))
if __name__=='__main__':unittest.main()
