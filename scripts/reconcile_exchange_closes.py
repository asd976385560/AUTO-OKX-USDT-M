# -*- coding: utf-8 -*-
r"""交易所侧/执行后平仓漏落账对账（F2，2026-07-06）。

背景：algo 止损或主动平仓已在交易所成交后，账本（live/demo trades.db）可能漏写——
仓位从 position_snapshots 消失、trades 表却无 close 行，形成「幽灵仓」：
  - 账本轧差净持仓 > OKX API 现仓 → 幽灵；
  - 实证：live LINK-USDT-SWAP 11.4 张（2026-07-05 10:11:55 SL 卖出 pnl=-1.5276）、
    demo LAB-USDT-SWAP 110 张（2026-07-06 04:22:40 SL 卖出 pnl=-5.346）。

逻辑（先例＝2026-07-04 demo ATOM 补账：fills 实证 → trades_writer.write_trades 直调）：
  1. 读该 profile trades 轧差净持仓（open/add 加、close/stop_loss/reduce 减，按 symbol+side）；
  2. 对比 OKX API 现仓（`account positions --instType SWAP`）；
  3. 账本多出的幽灵仓 → 按 symbol 回读 fills（recent + --archive 合并去重），
     取 open 窗口起点之后的反向平仓成交，按 ordId 分组；
  4. 平仓历史按 ordId 定向补全，已有 close 按订单身份及经济量核销；
     未入账候选须与交易所订单终态/累计成交量一致，补查次数和总时长有界；
  5. 【精确匹配】判定（满足其一才可补，匹配集张数合计恒 == 幽灵 sz）：
     a) 剩余 fills 组张数合计 == 幽灵 sz → 全部剩余组即匹配集；
     b) 恰有唯一一个剩余组 sz == 幽灵 sz → 该组即匹配集（其余剩余组=独立未记账
        成交，如小额同 sz 往返，净额为 0 不影响轧差——只报告 [LEFTOVER] 不写）；
     两者都不满足 → 模糊，只报告；
  6. --apply：精确匹配项经 collectors/trades_writer.write_trades 直调补一行 close
     （action='close'，pnl=fills fillPnl 合计，cycle_id=平仓时刻所在 15min 槽，
      优先用执行 journal 还原主动平仓语义，否则中性标记 exchange fills）；
      目标 cycle 已存在时先读原行，
      原有 trades 行合并进 payload（write_trades 是 REPLACE+DELETE 语义，不合并会销账）。

只报告不写的类别：
  - [GHOST-FUZZY]  fills 对不上幽灵 sz / API 失败 → 人工核；
  - [OVER_CLOSED]  账本净持仓为负（close 多于 open，缺 open 行）→ 非本脚本可补；
  - [UNRECORDED]   交易所有仓账本无（下单成功未记账）→ 非本脚本可补。

退出码：0=无幽灵（或 --apply 全部补完）；1=有精确可补项（dry，待 --apply）；
        3=存在模糊幽灵（需人工）；2=API/库/写入错误。

用法：
  pwsh -NoProfile -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 \
      <PROJECT_ROOT>/scripts/reconcile_exchange_closes.py --profile live [--db-root <PROJECT_ROOT>/db] [--apply]
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, _public_project_path('scripts'))
sys.path.insert(0, _public_project_path('collectors'))
from _okxcli import okx_json  # noqa: E402
import trades_writer  # noqa: E402  （硬化 writer：write_trades / write_experiences / normalize_ts）
from core.decision_card import (  # noqa: E402
    MINIMAL_DECISION_PROTOCOL,
    OPEN_EXECUTION_PACKAGE_KEY,
    canonical_open_execution_package,
    is_lightweight_open_card,
    is_open_execution_package,
    validate_card,
    validate_open_execution_package,
)
from scripts import _acceptance_thresholds as thresholds  # noqa: E402

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
SZ_TOL = 1e-6              # 张数比较容差（float 累加误差级）
CONSUME_WINDOW_MIN = 45    # 账本 close 行 ts ↔ fills 组时间匹配窗口（分钟）
OPEN_TS_BUFFER_MIN = 10    # fills 检索窗口起点 = 最早 open 行 ts - buffer（fills 常先于落库 ts）
RAW_FILLS_CAP = 50         # raw 里最多存多少条 fills 明细
CLOSE_EVIDENCE_BUDGET_SEC = 60.0
CLOSE_ORDER_LOOKUP_LIMIT = 6
CLOSE_QUERY_TIMEOUT_SEC = 15.0
# `swap fills` 取不到第二页：CLI（1.4.4 与 1.4.6 实测一致）的 cmdSwapFills 只透传
# instId/ordId/archive，after/before/begin/end/limit 全被丢弃——传了不报错、也不生效。
# 于是 recent 页只覆盖交易所侧近 3 天，archive 页被 CLI 固定成 20 条。首页没覆盖到
# t0_ms 时，「窗口内无未销账的成交」是**证不出来**的结论：2026-09-11 SOXL 的 09-07
# 平仓就落在 37 条 recent 页与 20 条 archive 页之外，被误判成 GHOST-FUZZY。证不到
# 覆盖一律失败关闭，绝不把截断的视图当成「窗口内没有成交」。页内含活动持仓段首笔
# 开仓的全部成交同样自证覆盖（2026-09-11 BCH，见 `_fills_page_reaches_window`）；
# 开仓腿由已核回执的 intent 订单整单在页内自证（见 `receipt_open_anchor`）。
ARCHIVE_FILLS_PAGE_CAP = 20
CLOSE_DEFERRED_REASONS = frozenset({
    "close_evidence_time_budget_exhausted",
    "close_evidence_order_budget_exhausted",
})
_CLOSE_BUDGET = ContextVar("close_reconcile_budget", default=None)
# classify 回读幽灵平仓腿期间有效：活动持仓段首笔开仓的 (ordId, 张数)，供覆盖判据自证。
_CLOSE_EPOCH_ANCHOR = ContextVar("close_epoch_open_anchor", default=None)


class CloseEvidenceError(ValueError):
    """A close cannot be attributed from complete, conflict-free evidence."""


class _CloseBudget:
    def __init__(self):
        self.deadline = time.monotonic() + CLOSE_EVIDENCE_BUDGET_SEC
        self.orders = set()
        self.details = {}

    def timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0.05:
            raise CloseEvidenceError("close_evidence_time_budget_exhausted")
        return min(CLOSE_QUERY_TIMEOUT_SEC, remaining)

    def claim(self, oid):
        self.timeout()
        if oid not in self.orders and len(self.orders) >= CLOSE_ORDER_LOOKUP_LIMIT:
            raise CloseEvidenceError("close_evidence_order_budget_exhausted")
        self.orders.add(oid)


def _row_value(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _close_raw_ids(row):
    raw = _row_value(row, "raw")
    if raw in (None, ""):
        return set()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise CloseEvidenceError("invalid_recorded_close_raw") from exc
    if not isinstance(raw, dict):
        raise CloseEvidenceError("invalid_recorded_close_raw")
    values = [raw.get("ordId"), raw.get("ord_id")]
    ids = raw.get("ord_ids") or []
    if not isinstance(ids, (list, tuple)):
        raise CloseEvidenceError("invalid_recorded_close_order_ids")
    values.extend(ids)
    for item in raw.get("fills") or []:
        if not isinstance(item, dict):
            raise CloseEvidenceError("invalid_recorded_close_fill")
        values.append(item.get("ordId"))
    return {str(value).strip() for value in values if value not in (None, "")}


def _open_raw_ids(row):
    """已记账 open/add 行携带的订单身份（宽容读：坏 raw 视为无身份，不抛）。"""
    raw = _row_value(row, "raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return set()
    if not isinstance(raw, dict):
        return set()
    values = [raw.get("ordId"), raw.get("ord_id"), raw.get("open_id")]
    ids = raw.get("ord_ids")
    if isinstance(ids, (list, tuple)):
        values.extend(ids)
    for item in raw.get("fills") or []:
        if isinstance(item, dict):
            values.append(item.get("ordId"))
    return {str(value).strip() for value in values if value not in (None, "")}


def _finite_close_number(value, name, *, positive=False):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CloseEvidenceError(f"invalid_close_{name}") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise CloseEvidenceError(f"invalid_close_{name}")
    return number


def _merge_close_fills(fills, sym, side):
    """Merge endpoint/order reads without losing conflicting trade identities."""
    merged = {}
    expected_side = "sell" if side == "long" else "buy"
    for item in fills:
        if not isinstance(item, dict):
            raise CloseEvidenceError("invalid_close_fill")
        if item.get("instId") not in (None, "", sym):
            raise CloseEvidenceError("close_fill_instrument_mismatch")
        if item.get("side") not in (None, "", expected_side):
            raise CloseEvidenceError("close_fill_direction_mismatch")
        if item.get("posSide") in ("long", "short") and item["posSide"] != side:
            raise CloseEvidenceError("close_fill_position_side_mismatch")
        oid, tid = str(item.get("ordId") or "").strip(), str(item.get("tradeId") or "").strip()
        if not oid or oid == "?" or not tid:
            raise CloseEvidenceError("close_fill_identity_missing")
        signature = (oid,
                     _finite_close_number(item.get("fillTime"), "time", positive=True),
                     _finite_close_number(item.get("fillSz"), "quantity", positive=True),
                     _finite_close_number(item.get("fillPx"), "price", positive=True),
                     _finite_close_number(item.get("fillPnl"), "pnl"))
        if tid in merged:
            previous, previous_signature = merged[tid]
            if previous_signature != signature:
                raise CloseEvidenceError(f"conflicting_close_trade_id:{tid}")
            if previous.get("fee") not in (None, "") and item.get("fee") not in (None, ""):
                if float(previous["fee"]) != float(item["fee"]) or previous.get("feeCcy") != item.get("feeCcy"):
                    raise CloseEvidenceError(f"conflicting_close_trade_fee:{tid}")
            continue
        merged[tid] = (item, signature)
    return [row for row, _ in merged.values()]


def _close_api(budget, *args, profile):
    payload = okx_json(*args, global_args=["--profile", profile],
                       timeout_sec=budget.timeout(), retries=0)
    if isinstance(payload, dict) and (payload.get("ok") is False or
            payload.get("code", "0") not in (None, 0, "0")):
        raise CloseEvidenceError("close_api_failed")
    return payload


def _fills_page_reaches_window(rows, t0_ms, cap=None, anchor=None):
    """Whether one un-paginated page proves the view reaches back to t0_ms.

    A page holding a fill at or older than the window start has crossed the
    boundary and proves coverage by itself.  A short page proves coverage only
    for the archive source (``cap`` supplied), whose horizon spans any window
    this tool reconciles.  The recent source is server-bounded to about three
    days, so a short recent page says nothing about older fills -- that is how
    a 37-row recent page hid the 2026-09-11 SOXL close from its own window.

    ``anchor`` is ``(ordId, sz)`` of one order whose total size is known
    apart from the page: the active epoch's first recorded open on the close
    path, the receipt-verified intent order on the open path
    (`receipt_open_anchor`).  A page is the instrument's newest fills, newest
    first, so a page holding every fill of that order (fillSz summing to sz)
    also holds every later fill -- each close of the epoch, or each open leg
    from the intent order on.  Only fills older than that order yet no older
    than t0_ms (its padding for ledger-ts write lag, or for the minutes before
    the intent was reserved) can stay off the page.  2026-09-11 BCH: first
    opened 11:22 after three days without a BCH fill, so its 28-row recent
    page could never cross t0 while that day's fills alone filled the 20-row
    archive page; a fresh-symbol open filled in 20+ parts (2026-09-09 XPL: 31)
    overflows the archive page by itself.
    """
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get("fillTime") or 0) <= t0_ms:
                return True
        except (TypeError, ValueError):
            continue
    if anchor is not None and _page_holds_order(rows, *anchor):
        return True
    return cap is not None and len(rows) < cap


def _page_holds_order(rows, ord_id, sz):
    """Every fill of `ord_id` is on the page: its fillSz sum equals `sz`."""
    total = 0.0
    for item in rows:
        if isinstance(item, dict) and str(item.get("ordId") or "") == ord_id:
            try:
                total += float(item.get("fillSz"))
            except (TypeError, ValueError):
                return False
    return total > SZ_TOL and abs(total - sz) <= SZ_TOL


def _fetch_reduce_order_fills(profile, sym, side, t0_ms, ord_id=None):
    budget = _CLOSE_BUDGET.get() or _CloseBudget()
    if ord_id:
        budget.claim(str(ord_id))
    merged, errors, covered = [], [], False
    for extra in ([], ["--archive"]):
        args = ["swap", "fills", "--instId", sym]
        if ord_id:
            args += ["--ordId", str(ord_id)]
        try:
            rows = rows_of(_close_api(budget, *args, *extra, profile=profile))
        except CloseEvidenceError:
            raise
        except Exception as exc:
            errors.append(str(exc))
            continue
        if not isinstance(rows, list):
            raise CloseEvidenceError("invalid_close_fills_response")
        if _fills_page_reaches_window(
                rows, t0_ms, ARCHIVE_FILLS_PAGE_CAP if extra else None,
                None if ord_id else _CLOSE_EPOCH_ANCHOR.get()):
            covered = True
        for item in rows:
            if not isinstance(item, dict):
                raise CloseEvidenceError("invalid_close_fill")
            if ord_id and str(item.get("ordId") or "") != str(ord_id):
                raise CloseEvidenceError("close_order_filter_mismatch")
            if item.get("instId") not in (None, "", sym):
                raise CloseEvidenceError("close_fill_instrument_mismatch")
            if int(item.get("fillTime") or 0) < t0_ms:
                continue
            if item.get("side") != ("sell" if side == "long" else "buy"):
                continue
            ps = item.get("posSide")
            if ps in ("long", "short") and ps != side:
                continue
            if ps not in ("long", "short") and abs(f(item.get("fillPnl"), 0) or 0) <= 1e-12:
                continue
            merged.append(item)
    if errors and not merged:
        raise CloseEvidenceError("close_fills_unavailable:" + ";".join(errors)[:300])
    if not ord_id and not covered:
        # 定向 ordId 取证有自己的完备性证明（组张数 == 订单 accFillSz，见
        # `_prepare_close_groups`），不适用窗口覆盖判据；无 ordId 的窗口扫描
        # 证不到覆盖就必须失败关闭，交给上层报 FUZZY。
        raise CloseEvidenceError("close_fills_window_coverage_unproven")
    return _merge_close_fills(merged, sym, side)


def _read_close_order(profile, sym, side, oid):
    budget = _CLOSE_BUDGET.get() or _CloseBudget()
    budget.claim(oid)
    if oid not in budget.details:
        payload = _close_api(budget, "swap", "get", "--instId", sym,
                             "--ordId", oid, profile=profile)
        rows = rows_of(payload)
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or len(rows) != 1:
            raise CloseEvidenceError("close_order_not_unique")
        order = rows[0]
        if (not isinstance(order, dict) or str(order.get("ordId")) != oid or
                order.get("instId") != sym or order.get("side") != ("sell" if side == "long" else "buy") or
                order.get("posSide") not in (side, "net") or
                order.get("state") not in ("filled", "canceled")):
            raise CloseEvidenceError("close_order_terminal_or_scope_unverified")
        _finite_close_number(order.get("accFillSz"), "order_quantity", positive=True)
        _finite_close_number(order.get("avgPx"), "order_price", positive=True)
        budget.details[oid] = order
    return budget.details[oid]


def _close_group_matches_row(selected, row):
    quantity = sum(g["sz"] for g in selected)
    if not selected or abs(quantity - _finite_close_number(row["sz"], "ledger_quantity", positive=True)) > SZ_TOL:
        return False
    price = sum(g["sz"] * g["wavg_px"] for g in selected) / quantity
    pnl = sum(g["pnl"] for g in selected)
    return (math.isclose(price, _finite_close_number(row["fill_px"], "ledger_price", positive=True), rel_tol=1e-8, abs_tol=1e-8) and
            math.isclose(pnl, _finite_close_number(row["pnl"], "ledger_pnl"), rel_tol=1e-8, abs_tol=1e-6))


def _prepare_close_groups(profile, sym, side, fills, rows, t0_dt):
    """Complete observed historical orders, then attest all remaining order groups."""
    fills = _merge_close_fills(fills, sym, side)
    t0_ms = int(t0_dt.timestamp() * 1000)
    for row in rows:
        if (row["action"] or "").lower() not in CLOSE_ACTIONS:
            continue
        ids = _close_raw_ids(row)
        groups = group_by_ord(fills)
        selected = [g for g in groups if g["ordId"] in ids]
        if not selected:
            continue  # No fetched fragment of this old recorded order.
        if {g["ordId"] for g in selected} != ids or not _close_group_matches_row(selected, row):
            if sum(g["sz"] for g in selected) > float(row["sz"]) + SZ_TOL:
                raise CloseEvidenceError("recorded_close_order_quantity_excess")
            for oid in sorted(ids):
                fills = _merge_close_fills(fills + _fetch_reduce_order_fills(profile, sym, side, t0_ms, oid), sym, side)
    groups = group_by_ord(fills)
    remaining, notes = consume_recorded(groups, rows, t0_dt)
    for group in list(remaining):
        oid = group["ordId"]
        order = _read_close_order(profile, sym, side, oid)
        quantity = float(order["accFillSz"])
        if group["sz"] < quantity - SZ_TOL:
            fills = _merge_close_fills(fills + _fetch_reduce_order_fills(profile, sym, side, t0_ms, oid), sym, side)
            group = next(g for g in group_by_ord(fills) if g["ordId"] == oid)
        if (abs(group["sz"] - quantity) > SZ_TOL or
                not math.isclose(group["wavg_px"], float(order["avgPx"]), rel_tol=1e-8, abs_tol=1e-8) or
                (order.get("pnl") not in (None, "") and not math.isclose(group["pnl"], float(order["pnl"]), rel_tol=1e-8, abs_tol=1e-6))):
            raise CloseEvidenceError("close_order_fills_incomplete_or_mismatched")
    groups = group_by_ord(fills)
    remaining, notes = consume_recorded(groups, rows, t0_dt)
    return groups, remaining, notes


def _completed_business_terminal_context(raw, cycle_id):
    """Return a proven completed runner terminal, otherwise an empty dict.

    Reconciliation is allowed to add already-occurred exchange fills, not to
    manufacture a successful runner terminal.  Preserve the exact old proof
    only when every state bit and the terminal timestamp/cycle validate.
    """
    if not isinstance(raw, dict):
        return {}
    terminal = raw.get("business_terminal")
    if not (
        raw.get("status") == "ok"
        and raw.get("batch_status") == "completed"
        and raw.get("batch_ok") is True
        and raw.get("runner_in_progress") is False
        and isinstance(terminal, dict)
        and terminal.get("schema_version") == 1
        and terminal.get("cycle_id") == cycle_id
        and terminal.get("status") == "completed"
        and trades_writer.strict_cst_ts(
            terminal.get("completed_at_cst")) is not None
        and isinstance(terminal.get("clock_stop"), str)
        and terminal.get("clock_stop").strip()
    ):
        return {}
    context = {
        "status": "ok",
        "batch_status": "completed",
        "batch_ok": True,
        "runner_in_progress": False,
        "business_terminal": dict(terminal),
        "business_terminal_preserved_after_reconcile": True,
    }
    for key in (
        "facts_hash",
        "plan_sha256",
        "position_action_plan_hash",
        "action_taken",
    ):
        value = raw.get(key)
        if value not in (None, "", [], {}):
            context[key] = value
    return context


def _active_runner_progression_context(raw, cycle_id):
    """Preserve a proven same-cycle runner interim across reconciliation.

    A protective close can be reconciled while the deterministic runner is
    still committing later actions from the same plan.  Replacing the cycle
    header with reconciliation-only raw would erase the three binding hashes;
    the runner's later superset receipt would then look like an ambiguous
    partial overlap and its newly confirmed fill would not be persisted.

    This does not manufacture a successful terminal.  It preserves only the
    exact active ``partial`` state and all three SHA-256 bindings so the writer
    can prove the subsequent receipt is a continuation of that same plan.
    """
    if not isinstance(raw, dict):
        return {}
    if not (
        raw.get("status") == "ok"
        and raw.get("batch_status") == "partial"
        and raw.get("batch_ok") is True
        and raw.get("runner_in_progress") is True
        and raw.get("business_terminal") in (None, {})
        and raw.get("cycle_id") == cycle_id
    ):
        return {}
    binding = {}
    for key in (
        "facts_hash",
        "plan_sha256",
        "position_action_plan_hash",
    ):
        value = raw.get(key)
        if not (
            isinstance(value, str)
            and len(value) == 64
            and all(ch in "0123456789abcdef" for ch in value)
        ):
            return {}
        binding[key] = value
    context = {
        "status": "ok",
        "batch_status": "partial",
        "batch_ok": True,
        "runner_in_progress": True,
        "runner_progression_preserved_after_reconcile": True,
        # Re-state the proven cycle: this context becomes the header raw
        # of the maintenance write, and the next same-slot heal (or the
        # runner receipt) re-checks it before trusting the bindings.
        "cycle_id": cycle_id,
        **binding,
    }
    action_taken = raw.get("action_taken")
    if action_taken not in (None, "", [], {}):
        context["action_taken"] = action_taken
    return context


def _report_business_context(raw, cycle_id):
    """从原业务终态回执中提取有界的报告上下文。

    交易所保护单可能在 Agent 已落 HOLD/WAIT 终态后成交。对账回收同一
    cycle 时会重写 ``trade_cycles.raw``；如果只保留超长 raw 的文本前缀，
    Push 就会丢失同轮 ``decision_card_v1``、Live facts 与组合 IMR。

    这里只保留报告实际消费的已验证字段；特意不携带 live_facts.exchange
    原始 API 大块，避免多次对账时 raw 无界嵌套增长。
    """
    if not isinstance(raw, dict):
        return {}

    context = _completed_business_terminal_context(raw, cycle_id)
    if not context:
        context = _active_runner_progression_context(raw, cycle_id)
    minimal_policy = thresholds.minimal_decision_contract_active(cycle_id)
    closure_policy = thresholds.minimal_contract_closure_active(cycle_id)
    card = raw.get(
        OPEN_EXECUTION_PACKAGE_KEY if closure_policy else "decision_card")
    protocol = raw.get("decision_protocol")
    if minimal_policy:
        # minimal 周期只保留裁决结论/持仓复核，不把旧六项卡随对账
        # 重写回 trade_cycles.raw。周期边界是权威来源，不信任 raw 自报。
        context["decision_protocol"] = MINIMAL_DECISION_PROTOCOL
        reasoning = raw.get("reasoning") or raw.get("reason")
        if isinstance(reasoning, str) and reasoning.strip():
            context["reasoning"] = reasoning.strip()
        reviews = raw.get("position_reviews")
        if isinstance(reviews, (dict, list)):
            context["position_reviews"] = reviews
        if closure_policy and is_open_execution_package(card):
            context[OPEN_EXECUTION_PACKAGE_KEY] = card
    elif isinstance(card, dict) and card:
        context["decision_card"] = card
        if protocol:
            context["decision_protocol"] = protocol

    facts = raw.get("live_facts")
    if isinstance(facts, dict):
        report_fact_keys = (
            "schema_version", "source", "cycle_id", "profile", "status",
            "as_of", "as_of_ms", "position_truth_verified", "balance",
            "positions", "errors",
        )
        compact_facts = {
            key: facts[key] for key in report_fact_keys if key in facts
        }
        if compact_facts:
            context["live_facts"] = compact_facts

    if context:
        context["business_context_preserved"] = True
        context["business_context_source_cycle_id"] = cycle_id
    return context


def f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def norm_side(s):
    s = (s or "").lower()
    if s in ("sell", "short"):
        return "short"
    if s in ("buy", "long"):
        return "long"
    return s or "?"


def rows_of(payload):
    if isinstance(payload, dict):
        return payload.get("data") or []
    return payload or []


def parse_ts(s):
    """账本 ts（UTC+8 字符串，先经 writer normalize）→ aware datetime；坏格式返 None。"""
    try:
        return datetime.strptime(trades_writer.normalize_ts(str(s or "")), TS_FMT).replace(tzinfo=CST)
    except (ValueError, TypeError):
        return None


def fill_dt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=CST)


def slot_cycle_id(dt_obj):
    """UTC+8 datetime → 所在 15min 槽 cycle_id 'YYYY-MM-DDTHH:MM'。"""
    return dt_obj.replace(minute=(dt_obj.minute // 15) * 15).strftime("%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------------------
# 账本 / 现仓
# ---------------------------------------------------------------------------
class _LedgerRows(defaultdict):
    def __init__(self, con):
        super().__init__(list)
        self.connection = con


def _close_identity_rows(by_key, key):
    rows = by_key[key]
    con = getattr(by_key, "connection", None)
    if con is None:
        return rows
    ids = [r["id"] for r in rows if (r["action"] or "").lower() in CLOSE_ACTIONS]
    raw_by_id = {}
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        placeholders = ",".join("?" for _ in batch)
        raw_by_id.update((r["id"], r["raw"]) for r in con.execute(
            f"SELECT id,raw FROM trades WHERE id IN ({placeholders})", batch))
    return [{**dict(row), "raw": raw_by_id.get(row["id"])} for row in rows]


def ledger_rows(con):
    """全量 trades 行 → {(symbol, side): [row, ...]}（rowid 序）。"""
    by_key = _LedgerRows(con)
    for r in con.execute(
            "SELECT id, cycle_id, ts, symbol, action, side, sz, fill_px, lev, pnl "
            "FROM trades ORDER BY rowid"):
        by_key[(r["symbol"], norm_side(r["side"]))].append(r)
    return by_key


def _active_close_epoch(rows):
    """Fence off quantity-flat history outside both accepted time windows.

    This only selects the evidence window for a new close. It does not certify,
    change or repair historical rows. Ambiguous chronology, invalid quantities,
    negative prefixes and rapid re-entry retain the full-history path. Current
    fills still require exact order identities, complete order totals and the
    unchanged ghost-quantity match.
    """
    original = list(rows)
    ordered = []
    try:
        for row in original:
            action = (row['action'] or '').lower()
            if action not in (*OPEN_ACTIONS, *CLOSE_ACTIONS):
                continue
            at = parse_ts(row['ts'])
            quantity = Decimal(str(row['sz']))
            if at is None or not quantity.is_finite() or quantity <= 0:
                return original, []
            ordered.append((at, int(row['id']), action, quantity, row))
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return original, []
    ordered.sort(key=lambda item: (item[0], item[1]))
    total = Decimal(0)
    tolerance = Decimal(str(SZ_TOL))
    boundary = None
    separation = timedelta(minutes=CONSUME_WINDOW_MIN + OPEN_TS_BUFFER_MIN)
    for index, (at, _, action, quantity, _) in enumerate(ordered):
        total += quantity if action in OPEN_ACTIONS else -quantity
        if total < -tolerance:
            return original, []
        if abs(total) <= tolerance and index + 1 < len(ordered):
            next_at, _, next_action, _, _ = ordered[index + 1]
            if next_action == 'open' and next_at - at > separation:
                boundary = index + 1
    if boundary is None or total <= tolerance:
        return original, []
    selected = [item[4] for item in ordered[boundary:]]
    selected_total = sum((item[3] if item[2] in OPEN_ACTIONS else -item[3]
                          for item in ordered[boundary:]), Decimal(0))
    if abs(selected_total - total) > tolerance:
        return original, []
    previous_at = ordered[boundary - 1][0]
    start_at = ordered[boundary][0]
    return selected, [
        f"本次仅核对活动持仓段：此前{boundary}行数量已归零，"
        f"末行时间={previous_at.strftime(TS_FMT)}；"
        f"新开仓={start_at.strftime(TS_FMT)}，间隔大于历史核销与开仓缓冲窗合计；"
        "旧段不改写且不据此宣称历史成交完整"]


def _epoch_open_anchor(by_key, rows):
    """(ordId, sz) of the first open in `rows`, or None when it is not provable.

    Coverage evidence for `_fills_page_reaches_window` only, never a match
    input.  An unparseable or tied first ts, a raw without exactly one order
    identity, a bad quantity or an unreadable raw all yield None, which leaves
    the plain window criterion in force.
    """
    try:
        opens = []
        for row in rows:
            if (row["action"] or "").lower() not in OPEN_ACTIONS:
                continue
            at = parse_ts(row["ts"])
            if at is None:
                return None
            opens.append((at, int(row["id"]), row))
        if not opens:
            return None
        opens.sort(key=lambda item: (item[0], item[1]))
        if len(opens) > 1 and opens[1][0] == opens[0][0]:
            return None
        _, row_id, first = opens[0]
        raw = _row_value(first, "raw")
        con = getattr(by_key, "connection", None)
        if raw in (None, "") and con is not None:
            hit = con.execute("SELECT raw FROM trades WHERE id=?", (row_id,)).fetchone()
            raw = hit[0] if hit else None
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            return None
        ids = [raw.get("ordId"), raw.get("ord_id")]
        listed = raw.get("ord_ids") or []
        if not isinstance(listed, (list, tuple)):
            return None
        ids.extend(listed)
        for item in raw.get("fills") or []:
            if not isinstance(item, dict):
                return None
            ids.append(item.get("ordId"))
        ids = {str(value).strip() for value in ids if value not in (None, "")}
        quantity = float(first["sz"])
        if len(ids) != 1 or not math.isfinite(quantity) or quantity <= SZ_TOL:
            return None
        return next(iter(ids)), quantity
    except Exception:  # noqa: BLE001 —— 锚点取不到只是退回窗口判据
        return None


def net_of(rows):
    net = 0.0
    for r in rows:
        act = (r["action"] or "").lower()
        sz = f(r["sz"], 0.0) or 0.0
        if act in ("open", "add"):
            net += sz
        elif act in ("close", "stop_loss", "reduce"):
            net -= sz
    return net


def venue_positions(profile):
    """OKX API 现仓 {(instId, side): sz}。失败抛异常（exit 2）。"""
    rows = rows_of(okx_json("account", "positions", "--instType", "SWAP",
                            global_args=["--profile", profile]))
    out = defaultdict(float)
    for r in rows:
        if not isinstance(r, dict):
            continue
        pos = f(r.get("pos"), 0.0)
        if not pos:
            continue
        side = r.get("posSide") if r.get("posSide") in ("long", "short") else (
            "long" if pos > 0 else "short")
        out[(r.get("instId"), side)] += abs(pos)
    return dict(out)


# ---------------------------------------------------------------------------
# fills 回读 + ordId 分组 + 已记账销账
# ---------------------------------------------------------------------------
def fetch_reduce_fills(profile, sym, side, t0_ms):
    """回读 sym 的反向平仓成交（recent + --archive 合并、tradeId 去重）。

    平仓腿判定：side=反向（long→sell / short→buy）且 posSide 匹配持仓方向；
    posSide 非 long/short（net 模式历史）时要求 fillPnl≠0。

    CLI 不支持 fills 分页，只能拿首页；首页证不到覆盖至 t0_ms 时抛
    `close_fills_window_coverage_unproven`（上层记 FUZZY），不返回截断视图。
    """
    return _fetch_reduce_order_fills(profile, sym, side, t0_ms)


def fetch_open_fills(profile, sym, side, t0_ms, anchor=None):
    """回读 sym 的**开仓腿**成交（P2·2026-08-04）——`fetch_reduce_fills` 的镜像。

    开仓腿判定：side=同向（long→buy / short→sell）且 posSide 匹配持仓方向；
    posSide 非 long/short（net 模式历史）时要求 fillPnl==0（开仓不产生已实现盈亏，
    与平仓腿的 fillPnl≠0 正好互补）。

    与平仓腿同样受「CLI 无 fills 分页」约束：首页证不到覆盖至 t0_ms 就抛
    RuntimeError，由 `ledger_autoheal._plan_unrecorded` 记 T3（只报告不写库）。
    ``anchor``（`receipt_open_anchor` 的结果）整单在某一页内同样自证覆盖：新标的
    前三天无成交时 recent 页越不过 t0_ms，开仓单一拆 20 笔以上又独占 archive 页，
    旧判据下严格 T1 补开仓只能落 T3（2026-09-09 XPL 一单 31 笔即此形态）。
    """
    open_side = "buy" if side == "long" else "sell"
    merged, seen = [], set()
    errors = []
    covered = False
    for extra in ([], ["--archive"]):
        try:
            fills = rows_of(okx_json("swap", "fills", "--instId", sym, *extra,
                                     global_args=["--profile", profile]))
        except Exception as e:  # noqa: BLE001 —— 单源失败不致命，两源全失败才报
            errors.append(f"fills{' --archive' if extra else ''} 失败: {e}")
            continue
        if _fills_page_reaches_window(
                fills if isinstance(fills, list) else [], t0_ms,
                ARCHIVE_FILLS_PAGE_CAP if extra else None, anchor):
            covered = True
        for x in fills:
            if not isinstance(x, dict):
                continue
            key = x.get("tradeId") or (
                f"{x.get('ordId')}|{x.get('fillTime')}|{x.get('fillSz')}|{x.get('fillPx')}")
            if key in seen:
                continue
            seen.add(key)
            if int(x.get("fillTime") or 0) < t0_ms:
                continue
            if x.get("side") != open_side:
                continue
            ps = x.get("posSide")
            if ps in ("long", "short"):
                if ps != side:
                    continue
            elif abs(f(x.get("fillPnl"), 0.0) or 0.0) > 1e-12:
                continue  # net 模式无 posSide：以 fillPnl==0 认开仓腿
            merged.append(x)
    if not merged and len(errors) >= 2:
        raise RuntimeError("; ".join(errors))
    if not covered:
        # 同 `_fetch_reduce_order_fills`：首页没覆盖到 t0_ms 就无法证明
        # 「缺口只能由这些开仓腿解释」，按证据不足交回调用方（T3，不写库）。
        raise RuntimeError(
            "开仓腿 fills 首页未覆盖窗口起点（CLI 不支持 fills 分页）——证据不足")
    return merged


def receipt_open_anchor(intent):
    """(ordId, sz) of a receipt-verified intent order, or None when unprovable.

    Coverage evidence for `fetch_open_fills` only, never a match input.  The
    size is the executor's stored receipt of that exact order, never read off
    the page being judged.  An intent without a verified receipt, a receipt
    trade naming another order, or a size that is missing, not finite, not
    positive or contradicted by ``fill_sz`` yields None, which leaves the plain
    window criterion in force.
    """
    if (not isinstance(intent, dict)
            or intent.get("completed_receipt_verified") is not True):
        return None
    trade = intent.get("receipt_trade")
    ord_id = str(intent.get("ord_id") or "").strip()
    if not isinstance(trade, dict) or not ord_id or ord_id != str(
            trade.get("ordId") or trade.get("ord_id") or "").strip():
        return None
    try:
        sizes = [float(trade.get("sz"))]
        if trade.get("fill_sz") is not None:
            sizes.append(float(trade.get("fill_sz")))
    except (TypeError, ValueError):
        return None
    if (not all(math.isfinite(value) for value in sizes) or sizes[0] <= SZ_TOL
            or abs(sizes[-1] - sizes[0]) > SZ_TOL):
        return None
    return ord_id, sizes[0]


def group_by_ord(fills):
    """fills → [{ordId, sz, pnl, wavg_px, t_last_ms, fills}]（按时间升序）。"""
    groups = defaultdict(list)
    for x in fills:
        groups[x.get("ordId") or "?"].append(x)
    out = []
    for oid, xs in groups.items():
        sz = sum(f(x.get("fillSz"), 0.0) or 0.0 for x in xs)
        pnl = sum(f(x.get("fillPnl"), 0.0) or 0.0 for x in xs)
        wavg = (sum((f(x.get("fillPx"), 0.0) or 0.0) * (f(x.get("fillSz"), 0.0) or 0.0)
                    for x in xs) / sz) if sz else 0.0
        out.append({"ordId": oid, "sz": sz, "pnl": pnl, "wavg_px": wavg,
                    "t_last_ms": max(int(x.get("fillTime") or 0) for x in xs),
                    "fills": xs})
    out.sort(key=lambda g: g["t_last_ms"])
    return out


CLOSE_ACTIONS = ("close", "stop_loss", "reduce")
OPEN_ACTIONS = ("open", "add")


def consume_recorded(groups, rows, t0_dt, actions=CLOSE_ACTIONS):
    """CLOSE按已记录订单身份核销；OPEN保留原数量/时间契约。

    `actions` 默认平仓腿（幽灵仓补 close 用）；P2 补 open 时传 `OPEN_ACTIONS`
    销账已记录的开仓行，逻辑完全对称。

    返回 (remaining_groups, consume_notes)。销不掉的账本行只记备注
    （可能超 fills API 窗口）——最终以「剩余组合计 == 目标 sz」硬门兜底。
    """
    if tuple(actions) == CLOSE_ACTIONS:
        notes, remaining, claimed = [], list(groups), set()
        for row in rows:
            if (row["action"] or "").lower() not in CLOSE_ACTIONS:
                continue
            r_dt = parse_ts(row["ts"])
            if r_dt is None or (t0_dt is not None and r_dt < t0_dt):
                continue
            ids = _close_raw_ids(row)
            if ids & claimed:
                raise CloseEvidenceError("duplicate_recorded_close_order_identity")
            claimed.update(ids)
            selected = [g for g in remaining if g["ordId"] in ids]
            if not ids:
                if any(abs((fill_dt(g["t_last_ms"]) - r_dt).total_seconds()) <= CONSUME_WINDOW_MIN * 60 for g in remaining):
                    raise CloseEvidenceError("recorded_close_identity_missing")
                notes.append(f"账本行 id={row['id']} 无订单身份且本次无邻近成交，未销账")
                continue
            if not selected:
                notes.append(f"账本行 id={row['id']} 的订单不在本次成交回包，未销账")
                continue
            if {g["ordId"] for g in selected} != ids or not _close_group_matches_row(selected, row):
                raise CloseEvidenceError(f"recorded_close_identity_economics_mismatch:row={row['id']}")
            remaining = [g for g in remaining if g not in selected]
            notes.append(f"账本行 id={row['id']} 按 ordId={','.join(sorted(ids))} 精确核销 sz={row['sz']}")
        return remaining, notes
    # OPEN/UNRECORDED retains its existing consumption contract.
    notes, remaining = [], list(groups)
    for r in rows:
        act = (r["action"] or "").lower()
        if act not in actions:
            continue
        r_dt = parse_ts(r["ts"])
        if r_dt is None or (t0_dt is not None and r_dt < t0_dt):
            continue
        r_sz = f(r["sz"], 0.0) or 0.0
        best, best_gap = None, None
        for g in remaining:
            if abs(g["sz"] - r_sz) > SZ_TOL:
                continue
            gap = abs((fill_dt(g["t_last_ms"]) - r_dt).total_seconds())
            if gap <= CONSUME_WINDOW_MIN * 60 and (best_gap is None or gap < best_gap):
                best, best_gap = g, gap
        if best is not None:
            remaining.remove(best)
            notes.append(f"账本行 id={r['id']}({act} sz={r_sz} ts={r['ts']}) ↔ "
                         f"ordId={best['ordId']} 已销账")
        else:
            notes.append(f"账本行 id={r['id']}({act} sz={r_sz} ts={r['ts']}) 无对应 fills 组"
                         f"（可能超 API 窗口，靠合计硬门兜底）")
    return remaining, notes


def find_journal_close(db_path, profile, sym, ord_ids):
    """按 ordId 从 append-only 执行 journal 找已确认 close；找不到返回 None。"""
    wanted = {str(value) for value in ord_ids if value not in (None, "")}
    if not wanted:
        return None
    path = Path(db_path).parent / "journal" / f"exec_{profile}.jsonl"
    if not path.is_file():
        return None
    found = None
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            trade = record.get("trade")
            if not isinstance(trade, dict):
                continue
            if str(trade.get("symbol") or "") != str(sym):
                continue
            if str(trade.get("action") or "").lower() != "close":
                continue
            trade_ord_ids = {
                str(trade.get("ordId") or ""),
                *[
                    str(value)
                    for value in ((trade.get("raw") or {}).get("ord_ids") or [])
                ],
            }
            if wanted.isdisjoint(trade_ord_ids):
                continue
            found = {
                "line_no": line_no,
                "record": record,
                "trade": trade,
                "path": str(path),
            }
    return found


def repair_existing_from_journal(db_path, profile, ord_id, con_ro):
    """把已补账但误标为 SL 的 close 元数据改为执行 journal 实情。

    仅在唯一 trade 行明确含目标 ordId、且 journal 的 symbol/side/sz/px/pnl
    与主账一致时允许写；成交事实不变，仍经 trades_writer 整 cycle 重写。
    """
    matches = []
    rows = con_ro.execute(
        "SELECT id,cycle_id,ts,symbol,action,side,sz,fill_px,lev,margin,notional,"
        "score_total,reasoning,deviation,degradation,pnl,raw "
        "FROM trades ORDER BY rowid"
    ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row["raw"] or "{}")
        except (json.JSONDecodeError, TypeError):
            raw = {}
        row_ord_ids = {
            str(raw.get("ordId") or ""),
            *[str(value) for value in (raw.get("ord_ids") or [])],
        }
        if str(ord_id) in row_ord_ids:
            matches.append((row, raw))
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError(
            f"ordId={ord_id} 主账命中 {len(matches)} 行，拒绝元数据校正"
        )
    target, target_raw = matches[0]

    journal = find_journal_close(
        db_path, profile, target["symbol"], [str(ord_id)]
    )
    if not journal:
        raise RuntimeError(f"ordId={ord_id} 未找到执行 journal close，拒绝校正")
    jt = journal["trade"]
    comparisons = (
        ("side", str(target["side"]), str(jt.get("side"))),
        ("sz", f(target["sz"], 0.0), f(jt.get("sz"), 0.0)),
        ("fill_px", f(target["fill_px"], 0.0), f(jt.get("fill_px"), 0.0)),
        ("pnl", f(target["pnl"], 0.0), f(jt.get("pnl"), 0.0)),
    )
    for name, actual, expected in comparisons:
        if name == "side":
            equal = actual == expected
        else:
            equal = abs(actual - expected) <= SZ_TOL
        if not equal:
            raise RuntimeError(
                f"ordId={ord_id} journal {name} 不一致: ledger={actual} journal={expected}"
            )

    cycle_id = target["cycle_id"]
    prev = con_ro.execute(
        "SELECT cycle_id,ts,decision,n_orders,equity,note,raw "
        "FROM trade_cycles WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if not prev:
        raise RuntimeError(f"cycle={cycle_id} trade_cycles 缺失，拒绝校正")
    clean_note = f"execution journal 已核验主动平仓 ordId={ord_id}"
    if (
        target_raw.get("reconcile_source") == "execution_journal_recovery"
        and clean_note in (prev["note"] or "")
        and "exchange-side SL" not in (prev["note"] or "")
        and (target["reasoning"] or "") == (jt.get("reason") or "")
    ):
        return {"status": "already_consistent", "cycle_id": cycle_id}
    cycle_trades = [
        dict(row)
        for row in con_ro.execute(
            "SELECT symbol,action,side,sz,fill_px,lev,margin,notional,"
            "score_total,reasoning,deviation,degradation,pnl,raw "
            "FROM trades WHERE cycle_id=? ORDER BY rowid",
            (cycle_id,),
        ).fetchall()
    ]
    replaced = 0
    for trade in cycle_trades:
        try:
            raw = json.loads(trade.get("raw") or "{}")
        except (json.JSONDecodeError, TypeError):
            raw = {}
        row_ord_ids = {
            str(raw.get("ordId") or ""),
            *[str(value) for value in (raw.get("ord_ids") or [])],
        }
        if str(ord_id) not in row_ord_ids:
            continue
        raw.setdefault("original_reconcile_source", raw.get("reconcile_source"))
        raw["reconcile_source"] = "execution_journal_recovery"
        raw["journal_path"] = journal["path"]
        raw["journal_line"] = journal["line_no"]
        raw["journal_ts"] = journal["record"].get("ts")
        raw["action_taken"] = journal["record"].get("action_taken")
        trade["raw"] = raw
        trade["reasoning"] = jt.get("reason") or trade.get("reasoning")
        replaced += 1
    if replaced != 1:
        raise RuntimeError(f"ordId={ord_id} cycle 内替换行数={replaced}，拒绝校正")

    try:
        cycle_raw = json.loads(prev["raw"] or "{}")
    except (json.JSONDecodeError, TypeError):
        cycle_raw = {}
    if not isinstance(cycle_raw, dict):
        cycle_raw = {"original_raw": cycle_raw}
    cycle_raw.setdefault("original_note", prev["note"])
    cycle_raw.setdefault(
        "original_reconcile_source", cycle_raw.get("reconcile_source")
    )
    cycle_raw["reconcile_source"] = "execution_journal_recovery"
    cycle_raw["journal_evidence"] = {
        "path": journal["path"],
        "line": journal["line_no"],
        "ts": journal["record"].get("ts"),
        "ord_id": str(ord_id),
    }
    data = {
        "cycle_id": cycle_id,
        "ts": prev["ts"],
        "decision": prev["decision"],
        "action": (
            f"journal recovery: {target['symbol']} {target['side']} "
            f"close {target['sz']:g} @ {target['fill_px']:.6g}"
        ),
        "note": (
            clean_note
        ),
        "n_orders": len(cycle_trades),
        "equity": prev["equity"],
        "trades": cycle_trades,
        "raw": cycle_raw,
        "_profile": profile,
    }
    result = trades_writer.maintenance_write_trades(
        data,
        Path(db_path),
        trusted_timestamp=data.get("ts"),
        preserve_equity_none=True,
    )
    if not result.get("ok") or result.get("refused"):
        raise RuntimeError(f"trades_writer 拒绝 journal 元数据校正: {result}")
    return {
        "status": "repaired",
        "cycle_id": cycle_id,
        "ord_id": str(ord_id),
        "journal_line": journal["line_no"],
        "writer": result,
    }


# ---------------------------------------------------------------------------
# 补账（--apply，经硬化 writer；目标 cycle 已存在时合并原行防销账）
# ---------------------------------------------------------------------------
def apply_reconcile(db_path, profile, sym, side, ghost_sz, matched, con_ro,
                    open_lev=None):
    """把精确匹配的 fills 组集合补成一行 close（trades_writer.write_trades 直调）。

    write_trades 对 trade_cycles 是 INSERT OR REPLACE、对 trades 是 DELETE+INSERT——
    目标 cycle 已存在时必须先读原行并把原 trades 合并进 payload，否则会销掉同 cycle 已有账。
    open_lev = 该幽灵最近 open/add 行的 lev（writer 补算 margin 用，可 None）。
    """
    all_fills = [x for g in matched for x in g["fills"]]
    tot_sz = sum(f(x.get("fillSz"), 0.0) or 0.0 for x in all_fills)
    tot_pnl = round(sum(f(x.get("fillPnl"), 0.0) or 0.0 for x in all_fills), 6)
    wavg_px = (sum((f(x.get("fillPx"), 0.0) or 0.0) * (f(x.get("fillSz"), 0.0) or 0.0)
                   for x in all_fills) / tot_sz) if tot_sz else None
    close_dt = fill_dt(max(int(x.get("fillTime") or 0) for x in all_fills))
    close_ts = close_dt.strftime(TS_FMT)
    cycle_id = slot_cycle_id(close_dt)
    ord_ids = sorted({g["ordId"] for g in matched})
    if (not matched or not ord_ids or any(not oid or oid == "?" for oid in ord_ids)
            or not math.isfinite(tot_sz) or abs(tot_sz - ghost_sz) > SZ_TOL):
        raise CloseEvidenceError("apply_close_quantity_or_identity_mismatch")
    # The authoritative writer stays the only write entry; reject overlapping
    # identities before composing a merge, and make an exact repeat a no-op.
    for existing in con_ro.execute(
            "SELECT id,cycle_id,ts,symbol,side,action,sz,fill_px,pnl,raw "
            "FROM trades WHERE action IN ('close','stop_loss','reduce')"):
        existing_raw = _row_value(existing, "raw")
        if not any(oid in str(existing_raw or "") for oid in ord_ids):
            continue
        existing_ids = _close_raw_ids(existing)
        if not existing_ids.intersection(ord_ids):
            continue
        if (existing_ids == set(ord_ids) and existing["symbol"] == sym
                and norm_side(existing["side"]) == side
                and _close_group_matches_row(matched, existing)):
            return {"status": "already_recorded", "cycle_id": existing["cycle_id"],
                    "close_ts": close_ts, "sz": tot_sz, "pnl": tot_pnl,
                    "wavg_px": wavg_px, "ord_ids": ord_ids,
                    "writer": {"ok": True, "already_recorded": True},
                    "exp": {"skipped": "already_recorded; verify experience separately"},
                    "merged_prev_trades": 0}
        raise CloseEvidenceError("apply_close_identity_already_claimed")
    journal = find_journal_close(db_path, profile, sym, ord_ids)
    journal_trade = journal["trade"] if journal else {}
    reconcile_source = (
        "execution_journal_recovery" if journal else "exchange_fills_reconcile"
    )

    # 目标 cycle 原行（只读连接查）
    prev = con_ro.execute(
        "SELECT cycle_id, ts, decision, n_orders, equity, note, raw FROM trade_cycles "
        "WHERE cycle_id=?", (cycle_id,)).fetchone()
    prev_trades = con_ro.execute(
        "SELECT ts, symbol, action, side, sz, fill_px, lev, margin, notional, score_total, "
        "reasoning, deviation, degradation, pnl, raw FROM trades WHERE cycle_id=? "
        "ORDER BY rowid", (cycle_id,)).fetchall()

    trades = [dict(t) for t in prev_trades]
    for trade in trades:
        trade["fill_ts"] = trade["ts"]
        old_trade_raw = trade.get("raw")
        if isinstance(old_trade_raw, str):
            try:
                old_trade_raw = json.loads(old_trade_raw)
            except (TypeError, ValueError):
                old_trade_raw = {}
        if not isinstance(old_trade_raw, dict):
            old_trade_raw = {}
        trade["ts_source"] = old_trade_raw.get("ts_source") or "trusted_internal_override"
    fills_evidence = [{"ts": fill_dt(x.get("fillTime")).strftime(TS_FMT),
                       "px": x.get("fillPx"), "sz": x.get("fillSz"),
                       "pnl": x.get("fillPnl"), "ordId": x.get("ordId"),
                       "tradeId": x.get("tradeId"), "execType": x.get("execType")}
                      for x in all_fills[:RAW_FILLS_CAP]]
    reconcile_trade = {
        "symbol": sym,
        "action": "close",
        "side": side,
        "sz": tot_sz,
        "fill_px": round(wavg_px, 8) if wavg_px else None,
        "fill_ts": close_ts,
        "ts_source": "fills.fillTime",
        "lev": open_lev,
        "margin": None,
        "notional": None,
        "score_total": None,
        "reasoning": (
            journal_trade.get("reason")
            or (
                f"reconcile_exchange_closes 补账：交易所侧平仓漏落账；"
                f"fills 实证 {len(all_fills)} 笔 ordId={','.join(ord_ids)} "
                f"平仓时刻={close_ts} pnl={tot_pnl}"
            )
        ),
        "deviation": None,
        "degradation": None,
        "pnl": tot_pnl,
        "raw": {
            "reconcile_source": reconcile_source,
            "close_ts": close_ts,
            "ord_ids": ord_ids,
            "fills": fills_evidence,
            "journal_path": journal.get("path") if journal else None,
            "journal_line": journal.get("line_no") if journal else None,
            "journal_ts": (
                journal["record"].get("ts") if journal else None
            ),
        },
    }
    trades.append(reconcile_trade)

    prev_note = (prev["note"] if prev else "") or ""
    prev_raw_full = None
    prev_raw_obj = None
    if prev and prev["raw"]:
        try:
            parsed = json.loads(prev["raw"])
            prev_raw_full = parsed if isinstance(parsed, dict) else None
            prev_raw_obj = (
                parsed if len(prev["raw"]) < 20000
                else {
                    "_truncated": prev["raw"][:2000],
                    "_original_chars": len(prev["raw"]),
                }
            )
        except (json.JSONDecodeError, TypeError):
            prev_raw_obj = {"_unparsed": str(prev["raw"])[:2000]}

    raw_obj = {
        "reconcile_source": reconcile_source,
        "reconciled_at": datetime.now(CST).strftime(TS_FMT),
        "symbol": sym, "side": side, "ghost_sz": ghost_sz,
        "close_ts": close_ts, "pnl": tot_pnl, "wavg_px": wavg_px,
        "ord_ids": ord_ids,
        "fills": fills_evidence,
        "journal_evidence": (
            {
                "path": journal["path"],
                "line": journal["line_no"],
                "ts": journal["record"].get("ts"),
            }
            if journal
            else None
        ),
        "prev_cycle": ({"decision": prev["decision"], "n_orders": prev["n_orders"],
                        "ts": prev["ts"], "note": prev_note[:1000],
                        "raw": prev_raw_obj} if prev else None),
    }
    raw_obj.update(_report_business_context(prev_raw_full, cycle_id))
    raw_obj["decision"] = "traded"
    raw_obj["n_orders"] = len(trades)
    if raw_obj.get("business_terminal_preserved_after_reconcile") is True:
        raw_obj["reconciled_close_preserved"] = True
        raw_obj["merge_guard_kept_rows"] = len(prev_trades)

    data = {
        "cycle_id": cycle_id,
        # Preserve any existing cycle's completion time, including HOLD with
        # zero trades. The late exchange fill keeps its own fill_ts below.
        "ts": (prev["ts"] if prev else close_ts),
        "decision": "traded",
        "action": (
            f"reconcile: {sym} {side} close {tot_sz:g} @ {wavg_px:.6g} "
            f"({'execution journal' if journal else 'exchange fills'})"
        ),
        "note": (f"reconcile_exchange_closes 补账 pnl={tot_pnl}"
                 + (f" | 原行 note: {prev_note[:300]}" if prev_note else "")),
        "n_orders": len(trades),
        "equity": (prev["equity"] if prev else None),
        "trades": trades,
        "raw": raw_obj,
        "_profile": profile,
    }
    result = trades_writer.maintenance_write_trades(
        data,
        Path(db_path),
        trusted_timestamp=data.get("ts"),
        preserve_equity_none=True,
    )
    if not result.get("ok") or result.get("refused"):
        raise RuntimeError(f"trades_writer 拒绝补账: {result}")
    # 经验库闭环（非致命）：只喂 reconcile 那一行，防止合并进来的原 trades 重复写经验
    exp = trades_writer.write_experiences(
        {"cycle_id": cycle_id, "trades": [reconcile_trade]}, profile, close_ts)
    return {"cycle_id": cycle_id, "close_ts": close_ts, "sz": tot_sz, "pnl": tot_pnl,
            "wavg_px": wavg_px, "ord_ids": ord_ids, "writer": result, "exp": exp,
            "merged_prev_trades": len(prev_trades)}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def match_exact_groups(remaining, target_sz):
    """精确匹配判定（唯一定义源）——幽灵仓补 close 与 UNRECORDED 补 open 共用。

    规则（满足其一才算精确，匹配集张数合计恒 == target_sz）：
      a) 剩余组张数合计 == target_sz → 全部剩余组即匹配集；
      b) 恰有唯一一个剩余组 sz == target_sz → 该组即匹配集（其余为独立未记账成交）。
    返回 (matched_or_None, leftover, reason)；matched 为 None 表示模糊。
    """
    rem_sz = sum(g["sz"] for g in remaining)
    if remaining and abs(rem_sz - target_sz) <= SZ_TOL:
        return list(remaining), [], None
    hits = [g for g in remaining if abs(g["sz"] - target_sz) <= SZ_TOL]
    if len(hits) == 1:
        leftover = [g for g in remaining if g is not hits[0]]
        return hits, leftover, None
    reason = (f"剩余 fills 组无法唯一对齐 sz={target_sz:g}（sz 相等组 {len(hits)} 个）"
              if remaining else "窗口内无未销账的成交")
    return None, list(remaining), reason


def apply_unrecorded(db_path, profile, sym, side, missing_sz, matched, con_ro,
                     *, lev=None, card=None, intent=None, sl_probe=None):
    """P2·2026-08-04：把交易所已有、账本却没记的仓位补成一行 `open`。

    与 `apply_reconcile`（补 close）互为镜像，共用 `maintenance_write_trades` 宽松路径。
    **不编造决策卡**：`card` 由调用方从 `analysis_signals` 取真卡；取不到就传 None，
    此时 payload 不带 `decision_protocol`（合法），并在 degradation 标注卡缺失。

    `intent` = `execution_intents` 归属证据（T1 有 / T2 无）；`sl_probe` = 交易所侧
    algo 止损探测结果。两者都只入 raw 留痕，不参与放行判定（判定在调用方）。

    2026-09-11（主人拍板自动补开仓）补齐与 `apply_reconcile` 同等的写形状：
    同 cycle 原行保留各自 fill_ts；补出的行带单一 `ordId`、成交时刻 fill_ts、
    名义价值与已核回执的保护字段；写前按订单身份查重，已落账则返回
    `already_recorded` 不重写；目标 cycle 已存在时保留其 ts 与 prev_cycle 快照。
    """
    all_fills = [x for g in matched for x in g["fills"]]
    tot_sz = sum(f(x.get("fillSz"), 0.0) or 0.0 for x in all_fills)
    wavg_px = (sum((f(x.get("fillPx"), 0.0) or 0.0) * (f(x.get("fillSz"), 0.0) or 0.0)
                   for x in all_fills) / tot_sz) if tot_sz else None
    open_dt = fill_dt(max(int(x.get("fillTime") or 0) for x in all_fills))
    open_ts = open_dt.strftime(TS_FMT)
    ord_ids = sorted({g["ordId"] for g in matched})
    # cycle 归属优先用 intent 的真 cycle_id（T1）；无 intent 则落成交时刻所在槽（T2）
    cycle_id = (intent or {}).get("cycle_id") or slot_cycle_id(open_dt)
    if (not matched or not ord_ids or any(not oid or oid == "?" for oid in ord_ids)
            or not math.isfinite(tot_sz) or abs(tot_sz - float(missing_sz)) > SZ_TOL):
        raise CloseEvidenceError("apply_open_quantity_or_identity_mismatch")
    single_oid = str(ord_ids[0]) if len(ord_ids) == 1 else None
    # 写前查重（2026-09-11）：分级读的是只读快照，真写入者可能已在这之后落账
    # 同一订单；已落账就不重写，身份部分重叠一律拒绝。
    for existing in con_ro.execute(
            "SELECT cycle_id, symbol, side, action, sz, fill_px, raw FROM trades "
            "WHERE action IN ('open','add') AND symbol=?", (sym,)):
        existing_raw = str(_row_value(existing, "raw") or "")
        if not any(str(oid) in existing_raw for oid in ord_ids):
            continue
        existing_ids = _open_raw_ids(existing)
        if not existing_ids.intersection(ord_ids):
            continue
        # 与 apply_reconcile 同：身份一致还得经济量一致才算「已落账」，
        # 部分记账/价格不符是身份冲突，交人工（2026-09-11 审查）。
        same_identity = (existing_ids == set(ord_ids)
                         and norm_side(existing["side"]) == side)
        same_economics = (
            abs((f(_row_value(existing, "sz"), 0.0) or 0.0) - tot_sz) <= SZ_TOL
            and wavg_px is not None
            and math.isclose(f(_row_value(existing, "fill_px"), 0.0) or 0.0,
                             wavg_px, rel_tol=1e-6, abs_tol=1e-9))
        if not (same_identity and same_economics):
            raise CloseEvidenceError(
                "apply_open_identity_economics_mismatch" if same_identity
                else "apply_open_identity_already_claimed")
        return {"status": "already_recorded", "cycle_id": existing["cycle_id"],
                "open_ts": open_ts, "sz": tot_sz, "wavg_px": wavg_px,
                "ord_ids": ord_ids,
                "writer": {"ok": True, "already_recorded": True},
                "exp": {"skipped": "already_recorded"},
                "degradation": [], "merged_prev_trades": 0}

    prev = con_ro.execute(
        "SELECT cycle_id, ts, decision, n_orders, equity, note, raw FROM trade_cycles "
        "WHERE cycle_id=?", (cycle_id,)).fetchone()
    prev_trades = con_ro.execute(
        "SELECT ts, symbol, action, side, sz, fill_px, lev, margin, notional, score_total, "
        "reasoning, deviation, degradation, pnl, raw FROM trades WHERE cycle_id=? "
        "ORDER BY rowid", (cycle_id,)).fetchall()
    trades = [dict(t) for t in prev_trades]
    for trade in trades:
        # 与 apply_reconcile 同：原行按自己的成交时刻重写，不被 cycle ts 覆盖。
        trade["fill_ts"] = trade["ts"]
        old_trade_raw = trade.get("raw")
        if isinstance(old_trade_raw, str):
            try:
                old_trade_raw = json.loads(old_trade_raw)
            except (TypeError, ValueError):
                old_trade_raw = {}
        if not isinstance(old_trade_raw, dict):
            old_trade_raw = {}
        trade["ts_source"] = old_trade_raw.get("ts_source") or "trusted_internal_override"
    receipt_trade = (intent or {}).get("receipt_trade")
    if not isinstance(receipt_trade, dict):
        receipt_trade = None
    intent_meta = ({k: v for k, v in intent.items() if k != "receipt_trade"}
                   if isinstance(intent, dict) else intent)

    fills_evidence = [{"ts": fill_dt(x.get("fillTime")).strftime(TS_FMT),
                       "px": x.get("fillPx"), "sz": x.get("fillSz"),
                       "ordId": x.get("ordId"), "tradeId": x.get("tradeId"),
                       "execType": x.get("execType")}
                      for x in all_fills[:RAW_FILLS_CAP]]
    # 末道防线：卡不合法就降级为无卡（带着坏卡写会被 writer 整单拒绝→补账失败→交易继续冻结）。
    # 与 ledger_autoheal._card_for 的前置校验重复是有意的：本函数对任何调用方都必须安全。
    minimal_policy = thresholds.minimal_decision_contract_active(cycle_id)
    closure_policy = thresholds.minimal_contract_closure_active(cycle_id)
    if closure_policy:
        card = canonical_open_execution_package(card)
        if card is not None and validate_open_execution_package(
                card, OPEN_EXECUTION_PACKAGE_KEY, expected_side=side):
            card = None
    elif card is not None:
        try:
            if minimal_policy and not is_lightweight_open_card(card):
                card = None
            elif validate_card(card, "decision_card"):
                card = None
        except Exception:  # noqa: BLE001
            card = None

    degradation = []
    if card is None:
        degradation.append(
            "open_execution_package_missing"
            if closure_policy else "decision_card_missing")
    if intent is None:
        degradation.append("no_execution_intent")
    if sl_probe is not None and not sl_probe.get("has_sl"):
        degradation.append("naked_position_no_algo_sl")

    receipt_action = str((receipt_trade or {}).get("action") or "open").lower()
    lev_value = f((receipt_trade or {}).get("lev"), None) or lev
    ct_val = f((receipt_trade or {}).get("ct_val"), None)
    if not ct_val or ct_val <= 0:
        try:
            ct_val = trades_writer._ctval_for(sym)
        except Exception:  # noqa: BLE001 —— 规格读不到就保 NULL，与 writer 同口径
            ct_val = None
    fill_px = round(wavg_px, 8) if wavg_px else None
    notional = (round(fill_px * tot_sz * ct_val, 8)
                if fill_px and ct_val and ct_val > 0 else None)
    margin = (round(notional / float(lev_value), 8)
              if notional and lev_value and float(lev_value) > 0 else None)
    recon_trade = {
        "symbol": sym,
        "action": receipt_action if receipt_action in ("open", "add") else "open",
        "side": side,
        "sz": tot_sz,
        "fill_sz": tot_sz,
        "fill_px": fill_px,
        "fill_ts": open_ts,
        "ts_source": "fills.fillTime",
        "fill_source": "fills",
        "lev": lev_value,
        "ct_val": ct_val,
        "margin": margin,
        "notional": notional,
        "score_total": None,
        "reasoning": (
            f"ledger_autoheal 补账（UNRECORDED）：交易所有仓账本无；"
            f"fills 实证 {len(all_fills)} 笔 ordId={','.join(ord_ids)} "
            f"开仓时刻={open_ts} sz={tot_sz:g}"
            + ("；intent 归属已核" if intent else "；**无 execution_intent 归属证据**")
        ),
        "deviation": None,
        "degradation": ",".join(degradation) or None,
        "pnl": 0.0,
        "raw": {
            "reconcile_source": "exchange_fills_unrecorded",
            "open_ts": open_ts,
            "ord_ids": ord_ids,
            "fills": fills_evidence,
            "intent": intent_meta,
            "sl_probe": sl_probe,
            "receipt_trade_source": (
                "execution_intents.receipt_json" if receipt_trade else None),
            (
                "open_execution_package_source"
                if closure_policy else "decision_card_source"
            ): "analysis_signals" if card else None,
        },
    }
    if single_oid:
        # 单一订单身份写在顶层与 raw：writer 合并闸、经验去重、ledger_invariants
        # 与人工修复工具都按它对齐迟到的真回执，不再依赖 8 位小数指纹。
        recon_trade["ordId"] = single_oid
        recon_trade["raw"]["ordId"] = single_oid
    for key in ("sl_trigger_px", "tp_trigger_px", "algo_id", "tp_algo_id",
                "sl_mode", "tp_mode", "exit_mode", "sl_verified", "tp_verified"):
        if receipt_trade and receipt_trade.get(key) is not None:
            recon_trade[key] = receipt_trade[key]
    # writer 只持久化 t["raw"]：要进 trades.raw 的字段都得镜像进去——战报 SL 距离、
    # 出口 R 口径、报告间成交取证都读 trades.raw（2026-09-11 审查）。
    for key in ("fill_sz", "fill_source", "ct_val", "lev", "sl_trigger_px",
                "tp_trigger_px", "algo_id", "tp_algo_id", "sl_mode", "tp_mode",
                "exit_mode", "sl_verified", "tp_verified"):
        if recon_trade.get(key) is not None:
            recon_trade["raw"][key] = recon_trade[key]
    if card:
        recon_trade[
            OPEN_EXECUTION_PACKAGE_KEY if closure_policy else "decision_card"
        ] = card
    trades.append(recon_trade)

    prev_note = (prev["note"] if prev else "") or ""
    prev_raw_full = None
    prev_raw_obj = None
    if prev and prev["raw"]:
        try:
            parsed = json.loads(prev["raw"])
            prev_raw_full = parsed if isinstance(parsed, dict) else None
            prev_raw_obj = (
                parsed if len(prev["raw"]) < 20000
                else {
                    "_truncated": prev["raw"][:2000],
                    "_original_chars": len(prev["raw"]),
                }
            )
        except (json.JSONDecodeError, TypeError):
            prev_raw_obj = {"_unparsed": str(prev["raw"])[:2000]}
    raw_obj = {
        "reconcile_source": "exchange_fills_unrecorded",
        "reconciled_at": datetime.now(CST).strftime(TS_FMT),
        "symbol": sym, "side": side, "missing_sz": missing_sz,
        "open_ts": open_ts, "wavg_px": wavg_px, "ord_ids": ord_ids,
        "fills": fills_evidence, "intent": intent_meta, "sl_probe": sl_probe,
        "degradation": degradation,
        # 与 apply_reconcile 同：保留被合并 cycle 的原终态快照（失败证据不丢）。
        "prev_cycle": ({"decision": prev["decision"], "n_orders": prev["n_orders"],
                        "ts": prev["ts"], "note": prev_note[:1000],
                        "raw": prev_raw_obj} if prev else None),
    }
    raw_obj.update(_report_business_context(prev_raw_full, cycle_id))
    raw_obj["decision"] = "traded"
    raw_obj["n_orders"] = len(trades)
    if raw_obj.get("business_terminal_preserved_after_reconcile") is True:
        raw_obj["reconciled_close_preserved"] = True
        raw_obj["merge_guard_kept_rows"] = len(prev_trades)

    data = {
        "cycle_id": cycle_id,
        # 已存在的 cycle（含零成交 HOLD）保留其完成时刻；补出的行自带 fill_ts。
        "ts": (prev["ts"] if prev else open_ts),
        "decision": "traded",
        "action": (f"autoheal-unrecorded: {sym} {side} open {tot_sz:g} "
                   f"@ {wavg_px:.6g}" if wavg_px else
                   f"autoheal-unrecorded: {sym} {side} open {tot_sz:g}"),
        "note": (f"ledger_autoheal 补 UNRECORDED open sz={tot_sz:g}"
                 + (f" | 原行 note: {prev_note[:300]}" if prev_note else "")),
        "n_orders": len(trades),
        "equity": (prev["equity"] if prev else None),
        "trades": trades,
        "raw": raw_obj,
        "_profile": profile,
    }
    if minimal_policy:
        # 补账只回收已在交易所发生的真成交。当前周期顶层不得因
        # analysis_signals 中的 OPEN 机器包而倒退成 decision_card_v1。
        data["decision_protocol"] = MINIMAL_DECISION_PROTOCOL
    elif card:
        data["decision_protocol"] = "decision_card_v1"
        data["decision_card"] = card

    result = trades_writer.maintenance_write_trades(
        data, Path(db_path), trusted_timestamp=data["ts"], preserve_equity_none=True)
    if not result.get("ok") or result.get("refused"):
        raise RuntimeError(f"trades_writer 拒绝补 open: {result}")
    exp = trades_writer.write_experiences(
        {"cycle_id": cycle_id, "trades": [recon_trade]}, profile, open_ts)
    return {"status": "applied", "cycle_id": cycle_id, "open_ts": open_ts,
            "sz": tot_sz, "wavg_px": wavg_px, "ord_ids": ord_ids,
            "writer": result, "exp": exp, "degradation": degradation,
            "merged_prev_trades": len(prev_trades)}


def classify(profile, by_key, nets, ven):
    """账本轧差 ↔ OKX 现仓差异分级（唯一定义源）。

    只做判定：不打印、不写库。内部会为幽灵组回读 fills（只读 API）。
    调用方一律 import 本函数，**禁止各处自写分级规则**——CLI 与
    `ledger_autoheal.py` 共用同一套 EXACT/FUZZY 口径，避免两边漂移。

    返回 dict:
      ghosts      [((sym, side), ghost_sz), ...]        账本 > 现仓
      over_closed [((sym, side), net), ...]             账本净持仓为负
      unrecorded  [((sym, side), venue_sz), ...]        现仓 > 账本
      exact       [((sym, side), ghost_sz, matched, detail), ...]  可补
      fuzzy       [((sym, side), ghost_sz, reason, detail), ...]   只报告
    """
    ghosts, over_closed = [], []
    for k, net in nets.items():
        if net < -SZ_TOL:
            over_closed.append((k, net))
            continue
        ven_sz = ven.get(k, 0.0)
        if net > ven_sz + SZ_TOL:
            ghosts.append((k, net - ven_sz))
    unrecorded = [(k, sz) for k, sz in ven.items()
                  if sz > nets.get(k, 0.0) + SZ_TOL]

    exact, fuzzy, deferred, leftover_orders = [], [], [], []
    close_budget = _CloseBudget()
    for (sym, side), ghost_sz in ghosts:
        rows = _close_identity_rows(by_key, (sym, side))
        rows, epoch_notes = _active_close_epoch(rows)
        opens = [parse_ts(r["ts"]) for r in rows
                 if (r["action"] or "").lower() in ("open", "add")]
        opens = [d for d in opens if d is not None]
        if not opens:
            fuzzy.append(((sym, side), ghost_sz, "账本无可解析的 open 行 ts", []))
            continue
        t0_dt = min(opens) - timedelta(minutes=OPEN_TS_BUFFER_MIN)
        t0_ms = int(t0_dt.timestamp() * 1000)
        anchor_token = _CLOSE_EPOCH_ANCHOR.set(_epoch_open_anchor(by_key, rows))
        token = _CLOSE_BUDGET.set(close_budget)
        try:
            fills = fetch_reduce_fills(profile, sym, side, t0_ms)
            groups, remaining, notes = _prepare_close_groups(profile, sym, side, fills, rows, t0_dt)
        except Exception as e:  # noqa: BLE001
            fuzzy.append(((sym, side), ghost_sz, f"平仓证据未能证实: {e}", []))
            # Typed scheduling metadata only: the unproved group stays FUZZY.
            # Callers may drain other independently exact closes, never this
            # group, without increasing either evidence budget.
            if isinstance(e, CloseEvidenceError) and str(e) in CLOSE_DEFERRED_REASONS:
                deferred.append(((sym, side), ghost_sz, str(e)))
            continue
        finally:
            _CLOSE_BUDGET.reset(token)
            _CLOSE_EPOCH_ANCHOR.reset(anchor_token)
        rem_sz = sum(g["sz"] for g in remaining)
        detail = [f"窗口起点 {t0_dt.strftime(TS_FMT)}，平仓腿 fills {len(fills)} 笔 / "
                  f"{len(groups)} 组，销账后剩 {len(remaining)} 组合计 {rem_sz:g} 张"]
        detail += epoch_notes + notes
        for g in remaining:
            detail.append(f"  剩余组 ordId={g['ordId']} sz={g['sz']:g} "
                          f"px≈{g['wavg_px']:.6g} pnl={g['pnl']:+.6g} "
                          f"t={fill_dt(g['t_last_ms']).strftime(TS_FMT)}")
        hit, leftover, reason = match_exact_groups(remaining, ghost_sz)
        if hit is None:
            fuzzy.append(((sym, side), ghost_sz, reason, detail))
            continue
        if leftover:
            leftover_orders.append(((sym, side), [g["ordId"] for g in leftover]))
            detail.append(f"  规则b命中：唯一 ordId={hit[0]['ordId']} 组 "
                          f"sz={hit[0]['sz']:g} == 幽灵 sz；其余 {len(leftover)} 组"
                          f"为独立未记账成交（只报告不写）")
            for g in leftover:
                detail.append(f"  [LEFTOVER] ordId={g['ordId']} sz={g['sz']:g} "
                              f"px≈{g['wavg_px']:.6g} pnl={g['pnl']:+.6g} "
                              f"t={fill_dt(g['t_last_ms']).strftime(TS_FMT)} "
                              f"—— 疑似未记账小额往返（净额自平），人工核")
        exact.append(((sym, side), ghost_sz, hit, detail))

    return {"ghosts": ghosts, "over_closed": over_closed,
            "unrecorded": unrecorded, "exact": exact, "fuzzy": fuzzy,
            "deferred": deferred, "leftover_orders": leftover_orders}


def main():
    ap = argparse.ArgumentParser(description="交易所侧/执行后平仓漏落账对账")
    ap.add_argument("--profile", choices=["live"], required=True)
    ap.add_argument("--db-root", default=_public_project_path('db'))
    ap.add_argument("--apply", action="store_true",
                    help="对精确匹配幽灵经 trades_writer 补 close 行（默认 dry-run 只报告）")
    ap.add_argument("--ordid",
                    help="仅 apply 含该 ordId 的唯一 GHOST-EXACT；live --apply 必填")
    args = ap.parse_args()
    if args.apply and args.profile == "live" and not args.ordid:
        print("[reconcile][ERROR] live --apply 必须同时给 --ordid；"
              "禁止宽口径一次补全部精确项")
        return 2

    db_root = Path(args.db_root)
    db_path = db_root / f"{args.profile}_trades.db"
    if not db_path.exists():
        print(f"[reconcile][ERROR] 账本不存在: {db_path}")
        return 2
    # 经验库/equity 兜底同步指向本 db-root（测试副本时不碰真 account.db）
    os.environ["OKX_ACCOUNT_DB"] = str(db_root / "account.db")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"== 交易所侧平仓落账对账 profile={args.profile} @ "
          f"{datetime.now(CST).strftime(TS_FMT)} ({mode}) ==")

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    by_key = ledger_rows(con)
    nets = {k: net_of(rows) for k, rows in by_key.items()}
    nets = {k: v for k, v in nets.items() if abs(v) > SZ_TOL}

    try:
        ven = venue_positions(args.profile)
    except Exception as e:  # noqa: BLE001
        print(f"[reconcile][ERROR] OKX 现仓 API 失败: {e}")
        con.close()
        return 2

    print(f"账本轧差净持仓 {len(nets)} 组: "
          + ("; ".join(f"{k[0]}/{k[1]}={v:g}" for k, v in nets.items()) or "空"))
    print(f"OKX 现仓 {len(ven)} 组: "
          + ("; ".join(f"{k[0]}/{k[1]}={v:g}" for k, v in ven.items()) or "空"))

    verdict = classify(args.profile, by_key, nets, ven)
    ghosts = verdict["ghosts"]
    over_closed = verdict["over_closed"]
    unrecorded = verdict["unrecorded"]
    exact, fuzzy = verdict["exact"], verdict["fuzzy"]

    if over_closed:
        print(f"\n[OVER_CLOSED] {len(over_closed)} 组（账本净持仓为负=close 多于 open，"
              f"缺 open 行；只报告，非本脚本可补）:")
        for (sym, side), net in over_closed:
            print(f"  {sym} {side} net={net:g}")
    if unrecorded:
        print(f"\n[UNRECORDED] {len(unrecorded)} 组（交易所有仓账本无/账本少记；"
              f"只报告，人工核 orders-history 后经 writer 补 open）:")
        for (sym, side), sz in unrecorded:
            print(f"  {sym} {side} venue={sz:g} ledger={nets.get((sym, side), 0.0):g}")

    if not ghosts:
        if args.apply and args.ordid:
            try:
                metadata_result = repair_existing_from_journal(
                    db_path, args.profile, args.ordid, con
                )
            except Exception as exc:  # noqa: BLE001
                print(f"\n[reconcile][ERROR] journal 元数据校正失败: {exc}")
                con.close()
                return 2
            if metadata_result is None:
                print(
                    f"\n[reconcile][ERROR] ordId={args.ordid} 未命中已落账 close；"
                    "拒绝把无幽灵仓误报为修复成功"
                )
                con.close()
                return 2
            print(
                "\n[JOURNAL-METADATA] "
                f"ordId={args.ordid} status={metadata_result['status']} "
                f"cycle={metadata_result['cycle_id']} "
                f"journal_line={metadata_result.get('journal_line', '-')}"
            )
        print("\n结论: 无幽灵仓（账本 ≤ 现仓）✓")
        con.close()
        return 0

    for (sym, side), ghost_sz, matched, detail in exact:
        close_dt = fill_dt(max(g["t_last_ms"] for g in matched))
        print(f"\n[GHOST-EXACT] {sym} {side} sz={ghost_sz:g} → 精确匹配，"
              f"平仓时刻={close_dt.strftime(TS_FMT)} cycle={slot_cycle_id(close_dt)}"
              + ("（--apply 可补账）" if not args.apply else "（补账中…）"))
        for line in detail:
            print(f"  {line}")
    for (sym, side), ghost_sz, reason, detail in fuzzy:
        print(f"\n[GHOST-FUZZY] {sym} {side} sz={ghost_sz:g} → 模糊，只报告不写：{reason}")
        for line in detail:
            print(f"  {line}")

    apply_exact = exact
    if args.ordid:
        apply_exact = [
            row for row in exact
            if str(args.ordid) in {str(g.get("ordId")) for g in row[2]}
        ]
        if len(apply_exact) != 1:
            con.close()
            print(f"\n结论: --ordid={args.ordid} 必须唯一命中 1 个 "
                  f"GHOST-EXACT，实际={len(apply_exact)}（exit 2）")
            return 2

    rc_apply_err = False
    if args.apply and exact:
        print(f"\n== APPLY：补账 {len(apply_exact)} 项（经 trades_writer.write_trades）==")
        for (sym, side), ghost_sz, matched, _ in apply_exact:
            open_lev = None
            for r in reversed(by_key[(sym, side)]):
                if (r["action"] or "").lower() in ("open", "add") and r["lev"]:
                    open_lev = r["lev"]
                    break
            try:
                res = apply_reconcile(db_path, args.profile, sym, side,
                                      ghost_sz, matched, con, open_lev=open_lev)
                print(f"  [APPLIED] {sym} {side} close sz={res['sz']:g} "
                      f"pnl={res['pnl']:+g} px≈{res['wavg_px']:.6g} "
                      f"cycle={res['cycle_id']} close_ts={res['close_ts']} "
                      f"ordId={','.join(res['ord_ids'])} "
                      f"writer={res['writer']} exp={res['exp']} "
                      f"merged_prev_trades={res['merged_prev_trades']}")
            except Exception as e:  # noqa: BLE001
                rc_apply_err = True
                print(f"  [APPLY-ERROR] {sym} {side}: {e}")

    con.close()
    if rc_apply_err:
        print("\n结论: 补账存在失败项（exit 2）")
        return 2
    if fuzzy:
        print("\n结论: 存在模糊幽灵，需人工核（exit 3）")
        return 3
    if exact and not args.apply:
        print("\n结论: 有精确可补幽灵（exit 1，加 --apply 补账）")
        return 1
    if args.apply and len(apply_exact) < len(exact):
        print(f"\n结论: 指定 ordId 已补，但仍有 "
              f"{len(exact) - len(apply_exact)} 个其他 GHOST-EXACT 未处理（exit 1）")
        return 1
    print("\n结论: 精确幽灵已全部补账 ✓（复跑 dry 验证轧差与现仓一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
