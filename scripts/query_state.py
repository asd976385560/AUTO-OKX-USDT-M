# -*- coding: utf-8 -*-
"""
query_state.py — V2.0 数据校验聚合查询
================================================================
背景：
  早期 agent 曾在 P3 数据校验阶段 ad-hoc 调 sqlite3 / heredoc
  - PowerShell `-File` 不支持 heredoc → ParserError
  - sqlite3.exe 不在 PATH → "term not recognized"
  - 反复写临时 .py 猜 column 名（usdt_dxy/btc_dom 实际不存在）→ 1-2 分钟/轮
  - P3 标 "CLI blocked" → P4 IDLE → 5+ 小时不交易
治本：
  P3 全部检查封装成这个脚本，agent 永远走 run_okx_python.ps1 包装。
  sqlite3 CLI 路径（C:\\ProgramData\\chocolatey\\bin\\sqlite3.exe）仅留作 ad-hoc 排查。

调用：
  pwsh -NoProfile -File <PROJECT_ROOT>\\scripts\\run_okx_python.ps1 ^
      <PROJECT_ROOT>\\scripts\\query_state.py --check all --db-root <PROJECT_ROOT>\\db
  pwsh -NoProfile -File <PROJECT_ROOT>\\scripts\\run_okx_python.ps1 ^
      <PROJECT_ROOT>\\scripts\\query_state.py --check regime --db-root <PROJECT_ROOT>\\db --json

参数：
  --check {all|tickers|regime|news|account|kline|volume_anomaly|degraded|cycle_fresh|playbook|lost_cycles}
         all = 跑全部可用检查
  --db-root <PROJECT_ROOT>\\db   (硬编码默认)
  --stale-min 15          (新鲜度阈值分钟；FRESH<10 / STALE 10-15 / STALE+ >15)
  --hh01-only             (regime HH:01 必检；非 HH:01 复制 cross_market 最新行视为合规)
  --json                  (输出 JSON 而非 text，给脚本/parse 用)

退出码：
  0 = 全部 PASS/WARN
  1 = 任一 FAIL
  2 = 执行错误（DB 不可读等）
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

from _db_ro import connect_ro

CST = timezone(timedelta(hours=8))


def now_cst() -> datetime:
    return datetime.now(CST)


def parse_utc_iso(s):
    """'2026-06-05T17:16:24Z' → aware UTC datetime"""
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z") and "T" in s:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).astimezone(timezone.utc)
    except Exception:
        return None


def fmt_age_minutes(utc_dt):
    if not utc_dt:
        return None
    return round((datetime.now(timezone.utc) - utc_dt).total_seconds() / 60.0, 1)


def safe_connect(path):
    if not os.path.exists(path):
        return None
    try:
        return connect_ro(path)  # 只读 mode=ro（2026-07-03：防可写打开静默建 0 字节假库）
    except Exception as e:
        return e  # caller 检查


def check_tickers(db_root, stale_min, hh01_only, results):
    """P3-1: tickers count + freshness"""
    market = os.path.join(db_root, "market.db")
    con = safe_connect(market)
    if not con or isinstance(con, Exception):
        results.append({"name": "tickers", "status": "FAIL", "msg": f"market.db 不可读: {con}"})
        return
    try:
        r = con.execute(
            "SELECT ts, COUNT(DISTINCT symbol) FROM tick_snapshots "
            "WHERE ts = (SELECT MAX(ts) FROM tick_snapshots) GROUP BY ts"
        ).fetchone()
        if not r:
            results.append({"name": "tickers", "status": "FAIL", "msg": "tick_snapshots 为空"})
            return
        ts_raw, count = r
        age = fmt_age_minutes(parse_utc_iso(ts_raw))
        if count < 300:
            status, msg = "FAIL", f"{count} symbols (threshold 300)"
        elif age is not None and age > stale_min:
            status, msg = "WARN", f"{count} symbols @ {ts_raw} age={age}m > {stale_min}m"
        else:
            status, msg = "PASS", f"{count} symbols @ {ts_raw} (age={age}m, threshold 300)"
        results.append({"name": "tickers", "status": status, "msg": msg, "count": count, "ts": ts_raw, "age_min": age})
    finally:
        con.close()


def check_kline(db_root, stale_min, hh01_only, results):
    """P3-2: kline freshness（任意 ts 有数据即可）"""
    market = os.path.join(db_root, "market.db")
    con = safe_connect(market)
    if not con or isinstance(con, Exception):
        results.append({"name": "kline", "status": "FAIL", "msg": f"market.db 不可读: {con}"})
        return
    try:
        r = con.execute(
            "SELECT MAX(ts), COUNT(*) FROM kline_cache"
        ).fetchone()
        if not r or not r[0]:
            results.append({"name": "kline", "status": "WARN", "msg": "kline_cache 为空（非 HH:01 轮次可能正常）"})
            return
        ts_raw, count = r
        age = fmt_age_minutes(parse_utc_iso(ts_raw))
        # kline 主要在 HH:01 写，非 HH:01 视为合规
        now = now_cst()
        if now.hour == 1 and now.minute < 30:
            if age is None or age > 120:
                status = "FAIL"
                msg = f"HH:01 轮 kline 滞后 {age}m > 120m"
            else:
                status, msg = "PASS", f"{count} rows, age={age}m"
        else:
            status, msg = "PASS", f"{count} rows @ {ts_raw} (非 HH:01 轮次，跳过滞后校验)"
        results.append({"name": "kline", "status": status, "msg": msg, "ts": ts_raw, "age_min": age})
    finally:
        con.close()


def check_regime(db_root, stale_min, hh01_only, results):
    """P3-3: cross_market.regime 非空 + 新鲜度
    （V2.0 2026-06-26：regime.db 优先、market.db 按 ts 兜底——见 scripts/_regime_read）"""
    try:
        import sys as _sys
        _sd = os.path.dirname(os.path.abspath(__file__))
        if _sd not in _sys.path:
            _sys.path.insert(0, _sd)
        from _regime_read import latest_cross_market, latest_source
    except Exception as e:
        results.append({"name": "regime", "status": "FAIL", "msg": f"_regime_read 不可用: {e}"})
        return
    row = latest_cross_market(db_root)
    if not row:
        results.append({"name": "regime", "status": "FAIL", "msg": "cross_market 表为空（regime.db/market.db 均无）"})
        return
    src = latest_source(db_root)
    ts_raw = row.get("ts")
    regime = row.get("regime")
    dxy, vix, spx, etf = row.get("dxy"), row.get("vix"), row.get("spx"), row.get("btc_etf_flow")
    age = fmt_age_minutes(parse_utc_iso(ts_raw))
    # regime 必非空
    if not regime or str(regime).strip() in ("", "null", "None"):
        results.append({"name": "regime", "status": "FAIL", "msg": f"regime 为空 @ {ts_raw} [src={src}]"})
        return
    # 新鲜度：HH:01 写后 3h 内合规；其他时间允许 24h
    now = now_cst()
    threshold = 180 if (now.hour == 1 and now.minute < 30) else 24 * 60
    if age is not None and age > threshold:
        status, msg = "WARN", f"regime={regime} @ {ts_raw} age={age}m > {threshold}m [src={src}]"
    else:
        status, msg = "PASS", f"regime={regime} @ {ts_raw} (age={age}m) [src={src}]"
    results.append({
        "name": "regime", "status": status, "msg": msg,
        "regime": regime, "ts": ts_raw, "age_min": age,
        "dxy": dxy, "vix": vix, "spx": spx, "btc_etf_flow": etf, "src": src,
    })


def check_news(db_root, stale_min, hh01_only, results):
    """P3-4: news_items 最近 2h >= 3；1h 低量只 WARN，不再频繁 FAIL。"""
    news = os.path.join(db_root, "news.db")
    con = safe_connect(news)
    if not con or isinstance(con, Exception):
        results.append({"name": "news", "status": "FAIL", "msg": f"news.db 不可读: {con}"})
        return
    try:
        # 2026-07-02 修：news_items.ts 混两种格式并发写入（V2.0 news_writer 写 CST 空格
        # 'YYYY-MM-DD HH:MM:SS'；collect_data 遗留写 UTC-Z 'YYYY-MM-DDTHH:MM:SSZ'）。字符串
        # 直比会漏——现活跃新闻多为 CST，其 ' '(0x20) < 'T'(0x54) 被 UTC-T 截点排除，实测漏 ~92%
        # （24 vs 真实 290），致假 "news 0 items" FAIL。归一到 naive-UTC 再比：UTC-Z→datetime()、
        # CST-space→datetime(-8h)；截点用 naive-UTC 空格格式。
        _tsn = "CASE WHEN ts LIKE '%Z' THEN datetime(ts) ELSE datetime(ts,'-8 hours') END"
        one_h_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        two_h_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        count_1h = con.execute(
            f"SELECT COUNT(*) FROM news_items WHERE {_tsn} >= ?", (one_h_ago,)
        ).fetchone()[0]
        count_2h = con.execute(
            f"SELECT COUNT(*) FROM news_items WHERE {_tsn} >= ?", (two_h_ago,)
        ).fetchone()[0]
        latest = con.execute(f"SELECT MAX({_tsn}) FROM news_items").fetchone()[0]
        # v7.1.2：新闻源有批量/时区抖动，避免 1h=0 误触发 P1 FAIL；2h <3 才 FAIL。
        if count_2h < 3:
            status, msg = "FAIL", f"{count_1h} items/1h, {count_2h} items/2h (threshold 2h>=3, latest={latest})"
        elif count_1h < 3:
            status, msg = "WARN", f"{count_1h} items/1h, {count_2h} items/2h (1h 偏低，按 2h 兜底继续)"
        elif count_1h < 5:
            status, msg = "WARN", f"{count_1h} items/1h, {count_2h} items/2h (低于 5 条，建议补采)"
        else:
            status, msg = "PASS", f"{count_1h} items/1h, {count_2h} items/2h"
        results.append({"name": "news", "status": status, "msg": msg, "count_1h": count_1h, "count_2h": count_2h, "latest": latest})
    finally:
        con.close()


def check_account(db_root, stale_min, hh01_only, results):
    """P3-5: account_snapshots 最新一行存在 + 新鲜"""
    account = os.path.join(db_root, "account.db")
    con = safe_connect(account)
    if not con or isinstance(con, Exception):
        results.append({"name": "account", "status": "FAIL", "msg": f"account.db 不可读: {con}"})
        return
    try:
        # C3（2026-07-03）：ts 混 Z/CST 期间裸词典序 'T'(0x54)>' '(0x20) 会让旧 Z 行
        # 恒压过更新的 CST 行 → 假 stale FAIL。按归一化时间排序（对全 CST/全 Z 均正确）。
        r = con.execute(
            "SELECT ts, totalEq, availBal, upl FROM account_snapshots "
            "WHERE profile='live' AND ts GLOB '[0-9][0-9][0-9][0-9]-*' "
            "ORDER BY (CASE WHEN ts LIKE '%Z' THEN datetime(ts) "
            "ELSE datetime(ts, '-8 hours') END) DESC LIMIT 1"
        ).fetchone()
        if not r:
            results.append({"name": "account", "status": "FAIL", "msg": "live 账户无快照"})
            return
        ts_raw, total_eq, avail, upl = r
        age = fmt_age_minutes(parse_utc_iso(ts_raw))
        if age is not None and age > stale_min:
            status, msg = "FAIL", f"totalEq={total_eq} @ {ts_raw} age={age}m > {stale_min}m（live 账户快照过期，P0 排查）"
        else:
            status, msg = "PASS", f"totalEq={total_eq} avail={avail} upl={upl} (age={age}m)"
        results.append({
            "name": "account", "status": status, "msg": msg,
            "ts": ts_raw, "age_min": age, "totalEq": total_eq, "upl": upl,
        })
    finally:
        con.close()


def check_volume_anomaly(db_root, stale_min, hh01_only, results):
    """P3-6: 异常成交 — 每币与自身历史基线比（v7.1.3 2026-06-11 体检 3.6 重写）

    旧实现把全币种最新批次 vol24h 拉通求均值：量纲不可比（BTC 千万张 vs 股票型
    合约几十张），均值被小币拖成个位数，27 币全天永真报警，无信息量。
    新实现：每币当前 vol24h > 3x 自身过去 26 天均值（排除最近 24h 防自污染）
    才算异常；历史样本 < 1 天（96 行）的币跳过不判。
    """
    market = os.path.join(db_root, "market.db")
    con = safe_connect(market)
    if not con or isinstance(con, Exception):
        results.append({"name": "volume_anomaly", "status": "FAIL", "msg": f"market.db 不可读: {con}"})
        return
    try:
        rows = con.execute(
            """
            WITH ranked AS (
                SELECT symbol, vol24h, ts,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
                FROM tick_snapshots
                WHERE vol24h IS NOT NULL AND vol24h > 0
            ),
            cur AS (SELECT symbol, vol24h, ts FROM ranked WHERE rn = 1),
            base AS (
                SELECT t.symbol, AVG(t.vol24h) AS avg_vol, COUNT(*) AS n
                FROM tick_snapshots t
                JOIN cur c ON c.symbol = t.symbol
                WHERE t.vol24h IS NOT NULL AND t.vol24h > 0
                  AND t.ts <  datetime(c.ts, '-1 day')
                  AND t.ts >= datetime(c.ts, '-27 days')
                GROUP BY t.symbol
            )
            SELECT cur.symbol, cur.vol24h, base.avg_vol, base.n
            FROM cur LEFT JOIN base ON base.symbol = cur.symbol
            """
        ).fetchall()
        if not rows or len(rows) < 10:
            results.append({"name": "volume_anomaly", "status": "WARN", "msg": "样本不足，跳过"})
            return
        anomalies = []   # (symbol, cur_vol, ratio)
        skipped = []
        for sym, cur_vol, avg_vol, n in rows:
            if avg_vol is None or (n or 0) < 96:
                skipped.append(sym)
                continue
            if cur_vol > 3.0 * avg_vol:
                anomalies.append((sym, cur_vol, round(cur_vol / avg_vol, 1)))
        anomalies.sort(key=lambda x: -x[2])
        if anomalies:
            top = ", ".join(f"{s}({r}x)" for s, _, r in anomalies[:5])
            status = "WARN"
            msg = f">3x 自身26天均值 {len(anomalies)} 个: {top}"
        else:
            status = "PASS"
            msg = f"全部 {len(rows) - len(skipped)} 币无 >3x 自身基线异常"
        if skipped:
            msg += f"（{len(skipped)} 币历史样本不足跳过）"
        results.append({
            "name": "volume_anomaly", "status": status, "msg": msg,
            "anomaly_count": len(anomalies),
            "anomalies": [(s, v) for s, v, _ in anomalies[:10]],
            "skipped_insufficient": skipped[:10],
        })
    finally:
        con.close()


def check_degraded(db_root, stale_min, hh01_only, results):
    """P3-7: 已知降级源（FRED 冻结 / ETF proxy 振幅）"""
    # 2026-06-27 regime 拆库收尾：cross_market 迁 regime.db（market.db 表已 DROP）
    regime = os.path.join(db_root, "regime.db")
    con = safe_connect(regime)
    if not con or isinstance(con, Exception):
        results.append({"name": "degraded", "status": "WARN", "msg": f"regime.db 不可读: {con}，跳过降级检测"})
        return
    try:
        r = con.execute(
            "SELECT ts, dxy, dxy_d1, vix, vix_d1, spx, spx_d1, btc_etf_flow, gold, gold_d1 "
            "FROM cross_market ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not r:
            results.append({"name": "degraded", "status": "WARN", "msg": "cross_market 空，跳过"})
            return
        ts_raw, dxy, dxy_d1, vix, vix_d1, spx, spx_d1, etf, gold, gold_d1 = r
        marks = []
        # v7.0e.4 (2026-06-07 主人指令) A 方案：美股周末/节假日 FRED 无 d1 是正常的
        # cst_now.weekday() 5=周六 6=周日；只在工作日 d1=None 才标冻结
        cst_now = now_cst()
        is_weekend = cst_now.weekday() in (5, 6)
        weekend_note = "（周末，美股休市，FRED 无 d1 正常）" if is_weekend else ""
        # FRED 冻结：DXY/VIX/SPX 在 P2 后 24h 内若无变化视为冻结
        # v7.1.4（2026-06-11 全流程验证 B3）：值整列 NULL（如 SSL 失败后 dxy=None）比冻结更糟，
        # 旧条件 `v is not None` 让缺失静默逃过检测，一并纳入 fred_marks。
        fred_marks = []
        # (2026-06-11 降级语义修正) 三档：
        #   值=None         → 真问题（拉取失败且无可沿用历史），权重=0
        #   值在但 d1=None  → 正常（DTWEXBGS 官方发布延迟约 1 周；SP500/VIX T+1；
        #                     或 collect_slow 沿用了上一行值）——值仍可用，不降权不标记
        #   d1≈0 连续同值   → 冻结嫌疑（仅观测提示）
        for name, v, d1 in (("dxy", dxy, dxy_d1), ("vix", vix, vix_d1), ("spx", spx, spx_d1)):
            if is_weekend:
                continue
            if v is None:
                fred_marks.append(f"FRED 值缺失: {name}=None（拉取失败，权重=0）")
            elif d1 is not None and abs(d1) < 1e-6:
                fred_marks.append(f"FRED 冻结嫌疑: {name}={v} 连续同值")
        # (2026-06-11 阈值修正) btc_etf_flow 实为 BTC 24h 市值变化 USD——1.2T 市值
        # 日波动 ±2%（±2.4e10）属正常行情。旧阈值 1e9（市值 0.08%）几乎天天误报
        # "异常"并被 agent 用来压置信度。新阈值 6e10（≈市值 5%）仅极端值才疑数据质量。
        if etf is not None and abs(etf) > 6e10:
            marks.append(f"ETF proxy 振幅: btc_etf_flow={etf:.2e}（>5% BTC 市值，疑数据质量）")
        # v7.1.3（2026-06-11 体检 3.7）：FRED 冻结是长期已知降级，每轮 WARN 全天 96 次
        # 纯噪音。降为只在每日 08:00-08:30（P7 复盘窗口）报 WARN；其余轮次 PASS，
        # 但 msg 与 fred_frozen 字段保留——agent 仍按 决策权重=0 处理。
        in_daily_window = cst_now.hour == 8 and cst_now.minute <= 30
        if marks or (fred_marks and in_daily_window):
            status = "WARN"
            msg = "; ".join(marks + fred_marks) + " (决策权重=0)"
        elif fred_marks:
            status = "PASS"
            msg = "; ".join(fred_marks) + " (已知降级，权重=0，仅每日 08:00-08:30 报 WARN)"
        elif is_weekend and (dxy_d1 is None or spx_d1 is None or vix_d1 is None):
            status, msg = "PASS", f"周末美股休市，DXY={dxy} | VIX={vix} | SPX={spx} (FRED d1 缺省属正常)"
        else:
            status, msg = "PASS", f"无降级标记（DXY={dxy} d1={dxy_d1} | VIX={vix} d1={vix_d1} | SPX={spx} d1={spx_d1} | ETF={etf}）"
        results.append({
            "name": "degraded", "status": status, "msg": msg,
            "ts": ts_raw, "dxy": dxy, "vix": vix, "spx": spx, "btc_etf_flow": etf,
            "fred_frozen": bool(fred_marks),
        })
    finally:
        con.close()


def check_cycle_fresh(db_root, stale_min, hh01_only, results):
    """P3-8: V2.0 交易账本新鲜度——读 live_trades.db.trade_cycles 最近一行 ts。
    （account.db.cycle_runs.cycle_count 不再推进——V2.0 trader
    只写 trade_cycles 不调 phase5_writer；权威账本＝trade_cycles 槽位 cycle_id。）"""
    ltdb = os.path.join(db_root, "live_trades.db")
    con = safe_connect(ltdb)
    if not con or isinstance(con, Exception):
        results.append({"name": "cycle_fresh", "status": "FAIL", "msg": f"live_trades.db 不可读: {con}"})
        return
    try:
        r = con.execute(
            "SELECT cycle_id, ts FROM trade_cycles ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if not r:
            results.append({"name": "cycle_fresh", "status": "FAIL", "msg": "trade_cycles 为空（交易账本断链）"})
            return
        cycle_id, ts = r
        dt = parse_utc_iso(ts)
        age = fmt_age_minutes(dt)
        if age is None:
            status, msg = "WARN", f"trade_cycle={cycle_id} ts 无法解析 ts={ts}"
        elif age > 75:
            status, msg = "FAIL", f"latest trade_cycle={cycle_id} @ {ts} age={age}m > 75m（交易账本断链）"
        elif age > 45:
            status, msg = "WARN", f"latest trade_cycle={cycle_id} @ {ts} age={age}m > 45m（dispatcher/采集延迟？）"
        else:
            status, msg = "PASS", f"trade_cycle={cycle_id} @ {ts} age={age}m"
        results.append({
            "name": "cycle_fresh", "status": status, "msg": msg,
            "cycle_id": cycle_id, "ts": ts, "age_min": age,
        })
    finally:
        con.close()


def check_playbook(db_root, stale_min, hh01_only, results):
    """P3-9: playbook 可达性（v7.1.3 2026-06-11 体检 3.9 新增）

    06-10 22:31 现场：新 session 去 lessons.db 找 playbook 表（实际在 account.db），
    每轮渲染 play_id=-1 "(no playbook table)"，P4 决策失去经验引用。
    本检查确认 account.db.playbook 表存在且有行；查不到即 FAIL 并在 msg 里
    写明正确库表位置（hypotheses 同在 account.db）。
    """
    account = os.path.join(db_root, "account.db")
    con = safe_connect(account)
    if not con or isinstance(con, Exception):
        results.append({"name": "playbook", "status": "FAIL", "msg": f"account.db 不可读: {con}"})
        return
    try:
        t = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='playbook'"
        ).fetchone()
        if not t:
            results.append({"name": "playbook", "status": "FAIL",
                            "msg": "account.db 缺 playbook 表（注意：playbook/hypotheses 都在 account.db，不在 lessons.db）"})
            return
        n = con.execute("SELECT COUNT(*) FROM playbook").fetchone()[0]
        if n == 0:
            results.append({"name": "playbook", "status": "WARN",
                            "msg": "playbook 0 行（P4 无经验可引用）"})
            return
        results.append({"name": "playbook", "status": "PASS",
                        "msg": f"playbook {n} 条 @ account.db（P4 必引；hypotheses 同库）",
                        "rows": n})
    finally:
        con.close()


def check_lost_cycles(db_root, stale_min, hh01_only, results):
    """派单丢轮告警（2026-07-02，红线安全：只告警不自动重试）。

    form①：决策首棒已派 >LOST_MIN 分钟但 analysis_runs 无对应行（历史 analyst；
    2026-07-23 起 unified live）；
    form②：fast 采集已完成、槽位已过 30 分钟但 stage_dispatch 从未出现 analyst/live。
    两者都会令该 cycle 分析+交易+推送全丢。本检测供 reviewer/巡检
    FAIL 告警（推统一 QQ target）；**不自动 release/refire**（禁 watchdog 红线；V2.0 无任何
    自动兜底派发，过窗槽 dispatcher 仅打 "collection window expired" alert-only WARN，
    永不补派）。dispatched_at 是 UTC+8 空格格式；datetime('now','+8 hours')=CST now。
    回看窗 2026-07-06 由 6h 放宽到 24h：13:30 型静默丢槽在晚间巡检时已出 6h 窗而漏检；
    丢轮信号按日累计降噪，孤立丢轮静默记 audit。
    """
    ledger_db = os.path.join(db_root, "ledger.db")
    analysis_db = os.path.join(db_root, "analysis.db")
    con = safe_connect(ledger_db)
    if not con or isinstance(con, Exception):
        results.append({"name": "lost_cycles", "status": "WARN", "msg": f"ledger.db 不可读: {con}"})
        return
    LOST_MIN = 12  # 首段 analysis 正常数分钟产出；派发 >12min 仍无产出 = 丢轮
    try:
        rows = con.execute(
            "SELECT cycle_id, MIN(dispatched_at) dispatched_at FROM stage_dispatch "
            "WHERE stage IN ('analyst','live') "
            "AND datetime(dispatched_at) <= datetime('now','+8 hours',?) "
            "AND datetime(dispatched_at) >= datetime('now','+8 hours','-24 hours') "
            "AND cycle_id NOT LIKE 'TEST-%' "
            "GROUP BY cycle_id ORDER BY cycle_id",
            (f"-{LOST_MIN} minutes",)).fetchall()
        never = con.execute(
            "SELECT c.cycle_id,c.ts FROM collection_runs c "
            "WHERE c.source='fast' AND lower(c.status) NOT IN ('error','timeout','fail','failed') "
            "AND c.cycle_id NOT LIKE 'TEST-%' "
            "AND datetime(replace(c.cycle_id,'T',' ')) <= datetime('now','+8 hours','-30 minutes') "
            "AND datetime(replace(c.cycle_id,'T',' ')) >= datetime('now','+8 hours','-24 hours') "
            "AND NOT EXISTS (SELECT 1 FROM stage_dispatch s "
            "                WHERE s.cycle_id=c.cycle_id AND s.stage IN ('analyst','live')) "
            "ORDER BY c.cycle_id").fetchall()
    finally:
        con.close()
    acon = safe_connect(analysis_db)
    if not acon or isinstance(acon, Exception):
        results.append({"name": "lost_cycles", "status": "WARN", "msg": f"analysis.db 不可读: {acon}"})
        return
    lost = []
    try:
        for cyc, disp in rows:
            r = acon.execute("SELECT 1 FROM analysis_runs WHERE cycle_id=? LIMIT 1", (cyc,)).fetchone()
            if not r:
                lost.append((cyc, disp))
    finally:
        acon.close()
    never_cycles = [(c, t) for c, t in never]
    all_cycles = [c for c, _ in lost] + [c for c, _ in never_cycles]
    if not all_cycles:
        results.append({"name": "lost_cycles", "status": "PASS",
                        "msg": f"近24h 无派单丢轮（已派无产出=0，从未派发=0）"})
    else:
        fired_detail = ", ".join(f"{c}(派于{d})" for c, d in lost[:5]) or "无"
        never_detail = ", ".join(f"{c}(采集于{t})" for c, t in never_cycles[:5]) or "无"
        status = "FAIL" if len(all_cycles) >= 2 else "WARN"
        form = "mixed" if lost and never_cycles else (
            "decision_fired_no_analysis" if lost else "never_dispatched")
        results.append({"name": "lost_cycles", "status": status,
                        "form": form,
                        "lost_cycles": all_cycles,
                        "decision_fired_no_analysis": [c for c, _ in lost],
                        "analyst_fired_no_analysis": [c for c, _ in lost],
                        "never_dispatched": [c for c, _ in never_cycles],
                        "msg": f"近24h {len(all_cycles)} 轮丢失：已派无产出={len(lost)} "
                               f"[{fired_detail}]；采集成功但从未派分析/实盘首棒={len(never_cycles)} "
                               f"[{never_detail}]。只告警、不自动补派。"})


CHECK_FUNCS = {
    "tickers": check_tickers,
    "lost_cycles": check_lost_cycles,
    "kline": check_kline,
    "regime": check_regime,
    "news": check_news,
    "account": check_account,
    "volume_anomaly": check_volume_anomaly,
    "degraded": check_degraded,
    "cycle_fresh": check_cycle_fresh,
    "playbook": check_playbook,
}


def main():
    p = argparse.ArgumentParser(
        description="P3 数据校验聚合查询（v7.0c 巡检补漏）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--check", default="all",
                   choices=["all"] + list(CHECK_FUNCS.keys()),
                   help="跑哪个检查；all = 跑全部")
    p.add_argument("--db-root", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db"))
    p.add_argument("--stale-min", type=int, default=15, help="新鲜度阈值（分钟）")
    p.add_argument("--hh01-only", action="store_true", help="regime/kline 在 HH:01 必检；其他时间放宽")
    p.add_argument("--json", action="store_true", help="输出 JSON（默认 text）")
    args = p.parse_args()

    if args.check == "all":
        targets = list(CHECK_FUNCS.keys())
    else:
        targets = [args.check]

    results = []
    for name in targets:
        try:
            CHECK_FUNCS[name](args.db_root, args.stale_min, args.hh01_only, results)
        except Exception as e:
            results.append({"name": name, "status": "FAIL", "msg": f"执行异常: {e}"})

    # 退出码
    fail_count = sum(1 for r in results if r["status"] == "FAIL")
    if fail_count > 0:
        exit_code = 1
    else:
        exit_code = 0

    # 输出
    if args.json:
        out = {
            "ts": now_cst().strftime("%Y-%m-%d %H:%M:%S"),
            "db_root": args.db_root,
            "exit_code": exit_code,
            "results": results,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        ts = now_cst().strftime("%Y-%m-%d %H:%M:%S")
        print(f"=== P3 数据校验 @ {ts} (UTC+8) | db={args.db_root} ===")
        for r in results:
            print(f"[{r['status']}] {r['name']}: {r['msg']}")
        pass_n = sum(1 for r in results if r["status"] == "PASS")
        warn_n = sum(1 for r in results if r["status"] == "WARN")
        fail_n = sum(1 for r in results if r["status"] == "FAIL")
        print(f"\nsummary: {pass_n} PASS / {warn_n} WARN / {fail_n} FAIL  →  exit {exit_code}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
