# -*- coding: utf-8 -*-
"""日报/周报/月报硬化写入器。

当前约束：
1. 周期编号禁跳号/回滚：trade_day_num = MAX(trade_day_num)+1（事务内）
2. ts 为 UTC+8 字符串 YYYY-MM-DD HH:MM:SS
3. 写后 read-after-write 校验
4. 绝不执行 DELETE/UPDATE 已有 trade_day_num（只 INSERT）
5. --rewrite-null-and-renumber 仅用于显式维护：把 #NULL 行重新编号并补缺号
6. 默认 dry-run 模式（--apply 才真写）
7. 同时落盘 daily/weekly/monthly 的 UTF-8 Markdown（原子替换）

调用：
  echo '<json>' | run_okx_python.ps1 scripts/daily_report_writer.py --stdin
  run_okx_python.ps1 scripts/daily_report_writer.py --json-file path.json [--apply] [--profiles live]
  run_okx_python.ps1 scripts/daily_report_writer.py --rewrite-null-and-renumber [--apply]
  run_okx_python.ps1 scripts/daily_report_writer.py --backfill-daily-revision --report-ts "YYYY-MM-DD HH:MM:SS" [--apply]

说明：2026-08-06 demo 全量下线后只写 live 一段。

退出码：0=成功且校验通过；非0=失败（Agent 须视为 P0）
"""


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import trade_report_stats
import _acceptance_thresholds as thresholds
from _acceptance_thresholds import coverage_migration_facts


def sanitize_text(value: str) -> str:
    """Drop invalid surrogate code points that can appear from PowerShell pipes."""
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
DB_PATH = Path(os.environ.get('OKX_ACCOUNT_DB', _public_project_path('db', 'account.db')))
REPORTS_DIR = Path(os.environ.get('OKX_DAILY_REPORTS_DIR', _public_project_path('reports', 'daily-reports')))
WEEKLY_REPORTS_DIR = Path(os.environ.get(
    'OKX_WEEKLY_REPORTS_DIR', _public_project_path('reports', 'weekly')))
MONTHLY_REPORTS_DIR = Path(os.environ.get(
    'OKX_MONTHLY_REPORTS_DIR', _public_project_path('reports', 'monthly')))
LIVE_TRADES_DB = Path(os.environ.get(
    'OKX_LIVE_TRADES_DB', _public_project_path('db', 'live_trades.db')))
MARKET_DB = Path(os.environ.get('OKX_MARKET_DB', _public_project_path('db', 'market.db')))
LEDGER_DB = Path(os.environ.get('OKX_LEDGER_DB', _public_project_path('db', 'ledger.db')))
LESSONS_DB = Path(os.environ.get('OKX_LESSONS_DB', _public_project_path('db', 'lessons.db')))
BRIEFING_LOG_DIR = Path(os.environ.get(
    'OKX_BRIEFING_LOG_DIR', _public_project_path('logs', 'briefing')))
QUALITY_REPORT_DIR = Path(os.environ.get(
    'OKX_QUALITY_REPORT_DIR', _public_project_path('reports', 'quality')))
REVIEWER_READY_DIR = Path(os.environ.get(
    'OKX_REVIEWER_READY_DIR', str(QUALITY_REPORT_DIR)))
MISSED_OPPORTUNITY_OUTCOME_HOURS = 4
MISSED_OPPORTUNITY_EVIDENCE_STATES = {
    "COMPLETE", "SOURCE_LAG", "NO_DATA", "ERROR",
}
FROZEN_WEEKLY_REPORT_KEYS = {"2026-08-31 00:00:00"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


def now_cst() -> str:
    return datetime.now(CST).strftime(TS_FMT)


def _snapshot_equity(db_path, profile: str, as_of_ts: str | None = None):
    """account.db.account_snapshots 截至报告时点的最新 totalEq。

    按 datetime(ts),rowid DESC，避开 MAX(ts) 词典序坑。
    返回 float 或 None（库缺/锁/异常一律降级 None，不抛、不拖垮日报渲染）。"""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            if as_of_ts:
                r = con.execute(
                    "SELECT totalEq FROM account_snapshots WHERE profile=? AND datetime(ts)<=datetime(?) "
                    "ORDER BY datetime(ts) DESC,rowid DESC LIMIT 1", (profile, as_of_ts)).fetchone()
            else:
                r = con.execute(
                    "SELECT totalEq FROM account_snapshots WHERE profile=? "
                    "ORDER BY ts DESC,rowid DESC LIMIT 1",
                    (profile,)).fetchone()
        finally:
            con.close()
        return float(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


def _authoritative_cum_pnl(db_path, profile: str, as_of_ts: str | None = None):
    """复用 cum_pnl.py 累计交易PnL口径；失败返回 None，绝不回退裸 SUM。"""
    try:
        import cum_pnl
        info = cum_pnl.cum_for(Path(db_path).parent, profile, as_of_ts=as_of_ts)
        return float(info["cum_pnl"]) if info.get("ok") else None
    except Exception:
        return None


def _account_bill_net_for_window(
    db_path, profile: str, start_ts: str, end_ts: str
):
    """OKX 账单复盘周期净变动：交易(type=2)+资金费(type=8)，含手续费。

    返回值带账单覆盖上限，避免把尚未采到报告时点的部分账单冒充完整日净收益。
    """
    try:
        path = Path(db_path).resolve()
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute(
                "SELECT SUM(COALESCE(bal_change,0)),"
                "SUM(COALESCE(fee,0)),SUM(COALESCE(pnl,0)),"
                "MIN(ts),MAX(ts),COUNT(*) FROM account_bills "
                "WHERE profile=? AND type IN ('2','8') "
                "AND ts>=? AND ts<?",
                (profile, start_ts, end_ts),
            ).fetchone()
        finally:
            con.close()
        if not row or not row[5]:
            return None
        return {
            "net": float(row[0] or 0),
            "fees": float(row[1] or 0),
            "pnl_and_funding": float(row[2] or 0),
            "first_ts": row[3], "last_ts": row[4], "rows": int(row[5]),
            "period_start_ts": start_ts, "period_end_ts": end_ts,
            "period_end_exclusive": True,
        }
    except Exception:
        return None


def _fmt_num(value):
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "-"


# ── 2026-08-13 规格书四段（确定性 writer 侧回读；reviewer 只判断不加工） ────
# 全部按 _snapshot_equity 同款降级契约：库缺/锁/异常 → 显式"不可用"文案，
# 不抛、不拖垮日报渲染；绝不伪造数值。市场三表/cross_market 为 UTC-Z 时间，
# 比较前先由报告 CST ts 归一（红线：跨表比较先归一）。

SPEC_SECTIONS_ACTIVATION_TS = "2026-08-14 00:00:00"  # 与 validator 同源激活边界
# 错失机会池 briefing_layer_v1 第二来源（missed_opps_writer 同源常量）：
# 2026-08-15 吞吐契约后 wait 行断流，池对照组换读 briefing 候选快照；该边界起
# 的日报在错失行注明来源口径（计数本身仍=lessons.db 窗口 COUNT，与 validator
# 独立复核天然一致）。边界前历史归档不反向加责。
MISSED_BRIEFING_SOURCE_ACTIVATION_TS = "2026-08-19 08:00:00"
# 退出质量段（浮盈峰值回吐分布、≥50% 保证金收益率复核率与处置、错失止盈池）：
# 预注册激活边界起的日报必须带；边界前的历史归档不反向加责。与 validator 同源。
EXIT_QUALITY_ACTIVATION_TS = "2026-08-16 08:00:00"
# 2026-08-19 P0-1 激活边界，**必须与校验侧逐字同源**：
#   validate_daily_report.TOTAL_REALIZED_PNL_REQUIRED_FROM   = "2026-08-20 08:00:00"
#   validate_periodic_report.TOTAL_REALIZED_PNL_REQUIRED_FROM = "2026-08-20"
# 边界后头条 total_pnl = close + reduce；边界前仍只计 close。
# 只加在校验侧不加写入侧会死锁：补录边界前的日报时 writer 写 close+reduce、
# validator 要 close，必挂 `audit: live report-time facts differ`
# （2026-08-19 实证：08-17 有 2 笔 reduce +9.3774，writer −17.3741 /
#  validator −26.7515）。
TOTAL_REALIZED_PNL_REQUIRED_FROM = "2026-08-20 08:00:00"
# 与 daily_maintenance.PROVISIONAL_ON_FAILURE_STEPS 对齐的那一步名。
# 刻意不 import daily_maintenance：writer 会被隔离测试与补录脚本单独调用，
# 拉进整个维护编排器会带一大票副作用导入。名字本身是契约，两边同改。
EXIT_QUALITY_DEGRADED_STEP = "exit_quality"
PERIODIC_TOTAL_REALIZED_PNL_REQUIRED_FROM = "2026-08-20"
EXIT_QUALITY_SCHEMA_VERSION = 2
EXIT_QUALITY_METHOD_VERSION = "exit_quality_v2_forward_frozen"
# 2026-08-19 G1：净 R 口径起用 v2；此处是**消费/校验**侧，接受 v1|v2，
# 边界前归档的 v1 工件继续通过（历史不反向加责）。
EXIT_QUALITY_PEAK_METHOD_VERSION = "peak_giveback_forward_v2"
EXIT_QUALITY_PEAK_METHOD_VERSIONS_ACCEPTED = (
    "peak_giveback_forward_v1", "peak_giveback_forward_v2")
EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE = "2026-08-15T14:45"
EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_MISSED_TP_METHOD = (
    "post_exit_counterfactual_16x15m_v1")
EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD = (
    "authoritative_exit_fill_market_16x15m_v1")


def _cst_to_utc_z(ts_cst: str) -> str | None:
    try:
        dt = datetime.strptime(str(ts_cst)[:19], TS_FMT)
        return (dt - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _ro_connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(
        f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _market_overview_block(db_root: Path, ts: str) -> str:
    """市场总览（BTC/ETH·总市值·BTC.D·恐贪·regime·TVL），按报告时点回读。"""
    ts_utc = _cst_to_utc_z(ts)
    if ts_utc is None:
        return "市场总览不可用（报告时间无法解析）"
    lines: list[str] = []
    try:
        con = _ro_connect(Path(db_root) / "market.db")
        try:
            snap_ts = con.execute(
                "SELECT MAX(ts) FROM tick_snapshots WHERE ts<=?", (ts_utc,)
            ).fetchone()[0]
            if snap_ts:
                pair_bits = []
                for sym in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
                    r = con.execute(
                        "SELECT last, chg24h FROM tick_snapshots "
                        "WHERE ts=? AND symbol=?", (snap_ts, sym)).fetchone()
                    if r and r["last"] is not None:
                        chg = (f"{r['chg24h']:+.2f}%"
                               if r["chg24h"] is not None else "?")
                        pair_bits.append(
                            f"{sym.split('-')[0]} ${r['last']:,.0f}（24h {chg}）")
                if pair_bits:
                    lines.append(
                        "- " + " | ".join(pair_bits)
                        + f"（快照 {snap_ts}，UTC）")
        finally:
            con.close()
    except Exception:
        lines.append("- BTC/ETH 快照不可用（market.db 缺失或不可读）")
    try:
        con = _ro_connect(Path(db_root) / "regime.db")
        try:
            r = con.execute(
                "SELECT * FROM cross_market WHERE ts<=? "
                "ORDER BY ts DESC LIMIT 1", (ts_utc,)).fetchone()
        finally:
            con.close()
        if r is not None:
            keys = r.keys()

            def val(col):
                return r[col] if col in keys else None

            mcap = val("total_mcap_usd")
            dom = val("btc_dominance")
            fear = val("fear_greed")
            fear_label = val("fear_greed_label")
            tvl = val("defillama_tvl_total")
            lines.append(
                "- 总市值 "
                + (f"${mcap/1e12:.2f}T" if isinstance(mcap, (int, float)) else "未采到")
                + " | BTC.D "
                + (f"{dom:.2f}%" if isinstance(dom, (int, float)) else "未采到")
                + " | 恐贪指数 "
                + (f"{fear:.0f}/{fear_label or '?'}（Alternative.me）"
                   if isinstance(fear, (int, float)) else "未采到")
                + " | TVL "
                + (f"${tvl/1e9:.1f}B" if isinstance(tvl, (int, float)) else "未采到"))
            regime = val("regime")
            if regime:
                lines.append(
                    f"- 24h回归预报={regime}（原regime标签，语义=未来24h均值回归预报，非当前趋势；行 ts={r['ts']} UTC）")
        else:
            lines.append("- cross_market 在报告时点前无行（宏观总览未采到）")
    except Exception:
        lines.append("- 宏观总览不可用（regime.db 缺失或不可读）")
    return "\n".join(lines) if lines else "市场总览数据不可用"


def _cycle_bounds_for_window(start_ts: str, end_ts: str) -> tuple[str, str]:
    """CST 窗口 'YYYY-MM-DD HH:MM:SS' → cycle_id 界 'YYYY-MM-DDTHH:MM'（右开）。"""
    return (
        str(start_ts)[:16].replace(" ", "T"),
        str(end_ts)[:16].replace(" ", "T"),
    )


def _universe_scan_block(db_root: Path, start_ts: str, end_ts: str) -> str:
    """全市场扫描结果：宇宙规模 + 窗口内 analysis 覆盖 + 判断吞吐影子计数。

    相对 rank/候选只是三周期相对选择，非校准概率——本段只给计数与覆盖，
    不显示任何"可信度分值"（独立前向门未过时 confidence_claim_allowed=false）。
    """
    lines: list[str] = []
    ts_utc = _cst_to_utc_z(end_ts)
    try:
        con = _ro_connect(Path(db_root) / "market.db")
        try:
            snap_ts = con.execute(
                "SELECT MAX(ts) FROM tick_snapshots WHERE ts<=?",
                (ts_utc,)).fetchone()[0] if ts_utc else None
            if snap_ts:
                n_universe = con.execute(
                    "SELECT COUNT(DISTINCT symbol) FROM tick_snapshots "
                    "WHERE ts=?", (snap_ts,)).fetchone()[0]
                lines.append(
                    f"- 采集宇宙：{int(n_universe)} 个 USDT 线性永续"
                    f"（快照 {snap_ts}，UTC；全宇宙无白名单）")
        finally:
            con.close()
    except Exception:
        lines.append("- 采集宇宙规模不可用（market.db 缺失或不可读）")
    c_start, c_end = _cycle_bounds_for_window(start_ts, end_ts)
    try:
        con = _ro_connect(Path(db_root) / "analysis.db")
        try:
            runs = con.execute(
                # F1：'error' 是 9:30 硬闸占位行，与 skipped/stale 同为退化轮。
                "SELECT COUNT(*), SUM(CASE WHEN status IN "
                "('skipped','stale','error') "
                "THEN 1 ELSE 0 END) FROM analysis_runs "
                "WHERE cycle_id>=? AND cycle_id<?", (c_start, c_end)).fetchone()
            sig = con.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM analysis_signals "
                "WHERE cycle_id>=? AND cycle_id<?", (c_start, c_end)).fetchone()
            actions = con.execute(
                "SELECT COALESCE(action,'null') a, COUNT(*) n "
                "FROM analysis_signals WHERE cycle_id>=? AND cycle_id<? "
                "GROUP BY a ORDER BY n DESC", (c_start, c_end)).fetchall()
        finally:
            con.close()
        total_runs = int(runs[0] or 0)
        skip_stale = int(runs[1] or 0)
        lines.append(
            f"- 分析轮次：{total_runs} 轮（skip/stale {skip_stale}）；"
            f"信号 {int(sig[0] or 0)} 条 / 覆盖 {int(sig[1] or 0)} 个标的")
        if actions:
            dist = " ".join(f"{row['a']}={row['n']}" for row in actions[:6])
            lines.append(f"- 动作分布：{dist}")
    except Exception:
        lines.append("- 窗口内 analysis 统计不可用（analysis.db 缺失或不可读）")
    try:
        shadow_root = Path(os.environ.get(
            "OKX_QUALITY_REPORT_DIR", _public_project_path('reports', 'quality')))
        shadow_root = shadow_root / "universe-shadow"
        dates = {str(start_ts)[:10], str(end_ts)[:10]}
        n_files = 0
        seen_dirs = 0
        for d in sorted(dates):
            day_dir = shadow_root / d
            if day_dir.is_dir():
                seen_dirs += 1
                n_files += sum(1 for p in day_dir.glob("*.json"))
        if seen_dirs:
            lines.append(
                f"- 全宇宙判断吞吐影子：窗口两日目录共 {n_files} 份快照"
                "（00/08/16 三次自然调度口径；只读影子，不下单）")
        else:
            lines.append("- 全宇宙判断吞吐影子：窗口内无快照目录（未生成或路径不可达）")
    except Exception:
        lines.append("- 全宇宙判断吞吐影子计数不可用")
    lines.append(
        "- 口径：三周期 rank 为相对选择、非校准概率；独立前向门未通过期间"
        "禁止显示任何可信度分值。")
    return "\n".join(lines)


def _data_completeness_block(db_root: Path, start_ts: str, end_ts: str) -> str:
    """数据完善率统计（复盘窗口）：ledger.collection_runs 逐源完成率。

    完善率=(ok+degraded)/应记录运行；达标率=ok/应记录运行。缺失/失败源逐条
    列出（≤8 条），与 ⚠️ 异常段互补；分母只计窗口内实际记账运行，不虚构计划槽。
    """
    migration = coverage_migration_facts(end_ts)
    activation = migration["activation_cst"]
    if migration["activated"]:
        policy_line = (
            f"- 规格线：四族审计闸门自预注册激活边界 {activation} 起，只对"
            "新样本按 ≥95% 判定；边界前历史仍按 ≥99% 判定，不重算、不重判。")
    else:
        policy_line = (
            f"- 规格线：本报告时点早于预注册激活边界 {activation}；四族仍按"
            " ≥99% 判定，不提前套用 ≥95%。")
    scope_line = (
        "- 口径：本段仅为复盘窗口运行完善率；源健康、市场字段/特征、持仓倾向、"
        "Push/送达/日报/周期报告四族由各预注册审计前向累计，按 ≥99% 的达成率"
        "保留为诊断列。audit_multitimeframe_coverage、audit_asset_class_coverage、"
        "audit_contract_statistics_coverage 仍为 ≥99%，不属本次四族迁移。")
    c_start, c_end = _cycle_bounds_for_window(start_ts, end_ts)
    try:
        con = _ro_connect(Path(db_root) / "ledger.db")
        try:
            rows = con.execute(
                "SELECT source, status, COUNT(*) n FROM collection_runs "
                "WHERE cycle_id>=? AND cycle_id<? GROUP BY source, status",
                (c_start, c_end)).fetchall()
        finally:
            con.close()
    except Exception:
        return ("数据完善率不可用（ledger.db 缺失或不可读）；"
                "采集账本是完善率唯一权威，缺账本即无法声明完善率。\n"
                f"{policy_line}\n{scope_line}")
    if not rows:
        return ("窗口内无采集账本行（停机或窗口异常）；不虚构完善率。\n"
                f"{policy_line}\n{scope_line}")
    per: dict[str, dict[str, int]] = {}
    for r in rows:
        d = per.setdefault(str(r["source"]), {"ok": 0, "degraded": 0, "other": 0})
        status = str(r["status"] or "").lower()
        if status == "ok":
            d["ok"] += int(r["n"])
        elif status == "degraded":
            d["degraded"] += int(r["n"])
        else:
            d["other"] += int(r["n"])
    total = sum(sum(d.values()) for d in per.values())
    good = sum(d["ok"] + d["degraded"] for d in per.values())
    ok_only = sum(d["ok"] for d in per.values())
    lines = [
        f"- 总体：完善率 {good}/{total}={good / total * 100:.1f}%"
        f"（ok+degraded）；纯 ok 达标率 {ok_only / total * 100:.1f}%"
        "（分母=窗口内账本记录的采集运行）",
    ]
    offenders = []
    for source, d in sorted(per.items()):
        t = sum(d.values())
        bad = d["other"]
        if bad:
            offenders.append((bad / t, source, bad, t))
    offenders.sort(reverse=True)
    if offenders:
        for _rate, source, bad, t in offenders[:8]:
            lines.append(f"- ⚠️ {source}: 失败/超时 {bad}/{t} 次")
        if len(offenders) > 8:
            lines.append(f"- …另有 {len(offenders) - 8} 个源存在失败（详见账本）")
    else:
        lines.append("- 窗口内无失败/超时源")
    lines.extend((policy_line, scope_line))
    return "\n".join(lines)


def _snapshot_positions_summary(db_path, profile: str, as_of_ts: str | None = None,
                                max_age_min: int = 30):
    """读取报告时点之前最近一批 OKX API position_snapshots，精确按批次、不 GROUP BY。

    有 __FLAT__ 哨兵返回“空仓”；无批次或批次距报告时点过旧返回 None，防把缺数据写成空仓。
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            if as_of_ts:
                batch = con.execute(
                    "SELECT ts FROM position_snapshots WHERE profile=? AND datetime(ts)<=datetime(?) "
                    "ORDER BY datetime(ts) DESC,rowid DESC LIMIT 1", (profile, as_of_ts)).fetchone()
            else:
                batch = con.execute(
                    "SELECT ts FROM position_snapshots WHERE profile=? "
                    "ORDER BY ts DESC,rowid DESC LIMIT 1",
                    (profile,)).fetchone()
            if not batch:
                return None
            ref = datetime.strptime(as_of_ts or now_cst(), TS_FMT).replace(tzinfo=CST)
            batch_dt = datetime.strptime(str(batch[0]), TS_FMT).replace(tzinfo=CST)
            age_min = (ref - batch_dt).total_seconds() / 60.0
            if age_min < -5 or age_min > max_age_min:
                return None
            rows = con.execute(
                "SELECT symbol,side,sz,avgPx,lev,upl FROM position_snapshots "
                "WHERE profile=? AND ts=? ORDER BY rowid", (profile, batch[0])).fetchall()
        finally:
            con.close()
        if not rows:
            return None
        real = [r for r in rows if str(r[0] or "").strip() != "__FLAT__"]
        if not real:
            return "空仓"
        lines = []
        for symbol, side, sz, avg_px, lev, upl in real:
            side_cn = {"long": "多", "short": "空"}.get(str(side or "").lower(), str(side or "-"))
            line = f"- {symbol} {side_cn} {_fmt_num(sz)}张 @{_fmt_num(avg_px)} {_fmt_num(lev)}x"
            if upl is not None:
                line += f" | 浮盈 {float(upl):+.2f}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return None


def fail(msg: str, code: int = 2):
    print(f"[daily_report_writer][FAIL] {msg}", file=sys.stderr)
    sys.exit(code)


def read_stdin_text() -> str:
    if hasattr(sys.stdin, "buffer"):
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def _anomaly_items(value) -> list[str]:
    items: list[str] = []
    for line in str(value or "").splitlines():
        text = line.strip()
        if not text or text in ("无", "- 无"):
            continue
        item = text if text.startswith("-") else f"- {text}"
        if item not in items:
            items.append(item)
    return items


def _append_anomaly(payload: dict, text: str) -> None:
    items = _anomaly_items(payload.get("anomalies"))
    item = text.strip()
    item = item if item.startswith("-") else f"- {item}"
    if item not in items:
        items.append(item)
    payload["anomalies"] = "\n".join(items) if items else "无"


def _raw_object(value) -> dict:
    """Keep reviewer raw facts while adding deterministic report audit data."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {"reviewer_raw_text": value}
        if isinstance(decoded, dict):
            return decoded
        return {"reviewer_raw_value": decoded}
    return {}


def _initial_daily_revision(existing: object = None) -> dict:
    """Return a machine-readable, non-sending revision state for a new report."""
    if isinstance(existing, dict) and existing:
        revision = dict(existing)
        revision.setdefault("schema_version", 1)
        revision.setdefault("number", 1)
        revision.setdefault("kind", "initial")
        revision.setdefault("corrected", False)
        revision.setdefault("resend_review_required", False)
        revision.setdefault("resend_status", "not_requested")
        revision["auto_resend"] = False
        return revision
    return {
        "schema_version": 1,
        "number": 1,
        "kind": "initial",
        "corrected": False,
        "generated_at": now_cst(),
        "resend_review_required": False,
        "resend_status": "not_requested",
        "auto_resend": False,
    }


def _mark_daily_corrected(
    payload: dict, previous_raw_values: list[object]
) -> dict:
    """Advance the daily artifact revision without sending or scheduling a push."""
    previous_number = 0
    for value in previous_raw_values:
        raw = _raw_object(value)
        audit = raw.get("report_audit")
        if not isinstance(audit, dict):
            continue
        revision = audit.get("revision")
        if not isinstance(revision, dict):
            continue
        try:
            previous_number = max(previous_number, int(revision.get("number") or 0))
        except (TypeError, ValueError):
            continue

    raw = _raw_object(payload.get("raw"))
    audit = raw.get("report_audit")
    if not isinstance(audit, dict):
        audit = {}
        raw["report_audit"] = audit
    revision_number = max(previous_number, 1) + 1
    audit["revision"] = {
        "schema_version": 1,
        "number": revision_number,
        "artifact_version": (
            f"daily:{str(payload.get('ts') or '')[:10]}:r{revision_number}"
        ),
        "kind": "corrected",
        "corrected": True,
        "corrected_at": now_cst(),
        "resend_review_required": True,
        "resend_status": "review_required",
        "auto_resend": False,
    }
    payload["report_revision"] = revision_number
    payload["report_revision_kind"] = "corrected"
    payload["resend_review_required"] = True
    payload["raw"] = json.dumps(raw, ensure_ascii=False)
    return payload


REVISION_LINE_RE = re.compile(
    r"(?m)^>\s*report_revision:\s*(\d+)\s*\|\s*"
    r"revision_kind:\s*([a-z_]+)\s*\|\s*"
    r"resend_review_required:\s*(true|false)\s*\|\s*"
    r"auto_resend:\s*(true|false)\s*$",
    re.IGNORECASE,
)
REQUIRED_REVISION_FIELDS = frozenset({
    "number",
    "kind",
    "corrected",
    "resend_review_required",
    "resend_status",
    "auto_resend",
})


def _revision_line(revision: dict) -> str:
    return (
        f"> report_revision: {int(revision['number'])} | "
        f"revision_kind: {revision['kind']} | "
        "resend_review_required: "
        f"{str(bool(revision['resend_review_required'])).lower()} | "
        "auto_resend: false"
    )


def _patch_revision_line(content: str, revision: dict) -> tuple[str, bool]:
    """Insert only the machine revision line; never re-render report facts."""
    expected = {
        "number": int(revision["number"]),
        "kind": str(revision["kind"]).lower(),
        "resend_review_required": bool(
            revision["resend_review_required"]),
        "auto_resend": False,
    }
    match = REVISION_LINE_RE.search(content)
    if match:
        actual = {
            "number": int(match.group(1)),
            "kind": match.group(2).lower(),
            "resend_review_required": match.group(3).lower() == "true",
            "auto_resend": match.group(4).lower() == "true",
        }
        if actual != expected:
            raise RuntimeError(
                "日报已有 revision 行但与数据库修订状态不一致；"
                "拒绝由 metadata backfill 覆盖")
        return content, False

    report_ts = re.search(
        r"(?m)^>\s*ts:\s*(\d{4}-\d{2}-\d{2} "
        r"\d{2}:\d{2}:\d{2})\b",
        content,
    )
    if not report_ts:
        raise RuntimeError("日报缺少规范 ts 行，拒绝 revision backfill")

    lines = content.splitlines(keepends=True)
    insert_after = None
    for index, line in enumerate(lines):
        if line.startswith("> **报告状态："):
            insert_after = index
            break
    if insert_after is None:
        for index, line in enumerate(lines):
            if re.match(r"^>\s*ts:", line):
                insert_after = index
                break
    if insert_after is None:
        raise RuntimeError("日报缺少可定位的元数据区，拒绝 revision backfill")

    newline = "\r\n" if "\r\n" in content else "\n"
    if not lines[insert_after].endswith(("\n", "\r")):
        lines[insert_after] += newline
    lines.insert(insert_after + 1, _revision_line(revision) + newline)
    return "".join(lines), True


def plan_daily_revision_backfill(
    con: sqlite3.Connection,
    report_ts: str,
    report_path: Path,
) -> dict:
    """Plan a metadata-only repair for one existing daily report row.

    2026-08-06 demo 全量下线：历史 ts 可能是 live+demo 两行（下线前生成）或只有
    live 一行。刻意放宽为「live 行必须在，多余 profile 忽略」——写死两行会让所有
    历史日报的 revision 修复当场失效。"""
    canonical_ts = trade_report_stats.fmt_ts(report_ts)
    rows = con.execute(
        "SELECT rowid,ts,profile,trade_day_num,raw "
        "FROM daily_reports WHERE ts=? ORDER BY profile",
        (canonical_ts,),
    ).fetchall()
    profiles = [str(row[2]) for row in rows]
    if "live" not in profiles:
        raise RuntimeError(
            "revision backfill 要求 report_ts 至少有 live 行")
    if not report_path.exists():
        raise FileNotFoundError(f"日报 Markdown 不存在：{report_path}")

    existing_revisions = []
    row_state = []
    for row in rows:
        raw = _raw_object(row[4])
        audit = raw.get("report_audit")
        if not isinstance(audit, dict):
            raise RuntimeError(
                f"{row[2]} 缺少 report_audit；metadata backfill 不重算事实")
        if audit.get("version") != 1 or audit.get("period_kind") != "daily":
            raise RuntimeError(
                f"{row[2]} report_audit 版本/周期无效；拒绝扩大修复范围")
        revision = audit.get("revision")
        if revision is not None:
            if not isinstance(revision, dict):
                raise RuntimeError(f"{row[2]} revision 不是对象")
            if not REQUIRED_REVISION_FIELDS.issubset(revision):
                raise RuntimeError(f"{row[2]} revision 字段不完整")
            if revision.get("auto_resend") is not False:
                raise RuntimeError(f"{row[2]} auto_resend 必须为 false")
            existing_revisions.append(dict(revision))
        row_state.append({
            "rowid": int(row[0]),
            "ts": row[1],
            "profile": row[2],
            "trade_day_num": row[3],
            "old_raw": row[4],
            "raw_object": raw,
            "has_revision": isinstance(revision, dict),
        })

    if existing_revisions:
        revision = existing_revisions[0]
        if any(item != revision for item in existing_revisions[1:]):
            raise RuntimeError("已有 revision 不一致，拒绝自动选择")
    else:
        revision = _initial_daily_revision()
        revision.update({
            "artifact_version": f"daily:{canonical_ts[:10]}:r1",
            "metadata_backfilled_at": now_cst(),
            "metadata_backfill_only": True,
            "auto_resend": False,
        })

    updates = []
    for item in row_state:
        if item["has_revision"]:
            continue
        raw = item["raw_object"]
        raw["report_audit"]["revision"] = dict(revision)
        updates.append({
            "rowid": item["rowid"],
            "profile": item["profile"],
            "old_raw": item["old_raw"],
            "new_raw": json.dumps(raw, ensure_ascii=False),
        })

    original_bytes = report_path.read_bytes()
    content = original_bytes.decode("utf-8")
    embedded_ts = re.search(
        r"(?m)^>\s*ts:\s*(\d{4}-\d{2}-\d{2} "
        r"\d{2}:\d{2}:\d{2})\b",
        content,
    )
    if not embedded_ts or embedded_ts.group(1) != canonical_ts:
        raise RuntimeError("日报 Markdown ts 与 --report-ts 不一致")
    patched_content, markdown_change = _patch_revision_line(
        content, revision)
    return {
        "report_ts": canonical_ts,
        "report_path": str(report_path),
        "revision": revision,
        "row_updates": updates,
        "markdown_change": markdown_change,
        "_original_markdown_sha256": hashlib.sha256(
            original_bytes).hexdigest(),
        "_patched_content": patched_content,
    }


def apply_daily_revision_backfill_db(
    con: sqlite3.Connection, plan: dict
) -> None:
    """Apply only ``daily_reports.raw`` with optimistic read-before-write."""
    for update in plan["row_updates"]:
        cursor = con.execute(
            "UPDATE daily_reports SET raw=? "
            "WHERE rowid=? AND raw IS ?",
            (update["new_raw"], update["rowid"], update["old_raw"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"revision backfill 并发校验失败 rowid={update['rowid']}")
        stored = con.execute(
            "SELECT raw FROM daily_reports WHERE rowid=?",
            (update["rowid"],),
        ).fetchone()
        if not stored or stored[0] != update["new_raw"]:
            raise RuntimeError(
                f"revision backfill 回读失败 rowid={update['rowid']}")


def apply_daily_revision_backfill_markdown(plan: dict) -> None:
    if plan["markdown_change"]:
        path = Path(plan["report_path"])
        current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if current_hash != plan["_original_markdown_sha256"]:
            raise RuntimeError(
                "revision backfill Markdown 并发校验失败；拒绝覆盖新内容")
        _atomic_write_text(path, plan["_patched_content"])


def public_daily_revision_backfill_plan(
    plan: dict, applied: bool, backup: dict | None = None,
    idempotent_verified: bool = False,
) -> dict:
    return {
        "backfill_daily_revision": True,
        "dry_run": not applied,
        "applied": applied,
        "report_ts": plan["report_ts"],
        "report_path": plan["report_path"],
        "profiles": sorted(
            update["profile"] for update in plan["row_updates"]),
        "database_rows_changed": (
            len(plan["row_updates"]) if applied else 0),
        "database_rows_planned": len(plan["row_updates"]),
        "database_columns": ["raw"],
        "markdown_change": plan["markdown_change"],
        "revision": plan["revision"],
        "facts_recomputed": False,
        "auto_send": False,
        "auto_resend": False,
        "backup": backup,
        "backup_required_for_apply": True,
        "idempotent_verified": idempotent_verified,
    }


def create_daily_revision_backup(
    db_path: Path,
    report_path: Path,
    backup_dir: Path,
    report_ts: str,
) -> dict:
    """Create and verify a consistent DB backup plus the original Markdown."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S-%f")
    date_key = report_ts[:10].replace("-", "")
    db_backup = backup_dir / (
        f"{db_path.stem}.daily-revision-{date_key}-before-{stamp}.db")
    report_backup = backup_dir / (
        f"{report_path.stem}.daily-revision-before-{stamp}.md")
    if db_backup.exists() or report_backup.exists():
        raise FileExistsError("revision backfill backup target exists")

    source = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    destination = sqlite3.connect(db_backup)
    try:
        source.execute("PRAGMA busy_timeout=5000")
        source.backup(destination)
        check = destination.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError("revision backfill DB backup quick_check failed")
    finally:
        destination.close()
        source.close()
    shutil.copy2(report_path, report_backup)
    return {
        "database": str(db_backup),
        "database_sha256": hashlib.sha256(
            db_backup.read_bytes()).hexdigest(),
        "markdown": str(report_backup),
        "markdown_sha256": hashlib.sha256(
            report_backup.read_bytes()).hexdigest(),
    }


def _load_frozen_exit_quality(
    report_start_ts: str,
    report_end_ts: str,
    *,
    allow_degraded_backfill: bool = False,
) -> dict | None:
    """Load the ready-bound artifact; never recompute exit quality here.

    返回 ``None`` = 本日退出质量段**如实不可用**（渲染侧据此打「不以 0 冒充
    无回吐/无错失」的留空段）。两条路径会走到 None：

    1. manifest 自己在 ``degraded_critical_steps`` 里记了 exit_quality —— 这是
       2026-08-20 起 ``daily_maintenance.PROVISIONAL_ON_FAILURE_STEPS`` 的配套：
       该步失败改判 provisional 而非 blocked，writer 必须跟着放行，否则 manifest
       说「可以发」而 writer 拒写，两边对不上，等于没改。
    2. ``allow_degraded_backfill`` 显式打开，且 manifest 里**唯一**未被接受的
       关键步就是 exit_quality —— 给边界之前的历史日补录用。

    原有的「不以 unavailable 软降级」仍然成立：它禁的是**无凭证的静默回退**，
    而这两条都要求 manifest 里有显式记录；任何非 exit_quality 的关键步失败一律
    照旧拒写。
    """
    business_date = str(report_end_ts)[:10]
    manifest_path = REVIEWER_READY_DIR / (
        f"reviewer_ready_{business_date}.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("reviewer ready manifest root invalid")
    if manifest.get("business_date") != business_date:
        raise ValueError("reviewer ready manifest identity/state invalid")
    steps_map = manifest.get("steps") or {}
    step = steps_map.get("exit_quality")
    if EXIT_QUALITY_DEGRADED_STEP in (
            manifest.get("degraded_critical_steps") or []):
        return None
    if allow_degraded_backfill:
        unaccepted = sorted(
            name for name, item in steps_map.items()
            if isinstance(item, dict) and item.get("accepted") is not True
        )
        if unaccepted and unaccepted != [EXIT_QUALITY_DEGRADED_STEP]:
            raise ValueError(
                "degraded backfill refused: non-degradable critical steps "
                + ",".join(unaccepted))
        if unaccepted:
            return None
    if (
        manifest.get("state") != "ready"
        or manifest.get("ready") is not True
    ):
        raise ValueError("reviewer ready manifest identity/state invalid")
    artifact = step.get("artifact") if isinstance(step, dict) else None
    if not isinstance(step, dict) or step.get("accepted") is not True:
        raise ValueError("reviewer ready exit_quality step not accepted")
    if not isinstance(artifact, dict):
        raise ValueError("reviewer ready exit_quality artifact missing")
    artifact_path = Path(str(artifact.get("path") or ""))
    expected_sha = str(artifact.get("sha256") or "").lower()
    expected_size = artifact.get("size_bytes")
    if (
        not str(artifact_path) or not expected_sha
        or not isinstance(expected_size, int) or expected_size < 0
    ):
        raise ValueError("exit_quality artifact path/hash/size missing")
    raw = artifact_path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        raise ValueError("exit_quality artifact hash differs from ready")
    if len(raw) != expected_size:
        raise ValueError("exit_quality artifact size differs from ready")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("exit_quality artifact root invalid")
    candidate_start = (
        datetime.strptime(report_start_ts, TS_FMT) - timedelta(hours=4)
    ).strftime(TS_FMT)
    candidate_end = (
        datetime.strptime(report_end_ts, TS_FMT) - timedelta(hours=4)
    ).strftime(TS_FMT)
    peak_effective_start = max(
        candidate_start, EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS)
    expected_peak_status = (
        "PENDING" if peak_effective_start >= candidate_end else "COMPLETE")
    try:
        generated_at = datetime.strptime(
            str(payload.get("generated_at") or "")[:19], TS_FMT)
    except ValueError:
        generated_at = None
    report_end_dt = datetime.strptime(report_end_ts, TS_FMT)
    peak_value = payload.get("peak_giveback")
    margin_value = payload.get("margin_return_review")
    missed_value = payload.get("missed_take_profit")
    peak = peak_value if isinstance(peak_value, dict) else {}
    margin = margin_value if isinstance(margin_value, dict) else {}
    missed = missed_value if isinstance(missed_value, dict) else {}
    missed_classes_value = missed.get("classification_counts")
    missed_classes = (
        missed_classes_value if isinstance(missed_classes_value, dict) else {})
    dispositions = margin.get("disposition_counts") or {}
    checks = (
        payload.get("schema_version") == EXIT_QUALITY_SCHEMA_VERSION,
        payload.get("method_version") == EXIT_QUALITY_METHOD_VERSION,
        payload.get("business_date") == business_date,
        generated_at is not None and generated_at >= report_end_dt,
        payload.get("report_activation_cst") == EXIT_QUALITY_ACTIVATION_TS,
        payload.get("margin_fact_activation_cycle")
        == EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE,
        payload.get("counterfactual_activation_cst")
        == EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS,
        (payload.get("report_window") or {}).get("start_ts")
        == report_start_ts,
        (payload.get("report_window") or {}).get("end_ts") == report_end_ts,
        (payload.get("report_window") or {}).get("end_exclusive") is True,
        (payload.get("candidate_window") or {}).get("start_ts")
        == candidate_start,
        (payload.get("candidate_window") or {}).get("end_ts")
        == candidate_end,
        (payload.get("candidate_window") or {}).get("end_exclusive") is True,
        missed.get("method_version") == EXIT_QUALITY_MISSED_TP_METHOD,
        missed.get("evidence_method_version")
        == EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD,
        missed.get("counterfactual_activation_cst")
        == EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS,
        missed.get("upstream_status") == "READY",
        peak.get("method_version")
        in EXIT_QUALITY_PEAK_METHOD_VERSIONS_ACCEPTED,
        peak.get("fact_activation_cst")
        == EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS,
        peak.get("status") == expected_peak_status,
        (
            (peak.get("effective_window") or {}).get("start_ts"),
            (peak.get("effective_window") or {}).get("end_ts"),
            (peak.get("effective_window") or {}).get("end_exclusive"),
        ) == (peak_effective_start, candidate_end, True),
        all(isinstance(peak.get(key), int) and peak.get(key) >= 0 for key in (
            "candidate_closed_rows", "pre_activation_excluded_rows")),
        all(isinstance(peak.get(key), int) and peak.get(key) >= 0 for key in (
            "source_closed_rows", "excluded_non_live_rows",
            "excluded_non_open_rows", "closed_rows")),
        all(isinstance(margin.get(key), int) and margin.get(key) >= 0 for key in (
            "source_candidate_cycle_rows", "excluded_non_live_cycle_rows",
            "excluded_non_open_position_rows", "total_position_cycles")),
        "requested_unconfirmed" in dispositions,
        all(isinstance(missed.get(key), int) and missed.get(key) >= 0 for key in (
            "source_closed_rows", "excluded_profile_count",
            "excluded_fallback_count")),
        all(isinstance(missed_classes.get(key), int)
            and missed_classes.get(key) >= 0 for key in (
                "missed_take_profit", "excluded_profile",
                "excluded_fallback")),
        isinstance(missed.get("pool_size"), int)
        and missed.get("pool_size") >= 0
        and missed.get("pool_size")
        == missed_classes.get("missed_take_profit"),
        payload.get("safety") == {
            "production_database_writes": 0,
            "cycles_replayed": 0,
            "window_extended": False,
            "orders_placed": 0,
        },
    )
    if not all(checks):
        raise ValueError("exit_quality frozen artifact contract differs")
    return {
        **payload,
        "frozen_artifact": {
            "path": str(artifact_path),
            "sha256": actual_sha,
            "size_bytes": len(raw),
            "ready_manifest": str(manifest_path),
        },
    }


def _exit_quality_block(payload: dict) -> str:
    """退出质量段：只渲染确定性统计，未知一律写「未知」不写 0。"""
    block = payload.get("exit_quality")
    if not isinstance(block, dict):
        return (
            "## 🚪 退出质量\n\n"
            "退出质量统计不可用（如实留空，不以 0 冒充无回吐/无错失）。\n"
        )
    window = block.get("candidate_window") or {}
    giveback = block.get("peak_giveback") or {}
    review = block.get("margin_return_review") or {}
    missed = block.get("missed_take_profit") or {}
    buckets = giveback.get("profitable_peak_giveback_buckets_r") or {}
    bucket_text = "、".join(
        f"{label} {count}笔" for label, count in buckets.items()
    ) or "无样本"
    rate = review.get("explicit_review_rate")
    rate_text = "无被标记仓位" if rate is None else f"{float(rate):.1%}"
    dispositions = review.get("disposition_counts") or {}
    disposition_text = "、".join(
        f"{name} {count}次" for name, count in dispositions.items()
    ) or "无"
    action_layers = review.get("action_layer_counts") or {}
    # 冻结 JSON 为稳定 SHA 使用 sort_keys=True；展示不能继承序列化后的
    # 字母序，必须按四层语义和独立 validator 的动作契约确定性重建。
    layer_order = ("requested", "succeeded", "fills", "failed")
    action_order = ("close", "reduce", "adjust", "add", "open")
    layer_text = "；".join(
        f"{layer}=" + ",".join(
            f"{action}:{(action_layers.get(layer) or {}).get(action, 0)}"
            for action in action_order)
        for layer in layer_order
    ) or "无"
    lines = [
        "## 🚪 退出质量",
        "",
        f"> 后验窗固化: 候选窗口 [{window.get('start_ts')}, "
        f"{window.get('end_ts')})，UTC+8；报告窗整体前移 "
        f"{block.get('outcome_horizon_hours')} 小时，只统计结果已成熟的平仓；"
        f"method={block.get('method_version')}；Reviewer 不重跑周期、不扩窗。",
        "",
        f"- 峰值回吐: {giveback.get('status')}（method="
        f"{giveback.get('method_version')}；激活="
        f"{giveback.get('fact_activation_cst')}）",
        f"- 峰值有效窗 [{(giveback.get('effective_window') or {}).get('start_ts')}, "
        f"{(giveback.get('effective_window') or {}).get('end_ts')})；"
        f"候选open平仓 {giveback.get('candidate_closed_rows')} 笔、激活前排除 "
        f"{giveback.get('pre_activation_excluded_rows')} 笔",
        f"- 已成熟平仓: {giveback.get('closed_rows')} 笔（路径可测 "
        f"{giveback.get('measured_rows')} 笔，覆盖不足按未知计 "
        f"{giveback.get('unknown_path_rows')} 笔）；源关闭记录 "
        f"{giveback.get('source_closed_rows')} 笔、排除非live "
        f"{giveback.get('excluded_non_live_rows')} 笔、排除非open "
        f"{giveback.get('excluded_non_open_rows')} 笔",
        f"- 浮盈峰值回吐分布（峰值≥"
        f"{giveback.get('profitable_peak_threshold_r')}R 的 "
        f"{giveback.get('profitable_peak_rows')} 笔）: {bucket_text}；"
        f"中位回吐 {giveback.get('profitable_peak_giveback_median_r')}R，"
        f"峰值留存中位 {giveback.get('peak_retention_median')}",
        f"- 曾达1R: {giveback.get('reached_1r')} 笔，平仓仍≥1R: "
        f"{giveback.get('closed_at_or_above_1r')} 笔",
        f"- 持仓期利润回吐案例: {giveback.get('profit_giveback_case_count')} 笔"
        "（曾达1R但平仓低于1R；只作回吐证据，不冒充平仓后反事实）",
        f"- 错失止盈池: {missed.get('status')}（method="
        f"{missed.get('method_version')}；evidence="
        f"{missed.get('evidence_method_version')}；仅 fixed_tp；需平仓后"
        f"{missed.get('required_15m_bars')}根15m；候选"
        f"{missed.get('candidate_exits')}、已评估"
        f"{missed.get('evaluated_exits')}、未知"
        f"{missed.get('unknown_exits')}、覆盖率"
        f"{missed.get('coverage_rate')}、pool="
        f"{missed.get('pool_size')}（分类计数="
        f"{(missed.get('classification_counts') or {}).get('missed_take_profit')}）；"
        f"源行={missed.get('source_closed_rows')}、排除非live="
        f"{missed.get('excluded_profile_count')}、fallback="
        f"{missed.get('excluded_fallback_count')}；未知原因="
        f"{missed.get('unknown_reason_counts')}；激活前排除="
        f"{missed.get('pre_activation_excluded_exits')}、非固定TP不适用="
        f"{missed.get('not_applicable_exits')}）",
        f"- ≥50%事实有效窗: [{(review.get('effective_cycle_window') or {}).get('start_cycle')}, "
        f"{(review.get('effective_cycle_window') or {}).get('end_cycle')})；"
        f"激活前排除 {review.get('pre_activation_excluded_cycle_rows')} 个cycle；"
        f"源cycle {review.get('source_candidate_cycle_rows')}、排除非live "
        f"{review.get('excluded_non_live_cycle_rows')}、排除非open仓位 "
        f"{review.get('excluded_non_open_position_rows')}；"
        f"字段可观测 {review.get('fact_observed_position_cycles')}/"
        f"{review.get('total_position_cycles')}，未知 "
        f"{review.get('unknown_fact_position_cycles')}（未知不进复核分母）",
        f"- 保证金收益率≥{float(review.get('threshold_fraction') or 0.5):.0%} "
        f"被标记仓位-周期: {review.get('flagged_position_cycles')} 次，"
        f"决策卡显式复核 {review.get('explicitly_reviewed')} 次"
        f"（复核率 {rate_text}）；处置分布: {disposition_text}",
        f"- 动作证据分层（请求/成功回执/实际成交/失败尝试不互相冒充）: "
        f"{layer_text}",
    ]
    for item in (giveback.get("profit_giveback_cases") or [])[:10]:
        lines.append(
            f"  - {item.get('symbol')} {item.get('side')} 峰值 "
            f"{item.get('peak_r')}R → 平仓 {item.get('realized_r_net')}R"
            f"（回吐 {item.get('giveback_r')}R，出口 "
            f"{item.get('exit_category')}）"
        )
    lines.append("")
    return "\n".join(lines)


def _report_state(payload: dict) -> dict:
    """Classify a report without blocking publication on live reconcile drift."""
    raw_status = str(payload.get("live_reconcile_status") or "").strip().lower()
    try:
        issue_count = int(payload.get("live_reconcile_issue_count") or 0)
    except (TypeError, ValueError):
        issue_count = 0
    summary = str(payload.get("summary") or "")

    clean_mark = bool(re.search(
        r"Live对账.*(?:已清零|一致|无\s+GHOST/OVER_CLOSED/UNRECORDED|:\s*OK)",
        summary,
        re.IGNORECASE | re.DOTALL,
    ))
    pending_mark = bool(re.search(
        r"Live对账.*(?:未消|待对账|GHOST|OVER_CLOSED|UNRECORDED|LEFTOVER)",
        summary,
        re.IGNORECASE | re.DOTALL,
    ))
    if raw_status in {"clean", "ok", "cleared", "final"}:
        status, reason = "final", "live 对账已清零"
    elif issue_count > 0 or raw_status in {
            "pending", "dirty", "unresolved", "provisional"}:
        status = "provisional"
        reason = (
            f"live 对账待清零（{issue_count} 项）"
            if issue_count > 0 else "live 对账待清零"
        )
    elif raw_status in {"unavailable", "unknown", "error"}:
        status, reason = "provisional", "live 对账状态不可用"
    elif clean_mark:
        status, reason = "final", "live 对账已清零（由 summary 识别）"
    elif pending_mark:
        status, reason = "provisional", "live 对账待清零（由 summary 识别）"
    else:
        status, reason = "provisional", "live 对账状态未声明"
    evidence = payload.get("missed_opportunity_evidence_contract")
    evidence_status = None
    release_allowed = True
    if isinstance(evidence, dict):
        evidence_status = str(evidence.get("status") or "ERROR")
        release_allowed = bool(
            evidence_status == "COMPLETE"
            and evidence.get("release_eligible") is True
        )
        if not release_allowed:
            status = "evidence_draft"
            reason = f"错失机会证据 {evidence_status}"
    return {
        "status": status,
        "reason": reason,
        "live_reconcile_status": raw_status or "unspecified",
        "live_reconcile_issue_count": issue_count,
        "missed_opportunity_evidence_status": evidence_status,
        "release_allowed": release_allowed,
    }


def _report_label(report_state: dict) -> str:
    if report_state.get("status") == "final":
        return f"最终报告｜{report_state.get('reason')}"
    if report_state.get("status") == "evidence_draft":
        return f"证据草稿｜{report_state.get('reason')}｜禁止外发"
    return f"临时报告｜{report_state.get('reason')}"


def _compact_trade_metrics(stats: dict) -> dict:
    rejects = stats["risk_rejected_open_attempts"]
    return {
        "source": stats["source"],
        "period_start_ts": stats["period_start_ts"],
        "period_end_ts": stats["period_end_ts"],
        "period_end_exclusive": stats["period_end_exclusive"],
        "open_count": stats["open_count"],
        "close_count": stats["close_count"],
        # P0-1：realized_pnl 冻结为仅 close；total_realized_pnl 才是头条口径。
        "realized_pnl": stats["realized_pnl"],
        "close_realized_pnl": stats.get("close_realized_pnl"),
        "reduce_count": stats.get("reduce_count"),
        "reduce_realized_pnl": stats.get("reduce_realized_pnl"),
        "total_realized_pnl": stats.get("total_realized_pnl"),
        "realizing_actions": stats.get("realizing_actions"),
        "excluded_non_fill_rows": stats.get("excluded_non_fill_rows"),
        "win_rate_pct": stats["win_rate_pct"],
        "best_trade": stats["best_trade"],
        "worst_trade": stats["worst_trade"],
        "best_realizing_trade": stats.get("best_realizing_trade"),
        "worst_realizing_trade": stats.get("worst_realizing_trade"),
        "close_side_breakdown": stats.get("close_side_breakdown"),
        "closed_position_avg_hold_hours": stats.get(
            "closed_position_avg_hold_hours"),
        "closed_position_hold_sample_count": stats.get(
            "closed_position_hold_sample_count"),
        "closed_position_hold_unmatched_count": stats.get(
            "closed_position_hold_unmatched_count"),
        "excluded_rejected_rows": stats["excluded_rejected_rows"],
        "excluded_incomplete_rows": stats["excluded_incomplete_rows"],
        "risk_rejected_open_attempts": {
            "count": rejects["count"],
            "reasons": rejects["reasons"],
            "items": rejects["items"],
        },
    }


def _risk_reject_text(stats: dict) -> str:
    rejected = stats["risk_rejected_open_attempts"]
    if not rejected["count"]:
        return "0 笔"
    symbols = sorted({
        str(item["symbol"]).replace("-USDT-SWAP", "")
        for item in rejected["items"]
    })
    reasons = "、".join(
        f"{reason}×{count}"
        for reason, count in rejected["reasons"].items()
    )
    detail = "/".join(symbols)
    suffix = f"；{reasons}" if reasons else ""
    return f"{rejected['count']} 笔（{detail}{suffix}）"


def _numeric_diff(left, right, tolerance: float = 1e-9) -> bool:
    try:
        return abs(float(left) - float(right)) > tolerance
    except (TypeError, ValueError):
        return True


def _missed_opps_candidate_window(
    start_ts: str,
    end_ts: str,
) -> tuple[str, str]:
    """Return the continuous 24H candidate window with mature 4H outcomes."""
    shift = timedelta(hours=MISSED_OPPORTUNITY_OUTCOME_HOURS)
    start = trade_report_stats.parse_cst(start_ts) - shift
    end = trade_report_stats.parse_cst(end_ts) - shift
    return start.strftime(TS_FMT), end.strftime(TS_FMT)


def _missed_opps_window_count(start_ts: str, end_ts: str) -> int | None:
    """Count the shifted, fully matured outcome window; unknown is not zero."""
    if not LESSONS_DB.exists():
        return None
    candidate_start, candidate_end = _missed_opps_candidate_window(
        start_ts, end_ts)
    try:
        con = sqlite3.connect(f"file:{LESSONS_DB}?mode=ro", uri=True, timeout=5)
        try:
            if not con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='missed_opportunities'").fetchone():
                return None
            # 2026-08-19 F5：断供保护。写方（missed_opps_writer）依赖
            # analysis_signals 的 wait 行，08-15 吞吐契约后 unified 轮不再
            # 落 wait —— 实测 max(ts)=2026-08-14 05:45。写方最后一行早于
            # 窗口右端时，本窗的 0 是「不知道」而不是「真的没有」，返回
            # None 让上游按 unknown 处理（_lint_narrative_against_facts 只
            # 在 missed_count>0 时判违规，None 天然不会把断供渲染成
            # 「无错失机会」的肯定句）。
            source_max = con.execute(
                "SELECT MAX(ts) FROM missed_opportunities "
                "WHERE ts LIKE '202%'").fetchone()[0]
            if source_max is None or str(source_max) < str(candidate_end):
                return None
            return int(con.execute(
                "SELECT COUNT(*) FROM missed_opportunities "
                "WHERE ts LIKE '202%' AND datetime(ts)>=datetime(?) "
                "AND datetime(ts)<datetime(?)",
                (candidate_start, candidate_end)).fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _missed_opportunity_evidence_contract(
    start_ts: str,
    end_ts: str,
) -> dict | None:
    """Build the forward-only shared read contract for one report window."""
    if not thresholds.missed_opportunity_evidence_contract_active(end_ts):
        return None
    contract = trade_report_stats.missed_opportunity_evidence_contract(
        report_start_ts=start_ts,
        report_end_ts=end_ts,
        lessons_db=LESSONS_DB,
        live_trades_db=LIVE_TRADES_DB,
        market_db=MARKET_DB,
        briefing_dir=BRIEFING_LOG_DIR,
        contract_activation_cst=(
            thresholds.MISSED_OPPORTUNITY_EVIDENCE_ACTIVATION_CST),
    )
    if not isinstance(contract, dict):
        raise ValueError("missed opportunity evidence contract must be dict")
    state = str(contract.get("status") or "")
    if state not in MISSED_OPPORTUNITY_EVIDENCE_STATES:
        raise ValueError(
            f"missed opportunity evidence contract status invalid: {state!r}")
    if state == "COMPLETE":
        count = contract.get("count")
        if type(count) is not int or count < 0:
            raise ValueError(
                "COMPLETE missed opportunity evidence requires nonnegative int count")
    elif contract.get("count") is not None:
        raise ValueError(
            f"{state} missed opportunity evidence count must be null")
    return contract


def _missed_opportunity_machine_line(payload: dict) -> str:
    """Stable machine-readable release line for forward report validators."""
    contract = payload.get("missed_opportunity_evidence_contract")
    if not isinstance(contract, dict):
        return ""
    state = str(contract.get("status") or "ERROR")
    count = contract.get("count") if state == "COMPLETE" else None
    count_text = str(count) if type(count) is int else "N/A"
    release = "true" if contract.get("release_eligible") is True else "false"
    window = contract.get("candidate_window") or {}
    start = str(window.get("start") or window.get("start_ts") or "N/A")
    end = str(window.get("end") or window.get("end_ts") or "N/A")
    self_sha = str(contract.get("self_sha256") or "N/A")
    return (
        "> missed_opportunity_evidence_contract: "
        f"status={state} release_eligible={release} count={count_text} "
        f"candidate_window=[{start},{end}) self_sha256={self_sha}\n"
    )


_SIDE_PAIR_RES = (
    # "11空/2多"、"11 空、2 多" 及反序
    (re.compile(r"(\d+)\s*(?:笔)?\s*空\s*[/、]\s*(\d+)\s*(?:笔)?\s*多"),
     ("short", "long")),
    (re.compile(r"(\d+)\s*(?:笔)?\s*多\s*[/、]\s*(\d+)\s*(?:笔)?\s*空"),
     ("long", "short")),
)
_SIDE_LABEL_RES = (
    (re.compile(r"空头(?:方向)?\s*(\d+)\s*笔"), "short"),
    (re.compile(r"多头(?:方向)?\s*(\d+)\s*笔"), "long"),
)
_NO_MISSED_RE = re.compile(r"无错失机会|错失机会\s*[:：]?\s*0\s*(?:条|笔|个)?")
_NO_REJECT_RE = re.compile(r"无风控拒绝|风控拒绝\s*[:：]?\s*0\s*(?:条|笔)?")
_LEGACY_1R_RE = re.compile(r"(?i)\bhit[_ ]?1r\b")


def _lint_narrative_against_facts(payload: dict, stats: dict,
                                  missed_count: int | None, *,
                                  allow_legacy_r_vocabulary: bool = False,
                                  ) -> list[str]:
    """过渡期叙事 lint（Wave0-2）：只拦已实际烧过的三类事实冲突——

    ① 方向计数（周报曾写 11空/2多，账本 10空/3多）；
    ② "无错失机会"断言 vs lessons.db 窗口实数；
    ③ "无风控拒绝"断言 vs execution_intents 实数。
    只校验 summary/lessons 文字段；数字来源恒为确定性 stats。不做泛化数字
    审查（价格、百分比等不在射程），完整 fact_id 绑定属后续 wave。
    """
    text = f"{payload.get('summary') or ''}\n{payload.get('lessons') or ''}"
    if not text.strip():
        return []
    problems: list[str] = []
    sides = stats.get("close_side_breakdown") or {}
    actual = {
        "long": (sides.get("long") or {}).get("close_count"),
        "short": (sides.get("short") or {}).get("close_count"),
    }
    claimed: dict[str, set[int]] = {"long": set(), "short": set()}
    for pattern, (first, second) in _SIDE_PAIR_RES:
        for m in pattern.finditer(text):
            claimed[first].add(int(m.group(1)))
            claimed[second].add(int(m.group(2)))
    for pattern, side in _SIDE_LABEL_RES:
        for m in pattern.finditer(text):
            claimed[side].add(int(m.group(1)))
    for side in ("long", "short"):
        expected = actual.get(side)
        if expected is None:
            continue
        wrong = {n for n in claimed[side] if n != expected}
        if wrong:
            label = "多" if side == "long" else "空"
            problems.append(
                f"文字段声称{label}头 {sorted(wrong)} 笔，"
                f"确定性统计为 {expected} 笔")
    if missed_count is not None and missed_count > 0 and \
            _NO_MISSED_RE.search(text):
        problems.append(
            f"文字段声称无错失机会，lessons.db 本窗口实有 {missed_count} 条")
    rejects = (stats.get("risk_rejected_open_attempts") or {}).get("count")
    if rejects and _NO_REJECT_RE.search(text):
        problems.append(
            f"文字段声称无风控拒绝，本窗口实有 {rejects} 笔")
    # 词汇规则可被历史补渲染豁免（render_monthly_markdown_from_existing
    # 经 payload 标记设置）；①②③事实核对恒不豁免。
    if not allow_legacy_r_vocabulary and _LEGACY_1R_RE.search(text):
        problems.append(
            "文字段含退化 hit_1R/hit1R 旧口径；请改用 "
            "is_gross_profit_close、ever_hit_1r 或 would_hit_1r_fixed2pct 的明确语义"
        )
    return problems


def _prepare_trade_payload(
    payload: dict,
    *,
    start_ts: str,
    end_ts: str,
    end_exclusive: bool,
    include_avg_hold: bool,
    period_kind: str,
) -> dict:
    """Hydrate report metrics from fill/intent ledgers and audit overrides."""
    out = {
        **payload,
        "period_start_ts": trade_report_stats.fmt_ts(start_ts),
        "period_end_ts": trade_report_stats.fmt_ts(end_ts),
        "period_end_exclusive": bool(end_exclusive),
    }
    stats_by_profile = {}
    fees_reconciliation_by_profile = {}   # P0-2：手续费事实来源留痕
    corrections = []
    paths = {"live": LIVE_TRADES_DB}
    for profile in ("live",):
        stats = trade_report_stats.profile_statistics(
            profile,
            paths[profile],
            LEDGER_DB,
            start_ts,
            end_ts,
            end_exclusive=end_exclusive,
            include_avg_hold=include_avg_hold,
        )
        stats_by_profile[profile] = stats
        # P0-1 边界键：daily 用报告 ts（= validator 的 report_ts），
        # weekly/monthly 用期初键（= validate_periodic_report 的 key）。
        if period_kind == "daily":
            _pnl_key = str(
                out.get("ts") or out["period_end_ts"])
            _pnl_from = TOTAL_REALIZED_PNL_REQUIRED_FROM
        else:
            _pnl_key = str(out["period_start_ts"])
            _pnl_from = PERIODIC_TOTAL_REALIZED_PNL_REQUIRED_FROM
        authoritative = {
            "open_count": stats["open_count"],
            "close_count": stats["close_count"],
            # P0-1：边界后头条已实现盈亏 = close + reduce；边界前仍只计 close
            # （历史不反向加责，且与校验侧同源，见文件头常量注释）。
            # realized_pnl（仅 close）语义冻结，另作 close_realized_pnl
            # 保留在审计块里可对照。
            "total_pnl": (stats["total_realized_pnl"]
                          if _pnl_key >= _pnl_from
                          else stats["realized_pnl"]),
            "close_realized_pnl": stats["close_realized_pnl"],
            "reduce_count": stats["reduce_count"],
            "reduce_realized_pnl": stats["reduce_realized_pnl"],
            "best_trade": stats["best_trade"],
            "worst_trade": stats["worst_trade"],
            "best_realizing_trade": stats.get("best_realizing_trade"),
            "worst_realizing_trade": stats.get("worst_realizing_trade"),
            "risk_rejected_open_count":
                stats["risk_rejected_open_attempts"]["count"],
            "risk_rejected_open_summary": _risk_reject_text(stats),
        }
        if include_avg_hold:
            authoritative["avg_hold_hours"] = (
                stats.get("closed_position_avg_hold_hours")
            )
        if period_kind == "weekly":
            authoritative["win_rate"] = stats["win_rate_pct"]

        sides = stats.get("close_side_breakdown") or {}
        authoritative["close_side_breakdown"] = sides
        authoritative["close_long_count"] = (
            sides.get("long") or {}).get("close_count", 0)
        authoritative["close_short_count"] = (
            sides.get("short") or {}).get("close_count", 0)

        # 2026-08-19 P0-2：total_fees 此前纯取 payload（agent 不填即 0）——
        # 85 份日报里 31 份「有平仓、手续费 0」，30 天真实手续费 -46.14 USDT
        # 长期不进报。改为与 open/close/pnl 同级的 writer 权威字段：从
        # account.db.account_bills 同窗 SUM(fee) 确定性求和。符号约定：库里
        # fee 恒为负（成本），日报列历史上存**绝对值**（08-06/08-10/08-16
        # 六份有值日报 100% 匹配 abs()），此处 abs() 保持向后兼容。账单不可用
        # 时保留 payload 值并标 unavailable，不伪造 0。
        _bills = _account_bill_net_for_window(
            DB_PATH, profile, out["period_start_ts"], out["period_end_ts"])
        _fees_abs = abs(float(_bills.get("fees") or 0.0)) if _bills else None
        _payload_fees = out.get(f"{profile}_total_fees", out.get("total_fees"))
        if _fees_abs is None:
            authoritative["total_fees"] = float(_payload_fees or 0.0)
            authoritative["fees_source"] = "unavailable"
        else:
            authoritative["total_fees"] = _fees_abs
            authoritative["fees_source"] = "account_bills.sum_abs_fee"
        authoritative["fees_account_bills_abs"] = _fees_abs
        authoritative["fees_bill_rows"] = (_bills or {}).get("rows")
        fees_reconciliation_by_profile[profile] = {
            "source": authoritative["fees_source"],
            "account_bills_sum_abs_fee": _fees_abs,
            "payload_total_fees": (
                float(_payload_fees) if _payload_fees is not None else None),
            "bill_rows": (_bills or {}).get("rows"),
            "bill_types": ["2", "8"],
            "period_start_ts": out["period_start_ts"],
            "period_end_ts": out["period_end_ts"],
            "period_end_exclusive": True,
            "sign_convention": "库内 fee<=0；本字段存正数（成本绝对值）",
        }

        for key in ("open_count", "close_count", "total_pnl", "total_fees"):
            field = f"{profile}_{key}"
            if field in out and _numeric_diff(
                    out[field], authoritative[key],
                    tolerance=(1e-6 if key in ("total_pnl", "total_fees")
                               else 0)):
                corrections.append(
                    f"{profile}.{key} {out[field]}→{authoritative[key]}")
        for key, value in authoritative.items():
            out[f"{profile}_{key}"] = value

    if corrections:
        _append_anomaly(
            out,
            "成交统计已按有效 fill 自动校正: " + "；".join(corrections),
        )

    missed_evidence = _missed_opportunity_evidence_contract(
        out["period_start_ts"], out["period_end_ts"])
    if missed_evidence is None:
        missed_count = _missed_opps_window_count(
            out["period_start_ts"], out["period_end_ts"])
    else:
        missed_count = (
            missed_evidence.get("count")
            if missed_evidence.get("status") == "COMPLETE" else None
        )
        out["missed_opportunity_evidence_contract"] = missed_evidence
    out["missed_opps_window_count"] = missed_count
    (
        out["missed_opps_candidate_start_ts"],
        out["missed_opps_candidate_end_ts"],
    ) = _missed_opps_candidate_window(
        out["period_start_ts"], out["period_end_ts"])
    out["missed_opps_outcome_horizon_hours"] = (
        MISSED_OPPORTUNITY_OUTCOME_HOURS)

    if period_kind == "daily":
        if out["period_end_ts"] >= EXIT_QUALITY_ACTIVATION_TS:
            # 激活后 fail closed：只消费 ready manifest 绑定的冻结 JSON；
            # writer 不导入 producer、不读源表重算，也不以 unavailable 软降级。
            out["exit_quality"] = _load_frozen_exit_quality(
                out["period_start_ts"], out["period_end_ts"],
                allow_degraded_backfill=bool(
                    payload.get("allow_degraded_backfill")))
        else:
            # 历史报告不反向加责，也不插入一个新的“不可用”段。
            out["exit_quality"] = None

    lint_problems: list[str] = []
    for stats in stats_by_profile.values():
        lint_problems.extend(
            _lint_narrative_against_facts(
                out, stats, missed_count,
                allow_legacy_r_vocabulary=bool(
                    out.get("allow_legacy_r_vocabulary"))))
    if lint_problems:
        fail(
            "报告文字段与确定性事实冲突，拒写（修正 summary/lessons 后重交）: "
            + "；".join(lint_problems)
        )

    report_state = _report_state(out)
    out["report_status"] = report_state["status"]
    out["report_status_reason"] = report_state["reason"]
    raw = _raw_object(out.get("raw"))
    raw["report_audit"] = {
        "version": 1,
        "period_kind": period_kind,
        "report_state": report_state,
        "trade_metrics": {
            profile: _compact_trade_metrics(stats)
            for profile, stats in stats_by_profile.items()
        },
        "exit_quality": out.get("exit_quality"),
        # P0-2：手续费不再是「没人填就 0」，来源与账单行数一并自证。
        "fees_reconciliation": fees_reconciliation_by_profile,
        # P0-3：周窗缺哪几天日报，确定性落库（周报专用；日报恒为 []）。
        "missing_daily_windows": out.get("missing_daily_windows") or [],
        # F4：入场质量（MAE/MFE 分档 + 出场通道）。数据已冻结在
        # trade_experiences，复盘此前一处都不读回去。None = 库/列不可用
        # （unknown ≠ 0），报告侧据此显示 N/A。
        "entry_quality": trade_report_stats.entry_quality_stats(
            DB_PATH, out["period_start_ts"], out["period_end_ts"],
            end_exclusive=bool(out.get("period_end_exclusive", True))),
        "missed_opportunity_metrics": {
            "source": str(LESSONS_DB),
            "candidate_window_start_ts": out[
                "missed_opps_candidate_start_ts"],
            "candidate_window_end_ts": out[
                "missed_opps_candidate_end_ts"],
            "candidate_window_end_exclusive": True,
            "outcome_horizon_hours": MISSED_OPPORTUNITY_OUTCOME_HOURS,
            "required_15m_bars": 16,
            "count": missed_count,
            **({"evidence_contract": missed_evidence}
               if missed_evidence is not None else {}),
        },
    }
    if period_kind == "daily":
        previous_audit = _raw_object(payload.get("raw")).get("report_audit")
        previous_revision = (
            previous_audit.get("revision")
            if isinstance(previous_audit, dict) else None
        )
        revision = _initial_daily_revision(previous_revision)
        revision.setdefault(
            "artifact_version",
            f"daily:{str(out.get('ts') or '')[:10]}:r{revision['number']}",
        )
        raw["report_audit"]["revision"] = revision
        out["report_revision"] = revision["number"]
        out["report_revision_kind"] = revision["kind"]
        out["resend_review_required"] = bool(
            revision["resend_review_required"])
    out["raw"] = json.dumps(raw, ensure_ascii=False)
    return out


def prepare_daily_payload(payload: dict) -> dict:
    """Make the fixed trailing-24h facts authoritative for a daily report."""
    report_ts = trade_report_stats.fmt_ts(payload.get("ts") or now_cst())
    start_ts, end_ts = trade_report_stats.daily_window(report_ts)
    out = {**payload, "ts": report_ts}
    return _prepare_trade_payload(
        out,
        start_ts=start_ts,
        end_ts=end_ts,
        end_exclusive=True,
        include_avg_hold=False,
        period_kind="daily",
    )


def _canonical_period_key(payload: dict, key: str, kind: str) -> str:
    raw = payload.get(key)
    if not raw:
        raise ValueError(f"{kind} 必填 {key}")
    value = trade_report_stats.parse_cst(str(raw))
    if any((value.hour, value.minute, value.second, value.microsecond)):
        raise ValueError(f"{key} 必须是 00:00:00 报告键")
    if kind == "weekly" and value.weekday() != 0:
        raise ValueError("week_start_ts 必须是周一 00:00:00")
    if kind == "monthly" and value.day != 1:
        raise ValueError("month_start_ts 必须是当月 1 号 00:00:00")
    return value.strftime(TS_FMT)


def _assert_weekly_report_mutation_allowed(
    week_start: str,
    operation: str,
) -> None:
    """Freeze the historical 2026-08-31 draft against rewrite or correction."""
    normalized = trade_report_stats.fmt_ts(week_start)
    if normalized in FROZEN_WEEKLY_REPORT_KEYS:
        raise ValueError(
            f"weekly report {normalized} is frozen; {operation} prohibited")


def _require_expected_period_window(
    payload: dict,
    expected_start: str,
    expected_end: str,
    kind: str,
) -> tuple[str, str]:
    """Reject caller-supplied window drift instead of silently trusting it."""
    start = trade_report_stats.fmt_ts(
        payload.get("period_start_ts") or expected_start)
    end = trade_report_stats.fmt_ts(
        payload.get("period_end_ts") or expected_end)
    if (start, end) != (expected_start, expected_end):
        raise ValueError(
            f"{kind} 统计窗口必须为 [{expected_start}, {expected_end})，"
            f"got [{start}, {end})"
        )
    if payload.get("period_end_exclusive") is False:
        raise ValueError(f"{kind} 统计窗口必须使用右开区间 [start,end)")
    return start, end


def _caliber_revision_block(report_ts: str) -> str:
    """P0-1 口径切换的一次性修订说明（主人拍板：不原地改写历史报告）。

    2026-08-20 08:00 起头条已实现盈亏由「仅 close」改为「close + reduce」。
    reduce 是**已实现**盈亏却长期被 FILL_ACTIONS 排除在头条外，8 月实测
    close 67 笔 -277.6129 / reduce 5 笔 +82.5031，头条误差 42%，其中 08-16
    直接符号翻转（-44.71 实为 +27.95）。

    已发布的历史报告**保持原样不动**：原地改写会触发 revision 语义、需要手写
    UPDATE，违反「写库必走硬化 writer」铁律，且会毁掉归档的可复现性。代价是
    归档里的旧数字与新口径不可比 —— 这一段就是为了让这件事被写下来而不是被
    发现。数字全部现算自 live_trades，不硬编码，避免说明本身随时间失真。

    只在边界后的**第一份**日报出现（DB 里没有更早的边界后日报时才渲染），
    之后自然消失；若首份报告没跑成，下一份会接着承担这个说明。
    """
    if str(report_ts) < TOTAL_REALIZED_PNL_REQUIRED_FROM:
        return ""
    try:
        con = _ro_connect(DB_PATH)
        try:
            earlier = con.execute(
                "SELECT COUNT(*) FROM daily_reports "
                "WHERE profile='live' AND ts>=? AND ts<?",
                (TOTAL_REALIZED_PNL_REQUIRED_FROM, str(report_ts))).fetchone()[0]
            if earlier:
                return ""
            daily_rows = con.execute(
                "SELECT ts,total_pnl FROM daily_reports "
                "WHERE profile='live' AND ts<? ORDER BY ts",
                (TOTAL_REALIZED_PNL_REQUIRED_FROM,)).fetchall()
            weekly_rows = con.execute(
                "SELECT week_start_ts,total_pnl,raw FROM weekly_reports "
                "WHERE profile='live' AND week_start_ts<? ORDER BY week_start_ts",
                (PERIODIC_TOTAL_REALIZED_PNL_REQUIRED_FROM,)).fetchall()
        finally:
            con.close()
        trades = _ro_connect(DB_PATH.parent / "live_trades.db")
    except sqlite3.Error:
        return ""

    def _reduce(start: str, end: str):
        row = trades.execute(
            "SELECT COALESCE(SUM(pnl),0), COUNT(*) FROM trades "
            "WHERE ts>=? AND ts<? AND action='reduce' AND pnl IS NOT NULL",
            (start, end)).fetchone()
        return float(row[0] or 0.0), int(row[1] or 0)

    lines = []
    try:
        for row in daily_rows:
            end = str(row["ts"])
            start = trade_report_stats.fmt_ts(
                trade_report_stats.daily_window(end)[0])
            delta, count = _reduce(start, end)
            if not count:
                continue
            old = float(row["total_pnl"] or 0.0)
            flip = "｜**符号翻转**" if (old < 0) != (old + delta < 0) else ""
            lines.append(
                f"| 日报 {end[:10]} | {old:.4f} | {old + delta:.4f} | "
                f"{delta:+.4f} | {count} 笔{flip} |")
        for row in weekly_rows:
            raw = _raw_object(row["raw"])
            metrics = ((raw.get("report_audit") or {}).get(
                "trade_metrics") or {}).get("live") or {}
            start = str(metrics.get("period_start_ts") or "")
            end = str(metrics.get("period_end_ts") or "")
            if not start or not end:
                continue
            delta, count = _reduce(start, end)
            if not count:
                continue
            old = float(row["total_pnl"] or 0.0)
            flip = "｜**符号翻转**" if (old < 0) != (old + delta < 0) else ""
            lines.append(
                f"| 周报 {str(row['week_start_ts'])[:10]} | {old:.4f} | "
                f"{old + delta:.4f} | {delta:+.4f} | {count} 笔{flip} |")
    finally:
        trades.close()
    if not lines:
        return ""
    return (
        "## 🧮 口径修订说明（一次性）\n\n"
        f"自 `{TOTAL_REALIZED_PNL_REQUIRED_FROM}` 起，日/周/月报头条「已实现 PnL」"
        "由**仅 close** 改为 **close + reduce**。`reduce`（部分减仓）落的是"
        "**已实现**盈亏，此前被 `FILL_ACTIONS` 排除在头条之外 —— 8 月实测 close "
        "67 笔 −277.6129、reduce 5 笔 +82.5031，头条误差 42%。\n\n"
        "下列**已发布报告保持原样不改**（原地改写会触发 revision 语义并需手写 "
        "UPDATE，违反 writer 铁律，也会毁掉归档可复现性）。归档里的数字仍是当时"
        "口径，与本报告起的新口径**不可直接比较**：\n\n"
        "| 报告 | 旧值（仅 close） | 新口径（close+reduce） | 差额 | reduce |\n"
        "|---|---:|---:|---:|---|\n"
        + "\n".join(lines)
        + "\n\n> 口径切换只影响**头条汇总数**，不改任何一笔成交事实；"
        "`realized_pnl`（仅 close）语义冻结保留在 `raw.trade_stats` 内可对照。"
        "本段只在边界后第一份日报出现一次。\n"
    )


def _missing_daily_report_days(start_ts: str, end_ts: str) -> list:
    """周窗 [start, end) 内缺失的日报（按 ts 日期部分）。只读，失败返回 []。

    日报窗为 [D-1 08:00, D 08:00)，报告 ts 的日期即窗尾日期。历史 ts 形态混杂
    （'2026-05-17' / '2026-05-31T00:00:00Z' / '2026-08-04 08:08:04'），只按
    substr(ts,1,10) 匹配日期，避免精确匹配把全部周报误判成缺失。
    """
    try:
        start = trade_report_stats.parse_cst(start_ts)
        end = trade_report_stats.parse_cst(end_ts)
        wanted = []
        cursor = start + timedelta(days=1)
        while cursor <= end:
            wanted.append(cursor.strftime("%Y-%m-%d"))
            cursor += timedelta(days=1)
        path = Path(DB_PATH).resolve()
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            present = {
                str(r[0]) for r in con.execute(
                    "SELECT DISTINCT substr(ts,1,10) FROM daily_reports "
                    "WHERE profile='live'")
            }
        finally:
            con.close()
        return [day for day in wanted if day not in present]
    except Exception:
        return []


def prepare_weekly_payload(payload: dict) -> dict:
    """Use the previous complete Monday-to-Monday interval for weekly facts.

    事实窗锚在 08:00（`[上周一 08:00, 本周一 08:00)`），与日报同相位，
    使一周七份日报恰好平铺该区间；`week_start_ts` 仍是 本周一 00:00:00 报告键。
    """
    week_start = _canonical_period_key(
        payload, "week_start_ts", "weekly")
    default_start, default_end = trade_report_stats.weekly_window(week_start)
    start, end = _require_expected_period_window(
        payload, default_start, default_end, "weekly")
    out = {
        **payload,
        "week_start_ts": week_start,
        "period_start_ts": start,
        "period_end_ts": end,
        "period_end_exclusive": True,
        # 2026-08-19 P0-3：周窗必须被 7 份日报铺满。此前只校验窗口起止对齐，
        # 从不检查覆盖 —— W10 的 7 个日窗只有 4 份日报，差额 -127.05 USDT
        # 无人发现。改为确定性计算并落 raw（agent 手填的 blocked_daily_reports
        # 不再是唯一线索），正文必须显式列出。
        "missing_daily_windows": _missing_daily_report_days(start, end),
    }
    return _prepare_trade_payload(
        out,
        start_ts=start,
        end_ts=end,
        end_exclusive=True,
        include_avg_hold=True,
        period_kind="weekly",
    )


def prepare_monthly_payload(payload: dict) -> dict:
    """Make the previous complete calendar month authoritative.

    The report key is current month day 1 at 00:00.  The fact window is the
    previous calendar month on the shared 08:00 daily anchor.  PnL, realized
    PnL drawdown, and approximate Sharpe are recomputed from confirmed live
    close fills and embedded in ``raw.report_audit``.
    """
    month_start = _canonical_period_key(
        payload, "month_start_ts", "monthly")
    default_start, default_end = trade_report_stats.monthly_window(month_start)
    start, end = _require_expected_period_window(
        payload, default_start, default_end, "monthly")
    out = {
        **payload,
        "month_start_ts": month_start,
        "period_start_ts": start,
        "period_end_ts": end,
        "period_end_exclusive": True,
    }
    out = _prepare_trade_payload(
        out,
        start_ts=start,
        end_ts=end,
        end_exclusive=True,
        include_avg_hold=False,
        period_kind="monthly",
    )
    performance = trade_report_stats.realized_performance_stats(
        LIVE_TRADES_DB,
        start,
        end,
        end_exclusive=True,
    )
    authoritative = {
        "max_drawdown": performance["max_drawdown_usdt"],
        "sharpe_approx": performance["sharpe_approx"],
    }
    corrections = []
    for key, value in authoritative.items():
        incoming_key = (
            f"live_{key}" if f"live_{key}" in out
            else key if key in out else None
        )
        if incoming_key is not None:
            incoming = out[incoming_key]
            differs = (
                incoming is not None or value is not None
            ) and not (
                incoming is None and value is None
            ) and _numeric_diff(incoming, value, tolerance=1e-8)
            if differs:
                corrections.append(f"live.{key} {incoming}→{value}")
        out[f"live_{key}"] = value
    if corrections:
        _append_anomaly(
            out,
            "月报绩效已按确认平仓 PnL 自动校正: " + "；".join(corrections),
        )

    raw = _raw_object(out.get("raw"))
    audit = raw.setdefault("report_audit", {})
    audit["performance_metrics"] = {"live": performance}
    units = raw.setdefault("metric_units", {})
    units.update({
        "monthly_reports.total_pnl": "USDT confirmed close-fill realized PnL",
        "monthly_reports.max_drawdown": (
            "USDT peak-to-trough confirmed realized-PnL curve"
        ),
        "monthly_reports.sharpe_approx": (
            "annualized 08:00 daily realized-PnL mean/stdev sqrt(365)"
        ),
    })
    out["raw"] = json.dumps(raw, ensure_ascii=False)
    return out


def _augment_operational_anomalies(payload: dict) -> None:
    """从详细 summary 确定性提取丢轮/对账自修，避免顶部仍显示“无”。"""
    for line in str(payload.get("summary") or "").splitlines():
        text = line.strip().lstrip("-").strip()
        if not text:
            continue
        if "丢轮:" in text and not re.search(r"丢轮:\s*(?:PASS|无|0\s*轮)", text):
            _append_anomaly(payload, f"WARN: {text}")
        if ("reconcile补账" in text or "对账补账" in text
                or "对账已自动补账" in text):
            _append_anomaly(payload, f"自修: {text}")
    if not _anomaly_items(payload.get("anomalies")):
        payload["anomalies"] = "无"


def load_payload(args) -> dict:
    if args.stdin:
        raw = read_stdin_text()
    elif args.json_file:
        with open(args.json_file, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    elif args.json:
        raw = args.json
    elif args.rewrite_null_and_renumber:
        return {"_mode": "rewrite_null_and_renumber"}
    elif args.markdown_only and args.kind == "weekly" and args.week_start:
        return {"week_start_ts": args.week_start}
    elif args.markdown_only and args.kind == "monthly" and args.month_start:
        return {"month_start_ts": args.month_start}
    elif args.backfill_daily_revision:
        if not args.report_ts:
            fail("--backfill-daily-revision 要求 --report-ts")
        return {
            "_mode": "backfill_daily_revision",
            "ts": args.report_ts,
        }
    else:
        fail(
            "缺少输入：需 --stdin / --json-file / --json / "
            "--rewrite-null-and-renumber / --backfill-daily-revision 之一")
    raw = sanitize_text(raw)
    try:
        return json.loads(raw)
    except Exception as e:
        fail((f"输入 JSON 解析失败: {e}；含中文/特殊符号时建议先写 <PROJECT_ROOT>\\tmp\\*.json 再用 --json-file").replace('<PROJECT_ROOT>', _public_project_path()))


def next_trade_day_num(con, report_ts: str | None = None) -> int:
    """返回日报 trade_day_num。

    v7.0e.7 修复：同一天共享同一个 trade_day_num，不能每 INSERT 一行就 +1。
    - 若当天已有非空 trade_day_num：复用当天编号
    - 否则：取所有历史 MAX(trade_day_num)+1
    """
    date_str = (report_ts or now_cst())[:10]
    cur = con.execute(
        "SELECT MIN(trade_day_num) FROM daily_reports "
        "WHERE substr(ts,1,10)=? AND trade_day_num IS NOT NULL",
        (date_str,),
    )
    same_day = cur.fetchone()[0]
    if same_day is not None:
        return int(same_day)
    cur = con.execute("SELECT MAX(trade_day_num) FROM daily_reports WHERE trade_day_num IS NOT NULL")
    mx = cur.fetchone()[0]
    return (mx if mx is not None else 0) + 1


def _daily_fields(payload: dict, profile: str) -> dict:
    """把 payload 规范成一盘 daily_reports 字段（不含 trade_day_num）。"""
    now = now_cst()

    def pf(key, default=0):
        """按 profile 优先读取 live_ 前缀字段，兼容旧无前缀字段。"""
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    # v7.0e.1/e.7: payload 按 profile 前缀拆分字段
    return {
        "ts": payload.get("ts") or now,
        "profile": profile,
        "open_count": int(pf("open_count", 0)),
        "close_count": int(pf("close_count", 0)),
        "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
        "total_fees": float(pf("total_fees", 0.0) or 0.0),
        "best_trade": pf("best_trade", None) or None,
        "worst_trade": pf("worst_trade", None) or None,
        "summary": payload.get("summary") or "",
        "lessons": payload.get("lessons") or "",
        "raw": payload.get("raw") or "",
    }


def _inherit_existing_daily_revision(con, payload: dict) -> None:
    """Preserve the stored revision state during Markdown-only re-rendering."""
    report_ts = str(payload.get("ts") or "").strip()
    if not report_ts:
        return
    row = con.execute(
        "SELECT raw FROM daily_reports WHERE ts=? "
        "ORDER BY CASE profile WHEN 'live' THEN 0 ELSE 1 END LIMIT 1",
        (report_ts,),
    ).fetchone()
    if not row:
        return
    stored_audit = _raw_object(row[0]).get("report_audit")
    stored_revision = (
        stored_audit.get("revision")
        if isinstance(stored_audit, dict) else None
    )
    if not isinstance(stored_revision, dict):
        return
    raw = _raw_object(payload.get("raw"))
    audit = raw.get("report_audit")
    if not isinstance(audit, dict):
        audit = {}
        raw["report_audit"] = audit
    audit["revision"] = dict(stored_revision)
    payload["raw"] = json.dumps(raw, ensure_ascii=False)
    payload["report_revision"] = stored_revision.get("number")
    payload["report_revision_kind"] = stored_revision.get("kind")
    payload["resend_review_required"] = bool(
        stored_revision.get("resend_review_required", False))


def write_daily(con, payload: dict, apply: bool) -> dict:
    """INSERT 一行 daily_reports，apply=False 只 print。返回结果 dict。"""
    profile = payload.get("profile", "live")
    fields = _daily_fields(payload, profile)

    if not apply:
        print(f"[DRY-RUN] would INSERT daily_reports:")
        for k, v in fields.items():
            v_disp = (v[:120] + '...') if isinstance(v, str) and len(v) > 120 else v
            print(f"  {k:14}= {v_disp}")
        return {"dry_run": True, "fields": fields}

    # 计算 trade_day_num（同一天共享编号）
    fields["trade_day_num"] = next_trade_day_num(con, fields["ts"])

    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    try:
        con.execute(
            f"INSERT INTO daily_reports ({cols}) VALUES ({placeholders})",
            list(fields.values())
        )
    except sqlite3.IntegrityError as e:
        fail(f"INSERT 失败（IntegrityError）: {e}")

    # read-after-write 校验：用 last_insert_rowid 精确回读本次插入行
    rowid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = con.execute(
        "SELECT rowid, trade_day_num, ts, profile, open_count, close_count, total_pnl "
        "FROM daily_reports WHERE rowid = ?",
        (rowid,)
    ).fetchone()
    if not row:
        fail(f"read-after-write 校验失败：rowid={rowid} 未找到")
    print(f"[OK] INSERT daily_reports: rowid={row[0]} trade_day_num={row[1]} ts={row[2]} "
          f"profile={row[3]} opn={row[4]} cls={row[5]} pnl={row[6]}")
    return {"applied": True, "rowid": row[0], "trade_day_num": row[1], "fields": fields}


def correct_existing_daily(
    con, payload: dict, profiles: list[str], apply: bool
) -> dict:
    """精确更正已存在日报，不新增行、不改主键/rowid/trade_day_num。

    这是账务迟到修复后的受控路径。每个目标 ``(ts, profile)`` 必须恰好存在
    一行；任一缺失即整批拒绝。更新后逐字段回读，事务由 main 统一提交。
    """
    report_ts = str(payload.get("ts") or "").strip()
    if not report_ts:
        raise ValueError("--correct-existing 要求 payload.ts 精确锁定原日报")
    update_cols = (
        "open_count", "close_count", "total_pnl", "total_fees",
        "best_trade", "worst_trade", "summary", "lessons", "raw",
    )
    identities = []
    previous_raw_values = []
    for profile in profiles:
        rows = con.execute(
            "SELECT rowid,trade_day_num,ts,profile,raw FROM daily_reports "
            "WHERE ts=? AND profile=?",
            (report_ts, profile),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                f"daily_reports(ts={report_ts},profile={profile}) "
                f"必须恰好 1 行，实际={len(rows)}")
        row = rows[0]
        identities.append({
            "rowid": int(row[0]),
            "trade_day_num": row[1],
            "profile": profile,
        })
        previous_raw_values.append(row[4])

    _mark_daily_corrected(payload, previous_raw_values)
    targets = []
    for identity in identities:
        profile = identity["profile"]
        fields = _daily_fields(payload, profile)
        targets.append({
            "rowid": identity["rowid"],
            "trade_day_num": identity["trade_day_num"],
            "profile": profile,
            "fields": fields,
        })

    if not apply:
        return {"dry_run": True, "correct_existing": True, "targets": targets}

    assignments = ",".join(f"{col}=?" for col in update_cols)
    for target in targets:
        fields = target["fields"]
        con.execute(
            f"UPDATE daily_reports SET {assignments} WHERE rowid=?",
            [fields[col] for col in update_cols] + [target["rowid"]],
        )
        row = con.execute(
            "SELECT rowid,trade_day_num,ts,profile,"
            + ",".join(update_cols)
            + " FROM daily_reports WHERE rowid=?",
            (target["rowid"],),
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"更正回读失败：rowid={target['rowid']} 不存在")
        if (int(row[0]) != target["rowid"]
                or row[1] != target["trade_day_num"]
                or row[2] != report_ts
                or row[3] != target["profile"]):
            raise RuntimeError(
                f"更正越界：rowid/编号/主键发生变化 profile={target['profile']}")
        stored = dict(zip(update_cols, row[4:]))
        expected = {col: fields[col] for col in update_cols}
        if stored != expected:
            raise RuntimeError(
                f"更正字段回读不一致 profile={target['profile']}: "
                f"stored={stored} expected={expected}")
    return {"applied": True, "correct_existing": True, "targets": targets}


def correct_existing_weekly(
    con, payload: dict, profiles: list[str], apply: bool
) -> dict:
    """Correct an existing weekly pair without changing its identity/number."""
    week_start = str(payload.get("week_start_ts") or "").strip()
    if not week_start:
        raise ValueError(
            "--correct-existing --kind weekly 要求 week_start_ts")
    _assert_weekly_report_mutation_allowed(
        week_start, "--correct-existing")
    update_cols = (
        "open_count", "close_count", "total_pnl", "win_rate",
        "avg_hold_hours", "margin_util_pct", "idle_ratio",
        "summary", "lessons", "raw",
    )
    targets = []

    for profile in profiles:
        rows = con.execute(
            "SELECT rowid,trade_week_num,week_start_ts,profile "
            "FROM weekly_reports WHERE week_start_ts=? AND profile=?",
            (week_start, profile),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                f"weekly_reports(week_start_ts={week_start},"
                f"profile={profile}) 必须恰好 1 行，实际={len(rows)}")
        row = rows[0]

        def pf(key, default=None):
            return payload.get(
                f"{profile}_{key}", payload.get(key, default))

        fields = {
            "open_count": int(pf("open_count", 0) or 0),
            "close_count": int(pf("close_count", 0) or 0),
            "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
            "win_rate": pf("win_rate", None),
            "avg_hold_hours": pf("avg_hold_hours", None),
            "margin_util_pct": pf("margin_util_pct", None),
            "idle_ratio": pf("idle_ratio", None),
            "summary": payload.get("summary") or "",
            "lessons": payload.get("lessons") or "",
            "raw": payload.get("raw") or "",
        }
        targets.append({
            "rowid": int(row[0]),
            "trade_week_num": row[1],
            "profile": profile,
            "fields": fields,
        })

    if not apply:
        return {
            "dry_run": True, "correct_existing": True,
            "kind": "weekly", "targets": targets,
        }

    assignments = ",".join(f"{col}=?" for col in update_cols)
    for target in targets:
        fields = target["fields"]
        con.execute(
            f"UPDATE weekly_reports SET {assignments} WHERE rowid=?",
            [fields[col] for col in update_cols] + [target["rowid"]],
        )
        row = con.execute(
            "SELECT rowid,trade_week_num,week_start_ts,profile,"
            + ",".join(update_cols)
            + " FROM weekly_reports WHERE rowid=?",
            (target["rowid"],),
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"weekly 更正回读失败：rowid={target['rowid']} 不存在")
        if (int(row[0]) != target["rowid"]
                or row[1] != target["trade_week_num"]
                or row[2] != week_start
                or row[3] != target["profile"]):
            raise RuntimeError(
                "weekly 更正越界：rowid/编号/主键发生变化 "
                f"profile={target['profile']}")
        stored = dict(zip(update_cols, row[4:]))
        expected = {col: fields[col] for col in update_cols}
        if stored != expected:
            raise RuntimeError(
                f"weekly 更正字段回读不一致 profile={target['profile']}: "
                f"stored={stored} expected={expected}")
    return {
        "applied": True, "correct_existing": True,
        "kind": "weekly", "targets": targets,
    }


def _shared_period_num(con, table: str, num_col: str, ts_col: str, ts_val: str) -> int:
    """同一周期共享编号；无则 MAX+1（禁跳号/回滚）。"""
    cur = con.execute(
        f"SELECT MIN({num_col}) FROM {table} WHERE {ts_col}=? AND {num_col} IS NOT NULL",
        (ts_val,),
    )
    same = cur.fetchone()[0]
    if same is not None:
        return int(same)
    mx = con.execute(f"SELECT MAX({num_col}) FROM {table} WHERE {num_col} IS NOT NULL").fetchone()[0]
    return (mx if mx is not None else 0) + 1


def write_weekly(con, payload: dict, apply: bool) -> dict:
    """INSERT 一行 weekly_reports（PK: week_start_ts+profile；重复即报错，不覆盖）。"""
    profile = payload.get("profile", "live")

    def pf(key, default=None):
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    week_start = payload.get("week_start_ts")
    if not week_start:
        fail("weekly 必填 week_start_ts（本周一 'YYYY-MM-DD HH:MM:SS' UTC+8）")
    fields = {
        "week_start_ts": week_start,
        "profile": profile,
        "open_count": int(pf("open_count", 0) or 0),
        "close_count": int(pf("close_count", 0) or 0),
        "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
        "win_rate": pf("win_rate", None),
        "avg_hold_hours": pf("avg_hold_hours", None),
        "margin_util_pct": pf("margin_util_pct", None),
        "idle_ratio": pf("idle_ratio", None),
        "summary": payload.get("summary") or "",
        "lessons": payload.get("lessons") or "",
        "raw": payload.get("raw") or "",
    }
    if not apply:
        print("[DRY-RUN] would INSERT weekly_reports:")
        for k, v in fields.items():
            print(f"  {k:16}= {(str(v)[:100] if v is not None else None)}")
        return {"dry_run": True, "kind": "weekly", "fields": fields}

    fields["trade_week_num"] = _shared_period_num(con, "weekly_reports", "trade_week_num",
                                                  "week_start_ts", week_start)
    cols = ", ".join(fields.keys())
    ph = ", ".join(["?"] * len(fields))
    try:
        con.execute(f"INSERT INTO weekly_reports ({cols}) VALUES ({ph})", list(fields.values()))
    except sqlite3.IntegrityError as e:
        fail(f"weekly INSERT 失败（该周期+profile 已存在，禁覆盖；如需重写请人工处理）: {e}")
    row = con.execute(
        "SELECT trade_week_num, week_start_ts, profile, total_pnl FROM weekly_reports "
        "WHERE week_start_ts=? AND profile=?",
        (week_start, profile),
    ).fetchone()
    if not row:
        fail("weekly read-after-write 校验失败")
    print(f"[OK] INSERT weekly_reports: trade_week_num={row[0]} week={row[1]} profile={row[2]} pnl={row[3]}")
    return {"applied": True, "kind": "weekly", "trade_week_num": row[0], "fields": fields}


def write_monthly(con, payload: dict, apply: bool) -> dict:
    """INSERT 一行 monthly_reports（PK: month_start_ts+profile；重复即报错，不覆盖）。"""
    profile = payload.get("profile", "live")

    def pf(key, default=None):
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    month_start = payload.get("month_start_ts")
    if not month_start:
        fail("monthly 必填 month_start_ts（本月 1 号 'YYYY-MM-DD HH:MM:SS' UTC+8）")
    fields = {
        "month_start_ts": month_start,
        "profile": profile,
        "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
        "max_drawdown": pf("max_drawdown", None),
        "sharpe_approx": pf("sharpe_approx", None),
        "summary": payload.get("summary") or "",
        "lessons": payload.get("lessons") or "",
        "raw": payload.get("raw") or "",
    }
    if not apply:
        print("[DRY-RUN] would INSERT monthly_reports:")
        for k, v in fields.items():
            print(f"  {k:16}= {(str(v)[:100] if v is not None else None)}")
        return {"dry_run": True, "kind": "monthly", "fields": fields}

    fields["trade_month_num"] = _shared_period_num(con, "monthly_reports", "trade_month_num",
                                                   "month_start_ts", month_start)
    cols = ", ".join(fields.keys())
    ph = ", ".join(["?"] * len(fields))
    try:
        con.execute(f"INSERT INTO monthly_reports ({cols}) VALUES ({ph})", list(fields.values()))
    except sqlite3.IntegrityError as e:
        fail(f"monthly INSERT 失败（该周期+profile 已存在，禁覆盖；如需重写请人工处理）: {e}")
    row = con.execute(
        "SELECT trade_month_num, month_start_ts, profile, total_pnl FROM monthly_reports "
        "WHERE month_start_ts=? AND profile=?",
        (month_start, profile),
    ).fetchone()
    if not row:
        fail("monthly read-after-write 校验失败")
    print(f"[OK] INSERT monthly_reports: trade_month_num={row[0]} month={row[1]} profile={row[2]} pnl={row[3]}")
    return {"applied": True, "kind": "monthly", "trade_month_num": row[0], "fields": fields}


def rewrite_null_and_renumber(con, apply: bool) -> dict:
    """C 方案：把所有 trade_day_num=NULL 的行重新编号（按 ts 升序）"""
    # 现有 #NULL 行
    cur = con.execute("SELECT rowid, ts, substr(summary, 1, 60) FROM daily_reports "
                       "WHERE trade_day_num IS NULL ORDER BY ts")
    nulls = cur.fetchall()
    print(f"[C 方案] 找到 {len(nulls)} 行 trade_day_num=NULL:")
    for r in nulls:
        print(f"  rowid={r[0]} ts={r[1]} summary={r[2]}...")

    # 当前最大 trade_day_num
    cur = con.execute("SELECT MAX(trade_day_num) FROM daily_reports WHERE trade_day_num IS NOT NULL")
    mx = cur.fetchone()[0] or 0
    print(f"[C 方案] 当前 MAX(trade_day_num)={mx}")

    if not apply:
        print(f"[DRY-RUN] C 方案：会按 ts 升序给 NULL 行分配 #{mx+1} ~ #{mx+len(nulls)}")
        return {"dry_run": True, "nulls_count": len(nulls), "next_num": mx+1}

    # 真写
    next_num = mx
    for rowid, ts, _summ in nulls:
        next_num += 1
        con.execute("UPDATE daily_reports SET trade_day_num = ? WHERE rowid = ?", (next_num, rowid))
        print(f"  [OK] rowid={rowid} ts={ts} → trade_day_num={next_num}")

    return {"applied": True, "renumbered": len(nulls), "next_num": next_num}


def write_markdown(payload: dict, apply: bool) -> str:
    """写 reports/daily-reports/daily-YYYY-MM-DD.md（v7.4 固定24h复盘窗口）"""
    if not apply:
        ts = payload.get("ts", now_cst())
        date_str = ts[:10]
        path = REPORTS_DIR / f"daily-{date_str}.md"
        print(f"[DRY-RUN] would write markdown: {path}")
        return str(path)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = payload.get("ts", now_cst())
    date_str = ts[:10]
    path = REPORTS_DIR / f"daily-{date_str}.md"
    _augment_operational_anomalies(payload)
    default_start, default_end = trade_report_stats.daily_window(ts)
    period_start_ts = trade_report_stats.fmt_ts(
        payload.get("period_start_ts") or default_start)
    period_end_ts = trade_report_stats.fmt_ts(
        payload.get("period_end_ts") or default_end)
    period_end_exclusive = bool(
        payload.get("period_end_exclusive", True))
    if not period_end_exclusive:
        raise ValueError("日报复盘窗口必须使用右开区间 [start,end)")

    # v7.0e.1: 按 profile 前缀读
    def v(prefix, key, default=0):
        """读 payload[key]，优先用 live_ 前缀"""
        return payload.get(f"{prefix}_{key}", payload.get(key, default))

    # writer 自取权威值：避免 reviewer 漏传字段时把顶部默认为 0/空仓，而详细 summary 又写真值。
    # 全部按报告 ts 回看，允许安全重渲染历史日报，不误用当前仓位/当前累计收益。
    _live_eq = v('live', 'equity', payload.get('current_equity', None))
    _live_eq_db = _snapshot_equity(DB_PATH, 'live', ts)
    live_eq = _live_eq_db if _live_eq_db is not None else (_live_eq or 0)
    _live_cum_db = _authoritative_cum_pnl(DB_PATH, 'live', ts)
    live_realized_pnl = _live_cum_db if _live_cum_db is not None else v('live', 'realized_pnl', 0)
    live_bill_net = _account_bill_net_for_window(
        DB_PATH, 'live', period_start_ts, period_end_ts)
    live_open = v('live', 'open_count')
    live_close = v('live', 'close_count')
    live_pnl_today = v('live', 'total_pnl', 0)
    live_fees = v('live', 'total_fees', 0)
    live_best = v('live', 'best_trade', '—') or '—'
    live_worst = v('live', 'worst_trade', '—') or '—'
    _live_pos_db = _snapshot_positions_summary(DB_PATH, 'live', ts)
    live_pos = _live_pos_db if _live_pos_db is not None else v(
        'live', 'positions_summary', payload.get('positions_summary', '持仓数据不可用'))

    # demo 的资产/交易变量装配（含 live↔demo equity 混淆告警）随 2026-08-06
    # demo 全量下线整块移除。
    live_rejects = v('live', 'risk_rejected_open_summary', '0 笔')
    live_close_long = v('live', 'close_long_count', None)
    live_close_short = v('live', 'close_short_count', None)
    if live_close_long is not None and live_close_short is not None:
        live_side_line = (
            f"- 平仓方向: 多 {int(live_close_long)} / 空 {int(live_close_short)}"
            "（确定性统计）\n")
    else:
        live_side_line = ""
    _missed = payload.get('missed_opps_window_count')
    _missed_start = payload.get('missed_opps_candidate_start_ts')
    _missed_end = payload.get('missed_opps_candidate_end_ts')
    # 2026-08-19 08:00 边界起注明第二来源口径（validator 正则止于 UTC+8，
    # 计数复核读同一 lessons.db 窗口 COUNT，注明文字不影响一致性校验）。
    _missed_src = (
        "；lessons.db 权威计数；含 briefing_layer_v1 前向源"
        "（2026-08-19 08:00 激活，触及率不与旧 wait 口径直接对比））\n"
        if ts >= MISSED_BRIEFING_SOURCE_ACTIVATION_TS
        else "；lessons.db 权威计数）\n")
    missed_line = (
        "- 已完整成熟4小时的错失机会记录: "
        f"{int(_missed)} 条（候选窗口 [{_missed_start}, {_missed_end})，"
        f"UTC+8{_missed_src}"
        if _missed is not None and _missed_start and _missed_end else "")
    missed_machine_line = _missed_opportunity_machine_line(payload)
    exit_quality_block = (
        _exit_quality_block(payload)
        if ts >= EXIT_QUALITY_ACTIVATION_TS else ""
    )
    caliber_revision_block = _caliber_revision_block(ts)
    report_state = _report_state(payload)
    report_banner = _report_label(report_state)
    if report_state["status"] == "provisional":
        report_banner = (
            f"临时报告｜{report_state['reason']}；允许发布，"
            "成交与收益以后续对账补正为准"
        )
    raw_for_revision = _raw_object(payload.get("raw"))
    audit_for_revision = raw_for_revision.get("report_audit")
    revision = (
        audit_for_revision.get("revision")
        if isinstance(audit_for_revision, dict) else {}
    )
    if not isinstance(revision, dict):
        revision = {}
    try:
        revision_number = int(revision.get("number") or 1)
    except (TypeError, ValueError):
        revision_number = 1
    revision_kind = str(revision.get("kind") or "initial")
    resend_review_required = bool(
        revision.get("resend_review_required", False))

    live_bill_line = (
        f"${live_bill_net['net']:.2f}（账单至 {live_bill_net['last_ts']}）"
        if live_bill_net else "账单未覆盖"
    )

    md = f"""# 📊 小灵日报 {date_str}（v7.4 固定24h复盘窗口）

> 自动生成 by daily_report_writer.py (P7 复盘写入器) — v7.4 收益口径拆分
> ts: {ts} | trade_day_num 见 db
> 统计窗口: [{period_start_ts}, {period_end_ts})，UTC+8（固定24小时）
> **报告状态：{report_banner}**
> report_revision: {revision_number} | revision_kind: {revision_kind} | resend_review_required: {str(resend_review_required).lower()} | auto_resend: false

---

## 💰 资产

### 🟢 实盘（live）
| 项 | 数值 |
|---|---|
| 资金总额 | ${float(live_eq):.2f} |
| 累计交易PnL（未扣手续费/资金费） | ${float(live_realized_pnl):.2f} |
| 本复盘周期账户账单净变动（含手续费/资金费） | {live_bill_line} |

> 累计交易PnL = 冻结基线 + reset 后 trades.pnl；不含手续费、资金费和浮动盈亏。
> 本复盘周期账户账单净变动 = OKX account_bills 中 type=2/8 的 bal_change；严格使用上方固定24小时统计窗口，仅代表注明的账单采集覆盖。

## 📈 持仓

### 🟢 实盘
{live_pos}

## 🎯 交易

### 🟢 实盘
- 本复盘周期成交开仓: {int(live_open)} 笔
- 本复盘周期成交平仓: {int(live_close)} 笔
{live_side_line}{missed_line}{missed_machine_line}- 开仓尝试被风控拒绝: {live_rejects}
- 净 PnL: ${float(live_pnl_today):.2f}
- 手续费: ${float(live_fees):.2f}
- 最佳: {live_best} | 最差: {live_worst}

{caliber_revision_block}{exit_quality_block}
## ⚠️ 异常 / 🛠 自修

{payload.get('anomalies', '无')}

## 🛰 全市场扫描

{_universe_scan_block(DB_PATH.parent, period_start_ts, period_end_ts)}

## 📡 数据完善率

{_data_completeness_block(DB_PATH.parent, period_start_ts, period_end_ts)}

## 🌍 市场

### 市场总览（writer 权威回读）

{_market_overview_block(DB_PATH.parent, ts)}

### 复盘观察

{payload.get('market', '见 push_archive latest')}

## 🔭 次日关注

{payload.get('focus_next_day', '未填写（激活边界后 validator 将拒绝外发）')}

## 🧠 教训

{payload.get('lessons', '见 lessons.db')}

---

## 详细 summary

{payload.get('summary', '')}

## 详细 lessons (JSON)

```json
{payload.get('lessons', '')}
```

---

🤖 自动生成 by 小灵 🧚‍♀️ | {now_cst()} CST | daily_report_writer.py v1.4 (v7.4)
"""
    _atomic_write_text(path, md)
    print(f"[OK] wrote markdown: {path} ({path.stat().st_size}B)")
    return str(path)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write a UTF-8 text artifact with same-directory atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
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
            tmp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def write_weekly_markdown(payload: dict, apply: bool) -> str:
    """Render the authoritative weekly payload to a durable Markdown artifact."""
    week_key = str(payload.get("week_start_ts") or "").strip()
    if not week_key:
        raise ValueError("weekly markdown 要求 week_start_ts")
    path = WEEKLY_REPORTS_DIR / f"weekly-{week_key[:10]}.md"
    if not apply:
        print(f"[DRY-RUN] would atomically write weekly markdown: {path}")
        return str(path)

    def value(profile: str, key: str, default=None):
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    def number(raw, digits=4):
        if raw in (None, ""):
            return "—"
        try:
            return f"{float(raw):.{digits}f}"
        except (TypeError, ValueError):
            return str(raw)

    report_state = _report_state(payload)
    report_label = _report_label(report_state)
    start = str(payload.get("period_start_ts") or "")
    end = str(payload.get("period_end_ts") or "")
    if start and end:
        period_line = f"[{start}, {end})，UTC+8"
    else:
        period_line = "历史记录未声明（以 account.db 原行及 summary 为准）"
    week_num = payload.get("trade_week_num")
    _wl = payload.get("live_close_long_count")
    _ws = payload.get("live_close_short_count")
    _wm = payload.get("missed_opps_window_count")
    _facts_bits = []
    if _wl is not None and _ws is not None:
        _facts_bits.append(f"平仓方向: 多 {int(_wl)} / 空 {int(_ws)}")
    if _wm is not None:
        _facts_bits.append(
            "已完整成熟4小时的错失机会记录 "
            f"{int(_wm)} 条（候选窗口 "
            f"[{payload.get('missed_opps_candidate_start_ts')}, "
            f"{payload.get('missed_opps_candidate_end_ts')})）")
    side_facts_line = (
        "> 确定性事实：" + "；".join(_facts_bits) + "（文字段与此冲突以本行为准）\n"
        if _facts_bits else "")
    missed_machine_line = _missed_opportunity_machine_line(payload)
    rows = []
    for profile, label in (("live", "实盘"),):
        rows.append(
            "| {label} | {opens} | {closes} | {pnl} | {win_rate}% | "
            "{rejects} | {avg_hold} |".format(
                label=label,
                opens=int(value(profile, "open_count", 0) or 0),
                closes=int(value(profile, "close_count", 0) or 0),
                pnl=number(value(profile, "total_pnl", 0.0)),
                win_rate=number(value(profile, "win_rate"), 2),
                rejects=str(
                    value(profile, "risk_rejected_open_summary", "0 笔")
                    or "0 笔"
                ),
                avg_hold=number(value(profile, "avg_hold_hours"), 2),
            )
        )

    side_breakdown = value("live", "close_side_breakdown", {}) or {}
    side_rows = []
    for side_key, label in (("long", "多"), ("short", "空")):
        item = side_breakdown.get(side_key) or {}
        wr = item.get("win_rate_pct")
        avg = item.get("pnl_avg_usdt")
        side_rows.append(
            "| {label} | {count} | {wins} | {wr} | {pnl_sum} | {pnl_avg} |".format(
                label=label,
                count=int(item.get("close_count") or 0),
                wins=int(item.get("win_count") or 0),
                wr=("—" if wr is None else f"{float(wr):.2f}%"),
                pnl_sum=number(item.get("pnl_sum_usdt")),
                pnl_avg=("—" if avg is None else number(avg)),
            )
        )

    content = f"""# 小灵周报 {week_key[:10]}

> 报告键：{week_key}（本周一边界）
> 统计窗口：{period_line}
> trade_week_num：{week_num if week_num is not None else "见 account.db"}
> 报告状态：{report_label}
> 胜率单位：百分数（0–100）

## 成交与绩效

| 盘别 | 成交开仓 | 成交平仓 | 已实现 PnL | 胜率 | 风控拒绝开仓尝试 | 已平仓平均持仓小时 |
|---|---:|---:|---:|---:|---|---:|
{chr(10).join(rows)}

{side_facts_line}{missed_machine_line}
## 平仓方向明细

| 方向 | 平仓数 | 胜单数 | 胜率 | PnL 合计（USDT） | PnL 均值（USDT） |
|---|---:|---:|---:|---:|---:|
{chr(10).join(side_rows)}

## 复盘摘要

{payload.get("summary") or "无"}

## 教训

{payload.get("lessons") or "无"}

---

自动生成：daily_report_writer.py | {now_cst()} CST
"""
    _atomic_write_text(path, content)
    print(f"[OK] atomically wrote weekly markdown: {path} ({path.stat().st_size}B)")
    return str(path)


def render_monthly_markdown_from_existing(con, month_start) -> str:
    """从现存 monthly_reports 行补渲染月报 Markdown；只读，不写任何表。

    行按存储键精确匹配。历史行（期初键规范化前）的键是窗口起点（如
    2026-07-01 08:00:00 覆盖 7 月），而 canonical 键是产出月 1 号 00:00:00
    （窗口=上一自然月），故 canonical 键 = 存储键月份 + 1 个月。窗口统计
    （开/平仓、方向明细、回撤、Sharpe）由 prepare_monthly_payload 从成交库
    确定性重算；重算 live_total_pnl 与存储行 total_pnl 相差 >1e-6 即中止、
    不落盘——防止把与既有事实矛盾的数字写成归档。summary/lessons/
    trade_month_num 以存储行为权威。"""
    key = str(month_start or "").strip()
    if not key:
        fail("--kind monthly --markdown-only 要求 --month-start（精确存储键）")
    row = con.execute(
        "SELECT month_start_ts, total_pnl, summary, lessons, trade_month_num"
        " FROM monthly_reports WHERE month_start_ts=? AND profile='live'",
        (key,),
    ).fetchone()
    if row is None:
        fail(f"monthly_reports 无此行：month_start_ts={key!r} profile=live")
    stored_key = str(row[0])
    try:
        year = int(stored_key[:4])
        month = int(stored_key[5:7])
        day = int(stored_key[8:10])
    except ValueError:
        year = month = day = -1
    if day != 1:
        fail(f"存储键不是月首日，拒绝推断窗口：{stored_key!r}")
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    canonical = f"{year:04d}-{month:02d}-01 00:00:00"
    payload = prepare_monthly_payload({
        "month_start_ts": canonical,
        "summary": row[2],
        "lessons": row[3],
        "trade_month_num": row[4],
        # 历史文本先于 r-语义 lint（Wave0-2）；词汇规则对补渲染豁免，
        # 事实核对与 total_pnl 一致性闸保持生效。
        "allow_legacy_r_vocabulary": True,
    })
    stored_pnl = row[1]
    recomputed = payload.get("live_total_pnl")
    if abs(float(stored_pnl or 0.0) - float(recomputed or 0.0)) > 1e-6:
        fail(
            "月报补渲染中止：窗口重算 total_pnl 与存储行不一致 "
            f"(stored={stored_pnl} recomputed={recomputed} "
            f"window=[{payload.get('period_start_ts')}, "
            f"{payload.get('period_end_ts')}))；"
            "该行事实与月窗口对不上，不落盘"
        )
    payload["trade_month_num"] = row[4]
    return write_monthly_markdown(payload, True)


def write_monthly_markdown(payload: dict, apply: bool) -> str:
    """Render an authoritative monthly payload to a durable Markdown file."""
    month_key = str(payload.get("month_start_ts") or "").strip()
    if not month_key:
        raise ValueError("monthly markdown 要求 month_start_ts")
    path = MONTHLY_REPORTS_DIR / f"monthly-{month_key[:10]}.md"
    if not apply:
        print(f"[DRY-RUN] would atomically write monthly markdown: {path}")
        return str(path)

    def value(key: str, default=None):
        return payload.get(f"live_{key}", payload.get(key, default))

    def number(raw, digits=4):
        if raw in (None, ""):
            return "—"
        try:
            return f"{float(raw):.{digits}f}"
        except (TypeError, ValueError):
            return str(raw)

    report_state = _report_state(payload)
    report_label = _report_label(report_state)
    start = str(payload.get("period_start_ts") or "")
    end = str(payload.get("period_end_ts") or "")
    if not start or not end:
        raise ValueError("monthly markdown 缺少权威 period_start_ts/end_ts")
    month_num = payload.get("trade_month_num")
    side_breakdown = value("close_side_breakdown", {}) or {}
    side_rows = []
    for side_key, label in (("long", "多"), ("short", "空")):
        item = side_breakdown.get(side_key) or {}
        wr = item.get("win_rate_pct")
        avg = item.get("pnl_avg_usdt")
        side_rows.append(
            "| {label} | {count} | {wins} | {wr} | {pnl_sum} | {pnl_avg} |".format(
                label=label,
                count=int(item.get("close_count") or 0),
                wins=int(item.get("win_count") or 0),
                wr=("—" if wr is None else f"{float(wr):.2f}%"),
                pnl_sum=number(item.get("pnl_sum_usdt")),
                pnl_avg=("—" if avg is None else number(avg)),
            )
        )
    missed = payload.get("missed_opps_window_count")
    missed_line = (
        "> 已完整成熟4小时的错失机会记录："
        f"{int(missed)} 条（候选窗口 "
        f"[{payload.get('missed_opps_candidate_start_ts')}, "
        f"{payload.get('missed_opps_candidate_end_ts')})，UTC+8）\n"
        if missed is not None else ""
    )
    missed_machine_line = _missed_opportunity_machine_line(payload)
    content = f"""# 小灵月报 {month_key[:10]}

> 报告键：{month_key}（本月 1 号边界）
> 统计窗口：[{start}, {end})，UTC+8
> trade_month_num：{month_num if month_num is not None else "见 account.db"}
> 报告状态：{report_label}
> 最大回撤口径：确认平仓 realized PnL 累计曲线峰谷差，单位 USDT
> Sharpe 近似口径：08:00 日界已实现 PnL（含零收益日），sqrt(365)，未扣无风险利率

## 成交与绩效

| 盘别 | 成交开仓 | 成交平仓 | 已实现 PnL | 最大回撤（USDT） | Sharpe 近似 | 风控拒绝开仓尝试 |
|---|---:|---:|---:|---:|---:|---|
| 实盘 | {int(value('open_count', 0) or 0)} | {int(value('close_count', 0) or 0)} | {number(value('total_pnl', 0.0))} | {number(value('max_drawdown'))} | {number(value('sharpe_approx'))} | {value('risk_rejected_open_summary', '0 笔') or '0 笔'} |

{missed_line}{missed_machine_line}## 平仓方向明细

| 方向 | 平仓数 | 胜单数 | 胜率 | PnL 合计（USDT） | PnL 均值（USDT） |
|---|---:|---:|---:|---:|---:|
{chr(10).join(side_rows)}

## 复盘摘要

{payload.get("summary") or "无"}

## 教训

{payload.get("lessons") or "无"}

---

自动生成：daily_report_writer.py | {now_cst()} CST
"""
    _atomic_write_text(path, content)
    print(
        f"[OK] atomically wrote monthly markdown: {path} "
        f"({path.stat().st_size}B)"
    )
    return str(path)


def _weekly_percent_value(value, raw: dict, profile: str):
    """Normalize legacy ratio rows for read-only Markdown backfill display."""
    try:
        audited = raw["report_audit"]["trade_metrics"][profile]["win_rate_pct"]
        if audited is not None:
            return float(audited)
    except (KeyError, TypeError, ValueError):
        pass
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return value
    units = raw.get("metric_units")
    marked_percent = (
        isinstance(units, dict)
        and units.get("weekly_reports.win_rate") == "percent_0_100"
    )
    if number is not None and 0 <= number <= 1 and not marked_percent:
        return number * 100.0
    return number


def load_existing_weekly_payload(
    con: sqlite3.Connection, week_start: str
) -> dict:
    """Merge one existing weekly report row using a read-only connection.

    同 daily：历史周报可能是 live+demo 两行或 live 单行，放宽为只要求 live。"""
    rows = con.execute(
        "SELECT week_start_ts,profile,open_count,close_count,total_pnl,"
        "win_rate,avg_hold_hours,margin_util_pct,idle_ratio,summary,"
        "lessons,raw,trade_week_num FROM weekly_reports "
        "WHERE week_start_ts=? ORDER BY profile",
        (week_start,),
    ).fetchall()
    if not rows or "live" not in {str(row[1]) for row in rows}:
        raise RuntimeError(
            f"weekly markdown backfill requires a live row: {week_start}")
    rows = [row for row in rows if str(row[1]) == "live"]
    payload = {
        "week_start_ts": week_start,
        "trade_week_num": rows[0][12],
        "summary": rows[0][9] or "",
        "lessons": rows[0][10] or "",
        "raw": rows[0][11] or "",
    }
    raw = _raw_object(payload["raw"])
    audit = raw.get("report_audit")
    if isinstance(audit, dict):
        state = audit.get("report_state")
        if isinstance(state, dict):
            payload["live_reconcile_status"] = state.get(
                "live_reconcile_status")
            payload["live_reconcile_issue_count"] = state.get(
                "live_reconcile_issue_count")
        metrics = audit.get("trade_metrics")
        if isinstance(metrics, dict):
            live_metrics = metrics.get("live")
            if isinstance(live_metrics, dict):
                payload["period_start_ts"] = live_metrics.get(
                    "period_start_ts")
                payload["period_end_ts"] = live_metrics.get(
                    "period_end_ts")
                side_breakdown = live_metrics.get("close_side_breakdown")
                if isinstance(side_breakdown, dict):
                    payload["live_close_side_breakdown"] = side_breakdown
                    for side in ("long", "short"):
                        side_metrics = side_breakdown.get(side)
                        if isinstance(side_metrics, dict):
                            payload[f"live_close_{side}_count"] = (
                                side_metrics.get("close_count"))
        missed_metrics = audit.get("missed_opportunity_metrics")
        if isinstance(missed_metrics, dict):
            missed_start = missed_metrics.get("candidate_window_start_ts")
            missed_end = missed_metrics.get("candidate_window_end_ts")
            missed_count = missed_metrics.get("count")
            payload["missed_opps_candidate_start_ts"] = missed_start
            payload["missed_opps_candidate_end_ts"] = missed_end
            evidence_contract = missed_metrics.get("evidence_contract")
            if isinstance(evidence_contract, dict):
                payload["missed_opportunity_evidence_contract"] = (
                    evidence_contract)
            if (missed_count is None
                    and not isinstance(evidence_contract, dict)
                    and payload.get("period_start_ts")
                    and payload.get("period_end_ts")):
                expected_missed = _missed_opps_candidate_window(
                    payload["period_start_ts"], payload["period_end_ts"])
                if (missed_start, missed_end) != expected_missed:
                    raise RuntimeError(
                        "weekly markdown backfill refused: stored missed-"
                        "opportunity window differs from fixed contract")
                # 只读恢复旧行在生成时尚未成熟的 count。生产函数仅在数据源
                # 已覆盖候选窗右端时返回整数，否则仍为 None 并保持 fail-closed。
                missed_count = _missed_opps_window_count(
                    payload["period_start_ts"], payload["period_end_ts"])
            payload["missed_opps_window_count"] = missed_count
    for row in rows:
        profile = str(row[1])
        row_raw = _raw_object(row[11])
        payload.update({
            f"{profile}_open_count": row[2],
            f"{profile}_close_count": row[3],
            f"{profile}_total_pnl": row[4],
            f"{profile}_win_rate": _weekly_percent_value(
                row[5], row_raw, profile),
            f"{profile}_avg_hold_hours": row[6],
            f"{profile}_margin_util_pct": row[7],
            f"{profile}_idle_ratio": row[8],
        })
        try:
            reject_count = row_raw[
                "report_audit"]["trade_metrics"][profile][
                    "risk_rejected_open_attempts"]["count"]
            payload[f"{profile}_risk_rejected_open_summary"] = (
                f"{int(reject_count)} 笔")
        except (KeyError, TypeError, ValueError):
            payload[f"{profile}_risk_rejected_open_summary"] = "历史未记录"
    return payload


def _commit_then_write_weekly(
    con: sqlite3.Connection, payload: dict, apply: bool
) -> str:
    """Commit DB facts before atomic file replacement; backfill repairs failures."""
    con.commit()
    return write_weekly_markdown(payload, apply)


def _commit_then_write_monthly(
    con: sqlite3.Connection, payload: dict, apply: bool
) -> str:
    """Commit DB facts before monthly file replacement."""
    con.commit()
    return write_monthly_markdown(payload, apply)


def _commit_then_write_daily(
    con: sqlite3.Connection, payload: dict, apply: bool
) -> str:
    """Commit DB facts before atomic file replacement; backfill repairs failures."""
    con.commit()
    return write_markdown(payload, apply)


def main():
    global DB_PATH, REPORTS_DIR, WEEKLY_REPORTS_DIR, MONTHLY_REPORTS_DIR
    global LIVE_TRADES_DB, MARKET_DB, LEDGER_DB, BRIEFING_LOG_DIR
    global QUALITY_REPORT_DIR, REVIEWER_READY_DIR
    ap = argparse.ArgumentParser(description="Daily Report Writer (P7 hardened writer)")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读 JSON")
    ap.add_argument("--json-file", help="从文件读 JSON")
    ap.add_argument("--json", help="JSON 字符串")
    ap.add_argument("--apply", action="store_true", help="真写模式（默认 dry-run）")
    ap.add_argument("--rewrite-null-and-renumber", action="store_true",
                    help="C 方案：把 trade_day_num=NULL 的行重新编号（需 --apply 才生效）")
    ap.add_argument("--no-markdown", action="store_true", help="不写 markdown 文件")
    ap.add_argument("--markdown-only", action="store_true",
                    help="仅重渲染 daily/weekly markdown，不改报告表；仍需 --apply")
    ap.add_argument(
        "--week-start",
        help="weekly --markdown-only 的现存周报键 YYYY-MM-DD HH:MM:SS",
    )
    ap.add_argument(
        "--month-start",
        help="monthly --markdown-only 的现存月报键 YYYY-MM-DD HH:MM:SS（按存储值精确匹配，含历史非规范键）",
    )
    ap.add_argument("--correct-existing", action="store_true",
                    help="精确更正已存在 daily/weekly 行；不插入、不改 rowid/编号/主键")
    ap.add_argument(
        "--backfill-daily-revision",
        action="store_true",
        help=(
            "仅为既有日报补 raw.report_audit.revision 与 Markdown revision 行；"
            "默认 dry-run，不重算其他事实、不自动重发"
        ),
    )
    ap.add_argument(
        "--report-ts",
        help="revision backfill 精确日报键 YYYY-MM-DD HH:MM:SS",
    )
    ap.add_argument(
        "--report-file",
        help="revision backfill Markdown 路径；默认按 report-ts 日期定位",
    )
    ap.add_argument(
        "--backup-dir",
        help="revision backfill --apply 必填；更新前备份并校验数据库和 Markdown",
    )
    ap.add_argument("--kind", choices=("daily", "weekly", "monthly"), default="daily",
                    help="报告类型：daily=daily_reports（默认）；weekly=weekly_reports（需 week_start_ts）；monthly=monthly_reports（需 month_start_ts）")
    ap.add_argument("--profiles", choices=("live",), default="live",
                    help="写入 profile 范围（2026-08-06 demo 下线后只剩 live）")
    ap.add_argument("--db-path", default=str(DB_PATH), help='account.db 路径（默认 <PROJECT_ROOT>\\db\\account.db；测试可传临时库）'.replace('<PROJECT_ROOT>', _public_project_path()))
    ap.add_argument("--reports-dir", default=str(REPORTS_DIR), help="日报 markdown 输出目录")
    ap.add_argument(
        "--weekly-reports-dir",
        default=str(WEEKLY_REPORTS_DIR),
        help="周报 markdown 输出目录",
    )
    ap.add_argument(
        "--monthly-reports-dir",
        default=str(MONTHLY_REPORTS_DIR),
        help="月报 markdown 输出目录",
    )
    ap.add_argument("--live-trades-db", default=str(LIVE_TRADES_DB),
                    help="live_trades.db 路径")
    ap.add_argument("--market-db", default=str(MARKET_DB),
                    help="market.db 路径（错失机会证据只读复算）")
    ap.add_argument("--ledger-db", default=str(LEDGER_DB),
                    help="ledger.db 路径（风控拒绝尝试事实源）")
    ap.add_argument("--briefing-log-dir", default=str(BRIEFING_LOG_DIR),
                    help="briefing_candidates_v1 JSONL 目录")
    ap.add_argument("--quality-report-dir", default=str(QUALITY_REPORT_DIR),
                    help="冻结质量工件目录")
    ap.add_argument("--reviewer-ready-dir", default=str(REVIEWER_READY_DIR),
                    help="reviewer ready manifest 目录")
    args = ap.parse_args()

    DB_PATH = Path(args.db_path)
    REPORTS_DIR = Path(args.reports_dir)
    WEEKLY_REPORTS_DIR = Path(args.weekly_reports_dir)
    MONTHLY_REPORTS_DIR = Path(args.monthly_reports_dir)
    LIVE_TRADES_DB = Path(args.live_trades_db)
    MARKET_DB = Path(args.market_db)
    LEDGER_DB = Path(args.ledger_db)
    BRIEFING_LOG_DIR = Path(args.briefing_log_dir)
    QUALITY_REPORT_DIR = Path(args.quality_report_dir)
    REVIEWER_READY_DIR = Path(args.reviewer_ready_dir)

    payload = (
        {}
        if args.markdown_only and args.kind == "weekly"
        else load_payload(args)
    )
    # 内部豁免标记只允许 render_monthly_markdown_from_existing 进程内设置；
    # 外部输入（stdin/json/file）一律剥离，防新报告借标记绕叙事 lint。
    payload.pop("allow_legacy_r_vocabulary", None)

    if not DB_PATH.exists():
        fail(f"db 不存在：{DB_PATH}")
    if args.correct_existing and args.kind == "monthly":
        fail("--correct-existing 暂不支持 --kind monthly")
    if sum(bool(x) for x in (
            args.markdown_only, args.correct_existing,
            args.rewrite_null_and_renumber,
            args.backfill_daily_revision)) > 1:
        fail(
            "--markdown-only/--correct-existing/--rewrite-null-and-renumber/"
            "--backfill-daily-revision 互斥")
    if args.backfill_daily_revision:
        if args.kind != "daily":
            fail("--backfill-daily-revision 仅支持 --kind daily")
        if args.no_markdown:
            fail("--backfill-daily-revision 不允许 --no-markdown")
        if args.profiles != "live":
            fail("--backfill-daily-revision 需要 live 行")
        if args.apply and not args.backup_dir:
            fail("--backfill-daily-revision --apply 必须提供 --backup-dir")
    if args.markdown_only and args.kind == "weekly":
        if not args.apply:
            fail("weekly --markdown-only 需同时给 --apply")
        if args.no_markdown:
            fail("--markdown-only 与 --no-markdown 冲突")
        if not args.week_start:
            fail("weekly --markdown-only 要求 --week-start")
        try:
            _assert_weekly_report_mutation_allowed(
                args.week_start, "--markdown-only")
        except ValueError as exc:
            fail(str(exc))
        ro = sqlite3.connect(
            f"file:{DB_PATH.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=10,
        )
        try:
            ro.execute("PRAGMA busy_timeout=5000")
            payload = load_existing_weekly_payload(ro, args.week_start)
        finally:
            ro.close()
        result = {
            "markdown_only": True,
            "kind": "weekly",
            "path": write_weekly_markdown(payload, True),
            "database_write": False,
        }
        print(f"\n[result] {json.dumps(result, ensure_ascii=False)}")
        sys.exit(0)

    if args.backfill_daily_revision:
        report_ts = trade_report_stats.fmt_ts(args.report_ts)
        report_path = (
            Path(args.report_file)
            if args.report_file
            else REPORTS_DIR / f"daily-{report_ts[:10]}.md"
        )
        if not report_path.exists():
            fail(f"revision backfill 日报 Markdown 不存在：{report_path}")
        if not args.apply:
            try:
                ro = sqlite3.connect(
                    f"file:{DB_PATH.resolve().as_posix()}?mode=ro",
                    uri=True,
                    timeout=10,
                )
                try:
                    ro.execute("PRAGMA busy_timeout=5000")
                    plan = plan_daily_revision_backfill(
                        ro, report_ts, report_path)
                finally:
                    ro.close()
            except Exception as exc:
                fail(f"revision backfill dry-run 失败：{exc}")
            result = public_daily_revision_backfill_plan(
                plan, False)
        else:
            # Validate the exact scope before creating backup artifacts.
            try:
                preflight = sqlite3.connect(
                    f"file:{DB_PATH.resolve().as_posix()}?mode=ro",
                    uri=True,
                    timeout=10,
                )
                try:
                    preflight.execute("PRAGMA busy_timeout=5000")
                    plan_daily_revision_backfill(
                        preflight, report_ts, report_path)
                finally:
                    preflight.close()
                backup = create_daily_revision_backup(
                    DB_PATH,
                    report_path,
                    Path(args.backup_dir),
                    report_ts,
                )
            except Exception as exc:
                fail(f"revision backfill 备份前检查失败：{exc}")
            con = sqlite3.connect(DB_PATH, timeout=10)
            try:
                con.execute("PRAGMA busy_timeout=5000")
                plan = plan_daily_revision_backfill(
                    con, report_ts, report_path)
                con.execute("BEGIN IMMEDIATE")
                apply_daily_revision_backfill_db(con, plan)
                con.commit()
                # DB metadata is the durable fact.  If atomic file replacement
                # fails, rerunning this idempotent command repairs only the file.
                apply_daily_revision_backfill_markdown(plan)
                verification = plan_daily_revision_backfill(
                    con, report_ts, report_path)
                idempotent_verified = (
                    not verification["row_updates"]
                    and not verification["markdown_change"]
                )
                if not idempotent_verified:
                    raise RuntimeError(
                        "revision backfill 二次幂等校验失败")
                check = con.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError(
                        "revision backfill apply 后 quick_check 失败")
                result = public_daily_revision_backfill_plan(
                    plan,
                    True,
                    backup=backup,
                    idempotent_verified=True,
                )
            except Exception as exc:
                con.rollback()
                fail(f"revision backfill 失败：{exc}")
            finally:
                con.close()
        print(f"\n[result] {json.dumps(result, ensure_ascii=False)}")
        sys.exit(0)

    con = sqlite3.connect(DB_PATH)
    weekly_markdown_pending = False
    monthly_markdown_pending = False
    daily_markdown_pending = False
    try:
        if (args.kind == "daily"
                and payload.get("_mode") != "rewrite_null_and_renumber"):
            payload = prepare_daily_payload(payload)
        elif args.kind == "weekly":
            payload = prepare_weekly_payload(payload)
        elif args.kind == "monthly" and not args.markdown_only:
            payload = prepare_monthly_payload(payload)

        if args.markdown_only:
            if args.kind not in ("daily", "monthly"):
                fail("--markdown-only 仅支持 --kind daily/monthly")
            if not args.apply:
                fail("--markdown-only 需同时给 --apply")
            if args.kind == "monthly":
                result = {
                    "markdown_only": True,
                    "path": render_monthly_markdown_from_existing(
                        con, payload.get("month_start_ts")),
                }
            else:
                _inherit_existing_daily_revision(con, payload)
                result = {
                    "markdown_only": True,
                    "path": write_markdown(payload, True),
                }
        elif args.correct_existing:
            profiles = ["live"]
            corrector = (
                correct_existing_daily
                if args.kind == "daily" else correct_existing_weekly
            )
            result = corrector(con, payload, profiles, args.apply)
            if args.kind == "daily" and args.apply and not args.no_markdown:
                daily_markdown_pending = True
            elif args.kind == "weekly" and not args.no_markdown:
                if result.get("targets"):
                    payload["trade_week_num"] = result["targets"][0].get(
                        "trade_week_num")
                weekly_markdown_pending = True
        elif payload.get("_mode") == "rewrite_null_and_renumber":
            result = rewrite_null_and_renumber(con, args.apply)
        else:
            writer = {"daily": write_daily, "weekly": write_weekly, "monthly": write_monthly}[args.kind]
            # demo 段写入随 2026-08-06 全量下线移除，只写 live。
            result = writer(con, payload, args.apply)
            if args.kind == "daily" and not args.no_markdown:
                if args.apply:
                    daily_markdown_pending = True
                else:
                    write_markdown(payload, False)
            elif args.kind == "weekly" and not args.no_markdown:
                payload["trade_week_num"] = result.get("trade_week_num")
                weekly_markdown_pending = True
            elif args.kind == "monthly" and not args.no_markdown:
                payload["trade_month_num"] = result.get("trade_month_num")
                monthly_markdown_pending = True
        if weekly_markdown_pending:
            result["markdown"] = _commit_then_write_weekly(
                con, payload, args.apply)
        elif monthly_markdown_pending:
            result["markdown"] = _commit_then_write_monthly(
                con, payload, args.apply)
        elif daily_markdown_pending:
            result["markdown"] = _commit_then_write_daily(
                con, payload, args.apply)
        else:
            con.commit()
    except Exception as e:
        con.rollback()
        fail(f"执行失败：{e}")
    finally:
        con.close()

    print(f"\n[result] {json.dumps(result, ensure_ascii=False, default=str)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
