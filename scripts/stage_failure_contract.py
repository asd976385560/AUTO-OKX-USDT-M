# -*- coding: utf-8 -*-
"""Minimal, read-only contract for a terminal upstream failure report.

The contract deliberately exposes no prompt, response, provider, model, or
fallback-chain details.  It proves either that the supervised live stage reached
an immutable failed terminal or that collection failed before any Agent or
executor path existed, each behind its own future-only activation boundary.
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import json
import hashlib
import importlib.util
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _ledger():
    """惰性加载 collectors/ledger 的绝对路径（唯一事实源）。

    2026-08-19 G6：必需源集合与完成状态只有一个真源。本模块曾内联手写过一份
    （``required={"fast"}`` / ``{"ok","degraded"}``），值虽一致但属未爆的双份
    真相 —— 而这段代码决定「能否发 collection_gate_failed 的 WAIT 报告」。
    本模块是只读契约，取不到权威定义就抛，让调用方失败关闭。
    2026-08-21 实盘 07:00 证明双导入 fallback 仍会用第二个异常覆盖第一因；
    现按本文件位置加载唯一权威文件，不再依赖 cwd / PYTHONPATH。
    """
    path = Path(__file__).resolve().parents[1] / "collectors" / "ledger.py"
    spec = importlib.util.spec_from_file_location(
        "_okx_authoritative_collection_ledger", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load authoritative ledger: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for required in ("expected_sources", "DONE_STATUS", "is_failure_status"):
        if not hasattr(mod, required):
            raise ImportError(
                f"authoritative ledger missing {required}: {path}")
    return mod


CST = timezone(timedelta(hours=8))
DEFAULT_STATUS_DIR = Path(_public_project_path('logs', 'stage-status'))
DEFAULT_COLLECT_LOG_DIR = Path(_public_project_path('logs', 'collect'))
FAILURE_REPORT_ACTIVATION_CYCLE = "2026-08-13T04:00"
REPORT_RECONCILE_BARRIER_FROM = "2026-08-14T19:00"
COLLECTION_FAILURE_REPORT_ACTIVATION_CYCLE = "2026-08-15T13:00"
_CYCLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)$")
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,79}$")
_REQUEST_RE = re.compile(r"^[a-f0-9-]{16,64}$")


def _cycle_time(value: str) -> datetime:
    if not _CYCLE_RE.fullmatch(str(value)):
        raise ValueError("cycle must be CST YYYY-MM-DDTHH:00|15|30|45")
    return datetime.strptime(str(value), "%Y-%m-%dT%H:%M").replace(tzinfo=CST)


def _terminal_time(value: object) -> datetime:
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=CST)


def status_path(cycle: str, status_dir: Path | str = DEFAULT_STATUS_DIR) -> Path:
    _cycle_time(cycle)
    return Path(status_dir) / f"live-{cycle.replace(':', '-')}.json"


def _report_barrier_from_status(raw: dict, cycle: str) -> dict | None:
    """Validate the post-Agent exchange/ledger read before report release."""
    barrier = raw.get("report_reconcile_barrier")
    if not isinstance(barrier, dict):
        return None
    try:
        stage_finished = _terminal_time(raw.get("finished_at"))
        started_at = _terminal_time(barrier.get("started_at"))
        finished_at = _terminal_time(barrier.get("finished_at"))
        rc = int(barrier.get("rc"))
    except (TypeError, ValueError):
        return None
    if (
        barrier.get("schema_version") != 1
        or barrier.get("required") is not True
        or barrier.get("profile") != "live"
        or barrier.get("cycle_id") != cycle
        or barrier.get("contract_version") != 1
        or not _REQUEST_RE.fullmatch(str(barrier.get("request_id") or ""))
        or barrier.get("contract_valid") is not True
        or barrier.get("report_safe") is not True
        or barrier.get("status") not in {"ok", "applied"}
        or rc != 0
        or barrier.get("blocking") is not False
        or barrier.get("p0") is not False
        or type(barrier.get("applied")) is not bool
        or type(barrier.get("findings_count")) is not int
        or type(barrier.get("healed_count")) is not int
        or barrier.get("findings_count") < 0
        or barrier.get("healed_count") < 0
        or started_at < stage_finished
        or finished_at < started_at
    ):
        return None
    return {
        "schema_version": 1,
        "required": True,
        "profile": "live",
        "cycle_id": cycle,
        "status": barrier["status"],
        "rc": 0,
        "applied": barrier["applied"],
        "blocking": False,
        "p0": False,
        "contract_valid": True,
        "report_safe": True,
        "started_at": barrier["started_at"],
        "finished_at": barrier["finished_at"],
        "findings_count": barrier["findings_count"],
        "healed_count": barrier["healed_count"],
    }


def load_live_report_barrier(
    cycle: str,
    *,
    status_dir: Path | str = DEFAULT_STATUS_DIR,
    activation_cycle: str = REPORT_RECONCILE_BARRIER_FROM,
) -> dict | None:
    """Return a safe report-release barrier, or ``None`` when not proved."""
    try:
        if _cycle_time(cycle) < _cycle_time(activation_cycle):
            return {
                "schema_version": 1,
                "required": False,
                "activation_cycle": activation_cycle,
            }
        raw = json.loads(status_path(cycle, status_dir).read_text(
            encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or (
        raw.get("stage") != "live"
        or raw.get("cycle_id") != cycle
        or raw.get("status") not in {"succeeded", "failed"}
        or raw.get("profile_lease_released") is not True
    ):
        return None
    return _report_barrier_from_status(raw, cycle)


def load_live_failure(
    cycle: str,
    *,
    status_dir: Path | str = DEFAULT_STATUS_DIR,
    now: datetime | None = None,
    activation_cycle: str = FAILURE_REPORT_ACTIVATION_CYCLE,
) -> dict | None:
    """Return a redacted terminal contract or ``None`` when not eligible."""
    try:
        cycle_at = _cycle_time(cycle)
        activation_at = _cycle_time(activation_cycle)
    except (TypeError, ValueError):
        return None
    if cycle_at < activation_at:
        return None
    try:
        raw = json.loads(status_path(cycle, status_dir).read_text(
            encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    if (
        raw.get("stage") != "live"
        or raw.get("cycle_id") != cycle
        or raw.get("status") != "failed"
        or raw.get("mode") not in {"unified", "full"}
        or raw.get("profile_lease_released") is not True
    ):
        return None
    try:
        barrier_required = cycle_at >= _cycle_time(
            REPORT_RECONCILE_BARRIER_FROM)
    except (TypeError, ValueError):
        return None
    report_barrier = (
        _report_barrier_from_status(raw, cycle)
        if barrier_required else {
            "schema_version": 1,
            "required": False,
            "activation_cycle": REPORT_RECONCILE_BARRIER_FROM,
        }
    )
    if report_barrier is None:
        return None
    try:
        child_rc = int(raw.get("child_returncode"))
        return_code = int(raw.get("returncode"))
        started_at = _terminal_time(raw.get("started_at"))
        finished_at = _terminal_time(raw.get("finished_at"))
    except (TypeError, ValueError):
        return None
    # 一个同名文件也必须证明它确实属于该周期之后启动的监督进程；
    # 否则拒绝把陈旧/错位终态包装成本轮失败报告。
    if (
        return_code == 0
        or started_at < cycle_at
        or finished_at < started_at
    ):
        return None
    observed_now = now or datetime.now(CST)
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=CST)
    else:
        observed_now = observed_now.astimezone(CST)
    if finished_at > observed_now + timedelta(seconds=60):
        return None

    kind = str(raw.get("failure_kind") or "").strip().lower()
    if not _KIND_RE.fullmatch(kind):
        kind = (
            "agent_process_failed"
            if child_rc != 0
            else "business_output_missing"
        )
    return {
        "stage": "live",
        "cycle_id": cycle,
        "mode": raw["mode"],
        "status": "failed",
        "failure_kind": kind,
        "child_returncode": child_rc,
        "returncode": return_code,
        "started_at": raw["started_at"],
        "finished_at": raw["finished_at"],
        "profile_lease_released": True,
        "report_reconcile_barrier": report_barrier,
        # A terminal supervisor file alone cannot prove the absence of local
        # execution writes or exchange side effects.  The db-root-aware
        # load_upstream_failure path binds those facts before Push may proceed.
        "side_effect_proof": "not_proven",
        "production_database_writes": None,
        "orders_placed": None,
    }


def require_live_failure(
    cycle: str,
    *,
    status_dir: Path | str = DEFAULT_STATUS_DIR,
    now: datetime | None = None,
    activation_cycle: str = FAILURE_REPORT_ACTIVATION_CYCLE,
) -> dict:
    result = load_live_failure(
        cycle,
        status_dir=status_dir,
        now=now,
        activation_cycle=activation_cycle,
    )
    if result is None:
        raise RuntimeError(
            "live failure status is missing, nonterminal, unsafe, or before activation")
    return result


def _readonly_row(
    db_path: Path,
    sql: str,
    params: tuple[object, ...],
) -> sqlite3.Row | None:
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, params).fetchone()
    finally:
        connection.close()


def _live_execution_path_absence(
    cycle: str,
    db_root: Path | str,
) -> dict | None:
    """Prove no local trade/intent/journal path existed for a failed live slot."""
    root = Path(db_root)
    checks: list[dict] = []
    try:
        live_db = root / "live_trades.db"
        ledger_db = root / "ledger.db"
        if not live_db.is_file() or not ledger_db.is_file():
            return None
        for table in ("trade_cycles", "trades"):
            found = _readonly_row(
                live_db,
                f"SELECT 1 FROM {table} WHERE cycle_id=? LIMIT 1",
                (cycle,),
            ) is not None
            checks.append({
                "db": "live_trades.db", "table": table, "found": found,
            })
        intent_table_present = _readonly_row(
            ledger_db,
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("execution_intents",),
        ) is not None
        intent_found = (
            _readonly_row(
                ledger_db,
                "SELECT 1 FROM execution_intents WHERE cycle_id=? LIMIT 1",
                (cycle,),
            ) is not None
            if intent_table_present else False
        )
        checks.append({
            "db": "ledger.db", "table": "execution_intents",
            "found": intent_found, "table_present": intent_table_present,
        })

        journal_path = root / "journal" / "exec_live.jsonl"
        journal_found = False
        if journal_path.exists():
            if not journal_path.is_file() or journal_path.stat().st_size > 64 * 1024 * 1024:
                return None
            for line in journal_path.read_text(
                    encoding="utf-8", errors="strict").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    return None
                if str(row.get("cycle_id") or row.get("cycle") or "") == cycle:
                    journal_found = True
                    break
        checks.append({
            "path": "journal/exec_live.jsonl", "found": journal_found,
        })
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, sqlite3.Error):
        return None
    if any(item["found"] for item in checks):
        return None
    return {
        "ok": True,
        "status": "proved_absent",
        "checks": checks,
    }


def _collection_receipt(
    cycle: str,
    *,
    collect_log_dir: Path | str,
) -> tuple[dict, str] | None:
    """Return the unique natural terminal receipt, never a duplicate guard row."""
    cycle_at = _cycle_time(cycle)
    path = Path(collect_log_dir) / (
        "collect_cycle_" + cycle_at.strftime("%Y%m%d") + ".jsonl")
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        return None
    matches: list[tuple[dict, str]] = []
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict) or raw.get("cycle") != cycle:
            continue
        if raw.get("duplicate_skip") is not None:
            continue
        matches.append((raw, stripped))
    # Natural collection is single-shot.  Multiple terminal receipts are
    # ambiguous/manual replay evidence and must not authorize a report.
    return matches[0] if len(matches) == 1 else None


def load_collection_failure(
    cycle: str,
    *,
    db_root: Path | str,
    collect_log_dir: Path | str = DEFAULT_COLLECT_LOG_DIR,
    now: datetime | None = None,
    activation_cycle: str = COLLECTION_FAILURE_REPORT_ACTIVATION_CYCLE,
) -> dict | None:
    """Prove a terminal collection-gate failure with no execution path.

    The receipt is only an initial terminal signal.  The ledger, dispatch
    latch, profile lease, analysis database, and live trade database are then
    re-read independently.  A late successful required source or any evidence
    that an Agent/executor path existed rejects this synthetic WAIT report.
    """
    try:
        cycle_at = _cycle_time(cycle)
        if cycle_at < _cycle_time(activation_cycle):
            return None
        receipt = _collection_receipt(
            cycle, collect_log_dir=collect_log_dir)
        if receipt is None:
            return None
        raw, receipt_text = receipt
        expected_tier = "hourly" if cycle.endswith(":00") else "quarter"
        failed = raw.get("failed")
        steps = raw.get("steps")
        if (
            raw.get("ok") is not False
            or raw.get("tier") != expected_tier
            or not isinstance(failed, list)
            or not failed
            or not all(
                isinstance(item, str) and _KIND_RE.fullmatch(item)
                for item in failed
            )
            or not isinstance(steps, list)
        ):
            return None
        failed_steps = {
            str(item.get("name"))
            for item in steps
            if isinstance(item, dict) and item.get("ok") is False
        }
        if not set(failed).issubset(failed_steps):
            return None
        latency_ms = raw.get("latency_ms")
        if (
            type(latency_ms) is not int
            or latency_ms <= 0
            or latency_ms > 30 * 60 * 1000
        ):
            return None
        started_at = _terminal_time(raw.get("ts"))
        finished_at = started_at + timedelta(milliseconds=latency_ms)
        if started_at < cycle_at or started_at > cycle_at + timedelta(minutes=2):
            return None
        observed_now = now or datetime.now(CST)
        if observed_now.tzinfo is None:
            observed_now = observed_now.replace(tzinfo=CST)
        else:
            observed_now = observed_now.astimezone(CST)
        if finished_at > observed_now + timedelta(seconds=60):
            return None

        root = Path(db_root)
        ledger_path = root / "ledger.db"
        connection = sqlite3.connect(
            f"file:{ledger_path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT source,status FROM collection_runs WHERE cycle_id=?",
                (cycle,),
            ).fetchall()
            # G6：必需源集合与完成状态一律取自 collectors/ledger，禁本地重写。
            _led = _ledger()
            required = set(_led.expected_sources(cycle))
            _done = {str(s).lower() for s in _led.DONE_STATUS}
            ready = {
                str(row["source"])
                for row in rows
                if str(row["status"] or "").strip().lower() in _done
            }
            missing = sorted(required - ready)
            if not missing:
                return None
            dispatched = connection.execute(
                "SELECT stage FROM stage_dispatch WHERE cycle_id=? "
                "AND stage IN ('analyst','live') LIMIT 1",
                (cycle,),
            ).fetchone()
            active_lease = connection.execute(
                "SELECT cycle_id FROM stage_profile_leases "
                "WHERE profile='live' AND cycle_id=? LIMIT 1",
                (cycle,),
            ).fetchone()
        finally:
            connection.close()
        if dispatched is not None or active_lease is not None:
            return None

        analysis_found = _readonly_row(
            root / "analysis.db",
            "SELECT 1 FROM analysis_runs WHERE cycle_id=? LIMIT 1",
            (cycle,),
        ) is not None
        trade_cycle_found = _readonly_row(
            root / "live_trades.db",
            "SELECT 1 FROM trade_cycles WHERE cycle_id=? LIMIT 1",
            (cycle,),
        ) is not None
        trade_found = _readonly_row(
            root / "live_trades.db",
            "SELECT 1 FROM trades WHERE cycle_id=? LIMIT 1",
            (cycle,),
        ) is not None
        if analysis_found or trade_cycle_found or trade_found:
            return None

        finished_text = finished_at.strftime("%Y-%m-%d %H:%M:%S")
        request_id = hashlib.sha256(
            f"collection-failure-report|{cycle}|{finished_text}".encode("utf-8")
        ).hexdigest()[:32]
        report_barrier = {
            "schema_version": 1,
            "required": True,
            "profile": "live",
            "cycle_id": cycle,
            "contract_version": 1,
            "request_id": request_id,
            "status": "ok",
            "rc": 0,
            "applied": False,
            "blocking": False,
            "p0": False,
            "contract_valid": True,
            "report_safe": True,
            "started_at": finished_text,
            "finished_at": finished_text,
            "findings_count": len(missing),
            "healed_count": 0,
            "evidence_kind": "collection_terminal_and_execution_path_absence",
        }
        return {
            "stage": "collection",
            "cycle_id": cycle,
            "mode": expected_tier,
            "status": "failed",
            "failure_kind": "collection_gate_failed",
            "child_returncode": 1,
            "returncode": 1,
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": finished_text,
            "profile_lease_released": True,
            "same_cycle_live_dispatched": False,
            "failed_steps": sorted(set(failed)),
            "missing_required_sources": missing,
            "collection_latency_ms": latency_ms,
            "collection_receipt_sha256": hashlib.sha256(
                receipt_text.encode("utf-8")).hexdigest(),
            "business_check": {
                "ok": True,
                "checks": [
                    {"db": "analysis.db", "table": "analysis_runs", "found": False},
                    {"db": "live_trades.db", "table": "trade_cycles", "found": False},
                    {"db": "live_trades.db", "table": "trades", "found": False},
                ],
            },
            "report_reconcile_barrier": report_barrier,
            "production_database_writes": 0,
            "orders_placed": 0,
        }
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None


def load_upstream_failure(
    cycle: str,
    *,
    db_root: Path | str,
    status_dir: Path | str = DEFAULT_STATUS_DIR,
    collect_log_dir: Path | str = DEFAULT_COLLECT_LOG_DIR,
    now: datetime | None = None,
) -> dict | None:
    """Prefer an actual live failure; otherwise prove collection-path absence."""
    live = load_live_failure(
        cycle, status_dir=status_dir, now=now)
    if live is not None:
        proof = _live_execution_path_absence(cycle, db_root)
        if proof is None:
            return None
        return {
            **live,
            "side_effect_proof": "proved_absent",
            "business_check": proof,
            "production_database_writes": 0,
            "orders_placed": 0,
        }
    return load_collection_failure(
        cycle,
        db_root=db_root,
        collect_log_dir=collect_log_dir,
        now=now,
    )


def require_upstream_failure(
    cycle: str,
    *,
    db_root: Path | str,
    status_dir: Path | str = DEFAULT_STATUS_DIR,
    collect_log_dir: Path | str = DEFAULT_COLLECT_LOG_DIR,
    now: datetime | None = None,
) -> dict:
    result = load_upstream_failure(
        cycle,
        db_root=db_root,
        status_dir=status_dir,
        collect_log_dir=collect_log_dir,
        now=now,
    )
    if result is None:
        raise RuntimeError(
            "upstream failure is missing, nonterminal, unsafe, or before activation")
    return result
