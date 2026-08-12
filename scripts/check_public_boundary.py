# -*- coding: utf-8 -*-
"""Fail CI when tracked public files contain private runtime material.

The scanner reports only rule, path, and line number. It never echoes the
matched value, so a failed check cannot copy private data into Actions logs.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".cfg", ".ini", ".json", ".md", ".ps1", ".py", ".sql",
    ".toml", ".txt", ".yaml", ".yml",
}
FORBIDDEN_SUFFIXES = (
    ".db", ".db-shm", ".db-wal", ".log", ".sqlite", ".sqlite3",
)
FORBIDDEN_TOP_LEVEL = {"backups", "logs", "memory", "reports", "tmp"}
FORBIDDEN_BASENAMES = {"config.md"}
CONTENT_RULES = (
    (
        "numeric_push_target",
        re.compile(
            r"(?i)(?:target|group|c2c)[^\r\n]{0,80}\b[0-9]{8,20}\b"
        ),
    ),
    (
        "concrete_qq_route",
        re.compile(
            r"(?i)\b(?:qqbot:)?(?:group|c2c):(?!PUBLIC_)[A-Za-z0-9_-]{6,}\b"
        ),
    ),
    (
        "private_ipv4",
        re.compile(
            r"(?<![0-9])(?:10(?:\.[0-9]{1,3}){3}|"
            r"192\.168(?:\.[0-9]{1,3}){2}|"
            r"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2})(?![0-9])"
        ),
    ),
    (
        "concrete_windows_user_home",
        re.compile(r"(?i)\b[A-Z]:\\Users\\(?!<|%|\{)[^\\/\s]+"),
    ),
    (
        "production_root_path",
        re.compile(r"(?i)\b[A-Z]:\\(?:OKX|AUTO-OKX-USDT-M)(?:\\|\b)"),
    ),
)


def _tracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}",
         "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("git ls-files failed; public boundary was not checked")
    return [value.decode("utf-8") for value in result.stdout.split(b"\0") if value]


def scan_files(root: Path, relative_paths: Iterable[str]) -> list[dict]:
    root = Path(root).resolve()
    findings: list[dict] = []
    for relative in relative_paths:
        normalized = str(relative).replace("\\", "/")
        path = root / normalized
        parts = Path(normalized).parts
        basename = path.name.lower()
        if (
            basename in FORBIDDEN_BASENAMES
            or any(basename.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)
            or (parts and parts[0].lower() in FORBIDDEN_TOP_LEVEL)
        ):
            findings.append({
                "rule": "forbidden_runtime_artifact",
                "path": normalized,
                "line": None,
            })
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append({
                "rule": "tracked_text_not_utf8", "path": normalized, "line": None
            })
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in CONTENT_RULES:
                if pattern.search(line):
                    findings.append({
                        "rule": rule, "path": normalized, "line": line_number
                    })
    return findings


def scan_repository(root: Path = ROOT) -> list[dict]:
    root = Path(root).resolve()
    return scan_files(root, _tracked_paths(root))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan tracked files for public-boundary violations"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        findings = scan_repository(args.root)
        error = None
    except Exception as exc:  # noqa: BLE001
        findings = []
        error = f"{type(exc).__name__}: {exc}"
    result = {"ok": not findings and error is None, "findings": findings}
    if error is not None:
        result["error"] = error
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"public boundary: {'PASS' if result['ok'] else 'FAIL'}")
        for finding in findings:
            print(
                f"  - {finding['rule']}: {finding['path']}"
                f":{finding['line'] or '-'} (value redacted)"
            )
        if error is not None:
            print(f"  - scanner_error: {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
