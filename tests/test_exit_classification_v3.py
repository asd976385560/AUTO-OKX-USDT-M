# -*- coding: utf-8 -*-
"""2026-09-26 对照 V3 experience::journey：平仓出口按价位实证分类。"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import apply_path_metrics_schema as path_metrics  # noqa: E402
import exit_taxonomy_report as taxonomy  # noqa: E402
import trade_experience_writer as writer  # noqa: E402


class AlgoExitTests(unittest.TestCase):
    def test_nearest_level_within_tolerance_wins(self):
        # 多头 100 开仓，开仓止损 95，移动止损 101（盈利侧），止盈 110
        self.assertEqual(
            ("tp_hit", False),
            taxonomy.classify_algo_exit("long", 100, 109.5, 95, 101, [110], 9.0))
        self.assertEqual(
            ("sl_hit", False),
            taxonomy.classify_algo_exit("long", 100, 94.2, 95, None, [110], -6.0))
        self.assertEqual(
            ("trail_stop", False),
            taxonomy.classify_algo_exit("long", 100, 100.8, 95, 101, [110], 0.5))
        self.assertEqual(
            ("breakeven_stop", False),
            taxonomy.classify_algo_exit("long", 100, 100.1, 95, 100.3, [110], -0.2))
        # 移动止损仍在亏损一侧 → sl_hit
        self.assertEqual(
            ("sl_hit", False),
            taxonomy.classify_algo_exit("long", 100, 97.0, 95, 97.0, [110], -3.0))
        # 空头镜像：移动止损 99（盈利侧）
        self.assertEqual(
            ("trail_stop", False),
            taxonomy.classify_algo_exit("short", 100, 99.2, 105, 99, [90], 0.8))

    def test_ties_prefer_take_profit_then_initial_stop(self):
        # 成交 100 恰好同时等距于 TP=100 与移动止损=100：止盈优先；
        # 开仓止损与移动止损相同：记 sl_hit
        self.assertEqual(
            ("tp_hit", False),
            taxonomy.classify_algo_exit("long", 99, 100, 100, 100, [100], 1.0))
        self.assertEqual(
            ("sl_hit", False),
            taxonomy.classify_algo_exit("long", 105, 100, 100, 100, [], -5.0))

    def test_no_match_falls_back_to_sign_and_is_flagged(self):
        self.assertEqual(
            ("sl_hit", True),
            taxonomy.classify_algo_exit("long", 100, 90, 95, None, [110], -10.0))
        self.assertEqual(
            ("tp_hit", True),
            taxonomy.classify_algo_exit("long", 100, None, 95, None, [110], 3.0))
        self.assertEqual(
            ("tp_hit", True),
            taxonomy.classify_algo_exit("long", 100, "nan", 95, None, [], None))


class ManualCloseTests(unittest.TestCase):
    def test_four_way_split(self):
        self.assertEqual(
            "manual_close_invalidated",
            taxonomy.classify_manual_close("long", 98.0, 97.5))
        self.assertEqual(
            "manual_close_discretionary",
            taxonomy.classify_manual_close("long", 98.0, 99.0))
        self.assertEqual(
            "manual_close_invalidated",
            taxonomy.classify_manual_close("short", 102.0, 102.0))
        self.assertEqual(
            "manual_close_unverified",
            taxonomy.classify_manual_close("long", 98.0, None))
        self.assertEqual(
            "manual_close_no_invalidation_px",
            taxonomy.classify_manual_close("long", None, 97.0))

    def test_invalidation_and_take_profit_extraction(self):
        raw = {
            "tp_trigger_px": 110.0,
            "open_execution_package": {"target": 110.0, "invalidation_px": 98.5},
            "decision_card": {
                "risk_reward": {"target": 112.0},
                "invalidation_point": {"condition": "close below 98", "price": 98.0},
            },
        }
        self.assertEqual([110.0, 112.0], taxonomy.take_profit_levels(raw))
        self.assertEqual(98.5, taxonomy.invalidation_price(raw))
        self.assertEqual(
            98.0,
            taxonomy.invalidation_price({"decision_card": raw["decision_card"]}))
        self.assertIsNone(taxonomy.invalidation_price(
            {"decision_card": {"invalidation_point": {"condition": "text only"}}}))
        self.assertEqual([], taxonomy.take_profit_levels("not a dict"))


class ContextTests(unittest.TestCase):
    OPEN_RAW = {
        "fill_px": 100.0, "sl_trigger_px": 95.0, "tp_trigger_px": 110.0,
        "open_execution_package": {"invalidation_px": 98.0},
        "close_events": [{"fill_px": 100.9, "reason": "RECON-x"}],
    }

    def _root_with_adjustments(self, root: Path) -> None:
        con = sqlite3.connect(root / "live_trades.db")
        con.execute(
            "CREATE TABLE trade_cycles(cycle_id TEXT PRIMARY KEY,ts TEXT,"
            "mode TEXT,decision TEXT,n_orders INTEGER,equity REAL,note TEXT,raw TEXT)")
        rows = [
            ("c1", "2026-08-10 09:00:00", json.dumps({
                "action_taken": "ADJUST_PROTECTION", "symbol": "BTC-USDT-SWAP",
                "pos_side": "long", "applied": {"sl": 99.0, "sz": 1}})),
            ("c2", "2026-08-10 10:00:00", json.dumps({
                "action_taken": "ADJUST_PROTECTION", "symbol": "BTC-USDT-SWAP",
                "pos_side": "long", "applied": {"sl": 101.0, "sz": 1}})),
            ("c3", "2026-08-10 10:30:00", json.dumps({
                "action_taken": "ADJUST_PROTECTION", "symbol": "ETH-USDT-SWAP",
                "pos_side": "long", "applied": {"sl": 150.0, "sz": 1}})),
            ("c4", "2026-08-10 12:00:00", json.dumps({
                "action_taken": "ADJUST_PROTECTION", "symbol": "BTC-USDT-SWAP",
                "pos_side": "long", "applied": {"sl": 103.0, "sz": 1}})),
            ("c5", "2026-08-10 10:45:00", json.dumps({"action_taken": "HOLD"})),
        ]
        con.executemany(
            "INSERT INTO trade_cycles(cycle_id,ts,raw) VALUES(?,?,?)", rows)
        con.commit()
        con.close()

    def test_last_adjusted_sl_is_scoped_to_symbol_side_and_holding_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._root_with_adjustments(root)
            self.assertEqual(101.0, taxonomy.last_adjusted_sl(
                root, "BTC-USDT-SWAP", "long",
                "2026-08-10 08:00:00", "2026-08-10 11:00:00"))
            self.assertIsNone(taxonomy.last_adjusted_sl(
                root, "BTC-USDT-SWAP", "short",
                "2026-08-10 08:00:00", "2026-08-10 11:00:00"))
            self.assertIsNone(taxonomy.last_adjusted_sl(
                root, "BTC-USDT-SWAP", "long",
                "2026-08-10 10:30:00", "2026-08-10 10:59:00"))
            self.assertIsNone(taxonomy.last_adjusted_sl(
                root / "missing", "BTC-USDT-SWAP", "long", "a", "b"))

    def test_last_closed_15m_close_excludes_the_bar_still_open(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE kline_cache(symbol TEXT,tf TEXT,ts TEXT,c REAL)")
        con.executemany("INSERT INTO kline_cache VALUES(?,?,?,?)", [
            ("BTC-USDT-SWAP", "15m", "2026-08-10T02:30:00Z", 97.0),
            ("BTC-USDT-SWAP", "15m", "2026-08-10T02:45:00Z", 98.0),
            ("BTC-USDT-SWAP", "15m", "2026-08-10T03:00:00Z", 99.0),
        ])
        # 平仓 11:07 CST = 03:07Z：03:00 那根还没收盘，取 02:45（收盘 03:00）
        self.assertEqual(98.0, taxonomy.last_closed_15m_close(
            con, "BTC-USDT-SWAP", "2026-08-10 11:07:00"))
        self.assertEqual(99.0, taxonomy.last_closed_15m_close(
            con, "BTC-USDT-SWAP", "2026-08-10 11:15:00"))
        self.assertIsNone(taxonomy.last_closed_15m_close(
            None, "BTC-USDT-SWAP", "2026-08-10 11:15:00"))
        con.close()

    def test_exchange_side_close_is_classified_by_price(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._root_with_adjustments(root)
            close_trade = {"fill_px": 100.9, "reason": "RECON-fill",
                           "reconcile_source": "fills"}
            context = taxonomy.exit_context(
                self.OPEN_RAW, close_trade, symbol="BTC-USDT-SWAP", side="long",
                open_ts="2026-08-10 08:00:00", close_ts="2026-08-10 11:00:00",
                realized_pnl=0.8, db_root=root, mcon=None)
        self.assertEqual(101.0, context["moved_sl"])
        self.assertEqual([110.0], context["tps"])
        self.assertEqual(98.0, context["invalidation_px"])
        self.assertEqual("trail_stop", taxonomy.classify_exit(**context))

    def test_agent_close_is_split_by_invalidation_price(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE kline_cache(symbol TEXT,tf TEXT,ts TEXT,c REAL)")
        con.execute("INSERT INTO kline_cache VALUES(?,?,?,?)",
                    ("BTC-USDT-SWAP", "15m", "2026-08-10T02:30:00Z", 97.5))
        close_trade = {"fill_px": 97.6, "reason": "thesis broken, closing"}
        context = taxonomy.exit_context(
            self.OPEN_RAW, close_trade, symbol="BTC-USDT-SWAP", side="long",
            open_ts="2026-08-10 08:00:00", close_ts="2026-08-10 11:00:00",
            realized_pnl=-2.4, db_root=None, mcon=con)
        con.close()
        self.assertEqual(97.5, context["last_close_px"])
        self.assertEqual("manual_close_invalidated",
                         taxonomy.classify_exit(**context))
        no_price = dict(context, invalidation_px=None)
        self.assertEqual("manual_close_no_invalidation_px",
                         taxonomy.classify_exit(**no_price))
        maintenance = dict(context, reason="IMR 破 0.66 硬闸去风险")
        self.assertEqual("imr_forced_reduce", taxonomy.classify_exit(**maintenance))

    def test_persisted_exchange_side_flag_is_honoured(self):
        self.assertTrue(taxonomy.is_exchange_side_close("filled", {"exchange_side": True}))
        self.assertFalse(taxonomy.is_exchange_side_close("filled", {"exchange_side": False}))
        context = taxonomy.exit_context(
            self.OPEN_RAW, None, symbol="BTC-USDT-SWAP", side="long",
            open_ts="2026-08-10 08:00:00", close_ts="2026-08-10 11:00:00",
            realized_pnl=0.8, db_root=None, mcon=None)
        # close_events 里的最后一次事件带 exchange_side=True 时走价位实证
        self.assertEqual("reconcile_backfill" if False else taxonomy.classify_exit(
            **dict(context, raw={"exchange_side": True}, fill_px=109.6)), "tp_hit")

    def test_report_cli_uses_the_full_exit_context(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._root_with_adjustments(root)
            con = sqlite3.connect(root / "live_trades.db")
            con.execute(
                "CREATE TABLE trades(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "cycle_id TEXT,ts TEXT,symbol TEXT,action TEXT,side TEXT,sz REAL,"
                "fill_px REAL,lev REAL,margin REAL,notional REAL,score_total INTEGER,"
                "reasoning TEXT,deviation TEXT,degradation TEXT,pnl REAL,raw TEXT)")
            con.execute(
                "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,fill_px,"
                "notional,reasoning,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("c0", "2026-08-10 08:00:00", "BTC-USDT-SWAP", "open", "long",
                 1.0, 100.0, 1000.0, "open", None,
                 json.dumps({"sl_trigger_px": 95.0, "tp_trigger_px": 110.0})))
            con.execute(
                "INSERT INTO trades(cycle_id,ts,symbol,action,side,sz,fill_px,"
                "notional,reasoning,pnl,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("c9", "2026-08-10 11:00:00", "BTC-USDT-SWAP", "close", "long",
                 1.0, 100.9, 1009.0, "RECON-fill", 8.0,
                 json.dumps({"reconcile_source": "fills"})))
            con.commit()
            con.close()
            output = io.StringIO()
            with mock.patch.object(sys, "argv", [
                "exit_taxonomy_report.py", "--db-root", str(root),
                "--since", "2026-08-01", "--until", "2026-09-01",
            ]), contextlib.redirect_stdout(output):
                self.assertEqual(0, taxonomy.main())
        report = json.loads(output.getvalue())
        self.assertEqual(1, report["total_closes"])
        # 成交 100.9 贴 10:00 那次 ADJUST_PROTECTION 落地的止损 101（盈利侧）→ trail_stop
        self.assertEqual("trail_stop", report["rows"][0]["category"])
        self.assertEqual("sl_from_open_raw", report["rows"][0]["r_source"])

    def test_legacy_four_argument_call_still_works(self):
        self.assertEqual(
            "reconcile_backfill",
            taxonomy.classify("RECON-fill", {"reconcile_source": "fills"}, 95.0, None))
        self.assertEqual(
            "manual_close_no_invalidation_px",
            taxonomy.classify("closing", {}, 95.0, 97.0))


class WriterIntegrationTests(unittest.TestCase):
    def test_close_event_carries_reason_and_exchange_side_flag(self):
        event = writer._close_event(
            {"ordId": "1", "fill_px": 99.0, "reason": "RECON-x",
             "reconcile_source": "fills"},
            "c1", "2026-08-10 11:00:00", 1.0, -1.0)
        self.assertEqual("RECON-x", event["reason"])
        self.assertTrue(event["exchange_side"])
        agent = writer._close_event(
            {"ordId": "2", "fill_px": 99.0, "reasoning": "manual"},
            "c1", "2026-08-10 11:00:00", 1.0, -1.0)
        self.assertEqual("manual", agent["reason"])
        self.assertFalse(agent["exchange_side"])

    def test_fill_path_metrics_writes_price_based_category(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market = sqlite3.connect(root / "market.db")
            market.execute(
                "CREATE TABLE kline_cache(symbol TEXT,tf TEXT,ts TEXT,"
                "o REAL,h REAL,l REAL,c REAL)")
            market.commit()
            market.close()
            con = sqlite3.connect(":memory:")
            con.execute(
                "CREATE TABLE trade_experiences("
                "id INTEGER PRIMARY KEY,initial_risk_usdt REAL,mfe_r REAL,"
                "mae_r REAL,realized_r_net REAL,close_at_1r INTEGER,"
                "ever_hit_1r INTEGER,exit_category TEXT,path_coverage TEXT,"
                "path_metric_version INTEGER)")
            con.execute("INSERT INTO trade_experiences(id) VALUES(1)")
            open_raw = {"fill_px": 100.0, "sl_trigger_px": 95.0,
                        "tp_trigger_px": 110.0, "notional": 1000.0}
            close_trade = {"fill_px": 109.6, "reason": "RECON-fill",
                           "reconcile_source": "fills"}
            with mock.patch.object(writer, "_DB_ROOT", root):
                writer._fill_path_metrics(
                    con, 1, "BTC-USDT-SWAP", "long", open_raw,
                    "2026-08-10 10:00:00", "2026-08-10 11:00:00", 96.0,
                    close_trade)
            category, version = con.execute(
                "SELECT exit_category,path_metric_version FROM trade_experiences "
                "WHERE id=1").fetchone()
            con.close()
        self.assertEqual("tp_hit", category)
        self.assertEqual(path_metrics.PATH_METRIC_VERSION, version)

    def test_backfill_row_uses_close_event_context(self):
        market = sqlite3.connect(":memory:")
        market.row_factory = sqlite3.Row
        market.execute(
            "CREATE TABLE kline_cache(symbol TEXT,tf TEXT,ts TEXT,"
            "o REAL,h REAL,l REAL,c REAL)")
        market.execute(
            "CREATE TABLE trade_experiences(id INTEGER,ts TEXT,closed_at TEXT,"
            "symbol TEXT,side TEXT,open_sz REAL,realized_pnl REAL,raw TEXT)")
        raw = {
            "fill_px": 100.0, "sl_trigger_px": 95.0, "notional": 1000.0,
            "close_events": [{"fill_px": 94.8, "reason": "RECON-fill",
                              "exchange_side": True, "pnl": -52.0}],
        }
        market.execute(
            "INSERT INTO trade_experiences VALUES(?,?,?,?,?,?,?,?)",
            (1, "2026-08-10 10:00:00", "2026-08-10 12:00:00", "BTC-USDT-SWAP",
             "long", 1.0, -52.0, json.dumps(raw)))
        row = market.execute("SELECT * FROM trade_experiences").fetchone()
        metrics = path_metrics.metrics_for_row(market, row, None)
        market.close()
        self.assertEqual("sl_hit", metrics["exit_category"])


if __name__ == "__main__":
    unittest.main()
