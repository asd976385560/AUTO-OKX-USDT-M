# -*- coding: utf-8 -*-
r"""experience_features_v2.py — v2 相似特征派生与回填（Wave2 序9）。

派生（全部确定性，as-of 语义）：
  asset_class        core.asset_class 权威表
  stop_distance_pct  |fill_px - sl_trigger_px| / fill_px（行 raw）
  planned_rr         raw.decision_card.ev_check.gross_rr（Wave1 起）
                     或 risk_reward entry/stop/target 几何重算（旧卡）
  funding_rate       market.db.derivatives 最近一条 ≤ as_of（4h 内，否则 None）
  vol_24h_pct        15m K 线 as_of 前 24h (max(h)-min(l))/last(c)
  trend_1h/4h        1H/4H K 线 as_of 时 MA20 vs MA50（+1/-1；bars<50=None）

CLI 回填：把 trade_experiences 全部行的 experience_vector 升级为
  {"v":2, "features": {...}, "legacy_v1": [旧10维数组]}
旧向量原位保留（终稿放行条件：旧向量可追溯）。幂等：已是 v2 的行跳过。
默认 dry-run；--apply 真写。kline/derivatives 保留期 ≈3 个月，覆盖全部现存行；
个别派生不出的特征 None 如实留空（覆盖惩罚在 similarity_v2 内）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SCRIPTS = str(_ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import _simutil  # noqa: E402
from core.asset_class import asset_class_of  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))


def _cst_to_utcz(ts_cst: str) -> Optional[str]:
    try:
        dt = datetime.strptime(str(ts_cst)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=CST).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _ma(vals: list[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def derive_market_features(mcon: sqlite3.Connection, symbol: str,
                           as_of_cst: str) -> dict[str, Any]:
    """funding / vol / trend as-of（只读 market.db；缺数据=None）。"""
    out: dict[str, Any] = {"funding_rate": None, "vol_24h_pct": None,
                           "trend_1h": None, "trend_4h": None}
    as_of_z = _cst_to_utcz(as_of_cst)
    if not as_of_z:
        return out
    row = mcon.execute(
        "SELECT funding_rate FROM derivatives WHERE symbol=? AND ts<=? "
        "AND ts>=datetime(?, '-4 hours') ORDER BY ts DESC LIMIT 1",
        (symbol, as_of_z, as_of_z)).fetchone()
    if row and row[0] is not None:
        try:
            out["funding_rate"] = float(row[0])
        except (TypeError, ValueError):
            pass
    bars = mcon.execute(
        "SELECT h, l, c FROM kline_cache WHERE symbol=? AND tf='15m' "
        "AND ts<=? AND ts>=datetime(?, '-24 hours') ORDER BY ts",
        (symbol, as_of_z, as_of_z)).fetchall()
    if len(bars) >= 24:
        hi = max(b[0] for b in bars if b[0] is not None)
        lo = min(b[1] for b in bars if b[1] is not None)
        last_c = next((b[2] for b in reversed(bars) if b[2]), None)
        if hi and lo and last_c:
            out["vol_24h_pct"] = round((hi - lo) / last_c, 6)
    for tf, key in (("1H", "trend_1h"), ("4H", "trend_4h")):
        closes = [r[0] for r in mcon.execute(
            "SELECT c FROM kline_cache WHERE symbol=? AND tf=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 50", (symbol, tf, as_of_z)).fetchall()
            if r[0] is not None]
        closes.reverse()
        ma20, ma50 = _ma(closes, 20), _ma(closes, 50)
        if ma20 is not None and ma50 is not None:
            out[key] = 1 if ma20 > ma50 else (-1 if ma20 < ma50 else 0)
    return out


def _planned_rr_from_card(card: Any) -> Optional[float]:
    if not isinstance(card, dict):
        return None
    ev = card.get("ev_check")
    if isinstance(ev, dict) and isinstance(ev.get("gross_rr"), (int, float)):
        return float(ev["gross_rr"])
    rr = card.get("risk_reward")
    if isinstance(rr, dict):
        try:
            entry = float(rr.get("entry"))
            stop = float(rr.get("stop"))
            target = float(rr.get("target"))
            risk = abs(entry - stop)
            if risk > 0:
                return round(abs(target - entry) / risk, 4)
        except (TypeError, ValueError):
            pass
    return None


def features_for_row(mcon: sqlite3.Connection, row: sqlite3.Row,
                     db_root: Path) -> dict[str, Any]:
    """一条 trade_experiences 行 → v2 特征 dict。"""
    raw: dict[str, Any] = {}
    try:
        parsed = json.loads(row["raw"] or "{}")
        if isinstance(parsed, dict):
            raw = parsed
    except json.JSONDecodeError:
        pass
    stop_distance = None
    try:
        fill_px = float(raw.get("fill_px") or raw.get("px") or 0)
        sl = float(raw.get("sl_trigger_px") or 0)
        if fill_px > 0 and sl > 0:
            stop_distance = round(abs(fill_px - sl) / fill_px, 6)
    except (TypeError, ValueError):
        pass
    base = {
        "asset_class": asset_class_of(row["symbol"], db_root),
        "side": row["side"], "action": row["action"], "regime": row["regime"],
        "stop_distance_pct": stop_distance,
        "planned_rr": _planned_rr_from_card(raw.get("decision_card")),
    }
    base.update(derive_market_features(mcon, row["symbol"], row["ts"]))
    return _simutil.experience_features_v2(base)


def backfill(db_root: Path, apply: bool) -> dict[str, Any]:
    acon = sqlite3.connect(str(db_root / "account.db"), timeout=15)
    acon.execute("PRAGMA busy_timeout=10000")
    acon.row_factory = sqlite3.Row
    mcon = sqlite3.connect(
        f"file:{db_root / 'market.db'}?mode=ro", uri=True, timeout=15)
    try:
        rows = acon.execute(
            "SELECT id, ts, symbol, side, action, regime, regime_stale, "
            "score_total, experience_vector, raw FROM trade_experiences "
            "ORDER BY id").fetchall()
        upgraded = skipped_v2 = 0
        coverage_counter: dict[str, int] = {}
        updates = []
        for row in rows:
            legacy = None
            try:
                stored = json.loads(row["experience_vector"] or "null")
            except json.JSONDecodeError:
                stored = None
            if isinstance(stored, dict) and stored.get("v") == 2:
                skipped_v2 += 1
                continue
            if isinstance(stored, list):
                legacy = stored
            feats = features_for_row(mcon, row, db_root)
            for k, v in feats.items():
                if k in ("v",):
                    continue
                if v is not None:
                    coverage_counter[k] = coverage_counter.get(k, 0) + 1
            payload = {"v": 2, "features": feats, "legacy_v1": legacy}
            updates.append((json.dumps(payload, ensure_ascii=False), row["id"]))
            upgraded += 1
        if apply and updates:
            acon.executemany(
                "UPDATE trade_experiences SET experience_vector=? WHERE id=?",
                updates)
            acon.commit()
        return {"ok": True, "dry_run": not apply, "total_rows": len(rows),
                "upgraded": upgraded, "already_v2": skipped_v2,
                "feature_coverage": coverage_counter}
    finally:
        acon.close()
        mcon.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="经验特征 v2 回填（默认 dry-run）")
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    print(json.dumps(backfill(Path(args.db_root), args.apply),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
