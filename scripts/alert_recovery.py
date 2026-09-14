# -*- coding: utf-8 -*-
"""Evidence-gated recovery notification after a natural V2 push cycle.

Business databases and historical receipts are read-only. Existing retry and
ledger-autoheal owners perform repairs; this observer never trades, changes
strategy, restarts services, clears P0, or rewrites failed cycles. QQ's existing
atomic dedupe writer owns delivery. Default CLI mode only previews evidence.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8))
CONFIG_NAME = 'alert_recovery.json'
MAX_LOOKBACK_SLOTS = 96
MAX_KNOWN_FAILED_SEND_ATTEMPTS = 3
RUNNER_COMMIT_REQUIRED_FROM = '2026-09-13T13:00'


def cycle_time(cycle: str) -> datetime:
    value = datetime.strptime(cycle, '%Y-%m-%dT%H:%M').replace(tzinfo=CST)
    if value.strftime('%Y-%m-%dT%H:%M') != cycle or value.minute % 15:
        raise ValueError('invalid natural cycle')
    return value


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'object required: {path.name}')
    return value


def ro(path: Path):
    con = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.'+path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def _require(condition, reason):
    if not condition: raise ValueError(reason)


def configuration(root: Path) -> dict:
    path = root/'config'/CONFIG_NAME
    if not path.exists(): return {'enabled':False}
    result = read_json(path)
    _require(result.get('schema_version') == 1, 'recovery config schema invalid')
    _require(isinstance(result.get('enabled'), bool), 'recovery config enabled invalid')
    if result['enabled']: cycle_time(result['activation_cycle'])
    return result


def certify_cycle(root: Path, cycle: str, now: datetime) -> dict:
    """Independent readback of the current completed flow; missing proof rejects."""
    instant = cycle_time(cycle)
    _require(0 <= (now-instant).total_seconds() <= 960, 'cycle stale or future')
    slug = cycle.replace(':','-')
    paths = {
        'live':root/'logs/stage-status'/f'live-{slug}.json',
        'push':root/'logs/stage-status'/f'push-{slug}.json',
        'pipeline':root/'reports/push'/instant.strftime('%Y/%m/%d')/f'pipeline-{instant:%Y-%m-%d-%H%M}.json',
    }
    if cycle >= RUNNER_COMMIT_REQUIRED_FROM:
        paths.update({
            'runner':root/'tmp'/f'live_runner_state_{slug}.json',
            'plan':root/'tmp'/f'position_plan_{slug}.json',
            'facts':root/'tmp'/f'live_facts_{slug}.json',
        })
    raw_inputs = {key:path.read_bytes() for key,path in paths.items()}
    documents = {key:json.loads(raw.decode('utf-8-sig')) for key,raw in raw_inputs.items()}
    _require(all(isinstance(value,dict) for value in documents.values()), 'cycle evidence must contain objects')
    live, push, pipe = [documents[k] for k in ('live','push','pipeline')]
    if cycle >= RUNNER_COMMIT_REQUIRED_FROM:
        runner, facts = documents['runner'], documents['facts']
        _require(runner.get('cycle_id') == cycle and runner.get('state') == 'committed'
                 and runner.get('plan_sha256') == hashlib.sha256(raw_inputs['plan']).hexdigest()
                 and runner.get('facts_hash') == facts.get('facts_hash')
                 and facts.get('cycle_id') == cycle, 'runner persistence tail not committed or bound')
    for stage, row in (('live',live),('push',push)):
        _require(row.get('cycle_id') == cycle and row.get('stage') == stage,
                 f'{stage} cycle identity mismatch')
        _require(row.get('status') == 'succeeded' and row.get('returncode') == 0,
                 f'{stage} is not successful')
    _require(live.get('profile_lease_released') is True, 'live lease not released')
    _require(live.get('business_check',{}).get('ok') is True, 'business terminal unverified')
    barrier = live.get('report_reconcile_barrier') or {}
    _require(barrier.get('cycle_id') == cycle and barrier.get('profile') == 'live' and
             barrier.get('status') == 'ok' and barrier.get('rc') == 0 and barrier.get('report_safe') is True and
             barrier.get('blocking') is False, 'report barrier not clean')
    sla = push.get('complete_cycle_sla') or {}
    _require(sla.get('strict_cycle_pass') is True and sla.get('status') == 'met',
             'complete natural cycle not verified')
    post = push.get('post_live_reconcile') or {}
    _require(post.get('rc') == 0 and post.get('started') is True and
             post.get('timed_out') is False and post.get('deadline_exceeded') is False,
             'post-push reconciliation did not complete')
    post_output = json.loads(post.get('output') or '{}')
    _require(post_output.get('cycle_id') == cycle and post_output.get('profile') == 'live'
             and post_output.get('ok') is True and post_output.get('issue') is False
             and post_output.get('rc') == 0 and not post_output.get('skipped'),
             'post-push reconciliation evidence incomplete')
    _require(pipe.get('cycle') == cycle and pipe.get('ok') is True and
             pipe.get('send_status') == 'sent' and
             pipe.get('natural_production_evidence') is True and
             pipe.get('execution_context') == 'production', 'natural delivery not verified')
    steps = pipe.get('steps') or {}
    _require(steps.get('send',{}).get('rc') == 0 and bool(re.search(
        r'"messageId"\s*:\s*"[^"\s]+"', str(steps.get('send',{}).get('out') or ''))),
        'delivery messageId missing')
    attestations = [steps.get(k) or {} for k in ('business_attestation_pre_archive','business_attestation_pre_send')]
    _require(all(a.get('ok') is True and a.get('required') is True for a in attestations),
             'business attestations missing')
    _require(re.fullmatch(r'[0-9a-f]{64}', str(attestations[0].get('sha256') or '')) is not None
             and attestations[0]['sha256'] == attestations[1].get('sha256'), 'business fingerprint mismatch')
    with closing(ro(root/'db/ledger.db')) as con:
        _require(con.execute("SELECT 1 FROM execution_intents WHERE profile='live' AND state NOT IN ('completed','failed_clean') LIMIT 1").fetchone() is None,
                 'unresolved execution intent remains')
        _require(con.execute('SELECT 1 FROM stage_profile_leases WHERE profile=? AND expires_at>? LIMIT 1',
                 ('live',now.strftime('%Y-%m-%d %H:%M:%S'))).fetchone() is None, 'another live runner active')
        fast = con.execute("SELECT status FROM collection_runs WHERE cycle_id=? AND source='fast'",(cycle,)).fetchone()
        _require(fast is not None and fast['status'] == 'ok', 'current fast collection not fully healthy')
    with closing(ro(root/'db/analysis.db')) as con:
        row = con.execute('SELECT status FROM analysis_runs WHERE cycle_id=?',(cycle,)).fetchone()
        _require(row is not None and row['status'] == 'ok', 'analysis database not successful')
    with closing(ro(root/'db/live_trades.db')) as con:
        row = con.execute('SELECT decision,n_orders FROM trade_cycles WHERE cycle_id=?',(cycle,)).fetchone()
        _require(row is not None and row['decision'] in ('hold','traded'), 'trade database not successful')
        count = con.execute('SELECT count(*) FROM trades WHERE cycle_id=?',(cycle,)).fetchone()[0]
        _require(row['n_orders'] == count and all(a.get('n_orders') == count and a.get('trade_count') == count
                 and a.get('decision') == row['decision'] for a in attestations), 'trade count/decision mismatch')
    with closing(ro(root/'db/account.db')) as con:
        pending_repairs = con.execute("SELECT count(*) FROM repair_queue WHERE status NOT IN ('closed','resolved')").fetchone()[0]
    return {'cycle':cycle,'certified_at':now.isoformat(),'business_elapsed_seconds':sla.get('elapsed_seconds'),
            'trade_count':count,'pending_independent_repairs':pending_repairs,
            'healed_at_report_barrier':barrier.get('healed_count',0),
            'files':{k:{'path':str(p),'sha256':hashlib.sha256(raw_inputs[k]).hexdigest()} for k,p in paths.items()}}


def recent_failure(root: Path, cycle: str, activation: str) -> dict | None:
    instant = cycle_time(cycle)
    lower = max(cycle_time(activation), instant-timedelta(minutes=15*MAX_LOOKBACK_SLOTS))
    failures = {}
    for index in range(1, MAX_LOOKBACK_SLOTS+1):
        previous = instant-timedelta(minutes=15*index)
        if previous < lower: break
        key = previous.strftime('%Y-%m-%dT%H:%M')
        for stage in ('live','push'):
            path = root/'logs/stage-status'/f'{stage}-{key.replace(":","-")}.json'
            if not path.exists(): continue
            row = read_json(path)
            _require(row.get('cycle_id') == key and row.get('stage') == stage, 'historical stage identity mismatch')
            if row.get('status') == 'failed':
                failures.setdefault(key,[]).append({'component':stage,'kind':str(row.get('failure_kind') or 'stage_failed')[:100]})
            barrier = row.get('report_reconcile_barrier') or {}
            if stage == 'live' and barrier.get('required') is True and barrier.get('report_safe') is False:
                failures.setdefault(key,[]).append({'component':'report_reconcile','kind':'report_barrier_blocked'})
    with closing(ro(root/'db/ledger.db')) as con:
        rows = con.execute("SELECT cycle_id,source,status FROM collection_runs WHERE cycle_id>=? AND cycle_id<? "
                           "AND source IN ('fast','slow','regime') AND status NOT IN ('ok','degraded')",
                           (lower.strftime('%Y-%m-%dT%H:%M'),cycle))
        for row in rows:
            cycle_time(row['cycle_id'])
            failures.setdefault(row['cycle_id'],[]).append({'component':'collection:'+row['source'],'kind':row['status']})
    if not failures: return None
    last = max(failures)
    result = {'last_failed_cycle':last,'causes':failures[last]}
    receipt = root/'tmp'/f'_receipt_live_{last.replace(":","-")}.json'
    if receipt.exists():
        data = read_json(receipt)
        _require(data.get('cycle_id') == last, 'failure receipt cycle mismatch')
        if any((x.get('result') or {}).get('reject_detail') == 'target_risk_below_minimum_order'
               for x in data.get('position_action_failures') or []):
            result['guard_note'] = '上次计划低于交易所最小下单量；风险预算没有自动放大。'
    return result


def render_recovery(fault: dict, proof: dict) -> str:
    parts = ['✅ V2 后续轮处理已恢复',
             f"最近失败轮：{fault['last_failed_cycle']}",
             f"恢复核验轮：{proof['cycle']}，业务用时 {proof['business_elapsed_seconds']} 秒。",
             '本轮采集、分析、交易终态、推送回执及推送后对账均已核验通过。']
    if proof.get('healed_at_report_barrier'):
        parts.append(f"本轮报告前已由既有自愈流程补记 {proof['healed_at_report_barrier']} 项。")
    if fault.get('guard_note'): parts.append(fault['guard_note'])
    if proof.get('pending_independent_repairs'):
        parts.append(f"另有 {proof['pending_independent_repairs']} 条独立修复工单仍待处理。")
    parts.append('原失败轮继续保留；本通知不代表旧订单重试或所有历史数据缺口已修复。')
    return '\n'.join(parts)


def delivery_status(root: Path, key: str) -> str | None:
    path = root/'db/qq_push_dedupe.db'
    if not path.exists(): return None
    digest = hashlib.sha256(('alert|'+key).encode()).hexdigest()
    with closing(ro(path)) as con:
        row = con.execute('SELECT status FROM sent WHERE k=?',(digest,)).fetchone()
    return str(row['status']) if row else None


def observe(cycle: str, *, root: Path = ROOT, send: bool = False,
            now: datetime | None = None, run_command=None) -> dict:
    now = now or datetime.now(CST)
    started = time.monotonic()
    result = {'schema_version':1,'cycle':cycle,'business_writes':0,'exchange_writes':0,'service_restarts':0}
    try:
        cfg = configuration(root)
        if not cfg['enabled']: return {**result,'status':'disabled'}
        if cycle_time(cycle) < cycle_time(cfg['activation_cycle']):
            return {**result,'status':'before_activation'}
        fault = recent_failure(root,cycle,cfg['activation_cycle'])
        if fault is None: return {**result,'status':'no_prior_failure'}
        key = 'v2-recovered:flow:'+fault['last_failed_cycle']
        existing = delivery_status(root,key)
        if existing == 'sent': return {**result,'status':'already_notified','dedupe_key':key}
        if existing not in (None,'failed'):
            return {**result,'status':'delivery_requires_verification','delivery_status':existing,'dedupe_key':key}
        proof = certify_cycle(root,cycle,now)
        text = render_recovery(fault,proof)
        result.update(status='verified_recovery',fault=fault,proof=proof,dedupe_key=key,would_send=text)
        if not send: return result
        remaining = (cycle_time(cycle)+timedelta(seconds=960)-now).total_seconds()-(time.monotonic()-started)
        # QQ owns a 55s total send budget. Defer instead of terminating it midway;
        # the same incident will be evaluated after a later natural success.
        if remaining < 62: return {**result,'status':'deferred_notification_budget'}
        folder = root/'logs/alert-recovery'/cycle_time(fault['last_failed_cycle']).strftime('%Y/%m/%d')
        name = 'flow-'+fault['last_failed_cycle'].replace(':','-')
        receipt = folder/(name+'.json')
        prior = read_json(receipt) if receipt.exists() else {}
        attempts = list(prior.get('attempts') or [])
        if prior.get('status') in ('submitting','unconfirmed_delivery') and existing is None:
            return {**result,'status':'delivery_requires_verification','reason':'attempt exists without authoritative dedupe state'}
        if len(attempts) >= MAX_KNOWN_FAILED_SEND_ATTEMPTS:
            return {**result,'status':'notification_retry_limit'}
        folder.mkdir(parents=True,exist_ok=True)
        marker = folder/(name+'.'+cycle.replace(':','-')+'.claim')
        try:
            with marker.open('x',encoding='utf-8') as handle:
                handle.write(key);handle.flush();os.fsync(handle.fileno())
        except FileExistsError:
            return {**result,'status':'same_cycle_already_attempted'}
        # Check the exact authoritative artifacts again immediately before send.
        _require(all(hashlib.sha256(Path(v['path']).read_bytes()).hexdigest()==v['sha256']
                     for v in proof['files'].values()),'evidence changed before recovery notification')
        # Parent stage status will append this observer's result after return.
        # Freeze the exact bytes certified here so that later audit additions
        # cannot invalidate or silently replace the notification's evidence.
        for kind, item in proof['files'].items():
            raw = Path(item['path']).read_bytes()
            _require(hashlib.sha256(raw).hexdigest()==item['sha256'], 'evidence changed during snapshot')
            snapshot = folder/(name+'.'+cycle.replace(':','-')+'.'+kind+'.json')
            with snapshot.open('xb') as handle:
                handle.write(raw);handle.flush();os.fsync(handle.fileno())
            item['snapshot'] = str(snapshot)
        message = folder/(name+'.txt')
        message.write_text(text,encoding='utf-8')
        attempts.append({'cycle':cycle,'at':datetime.now(CST).isoformat()})
        result.update(status='submitting',attempts=attempts)
        atomic_json(receipt,result)
        command = [sys.executable,str(root/'scripts/qq_push.py'),'--content-file',str(message),'--alert','--dedupe-key',key]
        runner = run_command or subprocess.run
        try:
            response = runner(command,cwd=str(root),capture_output=True,text=True,encoding='utf-8',
                              errors='replace',timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            result['notification_returncode'] = int(response.returncode)
        except Exception as exc:
            result['notification_error'] = type(exc).__name__
        state = delivery_status(root,key)
        result['delivery_status'] = state
        result['status'] = 'notified' if state=='sent' else ('failed' if state=='failed' else 'unconfirmed_delivery')
        atomic_json(receipt,result)
        return result
    except (OSError,ValueError,KeyError,TypeError,AttributeError,sqlite3.Error) as exc:
        # Observation failure must never turn the already-finalized business
        # cycle into a new trading failure or fabricate a recovery notification.
        return {**result,'status':'not_verified','reason':f'{type(exc).__name__}: {exc}'[:350]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cycle',required=True)
    ap.add_argument('--root',default=str(ROOT))
    ap.add_argument('--send',action='store_true')
    ap.add_argument('--json-out')
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    if args.send and Path(__file__).resolve() != root/'scripts/alert_recovery.py':
        raise SystemExit('send is restricted to the installed production entry')
    result = observe(args.cycle,root=root,send=args.send)
    if args.json_out: atomic_json(Path(args.json_out),result)
    print(json.dumps(result,ensure_ascii=False,indent=1))
    return 0


if __name__ == '__main__':raise SystemExit(main())
