# -*- coding: utf-8 -*-
"""Detached stage 监督包装器：记录 running/succeeded/failed，失败只告警不重试。

由 collectors/trigger_agent.py detached 拉起。本脚本同步等待真正的 agent/push
子进程，因此能取得最终退出码；dispatcher 仍只认 stage_dispatch 做幂等，本状态文件
仅用于终态可观测性。任何失败都不会释放闩锁、补派或重试。
"""
from __future__ import annotations

import argparse
import hashlib
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
ROOT = Path(
    os.environ.get("OKX_ROOT") or Path(__file__).resolve().parents[1]
).resolve()
COLLECTORS = ROOT / "collectors"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))
import ledger  # noqa: E402
import _proc  # noqa: E402
from collectors.cycle_contract import (  # noqa: E402
    cycle_session_token,
    cycle_status_token,
    validate_cycle_id,
)
from stage_failure_contract import (  # noqa: E402
    REPORT_RECONCILE_BARRIER_FROM,
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
                  or (ROOT / "logs" / "stage-status"))
QQ_PUSH = ROOT / "scripts" / "qq_push.py"
LIVE_RECON_MONITOR = ROOT / "scripts" / "live_reconcile_monitor.py"
DB_ROOT = Path(os.environ.get("OKX_DB_ROOT") or (ROOT / "db"))
CANONICAL_DB_ROOT = (ROOT / "db").resolve()
OPENCLAW_STATE_ROOT = Path(
    os.environ.get("OKX_OPENCLAW_STATE_ROOT")
    or (Path.home() / ".openclaw")
)
_OPENCLAW_NODE = Path(os.environ.get(
    "OKX_NODE_BIN", r"C:\Program Files\nodejs\node.exe"))
_OPENCLAW_MJS = Path(os.environ.get(
    "OKX_OPENCLAW_MJS",
    str(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" /
        "openclaw" / "openclaw.mjs"),
))
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_BUSINESS_FAILURE_RC = 86
_COMPLETE_CYCLE_SLA_SECONDS = 14 * 60 + 30
_ANALYSIS_DEADLINE_GUARD_FROM = "2026-08-15T21:45"
_ANALYSIS_DEADLINE_SECONDS = 9 * 60 + 30
_LIVE_CHILD_DEADLINE_SECONDS = 13 * 60
_MINIMUM_LIVE_CHILD_BUDGET_SECONDS = 60.0
_GATEWAY_ABORT_RPC_TIMEOUT_MS = 10_000
_GATEWAY_ABORT_PROCESS_TIMEOUT_SECONDS = 15
_BUSINESS_OUTPUT_SETTLE_SECONDS = 45.0
_BUSINESS_OUTPUT_POLL_SECONDS = 0.25
# 今日成功轮从 position_exit 到 plan 的实测为 81.9s--176.6s；45s 会把
# 正常逐仓判断误杀。给判断最多 180s，但 plan 一旦落盘只再给 runner 30s，
# 最外层 cycle+13:00 硬截止仍不可越过。
_LIVE_HANDOFF_AFTER_POSITION_EXIT_SECONDS = 180.0
_LIVE_RUNNER_START_AFTER_PLAN_SECONDS = 30.0
_LIVE_OBSERVER_POLL_SECONDS = 0.5
_LIVE_HANDOFF_FAILURE_RC = 87
_LIVE_ANALYSIS_FAILURE_RC = 88
_STAGE_AGENTS = {
    "analyst": "okx-analyst",
    "live": "okx-live-trader",
}


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _root_namespace(db_root: Path | str | None = None) -> str:
    resolved = Path(db_root or DB_ROOT).resolve()
    if os.path.normcase(os.fspath(resolved)) == os.path.normcase(
        os.fspath(CANONICAL_DB_ROOT)
    ):
        return ""
    return "r" + hashlib.sha256(
        os.path.normcase(os.fspath(resolved)).encode("utf-8")
    ).hexdigest()[:10]


def _live_deadline_at(cycle: str) -> datetime:
    """Return the fail-closed live-child deadline for one natural cycle."""
    cycle_start = datetime.strptime(
        str(cycle), "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    return cycle_start + timedelta(seconds=_LIVE_CHILD_DEADLINE_SECONDS)


def _analysis_deadline_at(cycle: str) -> datetime:
    """Return the prompt/writer/stage analysis cutoff for one natural cycle."""
    cycle_start = datetime.strptime(
        str(cycle), "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    return cycle_start + timedelta(seconds=_ANALYSIS_DEADLINE_SECONDS)


def _gateway_session_key(
    stage: str,
    cycle: str,
    db_root: Path | str | None = None,
) -> str:
    agent_id = _STAGE_AGENTS[stage]
    return f"agent:{agent_id}:{_stage_session_key(stage, cycle, db_root)}"


class _LiveChildObserver:
    """Observe the deterministic post-facts handoff without touching orders.

    The model may decide the plan, but after ``position_exit`` it must hand one
    canonical plan to the fixed runner.  This observer only reads files and the
    trade-cycle terminal.  It never creates a receipt, retries an action, or
    interprets exchange state.
    """

    _ACTIVE_STATES = frozenset({"started", "executing"})
    _TERMINAL_STATES = frozenset({"committed", "failed"})

    def __init__(
        self,
        cycle: str,
        *,
        tmp_root: Path | None = None,
        db_root: Path | None = None,
        now_fn=None,
        enforce_analysis_deadline: bool = False,
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
        self.plan_path = self.tmp_root / f"position_plan_{safe_cycle}.json"
        self.marker_path = (
            self.tmp_root / f"live_runner_state_{safe_cycle}.json")
        self.evidence: dict = {
            "cycle_id": self.cycle,
            "position_exit": str(self.position_exit_path),
            "plan": str(self.plan_path),
            "runner_state": str(self.marker_path),
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _age(self, path: Path) -> float:
        return max(0.0, float(self.now_fn()) - path.stat().st_mtime)

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
            # Observer degradation must not replace the existing cycle+13 hard
            # stop.  The ordinary post-child business check remains fail-closed.
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
        if marker.get("schema_version") != 1:
            errors.append("schema_version")
        if str(marker.get("cycle_id") or "") != self.cycle:
            errors.append("cycle_id")
        state = str(marker.get("state") or "").strip().lower()
        if state not in self._ACTIVE_STATES | self._TERMINAL_STATES:
            errors.append("state")
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

    def _stop(self, reason: str) -> str:
        self.evidence.update({
            "stop_reason": reason,
            "observed_at": now_cst(),
        })
        return reason

    def __call__(self) -> str | None:
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

        expected_plan_hash = None
        expected_facts_hash = None
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
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                expected_facts_hash = None
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
            if state in self._TERMINAL_STATES:
                return self._stop(f"runner_terminal:{state}")

        if self.position_exit_path.exists() and not plan_exists:
            age = self._age(self.position_exit_path)
            self.evidence["position_exit_age_seconds"] = round(age, 3)
            if age >= _LIVE_HANDOFF_AFTER_POSITION_EXIT_SECONDS:
                return self._stop(
                    "post_facts_runner_handoff_violation:no_plan")

        if plan_exists:
            age = self._age(self.plan_path)
            self.evidence["plan_age_seconds"] = round(age, 3)
            if age >= _LIVE_RUNNER_START_AFTER_PLAN_SECONDS:
                return self._stop(
                    "post_facts_runner_handoff_violation:no_valid_runner_marker")
        return None


def _abort_gateway_session(
    stage: str,
    cycle: str,
    db_root: Path | str | None = None,
) -> dict:
    """Abort the Gateway-owned turn after the local CLI timeout.

    On Windows the guarded hard kill terminates the waiting CLI process without
    giving its SIGTERM/SIGINT handler a chance to send ``chat.abort``.  The
    stable OpenClaw Gateway exposes the equivalent first-class
    ``sessions.abort`` RPC, keyed by this cycle's isolated session.
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
    """Run one stage child; live is bounded by the cycle's absolute clock.

    OpenClaw's own ``--timeout`` can include a long gateway/provider queue and
    is deliberately larger than the business SLA.  Letting that timeout own
    the profile lease can make one already-late cycle block the next natural
    slot.  The live child therefore gets only the remaining time until
    ``cycle+13:00``; one minute remains for the existing reconcile barrier,
    push, and post-live monitor.  Timeout kills the complete Windows process
    tree through the shared guarded-process helper.
    """
    resolved_db_root = Path(db_root or DB_ROOT).resolve()
    child_env = os.environ.copy()
    child_env["OKX_DB_ROOT"] = str(resolved_db_root)
    if stage != "live":
        proc = subprocess.run(
            command, cwd=str(ROOT), creationflags=_CREATE_NO_WINDOW,
            env=child_env)
        return {
            "returncode": int(proc.returncode),
            "timed_out": False,
            "started": True,
        }

    current = now or datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    current = current.astimezone(CST)
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

    observer = _LiveChildObserver(
        cycle,
        db_root=resolved_db_root,
        enforce_analysis_deadline=True,
    )
    previous_db_root = os.environ.get("OKX_DB_ROOT")
    os.environ["OKX_DB_ROOT"] = str(resolved_db_root)
    try:
        child_rc, stdout, stderr, timed_out = _proc.run_guarded(
            command,
            timeout=remaining,
            cwd=str(ROOT),
            creationflags=_CREATE_NO_WINDOW,
            observer=observer,
            observer_poll_seconds=_LIVE_OBSERVER_POLL_SECONDS,
        )
    finally:
        if previous_db_root is None:
            os.environ.pop("OKX_DB_ROOT", None)
        else:
            os.environ["OKX_DB_ROOT"] = previous_db_root
    observed_evidence = (
        dict(observer.evidence)
        if int(child_rc) == _proc.RC_OBSERVED_STOP
        else None
    )
    observed_reason = str(
        (observed_evidence or {}).get("stop_reason") or "")
    if terminal_callback is not None:
        # Publish status=stopping before any Gateway abort attempt.  A late
        # background turn therefore loses runner authority even while the
        # supervisor still owns the live lease for reconciliation.
        terminal_callback({
            "child_returncode": int(child_rc),
            "child_timed_out": bool(timed_out),
            "observed_stop_reason": observed_reason or None,
        })
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
    }
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
        if db_root is None:
            result["gateway_abort"] = _abort_gateway_session(stage, cycle)
        else:
            result["gateway_abort"] = _abort_gateway_session(
                stage, cycle, resolved_db_root)
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
        result["error"] = (
            "live absolute cycle deadline reached; process tree terminated"
        )
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
        if db_root is None:
            current = verify_business_output("live", cycle, mode)
        else:
            current = verify_business_output(
                "live", cycle, mode, db_root=Path(db_root).resolve())
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


def build_complete_cycle_sla(
    cycle: str,
    post_reconcile: dict,
    *,
    live_status: dict | None = None,
) -> dict:
    """Build the strict cycle->post-reconcile latency contract.

    ``< 870`` is deliberate: exactly 14:30 is late.  The runtime still keeps
    its earlier 14:00 operational release target as a 30-second safety reserve.
    A skipped or malformed
    post-live monitor is incomplete, not a successful sample, and no timestamp
    is guessed from the earlier push child completion.
    """
    base = {
        "schema_version": 2,
        "measurement": "cycle_start_to_successful_post_live_reconcile",
        "threshold_seconds": _COMPLETE_CYCLE_SLA_SECONDS,
        "comparison": "<",
        "complete": False,
        "under_14m30": False,
    }
    if live_status is not None:
        if not isinstance(live_status, dict) or not live_status:
            return {
                **base,
                "status": "incomplete",
                "reason": "live_stage_status_missing",
            }
        if (
            live_status.get("status") != "succeeded"
            or int(live_status.get("returncode", -1)) != 0
        ):
            return {
                **base,
                "status": "incomplete",
                "reason": "live_stage_not_succeeded",
                "live_stage_status": live_status.get("status"),
                "live_failure_kind": live_status.get("failure_kind"),
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
        "completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": elapsed,
    }


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
        origin = f"stage_runner:live_terminal:{cycle}"
        if db_root is None:
            return _nudge_mod.nudge(origin)
        return _nudge_mod.nudge(origin, db_root=Path(db_root).resolve())
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
    """Read-only reconcile check after the Agent stops, before push.

    The live profile lease is deliberately still held while this runs.  That
    makes the Agent immutable, keeps the next live cycle out, and lets the
    existing ``ledger_autoheal`` contract become the single report release
    barrier.  The public runner never authorizes repair, even if a legacy caller
    passes ``allow_apply=True``.
    """
    resolved_db_root = Path(db_root or DB_ROOT).resolve()
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
            "live", cycle, db_root=resolved_db_root)
    except Exception as exc:  # noqa: BLE001
        result = {
            "contract_version": None,
            "request_id": None,
            "profile": "live",
            "cycle": cycle,
            "db_root": str(resolved_db_root),
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
        "ok", "needs_human", "error", "skipped", "p0_blocked",
    }
    try:
        contract_valid = (
            result.get("contract_version") == 1
            and isinstance(result.get("request_id"), str)
            and len(result["request_id"]) >= 16
            and result.get("profile") == "live"
            and result.get("cycle") == cycle
            and Path(str(result.get("db_root") or "")).resolve()
            == resolved_db_root
            and result.get("status") in allowed_status
            and type(result.get("applied")) is bool
            and result.get("applied") is False
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
        and result.get("status") == "ok"
    )
    return {
        "schema_version": 1,
        "required": True,
        "apply_authorized": False,
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
        "healed_count": len(result.get("healed") or []),
    }


def _send_report_barrier_alert(
    cycle: str,
    barrier: dict,
    db_root: Path | str | None = None,
) -> dict:
    """Keep an unsafe report barrier observable without sending bad facts."""
    if os.environ.get("OKX_STAGE_RUNNER_NO_ALERT") == "1":
        return {"skipped": "OKX_STAGE_RUNNER_NO_ALERT=1"}
    suffix = _root_namespace(db_root)
    tail = f"-{suffix}" if suffix else ""
    alert_file = STATUS_DIR / f"alert-report-barrier-{_safe(cycle)}{tail}.txt"
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
             "--alert", "--dedupe-key", f"report-barrier:{cycle}:{suffix or 'default'}",
             "--db-root", str(Path(db_root or DB_ROOT).resolve())],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, creationflags=_CREATE_NO_WINDOW,
        )
        return {"rc": int(proc.returncode), "delivered": proc.returncode == 0}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


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
    session_dir = root / "agents" / agent / "sessions"
    index_path = session_dir / "sessions.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        lookup_key = (
            f"agent:{agent}:{_stage_session_key(stage, cycle, db_root)}"
        )
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


def _status_path(
    stage: str,
    cycle: str,
    db_root: Path | str | None = None,
) -> Path:
    suffix = _root_namespace(db_root)
    tail = f"-{suffix}" if suffix else ""
    return STATUS_DIR / f"{_safe(stage)}-{cycle_status_token(cycle)}{tail}.json"


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def _send_failure_alert(stage: str, cycle: str, rc: int,
                        status_path: Path,
                        failure_detail: dict | None = None,
                        db_root: Path | str | None = None) -> dict:
    if os.environ.get("OKX_STAGE_RUNNER_NO_ALERT") == "1":
        return {"skipped": "OKX_STAGE_RUNNER_NO_ALERT=1"}
    alert_file = status_path.with_name(f"alert-{status_path.stem}.txt")
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
             "--alert",  # 告警走 C2C 私聊，不混进业务播报群（2026-08-04）
             "--dedupe-key", f"stage-failed:{status_path.stem}",
             "--db-root", str(Path(db_root or DB_ROOT).resolve())],
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
            require_trade_terminal("live_trades.db")
        elif stage == "live":
            require_trade_terminal("live_trades.db")
        elif stage == "analyst":
            require_analysis_terminal()
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


def _run_post_push_monitor(
    cycle: str,
    profile: str,
    db_root: Path | str | None = None,
) -> dict:
    """push 后运行指定 profile dry reconciliation；告警由 monitor 自己去重。

    该检查永远不改变 push stage 的成功/失败，也不 apply/replay。
    """
    if os.environ.get("OKX_POST_PUSH_RECONCILE", "1") == "0":
        return {"skipped": "OKX_POST_PUSH_RECONCILE=0"}
    try:
        proc = subprocess.run(
            [sys.executable, str(LIVE_RECON_MONITOR),
             "--cycle", cycle, "--profile", profile,
             "--db-root", str(Path(db_root or DB_ROOT).resolve())],
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
    ap.add_argument("--cycle", required=True, type=validate_cycle_id)
    ap.add_argument("--mode", default="full")
    ap.add_argument("--db-root", default=str(DB_ROOT))
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    runtime_db_root = Path(args.db_root).resolve()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        ap.error("缺少 -- 后的实际命令")

    started_mono = time.monotonic()
    path = _status_path(args.stage, args.cycle, runtime_db_root)
    status = {
        "stage": args.stage,
        "cycle_id": args.cycle,
        "mode": args.mode,
        "status": "running",
        "started_at": now_cst(),
        "runner_pid": os.getpid(),
        "db_root": str(runtime_db_root),
    }
    if args.stage == "live":
        try:
            status["absolute_child_deadline_at"] = _live_deadline_at(
                args.cycle).strftime("%Y-%m-%d %H:%M:%S")
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
        child_kwargs = {
            "terminal_callback": (
                publish_live_stopping if args.stage == "live" else None
            ),
        }
        if runtime_db_root != DB_ROOT.resolve():
            child_kwargs["db_root"] = runtime_db_root
        child_result = _run_stage_child(
            args.stage, args.cycle, command, **child_kwargs)
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
            args.stage, args.cycle, args.mode, db_root=runtime_db_root)
        if args.stage == "live" and not business_check.get("ok"):
            business_check, business_output_settle = (
                _settle_late_live_business_output(
                    args.cycle, args.mode, business_check,
                    db_root=runtime_db_root))
        if not business_check.get("ok"):
            rc = _BUSINESS_FAILURE_RC
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
                if runtime_db_root == DB_ROOT.resolve():
                    child_result["gateway_abort"] = _abort_gateway_session(
                        args.stage, args.cycle)
                else:
                    child_result["gateway_abort"] = _abort_gateway_session(
                        args.stage, args.cycle, runtime_db_root)
    if child_result.get("failure_kind"):
        failure_kind = str(child_result["failure_kind"])
        terminal_evidence = {
            "failure_kind": failure_kind,
            "child_started": child_result.get("started") is True,
            "process_tree_terminated": True,
            "observed_stop": child_result.get("observed_stop"),
        }
        if child_result.get("gateway_abort") is not None:
            terminal_evidence["gateway_abort"] = child_result["gateway_abort"]
    elif child_result.get("timed_out"):
        failure_kind = "cycle_deadline_exceeded"
        terminal_evidence = {
            "failure_kind": failure_kind,
            "absolute_deadline_at": child_result.get(
                "absolute_deadline_at",
                status.get("absolute_child_deadline_at"),
            ),
            "child_started": child_result.get("started") is True,
            "process_tree_terminated": child_result.get("started") is True,
        }
        if child_result.get("gateway_abort") is not None:
            terminal_evidence["gateway_abort"] = child_result["gateway_abort"]
    elif rc != 0:
        terminal_evidence = detect_agent_terminal_failure(
            args.stage, args.cycle, db_root=runtime_db_root)
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
        if child_result.get("budget_seconds") is not None:
            status["child_budget_seconds"] = round(
                float(child_result["budget_seconds"]), 3)
        if child_result.get("gateway_abort") is not None:
            status["gateway_abort"] = child_result["gateway_abort"]
        if child_result.get("observed_stop") is not None:
            status["observed_stop"] = child_result["observed_stop"]
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
            args.cycle, "live", runtime_db_root)
        try:
            live_status = json.loads(
                _status_path(
                    "live", args.cycle, runtime_db_root
                ).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            live_status = {}
        status["complete_cycle_sla"] = build_complete_cycle_sla(
            args.cycle,
            status["post_live_reconcile"],
            live_status=live_status,
        )
    _write_status(path, status)
    if rc != 0 and args.stage != "live":
        status["alert"] = _send_failure_alert(
            args.stage, args.cycle, rc, path,
            business_check or terminal_evidence, runtime_db_root)
        _write_status(path, status)
    if args.stage == "live":
        # Agent 子进程已结束，但 profile lease 仍在：公开版仅执行只读
        # autoheal 核验；任何账实不一致都会阻止报告发布，不自动补账。
        # A missing/invalid business terminal can still race a background
        # executor tool that OpenClaw detached after its foreground turn
        # returned.  In that state the report barrier remains read-only: an
        # exact-looking GHOST may simply be the order whose Agent writer is a
        # few seconds late.  Applying a second close for the same ordId would
        # double-count one physical fill.  Only a fully successful child plus
        # verified business terminal does not expand public write authority.
        status["report_reconcile_barrier"] = (
            _run_live_report_reconcile_barrier(
                args.cycle,
                db_root=runtime_db_root,
            ))
        if (
            status["report_reconcile_barrier"].get("required") is True
            and status["report_reconcile_barrier"].get("report_safe") is not True
        ):
            status["report_reconcile_alert"] = _send_report_barrier_alert(
                args.cycle, status["report_reconcile_barrier"],
                runtime_db_root)
        _write_status(path, status)
        try:
            status["profile_lease_released"] = ledger.release_profile_lease(
                runtime_db_root / "ledger.db", args.stage, args.cycle)
        except Exception as exc:
            status["profile_lease_release_error"] = (
                f"{type(exc).__name__}: {exc}")
        _write_status(path, status)
        status["post_release_dispatch_nudge"] = _nudge_after_live_release(
            args.cycle, status.get("profile_lease_released") is True,
            runtime_db_root)
        _write_status(path, status)
        if rc != 0:
            # Do not let a potentially slow notification hold the live profile
            # lease after the business child is already terminal.
            status["alert"] = _send_failure_alert(
                args.stage, args.cycle, rc, path,
                business_check or terminal_evidence, runtime_db_root)
            _write_status(path, status)
    return rc if 0 <= rc <= 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
