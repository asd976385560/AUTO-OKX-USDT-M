# -*- coding: utf-8 -*-
"""新闻文本币名 → 合约代码的唯一登记表（2026-09-26 对照 V3 news::rules::symbols_in）。

此前 news_rss / news_jinse / news_panews / news_odaily / news_blockbeats 各存一份
币名表与正则，彼此漂移："Bitcoin Cash" 被记成 BTC、Polygon 仍映射到已下架的
MATIC 合约、near/link/ton 这类英文常用词在不区分大小写下误命中 ticker。

规则（与 V3 一致的部分注明）：
  - 英文全称不区分大小写、按**整词**匹配；长名优先并占住区段，"Bitcoin Cash"
    只记 BCH、"Ethereum Classic" 只记 ETC，whether/together 不再命中 ether（V3 O-01/I-02）；
  - 裸 ticker 只认**全大写且 ≥3 位**（V3：避免 AI/US 之类误伤；NEAR/LINK/TON 小写
    不再命中）；2 位 ticker 只认 $OP 形式的现金标签；
  - $TICKER 现金标签任意大小写、2–10 位（V3 同）；
  - 中文币名子串匹配（无词边界，V3 同）；
  - 稳定币（USDT/USDC）不是可交易永续，不输出；MATIC 归一为 POL（V3 同）；
  - 输出 <BASE>-USDT-SWAP，按出现顺序去重，最多 10 个（V3 同）。
"""
from __future__ import annotations

import re
from typing import Mapping, Optional

SWAP_SUFFIX = "-USDT-SWAP"
MAX_SYMBOLS = 10

# 英文全称（小写）→ base。多词名靠正则按长度降序排列先行占住区段。
NAME_MAP: dict[str, str] = {
    "bitcoin cash": "BCH",
    "ethereum classic": "ETC",
    "binance coin": "BNB",
    "shiba inu": "SHIB",
    "sui network": "SUI",
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "ether": "ETH",
    "solana": "SOL",
    "ripple": "XRP",
    "dogecoin": "DOGE",
    "cardano": "ADA",
    "avalanche": "AVAX",
    "chainlink": "LINK",
    "polygon": "POL",
    "litecoin": "LTC",
    "tether": "USDT",
    "uniswap": "UNI",
    "polkadot": "DOT",
    "toncoin": "TON",
    "aptos": "APT",
    "arbitrum": "ARB",
    "optimism": "OP",
    "tron": "TRX",
    "filecoin": "FIL",
    "monero": "XMR",
    "render": "RNDR",
}

# 裸 ticker（全大写整词）；五处副本的并集。
TICKERS: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "TRX",
    "TON", "DOT", "MATIC", "POL", "SHIB", "LTC", "BCH", "UNI", "AAVE", "ARB",
    "OP", "SUI", "APT", "INJ", "SEI", "TIA", "PEPE", "WIF", "NEAR", "FIL",
    "ATOM", "ETC", "XLM", "ICP", "HBAR", "RNDR", "HYPE", "ZEC", "ORDI", "JUP",
    "STX", "RUNE", "BLUR", "JTO", "PYTH", "FTM", "LIT", "TRUMP", "POPCAT",
    "WBTC", "USDT", "USDC", "POND", "ALCX", "ARDR", "NFP", "CAP", "ARX",
    "POPMART", "XMR", "EOS", "VET", "OKB", "ALLO",
})
# ticker 归一（已更名 / 迁移的合约）。
TICKER_ALIASES: dict[str, str] = {"MATIC": "POL", "RENDER": "RNDR"}
# 不是可交易 USDT 永续的 base，不输出。
EXCLUDED_BASES: frozenset[str] = frozenset({"USDT", "USDC"})

# 中文币名 → base（子串匹配）；五处副本的并集，「稳定币」泛指不映射。
CN_NAMES: dict[str, str] = {
    "比特币": "BTC", "以太坊": "ETH", "以太币": "ETH", "以太": "ETH",
    "索拉纳": "SOL", "瑞波币": "XRP", "瑞波": "XRP", "狗狗币": "DOGE",
    "狗狗": "DOGE", "狗币": "DOGE", "莱特币": "LTC", "卡尔达诺": "ADA",
    "艾达币": "ADA", "艾达": "ADA", "波卡": "DOT", "波场币": "TRX", "波场": "TRX",
    "柚子": "EOS", "门罗币": "XMR", "门罗": "XMR", "雪崩": "AVAX",
    "比特现金": "BCH", "恒星币": "XLM", "恒星": "XLM", "屎币": "SHIB",
    "柴犬币": "SHIB", "柴犬": "SHIB", "佩佩": "PEPE", "大零币": "ZEC",
    "唯链": "VET", "链克": "LINK", "币安币": "BNB", "OKX平台币": "OKB",
    "欧易平台币": "OKB",
}

_ASCII_BOUNDARY_L = r"(?<![A-Za-z0-9])"
_ASCII_BOUNDARY_R = r"(?![A-Za-z0-9])"
_NAME_RE = re.compile(
    _ASCII_BOUNDARY_L
    + "(" + "|".join(re.escape(name) for name in sorted(NAME_MAP, key=len, reverse=True)) + ")"
    + _ASCII_BOUNDARY_R,
    re.IGNORECASE,
)
_TICKER_RE = re.compile(
    _ASCII_BOUNDARY_L
    + "(" + "|".join(sorted((t for t in TICKERS if len(t) >= 3), key=len, reverse=True)) + ")"
    + _ASCII_BOUNDARY_R,
)
_CASHTAG_RE = re.compile(r"\$([A-Za-z0-9]{2,10})" + _ASCII_BOUNDARY_R)


def canonical_base(value: object) -> Optional[str]:
    """ticker / base → 归一后的 base；未知或不可交易 → None。"""
    base = str(value or "").strip().upper()
    if not base:
        return None
    base = TICKER_ALIASES.get(base, base)
    if base in EXCLUDED_BASES:
        return None
    return base


def swap_symbol(value: object) -> Optional[str]:
    """已登记的 ticker / base → <BASE>-USDT-SWAP；未登记、不可交易 → None。"""
    raw = str(value or "").strip().upper()
    if raw not in TICKERS and raw not in TICKER_ALIASES:
        return None
    base = canonical_base(raw)
    return f"{base}{SWAP_SUFFIX}" if base else None


def extract_symbols(text: object, *, extra_names: Optional[Mapping[str, str]] = None,
                    limit: int = MAX_SYMBOLS) -> list[str]:
    """文本 → 按出现顺序去重的 <BASE>-USDT-SWAP 列表（见模块 docstring 的规则）。

    ``extra_names``：调用方私有的补充名表（ASCII 名整词、非 ASCII 名子串）。
    """
    found: list[str] = []
    body = str(text or "")

    def add(base: object) -> None:
        canonical = canonical_base(base)
        if canonical is None:
            return
        symbol = f"{canonical}{SWAP_SUFFIX}"
        if symbol not in found:
            found.append(symbol)

    # 各路命中先按出现位置排序再登记：symbol（主币）= 文本里最先出现的那个。
    hits: list[tuple[int, int, str]] = []
    for match in _NAME_RE.finditer(body):
        hits.append((match.start(), 0, NAME_MAP[match.group(1).lower()]))
    for match in _CASHTAG_RE.finditer(body):
        token = match.group(1).upper()
        if token in TICKERS or token in TICKER_ALIASES:
            hits.append((match.start(), 1, token))
    for match in _TICKER_RE.finditer(body):
        hits.append((match.start(), 2, match.group(1)))
    for name, base in CN_NAMES.items():
        position = body.find(name)
        if position >= 0:
            hits.append((position, 3, base))
    for name, base in (extra_names or {}).items():
        if not name:
            continue
        if name.isascii():
            match = re.search(
                _ASCII_BOUNDARY_L + re.escape(name) + _ASCII_BOUNDARY_R,
                body, re.IGNORECASE)
            if match:
                hits.append((match.start(), 4, base))
        else:
            position = body.find(name)
            if position >= 0:
                hits.append((position, 4, base))
    for _position, _rank, base in sorted(hits):
        add(base)
    return found[:limit]
