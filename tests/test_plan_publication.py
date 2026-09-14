from datetime import datetime,timezone,timedelta
import hashlib,json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import write_position_plan as writer
import _plan_publication as publication
CYCLE='2026-09-13T12:00';SLUG=CYCLE.replace(':','-');NOW=datetime(2026,9,13,12,3,tzinfo=timezone(timedelta(hours=8)))
class PlanPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.facts=self.root/f'live_facts_{SLUG}.json';self.view=self.root/f'position_exit_view_{SLUG}.json'
        self.facts.write_text(json.dumps({'cycle_id':CYCLE,'profile':'live','facts_hash':'a'*64}))
        self.view.write_text('{}')
        handoff={'schema_version':1,'cycle_id':CYCLE,'status':'ready','detail_status':'ready','facts_file':str(self.facts),'decision_view_file':str(self.view),
                 'facts_hash':'a'*64,'facts_status':'ok','decision_view_hash':'b'*64,'position_count':0,'production_database_writes':0,'orders_placed':0}
        self.handoff=self.root/f'live_input_handoff_{SLUG}.json';self.handoff.write_text(json.dumps(handoff))
        self.draft=self.root/f'position_plan_draft_{SLUG}.json';self.plan=self.root/f'position_plan_{SLUG}.json'
        self.payload={'cycle_id':CYCLE,'receipt_context':{'cycle_id':CYCLE,'reasoning':'first line\nsecond line'},'actions':[]}
        self.draft.write_text(json.dumps(self.payload),encoding='utf-8')
    def publish(self):return writer.publish(CYCLE,tmp_root=self.root,now=NOW)
    def test_valid_plan_is_atomically_published_and_bound(self):
        result=self.publish();self.assertTrue(result['ok']);self.assertEqual(json.loads(self.plan.read_text()),self.payload)
        self.assertTrue(publication.validate(self.plan,CYCLE,hashlib.sha256(self.plan.read_bytes()).hexdigest(),'a'*64)['ok'])
        again=self.publish();self.assertEqual(again['status'],'already_published');self.assertEqual(again['attempts'],1)
    def test_original_unescaped_newline_is_rejected_before_canonical_file_exists(self):
        self.draft.write_text(json.dumps(self.payload).replace('\\n','\n'),encoding='utf-8')
        bad=self.publish();self.assertFalse(bad['ok']);self.assertTrue(bad['may_rewrite']);self.assertFalse(self.plan.exists())
        same=self.publish();self.assertEqual(same['attempts'],1);self.assertFalse(self.plan.exists())
        self.draft.write_text(json.dumps(self.payload),encoding='utf-8')
        good=self.publish();self.assertTrue(good['ok']);self.assertEqual(good['attempts'],2)
    def test_duplicate_keys_and_nonfinite_numbers_are_not_published(self):
        self.draft.write_text('{"cycle_id":"'+CYCLE+'","cycle_id":"'+CYCLE+'"}')
        first=self.publish();self.assertFalse(first['ok']);self.assertIn('duplicate',first['error'])
        self.draft.write_text('{"cycle_id":"'+CYCLE+'","x":NaN}')
        second=self.publish();self.assertFalse(second['ok']);self.assertFalse(second['may_rewrite']);self.assertFalse(self.plan.exists())
        self.draft.write_text(json.dumps(self.payload))
        with self.assertRaises(ValueError):self.publish()
    def test_active_and_terminal_runner_states_cannot_be_overwritten(self):
        self.publish();original=self.plan.read_bytes()
        self.payload['receipt_context']['reasoning']='changed';self.draft.write_text(json.dumps(self.payload))
        state=self.root/f'live_runner_state_{SLUG}.json'
        for name in ('started','executing','committed','failed','uncertain'):
            with self.subTest(state=name):
                state.write_text(json.dumps({'cycle_id':CYCLE,'state':name}))
                with self.assertRaises(ValueError):self.publish()
                self.assertEqual(self.plan.read_bytes(),original)
    def test_only_existing_preflight_retry_is_admitted(self):
        first=self.publish()
        state=self.root/f'live_runner_state_{SLUG}.json'
        state.write_text(json.dumps({'schema_version':2,'cycle_id':CYCLE,'state':'failed_preflight','preflight_attempts':1,'facts_hash':'a'*64,'plan_sha256':first['plan_sha256']}))
        self.payload['receipt_context']['reasoning']='one corrected plan';self.draft.write_text(json.dumps(self.payload))
        second=self.publish();self.assertTrue(second['ok']);self.assertEqual(second['attempts'],2)
        state.write_text(json.dumps({'schema_version':2,'cycle_id':CYCLE,'state':'failed_preflight','preflight_attempts':2,'facts_hash':'a'*64,'plan_sha256':second['plan_sha256']}))
        with self.assertRaises(ValueError):self.publish()
    def test_revoked_handoff_and_changed_facts_fail_closed(self):
        (self.root/f'live_runner_handoff_{SLUG}.json').write_text(json.dumps({'cycle_id':CYCLE,'state':'revoked'}))
        with self.assertRaises(ValueError):self.publish()
        (self.root/f'live_runner_handoff_{SLUG}.json').unlink()
        self.facts.write_text(json.dumps({'cycle_id':CYCLE,'profile':'live','facts_hash':'c'*64}))
        with self.assertRaises(ValueError):self.publish()
        self.assertFalse(self.plan.exists())
    def test_publication_hash_mismatch_is_not_authority(self):
        self.publish();self.plan.write_text('{}')
        self.assertFalse(publication.validate(self.plan,CYCLE,hashlib.sha256(self.plan.read_bytes()).hexdigest(),'a'*64)['ok'])
        self.assertFalse(publication.validate(self.plan,CYCLE,'a'*64,'wrong-facts')['ok'])
    def test_no_publish_after_business_deadline(self):
        with self.assertRaises(ValueError):writer.publish(CYCLE,tmp_root=self.root,now=NOW+timedelta(minutes=12))
        self.assertFalse(self.plan.exists())
    def test_publisher_respects_existing_runner_process_lock(self):
        with writer.runner._runner_cycle_lock(self.root/'live_runner.lock',CYCLE):
            with self.assertRaises(writer.runner.PlanError):self.publish()
        self.assertFalse(self.plan.exists())
if __name__=='__main__':unittest.main()
