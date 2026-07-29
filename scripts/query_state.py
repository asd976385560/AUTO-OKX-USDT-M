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
  --check {all|tickers|regime|analysis_macro|news|account|kline|volume_anomaly|degraded|cycle_fresh|playbook|lost_cycles|collection_failures}
         all = 跑全部可用检查
         analysis_macro = 交易侧 regime/DXY 权威（analysis.db.analysis_runs，非 system_state.live_*）
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
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

from _db_ro import connect_ro

CST = timezone(timedelta(hours=8))
_REGISTRY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "collectors", "sources"
)
if _REGISTRY_DIR not in sys.path:
    sys.path.insert(0, _REGISTRY_DIR)
import _registry  # noqa: E402


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


def load_public_macro_snapshot(db_root):
    """读取公开宏观权威观测表；失败时返回空字典，由 cross_market 旧字段兜底。"""
    regime = os.path.join(db_root, "regime.db")
    try:
        from public_macro import latest_snapshot

        con = connect_ro(regime, row_factory=sqlite3.Row)
        try:
            return latest_snapshot(con)
        finally:
            con.close()
    except Exception:
        return {}


def _openclaw_state_db() -> str:
    """允许巡检/隔离测试覆盖；默认只读当前用户 OpenClaw 状态库。"""
    return os.environ.get(
        "OPENCLAW_STATE_DB",
        os.path.join(os.path.expanduser("~"), ".openclaw", "state", "openclaw.sqlite"),
    )


def _ms_to_cst_str(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000.0, CST).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (TypeError, ValueError, OSError):
        return None


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
    dxy, vix, spx = row.get("dxy"), row.get("vix"), row.get("spx")
    btc_mcap_chg = row.get("btc_mcap_chg_24h_usd")
    if btc_mcap_chg is None:
        btc_mcap_chg = row.get("btc_etf_flow")
    btc_etf_net = row.get("btc_etf_net_flow_usd")
    dxy_calc_ecb = row.get("dxy_calc_ecb")
    fear_greed = row.get("fear_greed")
    fear_greed_label = row.get("fear_greed_label")
    public_macro = load_public_macro_snapshot(db_root)
    if public_macro:
        dxy_calc_row = public_macro.get("dxy_calc_ecb") or {}
        fear_row = public_macro.get("fear_greed") or {}
        etf_confirmed = public_macro.get("etf_confirmed") or {}
        dxy_calc_ecb = dxy_calc_row.get("value", dxy_calc_ecb)
        fear_greed = fear_row.get("value", fear_greed)
        fear_greed_label = fear_row.get("label", fear_greed_label)
        btc_etf_net = etf_confirmed.get("value")
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
        "dxy": dxy, "vix": vix, "spx": spx,
        "btc_mcap_chg_24h_usd": btc_mcap_chg,
        "btc_etf_net_flow_usd": btc_etf_net,
        "dxy_calc_ecb": dxy_calc_ecb,
        "fear_greed": fear_greed,
        "fear_greed_label": fear_greed_label,
        "src": src,
    })


def _macro_pick(macro: dict, *keys):
    """从 market_summary.macro 多别名取第一个非空值。"""
    if not isinstance(macro, dict):
        return None
    for key in keys:
        value = macro.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip() in ("", "-", "null", "None"):
            continue
        return value
    return None


def _parse_macro_json(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def check_analysis_macro(db_root, stale_min, hh01_only, results):
    """交易侧 regime/DXY 监控权威：analysis.db.analysis_runs（最新 ok 行）。

    2026-07-28：V2 push_pipeline 不再回写 system_state.live_dxy / live_regime，
    这两键自 07-07 起冻结，不得再当监控源。慢源采集仍走 regime.db（check_regime）；
    交易/巡检当前判断以 analysis_runs 为准。
    """
    path = os.path.join(db_root, "analysis.db")
    con = safe_connect(path)
    if not con or isinstance(con, Exception):
        results.append({
            "name": "analysis_macro",
            "status": "FAIL",
            "msg": f"analysis.db 不可读: {con}",
        })
        return
    try:
        row = con.execute(
            "SELECT cycle_id, ts, regime, regime_stale, market_summary, status "
            "FROM analysis_runs WHERE status='ok' ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if not row:
            row = con.execute(
                "SELECT cycle_id, ts, regime, regime_stale, market_summary, status "
                "FROM analysis_runs ORDER BY ts DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if not row:
            results.append({
                "name": "analysis_macro",
                "status": "FAIL",
                "msg": "analysis_runs 为空（交易侧 regime/DXY 无权威行）",
            })
            return
        cycle_id, ts_raw, regime, regime_stale, market_summary, status = row
        summary = _parse_macro_json(market_summary)
        macro = summary.get("macro") if isinstance(summary.get("macro"), dict) else summary
        if not isinstance(macro, dict):
            macro = {}
        dxy = _macro_pick(
            macro,
            "usd_broad_dtwexbgs", "usd_broad", "dxy_broad_dtwbxgs",
            "dxy", "dxy_value", "live_dxy",
        )
        dxy_zone = _macro_pick(
            macro,
            "dxy_zone", "usd_broad_zone", "usd_broad_zone_label",
        )
        dxy_d1 = _macro_pick(macro, "usd_broad_d1", "dxy_d1")
        if not regime or str(regime).strip() in ("", "null", "None"):
            regime = _macro_pick(macro, "regime", "regime_label")
        age = fmt_age_minutes(parse_utc_iso(ts_raw))
        # 交易轮 15m 节奏：>45m WARN，>75m FAIL（与 cycle_fresh 同阶）
        if not regime or str(regime).strip() in ("", "null", "None"):
            results.append({
                "name": "analysis_macro",
                "status": "FAIL",
                "msg": f"analysis regime 为空 @ {cycle_id} ts={ts_raw} status={status}",
                "cycle_id": cycle_id, "ts": ts_raw, "age_min": age,
            })
            return
        if age is None:
            st, msg = "WARN", (
                f"analysis regime={regime} dxy={dxy} zone={dxy_zone} "
                f"@ {cycle_id} ts 无法解析 ts={ts_raw}"
            )
        elif age > 75:
            st, msg = "FAIL", (
                f"analysis regime={regime} dxy={dxy} zone={dxy_zone} "
                f"@ {cycle_id} age={age}m > 75m（交易侧宏观断更）"
            )
        elif age > 45:
            st, msg = "WARN", (
                f"analysis regime={regime} dxy={dxy} zone={dxy_zone} "
                f"@ {cycle_id} age={age}m > 45m"
            )
        else:
            st, msg = "PASS", (
                f"analysis regime={regime} dxy={dxy} zone={dxy_zone} "
                f"@ {cycle_id} age={age}m"
            )
        # 废弃缓存：仅标注漂移，不参与权威判定、不因漂移 FAIL
        deprecated = {}
        acc = os.path.join(db_root, "account.db")
        acon = safe_connect(acc)
        if acon and not isinstance(acon, Exception):
            try:
                for key in ("live_dxy", "live_regime"):
                    kr = acon.execute(
                        "SELECT value, updated_utc FROM system_state WHERE key=?", (key,)
                    ).fetchone()
                    if kr:
                        deprecated[key] = {"value": kr[0], "updated_utc": kr[1]}
            finally:
                acon.close()
        drift_bits = []
        if deprecated.get("live_regime") and str(deprecated["live_regime"]["value"]).strip() != str(regime).strip():
            drift_bits.append(
                f"system_state.live_regime={deprecated['live_regime']['value']}@{deprecated['live_regime']['updated_utc']}(deprecated)"
            )
        if deprecated.get("live_dxy") and dxy is not None:
            try:
                old_v = float(deprecated["live_dxy"]["value"])
                new_v = float(dxy)
                if abs(old_v - new_v) > 1e-6:
                    drift_bits.append(
                        f"system_state.live_dxy={deprecated['live_dxy']['value']}@{deprecated['live_dxy']['updated_utc']}(deprecated)"
                    )
            except Exception:
                if str(deprecated["live_dxy"]["value"]).strip() != str(dxy).strip():
                    drift_bits.append(
                        f"system_state.live_dxy={deprecated['live_dxy']['value']}@{deprecated['live_dxy']['updated_utc']}(deprecated)"
                    )
        if drift_bits:
            msg = msg + " | ignore " + "; ".join(drift_bits)
        results.append({
            "name": "analysis_macro",
            "status": st,
            "msg": msg,
            "cycle_id": cycle_id,
            "ts": ts_raw,
            "age_min": age,
            "regime": regime,
            "regime_stale": regime_stale,
            "dxy": dxy,
            "dxy_zone": dxy_zone,
            "dxy_d1": dxy_d1,
            "analysis_status": status,
            "authority": "analysis.db.analysis_runs",
            "deprecated_system_state": deprecated or None,
        })
    finally:
        con.close()


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
    """P3-7: 已知降级源（FRED 真时效 / BTC市值24h振幅 / 真实ETF缺口）"""
    # 2026-06-27 regime 拆库收尾：cross_market 迁 regime.db（market.db 表已 DROP）
    regime = os.path.join(db_root, "regime.db")
    con = safe_connect(regime)
    if not con or isinstance(con, Exception):
        results.append({"name": "degraded", "status": "WARN", "msg": f"regime.db 不可读: {con}，跳过降级检测"})
        return
    try:
        r = con.execute(
            "SELECT ts,dxy,dxy_d1,vix,vix_d1,spx,spx_d1,"
            "btc_mcap_chg_24h_usd,btc_etf_net_flow_usd,gold,gold_d1,"
            "dxy_calc_ecb,dxy_calc_ecb_d1,fear_greed,fear_greed_label,source_meta "
            "FROM cross_market ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not r:
            results.append({"name": "degraded", "status": "WARN", "msg": "cross_market 空，跳过"})
            return
        (
            ts_raw, dxy, dxy_d1, vix, vix_d1, spx, spx_d1,
            btc_mcap_chg, btc_etf_net, gold, gold_d1,
            dxy_calc_ecb, dxy_calc_ecb_d1, fear_greed, fear_greed_label,
            source_meta,
        ) = r
        public_macro = load_public_macro_snapshot(db_root)
        if public_macro:
            dxy_calc_row = public_macro.get("dxy_calc_ecb") or {}
            fear_row = public_macro.get("fear_greed") or {}
            etf_confirmed = public_macro.get("etf_confirmed") or {}
            dxy_calc_ecb = dxy_calc_row.get("value", dxy_calc_ecb)
            dxy_calc_ecb_d1 = public_macro.get(
                "dxy_calc_ecb_d1", dxy_calc_ecb_d1
            )
            fear_greed = fear_row.get("value", fear_greed)
            fear_greed_label = fear_row.get("label", fear_greed_label)
            btc_etf_net = etf_confirmed.get("value")
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
        try:
            meta = json.loads(source_meta or "{}")
            dxy_as_of = str((meta.get("dxy") or {}).get("source_as_of") or "")
            etf_meta = meta.get("btc_etf_net_flow_usd") or {}
        except (TypeError, json.JSONDecodeError):
            dxy_as_of = ""
            etf_meta = {}
        if public_macro:
            etf_confirmed = public_macro.get("etf_confirmed") or {}
            etf_provisional = public_macro.get("etf_provisional") or {}
            etf_conflict = public_macro.get("etf_conflict") or {}
            if etf_confirmed:
                etf_meta = {
                    "status": "cross_checked",
                    "source_as_of": etf_confirmed.get("observation_date"),
                }
            elif etf_conflict:
                etf_meta = {
                    "status": "conflict",
                    "source_as_of": etf_conflict.get("observation_date"),
                }
            elif etf_provisional:
                etf_meta = {
                    "status": "provisional_single_source",
                    "provisional_value_usd": etf_provisional.get("value"),
                    "source_as_of": etf_provisional.get("observation_date"),
                    "source": etf_provisional.get("source"),
                }
        dxy_last_seen = f"{dxy_as_of} 23:59:59" if len(dxy_as_of) == 10 else None
        if dxy_last_seen and _registry.is_stale(
            "weekday", dxy_last_seen, now=cst_now
        ):
            fred_marks.append(
                "USD_BROAD(DTWEXBGS)源旧: "
                f"source_as_of={dxy_as_of}（legacy字段=dxy；非ICE DXY）"
            )
        # (2026-06-11 阈值修正) btc_etf_flow 实为 BTC 24h 市值变化 USD——1.2T 市值
        # 日波动 ±2%（±2.4e10）属正常行情。旧阈值 1e9（市值 0.08%）几乎天天误报
        # "异常"并被 agent 用来压置信度。新阈值 6e10（≈市值 5%）仅极端值才疑数据质量。
        if btc_mcap_chg is not None and abs(btc_mcap_chg) > 6e10:
            marks.append(
                "BTC市值24h振幅: "
                f"btc_mcap_chg_24h_usd={btc_mcap_chg:.2e}"
                "（>5% BTC 市值，疑数据质量）"
            )
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
            status, msg = (
                "PASS",
                f"周末美股休市，USD_BROAD(DTWEXBGS)={dxy} | "
                f"VIX={vix} | SPX={spx} (FRED d1 缺省属正常)",
            )
        else:
            etf_s = (
                str(btc_etf_net)
                if btc_etf_net is not None
                else (
                    f"NULL(provisional={etf_meta.get('provisional_value_usd')},"
                    f" as_of={etf_meta.get('source_as_of')})"
                    if etf_meta.get("status") == "provisional_single_source"
                    else "NULL(尚无双源一致值)"
                )
            )
            status, msg = (
                "PASS",
                f"无降级标记（USD_BROAD(DTWEXBGS)={dxy} d1={dxy_d1} | "
                f"DXY_CALC_ECB={dxy_calc_ecb} d1={dxy_calc_ecb_d1}"
                "（非ICE官方报价） | "
                f"VIX={vix} d1={vix_d1} | SPX={spx} d1={spx_d1} | "
                f"BTC_MCAP_CHG_24H_USD={btc_mcap_chg} | "
                f"BTC_ETF_NET_FLOW_USD={etf_s} | "
                f"FEAR_GREED={fear_greed}/{fear_greed_label}）",
            )
        results.append({
            "name": "degraded", "status": status, "msg": msg,
            "ts": ts_raw, "dxy": dxy, "vix": vix, "spx": spx,
            "btc_mcap_chg_24h_usd": btc_mcap_chg,
            "btc_etf_net_flow_usd": btc_etf_net,
            "btc_etf_flow_status": etf_meta.get("status"),
            "dxy_calc_ecb": dxy_calc_ecb,
            "dxy_calc_ecb_d1": dxy_calc_ecb_d1,
            "fear_greed": fear_greed,
            "fear_greed_label": fear_greed_label,
            "dxy_source_as_of": dxy_as_of or None,
            "fred_frozen": bool(fred_marks),
        })
    finally:
        con.close()


def check_cycle_fresh(db_root, stale_min, hh01_only, results):
    """P3-8: V2.0 交易账本新鲜度——读 live_trades.db.trade_cycles 最新槽位 ts。
    （account.db.cycle_runs.cycle_count 不再推进——V2.0 trader
    只写 trade_cycles，不旁路调用其他 writer；权威账本＝trade_cycles 槽位 cycle_id。
    历史补账会 INSERT OR REPLACE 旧槽位并改变 rowid，故不能按 rowid 判断最新周期。）"""
    ltdb = os.path.join(db_root, "live_trades.db")
    con = safe_connect(ltdb)
    if not con or isinstance(con, Exception):
        results.append({"name": "cycle_fresh", "status": "FAIL", "msg": f"live_trades.db 不可读: {con}"})
        return
    try:
        r = con.execute(
            "SELECT cycle_id, ts FROM trade_cycles ORDER BY cycle_id DESC LIMIT 1"
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


def check_collection_failures(db_root, stale_min, hh01_only, results):
    """近24h采集/cron失败审计；只告警，不补采、不重跑、不改变调度状态。

    lost_cycles 只覆盖“采集成功后未派发”，本检查专门覆盖：
      1. collection_runs 已明确记为 error/timeout/fail/failed；
      2. 外层 OpenClaw 命令在落账前超时/失败；
      3. 当前仍有 consecutive_errors 的 OKX cron。
    """
    failure_states = ("error", "timeout", "fail", "failed")
    ledger_db = os.path.join(db_root, "ledger.db")
    collection_errors = []
    con = safe_connect(ledger_db)
    if not con or isinstance(con, Exception):
        results.append({
            "name": "collection_failures",
            "status": "WARN",
            "msg": f"ledger.db 不可读，无法核对采集失败: {con}",
        })
        return
    try:
        placeholders = ",".join("?" for _ in failure_states)
        collection_errors = con.execute(
            "SELECT cycle_id,source,status,ts,COALESCE(err,'') "
            "FROM collection_runs "
            f"WHERE lower(status) IN ({placeholders}) "
            "AND cycle_id NOT LIKE 'TEST-%' "
            "AND datetime(ts) >= datetime('now','+8 hours','-24 hours') "
            "ORDER BY datetime(ts)",
            failure_states,
        ).fetchall()
    finally:
        con.close()

    cron_errors = []
    active_errors = []
    cron_db_error = None
    cron_db = _openclaw_state_db()
    ocon = safe_connect(cron_db)
    if not ocon or isinstance(ocon, Exception):
        cron_db_error = f"OpenClaw 状态库不可读: {ocon or cron_db}"
    else:
        try:
            placeholders = ",".join("?" for _ in failure_states)
            since_ms = int((datetime.now(timezone.utc).timestamp() - 86400) * 1000)
            cron_errors = ocon.execute(
                "SELECT j.name,l.status,COALESCE(l.error,''),l.run_at_ms,l.duration_ms "
                "FROM cron_run_logs l JOIN cron_jobs j "
                "ON j.store_key=l.store_key AND j.job_id=l.job_id "
                "WHERE j.name LIKE 'okx-%' "
                f"AND lower(COALESCE(l.status,'')) IN ({placeholders}) "
                "AND l.ts>=? ORDER BY l.ts",
                (*failure_states, since_ms),
            ).fetchall()
            active_errors = ocon.execute(
                "SELECT name,last_run_status,COALESCE(last_error,''),"
                "COALESCE(consecutive_errors,0) "
                "FROM cron_jobs WHERE name LIKE 'okx-%' "
                "AND (COALESCE(consecutive_errors,0)>0 "
                f"OR lower(COALESCE(last_run_status,'')) IN ({placeholders})) "
                "ORDER BY name",
                failure_states,
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            cron_db_error = f"OpenClaw cron 表核对失败: {exc}"
        finally:
            ocon.close()

    if not collection_errors and not cron_errors and not active_errors and not cron_db_error:
        results.append({
            "name": "collection_failures",
            "status": "PASS",
            "msg": "近24h 无采集失败、OpenClaw cron 错误或当前连续错误",
            "collection_errors": [],
            "cron_errors": [],
            "active_cron_errors": [],
        })
        return

    collection_detail = [
        {
            "cycle_id": row[0],
            "source": row[1],
            "status": row[2],
            "ts": row[3],
            "err": str(row[4] or "")[:160],
        }
        for row in collection_errors[:10]
    ]
    cron_detail = [
        {
            "job": row[0],
            "status": row[1],
            "error": str(row[2] or "")[:160],
            "run_at": _ms_to_cst_str(row[3]),
            "duration_ms": row[4],
        }
        for row in cron_errors[:10]
    ]
    active_detail = [
        {
            "job": row[0],
            "status": row[1],
            "error": str(row[2] or "")[:160],
            "consecutive_errors": row[3],
        }
        for row in active_errors
    ]
    msg = (
        f"近24h 采集失败={len(collection_errors)}，"
        f"OpenClaw cron 错误={len(cron_errors)}，"
        f"当前连续错误={len(active_errors)}。只告警、不自动补采。"
    )
    if cron_db_error:
        msg += f" {cron_db_error}"
    results.append({
        "name": "collection_failures",
        "status": "WARN",
        "msg": msg,
        "collection_errors": collection_detail,
        "cron_errors": cron_detail,
        "active_cron_errors": active_detail,
        "cron_db_error": cron_db_error,
    })


CHECK_FUNCS = {
    "tickers": check_tickers,
    "lost_cycles": check_lost_cycles,
    "collection_failures": check_collection_failures,
    "kline": check_kline,
    "regime": check_regime,
    "analysis_macro": check_analysis_macro,
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
