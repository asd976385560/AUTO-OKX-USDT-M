# -*- coding: utf-8 -*-
"""V2.0 §6 —— 源注册表读/校验 + 按源原生节奏判时效（registry-aware staleness）。

registry.json 现役消费方＝news_collect（news 源迭代）+ ledger.gate_collection_fresh
（慢源时效判定）+ scripts/source_freshness.py；market/macro 采集仍在 collect_data/
collect_slow 内声明（registry 对其仅登记，采集器暂未改读）。
**时效按源原生节奏判**（关键，根治稀疏源误降级）：
周更/工作日更/日更源在周末或非更新日无更新**不算 stale**；只有「超过该源应更新周期
仍无」才标 stale。event 源（X 突发）不进 required、失败不阻断。

零模型名（红线 #1）；纯逻辑（load 外无 IO）。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[2]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"

DEFAULT_REGISTRY = Path(os.environ.get(
    "OKX_REGISTRY_PATH", _project_path('collectors', 'sources', 'registry.json')))

VALID_TYPES = {"market", "news", "macro", "social"}
VALID_CADENCE = {"15m", "hourly", "daily", "weekday", "weekly", "event"}

# cadence → 默认 staleness（秒）。daily 给 26h 容当日晚更；weekday 另叠周末宽限。
DEFAULT_STALENESS = {
    "15m": 1800,       # 2 cycles
    "hourly": 7200,    # 2h
    "daily": 93600,    # 26h
    "weekday": 93600,  # 26h + 周末宽限（见 is_stale）
    "weekly": 691200,  # 8d
    "event": None,     # 永不 stale
}

FAST_CADENCES = {"15m"}
SLOW_CADENCES = {"hourly", "daily", "weekday", "weekly"}


# ---------------------------------------------------------------------------
# load / validate
# ---------------------------------------------------------------------------
def load_registry(path: str | os.PathLike = DEFAULT_REGISTRY) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    sources = registry.get("sources")
    if not isinstance(sources, list):
        return ["registry 缺 sources 列表"]
    seen_ids: set[str] = set()
    for i, s in enumerate(sources):
        sid = s.get("id")
        if not sid:
            errors.append(f"sources[{i}] 缺 id")
            continue
        if sid in seen_ids:
            errors.append(f"重复 id: {sid}")
        seen_ids.add(sid)
        if s.get("type") not in VALID_TYPES:
            errors.append(f"{sid} type 非法: {s.get('type')}")
        if s.get("native_cadence") not in VALID_CADENCE:
            errors.append(f"{sid} native_cadence 非法: {s.get('native_cadence')}")
        if not isinstance(s.get("required", False), bool):
            errors.append(f"{sid} required 须 bool")
        if not isinstance(s.get("enabled", True), bool):
            errors.append(f"{sid} enabled 须 bool")
        poll_min = s.get("poll_interval_min")
        if poll_min is not None:
            if (isinstance(poll_min, bool) or not isinstance(poll_min, int)
                    or poll_min < 15 or poll_min > 1440
                    or poll_min % 15 != 0 or 1440 % poll_min != 0):
                errors.append(
                    f"{sid} poll_interval_min 须为可整除一天的 15 分钟倍数"
                    f"（15..1440）: {poll_min}")
    # 至少 1 个 enabled 的 news required（§6 一期约束）
    news_req = [s for s in sources if s.get("type") == "news"
                and s.get("required") and s.get("enabled")]
    if not news_req:
        errors.append("无 enabled 的 news required 源（§6 一期要求至少 1 个）")
    return errors


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------
def enabled_sources(registry: dict[str, Any], type_: Optional[str] = None,
                    cadence_in: Optional[set[str]] = None) -> list[dict[str, Any]]:
    out = []
    for s in registry.get("sources", []):
        if not s.get("enabled"):
            continue
        if type_ is not None and s.get("type") != type_:
            continue
        if cadence_in is not None and s.get("native_cadence") not in cadence_in:
            continue
        out.append(s)
    return out


def fast_sources(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """快采迭代：15m/即时源（排除 EXTERNAL_SCOUT/event——scout 独立 cron）。"""
    return [s for s in enabled_sources(registry, cadence_in=FAST_CADENCES)
            if s.get("adapter") != "EXTERNAL_SCOUT"]


def slow_sources(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """慢采迭代：hourly/daily/weekday/weekly 源。"""
    return [s for s in enabled_sources(registry, cadence_in=SLOW_CADENCES)
            if s.get("adapter") != "EXTERNAL_SCOUT"]


def required_ids(registry: dict[str, Any]) -> set[str]:
    return {s["id"] for s in registry.get("sources", [])
            if s.get("required") and s.get("enabled")}


# ---------------------------------------------------------------------------
# registry-aware staleness（核心）
# ---------------------------------------------------------------------------
def _parse_cst(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts, TS_FMT).replace(tzinfo=CST)
    except (ValueError, TypeError):
        return None


def _weekend_days_between(t0: datetime, t1: datetime) -> int:
    """(t0, t1] 间的周末日历日数（Sat=5/Sun=6）。"""
    if t1 <= t0:
        return 0
    n = 0
    d = t0.date()
    end = t1.date()
    while d < end:
        d = d + timedelta(days=1)
        if d.weekday() >= 5:
            n += 1
    return n


def default_staleness(cadence: str) -> Optional[int]:
    return DEFAULT_STALENESS.get(cadence)


def is_stale(cadence: str, last_ts: Optional[str], now: Optional[datetime] = None,
             threshold_sec: Optional[int] = None) -> bool:
    """按源原生节奏判 stale。event→永不 stale；weekday→叠周末宽限。

    last_ts=None（从未采到）→ 视为 stale（由 freshness_report 区分 required/非）。
    """
    if cadence == "event":
        return False
    now = now or datetime.now(CST)
    t = _parse_cst(last_ts) if last_ts else None
    if t is None:
        return True
    base = threshold_sec if threshold_sec is not None else default_staleness(cadence)
    if base is None:
        return False
    allowance = base
    if cadence == "weekday":
        # 周末非更新日：每个周末日加 1 天宽限（Fri 更新跨周末到 Mon 不算 stale）
        allowance += _weekend_days_between(t, now) * 86400
    age = (now - t).total_seconds()
    return age > allowance


def freshness_report(registry: dict[str, Any], last_seen: dict[str, str],
                     now: Optional[datetime] = None) -> dict[str, Any]:
    """对 enabled 源逐一判时效。

    last_seen: {source_id: last_ts_cst_str}（来自 ledger.collection_runs / data_source_quality）。
    返回 {ok, stale, missing_required, missing_optional, skipped_event}。
    **缺非必需源/稀疏源未到更新期 → 不进 stale**（不误降级）。
    """
    now = now or datetime.now(CST)
    ok, stale, miss_req, miss_opt, skipped = [], [], [], [], []
    for s in registry.get("sources", []):
        if not s.get("enabled"):
            continue
        sid = s["id"]
        cadence = s.get("native_cadence")
        if cadence == "event":
            skipped.append(sid)
            continue
        last = last_seen.get(sid)
        if last is None:
            (miss_req if s.get("required") else miss_opt).append(sid)
            continue
        if is_stale(cadence, last, now, s.get("staleness_sec")):
            stale.append(sid)
        else:
            ok.append(sid)
    # abort 条件：必需源缺或 stale
    req = required_ids(registry)
    abort = sorted((set(miss_req) | (set(stale) & req)))
    return {"ok": ok, "stale": stale, "missing_required": miss_req,
            "missing_optional": miss_opt, "skipped_event": skipped,
            "abort_sources": abort, "should_abort": bool(abort)}


def _main() -> int:
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="V2.0 源注册表校验/查询")
    ap.add_argument("--path", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()
    reg = load_registry(args.path)
    errs = validate(reg)
    print(json.dumps({
        "n_sources": len(reg.get("sources", [])),
        "fast": [s["id"] for s in fast_sources(reg)],
        "slow": [s["id"] for s in slow_sources(reg)],
        "required": sorted(required_ids(reg)),
        "errors": errs,
    }, ensure_ascii=False, indent=2))
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(_main())
