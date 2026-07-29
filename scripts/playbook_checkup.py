# -*- coding: utf-8 -*-
"""playbook 周度体检（T5，2026-06-12）——P7 weekly 步骤 ③。

规则:
  1. 战绩淘汰: n(=win_count+loss_count)≥10 且 win_rate<0.30 且未弃用 → category 加 'deprecated:' 前缀
     （条目保留可查，统计列不清零；低胜率条目仍可作"反向/时机不可靠"参考，由简报标注）
  2. 实验候选: 未验证条目(n<5、未弃用)随机抽 3 条打印——由 P7 weekly 的 agent 经
     hypotheses_writer.py 登记为 demo 实验假设（本脚本不直写 hypotheses，保持唯一入口纪律）
  3. 周度统计快照打印（有战绩/胜者/弃用计数）

用法:
  pwsh ... run_okx_python.ps1 scripts/playbook_checkup.py [--db-root <PROJECT_ROOT>\\db] [--apply]
默认 dry-run；退出码 0=成功（含 dry-run），1=异常。
"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

DEPRECATE_N = 10
DEPRECATE_WR = 0.30


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--apply", action="store_true", help="执行降级写库（默认 dry-run）")
    ap.add_argument("--candidates", type=int, default=3)
    args = ap.parse_args()

    con = sqlite3.connect(f"{args.db_root}\\account.db", timeout=15)
    con.row_factory = sqlite3.Row

    print(f"== playbook 周度体检 ({'APPLY' if args.apply else 'DRY-RUN'}) ==")

    total, depr = con.execute(
        "SELECT COUNT(*), SUM(CASE WHEN category LIKE 'deprecated%' THEN 1 ELSE 0 END) FROM playbook"
    ).fetchone()
    proven = con.execute(
        f"SELECT COUNT(*) FROM playbook WHERE win_count+loss_count >= 5"
    ).fetchone()[0]
    print(f"总条目 {total} | 有战绩(n≥5) {proven} | 已弃用 {depr}")

    # 1) 战绩淘汰
    losers = con.execute(
        "SELECT id, summary, win_count+loss_count AS n, win_rate, avg_pnl_pct FROM playbook "
        "WHERE win_count+loss_count >= ? AND win_rate < ? AND category NOT LIKE 'deprecated%' "
        "ORDER BY id",
        (DEPRECATE_N, DEPRECATE_WR),
    ).fetchall()
    if losers:
        print(f"\n[淘汰] n≥{DEPRECATE_N} 且 wr<{DEPRECATE_WR:.0%} 共 {len(losers)} 条:")
        for r in losers:
            print(f"  #{r['id']} n={r['n']} wr={r['win_rate']:.0%} avg={r['avg_pnl_pct']:+.1f}% | {r['summary'][:50]}")
        if args.apply:
            con.executemany(
                "UPDATE playbook SET category='deprecated:'||category, "
                "summary=summary||' [auto-deprecated 低胜率 '||date('now')||']' WHERE id=?",
                [(r["id"],) for r in losers],
            )
            con.commit()
            print(f"  → 已写库（category 前缀 deprecated:，summary 加标注）")
    else:
        print(f"\n[淘汰] 无符合条件条目（n≥{DEPRECATE_N} & wr<{DEPRECATE_WR:.0%}）")

    # 2) 胜者看板
    winners = con.execute(
        "SELECT id, summary, win_count+loss_count AS n, win_rate, avg_pnl_pct FROM playbook "
        "WHERE win_count+loss_count >= 8 AND win_rate >= 0.5 AND category NOT LIKE 'deprecated%' "
        "ORDER BY win_rate DESC LIMIT 5"
    ).fetchall()
    print(f"\n[胜者] n≥8 且 wr≥50%: {len(winners)} 条")
    for r in winners:
        print(f"  #{r['id']} n={r['n']} wr={r['win_rate']:.0%} avg={r['avg_pnl_pct']:+.1f}% | {r['summary'][:50]}")

    # 3) 实验候选（打印，由 agent 经 hypotheses_writer 登记）
    cands = con.execute(
        "SELECT id, summary, category FROM playbook "
        "WHERE win_count+loss_count < 5 AND category NOT LIKE 'deprecated%' "
        "ORDER BY RANDOM() LIMIT ?",
        (args.candidates,),
    ).fetchall()
    print(f"\n[实验候选] 本周抽样 {len(cands)} 条未验证条目 → 请经 hypotheses_writer.py 登记 demo 实验:")
    for r in cands:
        print(f"  #{r['id']} [{r['category']}] {r['summary'][:60]}")
        print(f"    建议假设格式: hypothesis_id=PB-EXP-{r['id']}, falsifiable=demo 实测 n≥5 后按 wr/avg 判存废")

    con.close()
    print("\nOK playbook 体检完成")


if __name__ == "__main__":
    main()
