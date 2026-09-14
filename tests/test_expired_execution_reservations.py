import contextlib,sqlite3,sys,tempfile,unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import execution_intent as ei
CST=timezone(timedelta(hours=8))

class ExpiredReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.db'
        self.clock=mock.patch.object(ei,'_clock_cst',return_value=datetime(2026,9,5,14,0,tzinfo=CST))
        self.clock.start();self.addCleanup(self.clock.stop)

    def reserve(self, cycle='2026-09-05T13:30', symbol='ETH-USDT-SWAP', action='open'):
        return ei.reserve(self.path,profile='live',cycle_id=cycle,symbol=symbol,side='short',
                          request={'cycle':cycle,'symbol':symbol},now_ts=cycle.replace('T',' ')+':01',action=action)

    def test_expired_reserved_releases_but_old_process_cannot_submit_or_replay(self):
        reservation = self.reserve()
        fresh=self.reserve('2026-09-05T14:00','BTC-USDT-SWAP')
        self.assertEqual(fresh['status'],'reserved')
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("SELECT state,submitted_at,ord_id FROM execution_intents WHERE symbol='ETH-USDT-SWAP'").fetchone(),('failed_clean',None,None))
        with self.assertRaises(RuntimeError):
            ei.mark_submitting(self.path,profile='live',cycle_id='2026-09-05T13:30',symbol='ETH-USDT-SWAP',side='short',fingerprint=reservation['fingerprint'],now_ts='2026-09-05 14:00:02')
        self.assertEqual(self.reserve()['reason'],ei.EXPIRED_RESERVATION_ERROR)

    def test_submitting_submitted_uncertain_and_protection_stay_blocked(self):
        for state in ('submitting','submitted','uncertain'):
            with self.subTest(state=state):
                path=Path(self.tmp.name)/(state+'.db')
                ei.reserve(path,profile='live',cycle_id='2026-09-05T13:30',symbol='ETH-USDT-SWAP',side='short',request={},now_ts='2026-09-05 13:30:01')
                with contextlib.closing(sqlite3.connect(path)) as c,c:c.execute('UPDATE execution_intents SET state=?',(state,))
                self.assertEqual(ei.release_expired_reserved_opens(path,profile='live',before_cycle='2026-09-05T14:00'),[])
        self.reserve(action='adjust_protection')
        self.assertEqual(ei.release_expired_reserved_opens(self.path,profile='live',before_cycle='2026-09-05T14:00'),[])

    def test_current_future_legacy_and_reserved_with_submission_evidence_never_release(self):
        self.reserve()
        self.assertEqual(ei.release_expired_reserved_opens(self.path,profile='live',before_cycle='2026-09-05T13:30'),[])
        self.assertEqual(ei.release_expired_reserved_opens(self.path,profile='live',before_cycle='2026-09-05T14:15'),[])
        with contextlib.closing(sqlite3.connect(self.path)) as c,c:c.execute("UPDATE execution_intents SET ord_id='known-order'")
        self.assertEqual(ei.release_expired_reserved_opens(self.path,profile='live',before_cycle='2026-09-05T14:00'),[])
        with contextlib.closing(sqlite3.connect(self.path)) as c,c:c.execute("UPDATE execution_intents SET ord_id=NULL,submitted_at='2026-09-05 13:35:00'")
        self.assertEqual(ei.release_expired_reserved_opens(self.path,profile='live',before_cycle='2026-09-05T14:00'),[])
        with contextlib.closing(sqlite3.connect(self.path)) as c,c:c.execute("UPDATE execution_intents SET submitted_at=NULL,cycle_id='2026-09-04T13:30',reserved_at='2026-09-04 13:30:01'")
        self.assertEqual(ei.release_expired_reserved_opens(self.path,profile='live',before_cycle='2026-09-05T14:00'),[])

    def test_submit_and_expiry_race_have_only_one_winner(self):
        reservation = self.reserve()
        barrier=threading.Barrier(2)
        def submit():
            barrier.wait()
            try:
                ei.mark_submitting(self.path,profile='live',cycle_id='2026-09-05T13:30',symbol='ETH-USDT-SWAP',side='short',fingerprint=reservation['fingerprint'],now_ts='2026-09-05 14:00:01')
                return True
            except RuntimeError:
                return False
        def expire():
            barrier.wait()
            return ei.release_expired_reserved_opens(self.path,profile='live',before_cycle='2026-09-05T14:00')
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(submit);second=pool.submit(expire)
            submitted=first.result();released=second.result()
        self.assertNotEqual(submitted,bool(released))
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT state FROM execution_intents').fetchone()[0], 'submitting' if submitted else 'failed_clean')

if __name__=='__main__':unittest.main()
