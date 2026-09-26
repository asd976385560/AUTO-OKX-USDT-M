# -*- coding: utf-8 -*-
r"""Manage <PROJECT_ROOT> scratch lifecycle safely.  (v2 — 2026-06-27)

在原版基础上新增两项(根治 tmp 只进不出、GB 级膨胀):
  1. --hard-delete-tmp-days N : tmp/ 根下超过 N 天的文件【直接删除】, 而非搬进 archive
     (默认 None = 关闭, 保持与原版完全兼容)。
  2. --purge-archive (+ --archive-keep-days M, 默认 30) : 清理 tmp/archive/ 自身——
     超期的【日常】归档子目录(纯时间戳 / source-snapshot-*)整体删除, 但用
     ARCHIVE_KEEP_SUBSTR 白名单【保护命名的迁移/库备份】(precutover / cross_market /
     regime-option / *-manual-fix 等), 绝不误删回滚点。默认仍 dry-run, 需同时 --apply 才真删。

安全设计:默认 dry-run 且不写审计库;白名单硬保护核心文档与命名备份;keep 窗口内不动
in-flight;Reviewer 的三日 apply 在任何文件变更前以 BEGIN IMMEDIATE 原子 claim，崩溃、
并发或最终审计更新失败都不会让次日重复清理；结构化限时 marker 可保护待拍板证据子树。
两个 v2 开关都是【显式 opt-in】, 不传则行为同原版。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SKIP_DIR_NAMES = {"archive", "pycache", "__pycache__"}
SKIP_DIR_PREFIXES = ("deploy-tests-",)
STAGING_DIR_TOKEN = "-staging-"
TMP_PROTECT_SENTINEL = ".tmp-cleanup-keep.json"

# The ONLY files allowed to live in <PROJECT_ROOT> root; never touched by the root sweep.
ROOT_KEEP = {"config.md", "README.md", "skill.md", "focus.md"}

# archive/ 子目录名包含这些子串 => 命名的迁移/库/配置备份 = 回滚点, --purge-archive 永不删。
# 纯时间戳目录(如 20260626-143657)与 source-snapshot-* 不在此列 => 视为日常归档, 可按 age 清。
ARCHIVE_KEEP_SUBSTR = (
    "precutover", "cross_market", "regime-option", "manual-fix",
    "agentdeploy", "-registry", "syskeys", "cumpnl", "skill-v2",
    "pre-init", "oneoff-removed", "cleanup-manifest", "predeploy",
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
    kept_protected: int = 0
    protection_marker_invalid: int = 0
    protection_marker_expired: int = 0
    skipped: int = 0
    archived: int = 0
    bytes_archived: int = 0
    root_archived: int = 0
    scratch_archived: int = 0
    empty_db_deleted: int = 0
    surfaced_nonempty_db: int = 0
    # v2 新增
    tmp_hard_deleted: int = 0
    tmp_hard_delete_failed: int = 0
    archive_purged: int = 0
    archive_purge_failed: int = 0
    archive_kept_protected: int = 0
    archive_kept_recent: int = 0
    archive_marker_invalid: int = 0
    archive_marker_expired: int = 0
    # 2026-08-06 新增：tmp 根下遮蔽标准库的 .py（与 age 无关，--apply 即删）
    stdlib_shadow_found: int = 0
    stdlib_shadow_removed: int = 0
    stdlib_shadow_remove_failed: int = 0


_TMP_CLEANUP_RUNS_DDL = """
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


def find_stdlib_shadows(tmp_root: Path) -> list[Path]:
    """tmp 根下与标准库同名的 .py —— 会让**任何在 tmp 里执行的脚本**炸在 import。

    成因：两个 trader 的契约都规定当轮临时执行脚本只能写 `<PROJECT_ROOT>/tmp/`，而 Python
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


def is_recent_staging_subtree(
    path: Path,
    tmp_root: Path,
    now_ts: float,
    keep_seconds: float,
) -> bool:
    """Protect a newly materialized staging tree even when copied files are old.

    Copy operations intentionally preserve source mtimes.  Judging only each
    file's mtime can therefore archive an active ``*-staging-*`` workspace on
    its creation day.  The staging directory's own mtime is the lifecycle
    clock; after the normal keep window expires its old files are eligible
    again, so this is not a permanent cleanup exemption.
    """
    try:
        rel_parts = path.relative_to(tmp_root).parts
    except ValueError:
        return False
    for index, part in enumerate(rel_parts[:-1]):
        if STAGING_DIR_TOKEN not in part:
            continue
        staging_root = tmp_root.joinpath(*rel_parts[: index + 1])
        try:
            return (now_ts - staging_root.stat().st_mtime) < keep_seconds
        except OSError:
            return False
    return False


def tmp_protection_status(
    path: Path,
    tmp_root: Path,
    now_utc: datetime | None = None,
) -> tuple[str, Path | None, dict[str, object] | None]:
    """Return ``valid``, ``expired``, ``invalid`` or ``absent`` for a subtree."""
    marker: Path | None = None
    try:
        path.relative_to(tmp_root)
    except ValueError:
        return "absent", None, None
    current = path.parent
    while current != tmp_root:
        candidate = current / TMP_PROTECT_SENTINEL
        if candidate.is_file():
            marker = candidate
            break
        if tmp_root not in current.parents:
            return "absent", None, None
        current = current.parent
    if marker is None:
        return "absent", None, None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("schema_version must be 1")
        for key in ("reason", "owner", "created_at_cst", "expires_at_cst"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError(f"{key} must be a non-empty string")
        created = datetime.fromisoformat(str(payload["created_at_cst"]))
        expires = datetime.fromisoformat(str(payload["expires_at_cst"]))
        if created.tzinfo is None or expires.tzinfo is None:
            raise ValueError("marker timestamps must be timezone-aware")
        if expires <= created:
            raise ValueError("expires_at_cst must be after created_at_cst")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return "invalid", marker, None
    current_value = now_utc or utc_now()
    if current_value.tzinfo is None:
        return "invalid", marker, None
    current_utc = current_value.astimezone(timezone.utc)
    if current_utc < created.astimezone(timezone.utc):
        return "invalid", marker, None
    if current_utc >= expires.astimezone(timezone.utc):
        return "expired", marker, payload
    return "valid", marker, payload


def is_explicitly_protected_tmp_subtree(
    path: Path,
    tmp_root: Path,
    now_utc: datetime | None = None,
) -> bool:
    """Keep a tmp subtree while an owner decision or audit dependency is open.

    A structured marker protects only its containing directory and descendants.
    It must declare schema/reason/owner/creation/expiry; malformed or expired
    markers do not protect.  A marker directly under ``tmp_root`` is deliberately
    ignored so one accidental file cannot exempt the entire scratch tree.
    Removing the marker restores the normal age policy.
    """
    status, _, _ = tmp_protection_status(path, tmp_root, now_utc)
    return status == "valid"


def archive_subtree_protection(
    subtree: Path,
    tmp_root: Path,
    now_utc: datetime,
) -> tuple[Path | None, int, int]:
    """Audit markers inside one first-level archive purge unit.

    A valid descendant marker retains the complete purge unit because that is
    the deletion granularity.  Sibling units and markers at archive/tmp root do
    not gain protection.  The complete marker set is scanned so malformed and
    expired markers remain observable even when a valid marker also exists.
    """
    active_marker: Path | None = None
    invalid = expired = 0
    try:
        markers = sorted(
            marker for marker in subtree.rglob(TMP_PROTECT_SENTINEL)
            if marker.is_file()
        )
    except OSError:
        markers = []
    for marker in markers:
        status, actual_marker, _ = tmp_protection_status(
            marker, tmp_root, now_utc
        )
        if status == "valid" and active_marker is None:
            active_marker = actual_marker
        elif status == "invalid":
            invalid += 1
        elif status == "expired":
            expired += 1
    return active_marker, invalid, expired


def last_applied_run_utc(db_root: Path) -> datetime | None:
    """Return the latest recorded apply run using a read-only SQLite handle."""
    account_db = db_root / "account.db"
    if not account_db.is_file():
        raise FileNotFoundError(f"cleanup cadence authority missing: {account_db}")
    uri = f"file:{account_db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='tmp_cleanup_runs'"
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            "SELECT run_utc FROM tmp_cleanup_runs "
            "WHERE dry_run=0 ORDER BY run_utc DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    parsed = datetime.fromisoformat(str(row[0]))
    if parsed.tzinfo is None:
        raise ValueError("tmp_cleanup_runs.run_utc must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def cleanup_interval_due(
    db_root: Path,
    now_utc: datetime,
    minimum_interval_days: float,
) -> tuple[bool, datetime | None]:
    """Read-only cadence diagnostic; mutation paths must use atomic claim."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    last_run = last_applied_run_utc(db_root)
    if last_run is None:
        return True, None
    due = now_utc.astimezone(timezone.utc) >= (
        last_run + timedelta(days=minimum_interval_days)
    )
    return due, last_run


def claim_cleanup_run(
    db_root: Path,
    now_utc: datetime,
    minimum_interval_days: float,
) -> dict[str, object]:
    """Atomically claim an apply run before any filesystem mutation.

    The claim itself is a ``dry_run=0`` audit row.  A crash or final audit
    update failure therefore keeps subsequent daily invocations fail-closed
    for the configured interval instead of repeating destructive work.
    ``BEGIN IMMEDIATE`` serializes concurrent Reviewer/manual invocations.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    normalized = now_utc.astimezone(timezone.utc)
    account_db = db_root / "account.db"
    if not account_db.is_file():
        raise FileNotFoundError(f"cleanup cadence authority missing: {account_db}")
    conn = sqlite3.connect(str(account_db), timeout=5.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_TMP_CLEANUP_RUNS_DDL)
        row = conn.execute(
            "SELECT run_utc FROM tmp_cleanup_runs "
            "WHERE dry_run=0 ORDER BY run_utc DESC LIMIT 1"
        ).fetchone()
        last_run: datetime | None = None
        if row:
            last_run = datetime.fromisoformat(str(row[0]))
            if last_run.tzinfo is None:
                raise ValueError(
                    "tmp_cleanup_runs.run_utc must be timezone-aware")
            last_run = last_run.astimezone(timezone.utc)
        if last_run is not None and normalized < (
            last_run + timedelta(days=minimum_interval_days)
        ):
            conn.rollback()
            return {
                "claimed": False,
                "claim_run_utc": None,
                "last_apply_run_utc": last_run,
            }
        claim_run_utc = normalized.isoformat()
        conn.execute(
            """
            INSERT INTO tmp_cleanup_runs
            (run_utc, dry_run, scanned, kept_recent, skipped, archived,
             bytes_archived, archive_dir, raw_json)
            VALUES (?, 0, 0, 0, 0, 0, 0, NULL, ?)
            """,
            (
                claim_run_utc,
                json.dumps({
                    "status": "claimed",
                    "minimum_interval_days": minimum_interval_days,
                    "claimed_at_utc": claim_run_utc,
                }, ensure_ascii=False),
            ),
        )
        conn.commit()
        return {
            "claimed": True,
            "claim_run_utc": claim_run_utc,
            "last_apply_run_utc": last_run,
        }
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def finalize_cleanup_claim(
    db_root: Path,
    claim_run_utc: str,
    stats: CleanupStats,
    archive_dir: Path | None,
) -> None:
    """Complete one previously committed claim; failure leaves claim active."""
    account_db = db_root / "account.db"
    conn = sqlite3.connect(str(account_db), timeout=5.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE tmp_cleanup_runs
               SET scanned=?, kept_recent=?, skipped=?, archived=?,
                   bytes_archived=?, archive_dir=?, raw_json=?
             WHERE run_utc=? AND dry_run=0
            """,
            (
                stats.scanned,
                stats.kept_recent,
                stats.skipped,
                stats.archived,
                stats.bytes_archived,
                str(archive_dir) if archive_dir else None,
                json.dumps({
                    **asdict(stats),
                    "status": (
                        "completed_with_errors"
                        if cleanup_failure_count(stats) else "completed"
                    ),
                }, ensure_ascii=False),
                claim_run_utc,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("cleanup claim row missing during finalization")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def cleanup_failure_count(stats: CleanupStats) -> int:
    """Return confirmed filesystem-operation failures for final status."""
    return (
        stats.tmp_hard_delete_failed
        + stats.archive_purge_failed
        + stats.stdlib_shadow_remove_failed
    )


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
        marker, invalid_markers, expired_markers = archive_subtree_protection(
            sub,
            tmp_root,
            datetime.fromtimestamp(now_ts, timezone.utc),
        )
        stats.archive_marker_invalid += invalid_markers
        stats.archive_marker_expired += expired_markers
        if marker is not None:
            stats.archive_kept_protected += 1
            moves.append({
                "from": str(sub),
                "to": "(KEPT: active structured tmp-cleanup marker)",
                "bytes": 0,
                "kind": "archive_protected_marker",
                "marker": str(marker),
            })
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
        if not dry_run:
            try:
                shutil.rmtree(str(sub))
                if sub.exists():
                    raise OSError("archive subtree still exists after rmtree")
            except OSError as exc:
                stats.archive_purge_failed += 1
                moves.append({
                    "from": str(sub),
                    "to": "(FAILED aged archive purge)",
                    "bytes": size,
                    "kind": "archive_purge_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
        moves.append({
            "from": str(sub),
            "to": (
                "(WOULD DELETE aged archive subdir)" if dry_run
                else "(DELETED aged archive subdir)"
            ),
            "bytes": size,
            "kind": "archive_purge",
        })
        stats.archive_purged += 1
        stats.bytes_archived += size


def record_run(db_root: Path, stats: CleanupStats, archive_dir: Path | None, dry_run: bool) -> None:
    account_db = db_root / "account.db"
    if dry_run or not account_db.exists():
        return
    try:
        conn = sqlite3.connect(str(account_db))
        try:
            conn.execute(_TMP_CLEANUP_RUNS_DDL)
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
    parser = argparse.ArgumentParser(description='Safe scratch lifecycle manager for <PROJECT_ROOT> (v2)'.replace('<PROJECT_ROOT>', _public_project_path()))
    parser.add_argument("--okx-root", default=_public_project_path())
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
    parser.add_argument(
        "--minimum-interval-days", type=float, default=None,
        help="--apply 的最短执行间隔；未到期时只输出 no-op，不移动/删除/写审计库",
    )
    args = parser.parse_args()

    if args.minimum_interval_days is not None and args.minimum_interval_days < 0:
        parser.error("--minimum-interval-days must be >= 0")

    okx_root = Path(args.okx_root)
    tmp_root = okx_root / "tmp"
    db_root = okx_root / "db"
    now_utc = utc_now()
    now_ts = now_utc.timestamp()
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

    # This read-only P1 probe must still run when the three-day cadence gate
    # returns a no-op.  A no-op never removes the shadow, but it must not hide
    # a file known to break the next OPEN/CLOSE import path.
    shadows = find_stdlib_shadows(tmp_root)
    shadow_paths = set(shadows)
    claim_run_utc: str | None = None

    if (
        not dry_run
        and args.minimum_interval_days is not None
        and args.minimum_interval_days > 0
    ):
        try:
            claim = claim_cleanup_run(
                db_root, now_utc, args.minimum_interval_days
            )
        except Exception as exc:
            print(f"[P1] tmp cleanup cadence cannot be verified; fail closed: {exc}")
            return 2
        if claim.get("claimed") is not True:
            last_run = claim.get("last_apply_run_utc")
            report = {
                "okx_root": str(okx_root),
                "tmp_root": str(tmp_root),
                "dry_run": False,
                "status": "SKIPPED_MINIMUM_INTERVAL",
                "minimum_interval_days": args.minimum_interval_days,
                "last_apply_run_utc": (
                    last_run.isoformat()
                    if isinstance(last_run, datetime) else None
                ),
                "database_writes": 0,
                "cleanup_filesystem_changes": 0,
                "stdlib_shadow_found": [p.name for p in shadows],
            }
            if args.report_json:
                out = Path(args.report_json)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if shadows:
                print(
                    "[P1] cadence no-op did not remove tmp stdlib shadows: "
                    + ", ".join(p.name for p in shadows)
                )
                return 2
            return 0
        claim_run_utc = str(claim["claim_run_utc"])

    if not dry_run:
        archive_dir = tmp_root / "archive" / utc_now().strftime("%Y%m%d-%H%M%S")
        archive_dir.mkdir(parents=True, exist_ok=True)

    # --- tmp/ archiving (+ v2 hard-delete) ---
    for path in iter_files(tmp_root):
        stats.scanned += 1
        if should_skip(path, tmp_root):
            stats.skipped += 1
            continue
        # Keep the P1 shadow on its dedicated, age-independent removal path.
        # Otherwise an old shadow can be hard-deleted here first and later be
        # falsely reported as a failed shadow removal.
        if path in shadow_paths:
            stats.skipped += 1
            continue
        protection_status, marker, protection = tmp_protection_status(
            path, tmp_root, now_utc
        )
        if protection_status == "valid":
            stats.kept_protected += 1
            try:
                protected_size = path.stat().st_size
            except OSError:
                protected_size = 0
            moves.append({
                "from": str(path),
                "to": f"(KEPT: {marker})",
                "bytes": protected_size,
                "kind": "tmp_protected",
                "reason": protection.get("reason") if protection else None,
                "expires_at_cst": (
                    protection.get("expires_at_cst") if protection else None
                ),
            })
            continue
        if protection_status == "invalid":
            stats.protection_marker_invalid += 1
            moves.append({
                "from": str(marker),
                "to": "(INVALID: does not protect subtree)",
                "bytes": 0,
                "kind": "tmp_protection_invalid",
            })
        elif protection_status == "expired":
            stats.protection_marker_expired += 1
            moves.append({
                "from": str(marker),
                "to": "(EXPIRED: normal age policy applies)",
                "bytes": 0,
                "kind": "tmp_protection_expired",
            })
        if is_recent_staging_subtree(path, tmp_root, now_ts, keep_seconds):
            stats.kept_recent += 1
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
            if not dry_run:
                try:
                    path.unlink()
                    if path.exists():
                        raise OSError("tmp file still exists after unlink")
                except OSError as exc:
                    stats.tmp_hard_delete_failed += 1
                    moves.append({
                        "from": str(path),
                        "to": "(FAILED tmp file hard-delete)",
                        "bytes": size,
                        "kind": "tmp_hard_delete_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    continue
            moves.append({
                "from": str(path),
                "to": (
                    "(WOULD DELETE tmp file, hard-delete)" if dry_run
                    else "(DELETED tmp file, hard-delete)"
                ),
                "bytes": size,
                "kind": "tmp_hard_delete",
            })
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
    shadow_removed: list[str] = []
    for path in shadows:
        if dry_run:
            moves.append({
                "from": str(path),
                "to": "(WOULD DELETE stdlib shadow)",
                "bytes": 0,
                "kind": "stdlib_shadow",
            })
            continue
        try:
            path.unlink()
            if path.exists():
                raise OSError("stdlib shadow still exists after unlink")
            shadow_removed.append(path.name)
            stats.stdlib_shadow_removed += 1
        except OSError as exc:
            stats.stdlib_shadow_remove_failed += 1
            moves.append({
                "from": str(path),
                "to": "(FAILED stdlib shadow removal)",
                "bytes": 0,
                "kind": "stdlib_shadow_remove_failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"[WARN] 遮蔽文件删除失败 {path}: {exc}")
    stats.stdlib_shadow_found = len(shadows)
    if shadows:
        # Keep the f-string replacement field on a single line: an expression
        # that spans lines inside ``{...}`` is PEP 701 syntax (Python >= 3.12)
        # and is a SyntaxError on 3.11, which broke ``import tmp_cleanup``.
        shadow_action = (
            "已删除" if not dry_run
            else "下一笔 OPEN/CLOSE 会炸在 import；加 --apply 删除，或手工移走"
        )
        print(f"[P1] tmp 根下有 {len(shadows)} 个文件遮蔽标准库："
              f"{', '.join(p.name for p in shadows)}"
              f"——{shadow_action}")

    operation_failures = cleanup_failure_count(stats)
    report = {
        "okx_root": str(okx_root),
        "tmp_root": str(tmp_root),
        "dry_run": dry_run,
        "status": (
            "COMPLETED_WITH_ERRORS" if operation_failures else "COMPLETED"
        ),
        "claim_run_utc": claim_run_utc,
        "operation_failure_count": operation_failures,
        "minimum_interval_days": args.minimum_interval_days,
        "hard_delete_tmp_days": args.hard_delete_tmp_days,
        "purge_archive": args.purge_archive,
        "archive_keep_days": args.archive_keep_days if args.purge_archive else None,
        "archive_dir": str(archive_dir) if archive_dir else None,
        "stdlib_shadow_found": [p.name for p in shadows],
        "stdlib_shadow_removed": shadow_removed,
        "stats": asdict(stats),
        "moves_sample": moves[:100],
    }

    if claim_run_utc is not None:
        try:
            finalize_cleanup_claim(
                db_root, claim_run_utc, stats, archive_dir
            )
        except Exception as exc:
            report["status"] = "CLAIM_FINALIZATION_FAILED"
            report["claim_finalization_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            if args.report_json:
                out = Path(args.report_json)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print(f"[P1] cleanup claim finalization failed: {exc}")
            return 2
    else:
        record_run(db_root, stats, archive_dir, dry_run)
    if args.report_json:
        out = Path(args.report_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if operation_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
