# -*- coding: utf-8 -*-
r"""账本自愈（GHOST-EXACT 与受控 UNRECORDED 补账，2026-08-04）。

背景：`order_executor` 开仓时附挂的 algo 止损在交易所成交后，成交发生在任何 agent
轮次之外，系统没有回写入口 → 账本仍认为持仓在、OKX 已平 → `pretrade_ledger_position_mismatch`
把**后续所有 live 开仓**挡死（2026-08-04 实例：SKHY-USDT-SWAP 幽灵仓冻结 live 9h20m）。

本脚本把「检测 → 修复」内置进交易环节，取代人工逐笔补账。

**不重复实现分级规则**：EXACT/FUZZY 判定一律 import
`reconcile_exchange_closes.classify`（唯一定义源），写库一律经该模块的
`apply_reconcile` → `collectors/trades_writer`。本脚本只负责**闸门与编排**。

自愈范围：
  - [GHOST-EXACT] 账本 > 现仓且 fills 精确解释差额 → 显式 `--apply` 时补 close
  - [GHOST-FUZZY] fills 对不上 → 只报告，转人工 ❌
  - [UNRECORDED] 只有 intent/ordId/fills 归属精确、单一订单组且完整保护性
    SL 已确认的 T1，才可在 `--apply --enable-unrecorded` 同时开启时补 open。
    CLI 默认不开；生产调用方（插入点 A/B 与报告闸）是否默认传入见调用方。
    哨兵文件 `config/ledger_autoheal_unrecorded.off` 存在时一律只报告不写
    （它落在所有调用方共享的本子进程里，nudge 派发链收不到环境变量也生效）。
    订单属于调用方自身 cycle 时只报告，留给该 cycle 的 runner 自己落账。
    T2（无 intent）及 SL 缺失/未知均为 P0，写前阻断。
  - [OVER_CLOSED] 账本净持仓为负 → 只报告，转人工 ❌（P3 另行设计）

核心硬闸：
  1. 只补 EXACT；FUZZY、T2/T3 UNRECORDED、OVER_CLOSED 一律不写。
  2. close/open 分别需要独立正向授权；任何 P0 在本轮写库前阻断。
  3. 单次自愈写入上限 `--max-heals`（默认 3）。纯精确平仓积压可分批消化；
     其余未证明项继续阻断。含开仓漏账、负净仓、真实歧义或身份冲突时，
     超限仍一笔不补。查询预算耗尽项保持 FUZZY，后续调用重新取证。
  4. runner 执行期互斥：同 profile 有 running runner 时跳过（`--self-cycle`
     放行调用方自身那一条，因为插入点 A 就跑在该 runner 会话内）。
  5. 幂等：复用 `consume_recorded` 先销账已记录的平仓腿，重复跑不重复补。
  6. 全留痕：结构化 JSON + 自愈成功后关闭对应 repair_queue 条目，绝不静默改账本。

退出码：0=干净或安全写入完成；1=未解决/需人工；2=错误；
        3=runner 互斥跳过；4=P0。任何非 0 结果均 `blocking=true`。

用法：
  pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 ^
      <PROJECT_ROOT>\scripts\ledger_autoheal.py --profile live [--apply]
      [--enable-unrecorded] [--max-heals 3]
      [--self-cycle 2026-08-04T13:00] [--request-id <uuid>] [--json-out <path>]
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
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, _public_project_path('scripts'))
sys.path.insert(0, _public_project_path('collectors'))

sys.path.insert(0, _public_project_path('core', 'lib'))

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
# 补 open 的非环境变量 kill switch（2026-09-11）：nudge 起的派发链只带白名单
# 环境变量、收不到 OKX_*，所以关闭开关必须放在 A/B/报告闸共享的本子进程里。
UNRECORDED_KILL_SWITCH = (
    Path(__file__).resolve().parent.parent / "config"
    / "ledger_autoheal_unrecorded.off")
# 非 completed 的 intent 只在提交满这么久后才当 T1 写入依据：执行器/runner 可能
# 还在补交它自己的回执（2026-09-11 审查：迟到写入者闸）。
INFLIGHT_INTENT_GRACE = timedelta(minutes=15)


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
    ``completed`` 也可能在 intent 持久化后、trade writer 失败时留下真实
    UNRECORDED；此时必须额外验证 stored receipt 的成交身份。failed_clean 永不
    作为开仓归属证据。
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
        rows = con.execute(
            "SELECT cycle_id, symbol, action, side, state, reserved_at, "
            "       submitted_at, ord_id, error, receipt_json "
            "FROM execution_intents "
            "WHERE profile=? AND symbol=? AND side=? AND action IN ('open','add') "
            "  AND state <> 'failed_clean' "
            "  AND ord_id IS NOT NULL AND ord_id <> '' "
            "ORDER BY reserved_at DESC LIMIT 20",
            (profile, sym, side),
        ).fetchall()
        for row in rows:
            item = dict(row)
            if item.get("state") != "completed":
                item.pop("receipt_json", None)
                return item
            try:
                receipt = json.loads(item.get("receipt_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            ord_id = str(item.get("ord_id") or "")
            trades = receipt.get("trades") if isinstance(receipt, dict) else None
            matching = [
                trade for trade in (trades or [])
                if isinstance(trade, dict)
                and str(trade.get("symbol") or "") == sym
                and str(trade.get("side") or "").lower() == side
                and str(trade.get("ordId") or trade.get("ord_id") or "")
                == ord_id
                and str(trade.get("action") or "").lower() in {"open", "add"}
            ]
            if (
                receipt.get("ok") is True
                and str(receipt.get("ord_id") or receipt.get("ordId") or "")
                == ord_id
                and len(matching) == 1
            ):
                item["completed_receipt_verified"] = True
                # 保留已核身份的回执成交行：补 open 时沿用其保护/名义字段，
                # 让补出的行与迟到的真回执按同一 ordId 对齐（2026-09-11）。
                item["receipt_trade"] = dict(matching[0])
                item.pop("receipt_json", None)
                return item
        return None
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


def _unrecorded_kill_switch_on() -> bool:
    """哨兵文件存在即关闭补 open；读不到状态按关闭处理（宁可不写）。

    不能用 Path.exists()：它把 PermissionError 等 OSError 都吞成 False，
    「读不到」会被当成「没有开关」而继续写（2026-09-11 审查）。
    """
    try:
        os.stat(UNRECORDED_KILL_SWITCH)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _experience_open_recorded(account_db: Path, profile: str, sym: str,
                              side: str, ord_ids) -> bool:
    """只读回查补 open 应留下的开仓经验行（经验写入非致命，必须回读确认）。"""
    ids = [str(oid) for oid in (ord_ids or []) if str(oid or "").strip()]
    if not ids or not account_db.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{account_db.as_posix()}?mode=ro",
                              uri=True, timeout=10)
    except sqlite3.Error:
        return False
    try:
        for oid in ids:
            row = con.execute(
                "SELECT 1 FROM trade_experiences WHERE profile=? AND symbol=? "
                "AND LOWER(side)=? AND action='open' "
                "AND status NOT IN ('superseded','orphaned') AND raw LIKE ? LIMIT 1",
                (profile, sym, side, f"%{oid}%")).fetchone()
            if row is None:
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        con.close()


def _experience_row_closable(account_db: Path, profile: str, sym: str,
                             side: str) -> bool:
    """GHOST 自愈能否顺手关同 sym/side 的 experience_position 工单。

    UNRECORDED 方向（经验剩余 < 实仓）且经验库在发现时刻前后没有开仓行的工单
    由 jobb 的 ``hold_unrecorded_vanished`` 保持可见；这里与它同一判定、只读。
    读不到时不关（留给 jobb 同步，宁可晚关）。
    """
    name = f"ledger_invariant:experience_position:{profile}:{sym}:{side}"
    if not account_db.exists():
        return False
    try:
        import ledger_invariants  # noqa: PLC0415  与 jobb 共用唯一判定

        con = sqlite3.connect(f"file:{account_db.as_posix()}?mode=ro",
                              uri=True, timeout=10)
    except Exception:  # noqa: BLE001
        return False
    try:
        row = con.execute(
            "SELECT id, check_name, issue, ts FROM repair_queue "
            "WHERE status='pending' AND check_name=? ORDER BY id DESC LIMIT 1",
            (name,)).fetchone()
        if row is None:
            return True
        note = ledger_invariants.hold_unrecorded_vanished(
            con, {"id": row[0], "check_name": row[1],
                  "issue": row[2], "ts": row[3]})
        return not note
    except Exception:  # noqa: BLE001
        return False
    finally:
        con.close()


def _pending_queue_ids(account_db: Path, profile: str, sym: str, side: str,
                       include_experience: bool = True) -> list[int]:
    """找出因该 sym/side 不一致而开、且仍 pending 的 repair_queue 条目。

    ``include_experience=False`` 只取 pretrade 账仓不一致工单：补 open 的经验行
    未回读确认时，experience_position 工单必须留着（2026-09-11）。
    """
    if not account_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{account_db.as_posix()}?mode=ro",
                              uri=True, timeout=10)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        experience_name = (
            f"ledger_invariant:experience_position:{profile}:{sym}:{side}"
            if include_experience else None)
        rows = con.execute(
            "SELECT id FROM repair_queue WHERE status='pending' AND ("
            "  (check_name='order_executor'"
            "   AND issue LIKE '%pretrade_ledger_position_mismatch%'"
            "   AND issue LIKE ?)"
            "  OR check_name = ?"
            ")",
            (f"%{sym}/{side}%", experience_name),
        ).fetchall()
        return [int(r["id"]) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _pending_profile_ledger_blocks(account_db: Path, profile: str) -> list[int]:
    """Only pre-submit/no-order ledger-block tickets; never intent/SL/fill issues."""
    if not account_db.exists():
        return []
    con = None
    try:
        con = sqlite3.connect(account_db.resolve().as_uri()+"?mode=ro", uri=True, timeout=3)
        pattern = re.compile(r"\["+re.escape(profile)+r"\] [A-Z0-9]+-USDT-SWAP ord=None: pretrade_ledger_autoheal_blocked:.+")
        return [int(row[0]) for row in con.execute(
            "SELECT id,issue FROM repair_queue WHERE status='pending' AND check_name='order_executor'")
            if pattern.fullmatch(str(row[1] or ""))]
    except sqlite3.Error:
        return []
    finally:
        if con is not None: con.close()


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
                      sym: str, side: str, venue_sz: float, enabled: bool,
                      self_cycle: str | None = None) -> dict:
    """UNRECORDED 三级证据链定级（P2·2026-08-04）。

    T1 = intent 归属证据齐 + 开仓腿 fills 精确解释缺口 → 自动补，元数据全真
    T2 = 无 intent，但 fills 精确解释缺口 → 只报告 + **P0**
    T3 = fills 对不上 / API 失败 → 只报告转人工

    ``enabled`` 只控制最终写入，不关闭只读证据检查。
    2026-09-11 起 T1 另须：匹配集只有一个订单组（即 intent 那一单）、已核回执
    的张数与该组一致；订单属于 ``self_cycle`` 时只报告（``write_hold``）。
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
        # intent 订单整单在页内（张数 == 已核回执）同样自证 fills 覆盖（2026-09-11）。
        fills = rec.fetch_open_fills(profile, sym, side, int(t0.timestamp() * 1000),
                                     anchor=rec.receipt_open_anchor(intent))
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

    # 一行只记一单：规则 a 可能把多笔成交折成一行、落进最新 intent 的 cycle，
    # 其它订单的迟到写入者就无法按 ordId 去重（2026-09-11）。
    if len(matched) != 1:
        return {**base, "tier": "T3",
                "reason": (f"匹配集含 {len(matched)} 个订单组；自动补 open 只接受"
                           "与 intent 单号一致的单一订单组，转人工"),
                "ord_ids": sorted({str(g.get("ordId")) for g in matched})}
    receipt_trade = intent.get("receipt_trade")
    if isinstance(receipt_trade, dict):
        try:
            receipt_sz = float(receipt_trade.get("sz"))
        except (TypeError, ValueError):
            receipt_sz = None
        if (receipt_sz is None or not math.isfinite(receipt_sz)
                or abs(receipt_sz - float(matched[0]["sz"])) > rec.SZ_TOL):
            return {**base, "tier": "T3",
                    "reason": (f"intent 回执张数 {receipt_trade.get('sz')} 与 fills "
                               f"组 {matched[0]['sz']:g} 不一致，归属存疑")}

    cycle_id = (intent or {}).get("cycle_id") or rec.slot_cycle_id(
        rec.fill_dt(max(int(x.get("fillTime") or 0) for g in matched for x in g["fills"])))
    write_hold = None
    if self_cycle and str(cycle_id) == str(self_cycle):
        write_hold = ("订单属于调用方自身 cycle，留给该 cycle 的 runner "
                      "自己落账；本轮只报告")
    if write_hold is None and intent.get("state") != "completed":
        started = rec.parse_ts(
            intent.get("submitted_at") or intent.get("reserved_at"))
        if started is None or datetime.now(CST) - started < INFLIGHT_INTENT_GRACE:
            write_hold = (f"intent 仍在途（state={intent.get('state')}）且提交不足 "
                          f"{int(INFLIGHT_INTENT_GRACE.total_seconds() // 60)} 分钟，"
                          "可能还有迟到回执；本轮只报告")
    return {**base,
            "tier": "T1",
            "cycle_id": cycle_id,
            "intent": intent,
            "card": _card_for(db_root, cycle_id, sym),
            "matched": matched,
            "leftover_groups": len(leftover),
            "write_hold": write_hold,
            "ord_ids": sorted({str(g.get("ordId")) for g in matched})}


def _close_backlog_batch(verdict: dict, max_heals: int) -> list | None:
    """Bounded progress only for independent, exact, close-only repairs.

    No quantity/price/identity matching lives here; classify remains the only
    authority. Only its typed budget deferrals may coexist with an oversized
    batch. Genuine ambiguity and missing opens retain the systemic stop.
    """
    if (verdict["unrecorded"] or verdict["over_closed"]
            or verdict.get("leftover_orders")):
        return None
    deferred = verdict.get("deferred", [])
    if (len(deferred) != len(verdict["fuzzy"])
            or {(key, sz) for key, sz, _ in deferred}
            != {(key, sz) for key, sz, _, _ in verdict["fuzzy"]}
            or any(reason not in rec.CLOSE_DEFERRED_REASONS
                   for _, _, reason in deferred)):
        return None
    claimed = set()
    for _, _, matched, _ in verdict["exact"]:
        if not matched:
            return None
        for group in matched:
            oid = str(group.get("ordId") or "").strip()
            if not oid or oid == "?" or oid in claimed:
                return None
            claimed.add(oid)
    # Ledger order is stable; written groups disappear on the next fresh read.
    return verdict["exact"][:max_heals]


def autoheal(profile: str, db_root: Path, apply: bool,
             max_heals: int, self_cycle: str | None,
             enable_unrecorded: bool = False,
             request_id: str | None = None) -> dict:
    db_root = Path(db_root).resolve()
    out = _new_result(profile, db_root, self_cycle, request_id)
    out["apply"] = bool(apply)
    out["unrecorded_write_enabled"] = bool(enable_unrecorded)
    if apply or enable_unrecorded:
        out["error"] = (
            "public release ledger_autoheal is permanently read-only; "
            "--apply and --enable-unrecorded are disabled"
        )
        out["rc"] = RC_ERROR
        return _finalize_result(out)
    if enable_unrecorded and _unrecorded_kill_switch_on():
        enable_unrecorded = False
        out["unrecorded_kill_switch"] = str(UNRECORDED_KILL_SWITCH)
    out["unrecorded_write_enabled"] = bool(enable_unrecorded)
    if type(max_heals) is not int or max_heals < 1:
        out["error"] = "max_heals must be a positive integer"
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
                                    sym, side, sz, enable_unrecorded,
                                    self_cycle=self_cycle)
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
            if apply and not out["needs_human"]:
                from core.open_intent_recovery import recover_booked_submitted_open
                out["intent_recovery"] = recover_booked_submitted_open(db_root,self_cycle,apply=True)
            from scripts.ledger_recovery import enabled as recovery_enabled
            if apply and recovery_enabled(self_cycle) and not out["needs_human"]:
                # A block ticket names the attempted new symbol, not the old
                # position that caused the profile mismatch. Close only this
                # exact no-order ticket family after a clean full comparison.
                queue_result = {"queue_closed": [], "needs_human": [], "rc": RC_OK}
                _close_pending_queue(
                    account_db, _pending_profile_ledger_blocks(account_db, profile),
                    "全集账仓独立核对一致；此前交易前未下单的账本阻断已解除，未重放订单。",
                    queue_result)
                out["queue_closed"].extend(queue_result["queue_closed"])
                if queue_result["needs_human"]:
                    out.setdefault("warnings", []).extend(queue_result["needs_human"])
            return _finalize_result(out)

        # --- 闸 3：每次最多 max_heals 组；纯精确平仓积压允许有界进展 ---
        if total_heals > max_heals:
            batch = _close_backlog_batch(verdict, max_heals)
            if batch is None:
                out["error"] = (f"待自愈 {total_heals} 组（幽灵 {len(exact)} + "
                                f"UNRECORDED {len(unrecorded_todo)}）> 上限 {max_heals}，"
                                "且不满足纯精确平仓分批条件，本轮不自愈（升级人工）")
                out["needs_human"].append({
                    "kind": "OVER_CAP", "count": total_heals, "cap": max_heals,
                    "reason": out["error"]})
                out["rc"] = RC_ERROR
                return _finalize_result(out)
            pending = exact[len(batch):]
            out["backlog"] = {
                "cap": max_heals,
                "selected_count": len(batch),
                "remaining_exact_count": len(pending),
                "evidence_deferred_count": len(verdict.get("deferred", [])),
                "pending": [
                    {"symbol": sym, "side": side, "sz": sz,
                     "ord_ids": [str(g["ordId"]) for g in matched]}
                    for (sym, side), sz, matched, _ in pending],
            }
            out["needs_human"].append({
                "kind": "AUTOHEAL-BACKLOG", "count": len(pending),
                "cap": max_heals,
                "reason": f"本次最多处理 {len(batch)} 组精确平仓账；"
                          f"其余 {len(pending)} 组待后续调用重新取证，"
                          "账仓未全部一致前继续阻断交易/报告放行",
            })
            exact = batch

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
            # UNRECORDED 方向、经验库又没有对应开仓的 experience_position 工单
            # （补 open 时经验写入失败留下的）不在这里顺手关，交给 jobb 的
            # hold_unrecorded_vanished 判定（2026-09-11 审查）。
            qids = _pending_queue_ids(
                account_db, profile, sym, side,
                include_experience=_experience_row_closable(
                    account_db, profile, sym, side))
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
                item["note"] = ("report-only: 补 open 已被哨兵文件关闭，未写库"
                                if out.get("unrecorded_kill_switch") else
                                "report-only: --enable-unrecorded 未开启，未写库")
                out["healed"].append(item)
                continue
            if plan.get("write_hold"):
                item["note"] = f"hold: {plan['write_hold']}"
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
            if res.get("status") == "already_recorded":
                item["note"] = ("already_recorded: 该 ordId 已由其他写入方落账，"
                                "本轮未重复写入")
                out["healed"].append(item)
                continue
            experience_ok = _experience_open_recorded(
                account_db, profile, sym, side, plan["ord_ids"])
            item.update({"applied": True, "fill_px": res["wavg_px"],
                         "cycle_id": res["cycle_id"], "open_ts": res["open_ts"],
                         "degradation": res["degradation"],
                         "experience": {"verified": experience_ok,
                                        "write": res.get("exp")}})
            out["healed"].append(item)
            if not experience_ok:
                # 经验写入非致命，但不能静默：账本已一致而经验缺 open 行时，
                # experience_position 工单留给人工补经验（不影响交易放行）。
                out.setdefault("warnings", []).append({
                    "kind": "UNRECORDED-EXPERIENCE-MISSING", "symbol": sym,
                    "side": side,
                    "reason": "账本已补 open，但经验库回读不到对应开仓行；"
                              "experience_position 工单保持 pending，补经验后人工关单",
                })
            qids = _pending_queue_ids(account_db, profile, sym, side,
                                      include_experience=experience_ok)
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

    if apply and not out.get("needs_human") and not out.get("error"):
        from core.open_intent_recovery import recover_booked_submitted_open
        out["intent_recovery"] = recover_booked_submitted_open(db_root,self_cycle,apply=True)
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
    ap.add_argument("--db-root", default=_public_project_path('db'))
    ap.add_argument("--apply", action="store_true",
                    help="真补账（默认 dry-run 只报告）")
    ap.add_argument("--max-heals", type=int, default=DEFAULT_MAX_HEALS,
                    help=f"单次写入上限；纯精确平仓积压可分批（默认 {DEFAULT_MAX_HEALS}）")
    ap.add_argument("--self-cycle",
                    help="调用方自身 cycle_id；该 runner 不视为互斥冲突")
    ap.add_argument("--request-id",
                    help="调用方生成的唯一契约身份；人工调用留空则自动生成")
    ap.add_argument("--json-out", help="结构化结果原子落盘路径（UTF-8）")
    ap.add_argument("--enable-unrecorded", action="store_true",
                    help="额外允许严格 T1 UNRECORDED 补 open（CLI 默认关闭；"
                         "哨兵文件 config/ledger_autoheal_unrecorded.off 存在时无效）")
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
