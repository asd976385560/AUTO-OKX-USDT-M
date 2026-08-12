# -*- coding: utf-8 -*-
"""push_pipeline.py — 由 dispatcher 触发的纯脚本推送编排器。

一条确定性链，无 LLM 参与：
  build_push_payload → render_push_report → validate_push_format
    → push_archive hard-check → qq_push（--no-send 跳过）→ system_state_writer → 环节报告

幂等：dispatcher 的 ledger.stage_dispatch(cycle,'push') 闩锁保每 cycle 单发；
qq_push 层用显式 --dedupe-key push:{cycle}。
故本脚本可安全重跑。

每环节出详细报告：reports/push/pipeline-<cycle>.json + 追加 logs/push/pipeline_runs.jsonl，
含 build/render/validate/send/archive/state 各步 rc 与关键指标。

用法（阶段一开发，安全）:
  push_pipeline.py --cycle 2026-07-07T12:00 --no-send        # build→render→validate→archive，不外发
用法（阶段二生产，dispatcher 起）:
  push_pipeline.py --cycle 2026-07-07T12:00                  # 全链含外发
"""
from __future__ import annotations

import argparse
import importlib.util as ilu
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _proc  # 子进程超时整树杀（详见模块 docstring）
from collectors.cycle_contract import (  # noqa: E402
    cycle_artifact_token,
    validate_cycle_id,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
OKX = r"."
SCRIPTS = r"./scripts"
WRAP = r"./scripts/run_okx_python.ps1"
# 绝对 pwsh 路径——对齐 okx-* cron（cron 进程 PATH 不保证有 pwsh）；env 可覆盖
PWSH = os.environ.get("OKX_PWSH_BIN", r"C:\Program Files\PowerShell\7\pwsh.exe")
# 组装器（2026-07-07 已迁 scripts/）；env 可覆盖
BUILD_PY = os.environ.get("OKX_BUILD_PY", r"./scripts/build_push_payload.py")
WORK = Path(r"./tmp/push_pipeline")
REPORT_DIR = Path(r"./reports/push")
RUNLOG = Path(r"./logs/push/pipeline_runs.jsonl")
_CREATE_NO_WINDOW = 0x08000000


def now_ts() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _run(script: str, args: list, stdin_text: str | None = None):
    """经 wrapper 跑脚本，返回 (rc, stdout, stderr)。

    必须保留 wrapper：本脚本由 trigger_agent 用**裸 python.exe** detached 起
    （见 trigger_agent.py:96 注释），自身 env 不含 PYTHONPATH/MX_APIKEY/代理，
    内层各步正是靠 wrapper 拿到它们。故此处只换执行方式（整树杀超时），不动 argv。
    """
    cmd = [PWSH, "-NoProfile", "-File", WRAP, script, *args]
    rc, out, err, _ = _proc.run_guarded(cmd, timeout=120, input_text=stdin_text,
                                        creationflags=_CREATE_NO_WINDOW)
    return rc, out.strip(), err.strip()


def _load_build():
    spec = ilu.spec_from_file_location("build_push_payload", BUILD_PY)
    m = ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _archive_hard_check(
    rc: int, receipt: dict, content_file: str
) -> tuple[bool, str | None]:
    """确认时间戳归档真实存在且完整包含本轮渲染正文，成功前禁止 send。"""
    if rc != 0:
        return False, f"archive_rc_{rc}"
    if receipt.get("ok") is not True:
        return False, "archive_receipt_not_ok"
    if receipt.get("degraded"):
        return False, "archive_degraded"
    archive_path = receipt.get("path")
    if not isinstance(archive_path, str) or not archive_path.strip():
        return False, "archive_path_missing"
    try:
        source = Path(content_file).read_text(encoding="utf-8")
        archived = Path(archive_path).read_text(encoding="utf-8")
        actual_bytes = Path(archive_path).stat().st_size
        declared_bytes = int(receipt.get("bytes"))
    except (OSError, TypeError, ValueError):
        return False, "archive_file_unreadable"
    if not source or not archived.endswith(source):
        return False, "archive_content_mismatch"
    if actual_bytes <= 0 or declared_bytes != actual_bytes:
        return False, "archive_size_mismatch"
    return True, None


def run(cycle: str, db_root: str, no_send: bool) -> dict:
    cycle = validate_cycle_id(cycle)
    WORK.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    RUNLOG.parent.mkdir(parents=True, exist_ok=True)
    safe = cycle_artifact_token(cycle)
    payload_f = str(WORK / f"payload-{safe}.json")
    content_f = str(WORK / f"content-{safe}.txt")

    rep: dict = {"cycle": cycle, "ts": now_ts(), "steps": {}, "ok": False}

    # 1. build（进程内直调，确定性组装）
    try:
        bpp = _load_build()
        payload = bpp.build(db_root, cycle)
        with open(payload_f, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        rep["steps"]["build"] = {"ok": True, "action": payload.get("action_taken"),
                                 "symbol": payload.get("symbol"),
                                 "n_trades": len(payload.get("trades", {}).get("live", []))
}
    except Exception as e:
        rep["steps"]["build"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        rep["fatal"] = "build_failed"
        return _finish(rep)

    # 2. render：透传 db_root，确保库权威覆盖读取同一根目录。
    rc, out, err = _run(r"./scripts/render_push_report.py",
                        ["--json-file", payload_f, "--out-file", content_f,
                         "--db-root", str(db_root)])
    receipt = {}
    try:
        receipt = json.loads(out) if out else {}
    except Exception:
        pass
    rep["steps"]["render"] = {"rc": rc, "bytes": receipt.get("bytes"),
                              "title": receipt.get("title"), "err": err[:200] if rc else None}
    if rc != 0 or not Path(content_f).exists():
        rep["fatal"] = "render_failed"
        return _finish(rep)

    # 3. validate（必须 rc=0 才外发）
    validate_args = ["--file", content_f, "--cycle-id", cycle]
    if no_send:
        validate_args.append("--no-repair-queue")
    rc, out, err = _run(
        r"./scripts/validate_push_format.py",
        validate_args,
    )
    vres = {}
    try:
        vres = json.loads(out) if out else {}
    except Exception:
        pass
    rep["steps"]["validate"] = {"rc": rc, "errors": vres.get("errors"),
                                "missing": vres.get("missing_fields"),
                                "char_count": vres.get("char_count")}
    if rc != 0:
        rep["fatal"] = "validate_failed"       # 不外发残缺内容（阶段二在此写 repair_queue + 告警）
        return _finish(rep)

    # 4. 归档前置硬闸：归档返回成功且文件内容核验完成，才允许进入 send。
    # --no-send 下归到 dev 目录，不覆写生产 latest.md。
    title = receipt.get("title") or f"push {cycle}"
    arch_in = json.dumps({"ts": now_ts(), "content_file": content_f, "title": title},
                         ensure_ascii=False)
    arch_args = ["--stdin"]
    if no_send:
        arch_args = ["--reports-dir", str(WORK / "reports"), "--stdin"]
    rc, out, err = _run(r"./scripts/push_archive.py", arch_args, stdin_text=arch_in)
    ares = {}
    try:
        ares = json.loads(out) if out else {}
    except Exception:
        pass
    archive_ok, archive_reason = _archive_hard_check(rc, ares, content_f)
    rep["steps"]["archive"] = {"rc": rc, "path": ares.get("path"), "bytes": ares.get("bytes"),
                               "degraded": ares.get("degraded"),
                               "hard_check": archive_ok}
    if archive_reason:
        rep["steps"]["archive"]["hard_check_error"] = archive_reason
    if not archive_ok:
        rep["steps"]["send"] = {"skipped": True, "reason": "archive_hard_check_failed"}
        rep["fatal"] = "archive_hard_check_failed"
        return _finish(rep)

    # 5. 外发（--no-send 跳过）。发送失败时，前置时间戳归档已经完整保留。
    if no_send:
        rep["steps"]["send"] = {"skipped": True, "reason": "--no-send"}
    else:
        # 显式身份键 push:{cycle}：同 cycle 任何 content 同键，重跑幂等。
        try:
            rc, out, err = _run(
                r"./scripts/qq_push.py",
                ["--content-file", content_f, "--dedupe-key", f"push:{cycle}"],
            )
            rep["steps"]["send"] = {
                "rc": rc,
                "out": out[:500],
                "err": err[:200] if rc else None,
            }
        except Exception as exc:
            # 归档已过硬闸；即使子进程启动/超时异常，也保留存证并继续写 failed 状态。
            rc, out = 1, ""
            rep["steps"]["send"] = {
                "rc": rc,
                "out": "",
                "err": f"{type(exc).__name__}: {exc}"[:200],
            }

    # 6. 状态落库（--no-send 下不真写生产 account.db，只报告将写什么）
    if no_send:
        status = "skipped"
    else:
        _send = rep["steps"].get("send", {})
        _o = (_send.get("out") or "").lower()
        if _send.get("rc") == 0:
            status = "duplicate_skip" if ("duplicate" in _o or "skip" in _o) else "sent"
        elif ("messageid" in _o) or ('"action": "send"' in _o):
            # rc!=0 但回执带 messageId=已投递（qq_push 送达后置步骤偶发报错，非漏推）
            status = "sent"
        else:
            status = "failed"
    if no_send:
        rep["steps"]["system_state"] = {"skipped": True,
                                        "would_write": {"push_last_cycle": cycle,
                                                        "push_last_status": status}}
    else:
        state_json = str(WORK / f"state-{safe}.json")
        with open(state_json, "w", encoding="utf-8") as f:
            json.dump({"updates": {"push_last_cycle": cycle, "push_last_status": status},
                       "ts": now_ts()}, f, ensure_ascii=False)
        rc, out, err = _run(r"./scripts/system_state_writer.py", ["--json-file", state_json])
        rep["steps"]["system_state"] = {"rc": rc}

    rep["send_status"] = status
    rep["ok"] = no_send or status in {"sent", "duplicate_skip"}
    if not rep["ok"]:
        rep["fatal"] = "send_failed"
    return _finish(rep)


def _finish(rep: dict) -> dict:
    """落环节报告 + 追加 run-log。"""
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        safe = rep["cycle"].replace(":", "").replace("T", "-")
        with open(REPORT_DIR / f"pipeline-{safe}.json", "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        RUNLOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RUNLOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rep, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[push_pipeline] WARN 报告落盘失败: {e}", file=sys.stderr)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description="纯脚本推送编排器")
    ap.add_argument("--cycle", required=True)
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--no-send", action="store_true", help="跳过 QQ 外发（阶段一开发用）")
    args = ap.parse_args()
    rep = run(args.cycle, args.db_root, args.no_send)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
