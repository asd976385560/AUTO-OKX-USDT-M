# -*- coding: utf-8 -*-
"""Strict cycle-id validation and safe artifact-name formatting.

The public runtime contract accepts only a real minute timestamp in the exact
``YYYY-MM-DDTHH:MM`` shape.  Formatting helpers validate before deriving any
path/session token, so callers cannot accidentally turn traversal text into a
partially-sanitized filename.
"""
from __future__ import annotations

import re
from datetime import datetime


_CYCLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
_CYCLE_FORMAT = "%Y-%m-%dT%H:%M"


def _parse_cycle_id(value: object) -> datetime:
    if not isinstance(value, str) or _CYCLE_RE.fullmatch(value) is None:
        raise ValueError("cycle_id must use exact YYYY-MM-DDTHH:MM format")
    try:
        parsed = datetime.strptime(value, _CYCLE_FORMAT)
    except ValueError as exc:
        raise ValueError(
            "cycle_id must be a real YYYY-MM-DDTHH:MM timestamp"
        ) from exc
    # Keep the comparison explicit: it documents that permissive parser
    # normalization must never become part of the external contract.
    if parsed.strftime(_CYCLE_FORMAT) != value:
        raise ValueError("cycle_id must use canonical YYYY-MM-DDTHH:MM format")
    return parsed


def validate_cycle_id(value: object) -> str:
    """Return a valid cycle id or raise ``ValueError`` without side effects."""
    _parse_cycle_id(value)
    return value


def cycle_session_token(value: object) -> str:
    """Return ``YYYYMMDD-HHMM`` for OpenClaw session keys."""
    return _parse_cycle_id(value).strftime("%Y%m%d-%H%M")


def cycle_status_token(value: object) -> str:
    """Return ``YYYY-MM-DDTHH-MM`` for stage status filenames."""
    return _parse_cycle_id(value).strftime("%Y-%m-%dT%H-%M")


def cycle_artifact_token(value: object) -> str:
    """Return ``YYYY-MM-DD-HHMM`` for push pipeline artifacts."""
    return _parse_cycle_id(value).strftime("%Y-%m-%d-%H%M")
