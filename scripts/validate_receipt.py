# -*- coding: utf-8 -*-
r"""V2.0 analysis / trade 回执兼容校验入口。

规则唯一来自 writer：`analyst_writer.validate_receipt` / `trades_writer.validate`。
本文件只保留旧 CLI 的薄包装，不复制任何字段规则；现役 Agent 应直接使用 writer
自带的 `--validate-only`（analysis）或最终 writer 校验。

输入只从 stdin 读 JSON，不写数据库。

用法：
  echo '<JSON>' | run_okx_python.ps1 scripts/validate_receipt.py --type analysis
  echo '<JSON>' | run_okx_python.ps1 scripts/validate_receipt.py --type trade
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_COLLECTORS = str(_ROOT / "collectors")
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)

from analyst_writer import validate_receipt as validate_analysis  # noqa: E402
from trades_writer import validate as validate_trade  # noqa: E402


_VALIDATORS = {"analysis": validate_analysis, "trade": validate_trade}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="analysis/trade 回执 schema 校验器")
    ap.add_argument("--type", required=True, choices=list(_VALIDATORS))
    args = ap.parse_args()
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "errors": [f"JSON 解析失败: {e}"]}, ensure_ascii=False))
        return 1
    errs = _VALIDATORS[args.type](data)
    print(json.dumps({"ok": not errs, "errors": errs}, ensure_ascii=False))
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
