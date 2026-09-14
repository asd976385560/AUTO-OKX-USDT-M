# -*- coding: utf-8 -*-
"""告警/战报展示层中文标签单一真源（2026-08-21 中文化批次）。

只做展示映射；枚举码本体是跨脚本契约（stage-status、autoheal findings、
collection_runs.status），禁止在任何生产/存储路径改写码值本身。
本模块只有常量与纯函数：不读库、不写文件、不发请求、不下单。
收录完整性由 tests/test_zh_labels_coverage.py 以 AST/可执行形式强制；
运行时对未收录值一律静默兜底（告警路径绝不抛异常）。
"""

# stage 失败分类 → 中文。译法与报告工件 build_report_artifact.py 的
# FAILURE_ZH 保持一致（孪生断言见覆盖测试）。
FAILURE_KIND_ZH = {
    "analysis_deadline_exceeded": "分析截止时间超限",
    "business_output_missing": "业务输出缺失",
    "business_verification_error": "业务校验失败",
    "post_reconcile_business_verification_error": "对账后业务终态校验失败",
    "gateway_terminal_error": "模型网关终态错误",
    "post_facts_runner_handoff_violation": "事实生成后 runner 交接违规",
    "unspecified_failed": "未细分失败",
    "live_stage_status_missing": "Live 阶段终态缺失",
    "cycle_deadline_exceeded": "周期截止超时硬止",
    "post_push_reconcile_failed": "推送后对账失败",
    "push_postcheck_exception": "推送后置核验程序异常",
    "model_output_length": "模型输出超长截断",
    "agent_idle_timeout": "Agent 空闲超时",
    "agent_first_event_timeout": "模型首个流式事件超时",
    "model_empty_output": "模型空输出",
    "collection_gate_failed": "采集闸失败未派发",
    "collection_gate_missing": "采集事实闸缺失",
    "upstream_collection_failed": "上游采集明确失败",
    "agent_process_failed": "Agent 进程失败",
    "agent_protocol_error": "Agent 未完成必需写入即终止",
    "missing_collection_run": "缺采集运行记录",
}

# 账本自愈 finding kind → 中文短注（kind 本体是 findings/告警指纹契约）。
AUTOHEAL_KIND_ZH = {
    "GHOST-EXACT": "幽灵仓·fills精确可补",
    "GHOST-FUZZY": "幽灵仓·含糊不可自动补",
    "UNRECORDED": "交易所有仓账本缺开仓",
    "OVER_CLOSED": "账本净负·多记平仓",
    "NAKED-POSITION-P0": "现仓缺有效保护止损",
    "OVER_CAP": "单轮自愈笔数超上限",
    "AUTOHEAL-BACKLOG": "精确平仓账待分批修复",
    "APPLY-ERROR": "补账写入失败",
    "APPLY-ERROR-UNRECORDED": "补开仓写入失败",
    "AUTOHEAL-ERROR": "自愈执行错误",
    "AUTOHEAL-SKIPPED": "自愈被跳过",
    "QUEUE-CLOSE-ERROR": "修复队列闭合失败",
    "UNRECORDED-EXPERIENCE-MISSING": "补开仓后经验行缺失",
    "UNKNOWN": "未知类别",
}

# collection_runs.status → 中文。真源 collectors/ledger.py 的
# DONE_STATUS/FAIL_STATUS/FAIL_STATUS_PREFIXES；'stale(age=…)' 带参前缀
# 由 collection_status_zh 处理，保留参数原文。
COLLECTION_STATUS_ZH = {
    "ok": "正常",
    "degraded": "降级",
    "error": "错误",
    "fail": "失败",
    "failed": "失败",
    "timeout": "超时",
    "pending": "待落库",
}


# stage 失败的细分原因码 → 中文（stop_reason 冒号后的子码、marker_error、
# post_reconcile_reason 共用一张表）。子码多为 f-string 拼接、AST 收集不可靠，
# 本表尽力收录、未收录时告警显示裸码，不做覆盖测试强制。
STAGE_STOP_ZH = {
    "no_valid_runner_marker": "runner 未留下有效执行凭证",
    "no_timely_analysis": "分析未按时产出",
    "mismatch:plan_sha256": "计划文件与交接凭证哈希不符，凭证签发后计划被改写",
    "live_stage_not_succeeded": "本槽 live 业务未成功；消息送达结果单独核验",
    "failed_preflight": "预检拒绝，交易所零副作用，等待一次计划重写",
    "failed_preflight_rewrite_timeout": "预检拒后未在时限内重写计划",
}


def failure_kind_zh(kind) -> str:
    """有映射返回中文，未收录返回空串（调用方自行决定是否省略括注）。"""
    return FAILURE_KIND_ZH.get(str(kind or "").strip(), "")


def stage_stop_zh(code) -> str:
    """细分原因码的中文；未收录返回空串。"""
    return STAGE_STOP_ZH.get(str(code or "").strip(), "")


def autoheal_kind_gloss(kind) -> str:
    """'GHOST-EXACT' -> 'GHOST-EXACT(幽灵仓·fills精确可补)'；未收录只回原码。"""
    code = str(kind or "UNKNOWN")
    zh = AUTOHEAL_KIND_ZH.get(code)
    return f"{code}({zh})" if zh else code


def collection_status_zh(status) -> str:
    """展示层 status 映射；未收录原样返回（P0/P1 等 level 值直接透传）。

    只认小写 'stale' 前缀（ledger 落库即小写）；大写 STALE 属 dxy_zone
    契约 token，不会进本函数的调用面，防御起见也不改写它。
    """
    raw = str(status or "").strip()
    if raw.startswith("stale"):
        return "过期" + raw[5:]
    return COLLECTION_STATUS_ZH.get(raw.lower(), raw)
