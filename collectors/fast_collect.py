# -*- coding: utf-8 -*-
"""V2.0 快采脚本（系统层，零 agent）。

由 OpenClaw 命令型 cron `okx-fast-collect` 在 :00/:15/:30/:45 调度。

包装现有 collect_data.py + jobb_live_account_check.py（**原样不改**），结尾：
  1. 写账本 collection_runs(cycle_id, 'fast', status)
  2. （可选）X 搜索 → 写账本 'x_search'（失败不阻断快采主体，§2）
  3. 通过 _dispatch_nudge 通知 core/dispatcher.py；定时 dispatcher 负责兜底

与生产隔离：默认 --db-root <PROJECT_ROOT>\\db；tmp 验证传临时目录。--dry-collect 跳过真采集
（不联网、不写生产），只验账本+触发 plumbing。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
import ledger          # noqa: E402

try:  # HANDOFF-4B 采集侧事件通知（可缺省，守卫式导入照 analyst_writer 惯例）
    import _dispatch_nudge as _nudge_mod  # noqa: E402
except Exception:  # noqa: BLE001
    _nudge_mod = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(_project_path())
SCRIPTS = ROOT / "scripts"
WRAPPER = SCRIPTS / "run_okx_python.ps1"
CST = timezone(timedelta(hours=8))
# 子进程隐藏窗口：本脚本被 wscript 以无窗口起，console 子进程(pwsh)默认会新开可见窗口——加此 flag 抑制
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


def run_step(name: str, script: Path, sargs: list[str], timeout: int) -> dict:
    cmd = ["pwsh", "-NoProfile", "-File", str(WRAPPER), str(script), *sargs]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return {"name": name, "ok": p.returncode == 0, "rc": p.returncode,
                "dur_s": round(time.time() - t0, 2),
                "payload": _last_json(p.stdout or ""),
                "stderr_tail": (p.stderr or "")[-500:]}
    except subprocess.TimeoutExpired:
        return {"name": name, "ok": False, "rc": 124,
                "dur_s": round(time.time() - t0, 2), "stderr_tail": "timeout"}
    except Exception as e:  # noqa: BLE001 —— OSError/FileNotFoundError 等 spawn 失败：
        # 捕获 spawn 失败，确保 main 仍能写 collection_runs 归因。
        return {"name": name, "ok": False, "rc": -1,
                "dur_s": round(time.time() - t0, 2),
                "stderr_tail": f"spawn_failed: {e}"[:500]}


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 快采（系统层）")
    ap.add_argument("--db-root", default=str(ROOT / "db"))
    ap.add_argument("--profile", default="live")
    ap.add_argument("--cycle", default=None, help="覆盖 cycle_id（默认按当前时刻归槽）")
    # 步级默认超时 + 全程预算钳（见 main 内 deadline），保证账本写入与派发
    # 能在 cron 超时前完成。
    ap.add_argument("--collect-timeout", type=int, default=175)  # 观测 max≈171s（Dreaming 窗），留 4s
    ap.add_argument("--account-timeout", type=int, default=45)
    ap.add_argument("--features-timeout", type=int, default=40)
    ap.add_argument("--total-budget", type=int, default=205,
                    help="全部采集步骤的总时长预算（秒），须 < cron timeoutSeconds=240 留出记账+派发余量")
    ap.add_argument("--dry-collect", action="store_true",
                    help="跳过真采集（不联网/不写生产），只验账本+触发")
    # 生产 cron 仍可能携带该历史参数；保留为无副作用兼容参数，派发始终走 nudge+dispatcher。
    ap.add_argument("--no-dispatch", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    db_root = Path(args.db_root)
    ledger_db = db_root / "ledger.db"
    ledger.init_ledger(ledger_db)
    cycle = args.cycle or ledger.cycle_id_for()

    t0 = time.time()
    deadline = t0 + args.total_budget

    def budget(default_timeout: int) -> int:
        """步级超时 = min(默认, 剩余预算)，兜底 12s——保证总时长 <= total_budget+min 级溢出，
        cron 240s 内应能走到 record_collection。"""
        return max(12, min(default_timeout, int(deadline - time.time())))

    steps = []
    if args.dry_collect:
        status, rows = "ok", 0
    else:
        steps.append(run_step("collect_data", SCRIPTS / "collect_data.py",
                              ["--profile", args.profile, "--db-root", str(db_root),
                               "--skip-news"],
                              budget(args.collect_timeout)))
        # 50档订单簿 + 最近逐笔成交影子特征。失败只把 fast 标 degraded，不阻断主行情：
        # analyst 仍可按既有 tick/kline/funding 运行，且 missing_sources 可见。
        steps.append(run_step(
            "market_features", SCRIPTS / "collect_market_features.py",
            ["--db-root", str(db_root), "--depth", "50", "--cycle", cycle],
            budget(args.features_timeout),
        ))
        steps.append(run_step("live_account_check", SCRIPTS / "jobb_live_account_check.py",
                              ["--profile", args.profile, "--db-root", str(db_root)],
                              budget(args.account_timeout)))
        primary_ok = steps[0]["ok"]
        all_ok = all(s["ok"] for s in steps)
        status = "ok" if all_ok else ("degraded" if primary_ok else "error")
        rows = 0
        for step in steps:
            wrote = (step.get("payload") or {}).get("wrote")
            if isinstance(wrote, dict):
                rows += sum(int(v or 0) for v in wrote.values() if isinstance(v, (int, float)))
        if rows == 0:
            rows = None
        # demo 权益快照随快采刷新，避免 account_snapshots 依赖交易会话更新。
        # 内部硬编码 --profile demo（与 fast_collect 的 live profile 互不干扰）。
        # 退出码契约：0=一致、1=仅 pnl 待回填、3=账实差异、其他=错误。
        # 只有 rc∈(0,1) 视为 ok；demo step 不纳入 all_ok/status，不阻断 live 主链。
        demo_step = run_step("demo_account_check", SCRIPTS / "demo_account_check.py",
                             ["--db-root", str(db_root)], budget(args.account_timeout))
        demo_step["ok"] = demo_step["rc"] in (0, 1)
        steps.append(demo_step)
        # 2026-07-03 C4b 修：demo position_snapshots 自 06-11 停写——全系统没有任何定时任务
        # 以 demo profile 调 position 快照 writer（demo_account_check 只写 account_snapshots）。
        # 补上 jobb --profile demo（现成 live 验证过的 writer：position_snapshots + 消失仓对账；
        # 06-10 陈旧批的一次性 reconcile 补记已于 2026-07-03 手动首跑完成、复跑验证幂等）。
        # 同 demo_account_check：不纳入 all_ok/status（demo 失败不阻断 live 主链）。
        demo_pos_step = run_step("demo_position_check",
                                 SCRIPTS / "jobb_live_account_check.py",
                                 ["--profile", "demo", "--db-root", str(db_root)],
                                 budget(args.account_timeout))
        steps.append(demo_pos_step)
    latency_ms = int((time.time() - t0) * 1000)
    ledger.record_collection(ledger_db, cycle, ledger.SRC_FAST, status,
                             rows=rows, latency_ms=latency_ms)

    # HANDOFF-4B（2026-07-17）：落账后事件通知 dispatcher——消「采集完成→analyst 派发」的
    # 0-2min tick 等待。三道采集侧门（dry-collect/非生产 db-root/status 非 ok|degraded 不发）
    # + nudge() 四道守护闸都在 _dispatch_nudge 内；非致命，不改本脚本退出码。
    if _nudge_mod is not None:
        _nudge_mod.nudge_from_collector("fast_collect", args.db_root, [status],
                                        dry_collect=args.dry_collect)

    out = {"ok": status in ledger.DONE_STATUS, "cycle": cycle, "source": "fast",
           "status": status, "latency_ms": latency_ms,
           "steps": [{k: s[k] for k in ("name", "ok", "rc", "dur_s")} for s in steps],
           "dispatch": None}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if status in ledger.DONE_STATUS else 1


if __name__ == "__main__":
    raise SystemExit(main())
