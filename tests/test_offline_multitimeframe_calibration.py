from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import offline_multitimeframe_calibration as calibration  # noqa: E402


def _create_enrichment_tables(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE market_microstructure(
            ts TEXT,cycle_id TEXT,symbol TEXT,spread_bps REAL,
            bid_depth_10bp_usd REAL,ask_depth_10bp_usd REAL,
            bid_depth_25bp_usd REAL,ask_depth_25bp_usd REAL,
            bid_depth_50bp_usd REAL,ask_depth_50bp_usd REAL,
            imbalance_10bp REAL,imbalance_25bp REAL,imbalance_50bp REAL,
            buy_slippage_100usd_bps REAL,sell_slippage_100usd_bps REAL,
            buy_slippage_500usd_bps REAL,sell_slippage_500usd_bps REAL,
            buy_slippage_1000usd_bps REAL,sell_slippage_1000usd_bps REAL
        );
        CREATE TABLE market_trade_flow(
            ts TEXT,cycle_id TEXT,symbol TEXT,sample_count INTEGER,
            sample_span_ms INTEGER,buy_notional_usd REAL,sell_notional_usd REAL,
            taker_buy_ratio REAL,cvd_notional_usd REAL,largest_trade_usd REAL
        );
        CREATE TABLE market_positioning(
            collected_ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,
            long_ratio REAL,short_ratio REAL,long_short_ratio REAL
        );
        CREATE TABLE market_contract_statistics(
            ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,
            oi_contracts REAL,oi_ccy REAL,oi_usd REAL,
            taker_sell_usd REAL,taker_buy_usd REAL,taker_buy_ratio REAL,
            raw TEXT,source TEXT
        );
        """
    )


def _insert_enrichment_cycle(
    con: sqlite3.Connection,
    micro_ts: str,
    flow_ts: str,
    positioning_ts: str | None = None,
) -> None:
    cycle = "2026-08-11T08:00"
    symbol = "BTC-USDT-SWAP"
    con.execute(
        "INSERT INTO market_microstructure VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            micro_ts, cycle, symbol, 1.0,
            10_000.0, 11_000.0, 20_000.0, 21_000.0, 30_000.0, 31_000.0,
            -0.05, -0.02, 0.01, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7,
        ),
    )
    con.execute(
        "INSERT INTO market_trade_flow VALUES(?,?,?,?,?,?,?,?,?,?)",
        (flow_ts, cycle, symbol, 500, 60_000, 6_000.0, 4_000.0, 0.6, 2_000.0, 500.0),
    )
    if positioning_ts is not None:
        con.execute(
            "INSERT INTO market_positioning VALUES(?,?,?,?,?,?,?)",
            (positioning_ts, cycle, symbol, "1H", 0.55, 0.45, 1.22),
        )


class OfflineMultitimeframeCalibrationTests(unittest.TestCase):
    def test_datetime_integer_contract_is_nanoseconds(self) -> None:
        values = pd.Series(pd.to_datetime([
            "2026-08-11T00:00:00Z",
            "2026-08-11T00:15:00Z",
        ], utc=True))
        encoded = calibration._datetime_ns(values)
        self.assertEqual(encoded[1] - encoded[0], 900_000_000_000)

    def test_entry_is_strictly_after_observation_and_horizons_use_entry_clock(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT,last REAL,bid REAL,ask REAL)")
        con.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
            [
                ("2026-08-11T00:00:00Z", "BTC-USDT-SWAP", 50.0, 49.9, 50.1),
                ("2026-08-11T00:15:00Z", "BTC-USDT-SWAP", 100.0, 99.9, 100.1),
                ("2026-08-11T00:30:00Z", "BTC-USDT-SWAP", 101.0, 100.9, 101.1),
                ("2026-08-11T01:15:00Z", "BTC-USDT-SWAP", 102.0, 101.9, 102.1),
                ("2026-08-11T04:15:00Z", "BTC-USDT-SWAP", 103.0, 102.9, 103.1),
            ],
        )
        frame = pd.DataFrame({
            "obs_id": [0],
            "symbol": ["BTC-USDT-SWAP"],
            "obs_ts": pd.to_datetime(["2026-08-11T00:00:00Z"], utc=True),
        })
        labeled, audit = calibration._add_forward_labels(con, frame, 20.0)
        con.close()

        self.assertEqual(audit["entry_labeled_rows"], 1)
        self.assertEqual(labeled.loc[0, "entry_ts"], pd.Timestamp("2026-08-11T00:15:00Z"))
        self.assertEqual(labeled.loc[0, "15m_exit_ts"], pd.Timestamp("2026-08-11T00:30:00Z"))
        self.assertEqual(labeled.loc[0, "1H_exit_ts"], pd.Timestamp("2026-08-11T01:15:00Z"))
        self.assertEqual(labeled.loc[0, "4H_exit_ts"], pd.Timestamp("2026-08-11T04:15:00Z"))
        self.assertTrue(bool(labeled.loc[0, "15m_long_success"]))
        self.assertTrue(bool(labeled.loc[0, "1H_long_success"]))
        self.assertTrue(bool(labeled.loc[0, "4H_long_success"]))

    def test_executable_labels_use_ask_to_bid_and_bid_to_ask_without_last_fallback(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT,last REAL,bid REAL,ask REAL)")
        con.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
            [
                ("2026-08-11T00:15:00Z", "BTC-USDT-SWAP", 100.0, 99.0, 101.0),
                ("2026-08-11T00:30:00Z", "BTC-USDT-SWAP", 103.0, 102.0, 104.0),
                ("2026-08-11T01:15:00Z", "BTC-USDT-SWAP", 96.0, 95.0, 97.0),
                ("2026-08-11T04:15:00Z", "BTC-USDT-SWAP", 100.0, None, None),
            ],
        )
        frame = pd.DataFrame({
            "obs_id": [0], "symbol": ["BTC-USDT-SWAP"],
            "obs_ts": pd.to_datetime(["2026-08-11T00:00:00Z"], utc=True),
        })

        labeled, audit = calibration._add_forward_labels(
            con, frame, 20.0, price_mode="executable")
        con.close()

        self.assertEqual(audit["price_mode"], "executable")
        self.assertAlmostEqual(
            labeled.loc[0, "15m_long_return"], 102.0 / 101.0 - 1.0)
        self.assertAlmostEqual(
            labeled.loc[0, "15m_short_return"], 1.0 - 104.0 / 99.0)
        self.assertTrue(bool(labeled.loc[0, "15m_long_success"]))
        self.assertFalse(bool(labeled.loc[0, "15m_short_success"]))
        self.assertFalse(bool(labeled.loc[0, "1H_long_success"]))
        self.assertTrue(bool(labeled.loc[0, "1H_short_success"]))
        self.assertTrue(pd.isna(labeled.loc[0, "4H_long_success"]))
        self.assertTrue(pd.isna(labeled.loc[0, "4H_short_success"]))

    def test_enrichment_decision_clock_prevents_pre_feature_entry(self) -> None:
        con = sqlite3.connect(":memory:")
        _create_enrichment_tables(con)
        _insert_enrichment_cycle(
            con,
            "2026-08-11T00:00:40Z",
            "2026-08-11T00:00:42Z",
            "2026-08-11T00:00:45Z",
        )
        con.execute(
            "CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT,last REAL,bid REAL,ask REAL)")
        con.executemany(
            "INSERT INTO tick_snapshots VALUES(?,?,?,?,?)",
            [
                ("2026-08-11T00:00:00Z", "BTC-USDT-SWAP", 99.0, 98.9, 99.1),
                ("2026-08-11T00:00:30Z", "BTC-USDT-SWAP", 100.0, 99.9, 100.1),
                ("2026-08-11T00:15:00Z", "BTC-USDT-SWAP", 101.0, 100.9, 101.1),
                ("2026-08-11T00:30:00Z", "BTC-USDT-SWAP", 102.0, 101.9, 102.1),
                ("2026-08-11T01:15:00Z", "BTC-USDT-SWAP", 103.0, 102.9, 103.1),
                ("2026-08-11T04:15:00Z", "BTC-USDT-SWAP", 104.0, 103.9, 104.1),
            ],
        )
        frame = pd.DataFrame({
            "obs_id": [0],
            "symbol": ["BTC-USDT-SWAP"],
            "obs_ts": pd.to_datetime(["2026-08-11T00:00:00Z"], utc=True),
        })
        enhanced, audit = calibration._add_enrichment_features(con, frame)
        labeled, label_audit = calibration._add_forward_labels(
            con, enhanced, 20.0, decision_time_col="decision_ts"
        )
        con.close()

        self.assertTrue(bool(enhanced.loc[0, "enrichment_ready"]))
        self.assertEqual(audit["positioning_available_rows"], 1)
        self.assertEqual(enhanced.loc[0, "decision_ts"], pd.Timestamp("2026-08-11T00:00:45Z"))
        self.assertEqual(label_audit["entry_anchor"], "decision_ts")
        self.assertEqual(labeled.loc[0, "entry_ts"], pd.Timestamp("2026-08-11T00:15:00Z"))

    def test_enrichment_after_ten_minutes_fails_closed(self) -> None:
        con = sqlite3.connect(":memory:")
        _create_enrichment_tables(con)
        _insert_enrichment_cycle(
            con,
            "2026-08-11T00:11:00Z",
            "2026-08-11T00:11:01Z",
        )
        frame = pd.DataFrame({
            "obs_id": [0],
            "symbol": ["BTC-USDT-SWAP"],
            "obs_ts": pd.to_datetime(["2026-08-11T00:00:00Z"], utc=True),
        })
        enhanced, audit = calibration._add_enrichment_features(con, frame)
        con.close()

        self.assertFalse(bool(enhanced.loc[0, "enrichment_ready"]))
        self.assertEqual(audit["microstructure_late_or_early_rows"], 1)
        self.assertEqual(audit["trade_flow_late_or_early_rows"], 1)
        self.assertEqual(enhanced.loc[0, "decision_ts"], pd.Timestamp("2026-08-11T00:00:00Z"))

    def test_contract_statistics_are_optional_point_in_time_features(self) -> None:
        con = sqlite3.connect(":memory:")
        _create_enrichment_tables(con)
        _insert_enrichment_cycle(
            con,
            "2026-08-11T00:00:40Z",
            "2026-08-11T00:00:42Z",
        )
        con.executemany(
            "INSERT INTO market_contract_statistics VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "2026-08-10T23:45:00Z", "2026-08-10T23:46:00Z",
                    "2026-08-11T07:45", "BTC-USDT-SWAP", "15m",
                    90.0, 9.0, 1000.0, 500.0, 500.0, 0.5, "{}",
                    "okx_rest_contract_oi_taker_15m",
                ),
                (
                    "2026-08-11T00:00:00Z", "2026-08-11T00:00:48Z",
                    "2026-08-11T08:00", "BTC-USDT-SWAP", "15m",
                    100.0, 10.0, 1100.0, 400.0, 600.0, 0.6, "{}",
                    "okx_rest_contract_oi_taker_15m",
                ),
            ],
        )
        frame = pd.DataFrame({
            "obs_id": [0],
            "symbol": ["BTC-USDT-SWAP"],
            "obs_ts": pd.to_datetime(["2026-08-11T00:00:00Z"], utc=True),
        })

        enhanced, audit = calibration._add_enrichment_features(con, frame)
        con.close()

        self.assertTrue(bool(enhanced.loc[0, "enrichment_ready"]))
        self.assertEqual(audit["contract_statistics_available_rows"], 1)
        self.assertEqual(
            enhanced.loc[0, "decision_ts"],
            pd.Timestamp("2026-08-11T00:00:48Z"),
        )
        self.assertAlmostEqual(
            enhanced.loc[0, "contract_taker_buy_centered"], 0.1)
        self.assertAlmostEqual(
            enhanced.loc[0, "contract_oi_log_change_15m"],
            np.log1p(1100.0) - np.log1p(1000.0),
        )

    def test_carried_contract_statistics_are_excluded_from_model_features(self) -> None:
        con = sqlite3.connect(":memory:")
        _create_enrichment_tables(con)
        _insert_enrichment_cycle(
            con,
            "2026-08-11T00:00:40Z",
            "2026-08-11T00:00:42Z",
        )
        carry_raw = json.dumps({
            "method": "official_previous_batch_carry_forward",
            "semantics": "excluded from model features",
        })
        con.executemany(
            "INSERT INTO market_contract_statistics VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "2026-08-10T23:45:00Z", "2026-08-10T23:46:00Z",
                    "2026-08-11T07:45", "BTC-USDT-SWAP", "15m",
                    90.0, 9.0, 1000.0, 500.0, 500.0, 0.5, "{}",
                    "okx_rest_contract_oi_taker_15m",
                ),
                (
                    "2026-08-10T23:45:00Z", "2026-08-11T00:00:48Z",
                    "2026-08-11T08:00", "BTC-USDT-SWAP", "15m",
                    90.0, 9.0, 1000.0, 500.0, 500.0, 0.5, carry_raw,
                    "okx_rest_contract_oi_taker_15m",
                ),
            ],
        )
        frame = pd.DataFrame({
            "obs_id": [0],
            "symbol": ["BTC-USDT-SWAP"],
            "obs_ts": pd.to_datetime(["2026-08-11T00:00:00Z"], utc=True),
        })

        enhanced, audit = calibration._add_enrichment_features(con, frame)
        con.close()

        self.assertEqual(audit["contract_statistics_carry_rows_excluded"], 1)
        self.assertEqual(audit["contract_statistics_available_rows"], 0)
        self.assertEqual(enhanced.loc[0, "contract_stats_available"], 0.0)
        self.assertTrue(pd.isna(
            enhanced.loc[0, "contract_oi_log_change_15m"]
        ))

    def test_feature_spec_records_enhanced_feature_contract(self) -> None:
        features = (*calibration.CONTINUOUS_FEATURES, *calibration.ENRICHMENT_FEATURES)
        rows = 25
        frame = pd.DataFrame({
            name: np.linspace(0.0, 1.0, rows) for name in features
        })
        frame["asset_class"] = "crypto"
        train_mask = pd.Series(np.ones(rows, dtype=bool))
        spec = calibration.fit_feature_spec(frame, train_mask, features)
        matrix = calibration.transform_features(frame, spec)

        self.assertEqual(spec.continuous_features, features)
        self.assertIn("book_spread_bps", spec.feature_names)
        self.assertIn("positioning_available", spec.feature_names)
        self.assertIn("contract_stats_available", spec.feature_names)
        self.assertEqual(matrix.shape[1], len(spec.feature_names))

    def test_indicator_recomputation_has_no_future_leakage(self) -> None:
        times = pd.date_range("2026-07-01", periods=80, freq="4h", tz="UTC")
        base = pd.DataFrame({
            "symbol": "BTC-USDT-SWAP",
            "bar_ts": times,
            "o": np.arange(80, dtype=float) + 100,
            "h": np.arange(80, dtype=float) + 102,
            "l": np.arange(80, dtype=float) + 98,
            "c": np.arange(80, dtype=float) + 101,
            "v": 1.0,
        })
        first = calibration._derive_indicators(base)
        changed = base.copy()
        changed.loc[79, ["o", "h", "l", "c"]] = [999.0, 1001.0, 998.0, 1000.0]
        second = calibration._derive_indicators(changed)
        for name in ("ma5", "ma20", "atr14", "rsi14", "macd_hist"):
            self.assertAlmostEqual(float(first.loc[70, name]), float(second.loc[70, name]), places=12)

    def test_time_splits_have_four_hour_purge(self) -> None:
        frame = pd.DataFrame({
            "obs_ts": pd.date_range("2026-07-20", "2026-08-11 15:00", freq="1h", tz="UTC")
        })
        masks, contract = calibration._split_masks(frame)
        train_last = frame.loc[masks["train"], "obs_ts"].max()
        calibration_first = frame.loc[masks["calibration"], "obs_ts"].min()
        calibration_last = frame.loc[masks["calibration"], "obs_ts"].max()
        test_first = frame.loc[masks["test"], "obs_ts"].min()
        self.assertGreaterEqual(calibration_first - train_last, pd.Timedelta(hours=5))
        self.assertGreaterEqual(test_first - calibration_last, pd.Timedelta(hours=5))
        self.assertEqual(contract["purge_hours"], "4")

    def test_threshold_target_requires_sample_and_time_diversity(self) -> None:
        count = 200
        frame = pd.DataFrame({
            "probability": np.linspace(0.99, 0.70, count),
            "success": np.r_[np.ones(190), np.zeros(10)],
            "obs_ts": pd.date_range("2026-08-01", periods=count, freq="1h", tz="UTC"),
            "side": ["long"] * count,
            "horizon": ["1H"] * count,
            "signed_return_after_cost": np.r_[np.full(190, 0.01), np.full(10, -0.01)],
        })
        threshold, metrics = calibration._choose_threshold(frame, min_n=100)
        self.assertLessEqual(threshold, 0.99)
        self.assertEqual(metrics["selection_status"], "target_reached_on_calibration")
        self.assertGreaterEqual(metrics["n"], 100)
        self.assertGreaterEqual(metrics["precision"], 0.90)
        self.assertGreaterEqual(metrics["distinct_days"], 4)

    def test_best_per_observation_rejects_right_censored_candidate_sets(self) -> None:
        frames = []
        for horizon in calibration.TIMEFRAMES:
            for side in ("long", "short"):
                rows = [{
                    "obs_id": 1,
                    "probability": 0.7 if horizon == "4H" else 0.5,
                    "horizon": horizon,
                    "side": side,
                }]
                if horizon == "15m":
                    rows.append({
                        "obs_id": 2,
                        "probability": 0.99,
                        "horizon": horizon,
                        "side": side,
                    })
                frames.append(pd.DataFrame(rows))
        best = calibration._best_per_observation(frames)
        self.assertEqual([1], best["obs_id"].tolist())
        self.assertEqual("4H", best.iloc[0]["horizon"])

    def test_research_panel_preserves_split_and_immature_labels(self) -> None:
        rows = 3
        data = {
            "obs_id": [1, 2, 3],
            "obs_ts": pd.date_range(
                "2026-08-01", periods=rows, freq="1h", tz="UTC"),
            "decision_ts": pd.date_range(
                "2026-08-01 00:01", periods=rows, freq="1h", tz="UTC"),
            "entry_ts": pd.date_range(
                "2026-08-01 00:15", periods=rows, freq="1h", tz="UTC"),
            "symbol": ["A-USDT-SWAP", "B-USDT-SWAP", "C-USDT-SWAP"],
            "asset_class": ["crypto"] * rows,
            "rule_direction": ["long", "short", "wait"],
            "feature_a": [0.1, 0.2, 0.3],
        }
        for timeframe in calibration.TIMEFRAMES:
            data[f"{timeframe}_return"] = [0.01, -0.01, None]
            data[f"{timeframe}_long_success"] = [1.0, 0.0, None]
            data[f"{timeframe}_short_success"] = [0.0, 1.0, None]
        frame = pd.DataFrame(data)
        masks = {
            "train": pd.Series([True, False, False]),
            "calibration": pd.Series([False, True, False]),
            "test": pd.Series([False, False, True]),
        }
        panel = calibration._research_panel(frame, masks, ("feature_a",))
        self.assertEqual(["train", "calibration", "test"], panel["split"].tolist())
        self.assertTrue(pd.isna(panel.loc[2, "4H_long_success"]))
        self.assertEqual(rows, panel["obs_id"].nunique())

        duplicate = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
        duplicate_masks = {
            name: pd.Series([*mask.tolist(), False])
            for name, mask in masks.items()
        }
        with self.assertRaisesRegex(ValueError, "obs_id must be unique"):
            calibration._research_panel(
                duplicate, duplicate_masks, ("feature_a",))


if __name__ == "__main__":
    unittest.main()
