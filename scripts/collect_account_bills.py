# -*- coding: utf-8 -*-
"""采集交易所账单，沉淀手续费、资金费、已实现盈亏与余额变化。"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
import ledger  # noqa: E402

from _okxcli import okx_json

CST = timezone(timedelta(hours=8))


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, CST).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def collect(profile: str, limit: int) -> list[tuple]:
    payload = okx_json(
        "account", "bills", "--instType", "SWAP", "--limit", str(limit),
        global_args=["--profile", profile], timeout_sec=45,
    )
    items = payload if isinstance(payload, list) else (payload.get("data") or [])
    fetched_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for item in items:
        bill_id = str(item.get("billId") or "")
        ts = fmt_ms(item.get("ts") or item.get("fillTime"))
        if not bill_id or not ts:
            continue
        rows.append((
            profile, bill_id, ts, item.get("instId"), item.get("instType"),
            item.get("ccy"), str(item.get("type") or ""), str(item.get("subType") or ""),
            to_float(item.get("balChg")), to_float(item.get("fee")),
            to_float(item.get("pnl")), to_float(item.get("interest")),
            to_float(item.get("px")), to_float(item.get("sz")),
            item.get("ordId"), item.get("tradeId"), item.get("execType"),
            fetched_at, json.dumps(item, ensure_ascii=False),
        ))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="实盘/模拟盘账单采集")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--profiles", default="live,demo")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    all_rows = []
    errors = []
    for profile in profiles:
        try:
            all_rows.extend(collect(profile, max(1, min(args.limit, 100))))
        except Exception as exc:
            errors.append(f"{profile}:{type(exc).__name__}:{exc}")
    con = ledger.connect(Path(args.db_root) / "account.db")
    try:
        before = con.total_changes
        con.executemany(
            "INSERT OR IGNORE INTO account_bills "
            "(profile,bill_id,ts,inst_id,inst_type,ccy,type,subtype,bal_change,fee,pnl,"
            "interest,px,sz,ord_id,trade_id,exec_type,fetched_at,raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            all_rows,
        )
        inserted = con.total_changes - before
        con.commit()
    finally:
        con.close()
    print(json.dumps({
        "ok": bool(all_rows) or not errors,
        "profiles": profiles, "fetched": len(all_rows), "inserted": inserted,
        "errors": errors,
    }, ensure_ascii=False))
    return 0 if all_rows or not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
