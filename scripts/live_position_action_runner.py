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

from core import order_executor as oe  # noqa: E402
from core import risk_validator as rv  # noqa: E402
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
RUNNER_STATE_SCHEMA_VERSION = 1
CST = timezone(timedelta(hours=8))
LIVE_CHILD_DEADLINE_SECONDS = 13 * 60
ANALYSIS_DEADLINE_GUARD_FROM = "2026-08-15T21:45"
ANALYSIS_DEADLINE_SECONDS = 9 * 60 + 30
DEFAULT_STAGE_STATUS_DIR = Path(os.environ.get(
    "OKX_STAGE_STATUS_DIR", ROOT / "logs" / "stage-status"))


class PlanError(ValueError):
    """A plan failed before any exchange or business-database write."""


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
    cycle = str(cycle_id or "").strip()
    try:
        cycle_start = datetime.strptime(
            cycle, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    except ValueError as exc:
        raise PlanError(f"cycle_id 非法: {cycle!r}") from exc
    current = now or datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    current = current.astimezone(CST)
    deadline = cycle_start + timedelta(seconds=LIVE_CHILD_DEADLINE_SECONDS)
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
            seconds=ANALYSIS_DEADLINE_SECONDS)
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


def _invoke_runtime_guard(runtime_guard, cycle_id: str) -> None:
    if runtime_guard is not None:
        runtime_guard(cycle_id)


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
    return {
        "action": str(row["action"] or "").strip().lower(),
        "side": str(row["side"] or "").strip().lower(),
        "reasoning": str(row["reasoning"] or "").strip(),
        "decision_card": card,
    }


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
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
        newline="\n",
    )
    os.replace(tmp, path)


def _default_runner_state_file(cycle_id: str) -> Path:
    return ROOT / "tmp" / f"live_runner_state_{cycle_id.replace(':', '-')}.json"


def _default_runner_lock_file(state_file: Path, cycle_id: str) -> Path:
    # Profile-wide serialization also prevents adjacent cycles from issuing
    # overlapping CLOSE/OPEN actions against the same live account.
    return Path(state_file).with_name("live_runner.lock")


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


def _reject_existing_runner_state(path: Path, cycle_id: str) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"runner state 已存在但不可校验: {path}") from exc
    if not isinstance(payload, dict) or payload.get("cycle_id") != cycle_id:
        raise PlanError("runner state identity 不匹配，拒绝覆盖")
    state = str(payload.get("state") or "").strip().lower()
    if state in {"started", "executing", "committed", "failed"}:
        raise PlanError(f"本 cycle runner state={state}，拒绝重复执行")
    raise PlanError(f"runner state={state or 'missing'} 非法，拒绝覆盖")


def _write_runner_state(
    path: Path,
    *,
    cycle_id: str,
    state: str,
    facts_hash: str,
    plan_sha256: str,
    error: str | None = None,
) -> None:
    if state not in {"started", "executing", "committed", "failed"}:
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
    }
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
    forbidden = sorted(TERMINAL_CONTEXT_KEYS & set(raw))
    if forbidden:
        raise PlanError(
            "receipt_context 不得预填终局字段: " + ",".join(forbidden)
        )
    context = copy.deepcopy(raw)
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
            tolerance = max(1e-6, abs(float(canonical_equity)) * 1e-8)
            if abs(float(supplied_equity) - float(canonical_equity)) > tolerance:
                raise PlanError("receipt_context.equity 与 live_facts 不一致")
        except (TypeError, ValueError) as exc:
            raise PlanError("receipt_context.equity 必须是有效数字") from exc
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

    context = _normalize_context(plan, facts, cycle_id)
    raw_actions = plan.get("actions")
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
    seen: set[tuple[str, str]] = set()

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
        if key in seen:
            raise PlanError(
                f"同一仓位每轮只能提交一个最终动作: {symbol}/{side}"
            )
        seen.add(key)
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
            card = signal["decision_card"]
            risk_reward = card.get("risk_reward")
            if not isinstance(risk_reward, dict):
                raise PlanError(
                    f"actions[{index}] canonical decision_card.risk_reward 缺失"
                )
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
                or str(card.get("agent_judgement") or "").strip()
            )
            if not reasoning:
                raise PlanError(
                    f"actions[{index}] canonical analysis reasoning/judgement 为空"
                )
            item = {
                "action": action,
                "symbol": symbol,
                "side": side,
                "target_stop_risk_pct_equity": target_pct,
                "lev": lev,
                "reasoning": reasoning,
                "_decision_card": copy.deepcopy(card),
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
            action_context = _action_context(context, item)
            action_context_errors = oe.validate_receipt_context(
                action_context,
                cycle_id=cycle_id,
                required=True,
                expected_symbol=symbol,
                expected_side=side,
                expected_regime=str(context.get("regime") or "") or None,
                require_experience=True,
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
    return context, normalized


def _action_context(
    context: dict[str, Any], action: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(context)
    action_name = action["action"]
    if action_name in {"OPEN", "ADD"}:
        result["decision_card"] = copy.deepcopy(action["_decision_card"])
    result.update({
        "decision": "hold" if action_name == "ADJUST_PROTECTION" else "traded",
        "n_orders": 0 if action_name == "ADJUST_PROTECTION" else 1,
        "errors": [],
    })
    return result


def _call_executor(
    action: dict[str, Any],
    *,
    context: dict[str, Any],
    facts: dict[str, Any],
    cycle_id: str,
    db_root: Path,
) -> dict[str, Any]:
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
        mark_px = oe.ox.get_mark_price(action["symbol"], "live")
        balance = facts.get("balance") or {}
        sizing = rv.size_for_target_stop_risk(
            mark_px=mark_px,
            ct_val=specs.get("ct_val"),
            lot_sz=specs.get("lot_sz"),
            min_order_size=specs.get("min_sz"),
            equity=balance.get("totalEq"),
            sl_trigger_px=action["_sl_trigger_px"],
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
            for trade in result.get("trades") or []:
                if isinstance(trade, dict):
                    # Writer persists trade-level cards first.  This binds a
                    # multi-order receipt to the exact canonical card used for
                    # each symbol instead of one top-level shared card.
                    trade["decision_card"] = copy.deepcopy(
                        action["_decision_card"]
                    )
                    trade["decision_protocol"] = "decision_card_v1"
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist prior fills before a later OPEN/ADD re-runs ledger preflight."""
    interim = _aggregate_receipt(
        context,
        facts,
        plan_hash=plan_hash,
        requested=[copy.deepcopy(row["request"]) for row in successes],
        successes=successes,
        failures=[],
    )
    interim["batch_status"] = "partial"
    interim["batch_ok"] = True
    interim["runner_in_progress"] = True
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
    nudge: bool = True,
    plan_sha256: str | None = None,
    state_file: Path | None = None,
    runtime_guard=None,
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
        _write_runner_state(
            marker_path,
            cycle_id=cycle_id,
            state="started",
            facts_hash=marker_facts_hash,
            plan_sha256=marker_plan_sha,
        )

    try:
        context, actions = preflight_plan(
            plan,
            facts,
            cycle_id=cycle_id,
            db_root=db_root,
        )
        context["facts_hash"] = marker_facts_hash
        context["plan_sha256"] = marker_plan_sha
        plan_hash = _canonical_hash(plan)
        requested = [_audit_action(action) for action in actions]

        if facts.get("status") == "blocking" and not actions:
            _invoke_runtime_guard(runtime_guard, cycle_id)
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
        for action in actions:
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
            _invoke_runtime_guard(runtime_guard, cycle_id)
            if marker_path is not None:
                _write_runner_state(
                    marker_path,
                    cycle_id=cycle_id,
                    state="executing",
                    facts_hash=marker_facts_hash,
                    plan_sha256=marker_plan_sha,
                )
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
            _invoke_runtime_guard(runtime_guard, cycle_id)
        receipt = _aggregate_receipt(
            context,
            facts,
            plan_hash=plan_hash,
            requested=requested,
            successes=successes,
            failures=failures,
        )
        validation_errors = _receipt_validation_errors(receipt)
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
            _write_runner_state(
                marker_path,
                cycle_id=cycle_id,
                state="failed",
                facts_hash=marker_facts_hash,
                plan_sha256=marker_plan_sha,
                error=f"{type(exc).__name__}: {exc}",
            )
        raise


def execute_position_plan(
    plan: dict[str, Any],
    facts: dict[str, Any],
    *,
    cycle_id: str,
    db_root: Path,
    receipt_file: Path,
    nudge: bool = True,
    plan_sha256: str | None = None,
    state_file: Path | None = None,
    runtime_guard=None,
) -> dict[str, Any]:
    """Execute under a same-cycle cross-process lock and marker CAS."""
    marker_path = (
        Path(state_file)
        if state_file is not None
        else Path(receipt_file).with_name(
            f"live_runner_state_{cycle_id.replace(':', '-')}.json"
        )
    )
    effective_plan_sha = plan_sha256 or _canonical_hash(plan)
    lock_path = _default_runner_lock_file(marker_path, cycle_id)
    with _runner_cycle_lock(lock_path, cycle_id):
        _invoke_runtime_guard(runtime_guard, cycle_id)
        _reject_existing_runner_state(marker_path, cycle_id)
        return _execute_position_plan_locked(
            plan,
            facts,
            cycle_id=cycle_id,
            db_root=db_root,
            receipt_file=receipt_file,
            nudge=nudge,
            plan_sha256=effective_plan_sha,
            state_file=marker_path,
            runtime_guard=runtime_guard,
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
        plan, plan_sha256 = _read_json_with_sha(args.plan_file, "plan-file")
        facts = _read_json(args.facts_file, "facts-file")
        result = execute_position_plan(
            plan,
            facts,
            cycle_id=args.cycle_id,
            db_root=args.db_root,
            receipt_file=args.receipt_file,
            plan_sha256=plan_sha256,
            state_file=_default_runner_state_file(args.cycle_id),
            runtime_guard=lambda cycle: validate_live_runtime_authority(
                cycle,
                db_root=args.db_root,
            ),
        )
        summary = {
            "ok": result["ok"],
            "committed": result["committed"],
            "cycle_id": args.cycle_id,
            "batch_status": result["batch_status"],
            "action_taken": result["receipt"].get("action_taken"),
            "n_orders": result["receipt"].get("n_orders"),
            "requested_actions": len(result["receipt"].get(
                "requested_position_actions") or []),
            "completed_actions": len(result["receipt"].get(
                "position_action_results") or []),
            "failed_actions": len(result["receipt"].get(
                "position_action_failures") or []),
            "receipt_file": str(args.receipt_file),
            "receipt_hash": _canonical_hash(result["receipt"]),
            "writer": result["writer"],
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        if not result["committed"]:
            return 4
        return 0 if result["ok"] else 3
    except PlanError as exc:
        print(json.dumps({
            "ok": False,
            "committed": False,
            "error_kind": "plan_preflight_failed",
            "error": str(exc),
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
