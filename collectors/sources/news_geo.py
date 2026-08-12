# -*- coding: utf-8 -*-
"""妙想地缘新闻 adapter：registry → news_writer。"""
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
from _mx_news_common import GEO_QUERIES, api_key, normalize, search  # noqa: E402


def fetch_items(errors: list[str] | None = None,
                retry_stats: dict | None = None) -> list[dict]:
    key = api_key()
    if not key:
        if errors is not None:
            errors.append("MX_APIKEY missing")
        return []
    rows: list[dict] = []
    recovered = 0
    final_failed = 0
    for index, query in enumerate(GEO_QUERIES):
        last_error: Exception | None = None
        for attempt, timeout_sec in ((1, 6.0), (2, 4.0)):
            if attempt == 2:
                time.sleep(0.5)
            try:
                rows.extend(search(query, key=key, timeout_sec=timeout_sec))
                if attempt == 2:
                    recovered += 1
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        else:
            final_failed += 1
            if errors is not None:
                errors.append(
                    f"{query}: {type(last_error).__name__}: {last_error}"[:150])
        if index + 1 < len(GEO_QUERIES):
            time.sleep(0.5)

    if retry_stats is not None:
        retry_stats.update({
            "queries": len(GEO_QUERIES),
            "recovered_after_retry": recovered,
            "final_failed": final_failed,
        })

    items = []
    seen_codes: set[str] = set()
    for row in rows:
        code = str(row.get("code") or "").strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        item = normalize(
            row,
            source="geo-political",
            fingerprint_prefix="geo",
            tags=["geopolitical", "macro"],
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
    parser = argparse.ArgumentParser(description="妙想地缘新闻 → news_writer")
    parser.add_argument("--db", default=str(news_writer.DEFAULT_NEWS_DB))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = collect(args.db, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
