# -*- coding: utf-8 -*-
"""持仓行 SL 双口径（现价缓冲 + 开仓计划距）契约回归（2026-08-13）。

背景：旧 `SL距X%` = |sl−开仓均价|/开仓均价——entry 与 SL 冻结故随价格恒定，
被误读为"现价距止损"。双口径修复：现价缓冲随行情变动、≤0 显式标注；
计划口径保留并改名明确语义；缓冲不可得宁缺勿假回退计划口径。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import build_push_payload as bpp  # noqa: E402
import render_push_report as rpr  # noqa: E402

CST = timezone(timedelta(hours=8))


def _portable_connect(db_root, name):
    """生产 connect 用 Windows 反斜杠拼 URI；测试环境用 Path 组装等价只读连接。"""
    path = (Path(db_root) / name).as_posix()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


class SlBufferMathTests(unittest.TestCase):
    def test_direction_aware_buffer(self) -> None:
        # long: mark 8.697, sl 8.25 → (8.697-8.25)/8.697 = 5.1%
        self.assertEqual(bpp._sl_buffer_pct("long", 8.697, 8.25), 5.1)
        # short: sl 在上方
        self.assertEqual(bpp._sl_buffer_pct("short", 100.0, 105.0), 5.0)
        # 价格已越过触发边界 → 负值（瞬时状态，render 显式标注）
        self.assertEqual(bpp._sl_buffer_pct("long", 8.2, 8.25), -0.6)

    def test_missing_or_invalid_inputs_return_none(self) -> None:
        self.assertIsNone(bpp._sl_buffer_pct("long", None, 8.25))
        self.assertIsNone(bpp._sl_buffer_pct("long", 8.697, None))
        self.assertIsNone(bpp._sl_buffer_pct("net", 8.697, 8.25))
        self.assertIsNone(bpp._sl_buffer_pct("long", 0, 8.25))
        self.assertIsNone(bpp._sl_buffer_pct("long", "bad", 8.25))

    def test_planned_distance_semantics_unchanged(self) -> None:
        # 计划口径与历史一致：|sl−avg|/avg（与现价无关，语义即如此）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "live_trades.db")
            con.executescript(
                "CREATE TABLE trades (ts TEXT, action TEXT, symbol TEXT, raw TEXT);")
            con.execute(
                "INSERT INTO trades VALUES (?,?,?,?)",
                ("2026-08-11 16:45:00", "open", "LINK-USDT-SWAP",
                 '{"sl_trigger_px": 8.25}'))
            con.commit()
            con.close()
            with mock.patch.object(bpp, "connect", _portable_connect):
                pct, sl_px = bpp._open_sl_info(
                    root, "live", "LINK-USDT-SWAP", 8.47382)
        self.assertEqual(pct, 2.6)
        self.assertEqual(sl_px, 8.25)

    def test_open_sl_info_without_sl_returns_none_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "live_trades.db")
            con.executescript(
                "CREATE TABLE trades (ts TEXT, action TEXT, symbol TEXT, raw TEXT);")
            con.commit()
            con.close()
            with mock.patch.object(bpp, "connect", _portable_connect):
                self.assertEqual(
                    bpp._open_sl_info(root, "live", "LINK-USDT-SWAP", 8.47),
                    (None, None))


class FreshTickTests(unittest.TestCase):
    def _root_with_tick(self, tick_ts_utc: str):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        con = sqlite3.connect(root / "market.db")
        con.executescript(
            "CREATE TABLE tick_snapshots (ts TEXT, symbol TEXT, last REAL);")
        con.execute("INSERT INTO tick_snapshots VALUES (?,?,?)",
                    (tick_ts_utc, "LINK-USDT-SWAP", 8.697))
        con.commit()
        con.close()
        return tmp, root

    def test_fresh_tick_is_used_and_stale_is_rejected(self) -> None:
        as_of = datetime(2026, 8, 13, 12, 30, tzinfo=CST)  # = 04:30 UTC
        with mock.patch.object(bpp, "connect", _portable_connect):
            tmp, root = self._root_with_tick("2026-08-13T04:15:02Z")  # 15min 前
            try:
                self.assertEqual(
                    bpp._latest_fresh_last(root, "LINK-USDT-SWAP", as_of), 8.697)
            finally:
                tmp.cleanup()
            tmp, root = self._root_with_tick("2026-08-13T03:30:00Z")  # 60min 前 → 过旧
            try:
                self.assertIsNone(
                    bpp._latest_fresh_last(root, "LINK-USDT-SWAP", as_of))
            finally:
                tmp.cleanup()

    def test_future_tick_relative_to_as_of_is_excluded(self) -> None:
        # 历史重渲染：as_of 之后的 tick 不得泄漏进当时的缓冲
        as_of = datetime(2026, 8, 13, 4, 0, tzinfo=CST)  # = 前一日 20:00 UTC
        with mock.patch.object(bpp, "connect", _portable_connect):
            tmp, root = self._root_with_tick("2026-08-13T04:15:02Z")
            try:
                self.assertIsNone(
                    bpp._latest_fresh_last(root, "LINK-USDT-SWAP", as_of))
            finally:
                tmp.cleanup()


class RenderDualMetricTests(unittest.TestCase):
    BASE = {
        "profile": "live", "symbol": "LINK-USDT-SWAP", "side": "多",
        "sz": 100.0, "avgPx": 8.47382, "lev": 10, "upl": 25.62,
        "hold_min": 2630,
    }

    def test_buffer_and_planned_render_together(self) -> None:
        line = rpr.format_position({**self.BASE, "sl_pct": 2.6,
                                    "sl_buffer_pct": 5.1})
        self.assertIn("SL缓冲(现价)5.1%|计划距(开仓)2.6%", line)
        self.assertNotIn("SL距2.6%", line)  # 旧歧义标签退役

    def test_breached_buffer_is_flagged_not_hidden(self) -> None:
        line = rpr.format_position({**self.BASE, "sl_pct": 2.6,
                                    "sl_buffer_pct": -0.6})
        self.assertIn("SL缓冲(现价)≤0(已到触发边界)", line)
        self.assertIn("计划距(开仓)2.6%", line)

    def test_missing_buffer_falls_back_to_planned_only(self) -> None:
        line = rpr.format_position({**self.BASE, "sl_pct": 2.6})
        self.assertIn("计划SL距(开仓)2.6%", line)
        self.assertNotIn("SL缓冲", line)

    def test_anomalous_values_keep_guards(self) -> None:
        # 计划距 >30% 仍标核对；缓冲 >50% 标核对 markPx
        line = rpr.format_position({**self.BASE, "sl_pct": 44.0,
                                    "sl_buffer_pct": 61.0})
        self.assertIn("值异常,核对slTriggerPx", line)
        self.assertIn("SL缓冲(现价)61%(值异常,核对markPx)", line)

    def test_no_sl_still_renders_honest_missing(self) -> None:
        line = rpr.format_position(dict(self.BASE))
        self.assertIn("SL未挂", line)
        # bool 不得冒充数值缓冲
        line2 = rpr.format_position({**self.BASE, "sl_pct": 2.6,
                                     "sl_buffer_pct": True})
        self.assertIn("计划SL距(开仓)2.6%", line2)

    def test_margin_return_and_locked_profit_are_explicit(self) -> None:
        line = rpr.format_position({
            **self.BASE,
            "sl_pct": 2.6,
            "sl_buffer_pct": 2.8,
            "upl_pct_initial_margin": 116.4974,
            "secured_profit_at_stop_usdt": 72.6178,
            "giveback_to_stop_pct_of_current_upl": 26.439,
        })
        self.assertIn("保证金收益率+116.5%", line)
        self.assertIn("SL锁盈≈$72.62", line)
        self.assertIn("到SL将回吐当前浮盈26.4%", line)


if __name__ == "__main__":
    unittest.main()
