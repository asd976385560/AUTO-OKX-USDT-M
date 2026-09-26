# -*- coding: utf-8 -*-
"""共享币种抽取（collectors/sources/_coin_names.py）契约回归。

覆盖两处历史缺陷（"Bitcoin Cash" 抽成 BTC、POLYGON 映射到已下架 MATIC 合约）、
误命中防护，以及各 adapter 确实统一走共享模块。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "collectors" / "sources"
COLLECTORS = ROOT / "collectors"
for _p in (SOURCES, COLLECTORS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _coin_names as cn  # noqa: E402
import _mx_news_common as mx_common  # noqa: E402
import news_blockbeats  # noqa: E402
import news_jinse  # noqa: E402
import news_odaily  # noqa: E402
import news_okx  # noqa: E402
import news_panews  # noqa: E402
import news_rss  # noqa: E402


class MultiWordNameTests(unittest.TestCase):
    def test_bitcoin_cash_maps_to_bch_only(self) -> None:
        self.assertEqual(
            cn.extract_symbols("Bitcoin Cash rallies after halving"),
            ["BCH-USDT-SWAP"])
        self.assertEqual(
            cn.extract_symbols("BITCOIN  CASH hard fork"), ["BCH-USDT-SWAP"])

    def test_ethereum_classic_maps_to_etc_only(self) -> None:
        self.assertEqual(
            cn.extract_symbols("Ethereum Classic miners upgrade"),
            ["ETC-USDT-SWAP"])

    def test_plain_names_still_map_to_parent_chain(self) -> None:
        self.assertEqual(
            cn.extract_symbols("Bitcoin and Ethereum lead the market"),
            ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

    def test_mixed_mentions_keep_both_in_text_order(self) -> None:
        self.assertEqual(
            cn.extract_symbols("Ethereum vs Ethereum Classic vs Bitcoin Cash"),
            ["ETH-USDT-SWAP", "ETC-USDT-SWAP", "BCH-USDT-SWAP"])

    def test_chinese_long_names_win_over_prefixes(self) -> None:
        self.assertEqual(
            cn.extract_symbols("以太经典升级，比特币现金减半"),
            ["BCH-USDT-SWAP", "ETC-USDT-SWAP"])
        self.assertEqual(cn.extract_symbols("比特币突破"), ["BTC-USDT-SWAP"])


class PolygonMappingTests(unittest.TestCase):
    def test_polygon_and_matic_map_to_pol_swap(self) -> None:
        self.assertEqual(cn.extract_symbols("Polygon upgrade"), ["POL-USDT-SWAP"])
        self.assertEqual(cn.extract_symbols("MATIC to POL"), ["POL-USDT-SWAP"])
        self.assertEqual(cn.instrument_for_ticker("matic"), "POL-USDT-SWAP")
        self.assertEqual(cn.instrument_for_ticker("POL"), "POL-USDT-SWAP")
        self.assertIsNone(cn.instrument_for_ticker("NOPE"))
        self.assertIsNone(cn.instrument_for_ticker(None))

    def test_no_delisted_matic_instrument_can_be_produced(self) -> None:
        self.assertNotIn(
            "MATIC-USDT-SWAP",
            cn.extract_symbols("MATIC Polygon matic $MATIC 马蹄"))


class FalsePositiveTests(unittest.TestCase):
    def test_longer_latin_words_do_not_contain_tickers(self) -> None:
        self.assertEqual(cn.extract_symbols("Bitcoinist: BTCUSD outlook"), [])
        self.assertEqual(cn.extract_symbols("ETHEREUMPRICE"), [])

    def test_common_english_words_are_not_lowercase_tickers(self) -> None:
        self.assertEqual(
            cn.extract_symbols(
                "market cap near $2T as traders lit up the pond; a ton of hype"),
            [])

    def test_uppercase_tickers_and_dollar_prefix_still_match(self) -> None:
        self.assertEqual(
            cn.extract_symbols("$btc, $HYPE and NEAR rally"),
            ["BTC-USDT-SWAP", "HYPE-USDT-SWAP", "NEAR-USDT-SWAP"])

    def test_cjk_adjacent_tickers_match(self) -> None:
        self.assertEqual(
            cn.extract_symbols("美国HYPE现货上线，ZEC空单增加，中BTC和"),
            ["HYPE-USDT-SWAP", "ZEC-USDT-SWAP", "BTC-USDT-SWAP"])

    def test_dedup_and_empty_input(self) -> None:
        self.assertEqual(
            cn.extract_symbols("BTC bitcoin 比特币 $BTC"), ["BTC-USDT-SWAP"])
        self.assertEqual(cn.extract_symbols(""), [])
        self.assertEqual(cn.extract_symbols(None), [])

    def test_generic_stablecoin_word_has_no_symbol(self) -> None:
        self.assertEqual(cn.extract_symbols("稳定币监管新规"), [])


class SharedTableConsistencyTests(unittest.TestCase):
    def test_full_name_and_alias_targets_are_canonical_tickers(self) -> None:
        for name, ticker in cn.FULL_NAMES.items():
            self.assertIn(ticker, cn.TICKERS, name)
        for name, ticker in cn.ZH_NAMES.items():
            self.assertIn(ticker, cn.TICKERS, name)
        for old, new in cn.TICKER_ALIASES.items():
            self.assertIn(old, cn.TICKERS, old)
            self.assertIn(new, cn.TICKERS, new)

    def test_adapters_share_the_single_extractor(self) -> None:
        for module in (news_rss, news_jinse, news_panews, news_odaily,
                       news_blockbeats):
            self.assertIs(module._extract_symbols, cn.extract_symbols, module)
        self.assertIs(mx_common._symbols, cn.extract_symbols)
        for module in (news_rss, news_jinse, news_panews, news_odaily,
                       news_blockbeats, news_okx, mx_common):
            for stale in ("_COINS", "_COIN_TO_SYM", "_COIN_RE", "_COIN_TO_SWAP",
                          "_CN_COINS", "_CN_COIN_MAP", "_CN_COIN_TO_SYM",
                          "_ZH_COIN", "_SYMBOLS"):
                self.assertFalse(hasattr(module, stale), f"{module} {stale}")

    def test_okx_ccy_lists_go_through_shared_mapping(self) -> None:
        item = {
            "ccyList": ["matic", "BTC", "UNKNOWNCOIN", "btc"],
            "ccySentiments": [{"ccy": "POL"}, {"ccy": "ETH"}, "junk"],
        }
        self.assertEqual(
            news_okx._symbols_from_item(item),
            ["POL-USDT-SWAP", "BTC-USDT-SWAP", "ETH-USDT-SWAP"])


if __name__ == "__main__":
    unittest.main()
