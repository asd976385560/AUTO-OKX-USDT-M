# -*- coding: utf-8 -*-
r"""V2.0 采集聚合入口（2026-08-08 cron 整并；2026-08-15 全槽解耦）。

主要由 OpenClaw 命令型 cron 调度（经 run_okx_python.ps1 wrapper）：
  okx-collect-hourly   `0 * * * *`        --tier hourly   fast → (news || slow) → nudge
  okx-collect-quarter  `15,30,45 * * * *` --tier quarter  fast → news

外部调度器也可每 15 分钟计算当前自然槽，并显式传入 hourly/quarter 层级；
宿主专用 Scheduled Task 适配器不属于公开仓库。所有调度进入本脚本后先
争抢 `logs/collect/guards/<tier>-<cycle>.lock` 的 O_EXCL 单实例锁；成功轮原子写
同键 receipt，晚到的另一调度只记录 duplicate_completed 后 rc=0 退出，不重复采集。

步序即派发时延设计：fast 先跑、落账即 nudge——:15/:30/:45 槽（gate 只必需 fast）
的 unified live 派发时延与拆分时代完全一致。:00 槽在 fast 后并行跑 news 与
slow；slow 使用 ``--defer-dispatch-nudge`` 只落账，runner 等两步都终止后再拍一次
dispatcher。这使分析仍不会抢在当轮新闻之前，而小时轮关键路径从
``fast+news+slow`` 缩为 ``fast+max(news, slow)``。news 仍不进派发闸
（ledger.expected_sources），失败只按原有契约外显，不改交易安全闸。

子脚本各自负责：账本落账（fast/slow → collection_runs[fast|slow|regime]，
news_collect → 逐源 collection_runs[<source_id>] 带 err）。fast 仍落账后自行 nudge；
hourly 的 slow 由 runner 显式延后 nudge，避免新闻尚未收口就起分析。runner 另负责
编排 + 汇总 + 追加 JSONL 运行日志
（logs/collect/collect_cycle_YYYYMMDD.jsonl，随 log_rotate --dirs 含 collect 7 天轮转）。

步序 fail-safe（照 daily_maintenance）：一步失败/超时不阻断下一步；任一步失败
聚合 exit 1（cron 记 error 外显、failureAlert 计数），失败原因进 stdout JSON +
JSONL + 子脚本自身账本，不自动重试。news 判定特例：news_collect 逐源隔离恒
rc=0，仅本体崩溃/超时才非 0；runner 额外把「全部到期源 failed」也判步失败
（如实外显断网/代理全挂），部分源失败只进 warnings 不置 error（与旧独立
okx-news-rss cron 的告警灵敏度一致）。news 无论如何不阻断后续 slow，不碰交易链。

超时预算：fast 360s + max(news 300s, slow 480s) = 840s < hourly cron 1200s；
quarter 660s < 900s。真正的功能死线是 15min 槽界 + gate 900s 新鲜窗，cron
超时只是防挂死兜底。cycle 归槽：fast 显式 --cycle 钉 runner 启动槽（防链内
漂移错槽）；外部显式 --cycle 只接受当前 UTC+8 自然槽，过期/未来/层级不符在
联网、日志和数据库写入前拒绝；slow 用同一显式槽；news 自算
（无 --cycle 参数，链内漂移最多损当轮 60/120min 到期源，下轮自愈）。

tmp 验证：--dry-collect 透传 fast/slow（不联网不写生产），news 无 dry 模式改跳过；
dry 模式不创建 guard/receipt，不能影响随后真实轮。非生产 db-root 时子脚本的
nudge 自带闸门不发。零模型名（红线①）；UTF-8 无 BOM。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, r".\collectors")
import ledger          # noqa: E402  cycle_id_for

try:  # hourly 并行尾段收口后的单次延后派发
    import _dispatch_nudge as _nudge_mod  # noqa: E402
except Exception:  # noqa: BLE001 —— cron dispatcher 仍是兜底
    _nudge_mod = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(r".")
COLLECTORS = ROOT / "collectors"
CST = timezone(timedelta(hours=8))
# 子进程隐藏窗口：cron 经 wrapper 无窗口起本脚本，console 子进程默认新开可见窗口——抑制
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RUN_GUARD_SCHEMA_VERSION = 1
NATURAL_CYCLE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)$")


def _resolve_natural_cycle(tier: str, requested_cycle: str | None) -> str:
    """Resolve exactly one current UTC+8 slot and reject stale/mistyped work.

    The all-slot Windows guard passes ``--cycle`` so its slot is pinned once.
    OpenClaw callers may omit it for compatibility, but their declared tier
    must still match the current natural slot. Every rejection happens before
    the run guard, network calls, log writes, or database writes.
    """
    current_cycle = ledger.cycle_id_for()
    cycle = requested_cycle or current_cycle
    if not NATURAL_CYCLE_RE.fullmatch(cycle):
        raise ValueError(
            "cycle must be YYYY-MM-DDTHH:(00|15|30|45); "
            f"observed={cycle!r}")
    if cycle != current_cycle:
        raise ValueError(
            f"cycle is not the current natural slot: requested={cycle} "
            f"current={current_cycle}")
    expected_tier = "hourly" if cycle.endswith(":00") else "quarter"
    if tier != expected_tier:
        raise ValueError(
            f"tier does not match natural slot: cycle={cycle} "
            f"expected={expected_tier} observed={tier}")
    return cycle


def _guard_key(tier: str, cycle: str) -> str:
    return f"{tier}-{cycle.replace(':', '-')}"


def _guard_paths(guard_dir: Path, tier: str, cycle: str) -> tuple[Path, Path]:
    key = _guard_key(tier, cycle)
    return guard_dir / f"{key}.lock", guard_dir / f"{key}.receipt.json"


def _pid_is_alive(pid: int) -> bool:
    """Read-only PID liveness probe; never signals or terminates a process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            process_query_limited_information, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _valid_success_receipt(path: Path, tier: str, cycle: str) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if (
        data.get("schema_version") != RUN_GUARD_SCHEMA_VERSION
        or data.get("tier") != tier
        or data.get("cycle") != cycle
        or data.get("status") != "succeeded"
    ):
        return None
    return data


def _acquire_run_guard(
    guard_dir: Path,
    tier: str,
    cycle: str,
    *,
    stale_after_seconds: int,
) -> dict:
    """Acquire one cross-scheduler cycle lock or return a safe duplicate state."""
    try:
        guard_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"status": "error", "error": f"guard_dir: {exc}"[:300]}
    lock_path, receipt_path = _guard_paths(guard_dir, tier, cycle)
    receipt = _valid_success_receipt(receipt_path, tier, cycle)
    if receipt is not None:
        return {
            "status": "duplicate_completed",
            "receipt": str(receipt_path),
            "completed_at": receipt.get("completed_at"),
        }

    def _try_create() -> dict:
        token = secrets.token_hex(16)
        payload = {
            "schema_version": RUN_GUARD_SCHEMA_VERSION,
            "tier": tier,
            "cycle": cycle,
            "pid": os.getpid(),
            "token": token,
            "created_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            fd = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError:
            return {"status": "exists"}
        except OSError as exc:
            return {"status": "error", "error": f"guard_lock: {exc}"[:300]}
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.write("\n")
        except Exception:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return {
            "status": "acquired",
            "lock_path": str(lock_path),
            "receipt_path": str(receipt_path),
            "token": token,
        }

    created = _try_create()
    if created["status"] != "exists":
        return created

    # A previous owner can finish between the first receipt check and the
    # exclusive-create attempt.  Prefer its immutable success receipt.
    receipt = _valid_success_receipt(receipt_path, tier, cycle)
    if receipt is not None:
        return {
            "status": "duplicate_completed",
            "receipt": str(receipt_path),
            "completed_at": receipt.get("completed_at"),
        }
    try:
        age_seconds = max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        age_seconds = 0.0
    try:
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        owner_pid = int(owner.get("pid")) if isinstance(owner, dict) else -1
    except (OSError, ValueError, TypeError):
        owner_pid = -1
    if _pid_is_alive(owner_pid) or age_seconds < stale_after_seconds:
        return {
            "status": "duplicate_running",
            "owner_pid": owner_pid if owner_pid > 0 else None,
            "lock_age_seconds": round(age_seconds, 3),
        }

    # Dead and older than the full tier budget: reclaim only this exact-cycle
    # lock, then repeat O_EXCL.  No broad/glob deletion is used.
    try:
        lock_path.unlink()
    except OSError as exc:
        return {"status": "error", "error": f"stale_guard: {exc}"[:300]}
    retried = _try_create()
    if retried["status"] == "exists":
        return {"status": "duplicate_running", "owner_pid": None}
    return retried


def _write_success_receipt(guard: dict, tier: str, cycle: str,
                           started_at: str) -> str | None:
    receipt_path = Path(str(guard["receipt_path"]))
    payload = {
        "schema_version": RUN_GUARD_SCHEMA_VERSION,
        "tier": tier,
        "cycle": cycle,
        "status": "succeeded",
        "started_at": started_at,
        "completed_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
    }
    tmp_path = receipt_path.with_name(
        f"{receipt_path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp_path, receipt_path)
        return None
    except OSError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f"run_guard_receipt_write_failed: {exc}"[:300]


def _release_run_guard(guard: dict | None) -> None:
    if not guard or guard.get("status") != "acquired":
        return
    path = Path(str(guard.get("lock_path")))
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
        if owner.get("token") != guard.get("token"):
            return
        path.unlink()
    except (OSError, ValueError, TypeError):
        return


def _last_json(text: str):
    """取 stdout 末尾的 JSON 文档。

    fast/slow 输出单行 JSON（前可有 WARN 行）；news_collect 输出 indent=2 多行
    JSON——故自底向上找每个以 "{" 开头的行，尝试把「该行到结尾」整段解析，
    先成者胜（天然命中最后一个完整 JSON 文档）。
    """
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].lstrip().startswith("{"):
            continue
        try:
            return json.loads("\n".join(lines[i:]))
        except json.JSONDecodeError:
            continue
    return None


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """终止超时子任务及其后代（fast_collect 同款：子脚本还会拉孙进程，只杀直接
    子进程会留孙进程持有 stdout 管道，communicate() 二次阻塞，超时失效）。"""
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
    """本 runner 已由 run_okx_python.ps1 启动，sys.executable 与 env/PYTHONPATH/
    代理/凭证均已受控；内层直接起 Python（fast_collect 2026-08-05 同款修法，
    消 pwsh→python 管道继承导致超时杀不净的问题）。"""
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
    except Exception as e:  # noqa: BLE001 —— spawn 失败也要能汇总归因
        if proc is not None and proc.poll() is None:
            _terminate_process_tree(proc)
        return {"name": name, "ok": False, "rc": -1,
                "dur_s": round(time.time() - t0, 2),
                "stderr_tail": f"spawn_failed: {e}"[:500]}


def _run_hourly_tail(
    db_root: str,
    cycle: str,
    *,
    news_timeout: int,
    slow_timeout: int,
) -> tuple[dict, dict]:
    """Run independent hourly news/slow branches on one critical path.

    Fast has already finished, so there is no concurrent market.db writer.
    News writes news.db and slow writes market/regime/account; their only shared
    write is a short ledger.db transaction, whose connector is WAL with a five
    second busy timeout. Slow's nudge is deferred so analysis cannot start until
    both branches have returned.
    """
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="collect-hourly") as pool:
        news_future = pool.submit(
            run_step,
            "news",
            COLLECTORS / "sources" / "news_collect.py",
            ["--apply", "--db-root", db_root],
            news_timeout,
        )
        slow_future = pool.submit(
            run_step,
            "slow",
            COLLECTORS / "slow_collect.py",
            [
                "--db-root", db_root,
                "--cycle", cycle,
                "--defer-dispatch-nudge",
            ],
            slow_timeout,
        )
        # Preserve public/log ordering even if slow returns first.
        return news_future.result(), slow_future.result()


def _nudge_after_hourly_tail(db_root: str, slow_step: dict) -> dict:
    """Nudge once after both hourly branches finish; fail closed to cron fallback."""
    if _nudge_mod is None:
        return {"nudged": False, "reason": "module_unavailable"}
    payload = slow_step.get("payload")
    if not isinstance(payload, dict):
        return {"nudged": False, "reason": "slow_payload_missing"}
    statuses = [payload.get("status_slow"), payload.get("status_regime")]
    if any(status is None for status in statuses):
        return {"nudged": False, "reason": "slow_status_missing"}
    return _nudge_mod.nudge_from_collector(
        "collect_cycle_hourly_complete",
        db_root,
        statuses,
        dry_collect=False,
    )


def _step_error(step: dict) -> str | None:
    """提取可读短错误：payload.error > stderr_tail > rc（照 fast_collect）。"""
    if step.get("ok"):
        return None
    payload = step.get("payload")
    detail = payload.get("error") if isinstance(payload, dict) else None
    if not detail:
        detail = (step.get("stderr_tail") or "").strip()
    if not detail:
        detail = f"rc={step.get('rc')}"
    return f"{step.get('name', 'unknown')}: {detail}"[:500]


def _news_verdict(step: dict) -> tuple[bool, list[str]]:
    """news 步终判：(ok, warnings)。

    rc!=0（崩溃/超时/spawn 失败）→ 失败。rc==0 时看逐源结果：到期源全 failed →
    失败（整链断网/代理全挂必须外显）；部分 failed → ok + warnings（逐源 err
    已由 news_collect 落账 collection_runs，这里只带简因方便 cron 面板直读）。
    """
    if not step.get("ok"):
        return False, []
    payload = step.get("payload")
    sources = (payload or {}).get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        return True, []
    attempted = [s for s in sources if s.get("status") not in ("skipped",)]
    failed = [s for s in attempted if s.get("status") == "failed"]
    warnings = [
        f"news:{s.get('id')}: {s.get('err') or 'failed'}"[:200] for s in failed
    ]
    if attempted and len(failed) == len(attempted):
        step["ok"] = False
        step["all_sources_failed"] = True
        return False, warnings
    return True, warnings


def _fast_warnings(step: dict) -> list[str]:
    """把 fast 业务降级外显，但不把可继续派发的 degraded 当进程失败。"""
    if not step.get("ok"):
        return []
    payload = step.get("payload")
    if not isinstance(payload, dict) or payload.get("status") != "degraded":
        return []
    details = payload.get("warnings")
    if isinstance(details, list):
        clean = [str(value).strip() for value in details if str(value).strip()]
    else:
        clean = []
    if not clean and payload.get("error"):
        clean = [str(payload["error"]).strip()]
    detail = "; ".join(clean) or "enhancement quality gate degraded"
    step["degraded"] = True
    return [f"fast:degraded: {detail}"[:500]]


def _slim_for_log(out: dict) -> dict:
    """JSONL 行瘦身：ok 步骤去 payload（成功细节子脚本已各自落账），失败步骤
    全量留档。.jsonl 在 log_rotate 的 PROTECT_SUFFIX 保护内永不轮转（审计类
    单独管），体量靠这里控（~96 行/天 × 通常 <1KB）。"""
    steps = []
    for step in out["steps"]:
        if not step.get("ok"):
            steps.append(step)
            continue
        slim = {k: v for k, v in step.items() if k != "payload"}
        payload = step.get("payload")
        if (
            step.get("name") == "fast"
            and isinstance(payload, dict)
            and isinstance(payload.get("data_quality"), dict)
        ):
            slim["data_quality"] = payload["data_quality"]
        steps.append(slim)
    return {**out, "steps": steps}


def _append_run_log(log_dir: Path, record: dict) -> str | None:
    """追加单行 JSONL 到 logs/collect/collect_cycle_YYYYMMDD.jsonl（按日分文件）。

    写失败不影响退出码（账本与 cron diagnostics 仍是主记录），返回错误串供
    stdout 汇总外显。
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fname = f"collect_cycle_{datetime.now(CST).strftime('%Y%m%d')}.jsonl"
        with open(log_dir / fname, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return None
    except Exception as e:  # noqa: BLE001
        return f"run_log_write_failed: {e}"[:200]


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 采集聚合 runner（系统层）")
    ap.add_argument("--tier", required=True, choices=("hourly", "quarter"))
    ap.add_argument(
        "--cycle",
        help=("钉定本次当前 UTC+8 自然 15 分钟槽；仅接受当前槽，"
              "过期/未来/层级不符均在联网和写库前拒绝"),
    )
    ap.add_argument("--db-root", default=str(ROOT / "db"))
    ap.add_argument("--fast-timeout", type=int, default=360)
    ap.add_argument("--news-timeout", type=int, default=300)
    ap.add_argument("--slow-timeout", type=int, default=480)
    ap.add_argument("--log-dir", default=str(ROOT / "logs" / "collect"))
    ap.add_argument(
        "--guard-dir", default=str(ROOT / "logs" / "collect" / "guards"),
        help="跨调度器同周期单实例锁与成功回执目录",
    )
    ap.add_argument("--dry-collect", action="store_true",
                    help="透传 fast/slow（不联网不写生产）；news 无 dry 模式改跳过")
    args = ap.parse_args()

    t0 = time.time()
    started_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        cycle = _resolve_natural_cycle(args.tier, args.cycle)
    except ValueError as exc:
        print(json.dumps({
            "ok": False,
            "tier": args.tier,
            "cycle": args.cycle,
            "ts": started_at,
            "latency_ms": int((time.time() - t0) * 1000),
            "failed": ["natural_cycle_guard"],
            "warnings": [],
            "steps": [],
            "error": str(exc),
            "network_started": False,
            "database_writes": 0,
        }, ensure_ascii=False))
        return 2

    db_root = str(args.db_root)
    log_dir = Path(args.log_dir)

    # 03:15 可由 Windows 硬触发与 OpenClaw 延迟兜底同时覆盖。真实采集必须先
    # 赢得同周期 O_EXCL 锁；dry 模式刻意不创建生产回执，避免随后真实轮被误跳过。
    guard: dict | None = None
    if not args.dry_collect:
        tier_budget = args.fast_timeout + args.news_timeout + 120
        if args.tier == "hourly":
            tier_budget = (
                args.fast_timeout
                + max(args.news_timeout, args.slow_timeout)
                + 120
            )
        guard = _acquire_run_guard(
            Path(args.guard_dir),
            args.tier,
            cycle,
            stale_after_seconds=tier_budget,
        )
        if guard.get("status") in {
            "duplicate_completed", "duplicate_running",
        }:
            out = {
                "ok": True,
                "tier": args.tier,
                "cycle": cycle,
                "ts": started_at,
                "latency_ms": int((time.time() - t0) * 1000),
                "duplicate_skip": guard.get("status"),
                "run_guard": {
                    key: value for key, value in guard.items()
                    if key not in {"token", "lock_path", "receipt_path"}
                },
                "failed": [],
                "warnings": [],
                "steps": [],
            }
            log_err = _append_run_log(log_dir, _slim_for_log(out))
            if log_err:
                out["warnings"].append(log_err)
            print(json.dumps(out, ensure_ascii=False))
            return 0
        if guard.get("status") != "acquired":
            out = {
                "ok": False,
                "tier": args.tier,
                "cycle": cycle,
                "ts": started_at,
                "latency_ms": int((time.time() - t0) * 1000),
                "failed": ["run_guard"],
                "warnings": [],
                "steps": [],
                "error": str(guard.get("error") or "run guard unavailable")[:300],
            }
            _append_run_log(log_dir, _slim_for_log(out))
            print(json.dumps(out, ensure_ascii=False))
            return 1

    steps: list[dict] = []
    warnings: list[str] = []
    try:
        fast_args = ["--db-root", db_root, "--cycle", cycle]
        if args.dry_collect:
            fast_args.append("--dry-collect")
        fast_step = run_step("fast", COLLECTORS / "fast_collect.py",
                             fast_args, args.fast_timeout)
        warnings.extend(_fast_warnings(fast_step))
        steps.append(fast_step)

        deferred_dispatch_nudge = None
        hourly_parallel_tail = args.tier == "hourly" and not args.dry_collect
        if hourly_parallel_tail:
            news_step, slow_step = _run_hourly_tail(
                db_root,
                cycle,
                news_timeout=args.news_timeout,
                slow_timeout=args.slow_timeout,
            )
            _ok, news_warnings = _news_verdict(news_step)
            warnings.extend(news_warnings)
            steps.extend((news_step, slow_step))
            deferred_dispatch_nudge = _nudge_after_hourly_tail(
                db_root, slow_step)
            if deferred_dispatch_nudge.get("reason") in {
                "module_unavailable", "slow_payload_missing", "slow_status_missing",
            }:
                warnings.append(
                    "hourly deferred dispatch nudge skipped: "
                    f"{deferred_dispatch_nudge['reason']} (cron fallback retained)"
                )
        elif args.dry_collect:
            steps.append({"name": "news", "ok": True, "rc": None,
                          "dur_s": 0.0,
                          "skipped": "dry-collect（news 无 dry 模式）"})
        else:
            news_step = run_step(
                "news", COLLECTORS / "sources" / "news_collect.py",
                ["--apply", "--db-root", db_root], args.news_timeout)
            _ok, news_warnings = _news_verdict(news_step)
            warnings.extend(news_warnings)
            steps.append(news_step)

        if args.tier == "hourly" and not hourly_parallel_tail:
            slow_args = ["--db-root", db_root, "--cycle", cycle]
            if args.dry_collect:
                slow_args.append("--dry-collect")
            steps.append(run_step("slow", COLLECTORS / "slow_collect.py",
                                  slow_args, args.slow_timeout))

        failed = [s["name"] for s in steps if not s.get("ok")]
        out = {
            "ok": not failed,
            "tier": args.tier,
            "cycle": cycle,
            "ts": started_at,
            "latency_ms": int((time.time() - t0) * 1000),
            "failed": failed,
            "warnings": warnings,
            "steps": steps,
            "hourly_parallel_tail": hourly_parallel_tail,
            "deferred_dispatch_nudge": deferred_dispatch_nudge,
        }
        if not failed and guard is not None:
            receipt_error = _write_success_receipt(
                guard, args.tier, cycle, started_at)
            if receipt_error:
                out["ok"] = False
                out["failed"].append("run_guard_receipt")
                out["warnings"].append(receipt_error)
        log_err = _append_run_log(log_dir, _slim_for_log(out))
        if log_err:
            out["warnings"].append(log_err)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out["ok"] else 1
    finally:
        _release_run_guard(guard)


if __name__ == "__main__":
    raise SystemExit(main())
