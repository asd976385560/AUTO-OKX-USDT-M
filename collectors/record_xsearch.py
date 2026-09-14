# -*- coding: utf-8 -*-
"""V2.0 §6 —— news-scout 每轮 ledger 记账入口（成功/降级/失败都写一行）。

scout 取数 + 经 news_writer 落库后，本轮结尾必调本脚本记一行
collection_runs(source='x_search')，让主链可观测、按需审计。

写库走 ledger.record_collection（唯一权威），禁手写 INSERT。
中文/复杂逻辑禁 python -c（GBK 坏码）——故落成 .py 入口经 wrapper 跑。
零模型名（红线 #1）。
2026-09-13（主人拍板）：cycle 按本轮**槽起点**归一而非记账时刻，见 scout_cycle_id。
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import os
import sys
from datetime import datetime, timedelta

_COLLECTORS = os.path.dirname(os.path.abspath(__file__))
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)
import ledger  # noqa: E402  复用 cycle_id_for / record_collection / SRC_XSEARCH

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_VALID_STATUS = ("ok", "degraded", "failed")

# news-scout 的 cron 槽是 10,25,40,55 * * * *（agents/news_scout.md 头部 trigger），
# 即每个刻钟（:00/:15/:30/:45）的第 10 分钟启动。
SCOUT_SLOT_OFFSET_MIN = 10


def _now_cst() -> datetime:
    return datetime.now(ledger.CST)


def scout_cycle_id(now: datetime | None = None) -> str:
    """按本轮**槽起点**归一 cycle_id，而不是按记账时刻（2026-09-13 主人拍板）。

    记账发生在整轮结尾。一轮只要跑过 5 分钟就越过下一个刻钟边界，与下一槽落到同一个
    cycle，record_collection 的 INSERT OR REPLACE 会把前一轮抹掉——2026-09-13 实证：
    10:10 槽 10:15:25 记 T23:15 degraded rows=4，10:25 槽 1.5 分钟跑完再记 T23:15 ok
    rows=1，前者消失；当日 42 轮里 5 对相撞，被抹的多是慢轮/降级轮，degraded 率被低估。
    槽起点恒在刻钟第 10 分钟，把当前时刻回拨 10 分钟再归一即落回本槽所属刻钟：
    :10+d-10 ∈ [:00,:15) ⇔ 一轮耗时 d < 15 min（超过则本就与下一槽重叠，归到下一刻钟）。
    """
    now = _now_cst() if now is None else now
    return ledger.cycle_id_for(now - timedelta(minutes=SCOUT_SLOT_OFFSET_MIN))


def main() -> int:
    ap = argparse.ArgumentParser(description="news-scout ledger record (collection_runs / x_search)")
    ap.add_argument("--status", required=True, choices=_VALID_STATUS,
                    help="ok=成功 / degraded=取到但通道慢或部分失败 / failed=整轮取不到")
    ap.add_argument("--rows", type=int, default=None, help="本轮 news_writer 落库条数")
    ap.add_argument("--latency-ms", type=int, default=None, help="本轮取数耗时(ms)，可缺")
    ap.add_argument("--err", default=None, help="失败/降级原因摘要，可缺")
    ap.add_argument("--db-root", default=os.path.join(os.path.dirname(_COLLECTORS), "db"),
                    help='db 目录，默认 <PROJECT_ROOT>\\db'.replace('<PROJECT_ROOT>', _public_project_path()))
    args = ap.parse_args()

    ledger_db = os.path.join(args.db_root, "ledger.db")
    cycle_id = scout_cycle_id()
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
