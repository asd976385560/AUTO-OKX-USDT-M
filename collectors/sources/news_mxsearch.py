# -*- coding: utf-8 -*-
"""妙想加密新闻 adapter：registry → news_writer。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_COLLECTORS = str(Path(__file__).resolve().parents[1])
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)

import news_writer  # noqa: E402
from _mx_news_common import (  # noqa: E402
    MX_QUERY,
    MXQuotaExceeded,
    api_key,
    normalize,
    search,
)


def fetch_items(errors: list[str] | None = None,
                retry_stats: dict | None = None) -> list[dict]:
    key = api_key()
    if not key:
        if errors is not None:
            errors.append("MX_APIKEY missing")
        return []
    rows: list[dict] = []
    last_error: Exception | None = None
    succeeded = False
    attempts = 0
    quota_exhausted = False
    for attempt, timeout_sec in ((1, 6.0), (2, 4.0)):
        attempts = attempt
        if attempt == 2:
            time.sleep(0.5)
        try:
            rows = search(MX_QUERY, key=key, timeout_sec=timeout_sec)
            succeeded = True
            if retry_stats is not None:
                retry_stats.update({
                    "attempts": attempt,
                    "recovered_after_retry": attempt == 2,
                })
            break
        except MXQuotaExceeded as exc:
            last_error = exc
            quota_exhausted = True
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if not succeeded:
        if errors is not None:
            errors.append(f"{type(last_error).__name__}: {last_error}"[:150])
        if retry_stats is not None:
            retry_stats.update({
                "attempts": attempts,
                "final_failed": True,
                "quota_exhausted": quota_exhausted,
                "retry_skipped_non_retryable": quota_exhausted,
            })
        return []
    items = []
    for row in rows:
        item = normalize(
            row,
            source="mx-search",
            fingerprint_prefix="mx",
            tags=["mx_search", "crypto"],
        )
        if item:
            items.append(item)
    return items


def collect(db_path: str, apply: bool = False) -> dict:
    errors: list[str] = []
    retry_stats: dict = {}
    items = fetch_items(errors, retry_stats)
    err_txt = "; ".join(errors)[:150] if errors else None
    if not apply:
        out = {
            "ok": not (err_txt and not items),
            "dry_run": True,
            "fetched": len(items),
            "retry_stats": retry_stats,
            "sample": [{"t": i["title"][:70], "et": i["event_time"]}
                       for i in items[:8]],
        }
        if err_txt:
            out["err"] = err_txt
        return out
    if err_txt and not items:
        return {"ok": False, "fetched": 0, "inserted": 0, "err": err_txt}
    result = news_writer.write_news(items, db_path)
    result["fetched"] = len(items)
    result["retry_stats"] = retry_stats
    if err_txt:
        result["err"] = err_txt
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="妙想加密新闻 → news_writer")
    parser.add_argument("--db", default=str(news_writer.DEFAULT_NEWS_DB))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = collect(args.db, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
