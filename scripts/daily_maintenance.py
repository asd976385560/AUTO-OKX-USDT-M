# -*- coding: utf-8 -*-
r"""daily_maintenance.py — 日频运维合并入口（2026-07-17 cron 治理，两轮合并后=
okx-reconcile + okx-log-rotate + okx-audit-snapshot 三条日频 cron 合为一条
okx-daily-maintenance，07:55 起跑）。

顺序跑：① reconcile_daily.py（交易所侧对账：live dry+P1；demo 已于 2026-08-06 下线）
        ② collect_account_bills.py（手续费/资金费/已实现盈亏账单）
        ③ missed_opps_writer.py（确定性写：将完整成熟4小时的错失机会24小时窗
          写入lessons.db；缺精确16根15m K线则阻断 reviewer ready）
        ④-a exit_quality.py（只读：固化退出质量 JSON；写入后只认 manifest SHA-256）
        ④-b ledger_invariants.py（只读账本硬闸；完成后才允许发布 reviewer ready）
        ④-c quality_metrics.py（质量指标；完成后立即原子发布 reviewer ready handoff）
        ④-c collect_account_bills.py --cash-flows-forward（非关键：只采预注册
          当日窗口内 type=1 转入/转出并固化零事件也可审计的日期收据）
        ④-d collect_account_bills.py --trading-bills-forward（非关键：按08:00
          日窗分页采集SWAP账单并固化完整性收据）
        ④-e audit_weekly_trading_net_profit.py（只读、非关键：Goal 10 唯一
          验收为完整周交易净利润>0并排除转账）
        ④-f audit_live_profitability.py（只读、非关键：生命周期胜率、利润因子和
          权益只作诊断，不再承担Goal 10验收）
        ⑤ evaluate_universe_judgments.py（只读：全宇宙一致度影子标签）
        ⑥ evaluate_multitimeframe_model_shadow.py（只读：冻结模型未来前向校准）
        ⑦ audit_model_shadow_label_quality.py（只读：独立从冻结工件与原始ticker
          重建执行价标签，拒绝重复键、时间穿越、价格/收益/汇总不一致及门槛降级）
        ⑧ audit_analysis_signal_forward_quality.py（只读：以 analysis 完成时刻
          后首个快照为入场，成熟生产LLM开多/开空的15m/1H/4H标签）
        ⑨ audit_report_completeness.py（只读：从2026-07-28验收基线至昨日的
          日报完整率；缺失日期也进入分母）
        ⑩ audit_periodic_report_completeness.py（只读：按周一/月初边界重建
          周/月报生成与已验证送达分母；原始JSON直推不算完整）
        ⑪ audit_push_completeness.py（只读：按完整北京时间自然日的15分钟
          计划槽重建Push报告与精确送达分母；独立复验生产归档，并要求sent
          回执哈希与有效归档一致；缺槽、pending和失败均不算完整）
        ⑫ audit_source_health.py（只读：按15分钟计划槽重建 fast 分母，缺失槽也
          计失败；同时保留14日滚动窗和修复后前向窗）
        ⑬ audit_news_source_health.py（只读：按各新闻源真实计划槽重建分母；
          degraded与缺行不计入严格完整率，英文RSS六个发布方逐源前向验收）
        ⑭ audit_positioning_coverage.py（只读：官方REST最新1H多空比批次逐币
          对齐最新USDT线性SWAP宇宙；批次键修复后从03:00起重新累计24个
          整点批次完整性槽和96个15分钟决策可用性槽；缺失、额外、比例非法、
          非计划采集或决策时来源年龄超90分钟均失败关闭）
        ⑮ audit_asset_class_coverage.py（只读：逐币对齐OKX官方instCategory，
          分开审计本地分类有行率与语义兼容率）
        ⑯ audit_contract_statistics_coverage.py（只读：官方15m合约OI与主动
          买卖量最新批次严格审计；同时按15分钟计划槽重建修复后分母，
          缺批次计失败，不足96槽不得通过）
        ⑰ audit_market_field_coverage.py（只读：按每个15分钟自然槽的
          不可变官方交易对快照重建分母，逐币校验七个行情字段与
          可执行买卖盘；缺槽、缺币、无效字段均进分母，不足96槽不得通过）
        ⑱ audit_market_feature_coverage.py（只读：每槽固定100币
          动态增强分母，独立复算选择哈希、50档盘口与最近逐笔流；
          缺行、重复、过时或派生值不一致均进分母，不足96槽不得通过）
        ⑲ audit_multitimeframe_coverage.py（只读：按最新交易宇宙逐币核验精确
          已收盘15m/1H/4H OHLCV与指标就绪率；新区历史不足仍留在分母）
        ⑲-b audit_ws_market_health.py（只读：ws_first后的行情WS健康硬门；
          PENDING/PASSED rc=0，NOT_MET rc=2 按日维护失败外显）
        ⑳ ledger_invariants.py（只读：近24h重复执行、负净仓、经验数量错配、
          未决/含糊执行意图；有发现 rc=1，使日维护失败外显，不自动补单/改账）
        ㉑ log_rotate.py --apply --days 7 --dirs trigger,push,stage-status,stage-control,analysis-validation,collect/guards
          （高频调试日志＋分析校验 json＋采集守护回执/锁，超 7 天轮转；.jsonl 审计类受保护后缀豁免）
        ㉒ audit_snapshot.py（audit_events 增量导出，防滚动窗丢失）
        ㉓ reports_rotate.py --apply（reports/agents+push 超 30 天月度压包，
          封顶无界增长——2026-07-17 主人拍板）
        ㉔ collect_public_macro.py（Alternative.me、ECB复算DXY、ETF权威证据核验）
        ㉕ collect_macro_events.py（未来7天高重要度经济日历）
每步独立 fail-safe：一步失败/超时不阻断下一步；任一失败聚合 exit 1（cron 记 error
可见），全过 exit 0。新增日频运维项往这里加，不再开新 cron。
注意：本 cron 含 reconcile（历史上 demo 会真动账本，现已下线）已入 fulltest BUSINESS_CRONS——
测试窗内随业务 cron 一并停/复。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from report_diagnostic_receipt import (
    CONTRACT_VERSION as DIAGNOSTIC_CONTRACT_VERSION,
    build_receipt, compact_summary, failure, manifest_receipt, write_once_receipt,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = Path(__file__).resolve().parent
QUALITY_REPORT_DIR = Path(os.environ.get(
    "OKX_QUALITY_REPORT_DIR", _public_project_path('reports', 'quality')))
REVIEWER_READY_DIR = Path(os.environ.get(
    "OKX_REVIEWER_READY_DIR", str(QUALITY_REPORT_DIR)))
EXIT_QUALITY_REPORT_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_SCHEMA_VERSION = 2
EXIT_QUALITY_METHOD_VERSION = "exit_quality_v2_forward_frozen"
# 2026-08-19 G1：净 R 口径起用 v2；此处是**消费/校验**侧，接受 v1|v2，
# 边界前归档的 v1 工件继续通过（历史不反向加责）。
EXIT_QUALITY_PEAK_METHOD_VERSION = "peak_giveback_forward_v2"
EXIT_QUALITY_PEAK_METHOD_VERSIONS_ACCEPTED = (
    "peak_giveback_forward_v1", "peak_giveback_forward_v2")
EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE = "2026-08-15T14:45"
EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS = "2026-08-16 08:00:00"
EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD = (
    "authoritative_exit_fill_market_16x15m_v1")
BASE_REVIEWER_CRITICAL_STEPS = (
    "reconcile",
    "account_bills",
    "missed_opportunities",
    "ledger_invariants",
    "quality_metrics",
)
REVIEWER_CRITICAL_STEPS = (
    "reconcile",
    "account_bills",
    "missed_opportunities",
    "exit_quality",
    "ledger_invariants",
    "quality_metrics",
)
# 2026-08-20：这些关键步失败**不再打死整份日报**，而是与 reconcile rc=1
# 同走 provisional 通道。起因是 exit_quality 因一笔七天前老单缺 exit_mode
# 判 blocked，代价是 2026-08-19 日报整份不存在 —— 而日报的核心事实
# （成交/PnL/手续费/持仓）与退出质量分析彼此独立，丢一个回顾性分析段
# 不该抹掉整份报告。它们仍留在 REVIEWER_CRITICAL_STEPS 里：失败照样
# 被记录、被外显，只是不再是「有没有报告」的开关。
PROVISIONAL_ON_FAILURE_STEPS = ("exit_quality",)
# 预注册边界（只向前）：边界前的 manifest 保持原判定，不反向重解释历史。
PROVISIONAL_DEGRADE_FROM = "2026-08-21"
CST = timezone(timedelta(hours=8))


def reviewer_critical_steps(business_date: str) -> tuple[str, ...]:
    """Add exit quality only for reports at/after its forward boundary."""
    if f"{business_date} 08:00:00" < EXIT_QUALITY_REPORT_ACTIVATION_TS:
        return BASE_REVIEWER_CRITICAL_STEPS
    return REVIEWER_CRITICAL_STEPS


STEPS = [
    # (名字, argv, 单步超时秒, 合法退出码)。reconcile 现只剩 live 一次 OKX API 往返
    # （2026-08-06 前还有 demo 的 dry→apply→复检三次）；其 rc=1=「有账实差异且告警
    # 已推」（脚本工作正常，差异经 QQ P1 走人工通道），只有 rc≥2 才算本步失败。
    ("reconcile", [str(SCRIPTS / "reconcile_daily.py")], 1200, (0, 1)),
    ("account_bills", [
        str(SCRIPTS / "collect_account_bills.py"),
    ], 120, (0,)),
    # 日报08:05生成前，先固化整体前移4小时的连续24h错失机会结果窗；
    # 该窗最晚候选为03:45，07:55运行时其16根15m后验已完整闭合。
    ("missed_opportunities", [
        str(SCRIPTS / "missed_opps_writer.py"),
    ], 180, (0,)),
    # 退出质量与错失开仓池一样只在 ready 前执行一次；消费者只读冻结工件。
    ("exit_quality", [
        str(SCRIPTS / "exit_quality.py"),
        "--market-db", _public_project_path('db', 'market.db'),
        "--quality-dir", str(QUALITY_REPORT_DIR),
        "--window-close-wait-seconds", "600",
    ], 720, (0,)),
    # Reviewer 发布前的真实账本硬闸。保持只读；任何 finding 都令 ready 阻断，
    # 不自动重放、补单、改账或清 repair_queue。
    ("ledger_invariants", [
        str(SCRIPTS / "ledger_invariants.py"),
        "--profile", "live", "--window-min", "1440", "--compact",
    ], 120, (0,)),
    # 质量指标是 reviewer handoff 的最后一个关键步骤。成功落完整 JSON 后立即
    # 发布 ready manifest；后续非关键维护继续跑，不阻塞 08:05 reviewer。
    ("quality_metrics", [str(SCRIPTS / "quality_metrics.py")], 120, (0,)),
    # 资金流来源从预注册边界起只收本日固定窗；不补边界前历史，也不改关键
    # SWAP 账单步骤。每日收据即使零事件也证明 type=1 查询成功。
    ("account_cash_flows", [
        str(SCRIPTS / "collect_account_bills.py"),
        "--db-root", _public_project_path('db'),
        "--profiles", "live",
        "--cash-flows-forward",
        "--cash-flow-forward-start", "2026-08-18T00:00:00+08:00",
        "--cash-flow-max-pages", "10",
        "--cash-flow-page-delay", "0.45",
        "--cash-flow-wait-for-close-seconds", "300",
        "--receipt-dir", str(
            QUALITY_REPORT_DIR / "account-cash-flow-forward"),
    ], 420, (0,)),
    # 目标10的账单完整性证据：08:00窗闭合后按页回读当日全部SWAP账单，
    # 与转账收据一样只向前留日窗收据；不补旧窗、不下单。
    ("account_trading_bills_forward", [
        str(SCRIPTS / "collect_account_bills.py"),
        "--db-root", _public_project_path('db'),
        "--profiles", "live",
        "--trading-bills-forward",
        "--trading-forward-start", "2026-08-31T08:00:00+08:00",
        "--trading-max-pages", "20",
        "--trading-page-delay", "0.45",
        "--trading-wait-for-close-seconds", "300",
        "--trading-receipt-dir", str(
            QUALITY_REPORT_DIR / "account-trading-bills-forward"),
    ], 420, (0,)),
    # Goal 10 唯一盈利验收：完整周 type=2/8 账单净变动严格>0，type=1
    # 转账排除。状态只写工件，不改变 reviewer ready 或交易行为。
    ("weekly_trading_net_profit", [
        str(SCRIPTS / "audit_weekly_trading_net_profit.py"),
        "--account-db", _public_project_path('db', 'account.db'),
        "--transfer-receipt-dir", str(
            QUALITY_REPORT_DIR / "account-cash-flow-forward"),
        "--trading-receipt-dir", str(
            QUALITY_REPORT_DIR / "account-trading-bills-forward"),
        "--activation-cst", "2026-08-31T08:00:00+08:00",
        "--json-out", str(
            QUALITY_REPORT_DIR / "weekly-trading-net-profit-audit.json"),
    ], 120, (0,)),
    # 生命周期胜率、利润因子、回撤与权益继续作诊断，不再承担Goal 10验收。
    ("live_profitability", [
        str(SCRIPTS / "audit_live_profitability.py"),
        "--account-db", _public_project_path('db', 'account.db'),
        "--trades-db", _public_project_path('db', 'live_trades.db'),
        "--ledger-db", _public_project_path('db', 'ledger.db'),
        "--cash-flow-receipt-dir", str(
            QUALITY_REPORT_DIR / "account-cash-flow-forward"),
        "--cash-flow-forward-start", "2026-08-18T00:00:00+08:00",
        "--window-days", "30",
        "--minimum-closed-lifecycles", "100",
        "--json-out", str(
            QUALITY_REPORT_DIR / "live-profitability-audit.json"),
    ], 120, (0,)),
    ("universe_judgment_evaluation", [
        str(SCRIPTS / "evaluate_universe_judgments.py"),
        "--snapshot-root", str(QUALITY_REPORT_DIR / "universe-shadow"),
        "--market-db", _public_project_path('db', 'market.db'),
        "--json-out", str(QUALITY_REPORT_DIR / "universe-shadow-evaluation.json"),
        "--labels-out", str(QUALITY_REPORT_DIR / "universe-shadow-labels.csv"),
    ], 180, (0,)),
    ("frozen_model_shadow_evaluation", [
        str(SCRIPTS / "evaluate_multitimeframe_model_shadow.py"),
        "--shadow-root", str(QUALITY_REPORT_DIR / "model-shadow" / "forward"),
        "--market-db", _public_project_path('db', 'market.db'),
        "--json-out", str(QUALITY_REPORT_DIR / "model-shadow-evaluation.json"),
        "--labels-out", str(QUALITY_REPORT_DIR / "model-shadow-labels.csv"),
    ], 180, (0,)),
    ("frozen_model_shadow_label_quality", [
        str(SCRIPTS / "audit_model_shadow_label_quality.py"),
        "--evaluation", str(QUALITY_REPORT_DIR / "model-shadow-evaluation.json"),
        "--labels", str(QUALITY_REPORT_DIR / "model-shadow-labels.csv"),
        "--shadow-root", str(QUALITY_REPORT_DIR / "model-shadow" / "forward"),
        "--market-db", _public_project_path('db', 'market.db'),
        "--json-out", str(
            QUALITY_REPORT_DIR / "model-shadow-label-quality-audit.json"),
    ], 180, (0,)),
    ("analysis_signal_forward_quality", [
        str(SCRIPTS / "audit_analysis_signal_forward_quality.py"),
        "--analysis-db", _public_project_path('db', 'analysis.db'),
        "--market-db", _public_project_path('db', 'market.db'),
        "--json-out", str(
            QUALITY_REPORT_DIR / "analysis-signal-forward-evaluation.json"),
        "--labels-out", str(
            QUALITY_REPORT_DIR / "analysis-signal-forward-labels.csv"),
    ], 180, (0,)),
    ("daily_report_completeness", [
        str(SCRIPTS / "audit_report_completeness.py"),
        "--start", "2026-07-28",
        "--forward-start", "2026-08-13",
        "--forward-minimum-days", "30",
        "--reports-dir", _public_project_path('reports', 'daily-reports'),
        "--account-db", _public_project_path('db', 'account.db'),
        "--live-trades-db", _public_project_path('db', 'live_trades.db'),
        "--ledger-db", _public_project_path('db', 'ledger.db'),
        "--event-log", _public_project_path('logs', 'push', 'qq_push_dedupe.jsonl'),
        "--json-out", str(
            QUALITY_REPORT_DIR / "daily-report-completeness.json"),
    ], 60, (0,)),
    ("periodic_report_completeness", [
        str(SCRIPTS / "audit_periodic_report_completeness.py"),
        "--weekly-start", "2026-08-03",
        "--monthly-start", "2026-08-01",
        "--forward-weekly-start", "2026-08-17",
        "--forward-monthly-start", "2026-09-01",
        "--forward-weekly-minimum", "12",
        "--forward-monthly-minimum", "6",
        "--json-out", str(
            QUALITY_REPORT_DIR / "periodic-report-completeness-audit.json"),
    ], 60, (0,)),
    ("push_completeness", [
        str(SCRIPTS / "audit_push_completeness.py"),
        "--days", "14",
        "--pipeline-log", _public_project_path('logs', 'push', 'pipeline_runs.jsonl'),
        "--event-log", _public_project_path('logs', 'push', 'qq_push_dedupe.jsonl'),
        "--dedupe-db", _public_project_path('db', 'qq_push_dedupe.db'),
        "--reports-dir", _public_project_path('reports', 'agents'),
        "--json-out", str(
            QUALITY_REPORT_DIR / "push-completeness-audit.json"),
    ], 120, (0,)),
    ("complete_cycle_sla", [
        str(SCRIPTS / "audit_complete_cycle_sla.py"),
        "--forward-start", "2026-08-15T00:00:00+08:00",
        "--finality-seconds", "900",
        "--minimum-slots", "96",
        "--status-dir", _public_project_path('logs', 'stage-status'),
        "--analysis-db", _public_project_path('db', 'analysis.db'),
        "--live-trades-db", _public_project_path('db', 'live_trades.db'),
        "--mtf-dir", _public_project_path('tmp'),
        "--collect-log-dir", _public_project_path('logs', 'collect'),
        "--briefing-log-dir", _public_project_path('logs', 'briefing'),
        "--trigger-log-dir", _public_project_path('logs', 'trigger'),
        "--json-out", str(
            QUALITY_REPORT_DIR / "complete-cycle-sla-audit.json"),
    ], 120, (0,)),
    ("fast_source_health", [
        str(SCRIPTS / "audit_source_health.py"),
        "--ledger-db", _public_project_path('db', 'ledger.db'),
        "--forward-start", "2026-08-12T16:00:00+08:00",
        "--rolling-days", "14",
        "--forward-minimum-slots", "96",
        "--json-out", str(QUALITY_REPORT_DIR / "source-health-audit.json"),
    ], 180, (0,)),
    ("news_source_health", [
        str(SCRIPTS / "audit_news_source_health.py"),
        "--ledger-db", _public_project_path('db', 'ledger.db'),
        "--registry", _public_project_path('collectors', 'sources', 'registry.json'),
        "--forward-start", "2026-08-12T16:15:00+08:00",
        "--rolling-days", "14",
        "--minimum-window-hours", "24",
        "--json-out", str(
            QUALITY_REPORT_DIR / "news-source-health-audit.json"),
    ], 60, (0,)),
    ("positioning_coverage", [
        str(SCRIPTS / "audit_positioning_coverage.py"),
        "--market-db", _public_project_path('db', 'market.db'),
        "--maximum-source-age-minutes", "90",
        "--forward-start", "2026-08-13T03:00:00+08:00",
        "--forward-minimum-slots", "24",
        "--availability-forward-start", "2026-08-13T03:00:00+08:00",
        "--availability-minimum-slots", "96",
        "--json-out", str(
            QUALITY_REPORT_DIR / "positioning-coverage-audit.json"),
    ], 120, (0, 1)),
    ("asset_class_coverage", [
        str(SCRIPTS / "audit_asset_class_coverage.py"),
        "--market-db", _public_project_path('db', 'market.db'),
        "--minimum-rate", "0.99",
        "--json-out", str(
            QUALITY_REPORT_DIR / "asset-class-coverage-audit.json"),
    ], 60, (0,)),
    # audit_contract_statistics_coverage.py 的 rc=1 表示审计工件已正常生成、
    # 长窗 analysis-ready 指标仍为 NOT_MET；这属于数据结论，不是进程故障。
    # rc>=2 才按运行失败处理，NOT_MET 继续由 JSON 工件原样外显。
    ("contract_statistics_coverage", [
        str(SCRIPTS / "audit_contract_statistics_coverage.py"),
        "--db", _public_project_path('db', 'market.db'),
        "--minimum-coverage", "0.99",
        "--forward-start", "2026-08-12T16:00:00+08:00",
        "--forward-minimum-slots", "96",
        "--json-out", str(
            QUALITY_REPORT_DIR / "contract-statistics-coverage-audit.json"),
    ], 180, (0, 1)),
    ("market_field_coverage", [
        str(SCRIPTS / "audit_market_field_coverage.py"),
        "--market-db", _public_project_path('db', 'market.db'),
        "--forward-start", "2026-08-12T22:45:00+08:00",
        "--minimum-slots", "96",
        "--json-out", str(
            QUALITY_REPORT_DIR / "market-field-coverage-audit.json"),
    ], 180, (0,)),
    ("market_feature_coverage", [
        str(SCRIPTS / "audit_market_feature_coverage.py"),
        "--market-db", _public_project_path('db', 'market.db'),
        "--forward-start", "2026-08-12T23:15:00+08:00",
        "--minimum-slots", "96",
        "--expected-symbols-per-slot", "100",
        "--json-out", str(
            QUALITY_REPORT_DIR / "market-feature-coverage-audit.json"),
    ], 180, (0,)),
    ("multitimeframe_coverage", [
        str(SCRIPTS / "audit_multitimeframe_coverage.py"),
        "--market-db", _public_project_path('db', 'market.db'),
        "--minimum-rate", "0.99",
        "--json-out", str(
            QUALITY_REPORT_DIR / "multitimeframe-coverage-audit.json"),
    ], 60, (0,)),
    # 2026-08-29：WS健康NOT_MET是业务质量结论，不是进程故障。rc=0覆盖
    # PENDING/PASSED，rc=1是结构合法的NOT_MET；rc>=2、异常、超时或工件损坏
    # 才使维护失败。本步只读且不自动改变数据源阶段。
    ("ws_market_health", [
        str(SCRIPTS / "audit_ws_market_health.py"),
        "--cache-db", _public_project_path('db', 'ws_market_cache.db'),
        "--market-db", _public_project_path('db', 'market.db'),
        "--required-hours", "24",
        "--json-out", str(
            QUALITY_REPORT_DIR / "ws-market-health-audit.json"),
    ], 120, (0, 1)),
    # 2026-08-19 F3：Agent 规则遵守度只读审计（3×ATR / 探针 fixed_tp / EV
    # override）。只读双库、不改库、不下单；违规是数据不是进程失败，故只接受
    # rc=0，结论看 JSON 的 overall_status。非关键步：不进 REVIEWER_CRITICAL_STEPS。
    ("rule_compliance", [
        str(SCRIPTS / "audit_rule_compliance.py"),
        "--db-root", _public_project_path('db'),
        "--json-out", str(
            QUALITY_REPORT_DIR / "rule-compliance-audit.json"),
    ], 120, (0,)),
    ("log_rotate", [
        str(SCRIPTS / "log_rotate.py"),
        "--apply", "--days", "7",
        "--dirs", "trigger,push,stage-status,stage-control,analysis-validation,collect/guards",
    ], 120, (0,)),
    ("audit_snapshot", [str(SCRIPTS / "audit_snapshot.py")], 120, (0,)),
    ("reports_rotate", [str(SCRIPTS / "reports_rotate.py"), "--apply"], 120, (0,)),
    # 公开日频宏观：rc=2 表示部分可选源不可达；数据自身 freshness 另行外显，
    # 不让一个可选源阻断对账/审计等日维护步骤。
    ("public_macro", [str(SCRIPTS / "collect_public_macro.py")], 180, (0, 2)),
    ("macro_events", [str(SCRIPTS / "collect_macro_events.py")], 90, (0,)),
]


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _quality_artifact(business_date: str) -> dict:
    path = QUALITY_REPORT_DIR / f"quality_metrics_{business_date}.json"
    result = {"path": str(path), "valid": False}
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("root must be an object")
        if str(payload.get("ts") or "")[:10] != business_date:
            raise ValueError("business date differs")
        if not isinstance(payload.get("metrics"), dict):
            raise ValueError("metrics missing")
        result.update({
            "valid": True,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        })
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _exit_quality_artifact(business_date: str) -> dict:
    """Independently bind the forward-only frozen artifact into ready."""
    path = QUALITY_REPORT_DIR / f"exit_quality_{business_date}.json"
    result = {"path": str(path), "valid": False}
    report_end = f"{business_date} 08:00:00"
    report_start = (
        datetime.strptime(report_end, "%Y-%m-%d %H:%M:%S")
        - timedelta(days=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    candidate_start = (
        datetime.strptime(report_start, "%Y-%m-%d %H:%M:%S")
        - timedelta(hours=4)
    ).strftime("%Y-%m-%d %H:%M:%S")
    candidate_end = (
        datetime.strptime(report_end, "%Y-%m-%d %H:%M:%S")
        - timedelta(hours=4)
    ).strftime("%Y-%m-%d %H:%M:%S")
    peak_effective_start = max(
        candidate_start, EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS)
    expected_peak_status = (
        "PENDING" if peak_effective_start >= candidate_end else "COMPLETE")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("root must be an object")
        peak_value = payload.get("peak_giveback")
        margin_value = payload.get("margin_return_review")
        missed_value = payload.get("missed_take_profit")
        peak = peak_value if isinstance(peak_value, dict) else {}
        margin = margin_value if isinstance(margin_value, dict) else {}
        missed = missed_value if isinstance(missed_value, dict) else {}
        missed_classes_value = missed.get("classification_counts")
        missed_classes = (
            missed_classes_value
            if isinstance(missed_classes_value, dict) else {})
        try:
            generated_at = datetime.strptime(
                str(payload.get("generated_at") or "")[:19],
                "%Y-%m-%d %H:%M:%S")
        except ValueError:
            generated_at = None
        report_end_dt = datetime.strptime(report_end, "%Y-%m-%d %H:%M:%S")
        expected = (
            payload.get("schema_version") == EXIT_QUALITY_SCHEMA_VERSION,
            payload.get("method_version") == EXIT_QUALITY_METHOD_VERSION,
            payload.get("business_date") == business_date,
            generated_at is not None and generated_at >= report_end_dt,
            payload.get("report_activation_cst")
            == EXIT_QUALITY_REPORT_ACTIVATION_TS,
            payload.get("margin_fact_activation_cycle")
            == EXIT_QUALITY_MARGIN_FACT_ACTIVATION_CYCLE,
            payload.get("counterfactual_activation_cst")
            == EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS,
            (payload.get("report_window") or {}).get("start_ts")
            == report_start,
            (payload.get("report_window") or {}).get("end_ts") == report_end,
            (payload.get("report_window") or {}).get("end_exclusive") is True,
            (payload.get("candidate_window") or {}).get("start_ts")
            == candidate_start,
            (payload.get("candidate_window") or {}).get("end_ts")
            == candidate_end,
            (payload.get("candidate_window") or {}).get("end_exclusive")
            is True,
            isinstance(peak, dict),
            isinstance(margin, dict),
            isinstance(missed, dict),
            missed.get("evidence_method_version")
            == EXIT_QUALITY_COUNTERFACTUAL_EVIDENCE_METHOD,
            missed.get("counterfactual_activation_cst")
            == EXIT_QUALITY_COUNTERFACTUAL_ACTIVATION_TS,
            missed.get("upstream_status") == "READY",
            peak.get("method_version")
            in EXIT_QUALITY_PEAK_METHOD_VERSIONS_ACCEPTED,
            peak.get("fact_activation_cst")
            == EXIT_QUALITY_PEAK_FACT_ACTIVATION_TS,
            peak.get("status") == expected_peak_status,
            (
                (peak.get("effective_window") or {}).get("start_ts"),
                (peak.get("effective_window") or {}).get("end_ts"),
                (peak.get("effective_window") or {}).get("end_exclusive"),
            ) == (peak_effective_start, candidate_end, True),
            all(isinstance(peak.get(key), int) and peak.get(key) >= 0
                for key in ("candidate_closed_rows",
                            "pre_activation_excluded_rows")),
            all(isinstance(peak.get(key), int) and peak.get(key) >= 0
                for key in ("source_closed_rows", "excluded_non_live_rows",
                            "excluded_non_open_rows", "closed_rows")),
            all(isinstance(margin.get(key), int) and margin.get(key) >= 0
                for key in ("source_candidate_cycle_rows",
                            "excluded_non_live_cycle_rows",
                            "excluded_non_open_position_rows",
                            "total_position_cycles")),
            "requested_unconfirmed" in (
                margin.get("disposition_counts") or {}),
            all(isinstance(missed.get(key), int) and missed.get(key) >= 0
                for key in ("source_closed_rows", "excluded_profile_count",
                            "excluded_fallback_count")),
            all(isinstance(missed_classes.get(key), int)
                and missed_classes.get(key) >= 0
                for key in ("missed_take_profit", "excluded_profile",
                            "excluded_fallback")),
            isinstance(missed.get("pool_size"), int)
            and missed.get("pool_size") >= 0
            and missed.get("pool_size")
            == missed_classes.get("missed_take_profit"),
            payload.get("safety") == {
                "production_database_writes": 0,
                "cycles_replayed": 0,
                "window_extended": False,
                "orders_placed": 0,
            },
        )
        if not all(expected):
            raise ValueError("frozen artifact identity/contract differs")
        result.update({
            "valid": True,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "schema_version": EXIT_QUALITY_SCHEMA_VERSION,
            "method_version": EXIT_QUALITY_METHOD_VERSION,
        })
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


BUSINESS_STATUS_ARTIFACTS = {
    "positioning_coverage": (
        QUALITY_REPORT_DIR / "positioning-coverage-audit.json", "overall_status"),
    "ws_market_health": (
        QUALITY_REPORT_DIR / "ws-market-health-audit.json", "status"),
    "frozen_model_shadow_evaluation": (
        QUALITY_REPORT_DIR / "model-shadow-evaluation.json", "model_statuses"),
}


def _business_status_result(step_name: str) -> dict | None:
    """Read a valid audit's business conclusion without conflating it with rc."""
    spec = BUSINESS_STATUS_ARTIFACTS.get(step_name)
    if spec is None:
        return None
    path, field = spec
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("artifact root must be an object")
        if field == "model_statuses":
            statuses = [
                str((item.get("overall") or {}).get("status") or "UNKNOWN")
                for item in payload.get("models") or []
                if isinstance(item, dict)
            ]
            if not statuses:
                raise ValueError("model statuses missing")
            status = statuses[0] if len(set(statuses)) == 1 else "MIXED"
            detail = {"model_statuses": statuses}
        else:
            status = str(payload.get(field) or "").strip()
            if not status:
                raise ValueError(f"{field} missing")
            detail = {}
        return {
            "valid": True,
            "status": status,
            "artifact": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            **detail,
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "valid": False,
            "status": "UNKNOWN",
            "artifact": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        }


def build_reviewer_manifest(report: dict, state: str) -> dict:
    """Build the bounded hand-off contract consumed by the 08:05 reviewer."""
    business_date = str(report["business_date"])
    steps = report.get("steps") or {}
    critical = {}
    required_steps = reviewer_critical_steps(business_date)
    for name in required_steps:
        step = steps.get(name)
        critical[name] = {
            "completed": isinstance(step, dict),
            "rc": step.get("rc") if isinstance(step, dict) else None,
            "accepted": bool(step.get("accepted")) if isinstance(step, dict) else False,
        }
        if isinstance(step, dict) and step.get("artifact"):
            critical[name]["artifact"] = step["artifact"]
        if isinstance(step, dict):
            critical[name]["stderr_summary"] = compact_summary(
                step.get("stderr_summary", step.get("stderr")))
            if step.get("error"):
                critical[name]["error"] = compact_summary(step["error"])

    degradable = (
        set(PROVISIONAL_ON_FAILURE_STEPS)
        if business_date >= PROVISIONAL_DEGRADE_FROM else set()
    )
    degraded_steps = sorted(
        name for name, item in critical.items()
        if name in degradable
        and not (item["completed"] and item["accepted"])
    )
    ready = (
        state == "completed"
        and all(item["completed"] and item["accepted"]
                for name, item in critical.items()
                if name not in degradable)
    )
    reconcile_rc = critical["reconcile"]["rc"]
    all_steps = {}
    for name, step in steps.items():
        if not isinstance(step, dict):
            continue
        all_steps[name] = {
            "rc": step.get("rc"),
            "accepted": bool(step.get("accepted")),
            "completed_at": step.get("completed_at"),
        }
        if step.get("duration_seconds") is not None:
            all_steps[name]["duration_seconds"] = step["duration_seconds"]
        if step.get("business_result") is not None:
            all_steps[name]["business_result"] = step["business_result"]
        if step.get("performance_warning") is not None:
            all_steps[name]["performance_warning"] = step["performance_warning"]
    manifest = {
        "schema_version": 1,
        "diagnostic_contract_version": DIAGNOSTIC_CONTRACT_VERSION,
        "business_date": business_date,
        "run_id": report["run_id"],
        "maintenance_started_at": report["started_at"],
        "critical_steps_completed_at": report.get(
            "critical_steps_completed_at"),
        "maintenance_completed_at": report.get("completed_at"),
        "maintenance_ok": report.get("ok"),
        "maintenance_steps": all_steps,
        "generated_at": now_cst(),
        "state": "ready" if ready else (
            "not_ready" if state == "completed" else "running"),
        "ready": ready,
        "critical_steps": list(required_steps),
        "steps": critical,
        # rc=1 is an accepted reconciliation run with unresolved differences.
        # The reviewer may proceed, but only with a provisional report.
        "provisional_required": (
            reconcile_rc == 1 or bool(degraded_steps)),
        # 哪一步把报告降成临时的，必须能分辨：对账未清零与退出质量段
        # 不可用是两件事，复盘正文要写的话也不同。
        "provisional_reasons": (
            (["live_reconcile_unresolved"] if reconcile_rc == 1 else [])
            + [f"critical_step_degraded:{name}" for name in degraded_steps]
        ),
        "degraded_critical_steps": degraded_steps,
        "provisional_degrade_from": PROVISIONAL_DEGRADE_FROM,
        "report_mode": (
            "provisional"
            if ready and (reconcile_rc == 1 or degraded_steps)
            else "final_candidate" if ready
            else "blocked"
        ),
        "auto_send": False,
    }
    if state == "completed" and manifest["report_mode"] != "final_candidate":
        manifest["diagnostic_receipt"] = manifest_receipt(
            manifest, business_date, manifest["report_mode"])
    return manifest


def write_reviewer_manifest(report: dict, state: str) -> dict:
    manifest = build_reviewer_manifest(report, state)
    if manifest.get("diagnostic_receipt"):
        manifest["diagnostic_receipt_path"] = str(write_once_receipt(
            REVIEWER_READY_DIR / "diagnostics", manifest["diagnostic_receipt"]))
    path = REVIEWER_READY_DIR / (
        f"reviewer_ready_{manifest['business_date']}.json")
    _atomic_write_json(path, manifest)
    return {**manifest, "path": str(path)}


def _handoff_write_failure(report: dict, exc: Exception) -> dict:
    error = compact_summary(f"{type(exc).__name__}: {exc}")
    return {
        "ready": False,
        "error": error,
        "diagnostic_receipt": build_receipt(
            report["business_date"], report["run_id"], "blocked",
            [failure("reviewer_ready.write", reason=error)]),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "按固定顺序执行日频维护步骤；不带参数时才执行，"
            "未知参数会立即退出。"
        )
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    parse_args(argv)
    started_at = now_cst()
    report = {
        "ts": started_at,
        "business_date": started_at[:10],
        "run_id": datetime.now(CST).strftime("%Y%m%dT%H%M%S.%f%z"),
        "started_at": started_at,
        "steps": {},
    }
    required_critical_steps = reviewer_critical_steps(report["business_date"])
    try:
        write_reviewer_manifest(report, "running")
    except Exception as exc:
        report["reviewer_ready"] = _handoff_write_failure(report, exc)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 2

    all_ok = True
    critical_handoff_attempted = False
    child_env = os.environ.copy()
    # Canonical quality artifacts are only published by this registered
    # production maintenance chain (or another caller that explicitly opts
    # in). Direct/manual audit probes default to timestamped isolation.
    child_env["OKX_AUDIT_EXECUTION_CONTEXT"] = "production"
    for name, argv, step_timeout, ok_codes in STEPS:
        step_started = time.monotonic()
        try:
            p = subprocess.run([sys.executable, *argv], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=step_timeout, env=child_env)
            tail = (p.stdout or "").strip().splitlines()[-3:]
            accepted = p.returncode in ok_codes
            report["steps"][name] = {
                "rc": p.returncode,
                "accepted": accepted,
                "completed_at": now_cst(),
                "duration_seconds": round(time.monotonic() - step_started, 3),
                "tail": tail,
                # rc=1 reconciliation is accepted but still needs diagnostics.
                "stderr_summary": compact_summary(p.stderr),
            }
            if accepted:
                business_result = _business_status_result(name)
                if business_result is not None:
                    report["steps"][name]["business_result"] = business_result
                    # A missing or malformed business artifact is a process-contract
                    # failure even when the child happened to return an accepted rc.
                    if not business_result["valid"]:
                        accepted = False
                        report["steps"][name]["accepted"] = False
            if (
                name == "complete_cycle_sla"
                and report["steps"][name]["duration_seconds"] > 45.0
            ):
                report["steps"][name]["performance_warning"] = {
                    "threshold_seconds": 45.0,
                    "observed_seconds": report["steps"][name]["duration_seconds"],
                    "blocking": False,
                }
            if name == "quality_metrics" and accepted:
                artifact = _quality_artifact(report["business_date"])
                report["steps"][name]["artifact"] = artifact
                accepted = bool(artifact["valid"])
                report["steps"][name]["accepted"] = accepted
            if name == "exit_quality" and accepted:
                artifact = _exit_quality_artifact(report["business_date"])
                report["steps"][name]["artifact"] = artifact
                accepted = bool(artifact["valid"])
                report["steps"][name]["accepted"] = accepted
            if not accepted:
                all_ok = False
                err_tail = (p.stderr or "").strip().splitlines()[-3:]
                report["steps"][name]["stderr"] = err_tail
        except Exception as e:
            all_ok = False
            report["steps"][name] = {
                "rc": 99,
                "accepted": False,
                "completed_at": now_cst(),
                "duration_seconds": round(time.monotonic() - step_started, 3),
                "error": f"{type(e).__name__}: {e}",
                "stderr_summary": compact_summary(getattr(e, "stderr", None)),
            }
        if (
            not critical_handoff_attempted
            and all(step in report["steps"]
                    for step in required_critical_steps)
        ):
            critical_handoff_attempted = True
            report["critical_steps_completed_at"] = now_cst()
            try:
                report["reviewer_ready"] = write_reviewer_manifest(
                    report, "completed")
            except Exception as exc:
                all_ok = False
                report["reviewer_ready"] = _handoff_write_failure(report, exc)
    report["ok"] = all_ok
    report["completed_at"] = now_cst()
    try:
        report["reviewer_ready"] = write_reviewer_manifest(
            report, "completed")
    except Exception as exc:
        report["ok"] = False
        report["reviewer_ready"] = _handoff_write_failure(report, exc)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
