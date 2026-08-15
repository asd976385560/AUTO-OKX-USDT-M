# -*- coding: utf-8 -*-
"""退出质量闭环契约（V2.1 §7）：后验窗固化、未知不冒充 0、writer/validator 同源。"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import daily_report_writer  # noqa: E402
import exit_quality  # noqa: E402
import validate_daily_report  # noqa: E402


def _account_db(path: Path, rows: list[tuple]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE trade_experiences("
        "id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, status TEXT,"
        "closed_at TEXT, mfe_r REAL, mae_r REAL, realized_r_net REAL,"
        "ever_hit_1r INTEGER, close_at_1r INTEGER, exit_category TEXT,"
        "path_coverage TEXT)"
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
        "CREATE TABLE trade_cycles(cycle_id TEXT, ts TEXT, mode TEXT,"
        " decision TEXT, n_orders INTEGER, equity REAL, note TEXT, raw TEXT)")
    connection.execute(
        "CREATE TABLE trades(id INTEGER PRIMARY KEY, cycle_id TEXT, ts TEXT,"
        " symbol TEXT, action TEXT, side TEXT, sz REAL)")
    connection.executemany(
        "INSERT INTO trade_cycles(cycle_id,ts,mode,raw) VALUES(?,?,?,?)",
        cycles)
    connection.executemany(
        "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz) "
        "VALUES(?,?,?,?,?,?)", fills)
    connection.commit()
    connection.close()
    return path


class CandidateWindowTests(unittest.TestCase):
    def test_window_is_shifted_by_the_outcome_horizon(self):
        start, end = exit_quality.candidate_window(
            "2026-08-14 08:00:00", "2026-08-15 08:00:00")
        self.assertEqual("2026-08-14 04:00:00", start)
        self.assertEqual("2026-08-15 04:00:00", end)

    def test_writer_and_validator_derive_the_same_window(self):
        writer = exit_quality.candidate_window(
            "2026-08-14 08:00:00", "2026-08-15 08:00:00")
        validator = validate_daily_report._expected_exit_quality_window(
            "2026-08-14 08:00:00", "2026-08-15 08:00:00")
        self.assertEqual(writer, validator)

    def test_activation_boundary_is_shared(self):
        self.assertEqual(
            daily_report_writer.EXIT_QUALITY_ACTIVATION_TS,
            validate_daily_report.EXIT_QUALITY_ACTIVATION_TS,
        )

    def test_outcome_horizon_is_shared(self):
        self.assertEqual(
            exit_quality.OUTCOME_HORIZON_HOURS,
            validate_daily_report.EXIT_QUALITY_OUTCOME_HOURS,
        )


class PeakGivebackTests(unittest.TestCase):
    ROWS = [
        # 曾达 1R 却在 1R 以下平仓 → 进错失止盈池
        ("AAA-USDT-SWAP", "short", "closed", "2026-08-14 10:00:00",
         1.62, 0.05, 0.04, 1, 0, "discretionary_manual", "full:1.00"),
        # 曾达 1R 且平仓仍 ≥1R → 不进池
        ("BBB-USDT-SWAP", "long", "closed", "2026-08-14 11:00:00",
         2.40, 0.10, 1.90, 1, 1, "discretionary_manual", "full:1.00"),
        # 路径覆盖不足 → 未知，不进任何分布，也不算 0
        ("CCC-USDT-SWAP", "long", "closed", "2026-08-14 12:00:00",
         3.00, 0.10, 0.10, 1, 0, "discretionary_manual", "partial:0.40"),
        # 窗口外（候选窗右开）→ 不计
        ("DDD-USDT-SWAP", "long", "closed", "2026-08-15 05:00:00",
         9.00, 0.10, 0.10, 1, 0, "discretionary_manual", "full:1.00"),
    ]

    def _run(self):
        with tempfile.TemporaryDirectory() as temp:
            account = _account_db(Path(temp) / "account.db", self.ROWS)
            return exit_quality.peak_giveback(
                account, "2026-08-14 04:00:00", "2026-08-15 04:00:00")

    def test_pool_only_holds_peaks_that_were_not_kept(self):
        result = self._run()
        self.assertEqual(1, result["missed_take_profit_pool_size"])
        self.assertEqual(
            "AAA-USDT-SWAP",
            result["missed_take_profit_pool"][0]["symbol"])
        self.assertEqual(1.58, result["missed_take_profit_pool"][0]["giveback_r"])

    def test_insufficient_path_coverage_is_unknown_not_zero(self):
        result = self._run()
        self.assertEqual(3, result["closed_rows"])
        self.assertEqual(2, result["measured_rows"])
        self.assertEqual(1, result["unknown_path_rows"])
        # 覆盖不足那笔曾达 1R，但既不进分布也不进池——不冒充「没错失」。
        self.assertEqual(2, result["reached_1r"])
        self.assertEqual(1, result["closed_at_or_above_1r"])

    def test_window_right_edge_is_exclusive(self):
        result = self._run()
        symbols = {item["symbol"] for item in result["missed_take_profit_pool"]}
        self.assertNotIn("DDD-USDT-SWAP", symbols)

    def test_losses_never_masquerade_as_peak_giveback(self):
        rows = [
            ("EEE-USDT-SWAP", "long", "closed", "2026-08-14 10:00:00",
             0.20, 1.00, -1.10, None, 0, "discretionary_sl_cite", "full:1.00"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            account = _account_db(Path(temp) / "account.db", rows)
            result = exit_quality.peak_giveback(
                account, "2026-08-14 04:00:00", "2026-08-15 04:00:00")
        # 从没攒出浮盈峰值 → 不进「浮盈峰值回吐」分布
        self.assertEqual(0, result["profitable_peak_rows"])
        self.assertEqual(1, result["measured_rows"])


class MarginReviewTests(unittest.TestCase):
    def _cycles(self):
        flagged = {
            "live_facts": {"positions": [{
                "instId": "AAA-USDT-SWAP",
                "upl_ratio_initial_margin": 1.10,
                "margin_return_review_at_or_above_50pct": True,
            }]},
            "decision_card": {
                "agent_judgement": "AAA REDUCE 50: 峰值回吐超过复核线。"},
        }
        silent = {
            "live_facts": {"positions": [{
                "instId": "BBB-USDT-SWAP",
                "upl_ratio_initial_margin": 0.80,
                "margin_return_review_at_or_above_50pct": True,
            }]},
            "decision_card": {"agent_judgement": "No action this cycle."},
        }
        below = {
            "live_facts": {"positions": [{
                "instId": "CCC-USDT-SWAP",
                "upl_ratio_initial_margin": 0.10,
                "margin_return_review_at_or_above_50pct": False,
            }]},
            "decision_card": {"agent_judgement": "CCC HOLD."},
        }
        return [
            ("2026-08-14T10:00", "2026-08-14 10:05:00", "live",
             json.dumps(flagged)),
            ("2026-08-14T10:15", "2026-08-14 10:20:00", "live",
             json.dumps(silent)),
            ("2026-08-14T10:30", "2026-08-14 10:35:00", "live",
             json.dumps(below)),
        ]

    def test_review_rate_and_disposition_come_from_evidence(self):
        fills = [("2026-08-14T10:00", "2026-08-14 10:06:00",
                  "AAA-USDT-SWAP", "reduce", "long", 5.0)]
        with tempfile.TemporaryDirectory() as temp:
            live = _live_db(
                Path(temp) / "live_trades.db", self._cycles(), fills)
            result = exit_quality.margin_return_review(
                live, "2026-08-14 04:00:00", "2026-08-15 04:00:00")
        self.assertEqual(2, result["flagged_position_cycles"])
        self.assertEqual(1, result["explicitly_reviewed"])
        self.assertEqual(0.5, result["explicit_review_rate"])
        self.assertEqual({"hold": 1, "reduce": 1}, result["disposition_counts"])

    def test_no_flagged_position_reports_none_not_zero_rate(self):
        with tempfile.TemporaryDirectory() as temp:
            live = _live_db(
                Path(temp) / "live_trades.db", self._cycles()[2:], [])
            result = exit_quality.margin_return_review(
                live, "2026-08-14 04:00:00", "2026-08-15 04:00:00")
        self.assertEqual(0, result["flagged_position_cycles"])
        self.assertIsNone(result["explicit_review_rate"])


class ReadOnlyContractTests(unittest.TestCase):
    def test_compute_declares_no_writes_no_replay_no_orders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            account = _account_db(root / "account.db", PeakGivebackTests.ROWS)
            live = _live_db(root / "live_trades.db", [], [])
            block = exit_quality.compute(
                account_db=account,
                live_trades_db=live,
                report_start_ts="2026-08-14 08:00:00",
                report_end_ts="2026-08-15 08:00:00",
            )
        self.assertEqual(0, block["safety"]["production_database_writes"])
        self.assertEqual(0, block["safety"]["cycles_replayed"])
        self.assertFalse(block["safety"]["window_extended"])
        self.assertEqual(0, block["safety"]["orders_placed"])


class RenderedSectionTests(unittest.TestCase):
    def test_missing_block_renders_unavailable_not_zero(self):
        text = daily_report_writer._exit_quality_block({})
        self.assertIn("退出质量统计不可用", text)
        self.assertNotIn("错失止盈池: 0", text)

    def test_rendered_section_is_parseable_by_the_validator(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            account = _account_db(root / "account.db", PeakGivebackTests.ROWS)
            live = _live_db(root / "live_trades.db", [], [])
            block = exit_quality.compute(
                account_db=account,
                live_trades_db=live,
                report_start_ts="2026-08-14 08:00:00",
                report_end_ts="2026-08-15 08:00:00",
            )
            text = daily_report_writer._exit_quality_block(
                {"exit_quality": block})
            counts = validate_daily_report._independent_exit_quality_counts(
                account, "2026-08-14 04:00:00", "2026-08-15 04:00:00")
        self.assertIn("## 🚪 退出质量", text)
        self.assertIn("候选窗口 [2026-08-14 04:00:00, 2026-08-15 04:00:00)", text)
        self.assertIn(
            f"已成熟平仓: {counts['closed_rows']} 笔", text)
        self.assertIn(
            f"错失止盈池: {counts['missed_take_profit_pool_size']} 笔", text)


if __name__ == "__main__":
    unittest.main()
