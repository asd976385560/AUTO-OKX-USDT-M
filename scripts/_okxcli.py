from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

OKX_PS1 = Path(os.environ.get("OKX_CLI_PS1", r"C:\Users\97638\AppData\Roaming\npm\okx.ps1"))
DEFAULT_TIMEOUT_SEC = 45.0
DEFAULT_MIN_INTERVAL_SEC = 0.25
DEFAULT_RETRY_COUNT = 1

_LOCK = threading.Lock()
_LAST_CALL_AT = 0.0


def _throttle(min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC) -> None:
    global _LAST_CALL_AT
    with _LOCK:
        now = time.monotonic()
        wait_for = _LAST_CALL_AT + min_interval_sec - now
        if wait_for > 0:
            time.sleep(wait_for)
        _LAST_CALL_AT = time.monotonic()


def _extract_json_payload(stdout: str) -> str:
    stripped = stdout.strip()
    indexes = [index for index in (stripped.find("{"), stripped.find("[")) if index != -1]
    for index in sorted(indexes):
        candidate = stripped[index:]
        json.loads(candidate)
        return candidate
    raise ValueError("stdout 中未找到 JSON 载荷")


def _is_timeout_like(message: str) -> bool:
    lowered = message.lower()
    return "timeout" in lowered or "timed out" in lowered


def _global_args_from_env() -> list[str]:
    raw = os.environ.get("OKX_CLI_GLOBAL_ARGS", "").strip()
    if not raw:
        return []
    return shlex.split(raw, posix=False)


def okx_json(*args: str, timeout_sec: float = DEFAULT_TIMEOUT_SEC, global_args: list[str] | None = None) -> Any:
    if not OKX_PS1.exists():
        raise FileNotFoundError(f"OKX CLI 不存在：{OKX_PS1}")
    _throttle()
    resolved_global_args = global_args if global_args is not None else _global_args_from_env()
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(OKX_PS1),
        "--json",
        *resolved_global_args,
        *args,
    ]
    last_message = ""
    for attempt in range(DEFAULT_RETRY_COUNT + 1):
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            last_message = f"OKX CLI 超时（{timeout_sec:.0f}s）：{' '.join(args)}"
            if attempt < DEFAULT_RETRY_COUNT:
                time.sleep(DEFAULT_MIN_INTERVAL_SEC)
                continue
            raise TimeoutError(last_message) from exc

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if completed.returncode != 0:
            message = stderr or stdout or f"returncode={completed.returncode}"
            last_message = message
            if attempt < DEFAULT_RETRY_COUNT and _is_timeout_like(message):
                time.sleep(DEFAULT_MIN_INTERVAL_SEC)
                continue
            raise RuntimeError(f"OKX CLI 失败：{' '.join(args)} :: {message}")
        payload = _extract_json_payload(stdout)
        return json.loads(payload)
    raise RuntimeError(f"OKX CLI 失败：{' '.join(args)} :: {last_message}")


def self_check(profile: str, *, demo: bool = False, timeout_sec: float = 10.0) -> dict:
    """启动金丝雀：调用 account balance 一次性验证 CLI/网络/签名/账户权限。

    OKX CLI 1.3.x 缺少不需签名的轻量 public 端点，故只用 balance 一步覆盖
    所有失败模式（CLI 不存在、网络不通、签名错、权限不足）。

    返回 {ok, profile, demo, usdt_avail, total_eq, latency_ms}；失败时 RuntimeError。
    """
    base: list[str] = ["--profile", profile]
    if demo:
        base.append("--demo")

    t0 = time.monotonic()
    try:
        bal_payload = okx_json("account", "balance", global_args=base, timeout_sec=timeout_sec)
    except Exception as exc:
        raise RuntimeError(f"self_check step=account_balance 失败: {exc}") from exc
    balance_ms = int((time.monotonic() - t0) * 1000)

    usdt_avail: str | None = None
    total_eq: str | None = None
    bal_rows: list = []
    if isinstance(bal_payload, list):
        bal_rows = bal_payload
    elif isinstance(bal_payload, dict):
        data = bal_payload.get("data")
        if isinstance(data, list):
            bal_rows = data
    for row in bal_rows:
        if not isinstance(row, dict):
            continue
        if total_eq is None:
            total_eq = str(row.get("totalEq") or "")
        for detail in row.get("details", []) or []:
            if isinstance(detail, dict) and detail.get("ccy") == "USDT":
                usdt_avail = str(detail.get("availBal") or detail.get("availEq") or "")
                break
        if usdt_avail is not None:
            break

    return {
        "ok": True,
        "profile": profile,
        "demo": demo,
        "usdt_avail": usdt_avail,
        "total_eq": total_eq,
        "latency_ms": {"balance": balance_ms},
    }


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="OKX CLI 启动自检（public time + account balance）")
    parser.add_argument("--profile", required=True, help="OKX CLI profile 名称")
    parser.add_argument("--demo", action="store_true", help="使用 demo 环境")
    parser.add_argument("--timeout", type=float, default=10.0, help="单步超时（秒）")
    args = parser.parse_args()

    try:
        result = self_check(args.profile, demo=args.demo, timeout_sec=args.timeout)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())