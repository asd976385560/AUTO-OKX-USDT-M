# -*- coding: utf-8 -*-
"""Regression coverage for the 2026-08-11 judgment/data-truth hardening."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from collectors import news_writer
from core import ev_calculator
from core import news_context
from core.lib import _okxorder
from scripts import apply_path_metrics_schema as path_metrics
from scripts import decision_briefing
from scripts import experience_summary


CST = timezone(timedelta(hours=8))


class ExperienceSummaryV2Tests(unittest.TestCase):
    def test_refresh_removes_legacy_free_text_and_versions_the_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "account.db")
            try:
                con.execute(
                    "CREATE TABLE trade_experiences("
                    "id INTEGER PRIMARY KEY,status TEXT,regime TEXT,side TEXT,"
                    "pnl_pct REAL,is_gross_profit_close INTEGER,hold_hours REAL,"
                    "raw TEXT,experience_summary TEXT,"
                    "experience_summary_version INTEGER)"
                )
                raw = {"decision_card": {"historical_experience": {
                    "usage": "partial",
                    "reason": "adopt n=7 hit_1R confirmed by old prose",
                }}}
                con.execute(
                    "INSERT INTO trade_experiences VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (1, "closed", "trend_up", "short", -1.25, 0, 3.5,
                     json.dumps(raw), "hit_1R miss n=7", 1),
                )
                con.commit()
            finally:
                con.close()

            result = experience_summary.fill(
                root, apply=True, refresh_all_closed=True)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["filled"], 1)
            con = sqlite3.connect(root / "account.db")
            try:
                summary, version = con.execute(
                    "SELECT experience_summary,experience_summary_version "
                    "FROM trade_experiences WHERE id=1"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(version, 2)
            self.assertIn("gross_loss", summary)
            self.assertIn("history=partial", summary)
            lowered = summary.lower()
            self.assertNotIn("hit_1r", lowered)
            self.assertNotIn("n=7", lowered)
            self.assertNotIn("confirmed by old prose", lowered)

    def test_missing_version_column_fails_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "account.db")
            try:
                con.execute(
                    "CREATE TABLE trade_experiences("
                    "id INTEGER,status TEXT,regime TEXT,side TEXT,pnl_pct REAL,"
                    "is_gross_profit_close INTEGER,hold_hours REAL,raw TEXT,"
                    "experience_summary TEXT)"
                )
                con.commit()
            finally:
                con.close()
            result = experience_summary.fill(root, apply=True)
            self.assertFalse(result["ok"], result)
            self.assertIn("experience_summary_version", result["error"])


class PathMetricV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE kline_cache(symbol TEXT,tf TEXT,ts TEXT,h REAL,l REAL)"
        )

    def tearDown(self) -> None:
        self.con.close()

    def _bar(self, cst: str, high: float, low: float) -> None:
        dt = datetime.strptime(cst, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        ts = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.con.execute(
            "INSERT INTO kline_cache VALUES('BTC-USDT-SWAP','15m',?,?,?)",
            (ts, high, low),
        )

    def test_exact_boundaries_allow_full_and_proven_one_r(self) -> None:
        self._bar("2026-08-10 10:15:00", 101.0, 99.0)
        self._bar("2026-08-10 10:30:00", 106.0, 98.0)
        self._bar("2026-08-10 10:45:00", 104.0, 94.0)
        result = path_metrics.compute_path_metrics(
            self.con, "BTC-USDT-SWAP", "long", 100.0, 95.0, 1000.0,
            "2026-08-10 10:15:00", "2026-08-10 11:00:00", 60.0,
        )
        self.assertEqual(result["path_metric_version"], 2)
        self.assertEqual(result["path_coverage"], "full")
        self.assertAlmostEqual(result["mfe_r"], 1.2)
        self.assertAlmostEqual(result["mae_r"], 1.2)
        self.assertEqual(result["ever_hit_1r"], 1)

    def test_boundary_partial_never_turns_unknown_into_false(self) -> None:
        self._bar("2026-08-10 10:15:00", 102.0, 99.0)
        self._bar("2026-08-10 10:30:00", 103.0, 98.0)
        self._bar("2026-08-10 10:45:00", 104.0, 97.0)
        result = path_metrics.compute_path_metrics(
            self.con, "BTC-USDT-SWAP", "long", 100.0, 95.0, 1000.0,
            "2026-08-10 10:07:00", "2026-08-10 11:02:00", -10.0,
        )
        self.assertEqual(result["path_coverage"], "partial_boundary:1.00")
        self.assertLess(result["mfe_r"], 1.0)
        self.assertIsNone(result["ever_hit_1r"])

    def test_adverse_and_favorable_excursions_are_nonnegative_for_short(self) -> None:
        self._bar("2026-08-10 10:15:00", 104.0, 94.0)
        result = path_metrics.compute_path_metrics(
            self.con, "BTC-USDT-SWAP", "short", 100.0, 105.0, 1000.0,
            "2026-08-10 10:15:00", "2026-08-10 10:30:00", 0.0,
        )
        self.assertGreaterEqual(result["mfe_r"], 0.0)
        self.assertGreaterEqual(result["mae_r"], 0.0)
        self.assertAlmostEqual(result["mfe_r"], 1.2)
        self.assertAlmostEqual(result["mae_r"], 0.8)


class NewsTimeSemanticsV2Tests(unittest.TestCase):
    def test_only_allowlisted_primary_source_url_is_retained(self) -> None:
        accepted = news_writer.normalize_item({
            "title": "filing", "source": "secondary",
            "url": "https://example.com/story",
            "primary_source_url": "https://www.sec.gov/Archives/form-rw.htm",
        })
        rejected = news_writer.normalize_item({
            "title": "post", "source": "social",
            "url": "https://x.com/example/status/1",
            "primary_source_url": "https://random-blog.example/repost",
        })
        self.assertEqual(
            accepted["primary_source_url"],
            "https://www.sec.gov/Archives/form-rw.htm",
        )
        self.assertIsNone(rejected["primary_source_url"])

    def test_yearless_future_date_is_scheduled_not_rejected(self) -> None:
        self.assertEqual(
            news_writer.extract_event_date(
                "Protocol vote scheduled for Aug 20", "2026-08-11 12:00:00"),
            "2026-08-20",
        )
        status, _ = decision_briefing.catalyst_freshness(
            "2026-08-20", datetime(2026, 8, 11, 12, tzinfo=CST))
        self.assertEqual(status, "scheduled")

    def test_old_event_remains_stale_when_first_seen_is_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "news.db")
            try:
                con.execute(
                    "CREATE TABLE news_items("
                    "id INTEGER,ts TEXT,ingested_at TEXT,severity TEXT,symbol TEXT,"
                    "title TEXT,event_occurred_at TEXT,event_time_confidence TEXT,"
                    "first_seen_at TEXT,source_grade TEXT,primary_source_url TEXT,"
                    "event_key TEXT,news_time_version INTEGER)"
                )
                con.execute(
                    "INSERT INTO news_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (1, "2026-08-10 11:16:00", "2026-08-10 11:16:00", "high",
                     "DOT-USDT-SWAP", "old filing relayed now", "2026-08-07",
                     "extracted_title", "2026-08-10 11:16:00", "secondary",
                     None, "ev-dot", 2),
                )
                con.commit()
            finally:
                con.close()
            context = news_context.build_news_context(
                root, "2026-08-10T11:20", window_hours=6)
            self.assertEqual(len(context["items"]), 1)
            item = context["items"][0]
            self.assertEqual(item["catalyst_freshness"], "stale")
            self.assertEqual(item["first_seen_at"], "2026-08-10 11:16:00")
            self.assertEqual(item["news_time_version"], 2)

    def test_cross_source_event_key_uses_structured_event_identity(self) -> None:
        kwargs = dict(
            dedupe_hash="a" * 32, primary_source_url=None,
            event_date="2026-08-07", symbols=["DOT-USDT-SWAP"],
            tags=["filing", "withdrawal"],
        )
        first = news_writer.event_key_for(
            url="https://example.com/story-a", **kwargs)
        second = news_writer.event_key_for(
            url="https://another.example/story-b", **kwargs)
        self.assertEqual(first, second)


class ExperienceCalibrationV2Tests(unittest.TestCase):
    def test_usage_hold_regime_and_asset_groups_are_structured_only(self) -> None:
        rows = [
            {
                "symbol": "BTC-USDT-SWAP", "side": "short",
                "cycle_id": "2026-08-01T00:00", "regime": "trend_up",
                "pnl_pct": -2.0, "realized_pnl": -20.0,
                "hold_hours": 3.0,
                "raw": json.dumps({"decision_card": {
                    "historical_experience": {"usage": "adopt"}}}),
                "experience_vector": json.dumps({
                    "v": 2, "features": {"asset_class": "crypto"}}),
            },
            {
                "symbol": "ETH-USDT-SWAP", "side": "short",
                "cycle_id": "2026-08-02T00:00", "regime": "trend_up",
                "pnl_pct": -1.0, "realized_pnl": -10.0,
                "hold_hours": 8.0,
                "raw": json.dumps({"decision_card": {
                    "historical_experience": {"usage": "adopt"}}}),
                "experience_vector": json.dumps({
                    "v": 2, "features": {"asset_class": "crypto"}}),
            },
            {
                "symbol": "AAOI-USDT-SWAP", "side": "long",
                "cycle_id": "2026-08-03T00:00", "regime": "range",
                "pnl_pct": 2.0, "realized_pnl": 5.0,
                "hold_hours": 55.0,
                "raw": json.dumps({"decision_card": {
                    "historical_experience": {"usage": "none"}}}),
                "experience_vector": None,
            },
            {
                "symbol": "CL-USDT-SWAP", "side": "long",
                "cycle_id": "2026-08-04T00:00", "regime": "range",
                "pnl_pct": 1.0, "realized_pnl": 2.0,
                "hold_hours": 30.0,
                # Narrative mentions adopt, but no structured usage: stay unknown.
                "raw": json.dumps({"reason": "history usage=adopt"}),
                "experience_vector": None,
            },
        ]
        result = decision_briefing.experience_calibration(
            rows,
            {"AAOI-USDT-SWAP": "tokenized_stock",
             "CL-USDT-SWAP": "commodity"},
            {("2026-08-01T00:00", "BTC-USDT-SWAP"): "none"},
        )
        self.assertEqual(result["sample_n"], 4)
        self.assertEqual(result["history_usage"]["adopt"]["n"], 1)
        self.assertEqual(
            result["history_usage"]["adopt"]["win_rate_pct"], 0.0)
        self.assertEqual(result["history_usage"]["none"]["n"], 2)
        self.assertEqual(result["history_usage"]["none"]["wins"], 1)
        self.assertEqual(result["history_usage"]["unknown"]["n"], 1)
        self.assertEqual(
            result["history_usage"]["adopt"]["realized_pnl_sum_usdt"],
            -10.0,
        )
        self.assertEqual(
            result["history_usage_source_counts"],
            {"analysis_signal": 1, "trade_receipt": 2, "unknown": 1},
        )
        self.assertEqual(result["hold_bucket"]["<4h"]["n"], 1)
        self.assertEqual(result["hold_bucket"][">=48h"]["n"], 1)
        self.assertEqual(result["asset_class"]["crypto"]["n"], 2)
        self.assertEqual(result["asset_class"]["tokenized_stock"]["n"], 1)
        self.assertEqual(result["asset_class_current_map_fallback_n"], 2)


class EvOverrideV2Tests(unittest.TestCase):
    @staticmethod
    def _card(p_claim: float) -> dict:
        def summary(scope: str) -> dict:
            return {"scope": scope, "n": 0, "wins": 0, "losses": 0}
        cross = {"scope": "cross_symbol_similar", "n": 10,
                 "wins": 4, "losses": 6}
        return {
            "risk_reward": {
                "entry": 100.0, "stop": 105.0, "target": 95.5,
                "rr": 0.9,
                "ev_override": {"reason": "event-specific repricing",
                                "p_win_claim": p_claim},
            },
            "historical_experience": {"evidence_contract": {
                "summaries": {
                    "exact_setup": summary("same_symbol_side_action_regime"),
                    "same_symbol_similar": summary("same_symbol_similar"),
                    "cross_symbol_similar": cross,
                }
            }},
        }

    def test_override_probability_must_improve_on_baseline(self) -> None:
        _block, errors = ev_calculator.build_ev_check(
            self._card(0.40), "short")
        self.assertTrue(any("必须高于" in item for item in errors), errors)

    def test_still_negative_claim_is_explicitly_recorded(self) -> None:
        # 45% 高于历史 40%，但仍低于约 53% 盈亏平衡线；允许判断自由，必须留痕。
        block, errors = ev_calculator.build_ev_check(
            self._card(0.45), "short")
        self.assertEqual(errors, [], errors)
        self.assertLess(block["claim_ev_r"], 0)
        self.assertTrue(block["accepts_negative_ev"])


class OptionalTakeProfitV2Tests(unittest.TestCase):
    def test_market_open_rejects_combined_tp_sl_before_cli(self) -> None:
        with mock.patch.object(_okxorder, "is_dryrun", return_value=False), \
             mock.patch.object(_okxorder, "_call") as call:
            result = _okxorder.place_market_open(
                "BTC-USDT-SWAP", "long", 1.0, "live",
                sl_trigger_px=95.0, tp_trigger_px=110.0)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "combined_tp_sl_unsupported_use_independent_tp")
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
