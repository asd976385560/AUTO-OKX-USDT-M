from contextlib import closing
from datetime import datetime,timezone,timedelta
import hashlib,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT/'tests'))
try:
    import tail_fix_stage_runner_under_test as stage
except ImportError:
    import stage_runner as stage
import apply_position_snapshot_side_key as migration
try:
    import tail_fix_alert_recovery_under_test as recovery
except ImportError:
    import alert_recovery as recovery
import test_alert_recovery as recovery_fixtures
CYCLE='2026-09-13T13:00';CST=timezone(timedelta(hours=8))
class PersistenceTailTests(unittest.TestCase):
    def observer(self,tmp,clock):
        o=object.__new__(stage._LiveChildObserver);o.tmp_root=Path(tmp);o.cycle=CYCLE;o.now_fn=lambda:clock[0];o.evidence={};return o
    def test_active_writer_is_not_aborted_after_trade_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=[datetime(2026,9,13,13,10,tzinfo=CST).timestamp()];o=self.observer(tmp,clock)
            handle=stage._try_handoff_lock(Path(tmp)/'live_runner.lock')
            try:
                self.assertTrue(o._wait_for_business_persistence({'state':'executing'}))
                clock[0]+=2
                self.assertTrue(o._wait_for_business_persistence({'state':'executing'}))
                self.assertFalse(o._wait_for_business_persistence({'state':'committed'}))
                self.assertEqual(o.evidence['business_persistence']['status'],'runner_committed')
            finally:stage._release_handoff_lock(handle)
    def test_dead_runner_retains_durable_trade_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            o=self.observer(tmp,[datetime(2026,9,13,13,10,tzinfo=CST).timestamp()])
            self.assertFalse(o._wait_for_business_persistence({'state':'executing'}))
            self.assertEqual(o.evidence['business_persistence']['status'],'runner_gone_before_commit_marker')
    def test_wait_is_bounded_by_30_seconds_and_record_deadline(self):
        for seconds,wait in ((600,30),(895,5)):
            with self.subTest(seconds=seconds),tempfile.TemporaryDirectory() as tmp:
                clock=[datetime(2026,9,13,13,0,tzinfo=CST).timestamp()+seconds];o=self.observer(tmp,clock)
                handle=stage._try_handoff_lock(Path(tmp)/'live_runner.lock')
                try:
                    self.assertTrue(o._wait_for_business_persistence({'state':'executing'}))
                    clock[0]+=wait
                    self.assertFalse(o._wait_for_business_persistence({'state':'executing'}))
                    self.assertEqual(o.evidence['business_persistence']['status'],'runner_commit_wait_expired')
                finally:stage._release_handoff_lock(handle)
    def test_recovery_waits_for_bound_committed_runner(self):
        fixture=recovery_fixtures.RecoveryTests(methodName='runTest');fixture.setUp()
        try:
            root=fixture.root;cycle=recovery_fixtures.CYCLE;slug=cycle.replace(':','-')
            (root/'tmp'/f'position_plan_{slug}.json').write_text('{}')
            (root/'tmp'/f'live_facts_{slug}.json').write_text(json.dumps({'cycle_id':cycle,'facts_hash':'f'*64}))
            marker=root/'tmp'/f'live_runner_state_{slug}.json'
            base={'cycle_id':cycle,'state':'executing','facts_hash':'f'*64,'plan_sha256':hashlib.sha256(b'{}').hexdigest()}
            marker.write_text(json.dumps(base))
            with mock.patch.object(recovery,'RUNNER_COMMIT_REQUIRED_FROM','2026-09-12T00:00'):
                with self.assertRaises(ValueError):recovery.certify_cycle(root,cycle,recovery_fixtures.NOW)
                marker.write_text(json.dumps({**base,'state':'committed'}))
                self.assertEqual(recovery.certify_cycle(root,cycle,recovery_fixtures.NOW)['cycle'],cycle)
        finally:fixture.doCleanups()

OLD_DDL="""CREATE TABLE position_snapshots (
 ts TEXT NOT NULL,profile TEXT NOT NULL DEFAULT 'live',symbol TEXT NOT NULL,
 side TEXT CHECK(side IN ('long','short')),sz REAL,avgPx REAL,lev REAL,liqPx REAL,upl REAL,marginRatio REAL,
 PRIMARY KEY (ts, profile, symbol)
)"""
class SnapshotSideMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.db=self.root/'account.db'
        with closing(sqlite3.connect(self.db)) as c:
            c.execute(OLD_DDL);c.execute('CREATE INDEX idx_pos_symbol_ts ON position_snapshots(symbol,ts)')
            c.execute('CREATE TABLE untouched(value)');c.execute("INSERT INTO untouched VALUES('keep')")
            c.execute("INSERT INTO position_snapshots VALUES('2026-09-13 12:00:00','live','DASH-USDT-SWAP','long',52,54,5,NULL,1,NULL)");c.commit()
    def test_dry_run_preserves_schema_and_rows(self):
        result=migration.migrate(self.db)
        self.assertEqual(result['status'],'ready')
        with closing(sqlite3.connect(self.db)) as c:self.assertEqual(migration.primary_key(c),('ts','profile','symbol'))
    def test_migration_retains_history_and_allows_both_sides(self):
        result=migration.migrate(self.db,apply=True,backup_dir=self.root/'backup')
        self.assertEqual(result['status'],'migrated')
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT side,sz FROM position_snapshots').fetchall(),[('long',52)])
            c.execute("INSERT OR REPLACE INTO position_snapshots VALUES('2026-09-13 12:00:00','live','DASH-USDT-SWAP','short',28,54,5,NULL,1,NULL)");c.commit()
            self.assertEqual(c.execute('SELECT side,sz FROM position_snapshots ORDER BY side').fetchall(),[('long',52),('short',28)])
            self.assertEqual(c.execute('SELECT value FROM untouched').fetchone()[0],'keep')
            self.assertIsNotNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_pos_symbol_ts'").fetchone())
        self.assertEqual(migration.migrate(self.db,apply=True,backup_dir=self.root/'backup')['status'],'already_current')
    def test_null_side_flat_sentinel_stays_idempotent(self):
        migration.migrate(self.db,apply=True,backup_dir=self.root/'backup')
        with closing(sqlite3.connect(self.db)) as c:
            for _ in range(2):c.execute("INSERT OR REPLACE INTO position_snapshots(ts,profile,symbol,side,sz) VALUES('2026-09-13 12:15:00','live','__FLAT__',NULL,0)")
            c.commit();self.assertEqual(c.execute("SELECT count(*) FROM position_snapshots WHERE symbol='__FLAT__'").fetchone()[0],1)
    def test_unreviewed_dependency_fails_without_data_loss(self):
        with closing(sqlite3.connect(self.db)) as c:c.execute('CREATE VIEW snapshot_view AS SELECT * FROM position_snapshots');c.commit()
        with self.assertRaises(ValueError):migration.migrate(self.db,apply=True,backup_dir=self.root/'backup')
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(migration.primary_key(c),('ts','profile','symbol'))
            self.assertEqual(c.execute('SELECT count(*) FROM position_snapshots').fetchone()[0],1)
if __name__=='__main__':unittest.main()
