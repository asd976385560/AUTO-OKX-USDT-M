# -*- coding: utf-8 -*-
r"""V2.0 §6/P5 —— 确定性币种情绪统计（OKX sentiment-rank 的确定性备援；2026-07-27 CLI 1.4.2 下 okx news 已复核可返回，本模块角色不变）。

从 **本系统自有** news.db.news_items（+ news_events_index 多币 + source='x_search' 的 X 提及）
按币种聚合：news_mention_cnt / x_mention_cnt / 规则极性（bullish/bearish 词典）→ label。
**纯确定性**（无 LLM，红线 #10：采集器零 LLM），供 collect_slow 在 OKX sentiment-rank 不可用时
兜底（resilience）；OKX 可用时由 collect_slow 优先 OKX 再回退本模块。analyst 仍做真正的情绪/影响判断。

覆盖面 = 我们有新闻/X 提及的币（比 OKX 全币种稀疏，属预期：自有数据口径）。

用法（独立诊断）：
  run_okx_python.ps1 scripts/sentiment_compute.py --db ./db/news.db --period 24h
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 规则极性词典（英文为主——RSS/X 多英文；命中即计，纯统计非语义）
BULLISH = {
    "surge", "soar", "rally", "gain", "gains", "bull", "bullish", "breakout", "pump",
    "ath", "all-time high", "approve", "approved", "adopt", "adoption", "partner",
    "partnership", "upgrade", "listing", "lists", "inflow", "inflows", "accumulate",
    "buy", "buys", "long", "moon", "soars", "jumps", "jump", "rises", "rise", "up",
    "record", "integration", "launch", "launches", "support", "boost", "outperform",
}
BEARISH = {
    "crash", "plunge", "dump", "drop", "drops", "bear", "bearish", "hack", "hacked",
    "exploit", "lawsuit", "sue", "ban", "banned", "sell-off", "selloff", "liquidation",
    "liquidated", "outflow", "outflows", "delist", "delisted", "down", "falls", "fall",
    "slump", "fear", "warning", "warn", "fraud", "scam", "halt", "freeze", "lose",
    "loss", "losses", "decline", "tumble", "short", "risk", "collapse", "rug",
}

_WORD = re.compile(r"[a-z0-9\-]+")


def _polarity(text: str) -> int:
    """+1 偏多 / -1 偏空 / 0 中性（词典命中差）。"""
    if not text:
        return 0
    toks = set(_WORD.findall(text.lower()))
    b = len(toks & BULLISH)
    s = len(toks & BEARISH)
    if b > s:
        return 1
    if s > b:
        return -1
    return 0


def _period_hours(period: str) -> int:
    return {"1h": 1, "4h": 4, "12h": 12, "24h": 24, "48h": 48}.get(period, 24)


def compute(news_db: str | Path, period: str = "24h",
            now: Optional[datetime] = None) -> list[dict]:
    """返回 per-symbol 情绪统计行（dict），列对齐 coin_sentiment 表。"""
    news_db = Path(news_db)
    if not news_db.exists():
        return []
    hours = _period_hours(period)
    con = sqlite3.connect(f"file:{news_db}?mode=ro", uri=True, timeout=8)
    con.row_factory = sqlite3.Row
    try:
        # 时间窗（2026-07-02 修）：ingested_at/ts 混 UTC-Z 与 CST-space 两种格式并发写入，
        # UTC-Z cutoff 字符串直比 CST 行会整日错排（漂 8-32h）。归一到 naive-UTC 再比：
        # UTC-Z→datetime()、CST-space→datetime(-8h)；cutoff 用 naive-UTC 空格格式。
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
        cutoff_utc = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        _tsn = ("CASE WHEN COALESCE({c}) LIKE '%Z' THEN datetime(COALESCE({c})) "
                "ELSE datetime(COALESCE({c}),'-8 hours') END")
        _n_ni = _tsn.format(c="ingested_at, ts")
        _n_idx = _tsn.format(c="n.ingested_at, n.ts")
        # 直接 news_items.symbol（单币标注）；多币经 news_events_index 展开
        rows = con.execute(
            "SELECT symbol, source, title, sentiment FROM news_items "
            f"WHERE symbol IS NOT NULL AND symbol<>'' AND {_n_ni} >= ?",
            (cutoff_utc,)).fetchall()
        extra = []
        try:
            extra = con.execute(
                "SELECT i.symbol AS symbol, n.source AS source, n.title AS title, "
                "n.sentiment AS sentiment FROM news_events_index i "
                "JOIN news_items n ON n.id = i.news_id "
                f"WHERE {_n_idx} >= ?", (cutoff_utc,)).fetchall()
        except sqlite3.OperationalError:
            extra = []
    finally:
        con.close()

    agg: dict[str, dict] = {}
    seen: set = set()  # (symbol, title) 去重，防 news_items + index 双计同条

    def _norm_symbol(sym: str) -> str:
        sym = str(sym).strip().upper()
        if sym and "-" not in sym:
            sym = f"{sym}-USDT-SWAP"
        return sym

    for r in list(rows) + list(extra):
        sym = _norm_symbol(r["symbol"])
        if not sym or sym == "-USDT-SWAP":
            continue
        key = (sym, (r["title"] or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        a = agg.setdefault(sym, {"news": 0, "x": 0, "bull": 0, "bear": 0, "neu": 0})
        is_x = str(r["source"] or "").lower() == "x_search"
        if is_x:
            a["x"] += 1
        else:
            a["news"] += 1
        # 极性：优先用已存 sentiment 字段，否则规则词典扫 title
        sent = str(r["sentiment"] or "").lower()
        if sent in ("bullish", "positive", "pos"):
            pol = 1
        elif sent in ("bearish", "negative", "neg"):
            pol = -1
        else:
            pol = _polarity(r["title"] or "")
        if pol > 0:
            a["bull"] += 1
        elif pol < 0:
            a["bear"] += 1
        else:
            a["neu"] += 1

    out = []
    ts_now = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for sym, a in agg.items():
        total_pol = a["bull"] + a["bear"] + a["neu"]
        mention = a["news"] + a["x"]
        denom = a["bull"] + a["bear"]
        bull_ratio = round(a["bull"] / denom, 4) if denom else 0.0
        bear_ratio = round(a["bear"] / denom, 4) if denom else 0.0
        if denom == 0:
            label = "neutral"
        elif bull_ratio >= 0.6:
            label = "bullish"
        elif bear_ratio >= 0.6:
            label = "bearish"
        else:
            label = "neutral"
        out.append({
            "ts": ts_now, "symbol": sym, "period": period, "label": label,
            "bullish_ratio": bull_ratio, "bearish_ratio": bear_ratio,
            "bullish_cnt": a["bull"], "bearish_cnt": a["bear"], "neutral_cnt": a["neu"],
            "mention_cnt": mention, "news_mention_cnt": a["news"],
            "x_mention_cnt": a["x"],
            "raw": json.dumps({"src": "deterministic", "pol_total": total_pol},
                              ensure_ascii=False),
        })
    out.sort(key=lambda d: d["mention_cnt"], reverse=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性币种情绪统计（脱 OKX）")
    ap.add_argument("--db", default=r"./db/news.db")
    ap.add_argument("--period", default="24h")
    args = ap.parse_args()
    rows = compute(args.db, args.period)
    print(json.dumps({"n": len(rows), "top": rows[:10]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
