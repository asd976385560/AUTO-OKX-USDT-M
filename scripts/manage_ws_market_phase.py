# -*- coding: utf-8 -*-
"""以成熟审计工件安全推进或回滚 WS 行情阶段。"""
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
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(_public_project_path())
CST = timezone(timedelta(hours=8))
DEFAULT_CONFIG = ROOT / "config" / "ws_market_source.json"
BACKUP_ROOT = ROOT / "backups" / "ws-market-config"
VALID_MODES = {"shadow", "dual_read", "ws_first", "rest_only"}


class PhaseError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PhaseError(f"json_invalid:{path}:{type(exc).__name__}:{exc}") from exc
    if not isinstance(payload, dict):
        raise PhaseError(f"json_root_not_object:{path}")
    return payload


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_audit(
    path: Path,
    *,
    required_mode: str,
    minimum_hours: int,
) -> dict[str, Any]:
    audit = load_json(path)
    window = audit.get("window") or {}
    gates = audit.get("current_gates") or {}
    if audit.get("mode") != required_mode:
        raise PhaseError(
            f"audit_mode_mismatch:{audit.get('mode')}!={required_mode}"
        )
    if audit.get("status") != "PASSED":
        raise PhaseError(f"audit_not_passed:{audit.get('status')}")
    if window.get("mature") is not True:
        raise PhaseError("audit_window_not_mature")
    if int(window.get("required_hours") or 0) < minimum_hours:
        raise PhaseError(
            f"audit_window_too_short:{window.get('required_hours')}<{minimum_hours}"
        )
    failed = sorted(key for key, value in gates.items() if value is not True)
    if failed:
        raise PhaseError("audit_gates_failed:" + ",".join(failed))
    return {
        "path": str(path),
        "sha256": file_sha256(path),
        "generated_at": audit.get("generated_at"),
        "elapsed_hours": window.get("elapsed_hours"),
        "required_hours": window.get("required_hours"),
        "close_p99": (
            (audit.get("candle_window_summary") or {})
            .get("combined_close_latency_seconds", {})
            .get("p99")
        ),
        "recovery_p99": (
            (audit.get("resources") or {}).get("recovery_seconds", {}).get("p99")
        ),
    }


def next_natural_hour(now: datetime) -> datetime:
    local = now.astimezone(CST)
    boundary = local.replace(minute=0, second=0, microsecond=0)
    if local >= boundary:
        boundary += timedelta(hours=1)
    return boundary


def plan_transition(
    config: dict[str, Any],
    *,
    target: str,
    audit_path: Path | None,
    shadow_audit_path: Path | None,
    threshold_registration_path: Path | None,
    now: datetime,
    reason: str,
    restart_window: bool = False,
    force_cutover: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = str(config.get("mode") or "rest_only")
    if current not in VALID_MODES:
        raise PhaseError(f"current_mode_invalid:{current}")
    if target not in VALID_MODES:
        raise PhaseError(f"target_mode_invalid:{target}")
    if target == "rest_only":
        evidence = {"kind": "rollback", "reason": reason}
        activation = now.astimezone(CST)
        next_configured = "rest_only"
        pending = None
    elif current == "dual_read" and target == "dual_read":
        if not restart_window:
            raise PhaseError("dual_read_restart_requires_explicit_flag")
        evidence = {
            "kind": "dual_read_window_restart",
            "reason": reason,
        }
        activation = now.astimezone(CST)
        next_configured = "dual_read"
        pending = None
    elif current == "shadow" and target == "dual_read":
        if audit_path is None:
            raise PhaseError("shadow_audit_required")
        evidence = validate_audit(
            audit_path, required_mode="shadow", minimum_hours=24
        )
        activation = now.astimezone(CST)
        next_configured = "dual_read"
        pending = None
    elif current == "dual_read" and target == "ws_first" and force_cutover:
        if audit_path is None or shadow_audit_path is None:
            raise PhaseError("force_cutover_current_and_shadow_audits_required")
        current_audit = load_json(audit_path)
        if current_audit.get("mode") != "dual_read":
            raise PhaseError(
                f"audit_mode_mismatch:{current_audit.get('mode')}!=dual_read"
            )
        shadow = validate_audit(
            shadow_audit_path, required_mode="shadow", minimum_hours=24
        )
        current_window = current_audit.get("window") or {}
        current_gates = current_audit.get("current_gates") or {}
        evidence = {
            "kind": "explicit_user_force_cutover",
            "reason": reason,
            "waived_requirements": [
                "dual_read_48h_maturity",
                "two_round_threshold_registration",
                "next_natural_hour_activation",
            ],
            "shadow": shadow,
            "dual_read_current": {
                "path": str(audit_path),
                "sha256": file_sha256(audit_path),
                "generated_at": current_audit.get("generated_at"),
                "status": current_audit.get("status"),
                "elapsed_hours": current_window.get("elapsed_hours"),
                "mature": current_window.get("mature"),
                "failed_gates": sorted(
                    key for key, value in current_gates.items() if value is not True
                ),
                "current_gates": current_gates,
            },
        }
        activation = now.astimezone(CST)
        next_configured = "ws_first"
        pending = None
    elif current == "dual_read" and target == "ws_first":
        if audit_path is None or shadow_audit_path is None:
            raise PhaseError("dual_and_shadow_audits_required")
        dual = validate_audit(
            audit_path, required_mode="dual_read", minimum_hours=48
        )
        shadow = validate_audit(
            shadow_audit_path, required_mode="shadow", minimum_hours=24
        )
        if threshold_registration_path is None:
            raise PhaseError("threshold_registration_required")
        registration = load_json(threshold_registration_path)
        if registration.get("status") != "REGISTERED":
            raise PhaseError(
                f"threshold_registration_not_registered:{registration.get('status')}"
            )
        if registration.get("shadow_audit_sha256") != shadow["sha256"]:
            raise PhaseError("threshold_shadow_audit_hash_mismatch")
        if registration.get("dual_read_audit_sha256") != dual["sha256"]:
            raise PhaseError("threshold_dual_audit_hash_mismatch")
        close_limit = float(registration.get("candle_close_threshold_seconds") or 9999)
        recovery_limit = float(registration.get("recovery_threshold_seconds") or 9999)
        if close_limit > 30 or recovery_limit > 120:
            raise PhaseError(
                f"registered_threshold_exceeds_hard_ceiling:{close_limit}/{recovery_limit}"
            )
        evidence = {
            "kind": "two_round_cutover",
            "shadow": shadow,
            "dual_read": dual,
            "threshold_registration": {
                "path": str(threshold_registration_path),
                "sha256": file_sha256(threshold_registration_path),
                "candle_close_threshold_seconds": close_limit,
                "recovery_threshold_seconds": recovery_limit,
            },
        }
        activation = next_natural_hour(now)
        next_configured = "dual_read"
        pending = "ws_first"
    else:
        raise PhaseError(f"transition_not_allowed:{current}->{target}")

    updated = dict(config)
    updated["schema_version"] = max(1, int(updated.get("schema_version") or 1))
    updated["mode"] = next_configured
    updated["pending_mode"] = pending
    updated["activation_boundary"] = activation.isoformat()
    updated["phase_started_at_cst"] = activation.isoformat()
    history = list(updated.get("phase_history") or [])
    history.append(
        {
            "mode": target,
            "configured_mode": next_configured,
            "pending": bool(pending),
            "started_at_cst": activation.isoformat(),
            "recorded_at_cst": now.astimezone(CST).isoformat(),
            "reason": reason,
            "evidence": evidence,
        }
    )
    updated["phase_history"] = history
    summary = {
        "ok": True,
        "current_mode": current,
        "target_mode": target,
        "configured_mode": next_configured,
        "pending_mode": pending,
        "activation_boundary": activation.isoformat(),
        "evidence": evidence,
    }
    return updated, summary


def atomic_write_config(path: Path, payload: dict[str, Any]) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_ROOT / f"ws_market_source-{stamp}.json"
    if path.exists():
        shutil.copy2(path, backup)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return {
        "backup": str(backup) if backup.exists() else "",
        "config_sha256": file_sha256(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="安全推进或回滚WS行情阶段")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument(
        "--target", choices=("dual_read", "ws_first", "rest_only"), required=True
    )
    parser.add_argument("--audit-json")
    parser.add_argument("--shadow-audit-json")
    parser.add_argument("--threshold-registration-json")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--restart-window", action="store_true")
    parser.add_argument("--force-cutover", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config)
    try:
        config = load_json(config_path)
        updated, summary = plan_transition(
            config,
            target=args.target,
            audit_path=Path(args.audit_json) if args.audit_json else None,
            shadow_audit_path=(
                Path(args.shadow_audit_json) if args.shadow_audit_json else None
            ),
            threshold_registration_path=(
                Path(args.threshold_registration_json)
                if args.threshold_registration_json
                else None
            ),
            now=datetime.now(CST),
            reason=args.reason,
            restart_window=bool(args.restart_window),
            force_cutover=bool(args.force_cutover),
        )
        summary["apply"] = bool(args.apply)
        if args.apply:
            summary.update(atomic_write_config(config_path, updated))
        else:
            summary["planned_config"] = updated
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except PhaseError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "production_config_writes": 0,
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
