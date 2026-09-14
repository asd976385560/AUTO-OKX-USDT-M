# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (
    ROOT / "scripts",
    ROOT / "collectors",
    ROOT / "collectors" / "sources",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import public_macro as pm  # noqa: E402
import _registry as source_registry  # noqa: E402


class PublicMacroTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(pm.TABLE_DDL)

    def tearDown(self):
        self.con.close()

    def test_alternative_payload_is_normalized(self):
        rows = pm.parse_alternative_payload(
            {
                "data": [
                    {
                        "value": "30",
                        "value_classification": "Fear",
                        "timestamp": "1785110400",
                    }
                ]
            },
            collected_at="2026-07-27T00:00:00Z",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metric"], pm.METRIC_FEAR_GREED)
        self.assertEqual(rows[0]["value"], 30.0)
        self.assertEqual(rows[0]["label"], "Fear")
        self.assertEqual(rows[0]["status"], "official_primary")

    def test_ecb_formula_uses_ice_index_constant(self):
        # 六个交叉汇率均为 1 时，几何乘积为 1，结果必须等于 ICE 公布常数。
        rates = {currency: 1.0 for currency in pm.REQUIRED_ECB_CURRENCIES}
        self.assertAlmostEqual(
            pm.calculate_dxy_from_ecb(rates), pm.ICE_DXY_CONSTANT, places=8
        )

    def test_ecb_xml_requires_all_six_rates(self):
        xml = """<?xml version="1.0"?>
        <Envelope><Cube><Cube time="2026-07-23">
          <Cube currency="USD" rate="1.1392"/>
          <Cube currency="JPY" rate="186.23"/>
          <Cube currency="GBP" rate="0.85318"/>
          <Cube currency="CAD" rate="1.5721"/>
          <Cube currency="SEK" rate="10.932"/>
          <Cube currency="CHF" rate="0.9185"/>
        </Cube></Cube></Envelope>"""
        rows = pm.ecb_rows(xml, "2026-07-27T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observation_date"], "2026-07-23")
        self.assertFalse(rows[0]["raw"]["is_ice_official_quote"])

    def test_ecb_fetch_honors_hourly_timeout_budget(self):
        xml = """<Envelope><Cube><Cube time="2026-08-17">
          <Cube currency="USD" rate="1.1593"/>
          <Cube currency="JPY" rate="184.34"/>
          <Cube currency="GBP" rate="0.8532"/>
          <Cube currency="CAD" rate="1.605"/>
          <Cube currency="SEK" rate="10.98"/>
          <Cube currency="CHF" rate="0.938"/>
        </Cube></Cube></Envelope>"""
        response = mock.Mock(text=xml)
        response.raise_for_status.return_value = None
        client = mock.Mock()
        client.get.return_value = response

        rows = pm.fetch_ecb_dxy(client, timeout=12.0)

        self.assertEqual(len(rows), 1)
        self.assertEqual(client.get.call_args.kwargs["timeout"], 12.0)

    def test_single_source_etf_is_provisional_not_consensus(self):
        evidence = {
            "metric": "btc_spot_etf_daily_net_flow",
            "as_of": "2026-07-24",
            "unit": "USD",
            "source_values": [
                {"source": "Farside Investors", "value": -240100000}
            ],
        }
        rows = pm.evidence_rows([{"raw": json.dumps(evidence)}])
        pm.upsert_observations(self.con, rows)
        result = pm.reconcile_etf_consensus(self.con)
        snap = pm.latest_snapshot(self.con)
        self.assertEqual(result["cross_checked"], 0)
        self.assertIsNone(snap["etf_confirmed"])
        self.assertEqual(snap["etf_provisional"]["value"], -240100000)

    def test_variant_etf_evidence_is_forward_only_and_normalized(self):
        evidence = {
            "metric": "btc_spot_etf_daily_net_flow",
            "trade_date": "2026-08-18",
            "unit": "USD_million",
            "verification_status": "cross_checked",
            "sources": [
                {
                    "name": "Farside",
                    "value_usd_million": 101.7,
                    "url": "https://example.test/farside",
                },
                {
                    "name": "SoSoValue",
                    "value_usd_million": 98.85,
                    "url": "https://example.test/sosovalue",
                },
            ],
        }
        historical = pm.evidence_rows([{
            "raw": json.dumps(evidence),
            "evidence_ingested_at": "2026-08-10 08:27:35",
        }])
        forward = pm.evidence_rows([{
            "raw": json.dumps(evidence),
            "evidence_ingested_at": "2026-08-18 03:00:00",
        }])

        self.assertEqual(historical, [])
        self.assertEqual(len(forward), 2)
        values = {row["source"]: row["value"] for row in forward}
        self.assertEqual(values[pm.SOURCE_FARSIDE], 101_700_000.0)
        self.assertEqual(values[pm.SOURCE_SOSOVALUE], 98_850_000.0)

    def test_variant_etf_evidence_accepts_trading_day_and_value_usd(self):
        evidence = {
            "metric": "btc_spot_etf_daily_net_flow",
            "trading_day": "2026-08-18",
            "unit": "USD",
            "sources": [
                {"name": "Farside", "value_usd": 211_500_000},
                {"name": "SoSoValue", "value_usd": 211_490_000},
            ],
        }
        rows = pm.evidence_rows([{
            "raw": json.dumps(evidence),
            "evidence_ingested_at": "2026-08-18T03:00:00+08:00",
        }])
        pm.upsert_observations(self.con, rows)
        result = pm.reconcile_etf_consensus(self.con)
        snapshot = pm.latest_snapshot(self.con)

        self.assertEqual(result["cross_checked"], 1)
        self.assertEqual(snapshot["etf_confirmed"]["value"], 211_500_000)

    def test_two_matching_etf_sources_create_consensus(self):
        rows = [
            {
                "metric": pm.METRIC_BTC_ETF,
                "observation_date": "2026-07-24",
                "source": pm.SOURCE_FARSIDE,
                "status": "source_reported",
                "value": -240100000,
                "unit": "USD",
            },
            {
                "metric": pm.METRIC_BTC_ETF,
                "observation_date": "2026-07-24",
                "source": pm.SOURCE_SOSOVALUE,
                "status": "source_reported",
                "value": -240000000,
                "unit": "USD",
            },
        ]
        pm.upsert_observations(self.con, rows)
        result = pm.reconcile_etf_consensus(self.con)
        snap = pm.latest_snapshot(self.con)
        self.assertEqual(result["cross_checked"], 1)
        self.assertEqual(snap["etf_confirmed"]["value"], -240100000)
        self.assertEqual(snap["etf_confirmed"]["status"], "cross_checked")

    def test_fed_funds_rows_normalize_and_reach_snapshot(self):
        rows = pm.fed_funds_rows(
            "4.33", "2026-08-11", d1=0.0, collected_at="2026-08-13T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metric"], pm.METRIC_FED_FUNDS)
        self.assertEqual(rows[0]["source"], pm.SOURCE_FRED)
        self.assertEqual(rows[0]["unit"], "percent_annual")
        self.assertEqual(rows[0]["status"], "official_primary")
        pm.upsert_observations(self.con, rows)
        snap = pm.latest_snapshot(self.con)
        self.assertEqual(snap["fed_funds"]["value"], 4.33)
        self.assertEqual(snap["fed_funds"]["observation_date"], "2026-08-11")
        dates = pm.source_dates(self.con)
        self.assertEqual(dates["macro_fed_funds"], "2026-08-11")

    def test_fed_funds_rows_reject_bad_value_or_date(self):
        # 值/日期非法宁缺勿假；越界（负或 >30%）视为坏数据。
        self.assertEqual(pm.fed_funds_rows(None, "2026-08-11"), [])
        self.assertEqual(pm.fed_funds_rows("4.33", None), [])
        self.assertEqual(pm.fed_funds_rows("4.33", "bad-date"), [])
        self.assertEqual(pm.fed_funds_rows("-1", "2026-08-11"), [])
        self.assertEqual(pm.fed_funds_rows("55", "2026-08-11"), [])
        snap = pm.latest_snapshot(self.con)
        self.assertIsNone(snap["fed_funds"])

    def test_fed_funds_registry_preregisters_real_birth_boundary(self):
        registry = json.loads(
            (ROOT / "collectors" / "sources" / "registry.json").read_text(
                encoding="utf-8"))
        source = next(
            row for row in registry["sources"]
            if row.get("id") == "macro_fed_funds")
        self.assertEqual(
            "2026-08-14T04:05:30+08:00",
            source["audit_forward_start_cst"],
        )
        self.assertFalse(source["required"])
        self.assertEqual([], source_registry.validate(registry))

        source["audit_forward_start_cst"] = "2026-08-14T04:05:30Z"
        errors = source_registry.validate(registry)
        self.assertTrue(any(
            "macro_fed_funds audit_forward_start_cst" in error
            for error in errors
        ))

    def test_conflicting_etf_sources_do_not_enter_hard_value(self):
        rows = [
            {
                "metric": pm.METRIC_BTC_ETF,
                "observation_date": "2026-07-24",
                "source": pm.SOURCE_FARSIDE,
                "status": "source_reported",
                "value": -240100000,
            },
            {
                "metric": pm.METRIC_BTC_ETF,
                "observation_date": "2026-07-24",
                "source": pm.SOURCE_SOSOVALUE,
                "status": "source_reported",
                "value": -200000000,
            },
        ]
        pm.upsert_observations(self.con, rows)
        result = pm.reconcile_etf_consensus(self.con)
        snap = pm.latest_snapshot(self.con)
        self.assertEqual(result["conflicts"], 1)
        self.assertIsNone(snap["etf_confirmed"])
        self.assertEqual(snap["etf_conflict"]["status"], "conflict")


class MacroEventsUpsertTests(unittest.TestCase):
    """D1｜``upsert_macro_events`` 是 macro_events 的唯一硬化写入口。

    旧写法是脚本内手写 ``INSERT OR REPLACE``（违反 writer 红线），且回查窗
    采到 actual 之后，下一次未来窗刷新会把同一行的 forecast/previous 整体
    冲回 NULL —— 这正是 ``actual`` 长期 0/77 的一半原因。新写入口语义是
    **只补不抹**：excluded 侧为 NULL/'' 的字段一律保留库内既有值。
    """

    # regime.db 的真实 DDL（db/schema.sql:753），calendar_id 是唯一冲突键。
    DDL = """
    CREATE TABLE macro_events (
        calendar_id TEXT PRIMARY KEY,
        event_ts    TEXT NOT NULL,
        region      TEXT,
        category    TEXT,
        event       TEXT NOT NULL,
        importance  INTEGER,
        forecast    TEXT,
        previous    TEXT,
        actual      TEXT,
        unit        TEXT,
        ref_date    TEXT,
        updated_at  TEXT,
        fetched_at  TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT 'okx_economic_calendar',
        raw         TEXT
    )
    """

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(self.DDL)

    def tearDown(self):
        self.con.close()

    def _row(self, cid="cal-1"):
        return dict(self.con.execute(
            "SELECT * FROM macro_events WHERE calendar_id=?", (cid,)
        ).fetchone())

    def test_new_rows_are_inserted_with_defaults(self):
        written = pm.upsert_macro_events(self.con, [{
            "calendar_id": "cal-1", "event_ts": "2026-08-20 20:30:00",
            "region": "US", "category": "Inflation", "event": "CPI YoY",
            "importance": 3, "forecast": "2.9%", "previous": "3.0%",
        }])
        self.assertEqual(written, 1)
        row = self._row()
        self.assertEqual(row["event"], "CPI YoY")
        self.assertEqual(row["forecast"], "2.9%")
        self.assertIsNone(row["actual"])
        self.assertEqual(row["source"], "okx_economic_calendar")
        self.assertTrue(row["fetched_at"])

    def test_lookback_actual_fills_in_without_wiping_forecast(self):
        """回查窗只带 actual → 补上 actual，forecast/previous 原样保留。"""
        pm.upsert_macro_events(self.con, [{
            "calendar_id": "cal-1", "event_ts": "2026-08-20 20:30:00",
            "region": "US", "category": "Inflation", "event": "CPI YoY",
            "importance": 3, "forecast": "2.9%", "previous": "3.0%",
        }])
        pm.upsert_macro_events(self.con, [{
            "calendar_id": "cal-1", "event_ts": "2026-08-20 20:30:00",
            "event": "CPI YoY", "importance": 3, "actual": "3.1%",
        }])
        row = self._row()
        self.assertEqual(row["actual"], "3.1%")
        self.assertEqual(row["forecast"], "2.9%")
        self.assertEqual(row["previous"], "3.0%")
        self.assertEqual(row["region"], "US")
        self.assertEqual(row["category"], "Inflation")

    def test_future_window_refresh_never_wipes_collected_actual(self):
        """已采到 actual 后，未来窗再刷（actual 为空）不得把它抹掉。

        这是旧 INSERT OR REPLACE 的致命行为，也是本用例存在的全部理由。
        """
        pm.upsert_macro_events(self.con, [{
            "calendar_id": "cal-1", "event_ts": "2026-08-20 20:30:00",
            "event": "CPI YoY", "importance": 3, "actual": "3.1%",
            "forecast": "2.9%",
        }])
        pm.upsert_macro_events(self.con, [{
            "calendar_id": "cal-1", "event_ts": "2026-08-20 20:30:00",
            "event": "CPI YoY", "importance": 3, "actual": "", "forecast": "",
            "previous": None,
        }])
        row = self._row()
        self.assertEqual(row["actual"], "3.1%")
        self.assertEqual(row["forecast"], "2.9%")

    def test_event_ts_and_importance_are_authoritative_on_update(self):
        """改期/降级是真实事实变更，这两列必须跟随最新一次采集。"""
        pm.upsert_macro_events(self.con, [{
            "calendar_id": "cal-1", "event_ts": "2026-08-20 20:30:00",
            "event": "CPI YoY", "importance": 3,
        }])
        pm.upsert_macro_events(self.con, [{
            "calendar_id": "cal-1", "event_ts": "2026-08-21 20:30:00",
            "event": "CPI YoY", "importance": 2,
        }])
        row = self._row()
        self.assertEqual(row["event_ts"], "2026-08-21 20:30:00")
        self.assertEqual(row["importance"], 2)

    def test_rows_missing_identity_are_dropped_not_written_blank(self):
        written = pm.upsert_macro_events(self.con, [
            {"calendar_id": "", "event_ts": "2026-08-20 20:30:00", "event": "X"},
            {"calendar_id": "cal-2", "event_ts": "2026-08", "event": "X"},
            {"calendar_id": "cal-3", "event_ts": "2026-08-20 20:30:00", "event": ""},
        ])
        self.assertEqual(written, 0)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM macro_events").fetchone()[0], 0)

    def test_empty_iterable_is_a_noop(self):
        self.assertEqual(pm.upsert_macro_events(self.con, []), 0)


class SosovalueKeyResolutionTests(unittest.TestCase):
    """2026-08-19：key 解析改走 _http.load_sosovalue_key（env 优先→config.md §4.6b）。

    背景：`collect_public_macro.py` 由 daily_maintenance 以裸 python 起，拿不到
    `run_okx_python.ps1` 注入的 env —— 只靠机器级环境变量会在 cron 侧静默失效。
    """

    SECTION = (
        "### 4.6b SoSoValue（BTC 现货 ETF 日净流 · 官方结构化 API）\n\n"
        "| 项目 | 值 |\n|------|-----|\n"
        "| 数据源 | `POST https://api.sosovalue.xyz/openapi/v2/etf/"
        "historicalInflowChart` |\n"
        "| API Key | SOSO-configkey000000 |\n"
        "| 认证头 | `x-soso-api-key: <key>` |\n"
    )

    def _config(self, body: str) -> Path:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8", newline="\n")
        tmp.write(body)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Path(tmp.name)

    @staticmethod
    def _no_env():
        """清掉 env 键，确保测的是 config.md 回落路径。"""
        patcher = mock.patch.dict("os.environ", {}, clear=False)
        patcher.start()
        import os as _os
        _os.environ.pop("SOSOVALUE_API_KEY", None)
        return patcher

    def test_config_section_key_is_used_when_env_absent(self):
        import _http
        cfg = self._config("# config\n\n" + self.SECTION)
        patcher = self._no_env()
        self.addCleanup(patcher.stop)
        with mock.patch.object(_http, "_CONFIG", cfg):
            self.assertEqual(
                _http.load_sosovalue_key(), "SOSO-configkey000000")

    def test_env_wins_over_config_section(self):
        import _http
        cfg = self._config("# config\n\n" + self.SECTION)
        with mock.patch.object(_http, "_CONFIG", cfg), \
                mock.patch.dict(
                    "os.environ", {"SOSOVALUE_API_KEY": "SOSO-envkey"}):
            self.assertEqual(_http.load_sosovalue_key(), "SOSO-envkey")

    def test_missing_section_returns_empty_and_fetch_skips(self):
        import _http
        cfg = self._config(
            "# config\n\n### 4.1 FRED\n\n| API Key | fredkey |\n")
        patcher = self._no_env()
        self.addCleanup(patcher.stop)
        with mock.patch.object(_http, "_CONFIG", cfg):
            self.assertEqual(_http.load_sosovalue_key(), "")
            # 无 key 时必须静默跳过：不触网、不抛错（collect 侧记 skipped）
            client = mock.Mock()
            self.assertEqual(pm.fetch_sosovalue(client), [])
            client.post.assert_not_called()

    def test_fetch_posts_resolved_key_in_header(self):
        import _http
        cfg = self._config("# config\n\n" + self.SECTION)
        response = mock.Mock()
        response.json.return_value = {
            "code": 0,
            "data": {"list": [{"date": "2026-08-18", "totalNetInflow": 1.5e8}]},
        }
        client = mock.Mock()
        client.post.return_value = response
        patcher = self._no_env()
        self.addCleanup(patcher.stop)
        with mock.patch.object(_http, "_CONFIG", cfg):
            rows = pm.fetch_sosovalue(client)
        headers = client.post.call_args.kwargs["headers"]
        self.assertEqual(headers["x-soso-api-key"], "SOSO-configkey000000")
        self.assertEqual(rows[0]["source"], pm.SOURCE_SOSOVALUE)
        self.assertEqual(rows[0]["observation_date"], "2026-08-18")
        self.assertEqual(rows[0]["status"], "source_reported")

    def test_diagnostic_separates_no_key_from_unparsable_payload(self):
        """0 行必须能自证成因：没 key vs 结构/权限不符（2026-08-19）。"""
        import _http
        patcher = self._no_env()
        self.addCleanup(patcher.stop)

        # ① 没 key：不触网，诊断标 key_resolved=False
        cfg_nokey = self._config("# config\n\n### 4.1 FRED\n\n| API Key | k |\n")
        probe: dict = {}
        with mock.patch.object(_http, "_CONFIG", cfg_nokey):
            client = mock.Mock()
            self.assertEqual(
                pm.fetch_sosovalue(client, diagnostic=probe), [])
        client.post.assert_not_called()
        self.assertIs(probe["key_resolved"], False)

        # ② 有 key 但字段名不符：诊断必须把 first_item_keys 摊出来，
        #    且不得伪装成「没配 key」
        cfg = self._config("# config\n\n" + self.SECTION)
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "code": 0, "msg": "ok",
            "data": [{"tradingDay": "2026-08-18", "netFlow": 1.0}],
        }
        client = mock.Mock()
        client.post.return_value = response
        probe = {}
        with mock.patch.object(_http, "_CONFIG", cfg):
            rows = pm.fetch_sosovalue(client, diagnostic=probe)
        self.assertEqual(rows, [])
        self.assertIs(probe["key_resolved"], True)
        self.assertEqual(probe["parsed_rows"], 0)
        self.assertEqual(probe["data_shape"], "list")
        self.assertEqual(probe["list_len"], 1)
        self.assertEqual(probe["first_item_keys"], ["netFlow", "tradingDay"])
        # 诊断只带结构元信息，绝不带 key
        self.assertNotIn(
            "SOSO-configkey000000", json.dumps(probe, default=str))

    def test_data_as_bare_list_is_parsed(self):
        """2026-08-19 线上实测形状：data 直接是数组（原解析只认 data.list → 恒 0 行）。"""
        import _http
        cfg = self._config("# config\n\n" + self.SECTION)
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "code": 0, "msg": None, "tid": "t", "traceId": "x",
            "data": [
                {"date": "2026-08-18", "totalNetInflow": 1.5e8},
                {"date": "2026-08-17", "totalNetInflow": -2.0e7},
            ],
        }
        client = mock.Mock()
        client.post.return_value = response
        patcher = self._no_env()
        self.addCleanup(patcher.stop)
        probe: dict = {}
        with mock.patch.object(_http, "_CONFIG", cfg):
            rows = pm.fetch_sosovalue(client, diagnostic=probe)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["observation_date"], "2026-08-18")
        self.assertEqual(rows[0]["value"], 1.5e8)
        self.assertEqual(rows[0]["source"], pm.SOURCE_SOSOVALUE)
        self.assertEqual(probe["data_shape"], "list")
        self.assertEqual(probe["parsed_rows"], 2)

    def test_dict_wrapped_shapes_still_parsed(self):
        """旧/变体形状 data={list|records|rows|items:[...]} 仍须解析（不回归）。"""
        for key in ("list", "records", "rows", "items"):
            payload = {
                "code": 0,
                "data": {key: [{"date": "2026-08-18",
                                "totalNetInflow": 1.0e8}]},
            }
            rows = pm.parse_sosovalue_payload(payload)
            self.assertEqual(len(rows), 1, key)
            self.assertEqual(rows[0]["observation_date"], "2026-08-18", key)

    def test_unknown_shapes_yield_nothing_not_garbage(self):
        """取不到数组一律 []（宁缺勿假，不猜数值）。"""
        for payload in (
            {"code": 0, "data": None},
            {"code": 0, "data": "oops"},
            {"code": 0, "data": {"unexpected": {"date": "2026-08-18"}}},
            {"code": 0},
        ):
            self.assertEqual(pm.parse_sosovalue_payload(payload), [])


if __name__ == "__main__":
    unittest.main()
