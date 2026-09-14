import copy
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import alert_recovery as recovery
import stage_runner as stage

CYCLE='2026-09-12T22:00'
FAILURE='2026-09-12T21:45'
NOW=recovery.cycle_time(CYCLE)+timedelta(minutes=12)

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('scripts','db','config','tmp','logs/stage-status'): (self.root/name).mkdir(parents=True,exist_ok=True)
        self.write('config/alert_recovery.json',{'schema_version':1,'enabled':True,'activation_cycle':'2026-09-12T21:30'})
        self.sql('ledger.db',"CREATE TABLE execution_intents(profile,state); CREATE TABLE stage_profile_leases(profile,expires_at); CREATE TABLE collection_runs(cycle_id,source,status,PRIMARY KEY(cycle_id,source));")
        self.sql('analysis.db','CREATE TABLE analysis_runs(cycle_id PRIMARY KEY,status);')
        self.sql('live_trades.db','CREATE TABLE trade_cycles(cycle_id PRIMARY KEY,decision,n_orders); CREATE TABLE trades(cycle_id);')
        self.sql('account.db','CREATE TABLE repair_queue(status);')
        self.sql('qq_push_dedupe.db','CREATE TABLE sent(k PRIMARY KEY,status);')
        self.good(CYCLE)
        self.bad(FAILURE)
        self.sent=[]

    def write(self,path,value):
        destination=self.root/path;destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(value),encoding='utf-8')

    def sql(self,name,text,args=None):
        with closing(sqlite3.connect(self.root/'db'/name)) as con:
            with con:
                if args is None:con.executescript(text)
                else:con.execute(text,args)

    def good(self,cycle):
        live={'stage':'live','cycle_id':cycle,'status':'succeeded','returncode':0,'profile_lease_released':True,
              'business_check':{'ok':True},'report_reconcile_barrier':{'required':True,'cycle_id':cycle,'profile':'live','status':'ok','rc':0,'report_safe':True,'blocking':False,'healed_count':0}}
        post={'cycle_id':cycle,'profile':'live','ok':True,'issue':False,'rc':0}
        push={'stage':'push','cycle_id':cycle,'status':'succeeded','returncode':0,
              'complete_cycle_sla':{'strict_cycle_pass':True,'status':'met','elapsed_seconds':720},
              'post_live_reconcile':{'rc':0,'started':True,'timed_out':False,'deadline_exceeded':False,'output':json.dumps(post)}}
        att={'ok':True,'required':True,'sha256':'a'*64,'decision':'hold','n_orders':0,'trade_count':0}
        pipe={'cycle':cycle,'ok':True,'send_status':'sent','natural_production_evidence':True,'execution_context':'production',
              'steps':{'send':{'rc':0,'out':'{"messageId":"test-message"}'},'business_attestation_pre_archive':copy.deepcopy(att),'business_attestation_pre_send':copy.deepcopy(att)}}
        self.write(f'logs/stage-status/live-{cycle.replace(":","-")}.json',live)
        self.write(f'logs/stage-status/push-{cycle.replace(":","-")}.json',push)
        self.write('reports/push/2026/09/12/pipeline-'+cycle.replace('T','-').replace(':','')+'.json',pipe)
        self.sql('ledger.db','INSERT OR REPLACE INTO collection_runs VALUES(?,?,?)',(cycle,'fast','ok'))
        self.sql('analysis.db','INSERT OR REPLACE INTO analysis_runs VALUES(?,?)',(cycle,'ok'))
        self.sql('live_trades.db','INSERT OR REPLACE INTO trade_cycles VALUES(?,?,?)',(cycle,'hold',0))

    def bad(self,cycle):
        self.write(f'logs/stage-status/live-{cycle.replace(":","-")}.json',{'stage':'live','cycle_id':cycle,'status':'failed','returncode':86,'failure_kind':'business_verification_error'})

    def mutate(self,kind,change):
        name={'live':f'logs/stage-status/live-{CYCLE.replace(":","-")}.json','push':f'logs/stage-status/push-{CYCLE.replace(":","-")}.json','pipeline':'reports/push/2026/09/12/pipeline-2026-09-12-2200.json'}[kind]
        value=recovery.read_json(self.root/name);change(value);self.write(name,value)

    def observe(self,cycle=CYCLE,send=False,now=NOW):
        return recovery.observe(cycle,root=self.root,now=now,send=send,run_command=self.sender)

    def mark(self,key,status):
        digest=hashlib.sha256(('alert|'+key).encode()).hexdigest()
        self.sql('qq_push_dedupe.db','INSERT OR REPLACE INTO sent VALUES(?,?)',(digest,status))

    def sender(self,command,**kwargs):
        self.assertIn('--alert',command)
        self.assertNotIn('--force',command)
        self.assertEqual(Path(command[1]).name,'qq_push.py')
        self.assertLessEqual(kwargs['timeout'],60)
        self.sent.append(command)
        self.mark(command[command.index('--dedupe-key')+1],'sent')
        return SimpleNamespace(returncode=0,stdout='{"messageId":"recovery"}',stderr='')

    def test_preview_proves_recovery_without_writing_any_business_database(self):
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'db').glob('*.db')}
        r=self.observe()
        self.assertEqual(r['status'],'verified_recovery')
        self.assertIn('原失败轮继续保留',r['would_send'])
        self.assertEqual(self.sent,[])
        self.assertFalse((self.root/'logs/alert-recovery').exists())
        self.assertEqual(hashes,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'db').glob('*.db')})

    def test_initial_fault_then_recovery_sends_once(self):
        historical=(self.root/f'logs/stage-status/live-{FAILURE.replace(":","-")}.json').read_bytes()
        self.assertEqual(self.observe(send=True)['status'],'notified')
        self.assertEqual(self.observe(send=True)['status'],'already_notified')
        self.good('2026-09-12T22:15')
        self.assertEqual(self.observe('2026-09-12T22:15',send=True,now=NOW+timedelta(minutes=15))['status'],'already_notified')
        self.assertEqual(len(self.sent),1)
        self.assertEqual(historical,(self.root/f'logs/stage-status/live-{FAILURE.replace(":","-")}.json').read_bytes())

    def test_recurrence_has_a_new_incident_identity(self):
        first=self.observe(send=True)
        self.bad(CYCLE);self.good('2026-09-12T22:15')
        second=self.observe('2026-09-12T22:15',send=True,now=NOW+timedelta(minutes=15))
        self.assertEqual(second['status'],'notified')
        self.assertNotEqual(first['dedupe_key'],second['dedupe_key'])
        self.assertEqual(len(self.sent),2)

    def test_changed_evidence_between_read_and_send_blocks_notification(self):
        real_ro=recovery.ro
        mutated=False
        def racing_reader(path):
            nonlocal mutated
            if path.name=='live_trades.db' and not mutated:
                mutated=True
                self.mutate('push',lambda x:x.update(status='failed',returncode=86))
            return real_ro(path)
        with mock.patch.object(recovery,'ro',side_effect=racing_reader):
            self.assertEqual(self.observe(send=True)['status'],'not_verified')
        self.assertEqual(self.sent,[])

    def test_notification_keeps_immutable_evidence_after_parent_status_update(self):
        result=self.observe(send=True)
        self.assertEqual(result['status'],'notified')
        self.mutate('push',lambda x:x.update(alert_recovery={'status':'notified'}))
        for item in result['proof']['files'].values():
            self.assertEqual(hashlib.sha256(Path(item['snapshot']).read_bytes()).hexdigest(),item['sha256'])
        self.assertNotEqual(hashlib.sha256(Path(result['proof']['files']['push']['path']).read_bytes()).hexdigest(),result['proof']['files']['push']['sha256'])

    def test_pipeline_success_does_not_hide_independent_open_repair_tickets(self):
        self.sql('account.db',"INSERT INTO repair_queue VALUES('pending')")
        r=self.observe()
        self.assertEqual(r['status'],'verified_recovery')
        self.assertIn('1 条独立修复工单仍待处理',r['would_send'])

    def test_strategy_minimum_order_rejection_is_not_claimed_repaired(self):
        self.write('tmp/_receipt_live_2026-09-12T21-45.json',{'cycle_id':FAILURE,'position_action_failures':[{'result':{'reject_detail':'target_risk_below_minimum_order'}}]})
        r=self.observe()
        self.assertIn('风险预算没有自动放大',r['would_send'])

    def test_verified_existing_autoheal_count_is_reported(self):
        self.mutate('live',lambda x:x['report_reconcile_barrier'].update(healed_count=2))
        self.assertIn('补记 2 项',self.observe()['would_send'])

    def test_missing_message_id_blocks_recovery(self):
        self.mutate('pipeline',lambda x:x['steps']['send'].update(out='PUSH OK'))
        self.assertEqual(self.observe(send=True)['status'],'not_verified');self.assertEqual(self.sent,[])

    def test_successful_failure_report_is_not_business_recovery(self):
        self.mutate('live',lambda x:x.update(status='failed',returncode=86))
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_skipped_reconcile_is_not_recovery(self):
        self.mutate('push',lambda x:x['post_live_reconcile'].update(output=json.dumps({'cycle_id':CYCLE,'profile':'live','ok':True,'issue':False,'rc':0,'skipped':'live_runner_active'})))
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_unresolved_intent_blocks_recovery(self):
        self.sql('ledger.db',"INSERT INTO execution_intents VALUES('live','uncertain')")
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_active_profile_lease_blocks_recovery(self):
        self.sql('ledger.db',"INSERT INTO stage_profile_leases VALUES('live','2026-09-13 00:00:00')")
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_ledger_count_drift_blocks_recovery(self):
        self.sql('live_trades.db','INSERT INTO trades VALUES(?)',(CYCLE,))
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_attestation_fingerprint_drift_blocks_recovery(self):
        self.mutate('pipeline',lambda x:x['steps']['business_attestation_pre_send'].update(sha256='b'*64))
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_database_analysis_failure_blocks_recovery(self):
        self.sql('analysis.db',"UPDATE analysis_runs SET status='error'")
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_degraded_collection_is_not_full_recovery(self):
        self.sql('ledger.db',"UPDATE collection_runs SET status='degraded'")
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_wrong_barrier_identity_blocks_recovery(self):
        self.mutate('live',lambda x:x['report_reconcile_barrier'].update(cycle_id=FAILURE))
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_malformed_post_reconcile_data_fails_closed(self):
        self.mutate('push',lambda x:x['post_live_reconcile'].update(output='[]'))
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_stale_or_future_cycle_not_called_recovered(self):
        for now in (recovery.cycle_time(CYCLE)-timedelta(seconds=1),recovery.cycle_time(CYCLE)+timedelta(seconds=961)):
            with self.subTest(now=now):self.assertEqual(self.observe(now=now)['status'],'not_verified')

    def test_incomplete_sla_does_not_clear_failure(self):
        self.mutate('push',lambda x:x['complete_cycle_sla'].update(strict_cycle_pass=False))
        self.assertEqual(self.observe()['status'],'not_verified')

    def test_missing_database_does_not_send(self):
        (self.root/'db/analysis.db').unlink()
        self.assertEqual(self.observe(send=True)['status'],'not_verified');self.assertEqual(self.sent,[])

    def test_no_prior_fault_produces_no_recovery_spam(self):
        (self.root/'logs/stage-status/live-2026-09-12T21-45.json').unlink()
        self.assertEqual(self.observe(send=True)['status'],'no_prior_failure');self.assertEqual(self.sent,[])

    def test_activation_preserves_historical_boundaries(self):
        self.write('config/alert_recovery.json',{'schema_version':1,'enabled':True,'activation_cycle':CYCLE})
        self.assertEqual(self.observe()['status'],'no_prior_failure')
        self.assertEqual(self.observe(FAILURE)['status'],'before_activation')

    def test_kill_switch_disables_notifications(self):
        self.write('config/alert_recovery.json',{'schema_version':1,'enabled':False})
        self.assertEqual(self.observe(send=True)['status'],'disabled');self.assertEqual(self.sent,[])

    def test_delivery_uncertain_or_pending_is_never_retried(self):
        for status in ('uncertain_delivery','pending','unrecognized'):
            with self.subTest(status=status):
                self.mark('v2-recovered:flow:'+FAILURE,status)
                self.assertEqual(self.observe(send=True)['status'],'delivery_requires_verification')
        self.assertEqual(self.sent,[])

    def test_timeout_after_dedupe_claim_does_not_resend(self):
        def timeout(command,**kwargs):
            self.sent.append(command);self.mark(command[-1],'pending')
            raise subprocess.TimeoutExpired(command,60)
        self.sender=timeout
        self.assertEqual(self.observe(send=True)['status'],'unconfirmed_delivery')
        self.good('2026-09-12T22:15')
        self.assertEqual(self.observe('2026-09-12T22:15',send=True,now=NOW+timedelta(minutes=15))['status'],'delivery_requires_verification')
        self.assertEqual(len(self.sent),1)

    def test_notification_failure_is_distinct_from_business_recovery(self):
        def failed(command,**kwargs):
            self.sent.append(command);self.mark(command[-1],'failed')
            return SimpleNamespace(returncode=1)
        self.sender=failed
        self.assertEqual(self.observe(send=True)['status'],'failed')
        self.assertEqual(self.observe(send=True)['status'],'same_cycle_already_attempted')
        self.assertEqual(len(self.sent),1)
        self.assertEqual(recovery.read_json(self.root/'logs/stage-status/live-2026-09-12T22-00.json')['status'],'succeeded')

    def test_notification_budget_does_not_kill_a_send_midway(self):
        r=self.observe(send=True,now=recovery.cycle_time(CYCLE)+timedelta(seconds=910))
        self.assertEqual(r['status'],'deferred_notification_budget');self.assertEqual(self.sent,[])

    def test_known_failed_notification_retries_are_bounded_across_cycles(self):
        def failed(command,**kwargs):
            self.sent.append(command);self.mark(command[-1],'failed')
            return SimpleNamespace(returncode=1)
        self.sender=failed
        for index in range(4):
            cycle=(recovery.cycle_time(CYCLE)+timedelta(minutes=15*index)).strftime('%Y-%m-%dT%H:%M')
            self.good(cycle)
            result=self.observe(cycle,send=True,now=NOW+timedelta(minutes=15*index))
            self.assertEqual(result['status'],'failed' if index<3 else 'notification_retry_limit')
        self.assertEqual(len(self.sent),3)

    def test_failed_collection_without_live_receipt_can_recover(self):
        (self.root/'logs/stage-status/live-2026-09-12T21-45.json').unlink()
        self.sql('ledger.db','INSERT INTO collection_runs VALUES(?,?,?)',(FAILURE,'fast','error'))
        r=self.observe()
        self.assertEqual(r['status'],'verified_recovery')
        self.assertEqual(r['fault']['causes'][0]['component'],'collection:fast')

class StageHookTests(unittest.TestCase):
    def test_observer_errors_are_nonfatal_and_keep_sensitive_output_out(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ('scripts/alert_recovery.py','config/alert_recovery.json'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
            with mock.patch.multiple(stage,ROOT=root,DB_ROOT=root/'db',STATUS_DIR=root/'logs/stage-status'), mock.patch.object(stage,'_post_push_monitor_deadline_at',return_value=datetime.now(stage.CST)+timedelta(minutes=3)):
                with mock.patch.object(stage._proc,'run_guarded',return_value=(0,json.dumps({'status':'notified','dedupe_key':'key','would_send':'private','delivery_status':'sent'}),'',False)):
                    result=stage._run_alert_recovery_observer(CYCLE)
                    self.assertEqual(result['status'],'notified');self.assertNotIn('would_send',result)
                with mock.patch.object(stage._proc,'run_guarded',side_effect=OSError('test')):
                    self.assertEqual(stage._run_alert_recovery_observer(CYCLE)['status'],'observer_failed')

if __name__=='__main__':unittest.main()
