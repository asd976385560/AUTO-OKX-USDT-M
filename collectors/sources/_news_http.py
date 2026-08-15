# -*- coding: utf-8 -*-
"""News adapters' bounded alternate HTTP transport.

The primary adapters intentionally keep their existing urllib/API clients.  This
module is only used after that transport has failed, so a transient TLS-stack
problem does not silently remove an otherwise healthy official source.

``httpx`` and CPython's ``urllib`` share the OpenSSL/proxy failure domain on the
production Windows host.  During a machine-wide ``UNEXPECTED_EOF`` incident the
same official URL can still be reachable through Windows Schannel.  A single
native-curl request therefore acts as a transport fallback *inside the original
timeout budget*.  It does not change the URL, proxy configuration, source
identity, TLS verification, collection cycle, or retry count.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

import httpx


_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MIN_FALLBACK_BUDGET_SECONDS = 0.5


def _native_curl_path() -> str | None:
    """Return the OS-owned curl binary, preferring the fixed Windows path."""
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    fixed = Path(system_root) / "System32" / "curl.exe"
    if fixed.is_file():
        return str(fixed)
    # Non-production/test hosts may not have the Windows fixed path.  This is
    # only a compatibility fallback; production always resolves the OS binary.
    return shutil.which("curl.exe") or shutil.which("curl")


def _fetch_text_schannel(
    url: str,
    *,
    timeout: float,
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
) -> str:
    """Fetch one HTTPS URL with the OS TLS stack and a hard byte/time bound."""
    binary = _native_curl_path()
    if not binary:
        raise FileNotFoundError("OS curl transport is unavailable")
    timeout = max(_MIN_FALLBACK_BUDGET_SECONDS, float(timeout))
    args = [
        binary,
        "--silent",
        "--show-error",
        "--fail",
        "--location",
        "--max-redirs",
        "3",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--connect-timeout",
        f"{min(4.0, timeout):.3f}",
        "--max-time",
        f"{timeout:.3f}",
        "--max-filesize",
        str(_MAX_RESPONSE_BYTES),
        "--compressed",
    ]
    if proxy:
        args.extend(("--proxy", str(proxy)))
    for raw_name, raw_value in (headers or {}).items():
        name = str(raw_name).strip()
        value = str(raw_value).strip()
        if not name or "\r" in name or "\n" in name:
            continue
        if "\r" in value or "\n" in value:
            continue
        args.extend(("--header", f"{name}: {value}"))
    args.extend(("--url", str(url)))
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    proc = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        creationflags=creationflags,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"native Schannel transport rc={proc.returncode}: {detail[:180]}"
        )
    if len(proc.stdout) > _MAX_RESPONSE_BYTES:
        raise ValueError("native Schannel response exceeds byte limit")
    if not proc.stdout:
        raise ValueError("native Schannel response is empty")
    return proc.stdout.decode("utf-8", errors="ignore")


def fetch_text(url: str, *, timeout: float,
               headers: Mapping[str, str] | None = None) -> str:
    """Fetch UTF-8 text with one bounded, same-URL TLS-stack fallback."""
    started = time.monotonic()
    proxy = (
        os.environ.get("OKX_PROXY_URL")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    client_kwargs: dict = {
        "trust_env": False,
        "timeout": timeout,
        "follow_redirects": True,
    }
    if proxy:
        client_kwargs["proxy"] = proxy
    try:
        with httpx.Client(**client_kwargs) as client:
            response = client.get(url, headers=dict(headers or {}))
            response.raise_for_status()
            return response.content.decode("utf-8", errors="ignore")
    except httpx.HTTPStatusError:
        # The server answered authoritatively.  A second TLS stack cannot turn
        # a real HTTP status into valid source data, so fail without retrying.
        raise
    except httpx.TransportError as primary_error:
        remaining = float(timeout) - (time.monotonic() - started)
        if remaining < _MIN_FALLBACK_BUDGET_SECONDS:
            raise
        try:
            return _fetch_text_schannel(
                url,
                timeout=remaining,
                headers=headers,
                proxy=proxy,
            )
        except Exception as fallback_error:  # noqa: BLE001 - bounded evidence
            raise RuntimeError(
                "alternate TLS stacks failed: "
                f"httpx={type(primary_error).__name__}: {primary_error}; "
                f"schannel={type(fallback_error).__name__}: {fallback_error}"
            ) from fallback_error
