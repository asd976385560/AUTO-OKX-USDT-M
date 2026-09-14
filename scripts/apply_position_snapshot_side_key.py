# -*- coding: utf-8 -*-
"""Preserve hedged positions by including side in snapshot identity.

Default is read-only. --apply requires an online backup directory. Historical
rows are copied exactly; missing historical opposite sides are not invented.
INSERT OR REPLACE callers remain compatible, including the NULL-side flat sentinel.
"""


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

from contextlib import closing
from datetime import datetime,timezone,timedelta
import argparse,hashlib,json,re,sqlite3,sys,subprocess
from pathlib import Path
for stream in (sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
EXPECTED_COLUMNS=('ts','profile','symbol','side','sz','avgPx','lev','liqPx','upl','marginRatio')
NEW_TABLE='position_snapshots__side_key_20260913'
NULL_INDEX='uq_position_snapshot_side_identity'
CST=timezone(timedelta(hours=8))

def ro(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');return c
def primary_key(con):
    return tuple(r[1] for r in sorted(con.execute('PRAGMA table_info(position_snapshots)'),key=lambda r:r[5]) if r[5])
def production_quiet_check(path):
    if Path(path).resolve()!=Path(_public_project_path('db', 'account.db')).resolve():return
    with closing(ro(Path(path).parent/'ledger.db')) as c:
        now=datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')
        if c.execute("SELECT 1 FROM stage_profile_leases WHERE expires_at>?",(now,)).fetchone():raise RuntimeError('live profile active')
        if c.execute("SELECT 1 FROM execution_intents WHERE profile='live' AND state NOT IN ('completed','failed_clean')").fetchone():raise RuntimeError('execution intent pending')
    names='collect_cycle|fast_collect|slow_collect|jobb_live_account_check|live_position_action_runner|ledger_autoheal|reconcile_exchange_closes|live_reconcile_monitor|push_pipeline|trades_writer'
    command=r"Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '[\\/\s]("+names+r")\.py' } | Select-Object ProcessId | ConvertTo-Json -Compress"
    p=subprocess.run(['pwsh','-NoProfile','-NonInteractive','-Command',command],capture_output=True,text=True,encoding='utf-8',timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if p.returncode!=0:raise RuntimeError('runtime writer inspection failed')
    if p.stdout.strip():raise RuntimeError('collector/account/runtime writer active')
def inspect(con):
    info=con.execute('PRAGMA table_info(position_snapshots)').fetchall()
    if tuple(r[1] for r in info)!=EXPECTED_COLUMNS:raise ValueError('unexpected position snapshot schema')
    pk=primary_key(con)
    if pk not in (('ts','profile','symbol'),('ts','profile','symbol','side')):raise ValueError('unsupported snapshot primary key')
    sql=con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='position_snapshots'").fetchone()[0]
    indices=[r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='position_snapshots' AND sql IS NOT NULL AND name!=?",(NULL_INDEX,))]
    index=con.execute('SELECT sql FROM sqlite_master WHERE type=\'index\' AND name=? AND tbl_name=\'position_snapshots\'',(NULL_INDEX,)).fetchone()
    has_null_index=index is not None
    if index is not None:
        info_index=[r for r in con.execute('PRAGMA index_list(position_snapshots)') if r[1]==NULL_INDEX]
        keys=[r[2] for r in con.execute(f'PRAGMA index_xinfo({NULL_INDEX})') if r[5]]
        compact=re.sub(r'\s+','',index[0]).lower()
        if len(info_index)!=1 or info_index[0][2]!=1 or info_index[0][4]!=0 or keys!=['ts','profile','symbol',None] or "coalesce(side,'')" not in compact:
            raise ValueError('unexpected NULL-side identity index')
    return {'pk':pk,'sql':sql,'indices':indices,'row_count':con.execute('SELECT count(*) FROM position_snapshots').fetchone()[0],'null_index':has_null_index}
def migrate(path: Path, *, apply=False, backup_dir: Path|None=None):
    path=Path(path).resolve()
    with closing(ro(path)) as c:before=inspect(c)
    result={'apply':apply,'old_pk':list(before['pk']),'new_pk':['ts','profile','symbol','side'],'historical_rows':before['row_count'],'historical_backfill':False}
    if before['pk']==('ts','profile','symbol','side') and before['null_index']:
        return {**result,'status':'already_current','changed':False}
    if not apply:return {**result,'status':'ready','would_change':True}
    production_quiet_check(path)
    if backup_dir is None:raise ValueError('--apply requires a backup directory')
    backup_dir=Path(backup_dir).resolve();backup_dir.mkdir(parents=True,exist_ok=True)
    backup=backup_dir/('account-before-side-key-'+datetime.now(CST).strftime('%Y%m%d-%H%M%S-%f')+'.db')
    if backup==path or backup.exists():raise ValueError('backup must be a new separate file')
    with closing(ro(path)) as source,closing(sqlite3.connect(backup)) as dest:
        source.backup(dest);dest.commit()
        if dest.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise RuntimeError('backup failed integrity check')
    result['backup']={'path':str(backup),'sha256':hashlib.sha256(backup.read_bytes()).hexdigest()}
    with closing(sqlite3.connect(path,timeout=10)) as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA busy_timeout=5000');c.execute('PRAGMA synchronous=NORMAL')
        try:
            c.execute('BEGIN IMMEDIATE')
            current=inspect(c)
            if current!=before:raise RuntimeError('snapshot table changed after backup; retry from fresh read')
            if c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(NEW_TABLE,)).fetchone():raise RuntimeError('migration staging table already exists')
            dependencies=c.execute("SELECT name FROM sqlite_master WHERE type IN ('view','trigger') AND lower(sql) LIKE '%position_snapshots%'").fetchall()
            if dependencies:raise ValueError('snapshot has views/triggers requiring explicit migration review')
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                quoted='"'+str(name).replace('"','""')+'"'
                if any(str(r[2]).lower()=='position_snapshots' for r in c.execute('PRAGMA foreign_key_list('+quoted+')')):
                    raise ValueError('snapshot foreign-key dependency requires explicit migration review')
            if current['pk']==('ts','profile','symbol'):
                ddl,n=re.subn(r'PRIMARY KEY\s*\(\s*ts\s*,\s*profile\s*,\s*symbol\s*\)',
                              'PRIMARY KEY (ts, profile, symbol, side)',current['sql'],flags=re.I)
                if n!=1 or not ddl.startswith('CREATE TABLE position_snapshots '):raise ValueError('DDL not recognized')
                ddl=ddl.replace('CREATE TABLE position_snapshots ','CREATE TABLE '+NEW_TABLE+' ',1)
                c.execute(ddl)
                cols=','.join(EXPECTED_COLUMNS)
                c.execute(f'INSERT INTO {NEW_TABLE} ({cols}) SELECT {cols} FROM position_snapshots')
                if c.execute(f'SELECT count(*) FROM {NEW_TABLE}').fetchone()[0]!=before['row_count']:raise RuntimeError('row count mismatch')
                if c.execute(f'SELECT {cols} FROM position_snapshots EXCEPT SELECT {cols} FROM {NEW_TABLE} LIMIT 1').fetchone() or c.execute(f'SELECT {cols} FROM {NEW_TABLE} EXCEPT SELECT {cols} FROM position_snapshots LIMIT 1').fetchone():
                    raise RuntimeError('historical row content changed')
                c.execute('DROP TABLE position_snapshots')
                c.execute(f'ALTER TABLE {NEW_TABLE} RENAME TO position_snapshots')
                for ddl in current['indices']:c.execute(ddl)
            c.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS {NULL_INDEX} ON position_snapshots(ts,profile,symbol,COALESCE(side,\'\'))')
            if primary_key(c)!=('ts','profile','symbol','side'):raise RuntimeError('primary-key readback failed')
            if [r[0] for r in c.execute('PRAGMA quick_check')]!=['ok']:raise RuntimeError('post-migration integrity failure')
            c.commit()
        except Exception:c.rollback();raise
    return {**result,'status':'migrated','changed':True}
def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db',default=_public_project_path('db', 'account.db'));ap.add_argument('--apply',action='store_true');ap.add_argument('--backup-dir');ap.add_argument('--json-out')
    args=ap.parse_args(argv)
    result=migrate(Path(args.db),apply=args.apply,backup_dir=Path(args.backup_dir) if args.backup_dir else None)
    if args.json_out:Path(args.json_out).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0
if __name__=='__main__':raise SystemExit(main())
