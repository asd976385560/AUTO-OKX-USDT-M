import copy
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core import order_executor as oe

class TickGridTests(unittest.TestCase):
    def test_open_submits_and_records_the_aligned_sl(self):
        sys.path.insert(0,str(ROOT/'tests'))
        import test_add_protection_sync as fixture
        calls={}
        with mock.patch.object(fixture.oe,'protection_price_grid_enabled',return_value=True),mock.patch.object(fixture.oe,'fetch_protection_price_tick',return_value={'tick_sz':'0.001','source':'fixture'}),mock.patch.object(fixture.oe,'_cycle_side_effect_reject',return_value=None):
            result,_,_,_=fixture._run_open(0,io_mocks=calls)
        self.assertTrue(result['ok'],result)
        self.assertEqual(calls['place_market_open'].call_args.kwargs['sl_trigger_px'],0.068)
        self.assertEqual(result['trades'][0]['sl_trigger_px'],0.068)
        self.assertEqual(result['protection_price_normalization']['requested_sl'],0.0677)

    def test_csop_short_aligns_inward_and_existing_sl_verifies(self):
        aligned=oe.normalize_protection_prices('short',5.406,5.088,'0.01')
        self.assertEqual((aligned['sl'],aligned['tp']),(5.4,5.09))
        algo={'instId':'CSOPSKHYNIX2L-USDT-SWAP','algoId':'a','state':'live','side':'buy','posSide':'short',
              'reduceOnly':'true','sz':'2','slTriggerPx':'5.4','cTime':'1789256434490'}
        with mock.patch.object(oe.ox,'get_algo_orders',return_value=[algo]):
            old=oe._verify_sl_placed(algo['instId'],'short','live',5.406,retries=1,expected_sz=2,since_ms=1789256423024)
            new=oe._verify_sl_placed(algo['instId'],'short','live',aligned['sl'],retries=1,expected_sz=2,since_ms=1789256423024)
        self.assertFalse(old['verified']);self.assertEqual(old['found'][0]['errors'],['slTriggerPx'])
        self.assertTrue(new['verified'])

    def test_long_rounding_cannot_widen_stop_or_push_tp_further(self):
        result=oe.normalize_protection_prices('long','5.194','5.512','0.01')
        self.assertEqual((result['sl'],result['tp']),(5.2,5.51))

    def test_non_power_of_ten_tick_and_already_aligned_values(self):
        result=oe.normalize_protection_prices('long','9.011','11.019','0.025')
        self.assertEqual((result['sl'],result['tp']),(9.025,11.0))
        same=oe.normalize_protection_prices('short','5.4','5.09','0.01')
        self.assertFalse(same['changed'])

    def test_invalid_prices_and_ticks_rejected(self):
        for tick in ('0','-0.01','NaN','Infinity','invalid'):
            with self.subTest(tick=tick),self.assertRaises(ValueError):oe.normalize_protection_prices('long',1,2,tick)
        for px in ('NaN','Infinity','0','-1'):
            with self.subTest(px=px),self.assertRaises(ValueError):oe.normalize_protection_prices('short',px,None,'0.01')

    def test_aligned_price_must_remain_on_correct_side(self):
        result=oe.normalize_protection_prices('long','5.295',None,'0.01')
        self.assertTrue(oe._aligned_price_direction_errors('long',5.3,result['sl'],None))
        self.assertEqual(oe._aligned_price_direction_errors('short',5.3,5.4,5.09),[])

    def test_official_tick_requires_exact_live_instrument_and_is_bounded(self):
        oe._PROTECTION_TICK_CACHE.clear()
        row={'instId':'TEST-USDT-SWAP','instType':'SWAP','state':'live','tickSz':'0.01'}
        with mock.patch.object(oe.ox,'_call',return_value={'ok':True,'data':[row]}) as call:
            first=oe.fetch_protection_price_tick('TEST-USDT-SWAP','live')
            second=oe.fetch_protection_price_tick('TEST-USDT-SWAP','live')
        self.assertEqual(first,second);self.assertEqual(call.call_count,1)
        self.assertEqual(call.call_args.kwargs['timeout_sec'],5)
        self.assertEqual(call.call_args.kwargs['retries'],1)
        oe._PROTECTION_TICK_CACHE.clear()
        with mock.patch.object(oe.ox,'_call',return_value={'ok':True,'data':[{**row,'instId':'WRONG'}]}),self.assertRaises(ValueError):
            oe.fetch_protection_price_tick('TEST-USDT-SWAP','live')

    def test_forward_boundary_keeps_historical_behavior(self):
        self.assertFalse(oe.protection_price_grid_enabled('2026-09-13T11:15'))
        self.assertTrue(oe.protection_price_grid_enabled('2026-09-13T11:30'))
        self.assertFalse(oe.protection_price_grid_enabled('CYCLE-TEST'))

class UnwindContextTests(unittest.TestCase):
    def context(self):
        return {'cycle_id':'2026-09-13T12:00','mode':'live','status':'ok','regime':'range',
                'decision_protocol':'minimal_decision_v2','reasoning':'verified continuation','position_reviews':[],
                'facts_hash':'a'*64,'plan_sha256':'b'*64,'actor_attestation':{'proof':'retained'},
                'open_execution_package':{'contract':'open_execution_package_v1','entry':5.3,'stop':5.406,'target':5.088,'exit_mode':'fixed_tp'}}
    def test_only_open_package_is_removed_and_original_remains_immutable(self):
        original=self.context();before=copy.deepcopy(original)
        cleaned=oe._non_open_continuation_context(original)
        self.assertEqual(original,before)
        self.assertNotIn('open_execution_package',cleaned)
        self.assertEqual(cleaned,{k:v for k,v in original.items() if k!='open_execution_package'})
        self.assertIn('非 OPEN/ADD context 禁止携带 open_execution_package',oe.validate_receipt_context(original,cycle_id=original['cycle_id']))
        self.assertEqual(oe.validate_receipt_context(cleaned,cycle_id=original['cycle_id']),[])

    def test_cleaned_unwind_reaches_reduce_only_boundary_without_an_order(self):
        class BoundaryReached(Exception):pass
        context=oe._non_open_continuation_context(self.context())
        position={'symbol':'CSOPSKHYNIX2L-USDT-SWAP','side':'short','sz':2}
        with mock.patch.object(oe.ox,'is_dryrun',return_value=False),mock.patch.object(oe,'fetch_open_positions',return_value=[position]),mock.patch.object(oe.ox,'place_reduce_only_market',side_effect=BoundaryReached) as send:
            with self.assertRaises(BoundaryReached):
                oe.close_position(position['symbol'],'live',pos_side='short',cycle_id=context['cycle_id'],_unwind=True,receipt_context=context)
        self.assertEqual(send.call_args.args[:4],(position['symbol'],'short',2.0,'live'))

if __name__=='__main__':unittest.main()
