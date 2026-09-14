import json,sqlite3,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import query_state

class CronStorageTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)

    def new_schema(self):
        self.c.executescript('''
          CREATE TABLE cron_jobs(store_key TEXT,job_id TEXT,name TEXT,state_json TEXT);
          CREATE TABLE cron_run_receipts(store_key TEXT,job_id TEXT,status TEXT,error_text TEXT,started_at_ms INTEGER,finished_at_ms INTEGER);
        ''')

    def test_receipts_window_system_scope_and_active_state(self):
        self.new_schema()
        self.c.executemany('INSERT INTO cron_jobs VALUES(?,?,?,?)',[
            ('a','job','okx-collect',json.dumps({'lastRunStatus':'error','lastError':'failure','consecutiveErrors':2})),
            ('a','sys','heartbeat-main',json.dumps({'lastRunStatus':'error'}))])
        self.c.executemany('INSERT INTO cron_run_receipts VALUES(?,?,?,?,?,?)',[
            ('a','job','error','included',50,100),('a','job','error','right-end-excluded',100,200),
            ('a','job','ok','',120,130),('a','job','skipped','',120,140),('a','sys','error','system',110,150)])
        failures,active=query_state._read_openclaw_cron_failures(self.c,100,200,('error','failed','timeout'))
        self.assertEqual(len(failures),1)
        self.assertEqual(failures[0][2:],[ 'included',50,50 ] if isinstance(failures[0],list) else ('included',50,50))
        self.assertEqual(active,[('okx-collect','error','failure',2)])

    def test_legacy_schema_is_supported(self):
        self.c.executescript('''
          CREATE TABLE cron_jobs(store_key TEXT,job_id TEXT,name TEXT,last_run_status TEXT,last_error TEXT,consecutive_errors INTEGER);
          CREATE TABLE cron_run_logs(store_key TEXT,job_id TEXT,ts INTEGER,status TEXT,error TEXT,run_at_ms INTEGER,duration_ms INTEGER);
          INSERT INTO cron_jobs VALUES('a','job','okx-collect','error','failure',1);
          INSERT INTO cron_run_logs VALUES('a','job',150,'error','failure',120,30);
        ''')
        failures,active=query_state._read_openclaw_cron_failures(self.c,100,200,('error',))
        self.assertEqual(len(failures),1);self.assertEqual(len(active),1)

    def test_unknown_schema_or_invalid_authoritative_json_is_unavailable(self):
        with self.assertRaises(RuntimeError):query_state._read_openclaw_cron_failures(self.c,0,200,('error',))
        self.new_schema()
        self.c.execute("INSERT INTO cron_jobs VALUES('a','job','okx-collect','broken')")
        with self.assertRaises(ValueError):query_state._read_openclaw_cron_failures(self.c,0,200,('error',))

if __name__=='__main__':unittest.main()
