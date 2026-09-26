# -*- coding: utf-8 -*-
r"""apply_path_metrics_schema.py — 路径与退出埋点（Wave2 序10）。

trade_experiences 新增 7 列（全部可空，ALTER ADD）：
  initial_risk_usdt  开仓名义 × |入场-SL|/入场（raw 的 sl_trigger_px/fill_px）
  mfe_r / mae_r      持仓期内最大有利/不利波幅（R 单位；15m K 线路径）
  realized_r_net     (realized_pnl - 费用估计) / initial_risk_usdt
  close_at_1r        1=平仓时已 ≥1R | 0=否 | NULL=风险基准不可得
  exit_category      互斥出口类别（复用 exit_taxonomy_report.classify）
  path_coverage      K 线路径覆盖率 'full'|'partial:<pct>'|
                     'partial_boundary:<pct>'|'none'
  path_metric_version 当前边界/未知语义版本（v2）
并回填全部 closed 行 + 真实回填 ever_hit_1r（Wave0 起恒 NULL 的承诺兑现：
mfe_r≥1 → 1；路径完整且 <1 → 0；路径不完整 → 维持 NULL，未知不冒充 0）。

费用估计 = 名义 × (RISK_FEE_BUFFER_PCT + RISK_SLIPPAGE_BUFFER_PCT)/2 ×2 边
（与 risk_validator 单一真源）。默认 dry-run；--apply 必配 --backup-dir。
幂等：已有列跳过加列；v2 会重算所有旧版本 closed 行并覆盖 v1 泄漏值。
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
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.risk_validator import (  # noqa: E402
    RISK_FEE_BUFFER_PCT,
    RISK_SLIPPAGE_BUFFER_PCT,
)
from exit_taxonomy_report import classify_exit  # noqa: E402,F401  旧名保留供调用方

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
FRICTION_PCT = RISK_FEE_BUFFER_PCT + RISK_SLIPPAGE_BUFFER_PCT

NEW_COLUMNS = (
    ("initial_risk_usdt", "REAL"),
    ("mfe_r", "REAL"),
    ("mae_r", "REAL"),
    ("realized_r_net", "REAL"),
    ("close_at_1r", "INTEGER"),
    ("exit_category", "TEXT"),
    ("path_coverage", "TEXT"),
    ("path_metric_version", "INTEGER"),
)
# 2026-08-19 G1/G2：v3 = 1R 统一为**净风险**（与 core/ev_calculator 同口径）
# + path_coverage 的 'full' 改判「包络完整」。版本号跳变即触发全量回填
# （回填 WHERE 就是 COALESCE(path_metric_version,0)<>PATH_METRIC_VERSION）。
PATH_METRIC_VERSION = 3
BAR_SECONDS = 15 * 60


def _parse_cst(ts_cst: str) -> Optional[datetime]:
    try:
        dt = datetime.strptime(str(ts_cst)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=CST)


def _utcz(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ceil_bar(dt: datetime) -> datetime:
    epoch = int(dt.timestamp())
    rounded = ((epoch + BAR_SECONDS - 1) // BAR_SECONDS) * BAR_SECONDS
    return datetime.fromtimestamp(rounded, tz=dt.tzinfo)


def _floor_bar(dt: datetime) -> datetime:
    """向下取整到所属 15m bar 起点（构造包住开/平仓时刻的跨界 bar）。"""
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(
        (epoch // BAR_SECONDS) * BAR_SECONDS, tz=dt.tzinfo)


# v3 起不再参与 path_coverage 判定，仅供诊断保留。
def _is_bar_boundary(dt: datetime) -> bool:
    return dt.second == 0 and dt.microsecond == 0 and dt.minute % 15 == 0


def compute_path_metrics(mcon: sqlite3.Connection, symbol: str, side: str,
                         entry_px: float, sl_px: float, notional: float,
                         open_ts_cst: str, close_ts_cst: str,
                         realized_pnl: Optional[float]) -> dict[str, Any]:
    """一段持仓的路径指标（确定性；K 线不全 → 覆盖如实、指标 NULL）。"""
    out: dict[str, Any] = {
        "initial_risk_usdt": None, "mfe_r": None, "mae_r": None,
        "realized_r_net": None, "close_at_1r": None,
        "ever_hit_1r": None, "path_coverage": "none",
        "path_metric_version": PATH_METRIC_VERSION,
    }
    risk_dist = abs(entry_px - sl_px) / entry_px if entry_px > 0 else 0
    if risk_dist <= 0 or notional <= 0:
        return out
    # G1 v3 单一口径：1R = 净风险 = 毛止损距 + 摩擦，与 core/ev_calculator 的
    # risk_net = risk + FRICTION_PCT 完全一致 → 「打到止损」恰好 -1R。
    # v2 的分母是毛止损距、只在 realized 的分子扣摩擦，而 mfe_r/mae_r 分子分母
    # 都不含摩擦，于是 exit_quality 的 giveback = mfe_r - realized_r_net 拿
    # 「无摩擦 MFE」减「含摩擦 realized」，164 行实测中位虚增 0.065R、
    # 紧止损单最高 0.70R。
    risk_net_dist = risk_dist + FRICTION_PCT
    initial_risk = notional * risk_net_dist
    out["initial_risk_usdt"] = round(initial_risk, 4)
    if realized_pnl is not None:
        fee_est = notional * FRICTION_PCT
        out["realized_r_net"] = round(
            (float(realized_pnl) - fee_est) / initial_risk, 4)
        out["close_at_1r"] = 1 if out["realized_r_net"] >= 1.0 else 0
    opened, closed = _parse_cst(open_ts_cst), _parse_cst(close_ts_cst)
    if not opened or not closed or closed <= opened:
        return out
    # kline_cache.ts 是 bar start。只允许完整落在持仓窗内的 15m bar：
    # bar_start >= open 且 bar_start+15m <= close，杜绝开仓前/平仓后路径泄漏。
    first = _ceil_bar(opened)
    last = closed - timedelta(seconds=BAR_SECONDS)
    if first > last:
        return out
    expected = int((last - first).total_seconds() // BAR_SECONDS) + 1
    if expected <= 0:
        return out
    bars = mcon.execute(
        "SELECT ts, h, l FROM kline_cache WHERE symbol=? AND tf='15m' "
        "AND ts>=? AND ts<=? ORDER BY ts",
        (symbol, _utcz(first), _utcz(last)),
    ).fetchall()
    distinct = {str(b[0]) for b in bars}
    coverage = min(1.0, len(distinct) / expected)
    # G2 v3：'full' 不再要求开/平仓时间戳精确落在 :00/:15/:30/:45（真实成交
    # 命中概率约 1/900 → 实测 164 行 path_coverage **全部** partial_boundary、
    # 0 行 full，使 ever_hit_1r=0 分支成为 100% 死代码，连带 exit_quality 的
    # ever_hit_1r_flag_disagreements 自检结构性恒为 0）。改判「包络完整」：
    # 内部 bar 一根不缺，且包住开/平仓时刻的两根跨界 bar 也在库里 ——
    # 此时整段持仓被 K 线完全笼罩，可用包络极值给出真实 MFE 的上界。
    env_first = _floor_bar(opened)
    # 平仓恰好落在 bar 边界时，以 close 起点的那根 bar 不属于持仓窗；
    # 退 1 微秒再向下取整，边界与非边界两种情形统一。
    env_last = _floor_bar(closed - timedelta(microseconds=1))
    env_expected = int(
        (env_last - env_first).total_seconds() // BAR_SECONDS) + 1
    env_bars = mcon.execute(
        "SELECT ts, h, l FROM kline_cache WHERE symbol=? AND tf='15m' "
        "AND ts>=? AND ts<=? ORDER BY ts",
        (symbol, _utcz(env_first), _utcz(env_last)),
    ).fetchall()
    envelope_full = len({str(b[0]) for b in env_bars}) >= env_expected > 0
    if not bars:
        out["path_coverage"] = "none"
    elif coverage >= 1.0 and envelope_full:
        out["path_coverage"] = "full"
    else:
        # 前缀保持 'partial'：exit_quality / validate_daily_report 的解析器写的
        # 是 startswith("partial")，历史 'partial_boundary:...' 行仍可解析。
        out["path_coverage"] = f"partial:{coverage:.2f}"
    highs = [float(b[1]) for b in bars if b[1] is not None]
    lows = [float(b[2]) for b in bars if b[2] is not None]
    if not highs or not lows:
        return out
    if str(side).lower() == "long":
        mfe = max(0.0, (max(highs) - entry_px) / entry_px)
        mae = max(0.0, (entry_px - min(lows)) / entry_px)
    else:
        mfe = max(0.0, (entry_px - min(lows)) / entry_px)
        mae = max(0.0, (max(highs) - entry_px) / entry_px)
    # G1 净口径：在峰值离场同样要付摩擦 → 分子扣 F；在最差点离场要多付 F →
    # 分子加 F。分母同为净风险，故「打到止损」恒等于 mae_r=1.0，
    # giveback = mfe_r - realized_r_net 两端同口径可减。
    out["mfe_r"] = round(max(0.0, mfe - FRICTION_PCT) / risk_net_dist, 4)
    out["mae_r"] = round((mae + FRICTION_PCT) / risk_net_dist, 4)
    if out["mfe_r"] >= 1.0:
        # 部分路径也能正向证明“曾触达”。
        out["ever_hit_1r"] = 1
    elif out["path_coverage"] == "full":
        # G2：包络极值是真实 MFE 的**上界**（含仓外价格，只会高估）。上界仍
        # <1R → 真值必然 <1R，「未触达」被证明，可以写 0；否则维持 NULL。
        env_hi = [float(b[1]) for b in env_bars if b[1] is not None]
        env_lo = [float(b[2]) for b in env_bars if b[2] is not None]
        if env_hi and env_lo:
            env_mfe = (max(0.0, (max(env_hi) - entry_px) / entry_px)
                       if str(side).lower() == "long"
                       else max(0.0, (entry_px - min(env_lo)) / entry_px))
            if max(0.0, env_mfe - FRICTION_PCT) / risk_net_dist < 1.0:
                out["ever_hit_1r"] = 0
    return out


def metrics_for_row(mcon: sqlite3.Connection, row: sqlite3.Row,
                    db_root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    try:
        raw = json.loads(row["raw"] or "{}")
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        entry = float(raw.get("fill_px") or raw.get("px") or 0)
        sl = float(raw.get("sl_trigger_px") or 0)
    except (TypeError, ValueError):
        entry = sl = 0.0
    notional = None
    try:
        notional = float(raw.get("notional") or 0)
    except (TypeError, ValueError):
        pass
    if not notional:
        try:
            ct_val = float(raw.get("ct_val") or 1.0)
            sz = float(row["open_sz"] or raw.get("sz") or 0)
            notional = sz * ct_val * entry
        except (TypeError, ValueError):
            notional = 0.0
    if entry <= 0 or sl <= 0 or not notional:
        return None
    close_ts = row["closed_at"] or row["ts"]
    metrics = compute_path_metrics(
        mcon, row["symbol"], row["side"], entry, sl, notional,
        row["ts"], close_ts, row["realized_pnl"])
    # 出口类别（2026-09-26 对照 V3 按价位实证）：close_events 最后一次事件的
    # 理由 / 来源 / 成交价 + 开仓 SL/TP + 持仓期内移动过的止损 + 平仓前 15m 收盘。
    import exit_taxonomy_report as _exit_taxonomy
    context = _exit_taxonomy.exit_context(
        raw, None, symbol=row["symbol"], side=row["side"], open_ts=row["ts"],
        close_ts=close_ts, realized_pnl=row["realized_pnl"], db_root=db_root,
        mcon=mcon)
    metrics["exit_category"] = _exit_taxonomy.classify_exit(**context)
    return metrics


def main() -> int:
    ap = argparse.ArgumentParser(
        description="trade_experiences 路径/退出埋点迁移+回填（默认 dry-run）")
    ap.add_argument("--db-root", default=_public_project_path('db'))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-dir", default=None)
    args = ap.parse_args()
    root = Path(args.db_root)
    acc_path = root / "account.db"
    if not acc_path.exists():
        print(json.dumps({"ok": False, "error": f"库不存在: {acc_path}"}))
        return 2

    acon = sqlite3.connect(acc_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    acon.execute("PRAGMA busy_timeout=10000")
    acon.row_factory = sqlite3.Row
    mcon = sqlite3.connect(
        f"file:{root / 'market.db'}?mode=ro", uri=True, timeout=15)
    try:
        have = {str(r[1]) for r in acon.execute(
            "PRAGMA table_info(trade_experiences)")}
        missing_cols = [c for c, _ in NEW_COLUMNS if c not in have]
        where_backfill = (
            "status='closed' AND COALESCE(path_metric_version,0)<>?"
            if "path_metric_version" in have else "status='closed'")
        n_backfill = acon.execute(
            f"SELECT COUNT(*) FROM trade_experiences WHERE {where_backfill}",
            (() if "path_metric_version" not in have
             else (PATH_METRIC_VERSION,)),
        ).fetchone()[0]
        report = {"db": str(acc_path), "dry_run": not args.apply,
                  "missing_columns": missing_cols,
                  "closed_rows_to_backfill": n_backfill}
        if not missing_cols and n_backfill == 0:
            print(json.dumps({**report, "ok": True, "action": "none"},
                             ensure_ascii=False, indent=1))
            return 0
        if not args.apply:
            print(json.dumps({**report, "ok": True, "action": "plan-only"},
                             ensure_ascii=False, indent=1))
            return 0
        if not args.backup_dir:
            print(json.dumps({**report, "ok": False,
                              "error": "--apply 必须配 --backup-dir"},
                             ensure_ascii=False, indent=1))
            return 2
        bdir = Path(args.backup_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_path = bdir / f"account.db.bak_path-metrics_{tag}"
        bak = sqlite3.connect(str(bak_path))
        with bak:
            acon.backup(bak)
        qc = bak.execute("PRAGMA quick_check").fetchone()[0]
        bak.close()
        if qc != "ok":
            print(json.dumps({**report, "ok": False,
                              "error": f"备份 quick_check={qc}"},
                             ensure_ascii=False, indent=1))
            return 2

        acon.close()
        acon = sqlite3.connect(acc_path, timeout=15)
        acon.execute("PRAGMA busy_timeout=30000")
        acon.row_factory = sqlite3.Row

        for col, typ in NEW_COLUMNS:
            if col not in have:
                acon.execute(
                    f"ALTER TABLE trade_experiences ADD COLUMN {col} {typ}")
        acon.commit()

        rows = acon.execute(
            "SELECT id, ts, closed_at, symbol, side, open_sz, realized_pnl, "
            "raw FROM trade_experiences "
            "WHERE status='closed' AND COALESCE(path_metric_version,0)<>?",
            (PATH_METRIC_VERSION,)).fetchall()
        filled = skipped = ever1 = 0
        for row in rows:
            metrics = metrics_for_row(mcon, row, root)
            if metrics is None:
                acon.execute(
                    "UPDATE trade_experiences SET initial_risk_usdt=NULL, "
                    "mfe_r=NULL, mae_r=NULL, realized_r_net=NULL, "
                    "close_at_1r=NULL, ever_hit_1r=NULL, exit_category=NULL, "
                    "path_coverage='none', path_metric_version=? WHERE id=?",
                    (PATH_METRIC_VERSION, row["id"]),)
                skipped += 1
                continue
            acon.execute(
                "UPDATE trade_experiences SET initial_risk_usdt=?, mfe_r=?, "
                "mae_r=?, realized_r_net=?, close_at_1r=?, ever_hit_1r=?, "
                "exit_category=?, path_coverage=?, path_metric_version=? "
                "WHERE id=?",
                (metrics["initial_risk_usdt"], metrics["mfe_r"],
                 metrics["mae_r"], metrics["realized_r_net"],
                 metrics["close_at_1r"], metrics["ever_hit_1r"],
                 metrics["exit_category"], metrics["path_coverage"],
                 PATH_METRIC_VERSION,
                 row["id"]))
            filled += 1
            if metrics["ever_hit_1r"] == 1:
                ever1 += 1
        acon.commit()
        qc2 = acon.execute("PRAGMA quick_check").fetchone()[0]
        print(json.dumps({**report, "ok": qc2 == "ok", "action": "applied",
                          "backup": str(bak_path), "columns_added": missing_cols,
                          "rows_filled": filled, "rows_no_risk_base": skipped,
                          "ever_hit_1r_true": ever1, "quick_check": qc2},
                         ensure_ascii=False, indent=1))
        return 0 if qc2 == "ok" else 2
    finally:
        acon.close()
        mcon.close()


if __name__ == "__main__":
    raise SystemExit(main())
