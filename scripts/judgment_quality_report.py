# -*- coding: utf-8 -*-
"""周度判断质量报告（T11，2026-06-12）——P7 weekly 固定段落数据源。

输出 markdown 到 stdout，由周报 agent 原样嵌入推送（统一 QQ target）。四段:
  1. 六项决策卡完整率与历史经验取舍结果
  2. regime 判定 vs BTC 实际走势（近 7 天逐日复盘）
  3. 轮次可靠性（cron 运行成功率 / 丢轮 / provider 分布，读 openclaw.sqlite 只读）
  4. 推送健康（归档数量 / 劣化计数）

用法: pwsh ... run_okx_python.ps1 scripts/judgment_quality_report.py [--db-root <PROJECT_ROOT>\\db] [--days 7]
任何子段失败标 N/A 不中断。退出码恒 0（报告性质）。
"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

OPENCLAW_DB = os.environ.get(
    "OKX_OPENCLAW_STATE_DB",
    str(Path.home() / ".openclaw" / "state" / "openclaw.sqlite"),
)
# trader 统一由 dispatcher 完成触发，无独立 cron。
# 轮次可靠性/效率改聚合核心周期 cron。job_id 是 UUID、随 cron 重建会变——按 name 动态解析。
CYCLE_CRON_NAMES = ("okx-fast-collect", "okx-slow-collect", "okx-analyst-cron", "okx-dispatcher")
REPORTS_DIR = _project_path('reports', 'agents')


def ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def cycle_jobs(oc):
    """解析核心周期 cron 的 [(name, job_id), ...]（按 CYCLE_CRON_NAMES 顺序，缺失略过）。"""
    ph = ",".join("?" * len(CYCLE_CRON_NAMES))
    rows = oc.execute(f"SELECT name, job_id FROM cron_jobs WHERE name IN ({ph})",
                      CYCLE_CRON_NAMES).fetchall()
    order = {n: i for i, n in enumerate(CYCLE_CRON_NAMES)}
    return sorted(((r["name"], r["job_id"]) for r in rows), key=lambda x: order.get(x[0], 99))


def safe(title, fn):
    print(f"\n### {title}")
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        print(f"N/A（{type(e).__name__}: {str(e)[:60]}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    root, days = args.db_root, args.days

    print(f"## 判断质量周报 @ {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')} (UTC+8)")

    # 1) 六项卡与历史经验取舍
    def s_decision_cards():
        ana = ro(os.path.join(root, "analysis.db"))
        rows = ana.execute(
            "SELECT decision_card FROM analysis_signals "
            "WHERE cycle_id>=date('now','-30 days')"
        ).fetchall()
        ana.close()
        required = {
            "direction_evidence", "opposing_evidence", "execution_conditions",
            "invalidation_point", "risk_reward", "portfolio_impact",
        }
        cards = []
        for row in rows:
            try:
                card = json.loads(row["decision_card"]) if row["decision_card"] else None
            except (json.JSONDecodeError, TypeError):
                card = None
            if isinstance(card, dict):
                cards.append(card)
        complete = sum(required.issubset(card) for card in cards)
        print(f"决策卡 {len(cards)}/{len(rows)} 行；六项完整 {complete}/{len(cards) or 1}")

        acc = ro(os.path.join(root, "account.db"))
        exps = acc.execute(
            "SELECT pnl_pct,raw FROM trade_experiences "
            "WHERE status='closed' AND pnl_pct IS NOT NULL "
            "AND ts>=datetime('now','-30 days')"
        ).fetchall()
        acc.close()
        stats = {}
        for row in exps:
            try:
                raw = json.loads(row["raw"] or "{}")
                card = raw.get("decision_card") if isinstance(raw, dict) else None
                hist = card.get("historical_experience") if isinstance(card, dict) else None
                usage = str(hist.get("usage") or "none") if isinstance(hist, dict) else "legacy"
            except (json.JSONDecodeError, TypeError):
                usage = "legacy"
            bucket = stats.setdefault(usage, {"n": 0, "wins": 0, "pnl": 0.0})
            bucket["n"] += 1
            bucket["wins"] += row["pnl_pct"] > 0
            bucket["pnl"] += row["pnl_pct"]
        print("| 历史取舍 | 笔数 | 胜率 | 均收益 |")
        print("|---|---|---|---|")
        for usage, item in sorted(stats.items()):
            n = item["n"]
            print(f"| {usage} | {n} | {item['wins']/n:.0%} | {item['pnl']/n:+.2f}% |")
        print("以上仅用于复盘 Agent 的取舍质量，不形成未来交易阈值。")

    safe("六项决策卡与历史经验取舍（30 天）", s_decision_cards)

    # 2) regime vs BTC 实际
    def s_regime():
        # V2.0 (2026-06-26) Option A: regime 取 regime.db（已回填全历史，缺则回退 market.db）；
        # kline_cache（BTC 实际走势）仍只在 market.db —— 两源分开读。
        _rp = os.path.join(root, "regime.db")
        regdb = ro(_rp if os.path.exists(_rp) else os.path.join(root, "market.db"))
        mkt = ro(os.path.join(root, "market.db"))
        days_rows = regdb.execute(
            "SELECT substr(ts,1,10) AS d, regime, COUNT(*) AS n FROM cross_market "
            "WHERE ts>=datetime('now',?) AND regime IS NOT NULL "
            "GROUP BY d, regime", (f"-{days} days",)
        ).fetchall()
        dom = {}
        for r in days_rows:
            cur = dom.get(r["d"])
            if cur is None or r["n"] > cur[1]:
                dom[r["d"]] = (r["regime"], r["n"])
        btc = {r["ts"][:10]: r["c"] for r in mkt.execute(
            "SELECT ts, c FROM kline_cache WHERE symbol='BTC-USDT-SWAP' AND tf='1D' "
            "ORDER BY ts DESC LIMIT ?", (days + 2,)
        ).fetchall()}
        dates = sorted(btc.keys())
        chg = {dates[i]: (btc[dates[i]] - btc[dates[i - 1]]) / btc[dates[i - 1]] * 100
               for i in range(1, len(dates))}
        print("| 日期 | regime(当日主导) | BTC 当日 | 一致性 |")
        print("|---|---|---|---|")
        agree = total = 0
        for d in sorted(dom.keys()):
            reg = dom[d][0]
            c = chg.get(d)
            c_s = f"{c:+.2f}%" if c is not None else "?"
            if c is None or reg == "range":
                mark = "—"
            else:
                ok = (reg == "trend_down" and c < 0) or (reg == "trend_up" and c > 0)
                mark = "✓" if ok else "✗"
                agree += ok
                total += 1
            print(f"| {d} | {reg} | {c_s} | {mark} |")
        if total:
            print(f"方向一致率: {agree}/{total}（range 日不计；regime 是宏观滤镜非日内预测，仅供观察）")
        mkt.close()
        regdb.close()

    safe(f"regime vs BTC 实际（{days} 天）", s_regime)

    # 3) 轮次可靠性（聚合核心周期 cron：采集/分析/派单——trader 经 dispatcher 起无独立 cron）
    def s_rounds():
        oc = ro(OPENCLAW_DB)
        cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        jobs = cycle_jobs(oc)
        if not jobs:
            oc.close()
            print("无数据（未匹配到周期 cron——cron 可能已重建，核对 CYCLE_CRON_NAMES）")
            return
        ids = [j for _, j in jobs]
        ph = ",".join("?" * len(ids))
        rows = oc.execute(
            f"SELECT job_id, status, provider, COUNT(*) AS n FROM cron_run_logs "
            f"WHERE job_id IN ({ph}) AND ts>=? GROUP BY job_id, status, provider",
            (*ids, cutoff_ms)
        ).fetchall()
        oc.close()
        total = sum(r["n"] for r in rows)
        if not total:
            print(f"无数据（{days} 天内无周期 cron 运行记录）")
            return
        errs = sum(r["n"] for r in rows if r["status"] != "ok")
        provs, per_job = {}, {}
        for r in rows:
            if r["provider"]:
                provs[r["provider"]] = provs.get(r["provider"], 0) + r["n"]
            pj = per_job.setdefault(r["job_id"], [0, 0])
            pj[0] += r["n"]
            if r["status"] != "ok":
                pj[1] += r["n"]
        prov_s = (" ".join(f"{k}×{v}" for k, v in sorted(provs.items(), key=lambda x: -x[1]))
                  or "—（采集/派单为命令型，无 provider）")
        print(f"聚合 {len(jobs)} 个周期 cron | 总轮次 {total} | 失败 {errs}"
              f"（{errs / total:.1%}） | LLM provider: {prov_s}")
        print("| cron | 轮次 | 失败 | 失败率 |")
        print("|---|---|---|---|")
        for n, j in jobs:
            t, e = per_job.get(j, [0, 0])
            rate = f"{e / t:.1%}" if t else "—"
            print(f"| {n} | {t} | {e} | {rate} |")

    safe(f"轮次可靠性（{days} 天）", s_rounds)

    # 4) 推送健康
    def s_push():
        cutoff = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=days)).strftime("%Y%m%d")
        files = [f for f in os.listdir(REPORTS_DIR)
                 if f.startswith("v2-push-2") and f.endswith(".md")
                 and f[8:16] >= cutoff]
        sizes = [os.path.getsize(os.path.join(REPORTS_DIR, f)) for f in files]
        degraded = sum(1 for s in sizes if s < 300)
        print(f"归档 {len(files)} 份 | 劣化(<300B) {degraded} 份"
              + ("  ⚠️ 存在塌缩轮（查 push_pipeline 环节报告）" if degraded else " ✓"))

    safe(f"推送健康（{days} 天）", s_push)

    # 5) 效率（L5 2026-06-14）：单轮计费 token 趋势——session_context×工具往返的代理指标
    def s_efficiency():
        oc = ro(OPENCLAW_DB)
        cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        ids = [j for _, j in cycle_jobs(oc)]
        if not ids:
            oc.close()
            print("无数据（未匹配到周期 cron）")
            return
        ph = ",".join("?" * len(ids))
        toks = [r["total_tokens"] for r in oc.execute(
            f"SELECT total_tokens FROM cron_run_logs WHERE job_id IN ({ph}) AND ts>=? "
            f"AND total_tokens IS NOT NULL", (*ids, cutoff_ms)).fetchall()]
        oc.close()
        if not toks:
            print("（无 token 记录）")
            return
        avg = sum(toks) / len(toks)
        print(f"单轮计费 token：均 {avg/1e6:.2f}M / 峰 {max(toks)/1e6:.2f}M / 谷 {min(toks)/1e6:.2f}M（n={len(toks)}）")
        print("➤ 单轮 token≈session_context×工具往返；降它靠简报自足(L5)+session 适时轮换，趋势应平/降不应单调增")

    safe(f"效率·token（{days} 天）", s_efficiency)

    # 6) demo 周转率（N4 2026-06-14）：量化"ADJUST 僵持"——满仓微盈长持不交易
    def s_turnover():
        drl = ro(os.path.join(root, "drill.db"))
        opened = drl.execute("SELECT COUNT(*) c FROM drill_trades WHERE ts >= datetime('now', ?)",
                             (f"-{days} days",)).fetchone()["c"]
        closed = drl.execute("SELECT COUNT(*) c FROM drill_trades WHERE status='closed' "
                             "AND close_ts >= datetime('now', ?)", (f"-{days} days",)).fetchone()["c"]
        avg_hold = drl.execute(
            "SELECT AVG((julianday('now')-julianday(ts))*24) h FROM drill_trades WHERE status='open'"
        ).fetchone()["h"]
        drl.close()
        ah = f"{avg_hold:.0f}h" if avg_hold is not None else "—"
        print(f"demo {days} 天：开仓 {opened} / 平仓 {closed} 笔（日均开 {opened/days:.1f}）| 当前 open 平均持有 {ah}")
        print("➤ 周转过低（日均开<1 且持有>24h）= ADJUST 僵持/学习闭环停滞；周转健康才有新 pnl 归因")

    safe(f"demo 周转率（{days} 天）", s_turnover)

    print("\n（judgment_quality_report.py 生成，P7 weekly 原样嵌入周报）")


if __name__ == "__main__":
    main()
