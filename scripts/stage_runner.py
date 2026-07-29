# -*- coding: utf-8 -*-
"""Detached stage 监督包装器：记录 running/succeeded/failed，失败只告警不重试。

由 collectors/trigger_agent.py detached 拉起。本脚本同步等待真正的 agent/push
子进程，因此能取得最终退出码；dispatcher 仍只认 stage_dispatch 做幂等，本状态文件
仅用于终态可观测性。任何失败都不会释放闩锁、补派或重试。
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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


CST = timezone(timedelta(hours=8))
ROOT = Path(_project_path())
STATUS_DIR = Path(os.environ.get("OKX_STAGE_STATUS_DIR")
                  or _project_path('logs', 'stage-status'))
QQ_PUSH = ROOT / "scripts" / "qq_push.py"
LIVE_RECON_MONITOR = ROOT / "scripts" / "live_reconcile_monitor.py"
DB_ROOT = Path(os.environ.get("OKX_DB_ROOT") or _project_path('db'))
OPENCLAW_STATE_ROOT = Path(
    os.environ.get("OKX_OPENCLAW_STATE_ROOT")
    or (Path.home() / ".openclaw")
)
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_BUSINESS_FAILURE_RC = 86
_STAGE_AGENTS = {
    "analyst": "okx-analyst",
    "live": "okx-live-trader",
    "demo": "okx-demo-trader",
}


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _safe(value: str) -> str:
    return _SAFE_RE.sub("-", str(value)).strip("-")[:100] or "unknown"


def _stage_session_key(stage: str, cycle: str) -> str:
    safe_cycle = str(cycle).replace("-", "").replace(":", "").replace("T", "-")
    return f"{stage}-{safe_cycle}"


def _walk_dicts(value):
    """Yield nested dictionaries without retaining or emitting model metadata."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            yield item
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def detect_agent_terminal_failure(
    stage: str,
    cycle: str,
    state_root: Path | None = None,
) -> dict | None:
    """Read only the matching terminal reason; never persist model-chain data."""
    agent = _STAGE_AGENTS.get(stage)
    if not agent:
        return None
    root = Path(state_root or OPENCLAW_STATE_ROOT)
    session_dir = root / "agents" / agent / "sessions"
    index_path = session_dir / "sessions.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        lookup_key = f"agent:{agent}:{_stage_session_key(stage, cycle)}"
        entry = index.get(lookup_key)
        if not isinstance(entry, dict) or not entry.get("sessionId"):
            return None
        trajectory = session_dir / f"{entry['sessionId']}.trajectory.jsonl"
        stop_reason = None
        terminal_error = None
        total_tokens = None
        with trajectory.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if record.get("type") == "trace.artifacts":
                    data = record.get("data")
                    if isinstance(data, dict) and data.get("terminalError"):
                        terminal_error = str(data["terminalError"])[:160]
                for item in _walk_dicts(record.get("data")):
                    if str(item.get("stopReason") or "").lower() == "length":
                        stop_reason = "length"
                        usage = item.get("usage")
                        if isinstance(usage, dict):
                            try:
                                total_tokens = int(usage.get("totalTokens"))
                            except (TypeError, ValueError):
                                pass
        if stop_reason != "length":
            return None
        result = {
            "failure_kind": "model_output_length",
            "stop_reason": stop_reason,
        }
        if terminal_error:
            result["terminal_error"] = terminal_error
        if total_tokens is not None:
            result["total_tokens"] = total_tokens
        return result
    except (OSError, ValueError, TypeError):
        return None


def _status_path(stage: str, cycle: str) -> Path:
    return STATUS_DIR / f"{_safe(stage)}-{_safe(cycle)}.json"


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def _send_failure_alert(stage: str, cycle: str, rc: int,
                        status_path: Path,
                        failure_detail: dict | None = None) -> dict:
    if os.environ.get("OKX_STAGE_RUNNER_NO_ALERT") == "1":
        return {"skipped": "OKX_STAGE_RUNNER_NO_ALERT=1"}
    alert_file = STATUS_DIR / f"alert-{_safe(stage)}-{_safe(cycle)}.txt"
    detail_line = ""
    if failure_detail:
        detail_line = (
            "· 业务后置校验："
            + json.dumps(failure_detail, ensure_ascii=False, separators=(",", ":"))[:900]
            + "\n"
        )
    alert_file.write_text(
        f"⚠️ OKX 阶段执行失败 [P1]\n"
        f"· stage={stage} cycle={cycle} rc={rc}\n"
        f"{detail_line}"
        f"· 已记录 failed 终态：{status_path}\n"
        f"· 处置：只告警，不自动补派/重试；请核 trigger 日志与账本。\n",
        encoding="utf-8",
    )
    try:
        p = subprocess.run(
            [sys.executable, str(QQ_PUSH), "--content-file", str(alert_file),
             "--dedupe-key", f"stage-failed:{stage}:{cycle}"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
            creationflags=_CREATE_NO_WINDOW,
        )
        # qq_push 的 stdout 可能包含 messageId、接收目标及完整 payload。
        # stage-status 只需要投递终态，禁止复制这些通道标识；详细故障留在
        # qq_push 自身日志中排查。
        result = {
            "rc": int(p.returncode),
            "delivered": p.returncode == 0,
        }
        if p.returncode != 0:
            result["error"] = "qq_push exited non-zero; inspect dedicated push logs"
        return result
    except Exception as exc:  # 告警失败不能掩盖原始 stage 终态
        return {"error": f"{type(exc).__name__}: {exc}"}


def _row_exists(db_root: Path, filename: str, table: str,
                cycle: str, columns: str = "1") -> tuple[bool, dict | None]:
    path = db_root / filename
    if not path.exists():
        raise FileNotFoundError(f"业务库不存在: {path}")
    con = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=8)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            f"SELECT {columns} FROM {table} WHERE cycle_id=? LIMIT 1",
            (cycle,),
        ).fetchone()
    finally:
        con.close()
    return row is not None, (dict(row) if row is not None else None)


def verify_business_output(stage: str, cycle: str, mode: str,
                           db_root: Path | None = None) -> dict:
    """只读验证 stage 的确定性业务产物。

    runner 子进程 rc=0 只代表 OpenClaw/脚本进程结束，不代表 writer 已落库。
    本校验绝不释放 stage_dispatch、补派或重试；异常按 fail-closed 返回。
    unified gate 主动写 skipped/stale 时按合法无交易终态处理。
    """
    root = Path(db_root or DB_ROOT)
    checks: list[dict] = []

    def require(filename: str, table: str,
                columns: str = "1") -> dict | None:
        found, row = _row_exists(root, filename, table, cycle, columns)
        checks.append({"db": filename, "table": table, "found": found})
        if not found:
            raise LookupError(f"{filename}.{table}[{cycle}] 缺失")
        return row

    try:
        if stage == "live" and mode == "unified":
            analysis = require(
                "analysis.db", "analysis_runs", "status,ts,mode")
            analysis_status = str((analysis or {}).get("status") or "").lower()
            if analysis_status in ("skipped", "stale"):
                return {
                    "ok": True,
                    "terminal": f"analysis_{analysis_status}",
                    "checks": checks,
                }
            if analysis_status != "ok":
                raise RuntimeError(
                    f"analysis status={analysis_status or 'missing'} 非可交易终态")
            require("live_trades.db", "trade_cycles",
                    "decision,n_orders,ts")
        elif stage == "live":
            require("live_trades.db", "trade_cycles",
                    "decision,n_orders,ts")
        elif stage == "demo":
            require("demo_trades.db", "trade_cycles",
                    "decision,n_orders,ts")
        elif stage == "analyst":
            require("analysis.db", "analysis_runs", "status,ts,mode")
        else:
            return {"ok": True, "skipped": f"stage={stage} 无额外业务后置条件"}
        return {"ok": True, "checks": checks}
    except LookupError as exc:
        return {
            "ok": False,
            "failure_kind": "business_output_missing",
            "error": str(exc),
            "checks": checks,
        }
    except Exception as exc:
        return {
            "ok": False,
            "failure_kind": "business_verification_error",
            "error": f"{type(exc).__name__}: {exc}",
            "checks": checks,
        }


def _run_post_push_monitor(cycle: str) -> dict:
    """push 后运行 live-only dry reconciliation；告警由 monitor 自己去重。

    该检查永远不改变 push stage 的成功/失败，也不 apply/replay。
    """
    if os.environ.get("OKX_POST_PUSH_RECONCILE", "1") == "0":
        return {"skipped": "OKX_POST_PUSH_RECONCILE=0"}
    try:
        proc = subprocess.run(
            [sys.executable, str(LIVE_RECON_MONITOR), "--cycle", cycle],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=240, creationflags=_CREATE_NO_WINDOW)
        return {
            "rc": int(proc.returncode),
            "output": ((proc.stdout or "") + (proc.stderr or ""))[-2000:],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser(description="OKX detached stage lifecycle runner")
    ap.add_argument("--stage", required=True)
    ap.add_argument("--cycle", required=True)
    ap.add_argument("--mode", default="full")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        ap.error("缺少 -- 后的实际命令")

    started_mono = time.monotonic()
    path = _status_path(args.stage, args.cycle)
    status = {
        "stage": args.stage,
        "cycle_id": args.cycle,
        "mode": args.mode,
        "status": "running",
        "started_at": now_cst(),
        "runner_pid": os.getpid(),
    }
    _write_status(path, status)
    try:
        # runner 自身由 DETACHED_PROCESS 拉起；其内部再次启动 console 程序时，
        # Windows 仍可能新建控制台。内层只用 CREATE_NO_WINDOW（不再叠 DETACHED），
        # 保持可等待/取退出码，同时彻底阻止 openclaw-agent 定时弹窗。
        proc = subprocess.run(
            command, cwd=str(ROOT), creationflags=_CREATE_NO_WINDOW)
        child_rc = int(proc.returncode)
        error = None
    except Exception as exc:
        child_rc = 127
        error = f"{type(exc).__name__}: {exc}"

    rc = child_rc
    business_check = None
    failure_kind = None
    terminal_evidence = None
    if child_rc == 0:
        business_check = verify_business_output(
            args.stage, args.cycle, args.mode)
        if not business_check.get("ok"):
            rc = _BUSINESS_FAILURE_RC
            failure_kind = business_check.get(
                "failure_kind", "business_verification_error")
    if rc != 0:
        terminal_evidence = detect_agent_terminal_failure(
            args.stage, args.cycle)
        if terminal_evidence:
            failure_kind = terminal_evidence["failure_kind"]
            if business_check is not None:
                business_check = {
                    **business_check,
                    "failure_kind": failure_kind,
                    "terminal_evidence": terminal_evidence,
                }

    status.update({
        "status": "succeeded" if rc == 0 else "failed",
        "finished_at": now_cst(),
        "duration_ms": int((time.monotonic() - started_mono) * 1000),
        "child_returncode": child_rc,
        "returncode": rc,
    })
    if error:
        status["error"] = error
    if business_check is not None:
        status["business_check"] = business_check
    if failure_kind:
        status["failure_kind"] = failure_kind
    if terminal_evidence:
        status["agent_terminal_evidence"] = terminal_evidence
    if rc == 0 and args.stage == "push":
        status["post_live_reconcile"] = _run_post_push_monitor(args.cycle)
    _write_status(path, status)
    if rc != 0:
        status["alert"] = _send_failure_alert(
            args.stage, args.cycle, rc, path, business_check)
        _write_status(path, status)
    return rc if 0 <= rc <= 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
