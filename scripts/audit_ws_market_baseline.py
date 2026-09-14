# -*- coding: utf-8 -*-
"""冻结公共行情 WS 改造前的可复现生产基线。"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(_public_project_path())
CST = timezone(timedelta(hours=8))


def percentile(values: Sequence[float], percent: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50_seconds": _round(percentile(values, 0.50)),
        "p95_seconds": _round(percentile(values, 0.95)),
        "p99_seconds": _round(percentile(values, 0.99)),
        "max_seconds": _round(max(values) if values else None),
    }


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def latest_sla_artifact(report_root: Path) -> dict[str, Any] | None:
    candidates = sorted(
        report_root.rglob("complete-cycle-sla.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    path = candidates[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    tier = ((payload.get("strict_sla") or {}).get("pass_rate_tier") or {})
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "generated_at": payload.get("generated_at"),
        "mature_end_inclusive": (payload.get("forward_window") or {}).get(
            "mature_end_inclusive"
        ),
        "tier": tier.get("tier"),
        "target_rate": tier.get("target_rate"),
        "planned_cycles": tier.get("planned_cycles"),
        "strict_cycle_passes": tier.get("strict_cycle_passes"),
        "strict_pass_rate": tier.get("strict_pass_rate"),
        "status": tier.get("status"),
        "definition": {
            "threshold_seconds": (payload.get("definition") or {}).get(
                "threshold_seconds"
            ),
            "stage_gates": (
                ((payload.get("definition") or {}).get("measurement_migration") or {}).get(
                    "stage_gates"
                )
            ),
        },
    }


def collect_baseline(hours: int, now: datetime | None = None) -> dict[str, Any]:
    generated = (now or datetime.now(CST)).astimezone(CST)
    cutoff = generated - timedelta(hours=hours)
    cutoff_cycle = cutoff.strftime("%Y-%m-%dT%H:%M")
    ledger = sqlite3.connect(
        f"file:{(ROOT / 'db' / 'ledger.db').as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    ledger.row_factory = sqlite3.Row
    try:
        rows = ledger.execute(
            """
            SELECT cycle_id,source,status,ts,rows,latency_ms,err
            FROM collection_runs
            WHERE cycle_id>=? AND source IN ('fast','slow','regime')
            ORDER BY cycle_id,source
            """,
            (cutoff_cycle,),
        ).fetchall()
    finally:
        ledger.close()

    by_source: dict[str, list[sqlite3.Row]] = defaultdict(list)
    by_cycle: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_source[str(row["source"])].append(row)
        by_cycle[str(row["cycle_id"])].append(row)

    source_summary = {}
    for source, source_rows in sorted(by_source.items()):
        latencies = [
            float(row["latency_ms"]) / 1000
            for row in source_rows
            if row["latency_ms"] is not None
        ]
        source_summary[source] = {
            **distribution(latencies),
            "status_counts": dict(sorted(Counter(row["status"] for row in source_rows).items())),
            "error_cycles": [
                row["cycle_id"]
                for row in source_rows
                if row["status"] not in {"ok"}
            ],
        }

    hourly_terminal_seconds = []
    hourly_cycles = []
    for cycle_id, cycle_rows in sorted(by_cycle.items()):
        if not cycle_id.endswith(":00"):
            continue
        cycle_start = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
        terminals = []
        statuses = {}
        for row in cycle_rows:
            statuses[row["source"]] = row["status"]
            try:
                terminals.append(
                    datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=CST
                    )
                )
            except (TypeError, ValueError):
                continue
        elapsed = (
            max(0.0, (max(terminals) - cycle_start).total_seconds())
            if terminals
            else None
        )
        if elapsed is not None:
            hourly_terminal_seconds.append(elapsed)
        hourly_cycles.append(
            {"cycle_id": cycle_id, "terminal_seconds": elapsed, "statuses": statuses}
        )

    market_path = ROOT / "db" / "market.db"
    market = sqlite3.connect(
        f"file:{market_path.as_posix()}?mode=ro", uri=True, timeout=20
    )
    try:
        live_symbols = int(
            market.execute(
                "SELECT count(*) FROM instruments_cache "
                "WHERE state='live' AND instId LIKE '%-USDT-SWAP'"
            ).fetchone()[0]
        )
        kline_counts = {
            row[0]: int(row[1])
            for row in market.execute(
                "SELECT tf,count(*) FROM kline_cache GROUP BY tf"
            ).fetchall()
        }
    finally:
        market.close()

    return {
        "schema_version": 1,
        "generated_at_cst": generated.isoformat(),
        "window": {
            "hours": hours,
            "start_cst": cutoff.isoformat(),
            "end_cst": generated.isoformat(),
            "missing_failed_late_retained": True,
        },
        "market": {
            "live_usdt_swap_symbols": live_symbols,
            "market_db_bytes": market_path.stat().st_size,
            "kline_rows_by_timeframe": kline_counts,
            "estimated_rest_candle_requests_per_hour": live_symbols * 9,
        },
        "collection_sources": source_summary,
        "hourly_required_terminal": {
            **distribution(hourly_terminal_seconds),
            "cycles": hourly_cycles,
        },
        "sla": latest_sla_artifact(ROOT / "reports" / "quality"),
        "notes": [
            "Rubik Trading Statistics 没有等价WS，相关失败不得计作本次WS可修复项。",
            "本基线只冻结事实，不登记新SLA、Push或通过率阈值。",
        ],
    }


def markdown(payload: dict[str, Any]) -> str:
    market = payload["market"]
    fast = payload["collection_sources"].get("fast", {})
    slow = payload["collection_sources"].get("slow", {})
    hourly = payload["hourly_required_terminal"]
    sla = payload.get("sla") or {}
    return "\n".join(
        [
            "# OKX 公共行情 WebSocket 改造前48小时基线",
            "",
            f"生成时间（北京时间）：`{payload['generated_at_cst']}`。本工件冻结改造前事实，不改变任何验收口径。",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| Live USDT SWAP | {market['live_usdt_swap_symbols']} |",
            f"| market.db 字节 | {market['market_db_bytes']} |",
            f"| 估算K线REST请求/小时 | {market['estimated_rest_candle_requests_per_hour']} |",
            f"| fast P50/P95/最大（秒） | {fast.get('p50_seconds')} / {fast.get('p95_seconds')} / {fast.get('max_seconds')} |",
            f"| slow P50/P95/最大（秒） | {slow.get('p50_seconds')} / {slow.get('p95_seconds')} / {slow.get('max_seconds')} |",
            f"| 整点必需采集终态 P50/P95/最大（秒） | {hourly.get('p50_seconds')} / {hourly.get('p95_seconds')} / {hourly.get('max_seconds')} |",
            f"| 当前SLA首档 | {sla.get('strict_cycle_passes')}/{sla.get('planned_cycles')}={sla.get('strict_pass_rate')}，{sla.get('status')} |",
            "",
            "## 解释边界",
            "",
            "- 所有失败、缺失和迟到继续留在分母。",
            "- Rubik Trading Statistics 没有等价WebSocket，其SSL或超时不属于本次可回收失败。",
            "- 870秒两道事实闸、Push时效和通过率分档均未修改。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结OKX公共行情WS改造前基线")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()
    payload = collect_baseline(max(1, args.hours))
    json_path = Path(args.json_out)
    md_path = Path(args.md_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(markdown(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "json_out": str(json_path),
                "md_out": str(md_path),
                "live_symbols": payload["market"]["live_usdt_swap_symbols"],
                "fast": payload["collection_sources"].get("fast"),
                "slow": payload["collection_sources"].get("slow"),
                "hourly": {
                    key: payload["hourly_required_terminal"].get(key)
                    for key in ("n", "p50_seconds", "p95_seconds", "max_seconds")
                },
                "sla": payload.get("sla"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
