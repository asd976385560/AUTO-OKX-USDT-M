# -*- coding: utf-8 -*-
"""OKX 账户容量回包解析（纯函数、无网络/DB 副作用）。

``totalEq`` 是全账户按 USD 折算后的总权益。在多币种账户中，它可能包含 BTC、ETH、
OKB 等现货资产，并不等于 USDT 永续可以立即占用的结算币保证金。USDT-SWAP 开仓预检
中，live 使用 ``details[ccy=USDT].availBal/availEq``，并从账户级 ``imr``（Futures
mode 回包则为 ``details[USDT].imr``）取得当前组合初始保证金；Demo 则只使用目标合约
和方向的 ``account max-size``。两种 parser 严格分离，字段缺失时均 fail-safe。
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


def _extract_account_imr(
    row: dict[str, Any],
    detail: Optional[dict[str, Any]],
    settlement_ccy: str,
) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """按账户级优先顺序提取 IMR，并区分“缺失”与“存在但非法”。

    顶层 ``imr`` 是多币种/组合保证金账户的账户级权威值。只有该字段确实为空时，
    才允许回退到期货模式的币种级 ``details[].imr``；若顶层字段存在但非法，
    继续回退可能把跨币种账户错误缩窄为单币种 IMR，因此必须让调用方失败关闭。
    """
    top_source = "account.balance.imr"
    top_raw = row.get("imr")
    if top_raw not in (None, ""):
        top_value = _to_float(top_raw)
        if top_value is None or top_value < 0:
            return None, top_source, "account_imr_invalid"
        return top_value, top_source, None

    detail_source = f"account.balance.details.{settlement_ccy}.imr"
    detail_raw = detail.get("imr") if isinstance(detail, dict) else None
    if detail_raw not in (None, ""):
        detail_value = _to_float(detail_raw)
        if detail_value is None or detail_value < 0:
            return None, detail_source, "account_imr_invalid"
        return detail_value, detail_source, None
    return None, None, "account_imr_missing"


def extract_settlement_capacity(
    payload: Any,
    settlement_ccy: str = "USDT",
) -> dict[str, Any]:
    """从账户余额回包提取某结算币的保守可用保证金。

    若 ``availBal`` 与 ``availEq`` 同时存在，取二者较小值；二者通常相等，取最小值可
    避免账户模式差异时把较宽松字段误当成确定可用额度。组合 IMR 优先取账户行顶层
    ``imr``，为空时才取目标结算币 ``details[].imr``，绝不以
    ``totalEq - availBal`` 猜测。结算币行或两个可用字段均缺失时返回 ``ok=False``，
    调用方必须拒绝开仓。
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
        account_imr, account_imr_source, account_imr_error = (
            _extract_account_imr(row, None, ccy)
        )
        return {
            "ok": False,
            "settlement_ccy": ccy,
            "total_equity": _to_float(row.get("totalEq")),
            "account_imr": account_imr,
            "account_imr_source": account_imr_source,
            "account_imr_error": account_imr_error,
            "available_margin": None,
            "error": "settlement_currency_missing",
        }

    avail_bal = _to_float(detail.get("availBal"))
    avail_eq = _to_float(detail.get("availEq"))
    account_imr, account_imr_source, account_imr_error = (
        _extract_account_imr(row, detail, ccy)
    )
    top_level_mgn_ratio = _to_float(row.get("mgnRatio"))
    settlement_mgn_ratio = _to_float(detail.get("mgnRatio"))
    account_mgn_ratio = (
        top_level_mgn_ratio
        if top_level_mgn_ratio is not None else settlement_mgn_ratio
    )
    candidates = [value for value in (avail_bal, avail_eq) if value is not None]
    if not candidates:
        return {
            "ok": False,
            "settlement_ccy": ccy,
            "total_equity": _to_float(row.get("totalEq")),
            "settlement_equity": _to_float(detail.get("eq")),
            "account_imr": account_imr,
            "account_imr_source": account_imr_source,
            "account_imr_error": account_imr_error,
            "account_mgn_ratio_observation_only": account_mgn_ratio,
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
            "account_imr": account_imr,
            "account_imr_source": account_imr_source,
            "account_imr_error": account_imr_error,
            "account_mgn_ratio_observation_only": account_mgn_ratio,
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
        "account_imr": account_imr,
        "account_imr_source": account_imr_source,
        "account_imr_error": account_imr_error,
        # mgnRatio 是维持保证金健康比率，不是 IMR/totalEq；只留痕，严禁用于容量。
        "account_mgn_ratio_observation_only": account_mgn_ratio,
        "available_margin": available,
        "available_balance": avail_bal,
        "available_equity": avail_eq,
        "frozen_balance": _to_float(detail.get("frozenBal")),
        "cash_balance": _to_float(detail.get("cashBal")),
        "source": f"details.{ccy}.min({','.join(source_fields)})",
    }


def extract_directional_max_size(
    payload: Any,
    inst_id: str,
    side: str,
) -> dict[str, Any]:
    """严格提取 OKX ``account max-size`` 的方向可开张数。

    ``long`` 对应 ``maxBuy``，``short`` 对应 ``maxSell``。只接受与
    ``inst_id`` 完全匹配的唯一数据行；接口失败、方向非法、标的缺失/重复、
    字段缺失、非有限数或负数均返回 ``ok=False``。本函数不从 balance、
    ``totalEq`` 或另一方向字段回退，避免把 live/demo 的不同账户容量口径混用。
    零值是合法的交易所结果，由调用方按“当前无可开容量”处理。
    """
    normalized_side = str(side or "").lower()
    direction_field = {
        "long": "maxBuy",
        "short": "maxSell",
    }.get(normalized_side)
    base = {
        "ok": False,
        "inst_id": str(inst_id or ""),
        "side": normalized_side,
        "max_size": None,
        "source": (
            f"account.max-size.{direction_field}"
            if direction_field else "account.max-size"
        ),
        "direction_field": direction_field,
        "direction_value": None,
        "error": None,
    }
    if direction_field is None:
        return {**base, "error": "bad_side"}
    if not isinstance(payload, dict):
        return {**base, "error": "max_size_response_unavailable"}
    if payload.get("ok") is False:
        return {**base, "error": "max_size_response_failed"}
    code = payload.get("code")
    if code is not None and str(code) not in ("", "0"):
        return {**base, "error": "max_size_response_failed"}

    data = payload.get("data")
    if not isinstance(data, list):
        return {**base, "error": "max_size_data_missing"}
    matches = [
        row for row in data
        if isinstance(row, dict) and row.get("instId") == inst_id
    ]
    if not matches:
        return {**base, "error": "max_size_instrument_missing"}
    if len(matches) != 1:
        return {**base, "error": "max_size_instrument_ambiguous"}

    raw_value = matches[0].get(direction_field)
    result = {**base, "direction_value": raw_value}
    max_size = _to_float(raw_value)
    if max_size is None:
        return {**result, "error": "max_size_field_invalid"}
    if max_size < 0:
        return {**result, "error": "max_size_negative"}
    return {
        **result,
        "ok": True,
        "max_size": max_size,
        "error": None,
    }
