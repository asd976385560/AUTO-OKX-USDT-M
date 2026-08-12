# -*- coding: utf-8 -*-
r"""apply_asset_class_schema.py — instrument_class 权威资产类别表（Wave2 序9 前置）。

背景（终稿 Wave2）：8 月股票/商品型永续亏损 -78.3 的归因、相似度 v2 的跨资产
硬门、regime 拆分（BTC 口径对 SNDK/CL 只算 context）都需要权威 asset_class，
此前只能人工临时分类、无法稳定复现。

分类事实与方法：
  - OKX 代币化永续 24/7 连续交易（周末合成盘），K 线存在性与周末方差都无法
    结构化分类（已实测：GOOGL 周六方差比 0.438 ≈ BTC 0.447）→ 人工权威表。
  - 种子 = 高置信 curated 名单（source='curated'）；其余默认 crypto
    （source='default_crypto'，可审计可改）；歧义 ticker（OPEN/GPS/CHIP/O/A/S
    等股票与 crypto 同名冲突）**刻意留默认**，宁可维持现状不错杀——错分成
    stock 会把 crypto 候选错误挡在跨资产硬门外。
  - 维护：本脚本只负责初始建表/种子；正常 fast 采集通过 OKX 官方
    ``instCategory`` 自动补齐新上市并纠正非 manual 的大类冲突。人工修正
    （source='manual'）由自动同步永久保护。

类别：crypto | tokenized_stock | tokenized_commodity | tokenized_index_etf
默认 dry-run；--apply 落 market.db（表已存在时只补缺失行，不覆盖 manual 行）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from migration_guard import (
    add_migration_arguments,
    backup_databases,
    resolve_apply,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))

TOKENIZED_STOCK = {
    # 半导体/硬件（本项目 8 月亏损重灾区，全部实际交易过或进过候选）
    "SNDK", "MU", "SKHY", "SKHYNIX", "SAMSUNG", "KIOXIA", "DRAM",
    "AMD", "NVDA", "INTC", "AVGO", "QCOM", "ARM", "SMCI", "TSM", "ASML",
    "AMAT", "LRCX", "KLAC", "MRVL", "ON", "COHR", "CRDO", "ALAB", "AEHR",
    "AXTI", "TER", "TSEM", "SIMO", "GLW", "CIEN", "AAOI", "TTMI", "WDC",
    "POET", "APLD", "CGNX",
    # 美股大盘/科技
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NFLX", "TSLA", "ADBE",
    "ORCL", "CRM", "NOW", "IBM", "CSCO", "DELL", "HPE", "NOK", "ZM",
    "TWLO", "OKTA", "PANW", "CRWD", "SNOW", "SHOP", "APP",
    # 金融/消费/医疗/工业
    "BRKB", "BX", "KO", "COST", "JNJ", "LLY", "UNH", "GME",
    "DKNG", "HIMS", "ISRG", "ROK", "GEV", "FLNC", "VRT", "OSCR", "TTWO",
    "RDDT", "PLTR", "RIVN", "LUNR", "RKLB", "ASTS", "NBIS", "CRWV",
    "BB", "BE", "IREN", "HOOD", "COIN", "MSTR", "STRC", "CRCL",
    "SBET", "BMNR", "RDW",
    # OKX official instCategory=3 additions; several collide with crypto
    # tickers on other venues, so the exchange-owned instrument category is
    # the deciding evidence for these exact OKX contracts.
    "BOT", "BSP", "CBRS", "FLY", "FWDI", "INFQ", "INTW", "LITE",
    "MVLL", "NET", "ONDS", "PENG", "POPMART", "QNT", "RAM", "RIOT",
    "SHAZ", "SNXX", "USAR", "WEN", "XIAOMI",
    # 亚洲股票
    "SONY", "SOFTBANK", "HYUNDAI",
    # Pre-IPO / 私营公司份额型
    "SPACEX", "SPCX", "OPENAI", "ANTHROPIC", "MINIMAX", "ZHIPU",
    # 单股杠杆代币（U/D 后缀族，锚定对应股票）
    "MUU", "SKUU", "SKDD",
}
TOKENIZED_COMMODITY = {
    "XAG", "XAU", "XPD", "XPT", "XCU", "CL", "BZ", "NG",
}
TOKENIZED_INDEX_ETF = {
    "SPY", "QQQ", "IWM", "SMH", "XBI", "XLE", "SOXL", "SOXS",
    "TQQQ", "SQQQ", "TMF", "UVXY", "USO", "URNM", "SHLD",
    "EWJ", "EWT", "EWY", "EWZ", "KR200", "KORU",
}

VALID_CLASSES = ("crypto", "tokenized_stock", "tokenized_commodity",
                 "tokenized_index_etf")

DDL = """CREATE TABLE IF NOT EXISTS instrument_class (
    symbol      TEXT PRIMARY KEY,          -- 完整 instId（<BASE>-USDT-SWAP）
    asset_class TEXT NOT NULL CHECK (asset_class IN
        ('crypto','tokenized_stock','tokenized_commodity','tokenized_index_etf')),
    source      TEXT NOT NULL,             -- curated | default_crypto | manual
    updated_at  TEXT NOT NULL
)"""


def classify_base(base: str) -> tuple[str, str]:
    if base in TOKENIZED_STOCK:
        return "tokenized_stock", "curated"
    if base in TOKENIZED_COMMODITY:
        return "tokenized_commodity", "curated"
    if base in TOKENIZED_INDEX_ETF:
        return "tokenized_index_etf", "curated"
    return "crypto", "default_crypto"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="instrument_class 权威资产类别表（默认 dry-run）")
    ap.add_argument("--db", default="./db/market.db")
    add_migration_arguments(ap)
    args = ap.parse_args()
    apply = resolve_apply(ap, args)
    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"库不存在: {db_path}"}))
        return 2

    con = sqlite3.connect(str(db_path), timeout=15)
    con.execute("PRAGMA busy_timeout=10000")
    try:
        universe = sorted({
            str(r[0]) for r in con.execute(
                "SELECT DISTINCT symbol FROM kline_cache")
            if str(r[0]).endswith("-USDT-SWAP")
        })
        existing: dict[str, str] = {}
        has_table = bool(con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='instrument_class'").fetchone())
        if has_table:
            existing = {
                r[0]: r[1] for r in con.execute(
                    "SELECT symbol, source FROM instrument_class")}

        plan = []
        counts = {c: 0 for c in VALID_CLASSES}
        for sym in universe:
            if sym in existing:
                continue  # 已有行不动（保护 manual 修正）
            base = sym[: -len("-USDT-SWAP")]
            cls, source = classify_base(base)
            counts[cls] += 1
            plan.append((sym, cls, source))

        report = {
            "db": str(db_path), "dry_run": not apply,
            "universe": len(universe), "already_classified": len(existing),
            "to_insert": len(plan), "insert_class_counts": counts,
            "curated_noncrypto_total": (
                len(TOKENIZED_STOCK) + len(TOKENIZED_COMMODITY)
                + len(TOKENIZED_INDEX_ETF)),
        }
        if not apply:
            preview = [p for p in plan if p[1] != "crypto"][:40]
            print(json.dumps({**report, "ok": True, "action": "plan-only",
                              "noncrypto_preview": preview},
                             ensure_ascii=False, indent=1))
            return 0

        backups = backup_databases(
            [db_path], Path(args.backup_dir), "asset-class-schema")
        con.execute(DDL)
        now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        con.executemany(
            "INSERT OR IGNORE INTO instrument_class"
            "(symbol, asset_class, source, updated_at) VALUES (?,?,?,?)",
            [(sym, cls, source, now) for sym, cls, source in plan])
        con.commit()
        final = dict(con.execute(
            "SELECT asset_class, COUNT(*) FROM instrument_class "
            "GROUP BY asset_class").fetchall())
        print(json.dumps({**report, "ok": True, "action": "applied",
                          "backup": str(backups[db_path.resolve()]),
                          "table_class_counts": final},
                         ensure_ascii=False, indent=1))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
