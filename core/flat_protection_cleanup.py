"""Exact-ID, evidence-gated cleanup of protection on a verified flat side."""
from __future__ import annotations
from collections import Counter
from decimal import Decimal,InvalidOperation
import time

def _number(value):
    if isinstance(value,bool):raise ValueError('boolean quantity')
    try:x=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError) as exc:raise ValueError('invalid number') from exc
    if not x.is_finite():raise ValueError('nonfinite number')
    return x

def _orders(response,symbol):
    if not isinstance(response,dict) or response.get('ok') is not True:
        reason=(response.get('error') or response.get('sMsg')) if isinstance(response,dict) else 'invalid_response_type'
        raise ValueError('pending_read_failed:'+str(reason or 'unknown')[:250])
    rows=response.get('data')
    if not isinstance(rows,list) or not all(isinstance(x,dict) for x in rows):raise ValueError('pending_rows_invalid')
    if max(Counter(x.get('ordType','unknown') for x in rows).values(),default=0)>=100:raise ValueError('pending_page_incomplete')
    ids=[]
    for x in rows:
        if (str(x.get('sCode','0')) not in ('0','') or x.get('instId')!=symbol
                or not isinstance(x.get('algoId'),str) or not x['algoId']
                or x.get('state')!='live' or x.get('posSide') not in ('long','short')
                or x.get('side') not in ('buy','sell')
                or str(x.get('reduceOnly')).lower() not in ('true','false','1','0')):
            raise ValueError('pending_order_identity_or_state_invalid')
        ids.append(x['algoId'])
    if len(ids)!=len(set(ids)):raise ValueError('duplicate_pending_identity')
    return rows

def _flat(response,symbol,side):
    if not isinstance(response,dict) or response.get('ok') is not True:raise ValueError('positions_read_failed')
    rows=response.get('data')
    if not isinstance(rows,list):raise ValueError('positions_not_list')
    flat=True
    for x in rows:
        if not isinstance(x,dict) or not isinstance(x.get('instId'),str) or not x['instId']:raise ValueError('position_identity_invalid')
        size=_number(x.get('pos'))
        if size and x.get('posSide') not in ('long','short'):raise ValueError('position_side_invalid')
        if size and x['instId']==symbol and x.get('posSide')==side:flat=False
    return flat

def _scope(rows,side):
    return [x for x in rows if x['posSide']==side and x['side']==('sell' if side=='long' else 'buy')
            and str(x['reduceOnly']).lower() in ('true','1')]

def _fingerprint(row):
    return tuple(str(row.get(k) or '') for k in ('algoId','instId','posSide','side','reduceOnly','sz','cTime','ordType','slTriggerPx','tpTriggerPx','callbackRatio','callbackSpread'))

def _transport_failure(response):
    if response.get('ok') is True:return False
    text=' '.join(str(response.get(k) or '') for k in ('error','sMsg','error_type')).lower()
    if any(x in text for x in ('api key','passphrase','invalid signature','permission','invalid argument','parameter','unknown option','authentication')):
        return False
    return any(x in text for x in ('failed to call okx endpoint','network','timeout','timed out','econnreset','econnrefused',
                                  'socket','tls','connection closed','rate limit','too many requests','too frequent','service unavailable','system busy'))

def cleanup_flat_side(symbol,side,*,read_orders,read_positions,cancel_order,
                      now_ms,min_age_ms=0,enabled=True,budget_seconds=45,
                      clock=None,sleep=None):
    """Callbacks preserve the caller's credentials and original runtime budget.

    No cancellation retry is permitted without a fresh complete pending list,
    the same exact old-order fingerprint, and a fresh flat-position proof.
    Acknowledged cancellation is read back, not blindly re-issued.
    """
    if side not in ('long','short'):raise ValueError('invalid side')
    clock=clock or time.monotonic;sleep=sleep or time.sleep
    start=clock();deadline=start+max(0,float(budget_seconds))
    out={'ok':False,'cancel_requested':[],'cancel_failed':[],'remaining':[],
         'kept_recent':[],'attempts':[],'read_error':None,'scope_verified':False,
         'max_attempts_per_id':2,'budget_seconds':max(0,float(budget_seconds))}
    def remaining():
        value=deadline-clock()
        if value<=0.1:raise TimeoutError('cleanup_budget_exhausted')
        return value
    def pending():
        value=_orders(read_orders(remaining()),symbol);remaining();return value
    def flat():
        value=_flat(read_positions(remaining()),symbol,side);remaining();return value
    def settle():
        if remaining()>1.2:sleep(1.0)
    try:
        current=pending();scoped=_scope(current,side);out['remaining']=[x['algoId'] for x in scoped]
        if not flat():raise ValueError('side_reopened_or_not_flat')
        if not scoped:out.update(ok=True,scope_verified=True);return out
        if not enabled:raise ValueError('cleanup_disabled_with_pending_protection')
        selected=[]
        for row in scoped:
            try:old=0<_number(row.get('cTime'))<=_number(now_ms)-_number(min_age_ms)
            except ValueError:old=False
            if old:selected.append(row)
            else:out['kept_recent'].append(row['algoId'])
        for original in selected:
            oid=original['algoId'];resolved=False
            for attempt in range(2):
                current=pending();found=next((x for x in current if x['algoId']==oid),None)
                if found is None:resolved=True;break
                if _fingerprint(found)!=_fingerprint(original):raise ValueError('target_order_changed:'+oid)
                if not flat():raise ValueError('side_reopened_or_not_flat')
                if remaining()<10:raise TimeoutError('insufficient_cancel_and_readback_budget')
                out['cancel_requested'].append(oid)
                try:response=cancel_order(oid,remaining())
                except Exception as exc:response={'ok':False,'sCode':None,'error_type':type(exc).__name__,'error':str(exc)[:300]}
                if not isinstance(response,dict):
                    response={'ok':False,'sCode':None,'error_type':'MalformedCancelResponse','error':'cancel response was not an object'}
                out['attempts'].append({'algoId':oid,'attempt':attempt+1,
                    'response':{k:response.get(k) for k in ('ok','sCode','sMsg','error','error_type','data')},
                    'fresh_pending_and_flat_verified':True})
                settle();current=pending()
                if not any(x['algoId']==oid for x in current):resolved=True;break
                # Retry only a transport failure after the next loop re-proves
                # this exact target live and unchanged on the still-flat side.
                if not _transport_failure(response):break
            if not resolved:out['cancel_failed'].append(oid)
        current=pending();out['remaining']=[x['algoId'] for x in _scope(current,side)]
        out['scope_verified']=flat()
        out['ok']=out['scope_verified'] and not out['remaining']
        # A later complete read may settle a previously unconfirmed response.
        out['cancel_failed']=[x for x in out['cancel_failed'] if x in out['remaining']]
    except Exception as exc:
        out['read_error']=f'{type(exc).__name__}: {exc}'[:400]
    finally:
        out['elapsed_seconds']=round(clock()-start,3)
    return out
