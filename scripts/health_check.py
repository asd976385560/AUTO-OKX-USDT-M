# -*- coding: utf-8 -*-
"""
health_check.py - Pre-flight connectivity check for OKX v2.0

Validates that all external APIs are reachable before starting data collection.
Run this once before the first Job A / Job E cycle.

Usage:
    python scripts/health_check.py
"""

from __future__ import annotations

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        if _s.encoding and _s.encoding.lower() != "utf-8":
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import json
import urllib.request
import urllib.error
from pathlib import Path

OK = "\u2705"
FAIL = "\u274c"

# Read config to find project root


def check_url(url: str, timeout: int = 10) -> tuple[bool, str]:
    """Check if a URL is reachable. Returns (ok, detail)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OKX-v2-HealthCheck/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        status = resp.getcode()
        return True, f"HTTP {status}"
    except urllib.error.HTTPError as e:
        # 401/403 = API reachable but needs auth = OK
        if e.code in (401, 403):
            return True, f"HTTP {e.code} (needs auth)"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)[:80]


def main() -> None:
    print("[OKX v2.0] Health Check\n")

    results: list[tuple[str, bool, str]] = []

    # 1. OKX API
    ok, detail = check_url("https://www.okx.com/api/v5/public/time")
    results.append(("OKX API", ok, detail))

    # 2. FRED
    ok, detail = check_url("https://api.stlouisfed.org/fred/series?series_id=VIXCLS&api_key=placeholder&file_type=json")
    results.append(("FRED API", ok, detail))

    # 3. DefiLlama
    ok, detail = check_url("https://api.llama.fi/v2/chains")
    results.append(("DefiLlama TVL", ok, detail))

    # 4. CoinGecko
    ok, detail = check_url("https://api.coingecko.com/api/v3/ping")
    results.append(("CoinGecko API", ok, detail))

    # 5. Python + SQLite
    import sqlite3
    conn = sqlite3.connect(":memory:")
    ver = sqlite3.sqlite_version
    conn.close()
    results.append(("Python + SQLite", True, f"SQLite {ver}"))

    # 6. OKX CLI
    import shutil, subprocess
    okx_cmd = shutil.which("okx")
    try:
        if okx_cmd:
            r = subprocess.run([okx_cmd, "--version"], capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            r = subprocess.run(["pwsh", "-Command", "okx --version"], capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ver = (r.stdout or r.stderr or "unknown").strip()[:40]
        results.append(("OKX CLI", r.returncode == 0, ver))
    except FileNotFoundError:
        results.append(("OKX CLI", False, "not installed (npm i -g @okx_ai/okx-trade-cli)"))
    except Exception as e:
        results.append(("OKX CLI", False, str(e)[:60]))

    # Print results
    all_ok = True
    for name, ok, detail in results:
        icon = OK if ok else FAIL
        print(f"  {icon} {name}: {detail}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("[PASS] All checks passed. Ready to start data collection.")
    else:
        print("[FAIL] Some checks failed. Fix above issues before starting.")
        sys.exit(1)


if __name__ == "__main__":
    main()
