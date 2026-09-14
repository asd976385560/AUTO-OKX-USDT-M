# -*- coding: utf-8 -*-
"""Forward-only frozen exit-quality closure tests (V2.1 section 7)."""
import hashlib
import importlib
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# The staging bundle intentionally contains only scoped files.  In the real
# production tree, import the real package so this test module cannot poison a
# later combined unittest run with an empty-path ``core`` stub.  Only the
# reduced staging bundle receives the two constant-only fallback modules.
if (
    (ROOT / "core" / "risk_validator.py").is_file()
    and (ROOT / "core" / "multitimeframe_gate.py").is_file()
):
    importlib.import_module("core.risk_validator")
    importlib.import_module("core.multitimeframe_gate")
elif "core.risk_validator" not in sys.modules:
    core = types.ModuleType("core")
    core.__path__ = []
    risk = types.ModuleType("core.risk_validator")
    risk.MAX_PORTFOLIO_IMR_RATIO = 0.666
    mtf = types.ModuleType("core.multitimeframe_gate")
    mtf.MINIMUM_BARS_FOR_FULL_INDICATORS = 34
    mtf.validate_kline_row = lambda *args, **kwargs: []
    sys.modules["core"] = core
    sys.modules["core.risk_validator"] = risk
    sys.modules["core.multitimeframe_gate"] = mtf

import daily_maintenance  # noqa: E402
import daily_report_writer  # noqa: E402
import decision_briefing  # noqa: E402
import exit_quality  # noqa: E402
import reviewer_preflight  # noqa: E402
import validate_daily_report  # noqa: E402


BUSINESS_DATE = "2026-08-16"
REPORT_START = "2026-08-15 08:00:00"
REPORT_END = "2026-08-16 08:00:00"
CANDIDATE_START = "2026-08-15 04:00:00"
CANDIDATE_END = "2026-08-16 04:00:00"


def _account_db(path: Path, rows: list[tuple]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE trade_experiences("
        "id INTEGER PRIMARY KEY, profile TEXT DEFAULT 'live', symbol TEXT, "
        "side TEXT, action TEXT DEFAULT 'open', status TEXT,"
        "closed_at TEXT, mfe_r REAL, mae_r REAL, realized_r_net REAL,"
        "ever_hit_1r INTEGER, close_at_1r INTEGER, exit_category TEXT,"
        "path_coverage TEXT, raw TEXT)"
    )
    connection.executemany(
        "INSERT INTO trade_experiences(symbol,side,status,closed_at,mfe_r,"
        "mae_r,realized_r_net,ever_hit_1r,close_at_1r,exit_category,"
        "path_coverage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.commit()
    connection.close()
    return path


def _live_db(path: Path, cycles: list[tuple], fills: list[tuple]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY, ts TEXT, mode TEXT,"
        " decision TEXT, n_orders INTEGER, equity REAL, note TEXT, raw TEXT)")
    connection.execute(
        "CREATE TABLE trades(id INTEGER PRIMARY KEY, cycle_id TEXT, ts TEXT,"
        " symbol TEXT, action TEXT, side TEXT, sz REAL, fill_px REAL, raw TEXT)")
    connection.executemany(
        "INSERT INTO trade_cycles(cycle_id,ts,mode,raw) VALUES(?,?,?,?)", cycles)
    connection.executemany(
        "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz) "
        "VALUES(?,?,?,?,?,?)", fills)
    connection.commit()
    connection.close()
    return path


def _position_raw(
    symbol: str,
    upl: object = None,
    flag: object = None,
    text: str = "",
    *,
    contracts: object = 1.0,
    include_upl: bool = True,
    include_flag: bool = True,
    requested: list[dict] | None = None,
    results: list[dict] | None = None,
    failures: list[dict] | None = None,
) -> str:
    position = {"instId": symbol, "contracts": contracts}
    if include_upl:
        position["upl_ratio_initial_margin"] = upl
    if include_flag:
        position["margin_return_review_at_or_above_50pct"] = flag
    return json.dumps({
        "live_facts": {"positions": [position]},
        "decision_card": {"agent_judgement": text},
        "requested_position_actions": requested or [],
        "position_action_results": results or [],
        "position_action_failures": failures or [],
    })


PEAK_ROWS = [
    ("AAA-USDT-SWAP", "short", "closed", "2026-08-15 10:00:00",
     1.62, 0.05, 0.04, 1, 0, "discretionary_manual", "full"),
    ("BBB-USDT-SWAP", "long", "closed", "2026-08-15 11:00:00",
     2.40, 0.10, 1.90, 1, 1, "discretionary_manual",
     "partial_boundary:1.00"),
    ("CCC-USDT-SWAP", "long", "closed", "2026-08-15 12:00:00",
     3.00, 0.10, 0.10, 1, 0, "discretionary_manual", "none"),
    ("DDD-USDT-SWAP", "long", "closed", "2026-08-16 04:00:00",
     9.00, 0.10, 0.10, 1, 0, "discretionary_manual", "full"),
]


def _margin_cycles() -> tuple[list[tuple], list[tuple]]:
    adjust_request = {"action": "ADJUST_PROTECTION", "symbol": "BBB-USDT-SWAP"}
    failed_request = {"action": "CLOSE", "symbol": "CCC-USDT-SWAP"}
    cycles = [
        # Pre-activation: excluded instead of retroactively counted unknown.
        ("2026-08-15T14:30", "2099-01-01 00:00:00", "live",
         _position_raw("OLD-USDT-SWAP", include_upl=False, include_flag=False)),
        # Post-activation missing fields: explicit unknown, outside denominator.
        ("2026-08-15T14:45", "1999-01-01 00:00:00", "live",
         _position_raw("UNK-USDT-SWAP", include_upl=False, include_flag=False)),
        ("2026-08-15T15:00", "1999-01-01 00:00:00", "live",
         _position_raw(
             "AAA-USDT-SWAP", 1.1, True, "AAA-USDT-SWAP REDUCE")),
        ("2026-08-15T15:15", "1999-01-01 00:00:00", "live",
         _position_raw(
             "BBB-USDT-SWAP", 0.9, True,
             "BBB-USDT-SWAP ADJUST_PROTECTION",
             requested=[adjust_request],
             results=[{
                 "request": adjust_request,
                 "result": {"status": "ok", "action_taken": "ADJUST_PROTECTION"},
             }],
         )),
        ("2026-08-15T15:30", "1999-01-01 00:00:00", "live",
         _position_raw(
             "CCC-USDT-SWAP", 0.8, True,
             "CCC-USDT-SWAP CLOSE attempted",
             requested=[failed_request],
             failures=[{"request": failed_request, "problem": "exchange error"}],
         )),
        ("2026-08-15T15:45", "1999-01-01 00:00:00", "live",
         _position_raw("DDD-USDT-SWAP", 0.7, True, "No action this cycle")),
        ("2026-08-15T16:00", "1999-01-01 00:00:00", "live",
         _position_raw(
             "EEE-USDT-SWAP", 0.6, True, "EEE-USDT-SWAP CLOSE")),
        ("2026-08-15T16:15", "1999-01-01 00:00:00", "live",
         _position_raw(
             "FFF-USDT-SWAP", 0.1, False, "FFF-USDT-SWAP HOLD")),
        ("2026-08-15T16:30", "1999-01-01 00:00:00", "live",
         _position_raw(
             "GGG-USDT-SWAP", None, False, "GGG-USDT-SWAP HOLD")),
    ]
    fills = [
        # Deliberately impossible ts values prove the join uses selected cycle_id.
        ("2026-08-15T15:00", "2099-01-01 00:00:00",
         "AAA-USDT-SWAP", "reduce", "long", 5.0),
        ("2026-08-15T16:00", "2099-01-01 00:00:00",
         "EEE-USDT-SWAP", "close", "long", 5.0),
    ]
    return cycles, fills


def _market_db(path: Path, bars: list[tuple] | None = None) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE kline_cache(symbol TEXT,tf TEXT,ts TEXT,o REAL,h REAL,"
        "l REAL,c REAL)")
    connection.executemany(
        "INSERT INTO kline_cache(symbol,tf,ts,o,h,l,c) VALUES(?,?,?,?,?,?,?)",
        bars or [],
    )
    connection.commit()
    connection.close()
    return path


def _counterfactual_bars(
    symbol: str = "AAA-USDT-SWAP",
    *,
    non_finite_index: int | None = None,
) -> list[tuple]:
    bars = []
    for index in range(16):
        minute = index * 15
        hour, minute = 2 + minute // 60, minute % 60
        high = float("inf") if index == non_finite_index else 99.5
        bars.append((
            symbol, "15m", f"2026-08-16T{hour:02d}:{minute:02d}:00Z",
            99.0, high, 97.5 if index == 4 else 98.5, 99.0,
        ))
    return bars


def _counterfactual_sources(
    root: Path,
    *,
    target: float = 98.0,
    event_fill_px: float = 99.0,
    event_ord_id: str | None = "close-1",
    close_events: list[dict] | None = None,
    live_rows: list[tuple] | None = None,
    bars: list[tuple] | None = None,
) -> tuple[Path, Path, Path]:
    symbol = "AAA-USDT-SWAP"
    closed_at = "2026-08-16 10:00:00"
    account = _account_db(root / "account.db", [])
    if close_events is None:
        event = {
            "cycle_id": "2026-08-16T10:00",
            "ts": closed_at,
            "sz": 1.0,
            "fill_px": event_fill_px,
        }
        if event_ord_id is not None:
            event["ordId"] = event_ord_id
        close_events = [event]
    raw = {
        "symbol": symbol,
        "side": "short",
        "decision_card": {"risk_reward": {
            "target": target,
            "exit_mode": "fixed_tp",
        }},
        "close_events": close_events,
    }
    con = sqlite3.connect(account)
    con.execute(
        "INSERT INTO trade_experiences("
        "profile,symbol,side,action,status,closed_at,mfe_r,mae_r,"
        "realized_r_net,ever_hit_1r,close_at_1r,exit_category,"
        "path_coverage,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("live", symbol, "short", "open", "closed", closed_at,
         1.62, 0.05, 0.04, 1, 0, "discretionary_manual", "full",
         json.dumps(raw)),
    )
    con.commit()
    con.close()

    live = _live_db(root / "live_trades.db", [], [])
    if live_rows is None:
        live_rows = [(
            "2026-08-16T10:00", closed_at, symbol, "close", "short",
            1.0, event_fill_px,
            json.dumps({"ordId": event_ord_id or "live-only-ord"}),
        )]
    con = sqlite3.connect(live)
    con.executemany(
        "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,fill_px,raw) "
        "VALUES(?,?,?,?,?,?,?,?)",
        live_rows,
    )
    con.commit()
    con.close()
    market = _market_db(
        root / "market.db",
        _counterfactual_bars(symbol) if bars is None else bars,
    )
    return account, live, market


def _source_dbs(root: Path) -> tuple[Path, Path, Path]:
    cycles, fills = _margin_cycles()
    return (
        _account_db(root / "account.db", PEAK_ROWS),
        _live_db(root / "live_trades.db", cycles, fills),
        _market_db(root / "market.db"),
    )


def _frozen(root: Path) -> tuple[dict, Path, Path]:
    account, live, market = _source_dbs(root)
    payload = exit_quality.compute(
        account_db=account,
        live_trades_db=live,
        market_db=market,
        report_start_ts=REPORT_START,
        report_end_ts=REPORT_END,
        generated_at="2026-08-16 08:00:00",
    )
    quality = root / "quality"
    artifact_path = quality / f"exit_quality_{BUSINESS_DATE}.json"
    raw, created = exit_quality.atomic_write_once_json(artifact_path, payload)
    assert created
    quality_payload = {"ts": "2026-08-16 07:56:00", "metrics": {}}
    quality_path = quality / f"quality_metrics_{BUSINESS_DATE}.json"
    quality_path.write_text(
        json.dumps(quality_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    quality_raw = quality_path.read_bytes()
    manifest = {
        "schema_version": 1,
        "business_date": BUSINESS_DATE,
        "run_id": "test-run",
        "maintenance_started_at": "2026-08-16 07:55:00",
        "critical_steps_completed_at": "2026-08-16 07:58:00",
        "state": "ready",
        "ready": True,
        "auto_send": False,
        "critical_steps": list(daily_maintenance.REVIEWER_CRITICAL_STEPS),
        "steps": {
            "reconcile": {"completed": True, "accepted": True, "rc": 0},
            "account_bills": {"completed": True, "accepted": True, "rc": 0},
            "missed_opportunities": {
                "completed": True, "accepted": True, "rc": 0},
            "ledger_invariants": {
                "completed": True, "accepted": True, "rc": 0},
            "exit_quality": {
                "completed": True,
                "accepted": True,
                "rc": 0,
                "artifact": {
                    "path": str(artifact_path),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                },
            },
            "quality_metrics": {
                "completed": True,
                "accepted": True,
                "rc": 0,
                "artifact": {
                    "path": str(quality_path),
                    "sha256": hashlib.sha256(quality_raw).hexdigest(),
                    "size_bytes": len(quality_raw),
                },
            },
        },
        "provisional_required": False,
        "report_mode": "final_candidate",
    }
    manifest_path = quality / f"reviewer_ready_{BUSINESS_DATE}.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload, artifact_path, manifest_path


class MinimalExecutionPackageExitPlanTests(unittest.TestCase):
    def test_flat_open_execution_package_is_original_plan_authority(self):
        raw = json.dumps({
            "symbol": "AAA-USDT-SWAP",
            "side": "long",
            "open_execution_package": {
                "contract": "open_execution_package_v1",
                "entry": 100.0,
                "stop": 97.0,
                "target": 106.0,
                "exit_mode": "fixed_tp",
            },
        })
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT '2026-09-03 00:00:00' AS ts,"
                "'AAA-USDT-SWAP' AS symbol,'long' AS side,? AS raw",
                (raw,),
            ).fetchone()
            produced = exit_quality._original_plan(row)
            rebuilt = validate_daily_report._v_original_plan(row)
        finally:
            con.close()
        self.assertEqual(({"exit_mode": "fixed_tp", "target_px": 106.0},
                          "eligible", []), produced)
        self.assertEqual(produced, rebuilt)

    def test_malformed_flat_package_does_not_fall_back_to_legacy_card(self):
        raw = json.dumps({
            "symbol": "AAA-USDT-SWAP",
            "side": "long",
            "open_execution_package": {
                "contract": "open_execution_package_v1",
                "entry": 100.0,
                "stop": 97.0,
                "target": 106.0,
                "exit_mode": "fixed_tp",
                "extra": "forbidden",
            },
            "decision_card": {"risk_reward": {
                "target": 999.0, "exit_mode": "fixed_tp"}},
        })
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT '2026-09-03 00:00:00' AS ts,"
                "'AAA-USDT-SWAP' AS symbol,'long' AS side,? AS raw",
                (raw,),
            ).fetchone()
            produced = exit_quality._original_plan(row)
            rebuilt = validate_daily_report._v_original_plan(row)
        finally:
            con.close()
        self.assertEqual("blocked", produced[1])
        self.assertEqual(
            ["original_open_execution_package_invalid"], produced[2])
        self.assertEqual(produced, rebuilt)

    def test_reconciled_close_uses_raw_close_ts_not_cycle_writer_ts(self):
        close_event = {
            "ordId": "CLOSE-1",
            "cycle_id": "2026-09-03T02:45",
            "ts": "2026-09-03 02:56:08",
            "sz": 101.0,
            "fill_px": 0.11811,
        }
        experience_raw = json.dumps({"close_events": [close_event]})
        trade_raw = json.dumps({
            "reconcile_source": "exchange_fills_reconcile",
            "close_ts": "2026-09-03 02:56:08",
            "ord_ids": ["CLOSE-1"],
            "fills": [{"ordId": "CLOSE-1"}],
            "ts_source": "trusted_internal_override",
        })
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            con.execute(
                "CREATE TABLE trades(id INTEGER,cycle_id TEXT,ts TEXT,"
                "symbol TEXT,action TEXT,side TEXT,sz REAL,fill_px REAL,raw TEXT)")
            con.execute(
                "INSERT INTO trades VALUES(1,?,?,?,?,?,?,?,?)",
                ("2026-09-03T02:45", "2026-09-03 02:53:15",
                 "USELESS-USDT-SWAP", "close", "short", 101.0,
                 0.11811, trade_raw))
            row = con.execute(
                "SELECT '2026-09-03 02:56:08' AS closed_at,"
                "'USELESS-USDT-SWAP' AS symbol,'short' AS side,? AS raw",
                (experience_raw,),
            ).fetchone()
            produced = exit_quality._authoritative_exit_fill(row, con)
            rebuilt = validate_daily_report._v_exit_fill(row, con)
        finally:
            con.close()
        self.assertEqual([], produced[1])
        self.assertEqual("2026-09-03 02:56:08", produced[0]["ts"])
        self.assertEqual(produced, rebuilt)


class CoverageAndGivebackTests(unittest.TestCase):
    def test_real_full_and_none_contract(self):
        self.assertEqual(1.0, exit_quality._coverage_ratio("full"))
        self.assertEqual(1.0, validate_daily_report._exit_coverage_ratio("full"))
        self.assertIsNone(exit_quality._coverage_ratio("none"))
        self.assertIsNone(validate_daily_report._exit_coverage_ratio("none"))

    def test_unknown_path_is_excluded_not_zero(self):
        rows = [
            (*PEAK_ROWS[index][:3], f"2026-08-16 {10 + index:02d}:00:00",
             *PEAK_ROWS[index][4:])
            for index in range(3)
        ]
        with tempfile.TemporaryDirectory() as temp:
            account = _account_db(Path(temp) / "account.db", rows)
            result = exit_quality.peak_giveback(
                account, "2026-08-16 08:00:00", "2026-08-17 08:00:00")
        self.assertEqual(3, result["closed_rows"])
        self.assertEqual(2, result["measured_rows"])
        self.assertEqual(1, result["unknown_path_rows"])
        self.assertEqual(1, result["profit_giveback_case_count"])
        self.assertEqual("AAA-USDT-SWAP", result["profit_giveback_cases"][0]["symbol"])

    def test_peak_giveback_is_live_open_only_and_exclusions_are_visible(self):
        live_row = (
            *PEAK_ROWS[0][:3], "2026-08-16 10:00:00", *PEAK_ROWS[0][4:])
        with tempfile.TemporaryDirectory() as temp:
            account = _account_db(Path(temp) / "account.db", [live_row])
            con = sqlite3.connect(account)
            con.executemany(
                "INSERT INTO trade_experiences("
                "profile,symbol,side,action,status,closed_at,mfe_r,"
                "realized_r_net,path_coverage) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("demo", "DEMO-USDT-SWAP", "short", "open", "closed",
                     "2026-08-16 10:15:00", 9.0, -9.0, "full"),
                    ("live", "FALLBACK-USDT-SWAP", "short", "close", "closed",
                     "2026-08-16 10:30:00", 9.0, -9.0, "full"),
                ],
            )
            con.commit()
            con.close()
            result = exit_quality.peak_giveback(
                account, "2026-08-16 08:00:00", "2026-08-17 08:00:00")
            rebuilt = validate_daily_report._independent_peak_giveback(
                account, "2026-08-16 08:00:00", "2026-08-17 08:00:00")
        self.assertEqual(result, rebuilt)
        self.assertEqual(3, result["source_closed_rows"])
        self.assertEqual(1, result["excluded_non_live_rows"])
        self.assertEqual(1, result["excluded_non_open_rows"])
        self.assertEqual(1, result["closed_rows"])
        self.assertEqual(1, result["measured_rows"])

    def test_preactivation_exits_are_not_rejudged(self):
        with tempfile.TemporaryDirectory() as temp:
            account, live, market = _source_dbs(Path(temp))
            block = exit_quality.compute(
                account_db=account, live_trades_db=live,
                market_db=market,
                report_start_ts=REPORT_START, report_end_ts=REPORT_END)
        missed = block["missed_take_profit"]
        peak = block["peak_giveback"]
        # 2026-08-19 G1：净 R 口径起用 v2。此处断言跟随生产常量，避免每次
        # 口径升版都要手改字面量（消费侧一律接受 v1|v2，历史工件不重判）。
        self.assertEqual(
            exit_quality.PEAK_GIVEBACK_METHOD_VERSION, peak["method_version"])
        self.assertEqual("2026-08-16 08:00:00", peak["fact_activation_cst"])
        self.assertEqual("PENDING", peak["status"])
        self.assertEqual(3, peak["candidate_closed_rows"])
        self.assertEqual(3, peak["pre_activation_excluded_rows"])
        self.assertEqual(0, peak["closed_rows"])
        self.assertEqual("NOT_ACTIVATED_WINDOW", missed["status"])
        self.assertEqual("READY", missed["upstream_status"])
        self.assertEqual(16, missed["required_15m_bars"])
        self.assertEqual(0, missed["pool_size"])
        self.assertEqual(3, missed["pre_activation_excluded_exits"])
        self.assertEqual(0, missed["candidate_exits"])

    def test_complete_frozen_16_bar_evidence_forms_real_pool(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = (
                "AAA-USDT-SWAP", "short", "closed",
                "2026-08-16 10:00:00", 1.62, 0.05, 0.04, 1, 0,
                "discretionary_manual", "full",
            )
            account = _account_db(root / "account.db", [row])
            open_raw = {
                "symbol": "AAA-USDT-SWAP",
                "side": "short",
                "decision_card": {"risk_reward": {
                    "target": 98.0, "exit_mode": "fixed_tp"}},
                "close_events": [{
                    "ordId": "close-1", "cycle_id": "2026-08-16T10:00",
                    "ts": "2026-08-16 10:00:00", "sz": 1.0,
                    "fill_px": 99.0,
                }],
            }
            con = sqlite3.connect(account)
            con.execute("UPDATE trade_experiences SET raw=?", (
                json.dumps(open_raw),))
            con.commit()
            con.close()
            live = _live_db(root / "live_trades.db", [], [])
            con = sqlite3.connect(live)
            con.execute(
                "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,fill_px,raw) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("2026-08-16T10:00", "2026-08-16 10:00:00",
                 "AAA-USDT-SWAP", "close", "short", 1.0, 99.0,
                 json.dumps({"ordId": "close-1"})),
            )
            con.commit()
            con.close()
            bars = []
            for index in range(16):
                minute = index * 15
                hour, minute = 2 + minute // 60, minute % 60
                bars.append((
                    "AAA-USDT-SWAP", "15m",
                    f"2026-08-16T{hour:02d}:{minute:02d}:00Z",
                    99.0, 99.5, 97.5 if index == 4 else 98.5, 99.0,
                ))
            market = _market_db(root / "market.db", bars)
            produced = exit_quality.missed_take_profit(
                account, live, market,
                "2026-08-16 04:00:00", "2026-08-17 04:00:00")
            rebuilt = validate_daily_report._independent_missed_take_profit(
                account, live, market,
                "2026-08-16 04:00:00", "2026-08-17 04:00:00")
        self.assertEqual(produced, rebuilt)
        self.assertEqual("COMPLETE", produced["status"])
        self.assertEqual(1, produced["eligible_exits"])
        self.assertEqual(1, produced["pool_size"])
        item = produced["evaluated_items"][0]
        self.assertEqual(
            item["source_snapshot_sha256"],
            exit_quality._canonical_snapshot_sha256(item["source_snapshot"]),
        )

    def test_demo_and_close_fallback_are_explicitly_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            account, live, market = _counterfactual_sources(root)
            con = sqlite3.connect(account)
            original_raw = con.execute(
                "SELECT raw FROM trade_experiences WHERE id=1").fetchone()[0]
            con.executemany(
                "INSERT INTO trade_experiences("
                "profile,symbol,side,action,status,closed_at,raw) "
                "VALUES(?,?,?,?,?,?,?)",
                [
                    ("demo", "DEMO-USDT-SWAP", "short", "open", "closed",
                     "2026-08-16 10:15:00", original_raw),
                    ("live", "FALLBACK-USDT-SWAP", "short", "close", "closed",
                     "2026-08-16 10:30:00", json.dumps({
                         "unmatched_sz": 1.0,
                         "close_cycle_id": "2026-08-16T10:30",
                     })),
                ],
            )
            con.commit()
            con.close()
            produced = exit_quality.missed_take_profit(
                account, live, market,
                "2026-08-16 04:00:00", "2026-08-17 04:00:00")
            rebuilt = validate_daily_report._independent_missed_take_profit(
                account, live, market,
                "2026-08-16 04:00:00", "2026-08-17 04:00:00")
        self.assertEqual(produced, rebuilt)
        self.assertEqual("READY", produced["upstream_status"])
        self.assertEqual(1, produced["candidate_exits"])
        self.assertEqual(1, produced["eligible_exits"])
        self.assertEqual(
            1, produced["classification_counts"]["excluded_profile"])
        self.assertEqual(
            1, produced["classification_counts"]["excluded_fallback"])
        self.assertEqual([], produced["blocking_items"])

    def test_close_event_without_ord_id_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            account, live, market = _counterfactual_sources(
                Path(temp), event_ord_id=None)
            produced = exit_quality.missed_take_profit(
                account, live, market,
                "2026-08-16 04:00:00", "2026-08-17 04:00:00")
            rebuilt = validate_daily_report._independent_missed_take_profit(
                account, live, market,
                "2026-08-16 04:00:00", "2026-08-17 04:00:00")
        self.assertEqual(produced, rebuilt)
        self.assertEqual("BLOCKED", produced["upstream_status"])
        self.assertEqual(
            1,
            produced["unknown_reason_counts"][
                "final_close_event_ord_id_missing"],
        )
        self.assertEqual(1, produced["unknown_exits"])

    def test_same_second_partial_events_use_append_final_ord_id(self):
        closed_at = "2026-08-16 10:00:00"
        symbol = "AAA-USDT-SWAP"
        events = [
            {"ordId": "partial-1", "cycle_id": "2026-08-16T10:00",
             "ts": closed_at, "sz": 0.5, "fill_px": 100.0},
            {"ordId": "final-2", "cycle_id": "2026-08-16T10:00",
             "ts": closed_at, "sz": 0.5, "fill_px": 99.0},
        ]
        live_rows = [
            ("2026-08-16T10:00", closed_at, symbol, "reduce", "short",
             0.5, 100.0, json.dumps({"ordId": "partial-1"})),
            ("2026-08-16T10:00", closed_at, symbol, "close", "short",
             0.5, 99.0, json.dumps({"ordId": "final-2"})),
        ]
        with tempfile.TemporaryDirectory() as temp:
            account, live, market = _counterfactual_sources(
                Path(temp), close_events=events, live_rows=live_rows)
            produced = exit_quality.missed_take_profit(
                account, live, market,
                "2026-08-16 04:00:00", "2026-08-17 04:00:00")
            rebuilt = validate_daily_report._independent_missed_take_profit(
                account, live, market,
                "2026-08-16 04:00:00", "2026-08-17 04:00:00")
        self.assertEqual(produced, rebuilt)
        self.assertEqual("COMPLETE", produced["status"])
        self.assertEqual(1, produced["eligible_exits"])
        fill = produced["evaluated_items"][0]["source_snapshot"]["exit_fill"]
        self.assertEqual("final-2", fill["ord_id"])
        self.assertEqual(99.0, fill["px"])

    def test_non_finite_counterfactual_inputs_block_with_reason(self):
        cases = (
            ("target", {"target": float("nan")},
             "original_plan_fixed_tp_target_not_finite"),
            ("fill", {"event_fill_px": float("inf")},
             "final_close_event_fill_px_not_finite"),
            ("bar", {
                "bars": _counterfactual_bars(non_finite_index=3),
            }, "post_exit_bar_non_finite"),
        )
        for label, kwargs, reason in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                account, live, market = _counterfactual_sources(
                    Path(temp), **kwargs)
                produced = exit_quality.missed_take_profit(
                    account, live, market,
                    "2026-08-16 04:00:00", "2026-08-17 04:00:00")
                rebuilt = validate_daily_report._independent_missed_take_profit(
                    account, live, market,
                    "2026-08-16 04:00:00", "2026-08-17 04:00:00")
            self.assertEqual(produced, rebuilt)
            self.assertEqual("BLOCKED", produced["upstream_status"])
            self.assertEqual(1, produced["unknown_reason_counts"][reason])
            self.assertEqual(1, produced["unknown_exits"])


class MarginForwardBoundaryTests(unittest.TestCase):
    def test_structured_position_action_review_is_forward_only_and_exact_symbol(self):
        payload = {
            "decision_card": {"agent_judgement": ""},
            "requested_position_actions": [{
                "action": "ADJUST_PROTECTION",
                "symbol": "AAA-USDT-SWAP",
                "reasoning": "保护地板已突破，收紧到新的结构失效位",
            }],
            "position_action_results": [],
            "position_action_failures": [],
        }
        before = "2026-08-20T17:45"
        at = "2026-08-20T18:00"
        self.assertFalse(exit_quality._explicit_position_review(
            payload, "AAA-USDT-SWAP", cycle_id=before))
        self.assertFalse(validate_daily_report._exit_explicit_review(
            payload, "AAA-USDT-SWAP", cycle_id=before))
        self.assertTrue(exit_quality._explicit_position_review(
            payload, "AAA-USDT-SWAP", cycle_id=at))
        self.assertTrue(validate_daily_report._exit_explicit_review(
            payload, "AAA-USDT-SWAP", cycle_id=at))
        self.assertFalse(exit_quality._explicit_position_review(
            payload, "BBB-USDT-SWAP", cycle_id=at))
        self.assertFalse(validate_daily_report._exit_explicit_review(
            payload, "BBB-USDT-SWAP", cycle_id=at))

    def test_structured_action_without_reason_is_not_explicit_review(self):
        payload = {
            "decision_card": {"agent_judgement": ""},
            "requested_position_actions": [{
                "action": "CLOSE", "symbol": "AAA-USDT-SWAP"}],
        }
        cycle = "2026-08-20T18:00"
        self.assertFalse(exit_quality._explicit_position_review(
            payload, "AAA-USDT-SWAP", cycle_id=cycle))
        self.assertFalse(validate_daily_report._exit_explicit_review(
            payload, "AAA-USDT-SWAP", cycle_id=cycle))

    def test_cycle_id_window_unknown_denominator_and_action_layers(self):
        with tempfile.TemporaryDirectory() as temp:
            _, live, _ = _source_dbs(Path(temp))
            result = exit_quality.margin_return_review(
                live, REPORT_START, REPORT_END)
        self.assertEqual("2026-08-15T14:45", result["fact_activation_cycle"])
        self.assertEqual(1, result["pre_activation_excluded_cycle_rows"])
        self.assertEqual(8, result["total_position_cycles"])
        self.assertEqual(2, result["unknown_fact_position_cycles"])
        self.assertEqual(6, result["fact_observed_position_cycles"])
        self.assertEqual(5, result["flagged_position_cycles"])
        self.assertEqual(4, result["explicitly_reviewed"])
        self.assertEqual(0.8, result["explicit_review_rate"])
        self.assertEqual(5, result["legacy_semantics_flagged"])
        self.assertEqual(0, result["structured_semantics_active_flagged"])
        self.assertEqual({
            "hold": 1,
            "close": 1,
            "reduce": 1,
            "adjust": 1,
            "add": 0,
            "open": 0,
            "attempted_failed": 1,
            "requested_unconfirmed": 0,
        }, result["disposition_counts"])
        self.assertEqual(1, result["action_layer_counts"]["failed"]["close"])
        self.assertEqual(1, result["action_layer_counts"]["fills"]["reduce"])

    def test_margin_review_is_live_open_position_only_with_exclusions(self):
        cycles = [
            ("2026-08-15T15:00", "1999-01-01 00:00:00", "demo",
             _position_raw("DEMO-USDT-SWAP", 0.9, True, "DEMO HOLD")),
            ("2026-08-15T15:15", "1999-01-01 00:00:00", "live",
             _position_raw(
                 "CLOSED-USDT-SWAP", 0.9, True, "CLOSED HOLD",
                 contracts=0.0)),
            ("2026-08-15T15:30", "1999-01-01 00:00:00", "live",
             _position_raw("OPEN-USDT-SWAP", 0.9, True, "OPEN HOLD")),
        ]
        with tempfile.TemporaryDirectory() as temp:
            live = _live_db(Path(temp) / "live_trades.db", cycles, [])
            result = exit_quality.margin_return_review(
                live, REPORT_START, REPORT_END)
            rebuilt = validate_daily_report._independent_margin_review(
                live, REPORT_START, REPORT_END)
        self.assertEqual(result, rebuilt)
        self.assertEqual(3, result["source_candidate_cycle_rows"])
        self.assertEqual(1, result["excluded_non_live_cycle_rows"])
        self.assertEqual(1, result["excluded_non_open_position_rows"])
        self.assertEqual(1, result["total_position_cycles"])
        self.assertEqual("OPEN-USDT-SWAP", result["items"][0]["symbol"])

    def test_requested_only_is_unconfirmed_not_failed_or_hold(self):
        request = {"action": "CLOSE", "symbol": "AAA-USDT-SWAP"}
        cycles = [(
            "2026-08-15T15:00", "1999-01-01 00:00:00", "live",
            _position_raw(
                "AAA-USDT-SWAP", 0.9, True,
                "AAA-USDT-SWAP CLOSE requested; receipt is still pending",
                requested=[request],
            ),
        )]
        with tempfile.TemporaryDirectory() as temp:
            live = _live_db(Path(temp) / "live_trades.db", cycles, [])
            result = exit_quality.margin_return_review(
                live, REPORT_START, REPORT_END)
            rebuilt = validate_daily_report._independent_margin_review(
                live, REPORT_START, REPORT_END)
        self.assertEqual(result, rebuilt)
        self.assertEqual(1, result["disposition_counts"]["requested_unconfirmed"])
        self.assertEqual(0, result["disposition_counts"]["attempted_failed"])
        self.assertEqual(0, result["disposition_counts"]["hold"])
        self.assertEqual(1, result["action_layer_counts"]["requested"]["close"])
        self.assertEqual(0, result["action_layer_counts"]["failed"]["close"])
        self.assertEqual("requested_unconfirmed", result["items"][0]["disposition"])
        self.assertEqual([], result["items"][0]["action_layers"]["failed"])

    def test_multi_action_disposition_has_explicit_shared_priority(self):
        payload = {
            "requested_position_actions": [{
                "action": "ADJUST_PROTECTION", "symbol": "AAA-USDT-SWAP"}],
            "position_action_failures": [{
                "action": "ADJUST_PROTECTION", "symbol": "AAA-USDT-SWAP",
                "problem": "venue timeout"}],
        }
        fills = {"adjust_protection", "open", "add", "reduce", "close"}
        self.assertEqual(
            "close",
            exit_quality._cycle_disposition(payload, "AAA-USDT-SWAP", fills))
        self.assertEqual(
            "close",
            validate_daily_report._exit_disposition(
                payload, "AAA-USDT-SWAP", fills))
        producer_layers = exit_quality._action_layers(
            payload, "AAA-USDT-SWAP", fills)
        validator_layers = validate_daily_report._exit_action_layers(
            payload, "AAA-USDT-SWAP", fills)
        self.assertEqual(producer_layers, validator_layers)
        self.assertEqual(["adjust"], producer_layers["failed"])
        self.assertEqual(
            ["add", "adjust", "close", "open", "reduce"],
            producer_layers["fills"])

    def test_validator_rebuild_matches_every_deterministic_field(self):
        with tempfile.TemporaryDirectory() as temp:
            account, live, market = _source_dbs(Path(temp))
            produced = exit_quality.compute(
                account_db=account, live_trades_db=live,
                market_db=market,
                report_start_ts=REPORT_START, report_end_ts=REPORT_END,
                generated_at="2026-08-16 08:00:00")
            rebuilt = validate_daily_report._independent_exit_quality(
                account, live, market, REPORT_START, REPORT_END)
        actual = dict(produced)
        actual.pop("generated_at")
        self.assertEqual(rebuilt, actual)
        self.assertNotIn(
            "import exit_quality", inspect.getsource(validate_daily_report))

    def test_independent_validator_rejects_self_consistent_snapshot_tamper(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            account, live, market = _counterfactual_sources(root)
            produced = exit_quality.compute(
                account_db=account,
                live_trades_db=live,
                market_db=market,
                report_start_ts="2026-08-16 08:00:00",
                report_end_ts="2026-08-17 08:00:00",
                generated_at="2026-08-17 08:00:00",
            )
            rebuilt = validate_daily_report._independent_exit_quality(
                account, live, market,
                "2026-08-16 08:00:00", "2026-08-17 08:00:00")
            tampered = json.loads(json.dumps(produced))
            missed = tampered["missed_take_profit"]
            for collection in (missed["evaluated_items"], missed["items"]):
                item = collection[0]
                item["source_snapshot"]["bars_15m"][0]["c"] = 98.75
                item["source_snapshot_sha256"] = (
                    exit_quality._canonical_snapshot_sha256(
                        item["source_snapshot"]))

            artifact = root / "exit_quality_tampered.json"
            artifact.write_text(
                json.dumps(tampered, ensure_ascii=False) + "\n",
                encoding="utf-8")
            raw = artifact.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            manifest = root / "reviewer_ready_tampered.json"
            manifest.write_text(json.dumps({
                "business_date": "2026-08-17",
                "state": "ready",
                "ready": True,
                "steps": {"exit_quality": {
                    "accepted": True,
                    "artifact": {
                        "path": str(artifact),
                        "sha256": sha,
                        "size_bytes": len(raw),
                    },
                }},
            }), encoding="utf-8")
            embedded = {
                **tampered,
                "frozen_artifact": {
                    "path": str(artifact),
                    "sha256": sha,
                    "size_bytes": len(raw),
                    "ready_manifest": str(manifest),
                },
            }
            proof_errors, readback = (
                validate_daily_report._verify_frozen_exit_artifact(embedded))
        self.assertEqual([], proof_errors)
        self.assertEqual(tampered, readback)
        actual_without_generated = dict(readback)
        actual_without_generated.pop("generated_at")
        self.assertNotEqual(rebuilt, actual_without_generated)
        self.assertNotEqual(
            rebuilt["missed_take_profit"]["evaluated_items"][0][
                "source_snapshot_sha256"],
            readback["missed_take_profit"]["evaluated_items"][0][
                "source_snapshot_sha256"],
        )


class FrozenArtifactTests(unittest.TestCase):
    def test_historical_frozen_authority_is_date_bounded(self):
        now = datetime(2026, 8, 20, 23, 0, 0)
        self.assertTrue(
            validate_daily_report._historical_frozen_exit_is_authority(
                "2026-08-19 08:00:00", now=now))
        self.assertFalse(
            validate_daily_report._historical_frozen_exit_is_authority(
                "2026-08-20 08:00:00", now=now))
        self.assertFalse(
            validate_daily_report._historical_frozen_exit_is_authority(
                "2026-08-21 08:00:00", now=now))

    def test_frozen_action_layers_render_in_contract_order(self):
        alphabetic_json_order = {
            "add": 0,
            "adjust": 2,
            "close": 0,
            "open": 0,
            "reduce": 1,
        }
        self.assertEqual(
            validate_daily_report._exit_action_layer_text(
                "requested", alphabetic_json_order),
            "requested=close:0,reduce:1,adjust:2,add:0,open:0",
        )

    def test_window_cannot_freeze_before_right_open_boundary(self):
        class BeforeBoundary(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 16, 7, 55, 0, tzinfo=tz)

        class AtBoundary(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 16, 8, 0, 0, tzinfo=tz)

        with mock.patch.object(exit_quality, "datetime", BeforeBoundary):
            with self.assertRaisesRegex(RuntimeError, "window is still open"):
                exit_quality._wait_for_closed_report_window(REPORT_END, 0)
        with mock.patch.object(exit_quality, "datetime", AtBoundary):
            exit_quality._wait_for_closed_report_window(REPORT_END, 0)

    def test_atomic_write_once_never_replaces_existing_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "exit_quality_2026-08-16.json"
            first, created = exit_quality.atomic_write_once_json(path, {"value": 1})
            second, replaced = exit_quality.atomic_write_once_json(path, {"value": 2})
        self.assertTrue(created)
        self.assertFalse(replaced)
        self.assertEqual(first, second)
        self.assertIn(b'"value": 1', second)

    def test_atomic_write_once_concurrent_publish_keeps_first_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "exit_quality_2026-08-16.json"
            barrier = threading.Barrier(2)

            def publish(value: int) -> tuple[bytes, bool]:
                barrier.wait()
                return exit_quality.atomic_write_once_json(path, {"value": value})

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(publish, (1, 2)))
            frozen = path.read_bytes()

        self.assertEqual(1, sum(created for _, created in results))
        self.assertTrue(all(raw == frozen for raw, _ in results))
        self.assertIn(json.loads(frozen)["value"], (1, 2))

    def test_cli_duplicate_loads_frozen_bytes_without_requery(self):
        class AfterBoundary(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 16, 8, 5, 0, tzinfo=tz)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            account, live, market = _source_dbs(root)
            output = root / "quality" / "exit_quality_2026-08-16.json"
            args = [
                "--as-of", "2026-08-16 08:05:00",
                "--account-db", str(account),
                "--live-trades-db", str(live),
                "--market-db", str(market),
                "--out-file", str(output),
            ]
            with mock.patch.object(exit_quality, "datetime", AfterBoundary):
                self.assertEqual(0, exit_quality.main(args))
            original = output.read_bytes()
            account.unlink()
            live.unlink()
            market.unlink()
            with mock.patch.object(exit_quality, "datetime", AfterBoundary):
                self.assertEqual(0, exit_quality.main(args))
            self.assertEqual(original, output.read_bytes())

    def test_ready_preflight_and_writer_are_hash_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload, artifact_path, manifest_path = _frozen(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            checked = reviewer_preflight.validate_manifest(manifest, BUSINESS_DATE)
            self.assertTrue(checked["ok"], checked["errors"])
            with mock.patch.object(daily_report_writer, "REVIEWER_READY_DIR", root / "quality"):
                loaded = daily_report_writer._load_frozen_exit_quality(
                    REPORT_START, REPORT_END)
            self.assertEqual(payload, {
                key: value for key, value in loaded.items()
                if key != "frozen_artifact"})
            artifact_path.write_bytes(artifact_path.read_bytes() + b" ")
            with mock.patch.object(daily_report_writer, "REVIEWER_READY_DIR", root / "quality"):
                with self.assertRaises(ValueError):
                    daily_report_writer._load_frozen_exit_quality(
                        REPORT_START, REPORT_END)

    def test_consumers_reject_self_consistent_artifact_frozen_before_window_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload, artifact_path, manifest_path = _frozen(root)
            payload["generated_at"] = "2026-08-16 07:59:59"
            artifact_path.write_text(
                json.dumps(payload, ensure_ascii=False) + "\n",
                encoding="utf-8")
            raw = artifact_path.read_bytes()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["steps"]["exit_quality"]["artifact"].update({
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            })
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8")

            checked = reviewer_preflight.validate_manifest(
                manifest, BUSINESS_DATE)
            with mock.patch.object(
                    daily_report_writer, "REVIEWER_READY_DIR", root / "quality"):
                with self.assertRaisesRegex(
                        ValueError, "frozen artifact contract differs"):
                    daily_report_writer._load_frozen_exit_quality(
                        REPORT_START, REPORT_END)
            with mock.patch.object(
                    daily_maintenance, "QUALITY_REPORT_DIR", root / "quality"):
                maintenance = daily_maintenance._exit_quality_artifact(
                    BUSINESS_DATE)
            db_root = root / "db"
            db_root.mkdir()
            with mock.patch.dict(os.environ, {
                "OKX_QUALITY_REPORT_DIR": str(root / "quality"),
                "OKX_REVIEWER_READY_DIR": str(root / "quality"),
            }):
                history = decision_briefing._frozen_exit_quality_history(db_root)
        self.assertFalse(checked["ok"])
        self.assertTrue(any(
            "window closed before generation" in error
            for error in checked["errors"]))
        self.assertFalse(maintenance["valid"])
        self.assertEqual([], history)

    def test_active_manifest_requires_exit_but_history_does_not(self):
        report = {
            "business_date": BUSINESS_DATE,
            "run_id": "r",
            "started_at": "2026-08-16 07:55:00",
            "steps": {
                name: {"rc": 0, "accepted": True}
                for name in daily_maintenance.BASE_REVIEWER_CRITICAL_STEPS
            },
        }
        active = daily_maintenance.build_reviewer_manifest(report, "completed")
        self.assertFalse(active["ready"])
        self.assertIn("exit_quality", active["critical_steps"])
        report["business_date"] = "2026-08-15"
        historical = daily_maintenance.build_reviewer_manifest(report, "completed")
        self.assertTrue(historical["ready"])
        self.assertNotIn("exit_quality", historical["critical_steps"])

    def test_validator_verifies_file_and_ready_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload, artifact_path, manifest_path = _frozen(root)
            raw = artifact_path.read_bytes()
            embedded = {
                **payload,
                "frozen_artifact": {
                    "path": str(artifact_path),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                    "ready_manifest": str(manifest_path),
                },
            }
            errors, readback = validate_daily_report._verify_frozen_exit_artifact(
                embedded)
        self.assertEqual([], errors)
        self.assertEqual(payload, readback)


class ConsumerAndRenderingTests(unittest.TestCase):
    def test_briefing_reads_only_ready_frozen_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload, _, _ = _frozen(root)
            db_root = root / "db"
            db_root.mkdir()
            with mock.patch.dict(os.environ, {
                "OKX_QUALITY_REPORT_DIR": str(root / "quality"),
                "OKX_REVIEWER_READY_DIR": str(root / "quality"),
            }):
                history = decision_briefing._frozen_exit_quality_history(db_root)
            summary = decision_briefing._summarize_frozen_exit_quality(history)
        self.assertEqual([payload], history)
        self.assertEqual(1, summary["days"])
        self.assertEqual("PENDING", summary["peak_status"])
        self.assertEqual(
            "NOT_ACTIVATED_WINDOW", summary["missed_take_profit_status"])
        self.assertEqual(0, summary["missed_take_profit_pool_size"])
        self.assertEqual(0, summary["missed_take_profit_classified_count"])
        self.assertEqual(0, summary["missed_take_profit_unknown_pool_days"])
        self.assertNotIn("exit_quality.compute", inspect.getsource(decision_briefing))

    def test_rendering_exposes_unknown_and_effective_window(self):
        with tempfile.TemporaryDirectory() as temp:
            payload, artifact_path, _ = _frozen(Path(temp))
            # 用 fixture 构造器落盘的 sort_keys 冻结工件，不手抄互锁计数。
            frozen = json.loads(artifact_path.read_text(encoding="utf-8"))
            text = daily_report_writer._exit_quality_block(
                {"exit_quality": frozen})
        self.assertIn("错失止盈池: NOT_ACTIVATED_WINDOW", text)
        self.assertIn("峰值回吐: PENDING", text)
        self.assertIn(
            "候选open平仓 3 笔、激活前排除 3 笔", text)
        self.assertIn(
            "源关闭记录 3 笔、排除非live 0 笔、排除非open 0 笔", text)
        self.assertIn("pool=0（分类计数=0）", text)
        self.assertIn("排除非live=0、fallback=0", text)
        self.assertIn("源cycle 9、排除非live 0、排除非open仓位 0", text)
        self.assertIn("2026-08-15T14:45", text)
        self.assertIn("未知不进复核分母", text)
        for disposition in exit_quality.DISPOSITION_KEYS:
            self.assertIn(f"{disposition} ", text)
        layer_counts = payload["margin_return_review"]["action_layer_counts"]
        expected_layers = []
        for layer in ("requested", "succeeded", "fills", "failed"):
            expected_layers.append(
                f"{layer}=" + ",".join(
                    f"{action}:{layer_counts[layer][action]}"
                    for action in exit_quality.ACTION_LAYER_KEYS))
        self.assertIn("；".join(expected_layers), text)

    def test_historical_markdown_gate_does_not_rejudge(self):
        source = inspect.getsource(daily_report_writer.write_markdown)
        self.assertIn("if ts >= EXIT_QUALITY_ACTIVATION_TS else", source)


if __name__ == "__main__":
    unittest.main()
