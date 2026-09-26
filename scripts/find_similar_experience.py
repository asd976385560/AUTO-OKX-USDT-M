# -*- coding: utf-8 -*-
"""V2.0 §8.5 —— 交易经验检索（LLM 决策的长期记忆）。

新判断前必搜相似经验：读 account.db.trade_experiences（已平仓、有 pnl），在前向
v4 特征空间（2026-09-26 对照 V3 experience::similarity：1H ATR% / RSI / EMA 排列 /
4h·16h 收益 / 成交额 z / 止损距离 / 开仓小时，几何平均贴近度，异币 ×0.9）计算
相似度，分别返回盈利、亏损与统计摘要。历史 v1/v2/v3 行按行 ts 从 kline_cache
现算 v4 特征后同场比较（V3 对 legacy 行同一做法：有什么特征比什么）；只有一个
可比特征都没有的行才被排除并在 feature_epoch_filter 外显。
**只采不拦**：所有历史数据只供 Agent 自主裁决；Agent 必须说明 adopt/partial/ignore/none，
但历史结果、可信度和样本数均不能自动批准或否决交易。

可信度公式（4 因子，主人拍板）：
  credibility = wr_at_sim × conf_sim × age_decay × min(n/20, 1.0)
    wr_at_sim = sim≥min_sim 邻居中 pnl>0 占比
    conf_sim  = 邻居平均 sim（裁掉 <min_sim 后）
    age_decay = 0.5 ^ (age_days / 60)（半衰期 60 天，用邻居平均 age）
    n         = 入算邻居数

样本充足（n≥5）的 scope 另给（2026-09-26 对照 V3 similarity 模块补齐，只展示不设闸）：
  win_rate_lo95       胜率的 Wilson 95% 下界（对小样本不撒谎）
  sim_weighted        贴近度加权的胜率 / 平均 pnl_pct（权重 = 各邻居 sim）
  avg_realized_r_net  有路径埋点邻居的净 R 均值，realized_r_n 为其样本数

min_sim 缺省 0.35（V3 同值；几何平均的贴近度尺度低于旧 v2/v3 的算术平均）。
豁免阈值（拍板「无足够样本」）：n<3 或所有 sim<min_sim → summary.sufficient=False；
cred<0.2 标 low_credibility（briefing 显式标，禁凭单条低相似锁决策）。找不到→不另加限制，
只走 §7 硬上限。

不复用 find_similar_history（特征空间/输出 schema/前向窗口全异）。
零模型名（红线 #1）。
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
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
import _simutil  # noqa: E402
from core.ev_calculator import (  # noqa: E402  p_win 样本门与 EV 单一真源
    MIN_P_SAMPLE_N,
    build_ev_check,
)
from core.policy_epochs import (  # noqa: E402  纪元边界单一真源
    CURRENT_EPOCH,
    EXCLUDED_FROM_EV_PRIOR,
    is_ev_prior_eligible,
    policy_epoch,
)
from core.experience_contract import (  # noqa: E402
    build_contract,
    normalize_symbol,
    normalize_token,
    setup_from_metrics,
    setup_from_prices,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
DEFAULT_DB_ROOT = Path(_public_project_path('db'))
_LEGACY_SCORE_MARKERS = (
    re.compile(
        r"[\"']?(?:score(?:_total)?|total|conf(?:idence)?)[\"']?"
        r"\s*[=:]?\s*-?\d+(?:\.\d+)?%?",
        re.IGNORECASE,
    ),
    re.compile(r"(?:评分|置信度)\s*[=:：]?\s*-?\d+(?:\.\d+)?%?"),
)
_LEGACY_1R_RE = re.compile(r"(?i)\bhit[_ ]?1r\b")


def _without_legacy_scores(value: Any) -> str | None:
    """隐藏评分兼容标记，保留历史事实和定性教训。"""
    if value is None:
        return None
    text = str(value)
    for pattern in _LEGACY_SCORE_MARKERS:
        text = pattern.sub("", text)
    text = _LEGACY_1R_RE.sub("legacy_r_metric", text)
    return " ".join(text.split())


def _safe_experience_lesson(row: sqlite3.Row) -> str:
    """Expose only a v2 deterministic summary; legacy prose remains DB audit-only."""
    version = (
        row["experience_summary_version"]
        if "experience_summary_version" in row.keys() else None
    )
    summary = row["experience_summary"]
    if version == 2 and summary and not _LEGACY_1R_RE.search(str(summary)):
        return _without_legacy_scores(summary) or ""
    regime = str(row["regime"] or "?")
    side = str(row["side"] or "?")
    pnl = row["pnl_pct"]
    hold = row["hold_hours"]
    parts = [f"{regime}/{side}"]
    if pnl is not None:
        parts.append(f"pnl{float(pnl):+.2f}%")
        parts.append("gross_win" if float(pnl) > 0 else "gross_loss")
    else:
        parts.append("gross_unknown")
    if hold is not None:
        parts.append(f"hold{float(hold):.1f}h")
    parts.append("summary_fallback_v2")
    return " ".join(parts)


def _age_days(ts_str: str, now: datetime) -> float:
    try:
        t = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        return max(0.0, (now - t).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 9999.0


def _parse_as_of(value: str | None) -> datetime | None:
    """Parse a cycle/as-of value as CST, preserving explicit offsets."""
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            "--as-of 必须是 ISO 时间，如 2026-08-10T08:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def _load_vec(row: sqlite3.Row) -> list[float]:
    """取存的 experience_vector（JSON）；缺/坏 → 从行字段重算（兼容旧行）。"""
    raw = row["experience_vector"] if "experience_vector" in row.keys() else None
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list) and v:
                return [float(x) for x in v]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return _simutil.experience_vector({
        "regime": row["regime"], "side": row["side"], "action": row["action"],
        "score_total": row["score_total"], "regime_stale": row["regime_stale"],
        "symbol": row["symbol"],
    })


def _experience_summary(
    neighbors: list[tuple[float, sqlite3.Row]],
    now: datetime,
    *,
    scope: str,
) -> dict[str, Any]:
    n = len(neighbors)
    # 2026-08-06 demo 全量下线，117 条历史 demo 样本已从 trade_experiences 清除，
    # 本池此后恒为纯 live。原先每轮都输出的 `live N / demo M` 构成因此退化成恒定
    # 噪音（每次都进 LLM 上下文，且 demo 恒为 0）——改为**只在真出现非 live 样本
    # 时才报**：正常轮零开销，一旦有东西把别的 profile 写回经验库就立刻显形。
    # 写成「非 live」而不是「demo」，是因为要防的是任何意外 profile，不止 demo。
    non_live = sorted({
        str(r["profile"] or "?") for _, r in neighbors
        if str(r["profile"] or "") != "live"
    })
    mix = {
        "profile_mix": (
            f"混入非 live 样本 {', '.join(non_live)}——聚合 win_rate/avg_pnl_pct "
            "为混算值，各 profile 的仓位授权与保证金口径不同，采不采信自行判断"
        )
    } if non_live else {}
    pnls = [r["pnl_pct"] for _, r in neighbors if r["pnl_pct"] is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    base = {
        "scope": scope,
        "n": n,
        "wins": wins,
        "losses": losses,
        # v2 契约：样本身份与计数同源（trade_experiences 行 id，排序去重），
        # 随 evidence_hash 冻结——计数不可再脱离样本手写。
        "sample_ids": sorted(int(r["id"]) for _, r in neighbors),
        **mix,
    }
    # 2026-08-19 G3：阈值与 EV 闸单一真源对齐。旧写法 n>=3 就输出 win_rate，
    # 而 core/ev_calculator.MIN_P_SAMPLE_N=5 才算 EV —— n=3/4 时卡里有一个可被
    # Agent 引用的胜率，ev_check 却是 indeterminate，`ev_r<0 必须带 ev_override`
    # 的硬闸完全不触发。样本最少、最容易过度自信的区间，恰好是唯一没有确定性
    # EV 校验的区间。诊断值另起名并显式标注不可引用。
    if n < MIN_P_SAMPLE_N:
        return {
            **base,
            "sufficient": False,
            "credibility": 0.0,
            "reason": (
                "no_experiences" if n == 0
                else f"insufficient_samples (n<{MIN_P_SAMPLE_N})"
            ),
            "win_rate_diagnostic_not_citable": (
                round(sum(1 for p in pnls if p > 0) / len(pnls), 4)
                if pnls else None),
        }
    sims = [s for s, _ in neighbors]
    ages = [_age_days(r["ts"], now) for _, r in neighbors]
    wr_at_sim = (
        sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0)
    conf_sim = sum(sims) / len(sims)
    avg_age = sum(ages) / len(ages)
    age_decay = 0.5 ** (avg_age / 60.0)
    credibility = wr_at_sim * conf_sim * age_decay * min(n / 20.0, 1.0)
    # 2026-09-26 对照 V3 similarity 模块补齐：Wilson 下界与贴近度加权统计
    # 只展示不设闸；计数/样本身份仍由 n/wins/losses/sample_ids 冻结。
    weighted_win_rate, weighted_pnl = _simutil.similarity_weighted(
        (s, r["pnl_pct"]) for s, r in neighbors if r["pnl_pct"] is not None)
    realized_r = [
        value for value in (
            _simutil._finite(r["realized_r_net"])
            if "realized_r_net" in r.keys() else None
            for _, r in neighbors)
        if value is not None
    ]
    return {
        **base,
        "sufficient": True,
        "win_rate": round(wr_at_sim, 4),
        "win_rate_lo95": (
            round(_simutil.wilson_lo95(wins, len(pnls)), 4) if pnls else None),
        "avg_sim": round(conf_sim, 4),
        "avg_age_days": round(avg_age, 1),
        "age_decay": round(age_decay, 4),
        "credibility": round(credibility, 4),
        "low_credibility": credibility < 0.2,
        "avg_pnl_pct": (
            round(sum(pnls) / len(pnls), 4) if pnls else None),
        "sim_weighted": {
            "win_rate": (
                round(weighted_win_rate, 4)
                if weighted_win_rate is not None else None),
            "avg_pnl_pct": (
                round(weighted_pnl, 4) if weighted_pnl is not None else None),
        },
        "avg_realized_r_net": (
            round(sum(realized_r) / len(realized_r), 4)
            if realized_r else None),
        "realized_r_n": len(realized_r),
    }


def _query_features_v4(query_symbol: str, query_side: str, query_regime: str,
                       query_action: str, as_of_cst: str, db_root: Path,
                       stop_distance_pct: Optional[float],
                       planned_rr: Optional[float]) -> dict[str, Any]:
    """查询侧前向 v4 特征：与 writer 落库时同一派生实现（experience_features_v2）。

    market.db 读失败 → 市场态 None 如实留空，资产类别 / 止损距离 / 开仓小时照常参与。
    """
    from core.asset_class import asset_class_of
    import experience_features_v2 as efv2
    base = {
        "symbol": query_symbol,
        "asset_class": asset_class_of(query_symbol, db_root),
        "side": query_side, "action": query_action, "regime": query_regime,
        "stop_distance_pct": stop_distance_pct,
        "sl_pct": stop_distance_pct,
        "planned_rr": planned_rr,
        "hour_utc": efv2.hour_utc(as_of_cst),
    }
    try:
        base.update(efv2.market_features(query_symbol, as_of_cst, db_root))
    except sqlite3.Error:
        pass
    return _simutil.experience_features_v4(base)


# 旧名兼容（v3 epoch 时代的查询侧装配入口）。
_query_features_v3 = _query_features_v4


def _row_features_v3(row: sqlite3.Row) -> Optional[dict[str, Any]]:
    """只读 exact v3 epoch；v1/v2/坏行均显式排除（判定与版本分布报告共用）。"""
    raw = row["experience_vector"] if "experience_vector" in row.keys() else None
    if not raw:
        return None
    try:
        stored = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return _simutil.stored_v3_features(stored)


def find_similar_experience(
    symbol: str,
    side: str,
    regime: str,
    action: str,
    top_k: int = 8,
    min_sim: float = 0.35,
    profile_filter: str = "all",
    score_total: Optional[float] = None,
    db_root: Path = DEFAULT_DB_ROOT,
    now: Optional[datetime] = None,
    stop_distance_pct: Optional[float] = None,
    planned_rr: Optional[float] = None,
    entry: Optional[float] = None,
    stop: Optional[float] = None,
    target: Optional[float] = None,
    include_all_epochs: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now(CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    else:
        now = now.astimezone(CST)
    query_symbol = normalize_symbol(symbol)
    query_side = normalize_token(side)
    query_regime = normalize_token(regime)
    query_action = normalize_token(action)
    query = {
        "symbol": query_symbol,
        "side": query_side,
        "regime": query_regime,
        "action": query_action,
        "profile": normalize_token(profile_filter),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "min_sim": float(min_sim),
        "top_k": int(top_k),
        # 相似度版本与前向 feature epoch 进契约（随 evidence_hash 冻结）。
        "similarity_version": _simutil.SIMILARITY_VERSION,
    }
    price_geometry = (entry, stop, target)
    price_fields_supplied = sum(value is not None for value in price_geometry)
    legacy_fields_supplied = sum(
        value is not None for value in (stop_distance_pct, planned_rr))
    if price_fields_supplied not in (0, 3):
        raise ValueError("entry, stop and target must be supplied together")
    if legacy_fields_supplied not in (0, 2):
        raise ValueError(
            "stop_distance_pct and planned_rr must be supplied together")
    if price_fields_supplied and legacy_fields_supplied:
        raise ValueError(
            "use entry/stop/target or legacy setup metrics, not both")
    setup = None
    if price_fields_supplied:
        setup = setup_from_prices(entry, stop, target)
    elif legacy_fields_supplied:
        setup = setup_from_metrics(stop_distance_pct, planned_rr)

    account = Path(db_root) / "account.db"
    feature_distance = (
        setup.get("stop_distance_pct") if setup else None)
    feature_rr = setup.get("planned_rr") if setup else None
    query_vec = _query_features_v4(
        query_symbol, query_side, query_regime, query_action,
        query["as_of"], Path(db_root), feature_distance, feature_rr)
    query["feature_epoch"] = _simutil.FEATURE_EPOCH_V4
    query["query_features"] = query_vec
    try:
        from core.instrument_context import build_instrument_context
        query["instrument_context"] = build_instrument_context(
            query_symbol, query_regime, now.strftime("%Y-%m-%dT%H:%M"), db_root)
    except Exception as exc:  # noqa: BLE001  未知如实进入契约，不伪造 range
        query["instrument_context"] = {
            "version": "instrument_context_v1",
            "as_of": query["as_of"],
            "btc_crypto_regime": query_regime,
            "applies_to_instrument": None,
            "asset_class": "unknown",
            "instrument_regime": "not_available",
            "error": type(exc).__name__,
        }
    if setup is not None:
        query["setup"] = setup
    empty_exact = _experience_summary(
        [], now, scope="same_symbol_side_action_regime")
    empty_same = _experience_summary(
        [], now, scope="same_symbol_similar")
    empty_cross = _experience_summary(
        [], now, scope="cross_symbol_similar")
    empty_contract = build_contract(
        query,
        exact_setup=empty_exact,
        same_symbol_similar=empty_same,
        cross_symbol_similar=empty_cross,
    )
    empty = {
        "matches": [],
        "matched_wins": [],
        "matched_losses": [],
        "cross_symbol_wins": [],
        "cross_symbol_losses": [],
        "missed_opportunities": [],
        "summary": empty_same,
        "same_symbol_summary": empty_same,
        "exact_setup_summary": empty_exact,
        "cross_summary": empty_cross,
        "cross_symbol_summary": empty_cross,
        "query": query,
        "evidence_contract": empty_contract,
        "ev_preview": build_ev_preview(
            entry, stop, target, query_side, empty_contract),
        "policy_epoch_filter": build_policy_epoch_filter(
            [], include_all_epochs),
        "query_symbol": query_symbol,
        "query_vec": query_vec,
    }
    if not account.exists():
        return empty

    con = sqlite3.connect(f"file:{account}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        # 只有 trade_experiences 表存在才查
        has = con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                          "AND name='trade_experiences'").fetchone()
        if not has:
            return empty
        columns = {
            str(row[1]) for row in con.execute(
                "PRAGMA table_info(trade_experiences)"
            ).fetchall()
        }
        availability_col = "closed_at" if "closed_at" in columns else "ts"
        summary_version_sql = (
            "experience_summary_version"
            if "experience_summary_version" in columns
            else "NULL AS experience_summary_version"
        )
        realized_r_sql = (
            "realized_r_net" if "realized_r_net" in columns
            else "NULL AS realized_r_net"
        )
        sql = ("SELECT id, cycle_id, ts, profile, symbol, side, action, regime, "
               "regime_stale, score_total, confidence, playbook_ref, "
               "experience_vector, pnl_pct, hold_hours, raw, "
               f"experience_summary, {summary_version_sql}, {realized_r_sql} "
               "FROM trade_experiences WHERE status='closed' "
               "AND pnl_pct IS NOT NULL "
               f"AND {availability_col} IS NOT NULL "
               f"AND {availability_col}<=?")
        params: list[Any] = [query["as_of"]]
        if profile_filter == "live":
            sql += " AND profile=?"
            params.append(profile_filter)
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    import experience_features_v2 as efv2
    scored = []
    coverage_by_id: dict[int, int] = {}
    feature_epoch_excluded_rows: list[Any] = []
    feature_epoch_included_total = 0
    epoch_excluded_rows: list[Any] = []
    market_db = Path(db_root) / "market.db"
    mcon = None
    if market_db.exists():
        try:
            mcon = sqlite3.connect(
                f"file:{market_db}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            mcon = None
    indicator_cache: dict[Any, dict[str, Any]] = {}
    try:
        for r in rows:
            # 历史 v1/v2/v3 行按行 ts 现算 v4 特征（V3 对 legacy 行同一做法）。
            rf = efv2.features_v4_for_row(r, db_root, mcon, indicator_cache)
            if not _simutil.v4_comparable(rf):
                # 一个可比特征都没有：不冒充可比，排除数在返回合同中外显。
                feature_epoch_excluded_rows.append(r)
                continue
            feature_epoch_included_total += 1
            # 已撤回策略纪元的样本不作现行策略的 EV 先验（core/policy_epochs.py 单点
            # 登记）。**只影响先验，不影响任何验收/报表口径**；被剔除的样本不静默
            # 消失，逐 scope 外显计数与胜负，见 epoch_excluded_* 字段。
            if not include_all_epochs and not is_ev_prior_eligible(r["cycle_id"]):
                epoch_excluded_rows.append(r)
                continue
            sim, coverage = _simutil.similarity_v4(query_vec, rf)
            coverage_by_id[int(r["id"])] = coverage
            scored.append((sim, r))
    finally:
        if mcon is not None:
            mcon.close()
    scored.sort(key=lambda x: x[0], reverse=True)

    # 邻居 = sim≥min_sim
    neighbors = [(s, r) for s, r in scored if s >= min_sim]
    all_matches = []
    for sim, r in neighbors:
        age = _age_days(r["ts"], now)
        all_matches.append({
            "experience_id": r["id"],
            "sim": round(sim, 4),
            "coverage": coverage_by_id.get(int(r["id"]), 0),
            "pnl_pct": r["pnl_pct"],
            "hold_hours": r["hold_hours"],
            "side_match": normalize_token(r["side"]) == query_side,
            "action_match": normalize_token(r["action"]) == query_action,
            "regime_match": normalize_token(r["regime"]) == query_regime,
            "age_days": round(age, 1),
            "playbook_ref": r["playbook_ref"],
            "cycle_id": r["cycle_id"],
            "profile": r["profile"],
            "symbol": r["symbol"],
            "outcome": "win" if r["pnl_pct"] > 0 else "loss",
            "lesson": _safe_experience_lesson(r),
            # 原始回执仍在 account.db 留作审计，但不再进入模型检索上下文；
            # 历史 raw 含退化 1R 文本，截断 JSON 也无法可靠净化。
            "raw_snippet": None,
        })

    exact_neighbors = [
        (s, r) for s, r in neighbors
        if normalize_symbol(r["symbol"]) == query_symbol
    ]
    cross_neighbors = [
        (s, r) for s, r in neighbors
        if normalize_symbol(r["symbol"]) != query_symbol
    ]
    # Exact setup is deliberately independent of the cosine threshold.  It is
    # the stable direct statistic agents may quote as "symbol/side/regime".
    exact_setup_neighbors = [
        (s, r) for s, r in scored
        if normalize_symbol(r["symbol"]) == query_symbol
        and normalize_token(r["side"]) == query_side
        and normalize_token(r["action"]) == query_action
        and (
            not query_regime
            or normalize_token(r["regime"]) == query_regime
        )
    ]
    exact_matches = [
        m for m in all_matches
        if normalize_symbol(m.get("symbol")) == query_symbol
    ]
    cross_matches = [
        m for m in all_matches
        if normalize_symbol(m.get("symbol")) != query_symbol
    ]

    side_cap = max(1, top_k // 2)
    matched_wins = [
        m for m in exact_matches if m["outcome"] == "win"][:side_cap]
    matched_losses = [
        m for m in exact_matches if m["outcome"] == "loss"][:side_cap]
    cross_symbol_wins = [
        m for m in cross_matches if m["outcome"] == "win"][:side_cap]
    cross_symbol_losses = [
        m for m in cross_matches if m["outcome"] == "loss"][:side_cap]
    matches = sorted(
        matched_wins + matched_losses,
        key=lambda item: item["sim"],
        reverse=True,
    )[:top_k]

    missed = []
    lessons = Path(db_root) / "lessons.db"
    if lessons.exists():
        lcon = sqlite3.connect(f"file:{lessons}?mode=ro", uri=True, timeout=5)
        lcon.row_factory = sqlite3.Row
        try:
            mrows = lcon.execute(
                "SELECT ts,symbol,regime,direction_hint,actual_4h_pct,"
                "would_hit_1r_fixed2pct,notes "
                "FROM missed_opportunities WHERE symbol=? AND ts LIKE '202%' "
                "AND ts<=? "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (query_symbol, query["as_of"], max(1, top_k // 2)),
            ).fetchall()
            missed = []
            for row in mrows:
                item = dict(row)
                item["notes"] = _without_legacy_scores(item.get("notes"))
                missed.append(item)
        except sqlite3.Error:
            missed = []
        finally:
            lcon.close()

    same_summary = _experience_summary(
        exact_neighbors, now, scope="same_symbol_similar")
    exact_setup_summary = _experience_summary(
        exact_setup_neighbors,
        now,
        scope="same_symbol_side_action_regime",
    )
    cross_summary = _experience_summary(
        cross_neighbors, now, scope="cross_symbol_similar")
    evidence_contract = build_contract(
        query,
        exact_setup=exact_setup_summary,
        same_symbol_similar=same_summary,
        cross_symbol_similar=cross_summary,
    )
    return {
        "matches": matches,
        "matched_wins": matched_wins,
        "matched_losses": matched_losses,
        "cross_symbol_wins": cross_symbol_wins,
        "cross_symbol_losses": cross_symbol_losses,
        "missed_opportunities": missed,
        # summary/cross_summary are retained as compatibility aliases.  New
        # decision cards must copy evidence_contract and quote its named scopes.
        "summary": same_summary,
        "same_symbol_summary": same_summary,
        "exact_setup_summary": exact_setup_summary,
        "cross_summary": cross_summary,
        "cross_symbol_summary": cross_summary,
        "query": query,
        "evidence_contract": evidence_contract,
        "ev_preview": build_ev_preview(
            entry, stop, target, query_side, evidence_contract),
        "policy_epoch_filter": build_policy_epoch_filter(
            epoch_excluded_rows, include_all_epochs),
        "feature_epoch_filter": build_feature_epoch_filter(
            feature_epoch_excluded_rows, feature_epoch_included_total),
        "query_symbol": query_symbol,
        "query_vec": query_vec,
        "similarity_version": _simutil.SIMILARITY_VERSION,
        "feature_epoch": _simutil.FEATURE_EPOCH_V4,
        "feature_epoch_excluded_rows": len(feature_epoch_excluded_rows),
        "legacy_rows_skipped": len(feature_epoch_excluded_rows),
    }


def build_feature_epoch_filter(
    excluded_rows: list[Any], included_total: int,
) -> dict[str, Any]:
    wins = sum(1 for row in excluded_rows if row["pnl_pct"] > 0)
    return {
        "version": "feature_epoch_filter_v2",
        "active_epoch": _simutil.FEATURE_EPOCH_V4,
        "included_total": int(included_total),
        "excluded_total": len(excluded_rows),
        "excluded_wins": wins,
        "excluded_losses": len(excluded_rows) - wins,
        "excluded_sample_ids": sorted(int(row["id"]) for row in excluded_rows),
        "scope": (
            "排除的是连一个可比 v4 特征都现算不出的行；只约束相似经验与EV先验，"
            "历史胜率、净利、日周月报仍使用全量样本"
        ),
    }


def build_policy_epoch_filter(
    excluded_rows: list[Any], include_all_epochs: bool) -> dict[str, Any]:
    """外显被 EV 先验排除的样本——计数、胜负、样本 id 一个都不许藏。

    项目红线是「禁止通过排除失败样本提高任何指标」。本块存在的意义就是让这次
    排除**可被独立复核**：排的是哪个纪元、几条、几胜几负、具体是哪些行 id。
    验收口径（胜率/净利/日周月报/盈利审计）完全不看本块，那些统计仍是全量。
    """
    by_epoch: dict[str, dict[str, Any]] = {}
    for row in excluded_rows:
        name = policy_epoch(row["cycle_id"])
        bucket = by_epoch.setdefault(
            name, {"n": 0, "wins": 0, "losses": 0, "sample_ids": []})
        bucket["n"] += 1
        pnl = row["pnl_pct"]
        if pnl is not None:
            if pnl > 0:
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1
        bucket["sample_ids"].append(int(row["id"]))
    for bucket in by_epoch.values():
        bucket["sample_ids"].sort()
    return {
        "version": "policy_epoch_filter_v1",
        "current_epoch": CURRENT_EPOCH,
        "excluded_epochs": sorted(EXCLUDED_FROM_EV_PRIOR),
        "include_all_epochs": bool(include_all_epochs),
        "excluded_total": sum(b["n"] for b in by_epoch.values()),
        "excluded_by_epoch": by_epoch,
        "scope": (
            "只作用于 EV 先验（p_win）的取样；胜率/净利/日周月报/盈利审计一律全量，"
            "不受本过滤影响"
        ),
        "source": "core/policy_epochs.py（纪元边界单点登记，只向前预注册）",
    }


EV_PREVIEW_VERSION = "ev_preview_v1"


def build_ev_preview(
    entry: Optional[float],
    stop: Optional[float],
    target: Optional[float],
    side: str,
    evidence_contract: Any,
) -> dict[str, Any]:
    """按拟用三价 + 本次证据契约预演 writer 的 ev_check（2026-08-20）。

    背景：`ev_check` 此前只在 analyst_writer 落卡时才算，Agent 在决定写不写这
    张卡时看不到 EV 符号；而写卡被拒的代价是「整文件重写只允许一次、第二次失败
    锁死本轮」。于是「EV 符号不确定」变成了纯粹的下行风险，理性策略是不写——
    2026-08-18~20 连续 207 轮 `signals=[]` 期间，实测被否决的候选里存在 ev_r 为
    正的（XPL 2026-08-20T10:15：p_win=40.68% / net_rr=1.86 / ev_r=+0.163）。

    本函数调用**同一个** `core.ev_calculator.build_ev_check` 纯函数、喂**同一份**
    evidence_contract，因此预览与落库 canonical 值在输入相同时逐字段一致。这只是
    把 writer 反正要算的结果提前告知，不新增闸、不放宽闸、不产生授权。

    刻意不叫 `ev_check`：canonical 值只能由 writer 注入卡内，预览块禁止被复制进
    决策卡（见 `note`）。
    """
    preview: dict[str, Any] = {
        "version": EV_PREVIEW_VERSION,
        "source": "core.ev_calculator.build_ev_check",
        "status": "unavailable",
        "note": (
            "只读预览：与 writer 落库 ev_check 同一纯函数同一输入；禁止复制进决策卡"
            "（canonical ev_check 只由 writer 注入）。预览不构成开仓授权。"
        ),
    }
    if entry is None or stop is None or target is None:
        preview["reason"] = "未传 entry/stop/target，无法预演 EV"
        return preview

    # rr 按几何精确回填：预览的目的是看 EV 符号，不是复查模型手写的 rr 字段，
    # 因此不能让 rr 容差错误掩盖真正要看的 ev_r。
    try:
        if side == "long":
            risk = (entry - stop) / entry
            reward = (target - entry) / entry
        else:
            risk = (stop - entry) / entry
            reward = (entry - target) / entry
        rr = reward / risk if risk else None
    except (TypeError, ZeroDivisionError):
        rr = None
    card = {
        "risk_reward": {
            "entry": entry, "stop": stop, "target": target, "rr": rr,
        },
        "historical_experience": {"evidence_contract": evidence_contract},
    }
    ev_check, errors = build_ev_check(card, side)
    if not ev_check:
        preview["reason"] = "；".join(errors) or "EV 无法计算"
        return preview

    ev_r = ev_check.get("ev_r")
    needs_override = (
        ev_check.get("status") == "computed"
        and isinstance(ev_r, (int, float))
        and ev_r < 0
    )
    own_note = preview["note"]
    preview.update(ev_check)
    # ev_check 自带 note，update 会把「禁止复制进决策卡」那句盖掉——预览身份声明
    # 必须活下来，calculator 的口径说明另存一个键。
    preview["note"] = own_note
    preview["ev_calculator_note"] = ev_check.get("note")
    preview["version"] = EV_PREVIEW_VERSION
    preview["source"] = "core.ev_calculator.build_ev_check"
    preview["needs_override"] = needs_override
    preview["would_block_write_without_override"] = needs_override
    # 摩擦占 R 的比例：窄止损被手续费+滑点吃掉的那部分此前只能靠感觉估。
    # 实测 3×ATR 很小的标的（如 TRX 3×ATR=0.77%）摩擦占 R 达 26%，盈亏平衡胜率
    # 被推到 42%，远高于经验池先验——把它显式算出来，取舍才有依据。
    risk_pct = ev_check.get("risk_pct")
    if isinstance(risk_pct, (int, float)) and risk_pct > 0:
        preview["friction_share_of_risk"] = round(
            ev_check.get("friction_pct", 0.0) / risk_pct, 4)
    preview["writer_errors_preview"] = list(errors)
    if ev_check.get("status") == "indeterminate":
        preview["reason"] = (
            f"三个具名 scope 均无 n≥{MIN_P_SAMPLE_N} 样本，无确定性 p_win；"
            "EV 无闸，按结构与催化自证"
        )
    return preview


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return the decision-useful subset without raw/vector payload bloat."""
    keep_match = (
        "experience_id", "sim", "coverage", "pnl_pct", "hold_hours", "age_days",
        "playbook_ref", "cycle_id", "profile", "symbol", "outcome", "lesson",
        "side_match", "action_match", "regime_match",
    )
    keep_missed = (
        "ts", "symbol", "regime", "direction_hint", "actual_4h_pct",
        "would_hit_1r_fixed2pct", "notes",
    )

    def pick(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        out = {key: item.get(key) for key in keys if key in item}
        for key in ("lesson", "notes"):
            if isinstance(out.get(key), str):
                out[key] = out[key][:240]
        return out

    return {
        "summary": result.get("summary") or {},
        "same_symbol_summary": result.get("same_symbol_summary") or {},
        "exact_setup_summary": result.get("exact_setup_summary") or {},
        "cross_summary": result.get("cross_summary") or {},
        "cross_symbol_summary": result.get("cross_symbol_summary") or {},
        "query": result.get("query") or {},
        "evidence_contract": result.get("evidence_contract") or {},
        "ev_preview": result.get("ev_preview") or {},
        "policy_epoch_filter": result.get("policy_epoch_filter") or {},
        "feature_epoch_filter": result.get("feature_epoch_filter") or {},
        "feature_epoch": result.get("feature_epoch"),
        "similarity_version": result.get("similarity_version"),
        "query_symbol": result.get("query_symbol"),
        "matched_wins": [
            pick(item, keep_match)
            for item in (result.get("matched_wins") or [])
            if isinstance(item, dict)
        ],
        "matched_losses": [
            pick(item, keep_match)
            for item in (result.get("matched_losses") or [])
            if isinstance(item, dict)
        ],
        "cross_symbol_wins": [
            pick(item, keep_match)
            for item in (result.get("cross_symbol_wins") or [])
            if isinstance(item, dict)
        ],
        "cross_symbol_losses": [
            pick(item, keep_match)
            for item in (result.get("cross_symbol_losses") or [])
            if isinstance(item, dict)
        ],
        "missed_opportunities": [
            pick(item, keep_missed)
            for item in (result.get("missed_opportunities") or [])
            if isinstance(item, dict)
        ],
    }


def _atomic_write_json(path: Path, value: dict[str, Any], pretty: bool) -> int:
    """Write UTF-8 JSON by same-directory replace so readers never see a tear."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        value, ensure_ascii=False, indent=2 if pretty else None
    ) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = Path(handle.name)
        os.replace(tmp_path, path)
        return len(content.encode("utf-8"))
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 交易经验检索")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--side", required=True, choices=["long", "short"])
    ap.add_argument("--regime", default="")
    ap.add_argument("--action", default="open")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--min-sim", type=float, default=0.35,
                    help="贴近度门槛（V3 同值 0.35；几何平均尺度低于旧算术平均）")
    ap.add_argument("--profile", default="live", choices=["live", "all"])
    ap.add_argument(
        "--as-of",
        help=(
            "经验在该 CST 时刻必须已经 closed；生产决策传固定 cycle，"
            "如 2026-08-10T08:00"
        ),
    )
    ap.add_argument("--db-root", default=_public_project_path('db'))
    ap.add_argument("--stop-distance-pct", type=float, default=None,
                    help="拟用止损距离（0.04=4%%）；action=open 时必填")
    ap.add_argument("--planned-rr", type=float, default=None,
                    help="兼容参数：计划盈亏比；须与 --stop-distance-pct 同传")
    ap.add_argument("--entry", type=float, default=None,
                    help="拟用入场价；与 --stop/--target 同传（open 推荐）")
    ap.add_argument("--stop", type=float, default=None,
                    help="拟用止损价；与 --entry/--target 同传（open 推荐）")
    ap.add_argument("--target", type=float, default=None,
                    help="拟用目标价；与 --entry/--stop 同传（open 推荐）")
    ap.add_argument(
        "--include-all-epochs", action="store_true",
        help=("审计用：连同已撤回策略纪元的样本一起入 EV 先验。"
              "生产检索不要传——纪元过滤只影响先验，不影响任何验收口径"))
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument(
        "--compact",
        action="store_true",
        help="仅输出决策所需摘要、正反样本与错失机会，省略向量和 raw_snippet",
    )
    ap.add_argument(
        "--out-file",
        help="把结果原子写为 UTF-8 JSON；stdout 只返回短回执，禁止再接管道/重定向",
    )
    args = ap.parse_args()
    try:
        as_of = _parse_as_of(args.as_of)
    except ValueError as exc:
        ap.error(str(exc))
    price_count = sum(
        value is not None for value in (args.entry, args.stop, args.target))
    legacy_count = sum(value is not None for value in (
        args.stop_distance_pct, args.planned_rr))
    if price_count not in (0, 3):
        ap.error("--entry/--stop/--target 必须三项同传")
    if legacy_count not in (0, 2):
        ap.error("--stop-distance-pct/--planned-rr 必须两项同传")
    if price_count and legacy_count:
        ap.error("新价格参数与兼容比例参数不可同时使用")
    if args.action == "open" and not (price_count == 3 or legacy_count == 2):
        ap.error(
            "open 检索必须传 --entry/--stop/--target；兼容旧调用也可同时传 "
            "--stop-distance-pct/--planned-rr"
        )
    try:
        res = find_similar_experience(
            args.symbol, args.side, args.regime, args.action, top_k=args.top_k,
            min_sim=args.min_sim, profile_filter=args.profile,
            db_root=Path(args.db_root), now=as_of,
            stop_distance_pct=args.stop_distance_pct,
            planned_rr=args.planned_rr,
            entry=args.entry, stop=args.stop, target=args.target,
            include_all_epochs=args.include_all_epochs)
    except ValueError as exc:
        ap.error(str(exc))
    output = compact_result(res) if args.compact else res
    if args.out_file:
        path = Path(args.out_file)
        size = _atomic_write_json(path, output, args.pretty)
        print(json.dumps({
            "ok": True,
            "out_file": str(path),
            "bytes": size,
            "compact": bool(args.compact),
            "summary": output.get("summary") or {},
            "policy_epoch_excluded": (
                output.get("policy_epoch_filter") or {}).get("excluded_total"),
            # EV 符号必须在「要不要写这张卡」之前就可见，所以进 stdout 摘要而不是
            # 只躺在 out_file 里。
            "ev_preview": {
                key: (output.get("ev_preview") or {}).get(key)
                for key in ("status", "p_win", "p_scope", "p_n", "net_rr",
                            "breakeven_p", "ev_r", "needs_override")
            },
        }, ensure_ascii=False))
    else:
        print(json.dumps(
            output, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
