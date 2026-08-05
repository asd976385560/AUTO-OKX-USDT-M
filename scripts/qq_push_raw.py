# -*- coding: utf-8 -*-
r"""确定性 QQ 推送 helper（公开版）。

业务播报与告警使用两条独立路由，目标只从部署环境读取：

* `OKX_QQ_TARGET=group:<group_openid>`：业务播报；
* `OKX_QQ_ALERT_TARGET=c2c:<user_openid>`：带 ``--alert`` 的告警。

公开代码不提供目标、Node 或 OpenClaw 路径的默认值，也不接受 CLI 目标覆盖。
实际发送还必须设置 `OKX_NODE_BIN` 和 `OKX_OPENCLAW_MJS`。

用法：
    python qq_push.py --content-file <PROJECT_ROOT>/tmp/render_last_content.txt
    python qq_push.py --message "..." [--alert] [--dry-run]
退出码：0=送达（回执含 messageId）；1=外发失败；2=输入或配置错误。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_NODE_ENV = "OKX_NODE_BIN"
_MJS_ENV = "OKX_OPENCLAW_MJS"
_BROADCAST_TARGET_ENV = "OKX_QQ_TARGET"
_ALERT_TARGET_ENV = "OKX_QQ_ALERT_TARGET"
_CHANNEL = "qqbot"
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _resolve_target(alert: bool) -> tuple[str, str]:
    """返回 ``(target, env_name)``；目标只能来自所选路由的环境变量。"""
    env_name = _ALERT_TARGET_ENV if alert else _BROADCAST_TARGET_ENV
    target = os.environ.get(env_name, "").strip()
    expected_prefix = "c2c:" if alert else "group:"
    if target and not target.startswith(expected_prefix):
        return "", env_name
    return target, env_name


def _resolve_runtime() -> tuple[str, str, str]:
    """返回 ``(node, openclaw_mjs, missing_env)``，公开版无宿主路径 fallback。"""
    node = os.environ.get(_NODE_ENV, "").strip()
    mjs = os.environ.get(_MJS_ENV, "").strip()
    if not node:
        return "", "", _NODE_ENV
    if not mjs:
        return "", "", _MJS_ENV
    return node, mjs, ""


def push(content: str, *, alert: bool = False, timeout: float = 60.0) -> tuple[bool, str]:
    """发送 content；返回 ``(ok, 原始输出)``。目标由 alert 对应环境变量决定。"""
    target, target_env = _resolve_target(alert)
    if not target:
        return False, f"QQ target is not configured or has the wrong route prefix; set {target_env}"
    node, mjs, missing_env = _resolve_runtime()
    if missing_env:
        return False, f"push runtime is not configured; set {missing_env}"
    cmd = [node, mjs, "message", "send", "--channel", _CHANNEL,
           "--target", target, "--message", content, "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
    ok = p.returncode == 0 and '"messageId"' in (p.stdout or "")
    return ok, out


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性 QQ 群推送")
    ap.add_argument("--content-file", help="UTF-8 文件，内容作为消息体（与 render --out-file 对接）")
    ap.add_argument("--message", help="直接给消息体字符串")
    ap.add_argument("--alert", action="store_true",
                    help="从 OKX_QQ_ALERT_TARGET 读取告警私聊目标；默认读取 OKX_QQ_TARGET")
    ap.add_argument("--dry-run", action="store_true", help="只打印不发送")
    args = ap.parse_args()

    if args.content_file:
        try:
            content = Path(args.content_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"ERROR: read content-file failed: {e}")
            return 2
    elif args.message:
        content = args.message
    else:
        print("ERROR: need --content-file or --message")
        return 2

    content = content.strip()
    if not content:
        print("ERROR: empty content")
        return 2
    target, target_env = _resolve_target(args.alert)
    if not target:
        print(f"ERROR: QQ target is not configured or has the wrong route prefix; set {target_env}")
        return 2

    if args.dry_run:
        print(f"[dry-run] would send {len(content)} chars via {target_env}")
        return 0

    ok, out = push(content, alert=args.alert)
    print(out[:600])
    if ok:
        print(f"\nPUSH OK via {target_env} ({len(content)} chars)")
        return 0
    print(f"\nPUSH FAILED via {target_env}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
