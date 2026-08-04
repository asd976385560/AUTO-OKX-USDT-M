# -*- coding: utf-8 -*-
"""Validate the public release version without Git, network, or runtime data.

``VERSION`` is the single public-release version source and intentionally does
not replace the internal V2.0 architecture, document, or schema identifiers.
Stable and prerelease SemVer values are accepted; build metadata is rejected so
one VERSION value maps to exactly one Git tag and GitHub Release.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"

_NUMERIC = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_ID = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_PATTERN = (
    rf"(?P<major>{_NUMERIC})\."
    rf"(?P<minor>{_NUMERIC})\."
    rf"(?P<patch>{_NUMERIC})"
    rf"(?:-(?P<prerelease>{_PRERELEASE_ID}(?:\.{_PRERELEASE_ID})*))?"
)
SEMVER_RE = re.compile(rf"^{SEMVER_PATTERN}$")
CHANGELOG_HEADING_RE = re.compile(
    r"^## \[([^\]]+)\](?: - ([^\r\n]+))?\s*$", re.MULTILINE
)

ParsedVersion = tuple[int, int, int, tuple[str, ...]]


def parse_semver(value: str) -> Optional[ParsedVersion]:
    """Parse SemVer without build metadata; return None for non-canonical input."""
    match = SEMVER_RE.fullmatch(value)
    if match is None:
        return None
    prerelease = tuple((match.group("prerelease") or "").split("."))
    if prerelease == ("",):
        prerelease = ()
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        prerelease,
    )


def compare_semver(left: ParsedVersion, right: ParsedVersion) -> int:
    """Return positive when left is newer than right under SemVer precedence."""
    if left[:3] != right[:3]:
        return 1 if left[:3] > right[:3] else -1
    left_pre, right_pre = left[3], right[3]
    if not left_pre and not right_pre:
        return 0
    if not left_pre:
        return 1
    if not right_pre:
        return -1
    for left_id, right_id in zip(left_pre, right_pre):
        if left_id == right_id:
            continue
        left_numeric = left_id.isdigit()
        right_numeric = right_id.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_id) > int(right_id) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_id > right_id else -1
    if len(left_pre) == len(right_pre):
        return 0
    return 1 if len(left_pre) > len(right_pre) else -1


def _read_version(path: Path, errors: list[str]) -> tuple[Optional[str], Optional[ParsedVersion]]:
    try:
        raw = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"VERSION unreadable: {exc}")
        return None, None
    normalized = raw.rstrip("\r\n")
    allowed_forms = {normalized, normalized + "\n", normalized + "\r\n"}
    if (
        not normalized
        or "\n" in normalized
        or "\r" in normalized
        or raw not in allowed_forms
    ):
        errors.append("VERSION must contain exactly one non-empty line")
        return None, None
    if normalized != normalized.strip():
        errors.append("VERSION must not contain surrounding whitespace")
        return None, None
    parsed = parse_semver(normalized)
    if parsed is None:
        errors.append(
            "VERSION must be canonical SemVer without a v prefix or build metadata"
        )
        return normalized, None
    return normalized, parsed


def _validate_changelog(
    path: Path,
    version: Optional[str],
    errors: list[str],
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"CHANGELOG.md unreadable: {exc}")
        return

    headings = CHANGELOG_HEADING_RE.findall(text)
    unreleased = [index for index, item in enumerate(headings) if item[0] == "Unreleased"]
    if len(unreleased) != 1:
        errors.append("CHANGELOG.md must contain exactly one [Unreleased] heading")
    elif unreleased[0] != 0 or headings[0][1]:
        errors.append("[Unreleased] must be the first version heading and have no date")

    formal: list[tuple[str, ParsedVersion]] = []
    for label, released_on in headings:
        if label == "Unreleased":
            continue
        parsed = parse_semver(label)
        if parsed is None:
            errors.append(f"CHANGELOG.md has a non-SemVer version heading: {label}")
            continue
        try:
            parsed_date = date.fromisoformat(released_on)
        except ValueError:
            errors.append(
                f"CHANGELOG.md version {label} must have a valid YYYY-MM-DD date"
            )
        else:
            if parsed_date.isoformat() != released_on:
                errors.append(
                    f"CHANGELOG.md version {label} date is not canonical YYYY-MM-DD"
                )
        formal.append((label, parsed))

    if version is not None:
        occurrences = [item for item in formal if item[0] == version]
        if len(occurrences) != 1:
            errors.append(
                f"CHANGELOG.md must contain exactly one [{version}] release entry"
            )
        elif not formal or formal[0][0] != version:
            errors.append(
                f"CHANGELOG.md [{version}] must be the newest formal release entry"
            )

    for newer, older in zip(formal, formal[1:]):
        if compare_semver(newer[1], older[1]) <= 0:
            errors.append(
                "CHANGELOG.md formal versions must be strictly newest-to-oldest: "
                f"{newer[0]} before {older[0]}"
            )


def validate_release_contract(
    root: Path = ROOT,
    tag: Optional[str] = None,
) -> dict:
    root = Path(root).resolve()
    errors: list[str] = []
    version, parsed = _read_version(root / "VERSION", errors)
    _validate_changelog(root / "CHANGELOG.md", version, errors)

    if tag is not None:
        if not tag.startswith("v") or parse_semver(tag[1:]) is None:
            errors.append("release tag must be v followed by canonical SemVer")
        elif version is not None and tag != f"v{version}":
            errors.append(f"release tag {tag} does not match VERSION {version}")

    return {
        "ok": not errors,
        "version": version,
        "tag": tag,
        "prerelease": bool(parsed and parsed[3]),
        "version_file": str(root / "VERSION"),
        "changelog_file": str(root / "CHANGELOG.md"),
        "errors": errors,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate VERSION, CHANGELOG.md, and an optional release tag"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--tag")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = validate_release_contract(args.root, args.tag)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        state = "PASS" if result["ok"] else "FAIL"
        print(f"release version contract: {state} version={result['version']}")
        for error in result["errors"]:
            print(f"  - {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
