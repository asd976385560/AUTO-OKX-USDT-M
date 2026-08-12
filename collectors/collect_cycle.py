# -*- coding: utf-8 -*-
r"""V2.0 采集聚合入口（2026-08-08 cron 整并：3 条采集 cron → 2 条聚合）。

由 OpenClaw 命令型 cron 调度（经 run_okx_python.ps1 wrapper）：
  okx-collect-hourly   `0 * * * *`        --tier hourly   fast → news → slow
  okx-collect-quarter  `15,30,45 * * * *` --tier quarter  fast → news

步序即派发时延设计：fast 先跑、落账即 nudge——:15/:30/:45 槽（gate 只必需 fast）
的 unified live 派发时延与拆分时代完全一致；slow 压轴只影响 :00 槽（gate 另需
slow+regime），trader 约晚 1-2min 起。news 不进派发闸（ledger.expected_sources），
排中间只为串行化外网请求（取代 2026-07-02/03 的错峰挪槽）。

子脚本各自负责：账本落账（fast/slow → collection_runs[fast|slow|regime]，
news_collect → 逐源 collection_runs[<source_id>] 带 err）+ 落账后 _dispatch_nudge。
本 runner 不写账本、不 nudge，只编排 + 汇总 + 追加 JSONL 运行日志
（logs/collect/collect_cycle_YYYYMMDD.jsonl，随 log_rotate --dirs 含 collect 7 天轮转）。

步序 fail-safe（照 daily_maintenance）：一步失败/超时不阻断下一步；任一步失败
聚合 exit 1（cron 记 error 外显、failureAlert 计数），失败原因进 stdout JSON +
JSONL + 子脚本自身账本，不自动重试。news 判定特例：news_collect 逐源隔离恒
rc=0，仅本体崩溃/超时才非 0；runner 额外把「全部到期源 failed」也判步失败
（如实外显断网/代理全挂），部分源失败只进 warnings 不置 error（与旧独立
okx-news-rss cron 的告警灵敏度一致）。news 无论如何不阻断后续 slow，不碰交易链。

超时预算：fast 360s + news 300s + slow 480s = 1140s < hourly cron 1200s；
quarter 660s < 900s。真正的功能死线是 15min 槽界 + gate 900s 新鲜窗，cron
超时只是防挂死兜底。cycle 归槽：fast 显式 --cycle 钉 runner 启动槽（防链内
漂移错槽）；slow 用自身「固定归当前小时 :00」默认（历史语义不动）；news 自算
（无 --cycle 参数，链内漂移最多损当轮 60/120min 到期源，下轮自愈）。

tmp 验证：--dry-collect 透传 fast/slow（不联网不写生产），news 无 dry 模式改跳过；
非生产 db-root 时子脚本的 nudge 自带闸门不发。零模型名（红线①）；UTF-8 无 BOM。
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

sys.path.insert(0, r"./collectors")
import ledger          # noqa: E402  cycle_id_for

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(r".")
COLLECTORS = ROOT / "collectors"
CST = timezone(timedelta(hours=8))
# 子进程隐藏窗口：cron 经 wrapper 无窗口起本脚本，console 子进程默认新开可见窗口——抑制
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


def _slim_for_log(out: dict) -> dict:
    """JSONL 行瘦身：ok 步骤去 payload（成功细节子脚本已各自落账），失败步骤
    全量留档。.jsonl 在 log_rotate 的 PROTECT_SUFFIX 保护内永不轮转（审计类
    单独管），体量靠这里控（~96 行/天 × 通常 <1KB）。"""
    return {**out, "steps": [
        ({k: v for k, v in s.items() if k != "payload"} if s.get("ok") else s)
        for s in out["steps"]
    ]}


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
    ap.add_argument("--db-root", default=str(ROOT / "db"))
    ap.add_argument("--fast-timeout", type=int, default=360)
    ap.add_argument("--news-timeout", type=int, default=300)
    ap.add_argument("--slow-timeout", type=int, default=480)
    ap.add_argument("--log-dir", default=str(ROOT / "logs" / "collect"))
    ap.add_argument("--dry-collect", action="store_true",
                    help="透传 fast/slow（不联网不写生产）；news 无 dry 模式改跳过")
    args = ap.parse_args()

    db_root = str(args.db_root)
    cycle = ledger.cycle_id_for()  # 钉 runner 启动槽，传 fast 防链内漂移
    t0 = time.time()
    started_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    steps: list[dict] = []
    warnings: list[str] = []

    fast_args = ["--db-root", db_root, "--cycle", cycle]
    if args.dry_collect:
        fast_args.append("--dry-collect")
    steps.append(run_step("fast", COLLECTORS / "fast_collect.py",
                          fast_args, args.fast_timeout))

    if args.dry_collect:
        steps.append({"name": "news", "ok": True, "rc": None, "dur_s": 0.0,
                      "skipped": "dry-collect（news 无 dry 模式）"})
    else:
        news_step = run_step("news", COLLECTORS / "sources" / "news_collect.py",
                             ["--apply", "--db-root", db_root],
                             args.news_timeout)
        _ok, news_warnings = _news_verdict(news_step)
        warnings.extend(news_warnings)
        steps.append(news_step)

    if args.tier == "hourly":
        slow_args = ["--db-root", db_root]
        if args.dry_collect:
            slow_args.append("--dry-collect")
        steps.append(run_step("slow", COLLECTORS / "slow_collect.py",
                              slow_args, args.slow_timeout))

    failed = [s["name"] for s in steps if not s.get("ok")]
    errors = [err for s in steps if (err := _step_error(s))]
    out = {
        "ok": not failed,
        "tier": args.tier,
        "cycle": cycle,
        "ts": started_at,
        "latency_ms": int((time.time() - t0) * 1000),
        "failed": failed,
        "warnings": warnings,
        "steps": steps,
    }
    log_err = _append_run_log(Path(args.log_dir), _slim_for_log(out))
    if log_err:
        out["warnings"] = [*warnings, log_err]
    print(json.dumps(out, ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
