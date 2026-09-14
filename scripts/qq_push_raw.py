# -*- coding: utf-8 -*-
r"""确定性 QQ 群推送 helper（2026-06-26）。

唯一职责：把已渲染好的 content（文件或字符串）经常驻 OpenClaw Gateway 的
官方 send RPC 发到配置的 QQ 目标；qq_gateway_send.mjs 只做本机传输适配。
避免每条消息重复启动完整 CLI、检查共享状态库并竞争启动检查表写锁。
显式 OKX_QQ_TRANSPORT=cli 可回滚到旧 CLI；不在失败后自动切换或重发。
QQ 目标只能来自部署者显式配置；公开版没有默认目标。

2026-08-14 主人拍板（全量推送批次）：渲染端已去掉 3500 字压缩与段内截断，
本层**整条全量单发、不做任何本地截断或分段**——超长消息由 QQ 侧收到后自行
分段展示。禁止再往本层加 split/多段连发逻辑（当日实现过又按主人指示撤除）。

用法：
    python qq_push.py --content-file <PROJECT_ROOT>/tmp/render_last_content.txt
    python qq_push.py --message "..." [--alert | --report] [--dry-run]
退出码：0=送达（回执含 messageId）；1=外发明确失败；2=输入错误；
3=提交结果不明（包括命令超时/无回执，uncertain_delivery，禁止自动重试）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_NODE = os.environ.get("OKX_NODE_BIN", "")
_MJS = os.environ.get("OKX_OPENCLAW_MJS", "")
DEFAULT_TARGET = os.environ.get("OKX_QQ_TARGET", "")
ALERT_TARGET = os.environ.get("OKX_QQ_ALERT_TARGET", "")
REPORT_TARGET = os.environ.get("OKX_QQ_REPORT_TARGET", "")
_CHANNEL = "qqbot"
_GATEWAY_MJS = str(Path(__file__).with_name("qq_gateway_send.mjs"))
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_PRE_SUBMIT_TOKEN_TLS_MARKERS = (
    "network error getting access_token",
    "client network socket disconnected before secure tls connection was established",
)
_DELIVERY_RECEIPT_MARKERS = ('"messageid"', '"action": "send"')
_MAX_DELIVERY_BUDGET_SECONDS = 55.0
UNCERTAIN_DELIVERY_EXIT_CODE = 3
UNCERTAIN_DELIVERY_MARKER = (
    "[qq_push_raw] UNCERTAIN_DELIVERY: delivery outcome unknown; no automatic retry"
)


def _retryable_pre_submit_token_tls_failure(output: str) -> bool:
    """Only retry the proven pre-submit token TLS failure.

    The transport explicitly says that TLS was never established while getting
    an access token, so the message body could not have reached the send API.
    Any receipt-like output makes the result ambiguous and therefore
    non-retryable; unknown failures and timeouts also remain single-attempt.
    """
    normalized = str(output or "").lower()
    if any(marker in normalized for marker in _DELIVERY_RECEIPT_MARKERS):
        return False
    return all(marker in normalized for marker in _PRE_SUBMIT_TOKEN_TLS_MARKERS)


def push(content: str, target: str | None = None, timeout: float = 60.0) -> tuple[bool, str]:
    """发送 content 到 target。返回 (ok, 原始输出)。ok = 回执含 messageId。

    默认网关路径只调用一次；结果不明必须返回 uncertain_delivery。
    显式旧 CLI 路径仅对已证明发生于提交前的 access-token TLS 失败保留一次
    短预算重试。两条路径均不自动互相回退。
    """
    target = DEFAULT_TARGET if target is None else target
    if not target or "PUBLIC_" in target:
        return False, "QQ target is not configured; set the selected OKX_QQ_*_TARGET"
    if not _NODE or not _MJS:
        return False, "push runtime is not configured; set OKX_NODE_BIN and OKX_OPENCLAW_MJS"
    transport = os.environ.get("OKX_QQ_TRANSPORT", "gateway").strip().lower()
    if transport not in {"gateway", "cli"}:
        return False, "Invalid OKX_QQ_TRANSPORT; expected gateway or cli"
    cmd = ([_NODE, _GATEWAY_MJS] if transport == "gateway" else
           [_NODE, _MJS, "message", "send", "--channel", _CHANNEL,
            "--target", target, "--message", content, "--json"])
    outputs: list[str] = []
    total_budget = min(max(float(timeout), 1.0), _MAX_DELIVERY_BUDGET_SECONDS)
    deadline = time.monotonic() + total_budget
    for attempt in range(1 if transport == "gateway" else 2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            outputs.append(f"delivery budget exhausted after {total_budget:g}s")
            return False, "\n".join(outputs)
        attempt_timeout = min(
            remaining,
            total_budget if attempt == 0 else 25.0,
        )
        try:
            p = subprocess.run(
                cmd,
                input=(json.dumps({"content": content, "target": target,
                                  "openclawMjs": _MJS,
                                  "timeoutMs": max(1000, int((attempt_timeout - 6) * 1000))},
                                 ensure_ascii=False)
                       if transport == "gateway" else None),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=attempt_timeout,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            outputs.append(f"timeout after {attempt_timeout:g}s")
            outputs.append(UNCERTAIN_DELIVERY_MARKER)
            return False, "\n".join(outputs)
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        outputs.append(out)
        if transport == "gateway" and p.returncode == UNCERTAIN_DELIVERY_EXIT_CODE:
            outputs.append(UNCERTAIN_DELIVERY_MARKER)
            return False, "\n".join(outputs)
        ok = p.returncode == 0 and '"messageId"' in (p.stdout or "")
        if ok:
            return True, "\n".join(outputs)
        if transport == "cli" and attempt == 0 and _retryable_pre_submit_token_tls_failure(out):
            outputs.append("[qq_push_raw] bounded retry: pre-submit token TLS failure")
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
            continue
        return False, "\n".join(outputs)
    return False, "\n".join(outputs)


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性 QQ 群推送")
    ap.add_argument("--content-file", help="UTF-8 文件，内容作为消息体（与 render --out-file 对接）")
    ap.add_argument("--message", help="直接给消息体字符串")
    ap.add_argument("--alert", action="store_true",
                    help="告警走 C2C 私聊 ALERT_TARGET（与业务播报分流）；--target 显式给值时优先")
    ap.add_argument("--report", action="store_true",
                    help="日/周/月报告走 C2C 私聊 REPORT_TARGET（2026-08-26 主人拍板）；"
                         "--target 显式给值时优先")
    ap.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="总外发预算秒数；内部仍硬封顶 55 秒",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印不发送")
    args = ap.parse_args()
    args.target = None
    if args.target is None:
        if args.alert:
            args.target = ALERT_TARGET
        elif args.report:
            args.target = REPORT_TARGET
        else:
            args.target = DEFAULT_TARGET

    if not args.target or "PUBLIC_" in args.target:
        print("ERROR: configure the selected OKX_QQ_*_TARGET environment variable")
        return 2

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

    if args.dry_run:
        print(f"[dry-run] would send {len(content)} chars to {args.target}")
        return 0

    ok, out = push(content, args.target, timeout=args.timeout)
    print(out[:600])
    if ok:
        print(f"\nPUSH OK -> {args.target} ({len(content)} chars)")
        return 0
    if UNCERTAIN_DELIVERY_MARKER in out:
        print(
            f"\nPUSH UNCERTAIN -> {args.target} "
            "(no receipt; no automatic retry)",
        )
        return UNCERTAIN_DELIVERY_EXIT_CODE
    print(f"\nPUSH FAILED -> {args.target}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
