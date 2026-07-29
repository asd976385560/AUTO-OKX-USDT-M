# -*- coding: utf-8 -*-
"""V2.0 §6 registry 驱动新闻采集器（替「单跑 news_rss 一个源」的旧 cron）。

读 collectors/sources/registry.json → 迭代 enabled 的 type=news 源（其 adapter 模块存在者）
→ 逐源 adapter.collect(news.db, apply) 经 news_writer 落库 → 聚合回执 + 每源记
ledger.collection_runs(source=<id>)（让 freshness_report 可逐源判时效）。

不迭代：x_search(adapter=EXTERNAL_SCOUT，news-scout 独立 cron 负责)、无 adapter 模块的源。
okx_news、mx_search、geo_political 均在本链通过各自 adapter 经 news_writer 落库。

失败隔离（§6/§10）：任一源失败/超时只标该源，不影响其它源。零模型名（红线①）；UTF-8 无 BOM。
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


import argparse
import importlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

_SRC = Path(__file__).resolve().parent            # collectors/sources
_COLLECTORS = _SRC.parent                          # collectors
for _p in (str(_SRC), str(_COLLECTORS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import _registry          # noqa: E402
import ledger             # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SKIP_ADAPTERS = {"EXTERNAL_SCOUT"}


def _load_adapter(name: str):
    """import collectors/sources/<name>.py（存在才 import）；缺/导入失败 → None（跳过）。"""
    if not name or not (_SRC / f"{name}.py").exists():
        return None
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _source_due(source: dict, cycle_id: str) -> bool:
    """按 cycle 槽判断分源轮询是否到期；未配置时保持每个 news cron 都采。"""
    interval_min = source.get("poll_interval_min")
    if interval_min is None:
        return True
    try:
        slot = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M")
        minute_of_day = slot.hour * 60 + slot.minute
        return minute_of_day % int(interval_min) == 0
    except (TypeError, ValueError):
        return True  # registry validate 会报非法值；运行期 fail-safe 保持旧行为


def collect_all(db_root: str, apply: bool = False,
                registry_path: str | None = None) -> dict:
    reg = _registry.load_registry(registry_path or _registry.DEFAULT_REGISTRY)
    news_db = str(Path(db_root) / "news.db")
    ledger_db = str(Path(db_root) / "ledger.db")
    cycle_id = ledger.cycle_id_for()
    sources = _registry.enabled_sources(reg, type_="news")
    results = []
    total_inserted = 0
    total_fetched = 0
    for s in sources:
        sid = s.get("id")
        adapter = s.get("adapter")
        if not _source_due(s, cycle_id):
            results.append({
                "id": sid,
                "adapter": adapter,
                "status": "skipped",
                "why": f"poll_interval_min={s.get('poll_interval_min')}",
            })
            continue
        if not adapter or adapter in SKIP_ADAPTERS:
            results.append({"id": sid, "status": "skipped", "why": "external/no-adapter"})
            continue
        mod = _load_adapter(adapter)
        if mod is None or not hasattr(mod, "collect"):
            results.append({"id": sid, "adapter": adapter, "status": "skipped",
                            "why": "module-missing"})
            continue
        t0 = time.time()
        try:
            res = mod.collect(news_db, apply=apply)
            dur_ms = int((time.time() - t0) * 1000)
            fetched = int(res.get("fetched") or 0)
            inserted = res.get("inserted") if apply else None
            total_fetched += fetched
            total_inserted += int(inserted or 0)
            status = "ok" if (res.get("ok") and fetched > 0) else (
                "degraded" if res.get("ok") else "failed")
            # 可观测性（2026-07-06）：adapter 返回的失败简因（HTTP 码/超时，见
            # news_panews.collect 的 err 键；news_writer 失败的 error 键）随账本
            # err 列必须落库，便于区分限流、网络和解析失败。
            err_txt = res.get("err") or res.get("error")
            err_txt = str(err_txt)[:150] if err_txt else None
            entry = {"id": sid, "adapter": adapter, "status": status,
                     "fetched": fetched, "inserted": inserted, "dur_ms": dur_ms}
            if err_txt:
                entry["err"] = err_txt
            results.append(entry)
            if apply:
                ledger.record_collection(
                    ledger_db, cycle_id, sid, status,
                    rows=(inserted if inserted is not None else fetched),
                    latency_ms=dur_ms, err=err_txt)
        except Exception as e:  # noqa: BLE001 —— 失败隔离
            dur_ms = int((time.time() - t0) * 1000)
            results.append({"id": sid, "adapter": adapter, "status": "failed",
                            "err": str(e)[:160], "dur_ms": dur_ms})
            if apply:
                try:
                    ledger.record_collection(ledger_db, cycle_id, sid, "failed",
                                             latency_ms=dur_ms, err=str(e)[:160])
                except Exception:
                    pass
    return {"ok": True, "cycle_id": cycle_id, "apply": apply,
            "total_fetched": total_fetched, "total_inserted": total_inserted,
            "sources": results}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="V2.0 registry 驱动新闻采集（迭代 enabled type=news 源）")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--registry", default=None)
    args = ap.parse_args()
    res = collect_all(args.db_root, apply=args.apply, registry_path=args.registry)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
