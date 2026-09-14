# -*- coding: utf-8 -*-
"""Post-cycle reconciliation; optional bounded exact-close recovery.

Standalone calls remain read-only. The supervisor explicitly enables recovery
for forward cycles, after positive writer-finality proof and a released Live lease.
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
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(_public_project_path())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DB_ROOT = Path(os.environ.get("OKX_DB_ROOT") or ROOT / "db")
AUTOHEAL_ACTIVATION_CYCLE = "2026-09-13T22:15"
FINALIZED_FAILURE_RECOVERY_FROM = "2026-09-13T23:15"
RECON = ROOT / "scripts" / "reconcile_exchange_closes.py"
QQ_PUSH = ROOT / "scripts" / "qq_push.py"
LOG_DIR = ROOT / "logs" / "reconcile"
STAGE_STATUS_DIR = Path(
    os.environ.get("OKX_STAGE_STATUS_DIR") or ROOT / "logs" / "stage-status")
CST = timezone(timedelta(hours=8))
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_MARKERS = (
    "[GHOST-EXACT]", "[GHOST-FUZZY]", "[OVER_CLOSED]",
    "[UNRECORDED]", "[LEFTOVER]",
)


def now_cst() -> datetime:
    return datetime.now(CST)


def _findings(out: str, cap: int = 10) -> str:
    # 与 reconcile_daily 的用户可读摘要保持一致，但本脚本独立可运行。
    lines: list[str] = []
    capture_children = False
    for raw in str(out or "").splitlines():
        text = raw.strip()
        if any(marker in text for marker in _MARKERS):
            lines.append(text)
            capture_children = (
                text.startswith("[OVER_CLOSED]")
                or text.startswith("[UNRECORDED]")
            )
            continue
        if capture_children and raw[:1].isspace() and text:
            lines.append(text)
            continue
        capture_children = False
    return " | ".join(lines[:cap]) or "(无分类行，见完整输出)"


def evaluate(rc: int, out: str) -> dict:
    markers = [marker for marker in _MARKERS if marker in str(out or "")]
    issue = rc != 0 or bool(markers)
    if rc == 0 and not markers:
        return {"ok": True, "issue": False, "rc": rc, "markers": []}
    classes = []
    mapping = {
        "[GHOST-EXACT]": "GHOST-EXACT 可补",
        "[GHOST-FUZZY]": "GHOST-FUZZY 含糊",
        "[OVER_CLOSED]": "OVER_CLOSED 缺 open",
        "[UNRECORDED]": "UNRECORDED 缺 open",
        "[LEFTOVER]": "LEFTOVER 未归因成交",
    }
    for marker in markers:
        classes.append(mapping[marker])
    if not classes:
        classes.append("对账执行错误")
    findings = _findings(out)
    if not markers:
        detail = " ".join(str(out or "").strip().split())
        findings = detail[-900:] or "(执行无输出)"
    return {
        "ok": rc in (0, 1, 3),
        "issue": issue,
        "rc": rc,
        "markers": markers,
        "classification": " + ".join(classes),
        "findings": findings,
    }


def active_runner(profile: str) -> dict | None:
    cutoff = now_cst() - timedelta(minutes=45)
    try:
        paths = sorted(
            STAGE_STATUS_DIR.glob(f"{profile}-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:8]
    except OSError:
        return None
    for path in paths:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            started = datetime.strptime(
                str(item.get("started_at") or "")[:19],
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=CST)
        except (OSError, ValueError, TypeError):
            continue
        if item.get("status") == "running" and started >= cutoff:
            return {
                "path": str(path),
                "cycle_id": item.get("cycle_id"),
                "started_at": item.get("started_at"),
            }
    return None


def active_live_runner() -> dict | None:
    """Backward-compatible live probe used by existing diagnostics/tests."""
    return active_runner("live")


def run_reconcile(profile: str = "live") -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [sys.executable, str(RECON), "--profile", profile],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180, creationflags=_CREATE_NO_WINDOW)
        return int(proc.returncode), (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return 99, f"{type(exc).__name__}: {exc}"


def _finalized_failed_writer(cycle: str, status: dict) -> bool:
    """An independently committed failed plan cannot submit further actions.

    Business failure is retained. This grants only the existing close-record
    recovery, still under its exclusive lease and original time budgets.
    """
    if not (cycle >= FINALIZED_FAILURE_RECOVERY_FROM
            and status.get("status") == "failed"
            and status.get("returncode") == 86
            and status.get("failure_kind") == "business_verification_error"):
        return False
    try:
        slug = cycle.replace(":", "-")
        def read(name):
            value = json.loads((ROOT / "tmp" / name).read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("invalid terminal artifact")
            return value
        runner = read(f"live_runner_state_{slug}.json")
        receipt = read(f"_receipt_live_{slug}.json")
        facts = read(f"live_facts_{slug}.json")
        plan_hash = hashlib.sha256((ROOT / "tmp" / f"position_plan_{slug}.json").read_bytes()).hexdigest()
        facts_hash = str(facts.get("facts_hash") or "")
        if not (runner.get("cycle_id") == receipt.get("cycle_id") == facts.get("cycle_id") == cycle
                and runner.get("state") == "committed"
                and receipt.get("profile") == facts.get("profile") == "live"
                and receipt.get("runner_in_progress") is False
                and receipt.get("batch_status") in ("partial", "completed")
                and receipt.get("ok") is True
                and len(facts_hash) == 64
                and runner.get("plan_sha256") == receipt.get("plan_sha256") == plan_hash
                and runner.get("facts_hash") == receipt.get("facts_hash") == facts_hash):
            return False
        with closing(sqlite3.connect((DB_ROOT / "live_trades.db").resolve().as_uri()
                                     + "?mode=ro", uri=True, timeout=3)) as con:
            row = con.execute("SELECT raw FROM trade_cycles WHERE cycle_id=?", (cycle,)).fetchone()
        if not row:
            return False
        saved = json.loads(row[0])
        return bool(isinstance(saved, dict)
                    and saved.get("runner_in_progress") is False
                    and saved.get("plan_sha256") == plan_hash
                    and saved.get("facts_hash") == facts_hash)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return False


def _recovery_live_ready(cycle: str) -> bool:
    """Require a completed writer, released lease and no unresolved intent."""
    status = json.loads((STAGE_STATUS_DIR / (
        "live-" + cycle.replace(":", "-") + ".json")).read_text(encoding="utf-8"))
    finalized = (status.get("status") == "succeeded"
                 and status.get("returncode") == 0
                 and status.get("business_check", {}).get("ok") is True)
    if not (status.get("cycle_id") == cycle
            and (finalized or _finalized_failed_writer(cycle, status))
            and status.get("profile_lease_released") is True
            and active_runner("live") is None):
        return False
    with closing(sqlite3.connect((DB_ROOT / "ledger.db").resolve().as_uri()
                                 + "?mode=ro", uri=True, timeout=3)) as con:
        return con.execute(
            "SELECT COUNT(*) FROM execution_intents WHERE profile='live' "
            "AND (state IS NULL OR state NOT IN ('completed','failed_clean'))").fetchone()[0] == 0


def _recovery_backups(directory: Path) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=False)
    result = []
    for name in ("ledger.db", "live_trades.db", "account.db"):
        source = DB_ROOT / name
        target = directory / name
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=3)) as src:
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
                assert dst.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        result.append({"path": str(target),
                       "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    return result


def _recovery_once(cycle: str, budget: float, *, apply: bool) -> dict:
    from collectors.trigger_agent import _read_autoheal_contract
    if budget <= 0:
        raise TimeoutError("post-push recovery budget exhausted")
    request = uuid.uuid4().hex
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / f"post-push-autoheal-{cycle.replace(':', '-')}-{request}.json"
    command = [sys.executable, str(ROOT / "scripts/ledger_autoheal.py"),
               "--profile", "live", "--db-root", str(DB_ROOT.resolve()),
               "--self-cycle", cycle, "--request-id", request,
               "--json-out", str(out)]
    if apply:
        if (os.environ.get("OKX_DISABLE_LEDGER_AUTOHEAL") == "1"
                or os.environ.get("OKX_LEDGER_AUTOHEAL_APPLY", "1") == "0"):
            raise RuntimeError("ledger recovery disabled")
        command.append("--apply")
    # This insertion point never enables UNRECORDED/open repair.
    proc = subprocess.run(command, cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=budget, creationflags=_CREATE_NO_WINDOW)
    result = _read_autoheal_contract(
        out, request_id=request, profile="live", cycle_id=cycle,
        db_root=DB_ROOT, returncode=int(proc.returncode))
    result["json_out"] = str(out)
    return result


def recover_exact_after_push(cycle: str, initial: dict,
                             *, monitor_started: float) -> dict:
    """Use the original owner under the dispatcher lease; never replay orders."""
    from collectors import ledger
    from scripts.ledger_recovery import recover_in_budget
    from scripts import _acceptance_thresholds as thresholds
    result = {"status": "not_attempted", "recovered": False,
              "exchange_writes": 0, "unrecorded_write_enabled": False}
    if (initial.get("rc") != 1 or initial.get("markers") != ["[GHOST-EXACT]"]
            or not cycle or cycle < AUTOHEAL_ACTIVATION_CYCLE):
        return {**result, "reason": "not_forward_pure_exact_close"}
    if (os.environ.get("OKX_DISABLE_LEDGER_AUTOHEAL") == "1"
            or os.environ.get("OKX_LEDGER_AUTOHEAL_APPLY", "1") == "0"):
        return {**result, "reason": "ledger_recovery_disabled"}
    owner = "post-push-recovery-" + uuid.uuid4().hex
    acquired = False
    def budget_left() -> float:
        start = datetime.strptime(cycle, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
        stop = start + timedelta(seconds=thresholds.sla_business_terminal_deadline_seconds(cycle))
        # Preserve the 240s monitor guard and 65s for notification/terminal I/O.
        return max(0.0, min(180.0, (stop-now_cst()).total_seconds(),
                           175.0-(time.monotonic()-monitor_started)))
    try:
        if budget_left() < 60 or not _recovery_live_ready(cycle):
            return {**result, "reason": "writer_not_ready_or_budget_insufficient"}
        acquired = ledger.try_profile_lease(DB_ROOT / "ledger.db", "live", owner,
                                            ttl_sec=240)
        if not acquired:
            return {**result, "reason": "profile_lease_busy"}
        if not _recovery_live_ready(cycle) or budget_left() < 60:
            result["reason"] = "writer_or_budget_changed"
            return result
        directory = ROOT / "backups" / owner
        result["backups"] = _recovery_backups(directory)
        budget = budget_left()
        if budget < 60:
            result["reason"] = "backup_consumed_recovery_budget"
            return result
        contract = recover_in_budget(
            lambda remaining: _recovery_once(cycle, remaining, apply=True),
            verify_once=lambda remaining: _recovery_once(cycle, remaining, apply=False),
            cycle=cycle, timeout_sec=budget, now=now_cst())
        chain = contract.get("recovery_chain", {})
        clean = (contract.get("status") == "ok" and contract.get("rc") == 0
                 and contract.get("blocking") is False and contract.get("p0") is False
                 and contract.get("unrecorded_count") == 0
                 and not contract.get("findings"))
        result.update(status="completed", contract=contract,
                      recovered=bool(clean and (
                          chain.get("verified_after_write") is True
                          or (chain.get("applied_any") is False
                              and contract.get("applied") is False))),
                      reason=chain.get("stop_reason"))
    except Exception as exc:
        result.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
    finally:
        if acquired:
            try:
                result["lease_released"] = ledger.release_profile_lease(
                    DB_ROOT / "ledger.db", "live", owner)
            except Exception as exc:
                result.update(recovered=False, lease_released=False,
                              release_error=f"{type(exc).__name__}: {exc}")
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / f"{owner}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


from collectors.cycle_contract import validate_cycle_id

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", default="")
    ap.add_argument("--profile", choices=("live",), default="live")
    ap.add_argument("--dry-run", action="store_true",
                    help="只检测和打印，不写告警文件、不推 QQ")
    ap.add_argument("--autoheal-exact", action="store_true",
                    help="监督入口：执行终态已证且租约释放后，在原预算内恢复精确平仓账差")
    args = ap.parse_args()
    try:
        args.cycle = validate_cycle_id(args.cycle)
    except ValueError as exc:
        ap.error(str(exc))
    monitor_started = time.monotonic()

    active = active_runner(args.profile)
    if active:
        print(json.dumps({
            "ok": True,
            "issue": False,
            "skipped": f"{args.profile}_runner_active",
            "active": active,
            "cycle_id": args.cycle or None,
        }, ensure_ascii=False, indent=1))
        return 0

    rc, out = run_reconcile(args.profile)
    report = {
        "ts": now_cst().strftime("%Y-%m-%d %H:%M:%S"),
        "cycle_id": args.cycle or None,
        "profile": args.profile,
        **evaluate(rc, out),
    }
    if not report["issue"]:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    if args.autoheal_exact and not args.dry_run:
        report["recovery"] = recover_exact_after_push(
            args.cycle, report, monitor_started=monitor_started)
        if report["recovery"].get("recovered") is True:
            report.update(issue=False, rc=0, recovered=True)

    recovered = report.get("recovered") is True
    title = "✅ 日内账实已恢复" if recovered else "⚠️ 日内账实对账告警 [P1]"
    text = (
        f"{title} {args.profile.upper()} "
        f"[{now_cst():%Y-%m-%d %H:%M}]"
        f" (统一QQ告警)\n"
        f"· cycle={args.cycle or 'post-cycle'} rc={rc} "
        f"{report.get('classification', '')}\n"
        f"· {report.get('findings')}\n"
        + ("· 处置：账仓已独立复核一致，所需补账仅经原writer；未重放交易。\n"
           if recovered else
           "· 处置：差异尚未消除；可证精确平仓仅在执行终态有证据、租约已释放且预算充足时自动补账，"
           "其余保留阻断与原始证据。\n")
    )
    if args.dry_run:
        report["alert"] = {"dry_run": True, "would_send": text}
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 1

    digest = hashlib.sha256(
        (str(report.get("classification")) + "|" + str(report.get("findings")))
        .encode("utf-8")
    ).hexdigest()[:20]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    alert_file = (
        LOG_DIR / f"{args.profile}_monitor_{now_cst():%Y%m%d_%H%M%S}.txt")
    alert_file.write_text(text, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(QQ_PUSH), "--content-file", str(alert_file),
             "--alert",  # 告警走 C2C 私聊，不混进业务播报群（2026-08-04）
             "--dedupe-key", f"{args.profile}-reconcile-monitor{'-recovered' if recovered else ''}:{digest}"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, creationflags=_CREATE_NO_WINDOW)
        report["alert"] = {
            "rc": int(proc.returncode),
            "dedupe_key": f"{args.profile}-reconcile-monitor{'-recovered' if recovered else ''}:{digest}",
        }
    except Exception as exc:
        report["alert"] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if recovered else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
