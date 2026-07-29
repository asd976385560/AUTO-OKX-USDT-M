# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "collectors"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import public_macro as pm  # noqa: E402


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
        self.assertIsNone(snap["etf_provisional"])
        self.assertIsNone(snap["etf_conflict"])
        self.assertEqual(
            pm.source_dates(self.con)["macro_etf_flow"], "2026-07-24"
        )

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
        self.assertIsNone(snap["etf_provisional"])

    def test_newer_conflict_supersedes_older_confirmed_value(self):
        pm.upsert_observations(
            self.con,
            [
                {
                    "metric": pm.METRIC_BTC_ETF,
                    "observation_date": "2026-07-24",
                    "source": pm.SOURCE_ETF_CONSENSUS,
                    "status": "cross_checked",
                    "value": 10_000_000,
                },
                {
                    "metric": pm.METRIC_BTC_ETF,
                    "observation_date": "2026-07-25",
                    "source": pm.SOURCE_FARSIDE,
                    "status": "source_reported",
                    "value": 20_000_000,
                },
                {
                    "metric": pm.METRIC_BTC_ETF,
                    "observation_date": "2026-07-25",
                    "source": pm.SOURCE_SOSOVALUE,
                    "status": "source_reported",
                    "value": 40_000_000,
                },
            ],
        )
        pm.reconcile_etf_consensus(self.con)

        snap = pm.latest_snapshot(self.con)
        self.assertIsNone(snap["etf_confirmed"])
        self.assertIsNone(snap["etf_provisional"])
        self.assertEqual(
            snap["etf_conflict"]["observation_date"], "2026-07-25"
        )
        self.assertEqual(
            pm.source_dates(self.con)["macro_etf_flow"], "2026-07-25"
        )

    def test_newer_single_source_supersedes_older_confirmed_value(self):
        pm.upsert_observations(
            self.con,
            [
                {
                    "metric": pm.METRIC_BTC_ETF,
                    "observation_date": "2026-07-24",
                    "source": pm.SOURCE_ETF_CONSENSUS,
                    "status": "cross_checked",
                    "value": 10_000_000,
                },
                {
                    "metric": pm.METRIC_BTC_ETF,
                    "observation_date": "2026-07-25",
                    "source": pm.SOURCE_FARSIDE,
                    "status": "source_reported",
                    "value": 20_000_000,
                },
            ],
        )

        snap = pm.latest_snapshot(self.con)
        self.assertIsNone(snap["etf_confirmed"])
        self.assertIsNone(snap["etf_conflict"])
        self.assertEqual(
            snap["etf_provisional"]["observation_date"], "2026-07-25"
        )
        self.assertEqual(
            pm.source_dates(self.con)["macro_etf_flow"], "2026-07-25"
        )

    def test_corrected_same_day_sources_replace_conflict_with_confirmation(self):
        rows = [
            {
                "metric": pm.METRIC_BTC_ETF,
                "observation_date": "2026-07-25",
                "source": pm.SOURCE_FARSIDE,
                "status": "source_reported",
                "value": 20_000_000,
            },
            {
                "metric": pm.METRIC_BTC_ETF,
                "observation_date": "2026-07-25",
                "source": pm.SOURCE_SOSOVALUE,
                "status": "source_reported",
                "value": 40_000_000,
            },
        ]
        pm.upsert_observations(self.con, rows)
        pm.reconcile_etf_consensus(self.con)
        self.assertIsNotNone(pm.latest_snapshot(self.con)["etf_conflict"])

        rows[1]["value"] = 20_100_000
        pm.upsert_observations(self.con, [rows[1]])
        pm.reconcile_etf_consensus(self.con)

        snap = pm.latest_snapshot(self.con)
        self.assertEqual(snap["etf_confirmed"]["value"], 20_000_000)
        self.assertIsNone(snap["etf_conflict"])
        self.assertIsNone(snap["etf_provisional"])

    def test_newer_observation_date_beats_later_collection_timestamp(self):
        pm.upsert_observations(
            self.con,
            [
                {
                    "metric": pm.METRIC_BTC_ETF,
                    "observation_date": "2026-07-24",
                    "source": pm.SOURCE_ETF_CONSENSUS,
                    "collected_at": "2026-07-27T00:00:00Z",
                    "status": "cross_checked",
                    "value": 10_000_000,
                },
                {
                    "metric": pm.METRIC_BTC_ETF,
                    "observation_date": "2026-07-25",
                    "source": pm.SOURCE_FARSIDE,
                    "collected_at": "2026-07-26T00:00:00Z",
                    "status": "source_reported",
                    "value": 20_000_000,
                },
            ],
        )

        snap = pm.latest_snapshot(self.con)
        self.assertIsNone(snap["etf_confirmed"])
        self.assertEqual(
            snap["etf_provisional"]["observation_date"], "2026-07-25"
        )

    def test_no_etf_rows_return_empty_mutually_exclusive_state(self):
        state = pm.latest_etf_state(self.con)
        self.assertEqual(
            state,
            {
                "observation_date": None,
                "etf_confirmed": None,
                "etf_provisional": None,
                "etf_conflict": None,
            },
        )
        snap = pm.latest_snapshot(self.con)
        self.assertIsNone(snap["etf_confirmed"])
        self.assertIsNone(snap["etf_provisional"])
        self.assertIsNone(snap["etf_conflict"])
        self.assertIsNone(pm.source_dates(self.con)["macro_etf_flow"])


if __name__ == "__main__":
    unittest.main()
