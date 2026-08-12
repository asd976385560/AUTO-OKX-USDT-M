# -*- coding: utf-8 -*-
"""instrument_class 权威资产类别访问器（Wave2 序9 前置）。

唯一读取入口：find_similar 跨资产硬门、briefing regime 标注、经验特征、
报表分组都经此处，禁止各处再维护私有 crypto/非 crypto 名单（Wave0 出口分类
报告用的人工集合即为反例，已由本表取代）。

未分类 symbol 兜底 `crypto`（与 apply_asset_class_schema 的 default_crypto
一致），并在返回中如实携带 source——消费方可区分权威分类与兜底。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

VALID_CLASSES = ("crypto", "tokenized_stock", "tokenized_commodity",
                 "tokenized_index_etf")
_FileSignature = tuple[int, int, int, int]
_CACHE: dict[
    str,
    tuple[_FileSignature, dict[str, tuple[str, str]]],
] = {}


def _stat_pair(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (-1, -1)
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _db_signature(db: Path) -> _FileSignature:
    """Track both the main database and WAL for cross-process commits."""
    main_mtime, main_size = _stat_pair(db)
    wal_mtime, wal_size = _stat_pair(Path(str(db) + "-wal"))
    return (main_mtime, main_size, wal_mtime, wal_size)


def _norm(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    if s.endswith("-USDT-SWAP"):
        return s
    if s.endswith("-USDT"):
        return s + "-SWAP"
    return s + "-USDT-SWAP"


def load_map(db_root: str | os.PathLike = r"./db",
             refresh: bool = False) -> dict[str, tuple[str, str]]:
    """symbol → (asset_class, source), refreshed after external DB commits."""
    key = str(db_root)
    out: dict[str, tuple[str, str]] = {}
    db = Path(db_root) / "market.db"
    signature = _db_signature(db)
    cached = _CACHE.get(key)
    if not refresh and cached is not None and cached[0] == signature:
        return cached[1]
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                if con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='instrument_class'").fetchone():
                    for sym, cls, src in con.execute(
                            "SELECT symbol, asset_class, source "
                            "FROM instrument_class"):
                        out[str(sym)] = (str(cls), str(src))
            finally:
                con.close()
        except sqlite3.Error:
            # A transient read error must not pin an empty map until the next
            # file mutation.  Keep fail-safe fallback behavior, but retry on
            # the next call.
            _CACHE.pop(key, None)
            return {}
    _CACHE[key] = (_db_signature(db), out)
    return out


def asset_class_of(symbol: str,
                   db_root: str | os.PathLike = r"./db") -> str:
    """asset_class（未分类兜底 crypto）。"""
    cls, _ = classify(symbol, db_root)
    return cls


def classify(symbol: str,
             db_root: str | os.PathLike = r"./db"
             ) -> tuple[str, str]:
    """(asset_class, source); source also includes official_inst_category."""
    entry = load_map(db_root).get(_norm(symbol))
    if entry:
        return entry
    return "crypto", "fallback"


def is_crypto(symbol: str,
              db_root: str | os.PathLike = r"./db") -> bool:
    return asset_class_of(symbol, db_root) == "crypto"
