# -*- coding: utf-8 -*-
"""v7.2 推送格式自检脚本

在 QQ 外发前、归档前调用，检查推送内容是否符合
templates/push_template.md 的固定格式。
不符合 → exit 1 并写 repair_queue；过时格式 → warn 不阻塞。

输入：stdin JSON 或 --content "..." 直接传字符串
输出：JSON {ok, errors[], warnings[], missing_fields[], char_count}
退出码：0=通过（或有 warn 但无 error）/ 1=有 error / 2=输入错误

注意：V7.2 起不设置内部最大字符数限制；char_count 仅作为观测信息。
QQ 平台若返回长度错误，视为 P2 推送失败，完整内容仍必须 push_archive.py 本地归档。
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
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import _acceptance_thresholds as thresholds  # noqa: E402
from scripts import _push_duration  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding='utf-8')

CST = timezone(timedelta(hours=8))
DB_PATH = Path(_public_project_path('db', 'account.db'))

# 2026-08-12 20:00（北京时间）起的新报告契约。历史归档继续按原始
# 16 项基线复核，禁止用后来新增的展示字段反向判坏既有送达证据。
MULTITIMEFRAME_REPORT_REQUIRED_FROM = '2026-08-12T20:00'
EXECUTION_AUDIT_REQUIRED_FROM = '2026-08-14T02:15'
BUSINESS_ATTESTATION_REQUIRED_FROM = '2026-08-14T07:00'
INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM = '2026-08-15T08:00'
# 2026-08-19 G8③ 激活边界（只向前）：自该 cycle 起，执行段除「报告间交易所
# 成交=N笔」外还必须写出「另M笔证据不足未计入」。此前不合格成交被静默丢弃
# （8 月实测跨轮候选丢 11 笔），正文只显示一个更小的数字，validator 永远
# 发现不了。边界前归档的报告无此字段，不反向加责。
INTER_REPORT_RECONCILE_COUNT_REQUIRED_FROM = '2026-08-20T08:00'

# P0-5 5b｜降级战报的三处留痕标记（render_push_report 写、本校验器反查）。
# 刻意用中文横幅而非新增英文动作枚举：DEGRADED 这个词在本仓已被
# trade_cycles.decision='degraded'（零成交零订单的业务终态）占用，复用它会让
# 「无成交的降级终态」与「有成交但缺终态凭证」两件完全不同的事撞进同一个校验分支。
DEGRADED_REPORT_BANNER = '⚠️降级战报'
DEGRADED_REPORT_CAVEAT = 'live终态凭证未就绪'
DEGRADED_REPORT_EXEC_TOKEN = 'report_barrier=not_ready'
_CYCLE_ID_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)$')
_OPEN_REPORT_RE = re.compile(r'\b(?:OPEN_LONG|OPEN_SHORT|ADD)\b')
_MTF_LINE_RE = re.compile(
    r'^(15m|1H|4H) rank=([123]) direction=(long|short|neutral) '
    r'exact=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)',
    re.MULTILINE,
)
_MTF_GROUP_HEADER_RE = re.compile(
    r'^交易对=([A-Z0-9][A-Z0-9-]*-SWAP) side=(long|short)\s*$',
    re.MULTILINE,
)

# 必填段 / 字段。只做存在性校验，不限制字数。
REQUIRED_SECTIONS = [
    (r'第\d+轮', '轮次'),
    (r'⏱', '耗时'),
    (r'\b(OPEN_LONG|OPEN_SHORT|CLOSE|STOP_LOSS|ADJUST|HOLD|WAIT|NONE|REDUCE|ADD|ERROR|DEGRADED)\b', '动作'),
    (r'📊 资产', '资产段'),
    (r'🟢 实盘', '实盘资产'),
    # 2026-08-06：demo 全量下线，模拟盘资产段不再必填（渲染侧同步移除）。
    (r'资金', '资金字段'),
    (r'累计收益', '累计收益字段'),
    (r'💼 持仓详情', '持仓详情'),
    (r'🛡 风控', '风控'),
    (r'🌍 行情', '行情'),
    (r'BTC', 'BTC 行情'),
    (r'ETH', 'ETH 行情'),
    (r'🎯 Agent裁决', 'Agent裁决'),
    (r'🧭 六项决策卡', '六项决策卡'),
    (r'📚 历史经验', '历史经验'),
    (r'⏰ 时间线', '时间线'),
]

MIN_LINE_COUNT = 18
MIN_HARDBREAK_LINES = 12

# 过时格式（warn）
DEPRECATED_PATTERNS = [
    (r'基准\s*\$?\s*1110', '禁止固定基准 $1110.06'),
    (r'永久基准', '禁止永久基准口径'),
    (r'累计收益率', '资产口径使用累计收益 USDT，不使用累计收益率%'),
    (r'收益率\s*%', '资产口径不使用收益率%'),
    (r'session_return_pct', '推送不使用 session_return_pct'),
    (r'→\s*现值', '禁止“→现值”格式'),
    (r'format\s*[=:]\s*4', 'format=4 禁用，必须 format=3'),
]


def normalize_content(content: str) -> str:
    """Normalize JSON-decoded emoji surrogate pairs from PowerShell pipes."""
    if not isinstance(content, str):
        return ''
    try:
        return content.encode('utf-16', errors='surrogatepass').decode('utf-16')
    except Exception:
        return content


def _validate_multitimeframe_section(
    content: str, first_line: str,
) -> tuple[list[str], list[str]]:
    """Validate the human-visible OPEN/ADD three-timeframe report block."""
    errors: list[str] = []
    missing: list[str] = []
    if '🧩 三周期判断' not in content:
        return ['缺少版本化必填段: 三周期判断'], ['三周期判断']
    section = content.split('🧩 三周期判断', 1)[1]
    if '🧭 六项决策卡' in section:
        section = section.split('🧭 六项决策卡', 1)[0]

    if not _OPEN_REPORT_RE.search(first_line):
        for token, name in (
            ('非OPEN/ADD，本轮不适用', '三周期非开仓说明'),
            ('校准可信度=未通过', '三周期校准状态'),
            ('可信度声明=禁止', '三周期可信度声明'),
        ):
            if token not in section:
                errors.append(f'缺少版本化必填字段: {name}')
                missing.append(name)
        return errors, missing

    coverage = re.search(r'^OPEN/ADD覆盖=(\d+)/(\d+)\s*$', section, re.MULTILINE)
    if coverage is None:
        errors.append('OPEN/ADD 三周期判断缺少覆盖计数')
        missing.append('OPEN/ADD覆盖计数')
        declared_complete, declared_total = -1, -1
    else:
        declared_complete = int(coverage.group(1))
        declared_total = int(coverage.group(2))
        if declared_total < 1 or declared_complete != declared_total:
            errors.append('OPEN/ADD 三周期覆盖必须为 N/N 且 N>=1')
            missing.append('OPEN/ADD全覆盖')

    headers = list(_MTF_GROUP_HEADER_RE.finditer(section))
    if len(headers) != declared_total:
        errors.append(
            'OPEN/ADD 三周期交易对组数与覆盖分母不一致')
        missing.append('三周期交易对组')
    seen_legs: set[tuple[str, str]] = set()
    for index, header in enumerate(headers):
        symbol, side = header.group(1), header.group(2)
        leg = (symbol, side)
        if leg in seen_legs:
            errors.append(f'OPEN/ADD 三周期交易对组重复: {symbol}/{side}')
            missing.append('三周期交易对唯一性')
        seen_legs.add(leg)
        end = headers[index + 1].start() if index + 1 < len(headers) else len(section)
        group = section[header.end():end]
        rows = _MTF_LINE_RE.findall(group)
        by_timeframe = {
            timeframe: {
                'rank': int(rank), 'direction': direction, 'exact': exact,
            }
            for timeframe, rank, direction, exact in rows
        }
        if len(rows) != 3 or set(by_timeframe) != {'15m', '1H', '4H'}:
            errors.append(
                f'{symbol}/{side} 必须且只能完整显示 15m/1H/4H')
            missing.append('15m/1H/4H判断')
        elif sorted(row['rank'] for row in by_timeframe.values()) != [1, 2, 3]:
            errors.append(f'{symbol}/{side} relative rank 必须恰为 1,2,3')
            missing.append('三周期唯一rank')

        selected = re.search(
            r'选择=(15m|1H|4H)/(long|short) rank=1', group)
        if selected is None:
            errors.append(f'{symbol}/{side} 缺少 rank=1 的最终选择')
            missing.append('三周期最终选择')
        elif selected.group(1) in by_timeframe:
            row = by_timeframe[selected.group(1)]
            if (
                row['rank'] != 1
                or row['direction'] != selected.group(2)
                or selected.group(2) != side
            ):
                errors.append(
                    f'{symbol}/{side} 最终选择与 rank/direction/成交方向不一致')
                missing.append('三周期选择一致性')

        shown_symbol = re.search(
            r'\bsymbol=([A-Z0-9][A-Z0-9-]*-SWAP)\b', group)
        if shown_symbol is None or shown_symbol.group(1) != symbol:
            errors.append(f'{symbol}/{side} 缺少或错配完整交易对 symbol')
            missing.append('三周期交易对')
        for token, name in (
            ('方法=三周期相对最优（非概率）', '三周期选择方法'),
            ('校准可信度=未通过', '三周期校准状态'),
            ('可信度声明=禁止', '三周期可信度声明'),
        ):
            if token not in group:
                errors.append(f'{symbol}/{side} 缺少版本化必填字段: {name}')
                missing.append(name)
        hashes = re.findall(r'evidence_hash=([0-9a-f]{64})\b', group)
        if len(hashes) != 1:
            errors.append(f'{symbol}/{side} 必须且只能显示一个完整 evidence_hash')
            missing.append('三周期证据哈希')
    return errors, missing


def validate(
    content: str,
    *,
    cycle_id: str | None = None,
    expect_degraded: bool = False,
    db_root: str | Path = _public_project_path('db'),
) -> dict:
    content = normalize_content(content)
    errors = []
    warnings = []
    missing = []

    # P0-5 5b｜降级战报（有业务终态但 live 报告对账屏障未就绪）。
    # 契约是**加法不是减法**：所有既有必填段照旧必填 —— 降级降的是裁决效力，
    # 不是事实完整度。额外要求两处留痕：
    #   ① 第 2 行横幅（紧贴头行，读者第一眼可见）；
    #   ② 执行段 report_barrier=not_ready（机器可读，与账实成交/业务指纹并列）。
    # expect_degraded 由 push_pipeline 在自认降级时传入 —— 少了横幅就是一份
    # 「看起来完全正常、实则无终态凭证」的战报，比不发更危险，故 fail-closed。
    degraded_declared = bool(
        DEGRADED_REPORT_BANNER in content
        and DEGRADED_REPORT_CAVEAT in content
    )
    if expect_degraded and not degraded_declared:
        errors.append(
            f'管道自认降级战报，正文却缺少降级横幅 '
            f'（需同时含「{DEGRADED_REPORT_BANNER}」与「{DEGRADED_REPORT_CAVEAT}」）')
        missing.append('降级战报横幅')
    if degraded_declared:
        _exec_section = content.split('⚙️ 执行', 1)[1] \
            if '⚙️ 执行' in content else ''
        if '⏰ 时间线' in _exec_section:
            _exec_section = _exec_section.split('⏰ 时间线', 1)[0]
        if DEGRADED_REPORT_EXEC_TOKEN not in _exec_section:
            errors.append(
                f'降级战报执行段缺少屏障标记 {DEGRADED_REPORT_EXEC_TOKEN}')
            missing.append('降级屏障标记')

    line_count = content.count('\n') + 1 if content else 0
    hardbreak_lines = sum(1 for line in content.splitlines() if line.endswith('  '))
    if line_count < MIN_LINE_COUNT:
        errors.append(f"推送换行疑似丢失: 仅 {line_count} 行，至少需要 {MIN_LINE_COUNT} 行")
        missing.append('换行结构')
    if hardbreak_lines < MIN_HARDBREAK_LINES:
        errors.append(f"QQ Markdown 硬换行不足: 仅 {hardbreak_lines} 行以两个空格结尾，至少需要 {MIN_HARDBREAK_LINES} 行")
        missing.append('Markdown硬换行')

    minimal_policy = bool(
        cycle_id is not None
        and _CYCLE_ID_RE.fullmatch(str(cycle_id))
        and thresholds.minimal_decision_contract_active(str(cycle_id)))
    for pattern, name in REQUIRED_SECTIONS:
        if minimal_policy and name in {"六项决策卡", "历史经验"}:
            continue
        if not re.search(pattern, content):
            errors.append(f"缺少必填段/字段: {name} (pattern: {pattern})")
            missing.append(name)

    # 标题（头行）含 UNKNOWN 表示 symbol 未解析，触发 error 重组；
    # __FLAT__ 哨兵泄漏进推送内容时同样拦截。
    # push_archive 会在正文前加 Markdown 标题；独立复核归档时仍须找到真正
    # 含“第N轮”的报告头，而不是把归档标题误当成动作头。
    first_line = next(
        (ln for ln in content.splitlines() if re.search(r'第\d+轮', ln)),
        next((ln for ln in content.splitlines() if ln.strip()), ''),
    )
    duration_evidence = None
    if _push_duration.duration_contract_active(cycle_id):
        duration_evidence = _push_duration.read_business_duration(db_root, cycle_id)
        match = re.search(r'/\s*⏱(未知|\d+s)\s*/', first_line)
        if match is None:
            errors.append('耗时必须是经证实的整数秒或明确的「未知」')
            missing.append('业务耗时')
        elif match.group(1) == '未知':
            warnings.append('本轮业务耗时未知，未以0秒替代')
            if '本轮耗时未知：' not in content:
                errors.append('未知耗时缺少原因说明')
        elif duration_evidence['status'] == 'known':
            if int(match.group(1)[:-1]) != duration_evidence['elapsed_seconds']:
                errors.append('标题耗时与同轮业务完成证据不一致')
        else:
            errors.append('标题给出数字耗时，但同轮业务完成证据未能证实')
        if _push_duration.DISPLAY_SEMANTICS not in content:
            errors.append('缺少业务耗时口径说明')
    if 'UNKNOWN' in first_line:
        errors.append("标题含 UNKNOWN（symbol 未解析）——修正 payload symbol/trades 后重新渲染")
        missing.append('标题symbol')
    if '__FLAT__' in content:
        errors.append("内容含 __FLAT__ 哨兵（空仓标记行不得渲染进推送）——重新渲染")
        missing.append('FLAT哨兵泄漏')

    versioned_required = False
    if cycle_id is not None:
        if not _CYCLE_ID_RE.fullmatch(str(cycle_id)):
            errors.append('cycle_id 格式非法，必须为北京时间 YYYY-MM-DDTHH:00|15|30|45')
            missing.append('cycle_id')
        else:
            versioned_required = (
                str(cycle_id) >= MULTITIMEFRAME_REPORT_REQUIRED_FROM
                and thresholds.open_multitimeframe_contract_required(
                    str(cycle_id)))
    if versioned_required:
        mtf_errors, mtf_missing = _validate_multitimeframe_section(
            content, first_line)
        errors.extend(mtf_errors)
        missing.extend(mtf_missing)
    if minimal_policy:
        for retired in ("🧩 三周期判断", "🧭 六项决策卡"):
            if retired in content:
                errors.append(f"新策略禁止退役段: {retired}")

    execution_audit_required = bool(
        cycle_id is not None
        and _CYCLE_ID_RE.fullmatch(str(cycle_id))
        and str(cycle_id) >= EXECUTION_AUDIT_REQUIRED_FROM
    )
    if execution_audit_required:
        execution_section = content.split('⚙️ 执行', 1)[1] \
            if '⚙️ 执行' in content else ''
        if '⏰ 时间线' in execution_section:
            execution_section = execution_section.split('⏰ 时间线', 1)[0]
        first_action = re.search(
            r'\b(ADJUST|ERROR|DEGRADED)\b', first_line)
        required_tokens: tuple[tuple[str, str], ...] = ()
        if first_action and first_action.group(1) == 'ADJUST':
            required_tokens = (
                ('ADJUST_PROTECTION', '调保护动作原语'),
                ('no_fill', '调保护零成交语义'),
                ('path=', '调保护执行路径'),
                ('sz=', '调保护覆盖张数'),
                ('SL=', '调保护已应用止损'),
                ('algoId=', '调保护算法单身份'),
                ('readback=verified/', '调保护回读状态'),
                ('exchange_side_effect=protection_only', '调保护副作用边界'),
            )
        elif first_action:
            required_tokens = (
                ('no_fill', '错误终态零成交语义'),
                ('orders=0', '错误终态零订单'),
                ('exchange_side_effect=none', '错误终态零交易所副作用'),
                ('reason=', '错误终态原始原因'),
            )
        for token, name in required_tokens:
            if token not in execution_section:
                errors.append(f'执行段缺少版本化必填字段: {name}')
                missing.append(name)

    business_attestation_required = bool(
        cycle_id is not None
        and _CYCLE_ID_RE.fullmatch(str(cycle_id))
        and str(cycle_id) >= BUSINESS_ATTESTATION_REQUIRED_FROM
    )
    if business_attestation_required:
        execution_section = content.split('⚙️ 执行', 1)[1] \
            if '⚙️ 执行' in content else ''
        if '⏰ 时间线' in execution_section:
            execution_section = execution_section.split('⏰ 时间线', 1)[0]
        if not re.search(r'账实成交=\d+笔', execution_section):
            errors.append('执行段缺少版本化账实成交计数')
            missing.append('账实成交计数')
        if not re.search(r'业务指纹=[0-9a-f]{64}\b', execution_section):
            errors.append('执行段缺少版本化业务指纹')
            missing.append('业务指纹')

    inter_report_exchange_required = bool(
        cycle_id is not None
        and _CYCLE_ID_RE.fullmatch(str(cycle_id))
        and str(cycle_id) >= INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM
    )
    if inter_report_exchange_required:
        execution_section = content.split('⚙️ 执行', 1)[1] \
            if '⚙️ 执行' in content else ''
        if '⏰ 时间线' in execution_section:
            execution_section = execution_section.split('⏰ 时间线', 1)[0]
        if not re.search(r'报告间交易所成交=\d+笔', execution_section):
            errors.append('执行段缺少报告间交易所成交计数')
            missing.append('报告间交易所成交计数')
        if not re.search(r'区间指纹=[0-9a-f]{64}\b', execution_section):
            errors.append('执行段缺少报告间交易所成交区间指纹')
            missing.append('区间指纹')
        # G8③：未计入笔数必须外显（带独立激活边界）。
        if str(cycle_id) >= INTER_REPORT_RECONCILE_COUNT_REQUIRED_FROM:
            if not re.search(r'另\d+笔证据不足未计入', execution_section):
                errors.append('执行段缺少报告间交易所成交未计入笔数')
                missing.append('报告间未计入笔数')

    for pattern, msg in DEPRECATED_PATTERNS:
        if re.search(pattern, content):
            warnings.append(f"过时格式: {msg}")

    # B10 防呆（live/demo 资金完全相同 → 疑似 demo 槽误填 live equity）已随
    # 2026-08-06 demo 全量下线移除：推送只剩实盘一个资金槽，无从混淆。

    return {
        'ok': len(errors) == 0,
        'errors': errors,
        'warnings': warnings,
        'missing_fields': missing,
        'degraded_report_declared': degraded_declared,
        'degraded_report_expected': bool(expect_degraded),
        'char_count': len(content),
        'line_count': line_count,
        'hardbreak_lines': hardbreak_lines,
        'cycle_id': cycle_id,
        'cycle_duration_evidence': duration_evidence,
        'multitimeframe_contract_required': versioned_required,
        'multitimeframe_required_from': MULTITIMEFRAME_REPORT_REQUIRED_FROM,
        'execution_audit_required': execution_audit_required,
        'execution_audit_required_from': EXECUTION_AUDIT_REQUIRED_FROM,
        'business_attestation_required': business_attestation_required,
        'business_attestation_required_from': BUSINESS_ATTESTATION_REQUIRED_FROM,
        'inter_report_exchange_required': inter_report_exchange_required,
        'inter_report_exchange_required_from': (
            INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM),
    }


def write_repair_queue(check_name: str, issue: str, fix_action: str) -> None:
    """写 repair_queue 表（P3 校验失败时）"""
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        now_cst = datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')
        # created_utc 使用 CST，与 order_executor._enqueue_repair 保持同表同口径；
        # 业务域统一 CST，列名沿用不改（历史 Z 行由迁移批处理）。
        conn.execute(
            "INSERT INTO repair_queue (ts, check_name, issue, fix_action, status, created_utc) "
            "VALUES (?, ?, ?, ?, 'open', ?)",
            (now_cst, check_name, issue, fix_action, now_cst),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[validate_push][WARN] repair_queue 写入失败: {e}", file=sys.stderr)


def close_healed_push_format() -> None:
    """L1 自愈关单（D6 2026-07-15）：本次校验通过＝推送格式管道当前健康，同日更早的
    open push_format 行已自愈——自动关掉（closed_by 标 auto-heal，留审计痕迹）。
    任何异常静默 WARN，绝不影响校验主流程；列未迁移（无 closed_at）时自动跳过。"""
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(repair_queue)").fetchall()}
        if "closed_at" not in cols:
            conn.close()
            return
        now = datetime.now(CST)
        now_cst = now.strftime('%Y-%m-%d %H:%M:%S')
        day_start = now.strftime('%Y-%m-%d 00:00:00')
        cur = conn.execute(
            "UPDATE repair_queue SET status='closed', closed_at=?, "
            "closed_by='validate_push_format:auto-heal', "
            "resolution='同日后续推送校验通过（自愈）' "
            "WHERE check_name='push_format' AND status='open' AND ts>=? AND ts<?",
            (now_cst, day_start, now_cst))
        if cur.rowcount:
            print(f"[validate_push] repair_queue 自愈关单 {cur.rowcount} 行", file=sys.stderr)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[validate_push][WARN] 自愈关单失败(不影响校验): {e}", file=sys.stderr)


def read_stdin_text() -> str:
    if hasattr(sys.stdin, 'buffer'):
        return sys.stdin.buffer.read().decode('utf-8', errors='replace')
    return sys.stdin.read()


def main() -> int:
    ap = argparse.ArgumentParser(description='v7.2 推送格式自检')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--stdin', action='store_true', help='从 stdin 读 JSON {"content": "..."}')
    g.add_argument('--content', type=str, help='直接传推送内容字符串')
    g.add_argument('--file', type=str, help='从文件读推送内容')
    # repair_queue 写入/自愈必须显式开启；隔离或开发干跑不得写生产 account.db。
    # 关掉真实 open 行。生产管道不传本 flag，行为不变。
    ap.add_argument('--no-repair-queue', action='store_true',
                    help='跳过 repair_queue 写入与自愈关单（隔离/开发干跑用，纯校验零写库）')
    ap.add_argument('--cycle-id', type=str,
                    help='北京时间 cycle；20:00 版本边界后启用三周期报告硬校验')
    # P0-5 5b：由 push_pipeline 在自认降级战报时传入，反查渲染是否真打了横幅。
    # 只做「声称降级 ⇒ 必须留痕」的单向断言，不反过来禁止独立复核归档内容。
    ap.add_argument('--expect-degraded', action='store_true',
                    help='断言正文必须带降级战报横幅与执行段屏障标记')
    ap.add_argument('--db-root', default=_public_project_path('db'),
                    help='业务耗时证据读取根目录；仅只读，不回退其他目录')
    args = ap.parse_args()

    if args.stdin:
        raw = read_stdin_text()
        try:
            data = json.loads(raw)
            content = data.get('content', '')
        except Exception as e:
            print(f"[validate_push][FAIL] JSON 解析失败: {e}", file=sys.stderr)
            return 2
    elif args.content is not None:
        # 按 is not None 分派，使 --content "" 进入内容校验并返回“内容太短”。
        content = args.content
    elif args.file is not None:
        content = Path(args.file).read_text(encoding='utf-8')
    else:
        content = ''

    if not content or len(content.strip()) < 50:
        print(f"[validate_push][FAIL] 推送内容太短（{len(content)}字符）", file=sys.stderr)
        return 2

    result = validate(content, cycle_id=args.cycle_id,
                      expect_degraded=args.expect_degraded, db_root=args.db_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not result['ok']:
        if not args.no_repair_queue:
            write_repair_queue(
                'push_format',
                f"推送格式错误: {', '.join(result['errors'][:3])}",
                '使用 render_push_report.py 重新渲染，并按 templates/push_template.md §2 核对必填段',
            )
        return 1

    if result['warnings']:
        print(f"[validate_push][WARN] {len(result['warnings'])} 个过时格式", file=sys.stderr)

    if not args.no_repair_queue:
        close_healed_push_format()
    return 0


if __name__ == '__main__':
    sys.exit(main())
