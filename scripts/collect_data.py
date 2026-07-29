# -*- coding: utf-8 -*-



"""



collect_data.py —— Job A 数据采集（每 15 分钟由 cron 调用）。







职责：



    1) 通过 OKX公共接口拉取全量 USDT永续 ticker / funding / OI



    2) 拉取 15m K 线并计算 MA / ATR / RSI / MACD (4H 及更长周期 K 线由 collect_slow.py 采集)



    3) 读取最近一条 slow snapshot（cross_market由慢采单写）



    4) 不采集账户/持仓；该职责由 jobb_live_account_check.py 单写



    5) 更新 state.json 中的采集完成时间与最近快照摘要



    6) 采集轮次审计由 fast_collect/ledger 统一维护



    新闻由 registry adapters 独立采集并统一经 collectors/news_writer.py 落库。







绝不做的事：



    - 不调用交易 CLI（不下单）



    - 不写 account.db / daily-reports / records / trade-events







退出码：



    0 = 成功



    1 = 严重失败（DB 不可写、CLI 不可用等）



"""



from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))








import argparse






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

    fetch_open_interest_all_sync,

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



DEFAULT_DB_ROOT = Path(_project_path('db'))



DEFAULT_STATE_PATH = Path.home() / ".openclaw" / "workspace" / ".okx" / "state.json"



DEFAULT_PROFILE = "live"











def utc_now_iso() -> str:



    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_CST_TZ = timezone(timedelta(hours=8))


def cst_now_str() -> str:
    """ingested_at 使用 CST 'YYYY-MM-DD HH:MM:SS'，与 news_writer 对齐。"""
    return datetime.now(_CST_TZ).strftime("%Y-%m-%d %H:%M:%S")


def iso_to_cst_str(iso_ts: str | None) -> str | None:
    """UTC-Z/ISO 时刻 → CST 'YYYY-MM-DD HH:MM:SS'；缺失/解析失败返 None（禁 fallback now）。"""
    if not iso_ts:
        return None
    try:
        t = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(_CST_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None











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



    """Compute MA/ATR/RSI/MACD for candle list.



    v5.3.2: MA uses EMA (not SMA), ATR/RSI use Wilder's smoothing,

    matching collect_slow.py for consistent signals across timeframes.

    """

    if not candles:

        return candles



    closes = [row["c"] for row in candles]

    highs  = [row["h"] for row in candles]

    lows   = [row["l"] for row in candles]



    # ---- MA5 / MA20: EMA (match collect_slow.py) ----

    ma5_series  = ema_series(closes, 5)

    ma20_series = ema_series(closes, 20)



    # ---- MACD: EMA12 - EMA26, signal = EMA9(MACD) ----

    ema12 = ema_series(closes, 12)

    ema26 = ema_series(closes, 26)

    macd_line: list[float | None] = []

    for a, b in zip(ema12, ema26):

        macd_line.append(None if a is None or b is None else a - b)



    # Fix: don't replace None with 0.0 before EMA (distorts signal)

    macd_none_count = sum(1 for v in macd_line if v is None)

    macd_valid = [v for v in macd_line if v is not None]

    if macd_valid:

        signal_valid = ema_series(macd_valid, 9)

        signal_line = [None] * macd_none_count + signal_valid

    else:

        signal_line = [None] * len(macd_line)



    hist_series: list[float | None] = []

    for m, s in zip(macd_line, signal_line):

        hist_series.append(None if m is None or s is None else m - s)



    # ---- ATR14: Wilder's smoothing (match collect_slow.py) ----

    trs: list[float] = []

    for i in range(1, len(closes)):

        tr = max(

            highs[i] - lows[i],

            abs(highs[i] - closes[i - 1]),

            abs(lows[i] - closes[i - 1]),

        )

        trs.append(tr)

    atr_series: list[float | None] = [None] * 14

    if len(trs) >= 14:

        atr_series.append(sum(trs[0:14]) / 14.0)

        for i in range(14, len(trs)):

            atr_series.append((atr_series[-1] * 13 + trs[i]) / 14.0)



    # ---- RSI14: Wilder's smoothing (match collect_slow.py) ----

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    rsi_series: list[float | None] = [None] * 14

    if len(deltas) >= 14:

        gain = [d if d > 0 else 0.0 for d in deltas[:14]]

        loss = [-d if d < 0 else 0.0 for d in deltas[:14]]

        avg_gain = sum(gain) / 14.0

        avg_loss = sum(loss) / 14.0

        for i in range(14, len(deltas) + 1):

            rs = avg_gain / avg_loss if avg_loss != 0 else 100.0

            rsi_series.append(100.0 - (100.0 / (1.0 + rs)))

            if i < len(deltas):

                avg_gain = (avg_gain * 13 + (deltas[i] if deltas[i] > 0 else 0.0)) / 14.0

                avg_loss = (avg_loss * 13 + (-deltas[i] if deltas[i] < 0 else 0.0)) / 14.0



    # ---- Assign to candles ----

    for i, candle in enumerate(candles):

        candle["ma5"]  = ma5_series[i]  if i < len(ma5_series)  else None

        candle["ma20"] = ma20_series[i] if i < len(ma20_series) else None

        candle["atr14"] = atr_series[i] if i < len(atr_series) else None

        candle["rsi14"] = rsi_series[i] if i < len(rsi_series) else None

        candle["macd_hist"] = hist_series[i] if i < len(hist_series) else None



    return candles













# K-line + indicator treatment limited to top ~46 by vol24h to stay within 10-minute execution window.

























def collect_tickers(market_con: sqlite3.Connection, ts: str) -> tuple[int, dict[str, dict]]:



    # ── Batch HTTP (fast): tickers + funding rates ──────────────────────────

    all_tickers_raw = fetch_tickers_all_sync()



    # Index by instId for O(1) lookup

    ticker_map = {item.get("instId"): item for item in all_tickers_raw}



    # Funding rates: HTTP batch concurrent (was 292 CLI subprocess calls)

    funding_map: dict[str, dict] = fetch_funding_rates_batch_sync(SYMBOLS)

    # OI：公共端点支持 instType=SWAP 一次取全量，供分析观察 OI 24h 变化。
    try:
        oi_map: dict[str, dict] = fetch_open_interest_all_sync("SWAP")
    except Exception as oi_exc:  # 单项失败不阻断 tick/funding 主采集
        oi_map = {}
        print(f"[collect_data] OI degraded: {oi_exc}", file=sys.stderr)



    tick_rows: list[tuple] = []

    derivative_rows: list[tuple] = []

    snapshot: dict[str, dict] = {}

    for symbol in SYMBOLS:

        ticker = ticker_map.get(symbol, {})

        funding = funding_map.get(symbol, {})

        oi_row = oi_map.get(symbol, {})

        # chg24h 直接落库，供分析与推送读取 24h 涨跌幅。
        last_px = to_float(ticker.get("last"))

        open_24h = to_float(ticker.get("open24h"))

        chg24h = None

        if last_px is not None and open_24h not in (None, 0):

            chg24h = (last_px - open_24h) / open_24h * 100.0

        tick_rows.append(

            (

                ts,

                symbol,

                last_px,

                to_float(ticker.get("bidPx")),

                to_float(ticker.get("askPx")),

                to_float(ticker.get("vol24h")),

                to_float(funding.get("fundingRate")),

                to_float(oi_row.get("oi")),

                chg24h,

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

                to_float(oi_row.get("oi")),

                to_float(oi_row.get("oiCcy")),

                to_float(oi_row.get("oiUsd")),

            )

        )

        snapshot[symbol] = {

            "last": last_px,

            "bid": to_float(ticker.get("bidPx")),

            "ask": to_float(ticker.get("askPx")),

            "vol24h": to_float(ticker.get("vol24h")),

            "fundingRate": to_float(funding.get("fundingRate")),

            "oiUsd": to_float(oi_row.get("oiUsd")),

            "chg24h": chg24h,

        }



    market_con.executemany(

        "INSERT OR REPLACE INTO tick_snapshots "

        "(ts, symbol, last, bid, ask, vol24h, fundingRate, oi, chg24h) VALUES (?,?,?,?,?,?,?,?,?)",

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





def collect_klines(market_con: sqlite3.Connection, kline_symbols: list[str], prefetched: dict | None = None) -> int:

    """15m K-lines for ALL symbols (no volume filter).



    Longer timeframes (1H/4H/D/W/M/Y) are collected by collect_slow.py (Job E).

    (2026-04-22: removed TOP_N restriction -- all symbols now get 15m K-lines)

    """



    # Fetch all klines concurrently via HTTP (was 292 CLI subprocess calls).
    # 2026-06-30: 支持 main() 预取并发——传入 prefetched={sym:[candle..]} 则跳过本地抓取。

    if prefetched is not None:

        kline_data: dict[str, list[list]] = prefetched

    else:

        kline_data = {}

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
    """V2.0 (2026-06-26) Option A: cross_market 唯一写者=慢采(regime.db)。
    fast 路径不 carry-forward 写 market.db.cross_market（避免重建第二写者）。
    只从 regime.db 读最新 regime 快照喂 update_state（system_state.current_regime），不写库；
    regime.db 不可达时回退只读 market_con。返回 (0, snapshot)——0=本轮未写 cross_market。"""
    import os as _os, sys as _sys
    _sd = _os.path.dirname(_os.path.abspath(__file__))
    if _sd not in _sys.path:
        _sys.path.insert(0, _sd)
    row = None
    try:
        from _regime_read import latest_cross_market as _lcm
        _main = next((r[2] for r in market_con.execute("PRAGMA database_list").fetchall()
                      if r[1] == "main"), None)
        _db_root = _os.path.dirname(_main) if _main else _project_path('db')
        row = _lcm(_db_root)
    except Exception:
        row = None
    if not row:
        try:
            r2 = market_con.execute(
                "SELECT dxy, gold, vix, spx, btc_etf_flow, dxy_d1, vix_d1, regime, "
                "defillama_tvl_total, btc_dominance, total_mcap_usd, total_volume_24h_usd "
                "FROM cross_market ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        except Exception:
            r2 = None  # market.db.cross_market 已 DROP（2026-06-27 regime 拆库收尾）；regime.db 不可达时本轮无 regime 快照
        if r2:
            _k = ["dxy", "gold", "vix", "spx", "btc_etf_flow", "dxy_d1", "vix_d1", "regime",
                  "defillama_tvl_total", "btc_dominance", "total_mcap_usd", "total_volume_24h_usd"]
            row = dict(zip(_k, r2))
        else:
            row = {}
    copied_regime = row.get("regime") if row.get("regime") is not None else regime
    snapshot = {
        "dxy": row.get("dxy"),
        "gold": row.get("gold"),
        "vix": row.get("vix"),
        "spx": row.get("spx"),
        "btc_etf_flow": row.get("btc_etf_flow"),
        "dxy_d1": row.get("dxy_d1"),
        "vix_d1": row.get("vix_d1"),
        "defillama_tvl_total": row.get("defillama_tvl_total"),
        "btc_dominance": row.get("btc_dominance"),
        "total_mcap_usd": row.get("total_mcap_usd"),
        "total_volume_24h_usd": row.get("total_volume_24h_usd"),
        "regime": copied_regime,
    }
    return 0, snapshot











def update_state(
    state_path: Path,
    ts: str,
    ticker_snapshot: dict[str, dict],
    cross_market: dict,
    regime: str | None,
) -> None:



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



    write_state(state_path, state)











def main() -> int:



    parser = argparse.ArgumentParser(description="OKX Job A: data collection.")



    parser.add_argument("--db-root", default=str(DEFAULT_DB_ROOT))



    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))



    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="记录在 cycle_runs/profile 与 summary/profile 中的执行 profile")



    parser.add_argument("--demo", action="store_true", help="通过 OKX CLI 的全局 demo 参数采集账户与市场数据")



    parser.add_argument("--skip-news", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()







    db_root = Path(args.db_root)



    state_path = Path(args.state_path)



    ts_start = utc_now_iso()



    public_cli_global_args = build_public_cli_global_args(args.demo)



    # Account/profile reads are isolated in jobb_live_account_check.py.







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

    # instruments 拉取失败导致空 symbols 时，本轮无市场数据，必须记 error，
    # 让 fast_collect 判为 error 并阻止分析使用陈旧行情。
    if not SYMBOLS:
        error = "instruments_empty: 未取到任何 SWAP symbols，本轮无市场数据（fail-safe 阻止陈旧行情当新鲜）"



    market_con = None



    try:



        market_con = open_db(db_root, "market.db")



        # V2.0 的账户/持仓快照唯一权威写入方是
        # scripts/jobb_live_account_check.py；collect_data 仅写 market.db。







        # 2026-06-30 提速：candles 预取与 collect_tickers(含 funding 抓取) 并发——
        # _okx_http 已按端点分桶限速、两端点互不阻塞；candles 后台跑，collect_tickers
        # 同时抓 tickers+funding 并写库（market_con 只此主线程写），wall≈max 而非串行相加。
        with ThreadPoolExecutor(max_workers=1) as _cand_ex:
            _cand_fut = _cand_ex.submit(fetch_candles_batch_sync, SYMBOLS, "15m", 60)
            tick_count, ticker_snapshot = collect_tickers(market_con, ts_start)
            summary["wrote"]["tickers"] = tick_count
            _cand_data = _cand_fut.result()

        # Job A: ALL symbols get 15m K-lines (full coverage for accuracy)
        summary["wrote"]["klines"] = collect_klines(market_con, SYMBOLS, prefetched=_cand_data)



        regime = compute_regime(market_con)



        cross_count, cross_snapshot = collect_cross_market(market_con, ts_start, regime)



        # B1 修复（2026-06-11 全流程验证）：summary["regime"] 必须是实际落库值
        # （copied_regime，复制自上一行权威），而不是本地计算值——两者不一致时
        # 旧输出会误导读 stdout 的 agent（曾打 low_vol 而库里是 trend_down）。
        summary["regime"] = cross_snapshot.get("regime")

        summary["regime_computed_local"] = regime



        summary["wrote"]["cross_market"] = cross_count



        # 新闻已迁至 registry adapters；保留旧参数仅兼容现有 fast_collect 调用。
        summary["wrote"]["news"] = 0
        summary["wrote"]["news_mx"] = 0
        summary["wrote"]["news_geo"] = 0
        # 账户/持仓快照由 jobb_live_account_check.py 单独写入。
        summary["wrote"]["account"] = 0



        update_state(state_path, ts_start, ticker_snapshot, cross_snapshot, regime)



    except Exception as exc:  # noqa: BLE001



        error = f"{type(exc).__name__}: {exc}"



        traceback.print_exc(file=sys.stderr)



    finally:



        # market.db 高频表保留裁剪（架构评审 2026-07-07）：collect_data 是 fast 路径 market.db
        # 唯一 writer（单写不变量，本文件多处注释强调不重建第二写者），故 prune 用现成 market_con、
        # 关连接前做——零新增 writer。守 04:30 槽（每日一次、避开 collect_slow :02 instruments_cache
        # 写）；裁 tick/derivatives 超 45 天（kline_cache 历史深度不裁），失败不阻断采集。
        try:
            _pnow = datetime.now(timezone(timedelta(hours=8)))
            if (market_con is not None and _pnow.hour == 4 and 30 <= _pnow.minute < 45):
                if _project_path('scripts') not in sys.path:
                    sys.path.insert(0, _project_path('scripts'))
                import market_prune
                _pstat = market_prune.prune(market_con, retention_days=45, apply=True)
                print(f"[collect_data] market.db prune: {_pstat}", file=sys.stderr)
                # DELETE 只把页挂 freelist、文件不缩；market.db auto_vacuum=INCREMENTAL，跟一句
                # 增量回收把页还给 OS（不停机、单 writer、分块防 WAL 尖峰）。回收失败不阻断采集。
                _rstat = market_prune.incremental_reclaim(market_con)
                print(f"[collect_data] market.db reclaim: {_rstat}", file=sys.stderr)
        except Exception as _pe:
            print(f"[collect_data] market prune 跳过（不阻断采集）: {_pe}", file=sys.stderr)

        for connection in (market_con,):



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
