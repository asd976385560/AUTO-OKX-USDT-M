# -*- coding: utf-8 -*-
r"""Audit agent rule compliance from frozen decision cards and position plans.

2026-08-19 F3。只读审计：不改库、不下单、不重放、不补采。三条规则各带预注册
激活边界，边界前的历史一律只进 ``pre_activation_diagnostics``，不反向加责。
违规是**数据**不是进程失败：审计跑通即 rc=0，结论写在 JSON 的
``overall_status``；只有审计自身无法完成（库缺失/解析失败）才 rc=2。

2026-08-28 判断门槛退役：主人拍板判断类开仓门槛全面降级为参考（硬限制只保留
确定性风控闸与数据真实性契约），三条规则自 ``RULES_RETIRED_CYCLE`` 起停止判
违规，之后的卡只进 ``post_retirement_diagnostics`` 观察；边界前的历史判定与
诊断口径原样保留，同样不反向重判。

规则阈值硬编码在本文件（单一事实源），并用 ``RULE_DOC_ANCHORS`` 对
``agents/live_trader.md`` 做子串存在性守卫：手册改写导致锚点消失时本审计
自报 ``doc_drift`` 并降级为 INSUFFICIENT_EVIDENCE，绝不静默继续按旧阈值判定。
刻意不解析手册正文取阈值——该文件是中文散文，数字埋在句中，任何改写都会让
正则静默失配，等于审计器无声地把自己关掉，而这正是本脚本要治的病。

数据来源（双库，缺一不可）：
  analysis.db.analysis_signals.decision_card
      → risk_reward.{entry,stop,rr,exit_mode,ev_override}
      → multitimeframe_analysis.evidence_contract.timeframes["1H"].values.atr14
      → reference_overrides（stop_below_3x_atr_1h）
      → ev_check.ev_r（writer 注入的 canonical 值）
  live_trades.db.trade_cycles.raw.requested_position_actions[]
      → target_stop_risk_pct_equity（**只在这里有，不在 analysis.db**）

用法：
    python audit_rule_compliance.py --db-root <PROJECT_ROOT>\db \
        --json-out <PROJECT_ROOT>\reports\quality\rule-compliance-audit.json
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
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _db_ro import connect_ro

CST = timezone(timedelta(hours=8))

# ── 预注册激活边界（只向前生效）──
# 2026-08-20 更正：止损 3×ATR 与探针 fixed_tp 两条边界原为 `2026-08-17T00:00`
# （部署日零点），但两条规则都是 2026-08-17 16:30 那批才写进文档的——
# `reports/quality/backups/loss-remediation-20260817-1630/` 下的三份 before 备份
# （okx-live-trader-AGENTS / workspace-AGENTS / skill）里都**不含**「3×该标的 1H
# atr14」与「exit_mode=fixed_tp 且 rr>=2.0」。以零点为界等于拿规则生效前 16 小时
# 的卡按新规重判，与本项目「边界只向前、历史不重算不重判」直接冲突（当天 01:30–
# 13:15 的 8+5 条「违规」全部由此产生）。按 `_acceptance_thresholds` 同一约定改为
# 「本批部署后的第一个整点」：批次 16:30:28 起、16:46:17 收，取 17:00。
# 该更正不改变任何一笔交易的盈亏、胜率或报表统计，被重新归类的卡仍逐条留在
# pre_activation_diagnostics 里可查。
STOP_ATR_ACTIVATION_CYCLE = "2026-08-17T17:00"
PROBE_FIXED_TP_ACTIVATION_CYCLE = "2026-08-17T17:00"
# ev_override 规则早于本批（2026-08-10 Wave1 拍板），此处边界只是审计起算点，
# 提前它会反过来重判更早的历史，故保持不动。
EV_OVERRIDE_ACTIVATION_CYCLE = "2026-08-17T00:00"

# 扫描起点必须是**最早**的那条边界，否则抬高任一规则的边界会把边界前的卡直接
# 滤出查询，pre_activation 诊断跟着一起消失——那就成了「把不合口径的历史删掉」
# 而不是「重新归类」。这里显式取 min，随边界自动跟随。
AUDIT_SCAN_START_CYCLE = min(
    STOP_ATR_ACTIVATION_CYCLE,
    PROBE_FIXED_TP_ACTIVATION_CYCLE,
    EV_OVERRIDE_ACTIVATION_CYCLE,
)

# ── 硬编码阈值（与 agents/live_trader.md 第 4/6 条同源）──
STOP_ATR_MIN_MULTIPLE = 3.0
STOP_ATR_OVERRIDE_TOKEN = "stop_below_3x_atr_1h"
PROBE_RISK_PCT_EQUITY_MAX = 0.01
PROBE_REQUIRED_EXIT_MODE = "fixed_tp"
PROBE_MIN_RR = 2.0
# 2026-08-20 override 路径二：无合格催化的负 EV 候选可开，但门更高。
PATH_TWO_MIN_RR = 2.5
# 预注册前向边界：路径二与宽 ATR 上限都是 2026-08-20 才成文的条款，边界之前
# 写的卡按当时生效的口径判定，一律不重算、不重判，只作 pre_activation 诊断。
PATH_TWO_ACTIVATION_CYCLE = "2026-08-20T12:00"
STOP_ATR_WIDE_CAP_PCT = 0.08          # 3×ATR 超过入场价 8% 时下限不再强制
STOP_ATR_WIDE_OVERRIDE_TOKEN = "stop_capped_wide_atr"
STOP_ATR_WIDE_MIN_MULTIPLE = 1.5      # 上限放开后仍不得紧于 1.5×ATR

# ── 判断门槛退役边界（2026-08-28 主人拍板：判断类开仓门槛全面降级为参考，
# 硬限制只保留确定性风控闸与数据真实性契约）──
# 与激活边界同一约定「边界只向前生效」：退役边界之后的卡不再按已撤销的门槛
# 判违规，只进 post_retirement_diagnostics 作观察；边界之前的历史判定原样保留。
# 取值=角色契约 V2.13-role 部署完成后的首个 15m 槽。退役方向的边界宁早勿晚——
# 取晚了会把已按新契约（无门槛）行事的卡按已废规则判违规，性质等同激活边界
# 取早（拿尚不存在的规则判历史）。三条规则同一边界：本批一次性全部降级。
RULES_RETIRED_CYCLE = "2026-08-28T11:45"

# ── 文档漂移守卫：阈值靠代码，同步靠断言 ──
RULE_DOC_ANCHORS = (
    "all_market_lightweight_open_v1",
    "OPEN不再要求六/九项展示卡",
    "signals.analysis_signals",
    "不设置最低开仓数",
    "钱路硬闸保持不变",
)
DEFAULT_RULE_DOC = Path(_public_project_path('agents', 'live_trader.md'))

OPEN_ACTIONS = ("open_long", "open_short")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _obj(value):
    """把可能是 JSON 串的字段安全解成 dict；失败返回 {}。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or abs(value) == float("inf"):
        return None
    return float(value)


def _atr14_1h(card: dict):
    contract = ((card.get("multitimeframe_analysis") or {})
                .get("evidence_contract") or {})
    frame = (contract.get("timeframes") or {}).get("1H") or {}
    return _num((frame.get("values") or {}).get("atr14"))


def _override_tokens(card: dict) -> list:
    """reference_overrides 可能是 list[str] 或 list[dict]，统一成小写串。"""
    raw = card.get("reference_overrides")
    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                out.append(item.strip().lower())
            elif isinstance(item, dict):
                for key in ("token", "rule", "name", "id", "override"):
                    val = item.get(key)
                    if isinstance(val, str):
                        out.append(val.strip().lower())
    elif isinstance(raw, str):
        out.append(raw.strip().lower())
    return out


def _doc_guard(doc_path: Path) -> dict:
    """手册锚点存在性守卫。锚点缺失即 doc_drift（阈值可能已被改写）。"""
    try:
        text = doc_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"checked": False, "doc": str(doc_path), "error": str(exc),
                "doc_drift": True, "missing_anchors": list(RULE_DOC_ANCHORS)}
    missing = [a for a in RULE_DOC_ANCHORS if a not in text]
    return {"checked": True, "doc": str(doc_path),
            "anchors_total": len(RULE_DOC_ANCHORS),
            "missing_anchors": missing, "doc_drift": bool(missing)}


def _probe_risk_by_cycle_symbol(live_db: Path, since_cycle: str) -> dict:
    """从 trade_cycles.raw.requested_position_actions 取目标止损风险。

    该字段**不在 analysis.db**；OPEN/ADD 的 target_stop_risk_pct_equity 只在
    plan/回执里出现（首现 2026-08-15T21:45）。键 = (cycle_id, symbol)。
    """
    out: dict = {}
    con = connect_ro(live_db, timeout=10)
    try:
        rows = con.execute(
            "SELECT cycle_id, raw FROM trade_cycles WHERE cycle_id>=?",
            (since_cycle,)).fetchall()
    finally:
        con.close()
    for cycle_id, raw in rows:
        actions = (_obj(raw).get("requested_position_actions") or [])
        if not isinstance(actions, list):
            continue
        for act in actions:
            if not isinstance(act, dict):
                continue
            if str(act.get("action") or "").strip().upper() not in ("OPEN", "ADD"):
                continue
            risk = _num(act.get("target_stop_risk_pct_equity"))
            if risk is None:
                continue
            sym = str(act.get("symbol") or "").strip()
            if sym:
                out[(str(cycle_id), sym)] = risk
    return out


def audit(db_root: Path, since_cycle: str, doc_path: Path) -> dict:
    analysis_db = db_root / "analysis.db"
    live_db = db_root / "live_trades.db"
    doc = _doc_guard(doc_path)

    con = connect_ro(analysis_db, timeout=10)
    try:
        rows = con.execute(
            "SELECT cycle_id, symbol, action, decision_card "
            "FROM analysis_signals WHERE cycle_id>=? AND action IN (?,?) "
            "ORDER BY cycle_id, symbol",
            (since_cycle, *OPEN_ACTIONS)).fetchall()
    finally:
        con.close()
    probe_risk = _probe_risk_by_cycle_symbol(live_db, since_cycle)

    stop_atr = {"evaluated": 0, "violations": [], "declared_overrides": 0,
                "wide_atr_candidates": 0, "wide_atr_declared": 0,
                "multiples": [], "no_atr": 0}
    probe = {"evaluated": 0, "violations": []}
    ev = {"evaluated": 0, "negative": 0, "with_override": 0,
          "accepts_negative": 0, "path_two_compliant": 0, "violations": []}
    pre_activation: list = []
    post_retirement: list = []

    for cycle_id, symbol, action, card_json in rows:
        card = _obj(card_json)
        if not card:
            continue
        rr_block = card.get("risk_reward") or {}
        entry = _num(rr_block.get("entry"))
        stop = _num(rr_block.get("stop"))
        atr = _atr14_1h(card)
        tokens = _override_tokens(card)
        ident = {"cycle_id": cycle_id, "symbol": symbol, "action": action}

        # ── 规则一：止损距离 ≥ 3×ATR14(1H)（2026-08-28 起退役，只观察）──
        if entry is not None and stop is not None and atr:
            multiple = round(abs(entry - stop) / atr, 3)
            if str(cycle_id) >= RULES_RETIRED_CYCLE:
                post_retirement.append({**ident, "rule": "stop_atr",
                                        "multiple": multiple})
            elif str(cycle_id) >= STOP_ATR_ACTIVATION_CYCLE:
                stop_atr["evaluated"] += 1
                stop_atr["multiples"].append(multiple)
                declared = STOP_ATR_OVERRIDE_TOKEN in tokens
                if declared:
                    stop_atr["declared_overrides"] += 1
                # 2026-08-20 宽 ATR 上限：3×ATR 超过入场价 8% 时下限不再强制，
                # 但必须声明 stop_capped_wide_atr 且不得紧于 1.5×ATR。
                wide = (str(cycle_id) >= PATH_TWO_ACTIVATION_CYCLE
                        and entry
                        and (STOP_ATR_MIN_MULTIPLE * atr / abs(entry)
                             > STOP_ATR_WIDE_CAP_PCT))
                if wide:
                    stop_atr["wide_atr_candidates"] += 1
                    if STOP_ATR_WIDE_OVERRIDE_TOKEN in tokens:
                        stop_atr["wide_atr_declared"] += 1
                        if multiple < STOP_ATR_WIDE_MIN_MULTIPLE:
                            stop_atr["violations"].append({
                                **ident, "atr14_1h": atr,
                                "multiple": multiple,
                                "required": STOP_ATR_WIDE_MIN_MULTIPLE,
                                "rule": "wide_atr_floor"})
                        continue_floor = False
                    else:
                        continue_floor = True
                else:
                    continue_floor = True
                if (continue_floor and multiple < STOP_ATR_MIN_MULTIPLE
                        and not declared):
                    stop_atr["violations"].append({
                        **ident, "atr14_1h": atr, "stop_distance": abs(entry - stop),
                        "multiple": multiple, "required": STOP_ATR_MIN_MULTIPLE,
                        "override_declared": False})
            else:
                pre_activation.append({**ident, "rule": "stop_atr",
                                       "multiple": multiple})
        elif RULES_RETIRED_CYCLE > str(cycle_id) >= STOP_ATR_ACTIVATION_CYCLE:
            stop_atr["no_atr"] += 1

        # ── 规则二：探针尺寸必须 fixed_tp 且 rr>=2.0（2026-08-28 起退役，只观察）──
        risk_pct = probe_risk.get((str(cycle_id), str(symbol)))
        if risk_pct is not None and risk_pct <= PROBE_RISK_PCT_EQUITY_MAX:
            exit_mode = str(rr_block.get("exit_mode") or "").strip().lower()
            rr_value = _num(rr_block.get("rr"))
            if str(cycle_id) >= RULES_RETIRED_CYCLE:
                post_retirement.append({
                    **ident, "rule": "probe_fixed_tp",
                    "target_stop_risk_pct_equity": risk_pct,
                    "exit_mode": exit_mode, "rr": rr_value})
            elif str(cycle_id) >= PROBE_FIXED_TP_ACTIVATION_CYCLE:
                probe["evaluated"] += 1
                bad = []
                if exit_mode != PROBE_REQUIRED_EXIT_MODE:
                    bad.append(f"exit_mode={exit_mode or 'missing'}")
                if rr_value is None or rr_value < PROBE_MIN_RR:
                    bad.append(f"rr={rr_value}")
                if bad:
                    probe["violations"].append({
                        **ident, "target_stop_risk_pct_equity": risk_pct,
                        "problems": bad})
            else:
                pre_activation.append({
                    **ident, "rule": "probe_fixed_tp",
                    "target_stop_risk_pct_equity": risk_pct,
                    "exit_mode": exit_mode, "rr": rr_value})

        # ── 规则三：ev_r<0 必须带 ev_override（2026-08-28 起退役，只观察；
        #    ev_override 结构存在性由 analyst_writer 在写入时继续强制——那是
        #    数据真实性契约，不随判断门槛退役）──
        ev_r = _num((card.get("ev_check") or {}).get("ev_r"))
        if ev_r is not None:
            if str(cycle_id) >= RULES_RETIRED_CYCLE:
                post_retirement.append({**ident, "rule": "ev_override",
                                        "ev_r": ev_r})
            elif str(cycle_id) >= EV_OVERRIDE_ACTIVATION_CYCLE:
                ev["evaluated"] += 1
                if ev_r < 0:
                    ev["negative"] += 1
                    override = rr_block.get("ev_override")
                    if not override:
                        ev["violations"].append({**ident, "ev_r": ev_r,
                                                 "rule": "missing_override"})
                    else:
                        ev["with_override"] += 1
                        # 路径二（自证胜率）只在无一级源催化时使用，本审计无法
                        # 判定催化是否合格，故只钉住路径二自身的算术与结构门：
                        # claim_ev_r 必须为正、rr>=2.5、fixed_tp。命中探针尺寸
                        # 且 claim_ev_r<=0 一定不合规——两条路径都不允许自认
                        # 负期望还开仓。
                        ev_block = card.get("ev_check") or {}
                        claim = _num(ev_block.get("claim_ev_r"))
                        if str(cycle_id) < PATH_TWO_ACTIVATION_CYCLE:
                            pre_activation.append({
                                **ident, "rule": "ev_path_two",
                                "ev_r": ev_r, "claim_ev_r": claim})
                            continue
                        risk_p = probe_risk.get((str(cycle_id), str(symbol)))
                        is_probe = (risk_p is not None
                                    and risk_p <= PROBE_RISK_PCT_EQUITY_MAX)
                        if claim is not None and claim <= 0:
                            ev["accepts_negative"] += 1
                            ev["violations"].append({
                                **ident, "ev_r": ev_r, "claim_ev_r": claim,
                                "rule": "accepts_negative_ev"})
                        elif is_probe:
                            rr_v = _num(rr_block.get("rr"))
                            mode = str(
                                rr_block.get("exit_mode") or "").strip().lower()
                            bad = []
                            if rr_v is None or rr_v < PATH_TWO_MIN_RR:
                                bad.append(f"rr={rr_v}<{PATH_TWO_MIN_RR}")
                            if mode != PROBE_REQUIRED_EXIT_MODE:
                                bad.append(f"exit_mode={mode or 'missing'}")
                            if bad:
                                ev["violations"].append({
                                    **ident, "ev_r": ev_r,
                                    "claim_ev_r": claim,
                                    "rule": "path_two_conditions",
                                    "problems": bad})
                            else:
                                ev["path_two_compliant"] += 1
            else:
                pre_activation.append({**ident, "rule": "ev_override",
                                       "ev_r": ev_r})

    multiples = sorted(stop_atr.pop("multiples"))
    stop_atr["multiple_median"] = (
        multiples[len(multiples) // 2] if multiples else None)
    stop_atr["multiple_min"] = multiples[0] if multiples else None
    stop_atr["multiple_max"] = multiples[-1] if multiples else None
    stop_atr["violation_count"] = len(stop_atr["violations"])
    probe["violation_count"] = len(probe["violations"])
    ev["violation_count"] = len(ev["violations"])
    ev["override_rate_pct"] = (
        round(ev["with_override"] / ev["negative"] * 100, 1)
        if ev["negative"] else None)

    total_violations = (stop_atr["violation_count"] + probe["violation_count"]
                        + ev["violation_count"])

    # 逐规则状态：**零样本不等于合规**。边界更正后这两条规则的激活期恰好落在
    # 2026-08-17T15:15 之后的零开仓窗内，`evaluated=0`；若仍按「无违规即 PASS」
    # 收口，审计会对一条从未被检验过的规则报绿灯——正是本项目禁止的
    # 「样本不足只能判定为证据不足，不得判定达标」。
    for block in (stop_atr, probe, ev):
        if block["violation_count"]:
            block["status"] = "VIOLATIONS_FOUND"
        elif block["evaluated"]:
            block["status"] = "PASS"
        else:
            # 与 audit_complete_cycle_sla / audit_contract_statistics_coverage
            # 同一词汇：前向窗口还没攒到样本，不是达标。
            block["status"] = "PENDING_FORWARD_EVIDENCE"
            block["status_reason"] = (
                "激活边界之后尚无 open 决策卡进入分母；本规则未被检验，不得读作合规")
    evaluated_total = (stop_atr["evaluated"] + probe["evaluated"]
                       + ev["evaluated"])
    pending_rules = [
        name for name, block in (
            ("stop_distance_vs_atr_1h", stop_atr),
            ("probe_fixed_tp", probe),
            ("ev_override", ev))
        if block["status"] == "PENDING_FORWARD_EVIDENCE"]

    if doc.get("doc_drift"):
        overall = "INSUFFICIENT_EVIDENCE"
    elif total_violations:
        overall = "VIOLATIONS_FOUND"
    elif evaluated_total == 0:
        overall = "PENDING_FORWARD_EVIDENCE"
    else:
        # PASS 只表示「可测的部分没有违规」；仍未取得前向样本的规则单列，
        # 不许被这个 PASS 顺带读成合规。
        overall = "PASS"

    return {
        "schema_version": 1,
        "generated_at_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "db_root": str(db_root),
        "since_cycle": since_cycle,
        "overall_status": overall,
        "total_violations": total_violations,
        "evaluated_total": evaluated_total,
        "rules_pending_forward_evidence": pending_rules,
        "overall_status_note": (
            "PASS 只覆盖 evaluated>0 的规则；rules_pending_forward_evidence "
            "列出的规则尚无前向样本，未被检验，不得读作合规。三条规则自 "
            "rules_retired_cycle 起退役（判断门槛降级为参考），之后的卡只进 "
            "post_retirement_diagnostics 观察，不判违规"),
        "rules_retired_cycle": RULES_RETIRED_CYCLE,
        "rule_doc_guard": doc,
        "thresholds": {
            "stop_atr_min_multiple": STOP_ATR_MIN_MULTIPLE,
            "stop_atr_override_token": STOP_ATR_OVERRIDE_TOKEN,
            "probe_risk_pct_equity_max": PROBE_RISK_PCT_EQUITY_MAX,
            "probe_required_exit_mode": PROBE_REQUIRED_EXIT_MODE,
            "probe_min_rr": PROBE_MIN_RR,
        },
        "activation_boundaries": {
            "stop_atr": STOP_ATR_ACTIVATION_CYCLE,
            "probe_fixed_tp": PROBE_FIXED_TP_ACTIVATION_CYCLE,
            "ev_override": EV_OVERRIDE_ACTIVATION_CYCLE,
        },
        "open_cards_scanned": len(rows),
        "stop_distance_vs_atr_1h": stop_atr,
        "probe_fixed_tp": probe,
        "ev_override": ev,
        "pre_activation_diagnostics_count": len(pre_activation),
        "pre_activation_diagnostics": pre_activation[:50],
        "post_retirement_diagnostics_count": len(post_retirement),
        "post_retirement_diagnostics": post_retirement[:50],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Agent 规则遵守度只读审计（3×ATR / 探针 fixed_tp / EV override）")
    ap.add_argument("--db-root", default=_public_project_path('db'))
    ap.add_argument("--since-cycle", default=AUDIT_SCAN_START_CYCLE,
                    help="只审计该 cycle_id 起的开仓卡（默认=最早激活边界）")
    ap.add_argument("--rule-doc", default=str(DEFAULT_RULE_DOC))
    ap.add_argument("--json-out", default=None)
    # 2026-08-19：固定名只是「最新快照」指针，另落一份按业务日命名的历史件。
    # 起因：daily_maintenance 每天往同一个固定名里写，跑一个月也只剩最后一天，
    # 而放宽 3×ATR 条款的前提恰恰是「先有 2~4 周连续观测数据」（方案 §F）——
    # 固定名等于把这个前提永远卡死。同日重跑覆盖当日件（当日最后一次即当日结论）。
    ap.add_argument("--no-dated-copy", action="store_true",
                    help="只写 --json-out 固定名，不落按日历史件")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        payload = audit(Path(args.db_root), args.since_cycle,
                        Path(args.rule_doc))
    except Exception as exc:            # 审计自身失败才是进程失败
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.json_out:
        out_path = Path(args.json_out)
        _atomic_write_json(out_path, payload)
        if not args.no_dated_copy:
            # 业务日取 payload 自报的 generated_at_cst，而不是再读一次时钟——
            # 跨零点时两者会落在不同日，历史件必须与内容自证的时间同源。
            day = str(payload.get("generated_at_cst") or "")[:10]
            if len(day) == 10:
                _atomic_write_json(
                    out_path.with_name(f"{out_path.stem}-{day}{out_path.suffix}"),
                    payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
