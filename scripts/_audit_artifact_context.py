# -*- coding: utf-8 -*-
"""Separate natural-production quality artifacts from tests and probes."""

from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(_public_project_path())
PRODUCTION_QUALITY_ROOT = ROOT / "reports" / "quality"
CST = timezone(timedelta(hours=8))
CONTEXTS = {"production", "test", "probe"}


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def resolve_audit_output(
    requested: str | Path,
    *,
    tool_name: str,
    execution_context: str | None = None,
    artifact_root: str | Path | None = None,
) -> tuple[Path, str, dict]:
    """Return actual output, context and public provenance fields.

    A non-production output supplied by a test remains where requested.
    A direct/manual attempt to overwrite the canonical quality directory
    defaults to a timestamped probe artifact unless its parent runtime has
    explicitly declared ``OKX_AUDIT_EXECUTION_CONTEXT=production``.
    """
    target = Path(requested)
    explicit = execution_context is not None
    context = str(
        execution_context
        or os.environ.get("OKX_AUDIT_EXECUTION_CONTEXT")
        or ("probe" if _within(target, PRODUCTION_QUALITY_ROOT) else "test")
    ).strip().lower()
    if context not in CONTEXTS:
        raise ValueError(f"invalid audit execution context: {context!r}")
    if context == "production":
        if artifact_root is not None:
            raise ValueError("production audit output cannot use artifact_root")
        actual = target
    elif artifact_root is None and not _within(target, PRODUCTION_QUALITY_ROOT):
        actual = target
    else:
        root = Path(artifact_root) if artifact_root is not None else (
            ROOT / "tmp" / "audit-artifacts" / context / tool_name
            / datetime.now(CST).strftime("%Y%m%d-%H%M%S-%f")
        )
        actual = root / target.name
    return actual, context, {
        "execution_context": context,
        "natural_production_evidence": context == "production",
        "requested_output": str(target),
        "actual_output": str(actual),
        "context_explicit": explicit,
    }
