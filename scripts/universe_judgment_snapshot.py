# -*- coding: utf-8 -*-
"""Build a read-only, full-universe 15m/1H/4H judgment snapshot.

This is a shadow/audit artifact.  It never writes a trading database and never
authorizes an order.  Every live USDT linear swap in the latest OKX ticker
snapshot receives one explicit judgment record, including explicit
``wait_data`` and ``wait_liquidity`` outcomes.

The technical score is deliberately named ``uncalibrated_alignment_score``.
It is evidence alignment, not a probability and not a 90% confidence claim.
Only fully closed candles available at the evaluation time are used.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))
UTC = timezone.utc
TIMEFRAMES = ("15m", "1H", "4H")
TIMEFRAME_SECONDS = {"15m": 15 * 60, "1H": 60 * 60, "4H": 4 * 60 * 60}
TIMEFRAME_WEIGHTS = {"15m": 0.25, "1H": 0.35, "4H": 0.40}
REQUIRED_KLINE_FIELDS = ("o", "h", "l", "c", "v")
REQUIRED_INDICATOR_FIELDS = ("ma5", "ma20", "atr14", "rsi14", "macd_hist")
DEFAULT_DB_ROOT = Path(r"./db")
DEFAULT_MIN_QUOTE_VOLUME_USD = 5_000_000.0
DEFAULT_MIN_OI_USD = 5_000_000.0
DEFAULT_MIN_COMPLETENESS_PCT = 99.0
DEFAULT_MIN_SCREENED_SYMBOLS = 300
DEFAULT_NEWS_FRESH_MINUTES = 60
DEFAULT_FEATURE_FRESH_MINUTES = 30
CYCLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def _round(value: Any, digits: int = 6) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 3) if denominator else 0.0


def _parse_utc(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cst_text(value: datetime) -> str:
    return value.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def _ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(str(path))
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _closed_bar_cutoff(evaluation_utc: datetime, timeframe: str) -> str:
    duration = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    return _iso_utc(evaluation_utc - duration)


def _latest_row(
    con: sqlite3.Connection,
    table: str,
    symbol: str,
    not_after: str,
    *,
    timeframe: str | None = None,
) -> sqlite3.Row | None:
    if timeframe is None:
        return con.execute(
            f"SELECT * FROM {table} WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (symbol, not_after),
        ).fetchone()
    return con.execute(
        f"SELECT * FROM {table} WHERE symbol=? AND tf=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (symbol, timeframe, not_after),
    ).fetchone()


def _timeframe_record(
    con: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    evaluation_utc: datetime,
) -> dict[str, Any]:
    cutoff = _closed_bar_cutoff(evaluation_utc, timeframe)
    row = _latest_row(con, "kline_cache", symbol, cutoff, timeframe=timeframe)
    if row is None:
        return {
            "timeframe": timeframe,
            "status": "missing",
            "closed_bar_cutoff_utc": cutoff,
            "missing_fields": [*REQUIRED_KLINE_FIELDS, *REQUIRED_INDICATOR_FIELDS],
            "bars_seen": 0,
            "score": None,
        }

    bars_seen = int(
        con.execute(
            "SELECT COUNT(*) FROM kline_cache WHERE symbol=? AND tf=? AND ts<=?",
            (symbol, timeframe, row["ts"]),
        ).fetchone()[0]
    )
    raw_missing = [field for field in REQUIRED_KLINE_FIELDS if row[field] is None]
    indicator_missing = [field for field in REQUIRED_INDICATOR_FIELDS if row[field] is None]
    if raw_missing:
        status = "missing_raw"
    elif indicator_missing and bars_seen < 35:
        status = "insufficient_history"
    elif indicator_missing:
        status = "indicator_missing"
    else:
        status = "ready"

    votes: dict[str, int] = {}
    score = None
    if status == "ready":
        votes = {
            "close_vs_ma20": 1 if row["c"] > row["ma20"] else -1,
            "ma5_vs_ma20": 1 if row["ma5"] > row["ma20"] else -1,
            "macd_hist": 1 if row["macd_hist"] > 0 else -1,
            "rsi_zone": 1 if row["rsi14"] >= 55 else (-1 if row["rsi14"] <= 45 else 0),
        }
        score = sum(votes.values()) / len(votes)

    return {
        "timeframe": timeframe,
        "status": status,
        "closed_bar_cutoff_utc": cutoff,
        "bar_ts_utc": row["ts"],
        "bars_seen": bars_seen,
        "missing_fields": [*raw_missing, *indicator_missing],
        "o": _round(row["o"]),
        "h": _round(row["h"]),
        "l": _round(row["l"]),
        "c": _round(row["c"]),
        "v": _round(row["v"]),
        "ma5": _round(row["ma5"]),
        "ma20": _round(row["ma20"]),
        "atr14": _round(row["atr14"]),
        "rsi14": _round(row["rsi14"]),
        "macd_hist": _round(row["macd_hist"]),
        "votes": votes,
        "score": _round(score),
    }


def _feature_is_fresh(row: sqlite3.Row | None, evaluation_utc: datetime, minutes: int) -> bool:
    if row is None or not row["ts"]:
        return False
    try:
        age = (evaluation_utc - _parse_utc(row["ts"])).total_seconds()
    except (TypeError, ValueError):
        return False
    return -60 <= age <= minutes * 60


def _news_context(
    news: sqlite3.Connection | None,
    evaluation_utc: datetime,
    news_fresh_minutes: int,
) -> tuple[dict[str, dict[str, int]], dict[str, Any]]:
    if news is None:
        return {}, {
            "status": "missing_database",
            "latest_seen_at_cst": None,
            "age_minutes": None,
            "fresh": False,
            "official_okx_fresh": False,
        }

    end_cst = _cst_text(evaluation_utc)
    start_cst = _cst_text(evaluation_utc - timedelta(hours=24))
    rows = news.execute(
        """
        SELECT upper(symbol) symbol, COUNT(*) events,
               SUM(CASE WHEN source='okx_news' THEN 1 ELSE 0 END) okx_events
        FROM news_items
        WHERE symbol IS NOT NULL AND trim(symbol)<>''
          AND datetime(COALESCE(first_seen_at,ingested_at,ts))>=datetime(?)
          AND datetime(COALESCE(first_seen_at,ingested_at,ts))<=datetime(?)
        GROUP BY upper(symbol)
        """,
        (start_cst, end_cst),
    ).fetchall()
    by_symbol = {
        str(row["symbol"]): {
            "events_24h": int(row["events"] or 0),
            "official_okx_events_24h": int(row["okx_events"] or 0),
        }
        for row in rows
    }
    latest = news.execute(
        "SELECT MAX(COALESCE(first_seen_at,ingested_at,ts)) FROM news_items "
        "WHERE datetime(COALESCE(first_seen_at,ingested_at,ts))<=datetime(?)",
        (end_cst,),
    ).fetchone()[0]
    latest_okx = news.execute(
        "SELECT MAX(COALESCE(first_seen_at,ingested_at,ts)) FROM news_items "
        "WHERE source='okx_news' AND datetime(COALESCE(first_seen_at,ingested_at,ts))<=datetime(?)",
        (end_cst,),
    ).fetchone()[0]

    def local_age_minutes(value: str | None) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("T", " ")).replace(tzinfo=CST)
        except ValueError:
            return None
        return round((evaluation_utc - parsed.astimezone(UTC)).total_seconds() / 60.0, 2)

    age = local_age_minutes(latest)
    okx_age = local_age_minutes(latest_okx)
    fresh = age is not None and -1 <= age <= news_fresh_minutes
    okx_fresh = okx_age is not None and -1 <= okx_age <= news_fresh_minutes
    return by_symbol, {
        "status": "fresh" if fresh and okx_fresh else "stale",
        "latest_seen_at_cst": latest,
        "age_minutes": age,
        "fresh": fresh,
        "official_okx_latest_seen_at_cst": latest_okx,
        "official_okx_age_minutes": okx_age,
        "official_okx_fresh": okx_fresh,
        "zero_symbol_events_semantics": "valid_no_event_not_missing",
    }


def _symbol_judgment(
    market: sqlite3.Connection,
    tick: sqlite3.Row,
    evaluation_utc: datetime,
    news_by_symbol: dict[str, dict[str, int]],
    *,
    min_quote_volume_usd: float,
    min_oi_usd: float,
    feature_fresh_minutes: int,
) -> dict[str, Any]:
    symbol = str(tick["symbol"])
    instrument = market.execute(
        "SELECT ctVal,lotSz FROM instruments_cache WHERE instId=?", (symbol,)
    ).fetchone()
    asset = market.execute(
        "SELECT asset_class,source,updated_at FROM instrument_class WHERE symbol=?",
        (symbol,),
    ).fetchone()
    derivative = _latest_row(market, "derivatives", symbol, _iso_utc(evaluation_utc))
    timeframes = {
        timeframe: _timeframe_record(market, symbol, timeframe, evaluation_utc)
        for timeframe in TIMEFRAMES
    }

    missing_universal: list[str] = []
    for field in ("last", "bid", "ask", "vol24h"):
        if tick[field] is None:
            missing_universal.append(f"ticker.{field}")
    if instrument is None or instrument["ctVal"] in (None, 0):
        missing_universal.append("instrument.ctVal")
    if asset is None or not asset["asset_class"]:
        missing_universal.append("instrument.asset_class")
    for field in ("funding_rate", "oi_usd"):
        if derivative is None or derivative[field] is None:
            missing_universal.append(f"derivatives.{field}")
    for timeframe, record in timeframes.items():
        for field in REQUIRED_KLINE_FIELDS:
            if record.get(field) is None:
                missing_universal.append(f"{timeframe}.{field}")

    ct_val = float(instrument["ctVal"]) if instrument and instrument["ctVal"] else None
    quote_volume_usd = None
    if tick["last"] is not None and tick["vol24h"] is not None and ct_val is not None:
        quote_volume_usd = float(tick["last"]) * float(tick["vol24h"]) * ct_val
    oi_usd = float(derivative["oi_usd"]) if derivative and derivative["oi_usd"] is not None else None
    liquidity_eligible = bool(
        quote_volume_usd is not None
        and oi_usd is not None
        and quote_volume_usd >= min_quote_volume_usd
        and oi_usd >= min_oi_usd
    )
    analysis_ready = not missing_universal and all(
        record["status"] == "ready" for record in timeframes.values()
    )

    weighted_score = None
    aligned_timeframes = 0
    judgment = "wait_data"
    if analysis_ready:
        weighted_score = sum(
            TIMEFRAME_WEIGHTS[timeframe] * float(timeframes[timeframe]["score"])
            for timeframe in TIMEFRAMES
        )
        signs = [
            1 if timeframes[timeframe]["score"] > 0 else (
                -1 if timeframes[timeframe]["score"] < 0 else 0
            )
            for timeframe in TIMEFRAMES
        ]
        weighted_sign = 1 if weighted_score >= 0.25 else (-1 if weighted_score <= -0.25 else 0)
        aligned_timeframes = sum(1 for sign in signs if sign == weighted_sign and sign != 0)
        if weighted_sign > 0 and aligned_timeframes >= 2:
            judgment = "long_bias"
        elif weighted_sign < 0 and aligned_timeframes >= 2:
            judgment = "short_bias"
        else:
            judgment = "wait_mixed"

    micro = _latest_row(market, "market_microstructure", symbol, _iso_utc(evaluation_utc))
    flow = _latest_row(market, "market_trade_flow", symbol, _iso_utc(evaluation_utc))
    micro_fresh = _feature_is_fresh(micro, evaluation_utc, feature_fresh_minutes)
    flow_fresh = _feature_is_fresh(flow, evaluation_utc, feature_fresh_minutes)
    enrichment_ready = micro_fresh and flow_fresh
    if not analysis_ready:
        execution_readiness = "data_incomplete"
    elif not liquidity_eligible:
        execution_readiness = "liquidity_rejected"
    elif judgment not in {"long_bias", "short_bias"}:
        execution_readiness = "no_direction"
    elif not enrichment_ready:
        execution_readiness = "enrichment_incomplete"
    else:
        execution_readiness = "shadow_candidate"

    news_counts = news_by_symbol.get(symbol, {"events_24h": 0, "official_okx_events_24h": 0})
    return {
        "symbol": symbol,
        "asset_class": asset["asset_class"] if asset else None,
        "snapshot_ts_utc": tick["ts"],
        "data_status": "ready" if analysis_ready else "incomplete",
        "universal_market_data_complete": not missing_universal,
        "analysis_ready": analysis_ready,
        "missing_universal_fields": missing_universal,
        "market": {
            "last": _round(tick["last"]),
            "bid": _round(tick["bid"]),
            "ask": _round(tick["ask"]),
            "chg24h_pct": _round(tick["chg24h"]),
            "contracts_24h": _round(tick["vol24h"]),
            "contract_value": _round(ct_val),
            "quote_volume_24h_usd": _round(quote_volume_usd, 2),
            "funding_rate": _round(derivative["funding_rate"] if derivative else None, 10),
            "open_interest_usd": _round(oi_usd, 2),
        },
        "liquidity": {
            "eligible": liquidity_eligible,
            "minimum_quote_volume_usd": min_quote_volume_usd,
            "minimum_open_interest_usd": min_oi_usd,
        },
        "timeframes": timeframes,
        "judgment": judgment,
        "uncalibrated_alignment_score": _round(weighted_score),
        "aligned_timeframes": aligned_timeframes,
        "calibrated_confidence": None,
        "confidence_claim_allowed": False,
        "news": {
            **news_counts,
            "status": "events_found" if news_counts["events_24h"] else "no_event",
            "no_event_is_missing": False,
        },
        "candidate_enrichment": {
            "microstructure_fresh": micro_fresh,
            "trade_flow_fresh": flow_fresh,
            "ready": enrichment_ready,
        },
        "execution_readiness": execution_readiness,
        "production_execution_authorized": False,
    }


def build_snapshot(
    db_root: Path,
    *,
    evaluation_utc: datetime,
    cycle_id: str,
    min_quote_volume_usd: float = DEFAULT_MIN_QUOTE_VOLUME_USD,
    min_oi_usd: float = DEFAULT_MIN_OI_USD,
    min_completeness_pct: float = DEFAULT_MIN_COMPLETENESS_PCT,
    min_screened_symbols: int = DEFAULT_MIN_SCREENED_SYMBOLS,
    news_fresh_minutes: int = DEFAULT_NEWS_FRESH_MINUTES,
    feature_fresh_minutes: int = DEFAULT_FEATURE_FRESH_MINUTES,
) -> dict[str, Any]:
    market = _ro(db_root / "market.db")
    news: sqlite3.Connection | None = None
    try:
        try:
            news = _ro(db_root / "news.db")
        except FileNotFoundError:
            news = None

        evaluation_iso = _iso_utc(evaluation_utc)
        tick_ts_row = market.execute(
            "SELECT MAX(ts) FROM tick_snapshots WHERE ts<=?", (evaluation_iso,)
        ).fetchone()
        tick_ts = tick_ts_row[0] if tick_ts_row else None
        if not tick_ts:
            raise RuntimeError(f"no tick snapshot at or before {evaluation_iso}")
        ticks = market.execute(
            "SELECT * FROM tick_snapshots WHERE ts=? ORDER BY symbol", (tick_ts,)
        ).fetchall()
        news_by_symbol, news_health = _news_context(news, evaluation_utc, news_fresh_minutes)
        records = [
            _symbol_judgment(
                market,
                tick,
                evaluation_utc,
                news_by_symbol,
                min_quote_volume_usd=min_quote_volume_usd,
                min_oi_usd=min_oi_usd,
                feature_fresh_minutes=feature_fresh_minutes,
            )
            for tick in ticks
        ]
    finally:
        market.close()
        if news is not None:
            news.close()

    universe = len(records)
    universal_complete = sum(record["universal_market_data_complete"] for record in records)
    analysis_ready = sum(record["analysis_ready"] for record in records)
    eligible = sum(record["liquidity"]["eligible"] for record in records)
    eligible_enriched = sum(
        record["liquidity"]["eligible"] and record["candidate_enrichment"]["ready"]
        for record in records
    )
    judgment_counts = Counter(record["judgment"] for record in records)
    readiness_counts = Counter(record["execution_readiness"] for record in records)
    universal_pct = _pct(universal_complete, universe)
    analysis_ready_pct = _pct(analysis_ready, universe)
    enrichment_pct = _pct(eligible_enriched, eligible)
    gates = {
        "universal_market_data_at_least_target": universal_pct >= min_completeness_pct,
        "analysis_ready_at_least_target": analysis_ready_pct >= min_completeness_pct,
        "eligible_enrichment_at_least_target": enrichment_pct >= min_completeness_pct if eligible else False,
        "news_fresh": bool(news_health["fresh"]),
        "official_okx_news_fresh": bool(news_health["official_okx_fresh"]),
        "minimum_symbols_screened": universe >= min_screened_symbols,
        "confidence_calibrated": False,
        "production_execution_authorized": False,
    }
    status = "ready_for_shadow_evaluation" if all(
        gates[key]
        for key in (
            "universal_market_data_at_least_target",
            "analysis_ready_at_least_target",
            "eligible_enrichment_at_least_target",
            "news_fresh",
            "official_okx_news_fresh",
            "minimum_symbols_screened",
        )
    ) else "degraded"

    def top(direction: str) -> list[dict[str, Any]]:
        chosen = [
            record for record in records
            if record["judgment"] == direction
            and record["execution_readiness"] == "shadow_candidate"
        ]
        chosen.sort(
            key=lambda record: (
                abs(record["uncalibrated_alignment_score"] or 0),
                record["market"]["quote_volume_24h_usd"] or 0,
            ),
            reverse=True,
        )
        return [
            {
                "symbol": record["symbol"],
                "judgment": record["judgment"],
                "uncalibrated_alignment_score": record["uncalibrated_alignment_score"],
                "quote_volume_24h_usd": record["market"]["quote_volume_24h_usd"],
                "open_interest_usd": record["market"]["open_interest_usd"],
            }
            for record in chosen[:20]
        ]

    return {
        "schema_version": 1,
        "artifact_type": "full_universe_shadow_judgment",
        "cycle_id": cycle_id,
        "generated_at_utc": _iso_utc(datetime.now(UTC)),
        "evaluation_at_utc": _iso_utc(evaluation_utc),
        "evaluation_at_cst": _cst_text(evaluation_utc),
        "market_snapshot_ts_utc": tick_ts,
        "status": status,
        "method": {
            "timeframes": list(TIMEFRAMES),
            "weights": TIMEFRAME_WEIGHTS,
            "closed_candles_only": True,
            "liquidity_quote_volume_formula": "last * contracts_24h * contract_value",
            "score_semantics": "uncalibrated evidence alignment, not probability",
            "news_zero_event_semantics": "valid no-event state, not missing data",
        },
        "targets": {
            "minimum_completeness_pct_each_family": min_completeness_pct,
            "minimum_unique_symbols_screened_daily": min_screened_symbols,
            "minimum_snapshot_rows_for_plus_50pct_daily_signal_target": 993,
            "minimum_full_snapshots_per_day_for_plus_50pct_target": math.ceil(993 / universe) if universe else None,
            "decision_credibility_pct": 90.0,
        },
        "metrics": {
            "universe_symbols": universe,
            "judgment_records": universe,
            "universal_market_data_complete_symbols": universal_complete,
            "universal_market_data_completeness_pct": universal_pct,
            "analysis_ready_symbols": analysis_ready,
            "analysis_ready_pct": analysis_ready_pct,
            "liquidity_eligible_symbols": eligible,
            "eligible_enriched_symbols": eligible_enriched,
            "eligible_enrichment_pct": enrichment_pct,
            "judgment_counts": dict(sorted(judgment_counts.items())),
            "execution_readiness_counts": dict(sorted(readiness_counts.items())),
        },
        "news_health": news_health,
        "quality_gates": gates,
        "top_shadow_long": top("long_bias"),
        "top_shadow_short": top("short_bias"),
        "records": records,
        "production_mutation": False,
        "orders_placed": 0,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a read-only full-universe 15m/1H/4H shadow judgment JSON"
    )
    parser.add_argument("--db-root", type=Path, default=DEFAULT_DB_ROOT)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--as-of", help="Evaluation timestamp, ISO-8601; default now UTC")
    parser.add_argument("--cycle-id", help="CST cycle id YYYY-MM-DDTHH:MM")
    parser.add_argument("--min-quote-volume-usd", type=float, default=DEFAULT_MIN_QUOTE_VOLUME_USD)
    parser.add_argument("--min-oi-usd", type=float, default=DEFAULT_MIN_OI_USD)
    parser.add_argument("--min-completeness-pct", type=float, default=DEFAULT_MIN_COMPLETENESS_PCT)
    parser.add_argument("--min-screened-symbols", type=int, default=DEFAULT_MIN_SCREENED_SYMBOLS)
    parser.add_argument("--news-fresh-minutes", type=int, default=DEFAULT_NEWS_FRESH_MINUTES)
    parser.add_argument("--feature-fresh-minutes", type=int, default=DEFAULT_FEATURE_FRESH_MINUTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        evaluation_utc = _parse_utc(args.as_of) if args.as_of else datetime.now(UTC)
        cycle_id = args.cycle_id or evaluation_utc.astimezone(CST).strftime("%Y-%m-%dT%H:%M")
        if not CYCLE_RE.fullmatch(cycle_id):
            raise ValueError("cycle_id must be YYYY-MM-DDTHH:MM")
        if not 0 < args.min_completeness_pct <= 100:
            raise ValueError("min_completeness_pct must be in (0,100]")
        payload = build_snapshot(
            args.db_root,
            evaluation_utc=evaluation_utc,
            cycle_id=cycle_id,
            min_quote_volume_usd=args.min_quote_volume_usd,
            min_oi_usd=args.min_oi_usd,
            min_completeness_pct=args.min_completeness_pct,
            min_screened_symbols=args.min_screened_symbols,
            news_fresh_minutes=args.news_fresh_minutes,
            feature_fresh_minutes=args.feature_fresh_minutes,
        )
        _atomic_write_json(args.json_out, payload)
        receipt = {
            "ok": payload["status"] == "ready_for_shadow_evaluation",
            "status": payload["status"],
            "json_out": str(args.json_out.resolve()),
            "cycle_id": payload["cycle_id"],
            "universe_symbols": payload["metrics"]["universe_symbols"],
            "judgment_records": payload["metrics"]["judgment_records"],
            "universal_market_data_completeness_pct": payload["metrics"]["universal_market_data_completeness_pct"],
            "analysis_ready_pct": payload["metrics"]["analysis_ready_pct"],
            "eligible_enrichment_pct": payload["metrics"]["eligible_enrichment_pct"],
            "production_mutation": False,
            "orders_placed": 0,
        }
        print(json.dumps(receipt, ensure_ascii=False, allow_nan=False))
        return 0 if receipt["ok"] else 2
    except Exception as exc:  # noqa: BLE001 - fail closed with one structured receipt
        print(json.dumps({
            "ok": False,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "json_out": str(args.json_out),
            "production_mutation": False,
            "orders_placed": 0,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
