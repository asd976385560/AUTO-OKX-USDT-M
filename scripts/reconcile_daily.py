# -*- coding: utf-8 -*-
r"""reconcile_daily.py — 日频交易所侧确定性对账编排。

reconcile_exchange_closes.py 本体不动（比对/分类/--apply 全复用），本脚本只做编排。

2026-08-06 demo 全量下线后只剩 live 一盘；原「demo 自动 --apply 自愈 / live 只 dry」
的分级（主人 2026-07-16 拍板）退化为单盘：
  live → **本脚本永远只 dry**；rc∈{1,3} → P1 告警附人工命令，本脚本绝不自动 --apply。
         （2026-08-04 口径变更：live 的 GHOST-EXACT 幽灵仓已由 scripts/ledger_autoheal.py
          在交易环节自动补账——起棒前 + pretrade 拒单前各一次。所以日维护跑到这里时
          GHOST-EXACT 通常已归零；本脚本的 live 人工告警实际只覆盖
          FUZZY / OVER_CLOSED / UNRECORDED 三类。本脚本自身行为未变。）
  rc=2（API/账本错误）→ P1。
推送经 scripts/qq_push.py，--dedupe-key reconcile:{YYYYMMDD}（显式身份键契约；
当日重跑同键去重、送达失败可重试）。
exit：0=全清 1=有告警已推 2=编排自身错误。

用法：
  reconcile_daily.py               # 真跑（告警真推 QQ）
  reconcile_daily.py --dry-run     # 只 dry 检，不推送，只打报告
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _proc  # 子进程超时整树杀（详见模块 docstring）

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
PWSH = os.environ.get("OKX_PWSH_BIN", r"C:\Program Files\PowerShell\7\pwsh.exe")
WRAP = r"./scripts/run_okx_python.ps1"
RECON = r"./scripts/reconcile_exchange_closes.py"
QQ_PUSH = r"./scripts/qq_push.py"
LOG_DIR = Path(r"./logs/reconcile")
_CREATE_NO_WINDOW = 0x08000000
_MARK_WORDS = ("[GHOST", "[OVER_CLOSED", "[UNRECORDED", "[LEFTOVER")
# LEDGER_DB / _APPLIED_CYCLE_RE 随 demo 自动 --apply 自愈路径一并移除（2026-08-06）。


def now_cst() -> datetime:
    return datetime.now(CST)


def log(msg: str) -> None:
    print(f"[{now_cst():%Y-%m-%d %H:%M:%S}] RECONCILE: {msg}", flush=True)


def run_recon(profile: str, apply: bool = False) -> tuple[int, str]:
    """跑一次 reconcile_exchange_closes（经 wrapper，180s 超时容 OKX API 往返）。"""
    cmd = [PWSH, "-NoProfile", "-File", WRAP, RECON, "--profile", profile]
    if apply:
        cmd.append("--apply")
    try:
        # 超时走整树杀：只杀 pwsh 会留下 reconcile_exchange_closes 持有管道，
        # 回收阶段二次阻塞，180s 超时形同虚设（详见 _proc 模块）。
        rc, out, err, _to = _proc.run_guarded(
            cmd, timeout=180, creationflags=_CREATE_NO_WINDOW)
        return rc, out + err
    except Exception as e:
        return 99, f"__RUN_ERR__ {type(e).__name__}: {e}"


def _findings(out: str, cap: int = 8) -> str:
    lines: list[str] = []
    capture_children = False
    for raw in out.splitlines():
        text = raw.strip()
        if any(w in text for w in _MARK_WORDS):
            lines.append(text)
            capture_children = (
                text.startswith("[OVER_CLOSED]")
                or text.startswith("[UNRECORDED]")
            )
            continue
        if capture_children and raw[:1].isspace() and text:
            # OVER_CLOSED / UNRECORDED 的 marker 只含组数，真实 symbol 在后续
            # 缩进行；GHOST 的长 fills 诊断不纳入 QQ 摘要。
            lines.append(text)
            continue
        capture_children = False
    return " | ".join(lines[:cap]) or "(无分类行，见完整输出)"


def _live_classification(rc: int, out: str) -> str:
    parts: list[str] = []
    if "[GHOST-EXACT]" in out:
        parts.append("GHOST-EXACT 可补")
    if "[GHOST-FUZZY]" in out:
        parts.append("GHOST-FUZZY 含糊")
    if "[OVER_CLOSED]" in out:
        parts.append("OVER_CLOSED 缺 open 需人工")
    if "[UNRECORDED]" in out:
        parts.append("UNRECORDED 缺 open 需人工")
    if "[LEFTOVER]" in out:
        parts.append("LEFTOVER 需人工")
    if not parts:
        parts.append("EXACT可修" if rc == 1 else "FUZZY含糊")
    return " + ".join(parts)


# `_applied_cycles` / `_cycles_without_push` 随 2026-08-06 demo 全量下线移除：
# 二者只服务 demo 的自动 --apply 自愈路径（补账后标记哪些轮没推过历史消息），
# live 侧永远只 dry，不存在自动补账，因此没有调用方。


def main() -> int:
    ap = argparse.ArgumentParser(description="日频交易所侧对账分级编排")
    ap.add_argument("--dry-run", action="store_true",
                    help="只 dry 检，不推送")
    args = ap.parse_args()

    alerts: list[str] = []
    report: dict = {"ts": f"{now_cst():%Y-%m-%d %H:%M:%S}", "dry_run": args.dry_run}

    # 2026-08-06 demo 全量下线：原先 demo 先跑并可自动 --apply 自愈，live 只 dry。
    # demo 分级整段移除后只剩 live，分级编排退化为「永远只 dry + P1 人工」。
    for profile in ("live",):
        rc, out = run_recon(profile)
        entry: dict = {"rc": rc, "findings": _findings(out)}
        report[profile] = entry
        if rc == 0:
            # 核验修（2026-07-16）：底层脚本 rc 只反映 GHOST（exact=1/fuzzy=3）——
            # [UNRECORDED]（交易所有仓账本无 open）/[OVER_CLOSED]（账本净负）是 print-only
            # 照样 rc=0。这两类恰是 journal 安全网兜不住的缺口（成交前进程死/带外单），
            # 必须从输出文本捞出来 P1，否则日频对账对"缺 open"类永远静默。
            silent = [w for w in ("[UNRECORDED]", "[OVER_CLOSED]") if w in out]
            if silent:
                alerts.append(f"[P1] {profile} 对账 rc=0 但存在 "
                              f"{'/'.join(silent)}（缺 open/净负持仓，需人工）: "
                              f"{entry['findings']}")
            else:
                log(f"{profile} 对账一致 rc=0")
            continue
        if rc in (99, 2) or rc not in (1, 3):
            alerts.append(f"[P1] {profile} 对账执行错误 rc={rc}: {entry['findings']}")
            continue
        # 主人拍板：live 永远只 dry+P1 人工，绝不自动 --apply
        classification = _live_classification(rc, out)
        apply_hint = (
            "｜人工: reconcile_exchange_closes.py --profile live "
            "--apply --ordid <已核实的精确ordId>"
            "（逐笔；只补 GHOST-EXACT，OVER_CLOSED/UNRECORDED/LEFTOVER 不会补）"
            if "[GHOST-EXACT]" in out
            else "｜人工逐笔核实（含糊项禁止 --apply）"
        )
        alerts.append(
            f"[P1] live 账实差异 rc={rc}({classification}): "
            f"{entry['findings']}{apply_hint}")

    report["alerts"] = alerts
    messages = alerts
    if messages and not args.dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = f"{now_cst():%Y%m%d}"
        # notices/P2 自愈通知随 demo 自动 --apply 一并移除，只剩 live 的 P1 告警。
        level = "P1"
        dedupe = f"reconcile:{stamp}"
        text = (f"⚠️ 日频对账告警 [{level}] [{f'{now_cst():%Y-%m-%d %H:%M}'}] (统一QQ告警)\n"
                + "\n".join(f"· {a}" for a in messages)
                + "\n（reconcile_daily：live 只 dry + P1 人工，绝不自动 --apply）")
        f = LOG_DIR / f"alert_{stamp}.txt"
        f.write_text(text, encoding="utf-8")
        cmd = [PWSH, "-NoProfile", "-File", WRAP, QQ_PUSH,
               "--content-file", str(f), "--alert",  # 告警走 C2C 私聊（2026-08-04）
               "--dedupe-key", dedupe]
        try:
            rc, _out, _err, _to = _proc.run_guarded(
                cmd, timeout=60, creationflags=_CREATE_NO_WINDOW)
            report["alert_sent"] = {"rc": rc}
            log(f"告警已推 rc={rc}: {len(messages)} 项")
        except Exception as e:
            report["alert_sent"] = {"error": str(e)}
    elif messages:
        log(f"[dry-run] 将告警 {len(messages)} 项（未推）")

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 1 if alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
