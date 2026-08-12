# -*- coding: utf-8 -*-
"""记录 Agent 未执行机会的后验表现，供后续决策卡参考。

背景：该表曾停更，导致压制策略缺少对照组——
「不开仓」的机会成本无人量化，压制经验只能自证。本脚本按日回填：

  取窗口内 decision_card_v1 中 **action=wait 且带方向** 的候选；
  无 decision_card 的兼容记录仍按 total/confidence 阈值读取，
  剔除同 cycle 同 symbol 已真实成交的行，按 15m kline 计算其后 4h 实际走幅与
  would_hit_1r_fixed2pct（**固定 ±2% 代理口径**：-2% SL 的对称目标 +2%；与候选
  真实计划止损无关，列名 2026-08-10 由 would_hit_1R 更名以杜绝口径混用；side
  缺失按 long 惯例并在 notes 标注），幂等写入 missed_opportunities（同 ts+symbol
  已存在则跳过）。

为什么只取 wait（2026-07-31 主人拍板）：
  hold = 持有既有仓位，没有「本可入场却没入」的对照意义，方向恒为 null；
  wait = 看到方向但本轮不入场，正是机会成本要量化的对象。
  历史上 hold 曾带方向并贡献了约 3/4 的样本，那是旧契约下的语义混用，不再沿用。

停写监控：窗口内存在 wait 信号却无一带方向 = 分析侧没在履行契约，
本脚本会打 [WARN] 而不是静默写 0 —— 2026-07-29~31 的两天空窗就是这么被漏掉的。

调度：reviewer 每日复盘（08:05）跑 `--as-of "<日报 ts>"`；也可手动补历史窗。
事实窗与日报同源：`trade_report_stats.daily_window` 给出 `[前一日 08:00, 当日 08:00)`，
按 cycle_id 半开过滤（cycle_id 为 'YYYY-MM-DDTHH:MM'，字典序即时序）。
2026-07-31 前本脚本按自然日 `LIKE 'YYYY-MM-DD%'` 取数，与日报成交窗差 8 小时，
同一份日报里"错失机会"和"成交统计"覆盖不同时段；现已统一。
写库纪律：lessons.db writer=复盘链路，本脚本是该链路的确定性组件。
ts 写 CST 'YYYY-MM-DD HH:MM:SS'（禁 JobB-/UTC-Z 混入——该表 ts 已有历史混格式之痛）。

用法：
  pwsh ... missed_opps_writer.py --as-of "2026-07-31 08:05:00" [--dry-run]
  --date 2026-07-31   # 兼容入口：等价 --as-of 该日 08:05，窗口 [前一日 08:00, 当日 08:00)
  --date yesterday    # 等价 --as-of 今日 08:05
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trade_report_stats  # noqa: E402  日报事实窗唯一定义源

sys.stdout.reconfigure(encoding="utf-8")

TOTAL_MIN = 45
CONF_MIN = 0.40
R_PCT = 2.0  # 兼容记录无失效距离时的后验评估兜底


def _utcz_from_cst(cst_str: str) -> str:
    dt = datetime.strptime(cst_str, "%Y-%m-%d %H:%M:%S") - timedelta(hours=8)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", dest="as_of",
                    help="日报 ts（CST）；窗口取 [前一日 08:00, 当日 08:00)")
    ap.add_argument("--date", default=None,
                    help="兼容入口：YYYY-MM-DD 或 'yesterday'，等价 --as-of 该日 08:05")
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.as_of:
        as_of = args.as_of
    else:
        anchor_day = (
            datetime.now().strftime("%Y-%m-%d")
            if (args.date or "yesterday") == "yesterday" else args.date
        )
        as_of = f"{anchor_day} 08:05:00"
    start_ts, end_ts = trade_report_stats.daily_window(as_of)
    # cycle_id 是 'YYYY-MM-DDTHH:MM'，字典序即时序，半开区间与日报成交窗一致。
    start_cyc = start_ts[:16].replace(" ", "T")
    end_cyc = end_ts[:16].replace(" ", "T")
    print(f"[window] cycle_id ∈ [{start_cyc}, {end_cyc})  (as-of {as_of})")
    root = args.db_root

    ana = sqlite3.connect(f"file:{root}\\analysis.db?mode=ro", uri=True)
    mkt = sqlite3.connect(f"file:{root}\\market.db?mode=ro", uri=True)
    exe = {}  # (cycle_id, symbol) 已成交集合
    # 2026-08-06 demo 全量下线：原先还扫 demo_trades.db。错失机会的判定是
    # 「分析给了 wait 而实际没成交」，demo 成交曾算作「抓住了」——demo 停跑后
    # 那个来源恒为空，留着只会在库删除后抛 unable to open database file。
    con = sqlite3.connect(f"file:{root}\\live_trades.db?mode=ro", uri=True)
    for cyc, sym in con.execute(
        "SELECT cycle_id, symbol FROM trades "
        "WHERE cycle_id >= ? AND cycle_id < ?", (start_cyc, end_cyc)
    ):
        exe[(cyc, sym)] = True
    con.close()

    cands = ana.execute(
        "SELECT cycle_id, symbol, total, confidence, side, decision_card, regime.regime "
        "FROM analysis_signals "
        "JOIN (SELECT cycle_id AS c2, regime FROM analysis_runs) regime ON regime.c2 = analysis_signals.cycle_id "
        "WHERE analysis_signals.cycle_id >= ? AND analysis_signals.cycle_id < ? "
        "AND action = 'wait'",
        (start_cyc, end_cyc),
    ).fetchall()
    # 契约健康度：窗口内有 wait 却全无方向 = 分析侧没填 side，对照组会静默断供。
    wait_total = len(cands)
    wait_directional = sum(
        1 for row in cands if str(row[4] or "").lower() in ("long", "short")
    )
    ana.close()

    les = sqlite3.connect(f"{root}\\lessons.db")
    les.execute("PRAGMA busy_timeout=5000")
    written = skipped = nodata = 0
    try:
        selected = 0
        for cyc, sym, total, conf, side, card_raw, regime in cands:
            try:
                card = json.loads(card_raw) if card_raw else None
            except (json.JSONDecodeError, TypeError):
                card = None
            is_card = isinstance(card, dict)
            if is_card:
                # 没有可检验方向的纯持仓观察不记为错失机会。
                if side not in ("long", "short"):
                    continue
            elif not (
                (total is not None and total >= TOTAL_MIN)
                or (conf is not None and conf >= CONF_MIN)
            ):
                continue
            selected += 1
            if (cyc, sym) in exe:
                continue
            slot_cst = cyc.replace("T", " ") + ":00"  # 'YYYY-MM-DDTHH:MM' -> CST ts
            if les.execute(
                "SELECT 1 FROM missed_opportunities WHERE ts=? AND symbol=?", (slot_cst, sym)
            ).fetchone():
                skipped += 1
                continue
            t0 = _utcz_from_cst(slot_cst)
            t4 = _utcz_from_cst(
                (datetime.strptime(slot_cst, "%Y-%m-%d %H:%M:%S") + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
            )
            rows = mkt.execute(
                "SELECT ts, o, h, l, c FROM kline_cache WHERE symbol=? AND tf='15m' "
                "AND ts>=? AND ts<? ORDER BY ts",
                (sym, t0, t4),
            ).fetchall()
            if len(rows) < 8:  # 4h 窗至少要 8 根 15m 才算数据可用
                nodata += 1
                continue
            px0 = rows[0][1]
            close4 = rows[-1][4]
            hi = max(r[2] for r in rows)
            lo = min(r[3] for r in rows)
            direction = side if side in ("long", "short") else "long"
            # 2026-08-10 r-semantics：恒用固定 2% 代理口径，列名 would_hit_1r_fixed2pct
            # 才始终为真。旧的"卡上 risk_pct 覆盖"分支已删——它混入过小数比例
            # （0.025 被当 0.025% 用，阈值缩小 100 倍恒判 1）且让列名对那些行撒谎；
            # 按真实计划止损的 1R 属 Wave1 EV 计算器，另立字段。
            risk_pct = R_PCT
            if direction == "short":
                actual = (px0 - close4) / px0 * 100.0
                hit_1r = 1 if (px0 - lo) / px0 * 100.0 >= risk_pct else 0
            else:
                actual = (close4 - px0) / px0 * 100.0
                hit_1r = 1 if (hi - px0) / px0 * 100.0 >= risk_pct else 0
            note = (
                f"cycle={cyc} 未执行候选；risk_pct={risk_pct:.3f}"
                + ("；decision_card_v1" if is_card else f"；兼容格式 total={total} conf={conf}")
                + ("；side 缺失按 long 惯例" if side not in ("long", "short") else "")
            )
            if not args.dry_run:
                les.execute(
                    "INSERT INTO missed_opportunities"
                    "(ts, symbol, score, regime, direction_hint, actual_4h_pct, "
                    "would_hit_1r_fixed2pct, notes, reviewed_utc, decision_card)"
                    "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)",
                    (slot_cst, sym, total if total is not None else 0, regime,
                     direction, round(actual, 3), hit_1r, note, card_raw),
                )
            written += 1
        if not args.dry_run:
            les.commit()
    finally:
        les.close()
        mkt.close()

    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}ok window=[{start_cyc}, {end_cyc}) wait_signals={wait_total} "
          f"directional={wait_directional} candidates={selected} "
          f"written={written} dup_skipped={skipped} no_kline={nodata}")
    if wait_total > 0 and wait_directional == 0:
        # 静默写 0 正是 2026-07-29~31 对照组断供两天没被发现的原因，必须发声。
        print(
            f"[WARN] 窗口内 {wait_total} 条 wait 信号无一带方向（side 全为 null）→ "
            "错失机会对照组本轮无输入。分析侧 action=wait 应在能判方向时填 "
            "side=long|short（见 agents/analyst.md action/side 契约）。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
