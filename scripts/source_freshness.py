# -*- coding: utf-8 -*-
r"""V2.0 §6 —— 源时效审计（registry-aware staleness，根治稀疏源误降级）。

把 `_registry.freshness_report` 接上真实数据：从各数据表**推导**每个 registry 源的真·last_seen
（ledger.collection_runs 只到采集器粒度、data_source_quality 不每轮更新，故按数据表推导才准），
再按源 native_cadence 判 stale——周更/工作日更源周末无更新**不算 stale**。

**只读**（不写任何库），供主人触发的 on-demand 维护会话审源健康 / 决定是否灰度改
registry.json。

用法：run_okx_python.ps1 scripts/source_freshness.py --db-root ./db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, r"./collectors/sources")
sys.path.insert(0, r"./scripts")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import _registry  # noqa: E402
from public_macro import source_dates as _public_macro_source_dates  # noqa: E402

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


def _macro_source_timestamps(
    macro_ts: Optional[str],
    source_meta: Optional[str] = None,
    public_dates: Optional[dict[str, Optional[str]]] = None,
) -> dict[str, Optional[str]]:
    """公共行与独立日频事实分别赋真时效；禁止借共享行伪造新鲜。"""
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
    for source_id in (
        "macro_dxy_calc_ecb",
        "macro_etf_flow",
        "macro_fear_greed",
    ):
        observed = str(public_dates.get(source_id) or "").strip()
        out[source_id] = (
            _to_cst_str(observed + " 23:59:59")
            if len(observed) == 10
            else _to_cst_str(observed)
        )
    # DXY 的日频源日期已写在 source_meta；公共 cross_market 行每小时更新，不能
    # 用它掩盖 DXY 本身多日未更新。组合源以已知最慢成员 DXY 的日期为准。
    out["macro_dxy_vix_spx"] = (
        _source_meta_as_of(source_meta, "dxy") or macro_ts
    )
    return out


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
        ls["okx_top_long_short"] = _to_cst_str(_max_ts(
            mkt, "SELECT MAX(collected_ts) FROM market_positioning"))
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
        ls["okx_instruments"] = ls["okx_klines"]  # instruments_cache 无 ts，借慢采节奏代理
        # macro 源共享 cross_market 行（regime.db 优先）
        macro_ts = _to_cst_str(_max_ts(reg, "SELECT MAX(ts) FROM cross_market")) or \
            _to_cst_str(_max_ts(mkt, "SELECT MAX(ts) FROM cross_market"))
        macro_meta = _max_ts(
            reg,
            "SELECT source_meta FROM cross_market "
            "ORDER BY datetime(ts) DESC, rowid DESC LIMIT 1",
        ) or _max_ts(
            mkt,
            "SELECT source_meta FROM cross_market "
            "ORDER BY datetime(ts) DESC, rowid DESC LIMIT 1",
        )
        # Alternative.me / ECB复算DXY / ETF证据各读 macro_observations 自身日期，
        # 不借 cross_market 每小时公共行掩盖旧值。
        try:
            public_dates = _public_macro_source_dates(reg) if reg else {}
        except sqlite3.OperationalError:
            public_dates = {}
        ls.update(_macro_source_timestamps(macro_ts, macro_meta, public_dates))
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
        ls["geo_political"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source='geo-political'"))
        # 2026-06-27 registry news 源（news_collect 经各 adapter 落库）逐源判时效
        for _nsid in ("odaily", "panews", "jinse", "blockbeats"):
            ls[_nsid] = _to_cst_str(_max_ts(
                news, f"SELECT MAX({_n}) FROM news_items WHERE source=?",
                (_nsid,)))
        ls["x_search"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source='x_search'"))
        ls["x_authoritative_supplement"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items "
            "WHERE source='x_search' AND tags LIKE '%authoritative_data%'"))
        ls["okx_news"] = _to_cst_str(_max_ts(
            news, f"SELECT MAX({_n}) FROM news_items WHERE source='okx_news'"))
    finally:
        for c in (mkt, reg, news):
            if c:
                c.close()
    return ls


def main() -> int:
    ap = argparse.ArgumentParser(description="registry-aware 源时效审计（只读）")
    ap.add_argument("--db-root", default=r"./db")
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
