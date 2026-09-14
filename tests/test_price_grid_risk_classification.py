

def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

import tempfile,unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
from core import order_executor as oe
from core import risk_validator as rv
from scripts import live_position_action_runner as runner

CYCLE='2026-09-13T20:45'
class PriceGridRiskClassificationTests(unittest.TestCase):
    def test_sl_crossing_defers_to_formal_risk_while_other_checks_remain(self):
        self.assertEqual([],oe.pretrade_price_grid_errors(CYCLE,'long',99,100,104))
        self.assertIn('normalized SL is at or across current mark',oe.pretrade_price_grid_errors('2026-09-13T20:30','long',99,100,104))
        self.assertIn('normalized TP is at or across current mark',oe.pretrade_price_grid_errors(CYCLE,'long',105,100,104))
        self.assertTrue(oe.pretrade_price_grid_errors(CYCLE,'long',None,100,104))
        self.assertIn('normalized SL is at or across current mark',oe._aligned_price_direction_errors('long',99,100,104))
    def test_real_formal_risk_rejects_crossed_sl_without_approving(self):
        result=rv.validate(symbol='TEST-USDT-SWAP',side='long',intended_sz=1,lev=5,mark_px=99,ct_val=1,lot_sz=1,
            equity=1000,open_positions=[],sl_trigger_px=100,profile='live',available_margin=900,account_imr=0)
        self.assertFalse(result['approved']);self.assertEqual('sl_direction_invalid',result['reject_reason'])
    def run_action(self,cycle,grid_error=False):
        action={'action':'OPEN','symbol':'TEST-USDT-SWAP','side':'long','reasoning':'fixture','lev':5,
            'target_stop_risk_pct_equity':0.01,'_sl_trigger_px':100,'_tp_trigger_px':104,
            '_expected_pre_position_sz':0,'_expected_pre_position_pos_id':None,'_expected_pre_position_c_time':None}
        risk=rv.validate(symbol=action['symbol'],side='long',intended_sz=1,lev=5,mark_px=99,ct_val=1,lot_sz=1,equity=1000,
            open_positions=[],sl_trigger_px=100,profile='live',available_margin=900,account_imr=0)
        response={'ok':False,'action_taken':'REJECT','p0':False,'trades':[], 'reject_reason':risk['reject_reason'],
            'risk':risk,'position_reconciliation':{'ok':True}}
        with mock.patch.object(runner,'_ensure_actor_attestation'),mock.patch.object(runner,'_action_context',return_value={}),\
             mock.patch.object(oe,'fetch_instrument_specs',return_value={'ct_val':1,'lot_sz':1,'min_sz':1}),\
             mock.patch.object(oe.ox,'get_mark_price',return_value=99),mock.patch.object(oe.ox,'get_mark_price_evidence',return_value={}),\
             mock.patch.object(oe,'aligned_protection_prices',side_effect=ValueError('instrument unavailable') if grid_error else None,return_value={'sl':100,'tp':104}),\
             mock.patch.object(oe,'open_position',return_value=response) as execute:
            result=runner._call_executor(action,context={},facts={'balance':{'totalEq':1000,'availEq':900,'account_imr':0},'positions':[]},cycle_id=cycle,db_root=Path(_public_project_path('db')))
            return result,execute.call_count,action
    def test_preview_reaches_formal_risk_and_existing_clean_refusal_contract(self):
        result,count,action=self.run_action(CYCLE)
        self.assertEqual(1,count);self.assertEqual('sl_direction_invalid',result['reject_reason'])
        self.assertTrue(runner._is_closure_clean_hard_reject(result,action,CYCLE));self.assertEqual([],result['trades'])
    def test_bad_instrument_and_historical_behavior_still_fail_early(self):
        result,count,_=self.run_action(CYCLE,True);self.assertEqual(0,count);self.assertEqual('protection_price_grid_invalid',result['reject_reason'])
        result,count,_=self.run_action('2026-09-13T20:30');self.assertEqual(0,count);self.assertEqual('protection_price_grid_invalid',result['reject_reason'])
    def test_full_executor_denies_crossed_sl_before_leverage_and_new_order(self):
        with ExitStack() as stack:
            root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            for obj,name,value in [(oe.ox,'is_dryrun',False),(oe,'validate_receipt_context',[]),
                (oe,'_cycle_side_effect_reject',None),(oe.actor_att,'timeline_state',{'available':True,'handoff_detected':False}),
                (oe.ei,'reserve',{'status':'reserved','fingerprint':'a'*64}),
                (oe.thresholds,'open_multitimeframe_contract_required',False),
                (oe,'_preopen_protection_precheck',{'ok':True,'scope_verified':True,'cancel_requested':[]}),
                (oe.ox,'get_balance',{'ok':True,'data':[{'totalEq':'1000','imr':'0','details':[{'ccy':'USDT','availEq':'900','availBal':'900','imr':'0'}]}]}),
                (oe,'fetch_open_positions',[]),(oe,'_verify_pretrade_ledger_positions',{'ok':True,'diffs':[]}),
                (oe.ox,'get_mark_price',99),(oe.ox,'get_mark_price_evidence',{}),
                (oe,'aligned_protection_prices',{'sl':100,'tp':106}),
                (oe,'fetch_instrument_specs',{'ct_val':1,'lot_sz':1,'min_sz':1})]:
                stack.enter_context(mock.patch.object(obj,name,return_value=value))
            stack.enter_context(mock.patch.object(oe.ei,'mark_failed_clean'))
            leverage=stack.enter_context(mock.patch.object(oe.ox,'set_leverage'))
            place=stack.enter_context(mock.patch.object(oe.ox,'place_market_open'))
            r=oe.open_position('TEST-USDT-SWAP','long',1,5,100,profile='live',cycle_id=CYCLE,db_root=root,
                expected_pre_position_exists=False,tp_trigger_px=106,
                receipt_context={'cycle_id':CYCLE,'open_execution_package':{'contract':'open_execution_package_v1','entry':102,'stop':100,'target':106,'exit_mode':'fixed_tp'}})
            self.assertEqual('sl_direction_invalid',r['reject_reason']);self.assertFalse(r['risk']['approved'])
            self.assertTrue(r['position_reconciliation']['ok']);leverage.assert_not_called();place.assert_not_called()
