# -*- coding: utf-8 -*-
r"""daily_maintenance.py — 日频运维合并入口（2026-07-17 cron 治理，两轮合并后=
okx-reconcile + okx-log-rotate + okx-audit-snapshot 三条日频 cron 合为一条
okx-daily-maintenance，07:55 起跑）。

顺序跑：① reconcile_daily.py（交易所侧对账分级：demo 自动 --apply / live dry+P1）
        ② collect_account_bills.py（手续费/资金费/已实现盈亏账单）
        ③ quality_metrics.py（质量指标；完成后立即原子发布 reviewer ready handoff）
        ④ ledger_invariants.py（只读：近24h重复执行、负净仓、经验数量错配、
          未决/含糊执行意图；有发现 rc=1，使日维护失败外显，不自动补单/改账）
        ⑤ log_rotate.py --apply --days 7 --dirs trigger,push,stage-status
          （三类高频调试日志超 7 天轮转）
        ⑥ audit_snapshot.py（audit_events 增量导出，防滚动窗丢失）
        ⑦ reports_rotate.py --apply（reports/agents+push 超 30 天月度压包，
          封顶无界增长——2026-07-17 主人拍板）
        ⑧ collect_public_macro.py（Alternative.me、ECB复算DXY、ETF权威证据核验）
        ⑨ collect_macro_events.py（未来7天高重要度经济日历）
每步独立 fail-safe：一步失败/超时不阻断下一步；任一失败聚合 exit 1（cron 记 error
可见），全过 exit 0。新增日频运维项往这里加，不再开新 cron。
注意：本 cron 因含 reconcile（demo 会真动账本）已入 fulltest BUSINESS_CRONS——
测试窗内随业务 cron 一并停/复。
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
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = Path(__file__).resolve().parent
QUALITY_REPORT_DIR = Path(os.environ.get(
    "OKX_QUALITY_REPORT_DIR", _project_path('reports', 'quality')))
REVIEWER_READY_DIR = Path(os.environ.get(
    "OKX_REVIEWER_READY_DIR", str(QUALITY_REPORT_DIR)))
REVIEWER_CRITICAL_STEPS = (
    "reconcile",
    "account_bills",
    "quality_metrics",
)
CST = timezone(timedelta(hours=8))
STEPS = [
    # (名字, argv, 单步超时秒, 合法退出码)。reconcile 最坏 4 次 OKX API 往返（demo
    # dry→apply→复检→live dry）；其 rc=1=「有账实差异且告警已推」（脚本工作正常，
    # 差异经 QQ P1 走人工通道），只有 rc≥2 才算本步失败。
    ("reconcile", [str(SCRIPTS / "reconcile_daily.py")], 1200, (0, 1)),
    ("account_bills", [str(SCRIPTS / "collect_account_bills.py")], 120, (0,)),
    # 质量指标是 reviewer handoff 的第三个关键步骤。成功落完整 JSON 后立即
    # 发布 ready manifest；后续非关键维护继续跑，不阻塞 08:05 reviewer。
    ("quality_metrics", [str(SCRIPTS / "quality_metrics.py")], 120, (0,)),
    # 每日只读账本不变量：不启用 collection_monitor cron，不自动重放/补单/改账。
    # compact 保证聚合日志的 tail 能保留完整 findings；rc=1 作为日维护失败外显。
    ("ledger_invariants", [
        str(SCRIPTS / "ledger_invariants.py"),
        "--profile", "both", "--window-min", "1440", "--compact",
    ], 120, (0,)),
    ("log_rotate", [
        str(SCRIPTS / "log_rotate.py"),
        "--apply", "--days", "7",
        "--dirs", "trigger,push,stage-status",
    ], 120, (0,)),
    ("audit_snapshot", [str(SCRIPTS / "audit_snapshot.py")], 120, (0,)),
    ("reports_rotate", [str(SCRIPTS / "reports_rotate.py"), "--apply"], 120, (0,)),
    # 公开日频宏观：rc=2 表示部分可选源不可达；数据自身 freshness 另行外显，
    # 不让一个可选源阻断对账/审计等日维护步骤。
    ("public_macro", [str(SCRIPTS / "collect_public_macro.py")], 180, (0, 2)),
    ("macro_events", [str(SCRIPTS / "collect_macro_events.py")], 90, (0,)),
]


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _quality_artifact(business_date: str) -> dict:
    path = QUALITY_REPORT_DIR / f"quality_metrics_{business_date}.json"
    result = {"path": str(path), "valid": False}
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("root must be an object")
        if str(payload.get("ts") or "")[:10] != business_date:
            raise ValueError("business date differs")
        if not isinstance(payload.get("metrics"), dict):
            raise ValueError("metrics missing")
        result.update({
            "valid": True,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        })
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def build_reviewer_manifest(report: dict, state: str) -> dict:
    """Build the bounded hand-off contract consumed by the 08:05 reviewer."""
    business_date = str(report["business_date"])
    steps = report.get("steps") or {}
    critical = {}
    for name in REVIEWER_CRITICAL_STEPS:
        step = steps.get(name)
        critical[name] = {
            "completed": isinstance(step, dict),
            "rc": step.get("rc") if isinstance(step, dict) else None,
            "accepted": bool(step.get("accepted")) if isinstance(step, dict) else False,
        }
        if isinstance(step, dict) and step.get("artifact"):
            critical[name]["artifact"] = step["artifact"]

    ready = (
        state == "completed"
        and all(item["completed"] and item["accepted"]
                for item in critical.values())
    )
    reconcile_rc = critical["reconcile"]["rc"]
    all_steps = {}
    for name, step in steps.items():
        if not isinstance(step, dict):
            continue
        all_steps[name] = {
            "rc": step.get("rc"),
            "accepted": bool(step.get("accepted")),
            "completed_at": step.get("completed_at"),
        }
    return {
        "schema_version": 1,
        "business_date": business_date,
        "run_id": report["run_id"],
        "maintenance_started_at": report["started_at"],
        "critical_steps_completed_at": report.get(
            "critical_steps_completed_at"),
        "maintenance_completed_at": report.get("completed_at"),
        "maintenance_ok": report.get("ok"),
        "maintenance_steps": all_steps,
        "generated_at": now_cst(),
        "state": "ready" if ready else (
            "not_ready" if state == "completed" else "running"),
        "ready": ready,
        "critical_steps": list(REVIEWER_CRITICAL_STEPS),
        "steps": critical,
        # rc=1 is an accepted reconciliation run with unresolved differences.
        # The reviewer may proceed, but only with a provisional report.
        "provisional_required": reconcile_rc == 1,
        "report_mode": (
            "provisional" if ready and reconcile_rc == 1
            else "final_candidate" if ready
            else "blocked"
        ),
        "auto_send": False,
    }


def write_reviewer_manifest(report: dict, state: str) -> dict:
    manifest = build_reviewer_manifest(report, state)
    path = REVIEWER_READY_DIR / (
        f"reviewer_ready_{manifest['business_date']}.json")
    _atomic_write_json(path, manifest)
    return {**manifest, "path": str(path)}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "按固定顺序执行 9 项日频维护；不带参数时才执行，"
            "未知参数会立即退出。"
        )
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    parse_args(argv)
    started_at = now_cst()
    report = {
        "ts": started_at,
        "business_date": started_at[:10],
        "run_id": datetime.now(CST).strftime("%Y%m%dT%H%M%S.%f%z"),
        "started_at": started_at,
        "steps": {},
    }
    try:
        write_reviewer_manifest(report, "running")
    except Exception as exc:
        report["reviewer_ready"] = {
            "ready": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 2

    all_ok = True
    critical_handoff_attempted = False
    for name, argv, step_timeout, ok_codes in STEPS:
        try:
            p = subprocess.run([sys.executable, *argv], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=step_timeout)
            tail = (p.stdout or "").strip().splitlines()[-3:]
            accepted = p.returncode in ok_codes
            report["steps"][name] = {
                "rc": p.returncode,
                "accepted": accepted,
                "completed_at": now_cst(),
                "tail": tail,
            }
            if name == "quality_metrics" and accepted:
                artifact = _quality_artifact(report["business_date"])
                report["steps"][name]["artifact"] = artifact
                accepted = bool(artifact["valid"])
                report["steps"][name]["accepted"] = accepted
            if not accepted:
                all_ok = False
                err_tail = (p.stderr or "").strip().splitlines()[-3:]
                report["steps"][name]["stderr"] = err_tail
        except Exception as e:
            all_ok = False
            report["steps"][name] = {
                "rc": 99,
                "accepted": False,
                "completed_at": now_cst(),
                "error": f"{type(e).__name__}: {e}",
            }
        if (
            not critical_handoff_attempted
            and all(step in report["steps"]
                    for step in REVIEWER_CRITICAL_STEPS)
        ):
            critical_handoff_attempted = True
            report["critical_steps_completed_at"] = now_cst()
            try:
                report["reviewer_ready"] = write_reviewer_manifest(
                    report, "completed")
            except Exception as exc:
                all_ok = False
                report["reviewer_ready"] = {
                    "ready": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    report["ok"] = all_ok
    report["completed_at"] = now_cst()
    try:
        report["reviewer_ready"] = write_reviewer_manifest(
            report, "completed")
    except Exception as exc:
        report["ok"] = False
        report["reviewer_ready"] = {
            "ready": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
