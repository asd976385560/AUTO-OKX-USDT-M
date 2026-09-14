

def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

import copy,json,sqlite3,tempfile,unittest,uuid
from datetime import datetime,timedelta
from pathlib import Path
from unittest import mock
from scripts import ledger_recovery as recovery
from scripts import live_position_action_runner as runner
from scripts import live_decision_facts as facts
from scripts import ledger_autoheal as heal
from scripts import stage_runner as stage
from core import order_executor as oe
from collectors import trigger_agent as trigger
import test_live_decision_facts as fact_fixtures
import test_stage_retry_recovery as push_fixtures

CYCLE='2026-09-13T18:15'
NOW=datetime(2026,9,13,18,16,tzinfo=recovery.CST)
def result(ids=(),pending=0):
    rows=[{'kind':'GHOST-EXACT','symbol':f'T{x}-USDT-SWAP','side':'long','sz':1.0,'ord_ids':[str(x)],'applied':True} for x in ids]
    return {'contract_version':1,'request_id':uuid.uuid4().hex,'profile':'live','cycle':CYCLE,'apply':True,
      'applied':bool(ids),'blocking':pending>0,'p0':False,'rc':1 if pending else 0,
      'status':'needs_human' if pending else 'applied' if ids else 'ok',
      'findings':[{'kind':'AUTOHEAL-BACKLOG','count':pending,'cap':3}] if pending else [],'healed':rows,
      'unrecorded_count':0,'backlog':{'cap':3,'remaining_exact_count':pending,'evidence_deferred_count':0}}
def verified():return {**result(),'apply':False}

class RecoveryChainTests(unittest.TestCase):
    def run_chain(self,values,verify=None,clock=lambda:0.0):
        call=mock.Mock(side_effect=values);read=mock.Mock(return_value=verify or verified())
        out=recovery.recover_in_budget(call,verify_once=read,cycle=CYCLE,now=NOW,clock=clock)
        return out,call,read
    def test_four_ghosts_finish_as_three_one_then_independent_read(self):
        out,call,read=self.run_chain([result([1,2,3],1),result([4])])
        self.assertEqual(2,call.call_count);self.assertEqual(1,read.call_count)
        self.assertEqual('ok',out['status']);self.assertFalse(out['blocking'])
        self.assertFalse(out['applied']);self.assertTrue(out['recovery_chain']['applied_any'])
        self.assertTrue(out['recovery_chain']['verified_after_write'])
        self.assertTrue(out['recovery_chain']['attempts'][-1]['verification_only'])
    def test_unknown_unrecorded_p0_or_unapplied_never_continue(self):
        cases=[]
        for field,value in [('p0',True),('apply',False),('applied',False),('unrecorded_count',1),('status','error')]:
            x=result([1,2,3],1);x[field]=value;cases.append(x)
        x=result([1,2,3],1);x['findings'].append({'kind':'GHOST-FUZZY'});cases.append(x)
        x=result([1,2,3],1);x['backlog']['evidence_deferred_count']=1;cases.append(x)
        for x in cases:
            with self.subTest(x=x):
                out,call,read=self.run_chain([x]);self.assertEqual(1,call.call_count);read.assert_not_called();self.assertTrue(out['blocking'])
    def test_duplicate_committed_orders_stop_repeated_progress(self):
        out,call,read=self.run_chain([result([1,2,3],2),result([1,2,3],1)])
        self.assertEqual(2,call.call_count);read.assert_not_called()
        self.assertEqual('no_new_committed_progress',out['recovery_chain']['stop_reason'])
    def test_mutating_attempt_limit_remains_blocked(self):
        out,call,read=self.run_chain([result([1,2,3],8),result([4,5,6],5),result([7,8,9],2)])
        self.assertEqual(3,call.call_count);read.assert_not_called();self.assertTrue(out['blocking'])
    def test_insufficient_time_does_not_start_another_writer(self):
        ticks=[0.0]
        def call(budget):ticks[0]=151;return result([1,2,3],1)
        write=mock.Mock(side_effect=call);read=mock.Mock()
        out=recovery.recover_in_budget(write,verify_once=read,cycle=CYCLE,now=NOW,clock=lambda:ticks[0])
        self.assertEqual(1,write.call_count);read.assert_not_called()
        self.assertEqual('remaining_budget_insufficient',out['recovery_chain']['stop_reason'])
    def test_new_gap_on_independent_read_still_blocks(self):
        check={**result([2],1),'apply':False,'applied':False}
        out,write,read=self.run_chain([result([1])],check)
        self.assertTrue(out['blocking']);self.assertFalse(out['recovery_chain']['verified_after_write'])
        self.assertEqual(1,write.call_count)
    def test_clean_initial_state_has_no_extra_call(self):
        out,write,read=self.run_chain([result()]);self.assertEqual(1,write.call_count);read.assert_not_called()
    def test_business_deadline_caps_original_budget(self):
        call=mock.Mock(return_value=result())
        recovery.recover_in_budget(call,cycle=CYCLE,now=NOW+timedelta(minutes=13,seconds=20),clock=lambda:0)
        self.assertEqual(10.0,call.call_args.args[0])
    def test_activation_is_forward_only_and_canonical(self):
        self.assertFalse(recovery.enabled('2026-09-13T17:45'));self.assertTrue(recovery.enabled(CYCLE))
        for value in ['c1',None,'2026-09-13T18:01','2026-09-13T18:00Z']:self.assertFalse(recovery.enabled(value))
    def test_actual_clients_have_separate_read_only_verification(self):
        real_recover = recovery.recover_in_budget
        def frozen_recover(run_once, **kwargs):
            return real_recover(run_once, **kwargs, now=NOW)
        for module,name,callargs in [(oe,'_try_autoheal_ledger_once',('live',Path(_public_project_path('db')),CYCLE)),(trigger,'_autoheal_ledger_once',('live',CYCLE))]:
            entry=oe._try_autoheal_ledger if module is oe else trigger._autoheal_ledger
            with mock.patch.object(module,name,side_effect=[result([1,2,3],1),result([4]),verified()]) as one, mock.patch.object(recovery,'recover_in_budget',side_effect=frozen_recover):
                out=entry(*callargs)
                self.assertEqual(3,one.call_count);self.assertIs(one.call_args.kwargs['apply_enabled_override'],False)
                self.assertTrue(out['recovery_chain']['verified_after_write'])

class VanishedPositionTests(unittest.TestCase):
    def base(self):
        return {'ok':False,'action_taken':'REJECT','p0':False,'trades':[],
                'reject_reason':'pre_position_fingerprint_changed',
                'expected_pre_position':{'exists':True,'sz':9.0,'posId':'old-epoch','cTime':'1789200000000'},
                'actual_pre_position':None}
    def test_close_and_reduce_absent_target_need_no_order(self):
        for action in ('CLOSE','REDUCE'):
            self.assertTrue(runner._is_closure_clean_hard_reject(self.base(),{'action':action},CYCLE))
    def test_read_failure_reopened_position_and_submitted_order_stay_failed(self):
        mutations=[{'actual_pre_position':{'sz':8,'posId':'old-epoch'}},
                   {'actual_pre_position':{'sz':9,'posId':'new-epoch'}},
                   {'ordId':'123'},{'exchange_side_effect_uncertain':True},
                   {'reject_reason':'positions_fetch_failed'},{'p0':True}]
        for change in mutations:self.assertFalse(runner._is_closure_clean_hard_reject({**self.base(),**change},{'action':'CLOSE'},CYCLE))
    def test_absence_label_without_position_evidence_does_not_pass(self):
        for field in ['actual_pre_position','expected_pre_position']:
            value=self.base();value.pop(field)
            self.assertFalse(runner._is_closure_clean_hard_reject(value,{'action':'CLOSE'},CYCLE))
    def test_old_failed_cycles_keep_old_classification(self):
        self.assertFalse(runner._is_closure_clean_hard_reject(self.base(),{'action':'CLOSE'},'2026-09-13T16:30'))

class FactsCoherenceTests(unittest.TestCase):
    def fake(self,after='flat',protected_after=False):
        asof=int(NOW.timestamp()*1000);positions,balance,instruments,algos=fact_fixtures._raw_inputs(asof)
        if after=='flat':second=[]
        elif after=='changed':second=[{**positions[0],'pos':'3','posId':'NEW'}]
        else:second=positions
        client=mock.Mock()
        client.get_positions.side_effect=[{'ok':True,'data':positions},
            {'ok':False,'error':'timeout','data':[]} if after=='error' else {'ok':True,'data':second}]
        client.get_balance.return_value={'ok':True,'data':balance}
        client.get_instrument.side_effect=lambda s,p:instruments[s]
        client.get_algo_orders.side_effect=[[],algos['ETH-USDT-SWAP'] if protected_after else []]
        return client,asof
    def test_filled_stop_disappears_from_refreshed_facts(self):
        client,asof=self.fake();out=facts.build_facts(CYCLE,client=client,as_of_ms=asof)
        self.assertEqual('ok',out['status']);self.assertEqual([],out['positions'])
        self.assertEqual([],facts.validate_facts(out,expected_cycle=CYCLE));self.assertEqual(2,client.get_positions.call_count)
    def test_real_missing_protection_stays_blocking(self):
        client,asof=self.fake('same');out=facts.build_facts(CYCLE,client=client,as_of_ms=asof)
        self.assertEqual('blocking',out['status']);self.assertFalse(out['action_policy']['open_add_allowed_by_facts'])
    def test_replacement_stop_must_be_read_back(self):
        client,asof=self.fake('same',True);out=facts.build_facts(CYCLE,client=client,as_of_ms=asof)
        self.assertEqual('ok',out['status']);self.assertEqual(2,client.get_algo_orders.call_count)
    def test_changed_size_cannot_reuse_old_smaller_stop(self):
        client,asof=self.fake('changed',True);out=facts.build_facts(CYCLE,client=client,as_of_ms=asof)
        self.assertEqual('blocking',out['status']);self.assertEqual(3.0,out['positions'][0]['contracts'])
    def test_failed_refresh_is_never_interpreted_as_flat(self):
        client,asof=self.fake('error');out=facts.build_facts(CYCLE,client=client,as_of_ms=asof)
        self.assertEqual('blocking',out['status']);self.assertEqual(1,len(out['positions']))
        self.assertFalse(out['action_policy']['position_truth_verified'])
    def test_no_fault_has_no_extra_position_read(self):
        client,asof=self.fake('same',True);_,_,_,algos=fact_fixtures._raw_inputs(asof)
        client.get_algo_orders.side_effect=None;client.get_algo_orders.return_value=algos['ETH-USDT-SWAP']
        out=facts.build_facts(CYCLE,client=client,as_of_ms=asof)
        self.assertEqual('ok',out['status']);self.assertEqual(1,client.get_positions.call_count)

class QueueAndNotificationTests(unittest.TestCase):
    def test_only_exact_no_order_profile_block_tickets_are_selected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'account.db';c=sqlite3.connect(p)
            c.execute('CREATE TABLE repair_queue(id INTEGER,check_name TEXT,status TEXT,issue TEXT)')
            values=[(1,'order_executor','pending','[live] SKHY-USDT-SWAP ord=None: pretrade_ledger_autoheal_blocked:backlog'),
                    (2,'order_executor','pending','[live] SKHY-USDT-SWAP ord=123: pretrade_ledger_autoheal_blocked:unknown'),
                    (3,'order_executor','pending','[live] SKHY-USDT-SWAP ord=None: naked_position'),
                    (4,'ledger_invariant','pending','[live] SKHY-USDT-SWAP ord=None: pretrade_ledger_autoheal_blocked:backlog')]
            c.executemany('INSERT INTO repair_queue VALUES(?,?,?,?)',values);c.commit();c.close()
            self.assertEqual([1],heal._pending_profile_ledger_blocks(p,'live'))
    def fixture(self):
        values=push_fixtures.PartialPushTests().fixture()
        cycle,live,report,monitor=json.loads(json.dumps(values).replace('2026-09-08','2026-09-13').replace('T12:','T18:').replace(' 12:',' 18:'))
        for name in ['business_attestation_pre_archive','business_attestation_pre_send']:
            report['steps'][name].update(decision='hold',n_orders=0,trade_count=0)
        report['steps']['build'].update(action='HOLD',n_trades=0)
        report.update(natural_production_evidence=True,execution_context='production')
        return cycle,live,report,monitor
    def test_delivered_zero_order_failure_report_is_not_a_second_push_failure(self):
        cycle,live,report,monitor=self.fixture()
        self.assertTrue(stage._strict_business_error_push_report(cycle,report,live))
        sla=stage.build_complete_cycle_sla(cycle,monitor,live_status=live)
        self.assertIsNone(stage._forward_post_push_failure(cycle,'full',monitor,sla,live,push_report=report))
        self.assertEqual('failed',live['status']);self.assertFalse(sla['strict_cycle_pass'])
    def test_delivery_or_evidence_uncertainty_still_fails(self):
        for field,value in [('send_status','uncertain_delivery'),('execution_context','probe'),('natural_production_evidence',False)]:
            cycle,live,report,monitor=self.fixture();report[field]=value
            self.assertFalse(stage._strict_business_error_push_report(cycle,report,live))
        cycle,live,report,monitor=self.fixture();report['steps']['business_attestation_pre_send']['trade_count']=1
        self.assertFalse(stage._strict_business_error_push_report(cycle,report,live))
    def test_zero_fill_adjust_report_also_keeps_delivery_separate(self):
        cycle,live,report,monitor=self.fixture();report['steps']['build']['action']='ADJUST'
        self.assertTrue(stage._strict_business_error_push_report(cycle,report,live))
        report['steps']['build']['action']='OPEN_LONG'
        self.assertFalse(stage._strict_business_error_push_report(cycle,report,live))
    def test_combined_live_alert_contains_barrier_and_p0_without_extra_send(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'live.json';p.write_text(json.dumps({'report_reconcile_barrier':{'required':True,'report_safe':False,'status':'p0_blocked','rc':4,'p0':True,'findings_count':1,'healed_count':0}}),encoding='utf-8')
            with mock.patch.object(stage,'STATUS_DIR',Path(d)),mock.patch.object(stage.subprocess,'run',return_value=mock.Mock(returncode=0)) as send,mock.patch.dict('os.environ',{},clear=True):
                value=stage._send_failure_alert('live',CYCLE,86,p,{'failure_kind':'business_verification_error'})
                self.assertTrue(value['delivered']);self.assertEqual(1,send.call_count)
                body=next(Path(d).glob('alert-*.txt')).read_text(encoding='utf-8')
                self.assertIn('[P0]',body);self.assertIn('同轮账实核验',body)
