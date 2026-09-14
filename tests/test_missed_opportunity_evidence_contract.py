# -*- coding: utf-8 -*-
"""Read-only missed-opportunity producer evidence contract regressions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import trade_report_stats  # noqa: E402


LESSONS_SCHEMA = """
CREATE TABLE missed_opportunities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  direction_hint TEXT,
  actual_4h_pct REAL,
  would_hit_1r_fixed2pct INTEGER,
  notes TEXT,
  reviewed_utc TEXT
);
"""
TRADES_SCHEMA = """
CREATE TABLE trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id TEXT,
  symbol TEXT
);
"""
MARKET_SCHEMA = """
CREATE TABLE kline_cache(
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  tf TEXT NOT NULL,
  o REAL,
  h REAL,
  l REAL,
  c REAL,
  PRIMARY KEY(ts,symbol,tf)
);
"""
CST = timezone(timedelta(hours=8))


def _make_dbs(root: Path) -> tuple[Path, Path, Path]:
    lessons = root / "lessons.db"
    trades = root / "live_trades.db"
    market = root / "market.db"
    for path, schema in (
        (lessons, LESSONS_SCHEMA),
        (trades, TRADES_SCHEMA),
        (market, MARKET_SCHEMA),
    ):
        with closing(sqlite3.connect(path)) as con:
            con.executescript(schema)
    return lessons, trades, market


def _make_analysis_db(root: Path) -> Path:
    path = root / "analysis.db"
    with closing(sqlite3.connect(path)) as con:
        con.executescript(
            "CREATE TABLE analysis_runs(cycle_id TEXT,status TEXT);"
            "CREATE TABLE analysis_signals("
            "cycle_id TEXT,symbol TEXT,action TEXT,side TEXT);"
        )
    return path


def _cycles(start: datetime, end: datetime) -> list[str]:
    out = []
    cursor = start
    while cursor < end:
        out.append(cursor.strftime("%Y-%m-%dT%H:%M"))
        cursor += timedelta(minutes=15)
    return out


def _write_snapshots(
    briefing_dir: Path,
    start: datetime,
    end: datetime,
    *,
    candidates: dict[str, list[dict]] | None = None,
    omitted: set[str] | None = None,
    duplicated: set[str] | None = None,
    bad_schema: set[str] | None = None,
) -> None:
    briefing_dir.mkdir(parents=True, exist_ok=True)
    by_date: dict[str, list[str]] = {}
    candidates = candidates or {}
    omitted = omitted or set()
    duplicated = duplicated or set()
    bad_schema = bad_schema or set()
    for cycle in _cycles(start, end):
        if cycle in omitted:
            continue
        local = datetime.strptime(cycle, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
        tick = local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "schema": (
                "broken_schema" if cycle in bad_schema
                else "briefing_candidates_v1"),
            "cycle_id": cycle,
            "tick_ts": tick,
            "written_at_cst": local.strftime("%Y-%m-%d %H:%M:%S"),
            "candidates": candidates.get(cycle, []),
        }
        line = json.dumps(payload, ensure_ascii=False)
        by_date.setdefault(cycle[:10].replace("-", ""), []).append(line)
        if cycle in duplicated:
            by_date[cycle[:10].replace("-", "")].append(line)
    for date, lines in by_date.items():
        (briefing_dir / f"candidates-{date}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")


def _insert_bars(
    market: Path,
    *,
    cycle: str,
    symbol: str,
    final_close: float = 101.0,
) -> None:
    local = datetime.strptime(cycle, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    start = local.astimezone(timezone.utc)
    rows = []
    for index in range(16):
        ts = (start + timedelta(minutes=15 * index)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        close = final_close if index == 15 else 100.0
        rows.append((ts, symbol, "15m", 100.0, 103.0, 99.0, close))
    with closing(sqlite3.connect(market)) as con:
        con.executemany(
            "INSERT INTO kline_cache(ts,symbol,tf,o,h,l,c) "
            "VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()


def _insert_result(
    lessons: Path,
    *,
    cycle: str,
    symbol: str,
    side: str,
    actual: float = 1.0,
    hit: int = 1,
    source_tag: str = "briefing_layer_v1",
) -> None:
    with closing(sqlite3.connect(lessons)) as con:
        con.execute(
            "INSERT INTO missed_opportunities("
            "ts,symbol,direction_hint,actual_4h_pct,"
            "would_hit_1r_fixed2pct,notes,reviewed_utc) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                cycle.replace("T", " ") + ":00",
                symbol,
                side,
                actual,
                hit,
                f"source={source_tag} test",
                "2026-08-31 08:00:00",
            ),
        )
        con.commit()


def _contract(
    root: Path,
    *,
    report_start: str,
    report_end: str,
) -> dict:
    return trade_report_stats.missed_opportunity_evidence_contract(
        report_start_ts=report_start,
        report_end_ts=report_end,
        lessons_db=root / "lessons.db",
        live_trades_db=root / "live_trades.db",
        market_db=root / "market.db",
        briefing_dir=root / "briefing",
        contract_activation_cst="2026-08-20 08:00:00",
    )


def _semantic_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MissedOpportunityEvidenceContractTests(unittest.TestCase):
    def test_complete_empty_candidate_window_releases_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_dbs(root)
            start = datetime(2026, 8, 20, 4, tzinfo=CST)
            _write_snapshots(root / "briefing", start, start + timedelta(days=1))

            result = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )

            self.assertEqual(result["status"], "COMPLETE")
            self.assertTrue(result["release_eligible"])
            self.assertEqual(result["count"], 0)
            self.assertEqual(result["source_coverage"]["covered_cycles"], 96)
            self.assertEqual(result["outcome_coverage"]["expected_result_count"], 0)
            self.assertEqual(len(result["self_sha256"]), 64)

    def test_side_neutral_snapshot_binds_analysis_side_and_missing_run_is_lag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lessons, _trades, market = _make_dbs(root)
            analysis = _make_analysis_db(root)
            start = datetime(2026, 9, 3, 4, tzinfo=CST)
            cycle = "2026-09-03T04:00"
            candidate = {
                "ordinal": 1,
                "layer": "all_market",
                "symbol": "AAA-USDT-SWAP",
                "side": None,
                "eligible_sides": ["long", "short"],
                "opportunity_state": "SIDE_NEUTRAL",
                "selected_for_review": True,
            }
            _write_snapshots(
                root / "briefing", start, start + timedelta(days=1),
                candidates={cycle: [candidate]},
            )
            with closing(sqlite3.connect(analysis)) as con:
                con.execute(
                    "INSERT INTO analysis_runs VALUES(?,?)", (cycle, "ok"))
                con.execute(
                    "INSERT INTO analysis_signals VALUES(?,?,?,?)",
                    (cycle, "AAA-USDT-SWAP", "open_short", "short"))
                con.commit()
            _insert_bars(market, cycle=cycle, symbol="AAA-USDT-SWAP",
                         final_close=99.0)
            _insert_result(
                lessons, cycle=cycle, symbol="AAA-USDT-SWAP", side="short",
                hit=0, source_tag="briefing_symbol_review_v2")

            complete = _contract(
                root,
                report_start="2026-09-03 08:00:00",
                report_end="2026-09-04 08:00:00",
            )
            self.assertEqual("COMPLETE", complete["status"], complete)
            self.assertEqual(1, complete["count"])
            self.assertEqual(0, complete["source_coverage"][
                "analysis_direction_missing_cycles"])

            with closing(sqlite3.connect(analysis)) as con:
                con.execute("DELETE FROM analysis_runs WHERE cycle_id=?", (cycle,))
                con.execute(
                    "DELETE FROM analysis_signals WHERE cycle_id=?", (cycle,))
                con.commit()
            lagged = _contract(
                root,
                report_start="2026-09-03 08:00:00",
                report_end="2026-09-04 08:00:00",
            )
            self.assertEqual("SOURCE_LAG", lagged["status"], lagged)
            self.assertEqual([], lagged["diagnostics"][
                "invalid_snapshot_rows"]["items"])
            self.assertIn(cycle, lagged["diagnostics"][
                "analysis_cycle_ids_missing"]["items"])

    def test_first_seen_resets_per_daily_0400_bucket_and_trades_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lessons, trades, market = _make_dbs(root)
            start = datetime(2026, 8, 20, 4, tzinfo=CST)
            second = start + timedelta(days=1)
            btc = {"symbol": "BTC-USDT-SWAP", "side": "long", "layer": "mature"}
            eth = {"symbol": "ETH-USDT-SWAP", "side": "short", "layer": "early"}
            candidates = {
                start.strftime("%Y-%m-%dT%H:%M"): [btc, eth],
                (start + timedelta(minutes=15)).strftime(
                    "%Y-%m-%dT%H:%M"): [btc],
                second.strftime("%Y-%m-%dT%H:%M"): [btc],
            }
            _write_snapshots(
                root / "briefing", start, start + timedelta(days=2),
                candidates=candidates,
            )
            with closing(sqlite3.connect(trades)) as con:
                con.execute(
                    "INSERT INTO trades(cycle_id,symbol) VALUES(?,?)",
                    ("2026-08-20T12:00", "ETH-USDT-SWAP"),
                )
                con.commit()
            for cycle in ("2026-08-20T04:00", "2026-08-21T04:00"):
                _insert_bars(market, cycle=cycle, symbol="BTC-USDT-SWAP")
                _insert_result(
                    lessons, cycle=cycle, symbol="BTC-USDT-SWAP", side="long")

            result = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-22 08:00:00",
            )

            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["first_seen"]["pair_count"], 3)
            self.assertEqual(result["trade_exclusion"]["symbol_count"], 1)
            self.assertEqual(
                [item["eligible_pairs"] for item in result["producer_buckets"]],
                [1, 1],
            )

    def test_missing_cycle_is_source_lag_with_null_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_dbs(root)
            start = datetime(2026, 8, 20, 4, tzinfo=CST)
            missing = "2026-08-20T10:15"
            _write_snapshots(
                root / "briefing", start, start + timedelta(days=1),
                omitted={missing},
            )

            result = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )

            self.assertEqual(result["status"], "SOURCE_LAG")
            self.assertFalse(result["release_eligible"])
            self.assertIsNone(result["count"])
            self.assertEqual(
                result["source_coverage"]["source_contiguous_watermark_cycle"],
                "2026-08-20T10:00",
            )
            self.assertEqual(
                result["diagnostics"]["missing_cycle_ids"]["items"],
                [missing],
            )

    def test_complete_source_with_missing_market_bars_is_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_dbs(root)
            start = datetime(2026, 8, 20, 4, tzinfo=CST)
            cycle = "2026-08-20T04:00"
            _write_snapshots(
                root / "briefing", start, start + timedelta(days=1),
                candidates={cycle: [{
                    "symbol": "BTC-USDT-SWAP", "side": "long",
                    "layer": "mature",
                }]},
            )

            result = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )

            self.assertEqual(result["status"], "NO_DATA")
            self.assertIsNone(result["count"])
            self.assertEqual(result["outcome_coverage"]["no_data_count"], 1)

    def test_duplicate_or_bad_schema_source_is_error(self) -> None:
        for mode in ("duplicate", "schema"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _make_dbs(root)
                start = datetime(2026, 8, 20, 4, tzinfo=CST)
                cycle = "2026-08-20T04:00"
                _write_snapshots(
                    root / "briefing", start, start + timedelta(days=1),
                    duplicated={cycle} if mode == "duplicate" else set(),
                    bad_schema={cycle} if mode == "schema" else set(),
                )

                result = _contract(
                    root,
                    report_start="2026-08-20 08:00:00",
                    report_end="2026-08-21 08:00:00",
                )

                self.assertEqual(result["status"], "ERROR")
                self.assertFalse(result["release_eligible"])
                self.assertIsNone(result["count"])

    def test_result_value_mismatch_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lessons, _trades, market = _make_dbs(root)
            start = datetime(2026, 8, 20, 4, tzinfo=CST)
            cycle = "2026-08-20T04:00"
            _write_snapshots(
                root / "briefing", start, start + timedelta(days=1),
                candidates={cycle: [{
                    "symbol": "BTC-USDT-SWAP", "side": "long",
                    "layer": "mature",
                }]},
            )
            _insert_bars(market, cycle=cycle, symbol="BTC-USDT-SWAP")
            _insert_result(
                lessons, cycle=cycle, symbol="BTC-USDT-SWAP", side="long",
                actual=99.0,
            )

            result = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )

            self.assertEqual(result["status"], "ERROR")
            self.assertEqual(
                result["outcome_coverage"]["mismatched_result_count"], 1)

    def test_bad_or_wrong_schema_rows_outside_boundary_window_do_not_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_dbs(root)
            start = datetime(2026, 8, 20, 4, tzinfo=CST)
            _write_snapshots(root / "briefing", start, start + timedelta(days=1))
            baseline = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )
            self.assertEqual(baseline["status"], "COMPLETE")

            early = root / "briefing" / "candidates-20260820.jsonl"
            late = root / "briefing" / "candidates-20260821.jsonl"
            outside_early = json.dumps({
                "schema": "wrong",
                "cycle_id": "2026-08-20T00:00",
                "candidates": "also wrong",
            })
            outside_late = json.dumps({
                "schema": "wrong",
                "cycle_id": "2026-08-21T04:00",
                "candidates": "also wrong",
            })
            early.write_text(
                outside_early + "\n{unlocatable bad json\n"
                + early.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with late.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(outside_late + "\n{later bad json\n")

            rebuilt = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )

            self.assertEqual(rebuilt["status"], "COMPLETE")
            self.assertEqual(rebuilt["hashes"], baseline["hashes"])
            self.assertEqual(rebuilt["self_sha256"], baseline["self_sha256"])

    def test_unrelated_schema_columns_do_not_drift_contract_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lessons, trades, market = _make_dbs(root)
            start = datetime(2026, 8, 20, 4, tzinfo=CST)
            _write_snapshots(root / "briefing", start, start + timedelta(days=1))
            baseline = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )
            self.assertEqual(baseline["status"], "COMPLETE")

            for path, statement in (
                (lessons, "ALTER TABLE missed_opportunities ADD COLUMN sim_extra TEXT"),
                (trades, "ALTER TABLE trades ADD COLUMN unrelated_audit_note TEXT"),
                (market, "ALTER TABLE kline_cache ADD COLUMN unrelated_volume REAL"),
            ):
                with closing(sqlite3.connect(path)) as con:
                    con.execute(statement)
                    con.commit()

            rebuilt = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )

            self.assertEqual(rebuilt["status"], "COMPLETE")
            self.assertEqual(rebuilt["hashes"], baseline["hashes"])
            self.assertEqual(rebuilt["self_sha256"], baseline["self_sha256"])

    def test_presource_window_is_source_lag_even_with_full_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_dbs(root)
            start = datetime(2026, 8, 18, 4, tzinfo=CST)
            _write_snapshots(root / "briefing", start, start + timedelta(days=1))

            result = trade_report_stats.missed_opportunity_evidence_contract(
                report_start_ts="2026-08-18 08:00:00",
                report_end_ts="2026-08-19 08:00:00",
                lessons_db=root / "lessons.db",
                live_trades_db=root / "live_trades.db",
                market_db=root / "market.db",
                briefing_dir=root / "briefing",
                contract_activation_cst="2026-08-19 08:00:00",
            )

            self.assertEqual(result["status"], "SOURCE_LAG")
            self.assertEqual(
                result["source_coverage"]["pre_source_activation_cycles"], 96)

    def test_receipt_is_deterministic_bounded_and_databases_remain_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lessons, trades, market = _make_dbs(root)
            start = datetime(2026, 8, 20, 4, tzinfo=CST)
            # One source row leaves 95 missing cycles and exercises bounded output.
            _write_snapshots(
                root / "briefing", start, start + timedelta(minutes=15))
            before = {
                path.name: _semantic_hash(path)
                for path in (lessons, trades, market)
            }

            first = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )
            second = _contract(
                root,
                report_start="2026-08-20 08:00:00",
                report_end="2026-08-21 08:00:00",
            )

            self.assertEqual(first, second)
            self.assertEqual(
                first["diagnostics"]["missing_cycle_ids"]["count"], 95)
            self.assertEqual(
                len(first["diagnostics"]["missing_cycle_ids"]["items"]), 50)
            self.assertTrue(
                first["diagnostics"]["missing_cycle_ids"]["truncated"])
            copy = dict(first)
            self_hash = copy.pop("self_sha256")
            canonical = json.dumps(
                copy, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), self_hash)
            self.assertEqual(before, {
                path.name: _semantic_hash(path)
                for path in (lessons, trades, market)
            })


if __name__ == "__main__":
    unittest.main()
