# -*- coding: utf-8 -*-
"""Validate a draft, then atomically publish the canonical plan. No trade/DB writes.

At most one complete draft correction is admitted. Publication shares the
runner's profile/handoff locks; an executing, finalized or revoked plan cannot
be overwritten. The runner still owns all semantic and execution validation.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import sys
import time
for stream in (sys.stdout,sys.stderr):
    if hasattr(stream,"reconfigure"):stream.reconfigure(encoding="utf-8",errors="replace")
import live_position_action_runner as runner
import _acceptance_thresholds as thresholds
import _plan_publication as publication
ROOT=Path(__file__).resolve().parents[1]
CST=timezone(timedelta(hours=8))

def _pairs(pairs):
    out={}
    for key,value in pairs:
        if key in out:raise ValueError("duplicate JSON key: "+key)
        out[key]=value
    return out
def _constant(value):raise ValueError("nonfinite JSON number: "+value)

def publish(cycle: str, *, tmp_root: Path, now: datetime|None=None) -> dict:
    cycle=runner._validated_cycle_id(cycle)
    now=now or datetime.now(CST)
    if now.tzinfo is None:raise ValueError("timezone-aware publisher clock required")
    stamp=datetime.strptime(cycle,"%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    if not stamp<=now<stamp+timedelta(seconds=thresholds.sla_business_terminal_deadline_seconds(cycle)):
        raise ValueError("outside this natural cycle's business deadline")
    tmp=Path(tmp_root).resolve();slug=cycle.replace(":","-")
    draft=tmp/f"position_plan_draft_{slug}.json"
    plan=tmp/f"position_plan_{slug}.json"
    state=tmp/f"live_runner_state_{slug}.json"
    handoff_state=tmp/f"live_runner_handoff_{slug}.json"
    attempts_file=tmp/f"position_plan_validation_{slug}.json"
    facts_file=tmp/f"live_facts_{slug}.json"
    input_handoff=tmp/f"live_input_handoff_{slug}.json"
    view_file=tmp/f"position_exit_view_{slug}.json"
    with runner._runner_cycle_lock(tmp/"live_runner.lock",cycle):
        with runner._runner_cycle_lock(tmp/f"live_runner_handoff_{slug}.lock",cycle):
            if handoff_state.exists():
                h=json.loads(handoff_state.read_text(encoding="utf-8"))
                if h.get("cycle_id")!=cycle or h.get("state")=="revoked" or h.get("revoked") is True:
                    raise ValueError("handoff unavailable or revoked")
            handoff, _ = runner._precheck_stage_owned_live_input_handoff(
                input_handoff,cycle_id=cycle,facts_file=facts_file,decision_view_file=view_file)
            facts=json.loads(facts_file.read_text(encoding="utf-8"))
            facts_hash=str(facts.get("facts_hash") or "")
            if (not facts_hash or handoff.get("facts_hash")!=facts_hash
                    or facts.get("cycle_id")!=cycle or facts.get("profile")!="live"):
                raise ValueError("bound facts identity/hash mismatch")
            previous={}
            if attempts_file.exists():
                previous=json.loads(attempts_file.read_text(encoding="utf-8"))
                if previous.get("cycle_id")!=cycle or previous.get("facts_hash")!=facts_hash:
                    raise ValueError("publisher state/facts identity changed")
            attempts=int(previous.get("attempts") or 0)
            raw=draft.read_bytes()
            if len(raw)>2_000_000:raise ValueError("plan draft too large")
            draft_sha=hashlib.sha256(raw).hexdigest()
            # This check is before any overwrite, including idempotent retries.
            prior_state=json.loads(state.read_text(encoding="utf-8")) if state.exists() else None
            if prior_state is not None and prior_state.get("state")!="failed_preflight":
                raise ValueError("runner already started or finalized; plan is immutable")
            if previous.get("status")=="published" and previous.get("draft_sha256")==draft_sha and prior_state is None:
                existing_sha=hashlib.sha256(plan.read_bytes()).hexdigest()
                result=publication.validate(plan,cycle,existing_sha,facts_hash)
                if result.get("ok") is not True:raise ValueError("published plan drifted")
                return {"ok":True,"status":"already_published","cycle_id":cycle,"plan_file":str(plan),"plan_sha256":existing_sha,"attempts":attempts}
            if attempts>=2:raise ValueError("plan publication correction budget exhausted")
            if previous.get("status")=="invalid_draft" and previous.get("draft_sha256")==draft_sha:
                return {**previous,"ok":False,"may_rewrite":attempts<2,"error":"same invalid draft; rewrite once before retry"}
            attempt={"schema_version":1,"cycle_id":cycle,"facts_hash":facts_hash,"attempts":attempts+1,
                     "draft_sha256":draft_sha,"status":"validating","business_database_writes":0,"orders_sent":0}
            runner._atomic_write_json(attempts_file,attempt)
            try:
                value=json.loads(raw.decode("utf-8-sig"),object_pairs_hook=_pairs,parse_constant=_constant)
                if not isinstance(value,dict) or value.get("cycle_id")!=cycle:
                    raise ValueError("draft cycle/object invalid")
                context=value.get("receipt_context")
                if not isinstance(context,dict) or context.get("cycle_id")!=cycle or not isinstance(value.get("actions"),list):
                    raise ValueError("draft context/actions invalid")
                encoded=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode("utf-8")
                plan_sha=hashlib.sha256(encoded).hexdigest()
                if prior_state is not None:
                    runner._reject_existing_runner_state(state,cycle,facts_hash=facts_hash,plan_sha256=plan_sha)
                if plan.exists() and prior_state is None:
                    raise ValueError("canonical plan already exists without a rewritable preflight state")
            except (ValueError,UnicodeError,runner.PlanError) as exc:
                result={**attempt,"ok":False,"status":"invalid_draft","may_rewrite":attempts+1<2,
                        "error":f"{type(exc).__name__}: {exc}"}
                runner._atomic_write_json(attempts_file,result)
                return result
            # Existing atomic helper uses the same JSON representation above.
            runner._atomic_write_json(plan,value)
            actual=hashlib.sha256(plan.read_bytes()).hexdigest()
            record={**attempt,"ok":True,"producer":"write_position_plan.py","status":"published",
                    "plan_file":str(plan),"plan_sha256":actual,"published_at":now.isoformat()}
            runner._atomic_write_json(publication.receipt_path(plan,cycle),record)
            runner._atomic_write_json(attempts_file,record)
            return record

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycle-id",required=True)
    args=ap.parse_args(argv)
    try:result=publish(args.cycle_id,tmp_root=ROOT/"tmp")
    except (OSError,ValueError,TypeError,runner.PlanError) as exc:
        result={"ok":False,"status":"not_published","may_rewrite":False,"error":f"{type(exc).__name__}: {exc}","business_database_writes":0,"orders_sent":0}
    print(json.dumps(result,ensure_ascii=False,indent=1))
    return 0 if result.get("ok") else 2
if __name__=="__main__":raise SystemExit(main())
