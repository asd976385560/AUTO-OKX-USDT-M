# -*- coding: utf-8 -*-
"""采集交易所账单（仅 live），沉淀手续费、资金费、已实现盈亏与余额变化。

2026-08-06 demo 全量下线修：`--profiles` 原默认 `"live,demo"`，而
`daily_maintenance.py` 调它时**不传该参数**——也就是说每天 07:55 都会照默认
去拉一次 demo 账单，并往 `account_bills` 插 `profile='demo'` 行。demo 历史行
当天刚被清掉 2459 条，这条链会一天天把它们重新灌回来（OKX 的 demo 账户还在，
凭证也还在 CLI 配置里，调用会成功）。默认改为 `"live"`，并对 demo 直接拒绝。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, r".\collectors")
import ledger  # noqa: E402

from _okxcli import okx_json

CST = timezone(timedelta(hours=8))
_REQUEST_TIMEOUT_SECONDS = 45
_COLD_RETRY_DELAY_SECONDS = 3.0
_MAX_FETCH_ATTEMPTS = 2


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


def collect(
    profile: str,
    limit: int,
    *,
    retry_stats: dict | None = None,
    sleep_fn=time.sleep,
) -> list[tuple]:
    """Fetch the current bill page with one bounded cold recovery attempt.

    A missing bill page blocks the daily report because fees, funding and
    realized PnL would otherwise be incomplete.  The retry is deliberately
    limited to the same current request: it does not page backwards, backfill
    history, or turn a persistent exchange failure into a successful result.
    """
    if "demo" in str(profile).strip().lower():
        raise ValueError(
            f"collect_account_bills 只支持 live，收到 {profile!r}。"
            "demo 已于 2026-08-06 全量下线，账单历史行也已清除；"
            "再采会把 profile='demo' 行重新写回 account_bills。")
    started = time.monotonic()
    initial_error: Exception | None = None
    try:
        payload = okx_json(
            "account", "bills", "--instType", "SWAP", "--limit", str(limit),
            global_args=["--profile", profile],
            timeout_sec=_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the final transport error
        initial_error = exc
        sleep_fn(_COLD_RETRY_DELAY_SECONDS)
        try:
            payload = okx_json(
                "account", "bills", "--instType", "SWAP", "--limit", str(limit),
                global_args=["--profile", profile],
                timeout_sec=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as final_exc:  # noqa: BLE001
            if retry_stats is not None:
                retry_stats.update({
                    "attempts": _MAX_FETCH_ATTEMPTS,
                    "recovered_after_cold_retry": False,
                    "cold_retry_delay_seconds": _COLD_RETRY_DELAY_SECONDS,
                    "initial_error": (
                        f"{type(initial_error).__name__}: {initial_error}")[:240],
                    "final_error": (
                        f"{type(final_exc).__name__}: {final_exc}")[:240],
                    "historical_retry": False,
                    "unbounded_retry": False,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                })
            raise
    if retry_stats is not None:
        retry_stats.update({
            "attempts": 2 if initial_error is not None else 1,
            "recovered_after_cold_retry": initial_error is not None,
            "cold_retry_delay_seconds": (
                _COLD_RETRY_DELAY_SECONDS if initial_error is not None else 0.0),
            "initial_error": (
                f"{type(initial_error).__name__}: {initial_error}"[:240]
                if initial_error is not None else None),
            "historical_retry": False,
            "unbounded_retry": False,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        })
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
    ap = argparse.ArgumentParser(description="实盘账单采集")
    ap.add_argument("--db-root", default=r".\db")
    ap.add_argument("--profiles", default="live")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    all_rows = []
    errors = []
    retry_stats = {}
    for profile in profiles:
        profile_retry_stats: dict = {}
        try:
            all_rows.extend(collect(
                profile,
                max(1, min(args.limit, 100)),
                retry_stats=profile_retry_stats,
            ))
        except Exception as exc:
            errors.append(f"{profile}:{type(exc).__name__}:{exc}")
        finally:
            retry_stats[profile] = profile_retry_stats
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
        "errors": errors, "retry_stats": retry_stats,
    }, ensure_ascii=False))
    return 0 if all_rows or not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
