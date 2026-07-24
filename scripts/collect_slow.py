# -*- coding: utf-8 -*-
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from _http import TokenBucket, get_json, load_coingecko_key, load_fred_key, make_client
from _okxcli import okx_json
from _okx_http import fetch_candles_batch_sync, fetch_instruments_sync
from regime_classifier import classify_regime

DEFAULT_DB_ROOT = Path(_project_path('db'))
FRED_SERIES = {
    "dxy": "DTWEXBGS",
    "vix": "VIXCLS",
    "spx": "SP500",
}
# 入库时再补 -USDT-SWAP 后缀（见 collect_coin_sentiment 末尾 f"{ccy}-USDT-SWAP"）。
SYMBOLS = None  # None = 收集所有币种，不再过滤

# Job E (slow): 长周期 K 线时间框架
# v7.0e.3 恢复 1W/1M（2026-05-24 曾为省 HTTP 请求移除，2026-06-07 主人决定恢复——
# 否则 1W/1M 新行没 5 指标；每轮多 326×2=652 HTTP 请求约 5.4 分钟，888s 超时内）
SLOW_TIMEFRAMES = {
    "1H":  "1H",
    "4H":  "4H",
    "1D":  "1D",
    "1W":  "1W",
    "1M":  "1M",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms_to_iso(value: str | int | None) -> str | None:
    if value in (None, ""):
        return None
    try:
        milliseconds = int(str(value))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_float(value) -> float | None:
    if value in (None, "", "."):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_gold_etf_d1() -> float | None:
    """Fetch 518880 gold ETF daily return via mx-data. Returns e.g. -0.007475 for -0.7475%."""
    try:
        import sys
        from pathlib import Path
        candidates = []
        if os.environ.get("MX_DATA_PATH"):
            candidates.append(Path(os.environ["MX_DATA_PATH"]))
        candidates.extend([
            _PROJECT_ROOT.parent / "mx-data" / "mx_data.py",
            Path.home() / ".openclaw" / "workspace" / "skills" / "mx-data" / "mx_data.py",
        ])
        mx_data_path = next((p for p in candidates if p.exists()), None)
        if mx_data_path is None:
            print("[WARN] mx_data.py not found, gold ETF skipped", flush=True)
            return None
        if not os.environ.get("MX_APIKEY"):
            try:
                cfg = (_PROJECT_ROOT / "config.md").read_text(encoding="utf-8")
                import re
                m = re.search(r"###\s+4\.4 妙想资讯.*?\|\s*API Key\s*\|\s*([^|`\s][^|`]*)\s*\|", cfg, re.S)
                if m:
                    value = m.group(1).strip()
                    if not (value.startswith("<") and value.endswith(">")):
                        os.environ["MX_APIKEY"] = value
            except Exception:
                pass
        sys.path.insert(0, str(mx_data_path.parent))
        from mx_data import MXData
        mx = MXData()
        result = mx.query("518880黄金ETF近2个交易日最新价涨跌幅")
        tables, _, _, err = mx.parse_result(result)
        if err or not tables:
            print(f"[WARN] mx-data gold query failed: {err}", flush=True)
            return None
        # Find the change % row (most recent = first row)
        for table in tables:
            for row in table.get("rows", []):
                # Second field is usually change%, first is date, second is price, third is change%
                for k, v in row.items():
                    if isinstance(v, str) and "%" in v:
                        try:
                            pct = float(v.rstrip("%").rstrip("％")) / 100.0
                            print(f"[INFO] Gold ETF (518880) daily return: {pct:.4f}", flush=True)
                            return pct
                        except ValueError:
                            continue
        print("[WARN] No gold ETF change% found in mx-data result", flush=True)
        return None
    except Exception as e:
        print(f"[WARN] gold ETF fetch failed: {e}", flush=True)
        return None


def _fetch_gold_price_usd() -> float | None:
    """Fetch current gold spot price in USD via CoinGecko (tether-gold as proxy).
    FRED gold series are unavailable on free tier, so we use CoinGecko's tether-gold
    which tracks physical gold price closely."""
    try:
        import httpx
        from _http import load_coingecko_key
        cg_key = load_coingecko_key()
        if not cg_key:
            return None
        with httpx.Client(trust_env=True, timeout=20.0, follow_redirects=True) as c:
            resp = c.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "tether-gold", "vs_currencies": "usd"},
                headers={"x-cg-demo-api-key": cg_key, "accept": "application/json"},
            )
            data = resp.json()
            gold = to_float((data.get("tether-gold") or {}).get("usd"))
            if gold is not None:
                print(f"[INFO] Gold spot price (CoinGecko tether-gold): ${gold:.2f}", flush=True)
            return gold
    except Exception as e:
        print(f"[WARN] gold price fetch failed: {e}", flush=True)
        return None


def open_db(db_root: Path, name: str) -> sqlite3.Connection:
    path = db_root / name
    if not path.exists():
        raise RuntimeError(f"数据库不存在：{path}（请先运行 init_db.py）")
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA journal_mode=WAL;")
    return connection


# ── Indicator computation (same logic as collect_data.py) ────────────────────────

def ema_series(values: list[float], period: int) -> list[float | None]:
    series: list[float | None] = []
    multiplier = 2.0 / (period + 1)
    ema_value: float | None = None
    for index, value in enumerate(values):
        if index + 1 < period:
            series.append(None)
            continue
        if ema_value is None:
            ema_value = sum(values[index + 1 - period:index + 1]) / period
        else:
            ema_value = (value - ema_value) * multiplier + ema_value
        series.append(ema_value)
    return series


def compute_indicators(candles: list[dict]) -> list[dict]:
    """Add ma5/ma20/atr14/rsi14/macd_hist to each candle dict in-place."""
    if not candles:
        return candles
    closes = [float(c["c"]) for c in candles if c.get("c") is not None]
    if len(closes) < 26:
        for c in candles:
            c["ma5"] = None; c["ma20"] = None; c["atr14"] = None; c["rsi14"] = None; c["macd_hist"] = None
        return candles

    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_line: list[float | None] = []
    for a, b in zip(ema12, ema26):
        if a is None or b is None:
            macd_line.append(None)
        else:
            macd_line.append(a - b)
    signal_ema = ema_series([v for v in macd_line if v is not None], 9)
    signal_series: list[float | None] = []
    sig_idx = 0
    # v7.0e.3 修复：原版 `if sig_idx < len(signal_ema) and signal_ema[sig_idx] is not None` 在 sig_idx=0 时
    # signal_ema[0] 是 None（EMA 启动前），但 else 分支不前进 sig_idx，导致 signal_series 永远拿不到有效值，
    # hist_series 全空，macd_hist 永远是 None。改为：每次非 None macd 都无条件前进 sig_idx（None signal 也算占位）。
    for v in macd_line:
        if v is None:
            signal_series.append(None)
            continue
        signal_series.append(signal_ema[sig_idx] if sig_idx < len(signal_ema) else None)
        sig_idx += 1

    hist_series: list[float | None] = []
    for macd, sig in zip(macd_line, signal_series):
        if macd is None or sig is None:
            hist_series.append(None)
        else:
            hist_series.append(macd - sig)

    ma5_series  = ema_series(closes, 5)
    ma20_series = ema_series(closes, 20)

    # RSI (Wilder's smoothing, v5.3.2: fix per-bar RSI — was using final avg for all bars)
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    rsi_series: list[float | None] = [None] * 14
    gain = [d if d > 0 else 0.0 for d in deltas[:14]]
    loss = [-d if d < 0 else 0.0 for d in deltas[:14]]
    avg_gain = sum(gain) / 14 if gain else 0.0
    avg_loss = sum(loss) / 14 if loss else 0.0
    # Compute per-bar RSI, then update averages for next bar
    for i in range(14, len(deltas) + 1):
        rs = avg_gain / avg_loss if avg_loss != 0 else 100.0
        rsi_series.append(100.0 - (100.0 / (1.0 + rs)))
        if i < len(deltas):
            avg_gain = (avg_gain * 13 + (deltas[i] if deltas[i] > 0 else 0.0)) / 14
            avg_loss = (avg_loss * 13 + (-deltas[i] if deltas[i] < 0 else 0.0)) / 14

    # ATR
    highs = [float(c["h"]) for c in candles if c.get("h") is not None]
    lows  = [float(c["l"]) for c in candles if c.get("l") is not None]
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1]),
        )
        trs.append(tr)
    atr_series: list[float | None] = [None] * 14
    if len(trs) >= 14:
        atr_series.append(sum(trs[0:14]) / 14)
        for i in range(14, len(trs)):
            atr_series.append((atr_series[-1] * 13 + trs[i]) / 14)

    # Merge
    ma5_idx = ma20_idx = macd_idx = rsi_idx = atr_idx = 0
    for c in candles:
        c["ma5"]     = ma5_series[ma5_idx]     if ma5_idx     < len(ma5_series)     else None
        c["ma20"]    = ma20_series[ma20_idx]    if ma20_idx    < len(ma20_series)    else None
        c["macd_hist"] = hist_series[macd_idx]   if macd_idx   < len(hist_series)    else None
        c["rsi14"]   = rsi_series[rsi_idx]      if rsi_idx    < len(rsi_series)     else None
        c["atr14"]   = atr_series[atr_idx]      if atr_idx    < len(atr_series)     else None
        ma5_idx += 1; ma20_idx += 1; macd_idx += 1; rsi_idx += 1; atr_idx += 1
    return candles


# ── K-line fetch ───────────────────────────────────────────────────────────────

def _fetch_all_swap_symbols() -> list[str]:
    # Use HTTP (no subprocess) to avoid isolated-session process creation limits.
    all_instruments = fetch_instruments_sync("SWAP")
    symbols = [
        inst["instId"]
        for inst in all_instruments
        if inst.get("instType") == "SWAP"
        and inst.get("settleCcy") == "USDT"
        and inst.get("ctType") == "linear"
        and inst.get("state") == "live"
    ]
    return symbols, all_instruments


def _ensure_instruments_cache_table(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS instruments_cache (
            instId    TEXT PRIMARY KEY,
            ctVal     REAL,
            lotSz     REAL
        )
    """)
    con.commit()


def _collect_instruments_cache(
    market_con: sqlite3.Connection, all_instruments: list[dict]
) -> int:
    """
    Write ctVal / lotSz for all USDT-M linear SWAP instruments
    into market.db.instruments_cache.
    Called by Job E (collect_slow.py) every hour so ctVal data stays fresh.
    """
    _ensure_instruments_cache_table(market_con)
    count = 0
    for inst in all_instruments:
        inst_id = inst.get("instId", "")
        if "-USDT-SWAP" not in inst_id:
            continue
        try:
            market_con.execute(
                """INSERT OR REPLACE INTO instruments_cache (instId, ctVal, lotSz)
                   VALUES (?, ?, ?)""",
                (
                    inst_id,
                    float(inst.get("ctVal") or 1),
                    float(inst.get("lotSz") or 1),
                ),
            )
            count += 1
        except Exception as e:
            print(f"  [warn] instruments_cache {inst_id}: {e}")
    market_con.commit()
    return count


def collect_slow_klines(market_con: sqlite3.Connection, symbols: list[str]) -> int:
    """Fetch 1H/4H/1D/1W/1M/1Y K-lines + indicators for ALL symbols.

    Called by Job E (collect_slow.py). Writes to kline_cache table.
    Uses INSERT OR REPLACE so existing rows are updated with latest data.
    (2026-04-22: added to collect_slow.py -- all symbols now get long-period K-lines)
    """
    # Fetch slow K-lines per timeframe: HTTP concurrent (was 1460 CLI subprocess calls)
    rows_to_write: list[tuple] = []
    for tf, bar in SLOW_TIMEFRAMES.items():
        batch = fetch_candles_batch_sync(symbols, bar, limit=60)
        for symbol, raw_candles in batch.items():
            candles = [
                {
                    "ts": ms_to_iso(entry[0]),
                    "o": to_float(entry[1]),
                    "h": to_float(entry[2]),
                    "l": to_float(entry[3]),
                    "c": to_float(entry[4]),
                    "v": to_float(entry[7]),
                }
                for entry in reversed(raw_candles)
                if ms_to_iso(entry[0]) is not None
            ]
            enriched = compute_indicators(candles)
            for item in enriched:
                rows_to_write.append(
                    (
                        item["ts"],
                        symbol,
                        tf,
                        item["o"],
                        item["h"],
                        item["l"],
                        item["c"],
                        item["v"],
                        item["ma5"],
                        item["ma20"],
                        item["atr14"],
                        item["rsi14"],
                        item["macd_hist"],
                    )
                )

    market_con.executemany(
        "INSERT OR REPLACE INTO kline_cache "
        "(ts, symbol, tf, o, h, l, c, v, ma5, ma20, atr14, rsi14, macd_hist) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows_to_write,
    )
    market_con.commit()
    return len(rows_to_write)


# ── Macro / sentiment helpers ────────────────────────────────────────────────────

def fred_latest(client, bucket: TokenBucket, series_id: str, api_key: str) -> tuple[float | None, float | None]:
    """Fetch latest FRED observation + d1 change.
    v7.0e.3 修复：limit 2→10，应对 FRED 在某些边界日（如 SPX 周末/节假日）只返回 1 条 observation
    导致 d1 算不出的 bug。10 天窗口足以覆盖周末/周一开盘的 d1 计算。
    v7.0e.4 修复 (2026-06-07 主人指令)：加 retry=2，应对 mx-data / CoinGecko 占用 TokenBucket
    导致 FRED 调 25s timeout 后只剩 1 条 observation 的偶发故障。
    """
    last_err = None
    for attempt in range(2):
        try:
            payload = get_json(
                client,
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 20,  # v7.0e.4: 10→20（再覆盖一周节假日 + retry 后仍有 d1）
                },
                bucket=bucket,
            )
            # J1 (2026-06-13): 连带观测日期返回——"有值但滞后"需要日期才能判定
            pairs = [(to_float(item.get("value")), item.get("date"))
                     for item in payload.get("observations", [])]
            pairs = [p for p in pairs if p[0] is not None]
            if not pairs:
                return None, None, None
            latest, latest_date = pairs[0]
            prev = pairs[1][0] if len(pairs) > 1 else None
            if prev in (None, 0):
                return latest, None, latest_date
            return latest, (latest - prev) / prev, latest_date
        except Exception as e:
            last_err = e
            print(f"[WARN] FRED {series_id} attempt {attempt + 1}/2 failed: {e}", flush=True)
            if attempt == 0:
                import time as _t
                _t.sleep(2.0)  # retry 前等 2s 让 token bucket 恢复
    print(f"[WARN] FRED {series_id} 两次都失败: {last_err}", flush=True)
    return None, None, None


def synthetic_dxy_d1(client=None, bucket=None) -> float | None:
    """T7 (2026-06-12) / J1 修正 (2026-06-13): ECB(frankfurter, 免 key) 合成美元篮子 d1。

    触发: FRED DTWEXBGS d1 缺失 **或观测日滞后 >4 日历日**（发布延迟 ~1 周是常态）。
    ICE DXY 几何权重: EUR .576 / JPY .136 / GBP .119 / CAD .091 / SEK .042 / CHF .036。
    绝对值口径与 DXY 不同——**只用变化率，不写 dxy 绝对值列**。
    J1 修正: 自建短生命周期 client（旧版复用外层 client，但调用点在 with 块外、client 已关闭
    → 永远静默失败。client 参数仅为兼容保留，忽略）。
    """
    try:
        from datetime import date, timedelta as _td
        end = date.today()
        start = end - _td(days=6)
        with make_client() as _c:
            payload = get_json(
                _c,
                f"https://api.frankfurter.app/{start.isoformat()}..{end.isoformat()}",
                params={"from": "USD", "to": "EUR,JPY,GBP,CAD,SEK,CHF"},
                bucket=bucket,
            )
        rates = payload.get("rates") or {}
        days = sorted(rates.keys())
        if len(days) < 2:
            return None

        def idx(d):
            r = rates[d]
            # USD→XXX 报价，幂均为正（EURUSD^-w == USDEUR^w）
            return (r["EUR"] ** 0.576 * r["JPY"] ** 0.136 * r["GBP"] ** 0.119
                    * r["CAD"] ** 0.091 * r["SEK"] ** 0.042 * r["CHF"] ** 0.036)

        a, b = idx(days[-2]), idx(days[-1])
        return (b - a) / a if a else None
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] synthetic DXY (ECB/frankfurter) 失败: {e}", flush=True)
        return None


def defillama_total_tvl(client, bucket: TokenBucket) -> float | None:
    payload = get_json(client, "https://api.llama.fi/v2/chains", bucket=bucket)
    tvls = [to_float(item.get("tvl")) for item in payload if isinstance(item, dict)]
    values = [item for item in tvls if item is not None]
    return sum(values) if values else None


def coingecko_global(bucket: TokenBucket, api_key: str) -> dict:
    import httpx
    with httpx.Client(
        trust_env=True,
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "okx-cex-auto/1.0"},
    ) as cg_client:
        payload = get_json(
            cg_client,
            "https://api.coingecko.com/api/v3/global",
            bucket=bucket,
            headers={"x-cg-demo-api-key": api_key, "accept": "application/json"},
        )
    data = (payload or {}).get("data") or {}
    btc_d = to_float((data.get("market_cap_percentage") or {}).get("btc"))
    mcap = to_float((data.get("total_market_cap") or {}).get("usd"))
    vol = to_float((data.get("total_volume") or {}).get("usd"))
    return {"btc_d": btc_d, "total_mcap_usd": mcap, "total_volume_24h_usd": vol}


def _fetch_btc_mcap_change_proxy(bucket: TokenBucket, api_key: str) -> float | None:
    """Fetch BTC 24h market-cap change (USD) from CoinGecko.

    这是市值变化代理，不是 ETF 净流。旧存储列 btc_etf_flow 为兼容保留，
    正式消费口径统一读生成列 btc_mcap_chg_24h_usd。
    Returns positive/negative float in USD (e.g. 6_471_303_545).
    """
    import httpx
    try:
        with httpx.Client(
            trust_env=True,
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "okx-cex-auto/1.0"},
        ) as cg_client:
            payload = get_json(
                cg_client,
                "https://api.coingecko.com/api/v3/coins/bitcoin",
                bucket=bucket,
                headers={"x-cg-demo-api-key": api_key, "accept": "application/json"},
            )
        md = (payload or {}).get("market_data") or {}
        change = to_float((md.get("market_cap_change_24h_in_currency") or {}).get("usd"))
        print(f"[INFO] BTC market-cap change 24h USD: {change}", flush=True)
        return change
    except Exception as e:
        print(f"[WARN] BTC market-cap change fetch failed: {e}", flush=True)
        return None


def _deterministic_sentiment(news_con: sqlite3.Connection) -> int:
    """V2.0 §6/P5：OKX sentiment-rank 不可用时兜底——从自有 news_items 算确定性情绪
    （脱 OKX 硬依赖，OKX 死也不归零）。写库仍走慢采（news.db 单写者），不破单写不变量。"""
    try:
        import sys as _sys
        if _project_path('scripts') not in _sys.path:
            _sys.path.insert(0, _project_path('scripts'))
        import sentiment_compute as _sc
        main_db = next((r[2] for r in news_con.execute("PRAGMA database_list").fetchall()
                        if r[1] == "main"), None)
        if not main_db:
            return 0
        rows = _sc.compute(main_db, period="24h")
        n = 0
        for r in rows:
            news_con.execute(
                "INSERT OR REPLACE INTO coin_sentiment "
                "(ts, symbol, period, label, bullish_ratio, bearish_ratio, bullish_cnt, "
                "bearish_cnt, neutral_cnt, mention_cnt, news_mention_cnt, x_mention_cnt, raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["ts"], r["symbol"], r["period"], r["label"], r["bullish_ratio"],
                 r["bearish_ratio"], r["bullish_cnt"], r["bearish_cnt"], r["neutral_cnt"],
                 r["mention_cnt"], r["news_mention_cnt"], r["x_mention_cnt"], r["raw"]))
            n += 1
        news_con.commit()
        return n
    except Exception as e:  # noqa: BLE001
        print(f"[collect_slow][WARN] 确定性情绪兜底失败: {e}", flush=True)
        return 0


def collect_coin_sentiment(news_con: sqlite3.Connection) -> int:
    """主源 OKX sentiment-rank（可用时全币覆盖最广）；失败/空 → 确定性自有数据兜底
    （脱 OKX 依赖，§6/P5 resilience）。"""
    inserted = 0
    try:
        payload = okx_json("news", "sentiment-rank", "--period", "24h", "--limit", "50")
        ts_local = utc_now_iso()
        for batch in payload:
            # OKX sentiment-rank returns 24h aggregated data with a daily timestamp (UTC 00:00).
            # Using the server timestamp makes the data appear stale (>24h) within hours.
            # Always use local collection time to reflect freshness.
            ts = ts_local
            period = batch.get("period") or "24h"
            for detail in batch.get("details") or []:
                ccy = detail.get("ccy", "")
                if SYMBOLS is not None and ccy not in SYMBOLS:
                    continue
                sentiment = detail.get("sentiment") or {}
                news_con.execute(
                    "INSERT OR REPLACE INTO coin_sentiment "
                    "(ts, symbol, period, label, bullish_ratio, bearish_ratio, bullish_cnt, bearish_cnt, neutral_cnt, mention_cnt, news_mention_cnt, x_mention_cnt, raw) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        f"{ccy}-USDT-SWAP",
                        period,
                        sentiment.get("label"),
                        to_float(sentiment.get("bullishRatio")),
                        to_float(sentiment.get("bearishRatio")),
                        int(detail.get("bullishCnt", "0")),
                        int(detail.get("bearishCnt", "0")),
                        int(detail.get("neutralCnt", "0")),
                        int(detail.get("mentionCnt", "0")),
                        int(detail.get("newsMentionCnt", "0")),
                        int(detail.get("xMentionCnt", "0")),
                        json.dumps(detail, ensure_ascii=False),
                    ),
                )
                inserted += 1
        news_con.commit()
    except Exception as e:  # noqa: BLE001 —— OKX 不可用转兜底，不让整轮采集失败
        print(f"[collect_slow][WARN] OKX sentiment-rank 不可用，转确定性兜底: {e}", flush=True)
        inserted = 0
    if inserted == 0:
        det = _deterministic_sentiment(news_con)
        if det:
            print(f"[collect_slow] coin_sentiment 确定性兜底写 {det} 行（脱 OKX）", flush=True)
        return det
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="OKX Job E: slow data collector.")
    parser.add_argument("--db-root", default=str(DEFAULT_DB_ROOT))
    args = parser.parse_args()
    db_root = Path(args.db_root)
    ts = utc_now_iso()

    market_con = open_db(db_root, "market.db")
    news_con = open_db(db_root, "news.db")
    try:
        regime_con = open_db(db_root, "regime.db")
    except Exception as _e:
        regime_con = None
        print(f"[collect_slow][WARN] regime.db 打开失败，本轮 cross_market 跳过记 degraded ({_e})",
              flush=True)
    # V2.0 (2026-07-03 修): cross_market 唯一写者=慢采、唯一目标=regime.db。
    # 旧「regime.db 不可用降级写 market.db」路径已失效——market.db.cross_market 已 DROP
    # （2026-06-27 拆库），写必崩；现改为显式跳过 macro/cross_market 块并记 degraded，
    # 不再伪装降级（潜伏必崩路径清除，2026-07-03 审计核查项 4a）。
    cm_con = regime_con

    # 降级追踪（2026-07-02 修 exit0 说谎）：块级失败记入 degraded → 结尾 return 2（=degraded），
    # 让 slow_collect wrapper 如实记 ledger slow='degraded'（仍算完成不阻断，但 analyst 拿到信号），
    # 不再"K线/宏观全挂也 return 0 记 ok"。
    degraded: list[str] = []
    # Dynamically discover all live USDT-M SWAP symbols
    try:
        all_symbols, all_instruments = _fetch_all_swap_symbols()
        print(f"[collect_slow] Discovered {len(all_symbols)} USDT-M SWAP contracts", flush=True)
    except Exception as e:
        print(f"[collect_slow] WARNING: Could not fetch symbols ({e}); using empty list", flush=True)
        all_symbols, all_instruments = [], []
        degraded.append("symbols")

    # ── instruments_cache (ctVal / lotSz) ─────────────────────────────────
    try:
        if all_instruments:
            ic = _collect_instruments_cache(market_con, all_instruments)
            print(f"[collect_slow] instruments_cache: upserted {ic} rows", flush=True)
    except Exception as e:
        print(f"[collect_slow] instruments_cache skip ({e})", flush=True)

    bucket = TokenBucket(rate_per_sec=0.5, capacity=2)

    # ── K-lines (1H/4H/1D/1W/1M) ───────────────────────────────────────────
    # ALL symbols get slow K-lines (full coverage for accuracy)
    try:
        kline_rows = collect_slow_klines(market_con, all_symbols)
        print(f"[collect_slow] Wrote {kline_rows} slow kline rows for {len(all_symbols)} symbols", flush=True)
    except Exception as e:
        print(f"[collect_slow] K-line collection failed: {e}", flush=True)
        kline_rows = 0
        degraded.append("klines")
    if all_symbols and kline_rows == 0 and "klines" not in degraded:
        degraded.append("klines")  # 有币种却一根没写 = 静默全失败

    # ── Macro data ────────────────────────────────────────────────────────────
    # Gold ETF (518880) daily return via mx-data + gold spot price via FRED
    gold_d1 = _fetch_gold_etf_d1()
    gold_price = _fetch_gold_price_usd()

    try:
        with make_client() as client:
            fred_key = load_fred_key()
            dxy, dxy_d1, dxy_obs_date = fred_latest(client, bucket, FRED_SERIES["dxy"], fred_key)
            vix, vix_d1, _vix_date = fred_latest(client, bucket, FRED_SERIES["vix"], fred_key)
            spx, spx_d1, _spx_date = fred_latest(client, bucket, FRED_SERIES["spx"], fred_key)
            # DefiLlama /v2/chains 是本块唯一无内层保护的取数——超时/坏响应会抛异常冒泡到
            # 下方 except、跳过整行 cross_market（连带丢弃新鲜的 dxy/regime），使整个 :00 槽
            # regime stale 丢轮。与 CoinGecko/FRED 一致包内层 try：失败→None→走 697 行
            # tvl carry-forward，cross_market 照常写出（2026-07-05 修，A/B 实测复现+验证）。
            try:
                tvl_total = defillama_total_tvl(client, bucket)
            except Exception as _e:
                print(f"[collect_slow][WARN] defillama TVL 跳过（不可达，沿用上一行）: {_e}",
                      flush=True)
                tvl_total = None
            try:
                cg_key = load_coingecko_key()
                cg = coingecko_global(bucket, cg_key)
                btc_mcap_chg_24h_usd = _fetch_btc_mcap_change_proxy(bucket, cg_key)
            except Exception as e:
                print(f"[WARN] coingecko 跳过（API/key 不可达）: {e}", flush=True)
                cg = {"btc_d": None, "total_mcap_usd": None, "total_volume_24h_usd": None}
                btc_mcap_chg_24h_usd = None

        if cm_con is None:
            raise RuntimeError("regime.db 不可用，跳过 cross_market（记 degraded）")
        # (2026-06-11 降级治本) 宏观值瞬时拉取失败时沿用上一行——失败写 NULL 会让
        # 决策面对空值，且 P1 复制链把 NULL 传播到之后每一轮（DTWEXBGS 官方发布
        # 延迟约 1 周属正常节奏，值"陈旧"仍可用；d1 不伪造，沿用时置 None）。
        prev_vals = cm_con.execute(
            "SELECT dxy, vix, spx, gold, defillama_tvl_total, btc_etf_flow "
            "FROM cross_market WHERE dxy IS NOT NULL OR vix IS NOT NULL OR spx IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone() or (None,) * 6
        carried_forward = []
        if dxy is None and prev_vals[0] is not None:
            dxy, dxy_d1 = prev_vals[0], None
            carried_forward.append("dxy")
            print(f"[collect_slow][WARN] FRED dxy 拉取失败，沿用上一行值 {dxy}", flush=True)
        # T7 (2026-06-12) + J1 (2026-06-13): regime 唯一驱动输入 dxy_d1 的新鲜度治理——
        # ① d1 缺失 → 合成替补（T7 原逻辑）；② FRED 有值但观测日滞后 >4 日历日（DTWEXBGS
        # 发布延迟 ~1 周是常态）→ 滞后 d1 不代表当下美元动能，同样用合成 d1 驱动（J1 扩展，
        # 治 regime 三天连错 0/3 的根因）。落库 dxy_d1=实际驱动 regime 的值，日志标注来源。
        _need_synth, _why = dxy_d1 is None, "FRED d1 缺失"
        if not _need_synth and dxy_obs_date:
            try:
                from datetime import date as _date
                _lag = (_date.today() - _date.fromisoformat(str(dxy_obs_date))).days
                if _lag > 4:
                    _need_synth, _why = True, f"FRED 观测 {dxy_obs_date} 滞后 {_lag} 天(d1={dxy_d1:+.5f} 弃用)"
            except Exception:
                pass
        if _need_synth:
            _sd1 = synthetic_dxy_d1(bucket=bucket)
            if _sd1 is not None:
                dxy_d1 = _sd1
                print(f"[collect_slow][WARN] dxy_d1 来源=synthetic-ECB(frankfurter): {_sd1:+.5f}"
                      f"（{_why}；仅趋势参考，dxy 绝对值不动）", flush=True)
        if vix is None and prev_vals[1] is not None:
            vix, vix_d1 = prev_vals[1], None
            carried_forward.append("vix")
            print(f"[collect_slow][WARN] FRED vix 拉取失败，沿用上一行值 {vix}", flush=True)
        if spx is None and prev_vals[2] is not None:
            spx, spx_d1 = prev_vals[2], None
            carried_forward.append("spx")
            print(f"[collect_slow][WARN] FRED spx 拉取失败，沿用上一行值 {spx}", flush=True)
        if gold_price is None and prev_vals[3] is not None:
            gold_price = prev_vals[3]
            carried_forward.append("gold")
            print(f"[collect_slow][WARN] gold 拉取失败，沿用上一行值 {gold_price}", flush=True)
        if tvl_total is None and prev_vals[4] is not None:
            tvl_total = prev_vals[4]
            carried_forward.append("defillama_tvl_total")
            print(f"[collect_slow][WARN] TVL 拉取失败，沿用上一行值", flush=True)
        if btc_mcap_chg_24h_usd is None and prev_vals[5] is not None:
            btc_mcap_chg_24h_usd = prev_vals[5]
            carried_forward.append("btc_mcap_chg_24h_usd")
            print(f"[collect_slow][WARN] BTC 市值变化代理拉取失败，沿用上一行值", flush=True)

        # Regime 计算（2026-07-19 重做）：BTC 4H 多因子为主，DXY 只作一票修正。
        # 标签仍保持 trend_up/trend_down/range，兼容所有 V2.0 消费方；它是未来 24h
        # 方向观察输入，不是交易硬闸。参数由 backtest_regime_classifier.py 前 70%
        # 选参、后 30% 独立验证。核心 BTC 特征缺失时沿用上一行，绝不伪装 range。
        btc4h = market_con.execute(
            "SELECT ts,c,ma5,ma20,rsi14 FROM kline_cache "
            "WHERE symbol='BTC-USDT-SWAP' AND tf='4H' ORDER BY ts DESC LIMIT 7"
        ).fetchall()
        regime_result = None
        if len(btc4h) >= 7 and btc4h[0][1] and btc4h[6][1]:
            regime_result = classify_regime(
                close=btc4h[0][1],
                ma5=btc4h[0][2],
                ma20=btc4h[0][3],
                rsi14=btc4h[0][4],
                return_24h=btc4h[0][1] / btc4h[6][1] - 1.0,
                dxy_d1=dxy_d1,
            )
        if not regime_result or not regime_result.get("ok"):
            prev_row = cm_con.execute(
                "SELECT regime FROM cross_market WHERE regime IS NOT NULL ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            regime = prev_row[0] if prev_row and prev_row[0] else "range"
            why = (regime_result or {}).get("reason", "btc_4h_history_missing")
            print(f"[collect_slow][WARN] regime 特征不足({why})，沿用上一行: {regime}",
                  flush=True)
        else:
            regime = regime_result["regime"]
        if regime_result and regime_result.get("ok"):
            features = regime_result["features"]
            print(
                f"[collect_slow] Regime computed: {regime} "
                f"(score={regime_result['score']}, btc_structure={regime_result['btc_structure_score']}, "
                f"ret24h={features['return_24h']:+.4f}, "
                f"price_ma20={features['price_vs_ma20']:+.4f}, dxy_d1={dxy_d1})",
                flush=True,
            )

        # V2.0 (2026-06-26) Option A: cross_market 单写 cm_con（regime.db 优先，降级 market.db）。
        # 含真实列 btc_etf_flow（**禁**写生成列 btc_mcap_chg_24h_usd）。market.db 常规停写。
        cm_con.execute(
            "INSERT OR REPLACE INTO cross_market "
            "(ts, dxy, gold, gold_d1, vix, spx, spx_d1, btc_etf_flow, dxy_d1, vix_d1, "
            "defillama_tvl_total, regime, btc_dominance, total_mcap_usd, total_volume_24h_usd, "
            "btc_etf_net_flow_usd, source_meta, carried_forward) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, dxy, gold_price, gold_d1, vix, spx, spx_d1, btc_mcap_chg_24h_usd,
             dxy_d1, vix_d1, tvl_total, regime, cg["btc_d"], cg["total_mcap_usd"],
             cg["total_volume_24h_usd"], None,
             json.dumps({
                 "dxy": {"source": "fred", "source_as_of": dxy_obs_date},
                 "vix": {"source": "fred"},
                 "spx": {"source": "fred"},
                 "gold": {"source": "coingecko_tether_gold_proxy"},
                 "gold_d1": {"source": "mx_data_518880"},
                 "defillama_tvl_total": {"source": "defillama"},
                 "btc_mcap_chg_24h_usd": {"source": "coingecko"},
                 "btc_etf_net_flow_usd": {"source": None, "status": "not_collected"},
             }, ensure_ascii=False),
             json.dumps(carried_forward, ensure_ascii=False)),
        )
        cm_con.commit()
        print(f"[collect_slow] cross_market 单写 regime.db OK (regime={regime})", flush=True)
    except Exception as e:
        print(f"[collect_slow] Macro/cross_market collection failed: {e}", flush=True)
        degraded.append("cross_market")

    # ── Coin sentiment ────────────────────────────────────────────────────────
    try:
        sentiment_rows = collect_coin_sentiment(news_con)
    except Exception as e:
        print(f"[WARN] coin_sentiment 跳过（API 不可达）: {e}", flush=True)
        sentiment_rows = 0
        degraded.append("coin_sentiment")

    market_con.close()
    news_con.close()
    if regime_con is not None:
        try:
            regime_con.close()
        except Exception:
            pass

    # ── Write cycle_runs ──────────────────────────────────────────────────
    try:
        account_con = open_db(db_root, "account.db")
        ts_end = utc_now_iso()
        account_con.execute(
            "INSERT OR REPLACE INTO cycle_runs "
            "(ts_start, ts_end, job_id, profile, state_before, state_after, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, ts_end, "collect_slow", "live", None, None, None),
        )
        account_con.commit()
        account_con.close()
    except Exception as e:
        print(f"[collect_slow] cycle_runs write failed: {e}", flush=True)

    print(
        json.dumps(
            {
                "ts": ts,
                "wrote": {
                    "klines": kline_rows,
                    "cross_market": 0 if "cross_market" in degraded else 1,
                    "coin_sentiment": sentiment_rows,
                },
                "symbols_count": len(all_symbols),
                "degraded": degraded,
                "proxy": os.environ.get("OKX_PROXY_URL", "none"),
            },
            ensure_ascii=False,
        )
    )
    # exit 2 = 部分块降级（klines/cross_market/sentiment 有失败）；wrapper 映射为 slow='degraded'。
    return 2 if degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
