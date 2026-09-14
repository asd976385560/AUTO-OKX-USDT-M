# -*- coding: utf-8 -*-
"""Pre-registered, forward-only acceptance-threshold migrations.

单一事实源：任何验收口径变更都在这里登记**一次**预注册激活边界，消费脚本按本
次运行的 ``as_of`` 解析当次判定阈值。边界只向前生效——边界之前的运行仍按老口径
判定，已归档的证据文件不重算、不重判，也不回填历史窗口的结论。

同时提供「按老口径的达成率」诊断助手：闸门取新值，老值继续外显，长期信息不丢。

本模块只有常量与纯函数：不读库、不写文件、不发请求、不下单。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping


CST = timezone(timedelta(hours=8))

# 2026-08-15 主人拍板（《OKX 目标任务提示词 V2.1》§1/§3）：数据完善率与报告/
# 推送完整度四族审计闸门 99% → 95%。边界=本批部署后的第一个整点，只向前生效。
COVERAGE_TARGET_ACTIVATION_CST = "2026-08-15T20:00:00+08:00"
COVERAGE_LEGACY_TARGET_RATE = 0.99
COVERAGE_TARGET_RATE = 0.95

# 2026-08-15 主人拍板（同上 §2）：前向影子标签校准门 90% → 80%。点精度与
# Wilson 95% 下界是孪生阈值，必须同步下调，否则门实际仍卡在 90%。
SHADOW_CALIBRATION_ACTIVATION_CST = "2026-08-15T20:00:00+08:00"
SHADOW_LEGACY_TARGET_PRECISION = 0.90
SHADOW_TARGET_PRECISION = 0.80

# 2026-08-20 主人批准（《optimization_goal_v3.md》§3/§6/§7）：按有效
# ``analysis_runs.status='ok'`` 完成分布、live 业务终态、账实屏障和真实 Push
# ``sent`` 回执完成同批前向登记。激活边界取部署后的未来自然槽；历史 cycle
# 继续按原口径，不重算、不重判。
SLA_V3_REGISTRATION_ACTIVATION_CST = "2026-08-20T18:00:00+08:00"

# 2026-08-21 主人明确 §6 新口径：完整周期只含两道事实闸——必需采集完成，
# 以及分析+判断+交易完成；写库、报告、日志与 Push 均在 870 秒停表点之后独立
# 验收。边界取本批实际部署后的第一个整点，旧 cycle 保持 V3/V2 停表口径。
SLA_V4_PROCESS_SCOPE_ACTIVATION_CST = "2026-08-21T18:00:00+08:00"

# V2/V3 历史周期保留分析、业务终态、落记录+账实对账三段内部截止；V4 自前向
# 边界起把分析+判断+交易合并为严格 <870 秒的业务终态闸，落记录/对账改作停表
# 后独立收尾保护。以下 legacy 常量仍用于逐 cycle 解析旧口径，不得删除或回判。
SLA_ANALYSIS_DEADLINE_LEGACY_SECONDS = 9 * 60 + 30
SLA_ANALYSIS_DEADLINE_SECONDS = 10 * 60
SLA_BUSINESS_TERMINAL_DEADLINE_LEGACY_SECONDS = 13 * 60
SLA_BUSINESS_TERMINAL_DEADLINE_SECONDS = 13 * 60
SLA_RECORD_RECONCILE_DEADLINE_LEGACY_SECONDS = 14 * 60
SLA_RECORD_RECONCILE_DEADLINE_SECONDS = 14 * 60
COMPLETE_CYCLE_SLA_SECONDS = 14 * 60 + 30

# V4 只有一个 870 秒业务终态时间上界。采集是前置事实闸，不另拍秒数阈值；
# 分析、判断与交易共享严格 <870s 的业务终态闸。业务终态形成后，写回执/写库
# 获得独立 30 秒收尾窗；该窗以及后续报告、日志、Push 均不计入 §6 SLA。
SLA_V4_ANALYSIS_TRADE_DEADLINE_SECONDS = COMPLETE_CYCLE_SLA_SECONDS
SLA_V4_POST_BUSINESS_FINALIZE_DEADLINE_SECONDS = 15 * 60

# SLA 通过率 Tier-1 的历史登记。该档已自然成熟并达成；以下旧名继续作为
# Tier-1 兼容别名，供 Push 时效等独立契约保留既有最小样本，禁止删除或改写。
SLA_PASS_RATE_TIER_ACTIVATION_CST = SLA_V3_REGISTRATION_ACTIVATION_CST
SLA_V4_PASS_RATE_TIER_ACTIVATION_CST = SLA_V4_PROCESS_SCOPE_ACTIVATION_CST
SLA_PASS_RATE_TIER_INDEX = 1
SLA_PASS_RATE_TIER_TARGET = 0.80
SLA_PASS_RATE_MINIMUM_SLOTS = 96

# 2026-08-27 01:09:06 +08:00 主人明确批准下一档：Tier-2 目标>=90%，
# 最小样本192个自然15分钟槽（48小时），激活取批准后的下一个完整整点02:00。
# 严格<870s、两道事实闸、失败全留分母、禁止补派/重判历史等定义完全不变；
# 这里只新增前向业务通过率档位，不改变调度、模型、风控或交易行为。
SLA_PASS_RATE_TIER_2_DECISION_CST = "2026-08-27T01:09:06+08:00"
SLA_PASS_RATE_TIER_2_ACTIVATION_CST = "2026-08-27T02:00:00+08:00"
SLA_PASS_RATE_TIER_2_INDEX = 2
SLA_PASS_RATE_TIER_2_TARGET = 0.90
SLA_PASS_RATE_TIER_2_MINIMUM_SLOTS = 192

# 2026-08-27 08:04:13 +08:00 主人明确决定：由于 Memory Dreaming 保持
# 启用且当前不能并行，只把已经发生的 exact 2026-08-27T03:00 单槽从
# Tier-2 业务通过率验收分母排除。该决定不是每日 03:00 通配规则，不作用于
# 其它日期或其它指标；原始 failed cycle、失败分类、日志与全计划槽扫描全部保留。
# 这是观察结果后的用户批准单槽例外，审计必须同时外显 raw 与 adjusted 两套值。
SLA_PASS_RATE_TIER_2_EXCEPTION_DECISION_CST = (
    "2026-08-27T08:04:13+08:00")
SLA_PASS_RATE_TIER_2_EXCEPTION_CYCLES = ("2026-08-27T03:00",)
SLA_PASS_RATE_TIER_2_EXCEPTION_REASON = (
    "user_directed_one_off_memory_dreaming_no_parallel_solution")

# Push 时效从 live 报告账实屏障完成到 exact delivery ``sent`` 回执完成计时。
# 有效样本 p95=24 秒，固定 10% 余量后向上取整为 30 秒；比较符为 <=。
PUSH_DELIVERY_LATENCY_ACTIVATION_CST = SLA_V3_REGISTRATION_ACTIVATION_CST
PUSH_DELIVERY_LATENCY_TARGET_SECONDS = 30
PUSH_DELIVERY_LATENCY_COMPARISON = "<="

# 同标的结构化持仓动作只有在具备完整 symbol/action/reason 后才可作为显式复核；
# 该统计语义同样只从前向边界起生效。
STRUCTURED_POSITION_REVIEW_ACTIVATION_CST = SLA_V3_REGISTRATION_ACTIVATION_CST

# 既有 Push 同槽发布硬闸迁入单点事实源；旧激活边界与旧 cycle 口径不变。
PUSH_SAME_SLOT_ACTIVATION_CST = "2026-08-16T07:30:00+08:00"
PUSH_SAME_SLOT_LEGACY_MAX_AGE_SECONDS = 14 * 60
# V3 的第三道闸在 cycle+14:00 前完成落记录+账实对账；随后给已就绪的
# Push 完整 30 秒时效窗，仍严格留在同一 15 分钟自然槽内。
PUSH_SAME_SLOT_MAX_AGE_SECONDS = 14 * 60 + 30
POST_PUSH_MONITOR_LEGACY_DEADLINE_SECONDS = 14 * 60
POST_PUSH_MONITOR_DEADLINE_SECONDS = 15 * 60
PUSH_V4_MAX_AGE_SECONDS = 15 * 60 + 30
POST_PUSH_MONITOR_V4_DEADLINE_SECONDS = 16 * 60

# 2026-08-21 主人要求成熟后再追加一轮、两轮一致才登记。18:15 与 18:30
# 两份独立只读观测分别覆盖 96/97 个成熟 15 分钟槽，分布一致：1 槽会命中
# 30 个写方事件，2 槽保留全部 13 个多槽中断并过滤 17 个单槽波动，3 槽则会
# 漏掉 4 个持续两槽的中断。因此登记连续 2 个“本写方自身节奏”的成熟计划槽；
# 15 分钟写方相当于 30 分钟，slow 小时写方相当于 2 小时。只读、告警排查用，
# 不接入外部发送、调度或交易。激活取登记部署后的下一整点，历史不回判。
CRITICAL_OUTPUT_ZERO_STREAK_ACTIVATION_CST = "2026-08-21T19:00:00+08:00"
CRITICAL_OUTPUT_ZERO_STREAK_THRESHOLD_SLOTS = 2
CRITICAL_OUTPUT_ZERO_STREAK_COMPARISON = ">="

# 2026-08-26 主人明确重定义盈利验收：不再要求胜率、30日或100笔样本，
# 唯一业务结果门为每个完整周的交易净利润严格 >0；充值、提现与内部划转全部
# 排除。为避免把决定前的完整周按新规则重判，首个有约束力的事实窗从下一周一
# 08:00开始，与现行周报的七个日报窗口同相位。胜率、利润因子和回撤仅作诊断。
WEEKLY_TRADING_NET_PROFIT_DECISION_CST = "2026-08-26T14:26:06+08:00"
WEEKLY_TRADING_NET_PROFIT_ACTIVATION_CST = "2026-08-31T08:00:00+08:00"
WEEKLY_TRADING_NET_PROFIT_TARGET_USDT = 0.0
WEEKLY_TRADING_NET_PROFIT_COMPARISON = ">"

# 2026-08-31 19:32:24 +08:00 主人批准周报/日报/月报错失机会共享只读
# 证据合同。合同按报告事实窗右端 ``period_end`` 前向激活；边界前已生成的
# 报告继续使用 legacy 口径，不重算、不重判、不重渲染。激活后的报告只有
# ``COMPLETE`` 证据态可携带确定性 count；SOURCE_LAG/NO_DATA/ERROR 必须
# 保持证据草稿并禁止外发。
MISSED_OPPORTUNITY_EVIDENCE_DECISION_RECORDED_CST = (
    "2026-08-31T19:32:24+08:00")
MISSED_OPPORTUNITY_EVIDENCE_ACTIVATION_CST = (
    "2026-09-01T08:00:00+08:00")

# 2026-08-29 主人批准“16全筛查＋动态8深挖”。Shadow/consume 是两个
# 独立前向边界：shadow 只生成批量只读证据，Agent 继续逐币取证；只有24个
# 自然槽全部过门后才登记 consume。1696项全量回归通过后，shadow 边界登记为
# 留有超过30分钟部署/文档复核余量的未来整点；边界前所有cycle继续旧逐币路径。
CANDIDATE_BUNDLE_SHADOW_ACTIVATION_CST: str | None = (
    "2026-08-29T10:00:00+08:00")
CANDIDATE_BUNDLE_CONSUME_ACTIVATION_CST: str | None = None
CANDIDATE_BUNDLE_CONSUME_END_CST: str | None = None
CANDIDATE_BUNDLE_SHADOW_MINIMUM_SLOTS = 24
CANDIDATE_BUNDLE_CONSUME_MINIMUM_SLOTS = 96
CANDIDATE_SCREENING_TARGET_RATE = 1.0
CANDIDATE_BUNDLE_CONTRACT_PARITY_TARGET_RATE = 1.0
CANDIDATE_STRUCTURE_VALID_TARGET_RATE = 0.99
CANDIDATE_DYNAMIC_TARGET_UTILIZATION_TARGET_RATE = 0.90
CANDIDATE_ROTATION_COMPLIANCE_TARGET_RATE = 1.0
CANDIDATE_CONSUME_STRICT_CYCLE_PASS_TARGET_RATE = 0.98
CANDIDATE_BUNDLE_P90_TARGET_SECONDS = 8
CANDIDATE_BUNDLE_TIMEOUT_SECONDS = 12
CANDIDATE_DEEP_DIVE_MINIMUM = 3
CANDIDATE_DEEP_DIVE_MAXIMUM = 8
CANDIDATE_SCREENING_MAXIMUM = 16
CANDIDATE_CONSUME_SLOT_P90_MAX_SECONDS = {
    "00": 587,
    "15": 481,
    "30": 566,
    "45": 528,
}

# 2026-09-01 用户批准不开仓漏斗修复。状态/排序从首个自然验证槽起只向前
# 生效；exact identity OPEN闸、分侧软否决影子和96槽零OPEN watchdog留出
# 完整测试与文档部署窗口，自04:00起启用。历史cycle不回写、不重判。
CANDIDATE_OPPORTUNITY_STATE_ACTIVATION_CST = "2026-09-01T01:30:00+08:00"
CANDIDATE_OPPORTUNITY_STATE_V2_ACTIVATION_CST = "2026-09-01T02:45:00+08:00"
CANDIDATE_EXACT_IDENTITY_ENFORCEMENT_CST = "2026-09-01T04:00:00+08:00"
SIDE_REGIME_SOFT_VETO_SHADOW_ACTIVATION_CST = "2026-09-01T04:00:00+08:00"
ZERO_OPEN_WATCHDOG_ACTIVATION_CST = "2026-09-01T04:00:00+08:00"
ZERO_OPEN_WATCHDOG_THRESHOLD_SLOTS = 96

# 2026-09-01 主人明确批准撤销候选成交额/OI/4H方向/8+8/16项、OPEN数量、
# exact candidate identity、candidate-quality consume过滤、reason-family归一、
# OPEN MTF/history重卡与executor MTF复验。代码先以 None 落地并完成全量回归；
# 只有部署完成后登记的下一自然槽才前向生效，历史cycle不回写、不重判。
DECISION_RESTRICTION_REMOVAL_ACTIVATION_CST: str | None = (
    "2026-09-02T00:15:00+08:00")
# 00:15 prompt was already sealed before the final four-state/removed-volume-veto
# patch landed. Natural acceptance therefore starts at the next exact slot; the
# policy itself remains forward-active from 00:15 and no historical row is changed.
DECISION_RESTRICTION_REMOVAL_VALIDATION_CST = "2026-09-02T01:45:00+08:00"
DECISION_RESTRICTION_REMOVAL_POLICY = "all_market_lightweight_open_v1"

# 2026-09-02 主人继续明确删除“三周期判断”和“六项决策卡”。实现先以前向
# disabled 边界落地；全量回归、部署同步和控制轮通过后，才登记下一自然槽。
# OPEN 仍保留 entry/stop/target/exit_mode 这一机器执行包，因为 SL/TP 是真钱
# 安全输入，不属于六项展示卡。账户/账仓/intent/risk/fill/protection 硬闸不变。
MINIMAL_DECISION_CONTRACT_ACTIVATION_CST: str | None = (
    "2026-09-02T13:15:00+08:00")
MINIMAL_DECISION_CONTRACT_POLICY = "no_three_period_no_six_card_v1"
# 2026-09-02 owner-ordered full closure of the residual lightweight-package,
# prompt, facts handoff, exact-slice, single-candidate MTF, and review-ranking
# defects.  Land disabled; register the next natural slot only after backup,
# full regression, deployment readback, and isolated OPEN/HOLD validation.
MINIMAL_CONTRACT_CLOSURE_ACTIVATION_CST: str | None = "2026-09-02T20:00:00+08:00"
MINIMAL_CONTRACT_CLOSURE_POLICY = "minimal_contract_full_closure_v1"
# Input-unit preflight only; it reuses the existing SL distance hard limit.
# Register a future natural slot after isolated validation. No historical rewrite.
OPEN_PRICE_PREFLIGHT_ACTIVATION_CST: str | None = "2026-09-07T18:30:00+08:00"
RELAXED_MANIFEST_SANITY_MAXIMUM = 2048
RELAXED_DECISION_SLICE_MAXIMUM = CANDIDATE_DEEP_DIVE_MAXIMUM
RELAXED_CANDIDATE_BUNDLE_TIMEOUT_SECONDS = 45
RELAXED_BASIC_CONFIRMATION_SLOTS = 3
RELAXED_ZERO_OPEN_WATCHDOG_THRESHOLD_SLOTS = 12


def decision_restriction_removal_active(as_of: str | datetime) -> bool:
    activation = DECISION_RESTRICTION_REMOVAL_ACTIVATION_CST
    if not activation:
        return False
    try:
        return parse_cst(as_of) >= parse_cst(str(activation))
    except (TypeError, ValueError):
        return False


def minimal_decision_contract_active(as_of: str | datetime) -> bool:
    activation = MINIMAL_DECISION_CONTRACT_ACTIVATION_CST
    if not activation:
        return False
    try:
        return parse_cst(as_of) >= parse_cst(str(activation))
    except (TypeError, ValueError):
        return False


def minimal_contract_closure_active(as_of: str | datetime) -> bool:
    activation = MINIMAL_CONTRACT_CLOSURE_ACTIVATION_CST
    if not activation:
        return False
    try:
        return parse_cst(as_of) >= parse_cst(str(activation))
    except (TypeError, ValueError):
        return False


def open_price_preflight_active(as_of: str | datetime) -> bool:
    activation = OPEN_PRICE_PREFLIGHT_ACTIVATION_CST
    if not activation or not minimal_contract_closure_active(as_of):
        return False
    try:
        return parse_cst(as_of) >= parse_cst(activation)
    except (TypeError, ValueError):
        return False


def three_period_judgment_required(as_of: str | datetime) -> bool:
    return not minimal_decision_contract_active(as_of)


def six_field_decision_card_required(as_of: str | datetime) -> bool:
    return not minimal_decision_contract_active(as_of)


def candidate_manifest_maximum(as_of: str | datetime) -> int:
    return (
        RELAXED_MANIFEST_SANITY_MAXIMUM
        if decision_restriction_removal_active(as_of)
        else CANDIDATE_SCREENING_MAXIMUM
    )


def candidate_bundle_timeout_seconds(as_of: str | datetime) -> int:
    return (
        RELAXED_CANDIDATE_BUNDLE_TIMEOUT_SECONDS
        if decision_restriction_removal_active(as_of)
        else CANDIDATE_BUNDLE_TIMEOUT_SECONDS
    )


def open_multitimeframe_contract_required(as_of: str | datetime) -> bool:
    return not decision_restriction_removal_active(as_of)


def zero_open_watchdog_activation_cycle(as_of: str | datetime) -> str:
    if minimal_contract_closure_active(as_of):
        return str(MINIMAL_CONTRACT_CLOSURE_ACTIVATION_CST)[:16]
    if minimal_decision_contract_active(as_of):
        return str(MINIMAL_DECISION_CONTRACT_ACTIVATION_CST)[:16]
    if decision_restriction_removal_active(as_of):
        return str(DECISION_RESTRICTION_REMOVAL_ACTIVATION_CST)[:16]
    return str(ZERO_OPEN_WATCHDOG_ACTIVATION_CST)[:16]


def zero_open_watchdog_threshold_slots(as_of: str | datetime) -> int:
    return (
        RELAXED_ZERO_OPEN_WATCHDOG_THRESHOLD_SLOTS
        if decision_restriction_removal_active(as_of)
        else ZERO_OPEN_WATCHDOG_THRESHOLD_SLOTS
    )


def candidate_exact_identity_enforced(as_of: str | datetime) -> bool:
    return (
        not decision_restriction_removal_active(as_of)
        and parse_cst(as_of) >= parse_cst(
            CANDIDATE_EXACT_IDENTITY_ENFORCEMENT_CST)
    )


def candidate_funnel_repair_active(as_of: str | datetime) -> bool:
    return parse_cst(as_of) >= parse_cst(
        CANDIDATE_OPPORTUNITY_STATE_ACTIVATION_CST)


def side_regime_soft_veto_shadow_active(as_of: str | datetime) -> bool:
    return parse_cst(as_of) >= parse_cst(
        SIDE_REGIME_SOFT_VETO_SHADOW_ACTIVATION_CST)


def zero_open_watchdog_active(as_of: str | datetime) -> bool:
    return parse_cst(as_of) >= parse_cst(
        ZERO_OPEN_WATCHDOG_ACTIVATION_CST)


def parse_cst(value: str | datetime) -> datetime:
    """Parse a CST timestamp; naive input is interpreted as Beijing time."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def missed_opportunity_evidence_contract_active(
    period_end: str | datetime,
) -> bool:
    """Whether a report period end uses the forward evidence contract."""
    return parse_cst(period_end) >= parse_cst(
        MISSED_OPPORTUNITY_EVIDENCE_ACTIVATION_CST)


def missed_opportunity_evidence_registration_facts(
    period_end: str | datetime,
) -> dict[str, Any]:
    """Return the immutable forward registration for report consumers."""
    parsed_end = parse_cst(period_end)
    return {
        "decision_recorded": MISSED_OPPORTUNITY_EVIDENCE_DECISION_RECORDED_CST,
        "activation_cst": MISSED_OPPORTUNITY_EVIDENCE_ACTIVATION_CST,
        "period_end_cst": parsed_end.isoformat(),
        "comparison": ">=",
        "active": missed_opportunity_evidence_contract_active(parsed_end),
        "legacy_reports_rejudged": False,
    }


def candidate_bundle_phase(as_of: str | datetime) -> str:
    """Return off/shadow/consume/rollback for one exact natural cycle."""
    if decision_restriction_removal_active(as_of):
        return "consume"
    if CANDIDATE_BUNDLE_SHADOW_ACTIVATION_CST is None:
        return "off"
    moment = parse_cst(as_of)
    if moment < parse_cst(CANDIDATE_BUNDLE_SHADOW_ACTIVATION_CST):
        return "off"
    if CANDIDATE_BUNDLE_CONSUME_ACTIVATION_CST is None:
        return "shadow"
    if moment < parse_cst(CANDIDATE_BUNDLE_CONSUME_ACTIVATION_CST):
        return "shadow"
    if (
        CANDIDATE_BUNDLE_CONSUME_END_CST is not None
        and moment >= parse_cst(CANDIDATE_BUNDLE_CONSUME_END_CST)
    ):
        return "rollback"
    return "consume"


def candidate_bundle_registration_facts(
    as_of: str | datetime,
) -> dict[str, Any]:
    """Auditable forward-only registration for candidate batch evidence."""
    runtime_phase = candidate_bundle_phase(as_of)
    minimal_policy = minimal_decision_contract_active(as_of)
    closure_policy = minimal_contract_closure_active(as_of)
    phase = "minimal" if minimal_policy else runtime_phase
    legacy_shadow_gates = {
        "screening_coverage_rate": CANDIDATE_SCREENING_TARGET_RATE,
        "contract_parity_rate": CANDIDATE_BUNDLE_CONTRACT_PARITY_TARGET_RATE,
        "hash_cycle_order_consistency_rate": 1.0,
        "bundle_p90_seconds": CANDIDATE_BUNDLE_P90_TARGET_SECONDS,
        "bundle_hard_timeout_seconds": candidate_bundle_timeout_seconds(
            as_of),
    }
    legacy_consume_gates = {
        "screening_coverage_rate": CANDIDATE_SCREENING_TARGET_RATE,
        "structure_valid_deep_dive_rate": (
            CANDIDATE_STRUCTURE_VALID_TARGET_RATE),
        "average_dynamic_target_utilization_rate": (
            CANDIDATE_DYNAMIC_TARGET_UTILIZATION_TARGET_RATE),
        "rotation_compliance_rate": CANDIDATE_ROTATION_COMPLIANCE_TARGET_RATE,
        "strict_cycle_pass_rate": (
            CANDIDATE_CONSUME_STRICT_CYCLE_PASS_TARGET_RATE),
        "slot_business_terminal_p90_max_seconds": dict(
            CANDIDATE_CONSUME_SLOT_P90_MAX_SECONDS),
        "new_candidate_bundle_failure_types_allowed": 0,
    }
    return {
        "status": (
            "UNREGISTERED" if CANDIDATE_BUNDLE_SHADOW_ACTIVATION_CST is None
            else "REGISTERED_FORWARD_ONLY"
        ),
        "phase": phase,
        "runtime_bundle_phase": runtime_phase,
        "shadow_activation_cst": CANDIDATE_BUNDLE_SHADOW_ACTIVATION_CST,
        "consume_activation_cst": CANDIDATE_BUNDLE_CONSUME_ACTIVATION_CST,
        "consume_end_cst": CANDIDATE_BUNDLE_CONSUME_END_CST,
        "shadow_minimum_natural_slots": CANDIDATE_BUNDLE_SHADOW_MINIMUM_SLOTS,
        "consume_minimum_natural_slots": CANDIDATE_BUNDLE_CONSUME_MINIMUM_SLOTS,
        "screening_maximum_candidates": (
            None if decision_restriction_removal_active(as_of)
            else CANDIDATE_SCREENING_MAXIMUM),
        "manifest_sanity_maximum": candidate_manifest_maximum(as_of),
        "decision_slice_maximum": (
            RELAXED_DECISION_SLICE_MAXIMUM
            if decision_restriction_removal_active(as_of) else None),
        "owner_approved_restriction_removal": (
            decision_restriction_removal_active(as_of)),
        "decision_policy": (
            MINIMAL_CONTRACT_CLOSURE_POLICY if closure_policy else
            MINIMAL_DECISION_CONTRACT_POLICY if minimal_policy else
            DECISION_RESTRICTION_REMOVAL_POLICY
            if decision_restriction_removal_active(as_of) else "legacy"),
        "minimal_contract_closure_activation_cst": (
            MINIMAL_CONTRACT_CLOSURE_ACTIVATION_CST),
        "minimal_contract_closure_active": closure_policy,
        "three_period_judgment_required": not minimal_policy,
        "four_state_judgment_required": not minimal_policy,
        "six_field_decision_card_required": not minimal_policy,
        "deep_dive_range": (
            None if minimal_policy else [
                CANDIDATE_DEEP_DIVE_MINIMUM, CANDIDATE_DEEP_DIVE_MAXIMUM]),
        "review_range": (
            [min(RELAXED_BASIC_CONFIRMATION_SLOTS,
                 RELAXED_DECISION_SLICE_MAXIMUM),
             RELAXED_DECISION_SLICE_MAXIMUM]
            if minimal_policy else None),
        "shadow_gates": None if minimal_policy else legacy_shadow_gates,
        "consume_gates": None if minimal_policy else legacy_consume_gates,
        "minimal_policy_gates": ({
            "basic_confirmation_natural_slots": RELAXED_BASIC_CONFIRMATION_SLOTS,
            "full_confirmation_natural_slots": (
                RELAXED_ZERO_OPEN_WATCHDOG_THRESHOLD_SLOTS),
            "side_neutral_manifest_and_bundle_required": True,
            "full_market_screening_rate": 1.0,
            "timeframe_judgment_used": False,
            "four_state_judgment_used": False,
            "six_field_decision_card_used": False,
            "open_execution_package_key": (
                "open_execution_package" if closure_policy else
                "decision_card_legacy_storage"),
            "full_manifest_open_allowed": closure_policy,
            "opportunity_priority_plus_rotation": closure_policy,
        } if minimal_policy else None),
        "legacy_registration": ({
            "phase_at_boundary": runtime_phase,
            "shadow_gates": legacy_shadow_gates,
            "consume_gates": legacy_consume_gates,
            "acceptance_effect_after_minimal_activation": "none",
            "historical_rejudgement": False,
        } if minimal_policy else None),
        "required_collection_gate_changed": False,
        "complete_cycle_deadline_changed": False,
        "model_or_provider_changed": False,
        "scheduler_changed": False,
        "risk_or_trade_quota_changed": False,
        "historical_rejudgement": False,
        "consume_requires_completed_shadow_gate": not minimal_policy,
    }


def _resolve(
    as_of: str | datetime,
    *,
    activation_cst: str,
    legacy_value: float,
    migrated_value: float,
) -> float:
    """Return the threshold in force for a run performed at ``as_of``."""
    return (
        migrated_value
        if parse_cst(as_of) >= parse_cst(activation_cst)
        else legacy_value
    )


def coverage_target_rate(as_of: str | datetime) -> float:
    """完善率/完整度四族闸门：激活边界起 0.95，之前仍是 0.99。"""
    return _resolve(
        as_of,
        activation_cst=COVERAGE_TARGET_ACTIVATION_CST,
        legacy_value=COVERAGE_LEGACY_TARGET_RATE,
        migrated_value=COVERAGE_TARGET_RATE,
    )


def shadow_target_precision(as_of: str | datetime) -> float:
    """前向影子标签校准门（点精度与 Wilson 下界共用同一数值）。"""
    return _resolve(
        as_of,
        activation_cst=SHADOW_CALIBRATION_ACTIVATION_CST,
        legacy_value=SHADOW_LEGACY_TARGET_PRECISION,
        migrated_value=SHADOW_TARGET_PRECISION,
    )


def _resolve_int(
    as_of: str | datetime,
    *,
    activation_cst: str,
    legacy_value: int,
    migrated_value: int,
) -> int:
    return int(_resolve(
        as_of,
        activation_cst=activation_cst,
        legacy_value=float(legacy_value),
        migrated_value=float(migrated_value),
    ))


def sla_analysis_deadline_seconds(as_of: str | datetime) -> int:
    """Cycle-relative analysis freeze deadline in force for ``as_of``."""
    if parse_cst(as_of) >= parse_cst(SLA_V4_PROCESS_SCOPE_ACTIVATION_CST):
        return SLA_V4_ANALYSIS_TRADE_DEADLINE_SECONDS
    return _resolve_int(
        as_of,
        activation_cst=SLA_V3_REGISTRATION_ACTIVATION_CST,
        legacy_value=SLA_ANALYSIS_DEADLINE_LEGACY_SECONDS,
        migrated_value=SLA_ANALYSIS_DEADLINE_SECONDS,
    )


def sla_business_terminal_deadline_seconds(as_of: str | datetime) -> int:
    """Cycle-relative Agent business-terminal deadline."""
    if parse_cst(as_of) >= parse_cst(SLA_V4_PROCESS_SCOPE_ACTIVATION_CST):
        return SLA_V4_ANALYSIS_TRADE_DEADLINE_SECONDS
    return _resolve_int(
        as_of,
        activation_cst=SLA_V3_REGISTRATION_ACTIVATION_CST,
        legacy_value=SLA_BUSINESS_TERMINAL_DEADLINE_LEGACY_SECONDS,
        migrated_value=SLA_BUSINESS_TERMINAL_DEADLINE_SECONDS,
    )


def sla_record_reconcile_deadline_seconds(as_of: str | datetime) -> int:
    """Cycle-relative post-business persistence/reconcile guard."""
    if parse_cst(as_of) >= parse_cst(SLA_V4_PROCESS_SCOPE_ACTIVATION_CST):
        return SLA_V4_POST_BUSINESS_FINALIZE_DEADLINE_SECONDS
    return _resolve_int(
        as_of,
        activation_cst=SLA_V3_REGISTRATION_ACTIVATION_CST,
        legacy_value=SLA_RECORD_RECONCILE_DEADLINE_LEGACY_SECONDS,
        migrated_value=SLA_RECORD_RECONCILE_DEADLINE_SECONDS,
    )


def complete_cycle_uses_record_reconcile_stop(as_of: str | datetime) -> bool:
    """Whether §6 measures the live report barrier instead of post-Push monitor."""
    moment = parse_cst(as_of)
    return (
        parse_cst(SLA_V3_REGISTRATION_ACTIVATION_CST)
        <= moment
        < parse_cst(SLA_V4_PROCESS_SCOPE_ACTIVATION_CST)
    )


def complete_cycle_uses_business_terminal_stop(
    as_of: str | datetime,
) -> bool:
    """Whether §6 stops before persistence at the V4 business terminal."""
    return parse_cst(as_of) >= parse_cst(
        SLA_V4_PROCESS_SCOPE_ACTIVATION_CST)


def sla_pass_rate_window_activation_cst(
    as_of: str | datetime,
) -> str:
    """Return the forward-window boundary for the active SLA definition."""
    if parse_cst(as_of) >= parse_cst(SLA_PASS_RATE_TIER_2_ACTIVATION_CST):
        return SLA_PASS_RATE_TIER_2_ACTIVATION_CST
    return (
        SLA_V4_PASS_RATE_TIER_ACTIVATION_CST
        if complete_cycle_uses_business_terminal_stop(as_of)
        else SLA_PASS_RATE_TIER_ACTIVATION_CST
    )


def sla_pass_rate_target(as_of: str | datetime) -> float | None:
    """Return the active pre-registered SLA pass-rate target."""
    if parse_cst(as_of) < parse_cst(SLA_PASS_RATE_TIER_ACTIVATION_CST):
        return None
    if parse_cst(as_of) >= parse_cst(SLA_PASS_RATE_TIER_2_ACTIVATION_CST):
        return SLA_PASS_RATE_TIER_2_TARGET
    return SLA_PASS_RATE_TIER_TARGET


def sla_pass_rate_tier_index(as_of: str | datetime) -> int:
    """Return the active SLA pass-rate tier index."""
    if parse_cst(as_of) >= parse_cst(SLA_PASS_RATE_TIER_2_ACTIVATION_CST):
        return SLA_PASS_RATE_TIER_2_INDEX
    return SLA_PASS_RATE_TIER_INDEX


def sla_pass_rate_minimum_slots(as_of: str | datetime) -> int:
    """Return the active tier's minimum mature natural-slot sample."""
    if parse_cst(as_of) >= parse_cst(SLA_PASS_RATE_TIER_2_ACTIVATION_CST):
        return SLA_PASS_RATE_TIER_2_MINIMUM_SLOTS
    return SLA_PASS_RATE_MINIMUM_SLOTS


def sla_pass_rate_acceptance_exceptions(
    as_of: str | datetime,
) -> list[dict[str, Any]]:
    """Return exact user-approved cycle exceptions active for this audit.

    Exceptions alter only the active Tier-2 acceptance denominator.  Raw
    cycle facts, failure taxonomy and all-plan diagnostics remain untouched.
    """
    if parse_cst(as_of) < parse_cst(
            SLA_PASS_RATE_TIER_2_EXCEPTION_DECISION_CST):
        return []
    return [{
        "tier": SLA_PASS_RATE_TIER_2_INDEX,
        "cycle_id": cycle_id,
        "decision_cst": SLA_PASS_RATE_TIER_2_EXCEPTION_DECISION_CST,
        "reason": SLA_PASS_RATE_TIER_2_EXCEPTION_REASON,
        "scope": "tier2_acceptance_denominator_only",
        "user_approved": True,
        "post_observation": True,
        "raw_cycle_fact_preserved": True,
        "recurring": False,
        "future_daily_0300_excluded": False,
    } for cycle_id in SLA_PASS_RATE_TIER_2_EXCEPTION_CYCLES]


def sla_pass_rate_acceptance_exception_for_cycle(
    cycle_id: str,
    as_of: str | datetime,
) -> dict[str, Any] | None:
    """Return the exact exception for ``cycle_id``; never pattern-match."""
    normalized = parse_cst(cycle_id).strftime("%Y-%m-%dT%H:%M")
    for exception in sla_pass_rate_acceptance_exceptions(as_of):
        if exception["cycle_id"] == normalized:
            return exception
    return None


def sla_pass_rate_next_tier_registration(
    as_of: str | datetime,
) -> dict[str, Any] | None:
    """Return a registered future tier awaiting activation, if any."""
    moment = parse_cst(as_of)
    if not (
        parse_cst(SLA_PASS_RATE_TIER_2_DECISION_CST)
        <= moment
        < parse_cst(SLA_PASS_RATE_TIER_2_ACTIVATION_CST)
    ):
        return None
    return {
        "tier": SLA_PASS_RATE_TIER_2_INDEX,
        "decision_cst": SLA_PASS_RATE_TIER_2_DECISION_CST,
        "activation_cst": SLA_PASS_RATE_TIER_2_ACTIVATION_CST,
        "target_rate": SLA_PASS_RATE_TIER_2_TARGET,
        "minimum_slots": SLA_PASS_RATE_TIER_2_MINIMUM_SLOTS,
        "status": "REGISTERED_PENDING_ACTIVATION",
        "historical_rejudgement": False,
    }


def sla_pass_rate_prior_tier_registrations(
    as_of: str | datetime,
) -> list[dict[str, Any]]:
    """Return closed prior-tier definitions retained without rejudgement."""
    if parse_cst(as_of) < parse_cst(SLA_PASS_RATE_TIER_2_ACTIVATION_CST):
        return []
    return [{
        "tier": SLA_PASS_RATE_TIER_INDEX,
        "activation_cst": SLA_V4_PASS_RATE_TIER_ACTIVATION_CST,
        "end_exclusive_cst": SLA_PASS_RATE_TIER_2_ACTIVATION_CST,
        "target_rate": SLA_PASS_RATE_TIER_TARGET,
        "minimum_slots": SLA_PASS_RATE_MINIMUM_SLOTS,
        "closed_by_tier": SLA_PASS_RATE_TIER_2_INDEX,
        "historical_rejudgement": False,
    }]


def push_delivery_latency_target_seconds(
    as_of: str | datetime,
) -> int | None:
    """Return the active Push delivery-latency target, or ``None`` before it."""
    if parse_cst(as_of) < parse_cst(PUSH_DELIVERY_LATENCY_ACTIVATION_CST):
        return None
    return PUSH_DELIVERY_LATENCY_TARGET_SECONDS


def push_same_slot_max_age_seconds(as_of: str | datetime) -> int:
    """Latest strict same-slot age at which a new Push may still run."""
    if parse_cst(as_of) >= parse_cst(SLA_V4_PROCESS_SCOPE_ACTIVATION_CST):
        return PUSH_V4_MAX_AGE_SECONDS
    return _resolve_int(
        as_of,
        activation_cst=SLA_V3_REGISTRATION_ACTIVATION_CST,
        legacy_value=PUSH_SAME_SLOT_LEGACY_MAX_AGE_SECONDS,
        migrated_value=PUSH_SAME_SLOT_MAX_AGE_SECONDS,
    )


def post_push_monitor_deadline_seconds(as_of: str | datetime) -> int:
    """Independent post-Push monitor deadline; exact next slot is late."""
    if parse_cst(as_of) >= parse_cst(SLA_V4_PROCESS_SCOPE_ACTIVATION_CST):
        return POST_PUSH_MONITOR_V4_DEADLINE_SECONDS
    return _resolve_int(
        as_of,
        activation_cst=SLA_V3_REGISTRATION_ACTIVATION_CST,
        legacy_value=POST_PUSH_MONITOR_LEGACY_DEADLINE_SECONDS,
        migrated_value=POST_PUSH_MONITOR_DEADLINE_SECONDS,
    )


def structured_position_actions_count_as_review(
    as_of: str | datetime,
) -> bool:
    """Enable the forward-only structured position-action review semantic."""
    return parse_cst(as_of) >= parse_cst(
        STRUCTURED_POSITION_REVIEW_ACTIVATION_CST)


def critical_output_zero_streak_threshold_slots(
    as_of: str | datetime,
) -> int | None:
    """Return the active per-writer zero-output streak threshold."""
    if parse_cst(as_of) < parse_cst(
        CRITICAL_OUTPUT_ZERO_STREAK_ACTIVATION_CST
    ):
        return None
    return CRITICAL_OUTPUT_ZERO_STREAK_THRESHOLD_SLOTS


def critical_output_zero_streak_registration_facts(
    as_of: str | datetime,
) -> dict[str, Any]:
    """Auditable forward-only registration for silent-output observation."""
    effective = critical_output_zero_streak_threshold_slots(as_of)
    return {
        "status": "REGISTERED_FORWARD_ONLY",
        "activation_cst": CRITICAL_OUTPUT_ZERO_STREAK_ACTIVATION_CST,
        "activated": effective is not None,
        "alert_threshold_slots": CRITICAL_OUTPUT_ZERO_STREAK_THRESHOLD_SLOTS,
        "effective_alert_threshold_slots": effective,
        "comparison": CRITICAL_OUTPUT_ZERO_STREAK_COMPARISON,
        "cadence_semantics": "each_writer_own_expected_slot_cadence",
        "historical_rejudgement": False,
        "external_alert_wiring": False,
        "scheduler_authority": False,
        "trading_authority": False,
        "calibration_observations": [
            "2026-08-21T18:15:00+08:00/96-quarter-slots",
            "2026-08-21T18:30:00+08:00/97-quarter-slots",
        ],
    }


def sla_v3_registration_facts(as_of: str | datetime) -> dict[str, Any]:
    """Auditable facts for the forward SLA definition in force."""
    business_stop = complete_cycle_uses_business_terminal_stop(as_of)
    record_stop = complete_cycle_uses_record_reconcile_stop(as_of)
    activated = record_stop or business_stop
    next_tier = sla_pass_rate_next_tier_registration(as_of)
    acceptance_exceptions = sla_pass_rate_acceptance_exceptions(as_of)
    return {
        "activation_cst": SLA_V3_REGISTRATION_ACTIVATION_CST,
        "process_scope_activation_cst": SLA_V4_PROCESS_SCOPE_ACTIVATION_CST,
        "activated": activated,
        "analysis_deadline_seconds": sla_analysis_deadline_seconds(as_of),
        "business_terminal_deadline_seconds": (
            sla_business_terminal_deadline_seconds(as_of)),
        "record_reconcile_deadline_seconds": (
            sla_record_reconcile_deadline_seconds(as_of)),
        "push_same_slot_max_age_seconds": (
            push_same_slot_max_age_seconds(as_of)),
        "post_push_monitor_deadline_seconds": (
            post_push_monitor_deadline_seconds(as_of)),
        "complete_cycle_sla_seconds": COMPLETE_CYCLE_SLA_SECONDS,
        "complete_cycle_comparison": "<",
        "clock_stop": (
            "successful_analysis_judgment_trade_terminal_at"
            if business_stop
            else "successful_live_report_reconcile_barrier_finished_at"
            if record_stop
            else "successful_clean_post_live_reconcile_timestamp"
        ),
        "stage_gates": (
            [
                "required_collection_sources_completed",
                "analysis_judgment_trade_completed",
            ]
            if business_stop else [
                "analysis_completed",
                "live_business_terminal_committed",
                "record_reconcile_completed",
            ]
        ),
        "excluded_from_870_seconds": (
            [
                "receipt_file_write",
                "business_database_commit",
                "report_build_validate_archive",
                "log_write",
                "push_delivery",
                "post_push_monitor",
            ]
            if business_stop else ["push_delivery"]
        ),
        "post_business_finalize_deadline_seconds": (
            SLA_V4_POST_BUSINESS_FINALIZE_DEADLINE_SECONDS
            if business_stop else None
        ),
        "pass_rate_tier": {
            "tier": sla_pass_rate_tier_index(as_of),
            "target_rate": sla_pass_rate_target(as_of),
            "minimum_slots": sla_pass_rate_minimum_slots(as_of),
            "activation_cst": sla_pass_rate_window_activation_cst(as_of),
            "next_tier_registered": next_tier is not None,
            "next_tier": next_tier,
            "prior_tier_registrations": (
                sla_pass_rate_prior_tier_registrations(as_of)),
            "decision_cst": (
                SLA_PASS_RATE_TIER_2_DECISION_CST
                if sla_pass_rate_tier_index(as_of)
                == SLA_PASS_RATE_TIER_2_INDEX else None),
            "acceptance_exceptions": acceptance_exceptions,
            "historical_rejudgement": bool(acceptance_exceptions),
            "historical_rejudgement_scope": [
                item["cycle_id"] for item in acceptance_exceptions],
            "raw_cycle_facts_preserved": True,
        },
        "semantics": (
            "forward_only gates and clock stops remain unchanged; exact "
            "post-observation user exceptions listed in pass_rate_tier alter "
            "only that acceptance denominator while raw cycle facts stay "
            "preserved"
            if acceptance_exceptions else
            "forward_only; old cycles keep their original gates and clock stop; "
            "archived evidence is never recomputed or re-judged"
        ),
    }


def push_latency_registration_facts(as_of: str | datetime) -> dict[str, Any]:
    """Auditable facts for §3 record-complete to exact-delivery latency."""
    target = push_delivery_latency_target_seconds(as_of)
    return {
        "activation_cst": PUSH_DELIVERY_LATENCY_ACTIVATION_CST,
        "activated": target is not None,
        "target_seconds": target,
        "comparison": PUSH_DELIVERY_LATENCY_COMPARISON,
        "clock_start": "successful_live_report_reconcile_barrier_finished_at",
        "clock_stop": "exact_push_delivery_sent_receipt_updated_at",
        "required_pass_rate": coverage_target_rate(as_of),
        "minimum_slots": SLA_PASS_RATE_MINIMUM_SLOTS,
        "historical_rejudgement": False,
    }


def structured_position_review_migration_facts(
    as_of: str | datetime,
) -> dict[str, Any]:
    """Auditable facts for the forward-only explicit-review semantic."""
    activated = structured_position_actions_count_as_review(as_of)
    return {
        "activation_cst": STRUCTURED_POSITION_REVIEW_ACTIVATION_CST,
        "activated": activated,
        "structured_position_action_counts_as_review": activated,
        "required_fields": ["same_full_symbol", "action", "reason"],
        "historical_rejudgement": False,
    }


def coverage_migration_facts(as_of: str | datetime) -> dict[str, Any]:
    """Payload block proving which caliber judged this run, and why."""
    return {
        "activation_cst": COVERAGE_TARGET_ACTIVATION_CST,
        "activated": (
            parse_cst(as_of) >= parse_cst(COVERAGE_TARGET_ACTIVATION_CST)),
        "legacy_target_rate": COVERAGE_LEGACY_TARGET_RATE,
        "migrated_target_rate": COVERAGE_TARGET_RATE,
        "effective_target_rate": coverage_target_rate(as_of),
        "semantics": (
            "forward_only; runs before the boundary keep the legacy caliber; "
            "archived evidence is never recomputed or re-judged"
        ),
    }


def shadow_migration_facts(as_of: str | datetime) -> dict[str, Any]:
    """Payload block for the forward calibration gate migration."""
    return {
        "activation_cst": SHADOW_CALIBRATION_ACTIVATION_CST,
        "activated": (
            parse_cst(as_of)
            >= parse_cst(SHADOW_CALIBRATION_ACTIVATION_CST)),
        "legacy_target_precision": SHADOW_LEGACY_TARGET_PRECISION,
        "migrated_target_precision": SHADOW_TARGET_PRECISION,
        "effective_target_precision": shadow_target_precision(as_of),
        "twin_thresholds": (
            "point precision and Wilson 95% lower bound move together"
        ),
        "semantics": (
            "forward_only; passing the gate stays "
            "MET_FORWARD_SHADOW_REQUIRES_RISK_APPROVAL"
        ),
    }


def legacy_rate_diagnostics(
    rates: Mapping[str, Any],
    *,
    target_dependent: Iterable[str] = (),
) -> dict[str, Any]:
    """按老口径 0.99 复判已发布的率，作诊断列，不参与闸门。

    ``target_dependent`` 列出那些自身计算就依赖当次阈值的率（例如逐槽
    ``slot_pass_rate``）：它们在新旧口径下不同源，拿去和 0.99 比会是苹果对橘
    子，因此显式排除并点名，而不是悄悄照比。
    """
    excluded = sorted({str(name) for name in target_dependent})
    comparable = {
        str(name): (
            None if value is None else bool(float(value) >= COVERAGE_LEGACY_TARGET_RATE)
        )
        for name, value in rates.items()
        if str(name) not in set(excluded)
    }
    return {
        "legacy_target_rate": COVERAGE_LEGACY_TARGET_RATE,
        "rates_at_least_legacy_target": comparable,
        "all_comparable_rates_at_least_legacy_target": (
            all(value is True for value in comparable.values())
            if comparable else None
        ),
        "target_dependent_rates_excluded": excluded,
        "diagnostic_only": True,
    }
