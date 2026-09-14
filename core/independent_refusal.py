"""Proof of a local no-order refusal; it never authorizes another order."""
from __future__ import annotations
import hashlib,json,math,re
ACTIVATION_CYCLE='2026-09-14T11:15'
KEY='independent_refusal_proofs'

def can_continue(result,action,remaining,cycle):
    if (str(cycle)<ACTIVATION_CYCLE or action.get('action') not in {'OPEN','ADD'}
            or not isinstance(result,dict) or result.get('ok') is not False
            or result.get('action_taken')!='REJECT' or result.get('p0') is not False
            or result.get('symbol')!=action.get('symbol') or result.get('side')!=action.get('side')
            or result.get('trades')!=[] or result.get('reject_reason')!='deterministic_sizing_failed'
            or result.get('reject_detail')!='target_risk_below_minimum_order'
            or result.get('exchange_side_effect_uncertain') is True
            or any(result.get(k) not in (None,'',False,[],{}) for k in ('ordId','ord_id','order_id','submitted_at','applied','unwind','place_result'))
            or any(x.get('symbol')==action.get('symbol') for x in remaining)):
        return False
    sizing=result.get('sizing_intent')
    if not isinstance(sizing,dict) or sizing.get('ok') is not False or sizing.get('error')!='target_risk_below_minimum_order':return False
    try:target,minimum,size=(float(sizing[k]) for k in ('target_risk_usdt','minimum_risk_usdt','minimum_sz'))
    except (KeyError,TypeError,ValueError):return False
    return all(math.isfinite(x) for x in (target,minimum,size)) and 0<target<minimum and size>0

def row_hash(row):
    return hashlib.sha256(json.dumps(row,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')).hexdigest()

def proof_for(row,cycle):
    if not isinstance(row,dict):return None
    continuation=row.get('continuation') or {}
    if not (continuation.get('allowed') is True and continuation.get('same_action_retry') is False
            and continuation.get('scope')=='other_symbols_only'
            and can_continue(row.get('result'),row.get('request') or {},[],cycle)):
        return None
    return {'schema_version':1,'kind':'pre_submit_minimum_risk_refusal','action':row['request']['action'],
            'symbol':row['request']['symbol'],'side':row['request']['side'],
            'failure_sha256':row_hash(row)}

def attach(receipt,failures):
    proofs=[p for row in failures if (p:=proof_for(row,receipt.get('cycle_id'))) is not None]
    if proofs:receipt[KEY]=proofs
    return receipt

def validated_proofs(receipt,cycle):
    """Stored compaction must retain each verified failure's canonical hash."""
    if str(cycle)<ACTIVATION_CYCLE:return []
    proofs=receipt.get(KEY,[])
    if not isinstance(proofs,list):return None
    rows=receipt.get('position_action_failures') or []
    if not isinstance(rows,list):return None
    validated=[]
    for proof in proofs:
        if not (isinstance(proof,dict) and proof.get('schema_version')==1
                and proof.get('kind')=='pre_submit_minimum_risk_refusal'
                and proof.get('action') in {'OPEN','ADD'} and proof.get('symbol')
                and proof.get('side') in {'long','short'}
                and re.fullmatch('[0-9a-f]{64}',str(proof.get('failure_sha256')))):
            return None
        matching=[]
        for row in rows:
            if not isinstance(row,dict):continue
            request=row.get('request') or {}
            if any(request.get(k)!=proof.get(k) for k in ('action','symbol','side')):continue
            if isinstance(row.get('result'),dict):ok=proof_for(row,cycle)==proof
            else:ok=(receipt.get('raw_structurally_truncated') is True or receipt.get('independent_refusal_rows_compacted') is True) and row.get('independent_refusal_sha256')==proof['failure_sha256']
            if ok:matching.append(row)
        if len(matching)!=1 or proof in validated:return None
        validated.append(proof)
    return validated

def continuable_interim(receipt,cycle):
    proofs=validated_proofs(receipt,cycle)
    return bool(proofs and len(proofs)==len(receipt.get('position_action_failures') or [])
                and receipt.get('runner_in_progress') is True and receipt.get('batch_ok') is False)
