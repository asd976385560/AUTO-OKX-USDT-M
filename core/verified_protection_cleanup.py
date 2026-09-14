"""Cancel replaced protection only while a fixed surviving SL covers the same position."""
from __future__ import annotations
import time
from core.flat_protection_cleanup import _number,_orders,_scope,_fingerprint,_transport_failure

def cleanup_superseded(symbol,side,original_rows,*,survivor_id,expected_position,
                       expected_sl,expected_tp=None,read_orders,read_positions,
                       cancel_order,budget_seconds=45,clock=None,sleep=None):
    clock=clock or time.monotonic;sleep=sleep or time.sleep
    started=clock();deadline=started+max(0,float(budget_seconds))
    ids=[str(x.get('algoId') or '') for x in original_rows]
    out={'ok':False,'remaining':ids[:],'cancel_requested':[],'attempts':[],
         'survivor_id':survivor_id,'read_error':None,'max_attempts_per_id':2,
         'budget_seconds':max(0,float(budget_seconds))}
    def budget():
        left=deadline-clock()
        if left<=0.1:raise TimeoutError('superseded_cleanup_budget_exhausted')
        return left
    def pending():
        rows=_orders(read_orders(budget()),symbol);budget();return rows
    expected_epoch=None
    survivor_fp=None
    def covered(rows):
        nonlocal survivor_fp
        response=read_positions(budget());budget()
        if not isinstance(response,dict) or response.get('ok') is not True or not isinstance(response.get('data'),list):raise ValueError('positions_read_failed')
        matches=[]
        for row in response['data']:
            if not isinstance(row,dict) or not row.get('instId'):raise ValueError('position_identity_missing')
            size=_number(row.get('pos'))
            if size and row.get('posSide') not in ('long','short'):raise ValueError('position_side_invalid')
            if size and row['instId']==symbol and row.get('posSide')==side:matches.append(row)
        if len(matches)!=1:raise ValueError('position_missing_or_ambiguous')
        position=matches[0]
        epoch=(str(position.get('posId') or ''),str(position.get('cTime') or ''),_number(position['pos']))
        if not all(expected_epoch[:2]) or epoch!=expected_epoch or epoch[2]<=0:raise ValueError('position_epoch_changed')
        kept=next((x for x in _scope(rows,side) if x['algoId']==survivor_id),None)
        if kept is None:raise ValueError('surviving_sl_missing')
        sl=_number(kept.get('slTriggerPx'));mark=_number(position.get('markPx'))
        if (kept.get('slTriggerPxType')!='mark' or sl!=_number(expected_sl)
                or _number(kept.get('sz'))<epoch[2] or sl<=0 or mark<=0
                or not (sl<mark if side=='long' else sl>mark)):
            raise ValueError('surviving_sl_not_full_or_invalid')
        if expected_tp is not None:
            tp=_number(kept.get('tpTriggerPx'))
            if (tp!=_number(expected_tp) or kept.get('tpTriggerPxType')!='mark'
                    or not (tp>mark if side=='long' else tp<mark)):
                raise ValueError('surviving_tp_not_verified')
        fingerprint=_fingerprint(kept)
        if survivor_fp is not None and fingerprint!=survivor_fp:raise ValueError('surviving_protection_changed')
        survivor_fp=fingerprint
    try:
        expected_epoch=(str(expected_position.get('posId') or ''),str(expected_position.get('cTime') or ''),_number(expected_position.get('sz')))
        if side not in ('long','short') or not survivor_id or not ids or len(set(ids))!=len(ids) or '' in ids or survivor_id in ids:raise ValueError('cleanup_identity_invalid')
        rows=pending();covered(rows)
        baselines={}
        for original in original_rows:
            oid=str(original['algoId']);row=next((x for x in _scope(rows,side) if x['algoId']==oid),None)
            if row is None:
                if any(x['algoId']==oid for x in rows):raise ValueError('stale_scope_changed')
                continue
            for key in ('sz','slTriggerPx','tpTriggerPx','cTime'):
                if _number(row.get(key) or 0)!=_number(original.get(key) or 0):raise ValueError('stale_protection_changed:'+oid)
            baselines[oid]=_fingerprint(row)
        for oid in ids:
            for attempt in range(2):
                rows=pending();covered(rows)
                row=next((x for x in rows if x['algoId']==oid),None)
                if row is None:break
                if oid not in baselines or _fingerprint(row)!=baselines[oid]:raise ValueError('stale_protection_changed:'+oid)
                if budget()<10:raise TimeoutError('insufficient_cancel_and_readback_budget')
                out['cancel_requested'].append(oid)
                try:response=cancel_order(oid,budget())
                except Exception as exc:response={'ok':False,'error':str(exc),'error_type':type(exc).__name__}
                if not isinstance(response,dict):response={'ok':False,'error':'invalid_cancel_response'}
                out['attempts'].append({'algoId':oid,'attempt':attempt+1,'response':response,'fresh_survivor_and_epoch_verified':True})
                if budget()>1.2:sleep(1)
                rows=pending();covered(rows)
                if not any(x['algoId']==oid for x in rows):break
                if not _transport_failure(response):break
        rows=pending();covered(rows)
        out['remaining']=[oid for oid in ids if any(x['algoId']==oid for x in rows)]
        out['ok']=not out['remaining']
    except Exception as exc:out['read_error']=f'{type(exc).__name__}: {exc}'[:400]
    finally:out['elapsed_seconds']=round(clock()-started,3)
    return out
