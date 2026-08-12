# -*- coding: utf-8 -*-
"""News adapters' bounded alternate HTTP transport.

The primary adapters intentionally keep their existing urllib/API clients.  This
module is only used after that transport has failed, so a transient TLS-stack
problem does not silently remove an otherwise healthy official source.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

import httpx


def fetch_text(url: str, *, timeout: float,
               headers: Mapping[str, str] | None = None) -> str:
    """Fetch UTF-8 text through the runtime's explicit proxy, without env drift."""
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
    with httpx.Client(**client_kwargs) as client:
        response = client.get(url, headers=dict(headers or {}))
        response.raise_for_status()
        return response.content.decode("utf-8", errors="ignore")
