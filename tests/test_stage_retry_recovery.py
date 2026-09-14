# -*- coding: utf-8 -*-
"""Offline retry and alert regressions; subprocesses and business reads are mocked."""
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

for folder in ('scripts', 'collectors', 'tests'):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / folder))
import _okxcli as cli
import stage_runner
import test_reconcile_hardening as reconcile_tests


def proc(rc=0, out='[]', err=''):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = 0.0
        self.sleeps = []
        self.stack = []
        for patch in (
            mock.patch.object(cli, '_base_cmd', return_value=['node', 'offline-cli']),
            mock.patch.object(cli, '_throttle'),
            mock.patch.object(cli.time, 'monotonic', side_effect=lambda: self.elapsed),
            mock.patch.object(cli.time, 'sleep', side_effect=self.sleep),
        ):
            self.stack.append(patch)
            patch.start()
            self.addCleanup(patch.stop)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def test_transient_read_recovers_on_fourth_attempt_with_backoff(self):
        bad = proc(1, '', 'Error: Failed to call OKX endpoint GET /api/v5/account/positions. Hint: Please check network connectivity and retry the request in a few seconds.')
        with mock.patch.object(cli.subprocess, 'run', side_effect=[bad, bad, bad, proc(out='[{"pos":"1"}]')]) as run:
            self.assertEqual([{'pos': '1'}], cli.okx_json('account', 'positions'))
        self.assertEqual(4, run.call_count)
        self.assertEqual([1.0, 2.0, 4.0], self.sleeps)
        self.assertTrue(all(c.kwargs['creationflags'] == cli._CREATE_NO_WINDOW for c in run.call_args_list))

    def test_auth_and_parameter_errors_do_not_retry(self):
        for err in ('HTTP 401: invalid API key', 'HTTP 403: permission denied',
                    'Error: invalid argument --instType', 'Error: unknown option --bad'):
            with self.subTest(err=err), mock.patch.object(cli.subprocess, 'run', return_value=proc(1, '', err)) as run:
                with self.assertRaises(RuntimeError):
                    cli.okx_json('account', 'positions')
                self.assertEqual(1, run.call_count)
        self.assertEqual([], self.sleeps)

    def test_mutations_and_unknown_commands_never_retry(self):
        commands = [('swap', 'place'), ('swap', 'close'), ('swap', 'leverage'),
                    ('swap', 'algo', 'place'), ('swap', 'algo', 'amend'),
                    ('swap', 'algo', 'cancel'), ('account', 'transfer'), ('new-write', 'submit')]
        for args in commands:
            with self.subTest(args=args), mock.patch.object(cli.subprocess, 'run', return_value=proc(1, '', 'network error')) as run:
                with self.assertRaises(RuntimeError):
                    cli.okx_json(*args, retries=8)
                self.assertEqual(1, run.call_count)
        self.assertEqual([], self.sleeps)

    def test_timeouts_share_original_three_call_budget(self):
        def timeout(*args, **kwargs):
            self.elapsed += kwargs['timeout']
            raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
        with mock.patch.object(cli.subprocess, 'run', side_effect=timeout) as run:
            with self.assertRaises(TimeoutError):
                cli.okx_json('account', 'positions', timeout_sec=10)
        self.assertLessEqual(self.elapsed, 30)
        self.assertLessEqual(run.call_count, 3)

    def test_zero_retries_is_honored_and_api_rejection_is_not_retried(self):
        with mock.patch.object(cli.subprocess, 'run', return_value=proc(1, '', 'network error')) as run:
            with self.assertRaises(RuntimeError):
                cli.okx_json('market', 'mark-price', retries=0)
            self.assertEqual(1, run.call_count)
        with mock.patch.object(cli.subprocess, 'run', return_value=proc(out='{"code":"50101","data":[]}')) as run:
            self.assertEqual('50101', cli.okx_json('account', 'positions')['code'])
            self.assertEqual(1, run.call_count)
        self.assertEqual([], self.sleeps)

    def test_stale_output_after_total_deadline_is_rejected(self):
        def late(*args, **kwargs):
            self.elapsed = 31
            return proc(out='[{"pos":"1"}]')
        with mock.patch.object(cli.subprocess, 'run', side_effect=late) as run:
            with self.assertRaises(TimeoutError):
                cli.okx_json('account', 'positions', timeout_sec=10)
        self.assertEqual(1, run.call_count)


class PartialPushTests(unittest.TestCase):
    def fixture(self):
        values = reconcile_tests.StageBusinessOutputTests._business_error_push_fixture()
        cycle, live, report, monitor = json.loads(json.dumps(values).replace('2026-08-22', '2026-09-08').replace('09:', '12:'))
        live['business_check']['error'] = 'RuntimeError: live_trades.db batch_status=partial 非完整成功终态'
        for name in ('business_attestation_pre_archive', 'business_attestation_pre_send'):
            report['steps'][name].update(ok=True, required=True, mode='business_terminal', decision='traded', n_orders=2, trade_count=2)
        report['steps']['build'].update(action='CLOSE/OPEN_LONG', n_trades=2)
        return cycle, live, report, monitor

    def test_verified_partial_business_report_does_not_duplicate_live_alert(self):
        cycle, live, report, monitor = self.fixture()
        sla = stage_runner.build_complete_cycle_sla(cycle, monitor, live_status=live)
        self.assertEqual('incomplete', sla['status'])
        self.assertIsNone(stage_runner._forward_post_push_failure(cycle, 'full', monitor, sla, live, push_report=report))
        self.assertEqual('failed', live['status'])

    def test_partial_exception_requires_delivered_matching_safe_evidence(self):
        cycle, live, report, monitor = self.fixture()
        cases = []
        bad = copy.deepcopy(report); bad['send_status'] = 'uncertain_delivery'; cases.append((live, bad, monitor))
        bad = copy.deepcopy(report); bad['steps']['business_attestation_pre_send']['trade_count'] = 1; cases.append((live, bad, monitor))
        bad = copy.deepcopy(report); bad['steps']['build']['n_trades'] = 1; cases.append((live, bad, monitor))
        bad = copy.deepcopy(report); bad['steps']['business_attestation_pre_archive']['live_stage_terminal'] = {'proved': False}; cases.append((live, bad, monitor))
        bad = copy.deepcopy(report); bad['steps']['business_attestation_pre_send']['ok'] = False; cases.append((live, bad, monitor))
        bad = copy.deepcopy(report); bad['steps']['build']['action'] = 'ERROR'; cases.append((live, bad, monitor))
        bad = copy.deepcopy(live); bad['report_reconcile_barrier']['p0'] = True; cases.append((bad, report, monitor))
        bad = copy.deepcopy(monitor); bad['rc'] = 1; cases.append((live, report, bad))
        for i, (live_case, report_case, monitor_case) in enumerate(cases):
            with self.subTest(i=i):
                sla = stage_runner.build_complete_cycle_sla(cycle, monitor_case, live_status=live_case)
                self.assertIsNotNone(stage_runner._forward_post_push_failure(cycle, 'full', monitor_case, sla, live_case, push_report=report_case))

    def test_historical_partial_report_keeps_existing_failure_verdict(self):
        cycle, live, report, monitor = self.fixture()
        with mock.patch.object(stage_runner, '_PARTIAL_BUSINESS_REPORT_FROM', '2099-01-01T00:00', create=True):
            self.assertFalse(stage_runner._strict_business_error_push_report(cycle, report, live))


class FailureDetailTests(unittest.TestCase):
    def test_nested_protection_failure_is_visible(self):
        cycle = '2026-09-08T12:15'
        raw = {'cycle_id': cycle, 'batch_status': 'partial', 'position_action_failures': [{
            'request': {'action': 'ADD', 'symbol': 'X-USDT-SWAP', 'side': 'long'},
            'problem': 'executor p0=true',
            'result': {'p0': True, 'protection_sync': {'reject_reason': 'protection_place_failed', 'reject_detail': 'POST order-algo network error'}}}]}
        with mock.patch.object(stage_runner, '_row_exists', return_value=(True, {'decision': 'traded', 'n_orders': 1, 'raw': json.dumps(raw)})):
            result = stage_runner.verify_business_output('live', cycle, 'full')
        self.assertFalse(result['ok'])
        self.assertEqual('protection_place_failed', result['execution_failures'][0]['reason'])

    def test_compacted_failure_still_reports_action_and_problem(self):
        cycle = '2026-09-08T12:15'
        raw = {'cycle_id': cycle, 'batch_status': 'partial', 'position_action_failures': [{
            'request': {'action': 'ADD', 'symbol': 'X-USDT-SWAP', 'side': 'long'},
            'problem': 'executor p0=true', 'row_sha256': 'a' * 64}]}
        with mock.patch.object(stage_runner, '_row_exists', return_value=(True, {'decision': 'traded', 'n_orders': 1, 'raw': json.dumps(raw)})):
            result = stage_runner.verify_business_output('live', cycle, 'full')
        self.assertFalse(result['ok'])
        self.assertIn('executor p0=true', result['execution_failures'][0]['detail'])


if __name__ == '__main__':
    unittest.main()
