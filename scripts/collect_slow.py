# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from _http import TokenBucket, get_json, load_coingecko_key, load_fred_key, make_client
from _okxcli import okx_json
from _okx_http import fetch_candles_batch_sync, fetch_instruments_sync
from _okxcli import okx_json

DEFAULT_DB_ROOT = Path(r"E:\OKX\db")
FRED_SERIES = {
    "dxy": "DTWEXBGS",
    "vix": "VIXCLS",
    "spx": "SP500",
}
# 入库时再补 -USDT-SWAP 后缀（见 collect_coin_sentiment 末尾 f"{ccy}-USDT-SWAP"）。
SYMBOLS = None  # None = 收集所有币种，不再过滤

# Job E (slow): 长周期 K 线时间框架 (2026-04-22)
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
        candidates = [
            Path(__file__).resolve().parents[2] / "mx-data" / "mx_data.py",
            Path.home() / ".openclaw" / "workspace" / "skills" / "mx-data" / "mx_data.py",
        ]
        mx_data_path = next((p for p in candidates if p.exists()), None)
        if mx_data_path is None:
            print("[WARN] mx_data.py not found, gold ETF skipped", flush=True)
            return None
        if not os.environ.get("MX_APIKEY"):
            try:
                cfg = (Path(__file__).resolve().parents[1] / "config.md").read_text(encoding="utf-8")
                import re
                m = re.search(r"###\s+4\.4 妙想资讯.*?\|\s*API Key\s*\|\s*([^|`\s][^|`]*)\s*\|", cfg, re.S)
                if m:
                    mx_key = m.group(1).strip()
                    if mx_key and not mx_key.startswith("<REDACTED_"):
                        os.environ["MX_APIKEY"] = mx_key
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
    for v in macd_line:
        if v is None:
            signal_series.append(None)
        else:
            if sig_idx < len(signal_ema) and signal_ema[sig_idx] is not None:
                signal_series.append(signal_ema[sig_idx])
                sig_idx += 1
            else:
                signal_series.append(None)

    hist_series: list[float | None] = []
    for macd, sig in zip(macd_line, signal_series):
        if macd is None or sig is None:
            hist_series.append(None)
        else:
            hist_series.append(macd - sig)

    ma5_series  = ema_series(closes, 5)
    ma20_series = ema_series(closes, 20)

    # RSI
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    rsi_series: list[float | None] = [None] * 14
    gain = [d if d > 0 else 0.0 for d in deltas[:14]]
    loss = [-d if d < 0 else 0.0 for d in deltas[:14]]
    avg_gain = sum(gain) / 14 if gain else 0.0
    avg_loss = sum(loss) / 14 if loss else 0.0
    for i in range(14, len(deltas)):
        avg_gain = (avg_gain * 13 + (deltas[i] if deltas[i] > 0 else 0.0)) / 14
        avg_loss = (avg_loss * 13 + (-deltas[i] if deltas[i] < 0 else 0.0)) / 14
    for i in range(14, len(deltas) + 1):
        rs = avg_gain / avg_loss if avg_loss != 0 else 0.0
        rsi_series.append(100.0 - (100.0 / (1.0 + rs)))

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
    return symbols


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
    payload = get_json(
        client,
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 2,
        },
        bucket=bucket,
    )
    observations = [to_float(item.get("value")) for item in payload.get("observations", [])]
    values = [item for item in observations if item is not None]
    if not values:
        return None, None
    latest = values[0]
    prev = values[1] if len(values) > 1 else None
    if prev in (None, 0):
        return latest, None
    return latest, (latest - prev) / prev


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


def _fetch_btc_etf_flow_proxy(bucket: TokenBucket, api_key: str) -> float | None:
    """Fetch BTC 24h market-cap change (USD) from CoinGecko as a proxy for ETF flow.
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
        print(f"[INFO] BTC ETF flow proxy (market_cap_change_24h_usd): {change}", flush=True)
        return change
    except Exception as e:
        print(f"[WARN] BTC ETF flow proxy fetch failed: {e}", flush=True)
        return None


def collect_coin_sentiment(news_con: sqlite3.Connection) -> int:
    payload = okx_json("news", "sentiment-rank", "--period", "24h", "--limit", "50")
    inserted = 0
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
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="OKX Job E: slow data collector.")
    parser.add_argument("--db-root", default=str(DEFAULT_DB_ROOT))
    args = parser.parse_args()
    db_root = Path(args.db_root)
    ts = utc_now_iso()

    market_con = open_db(db_root, "market.db")
    news_con = open_db(db_root, "news.db")

    # Dynamically discover all live USDT-M SWAP symbols
    try:
        all_symbols = _fetch_all_swap_symbols()
        print(f"[collect_slow] Discovered {len(all_symbols)} USDT-M SWAP contracts", flush=True)
    except Exception as e:
        print(f"[collect_slow] WARNING: Could not fetch symbols ({e}); using empty list", flush=True)
        all_symbols = []

    bucket = TokenBucket(rate_per_sec=0.5, capacity=2)

    # ── K-lines (1H/4H/1D/1W/1M) ───────────────────────────────────────────
    # ALL symbols get slow K-lines (full coverage for accuracy)
    try:
        kline_rows = collect_slow_klines(market_con, all_symbols)
        print(f"[collect_slow] Wrote {kline_rows} slow kline rows for {len(all_symbols)} symbols", flush=True)
    except Exception as e:
        print(f"[collect_slow] K-line collection failed: {e}", flush=True)
        kline_rows = 0

    # ── Macro data ────────────────────────────────────────────────────────────
    # Gold ETF (518880) daily return via mx-data
    gold_d1 = _fetch_gold_etf_d1()

    try:
        with make_client() as client:
            fred_key = load_fred_key()
            dxy, dxy_d1 = fred_latest(client, bucket, FRED_SERIES["dxy"], fred_key)
            vix, vix_d1 = fred_latest(client, bucket, FRED_SERIES["vix"], fred_key)
            spx, spx_d1 = fred_latest(client, bucket, FRED_SERIES["spx"], fred_key)
            tvl_total = defillama_total_tvl(client, bucket)
            try:
                cg_key = load_coingecko_key()
                cg = coingecko_global(bucket, cg_key)
                btc_etf_flow = _fetch_btc_etf_flow_proxy(bucket, cg_key)
            except Exception as e:
                print(f"[WARN] coingecko 跳过（API/key 不可达）: {e}", flush=True)
                cg = {"btc_d": None, "total_mcap_usd": None, "total_volume_24h_usd": None}
                btc_etf_flow = None

        prev_row = market_con.execute(
            "SELECT regime FROM cross_market ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        prev_regime = prev_row[0] if prev_row else "low_vol"
        market_con.execute(
            "INSERT OR REPLACE INTO cross_market "
            "(ts, dxy, gold, vix, spx, btc_etf_flow, dxy_d1, vix_d1, defillama_tvl_total, regime, "
            "btc_dominance, total_mcap_usd, total_volume_24h_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, dxy, gold_d1, vix, spx_d1, btc_etf_flow, dxy_d1, vix_d1, tvl_total, prev_regime,
             cg["btc_d"], cg["total_mcap_usd"], cg["total_volume_24h_usd"]),
        )
        market_con.commit()
    except Exception as e:
        print(f"[collect_slow] Macro/cross_market collection failed: {e}", flush=True)

    # ── Coin sentiment ────────────────────────────────────────────────────────
    try:
        sentiment_rows = collect_coin_sentiment(news_con)
    except Exception as e:
        print(f"[WARN] coin_sentiment 跳过（API 不可达）: {e}", flush=True)
        sentiment_rows = 0

    market_con.close()
    news_con.close()

    print(
        json.dumps(
            {
                "ts": ts,
                "wrote": {
                    "klines": kline_rows,
                    "cross_market": 1,
                    "coin_sentiment": sentiment_rows,
                },
                "symbols_count": len(all_symbols),
                "proxy": os.environ.get("OKX_PROXY_URL", "none"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
