# -*- coding: utf-8 -*-
"""v7.0e.3 P7 复盘时调用：更新 playbook 的 evidence_count / win_count / loss_count

v7.0e.5 修复（2026-06-08 sub-E P7）：
  - hypotheses 表无 summary 列 → 改用 rationale 文本
  - hypotheses 表无 playbook_ref 列 → 用 regex 从 rationale/hypothesis 抽 "playbook #N"
  - trade_events 表无 cycle_id 列 → 从 raw JSON 解析 cycle_id
  - 兼容 v7.0d 起的 raw.invalidated=1 幽灵数据（统计时跳过）
  - 兼容 text_factory 缺省 str（解决 GBK 字节问题）
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse
import sys, sqlite3, json, re
sys.stdout.reconfigure(encoding='utf-8')

DB = _project_path('db', 'account.db')
PLAYBOOK_REF_RE = re.compile(r"playbook\s*#\s*(\d+)", re.IGNORECASE)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="v7.0e.3 P7: 更新 playbook 的 evidence_count / win_count / loss_count"
    )
    ap.add_argument("--db", default=DB, help=f"account.db 路径 (default: {DB})")
    ap.add_argument("--apply", action="store_true",
                    help="实际写库；缺省为 dry-run，仅展示将要更新的统计")
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=30)
    # v7.1.5（2026-06-11）：单行编码损坏（GBK 字节混入）不再拖垮整表查询——
    # 历史上多个 session 写入过 GBK 字节行（已修复 11 行），导致 hypotheses
    # 全表查询炸、引用数归 0。replace 容错保证统计永远能跑。
    con.text_factory = lambda b: b.decode("utf-8", errors="replace")
    cur = con.cursor()

    # 1) 所有 playbook
    plays = cur.execute(
        "SELECT id, summary FROM playbook"
    ).fetchall()
    if not plays:
        print("playbook 空，无需更新")
        con.close()
        return 0
    print(f"playbook rows: {len(plays)}")

    # 2) hypotheses 引用 → 找 playbook 关联
    #    hypotheses.cycle_id 是 TEXT 字符串（来自 cycle_runs.cycle_count）
    #    rationale / hypothesis 文本里包含 "playbook #N" 关键字
    refs: list[tuple[str, int]] = []  # (cycle_id_str, playbook_id)
    try:
        hyp_rows = cur.execute(
            "SELECT cycle_id, hypothesis, rationale FROM hypotheses"
        ).fetchall()
    except Exception as e:
        print(f"hypotheses 查询失败: {e}")
        hyp_rows = []
    for cycle_id, hyp_text, rat in hyp_rows:
        if not cycle_id:
            continue
        text = (hyp_text or "") + "  " + (rat or "")
        for m in PLAYBOOK_REF_RE.finditer(text):
            try:
                pid = int(m.group(1))
            except ValueError:
                continue
            refs.append((cycle_id, pid))
    print(f"hypotheses→playbook 引用数: {len(refs)}")

    # 3) trade_events：raw JSON 含 cycle_id
    events_by_cycle: dict[str, list[float]] = {}  # cycle_id_str → [pnl, ...]
    try:
        ev_rows = cur.execute(
            "SELECT raw, pnl, profile FROM trade_events"
        ).fetchall()
    except Exception as e:
        print(f"trade_events 查询失败: {e}")
        ev_rows = []
    ghost_skipped = 0
    for raw, pnl, profile in ev_rows:
        if not raw:
            continue
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("invalidated"):
            ghost_skipped += 1
            continue
        if pnl is None:
            continue
        cid = j.get("cycle_id")
        if cid is None:
            continue
        events_by_cycle.setdefault(str(cid), []).append(float(pnl))
    live_n = sum(len(v) for v in events_by_cycle.values())
    print(f"trade_events pnl rows aggregated: {live_n}  ghost_skipped={ghost_skipped}")

    # 3b) drill_trades（demo 已平仓）——v7.1.5（2026-06-11 全流程验证 S6）
    # 旧版只统计 trade_events（live），demo 验证 pnl 从不反哺 playbook（统计全 0），
    # "demo 验证→live 升级"闭环实际断裂。demo 按 cycle_id 列归因（2026-06-11 迁移加列，
    # phase5_writer v7.1.5 起写入；历史行 NULL 时 regex 从 reason 文本兜底抽 "cycle N"）。
    cycle_text_re = re.compile(r"cycle[\s#]*(\d{3,5})", re.IGNORECASE)
    demo_n = 0
    try:
        drill_con = sqlite3.connect(_project_path('db', 'drill.db'), timeout=30)
        dr_rows = drill_con.execute(
            "SELECT cycle_id, entry_reason, close_reason, pnl FROM drill_trades "
            "WHERE status='closed' AND pnl IS NOT NULL AND profile='demo'"
        ).fetchall()
        drill_con.close()
        for cid, er, cr, pnl in dr_rows:
            if cid is None:
                m = cycle_text_re.search((er or "") + " " + (cr or ""))
                if not m:
                    continue
                cid = int(m.group(1))
            events_by_cycle.setdefault(str(cid), []).append(float(pnl))
            demo_n += 1
    except Exception as e:
        print(f"drill_trades 查询失败（demo pnl 跳过）: {e}")
    print(f"drill_trades demo pnl rows aggregated: {demo_n}")

    # 4) 按 playbook 聚合
    by_play: dict[int, dict] = {p[0]: {"evidence": 0, "win": 0, "loss": 0, "pnls": []} for p in plays}
    for cycle_id, pid in refs:
        if pid not in by_play:
            continue
        by_play[pid]["evidence"] += 1
        for pnl in events_by_cycle.get(cycle_id, []):
            by_play[pid]["pnls"].append(pnl)
            if pnl > 0:
                by_play[pid]["win"] += 1
            elif pnl < 0:
                by_play[pid]["loss"] += 1

    # 5) UPDATE
    latest_cycle = cur.execute("SELECT MAX(cycle_count) FROM cycle_runs").fetchone()[0] or 0
    updated = 0
    planned: list[dict] = []
    for pid, stats in by_play.items():
        ev = stats["evidence"]
        if ev == 0:
            continue
        win = stats["win"]
        loss = stats["loss"]
        wr = win / ev if ev else None
        avg_pnl = sum(stats["pnls"]) / len(stats["pnls"]) if stats["pnls"] else None
        planned.append({
            "id": pid, "evidence": ev, "win": win, "loss": loss,
            "win_rate": wr, "avg_pnl_pct": avg_pnl, "last_validated_cycle": latest_cycle,
        })
        if args.apply:
            cur.execute(
                "UPDATE playbook SET evidence_count=?, win_count=?, loss_count=?, "
                "win_rate=?, avg_pnl_pct=?, last_validated_cycle=? WHERE id=?",
                (ev, win, loss, wr, avg_pnl, latest_cycle, pid),
            )
        updated += 1
    if args.apply:
        con.commit()
        print(f"[APPLY] 更新 {updated} 条 playbook 统计")
    else:
        print(f"[DRY-RUN] 将更新 {updated} 条 playbook 统计（加 --apply 才真写）")

    # 6) 输出 top
    rows = cur.execute(
        "SELECT id, summary, evidence_count, win_count, loss_count, win_rate, avg_pnl_pct "
        "FROM playbook WHERE evidence_count > 0 ORDER BY evidence_count DESC LIMIT 20"
    ).fetchall()
    print("\n--- 已验证 playbook top 20 ---")
    print(f"{'id':<6}{'引用':<8}{'胜':<6}{'负':<6}{'胜率':<8}{'均pnl%':<10}{'summary':<40}")
    for r in rows:
        sm = (r[1] or "")
        if isinstance(sm, bytes):
            sm = sm.decode("utf-8", errors="replace")
        print(f"{r[0]:<6}{r[2]:<8}{r[3]:<6}{r[4]:<6}{(r[5] or 0):<8.2f}{(r[6] or 0):<10.4f}{sm[:40]:<40}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
