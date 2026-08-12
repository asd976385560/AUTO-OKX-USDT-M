"""Weekly/monthly fact-window and pre-send validation regressions."""
from __future__ import annotations

import json
import sqlite3
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


if __name__ == "__main__":
    unittest.main()
