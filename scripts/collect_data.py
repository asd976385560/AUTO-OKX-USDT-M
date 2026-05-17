# -*- coding: utf-8 -*-

"""

collect_data.py —— Job A 数据采集（每 10 分钟由 cron 调用）。



职责：

    1) 通过 OKX CLI 拉取 7 个 USDT 永续的 ticker / funding / OI

    2) 拉取 15m K 线并计算 MA / ATR / RSI / MACD (4H 及更长周期 K 线由 collect_slow.py 采集)

    3) 将最近一条 slow snapshot 复制到当前 cross_market 行，并刷新 regime

    4) 通过 OKX CLI 拉取重要新闻与最新新闻，写入 news.db

    5) 通过 OKX CLI 拉取账户余额与持仓，写入 account.db

    6) 更新 state.json 中的采集完成时间与最近快照摘要

    7) 在 cycle_runs 表追加一行轮次审计



绝不做的事：

    - 不调用交易 CLI（不下单）

    - 不写 daily-reports / records / trade-events



退出码：

    0 = 成功

    1 = 严重失败（DB 不可写、CLI 不可用等）

"""

from __future__ import annotations



import argparse

import hashlib

import json

import sqlite3

import sys

import traceback

from concurrent.futures import ThreadPoolExecutor

from datetime import datetime, timedelta, timezone

from pathlib import Path



from _okxcli import okx_json
from _okx_http import (
    fetch_tickers_all_sync,
    fetch_candles_batch_sync,
    fetch_funding_rates_batch_sync,
)



# 与 OKX 公共接口频率限制保持安全余量；_okxcli 内部已有 0.25s 节流




#  Dynamic: fetch ALL live USDT-M linear SWAP contracts from OKX

# Cached in-memory per process run; 310 contracts as of 2026-04.

# Filters: instType=SWAP, quoteCcy=USDT, ctType=linear, state=live

def _fetch_all_swap_symbols(cli_global_args: list[str]) -> list[str]:

    all_instruments = okx_json(

        "market", "instruments", "--instType", "SWAP", global_args=cli_global_args

    )

    symbols = [

        inst["instId"]

        for inst in all_instruments

        if inst.get("instType") == "SWAP"

        and inst.get("settleCcy") == "USDT"

        and inst.get("ctType") == "linear"

        and inst.get("state") == "live"

    ]

    return symbols





SYMBOLS: list[str] = []   # filled in main() after args parsed

# Job A (fast): 15m K-lines for ALL symbols — no TOP_N limit (2026-04-22)
TIMEFRAME_TO_BAR = {"15m": "15m"}

COIN_TO_SYMBOL: dict[str, str] = {}

DEFAULT_DB_ROOT = Path(r"E:\OKX\db")

DEFAULT_STATE_PATH = Path.home() / ".openclaw" / "workspace" / ".okx" / "state.json"

DEFAULT_PROFILE = "live"





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

    if value in (None, ""):

        return None

    try:

        return float(value)

    except (TypeError, ValueError):

        return None





def state_symbol_key(symbol: str) -> str:

    return symbol.lower().replace("-swap", "").replace("-", "_")





def build_cli_global_args(profile: str, demo: bool) -> list[str]:

    flags: list[str] = []

    if profile:

        flags.extend(["--profile", profile])

    if demo:

        flags.append("--demo")

    return flags





def build_public_cli_global_args(demo: bool) -> list[str]:

    return ["--live"] if demo else []





def open_db(db_root: Path, name: str) -> sqlite3.Connection:

    path = db_root / name

    if not path.exists():

        raise RuntimeError(f"数据库不存在：{path}（请先运行 init_db.py）")

    connection = sqlite3.connect(str(path))

    connection.execute("PRAGMA journal_mode=WAL;")

    return connection





def load_state(path: Path) -> dict:

    if not path.exists():

        return {}

    return json.loads(path.read_text(encoding="utf-8-sig"))





def write_state(path: Path, payload: dict) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(path.suffix + ".tmp")

    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    temp_path.replace(path)





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

    closes = [row["c"] for row in candles]

    highs = [row["h"] for row in candles]

    lows = [row["l"] for row in candles]



    ema12 = ema_series(closes, 12)

    ema26 = ema_series(closes, 26)

    macd_line: list[float | None] = []

    for fast, slow in zip(ema12, ema26):

        macd_line.append(None if fast is None or slow is None else fast - slow)

    signal_input = [value if value is not None else 0.0 for value in macd_line]

    signal_line = ema_series(signal_input, 9)



    previous_close: float | None = None

    gains: list[float] = []

    losses: list[float] = []

    tr_values: list[float] = []

    enriched: list[dict] = []

    for index, candle in enumerate(candles):

        close_value = candle["c"]

        ma5 = sum(closes[index - 4:index + 1]) / 5.0 if index >= 4 else None

        ma20 = sum(closes[index - 19:index + 1]) / 20.0 if index >= 19 else None



        if previous_close is None:

            tr = highs[index] - lows[index]

            change = 0.0

        else:

            tr = max(

                highs[index] - lows[index],

                abs(highs[index] - previous_close),

                abs(lows[index] - previous_close),

            )

            change = close_value - previous_close

        previous_close = close_value

        tr_values.append(tr)

        atr14 = sum(tr_values[-14:]) / 14.0 if len(tr_values) >= 14 else None



        gains.append(max(change, 0.0))

        losses.append(abs(min(change, 0.0)))

        if len(gains) >= 14:

            avg_gain = sum(gains[-14:]) / 14.0

            avg_loss = sum(losses[-14:]) / 14.0

            if avg_loss == 0:

                rsi14 = 100.0

            else:

                rs = avg_gain / avg_loss

                rsi14 = 100.0 - (100.0 / (1.0 + rs))

        else:

            rsi14 = None



        macd_hist = None

        if macd_line[index] is not None and signal_line[index] is not None:

            macd_hist = macd_line[index] - signal_line[index]



        enriched.append(

            {

                **candle,

                "ma5": ma5,

                "ma20": ma20,

                "atr14": atr14,

                "rsi14": rsi14,

                "macd_hist": macd_hist,

            }

        )

    return enriched






# K-line + indicator treatment limited to top ~46 by vol24h to stay within 10-minute execution window.












def collect_tickers(market_con: sqlite3.Connection, ts: str) -> tuple[int, dict[str, dict]]:

    # ── Batch HTTP (fast): tickers + funding rates ──────────────────────────
    all_tickers_raw = fetch_tickers_all_sync()

    # Index by instId for O(1) lookup
    ticker_map = {item.get("instId"): item for item in all_tickers_raw}

    # Funding rates: HTTP batch concurrent (was 292 CLI subprocess calls)
    funding_map: dict[str, dict] = fetch_funding_rates_batch_sync(SYMBOLS)

    # ── OI: skip (oiCcy/oiUsd not used in compute_regime; DB col kept NULL) ─

    tick_rows: list[tuple] = []
    derivative_rows: list[tuple] = []
    snapshot: dict[str, dict] = {}
    for symbol in SYMBOLS:
        ticker = ticker_map.get(symbol, {})
        funding = funding_map.get(symbol, {})
        tick_rows.append(
            (
                ts,
                symbol,
                to_float(ticker.get("last")),
                to_float(ticker.get("bidPx")),
                to_float(ticker.get("askPx")),
                to_float(ticker.get("vol24h")),
                to_float(funding.get("fundingRate")),
                None,  # oi (not in ticker response; set NULL)
            )
        )
        derivative_rows.append(
            (
                ts,
                symbol,
                to_float(funding.get("fundingRate")),
                ms_to_iso(funding.get("fundingTime")),
                ms_to_iso(funding.get("nextFundingTime")),
                to_float(funding.get("premium")),
                None,  # oi
                None,  # oiCcy
                None,  # oiUsd (not in ticker; set NULL)
            )
        )
        snapshot[symbol] = {
            "last": to_float(ticker.get("last")),
            "bid": to_float(ticker.get("bidPx")),
            "ask": to_float(ticker.get("askPx")),
            "vol24h": to_float(ticker.get("vol24h")),
            "fundingRate": to_float(funding.get("fundingRate")),
            "oiUsd": None,
        }

    market_con.executemany(
        "INSERT OR REPLACE INTO tick_snapshots "
        "(ts, symbol, last, bid, ask, vol24h, fundingRate, oi) VALUES (?,?,?,?,?,?,?,?)",
        tick_rows,
    )
    market_con.executemany(
        "INSERT OR REPLACE INTO derivatives "
        "(ts, symbol, funding_rate, funding_time, next_funding_time, premium, oi, oi_ccy, oi_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        derivative_rows,
    )
    market_con.commit()
    return len(tick_rows), snapshot


def collect_klines(market_con: sqlite3.Connection, kline_symbols: list[str]) -> int:
    """15m K-lines for ALL symbols (no volume filter).

    Longer timeframes (1H/4H/D/W/M/Y) are collected by collect_slow.py (Job E).
    (2026-04-22: removed TOP_N restriction -- all symbols now get 15m K-lines)
    """

    # Fetch all klines concurrently via HTTP (was 292 CLI subprocess calls)
    kline_data: dict[str, list[list]] = {}
    for tf, bar in TIMEFRAME_TO_BAR.items():
        batch = fetch_candles_batch_sync(kline_symbols, bar, limit=60)
        for sym, candles in batch.items():
            kline_data.setdefault(sym, []).extend(candles)

    rows_to_write: list[tuple] = []

    for symbol, raw_candles in kline_data.items():
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
                    TIMEFRAME_TO_BAR["15m"],
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


def compute_regime(market_con: sqlite3.Connection) -> str | None:
    """Compute market regime (low_vol / trend_up / trend_down / range) from 4H K-lines.

    4H K-lines are written by collect_slow.py (Job E). This function reads from
    kline_cache so it can be called from Job A; regime will be None until Job E
    has run at least once and populated 4H data.
    """

    try:

        rows = market_con.execute(

            """

            SELECT ts, h, l, c FROM kline_cache

            WHERE symbol = ? AND tf = '4H'

            ORDER BY ts DESC LIMIT 30

            """,

            ("BTC-USDT-SWAP",),

        ).fetchall()

    except sqlite3.OperationalError:

        return None

    if len(rows) < 21:

        return None

    rows = list(reversed(rows))

    highs = [to_float(row[1]) for row in rows]

    lows = [to_float(row[2]) for row in rows]

    closes = [to_float(row[3]) for row in rows]

    if any(value is None for value in highs + lows + closes):

        return None



    trs: list[float] = []

    for index in range(1, len(closes)):

        tr = max(

            highs[index] - lows[index],

            abs(highs[index] - closes[index - 1]),

            abs(lows[index] - closes[index - 1]),

        )

        trs.append(tr)

    if len(trs) < 14:

        return None

    atr_series = [sum(trs[index - 14:index]) / 14.0 for index in range(14, len(trs) + 1)]

    current_atr = atr_series[-1]

    sorted_atr = sorted(atr_series)

    p30_index = max(0, int(len(sorted_atr) * 0.30) - 1)

    p30 = sorted_atr[p30_index]

    if current_atr <= p30:

        return "low_vol"



    ma20 = sum(closes[-20:]) / 20.0

    last3 = closes[-3:]

    if all(close > ma20 for close in last3):

        return "trend_up"

    if all(close < ma20 for close in last3):

        return "trend_down"

    return "range"





def collect_cross_market(market_con: sqlite3.Connection, ts: str, regime: str | None) -> tuple[int, dict]:

    latest = market_con.execute(

        "SELECT dxy, gold, vix, spx, btc_etf_flow, dxy_d1, vix_d1, defillama_tvl_total, "

        "btc_dominance, total_mcap_usd, total_volume_24h_usd "

        "FROM cross_market ORDER BY ts DESC LIMIT 1"

    ).fetchone()

    payload = latest or (None, None, None, None, None, None, None, None, None, None, None)

    market_con.execute(

        "INSERT OR REPLACE INTO cross_market "

        "(ts, dxy, gold, vix, spx, btc_etf_flow, dxy_d1, vix_d1, defillama_tvl_total, regime, "

        "btc_dominance, total_mcap_usd, total_volume_24h_usd) "

        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",

        (ts, *payload[:8], regime, *payload[8:]),

    )

    market_con.commit()

    snapshot = {

        "dxy": payload[0],

        "gold": payload[1],

        "vix": payload[2],

        "spx": payload[3],

        "btc_etf_flow": payload[4],

        "dxy_d1": payload[5],

        "vix_d1": payload[6],

        "defillama_tvl_total": payload[7],

        "btc_dominance": payload[8],

        "total_mcap_usd": payload[9],

        "total_volume_24h_usd": payload[10],

        "regime": regime,

    }

    return 1, snapshot





def sentiment_score(sentiments: list[dict], symbol: str | None) -> float | None:

    if not sentiments:

        return None

    label_map = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}

    values: list[float] = []

    for item in sentiments:

        if symbol is not None and item.get("ccy") not in (None, "", symbol.split("-")[0]):

            continue

        label = item.get("sentiment") or item.get("label")

        if label in label_map:

            values.append(label_map[label])

    if not values:

        return None

    return sum(values) / len(values)





def upsert_news_item(news_con: sqlite3.Connection, item: dict, ts: str, level: str, symbol: str | None) -> int:

    unique_seed = f"{item.get('id', '')}|{symbol or ''}|{item.get('title', '')}|{ts}"

    hash_value = hashlib.sha1(unique_seed.encode("utf-8")).hexdigest()

    source = ",".join(item.get("platformList") or []) or "okx-news"

    news_con.execute(

        "INSERT OR IGNORE INTO news_items (ts, source, hash, level, symbol, title, url, sentiment, raw) "

        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",

        (

            ts,

            source,

            hash_value,

            level,

            symbol,

            item.get("title") or "",

            item.get("sourceUrl"),

            sentiment_score(item.get("ccySentiments") or [], symbol),

            json.dumps(item, ensure_ascii=False),

        ),

    )

    row = news_con.execute("SELECT id FROM news_items WHERE hash = ?", (hash_value,)).fetchone()

    if row and symbol is not None:

        news_con.execute(

            "INSERT OR IGNORE INTO news_events_index (symbol, ts, news_id) VALUES (?, ?, ?)",

            (symbol, ts, row[0]),

        )

    return 1 if row else 0





def collect_news(news_con: sqlite3.Connection, cli_global_args: list[str]) -> int:

    important = okx_json("news", "important", "--limit", "20", global_args=cli_global_args)

    latest = okx_json("news", "latest", "--coins", ",".join(COIN_TO_SYMBOL), "--limit", "30", global_args=cli_global_args)

    details = list(important.get("details") or []) + list(latest.get("details") or [])



    inserted = 0

    for item in details:

        cTime_iso = ms_to_iso(item.get("cTime"))

        if cTime_iso is None:

            print(

                f"[WARN] collect_news: 新闻缺失 cTime，回退为当前 UTC 时间 (title={str(item.get('title') or '')[:60]!r})",

                file=sys.stderr,

            )

        ts = cTime_iso or utc_now_iso()

        importance = (item.get("importance") or "").lower()

        level = "A" if importance == "high" else "B" if importance == "medium" else "C"

        mapped_symbols = [COIN_TO_SYMBOL[ccy] for ccy in item.get("ccyList") or [] if ccy in COIN_TO_SYMBOL]

        targets = mapped_symbols or [None]

        for symbol in targets:

            inserted += upsert_news_item(news_con, item, ts, level, symbol)



    news_con.commit()

    return inserted





def normalize_profile_label(profile: str | None) -> str:

    """将 OKX CLI profile 名归一化为 'live' / 'demo'。"""

    if not profile:

        return "live"

    return "demo" if "demo" in str(profile).lower() else "live"





def collect_account(account_con: sqlite3.Connection, ts: str, cli_global_args: list[str], profile: str) -> tuple[int, dict]:

    profile_label = normalize_profile_label(profile)

    balance_rows = okx_json("account", "balance", global_args=cli_global_args)

    positions = okx_json("account", "positions", "--instType", "SWAP", global_args=cli_global_args)

    balance = balance_rows[0] if balance_rows else {}

    details = balance.get("details") or []

    usdt_row = next((row for row in details if row.get("ccy") == "USDT"), {})

    upl = to_float(balance.get("upl"))

    if upl is None:

        upl = sum(to_float(row.get("upl")) or 0.0 for row in positions)



    account_con.execute(

        "INSERT OR REPLACE INTO account_snapshots "

        "(ts, profile, totalEq, availBal, upl, daily_pnl, week_pnl, month_pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",

        (

            ts,

            profile_label,

            to_float(balance.get("totalEq")),

            to_float(usdt_row.get("availBal") or usdt_row.get("availEq")),

            upl,

            None,

            None,

            None,

        ),

    )



    # 同 ts + profile 的旧 position 行先清掉，避免上一轮残留；新一轮无持仓时也表示"该 profile 此刻无持仓"

    account_con.execute(

        "DELETE FROM position_snapshots WHERE ts = ? AND profile = ?",

        (ts, profile_label),

    )

    position_rows = []

    for item in positions:

        symbol = item.get("instId")

        if symbol not in SYMBOLS:

            continue

        pos_side = (item.get("posSide") or "").lower()

        side = pos_side if pos_side in {"long", "short"} else ("long" if (to_float(item.get("pos")) or 0.0) >= 0 else "short")

        position_rows.append(

            (

                ts,

                profile_label,

                symbol,

                side,

                abs(to_float(item.get("pos")) or 0.0),

                to_float(item.get("avgPx") or item.get("markPx")),

                to_float(item.get("lever")),

                to_float(item.get("liqPx")),

                to_float(item.get("upl")),

                to_float(item.get("mgnRatio")),

            )

        )

    if position_rows:

        account_con.executemany(

            "INSERT OR REPLACE INTO position_snapshots "

            "(ts, profile, symbol, side, sz, avgPx, lev, liqPx, upl, marginRatio) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",

            position_rows,

        )



    account_con.commit()

    snapshot = {

        "totalEq": to_float(balance.get("totalEq")),

        "availBal": to_float(usdt_row.get("availBal") or usdt_row.get("availEq")),

        "upl": upl,

        "positionCount": len(position_rows),

    }

    return 1 + len(position_rows), snapshot





def write_cycle_run(account_con: sqlite3.Connection, ts_start: str, ts_end: str, error: str | None, profile: str) -> None:

    account_con.execute(

        "INSERT OR REPLACE INTO cycle_runs "

        "(ts_start, ts_end, job_id, profile, state_before, state_after, error) VALUES (?, ?, ?, ?, ?, ?, ?)",

        (ts_start, ts_end, "collect", profile, None, None, error),

    )

    account_con.commit()





def update_state(state_path: Path, ts: str, ticker_snapshot: dict[str, dict], cross_market: dict, account_snapshot: dict, regime: str | None) -> None:

    state = load_state(state_path)

    state["last_collection_utc"] = ts

    pipeline = state.setdefault("pipeline", {})

    last_success = pipeline.setdefault("last_success_utc", {})

    last_success["collect_data"] = ts



    state["current_regime"] = cross_market.get("regime")

    state["regime_updated_utc"] = ts if regime is not None else state.get("regime_updated_utc")



    ticker_summary: dict[str, float] = {}

    for symbol, payload in ticker_snapshot.items():

        prefix = state_symbol_key(symbol)

        if payload.get("last") is not None:
            ticker_summary[prefix + ".last"] = float(payload["last"])
            ticker_summary[prefix + ".vol_24h"] = float(payload.get("vol_24h", 0))

    if ticker_summary:


        state["last_ticker_snapshot"] = ticker_summary



    state["last_cross_market_snapshot"] = {

        key: value

        for key, value in cross_market.items()

        if value is not None or key == "regime"

    }

    state["account_summary"] = {

        **(state.get("account_summary") or {}),

        "totalEq": account_snapshot.get("totalEq"),

        "availBal": account_snapshot.get("availBal"),

        "ccy": "USDT",

        "pos": account_snapshot.get("positionCount"),

    }

    write_state(state_path, state)





MX_SYMBOL_KEYWORDS = {

    "BTC": ("BTC", "比特币", "Bitcoin"),

    "ETH": ("ETH", "以太坊", "Ethereum"),

    "SOL": ("SOL", "Solana"),

    "OKB": ("OKB", "OKX平台币", "欧易平台币"),

    "DOGE": ("DOGE", "狗狗币", "Dogecoin"),

    "TRUMP": ("TRUMP", "TRUM", "Trump"),

    "ALLO": ("ALLO", "Allo"),

}

MX_LEVEL_A_KEYWORDS = ("暴涨", "暴跌", "崩盘", "重大", "破纪录", "历史新高", "急跌", "ETF", "降息", "加息")





def _parse_mx_date(s: str) -> str:

    """妙想新闻 date 字段假设为 UTC+8，转为 UTC ISO8601 'Z' 后缀。见 README 时区约定。"""

    try:

        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

        return (

            dt.replace(tzinfo=timezone(timedelta(hours=8)))

            .astimezone(timezone.utc)

            .strftime("%Y-%m-%dT%H:%M:%SZ")

        )

    except Exception:

        return utc_now_iso()





def _detect_mx_symbol(text: str) -> str | None:

    t = text or ""

    for code, kws in MX_SYMBOL_KEYWORDS.items():

        if any(kw in t for kw in kws):

            return COIN_TO_SYMBOL.get(code)

    return None





def collect_miaoxiang_news(news_con: sqlite3.Connection, query: str = "加密货币最新消息") -> int:
    """
    妙想资讯 (Dfcfs) 新闻采集。MX_APIKEY 缺失 / API 不可用 → 返回 0。

    每轮预算：每 15min 一次，一天 96 次，远低于妙想 500/天 额度。
    返回本轮实际插入的行数（已去重）。
    """
    import os
    import httpx

    api_key = os.getenv("MX_APIKEY")
    if not api_key:
        return 0
    try:
        with httpx.Client(trust_env=False, timeout=30.0) as cli:
            r = cli.post(
                "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search",
                headers={"apikey": api_key, "Content-Type": "application/json"},
                json={"query": query},
            )
            r.raise_for_status()
            payload = r.json()
    except Exception as exc:
        print(f"[collect_data] miaoxiang fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0

    items = (
        ((payload or {}).get("data") or {}).get("data", {}).get("llmSearchResponse", {}).get("data")
        or []
    )

    # Count before
    before = news_con.execute(
        "SELECT COUNT(*) FROM news_items WHERE source=?", ("mx-search",)
    ).fetchone()[0]

    for item in items:
        code = (item.get("code") or "").strip()
        title = (item.get("title") or "").strip()
        if not code or not title:
            continue
        ts_iso = _parse_mx_date(item.get("date") or "")
        content = item.get("content") or ""
        symbol = _detect_mx_symbol(title + " " + content)
        level = "A" if any(k in title for k in MX_LEVEL_A_KEYWORDS) else "B"
        hash_value = hashlib.sha1(f"mx|{code}".encode("utf-8")).hexdigest()
        news_con.execute(
            "INSERT OR IGNORE INTO news_items (ts, source, hash, level, symbol, title, url, sentiment, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts_iso,
                "mx-search",
                hash_value,
                level,
                symbol,
                title,
                item.get("jumpUrl"),
                None,
                json.dumps(item, ensure_ascii=False),
            ),
        )
    news_con.commit()

    after = news_con.execute(
        "SELECT COUNT(*) FROM news_items WHERE source=?", ("mx-search",)
    ).fetchone()[0]
    return after - before




_GEO_POLITICAL_QUERIES = [

    "地缘局势最新消息",

    "国际重大事件最新",

    "中东局势最新动态",

    "中美关系最新消息",

    "俄乌战争最新进展",

    "朝鲜半島局势最新",

    "全球宏观风险事件",

]



# Level A keywords for geopolitical news

_GEO_LEVEL_A_KEYWORDS = [

    "战争", "军事", "冲突", "制辕", "核", "导射",

    "经济危机", "金融风险", "黑天鹿", "主权债务",

    "革命", "政叜", "紧急状态",

]



def collect_geopolitical_news(news_con: sqlite3.Connection) -> int:

    """

    采集国际地缘政治新闻，写入 news_items(source='geo-political')。

    MX_APIKEY 缺失时返回 0。



    每轮使用 7 个地缘关键词查询，每次最多 15 条。

    Level A 判断：标题含 _GEO_LEVEL_A_KEYWORDS 或 MX_LEVEL_A_KEYWORDS 均触发。

    不关联具体币种（symbol=None）。

    """

    import os, httpx



    api_key = os.getenv("MX_APIKEY")

    if not api_key:

        return 0



    total_inserted = 0

    all_items = []



    try:

        with httpx.Client(trust_env=False, timeout=30.0) as cli:

            for query in _GEO_POLITICAL_QUERIES:

                try:

                    r = cli.post(

                        "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search",

                        headers={"apikey": api_key, "Content-Type": "application/json"},

                        json={"query": query},

                    )

                    r.raise_for_status()

                    payload = r.json()

                    items = (

                        ((payload or {}).get("data") or {})

                        .get("data", {})

                        .get("llmSearchResponse", {})

                        .get("data")

                        or []

                    )

                    all_items.extend(items)

                except Exception as exc:

                    print(

                        f"[collect_data] geo-political query '{query}' failed: {exc}",

                        file=sys.stderr,

                    )

    except Exception as exc:

        print(f"[collect_data] geo-political news fetch failed: {exc}", file=sys.stderr)

        return 0



    seen_codes = set()

    for item in all_items:

        code = (item.get("code") or "").strip()

        title = (item.get("title") or "").strip()

        if not code or not title:

            continue

        if code in seen_codes:

            continue

        seen_codes.add(code)



        ts_iso = _parse_mx_date(item.get("date") or "")

        content = item.get("content") or ""

        level = "A" if any(

            k in title

            for k in list(_GEO_LEVEL_A_KEYWORDS) + list(MX_LEVEL_A_KEYWORDS)

        ) else "B"

        hash_value = hashlib.sha1(f"geo|{code}".encode("utf-8")).hexdigest()


        total_inserted += 1  # INSERT OR IGNORE; duplicates silently ignored


    news_con.commit()

    return total_inserted







def main() -> int:

    parser = argparse.ArgumentParser(description="OKX Job A: data collection.")

    parser.add_argument("--db-root", default=str(DEFAULT_DB_ROOT))

    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))

    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="记录在 cycle_runs/profile 与 summary/profile 中的执行 profile")

    parser.add_argument("--demo", action="store_true", help="通过 OKX CLI 的全局 demo 参数采集账户与市场数据")

    parser.add_argument("--skip-news", action="store_true", help="本轮跳过新闻拉取")

    args = parser.parse_args()



    db_root = Path(args.db_root)

    state_path = Path(args.state_path)

    ts_start = utc_now_iso()

    public_cli_global_args = build_public_cli_global_args(args.demo)

    account_cli_global_args = build_cli_global_args(args.profile, args.demo)



    #  Dynamically fetch all live USDT-M linear SWAP symbols

    global SYMBOLS, COIN_TO_SYMBOL

    try:

        fetched = _fetch_all_swap_symbols(public_cli_global_args)

        SYMBOLS = sorted(fetched)

        COIN_TO_SYMBOL = {s.split("-")[0]: s for s in SYMBOLS}

        print(f"[collect_data] Discovered {len(SYMBOLS)} USDT-M SWAP contracts", file=sys.stderr)

    except Exception as sym_exc:

        print(f"[collect_data] WARNING: Could not fetch dynamic symbols ({sym_exc}); using empty list", file=sys.stderr)

        SYMBOLS = []

        COIN_TO_SYMBOL = {}

    #



    summary = {"profile": args.profile, "demo": args.demo, "ts_start": ts_start, "symbols_count": len(SYMBOLS), "wrote": {}}

    warnings: list[str] = []

    error: str | None = None

    market_con = news_con = account_con = None

    try:

        market_con = open_db(db_root, "market.db")

        news_con = open_db(db_root, "news.db")

        account_con = open_db(db_root, "account.db")



        tick_count, ticker_snapshot = collect_tickers(market_con, ts_start)

        summary["wrote"]["tickers"] = tick_count

        # Job A: ALL symbols get 15m K-lines (full coverage for accuracy)
        top_kline_symbols = SYMBOLS

        summary["wrote"]["klines"] = collect_klines(market_con, top_kline_symbols)

        regime = compute_regime(market_con)

        summary["regime"] = regime

        cross_count, cross_snapshot = collect_cross_market(market_con, ts_start, regime)

        summary["wrote"]["cross_market"] = cross_count

        if args.skip_news:

            summary["wrote"]["news"] = 0

            summary["wrote"]["news_mx"] = 0

            summary["wrote"]["news_geo"] = 0

        else:

            try:

                summary["wrote"]["news"] = collect_news(news_con, account_cli_global_args)

            except Exception as news_exc:  # noqa: BLE001

                summary["wrote"]["news"] = 0

                warnings.append(f"news_degraded: {type(news_exc).__name__}: {news_exc}")

            try:

                summary["wrote"]["news_mx"] = collect_miaoxiang_news(news_con)

            except Exception as mx_exc:  # noqa: BLE001

                summary["wrote"]["news_mx"] = 0

                warnings.append(f"miaoxiang_degraded: {type(mx_exc).__name__}: {mx_exc}")

            try:

                summary["wrote"]["news_geo"] = collect_geopolitical_news(news_con)

            except Exception as geo_exc:  # noqa: BLE001

                summary["wrote"]["news_geo"] = 0

                warnings.append(f"geo_political_degraded: {type(geo_exc).__name__}: {geo_exc}")

        try:

            account_count, account_snapshot = collect_account(account_con, ts_start, account_cli_global_args, args.profile)

            summary["wrote"]["account"] = account_count

        except Exception as acc_exc:

            account_count = 0

            account_snapshot = {}

            warnings.append(f"account_degraded: {type(acc_exc).__name__}: {acc_exc}")

        update_state(state_path, ts_start, ticker_snapshot, cross_snapshot, account_snapshot, regime)

    except Exception as exc:  # noqa: BLE001

        error = f"{type(exc).__name__}: {exc}"

        traceback.print_exc(file=sys.stderr)

    finally:

        ts_end = utc_now_iso()

        try:

            if account_con is not None:

                write_cycle_run(account_con, ts_start, ts_end, error, args.profile)

        except sqlite3.Error as exc:

            print(f"[collect_data] cycle_runs 写入失败: {exc}", file=sys.stderr)

        for connection in (market_con, news_con, account_con):

            if connection is not None:

                try:

                    connection.close()

                except sqlite3.Error:

                    pass



    summary["ts_end"] = utc_now_iso()

    summary["error"] = error

    if warnings:

        summary["warnings"] = warnings

    print(json.dumps(summary, ensure_ascii=False))

    return 1 if error else 0





if __name__ == "__main__":

    raise SystemExit(main())