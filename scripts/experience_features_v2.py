# -*- coding: utf-8 -*-
r"""experience_features_v2.py — 经验特征派生（v2 冻结、v3 前向启用）。

派生（全部确定性，as-of 语义）：
  asset_class        core.asset_class 权威表
  stop_distance_pct  |fill_px - sl_trigger_px| / fill_px（行 raw）
  planned_rr         raw.decision_card.ev_check.gross_rr（Wave1 起）
                     或 risk_reward entry/stop/target 几何重算（旧卡）
  funding_rate       market.db.derivatives 最近一条 ≤ as_of（4h 内，否则 None）
  vol_24h_pct        15m K 线 as_of 前 24h (max(h)-min(l))/last(c)
  trend_1h/4h        1H/4H K 线 as_of 时 MA20 vs MA50（+1/-1；bars<50=None）

现有 v2 向量按历史证据冻结，不回填、不重算。部署后 writer 仅新增带明确
``experience_features_v3_strict_24h`` epoch 的 v3；finder 也只在完全相同的
v3 epoch 内比较，禁止 v2/v3 静默混算。CLI 仅报告版本分布，--apply fail-closed。
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
    raw = str(ts_cst or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw[-1:].upper() == "Z" else raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _ma(vals: list[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _utc_window_bounds(as_of_z: str, hours: int) -> tuple[str, str]:
    upper = datetime.strptime(as_of_z, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    lower = upper - timedelta(hours=hours)
    return (
        lower.strftime("%Y-%m-%dT%H:%M:%SZ"),
        upper.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def derive_market_features(mcon: sqlite3.Connection, symbol: str,
                           as_of_cst: str) -> dict[str, Any]:
    """v3 funding / vol / trend as-of（严格 ``(lower, as_of]`` 窗）。"""
    out: dict[str, Any] = {"funding_rate": None, "vol_24h_pct": None,
                           "trend_1h": None, "trend_4h": None}
    as_of_z = _cst_to_utcz(as_of_cst)
    if not as_of_z:
        return out
    funding_lower_z, _ = _utc_window_bounds(as_of_z, 4)
    row = mcon.execute(
        "SELECT funding_rate FROM derivatives WHERE symbol=? AND ts<=? "
        "AND ts>? ORDER BY ts DESC LIMIT 1",
        (symbol, as_of_z, funding_lower_z)).fetchone()
    if row and row[0] is not None:
        try:
            out["funding_rate"] = float(row[0])
        except (TypeError, ValueError):
            pass
    vol_lower_z, _ = _utc_window_bounds(as_of_z, 24)
    bars = mcon.execute(
        "SELECT ts, h, l, c FROM kline_cache WHERE symbol=? AND tf='15m' "
        "AND ts<=? AND ts>? ORDER BY ts",
        (symbol, as_of_z, vol_lower_z)).fetchall()
    upper_dt = datetime.strptime(as_of_z, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    expected_ts = {
        (upper_dt - timedelta(minutes=15 * offset)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        for offset in range(96)
    }
    observed_ts = {str(row[0]) for row in bars}
    if len(bars) == 96 and observed_ts == expected_ts:
        hi = max(b[1] for b in bars if b[1] is not None)
        lo = min(b[2] for b in bars if b[2] is not None)
        last_c = next((b[3] for b in reversed(bars) if b[3]), None)
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
    """一条 trade_experiences 行 → 当前前向 v3 特征 dict（不写库）。"""
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
    return _simutil.experience_features_v3(base)


def backfill(db_root: Path, apply: bool) -> dict[str, Any]:
    acon = sqlite3.connect(
        f"file:{db_root / 'account.db'}?mode=ro", uri=True, timeout=15)
    acon.row_factory = sqlite3.Row
    try:
        rows = acon.execute(
            "SELECT id, experience_vector FROM trade_experiences "
            "ORDER BY id").fetchall()
        versions = {"v1_or_legacy": 0, "v2_frozen": 0, "v3_forward": 0,
                    "invalid": 0}
        for row in rows:
            try:
                stored = json.loads(row["experience_vector"] or "null")
            except json.JSONDecodeError:
                versions["invalid"] += 1
                continue
            if isinstance(stored, dict) and stored.get("v") == 3:
                versions["v3_forward"] += 1
            elif isinstance(stored, dict) and stored.get("v") == 2:
                versions["v2_frozen"] += 1
            elif isinstance(stored, list) or stored is None:
                versions["v1_or_legacy"] += 1
            else:
                versions["invalid"] += 1
        out = {
            "ok": not apply,
            "dry_run": True,
            "historical_mutation": False,
            "total_rows": len(rows),
            "versions": versions,
            "feature_epoch": _simutil.FEATURE_EPOCH_V3,
        }
        if apply:
            out["error"] = "historical_feature_backfill_frozen"
        return out
    finally:
        acon.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="经验特征版本分布（v2冻结；--apply 恒拒绝）")
    ap.add_argument("--db-root", default=_public_project_path('db'))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    result = backfill(Path(args.db_root), args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
