# -*- coding: utf-8 -*-
r"""确定性 QQ 群推送 helper（2026-06-26）。

唯一职责：把已渲染好的 content（文件或字符串）发到配置的 QQ 群——经实测可用的
`node openclaw.mjs message send --channel qqbot --target group:<openid>`。
QQ 官方机器人群接口使用 group_openid（不是群号）。公开版本不提供默认目标；
运行前必须设置 `OKX_QQ_TARGET=group:<openid>`，也可在 CLI 显式传 `--target`。

用法：
    python qq_push.py --content-file <PROJECT_ROOT>/tmp/render_last_content.txt
    python qq_push.py --message "..." [--target group:<openid>] [--dry-run]
退出码：0=送达（回执含 messageId）；1=外发失败；2=输入错误。
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

_NODE = os.environ.get("OKX_NODE_BIN", r"C:\Program Files\nodejs\node.exe")
_MJS = os.environ.get(
    "OKX_OPENCLAW_MJS",
    str(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "openclaw" / "openclaw.mjs"),
)
DEFAULT_TARGET = os.environ.get("OKX_QQ_TARGET", "").strip()
_CHANNEL = "qqbot"
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def push(content: str, target: str = DEFAULT_TARGET, timeout: float = 60.0) -> tuple[bool, str]:
    """发送 content 到 target。返回 (ok, 原始输出)。ok = 回执含 messageId。"""
    if not target:
        return False, "QQ target is not configured; set OKX_QQ_TARGET or pass --target"
    cmd = [_NODE, _MJS, "message", "send", "--channel", _CHANNEL,
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
    ap.add_argument("--target", default=DEFAULT_TARGET, help="qqbot target；公开版无默认值")
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
    if not args.target:
        print("ERROR: QQ target is not configured; set OKX_QQ_TARGET or pass --target")
        return 2

    if args.dry_run:
        print(f"[dry-run] would send {len(content)} chars to {args.target}")
        return 0

    ok, out = push(content, args.target)
    print(out[:600])
    if ok:
        print(f"\nPUSH OK -> {args.target} ({len(content)} chars)")
        return 0
    print(f"\nPUSH FAILED -> {args.target}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
