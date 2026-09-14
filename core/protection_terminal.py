# -*- coding: utf-8 -*-
"""Read-only proof that one newly placed stop triggered before pending readback."""
from __future__ import annotations

import math
import time
from typing import Any, Callable


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _same(left: Any, right: Any) -> bool:
    a, b = _number(left), _number(right)
    return a is not None and b is not None and abs(a-b) <= max(1e-12, abs(b)*1e-9)


def _rows(value: Any) -> list[dict]:
    if isinstance(value, dict) and (value.get("ok") is False or str(value.get("code", "0")) != "0"):
        raise ValueError("failed read response")
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        value = value["data"]
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise ValueError("invalid read response")
    return value


def validate_triggered_flat(
    *, symbol: str, side: str, algo_id: str, expected_sz: float,
    expected_sl: float, expected_tp: float | None, since_ms: float,
    algo: dict, order: dict, positions: list[dict], now_ms: float,
) -> dict:
    """Require the exact triggered reduce-only algo, its filled child and a flat side."""
    def reject(reason: str) -> dict:
        return {"verified": False, "reason": reason}

    size, sl, start, now = map(_number, (expected_sz, expected_sl, since_ms, now_ms))
    if side not in {"long", "short"} or not algo_id or not all(x is not None and x>0 for x in (size, sl, start, now)):
        return reject("invalid_expected_protection")
    if not isinstance(algo, dict) or not isinstance(order, dict):
        return reject("missing_order_evidence")
    close_side = "sell" if side == "long" else "buy"
    if not (str(algo.get("algoId")) == str(algo_id)
            and algo.get("instId") == symbol and algo.get("posSide") == side
            and algo.get("side") == close_side and algo.get("state") == "effective"
            and str(algo.get("reduceOnly")).lower() == "true"
            and str(algo.get("failCode", "")) in {"", "0"}
            and _same(algo.get("sz"), size) and _same(algo.get("actualSz"), size)
            and _same(algo.get("slTriggerPx"), sl)):
        return reject("algo_identity_or_economics_mismatch")
    if expected_tp is not None and not _same(algo.get("tpTriggerPx"), expected_tp):
        return reject("algo_tp_mismatch")
    actual_side = algo.get("actualSide")
    if actual_side not in ({"sl", "tp"} if expected_tp is not None else {"sl"}):
        return reject("unexpected_trigger_leg")
    created, triggered = _number(algo.get("cTime")), _number(algo.get("triggerTime"))
    if created is None or triggered is None or not start <= created <= triggered <= now+5000:
        return reject("algo_time_mismatch")
    child_ids = algo.get("ordIdList")
    if not isinstance(child_ids, list) or len(child_ids) != 1 or not child_ids[0]:
        return reject("single_child_order_required")
    child_id = str(child_ids[0])
    if not (str(order.get("ordId")) == child_id and str(order.get("algoId")) == str(algo_id)
            and order.get("instId") == symbol and order.get("instType") == "SWAP"
            and order.get("posSide") == side and order.get("side") == close_side
            and str(order.get("reduceOnly")).lower() == "true"
            and order.get("state") == "filled" and _same(order.get("accFillSz"), size)
            and _same(order.get("sz"), size)):
        return reject("child_order_identity_or_fill_mismatch")
    filled_at, updated, price = map(_number, (order.get("fillTime"), order.get("uTime"), order.get("avgPx")))
    if filled_at is None or updated is None or price is None or price<=0 or not triggered <= filled_at <= updated <= now+5000:
        return reject("child_order_time_or_price_mismatch")
    if not isinstance(positions, list) or not all(isinstance(p, dict) for p in positions):
        return reject("invalid_positions_response")
    for position in positions:
        if (position.get("instId") or position.get("symbol")) != symbol:
            continue
        ps = position.get("posSide") or position.get("side")
        if ps not in {"long", "short"}:
            return reject("position_side_unproven")
        if ps == side:
            current_size = _number(position.get("pos", position.get("sz")))
            if current_size is None or current_size != 0:
                return reject("position_not_flat")
    return {"verified": True, "kind": "triggered_protection_flat", "symbol": symbol,
            "side": side, "algo_id": str(algo_id), "child_order_id": child_id,
            "closed_sz": size, "fill_px": price, "fill_time_ms": filled_at,
            "trigger_time_ms": triggered, "algo_created_ms": created,
            "positions_confirmed_flat": True, "verified_at_ms": now,
            "position_rows": [dict(p) for p in positions if (p.get("instId") or p.get("symbol")) == symbol],
            "algo": dict(algo), "order": dict(order)}


def probe_triggered_flat(
    *, symbol: str, side: str, profile: str, algo_id: str | None,
    expected_sz: float, expected_sl: float, expected_tp: float | None,
    since_ms: float, read_json: Callable | None = None, budget_seconds: float = 18.0,
) -> dict:
    """At most three explicit GET commands; no retries, writes or substituted prices."""
    if profile != "live" or not algo_id:
        return {"verified": False, "reason": "missing_live_algo_identity"}
    if read_json is None:
        from _okxcli import okx_json
        read_json = okx_json
    budget = _number(budget_seconds)
    if budget is None or budget <= 0:
        return {"verified": False, "reason": "invalid_terminal_read_budget"}
    deadline = time.monotonic()+min(budget, 18.0)

    def read(*args):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError("protection terminal read budget exhausted")
        value = read_json(*args, global_args=["--profile", profile],
                          timeout_sec=min(5.0, remaining), retries=0)
        if time.monotonic() >= deadline:
            raise TimeoutError("late protection terminal evidence")
        return _rows(value)

    try:
        history = read("swap", "algo", "orders", "--instId", symbol, "--history")
        matches = [r for r in history if str(r.get("algoId")) == str(algo_id)]
        if len(matches) != 1:
            return {"verified": False, "reason": "exact_effective_algo_unavailable"}
        algo = matches[0]
        child_ids = algo.get("ordIdList")
        if not isinstance(child_ids, list) or len(child_ids) != 1 or not child_ids[0]:
            return {"verified": False, "reason": "single_child_order_required"}
        orders = read("swap", "get", "--instId", symbol, "--ordId", str(child_ids[0]))
        if len(orders) != 1:
            return {"verified": False, "reason": "exact_child_order_unavailable"}
        positions = read("account", "positions", "--instType", "SWAP")
        return validate_triggered_flat(
            symbol=symbol, side=side, algo_id=str(algo_id), expected_sz=expected_sz,
            expected_sl=expected_sl, expected_tp=expected_tp, since_ms=since_ms,
            algo=algo, order=orders[0], positions=positions, now_ms=time.time()*1000)
    except Exception as exc:
        return {"verified": False, "reason": "terminal_read_failed",
                "error_type": type(exc).__name__, "error": str(exc)[:350]}
