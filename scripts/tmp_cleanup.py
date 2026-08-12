# -*- coding: utf-8 -*-
r"""Manage . scratch lifecycle safely.  (v2 — 2026-06-27)

在原版基础上新增两项(根治 tmp 只进不出、GB 级膨胀):
  1. --hard-delete-tmp-days N : tmp/ 根下超过 N 天的文件【直接删除】, 而非搬进 archive
     (默认 None = 关闭, 保持与原版完全兼容)。
  2. --purge-archive (+ --archive-keep-days M, 默认 30) : 清理 tmp/archive/ 自身——
     超期的【日常】归档子目录(纯时间戳 / source-snapshot-*)整体删除, 但用
     ARCHIVE_KEEP_SUBSTR 白名单【保护命名的迁移/库备份】(precutover / cross_market /
     regime-option / *-manual-fix 等), 绝不误删回滚点。默认仍 dry-run, 需同时 --apply 才真删。

安全设计:默认 dry-run 且不写审计库;白名单硬保护核心文档与命名备份;keep 窗口内不动
in-flight;仅 --apply 后写 account.db.tmp_cleanup_runs 审计。两个新开关都是【显式
opt-in】, 不传则行为同原版。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SKIP_DIR_NAMES = {"archive", "pycache", "__pycache__"}
SKIP_DIR_PREFIXES = ("deploy-tests-",)

# The ONLY files allowed to live in . root; never touched by the root sweep.
ROOT_KEEP = {"config.md", "README.md", "skill.md", "focus.md"}

# archive/ 子目录名包含这些子串 => 命名的迁移/库/配置备份 = 回滚点, --purge-archive 永不删。
# 纯时间戳目录(如 20260626-143657)与 source-snapshot-* 不在此列 => 视为日常归档, 可按 age 清。
ARCHIVE_KEEP_SUBSTR = (
    "precutover", "cross_market", "regime-option", "manual-fix",
    "agentdeploy", "-registry", "syskeys", "cumpnl", "skill-v2",
    "pre-init", "oneoff-removed", "cleanup-manifest",
    "migrated-sidecar",  # 2026-07-17：OpenClaw 7.1 迁移修复锚冷归档（214MB zip，回滚点）
    "ledger-repair", "vanished-repair",  # 生产账本/仓位修复回滚点，禁止通用归档轮转删除
    # 2026-08-06 demo 全量下线：被删的角色文件/脚本/测试的唯一副本在
    # tmp/archive/20260806-demo-removal/。它是回滚点，不是日常草稿——名字里
    # 没有上面任何一个既有子串，不加这条就会在 30 天后被当普通归档轮转掉。
    "demo-removal",
)


@dataclass
class CleanupStats:
    scanned: int = 0
    kept_recent: int = 0
    skipped: int = 0
    archived: int = 0
    bytes_archived: int = 0
    root_archived: int = 0
    scratch_archived: int = 0
    empty_db_deleted: int = 0
    surfaced_nonempty_db: int = 0
    # v2 新增
    tmp_hard_deleted: int = 0
    archive_purged: int = 0
    archive_kept_protected: int = 0
    archive_kept_recent: int = 0
    # 2026-08-06 新增：tmp 根下遮蔽标准库的 .py（与 age 无关，--apply 即删）
    stdlib_shadow_found: int = 0
    stdlib_shadow_removed: int = 0


def find_stdlib_shadows(tmp_root: Path) -> list[Path]:
    """tmp 根下与标准库同名的 .py —— 会让**任何在 tmp 里执行的脚本**炸在 import。

    成因：两个 trader 的契约都规定当轮临时执行脚本只能写 `./tmp/`，而 Python
    会把脚本自身目录放进 `sys.path[0]`。tmp 里一旦落下 `bisect.py` / `inspect.py`
    这类调试残留，`order_executor` 的 `import tempfile` → `random` → `bisect` 就会
    解析到残留文件，**下一笔 OPEN/CLOSE 直接 ImportError**。

    2026-08-06 实证：13:34 落下的 bisect.py 埋了 6.4h 未引爆——期间双盘全是 HOLD，
    而 HOLD 回执走 `collectors/trades_writer.py`（sys.path[0] 是 collectors/），绕开
    了这条路径；直到手动平 demo 仓时才炸出来。性质同「部署漂移」：测试全绿、日志
    正常，只在真要下单那一刻失败。

    只扫 tmp **根目录**：sys.path[0] 只会是脚本自己所在的目录，archive/ 子目录里的
    同名文件不参与解析，不必误报。
    """
    if not tmp_root.is_dir():
        return []
    std = set(sys.stdlib_module_names)
    hits = []
    for path in sorted(tmp_root.glob("*.py")):
        if path.is_file() and path.stem in std:
            hits.append(path)
    return hits


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def should_skip(path: Path, tmp_root: Path) -> bool:
    rel_parts = path.relative_to(tmp_root).parts
    for part in rel_parts[:-1]:
        if part in SKIP_DIR_NAMES or any(part.startswith(p) for p in SKIP_DIR_PREFIXES):
            return True
    return False


def iter_files(tmp_root: Path):
    for path in tmp_root.rglob("*"):
        if path.is_file():
            yield path


def _move_into(path: Path, dest_dir: Path, dry_run: bool) -> None:
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))


def sweep_root_scratch(okx_root, archive_dir, now_ts, max_age_seconds, dry_run, stats, moves):
    """Archive top-level ROOT files that are not core docs. NON-recursive."""
    for path in okx_root.glob("*"):
        if not path.is_file() or path.name in ROOT_KEEP:
            continue
        if path.suffix == ".db":
            continue
        try:
            age = now_ts - path.stat().st_mtime
            size = path.stat().st_size
        except OSError:
            continue
        if age < max_age_seconds:
            stats.kept_recent += 1
            continue
        dest = (archive_dir / "root") if archive_dir else Path("root")
        moves.append({"from": str(path), "to": str(dest / path.name), "bytes": size, "kind": "root"})
        _move_into(path, dest, dry_run)
        stats.root_archived += 1
        stats.bytes_archived += size


def sweep_dir_scratch(scratch_dir, archive_dir, now_ts, max_age_seconds, dry_run, stats, moves, label):
    if not scratch_dir.exists():
        return
    for path in scratch_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            age = now_ts - path.stat().st_mtime
            size = path.stat().st_size
        except OSError:
            continue
        if age < max_age_seconds:
            stats.kept_recent += 1
            continue
        dest = (archive_dir / label) if archive_dir else Path(label)
        moves.append({"from": str(path), "to": str(dest / path.name), "bytes": size, "kind": label})
        _move_into(path, dest, dry_run)
        stats.scratch_archived += 1
        stats.bytes_archived += size


def sweep_empty_root_db(okx_root, now_ts, max_age_seconds, dry_run, stats, moves):
    for path in okx_root.glob("*.db"):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
            age = now_ts - path.stat().st_mtime
        except OSError:
            continue
        if size != 0:
            stats.surfaced_nonempty_db += 1
            moves.append({"from": str(path), "to": "(SURFACED: non-empty ROOT db, left untouched)", "bytes": size, "kind": "surfaced_db"})
            continue
        if path.with_name(path.name + "-wal").exists() or path.with_name(path.name + "-shm").exists():
            continue
        if age < max_age_seconds:
            continue
        moves.append({"from": str(path), "to": "(deleted: 0-byte stray db)", "bytes": 0, "kind": "empty_db"})
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                continue
        stats.empty_db_deleted += 1


def purge_archive(tmp_root, now_ts, keep_seconds, dry_run, stats, moves):
    """v2: 清理 tmp/archive/ 自身。删除超期的【日常】归档子目录, 但白名单保护命名迁移/库备份。

    判龄按【子目录内最新文件 mtime】(避免删刚写入的归档)。目录名含 ARCHIVE_KEEP_SUBSTR
    任一子串 => 保护不删。空目录(无文件)按目录自身 mtime 判。"""
    archive_root = tmp_root / "archive"
    if not archive_root.exists():
        return
    for sub in sorted(archive_root.iterdir()):
        if not sub.is_dir():
            continue
        name = sub.name
        if any(k in name for k in ARCHIVE_KEEP_SUBSTR):
            stats.archive_kept_protected += 1
            moves.append({"from": str(sub), "to": "(KEPT: whitelisted migration/backup)", "bytes": 0, "kind": "archive_protected"})
            continue
        try:
            files = [p for p in sub.rglob("*") if p.is_file()]
            mtimes = [p.stat().st_mtime for p in files]
            size = sum(p.stat().st_size for p in files)
        except OSError:
            continue
        newest = max(mtimes) if mtimes else sub.stat().st_mtime
        if (now_ts - newest) < keep_seconds:
            stats.archive_kept_recent += 1
            continue
        moves.append({"from": str(sub), "to": "(DELETED aged archive subdir)", "bytes": size, "kind": "archive_purge"})
        if not dry_run:
            shutil.rmtree(str(sub), ignore_errors=True)
        stats.archive_purged += 1
        stats.bytes_archived += size


def record_run(db_root: Path, stats: CleanupStats, archive_dir: Path | None, dry_run: bool) -> None:
    account_db = db_root / "account.db"
    if dry_run or not account_db.exists():
        return
    try:
        conn = sqlite3.connect(str(account_db))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tmp_cleanup_runs (
                    run_utc TEXT PRIMARY KEY,
                    dry_run INTEGER NOT NULL,
                    scanned INTEGER NOT NULL,
                    kept_recent INTEGER NOT NULL,
                    skipped INTEGER NOT NULL,
                    archived INTEGER NOT NULL,
                    bytes_archived INTEGER NOT NULL,
                    archive_dir TEXT,
                    raw_json TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO tmp_cleanup_runs
                (run_utc, dry_run, scanned, kept_recent, skipped, archived, bytes_archived, archive_dir, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now().isoformat(),
                    int(dry_run),
                    stats.scanned,
                    stats.kept_recent,
                    stats.skipped,
                    stats.archived,
                    stats.bytes_archived,
                    str(archive_dir) if archive_dir else None,
                    json.dumps(asdict(stats), ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Cleanup should not fail the caller merely because audit write failed.
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe scratch lifecycle manager for . (v2)")
    parser.add_argument("--okx-root", default=".")
    parser.add_argument("--keep-days", type=float, default=3.0, help="Keep tmp/ files newer than this many days")
    parser.add_argument("--archive-days", type=float, default=3.0, help="Archive tmp/ files older than this many days")
    parser.add_argument("--scratch-keep-hours", type=float, default=6.0,
                        help="Keep ROOT / collectors-scratch / empty-db newer than this many hours")
    parser.add_argument("--apply", action="store_true", help="Actually move/delete; default is dry-run")
    parser.add_argument("--report-json", default=None, help="Optional report output path")
    # ---- v2 新增开关(都是显式 opt-in, 不传则行为同原版) ----
    parser.add_argument("--hard-delete-tmp-days", type=float, default=None,
                        help="若设置: tmp/ 根超过该天数的文件【直接删除】而非搬 archive(根治 archive 膨胀)")
    parser.add_argument("--purge-archive", action="store_true",
                        help="额外清理 tmp/archive/ 下超期的日常归档子目录(白名单保护迁移/库备份)")
    parser.add_argument("--archive-keep-days", type=float, default=30.0,
                        help="--purge-archive 时, 保留近该天数的归档子目录")
    args = parser.parse_args()

    okx_root = Path(args.okx_root)
    tmp_root = okx_root / "tmp"
    db_root = okx_root / "db"
    now_ts = utc_now().timestamp()
    keep_seconds = args.keep_days * 86400
    archive_seconds = args.archive_days * 86400
    scratch_max_age = args.scratch_keep_hours * 3600
    hard_delete_seconds = args.hard_delete_tmp_days * 86400 if args.hard_delete_tmp_days is not None else None
    dry_run = not args.apply

    stats = CleanupStats()
    archive_dir: Path | None = None
    moves: list[dict[str, object]] = []

    if not tmp_root.exists():
        print(f"[WARN] tmp root missing: {tmp_root}")
        return 0

    if not dry_run:
        archive_dir = tmp_root / "archive" / utc_now().strftime("%Y%m%d-%H%M%S")
        archive_dir.mkdir(parents=True, exist_ok=True)

    # --- tmp/ archiving (+ v2 hard-delete) ---
    for path in iter_files(tmp_root):
        stats.scanned += 1
        if should_skip(path, tmp_root):
            stats.skipped += 1
            continue
        try:
            age_seconds = now_ts - path.stat().st_mtime
            size = path.stat().st_size
        except OSError:
            stats.skipped += 1
            continue

        if age_seconds < keep_seconds:
            stats.kept_recent += 1
            continue
        if age_seconds < archive_seconds:
            stats.kept_recent += 1
            continue

        # v2: 够旧且开启 hard-delete => 直接删, 不搬 archive(否则 archive 只进不出)
        if hard_delete_seconds is not None and age_seconds >= hard_delete_seconds:
            moves.append({"from": str(path), "to": "(DELETED tmp file, hard-delete)", "bytes": size, "kind": "tmp_hard_delete"})
            if not dry_run:
                try:
                    path.unlink()
                except OSError:
                    pass
            stats.tmp_hard_deleted += 1
            stats.bytes_archived += size
            continue

        rel = path.relative_to(tmp_root)
        moves.append({"from": str(path), "to": str((archive_dir / rel) if archive_dir else rel), "bytes": size})
        if not dry_run and archive_dir is not None:
            dest = archive_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
        stats.archived += 1
        stats.bytes_archived += size

    # --- scratch sweeps (root / collectors-scratch / empty root db) ---
    sweep_root_scratch(okx_root, archive_dir, now_ts, scratch_max_age, dry_run, stats, moves)
    sweep_dir_scratch(okx_root / "collectors" / "scratch", archive_dir, now_ts, scratch_max_age, dry_run, stats, moves, "collectors-scratch")
    sweep_empty_root_db(okx_root, now_ts, scratch_max_age, dry_run, stats, moves)

    # --- v2: archive 自身保留策略(显式 opt-in) ---
    if args.purge_archive:
        purge_archive(tmp_root, now_ts, args.archive_keep_days * 86400, dry_run, stats, moves)

    # --- 标准库遮蔽（与 age 无关）---
    # tmp 里没有任何合法文件该叫标准库的名字，所以这一类不走 keep-days 保护：
    # dry-run 一律外显，--apply 一律删。留着它比留一个超期草稿危险得多——
    # 它会让下一笔 OPEN/CLOSE 炸在 import（详见 find_stdlib_shadows docstring）。
    shadows = find_stdlib_shadows(tmp_root)
    shadow_removed: list[str] = []
    for path in shadows:
        if dry_run:
            continue
        try:
            path.unlink()
            shadow_removed.append(path.name)
            stats.stdlib_shadow_removed += 1
        except OSError as exc:
            print(f"[WARN] 遮蔽文件删除失败 {path}: {exc}")
    stats.stdlib_shadow_found = len(shadows)
    if shadows:
        print(f"[P1] tmp 根下有 {len(shadows)} 个文件遮蔽标准库："
              f"{', '.join(p.name for p in shadows)}"
              f"——{'已删除' if not dry_run else '下一笔 OPEN/CLOSE 会炸在 import；'
                 '加 --apply 删除，或手工移走'}")

    report = {
        "okx_root": str(okx_root),
        "tmp_root": str(tmp_root),
        "dry_run": dry_run,
        "hard_delete_tmp_days": args.hard_delete_tmp_days,
        "purge_archive": args.purge_archive,
        "archive_keep_days": args.archive_keep_days if args.purge_archive else None,
        "archive_dir": str(archive_dir) if archive_dir else None,
        "stdlib_shadow_found": [p.name for p in shadows],
        "stdlib_shadow_removed": shadow_removed,
        "stats": asdict(stats),
        "moves_sample": moves[:100],
    }

    if args.report_json:
        out = Path(args.report_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    record_run(db_root, stats, archive_dir, dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
