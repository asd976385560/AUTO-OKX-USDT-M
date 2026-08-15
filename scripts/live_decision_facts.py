# -*- coding: utf-8 -*-
"""Build one deterministic, read-only OKX fact package for a live decision.

The package is the only source for position age, contract multiplier, live stop
loss, stop-loss PnL and account IMR ratio in the live-trader stage.  It never
places orders and never writes a business database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(r".")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.lib import _okxorder as ox  # noqa: E402
from core.account_capacity import extract_settlement_capacity  # noqa: E402
from core.risk_validator import (  # noqa: E402  单笔闸常量单一真源
    MAX_PORTFOLIO_IMR_RATIO,
    MAX_SINGLE_ORDER_IMR_RATIO,
    MAX_SINGLE_ORDER_RISK_PCT_EQUITY,
    RISK_FEE_BUFFER_PCT,
    RISK_SLIPPAGE_BUFFER_PCT,
    SINGLE_ORDER_SIZING_HEADROOM_PCT,
)

CST = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
SOURCE = "okx_private_api"
_CYCLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)$")


def _number(value: Any, *, positive: bool = False,
            nonnegative: bool = False) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    if positive and result <= 0:
        return None
    if nonnegative and result < 0:
        return None
    return result


def _rounded(value: float | None, digits: int = 8) -> float | None:
    return None if value is None else round(float(value), digits)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _portfolio_margin_state(ratio: float | None) -> tuple[float | None, str, str]:
    """Classify margin wording against the 66.6% cap, not gross notional."""
    if ratio is None:
        return None, "unknown", "未知"
    utilization = ratio / MAX_PORTFOLIO_IMR_RATIO
    if ratio >= MAX_PORTFOLIO_IMR_RATIO:
        return utilization, "at_or_over_cap", "已达或超过上限"
    if utilization >= 0.90:
        return utilization, "near_cap", "接近上限"
    if utilization >= 0.50:
        return utilization, "normal", "正常"
    return utilization, "low_usage", "占用较低"


def _hash_payload(payload: dict) -> str:
    unsigned = {key: value for key, value in payload.items()
                if key != "facts_hash"}
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _atomic_write_json(path: Path, payload: dict, *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    try:
        tmp.write_text(text + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _position_side(row: dict) -> str | None:
    side = str(row.get("posSide") or "").strip().lower()
    if side in {"long", "short"}:
        return side
    if side == "net":
        pos = _number(row.get("pos"))
        if pos is not None and pos != 0:
            return "long" if pos > 0 else "short"
    return None


def _select_live_sl(position: dict, algos: list[dict]) -> dict:
    symbol = str(position.get("instId") or "")
    side = _position_side(position)
    contracts = abs(_number(position.get("pos")) or 0.0)
    mark = _number(position.get("markPx"), positive=True)
    close_side = "sell" if side == "long" else "buy"
    candidates: list[dict] = []
    for row in algos:
        if not isinstance(row, dict):
            continue
        trigger = _number(row.get("slTriggerPx"), positive=True)
        size = _number(row.get("sz"), positive=True)
        if trigger is None or size is None or mark is None or side is None:
            continue
        checks = (
            str(row.get("instId") or "") == symbol,
            str(row.get("posSide") or "").lower() == side,
            str(row.get("side") or "").lower() == close_side,
            str(row.get("state") or "").lower() == "live",
            _truthy(row.get("reduceOnly")),
            size + max(1e-9, contracts * 1e-9) >= contracts,
            trigger < mark if side == "long" else trigger > mark,
        )
        if not all(checks):
            continue
        candidates.append({
            "verified": True,
            "algoId": str(row.get("algoId") or "") or None,
            "trigger_px": trigger,
            "trigger_px_type": str(
                row.get("slTriggerPxType") or row.get("triggerPxType") or ""
            ) or None,
            "state": "live",
            "reduceOnly": True,
            "side": close_side,
            "posSide": side,
            "sz": size,
        })
    if not candidates:
        return {
            "verified": False,
            "algoId": None,
            "trigger_px": None,
            "trigger_px_type": None,
            "state": None,
            "reduceOnly": None,
            "side": close_side if side else None,
            "posSide": side,
            "sz": None,
        }
    # Long: highest valid stop below mark. Short: lowest valid stop above mark.
    return (
        max(candidates, key=lambda item: item["trigger_px"])
        if side == "long"
        else min(candidates, key=lambda item: item["trigger_px"])
    )


def _balance_facts(balance_rows: list[dict]) -> tuple[dict, list[str]]:
    errors: list[str] = []
    account = next((row for row in balance_rows if isinstance(row, dict)), {})
    details = account.get("details") if isinstance(account, dict) else []
    if not isinstance(details, list):
        details = []
    usdt = next(
        (row for row in details
         if isinstance(row, dict)
         and str(row.get("ccy") or "").upper() == "USDT"),
        {},
    )
    capacity = extract_settlement_capacity(account, "USDT")
    total_eq = _number(capacity.get("total_equity"), positive=True)
    imr = _number(capacity.get("account_imr"), nonnegative=True)
    mmr = _number(usdt.get("mmr"), nonnegative=True)
    avail_eq = _number(
        capacity.get("available_margin"),
        nonnegative=True,
    )
    upl = _number(usdt.get("upl"))
    if total_eq is None:
        errors.append("balance.totalEq_missing_or_invalid")
    if imr is None:
        errors.append("balance.USDT.imr_missing_or_invalid")
    if capacity.get("ok") is not True:
        errors.append(
            "balance.capacity_invalid:"
            + str(capacity.get("error") or "unknown")
        )
    ratio = imr / total_eq if imr is not None and total_eq else None
    headroom = (
        total_eq * MAX_PORTFOLIO_IMR_RATIO - imr
        if total_eq is not None and imr is not None else None
    )
    cap_utilization, margin_state, margin_label_cn = (
        _portfolio_margin_state(ratio)
    )
    return {
        "totalEq": _rounded(total_eq),
        "availEq": _rounded(avail_eq),
        "account_imr": _rounded(imr),
        "account_mmr": _rounded(mmr),
        "upl": _rounded(upl),
        "current_portfolio_imr_ratio": _rounded(ratio, 10),
        "max_portfolio_imr_ratio": MAX_PORTFOLIO_IMR_RATIO,
        "portfolio_imr_cap_utilization": _rounded(cap_utilization, 10),
        "portfolio_margin_state": margin_state,
        "portfolio_margin_label_cn": margin_label_cn,
        "portfolio_imr_ratio_unit": "fraction",
        "portfolio_imr_source": (
            str(capacity.get("account_imr_source")) + "/totalEq"
            if capacity.get("account_imr_source") else None
        ),
        "available_margin_source": capacity.get("source"),
        "headroom_before_cap_usdt": _rounded(headroom),
        # 2026-08-08 单笔闸补强：给 Agent 提案前的确定性单笔预算（14.7%×totalEq，
        # 已含滑点余量）；硬边界 15% 由 risk_validator 强制，此处仅供先行自查。
        "max_single_order_imr_ratio": MAX_SINGLE_ORDER_IMR_RATIO,
        "single_order_sizing_headroom_pct": SINGLE_ORDER_SIZING_HEADROOM_PCT,
        "single_order_budget_scope": "next_incremental_open_or_add_order",
        "single_order_budget_reduced_by_existing_position": False,
        "single_order_margin_budget_usdt": _rounded(
            total_eq * MAX_SINGLE_ORDER_IMR_RATIO
            * SINGLE_ORDER_SIZING_HEADROOM_PCT
            if total_eq is not None else None
        ),
        # 2026-08-10 Wave1 序7 止损风险闸：单笔风险预算（USDT 标量）= 5%×totalEq。
        # 提案自查换算：max_notional = 预算 ÷ (止损距离 + fee/slippage 缓冲)；
        # 硬缩量由 risk_validator 强制，此处仅供先行自查。
        "max_single_order_risk_pct_equity": MAX_SINGLE_ORDER_RISK_PCT_EQUITY,
        "risk_fee_buffer_pct": RISK_FEE_BUFFER_PCT,
        "risk_slippage_buffer_pct": RISK_SLIPPAGE_BUFFER_PCT,
        "single_order_risk_budget_usdt": _rounded(
            total_eq * MAX_SINGLE_ORDER_RISK_PCT_EQUITY
            if total_eq is not None else None
        ),
        "new_open_current_ratio_gate": (
            ratio < MAX_PORTFOLIO_IMR_RATIO
            if ratio is not None else None
        ),
    }, errors


def _position_facts(row: dict, instrument: dict | None, algos: list[dict],
                    as_of_ms: int) -> tuple[dict, list[str]]:
    symbol = str(row.get("instId") or "")
    side = _position_side(row)
    contracts = abs(_number(row.get("pos")) or 0.0)
    avg_px = _number(row.get("avgPx"), positive=True)
    mark_px = _number(row.get("markPx"), positive=True)
    ct_val = _number((instrument or {}).get("ctVal"), positive=True)
    lever = _number(row.get("lever"), positive=True)
    imr = _number(row.get("imr"), nonnegative=True)
    upl = _number(row.get("upl"))
    exchange_upl_ratio = _number(row.get("uplRatio"))
    ctime = _number(row.get("cTime"), positive=True)
    errors: list[str] = []
    for field, value in (
        ("instId", symbol or None),
        ("posSide", side),
        ("pos", contracts if contracts > 0 else None),
        ("avgPx", avg_px),
        ("markPx", mark_px),
        ("ctVal", ct_val),
        ("lever", lever),
        ("cTime", ctime),
    ):
        if value is None:
            errors.append(f"position[{symbol or '?'}].{field}_missing_or_invalid")
    age_hours = None
    if ctime is not None:
        age_hours = (as_of_ms - ctime) / 3_600_000.0
        if age_hours < -1 / 60:
            errors.append(f"position[{symbol}].cTime_in_future")
            age_hours = None
    base_qty = contracts * ct_val if ct_val is not None else None
    entry_notional = (
        avg_px * base_qty
        if avg_px is not None and base_qty is not None else None
    )
    mark_notional = (
        mark_px * base_qty
        if mark_px is not None and base_qty is not None else None
    )
    initial_margin_estimate = (
        entry_notional / lever
        if entry_notional is not None and lever is not None else None
    )
    upl_ratio = exchange_upl_ratio
    upl_ratio_source = (
        "exchange.positions.uplRatio" if upl_ratio is not None else None
    )
    if (
        upl_ratio is None and upl is not None
        and initial_margin_estimate is not None
        and initial_margin_estimate > 0
    ):
        upl_ratio = upl / initial_margin_estimate
        upl_ratio_source = "derived.upl/entry_notional_div_leverage"
    price_return = None
    if avg_px is not None and mark_px is not None and side is not None:
        price_return = (
            (mark_px - avg_px) / avg_px
            if side == "long" else (avg_px - mark_px) / avg_px
        )
    sl = _select_live_sl(row, algos)
    if not sl["verified"]:
        errors.append(f"position[{symbol}].full_size_live_reduce_only_sl_missing")
    trigger = _number(sl.get("trigger_px"), positive=True)
    pnl_at_stop = additional_to_stop = None
    if (
        base_qty is not None and avg_px is not None
        and mark_px is not None and trigger is not None and side is not None
    ):
        if side == "long":
            pnl_at_stop = (trigger - avg_px) * base_qty
            additional_to_stop = (trigger - mark_px) * base_qty
        else:
            pnl_at_stop = (avg_px - trigger) * base_qty
            additional_to_stop = (mark_px - trigger) * base_qty
    additional_loss_to_stop = (
        max(0.0, -additional_to_stop)
        if additional_to_stop is not None else None
    )
    secured_profit_at_stop = (
        max(0.0, pnl_at_stop) if pnl_at_stop is not None else None
    )
    profit_retention_at_stop_pct = None
    giveback_to_stop_pct = None
    if upl is not None and upl > 0:
        if secured_profit_at_stop is not None:
            profit_retention_at_stop_pct = (
                secured_profit_at_stop / upl * 100.0
            )
        if additional_loss_to_stop is not None:
            giveback_to_stop_pct = additional_loss_to_stop / upl * 100.0
    return {
        "instId": symbol,
        "posSide": side,
        "contracts": _rounded(contracts),
        "ctVal": _rounded(ct_val),
        "base_qty": _rounded(base_qty),
        "avgPx": _rounded(avg_px),
        "markPx": _rounded(mark_px),
        "lever": _rounded(lever),
        "mgnMode": str(row.get("mgnMode") or "") or None,
        "posId": str(row.get("posId") or "") or None,
        "cTime": int(ctime) if ctime is not None else None,
        "position_opened_at": (
            datetime.fromtimestamp(ctime / 1000, tz=CST).strftime(
                "%Y-%m-%d %H:%M:%S"
            ) if ctime is not None else None
        ),
        "position_age_hours": _rounded(age_hours, 4),
        "upl": _rounded(upl),
        "upl_ratio_initial_margin": _rounded(upl_ratio, 10),
        "upl_pct_initial_margin": _rounded(
            upl_ratio * 100.0 if upl_ratio is not None else None, 4),
        "upl_ratio_source": upl_ratio_source,
        "signed_price_return_pct_from_entry": _rounded(
            price_return * 100.0 if price_return is not None else None, 4),
        # 50% 只触发显式复核，不是自动平仓阈值；最终动作仍由 Agent
        # 结合逐仓 15m/1H/4H、原始计划、保护与组合机会自主裁决。
        "margin_return_review_at_or_above_50pct": bool(
            upl_ratio is not None and upl_ratio >= 0.5),
        "position_imr": _rounded(imr),
        "initial_margin_estimate_usdt": _rounded(initial_margin_estimate),
        "entry_notional_usdt": _rounded(entry_notional),
        "mark_notional_usdt": _rounded(mark_notional),
        "sl": sl,
        "pnl_at_stop_from_entry_usdt": _rounded(pnl_at_stop),
        "loss_at_stop_from_entry_usdt": _rounded(
            max(0.0, -pnl_at_stop) if pnl_at_stop is not None else None
        ),
        "additional_pnl_to_stop_from_mark_usdt": _rounded(additional_to_stop),
        "additional_loss_to_stop_from_mark_usdt": _rounded(
            additional_loss_to_stop),
        "secured_profit_at_stop_usdt": _rounded(secured_profit_at_stop),
        "profit_retention_at_stop_pct_of_current_upl": _rounded(
            profit_retention_at_stop_pct, 4),
        "giveback_to_stop_pct_of_current_upl": _rounded(
            giveback_to_stop_pct, 4),
    }, errors


def derive_facts(
    cycle_id: str,
    profile: str,
    positions_raw: list[dict],
    balance_raw: list[dict],
    instruments_by_id: dict[str, dict],
    algos_by_id: dict[str, list[dict]],
    *,
    as_of_ms: int | None = None,
    source_errors: list[str] | None = None,
) -> dict:
    as_of_ms = int(as_of_ms if as_of_ms is not None else time.time() * 1000)
    errors = list(source_errors or [])
    balance, balance_errors = _balance_facts(balance_raw)
    errors.extend(balance_errors)
    open_rows = []
    for row in positions_raw:
        if not isinstance(row, dict):
            continue
        pos = _number(row.get("pos"))
        if pos is not None and abs(pos) > 0:
            open_rows.append(row)
    positions: list[dict] = []
    for row in open_rows:
        symbol = str(row.get("instId") or "")
        item, item_errors = _position_facts(
            row,
            instruments_by_id.get(symbol),
            algos_by_id.get(symbol, []),
            as_of_ms,
        )
        positions.append(item)
        errors.extend(item_errors)
    errors = sorted(set(str(error) for error in errors if str(error).strip()))
    position_truth_verified = not any(
        error.startswith(("positions_query_failed", "positions_data_not_list"))
        for error in errors
    )
    # 2026-08-08 修矛盾：组合 IMR 已越 66.6% 时 open_add_allowed_by_facts=False，
    # 动作表必须同步剔除 open/add——此前两者不联动，Agent 会看到互相矛盾的信号。
    open_add_ok = (not errors
                   and balance.get("new_open_current_ratio_gate") is True)
    if not errors:
        allowed_executor_actions = (
            ["open", "add", "close", "reduce", "adjust_protection"]
            if open_add_ok and open_rows
            else ["open", "add"] if open_add_ok
            else ["close", "reduce", "adjust_protection"] if open_rows
            else [])
    elif position_truth_verified and open_rows:
        # 余额、ctVal 或保护单缺失时绝不新增风险，但已确认现仓仍必须保留
        # 去风险出口；不能因“开仓事实不全”反向阻断 CLOSE/REDUCE。
        allowed_executor_actions = ["close", "reduce", "adjust_protection"]
    else:
        allowed_executor_actions = []
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "cycle_id": cycle_id,
        "profile": profile,
        "as_of_ms": as_of_ms,
        "as_of": datetime.fromtimestamp(as_of_ms / 1000, tz=CST).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "status": "ok" if not errors else "blocking",
        "balance": balance,
        "positions": positions,
        "position_profit_review_policy": {
            "margin_return_attention_threshold_fraction": 0.5,
            "attention_flag_is_non_binding": True,
            "automatic_close_authorized": False,
            "agent_outcomes": [
                "hold", "close", "reduce", "adjust_protection",
            ],
            "decision_basis": (
                "current_exchange_facts_plus_position_15m_1H_4H_"
                "plus_original_exit_plan_and_portfolio_opportunity_cost"
            ),
        },
        "errors": errors,
        "action_policy": {
            "position_truth_verified": position_truth_verified,
            "allowed_executor_actions": allowed_executor_actions,
            "open_add_allowed_by_facts": open_add_ok,
        },
        "exchange": {
            "positions": positions_raw,
            "balance": balance_raw,
            "instruments": instruments_by_id,
            "algo_orders": algos_by_id,
            "source_errors": list(source_errors or []),
        },
    }
    payload["facts_hash"] = _hash_payload(payload)
    return payload


def _response_data(response: dict, label: str,
                   errors: list[str]) -> list[dict]:
    if not isinstance(response, dict) or response.get("ok") is not True:
        detail = (
            response.get("error") or response.get("sMsg")
            if isinstance(response, dict) else "invalid_response"
        )
        errors.append(f"{label}_query_failed:{detail}")
        return []
    data = response.get("data")
    if not isinstance(data, list):
        errors.append(f"{label}_data_not_list")
        return []
    return [row for row in data if isinstance(row, dict)]


def build_facts(cycle_id: str, profile: str = "live", *,
                as_of_ms: int | None = None, client=ox) -> dict:
    if not _CYCLE_RE.fullmatch(str(cycle_id)):
        raise ValueError("cycle_id 必须是 YYYY-MM-DDTHH:00/15/30/45")
    if profile != "live":
        raise ValueError("仅支持 profile=live")
    source_errors: list[str] = []
    try:
        positions_response = client.get_positions(profile)
    except Exception as exc:  # noqa: BLE001
        positions_response = {"ok": False, "error": str(exc), "data": []}
    try:
        balance_response = client.get_balance(profile)
    except Exception as exc:  # noqa: BLE001
        balance_response = {"ok": False, "error": str(exc), "data": []}
    positions = _response_data(
        positions_response, "positions", source_errors
    )
    balance = _response_data(balance_response, "balance", source_errors)

    instruments: dict[str, dict] = {}
    algos: dict[str, list[dict]] = {}
    for row in positions:
        pos = _number(row.get("pos"))
        if pos is None or abs(pos) == 0:
            continue
        symbol = str(row.get("instId") or "")
        if not symbol:
            continue
        try:
            instrument = client.get_instrument(symbol, profile)
        except Exception as exc:  # noqa: BLE001
            instrument = None
            source_errors.append(f"instrument_query_failed:{symbol}:{exc}")
        if isinstance(instrument, dict):
            instruments[symbol] = instrument
        else:
            source_errors.append(f"instrument_missing:{symbol}")
        try:
            rows = client.get_algo_orders(symbol, profile)
        except Exception as exc:  # noqa: BLE001
            rows = []
            source_errors.append(f"algo_orders_query_failed:{symbol}:{exc}")
        algos[symbol] = [item for item in rows if isinstance(item, dict)]
    return derive_facts(
        cycle_id,
        profile,
        positions,
        balance,
        instruments,
        algos,
        as_of_ms=as_of_ms,
        source_errors=source_errors,
    )


def validate_facts(
    payload: dict,
    *,
    expected_cycle: str | None = None,
    expected_profile: str = "live",
    require_ok: bool = False,
    now_ms: int | None = None,
    max_age_s: float | None = None,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["live_facts 必须是 dict"]
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("live_facts.schema_version 不支持")
    if payload.get("source") != SOURCE:
        errors.append("live_facts.source 必须是 okx_private_api")
    if expected_cycle and payload.get("cycle_id") != expected_cycle:
        errors.append("live_facts.cycle_id 与回执不一致")
    if payload.get("profile") != expected_profile:
        errors.append("live_facts.profile 与提交 profile 不一致")
    status = payload.get("status")
    fact_errors = payload.get("errors")
    if status not in {"ok", "blocking"}:
        errors.append("live_facts.status 必须是 ok|blocking")
    if not isinstance(fact_errors, list):
        errors.append("live_facts.errors 必须是 list")
    elif (status == "ok") != (len(fact_errors) == 0):
        errors.append("live_facts.status 与 errors 不一致")
    if require_ok and status != "ok":
        errors.append("live_facts 为 blocking，status=ok 回执不得继续")
    if payload.get("facts_hash") != _hash_payload(payload):
        errors.append("live_facts.facts_hash 校验失败（文件被改写或不完整）")

    as_of_ms = payload.get("as_of_ms")
    if not isinstance(as_of_ms, int) or as_of_ms <= 0:
        errors.append("live_facts.as_of_ms 无效")
    elif max_age_s is not None:
        current = int(now_ms if now_ms is not None else time.time() * 1000)
        age_s = (current - as_of_ms) / 1000
        if age_s < -60:
            errors.append("live_facts 来自未来时间")
        elif age_s > max_age_s:
            errors.append(f"live_facts 已过期: {age_s:.0f}s>{max_age_s:.0f}s")

    exchange = payload.get("exchange")
    if isinstance(exchange, dict) and isinstance(as_of_ms, int):
        try:
            rebuilt = derive_facts(
                str(payload.get("cycle_id") or ""),
                str(payload.get("profile") or ""),
                exchange.get("positions") or [],
                exchange.get("balance") or [],
                exchange.get("instruments") or {},
                exchange.get("algo_orders") or {},
                as_of_ms=as_of_ms,
                source_errors=exchange.get("source_errors") or [],
            )
            if rebuilt != payload:
                errors.append("live_facts 与内含交易所原始快照推导结果不一致")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"live_facts 重算失败: {exc}")
    else:
        errors.append("live_facts.exchange 原始快照缺失")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读生成 Live 决策权威事实包（不下单、不写业务库）"
    )
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--profile", choices=["live"], default="live")
    parser.add_argument("--out-file", required=True)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = build_facts(args.cycle_id, args.profile)
        _atomic_write_json(Path(args.out_file), payload, pretty=args.pretty)
        result = {
            "ok": payload["status"] == "ok",
            "status": payload["status"],
            "cycle_id": payload["cycle_id"],
            "positions": len(payload["positions"]),
            "errors": payload["errors"],
            "out_file": str(Path(args.out_file)),
            "facts_hash": payload["facts_hash"],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
