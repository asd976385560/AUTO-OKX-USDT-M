import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import reconcile_exchange_closes as rec

FIXTURE = Path(__file__).parent / 'fixtures/close_identity_synthetic.json'


class CloseIdentityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.calls = []
        self.all_fills = {x['tradeId']: x for x in self.data['base_fills'] + self.data['old_order_complete']}

    def api(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.assertEqual(kwargs['retries'], 0)
        self.assertGreater(kwargs['timeout_sec'], 0)
        self.assertLessEqual(kwargs['timeout_sec'], rec.CLOSE_QUERY_TIMEOUT_SEC)
        self.assertEqual(kwargs['global_args'], ['--profile', 'live'])
        oid = args[args.index('--ordId') + 1] if '--ordId' in args else None
        if args[:2] == ('swap', 'fills'):
            values = [x for x in self.all_fills.values() if x['ordId'] == oid] if oid else self.data['base_fills']
            return {'code': '0', 'data': copy.deepcopy(values)}
        self.assertEqual(args[:2], ('swap', 'get'))
        group = next(g for g in rec.group_by_ord(list(self.all_fills.values())) if g['ordId'] == oid)
        return {'code': '0', 'data': [{'instId': self.data['symbol'], 'ordId': oid,
                'side': 'sell', 'posSide': 'long', 'state': 'filled',
                'accFillSz': str(group['sz']), 'avgPx': str(group['wavg_px']), 'pnl': str(group['pnl'])}]}

    def classify(self, api=None):
        key = (self.data['symbol'], 'long')
        with mock.patch.object(rec, 'okx_json', side_effect=api or self.api):
            return rec.classify('live', {key: self.data['rows']}, {key: 33}, {})

    def test_real_incident_matches_new_33_after_closed_epoch_fence(self):
        result = self.classify()
        self.assertEqual(result['fuzzy'], [])
        self.assertEqual(len(result['exact']), 1)
        matched = result['exact'][0][2]
        self.assertEqual({g['ordId'] for g in matched}, set(self.data['expected_ids']))
        self.assertEqual(sum(g['sz'] for g in matched), 33)
        self.assertEqual(len(self.calls), 4)

    def test_multi_order_ledger_row_consumes_both_exact_ids(self):
        row = next(r for r in self.data['rows'] if r['id'] == 819)
        ids = rec._close_raw_ids(row)
        groups = [g for g in rec.group_by_ord(list(self.all_fills.values())) if g['ordId'] in ids]
        remaining, _ = rec.consume_recorded(groups, [row], None)
        self.assertEqual(remaining, [])

    def test_partial_history_without_order_completion_is_not_consumed_in_full_history(self):
        def api(*args, **kwargs):
            if args[:2] == ('swap', 'fills') and '--ordId' in args:
                return {'data': [x for x in self.data['base_fills'] if x['ordId'] == args[args.index('--ordId') + 1]]}
            return self.api(*args, **kwargs)
        with mock.patch.object(rec, 'okx_json', side_effect=api):
            with self.assertRaises(rec.CloseEvidenceError):
                rec._prepare_close_groups('live', self.data['symbol'], 'long',
                    self.data['base_fills'], self.data['rows'],
                    rec.parse_ts('2026-08-15 09:30:00'))

    def test_same_trade_identity_is_deduplicated_across_sources(self):
        fills = self.data['base_fills']
        self.assertEqual(len(rec._merge_close_fills(fills + fills, self.data['symbol'], 'long')), len(fills))

    def test_conflicting_trade_identity_is_rejected(self):
        item = copy.deepcopy(self.data['base_fills'][0]);item['fillSz'] = '1'
        with self.assertRaisesRegex(rec.CloseEvidenceError, 'conflicting_close_trade_id'):
            rec._merge_close_fills(self.data['base_fills'] + [item], self.data['symbol'], 'long')

    def test_missing_identity_and_nonfinite_values_are_rejected(self):
        for field, value in [('tradeId', ''), ('ordId', ''), ('fillSz', 'NaN'), ('fillPx', '0'), ('fillPnl', 'inf')]:
            with self.subTest(field=field):
                row = dict(self.data['base_fills'][0]);row[field] = value
                with self.assertRaises(rec.CloseEvidenceError):
                    rec._merge_close_fills([row], self.data['symbol'], 'long')

    def test_wrong_scope_is_rejected(self):
        for field, value in [('instId', 'OTHER-USDT-SWAP'), ('side', 'buy'), ('posSide', 'short')]:
            with self.subTest(field=field):
                row = dict(self.data['base_fills'][0]);row[field] = value
                with self.assertRaises(rec.CloseEvidenceError):
                    rec._merge_close_fills([row], self.data['symbol'], 'long')

    def test_duplicate_recorded_order_owner_is_rejected(self):
        row = next(r for r in self.data['rows'] if r['id'] == 819)
        groups = [g for g in rec.group_by_ord(list(self.all_fills.values())) if g['ordId'] in rec._close_raw_ids(row)]
        with self.assertRaisesRegex(rec.CloseEvidenceError, 'duplicate_recorded_close'):
            rec.consume_recorded(groups, [row, row], None)

    def test_recorded_order_economics_mismatch_is_not_consumed(self):
        row = copy.deepcopy(next(r for r in self.data['rows'] if r['id'] == 819));row['pnl'] += 1
        groups = [g for g in rec.group_by_ord(list(self.all_fills.values())) if g['ordId'] in rec._close_raw_ids(row)]
        with self.assertRaises(rec.CloseEvidenceError):
            rec.consume_recorded(groups, [row], None)

    def test_legacy_close_without_id_is_not_guessed(self):
        row = copy.deepcopy(next(r for r in self.data['rows'] if r['id'] == 819));row['raw'] = '{}'
        with self.assertRaisesRegex(rec.CloseEvidenceError, 'identity_missing'):
            rec.consume_recorded(rec.group_by_ord(self.data['base_fills']), [row], None)

    def test_nonterminal_candidate_order_stays_fuzzy(self):
        def api(*args, **kwargs):
            result = self.api(*args, **kwargs)
            if args[:2] == ('swap', 'get'):result['data'][0]['state'] = 'live'
            return result
        result = self.classify(api)
        self.assertEqual(result['exact'], [])
        self.assertIn('terminal_or_scope_unverified', result['fuzzy'][0][2])

    def test_candidate_cumulative_fill_mismatch_stays_fuzzy(self):
        def api(*args, **kwargs):
            result = self.api(*args, **kwargs)
            if args[:2] == ('swap', 'get'):result['data'][0]['accFillSz'] = '1000'
            return result
        self.assertEqual(self.classify(api)['exact'], [])

    def test_order_api_failure_stays_fuzzy(self):
        def api(*args, **kwargs):
            if args[:2] == ('swap', 'get'):raise TimeoutError('isolated timeout')
            return self.api(*args, **kwargs)
        self.assertEqual(self.classify(api)['exact'], [])

    def test_lookup_and_time_budgets_are_fail_closed(self):
        for key, value in [('CLOSE_ORDER_LOOKUP_LIMIT', 0), ('CLOSE_EVIDENCE_BUDGET_SEC', 0)]:
            with self.subTest(budget=key), mock.patch.object(rec, key, value):
                self.assertEqual(self.classify()['exact'], [])
        self.assertIsNone(rec._CLOSE_BUDGET.get())

    def test_clean_position_performs_no_history_or_order_requests(self):
        key = (self.data['symbol'], 'long')
        with mock.patch.object(rec, 'okx_json', side_effect=AssertionError('unexpected request')):
            result = rec.classify('live', {key: self.data['rows']}, {key: 33}, {key: 33})
        self.assertEqual(result['exact'], [])
        self.assertEqual(result['fuzzy'], [])

    def test_clean_ledger_does_not_load_wide_historical_raw(self):
        con = sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
        try:
            con.execute('CREATE TABLE trades(id INTEGER,cycle_id TEXT,ts TEXT,symbol TEXT,action TEXT,side TEXT,sz REAL,fill_px REAL,lev REAL,pnl REAL,raw TEXT)')
            con.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?)',(1,'2026-09-06T09:00','2026-09-06 09:00:00','ZEC-USDT-SWAP','open','long',33,1000,5,0,'large unused raw'))
            reads=[];con.set_trace_callback(reads.append)
            rows=rec.ledger_rows(con);key=('ZEC-USDT-SWAP','long')
            result=rec.classify('live',rows,{key:33},{key:33})
            self.assertEqual(result['ghosts'],[])
            self.assertEqual(len(reads),1)
            self.assertNotIn('raw',reads[0].lower())
        finally:
            con.close()

    def test_open_consumption_keeps_legacy_contract(self):
        row = {'id': 1, 'ts': '2026-09-05 13:24:47', 'action': 'open', 'sz': 17}
        group = {'ordId': 'OPEN-LEGACY', 'sz': 17, 't_last_ms': int(rec.parse_ts(row['ts']).timestamp() * 1000)}
        remaining, _ = rec.consume_recorded([group], [row], None, rec.OPEN_ACTIONS)
        self.assertEqual(remaining, [])


class ReconcileMergePreservationTests(unittest.TestCase):
    def test_same_cycle_uni_and_zec_keep_individual_times_and_repeat_is_noop(self):
        fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        matched = [g for g in rec.group_by_ord(fixture['base_fills']) if g['ordId'] in fixture['expected_ids']]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);path = root/'live_trades.db'
            con = sqlite3.connect(path)
            con.executescript('''CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY,ts TEXT NOT NULL,mode TEXT,decision TEXT,n_orders INTEGER,equity REAL,note TEXT,raw TEXT);
CREATE TABLE trades(id INTEGER PRIMARY KEY AUTOINCREMENT,cycle_id TEXT,ts TEXT NOT NULL,symbol TEXT NOT NULL,action TEXT NOT NULL,side TEXT NOT NULL,sz REAL,fill_px REAL,lev REAL,margin REAL,notional REAL,score_total INTEGER,reasoning TEXT,deviation TEXT,degradation TEXT,pnl REAL,raw TEXT);''')
            original_raw = {'reconcile_source': 'exchange_fills_reconcile', 'decision_protocol': 'minimal_decision_v2', 'prev_cycle': {'decision': 'error', 'raw': {'status': 'error', 'errors': ['pretrade_ledger_autoheal_blocked']}}}
            con.execute('INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)',('2026-09-06T09:00','2026-09-06 09:10:38','live','traded',1,751.3,'original UNI',json.dumps(original_raw)))
            con.execute('INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,fill_px,lev,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?,?)',('2026-09-06T09:00','2026-09-06 09:10:38','UNI-USDT-SWAP','close','long',23,7.405,5,7.314,json.dumps({'ord_ids':['UNI-EXISTING'],'close_ts':'2026-09-06 09:10:38','reconcile_source':'exchange_fills_reconcile'})))
            con.commit();con.close()
            ro = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);ro.row_factory=sqlite3.Row
            with closing(ro), mock.patch.dict(os.environ,{'OKX_ACCOUNT_DB':str(root/'account.db')}), mock.patch.object(rec,'find_journal_close',return_value=None), mock.patch.object(rec.trades_writer,'write_experiences',return_value={'written':1}) as experiences:
                result = rec.apply_reconcile(path,'live',fixture['symbol'],'long',33,matched,ro,open_lev=5)
                self.assertTrue(result['writer']['ok'])
                first = [tuple(r) for r in ro.execute('SELECT symbol,ts,sz,fill_px,pnl FROM trades ORDER BY symbol')]
                self.assertEqual(first[0],('UNI-USDT-SWAP','2026-09-06 09:10:38',23,7.405,7.314))
                self.assertEqual(first[1][0:3],('ZEC-USDT-SWAP','2026-09-06 09:05:54',33))
                self.assertAlmostEqual(first[1][3],1084.08030303)
                saved=json.loads(ro.execute('SELECT raw FROM trade_cycles').fetchone()[0])
                self.assertIn('pretrade_ledger_autoheal_blocked',json.dumps(saved))
                self.assertNotIn('business_terminal',saved)
                repeated=rec.apply_reconcile(path,'live',fixture['symbol'],'long',33,matched,ro,open_lev=5)
                self.assertEqual(repeated['status'],'already_recorded')
                self.assertEqual(first,[tuple(r) for r in ro.execute('SELECT symbol,ts,sz,fill_px,pnl FROM trades ORDER BY symbol')])
                self.assertEqual(experiences.call_count,1)
            ro.close()


if __name__ == '__main__':
    unittest.main()
