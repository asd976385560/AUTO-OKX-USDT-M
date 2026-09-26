# -*- coding: utf-8 -*-
"""2026-09-26 对照 V3 similarity 模块优化 V2 经验特征派生的回归覆盖。

覆盖：15m 栅格锚定的严格 24h 窗（非栅格 as_of 也能给数）、字段子集只跑对应
查询、执行包三价推 planned_rr、writer / finder / 特征脚本共用装配、版本分布与
finder 共用 exact-epoch 判定、贴近度的有限性门、Wilson 下界与加权统计、
path_metric_version 单一真源。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import _simutil  # noqa: E402
import apply_path_metrics_schema as path_metrics  # noqa: E402
import experience_features_v2 as features  # noqa: E402
import find_similar_experience  # noqa: E402
import trade_experience_writer  # noqa: E402
from core import instrument_context  # noqa: E402
from core.experience_contract import build_contract, validate_contract  # noqa: E402


UPPER = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)   # 2026-08-31 01:00 CST
UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _market(with_derivatives: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    if with_derivatives:
        connection.execute(
            "CREATE TABLE derivatives(symbol TEXT,ts TEXT,funding_rate REAL)")
    connection.execute(
        "CREATE TABLE kline_cache(symbol TEXT,tf TEXT,ts TEXT,h REAL,l REAL,c REAL)")
    return connection


def _fill_15m(connection: sqlite3.Connection, upper: datetime,
              count: int = 96, prices=(110.0, 90.0, 100.0)) -> None:
    lower = upper - timedelta(hours=24)
    rows = [
        ("BTC-USDT-SWAP", "15m",
         (lower + timedelta(minutes=15 * index)).strftime(UTC_FMT), *prices)
        for index in range(1, count + 1)
    ]
    connection.executemany("INSERT INTO kline_cache VALUES(?,?,?,?,?,?)", rows)


def _fill_trend(connection: sqlite3.Connection, tf: str, count: int,
                upper: datetime, null_at: int | None = None) -> None:
    step = timedelta(hours=1 if tf == "1H" else 4)
    rows = []
    for index in range(count):
        ts = upper - step * (count - 1 - index)
        close = None if index == null_at else 100.0 + index
        rows.append(("BTC-USDT-SWAP", tf, ts.strftime(UTC_FMT), close, close, close))
    connection.executemany("INSERT INTO kline_cache VALUES(?,?,?,?,?,?)", rows)


class StrictWindowGridTests(unittest.TestCase):
    def test_unaligned_as_of_anchors_to_the_last_grid_point(self) -> None:
        connection = _market()
        _fill_15m(connection, UPPER)
        try:
            for as_of in ("2026-08-31 01:07:23", "2026-08-31 01:14:59",
                          "2026-08-31T01:00:00+08:00", "2026-08-30T17:09:41Z"):
                result = features.derive_market_features(
                    connection, "BTC-USDT-SWAP", as_of)
                self.assertEqual(0.2, result["vol_24h_pct"], as_of)
        finally:
            connection.close()

    def test_next_grid_point_requires_its_own_bar(self) -> None:
        connection = _market()
        _fill_15m(connection, UPPER)
        try:
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:15:00")
            self.assertIsNone(result["vol_24h_pct"])
            connection.execute(
                "INSERT INTO kline_cache VALUES(?,?,?,?,?,?)",
                ("BTC-USDT-SWAP", "15m",
                 (UPPER + timedelta(minutes=15)).strftime(UTC_FMT),
                 130.0, 90.0, 100.0))
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:15:00")
            self.assertEqual(0.4, result["vol_24h_pct"])
        finally:
            connection.close()

    def test_all_null_prices_return_none_instead_of_raising(self) -> None:
        connection = _market()
        _fill_15m(connection, UPPER, prices=(None, None, None))
        try:
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:00:00")
        finally:
            connection.close()
        self.assertIsNone(result["vol_24h_pct"])

    def test_unparseable_as_of_yields_all_none(self) -> None:
        connection = _market()
        try:
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "not a time")
        finally:
            connection.close()
        self.assertEqual(
            {key: None for key in features.MARKET_FEATURE_KEYS}, result)


class SubsetAndTrendTests(unittest.TestCase):
    def test_fields_subset_skips_unrequested_queries(self) -> None:
        connection = _market(with_derivatives=False)
        _fill_trend(connection, "4H", 50, UPPER)
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        try:
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:00:00",
                fields=("trend_4h", "unknown_key"))
            self.assertEqual({"trend_4h": 1}, result)
            self.assertEqual(1, len(statements))
            self.assertIn("tf='4H'", statements[0])
            # 全量请求：缺 derivatives 表只让 funding_rate 留空，其它特征照算
            full = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:00:00")
        finally:
            connection.close()
        self.assertIsNone(full["funding_rate"])
        self.assertEqual(1, full["trend_4h"])
        self.assertEqual(len(features.MARKET_FEATURE_KEYS), len(full))

    def test_trend_needs_fifty_finite_closes(self) -> None:
        connection = _market()
        _fill_trend(connection, "1H", 49, UPPER)
        _fill_trend(connection, "4H", 50, UPPER, null_at=10)
        try:
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:00:00")
            self.assertIsNone(result["trend_1h"])
            self.assertIsNone(result["trend_4h"])
            connection.execute("DELETE FROM kline_cache")
            _fill_trend(connection, "1H", 50, UPPER)
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:00:00")
        finally:
            connection.close()
        self.assertEqual(1, result["trend_1h"])

    def test_instrument_context_needs_only_the_4h_trend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = sqlite3.connect(root / "market.db")
            connection.execute(
                "CREATE TABLE kline_cache("
                "symbol TEXT,tf TEXT,ts TEXT,h REAL,l REAL,c REAL)")
            _fill_trend(connection, "4H", 50, UPPER)
            connection.commit()
            connection.close()
            context = instrument_context.build_instrument_context(
                "BTC-USDT-SWAP", "range", "2026-08-31T01:00", root)
        self.assertEqual("trend_up", context["instrument_regime"])
        self.assertEqual("2026-08-31 01:00:00", context["as_of"])


class SharedBuilderTests(unittest.TestCase):
    PACKAGE = {
        "contract": "open_execution_package_v1",
        "entry": 100.0, "stop": 95.0, "target": 110.0, "exit_mode": "fixed_tp",
    }

    def test_planned_rr_prefers_card_then_execution_package(self) -> None:
        self.assertEqual(
            2.0,
            features.planned_rr_from_trade({"open_execution_package": self.PACKAGE}),
        )
        self.assertEqual(
            1.5,
            features.planned_rr_from_trade({
                "decision_card": {"ev_check": {"gross_rr": 1.5}},
                "open_execution_package": self.PACKAGE,
            }),
        )
        geometry_only = {"decision_card": {
            "ev_check": {"gross_rr": float("nan")},
            "risk_reward": {"entry": 100, "stop": 90, "target": 130},
        }}
        self.assertEqual(3.0, features.planned_rr_from_trade(geometry_only))
        self.assertIsNone(features.planned_rr_from_trade({
            "decision_card": {"ev_check": {"gross_rr": True}}}))
        self.assertIsNone(features.planned_rr_from_trade({
            "open_execution_package": {"entry": 100, "stop": 100, "target": 110}}))
        self.assertIsNone(features.planned_rr_from_trade("not a trade"))

    def test_stop_distance_guards(self) -> None:
        self.assertEqual(
            0.05,
            features.stop_distance_pct(
                {"fill_px": "abc", "px": 100.0, "sl_trigger_px": 95.0}),
        )
        self.assertIsNone(features.stop_distance_pct(
            {"fill_px": 100.0, "sl_trigger_px": "nan"}))
        self.assertIsNone(features.stop_distance_pct(
            {"fill_px": True, "sl_trigger_px": 95.0}))
        self.assertIsNone(features.stop_distance_pct(None))

    def test_writer_payload_uses_the_shared_builder(self) -> None:
        trade = {"fill_px": 100.0, "sl_trigger_px": 95.0,
                 "open_execution_package": self.PACKAGE}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            trade_experience_writer, "_DB_ROOT", Path(temporary)
        ), mock.patch(
            "core.asset_class.asset_class_of", return_value="tokenized_stock"
        ):
            payload = trade_experience_writer._v3_vector_payload(
                "AAPL-USDT-SWAP", "long", "open", "range",
                "2026-08-31 01:07:23", trade)
            base = features.experience_base(
                "AAPL-USDT-SWAP", "long", "open", "range", trade)
            base.update(features.market_context(
                "AAPL-USDT-SWAP", "2026-08-31 01:07:23", Path(temporary)))
            expected = features.vector_payload(base)
        self.assertEqual(expected, payload)
        self.assertEqual("tokenized_stock", payload["features"]["asset_class"])
        self.assertEqual(2.0, payload["features"]["planned_rr"])
        self.assertEqual(0.05, payload["features"]["stop_distance_pct"])
        self.assertEqual(0.05, payload["features"]["sl_pct"])
        self.assertEqual(_simutil.FEATURE_EPOCH_V4, payload["feature_epoch"])
        self.assertEqual(
            _simutil.FEATURE_EPOCH_V4, payload["features"]["feature_epoch"])
        self.assertEqual(
            _simutil.FEATURE_EPOCH_V3, payload["features_v3"]["feature_epoch"])

    def test_writer_payload_survives_derivation_failure(self) -> None:
        trade = {"fill_px": 100.0, "sl_trigger_px": 95.0,
                 "open_execution_package": self.PACKAGE}
        with mock.patch.object(
            features, "market_context", side_effect=RuntimeError("boom")
        ):
            payload = trade_experience_writer._v3_vector_payload(
                "BTC-USDT-SWAP", "long", "open", "range",
                "2026-08-31 01:07:23", trade)
        self.assertEqual(4, payload["v"])
        self.assertEqual(0.05, payload["features"]["stop_distance_pct"])
        self.assertEqual(2.0, payload["features"]["planned_rr"])
        self.assertIsNone(payload["features"]["asset_class"])
        self.assertIsNone(payload["features"]["vol_24h_pct"])

    def test_features_for_row_uses_the_same_derivation(self) -> None:
        connection = _market()
        _fill_15m(connection, UPPER)
        connection.execute(
            "INSERT INTO derivatives VALUES(?,?,?)",
            ("BTC-USDT-SWAP", "2026-08-30T16:00:00Z", 0.0002))
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE trade_experiences("
            "id INTEGER PRIMARY KEY,symbol TEXT,side TEXT,action TEXT,"
            "regime TEXT,ts TEXT,raw TEXT)")
        connection.execute(
            "INSERT INTO trade_experiences VALUES(?,?,?,?,?,?,?)",
            (1, "BTC-USDT-SWAP", "long", "open", "range",
             "2026-08-31 01:07:23",
             json.dumps({"fill_px": 100.0, "sl_trigger_px": 95.0,
                         "open_execution_package": self.PACKAGE})))
        row = connection.execute("SELECT * FROM trade_experiences").fetchone()
        with tempfile.TemporaryDirectory() as temporary:
            try:
                derived = features.features_for_row(
                    connection, row, Path(temporary))
            finally:
                connection.close()
        self.assertEqual(4, derived["v"])
        self.assertEqual("crypto", derived["asset_class"])
        self.assertEqual(17, derived["hour_utc"])
        self.assertEqual(0.05, derived["stop_distance_pct"])
        self.assertEqual(2.0, derived["planned_rr"])
        self.assertEqual(0.2, derived["vol_24h_pct"])
        self.assertEqual(0.0002, derived["funding_rate"])


class BackfillClassificationTests(unittest.TestCase):
    @staticmethod
    def _vectors() -> list[str | None]:
        exact = {"v": 3, "feature_epoch": _simutil.FEATURE_EPOCH_V3,
                 "features": _simutil.experience_features_v3({})}
        outer_wrong = {**exact, "feature_epoch": "wrong"}
        inner_wrong = {**exact, "features": {
            **exact["features"], "feature_epoch": "wrong"}}
        return [
            json.dumps({"v": 2, "features": _simutil.experience_features_v2({})}),
            json.dumps(exact),
            json.dumps(outer_wrong),
            json.dumps(inner_wrong),
            json.dumps([0.0] * 10),
            None,
            "{not json",
        ]

    def test_backfill_separates_epoch_mismatch_from_forward_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_root = Path(temporary)
            connection = sqlite3.connect(db_root / "account.db")
            connection.execute(
                "CREATE TABLE trade_experiences("
                "id INTEGER PRIMARY KEY,experience_vector TEXT)")
            connection.executemany(
                "INSERT INTO trade_experiences VALUES(?,?)",
                list(enumerate(self._vectors(), start=1)))
            connection.commit()
            connection.close()
            report = features.backfill(db_root, apply=False)
            refused = features.backfill(db_root, apply=True)
        self.assertTrue(report["ok"])
        self.assertEqual(7, report["total_rows"])
        self.assertEqual(
            {"v1_or_legacy": 2, "v2_frozen": 1, "v3_forward": 1,
             "v3_epoch_mismatch": 2, "v4_forward": 0, "v4_epoch_mismatch": 0,
             "invalid": 1},
            report["versions"],
        )
        self.assertFalse(refused["ok"])
        self.assertEqual("historical_feature_backfill_frozen", refused["error"])
        self.assertFalse(refused["historical_mutation"])

    def test_finder_row_loader_shares_the_acceptance_rule(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE sample(experience_vector TEXT)")
        connection.executemany(
            "INSERT INTO sample VALUES(?)",
            [(vector,) for vector in self._vectors()])
        rows = connection.execute("SELECT * FROM sample").fetchall()
        connection.close()
        accepted = [
            index for index, row in enumerate(rows)
            if find_similar_experience._row_features_v3(row) is not None
        ]
        self.assertEqual([1], accepted)
        classes = [
            _simutil.classify_stored_vector(
                json.loads(row["experience_vector"] or "null"))
            for row in rows[:6]
        ]
        self.assertEqual(
            ["v2_frozen", "v3_forward", "v3_epoch_mismatch",
             "v3_epoch_mismatch", "v1_or_legacy", "v1_or_legacy"],
            classes,
        )


class SimilarityStatisticsTests(unittest.TestCase):
    def test_similarity_treats_non_finite_numbers_as_missing(self) -> None:
        base = {"asset_class": "crypto", "side": "long", "action": "open",
                "regime": "range", "stop_distance_pct": 0.03}
        clean_query = _simutil.experience_features_v3(base)
        nan_query = _simutil.experience_features_v3(
            {**base, "funding_rate": float("nan")})
        row = _simutil.experience_features_v3(
            {**base, "funding_rate": 0.0001})
        self.assertEqual(
            _simutil.similarity_v3(clean_query, row),
            _simutil.similarity_v3(nan_query, row),
        )
        bool_query = _simutil.experience_features_v3(
            {**base, "planned_rr": True})
        self.assertEqual(
            _simutil.similarity_v3(clean_query, row),
            _simutil.similarity_v3(bool_query, row),
        )

    def test_wilson_lower_bound_and_weighted_stats(self) -> None:
        self.assertLess(_simutil.wilson_lo95(3, 3), 0.5)
        self.assertGreater(_simutil.wilson_lo95(60, 100), 0.5)
        self.assertEqual(0.0, _simutil.wilson_lo95(0, 0))
        self.assertEqual(0.0, _simutil.wilson_lo95(-1, 5))
        self.assertLessEqual(_simutil.wilson_lo95(9, 5), 1.0)
        win_rate, mean = _simutil.similarity_weighted(
            [(0.9, 1.0), (0.8, -1.0), (0.5, 0.5), (float("nan"), 9.0), (0.0, 9.0)])
        self.assertAlmostEqual((0.9 + 0.5) / 2.2, win_rate)
        self.assertAlmostEqual((0.9 - 0.8 + 0.25) / 2.2, mean)
        self.assertEqual((None, None), _simutil.similarity_weighted([]))

    @staticmethod
    def _neighbors(rows):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE t(id INTEGER,ts TEXT,profile TEXT,pnl_pct REAL,"
            "realized_r_net REAL)")
        connection.executemany("INSERT INTO t VALUES(?,?,?,?,?)", [
            (index, "2026-08-30 12:00:00", "live", pnl, r)
            for index, (_, pnl, r) in enumerate(rows, start=1)
        ])
        fetched = connection.execute("SELECT * FROM t ORDER BY id").fetchall()
        connection.close()
        return [(sim, row) for (sim, _, _), row in zip(rows, fetched)]

    def test_sufficient_summary_reports_lower_bound_and_weighted_stats(self) -> None:
        rows = [
            (0.9, 1.0, 1.0), (0.8, -1.0, -1.0), (0.7, 2.0, None),
            (0.6, -0.5, -0.5), (0.5, 1.5, None), (0.4, -2.0, -1.2),
        ]
        now = datetime(2026, 8, 31, 12, 0, tzinfo=find_similar_experience.CST)
        summary = find_similar_experience._experience_summary(
            self._neighbors(rows), now, scope="same_symbol_similar")
        self.assertTrue(summary["sufficient"])
        self.assertEqual(6, summary["n"])
        self.assertEqual(0.5, summary["win_rate"])
        self.assertEqual(round(_simutil.wilson_lo95(3, 6), 4),
                         summary["win_rate_lo95"])
        self.assertLess(summary["win_rate_lo95"], summary["win_rate"])
        self.assertEqual(round((0.9 + 0.7 + 0.5) / 3.9, 4),
                         summary["sim_weighted"]["win_rate"])
        self.assertEqual(
            round((0.9 - 0.8 + 1.4 - 0.3 + 0.75 - 0.8) / 3.9, 4),
            summary["sim_weighted"]["avg_pnl_pct"])
        self.assertEqual(4, summary["realized_r_n"])
        self.assertEqual(round((1.0 - 1.0 - 0.5 - 1.2) / 4, 4),
                         summary["avg_realized_r_net"])
        self.assertEqual([1, 2, 3, 4, 5, 6], summary["sample_ids"])

    def test_insufficient_summary_keeps_the_frozen_shape(self) -> None:
        rows = [(0.9, 1.0, 1.0), (0.8, -1.0, None), (0.7, 2.0, None)]
        now = datetime(2026, 8, 31, 12, 0, tzinfo=find_similar_experience.CST)
        summary = find_similar_experience._experience_summary(
            self._neighbors(rows), now, scope="same_symbol_similar")
        self.assertFalse(summary["sufficient"])
        for key in ("win_rate", "win_rate_lo95", "sim_weighted",
                    "avg_realized_r_net"):
            self.assertNotIn(key, summary)

    def test_new_fields_are_frozen_by_the_evidence_hash(self) -> None:
        rows = [(0.9, 1.0, None)] * 5
        now = datetime(2026, 8, 31, 12, 0, tzinfo=find_similar_experience.CST)
        same = find_similar_experience._experience_summary(
            self._neighbors(rows), now, scope="same_symbol_similar")
        empty_exact = find_similar_experience._experience_summary(
            [], now, scope="same_symbol_side_action_regime")
        empty_cross = find_similar_experience._experience_summary(
            [], now, scope="cross_symbol_similar")
        query = {"symbol": "BTC-USDT-SWAP", "side": "long", "regime": "range",
                 "action": "open", "profile": "live",
                 "as_of": "2026-08-31 12:00:00"}
        contract = build_contract(
            query, exact_setup=empty_exact, same_symbol_similar=same,
            cross_symbol_similar=empty_cross)
        self.assertEqual([], validate_contract(contract))
        tampered = json.loads(json.dumps(contract))
        tampered["summaries"]["same_symbol_similar"]["win_rate_lo95"] = 0.99
        self.assertTrue(any(
            "evidence_hash" in error for error in validate_contract(tampered)))


class PathMetricVersionTests(unittest.TestCase):
    def test_writer_stamps_the_single_source_version(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE trade_experiences("
            "id INTEGER PRIMARY KEY,initial_risk_usdt REAL,mfe_r REAL,"
            "mae_r REAL,realized_r_net REAL,close_at_1r INTEGER,"
            "ever_hit_1r INTEGER,exit_category TEXT,path_coverage TEXT,"
            "path_metric_version INTEGER)")
        connection.execute("INSERT INTO trade_experiences(id) VALUES(1)")
        trade_experience_writer._fill_path_metrics(
            connection, 1, "BTC-USDT-SWAP", "long", {"fill_px": 0},
            "2026-08-10 10:00:00", "2026-08-10 11:00:00", 0.0, {})
        coverage, version = connection.execute(
            "SELECT path_coverage,path_metric_version FROM trade_experiences "
            "WHERE id=1").fetchone()
        connection.close()
        self.assertEqual("none", coverage)
        self.assertEqual(path_metrics.PATH_METRIC_VERSION, version)
        self.assertEqual(3, version)


if __name__ == "__main__":
    unittest.main()
