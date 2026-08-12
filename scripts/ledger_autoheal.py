# -*- coding: utf-8 -*-
r"""账本自愈（GHOST-EXACT 与受控 UNRECORDED 补账，2026-08-04）。

背景：`order_executor` 开仓时附挂的 algo 止损在交易所成交后，成交发生在任何 agent
轮次之外，系统没有回写入口 → 账本仍认为持仓在、OKX 已平 → `pretrade_ledger_position_mismatch`
把**后续所有 live 开仓**挡死（2026-08-04 实例：SKHY-USDT-SWAP 幽灵仓冻结 live 9h20m）。

本脚本把「检测 → 修复」内置进交易环节，取代人工逐笔补账。

**不重复实现分级规则**：EXACT/FUZZY 判定一律 import
`reconcile_exchange_closes.classify`（唯一定义源），写库一律经该模块的
`apply_reconcile` → `collectors/trades_writer`。本脚本只负责**闸门与编排**。

公开版范围：
  - [GHOST-EXACT] 账本 > 现仓且 fills 精确解释差额 → 只生成诊断计划
  - [GHOST-FUZZY] fills 对不上 → 只报告，转人工 ❌
  - [UNRECORDED] 只有 intent/ordId/fills 归属精确且完整保护性 SL 已确认的
    T1，也只生成诊断计划，不补 open。
    T2（无 intent）及 SL 缺失/未知均为 P0，写前阻断。
  - [OVER_CLOSED] 账本净持仓为负 → 只报告，转人工 ❌（P3 另行设计）

核心硬闸：
  1. 只补 EXACT；FUZZY、T2/T3 UNRECORDED、OVER_CLOSED 一律不写。
  2. close/open 分别需要独立正向授权；任何 P0 在本轮写库前阻断。
  3. 单轮自愈上限 `--max-heals`（默认 3）；超限则**一笔都不补**并升级告警——
     那意味着系统性问题而非单笔漏账。
  4. runner 执行期互斥：同 profile 有 running runner 时跳过（`--self-cycle`
     放行调用方自身那一条，因为插入点 A 就跑在该 runner 会话内）。
  5. 幂等：复用 `consume_recorded` 先销账已记录的平仓腿，重复跑不重复补。
  6. 全留痕：结构化 JSON + 自愈成功后关闭对应 repair_queue 条目，绝不静默改账本。

退出码：0=干净或安全写入完成；1=未解决/需人工；2=错误；
        3=runner 互斥跳过；4=P0。任何非 0 结果均 `blocking=true`。

用法：
  pwsh -NoProfile -File ./scripts/run_okx_python.ps1 ^
      ./scripts/ledger_autoheal.py --profile live [--apply]
      [--enable-unrecorded] [--max-heals 3]
      [--self-cycle 2026-08-04T13:00] [--request-id <uuid>] [--json-out <path>]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"./scripts")
sys.path.insert(0, r"./collectors")

sys.path.insert(0, r"./core/lib")

import reconcile_exchange_closes as rec  # noqa: E402
import repair_queue_tool  # noqa: E402
from live_reconcile_monitor import active_runner  # noqa: E402

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_MAX_HEALS = 3
CONTRACT_VERSION = 1
RC_OK = 0
RC_NEEDS_HUMAN = 1
RC_ERROR = 2
RC_SKIPPED = 3
RC_P0 = 4
# UNRECORDED 无 intent 时的 fills 回看窗（天）——只用于定位开仓腿，不放宽判定
UNRECORDED_LOOKBACK_DAYS = 7
# execution_intents 终态；非终态 + 有 ord_id = 「单已提交、落库没跟上」的归属证据
INTENT_TERMINAL = ("completed", "failed_clean")


def _new_result(profile: str, db_root: Path, self_cycle: str | None,
                request_id: str | None) -> dict:
    """Create the v1 machine contract before any fallible business work."""
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": str(request_id or uuid.uuid4()),
        "profile": profile,
        "cycle": str(self_cycle) if self_cycle is not None else None,
        "db_root": str(Path(db_root).resolve()),
        "ts": _now(),
        "status": "ok",
        "applied": False,
        "p0": False,
        "blocking": False,
        "findings": [],
        "apply": False,
        "skipped": None,
        "healed": [],
        "needs_human": [],
        "queue_closed": [],
        "rc": RC_OK,
    }


def _finalize_result(out: dict) -> dict:
    """Finalize all legacy fields into one authoritative v1 contract.

    ``needs_human`` and ``healed`` remain for existing operators.  Callers must
    consume only the validated top-level contract.  A planned/dry-run repair is
    unresolved and therefore rc=1/blocking; a P0 dominates every other status.
    """
    findings = [dict(item) for item in out.get("needs_human", [])
                if isinstance(item, dict)]
    for item in out.get("healed", []):
        if not isinstance(item, dict) or item.get("applied") is True:
            continue
        findings.append({
            "kind": str(item.get("kind") or "GHOST-EXACT"),
            "tier": item.get("tier"),
            "symbol": item.get("symbol"),
            "side": item.get("side"),
            "sev": "P1",
            "reason": str(item.get("note") or item.get("error")
                          or "repair not applied"),
        })
    if out.get("error") and not any(
            str(item.get("reason") or "") == str(out["error"])
            for item in findings):
        findings.append({"kind": "AUTOHEAL-ERROR", "sev": "P1",
                         "reason": str(out["error"])})
    if out.get("skipped") and not findings:
        findings.append({"kind": "AUTOHEAL-SKIPPED", "sev": "P1",
                         "reason": str(out["skipped"])})

    applied = any(isinstance(item, dict) and item.get("applied") is True
                  for item in out.get("healed", []))
    p0 = any(str(item.get("sev") or "").upper() == "P0"
             for item in findings)
    prior_rc = int(out.get("rc") or RC_OK)
    if p0:
        rc, status = RC_P0, "p0_blocked"
    elif prior_rc == RC_ERROR or out.get("error"):
        rc, status = RC_ERROR, "error"
    elif prior_rc == RC_SKIPPED or out.get("skipped"):
        rc, status = RC_SKIPPED, "skipped"
    elif findings:
        rc, status = RC_NEEDS_HUMAN, "needs_human"
    elif applied:
        rc, status = RC_OK, "applied"
    else:
        rc, status = RC_OK, "ok"
    out.update({
        "status": status,
        "applied": applied,
        "p0": p0,
        "blocking": rc != RC_OK,
        "findings": findings,
        "rc": rc,
    })
    return out


def _intent_for(db_root: Path, profile: str, sym: str, side: str) -> dict | None:
    """找该 sym/side 的开仓意图归属证据（T1 判据）。

    非终态 + 有 ord_id ⇒ 单确实提交到交易所了、只是账没落上。
    全历史 95 条实测是干净二元分布（completed 全有单号 / failed_clean 全无），
    所以非终态带单号本身就是异常信号，正是我们要抓的那种。
    """
    led = db_root / "ledger.db"
    if not led.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{led.as_posix()}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT cycle_id, symbol, action, side, state, reserved_at, "
            "       submitted_at, ord_id, error "
            "FROM execution_intents "
            "WHERE profile=? AND symbol=? AND side=? AND action IN ('open','add') "
            f"  AND state NOT IN ({','.join('?' * len(INTENT_TERMINAL))}) "
            "  AND ord_id IS NOT NULL AND ord_id <> '' "
            "ORDER BY reserved_at DESC LIMIT 1",
            (profile, sym, side, *INTENT_TERMINAL),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _card_for(db_root: Path, cycle_id: str, sym: str) -> dict | None:
    """从 analysis.db 取该 cycle/symbol 的**真**决策卡。取不到返回 None——绝不编造。"""
    ana = db_root / "analysis.db"
    if not ana.exists() or not cycle_id:
        return None
    try:
        con = sqlite3.connect(f"file:{ana.as_posix()}?mode=ro", uri=True, timeout=10)
        row = con.execute(
            "SELECT decision_card FROM analysis_signals WHERE cycle_id=? AND symbol=?",
            (cycle_id, sym)).fetchone()
        con.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        card = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(card, dict):
        return None
    # 卡必须先过 validate_card：库里存的卡若不完整，带着它写会被 writer 整单拒绝，
    # 导致补账失败、交易继续冻结。降级为「无卡 + degradation 标注」比拒修更好。
    try:
        from core.decision_card import validate_card  # noqa: PLC0415

        if validate_card(card, "decision_card"):
            return None
    except Exception:  # noqa: BLE001  校验器不可用时保守当作无卡
        return None
    return card


def _probe_sl(profile: str, sym: str, side: str, expected_sz: float) -> dict:
    """Strictly verify an active protective stop for this exact position.

    Same-symbol opposite-side orders, fired/paused orders, non-reduce-only
    orders, invalid triggers, and undersized protection never count.  This is
    read-only and never places or amends an order.
    """
    try:
        from _okxorder import get_algo_orders  # noqa: PLC0415  仅此分支需要

        rows = get_algo_orders(sym, profile) or []
        close_side = "sell" if side == "long" else "buy"
        valid: list[dict] = []
        rejected = 0
        for row in rows:
            if not isinstance(row, dict):
                rejected += 1
                continue
            try:
                trigger_px = float(row.get("slTriggerPx"))
                row_sz = float(row.get("sz"))
            except (TypeError, ValueError):
                rejected += 1
                continue
            reduce_only = str(row.get("reduceOnly") or "").lower()
            checks = (
                str(row.get("instId") or "").upper() == sym.upper(),
                bool(str(row.get("algoId") or "").strip()),
                str(row.get("state") or "").lower() == "live",
                str(row.get("posSide") or "").lower() == side,
                str(row.get("side") or "").lower() == close_side,
                reduce_only in ("true", "1"),
                math.isfinite(trigger_px) and trigger_px > 0,
                math.isfinite(row_sz)
                and row_sz + rec.SZ_TOL >= float(expected_sz),
            )
            if not all(checks):
                rejected += 1
                continue
            valid.append(row)
        return {
            "has_sl": bool(valid),
            "n_pending": len(valid),
            "rejected_candidates": rejected,
            "algo_ids": [row.get("algoId") for row in valid[:5]],
        }
    except Exception as e:  # noqa: BLE001  探测失败不得阻断补账，但要如实标注未知
        return {"has_sl": None, "error": str(e)[:160]}


def _now() -> str:
    return datetime.now(CST).strftime(TS_FMT)


def _pending_queue_ids(account_db: Path, profile: str, sym: str, side: str) -> list[int]:
    """找出因该 sym/side 不一致而开、且仍 pending 的 repair_queue 条目。"""
    if not account_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{account_db.as_posix()}?mode=ro",
                              uri=True, timeout=10)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT id FROM repair_queue WHERE status='pending' AND ("
            "  (check_name='order_executor'"
            "   AND issue LIKE '%pretrade_ledger_position_mismatch%'"
            "   AND issue LIKE ?)"
            "  OR check_name = ?"
            ")",
            (f"%{sym}/{side}%",
             f"ledger_invariant:experience_position:{profile}:{sym}:{side}"),
        ).fetchall()
        return [int(r["id"]) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _close_pending_queue(account_db: Path, qids: list[int], resolution: str,
                         out: dict) -> None:
    """Close queue rows in the supplied db-root without polluting JSON stdout."""
    if not qids:
        return
    try:
        rcq = repair_queue_tool.do_close(
            qids, False, resolution, True,
            closed_by="ledger_autoheal", db_path=account_db, quiet=True,
        )
    except Exception as exc:  # noqa: BLE001
        rcq = RC_ERROR
        error = f"{type(exc).__name__}: {exc}"
    else:
        error = f"repair_queue_tool rc={rcq}" if rcq != RC_OK else None
    if rcq == RC_OK:
        out["queue_closed"].extend(qids)
        return
    out["needs_human"].append({
        "kind": "QUEUE-CLOSE-ERROR",
        "sev": "P1",
        "queue_ids": qids,
        "account_db": str(account_db.resolve()),
        "reason": error,
    })
    out["rc"] = RC_ERROR


def _plan_unrecorded(profile: str, db_root: Path, by_key, nets,
                      sym: str, side: str, venue_sz: float, enabled: bool) -> dict:
    """UNRECORDED 三级证据链定级（P2·2026-08-04）。

    T1 = intent 归属证据齐 + 开仓腿 fills 精确解释缺口 → 自动补，元数据全真
    T2 = 无 intent，但 fills 精确解释缺口 → 只报告 + **P0**
    T3 = fills 对不上 / API 失败 → 只报告转人工

    ``enabled`` 只控制最终写入，不关闭只读证据检查。
    """
    ledger_sz = nets.get((sym, side), 0.0)
    missing = venue_sz - ledger_sz
    base = {"kind": "UNRECORDED", "symbol": sym, "side": side,
            "venue_sz": venue_sz, "ledger_sz": ledger_sz,
            "missing_sz": round(missing, 8), "write_enabled": bool(enabled)}

    intent = _intent_for(db_root, profile, sym, side)
    t0 = rec.parse_ts(intent["reserved_at"]) if (intent and intent.get("reserved_at")) else None
    if t0 is None:
        t0 = datetime.now(CST) - timedelta(days=UNRECORDED_LOOKBACK_DAYS)
    t0 = t0 - timedelta(minutes=rec.OPEN_TS_BUFFER_MIN)
    try:
        fills = rec.fetch_open_fills(profile, sym, side, int(t0.timestamp() * 1000))
    except Exception as e:  # noqa: BLE001
        return {**base, "tier": "T3", "reason": f"开仓腿 fills API 失败: {e}"}

    groups = rec.group_by_ord(fills)
    rows = by_key.get((sym, side), [])
    remaining, notes = rec.consume_recorded(groups, rows, t0, rec.OPEN_ACTIONS)
    matched, leftover, reason = rec.match_exact_groups(remaining, missing)
    if matched is None:
        return {**base, "tier": "T3", "reason": reason, "notes": notes[:5]}

    if intent is None:
        return {
            **base,
            "tier": "T2",
            "sev": "P0",
            "reason": (
                "开仓 fills 虽精确解释缺口，但缺少 execution_intent "
                "归属证据；禁止自动补 open，转人工逐单核对"
            ),
            "notes": notes[:5],
            "ord_ids": sorted({str(g.get("ordId")) for g in matched}),
        }

    # intent ord_id 必须出现在匹配集里，否则归属存疑。
    if str(intent.get("ord_id")) not in {str(g.get("ordId")) for g in matched}:
        return {**base, "tier": "T3",
                "reason": f"intent ord_id={intent.get('ord_id')} 未出现在匹配 fills 组，归属存疑"}

    cycle_id = (intent or {}).get("cycle_id") or rec.slot_cycle_id(
        rec.fill_dt(max(int(x.get("fillTime") or 0) for g in matched for x in g["fills"])))
    return {**base,
            "tier": "T1",
            "cycle_id": cycle_id,
            "intent": intent,
            "card": _card_for(db_root, cycle_id, sym),
            "matched": matched,
            "leftover_groups": len(leftover),
            "ord_ids": sorted({str(g.get("ordId")) for g in matched})}


def autoheal(profile: str, db_root: Path, apply: bool,
             max_heals: int, self_cycle: str | None,
             enable_unrecorded: bool = False,
             request_id: str | None = None) -> dict:
    db_root = Path(db_root).resolve()
    out = _new_result(profile, db_root, self_cycle, request_id)
    out["apply"] = bool(apply)
    out["unrecorded_write_enabled"] = bool(enable_unrecorded)

    # Public-release boundary: this helper is permanently report-only.  Keep
    # the legacy flags parseable so old operators receive a structured,
    # fail-closed result instead of accidentally invoking a different tool.
    if apply or enable_unrecorded:
        out["error"] = (
            "public release ledger_autoheal is permanently read-only; "
            "--apply and --enable-unrecorded are disabled"
        )
        out["rc"] = RC_ERROR
        return _finalize_result(out)

    # --- 闸 4：runner 执行期互斥 ---
    try:
        active = active_runner(profile)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"runner 互斥探测失败: {type(exc).__name__}: {exc}"
        out["rc"] = RC_ERROR
        return _finalize_result(out)
    if active and str(active.get("cycle_id") or "") != str(self_cycle or ""):
        out["skipped"] = f"{profile}_runner_active"
        out["active_runner"] = active
        out["rc"] = RC_SKIPPED
        return _finalize_result(out)

    db_path = db_root / f"{profile}_trades.db"
    if not db_path.exists():
        out["error"] = f"账本不存在: {db_path}"
        out["rc"] = RC_ERROR
        return _finalize_result(out)
    # 经验库/equity 兜底同步指向本 db-root（测试副本时不碰真 account.db）
    account_db = db_root / "account.db"
    previous_account_db = os.environ.get("OKX_ACCOUNT_DB")
    os.environ["OKX_ACCOUNT_DB"] = str(account_db)

    try:
        con = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error as exc:
        if previous_account_db is None:
            os.environ.pop("OKX_ACCOUNT_DB", None)
        else:
            os.environ["OKX_ACCOUNT_DB"] = previous_account_db
        out["error"] = f"账本不可读: {type(exc).__name__}: {exc}"
        out["rc"] = RC_ERROR
        return _finalize_result(out)
    con.row_factory = sqlite3.Row
    try:
        by_key = rec.ledger_rows(con)
        nets = {k: rec.net_of(rows) for k, rows in by_key.items()}
        nets = {k: v for k, v in nets.items() if abs(v) > rec.SZ_TOL}
        try:
            ven = rec.venue_positions(profile)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"OKX 现仓 API 失败: {e}"
            out["rc"] = RC_ERROR
            return _finalize_result(out)

        verdict = rec.classify(profile, by_key, nets, ven)
        out["ledger_net"] = {f"{k[0]}/{k[1]}": v for k, v in nets.items()}
        out["venue"] = {f"{k[0]}/{k[1]}": v for k, v in ven.items()}

        # --- 闸 1/2：非 EXACT 一律转人工，绝不自动写 ---
        for (sym, side), ghost_sz, reason, _ in verdict["fuzzy"]:
            out["needs_human"].append({
                "kind": "GHOST-FUZZY", "symbol": sym, "side": side,
                "sz": ghost_sz, "reason": reason})
        unrecorded_todo = []
        for (sym, side), sz in verdict["unrecorded"]:
            item = _plan_unrecorded(profile, db_root, by_key, nets,
                                    sym, side, sz, enable_unrecorded)
            # 保护性 SL 是现仓安全事实，与写权限无关。默认只读也必须
            # 探测；缺失或未知在任何写入前升级 P0。
            sl = _probe_sl(profile, sym, side, item["venue_sz"])
            item["sl_probe"] = sl
            if sl.get("has_sl") is not True:
                out["needs_human"].append({
                    "kind": "NAKED-POSITION-P0", "symbol": sym, "side": side,
                    "sev": "P0", "sl_probe": sl,
                    "reason": "交易所现仓缺少或无法确认同侧、reduceOnly、"
                              "足量且 state=live 的保护性止损；"
                              "本轮不补账、不关工单、不下单",
                })
            if item["tier"] in ("T2", "T3"):
                out["needs_human"].append(item)
            else:
                unrecorded_todo.append(item)
        for (sym, side), net in verdict["over_closed"]:
            out["needs_human"].append({
                "kind": "OVER_CLOSED", "symbol": sym, "side": side,
                "net": net, "reason": "账本净持仓为负，缺 open 行；P1 不自动处理"})

        exact = verdict["exact"]
        out["exact_count"] = len(exact)
        out["unrecorded_count"] = len(unrecorded_todo)
        total_heals = len(exact) + len(unrecorded_todo)

        # 任何 P0 都在本轮第一笔写入前阻断；禁止“边补账边报 P0”。
        if any(str(item.get("sev") or "").upper() == "P0"
               for item in out["needs_human"] if isinstance(item, dict)):
            return _finalize_result(out)

        if not total_heals:
            return _finalize_result(out)

        # --- 闸 3：单轮上限（幽灵 + UNRECORDED 合并计），超限一笔都不补 ---
        if total_heals > max_heals:
            out["error"] = (f"待自愈 {total_heals} 组（幽灵 {len(exact)} + "
                            f"UNRECORDED {len(unrecorded_todo)}）> 上限 {max_heals}，"
                            f"判为系统性异常，本轮不自愈（升级人工）")
            out["needs_human"].append({
                "kind": "OVER_CAP", "count": total_heals, "cap": max_heals,
                "reason": out["error"]})
            out["rc"] = RC_ERROR
            return _finalize_result(out)

        for (sym, side), ghost_sz, matched, _ in exact:
            ord_ids = sorted({str(g.get("ordId")) for g in matched})
            item = {"kind": "GHOST-EXACT", "symbol": sym, "side": side,
                    "sz": ghost_sz,
                    "ord_ids": ord_ids, "applied": False}
            if not apply:
                item["note"] = "dry-run，未写库"
                out["healed"].append(item)
                continue
            open_lev = None
            for r in reversed(by_key[(sym, side)]):
                if (r["action"] or "").lower() in ("open", "add") and r["lev"]:
                    open_lev = r["lev"]
                    break
            try:
                res = rec.apply_reconcile(db_path, profile, sym, side,
                                          ghost_sz, matched, con,
                                          open_lev=open_lev)
            except Exception as e:  # noqa: BLE001
                item["error"] = str(e)
                out["needs_human"].append({
                    "kind": "APPLY-ERROR", "symbol": sym, "side": side,
                    "reason": str(e)})
                out["rc"] = RC_ERROR
                out["healed"].append(item)
                continue
            item.update({"applied": True, "pnl": res["pnl"],
                         "fill_px": res["wavg_px"], "cycle_id": res["cycle_id"],
                         "close_ts": res["close_ts"]})
            out["healed"].append(item)

            # --- 闸 6：留痕，关闭同因 pending 工单 ---
            qids = _pending_queue_ids(account_db, profile, sym, side)
            _close_pending_queue(
                account_db, qids,
                f"ledger_autoheal 自愈 {sym}/{side} sz={ghost_sz:g} "
                f"ordId={','.join(ord_ids)} close_ts={res['close_ts']}；"
                f"底层账实不一致已消除",
                out,
            )

        # --- P2：UNRECORDED 补 open（仅 T1）---
        for plan in unrecorded_todo:
            sym, side = plan["symbol"], plan["side"]
            item = {"kind": "UNRECORDED", "tier": plan["tier"], "symbol": sym,
                    "side": side, "sz": plan["missing_sz"],
                    "ord_ids": plan["ord_ids"], "cycle_id": plan.get("cycle_id"),
                    "has_real_card": bool(plan.get("card")), "applied": False,
                    "write_enabled": bool(plan.get("write_enabled")),
                    "sl_probe": plan.get("sl_probe")}
            sl = plan.get("sl_probe") or {"has_sl": None, "error": "probe missing"}
            if sl.get("has_sl") is not True:
                item["note"] = "blocked_before_write: protective SL not confirmed"
                out["healed"].append(item)
                continue
            if not apply:
                item["note"] = "dry-run，未写库"
                out["healed"].append(item)
                continue
            if not plan.get("write_enabled"):
                item["note"] = "report-only: --enable-unrecorded 未开启，未写库"
                out["healed"].append(item)
                continue
            lev = None
            for r in reversed(by_key.get((sym, side), [])):
                if (r["action"] or "").lower() in ("open", "add") and r["lev"]:
                    lev = r["lev"]
                    break
            try:
                res = rec.apply_unrecorded(
                    db_path, profile, sym, side, plan["missing_sz"],
                    plan["matched"], con, lev=lev, card=plan.get("card"),
                    intent=plan.get("intent"), sl_probe=sl)
            except Exception as e:  # noqa: BLE001
                item["error"] = str(e)
                out["needs_human"].append({
                    "kind": "APPLY-ERROR-UNRECORDED", "symbol": sym,
                    "side": side, "reason": str(e)})
                out["rc"] = RC_ERROR
                out["healed"].append(item)
                continue
            item.update({"applied": True, "fill_px": res["wavg_px"],
                         "cycle_id": res["cycle_id"], "open_ts": res["open_ts"],
                         "degradation": res["degradation"]})
            out["healed"].append(item)
            qids = _pending_queue_ids(account_db, profile, sym, side)
            _close_pending_queue(
                account_db, qids,
                f"ledger_autoheal 补 UNRECORDED open {sym}/{side} "
                f"sz={plan['missing_sz']:g} tier={plan['tier']} "
                f"ordId={','.join(plan['ord_ids'])}；账实已一致",
                out,
            )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["rc"] = RC_ERROR
    finally:
        con.close()
        if previous_account_db is None:
            os.environ.pop("OKX_ACCOUNT_DB", None)
        else:
            os.environ["OKX_ACCOUNT_DB"] = previous_account_db

    return _finalize_result(out)


def _write_json_atomic(path: Path, text: str) -> None:
    """Write the machine result atomically in the destination directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="账本自愈（GHOST-EXACT close / 受控 UNRECORDED T1 open）")
    ap.add_argument("--profile", choices=["live"], required=True)
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--apply", action="store_true",
                    help="公开版禁用；传入后 fail-closed，不写库")
    ap.add_argument("--max-heals", type=int, default=DEFAULT_MAX_HEALS,
                    help=f"单轮自愈上限，超限一笔不补（默认 {DEFAULT_MAX_HEALS}）")
    ap.add_argument("--self-cycle",
                    help="调用方自身 cycle_id；该 runner 不视为互斥冲突")
    ap.add_argument("--request-id",
                    help="调用方生成的唯一契约身份；人工调用留空则自动生成")
    ap.add_argument("--json-out", help="结构化结果原子落盘路径（UTF-8）")
    ap.add_argument("--enable-unrecorded", action="store_true",
                    help="公开版禁用；传入后 fail-closed，不写库")
    args = ap.parse_args()

    request_id = str(args.request_id or uuid.uuid4())
    db_root = Path(args.db_root)
    try:
        result = autoheal(
            args.profile, db_root, args.apply, args.max_heals, args.self_cycle,
            enable_unrecorded=args.enable_unrecorded,
            request_id=request_id,
        )
    except Exception as exc:  # final machine-contract boundary
        result = _new_result(args.profile, db_root, args.self_cycle, request_id)
        result["apply"] = bool(args.apply)
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["rc"] = RC_ERROR
        result = _finalize_result(result)

    text = json.dumps(result, ensure_ascii=False, indent=1, allow_nan=False)
    print(text)
    if args.json_out:
        _write_json_atomic(Path(args.json_out), text)
    return int(result["rc"])


if __name__ == "__main__":
    raise SystemExit(main())
