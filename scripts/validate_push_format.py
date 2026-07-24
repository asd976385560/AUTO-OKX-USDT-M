# -*- coding: utf-8 -*-
"""v7.2 推送格式自检脚本

在 QQ 外发前、归档前调用，检查推送内容是否符合 push 模板格式
（templates/push_template.md / agents/push.md）。
不符合 → exit 1 并写 repair_queue；过时格式 → warn 不阻塞。

输入：stdin JSON 或 --content "..." 直接传字符串
输出：JSON {ok, errors[], warnings[], missing_fields[], char_count}
退出码：0=通过（或有 warn 但无 error）/ 1=有 error / 2=输入错误

注意：V7.2 起不设置内部最大字符数限制；char_count 仅作为观测信息。
QQ 平台若返回长度错误，视为 P2 推送失败，完整内容仍必须 push_archive.py 本地归档。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

CST = timezone(timedelta(hours=8))
DB_PATH = Path(_project_path('db', 'account.db'))

# 必填段 / 字段。只做存在性校验，不限制字数。
REQUIRED_SECTIONS = [
    (r'第\d+轮', '轮次'),
    (r'⏱', '耗时'),
    (r'\b(OPEN_LONG|OPEN_SHORT|CLOSE|STOP_LOSS|ADJUST|HOLD|WAIT|NONE|REDUCE|ADD)\b', '动作'),
    (r'📊 资产', '资产段'),
    (r'🟢 实盘', '实盘资产'),
    (r'🟡 模拟盘', '模拟盘资产'),
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


def validate(content: str) -> dict:
    content = normalize_content(content)
    errors = []
    warnings = []
    missing = []

    line_count = content.count('\n') + 1 if content else 0
    hardbreak_lines = sum(1 for line in content.splitlines() if line.endswith('  '))
    if line_count < MIN_LINE_COUNT:
        errors.append(f"推送换行疑似丢失: 仅 {line_count} 行，至少需要 {MIN_LINE_COUNT} 行")
        missing.append('换行结构')
    if hardbreak_lines < MIN_HARDBREAK_LINES:
        errors.append(f"QQ Markdown 硬换行不足: 仅 {hardbreak_lines} 行以两个空格结尾，至少需要 {MIN_HARDBREAK_LINES} 行")
        missing.append('Markdown硬换行')

    for pattern, name in REQUIRED_SECTIONS:
        if not re.search(pattern, content):
            errors.append(f"缺少必填段/字段: {name} (pattern: {pattern})")
            missing.append(name)

    # 标题（头行）含 UNKNOWN 表示 symbol 未解析，触发 error 重组；
    # __FLAT__ 哨兵泄漏进推送内容时同样拦截。
    first_line = next((ln for ln in content.splitlines() if ln.strip()), '')
    if 'UNKNOWN' in first_line:
        errors.append("标题含 UNKNOWN（symbol 未解析）——修正 payload symbol/trades 后重新渲染")
        missing.append('标题symbol')
    if '__FLAT__' in content:
        errors.append("内容含 __FLAT__ 哨兵（空仓标记行不得渲染进推送）——重新渲染")
        missing.append('FLAT哨兵泄漏')

    for pattern, msg in DEPRECATED_PATTERNS:
        if re.search(pattern, content):
            warnings.append(f"过时格式: {msg}")

    # B10 防呆（2026-06-11 全流程验证）：生产推送曾把 live equity 误填进 demo 槽
    # （模拟盘显示 $592.81/0仓，实际 demo $71k/44仓）。两盘资金完全相同几乎必是
    # 组装口径混淆——WARN 提示重组（不 hard fail：极端巧合下可能真相等）。
    m_live = re.search(r'实盘[:：]\s*资金\s*\$?([\d,]+\.?\d*)', content)
    m_demo = re.search(r'模拟盘[:：]\s*资金\s*\$?([\d,]+\.?\d*)', content)
    if m_live and m_demo:
        try:
            v_live = float(m_live.group(1).replace(',', ''))
            v_demo = float(m_demo.group(1).replace(',', ''))
            if abs(v_live - v_demo) < 0.01:
                warnings.append(
                    f"live/demo 资金完全相同（${v_live}）——疑似 demo 槽误填 live equity，"
                    "请核对 assets.demo 数据源（account_snapshots(profile=demo)/OKX demo API，不是 live totalEq）"
                )
        except ValueError:
            pass

    return {
        'ok': len(errors) == 0,
        'errors': errors,
        'warnings': warnings,
        'missing_fields': missing,
        'char_count': len(content),
        'line_count': line_count,
        'hardbreak_lines': hardbreak_lines,
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

    result = validate(content)
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
