# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
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
import experience_features_v2 as features  # noqa: E402
import find_similar_experience  # noqa: E402
import trade_experience_writer  # noqa: E402


class ExperienceFeaturesV3Tests(unittest.TestCase):
    def _market(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.executescript("""
        CREATE TABLE derivatives(symbol TEXT,ts TEXT,funding_rate REAL);
        CREATE TABLE kline_cache(
          symbol TEXT,tf TEXT,ts TEXT,h REAL,l REAL,c REAL
        );
        """)
        return connection

    def test_offset_as_of_is_normalized_to_utc(self) -> None:
        self.assertEqual(
            "2026-08-30T17:00:00Z",
            features._cst_to_utcz("2026-08-31T01:00:00+08:00"),
        )

    def test_strict_24h_window_excludes_lower_open_bar_and_older_rows(self) -> None:
        connection = self._market()
        upper = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)   # as_of；这根 15m 还没收盘
        last_closed = upper - timedelta(minutes=15)
        lower = last_closed - timedelta(hours=24)                    # 严格下界，不含
        rows = [
            ("BTC-USDT-SWAP", "15m", "2026-08-29T00:00:00Z",
             1000.0, 1.0, 100.0),
            ("BTC-USDT-SWAP", "15m", lower.strftime("%Y-%m-%dT%H:%M:%SZ"),
             900.0, 2.0, 100.0),
            ("BTC-USDT-SWAP", "15m", upper.strftime("%Y-%m-%dT%H:%M:%SZ"),
             900.0, 2.0, 100.0),
        ]
        for index in range(1, 97):
            ts = lower + timedelta(minutes=15 * index)
            rows.append((
                "BTC-USDT-SWAP", "15m", ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                110.0, 90.0, 100.0,
            ))
        connection.executemany(
            "INSERT INTO kline_cache VALUES(?,?,?,?,?,?)", rows)
        try:
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:00:00")
        finally:
            connection.close()
        self.assertEqual(0.2, result["vol_24h_pct"])

    def test_funding_window_does_not_admit_the_exact_lower_boundary(self) -> None:
        connection = self._market()
        connection.executemany(
            "INSERT INTO derivatives VALUES(?,?,?)",
            [
                ("BTC-USDT-SWAP", "2026-08-30T13:00:00Z", 0.9),
                ("BTC-USDT-SWAP", "2026-08-30T13:01:00Z", 0.1),
            ],
        )
        try:
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:00:00")
        finally:
            connection.close()
        self.assertEqual(0.1, result["funding_rate"])

    def test_volatility_requires_all_96_distinct_quarter_hour_bars(self) -> None:
        connection = self._market()
        upper = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)
        lower = upper - timedelta(hours=24)
        rows = []
        for index in range(1, 96):
            ts = lower + timedelta(minutes=15 * index)
            rows.append((
                "BTC-USDT-SWAP", "15m",
                ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                110.0, 90.0, 100.0,
            ))
        connection.executemany(
            "INSERT INTO kline_cache VALUES(?,?,?,?,?,?)", rows)
        try:
            result = features.derive_market_features(
                connection, "BTC-USDT-SWAP", "2026-08-31 01:00:00")
        finally:
            connection.close()
        self.assertIsNone(result["vol_24h_pct"])

    def test_similarity_v2_trend_keys_go_through_the_finite_gate(self) -> None:
        base = {
            "asset_class": "crypto", "side": "long", "action": "open",
            "regime": "range", "stop_distance_pct": 0.04, "trend_4h": 1,
        }
        query = {**base, "trend_1h": 1}
        missing = _simutil.similarity_v2(query, {**base, "trend_1h": None})
        self.assertLess(missing, _simutil.similarity_v2(query, query))
        # 非有限 / 非数字的趋势值 = 缺失：既不算 0 分，也不能让 True 冒充 1
        for bad in (float("nan"), float("inf"), True, "up", "", "1x"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    missing, _simutil.similarity_v2(query, {**base, "trend_1h": bad}))
        # 1.0 / "1" 与 1 是同一个趋势值；-1 才是错配
        self.assertEqual(
            _simutil.similarity_v2(query, query),
            _simutil.similarity_v2(query, {**base, "trend_1h": 1.0}))
        self.assertEqual(
            _simutil.similarity_v2(query, query),
            _simutil.similarity_v2(query, {**base, "trend_1h": "1"}))
        self.assertLess(
            _simutil.similarity_v2(query, {**base, "trend_1h": -1}), missing)

    def test_similarity_v3_refuses_v2_and_wrong_epoch(self) -> None:
        base = {
            "asset_class": "crypto", "side": "long", "action": "open",
            "regime": "range",
        }
        v2 = _simutil.experience_features_v2(base)
        v3 = _simutil.experience_features_v3(base)
        wrong_epoch = {**v3, "feature_epoch": "wrong"}
        self.assertEqual(0.0, _simutil.similarity_v3(v3, v2))
        self.assertEqual(0.0, _simutil.similarity_v3(v3, wrong_epoch))
        self.assertGreater(_simutil.similarity_v3(v3, v3), 0.0)

    def test_writer_creates_explicit_v4_epoch_and_keeps_v3_features(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            trade_experience_writer, "_DB_ROOT", Path(temporary)
        ), mock.patch(
            "core.asset_class.asset_class_of", return_value="crypto"
        ):
            payload = trade_experience_writer._v3_vector_payload(
                "BTC-USDT-SWAP", "long", "open", "range",
                "2026-08-31 01:00:00",
                {"fill_px": 100.0, "sl_trigger_px": 95.0},
            )
        self.assertEqual(4, payload["v"])
        self.assertEqual(_simutil.FEATURE_EPOCH_V4, payload["feature_epoch"])
        self.assertEqual(4, payload["features"]["v"])
        self.assertEqual(
            _simutil.FEATURE_EPOCH_V4,
            payload["features"]["feature_epoch"],
        )
        self.assertEqual(0.05, payload["features"]["sl_pct"])
        self.assertEqual(17, payload["features"]["hour_utc"])
        self.assertEqual(3, payload["features_v3"]["v"])
        self.assertEqual(
            _simutil.FEATURE_EPOCH_V3,
            payload["features_v3"]["feature_epoch"],
        )

    def test_row_loader_accepts_only_exact_v3_epoch(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE sample(experience_vector TEXT)")
        v2 = {"v": 2, "features": _simutil.experience_features_v2({})}
        v3 = {
            "v": 3,
            "feature_epoch": _simutil.FEATURE_EPOCH_V3,
            "features": _simutil.experience_features_v3({}),
        }
        connection.executemany(
            "INSERT INTO sample VALUES(?)",
            [(json.dumps(v2),), (json.dumps(v3),)],
        )
        rows = connection.execute("SELECT * FROM sample").fetchall()
        connection.close()
        self.assertIsNone(find_similar_experience._row_features_v3(rows[0]))
        self.assertEqual(
            _simutil.FEATURE_EPOCH_V3,
            find_similar_experience._row_features_v3(rows[1])["feature_epoch"],
        )

    def test_feature_epoch_filter_externalizes_excluded_failures(self) -> None:
        rows = [
            {"id": 1, "pnl_pct": 1.0},
            {"id": 2, "pnl_pct": -1.0},
            {"id": 3, "pnl_pct": 0.0},
        ]
        result = find_similar_experience.build_feature_epoch_filter(rows, 4)
        self.assertEqual(4, result["included_total"])
        self.assertEqual(3, result["excluded_total"])
        self.assertEqual(1, result["excluded_wins"])
        self.assertEqual(2, result["excluded_losses"])
        self.assertEqual([1, 2, 3], result["excluded_sample_ids"])
        compact = find_similar_experience.compact_result({
            "feature_epoch_filter": result,
            "feature_epoch": _simutil.FEATURE_EPOCH_V3,
            "similarity_version": _simutil.SIMILARITY_VERSION,
        })
        self.assertEqual(result, compact["feature_epoch_filter"])

    def test_historical_v2_backfill_is_frozen_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_root = Path(temporary)
            db_path = db_root / "account.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE trade_experiences("
                "id INTEGER PRIMARY KEY,experience_vector TEXT)"
            )
            payload = json.dumps({
                "v": 2, "features": _simutil.experience_features_v2({})})
            connection.executemany(
                "INSERT INTO trade_experiences VALUES(?,?)",
                [(index, payload) for index in range(1, 196)],
            )
            connection.commit()
            connection.close()
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()
            result = features.backfill(db_root, apply=True)
            after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        self.assertFalse(result["ok"])
        self.assertFalse(result["historical_mutation"])
        self.assertEqual(195, result["versions"]["v2_frozen"])
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
