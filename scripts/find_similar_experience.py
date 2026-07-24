# -*- coding: utf-8 -*-
"""V2.0 §8.5 —— 交易经验检索（LLM 决策的长期记忆）。

新判断前必搜相似经验：读 account.db.trade_experiences（已平仓、有 pnl），算与当前
决策背景的 cosine 相似度，分别返回相似盈利、相似亏损、错失机会与统计摘要。
**只采不拦**：所有历史数据只供 Agent 自主裁决；Agent 必须说明 adopt/partial/ignore/none，
但历史结果、可信度和样本数均不能自动批准或否决交易。

可信度公式（4 因子，主人拍板）：
  credibility = wr_at_sim × conf_sim × age_decay × min(n/20, 1.0)
    wr_at_sim = sim≥min_sim 邻居中 pnl>0 占比
    conf_sim  = 邻居平均 sim（裁掉 <min_sim 后）
    age_decay = 0.5 ^ (age_days / 60)（半衰期 60 天，用邻居平均 age）
    n         = 入算邻居数

豁免阈值（拍板「无足够样本」）：n<3 或所有 sim<min_sim → summary.sufficient=False；
cred<0.2 标 low_credibility（briefing 显式标，禁凭单条低相似锁决策）。找不到→不另加限制，
只走 §7 硬上限。

不复用 find_similar_history（特征空间/输出 schema/前向窗口全异）；只共用 _simutil.cosine。
零模型名（红线 #1）。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, _project_path('scripts'))
import _simutil  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
DEFAULT_DB_ROOT = Path(_project_path('db'))
_LEGACY_SCORE_MARKERS = (
    re.compile(
        r"[\"']?(?:score(?:_total)?|total|conf(?:idence)?)[\"']?"
        r"\s*[=:]?\s*-?\d+(?:\.\d+)?%?",
        re.IGNORECASE,
    ),
    re.compile(r"(?:评分|置信度)\s*[=:：]?\s*-?\d+(?:\.\d+)?%?"),
)


def _without_legacy_scores(value: Any) -> str | None:
    """隐藏评分兼容标记，保留历史事实和定性教训。"""
    if value is None:
        return None
    text = str(value)
    for pattern in _LEGACY_SCORE_MARKERS:
        text = pattern.sub("", text)
    return " ".join(text.split())


def _age_days(ts_str: str, now: datetime) -> float:
    try:
        t = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        return max(0.0, (now - t).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 9999.0


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


def find_similar_experience(
    symbol: str,
    side: str,
    regime: str,
    action: str,
    top_k: int = 8,
    min_sim: float = 0.5,
    profile_filter: str = "all",
    score_total: Optional[float] = None,
    db_root: Path = DEFAULT_DB_ROOT,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or datetime.now(CST)
    account = Path(db_root) / "account.db"
    query_vec = _simutil.experience_vector({
        "symbol": symbol, "side": side, "regime": regime, "action": action,
        "score_total": score_total,
    })
    empty = {
        "matches": [],
        "matched_wins": [],
        "matched_losses": [],
        "missed_opportunities": [],
        "summary": {
            "n": 0,
            "sufficient": False,
            "credibility": 0.0,
            "reason": "no_experiences",
        },
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
        sql = ("SELECT cycle_id, ts, profile, symbol, side, action, regime, "
               "regime_stale, score_total, confidence, playbook_ref, "
         "experience_vector, pnl_pct, hold_hours, hit_1R, raw, experience_summary "
               "FROM trade_experiences WHERE status='closed' AND pnl_pct IS NOT NULL")
        params: list[Any] = []
        if profile_filter in ("live", "demo"):
            sql += " AND profile=?"
            params.append(profile_filter)
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    scored = []
    for r in rows:
        sim = _simutil.cosine(query_vec, _load_vec(r))
        scored.append((sim, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    # 邻居 = sim≥min_sim
    neighbors = [(s, r) for s, r in scored if s >= min_sim]
    all_matches = []
    for sim, r in neighbors:
        age = _age_days(r["ts"], now)
        all_matches.append({
            "sim": round(sim, 4),
            "pnl_pct": r["pnl_pct"],
            "hold_hours": r["hold_hours"],
            "hit_1R": r["hit_1R"],
            "regime_match": (r["regime"] or "").lower() == (regime or "").lower(),
            "age_days": round(age, 1),
            "playbook_ref": r["playbook_ref"],
            "cycle_id": r["cycle_id"],
            "profile": r["profile"],
            "outcome": "win" if r["pnl_pct"] > 0 else "loss",
            "lesson": _without_legacy_scores(r["experience_summary"]),
            "raw_snippet": _without_legacy_scores((r["raw"] or "")[:200]),
        })

    side_cap = max(1, top_k // 2)
    matched_wins = [m for m in all_matches if m["outcome"] == "win"][:side_cap]
    matched_losses = [m for m in all_matches if m["outcome"] == "loss"][:side_cap]
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
                "SELECT ts,symbol,regime,direction_hint,actual_4h_pct,would_hit_1R,notes "
                "FROM missed_opportunities WHERE symbol=? AND ts LIKE '202%' "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (symbol, max(1, top_k // 2)),
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

    n = len(neighbors)
    if n < 3:
        return {"matches": matches,
                "matched_wins": matched_wins,
                "matched_losses": matched_losses,
                "missed_opportunities": missed,
                "summary": {"n": n, "sufficient": False, "credibility": 0.0,
                            "reason": "insufficient_samples (n<3)"},
                "query_vec": query_vec}

    sims = [s for s, _ in neighbors]
    pnls = [r["pnl_pct"] for _, r in neighbors if r["pnl_pct"] is not None]
    ages = [_age_days(r["ts"], now) for _, r in neighbors]
    wr_at_sim = (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else 0.0
    conf_sim = sum(sims) / len(sims)
    avg_age = sum(ages) / len(ages)
    age_decay = 0.5 ** (avg_age / 60.0)
    credibility = wr_at_sim * conf_sim * age_decay * min(n / 20.0, 1.0)

    return {
        "matches": matches,
        "matched_wins": matched_wins,
        "matched_losses": matched_losses,
        "missed_opportunities": missed,
        "summary": {
            "n": n,
            "sufficient": True,
            "win_rate": round(wr_at_sim, 4),
            "avg_sim": round(conf_sim, 4),
            "avg_age_days": round(avg_age, 1),
            "age_decay": round(age_decay, 4),
            "credibility": round(credibility, 4),
            "low_credibility": credibility < 0.2,
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else None,
            "wins": sum(1 for p in pnls if p > 0),
            "losses": sum(1 for p in pnls if p <= 0),
        },
        "query_vec": query_vec,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 交易经验检索")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--side", required=True, choices=["long", "short"])
    ap.add_argument("--regime", default="")
    ap.add_argument("--action", default="open")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--min-sim", type=float, default=0.5)
    ap.add_argument("--profile", default="all", choices=["live", "demo", "all"])
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()
    res = find_similar_experience(
        args.symbol, args.side, args.regime, args.action, top_k=args.top_k,
        min_sim=args.min_sim, profile_filter=args.profile,
        db_root=Path(args.db_root))
    print(json.dumps(res, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
