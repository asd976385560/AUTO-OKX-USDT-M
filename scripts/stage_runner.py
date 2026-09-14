# -*- coding: utf-8 -*-
"""Detached stage 监督包装器：记录 running/succeeded/failed，失败只告警不重试。

由 collectors/trigger_agent.py detached 拉起。本脚本同步等待真正的 agent/push
子进程，因此能取得最终退出码；dispatcher 仍只认 stage_dispatch 做幂等，本状态文件
仅用于终态可观测性。任何失败都不会释放闩锁、补派或重试。
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
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


CST = timezone(timedelta(hours=8))
ROOT = Path(_public_project_path())
# Direct script entry may run outside the project cwd (notably Push).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
COLLECTORS = ROOT / "collectors"
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))
import ledger  # noqa: E402
import _proc  # noqa: E402
import _acceptance_thresholds as thresholds  # noqa: E402
import _zh_labels  # noqa: E402
from stage_failure_contract import (  # noqa: E402
    REPORT_RECONCILE_BARRIER_FROM,
    load_upstream_failure,
)

# Writers nudge while the live Agent still owns its profile lease.  Once the
# dispatcher defers that early tick, the supervisor emits one final event only
# after releasing the lease.  Import failure is non-fatal; the periodic
# dispatcher remains the fallback.
try:
    import _dispatch_nudge as _nudge_mod  # noqa: E402
except Exception:  # noqa: BLE001
    _nudge_mod = None

STATUS_DIR = Path(os.environ.get("OKX_STAGE_STATUS_DIR")
                  or _public_project_path('logs', 'stage-status'))
QQ_PUSH = ROOT / "scripts" / "qq_push.py"
LIVE_RECON_MONITOR = ROOT / "scripts" / "live_reconcile_monitor.py"
LIVE_POSITION_ACTION_RUNNER = (
    ROOT / "scripts" / "live_position_action_runner.py")
LIVE_DECISION_FACTS = ROOT / "scripts" / "live_decision_facts.py"
POSITION_REVIEW_EVIDENCE = (
    ROOT / "scripts" / "multitimeframe_decision_evidence.py")
DB_ROOT = Path(os.environ.get("OKX_DB_ROOT") or _public_project_path('db'))
OPENCLAW_STATE_ROOT = Path(
    os.environ.get("OKX_OPENCLAW_STATE_ROOT")
    or (Path.home() / ".openclaw")
)
_OPENCLAW_NODE = Path(os.environ.get(
    "OKX_NODE_BIN", r"C:\Program Files\nodejs\node.exe"))
_OPENCLAW_MJS = Path(os.environ.get(
    "OKX_OPENCLAW_MJS",
    '<USER_HOME>\\AppData\\Roaming\\npm\\node_modules\\openclaw\\openclaw.mjs'.replace('<USER_HOME>', str(__import__('pathlib').Path.home())),
))
_OPENCLAW_AGENT_ADAPTER = Path(os.environ.get(
    "OKX_OPENCLAW_AGENT_ADAPTER",
    _public_project_path('scripts', 'openclaw_agent_same_connection.mjs'),
))
_STAGE_CONTROL_DIR = Path(os.environ.get(
    "OKX_STAGE_CONTROL_DIR",
    _public_project_path('logs', 'stage-control'),
))
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_BUSINESS_FAILURE_RC = 86
_PARTIAL_BUSINESS_REPORT_FROM = "2026-09-08T12:15"
_COMPLETE_CYCLE_SLA_SECONDS = thresholds.COMPLETE_CYCLE_SLA_SECONDS
_ANALYSIS_DEADLINE_GUARD_FROM = "2026-08-15T21:45"
# Forward-only preregistration: slots before this boundary retain the original
# unbounded push-child / independent 240s reconciliation semantics.  Starting
# with this exact natural slot, Push and its post-Push monitor use the
# cycle-resolved deadlines in ``_acceptance_thresholds.py``; the original
# boundary resolved to cycle+14:00, while later V3/V4 cycles migrate forward.
_PUSH_RECONCILE_DEADLINE_ACTIVATION_CST = datetime.fromisoformat(
    thresholds.PUSH_SAME_SLOT_ACTIVATION_CST
)
_MINIMUM_LIVE_CHILD_BUDGET_SECONDS = 60.0
_GATEWAY_ABORT_RPC_TIMEOUT_MS = 10_000
_GATEWAY_ABORT_PROCESS_TIMEOUT_SECONDS = 15
_SAME_CONNECTION_ABORT_GRACE_SECONDS = 12.0
_SAME_CONNECTION_RECEIPT_SCHEMA = (
    "okx.openclaw-agent-control-receipt.v1")
_BUSINESS_OUTPUT_SETTLE_SECONDS = 45.0
_BUSINESS_OUTPUT_POLL_SECONDS = 0.25
_COLLECTION_GATE_READ_ATTEMPTS = 3
_COLLECTION_GATE_READ_RETRY_SECONDS = 0.05
# 持仓扩大到 18 个后，完整只读逐仓证据在 180s handoff 闸被误杀；该闸不是
# 交易风险门。给结构化逐仓判断最多 300s，plan 一旦落盘仍只再给 runner 30s，
# 最外层仍受本 cycle 单点事实源解析出的 870s 业务终态硬截止约束。
_LIVE_HANDOFF_AFTER_POSITION_EXIT_SECONDS = 300.0
_LIVE_RUNNER_START_AFTER_PLAN_SECONDS = 30.0
# The Agent remains the normal caller so it can receive a preflight error and
# use the one admitted full-file rewrite.  The supervisor no longer lets a
# provider/network turn consume the whole 30-second claim window: once a
# complete, stable plan has existed for 10 seconds without a valid marker, it
# starts the exact same fixed runner itself.  The runner's profile lock,
# handoff CAS and runtime authority still decide who owns the one execution.
_LIVE_SUPERVISOR_RUNNER_AUTOSTART_SECONDS = 10.0
_LIVE_SUPERVISOR_RUNNER_EXIT_GRACE_SECONDS = 5.0
# 2026-08-24：runner 预检拒（state=failed_preflight，零副作用）给 agent 一个
# 整文件重写 plan 的窗口；窗口从该 marker 的 mtime 起算。重写落盘会使旧
# marker 因 plan_sha256 失配失效，自然回到「plan 后 30 秒须起 runner」闸。
_LIVE_PREFLIGHT_REWRITE_SECONDS = 180.0
# 孪生常量：真源是 live_position_action_runner.PREFLIGHT_MAX_ATTEMPTS。
# supervisor 刻意不 import runner 模块（避免把交易所依赖拉进监督进程），
# 等值性由 tests/test_live_position_action_runner.py 的孪生断言强制。
_LIVE_PREFLIGHT_MAX_ATTEMPTS = 2
_LIVE_OBSERVER_POLL_SECONDS = 0.5
_LIVE_INPUT_PREPARE_TIMEOUT_SECONDS = 120.0
_LIVE_HANDOFF_FAILURE_RC = 87
_LIVE_ANALYSIS_FAILURE_RC = 88
_POST_PUSH_RECONCILE_FAILURE_RC = 89
_LIVE_RUNNER_STATE_SCHEMA_VERSION = 2
_LIVE_HANDOFF_GATE_SCHEMA_VERSION = 2
_STAGE_AGENTS = {
    "analyst": "okx-analyst",
    "live": "okx-live-trader",
}


from collectors.cycle_contract import validate_cycle_id, cycle_session_token, cycle_status_token


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _live_deadline_at(cycle: str) -> datetime:
    """Return the fail-closed live-child/finalization deadline."""
    cycle_start = datetime.strptime(
        str(cycle), "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    if thresholds.complete_cycle_uses_business_terminal_stop(cycle):
        return cycle_start + timedelta(
            seconds=thresholds.sla_record_reconcile_deadline_seconds(cycle))
    return cycle_start + timedelta(
        seconds=thresholds.sla_business_terminal_deadline_seconds(cycle))


def _analysis_deadline_at(cycle: str) -> datetime:
    """Return the prompt/writer/stage analysis cutoff for one natural cycle."""
    cycle_start = datetime.strptime(
        str(cycle), "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    return cycle_start + timedelta(
        seconds=thresholds.sla_analysis_deadline_seconds(cycle))


def _cycle_start_at(cycle: str) -> datetime:
    return datetime.strptime(
        str(cycle), "%Y-%m-%dT%H:%M").replace(tzinfo=CST)


def _collection_gate_contract(
    cycle: str,
    *,
    db_root: Path | None = None,
) -> dict:
    """Return the required-source collection gate for V4 §6."""
    cycle_start = _cycle_start_at(cycle)
    required = sorted(ledger.expected_sources(cycle))
    path = Path(db_root or DB_ROOT) / "ledger.db"
    rows = None
    last_error: Exception | None = None
    for attempt in range(_COLLECTION_GATE_READ_ATTEMPTS):
        try:
            con = ledger.connect(path, readonly=True)
            try:
                rows = con.execute(
                    "SELECT source,status,ts FROM collection_runs "
                    "WHERE cycle_id=?",
                    (cycle,),
                ).fetchall()
            finally:
                con.close()
            break
        except Exception as exc:
            last_error = exc
            message = str(exc).casefold()
            retryable = isinstance(exc, sqlite3.OperationalError) and (
                "disk i/o error" in message or "locked" in message
            )
            if not retryable or attempt + 1 >= _COLLECTION_GATE_READ_ATTEMPTS:
                break
            time.sleep(_COLLECTION_GATE_READ_RETRY_SECONDS * (attempt + 1))
    if rows is None:
        exc = last_error or RuntimeError("collection gate read returned no rows")
        return {
            "schema_version": 1,
            "status": "invalid",
            "cycle_id": cycle,
            "required_sources": required,
            "error": f"{type(exc).__name__}: {exc}",
        }
    usable = {
        str(row["source"]): row
        for row in rows
        if str(row["source"]) in required
        and str(row["status"]) in ledger.DONE_STATUS
    }
    missing = sorted(set(required) - set(usable))
    if missing:
        return {
            "schema_version": 1,
            "status": "incomplete",
            "cycle_id": cycle,
            "required_sources": required,
            "missing_sources": missing,
        }
    completed_values: list[datetime] = []
    try:
        for source in required:
            completed_values.append(datetime.strptime(
                str(usable[source]["ts"]), "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=CST))
    except (TypeError, ValueError) as exc:
        return {
            "schema_version": 1,
            "status": "invalid",
            "cycle_id": cycle,
            "required_sources": required,
            "error": f"collection completion timestamp invalid: {exc}",
        }
    completed_at = max(completed_values)
    if completed_at < cycle_start:
        return {
            "schema_version": 1,
            "status": "invalid",
            "cycle_id": cycle,
            "required_sources": required,
            "error": "collection completion precedes cycle start",
        }
    return {
        "schema_version": 1,
        "status": "met",
        "cycle_id": cycle,
        "required_sources": required,
        "completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": int((completed_at - cycle_start).total_seconds()),
        "time_threshold_seconds": None,
        "semantics": (
            "required source completion is a fact gate; the combined process "
            "must still stop at the business terminal strictly before 870s"),
    }


def _push_reconcile_deadline_enabled(cycle: str) -> bool:
    """Whether the preregistered forward-only push deadline applies."""
    return _cycle_start_at(cycle) >= _PUSH_RECONCILE_DEADLINE_ACTIVATION_CST


def _push_reconcile_deadline_at(cycle: str) -> datetime:
    """Return the strict same-slot deadline for the Push child."""
    return _cycle_start_at(cycle) + timedelta(
        seconds=thresholds.push_same_slot_max_age_seconds(cycle))


def _post_push_monitor_deadline_at(cycle: str) -> datetime:
    """Return the independent monitor deadline; exact next slot is late."""
    return _cycle_start_at(cycle) + timedelta(
        seconds=thresholds.post_push_monitor_deadline_seconds(cycle))


def _gateway_session_key(
    stage: str,
    cycle: str,
    db_root: Path | str | None = None,
) -> str:
    agent_id = _STAGE_AGENTS[stage]
    return f"agent:{agent_id}:{_stage_session_key(stage, cycle, db_root)}"


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _prepare_same_connection_abort(
    stage: str,
    cycle: str,
    command: list[str],
) -> tuple[list[str], dict | None]:
    """Inject one unguessable control tuple into the approved Node adapter."""
    adapter_index = next(
        (
            index
            for index, item in enumerate(command)
            if _same_path(item, _OPENCLAW_AGENT_ADAPTER)
        ),
        None,
    )
    if adapter_index is None:
        return list(command), None
    try:
        separator = command.index("--", adapter_index + 1)
    except ValueError:
        return list(command), None
    forbidden = {
        "--abort-control-file",
        "--abort-receipt-file",
        "--control-id",
    }
    if any(item in forbidden for item in command[adapter_index + 1:separator]):
        raise ValueError("same-connection adapter control flags already present")

    _STAGE_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    control_id = uuid.uuid4().hex
    safe_cycle = _SAFE_RE.sub(
        "_", str(cycle).replace(":", "-")).strip("._-")
    prefix = f"{stage}-{safe_cycle}-{control_id[:12]}"
    request_path = _STAGE_CONTROL_DIR / f"{prefix}.abort-request.json"
    receipt_path = _STAGE_CONTROL_DIR / f"{prefix}.abort-receipt.json"
    injected = [
        "--abort-control-file", str(request_path),
        "--abort-receipt-file", str(receipt_path),
        "--control-id", control_id,
    ]
    prepared = [*command[:separator], *injected, *command[separator:]]
    return prepared, {
        "schema_version": 1,
        "available": True,
        "adapter": _OPENCLAW_AGENT_ADAPTER.name,
        "control_id": control_id,
        "control_file": str(request_path),
        "receipt_file": str(receipt_path),
        "grace_seconds": _SAME_CONNECTION_ABORT_GRACE_SECONDS,
        "requested": False,
    }


def _write_abort_control_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _request_same_connection_abort(
    control: dict,
    stage: str,
    cycle: str,
    reason: str,
) -> bool:
    requested_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": 1,
        "control_id": control["control_id"],
        "action": "abort",
        "signal": "SIGTERM",
        "stage": stage,
        "cycle_id": cycle,
        "session_key": _gateway_session_key(stage, cycle),
        "requested_at": requested_at,
        "reason": str(reason)[:200],
    }
    try:
        _write_abort_control_atomic(Path(control["control_file"]), payload)
    except Exception as exc:  # noqa: BLE001 - caller retains hard kill
        control.update({
            "requested": False,
            "request_error": f"{type(exc).__name__}: {exc}"[:500],
        })
        return False
    control.update({
        "requested": True,
        "requested_at": requested_at,
        "reason": str(reason)[:200],
    })
    return True


def _load_same_connection_receipt(control: dict) -> dict:
    """Load only the small, identity-bound receipt fields into stage status."""
    receipt_path = Path(str(control.get("receipt_file") or ""))
    result = dict(control)
    try:
        if receipt_path.stat().st_size > 32_768:
            raise ValueError("receipt exceeds 32768 bytes")
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result.update({"receipt_valid": False, "receipt_status": "missing"})
        return result
    except Exception as exc:  # noqa: BLE001 - evidence must fail closed
        result.update({
            "receipt_valid": False,
            "receipt_status": "invalid",
            "receipt_error": f"{type(exc).__name__}: {exc}"[:500],
        })
        return result
    valid = bool(
        isinstance(payload, dict)
        and payload.get("schema") == _SAME_CONNECTION_RECEIPT_SCHEMA
        and payload.get("control_id") == control.get("control_id")
        and isinstance(payload.get("wrapper_pid"), int)
        and int(payload["wrapper_pid"]) > 0
        and isinstance(payload.get("control_request_observed"), bool)
        and isinstance(payload.get("signal_delivered"), bool)
        and isinstance(payload.get("command_completed"), bool)
        and payload.get("phase") in {
            "completed", "signal_exit", "process_exit"}
        and (
            payload.get("exit_code") is None
            or isinstance(payload.get("exit_code"), int)
        )
    )
    if payload.get("signal_delivered") is True:
        valid = bool(
            valid
            and payload.get("control_request_observed") is True
            and payload.get("phase") == "signal_exit"
            and payload.get("exit_code") in {130, 143}
        )
    receipt = {
        key: payload.get(key)
        for key in (
            "schema",
            "wrapper_pid",
            "control_id",
            "control_configured",
            "started_at",
            "control_request_observed",
            "control_requested_at",
            "control_reason",
            "signal_delivered",
            "signal_delivery_listener_count",
            "signal_delivered_at",
            "command_completed",
            "gateway_terminal_error_observed",
            "gateway_terminal_error_marker",
            "phase",
            "exit_code",
            "finished_at",
        )
        if key in payload
    }
    result.update({
        "receipt_valid": valid,
        "receipt_status": "valid" if valid else "invalid",
        "receipt": receipt,
    })
    if not valid:
        result["receipt_error"] = "identity or receipt-shape validation failed"
    return result


def _receipt_proves_gateway_terminal_error(control: dict) -> bool:
    receipt = (
        control.get("receipt")
        if isinstance(control.get("receipt"), dict) else {}
    )
    return bool(
        control.get("receipt_valid") is True
        and receipt.get("gateway_terminal_error_observed") is True
        and receipt.get("gateway_terminal_error_marker") == "all_models_failed"
        and receipt.get("phase") == "process_exit"
        and isinstance(receipt.get("exit_code"), int)
        and int(receipt["exit_code"]) != 0
    )


def _same_connection_abort_summary(control: dict) -> dict:
    receipt = (
        control.get("receipt")
        if isinstance(control.get("receipt"), dict) else {}
    )
    process_stop = (
        control.get("process_stop")
        if isinstance(control.get("process_stop"), dict) else {}
    )
    verification = (
        control.get("terminal_verification")
        if isinstance(control.get("terminal_verification"), dict) else {}
    )
    return {
        "available": control.get("available") is True,
        "requested": control.get("requested") is True,
        "receipt_valid": control.get("receipt_valid") is True,
        "control_request_observed": (
            receipt.get("control_request_observed") is True),
        "signal_delivered": receipt.get("signal_delivered") is True,
        "gateway_terminal_error_observed": (
            receipt.get("gateway_terminal_error_observed") is True),
        "graceful_stop_completed": (
            process_stop.get("graceful_completed") is True),
        "process_tree_terminated": (
            process_stop.get("process_tree_terminated") is True),
        "terminal_verification": {
            "rpc": verification.get("rpc"),
            "status": verification.get("status"),
            "terminal_confirmed": (
                verification.get("terminal_confirmed") is True),
        },
    }


def _try_handoff_lock(path: Path):
    """Try one kernel-released byte lock; return its handle or ``None``.

    The live runner uses the same byte lock while it validates authority and
    publishes its bound ``started`` marker.  Therefore the observer can make
    the timeout decision and persist revocation in one ordered transition.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size < 1:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - production runtime is Windows
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _release_handoff_lock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - production runtime is Windows
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _launch_live_position_action_runner(
    *,
    cycle: str,
    plan_file: Path,
    facts_file: Path,
    receipt_file: Path,
    db_root: Path,
    log_file: Path,
) -> subprocess.Popen:
    """Start the existing fixed runner without adding another order path.

    ``stage_runner`` owns only process launch/lifecycle.  The child still owns
    plan validation, runtime authority, the profile-wide process lock, handoff
    CAS, executor calls and writer commits.  Directly reusing this process's
    interpreter avoids a PowerShell hop in the already-detached supervisor and
    ``CREATE_NO_WINDOW`` preserves unattended Windows behaviour.
    """
    command = [
        sys.executable,
        str(LIVE_POSITION_ACTION_RUNNER),
        "--cycle-id", str(cycle),
        "--plan-file", str(plan_file),
        "--facts-file", str(facts_file),
        "--receipt-file", str(receipt_file),
        "--db-root", str(db_root),
    ]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as log_handle:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            creationflags=_CREATE_NO_WINDOW,
            close_fds=True,
        )


def _canonical_artifact_hash(payload: dict, hash_field: str) -> str:
    unsigned = {
        key: value for key, value in payload.items() if key != hash_field
    }
    return hashlib.sha256(json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _prepare_deterministic_live_inputs(
    *,
    cycle: str,
    facts_file: Path,
    position_exit_file: Path,
    decision_view_file: Path,
    db_root: Path,
    log_file: Path,
) -> dict:
    """Build and validate the private read-only handoff after analysis commit.

    This is intentionally a stage-supervisor operation rather than an Agent
    instruction.  It performs exactly one private facts read and one local
    position projection, writes no business database, and returns only hashes
    and counts so stage status/logs never copy account or position contents.
    """
    paths = (facts_file, position_exit_file, decision_view_file)
    preexisting = [str(path) for path in paths if path.exists()]
    python_wrapper = ROOT / "scripts" / "run_okx_python.ps1"
    facts_command = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File",
        python_wrapper.as_posix(),
        LIVE_DECISION_FACTS.as_posix(),
        "--profile", "live",
        "--cycle-id", str(cycle),
        "--out-file", facts_file.as_posix(),
        "--analysis-db", (db_root / "analysis.db").as_posix(),
    ]
    position_command = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File",
        python_wrapper.as_posix(),
        POSITION_REVIEW_EVIDENCE.as_posix(),
        "--db-root", db_root.as_posix(),
        "--facts-file", facts_file.as_posix(),
        "--cycle-id", str(cycle),
        "--out-file", position_exit_file.as_posix(),
        "--decision-view-file", decision_view_file.as_posix(),
    ]
    base = {
        "source": "stage_runner_deterministic_live_inputs",
        "cycle_id": str(cycle),
        "facts_file": str(facts_file),
        "position_exit_file": str(position_exit_file),
        "decision_view_file": str(decision_view_file),
        "facts_command": facts_command,
        "position_command": position_command,
        "production_database_writes": 0,
        "orders_placed": 0,
    }
    if preexisting:
        return {
            **base,
            "ok": False,
            "status": "preexisting_artifact_refused",
            "preexisting": preexisting,
        }

    log_file.parent.mkdir(parents=True, exist_ok=True)

    def run(command: list[str], label: str) -> subprocess.CompletedProcess:
        stop_report: dict = {}
        rc, stdout, stderr, timed_out = _proc.run_guarded(
            command, cwd=str(ROOT),
            timeout=_LIVE_INPUT_PREPARE_TIMEOUT_SECONDS,
            creationflags=_CREATE_NO_WINDOW, stop_report=stop_report)
        completed = subprocess.CompletedProcess(command, rc, stdout, stderr)
        # Both tools print summaries only.  Persisting those summaries makes
        # failures diagnosable without copying the private artifact itself.
        with log_file.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({
                "at": now_cst(),
                "step": label,
                "returncode": int(completed.returncode),
                "timed_out": timed_out,
                "process_stop": stop_report,
                "stdout": (completed.stdout or "")[-4000:],
                "stderr": (completed.stderr or "")[-4000:],
            }, ensure_ascii=False, sort_keys=True) + "\n")
        if timed_out:
            raise TimeoutError(
                f"{label} exceeded {_LIVE_INPUT_PREPARE_TIMEOUT_SECONDS}s; "
                f"process_tree_terminated={stop_report.get('process_tree_terminated')}")
        return completed

    try:
        facts_run = run(facts_command, "facts")
    except Exception as exc:  # noqa: BLE001 - fail closed, no retry
        return {
            **base,
            "ok": False,
            "status": "facts_process_failed",
            "error": f"{type(exc).__name__}:{exc}",
        }
    # rc=2 is the tool's intentional, structurally valid ``blocking`` facts
    # state.  It must still reach the Agent so explicitly whitelisted
    # risk-reducing position actions remain available; only new risk is fenced.
    if facts_run.returncode not in {0, 2}:
        return {
            **base,
            "ok": False,
            "status": "facts_process_rejected",
            "returncode": int(facts_run.returncode),
        }
    try:
        facts = json.loads(facts_file.read_text(encoding="utf-8"))
        facts_hash = str(facts.get("facts_hash") or "")
        facts_valid = bool(
            isinstance(facts, dict)
            and facts.get("schema_version") == 1
            and facts.get("source") == "okx_private_api"
            and facts.get("cycle_id") == cycle
            and facts.get("profile") == "live"
            and facts.get("status") in {"ok", "blocking"}
            and isinstance(facts.get("errors"), list)
            and (
                (facts.get("status") == "ok" and not facts.get("errors"))
                or (facts.get("status") == "blocking" and facts.get("errors"))
            )
            and isinstance(facts.get("positions"), list)
            and facts_hash
            and facts_hash == _canonical_artifact_hash(facts, "facts_hash")
        )
        if not facts_valid:
            raise ValueError("facts artifact contract/hash mismatch")
    except Exception as exc:  # noqa: BLE001 - private input must be exact
        return {
            **base,
            "ok": False,
            "status": "facts_validation_failed",
            "error": f"{type(exc).__name__}:{exc}",
        }

    try:
        position_run = run(position_command, "position_review")
    except Exception as exc:  # noqa: BLE001 - fail closed, no retry
        return {
            **base,
            "ok": False,
            "status": "position_process_failed",
            "facts_hash": facts_hash,
            "error": f"{type(exc).__name__}:{exc}",
        }
    if position_run.returncode != 0:
        return {
            **base,
            "ok": False,
            "status": "position_process_rejected",
            "facts_hash": facts_hash,
            "returncode": int(position_run.returncode),
        }
    try:
        position = json.loads(position_exit_file.read_text(encoding="utf-8"))
        view = json.loads(decision_view_file.read_text(encoding="utf-8"))
        position_hash = str(position.get("evidence_hash") or "")
        view_hash = str(view.get("view_hash") or "")
        expected_count = len(facts["positions"])
        position_valid = bool(
            isinstance(position, dict)
            and position.get("schema_version") == 2
            and position.get("cycle_id") == cycle
            and position.get("facts_hash") == facts_hash
            and position.get("status") == "PASSED"
            and position.get("timeframe_judgment_used") is False
            and position.get("position_count") == expected_count
            and position_hash
            and position_hash == _canonical_artifact_hash(
                position, "evidence_hash")
        )
        view_valid = bool(
            isinstance(view, dict)
            and view.get("schema")
            == "position_exit_decision_view_v2_no_timeframes"
            and view.get("cycle_id") == cycle
            and view.get("facts_hash") == facts_hash
            and view.get("source_evidence_hash") == position_hash
            and view.get("source_status") == "PASSED"
            and view.get("timeframe_judgment_used") is False
            and view.get("position_count") == expected_count
            and view_hash
            and view_hash == _canonical_artifact_hash(view, "view_hash")
        )
        if not position_valid or not view_valid:
            raise ValueError("position/view artifact contract/hash mismatch")
    except Exception as exc:  # noqa: BLE001 - no partial handoff
        return {
            **base,
            "ok": False,
            "status": "position_validation_failed",
            "facts_hash": facts_hash,
            "error": f"{type(exc).__name__}:{exc}",
        }
    return {
        **base,
        "ok": True,
        "status": "ready",
        "facts_status": facts.get("status"),
        "facts_hash": facts_hash,
        "position_evidence_hash": position_hash,
        "decision_view_hash": view_hash,
        "position_count": len(facts["positions"]),
    }


class _LiveChildObserver:
    """Observe and deterministically launch handoff without touching orders.

    The model may decide the plan, but after ``position_exit`` it must hand one
    canonical plan to the fixed runner.  This observer reads the fixed artifacts
    and trade-cycle terminal and may start that existing runner after a bounded
    grace.  It never creates a receipt, retries an action, interprets exchange
    state, or calls an executor itself.
    """

    _ACTIVE_STATES = frozenset({"started", "executing"})
    _TERMINAL_STATES = frozenset({"committed", "failed"})
    # failed_preflight is neither active nor terminal: the runner proved zero
    # side effects and parked the cycle for one full-file plan rewrite.
    _RETRYABLE_STATES = frozenset({"failed_preflight"})

    def __init__(
        self,
        cycle: str,
        *,
        tmp_root: Path | None = None,
        db_root: Path | None = None,
        now_fn=None,
        enforce_analysis_deadline: bool = False,
        expected_session_key: str | None = None,
        expected_stage_runner_pid: int | None = None,
        auto_start_runner: bool = False,
        runner_launch_fn=None,
        auto_prepare_live_inputs: bool | None = None,
        live_input_prepare_fn=None,
    ) -> None:
        safe_cycle = _safe(cycle)
        self.cycle = str(cycle)
        self.tmp_root = Path(tmp_root or (ROOT / "tmp"))
        self.db_root = Path(db_root or DB_ROOT)
        self.now_fn = now_fn or time.time
        self.enforce_analysis_deadline = bool(
            enforce_analysis_deadline
            and self.cycle >= _ANALYSIS_DEADLINE_GUARD_FROM
        )
        self.analysis_path = self.db_root / "analysis.db"
        self.facts_path = self.tmp_root / f"live_facts_{safe_cycle}.json"
        self.position_exit_path = (
            self.tmp_root / f"position_exit_{safe_cycle}.json")
        self.position_exit_view_path = (
            self.tmp_root / f"position_exit_view_{safe_cycle}.json")
        self.live_input_status_path = (
            self.tmp_root / f"live_input_handoff_{safe_cycle}.json")
        self.plan_path = self.tmp_root / f"position_plan_{safe_cycle}.json"
        self.marker_path = (
            self.tmp_root / f"live_runner_state_{safe_cycle}.json")
        self.receipt_path = (
            self.tmp_root / f"_receipt_live_{safe_cycle}.json")
        self.handoff_path = (
            self.tmp_root / f"live_runner_handoff_{safe_cycle}.json")
        self.handoff_lock_path = (
            self.tmp_root / f"live_runner_handoff_{safe_cycle}.lock")
        self.expected_session_key = (
            str(expected_session_key) if expected_session_key is not None
            else None
        )
        self.expected_stage_runner_pid = (
            int(expected_stage_runner_pid)
            if expected_stage_runner_pid is not None else None
        )
        self.auto_start_runner = bool(auto_start_runner)
        self.runner_launch_fn = (
            runner_launch_fn or _launch_live_position_action_runner)
        self.auto_prepare_live_inputs = bool(
            thresholds.minimal_contract_closure_active(self.cycle)
            if auto_prepare_live_inputs is None
            else auto_prepare_live_inputs
        )
        self.live_input_prepare_fn = (
            live_input_prepare_fn or _prepare_deterministic_live_inputs)
        self._live_input_guard = threading.RLock()
        self._live_input_attempted = False
        self.runner_log_path = (
            ROOT / "logs" / "runner-handoff"
            / f"live-{safe_cycle}.log")
        self.live_input_log_path = (
            ROOT / "logs" / "live-input-handoff"
            / f"live-{safe_cycle}.log")
        self._candidate_plan_sha256: str | None = None
        self._attempted_plan_sha256: set[str] = set()
        self._runner_guard = threading.RLock()
        self._supervised_runner_process: subprocess.Popen | None = None
        self._supervised_runner_record: dict | None = None
        self._supervised_runner_launches: list[dict] = []
        self.evidence: dict = {
            "cycle_id": self.cycle,
            "position_exit": str(self.position_exit_path),
            "position_exit_exists": False,
            "position_exit_required": None,
            "position_exit_position_count": None,
            "position_exit_status": "facts_unavailable",
            "plan": str(self.plan_path),
            "runner_state": str(self.marker_path),
            "runner_handoff": str(self.handoff_path),
            "deterministic_live_inputs": {
                "enabled": self.auto_prepare_live_inputs,
                "status": (
                    "waiting_for_analysis"
                    if self.auto_prepare_live_inputs else "disabled"),
                "facts": str(self.facts_path),
                "position_exit": str(self.position_exit_path),
                "decision_view": str(self.position_exit_view_path),
                "handoff": str(self.live_input_status_path),
                "log": str(self.live_input_log_path),
                "production_database_writes": 0,
                "orders_placed": 0,
            },
            "supervisor_runner_autostart": {
                "enabled": self.auto_start_runner,
                "after_plan_seconds": (
                    _LIVE_SUPERVISOR_RUNNER_AUTOSTART_SECONDS),
                "runner": str(LIVE_POSITION_ACTION_RUNNER),
                "receipt": str(self.receipt_path),
                "log": str(self.runner_log_path),
                "launches": self._supervised_runner_launches,
                "watcher": {
                    "status": "not_started",
                    "polls": 0,
                },
            },
        }
        if self.auto_prepare_live_inputs:
            _write_status(self.live_input_status_path, {
                "schema_version": 1,
                "cycle_id": self.cycle,
                "status": "waiting_for_analysis",
                "facts_file": str(self.facts_path),
                "decision_view_file": str(self.position_exit_view_path),
                "production_database_writes": 0,
                "orders_placed": 0,
            })

    def poll_deterministic_live_inputs(self) -> None:
        """Prepare the one stage-owned private input after writer authority."""
        if not self.auto_prepare_live_inputs:
            return
        with self._live_input_guard:
            state = self.evidence["deterministic_live_inputs"]
            if self._live_input_attempted or state.get("status") in {
                    "preparing", "ready", "failed"}:
                return
            analysis_state = self._analysis_state()
            if not (
                analysis_state.get("timely") is True
                and str(analysis_state.get("status") or "").strip().lower()
                == "ok"
            ):
                return
            self._live_input_attempted = True
            state.update({
                "status": "preparing",
                "started_at": now_cst(),
            })
            _write_status(self.live_input_status_path, {
                "schema_version": 1,
                "cycle_id": self.cycle,
                "status": "preparing",
                "facts_file": str(self.facts_path),
                "decision_view_file": str(self.position_exit_view_path),
                "production_database_writes": 0,
                "orders_placed": 0,
            })
            try:
                result = self.live_input_prepare_fn(
                    cycle=self.cycle,
                    facts_file=self.facts_path,
                    position_exit_file=self.position_exit_path,
                    decision_view_file=self.position_exit_view_path,
                    db_root=self.db_root,
                    log_file=self.live_input_log_path,
                )
            except Exception as exc:  # noqa: BLE001 - no retry/fallback
                result = {
                    "ok": False,
                    "status": "prepare_exception",
                    "error": f"{type(exc).__name__}:{exc}",
                }
            sanitized = {
                key: value for key, value in dict(result or {}).items()
                if key not in {"facts_command", "position_command"}
            }
            ready = bool(sanitized.get("ok") is True)
            detail_status = sanitized.get("status")
            state.update(sanitized)
            state["detail_status"] = detail_status
            state["status"] = "ready" if ready else "failed"
            state["finished_at"] = now_cst()
            handoff = {
                "schema_version": 1,
                "cycle_id": self.cycle,
                "status": state["status"],
                "detail_status": detail_status,
                "facts_file": str(self.facts_path),
                "decision_view_file": str(self.position_exit_view_path),
                "facts_hash": sanitized.get("facts_hash"),
                "facts_status": sanitized.get("facts_status"),
                "decision_view_hash": sanitized.get("decision_view_hash"),
                "position_count": sanitized.get("position_count"),
                "production_database_writes": 0,
                "orders_placed": 0,
            }
            if not ready:
                handoff["error"] = sanitized.get("error")
            _write_status(self.live_input_status_path, handoff)

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _age(self, path: Path) -> float:
        return max(0.0, float(self.now_fn()) - path.stat().st_mtime)

    def _refresh_supervised_runner(self) -> int | None:
        with self._runner_guard:
            process = self._supervised_runner_process
            record = self._supervised_runner_record
            if process is None or record is None:
                return None
            try:
                returncode = process.poll()
            except Exception as exc:  # noqa: BLE001 - stay fail-closed
                record.update({
                    "status": "poll_error",
                    "poll_error": f"{type(exc).__name__}: {exc}",
                })
                return None
            if returncode is None:
                record["status"] = "running"
                return None
            if record.get("status") not in {"exited", "terminated"}:
                record.update({
                    "status": "exited",
                    "returncode": int(returncode),
                    "finished_at": now_cst(),
                })
            return int(returncode)

    def _wait_for_business_persistence(self, marker: dict | None) -> bool:
        """Wait for the writer tail, preserving the already-stopped business clock."""
        state = str((marker or {}).get("state") or "")
        if state == "committed":
            self.evidence["business_persistence"] = {"status": "runner_committed"}
            return False
        handle = _try_handoff_lock(self.tmp_root / "live_runner.lock")
        if handle is not None:
            _release_handoff_lock(handle)
            self.evidence["business_persistence"] = {
                "status": "runner_gone_before_commit_marker", "marker_state": state,
                "business_terminal_preserved": True}
            return False
        now = float(self.now_fn())
        first = getattr(self, "_business_final_seen_at", None)
        if first is None:
            first = now
            self._business_final_seen_at = first
        deadline = min(first + 30.0, _live_deadline_at(self.cycle).timestamp())
        waiting = now < deadline
        self.evidence["business_persistence"] = {
            "status": "waiting_for_runner_commit" if waiting else "runner_commit_wait_expired",
            "marker_state": state, "elapsed_seconds": round(max(0.0, now-first),3),
            "deadline_at": datetime.fromtimestamp(deadline,CST).strftime("%Y-%m-%d %H:%M:%S"),
            "business_terminal_preserved": True}
        return waiting

    def _stable_plan_candidate(self) -> tuple[str, str] | None:
        """Return raw plan SHA/facts identity after two identical observations."""
        try:
            raw_plan = self.plan_path.read_bytes()
            plan = json.loads(raw_plan.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError,
                json.JSONDecodeError) as exc:
            self.evidence["supervisor_runner_plan_candidate"] = {
                "status": "invalid_json",
                "error_kind": type(exc).__name__,
            }
            self._candidate_plan_sha256 = None
            return None
        if not isinstance(plan, dict):
            self.evidence["supervisor_runner_plan_candidate"] = {
                "status": "not_object",
            }
            self._candidate_plan_sha256 = None
            return None
        try:
            facts = json.loads(self.facts_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError,
                json.JSONDecodeError) as exc:
            self.evidence["supervisor_runner_plan_candidate"] = {
                "status": "facts_unavailable",
                "error_kind": type(exc).__name__,
            }
            return None
        facts_hash = (
            str(facts.get("facts_hash") or "").strip()
            if isinstance(facts, dict) else ""
        )
        if not facts_hash:
            self.evidence["supervisor_runner_plan_candidate"] = {
                "status": "facts_hash_missing",
            }
            return None
        plan_sha256 = hashlib.sha256(raw_plan).hexdigest()
        from _plan_publication import validate as validate_plan_publication
        publication = validate_plan_publication(
            self.plan_path, self.cycle, plan_sha256, facts_hash)
        if publication.get("ok") is not True:
            self.evidence["supervisor_runner_plan_candidate"] = {
                "status": "not_published", "error": publication.get("error")}
            self._candidate_plan_sha256 = None
            return None
        stable = self._candidate_plan_sha256 == plan_sha256
        self._candidate_plan_sha256 = plan_sha256
        self.evidence["supervisor_runner_plan_candidate"] = {
            "status": "stable" if stable else "first_observation",
            "plan_sha256": plan_sha256,
            "facts_hash": facts_hash,
        }
        if not stable:
            return None
        return plan_sha256, facts_hash

    def _maybe_autostart_runner(self, *, plan_age: float) -> None:
        """Bound the model-to-runner handoff while preserving runner CAS."""
        with self._runner_guard:
            if not self.auto_start_runner:
                return
            self._refresh_supervised_runner()
            if (
                plan_age < _LIVE_SUPERVISOR_RUNNER_AUTOSTART_SECONDS
                or plan_age >= _LIVE_RUNNER_START_AFTER_PLAN_SECONDS
            ):
                return
            if (
                self._supervised_runner_process is not None
                and self._supervised_runner_process.poll() is None
            ):
                return
            candidate = self._stable_plan_candidate()
            if candidate is None:
                return
            plan_sha256, facts_hash = candidate
            if plan_sha256 in self._attempted_plan_sha256:
                return

            # Mark the exact bytes before Popen.  A launch exception or an early
            # child failure must not produce an unbounded respawn loop; only a
            # real full-file rewrite (new raw SHA) can create the admitted retry.
            self._attempted_plan_sha256.add(plan_sha256)
            started_at = now_cst()
            record = {
                "source": "stage_runner_deterministic_handoff",
                "status": "launching",
                "started_at": started_at,
                "plan_age_seconds": round(plan_age, 3),
                "plan_sha256": plan_sha256,
                "facts_hash": facts_hash,
            }
            self._supervised_runner_launches.append(record)
            self._supervised_runner_record = record
            try:
                process = self.runner_launch_fn(
                    cycle=self.cycle,
                    plan_file=self.plan_path,
                    facts_file=self.facts_path,
                    receipt_file=self.receipt_path,
                    db_root=self.db_root,
                    log_file=self.runner_log_path,
                )
            except Exception as exc:  # noqa: BLE001 - 30s gate stays closed
                record.update({
                    "status": "launch_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_at": now_cst(),
                })
                self._supervised_runner_process = None
                return
            self._supervised_runner_process = process
            record.update({
                "status": "running",
                "pid": int(process.pid),
            })

    def poll_supervisor_runner_autostart(self) -> None:
        """One watcher tick independent of the OpenClaw pipe wait."""
        self.poll_deterministic_live_inputs()
        if not self.auto_start_runner or not self.plan_path.exists():
            return
        marker = self._validated_marker()
        if marker is not None:
            return
        try:
            age = self._age(self.plan_path)
        except OSError:
            return
        self.evidence["plan_age_seconds"] = round(age, 3)
        self._maybe_autostart_runner(plan_age=age)

    def _preflight_retry_unavailable_after_agent_exit(self) -> str | None:
        """Fence a parked preflight retry once no Agent remains to rewrite."""
        marker = self._validated_marker()
        if marker is None:
            return None
        state = str(marker.get("state") or "").strip().lower()
        if state not in self._RETRYABLE_STATES:
            return None
        if (
            self._supervised_runner_process is not None
            and self._supervised_runner_process.poll() is None
        ):
            return None
        reason = (
            "post_facts_runner_handoff_violation:"
            "preflight_rewrite_unavailable_after_agent_exit"
        )
        if self._revoke_unclaimed_handoff(
                reason, treat_retryable_as_unclaimed=True):
            return self._stop(reason)
        return None

    def shutdown_supervised_runner(
        self,
        *,
        allow_exit_grace: bool,
    ) -> dict | None:
        """Bound and reap the supervisor-owned child after authority closes."""
        with self._runner_guard:
            process = self._supervised_runner_process
            record = self._supervised_runner_record
            if process is None or record is None:
                return None
            returncode = self._refresh_supervised_runner()
            if returncode is not None:
                return dict(record)
            if allow_exit_grace:
                try:
                    returncode = process.wait(
                        timeout=_LIVE_SUPERVISOR_RUNNER_EXIT_GRACE_SECONDS)
                    record.update({
                        "status": "exited",
                        "returncode": int(returncode),
                        "finished_at": now_cst(),
                    })
                    return dict(record)
                except subprocess.TimeoutExpired:
                    pass
                except Exception as exc:  # noqa: BLE001 - retain tree kill
                    record["wait_error"] = f"{type(exc).__name__}: {exc}"
            _proc.terminate_process_tree(process)
            returncode = process.poll()
            record.update({
                "status": "terminated",
                "returncode": (
                    int(returncode) if returncode is not None else None),
                "finished_at": now_cst(),
            })
            return dict(record)

    def _trade_cycle_state(self) -> dict:
        path = self.db_root / "live_trades.db"
        if not path.exists():
            return {"exists": False}
        try:
            con = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True, timeout=0.25)
            try:
                row = con.execute(
                    "SELECT raw FROM trade_cycles WHERE cycle_id=? LIMIT 1",
                    (self.cycle,),
                ).fetchone()
            finally:
                con.close()
            if row is None:
                return {"exists": False}
            state = {
                "exists": True,
                "runner_in_progress": None,
                "batch_status": None,
                "reconcile_source": None,
                "position_action_plan_hash": None,
                "facts_hash": None,
            }
            try:
                raw = json.loads(row[0]) if row[0] else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                state["raw_error"] = "invalid_json"
                return state
            if isinstance(raw, dict):
                if isinstance(raw.get("runner_in_progress"), bool):
                    state["runner_in_progress"] = raw["runner_in_progress"]
                state["batch_status"] = raw.get("batch_status")
                state["reconcile_source"] = raw.get("reconcile_source")
                state["position_action_plan_hash"] = raw.get(
                    "position_action_plan_hash")
                live_facts = raw.get("live_facts")
                if isinstance(live_facts, dict):
                    state["facts_hash"] = live_facts.get("facts_hash")
            else:
                state["raw_error"] = "not_object"
            return state
        except (OSError, sqlite3.Error):
            # Observer degradation must not replace the cycle-resolved
            # business-terminal hard stop.  The ordinary post-child business
            # check remains fail-closed.
            return {"exists": False, "read_error": True}

    def _analysis_state(self) -> dict:
        """Read the writer-owned analysis timestamp without mutating state."""
        if not self.analysis_path.exists():
            return {"exists": False}
        try:
            con = sqlite3.connect(
                f"file:{self.analysis_path.as_posix()}?mode=ro",
                uri=True,
                timeout=0.25,
            )
            try:
                row = con.execute(
                    "SELECT status,ts FROM analysis_runs "
                    "WHERE cycle_id=? LIMIT 1",
                    (self.cycle,),
                ).fetchone()
            finally:
                con.close()
        except (OSError, sqlite3.Error):
            return {"exists": False, "read_error": True}
        if row is None:
            return {"exists": False}
        status = str(row[0] or "").strip().lower()
        ts = str(row[1] or "").strip()
        try:
            written_at = datetime.strptime(
                ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        except ValueError:
            return {
                "exists": True,
                "status": status,
                "ts": ts,
                "timely": False,
                "ts_error": True,
            }
        deadline = _analysis_deadline_at(self.cycle)
        return {
            "exists": True,
            "status": status,
            "ts": ts,
            # Writer refuses at the exact boundary, so persisted authority
            # must be strictly earlier than cycle+09:30 as well.
            "timely": written_at < deadline,
        }

    def _validated_marker(self) -> dict | None:
        if not self.marker_path.exists():
            return None
        errors: list[str] = []
        try:
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            self.evidence["marker_error"] = f"invalid_json:{type(exc).__name__}"
            return None
        if not isinstance(marker, dict):
            self.evidence["marker_error"] = "marker_not_object"
            return None
        if marker.get("schema_version") != _LIVE_RUNNER_STATE_SCHEMA_VERSION:
            errors.append("schema_version")
        if str(marker.get("cycle_id") or "") != self.cycle:
            errors.append("cycle_id")
        state = str(marker.get("state") or "").strip().lower()
        if state not in (
            self._ACTIVE_STATES
            | self._TERMINAL_STATES
            | self._RETRYABLE_STATES
        ):
            errors.append("state")
        if (
            self.expected_session_key is not None
            and str(marker.get("session_key") or "")
            != self.expected_session_key
        ):
            errors.append("session_key")
        if self.expected_stage_runner_pid is not None:
            try:
                marker_stage_runner_pid = int(marker.get("stage_runner_pid"))
            except (TypeError, ValueError):
                marker_stage_runner_pid = -1
            if marker_stage_runner_pid != self.expected_stage_runner_pid:
                errors.append("stage_runner_pid")
        if self.facts_path.exists():
            try:
                facts = json.loads(self.facts_path.read_text(encoding="utf-8"))
                expected_facts_hash = str(facts.get("facts_hash") or "")
            except (OSError, ValueError, TypeError, AttributeError):
                expected_facts_hash = ""
            if (not expected_facts_hash
                    or str(marker.get("facts_hash") or "") != expected_facts_hash):
                errors.append("facts_hash")
        else:
            errors.append("facts_file")
        if self.plan_path.exists():
            try:
                expected_plan_hash = self._sha256(self.plan_path)
            except OSError:
                expected_plan_hash = ""
            if (not expected_plan_hash
                    or str(marker.get("plan_sha256") or "") != expected_plan_hash):
                errors.append("plan_sha256")
        else:
            errors.append("plan_file")
        if errors:
            self.evidence["marker_error"] = "mismatch:" + ",".join(errors)
            return None
        self.evidence.pop("marker_error", None)
        self.evidence["runner_state_value"] = state
        return marker

    def _handoff_binding(self) -> tuple[str | None, str | None]:
        plan_sha256 = None
        facts_hash = None
        if self.plan_path.exists():
            try:
                plan_sha256 = self._sha256(self.plan_path)
            except OSError:
                pass
        if self.facts_path.exists():
            try:
                facts = json.loads(self.facts_path.read_text(encoding="utf-8"))
                if isinstance(facts, dict):
                    facts_hash = str(facts.get("facts_hash") or "") or None
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        return plan_sha256, facts_hash

    def _revoke_unclaimed_handoff(
        self,
        reason: str,
        *,
        treat_retryable_as_unclaimed: bool = False,
    ) -> bool:
        """Win the runner/supervisor handoff CAS before declaring failure.

        ``True`` means a durable revocation now fences every later runner.
        ``False`` means a runner is inside the same claim transition or has
        already published a fully bound marker, so the observer must re-poll.
        ``treat_retryable_as_unclaimed`` lets the preflight-rewrite timeout
        path revoke past a still-valid ``failed_preflight`` marker: that
        marker proves an attempt ended without side effects, not that a
        runner currently owns the cycle.
        """
        handle = _try_handoff_lock(self.handoff_lock_path)
        if handle is None:
            self.evidence["handoff_arbitration"] = "claim_in_progress"
            return False
        try:
            # The runner writes the marker while holding this same lock.  A
            # second read here closes the check-then-stop race at 29.x seconds.
            claimed = self._validated_marker()
            if claimed is not None:
                claimed_state = str(
                    claimed.get("state") or "").strip().lower()
                if not (
                    treat_retryable_as_unclaimed
                    and claimed_state in self._RETRYABLE_STATES
                ):
                    self.evidence["handoff_arbitration"] = "runner_claimed"
                    return False

            plan_sha256, facts_hash = self._handoff_binding()
            if self.handoff_path.exists():
                try:
                    existing = json.loads(
                        self.handoff_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    existing = None
                valid_existing = bool(
                    isinstance(existing, dict)
                    and existing.get("schema_version")
                    == _LIVE_HANDOFF_GATE_SCHEMA_VERSION
                    and existing.get("state") == "revoked"
                    and str(existing.get("cycle_id") or "") == self.cycle
                    and existing.get("session_key")
                    == self.expected_session_key
                    and existing.get("stage_runner_pid")
                    == self.expected_stage_runner_pid
                    and existing.get("plan_sha256") == plan_sha256
                    and existing.get("facts_hash") == facts_hash
                )
                self.evidence["handoff_arbitration"] = (
                    "already_revoked" if valid_existing
                    else "invalid_gate_fail_closed"
                )
                self.evidence["handoff_revoked"] = True
                return True

            revoked_at = datetime.fromtimestamp(
                float(self.now_fn()), tz=CST).strftime("%Y-%m-%d %H:%M:%S")
            payload = {
                "schema_version": _LIVE_HANDOFF_GATE_SCHEMA_VERSION,
                "cycle_id": self.cycle,
                "state": "revoked",
                "reason": reason,
                "revoked_at": revoked_at,
                "session_key": self.expected_session_key,
                "stage_runner_pid": self.expected_stage_runner_pid,
                "plan_sha256": plan_sha256,
                "facts_hash": facts_hash,
            }
            _write_status(self.handoff_path, payload)
            self.evidence.update({
                "handoff_arbitration": "supervisor_revoked",
                "handoff_revoked": True,
                "handoff_revoked_at": revoked_at,
            })
            return True
        finally:
            _release_handoff_lock(handle)

    def _stop(self, reason: str) -> str:
        self.evidence.update({
            "stop_reason": reason,
            "observed_at": now_cst(),
        })
        return reason

    def __call__(self) -> str | None:
        self._refresh_supervised_runner()
        marker = self._validated_marker()
        plan_exists = self.plan_path.exists()
        cycle_state = self._trade_cycle_state()
        self.evidence["trade_cycle_state"] = cycle_state

        if self.enforce_analysis_deadline:
            analysis_state = self._analysis_state()
            self.evidence["analysis_state"] = analysis_state
            analysis_status = str(
                analysis_state.get("status") or "").strip().lower()
            timely_ok = bool(
                analysis_state.get("timely")
                and analysis_status == "ok"
            )
            timely_terminal = bool(
                analysis_state.get("timely")
                and analysis_status in {"skipped", "stale"}
            )
            if timely_terminal:
                return self._stop(f"analysis_terminal:{analysis_status}")
            if self.facts_path.exists() and not timely_ok:
                return self._stop(
                    "analysis_deadline_exceeded:facts_without_timely_analysis")
            current = datetime.fromtimestamp(
                float(self.now_fn()), tz=CST)
            if analysis_state.get("exists") and not analysis_state.get("timely"):
                return self._stop("analysis_deadline_exceeded:late_analysis")
            if current >= _analysis_deadline_at(self.cycle) and not timely_ok:
                return self._stop("analysis_deadline_exceeded:no_timely_analysis")
            if self.auto_prepare_live_inputs and timely_ok:
                input_state = self.evidence.get(
                    "deterministic_live_inputs") or {}
                input_status = str(input_state.get("status") or "")
                if input_status == "failed":
                    return self._stop(
                        "deterministic_live_input_failed:"
                        + str(input_state.get("detail_status") or "unknown")
                    )
                # A closure cycle may only proceed with the supervisor-owned,
                # fully validated handoff.  Merely finding a facts file is not
                # sufficient because an Agent-created or stale artifact must
                # never bypass deterministic preparation.
                if input_status != "ready":
                    return None

        expected_plan_hash = None
        expected_facts_hash = None
        facts_position_count: int | None = None
        if plan_exists:
            try:
                plan_payload = json.loads(
                    self.plan_path.read_text(encoding="utf-8"))
                expected_plan_hash = hashlib.sha256(json.dumps(
                    plan_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")).hexdigest()
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                expected_plan_hash = None
        if self.facts_path.exists():
            try:
                facts_payload = json.loads(
                    self.facts_path.read_text(encoding="utf-8"))
                if isinstance(facts_payload, dict):
                    expected_facts_hash = facts_payload.get("facts_hash")
                    positions = facts_payload.get("positions")
                    if isinstance(positions, list):
                        facts_position_count = len(positions)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                expected_facts_hash = None
                facts_position_count = None
        position_exit_exists = self.position_exit_path.exists()
        position_exit_required = (
            facts_position_count > 0
            if facts_position_count is not None else None
        )
        if position_exit_required is True:
            position_exit_status = (
                "verified" if position_exit_exists else "missing_required"
            )
        elif position_exit_required is False:
            position_exit_status = (
                "present_not_required"
                if position_exit_exists else "not_applicable"
            )
        else:
            position_exit_status = (
                "present_facts_unavailable"
                if position_exit_exists else "facts_unavailable"
            )
        self.evidence.update({
            "position_exit_exists": position_exit_exists,
            "position_exit_required": position_exit_required,
            "position_exit_position_count": facts_position_count,
            "position_exit_status": position_exit_status,
        })
        bound_final = bool(
            cycle_state.get("exists")
            and cycle_state.get("runner_in_progress") is False
            and not str(cycle_state.get("reconcile_source") or "").strip()
            and expected_plan_hash
            and cycle_state.get("position_action_plan_hash")
            == expected_plan_hash
            and expected_facts_hash
            and cycle_state.get("facts_hash") == expected_facts_hash
        )
        cycle_state["bound_final"] = bound_final

        # A final receipt is durable business output even if the runner crashes
        # after the DB commit but before it can flip its marker from executing
        # to committed.  Interim superset receipts explicitly carry
        # runner_in_progress=true and must not stop the in-flight runner.
        if bound_final:
            if position_exit_required is True and not position_exit_exists:
                return self._stop(
                    "post_facts_runner_handoff_violation:position_exit_missing"
                )
            if self._wait_for_business_persistence(marker):
                return None
            return self._stop("business_terminal_committed")

        # The runner may intentionally persist an interim partial receipt before
        # a later OPEN/ADD so the executor's global ledger/venue pretrade check
        # sees earlier fills.  While the bound marker is active, that row is not
        # the cycle terminal and the runner process tree must not be killed.
        if marker is not None:
            state = str(marker["state"]).strip().lower()
            if state in self._ACTIVE_STATES:
                return None

        if marker is not None:
            state = str(marker["state"]).strip().lower()
            if state in self._RETRYABLE_STATES:
                try:
                    attempts = int(marker.get("preflight_attempts") or 1)
                except (TypeError, ValueError):
                    attempts = 1
                self.evidence["preflight_attempts"] = attempts
                if attempts >= _LIVE_PREFLIGHT_MAX_ATTEMPTS:
                    # Defensive: the runner writes plain "failed" on the
                    # second rejection, so an exhausted retryable marker only
                    # appears if that contract ever regresses.
                    return self._stop(f"runner_terminal:{state}")
                age = self._age(self.marker_path)
                self.evidence["preflight_retry_age_seconds"] = round(age, 3)
                if age >= _LIVE_PREFLIGHT_REWRITE_SECONDS:
                    reason = "runner_terminal:failed_preflight_rewrite_timeout"
                    if self._revoke_unclaimed_handoff(
                            reason, treat_retryable_as_unclaimed=True):
                        return self._stop(reason)
                # Rewrite window still open, or a retry runner won the CAS
                # while we held the lock — either way keep observing.
                return None

        if marker is not None:
            state = str(marker["state"]).strip().lower()
            if state in self._TERMINAL_STATES:
                return self._stop(f"runner_terminal:{state}")

        if self.position_exit_path.exists() and not plan_exists:
            age = self._age(self.position_exit_path)
            self.evidence["position_exit_age_seconds"] = round(age, 3)
            if age >= _LIVE_HANDOFF_AFTER_POSITION_EXIT_SECONDS:
                reason = "post_facts_runner_handoff_violation:no_plan"
                if self._revoke_unclaimed_handoff(reason):
                    return self._stop(reason)

        if plan_exists:
            age = self._age(self.plan_path)
            self.evidence["plan_age_seconds"] = round(age, 3)
            if marker is None:
                self._maybe_autostart_runner(plan_age=age)
            if age >= _LIVE_RUNNER_START_AFTER_PLAN_SECONDS:
                reason = (
                    "post_facts_runner_handoff_violation:"
                    "no_valid_runner_marker"
                )
                if self._revoke_unclaimed_handoff(reason):
                    return self._stop(reason)
        return None


def _run_live_runner_autostart_watch(
    observer: _LiveChildObserver,
    stop_event: threading.Event,
    *,
    poll_seconds: float = _LIVE_OBSERVER_POLL_SECONDS,
) -> None:
    """Watch plan->marker independently of ``Popen.communicate`` timing."""
    autostart = observer.evidence.get("supervisor_runner_autostart")
    if not isinstance(autostart, dict):
        return
    watcher = autostart.get("watcher")
    if not isinstance(watcher, dict):
        watcher = {}
        autostart["watcher"] = watcher
    watcher.update({
        "status": "running",
        "started_at": now_cst(),
        "polls": 0,
    })
    delay = max(0.05, float(poll_seconds))
    try:
        while not stop_event.is_set():
            watcher["polls"] = int(watcher.get("polls") or 0) + 1
            watcher["last_poll_at"] = now_cst()
            try:
                observer.poll_supervisor_runner_autostart()
            except Exception as exc:  # noqa: BLE001 - 30s gate remains backup
                watcher.update({
                    "last_error": f"{type(exc).__name__}: {exc}",
                    "last_error_at": now_cst(),
                })
            if stop_event.wait(delay):
                break
    finally:
        watcher.update({
            "status": "stopped",
            "stopped_at": now_cst(),
        })


def _wait_for_live_handoff_after_agent_exit(
    observer: _LiveChildObserver,
    *,
    deadline_monotonic: float,
    monotonic_fn=None,
    sleep_fn=None,
) -> tuple[str | None, bool]:
    """Keep stage authority alive until an already-authored plan terminates.

    The OpenClaw CLI can return after the plan write while the independently
    launched fixed runner is still working.  Releasing the lease/status at that
    point would revoke the runner we just started.  This bounded follow-up wait
    observes the same artifacts only; it never creates business output.
    """
    if not observer.plan_path.exists():
        return None, False
    clock = monotonic_fn or time.monotonic
    sleeper = sleep_fn or time.sleep
    while True:
        reason = observer()
        if reason is not None:
            return str(reason), False
        retry_unavailable = (
            observer._preflight_retry_unavailable_after_agent_exit())
        if retry_unavailable is not None:
            return retry_unavailable, False
        remaining = float(deadline_monotonic) - float(clock())
        if remaining <= 0:
            return None, True
        sleeper(min(_LIVE_OBSERVER_POLL_SECONDS, remaining))


def _abort_gateway_session(
    stage: str,
    cycle: str,
    db_root: Path | str | None = None,
) -> dict:
    """Verify the exact Gateway session is terminal, or use the legacy fallback.

    The normal adapter now lets OpenClaw send ``chat.abort`` on its originating
    connection before process termination.  This new short-lived connection
    therefore serves as an independent ``no-active-run`` verification.  When
    a custom/legacy launcher has no adapter it remains a best-effort exact-key
    abort; an active run owned by another connection correctly stays
    unauthorized instead of widening the CLI to admin scope.
    """
    key = _gateway_session_key(stage, cycle, db_root)
    base = {
        "requested": True,
        "rpc": "sessions.abort",
        "session_key": key,
        "terminal_confirmed": False,
    }

    def call(method: str, params: dict) -> dict:
        command = [
            str(_OPENCLAW_NODE),
            "--stack-size=8192",
            str(_OPENCLAW_MJS),
            "gateway",
            "call",
            method,
            "--params",
            json.dumps(params, ensure_ascii=True, separators=(",", ":")),
            "--timeout",
            str(_GATEWAY_ABORT_RPC_TIMEOUT_MS),
            "--json",
        ]
        try:
            proc = subprocess.run(
                command,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GATEWAY_ABORT_PROCESS_TIMEOUT_SECONDS,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:  # noqa: BLE001 - stay bounded/fail closed
            return {
                "rpc": method,
                "returncode": None,
                "status": "rpc_error",
                "terminal_confirmed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        payload = _last_json_object(proc.stdout or "")
        if not isinstance(payload, dict):
            payload = {}
        rpc_error = payload.get("error")
        if isinstance(rpc_error, dict):
            error_message = str(rpc_error.get("message") or "").strip()
            error_code = str(rpc_error.get("code") or "").strip()
            if int(proc.returncode) != 0 or payload.get("ok") is not True:
                status = (
                    "unauthorized"
                    if error_message.lower() == "unauthorized"
                    else "rpc_error_response"
                )
                attempt = {
                    "rpc": method,
                    "returncode": int(proc.returncode),
                    "status": status,
                    "aborted_run_id_present": False,
                    "terminal_confirmed": False,
                    "error": (
                        error_message
                        or (proc.stderr or "").strip()[-500:]
                        or f"{method} returned an RPC error response"
                    ),
                }
                if error_code:
                    attempt["gateway_error_code"] = error_code
                if isinstance(rpc_error.get("retryable"), bool):
                    attempt["retryable"] = rpc_error["retryable"]
                if status == "unauthorized":
                    attempt["authorization_required"] = (
                        "originating_connection_or_admin_scope")
                return attempt
        if method == "sessions.abort":
            rpc_status = payload.get("status")
            aborted_run_id = payload.get("abortedRunId")
            terminal_confirmed = bool(
                int(proc.returncode) == 0
                and payload.get("ok") is True
                and (
                    (rpc_status == "aborted" and aborted_run_id)
                    or rpc_status == "no-active-run"
                )
            )
            attempt = {
                "rpc": method,
                "returncode": int(proc.returncode),
                "status": rpc_status or "invalid_response",
                "aborted_run_id_present": bool(aborted_run_id),
                "terminal_confirmed": terminal_confirmed,
            }
        else:
            aborted = payload.get("aborted")
            run_ids = payload.get("runIds")
            terminal_confirmed = bool(
                int(proc.returncode) == 0
                and payload.get("ok") is True
                and isinstance(aborted, bool)
                and isinstance(run_ids, list)
            )
            attempt = {
                "rpc": method,
                "returncode": int(proc.returncode),
                "status": (
                    "aborted" if aborted is True
                    else "no-active-run" if terminal_confirmed
                    else "invalid_response"
                ),
                "aborted_run_id_present": bool(run_ids),
                "terminal_confirmed": terminal_confirmed,
            }
        if not attempt["terminal_confirmed"]:
            attempt["error"] = (
                (proc.stderr or "").strip()[-500:]
                or f"{method} did not return an accepted terminal status"
            )
        return attempt

    primary = call("sessions.abort", {"key": key})
    if primary["terminal_confirmed"]:
        return {**base, **primary, "attempts": [primary]}
    if primary.get("status") == "unauthorized":
        return {
            **base,
            **primary,
            "attempts": [primary],
            "fallback_skipped": (
                "chat.abort uses the same requester authorization and cannot "
                "succeed after sessions.abort was unauthorized"
            ),
        }

    # Stable 2026.7.1 exposes both methods.  sessions.abort is the normalized
    # first choice; one same-session chat.abort fallback closes transient RPC
    # failures and has an equally explicit aborted/no-active-run response.
    fallback = call("chat.abort", {"sessionKey": key})
    if fallback["terminal_confirmed"]:
        return {
            **base,
            **fallback,
            "fallback_from": "sessions.abort",
            "attempts": [primary, fallback],
        }
    return {
        **base,
        "rpc": "sessions.abort+chat.abort",
        "returncode": fallback.get("returncode"),
        "status": fallback.get("status") or primary.get("status"),
        "aborted_run_id_present": False,
        "terminal_confirmed": False,
        "error": fallback.get("error") or primary.get("error"),
        "attempts": [primary, fallback],
    }


def _run_stage_child(
    stage: str,
    cycle: str,
    command: list[str],
    *,
    now: datetime | None = None,
    terminal_callback=None,
    db_root: Path | str | None = None,
) -> dict:
    """Run one stage child under its preregistered absolute clock.

    OpenClaw's own ``--timeout`` can include a long gateway/provider queue.
    Letting an unbounded turn own the profile lease can make one late cycle
    block the next natural slot.  The live child therefore gets only the time
    remaining to the cycle-resolved post-business finalization guard, while
    the observer stops normal work at the earlier business-terminal guard.
    Push and post-Push monitoring use their own cycle-resolved deadlines and
    are excluded from the V4 870-second clock.  A live stop first asks OpenClaw
    to abort through the originating connection; the complete Windows
    process-tree kill remains the bounded fallback.
    """
    push_deadline_enabled = bool(
        stage == "push" and _push_reconcile_deadline_enabled(cycle)
    )
    if stage != "live" and not push_deadline_enabled:
        proc = subprocess.run(
            command, cwd=str(ROOT), creationflags=_CREATE_NO_WINDOW)
        return {
            "returncode": int(proc.returncode),
            "timed_out": False,
            "started": True,
        }

    current = now or datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    current = current.astimezone(CST)

    if push_deadline_enabled:
        deadline = _push_reconcile_deadline_at(cycle)
        remaining = (deadline - current).total_seconds()
        base = {
            "absolute_deadline_at": deadline.strftime("%Y-%m-%d %H:%M:%S"),
            "deadline_activation_cst": (
                _PUSH_RECONCILE_DEADLINE_ACTIVATION_CST.isoformat()
            ),
            "deadline_seconds": thresholds.push_same_slot_max_age_seconds(
                cycle),
            "sla_registration": thresholds.sla_v3_registration_facts(cycle),
            "budget_seconds": max(0.0, float(remaining)),
        }
        # Strict cutoff: at the exact boundary the child is already late and
        # must not be launched merely to manufacture a successful-looking
        # push terminal.
        if remaining <= 0:
            return {
                **base,
                "returncode": _proc.RC_TIMEOUT,
                "timed_out": True,
                "started": False,
                "error": (
                    "push absolute cycle deadline reached; child not started"
                ),
            }
        child_rc, stdout, stderr, timed_out = _proc.run_guarded(
            command,
            timeout=remaining,
            cwd=str(ROOT),
            creationflags=_CREATE_NO_WINDOW,
        )
        if stdout:
            sys.stdout.write(stdout)
            sys.stdout.flush()
        if stderr:
            sys.stderr.write(stderr)
            sys.stderr.flush()
        result = {
            **base,
            "returncode": int(child_rc),
            "timed_out": bool(timed_out),
            "started": True,
        }
        if int(child_rc) == 0 and not timed_out:
            result["push_report"] = _last_json_object(stdout or "")
        if timed_out:
            result["error"] = (
                "push absolute cycle deadline reached; process tree terminated"
            )
        return result

    deadline = _live_deadline_at(cycle)
    remaining = (deadline - current).total_seconds()
    base = {
        "absolute_deadline_at": deadline.strftime("%Y-%m-%d %H:%M:%S"),
        "budget_seconds": max(0.0, float(remaining)),
    }
    if remaining < _MINIMUM_LIVE_CHILD_BUDGET_SECONDS:
        return {
            **base,
            "returncode": _proc.RC_TIMEOUT,
            "timed_out": True,
            "started": False,
            "error": (
                "live absolute cycle deadline has insufficient remaining "
                "budget; child not started"
            ),
        }

    prepared_command, same_connection_abort = (
        _prepare_same_connection_abort(stage, cycle, command))
    observer = _LiveChildObserver(
        cycle,
        enforce_analysis_deadline=True,
        expected_session_key=_gateway_session_key("live", cycle),
        expected_stage_runner_pid=os.getpid(),
        auto_start_runner=True,
    )
    runner_watch_stop = threading.Event()
    runner_watch_thread = threading.Thread(
        target=_run_live_runner_autostart_watch,
        args=(observer, runner_watch_stop),
        name=f"live-runner-handoff-{_safe(cycle)}",
        daemon=True,
    )
    runner_watch_thread.start()
    stop_report: dict = {}
    stopping_published = False

    def publish_stopping_from_guard(reason: str) -> None:
        nonlocal stopping_published
        if terminal_callback is None or stopping_published:
            return
        if reason == "timeout":
            guard_rc = _proc.RC_TIMEOUT
            guard_timed_out = True
            observed = None
        elif reason.startswith("guard_error:"):
            guard_rc = _proc.RC_GUARD_ERROR
            guard_timed_out = False
            observed = None
        else:
            guard_rc = _proc.RC_OBSERVED_STOP
            guard_timed_out = False
            observed = reason
        terminal_callback({
            "child_returncode": guard_rc,
            "child_timed_out": guard_timed_out,
            "observed_stop_reason": observed,
        })
        stopping_published = True

    def request_originating_connection_abort(
        _child: subprocess.Popen,
        reason: str,
    ) -> bool:
        publish_stopping_from_guard(reason)
        if same_connection_abort is None:
            return False
        return _request_same_connection_abort(
            same_connection_abort, stage, cycle, reason)

    guard_deadline_monotonic = time.monotonic() + remaining
    try:
        child_rc, stdout, stderr, timed_out = _proc.run_guarded(
            prepared_command,
            timeout=remaining,
            cwd=str(ROOT),
            creationflags=_CREATE_NO_WINDOW,
            observer=observer,
            observer_poll_seconds=_LIVE_OBSERVER_POLL_SECONDS,
            graceful_stop=(
                request_originating_connection_abort
                if same_connection_abort is not None else None
            ),
            graceful_stop_timeout=_SAME_CONNECTION_ABORT_GRACE_SECONDS,
            stop_report=stop_report,
        )
        agent_returncode_before_handoff_wait = int(child_rc)
        post_agent_handoff_wait = None
        if (
            int(child_rc) != _proc.RC_OBSERVED_STOP
            and not timed_out
            and observer.plan_path.exists()
        ):
            wait_started = time.monotonic()
            wait_reason, wait_timed_out = (
                _wait_for_live_handoff_after_agent_exit(
                    observer,
                    deadline_monotonic=guard_deadline_monotonic,
                )
            )
            post_agent_handoff_wait = {
                "attempted": True,
                "agent_returncode": agent_returncode_before_handoff_wait,
                "elapsed_seconds": round(
                    max(0.0, time.monotonic() - wait_started), 3),
                "reason": wait_reason,
                "timed_out": wait_timed_out,
            }
            if wait_timed_out:
                child_rc = _proc.RC_TIMEOUT
                timed_out = True
            elif wait_reason is not None:
                child_rc = _proc.RC_OBSERVED_STOP
    finally:
        runner_watch_stop.set()
        runner_watch_thread.join(timeout=2.0)
    observed_reason = str(
        observer.evidence.get("stop_reason")
        if int(child_rc) == _proc.RC_OBSERVED_STOP else "")
    if terminal_callback is not None and not stopping_published:
        # Publish status=stopping before any Gateway abort attempt.  A late
        # background turn therefore loses runner authority even while the
        # supervisor still owns the live lease for reconciliation.
        terminal_callback({
            "child_returncode": int(child_rc),
            "child_timed_out": bool(timed_out),
            "observed_stop_reason": observed_reason or None,
        })
        stopping_published = True
    supervisor_autostart_evidence = observer.evidence.get(
        "supervisor_runner_autostart")
    supervisor_autostart_enabled = bool(
        isinstance(supervisor_autostart_evidence, dict)
        and supervisor_autostart_evidence.get("enabled") is True
    )
    runner_cleanup = (
        observer.shutdown_supervised_runner(
            allow_exit_grace=bool(
                observed_reason == "business_terminal_committed"
                or observed_reason.startswith("runner_terminal:")
            )
        )
        if supervisor_autostart_enabled else None
    )
    observed_evidence = (
        dict(observer.evidence)
        if int(child_rc) == _proc.RC_OBSERVED_STOP
        else None
    )
    # Preserve the existing trigger log contract while keeping stage-status
    # free of raw model/tool output and channel identifiers.
    if stdout:
        sys.stdout.write(stdout)
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()
    observed_terminal = bool(
        observed_reason == "business_terminal_committed"
        or observed_reason.startswith("runner_terminal:")
        or observed_reason.startswith("analysis_terminal:")
    )
    analysis_failure = observed_reason.startswith("analysis_deadline_exceeded:")
    effective_rc = (
        0 if observed_terminal
        else (_LIVE_ANALYSIS_FAILURE_RC if analysis_failure
              else _LIVE_HANDOFF_FAILURE_RC
              if observed_evidence is not None else int(child_rc))
    )
    result = {
        **base,
        "returncode": int(effective_rc),
        "timed_out": bool(timed_out),
        "started": True,
        "process_tree_terminated": bool(
            stop_report.get(
                "process_tree_terminated",
                observed_evidence is not None or timed_out,
            )),
        "graceful_stop_completed": bool(
            stop_report.get("graceful_completed", False)),
    }
    if post_agent_handoff_wait is not None:
        result["post_agent_handoff_wait"] = post_agent_handoff_wait
    if supervisor_autostart_enabled:
        result["supervisor_runner_handoff"] = dict(
            supervisor_autostart_evidence)
        if runner_cleanup is not None:
            result["supervisor_runner_cleanup"] = runner_cleanup
    deterministic_inputs = observer.evidence.get(
        "deterministic_live_inputs")
    if (
        isinstance(deterministic_inputs, dict)
        and deterministic_inputs.get("enabled") is True
    ):
        result["deterministic_live_inputs"] = dict(deterministic_inputs)
    agent_protocol_evidence = _agent_cli_protocol_failure(stdout)
    if agent_protocol_evidence is not None:
        result["agent_protocol_evidence"] = agent_protocol_evidence
    if same_connection_abort is not None:
        same_connection_abort["process_stop"] = dict(stop_report)
        same_connection_abort = _load_same_connection_receipt(
            same_connection_abort)
        result["same_connection_abort"] = same_connection_abort
    # Any started live child that did not return naturally with rc=0 may have
    # left its Gateway-owned turn alive after the local CLI exited.  The
    # terminal callback above has already published status=stopping, so one
    # exact-session abort is now safe and precedes final status/lease release.
    needs_gateway_abort = bool(
        observed_evidence is not None
        or timed_out
        or int(child_rc) != 0
    )
    if needs_gateway_abort:
        result["gateway_abort"] = _abort_gateway_session(stage, cycle)
        if (
            same_connection_abort is not None
            and result["gateway_abort"].get("terminal_confirmed") is not True
            and _receipt_proves_gateway_terminal_error(
                same_connection_abort)
        ):
            cleanup_probe = result["gateway_abort"]
            receipt = same_connection_abort["receipt"]
            result["gateway_abort"] = {
                "requested": True,
                "rpc": "originating-agent-cli",
                "session_key": _gateway_session_key(stage, cycle, db_root),
                "terminal_confirmed": True,
                "returncode": int(receipt["exit_code"]),
                "status": "gateway-terminal-error",
                "verification_source": (
                    "identity_bound_wrapper_receipt"),
                "cleanup_probe": cleanup_probe,
            }
            # Preserve the exact identity-bound terminal cause at the stage
            # root so Push and SLA consumers do not have to collapse a proved
            # all-models-failed terminal into generic agent_process_failed.
            # This is diagnostic only: returncode/status and all SLA pass
            # semantics remain unchanged.
            result["failure_kind"] = "gateway_terminal_error"
        if same_connection_abort is not None:
            verification = result["gateway_abort"]
            same_connection_abort["terminal_verification"] = {
                "rpc": verification.get("rpc"),
                "status": verification.get("status"),
                "terminal_confirmed": (
                    verification.get("terminal_confirmed") is True),
            }
            result["same_connection_abort"] = same_connection_abort
    if analysis_failure:
        # 2026-08-19 F1 补齐：observer 判定越界时会先取消原连接并收口子进程，
        # analyst_writer 全程没被调用，它自己那条 9:30 占位行路径当然也
        # 没走到——实测 2026-08-19 有 7 轮 live 已派发却零 analysis 行，
        # query_state 的 lost_cycles 恒 FAIL，dispatcher 也只能按「压根没
        # 分析」派失败战报。此处补写同一条占位行（status='error'、
        # 无业务结论、永不覆盖已存在的 'ok' 行）。
        # 放在 Gateway abort/终态复核之后：child 已优雅退出或整树终止，抢在它前头写没
        # 意义；若模型侧仍有活跃 turn 后来写出真行，write_analysis 允许
        # 覆盖 'error'，真事实照样赢。整段吞异常——可见性补丁不得
        # 反过来制造新的失败面。
        try:
            from analyst_writer import commit_deadline_placeholder
            commit_deadline_placeholder(cycle, "full", {
                "refusal": observed_reason,
                "source": "stage_runner.live_observer",
                "child_returncode": int(child_rc),
            }, db_path=(Path(db_root).resolve() if db_root is not None else DB_ROOT) / "analysis.db")
            result["analysis_placeholder_written"] = True
        except Exception:  # noqa: BLE001
            result["analysis_placeholder_written"] = False
    if observed_evidence is not None:
        result["observed_stop"] = observed_evidence
        if not observed_terminal:
            result["failure_kind"] = (
                "analysis_deadline_exceeded"
                if analysis_failure
                else "post_facts_runner_handoff_violation"
            )
            result["error"] = observed_reason
    if timed_out:
        stop_text = (
            "originating-connection abort completed within grace"
            if result["graceful_stop_completed"]
            else "process tree terminated"
        )
        result["error"] = (
            f"live absolute cycle deadline reached; {stop_text}")
    return result


def _settle_late_live_business_output(
    cycle: str,
    mode: str,
    initial: dict,
    *,
    db_root: Path | str | None = None,
    timeout: float = _BUSINESS_OUTPUT_SETTLE_SECONDS,
    poll: float = _BUSINESS_OUTPUT_POLL_SECONDS,
    monotonic_fn=None,
    sleep_fn=None,
) -> tuple[dict, dict | None]:
    """Boundedly wait for a writer commit racing an already-returned CLI.

    OpenClaw can return the foreground CLI at its Gateway timeout while an
    already-started executor/writer tool finishes a few seconds later.  When
    analysis is present and only ``trade_cycles`` is missing, an immediate
    rc86 would race the failure-report path against a real order.  Poll for a
    short, fixed interval; never redispatch or create business output here.
    """
    checks = {
        (str(item.get("db")), str(item.get("table"))): item.get("found") is True
        for item in initial.get("checks", [])
        if isinstance(item, dict)
    }
    eligible = bool(
        initial.get("ok") is not True
        and initial.get("failure_kind") == "business_output_missing"
        and checks.get(("analysis.db", "analysis_runs")) is True
        and checks.get(("live_trades.db", "trade_cycles")) is False
    )
    if not eligible:
        return initial, None

    clock = monotonic_fn or time.monotonic
    sleeper = sleep_fn or time.sleep
    started = clock()
    deadline = started + max(0.0, float(timeout))
    current = initial
    attempts = 0
    while clock() < deadline:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(max(0.01, float(poll)), remaining))
        attempts += 1
        current = verify_business_output("live", cycle, mode)
        if current.get("ok") is True:
            break
    waited = max(0.0, clock() - started)
    return current, {
        "attempted": True,
        "timeout_seconds": float(timeout),
        "poll_seconds": float(poll),
        "attempts": attempts,
        "waited_seconds": round(waited, 3),
        "recovered": current.get("ok") is True,
    }


def _last_json_object(text: str) -> dict | None:
    """Return the last complete JSON object from mixed monitor output."""
    raw = str(text or "")
    lines = raw.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("{"):
            offsets.append(offset + len(line) - len(stripped))
        offset += len(line)

    decoder = json.JSONDecoder()
    for offset in reversed(offsets):
        fragment = raw[offset:]
        try:
            value, end = decoder.raw_decode(fragment)
        except (json.JSONDecodeError, TypeError):
            continue
        # A pretty-printed nested object can also begin a line.  Its decoded
        # suffix starts with the enclosing JSON delimiter, so it is not a
        # top-level monitor result.  Ordinary trailing stderr/warning text is
        # allowed and must not erase an otherwise complete result.
        suffix = fragment[end:].lstrip()
        if suffix.startswith((",", "}", "]")):
            continue
        if isinstance(value, dict):
            return value
    return None


def _strict_collection_failure_terminal(cycle: str, terminal: object) -> bool:
    """Accept only the canonical no-execution collection-failure contract."""
    if not isinstance(terminal, dict):
        return False
    latency_ms = terminal.get("collection_latency_ms")
    if (
        type(latency_ms) is not int
        or latency_ms <= 0
        or latency_ms > 30 * 60 * 1000
    ):
        return False
    try:
        cycle_started = datetime.strptime(cycle, "%Y-%m-%dT%H:%M")
        started_at = datetime.strptime(
            str(terminal["started_at"]), "%Y-%m-%d %H:%M:%S")
        finished_at = datetime.strptime(
            str(terminal["finished_at"]), "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return False
    expected_finished = started_at + timedelta(milliseconds=latency_ms)
    expected_finished_text = expected_finished.strftime("%Y-%m-%d %H:%M:%S")
    expected_request_id = hashlib.sha256(
        f"collection-failure-report|{cycle}|{expected_finished_text}".encode(
            "utf-8")
    ).hexdigest()[:32]
    barrier = terminal.get("report_reconcile_barrier")
    business = terminal.get("business_check")
    checks = business.get("checks") if isinstance(business, dict) else None
    check_identities = {
        (item.get("db"), item.get("table"), item.get("found"))
        for item in checks if isinstance(item, dict)
    } if isinstance(checks, list) else set()
    failed_steps = terminal.get("failed_steps")
    missing = terminal.get("missing_required_sources")
    expected_mode = "hourly" if str(cycle).endswith(":00") else "quarter"
    canonical_required = (
        ("fast", "regime", "slow")
        if expected_mode == "hourly" else ("fast",)
    )
    return bool(
        terminal.get("stage") == "collection"
        and terminal.get("cycle_id") == cycle
        and terminal.get("mode") == expected_mode
        and terminal.get("status") == "failed"
        and terminal.get("failure_kind") == "collection_gate_failed"
        and type(terminal.get("child_returncode")) is int
        and terminal["child_returncode"] == 1
        and type(terminal.get("returncode")) is int
        and terminal["returncode"] == 1
        and cycle_started <= started_at <= cycle_started + timedelta(minutes=2)
        and terminal["finished_at"] == expected_finished_text
        and finished_at >= started_at
        and terminal.get("profile_lease_released") is True
        and terminal.get("same_cycle_live_dispatched") is False
        and type(terminal.get("production_database_writes")) is int
        and terminal["production_database_writes"] == 0
        and type(terminal.get("orders_placed")) is int
        and terminal["orders_placed"] == 0
        and isinstance(failed_steps, list) and bool(failed_steps)
        and all(isinstance(x, str) and re.fullmatch(r"[a-z0-9_:-]{1,80}", x)
                for x in failed_steps)
        and failed_steps == sorted(set(failed_steps))
        and isinstance(missing, list) and bool(missing)
        and all(isinstance(x, str) and re.fullmatch(r"[a-z0-9_:-]{1,80}", x)
                for x in missing)
        and missing == [x for x in canonical_required if x in set(missing)]
        and set(missing).issubset(set(failed_steps))
        and re.fullmatch(
            r"[a-f0-9]{64}",
            str(terminal.get("collection_receipt_sha256") or ""),
        ) is not None
        and isinstance(business, dict)
        and set(business) == {"ok", "checks"}
        and business.get("ok") is True
        and isinstance(checks, list)
        and len(checks) == 3
        and all(
            isinstance(item, dict)
            and set(item) == {"db", "table", "found"}
            and item.get("found") is False
            for item in checks
        )
        and check_identities == {
            ("analysis.db", "analysis_runs", False),
            ("live_trades.db", "trade_cycles", False),
            ("live_trades.db", "trades", False),
        }
        and isinstance(barrier, dict)
        and barrier.get("schema_version") == 1
        and barrier.get("required") is True
        and barrier.get("profile") == "live"
        and barrier.get("cycle_id") == cycle
        and barrier.get("contract_version") == 1
        and barrier.get("request_id") == expected_request_id
        and barrier.get("status") == "ok"
        and type(barrier.get("rc")) is int
        and barrier["rc"] == 0
        and barrier.get("applied") is False
        and barrier.get("blocking") is False
        and barrier.get("p0") is False
        and barrier.get("contract_valid") is True
        and barrier.get("report_safe") is True
        and barrier.get("evidence_kind")
        == "collection_terminal_and_execution_path_absence"
        and barrier.get("started_at") == expected_finished_text
        and barrier.get("finished_at") == expected_finished_text
        and type(barrier.get("findings_count")) is int
        and barrier["findings_count"] == len(missing)
        and type(barrier.get("healed_count")) is int
        and barrier["healed_count"] == 0
        and _ordered_terminal_times(barrier)
    )


def _ordered_terminal_times(payload: dict) -> bool:
    try:
        started = datetime.strptime(str(payload["started_at"]), "%Y-%m-%d %H:%M:%S")
        finished = datetime.strptime(str(payload["finished_at"]), "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return False
    return finished >= started


def _strict_clean_collection_monitor(cycle: str, post_reconcile: object) -> bool:
    """Prove that collection failure is the monitor's only missing live fact."""
    if (
        not isinstance(post_reconcile, dict)
        or type(post_reconcile.get("rc")) is not int
        or post_reconcile["rc"] != 0
        or post_reconcile.get("timed_out") is not False
        or post_reconcile.get("started") is not True
        or post_reconcile.get("deadline_exceeded") is not False
        or type(post_reconcile.get("output")) is not str
    ):
        return False
    payload = _last_json_object(post_reconcile.get("output", ""))
    if not isinstance(payload, dict):
        return False
    if (
        payload.get("skipped")
        or payload.get("cycle_id") != cycle
        or payload.get("profile") != "live"
        or type(payload.get("rc")) is not int
        or payload["rc"] != 0
        or payload.get("ok") is not True
        or payload.get("issue") is not False
        or payload.get("markers") != []
    ):
        return False
    try:
        completed = datetime.strptime(str(payload["ts"]), "%Y-%m-%d %H:%M:%S")
        cycle_start = datetime.strptime(cycle, "%Y-%m-%dT%H:%M")
    except (KeyError, TypeError, ValueError):
        return False
    return cycle_start <= completed < cycle_start + timedelta(
        seconds=thresholds.post_push_monitor_deadline_seconds(cycle))


def _strict_upstream_failure_push_report(
    cycle: str, report: object, terminal: object,
) -> bool:
    if not isinstance(report, dict) or not isinstance(terminal, dict):
        return False
    child_terminal = report.get("upstream_failure")
    send = report.get("steps", {}).get("send") if isinstance(
        report.get("steps"), dict) else None
    return bool(
        report.get("cycle") == cycle
        and report.get("report_mode") == "upstream_failure"
        and report.get("ok") is True
        and report.get("send_status") in {"sent", "duplicate_skip"}
        and isinstance(send, dict)
        and type(send.get("rc")) is int and send["rc"] == 0
        and _strict_collection_failure_terminal(cycle, child_terminal)
        and child_terminal.get("collection_receipt_sha256")
        == terminal.get("collection_receipt_sha256")
    )


def _strict_business_error_push_report(
    cycle: str, report: object, live_status: object,
) -> bool:
    """Prove that a failed Live business terminal was safely reported.

    A decision=error/degraded terminal is still a failed business cycle, but
    its independently attested and delivered ERROR/DEGRADED report is not a
    second Push failure. From the registered boundary this also applies to
    partial batches with matching positive fill counts and trade actions.
    Malformed, unsafe, or unsent reports remain fail-closed.
    """
    if not isinstance(report, dict) or not isinstance(live_status, dict):
        return False
    try:
        live_returncode = int(live_status["returncode"])
    except (KeyError, TypeError, ValueError):
        return False
    live_barrier = live_status.get("report_reconcile_barrier")
    business_check = live_status.get("business_check")
    if not (
        live_status.get("stage") == "live"
        and live_status.get("cycle_id") == cycle
        and live_status.get("status") == "failed"
        and live_returncode != 0
        and live_status.get("failure_kind") == "business_verification_error"
        and live_status.get("profile_lease_released") is True
        and isinstance(business_check, dict)
        and business_check.get("ok") is False
        and isinstance(live_barrier, dict)
        and live_barrier.get("required") is True
        and live_barrier.get("profile") == "live"
        and live_barrier.get("cycle_id") == cycle
        and live_barrier.get("status") == "ok"
        and type(live_barrier.get("rc")) is int
        and live_barrier["rc"] == 0
        and live_barrier.get("blocking") is False
        and live_barrier.get("p0") is False
        and live_barrier.get("contract_valid") is True
        and live_barrier.get("report_safe") is True
    ):
        return False

    steps = report.get("steps")
    if not isinstance(steps, dict):
        return False
    build = steps.get("build")
    send = steps.get("send")
    before_archive = steps.get("business_attestation_pre_archive")
    before_send = steps.get("business_attestation_pre_send")
    if not all(isinstance(item, dict) for item in (
            build, send, before_archive, before_send)):
        return False

    decision = before_archive.get("decision")
    expected_action = {"error": "ERROR", "degraded": "DEGRADED"}.get(
        decision)
    partial_report = bool(
        cycle >= _PARTIAL_BUSINESS_REPORT_FROM
        and decision == "traded"
        and live_returncode == _BUSINESS_FAILURE_RC
        and business_check.get("error") == (
            "RuntimeError: live_trades.db batch_status=partial 非完整成功终态")
        and type(before_archive.get("n_orders")) is int
        and type(before_archive.get("trade_count")) is int
        and before_archive["n_orders"] == before_archive["trade_count"] > 0
        and all(a.get("ok") is True and a.get("required") is True
                and a.get("mode") == "business_terminal"
                for a in (before_archive, before_send))
        and isinstance(build.get("action"), str)
        and bool(build["action"])
        and all(action in {"OPEN_LONG", "OPEN_SHORT", "ADD", "CLOSE", "REDUCE",
                           "CLOSE_LONG", "CLOSE_SHORT", "REDUCE_LONG", "REDUCE_SHORT"}
                for action in build["action"].split("/"))
    )
    from scripts.ledger_recovery import enabled as recovery_enabled
    zero_order_report = bool(
        recovery_enabled(cycle) and decision == "hold"
        and live_returncode == _BUSINESS_FAILURE_RC
        and business_check.get("error") == (
            "RuntimeError: live_trades.db batch_status=partial 非完整成功终态")
        and type(before_archive.get("n_orders")) is int
        and type(before_archive.get("trade_count")) is int
        and before_archive["n_orders"] == before_archive["trade_count"] == 0
        and build.get("action") in {"HOLD", "ADJUST"}
        and report.get("natural_production_evidence") is True
        and report.get("execution_context") == "production"
        and all(a.get("ok") is True and a.get("required") is True
                and a.get("mode") == "business_terminal"
                for a in (before_archive, before_send))
    )
    if zero_order_report:
        expected_action = build["action"]
    expected_count = before_archive.get("trade_count") if partial_report else 0
    binding_fields = (
        "decision", "n_orders", "trade_count", "sha256",
        "inter_report_exchange_required",
        "inter_report_exchange_schema_version", "inter_report_fill_count",
        "inter_report_sha256", "inter_report_window_start_exclusive_cst",
        "inter_report_window_end_inclusive_cst",
    )
    if any(before_archive.get(key) != before_send.get(key)
           for key in binding_fields):
        return False
    if not (
        (expected_action is not None or partial_report)
        and type(before_archive.get("n_orders")) is int
        and before_archive["n_orders"] == expected_count
        and type(before_archive.get("trade_count")) is int
        and before_archive["trade_count"] == expected_count
        and re.fullmatch(
            r"[a-f0-9]{64}", str(before_archive.get("sha256") or ""))
        is not None
        and before_archive.get("inter_report_exchange_required") is True
        and type(before_archive.get("inter_report_exchange_schema_version"))
        is int
        and before_archive["inter_report_exchange_schema_version"] == 2
        and type(before_archive.get("inter_report_fill_count")) is int
        and before_archive["inter_report_fill_count"] >= 0
        and re.fullmatch(
            r"[a-f0-9]{64}",
            str(before_archive.get("inter_report_sha256") or ""),
        ) is not None
    ):
        return False

    def _attested_live_terminal(attestation: dict) -> bool:
        terminal = attestation.get("live_stage_terminal")
        if not isinstance(terminal, dict):
            return False
        nested_barrier = terminal.get("report_reconcile_barrier")
        return bool(
            terminal.get("status") == "failed"
            and type(terminal.get("returncode")) is int
            and terminal["returncode"] == live_returncode
            and terminal.get("finished_at") == live_status.get("finished_at")
            and terminal.get("profile_lease_released") is True
            and terminal.get("same_cycle_active_lease") is False
            and isinstance(nested_barrier, dict)
            and nested_barrier.get("profile") == "live"
            and nested_barrier.get("cycle_id") == cycle
            and nested_barrier.get("status") == "ok"
            and type(nested_barrier.get("rc")) is int
            and nested_barrier["rc"] == 0
            and nested_barrier.get("blocking") is False
            and nested_barrier.get("p0") is False
            and nested_barrier.get("contract_valid") is True
            and nested_barrier.get("report_safe") is True
        )

    return bool(
        report.get("cycle") == cycle
        and report.get("report_mode") == "business_terminal"
        and report.get("ok") is True
        and report.get("send_status") in {"sent", "duplicate_skip"}
        and build.get("ok") is True
        and (partial_report or build.get("action") == expected_action)
        and type(build.get("n_trades")) is int
        and build["n_trades"] == expected_count
        and type(send.get("rc")) is int
        and send["rc"] == 0
        and _attested_live_terminal(before_archive)
        and _attested_live_terminal(before_send)
    )


def build_complete_cycle_sla(
    cycle: str,
    post_reconcile: dict,
    *,
    live_status: dict | None = None,
    upstream_failure: dict | None = None,
    live_status_absent: bool = False,
) -> dict:
    """Build the forward-only strict complete-cycle latency contract.

    Before the V3 registration boundary, the historical clock stops at the
    clean post-Push monitor.  Starting at that boundary it stops at the live
    report reconcile barrier, so Push is measured separately under §3.  Old
    cycles are never re-judged.  Exactly 870 seconds is late in both calibers.
    """
    registration = thresholds.sla_v3_registration_facts(cycle)
    business_stop = thresholds.complete_cycle_uses_business_terminal_stop(cycle)
    record_stop = thresholds.complete_cycle_uses_record_reconcile_stop(cycle)
    base = {
        "schema_version": 4 if business_stop else 3 if record_stop else 2,
        "measurement": (
            "cycle_start_to_successful_analysis_judgment_trade_terminal"
            if business_stop
            else "cycle_start_to_successful_live_record_reconcile"
            if record_stop
            else "cycle_start_to_successful_post_live_reconcile"
        ),
        "threshold_seconds": _COMPLETE_CYCLE_SLA_SECONDS,
        "comparison": "<",
        "complete": False,
        "under_14m30": False,
        "strict_cycle_pass": False,
        "registration": registration,
    }
    collection_failed = _strict_collection_failure_terminal(
        cycle, upstream_failure)
    if collection_failed and live_status_absent and live_status == {}:
        if not _strict_clean_collection_monitor(cycle, post_reconcile):
            return {**base, "status": "incomplete", "reason": "monitor_not_clean"}
        return {
            **base,
            "status": "incomplete",
            "reason": "upstream_collection_failed",
            "upstream_failure_kind": "collection_gate_failed",
        }
    if business_stop:
        collection_gate = (
            live_status.get("collection_gate")
            if isinstance(live_status, dict) else None
        )
        if not isinstance(collection_gate, dict):
            return {
                **base,
                "status": "incomplete",
                "reason": "collection_gate_missing",
            }
        if collection_gate.get("status") != "met":
            return {
                **base,
                "status": "incomplete",
                "reason": "collection_gate_not_met",
                "collection_gate": collection_gate,
            }
    if live_status is not None:
        if not isinstance(live_status, dict) or not live_status:
            return {
                **base,
                "status": "incomplete",
                "reason": "live_stage_status_missing",
            }
        try:
            live_returncode = int(live_status.get("returncode", -1))
        except (TypeError, ValueError):
            return {
                **base,
                "status": "incomplete",
                "reason": "live_stage_status_invalid",
                "live_stage_status": live_status.get("status"),
            }
        if (
            live_status.get("status") != "succeeded"
            or live_returncode != 0
        ):
            return {
                **base,
                "status": "incomplete",
                "reason": "live_stage_not_succeeded",
                "live_stage_status": live_status.get("status"),
                "live_failure_kind": live_status.get("failure_kind"),
            }
    if business_stop:
        business = (
            live_status.get("business_check", {}).get("business_terminal")
            if isinstance(live_status, dict)
            and isinstance(live_status.get("business_check"), dict)
            else None
        )
        if not isinstance(business, dict):
            return {
                **base,
                "status": "incomplete",
                "reason": "business_terminal_missing",
            }
        try:
            cycle_start = _cycle_start_at(cycle)
            collection_completed = datetime.strptime(
                str(collection_gate["completed_at"]), "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=CST)
            completed_at = datetime.strptime(
                str(business["completed_at_cst"]), "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=CST)
        except (KeyError, TypeError, ValueError):
            return {
                **base,
                "status": "incomplete",
                "reason": "business_terminal_timestamp_invalid",
            }
        if (
            business.get("schema_version") != 1
            or business.get("cycle_id") != cycle
            or business.get("status") != "completed"
            or completed_at < collection_completed
            or collection_completed < cycle_start
        ):
            return {
                **base,
                "status": "incomplete",
                "reason": "business_terminal_contract_invalid",
            }
        elapsed = int((completed_at - cycle_start).total_seconds())
        under = elapsed < _COMPLETE_CYCLE_SLA_SECONDS
        return {
            **base,
            "status": "met" if under else "late",
            "reason": "ok" if under else "elapsed_not_strictly_under_threshold",
            "complete": True,
            "under_14m30": under,
            "strict_cycle_pass": under,
            "completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": elapsed,
            "collection_gate": collection_gate,
            "business_terminal_gate": {
                "status": "met" if under else "late",
                "threshold_seconds": _COMPLETE_CYCLE_SLA_SECONDS,
                "comparison": "<",
                "completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_seconds": elapsed,
                "met": under,
            },
            "post_business_processing": {
                "included_in_870_seconds": False,
                "components": registration.get("excluded_from_870_seconds", []),
            },
        }
    if record_stop:
        barrier = (
            live_status.get("report_reconcile_barrier")
            if isinstance(live_status, dict) else None
        )
        if not isinstance(barrier, dict):
            return {
                **base,
                "status": "incomplete",
                "reason": "record_reconcile_barrier_missing",
            }
        if (
            barrier.get("required") is not True
            or barrier.get("profile") != "live"
            or barrier.get("cycle_id") != cycle
            or barrier.get("status") not in {"ok", "applied"}
            or type(barrier.get("rc")) is not int
            or barrier.get("rc") != 0
            or barrier.get("contract_valid") is not True
            or barrier.get("report_safe") is not True
        ):
            return {
                **base,
                "status": "incomplete",
                "reason": "record_reconcile_barrier_not_clean",
            }
        try:
            cycle_start = _cycle_start_at(cycle)
            started_at = datetime.strptime(
                str(barrier["started_at"]), "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=CST)
            completed_at = datetime.strptime(
                str(barrier["finished_at"]), "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=CST)
        except (KeyError, TypeError, ValueError):
            return {
                **base,
                "status": "incomplete",
                "reason": "record_reconcile_completion_ts_invalid",
            }
        if completed_at < started_at or completed_at < cycle_start:
            return {
                **base,
                "status": "incomplete",
                "reason": "record_reconcile_completion_ts_invalid",
            }
        elapsed = int((completed_at - cycle_start).total_seconds())
        under = elapsed < _COMPLETE_CYCLE_SLA_SECONDS
        record_gate_seconds = (
            thresholds.sla_record_reconcile_deadline_seconds(cycle))
        record_gate_met = elapsed < record_gate_seconds
        strict_pass = bool(under and record_gate_met)
        reason = (
            "ok" if strict_pass
            else "record_reconcile_deadline_exceeded"
            if not record_gate_met
            else "elapsed_not_strictly_under_threshold"
        )
        return {
            **base,
            "status": "met" if strict_pass else "late",
            "reason": reason,
            "complete": True,
            "under_14m30": under,
            "strict_cycle_pass": strict_pass,
            "completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": elapsed,
            "record_reconcile_gate": {
                "threshold_seconds": record_gate_seconds,
                "comparison": "<",
                "met": record_gate_met,
            },
        }
    if not isinstance(post_reconcile, dict):
        return {**base, "status": "incomplete", "reason": "monitor_result_missing"}
    if int(post_reconcile.get("rc", -1)) != 0:
        return {**base, "status": "incomplete", "reason": "monitor_rc_nonzero"}
    payload = _last_json_object(post_reconcile.get("output", ""))
    if payload is None:
        return {**base, "status": "incomplete", "reason": "monitor_output_invalid"}
    if payload.get("skipped"):
        return {
            **base,
            "status": "incomplete",
            "reason": f"monitor_skipped:{payload.get('skipped')}",
        }
    if payload.get("ok") is not True or payload.get("issue") is not False:
        return {**base, "status": "incomplete", "reason": "monitor_not_clean"}
    try:
        cycle_start = datetime.strptime(cycle, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
        completed_at = datetime.strptime(
            str(payload["ts"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
    except (KeyError, TypeError, ValueError):
        return {**base, "status": "incomplete", "reason": "completion_ts_invalid"}
    elapsed = int((completed_at - cycle_start).total_seconds())
    if elapsed < 0:
        return {**base, "status": "incomplete", "reason": "completion_before_cycle"}
    under = elapsed < _COMPLETE_CYCLE_SLA_SECONDS
    return {
        **base,
        "status": "met" if under else "late",
        "reason": "ok" if under else "elapsed_not_strictly_under_threshold",
        "complete": True,
        "under_14m30": under,
        "strict_cycle_pass": under,
        "completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": elapsed,
    }


def _forward_post_push_failure(
    cycle: str,
    push_mode: str,
    post_reconcile: dict,
    complete_cycle_sla: dict,
    live_status: dict,
    *,
    upstream_failure: dict | None = None,
    push_report: dict | None = None,
    live_status_absent: bool = False,
) -> dict | None:
    """Classify the forward-only push/reconcile terminal contract.

    Failure-report pushes and safely attested business-error reports for an
    already-failed live stage remain reportable; they are intentionally
    SLA-incomplete but are not turned into a second push-stage failure.  A
    genuinely successful live stage, however, requires a clean reconciliation
    strictly before the cycle-resolved monitor guard.
    """
    if not _push_reconcile_deadline_enabled(cycle):
        return None
    if not isinstance(post_reconcile, dict):
        post_reconcile = {}
    if not isinstance(complete_cycle_sla, dict):
        complete_cycle_sla = {}
    mode = str(push_mode or "").strip().lower()
    try:
        live_returncode = int(live_status.get("returncode", -1))
        live_returncode_valid = True
    except (TypeError, ValueError, AttributeError):
        live_returncode = -1
        live_returncode_valid = False
    live_stage_succeeded = bool(
        isinstance(live_status, dict)
        and live_status.get("status") == "succeeded"
        and live_returncode_valid
        and live_returncode == 0
    )
    live_stage_explicitly_failed = bool(
        isinstance(live_status, dict)
        and live_status.get("status") == "failed"
        and live_returncode_valid
        and live_returncode != 0
    )
    # The only exception is an intentional WAIT/zero-risk failure report for
    # a live stage that is itself already a proved failure terminal.  Missing,
    # malformed, running, or mode-mismatched live state stays fail-closed.
    if mode == "failure_report" and live_stage_explicitly_failed:
        return None
    if (
        mode == "failure_report"
        and _strict_collection_failure_terminal(cycle, upstream_failure)
        and live_status_absent
        and live_status == {}
        and _strict_upstream_failure_push_report(
            cycle, push_report, upstream_failure)
        and _strict_clean_collection_monitor(cycle, post_reconcile)
        and complete_cycle_sla.get("status") == "incomplete"
        and complete_cycle_sla.get("reason") == "upstream_collection_failed"
    ):
        return None
    if (
        mode == "full"
        and live_stage_explicitly_failed
        and _strict_business_error_push_report(cycle, push_report, live_status)
        and _strict_clean_collection_monitor(cycle, post_reconcile)
        and complete_cycle_sla.get("status") == "incomplete"
        and complete_cycle_sla.get("reason") == "live_stage_not_succeeded"
    ):
        return None
    if mode != "full" or not live_stage_succeeded:
        return {
            "returncode": _POST_PUSH_RECONCILE_FAILURE_RC,
            "failure_kind": "post_push_reconcile_failed",
            "deadline_exceeded": False,
        }

    # V3 moves the SLA clock stop before Push, but the post-Push monitor remains
    # an independent consumer-side safety gate.  Validate it directly instead
    # of accidentally treating a clean pre-Push record barrier as proof that
    # the later monitor was also clean.
    if (
        thresholds.complete_cycle_uses_record_reconcile_stop(cycle)
        or thresholds.complete_cycle_uses_business_terminal_stop(cycle)
    ):
        if post_reconcile.get("deadline_exceeded") is True:
            return {
                "returncode": _proc.RC_TIMEOUT,
                "failure_kind": "cycle_deadline_exceeded",
                "deadline_exceeded": True,
            }
        if not _strict_clean_collection_monitor(cycle, post_reconcile):
            return {
                "returncode": _POST_PUSH_RECONCILE_FAILURE_RC,
                "failure_kind": "post_push_reconcile_failed",
                "deadline_exceeded": False,
            }
        return None

    elapsed = complete_cycle_sla.get("elapsed_seconds")
    timestamp_at_or_after_deadline = bool(
        isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and float(elapsed) >= thresholds.sla_record_reconcile_deadline_seconds(
            cycle)
    )
    deadline_failure = bool(
        post_reconcile.get("deadline_exceeded") is True
        or timestamp_at_or_after_deadline
    )
    if deadline_failure:
        return {
            "returncode": _proc.RC_TIMEOUT,
            "failure_kind": "cycle_deadline_exceeded",
            "deadline_exceeded": True,
        }
    if complete_cycle_sla.get("status") != "met":
        return {
            "returncode": _POST_PUSH_RECONCILE_FAILURE_RC,
            "failure_kind": "post_push_reconcile_failed",
            "deadline_exceeded": False,
        }
    return None


def _safe_post_push_classifier(*args, **kwargs) -> dict | None:
    """A verifier exception must still produce a failed supervisor terminal."""
    try:
        return _forward_post_push_failure(*args, **kwargs)
    except Exception as exc:
        return {"returncode": _POST_PUSH_RECONCILE_FAILURE_RC,
                "failure_kind": "push_postcheck_exception",
                "deadline_exceeded": False,
                "error": f"Push postcheck exception: {type(exc).__name__}: {exc}"[:500]}


def _nudge_after_live_release(
    cycle: str,
    released: bool,
    db_root: Path | str | None = None,
) -> dict:
    """Wake dispatch once the live Agent can no longer mutate its slot."""
    if not released:
        return {"nudged": False, "reason": "profile_lease_not_released"}
    if _nudge_mod is None:
        return {"nudged": False, "reason": "nudge_module_unavailable"}
    try:
        return _nudge_mod.nudge(f"stage_runner:live_terminal:{cycle}")
    except Exception as exc:  # nudge must never change the runner outcome
        return {
            "nudged": False,
            "reason": f"nudge_error: {type(exc).__name__}",
        }


def _run_live_report_reconcile_barrier(
    cycle: str,
    *,
    allow_apply: bool = False,
    db_root: Path | str | None = None,
) -> dict:
    """Reconcile exchange-triggered fills after the Agent stops, before push.

    The live profile lease is deliberately still held while this runs.  That
    makes the Agent immutable, keeps the next live cycle out, and lets the
    existing exact-only ``ledger_autoheal`` path become the single report
    release barrier.  No new repair logic is implemented here.
    """
    if str(cycle) < REPORT_RECONCILE_BARRIER_FROM:
        return {
            "schema_version": 1,
            "required": False,
            "activation_cycle": REPORT_RECONCILE_BARRIER_FROM,
        }
    started_at = now_cst()
    try:
        import trigger_agent

        result = trigger_agent._autoheal_ledger(
            "live",
            cycle,
            apply_enabled_override=allow_apply,
        )
    except Exception as exc:  # noqa: BLE001
        result = {
            "contract_version": None,
            "request_id": None,
            "profile": "live",
            "cycle": cycle,
            "db_root": str((Path(db_root).resolve() if db_root is not None else DB_ROOT).resolve()),
            "status": "client_error",
            "applied": False,
            "p0": False,
            "blocking": True,
            "findings": [],
            "healed": [],
            "needs_human": [],
            "rc": 2,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    allowed_status = {
        "ok", "applied", "needs_human", "error", "skipped", "p0_blocked",
    }
    try:
        contract_valid = (
            result.get("contract_version") == 1
            and isinstance(result.get("request_id"), str)
            and len(result["request_id"]) >= 16
            and result.get("profile") == "live"
            and result.get("cycle") == cycle
            and Path(str(result.get("db_root") or "")).resolve()
            == (Path(db_root).resolve() if db_root is not None else DB_ROOT).resolve()
            and result.get("status") in allowed_status
            and type(result.get("applied")) is bool
            and type(result.get("p0")) is bool
            and type(result.get("blocking")) is bool
            and isinstance(result.get("findings"), list)
            and isinstance(result.get("healed"), list)
            and isinstance(result.get("needs_human"), list)
            and type(result.get("rc")) is int
            and result.get("rc") in {0, 1, 2, 3, 4}
            and result.get("blocking") == (result.get("rc") != 0)
            and result.get("p0") == (result.get("rc") == 4)
        )
    except (OSError, TypeError, ValueError):
        contract_valid = False
    report_safe = bool(
        contract_valid
        and result.get("rc") == 0
        and result.get("status") in {"ok", "applied"}
    )
    return {
        "schema_version": 1,
        "required": True,
        "apply_authorized": allow_apply,
        "profile": "live",
        "cycle_id": cycle,
        "contract_version": result.get("contract_version"),
        "request_id": result.get("request_id"),
        "status": result.get("status"),
        "rc": result.get("rc"),
        "applied": result.get("applied") is True,
        "blocking": result.get("blocking") is True,
        "p0": result.get("p0") is True,
        "contract_valid": contract_valid,
        "report_safe": report_safe,
        "started_at": started_at,
        "finished_at": now_cst(),
        "findings_count": len(result.get("findings") or []),
        "healed_count": sum(
            item.get("applied") is True
            for attempt in ((result.get("recovery_chain") or {}).get("attempts")
                            or [{"healed": result.get("healed") or []}])
            for item in attempt.get("healed") or []),
        "planned_heals_count": sum(item.get("applied") is not True
                                   for item in result.get("healed") or []),
        "recovery_chain": result.get("recovery_chain"),
    }


def _send_report_barrier_alert(
    cycle: str,
    barrier: dict,
    db_root: Path | str | None = None,
) -> dict:
    """Keep an unsafe report barrier observable without sending bad facts."""
    if os.environ.get("OKX_STAGE_RUNNER_NO_ALERT") == "1":
        return {"skipped": "OKX_STAGE_RUNNER_NO_ALERT=1"}
    alert_file = STATUS_DIR / f"alert-report-barrier-{_safe(cycle)}.txt"
    alert_file.write_text(
        "⚠️ OKX 报告发布前账实核验未通过 [P1]\n"
        f"· cycle={cycle} status={barrier.get('status')} "
        f"rc={barrier.get('rc')}\n"
        "· 处置：本轮业务报告不外发、不补派、不重推；交易阶段结果保持原样。\n"
        "· 请核 stage-status 的 report_reconcile_barrier 与账实对账告警。\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [sys.executable, str(QQ_PUSH), "--content-file", str(alert_file),
             "--alert", "--dedupe-key", f"report-barrier:{cycle}"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, creationflags=_CREATE_NO_WINDOW,
        )
        return {"rc": int(proc.returncode), "delivered": proc.returncode == 0}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _run_zero_open_watchdog(cycle: str) -> dict:
    """Evaluate exact zero-OPEN slots after live release; never changes stage rc."""
    try:
        from scripts.zero_open_watchdog import (
            evaluate_zero_open_watchdog,
            publish_watchdog_artifact,
            render_alert,
        )
        result = evaluate_zero_open_watchdog(
            DB_ROOT / "analysis.db",
            cycle,
            stage_status_dir=STATUS_DIR,
            live_trades_db=DB_ROOT / "live_trades.db",
        )
        if result.get("status") in {
                "ALERT_CONDITION_OBSERVED", "SOURCE_INCONSISTENT"}:
            try:
                result["artifact_path"] = str(
                    publish_watchdog_artifact(result))
            except Exception as exc:  # noqa: BLE001 - alert remains independent
                result["artifact_error"] = f"{type(exc).__name__}: {exc}"
        if result.get("alert_required") is True:
            try:
                if os.environ.get("OKX_STAGE_RUNNER_NO_ALERT") == "1":
                    result["alert"] = {
                        "skipped": "OKX_STAGE_RUNNER_NO_ALERT=1"}
                else:
                    alert_file = STATUS_DIR / (
                        f"alert-zero-open-{_safe(cycle)}.txt")
                    alert_file.write_text(
                        render_alert(result), encoding="utf-8")
                    process = subprocess.run(
                        [
                            sys.executable, str(QQ_PUSH),
                            "--content-file", str(alert_file),
                            "--alert", "--dedupe-key",
                            str(result.get("alert_dedupe_key")),
                        ],
                        cwd=str(ROOT), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60,
                        creationflags=_CREATE_NO_WINDOW,
                    )
                    result["alert"] = {
                        "rc": int(process.returncode),
                        "delivered": process.returncode == 0,
                    }
            except Exception as exc:  # noqa: BLE001
                result["alert"] = {"error": f"{type(exc).__name__}: {exc}"}
        return result
    except Exception as exc:  # noqa: BLE001 - watchdog cannot change live result
        return {
            "schema": (
                "zero_open_watchdog_v2"
                if thresholds.decision_restriction_removal_active(cycle)
                else "zero_open_watchdog_v1"),
            "cycle_id": cycle,
            "status": "EVALUATOR_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "alert_required": False,
            "business_database_writes": 0,
            "writer_authority": False,
            "executor_authority": False,
            "dispatch_authority": False,
            "retry_authority": False,
            "scheduler_authority": False,
            "auto_order_authority": False,
        }


def _safe(value: str) -> str:
    return _SAFE_RE.sub("-", str(value)).strip("-")[:100] or "unknown"


def _stage_session_key(
    stage: str,
    cycle: str,
    db_root: Path | str | None = None,
) -> str:
    suffix = _root_namespace(db_root)
    tail = f"-{suffix}" if suffix else ""
    return f"{stage}-{cycle_session_token(cycle)}{tail}"


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


def _sqlite_agent_terminal_failure(path: Path, lookup_key: str) -> dict | None:
    """Use the current exact session, excluding framework status publications."""
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            if con.execute("PRAGMA user_version").fetchone()[0] not in (18, 19):
                return None
            node = con.execute(
                "SELECT current_session_id,entry_valid FROM session_nodes WHERE session_key=?",
                (lookup_key,),
            ).fetchone()
            if not node or not node[0] or node[1] != 1:
                return None
            window = con.execute("SELECT session_key FROM session_windows WHERE session_id=?", (node[0],)).fetchone()
            if not window or window[0] != lookup_key:
                return None
            last_assistant = None
            pending_timeout = None
            for count, (raw,) in enumerate(con.execute(
                "SELECT event_json FROM transcript_events WHERE session_id=? ORDER BY seq LIMIT 20001",
                (node[0],),
            ), start=1):
                if count > 20000:
                    return None
                event = json.loads(raw)
                message = event.get("message") if isinstance(event, dict) else None
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                if message.get("provider") == "openclaw" and message.get("model") in {
                    "gateway-injected", "gateway-publication", "delivery-mirror", "synthetic-empty-audio",
                }:
                    continue
                last_assistant = message
                message_stop = str(message.get("stopReason") or "").lower()
                message_error = str(message.get("errorMessage") or "").lower()
                if (message_stop in {"aborted", "error"}
                        and "first-event timeout" in message_error
                        and "did not deliver a first sse event" in message_error):
                    pending_timeout = {
                        "failure_kind": "agent_first_event_timeout",
                        "timed_out": True,
                        "timeout_phase": "first_event",
                        "stop_reason": message_stop,
                    }
                    duration = re.search(
                        r"within (\d+)ms after streaming headers", message_error)
                    if duration and 0 < int(duration[1]) <= 86_400_000:
                        pending_timeout["timeout_seconds"] = int(duration[1]) / 1000
                elif (message_stop in {"aborted", "error"} and "idle timeout" in message_error):
                    pending_timeout = {
                        "failure_kind": "agent_idle_timeout", "timed_out": True,
                        "idle_timed_out": True, "stop_reason": message_stop,
                    }
                elif any(isinstance(part, dict) and part.get("type") == "toolCall"
                         for part in message.get("content") or []):
                    # A real tool continuation proves the stalled attempt was
                    # superseded. A text-only unfinished-work summary does not.
                    pending_timeout = None
        finally:
            con.close()
        if not isinstance(last_assistant, dict):
            return None
        stop = str(last_assistant.get("stopReason") or "").lower()
        result = {"source_format": "sqlite"}
        if stop == "length":
            return {**result, "failure_kind": "model_output_length", "stop_reason": stop}
        if pending_timeout is not None:
            return {**result, **pending_timeout,
                    "later_text_only_terminal_observed": stop == "stop"}
        usage = last_assistant.get("usage") or {}
        if (stop == "stop" and last_assistant.get("content") == [] and isinstance(usage, dict)
                and usage.get("output", usage.get("outputTokens")) == 0):
            return {**result, "failure_kind": "model_empty_output", "stop_reason": stop,
                    "content_blocks": 0, "output_tokens": 0}
        return None
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None


def detect_agent_terminal_failure(
    stage: str,
    cycle: str,
    state_root: Path | None = None,
    db_root: Path | str | None = None,
) -> dict | None:
    """Read only the matching terminal reason; never persist model-chain data.

    Besides explicit output-length exhaustion, OpenClaw can occasionally end a
    session with a normal ``stop`` whose final assistant message has no content
    and zero output tokens.  When the deterministic business post-check also
    failed, that is an agent terminal failure rather than an unexplained writer
    miss.  Only the minimal terminal shape is returned; provider/model/message
    contents are deliberately excluded.
    """
    agent = _STAGE_AGENTS.get(stage)
    if not agent:
        return None
    root = Path(state_root or OPENCLAW_STATE_ROOT)
    sqlite_path = root / "agents" / agent / "agent" / "openclaw-agent.sqlite"
    if sqlite_path.exists():
        # A present current store must never borrow stale migration exports.
        return _sqlite_agent_terminal_failure(
            sqlite_path, f"agent:{agent}:{_stage_session_key(stage, cycle, db_root)}")
    session_dir = root / "agents" / agent / "sessions"
    index_path = session_dir / "sessions.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        lookup_key = f"agent:{agent}:{_stage_session_key(stage, cycle, db_root)}"
        entry = index.get(lookup_key)
        if not isinstance(entry, dict) or not entry.get("sessionId"):
            return None
        session_id = str(entry["sessionId"])
        trajectory = session_dir / f"{session_id}.trajectory.jsonl"
        stop_reason = None
        terminal_error = None
        total_tokens = None
        timeout_records = 0
        idle_timeout = False
        external_abort = False
        fallback_observed = False
        try:
            handle = trajectory.open("r", encoding="utf-8", errors="replace")
        except OSError:
            handle = None
        if handle is not None:
            with handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    data = record.get("data")
                    if isinstance(data, dict):
                        timed_out = bool(data.get("timedOut"))
                        idle_timed_out = bool(data.get("idleTimedOut"))
                        if timed_out or idle_timed_out:
                            timeout_records += 1
                        idle_timeout = idle_timeout or idle_timed_out
                        external_abort = external_abort or bool(
                            data.get("externalAbort"))
                    if record.get("type") == "model.fallback_step":
                        fallback_observed = True
                    if record.get("type") == "trace.artifacts":
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
        if stop_reason == "length":
            result = {
                "failure_kind": "model_output_length",
                "stop_reason": stop_reason,
            }
            if terminal_error:
                result["terminal_error"] = terminal_error
            if total_tokens is not None:
                result["total_tokens"] = total_tokens
            return result

        # A transport/provider attempt can time out before any usable assistant
        # content exists.  Classify from minimal terminal flags only: never copy
        # prompts, responses, provider identifiers, or the fallback chain.
        if idle_timeout:
            return {
                "failure_kind": "agent_idle_timeout",
                "timed_out": True,
                "idle_timed_out": True,
                "external_abort_observed": external_abort,
                "fallback_observed": fallback_observed,
                "timeout_terminal_records": timeout_records,
            }

        transcript = session_dir / f"{session_id}.jsonl"
        last_assistant = None
        with transcript.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                message = record.get("message")
                if (isinstance(message, dict)
                        and str(message.get("role") or "").lower() == "assistant"):
                    last_assistant = message
        if not isinstance(last_assistant, dict):
            return None
        content = last_assistant.get("content")
        usage = last_assistant.get("usage")
        output_tokens = None
        if isinstance(usage, dict):
            raw_output = usage.get("output", usage.get("outputTokens"))
            try:
                output_tokens = int(raw_output)
            except (TypeError, ValueError):
                pass
        if (str(last_assistant.get("stopReason") or "").lower() == "stop"
                and isinstance(content, list) and len(content) == 0
                and output_tokens == 0):
            return {
                "failure_kind": "model_empty_output",
                "stop_reason": "stop",
                "content_blocks": 0,
                "output_tokens": 0,
            }
        return None
    except (OSError, ValueError, TypeError):
        return None


_PROJECT_ROOT = Path(_public_project_path()).resolve()
_CANONICAL_DB_ROOT = (_PROJECT_ROOT / 'db').resolve()
CANONICAL_DB_ROOT = _CANONICAL_DB_ROOT

def _root_namespace(db_root: Path | str | None = None) -> str:
    resolved = Path(db_root or DB_ROOT).resolve()
    if os.path.normcase(os.fspath(resolved)) == os.path.normcase(
        os.fspath(CANONICAL_DB_ROOT)
    ):
        return ""
    return "r" + hashlib.sha256(
        os.path.normcase(os.fspath(resolved)).encode("utf-8")
    ).hexdigest()[:10]


def _status_path(
    stage: str,
    cycle: str,
    db_root: Path | str | None = None,
) -> Path:
    suffix = _root_namespace(db_root)
    tail = f"-{suffix}" if suffix else ""
    return STATUS_DIR / f"{_safe(stage)}-{cycle_status_token(cycle)}{tail}.json"


def _agent_cli_protocol_failure(stdout: object) -> dict | None:
    """Extract a terminal incomplete-work marker from OpenClaw JSON output.

    ``replayInvalid`` alone is not a failure (successful historical cycles may
    carry it).  The fail-closed combination is liveness ``blocked`` plus an
    ``error`` stop/finish reason.  Provider/model identity is deliberately not
    copied into stage status; exact provider diagnostics remain in the trigger
    log and the read-only SLA audit.
    """
    text = str(stdout or "").strip()
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(text[start:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    meta = result.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    completion = meta.get("completion")
    completion = completion if isinstance(completion, dict) else {}
    liveness = str(meta.get("livenessState") or "").strip().lower()
    stop_reason = str(
        meta.get("stopReason") or completion.get("stopReason") or ""
    ).strip().lower()
    finish_reason = str(completion.get("finishReason") or "").strip().lower()
    if liveness != "blocked" or "error" not in {stop_reason, finish_reason}:
        return None
    final_text = str(meta.get("finalAssistantVisibleText") or "").strip()
    if not final_text:
        payloads = result.get("payloads")
        if isinstance(payloads, list):
            for item in reversed(payloads):
                if isinstance(item, dict) and str(item.get("text") or "").strip():
                    final_text = str(item["text"]).strip()
                    break
    tool_summary = meta.get("toolSummary")
    tool_summary = tool_summary if isinstance(tool_summary, dict) else {}
    return {
        "schema_version": 1,
        "replay_invalid": meta.get("replayInvalid") is True,
        "liveness_state": liveness,
        "stop_reason": stop_reason or None,
        "finish_reason": finish_reason or None,
        "tool_calls": (
            int(tool_summary["calls"])
            if isinstance(tool_summary.get("calls"), int) else None
        ),
        "final_assistant_text": final_text[:300] or None,
        "business_semantics": "agent_stopped_before_required_writer_terminal",
    }


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def _failure_cause_lines(failure_detail: dict) -> list[str]:
    """从 failure_detail 提取人读得懂的直接原因行（尽力而为，绝不抛异常）。

    2026-08-22：主人反馈告警「不知道什么错误」——类别行之外，把 stop_reason、
    marker_error、分析产物状态、runner_state.error（如 PlanError）抽成中文行，
    原始 JSON 仍整体保留在后。
    """
    lines: list[str] = []
    try:
        business = failure_detail.get("business_check") or failure_detail
        if isinstance(business, dict):
            validation = business.get("analysis_validation")
            if isinstance(validation, dict):
                lines.append(
                    f"· 分析预检：失败{validation.get('failed_attempts')}/"
                    f"{validation.get('max_failed_attempts')}次；"
                    + "；".join(validation.get("last_errors") or ["详见该周期验证记录"]))
            for cause in (business.get("execution_failures") or [])[:3]:
                if isinstance(cause, dict):
                    lines.append(
                        f"· 执行原因：{cause.get('action') or '-'} "
                        f"{cause.get('symbol') or '-'}/{cause.get('side') or '-'}: "
                        f"{cause.get('reason') or '-'}；{cause.get('detail') or ''}")
        observed = failure_detail.get("observed_stop") or {}
        stop_reason = str(observed.get("stop_reason")
                          or failure_detail.get("stop_reason") or "")
        if stop_reason:
            sub = (stop_reason.split(":", 1)[1]
                   if ":" in stop_reason else stop_reason)
            zh = _zh_labels.stage_stop_zh(sub)
            lines.append(f"· 直接原因：{stop_reason}"
                         + (f"（{zh}）" if zh else ""))
        marker_error = str(observed.get("marker_error") or "")
        if marker_error:
            zh = _zh_labels.stage_stop_zh(marker_error)
            lines.append(f"· 凭证校验：{marker_error}"
                         + (f"（{zh}）" if zh else ""))
        analysis_state = observed.get("analysis_state") or {}
        if analysis_state and not analysis_state.get("exists"):
            lines.append("· 分析产物：本槽分析从未产出")
        elif analysis_state.get("exists") and analysis_state.get("timely") is False:
            lines.append("· 分析产物：已产出但超过截止时间")
        runner_state_path = str(observed.get("runner_state") or "")
        if runner_state_path:
            try:
                state = json.loads(Path(runner_state_path)
                                   .read_text(encoding="utf-8"))
                err = str(state.get("error") or "")
                if err:
                    lines.append(f"· runner 落败原因（原文）：{err[:200]}")
            except Exception:
                pass
        terminal = failure_detail.get("agent_terminal_evidence") or {}
        protocol = (
            terminal.get("agent_protocol_evidence")
            if isinstance(terminal, dict) else None
        )
        if not isinstance(protocol, dict):
            business = failure_detail.get("business_check") or {}
            protocol = (
                business.get("agent_protocol_evidence")
                if isinstance(business, dict) else None
            )
        if isinstance(protocol, dict):
            lines.append(
                "· Agent 协议终止："
                f"liveness={protocol.get('liveness_state') or '-'}, "
                f"stop={protocol.get('stop_reason') or '-'}"
            )
            final_text = str(protocol.get("final_assistant_text") or "").strip()
            if final_text:
                lines.append(f"· Agent 最终原文：{final_text[:200]}")
        post_reason = str(failure_detail.get("post_reconcile_reason") or "")
        if post_reason:
            zh = _zh_labels.stage_stop_zh(post_reason)
            lines.append(f"· 直接原因：{post_reason}"
                         + (f"（{zh}）" if zh else ""))
    except Exception:
        return lines
    return lines


def _send_failure_alert(stage: str, cycle: str, rc: int,
                        status_path: Path,
                        failure_detail: dict | None = None,
                        db_root: Path | str | None = None) -> dict:
    if os.environ.get("OKX_STAGE_RUNNER_NO_ALERT") == "1":
        return {"skipped": "OKX_STAGE_RUNNER_NO_ALERT=1"}
    alert_file = STATUS_DIR / f"alert-{_safe(stage)}-{_safe(cycle)}.txt"
    detail_line = ""
    if failure_detail:
        kind = str(failure_detail.get("failure_kind") or "")
        kind_zh = _zh_labels.failure_kind_zh(kind)
        if kind:
            detail_line += (
                f"· 失败类别：{kind}（{kind_zh}）\n" if kind_zh
                else f"· 失败类别：{kind}\n"
            )
        for cause_line in _failure_cause_lines(failure_detail):
            detail_line += cause_line + "\n"
        detail_line += (
            "· 业务后置校验（原始JSON，程序字段保留英文）："
            + json.dumps(failure_detail, ensure_ascii=False, separators=(",", ":"))[:900]
            + "\n"
        )
    severity = "P1"
    from scripts.ledger_recovery import enabled as recovery_enabled
    if stage == "live" and recovery_enabled(cycle):
        try:
            snapshot = json.loads(status_path.read_text(encoding="utf-8"))
            barrier = snapshot.get("report_reconcile_barrier") or {}
            if barrier.get("required") is True and barrier.get("report_safe") is not True:
                severity = "P0" if barrier.get("p0") is True else "P1"
                detail_line += (f"· 同轮账实核验：status={barrier.get('status')} "
                                f"rc={barrier.get('rc')}，待处理={barrier.get('findings_count')}，"
                                f"已实际修复={barrier.get('healed_count', 0)}；"
                                "业务报告继续由账实闸判断，相关故障合并通知。\n")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    alert_file.write_text(
        f"⚠️ OKX 阶段执行失败 [{severity}]\n"
        f"· stage={stage} cycle={cycle} rc={rc}\n"
        f"{detail_line}"
        f"· 已记录 failed 终态：{status_path}\n"
        f"· 处置：只读接口瞬时错误在预算内退避重试；整轮不重跑，交易写请求结果须先核实。\n",
        encoding="utf-8",
    )
    try:
        p = subprocess.run(
            [sys.executable, str(QQ_PUSH), "--content-file", str(alert_file),
             "--alert",  # 告警走 C2C 私聊，不混进业务播报群（2026-08-04）
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
    trade_terminal: dict | None = None
    execution_failures: list[dict] = []

    def require(filename: str, table: str,
                columns: str = "1") -> dict | None:
        found, row = _row_exists(root, filename, table, cycle, columns)
        checks.append({"db": filename, "table": table, "found": found})
        if not found:
            raise LookupError(f"{filename}.{table}[{cycle}] 缺失")
        return row

    def require_analysis_terminal() -> dict:
        row = require("analysis.db", "analysis_runs", "status,ts,mode") or {}
        status = str(row.get("status") or "").strip().lower()
        if status not in {"ok", "skipped", "stale"}:
            raise RuntimeError(
                f"analysis status={status or 'missing'} 非成功终态")
        if str(cycle) >= _ANALYSIS_DEADLINE_GUARD_FROM:
            try:
                written_at = datetime.strptime(
                    str(row.get("ts") or ""),
                    "%Y-%m-%d %H:%M:%S",
                ).replace(tzinfo=CST)
            except ValueError as exc:
                raise RuntimeError(
                    "analysis_deadline_exceeded: writer ts 不可校验"
                ) from exc
            deadline = _analysis_deadline_at(cycle)
            if written_at >= deadline:
                raise RuntimeError(
                    "analysis_deadline_exceeded: "
                    f"writer_ts={written_at:%Y-%m-%d %H:%M:%S} "
                    f"deadline={deadline:%Y-%m-%d %H:%M:%S}"
                )
        return row

    def require_trade_terminal(filename: str) -> dict:
        row = require(
            filename, "trade_cycles", "decision,n_orders,ts,raw") or {}
        # Keep the rejection that produced the failed terminal visible. The
        # generic decision/batch check below still owns the failure verdict.
        try:
            raw_details = row.get("raw")
            receipt_details = json.loads(raw_details) if isinstance(raw_details, str) else raw_details
            if isinstance(receipt_details, dict) and receipt_details.get("cycle_id") == cycle:
                for item in (receipt_details.get("position_action_failures") or [])[:3]:
                    if not isinstance(item, dict):
                        continue
                    request, result = item.get("request"), item.get("result")
                    if not isinstance(request, dict):
                        continue
                    result = result if isinstance(result, dict) else {}
                    protection = result.get("protection_sync")
                    protection = protection if isinstance(protection, dict) else {}
                    def compact(value):
                        return " ".join(str(value or "").split())[:220]
                    execution_failures.append({
                        "action": compact(request.get("action")),
                        "symbol": compact(request.get("symbol")),
                        "side": compact(request.get("side") or request.get("pos_side")),
                        "reason": compact(result.get("reject_reason")
                                          or protection.get("reject_reason") or "action_incomplete"),
                        "detail": compact(result.get("reject_detail")
                                          or protection.get("reject_detail") or item.get("problem")),
                    })
        except (TypeError, ValueError):
            pass  # Malformed raw is still rejected by the existing checks.
        decision = str(row.get("decision") or "").strip().lower()
        try:
            n_orders = int(row.get("n_orders"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{filename} n_orders={row.get('n_orders')!r} 非整数") from exc
        valid = (
            (decision == "traded" and n_orders > 0)
            or (decision in {"hold", "skip"} and n_orders == 0)
        )
        if not valid:
            raise RuntimeError(
                f"{filename} decision={decision or 'missing'},"
                f"n_orders={n_orders} 非成功终态")
        raw = row.get("raw")
        if isinstance(raw, str) and raw.strip():
            try:
                receipt = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"{filename} trade_cycles.raw 不是有效 JSON") from exc
            if isinstance(receipt, dict):
                batch_status = str(
                    receipt.get("batch_status") or ""
                ).strip().lower()
                if batch_status in {"partial", "failed", "uncertain"}:
                    raise RuntimeError(
                        f"{filename} batch_status={batch_status} 非完整成功终态"
                    )
                if thresholds.complete_cycle_uses_business_terminal_stop(cycle):
                    terminal = receipt.get("business_terminal")
                    if not isinstance(terminal, dict):
                        raise RuntimeError("business_terminal proof missing")
                    try:
                        terminal_at = datetime.strptime(
                            str(terminal.get("completed_at_cst") or ""),
                            "%Y-%m-%d %H:%M:%S",
                        ).replace(tzinfo=CST)
                    except ValueError as exc:
                        raise RuntimeError(
                            "business_terminal timestamp invalid") from exc
                    deadline = _cycle_start_at(cycle) + timedelta(
                        seconds=thresholds.COMPLETE_CYCLE_SLA_SECONDS)
                    if (
                        terminal.get("schema_version") != 1
                        or terminal.get("cycle_id") != cycle
                        or terminal.get("status") != "completed"
                        or not _cycle_start_at(cycle) <= terminal_at < deadline
                    ):
                        raise RuntimeError("business_terminal contract invalid")
                    row["business_terminal"] = terminal
        return row

    try:
        if stage == "live" and mode == "unified":
            analysis = require_analysis_terminal()
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
            trade_terminal = require_trade_terminal("live_trades.db")
        elif stage == "live":
            trade_terminal = require_trade_terminal("live_trades.db")
        elif stage == "analyst":
            require_analysis_terminal()
        else:
            return {"ok": True, "skipped": f"stage={stage} 无额外业务后置条件"}
        result = {"ok": True, "checks": checks}
        if isinstance(trade_terminal, dict) and isinstance(
            trade_terminal.get("business_terminal"), dict
        ):
            result["business_terminal"] = trade_terminal["business_terminal"]
        return result
    except LookupError as exc:
        validation_detail = {}
        if any(check.get("db") == "analysis.db" and check.get("found") is False
               for check in checks):
            try:
                state_dir = Path(os.environ.get(
                    "OKX_ANALYSIS_VALIDATION_STATE_DIR",
                    str(root.parent / "logs" / "analysis-validation")))
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)", cycle):
                    raise ValueError("invalid diagnostic cycle")
                state = json.loads((state_dir / f"analysis-{cycle.replace(':', '-')}.json")
                                   .read_text(encoding="utf-8"))
                if (isinstance(state, dict) and state.get("schema_version") == 1
                        and state.get("cycle_id") == cycle
                        and type(state.get("failed_attempts")) is int
                        and 0 < state["failed_attempts"] <= 2
                        and state.get("max_failed_attempts") == 2):
                    last_errors = state.get("last_errors")
                    validation_detail = {"analysis_validation": {
                        "failed_attempts": state["failed_attempts"],
                        "max_failed_attempts": 2,
                        "blocked": state.get("blocked") is True,
                        "last_errors": [" ".join(e.split())[:300] for e in
                                        (last_errors[:3] if isinstance(last_errors, list) else [])
                                        if isinstance(e, str)],
                    }}
            except (OSError, ValueError, TypeError):
                pass  # Diagnostic loss must never change the missing-output verdict.
        return {
            "ok": False,
            "failure_kind": "business_output_missing",
            "error": str(exc),
            "checks": checks,
            **validation_detail,
        }
    except Exception as exc:
        return {
            "ok": False,
            "failure_kind": "business_verification_error",
            "error": f"{type(exc).__name__}: {exc}",
            "checks": checks,
            **({"execution_failures": execution_failures} if execution_failures else {}),
        }


def _run_alert_recovery_observer(cycle: str) -> dict:
    """Recovery notification is independent of the committed business result."""
    script = ROOT / "scripts" / "alert_recovery.py"
    config = ROOT / "config" / "alert_recovery.json"
    if not script.exists() or not config.exists():
        return {"status": "not_configured"}
    # Isolated/test roots must never call the production notification channel.
    if DB_ROOT.resolve() != (ROOT / "db").resolve() or STATUS_DIR.resolve() != (ROOT / "logs/stage-status").resolve():
        return {"status": "isolated_context"}
    remaining = (_post_push_monitor_deadline_at(cycle) - datetime.now(CST)).total_seconds()
    if remaining < 3:
        return {"status": "deferred_notification_budget"}
    try:
        rc, stdout, stderr, timed_out = _proc.run_guarded(
            [sys.executable, str(script), "--cycle", cycle,
             "--root", str(ROOT), "--send"],
            cwd=str(ROOT), timeout=min(65.0, remaining),
        )
        if timed_out or rc != 0:
            return {"status": "observer_failed", "returncode": rc, "timed_out": timed_out}
        result = json.loads(stdout)
        return {key: result.get(key) for key in (
            "status", "reason", "dedupe_key", "delivery_status", "notification_returncode")
            if key in result}
    except Exception as exc:  # observer failure never changes trade/SLA state
        return {"status": "observer_failed", "error": type(exc).__name__}


def _run_post_push_monitor(
    cycle: str,
    profile: str,
    *,
    now: datetime | None = None,
) -> dict:
    """push 后运行指定 profile reconciliation；告警由 monitor 自己去重。

    2026-09-13T22:15起，成功Live释放后允许受租约保护的精确平仓补账，绝不重放交易。
    激活槽前保留独立 240s 历史语义；激活槽起
    使用单点事实源按 cycle 解析的独立 monitor 上界，并由 ``run_guarded``
    整树收口。V4 中它不计入 870 秒。
    """
    if os.environ.get("OKX_POST_PUSH_RECONCILE", "1") == "0":
        return {"skipped": "OKX_POST_PUSH_RECONCILE=0"}
    command = [
        sys.executable,
        str(LIVE_RECON_MONITOR),
        "--cycle",
        cycle,
        "--profile",
        profile,
    ]
    if profile == "live" and cycle >= "2026-09-13T22:15":
        command.append("--autoheal-exact")
    try:
        if _push_reconcile_deadline_enabled(cycle):
            current = now or datetime.now(CST)
            if current.tzinfo is None:
                current = current.replace(tzinfo=CST)
            current = current.astimezone(CST)
            deadline = _post_push_monitor_deadline_at(cycle)
            remaining = (deadline - current).total_seconds()
            base = {
                "absolute_deadline_at": deadline.strftime(
                    "%Y-%m-%d %H:%M:%S"),
                "deadline_activation_cst": (
                    _PUSH_RECONCILE_DEADLINE_ACTIVATION_CST.isoformat()
                ),
                "deadline_seconds": (
                    thresholds.post_push_monitor_deadline_seconds(cycle)),
                "sla_registration": thresholds.sla_v3_registration_facts(
                    cycle),
                "budget_seconds": max(0.0, float(remaining)),
            }
            if remaining <= 0:
                return {
                    **base,
                    "rc": _proc.RC_TIMEOUT,
                    "output": "",
                    "timed_out": True,
                    "started": False,
                    "deadline_exceeded": True,
                    "error": (
                        "post-push reconciliation absolute cycle deadline "
                        "reached; child not started"
                    ),
                }
            guard_timeout = min(240.0, remaining)
            guard_started_mono = time.monotonic()
            rc, stdout, stderr, timed_out = _proc.run_guarded(
                command,
                # The shared absolute cutoff may only tighten the monitor's
                # established 240s cap; it must never silently widen it.
                timeout=guard_timeout,
                cwd=str(ROOT),
                creationflags=_CREATE_NO_WINDOW,
            )
            guard_elapsed = max(
                0.0, time.monotonic() - guard_started_mono)
            deadline_was_guard_limiter = remaining <= 240.0
            absolute_deadline_reached = guard_elapsed >= remaining
            deadline_exceeded = bool(
                absolute_deadline_reached
                or (timed_out and deadline_was_guard_limiter)
            )
            result = {
                **base,
                "guard_timeout_seconds": guard_timeout,
                "guard_elapsed_seconds": round(guard_elapsed, 3),
                "rc": int(rc),
                "output": ((stdout or "") + (stderr or ""))[-2000:],
                "timed_out": bool(timed_out),
                "started": True,
                "deadline_exceeded": deadline_exceeded,
            }
            if timed_out:
                result["error"] = (
                    "post-push reconciliation absolute cycle deadline "
                    "reached; process tree terminated"
                    if deadline_exceeded
                    else "post-push reconciliation 240s guard timeout; "
                    "process tree terminated"
                )
            return result

        proc = subprocess.run(
            command,
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=240, creationflags=_CREATE_NO_WINDOW)
        return {
            "rc": int(proc.returncode),
            "output": ((proc.stdout or "") + (proc.stderr or ""))[-2000:],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _read_push_live_status(cycle: str) -> tuple[dict, bool]:
    """Read live status while preserving a genuine absent-file sentinel.

    Only ``FileNotFoundError`` proves that no live status file was ever
    created.  Empty objects, invalid JSON, non-object JSON, encoding failures,
    and other read errors are evidence failures and must remain non-empty so
    the collection-report exception cannot accept them as absence.
    """
    path = _status_path("live", cycle)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, True
    except (OSError, UnicodeError) as exc:
        return {
            "_status_evidence_valid": False,
            "_status_evidence_error": type(exc).__name__,
        }, False
    try:
        raw = json.loads(text)
    except (ValueError, TypeError) as exc:
        return {
            "_status_evidence_valid": False,
            "_status_evidence_error": type(exc).__name__,
        }, False
    if not isinstance(raw, dict) or not raw:
        return {
            "_status_evidence_valid": False,
            "_status_evidence_error": "empty_or_non_object",
        }, False
    return raw, False


def main() -> int:
    global DB_ROOT
    ap = argparse.ArgumentParser(description="OKX detached stage lifecycle runner")
    ap.add_argument("--stage", required=True)
    ap.add_argument("--cycle", required=True)
    ap.add_argument("--mode", default="full")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    ap.add_argument("--db-root", default=str(DB_ROOT))
    args = ap.parse_args()
    DB_ROOT = Path(args.db_root).resolve()
    try:
        args.cycle = validate_cycle_id(args.cycle)
    except ValueError as exc:
        ap.error(str(exc))
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
        "sla_registration": thresholds.sla_v3_registration_facts(args.cycle),
    }
    if args.stage == "live":
        try:
            status["absolute_child_deadline_at"] = _live_deadline_at(
                args.cycle).strftime("%Y-%m-%d %H:%M:%S")
            status["absolute_business_terminal_deadline_at"] = (
                _cycle_start_at(args.cycle) + timedelta(
                    seconds=thresholds.sla_business_terminal_deadline_seconds(
                        args.cycle))
            ).strftime("%Y-%m-%d %H:%M:%S")
            status["absolute_analysis_deadline_at"] = _analysis_deadline_at(
                args.cycle).strftime("%Y-%m-%d %H:%M:%S")
            status["absolute_record_reconcile_deadline_at"] = (
                _cycle_start_at(args.cycle) + timedelta(
                    seconds=thresholds.sla_record_reconcile_deadline_seconds(
                        args.cycle))
            ).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            ap.error(f"无效 cycle: {exc}")
    elif args.stage == "push":
        try:
            if _push_reconcile_deadline_enabled(args.cycle):
                status.update({
                    "absolute_cycle_deadline_at": (
                        _push_reconcile_deadline_at(args.cycle).strftime(
                            "%Y-%m-%d %H:%M:%S")
                    ),
                    "deadline_activation_cst": (
                        _PUSH_RECONCILE_DEADLINE_ACTIVATION_CST.isoformat()
                    ),
                    "absolute_post_push_monitor_deadline_at": (
                        _post_push_monitor_deadline_at(args.cycle).strftime(
                            "%Y-%m-%d %H:%M:%S")
                    ),
                })
        except ValueError as exc:
            ap.error(f"无效 cycle: {exc}")
    _write_status(path, status)

    def publish_live_stopping(child_terminal: dict) -> None:
        status.update({
            "status": "stopping",
            "stopping_at": now_cst(),
            "child_terminal": dict(child_terminal),
        })
        _write_status(path, status)

    try:
        # runner 自身由 DETACHED_PROCESS 拉起；其内部再次启动 console 程序时，
        # Windows 仍可能新建控制台。内层只用 CREATE_NO_WINDOW（不再叠 DETACHED），
        # 保持可等待/取退出码，同时彻底阻止 openclaw-agent 定时弹窗。
        child_result = _run_stage_child(
            args.stage,
            args.cycle,
            command,
            terminal_callback=(
                publish_live_stopping if args.stage == "live" else None
            ),
        )
        child_rc = int(child_result["returncode"])
        error = child_result.get("error")
    except Exception as exc:
        child_result = {
            "timed_out": False,
            "started": False,
        }
        child_rc = 127
        error = f"{type(exc).__name__}: {exc}"

    rc = child_rc
    business_check = None
    business_output_settle = None
    failure_kind = None
    terminal_evidence = None
    if child_rc == 0:
        business_check = verify_business_output(
            args.stage, args.cycle, args.mode)
        if args.stage == "live" and not business_check.get("ok"):
            protocol_evidence = child_result.get("agent_protocol_evidence")
            analysis_missing = any(
                isinstance(check, dict)
                and check.get("db") == "analysis.db"
                and check.get("table") == "analysis_runs"
                and check.get("found") is False
                for check in business_check.get("checks") or []
            )
            if isinstance(protocol_evidence, dict) and analysis_missing:
                failure_kind = "agent_protocol_error"
                child_result["failure_kind"] = failure_kind
                business_check = {
                    **business_check,
                    "failure_kind": failure_kind,
                    "agent_protocol_evidence": protocol_evidence,
                }
                business_output_settle = {
                    "attempted": False,
                    "reason": "agent_protocol_terminal_is_not_a_writer_race",
                    "recovered": False,
                }
                try:
                    from analyst_writer import commit_deadline_placeholder
                    commit_deadline_placeholder(args.cycle, "full", {
                        "refusal": "agent_protocol_error",
                        "source": "stage_runner.agent_protocol_terminal",
                        "child_returncode": child_rc,
                        "agent_protocol_evidence": protocol_evidence,
                    }, db_path=DB_ROOT / "analysis.db")
                    child_result["analysis_placeholder_written"] = True
                except Exception:  # noqa: BLE001 - diagnostic must not mask root
                    child_result["analysis_placeholder_written"] = False
            else:
                business_check, business_output_settle = (
                    _settle_late_live_business_output(
                        args.cycle, args.mode, business_check))
        if not business_check.get("ok"):
            rc = _BUSINESS_FAILURE_RC
            if failure_kind is None:
                failure_kind = business_check.get(
                    "failure_kind", "business_verification_error")
            # A local CLI may return rc=0 before its Gateway turn's detached
            # tool work commits.  Missing/invalid business terminal is not a
            # successful natural end: revoke runner authority first, then
            # abort this exact session before final status and lease release.
            if (
                args.stage == "live"
                and child_result.get("started") is True
                and child_result.get("gateway_abort") is None
            ):
                if status.get("status") != "stopping":
                    publish_live_stopping({
                        "child_returncode": child_rc,
                        "child_timed_out": False,
                        "observed_stop_reason": None,
                        "business_failure_kind": failure_kind,
                    })
                child_result["gateway_abort"] = _abort_gateway_session(
                    args.stage, args.cycle)
                same_connection = child_result.get(
                    "same_connection_abort")
                if isinstance(same_connection, dict):
                    verification = child_result["gateway_abort"]
                    same_connection["terminal_verification"] = {
                        "rpc": verification.get("rpc"),
                        "status": verification.get("status"),
                        "terminal_confirmed": (
                            verification.get("terminal_confirmed") is True),
                    }
    if child_result.get("failure_kind"):
        failure_kind = str(child_result["failure_kind"])
        terminal_evidence = {
            "failure_kind": failure_kind,
            "child_started": child_result.get("started") is True,
            "process_tree_terminated": (
                child_result.get("process_tree_terminated") is True),
            "graceful_stop_completed": (
                child_result.get("graceful_stop_completed") is True),
            "observed_stop": child_result.get("observed_stop"),
        }
        if child_result.get("same_connection_abort") is not None:
            terminal_evidence["same_connection_abort"] = (
                _same_connection_abort_summary(
                    child_result["same_connection_abort"]))
        if child_result.get("gateway_abort") is not None:
            terminal_evidence["gateway_abort"] = child_result["gateway_abort"]
        if child_result.get("agent_protocol_evidence") is not None:
            terminal_evidence["agent_protocol_evidence"] = (
                child_result["agent_protocol_evidence"])
    elif child_result.get("timed_out"):
        failure_kind = "cycle_deadline_exceeded"
        terminal_evidence = {
            "failure_kind": failure_kind,
            "absolute_deadline_at": child_result.get(
                "absolute_deadline_at",
                status.get("absolute_child_deadline_at"),
            ),
            "child_started": child_result.get("started") is True,
            "process_tree_terminated": (
                child_result.get("process_tree_terminated") is True),
            "graceful_stop_completed": (
                child_result.get("graceful_stop_completed") is True),
        }
        if child_result.get("same_connection_abort") is not None:
            terminal_evidence["same_connection_abort"] = (
                _same_connection_abort_summary(
                    child_result["same_connection_abort"]))
        if child_result.get("gateway_abort") is not None:
            terminal_evidence["gateway_abort"] = child_result["gateway_abort"]
    elif rc != 0:
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
    if args.stage == "live":
        status["child_timed_out"] = child_result.get("timed_out") is True
        status["child_started"] = child_result.get("started") is True
        # F1：占位行写没写必须外显。status 是逐键挑选而非整体合并 child_result，
        # 漏掉这一键会让「设了但永远看不见」——排查时读到 None 反而会误判成
        # 没触发，比不写更糟。
        if child_result.get("analysis_placeholder_written") is not None:
            status["analysis_placeholder_written"] = bool(
                child_result["analysis_placeholder_written"])
        if child_result.get("budget_seconds") is not None:
            status["child_budget_seconds"] = round(
                float(child_result["budget_seconds"]), 3)
        if child_result.get("gateway_abort") is not None:
            status["gateway_abort"] = child_result["gateway_abort"]
        if child_result.get("same_connection_abort") is not None:
            status["same_connection_abort"] = (
                child_result["same_connection_abort"])
        if child_result.get("observed_stop") is not None:
            status["observed_stop"] = child_result["observed_stop"]
        if child_result.get("post_agent_handoff_wait") is not None:
            status["post_agent_handoff_wait"] = (
                child_result["post_agent_handoff_wait"])
        if child_result.get("supervisor_runner_handoff") is not None:
            status["supervisor_runner_handoff"] = (
                child_result["supervisor_runner_handoff"])
        if child_result.get("supervisor_runner_cleanup") is not None:
            status["supervisor_runner_cleanup"] = (
                child_result["supervisor_runner_cleanup"])
        status["collection_gate"] = _collection_gate_contract(
            args.cycle, db_root=DB_ROOT)
    elif (
        args.stage == "push"
        and _push_reconcile_deadline_enabled(args.cycle)
    ):
        status["child_timed_out"] = child_result.get("timed_out") is True
        status["child_started"] = child_result.get("started") is True
        if child_result.get("budget_seconds") is not None:
            status["child_budget_seconds"] = round(
                float(child_result["budget_seconds"]), 3)
    if error:
        status["error"] = error
    if business_check is not None:
        status["business_check"] = business_check
    if business_output_settle is not None:
        status["business_output_settle"] = business_output_settle
    if failure_kind:
        status["failure_kind"] = failure_kind
    if terminal_evidence:
        status["agent_terminal_evidence"] = terminal_evidence
    if rc == 0 and args.stage == "push":
        # demo 的 post-push dry 对账随 2026-08-06 全量下线移除。
        status["post_live_reconcile"] = _run_post_push_monitor(
            args.cycle, "live")
        live_status, live_status_absent = _read_push_live_status(args.cycle)
        upstream_failure = None
        if args.mode == "failure_report":
            try:
                upstream_failure = load_upstream_failure(
                    args.cycle,
                    db_root=DB_ROOT,
                    status_dir=STATUS_DIR,
                )
            except Exception:  # fail closed in the classifier below
                upstream_failure = None
        status["complete_cycle_sla"] = build_complete_cycle_sla(
            args.cycle,
            status["post_live_reconcile"],
            live_status=live_status,
            upstream_failure=upstream_failure,
            live_status_absent=live_status_absent,
        )
        post_push_failure = _safe_post_push_classifier(
            args.cycle,
            args.mode,
            status["post_live_reconcile"],
            status["complete_cycle_sla"],
            live_status,
            upstream_failure=upstream_failure,
            push_report=child_result.get("push_report"),
            live_status_absent=live_status_absent,
        )
        if post_push_failure is not None:
            rc = int(post_push_failure["returncode"])
            failure_kind = str(post_push_failure["failure_kind"])
            terminal_evidence = {
                "failure_kind": failure_kind,
                "deadline_component": "post_live_reconcile",
                "absolute_deadline_at": (
                    status["post_live_reconcile"].get("absolute_deadline_at")
                    or _push_reconcile_deadline_at(args.cycle).strftime(
                        "%Y-%m-%d %H:%M:%S")
                ),
                "child_started": (
                    status["post_live_reconcile"].get("started") is True
                ),
                "process_tree_terminated": (
                    status["post_live_reconcile"].get("started") is True
                    and status["post_live_reconcile"].get("timed_out") is True
                ),
                "post_reconcile_reason": status[
                    "complete_cycle_sla"].get("reason"),
            }
            failure_error = (
                post_push_failure.get("error")
                or status["post_live_reconcile"].get("error")
                or "post-push reconciliation failed: "
                + str(status["complete_cycle_sla"].get("reason") or "unknown")
            )
            status.update({
                "status": "failed",
                "finished_at": now_cst(),
                "duration_ms": int(
                    (time.monotonic() - started_mono) * 1000),
                "returncode": rc,
                "failure_kind": failure_kind,
                "agent_terminal_evidence": terminal_evidence,
                "error": failure_error,
            })
    _write_status(path, status)
    if rc != 0 and args.stage != "live":
        status["alert"] = _send_failure_alert(
            args.stage, args.cycle, rc, path,
            terminal_evidence or business_check)
        _write_status(path, status)
    if args.stage == "live":
        # Agent 子进程已结束，但 profile lease 仍在：此时执行既有 exact-only
        # autoheal，确保交易所触发的止损/止盈先进入主账，再允许 dispatcher 起 push。
        # A missing/invalid business terminal can still race a background
        # executor tool that OpenClaw detached after its foreground turn
        # returned.  In that state the report barrier remains read-only: an
        # exact-looking GHOST may simply be the order whose Agent writer is a
        # few seconds late.  Applying a second close for the same ordId would
        # double-count one physical fill.  Only a fully successful child plus
        # verified business terminal authorizes the existing exact-only heal.
        reconcile_apply_authorized = bool(
            rc == 0
            and isinstance(business_check, dict)
            and business_check.get("ok") is True
        )
        status["report_reconcile_barrier"] = (
            _run_live_report_reconcile_barrier(
                args.cycle,
                allow_apply=reconcile_apply_authorized,
            ))
        if reconcile_apply_authorized:
            post_reconcile_check = verify_business_output(
                args.stage, args.cycle, args.mode)
            status["post_reconcile_business_check"] = post_reconcile_check
            business_check = post_reconcile_check
            status["business_check"] = post_reconcile_check
            if post_reconcile_check.get("ok") is not True:
                rc = _BUSINESS_FAILURE_RC
                failure_kind = "post_reconcile_business_verification_error"
                terminal_evidence = {
                    "failure_kind": failure_kind,
                    "deadline_component": "post_live_reconcile",
                    "child_started": child_result.get("started") is True,
                    "process_tree_terminated": False,
                    "post_reconcile_business_check": post_reconcile_check,
                }
                status.update({
                    "status": "failed",
                    "finished_at": now_cst(),
                    "duration_ms": int(
                        (time.monotonic() - started_mono) * 1000),
                    "returncode": rc,
                    "failure_kind": failure_kind,
                    "agent_terminal_evidence": terminal_evidence,
                    "error": (
                        "post-reconcile business verification failed: "
                        + str(post_reconcile_check.get("error") or "unknown")
                    ),
                })
            _write_status(path, status)
        if thresholds.complete_cycle_uses_business_terminal_stop(args.cycle):
            pre_push_sla = build_complete_cycle_sla(
                args.cycle, {}, live_status=status)
            status["business_terminal_gate"] = {
                "status": pre_push_sla.get("status"),
                "reason": pre_push_sla.get("reason"),
                "completed_at": pre_push_sla.get("completed_at"),
                "elapsed_seconds": pre_push_sla.get("elapsed_seconds"),
                "collection_gate": pre_push_sla.get("collection_gate"),
                "gate": pre_push_sla.get("business_terminal_gate"),
                "strict_cycle_pass": pre_push_sla.get("strict_cycle_pass"),
                "registration": pre_push_sla.get("registration"),
            }
        elif thresholds.complete_cycle_uses_record_reconcile_stop(args.cycle):
            pre_push_sla = build_complete_cycle_sla(
                args.cycle, {}, live_status=status)
            status["record_reconcile_gate"] = {
                "status": pre_push_sla.get("status"),
                "reason": pre_push_sla.get("reason"),
                "completed_at": pre_push_sla.get("completed_at"),
                "elapsed_seconds": pre_push_sla.get("elapsed_seconds"),
                "gate": pre_push_sla.get("record_reconcile_gate"),
                "strict_cycle_pass": pre_push_sla.get("strict_cycle_pass"),
                "registration": pre_push_sla.get("registration"),
            }
        if (
            status["report_reconcile_barrier"].get("required") is True
            and status["report_reconcile_barrier"].get("report_safe") is not True
        ):
            from scripts.ledger_recovery import enabled as recovery_enabled
            if rc != 0 and recovery_enabled(args.cycle):
                status["report_reconcile_alert"] = {
                    "coalesced_with": f"stage-failed:live:{args.cycle}",
                    "delivered": False,
                    "reason": "same-cycle barrier details included in live failure alert",
                }
            else:
                if str(args.cycle) >= "2026-09-14T11:15":
                    status["report_reconcile_alert"] = {"deferred_until_lease_release": True}
                else:
                    status["report_reconcile_alert"] = _send_report_barrier_alert(
                        args.cycle, status["report_reconcile_barrier"])
        _write_status(path, status)
        try:
            status["profile_lease_released"] = ledger.release_profile_lease(
                DB_ROOT / "ledger.db", args.stage, args.cycle)
        except Exception as exc:
            status["profile_lease_release_error"] = (
                f"{type(exc).__name__}: {exc}")
        _write_status(path, status)
        status["post_release_dispatch_nudge"] = _nudge_after_live_release(
            args.cycle, status.get("profile_lease_released") is True)
        _write_status(path, status)
        if status.get("report_reconcile_alert", {}).get("deferred_until_lease_release") is True:
            status["report_reconcile_alert"] = _send_report_barrier_alert(
                args.cycle, status["report_reconcile_barrier"])
            status["report_reconcile_alert"]["sent_after_lease_release_attempt"] = True
            _write_status(path, status)
        status["zero_open_watchdog"] = _run_zero_open_watchdog(args.cycle)
        _write_status(path, status)
        if rc != 0:
            # Do not let a potentially slow notification hold the live profile
            # lease after the business child is already terminal.
            status["alert"] = _send_failure_alert(
                args.stage, args.cycle, rc, path,
                business_check or terminal_evidence)
            if status.get("report_reconcile_alert", {}).get("coalesced_with"):
                status["report_reconcile_alert"]["delivered"] = (
                    status["alert"].get("delivered") is True)
            _write_status(path, status)
    if args.stage == "push" and rc == 0:
        # Written after delivery, SLA and post-push reconciliation have reached
        # their own final state. Recovery cannot rewrite that outcome.
        status["alert_recovery"] = _run_alert_recovery_observer(args.cycle)
        _write_status(path, status)
    return rc if 0 <= rc <= 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
