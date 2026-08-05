# -*- coding: utf-8 -*-
"""log_rotate.py — logs/ 日志轮转（纯文件卫生，默认 dry-run）。

背景（架构评审 #4，2026-07-07）：logs/trigger 每 cycle 落触发日志（msg-*.txt / *.log），
数天涨到 ~170MB/5000 文件（≈16GB/年）；reviewer 的 tmp_cleanup 只清 tmp/ 不碰 logs/。
本脚本删 logs 子目录下超 N 天的旧文件——纯磁盘卫生，不涉 DB/单writer。

**默认 dry-run**（只列不删），`--apply` 才真删。挂日常 cron（okx-log-rotate）每日跑。

用法：
  log_rotate.py                      # dry-run，列将删文件与释放空间
  log_rotate.py --apply              # 真删（超 7 天）
  log_rotate.py --days 14 --apply    # 保留窗 14 天
  log_rotate.py --dirs trigger,push,stage-status --days 7 --apply
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


import argparse
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_ROOT = Path(_project_path('logs'))
# 轮转目标：per-cycle 高频落盘的调试日志目录。审计类 jsonl（qq_push_dedupe/pipeline_runs/
# monitor audit）不在此删——它们是排障权威、体量小、单独按需管。
# standalone 默认保持 trigger/push；daily_maintenance 显式追加 stage-status。
DEFAULT_DIRS = ["trigger", "push"]
# 保护：这些结构化审计/状态文件不删（即便在目标目录内）
PROTECT_SUFFIX = (".jsonl",)
PROTECT_NAMES = ("alert_state.json",)


def rotate(dirs: list[str], days: float, apply: bool) -> dict:
    cutoff = time.time() - days * 86400
    deleted = kept_protected = 0
    freed = 0
    samples: list[str] = []
    for sub in dirs:
        d = LOG_ROOT / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not f.is_file():
                continue
            if f.suffix in PROTECT_SUFFIX or f.name in PROTECT_NAMES:
                kept_protected += 1
                continue
            try:
                if f.stat().st_mtime >= cutoff:
                    continue
                sz = f.stat().st_size
            except OSError:
                continue
            if len(samples) < 5:
                samples.append(f"{sub}/{f.name}")
            deleted += 1
            freed += sz
            if apply:
                try:
                    f.unlink()
                except OSError as e:
                    print(f"[log_rotate] WARN 删除失败 {f}: {e}", file=sys.stderr)
    return {"deleted": deleted, "freed_mb": round(freed / 1e6, 1),
            "kept_protected": kept_protected, "apply": apply, "days": days,
            "samples": samples}


def main() -> int:
    ap = argparse.ArgumentParser(description="logs/ 日志轮转（默认 dry-run）")
    ap.add_argument("--days", type=float, default=7.0, help="保留窗（天），超此的文件删")
    ap.add_argument("--dirs", default=",".join(DEFAULT_DIRS),
                    help="逗号分隔的 logs 子目录（默认 trigger,push）")
    ap.add_argument("--apply", action="store_true", help="真删（否则 dry-run 只列）")
    args = ap.parse_args()
    dirs = [d.strip() for d in args.dirs.split(",") if d.strip()]
    r = rotate(dirs, args.days, args.apply)
    import json
    print(json.dumps(r, ensure_ascii=False, indent=1))
    if not args.apply and r["deleted"]:
        print(f"[dry-run] 将删 {r['deleted']} 文件、释放 {r['freed_mb']}MB（--apply 才真删）",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
