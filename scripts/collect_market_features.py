# -*- coding: utf-8 -*-
"""采集高价值币种的50档订单簿与最近逐笔成交影子特征。

覆盖范围：
  BTC/ETH/SOL + focus.md关注币 + 最新快照按美元成交额动态补足。
生产默认50个币，避免对全市场416币抓取深度数据。

本脚本只生成影子特征，不改变分析评分和交易风控。
历史默认保留30天；每日04:45槽由本脚本自己的写连接裁剪，避免50档原始JSON无界增长。
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _okx_http import fetch_orderbooks_batch_sync, fetch_recent_trades_batch_sync

CST = timezone(timedelta(hours=8))
ROOT = Path(_project_path())
BASE_SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
DEPTH_BPS = (10, 25, 50)
SLIPPAGE_USD = (100, 500, 1000)
DEFAULT_RETENTION_DAYS = 30


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cycle_id_now() -> str:
    now = datetime.now(CST)
    minute = (now.minute // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def ms_to_iso(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def focus_symbols(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    part = text.split("## 关注币种", 1)
    if len(part) < 2:
        return []
    body = part[1].split("\n## ", 1)[0]
    out = []
    for line in body.splitlines():
        if not line.strip().startswith(("-", "*")):
            continue
        for token in re.findall(r"\b[A-Z][A-Z0-9]{1,14}\b", line.upper()):
            sym = token if token.endswith("-USDT-SWAP") else f"{token}-USDT-SWAP"
            if sym not in out:
                out.append(sym)
    return out


def select_symbols(con: sqlite3.Connection, max_symbols: int, focus_path: Path) -> list[str]:
    latest = con.execute("SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
    rows = con.execute(
        "SELECT t.symbol,t.last,t.vol24h,COALESCE(i.ctVal,0) ctVal "
        "FROM tick_snapshots t LEFT JOIN instruments_cache i ON i.instId=t.symbol "
        "WHERE t.ts=? AND t.last IS NOT NULL AND t.vol24h IS NOT NULL",
        (latest,),
    ).fetchall()
    ranked = sorted(
        rows,
        key=lambda r: (r[1] or 0) * (r[2] or 0) * (r[3] or 0),
        reverse=True,
    )
    live = {r[0] for r in rows}
    selected = []
    for symbol in (*BASE_SYMBOLS, *focus_symbols(focus_path), *(r[0] for r in ranked)):
        if symbol in live and symbol not in selected:
            selected.append(symbol)
        if len(selected) >= max_symbols:
            break
    return selected


def depth_usd(levels: list, mid: float, ct_val: float, side: str, bp: int) -> float:
    if not mid or not ct_val:
        return 0.0
    if side == "bid":
        bound = mid * (1 - bp / 10000)
        eligible = (lvl for lvl in levels if to_float(lvl[0]) is not None and float(lvl[0]) >= bound)
    else:
        bound = mid * (1 + bp / 10000)
        eligible = (lvl for lvl in levels if to_float(lvl[0]) is not None and float(lvl[0]) <= bound)
    total = 0.0
    for lvl in eligible:
        px, qty = to_float(lvl[0]), to_float(lvl[1])
        if px is not None and qty is not None:
            total += px * qty * ct_val
    return total


def estimate_slippage_bps(levels: list, mid: float, ct_val: float, target_usd: float) -> float | None:
    if not mid or not ct_val or target_usd <= 0:
        return None
    remaining = target_usd
    contracts = 0.0
    paid = 0.0
    for lvl in levels:
        px, qty = to_float(lvl[0]), to_float(lvl[1])
        if not px or not qty:
            continue
        level_usd = px * qty * ct_val
        take_usd = min(remaining, level_usd)
        take_contracts = take_usd / (px * ct_val)
        paid += take_contracts * px
        contracts += take_contracts
        remaining -= take_usd
        if remaining <= 1e-8:
            break
    if remaining > 1e-6 or contracts <= 0:
        return None
    avg_px = paid / contracts
    return abs(avg_px - mid) / mid * 10000


def book_features(book: dict, ct_val: float, cycle_id: str, collected_ts: str, symbol: str) -> tuple:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = to_float(bids[0][0]) if bids else None
    best_ask = to_float(asks[0][0]) if asks else None
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
    spread = (best_ask - best_bid) / mid * 10000 if mid else None
    depths = {}
    imbalances = {}
    for bp in DEPTH_BPS:
        bd = depth_usd(bids, mid, ct_val, "bid", bp) if mid else 0.0
        ad = depth_usd(asks, mid, ct_val, "ask", bp) if mid else 0.0
        depths[bp] = (bd, ad)
        imbalances[bp] = (bd - ad) / (bd + ad) if bd + ad else None
    slip = {}
    for target in SLIPPAGE_USD:
        slip[target] = (
            estimate_slippage_bps(asks, mid, ct_val, target) if mid else None,
            estimate_slippage_bps(bids, mid, ct_val, target) if mid else None,
        )
    return (
        collected_ts, cycle_id, symbol, min(50, len(bids), len(asks)),
        best_bid, best_ask, mid, spread,
        depths[10][0], depths[10][1], depths[25][0], depths[25][1],
        depths[50][0], depths[50][1],
        imbalances[10], imbalances[25], imbalances[50],
        slip[100][0], slip[100][1], slip[500][0], slip[500][1],
        slip[1000][0], slip[1000][1],
        ms_to_iso(book.get("ts")), book.get("seqId"),
        json.dumps(bids[:50], ensure_ascii=False),
        json.dumps(asks[:50], ensure_ascii=False), "okx",
    )


def flow_features(trades: list, ct_val: float, cycle_id: str, collected_ts: str, symbol: str) -> tuple:
    buy_qty = sell_qty = buy_usd = sell_usd = largest = 0.0
    timestamps = []
    for trade in trades:
        qty, px = to_float(trade.get("sz")), to_float(trade.get("px"))
        if qty is None or px is None:
            continue
        notion = qty * px * ct_val
        largest = max(largest, notion)
        if trade.get("side") == "buy":
            buy_qty += qty
            buy_usd += notion
        elif trade.get("side") == "sell":
            sell_qty += qty
            sell_usd += notion
        try:
            timestamps.append(int(trade.get("ts")))
        except (TypeError, ValueError):
            pass
    total = buy_usd + sell_usd
    start_ms = min(timestamps) if timestamps else None
    end_ms = max(timestamps) if timestamps else None
    return (
        collected_ts, cycle_id, symbol, len(trades),
        ms_to_iso(start_ms), ms_to_iso(end_ms),
        (end_ms - start_ms) if start_ms is not None and end_ms is not None else None,
        buy_qty, sell_qty, buy_usd, sell_usd,
        buy_usd / total if total else None, buy_usd - sell_usd, largest,
        json.dumps(trades[:50], ensure_ascii=False), "okx_recent_trades",
    )


def prune_feature_history(con: sqlite3.Connection, retention_days: int) -> dict[str, int]:
    """由本表权威writer裁剪历史；新表ts固定为UTC-Z，可直接交给SQLite datetime解析。"""
    if not 7 <= retention_days <= 365:
        raise ValueError("retention_days必须在7..365之间")
    out = {}
    for table in ("market_microstructure", "market_trade_flow"):
        cur = con.execute(
            f"DELETE FROM {table} WHERE datetime(ts) < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        out[table] = max(0, cur.rowcount)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="50档订单簿与逐笔成交影子采集")
    ap.add_argument("--db-root", default=str(ROOT / "db"))
    ap.add_argument("--focus-file", default=str(ROOT / "focus.md"))
    ap.add_argument("--max-symbols", type=int, default=50)
    ap.add_argument("--depth", type=int, default=50)
    ap.add_argument("--trades-limit", type=int, default=500)
    ap.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    ap.add_argument("--cycle", default=None)
    args = ap.parse_args()
    if args.depth != 50:
        print(json.dumps({"ok": False, "error": "生产深度固定为50档"}, ensure_ascii=False))
        return 2

    db_path = Path(args.db_root) / "market.db"
    con = sqlite3.connect(str(db_path), timeout=20)
    try:
        symbols = select_symbols(con, max(3, min(args.max_symbols, 100)), Path(args.focus_file))
        specs = {
            r[0]: float(r[1] or 0)
            for r in con.execute(
                "SELECT instId,ctVal FROM instruments_cache WHERE instId IN (%s)"
                % ",".join("?" for _ in symbols), symbols
            ).fetchall()
        } if symbols else {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            books_future = ex.submit(fetch_orderbooks_batch_sync, symbols, 50)
            trades_future = ex.submit(fetch_recent_trades_batch_sync, symbols, args.trades_limit)
            books = books_future.result()
            trades = trades_future.result()

        collected_ts = utc_now_iso()
        cycle = args.cycle or cycle_id_now()
        book_rows = []
        flow_rows = []
        errors = []
        for symbol in symbols:
            ct_val = specs.get(symbol, 0)
            book = books.get(symbol) or {}
            trade_rows = trades.get(symbol) or []
            if ct_val <= 0:
                errors.append(f"{symbol}:ctVal_missing")
                continue
            if book.get("bids") and book.get("asks"):
                book_rows.append(book_features(book, ct_val, cycle, collected_ts, symbol))
            else:
                errors.append(f"{symbol}:book_empty")
            if trade_rows:
                flow_rows.append(flow_features(trade_rows, ct_val, cycle, collected_ts, symbol))
            else:
                errors.append(f"{symbol}:trades_empty")

        con.executemany(
            "INSERT OR REPLACE INTO market_microstructure VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            book_rows,
        )
        con.executemany(
            "INSERT OR REPLACE INTO market_trade_flow VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            flow_rows,
        )
        pruned = {}
        if cycle.endswith("T04:45"):
            pruned = prune_feature_history(con, args.retention_days)
        con.commit()
        out = {
            "ok": bool(book_rows),
            "cycle": cycle,
            "selected": symbols,
            "wrote": {"microstructure": len(book_rows), "trade_flow": len(flow_rows)},
            "depth_levels": 50,
            "retention_days": args.retention_days,
            "pruned": pruned,
            "errors": errors[:20],
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0 if book_rows else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
