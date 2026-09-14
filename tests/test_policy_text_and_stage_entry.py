import copy,inspect,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
from collectors import trades_writer as writer
from scripts import stage_runner as stage
import test_reconcile_hardening as fixtures

ROOT=Path(__file__).resolve().parents[1]
class PolicyTextTests(unittest.TestCase):
    def test_prices_prefixes_and_observed_ratios_are_not_policy_claims(self):
        samples=[{'reasoning':'现价0.06667，买盘24%，选择轻风险做空。'},
                 {'reasoning':'价格0.0666；组合IMR上限66.6%。'},
                 {'reasoning':'当前IMR=0.0666，上限为0.666。'},
                 {'open_execution_package':{'entry':0.06667,'stop':0.0666,'target':0.065}},
                 {'note':'涨幅60%，单笔硬上限20%。'}]
        for value in samples:
            with self.subTest(value=value):self.assertEqual([],writer.human_policy_errors(value))
    def test_explicit_wrong_limits_still_fail(self):
        for text in ['IMR 阈值 0.0666','组合IMR上限=0.0666','IMR cap 0.0666','max_portfolio_imr_ratio=0.0666','IMR不得超过0.0666']:
            with self.subTest(text=text):self.assertTrue(any('0.0666' in x for x in writer.human_policy_errors({'note':text})))
        self.assertTrue(writer.human_policy_errors({'decision_card':{'portfolio_impact':{'max_portfolio_imr_ratio':0.0666}}}))
    def test_retired_concentration_rule_is_distinct_from_market_percentage(self):
        for text in ['同侧集中度 60% 硬上限','同侧上限60%','concentration limit 60%']:
            self.assertTrue(any('60%' in x for x in writer.human_policy_errors({'note':text})))
        self.assertEqual([],writer.human_policy_errors({'note':'同侧实际占比60%，组合IMR上限66.6%。'}))
    def test_explicit_corrections_are_not_new_limit_claims(self):
        for text in ['禁止使用IMR阈值0.0666','IMR阈值0.0666是错误的','do not use IMR cap 0.0666']:
            self.assertEqual([],writer.human_policy_errors({'note':text}))

class StageEntryTests(unittest.TestCase):
    def test_postcheck_exception_is_a_failed_result(self):
        with mock.patch.object(stage,'_forward_post_push_failure',side_effect=RuntimeError('fixture')):
            result=stage._safe_post_push_classifier()
        self.assertEqual('push_postcheck_exception',result['failure_kind']);self.assertNotEqual(0,result['returncode'])
        self.assertIn('RuntimeError',result['error'])
    def test_direct_entry_outside_repo_without_pythonpath(self):
        cycle,live,report,monitor=fixtures.StageBusinessOutputTests._business_error_push_fixture()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);data=root/'fixture.json';data.write_text(json.dumps([cycle,live,report]),encoding='utf-8')
            probe=root/'probe.py'
            probe.write_text('import json,runpy,sys\nfrom pathlib import Path\n'
                +'root=Path(sys.argv[1]); target=Path(sys.argv[2])\n'
                +'assert str(root) not in sys.path\nsys.path.insert(0,str(root/"scripts"))\n'
                +'ns=runpy.run_path(str(target),run_name="isolated_entry_probe")\nassert str(root) in sys.path\n'
                +'cycle,live,report=json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))\n'
                +'assert ns["_strict_business_error_push_report"](cycle,report,live)\nprint("entry_ok")\n',encoding='utf-8')
            env={k:v for k,v in os.environ.items() if k.upper()!='PYTHONPATH'}
            result=subprocess.run([sys.executable,'-I',str(probe),str(ROOT),inspect.getfile(stage),str(data)],cwd=d,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr);self.assertIn('entry_ok',result.stdout)
