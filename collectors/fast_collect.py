# -*- coding: utf-8 -*-
"""V2.0 快采脚本（系统层，零 agent）。

由聚合 runner `collect_cycle.py`（cron `okx-collect-hourly` :00 / `okx-collect-quarter`
:15,:30,:45）作为首步串行调用（2026-08-08 整并前：独立 cron `okx-fast-collect`）。

组合调用 collect_data.py + jobb_live_account_check.py，结尾：
  1. 写账本 collection_runs(cycle_id, 'fast', status)
  2. （可选）X 搜索 → 写账本 'x_search'（失败不阻断快采主体，§2）
  3. 通过 _dispatch_nudge 通知 core/dispatcher.py；定时 dispatcher 负责兜底

与生产隔离：默认 --db-root .\\db；tmp 验证传临时目录。--dry-collect 跳过真采集
（不联网、不写生产），只验账本+触发 plumbing。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, r".\collectors")
import ledger          # noqa: E402

try:  # HANDOFF-4B 采集侧事件通知（可缺省，守卫式导入照 analyst_writer 惯例）
    import _dispatch_nudge as _nudge_mod  # noqa: E402
except Exception:  # noqa: BLE001
    _nudge_mod = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(r".")
SCRIPTS = ROOT / "scripts"
CST = timezone(timedelta(hours=8))
# 子进程隐藏窗口：本脚本被 wscript 以无窗口起，console 子进程(pwsh)默认会新开可见窗口——加此 flag 抑制
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MIN_STEP_BUDGET_SECONDS = 12
CORE_FINALIZATION_RESERVE_SECONDS = 5
CONTRACT_RECOVERY_TIMEOUT_SECONDS = 28


def _last_json(text: str):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _step_error(step: dict) -> str | None:
    """提取可落账的短错误，避免只剩 rc=1 而底层根因永久丢失。"""
    if step.get("ok"):
        return None
    payload = step.get("payload")
    detail = payload.get("error") if isinstance(payload, dict) else None
    if not detail:
        detail = (step.get("stderr_tail") or "").strip()
    if not detail:
        detail = f"rc={step.get('rc')}"
    return f"{step.get('name', 'unknown')}: {detail}"[:500]


def _step_degraded_warning(step: dict) -> str | None:
    """Preserve payload warnings even when a >=99% enrichment step passes.

    A warning is evidence (for example a newly listed symbol without a closed
    statistics bucket), not automatically a failed completeness gate.  The
    step's explicit ``degraded`` flag controls status; warnings remain in the
    receipt and ledger so the sub-1% tail is never hidden.
    """
    payload = step.get("payload")
    if not isinstance(payload, dict):
        return None
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        detail = "; ".join(str(item) for item in warnings if str(item).strip())
    elif payload.get("degraded") is not True:
        return None
    else:
        quality = payload.get("quality")
        if isinstance(quality, dict) and quality:
            detail = json.dumps(
                quality, ensure_ascii=False, sort_keys=True)
        elif "positioning_coverage_rate" in payload:
            retry = payload.get("retry")
            retry = retry if isinstance(retry, dict) else {}
            try:
                coverage = f"{float(payload['positioning_coverage_rate']):.6f}"
            except (TypeError, ValueError):
                coverage = "invalid"
            detail = (
                f"coverage={coverage} "
                f"initial_invalid={retry.get('initial_invalid_symbols')} "
                f"retry_recovered={retry.get('retry_recovered_symbols')} "
                f"final_failed={retry.get('final_failed_symbols')}"
            )
        else:
            detail = str(payload.get("error") or "degraded")
    return f"{step.get('name', 'unknown')}: {detail or 'degraded'}"[:500]


def full_universe_shadow_due(cycle_id: str) -> bool:
    """Three broad universe snapshots/day keep the 300+ pair throughput audit."""
    try:
        value = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return False
    return value.minute == 0 and value.hour % 8 == 0


def frozen_model_shadow_due(cycle_id: str) -> bool:
    """Freeze one research-only model signal panel on every natural hour.

    Hourly panels accelerate prospective diagnostics and distinct-cycle
    diversity.  They do not make overlapping 4H outcomes statistically
    independent and never change the production confidence/order gates.
    """
    try:
        value = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return False
    return value.minute == 0


def official_positioning_due(cycle_id: str) -> bool:
    """Collect the official 1H account ratio at :00/:30.

    The upstream observation cadence remains hourly.  A second fetch at :30
    prevents the oldest symbol-level source row from crossing the 90-minute
    decision-freshness gate during the second half of an hour.
    """
    try:
        value = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return False
    return value.minute in (0, 30)


def frozen_model_shadow_evaluation_due(cycle_id: str) -> bool:
    """Settle mature horizons hourly after the natural :30 ticker snapshot."""
    try:
        value = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return False
    return value.minute == 30


def full_universe_shadow_path(db_root: Path, cycle_id: str) -> Path:
    """Keep production artifacts in reports; isolate non-production DB roots."""
    try:
        production = db_root.resolve() == (ROOT / "db").resolve()
    except OSError:
        production = False
    base = (
        ROOT / "reports" / "quality" / "universe-shadow"
        if production
        else db_root.parent / "reports" / "quality" / "universe-shadow"
    )
    return base / cycle_id[:10] / f"universe-shadow-{cycle_id.replace(':', '-')}.json"


def frozen_model_shadow_path(db_root: Path, cycle_id: str) -> Path:
    """Keep future frozen-model evidence separate from alignment snapshots."""
    try:
        production = db_root.resolve() == (ROOT / "db").resolve()
    except OSError:
        production = False
    base = (
        ROOT / "reports" / "quality" / "model-shadow" / "forward"
        if production
        else db_root.parent / "reports" / "quality" / "model-shadow" / "forward"
    )
    return base / cycle_id[:10] / f"model-shadow-{cycle_id.replace(':', '-')}.json"


def frozen_model_shadow_evaluation_paths(
    db_root: Path,
) -> tuple[Path, Path, Path]:
    """Return isolated forward input, evaluation receipt and label paths."""
    try:
        production = db_root.resolve() == (ROOT / "db").resolve()
    except OSError:
        production = False
    quality = (
        ROOT / "reports" / "quality"
        if production
        else db_root.parent / "reports" / "quality"
    )
    return (
        quality / "model-shadow" / "forward",
        quality / "model-shadow-evaluation.json",
        quality / "model-shadow-labels.csv",
    )


def frozen_model_shadow_quality_path(db_root: Path) -> Path:
    """Keep the independent label-quality receipt beside isolated outputs."""
    try:
        production = db_root.resolve() == (ROOT / "db").resolve()
    except OSError:
        production = False
    quality = (
        ROOT / "reports" / "quality"
        if production
        else db_root.parent / "reports" / "quality"
    )
    return quality / "model-shadow-label-quality-audit.json"


def multitimeframe_coverage_path(db_root: Path) -> Path:
    """Keep the latest closed-bar coverage receipt beside other quality reports."""
    try:
        production = db_root.resolve() == (ROOT / "db").resolve()
    except OSError:
        production = False
    base = (
        ROOT / "reports" / "quality"
        if production
        else db_root.parent / "reports" / "quality"
    )
    return base / "multitimeframe-coverage-audit.json"


def _collection_status(steps: list[dict]) -> str:
    """Classify the fast cycle without treating account writers as optional.

    Market microstructure is an enhancement and may degrade independently.
    Market collection plus the live account snapshot writer are required for
    dispatch; a missing or failed required step fails the whole cycle.

    2026-08-06：`demo_position_check` 从必需源移除（demo 全量下线第一步）。它曾是
    采集 gate 的必需项——一旦 demo 侧停写，gate 会永久 abort，`_collection_ready()`
    返回 False，**unified live 再也不派发、整条实盘链停摆**。所以必须先解除这条
    约束，再动 demo 的任何产出路径。
    """
    by_name = {str(step.get("name")): step for step in steps}
    required = ("collect_data", "live_account_check")
    if any(not bool(by_name.get(name, {}).get("ok")) for name in required):
        return "error"
    collect_payload = by_name.get("collect_data", {}).get("payload")
    if (
        isinstance(collect_payload, dict)
        and collect_payload.get("degraded") is True
    ):
        return "degraded"
    if not bool(by_name.get("market_features", {}).get("ok")):
        return "degraded"
    contract_step = by_name.get("contract_statistics")
    recovery_step = by_name.get("contract_statistics_recovery")
    effective_contract_step = contract_step
    if (
        contract_step is not None
        and not bool(contract_step.get("ok"))
        and recovery_step is not None
        and bool(recovery_step.get("ok"))
    ):
        effective_contract_step = recovery_step
    if (
        effective_contract_step is not None
        and not bool(effective_contract_step.get("ok"))
    ):
        return "degraded"
    contract_payload = (
        effective_contract_step.get("payload")
        if effective_contract_step is not None else None
    )
    if (
        isinstance(contract_payload, dict)
        and contract_payload.get("degraded") is True
    ):
        return "degraded"
    if (
        "official_positioning" in by_name
        and not bool(by_name["official_positioning"].get("ok"))
    ):
        return "degraded"
    positioning_payload = by_name.get("official_positioning", {}).get("payload")
    if (
        isinstance(positioning_payload, dict)
        and positioning_payload.get("degraded") is True
    ):
        return "degraded"
    return "ok"


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """终止超时子任务及其后代。

    直起 Python 已消掉 pwsh 这层，但 collect_data.py 自身仍会拉起孙进程
    （`_okxcli` 走 okx npm CLI）；只杀直接子进程会留下它们持有 stdout 管道，
    communicate() 二次阻塞，超时照样失效。故整树杀。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:  # noqa: BLE001
            proc.kill()
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        if proc.poll() is None:
            proc.kill()


def run_step(name: str, script: Path, sargs: list[str], timeout: int) -> dict:
    # fast_collect.py 本身已由 run_okx_python.ps1 启动，sys.executable 与 env/PYTHONPATH/
    # 代理/凭证均已受控。内层直接起 Python，消除 pwsh→python 管道继承导致 TimeoutExpired
    # 只杀 pwsh、真正 collect_data.py 继续存活的问题（2026-08-05 定位：该缺陷让步级
    # timeout 与 --total-budget 全部失效，fast 采集反复撞满 cron 480s 硬超时并整轮丢数据；
    # 同一修法 2026-07-28 已在 slow_collect 生效）。
    cmd = [sys.executable, str(script), *sargs]
    t0 = time.time()
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return {"name": name, "ok": proc.returncode == 0, "rc": proc.returncode,
                "dur_s": round(time.time() - t0, 2),
                "payload": _last_json(stdout or ""),
                "stderr_tail": (stderr or "")[-500:]}
    except subprocess.TimeoutExpired as exc:
        # 超时也尽量保住已刷出的 stdout：其中的末行 JSON 仍可能带底层 error 归因。
        partial_stdout = exc.stdout or ""
        partial_stderr = exc.stderr or ""
        if proc is not None:
            _terminate_process_tree(proc)
            try:
                tail_stdout, tail_stderr = proc.communicate(timeout=1)
                if tail_stdout:
                    partial_stdout = tail_stdout
                if tail_stderr:
                    partial_stderr = tail_stderr
            except Exception:  # noqa: BLE001
                pass
        if isinstance(partial_stdout, bytes):
            partial_stdout = partial_stdout.decode("utf-8", errors="replace")
        if isinstance(partial_stderr, bytes):
            partial_stderr = partial_stderr.decode("utf-8", errors="replace")
        return {"name": name, "ok": False, "rc": 124,
                "dur_s": round(time.time() - t0, 2),
                "payload": _last_json(str(partial_stdout)),
                "stderr_tail": (
                    f"timeout after {timeout}s; process tree terminated"
                    + (f" | child_stderr={str(partial_stderr)[-240:]}"
                       if partial_stderr else "")
                )[:500]}
    except Exception as e:  # noqa: BLE001 —— OSError/FileNotFoundError 等 spawn 失败：
        if proc is not None and proc.poll() is None:
            _terminate_process_tree(proc)
        # 捕获 spawn 失败，确保 main 仍能写 collection_runs 归因。
        return {"name": name, "ok": False, "rc": -1,
                "dur_s": round(time.time() - t0, 2),
                "stderr_tail": f"spawn_failed: {e}"[:500]}


def _bounded_step_timeout(
    *,
    deadline: float,
    requested: int,
    reserve_after: int = 0,
    now: float | None = None,
) -> int | None:
    """Bound a step without borrowing budget reserved for later gates."""
    current = time.time() if now is None else float(now)
    available = int(deadline - current) - max(0, int(reserve_after))
    if available < MIN_STEP_BUDGET_SECONDS:
        return None
    return max(
        MIN_STEP_BUDGET_SECONDS,
        min(max(MIN_STEP_BUDGET_SECONDS, int(requested)), available),
    )


def _budget_exhausted_step(name: str, reserve_after: int) -> dict:
    """Emit an explicit receipt instead of silently exceeding total budget."""
    return {
        "name": name,
        "ok": False,
        "rc": 124,
        "dur_s": 0.0,
        "payload": None,
        "stderr_tail": (
            "budget_exhausted_before_start; "
            f"reserved_after={max(0, int(reserve_after))}s"
        ),
    }


def _send_failure_alert(cycle: str, error_detail: str | None,
                        latency_ms: int) -> dict:
    """将快采失败显式路由到 C2C；告警失败只留诊断，不改变业务状态。"""
    safe_cycle = cycle.replace(":", "-")
    content_file = ROOT / "tmp" / f"fast_collect_failure_{safe_cycle}.txt"
    content_file.parent.mkdir(parents=True, exist_ok=True)
    content_file.write_text(
        (
            f"【P1 快采失败】cycle={cycle}\n"
            f"latency={latency_ms / 1000:.1f}s\n"
            f"detail={error_detail or 'unknown'}\n"
            "本轮已 fail-closed，不派发分析/交易；不会自动补跑或补派。"
        ),
        encoding="utf-8",
    )
    step = run_step(
        "failure_alert",
        SCRIPTS / "qq_push.py",
        [
            "--alert",
            "--content-file",
            str(content_file),
            "--dedupe-key",
            f"fast-collect:{cycle}",
            # 快采总预算 320s、外层 collect_cycle 360s；把告警内部预算
            # 固定为25s，给 wrapper/账本/进程收尾保留至少15s。
            "--timeout",
            "25",
        ],
        timeout=30,
    )
    step["diagnostic_only"] = True
    return step


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 快采（系统层）")
    ap.add_argument("--db-root", default=str(ROOT / "db"))
    ap.add_argument("--profile", default="live")
    ap.add_argument("--cycle", default=None, help="覆盖 cycle_id（默认按当前时刻归槽）")
    # 步级默认超时 + 全程预算钳（见 main 内 deadline），保证账本写入与派发
    # 能在 cron 超时前完成。
    # 聚合上线后390轮：P99=98.37s、最大139.79s。150s覆盖已观测长尾，
    # 同时由总预算预留保证账户/合约/记账不会被先到先得地挤占。
    ap.add_argument("--collect-timeout", type=int, default=150)
    # 2026-08-08~12的390个自然轮次：live账户P99=17.11s、最大17.66s。
    ap.add_argument("--account-timeout", type=int, default=25)
    # 既有样本最大52.14s；2026-08-13又出现3个恰好在60s被外层终止的自然槽，
    # 且终止时总预算仍有余量。放宽到75s覆盖网络长尾，仍受320s总预算钳与
    # collect_cycle 360s硬上限约束，不借用最终落账余量。
    ap.add_argument("--features-timeout", type=int, default=75)
    ap.add_argument("--contract-stats-timeout", type=int, default=75)
    ap.add_argument(
        "--contract-recovery-timeout",
        type=int,
        default=CONTRACT_RECOVERY_TIMEOUT_SECONDS,
    )
    ap.add_argument("--positioning-timeout", type=int, default=60)
    ap.add_argument("--shadow-timeout", type=int, default=20)
    ap.add_argument("--coverage-audit-timeout", type=int, default=15)
    ap.add_argument("--model-shadow-timeout", type=int, default=20)
    ap.add_argument("--model-evaluation-timeout", type=int, default=15)
    ap.add_argument("--model-quality-audit-timeout", type=int, default=20)
    ap.add_argument("--total-budget", type=int, default=320,
                    help=(
                        "全部采集步骤总预算（秒）；须低于collect_cycle的"
                        "fast-timeout=360并为记账、清理、失败告警留余量"))
    ap.add_argument("--dry-collect", action="store_true",
                    help="跳过真采集（不联网/不写生产），只验账本+触发")
    ap.add_argument("--no-universe-shadow", action="store_true",
                    help="显式关闭每日三次全市场影子判断（应急回退）")
    ap.add_argument("--no-model-shadow", action="store_true",
                    help="显式关闭每小时冻结增强模型未来影子评分（应急回退）")
    # 生产 cron 仍可能携带该历史参数；保留为无副作用兼容参数，派发始终走 nudge+dispatcher。
    ap.add_argument("--no-dispatch", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    db_root = Path(args.db_root)
    ledger_db = db_root / "ledger.db"
    ledger.init_ledger(ledger_db)
    cycle = args.cycle or ledger.cycle_id_for()

    t0 = time.time()
    deadline = t0 + args.total_budget
    positioning_due = official_positioning_due(cycle)

    def run_budgeted(
        name: str,
        script: Path,
        sargs: list[str],
        requested_timeout: int,
        *,
        reserve_after: int = 0,
    ) -> dict:
        timeout = _bounded_step_timeout(
            deadline=deadline,
            requested=requested_timeout,
            reserve_after=reserve_after,
        )
        if timeout is None:
            return _budget_exhausted_step(name, reserve_after)
        return run_step(name, script, sargs, timeout)

    steps = []
    if args.dry_collect:
        status, rows = "ok", 0
    else:
        # 390轮自然耗时证据：collect_data P99=98.37s/最大139.79s、
        # live账户P99=17.11s、合约统计P99=59.23s。先为后两项保留完整预算，网络故障时也不让
        # 可选盘口先到先得地耗尽必需账户与模型可用直采预算。
        core_reserve = (
            max(MIN_STEP_BUDGET_SECONDS, args.account_timeout)
            + max(MIN_STEP_BUDGET_SECONDS, args.contract_stats_timeout)
            + (
                max(MIN_STEP_BUDGET_SECONDS, args.positioning_timeout)
                if positioning_due else 0
            )
            + CORE_FINALIZATION_RESERVE_SECONDS
        )
        collect_step = run_budgeted(
            "collect_data", SCRIPTS / "collect_data.py",
            ["--profile", args.profile, "--db-root", str(db_root),
             "--cycle", cycle, "--skip-news"],
            args.collect_timeout,
            reserve_after=core_reserve,
        )
        steps.append(collect_step)
        # live账户是dispatcher必需源，优先于全部影子增强数据。
        steps.append(run_budgeted(
            "live_account_check", SCRIPTS / "jobb_live_account_check.py",
            ["--profile", args.profile, "--db-root", str(db_root)],
            args.account_timeout,
            reserve_after=(
                max(MIN_STEP_BUDGET_SECONDS, args.contract_stats_timeout)
                + (
                    max(MIN_STEP_BUDGET_SECONDS, args.positioning_timeout)
                    if positioning_due else 0
                )
                + CORE_FINALIZATION_RESERVE_SECONDS
            ),
        ))
        # 全宇宙官方15m合约统计单独进程顺序执行，避免与盘口/逐笔的连接池竞争。
        # 低于99%时本步 rc=1，使 fast 显式 degraded，但不阻断必需行情和账户快照。
        contract_step = run_budgeted(
            "contract_statistics", SCRIPTS / "collect_market_features.py",
            [
                "--db-root", str(db_root), "--cycle", cycle,
                "--contract-stats", "always", "--contract-stats-only",
            ],
            args.contract_stats_timeout,
            reserve_after=(
                (
                    max(MIN_STEP_BUDGET_SECONDS, args.positioning_timeout)
                    if positioning_due else 0
                )
                + CORE_FINALIZATION_RESERVE_SECONDS
            ),
        )
        steps.append(contract_step)
        # 初次严格直采未过门时，在同一自然周期启一个新进程/新连接，只恢复
        # 缺少直接官方行的精确标的集合。恢复器拒绝历史周期、每端点每币最多
        # 一次请求且无循环；预算不足或恢复仍低于99%时继续明确 degraded。
        if bool(collect_step.get("ok")) and not bool(contract_step.get("ok")):
            recovery_step = run_budgeted(
                "contract_statistics_recovery",
                SCRIPTS / "recover_contract_statistics_current.py",
                ["--db-root", str(db_root), "--cycle", cycle],
                args.contract_recovery_timeout,
                reserve_after=(
                    (
                        max(MIN_STEP_BUDGET_SECONDS, args.positioning_timeout)
                        if positioning_due else 0
                    )
                    + MIN_STEP_BUDGET_SECONDS
                    + CORE_FINALIZATION_RESERVE_SECONDS
                ),
            )
            steps.append(recovery_step)
            if recovery_step.get("ok"):
                # 首次失败不能抹去；作为诊断 warning 留在本轮输出/日志，
                # 但最终直采门由恢复器的独立复核结果决定。
                contract_step["diagnostic_only"] = True
                contract_step["recovered_by"] = "contract_statistics_recovery"
        # :00/:30全宇宙账户多空比与盘口/逐笔分进程顺序执行。独立入口只接受
        # 当前自然槽，并对首次无效标的做最多两波12秒递减精确恢复；不补历史。
        # 低于99%时本步rc=1并显式降级。
        if positioning_due:
            steps.append(run_budgeted(
                "official_positioning", SCRIPTS / "collect_positioning_current.py",
                [
                    "--db-root", str(db_root), "--cycle", cycle,
                ],
                args.positioning_timeout,
                reserve_after=CORE_FINALIZATION_RESERVE_SECONDS,
            ))
        # 50档订单簿与逐笔成交后置。预算不足时明确跳过，绝不借用记账余量。
        steps.append(run_budgeted(
            "market_features", SCRIPTS / "collect_market_features_resilient.py",
            [
                "--db-root", str(db_root), "--depth", "50",
                "--cycle", cycle, "--positioning", "off",
            ],
            args.features_timeout,
            reserve_after=CORE_FINALIZATION_RESERVE_SECONDS,
        ))
        if (
            not args.no_universe_shadow
            and full_universe_shadow_due(cycle)
        ):
            shadow_out = full_universe_shadow_path(db_root, cycle)
            shadow_step = run_budgeted(
                "universe_judgment_shadow",
                SCRIPTS / "universe_judgment_snapshot.py",
                [
                    "--db-root", str(db_root),
                    "--json-out", str(shadow_out),
                    "--cycle-id", cycle,
                ],
                args.shadow_timeout,
                reserve_after=CORE_FINALIZATION_RESERVE_SECONDS,
            )
            # 影子质量门不阻断行情/账户采集或 dispatcher；rc=2 会进入 warnings。
            shadow_step["diagnostic_only"] = True
            steps.append(shadow_step)
            coverage_step = run_budgeted(
                "multitimeframe_coverage_audit",
                SCRIPTS / "audit_multitimeframe_coverage.py",
                [
                    "--market-db", str(db_root / "market.db"),
                    "--minimum-rate", "0.99",
                    "--json-out", str(multitimeframe_coverage_path(db_root)),
                ],
                args.coverage_audit_timeout,
                reserve_after=CORE_FINALIZATION_RESERVE_SECONDS,
            )
            # 质量未达只作为诊断告警：不改变快采、dispatcher或订单路径。
            coverage_payload = coverage_step.get("payload")
            if (
                coverage_step.get("ok")
                and isinstance(coverage_payload, dict)
                and coverage_payload.get("status") != "PASSED"
            ):
                coverage_step["ok"] = False
                coverage_step["stderr_tail"] = (
                    "quality_status="
                    f"{coverage_payload.get('status', 'UNKNOWN')}; "
                    "data_completeness_status="
                    f"{coverage_payload.get('data_completeness_status', 'UNKNOWN')}; "
                    "analysis_readiness_status="
                    f"{coverage_payload.get('analysis_readiness_status', 'UNKNOWN')}"
                )
            coverage_step["diagnostic_only"] = True
            steps.append(coverage_step)
        if (
            not args.no_model_shadow
            and frozen_model_shadow_due(cycle)
        ):
            if collect_step.get("ok"):
                model_shadow_out = frozen_model_shadow_path(db_root, cycle)
                model_shadow_step = run_budgeted(
                    "frozen_model_shadow",
                    SCRIPTS / "score_multitimeframe_model_shadow.py",
                    [
                        "--db-root", str(db_root),
                        "--cycle-id", cycle,
                        "--json-out", str(model_shadow_out),
                    ],
                    args.model_shadow_timeout,
                    reserve_after=CORE_FINALIZATION_RESERVE_SECONDS,
                )
            else:
                # 没有本周期权威行情时，冻结评分器的输入契约必然不成立。
                # 直接外显前置失败，避免在空 DataFrame 上产生二次 KeyError；
                # 不触碰已冻结评分器及其清单哈希，也不改变 fast 的业务状态。
                model_shadow_step = {
                    "name": "frozen_model_shadow",
                    "ok": False,
                    "rc": 125,
                    "dur_s": 0.0,
                    "payload": None,
                    "stderr_tail": (
                        "prerequisite collect_data failed; scorer not started"
                    ),
                }
            # 研究模型离线门仍未通过；每小时评分只加快前瞻诊断，失败只告警，
            # 绝不影响主行情、账户快照、dispatcher、阈值或订单路径。
            model_shadow_step["diagnostic_only"] = True
            steps.append(model_shadow_step)
        if (
            not args.no_model_shadow
            and frozen_model_shadow_evaluation_due(cycle)
        ):
            (
                model_shadow_root,
                model_evaluation_out,
                model_labels_out,
            ) = frozen_model_shadow_evaluation_paths(db_root)
            model_evaluation_step = run_budgeted(
                "frozen_model_shadow_evaluation",
                SCRIPTS / "evaluate_multitimeframe_model_shadow.py",
                [
                    "--shadow-root", str(model_shadow_root),
                    "--market-db", str(db_root / "market.db"),
                    "--json-out", str(model_evaluation_out),
                    "--labels-out", str(model_labels_out),
                ],
                args.model_evaluation_timeout,
                reserve_after=CORE_FINALIZATION_RESERVE_SECONDS,
            )
            # 每个自然小时 :00 只生成冻结信号；:30 复用刚落地 ticker，
            # 尽快结算此前成熟的15m/1H/4H标签。只读业务库、只写质量工件，
            # 且不参与采集状态、dispatcher、风控阈值或订单路径。4H 标签窗会
            # 重叠，故小时样本只称不同周期，不宣称统计独立。
            model_evaluation_step["diagnostic_only"] = True
            steps.append(model_evaluation_step)
            if model_evaluation_step.get("ok"):
                model_quality_step = run_budgeted(
                    "frozen_model_shadow_label_quality",
                    SCRIPTS / "audit_model_shadow_label_quality.py",
                    [
                        "--evaluation", str(model_evaluation_out),
                        "--labels", str(model_labels_out),
                        "--shadow-root", str(model_shadow_root),
                        "--market-db", str(db_root / "market.db"),
                        "--json-out", str(
                            frozen_model_shadow_quality_path(db_root)),
                    ],
                    args.model_quality_audit_timeout,
                    reserve_after=CORE_FINALIZATION_RESERVE_SECONDS,
                )
            else:
                # 不审计上一次遗留工件冒充本次成功；前置评估失败时显式失败关闭。
                model_quality_step = {
                    "name": "frozen_model_shadow_label_quality",
                    "ok": False,
                    "rc": 125,
                    "dur_s": 0.0,
                    "payload": None,
                    "stderr_tail": "prerequisite frozen-model evaluation failed",
                }
            model_quality_step["diagnostic_only"] = True
            steps.append(model_quality_step)
        # 2026-08-06 demo 全量下线：原先每轮还跑两步 demo——`demo_account_check.py`
        # 账实诊断，以及 `jobb_live_account_check.py --profile demo` 写 demo 的
        # account_snapshots/position_snapshots。两步一并移除，demo 侧不再产生新数据。
        status = _collection_status(steps)
        rows = 0
        for step in steps:
            # 恢复器只把同周期 carry/无效行替换为直接官方行，不增加该周期
            # 的唯一业务行数；ledger rows 保持按最终分母计，避免重复计写次数。
            if step.get("name") == "contract_statistics_recovery":
                continue
            wrote = (step.get("payload") or {}).get("wrote")
            if isinstance(wrote, dict):
                rows += sum(
                    int(v or 0)
                    for v in wrote.values()
                    if isinstance(v, (int, float))
                )
        if rows == 0:
            rows = None
    latency_ms = int((time.time() - t0) * 1000)
    step_errors = [
        err
        for step in steps
        if not step.get("diagnostic_only")
        for err in (_step_error(step),)
        if err
    ]
    degraded_warnings = [
        warning
        for step in steps
        for warning in (_step_degraded_warning(step),)
        if warning
    ]
    error_detail = "; ".join(step_errors)[:1000] if step_errors else None
    ledger_detail = (
        error_detail
        or ("; ".join(degraded_warnings)[:1000] if degraded_warnings else None)
    )
    ledger.record_collection(ledger_db, cycle, ledger.SRC_FAST, status,
                             rows=rows, latency_ms=latency_ms, err=ledger_detail)

    # OpenClaw isolated cron 的隐式 failureAlert 可能因目标不唯一而静默失败；业务层
    # 对生产 fast error 使用明确 --alert(C2C) 路由。隔离 DB、dry-run 与 degraded 均不外发。
    try:
        production_db = db_root.resolve() == (ROOT / "db").resolve()
    except OSError:
        production_db = False
    if status == "error" and production_db and not args.dry_collect:
        try:
            steps.append(_send_failure_alert(cycle, error_detail, latency_ms))
        except Exception as alert_exc:  # noqa: BLE001
            steps.append({
                "name": "failure_alert",
                "ok": False,
                "rc": -1,
                "dur_s": 0.0,
                "stderr_tail": f"alert_setup_failed: {alert_exc}"[:500],
                "diagnostic_only": True,
            })

    diagnostic_warnings = degraded_warnings + [
        err
        for step in steps
        if step.get("diagnostic_only")
        for err in (_step_error(step),)
        if err
    ]

    # HANDOFF-4B（2026-07-17）：落账后事件通知 dispatcher——消「采集完成→analyst 派发」的
    # 0-2min tick 等待。三道采集侧门（dry-collect/非生产 db-root/status 非 ok|degraded 不发）
    # + nudge() 四道守护闸都在 _dispatch_nudge 内；非致命，不改本脚本退出码。
    if _nudge_mod is not None:
        _nudge_mod.nudge_from_collector("fast_collect", args.db_root, [status],
                                        dry_collect=args.dry_collect)

    collect_payload = next((
        step.get("payload")
        for step in steps
        if step.get("name") == "collect_data"
        and isinstance(step.get("payload"), dict)
    ), None)
    market_quality = (
        collect_payload.get("quality")
        if isinstance(collect_payload, dict) else None
    )
    data_quality = None
    if isinstance(market_quality, dict):
        # Keep a compact, non-secret transport receipt in the parent output.
        # collect_cycle may safely retain this even when successful, while the
        # full child payload remains trimmed from the long-lived JSONL.
        data_quality = {
            key: market_quality.get(key)
            for key in (
                "expected", "tickers", "ticker_coverage",
                "candle_coverage", "funding_coverage", "ticker_transport",
            )
            if key in market_quality
        }
    feature_payload = next((
        step.get("payload")
        for step in steps
        if step.get("name") == "market_features"
        and isinstance(step.get("payload"), dict)
    ), None)
    feature_transport = (
        feature_payload.get("market_feature_transport")
        if isinstance(feature_payload, dict) else None
    )
    if isinstance(feature_transport, dict):
        if data_quality is None:
            data_quality = {}
        data_quality["market_feature_transport"] = feature_transport

    out = {"ok": status in ledger.DONE_STATUS, "cycle": cycle, "source": "fast",
           "status": status, "error": error_detail, "latency_ms": latency_ms,
           "warnings": diagnostic_warnings,
           "data_quality": data_quality,
           "steps": [
               {
                   **{k: s[k] for k in ("name", "ok", "rc", "dur_s")},
                   **({"error": err} if (err := _step_error(s)) else {}),
               }
               for s in steps
           ],
           "dispatch": None}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if status in ledger.DONE_STATUS else 1


if __name__ == "__main__":
    raise SystemExit(main())
