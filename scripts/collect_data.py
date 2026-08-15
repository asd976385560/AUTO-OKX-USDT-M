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







import argparse
import hashlib






import json



import sqlite3



import sys

import time






import traceback



from concurrent.futures import ThreadPoolExecutor



from datetime import datetime, timedelta, timezone



from pathlib import Path







from _okxcli import okx_json

from _kline_indicators import (
    extend_with_boll_obv,
    extended_row_tail,
    kline_insert_plan,
)
from _okx_http import (

    fetch_tickers_all_sync,

    fetch_candles_batch_sync,

    fetch_funding_rates_batch_sync,

    fetch_open_interest_all_sync,

)
from _okx_ticker_fallback import (
    fetch_tickers_all_schannel_sync,
    fetch_tickers_batch_sync,
)
from asset_class_sync import sync_asset_classes


# The all-market ticker call is a single point of failure for every symbol.
# Keep its first connection bounded, then (only before any market write) give
# an incomplete/failed response one cold retry through a newly-created client.
# If both aggregate phases still miss the 99% contract, use the remaining
# current-cycle budget for one rate-limited official single-ticker pass over
# only missing symbols. Funding runs in parallel so recovery cannot consume
# its coverage budget. No phase reads or repairs a historical slot.
TICKER_INITIAL_TIMEOUT_SECONDS = 40.0
TICKER_COLD_RETRY_TIMEOUT_SECONDS = 25.0
TICKER_INITIAL_BUDGET_SHARE = 0.35
TICKER_COLD_RETRY_BUDGET_SHARE = 0.27
TICKER_COLD_RETRY_DELAY_SECONDS = 0.75
TICKER_SINGLE_FALLBACK_TIMEOUT_SECONDS = 60.0
TICKER_SCHANNEL_RESERVE_SECONDS = 6.0
TICKER_COMPLETE_COVERAGE = 0.99
TICKER_MAX_FETCH_PHASES = 3







# 与 OKX 公共接口频率限制保持安全余量；_okxcli 内部已有 0.25s 节流









#  Dynamic: fetch ALL live USDT-M linear SWAP contracts from OKX



# Cached in-memory per process run; 310 contracts as of 2026-04.



# Filters: instType=SWAP, quoteCcy=USDT, ctType=linear, state=live



def _fetch_all_swap_instruments(cli_global_args: list[str]) -> list[dict]:



    all_instruments = okx_json(



        "market", "instruments", "--instType", "SWAP", global_args=cli_global_args



    )



    return [
        inst for inst in all_instruments
        if inst.get("instType") == "SWAP"
        and inst.get("settleCcy") == "USDT"
        and inst.get("ctType") == "linear"
        and inst.get("state") == "live"
        and inst.get("instId")
    ]


def _fetch_all_swap_symbols(cli_global_args: list[str]) -> list[str]:
    """Compatibility wrapper for callers that only need instrument IDs."""
    return [
        str(inst["instId"])
        for inst in _fetch_all_swap_instruments(cli_global_args)
    ]


def discover_live_swap_instruments(
    cli_global_args: list[str],
) -> tuple[list[dict], str | None]:
    """Discover the live universe while preserving the transport root cause.

    The caller still fails closed on an empty result.  Returning an explicit
    empty list and diagnostic keeps a network failure from being replaced by a
    secondary uninitialized-local exception in snapshot and asset-class paths.
    """
    try:
        return _fetch_all_swap_instruments(cli_global_args), None
    except Exception as exc:  # noqa: BLE001 - boundary preserves provider error
        return [], f"{type(exc).__name__}: {exc}"


def _normalize_instrument_snapshot_row(instrument: dict) -> dict:
    return {
        "symbol": str(instrument.get("instId") or "").strip().upper(),
        "list_time_utc": ms_to_iso(instrument.get("listTime")),
        "state": str(instrument.get("state") or "").strip() or None,
        "settle_ccy": str(instrument.get("settleCcy") or "").strip() or None,
        "ct_type": str(instrument.get("ctType") or "").strip() or None,
        "inst_category": (
            str(instrument.get("instCategory") or "").strip() or None
        ),
        "ct_val": to_float(instrument.get("ctVal")),
        "lot_sz": to_float(instrument.get("lotSz")),
    }


def _instrument_snapshot_hash(rows: list[dict]) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def freeze_official_instrument_snapshot(
    connection: sqlite3.Connection,
    instruments: list[dict],
    *,
    cycle_id: str,
    collected_ts_utc: str,
) -> dict:
    """Freeze the exact official live universe once per natural slot."""
    parsed_cycle = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M")
    if parsed_cycle.minute not in (0, 15, 30, 45):
        raise ValueError("cycle_id must align to a 15-minute slot")
    rows = sorted(
        (_normalize_instrument_snapshot_row(item) for item in instruments),
        key=lambda row: row["symbol"],
    )
    symbols = [row["symbol"] for row in rows]
    if not rows or any(not symbol for symbol in symbols):
        raise ValueError("official instrument snapshot is empty or invalid")
    if len(symbols) != len(set(symbols)):
        raise ValueError("official instrument snapshot contains duplicate symbols")
    payload_hash = _instrument_snapshot_hash(rows)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS official_instrument_snapshot_runs(
            cycle_id         TEXT PRIMARY KEY,
            collected_ts_utc TEXT NOT NULL,
            symbol_count     INTEGER NOT NULL,
            payload_sha256   TEXT NOT NULL,
            complete         INTEGER NOT NULL CHECK(complete IN (0,1)),
            source           TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS official_instrument_snapshot_rows(
            cycle_id      TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            list_time_utc TEXT,
            state         TEXT,
            settle_ccy    TEXT,
            ct_type       TEXT,
            inst_category TEXT,
            ct_val        REAL,
            lot_sz        REAL,
            PRIMARY KEY(cycle_id,symbol),
            FOREIGN KEY(cycle_id)
              REFERENCES official_instrument_snapshot_runs(cycle_id)
        );
        CREATE INDEX IF NOT EXISTS idx_official_instrument_snapshot_symbol
          ON official_instrument_snapshot_rows(symbol,cycle_id);
    """)
    existing = connection.execute(
        "SELECT symbol_count,payload_sha256,complete "
        "FROM official_instrument_snapshot_runs WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if existing is not None:
        existing_count = int(existing[0])
        existing_hash = str(existing[1])
        stored_payload = [
            {
                "symbol": str(row[0] or "").strip().upper(),
                "list_time_utc": row[1],
                "state": row[2],
                "settle_ccy": row[3],
                "ct_type": row[4],
                "inst_category": row[5],
                "ct_val": row[6],
                "lot_sz": row[7],
            }
            for row in connection.execute(
                "SELECT symbol,list_time_utc,state,settle_ccy,ct_type,"
                "inst_category,ct_val,lot_sz "
                "FROM official_instrument_snapshot_rows WHERE cycle_id=? "
                "ORDER BY symbol",
                (cycle_id,),
            ).fetchall()
        ]
        stored_rows = len(stored_payload)
        stored_observed_hash = _instrument_snapshot_hash(stored_payload)
        identical = (
            int(existing[2]) == 1
            and existing_count == len(rows)
            and stored_rows == len(rows)
            and existing_hash == stored_observed_hash
            and existing_hash == payload_hash
        )
        return {
            "status": "reused" if identical else "conflict",
            "cycle_id": cycle_id,
            "symbol_count": len(rows),
            "payload_sha256": payload_hash,
            "stored_symbol_count": existing_count,
            "stored_payload_sha256": existing_hash,
            "stored_observed_payload_sha256": stored_observed_hash,
            "complete": identical,
        }
    try:
        connection.execute("SAVEPOINT freeze_official_instruments")
        connection.execute(
            "INSERT INTO official_instrument_snapshot_runs VALUES(?,?,?,?,?,?)",
            (
                cycle_id, collected_ts_utc, len(rows), payload_hash, 1,
                "okx_public_instruments_live_usdt_linear_swap",
            ),
        )
        connection.executemany(
            "INSERT INTO official_instrument_snapshot_rows VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    cycle_id, row["symbol"], row["list_time_utc"],
                    row["state"], row["settle_ccy"], row["ct_type"],
                    row["inst_category"], row["ct_val"], row["lot_sz"],
                )
                for row in rows
            ],
        )
        connection.execute("RELEASE SAVEPOINT freeze_official_instruments")
        connection.commit()
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT freeze_official_instruments")
        connection.execute("RELEASE SAVEPOINT freeze_official_instruments")
        raise
    return {
        "status": "inserted",
        "cycle_id": cycle_id,
        "symbol_count": len(rows),
        "payload_sha256": payload_hash,
        "complete": True,
    }











SYMBOLS: list[str] = []   # filled in main() after args parsed



# Job A (fast): 15m K-lines for ALL symbols — no TOP_N limit (2026-04-22)

TIMEFRAME_TO_BAR = {"15m": "15m"}



COIN_TO_SYMBOL: dict[str, str] = {}



DEFAULT_DB_ROOT = Path(r".\db")



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

























def _ticker_row_usable(row: object) -> bool:
    if not isinstance(row, dict) or not str(row.get("instId") or ""):
        return False
    last_price = to_float(row.get("last"))
    return last_price is not None and last_price > 0


def _ticker_symbol_coverage(rows: list[dict]) -> float:
    """Coverage of expected symbols with a usable official last price."""
    expected = set(SYMBOLS)
    if not expected:
        return 1.0 if rows else 0.0
    observed = {
        str(row.get("instId") or "")
        for row in rows
        if _ticker_row_usable(row)
    }
    return len(expected & observed) / len(expected)


def _fetch_tickers_with_cold_retry(
    deadline: float,
) -> tuple[list[dict], dict[str, object]]:
    """Fetch current tickers with one bounded pre-write cold retry.

    Calling ``fetch_tickers_all_sync`` again creates a fresh HTTP client.  A
    partial first response is retained if the retry is worse, and two failed
    transports still fail closed.  No historical slot or persisted row is
    touched by this helper.
    """
    first_rows: list[dict] = []
    first_error: Exception | None = None
    schannel_transport: dict[str, object] = {}

    def aggregate_fetch(timeout_seconds: float) -> list[dict]:
        return fetch_tickers_all_sync(
            timeout_seconds,
            transport_fallback=lambda url, params, timeout: (
                fetch_tickers_all_schannel_sync(
                    url,
                    params,
                    timeout,
                    transport=schannel_transport,
                )
            ),
            transport_fallback_reserve_s=min(
                TICKER_SCHANNEL_RESERVE_SECONDS,
                max(0.0, timeout_seconds / 3.0),
            ),
        )

    remaining = max(0.1, deadline - time.monotonic())
    first_budget = min(
        TICKER_INITIAL_TIMEOUT_SECONDS,
        max(0.1, remaining * TICKER_INITIAL_BUDGET_SHARE),
    )
    try:
        first_rows = aggregate_fetch(first_budget)
        if not isinstance(first_rows, list):
            raise TypeError("official ticker response is not a list")
    except Exception as exc:  # noqa: BLE001
        first_error = exc
        first_rows = []

    first_coverage = _ticker_symbol_coverage(first_rows)
    needs_retry = first_error is not None or first_coverage < TICKER_COMPLETE_COVERAGE
    stats: dict[str, object] = {
        "contract_version": 3,
        "attempts": 1,
        "maximum_fetch_phases": TICKER_MAX_FETCH_PHASES,
        "historical_retry": False,
        "unbounded_retry": False,
        "initial_timeout_seconds": round(first_budget, 3),
        "initial_coverage_rate": round(first_coverage, 6),
        "initial_error_type": type(first_error).__name__ if first_error else None,
        "cold_retry_requested": bool(needs_retry),
        "cold_retry_timeout_seconds": 0.0,
        "cold_retry_error_type": None,
        "recovered_after_cold_retry": False,
        "single_ticker_fallback_requested": False,
        "single_ticker_fallback_symbols": 0,
        "single_ticker_fallback_timeout_seconds": 0.0,
        "single_ticker_fallback_usable": 0,
        "single_ticker_fallback_transport_failures": 0,
        "single_ticker_fallback_error_type": None,
        "single_ticker_fallback_selected_base": None,
        "single_ticker_fallback_probe_attempts": 0,
        "recovered_after_single_ticker_fallback": False,
        "schannel_fallback_requested": int(
            schannel_transport.get("schannel_fallback_requested", 0)
        ),
        "schannel_fallback_successes": int(
            schannel_transport.get("schannel_fallback_successes", 0)
        ),
        "schannel_fallback_error_types": list(
            schannel_transport.get("schannel_fallback_error_types", [])
        ),
        "recovered_after_schannel_fallback": bool(
            schannel_transport.get("schannel_fallback_successes", 0)
            and first_coverage >= TICKER_COMPLETE_COVERAGE
        ),
    }
    if not needs_retry:
        stats["selected_coverage_rate"] = round(first_coverage, 6)
        return first_rows, stats

    delay = min(
        TICKER_COLD_RETRY_DELAY_SECONDS,
        max(0.0, deadline - time.monotonic() - 0.2),
    )
    if delay > 0:
        time.sleep(delay)
    remaining = max(0.1, deadline - time.monotonic())
    retry_budget = min(
        TICKER_COLD_RETRY_TIMEOUT_SECONDS,
        max(0.1, remaining * TICKER_COLD_RETRY_BUDGET_SHARE),
    )
    stats["attempts"] = 2
    stats["cold_retry_timeout_seconds"] = round(retry_budget, 3)
    retry_rows: list[dict] = []
    retry_error: Exception | None = None
    try:
        retry_rows = aggregate_fetch(retry_budget)
        if not isinstance(retry_rows, list):
            raise TypeError("official ticker cold retry response is not a list")
    except Exception as exc:  # noqa: BLE001
        retry_error = exc
        retry_rows = []
    retry_coverage = _ticker_symbol_coverage(retry_rows)
    stats["cold_retry_coverage_rate"] = round(retry_coverage, 6)
    stats["cold_retry_error_type"] = (
        type(retry_error).__name__ if retry_error else None
    )
    stats["schannel_fallback_requested"] = int(
        schannel_transport.get("schannel_fallback_requested", 0)
    )
    stats["schannel_fallback_successes"] = int(
        schannel_transport.get("schannel_fallback_successes", 0)
    )
    stats["schannel_fallback_error_types"] = list(
        schannel_transport.get("schannel_fallback_error_types", [])
    )

    # Combine the two same-slot official responses per symbol.  Prefer a valid
    # fresh retry row, but never let an empty retry erase a valid first row.
    by_symbol = {
        str(row.get("instId") or ""): row
        for row in first_rows
        if isinstance(row, dict) and row.get("instId")
    }
    for row in retry_rows:
        if not isinstance(row, dict) or not row.get("instId"):
            continue
        symbol = str(row["instId"])
        if _ticker_row_usable(row) or not _ticker_row_usable(
                by_symbol.get(symbol)):
            by_symbol[symbol] = row
    selected_rows = list(by_symbol.values())
    selected_coverage = _ticker_symbol_coverage(selected_rows)
    stats["selected_coverage_rate"] = round(selected_coverage, 6)
    stats["recovered_after_cold_retry"] = bool(
        selected_coverage >= TICKER_COMPLETE_COVERAGE
        and first_coverage < TICKER_COMPLETE_COVERAGE
    )
    stats["recovered_after_schannel_fallback"] = bool(
        stats["schannel_fallback_successes"]
        and selected_coverage >= TICKER_COMPLETE_COVERAGE
    )

    if selected_coverage < TICKER_COMPLETE_COVERAGE:
        missing_symbols = [
            symbol for symbol in SYMBOLS
            if not _ticker_row_usable(by_symbol.get(symbol))
        ]
        fallback_budget = min(
            TICKER_SINGLE_FALLBACK_TIMEOUT_SECONDS,
            max(0.0, deadline - time.monotonic() - 1.0),
        )
        stats["attempts"] = 3
        stats["single_ticker_fallback_requested"] = True
        stats["single_ticker_fallback_symbols"] = len(missing_symbols)
        stats["single_ticker_fallback_timeout_seconds"] = round(
            fallback_budget, 3
        )
        fallback_map: dict[str, dict] = {}
        fallback_outcomes: dict[str, dict] = {}
        fallback_transport: dict[str, object] = {}
        fallback_error: Exception | None = None
        if fallback_budget > 0.1:
            try:
                fallback_map = fetch_tickers_batch_sync(
                    missing_symbols,
                    batch_timeout_s=fallback_budget,
                    outcomes=fallback_outcomes,
                    transport=fallback_transport,
                )
            except Exception as exc:  # noqa: BLE001
                fallback_error = exc
        else:
            fallback_error = TimeoutError(
                "no current-cycle budget for official single-ticker fallback"
            )

        usable_fallback = 0
        for symbol in missing_symbols:
            row = fallback_map.get(symbol)
            if _ticker_row_usable(row):
                by_symbol[symbol] = row
                usable_fallback += 1
        stats["single_ticker_fallback_usable"] = usable_fallback
        stats["single_ticker_fallback_transport_failures"] = sum(
            1 for outcome in fallback_outcomes.values()
            if not bool(outcome.get("ok"))
        )
        stats["single_ticker_fallback_error_type"] = (
            type(fallback_error).__name__ if fallback_error else None
        )
        stats["single_ticker_fallback_selected_base"] = (
            fallback_transport.get("selected_base")
        )
        stats["single_ticker_fallback_probe_attempts"] = int(
            fallback_transport.get("probe_attempts") or 0
        )
        selected_rows = list(by_symbol.values())
        selected_coverage = _ticker_symbol_coverage(selected_rows)
        stats["selected_coverage_rate"] = round(selected_coverage, 6)
        stats["recovered_after_single_ticker_fallback"] = bool(
            selected_coverage >= TICKER_COMPLETE_COVERAGE
        )
    if selected_rows:
        return selected_rows, stats

    first_detail = (
        f"{type(first_error).__name__}: {first_error}"
        if first_error else "empty official ticker response"
    )
    retry_detail = (
        f"{type(retry_error).__name__}: {retry_error}"
        if retry_error else "empty official ticker response"
    )
    raise RuntimeError(
        "official ticker fetch failed after bounded cold retry and "
        "single-ticker fallback: "
        f"initial=({first_detail}); retry=({retry_detail}); "
        "single_ticker=(usable="
        f"{stats['single_ticker_fallback_usable']}, "
        f"error={stats['single_ticker_fallback_error_type']})"
    )


def collect_tickers(
    market_con: sqlite3.Connection,
    ts: str,
    batch_timeout_s: float = 135.0,
) -> tuple[int, dict[str, dict], dict[str, object]]:



    # ── Batch HTTP (fast): tickers + funding rates ──────────────────────────

    deadline = time.monotonic() + max(1.0, float(batch_timeout_s))

    def remaining() -> float:
        return max(0.1, deadline - time.monotonic())

    # Funding uses a distinct official endpoint/rate bucket. Start it before
    # ticker recovery so a rare per-instrument ticker fallback cannot consume
    # the funding completeness budget.
    with ThreadPoolExecutor(max_workers=1) as funding_executor:
        funding_future = funding_executor.submit(
            fetch_funding_rates_batch_sync,
            SYMBOLS,
            batch_timeout_s=remaining(),
        )
        all_tickers_raw, ticker_transport = (
            _fetch_tickers_with_cold_retry(deadline)
        )
        funding_map: dict[str, dict] = funding_future.result()

    # Index by instId for O(1) lookup
    ticker_map = {item.get("instId"): item for item in all_tickers_raw}

    # OI：公共端点支持 instType=SWAP 一次取全量，供分析观察 OI 24h 变化。
    official_instruments: list[dict] = []
    try:
        oi_map: dict[str, dict] = fetch_open_interest_all_sync(
            "SWAP",
            request_timeout_s=remaining(),
        )
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

        funding_rate = to_float(funding.get("fundingRate"))
        oi_value = to_float(oi_row.get("oi"))
        if last_px is not None and last_px > 0:
            tick_rows.append(
                (
                    ts,
                    symbol,
                    last_px,
                    to_float(ticker.get("bidPx")),
                    to_float(ticker.get("askPx")),
                    to_float(ticker.get("vol24h")),
                    funding_rate,
                    oi_value,
                    chg24h,
                )
            )
        # 不把全空占位行写成“本轮新衍生品快照”；否则一次批次超时会把
        # 最新值整体顶成 NULL，后续分析难以区分“真实 0”与“未采到”。
        if funding_rate is not None or oi_value is not None:
            derivative_rows.append(
                (
                    ts,
                    symbol,
                    funding_rate,
                    ms_to_iso(funding.get("fundingTime")),
                    ms_to_iso(funding.get("nextFundingTime")),
                    to_float(funding.get("premium")),
                    oi_value,
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

    expected = len(SYMBOLS)
    quality = {
        "expected": expected,
        "tickers": len(tick_rows),
        "funding": sum(
            1 for value in snapshot.values()
            if value.get("fundingRate") is not None
        ),
        "open_interest": sum(
            1 for value in snapshot.values()
            if value.get("oiUsd") is not None
        ),
        "ticker_transport": ticker_transport,
    }
    return len(tick_rows), snapshot, quality





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
    # 2026-08-13 BOLL/OBV 扩展列 migration-aware：迁移未跑按旧列写（零行为变化）。
    insert_plan = kline_insert_plan(market_con)
    if not insert_plan["extended"]:
        print(
            "[collect_data][WARN] kline_cache 缺 BOLL/OBV 扩展列"
            "（apply_kline_indicator_schema 未跑），按旧列写入",
            flush=True,
        )



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

        enriched = extend_with_boll_obv(compute_indicators(candles))

        for item in enriched:

            base_row = (
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
            if insert_plan["extended"]:
                base_row += extended_row_tail(item)
            rows_to_write.append(base_row)



    market_con.executemany(insert_plan["sql"], rows_to_write)

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
        _db_root = _os.path.dirname(_main) if _main else r".\db"
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

    parser.add_argument(
        "--cycle",
        default=None,
        help="北京时间15分钟自然槽 YYYY-MM-DDTHH:MM；默认按启动时刻归槽",
    )



    # 兼容解析后硬拒绝，给旧 cron/人工命令明确错误；绝不把 --demo 映射成 --live。
    parser.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)



    parser.add_argument("--skip-news", action="store_true", help=argparse.SUPPRESS)

    parser.add_argument(
        "--http-batch-timeout",
        type=float,
        default=135.0,
        help=(
            "快采公共行情批次内部截止秒数；默认 135，须早于 fast_collect "
            "外层 175 秒超时"
        ),
    )

    args = parser.parse_args()

    if args.demo:
        parser.error(
            "--demo 已于 2026-08-06 下线；collect_data 只采公共市场事实，"
            "禁止以 demo 标签写入生产 market.db"
        )







    db_root = Path(args.db_root)



    state_path = Path(args.state_path)



    ts_start = utc_now_iso()

    if args.cycle:
        try:
            parsed_cycle = datetime.strptime(args.cycle, "%Y-%m-%dT%H:%M")
        except ValueError as exc:
            parser.error(f"--cycle 格式无效: {exc}")
        if parsed_cycle.minute not in (0, 15, 30, 45):
            parser.error("--cycle 必须对齐北京时间15分钟自然槽")
        cycle_id = args.cycle
    else:
        started = datetime.fromisoformat(ts_start.replace("Z", "+00:00"))
        local = started.astimezone(_CST_TZ)
        cycle_id = local.replace(
            minute=(local.minute // 15) * 15,
            second=0,
            microsecond=0,
        ).strftime("%Y-%m-%dT%H:%M")



    public_cli_global_args: list[str] = []



    # Account/profile reads are isolated in jobb_live_account_check.py.







    #  Dynamically fetch all live USDT-M linear SWAP symbols



    global SYMBOLS, COIN_TO_SYMBOL



    official_instruments, instrument_discovery_error = (
        discover_live_swap_instruments(public_cli_global_args)
    )
    if instrument_discovery_error is None:
        SYMBOLS = sorted(str(inst["instId"]) for inst in official_instruments)
        COIN_TO_SYMBOL = {s.split("-")[0]: s for s in SYMBOLS}
        print(f"[collect_data] Discovered {len(SYMBOLS)} USDT-M SWAP contracts", file=sys.stderr)
    else:
        print(
            "[collect_data] WARNING: Could not fetch dynamic symbols "
            f"({instrument_discovery_error}); using empty list",
            file=sys.stderr,
        )
        SYMBOLS = []
        COIN_TO_SYMBOL = {}



    #







    summary = {
        "profile": args.profile,
        "cycle_id": cycle_id,
        "ts_start": ts_start,
        "symbols_count": len(SYMBOLS),
        "wrote": {},
    }



    warnings: list[str] = []



    error: str | None = None

    # instruments 拉取失败导致空 symbols 时，本轮无市场数据，必须记 error，
    # 让 fast_collect 判为 error 并阻止分析使用陈旧行情。
    if not SYMBOLS:
        error = "instruments_empty: 未取到任何 SWAP symbols，本轮无市场数据（fail-safe 阻止陈旧行情当新鲜）"
        if instrument_discovery_error is not None:
            error += f"; discovery_error={instrument_discovery_error}"



    market_con = None



    try:



        market_con = open_db(db_root, "market.db")

        instrument_snapshot_failed = False
        try:
            official_snapshot = freeze_official_instrument_snapshot(
                market_con,
                official_instruments,
                cycle_id=cycle_id,
                collected_ts_utc=ts_start,
            )
            summary["official_instrument_snapshot"] = official_snapshot
            if official_snapshot["status"] == "conflict":
                instrument_snapshot_failed = True
                warnings.append(
                    "official_instrument_snapshot_conflict=" + cycle_id
                )
        except (sqlite3.Error, ValueError) as snapshot_exc:
            instrument_snapshot_failed = True
            summary["official_instrument_snapshot"] = {
                "status": "failed",
                "cycle_id": cycle_id,
                "error": f"{type(snapshot_exc).__name__}: {snapshot_exc}",
            }
            warnings.append(
                "official_instrument_snapshot_failed: "
                f"{type(snapshot_exc).__name__}: {snapshot_exc}"
            )

        # OKX official ``instCategory`` is the authority for broad asset type.
        # The existing market writer only fills/corrects non-manual rows; manual
        # decisions remain immutable.  This closes new-listing gaps without a
        # second database writer or an out-of-band repair job.
        try:
            asset_sync = sync_asset_classes(
                market_con, official_instruments, apply=True)
            summary["asset_class_sync"] = {
                key: asset_sync[key]
                for key in (
                    "official_instruments",
                    "insert_count",
                    "update_count",
                    "unchanged_count",
                    "manual_conflict_count",
                    "unsupported_count",
                    "applied",
                )
            }
            if asset_sync["manual_conflict_count"]:
                warnings.append(
                    "asset_class_manual_conflicts="
                    f"{asset_sync['manual_conflict_count']}"
                )
            if asset_sync["unsupported_count"]:
                warnings.append(
                    "asset_class_unsupported_official_categories="
                    f"{asset_sync['unsupported_count']}"
                )
        except (sqlite3.Error, ValueError) as asset_exc:
            summary["asset_class_sync"] = {
                "applied": False,
                "error": f"{type(asset_exc).__name__}: {asset_exc}",
            }
            warnings.append(
                "asset_class_sync_failed: "
                f"{type(asset_exc).__name__}: {asset_exc}"
            )



        # V2.0 的账户/持仓快照唯一权威写入方是
        # scripts/jobb_live_account_check.py；collect_data 仅写 market.db。







        # 2026-06-30 提速：candles 预取与 collect_tickers(含 funding 抓取) 并发——
        # _okx_http 已按端点分桶限速、两端点互不阻塞；candles 后台跑，collect_tickers
        # 同时抓 tickers+funding 并写库（market_con 只此主线程写），wall≈max 而非串行相加。
        with ThreadPoolExecutor(max_workers=1) as _cand_ex:
            _cand_fut = _cand_ex.submit(
                fetch_candles_batch_sync,
                SYMBOLS,
                "15m",
                60,
                args.http_batch_timeout,
            )
            tick_count, ticker_snapshot, ticker_quality = collect_tickers(
                market_con,
                ts_start,
                batch_timeout_s=args.http_batch_timeout,
            )
            summary["wrote"]["tickers"] = tick_count
            _cand_data = _cand_fut.result()

        # Job A: ALL symbols get 15m K-lines (full coverage for accuracy)
        summary["wrote"]["klines"] = collect_klines(market_con, SYMBOLS, prefetched=_cand_data)

        expected = len(SYMBOLS)
        candle_symbols = sum(1 for rows in _cand_data.values() if rows)
        quality = {
            **ticker_quality,
            "candles": candle_symbols,
            "ticker_coverage": round(
                ticker_quality["tickers"] / expected, 4
            ) if expected else 0.0,
            "funding_coverage": round(
                ticker_quality["funding"] / expected, 4
            ) if expected else 0.0,
            "candle_coverage": round(
                candle_symbols / expected, 4
            ) if expected else 0.0,
            "batch_timeout_s": args.http_batch_timeout,
        }
        summary["quality"] = quality

        core_symbols = {
            "BTC-USDT-SWAP",
            "ETH-USDT-SWAP",
            "SOL-USDT-SWAP",
        }
        missing_core_tickers = sorted(
            symbol for symbol in core_symbols
            if symbol not in ticker_snapshot
            or ticker_snapshot[symbol].get("last") is None
        )
        missing_core_candles = sorted(
            symbol for symbol in core_symbols if not _cand_data.get(symbol)
        )
        quality_errors: list[str] = []
        if quality["ticker_coverage"] < 0.95:
            quality_errors.append(
                f"ticker_coverage={quality['ticker_coverage']:.1%}<95%"
            )
        if quality["candle_coverage"] < 0.80:
            quality_errors.append(
                f"candle_coverage={quality['candle_coverage']:.1%}<80%"
            )
        if missing_core_tickers:
            quality_errors.append(
                "core_ticker_missing=" + ",".join(missing_core_tickers)
            )
        if missing_core_candles:
            quality_errors.append(
                "core_candle_missing=" + ",".join(missing_core_candles)
            )
        if quality_errors and error is None:
            error = "market_quality_fail_closed: " + "; ".join(quality_errors)

        degraded_reasons: list[str] = []
        if instrument_snapshot_failed:
            degraded_reasons.append("official_instrument_snapshot_incomplete")
        if quality["ticker_coverage"] < TICKER_COMPLETE_COVERAGE:
            degraded_reasons.append(
                f"ticker_coverage={quality['ticker_coverage']:.1%}<99%"
            )
        if quality["candle_coverage"] < 0.95:
            degraded_reasons.append(
                f"candle_coverage={quality['candle_coverage']:.1%}<95%"
            )
        if quality["funding_coverage"] < 0.80:
            degraded_reasons.append(
                f"funding_coverage={quality['funding_coverage']:.1%}<80%"
            )
        if degraded_reasons:
            summary["degraded"] = True
            warnings.append("market_partial: " + "; ".join(degraded_reasons))
        else:
            summary["degraded"] = False



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
                if r".\scripts" not in sys.path:
                    sys.path.insert(0, r".\scripts")
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
