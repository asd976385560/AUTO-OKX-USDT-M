# -*- coding: utf-8 -*-
"""Deterministic trade statistics for daily/weekly reviewer reports.

Facts are split deliberately:

* filled opens/closes come from ``live_trades.db``;
* risk-rejected open attempts come from ``ledger.db.execution_intents``;
* rejected or incomplete rows in ``trades`` never count as fills.

This module is read-only.  It can also be called as a CLI so the reviewer uses
the same facts before rendering QQ text and before invoking the report writer.
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import hashlib
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
# 日报事实窗锚点（CST）。cron 08:05 触发，窗口闭合在 08:00，留 5min 让数据落定。
# 固定锚点而非跟随报告 ts：报告 ts 由 agent 写入且历史上会漂，跟随会造成
# 相邻日报缺口/重叠。改锚点需同步 validate_daily_report._expected_daily_window
# （该处刻意保持独立实现，勿改为共享此常量）。
DAILY_ANCHOR_HOUR = 8
DAILY_ANCHOR_MINUTE = 0
PROFILE_DB = {
    "live": Path(_public_project_path('db', 'live_trades.db')),
}
LEDGER_DB = Path(_public_project_path('db', 'ledger.db'))
FILL_ACTIONS = {"open", "close"}
POSITION_INCREASE_ACTIONS = {"open", "add"}
POSITION_DECREASE_ACTIONS = {"close", "reduce"}
# 2026-08-19 P0-1：reduce 是**已实现盈亏**却长期被 FILL_ACTIONS 排除在头条外
# （8 月 close 67 笔 -277.6129 / reduce 5 笔 +82.5031 被丢，头条误差 42%；
#  2026-08-16 日报因此符号翻转 -44.71 → 实为 +27.95）。新增独立集合而不是把
# reduce 塞进 FILL_ACTIONS —— open_count/close_count 是「建仓/清仓笔数」语义，
# reduce 不是 close，混入会污染 close_side_breakdown 与 weekly_reports.win_rate，
# 并让 85 份历史报告的三方等式全挂。
REALIZING_ACTIONS = {"close", "reduce"}

# The briefing-layer source is a forward-only producer fact.  Windows that
# reach before this boundary cannot prove complete source coverage.
MISSED_OPPORTUNITY_BRIEFING_SOURCE_ACTIVATION_CST = (
    "2026-08-19 08:00:00"
)
MISSED_OPPORTUNITY_SIDE_NEUTRAL_POLICY_CST = (
    "2026-09-02 13:15:00"
)
MISSED_OPPORTUNITY_SIDE_NEUTRAL_ACTIVATION_CST = (
    "2026-09-03 04:00:00"
)
MISSED_OPPORTUNITY_SIDE_NEUTRAL_SOURCE_TAG = (
    "briefing_symbol_review_v2"
)
MISSED_OPPORTUNITY_EVIDENCE_SCHEMA_VERSION = 1
MISSED_OPPORTUNITY_OUTCOME_HOURS = 4
MISSED_OPPORTUNITY_REQUIRED_15M_BARS = 16
MISSED_OPPORTUNITY_FIXED_1R_PCT = 2.0
MISSED_OPPORTUNITY_DIAGNOSTIC_LIMIT = 50
_MISSED_CYCLE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)$"
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def now_cst() -> str:
    return datetime.now(CST).strftime(TS_FMT)


def parse_cst(value: str | datetime) -> datetime:
    """Parse supported project timestamps and return a CST-aware datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("timestamp is empty")
        if len(text) == 10:
            text += " 00:00:00"
        elif len(text) == 16:
            text += ":00"
        text = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def fmt_ts(value: str | datetime) -> str:
    return parse_cst(value).strftime(TS_FMT)


def daily_window(as_of_ts: str | datetime) -> tuple[str, str]:
    """Return the fixed 24h reviewer window ``[前一日 08:00, 当日 08:00)``.

    The reviewer runs at 08:05 CST.  Anchoring the start at same-day midnight
    left every day's 08:05-24:00 trades outside all daily reports.  Anchoring
    on ``as_of_ts`` itself fixed the coverage hole but re-introduced drift:
    the report ts is agent-written and historically wandered (08:00 / 08:06 /
    08:12 / 08:36 ...), so one minute of jitter shifted the window and made
    consecutive reports gap or overlap.  Pinning both edges to 08:00 keeps the
    window deterministic and exactly tiling regardless of trigger jitter, and
    closes it 5 minutes before the run so the data has settled.
    """
    ref = parse_cst(as_of_ts)
    end = ref.replace(
        hour=DAILY_ANCHOR_HOUR, minute=DAILY_ANCHOR_MINUTE,
        second=0, microsecond=0)
    if ref < end:
        # Triggered before the anchor (manual re-run / early fire): report the
        # last complete window rather than a future-ending one.
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    return start.strftime(TS_FMT), end.strftime(TS_FMT)


def weekly_window(week_start_ts: str | datetime) -> tuple[str, str]:
    """Return ``[上周一 08:00, 本周一 08:00)`` for the given 本周一 report key.

    Anchored on the same 08:00 boundary as :func:`daily_window` so the seven
    daily windows of a week tile this interval exactly — a calendar-midnight
    weekly window would sit 8h out of phase and make the dailies unable to
    reconcile against the weekly.  ``week_start_ts`` stays the 本周一 00:00:00
    report key; only the fact window is anchored.
    """
    anchor = parse_cst(week_start_ts).replace(
        hour=DAILY_ANCHOR_HOUR, minute=DAILY_ANCHOR_MINUTE,
        second=0, microsecond=0)
    start = anchor - timedelta(days=7)
    return start.strftime(TS_FMT), anchor.strftime(TS_FMT)


def monthly_window(month_start_ts: str | datetime) -> tuple[str, str]:
    """Return the previous complete calendar month on the 08:00 fact anchor.

    ``month_start_ts`` is the current month's report key (day 1 at 00:00).
    Facts cover ``[previous month day 1 08:00, current month day 1 08:00)``
    so the interval is exactly tiled by the existing daily report windows.
    """
    report_month = parse_cst(month_start_ts)
    end = report_month.replace(
        day=1,
        hour=DAILY_ANCHOR_HOUR,
        minute=DAILY_ANCHOR_MINUTE,
        second=0,
        microsecond=0,
    )
    previous_month_last_day = end - timedelta(days=1)
    start = previous_month_last_day.replace(
        day=1,
        hour=DAILY_ANCHOR_HOUR,
        minute=DAILY_ANCHOR_MINUTE,
        second=0,
        microsecond=0,
    )
    return start.strftime(TS_FMT), end.strftime(TS_FMT)


def rolling_window(
    as_of_ts: str | datetime, days: int
) -> tuple[str, str]:
    if days <= 0:
        raise ValueError("days must be positive")
    end = parse_cst(as_of_ts)
    start = end - timedelta(days=days)
    return start.strftime(TS_FMT), end.strftime(TS_FMT)


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not Path(path).exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(
        f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10
    )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _missed_canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _missed_sha256(value: Any) -> str:
    return hashlib.sha256(_missed_canonical_bytes(value)).hexdigest()


def _missed_bounded(values: list[Any]) -> dict:
    limit = MISSED_OPPORTUNITY_DIAGNOSTIC_LIMIT
    return {
        "count": len(values),
        "items": values[:limit],
        "truncated": len(values) > limit,
        "limit": limit,
    }


def _seal_missed_opportunity_contract(receipt: dict) -> dict:
    """Enforce the four-state release/count invariant and self-hash."""
    if receipt.get("status") == "COMPLETE":
        receipt["release_eligible"] = True
        receipt["count"] = int(receipt.get("count") or 0)
    else:
        receipt["release_eligible"] = False
        receipt["count"] = None
    receipt.pop("self_sha256", None)
    receipt["self_sha256"] = _missed_sha256(receipt)
    return receipt


def _missed_table_contract(
    con: sqlite3.Connection,
    table: str,
    required_columns: set[str],
) -> dict:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None:
        raise ValueError(f"required table missing: {table}")
    table_info = list(con.execute(f'PRAGMA table_info("{table}")'))
    by_name = {str(item[1]): item for item in table_info}
    missing = sorted(required_columns - set(by_name))
    if missing:
        raise ValueError(
            f"required columns missing: {table}." + ",".join(missing))
    return {
        "table": table,
        # Bind only the fields consumed by this contract.  Full CREATE SQL or
        # unrelated columns (for example a future simulation diagnostic) must
        # not drift an already published receipt and retroactively rejudge it.
        "required_columns": [
            {
                "name": name,
                "type": str(by_name[name][2] or "").strip().upper(),
                "notnull": int(by_name[name][3] or 0),
                "pk": int(by_name[name][5] or 0),
            }
            for name in sorted(required_columns)
        ],
    }


def _missed_cycle_id(value: datetime) -> str:
    return value.astimezone(CST).strftime("%Y-%m-%dT%H:%M")


def _missed_cycle_ts(value: str) -> str:
    return value.replace("T", " ") + ":00"


def _missed_cycle_utc(value: str) -> datetime:
    local = datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    return local.astimezone(timezone.utc)


def _missed_utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _missed_error_receipt(
    *,
    report_start_ts: Any,
    report_end_ts: Any,
    contract_activation_cst: Any,
    error: Exception,
) -> dict:
    contract = {
        "schema_version": MISSED_OPPORTUNITY_EVIDENCE_SCHEMA_VERSION,
        "source_schema": "briefing_candidates_v1",
        "source_activation_cst": (
            MISSED_OPPORTUNITY_BRIEFING_SOURCE_ACTIVATION_CST),
        "contract_activation_cst": str(contract_activation_cst or ""),
        "timezone": "Asia/Shanghai(+08:00)",
        "cycle_interval_minutes": 15,
        "producer_bucket_anchor_cst": "04:00:00",
        "first_seen_key": ["producer_bucket_start", "symbol", "side"],
        "trade_exclusion": "any trade row excludes its symbol for the bucket",
        "outcome_horizon_hours": MISSED_OPPORTUNITY_OUTCOME_HOURS,
        "required_15m_bars": MISSED_OPPORTUNITY_REQUIRED_15M_BARS,
        "fixed_1r_pct": MISSED_OPPORTUNITY_FIXED_1R_PCT,
    }
    receipt = {
        "schema_version": MISSED_OPPORTUNITY_EVIDENCE_SCHEMA_VERSION,
        "artifact_type": "missed_opportunity_evidence_contract",
        "contract_activation_cst": str(contract_activation_cst or ""),
        "contract_active": False,
        "status": "ERROR",
        "release_eligible": False,
        "count": None,
        "report_window": {
            "start_ts": str(report_start_ts or ""),
            "end_ts": str(report_end_ts or ""),
            "end_exclusive": True,
        },
        "candidate_window": {
            "start_ts": None,
            "end_ts": None,
            "end_exclusive": True,
            "outcome_horizon_hours": MISSED_OPPORTUNITY_OUTCOME_HOURS,
            "required_15m_bars": MISSED_OPPORTUNITY_REQUIRED_15M_BARS,
        },
        "source_coverage": {
            "source_activation_cst": (
                MISSED_OPPORTUNITY_BRIEFING_SOURCE_ACTIVATION_CST),
            "expected_cycles": 0,
            "covered_cycles": 0,
            "missing_cycles": 0,
            "duplicate_cycles": 0,
            "invalid_rows": 0,
            "source_contiguous_watermark_cycle": None,
            "source_contiguous_watermark_end_ts": None,
            "max_seen_cycle": None,
        },
        "producer_watermark_end_ts": None,
        "producer_buckets": [],
        "first_seen": {"pair_count": 0},
        "trade_exclusion": {"row_count": 0, "symbol_count": 0},
        "outcome_coverage": {
            "expected_result_count": 0,
            "observed_result_count": 0,
            "no_data_count": 0,
            "missing_result_count": 0,
            "extra_result_count": 0,
            "duplicate_result_count": 0,
            "mismatched_result_count": 0,
        },
        "hashes": {
            "contract_sha256": _missed_sha256(contract),
            "schema_sha256": None,
            "snapshot_input_sha256": None,
            "trade_exclusion_sha256": None,
            "outcome_input_sha256": None,
            "expected_keys_sha256": None,
            "observed_results_sha256": None,
        },
        "diagnostics": {
            "reason_codes": ["contract_build_error"],
            "errors": _missed_bounded([
                f"{type(error).__name__}: {str(error)[:500]}"
            ]),
            "missing_cycle_ids": _missed_bounded([]),
            "duplicate_cycle_ids": _missed_bounded([]),
            "invalid_snapshot_rows": _missed_bounded([]),
            "no_data_keys": _missed_bounded([]),
            "missing_result_keys": _missed_bounded([]),
            "extra_result_keys": _missed_bounded([]),
            "duplicate_result_keys": _missed_bounded([]),
            "mismatched_result_keys": _missed_bounded([]),
        },
        "safety": {
            "sqlite_mode": "ro",
            "production_database_writes": 0,
            "orders_placed": 0,
        },
    }
    return _seal_missed_opportunity_contract(receipt)


def missed_opportunity_evidence_contract(
    *,
    report_start_ts: str | datetime,
    report_end_ts: str | datetime,
    lessons_db: str | Path,
    live_trades_db: str | Path,
    market_db: str | Path,
    briefing_dir: str | Path,
    contract_activation_cst: str | datetime,
) -> dict:
    """Independently rebuild the missed-opportunity producer evidence contract.

    The function is deliberately read-only: briefing JSONL is opened for read
    and every SQLite connection uses URI ``mode=ro``.  It does not trust
    ``MAX(missed_opportunities.ts)`` as a source watermark.  Source completeness
    is the exact set of scheduled 15-minute cycles, and producer first-seen
    state is reset for every ``[04:00, next 04:00)`` bucket.

    Only ``COMPLETE`` returns an integer ``count`` (including legitimate zero)
    and ``release_eligible=true``.  Every other state returns ``count=null``.
    """
    try:
        report_start = parse_cst(report_start_ts)
        report_end = parse_cst(report_end_ts)
        contract_activation = parse_cst(contract_activation_cst)
        source_activation = parse_cst(
            MISSED_OPPORTUNITY_BRIEFING_SOURCE_ACTIVATION_CST)
        if report_end <= report_start:
            raise ValueError("report window must be positive")

        shift = timedelta(hours=MISSED_OPPORTUNITY_OUTCOME_HOURS)
        candidate_start = report_start - shift
        candidate_end = report_end - shift
        if any((candidate_start.minute, candidate_start.second,
                candidate_start.microsecond, candidate_start.hour != 4)):
            raise ValueError("candidate window start must be aligned to 04:00 CST")
        if any((candidate_end.minute, candidate_end.second,
                candidate_end.microsecond, candidate_end.hour != 4)):
            raise ValueError("candidate window end must be aligned to 04:00 CST")
        if (candidate_end - candidate_start).total_seconds() % 86400:
            raise ValueError("candidate window must contain complete 24h buckets")

        contract = {
            "schema_version": MISSED_OPPORTUNITY_EVIDENCE_SCHEMA_VERSION,
            "source_schema": "briefing_candidates_v1",
            "source_activation_cst": source_activation.strftime(TS_FMT),
            "contract_activation_cst": contract_activation.strftime(TS_FMT),
            "timezone": "Asia/Shanghai(+08:00)",
            "cycle_interval_minutes": 15,
            "producer_bucket_anchor_cst": "04:00:00",
            "first_seen_key": ["producer_bucket_start", "symbol", "side"],
            "trade_exclusion": (
                "any live_trades.trades row in the producer bucket excludes "
                "its symbol for that bucket"),
            "outcome_horizon_hours": MISSED_OPPORTUNITY_OUTCOME_HOURS,
            "required_15m_bars": MISSED_OPPORTUNITY_REQUIRED_15M_BARS,
            "fixed_1r_pct": MISSED_OPPORTUNITY_FIXED_1R_PCT,
            "result_source_tag": "briefing_layer_v1",
            "result_key": ["ts", "symbol", "direction_hint"],
        }
        contract_active = report_end >= contract_activation

        expected_cycles: list[str] = []
        cursor = candidate_start
        while cursor < candidate_end:
            expected_cycles.append(_missed_cycle_id(cursor))
            cursor += timedelta(minutes=15)
        expected_set = set(expected_cycles)

        bucket_defs: list[tuple[datetime, datetime]] = []
        cursor = candidate_start
        while cursor < candidate_end:
            bucket_defs.append((cursor, cursor + timedelta(days=1)))
            cursor += timedelta(days=1)

        lessons_con = _connect_ro(Path(lessons_db))
        trades_con = _connect_ro(Path(live_trades_db))
        market_con = _connect_ro(Path(market_db))
        analysis_path = Path(lessons_db).with_name("analysis.db")
        analysis_con = (
            _connect_ro(analysis_path)
            if (
                analysis_path.exists()
                and candidate_end > parse_cst(
                    MISSED_OPPORTUNITY_SIDE_NEUTRAL_ACTIVATION_CST)
            )
            else None)
        try:
            schema_contracts = [
                _missed_table_contract(
                    lessons_con, "missed_opportunities", {
                        "ts", "symbol", "direction_hint", "actual_4h_pct",
                        "would_hit_1r_fixed2pct", "notes",
                    }),
                _missed_table_contract(
                    trades_con, "trades", {"cycle_id", "symbol"}),
                _missed_table_contract(
                    market_con, "kline_cache",
                    {"ts", "symbol", "tf", "o", "h", "l", "c"}),
            ]
            analysis_runs: dict[str, str] = {}
            analysis_signals: dict[str, list[dict]] = defaultdict(list)
            if analysis_con is not None:
                schema_contracts.extend([
                    _missed_table_contract(
                        analysis_con, "analysis_runs",
                        {"cycle_id", "status"}),
                    _missed_table_contract(
                        analysis_con, "analysis_signals",
                        {"cycle_id", "symbol", "action", "side"}),
                ])
                analysis_runs = {
                    str(row["cycle_id"]): str(row["status"] or "").lower()
                    for row in analysis_con.execute(
                        "SELECT cycle_id,status FROM analysis_runs "
                        "WHERE cycle_id>=? AND cycle_id<?",
                        (_missed_cycle_id(candidate_start),
                         _missed_cycle_id(candidate_end)),
                    )
                }
                for row in analysis_con.execute(
                    "SELECT cycle_id,symbol,action,side FROM analysis_signals "
                    "WHERE cycle_id>=? AND cycle_id<? "
                    "AND action IN ('open_long','open_short')",
                    (_missed_cycle_id(candidate_start),
                     _missed_cycle_id(candidate_end)),
                ):
                    analysis_signals[str(row["cycle_id"])].append(dict(row))

            snapshots: dict[str, list[dict]] = defaultdict(list)
            snapshot_hash_input: list[dict] = []
            invalid_snapshot_rows: list[str] = []
            analysis_cycle_ids_missing: set[str] = set()
            side_neutral_transition_cycle_ids: set[str] = set()
            first_date = candidate_start.date()
            last_date = (candidate_end - timedelta(microseconds=1)).date()
            expected_cycles_by_date = Counter(
                cycle[:10] for cycle in expected_cycles)
            current_date = first_date
            briefing_root = Path(briefing_dir)
            while current_date <= last_date:
                path = briefing_root / (
                    f"candidates-{current_date.strftime('%Y%m%d')}.jsonl")
                full_date_in_window = (
                    expected_cycles_by_date[current_date.strftime("%Y-%m-%d")]
                    == 96)
                if path.exists():
                    with path.open(encoding="utf-8") as handle:
                        for line_number, raw_line in enumerate(handle, 1):
                            raw = raw_line.strip()
                            if not raw:
                                continue
                            label = f"{path.name}:{line_number}"
                            try:
                                obj = json.loads(raw)
                            except json.JSONDecodeError as exc:
                                # A malformed row has no trustworthy cycle_id.  It
                                # can bind this receipt only when the whole source
                                # date belongs to the requested window; otherwise it
                                # may be a later/out-of-window append on a boundary
                                # date and must not rewrite an old receipt.
                                if full_date_in_window:
                                    invalid_snapshot_rows.append(
                                        f"{label}:json:{exc.msg}")
                                continue
                            if not isinstance(obj, dict):
                                if full_date_in_window:
                                    invalid_snapshot_rows.append(
                                        f"{label}:root_not_object")
                                continue
                            cycle_id = str(obj.get("cycle_id") or "")
                            if not _MISSED_CYCLE_RE.fullmatch(cycle_id):
                                if full_date_in_window:
                                    invalid_snapshot_rows.append(
                                        f"{label}:bad_cycle_id:{cycle_id}")
                                continue
                            try:
                                cycle_dt = datetime.strptime(
                                    cycle_id, "%Y-%m-%dT%H:%M").replace(
                                        tzinfo=CST)
                            except ValueError:
                                if full_date_in_window:
                                    invalid_snapshot_rows.append(
                                        f"{label}:bad_cycle_timestamp:{cycle_id}")
                                continue
                            # Parse cycle identity before validating the rest of the
                            # payload.  Boundary-date files contain legitimate rows
                            # outside this half-open window, and later appends there
                            # must not change the old receipt's status or hashes.
                            if cycle_id not in expected_set:
                                continue
                            if obj.get("schema") != "briefing_candidates_v1":
                                invalid_snapshot_rows.append(
                                    f"{label}:bad_schema:{cycle_id}")
                                continue
                            if cycle_dt.date() != current_date:
                                invalid_snapshot_rows.append(
                                    f"{label}:cycle_file_date_mismatch:{cycle_id}")
                                continue
                            candidates_raw = obj.get("candidates")
                            if not isinstance(candidates_raw, list):
                                invalid_snapshot_rows.append(
                                    f"{label}:candidates_not_list:{cycle_id}")
                                continue
                            candidates: list[dict] = []
                            symbols: set[str] = set()
                            ordinals: set[int] = set()
                            candidate_error = False
                            for index, item in enumerate(candidates_raw, 1):
                                if not isinstance(item, dict):
                                    invalid_snapshot_rows.append(
                                        f"{label}:candidate_not_object:{index}")
                                    candidate_error = True
                                    break
                                symbol = str(item.get("symbol") or "").strip()
                                side = str(item.get("side") or "").strip().lower()
                                layer = str(item.get("layer") or "").strip().lower()
                                ordinal_raw = item.get("ordinal", index)
                                try:
                                    ordinal = int(ordinal_raw)
                                except (TypeError, ValueError):
                                    ordinal = -1
                                legacy_candidate = (
                                    side in {"long", "short"}
                                    and layer in {"mature", "early"}
                                )
                                side_neutral_candidate = (
                                    not side
                                    and layer == "all_market"
                                    and set(item.get("eligible_sides") or [])
                                    == {"long", "short"}
                                    and item.get("opportunity_state")
                                    == "SIDE_NEUTRAL"
                                    and item.get("selected_for_review") is True
                                )
                                ordinal_valid = (
                                    ordinal == index
                                    if legacy_candidate else
                                    ordinal > 0 and ordinal not in ordinals
                                )
                                if (not symbol
                                        or not (legacy_candidate
                                                or side_neutral_candidate)
                                        or not ordinal_valid
                                        or symbol in symbols):
                                    invalid_snapshot_rows.append(
                                        f"{label}:invalid_candidate:{cycle_id}:{index}")
                                    candidate_error = True
                                    break
                                symbols.add(symbol)
                                ordinals.add(ordinal)
                                candidate_id = item.get("candidate_id")
                                if candidate_id is not None:
                                    candidate_id = str(candidate_id)
                                    if not candidate_id:
                                        invalid_snapshot_rows.append(
                                            f"{label}:empty_candidate_id:{cycle_id}:{index}")
                                        candidate_error = True
                                        break
                                candidates.append({
                                    "ordinal": ordinal,
                                    "candidate_id": candidate_id,
                                    "symbol": symbol,
                                    "side": side or None,
                                    "layer": layer,
                                    "direction_source": (
                                        "analysis_signals_v2"
                                        if side_neutral_candidate else
                                        "briefing_candidate_side_v1"),
                                })
                            if candidate_error:
                                continue
                            side_neutral = any(
                                candidate.get("side") is None
                                for candidate in candidates)
                            if side_neutral:
                                policy_cycle = str(
                                    MISSED_OPPORTUNITY_SIDE_NEUTRAL_POLICY_CST
                                ).replace(" ", "T")[:16]
                                source_cycle = str(
                                    MISSED_OPPORTUNITY_SIDE_NEUTRAL_ACTIVATION_CST
                                ).replace(" ", "T")[:16]
                                if (
                                    cycle_id < policy_cycle
                                    or not all(
                                        candidate.get("side") is None
                                        for candidate in candidates)
                                ):
                                    invalid_snapshot_rows.append(
                                        f"{label}:mixed_candidate_epoch:{cycle_id}")
                                    continue
                                if cycle_id < source_cycle:
                                    side_neutral_transition_cycle_ids.add(cycle_id)
                                    continue
                                if analysis_runs.get(cycle_id) != "ok":
                                    analysis_cycle_ids_missing.add(cycle_id)
                                    continue
                                reviewed = {
                                    candidate["symbol"]: candidate
                                    for candidate in candidates
                                }
                                selected: dict[str, dict] = {}
                                signal_error = False
                                for signal in analysis_signals.get(cycle_id, []):
                                    symbol = str(signal.get("symbol") or "")
                                    action = str(signal.get("action") or "")
                                    side = str(signal.get("side") or "").lower()
                                    expected_side = (
                                        "long" if action == "open_long" else
                                        "short" if action == "open_short" else "")
                                    if (
                                        symbol not in reviewed
                                        or side != expected_side
                                        or symbol in selected
                                    ):
                                        invalid_snapshot_rows.append(
                                            f"{label}:analysis_signal_mismatch:"
                                            f"{cycle_id}:{symbol or '?'}")
                                        signal_error = True
                                        break
                                    selected[symbol] = {
                                        **reviewed[symbol],
                                        "side": side,
                                        "source_tag": (
                                            MISSED_OPPORTUNITY_SIDE_NEUTRAL_SOURCE_TAG),
                                    }
                                if signal_error:
                                    continue
                                candidates = [
                                    selected[candidate["symbol"]]
                                    for candidate in candidates
                                    if candidate["symbol"] in selected
                                ]
                            canonical_snapshot = {
                                "cycle_id": cycle_id,
                                "tick_ts": obj.get("tick_ts"),
                                "candidates": candidates,
                            }
                            snapshots[cycle_id].append(canonical_snapshot)
                            snapshot_hash_input.append(canonical_snapshot)
                current_date += timedelta(days=1)

            missing_cycle_ids = [
                cycle for cycle in expected_cycles if not snapshots.get(cycle)]
            duplicate_cycle_ids = [
                cycle for cycle in expected_cycles
                if len(snapshots.get(cycle) or []) > 1
            ]
            covered_cycles = [
                cycle for cycle in expected_cycles if snapshots.get(cycle)]
            max_seen_cycle = max(covered_cycles) if covered_cycles else None
            contiguous_watermark = None
            for cycle in expected_cycles:
                if len(snapshots.get(cycle) or []) != 1:
                    break
                contiguous_watermark = cycle
            contiguous_watermark_end = None
            if contiguous_watermark is not None:
                contiguous_watermark_end = (
                    datetime.strptime(
                        contiguous_watermark, "%Y-%m-%dT%H:%M").replace(
                            tzinfo=CST) + timedelta(minutes=15)
                ).strftime(TS_FMT)
            pre_source_cycles = [
                cycle for cycle in expected_cycles
                if _missed_cycle_utc(cycle).astimezone(CST) < source_activation
            ]

            result_columns = {
                str(row[1]) for row in lessons_con.execute(
                    "PRAGMA table_info(missed_opportunities)")
            }
            reviewed_expr = (
                "reviewed_utc" if "reviewed_utc" in result_columns
                else "NULL AS reviewed_utc")

            all_first_seen: list[dict] = []
            all_trade_rows: list[dict] = []
            all_expected_keys: list[dict] = []
            all_outcome_inputs: list[dict] = []
            all_observed_results: list[dict] = []
            no_data_keys: list[str] = []
            missing_result_keys: list[str] = []
            extra_result_keys: list[str] = []
            duplicate_result_keys: list[str] = []
            mismatched_result_keys: list[str] = []
            producer_buckets: list[dict] = []

            for bucket_start, bucket_end in bucket_defs:
                start_cycle = _missed_cycle_id(bucket_start)
                end_cycle = _missed_cycle_id(bucket_end)
                bucket_expected = [
                    cycle for cycle in expected_cycles
                    if start_cycle <= cycle < end_cycle
                ]
                bucket_missing = [
                    cycle for cycle in bucket_expected
                    if not snapshots.get(cycle)
                ]
                bucket_duplicates = [
                    cycle for cycle in bucket_expected
                    if len(snapshots.get(cycle) or []) > 1
                ]
                first_seen: dict[tuple[str, str], dict] = {}
                for cycle in bucket_expected:
                    rows = snapshots.get(cycle) or []
                    if len(rows) != 1:
                        continue
                    for candidate in rows[0]["candidates"]:
                        key = (candidate["symbol"], candidate["side"])
                        if key not in first_seen:
                            first_seen[key] = {
                                "bucket_start_ts": bucket_start.strftime(TS_FMT),
                                "first_cycle_id": cycle,
                                **candidate,
                            }
                first_rows = sorted(
                    first_seen.values(),
                    key=lambda item: (
                        item["first_cycle_id"], item["ordinal"],
                        item["symbol"], item["side"]),
                )
                all_first_seen.extend(first_rows)

                trade_rows = [
                    {"cycle_id": str(row["cycle_id"]),
                     "symbol": str(row["symbol"])}
                    for row in trades_con.execute(
                        "SELECT cycle_id,symbol FROM trades "
                        "WHERE cycle_id>=? AND cycle_id<? "
                        "ORDER BY cycle_id,symbol",
                        (start_cycle, end_cycle),
                    )
                ]
                invalid_trade_rows = [
                    row for row in trade_rows
                    if (not _MISSED_CYCLE_RE.fullmatch(row["cycle_id"])
                        or not row["symbol"])
                ]
                if invalid_trade_rows:
                    mismatched_result_keys.append(
                        f"{start_cycle}:invalid_trade_rows={len(invalid_trade_rows)}")
                all_trade_rows.extend({
                    "bucket_start_ts": bucket_start.strftime(TS_FMT), **row
                } for row in trade_rows)
                traded_symbols = {row["symbol"] for row in trade_rows}
                eligible = [
                    item for item in first_rows
                    if item["symbol"] not in traded_symbols
                ]

                expected_outcomes: dict[tuple[str, str, str], dict] = {}
                bucket_no_data: list[str] = []
                for item in eligible:
                    cycle_id = item["first_cycle_id"]
                    start_utc = _missed_cycle_utc(cycle_id)
                    end_utc = start_utc + timedelta(
                        hours=MISSED_OPPORTUNITY_OUTCOME_HOURS)
                    bars = list(market_con.execute(
                        "SELECT ts,o,h,l,c FROM kline_cache "
                        "WHERE symbol=? AND tf='15m' AND ts>=? AND ts<? "
                        "ORDER BY ts",
                        (item["symbol"], _missed_utc_text(start_utc),
                         _missed_utc_text(end_utc)),
                    ))
                    canonical_bars: list[dict] = []
                    bars_valid = len(bars) == MISSED_OPPORTUNITY_REQUIRED_15M_BARS
                    expected_bar_times = [
                        _missed_utc_text(start_utc + timedelta(minutes=15 * i))
                        for i in range(MISSED_OPPORTUNITY_REQUIRED_15M_BARS)
                    ]
                    for index, row in enumerate(bars):
                        values: list[float] = []
                        try:
                            values = [float(row[name]) for name in ("o", "h", "l", "c")]
                        except (TypeError, ValueError):
                            bars_valid = False
                        if values:
                            if not all(math.isfinite(value) and value > 0
                                       for value in values):
                                bars_valid = False
                            open_px, high_px, low_px, close_px = values
                            if (high_px < max(open_px, low_px, close_px)
                                    or low_px > min(open_px, high_px, close_px)):
                                bars_valid = False
                            canonical_bars.append({
                                "ts": str(row["ts"]),
                                "o": open_px,
                                "h": high_px,
                                "l": low_px,
                                "c": close_px,
                            })
                        if (index >= len(expected_bar_times)
                                or str(row["ts"]) != expected_bar_times[index]):
                            bars_valid = False
                    key_text = "|".join((
                        bucket_start.strftime(TS_FMT), cycle_id,
                        item["symbol"], item["side"],
                    ))
                    all_outcome_inputs.append({
                        "key": key_text,
                        "bars": canonical_bars,
                        "valid": bars_valid,
                    })
                    if not bars_valid:
                        bucket_no_data.append(key_text)
                        no_data_keys.append(key_text)
                        continue
                    first_open = canonical_bars[0]["o"]
                    last_close = canonical_bars[-1]["c"]
                    highest = max(row["h"] for row in canonical_bars)
                    lowest = min(row["l"] for row in canonical_bars)
                    if item["side"] == "short":
                        actual = (first_open - last_close) / first_open * 100.0
                        hit = int(
                            (first_open - lowest) / first_open * 100.0
                            >= MISSED_OPPORTUNITY_FIXED_1R_PCT)
                    else:
                        actual = (last_close - first_open) / first_open * 100.0
                        hit = int(
                            (highest - first_open) / first_open * 100.0
                            >= MISSED_OPPORTUNITY_FIXED_1R_PCT)
                    result_key = (
                        _missed_cycle_ts(cycle_id),
                        item["symbol"], item["side"],
                    )
                    expected = {
                        "bucket_start_ts": bucket_start.strftime(TS_FMT),
                        "first_cycle_id": cycle_id,
                        "ts": result_key[0],
                        "symbol": item["symbol"],
                        "direction_hint": item["side"],
                        "actual_4h_pct": round(actual, 3),
                        "would_hit_1r_fixed2pct": hit,
                    }
                    expected_outcomes[result_key] = expected
                    all_expected_keys.append(expected)

                bucket_start_text = bucket_start.strftime(TS_FMT)
                bucket_end_text = bucket_end.strftime(TS_FMT)
                observed_rows = [dict(row) for row in lessons_con.execute(
                    "SELECT ts,symbol,direction_hint,actual_4h_pct,"
                    "would_hit_1r_fixed2pct,notes," + reviewed_expr + " "
                    "FROM missed_opportunities "
                    "WHERE datetime(ts)>=datetime(?) AND datetime(ts)<datetime(?) "
                    "AND (notes LIKE '%source=briefing_layer_v1%' "
                    "OR notes LIKE '%source=briefing_symbol_review_v2%') "
                    "ORDER BY ts,symbol,direction_hint",
                    (bucket_start_text, bucket_end_text),
                )]
                observed_map: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
                for row in observed_rows:
                    canonical_result = {
                        "bucket_start_ts": bucket_start_text,
                        "ts": str(row.get("ts") or ""),
                        "symbol": str(row.get("symbol") or ""),
                        "direction_hint": str(row.get("direction_hint") or ""),
                        "actual_4h_pct": row.get("actual_4h_pct"),
                        "would_hit_1r_fixed2pct": row.get(
                            "would_hit_1r_fixed2pct"),
                        "source_tag": (
                            MISSED_OPPORTUNITY_SIDE_NEUTRAL_SOURCE_TAG
                            if "source=briefing_symbol_review_v2" in str(
                                row.get("notes") or "")
                            else "briefing_layer_v1"),
                    }
                    all_observed_results.append(canonical_result)
                    observed_map[(
                        canonical_result["ts"], canonical_result["symbol"],
                        canonical_result["direction_hint"],
                    )].append(canonical_result)

                bucket_missing_results: list[str] = []
                bucket_duplicate_results: list[str] = []
                bucket_mismatches: list[str] = []
                for key, expected in expected_outcomes.items():
                    matches = observed_map.get(key) or []
                    key_text = "|".join(key)
                    if not matches:
                        bucket_missing_results.append(key_text)
                        missing_result_keys.append(key_text)
                        continue
                    if len(matches) != 1:
                        bucket_duplicate_results.append(key_text)
                        duplicate_result_keys.append(key_text)
                        continue
                    actual_row = matches[0]
                    try:
                        actual_pct = float(actual_row["actual_4h_pct"])
                        hit_value = int(actual_row["would_hit_1r_fixed2pct"])
                    except (TypeError, ValueError):
                        bucket_mismatches.append(key_text)
                        mismatched_result_keys.append(key_text)
                        continue
                    if (not math.isfinite(actual_pct)
                            or not math.isclose(
                                actual_pct, expected["actual_4h_pct"],
                                rel_tol=0.0, abs_tol=5e-4)
                            or hit_value != expected[
                                "would_hit_1r_fixed2pct"]):
                        bucket_mismatches.append(key_text)
                        mismatched_result_keys.append(key_text)

                expected_key_set = set(expected_outcomes)
                bucket_extra = [
                    "|".join(key) for key in observed_map
                    if key not in expected_key_set
                ]
                bucket_source_complete = (
                    not bucket_missing and not bucket_duplicates
                    and bucket_start >= source_activation)
                # When source cycles are missing, an otherwise "extra" result may
                # have originated in the unseen input and is therefore unresolved
                # SOURCE_LAG, not a proven result-contract error.
                if bucket_source_complete:
                    extra_result_keys.extend(bucket_extra)

                bucket_error = bool(
                    bucket_duplicate_results or bucket_mismatches
                    or bucket_missing_results
                    or (bucket_source_complete and bucket_extra)
                )
                if bucket_error:
                    bucket_status = "ERROR"
                elif not bucket_source_complete:
                    bucket_status = "SOURCE_LAG"
                elif bucket_no_data:
                    bucket_status = "NO_DATA"
                else:
                    bucket_status = "COMPLETE"
                producer_buckets.append({
                    "start_ts": bucket_start_text,
                    "end_ts": bucket_end_text,
                    "end_exclusive": True,
                    "status": bucket_status,
                    "expected_cycles": len(bucket_expected),
                    "covered_cycles": len(bucket_expected) - len(bucket_missing),
                    "missing_cycles": len(bucket_missing),
                    "duplicate_cycles": len(bucket_duplicates),
                    "first_seen_pairs": len(first_rows),
                    "trade_rows": len(trade_rows),
                    "traded_symbols": len(traded_symbols),
                    "eligible_pairs": len(eligible),
                    "evaluable_pairs": len(expected_outcomes),
                    "observed_results": len(observed_rows),
                    "no_data_pairs": len(bucket_no_data),
                    "missing_results": len(bucket_missing_results),
                    "extra_results": len(bucket_extra),
                    "duplicate_results": len(bucket_duplicate_results),
                    "mismatched_results": len(bucket_mismatches),
                    "count": (
                        len(expected_outcomes)
                        if bucket_status == "COMPLETE" else None),
                })

            fatal_errors = bool(
                invalid_snapshot_rows or duplicate_cycle_ids
                or duplicate_result_keys or mismatched_result_keys
                or missing_result_keys or extra_result_keys)
            source_lag = bool(
                missing_cycle_ids or analysis_cycle_ids_missing
                or side_neutral_transition_cycle_ids
                or pre_source_cycles or not contract_active)
            if fatal_errors:
                status = "ERROR"
            elif source_lag:
                status = "SOURCE_LAG"
            elif no_data_keys:
                status = "NO_DATA"
            else:
                status = "COMPLETE"

            producer_watermark_end = None
            for bucket in producer_buckets:
                if bucket["status"] != "COMPLETE":
                    break
                producer_watermark_end = bucket["end_ts"]

            reason_codes: list[str] = []
            if invalid_snapshot_rows:
                reason_codes.append("invalid_snapshot_rows")
            if duplicate_cycle_ids:
                reason_codes.append("duplicate_source_cycles")
            if missing_cycle_ids:
                reason_codes.append("source_cycles_missing")
            if analysis_cycle_ids_missing:
                reason_codes.append("analysis_cycles_missing")
            if side_neutral_transition_cycle_ids:
                reason_codes.append("side_neutral_transition_source_lag")
            if pre_source_cycles:
                reason_codes.append("window_precedes_source_activation")
            if not contract_active:
                reason_codes.append("contract_not_active")
            if no_data_keys:
                reason_codes.append("outcome_market_data_missing")
            if missing_result_keys:
                reason_codes.append("expected_results_missing")
            if extra_result_keys:
                reason_codes.append("unexpected_results_present")
            if duplicate_result_keys:
                reason_codes.append("duplicate_results_present")
            if mismatched_result_keys:
                reason_codes.append("result_values_mismatch")
            if not reason_codes and status == "COMPLETE":
                reason_codes.append("complete")

            max_result_ts = max(
                (str(row["ts"]) for row in all_observed_results),
                default=None,
            )
            snapshot_hash_payload = {
                "expected_cycles": expected_cycles,
                "records": sorted(
                    snapshot_hash_input,
                    key=lambda item: (
                        item["cycle_id"], _missed_sha256(item))),
                "missing_cycle_ids": missing_cycle_ids,
                "duplicate_cycle_ids": duplicate_cycle_ids,
                "invalid_snapshot_rows": invalid_snapshot_rows,
            }
            trade_hash_payload = sorted(
                all_trade_rows,
                key=lambda item: (
                    item["bucket_start_ts"], item["cycle_id"], item["symbol"]),
            )
            outcome_hash_payload = sorted(
                all_outcome_inputs, key=lambda item: item["key"])
            expected_hash_payload = sorted(
                all_expected_keys,
                key=lambda item: (
                    item["bucket_start_ts"], item["ts"], item["symbol"],
                    item["direction_hint"]),
            )
            observed_hash_payload = sorted(
                all_observed_results,
                key=lambda item: (
                    item["bucket_start_ts"], item["ts"], item["symbol"],
                    item["direction_hint"]),
            )
            receipt = {
                "schema_version": MISSED_OPPORTUNITY_EVIDENCE_SCHEMA_VERSION,
                "artifact_type": "missed_opportunity_evidence_contract",
                "contract_activation_cst": contract_activation.strftime(TS_FMT),
                "contract_active": contract_active,
                "status": status,
                "release_eligible": status == "COMPLETE",
                "count": (
                    len(all_expected_keys) if status == "COMPLETE" else None),
                "report_window": {
                    "start_ts": report_start.strftime(TS_FMT),
                    "end_ts": report_end.strftime(TS_FMT),
                    "end_exclusive": True,
                },
                "candidate_window": {
                    "start_ts": candidate_start.strftime(TS_FMT),
                    "end_ts": candidate_end.strftime(TS_FMT),
                    "end_exclusive": True,
                    "outcome_horizon_hours": (
                        MISSED_OPPORTUNITY_OUTCOME_HOURS),
                    "required_15m_bars": (
                        MISSED_OPPORTUNITY_REQUIRED_15M_BARS),
                },
                "source_coverage": {
                    "source_activation_cst": source_activation.strftime(TS_FMT),
                    "expected_cycles": len(expected_cycles),
                    "covered_cycles": len(covered_cycles),
                    "missing_cycles": len(missing_cycle_ids),
                    "duplicate_cycles": len(duplicate_cycle_ids),
                    "invalid_rows": len(invalid_snapshot_rows),
                    "pre_source_activation_cycles": len(pre_source_cycles),
                    "source_contiguous_watermark_cycle": contiguous_watermark,
                    "source_contiguous_watermark_end_ts": (
                        contiguous_watermark_end),
                    "max_seen_cycle": max_seen_cycle,
                    "analysis_direction_missing_cycles": len(
                        analysis_cycle_ids_missing),
                    "side_neutral_transition_cycles": len(
                        side_neutral_transition_cycle_ids),
                },
                "producer_watermark_end_ts": producer_watermark_end,
                "producer_buckets": producer_buckets,
                "first_seen": {"pair_count": len(all_first_seen)},
                "trade_exclusion": {
                    "row_count": len(all_trade_rows),
                    "symbol_count": len({
                        row["symbol"] for row in all_trade_rows}),
                    "semantics": contract["trade_exclusion"],
                },
                "outcome_coverage": {
                    "expected_result_count": len(all_expected_keys),
                    "observed_result_count": len(all_observed_results),
                    "no_data_count": len(no_data_keys),
                    "missing_result_count": len(missing_result_keys),
                    "extra_result_count": len(extra_result_keys),
                    "duplicate_result_count": len(duplicate_result_keys),
                    "mismatched_result_count": len(mismatched_result_keys),
                    # Diagnostic only.  It is never a completeness watermark.
                    "max_result_ts": max_result_ts,
                },
                "hashes": {
                    "contract_sha256": _missed_sha256(contract),
                    "schema_sha256": _missed_sha256(schema_contracts),
                    "snapshot_input_sha256": _missed_sha256(
                        snapshot_hash_payload),
                    "trade_exclusion_sha256": _missed_sha256(
                        trade_hash_payload),
                    "outcome_input_sha256": _missed_sha256(
                        outcome_hash_payload),
                    "expected_keys_sha256": _missed_sha256(
                        expected_hash_payload),
                    "observed_results_sha256": _missed_sha256(
                        observed_hash_payload),
                },
                "diagnostics": {
                    "reason_codes": reason_codes,
                    "errors": _missed_bounded([]),
                    "missing_cycle_ids": _missed_bounded(missing_cycle_ids),
                    "duplicate_cycle_ids": _missed_bounded(
                        duplicate_cycle_ids),
                    "invalid_snapshot_rows": _missed_bounded(
                        invalid_snapshot_rows),
                    "analysis_cycle_ids_missing": _missed_bounded(
                        sorted(analysis_cycle_ids_missing)),
                    "side_neutral_transition_cycle_ids": _missed_bounded(
                        sorted(side_neutral_transition_cycle_ids)),
                    "pre_source_activation_cycle_ids": _missed_bounded(
                        pre_source_cycles),
                    "no_data_keys": _missed_bounded(no_data_keys),
                    "missing_result_keys": _missed_bounded(
                        missing_result_keys),
                    "extra_result_keys": _missed_bounded(extra_result_keys),
                    "duplicate_result_keys": _missed_bounded(
                        duplicate_result_keys),
                    "mismatched_result_keys": _missed_bounded(
                        mismatched_result_keys),
                },
                "safety": {
                    "sqlite_mode": "ro",
                    "production_database_writes": 0,
                    "orders_placed": 0,
                },
            }
            return _seal_missed_opportunity_contract(receipt)
        finally:
            lessons_con.close()
            trades_con.close()
            market_con.close()
            if analysis_con is not None:
                analysis_con.close()
    except Exception as exc:  # noqa: BLE001 - contract returns ERROR fail-closed
        return _missed_error_receipt(
            report_start_ts=report_start_ts,
            report_end_ts=report_end_ts,
            contract_activation_cst=contract_activation_cst,
            error=exc,
        )


def _json_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _explicitly_rejected(row: dict | sqlite3.Row) -> bool:
    """Return True only for explicit non-fill/reject evidence."""
    top = dict(row)
    raw = _json_dict(top.get("raw"))
    for obj in (top, raw):
        status = str(obj.get("status") or "").strip().lower()
        if status in {"rejected", "reject", "failed", "error"}:
            return True
        if obj.get("ok") is False:
            return True
        if obj.get("success") is False:
            return True
        if str(obj.get("action_taken") or "").strip().upper() == "REJECT":
            return True
        if str(obj.get("reject_reason") or "").strip():
            return True
    return False


def _trade_label(item: dict) -> str:
    symbol = str(item["symbol"]).replace("-USDT-SWAP", "")
    side = str(item.get("side") or "-")
    pnl = float(item["pnl"])
    clock = str(item["ts"])[11:16]
    return f"{symbol} {side} {pnl:+.4f} ({clock} close)"


def filled_trade_stats(
    db_path: Path,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = False,
) -> dict:
    """Count confirmed fill rows within a CST interval.

    A row is a reportable fill only when:
    ``action`` is exactly ``open``/``close``, ``sz`` and ``fill_px`` are
    positive, and neither the row nor its raw receipt explicitly says reject.
    """
    start = fmt_ts(start_ts)
    end = fmt_ts(end_ts)
    op = "<" if end_exclusive else "<="
    con = _connect_ro(Path(db_path))
    try:
        rows = con.execute(
            "SELECT id,cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw "
            "FROM trades WHERE datetime(ts)>=datetime(?) "
            f"AND datetime(ts){op}datetime(?) "
            "ORDER BY datetime(ts),id",
            (start, end),
        ).fetchall()
    finally:
        con.close()

    fills: list[dict] = []
    rejected_rows: list[int] = []
    incomplete_rows: list[int] = []
    non_fill_rows: list[int] = []
    for raw_row in rows:
        row = dict(raw_row)
        action = str(row.get("action") or "").strip().lower()
        if action not in FILL_ACTIONS | REALIZING_ACTIONS:
            # 真正的非成交行（adjust_protection 等）仍只计数、不进任何口径。
            non_fill_rows.append(int(row["id"]))
            continue
        if _explicitly_rejected(row):
            rejected_rows.append(int(row["id"]))
            continue
        if not _positive(row.get("sz")) or not _positive(row.get("fill_px")):
            incomplete_rows.append(int(row["id"]))
            continue
        row["action"] = action
        fills.append(row)

    opens = [row for row in fills if row["action"] == "open"]
    closes = [row for row in fills if row["action"] == "close"]
    # reduce 走**独立列表**：不进 opens/closes，故 open_count/close_count 语义不变。
    reduces = [row for row in fills if row["action"] == "reduce"]
    closed_with_pnl = [
        row for row in closes if row.get("pnl") is not None
    ]
    reduced_with_pnl = [
        row for row in reduces if row.get("pnl") is not None
    ]
    # total_pnl 保留旧取值并另名透出为 close_realized_pnl —— 85 份历史日报的
    # raw.report_audit 与 validator 等式都绑在 realized_pnl 上，语义必须冻结。
    total_pnl = sum(float(row["pnl"]) for row in closed_with_pnl)
    reduce_pnl = sum(float(row["pnl"]) for row in reduced_with_pnl)
    # best/worst 刻意仍只看 close：标签文案写死 "(HH:MM close)"，且 validator
    # 的 markdown 正则按平仓解读；另给一组 realizing_* 供头条引用。
    best = max(closed_with_pnl, key=lambda row: float(row["pnl"]), default=None)
    worst = min(closed_with_pnl, key=lambda row: float(row["pnl"]), default=None)
    realizing_with_pnl = closed_with_pnl + reduced_with_pnl
    best_realizing = max(
        realizing_with_pnl, key=lambda row: float(row["pnl"]), default=None)
    worst_realizing = min(
        realizing_with_pnl, key=lambda row: float(row["pnl"]), default=None)

    # 2026-08-10 Wave0-2：平仓方向分解进入权威事实——周报文字段曾把 10空/3多
    # 手写成 11空/2多、把 USDT 均值标成百分比；方向计数与均值此后只认这里。
    close_side_breakdown = {}
    for side_key in ("long", "short"):
        side_rows = [
            row for row in closed_with_pnl
            if str(row.get("side") or "").strip().lower() == side_key
        ]
        side_pnl = sum(float(row["pnl"]) for row in side_rows)
        side_wins = sum(float(row["pnl"]) > 0 for row in side_rows)
        close_side_breakdown[side_key] = {
            "close_count": len(side_rows),
            "win_count": side_wins,
            "win_rate_pct": (
                side_wins / len(side_rows) * 100 if side_rows else None
            ),
            "pnl_sum_usdt": side_pnl,
            "pnl_avg_usdt": side_pnl / len(side_rows) if side_rows else None,
            "pnl_unit": "USDT",
        }

    return {
        "source": str(Path(db_path)),
        "period_start_ts": start,
        "period_end_ts": end,
        "period_end_exclusive": bool(end_exclusive),
        "open_count": len(opens),
        "close_count": len(closes),
        # realized_pnl 语义冻结＝仅 close，历史报告与 validator 依赖它保持可比。
        "realized_pnl": total_pnl,
        "close_realized_pnl": total_pnl,
        "reduce_count": len(reduces),
        "reduce_realized_pnl": reduce_pnl,
        # 头条唯一应引用的数：close + reduce 的已实现盈亏。
        "total_realized_pnl": total_pnl + reduce_pnl,
        "realizing_actions": sorted(REALIZING_ACTIONS),
        "best_realizing_trade": (
            _trade_label(best_realizing) if best_realizing else None),
        "worst_realizing_trade": (
            _trade_label(worst_realizing) if worst_realizing else None),
        "closed_with_pnl_count": len(closed_with_pnl),
        "win_count": sum(float(row["pnl"]) > 0 for row in closed_with_pnl),
        "win_rate_pct": (
            sum(float(row["pnl"]) > 0 for row in closed_with_pnl)
            / len(closed_with_pnl)
            * 100
            if closed_with_pnl
            else None
        ),
        "best_trade": _trade_label(best) if best else None,
        "worst_trade": _trade_label(worst) if worst else None,
        "close_side_breakdown": close_side_breakdown,
        "excluded_rejected_rows": len(rejected_rows),
        "excluded_rejected_row_ids": rejected_rows,
        "excluded_incomplete_rows": len(incomplete_rows),
        "excluded_incomplete_row_ids": incomplete_rows,
        "excluded_non_fill_rows": len(non_fill_rows),
        "fill_rows": [
            {
                "id": int(row["id"]),
                "cycle_id": row["cycle_id"],
                "ts": row["ts"],
                "symbol": row["symbol"],
                "action": row["action"],
                "side": row["side"],
                "sz": row["sz"],
                "fill_px": row["fill_px"],
                "pnl": row["pnl"],
            }
            for row in fills
        ],
    }


def realized_performance_stats(
    db_path: Path,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = True,
) -> dict:
    """Compute transparent monthly metrics from confirmed close-fill PnL.

    ``max_drawdown_usdt`` is the largest peak-to-trough loss on the cumulative
    realized-PnL curve, starting from zero. ``sharpe_approx`` is the annualized
    sample mean/stdev of 08:00-anchored daily realized PnL, including zero-PnL
    days and using ``sqrt(365)``.  It is ``None`` when variance is zero.
    These are reporting diagnostics, not account-equity or risk-gate inputs.
    """
    start = parse_cst(fmt_ts(start_ts))
    end = parse_cst(fmt_ts(end_ts))
    if end <= start:
        raise ValueError("performance interval must have end > start")
    seconds = (end - start).total_seconds()
    if seconds % 86400 != 0:
        raise ValueError("performance interval must tile whole 24h fact days")

    stats = filled_trade_stats(
        Path(db_path),
        start.strftime(TS_FMT),
        end.strftime(TS_FMT),
        end_exclusive=end_exclusive,
    )
    # P0-1：已实现盈亏曲线必须含 reduce，否则月报 max_drawdown/sharpe 与头条
    # total_pnl 不同源（8 月 reduce 合计 +82.5031）。
    closes = [
        row for row in stats["fill_rows"]
        if row["action"] in REALIZING_ACTIONS and row.get("pnl") is not None
    ]

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in closes:
        cumulative += float(row["pnl"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    day_count = int(seconds // 86400)
    daily_pnl = [0.0 for _ in range(day_count)]
    for row in closes:
        row_ts = parse_cst(str(row["ts"]))
        index = int((row_ts - start).total_seconds() // 86400)
        if 0 <= index < day_count:
            daily_pnl[index] += float(row["pnl"])

    sharpe = None
    if len(daily_pnl) >= 2:
        deviation = statistics.stdev(daily_pnl)
        if deviation > 1e-12:
            sharpe = statistics.mean(daily_pnl) / deviation * math.sqrt(365.0)

    return {
        "source": str(Path(db_path)),
        "period_start_ts": start.strftime(TS_FMT),
        "period_end_ts": end.strftime(TS_FMT),
        "period_end_exclusive": bool(end_exclusive),
        "realized_pnl": float(stats["realized_pnl"]),
        "close_count": int(stats["close_count"]),
        "closed_with_pnl_count": int(stats["closed_with_pnl_count"]),
        "max_drawdown_usdt": float(max_drawdown),
        "sharpe_approx": sharpe,
        "daily_anchor": "08:00 Asia/Shanghai",
        "daily_observations": day_count,
        "daily_realized_pnl": daily_pnl,
        "definitions": {
            "max_drawdown_usdt": (
                "peak-to-trough drawdown of cumulative confirmed close-fill "
                "realized PnL, starting at zero"
            ),
            "sharpe_approx": (
                "annualized mean/stdev of 08:00-anchored daily realized PnL; "
                "zero-PnL days included; sqrt(365); no risk-free adjustment"
            ),
        },
    }


def risk_rejected_open_attempts(
    ledger_path: Path,
    profile: str,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = False,
) -> dict:
    """Read risk-gate rejects, independently from filled trade rows."""
    start = fmt_ts(start_ts)
    end = fmt_ts(end_ts)
    op = "<" if end_exclusive else "<="
    con = _connect_ro(Path(ledger_path))
    try:
        rows = con.execute(
            "SELECT profile,cycle_id,symbol,action,side,state,reserved_at,"
            "updated_at,error FROM execution_intents "
            "WHERE profile=? AND action='open' AND state='failed_clean' "
            "AND error LIKE 'risk_reject:%' "
            "AND datetime(reserved_at)>=datetime(?) "
            f"AND datetime(reserved_at){op}datetime(?) "
            "ORDER BY datetime(reserved_at),symbol,side",
            (profile, start, end),
        ).fetchall()
    finally:
        con.close()

    items = []
    reasons: Counter[str] = Counter()
    for raw_row in rows:
        row = dict(raw_row)
        reason = str(row.get("error") or "").removeprefix("risk_reject:")
        reasons[reason or "unknown"] += 1
        items.append({
            "cycle_id": row["cycle_id"],
            "reserved_at": row["reserved_at"],
            "symbol": row["symbol"],
            "side": row["side"],
            "reason": reason or "unknown",
        })
    return {
        "source": str(Path(ledger_path)),
        "count": len(items),
        "reasons": dict(sorted(reasons.items())),
        "items": items,
    }


def open_position_avg_hold_hours(
    db_path: Path, as_of_ts: str
) -> float | None:
    """Approximate current ledger position age with FIFO lots.

    This is used only for the weekly turnover diagnostic.  It never replaces
    exchange/API position truth.
    """
    end = fmt_ts(as_of_ts)
    con = _connect_ro(Path(db_path))
    try:
        rows = con.execute(
            "SELECT id,ts,symbol,action,side,sz,fill_px,raw FROM trades "
            "WHERE datetime(ts)<=datetime(?) ORDER BY datetime(ts),id",
            (end,),
        ).fetchall()
    finally:
        con.close()

    lots: dict[tuple[str, str], list[list[Any]]] = defaultdict(list)
    for raw_row in rows:
        row = dict(raw_row)
        if _explicitly_rejected(row) or not _positive(row.get("sz")):
            continue
        action = str(row.get("action") or "").strip().lower()
        key = (str(row.get("symbol") or ""), str(row.get("side") or ""))
        qty = float(row["sz"])
        if action in POSITION_INCREASE_ACTIONS:
            if not _positive(row.get("fill_px")):
                continue
            lots[key].append([qty, parse_cst(str(row["ts"]))])
        elif action in POSITION_DECREASE_ACTIONS:
            remaining = qty
            while remaining > 1e-12 and lots[key]:
                take = min(remaining, float(lots[key][0][0]))
                lots[key][0][0] -= take
                remaining -= take
                if lots[key][0][0] <= 1e-12:
                    lots[key].pop(0)

    end_dt = parse_cst(end)
    weighted_hours = 0.0
    total_qty = 0.0
    for open_lots in lots.values():
        for qty, opened_at in open_lots:
            if qty <= 0:
                continue
            weighted_hours += qty * (end_dt - opened_at).total_seconds() / 3600
            total_qty += qty
    return weighted_hours / total_qty if total_qty > 0 else None


def closed_position_hold_stats(
    db_path: Path,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = True,
) -> dict:
    """Return FIFO holding time for confirmed close fills in the report window.

    Lots are reconstructed from all confirmed position-changing fills before
    ``end_ts`` so a position opened before the report window is still paired
    correctly.  Each fully matched ``close`` row contributes one observation;
    multiple FIFO lots consumed by that close are quantity-weighted first, then
    observations are averaged equally across close fills.  Any unmatched close
    makes the aggregate unknown instead of silently publishing a partial mean.
    """
    start = parse_cst(fmt_ts(start_ts))
    end = parse_cst(fmt_ts(end_ts))
    con = _connect_ro(Path(db_path))
    try:
        op = "<" if end_exclusive else "<="
        rows = con.execute(
            "SELECT id,ts,symbol,action,side,sz,fill_px,raw FROM trades "
            f"WHERE datetime(ts){op}datetime(?) ORDER BY datetime(ts),id",
            (end.strftime(TS_FMT),),
        ).fetchall()
    finally:
        con.close()

    lots: dict[tuple[str, str], list[list[Any]]] = defaultdict(list)
    samples: list[float] = []
    unmatched_close_row_ids: list[int] = []
    for raw_row in rows:
        row = dict(raw_row)
        if (_explicitly_rejected(row) or not _positive(row.get("sz"))
                or not _positive(row.get("fill_px"))):
            continue
        action = str(row.get("action") or "").strip().lower()
        if action not in POSITION_INCREASE_ACTIONS | POSITION_DECREASE_ACTIONS:
            continue
        row_ts = parse_cst(str(row["ts"]))
        key = (str(row.get("symbol") or ""), str(row.get("side") or ""))
        qty = float(row["sz"])
        if action in POSITION_INCREASE_ACTIONS:
            lots[key].append([qty, row_ts])
            continue

        remaining = qty
        matched = 0.0
        weighted_hours = 0.0
        while remaining > 1e-12 and lots[key]:
            lot_qty, opened_at = lots[key][0]
            take = min(remaining, float(lot_qty))
            weighted_hours += take * max(
                0.0, (row_ts - opened_at).total_seconds() / 3600.0)
            matched += take
            remaining -= take
            lots[key][0][0] -= take
            if lots[key][0][0] <= 1e-12:
                lots[key].pop(0)

        in_window = row_ts >= start and (
            row_ts < end if end_exclusive else row_ts <= end)
        if not in_window or action != "close":
            continue
        tolerance = max(1e-9, qty * 1e-9)
        if matched <= 0 or remaining > tolerance:
            unmatched_close_row_ids.append(int(row["id"]))
            continue
        samples.append(weighted_hours / matched)

    average = (
        sum(samples) / len(samples)
        if samples and not unmatched_close_row_ids else None
    )
    return {
        "closed_position_avg_hold_hours": average,
        "closed_position_hold_sample_count": len(samples),
        "closed_position_hold_unmatched_count": len(unmatched_close_row_ids),
        "closed_position_hold_unmatched_row_ids": unmatched_close_row_ids,
        "closed_position_hold_definition": (
            "FIFO quantity-weighted hours per confirmed close fill; "
            "arithmetic mean across fully matched close fills"
        ),
    }


def entry_quality_stats(account_db, start_ts: str, end_ts: str,
                        *, end_exclusive: bool = True) -> dict | None:
    """窗口内已平仓经验的 MAE/MFE 分档与出场通道分布（只读，零新增采集）。

    2026-08-19 F4：``trade_experiences`` 的 ``mae_r/mfe_r/exit_category``
    覆盖 132/143 却在日报、周报、简报三处都不出现。实测近 30 天：
    MAE<0.2R n=40 胜率 57.5% +169.32U；MAE 0.8-1.0R n=30 胜率 6.7% -157.72U；
    MFE<0.2R n=54 胜率 9.3% -170.68U；``reconcile_backfill`` 30/143（21%）
    根本不是 Agent 主动平的。判别力远超报告现有的任何切片。

    口径：按 ``COALESCE(closed_at, ts)`` 落在 [start, end) 的 closed 行；
    None/不可解析的路径值一律落 ``unknown``，绝不当 0。库缺表/缺列返回 None
    （unknown ≠ 0，报告侧据此显示 N/A 而不是「无」）。
    """
    from pathlib import Path as _Path
    path = _Path(account_db)
    if not path.exists():
        return None
    op = "<" if end_exclusive else "<="
    try:
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            cols = {str(r[1]) for r in con.execute(
                "PRAGMA table_info(trade_experiences)").fetchall()}
            if not cols:
                return None
            need = ("mae_r", "mfe_r", "exit_category", "realized_pnl")
            sel = ",".join(
                c if c in cols else f"NULL AS {c}" for c in need)
            clock = "COALESCE(closed_at,ts)" if "closed_at" in cols else "ts"
            rows = con.execute(
                f"SELECT {sel} FROM trade_experiences WHERE status='closed' "
                f"AND datetime({clock})>=datetime(?) "
                f"AND datetime({clock}){op}datetime(?)",
                (start_ts, end_ts)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not rows:
        return {"sample_n": 0, "mae_r_buckets": {}, "mfe_r_buckets": {},
                "exit_category": {}, "period_start_ts": start_ts,
                "period_end_ts": end_ts, "period_end_exclusive": end_exclusive}

    def bucket(value):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return "unknown"
        if num != num:
            return "unknown"
        for edge, label in ((0.2, "<0.2R"), (0.5, "0.2-0.5R"),
                            (0.8, "0.5-0.8R"), (1.0, "0.8-1.0R")):
            if num < edge:
                return label
        return ">=1.0R"

    def group(pairs):
        out = {}
        for label, pnl in pairs:
            slot = out.setdefault(
                label, {"n": 0, "wins": 0, "realized_pnl_sum_usdt": 0.0})
            slot["n"] += 1
            try:
                value = float(pnl)
            except (TypeError, ValueError):
                continue
            slot["realized_pnl_sum_usdt"] += value
            if value > 0:
                slot["wins"] += 1
        for slot in out.values():
            slot["win_rate_pct"] = round(slot["wins"] / slot["n"] * 100, 1)
            slot["realized_pnl_sum_usdt"] = round(
                slot["realized_pnl_sum_usdt"], 4)
        return dict(sorted(out.items()))

    return {
        "sample_n": len(rows),
        "method": "entry_quality_v1_frozen_path_metrics",
        "mae_r_buckets": group((bucket(r[0]), r[3]) for r in rows),
        "mfe_r_buckets": group((bucket(r[1]), r[3]) for r in rows),
        "exit_category": group(
            (str(r[2] or "unknown"), r[3]) for r in rows),
        "period_start_ts": start_ts,
        "period_end_ts": end_ts,
        "period_end_exclusive": end_exclusive,
    }


def profile_statistics(
    profile: str,
    trade_db: Path,
    ledger_db: Path,
    start_ts: str,
    end_ts: str,
    *,
    end_exclusive: bool = False,
    include_avg_hold: bool = False,
) -> dict:
    fills = filled_trade_stats(
        trade_db, start_ts, end_ts, end_exclusive=end_exclusive
    )
    rejects = risk_rejected_open_attempts(
        ledger_db, profile, start_ts, end_ts, end_exclusive=end_exclusive
    )
    result = {
        **fills,
        "risk_rejected_open_attempts": rejects,
    }
    if include_avg_hold:
        result.update(closed_position_hold_stats(
            trade_db,
            start_ts,
            end_ts,
            end_exclusive=end_exclusive,
        ))
        # Keep the current-open age as a separately named turnover diagnostic;
        # it must never populate weekly_reports.avg_hold_hours.
        result["open_position_avg_hold_hours"] = (
            open_position_avg_hold_hours(trade_db, end_ts)
        )
    return result


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only authoritative fill/reject metrics for reports"
    )
    parser.add_argument("--profile", choices=("live", "both"),
                        default="both")
    parser.add_argument("--as-of", default=now_cst())
    parser.add_argument("--window", choices=("daily", "rolling", "explicit"),
                        default="daily")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--start-ts")
    parser.add_argument("--end-ts")
    parser.add_argument("--end-exclusive", action="store_true")
    parser.add_argument("--include-avg-hold", action="store_true")
    parser.add_argument(
        "--include-rows", action="store_true",
        help="包含逐笔 fill 明细；默认仅输出紧凑汇总",
    )
    parser.add_argument("--db-root", default=_public_project_path('db'))
    args = parser.parse_args()

    as_of = fmt_ts(args.end_ts or args.as_of)
    if args.window == "daily":
        start, end = daily_window(as_of)
    elif args.window == "rolling":
        start, end = rolling_window(as_of, args.days)
    else:
        if not args.start_ts:
            parser.error("--window explicit requires --start-ts")
        start, end = fmt_ts(args.start_ts), as_of

    # Daily reviewer windows are adjacent half-open intervals.  The end must be
    # exclusive so an exact 08:05:00 fill cannot be counted in two reports.
    end_exclusive = bool(args.end_exclusive or args.window == "daily")
    root = Path(args.db_root)
    profiles = ("live",) if args.profile == "both" else (args.profile,)
    payload = {
        "as_of_ts": as_of,
        "period_start_ts": start,
        "period_end_ts": end,
        "period_end_exclusive": end_exclusive,
        "profiles": {},
    }
    for profile in profiles:
        stats = profile_statistics(
            profile,
            root / f"{profile}_trades.db",
            root / "ledger.db",
            start,
            end,
            end_exclusive=end_exclusive,
            include_avg_hold=args.include_avg_hold,
        )
        if not args.include_rows:
            stats.pop("fill_rows", None)
        payload["profiles"][profile] = stats
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
