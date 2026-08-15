# -*- coding: utf-8 -*-
"""BOLL/OBV 扩展指标与 kline_cache 迁移的契约回归（2026-08-13）。"""
from __future__ import annotations

import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import _kline_indicators as ki  # noqa: E402
import apply_kline_indicator_schema as mig  # noqa: E402

LEGACY_KLINE_DDL = """
CREATE TABLE kline_cache (
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    tf          TEXT NOT NULL CHECK(tf IN ('15m','1H','4H','1D','1W','1M')),
    o           REAL, h REAL, l REAL, c REAL, v REAL,
    ma5         REAL,
    ma20        REAL,
    atr14       REAL,
    rsi14       REAL,
    macd_hist   REAL,
    PRIMARY KEY (ts, symbol, tf)
);
"""


class BollSeriesTests(unittest.TestCase):
    def test_constant_series_has_zero_width_bands(self) -> None:
        closes = [10.0] * 25
        mid, up, dn = ki.boll_series(closes)
        self.assertIsNone(mid[18])  # 窗口不足
        self.assertEqual(mid[19], 10.0)
        self.assertEqual(up[19], 10.0)
        self.assertEqual(dn[19], 10.0)

    def test_known_window_matches_population_std(self) -> None:
        closes = [float(i) for i in range(1, 21)]  # 1..20
        mid, up, dn = ki.boll_series(closes)
        mean = sum(closes) / 20
        var = sum((c - mean) ** 2 for c in closes) / 20
        std = math.sqrt(var)
        self.assertAlmostEqual(mid[19], mean, places=10)
        self.assertAlmostEqual(up[19], mean + 2 * std, places=10)
        self.assertAlmostEqual(dn[19], mean - 2 * std, places=10)

    def test_window_with_missing_close_yields_none(self) -> None:
        # 含缺值的窗口全 None（宁缺勿假）；缺值滑出窗口后恢复计算
        closes = [10.0] * 31
        closes[10] = None
        mid, _up, _dn = ki.boll_series(closes)
        self.assertIsNone(mid[20])  # 窗口 [1..20] 含缺值
        self.assertIsNone(mid[29])  # 窗口 [10..29] 含缺值
        self.assertEqual(mid[30], 10.0)  # 窗口 [11..30] 干净


class ObvSeriesTests(unittest.TestCase):
    def test_directional_accumulation(self) -> None:
        closes = [10.0, 11.0, 11.0, 10.0, 12.0]
        volumes = [100.0, 50.0, 30.0, 20.0, 10.0]
        obv = ki.obv_series(closes, volumes)
        # 起点 0；升 +50；平不变；降 -20；升 +10
        self.assertEqual(obv, [0.0, 50.0, 50.0, 30.0, 40.0])

    def test_missing_values_do_not_fabricate(self) -> None:
        closes = [10.0, None, 12.0, 13.0]
        volumes = [100.0, 50.0, None, 25.0]
        obv = ki.obv_series(closes, volumes)
        self.assertEqual(obv[0], 0.0)
        self.assertIsNone(obv[1])           # close 缺 → None
        self.assertEqual(obv[2], 0.0)       # volume 缺 → 累计不变
        self.assertEqual(obv[3], 25.0)

    def test_extend_with_boll_obv_adds_keys(self) -> None:
        candles = [{"c": 10.0 + i * 0.1, "v": 5.0} for i in range(25)]
        out = ki.extend_with_boll_obv(candles)
        self.assertIn("boll20_mid", out[-1])
        self.assertIn("obv", out[-1])
        self.assertIsNotNone(out[-1]["boll20_mid"])
        self.assertIsNotNone(out[-1]["obv"])
        self.assertIsNone(out[0]["boll20_mid"])  # 窗口不足


class MigrationAwareWriteTests(unittest.TestCase):
    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.executescript(LEGACY_KLINE_DDL)
        return con

    def test_plan_legacy_then_extended(self) -> None:
        con = self._con()
        plan = ki.kline_insert_plan(con)
        self.assertFalse(plan["extended"])
        self.assertNotIn("boll20_mid", plan["sql"])
        # 旧 13 列写入可用
        con.execute(plan["sql"], (
            "2026-08-13T00:00:00Z", "BTC-USDT-SWAP", "15m",
            1, 2, 0.5, 1.5, 100, None, None, None, None, None))
        # 迁移
        for col in ki.EXTENDED_COLUMNS:
            con.execute(f"ALTER TABLE kline_cache ADD COLUMN {col} REAL")
        plan2 = ki.kline_insert_plan(con)
        self.assertTrue(plan2["extended"])
        row = (
            "2026-08-13T00:15:00Z", "BTC-USDT-SWAP", "15m",
            1, 2, 0.5, 1.5, 100, None, None, None, None, None,
        ) + ki.extended_row_tail(
            {"boll20_mid": 1.4, "boll20_up": 1.6, "boll20_dn": 1.2, "obv": 7.0})
        con.execute(plan2["sql"], row)
        got = con.execute(
            "SELECT boll20_mid, boll20_up, boll20_dn, obv FROM kline_cache "
            "WHERE ts='2026-08-13T00:15:00Z'").fetchone()
        self.assertEqual(tuple(got), (1.4, 1.6, 1.2, 7.0))
        con.close()

    def test_migration_script_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.db"
            con = sqlite3.connect(db)
            con.executescript(LEGACY_KLINE_DDL)
            con.commit()
            con.close()

            argv_backup = sys.argv
            try:
                # dry-run 不改库
                sys.argv = ["x", "--db", str(db)]
                self.assertEqual(mig.main(), 0)
                con = sqlite3.connect(db)
                self.assertFalse(ki.kline_insert_plan(con)["extended"])
                con.close()
                # apply requires a verified public backup; repeated apply is
                # then an idempotent no-op and must not need another write.
                backup_dir = Path(tmp) / "backups"
                sys.argv = [
                    "x", "--db", str(db), "--apply",
                    "--backup-dir", str(backup_dir),
                ]
                self.assertEqual(mig.main(), 0)
                self.assertEqual(mig.main(), 0)
                self.assertEqual(1, len(list(backup_dir.glob("*.db"))))
            finally:
                sys.argv = argv_backup
            con = sqlite3.connect(db)
            plan = ki.kline_insert_plan(con)
            self.assertTrue(plan["extended"])
            con.close()

    def test_collectors_share_single_implementation(self) -> None:
        # 两个采集器都必须经 _kline_indicators（防口径漂移复发）
        for name in ("collect_data.py", "collect_slow.py"):
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("extend_with_boll_obv", text, name)
            self.assertIn("kline_insert_plan", text, name)
            self.assertNotIn(
                '"INSERT OR REPLACE INTO kline_cache "', text,
                f"{name} 必须使用 kline_insert_plan 的 SQL（migration-aware）")


if __name__ == "__main__":
    unittest.main()
