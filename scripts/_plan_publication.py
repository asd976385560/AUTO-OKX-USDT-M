"""Strict binding of an Agent plan to the deterministic JSON publisher."""
from datetime import datetime
import json
from pathlib import Path
import re

ACTIVATION_CYCLE = "2026-09-13T11:30"

def required(cycle: str) -> bool:
    try: stamp=datetime.strptime(str(cycle),"%Y-%m-%dT%H:%M")
    except (TypeError,ValueError):return False
    return stamp.minute%15==0 and str(cycle)>=ACTIVATION_CYCLE

def receipt_path(plan_path: Path, cycle: str) -> Path:
    return Path(plan_path).with_name("position_plan_publication_"+cycle.replace(":","-")+".json")

def validate(plan_path: Path, cycle: str, plan_sha256: str, facts_hash: str) -> dict:
    if not required(cycle):return {"ok":True,"status":"legacy"}
    try:
        value=json.loads(receipt_path(plan_path,cycle).read_text(encoding="utf-8"))
        if not isinstance(value,dict):raise ValueError("publication must be an object")
        expected={"schema_version":1,"producer":"write_position_plan.py","status":"published","cycle_id":cycle,
                  "plan_sha256":plan_sha256,"facts_hash":facts_hash}
        if any(value.get(k)!=v for k,v in expected.items()):raise ValueError("publication identity/hash mismatch")
        if re.fullmatch(r"[0-9a-f]{64}",str(plan_sha256)) is None or re.fullmatch(r"[0-9a-f]{64}",str(facts_hash)) is None:
            raise ValueError("publication hashes invalid")
        if Path(value.get("plan_file","")).resolve()!=Path(plan_path).resolve():raise ValueError("publication path mismatch")
        return {"ok":True,"status":"verified","attempts":value.get("attempts")}
    except (OSError,ValueError,TypeError) as exc:
        return {"ok":False,"status":"not_published","error":f"{type(exc).__name__}: {exc}"}
