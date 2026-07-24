# -*- coding: utf-8 -*-
"""V2.0 §6 —— news-scout 每轮 ledger 记账入口（成功/降级/失败都写一行）。

scout 取数 + 经 news_writer 落库后，本轮结尾必调本脚本记一行
collection_runs(source='x_search')，让主链可观测、按需审计。

写库走 ledger.record_collection（唯一权威），禁手写 INSERT。
中文/复杂逻辑禁 python -c（GBK 坏码）——故落成 .py 入口经 wrapper 跑。
零模型名（红线 #1）。
"""
from __future__ import annotations

import argparse
import os
import sys

_COLLECTORS = os.path.dirname(os.path.abspath(__file__))
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)
import ledger  # noqa: E402  复用 cycle_id_for / record_collection / SRC_XSEARCH

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_VALID_STATUS = ("ok", "degraded", "failed")


def main() -> int:
    ap = argparse.ArgumentParser(description="news-scout ledger record (collection_runs / x_search)")
    ap.add_argument("--status", required=True, choices=_VALID_STATUS,
                    help="ok=成功 / degraded=取到但通道慢或部分失败 / failed=整轮取不到")
    ap.add_argument("--rows", type=int, default=None, help="本轮 news_writer 落库条数")
    ap.add_argument("--latency-ms", type=int, default=None, help="本轮取数耗时(ms)，可缺")
    ap.add_argument("--err", default=None, help="失败/降级原因摘要，可缺")
    ap.add_argument("--db-root", default=os.path.join(os.path.dirname(_COLLECTORS), "db"),
                    help="db 目录，默认 <PROJECT_ROOT>\\db")
    args = ap.parse_args()

    ledger_db = os.path.join(args.db_root, "ledger.db")
    cycle_id = ledger.cycle_id_for()
    ledger.record_collection(
        ledger_db,
        cycle_id,
        ledger.SRC_XSEARCH,
        args.status,
        rows=args.rows,
        latency_ms=args.latency_ms,
        err=args.err,
    )
    print(f"recorded cycle_id={cycle_id} source={ledger.SRC_XSEARCH} status={args.status} rows={args.rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
