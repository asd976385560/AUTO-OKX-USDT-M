# -*- coding: utf-8 -*-
"""采集公开宏观数据并写 regime.db.macro_observations。

默认动作：
  1. Alternative.me 恐慌贪婪；
  2. ECB 90 日官方参考汇率，按 ICE 公开公式复算 DXY；
  3. 有 SOSOVALUE_API_KEY 时调用其官方结构化 ETF API；
  4. 导入 news.db 中 news-scout 的 Farside/SoSoValue 权威证据；
  5. 同日双源一致才生成 ETF consensus 硬数据。

每轮结果除 stdout 外**原子落一份收据**（默认 logs/monitor/public_macro_last_run.json，
`--receipt-file` 可改，`--no-receipt` 可关）：本脚本被 daily_maintenance 以子进程起，
那边只留 stdout 末 3 行（对本脚本=JSON 的收尾花括号），事后无从复盘某源为何 0 行。
收据只含结构与状态，不含任何 key。

不下单、不改交易账本、不推送。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
DEFAULT_RECEIPT = Path(_public_project_path('logs', 'monitor', 'public_macro_last_run.json'))

sys.path.insert(0, _public_project_path('collectors'))
sys.path.insert(0, _public_project_path('scripts'))

import ledger  # noqa: E402
from _http import load_sosovalue_key, make_client  # noqa: E402
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


def write_receipt(path: Path, payload: dict) -> bool:
    """原子落收据；任何失败只警告不改 rc（收据是诊断件，不得反噬采集）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(
                    descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False,
                          indent=2, default=str)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] receipt write failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return False


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
            # 2026-08-19：0 行的成因必须自证。原文案一律写「key not configured」，
            # 在「key 已配但响应结构/套餐权限不符」时会把人指向错误方向（实测踩过）。
            soso_probe: dict = {}
            with make_client(timeout=35.0) as client:
                for name, fetcher in (
                    (
                        "alternative_me",
                        lambda: fetch_alternative(client, backfill=backfill),
                    ),
                    ("ecb_ice_formula", lambda: fetch_ecb_dxy(client)),
                    (
                        "sosovalue",
                        lambda: fetch_sosovalue(client, diagnostic=soso_probe),
                    ),
                ):
                    try:
                        rows = fetcher()
                        count = upsert_observations(regime, rows)
                        if name == "sosovalue" and not rows:
                            has_key = bool(load_sosovalue_key())
                            result["sources"][name] = {
                                "status": "skipped",
                                "reason": (
                                    "key 已配置（env 或 config.md §4.6b）但本轮解析出 0 行："
                                    "响应结构或套餐权限与 parse_sosovalue_payload 口径不符，"
                                    "见 payload_probe"
                                    if has_key else
                                    "SOSOVALUE_API_KEY 未配置，且 config.md §4.6b 无可用 key"
                                ),
                                "key_configured": has_key,
                                "payload_probe": soso_probe or None,
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
    parser.add_argument("--db-root", default=_public_project_path('db'))
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
    parser.add_argument(
        "--receipt-file",
        default=str(DEFAULT_RECEIPT),
        help=f"每轮结果收据落盘路径（默认 {DEFAULT_RECEIPT}）",
    )
    parser.add_argument(
        "--no-receipt", action="store_true", help="不落收据（只打 stdout）")
    args = parser.parse_args()
    result, rc = collect(
        Path(args.db_root),
        backfill=args.backfill,
        evidence_only=args.from_evidence_only,
    )
    if not args.no_receipt:
        write_receipt(Path(args.receipt_file), {
            "schema": "public_macro_run_receipt_v1",
            "ts_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "rc": rc,
            **result,
        })
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
