# -*- coding: utf-8 -*-
"""Point-in-time instrument regime context shared by retrieval and writer."""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def build_instrument_context(symbol: str, cycle_regime: Any, cycle_id: str,
                             db_root: str | Path) -> dict[str, Any]:
    from core.asset_class import asset_class_of
    import experience_features_v2 as efv2

    root = Path(db_root)
    asset_class = asset_class_of(symbol, root)
    as_of = cycle_id.replace("T", " ") + ":00" if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", str(cycle_id)) else None
    instrument_regime = "not_available"
    if as_of and (root / "market.db").exists():
        con = sqlite3.connect(
            f"file:{root / 'market.db'}?mode=ro", uri=True, timeout=5)
        try:
            trend = efv2.derive_market_features(con, symbol, as_of).get("trend_4h")
        finally:
            con.close()
        instrument_regime = {
            1: "trend_up", -1: "trend_down", 0: "range",
        }.get(trend, "not_available")
    applies = asset_class == "crypto"
    normalized_cycle_regime = str(cycle_regime or "").strip().lower() or "unknown"
    return {
        "version": "instrument_context_v1",
        "as_of": as_of,
        "btc_crypto_regime": normalized_cycle_regime,
        "applies_to_instrument": applies,
        "asset_class": asset_class,
        "instrument_regime": instrument_regime,
        "note": (
            "全局 regime 为 BTC/crypto 口径" + (
                "，适用本标的" if applies
                else "，对本标的仅作 context；方向论据使用 instrument_regime 与标的自身结构"
            )
        ),
    }
