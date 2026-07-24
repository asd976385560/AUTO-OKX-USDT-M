# -*- coding: utf-8 -*-
"""OKX 账户余额回包 -> USDT 合约可用保证金（纯函数、无网络/DB 副作用）。

``totalEq`` 是全账户按 USD 折算后的总权益。在多币种账户中，它可能包含 BTC、ETH、
OKB 等现货资产，并不等于 USDT 永续可以立即占用的结算币保证金。USDT-SWAP 开仓预检
必须使用 ``details[ccy=USDT].availBal/availEq``；字段缺失时 fail-safe，禁止回退
``totalEq``。
"""
from __future__ import annotations

import math
from typing import Any, Optional


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _balance_row(payload: Any) -> Optional[dict[str, Any]]:
    """接受 ``_okxorder.get_balance`` 归一回包或裸 balance row。"""
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is False:
        return None
    data = payload.get("data")
    if isinstance(data, list):
        return next((row for row in data if isinstance(row, dict)), None)
    # 裸 row 没有 data，但应具有 balance 的标志字段。
    if any(key in payload for key in ("totalEq", "details", "adjEq")):
        return payload
    return None


def extract_settlement_capacity(
    payload: Any,
    settlement_ccy: str = "USDT",
) -> dict[str, Any]:
    """从账户余额回包提取某结算币的保守可用保证金。

    若 ``availBal`` 与 ``availEq`` 同时存在，取二者较小值；二者通常相等，取最小值可
    避免账户模式差异时把较宽松字段误当成确定可用额度。结算币行或两个可用字段均缺失
    时返回 ``ok=False``，调用方必须拒绝开仓，不能回退全账户 ``totalEq``。
    """
    ccy = str(settlement_ccy or "").upper()
    row = _balance_row(payload)
    if row is None:
        return {
            "ok": False,
            "settlement_ccy": ccy,
            "available_margin": None,
            "error": "balance_unavailable",
        }

    details = row.get("details")
    if not isinstance(details, list):
        details = []
    detail = next(
        (
            item for item in details
            if isinstance(item, dict) and str(item.get("ccy") or "").upper() == ccy
        ),
        None,
    )
    if detail is None:
        return {
            "ok": False,
            "settlement_ccy": ccy,
            "total_equity": _to_float(row.get("totalEq")),
            "available_margin": None,
            "error": "settlement_currency_missing",
        }

    avail_bal = _to_float(detail.get("availBal"))
    avail_eq = _to_float(detail.get("availEq"))
    candidates = [value for value in (avail_bal, avail_eq) if value is not None]
    if not candidates:
        return {
            "ok": False,
            "settlement_ccy": ccy,
            "total_equity": _to_float(row.get("totalEq")),
            "settlement_equity": _to_float(detail.get("eq")),
            "available_margin": None,
            "error": "available_margin_missing",
        }

    available = min(candidates)
    if available < 0:
        return {
            "ok": False,
            "settlement_ccy": ccy,
            "total_equity": _to_float(row.get("totalEq")),
            "settlement_equity": _to_float(detail.get("eq")),
            "available_margin": available,
            "error": "available_margin_negative",
        }

    source_fields = []
    if avail_bal is not None:
        source_fields.append("availBal")
    if avail_eq is not None:
        source_fields.append("availEq")
    return {
        "ok": True,
        "settlement_ccy": ccy,
        "total_equity": _to_float(row.get("totalEq")),
        "settlement_equity": _to_float(detail.get("eq")),
        "settlement_equity_usd": _to_float(detail.get("eqUsd")),
        "available_margin": available,
        "available_balance": avail_bal,
        "available_equity": avail_eq,
        "frozen_balance": _to_float(detail.get("frozenBal")),
        "cash_balance": _to_float(detail.get("cashBal")),
        "source": f"details.{ccy}.min({','.join(source_fields)})",
    }
