# -*- coding: utf-8 -*-
"""2026-09-26 对照 V3 news::rules::symbols_in：新闻币名统一登记表与匹配规则。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "collectors", ROOT / "collectors" / "sources"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import _coin_names  # noqa: E402
import _mx_news_common  # noqa: E402
import news_blockbeats  # noqa: E402
import news_jinse  # noqa: E402
import news_odaily  # noqa: E402
import news_okx  # noqa: E402
import news_panews  # noqa: E402
import news_rss  # noqa: E402


class ExtractSymbolsTests(unittest.TestCase):
    def test_longer_names_consume_their_span(self):
        self.assertEqual(
            ["BCH-USDT-SWAP"],
            _coin_names.extract_symbols("Bitcoin Cash halving is near"))
        self.assertEqual(
            ["ETC-USDT-SWAP"],
            _coin_names.extract_symbols("Ethereum Classic upgrade"))
        self.assertEqual(
            ["BCH-USDT-SWAP", "BTC-USDT-SWAP"],
            _coin_names.extract_symbols("Bitcoin Cash and Bitcoin both rally"))

    def test_word_boundaries_and_case_rules(self):
        self.assertEqual([], _coin_names.extract_symbols(
            "whether together nether bitcoinist"))
        self.assertEqual([], _coin_names.extract_symbols(
            "near the link we saw a ton of op ads"))
        self.assertEqual(
            ["NEAR-USDT-SWAP", "LINK-USDT-SWAP"],
            _coin_names.extract_symbols("NEAR partners with LINK oracles"))
        self.assertEqual(
            ["OP-USDT-SWAP", "BTC-USDT-SWAP"],
            _coin_names.extract_symbols("$op and $Btc cashtags"))
        self.assertEqual([], _coin_names.extract_symbols("OP alone is two letters"))
        self.assertEqual(
            ["BTC-USDT-SWAP"],
            _coin_names.extract_symbols("中文里紧贴BTC也能抽到"))

    def test_polygon_maps_to_pol_and_stablecoins_are_dropped(self):
        self.assertEqual(
            ["POL-USDT-SWAP"],
            _coin_names.extract_symbols("Polygon (MATIC) migration completes"))
        self.assertEqual([], _coin_names.extract_symbols("Tether mints 1B USDT and USDC"))
        self.assertEqual("POL-USDT-SWAP", _coin_names.swap_symbol("matic"))
        self.assertIsNone(_coin_names.swap_symbol("USDT"))
        self.assertIsNone(_coin_names.swap_symbol("NOTACOIN"))

    def test_chinese_names_and_limit(self):
        self.assertEqual(
            ["BCH-USDT-SWAP", "DOGE-USDT-SWAP"],
            _coin_names.extract_symbols("比特现金与狗狗币同涨"))
        many = " ".join(sorted(t for t in _coin_names.TICKERS if len(t) >= 3))
        self.assertEqual(_coin_names.MAX_SYMBOLS, len(_coin_names.extract_symbols(many)))
        self.assertEqual([], _coin_names.extract_symbols(None))

    def test_extra_names_are_private_to_the_caller(self):
        self.assertEqual(
            ["OKB-USDT-SWAP", "TRUMP-USDT-SWAP"],
            _mx_news_common._symbols("OKX平台币 上涨，Trump 发言"))
        # 公共表认识 OKX平台币（OKB 是真实永续），但不把英文名 Trump 当币
        self.assertEqual(
            ["OKB-USDT-SWAP"], _coin_names.extract_symbols("OKX平台币 Trump"))


class CollectorDelegationTests(unittest.TestCase):
    def test_every_collector_uses_the_shared_table(self):
        for module in (news_rss, news_jinse, news_panews, news_odaily, news_blockbeats):
            with self.subTest(module=module.__name__):
                self.assertEqual(
                    ["BCH-USDT-SWAP"], module._extract_symbols("Bitcoin Cash rally"))
                self.assertEqual(
                    ["POL-USDT-SWAP"], module._extract_symbols("MATIC to POL"))
                self.assertFalse(hasattr(module, "_COIN_RE"))

    def test_okx_ccy_codes_go_through_the_same_aliases(self):
        self.assertEqual(
            ["POL-USDT-SWAP", "BTC-USDT-SWAP"],
            news_okx._symbols_from_item({
                "ccyList": ["MATIC", "btc", "USDT"],
                "ccySentiments": [{"ccy": "BTC"}, "junk"],
            }))


if __name__ == "__main__":
    unittest.main()
