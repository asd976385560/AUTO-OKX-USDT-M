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
import hashlib
import importlib.util as ilu
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_COLLECTORS_DIR = _PROJECT_ROOT / "collectors"
if str(_COLLECTORS_DIR) not in sys.path:
    sys.path.insert(0, str(_COLLECTORS_DIR))

import _proc  # 子进程超时整树杀（详见模块 docstring）
from cycle_contract import validate_cycle_id
from stage_failure_contract import (
    load_live_report_barrier,
    require_upstream_failure,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
OKX = r"."
SCRIPTS = r".\scripts"
WRAP = r".\scripts\run_okx_python.ps1"
# 绝对 pwsh 路径——对齐 okx-* cron（cron 进程 PATH 不保证有 pwsh）；env 可覆盖
PWSH = os.environ.get("OKX_PWSH_BIN", r"C:\Program Files\PowerShell\7\pwsh.exe")
# 组装器（2026-07-07 已迁 scripts/）；env 可覆盖
BUILD_PY = os.environ.get("OKX_BUILD_PY", r".\scripts\build_push_payload.py")
WORK = Path(r".\tmp\push_pipeline")
REPORT_DIR = Path(r".\reports\push")
RUNLOG = Path(r".\logs\push\pipeline_runs.jsonl")
STAGE_STATUS_DIR = Path(os.environ.get(
    "OKX_STAGE_STATUS_DIR", r".\logs\stage-status"))
BUSINESS_ATTESTATION_REQUIRED_FROM = "2026-08-14T07:00"
INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM = "2026-08-15T08:00"
INTER_REPORT_WINDOW_MINUTES = 15
INTER_REPORT_RECONCILE_SOURCES = {
    "exchange_fills_reconcile",
    "execution_journal_recovery",
}
INTER_REPORT_DIRECT_FILL_SOURCE = "fills"
INTER_REPORT_DIRECT_TS_SOURCE = "fills.fillTime"
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


def _canonical_hash(value: dict) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_trade_identity(row: sqlite3.Row) -> dict:
    def number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return {
        "id": int(row["id"]),
        "ts": str(row["ts"] or ""),
        "symbol": str(row["symbol"] or ""),
        "action": str(row["action"] or "").lower(),
        "side": str(row["side"] or "").lower(),
        "sz": number(row["sz"]),
        "fill_px": number(row["fill_px"]),
        "pnl": number(row["pnl"]),
    }


def _inter_report_window(cycle: str) -> tuple[str, str]:
    try:
        end = datetime.strptime(str(cycle), "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid report cycle for inter-report fills") from exc
    start = end - timedelta(minutes=INTER_REPORT_WINDOW_MINUTES)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _stable_inter_report_exchange_identity(
    row: sqlite3.Row,
) -> dict | None:
    try:
        raw = json.loads(row["raw"] or "{}")
    except (TypeError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        return None
    candidates = raw.get("ord_ids")
    if not isinstance(candidates, list):
        candidates = []
    candidates = [
        *candidates,
        raw.get("ord_id"),
        raw.get("ordId"),
        *[
            fill.get("ordId") or fill.get("ord_id")
            for fill in (raw.get("fills") or [])
            if isinstance(fill, dict)
        ],
    ]
    ord_ids = sorted({
        str(value).strip() for value in candidates
        if str(value or "").strip()
    })
    reconcile_source = str(raw.get("reconcile_source") or "").strip()
    if reconcile_source in INTER_REPORT_RECONCILE_SOURCES:
        proof_source = reconcile_source
    elif (
        str(raw.get("fill_source") or "").strip()
        == INTER_REPORT_DIRECT_FILL_SOURCE
        and str(raw.get("ts_source") or "").strip()
        == INTER_REPORT_DIRECT_TS_SOURCE
        and ord_ids
    ):
        proof_source = INTER_REPORT_DIRECT_FILL_SOURCE
    else:
        return None

    def number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return {
        "id": int(row["id"]),
        "original_cycle_id": str(row["cycle_id"] or ""),
        "ts": str(row["ts"] or ""),
        "symbol": str(row["symbol"] or ""),
        "action": str(row["action"] or "").lower(),
        "side": str(row["side"] or "").lower(),
        "sz": number(row["sz"]),
        "fill_px": number(row["fill_px"]),
        "pnl": number(row["pnl"]),
        "reconcile_source": proof_source,
        "ord_ids": ord_ids,
    }


def _current_inter_report_exchange_attestation(
    db_root: str, cycle: str, *, profile: str = "live"
) -> dict:
    """Independently re-read the half-open exchange-fill report interval."""
    start, end = _inter_report_window(cycle)
    db_path = Path(db_root) / f"{profile}_trades.db"
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id,cycle_id,ts,symbol,action,side,sz,fill_px,pnl,raw "
            "FROM trades WHERE ts>? AND ts<=? "
            "AND (cycle_id IS NULL OR cycle_id!=?) ORDER BY ts,id",
            (start, end, cycle),
        ).fetchall()
    finally:
        connection.close()
    fills = []
    for row in rows:
        identity = _stable_inter_report_exchange_identity(row)
        if identity is not None:
            fills.append(identity)
    body = {
        "schema_version": 1,
        "profile": profile,
        "cycle_id": str(cycle),
        "window_start_exclusive_cst": start,
        "window_end_inclusive_cst": end,
        "fill_count": len(fills),
        "fills": fills,
    }
    body["sha256"] = _canonical_hash(body)
    return body


def _verify_inter_report_exchange_attestation(
    payload: dict, db_root: str, cycle: str
) -> dict:
    if str(cycle) < INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM:
        return {
            "inter_report_exchange_required": False,
            "inter_report_exchange_activation_cycle": (
                INTER_REPORT_EXCHANGE_ATTESTATION_REQUIRED_FROM),
        }
    expected = payload.get("inter_report_exchange_attestation")
    if not isinstance(expected, dict):
        raise ValueError("payload inter-report exchange attestation missing")
    current = _current_inter_report_exchange_attestation(db_root, cycle)
    if expected != current:
        raise ValueError(
            "inter-report exchange fill set changed after build")
    return {
        "inter_report_exchange_required": True,
        "inter_report_exchange_schema_version": current["schema_version"],
        "inter_report_fill_count": current["fill_count"],
        "inter_report_sha256": current["sha256"],
        "inter_report_window_start_exclusive_cst": (
            current["window_start_exclusive_cst"]),
        "inter_report_window_end_inclusive_cst": (
            current["window_end_inclusive_cst"]),
    }


def _current_business_attestation(
    db_root: str,
    cycle: str,
    *,
    upstream_failure_report: bool,
    failure_kind: str | None = None,
) -> dict:
    """Re-read the authoritative terminal/fills immediately before send."""
    db_path = Path(db_root) / "live_trades.db"
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        cycle_row = connection.execute(
            "SELECT decision,n_orders FROM trade_cycles WHERE cycle_id=?",
            (cycle,),
        ).fetchone()
        trade_rows = connection.execute(
            "SELECT id,ts,symbol,action,side,sz,fill_px,pnl FROM trades "
            "WHERE cycle_id=? ORDER BY id",
            (cycle,),
        ).fetchall()
    finally:
        connection.close()

    if upstream_failure_report:
        if cycle_row is not None or trade_rows:
            raise ValueError(
                "failure report blocked by late business terminal or fill")
        # A zero-fill failure report is safe only while every same-cycle
        # execution intent remains pristine failed_clean.  Re-read this
        # independently both before archive and immediately before send: an
        # adjust/open/close submitted after build must never be hidden behind
        # the words "exchange side effect = none".
        intent_path = Path(db_root) / "ledger.db"
        intent_connection = sqlite3.connect(
            f"file:{intent_path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=10,
        )
        intent_connection.row_factory = sqlite3.Row
        try:
            intents = intent_connection.execute(
                "SELECT state,ord_id,submitted_at,completed_at "
                "FROM execution_intents WHERE profile='live' AND cycle_id=?",
                (cycle,),
            ).fetchall()
        finally:
            intent_connection.close()
        unsafe = [
            row for row in intents
            if (
                str(row["state"] or "").strip().lower() != "failed_clean"
                or row["ord_id"] not in (None, "")
                or row["submitted_at"] not in (None, "")
                or row["completed_at"] not in (None, "")
            )
        ]
        if unsafe:
            states = sorted({
                str(row["state"] or "<missing>") for row in unsafe})
            raise ValueError(
                "failure report blocked by non-clean execution intents: "
                + ",".join(states))
        if not str(failure_kind or "").strip():
            raise ValueError("failure report terminal failure_kind missing")
        body = {
            "schema_version": 1,
            "profile": "live",
            "cycle_id": str(cycle),
            "terminal": "absent",
            "trade_count": 0,
            "failure_kind": str(failure_kind),
            "intent_rows": len(intents),
            "failed_clean_rows": len(intents),
            "unsafe_rows": 0,
        }
        body["sha256"] = _canonical_hash(body)
        return body

    if cycle_row is None:
        raise ValueError("business terminal disappeared before send")
    trades = [_stable_trade_identity(row) for row in trade_rows]
    body = {
        "schema_version": 1,
        "profile": "live",
        "cycle_id": str(cycle),
        "decision": str(cycle_row["decision"] or "").strip().lower(),
        "n_orders": int(cycle_row["n_orders"]),
        "trade_count": len(trades),
        "trades": trades,
    }
    body["sha256"] = _canonical_hash(body)
    return body


def _collection_failure_identity(context: dict) -> dict:
    """Stable fields that must not drift between build/archive/send."""
    return {
        "stage": context.get("stage"),
        "cycle_id": context.get("cycle_id"),
        "mode": context.get("mode"),
        "status": context.get("status"),
        "failure_kind": context.get("failure_kind"),
        "started_at": context.get("started_at"),
        "finished_at": context.get("finished_at"),
        "returncode": context.get("returncode"),
        "same_cycle_live_dispatched": context.get(
            "same_cycle_live_dispatched"),
        "failed_steps": list(context.get("failed_steps") or []),
        "missing_required_sources": list(
            context.get("missing_required_sources") or []),
        "collection_latency_ms": context.get("collection_latency_ms"),
        "collection_receipt_sha256": context.get(
            "collection_receipt_sha256"),
    }


def _verify_live_stage_terminal(
    cycle: str,
    db_root: str,
    *,
    wait_seconds: float = 0.0,
    upstream_failure_report: bool = False,
    expected_failure_context: dict | None = None,
) -> dict:
    """Prove the same-cycle execution path can no longer append output."""
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    last_error: Exception | None = None
    while True:
        try:
            if upstream_failure_report:
                failure = require_upstream_failure(
                    cycle,
                    db_root=db_root,
                    status_dir=STAGE_STATUS_DIR,
                )
                if failure.get("stage") == "collection":
                    if (
                        not isinstance(expected_failure_context, dict)
                        or _collection_failure_identity(failure)
                        != _collection_failure_identity(
                            expected_failure_context)
                    ):
                        raise ValueError(
                            "collection failure receipt or gate proof drifted "
                            "during terminal verification")
                    return {
                        "stage": "collection",
                        "status": "failed",
                        "returncode": 1,
                        "finished_at": str(failure["finished_at"]),
                        "profile_lease_released": True,
                        "same_cycle_active_lease": False,
                        "report_reconcile_barrier": dict(
                            failure["report_reconcile_barrier"]),
                    }
            status_path = (
                STAGE_STATUS_DIR
                / f"live-{cycle.replace(':', '-')}.json")
            raw = json.loads(status_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("live stage status is not an object")
            if (
                raw.get("stage") != "live"
                or raw.get("cycle_id") != cycle
                or raw.get("mode") not in {"unified", "full"}
                or raw.get("status") not in {"succeeded", "failed"}
                or raw.get("profile_lease_released") is not True
                or not str(raw.get("finished_at") or "").strip()
            ):
                raise ValueError(
                    "live stage is not an immutable released terminal")
            try:
                returncode = int(raw.get("returncode"))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "live stage terminal returncode is invalid") from exc
            if (
                (raw["status"] == "succeeded" and returncode != 0)
                or (raw["status"] == "failed" and returncode == 0)
            ):
                raise ValueError(
                    "live stage status and returncode are inconsistent")

            ledger_path = Path(db_root) / "ledger.db"
            connection = sqlite3.connect(
                f"file:{ledger_path.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=10,
            )
            try:
                active = connection.execute(
                    "SELECT cycle_id FROM stage_profile_leases "
                    "WHERE profile='live' AND cycle_id=?",
                    (cycle,),
                ).fetchone()
            finally:
                connection.close()
            if active is not None:
                raise ValueError("same-cycle live profile lease still exists")
            report_barrier = load_live_report_barrier(
                cycle, status_dir=STAGE_STATUS_DIR)
            if report_barrier is None:
                raise ValueError(
                    "post-Agent report reconcile barrier missing or unsafe")
            return {
                "status": raw["status"],
                "returncode": returncode,
                "finished_at": str(raw["finished_at"]),
                "profile_lease_released": True,
                "same_cycle_active_lease": False,
                "report_reconcile_barrier": report_barrier,
            }
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise last_error
            time.sleep(0.1)


def _verify_business_attestation(
    payload: dict,
    db_root: str,
    cycle: str,
    *,
    upstream_failure_report: bool,
    terminal_wait_seconds: float = 0.0,
) -> dict:
    """Fail closed when the rendered business truth changed before send."""
    if str(cycle) < BUSINESS_ATTESTATION_REQUIRED_FROM:
        return {"ok": True, "required": False, "activation_cycle": (
            BUSINESS_ATTESTATION_REQUIRED_FROM)}
    failure_context = None
    if upstream_failure_report:
        failure_context = require_upstream_failure(
            cycle,
            db_root=db_root,
            status_dir=STAGE_STATUS_DIR,
        )
    stage_terminal = _verify_live_stage_terminal(
        cycle,
        db_root,
        wait_seconds=terminal_wait_seconds,
        upstream_failure_report=upstream_failure_report,
        expected_failure_context=failure_context,
    )
    expected = payload.get("business_report_attestation")
    if not isinstance(expected, dict):
        raise ValueError("payload business attestation missing")
    if (
        upstream_failure_report
        and failure_context.get("stage") == "collection"
    ):
        expected_failure = payload.get("upstream_failure")
        if (
            not isinstance(expected_failure, dict)
            or _collection_failure_identity(expected_failure)
            != _collection_failure_identity(failure_context)
        ):
            raise ValueError(
                "collection failure receipt or gate proof drifted after build")
    current = _current_business_attestation(
        db_root,
        cycle,
        upstream_failure_report=upstream_failure_report,
        failure_kind=(
            failure_context["failure_kind"]
            if upstream_failure_report else None
        ),
    )
    inter_report = _verify_inter_report_exchange_attestation(
        payload, db_root, cycle)
    if upstream_failure_report:
        if expected != current:
            raise ValueError(
                "failure report business or execution-intent proof drifted")
        return {
            "ok": True,
            "required": True,
            "mode": "upstream_failure",
            **current,
            **inter_report,
            "live_stage_terminal": stage_terminal,
        }
    if expected != current:
        raise ValueError("business terminal or fill set changed after build")
    return {
        "ok": True,
        "required": True,
        "mode": "business_terminal",
        "decision": current["decision"],
        "n_orders": current["n_orders"],
        "trade_count": current["trade_count"],
        "sha256": current["sha256"],
        **inter_report,
        "live_stage_terminal": stage_terminal,
    }


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


def run(
    cycle: str,
    db_root: str,
    no_send: bool,
    upstream_failure_report: bool = False,
) -> dict:
    # Validate before creating work/report/log directories.  The cycle is used
    # in filenames and database identities, so traversal-shaped or impossible
    # timestamps must fail without any filesystem side effect.
    cycle = validate_cycle_id(cycle)
    WORK.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    RUNLOG.parent.mkdir(parents=True, exist_ok=True)
    safe = cycle.replace(":", "").replace("T", "-")
    payload_f = str(WORK / f"payload-{safe}.json")
    content_f = str(WORK / f"content-{safe}.txt")

    rep: dict = {
        "cycle": cycle,
        "ts": now_ts(),
        "report_mode": (
            "upstream_failure" if upstream_failure_report else "business_terminal"),
        "steps": {},
        "ok": False,
    }

    # 1. build（进程内直调，确定性组装）
    try:
        failure_context = None
        if upstream_failure_report:
            # 与 dispatcher 分离的第二次完整身份/终态/激活边界校验；任何漂移
            # 都在 build 前失败关闭，因此无法靠命令行 flag 伪造失败报告。
            failure_context = require_upstream_failure(
                cycle,
                db_root=db_root,
                status_dir=STAGE_STATUS_DIR,
            )
            rep["upstream_failure"] = failure_context
        bpp = _load_build()
        # 保持正常业务终态的既有 build 调用契约；仅失败报告路径注入
        # 经过二次校验的最小失败上下文。这样旧的确定性组装器/测试替身
        # 不会因为一个与其无关的可选关键字参数而提前失败。
        if upstream_failure_report:
            payload = bpp.build(
                db_root, cycle, upstream_failure=failure_context)
        else:
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
    rc, out, err = _run(r".\scripts\render_push_report.py",
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
        r".\scripts\validate_push_format.py",
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

    # 4. 业务终态/成交指纹前置硬闸：自激活槽起，先证明同槽 live
    # runner 已终止、租约已释放，且渲染所用终态/逐笔成交仍与权威库一致。
    # 这样迟到同槽成交既不会外发，也不会进入 production reports 目录。
    try:
        business_attestation = _verify_business_attestation(
            payload,
            db_root,
            cycle,
            upstream_failure_report=upstream_failure_report,
            terminal_wait_seconds=5.0,
        )
        rep["steps"]["business_attestation_pre_archive"] = (
            business_attestation)
    except Exception as exc:
        rep["steps"]["business_attestation_pre_archive"] = {
            "ok": False,
            "required": str(cycle) >= BUSINESS_ATTESTATION_REQUIRED_FROM,
            "error": f"{type(exc).__name__}: {exc}",
        }
        rep["fatal"] = "business_attestation_failed"
        return _finish(rep)

    # 5. 归档前置硬闸：归档返回成功且文件内容核验完成，才允许进入 send。
    # --no-send 下归到 dev 目录，不覆写生产 latest.md。
    title = receipt.get("title") or f"push {cycle}"
    arch_in = json.dumps({"ts": now_ts(), "content_file": content_f, "title": title},
                         ensure_ascii=False)
    arch_args = ["--stdin"]
    if no_send:
        arch_args = ["--reports-dir", str(WORK / "reports"), "--stdin"]
    rc, out, err = _run(r".\scripts\push_archive.py", arch_args, stdin_text=arch_in)
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

    # 6. 外发（--no-send 跳过）。发送失败时，前置时间戳归档已经完整保留。
    if no_send:
        rep["steps"]["send"] = {"skipped": True, "reason": "--no-send"}
    else:
        # The renderer was fed one exact business terminal/fill set.  Re-read
        # that truth immediately before the irreversible external send so a
        # late same-cycle writer can never be silently omitted.  The live
        # profile lease prevents normal Agent writes here; this attestation is
        # the final independent defence against regressions or maintenance
        # races.  Any drift leaves the complete local archive in place and
        # fails closed without sending.
        try:
            rep["steps"]["business_attestation_pre_send"] = (
                _verify_business_attestation(
                    payload,
                    db_root,
                    cycle,
                    upstream_failure_report=upstream_failure_report,
                    terminal_wait_seconds=0.0,
                )
            )
        except Exception as exc:
            rep["steps"]["business_attestation_pre_send"] = {
                "ok": False,
                "required": str(cycle) >= BUSINESS_ATTESTATION_REQUIRED_FROM,
                "error": f"{type(exc).__name__}: {exc}",
            }
            rep["steps"]["send"] = {
                "skipped": True,
                "reason": "business_attestation_failed",
            }
            rep["fatal"] = "business_attestation_failed"
            return _finish(rep)
        # 显式身份键 push:{cycle}：同 cycle 任何 content 同键，重跑幂等。
        try:
            rc, out, err = _run(
                r".\scripts\qq_push.py",
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

    # 7. 状态落库（--no-send 下不真写生产 account.db，只报告将写什么）
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
        rc, out, err = _run(r".\scripts\system_state_writer.py", ["--json-file", state_json])
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
    ap.add_argument("--db-root", default=r".\db")
    ap.add_argument("--no-send", action="store_true", help="跳过 QQ 外发（阶段一开发用）")
    ap.add_argument(
        "--upstream-failure-report",
        action="store_true",
        help=("仅对未来已激活且已证明无执行副作用的 live/采集终局失败 "
              "生成 WAIT 报告"),
    )
    args = ap.parse_args()
    rep = run(
        args.cycle,
        args.db_root,
        args.no_send,
        upstream_failure_report=args.upstream_failure_report,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
