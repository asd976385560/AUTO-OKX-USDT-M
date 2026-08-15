# -*- coding: utf-8 -*-
"""子进程超时的共用兜底：整树终止 + 有界回收。

**为什么需要**：Windows 上 `subprocess.run(timeout=)` 超时后只对直接子进程
`TerminateProcess`。若该子进程还有存活的孙进程持有 stdout/stderr 管道
（典型形状：`pwsh -File run_okx_python.ps1` → `python`，或 python 脚本内部
再经 `_okxcli` 拉起 okx npm CLI），第二次 `communicate()` 会阻塞到孙进程自然
退出——**超时形同虚设**。

2026-08-05 实测：`timeout=3` 的调用实耗 **40.6s**；快采因此反复撞满 cron 的
480s 硬超时并整轮丢数据（7/16–8/5 共 22 次，20 次精确 480.1s）。同一缺陷
2026-07-28 已在 `slow_collect` 单独修过一次，但没有回移到 `fast_collect`——
本模块的存在就是为了不再出现「同一逻辑改了一处、漏了另一处」。

只依赖标准库，不读 env、不碰数据库，因此裸 python 起的进程（如
`push_pipeline.py`）也可安全 import。
"""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable

# 子进程隐藏窗口：cron/detached 场景下 console 子进程默认会新开可见窗口。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 超时退出码，对齐 GNU timeout(1)。
RC_TIMEOUT = 124
# 子进程并非超时，而是监督器观察到确定性终态/协议违规后主动整树收口。
# 调用方必须结合自己的 observer 证据把它映射成业务成功或失败，不能把 125
# 直接当成普通子进程退出码。
RC_OBSERVED_STOP = 125
# Popen 已成功，但 communicate/监督/回收发生了非 timeout 异常。调用方可据此
# 区分“命令根本没启动”和“子进程已启动后监督器失败”，并执行相应终态收口。
RC_GUARD_ERROR = 126


def terminate_process_tree(proc: subprocess.Popen) -> None:
    """终止子进程及其全部后代。

    只杀直接子进程会留下持有管道的孙进程，回收阶段照样阻塞，故必须整树杀。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10, creationflags=CREATE_NO_WINDOW,
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


def run_guarded(
    cmd: list[str],
    *,
    timeout: float,
    input_text: str | None = None,
    cwd: str | None = None,
    creationflags: int = CREATE_NO_WINDOW,
    observer: Callable[[], object | None] | None = None,
    observer_poll_seconds: float = 0.5,
) -> tuple[int, str, str, bool]:
    """跑子进程，超时整树杀。

    返回 `(rc, stdout, stderr, timed_out)`；超时时 rc=124，并尽量保留子进程
    在被杀前已刷出的部分输出（错误归因常常只在那几行里）。

    与 `subprocess.run(timeout=)` 的差别只有一点：**超时真的会在 timeout 附近
    返回**，不会被孙进程拖住。
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        creationflags=creationflags,
    )
    try:
        if observer is None:
            out, err = proc.communicate(input_text, timeout=timeout)
            return proc.returncode, out or "", err or "", False

        # ``communicate`` 可以在 TimeoutExpired 后继续调用；这样既持续排空
        # stdout/stderr（防 pipe 填满卡死），又能按短周期检查监督条件。
        deadline = time.monotonic() + max(0.0, float(timeout))
        pending_input = input_text
        poll = max(0.05, float(observer_poll_seconds))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                out, err = proc.communicate(
                    pending_input,
                    timeout=min(poll, remaining),
                )
                return proc.returncode, out or "", err or "", False
            except subprocess.TimeoutExpired:
                # input 只能在第一次 communicate 传入；后续调用继续回收输出。
                pending_input = None
                try:
                    stop_reason = observer()
                except Exception:  # noqa: BLE001 - observer 失效时保留原硬截止兜底
                    stop_reason = None
                if stop_reason is None:
                    continue
                terminate_process_tree(proc)
                try:
                    out, err = proc.communicate(timeout=1)
                except Exception:  # noqa: BLE001
                    out, err = "", ""
                note = f"observer stop: {stop_reason}; process tree terminated"
                return (
                    RC_OBSERVED_STOP,
                    str(out or ""),
                    f"{note} | {err}" if err else note,
                    False,
                )
    except subprocess.TimeoutExpired as exc:
        partial_out, partial_err = exc.stdout or "", exc.stderr or ""
        terminate_process_tree(proc)
        try:
            tail_out, tail_err = proc.communicate(timeout=1)
            if tail_out:
                partial_out = tail_out
            if tail_err:
                partial_err = tail_err
        except Exception:  # noqa: BLE001
            pass
        if isinstance(partial_out, bytes):
            partial_out = partial_out.decode("utf-8", errors="replace")
        if isinstance(partial_err, bytes):
            partial_err = partial_err.decode("utf-8", errors="replace")
        note = f"timeout after {timeout}s; process tree terminated"
        return (RC_TIMEOUT, str(partial_out or ""),
                (f"{note} | {partial_err}" if partial_err else note), True)
    except Exception as exc:  # noqa: BLE001 - Popen 后必须转成有证据的终态
        # Popen 已成功，因此这不是 started=False。无论异常来自 pipe、编码、
        # observer 还是回收，先整树终止，再返回专用非零码，让 stage 保留
        # child_started=true 并继续 stopping/Gateway abort 流程。
        terminate_process_tree(proc)
        try:
            tail_out, tail_err = proc.communicate(timeout=1)
        except Exception:  # noqa: BLE001
            tail_out, tail_err = "", ""
        note = (
            f"guard error after process start: {type(exc).__name__}: {exc}; "
            "process tree terminated"
        )
        return (
            RC_GUARD_ERROR,
            str(tail_out or ""),
            f"{note} | {tail_err}" if tail_err else note,
            False,
        )
    finally:
        # Covers even BaseException while a child is live.  Successful/natural
        # nonzero returns already have poll()!=None and are not touched.
        try:
            if proc.poll() is None:
                terminate_process_tree(proc)
        except Exception:  # noqa: BLE001 - do not mask the original failure
            pass
