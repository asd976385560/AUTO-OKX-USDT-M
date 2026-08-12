# -*- coding: utf-8 -*-
r"""确定性累计交易 PnL（2026-06-26）。

模型：累计交易 PnL = **冻结历史基线** + **V2.0 期 trades.pnl 增量**
  baseline = account.db.system_state.{profile}_cum_pnl（冻结的累计收益基线）
  v2_delta = SUM({profile}_trades.db.trades.pnl WHERE ts>=reset_ts)（V2.0 期已平仓真实 pnl；
             reset_ts = system_state.{profile}_cum_pnl_reset_ts，缺省则全量）
  cum_pnl  = baseline + v2_delta

该指标不是账户净收益：不扣交易手续费，不含资金费/利息与浮动盈亏。账户净变动
应从 account_bills 的交易/资金费账单 ``bal_change`` 单独展示，并注明覆盖区间。

为何用脚本：push.md 红线「累计收益禁 agent 自查 SQL 算」——本脚本是确定性 writer/reader，
agent 取本脚本**回执**（非自己拼 SQL），既得真值又守红线。trades.db 仅含 V2.0 期成交，
故必须叠加冻结基线才是终生累计（裸 SUM(trades.pnl) 会丢 −281/−282 历史）。

权威基线键＝`{profile}_cum_pnl`（其余 *_realized_pnl* 为历史僵尸键，已清理，见 memory）。

用法：
  python cum_pnl.py --profile live --db-root ./db
  python cum_pnl.py --both --db-root ./db        # {"live": {...}} 包一层
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _state(db_root: Path, profile: str):
    """读 baseline(`{p}_cum_pnl`) + reset_ts(`{p}_cum_pnl_reset_ts`) —— 同一只读连接。
    清零后 baseline=0、reset_ts=清零时刻；reset_ts 与 trades.ts 同口径(UTC+8 字符串)。"""
    db = db_root / "account.db"
    baseline, reset_ts = None, None
    if not db.exists():
        return baseline, reset_ts
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            rb = con.execute("SELECT value FROM system_state WHERE key=?",
                             (f"{profile}_cum_pnl",)).fetchone()
            rt = con.execute("SELECT value FROM system_state WHERE key=?",
                             (f"{profile}_cum_pnl_reset_ts",)).fetchone()
        finally:
            con.close()
        if rb and rb[0] not in (None, ""):
            baseline = float(rb[0])
        if rt and rt[0] not in (None, ""):
            reset_ts = str(rt[0])
    except Exception:
        pass
    return baseline, reset_ts


def _v2_delta(db_root: Path, profile: str, reset_ts=None, as_of_ts=None):
    """SUM(trades.pnl)；可限定 reset_ts 起点与 as_of_ts 报告时点。"""
    db = db_root / f"{profile}_trades.db"
    if not db.exists():
        return 0.0, 0
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            clauses = ["pnl IS NOT NULL"]
            args = []
            if reset_ts:
                clauses.append("ts >= ?")
                args.append(reset_ts)
            if as_of_ts:
                clauses.append("ts <= ?")
                args.append(as_of_ts)
            r = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades WHERE "
                + " AND ".join(clauses), tuple(args)).fetchone()
        finally:
            con.close()
        return float(r[1] or 0.0), int(r[0] or 0)
    except Exception:
        return 0.0, 0


def cum_for(db_root, profile: str, as_of_ts=None) -> dict:
    db_root = Path(db_root)
    baseline, reset_ts = _state(db_root, profile)
    delta, n = _v2_delta(db_root, profile, reset_ts, as_of_ts)
    cum = (baseline if baseline is not None else 0.0) + delta
    return {
        "profile": profile,
        "ok": baseline is not None,
        "baseline": baseline,
        "reset_ts": reset_ts,
        "as_of_ts": as_of_ts,
        "v2_delta": round(delta, 4),
        "n_v2_trades": n,
        "cum_pnl": round(cum, 4),
        "metric": "cumulative_trade_pnl",
        "excludes": ["trading_fees", "funding", "interest", "unrealized_pnl"],
        "note": None if baseline is not None else "baseline 缺（system_state.{p}_cum_pnl 无）→ 仅增量",
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="累计交易PnL（未扣手续费/资金费）= 冻结基线 + V2.0 trades.pnl 增量")
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--profile", choices=["live"])
    ap.add_argument("--both", action="store_true")
    ap.add_argument("--as-of", dest="as_of_ts", help="累计到该 UTC+8 时点（YYYY-MM-DD HH:MM:SS）")
    args = ap.parse_args()
    # 2026-08-06 demo 全量下线：`--both` 与「不传 --profile」原先都会算 demo。
    # 库删掉后 _v2_delta 的 `if not db.exists()` 会兜住不抛，但输出里会多一个
    # baseline=None / cum_pnl=0.0 的 demo 块——那是**假数**，会被下游当真值读。
    # `--both` 保留为兼容别名（脚本 docstring 与历史运维记录都写过），只出 live。
    if args.both or not args.profile:
        out = {"live": cum_for(args.db_root, "live", args.as_of_ts)}
    else:
        out = cum_for(args.db_root, args.profile, args.as_of_ts)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
