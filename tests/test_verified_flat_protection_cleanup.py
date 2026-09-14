

def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

import copy,json,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest import mock
from core import flat_protection_cleanup as flat
from core import order_executor as oe
from core.lib import _okxorder as ox

SYMBOL='TEST-USDT-SWAP'
NOW=1800000000000
def order(oid='111',side='short',ctime=NOW-120000):
    return {'algoId':oid,'instId':SYMBOL,'state':'live','ordType':'conditional','posSide':side,
            'side':'buy' if side=='short' else 'sell','reduceOnly':'true','sz':'7','cTime':str(ctime),
            'slTriggerPx':'','tpTriggerPx':'10'}

class FlatCleanupTests(unittest.TestCase):
    def run_case(self, *, items=None, action=None, positions=None, enabled=True, budget=45):
        state={'orders':copy.deepcopy(items if items is not None else [order()]),'positions':positions or [],'time':0.0,'calls':[]}
        def read_orders(b):return {'ok':True,'data':copy.deepcopy(state['orders'])}
        def read_positions(b):return {'ok':True,'data':copy.deepcopy(state['positions'])}
        def cancel(oid,b):
            state['calls'].append(oid)
            if action:return action(state,oid)
            state['orders']=[x for x in state['orders'] if x['algoId']!=oid]
            return {'ok':True,'sCode':'0','data':[{'algoId':oid,'sCode':'0'}]}
        result=flat.cleanup_flat_side(SYMBOL,'short',read_orders=read_orders,read_positions=read_positions,cancel_order=cancel,
            now_ms=NOW,min_age_ms=60000,enabled=enabled,budget_seconds=budget,
            clock=lambda:state['time'],sleep=lambda seconds:state.update(time=state['time']+seconds))
        return result,state
    def test_confirmed_flat_side_is_cleared_without_touching_other_side(self):
        r,s=self.run_case(items=[order(),order('222','long')]);self.assertTrue(r['ok'])
        self.assertEqual(['111'],s['calls']);self.assertEqual(['222'],[x['algoId'] for x in s['orders']])
    def test_transport_error_after_effect_is_read_back_without_retry(self):
        def action(s,oid):s['orders']=[];return {'ok':False,'sCode':None,'error':'network reset'}
        r,s=self.run_case(action=action);self.assertTrue(r['ok']);self.assertEqual(1,len(s['calls']))
    def test_transport_failure_retries_only_after_live_target_and_flat_proof(self):
        def action(s,oid):
            if len(s['calls'])==1:return {'ok':False,'sCode':None,'error':'network reset'}
            s['orders']=[];return {'ok':True,'sCode':'0'}
        r,s=self.run_case(action=action);self.assertTrue(r['ok']);self.assertEqual(['111','111'],s['calls'])
        self.assertTrue(all(x['fresh_pending_and_flat_verified'] for x in r['attempts']))
    def test_reopened_side_forbids_retry(self):
        def action(s,oid):
            s['positions']=[{'instId':SYMBOL,'posSide':'short','pos':'5'}]
            return {'ok':False,'sCode':None,'error':'network reset'}
        r,s=self.run_case(action=action);self.assertFalse(r['ok']);self.assertEqual(1,len(s['calls']))
        self.assertIn('not_flat',r['read_error'])
    def test_in_place_order_change_forbids_retry(self):
        def action(s,oid):s['orders'][0]['tpTriggerPx']='9';return {'ok':False,'sCode':None,'error':'network reset'}
        r,s=self.run_case(action=action);self.assertFalse(r['ok']);self.assertEqual(1,len(s['calls']))
        self.assertIn('target_order_changed',r['read_error'])
    def test_permanent_error_is_not_retried_and_remains_blocked(self):
        r,s=self.run_case(action=lambda s,oid:{'ok':False,'sCode':'50100','sMsg':'API key permission denied'})
        self.assertFalse(r['ok']);self.assertEqual(1,len(s['calls']));self.assertEqual(['111'],r['remaining'])
    def test_acknowledged_but_still_live_is_not_blindly_resubmitted(self):
        r,s=self.run_case(action=lambda s,oid:{'ok':True,'sCode':'0'})
        self.assertFalse(r['ok']);self.assertEqual(1,len(s['calls']))
    def test_recent_orders_and_disabled_cleanup_block_without_cancelling(self):
        for kwargs in [{'items':[order(ctime=NOW-1000)]},{'enabled':False}]:
            r,s=self.run_case(**kwargs);self.assertFalse(r['ok']);self.assertEqual([],s['calls'])
        r,s=self.run_case(items=[],enabled=False);self.assertTrue(r['ok']);self.assertEqual([],s['calls'])
    def test_full_page_malformed_identity_and_position_are_not_empty_proofs(self):
        cases=[{'items':[order(str(i)) for i in range(100)]},{'items':[{'algoId':'111'}]},
               {'positions':[{'instId':SYMBOL,'posSide':'short'}]}]
        for kwargs in cases:
            r,s=self.run_case(**kwargs);self.assertFalse(r['ok']);self.assertEqual([],s['calls'])
    def test_budget_does_not_allow_a_write_without_readback_room(self):
        r,s=self.run_case(budget=9);self.assertFalse(r['ok']);self.assertEqual([],s['calls'])
    def test_malformed_ack_still_allows_independent_absence_verification(self):
        def action(s,oid):s['orders']=[];return None
        r,s=self.run_case(action=action);self.assertTrue(r['ok']);self.assertEqual(1,len(s['calls']))

class ResponseNormalizationTests(unittest.TestCase):
    def test_bare_array_row_failure_is_not_success(self):
        r=ox._normalize([{'algoId':'111','sCode':'51400','sMsg':'rejected'}])
        self.assertFalse(r['ok']);self.assertEqual('51400',r['sCode']);self.assertEqual('rejected',r['sMsg'])
    def test_mixed_rows_keep_success_evidence_but_not_global_success(self):
        rows=[{'algoId':'111','sCode':'0'},{'algoId':'222','sCode':'51400','sMsg':'rejected'}]
        for payload in [rows,{'code':'0','data':rows}]:
            r=ox._normalize(payload);self.assertFalse(r['ok']);self.assertEqual(rows,r['data'])
    def test_empty_reads_and_valid_success_remain_success(self):
        self.assertTrue(ox._normalize([])['ok']);self.assertTrue(ox._normalize([{'algoId':'111','sCode':'0'}])['ok'])

class IntegrationBoundaryTests(unittest.TestCase):
    def test_registered_boundary_is_forward_only(self):
        self.assertFalse(oe._verified_cleanup_enabled('2026-09-13T19:45'))
        self.assertTrue(oe._verified_cleanup_enabled('2026-09-13T20:00'))
    def test_callback_adapters_use_bounded_reads_and_one_shot_writes(self):
        fixed=datetime(2026,9,13,20,1,tzinfo=timezone(oe.timedelta(hours=8)))
        fake_dt=mock.Mock();fake_dt.now.return_value=fixed;fake_dt.strptime.side_effect=datetime.strptime
        with mock.patch.object(oe,'datetime',fake_dt),mock.patch.object(oe,'_stale_protection_cleanup_disabled',return_value=False),mock.patch.object(oe,'_enqueue_repair'),mock.patch.object(flat,'cleanup_flat_side') as cleanup,mock.patch.object(oe.ox,'_call',return_value={'ok':True,'data':[]}) as call:
            def inspect(symbol,side,**kwargs):
                kwargs['read_orders'](12);self.assertEqual(1,call.call_args.kwargs['retries'])
                kwargs['read_positions'](12);self.assertEqual(1,call.call_args.kwargs['retries'])
                kwargs['cancel_order']('111',12);self.assertEqual(0,call.call_args.kwargs['retries'])
                return {'ok':False,'remaining':['111'],'read_error':'retained'}
            cleanup.side_effect=inspect
            r=oe._verified_flat_protection_cleanup(SYMBOL,'short','live',Path(_public_project_path('db')),'2026-09-13T20:00')
            self.assertFalse(r['ok'])
    def run_open_until_cleanup(self,cleanup_result,expected=False,balance=None):
        from contextlib import ExitStack
        with ExitStack() as stack:
            root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(mock.patch.object(oe.ox,'is_dryrun',return_value=False))
            stack.enter_context(mock.patch.object(oe,'validate_receipt_context',return_value=[]))
            stack.enter_context(mock.patch.object(oe,'_cycle_side_effect_reject',return_value=None))
            stack.enter_context(mock.patch.object(oe.actor_att,'timeline_state',return_value={'available':True,'handoff_detected':False}))
            stack.enter_context(mock.patch.object(oe.ei,'reserve',return_value={'status':'reserved','fingerprint':'a'*64}))
            stack.enter_context(mock.patch.object(oe.ei,'mark_failed_clean'))
            stack.enter_context(mock.patch.object(oe.thresholds,'open_multitimeframe_contract_required',return_value=False))
            events=[]
            def precheck(*a,**k):events.append('cleanup');return cleanup_result
            cleanup=stack.enter_context(mock.patch.object(oe,'_preopen_protection_precheck',side_effect=precheck))
            def get_balance(*a,**k):events.append('balance');return balance or {'ok':False,'error':'fixture stops before risk'}
            bal=stack.enter_context(mock.patch.object(oe.ox,'get_balance',side_effect=get_balance))
            mark=stack.enter_context(mock.patch.object(oe.ox,'get_mark_price'))
            place=stack.enter_context(mock.patch.object(oe.ox,'place_market_open'))
            r=oe.open_position(symbol=SYMBOL,side='long',intended_sz=1,lev=5,sl_trigger_px=98,tp_trigger_px=104,
                profile='live',cycle_id='2026-09-13T20:00',db_root=root,expected_pre_position_exists=expected,
                receipt_context={'cycle_id':'2026-09-13T20:00','open_execution_package':{'contract':'open_execution_package_v1','entry':100,'stop':98,'target':104,'exit_mode':'fixed_tp'}})
            return r,events,cleanup.call_count,bal.call_count,mark.call_count,place.call_count
    def test_unverified_old_protection_blocks_before_price_risk_or_new_order(self):
        r,events,cleanup,balance,mark,place=self.run_open_until_cleanup({'ok':False,'read_error':'still pending'})
        self.assertEqual('stale_protection_cleanup_unverified',r['reject_reason'])
        self.assertEqual(['cleanup'],events);self.assertEqual((balance,mark,place),(0,0,0))
    def test_cleanup_precedes_fresh_account_and_price_snapshot(self):
        r,events,cleanup,balance,mark,place=self.run_open_until_cleanup({'ok':True,'scope_verified':True,'cancel_requested':['111']})
        self.assertEqual(['cleanup','balance'],events);self.assertEqual(place,0)
    def test_missing_forward_fingerprint_cannot_bypass_cleanup(self):
        r,events,cleanup,balance,mark,place=self.run_open_until_cleanup({'ok':True},expected=None)
        self.assertEqual('pre_position_fingerprint_required',r['reject_reason']);self.assertEqual(events,[])
