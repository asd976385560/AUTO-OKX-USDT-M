# -*- coding: utf-8 -*-
r"""actor_attestation.py — 接管重验凭证生成 CLI（Wave1 序6）。

用法（live trader 在 executor 前检测到接管、或想主动自查时）：
  run_okx_python.ps1 scripts/actor_attestation.py --cycle-id 2026-08-10T11:15 \
      --out-file ./tmp/attestation_2026-08-10T11-15.json

输出 = core/actor_attestation.build_attestation 的确定性产物：会话 actor 时间线
（只含不透明指纹，零模型名）+ 接管检测 + 重验包（analysis 状态/证据契约/EV 重算/
facts 年龄）+ 全文指纹。接管模型把整份 JSON 原样放入 receipt_context
["actor_attestation"]；executor 独立重算比对，改一个字段即失效。

同 actor 正常轮不需要本工具（executor 零负担直通）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, r".")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.actor_attestation import build_attestation  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="接管重验凭证生成（确定性）")
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--db-root", default=r"./db")
    ap.add_argument("--stage", default="live")
    ap.add_argument("--out-file", default=None)
    args = ap.parse_args()

    attestation = build_attestation(
        args.cycle_id, db_root=args.db_root, stage=args.stage)
    text = json.dumps(attestation, ensure_ascii=False, indent=1)
    if args.out_file:
        Path(args.out_file).write_text(text, encoding="utf-8")
        print(json.dumps({
            "ok": True, "out_file": args.out_file,
            "handoff_detected": (
                attestation.get("timeline") or {}).get("handoff_detected"),
            "attestation_hash": attestation.get("attestation_hash"),
        }, ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
