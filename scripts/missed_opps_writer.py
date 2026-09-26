# -*- coding: utf-8 -*-
"""记录 Agent 未执行机会的后验表现，供后续决策卡参考。

背景：该表曾停更，导致压制策略缺少对照组——
「不开仓」的机会成本无人量化，压制经验只能自证。本脚本按日回填：

  取报告交易窗口整体前移4小时后的、已经完整成熟的24小时候选窗口中，
  decision_card_v1 **action=wait 且带方向** 的候选；
  无 decision_card 的兼容记录仍按 total/confidence 阈值读取，
  剔除同 cycle 同 symbol 已真实成交的行，严格要求连续16根15m kline，计算其后4h实际走幅与
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

2026-08-19T08:00 起新增第二来源 **briefing_layer_v1**（08-15 吞吐契约后
  unified 轮不再落 wait 行，本表唯一来源断流→对照组换到系统事实）：读
  decision_briefing 每轮追加的候选快照 logs/briefing/candidates-*.jsonl，
  每日候选窗内每 (symbol, direction) 只记**首次进入候选层**的一轮；窗口内该
  symbol 有任意成交视为已交互（caught）整组不记；notes 标
  source=briefing_layer_v1 与旧 wait 口径同表区分，触及率不与 08-14 前直接
  对比。断供监控同批迁移：激活后的窗口无任何快照 cycle 行 → [WARN]。

调度：reviewer 每日复盘（08:05）跑 `--as-of "<日报 ts>"`；也可手动补历史窗。
事实窗由日报窗确定性派生：日报交易窗 `[前一日08:00,当日08:00)` 对应已经
完整成熟的错失机会候选窗 `[前一日04:00,当日04:00)`；相邻日报连续平铺、无遗漏，
且每个候选在报告生成前都有完整4小时后验。按 cycle_id 半开过滤。
2026-07-31 前本脚本按自然日 `LIKE 'YYYY-MM-DD%'` 取数，与日报成交窗差 8 小时，
同一份日报里"错失机会"和"成交统计"覆盖不同时段；现已统一。
写库纪律：lessons.db writer=复盘链路，本脚本是该链路的确定性组件。
ts 写 CST 'YYYY-MM-DD HH:MM:SS'（禁 JobB-/UTC-Z 混入——该表 ts 已有历史混格式之痛）。

用法：
  pwsh ... missed_opps_writer.py --as-of "2026-07-31 08:05:00" [--dry-run]
  --date 2026-07-31   # 兼容入口：等价 --as-of 该日 08:05，窗口 [前一日 08:00, 当日 08:00)
  --date yesterday    # 等价 --as-of 今日 08:05
"""


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trade_report_stats  # noqa: E402  日报事实窗唯一定义源

sys.stdout.reconfigure(encoding="utf-8")

TOTAL_MIN = 45
CONF_MIN = 0.40
R_PCT = 2.0  # 兼容记录无失效距离时的后验评估兜底
OUTCOME_HOURS = 4
EXPECTED_15M_BARS = 16

# ── 实盘口径模拟（2026-08-28 首版 3×ATR/2R；2026-09-26 对照 V3 report::numbers 改口径） ──
# 固定 ±2% 代理与实盘不可比，故另有四列按实盘口径模拟。2026-09-26 起口径与 V3
# `sim_sl_pct` / `sim_outcome` 一致：
#   sim_stop_pct     = clamp(1×ATR14(1H)/入场价, 3%, 6%)；缺 ATR 用 4%（不再 no_data）
#   sim_tp_pct       = 固定 +5%
#   sim_outcome_24h  = hit_tp | hit_sl | ambiguous(同棒双触、先后不可知，不计入胜负)
#                      | neither(24h 内都没碰到) | no_data(入场K线缺失或24h覆盖<90%)
#   sim_first_touch_cst = 首触时刻（CST），neither/no_data 为 NULL
#   sim_rule         = 口径标签（SIM_RULE）；口径改动前已得出结论的行 rule 为 NULL、
#                      不重算——改了前后不可比（V3 同一纪律）
# ATR 按 V3 算法：判断时刻前 14 根已收盘 1H K 线的真实波幅均值；不足 15 根时退回
# kline_cache 存的 atr14；两者都没有用 4%。
# 写入时 24h 未成熟的行先记 no_data，由每次运行末尾的成熟回补通道
# （_mature_sim_backfill）在窗口成熟后重算——幂等、有界、无需改 cron。
SIM_ATR_MULT = 1.0
SIM_ATR_BARS = 14
SIM_SL_MIN_PCT = 3.0
SIM_SL_MAX_PCT = 6.0
SIM_SL_DEFAULT_PCT = 4.0
SIM_TP_PCT = 5.0
SIM_HOURS = 24
SIM_MIN_COVERAGE = 0.9
SIM_RULE = "v3_clamp_1xatr1h_3to6_tp5_h24"
SIM_COLUMNS = (
    ("sim_stop_pct", "REAL"),
    ("sim_tp_pct", "REAL"),
    ("sim_outcome_24h", "TEXT"),
    ("sim_first_touch_cst", "TEXT"),
    ("sim_rule", "TEXT"),
)


def sim_sl_pct(atr_pct_1h) -> float:
    """纯函数（V3 sim_sl_pct 移植，百分比单位）：clamp(1×ATR%, 3, 6)；缺 ATR → 4。"""
    try:
        atr = float(atr_pct_1h) if atr_pct_1h is not None else None
    except (TypeError, ValueError):
        atr = None
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return SIM_SL_DEFAULT_PCT
    return min(SIM_SL_MAX_PCT, max(SIM_SL_MIN_PCT, atr * SIM_ATR_MULT))


def sim_outcome(tp_index, sl_index) -> str:
    """纯函数（V3 sim_outcome 移植）：按先后到达的 bar 序判 hit_tp / hit_sl / ambiguous / neither。"""
    if tp_index is not None and sl_index is not None:
        if tp_index < sl_index:
            return "hit_tp"
        if tp_index > sl_index:
            return "hit_sl"
        return "ambiguous"
    if tp_index is not None:
        return "hit_tp"
    if sl_index is not None:
        return "hit_sl"
    return "neither"


def _atr_pct_1h(mkt, sym: str, t0_utcz: str, px0: float):
    """判断时刻前 14 根已收盘 1H 真实波幅均值 / 入场价（%）；退回存列 atr14；都无 → None。"""
    rows = mkt.execute(
        "SELECT h, l, c FROM kline_cache WHERE symbol=? AND tf='1H' AND ts<? "
        "ORDER BY ts DESC LIMIT ?",
        (sym, t0_utcz, SIM_ATR_BARS + 1),
    ).fetchall()
    rows = list(reversed(rows))
    if len(rows) == SIM_ATR_BARS + 1:
        ranges = []
        for index in range(1, len(rows)):
            try:
                hi, lo = float(rows[index][0]), float(rows[index][1])
                prev_close = float(rows[index - 1][2])
            except (TypeError, ValueError):
                ranges = []
                break
            if not all(math.isfinite(v) and v > 0 for v in (hi, lo, prev_close)):
                ranges = []
                break
            ranges.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        if len(ranges) == SIM_ATR_BARS:
            return sum(ranges) / SIM_ATR_BARS / px0 * 100.0
    stored = mkt.execute(
        "SELECT atr14 FROM kline_cache WHERE symbol=? AND tf='1H' "
        "AND ts<=? AND atr14 IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (sym, t0_utcz),
    ).fetchone()
    if stored and stored[0] is not None:
        try:
            atr = float(stored[0])
        except (TypeError, ValueError):
            return None
        if math.isfinite(atr) and atr > 0:
            return atr / px0 * 100.0
    return None


def _ensure_sim_columns(les) -> None:
    """幂等补列；lessons.db writer=复盘链路，本脚本是其确定性组件。"""
    have = {row[1] for row in les.execute(
        "PRAGMA table_info(missed_opportunities)")}
    for name, typ in SIM_COLUMNS:
        if name not in have:
            les.execute(
                f"ALTER TABLE missed_opportunities ADD COLUMN {name} {typ}")


def _open_lessons_database(path: str | Path, dry_run: bool):
    target = Path(path).resolve()
    if dry_run:
        con = sqlite3.connect(
            f"file:{target.as_posix()}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(target)
        _ensure_sim_columns(con)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _evaluate_sim(mkt, sym: str, slot_cst: str, direction: str):
    """V3 口径模拟：止损 clamp(1×ATR14(1H), 3%, 6%)、止盈 +5%、24h 内 15m 先触判定。

    返回 (stop_pct, tp_pct, outcome, first_touch_cst)。入场棒缺失 →
    (None, None, 'no_data', None)；同一根 15m 双触 'ambiguous'（不计入胜负）；
    未触及且 24h 覆盖 < SIM_MIN_COVERAGE → 'no_data'（不冒充 neither）。"""
    t0 = _utcz_from_cst(slot_cst)
    entry_row = mkt.execute(
        "SELECT o FROM kline_cache WHERE symbol=? AND tf='15m' AND ts=?",
        (sym, t0),
    ).fetchone()
    if not entry_row or not entry_row[0]:
        return None, None, "no_data", None
    px0 = float(entry_row[0])
    if not (math.isfinite(px0) and px0 > 0):
        return None, None, "no_data", None
    stop_pct = sim_sl_pct(_atr_pct_1h(mkt, sym, t0, px0))
    tp_pct = SIM_TP_PCT
    t24 = _utcz_from_cst(
        (datetime.strptime(slot_cst, "%Y-%m-%d %H:%M:%S")
         + timedelta(hours=SIM_HOURS)).strftime("%Y-%m-%d %H:%M:%S"))
    bars = mkt.execute(
        "SELECT ts, o, h, l, c FROM kline_cache WHERE symbol=? AND tf='15m' "
        "AND ts>=? AND ts<? ORDER BY ts",
        (sym, t0, t24),
    ).fetchall()
    if direction == "short":
        sl_px = px0 * (1 + stop_pct / 100.0)
        tp_px = px0 * (1 - tp_pct / 100.0)
    else:
        sl_px = px0 * (1 - stop_pct / 100.0)
        tp_px = px0 * (1 + tp_pct / 100.0)
    tp_index = sl_index = None
    touch = None
    for index, (ts_, _o, h, l, _c) in enumerate(bars):
        try:
            hi, lo = float(h), float(l)
        except (TypeError, ValueError):
            continue
        if direction == "short":
            sl_hit, tp_hit = hi >= sl_px, lo <= tp_px
        else:
            sl_hit, tp_hit = lo <= sl_px, hi >= tp_px
        if tp_hit and tp_index is None:
            tp_index = index
        if sl_hit and sl_index is None:
            sl_index = index
        if tp_hit or sl_hit:
            touch = ts_
            break
    outcome = sim_outcome(tp_index, sl_index)
    if outcome == "neither" and len(bars) < SIM_HOURS * 4 * SIM_MIN_COVERAGE:
        outcome = "no_data"
    touch_cst = None
    if touch:
        touch_cst = (
            datetime.strptime(str(touch), "%Y-%m-%dT%H:%M:%SZ")
            + timedelta(hours=8)
        ).strftime("%Y-%m-%d %H:%M:%S")
    return round(stop_pct, 4), round(tp_pct, 4), outcome, touch_cst


# 旧名兼容（2026-08-28 首版）；口径已随 SIM_RULE 变更。
_evaluate_sim_2r_atr = _evaluate_sim


def _mature_sim_backfill(les, mkt, now_cst: datetime | None = None):
    """成熟回补：sim 列为空/no_data 且 24h 窗已成熟的行按当前口径重算并 UPDATE。

    幂等有界：只扫 briefing_layer_v1 激活边界之后、ts ≤ now-25h 的行。
    重扫集合 = 从未评估（outcome NULL）∪ 记过 no_data 但还没按当前口径
    （sim_rule）评估过的行；当前口径下仍 no_data 的行（入场 K 线永久缺失）
    不再重扫。已得出结论的旧口径行冻结不动。返回 (评估行数, 得出结论行数)。"""
    now = now_cst or datetime.now()
    matured_before = (now - timedelta(hours=SIM_HOURS + 1)).strftime(
        "%Y-%m-%d %H:%M:%S")
    rows = les.execute(
        "SELECT id, ts, symbol, direction_hint FROM missed_opportunities "
        "WHERE ts >= ? AND ts <= ? AND (sim_outcome_24h IS NULL "
        "OR (sim_outcome_24h = 'no_data' AND COALESCE(sim_rule,'') <> ?))",
        (BRIEFING_SOURCE_ACTIVATION_CYC.replace("T", " ") + ":00",
         matured_before, SIM_RULE),
    ).fetchall()
    evaluated = concluded = 0
    for rid, ts, sym, direction in rows:
        d = direction if direction in ("long", "short") else "long"
        stop_pct, tp_pct, outcome, touch = _evaluate_sim(mkt, sym, str(ts), d)
        les.execute(
            "UPDATE missed_opportunities SET sim_stop_pct=?, sim_tp_pct=?, "
            "sim_outcome_24h=?, sim_first_touch_cst=?, sim_rule=? WHERE id=?",
            (stop_pct, tp_pct, outcome, touch, SIM_RULE, rid),
        )
        evaluated += 1
        if outcome not in (None, "no_data"):
            concluded += 1
    return evaluated, concluded

# ── 2026-08-18 briefing_layer_v1 第二来源（预注册前向边界，只向前） ────────
# 背景：2026-08-15 吞吐契约后 unified 轮 signals=最终开仓短名单，analysis_signals
# 不再有 wait 行 → 本表唯一来源断流、对照组前向致盲（08-15~边界间空窗如实留白，
# 不回补）。第二来源改读 decision_briefing 每轮追加的候选快照
# logs/briefing/candidates-YYYYMMDD.jsonl（成熟趋势/早期结构两层，系统事实）。
# 口径（主人 2026-08-18 拍板）：每日候选窗内每 (symbol, direction) 只记
# **首次进入候选层**的那一轮（量级贴近历史 wait 口径）；窗口内该 symbol 有任意
# 成交即视为已交互（caught），整组不记。与旧 wait 口径同库同列不分表，
# notes 标 source=briefing_layer_v1 区分；触及率不与 08-14 前旧口径直接对比。
BRIEFING_SOURCE_ACTIVATION_CYC = "2026-08-19T08:00"
BRIEFING_SOURCE_TAG = "briefing_layer_v1"
# Start the new evidence source at the next complete 04:00 producer bucket.
# The partial 13:15→04:00 transition remains historical SOURCE_LAG; never
# backfill it under a changed denominator merely to release today's report.
BRIEFING_SIDE_NEUTRAL_ACTIVATION_CYC = "2026-09-03T04:00"
BRIEFING_SIDE_NEUTRAL_SOURCE_TAG = "briefing_symbol_review_v2"


def _evaluate_outcome(mkt, sym: str, slot_cst: str, direction: str):
    """镜像主循环的 4h 后验计算（精确连续 16 根 15m；固定 ±2% 代理口径）。

    与主循环内联计算必须同步演进（改任一处必须同批改另一处）。
    返回 (ok, actual_pct, hit_1r)；ok=False 表示 K 线不完整。"""
    t0 = _utcz_from_cst(slot_cst)
    t4 = _utcz_from_cst(
        (datetime.strptime(slot_cst, "%Y-%m-%d %H:%M:%S")
         + timedelta(hours=OUTCOME_HOURS)).strftime("%Y-%m-%d %H:%M:%S"))
    rows = mkt.execute(
        "SELECT ts, o, h, l, c FROM kline_cache WHERE symbol=? AND tf='15m' "
        "AND ts>=? AND ts<? ORDER BY ts",
        (sym, t0, t4),
    ).fetchall()
    if not _complete_four_hour_rows(rows, t0):
        return False, None, None
    px0 = rows[0][1]
    close4 = rows[-1][4]
    hi = max(r[2] for r in rows)
    lo = min(r[3] for r in rows)
    if direction == "short":
        actual = (px0 - close4) / px0 * 100.0
        hit_1r = 1 if (px0 - lo) / px0 * 100.0 >= R_PCT else 0
    else:
        actual = (close4 - px0) / px0 * 100.0
        hit_1r = 1 if (hi - px0) / px0 * 100.0 >= R_PCT else 0
    return True, actual, hit_1r


def _briefing_layer_first_seen(root: str, start_cyc: str, end_cyc: str):
    """读窗口内候选快照 → (观察到的cycle数, {(symbol,side): (首现cycle, layer)})。

    快照格式/去重契约唯一事实源=decision_briefing.load_candidate_snapshots
    （同 cycle 重复行取首行）。只取 cycle ≥ 激活边界且落在候选窗内的行。"""
    import decision_briefing as _brief
    dates = sorted({start_cyc[:10], end_cyc[:10]})
    cycles = _brief.load_candidate_snapshots(root, dates)
    in_window = {
        c: m for c, m in cycles.items()
        if start_cyc <= c < end_cyc and c >= BRIEFING_SOURCE_ACTIVATION_CYC
    }
    # From the side-neutral policy epoch onward the briefing snapshot proves
    # only which symbols were reviewed.  Direction authority lives exclusively
    # in the same-cycle analysis signal; eligible_sides=[long,short] must never
    # be expanded into two hindsight opportunities.
    analysis_path = Path(root) / "analysis.db"
    if analysis_path.exists() and end_cyc > BRIEFING_SIDE_NEUTRAL_ACTIVATION_CYC:
        con = sqlite3.connect(
            f"file:{analysis_path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            run_status = {
                str(row["cycle_id"]): str(row["status"] or "").lower()
                for row in con.execute(
                    "SELECT cycle_id,status FROM analysis_runs "
                    "WHERE cycle_id>=? AND cycle_id<?",
                    (start_cyc, end_cyc),
                )
            }
            signals: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for row in con.execute(
                "SELECT cycle_id,symbol,action,side FROM analysis_signals "
                "WHERE cycle_id>=? AND cycle_id<? "
                "AND action IN ('open_long','open_short')",
                (start_cyc, end_cyc),
            ):
                signals[str(row["cycle_id"])].append(row)
        finally:
            con.close()

        for date in dates:
            path = Path(root).resolve().parent / "logs" / "briefing" / (
                f"candidates-{date.replace('-', '')}.jsonl")
            if not path.exists():
                continue
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                try:
                    payload = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError):
                    continue
                cyc = str(payload.get("cycle_id") or "")
                if not (
                    BRIEFING_SIDE_NEUTRAL_ACTIVATION_CYC <= cyc < end_cyc
                    and start_cyc <= cyc
                    and run_status.get(cyc) == "ok"
                ):
                    continue
                reviewed = {
                    str(item.get("symbol") or "")
                    for item in (payload.get("candidates") or [])
                    if isinstance(item, dict)
                    and item.get("layer") == "all_market"
                    and item.get("side") is None
                    and set(item.get("eligible_sides") or [])
                    == {"long", "short"}
                    and item.get("selected_for_review") is True
                }
                if not reviewed:
                    continue
                mapped: dict[str, dict] = {}
                for row in signals.get(cyc, []):
                    symbol = str(row["symbol"] or "")
                    action = str(row["action"] or "")
                    side = str(row["side"] or "").lower()
                    expected_side = (
                        "long" if action == "open_long" else
                        "short" if action == "open_short" else "")
                    if (
                        symbol not in reviewed
                        or side != expected_side
                        or symbol in mapped
                    ):
                        continue
                    mapped[symbol] = {
                        "side": side,
                        "layer": "all_market",
                        "source_tag": BRIEFING_SIDE_NEUTRAL_SOURCE_TAG,
                    }
                in_window[cyc] = mapped
    first_seen: dict = {}
    for cyc in sorted(in_window):
        for sym, entry in in_window[cyc].items():
            key = (sym, entry["side"])
            if key not in first_seen:
                first_seen[key] = (
                    cyc,
                    entry.get("layer"),
                    entry.get("source_tag") or BRIEFING_SOURCE_TAG,
                )
    return len(in_window), first_seen


def _utcz_from_cst(cst_str: str) -> str:
    dt = datetime.strptime(cst_str, "%Y-%m-%d %H:%M:%S") - timedelta(hours=8)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _matured_candidate_window(
    report_window_start: str,
    report_window_end: str,
) -> tuple[str, str]:
    """Shift the whole report window back by the fixed outcome horizon."""
    start = datetime.strptime(report_window_start, "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(report_window_end, "%Y-%m-%d %H:%M:%S")
    shift = timedelta(hours=OUTCOME_HOURS)
    return (
        (start - shift).strftime("%Y-%m-%d %H:%M:%S"),
        (end - shift).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _parse_utc_timestamp(value: str) -> datetime:
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _complete_four_hour_rows(rows: list[tuple], start_utc: str) -> bool:
    """Accept exact consecutive starts and valid positive OHLC in the 4H horizon."""
    if len(rows) != EXPECTED_15M_BARS:
        return False
    try:
        start = _parse_utc_timestamp(start_utc)
        observed = [_parse_utc_timestamp(row[0]) for row in rows]
    except (TypeError, ValueError):
        return False
    expected = [start + timedelta(minutes=15 * i)
                for i in range(EXPECTED_15M_BARS)]
    if observed != expected:
        return False
    for row in rows:
        try:
            open_px, high_px, low_px, close_px = map(float, row[1:5])
        except (TypeError, ValueError):
            return False
        if not all(
            math.isfinite(value) and value > 0
            for value in (open_px, high_px, low_px, close_px)
        ):
            return False
        if high_px < max(open_px, low_px, close_px):
            return False
        if low_px > min(open_px, high_px, close_px):
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", dest="as_of",
                    help="日报 ts（CST）；窗口取 [前一日 08:00, 当日 08:00)")
    ap.add_argument("--date", default=None,
                    help="兼容入口：YYYY-MM-DD 或 'yesterday'，等价 --as-of 该日 08:05")
    ap.add_argument("--db-root", default=_public_project_path('db'))
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
    report_start_ts, report_end_ts = trade_report_stats.daily_window(as_of)
    start_ts, end_ts = _matured_candidate_window(
        report_start_ts, report_end_ts)
    # cycle_id 是 'YYYY-MM-DDTHH:MM'，字典序即时序；候选窗整体前移4小时，
    # 保证相邻日报仍连续平铺且所有候选已有完整后验。
    start_cyc = start_ts[:16].replace(" ", "T")
    end_cyc = end_ts[:16].replace(" ", "T")
    print(
        f"[window] matured candidate cycle_id ∈ [{start_cyc}, {end_cyc}) "
        f"(report=[{report_start_ts}, {report_end_ts}), as-of {as_of})"
    )
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
    # briefing_layer_v1 用：窗口内成交过的 symbol 全集（任意成交=已交互）。
    traded_syms = {sym for (_cyc, sym) in exe}

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
    # briefing_layer_v1 用：cycle→regime（失败轮缺行时取窗口内最近的前一轮）。
    regime_by_cycle = dict(ana.execute(
        "SELECT cycle_id, regime FROM analysis_runs "
        "WHERE cycle_id >= ? AND cycle_id < ?", (start_cyc, end_cyc)))
    ana.close()

    les = _open_lessons_database(
        Path(root) / "lessons.db", args.dry_run)
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
            if not _complete_four_hour_rows(rows, t0):
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

        # ── briefing_layer_v1 第二来源（2026-08-19T08:00 起前向） ──────────
        b_written = b_skipped = b_nodata = b_caught = 0
        b_cycles = 0
        b_note = ""
        if end_cyc > BRIEFING_SOURCE_ACTIVATION_CYC:
            try:
                b_cycles, first_seen = _briefing_layer_first_seen(
                    root, start_cyc, end_cyc)
            except Exception as exc:  # noqa: BLE001 - 读快照失败照实外显
                b_cycles, first_seen = 0, {}
                b_note = f" read_error={type(exc).__name__}"
            for (sym, side), first_seen_fact in sorted(
                    first_seen.items(), key=lambda kv: kv[1][0]):
                cyc, layer = first_seen_fact[:2]
                source_tag = (
                    first_seen_fact[2]
                    if len(first_seen_fact) > 2 else BRIEFING_SOURCE_TAG)
                if sym in traded_syms:
                    b_caught += 1
                    continue
                slot_cst = cyc.replace("T", " ") + ":00"
                if les.execute(
                    "SELECT 1 FROM missed_opportunities "
                    "WHERE ts=? AND symbol=? AND direction_hint=?",
                    (slot_cst, sym, side),
                ).fetchone():
                    b_skipped += 1
                    continue
                ok, actual, hit_1r = _evaluate_outcome(mkt, sym, slot_cst, side)
                if not ok:
                    b_nodata += 1
                    continue
                regime = regime_by_cycle.get(cyc)
                if regime is None and regime_by_cycle:
                    earlier = [c for c in regime_by_cycle if c <= cyc]
                    if earlier:
                        regime = regime_by_cycle[max(earlier)]
                note = (
                    f"cycle={cyc} source={source_tag} "
                    f"layer={layer or '?'} 候选未成交；risk_pct={R_PCT:.3f}；"
                    "每日每(symbol,direction)首现；激活边界"
                    f"{BRIEFING_SOURCE_ACTIVATION_CYC}"
                )
                if not args.dry_run:
                    les.execute(
                        "INSERT INTO missed_opportunities"
                        "(ts, symbol, score, regime, direction_hint, actual_4h_pct, "
                        "would_hit_1r_fixed2pct, notes, reviewed_utc, decision_card)"
                        "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)",
                        (slot_cst, sym, 0, regime, side, round(actual, 3),
                         hit_1r, note, None),
                    )
                b_written += 1
        # ── 2026-08-28 实盘口径模拟：成熟回补通道（幂等有界，无独立 cron） ──
        sim_evaluated = sim_concluded = 0
        if not args.dry_run:
            sim_evaluated, sim_concluded = _mature_sim_backfill(les, mkt)
            les.commit()
    finally:
        les.close()
        mkt.close()

    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}ok window=[{start_cyc}, {end_cyc}) wait_signals={wait_total} "
          f"directional={wait_directional} candidates={selected} "
          f"written={written} dup_skipped={skipped} no_kline={nodata}")
    print(f"{tag}briefing_layer_v1 cycles={b_cycles} pairs_written={b_written} "
          f"caught={b_caught} dup_skipped={b_skipped} no_kline={b_nodata}"
          f"{b_note}")
    print(f"{tag}sim({SIM_RULE}) matured_evaluated={sim_evaluated} "
          f"concluded={sim_concluded}")
    if wait_total > 0 and wait_directional == 0:
        # 静默写 0 正是 2026-07-29~31 对照组断供两天没被发现的原因，必须发声。
        print(
            f"[WARN] 窗口内 {wait_total} 条 wait 信号无一带方向（side 全为 null）→ "
            "错失机会对照组本轮无输入。分析侧 action=wait 应在能判方向时填 "
            "side=long|short（见 agents/analyst.md action/side 契约）。",
            file=sys.stderr,
        )
    # 2026-08-18 断供监控迁移：08-15 后 wait 行恒为 0，旧监控（有 wait 无方向）
    # 永不触发——正是本次断源三天无报警的原因。新监控盯采集事实本身：
    # 激活边界之后的窗口若无任何候选快照 cycle 行，必须发声。
    if (
        end_cyc > BRIEFING_SOURCE_ACTIVATION_CYC
        and start_cyc >= BRIEFING_SOURCE_ACTIVATION_CYC
        and b_cycles == 0
    ):
        print(
            "[WARN] 窗口内无 briefing 候选快照（logs/briefing/candidates-*.jsonl "
            "缺失或空）→ briefing_layer_v1 对照组断供。核查 dispatcher 是否带 "
            "--cycle-id 派发预读、decision_briefing 是否正常运行。",
            file=sys.stderr,
        )
    if b_nodata:
        print(
            f"[WARN] briefing_layer_v1 有 {b_nodata} 个已成熟候选缺少精确连续"
            f"{EXPECTED_15M_BARS}根15m K线（该源为流动性闸内标的，缺K线值得核查；"
            "本源不因此非零退出）。",
            file=sys.stderr,
        )
    if nodata:
        print(
            f"[ERROR] {nodata} 个已成熟候选缺少精确连续{EXPECTED_15M_BARS}根15m K线",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
