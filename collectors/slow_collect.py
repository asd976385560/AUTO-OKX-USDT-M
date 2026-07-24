# -*- coding: utf-8 -*-
"""V2.0 慢采脚本（系统层，零 agent）。

由 OpenClaw 命令型 cron `okx-slow-collect` 每小时 :02 调度，归入 :00 槽。

包装现有 collect_slow.py（**原样不改**），结尾：
  1. 写账本 collection_runs(cycle_id, 'slow', status)
  2. 写账本 collection_runs(cycle_id, 'regime', status)
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


def _verify_regime_write(db_root: Path, cycle_id: str) -> tuple[str, str | None]:
    """验证 collect_slow.py 是否真的往 regime.db 写了新行。

    collect_slow.py 的 macro 块异常被 catch 后脚本仍 exit 0，
    但 cross_market INSERT 可能没执行。此函数查 regime.db 最新行的 ts，
    如果距今超过 10 分钟（正常 latency ~4min），则判定 regime 写入失败。

    返回 (status, err_detail)：status 只取 collection_runs 声明枚举
    ok|degraded|error，具体明细写账本 err 列。
    """
    import sqlite3 as _sqlite3
    regime_path = db_root / "regime.db"
    if not regime_path.exists():
        return "degraded", "regime.db 不存在"
    try:
        con = _sqlite3.connect(f"file:{regime_path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT ts FROM cross_market ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            return "degraded", "cross_market 无行/ts 为空"
        # cross_market.ts 是 UTC ISO 格式（如 '2026-06-29T01:02:07Z'）
        ts_str = row[0]
        try:
            # 解析 UTC ISO 时间戳
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            ts_cst = ts.astimezone(CST)
        except (ValueError, TypeError):
            return "degraded", f"cross_market.ts 不可解析: {ts_str!r}"
        now = datetime.now(CST)
        age_sec = int((now - ts_cst).total_seconds())
        # 正常 latency ~4min（slow_collect 跑 ~247s），给 10min 容差
        if age_sec > 600:
            return "error", f"regime stale age={age_sec}s"
        return "ok", None
    except Exception as e:
        return "error", f"verify 异常: {e}"


def run_step(name: str, script: Path, sargs: list[str], timeout: int) -> dict:
    cmd = ["pwsh", "-NoProfile", "-File", str(WRAPPER), str(script), *sargs]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        out = {"name": name, "ok": p.returncode == 0, "rc": p.returncode,
               "dur_s": round(time.time() - t0, 2),
               "stderr_tail": (p.stderr or "")[-500:]}
        # collect_slow.py 的降级 WARN/FAIL 走 stdout；提取含 [WARN]/[FAIL] 的行
        #（尾 5 行、每行截 200 字）。
        # 并入 summary；禁灌全量 stdout（summary 有体积上限风险）。
        warn_lines = [ln.strip()[:200] for ln in (p.stdout or "").splitlines()
                      if "[WARN]" in ln or "[FAIL]" in ln]
        if warn_lines:
            out["warn_tail"] = warn_lines[-5:]
        return out
    except subprocess.TimeoutExpired:
        return {"name": name, "ok": False, "rc": 124,
                "dur_s": round(time.time() - t0, 2), "stderr_tail": "timeout"}
    except Exception as e:  # noqa: BLE001 —— OSError/FileNotFoundError 等 spawn 失败：
        # 捕获 spawn 失败，确保 main 仍能写 collection_runs 归因。
        return {"name": name, "ok": False, "rc": -1,
                "dur_s": round(time.time() - t0, 2),
                "stderr_tail": f"spawn_failed: {e}"[:500]}


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 慢采（系统层）")
    ap.add_argument("--db-root", default=str(ROOT / "db"))
    ap.add_argument("--cycle", default=None, help="覆盖 cycle_id（默认按当前时刻归槽）")
    # 子步骤超时必须小于 cron 总超时，保证 record_collection 有时间执行。
    # → :00 槽 slow/regime 账本无行、丢轮无归因。收到 450s：wrapper 启动 ~1-3s + 步级 450s
    # + regime 验证/记账/nudge ~1s，最坏 <460s < 480s 必能走到落账（正常 ~220s 不受影响）。
    ap.add_argument("--collect-timeout", type=int, default=450)
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
    steps = []
    regime_err = None
    if args.dry_collect:
        slow_status, regime_status = "ok", "ok"
    else:
        step_result = run_step("collect_slow", SCRIPTS / "collect_slow.py",
                               ["--db-root", str(db_root)],
                               args.collect_timeout)
        steps.append(step_result)
        rc = step_result["rc"]
        # collect_slow.py rc 契约：0=ok；2=degraded（部分块失败但不阻断）；其他=error。
        if rc == 0:
            slow_status = "ok"
        elif rc == 2:
            slow_status = "degraded"
        else:
            slow_status = "error"
        # regime 独立验证解耦：只要 collect_slow 真跑完(rc 0/2)就验 regime.db 是否有新行；
        # 崩溃/超时(其它 rc)则 regime 记 degraded。
        if rc in (0, 2):
            regime_status, regime_err = _verify_regime_write(db_root, cycle)
            if regime_status != "ok":
                print(f"[slow_collect] regime.db 验证失败: {regime_status} ({regime_err})",
                      flush=True)
        else:
            regime_status = "degraded"
    latency_ms = int((time.time() - t0) * 1000)

    # 两行账本：slow + regime。
    ledger.record_collection(ledger_db, cycle, ledger.SRC_SLOW, slow_status,
                             latency_ms=latency_ms)
    ledger.record_collection(ledger_db, cycle, ledger.SRC_REGIME, regime_status,
                             latency_ms=latency_ms, err=regime_err)

    # HANDOFF-4B（2026-07-17）：落账后事件通知 dispatcher（:00 槽 slow/regime 是 gate 必需源，
    # 本 nudge 是该槽的有效触发；fast 侧同款）。三道采集侧门 + nudge() 四道守护闸在
    # _dispatch_nudge 内；非致命，不改本脚本退出码。
    if _nudge_mod is not None:
        _nudge_mod.nudge_from_collector("slow_collect", args.db_root,
                                        [slow_status, regime_status],
                                        dry_collect=args.dry_collect)

    out = {"ok": slow_status in ledger.DONE_STATUS, "cycle": cycle,
           "status_slow": slow_status, "status_regime": regime_status,
           "latency_ms": latency_ms,
           "steps": [{k: s[k] for k in ("name", "ok", "rc", "dur_s", "warn_tail")
                      if k in s} for s in steps],
           "dispatch": None}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if slow_status in ledger.DONE_STATUS else 1


if __name__ == "__main__":
    raise SystemExit(main())
