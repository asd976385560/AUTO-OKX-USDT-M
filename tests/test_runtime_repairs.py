# -*- coding: utf-8 -*-
"""2026-07-26 运行修复最小回归。

只覆盖本轮修复的确定性行为；不连接交易所、不写生产数据库、不触发 Agent。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "collectors", ROOT / "scripts", ROOT / "collectors" / "sources"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import fast_collect  # noqa: E402
import build_push_payload  # noqa: E402
import collect_slow  # noqa: E402
import collect_market_features  # noqa: E402
import daily_maintenance  # noqa: E402
import news_collect  # noqa: E402
import public_macro  # noqa: E402
import query_state  # noqa: E402
import reconcile_exchange_closes  # noqa: E402
import render_push_report  # noqa: E402
import slow_collect  # noqa: E402
import source_freshness  # noqa: E402
import validate_push_format  # noqa: E402
import _okx_http  # noqa: E402
from core import order_executor  # noqa: E402


class SlowCollectRegressionTests(unittest.TestCase):
    def test_collect_slow_connections_support_named_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "regime.db"
            seed = sqlite3.connect(db_path)
            seed.execute("CREATE TABLE sample(observation_date TEXT, value REAL)")
            seed.execute("INSERT INTO sample VALUES('2026-07-28', 1.0)")
            seed.commit()
            seed.close()

            connection = collect_slow.open_db(Path(tmp), "regime.db")
            try:
                row = connection.execute(
                    "SELECT observation_date,value FROM sample"
                ).fetchone()
                self.assertEqual(row["observation_date"], "2026-07-28")
                self.assertEqual(row["value"], 1.0)
            finally:
                connection.close()

    def test_retry_cycle_stays_on_hour_slot(self):
        now = datetime(2026, 7, 26, 23, 36, 58, tzinfo=slow_collect.CST)
        self.assertEqual(slow_collect._hour_cycle_id(now), "2026-07-26T23:00")

    def test_slow_kline_partial_batch_is_degraded_and_bounded(self):
        class FakeConnection:
            def __init__(self):
                self.rows = []
                self.committed = False

            def executemany(self, _sql, rows):
                self.rows.extend(rows)

            def commit(self):
                self.committed = True

        candle = [
            "1785196800000", "100", "101", "99", "100.5",
            "0", "0", "1234",
        ]
        connection = FakeConnection()
        with (
            mock.patch.object(collect_slow, "SLOW_TIMEFRAMES", {"1H": "1H"}),
            mock.patch.object(
                collect_slow,
                "fetch_candles_batch_sync",
                return_value={
                    "AAA-USDT-SWAP": [candle],
                    "BBB-USDT-SWAP": [],
                },
            ) as fetch,
        ):
            count, incomplete = collect_slow.collect_slow_klines(
                connection,
                ["AAA-USDT-SWAP", "BBB-USDT-SWAP"],
            )
        self.assertEqual(count, 1)
        self.assertEqual(incomplete, ["1H:1/2"])
        self.assertTrue(connection.committed)
        self.assertLessEqual(
            fetch.call_args.kwargs["batch_timeout_s"],
            collect_slow.SLOW_KLINE_TF_TIMEOUT_S,
        )

    def test_okx_batch_deadline_stops_before_request(self):
        client = mock.Mock()
        with self.assertRaises(TimeoutError):
            _okx_http._get_data(
                client,
                "/api/v5/market/candles",
                {"instId": "BTC-USDT-SWAP"},
                deadline=time.monotonic() - 1,
            )
        client.get.assert_not_called()

    def test_okx_request_without_batch_deadline_keeps_client_timeout(self):
        client = mock.Mock()
        response = client.get.return_value
        response.json.return_value = {"code": "0", "data": []}
        self.assertEqual(
            _okx_http._get_data(
                client,
                "/api/v5/market/candles",
                {"instId": "BTC-USDT-SWAP"},
                retries=0,
            ),
            [],
        )
        self.assertNotIn("timeout", client.get.call_args.kwargs)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_timeout_returns_before_outer_cron_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "sleepy.py"
            script.write_text(
                "import time\n"
                "print('[WARN] partial timeout evidence', flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            started = time.monotonic()
            result = slow_collect.run_step("sleepy", script, [], timeout=1)
            elapsed = time.monotonic() - started
        self.assertEqual(result["rc"], 124)
        self.assertIn("process tree terminated", result["stderr_tail"])
        self.assertIn("partial timeout evidence", result["warn_tail"][0])
        self.assertLess(elapsed, 8)


class FastCollectRegressionTests(unittest.TestCase):
    def test_payload_error_is_preserved(self):
        step = {
            "name": "collect_data",
            "ok": False,
            "rc": 1,
            "payload": {"error": "TimeoutError: upstream unavailable"},
            "stderr_tail": "",
        }
        self.assertEqual(
            fast_collect._step_error(step),
            "collect_data: TimeoutError: upstream unavailable",
        )

    def test_stderr_fallback_is_preserved(self):
        step = {
            "name": "market_features",
            "ok": False,
            "rc": 2,
            "payload": None,
            "stderr_tail": "schema mismatch",
        }
        self.assertEqual(
            fast_collect._step_error(step),
            "market_features: schema mismatch",
        )

    def test_positioning_runs_only_on_hour_slot_by_default(self):
        self.assertTrue(
            collect_market_features.positioning_due("2026-07-27T20:00", "auto")
        )
        self.assertFalse(
            collect_market_features.positioning_due("2026-07-27T20:15", "auto")
        )
        self.assertTrue(
            collect_market_features.positioning_due("2026-07-27T20:15", "always")
        )

    def test_okx_cli_top_long_short_payload_is_parsed(self):
        payload = [{
            "data": [{
                "instId": "BTC-USDT-SWAP",
                "timeframes": {
                    "1H": {
                        "indicators": {
                            "TOPLONGSHORT": [{
                                "ts": "1785153600000",
                                "values": {
                                    "longRatio": "0.54",
                                    "shortRatio": "0.46",
                                    "longShortRatio": "1.18",
                                },
                            }]
                        }
                    }
                },
            }]
        }]
        row = collect_market_features.positioning_row(
            payload, "2026-07-27T20:00", "2026-07-27T12:10:00Z",
            "BTC-USDT-SWAP",
        )
        self.assertEqual(row[3], "BTC-USDT-SWAP")
        self.assertEqual(row[4], "1H")
        self.assertAlmostEqual(row[5], 0.54)
        self.assertAlmostEqual(row[7], 1.18)


class DailyInspectionRegressionTests(unittest.TestCase):
    def test_macro_ratio_is_rendered_as_percent(self):
        self.assertEqual(build_push_payload._ratio_pct(-0.0007659), -0.08)
        self.assertIsNone(build_push_payload._ratio_pct(None))

    def test_daily_maintenance_help_never_executes_steps(self):
        with (
            mock.patch.object(daily_maintenance.subprocess, "run") as run,
            mock.patch("sys.stdout", new=io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            daily_maintenance.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        run.assert_not_called()

    def test_collection_and_cron_failures_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = sqlite3.connect(root / "ledger.db")
            ledger.execute(
                "CREATE TABLE collection_runs("
                "cycle_id TEXT,source TEXT,status TEXT,ts TEXT,err TEXT)"
            )
            now_cst = datetime.now(query_state.CST).strftime("%Y-%m-%d %H:%M:%S")
            ledger.execute(
                "INSERT INTO collection_runs VALUES(?,?,?,?,?)",
                ("2026-07-26T23:00", "fast", "error", now_cst, "network timeout"),
            )
            ledger.commit()
            ledger.close()

            openclaw_db = root / "openclaw.sqlite"
            cron = sqlite3.connect(openclaw_db)
            cron.execute(
                "CREATE TABLE cron_jobs("
                "store_key TEXT,job_id TEXT,name TEXT,last_run_status TEXT,"
                "last_error TEXT,consecutive_errors INTEGER)"
            )
            cron.execute(
                "CREATE TABLE cron_run_logs("
                "store_key TEXT,job_id TEXT,ts INTEGER,status TEXT,error TEXT,"
                "run_at_ms INTEGER,duration_ms INTEGER)"
            )
            now_ms = int(time.time() * 1000)
            cron.execute(
                "INSERT INTO cron_jobs VALUES(?,?,?,?,?,?)",
                ("cron", "slow", "okx-slow-collect", "error", "timed out", 2),
            )
            cron.execute(
                "INSERT INTO cron_run_logs VALUES(?,?,?,?,?,?,?)",
                ("cron", "slow", now_ms, "error", "timed out", now_ms, 480000),
            )
            cron.commit()
            cron.close()

            results = []
            with mock.patch.dict(
                os.environ, {"OPENCLAW_STATE_DB": str(openclaw_db)}, clear=False
            ):
                query_state.check_collection_failures(
                    str(root), stale_min=15, hh01_only=False, results=results
                )

        self.assertEqual(results[0]["status"], "WARN")
        self.assertEqual(len(results[0]["collection_errors"]), 1)
        self.assertEqual(len(results[0]["cron_errors"]), 1)
        self.assertEqual(len(results[0]["active_cron_errors"]), 1)

    def test_regime_check_prefers_authoritative_public_macro_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "regime.db")
            con.row_factory = sqlite3.Row
            con.executescript(public_macro.TABLE_DDL)
            public_macro.upsert_observations(
                con,
                [
                    {
                        "metric": public_macro.METRIC_DXY_ECB,
                        "observation_date": "2026-07-27",
                        "source": public_macro.SOURCE_ECB_DXY,
                        "status": "official_inputs_calculated",
                        "value": 101.25,
                    },
                    {
                        "metric": public_macro.METRIC_FEAR_GREED,
                        "observation_date": "2026-07-27",
                        "source": public_macro.SOURCE_ALTERNATIVE,
                        "status": "official_primary",
                        "value": 30,
                        "label": "Fear",
                    },
                ],
            )
            con.commit()
            con.close()
            now_utc = datetime.now(query_state.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            stale_cross_market = {
                "ts": now_utc,
                "regime": "neutral",
                "dxy": 120.0,
                "vix": 18.0,
                "spx": 6500.0,
                "btc_mcap_chg_24h_usd": 1.0,
                "btc_etf_net_flow_usd": None,
                "dxy_calc_ecb": None,
                "fear_greed": None,
                "fear_greed_label": None,
            }
            results = []
            with (
                mock.patch(
                    "_regime_read.latest_cross_market",
                    return_value=stale_cross_market,
                ),
                mock.patch("_regime_read.latest_source", return_value="regime.db"),
            ):
                query_state.check_regime(
                    str(root), stale_min=15, hh01_only=False, results=results
                )

        self.assertEqual(results[0]["dxy_calc_ecb"], 101.25)
        self.assertEqual(results[0]["fear_greed"], 30)
        self.assertEqual(results[0]["fear_greed_label"], "Fear")


class ExecutionAndPushContractTests(unittest.TestCase):
    def test_close_receipt_always_carries_dispatched_cycle_id(self):
        cycle_id = "2026-07-27T21:45"
        receipt_context = {
            "cycle_id": cycle_id,
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "decision_card": {
                "direction_evidence": ["close receipt regression"],
                "opposing_evidence": ["no open position may remain"],
                "execution_conditions": {"status": "test fixture"},
                "invalidation_point": {"condition": "context mismatch"},
                "risk_reward": {"summary": "no order expected"},
                "portfolio_impact": {"summary": "no position change expected"},
                "historical_experience": {
                    "matched_wins": [],
                    "matched_losses": [],
                    "missed_opportunities": [],
                    "usage": "none",
                    "reason": "cycle propagation regression only",
                },
                "agent_judgement": "preserve dispatched cycle identity",
                "reference_overrides": [],
            },
        }
        with mock.patch.object(
            order_executor, "fetch_open_positions", return_value=[]
        ):
            receipt = order_executor.close_position(
                "WLD-USDT-SWAP",
                profile="live",
                pos_side="short",
                db_root=Path(_project_path('db')),
                cycle_id=cycle_id,
                receipt_context=receipt_context,
            )
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["note"], "no_open_position")
        self.assertEqual(receipt["cycle_id"], cycle_id)

    def test_oversized_push_keeps_every_required_section(self):
        long_text = "超长决策证据" * 180
        positions = [
            {
                "profile": "live" if index < 10 else "demo",
                "symbol": f"TEST{index}-USDT-SWAP",
                "side": "long" if index % 2 == 0 else "short",
                "sz": 100 + index,
                "avgPx": 123.45 + index,
                "lev": 5,
                "margin": 25.0,
                "upl": 1.0,
            }
            for index in range(14)
        ]
        payload = {
            "cycle_id": "TEST-2026-07-27T22:00",
            "cycle_count": 1,
            "cycle_duration_s": 10,
            "hhmm": "22:00",
            "action_taken": "OPEN_SHORT",
            "symbol": "TEST",
            "action_summary": long_text,
            "assets": {
                "live": {"equity": 1000, "availBal": 700, "pnl": 1},
                "demo": {"equity": 70000, "availBal": 60000, "pnl": 2},
            },
            "positions": positions,
            "risk": {
                "margin_pct": 2.5,
                "lev": 5,
                "side_pct": 50,
                "position_count": 10,
                "status": "PASS",
            },
            "market": {
                "btc": 65000,
                "btc_chg24h": 1.2,
                "eth": 1900,
                "eth_chg24h": 2.3,
                "regime": "range",
                "dxy": 120,
            },
            "decision": {
                "decision_protocol": "decision_card_v1",
                "summary": long_text,
                "reason": long_text,
                "decision_card": {
                    "direction_evidence": [long_text],
                    "opposing_evidence": [long_text],
                    "execution_conditions": {"status": long_text},
                    "invalidation_point": {"condition": long_text},
                    "risk_reward": {"rr": long_text},
                    "portfolio_impact": {"summary": long_text},
                    "historical_experience": {
                        "matched_wins": [],
                        "matched_losses": [],
                        "missed_opportunities": [],
                        "usage": "partial",
                        "reason": long_text,
                    },
                },
            },
            "execution": {
                "result": long_text,
                "db_rows_live": 1,
                "db_rows_demo": 0,
            },
            "timeline": {"next_hh01_min": 46, "next_review_time": "08:05"},
            "exceptions": [{"name": "test", "detail": long_text}],
            "is_hh01": True,
            "macro": {
                "enabled": True,
                "dxy": 120,
                "dxy_d1": -0.08,
                "dxy_calc_ecb": 101.34,
                "dxy_calc_ecb_d1": -0.08,
                "fear_greed": 30,
                "fear_greed_label": "Fear",
                "btc_etf_net_flow_usd": "-240.1M provisional",
                "btc_etf_flow_status": "provisional_single_source",
                "btc_etf_flow_as_of": "2026-07-24",
            },
        }
        patches = (
            mock.patch.object(
                render_push_report, "authoritative_cycle_count", return_value=None
            ),
            mock.patch.object(
                render_push_report,
                "authoritative_cycle_duration",
                return_value=None,
            ),
            mock.patch.object(
                render_push_report, "authoritative_equity", return_value=None
            ),
            mock.patch.object(
                render_push_report, "authoritative_cum_pnl", return_value=None
            ),
            mock.patch.object(
                render_push_report,
                "authoritative_position_count",
                return_value=None,
            ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            rendered = render_push_report.render(payload)
        validation = validate_push_format.validate(rendered["content"])
        self.assertLessEqual(
            rendered["char_count"], render_push_report.MAX_CONTENT_CHARS
        )
        self.assertTrue(validation["ok"], validation)

    def test_reconcile_prefers_matching_execution_journal_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            journal_dir = db_root / "journal"
            journal_dir.mkdir(parents=True)
            db_path = db_root / "live_trades.db"
            db_path.touch()
            journal_record = {
                "ts": "2026-07-27 21:53:38",
                "action_taken": "CLOSE_SHORT",
                "trade": {
                    "symbol": "WLD-USDT-SWAP",
                    "action": "close",
                    "side": "short",
                    "ordId": "test-order-001",
                    "reason": "Agent 主动平仓",
                    "raw": {"ord_ids": ["test-order-001"]},
                },
            }
            (journal_dir / "exec_live.jsonl").write_text(
                json.dumps(journal_record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            matched = reconcile_exchange_closes.find_journal_close(
                db_path,
                "live",
                "WLD-USDT-SWAP",
                ["test-order-001"],
            )

        self.assertIsNotNone(matched)
        self.assertEqual(matched["line_no"], 1)
        self.assertEqual(matched["trade"]["reason"], "Agent 主动平仓")


class ScopeAndSourceRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(
            (ROOT / "collectors" / "sources" / "registry.json").read_text(
                encoding="utf-8"
            )
        )
        cls.sources = {row["id"]: row for row in cls.registry["sources"]}

    def test_mx_shared_daily_budget_is_72(self):
        mx = self.sources["mx_search"]
        geo = self.sources["geo_political"]
        calls = 1440 // mx["poll_interval_min"]
        calls += (1440 // geo["poll_interval_min"]) * 4
        self.assertEqual(mx["poll_interval_min"], 60)
        self.assertEqual(calls, 72)
        self.assertTrue(news_collect._source_due(mx, "2026-07-27T01:00"))
        self.assertFalse(news_collect._source_due(mx, "2026-07-27T01:30"))
        self.assertTrue(news_collect._source_due(geo, "2026-07-27T02:00"))
        self.assertFalse(news_collect._source_due(geo, "2026-07-27T01:00"))

    def test_public_macro_sources_use_independent_observation_dates(self):
        timestamps = source_freshness._macro_source_timestamps(
            "2026-07-26 23:00:00",
            public_dates={
                "macro_dxy_calc_ecb": "2026-07-24",
                "macro_etf_flow": "2026-07-24",
                "macro_fear_greed": "2026-07-27",
            },
        )
        self.assertEqual(
            timestamps["macro_dxy_calc_ecb"], "2026-07-24 23:59:59"
        )
        self.assertEqual(timestamps["macro_etf_flow"], "2026-07-24 23:59:59")
        self.assertEqual(
            timestamps["macro_fear_greed"], "2026-07-27 23:59:59"
        )
        self.assertTrue(self.sources["macro_fear_greed"]["enabled"])
        self.assertTrue(self.sources["macro_etf_flow"]["enabled"])
        self.assertTrue(self.sources["macro_dxy_calc_ecb"]["enabled"])
        self.assertTrue(self.sources["macro_btc_mcap_change"]["enabled"])

    def test_dxy_uses_its_source_as_of_not_shared_row_time(self):
        timestamps = source_freshness._macro_source_timestamps(
            "2026-07-27 21:00:00",
            json.dumps({"dxy": {"source": "fred", "source_as_of": "2026-07-17"}}),
        )
        self.assertEqual(
            timestamps["macro_dxy_vix_spx"], "2026-07-17 23:59:59"
        )

    def test_okx_first_and_authoritative_supplement_contract(self):
        self.assertTrue(self.sources["okx_top_long_short"]["enabled"])
        self.assertEqual(
            self.sources["okx_top_long_short"]["native_cadence"], "hourly"
        )
        self.assertTrue(self.sources["macro_economic_calendar"]["enabled"])
        supplement = self.sources["x_authoritative_supplement"]
        self.assertTrue(supplement["enabled"])
        self.assertEqual(supplement["adapter"], "EXTERNAL_SCOUT")
        self.assertEqual(supplement["native_cadence"], "daily")
        scout = (ROOT / "agents" / "news_scout.md").read_text(encoding="utf-8")
        self.assertIn("OKX CLI 专用结构化接口 > X 官方/权威账号", scout)
        self.assertIn("verification_status=cross_checked", scout)
        self.assertIn("Alternative.me API 直采", scout)
        self.assertIn("DXY 计算值已由 ECB 官方汇率直采", scout)
        macro = self.sources["macro_dxy_vix_spx"]
        self.assertIn("DTWEXBGS", macro["endpoint"])
        self.assertIn("非ICE DXY", macro["note"])
        inspection = (ROOT / "scripts" / "query_state.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("BTC_ETF_NET_FLOW_USD", inspection)
        self.assertNotIn(" | ETF={etf}", inspection)
        rendered = (ROOT / "scripts" / "render_push_report.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("USD_BROAD(DTWEXBGS)", rendered)
        self.assertNotIn("BTC ETF proxy", rendered)

    def test_all_linear_usdt_swaps_are_in_scope(self):
        live = (ROOT / "agents" / "live_trader.md").read_text(encoding="utf-8")
        demo = (ROOT / "agents" / "demo_trader.md").read_text(encoding="utf-8")
        self.assertIn("无资产类别排除", live)
        self.assertIn("无资产类别排除", demo)
        self.assertNotIn("排除股票代币", live)


if __name__ == "__main__":
    unittest.main()
