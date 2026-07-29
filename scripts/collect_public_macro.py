# -*- coding: utf-8 -*-
"""采集公开宏观数据并写 regime.db.macro_observations。

默认动作：
  1. Alternative.me 恐慌贪婪；
  2. ECB 90 日官方参考汇率，按 ICE 公开公式复算 DXY；
  3. 有 SOSOVALUE_API_KEY 时调用其官方结构化 ETF API；
  4. 导入 news.db 中 news-scout 的 Farside/SoSoValue 权威证据；
  5. 同日双源一致才生成 ETF consensus 硬数据。

不下单、不改交易账本、不推送。
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
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
sys.path.insert(0, _project_path('scripts'))

import ledger  # noqa: E402
from _http import make_client  # noqa: E402
from public_macro import (  # noqa: E402
    fetch_alternative,
    fetch_ecb_dxy,
    fetch_sosovalue,
    import_xsearch_etf,
    latest_snapshot,
    reconcile_etf_consensus,
    table_exists,
    upsert_observations,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def collect(
    db_root: Path, *, backfill: bool = False, evidence_only: bool = False
) -> tuple[dict, int]:
    regime_path = db_root / "regime.db"
    news_path = db_root / "news.db"
    if not regime_path.exists():
        return {"ok": False, "error": f"regime.db not found: {regime_path}"}, 1

    regime = ledger.connect(regime_path)
    news = None
    result = {
        "ok": True,
        "backfill": backfill,
        "evidence_only": evidence_only,
        "sources": {},
    }
    degraded = False
    try:
        if not table_exists(regime):
            return {
                "ok": False,
                "error": (
                    "macro_observations missing; run "
                    "apply_public_macro_schema.py first"
                ),
            }, 1

        if not evidence_only:
            with make_client(timeout=35.0) as client:
                for name, fetcher in (
                    (
                        "alternative_me",
                        lambda: fetch_alternative(client, backfill=backfill),
                    ),
                    ("ecb_ice_formula", lambda: fetch_ecb_dxy(client)),
                    ("sosovalue", lambda: fetch_sosovalue(client)),
                ):
                    try:
                        rows = fetcher()
                        count = upsert_observations(regime, rows)
                        if name == "sosovalue" and not rows:
                            result["sources"][name] = {
                                "status": "skipped",
                                "reason": "SOSOVALUE_API_KEY not configured",
                                "rows": 0,
                            }
                        else:
                            result["sources"][name] = {
                                "status": "ok",
                                "rows": count,
                            }
                    except Exception as exc:  # noqa: BLE001
                        degraded = True
                        result["sources"][name] = {
                            "status": "error",
                            "error": f"{type(exc).__name__}: {exc}",
                            "rows": 0,
                        }

        if news_path.exists():
            try:
                news = sqlite3.connect(
                    news_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
                )
                news.row_factory = sqlite3.Row
                count = import_xsearch_etf(news, regime)
                result["sources"]["xsearch_etf_evidence"] = {
                    "status": "ok",
                    "rows": count,
                }
            except Exception as exc:  # noqa: BLE001
                degraded = True
                result["sources"]["xsearch_etf_evidence"] = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "rows": 0,
                }
        else:
            result["sources"]["xsearch_etf_evidence"] = {
                "status": "skipped",
                "reason": "news.db not found",
                "rows": 0,
            }

        consensus = reconcile_etf_consensus(regime)
        regime.commit()
        result["etf_consensus"] = consensus
        result["latest"] = latest_snapshot(regime)
        result["degraded"] = degraded
        return result, 2 if degraded else 0
    finally:
        if news is not None:
            news.close()
        regime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="公开宏观数据采集")
    parser.add_argument("--db-root", default=_project_path('db'))
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Alternative.me 取完整历史；ECB 固定取官方近90日",
    )
    parser.add_argument(
        "--from-evidence-only",
        action="store_true",
        help="不联网，仅把 news-scout ETF 证据标准化并重新核验",
    )
    args = parser.parse_args()
    result, rc = collect(
        Path(args.db_root),
        backfill=args.backfill,
        evidence_only=args.from_evidence_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
