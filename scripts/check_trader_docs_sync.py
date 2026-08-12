# -*- coding: utf-8 -*-
r"""check_trader_docs_sync.py — 四角色上下文结构与 trader money-path 契约检查。

文件名为兼容既有入口保留；当前不再要求两份 trader 手册逐字同步。每份手册必须是角色本地
上下文，并按 ROLE_SCOPE/PATHS/DB_ACCESS/RUN_OUTPUT/STOP 五个稳定块组织。
本检查只读项目文件和 schema.sql，不连接交易所、不打开 SQLite、不部署 workspace。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ROLE_DOCS = {
    "analyst": ROOT / "agents" / "analyst.md",
    "live": ROOT / "agents" / "live_trader.md",
    "reviewer": ROOT / "agents" / "reviewer.md",
    "news": ROOT / "agents" / "news_scout.md",
}
LIVE_DOC = ROLE_DOCS["live"]
REQUIRED_SECTIONS = ("ROLE_SCOPE", "PATHS", "DB_ACCESS", "RUN_OUTPUT", "STOP")
SOFT_TOTAL_CHARS = 52_000
QUICKREF_MAX_CHARS = 1_400
_H2_RE = re.compile(r"(?m)^## ([A-Z_]+)\s*$")

ROLE_FACTS = {
    "analyst": {
        "paths": (
            "collectors/ledger.py",
            "collectors/analyst_writer.py",
            "scripts/decision_briefing.py",
            "scripts/find_similar_experience.py",
        ),
        "doc_tokens": (
            "analysis.db.analysis_runs", "regime.db", "news.db",
            "--stop-distance-pct", "--planned-rr", "event_occurred_at",
        ),
        "schema": {
            "analysis.db": ("analysis_runs", "analysis_signals"),
            "ledger.db": ("collection_runs",),
            "regime.db": ("cross_market",),
            "news.db": ("news_items",),
        },
    },
    "live": {
        "paths": (
            "core/order_executor.py",
            "core/risk_validator.py",
            "collectors/analyst_writer.py",
            "collectors/trades_writer.py",
            "scripts/live_decision_facts.py",
            "scripts/multitimeframe_decision_evidence.py",
            "core/multitimeframe_gate.py",
        ),
        # 15% 保证金、5% 止损风险、接管重验与可选 TP 都在真钱路径，手册必须同步。
        "doc_tokens": (
            "live_trades.db.trade_cycles", "ledger.db.execution_intents",
            "MAX_SINGLE_ORDER_IMR_RATIO", "MAX_SINGLE_ORDER_RISK_PCT_EQUITY",
            "actor_attestation", "tp_trigger_px",
            "multitimeframe_context_mismatch", "confidence_claim_allowed=false",
        ),
        "schema": {
            "analysis.db": ("analysis_runs", "analysis_signals"),
            "live_trades.db": ("trade_cycles", "trades"),
            "ledger.db": ("execution_intents",),
        },
    },
    "reviewer": {
        "paths": (
            "scripts/reviewer_preflight.py",
            "scripts/trade_report_stats.py",
            "scripts/daily_report_writer.py",
            "scripts/validate_daily_report.py",
            "scripts/validate_periodic_report.py",
            "scripts/qq_push.py",
        ),
        "doc_tokens": (
            "account.db.daily_reports",
            "live_reconcile_status",
            "reports/monthly/",
            "平仓方向明细",
            "experience_summary_version=2",
        ),
        "schema": {
            "account.db": ("daily_reports", "weekly_reports", "monthly_reports"),
            "live_trades.db": ("trade_cycles", "trades"),
            "ledger.db": ("execution_intents",),
        },
    },
    "news": {
        "paths": (
            "collectors/news_writer.py",
            "collectors/record_xsearch.py",
            "scripts/run_okx_python.ps1",
        ),
        "doc_tokens": (
            "news.db.news_items", "ledger.db.collection_runs",
            "event_occurred_at", "primary_source_url",
        ),
        "schema": {
            "news.db": ("news_items", "news_events_index"),
            "ledger.db": ("collection_runs",),
        },
    },
}


def extract_sections(text: str) -> dict[str, str]:
    """提取稳定二级标题块；重复标题直接报错。"""
    matches = list(_H2_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1)
        if name in sections:
            raise ValueError(f"重复 section: {name}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[name] = text[match.end():end].strip()
    return sections


def extract_section(text: str, name: str) -> str:
    sections = extract_sections(text)
    if name not in sections:
        raise ValueError(f"缺 section: {name}")
    return sections[name]


def _quickref_chars(text: str) -> int:
    match = re.search(
        r"(?ms)^### QUICKREF\s*$\n(.*?)(?=^## |^### [A-Z_]+\s*$|\Z)",
        text,
    )
    return len(match.group(1).strip()) if match else 0


def structure_problems(label: str, text: str) -> list[str]:
    problems: list[str] = []
    try:
        sections = extract_sections(text)
    except ValueError as exc:
        return [f"{label} {exc}"]

    actual = [m.group(1) for m in _H2_RE.finditer(text)]
    if actual != list(REQUIRED_SECTIONS):
        problems.append(
            f"{label} 稳定块必须且只能按顺序为 {list(REQUIRED_SECTIONS)}，实际={actual}"
        )
    for name in REQUIRED_SECTIONS:
        if not sections.get(name, "").strip():
            problems.append(f"{label} {name} 为空或缺失")

    db_access = sections.get("DB_ACCESS", "")
    for token in ("READ", "VIA ", "DENY"):
        if token not in db_access:
            problems.append(f"{label} DB_ACCESS 缺权限态 {token.strip()}")
    for token in ("<PROJECT_ROOT>/db/", "schema.sql", "<PROJECT_ROOT>/tmp/"):
        if token not in text:
            problems.append(f"{label} 缺项目路径事实: {token}")
    if re.search(r"(?m)^##\s+\d+[.、]", text):
        problems.append(f"{label} 仍使用固定编号章节")
    if "本节无" in text:
        problems.append(f"{label} 含凑章节占位“本节无”")

    quickref_chars = _quickref_chars(text)
    if quickref_chars > QUICKREF_MAX_CHARS:
        problems.append(
            f"{label} QUICKREF={quickref_chars} 字符，超过 {QUICKREF_MAX_CHARS}"
        )
    return problems


def _schema_db_section(schema_text: str, db_name: str) -> str:
    marker = f"-- 数据库: {db_name}"
    start = schema_text.find(marker)
    if start < 0:
        return ""
    next_start = schema_text.find("-- 数据库:", start + len(marker))
    return schema_text[start:next_start if next_start >= 0 else len(schema_text)]


def project_fact_problems(role: str, text: str, root: Path, schema_text: str) -> list[str]:
    """让文档中的小型路径/表清单同时对得上文件系统与 schema 静态导出。"""
    problems: list[str] = []
    facts = ROLE_FACTS[role]
    for relative in facts["paths"]:
        if relative not in text:
            problems.append(f"{role} 文档缺角色入口: {relative}")
        if not (root / Path(relative)).is_file():
            problems.append(f"{role} 项目入口不存在: {relative}")
    for token in facts["doc_tokens"]:
        if token not in text:
            problems.append(f"{role} 文档缺数据库事实: {token}")
    for db_name, tables in facts["schema"].items():
        section = _schema_db_section(schema_text, db_name)
        if not section:
            problems.append(f"schema.sql 缺数据库段: {db_name}")
            continue
        for table in tables:
            if not re.search(rf"(?m)^CREATE TABLE\s+{re.escape(table)}\s*\(", section):
                problems.append(f"schema.sql 缺 {db_name}.{table}")
    return problems


def role_isolation_problems(role: str, text: str) -> list[str]:
    """只禁其它角色可执行策略；DENY 行中的角色名本身允许存在。"""
    problems: list[str] = []
    forbidden: dict[str, tuple[str, ...]] = {
        # 2026-08-06 demo 全量下线：live 不再需要与 demo 做策略隔离。
        "news": (
            "open_position(",
            "close_position(",
            "risk_validator",
            "sl_trigger_px",
            "account max-size",
            "projected_portfolio_imr_ratio",
        ),
    }
    for token in forbidden.get(role, ()):
        if token in text:
            problems.append(f"{role} 含其它角色执行策略: {token}")

    if role == "reviewer":
        for token in (
            "账本自愈和修复由确定性系统负责",
            "禁止加 `--apply`",
            "禁止推断系统自动策略",
        ):
            if token not in text:
                problems.append(f"reviewer 缺稳定自愈边界: {token}")
        for token in ("GHOST-EXACT", "UNRECORDED", "自动 --apply", "live 永远 dry"):
            if token in text:
                problems.append(f"reviewer 含易漂移自愈策略: {token}")
    return problems


def receipt_contract_problems(label: str, text: str) -> list[str]:
    """守住真实/模拟资金路径所需的最小可见契约。"""
    problems: list[str] = []
    required = {
        "安全文件名": "YYYY-MM-DDTHH-MM.json",
        "同进程提交": "同一个临时 Python 进程",
        "成交前上下文": "receipt_context",
        "执行入口": "order_executor",
        "止损输入": "sl_trigger_px",
    }
    if label == "live":
        required.update({
            "同进程 writer": 'commit_receipt(receipt, "live")',
            "当前组合 IMR": "risk.math.account_imr",
            "预计组合 IMR": "projected_portfolio_imr_ratio",
            "组合 IMR 来源": "portfolio_imr_source=account.balance.imr",
            "超限整单拒绝": "整笔 reject OPEN/ADD，不 clamp",
            "禁错误比率替代": "mgnRatio",
            "减仓不受开仓闸": "CLOSE/REDUCE",
        })

    for name, token in required.items():
        if token not in text:
            problems.append(f"{label} 契约缺 {name}: {token}")

    safe_write_tokens = (
        f'Path("<PROJECT_ROOT>/tmp/_receipt_{label}_YYYY-MM-DDTHH-MM.json").write_text',
        f"write path=<PROJECT_ROOT>/tmp/_receipt_{label}_YYYY-MM-DDTHH-MM.json",
    )
    if not any(token in text for token in safe_write_tokens):
        problems.append(f"{label} 缺 UTF-8 安全回执文件入口")

    for obsolete in (
        "MAX_" + "MARGIN_PCT=" + "0." + str(20),
        "risk.single_trade_" + "margin_pct",
        "单笔保证金 ≤" + str(20) + "%",
    ):
        if obsolete in text:
            problems.append(f"{label} 仍含废弃单笔百分比契约: {obsolete}")
    if re.search(r"(?mi)^\s*Set-Content\b", text):
        problems.append(f"{label} 仍含可执行 Set-Content 示例")
    if re.search(r"(?mi)^\s*pwsh\b.*\s-Command\b", text):
        problems.append(f"{label} 仍含嵌套 pwsh -Command 示例")
    if f"_receipt_{label}_<cycle_id>" in text:
        problems.append(f"{label} 回执文件仍直接拼 raw cycle_id")
    return problems


def live_money_path_problems(text: str) -> list[str]:
    """守住 live 的 RUN_OUTPUT 同进程执行与主账提交边界。"""
    try:
        section = extract_section(text, "RUN_OUTPUT")
    except ValueError as exc:
        return [f"live {exc}，无法核验同进程落账契约"]
    problems: list[str] = []
    if 'commit_receipt(receipt, "live")' not in section:
        problems.append('live RUN_OUTPUT 缺 commit_receipt(receipt, "live")')
    if "同一个临时 Python 进程" not in section:
        problems.append("live RUN_OUTPUT 缺同进程提交约束")
    if re.search(
        r"交易回执喂 writer[^\n]*trades_writer\.py --json-file "
        r"<tmp 回执文件>",
        section,
    ):
        problems.append("live RUN_OUTPUT 仍要求成交后分步调用 trades_writer.py")
    return problems


def compare(live_text: str, demo_text: str | None = None) -> list[str]:
    """兼容旧调用名（demo_text 自 2026-08-06 起忽略）。"""
    return structure_problems("live", live_text)


def main() -> int:
    ap = argparse.ArgumentParser(description="四角色本地文档与 trader money-path 契约检查")
    ap.add_argument("--agents-root", default=str(ROOT / "agents"))
    ap.add_argument("--live", help="兼容入口：覆盖 live_trader.md 路径")
    args = ap.parse_args()

    agents_root = Path(args.agents_root)
    doc_paths = {
        "analyst": agents_root / "analyst.md",
        "live": Path(args.live) if args.live else agents_root / "live_trader.md",
        "reviewer": agents_root / "reviewer.md",
        "news": agents_root / "news_scout.md",
    }
    texts = {role: path.read_text(encoding="utf-8") for role, path in doc_paths.items()}
    schema_text = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")

    problems: list[str] = []
    for role, role_text in texts.items():
        problems.extend(structure_problems(role, role_text))
        problems.extend(project_fact_problems(role, role_text, ROOT, schema_text))
        problems.extend(role_isolation_problems(role, role_text))
    live_text = texts["live"]
    problems.extend(receipt_contract_problems("live", live_text))
    problems.extend(live_money_path_problems(live_text))

    if problems:
        print("角色文档契约失败：")
        for problem in problems:
            print(" -", problem)
        return 1

    total_chars = sum(len(role_text) for role_text in texts.values())
    if total_chars > SOFT_TOTAL_CHARS:
        print(f"[WARN] 四份角色文档共 {total_chars} chars，软上限 {SOFT_TOTAL_CHARS}")
    print(
        "四角色文档契约通过：稳定块、三态权限、项目事实、角色隔离与 money-path 均有效"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
