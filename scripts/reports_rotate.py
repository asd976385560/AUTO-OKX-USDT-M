# -*- coding: utf-8 -*-
r"""reports_rotate.py — reports/ 无界增长治理（2026-07-17 主人拍板）。

背景：reports/agents（push_archive 战报归档，~96 件/天，实测 2290 件）与
reports/push（pipeline 环节报告，~80 件/天，801 件）无轮转，年增 ~3.5 万文件。
策略：超 --days（默认 30）的文件**压入月度 zip 后删原件**——保全量可回溯、封顶文件数。
zip 落 reports/archive/<组>-<YYYYMM>.zip（旧平铺按文件 mtime、新日期布局按
路径声明的 YYYY/MM 分桶；不放 tmp/ 防误清）。

默认 dry-run 只报计划；--apply 真执行。日频由 daily_maintenance.py 第④步调度。
读方安全：agents 保持既有根平铺范围；仅 push 兼容严格 YYYY/MM/DD。
现役读方（reviewer/render 回退链）只读近几天文件，30 天窗远超其需求。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from _artifact_paths import (
    archive_member_name,
    artifact_month_key,
    iter_legacy_and_ymd_files,
    iter_plain_direct_files,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPORTS = Path(_public_project_path('reports'))
TARGETS = [
    ("agents", REPORTS / "agents", "*.md"),
    ("push", REPORTS / "push", "*.json"),
]
ARCHIVE_DIR = REPORTS / "archive"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_member(archive: zipfile.ZipFile, member: str) -> str:
    digest = hashlib.sha256()
    with archive.open(member, "r") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_month_atomic(
    zip_path: Path,
    files: list[Path],
    source_root: Path,
) -> int:
    """Build and verify a replacement ZIP before deleting any source file."""
    members: dict[str, Path] = {}
    for source in files:
        member = archive_member_name(source, source_root)
        previous = members.get(member)
        if previous is not None and previous != source:
            raise RuntimeError(
                f"archive member collision: {member} maps to multiple files")
        members[member] = source
    source_hashes = {
        member: _sha256_file(source) for member, source in members.items()
    }

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=zip_path.parent,
            prefix=f".{zip_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        if zip_path.exists():
            shutil.copy2(zip_path, temporary)
            with zipfile.ZipFile(temporary, "r") as archive:
                names = archive.namelist()
                if len(names) != len(set(names)):
                    raise RuntimeError(
                        f"archive contains duplicate member names: {zip_path}")
                bad = archive.testzip()
                if bad is not None:
                    raise RuntimeError(
                        f"archive CRC check failed: {zip_path} member={bad}")
                for member, expected in source_hashes.items():
                    if member in names and _sha256_member(
                            archive, member) != expected:
                        raise RuntimeError(
                            "archive member conflicts with source content: "
                            f"{member}")

        mode = "a" if zip_path.exists() else "w"
        with zipfile.ZipFile(
            temporary, mode, zipfile.ZIP_DEFLATED
        ) as archive:
            existing = set(archive.namelist())
            for member, source in members.items():
                if member not in existing:
                    archive.write(source, member)

        with zipfile.ZipFile(temporary, "r") as archive:
            bad = archive.testzip()
            if bad is not None:
                raise RuntimeError(
                    f"replacement archive CRC check failed: member={bad}")
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError(
                    "replacement archive contains duplicate member names")
            for member, expected in source_hashes.items():
                if member not in names:
                    raise RuntimeError(
                        f"replacement archive missing member: {member}")
                if _sha256_member(archive, member) != expected:
                    raise RuntimeError(
                        f"replacement archive content mismatch: {member}")

        os.replace(temporary, zip_path)
        temporary = None

        # Refuse deletion if a writer changed the source while the ZIP was
        # being built.  The verified archive remains safe and the newer source
        # is retained for a later, explicit conflict review.
        for member, source in members.items():
            if _sha256_file(source) != source_hashes[member]:
                raise RuntimeError(
                    f"source changed during archive rotation: {source}")
        for source in members.values():
            source.unlink()
        return len(members)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def rotate(days: int, apply: bool) -> dict:
    cutoff = time.time() - days * 86400
    report: dict = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "days": days, "dry_run": not apply, "groups": {}}
    for name, d, pat in TARGETS:
        if not d.exists():
            report["groups"][name] = {"error": "dir missing"}
            continue
        if name == "push":
            files = list(iter_legacy_and_ymd_files(d, pat))
            layouts_read = ["legacy_flat", "dated_ymd"]
        else:
            files = list(iter_plain_direct_files(d, pat))
            layouts_read = ["legacy_flat"]
        old = [f for f in files if f.stat().st_mtime < cutoff]
        by_month: dict[str, list[Path]] = {}
        for f in old:
            mon = artifact_month_key(f, d)
            by_month.setdefault(mon, []).append(f)
        g = {"candidates": len(old),
             "months": {m: len(fs) for m, fs in sorted(by_month.items())},
             "remaining_after": len(files) - len(old),
             "layouts_read": layouts_read,
             "layout_migration_moves": 0,
             "existing_retention_policy_unchanged": True}
        if apply and old:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            archived = 0
            for mon, fs in by_month.items():
                zp = ARCHIVE_DIR / f"{name}-{mon}.zip"
                archived += _archive_month_atomic(zp, fs, d)
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
