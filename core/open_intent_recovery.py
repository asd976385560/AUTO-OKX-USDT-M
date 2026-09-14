"""Bounded prelaunch recovery of one booked, fully filled and protected OPEN."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
import hashlib,json,sqlite3,time,uuid

ACTIVATION_CYCLE='2026-09-14T11:15'
CST=timezone(timedelta(hours=8))
def _ro(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
    c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');return c

def probe_open(old):
    from scripts._okxcli import okx_json
    symbol=old['symbol'];oid=str(old['ord_id']);start=time.time()*1000
    def read(*args):
        v=okx_json(*args,global_args=['--profile','live'],timeout_sec=6,retries=0)
        if isinstance(v,dict):v=v.get('data')
        if not isinstance(v,list):raise ValueError('invalid exchange read envelope')
        return v
    before=read('account','positions','--instType','SWAP','--instId',symbol)
    calls={'order':['swap','get','--instId',symbol,'--ordId',oid],
           'fills':['swap','fills','--instId',symbol,'--ordId',oid],
           'algos':['swap','algo','orders','--instId',symbol],
           'instrument':['market','instruments','--instType','SWAP','--instId',symbol]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        proof=dict(pool.map(lambda item:(item[0],read(*item[1])),calls.items()))
    if len(proof['order'])!=1 or len(proof['instrument'])!=1:raise ValueError('order/instrument identity not unique')
    proof['order']=proof['order'][0];proof['instrument']=proof['instrument'][0]
    proof.update(positions_before=before,positions_after=read('account','positions','--instType','SWAP','--instId',symbol),
                 read_started_at_ms=start,verified_at_ms=time.time()*1000)
    return proof

def recover_booked_submitted_open(db_root,cycle,*,apply=False):
    """Only prelaunch; never skip the profile pending-intent gate."""
    result={'status':'not_attempted','recovered':[],'exchange_orders_sent':0,'trade_rows_written':0}
    root=Path(db_root).resolve();workspace=root.parent
    if not apply or not cycle or str(cycle)<ACTIVATION_CYCLE:
        return {**result,'reason':'read_only_or_before_activation'}
    if (workspace/'logs/stage-status'/('live-'+str(cycle).replace(':','-')+'.json')).exists():
        return {**result,'reason':'live_stage_already_exists'}
    ledger=root/'ledger.db'
    if not ledger.is_file():return {**result,'reason':'no_execution_intent_database'}
    try:
        with closing(_ro(ledger)) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_intents'").fetchone():
                return {**result,'reason':'no_execution_intent_table'}
            pending=[dict(r) for r in c.execute("SELECT * FROM execution_intents WHERE profile='live' AND state NOT IN ('completed','failed_clean')")]
        if not pending:return {**result,'reason':'no_pending_intent'}
        if len(pending)!=1:return {**result,'reason':'multiple_pending_intents_require_review'}
        old=pending[0]
        if not (old.get('action')=='open' and old.get('state')=='submitted' and old.get('ord_id')
                and old.get('error') is None and old.get('receipt_json') is None and str(old['cycle_id'])<str(cycle)):
            return {**result,'reason':'pending_intent_outside_supported_contract'}
        submitted=datetime.strptime(old['submitted_at'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)
        if datetime.now(CST)<submitted+timedelta(minutes=15):return {**result,'reason':'pending_intent_still_settling'}
        with closing(_ro(root/'live_trades.db')) as c:
            rows=c.execute("SELECT raw FROM trades WHERE cycle_id=? AND symbol=? AND side=? AND action='open'",(old['cycle_id'],old['symbol'],old['side'])).fetchall()
        if not any(str(old['ord_id']) in str(row['raw'] or '') for row in rows):return {**result,'reason':'exact_open_not_yet_booked'}
        directory=workspace/'backups'/('submitted-open-recovery-'+datetime.now(CST).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
        directory.mkdir(parents=True,exist_ok=False);backup=directory/'ledger.db'
        with closing(_ro(ledger)) as src,closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('backup quick_check failed')
        result['backup']={'path':str(backup),'sha256':hashlib.sha256(backup.read_bytes()).hexdigest()}
        from core.execution_intent import reconcile_submitted_open
        proof=probe_open(old)
        answer=reconcile_submitted_open(ledger,expected_row=old,proof=proof,apply=True,backup_path=backup,prelaunch_cycle=str(cycle))
        result.update(status='completed',recovered=[{'cycle':old['cycle_id'],'symbol':old['symbol'],'ord_id':old['ord_id'],'state':'completed','evidence':str(directory/'receipt.json')}])
        (directory/'receipt.json').write_text(json.dumps({'previous_intent':old,'proof':proof,'result':answer},ensure_ascii=False,indent=2),encoding='utf-8')
        # Close only tickets explicitly caused by this exact pending order.
        account=root/'account.db';ids=[]
        with closing(_ro(account)) as c:
            for row in c.execute("SELECT id,issue FROM repair_queue WHERE status='pending' AND check_name='order_executor'"):
                issue=str(row['issue'] or '')
                if f'ord={old["ord_id"]}:' in issue and 'execution_intent_blocked:profile_pending_intent' in issue:ids.append(row['id'])
        if ids:
            from scripts import repair_queue_tool
            rc=repair_queue_tool.do_close(ids,False,'已按订单、逐笔成交、已入主账及唯一足量止损核实旧OPEN终态；未重放交易。证据 '+str(directory/'receipt.json'),True,
                closed_by='ledger_autoheal:verified_submitted_open',db_path=account,quiet=True)
            result['queue_closed']=ids if rc==0 else []
            if rc!=0:result['queue_warning']=f'close rc={rc}'
        return result
    except Exception as exc:
        if result['status']=='completed':result['post_commit_warning']=f'{type(exc).__name__}: {exc}'
        else:result.update(status='verification_blocked',error=f'{type(exc).__name__}: {exc}')
        return result
