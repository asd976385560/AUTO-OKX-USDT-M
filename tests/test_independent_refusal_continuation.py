from contextlib import ExitStack,closing
import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from core import independent_refusal as policy
from collectors import trades_writer as writer
from scripts import live_position_action_runner as runner
import test_runner_partial_final_merge as data

CYCLE='2026-09-14T11:15'
def failure():
    action={'action':'OPEN','symbol':'LOW-USDT-SWAP','side':'long','target_stop_risk_pct_equity':0.0005,'lev':5}
    result={'ok':False,'action_taken':'REJECT','symbol':action['symbol'],'side':'long','trades':[],'p0':False,
            'reject_reason':'deterministic_sizing_failed','reject_detail':'target_risk_below_minimum_order',
            'sizing_intent':{'ok':False,'error':'target_risk_below_minimum_order','target_risk_usdt':0.33,'minimum_risk_usdt':0.36,'minimum_sz':2}}
    return {'request':action,'result':result,'problem':'OPEN LOW-USDT-SWAP/long: deterministic_sizing_failed',
            'continuation':{'allowed':True,'scope':'other_symbols_only','same_action_retry':False,'reason':'verified_no_order_minimum_risk_refusal'}}
def receipt(stage='interim'):
    x=data.runner_receipt([data.runner_trade('BTC-USDT-SWAP','111')],stage,cycle_id=CYCLE,batch_status='partial',batch_ok=False,
        errors=[failure()['problem']],position_action_failures=[failure()])
    policy.attach(x,x['position_action_failures']);return x

class IndependentRefusalTests(unittest.TestCase):
    def test_minimum_risk_refusal_can_continue_only_other_symbols(self):
        row=failure();self.assertTrue(policy.can_continue(row['result'],row['request'],[{'symbol':'NEXT'}],CYCLE))
        self.assertFalse(policy.can_continue(row['result'],row['request'],[row['request']],CYCLE))
        self.assertFalse(policy.can_continue(row['result'],row['request'],[],'2026-09-14T10:45'))
    def test_unsafe_or_unknown_rejections_do_not_continue(self):
        for change in ({'p0':True},{'ord_id':'1'},{'exchange_side_effect_uncertain':True},{'trades':[{}]},{'reject_reason':'execution_intent_blocked'},{'reject_detail':'invalid_quote'}):
            row=failure();row['result'].update(change);self.assertFalse(policy.can_continue(row['result'],row['request'],[],CYCLE))
        row=failure();row['result']['sizing_intent']['minimum_risk_usdt']=0.1;self.assertFalse(policy.can_continue(row['result'],row['request'],[],CYCLE))
    def test_writer_accepts_progress_but_keeps_failure_in_final(self):
        first=receipt();later=receipt();final=receipt('failed_final');final['runner_in_progress']=False
        self.assertEqual('interim',writer._runner_same_plan_progression_kind(first,later,CYCLE))
        self.assertEqual('partial_finalization',writer._runner_same_plan_progression_kind(later,final,CYCLE))
        final.pop(policy.KEY);final['position_action_failures']=[]
        self.assertIsNone(writer._runner_same_plan_progression_kind(first,final,CYCLE))
    def test_proof_cannot_promote_known_refusal_to_business_success(self):
        first=receipt();final=receipt('completed_final');final['batch_status']='completed';final['batch_ok']=True
        self.assertIsNone(writer._runner_same_plan_progression_kind(first,final,CYCLE))
    def test_compaction_preserves_refusal_authority(self):
        first=receipt();first['reasoning']='x'*30000;first['position_action_failures'][0]['result']['context']='y'*30000
        policy.attach(first,first['position_action_failures'])
        compact=json.loads(writer._bounded_json(first,1024,'fixture'))
        self.assertTrue(compact['raw_structurally_truncated'])
        self.assertEqual(first[policy.KEY],compact[policy.KEY])
        self.assertEqual('partial_finalization',writer._runner_same_plan_progression_kind(compact,{**first,'runner_in_progress':False},CYCLE))
        compact_again=json.loads(writer._bounded_json(compact,512,'repeated fixture'))
        self.assertEqual(first[policy.KEY],policy.validated_proofs(compact_again,CYCLE))
    def test_interim_persists_refusal_without_losing_confirmed_fills(self):
        with tempfile.TemporaryDirectory() as td,patch.object(runner,'_receipt_validation_errors',return_value=[]),patch.object(runner.tw,'commit_receipt',return_value={'ok':True}) as commit:
            context={'cycle_id':CYCLE,'profile':'live','mode':'live','status':'ok','decision_protocol':'minimal_decision_v2','reasoning':'fixture'}
            trade=data.runner_trade('BTC-USDT-SWAP','111')
            success={'request':{'action':'OPEN','symbol':'BTC-USDT-SWAP','side':'long'},'result':{'ok':True,'action_taken':'OPEN_LONG','trades':[trade],'p0':False}}
            interim,_=runner._commit_interim_successes(context,{'facts_hash':'a'*64},plan_hash='c'*64,successes=[success],failures=[failure()],receipt_file=Path(td)/'receipt.json',db_root=Path(td))
        self.assertFalse(interim['batch_ok']);self.assertTrue(interim['runner_in_progress']);self.assertEqual(1,len(interim['trades']))
        self.assertEqual(1,len(commit.call_args.args[0]['position_action_failures']));self.assertTrue(interim[policy.KEY])
    def test_actual_runner_keeps_failed_action_and_executes_independent_actions(self):
        refused=failure();actions=[refused['request'],{'action':'OPEN','symbol':'BTC-USDT-SWAP','side':'long'}, {'action':'OPEN','symbol':'ETH-USDT-SWAP','side':'long'}]
        calls=[];writes=[]
        def execute(action,**kwargs):
            calls.append(action['symbol'])
            if action['symbol']=='LOW-USDT-SWAP':return copy.deepcopy(refused['result'])
            trade=data.runner_trade(action['symbol'],str(110+len(calls)))
            return {'ok':True,'p0':False,'action_taken':'OPEN_LONG','trades':[trade]}
        def commit(payload,*args,**kwargs):writes.append(copy.deepcopy(payload));return {'ok':True}
        with tempfile.TemporaryDirectory() as td,ExitStack() as stack:
            context={'cycle_id':CYCLE,'profile':'live','status':'ok','decision_protocol':'minimal_decision_v2','reasoning':'isolated independent order plan'}
            for name,value in [('preflight_plan',(context,actions)),('_validate_position_exit_evidence',None),('_require_same_stage_authority',None),('_receipt_validation_errors',[])]:
                stack.enter_context(patch.object(runner,name,return_value=value))
            stack.enter_context(patch.object(runner,'_call_executor',side_effect=execute))
            stack.enter_context(patch.object(runner,'_result_problem',side_effect=lambda result,*args:result.get('reject_reason') if not result.get('ok') else None))
            stack.enter_context(patch.object(runner.tw,'commit_receipt',side_effect=commit))
            result=runner._execute_position_plan_locked({}, {'facts_hash':'a'*64,'status':'ok'},cycle_id=CYCLE,db_root=Path(td),receipt_file=Path(td)/'receipt.json',nudge=False,plan_sha256='b'*64)
        self.assertEqual(calls,['LOW-USDT-SWAP','BTC-USDT-SWAP','ETH-USDT-SWAP'])
        self.assertFalse(result['ok']);self.assertTrue(result['committed'])
        self.assertEqual([len(x['trades']) for x in writes],[1,2])
        self.assertTrue(all(len(x['position_action_failures'])==1 and x['batch_ok'] is False for x in writes))
    def test_real_database_progression_cannot_drop_the_prior_refusal(self):
        fixture=data.RunnerPartialFinalMergeTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        def dated_trade(symbol,oid):
            trade=data.runner_trade(symbol,oid);trade['fill_ts']='2026-09-14 11:16:00';return trade
        first=receipt();first['trades']=[dated_trade('BTC-USDT-SWAP','111')]
        first['position_action_failures'][0]['result']['context']='x'*50000;policy.attach(first,first['position_action_failures'])
        later=copy.deepcopy(first);later['trades'].append(dated_trade('ETH-USDT-SWAP','222'));later['n_orders']=2
        final=copy.deepcopy(later);final['runner_in_progress']=False
        for payload in (first,later,final):
            result=writer.write_trades(payload,fixture.db);self.assertTrue(result.get('ok'),result);self.assertFalse(result.get('refused'),result)
        masked=copy.deepcopy(final);masked.pop(policy.KEY);masked['position_action_failures']=[];masked['errors']=[]
        masked.update(batch_ok=True,batch_status='completed',business_terminal={'schema_version':1,'cycle_id':CYCLE,'status':'completed','completed_at_cst':'2026-09-14 11:20:00'})
        writer.write_trades(masked,fixture.db)
        with closing(sqlite3.connect(fixture.db)) as c:
            self.assertEqual(2,c.execute('SELECT COUNT(*) FROM trades').fetchone()[0])
            saved=json.loads(c.execute('SELECT raw FROM trade_cycles WHERE cycle_id=?',(CYCLE,)).fetchone()[0]);self.assertFalse(saved['batch_ok']);self.assertTrue(saved[policy.KEY])
        # A real exchange-close maintenance merge must retain the refusal too.
        from datetime import datetime,timezone,timedelta
        stamp=int(datetime(2026,9,14,11,18,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000)
        fills=[{'ordId':'333','fillTime':str(stamp),'fillPx':'9.5','fillSz':'1','fillPnl':'-0.5','tradeId':'F333','execType':'T'}]
        with closing(fixture._ro()) as c:
            healed=data.reconcile.apply_reconcile(fixture.db,'live','BTC-USDT-SWAP','long',1,data.reconcile.group_by_ord(fills),c,open_lev=5)
        self.assertTrue(healed['writer']['ok'],healed)
        with closing(sqlite3.connect(fixture.db)) as c:
            self.assertEqual(3,c.execute('SELECT COUNT(*) FROM trades').fetchone()[0])
            saved=json.loads(c.execute('SELECT raw FROM trade_cycles WHERE cycle_id=?',(CYCLE,)).fetchone()[0]);self.assertFalse(saved['batch_ok']);self.assertTrue(saved[policy.KEY])
if __name__=='__main__':unittest.main()
