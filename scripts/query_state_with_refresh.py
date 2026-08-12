# -*- coding: utf-8 -*-
"""Run query_state with an optional account refresh first.

This is a small operational wrapper for long full-test rounds. It avoids false
P0 failures where query_state is executed 15+ minutes after the initial account
snapshot collection.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(r".")
PWSH = "C:/Program Files/PowerShell/7/pwsh.exe"
RUN_OKX = ROOT / "scripts" / "run_okx_python.ps1"


def run_okx(script: str, *args: str, timeout: int = 600) -> int:
    cp = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(RUN_OKX), str(ROOT / script), *args],
        cwd=str(ROOT),
        text=True,
        timeout=timeout,
    )
    return cp.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="query_state wrapper with optional account refresh")
    ap.add_argument("--db-root", default=str(ROOT / "db"))
    ap.add_argument("--check", default="all")
    ap.add_argument("--refresh-account-before-check", action="store_true")
    ap.add_argument("--refresh-timeout", type=int, default=300)
    ns = ap.parse_args(argv)

    if ns.refresh_account_before_check:
        rc = run_okx("collectors/fast_collect.py", "--no-dispatch", "--db-root", ns.db_root, timeout=ns.refresh_timeout)
        if rc != 0:
            return rc
    return run_okx("scripts/query_state.py", "--check", ns.check, "--db-root", ns.db_root, timeout=180)


if __name__ == "__main__":
    raise SystemExit(main())
