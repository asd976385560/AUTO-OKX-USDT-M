# -*- coding: utf-8 -*-
"""新闻 adapter 共享的币种名称抽取（确定性、零 LLM）。

原先 news_rss / news_jinse / news_panews / news_odaily / news_blockbeats /
news_okx / _mx_news_common 各自维护一份币种列表与正则，彼此漂移且带两处已知
缺陷（"Bitcoin Cash" 被抽成 BTC、POLYGON 仍映射到已下架的 MATIC-USDT-SWAP）。
本模块把它们合并成唯一的规范表与一个 ``extract_symbols`` 入口，并于 2026-09-26
对照 V3 news::rules::symbols_in 补齐了裸 ticker 长度、稳定币、排序与上限规则。
``news_okx_announcements.py`` 的 symbol 提取依赖公告标题结构，刻意不走本模块。

匹配规则
--------
* **英文全称**（``FULL_NAMES``）：大小写不敏感、整词匹配；多词名（"Bitcoin Cash"、
  "Ethereum Classic"）按长度倒序排在单词名之前并占住区段，故 "Bitcoin Cash"
  只产出 BCH，不再顺带产出 BTC；"whether" / "together" 也不会命中 "ether"。
* **裸 ticker**（``TICKERS``）：只匹配**全大写且至少 MIN_BARE_TICKER_LEN（3）位**的
  原文（V3 同口径：AI / US / OP 之类两字母大写词太容易误伤）。旧实现对 ticker 也
  大小写不敏感，合并列表后 "market cap"、"near"、"lit"、"pond" 等普通英文词会全部
  误命中 CAP/NEAR/LIT/POND，因此改为大小写敏感。
* **现金标签**：``$btc`` / ``$op`` 这类带 ``$`` 前缀的显式写法大小写不敏感，两字母
  ticker 只能以这种形式命中。
* **边界**：两侧不得是 ASCII 字母/数字（``\\b`` 在 CJK 相邻处失效，见
  news_panews 注释），因此 "美国HYPE现货" 命中而 "Bitcoinist" 不命中。
* **中文币名**（``ZH_NAMES``）：子串匹配，按长度倒序并在命中后从文本中扣除，
  故 "以太经典" 只产出 ETC、"比特币现金" 只产出 BCH。
* **别名**（``TICKER_ALIASES``）：已改名/迁移的 ticker 统一到当前 OKX 合约，
  目前只有 MATIC → POL（POLYGON / MATIC 均映射 POL-USDT-SWAP）。
* **稳定币**（``STABLECOINS``）：USDT / USDC 不是可交易的 USDT 永续；登记表认识
  它们（"泰达币"、OKX ccyList 里的 USDT）但从不输出合约（V3 同）。

返回值为 ``<TICKER>-USDT-SWAP`` 列表：英文名、ticker、中文名统一按**首次出现位置**
排序去重（首项即 news_writer 采用的主币），最多 ``MAX_SYMBOLS``（10）个（V3 同）。
"""
from __future__ import annotations

import re

# ── 规范 ticker 集合（各 adapter 原列表的并集）───────────────────────────────
TICKERS: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "TRX",
    "TON", "DOT", "MATIC", "POL", "SHIB", "LTC", "BCH", "UNI", "AAVE", "ARB",
    "OP", "SUI", "APT", "INJ", "SEI", "TIA", "PEPE", "WIF", "NEAR", "FIL",
    "ATOM", "ETC", "XLM", "ICP", "HBAR", "RNDR", "HYPE", "ZEC", "LIT", "ORDI",
    "BLUR", "JTO", "PYTH", "STX", "RUNE", "FTM", "JUP", "TRUMP", "POPCAT",
    "WBTC", "USDT", "USDC", "POND", "ALCX", "ARDR", "NFP", "CAP", "ARX",
    "POPMART", "OKB", "ALLO", "EOS", "XMR", "VET",
})

# 已改名 / 迁移的 ticker → 当前 OKX 合约 ticker
TICKER_ALIASES: dict[str, str] = {
    "MATIC": "POL",
}

# 登记表认识、但不是可交易 USDT 永续的 ticker：抽取与 ccy 映射都不输出（V3 同）
STABLECOINS: frozenset[str] = frozenset({"USDT", "USDC"})

# 裸 ticker（无 $ 前缀）至少要这么多位全大写字母才算币（V3 同）
MIN_BARE_TICKER_LEN = 3
# 单条文本最多输出的合约数（V3 同）
MAX_SYMBOLS = 10

# ── 英文全称 → ticker（大小写不敏感；多词名用单个空格分隔）───────────────────
FULL_NAMES: dict[str, str] = {
    "BITCOIN CASH": "BCH",
    "ETHEREUM CLASSIC": "ETC",
    "BINANCE COIN": "BNB",
    "SHIBA INU": "SHIB",
    "NEAR PROTOCOL": "NEAR",
    "INTERNET COMPUTER": "ICP",
    "BITCOIN": "BTC",
    "ETHEREUM": "ETH",
    "ETHER": "ETH",
    "SOLANA": "SOL",
    "RIPPLE": "XRP",
    "DOGECOIN": "DOGE",
    "CARDANO": "ADA",
    "AVALANCHE": "AVAX",
    "CHAINLINK": "LINK",
    "TRON": "TRX",
    "TONCOIN": "TON",
    "POLKADOT": "DOT",
    "POLYGON": "POL",
    "LITECOIN": "LTC",
    "UNISWAP": "UNI",
    "ARBITRUM": "ARB",
    "OPTIMISM": "OP",
    "APTOS": "APT",
    "INJECTIVE": "INJ",
    "CELESTIA": "TIA",
    "FILECOIN": "FIL",
    "HEDERA": "HBAR",
    "RENDER": "RNDR",
    "HYPERLIQUID": "HYPE",
    "ZCASH": "ZEC",
    "STACKS": "STX",
    "FANTOM": "FTM",
    "MONERO": "XMR",
    # 项目名与 ticker 同形且不是常见英文词的，允许 Title-case 写法（"Aave"、"Pepe"）
    "AAVE": "AAVE",
    "PEPE": "PEPE",
    "SUI": "SUI",
    "ORDI": "ORDI",
    "PYTH": "PYTH",
    "POPCAT": "POPCAT",
}

# ── 中文币名 → ticker（子串匹配）────────────────────────────────────────────
# 「稳定币」等泛指词不映射具体符号，故不收录。
ZH_NAMES: dict[str, str] = {
    "比特币现金": "BCH", "比特现金": "BCH", "以太经典": "ETC",
    "比特币": "BTC", "以太坊": "ETH", "以太币": "ETH", "以太": "ETH",
    "索拉纳": "SOL", "瑞波币": "XRP", "瑞波": "XRP",
    "狗狗币": "DOGE", "狗狗": "DOGE", "狗币": "DOGE",
    "莱特币": "LTC", "卡尔达诺": "ADA", "艾达币": "ADA", "艾达": "ADA",
    "波卡": "DOT", "波场币": "TRX", "波场": "TRX", "链克": "LINK",
    "柴犬币": "SHIB", "柴犬": "SHIB", "屎币": "SHIB", "佩佩": "PEPE",
    "柚子": "EOS", "门罗币": "XMR", "门罗": "XMR", "大零币": "ZEC",
    "雪崩": "AVAX", "币安币": "BNB", "恒星币": "XLM", "恒星": "XLM",
    "唯链": "VET", "泰达币": "USDT",
    "OKX平台币": "OKB", "欧易平台币": "OKB",
}


def _alternation(words: list[str]) -> str:
    """按长度倒序拼 alternation：保证多词/更长名称优先于其前缀。"""
    parts = []
    for word in sorted(words, key=len, reverse=True):
        parts.append(re.escape(word).replace(r"\ ", r"\s+"))
    return "|".join(parts)


_DOLLAR_TICKER_ALT = _alternation(sorted(TICKERS))
_BARE_TICKER_ALT = _alternation(
    sorted(t for t in TICKERS if len(t) >= MIN_BARE_TICKER_LEN))
_NAME_ALT = _alternation(list(FULL_NAMES))

# 单条正则：全称（不敏感）→ $ticker（不敏感，任意长度）→ 裸 ticker（大写敏感，≥3 位）。
# 边界：两侧不能是 ASCII 字母数字；``$`` 前也不能紧贴字母数字（"US$BTC" 不算）。
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9$])(?:"
    r"(?P<name>(?i:" + _NAME_ALT + r"))"
    r"|\$(?P<dollar>(?i:" + _DOLLAR_TICKER_ALT + r"))"
    r"|(?P<ticker>" + _BARE_TICKER_ALT + r")"
    r")(?![A-Za-z0-9])"
)

_ZH_ORDERED = sorted(ZH_NAMES, key=len, reverse=True)


def canonical_ticker(ticker: str | None) -> str | None:
    """规范化 ticker：大写、套用别名；不在规范集合内或是稳定币则返回 None。"""
    tok = str(ticker or "").strip().upper()
    if not tok:
        return None
    tok = TICKER_ALIASES.get(tok, tok)
    if tok not in TICKERS or tok in STABLECOINS:
        return None
    return tok


def instrument_for_ticker(ticker: str | None) -> str | None:
    """ticker → ``<TICKER>-USDT-SWAP``；未知 ticker / 稳定币返回 None。"""
    tok = canonical_ticker(ticker)
    return f"{tok}-USDT-SWAP" if tok else None


def _latin_hits(text: str) -> list[tuple[int, int, str]]:
    """英文全称 / $ticker / 裸 ticker 命中：(位置, 次序, 原始 ticker)。"""
    hits: list[tuple[int, int, str]] = []
    for m in _TOKEN_RE.finditer(text):
        if m.group("name") is not None:
            key = " ".join(m.group("name").upper().split())
            tok = FULL_NAMES.get(key)
        elif m.group("dollar") is not None:
            tok = m.group("dollar")
        else:
            tok = m.group("ticker")
        if tok:
            hits.append((m.start(), 0, tok))
    return hits


def _zh_hits(text: str) -> list[tuple[int, int, str]]:
    """中文币名命中（长名优先，命中后按原长度扣除以保持位置不变）。"""
    hits: list[tuple[int, int, str]] = []
    rest = text
    for zh in _ZH_ORDERED:
        position = rest.find(zh)
        if position < 0:
            continue
        hits.append((position, 1, ZH_NAMES[zh]))
        rest = rest.replace(zh, " " * len(zh))
    return hits


def extract_tickers(text: str | None) -> list[str]:
    """从文本抽取规范 ticker 列表（按首次出现位置去重，最多 MAX_SYMBOLS 个）。"""
    body = text or ""
    found: list[str] = []
    for _position, _rank, raw in sorted(_latin_hits(body) + _zh_hits(body)):
        tok = canonical_ticker(raw)
        if tok and tok not in found:
            found.append(tok)
            if len(found) >= MAX_SYMBOLS:
                break
    return found


def extract_symbols(text: str | None) -> list[str]:
    """从文本抽取 ``<TICKER>-USDT-SWAP`` 列表（按首次出现位置去重，最多 MAX_SYMBOLS 个）。"""
    return [f"{tok}-USDT-SWAP" for tok in extract_tickers(text)]


__all__ = [
    "TICKERS", "TICKER_ALIASES", "STABLECOINS", "FULL_NAMES", "ZH_NAMES",
    "MIN_BARE_TICKER_LEN", "MAX_SYMBOLS",
    "canonical_ticker", "instrument_for_ticker",
    "extract_tickers", "extract_symbols",
]
