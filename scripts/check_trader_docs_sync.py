# -*- coding: utf-8 -*-
r"""check_trader_docs_sync.py — live/demo trader 手册同步块防漂移校验。

背景：两手册 44% 机械重复，规则改动要双改（07-15 同侧闸取消实测要改 4 份文档，漏一份
= agent 跑矛盾规则）。机制：共享块用 `<!-- SYNC:<name> -->` … `<!-- /SYNC:<name> -->`
标注（HTML 注释，agent 渲染无感），本脚本校验两文档同名块在 profile 词归一
（live/demo → §P§）后**逐字节一致**；并守住 money-path 回执文件契约（普通 HOLD
使用 write 工具；live 成交脚本允许同进程 Path.write_text→commit_receipt；
文件名一律 HH-MM、组合保证金不冒充单笔字段）。不一致 → exit 1 + 打差异。

选择"同步校验"而非"base+overlay 生成管线"（读图原案）的原因：手册是高频直编的
money-path 文档，生成管线引入"编辑生成物被下次 compose 覆盖"的新事故面；校验器
零改编辑习惯、零内容风险，同样根治漂移（单点强制而非单点编辑）。

用法：check_trader_docs_sync.py [--live <path>] [--demo <path>]   # exit 0=同步
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
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LIVE_DOC = Path(_project_path('agents', 'live_trader.md'))
DEMO_DOC = Path(_project_path('agents', 'demo_trader.md'))

_BLOCK_RE = re.compile(
    r"<!--\s*SYNC:([\w-]+)[^>]*-->\r?\n(.*?)<!--\s*/SYNC:\1\s*-->",
    re.DOTALL,
)


def extract_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for m in _BLOCK_RE.finditer(text):
        name = m.group(1)
        if name in blocks:
            raise ValueError(f"同名 SYNC 块重复: {name}")
        blocks[name] = m.group(2)
    return blocks


def normalize(block: str) -> str:
    """profile 词归一（live/demo → §P§，纯文本替换，两侧对称施加）+ 行尾空白/CRLF 归一。"""
    t = block.replace("live", "§P§").replace("demo", "§P§")
    return "\n".join(line.rstrip() for line in t.replace("\r\n", "\n").split("\n")).strip()


def compare(live_text: str, demo_text: str) -> list[str]:
    """返回不同步问题列表；空=同步。"""
    problems: list[str] = []
    lb = extract_blocks(live_text)
    db = extract_blocks(demo_text)
    if set(lb) != set(db):
        only_l = sorted(set(lb) - set(db))
        only_d = sorted(set(db) - set(lb))
        if only_l:
            problems.append(f"块只在 live 有: {only_l}")
        if only_d:
            problems.append(f"块只在 demo 有: {only_d}")
    for name in sorted(set(lb) & set(db)):
        nl, nd = normalize(lb[name]), normalize(db[name])
        if nl != nd:
            ll, dl = nl.split("\n"), nd.split("\n")
            diff_at = next((i for i, (a, b) in enumerate(zip(ll, dl)) if a != b),
                           min(len(ll), len(dl)))
            problems.append(
                f"块 [{name}] 漂移（归一后首异 @ 行 {diff_at + 1}）:\n"
                f"  live: {ll[diff_at] if diff_at < len(ll) else '<缺行>'}\n"
                f"  demo: {dl[diff_at] if diff_at < len(dl) else '<缺行>'}")
    return problems


def receipt_contract_problems(label: str, text: str) -> list[str]:
    """防止再次引入 shell JSON 转义失败或 raw cycle 冒号/NTFS ADS。"""
    problems: list[str] = []
    required = {
        "安全文件名": "YYYY-MM-DDTHH-MM.json",
        "单笔字段为空": "risk.single_trade_margin_pct=null",
        "组合观察字段": "risk.portfolio_observation.estimated_margin_pct_equity",
    }
    file_write_tokens = [f"write path=<PROJECT_ROOT>/tmp/_receipt_{label}_"]
    if label == "live":
        file_write_tokens.append(
            'Path("<PROJECT_ROOT>/tmp/_receipt_live_YYYY-MM-DDTHH-MM.json").write_text')
    if not any(token in text for token in file_write_tokens):
        problems.append(
            f"{label} 回执契约缺安全文件写入路径: {file_write_tokens}")
    for name, token in required.items():
        if token not in text:
            problems.append(f"{label} 回执契约缺 {name}: {token}")
    if re.search(r"(?mi)^\s*Set-Content\b", text):
        problems.append(f"{label} 仍含可执行 Set-Content 示例（会受 JSON/PowerShell 转义影响）")
    if re.search(r"(?mi)^\s*pwsh\b.*\s-Command\b", text):
        problems.append(f"{label} 仍含嵌套 pwsh -Command 示例")
    if f"_receipt_{label}_<cycle_id>" in text:
        problems.append(f"{label} 回执文件仍直接拼 raw cycle_id（冒号会变 NTFS ADS）")
    if label == "live":
        problems.extend(live_money_path_problems(text))
    return problems


def live_money_path_problems(text: str) -> list[str]:
    """守住 live 成交执行与主账提交的同进程边界。"""
    match = re.search(
        r"(?ms)^## 7\. 强制流程（每轮）\s*$.*?(?=^## 8\. 失败 / 降级\s*$)",
        text,
    )
    if not match:
        return ["live 手册缺 §7 强制流程，无法核验同进程落账契约"]
    section = match.group(0)
    problems: list[str] = []
    if 'commit_receipt(receipt, "live")' not in section:
        problems.append(
            'live §7 缺同进程 commit_receipt(receipt, "live") 强制步骤')
    if re.search(
            r"交易回执喂 writer[^\n]*trades_writer\.py --json-file "
            r"<tmp 回执文件>",
            section):
        problems.append(
            "live §7 仍要求成交后分步调用 trades_writer.py --json-file")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="trader 双手册同步块校验")
    ap.add_argument("--live", default=str(LIVE_DOC))
    ap.add_argument("--demo", default=str(DEMO_DOC))
    args = ap.parse_args()
    live_text = Path(args.live).read_text(encoding="utf-8")
    demo_text = Path(args.demo).read_text(encoding="utf-8")
    problems = compare(live_text, demo_text)
    problems.extend(receipt_contract_problems("live", live_text))
    problems.extend(receipt_contract_problems("demo", demo_text))
    if problems:
        print("trader 手册同步块漂移（改共享规则必须双文档同步）：")
        for p in problems:
            print(" -", p)
        return 1
    n = len(extract_blocks(live_text))
    print(f"trader 手册同步块一致（{n} 块）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
