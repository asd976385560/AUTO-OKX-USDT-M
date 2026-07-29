# -*- coding: utf-8 -*-
r"""OKX CLI 调用封装。

本模块对齐公开消费者的调用契约（collect_data / collect_slow /
demo_account_check / jobb_live_account_check / core/lib/_okxorder）：

    okx_json(*args, global_args=["--profile", <p>], timeout_sec=45.0) -> 解析后的 JSON
        成功：返回 dict / list（OKX `--json` 输出原样解析）
        失败：抛 RuntimeError（rc!=0 / 空输出 / 非 JSON）或 TimeoutError（超时）

事实依据
--------
- CLI = npm `@okx_ai/okx-trade-cli`（通过 Node.js entry 调用）。
  实测 `node <entry> --json --profile demo account balance` 返 JSON（2026-06-26）。
- **连通**：当前封装默认让 okx CLI（undici）直连——env 有
  HTTP(S)_PROXY 时反走代理失败。`_subprocess_env()` 剥 HTTP(S)_PROXY/ALL_PROXY +
  设 NO_PROXY=* 强制直连。凭证由 CLI 自身 profile 配置（--profile live/demo），本模块不碰 key。

安全设计（live 真金）
--------------------
- **窗口隐藏**：Windows CREATE_NO_WINDOW，不弹控制台。
- **节流**：相邻调用最小间隔，防打爆。
- **写命令绝不在超时后重试**（防 place/close 重复下单致双仓）：只读命令可重试，
  写命令（place/close/leverage/cancel）超时即抛、rc!=0 即抛，由 order_executor 经
  fills 回读自行判定真实成交（不靠本层重试）。
- 零模型名（红线 #1）。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath


import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, Sequence

# --- CLI 定位（env 可覆盖）---------------------------------------------------
_NODE = os.environ.get("OKX_NODE_BIN", "node")
_ENTRY = os.environ.get(
    "OKX_CLI_ENTRY",
    str(_ProjectPath.home().joinpath('AppData', 'Roaming', 'npm', 'node_modules', '@okx_ai', 'okx-trade-cli', 'dist', 'index.js')),
)
# 备用：npm 全局 .cmd（entry 缺失时；okx 参数无 JSON，.cmd 不会 mangle）
_OKX_CMD = os.environ.get(
    "OKX_CLI_CMD", str(_ProjectPath.home().joinpath('AppData', 'Roaming', 'npm', 'okx.cmd'))
)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 写命令 token：命中即视为变更类，超时/rc!=0 不重试（防重复下单）
_WRITE_TOKENS = {"place", "close", "leverage", "amend", "cancel", "cancel-algos"}

# --- 节流 -------------------------------------------------------------------
_THROTTLE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
_MIN_INTERVAL_SEC = float(os.environ.get("OKX_CLI_MIN_INTERVAL", "0.12"))


def _throttle() -> None:
    with _THROTTLE_LOCK:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SEC - (now - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.monotonic()


def _subprocess_env() -> dict:
    """剥代理 env + NO_PROXY=*，让 okx CLI（undici）直连（OKX 经代理反不通）。"""
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def _base_cmd() -> list[str]:
    """优先 node + entry（最稳，无 .cmd / PATH 依赖）；entry 缺则退 okx.cmd。"""
    if Path(_ENTRY).exists():
        return [_NODE, _ENTRY, "--json"]
    if Path(_OKX_CMD).exists():
        return [_OKX_CMD, "--json"]
    raise RuntimeError(
        f"okx CLI not found: entry={_ENTRY} / cmd={_OKX_CMD}（设 OKX_CLI_ENTRY 覆盖）")


def _is_write(args: Sequence[str]) -> bool:
    return any(a in _WRITE_TOKENS for a in args)


def _parse_json(out: str) -> Optional[Any]:
    """解析 CLI stdout 为 JSON；容忍前后混杂日志行（抠第一个完整 JSON 块）。"""
    out = (out or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        pass
    # 退而求其次：取首个 '['/'{' 到末个 ']'/'}' 的子串
    starts = [i for i in (out.find("["), out.find("{")) if i >= 0]
    ends = [i for i in (out.rfind("]"), out.rfind("}")) if i >= 0]
    if starts and ends:
        s, e = min(starts), max(ends)
        if e > s:
            try:
                return json.loads(out[s:e + 1])
            except json.JSONDecodeError:
                return None
    return None


def okx_json(*args: str, global_args: Optional[Sequence[str]] = None,
             timeout_sec: float = 45.0, retries: int = 2) -> Any:
    """调 okx CLI 并返回解析后的 JSON。失败抛 RuntimeError / TimeoutError。

    写命令（place/close/leverage/cancel）超时或 rc!=0 **不重试**（防重复下单）；
    只读命令在 retries 次内重试瞬时失败。
    """
    cmd = _base_cmd() + list(global_args or []) + [str(a) for a in args]
    write = _is_write(args)
    env = _subprocess_env()
    attempt = 0
    last_err = ""
    while True:
        attempt += 1
        _throttle()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout_sec, env=env,
                creationflags=_CREATE_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            # node/entry 不可执行——什么都没发出去，但重试也救不了 → 直接抛
            raise RuntimeError(f"okx CLI not launchable: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            last_err = f"timeout after {timeout_sec}s"
            if write or attempt > retries:
                raise TimeoutError(
                    f"okx CLI {last_err}: {' '.join(map(str, args))}") from exc
            continue  # 只读：重试

        if proc.returncode != 0:
            last_err = (proc.stderr or proc.stdout or "").strip()
            if write or attempt > retries:
                raise RuntimeError(
                    f"okx CLI rc={proc.returncode}: {last_err[:600]}")
            continue  # 只读：重试瞬时网络/CLI 错

        parsed = _parse_json(proc.stdout)
        if parsed is not None:
            return parsed
        last_err = f"non-JSON/empty output: {(proc.stdout or '')[:400]}"
        if write or attempt > retries:
            raise RuntimeError(f"okx CLI {last_err}")
        continue


def _atomic_write_json(path: Path, value: Any, *, compact: bool = False) -> int:
    """Atomically write complete UTF-8 JSON and return its byte count."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    ) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return len(text.encode("utf-8"))


def main(argv: Optional[list[str]] = None) -> int:
    """CLI read helper with optional complete atomic JSON output."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raw = list(argv if argv is not None else sys.argv[1:])
    g: list[str] = []
    out_file: Path | None = None
    compact = False
    try:
        while raw and raw[0] in ("--profile", "--out-file", "--compact"):
            option = raw.pop(0)
            if option == "--compact":
                compact = True
                continue
            if not raw:
                raise ValueError(f"{option} requires a value")
            value = raw.pop(0)
            if option == "--profile":
                g += [option, value]
            else:
                out_file = Path(value)
        if not raw:
            raise ValueError("missing OKX command")
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 64
    try:
        out = okx_json(*raw, global_args=g)
    except (RuntimeError, TimeoutError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    if out_file is not None:
        try:
            size = _atomic_write_json(out_file, out, compact=compact)
        except OSError as exc:
            print(json.dumps({
                "ok": False,
                "error": f"out-file write failed: {type(exc).__name__}: {exc}",
            }, ensure_ascii=False))
            return 1
        print(json.dumps({
            "ok": True,
            "out_file": str(out_file),
            "bytes": size,
        }, ensure_ascii=False, separators=(",", ":")))
    else:
        # Never slice serialized JSON: truncation creates invalid JSON exactly
        # when an account has many positions.
        print(json.dumps(
            out,
            ensure_ascii=False,
            separators=(",", ":") if compact else None,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
