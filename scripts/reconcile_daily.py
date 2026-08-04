# -*- coding: utf-8 -*-
r"""reconcile_daily.py — 日频交易所侧确定性对账编排。

reconcile_exchange_closes.py 本体不动（比对/分类/--apply 全复用），本脚本只做分级编排
（主人 2026-07-16 拍板）：
  demo → dry 检；rc=1（GHOST-EXACT 可修）→ --apply 自动补账 → dry 复检验证归零；
         rc=3（FUZZY/含糊）→ P1 人工。
  live → **本脚本永远只 dry**；rc∈{1,3} → P1 告警附人工命令，本脚本绝不自动 --apply。
         （2026-08-04 口径变更：live 的 GHOST-EXACT 幽灵仓已由 scripts/ledger_autoheal.py
          在交易环节自动补账——起棒前 + pretrade 拒单前各一次。所以日维护跑到这里时
          GHOST-EXACT 通常已归零；本脚本的 live 人工告警实际只覆盖
          FUZZY / OVER_CLOSED / UNRECORDED 三类。本脚本自身行为未变。）
  rc=2（API/账本错误）→ 两盘均 P1。
推送经 scripts/qq_push.py，--dedupe-key reconcile:{YYYYMMDD}（显式身份键契约；
当日重跑同键去重、送达失败可重试）。
exit：0=全清（含 demo 自愈） 1=有告警已推 2=编排自身错误。

用法：
  reconcile_daily.py               # 真跑（demo 可自动 --apply、告警真推 QQ）
  reconcile_daily.py --dry-run     # 两盘只 dry 检，不 apply 不推送，只打报告
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
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
PWSH = os.environ.get("OKX_PWSH_BIN", r"C:\Program Files\PowerShell\7\pwsh.exe")
WRAP = _project_path('scripts', 'run_okx_python.ps1')
RECON = _project_path('scripts', 'reconcile_exchange_closes.py')
QQ_PUSH = _project_path('scripts', 'qq_push.py')
LOG_DIR = Path(_project_path('logs', 'reconcile'))
_CREATE_NO_WINDOW = 0x08000000
_MARK_WORDS = ("[GHOST", "[OVER_CLOSED", "[UNRECORDED", "[LEFTOVER")
LEDGER_DB = Path(_project_path('db', 'ledger.db'))
_APPLIED_CYCLE_RE = re.compile(r"\[APPLIED\].*?\bcycle=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})")


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
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180,
                           creationflags=_CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
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


def _applied_cycles(out: str) -> list[str]:
    return sorted(set(_APPLIED_CYCLE_RE.findall(out or "")))


def _cycles_without_push(cycles: list[str]) -> list[str]:
    """补账只修账，不补历史 push；只读 ledger 给运维通知精确标记缺失轮。"""
    if not cycles or not LEDGER_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{LEDGER_DB.as_posix()}?mode=ro",
                              uri=True, timeout=8)
        try:
            rows = con.execute(
                "SELECT cycle_id FROM stage_dispatch WHERE stage='push' "
                f"AND cycle_id IN ({','.join('?' for _ in cycles)})",
                cycles,
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return []
    pushed = {str(r[0]) for r in rows}
    return [c for c in cycles if c not in pushed]


def main() -> int:
    ap = argparse.ArgumentParser(description="日频交易所侧对账分级编排")
    ap.add_argument("--dry-run", action="store_true",
                    help="两盘只 dry 检，不 apply 不推送")
    args = ap.parse_args()

    alerts: list[str] = []
    notices: list[str] = []
    report: dict = {"ts": f"{now_cst():%Y-%m-%d %H:%M:%S}", "dry_run": args.dry_run}

    for profile in ("demo", "live"):
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
        if profile == "live":
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
            continue
        # demo 分级
        if rc == 3:
            alerts.append(f"[P1] demo 对账含糊(FUZZY/UNRECORDED)需人工: {entry['findings']}")
            continue
        # rc == 1：GHOST-EXACT 可修
        if args.dry_run:
            entry["would_apply"] = True
            log(f"[dry-run] demo rc=1，将自动 --apply（未执行）")
            continue
        rc_apply, out_apply = run_recon("demo", apply=True)
        rc_check, out_check = run_recon("demo")
        entry.update({"rc_apply": rc_apply, "rc_recheck": rc_check})
        if rc_apply == 0 and rc_check == 0:
            entry["self_healed"] = True
            applied_cycles = _applied_cycles(out_apply)
            missing_push = _cycles_without_push(applied_cycles)
            entry["applied_cycles"] = applied_cycles
            entry["missing_historical_push"] = missing_push
            suffix = (f"；原轮未推送={missing_push}，按策略不补发历史交易消息"
                      if missing_push else "")
            notices.append(
                f"[P2] demo 对账已自动补账并复检归零，cycles={applied_cycles or ['unknown']}"
                f"{suffix}")
            log(f"demo GHOST-EXACT 已自动补账并复检归零{suffix}")
        else:
            alerts.append(
                f"[P1] demo 对账 --apply 后未归零 rc_apply={rc_apply} "
                f"rc_recheck={rc_check}: {_findings(out_apply + out_check)}")

    report["alerts"] = alerts
    report["notices"] = notices
    messages = alerts + notices
    if messages and not args.dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = f"{now_cst():%Y%m%d}"
        level = "P1" if alerts else "P2"
        dedupe = f"reconcile:{stamp}" if alerts else f"reconcile-selfheal:{stamp}"
        text = (f"⚠️ 日频对账告警 [{level}] [{f'{now_cst():%Y-%m-%d %H:%M}'}] (统一QQ告警)\n"
                + "\n".join(f"· {a}" for a in messages)
                + "\n（reconcile_daily 分级编排：demo 自动/live 人工，2026-07-16 拍板）")
        f = LOG_DIR / f"alert_{stamp}.txt"
        f.write_text(text, encoding="utf-8")
        cmd = [PWSH, "-NoProfile", "-File", WRAP, QQ_PUSH,
               "--content-file", str(f), "--alert",  # 告警走 C2C 私聊（2026-08-04）
               "--dedupe-key", dedupe]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60,
                               creationflags=_CREATE_NO_WINDOW)
            report["alert_sent"] = {"rc": p.returncode}
            log(f"告警已推 rc={p.returncode}: {len(messages)} 项")
        except Exception as e:
            report["alert_sent"] = {"error": str(e)}
    elif messages:
        log(f"[dry-run] 将告警 {len(messages)} 项（未推）")

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 1 if alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
