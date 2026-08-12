# -*- coding: utf-8 -*-
"""Isolated regression checks for execution intent and quantity lifecycle."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "core", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core import execution_intent as ei  # noqa: E402
from core.order_executor import validate_receipt_context  # noqa: E402
import ledger_invariants as li  # noqa: E402
import trade_experience_writer as tew  # noqa: E402


EXP_DDL = """
CREATE TABLE trade_experiences (
 id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, ts TEXT NOT NULL,
 profile TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT, action TEXT,
 regime TEXT, regime_stale INTEGER DEFAULT 0, score_total INTEGER,
 confidence REAL, playbook_ref TEXT, hypothesis_id TEXT, market_snapshot TEXT,
 experience_vector TEXT, pnl_pct REAL, hold_hours REAL, is_gross_profit_close INTEGER,
 status TEXT DEFAULT 'open', raw TEXT, experience_summary TEXT
);
"""


def _card(cycle: str) -> dict:
    # `status` 是 validate_receipt_context 的必检字段（order_executor:993）。
    # 本 fixture 长期缺它，导致整个回归脚本在第 112 行断言就失败——**这与
    # 2026-08-06 的 demo 下线无关**，08-04 的备份里该检查就已存在，属于契约
    # 加字段时漏改 fixture。补上后本脚本才真正跑得完。
    return {
        "cycle_id": cycle,
        "status": "ok",
        "decision_protocol": "decision_card_v1",
        "decision_card": {
            "direction_evidence": ["d"], "opposing_evidence": ["o"],
            "execution_conditions": {"status": "ready"},
            "invalidation_point": {"condition": "x"},
            "risk_reward": {"rr": 2},
            "portfolio_impact": {"summary": "small"},
            "historical_experience": {
                "matched_wins": [], "matched_losses": [],
                "missed_opportunities": [], "usage": "none",
                "reason": "no sample",
            },
            "agent_judgement": "execute", "reference_overrides": [],
        },
    }


def _payload(cycle: str, action: str, trades: list[dict]) -> dict:
    return {
        "cycle_id": cycle, "profile": "live",
        "action_taken": action, "trades": trades,
    }


def _open(symbol: str, size: float, oid: str, notional: float) -> dict:
    return {
        "symbol": symbol, "action": "open", "side": "long", "sz": size,
        "fill_px": 100.0, "notional": notional, "ct_val": 1.0,
        "ordId": oid,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="okx-ledger-regression-") as td:
        root = Path(td)
        ledger = root / "ledger.db"
        request = {
            "profile": "live", "cycle_id": "2026-01-01T00:00",
            "symbol": "ETH-USDT-SWAP", "action": "open", "side": "long",
            "intended_sz": 4.0, "lev": 5.0, "sl_trigger_px": 90.0,
            "mgn_mode": "cross",
        }
        first = ei.reserve(
            ledger, profile="live", cycle_id="2026-01-01T00:00",
            symbol="ETH-USDT-SWAP", side="long", request=request,
            now_ts="2026-01-01 00:00:01")
        assert first["status"] == "reserved"
        blocked = ei.reserve(
            ledger, profile="live", cycle_id="2026-01-01T00:00",
            symbol="ETH-USDT-SWAP", side="long", request=request,
            now_ts="2026-01-01 00:00:02")
        assert blocked["status"] == "blocked"
        stale = li.execution_intent_findings(
            root, "live",
            datetime(2026, 1, 1, 0, 20, tzinfo=timezone(timedelta(hours=8))))
        assert len(stale) == 1
        assert stale[0]["kind"] == "execution_intent_stale"
        receipt = {"ok": True, "trades": [{"ordId": "OID-1"}]}
        ei.mark_completed(
            ledger, profile="live", cycle_id="2026-01-01T00:00",
            symbol="ETH-USDT-SWAP", side="long",
            fingerprint=first["fingerprint"], now_ts="2026-01-01 00:00:03",
            ord_id="OID-1", receipt=receipt, error=None)
        replay = ei.reserve(
            ledger, profile="live", cycle_id="2026-01-01T00:00",
            symbol="ETH-USDT-SWAP", side="long", request=request,
            now_ts="2026-01-01 00:00:04")
        assert replay["status"] == "replay"
        assert replay["receipt"] == receipt
        assert li.execution_intent_findings(
            root, "live",
            datetime(2026, 1, 1, 0, 20, tzinfo=timezone(timedelta(hours=8)))
        ) == []

        assert validate_receipt_context(
            _card("2026-01-01T00:00"),
            cycle_id="2026-01-01T00:00", required=True) == []
        assert validate_receipt_context(
            None, cycle_id="2026-01-01T00:00", required=True)

        account = sqlite3.connect(str(root / "account.db"))
        account.executescript(EXP_DDL)
        tew.insert_or_update_experiences(
            account, _payload("c1", "OPEN_LONG", [_open("ETH", 4, "O1", 400)]),
            "c1", "2026-01-01 01:00:00")
        tew.insert_or_update_experiences(
            account, _payload("c2", "OPEN_LONG", [_open("ETH", 4, "O2", 400)]),
            "c2", "2026-01-01 02:00:00")
        close = {
            "symbol": "ETH", "action": "close", "side": "long", "sz": 8,
            "fill_px": 101, "pnl": 8, "ordId": "C1",
        }
        result = tew.insert_or_update_experiences(
            account, _payload("c3", "CLOSE", [close]),
            "c3", "2026-01-01 03:00:00")
        assert result["closed"] == 2 and result["fallback"] == 0
        rows = account.execute(
            "SELECT status,open_sz,remaining_sz,realized_pnl,pnl_pct "
            "FROM trade_experiences WHERE symbol='ETH' ORDER BY id").fetchall()
        assert rows == [
            ("closed", 4.0, 0.0, 4.0, 1.0),
            ("closed", 4.0, 0.0, 4.0, 1.0),
        ], rows
        again = tew.insert_or_update_experiences(
            account, _payload("c3", "CLOSE", [close]),
            "c3", "2026-01-01 03:00:00")
        assert again["deduped"] == 1

        tew.insert_or_update_experiences(
            account, _payload("c4", "OPEN_LONG", [_open("BTC", 10, "O3", 1000)]),
            "c4", "2026-01-02 01:00:00")
        tew.insert_or_update_experiences(
            account, _payload("c5", "OPEN_LONG", [_open("BTC", 20, "O4", 2000)]),
            "c5", "2026-01-02 02:00:00")
        tew.insert_or_update_experiences(
            account, _payload("c6", "REDUCE", [{
                "symbol": "BTC", "action": "reduce", "side": "long", "sz": 15,
                "fill_px": 101, "pnl": 15, "ordId": "C2",
            }]), "c6", "2026-01-02 03:00:00")
        rows = account.execute(
            "SELECT status,remaining_sz,realized_pnl,pnl_pct "
            "FROM trade_experiences WHERE symbol='BTC' ORDER BY id").fetchall()
        assert rows == [
            ("closed", 0.0, 10.0, 1.0),
            ("open", 15.0, 5.0, None),
        ], rows
        account.commit()
        account.close()

        print(json.dumps({
            "ok": True,
            "checks": [
                "intent_reserve_block_complete_replay",
                "stale_intent_monitoring",
                "receipt_context_preflight",
                "multi_open_close_fifo",
                "partial_reduce_fifo",
                "close_ordid_dedup",
            ],
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
