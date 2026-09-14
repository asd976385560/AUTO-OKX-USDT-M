import copy,unittest
from contextlib import ExitStack
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from core import verified_protection_cleanup as h
from core import order_executor as oe

S='LINK-USDT-SWAP'
def row(oid,size,price='8.25',created='1'):
    return {'algoId':oid,'instId':S,'posSide':'long','side':'sell','reduceOnly':'true','state':'live',
            'sz':str(size),'slTriggerPx':price,'slTriggerPxType':'mark','tpTriggerPx':'','cTime':created,'ordType':'conditional'}

class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.rows=[row('KEEP',20),row('OLD',8,created='2')]
        self.pos={'instId':S,'posSide':'long','pos':'20','posId':'POS','cTime':'123','markPx':'8.7'}
        self.expected={'sz':20,'posId':'POS','cTime':'123'}
        self.calls=[];self.mode='success';self.nread=0
    def read_orders(self,_):self.nread+=1;return {'ok':True,'data':copy.deepcopy(self.rows)}
    def read_positions(self,_):return {'ok':True,'data':[copy.deepcopy(self.pos)]}
    def cancel(self,oid,_):
        self.calls.append(oid)
        if self.mode=='permanent':return {'ok':False,'error':'permission denied'}
        if self.mode=='ack_without_settlement':return {'ok':True}
        if self.mode=='transient' and len(self.calls)==1:return {'ok':False,'error':'network timeout'}
        self.rows=[r for r in self.rows if r['algoId']!=oid]
        if self.mode=='lost_ack':return {'ok':False,'error':'network timeout'}
        return {'ok':True}
    def run_cleanup(self,**kw):
        return h.cleanup_superseded(S,'long',[row('OLD',8,created='2')],survivor_id='KEEP',expected_position=self.expected,
            expected_sl=8.25,read_orders=self.read_orders,read_positions=self.read_positions,cancel_order=self.cancel,sleep=lambda _:None,**kw)
    def test_success_preserves_full_survivor_and_exact_target(self):
        result=self.run_cleanup();self.assertTrue(result['ok']);self.assertEqual(self.calls,['OLD'])
        self.assertEqual([r['algoId'] for r in self.rows],['KEEP']);self.assertEqual(self.rows[0]['sz'],'20')
    def test_transport_retry_has_fresh_proof_and_is_bounded(self):
        self.mode='transient';result=self.run_cleanup()
        self.assertTrue(result['ok']);self.assertEqual(self.calls,['OLD','OLD'])
        self.assertTrue(all(x['fresh_survivor_and_epoch_verified'] for x in result['attempts']))
    def test_lost_ack_but_absent_does_not_retry(self):
        self.mode='lost_ack';self.assertTrue(self.run_cleanup()['ok']);self.assertEqual(self.calls,['OLD'])
    def test_permanent_or_unsettled_ack_not_resent(self):
        for mode in ('permanent','ack_without_settlement'):
            self.mode=mode;self.calls=[];self.assertFalse(self.run_cleanup()['ok']);self.assertEqual(self.calls,['OLD'])
    def test_missing_underfilled_or_wrong_price_survivor_blocks(self):
        for field,value in [('sz','19'),('slTriggerPx','8.4'),('slTriggerPxType','last'),('reduceOnly','false')]:
            original=self.rows[0][field];self.rows[0][field]=value
            self.assertFalse(self.run_cleanup()['ok']);self.assertEqual(self.calls,[])
            self.rows[0][field]=original
    def test_epoch_change_or_flat_blocks(self):
        for field,value in [('posId','NEW'),('cTime','456'),('pos','0'),('pos','21')]:
            original=self.pos[field];self.pos[field]=value
            self.assertFalse(self.run_cleanup()['ok']);self.assertEqual(self.calls,[])
            self.pos[field]=original
    def test_new_target_identity_cannot_be_cancelled(self):
        self.rows[1]['slTriggerPx']='8.2'
        self.assertFalse(self.run_cleanup()['ok']);self.assertEqual(self.calls,[])
    def test_survivor_disappears_after_failure_blocks_retry(self):
        def cancel(oid,_):
            self.calls.append(oid);self.rows=[x for x in self.rows if x['algoId']!='KEEP']
            return {'ok':False,'error':'network timeout'}
        self.cancel=cancel
        self.assertFalse(self.run_cleanup()['ok']);self.assertEqual(self.calls,['OLD'])
    def test_budget_and_read_failure_do_not_cancel(self):
        self.assertFalse(self.run_cleanup(budget_seconds=0)['ok'])
        self.read_orders=lambda _: {'ok':False,'error':'timeout'}
        self.assertFalse(self.run_cleanup()['ok']);self.assertEqual(self.calls,[])
    def test_optional_tp_requires_preserved_oco_leg(self):
        result=h.cleanup_superseded(S,'long',[row('OLD',8,created='2')],survivor_id='KEEP',expected_position=self.expected,
            expected_sl=8.25,expected_tp=9,read_orders=self.read_orders,read_positions=self.read_positions,cancel_order=self.cancel,sleep=lambda _:None)
        self.assertFalse(result['ok']);self.assertEqual(self.calls,[])

class ExecutorIntegrationTests(unittest.TestCase):
    def test_actual_post_add_consolidation_recovers_one_transient_cancel(self):
        from test_adjust_protection import _Harness,_live_sl
        old=_live_sl('KEEP',px=8.25,sz=12);extra=_live_sl('OLD',px=8.1,sz=8);extra['cTime']=2.0
        exchange=_Harness([old,extra],full_sz=20)
        writes=[]
        def call(*args,**kw):
            if args[:3]==('swap','algo','orders'):
                return {'ok':True,'data':[{**r,'ordType':'conditional','slTriggerPxType':'mark'} for r in exchange.rows]}
            if args[:2]==('account','positions'):
                return {'ok':True,'data':[{'instId':S,'posSide':'long','pos':'20','posId':'POS-1','cTime':'1000','markPx':'8.7'}]}
            if args[:3]==('swap','algo','cancel'):
                oid=args[args.index('--algoId')+1];writes.append(oid)
                if len(writes)==1:return {'ok':False,'error':'network timeout'}
                exchange.rows=[r for r in exchange.rows if r['algoId']!=oid]
                return {'ok':True}
            raise AssertionError('unexpected exchange command: '+str(args))
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):return datetime(2026,9,13,22,50,tzinfo=timezone(timedelta(hours=8))).astimezone(tz)
        with ExitStack() as stack:
            exchange.apply(stack);stack.enter_context(patch.object(oe.ox,'_call',side_effect=call));stack.enter_context(patch.object(oe,'datetime',Frozen))
            result=oe.adjust_protection(S,'live',pos_side='long',cycle_id='2026-09-13T22:45',reason_code='post_add_resize',
                resize_to_full_position=True,consolidate_extra_sl=True,receipt_context={'cycle_id':'2026-09-13T22:45','status':'ok','decision_protocol':'minimal_decision_v2','reasoning':'isolated protection cleanup contract test'})
        self.assertTrue(result['ok'],result);self.assertEqual(writes,['OLD','OLD'])
        self.assertEqual(result['protection_state']['live_sl_count'],1)
        self.assertTrue(result['protection_cleanup'][0]['ok'])
        self.assertEqual(exchange.amend_calls[0]['sz'],20)

if __name__=='__main__':unittest.main()
