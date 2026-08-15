# -*- coding: utf-8 -*-
"""日报规格书四段（市场总览/全市场扫描/数据完善率/次日关注）契约回归。

writer 侧：确定性回读块降级安全（库缺→显式不可用文案，不抛）；
validator 侧：激活边界 2026-08-14 起硬性要求四段与 focus 非空，历史归档不反向加责。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import daily_report_writer as drw  # noqa: E402
import validate_daily_report as vdr  # noqa: E402


def _make_ledger(path: Path, rows) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE collection_runs (
            cycle_id TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
            ts TEXT NOT NULL, rows INTEGER, latency_ms INTEGER, err TEXT,
            PRIMARY KEY (cycle_id, source)
        );
        """
    )
    con.executemany(
        "INSERT INTO collection_runs (cycle_id,source,status,ts) "
        "VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()


def _make_market(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tick_snapshots (
            ts TEXT NOT NULL, symbol TEXT NOT NULL, last REAL, bid REAL,
            ask REAL, vol24h REAL, fundingRate REAL, oi REAL, chg24h REAL
        );
        """
    )
    con.executemany(
        "INSERT INTO tick_snapshots (ts,symbol,last,chg24h) VALUES (?,?,?,?)",
        [
            ("2026-08-13T23:45:00Z", "BTC-USDT-SWAP", 98000.0, 1.25),
            ("2026-08-13T23:45:00Z", "ETH-USDT-SWAP", 4200.0, -0.5),
            ("2026-08-13T23:45:00Z", "SOL-USDT-SWAP", 200.0, 3.0),
        ])
    con.commit()
    con.close()


def _make_regime(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE cross_market (
            ts TEXT PRIMARY KEY, regime TEXT, btc_dominance REAL,
            total_mcap_usd REAL, fear_greed REAL, fear_greed_label TEXT,
            defillama_tvl_total REAL
        );
        """
    )
    con.execute(
        "INSERT INTO cross_market VALUES "
        "('2026-08-13T23:00:00Z','trend_up',58.3,3.61e12,64,'Greed',1.42e11)")
    con.commit()
    con.close()


def _make_analysis(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE analysis_runs (cycle_id TEXT PRIMARY KEY, ts TEXT,
            status TEXT);
        CREATE TABLE analysis_signals (cycle_id TEXT, symbol TEXT,
            action TEXT);
        """
    )
    con.executemany(
        "INSERT INTO analysis_runs VALUES (?,?,?)",
        [
            ("2026-08-13T20:00", "2026-08-13 20:03:00", "ok"),
            ("2026-08-13T21:00", "2026-08-13 21:03:00", "skipped"),
        ])
    con.executemany(
        "INSERT INTO analysis_signals VALUES (?,?,?)",
        [
            ("2026-08-13T20:00", "BTC-USDT-SWAP", "hold"),
            ("2026-08-13T20:00", "ETH-USDT-SWAP", "open_long"),
        ])
    con.commit()
    con.close()


WINDOW_START = "2026-08-13 08:00:00"
WINDOW_END = "2026-08-14 08:00:00"


class WriterBlocksTests(unittest.TestCase):
    def test_completeness_block_counts_window_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_ledger(root / "ledger.db", [
                ("2026-08-13T20:00", "fast", "ok", "2026-08-13 20:01:00"),
                ("2026-08-13T20:00", "news", "degraded", "2026-08-13 20:02:00"),
                ("2026-08-13T21:00", "fast", "error", "2026-08-13 21:01:00"),
                # 窗口外行不计入
                ("2026-08-12T07:00", "fast", "error", "2026-08-12 07:01:00"),
            ])
            block = drw._data_completeness_block(root, WINDOW_START, WINDOW_END)
        self.assertIn("2/3=66.7%", block)
        self.assertIn("⚠️ fast: 失败/超时 1/2 次", block)
        self.assertIn("99%", block)

    def test_completeness_block_degrades_without_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            block = drw._data_completeness_block(
                Path(tmp), WINDOW_START, WINDOW_END)
        self.assertIn("数据完善率不可用", block)

    def test_market_overview_reads_snapshot_and_cross_market(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_market(root / "market.db")
            _make_regime(root / "regime.db")
            block = drw._market_overview_block(root, "2026-08-14 08:00:00")
        self.assertIn("BTC $98,000", block)
        self.assertIn("ETH $4,200", block)
        self.assertIn("总市值 $3.61T", block)
        self.assertIn("BTC.D 58.30%", block)
        self.assertIn("恐贪指数 64/Greed", block)
        self.assertIn("regime=trend_up", block)

    def test_market_overview_degrades_without_dbs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            block = drw._market_overview_block(
                Path(tmp), "2026-08-14 08:00:00")
        self.assertIn("不可用", block)

    def test_universe_scan_counts_analysis_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_market(root / "market.db")
            _make_analysis(root / "analysis.db")
            block = drw._universe_scan_block(root, WINDOW_START, WINDOW_END)
        self.assertIn("采集宇宙：3 个 USDT 线性永续", block)
        self.assertIn("分析轮次：2 轮（skip/stale 1）", block)
        self.assertIn("信号 2 条 / 覆盖 2 个标的", block)
        self.assertIn("open_long=1", block)
        self.assertIn("非校准概率", block)

    def test_cst_to_utc_z_conversion(self) -> None:
        self.assertEqual(
            drw._cst_to_utc_z("2026-08-14 08:00:00"), "2026-08-14T00:00:00Z")
        self.assertIsNone(drw._cst_to_utc_z("bad"))


class ValidatorGatingTests(unittest.TestCase):
    def _content(self, ts: str, with_sections: bool, focus: str) -> str:
        head = (
            f"# 📊 小灵日报 {ts[:10]}\n\n> ts: {ts}\n\n"
        )
        if not with_sections:
            return head
        return head + (
            "## 🛰 全市场扫描\n\n- 采集宇宙：3 个\n\n"
            "## 📡 数据完善率\n\n- 总体：完善率 96/96=100.0%\n\n"
            "## 🌍 市场\n\n### 市场总览（writer 权威回读）\n\n- BTC $98,000\n\n"
            f"## 🔭 次日关注\n\n{focus}\n\n"
            "## 🧠 教训\n\n无\n"
        )

    def _spec_errors(self, content: str) -> list[str]:
        """只提取激活闸相关错误（复用 validator 的实现逻辑做单元级验证）。"""
        errors: list[str] = []
        report_ts = vdr._extract_report_ts(content)
        assert report_ts
        if report_ts >= vdr.SPEC_SECTIONS_ACTIVATION_TS:
            for marker in vdr.SPEC_SECTION_MARKERS:
                if marker not in content:
                    errors.append(f"missing {marker}")
            focus_body = vdr._section_body(content, "## 🔭 次日关注")
            if focus_body is not None:
                cleaned = focus_body.strip()
                if not cleaned or vdr._FOCUS_PLACEHOLDER in cleaned:
                    errors.append("focus empty")
        return errors

    def test_pre_activation_reports_are_not_penalized(self) -> None:
        content = self._content("2026-08-10 08:05:00", with_sections=False,
                                focus="")
        self.assertEqual(self._spec_errors(content), [])

    def test_post_activation_requires_sections_and_focus(self) -> None:
        missing = self._content("2026-08-14 08:05:00", with_sections=False,
                                focus="")
        self.assertEqual(len(self._spec_errors(missing)),
                         len(vdr.SPEC_SECTION_MARKERS))

        placeholder = self._content(
            "2026-08-14 08:05:00", with_sections=True,
            focus="未填写（激活边界后 validator 将拒绝外发）")
        self.assertEqual(self._spec_errors(placeholder), ["focus empty"])

        good = self._content(
            "2026-08-14 08:05:00", with_sections=True,
            focus="- BTC 4H MA20 得失\n- CPI 事件窗 ±4H 谨慎开新仓")
        self.assertEqual(self._spec_errors(good), [])

    def test_section_body_extraction_stops_at_next_section(self) -> None:
        content = self._content(
            "2026-08-14 08:05:00", with_sections=True,
            focus="- A\n- B")
        body = vdr._section_body(content, "## 🔭 次日关注")
        self.assertIn("- A", body)
        self.assertNotIn("教训", body)

    def test_activation_boundary_matches_writer_constant(self) -> None:
        self.assertEqual(
            vdr.SPEC_SECTIONS_ACTIVATION_TS,
            drw.SPEC_SECTIONS_ACTIVATION_TS)


if __name__ == "__main__":
    unittest.main()
