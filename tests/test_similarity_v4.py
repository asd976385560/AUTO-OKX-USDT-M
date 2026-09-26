# -*- coding: utf-8 -*-
"""2026-09-26 对照 V3 experience::similarity：v4 特征集与几何平均贴近度的纯函数契约。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import _simutil  # noqa: E402


def _bars(closes, volumes=None):
    volumes = volumes or [100.0 + i for i in range(len(closes))]
    return [(c + 0.1, c - 0.1, c, v) for c, v in zip(closes, volumes)]


class IndicatorTests(unittest.TestCase):
    def test_rising_series_matches_v3_expectations(self):
        closes = [100.0 + i * 0.5 for i in range(250)]
        ind = _simutil.compute_indicators_1h(_bars(closes))
        self.assertEqual(250, ind["bars"])
        self.assertEqual(1, ind["ema_trend_1h"])
        self.assertGreater(ind["rsi_1h"], 90.0)
        self.assertGreater(ind["atr_pct_1h"], 0.0)
        self.assertAlmostEqual(ind["ret_4h"], closes[-1] / closes[-5] - 1.0)
        self.assertAlmostEqual(ind["ret_16h"], closes[-1] / closes[-17] - 1.0)
        self.assertGreater(ind["vol_z_1h"], 0.0)

    def test_short_series_leaves_features_missing(self):
        ind = _simutil.compute_indicators_1h(_bars([1.0, 2.0, 3.0]))
        self.assertEqual(3, ind["bars"])
        self.assertIsNone(ind["ema_trend_1h"])
        self.assertIsNone(ind["rsi_1h"])
        self.assertIsNone(ind["atr_pct_1h"])
        self.assertIsNone(ind["ret_4h"])
        self.assertIsNone(ind["vol_z_1h"])
        empty = _simutil.compute_indicators_1h([])
        self.assertEqual(0, empty["bars"])
        falling = _simutil.compute_indicators_1h(
            _bars([300.0 - i for i in range(250)]))
        self.assertEqual(-1, falling["ema_trend_1h"])
        self.assertLess(falling["rsi_1h"], 10.0)

    def test_garbage_bars_are_skipped(self):
        bars = _bars([100.0] * 30) + [("x", None, "nan", 1.0)]
        ind = _simutil.compute_indicators_1h(bars)
        self.assertEqual(30, ind["bars"])
        # 平盘：每根真实波幅 = 高低差 0.2 → ATR% = 0.2 / 100
        self.assertAlmostEqual(0.002, ind["atr_pct_1h"])
        self.assertEqual(0.0, ind["ret_4h"])


class SimilarityTests(unittest.TestCase):
    @staticmethod
    def feat(**overrides):
        base = {
            "asset_class": "crypto", "side": "long", "action": "open",
            "symbol": "BTC-USDT-SWAP", "atr_pct_1h": 0.02, "rsi_1h": 50.0,
            "ema_trend_1h": 1, "sl_pct": 0.04, "hour_utc": 3,
        }
        base.update(overrides)
        return _simutil.experience_features_v4(base)

    def test_identical_features_score_one_and_gates_hold(self):
        a = self.feat()
        sim, coverage = _simutil.similarity_v4(a, a)
        self.assertEqual(1.0, sim)
        self.assertEqual(5, coverage)  # atr, rsi, sl, trend, hour
        self.assertEqual((0.0, 0), _simutil.similarity_v4(a, self.feat(side="short")))
        self.assertEqual((0.0, 0), _simutil.similarity_v4(
            a, self.feat(asset_class="tokenized_stock")))
        self.assertEqual((0.0, 0), _simutil.similarity_v4(a, self.feat(action="close")))
        cross, _ = _simutil.similarity_v4(a, self.feat(symbol="ETH-USDT-SWAP"))
        self.assertEqual(0.9, cross)
        far, _ = _simutil.similarity_v4(
            a, self.feat(atr_pct_1h=0.10, rsi_1h=90.0, ema_trend_1h=-1, sl_pct=0.06))
        self.assertLess(far, 0.5)

    def test_hour_distance_is_circular_and_missing_features_only_drop_coverage(self):
        near = _simutil.similarity_v4(self.feat(hour_utc=23), self.feat(hour_utc=1))
        far = _simutil.similarity_v4(self.feat(hour_utc=11), self.feat(hour_utc=1))
        self.assertGreater(near[0], far[0])
        self.assertEqual(2, _simutil.hour_distance(23, 1))
        partial = _simutil.experience_features_v4(
            {"asset_class": "crypto", "side": "long", "action": "open",
             "symbol": "BTC-USDT-SWAP", "sl_pct": 0.04})
        sim, coverage = _simutil.similarity_v4(self.feat(), partial)
        self.assertEqual(1.0, sim)
        self.assertEqual(1, coverage)
        empty = _simutil.experience_features_v4(
            {"asset_class": "crypto", "side": "long", "action": "open"})
        self.assertEqual((0.0, 0), _simutil.similarity_v4(self.feat(), empty))

    def test_geometric_mean_and_epoch_isolation(self):
        a = self.feat(sl_pct=0.04, hour_utc=None, ema_trend_1h=None,
                      atr_pct_1h=None, rsi_1h=None)
        b = self.feat(sl_pct=0.055, hour_utc=None, ema_trend_1h=None,
                      atr_pct_1h=None, rsi_1h=None)
        sim, coverage = _simutil.similarity_v4(a, b)
        self.assertEqual(1, coverage)
        self.assertAlmostEqual(round(2.718281828 ** (-0.015 / 0.015), 4), sim)
        v3 = _simutil.experience_features_v3({"asset_class": "crypto", "side": "long",
                                              "action": "open", "stop_distance_pct": 0.04})
        self.assertEqual((0.0, 0), _simutil.similarity_v4(a, v3))
        wrong = dict(b, feature_epoch="wrong")
        self.assertEqual((0.0, 0), _simutil.similarity_v4(a, wrong))

    def test_v4_payload_carries_v2_keys_for_consumers(self):
        feats = self.feat(stop_distance_pct=0.04, trend_1h=1, trend_4h=-1, regime="range")
        for key in ("asset_class", "trend_1h", "trend_4h", "regime", "stop_distance_pct",
                    "planned_rr", "funding_rate", "vol_24h_pct"):
            self.assertIn(key, feats)
        self.assertEqual(4, feats["v"])
        self.assertEqual(_simutil.FEATURE_EPOCH_V4, feats["feature_epoch"])
        self.assertEqual(0.04, feats["sl_pct"])
        stored = {"v": 4, "feature_epoch": _simutil.FEATURE_EPOCH_V4, "features": feats}
        self.assertEqual("v4_forward", _simutil.classify_stored_vector(stored))
        self.assertEqual(
            "v4_epoch_mismatch",
            _simutil.classify_stored_vector({**stored, "feature_epoch": "x"}))
        self.assertIs(feats, _simutil.stored_v4_features(stored))


if __name__ == "__main__":
    unittest.main()


class FinderIntegrationTests(unittest.TestCase):
    """finder 在 v4 空间比较：历史 v1/v3 行按行 ts 现算特征，v4 行直接取。"""

    SCHEMA = (
        "CREATE TABLE trade_experiences("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,cycle_id TEXT,ts TEXT,profile TEXT,"
        "symbol TEXT,side TEXT,action TEXT,regime TEXT,regime_stale INTEGER,"
        "score_total REAL,confidence REAL,playbook_ref TEXT,experience_vector TEXT,"
        "pnl_pct REAL,hold_hours REAL,is_gross_profit_close INTEGER,raw TEXT,"
        "experience_summary TEXT,status TEXT,closed_at TEXT)"
    )

    @staticmethod
    def _market(root):
        import datetime as dt
        con = sqlite3.connect(root / "market.db")
        con.execute(
            "CREATE TABLE kline_cache(symbol TEXT,tf TEXT,ts TEXT,"
            "o REAL,h REAL,l REAL,c REAL,v REAL)")
        end = dt.datetime(2026, 8, 30, 17, 0, tzinfo=dt.timezone.utc)
        rows = []
        for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
            for index in range(60):
                ts = end - dt.timedelta(hours=59 - index)
                close = 100.0 + index * 0.5
                rows.append((symbol, "1H", ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             close, close + 0.4, close - 0.4, close, 1000.0 + index))
        con.executemany("INSERT INTO kline_cache VALUES(?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()

    def test_old_and_new_rows_share_the_v4_space(self):
        import json
        import tempfile
        from datetime import datetime
        import find_similar_experience

        v3_vec = {"v": 3, "feature_epoch": _simutil.FEATURE_EPOCH_V3,
                  "features": _simutil.experience_features_v3({
                      "asset_class": "crypto", "side": "long", "action": "open",
                      "regime": "range", "stop_distance_pct": 0.04})}
        v4_vec = {"v": 4, "feature_epoch": _simutil.FEATURE_EPOCH_V4,
                  "features": _simutil.experience_features_v4({
                      "asset_class": "crypto", "side": "long", "action": "open",
                      "regime": "range", "symbol": "ETH-USDT-SWAP", "sl_pct": 0.04,
                      "atr_pct_1h": 0.007, "rsi_1h": 95.0, "hour_utc": 12})}
        rows = [
            ("c1", "2026-08-30 20:00:00", "BTC-USDT-SWAP", "long", json.dumps(v3_vec),
             1.0, json.dumps({"fill_px": 100.0, "sl_trigger_px": 96.0})),
            ("c2", "2026-08-30 20:00:00", "ETH-USDT-SWAP", "long", json.dumps(v4_vec),
             -1.0, "{}"),
            ("c3", "2026-08-30 20:00:00", "BTC-USDT-SWAP", "long", json.dumps([0.0] * 10),
             2.0, "{}"),
            ("c4", "2026-08-30 20:00:00", "BTC-USDT-SWAP", "short", json.dumps(v3_vec),
             -2.0, "{}"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._market(root)
            con = sqlite3.connect(root / "account.db")
            con.execute(self.SCHEMA)
            for cycle, ts, symbol, side, vector, pnl, raw in rows:
                con.execute(
                    "INSERT INTO trade_experiences(cycle_id,ts,profile,symbol,side,"
                    "action,regime,regime_stale,experience_vector,pnl_pct,hold_hours,"
                    "raw,status,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cycle, ts, "live", symbol, side, "open", "range", 0, vector,
                     pnl, 2.0, raw, "closed", "2026-08-30 22:00:00"))
            con.commit()
            con.close()
            result = find_similar_experience.find_similar_experience(
                "BTC", "long", "range", "open", db_root=root,
                now=datetime(2026, 8, 31, 1, 0, tzinfo=find_similar_experience.CST),
                stop_distance_pct=0.04, planned_rr=2.0)
        self.assertEqual(_simutil.SIMILARITY_VERSION_V4, result["similarity_version"])
        self.assertEqual(_simutil.FEATURE_EPOCH_V4, result["feature_epoch"])
        self.assertEqual(4, result["query"]["query_features"]["v"])
        self.assertEqual(17, result["query"]["query_features"]["hour_utc"])
        self.assertIsNotNone(result["query"]["query_features"]["atr_pct_1h"])
        self.assertEqual("feature_epoch_filter_v2", result["feature_epoch_filter"]["version"])
        self.assertEqual(0, result["feature_epoch_filter"]["excluded_total"])
        self.assertEqual(4, result["feature_epoch_filter"]["included_total"])
        # exact_setup 不看贴近度门槛：BTC/long/open/range 的 v3 行与 v1 行都在
        self.assertEqual(2, result["exact_setup_summary"]["n"])
        self.assertEqual([1, 3], result["exact_setup_summary"]["sample_ids"])
        # v3 行：止损距离 + 开仓小时 + 按行 ts 现算的 1H 指标；v1 行（raw 无止损）
        # 同样现算指标，只少一个 sl_pct 特征——历史行不再按 epoch 一刀切排除
        by_id = {m["experience_id"]: m for m in result["matched_wins"] + result["matched_losses"]}
        self.assertIn(1, by_id)
        self.assertGreaterEqual(by_id[1]["coverage"], 3)
        self.assertGreaterEqual(by_id[1]["sim"], 0.35)
        self.assertIn(3, by_id)
        self.assertEqual(by_id[1]["coverage"] - 1, by_id[3]["coverage"])
        cross = {m["experience_id"]: m for m in result["cross_symbol_losses"]}
        self.assertIn(2, cross)
        self.assertLessEqual(cross[2]["sim"], 0.9)
        # 方向硬门：short 行进不了任何 scope
        self.assertNotIn(4, by_id)
        self.assertEqual(0, result["cross_summary"]["n"] - len(cross))
