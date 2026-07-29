# -*- coding: utf-8 -*-
r"""reports_rotate.py — reports/ 无界增长治理（2026-07-17 主人拍板）。

背景：reports/agents（push_archive 战报归档，~96 件/天，实测 2290 件）与
reports/push（pipeline 环节报告，~80 件/天，801 件）无轮转，年增 ~3.5 万文件。
策略：超 --days（默认 30）的文件**压入月度 zip 后删原件**——保全量可回溯、封顶文件数。
zip 落 reports/archive/<组>-<YYYYMM>.zip（按文件 mtime 月分桶；不放 tmp/ 防误清）。

默认 dry-run 只报计划；--apply 真执行。日频由 daily_maintenance.py 第④步调度。
读方安全：现役读方（reviewer/render 回退链）只读近几天文件，30 天窗远超其需求。
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
import json
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPORTS = Path(_project_path('reports'))
TARGETS = [
    ("agents", REPORTS / "agents", "*.md"),
    ("push", REPORTS / "push", "*.json"),
]
ARCHIVE_DIR = REPORTS / "archive"


def rotate(days: int, apply: bool) -> dict:
    cutoff = time.time() - days * 86400
    report: dict = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "days": days, "dry_run": not apply, "groups": {}}
    for name, d, pat in TARGETS:
        if not d.exists():
            report["groups"][name] = {"error": "dir missing"}
            continue
        old = [f for f in d.glob(pat) if f.is_file() and f.stat().st_mtime < cutoff]
        by_month: dict[str, list[Path]] = {}
        for f in old:
            mon = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y%m")
            by_month.setdefault(mon, []).append(f)
        g = {"candidates": len(old),
             "months": {m: len(fs) for m, fs in sorted(by_month.items())},
             "remaining_after": len(list(d.glob(pat))) - len(old)}
        if apply and old:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            archived = 0
            for mon, fs in by_month.items():
                zp = ARCHIVE_DIR / f"{name}-{mon}.zip"
                with zipfile.ZipFile(zp, "a", zipfile.ZIP_DEFLATED) as z:
                    existing = set(z.namelist())
                    for f in fs:
                        if f.name not in existing:  # 幂等：重跑不重复入包
                            z.write(f, f.name)
                # 入包核验后才删原件（zip 可读回同名成员）
                with zipfile.ZipFile(zp, "r") as z:
                    names = set(z.namelist())
                for f in fs:
                    if f.name in names:
                        f.unlink()
                        archived += 1
            g["archived_deleted"] = archived
        report["groups"][name] = g
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="reports/ 月度压包轮转（默认 dry-run）")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    r = rotate(args.days, args.apply)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
