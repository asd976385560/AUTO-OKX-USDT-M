# -*- coding: utf-8 -*-
"""公开宏观数据的确定性解析、入库与核验工具。

数据边界：
  - crypto_fear_greed：Alternative.me 自有指数 API。
  - dxy_calc_ecb：ICE 公布公式 + ECB 官方日参考汇率复算；不是 ICE 官方报价。
  - btc_spot_etf_net_flow_usd：SoSoValue API（有 key 时）或 news-scout 已落库的
    Farside/SoSoValue 权威证据。单源仅 provisional；双源同日同口径一致才生成
    consensus 行，供 cross_market 硬字段使用。

本模块不改变交易判断，只提供可追溯事实与明确的 verification status。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from _http import get_json

METRIC_FEAR_GREED = "crypto_fear_greed"
METRIC_DXY_ECB = "dxy_calc_ecb"
METRIC_BTC_ETF = "btc_spot_etf_net_flow_usd"

SOURCE_ALTERNATIVE = "alternative_me"
SOURCE_ECB_DXY = "ecb_ice_formula"
SOURCE_FARSIDE = "farside"
SOURCE_SOSOVALUE = "sosovalue"
SOURCE_ETF_CONSENSUS = "consensus_farside_sosovalue"

ALTERNATIVE_URL = "https://api.alternative.me/fng/"
ECB_90D_URL = (
    "https://www.ecb.europa.eu/stats/eurofxref/"
    "eurofxref-hist-90d.xml"
)
SOSOVALUE_URL = (
    "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart"
)
FARSIDE_PUBLIC_URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"

ICE_DXY_CONSTANT = 50.14348112
REQUIRED_ECB_CURRENCIES = ("USD", "JPY", "GBP", "CAD", "SEK", "CHF")
ETF_TOLERANCE_USD = 5_000_000.0
ETF_TOLERANCE_RATIO = 0.01

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS macro_observations (
    metric           TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    source           TEXT NOT NULL,
    collected_at     TEXT NOT NULL,
    value            REAL,
    unit             TEXT,
    label            TEXT,
    status           TEXT NOT NULL,
    source_url       TEXT,
    raw              TEXT,
    PRIMARY KEY (metric, observation_date, source)
);
CREATE INDEX IF NOT EXISTS idx_macro_observations_metric_date
    ON macro_observations(metric, observation_date DESC);
CREATE INDEX IF NOT EXISTS idx_macro_observations_source_date
    ON macro_observations(source, observation_date DESC);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def table_exists(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='macro_observations'"
    ).fetchone()
    return row is not None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def upsert_observations(
    con: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> int:
    payload = []
    for row in rows:
        metric = str(row.get("metric") or "").strip()
        observed = str(row.get("observation_date") or "").strip()[:10]
        source = str(row.get("source") or "").strip()
        status = str(row.get("status") or "").strip()
        if not metric or len(observed) != 10 or not source or not status:
            continue
        payload.append(
            (
                metric,
                observed,
                source,
                str(row.get("collected_at") or utc_now_iso()),
                _finite(row.get("value")),
                row.get("unit"),
                row.get("label"),
                status,
                row.get("source_url"),
                _json(row.get("raw") or {}),
            )
        )
    if not payload:
        return 0
    con.executemany(
        "INSERT OR REPLACE INTO macro_observations "
        "(metric,observation_date,source,collected_at,value,unit,label,status,"
        "source_url,raw) VALUES (?,?,?,?,?,?,?,?,?,?)",
        payload,
    )
    return len(payload)


def parse_alternative_payload(
    payload: dict[str, Any], collected_at: str | None = None
) -> list[dict[str, Any]]:
    collected_at = collected_at or utc_now_iso()
    rows: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        value = _finite(item.get("value"))
        try:
            observed = datetime.fromtimestamp(
                int(str(item.get("timestamp"))), tz=timezone.utc
            ).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            continue
        if value is None or not 0 <= value <= 100:
            continue
        rows.append(
            {
                "metric": METRIC_FEAR_GREED,
                "observation_date": observed,
                "source": SOURCE_ALTERNATIVE,
                "collected_at": collected_at,
                "value": value,
                "unit": "index_0_100",
                "label": str(item.get("value_classification") or "") or None,
                "status": "official_primary",
                "source_url": ALTERNATIVE_URL,
                "raw": item,
            }
        )
    return rows


def fetch_alternative(
    client, *, backfill: bool = False
) -> list[dict[str, Any]]:
    payload = get_json(
        client,
        ALTERNATIVE_URL,
        params={"limit": 0 if backfill else 10, "format": "json"},
        timeout=25.0,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Alternative.me response is not an object")
    return parse_alternative_payload(payload)


def parse_ecb_xml(xml_text: str) -> list[tuple[str, dict[str, float]]]:
    root = ElementTree.fromstring(xml_text)
    out: list[tuple[str, dict[str, float]]] = []
    for elem in root.iter():
        observed = elem.attrib.get("time")
        if not observed:
            continue
        rates: dict[str, float] = {}
        for child in elem:
            currency = child.attrib.get("currency")
            value = _finite(child.attrib.get("rate"))
            if currency and value is not None:
                rates[currency] = value
        if all(currency in rates for currency in REQUIRED_ECB_CURRENCIES):
            out.append((observed[:10], rates))
    out.sort(key=lambda row: row[0])
    return out


def calculate_dxy_from_ecb(rates: dict[str, float]) -> float:
    """按 ICE USDX 公式，用 ECB 的 EUR-base 日参考汇率复算指数绝对值。"""
    missing = [ccy for ccy in REQUIRED_ECB_CURRENCIES if ccy not in rates]
    if missing:
        raise ValueError(f"ECB rates missing: {','.join(missing)}")
    usd, jpy, gbp, cad, sek, chf = (
        float(rates[ccy]) for ccy in REQUIRED_ECB_CURRENCIES
    )
    if min(usd, jpy, gbp, cad, sek, chf) <= 0:
        raise ValueError("ECB rates must be positive")
    eurusd = usd
    usdjpy = jpy / usd
    gbpusd = usd / gbp
    usdcad = cad / usd
    usdsek = sek / usd
    usdchf = chf / usd
    return ICE_DXY_CONSTANT * (
        eurusd ** -0.576
        * usdjpy ** 0.136
        * gbpusd ** -0.119
        * usdcad ** 0.091
        * usdsek ** 0.042
        * usdchf ** 0.036
    )


def ecb_rows(
    xml_text: str, collected_at: str | None = None
) -> list[dict[str, Any]]:
    collected_at = collected_at or utc_now_iso()
    out = []
    for observed, rates in parse_ecb_xml(xml_text):
        value = calculate_dxy_from_ecb(rates)
        out.append(
            {
                "metric": METRIC_DXY_ECB,
                "observation_date": observed,
                "source": SOURCE_ECB_DXY,
                "collected_at": collected_at,
                "value": value,
                "unit": "index",
                "label": "ECB reference-rate calculation",
                "status": "calculated_public",
                "source_url": ECB_90D_URL,
                "raw": {
                    "rates_eur_base": {
                        key: rates[key] for key in REQUIRED_ECB_CURRENCIES
                    },
                    "formula": (
                        "50.14348112*EURUSD^-0.576*USDJPY^0.136*"
                        "GBPUSD^-0.119*USDCAD^0.091*USDSEK^0.042*"
                        "USDCHF^0.036"
                    ),
                    "is_ice_official_quote": False,
                },
            }
        )
    return out


def fetch_ecb_dxy(client) -> list[dict[str, Any]]:
    response = client.get(
        ECB_90D_URL,
        headers={"accept": "application/xml,text/xml;q=0.9,*/*;q=0.8"},
        timeout=30.0,
    )
    response.raise_for_status()
    rows = ecb_rows(response.text)
    if not rows:
        raise RuntimeError("ECB response contained no complete six-currency rows")
    return rows


def _source_id(name: Any) -> str | None:
    value = str(name or "").strip().lower()
    if "farside" in value:
        return SOURCE_FARSIDE
    if "sosovalue" in value or "soso value" in value:
        return SOURCE_SOSOVALUE
    return None


def _usd_value(value: Any, unit: Any) -> float | None:
    number = _finite(value)
    if number is None:
        return None
    norm = str(unit or "USD").strip().lower().replace(" ", "")
    if norm in {"us$m", "usdm", "usd_m", "millionusd", "usdmm"}:
        return number * 1_000_000.0
    return number


def evidence_rows(
    records: Iterable[sqlite3.Row | dict[str, Any]],
    collected_at: str | None = None,
) -> list[dict[str, Any]]:
    """把 news_items.raw 中的 Farside/SoSoValue 日净流证据标准化。"""
    collected_at = collected_at or utc_now_iso()
    out: list[dict[str, Any]] = []
    for record in records:
        try:
            raw_text = record["raw"]
        except (KeyError, IndexError, TypeError):
            raw_text = None
        try:
            raw = json.loads(raw_text or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        metric = str(raw.get("metric") or "").strip().lower()
        if metric not in {
            "btc_spot_etf_daily_net_flow",
            "btc_spot_etf_net_flow_usd",
            "btc_etf_net_flow",
        }:
            continue
        observed = str(raw.get("as_of") or "")[:10]
        if len(observed) != 10:
            continue
        candidates: list[dict[str, Any]] = []
        if raw.get("value") is not None:
            candidates.append(
                {
                    "source": raw.get("source_name"),
                    "value": raw.get("value"),
                    "unit": raw.get("unit"),
                    "url": raw.get("source_url"),
                }
            )
        for item in raw.get("source_values") or []:
            if isinstance(item, dict):
                candidates.append(item)
        for item in candidates:
            source = _source_id(item.get("source"))
            value = _usd_value(item.get("value"), item.get("unit") or raw.get("unit"))
            if source is None or value is None:
                continue
            out.append(
                {
                    "metric": METRIC_BTC_ETF,
                    "observation_date": observed,
                    "source": source,
                    "collected_at": collected_at,
                    "value": value,
                    "unit": "USD",
                    "label": "US spot BTC ETFs",
                    "status": "source_reported",
                    "source_url": (
                        item.get("url")
                        or raw.get("source_url")
                        or (
                            FARSIDE_PUBLIC_URL
                            if source == SOURCE_FARSIDE
                            else "https://sosovalue.com/assets/etf/us-btc-spot"
                        )
                    ),
                    "raw": {
                        "evidence": raw,
                        "verification_status": raw.get("verification_status"),
                    },
                }
            )
    # 同一批次、同一来源/日期可能同时来自 top-level 和 source_values；保留最后一个。
    unique = {
        (row["metric"], row["observation_date"], row["source"]): row
        for row in out
    }
    return list(unique.values())


def import_xsearch_etf(
    news_con: sqlite3.Connection, regime_con: sqlite3.Connection
) -> int:
    try:
        records = news_con.execute(
            "SELECT raw FROM news_items WHERE source='x_search' "
            "AND tags LIKE '%authoritative_data%' AND raw IS NOT NULL "
            "ORDER BY id DESC LIMIT 500"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    return upsert_observations(regime_con, evidence_rows(records))


def parse_sosovalue_payload(
    payload: dict[str, Any], collected_at: str | None = None
) -> list[dict[str, Any]]:
    collected_at = collected_at or utc_now_iso()
    data = payload.get("data") or {}
    items = data.get("list") if isinstance(data, dict) else None
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        observed = str(item.get("date") or "")[:10]
        value = _finite(item.get("totalNetInflow"))
        if len(observed) != 10 or value is None:
            continue
        out.append(
            {
                "metric": METRIC_BTC_ETF,
                "observation_date": observed,
                "source": SOURCE_SOSOVALUE,
                "collected_at": collected_at,
                "value": value,
                "unit": "USD",
                "label": "US spot BTC ETFs",
                "status": "source_reported",
                "source_url": SOSOVALUE_URL,
                "raw": item,
            }
        )
    return out


def fetch_sosovalue(client, api_key: str | None = None) -> list[dict[str, Any]]:
    key = (api_key or os.environ.get("SOSOVALUE_API_KEY") or "").strip()
    if not key:
        return []
    response = client.post(
        SOSOVALUE_URL,
        headers={"x-soso-api-key": key, "content-type": "application/json"},
        json={"type": "us-btc-spot"},
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") not in (0, "0", None):
        raise RuntimeError(f"SoSoValue API error: {payload.get('msg')}")
    return parse_sosovalue_payload(payload)


def reconcile_etf_consensus(con: sqlite3.Connection) -> dict[str, int]:
    """同交易日 Farside 与 SoSoValue 一致时生成 consensus；冲突则写 NULL conflict。"""
    rows = con.execute(
        "SELECT observation_date,source,value,collected_at,source_url "
        "FROM macro_observations WHERE metric=? AND source IN (?,?) "
        "AND value IS NOT NULL ORDER BY observation_date",
        (METRIC_BTC_ETF, SOURCE_FARSIDE, SOURCE_SOSOVALUE),
    ).fetchall()
    grouped: dict[str, dict[str, sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["observation_date"], {})[row["source"]] = row
    confirmed = conflicts = 0
    payload = []
    for observed, sources in grouped.items():
        if SOURCE_FARSIDE not in sources or SOURCE_SOSOVALUE not in sources:
            continue
        farside = float(sources[SOURCE_FARSIDE]["value"])
        soso = float(sources[SOURCE_SOSOVALUE]["value"])
        diff = abs(farside - soso)
        tolerance = max(
            ETF_TOLERANCE_USD,
            ETF_TOLERANCE_RATIO * max(abs(farside), abs(soso)),
        )
        matches = diff <= tolerance
        payload.append(
            {
                "metric": METRIC_BTC_ETF,
                "observation_date": observed,
                "source": SOURCE_ETF_CONSENSUS,
                "collected_at": utc_now_iso(),
                # Farside 是公开主表；不取平均，避免发明第三个数。
                "value": farside if matches else None,
                "unit": "USD",
                "label": "US spot BTC ETFs",
                "status": "cross_checked" if matches else "conflict",
                "source_url": FARSIDE_PUBLIC_URL,
                "raw": {
                    "farside": farside,
                    "sosovalue": soso,
                    "difference_usd": diff,
                    "tolerance_usd": tolerance,
                },
            }
        )
        if matches:
            confirmed += 1
        else:
            conflicts += 1
    upsert_observations(con, payload)
    return {"cross_checked": confirmed, "conflicts": conflicts}


def latest_observation(
    con: sqlite3.Connection,
    metric: str,
    *,
    sources: tuple[str, ...] | None = None,
    statuses: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    clauses = ["metric=?"]
    params: list[Any] = [metric]
    if sources:
        clauses.append("source IN (" + ",".join("?" for _ in sources) + ")")
        params.extend(sources)
    if statuses:
        clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    row = con.execute(
        "SELECT metric,observation_date,source,collected_at,value,unit,label,"
        "status,source_url,raw FROM macro_observations WHERE "
        + " AND ".join(clauses)
        + " ORDER BY observation_date DESC,collected_at DESC LIMIT 1",
        params,
    ).fetchone()
    return dict(row) if row else None


def latest_snapshot(con: sqlite3.Connection) -> dict[str, Any]:
    if not table_exists(con):
        return {}
    fear = latest_observation(
        con, METRIC_FEAR_GREED, sources=(SOURCE_ALTERNATIVE,)
    )
    dxy_rows = con.execute(
        "SELECT observation_date,value,status,collected_at FROM macro_observations "
        "WHERE metric=? AND source=? AND value IS NOT NULL "
        "ORDER BY observation_date DESC LIMIT 2",
        (METRIC_DXY_ECB, SOURCE_ECB_DXY),
    ).fetchall()
    dxy = dict(dxy_rows[0]) if dxy_rows else None
    dxy_d1 = None
    if len(dxy_rows) >= 2 and dxy_rows[1]["value"] not in (None, 0):
        dxy_d1 = (
            float(dxy_rows[0]["value"]) / float(dxy_rows[1]["value"]) - 1.0
        )
    etf_confirmed = latest_observation(
        con,
        METRIC_BTC_ETF,
        sources=(SOURCE_ETF_CONSENSUS,),
        statuses=("cross_checked",),
    )
    etf_provisional = latest_observation(
        con,
        METRIC_BTC_ETF,
        sources=(SOURCE_FARSIDE, SOURCE_SOSOVALUE),
        statuses=("source_reported",),
    )
    etf_conflict = latest_observation(
        con,
        METRIC_BTC_ETF,
        sources=(SOURCE_ETF_CONSENSUS,),
        statuses=("conflict",),
    )
    return {
        "fear_greed": fear,
        "dxy_calc_ecb": dxy,
        "dxy_calc_ecb_d1": dxy_d1,
        "etf_confirmed": etf_confirmed,
        "etf_provisional": etf_provisional,
        "etf_conflict": etf_conflict,
    }


def source_dates(con: sqlite3.Connection) -> dict[str, str | None]:
    """registry source id -> 最新 observation_date（date-only）。"""
    if not table_exists(con):
        return {
            "macro_dxy_calc_ecb": None,
            "macro_etf_flow": None,
            "macro_fear_greed": None,
        }
    mapping = {
        "macro_dxy_calc_ecb": METRIC_DXY_ECB,
        "macro_etf_flow": METRIC_BTC_ETF,
        "macro_fear_greed": METRIC_FEAR_GREED,
    }
    out: dict[str, str | None] = {}
    for source_id, metric in mapping.items():
        row = con.execute(
            "SELECT MAX(observation_date) FROM macro_observations WHERE metric=? "
            "AND status!='conflict' AND value IS NOT NULL",
            (metric,),
        ).fetchone()
        out[source_id] = row[0] if row and row[0] else None
    return out


def open_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con
