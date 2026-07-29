# -*- coding: utf-8 -*-
"""HANDOFF-4A（2026-07-16 主人拍板）：writer 落库成功后 detached 拍一次 core/dispatcher.py
（事件驱动派发）——消除 ~2min 轮询 tick 的纯等待（实测 analyst 落库→trader 派发 p50 2.8min /
trader 落库→push 派发 1.2min）。okx-dispatcher cron（*/2min）保持原样作兜底，nudge 是加速器
不是替代品；幂等安全性 = ledger.stage_dispatch 闩锁（双 tick 无害，单 tick 实测 1.7-2.6s）。

四道守护闸（对抗核验 2026-07-16 钉死，缺一即转致命，禁删）：
  1. 暂停语义闸：okx-dispatcher cron enabled=1 才准 spawn（直读 openclaw.sqlite mode=ro，
     读失败/行缺失一律拒发 fail-closed）——保住「停 cron=停派发」不变量：P0 PAUSE 后已启动
     agent 收尾落库不得绕过暂停派 trader 下真单；停用 cron 的测试窗自动静默。
  2. DRYRUN 拒发：env 见 OKX_TRIGGER_DRYRUN（任何值，存在即拒）——dryrun dispatcher 对真库
     干跑仍写 stage_dispatch 闩锁，nudge 出去等于闩锁投毒。
  3. env 白名单：spawn 的 dispatcher 只继承系统基础键，OKX_* 全系 12 个消费键与 MX_APIKEY
     零透传——writer 跑在 LLM agent 会话内，agent shell 层可被注入任意 export，
     OKX_COLLECTORS_DIR（sys.path 劫持）/ OKX_OPENCLAW_BIN（起棒二进制替换）等不得
     延伸进独立派发进程。dispatcher 链纯 stdlib（已核 2026-07-16），白名单功能完备。
  4. 非致命：全函数 try/except，任何失败只 stderr WARN——writer 回执已 commit，nudge 崩
     绝不改变 writer 退出码/stdout JSON 契约（否则 agent 会按失败重喂 writer 撞合并闸）。

spawn 参数照抄 trigger_agent._fire_push_script 生产成熟模式（2026-07-07 起）：
  - 直起原生 python.exe（sys.executable）——**禁 pwsh/.ps1**：DETACHED 下 .ps1 静默不执行；
  - DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP，**不叠 CREATE_NO_WINDOW**（MSDN 互斥，
    叠了反而弹窗，见 trigger_agent.fire() 注释）；
  - stdin=DEVNULL、stdout/stderr → logs/trigger/dispatch_nudge.log（追加，log_rotate 会轮转）。

直调路径（microtest / tests 直调 write_trades/write_analysis）不经 CLI main → 天然无 nudge，
属设计非遗漏。回滚开关：OKX_DISPATCH_NUDGE=0（env 级，不必改 writer）。
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


import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DISPATCHER = _project_path('core', 'dispatcher.py')
_DB_ROOT = _project_path('db')
_LOG = Path(_project_path('logs', 'trigger', 'dispatch_nudge.log'))
_STATE_DB = os.environ.get(
    "OKX_OPENCLAW_STATE_DB",
    str(_ProjectPath.home().joinpath('.openclaw', 'state', 'openclaw.sqlite')))
_DISPATCHER_CRON_NAME = "okx-dispatcher"  # job_id 会随重建漂移，name 稳定（runbook 附录 A 对照表）

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CST = timezone(timedelta(hours=8))

# 守护闸 3：系统基础键白名单（python.exe 与 dispatcher 链 stdlib 所需 + node/openclaw
# 起棒所需的用户目录键）。OKX_* / MX_APIKEY / PYTHON* / *_PROXY 一律不进。
_ENV_WHITELIST = (
    "SystemRoot", "SystemDrive", "windir", "ComSpec", "PATH", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)


def _spawn_env() -> dict:
    env = {}
    for k in _ENV_WHITELIST:
        v = os.environ.get(k)
        if v:
            env[k] = v
    return env


def _dispatcher_cron_enabled() -> bool:
    """守护闸 1：okx-dispatcher cron enabled=1 才 True；读失败/缺行 → False（fail-closed）。"""
    try:
        uri = "file:" + _STATE_DB.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            row = con.execute(
                "SELECT enabled FROM cron_jobs WHERE name=?",
                (_DISPATCHER_CRON_NAME,)).fetchone()
        finally:
            con.close()
        return bool(row) and int(row[0]) == 1
    except Exception:  # noqa: BLE001 —— fail-closed：状态不明=不发，cron 恒为兜底
        return False


def _default_spawn(cmd: list, fh) -> None:
    """真 spawn（模块级独立函数=测试可 patch 缝）。"""
    subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
        creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        cwd=_project_path(), close_fds=True, env=_spawn_env())


def nudge(origin: str) -> dict:
    """detached 起一次 dispatcher。返回 {"nudged": bool, "reason": str}；永不 raise、永不写 stdout。"""
    try:
        if os.environ.get("OKX_DISPATCH_NUDGE", "1") == "0":
            return {"nudged": False, "reason": "disabled"}
        if "OKX_TRIGGER_DRYRUN" in os.environ:
            return {"nudged": False, "reason": "dryrun_env"}
        if not _dispatcher_cron_enabled():
            return {"nudged": False, "reason": "cron_disabled_or_unreadable"}
        cmd = [sys.executable, _DISPATCHER, "--db-root", _DB_ROOT]
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as fh:
            ts = datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S")
            fh.write(f"[{ts}] nudge origin={origin} cmd={' '.join(cmd)}\n")
            fh.flush()
            _default_spawn(cmd, fh)
        return {"nudged": True, "reason": "ok"}
    except Exception as e:  # noqa: BLE001 —— 守护闸 4：非致命
        try:
            sys.stderr.write(f"[dispatch_nudge][WARN] skipped (non-fatal): {e}\n")
        except Exception:  # noqa: BLE001
            pass
        return {"nudged": False, "reason": f"spawn_failed: {e}"}


# 采集侧 status 完成集（与 ledger.DONE_STATUS 同值；不 import ledger 保持本模块零依赖）
_COLLECTOR_DONE_STATUS = ("ok", "degraded")


def nudge_from_collector(origin: str, db_root, statuses, dry_collect: bool = False) -> dict:
    """HANDOFF-4B（2026-07-17）：采集器落账后的事件通知入口——消「采集完成→analyst 派发」
    的 0-2min dispatcher tick 等待（fast/slow_collect 落账即拍，dispatcher 自会重验 gate，
    未齐活/不新鲜则 no-op，幂等靠 stage_dispatch 闩锁）。

    在 nudge() 四道守护闸之外加三道采集侧门（缺一即误拍）：
      a. dry_collect 假 ok 行不发（--dry-collect 只验 plumbing，账本行不代表真采集）；
      b. 仅生产 db-root 发——nudge spawn 的 dispatcher 硬编码打 <PROJECT_ROOT>\\db，隔离/tmp
         db-root 的采集落账若外拍会对生产库跑真 tick（幂等无害但破坏隔离语义）；
      c. 至少一个源 status ∈ ok|degraded 才发（error/timeout 轮 gate 必拒，白拍）。
    与 nudge() 同约：永不 raise、永不写 stdout、不改调用方退出码。
    """
    try:
        if dry_collect:
            return {"nudged": False, "reason": "dry_collect"}
        try:
            if Path(db_root).resolve() != Path(_DB_ROOT).resolve():
                return {"nudged": False, "reason": "non_production_db_root"}
        except Exception:  # noqa: BLE001 —— db_root 不可解析=按非生产处理，不发
            return {"nudged": False, "reason": "db_root_unresolvable"}
        sts = statuses if isinstance(statuses, (list, tuple, set)) else [statuses]
        if not any(str(s) in _COLLECTOR_DONE_STATUS for s in sts):
            return {"nudged": False, "reason": "no_done_status"}
        return nudge(origin)
    except Exception as e:  # noqa: BLE001 —— 与守护闸 4 同源：非致命
        try:
            sys.stderr.write(f"[dispatch_nudge][WARN] collector nudge skipped (non-fatal): {e}\n")
        except Exception:  # noqa: BLE001
            pass
        return {"nudged": False, "reason": f"error: {e}"}
