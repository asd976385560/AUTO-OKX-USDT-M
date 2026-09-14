"""Weekly/monthly fact-window and pre-send validation regressions."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import daily_report_writer  # noqa: E402
import trade_report_stats  # noqa: E402
import validate_daily_report  # noqa: E402
import validate_periodic_report  # noqa: E402


TRADE_SCHEMA = """
CREATE TABLE trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id TEXT,ts TEXT,symbol TEXT,action TEXT,side TEXT,sz REAL,
  fill_px REAL,pnl REAL,raw TEXT
);
"""

LEDGER_SCHEMA = """
CREATE TABLE execution_intents(
  profile TEXT,cycle_id TEXT,symbol TEXT,action TEXT,side TEXT,state TEXT,
  reserved_at TEXT,updated_at TEXT,error TEXT
);
"""

ACCOUNT_SCHEMA = """
CREATE TABLE weekly_reports(
  week_start_ts TEXT NOT NULL,profile TEXT NOT NULL,open_count INTEGER,
  close_count INTEGER,total_pnl REAL,win_rate REAL,avg_hold_hours REAL,
  margin_util_pct REAL,idle_ratio REAL,summary TEXT,lessons TEXT,raw TEXT,
  trade_week_num INTEGER,PRIMARY KEY(week_start_ts,profile)
);
CREATE TABLE monthly_reports(
  month_start_ts TEXT NOT NULL,profile TEXT NOT NULL,total_pnl REAL,
  max_drawdown REAL,sharpe_approx REAL,summary TEXT,lessons TEXT,raw TEXT,
  trade_month_num INTEGER,PRIMARY KEY(month_start_ts,profile)
);
"""


def _missed_evidence_contract(
    status: str = "COMPLETE", count: int | None = 3,
) -> dict:
    release = status == "COMPLETE"
    value = count if release else None
    digest = "a" * 64
    return {
        "schema_version": 1,
        "artifact_type": "missed_opportunity_evidence_contract",
        "contract_activation_cst": "2026-09-01 08:00:00",
        "contract_active": True,
        "status": status,
        "release_eligible": release,
        "count": value,
        "report_window": {
            "start_ts": "2026-08-25 08:00:00",
            "end_ts": "2026-09-01 08:00:00",
            "end_exclusive": True,
        },
        "candidate_window": {
            "start_ts": "2026-08-25 04:00:00",
            "end_ts": "2026-09-01 04:00:00",
            "end_exclusive": True,
            "outcome_horizon_hours": 4,
            "required_15m_bars": 16,
        },
        "source_coverage": {},
        "outcome_coverage": {},
        "hashes": {
            "contract_sha256": digest,
            "schema_sha256": digest,
            "snapshot_input_sha256": digest,
            "trade_exclusion_sha256": digest,
            "outcome_input_sha256": digest,
            "expected_keys_sha256": digest,
            "observed_results_sha256": digest,
        },
        "self_sha256": "b" * 64,
    }


def _missed_outer(contract: dict) -> dict:
    candidate = contract["candidate_window"]
    return {
        "candidate_window_start_ts": candidate["start_ts"],
        "candidate_window_end_ts": candidate["end_ts"],
        "candidate_window_end_exclusive": True,
        "outcome_horizon_hours": 4,
        "required_15m_bars": 16,
        "count": contract["count"],
        "evidence_contract": json.loads(json.dumps(contract)),
    }


def _make_dbs(root: Path) -> tuple[Path, Path, Path]:
    account = root / "account.db"
    trades = root / "live_trades.db"
    ledger = root / "ledger.db"
    with closing(sqlite3.connect(account)) as con:
        con.executescript(ACCOUNT_SCHEMA)
    with closing(sqlite3.connect(trades)) as con:
        con.executescript(TRADE_SCHEMA)
    with closing(sqlite3.connect(ledger)) as con:
        con.executescript(LEDGER_SCHEMA)
    return account, trades, ledger


def _trade(
    con: sqlite3.Connection,
    ts: str,
    action: str,
    pnl: float | None,
    *,
    symbol: str = "BTC-USDT-SWAP",
    side: str = "long",
    sz: float = 1.0,
) -> None:
    con.execute(
        "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (ts[:16], ts, symbol, action, side, sz, 100.0, pnl, "{}"),
    )


class PeriodicReportHardeningTests(unittest.TestCase):
    def _validate_evidence(self, contract: dict, metrics: dict | None = None):
        with (
            mock.patch.object(
                validate_periodic_report.thresholds,
                "missed_opportunity_evidence_contract_active",
                return_value=True,
            ),
            mock.patch.object(
                trade_report_stats,
                "missed_opportunity_evidence_contract",
                return_value=json.loads(json.dumps(contract)),
            ),
        ):
            return validate_periodic_report._validate_missed_opportunity_contract(
                period_start="2026-08-25 08:00:00",
                period_end="2026-09-01 08:00:00",
                embedded_metrics=metrics or _missed_outer(contract),
                lessons_db=Path("lessons.db"),
                live_trades_db=Path("live_trades.db"),
                market_db=Path("market.db"),
                briefing_dir=Path("logs/briefing"),
            )

    def test_complete_evidence_contract_is_release_eligible(self) -> None:
        result = self._validate_evidence(_missed_evidence_contract())
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["artifact_errors"], [])
        self.assertEqual(result["release_blockers"], [])
        self.assertIn(
            "missed_opportunity_evidence_contract", result["checks"])

    def test_noncomplete_evidence_is_valid_draft_but_not_releasable(self) -> None:
        for status in ("SOURCE_LAG", "NO_DATA", "ERROR"):
            with self.subTest(status=status):
                result = self._validate_evidence(
                    _missed_evidence_contract(status, None))
                self.assertEqual(result["artifact_errors"], [])
                self.assertEqual(result["status"], status)
                self.assertEqual(len(result["release_blockers"]), 1)

    def test_daily_validator_uses_the_same_draft_release_contract(self) -> None:
        contract = _missed_evidence_contract("SOURCE_LAG", None)
        with (
            mock.patch.object(
                validate_daily_report.thresholds,
                "missed_opportunity_evidence_contract_active",
                return_value=True,
            ),
            mock.patch.object(
                trade_report_stats,
                "missed_opportunity_evidence_contract",
                return_value=json.loads(json.dumps(contract)),
            ),
        ):
            result = validate_daily_report._validate_missed_opportunity_contract(
                period_start="2026-08-25 08:00:00",
                period_end="2026-09-01 08:00:00",
                embedded_metrics=_missed_outer(contract),
                lessons_db=Path("lessons.db"),
                live_trades_db=Path("live_trades.db"),
                market_db=Path("market.db"),
                briefing_dir=Path("logs/briefing"),
            )
        self.assertEqual(result["status"], "SOURCE_LAG")
        self.assertEqual(result["artifact_errors"], [])
        self.assertEqual(len(result["release_blockers"]), 1)

    def test_active_missing_contract_is_artifact_error(self) -> None:
        with (
            mock.patch.object(
                validate_periodic_report.thresholds,
                "missed_opportunity_evidence_contract_active",
                return_value=True,
            ),
            mock.patch.object(
                trade_report_stats,
                "missed_opportunity_evidence_contract",
            ) as rebuild,
        ):
            result = validate_periodic_report._validate_missed_opportunity_contract(
                period_start="2026-08-25 08:00:00",
                period_end="2026-09-01 08:00:00",
                embedded_metrics={},
                lessons_db=Path("lessons.db"),
                live_trades_db=Path("live_trades.db"),
                market_db=Path("market.db"),
                briefing_dir=Path("logs/briefing"),
            )
        rebuild.assert_not_called()
        self.assertEqual(result["status"], "ERROR")
        self.assertTrue(result["artifact_errors"])

    def test_contract_hash_window_or_count_drift_is_rejected(self) -> None:
        contract = _missed_evidence_contract()
        outer = _missed_outer(contract)
        outer["count"] = 9
        outer["evidence_contract"]["self_sha256"] = "c" * 64
        result = self._validate_evidence(contract, outer)
        joined = " | ".join(result["artifact_errors"])
        self.assertIn("embedded contract differs", joined)
        self.assertIn("outer count differs", joined)

    def test_pre_activation_keeps_legacy_path_without_rebuild(self) -> None:
        with (
            mock.patch.object(
                validate_periodic_report.thresholds,
                "missed_opportunity_evidence_contract_active",
                return_value=False,
            ),
            mock.patch.object(
                trade_report_stats,
                "missed_opportunity_evidence_contract",
            ) as rebuild,
        ):
            result = validate_periodic_report._validate_missed_opportunity_contract(
                period_start="2026-08-18 08:00:00",
                period_end="2026-08-25 08:00:00",
                embedded_metrics={},
                lessons_db=Path("lessons.db"),
                live_trades_db=Path("live_trades.db"),
                market_db=Path("market.db"),
                briefing_dir=Path("logs/briefing"),
            )
        rebuild.assert_not_called()
        self.assertFalse(result["active"])
        self.assertEqual(result["artifact_errors"], [])

    def test_monthly_window_handles_year_boundary(self) -> None:
        self.assertEqual(
            trade_report_stats.monthly_window("2026-01-01 00:00:00"),
            ("2025-12-01 08:00:00", "2026-01-01 08:00:00"),
        )

    def test_realized_performance_uses_confirmed_close_curve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _account, trades, _ledger = _make_dbs(Path(tmp))
            with closing(sqlite3.connect(trades)) as con:
                _trade(con, "2026-05-02 09:00:00", "close", 10.0)
                _trade(con, "2026-05-03 09:00:00", "close", -15.0)
                _trade(con, "2026-05-04 09:00:00", "close", 5.0)
                _trade(con, "2026-05-05 09:00:00", "open", 999.0)
                con.commit()
            result = trade_report_stats.realized_performance_stats(
                trades,
                "2026-05-01 08:00:00",
                "2026-06-01 08:00:00",
            )
            self.assertEqual(result["realized_pnl"], 0.0)
            self.assertEqual(result["max_drawdown_usdt"], 15.0)
            self.assertEqual(result["daily_observations"], 31)
            self.assertEqual(result["sharpe_approx"], 0.0)

    def test_weekly_window_override_is_rejected_before_query(self) -> None:
        with self.assertRaisesRegex(ValueError, "统计窗口必须"):
            daily_report_writer.prepare_weekly_payload({
                "week_start_ts": "2026-06-08 00:00:00",
                "period_start_ts": "2026-06-01 00:00:00",
                "period_end_ts": "2026-06-08 00:00:00",
            })

    def test_closed_hold_average_is_fifo_and_window_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _account, trades, _ledger = _make_dbs(Path(tmp))
            with closing(sqlite3.connect(trades)) as con:
                _trade(con, "2026-06-01 08:00:00", "open", None)
                _trade(con, "2026-06-01 20:00:00", "open", None)
                _trade(con, "2026-06-02 08:00:00", "close", 1.0, sz=2.0)
                _trade(con, "2026-06-03 09:00:00", "open", None,
                       symbol="ETH-USDT-SWAP")
                _trade(con, "2026-06-03 21:00:00", "close", 1.0,
                       symbol="ETH-USDT-SWAP")
                con.commit()
            result = trade_report_stats.closed_position_hold_stats(
                trades,
                "2026-06-02 00:00:00",
                "2026-06-04 00:00:00",
            )
            # First close consumes 24h and 12h lots => 18h; second is 12h.
            self.assertAlmostEqual(
                result["closed_position_avg_hold_hours"], 15.0)
            self.assertEqual(result["closed_position_hold_sample_count"], 2)
            self.assertEqual(result["closed_position_hold_unmatched_count"], 0)

    def test_weekly_and_monthly_artifacts_pass_independent_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account, trades, ledger = _make_dbs(root)
            weekly_dir = root / "weekly"
            monthly_dir = root / "monthly"
            with closing(sqlite3.connect(trades)) as con:
                _trade(con, "2026-05-02 09:00:00", "open", None)
                _trade(con, "2026-05-03 09:00:00", "close", 10.0)
                _trade(con, "2026-05-04 09:00:00", "open", None)
                _trade(con, "2026-05-05 09:00:00", "close", -15.0,
                       side="short")
                _trade(con, "2026-06-02 09:00:00", "open", None,
                       symbol="ETH-USDT-SWAP")
                _trade(con, "2026-06-03 09:00:00", "close", 4.0,
                       symbol="ETH-USDT-SWAP")
                con.commit()
            with closing(sqlite3.connect(ledger)) as con:
                con.execute(
                    "INSERT INTO execution_intents VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "live", "2026-05-04T09:00", "ETH-USDT-SWAP",
                        "open", "long", "failed_clean",
                        "2026-05-04 09:00:00", "2026-05-04 09:00:01",
                        "risk_reject:test_cap",
                    ),
                )
                con.commit()

            with mock.patch.object(
                    daily_report_writer, "LIVE_TRADES_DB", trades), mock.patch.object(
                    daily_report_writer, "LEDGER_DB", ledger), mock.patch.object(
                    daily_report_writer, "WEEKLY_REPORTS_DIR", weekly_dir), mock.patch.object(
                    daily_report_writer, "MONTHLY_REPORTS_DIR", monthly_dir):
                weekly = daily_report_writer.prepare_weekly_payload({
                    "week_start_ts": "2026-06-08 00:00:00",
                    "summary": "Live对账: OK",
                    "lessons": "weekly test",
                })
                self.assertAlmostEqual(
                    weekly["live_avg_hold_hours"], 24.0)
                monthly = daily_report_writer.prepare_monthly_payload({
                    "month_start_ts": "2026-06-01 00:00:00",
                    "live_total_pnl": 999.0,
                    "live_max_drawdown": 999.0,
                    "live_sharpe_approx": 999.0,
                    "summary": "Live对账: OK",
                    "lessons": "monthly test",
                })
                self.assertEqual(monthly["live_total_pnl"], -5.0)
                self.assertEqual(monthly["live_max_drawdown"], 15.0)
                self.assertEqual(
                    monthly["live_close_side_breakdown"]["long"]["close_count"], 1)
                self.assertEqual(
                    monthly["live_close_side_breakdown"]["short"]["close_count"], 1)
                raw = json.loads(monthly["raw"])
                self.assertEqual(
                    raw["report_audit"]["period_kind"], "monthly")
                self.assertIn(
                    "performance_metrics", raw["report_audit"])

                with closing(sqlite3.connect(account)) as con:
                    weekly_result = daily_report_writer.write_weekly(
                        con, weekly, True)
                    monthly_result = daily_report_writer.write_monthly(
                        con, monthly, True)
                    con.commit()
                weekly["trade_week_num"] = weekly_result["trade_week_num"]
                monthly["trade_month_num"] = monthly_result["trade_month_num"]
                weekly_path = Path(
                    daily_report_writer.write_weekly_markdown(weekly, True))
                monthly_path = Path(
                    daily_report_writer.write_monthly_markdown(monthly, True))

            weekly_check = validate_periodic_report.validate_report(
                kind="weekly",
                report_path=weekly_path,
                account_db=account,
                live_trades_db=trades,
                ledger_db=ledger,
            )
            monthly_check = validate_periodic_report.validate_report(
                kind="monthly",
                report_path=monthly_path,
                account_db=account,
                live_trades_db=trades,
                ledger_db=ledger,
            )
            self.assertTrue(weekly_check["ok"], weekly_check)
            self.assertTrue(monthly_check["ok"], monthly_check)

            monthly_path.write_text(
                monthly_path.read_text(encoding="utf-8").replace(
                    "| 实盘 | 2 | 2 | -5.0000 |",
                    "| 实盘 | 2 | 2 | 500.0000 |",
                ),
                encoding="utf-8",
            )
            tampered = validate_periodic_report.validate_report(
                kind="monthly",
                report_path=monthly_path,
                account_db=account,
                live_trades_db=trades,
                ledger_db=ledger,
            )
            self.assertFalse(tampered["ok"], tampered)

    def test_weekly_pnl_boundary_uses_fact_window_start(self) -> None:
        """A week straddling the migration keeps the pre-boundary PnL scope."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account, trades, ledger = _make_dbs(root)
            weekly_dir = root / "weekly"
            lessons = root / "lessons.db"
            with closing(sqlite3.connect(trades)) as con:
                _trade(con, "2026-08-18 09:00:00", "close", 10.0)
                _trade(con, "2026-08-19 09:00:00", "reduce", 5.0)
                con.commit()
            with closing(sqlite3.connect(lessons)) as con:
                con.execute("CREATE TABLE missed_opportunities(ts TEXT)")
                con.executemany(
                    "INSERT INTO missed_opportunities VALUES(?)",
                    [
                        ("2026-08-17 04:00:00",),
                        ("2026-08-23 03:45:00",),
                        ("2026-08-24 04:00:00",),
                    ],
                )
                con.commit()

            with (
                mock.patch.object(daily_report_writer, "DB_PATH", account),
                mock.patch.object(
                    daily_report_writer, "LIVE_TRADES_DB", trades),
                mock.patch.object(daily_report_writer, "LEDGER_DB", ledger),
                mock.patch.object(daily_report_writer, "LESSONS_DB", lessons),
                mock.patch.object(
                    daily_report_writer, "WEEKLY_REPORTS_DIR", weekly_dir),
                mock.patch.object(
                    daily_report_writer, "_load_frozen_exit_quality",
                    return_value=None),
            ):
                weekly = daily_report_writer.prepare_weekly_payload({
                    "week_start_ts": "2026-08-24 00:00:00",
                    "summary": "Live对账: OK",
                    "lessons": "boundary test",
                })
                self.assertEqual(weekly["live_total_pnl"], 10.0)
                self.assertEqual(weekly["missed_opps_window_count"], 2)
                # Reproduce the production W11 row: candidate-window contract
                # was frozen, but count was still null when the row was stored.
                raw = json.loads(weekly["raw"])
                raw["report_audit"]["missed_opportunity_metrics"][
                    "count"] = None
                weekly["raw"] = json.dumps(raw, ensure_ascii=False)
                with closing(sqlite3.connect(account)) as con:
                    result = daily_report_writer.write_weekly(
                        con, weekly, True)
                    con.commit()
                weekly["trade_week_num"] = result["trade_week_num"]
                weekly_path = Path(
                    daily_report_writer.write_weekly_markdown(weekly, True))

            check = validate_periodic_report.validate_report(
                kind="weekly",
                report_path=weekly_path,
                account_db=account,
                live_trades_db=trades,
                ledger_db=ledger,
                lessons_db=lessons,
            )
            self.assertTrue(check["ok"], check)
            self.assertIn(
                "missed_opportunity_count_recovered_from_authoritative_ledger",
                check["checks"],
            )


class MonthlyMarkdownBackfillTests(unittest.TestCase):
    """render_monthly_markdown_from_existing：历史行补渲染（只读+一致性闸）。"""

    def _seed(self, root: Path, *, stored_pnl: float):
        account, trades, ledger = _make_dbs(root)
        with closing(sqlite3.connect(trades)) as con:
            _trade(con, "2026-07-02 09:00:00", "close", 30.0)
            _trade(con, "2026-07-03 09:00:00", "close", -11.20942)
            con.commit()
        with closing(sqlite3.connect(account)) as con:
            con.execute(
                "INSERT INTO monthly_reports VALUES(?,?,?,?,?,?,?,?,?)",
                ("2026-07-01 08:00:00", "live", stored_pnl, None, None,
                 "七月复盘摘要（历史口径：曾达1R=hit_1R 25 笔）", "七月教训", "{}", 2),
            )
            con.commit()
        return account, trades, ledger

    def test_backfills_legacy_row_from_window_recompute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account, trades, ledger = self._seed(root, stored_pnl=18.79058)
            monthly_dir = root / "monthly"
            monthly_dir.mkdir()
            with mock.patch.object(
                    daily_report_writer, "LIVE_TRADES_DB", trades), mock.patch.object(
                    daily_report_writer, "LEDGER_DB", ledger), mock.patch.object(
                    daily_report_writer, "MONTHLY_REPORTS_DIR", monthly_dir):
                with closing(sqlite3.connect(account)) as con:
                    path = Path(
                        daily_report_writer.render_monthly_markdown_from_existing(
                            con, "2026-07-01 08:00:00"))
            # 历史键=窗口起点(7月)；canonical 键=产出月 1 号 => 文件名 2026-08-01
            self.assertEqual(path.name, "monthly-2026-08-01.md")
            text = path.read_text(encoding="utf-8")
            self.assertIn("2026-08-01 00:00:00", text)
            self.assertIn("[2026-07-01 08:00:00, 2026-08-01 08:00:00)", text)
            self.assertIn("18.7906", text)
            self.assertIn("七月复盘摘要", text)
            self.assertIn("七月教训", text)
            # 历史文本的 hit_1R 旧口径经窄豁免原样进档（事实核对未豁免）
            self.assertIn("hit_1R", text)
            with closing(sqlite3.connect(account)) as con:
                row = con.execute(
                    "SELECT month_start_ts, total_pnl, raw FROM monthly_reports"
                ).fetchone()
            # 只读：存储行（含历史键与 raw）不被改动
            self.assertEqual(row, ("2026-07-01 08:00:00", 18.79058, "{}"))

    def test_backfill_aborts_on_pnl_mismatch_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account, trades, ledger = self._seed(root, stored_pnl=999.0)
            monthly_dir = root / "monthly"
            monthly_dir.mkdir()
            with mock.patch.object(
                    daily_report_writer, "LIVE_TRADES_DB", trades), mock.patch.object(
                    daily_report_writer, "LEDGER_DB", ledger), mock.patch.object(
                    daily_report_writer, "MONTHLY_REPORTS_DIR", monthly_dir):
                with closing(sqlite3.connect(account)) as con:
                    with self.assertRaises(SystemExit):
                        daily_report_writer.render_monthly_markdown_from_existing(
                            con, "2026-07-01 08:00:00")
            self.assertEqual(list(monthly_dir.iterdir()), [])

    def test_external_payload_cannot_smuggle_vocabulary_exemption(self) -> None:
        """CLI 外部 payload 携带豁免标记必须被入口剥离，旧口径照拒。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account, trades, ledger = self._seed(root, stored_pnl=18.79058)
            monthly_dir = root / "monthly"
            monthly_dir.mkdir()
            payload = {
                "month_start_ts": "2026-08-01 00:00:00",
                "summary": "旧口径文本 hit_1R 走私测试",
                "lessons": "无",
                "allow_legacy_r_vocabulary": True,
            }
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "daily_report_writer.py"),
                    "--kind", "monthly", "--apply",
                    "--json", json.dumps(payload, ensure_ascii=False),
                    "--db-path", str(account),
                    "--live-trades-db", str(trades),
                    "--ledger-db", str(ledger),
                    "--monthly-reports-dir", str(monthly_dir),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("退化 hit_1R", proc.stdout + proc.stderr)
            self.assertEqual(list(monthly_dir.iterdir()), [])

    def test_backfill_requires_existing_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account, trades, ledger = self._seed(root, stored_pnl=18.79058)
            with mock.patch.object(
                    daily_report_writer, "LIVE_TRADES_DB", trades), mock.patch.object(
                    daily_report_writer, "LEDGER_DB", ledger):
                with closing(sqlite3.connect(account)) as con:
                    with self.assertRaises(SystemExit):
                        daily_report_writer.render_monthly_markdown_from_existing(
                            con, "2026-09-01 08:00:00")


if __name__ == "__main__":
    unittest.main()
