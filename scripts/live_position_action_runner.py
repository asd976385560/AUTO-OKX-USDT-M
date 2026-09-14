# -*- coding: utf-8 -*-
"""Execute one Agent-authored live action plan atomically.

The Agent owns each HOLD/OPEN/ADD/CLOSE/REDUCE/ADJUST_PROTECTION judgement.
This helper owns deterministic plumbing: validate immutable facts, bind every
new-risk action to the canonical analysis signal, convert an explicit stop-risk
target to contracts, call the existing executor entry points, preserve their
receipts, and commit one cycle receipt in the same process.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for module_path in (ROOT, ROOT / "collectors", ROOT / "scripts"):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

from core import actor_attestation as actor_att  # noqa: E402
from core import order_executor as oe  # noqa: E402
from core import risk_validator as rv  # noqa: E402
from core.candidate_quality_contract import (  # noqa: E402
    closure_retired_authority_reason_errors,
)
from core.decision_card import (  # noqa: E402
    OPEN_EXECUTION_PACKAGE_KEY,
    canonical_open_execution_package,
    closure_retired_structure_paths,
    validate_open_execution_package,
)
import _acceptance_thresholds as thresholds  # noqa: E402
import trades_writer as tw  # noqa: E402
from live_decision_facts import validate_facts  # noqa: E402


ALLOWED_ACTIONS = {"OPEN", "ADD", "CLOSE", "REDUCE", "ADJUST_PROTECTION"}
COMMON_ACTION_KEYS = {"action", "symbol", "pos_side", "reasoning"}
ACTION_KEYS = {
    "OPEN": {"action", "symbol", "side", "target_stop_risk_pct_equity", "lev"},
    "ADD": {"action", "symbol", "side", "target_stop_risk_pct_equity", "lev"},
    "CLOSE": COMMON_ACTION_KEYS,
    "REDUCE": COMMON_ACTION_KEYS | {"reduce_sz"},
    "ADJUST_PROTECTION": COMMON_ACTION_KEYS | {
        "new_sl_trigger_px",
        "new_tp_trigger_px",
        "resize_to_full_position",
        "consolidate_extra_sl",
    },
}
TERMINAL_CONTEXT_KEYS = {
    "action_taken", "batch_status", "decision", "errors", "n_orders", "ok",
    "position_action_failures", "position_action_results", "trades",
}
RUNNER_STATE_SCHEMA_VERSION = 2
HANDOFF_GATE_SCHEMA_VERSION = 2
STAGE_INPUT_HANDOFF_SCHEMA_VERSION = 1
# 2026-08-24：预检拒（preflight_plan 之前/之中的 PlanError，交易所与业务库
# 均零副作用）不再一次定死。允许同 cycle 整文件重写 plan 后重调一次 runner；
# 第二次预检拒或任何进入执行后的失败仍是粘滞 terminal（禁止重跑边界不变，
# 它防的是副作用重复，预检拒可证明尚无副作用）。
PREFLIGHT_MAX_ATTEMPTS = 2
ATOMIC_REPLACE_RETRY_DELAYS_SECONDS = (0.05, 0.10, 0.20, 0.40)
CST = timezone(timedelta(hours=8))
ANALYSIS_DEADLINE_GUARD_FROM = "2026-08-15T21:45"
DEFAULT_STAGE_STATUS_DIR = Path(os.environ.get(
    "OKX_STAGE_STATUS_DIR", ROOT / "logs" / "stage-status"))


class PlanError(ValueError):
    """A plan failed before any exchange or business-database write."""


def _reject_closure_retired_reason(value: object, path: str) -> None:
    errors = closure_retired_authority_reason_errors(str(value or ""))
    if errors:
        raise PlanError(
            f"{path} 禁止使用已删除的周期/候选状态授权词: "
            + ",".join(errors))


def _validate_closure_plan_reasoning(plan: dict[str, Any]) -> None:
    """Reject retired authority in every Agent-owned execution reason."""
    context = plan.get("receipt_context")
    if isinstance(context, dict):
        _reject_closure_retired_reason(
            context.get("reasoning"), "receipt_context.reasoning")
        reviews = context.get("position_reviews")
        if isinstance(reviews, list):
            rows = reviews
        elif isinstance(reviews, dict):
            rows = list(reviews.values())
        else:
            rows = []
        for index, review in enumerate(rows):
            if isinstance(review, dict):
                _reject_closure_retired_reason(
                    review.get("reason"),
                    f"receipt_context.position_reviews[{index}].reason")
    actions = plan.get("actions")
    if isinstance(actions, list):
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            name = str(action.get("action") or "").strip().upper()
            if name in {"CLOSE", "REDUCE", "ADJUST_PROTECTION"}:
                _reject_closure_retired_reason(
                    action.get("reasoning"), f"actions[{index}].reasoning")


def _validated_cycle_id(cycle_id: str) -> str:
    """Return one canonical natural-cycle id before it reaches a file path."""
    cycle = str(cycle_id or "").strip()
    try:
        parsed = datetime.strptime(cycle, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise PlanError(f"cycle_id 非法: {cycle!r}") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M") != cycle:
        raise PlanError(f"cycle_id 非 canonical 自然槽: {cycle!r}")
    if parsed.minute not in {0, 15, 30, 45}:
        raise PlanError(f"cycle_id 非 15 分钟自然槽: {cycle!r}")
    return cycle


def _require_direct_tmp_path(path: Path, label: str) -> Path:
    """Keep production CLI artifacts directly under this deployment's tmp."""
    expected_parent = (ROOT / "tmp").resolve(strict=False)
    candidate = Path(path).resolve(strict=False)
    if candidate.parent != expected_parent:
        raise PlanError(
            f"{label} 必须直接位于受控 tmp 目录: {expected_parent}"
        )
    return candidate


def validate_live_runtime_authority(
    cycle_id: str,
    *,
    db_root: Path,
    status_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Require the live stage, lease, and absolute clock before any executor.

    The CLI always installs this guard.  The library API leaves it injectable
    so isolated tests and offline validators never need a production lease.
    """
    cycle = _validated_cycle_id(cycle_id)
    cycle_start = datetime.strptime(
        cycle, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    current = now or datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    current = current.astimezone(CST)
    deadline = cycle_start + timedelta(
        seconds=thresholds.sla_business_terminal_deadline_seconds(cycle))
    if current >= deadline:
        raise PlanError(
            "cycle_deadline_exceeded: "
            f"now={current:%Y-%m-%d %H:%M:%S} "
            f"deadline={deadline:%Y-%m-%d %H:%M:%S}"
        )

    status_path = Path(status_dir or DEFAULT_STAGE_STATUS_DIR) / (
        f"live-{cycle.replace(':', '-')}.json"
    )
    try:
        stage = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError("live stage authority 不可读，拒绝 runner") from exc
    if not isinstance(stage, dict):
        raise PlanError("live stage authority 非 object，拒绝 runner")
    if (
        stage.get("stage") != "live"
        or str(stage.get("cycle_id") or "") != cycle
    ):
        raise PlanError("live stage authority identity 不匹配，拒绝 runner")
    stage_state = str(stage.get("status") or "").strip().lower()
    if stage_state != "running":
        raise PlanError(
            f"live stage status={stage_state or 'missing'}，拒绝晚到 runner"
        )
    try:
        runner_pid = int(stage.get("runner_pid"))
    except (TypeError, ValueError) as exc:
        raise PlanError("live stage runner_pid 不可校验，拒绝 runner") from exc
    if runner_pid <= 0:
        raise PlanError("live stage runner_pid 非法，拒绝 runner")

    ledger_path = Path(db_root) / "ledger.db"
    try:
        con = sqlite3.connect(
            f"file:{ledger_path.as_posix()}?mode=ro",
            uri=True,
            timeout=1,
        )
        try:
            row = con.execute(
                "SELECT cycle_id,expires_at FROM stage_profile_leases "
                "WHERE profile='live' LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise PlanError("live profile lease 不可校验，拒绝 runner") from exc
    if row is None or str(row[0] or "") != cycle:
        raise PlanError("live profile lease 已释放或不属本 cycle，拒绝 runner")
    try:
        expires_at = datetime.strptime(
            str(row[1] or ""), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=CST)
    except ValueError as exc:
        raise PlanError("live profile lease expires_at 非法，拒绝 runner") from exc
    if expires_at <= current:
        raise PlanError("live profile lease 已过期，拒绝 runner")

    analysis_ts = None
    if cycle >= ANALYSIS_DEADLINE_GUARD_FROM:
        analysis_path = Path(db_root) / "analysis.db"
        try:
            con = sqlite3.connect(
                f"file:{analysis_path.as_posix()}?mode=ro",
                uri=True,
                timeout=1,
            )
            try:
                analysis_row = con.execute(
                    "SELECT status,ts FROM analysis_runs "
                    "WHERE cycle_id=? LIMIT 1",
                    (cycle,),
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise PlanError(
                "analysis authority 不可校验，拒绝 runner") from exc
        if analysis_row is None:
            raise PlanError("analysis authority 缺失，拒绝 runner")
        analysis_status = str(analysis_row[0] or "").strip().lower()
        if analysis_status != "ok":
            raise PlanError(
                f"analysis status={analysis_status or 'missing'}，拒绝 runner"
            )
        try:
            analysis_written_at = datetime.strptime(
                str(analysis_row[1] or ""), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=CST)
        except ValueError as exc:
            raise PlanError(
                "analysis writer ts 不可校验，拒绝 runner") from exc
        analysis_deadline = cycle_start + timedelta(
            seconds=thresholds.sla_analysis_deadline_seconds(cycle))
        if analysis_written_at >= analysis_deadline:
            raise PlanError(
                "analysis_deadline_exceeded: "
                f"writer_ts={analysis_written_at:%Y-%m-%d %H:%M:%S} "
                f"deadline={analysis_deadline:%Y-%m-%d %H:%M:%S}"
            )
        analysis_ts = analysis_written_at.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "cycle_id": cycle,
        "stage_status": stage_state,
        "stage_runner_pid": runner_pid,
        "lease_expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        "analysis_status": "ok" if analysis_ts is not None else None,
        "analysis_writer_ts": analysis_ts,
        "absolute_deadline_at": deadline.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _gateway_session_key(cycle_id: str) -> str:
    safe_cycle = (
        str(cycle_id).replace("-", "").replace(":", "").replace("T", "-")
    )
    return f"agent:okx-live-trader:live-{safe_cycle}"


def _invoke_runtime_guard(runtime_guard, cycle_id: str):
    if runtime_guard is None:
        return None
    authority = runtime_guard(cycle_id)
    if not isinstance(authority, dict):
        raise PlanError("live runtime guard 未返回 authority object，拒绝执行")
    if str(authority.get("cycle_id") or "") != cycle_id:
        raise PlanError("live runtime authority cycle_id 不匹配，拒绝执行")
    try:
        stage_runner_pid = int(authority.get("stage_runner_pid"))
    except (TypeError, ValueError) as exc:
        raise PlanError("live runtime authority stage_runner_pid 缺失，拒绝执行") from exc
    if stage_runner_pid <= 0:
        raise PlanError("live runtime authority stage_runner_pid 非法，拒绝执行")
    return authority


def _load_analysis_signal(
    db_root: Path,
    cycle_id: str,
    symbol: str,
) -> dict[str, Any]:
    """Read one immutable writer-validated analysis signal in read-only mode."""
    path = Path(db_root) / "analysis.db"
    try:
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT action,side,reasoning,decision_card "
                "FROM analysis_signals WHERE cycle_id=? AND symbol=? LIMIT 1",
                (cycle_id, symbol),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise PlanError(f"analysis.db canonical signal 不可读: {exc}") from exc
    if row is None:
        raise PlanError(f"analysis_signals 缺少本轮候选: {cycle_id}/{symbol}")
    try:
        card = json.loads(row["decision_card"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise PlanError(
            f"analysis_signals decision_card 非有效 JSON: {cycle_id}/{symbol}"
        ) from exc
    if not isinstance(card, dict):
        raise PlanError(
            f"analysis_signals decision_card 顶层非 object: {cycle_id}/{symbol}"
        )
    closure_policy = thresholds.minimal_contract_closure_active(cycle_id)
    package_key = OPEN_EXECUTION_PACKAGE_KEY if closure_policy else "decision_card"
    if closure_policy:
        package = canonical_open_execution_package(card)
        package_errors = validate_open_execution_package(
            package, f"analysis_signals.{OPEN_EXECUTION_PACKAGE_KEY}",
            expected_side=str(row["side"] or "").strip().lower())
        if package_errors:
            raise PlanError(
                f"analysis_signals OPEN execution package 非法: "
                f"{cycle_id}/{symbol}: {'；'.join(package_errors)}")
    else:
        package = card
    return {
        "action": str(row["action"] or "").strip().lower(),
        "side": str(row["side"] or "").strip().lower(),
        "reasoning": str(row["reasoning"] or "").strip(),
        package_key: package,
    }


def _load_closure_open_signal_requirements(
    db_root: Path,
    cycle_id: str,
    positions: dict[tuple[str, str], dict[str, Any]],
) -> dict[tuple[str, str], str]:
    """Return every canonical OPEN signal and its required plan action."""
    path = Path(db_root) / "analysis.db"
    try:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            rows = connection.execute(
                "SELECT symbol,lower(action),lower(side) FROM analysis_signals "
                "WHERE cycle_id=? AND lower(action) IN ('open_long','open_short') "
                "ORDER BY rowid",
                (cycle_id,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PlanError("closure analysis OPEN集合不可校验") from exc
    required: dict[tuple[str, str], str] = {}
    for symbol_value, action_value, side_value in rows:
        symbol = str(symbol_value or "").strip()
        action = str(action_value or "").strip().lower()
        side = str(side_value or "").strip().lower()
        expected_side = "long" if action == "open_long" else "short"
        if not symbol or side != expected_side:
            raise PlanError(
                f"closure analysis OPEN集合非法: {symbol}/{action}/{side}")
        key = (symbol, side)
        if key in required:
            raise PlanError(f"closure analysis OPEN集合重复: {symbol}/{side}")
        required[key] = "ADD" if key in positions else "OPEN"
    return required


def _validate_closure_open_action_coverage(
    required: dict[tuple[str, str], str],
    normalized: list[dict[str, Any]],
) -> None:
    actual = {
        (str(item.get("symbol") or ""), str(item.get("side") or "")):
        str(item.get("action") or "")
        for item in normalized
        if item.get("action") in {"OPEN", "ADD"}
    }
    missing = sorted(
        f"{symbol}/{side}:{expected}"
        for (symbol, side), expected in required.items()
        if actual.get((symbol, side)) != expected
    )
    extra = sorted(
        f"{symbol}/{side}:{action}"
        for (symbol, side), action in actual.items()
        if required.get((symbol, side)) != action
    )
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra_or_wrong=" + ",".join(extra))
        raise PlanError(
            "closure OPEN signals 必须逐项进入plan并交由executor硬闸裁决: "
            + ";".join(detail))


def _canonicalize_cycle_context_for_open_actions(
    plan: dict[str, Any],
    raw_actions: object,
    *,
    cycle_id: str,
    db_root: Path,
) -> dict[str, Any]:
    """Use writer-validated evidence before validating an OPEN/ADD plan.

    The cycle-level receipt card is descriptive.  Every OPEN/ADD action is
    executed from its immutable ``analysis_signals`` card later in preflight,
    so validating an Agent-retyped/truncated copy first creates a false blocker
    after the canonical card already exists.  For a plan containing OPEN/ADD,
    rebuild that cycle-level copy from the first action's canonical card while
    preserving Agent-authored cycle ``agent_judgement``/``position_reviews``;
    all actions still undergo the existing per-symbol/side/card checks before
    any market or account I/O.
    """
    if thresholds.minimal_decision_contract_active(cycle_id):
        return plan
    if not isinstance(raw_actions, list):
        return plan
    selected: tuple[str, str, str] | None = None
    for raw in raw_actions:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip().upper()
        if action not in {"OPEN", "ADD"}:
            continue
        symbol = str(raw.get("symbol") or "").strip()
        side = str(raw.get("side") or "").strip().lower()
        if symbol and side in {"long", "short"}:
            selected = (action, symbol, side)
            break
    if selected is None:
        return plan
    action, symbol, side = selected
    signal = _load_analysis_signal(db_root, cycle_id, symbol)
    expected_signal_action = f"open_{side}"
    if signal["action"] != expected_signal_action or signal["side"] != side:
        raise PlanError(
            f"{action} 与 canonical analysis signal 不一致: "
            f"plan={symbol}/{side}, signal={signal['action']}/{signal['side']}"
        )
    context = plan.get("receipt_context")
    if not isinstance(context, dict):
        return plan
    canonical_plan = copy.deepcopy(plan)
    canonical_context = canonical_plan["receipt_context"]
    canonical_context["decision_protocol"] = "decision_card_v1"
    supplied_card = context.get("decision_card")
    cycle_card = copy.deepcopy(signal["decision_card"])
    if isinstance(supplied_card, dict):
        judgement = str(supplied_card.get("agent_judgement") or "").strip()
        if judgement:
            cycle_card["agent_judgement"] = judgement
        reviews = supplied_card.get("position_reviews")
        if isinstance(reviews, (dict, list)):
            cycle_card["position_reviews"] = copy.deepcopy(reviews)
    canonical_context["decision_card"] = cycle_card
    return canonical_plan


def _read_json_with_sha(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"{label} 不是可读 UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlanError(f"{label} 顶层必须是 object")
    return payload, hashlib.sha256(raw).hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    return _read_json_with_sha(path, label)[0]


def _same_resolved_path(left: object, right: Path) -> bool:
    try:
        return Path(str(left)).resolve() == Path(right).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _precheck_stage_owned_live_input_handoff(
    handoff_file: Path,
    *,
    cycle_id: str,
    facts_file: Path,
    decision_view_file: Path,
) -> tuple[dict[str, Any], str]:
    """Admit only the exact ready handoff written by the stage supervisor.

    This deliberately runs before plan parsing in the CLI.  It is not a
    substitute for the full facts/view/hash binding below; it prevents an
    Agent-created facts/view pair from reaching plan handling when the stage
    supervisor has not published ``ready``.
    """
    path = Path(handoff_file)
    safe_cycle = cycle_id.replace(":", "-")
    if (
        Path(facts_file).name != f"live_facts_{safe_cycle}.json"
        or Path(decision_view_file).name
        != f"position_exit_view_{safe_cycle}.json"
        or path.name != f"live_input_handoff_{safe_cycle}.json"
        or Path(facts_file).parent.resolve()
        != Path(decision_view_file).parent.resolve()
        or Path(facts_file).parent.resolve() != path.parent.resolve()
    ):
        raise PlanError(
            "stage_input_handoff_invalid: closure artifact必须是同目录具名文件")
    if not path.exists():
        raise PlanError("stage_input_handoff_missing: stage-owned handoff 不存在")
    if path.is_symlink():
        raise PlanError("stage_input_handoff_invalid: handoff 不得是 symlink")
    payload, raw_sha256 = _read_json_with_sha(
        path, "stage-owned live-input handoff")
    required_fields = {
        "schema_version", "cycle_id", "status", "detail_status",
        "facts_file", "decision_view_file", "facts_hash", "facts_status",
        "decision_view_hash", "position_count",
        "production_database_writes", "orders_placed",
    }
    problems: list[str] = []
    if set(payload) != required_fields:
        problems.append("fields")
    if payload.get("schema_version") != STAGE_INPUT_HANDOFF_SCHEMA_VERSION:
        problems.append("schema_version")
    if payload.get("status") != "ready":
        problems.append(f"status={payload.get('status') or '<missing>'}")
    if payload.get("detail_status") != "ready":
        problems.append("detail_status")
    if str(payload.get("cycle_id") or "") != cycle_id:
        problems.append("cycle_id")
    if not _same_resolved_path(payload.get("facts_file"), facts_file):
        problems.append("facts_file")
    if not _same_resolved_path(
            payload.get("decision_view_file"), decision_view_file):
        problems.append("decision_view_file")
    if payload.get("production_database_writes") != 0:
        problems.append("production_database_writes")
    if payload.get("orders_placed") != 0:
        problems.append("orders_placed")
    if problems:
        raise PlanError(
            "stage_input_handoff_not_ready: "
            + ",".join(dict.fromkeys(problems)))
    return payload, raw_sha256


def _validate_stage_owned_live_input_binding(
    plan: dict[str, Any],
    facts: dict[str, Any],
    *,
    cycle_id: str,
    plan_sha256: str,
    facts_file: Path,
    decision_view_file: Path,
    handoff_file: Path,
) -> dict[str, Any]:
    """Bind stage handoff, immutable facts, view and plan before live I/O."""
    handoff, handoff_sha256 = _precheck_stage_owned_live_input_handoff(
        handoff_file,
        cycle_id=cycle_id,
        facts_file=facts_file,
        decision_view_file=decision_view_file,
    )
    problems: list[str] = []
    if Path(facts_file).is_symlink() or Path(decision_view_file).is_symlink():
        problems.append("artifact_symlink")
    try:
        file_facts = _read_json(facts_file, "stage-owned live facts")
        view = _read_json(decision_view_file, "stage-owned decision view")
    except PlanError:
        raise
    if file_facts != facts:
        problems.append("facts_object_vs_file")
    fact_errors = validate_facts(
        file_facts,
        expected_cycle=cycle_id,
        expected_profile="live",
        require_ok=False,
        max_age_s=30 * 60,
    )
    if fact_errors:
        problems.append("facts_contract")
    facts_hash = str(file_facts.get("facts_hash") or "").strip().lower()
    canonical_facts = dict(file_facts)
    canonical_facts.pop("facts_hash", None)
    if (
        not facts_hash
        or _canonical_hash(canonical_facts) != facts_hash
        or facts_hash != str(handoff.get("facts_hash") or "")
    ):
        problems.append("facts_hash")
    positions = file_facts.get("positions")
    expected_count = len(positions) if isinstance(positions, list) else None
    declared_count = handoff.get("position_count")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != expected_count
    ):
        problems.append("position_count")
    if handoff.get("facts_status") != file_facts.get("status"):
        problems.append("facts_status")
    view_hash = str(view.get("view_hash") or "").strip().lower()
    canonical_view = dict(view)
    canonical_view.pop("view_hash", None)
    if (
        view.get("schema") != "position_exit_decision_view_v2_no_timeframes"
        or view.get("cycle_id") != cycle_id
        or view.get("facts_hash") != facts_hash
        or view.get("source_status") != "PASSED"
        or view.get("timeframe_judgment_used") is not False
        or view.get("position_count") != expected_count
        or not str(view.get("source_evidence_hash") or "").strip()
    ):
        problems.append("decision_view_contract")
    if (
        len(view_hash) != 64
        or any(char not in "0123456789abcdef" for char in view_hash)
        or _canonical_hash(canonical_view) != view_hash
        or view_hash != str(handoff.get("decision_view_hash") or "")
    ):
        problems.append("decision_view_hash")
    if plan.get("cycle_id") != cycle_id:
        problems.append("plan.cycle_id")
    receipt_context = plan.get("receipt_context")
    if (
        not isinstance(receipt_context, dict)
        or receipt_context.get("cycle_id") != cycle_id
    ):
        problems.append("plan.receipt_context.cycle_id")
    normalized_plan_sha = str(plan_sha256 or "").strip().lower()
    if (
        len(normalized_plan_sha) != 64
        or any(char not in "0123456789abcdef" for char in normalized_plan_sha)
    ):
        problems.append("plan_sha256")
    if problems:
        raise PlanError(
            "stage_input_binding_invalid: "
            + ",".join(dict.fromkeys(problems)))
    return {
        "schema_version": 1,
        "source": "stage_runner_deterministic_live_inputs",
        "cycle_id": cycle_id,
        "handoff_sha256": handoff_sha256,
        "facts_hash": facts_hash,
        "decision_view_hash": view_hash,
        "position_count": expected_count,
        "plan_sha256": normalized_plan_sha,
    }


def _validate_position_exit_evidence(
    facts: dict[str, Any],
    *,
    cycle_id: str,
    evidence_file: Path | None,
) -> None:
    """Bind every live position plan to the exact read-only exit review.

    The Agent-authored plan is not sufficient evidence for existing positions:
    ``multitimeframe_decision_evidence.py --facts-file`` must first produce one
    exact-cycle artifact tied to the immutable ``facts_hash``.  This check runs
    inside runner preflight, before any executor or business-database write.
    Library callers may omit ``evidence_file`` for isolated unit work; the
    production CLI always supplies the canonical path.
    """
    if evidence_file is None:
        return
    positions = facts.get("positions")
    if not isinstance(positions, list):
        raise PlanError("position_exit_evidence_invalid: live_facts.positions 必须是 list")
    if not positions:
        return
    path = Path(evidence_file)
    if not path.exists():
        raise PlanError(
            "position_exit_evidence_missing: 当前有持仓但逐仓退出证据文件不存在"
        )
    payload = _read_json(path, "position-exit-evidence")
    problems: list[str] = []
    if payload.get("cycle_id") != cycle_id:
        problems.append("cycle_id")
    facts_hash = str(facts.get("facts_hash") or "").strip()
    if not facts_hash or str(payload.get("facts_hash") or "") != facts_hash:
        problems.append("facts_hash")
    if payload.get("ok") is not True or payload.get("status") != "PASSED":
        problems.append("status")
    if payload.get("production_database_writes") != 0:
        problems.append("production_database_writes")
    if payload.get("orders_placed") != 0:
        problems.append("orders_placed")

    expected: list[tuple[str, str]] = []
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            problems.append(f"facts.positions[{index}]")
            continue
        symbol = str(position.get("instId") or "").strip()
        side = str(position.get("posSide") or "").strip().lower()
        if not symbol or side not in {"long", "short"}:
            problems.append(f"facts.positions[{index}].identity")
            continue
        expected.append((symbol, side))
    evidence_positions = payload.get("positions")
    observed: list[tuple[str, str]] = []
    if not isinstance(evidence_positions, list):
        problems.append("positions")
    else:
        for index, position in enumerate(evidence_positions):
            if not isinstance(position, dict):
                problems.append(f"positions[{index}]")
                continue
            symbol = str(position.get("symbol") or "").strip()
            side = str(position.get("side") or "").strip().lower()
            if not symbol or side not in {"long", "short"}:
                problems.append(f"positions[{index}].identity")
                continue
            observed.append((symbol, side))
    try:
        declared_count = int(payload.get("position_count"))
    except (TypeError, ValueError):
        declared_count = -1
    if declared_count != len(expected):
        problems.append("position_count")
    if len(set(expected)) != len(expected) or sorted(observed) != sorted(expected):
        problems.append("position_keys")
    evidence_hash = str(payload.get("evidence_hash") or "").strip().lower()
    if (
        len(evidence_hash) != 64
        or any(char not in "0123456789abcdef" for char in evidence_hash)
    ):
        problems.append("evidence_hash")
    else:
        canonical_payload = dict(payload)
        canonical_payload.pop("evidence_hash", None)
        if _canonical_hash(canonical_payload) != evidence_hash:
            problems.append("evidence_hash_mismatch")
    if problems:
        raise PlanError(
            "position_exit_evidence_invalid: " + ",".join(dict.fromkeys(problems))
        )


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    serialized = json.dumps(
        payload, ensure_ascii=False, indent=2, allow_nan=False)
    tmp.write_text(serialized, encoding="utf-8", newline="\n")

    def target_already_matches() -> bool:
        try:
            observed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return observed == payload

    try:
        for attempt, delay in enumerate(
            (*ATOMIC_REPLACE_RETRY_DELAYS_SECONDS, None), start=1
        ):
            try:
                os.replace(tmp, path)
                # CAS-style postcondition: success means the exact payload is
                # now visible, never merely that os.replace returned.
                if not target_already_matches():
                    raise OSError(
                        "atomic JSON replace postcondition mismatch")
                return
            except PermissionError as exc:
                # Windows can transiently deny replacement while an observer
                # has the destination open.  If the identical payload already
                # won the race, this call is complete; otherwise retry only the
                # filesystem commit.  No business/exchange action is retried.
                if target_already_matches():
                    return
                if getattr(exc, "winerror", None) not in {None, 5, 32}:
                    raise
                if delay is None:
                    raise PermissionError(
                        f"atomic JSON replace failed after {attempt} attempts: {path}"
                    ) from exc
                time.sleep(delay)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _default_runner_state_file(cycle_id: str) -> Path:
    cycle = _validated_cycle_id(cycle_id)
    path = ROOT / "tmp" / f"live_runner_state_{cycle.replace(':', '-')}.json"
    return _require_direct_tmp_path(path, "runner-state")


def _default_live_input_handoff_file(facts_file: Path, cycle_id: str) -> Path:
    safe_cycle = _validated_cycle_id(cycle_id).replace(":", "-")
    return Path(facts_file).with_name(
        f"live_input_handoff_{safe_cycle}.json")


def _default_decision_view_file(facts_file: Path, cycle_id: str) -> Path:
    safe_cycle = _validated_cycle_id(cycle_id).replace(":", "-")
    return Path(facts_file).with_name(
        f"position_exit_view_{safe_cycle}.json")


def _default_runner_lock_file(state_file: Path, cycle_id: str) -> Path:
    # Profile-wide serialization also prevents adjacent cycles from issuing
    # overlapping CLOSE/OPEN actions against the same live account.
    return Path(state_file).with_name("live_runner.lock")


def _default_handoff_state_file(state_file: Path, cycle_id: str) -> Path:
    safe_cycle = _validated_cycle_id(cycle_id).replace(":", "-")
    return Path(state_file).with_name(f"live_runner_handoff_{safe_cycle}.json")


def _default_handoff_lock_file(state_file: Path, cycle_id: str) -> Path:
    safe_cycle = _validated_cycle_id(cycle_id).replace(":", "-")
    return Path(state_file).with_name(f"live_runner_handoff_{safe_cycle}.lock")


@contextmanager
def _runner_cycle_lock(path: Path, cycle_id: str):
    """Hold one kernel-released, non-blocking lock for the whole runner call."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size < 1:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - production runtime is Windows
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise PlanError(
                f"同 cycle runner 已持有进程锁，拒绝并发执行: {cycle_id}"
            ) from exc

        owner = {
            "cycle_id": cycle_id,
            "pid": os.getpid(),
            "process_started_ns": time.time_ns(),
            "owner_nonce": secrets.token_hex(16),
        }
        handle.seek(1)
        handle.truncate()
        handle.write(json.dumps(owner, sort_keys=True).encode("utf-8"))
        handle.flush()
        yield owner
    finally:
        if locked:
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
        else:
            handle.close()


def _reject_existing_runner_state(
    path: Path,
    cycle_id: str,
    *,
    facts_hash: str | None = None,
    plan_sha256: str | None = None,
) -> int:
    """Admit at most one preflight rewrite; everything else stays sticky.

    Returns the number of preflight attempts already consumed for this cycle
    (0 when no marker exists).  ``failed_preflight`` is the only re-admittable
    state: it is written strictly before any exchange or business-database
    side effect, so one full-file plan rewrite cannot double-execute anything.
    The retry must keep the same facts identity — the contract is "same facts,
    rewritten plan", never "regenerate the cycle".
    """
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"runner state 已存在但不可校验: {path}") from exc
    if not isinstance(payload, dict) or payload.get("cycle_id") != cycle_id:
        raise PlanError("runner state identity 不匹配，拒绝覆盖")
    if payload.get("schema_version") != RUNNER_STATE_SCHEMA_VERSION:
        raise PlanError("runner state legacy/invalid schema，拒绝覆盖")
    state = str(payload.get("state") or "").strip().lower()
    if state == "failed_preflight":
        try:
            attempts = int(payload.get("preflight_attempts") or 1)
        except (TypeError, ValueError):
            attempts = PREFLIGHT_MAX_ATTEMPTS
        if attempts >= PREFLIGHT_MAX_ATTEMPTS:
            raise PlanError(
                "本 cycle 预检重写额度已用完"
                f"（preflight_attempts={attempts}），拒绝重复执行")
        if (
            facts_hash is not None
            and str(payload.get("facts_hash") or "") != facts_hash
        ):
            raise PlanError(
                "预检重写必须沿用同一 live_facts（facts_hash 已变化），拒绝执行")
        if (
            plan_sha256 is not None
            and str(payload.get("plan_sha256") or "") == plan_sha256
        ):
            prior_error = str(payload.get("error") or "").strip()
            detail = f"；首次错误={prior_error}" if prior_error else ""
            raise PlanError(
                "同一 plan 已预检失败，必须先整文件重写后才能使用唯一重试"
                f"{detail}"
            )
        return attempts
    if state in {"started", "executing", "committed", "failed"}:
        raise PlanError(f"本 cycle runner state={state}，拒绝重复执行")
    raise PlanError(f"runner state={state or 'missing'} 非法，拒绝覆盖")


def _reject_revoked_handoff(
    path: Path,
    cycle_id: str,
    *,
    session_key: str,
    stage_runner_pid: int | None,
    facts_hash: str,
    plan_sha256: str,
) -> None:
    """Fail closed when the stage supervisor won the handoff CAS."""
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError("runner handoff gate 不可校验，拒绝执行") from exc
    if not isinstance(payload, dict):
        raise PlanError("runner handoff gate 非 object，拒绝执行")
    errors: list[str] = []
    if payload.get("schema_version") != HANDOFF_GATE_SCHEMA_VERSION:
        errors.append("schema_version")
    if payload.get("state") != "revoked":
        errors.append("state")
    if str(payload.get("cycle_id") or "") != cycle_id:
        errors.append("cycle_id")
    if str(payload.get("session_key") or "") != session_key:
        errors.append("session_key")
    try:
        gate_stage_runner_pid = int(payload.get("stage_runner_pid"))
    except (TypeError, ValueError):
        gate_stage_runner_pid = None
    if gate_stage_runner_pid != stage_runner_pid:
        errors.append("stage_runner_pid")
    if str(payload.get("facts_hash") or "") != facts_hash:
        errors.append("facts_hash")
    if str(payload.get("plan_sha256") or "") != plan_sha256:
        errors.append("plan_sha256")
    if errors:
        raise PlanError(
            "runner handoff gate identity 不匹配，拒绝执行: "
            + ",".join(errors)
        )
    raise PlanError(
        "runner handoff authority 已由 supervisor 原子撤销，拒绝晚到 runner: "
        f"{payload.get('reason') or 'unspecified'}"
    )


def _authority_stage_runner_pid(authority: dict[str, Any] | None) -> int | None:
    if authority is None:
        return None
    # _invoke_runtime_guard has already made this a positive integer contract.
    return int(authority["stage_runner_pid"])


def _require_same_stage_authority(
    runtime_guard,
    cycle_id: str,
    *,
    expected_stage_runner_pid: int | None,
) -> dict[str, Any] | None:
    authority = _invoke_runtime_guard(runtime_guard, cycle_id)
    current_pid = _authority_stage_runner_pid(authority)
    if current_pid != expected_stage_runner_pid:
        raise PlanError(
            "live runtime authority stage_runner_pid 已变化，拒绝执行"
        )
    return authority


def _write_runner_state(
    path: Path,
    *,
    cycle_id: str,
    state: str,
    facts_hash: str,
    plan_sha256: str,
    error: str | None = None,
    session_key: str | None = None,
    stage_runner_pid: int | None = None,
    preflight_attempts: int | None = None,
) -> None:
    if state not in {
        "started", "executing", "committed", "failed", "failed_preflight",
    }:
        raise ValueError(f"invalid runner state: {state}")
    payload: dict[str, Any] = {
        "schema_version": RUNNER_STATE_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "state": state,
        # Copy the immutable facts artifact's own digest.  Do not hash the
        # envelope containing facts_hash again: stage supervision compares
        # this value directly with live_facts_<cycle>.json.
        "facts_hash": facts_hash,
        "plan_sha256": plan_sha256,
        "session_key": str(session_key) if session_key is not None else None,
        "stage_runner_pid": (
            int(stage_runner_pid) if stage_runner_pid is not None else None
        ),
    }
    if preflight_attempts is not None:
        payload["preflight_attempts"] = int(preflight_attempts)
    if error:
        payload["error"] = error
    _atomic_write_json(path, payload)


def _audit_action(action: dict[str, Any]) -> dict[str, Any]:
    """Remove runner-only canonical artifacts from persisted request audit."""
    return copy.deepcopy({
        key: value for key, value in action.items() if not key.startswith("_")
    })


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise PlanError(f"{label} 必须是有限正数")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{label} 必须是有限正数") from exc
    if not math.isfinite(number) or number <= 0:
        raise PlanError(f"{label} 必须是有限正数")
    return number


def _position_index(facts: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for item in facts.get("positions") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("instId") or "").strip()
        side = str(item.get("posSide") or "").strip().lower()
        if symbol and side in {"long", "short"}:
            rows[(symbol, side)] = item
    return rows


def _normalize_context(
    plan: dict[str, Any],
    facts: dict[str, Any],
    cycle_id: str,
) -> dict[str, Any]:
    raw = plan.get("receipt_context")
    if not isinstance(raw, dict):
        raise PlanError("plan.receipt_context 必须是完整 object")
    context = copy.deepcopy(raw)
    forbidden = sorted(TERMINAL_CONTEXT_KEYS & set(context))
    if forbidden:
        # These values are runner-owned outcomes, never Agent authority.  Drop
        # them before validation instead of letting harmless duplicated output
        # fields turn an otherwise safe HOLD/position plan into a missing
        # business terminal.  The original plan bytes remain bound by
        # plan_sha256/position_action_plan_hash, so the normalization is still
        # independently auditable and cannot conceal the supplied input.
        for key in forbidden:
            context.pop(key, None)
        print(
            "[live_position_action_runner][WARN] ignored untrusted "
            "receipt_context terminal fields: " + ",".join(forbidden),
            flush=True,
        )
    if context.get("cycle_id") != cycle_id:
        raise PlanError("receipt_context.cycle_id 与 plan/cmd cycle 不一致")
    claimed = str(context.get("mode") or context.get("profile") or "live").lower()
    if claimed != "live":
        raise PlanError("receipt_context mode/profile 必须是 live")
    context["mode"] = "live"
    context["profile"] = "live"
    context["status"] = "ok"

    canonical_equity = (facts.get("balance") or {}).get("totalEq")
    supplied_equity = context.get("equity")
    if canonical_equity is not None and supplied_equity is not None:
        try:
            canonical_equity_number = float(canonical_equity)
            supplied_equity_number = float(supplied_equity)
        except (TypeError, ValueError) as exc:
            raise PlanError("receipt_context.equity 必须是有效数字") from exc
        tolerance = max(1e-6, abs(canonical_equity_number) * 1e-8)
        if abs(supplied_equity_number - canonical_equity_number) > tolerance:
            raise PlanError("receipt_context.equity 与 live_facts 不一致")
    context["equity"] = canonical_equity
    if not str(context.get("regime") or "").strip():
        raise PlanError("receipt_context.regime 不得为空")

    context_errors = oe.validate_receipt_context(
        context,
        cycle_id=cycle_id,
        required=True,
    )
    if context_errors:
        raise PlanError("receipt_context 预检失败: " + "；".join(context_errors))
    return context


def _record_position_action(
    seen: dict[tuple[str, str], set[str]],
    key: tuple[str, str],
    action: str,
) -> None:
    prior_actions = seen.get(key, set())
    if prior_actions:
        combined = prior_actions | {action}
        # A protection repair and an ADD are distinct operations: the former
        # reduces risk on the existing position, while the latter still has to
        # face the live hard gates.  No other same-position combination passes.
        if combined != {"ADJUST_PROTECTION", "ADD"}:
            raise PlanError(
                "同一仓位每轮只能一个最终动作，唯一例外为"
                f"ADJUST_PROTECTION+ADD: {key[0]}/{key[1]}")
    seen.setdefault(key, set()).add(action)


def preflight_plan(
    plan: dict[str, Any],
    facts: dict[str, Any],
    *,
    cycle_id: str,
    db_root: Path = ROOT / "db",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if plan.get("cycle_id") != cycle_id:
        raise PlanError("plan.cycle_id 与命令 cycle-id 不一致")
    fact_errors = validate_facts(
        facts,
        expected_cycle=cycle_id,
        expected_profile="live",
        require_ok=False,
        max_age_s=30 * 60,
    )
    if fact_errors:
        raise PlanError("live_facts 校验失败: " + "；".join(fact_errors))

    raw_actions = plan.get("actions")
    context_plan = _canonicalize_cycle_context_for_open_actions(
        plan,
        raw_actions,
        cycle_id=cycle_id,
        db_root=Path(db_root),
    )
    context = _normalize_context(context_plan, facts, cycle_id)
    if not isinstance(raw_actions, list):
        raise PlanError("plan.actions 必须是 list；HOLD 使用空 list")

    policy = facts.get("action_policy") or {}
    allowed = {
        str(item).strip().lower()
        for item in (policy.get("allowed_executor_actions") or [])
    }
    positions_verified = policy.get("position_truth_verified") is True
    positions = _position_index(facts)
    normalized: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], set[str]] = {}

    for index, raw in enumerate(raw_actions):
        if not isinstance(raw, dict):
            raise PlanError(f"actions[{index}] 必须是 object")
        action = str(raw.get("action") or "").strip().upper()
        if action not in ALLOWED_ACTIONS:
            raise PlanError(
                f"actions[{index}].action 仅支持 "
                "OPEN|ADD|CLOSE|REDUCE|ADJUST_PROTECTION"
            )
        unknown = sorted(set(raw) - ACTION_KEYS[action])
        if unknown:
            raise PlanError(
                f"actions[{index}] 未知字段: " + ",".join(unknown)
            )
        if not positions_verified or action.lower() not in allowed:
            raise PlanError(
                f"actions[{index}] {action} 未获 live_facts.action_policy 授权"
            )
        symbol = str(raw.get("symbol") or "").strip()
        if not symbol:
            raise PlanError(f"actions[{index}].symbol 不得为空")
        side_field = "side" if action in {"OPEN", "ADD"} else "pos_side"
        side = str(raw.get(side_field) or "").strip().lower()
        if side not in {"long", "short"}:
            raise PlanError(f"actions[{index}].{side_field} 必须是 long|short")
        key = (symbol, side)
        if action == "OPEN" and key in positions:
            raise PlanError(
                f"actions[{index}] OPEN 与 facts 现仓冲突；已有仓须显式使用 ADD: "
                f"{symbol}/{side}"
            )
        if action == "ADD" and key not in positions:
            raise PlanError(
                f"actions[{index}] ADD 目标不在本轮 facts 现仓: {symbol}/{side}"
            )
        if action not in {"OPEN", "ADD"} and key not in positions:
            raise PlanError(
                f"actions[{index}] 目标不在本轮 facts 现仓: {symbol}/{side}"
            )
        _record_position_action(seen, key, action)
        if action in {"OPEN", "ADD"}:
            if facts.get("status") != "ok":
                raise PlanError(
                    f"actions[{index}] live_facts.status!=ok，禁止 OPEN/ADD"
                )
            target_pct = _positive_number(
                raw.get("target_stop_risk_pct_equity"),
                f"actions[{index}].target_stop_risk_pct_equity",
            )
            if target_pct > rv.MAX_SINGLE_ORDER_RISK_PCT_EQUITY:
                raise PlanError(
                    f"actions[{index}].target_stop_risk_pct_equity={target_pct:g} "
                    f"超过硬上限 {rv.MAX_SINGLE_ORDER_RISK_PCT_EQUITY:g}"
                )
            lev = _positive_number(raw.get("lev"), f"actions[{index}].lev")
            if lev > rv.MAX_LEVERAGE:
                raise PlanError(
                    f"actions[{index}].lev={lev:g} 超过硬上限 {rv.MAX_LEVERAGE:g}"
                )
            signal = _load_analysis_signal(Path(db_root), cycle_id, symbol)
            expected_signal_action = f"open_{side}"
            if signal["action"] != expected_signal_action or signal["side"] != side:
                raise PlanError(
                    f"actions[{index}] 与 canonical analysis signal 不一致: "
                    f"plan={symbol}/{side}, signal={signal['action']}/{signal['side']}"
                )
            closure_policy = thresholds.minimal_contract_closure_active(
                cycle_id)
            package = signal.get(
                OPEN_EXECUTION_PACKAGE_KEY
                if closure_policy else "decision_card")
            if not isinstance(package, dict):
                raise PlanError(
                    f"actions[{index}] canonical OPEN execution package 缺失"
                )
            risk_reward = package if closure_policy else package.get("risk_reward")
            if not isinstance(risk_reward, dict):
                raise PlanError(
                    f"actions[{index}] canonical execution risk fields 缺失")
            sl_trigger_px = _positive_number(
                risk_reward.get("stop"),
                f"actions[{index}].canonical risk_reward.stop",
            )
            exit_mode = str(risk_reward.get("exit_mode") or "").strip().lower()
            tp_trigger_px = None
            if exit_mode == "fixed_tp":
                tp_trigger_px = _positive_number(
                    risk_reward.get("target"),
                    f"actions[{index}].canonical risk_reward.target",
                )
            reasoning = (
                signal["reasoning"]
                or (
                    str(package.get("agent_judgement") or "").strip()
                    if not closure_policy else ""
                )
            )
            if not reasoning:
                raise PlanError(
                    f"actions[{index}] canonical analysis reasoning/judgement 为空"
                )
            if closure_policy:
                _reject_closure_retired_reason(
                    reasoning,
                    f"actions[{index}].canonical_analysis_reasoning")
            item = {
                "action": action,
                "symbol": symbol,
                "side": side,
                "target_stop_risk_pct_equity": target_pct,
                "lev": lev,
                "reasoning": reasoning,
                "_sl_trigger_px": sl_trigger_px,
                "_tp_trigger_px": tp_trigger_px,
                "_expected_pre_position_sz": (
                    _positive_number(
                        positions[key].get("contracts"),
                        f"facts.positions[{symbol}/{side}].contracts",
                    )
                    if action == "ADD" else 0.0
                ),
                "_expected_pre_position_pos_id": (
                    positions[key].get("posId") if action == "ADD" else None
                ),
                "_expected_pre_position_c_time": (
                    positions[key].get("cTime") if action == "ADD" else None
                ),
            }
            item[
                "_open_execution_package"
                if closure_policy else "_decision_card"
            ] = copy.deepcopy(package)
            action_context = _action_context(context, item)
            action_context_errors = oe.validate_receipt_context(
                action_context,
                cycle_id=cycle_id,
                required=True,
                expected_symbol=symbol,
                expected_side=side,
                expected_regime=str(context.get("regime") or "") or None,
                require_experience=(
                    not thresholds.decision_restriction_removal_active(cycle_id)),
            )
            if action_context_errors:
                raise PlanError(
                    f"actions[{index}] canonical receipt_context 预检失败: "
                    + "；".join(action_context_errors)
                )
        else:
            item = {
                "action": action,
                "symbol": symbol,
                "pos_side": side,
                "reasoning": str(raw.get("reasoning") or "").strip(),
                "_expected_pre_position_sz": _positive_number(
                    positions[key].get("contracts"),
                    f"facts.positions[{symbol}/{side}].contracts",
                ),
                "_expected_pre_position_pos_id": positions[key].get("posId"),
                "_expected_pre_position_c_time": positions[key].get("cTime"),
            }
            if not item["reasoning"]:
                raise PlanError(f"actions[{index}].reasoning 不得为空")

        if action == "REDUCE":
            reduce_sz = _positive_number(
                raw.get("reduce_sz"), f"actions[{index}].reduce_sz"
            )
            contracts = _positive_number(
                positions[key].get("contracts"),
                f"facts.positions[{symbol}/{side}].contracts",
            )
            if reduce_sz >= contracts:
                raise PlanError(
                    f"actions[{index}].reduce_sz 必须严格小于现仓张数 {contracts:g}；"
                    "全平请使用 CLOSE"
                )
            item["reduce_sz"] = reduce_sz
        elif action == "ADJUST_PROTECTION":
            resize = raw.get("resize_to_full_position", False)
            consolidate = raw.get("consolidate_extra_sl", False)
            if not isinstance(resize, bool) or not isinstance(consolidate, bool):
                raise PlanError(
                    f"actions[{index}] resize/consolidate 必须是 bool"
                )
            item["resize_to_full_position"] = resize
            item["consolidate_extra_sl"] = consolidate
            for field in ("new_sl_trigger_px", "new_tp_trigger_px"):
                value = raw.get(field)
                item[field] = (
                    None if value is None
                    else _positive_number(value, f"actions[{index}].{field}")
                )
            if (
                item["new_sl_trigger_px"] is None
                and item["new_tp_trigger_px"] is None
                and not resize
            ):
                raise PlanError(
                    f"actions[{index}] 未声明 SL/TP/全仓数量调整"
                )
        normalized.append(item)
    if (
        thresholds.minimal_contract_closure_active(cycle_id)
        and facts.get("status") == "ok"
        and policy.get("open_add_allowed_by_facts") is True
    ):
        required = _load_closure_open_signal_requirements(
            Path(db_root), cycle_id, positions)
        _validate_closure_open_action_coverage(required, normalized)
    # Validate policy prose before any action can fill. A price such as
    # 0.06667 is data, not an assertion that the portfolio IMR cap is 0.0666.
    policy_errors = tw.human_policy_errors(context)
    for action in normalized:
        candidate = _action_context(context, action)
        candidate["reasoning"] = str(context.get("reasoning") or "") + "；" + str(action.get("reasoning") or "")
        policy_errors.extend(tw.human_policy_errors(candidate))
    if policy_errors:
        raise PlanError("；".join(dict.fromkeys(policy_errors)))
    return context, normalized


def _action_context(
    context: dict[str, Any], action: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(context)
    action_name = action["action"]
    if action_name in {"OPEN", "ADD"}:
        if thresholds.minimal_contract_closure_active(
                str(context.get("cycle_id") or "")):
            result.pop("decision_card", None)
            result[OPEN_EXECUTION_PACKAGE_KEY] = copy.deepcopy(
                action["_open_execution_package"])
        else:
            result["decision_card"] = copy.deepcopy(
                action["_decision_card"])
    result.update({
        "decision": "hold" if action_name == "ADJUST_PROTECTION" else "traded",
        "n_orders": 0 if action_name == "ADJUST_PROTECTION" else 1,
        "errors": [],
    })
    return result


def _ensure_actor_attestation(
    context: dict[str, Any],
    action: dict[str, Any],
    cycle_id: str,
    db_root: Path,
) -> None:
    """Attach the deterministic takeover proof once when OPEN/ADD needs it.

    The Agent still owns the decision.  The runner only invokes the existing
    deterministic revalidator; executor independently rebuilds and verifies
    the same actor chain, facts and evidence before any account/order I/O.
    """
    if (
        action.get("action") not in {"OPEN", "ADD"}
        or isinstance(context.get("actor_attestation"), dict)
    ):
        return
    try:
        attestation = actor_att.build_attestation(
            cycle_id, db_root=db_root, stage="live")
    except Exception:  # executor retains fail-closed timeline enforcement
        return
    timeline = attestation.get("timeline")
    if isinstance(timeline, dict) and timeline.get("handoff_detected") is True:
        context["actor_attestation"] = attestation


def _call_executor(
    action: dict[str, Any],
    *,
    context: dict[str, Any],
    facts: dict[str, Any],
    cycle_id: str,
    db_root: Path,
) -> dict[str, Any]:
    _ensure_actor_attestation(context, action, cycle_id, db_root)
    action_context = _action_context(context, action)
    common = {
        "reasoning": action["reasoning"],
        "db_root": db_root,
        "cycle_id": cycle_id,
        "receipt_context": action_context,
    }
    position_fingerprint = {
        "expected_pre_position_exists": action["action"] != "OPEN",
        "expected_pre_position_sz": action.get("_expected_pre_position_sz"),
        "expected_pre_position_pos_id": action.get(
            "_expected_pre_position_pos_id"
        ),
        "expected_pre_position_c_time": action.get(
            "_expected_pre_position_c_time"
        ),
    }
    if action["action"] in {"OPEN", "ADD"}:
        specs = oe.fetch_instrument_specs(action["symbol"], "live", db_root)
        mark_read_started = time.monotonic()
        mark_px = oe.ox.get_mark_price(action["symbol"], "live")
        sizing_mark_evidence = oe.ox.get_mark_price_evidence(
            action["symbol"], "live", since_monotonic=mark_read_started)
        if mark_px is None:
            return {
                **action_context, "profile": "live", "cycle_id": cycle_id,
                "ok": False, "action_taken": "REJECT", "symbol": action["symbol"],
                "side": action["side"], "trades": [], "p0": False,
                "reject_reason": "mark_px_fetch_failed",
                "reject_detail": oe.ox.mark_price_failure_detail(sizing_mark_evidence),
                "mark_price_evidence": sizing_mark_evidence,
            }
        balance = facts.get("balance") or {}
        sizing_sl = action["_sl_trigger_px"]
        if oe.protection_price_grid_enabled(cycle_id):
            try:
                price_grid = oe.aligned_protection_prices(
                    action["symbol"], action["side"], action["_sl_trigger_px"],
                    action["_tp_trigger_px"], "live")
                sizing_sl = price_grid["sl"]
                direction_errors = oe.pretrade_price_grid_errors(
                    cycle_id, action["side"], mark_px, sizing_sl, price_grid["tp"])
                if direction_errors:
                    raise ValueError("; ".join(direction_errors))
            except Exception as exc:
                return {**action_context, "profile": "live", "cycle_id": cycle_id,
                        "ok": False, "action_taken": "REJECT", "symbol": action["symbol"],
                        "side": action["side"], "trades": [], "p0": False,
                        "reject_reason": "protection_price_grid_invalid",
                        "reject_detail": f"{type(exc).__name__}: {exc}"}
        sizing = rv.size_for_target_stop_risk(
            mark_px=mark_px,
            ct_val=specs.get("ct_val"),
            lot_sz=specs.get("lot_sz"),
            min_order_size=specs.get("min_sz"),
            equity=balance.get("totalEq"),
            sl_trigger_px=sizing_sl,
            target_risk_pct_equity=action["target_stop_risk_pct_equity"],
        )
        if sizing.get("ok") is not True:
            return {
                **action_context,
                "profile": "live",
                "cycle_id": cycle_id,
                "ok": False,
                "action_taken": "REJECT",
                "symbol": action["symbol"],
                "side": action["side"],
                "trades": [],
                "p0": False,
                "reject_reason": "deterministic_sizing_failed",
                "reject_detail": str(
                    sizing.get("error") or "unknown_sizing_error"
                ),
                "sizing_intent": sizing,
            }

        executor_positions: list[dict[str, Any]] = []
        for row in facts.get("positions") or []:
            if not isinstance(row, dict):
                continue
            normalized = copy.deepcopy(row)
            normalized.setdefault("symbol", row.get("instId"))
            normalized.setdefault("side", row.get("posSide"))
            normalized.setdefault("notional", row.get("mark_notional_usdt"))
            executor_positions.append(normalized)

        result = oe.open_position(
            action["symbol"],
            action["side"],
            sizing["intended_sz"],
            action["lev"],
            action["_sl_trigger_px"],
            profile="live",
            mgn_mode="cross",
            mark_px=mark_px,
            equity=balance.get("totalEq"),
            available_margin=balance.get("availEq"),
            account_imr=balance.get("account_imr"),
            open_positions=executor_positions,
            tp_trigger_px=action["_tp_trigger_px"],
            expected_pre_position_exists=action["action"] == "ADD",
            expected_pre_position_sz=action["_expected_pre_position_sz"],
            expected_pre_position_pos_id=action[
                "_expected_pre_position_pos_id"
            ],
            expected_pre_position_c_time=action[
                "_expected_pre_position_c_time"
            ],
            target_stop_risk_pct_equity=action[
                "target_stop_risk_pct_equity"
            ],
            **common,
        )
        if isinstance(result, dict):
            result["sizing_intent"] = copy.deepcopy(sizing)
            if sizing_mark_evidence is not None:
                result["sizing_mark_price_evidence"] = sizing_mark_evidence
            for trade in result.get("trades") or []:
                if isinstance(trade, dict):
                    # Bind each fill to the exact canonical machine package.
                    # The legacy DB analysis column remains readable, while
                    # closure-cycle business receipts no longer overload the
                    # retired ``decision_card`` name.
                    if thresholds.minimal_contract_closure_active(cycle_id):
                        trade.pop("decision_card", None)
                        trade[OPEN_EXECUTION_PACKAGE_KEY] = copy.deepcopy(
                            action["_open_execution_package"])
                    else:
                        trade["decision_card"] = copy.deepcopy(
                            action["_decision_card"])
                    trade["decision_protocol"] = (
                        "minimal_decision_v2"
                        if thresholds.minimal_decision_contract_active(cycle_id)
                        else "decision_card_v1")
        return result
    if action["action"] == "CLOSE":
        return oe.close_position(
            action["symbol"],
            "live",
            pos_side=action["pos_side"],
            **position_fingerprint,
            **common,
        )
    if action["action"] == "REDUCE":
        return oe.reduce_position(
            action["symbol"],
            "live",
            action["reduce_sz"],
            pos_side=action["pos_side"],
            **position_fingerprint,
            **common,
        )
    return oe.adjust_protection(
        action["symbol"],
        "live",
        pos_side=action["pos_side"],
        new_sl_trigger_px=action["new_sl_trigger_px"],
        new_tp_trigger_px=action["new_tp_trigger_px"],
        resize_to_full_position=action["resize_to_full_position"],
        consolidate_extra_sl=action["consolidate_extra_sl"],
        **position_fingerprint,
        **common,
    )


def _result_problem(
    result: object,
    action: dict[str, Any],
    facts: dict[str, Any],
) -> str | None:
    if not isinstance(result, dict):
        return "executor 未返回 object"
    if result.get("ok") is not True:
        return str(
            result.get("reject_reason")
            or result.get("error")
            or "executor ok!=true"
        )
    if action["action"] in {"OPEN", "ADD"}:
        expected_action = (
            "OPEN_LONG" if action["side"] == "long" else "OPEN_SHORT"
        )
    else:
        expected_action = action["action"]
    if str(result.get("action_taken") or "").strip().upper() != expected_action:
        return (
            f"executor action_taken={result.get('action_taken')!r} "
            f"与请求 {expected_action} 不一致"
        )
    if action["action"] in {"OPEN", "ADD"}:
        expected_is_add = action["action"] == "ADD"
        if result.get("is_add") is not expected_is_add:
            return (
                f"executor is_add={result.get('is_add')!r} 与请求 "
                f"{action['action']} 的执行时仓位语义不一致"
            )
    trades = result.get("trades")
    no_position = (
        action["action"] == "CLOSE"
        and isinstance(trades, list)
        and not trades
        and result.get("note") == "no_open_position"
    )
    if no_position:
        return None
    candidate = copy.deepcopy(result)
    candidate["live_facts"] = facts
    candidate["_profile"] = "live"
    errors = tw.validate(candidate) + tw.validate_strict_live_receipt(candidate)
    return "；".join(dict.fromkeys(errors)) if errors else None


_CLOSURE_CLEAN_HARD_REJECT_REASONS = frozenset({
    "portfolio_margin_cap_exceeded",
    "single_order_cap_infeasible",
    "single_order_risk_cap_infeasible",
    "leverage_exceeds",
    "existing_leverage_exceeds",
    "insufficient_available_margin",
    "available_margin_infeasible",
    "sl_direction_invalid",
})
_CLOSURE_CLEAN_PROTECTION_STATE_REJECT_REASONS = frozenset({
    "no_position",
    "pre_position_fingerprint_changed",
})


def _is_closure_clean_hard_reject(
    result: object,
    action: dict[str, Any],
    cycle_id: str,
) -> bool:
    """Classify a deterministic pre-submit hard-gate rejection as terminal.

    The executor returns ``ok=false`` for both safe risk refusals and genuine
    execution failures.  Only the small owner-approved hard-risk allowlist is
    a completed no-order business outcome.  Ambiguous/submitted/protection or
    account-truth failures remain ordinary batch failures.
    """
    if (
        not thresholds.minimal_contract_closure_active(cycle_id)
        or not isinstance(result, dict)
        or result.get("ok") is not False
        or str(result.get("action_taken") or "").strip().upper() != "REJECT"
        or result.get("p0") is True
        or not isinstance(result.get("trades"), list)
        or result.get("trades")
    ):
        return False
    action_name = str(action.get("action") or "").upper()
    reason = str(result.get("reject_reason") or "")
    from scripts.ledger_recovery import enabled as recovery_enabled
    if action_name in {"CLOSE", "REDUCE"} and recovery_enabled(cycle_id):
        expected = result.get("expected_pre_position")
        if not (
            reason == "pre_position_fingerprint_changed"
            and "actual_pre_position" in result
            and result["actual_pre_position"] is None
            and isinstance(expected, dict) and expected.get("exists") is True
            and expected.get("posId") not in (None, "")
            and expected.get("cTime") not in (None, "")
            and result.get("exchange_side_effect_uncertain") is not True
            and not any(key in result and result.get(key) not in (None, "", [], {})
                        for key in ("ordId", "ord_id", "order_id", "submitted_at", "applied"))
        ):
            return False
        try:
            size = float(expected.get("sz"))
        except (TypeError, ValueError):
            return False
        # No order or fill is fabricated. Any missing exchange-triggered close
        # is still reconciled by the independent report barrier before release.
        return math.isfinite(size) and size > 0
    if action_name == "ADJUST_PROTECTION":
        if (
            reason not in _CLOSURE_CLEAN_PROTECTION_STATE_REJECT_REASONS
            or result.get("exchange_side_effect_uncertain") is True
            or any(
                key in result and result.get(key) not in (None, "", [], {})
                for key in ("ordId", "ord_id", "order_id", "submitted_at",
                            "applied")
            )
        ):
            return False
        if reason == "pre_position_fingerprint_changed":
            actual = result.get("actual_pre_position")
            # Only a vanished target is safe to treat as a no-op.  A changed
            # but still-open position means the stage-owned facts are stale in
            # size/identity and must stop the batch.
            return actual is None
        return True
    if (
        action_name not in {"OPEN", "ADD"}
        or reason not in _CLOSURE_CLEAN_HARD_REJECT_REASONS
    ):
        return False
    risk = result.get("risk")
    if not isinstance(risk, dict):
        return False
    reconciliation = result.get("position_reconciliation")
    if not isinstance(reconciliation, dict):
        return False
    common_proof = (
        risk.get("approved") is False
        and str(risk.get("reject_reason") or "") == reason
        and isinstance(risk.get("math"), dict)
        and reconciliation.get("ok") is True
        and not any(
            key in result and result.get(key) not in (None, "", [], {})
            for key in ("ordId", "ord_id", "order_id", "submitted_at")
        )
    )
    if not common_proof:
        return False
    if reason != "sl_direction_invalid":
        return True

    # A stale stop geometry is a legitimate hard refusal only when the
    # executor's own risk math proves that price crossed the frozen stop before
    # submission.  Never turn a merely labelled reject into a clean outcome.
    math_facts = risk["math"]
    try:
        mark_px = float(math_facts.get("mark_px"))
        stop_px = float(math_facts.get("sl_trigger_px"))
    except (TypeError, ValueError):
        return False
    side = str(action.get("side") or "").strip().lower()
    symbol = str(action.get("symbol") or "").strip().upper()
    return (
        math.isfinite(mark_px)
        and math.isfinite(stop_px)
        and mark_px > 0
        and stop_px > 0
        and str(math_facts.get("symbol") or "").strip().upper() == symbol
        and str(math_facts.get("side") or "").strip().lower() == side
        and (
            (side == "long" and stop_px >= mark_px)
            or (side == "short" and stop_px <= mark_px)
        )
    )


def _can_continue_independent_refusal(result: object, action: dict[str, Any],
                                      remaining: list[dict[str, Any]], cycle_id: str) -> bool:
    """Retain a local no-order failure while later independent signals run.

    This never classifies a writer, account, intent, protection, submission or
    unknown failure as harmless. Each later action still enters all usual gates.
    """
    from core.independent_refusal import can_continue
    return can_continue(result,action,remaining,cycle_id)


def _failure_text(action: dict[str, Any], result: object, problem: str) -> str:
    side = action.get("side") or action.get("pos_side")
    return (
        f"{action['action']} {action['symbol']}/{side}: {problem}"
    )


def _error_receipt(
    context: dict[str, Any],
    facts: dict[str, Any],
    *,
    plan_hash: str,
    requested: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    receipt = copy.deepcopy(context)
    receipt.update({
        "mode": "live",
        "profile": "live",
        "status": "error",
        "decision": "error",
        "action_taken": "REJECT",
        "n_orders": 0,
        "trades": [],
        "errors": [reason],
        "batch_status": "failed",
        "batch_ok": False,
        "runner_in_progress": False,
        "position_action_plan_hash": plan_hash,
        "requested_position_actions": requested,
        "position_action_results": [],
        "position_action_failures": failures,
        "reject_reason": "position_action_batch_failed",
        "reject_detail": reason,
        "live_facts": facts,
    })
    return receipt


def _aggregate_receipt(
    context: dict[str, Any],
    facts: dict[str, Any],
    *,
    plan_hash: str,
    requested: list[dict[str, Any]],
    successes: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    adjustments: list[dict[str, Any]] = []
    for row in successes:
        result = row["result"]
        trades.extend(copy.deepcopy(result.get("trades") or []))
        if row["request"]["action"] == "ADJUST_PROTECTION":
            adjustments.append(result)

    if not trades and not adjustments and failures:
        first = failures[0]
        return _error_receipt(
            context,
            facts,
            plan_hash=plan_hash,
            requested=requested,
            failures=failures,
            reason=first["problem"],
        )

    receipt = copy.deepcopy(context)
    if trades:
        requested_trade_actions = {
            row["request"]["action"]
            for row in successes
            if row["result"].get("trades")
        }
        new_risk_actions = requested_trade_actions & {"OPEN", "ADD"}
        if new_risk_actions:
            action_taken = "ADD" if new_risk_actions == {"ADD"} else "OPEN"
        else:
            trade_actions = {
                str(item.get("action") or "").strip().lower()
                for item in trades
            }
            action_taken = "CLOSE" if "close" in trade_actions else "REDUCE"
        decision = "traded"
    elif adjustments:
        action_taken = "ADJUST_PROTECTION"
        decision = "hold"
    else:
        action_taken = "HOLD"
        decision = "hold"

    receipt.update({
        "mode": "live",
        "profile": "live",
        "status": "ok",
        "decision": decision,
        "action_taken": action_taken,
        "n_orders": len(trades),
        "trades": trades,
        "errors": [item["problem"] for item in failures],
        "ok": True,
        "batch_status": "partial" if failures else "completed",
        "batch_ok": not failures,
        "runner_in_progress": False,
        "position_action_plan_hash": plan_hash,
        "requested_position_actions": requested,
        "position_action_results": successes,
        "position_action_failures": failures,
        "live_facts": facts,
    })
    if adjustments:
        first = adjustments[0]
        for key in (
            "symbol", "pos_side", "side", "protection_change", "path",
            "protection_state", "applied", "previous",
        ):
            if key in first:
                receipt[key] = copy.deepcopy(first[key])
        receipt["protection_changes"] = [
            {
                key: copy.deepcopy(result.get(key))
                for key in (
                    "action_taken", "dryrun", "symbol", "pos_side",
                    "protection_change", "path",
                    "protection_state", "applied", "previous",
                )
            }
            for result in adjustments
        ]
    return receipt


def _receipt_validation_errors(receipt: dict[str, Any]) -> list[str]:
    payload = {**receipt, "_profile": "live"}
    return list(dict.fromkeys(
        tw.validate(payload) + tw.validate_strict_live_receipt(payload)
    ))


def _commit_interim_successes(
    context: dict[str, Any],
    facts: dict[str, Any],
    *,
    plan_hash: str,
    successes: list[dict[str, Any]],
    receipt_file: Path,
    db_root: Path,
    failures: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist prior fills before a later OPEN/ADD re-runs ledger preflight."""
    interim = _aggregate_receipt(
        context,
        facts,
        plan_hash=plan_hash,
        requested=[copy.deepcopy(row["request"]) for row in successes + (failures or [])],
        successes=successes,
        failures=failures or [],
    )
    interim["batch_status"] = "partial"
    interim["batch_ok"] = not bool(failures)
    interim["runner_in_progress"] = True
    from core.independent_refusal import attach
    attach(interim, failures or [])
    errors = _receipt_validation_errors(interim)
    if errors:
        raise RuntimeError(
            "interim 回执内部校验失败（未提交）: " + "；".join(errors)
        )
    receipt_file_error: str | None = None
    try:
        _atomic_write_json(receipt_file, interim)
    except Exception as exc:  # audit artifact must not strand confirmed fills
        receipt_file_error = f"{type(exc).__name__}: {exc}"
        interim["receipt_file_warning"] = receipt_file_error
        interim["errors"] = list(dict.fromkeys(
            list(interim.get("errors") or [])
            + [f"interim receipt 文件落盘失败: {receipt_file_error}"]
        ))
        interim["batch_ok"] = False
    try:
        writer = tw.commit_receipt(
            interim,
            "live",
            db_path=db_root / "live_trades.db",
            nudge=False,
            require_live_facts=True,
        )
    except Exception as exc:  # keep a structured terminal audit; never continue
        writer = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if receipt_file_error and writer.get("ok") and not writer.get("refused"):
        # Ledger persistence succeeded, but continuing to a later OPEN/ADD
        # without the required audit artifact would widen the failure.  Stop
        # the batch while retaining enough state to write a final superset.
        writer = {
            **writer,
            "ok": False,
            "ledger_committed": True,
            "receipt_file_error": receipt_file_error,
        }
    return interim, writer


def _execute_position_plan_locked(
    plan: dict[str, Any],
    facts: dict[str, Any],
    *,
    cycle_id: str,
    db_root: Path,
    receipt_file: Path,
    position_exit_file: Path | None = None,
    stage_facts_file: Path | None = None,
    stage_decision_view_file: Path | None = None,
    stage_input_handoff_file: Path | None = None,
    stage_input_binding: dict[str, Any] | None = None,
    nudge: bool = True,
    plan_sha256: str | None = None,
    state_file: Path | None = None,
    runtime_guard=None,
    marker_already_started: bool = False,
    marker_session_key: str | None = None,
    marker_stage_runner_pid: int | None = None,
    handoff_state_file: Path | None = None,
    handoff_lock_file: Path | None = None,
    preflight_attempt: int = 1,
) -> dict[str, Any]:
    marker_path = Path(state_file) if state_file is not None else None
    marker_facts_hash = str(facts.get("facts_hash") or "").strip()
    marker_plan_sha = str(plan_sha256 or "").strip().lower()
    if marker_path is not None:
        if not marker_facts_hash:
            raise PlanError("live_facts.facts_hash 缺失，不能建立 runner 状态契约")
        if (
            len(marker_plan_sha) != 64
            or any(char not in "0123456789abcdef" for char in marker_plan_sha)
        ):
            raise PlanError("plan_sha256 必须是 plan 文件原始 bytes 的 SHA256")
        if not marker_already_started:
            _write_runner_state(
                marker_path,
                cycle_id=cycle_id,
                state="started",
                facts_hash=marker_facts_hash,
                plan_sha256=marker_plan_sha,
                session_key=marker_session_key,
                stage_runner_pid=marker_stage_runner_pid,
                preflight_attempts=preflight_attempt,
            )

    preflight_completed = False
    try:
        context, actions = preflight_plan(
            plan,
            facts,
            cycle_id=cycle_id,
            db_root=db_root,
        )
        _validate_position_exit_evidence(
            facts,
            cycle_id=cycle_id,
            evidence_file=position_exit_file,
        )
        def revalidate_stage_input() -> None:
            if stage_input_binding is None:
                return
            if (
                stage_facts_file is None
                or stage_decision_view_file is None
                or stage_input_handoff_file is None
            ):
                raise PlanError(
                    "stage_input_binding_invalid: closure artifact path 缺失")
            observed = _validate_stage_owned_live_input_binding(
                plan,
                facts,
                cycle_id=cycle_id,
                plan_sha256=marker_plan_sha,
                facts_file=stage_facts_file,
                decision_view_file=stage_decision_view_file,
                handoff_file=stage_input_handoff_file,
            )
            if observed != stage_input_binding:
                raise PlanError(
                    "stage_input_binding_changed: preflight期间handoff/facts/view变化")

        # Re-read after plan preflight.  A coherent-looking Agent replacement
        # that races the first admission must still lose before any executor.
        revalidate_stage_input()
        preflight_completed = True
        context["facts_hash"] = marker_facts_hash
        context["plan_sha256"] = marker_plan_sha
        if stage_input_binding is not None:
            context["stage_input_binding"] = copy.deepcopy(
                stage_input_binding)
        plan_hash = _canonical_hash(plan)
        requested = [_audit_action(action) for action in actions]

        if facts.get("status") == "blocking" and not actions:
            _require_same_stage_authority(
                runtime_guard,
                cycle_id,
                expected_stage_runner_pid=marker_stage_runner_pid,
            )
            receipt = _error_receipt(
                context,
                facts,
                plan_hash=plan_hash,
                requested=requested,
                failures=[],
                reason="live_facts blocking 且无获准去风险动作",
            )
            _atomic_write_json(receipt_file, receipt)
            writer = tw.commit_receipt(
                receipt,
                "live",
                db_path=db_root / "live_trades.db",
                nudge=nudge,
                require_live_facts=True,
            )
            committed = bool(writer.get("ok") and not writer.get("refused"))
            if marker_path is not None:
                _write_runner_state(
                    marker_path,
                    cycle_id=cycle_id,
                    state="committed" if committed else "failed",
                    facts_hash=marker_facts_hash,
                    plan_sha256=marker_plan_sha,
                    error=None if committed else "writer_commit_refused",
                    session_key=marker_session_key,
                    stage_runner_pid=marker_stage_runner_pid,
                    preflight_attempts=preflight_attempt,
                )
            return {
                "ok": False,
                "committed": committed,
                "batch_status": "failed",
                "receipt": receipt,
                "writer": writer,
            }

        successes: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        persisted_success_count = 0
        interim_writer_failure: dict[str, Any] | None = None
        for action_index, action in enumerate(actions):
            if (
                action["action"] in {"OPEN", "ADD"}
                and any(
                    row["result"].get("trades")
                    for row in successes[persisted_success_count:]
                )
            ):
                _, interim_writer = _commit_interim_successes(
                    context,
                    facts,
                    plan_hash=plan_hash,
                    successes=successes,
                    receipt_file=receipt_file,
                    db_root=db_root,
                    failures=failures,
                )
                if (
                    interim_writer.get("ok") is not True
                    or interim_writer.get("refused")
                ):
                    interim_writer_failure = interim_writer
                    detail = str(
                        interim_writer.get("refused")
                        or interim_writer.get("error")
                        or "interim writer ok!=true"
                    )
                    failures.append({
                        "request": _audit_action(action),
                        "problem": _failure_text(
                            action,
                            interim_writer,
                            f"interim commit 失败，未调用后续 executor: {detail}",
                        ),
                        "result": copy.deepcopy(interim_writer),
                    })
                    break
                persisted_success_count = len(successes)
            _require_same_stage_authority(
                runtime_guard,
                cycle_id,
                expected_stage_runner_pid=marker_stage_runner_pid,
            )
            if marker_path is not None:
                _write_runner_state(
                    marker_path,
                    cycle_id=cycle_id,
                    state="executing",
                    facts_hash=marker_facts_hash,
                    plan_sha256=marker_plan_sha,
                    session_key=marker_session_key,
                    stage_runner_pid=marker_stage_runner_pid,
                )
            # Re-admit at the true executor call boundary.  This cannot cancel
            # a request already handed to OKX, but it prevents a later action
            # after stage stopping/deadline/revocation became observable.
            if handoff_state_file is not None and handoff_lock_file is not None:
                with _runner_cycle_lock(handoff_lock_file, cycle_id):
                    _require_same_stage_authority(
                        runtime_guard,
                        cycle_id,
                        expected_stage_runner_pid=marker_stage_runner_pid,
                    )
                    _reject_revoked_handoff(
                        handoff_state_file,
                        cycle_id,
                        session_key=str(marker_session_key or ""),
                        stage_runner_pid=marker_stage_runner_pid,
                        facts_hash=marker_facts_hash,
                        plan_sha256=marker_plan_sha,
                    )
                    revalidate_stage_input()
                    # Keep the same CAS lock through the actual call.  This is
                    # the linearization point: the supervisor cannot persist a
                    # contradictory revocation between the final guard and the
                    # executor hand-off.  Executor/network timeouts stay owned
                    # by the unchanged lower layer.
                    result = _call_executor(
                        action,
                        context=context,
                        facts=facts,
                        cycle_id=cycle_id,
                        db_root=db_root,
                    )
            else:
                revalidate_stage_input()
                result = _call_executor(
                    action,
                    context=context,
                    facts=facts,
                    cycle_id=cycle_id,
                    db_root=db_root,
                )
            audit_action = _audit_action(action)
            problem = _result_problem(result, action, facts)
            if problem:
                if _is_closure_clean_hard_reject(result, action, cycle_id):
                    clean_result = copy.deepcopy(result)
                    clean_result["clean_hard_reject"] = True
                    clean_result["business_outcome"] = (
                        "completed_no_action_target_gone"
                        if action.get("action") in {"ADJUST_PROTECTION", "CLOSE", "REDUCE"}
                        else "completed_no_order_hard_reject")
                    successes.append({
                        "request": audit_action,
                        "result": clean_result,
                    })
                    continue
                if (
                    isinstance(result, dict)
                    and isinstance(result.get("trades"), list)
                    and result.get("trades")
                ):
                    # A confirmed fill is accounting truth even if the
                    # executor's surrounding contract is malformed.  Preserve
                    # it in the final superset while keeping the batch failed.
                    successes.append({
                        "request": audit_action,
                        "result": copy.deepcopy(result),
                    })
                failures.append({
                    "request": audit_action,
                    "problem": _failure_text(action, result, problem),
                    "result": copy.deepcopy(result),
                })
                if _can_continue_independent_refusal(result, action, actions[action_index+1:], cycle_id):
                    failures[-1]["continuation"] = {
                        "allowed": True, "scope": "other_symbols_only", "same_action_retry": False,
                        "reason": "verified_no_order_minimum_risk_refusal",
                    }
                    continue
                break
            successes.append({
                "request": audit_action,
                "result": copy.deepcopy(result),
            })
            if isinstance(result, dict) and result.get("p0") is True:
                failures.append({
                    "request": audit_action,
                    "problem": _failure_text(
                        action, result, "executor 返回 p0=true，停止后续动作"
                    ),
                    "result": copy.deepcopy(result),
                })
                break

        if not actions:
            _require_same_stage_authority(
                runtime_guard,
                cycle_id,
                expected_stage_runner_pid=marker_stage_runner_pid,
            )
        receipt = _aggregate_receipt(
            context,
            facts,
            plan_hash=plan_hash,
            requested=requested,
            successes=successes,
            failures=failures,
        )
        from core.independent_refusal import attach
        attach(receipt, failures)
        validation_errors = _receipt_validation_errors(receipt)
        if (
            thresholds.complete_cycle_uses_business_terminal_stop(cycle_id)
            and not validation_errors
            and not failures
            and receipt.get("status") == "ok"
            and receipt.get("batch_status") == "completed"
            and receipt.get("batch_ok") is True
        ):
            # V4 §6 stops here: every Agent judgement and requested exchange
            # action/readback is complete, while receipt-file IO, business DB
            # commit, reports, logs and Push remain downstream reliability work.
            receipt["business_terminal"] = {
                "schema_version": 1,
                "cycle_id": cycle_id,
                "status": "completed",
                "completed_at_cst": datetime.now(CST).strftime(
                    "%Y-%m-%d %H:%M:%S"),
                "clock_stop": (
                    "analysis_judgment_trade_completed_before_persistence"),
                "persistence_completed": False,
                "excluded_from_sla": [
                    "receipt_file_write",
                    "business_database_commit",
                    "report_build_validate_archive",
                    "log_write",
                    "push_delivery",
                ],
            }
        salvage_errors: list[str] = []
        if validation_errors:
            if receipt.get("trades"):
                salvage_errors = validation_errors
                receipt["status"] = "error"
                receipt["ok"] = False
                receipt["batch_status"] = "partial"
                receipt["batch_ok"] = False
                receipt["runner_in_progress"] = False
                receipt["errors"] = list(dict.fromkeys(
                    list(receipt.get("errors") or []) + validation_errors
                ))
                receipt["contract_quarantine"] = {
                    "kind": "confirmed_trade_receipt_contract_invalid",
                    "validation_errors": validation_errors,
                    "side_effect_trades_preserved": len(receipt["trades"]),
                    "experience_write_skipped": True,
                }
            else:
                raise RuntimeError(
                    "聚合回执内部校验失败（未提交）: "
                    + "；".join(validation_errors)
                )

        receipt_file_error: str | None = None
        try:
            _atomic_write_json(receipt_file, receipt)
        except Exception as exc:  # never let an audit file strand a real fill
            receipt_file_error = f"{type(exc).__name__}: {exc}"
            receipt["receipt_file_warning"] = receipt_file_error
            receipt["errors"] = list(dict.fromkeys(
                list(receipt.get("errors") or [])
                + [f"final receipt 文件落盘失败: {receipt_file_error}"]
            ))
            receipt["batch_status"] = "partial"
            receipt["batch_ok"] = False
        if (
            interim_writer_failure is not None
            and not interim_writer_failure.get("ledger_committed")
        ):
            if marker_path is not None:
                _write_runner_state(
                    marker_path,
                    cycle_id=cycle_id,
                    state="failed",
                    facts_hash=marker_facts_hash,
                    plan_sha256=marker_plan_sha,
                    error="interim_writer_commit_failed",
                    session_key=marker_session_key,
                    stage_runner_pid=marker_stage_runner_pid,
                    preflight_attempts=preflight_attempt,
                )
            return {
                "ok": False,
                "committed": False,
                "batch_status": receipt["batch_status"],
                "receipt": receipt,
                "writer": interim_writer_failure,
            }
        if salvage_errors:
            writer = tw.commit_side_effect_salvage(
                receipt,
                "live",
                validation_errors=salvage_errors,
                db_path=db_root / "live_trades.db",
                _capability=tw._SIDE_EFFECT_SALVAGE_CAPABILITY,
            )
        else:
            writer = tw.commit_receipt(
                receipt,
                "live",
                db_path=db_root / "live_trades.db",
                nudge=nudge,
                require_live_facts=True,
            )
        committed = bool(writer.get("ok") and not writer.get("refused"))
        artifact_failed = bool(
            receipt_file_error
            or (
                interim_writer_failure is not None
                and interim_writer_failure.get("receipt_file_error")
            )
        )
        if marker_path is not None:
            _write_runner_state(
                marker_path,
                cycle_id=cycle_id,
                state="committed" if committed and not artifact_failed else "failed",
                facts_hash=marker_facts_hash,
                plan_sha256=marker_plan_sha,
                error=(
                    None
                    if committed and not artifact_failed
                    else (
                        "receipt_file_write_failed"
                        if artifact_failed
                        else "writer_commit_refused"
                    )
                ),
                session_key=marker_session_key,
                stage_runner_pid=marker_stage_runner_pid,
                preflight_attempts=preflight_attempt,
            )
        return {
            "ok": committed and not failures and not artifact_failed,
            "committed": committed,
            "batch_status": receipt["batch_status"],
            "receipt": receipt,
            "writer": writer,
            "receipt_file_error": receipt_file_error,
        }
    except Exception as exc:
        if marker_path is not None:
            # A PlanError before preflight completion provably left zero
            # exchange/business side effects, so it stays re-admittable for
            # exactly one full-file plan rewrite.  Anything after preflight
            # (or a non-contract crash) keeps the sticky terminal state.
            retryable = (
                not preflight_completed
                and isinstance(exc, PlanError)
                and preflight_attempt < PREFLIGHT_MAX_ATTEMPTS
            )
            _write_runner_state(
                marker_path,
                cycle_id=cycle_id,
                state="failed_preflight" if retryable else "failed",
                facts_hash=marker_facts_hash,
                plan_sha256=marker_plan_sha,
                error=f"{type(exc).__name__}: {exc}",
                session_key=marker_session_key,
                stage_runner_pid=marker_stage_runner_pid,
                preflight_attempts=preflight_attempt,
            )
        raise


def execute_position_plan(
    plan: dict[str, Any],
    facts: dict[str, Any],
    *,
    cycle_id: str,
    db_root: Path,
    receipt_file: Path,
    position_exit_file: Path | None = None,
    facts_file: Path | None = None,
    decision_view_file: Path | None = None,
    live_input_handoff_file: Path | None = None,
    nudge: bool = True,
    plan_sha256: str | None = None,
    state_file: Path | None = None,
    runtime_guard=None,
) -> dict[str, Any]:
    """Execute under a same-cycle cross-process lock and marker CAS."""
    cycle_id = _validated_cycle_id(cycle_id)
    marker_path = (
        Path(state_file)
        if state_file is not None
        else Path(receipt_file).with_name(
            f"live_runner_state_{cycle_id.replace(':', '-')}.json"
        )
    )
    effective_plan_sha = plan_sha256 or _canonical_hash(plan)
    stage_input_binding: dict[str, Any] | None = None
    if thresholds.minimal_contract_closure_active(cycle_id):
        if (
            facts_file is None
            or decision_view_file is None
            or live_input_handoff_file is None
        ):
            raise PlanError(
                "stage_input_handoff_missing: closure runner必须使用stage-owned "
                "handoff/facts/view")
        # First admission happens before runtime authority, plan preflight,
        # state-marker writes, account reads or executor calls.
        stage_input_binding = _validate_stage_owned_live_input_binding(
            plan,
            facts,
            cycle_id=cycle_id,
            plan_sha256=effective_plan_sha,
            facts_file=Path(facts_file),
            decision_view_file=Path(decision_view_file),
            handoff_file=Path(live_input_handoff_file),
        )
        retired_paths = closure_retired_structure_paths(plan)
        if retired_paths:
            raise PlanError(
                "closure plan 禁止携带退役机器结构: "
                + ",".join(retired_paths[:16]))
        _validate_closure_plan_reasoning(plan)
    lock_path = _default_runner_lock_file(marker_path, cycle_id)
    handoff_path = _default_handoff_state_file(marker_path, cycle_id)
    handoff_lock_path = _default_handoff_lock_file(marker_path, cycle_id)
    marker_session_key = _gateway_session_key(cycle_id)
    with _runner_cycle_lock(lock_path, cycle_id):
        with _runner_cycle_lock(handoff_lock_path, cycle_id):
            # The observer uses this same lock before persisting revocation.
            # Whichever side wins is durable: a revoked gate blocks this call,
            # while a bound started marker prevents a later timeout decision.
            authority = _invoke_runtime_guard(runtime_guard, cycle_id)
            marker_stage_runner_pid = _authority_stage_runner_pid(authority)
            marker_facts_hash = str(facts.get("facts_hash") or "").strip()
            marker_plan_sha = str(effective_plan_sha or "").strip().lower()
            if not marker_facts_hash:
                raise PlanError(
                    "live_facts.facts_hash 缺失，不能建立 runner 状态契约")
            if (
                len(marker_plan_sha) != 64
                or any(char not in "0123456789abcdef"
                       for char in marker_plan_sha)
            ):
                raise PlanError(
                    "plan_sha256 必须是 plan 文件原始 bytes 的 SHA256")
            _reject_revoked_handoff(
                handoff_path,
                cycle_id,
                session_key=marker_session_key,
                stage_runner_pid=marker_stage_runner_pid,
                facts_hash=marker_facts_hash,
                plan_sha256=marker_plan_sha,
            )
            prior_attempts = _reject_existing_runner_state(
                marker_path,
                cycle_id,
                facts_hash=marker_facts_hash,
                plan_sha256=marker_plan_sha,
            )
            preflight_attempt = prior_attempts + 1
            _write_runner_state(
                marker_path,
                cycle_id=cycle_id,
                state="started",
                facts_hash=marker_facts_hash,
                plan_sha256=marker_plan_sha,
                session_key=marker_session_key,
                stage_runner_pid=marker_stage_runner_pid,
                preflight_attempts=preflight_attempt,
            )
        return _execute_position_plan_locked(
            plan,
            facts,
            cycle_id=cycle_id,
            db_root=db_root,
            receipt_file=receipt_file,
            position_exit_file=position_exit_file,
            stage_facts_file=(
                Path(facts_file) if facts_file is not None else None),
            stage_decision_view_file=(
                Path(decision_view_file)
                if decision_view_file is not None else None),
            stage_input_handoff_file=(
                Path(live_input_handoff_file)
                if live_input_handoff_file is not None else None),
            stage_input_binding=stage_input_binding,
            nudge=nudge,
            plan_sha256=effective_plan_sha,
            state_file=marker_path,
            runtime_guard=runtime_guard,
            marker_already_started=True,
            marker_session_key=marker_session_key,
            marker_stage_runner_pid=marker_stage_runner_pid,
            handoff_state_file=handoff_path,
            handoff_lock_file=handoff_lock_path,
            preflight_attempt=preflight_attempt,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "执行 Agent 已裁决的 Live OPEN/ADD/持仓动作并原子提交唯一交易回执"
        )
    )
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--plan-file", type=Path, required=True)
    parser.add_argument("--facts-file", type=Path, required=True)
    parser.add_argument("--receipt-file", type=Path, required=True)
    parser.add_argument("--db-root", type=Path, default=ROOT / "db")
    args = parser.parse_args(argv)

    try:
        cycle_id = _validated_cycle_id(args.cycle_id)
        plan_file = _require_direct_tmp_path(args.plan_file, "plan-file")
        facts_file = _require_direct_tmp_path(args.facts_file, "facts-file")
        receipt_file = _require_direct_tmp_path(
            args.receipt_file, "receipt-file")
        decision_view_file = _default_decision_view_file(
            facts_file, cycle_id)
        live_input_handoff_file = _default_live_input_handoff_file(
            facts_file, cycle_id)
        if thresholds.minimal_contract_closure_active(cycle_id):
            # Gate on supervisor readiness before parsing an Agent-authored
            # plan.  Full artifact/hash binding is repeated below and again at
            # the executor boundary to close replacement races.
            _precheck_stage_owned_live_input_handoff(
                live_input_handoff_file,
                cycle_id=cycle_id,
                facts_file=facts_file,
                decision_view_file=decision_view_file,
            )
        plan, plan_sha256 = _read_json_with_sha(plan_file, "plan-file")
        facts = _read_json(facts_file, "facts-file")
        from _plan_publication import validate as validate_plan_publication
        publication = validate_plan_publication(
            plan_file, cycle_id, plan_sha256, str(facts.get("facts_hash") or ""))
        if publication.get("ok") is not True:
            raise PlanError("plan 必须经 write_position_plan.py 严格校验发布: " + str(publication.get("error")))
        position_exit_file = facts_file.with_name(
            f"position_exit_{cycle_id.replace(':', '-')}.json"
        )
        result = execute_position_plan(
            plan,
            facts,
            cycle_id=cycle_id,
            db_root=args.db_root,
            receipt_file=receipt_file,
            position_exit_file=position_exit_file,
            facts_file=facts_file,
            decision_view_file=decision_view_file,
            live_input_handoff_file=live_input_handoff_file,
            plan_sha256=plan_sha256,
            state_file=_default_runner_state_file(cycle_id),
            runtime_guard=lambda cycle: validate_live_runtime_authority(
                cycle,
                db_root=args.db_root,
            ),
        )
        summary = {
            "ok": result["ok"],
            "committed": result["committed"],
            "cycle_id": cycle_id,
            "batch_status": result["batch_status"],
            "action_taken": result["receipt"].get("action_taken"),
            "n_orders": result["receipt"].get("n_orders"),
            "requested_actions": len(result["receipt"].get(
                "requested_position_actions") or []),
            "completed_actions": len(result["receipt"].get(
                "position_action_results") or []),
            "failed_actions": len(result["receipt"].get(
                "position_action_failures") or []),
            "receipt_file": str(receipt_file),
            "receipt_hash": _canonical_hash(result["receipt"]),
            "writer": result["writer"],
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        if not result["committed"]:
            return 4
        return 0 if result["ok"] else 3
    except PlanError as exc:
        # Tell the agent whether the one-shot plan rewrite is still open:
        # only a marker parked in failed_preflight (below the attempt cap)
        # re-admits a corrected plan for this cycle.
        retry_allowed = False
        try:
            marker = json.loads(_default_runner_state_file(
                _validated_cycle_id(args.cycle_id)).read_text(
                    encoding="utf-8"))
            retry_allowed = (
                isinstance(marker, dict)
                and marker.get("state") == "failed_preflight"
                and int(marker.get("preflight_attempts") or 1)
                < PREFLIGHT_MAX_ATTEMPTS
            )
        except Exception:  # noqa: BLE001 - hint only, never mask the error
            retry_allowed = False
        print(json.dumps({
            "ok": False,
            "committed": False,
            "error_kind": "plan_preflight_failed",
            "error": str(exc),
            "plan_rewrite_retry_allowed": retry_allowed,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "ok": False,
            "committed": False,
            "error_kind": "runner_internal_failure",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
