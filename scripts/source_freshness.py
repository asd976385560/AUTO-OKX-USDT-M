# -*- coding: utf-8 -*-
r"""V2.0 §6 —— 源时效审计（registry-aware staleness，根治稀疏源误降级）。

把 `_registry.freshness_report` 接上真实数据：从各数据表**推导**每个 registry 源的真·last_seen
（ledger.collection_runs 只到采集器粒度、data_source_quality 不每轮更新，故按数据表推导才准），
再按源 native_cadence 判 stale——周更/工作日更源周末无更新**不算 stale**。

**只读**（不写任何库），供主人触发的 on-demand 维护会话审源健康 / 决定是否灰度改
registry.json。

用法：run_okx_python.ps1 scripts/source_freshness.py --db-root <PROJECT_ROOT>/db
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, _project_path('collectors', 'sources'))
sys.path.insert(0, _project_path('scripts'))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import _registry  # noqa: E402

CST = timezone(timedelta(hours=8))


def _ro(p: Path):
    if not p.exists():
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=8)
    return c


def _max_ts(con, sql, args=()) -> Optional[str]:
    if con is None:
        return None
    try:
        r = con.execute(sql, args).fetchone()
        return r[0] if r and r[0] else None
    except sqlite3.OperationalError:
        return None


def _to_cst_str(ts: Optional[str]) -> Optional[str]:
    """各表 ts 多为 UTC ISO('...Z') 或已是 CST 空格串 → 统一成 _registry 期望的 CST '%Y-%m-%d %H:%M:%S'。"""
    if not ts:
        return None
    s = str(ts).strip()
    try:
        if s.endswith("Z") or "T" in s:
            dtu = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dtu.tzinfo is None:
                dtu = dtu.replace(tzinfo=timezone.utc)
            return dtu.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
        # 已是空格格式：假定 CST
        datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return s[:19]
    except (ValueError, TypeError):
        return None


def derive_last_seen(db_root: Path) -> dict[str, Optional[str]]:
    """按 registry source_id 从真实数据表推导 last_seen（CST 串）。"""
    mkt = _ro(db_root / "market.db")
    reg = _ro(db_root / "regime.db")
    news = _ro(db_root / "news.db")
    ls: dict[str, Optional[str]] = {}
    try:
        # market 源
        ls["okx_tickers"] = _to_cst_str(_max_ts(mkt, "SELECT MAX(ts) FROM tick_snapshots"))
        ls["okx_klines"] = _to_cst_str(_max_ts(mkt, "SELECT MAX(ts) FROM kline_cache"))
        ls["okx_funding"] = _to_cst_str(_max_ts(mkt, "SELECT MAX(ts) FROM derivatives"))
        ls["okx_open_interest"] = _to_cst_str(_max_ts(
            mkt, "SELECT MAX(ts) FROM derivatives WHERE oi_usd IS NOT NULL"))
        ls["okx_orderbook_50"] = _to_cst_str(_max_ts(
            mkt, "SELECT MAX(ts) FROM market_microstructure"))
        ls["okx_recent_trades"] = _to_cst_str(_max_ts(
            mkt, "SELECT MAX(ts) FROM market_trade_flow"))
        ls["okx_instruments"] = ls["okx_klines"]  # instruments_cache 无 ts，借慢采节奏代理
        # macro 源共享 cross_market 行（regime.db 优先）
        macro_ts = _to_cst_str(_max_ts(reg, "SELECT MAX(ts) FROM cross_market")) or \
            _to_cst_str(_max_ts(mkt, "SELECT MAX(ts) FROM cross_market"))
        for mid in ("macro_dxy_vix_spx", "macro_btc_dominance", "macro_btc_mcap_change",
                    "macro_etf_flow",
                    "macro_fear_greed", "macro_tvl"):
            ls[mid] = macro_ts
        ls["macro_economic_calendar"] = _to_cst_str(_max_ts(
            reg, "SELECT MAX(fetched_at) FROM macro_events"))
        # news 源（按 source 串归桶）。2026-07-03 修：news_items 混 UTC-Z 与 CST-space
        # 两种格式并发写入，裸 MAX 是 TEXT 词典序（同日期 'T'>' ' → Z 行恒胜出即使
        # CST 行更新），混格式源时效最多被低估 ~16h。SQL 侧归一到 CST 再 MAX。
        _n = ("CASE WHEN COALESCE(ingested_at, ts) LIKE '%Z' "
              "THEN datetime(COALESCE(ingested_at, ts), '+8 hours') "
              "ELSE datetime(COALESCE(ingested_at, ts)) END")
        ls["rss_en"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source LIKE 'rss%'"))
        ls["mx_search"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source LIKE 'mx%'"))
        # 2026-06-27 registry news 源（news_collect 经各 adapter 落库）逐源判时效
        for _nsid in ("odaily", "panews", "jinse", "blockbeats"):
            ls[_nsid] = _to_cst_str(_max_ts(
                news, f"SELECT MAX({_n}) FROM news_items WHERE source=?",
                (_nsid,)))
        ls["x_search"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source='x_search'"))
        ls["okx_news"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source='okx_news'"))
    finally:
        for c in (mkt, reg, news):
            if c:
                c.close()
    return ls


def main() -> int:
    ap = argparse.ArgumentParser(description="registry-aware 源时效审计（只读）")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--registry", default=None)
    args = ap.parse_args()
    reg = _registry.load_registry(args.registry) if args.registry else _registry.load_registry()
    errs = _registry.validate(reg)
    last_seen = derive_last_seen(Path(args.db_root))
    report = _registry.freshness_report(reg, {k: v for k, v in last_seen.items() if v})
    out = {
        "registry_errors": errs,
        "last_seen": last_seen,
        "ok": report["ok"],
        "stale": report["stale"],
        "missing_required": report["missing_required"],
        "missing_optional": report["missing_optional"],
        "skipped_event": report["skipped_event"],
        "should_abort": report["should_abort"],
        "abort_sources": report["abort_sources"],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not report["should_abort"] and not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
