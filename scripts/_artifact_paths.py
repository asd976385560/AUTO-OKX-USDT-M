# -*- coding: utf-8 -*-
"""Shared path and bounded-reader helpers for ``YYYY/MM/DD`` artifacts.

The first migration family is intentionally narrow: per-cycle diagnostics in
``reports/push``.  Historical flat files remain where they are and stay
readable to maintenance code; only cycles at or after the preregistered
activation boundary receive the dated layout.

Pure path helpers are kept for layout decisions.  The explicit writer creates
only one validated artifact and rejects symlink, junction, and other reparse
points before and after every root/year/month/day/file step.  The bounded
reader applies the same rejection before descending.  This module never moves,
copies, deletes, or rewrites historical artifacts.
"""
from __future__ import annotations

import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator


REPORTS_PUSH_YMD_ACTIVATION_CYCLE = "2026-08-18T00:00"
_CYCLE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)$"
)
_YEAR_RE = re.compile(r"^\d{4}$")
_MONTH_DAY_RE = re.compile(r"^\d{2}$")


class UnsafeArtifactPathError(RuntimeError):
    """Structured fail-closed error for an unsafe artifact write path."""

    code = "unsafe_artifact_path"

    def __init__(
        self,
        *,
        stage: str,
        path: Path,
        reason: str,
        target_path: Path,
    ) -> None:
        self.stage = stage
        self.path = Path(path)
        self.reason = reason
        self.target_path = Path(target_path)
        super().__init__(
            f"{self.code}: stage={stage} reason={reason} path={self.path}"
        )

    def audit_fields(self) -> dict[str, str]:
        """Return stable fields suitable for the existing pipeline run-log."""
        return {
            "error_code": self.code,
            "unsafe_stage": self.stage,
            "unsafe_reason": self.reason,
            "unsafe_path": str(self.path),
        }


def parse_cycle(value: str) -> datetime:
    """Parse one canonical UTC+8 natural 15-minute cycle (timezone implicit)."""
    text = str(value or "").strip()
    if not _CYCLE_RE.fullmatch(text):
        raise ValueError("cycle must be YYYY-MM-DDTHH:00|15|30|45")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M")


def layout_for_cycle(
    cycle: str,
    *,
    activation_cycle: str = REPORTS_PUSH_YMD_ACTIVATION_CYCLE,
) -> str:
    """Return ``dated_ymd`` after activation, otherwise ``legacy_flat``."""
    return (
        "dated_ymd"
        if parse_cycle(cycle) >= parse_cycle(activation_cycle)
        else "legacy_flat"
    )


def forward_artifact_dir(
    root: Path | str,
    cycle: str,
    *,
    activation_cycle: str = REPORTS_PUSH_YMD_ACTIVATION_CYCLE,
) -> Path:
    """Resolve the only write directory for a cycle without touching history."""
    base = Path(root)
    parsed = parse_cycle(cycle)
    if parsed < parse_cycle(activation_cycle):
        return base
    return base / f"{parsed.year:04d}" / f"{parsed.month:02d}" / f"{parsed.day:02d}"


def forward_artifact_path(
    root: Path | str,
    cycle: str,
    filename: str,
    *,
    activation_cycle: str = REPORTS_PUSH_YMD_ACTIVATION_CYCLE,
) -> Path:
    """Return a safe basename under the cycle's forward-only write directory."""
    name = str(filename or "")
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("artifact filename must be one basename")
    return forward_artifact_dir(
        root,
        cycle,
        activation_cycle=activation_cycle,
    ) / name


def _valid_ymd_parts(parts: tuple[str, ...]) -> bool:
    if (
        len(parts) < 3
        or not _YEAR_RE.fullmatch(parts[0])
        or not _MONTH_DAY_RE.fullmatch(parts[1])
        or not _MONTH_DAY_RE.fullmatch(parts[2])
    ):
        return False
    try:
        datetime.strptime("-".join(parts[:3]), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _under_resolved_root(path: Path, root: Path) -> bool:
    """Provide a final resolved-root check after per-level reparse rejection."""
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _is_reparse_point(path: Path) -> bool:
    """Fail closed for symlinks, Windows junctions, and other reparse points."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _is_plain_directory(path: Path) -> bool:
    """Return true only for an existing non-reparse directory."""
    if _is_reparse_point(path):
        return False
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_plain_file(path: Path) -> bool:
    """Return true only for an existing non-reparse regular file."""
    if _is_reparse_point(path):
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _writer_lstat(
    path: Path,
    *,
    stage: str,
    target_path: Path,
):
    """Inspect an entry without following it; only true absence is optional."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnsafeArtifactPathError(
            stage=stage,
            path=path,
            reason=f"lstat_failed_errno_{exc.errno}",
            target_path=target_path,
        ) from exc


def _writer_is_reparse(info) -> bool:
    return bool(
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _writer_require_directory(
    path: Path,
    *,
    root: Path,
    stage: str,
    target_path: Path,
) -> bool:
    """Validate one existing directory, returning false only when absent."""
    info = _writer_lstat(path, stage=stage, target_path=target_path)
    if info is None:
        return False
    if _writer_is_reparse(info):
        raise UnsafeArtifactPathError(
            stage=stage,
            path=path,
            reason="reparse_point",
            target_path=target_path,
        )
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeArtifactPathError(
            stage=stage,
            path=path,
            reason="not_directory",
            target_path=target_path,
        )
    if not _under_resolved_root(path, root):
        raise UnsafeArtifactPathError(
            stage=stage,
            path=path,
            reason="resolved_outside_root",
            target_path=target_path,
        )
    return True


def _writer_require_create_parent(
    path: Path,
    *,
    root: Path,
    stage: str,
    target_path: Path,
) -> None:
    """Reject an unsafe or missing immediate parent before one-level mkdir."""
    parent = path.parent
    info = _writer_lstat(parent, stage=stage, target_path=target_path)
    reason = None
    if info is None:
        reason = "parent_missing"
    elif _writer_is_reparse(info):
        reason = "parent_reparse_point"
    elif not stat.S_ISDIR(info.st_mode):
        reason = "parent_not_directory"
    elif path != root and not _under_resolved_root(parent, root):
        reason = "parent_resolved_outside_root"
    if reason:
        raise UnsafeArtifactPathError(
            stage=stage,
            path=parent,
            reason=reason,
            target_path=target_path,
        )


def _writer_ensure_directory(
    path: Path,
    *,
    root: Path,
    stage: str,
    target_path: Path,
) -> None:
    """Check before and after creating exactly one ordinary directory level."""
    if not _writer_require_directory(
        path, root=root, stage=stage, target_path=target_path
    ):
        _writer_require_create_parent(
            path, root=root, stage=stage, target_path=target_path
        )
        try:
            path.mkdir()
        except FileExistsError:
            # A concurrent creator is acceptable only if the post-check proves
            # it created the expected ordinary in-root directory.
            pass
        except OSError as exc:
            raise UnsafeArtifactPathError(
                stage=stage,
                path=path,
                reason=f"mkdir_failed_errno_{exc.errno}",
                target_path=target_path,
            ) from exc
    _writer_require_directory(
        path, root=root, stage=stage, target_path=target_path
    )


def _writer_require_file(
    path: Path,
    *,
    root: Path,
    target_path: Path,
    allow_missing: bool,
) -> bool:
    """Validate the target without following it, before or after creation."""
    info = _writer_lstat(path, stage="file", target_path=target_path)
    if info is None:
        if allow_missing:
            return False
        raise UnsafeArtifactPathError(
            stage="file",
            path=path,
            reason="missing_after_write",
            target_path=target_path,
        )
    if _writer_is_reparse(info):
        raise UnsafeArtifactPathError(
            stage="file",
            path=path,
            reason="reparse_point",
            target_path=target_path,
        )
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeArtifactPathError(
            stage="file",
            path=path,
            reason="not_regular_file",
            target_path=target_path,
        )
    if not _under_resolved_root(path, root):
        raise UnsafeArtifactPathError(
            stage="file",
            path=path,
            reason="resolved_outside_root",
            target_path=target_path,
        )
    return True


def write_forward_text_artifact(
    root: Path | str,
    cycle: str,
    filename: str,
    content: str | Callable[[Path], str],
    *,
    activation_cycle: str = REPORTS_PUSH_YMD_ACTIVATION_CYCLE,
    encoding: str = "utf-8",
) -> Path:
    """Create one forward artifact after per-level pre/post safety checks.

    The content callback, when supplied, receives the final lexical path only
    after its directories are validated.  File opening remains inside this
    helper so callers cannot bypass the file-level reparse check.
    """
    base = Path(root)
    target = forward_artifact_path(
        base,
        cycle,
        filename,
        activation_cycle=activation_cycle,
    )
    parsed = parse_cycle(cycle)
    directories: list[tuple[str, Path]] = [("root", base)]
    if parsed >= parse_cycle(activation_cycle):
        directories.extend([
            ("year", base / f"{parsed.year:04d}"),
            ("month", base / f"{parsed.year:04d}" / f"{parsed.month:02d}"),
            (
                "day",
                base
                / f"{parsed.year:04d}"
                / f"{parsed.month:02d}"
                / f"{parsed.day:02d}",
            ),
        ])

    for stage, directory in directories:
        _writer_ensure_directory(
            directory,
            root=base,
            stage=stage,
            target_path=target,
        )
    _writer_require_file(
        target,
        root=base,
        target_path=target,
        allow_missing=True,
    )

    rendered = content(target) if callable(content) else content
    if not isinstance(rendered, str):
        raise TypeError("artifact content callback must return str")

    # The callback is caller code: repeat the complete chain immediately
    # before opening the target, then again after the write.
    for stage, directory in directories:
        _writer_require_directory(
            directory,
            root=base,
            stage=stage,
            target_path=target,
        )
    _writer_require_file(
        target,
        root=base,
        target_path=target,
        allow_missing=True,
    )
    with target.open("w", encoding=encoding) as handle:
        handle.write(rendered)
    for stage, directory in directories:
        _writer_require_directory(
            directory,
            root=base,
            stage=stage,
            target_path=target,
        )
    _writer_require_file(
        target,
        root=base,
        target_path=target,
        allow_missing=False,
    )
    return target


def _validated_direct_pattern(pattern: str) -> str:
    direct_pattern = str(pattern or "")
    if (
        not direct_pattern
        or Path(direct_pattern).name != direct_pattern
        or "/" in direct_pattern
        or "\\" in direct_pattern
        or direct_pattern in {".", ".."}
    ):
        raise ValueError("artifact pattern must address direct children only")
    return direct_pattern


def _iter_plain_direct_files(base: Path, direct_pattern: str) -> Iterator[Path]:
    if not _is_plain_directory(base):
        return
    for path in sorted(base.glob(direct_pattern)):
        if _is_plain_file(path) and _under_resolved_root(path, base):
            yield path


def iter_plain_direct_files(root: Path | str, pattern: str) -> Iterator[Path]:
    """Yield only ordinary direct children; never enter a dated subtree."""
    base = Path(root)
    yield from _iter_plain_direct_files(
        base,
        _validated_direct_pattern(pattern),
    )


def iter_legacy_and_ymd_files(root: Path | str, pattern: str) -> Iterator[Path]:
    """Yield flat legacy files plus strict direct ``YYYY/MM/DD`` descendants.

    Deliberately avoid an unrestricted recursive glob: unrelated backups,
    scratch folders, or future control-state directories must not silently
    enter a deletion/rotation scope.  The root, year, month, day, and yielded
    file must each be ordinary filesystem entries, never reparse points.
    """
    base = Path(root)
    direct_pattern = _validated_direct_pattern(pattern)
    yielded: set[Path] = set()
    for path in _iter_plain_direct_files(base, direct_pattern):
        yielded.add(path)
        yield path
    if not _is_plain_directory(base):
        return
    for year in sorted(base.iterdir()):
        if not _YEAR_RE.fullmatch(year.name) or not _is_plain_directory(year):
            continue
        for month in sorted(year.iterdir()):
            if (
                not _MONTH_DAY_RE.fullmatch(month.name)
                or not _is_plain_directory(month)
            ):
                continue
            for day in sorted(month.iterdir()):
                parts = (year.name, month.name, day.name)
                if not _valid_ymd_parts(parts) or not _is_plain_directory(day):
                    continue
                for path in sorted(day.glob(direct_pattern)):
                    if (
                        _is_plain_file(path)
                        and _under_resolved_root(path, base)
                        and path not in yielded
                    ):
                        yielded.add(path)
                        yield path


def archive_member_name(path: Path | str, root: Path | str) -> str:
    """Preserve the dated relative path in zip; keep legacy flat names stable."""
    file_path = Path(path)
    base = Path(root)
    relative = file_path.relative_to(base)
    if _valid_ymd_parts(tuple(relative.parts)):
        return relative.as_posix()
    if len(relative.parts) != 1:
        raise ValueError(f"unsupported artifact layout: {relative}")
    return relative.name


def artifact_month_key(path: Path | str, root: Path | str) -> str:
    """Use the path's declared YYYY/MM for dated files, mtime for legacy files."""
    file_path = Path(path)
    relative = file_path.relative_to(Path(root))
    if _valid_ymd_parts(tuple(relative.parts)):
        return relative.parts[0] + relative.parts[1]
    if len(relative.parts) != 1:
        raise ValueError(f"unsupported artifact layout: {relative}")
    return datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y%m")
