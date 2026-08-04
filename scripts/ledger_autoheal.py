# -*- coding: utf-8 -*-
r"""账本自愈（GHOST-EXACT 与 UNRECORDED 精确补账，2026-08-04）。

背景：`order_executor` 开仓时附挂的 algo 止损在交易所成交后，成交发生在任何 agent
轮次之外，系统没有回写入口 → 账本仍认为持仓在、OKX 已平 → `pretrade_ledger_position_mismatch`
把后续开仓挡死。本工具也能识别交易所有仓、账本缺少 open 的反向差异。

本脚本把「检测 → Demo 修复」内置进交易环节；Live 只做证据分类，仍须人工逐笔补账。

**不重复实现分级规则**：EXACT/FUZZY 判定一律 import
`reconcile_exchange_closes.classify`（唯一定义源），写库一律经该模块的
`apply_reconcile` → `collectors/trades_writer`。本脚本只负责**闸门与编排**。

自愈范围：
  - Live：永久只读分类；`--apply` / `--enable-unrecorded` 只会记录被阻断的
    写请求并返回非零，绝不写交易账本或关闭 repair_queue
  - Demo [GHOST-EXACT]：账本 > 现仓且 fills 精确解释差额 → `--apply` 时补 close 行
  - [GHOST-FUZZY] fills 对不上 → 只报告，转人工 ❌
  - Demo [UNRECORDED]：现仓 > 账本且开仓 fills 精确解释差额 → `--apply` 且显式传
    `--enable-unrecorded` 时只允许有 execution_intent 且 ordId 一致的 T1 补 open；
    无 intent 的 T2 只报告并升级 P0
  - [OVER_CLOSED] 账本净持仓为负 → 只报告，转人工 ❌

七条硬闸：
  1. 只补 EXACT；FUZZY、归属存疑的 UNRECORDED 与 OVER_CLOSED 一律不写。
  2. close 只允许 GHOST-EXACT；open 只允许开仓 fills 精确、订单归属一致的 T1，
     且必须用 `--enable-unrecorded` 独立开启。
  3. 单轮自愈上限 `--max-heals`（默认 3）；超限则**一笔都不补**并升级告警——
     那意味着系统性问题而非单笔漏账。
  4. runner 执行期互斥：同 profile 有 running runner 时跳过（`--self-cycle`
     放行调用方自身那一条，因为插入点 A 就跑在该 runner 会话内）。
  5. 幂等：复用 `consume_recorded` 先销账已记录的开/平仓腿，重复跑不重复补。
  6. Live 永久只读；Demo 写入成功后才允许关闭对应 repair_queue 条目。
  7. 全留痕：结构化 JSON 记录实际权限、被阻断的 Live 写请求和人工流程。

退出码：0=无差异或自愈全部成功；1=存在需人工项；2=错误/超上限/自愈失败；
        3=因 runner 互斥跳过（非故障）。

用法：
  pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 ^
      <PROJECT_ROOT>\scripts\ledger_autoheal.py --profile live
  pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 ^
      <PROJECT_ROOT>\scripts\ledger_autoheal.py --profile demo [--apply]
      [--enable-unrecorded] [--max-heals 3]
      [--self-cycle 2026-08-04T13:00] [--json-out <path>]

Live 无条件只读；若传写参数，仍完成只读分类并以结构化非零结果指向
unique ordId + 备份 + 写后复核的受控人工流程。Demo 不传 `--apply` 时只读。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, _project_path('scripts'))
sys.path.insert(0, _project_path('collectors'))

sys.path.insert(0, _project_path('core'))
sys.path.insert(0, _project_path('core', 'lib'))

import execution_intent as ei  # noqa: E402
import reconcile_exchange_closes as rec  # noqa: E402
import repair_queue_tool  # noqa: E402
from live_reconcile_monitor import active_runner  # noqa: E402

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_MAX_HEALS = 3
# UNRECORDED 无 intent 时的 fills 回看窗（天）——只用于定位开仓腿，不放宽判定
UNRECORDED_LOOKBACK_DAYS = 7
# execution_intents 终态；非终态 + 有 ord_id = 「单已提交、落库没跟上」的归属证据
INTENT_TERMINAL = ("completed", "failed_clean", "reconciled")


def _live_write_policy(requested_apply: bool,
                       requested_unrecorded: bool) -> dict:
    """Return the machine-readable public boundary for Live autoheal."""
    return {
        "mode": "read_only",
        "reason": "public_live_autoheal_permanently_read_only",
        "write_request_blocked": bool(
            requested_apply or requested_unrecorded
        ),
        "requested_apply": bool(requested_apply),
        "requested_enable_unrecorded": bool(requested_unrecorded),
        "manual_repair": {
            "tool": "scripts/reconcile_exchange_closes.py",
            "selection": "unique_ordId",
            "requirements": [
                "verify one unique exchange ordId",
                "create and verify SQLite backups before apply",
                "apply one record at a time",
                "post-check exchange positions, reconciliation, and ledger invariants",
            ],
        },
    }


def _intent_for(db_root: Path, profile: str, sym: str, side: str) -> dict | None:
    """找该 sym/side 的开仓意图归属证据（T1 判据）。

    非终态 + 有 ord_id ⇒ 单确实提交到交易所了、只是账没落上。
    历史样本在补账前是干净二元分布（completed 全有单号 / failed_clean 全无）；
    精确补账成功后会转为 reconciled。除此之外，非终态带单号本身就是异常信号。
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
            "SELECT cycle_id, symbol, action, side, request_fingerprint, "
            "       state, reserved_at, submitted_at, ord_id, error "
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


def _mark_intent_reconciled(
    db_root: Path,
    profile: str,
    intent: dict,
    result: dict,
) -> dict:
    """Terminalize the exact intent after its missing OPEN row is recovered.

    ``reconciled`` is intentionally distinct from ``completed``: the latter
    carries the executor's original replayable receipt, while this path only has
    exchange-fill and ledger-recovery evidence.  Both are profile-terminal, but
    retrying this exact logical order remains blocked to prevent a duplicate.
    """
    fingerprint = str(intent.get("request_fingerprint") or "").strip()
    ord_id = str(intent.get("ord_id") or "").strip()
    result_ord_ids = {str(value) for value in (result.get("ord_ids") or [])}
    if not fingerprint:
        raise RuntimeError("execution intent missing request_fingerprint")
    if not ord_id or ord_id not in result_ord_ids:
        raise RuntimeError("reconciled OPEN ordId does not match execution intent")

    evidence = {
        "ok": True,
        "intent_state": "reconciled",
        "reconciled_by": "ledger_autoheal",
        "profile": profile,
        "cycle_id": intent.get("cycle_id"),
        "symbol": intent.get("symbol"),
        "side": intent.get("side"),
        "ord_id": ord_id,
        "ledger_recovery": {
            "open_ts": result.get("open_ts"),
            "sz": result.get("sz"),
            "fill_px": result.get("wavg_px"),
            "ord_ids": sorted(result_ord_ids),
        },
    }
    ei.mark_reconciled(
        db_root / "ledger.db",
        profile=profile,
        cycle_id=str(intent.get("cycle_id") or ""),
        symbol=str(intent.get("symbol") or ""),
        side=str(intent.get("side") or ""),
        fingerprint=fingerprint,
        now_ts=_now(),
        ord_id=ord_id,
        receipt=evidence,
        error=None,
    )
    return evidence


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

    A same-symbol algo on the opposite side, a paused order, missing reduce-only
    semantics, an invalid trigger, or insufficient size never counts.  This probe
    is read-only and never places or amends an order.
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
                # OKX pending-algo truth is state=live.  An ``effective`` row
                # has already fired and therefore cannot protect the current
                # position from a future adverse move.
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
    """Close queue rows in the same db-root without corrupting JSON stdout.

    Queue bookkeeping is secondary to the already-applied ledger repair.  A
    queue failure is preserved as structured P1 evidence so the caller can
    still parse ``healed[].applied`` and rerun the authoritative position check.
    """
    if not qids:
        return
    try:
        rcq = repair_queue_tool.do_close(
            qids, False, resolution, True,
            closed_by="ledger_autoheal", db_path=account_db, quiet=True,
        )
    except Exception as exc:  # noqa: BLE001
        rcq = 2
        error = f"{type(exc).__name__}: {exc}"
    else:
        error = f"repair_queue_tool rc={rcq}" if rcq != 0 else None

    if rcq == 0:
        out["queue_closed"].extend(qids)
        return
    out["needs_human"].append({
        "kind": "QUEUE-CLOSE-ERROR",
        "sev": "P1",
        "queue_ids": qids,
        "account_db": str(account_db),
        "reason": error,
    })
    out["rc"] = 2


def _plan_unrecorded(profile: str, db_root: Path, by_key, nets,
                      sym: str, side: str, venue_sz: float, enabled: bool) -> dict:
    """UNRECORDED 三级证据链定级（P2·2026-08-04）。

    T1 = intent 归属证据齐 + 开仓腿 fills 精确解释缺口 → 自动补，元数据全真
    T2 = 无 intent，但 fills 精确解释缺口 → 只报告 + **P0**（归属证据不足）
    T3 = fills 对不上 / API 失败 → 只报告转人工

    ``enabled`` 只控制最终写入权限，不能关闭只读证据检查。否则默认配置
    恰好看不到 no-intent T2 和裸仓 P0，与“默认只读分析”的公开契约相反。
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
                "开仓 fills 虽精确解释缺口，但缺少 execution_intent 归属证据；"
                "公开版禁止自动补 open，转人工逐单核对"
            ),
            "notes": notes[:5],
            "ord_ids": sorted({str(g.get("ordId")) for g in matched}),
        }

    # intent ord_id 必须出现在匹配集里，否则归属证据与成交对不上 → 降级 T3
    if str(intent.get("ord_id")) not in {str(g.get("ordId")) for g in matched}:
        return {**base, "tier": "T3",
                "reason": f"intent ord_id={intent.get('ord_id')} 未出现在匹配 fills 组，归属存疑"}

    cycle_id = intent.get("cycle_id") or rec.slot_cycle_id(
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
             enable_unrecorded: bool = False) -> dict:
    requested_apply = bool(apply)
    requested_unrecorded = bool(enable_unrecorded)
    live_write_blocked = profile == "live" and (
        requested_apply or requested_unrecorded
    )
    # Defense in depth: runtime callers also withhold Live write flags, but the
    # Python API and direct CLI must remain read-only when called independently.
    if profile == "live":
        apply = False
        enable_unrecorded = False
    out: dict = {
        "ts": _now(),
        "profile": profile,
        "apply": bool(apply),
        "apply_requested": requested_apply,
        "enable_unrecorded": bool(enable_unrecorded),
        "enable_unrecorded_requested": requested_unrecorded,
        "skipped": None,
        "healed": [],
        "needs_human": [],
        "queue_closed": [],
        "rc": 2 if live_write_blocked else 0,
    }
    if profile == "live":
        out["write_policy"] = _live_write_policy(
            requested_apply, requested_unrecorded
        )

    # --- 闸 4：runner 执行期互斥 ---
    active = active_runner(profile, db_root)
    if active and str(active.get("cycle_id") or "") != str(self_cycle or ""):
        out["skipped"] = f"{profile}_runner_active"
        out["active_runner"] = active
        out["rc"] = 3
        return out

    db_path = db_root / f"{profile}_trades.db"
    if not db_path.exists():
        out["error"] = f"账本不存在: {db_path}"
        out["rc"] = 2
        return out
    # 经验库/equity 兜底同步指向本 db-root（测试副本时不碰真 account.db）
    account_db = db_root / "account.db"
    os.environ["OKX_ACCOUNT_DB"] = str(account_db)

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        by_key = rec.ledger_rows(con)
        nets = {k: rec.net_of(rows) for k, rows in by_key.items()}
        nets = {k: v for k, v in nets.items() if abs(v) > rec.SZ_TOL}
        try:
            ven = rec.venue_positions(profile)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"OKX 现仓 API 失败: {e}"
            out["rc"] = 2
            return out

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
            # 保护单是现仓安全事实，不是“是否允许补账”的写权限。默认只读路径也
            # 必须探测并升级裸仓；探测未知同样按 P0，绝不因关闭写开关而静默。
            sl = _probe_sl(profile, sym, side, item["venue_sz"])
            item["sl_probe"] = sl
            if sl.get("has_sl") is not True:
                out["needs_human"].append({
                    "kind": "NAKED-POSITION-P0", "symbol": sym, "side": side,
                    "sev": "P0", "sl_probe": sl,
                    "reason": "交易所现仓缺少或无法确认 pending algo 止损；"
                              "公开版保持账仓冻结，绝不补账、关工单或下单",
                })
                out["rc"] = 2
            if item["tier"] in ("T2", "T3"):
                out["needs_human"].append(item)
                if str(item.get("sev") or "").upper() == "P0":
                    out["rc"] = 2
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
        if not total_heals:
            out["rc"] = max(out["rc"], 1 if out["needs_human"] else 0)
            return out

        # --- 闸 3：单轮上限（幽灵 + UNRECORDED 合并计），超限一笔都不补 ---
        if total_heals > max_heals:
            out["error"] = (f"待自愈 {total_heals} 组（幽灵 {len(exact)} + "
                            f"UNRECORDED {len(unrecorded_todo)}）> 上限 {max_heals}，"
                            f"判为系统性异常，本轮不自愈（升级人工）")
            out["needs_human"].append({
                "kind": "OVER_CAP", "count": total_heals, "cap": max_heals,
                "reason": out["error"]})
            out["rc"] = 2
            return out

        for (sym, side), ghost_sz, matched, _ in exact:
            ord_ids = sorted({str(g.get("ordId")) for g in matched})
            item = {"symbol": sym, "side": side, "sz": ghost_sz,
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
                out["rc"] = 2
                out["healed"].append(item)
                continue
            item.update({"applied": True, "pnl": res["pnl"],
                         "fill_px": res["wavg_px"], "cycle_id": res["cycle_id"],
                         "close_ts": res["close_ts"]})
            out["healed"].append(item)

            # --- 闸 6：留痕，关闭同因 pending 工单 ---
            qids = _pending_queue_ids(account_db, profile, sym, side)
            _close_pending_queue(
                account_db,
                qids,
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
            # 闸 9：只读规划阶段已经确认交易所止损；未知或不存在都不得解除
            # 账仓冻结。自愈只补账、永不补挂订单。
            sl = plan.get("sl_probe") or {"has_sl": None, "error": "probe missing"}
            item["sl_probe"] = sl
            if sl.get("has_sl") is not True:
                item["note"] = "blocked_before_write: protective SL not confirmed"
                out["healed"].append(item)
                out["rc"] = 2
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
                out["rc"] = 2
                out["healed"].append(item)
                continue
            item.update({"applied": True, "fill_px": res["wavg_px"],
                         "cycle_id": res["cycle_id"], "open_ts": res["open_ts"],
                         "degradation": res["degradation"]})
            try:
                _mark_intent_reconciled(
                    db_root, profile, plan["intent"], res
                )
            except Exception as e:  # noqa: BLE001
                item["intent_transition_error"] = str(e)
                out["needs_human"].append({
                    "kind": "INTENT-RECONCILE-ERROR",
                    "sev": "P1",
                    "symbol": sym,
                    "side": side,
                    "ord_ids": plan["ord_ids"],
                    "reason": str(e),
                })
                out["rc"] = 2
                out["healed"].append(item)
                # The ledger write succeeded, but the profile remains blocked.
                # Keep repair_queue open so this split-brain state stays visible.
                continue
            item["intent_state"] = "reconciled"
            out["healed"].append(item)
            qids = _pending_queue_ids(account_db, profile, sym, side)
            _close_pending_queue(
                account_db,
                qids,
                f"ledger_autoheal 补 UNRECORDED open {sym}/{side} "
                f"sz={plan['missing_sz']:g} tier={plan['tier']} "
                f"ordId={','.join(plan['ord_ids'])}；账实已一致",
                out,
            )
    finally:
        con.close()

    if out["rc"] == 0 and out["needs_human"]:
        out["rc"] = 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="账本自愈（GHOST-EXACT close / UNRECORDED open，默认只读）")
    ap.add_argument("--profile", choices=["live", "demo"], required=True)
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--apply", action="store_true",
                    help="仅 Demo 真补账；Live 永久只读（默认 dry-run 只报告）")
    ap.add_argument("--max-heals", type=int, default=DEFAULT_MAX_HEALS,
                    help=f"单轮自愈上限，超限一笔不补（默认 {DEFAULT_MAX_HEALS}）")
    ap.add_argument("--self-cycle",
                    help="调用方自身 cycle_id；该 runner 不视为互斥冲突")
    ap.add_argument("--json-out", help="结构化结果原子落盘路径（UTF-8）")
    ap.add_argument("--enable-unrecorded", action="store_true",
                    help="仅 Demo 额外允许精确 UNRECORDED 补 open（默认关闭）")
    args = ap.parse_args()

    result = autoheal(args.profile, Path(args.db_root),
                      args.apply, args.max_heals, args.self_cycle,
                      enable_unrecorded=args.enable_unrecorded)

    text = json.dumps(result, ensure_ascii=False, indent=1)
    print(text)
    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    return int(result["rc"])


if __name__ == "__main__":
    raise SystemExit(main())
