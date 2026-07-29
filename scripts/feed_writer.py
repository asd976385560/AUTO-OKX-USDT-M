# -*- coding: utf-8 -*-
"""Demo trader helper: feed receipt JSON file to trades_writer.py via stdin.

Usage:
    python feed_writer.py <receipt.json> --cycle-id <id> --profile <live|demo>

Reads the JSON file as bytes (preserves original encoding, avoids PS pipe surrogates),
parses to ensure validity, then writes to trades_writer.py --stdin subprocess.
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
import subprocess
import sys
from pathlib import Path

WRITER = Path(_project_path('collectors', 'trades_writer.py'))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("receipt", type=Path)
    p.add_argument("--cycle-id", required=True)
    p.add_argument("--profile", choices=["live", "demo"], required=True)
    args = p.parse_args()

    if not args.receipt.exists():
        print(json.dumps({"ok": False, "error": f"receipt not found: {args.receipt}"}))
        return 1

    # Read raw bytes to avoid any encoding munging
    raw_bytes = args.receipt.read_bytes()
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as e:
        print(json.dumps({"ok": False, "error": f"utf-8 decode failed: {e}"}))
        return 1
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"json parse failed: {e}"}))
        return 1

    # Override cycle_id from CLI (authoritative)
    data["cycle_id"] = args.cycle_id

    # Re-serialize to canonical UTF-8 bytes (no BOM, no surrogates)
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")

    # Sanity: no lone surrogates
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as e:
        print(json.dumps({"ok": False, "error": f"payload has surrogate: {e}"}))
        return 1

    proc = subprocess.run(
        [sys.executable, str(WRITER), "--stdin", "--cycle-id", args.cycle_id, "--profile", args.profile],
        input=payload,
        capture_output=True,
        timeout=60,
    )
    sys.stdout.write(proc.stdout.decode("utf-8", errors="replace"))
    sys.stderr.write(proc.stderr.decode("utf-8", errors="replace"))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())