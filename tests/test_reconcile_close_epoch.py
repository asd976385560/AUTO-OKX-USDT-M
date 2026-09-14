import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
import reconcile_exchange_closes as rec


def row(i,ts,action,sz,oid=None,price=100,pnl=0):
    return {'id':i,'cycle_id':'TEST','ts':ts,'symbol':'SOL-USDT-SWAP',
            'side':'long','action':action,'sz':sz,'fill_px':price,'lev':5,
            'pnl':pnl,'raw':{'ordId':oid} if oid else {}}


class CloseEpochTests(unittest.TestCase):
    def setUp(self):
        self.rows=[row(1,'2026-07-04 00:25:05','open',1),
                   row(2,'2026-07-05 11:10:00','close',1,price=80.52,pnl=-.83),
                   row(3,'2026-09-06 14:06:50','open',4.23,'OPEN-NOW',106.19)]
        self.fills=[{'ordId':'CLOSE-OLD','tradeId':'FILL-OLD','instId':'SOL-USDT-SWAP',
                     'posSide':'long','side':'sell','fillSz':'1','fillPx':'80.52','fillPnl':'-.83',
                     'fillTime':str(int(rec.parse_ts('2026-07-05 11:10:11').timestamp()*1000))},
                    {'ordId':'CLOSE-NOW','tradeId':'FILL-NOW','instId':'SOL-USDT-SWAP',
                     'posSide':'long','side':'sell','fillSz':'4.23','fillPx':'105.07','fillPnl':'-4.7376',
                     'fillTime':str(int(rec.parse_ts('2026-09-06 15:11:37').timestamp()*1000))}]
        self.calls=[]

    def api(self,*args,**kwargs):
        self.calls.append(args)
        if args[:2]==('swap','fills'):return {'data':copy.deepcopy(self.fills)}
        self.assertEqual(args[:2],('swap','get'))
        self.assertEqual(args[-1],'CLOSE-NOW')
        return {'data':[{'ordId':'CLOSE-NOW','instId':'SOL-USDT-SWAP','side':'sell','posSide':'long',
                         'state':'filled','accFillSz':'4.23','avgPx':'105.07','pnl':'-4.7376'}]}

    def classify(self,rows=None,api=None):
        key=('SOL-USDT-SWAP','long')
        rows=self.rows if rows is None else rows
        with mock.patch.object(rec,'okx_json',side_effect=api or self.api):
            return rec.classify('live',{key:rows},{key:rec.net_of(rows)}, {})

    def test_real_incident_shape_excludes_closed_identityless_history(self):
        before=copy.deepcopy(self.rows)
        result=self.classify()
        self.assertEqual(result['fuzzy'],[])
        self.assertEqual(result['exact'][0][2][0]['ordId'],'CLOSE-NOW')
        self.assertEqual(result['exact'][0][1],4.23)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(before,self.rows)

    def test_legacy_rows_still_fail_in_full_history(self):
        with self.assertRaisesRegex(rec.CloseEvidenceError,'identity_missing'):
            rec.consume_recorded(rec.group_by_ord(self.fills),self.rows,None)

    def test_unclosed_prefix_remains_in_evidence_window(self):
        rows=copy.deepcopy(self.rows);rows[1]['sz']=.5
        self.assertEqual(rec._active_close_epoch(rows),(rows,[]))
        self.assertEqual(self.classify(rows)['exact'],[])

    def test_negative_prefix_cannot_authorize_a_cut(self):
        rows=copy.deepcopy(self.rows);rows[1]['sz']=1.1
        self.assertEqual(rec._active_close_epoch(rows),(rows,[]))

    def test_rapid_reentry_and_exact_window_boundary_keep_full_history(self):
        for at in ['2026-07-05 11:10:00','2026-07-05 11:30:00','2026-07-05 12:05:00']:
            with self.subTest(at=at):
                rows=copy.deepcopy(self.rows);rows[2]['ts']=at
                self.assertEqual(rec._active_close_epoch(rows),(rows,[]))

    def test_invalid_time_or_quantity_keeps_full_history(self):
        for field,value in [('ts','bad'),('sz','NaN'),('sz',0),('sz',-1)]:
            with self.subTest(field=field,value=value):
                rows=copy.deepcopy(self.rows);rows[0][field]=value
                self.assertEqual(rec._active_close_epoch(rows),(rows,[]))

    def test_added_from_zero_is_not_an_opening_anchor(self):
        rows=copy.deepcopy(self.rows);rows[2]['action']='add'
        self.assertEqual(rec._active_close_epoch(rows),(rows,[]))

    def test_storage_rowid_order_does_not_change_economic_epoch(self):
        selected,notes=rec._active_close_epoch(list(reversed(self.rows)))
        self.assertEqual([r['id'] for r in selected],[3]);self.assertTrue(notes)

    def test_current_epoch_missing_identity_still_fails_closed(self):
        rows=self.rows+[row(4,'2026-09-06 15:10:00','close',1)]
        self.assertEqual(self.classify(rows)['exact'],[])

    def test_current_order_incomplete_or_nonterminal_stays_fuzzy(self):
        for field,value in [('state','live'),('accFillSz','5')]:
            def api(*args,**kwargs):
                result=self.api(*args,**kwargs)
                if args[:2]==('swap','get'):result['data'][0][field]=value
                return result
            with self.subTest(field=field):self.assertEqual(self.classify(api=api)['exact'],[])

    def test_flat_final_ledger_has_no_active_epoch(self):
        rows=self.rows+[row(4,'2026-09-06 15:11:37','close',4.23,'CLOSE-NOW')]
        self.assertEqual(rec._active_close_epoch(rows),(rows,[]))

    def test_clean_position_never_invokes_epoch_or_history(self):
        key=('SOL-USDT-SWAP','long')
        with mock.patch.object(rec,'_active_close_epoch',side_effect=AssertionError('unexpected')):
            self.assertEqual(rec.classify('live',{key:self.rows},{key:4.23},{key:4.23})['ghosts'],[])

    def test_late_close_preserves_completed_hold_cycle_time_and_terminal(self):
        cycle='2026-09-06T15:00'
        terminal={'schema_version':1,'cycle_id':cycle,'status':'completed',
                  'completed_at_cst':'2026-09-06 15:07:38','clock_stop':'test_terminal'}
        raw={'status':'ok','batch_status':'completed','batch_ok':True,
             'runner_in_progress':False,'business_terminal':terminal,'action_taken':'HOLD',
             'facts_hash':'a'*64,'plan_sha256':'b'*64,'position_action_plan_hash':'c'*64}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=root/'live_trades.db'
            with closing(sqlite3.connect(db)) as c:
                c.executescript('''CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY,ts TEXT NOT NULL,mode TEXT,decision TEXT,n_orders INTEGER,equity REAL,note TEXT,raw TEXT);
CREATE TABLE trades(id INTEGER PRIMARY KEY AUTOINCREMENT,cycle_id TEXT,ts TEXT NOT NULL,symbol TEXT NOT NULL,action TEXT NOT NULL,side TEXT,sz REAL,fill_px REAL,lev REAL,margin REAL,notional REAL,score_total INTEGER,reasoning TEXT,deviation TEXT,degradation TEXT,pnl REAL,raw TEXT);''')
                c.execute('INSERT INTO trade_cycles VALUES(?,?,?,?,?,?,?,?)',(cycle,'2026-09-06 15:07:40','live','hold',0,1000,'original hold',json.dumps(raw)))
                c.commit()
            with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as c, mock.patch.dict(os.environ,{'OKX_ACCOUNT_DB':str(root/'account.db')}), mock.patch.object(rec.trades_writer,'write_experiences',return_value={'exp':1}), mock.patch.object(rec.trades_writer,'_analysis_context_for_cycle',return_value={}), mock.patch.object(rec.trades_writer,'_ctval_for',return_value=1):
                c.row_factory=sqlite3.Row
                rec.apply_reconcile(db,'live','SOL-USDT-SWAP','long',4.23,rec.group_by_ord(self.fills[1:]),c,open_lev=5)
            with closing(sqlite3.connect(db)) as c:
                saved=c.execute('SELECT ts,raw FROM trade_cycles').fetchone()
                trade_ts=c.execute('SELECT ts FROM trades').fetchone()[0]
            self.assertEqual(saved[0],'2026-09-06 15:07:40')
            self.assertEqual(json.loads(saved[1])['business_terminal'],terminal)
            self.assertEqual(trade_ts,'2026-09-06 15:11:37')


if __name__=='__main__':unittest.main()
