# -*- coding: utf-8 -*-
"""Production wrapper for bounded market-feature transport recovery.

The underlying collector remains the byte-identical v7 frozen dependency.
Only the production entrypoint substitutes current-cycle order-book/trade
fetchers and appends their transport attestation to the collector JSON.
"""
from __future__ import annotations

import contextlib
import io
import json

import _okx_market_feature_recovery as recovery
import collect_market_features as frozen


def main() -> int:
    frozen.fetch_orderbooks_batch_sync = recovery.fetch_orderbooks_batch_sync
    frozen.fetch_recent_trades_batch_sync = recovery.fetch_recent_trades_batch_sync
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        returncode = frozen.main()
    lines = captured.getvalue().splitlines()
    if not lines:
        return returncode
    try:
        payload = json.loads(lines[-1])
    except (TypeError, ValueError):
        print(captured.getvalue(), end="")
        return returncode
    if isinstance(payload, dict):
        payload["market_feature_transport"] = recovery.transport_snapshot()
    for line in lines[:-1]:
        print(line)
    print(json.dumps(payload, ensure_ascii=False))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
