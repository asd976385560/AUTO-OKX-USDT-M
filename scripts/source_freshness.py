# -*- coding: utf-8 -*-
r"""V2.0 §6 —— 源时效审计（registry-aware staleness，根治稀疏源误降级）。

把 `_registry.freshness_report` 接上真实数据：从各数据表**推导**每个 registry 源的真·last_seen。
对有逐源 ``collection_runs`` 的稀疏事件源，来源健康使用最近成功检查时间，同时另存
最近内容事件时间；成功检查但零新事件不能被误报成采集 stale。
再按源 native_cadence 判 stale——周更/工作日更源周末无更新**不算 stale**。
全宇宙批次源以最新精确批次里的最老真实观察时间判鲜，禁止用请求完成时间
或单个较新币掩盖其余币的过期值。

**只读**（不写任何库），供主人触发的 on-demand 维护会话审源健康 / 决定是否灰度改
registry.json。

用法：run_okx_python.ps1 scripts/source_freshness.py --db-root <PROJECT_ROOT>/db
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, _public_project_path('collectors', 'sources'))
sys.path.insert(0, _public_project_path('scripts'))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import _registry  # noqa: E402
from public_macro import (  # noqa: E402
    METRIC_BTC_ETF,
    METRIC_DXY_ECB,
    METRIC_FEAR_GREED,
    METRIC_FED_FUNDS,
    SOURCE_ETF_CONSENSUS,
    source_dates as _public_macro_source_dates,
)

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


def _source_meta_as_of(source_meta: Optional[str], key: str) -> Optional[str]:
    """从 cross_market.source_meta 取独立源日期；date-only 按当日末 CST。"""
    if not source_meta:
        return None
    try:
        meta = json.loads(source_meta)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict) or not isinstance(meta.get(key), dict):
        return None
    raw = str(meta[key].get("source_as_of") or "").strip()
    if not raw:
        return None
    if len(raw) == 10:
        raw += " 23:59:59"
    return _to_cst_str(raw)


def _macro_observation_check_times(
    con: Optional[sqlite3.Connection],
) -> dict[str, Optional[str]]:
    """Return the last successful direct collection time for official macro sources.

    ``observation_date`` is content time and may legitimately remain unchanged while
    FRED/ECB has no newer release.  ``collected_at`` is refreshed only after a valid
    response was parsed and upserted, so it is the appropriate source-health clock.
    ETF evidence is deliberately excluded: re-reading old evidence must not make an
    old market-flow fact look current.
    """
    if con is None:
        return {}
    mapping = {
        "macro_dxy_calc_ecb": METRIC_DXY_ECB,
        "macro_fear_greed": METRIC_FEAR_GREED,
        "macro_fed_funds": METRIC_FED_FUNDS,
    }
    out: dict[str, Optional[str]] = {}
    for source_id, metric in mapping.items():
        out[source_id] = _max_ts(
            con,
            "SELECT MAX(collected_at) FROM macro_observations "
            "WHERE metric=? AND status!='conflict' AND value IS NOT NULL",
            (metric,),
        )
    return out


def _confirmed_etf_content_date(
    con: Optional[sqlite3.Connection],
) -> Optional[str]:
    """Return only the cross-checked hard-field date, never provisional age."""
    return _max_ts(
        con,
        "SELECT MAX(observation_date) FROM macro_observations "
        "WHERE metric=? AND source=? AND status='cross_checked' "
        "AND value IS NOT NULL",
        (METRIC_BTC_ETF, SOURCE_ETF_CONSENSUS),
    )


def _macro_composite_times(
    con: Optional[sqlite3.Connection],
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(last_successful_check, latest_content_date)`` for FRED macro.

    A newly written ``cross_market`` row proves a successful FRED check only when
    USD_BROAD/VIX/SPX are all present and none was carried forward.  If the newest
    row is degraded, scan backward to the last clean row so repeated failures age
    naturally into ``stale`` instead of being hidden by the hourly row timestamp.
    """
    if con is None:
        return None, None
    try:
        rows = con.execute(
            "SELECT ts,dxy,vix,spx,source_meta,carried_forward "
            "FROM cross_market ORDER BY datetime(ts) DESC,rowid DESC LIMIT 2048"
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None

    check_time = None
    content_time = None
    for ts, dxy, vix, spx, source_meta, carried_raw in rows:
        source_as_of = _source_meta_as_of(source_meta, "dxy")
        if content_time is None and source_as_of:
            content_time = source_as_of

        try:
            carried_value = json.loads(carried_raw or "[]")
            if not isinstance(carried_value, list):
                carried_value = None
        except (TypeError, json.JSONDecodeError):
            carried_value = None
        if carried_value is None:
            continue
        carried = {str(value) for value in carried_value}
        if (
            check_time is None
            and source_as_of
            and all(value is not None for value in (dxy, vix, spx))
            and not carried.intersection({"dxy", "vix", "spx"})
        ):
            check_time = _to_cst_str(ts)
        if check_time and content_time:
            break
    return check_time, content_time


def _macro_source_timestamps(
    macro_ts: Optional[str],
    source_meta: Optional[str] = None,
    public_dates: Optional[dict[str, Optional[str]]] = None,
    public_checks: Optional[dict[str, Optional[str]]] = None,
    dxy_check_ts: Optional[str] = None,
    dxy_content_ts: Optional[str] = None,
) -> dict[str, Optional[str]]:
    """Expose source-health clocks separately from underlying content dates."""
    out = {
        mid: macro_ts
        for mid in (
            "macro_dxy_vix_spx",
            "macro_btc_dominance",
            "macro_btc_mcap_change",
            "macro_tvl",
        )
    }
    public_dates = public_dates or {}
    public_checks = public_checks or {}
    for source_id in (
        "macro_dxy_calc_ecb",
        "macro_etf_flow",
        "macro_fear_greed",
        "macro_fed_funds",
    ):
        observed = str(public_dates.get(source_id) or "").strip()
        content_ts = (
            _to_cst_str(observed + " 23:59:59")
            if len(observed) == 10
            else _to_cst_str(observed)
        )
        out[f"{source_id}_content"] = content_ts
        # Direct official fetches use their successful parse/upsert clock for
        # source health.  ETF remains content-timed because old imported evidence
        # is not a successful check of the underlying publishers.
        if source_id in public_checks:
            out[source_id] = (
                _to_cst_str(public_checks.get(source_id)) or content_ts
            )
        else:
            out[source_id] = content_ts

    dxy_content = (
        dxy_content_ts
        or _source_meta_as_of(source_meta, "dxy")
    )
    out["macro_dxy_vix_spx_content"] = dxy_content
    out["macro_dxy_vix_spx"] = dxy_check_ts or dxy_content or macro_ts
    return out


def _news_health_time(
    ledger: Optional[sqlite3.Connection],
    source_id: str,
    content_time: Optional[str],
) -> Optional[str]:
    """Use a successful deterministic check as health, preserving content apart."""
    checked = _to_cst_str(_max_ts(
        ledger,
        "SELECT MAX(ts) FROM collection_runs WHERE source=? "
        "AND status IN ('ok','degraded')",
        (source_id,),
    ))
    return checked or content_time


def derive_last_seen(db_root: Path) -> dict[str, Optional[str]]:
    """按 registry source_id 从真实数据表推导 last_seen（CST 串）。"""
    mkt = _ro(db_root / "market.db")
    reg = _ro(db_root / "regime.db")
    news = _ro(db_root / "news.db")
    ledger = _ro(db_root / "ledger.db")
    ls: dict[str, Optional[str]] = {}
    try:
        # market 源
        ls["okx_tickers"] = _to_cst_str(_max_ts(mkt, "SELECT MAX(ts) FROM tick_snapshots"))
        ls["okx_klines"] = _to_cst_str(_max_ts(mkt, "SELECT MAX(ts) FROM kline_cache"))
        ls["okx_funding"] = _to_cst_str(_max_ts(mkt, "SELECT MAX(ts) FROM derivatives"))
        ls["okx_open_interest"] = _to_cst_str(_max_ts(
            mkt, "SELECT MAX(ts) FROM derivatives WHERE oi_usd IS NOT NULL"))
        ls["okx_mark_price"] = _to_cst_str(_max_ts(
            mkt, "SELECT MAX(ts) FROM derivatives WHERE mark_px IS NOT NULL"))
        ls["okx_index_tickers"] = _to_cst_str(_max_ts(
            mkt, "SELECT MAX(ts) FROM derivatives WHERE index_px IS NOT NULL"))
        ls["okx_orderbook_50"] = _to_cst_str(_max_ts(
            mkt, "SELECT MAX(ts) FROM market_microstructure"))
        ls["okx_recent_trades"] = _to_cst_str(_max_ts(
            mkt, "SELECT MAX(ts) FROM market_trade_flow"))
        # A new request completion or one fresh symbol must not make an old
        # whole-universe batch look fresh.  Use the oldest observation inside
        # the latest exact official batch, matching the decision gate.
        ls["okx_top_long_short"] = _to_cst_str(_max_ts(
            mkt,
            "SELECT MIN(ts) FROM market_positioning "
            "WHERE source='okx_rest_contract_long_short_ratio' "
            "AND collected_ts=(SELECT MAX(collected_ts) "
            "FROM market_positioning WHERE "
            "source='okx_rest_contract_long_short_ratio')",
        ))
        # 合约统计可能包含受限的 previous-batch carry-forward；源新鲜度必须
        # 取原始 observation ``ts``，绝不能用每轮重写的 collected_ts 掩盖老化。
        contract_statistics_source_ts = _to_cst_str(_max_ts(
            mkt,
            "SELECT MAX(ts) FROM market_contract_statistics "
            "WHERE source='okx_rest_contract_oi_taker_15m'",
        ))
        ls["okx_contract_open_interest_history"] = (
            contract_statistics_source_ts)
        ls["okx_contract_taker_volume"] = contract_statistics_source_ts
        ls["okx_instruments"] = _to_cst_str(_max_ts(
            mkt,
            "SELECT MAX(metadata_updated_at) FROM instruments_cache "
            "WHERE metadata_updated_at IS NOT NULL",
        ))
        # macro 源共享 cross_market 行（regime.db 优先）
        macro_ts = _to_cst_str(_max_ts(reg, "SELECT MAX(ts) FROM cross_market")) or \
            _to_cst_str(_max_ts(mkt, "SELECT MAX(ts) FROM cross_market"))
        dxy_check, dxy_content = _macro_composite_times(reg)
        fallback_check, fallback_content = _macro_composite_times(mkt)
        dxy_check = dxy_check or fallback_check
        dxy_content = dxy_content or fallback_content
        # Direct official macro sources expose both the last successful check and
        # their independent content date.  This avoids treating an unchanged
        # upstream release as a dead collector while preserving content age.
        try:
            public_dates = _public_macro_source_dates(reg) if reg else {}
            public_checks = _macro_observation_check_times(reg)
        except sqlite3.OperationalError:
            public_dates = {}
            public_checks = {}
        provisional_etf_date = public_dates.get("macro_etf_flow")
        confirmed_etf_date = _confirmed_etf_content_date(reg)
        public_dates["macro_etf_flow"] = confirmed_etf_date
        ls.update(_macro_source_timestamps(
            macro_ts,
            public_dates=public_dates,
            public_checks=public_checks,
            dxy_check_ts=dxy_check,
            dxy_content_ts=dxy_content,
        ))
        ls["macro_etf_flow_provisional_content"] = _to_cst_str(
            str(provisional_etf_date) + " 23:59:59"
            if provisional_etf_date else None)
        ls["macro_economic_calendar"] = _to_cst_str(_max_ts(
            reg, "SELECT MAX(fetched_at) FROM macro_events"))
        # news 源（按 source 串归桶）。2026-07-03 修：news_items 混 UTC-Z 与 CST-space
        # 两种格式并发写入，裸 MAX 是 TEXT 词典序（同日期 'T'>' ' → Z 行恒胜出即使
        # CST 行更新），混格式源时效最多被低估 ~16h。SQL 侧归一到 CST 再 MAX。
        _n = ("CASE WHEN COALESCE(ingested_at, ts) LIKE '%Z' "
              "THEN datetime(COALESCE(ingested_at, ts), '+8 hours') "
              "ELSE datetime(COALESCE(ingested_at, ts)) END")
        rss_content = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source LIKE 'rss%'"))
        ls["rss_en_content"] = rss_content
        ls["rss_en"] = _news_health_time(ledger, "rss_en", rss_content)
        mx_content = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source LIKE 'mx%'"))
        ls["mx_search_content"] = mx_content
        ls["mx_search"] = _news_health_time(ledger, "mx_search", mx_content)
        geo_content = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source='geo-political'"))
        ls["geo_political_content"] = geo_content
        ls["geo_political"] = _news_health_time(
            ledger, "geo_political", geo_content)
        # 2026-06-27 registry news 源（news_collect 经各 adapter 落库）逐源判时效
        for _nsid in ("odaily", "panews", "jinse", "blockbeats"):
            content_time = _to_cst_str(_max_ts(
                news, f"SELECT MAX({_n}) FROM news_items WHERE source=?",
                (_nsid,)))
            ls[f"{_nsid}_content"] = content_time
            ls[_nsid] = _news_health_time(ledger, _nsid, content_time)
        ls["x_search"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source='x_search'"))
        ls["x_authoritative_supplement"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items "
            "WHERE source='x_search' AND tags LIKE '%authoritative_data%'"))
        okx_news_content = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source='okx_news'"))
        ls["okx_news_content"] = okx_news_content
        ls["okx_news"] = _news_health_time(
            ledger, "okx_news", okx_news_content)
        # 2026-08-13 官方公告源：公告可数日没有新事件。内容时间用于 Agent
        # 判断事件陈旧度，采集健康则使用逐源账本最近一次 ok/degraded 检查；
        # 成功 fetch 返回 0 行仍是完整检查，不能误报成来源 stale。严格 ok-only
        # 完整率继续由 audit_news_source_health 独立判定。
        announcement_content = _to_cst_str(_max_ts(
            news,
            f"SELECT MAX({_n}) FROM news_items WHERE source='okx_announcements'"))
        ls["okx_announcements_content"] = announcement_content
        ls["okx_announcements"] = _news_health_time(
            ledger, "okx_announcements", announcement_content)
    finally:
        for c in (mkt, reg, news, ledger):
            if c:
                c.close()
    return ls


def main() -> int:
    ap = argparse.ArgumentParser(description="registry-aware 源时效审计（只读）")
    ap.add_argument("--db-root", default=_public_project_path('db'))
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
