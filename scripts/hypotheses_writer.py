# -*- coding: utf-8 -*-
"""Hypotheses Writer - P4 假设硬化写入器（2026-06-11 体检 4.3）

背景：v7.x 起 P4 决策后"必写至少 1 条 hypotheses"，但一直靠 tmp/ 一次性
.py 脚本（hypo2185.py、hypo2201.py…）ad-hoc 写入——还出现过写错库
（lessons.db 旁路表，P7 update_playbook_stats 读不到，已于 2026-06-11
迁移回 account.db 并把旁路表改名 hypotheses_migrated_20260611）。
本脚本是公开版本唯一合法的 hypotheses 写入口。

权威表：**account.db.hypotheses**（不是 lessons.db！）
schema: id, cycle_id TEXT, ts TEXT, hypothesis_id TEXT, hypothesis TEXT,
        falsifiable_condition TEXT, confidence TEXT, rationale TEXT,
        status TEXT DEFAULT 'open'

调用（Agent P4 决策后）：
  echo '<json>' | run_okx_python.ps1 scripts/hypotheses_writer.py --stdin

输入 JSON 字段：
  必填：cycle_id, hypothesis, falsifiable_condition, confidence
  可选：hypothesis_id（默认 H{cycle_id}-{HHMMSS}）, rationale（建议含 playbook #N 引用，
        P7 update_playbook_stats 靠 regex 从 hypothesis/rationale 抽取关联）,
        status（默认 'open'）, ts（默认 now UTC+8）

退出码：0=成功且 read-after-write 校验通过；非 0=失败（Agent 视为 P2，重试一次）。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def now_cst() -> datetime:
    return datetime.now(CST)


def fail(msg: str, code: int = 2):
    print(f"[hypotheses_writer][FAIL] {msg}", file=sys.stderr)
    sys.exit(code)


def load_payload(args) -> dict:
    if args.stdin:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace") if hasattr(sys.stdin, "buffer") else sys.stdin.read()
    elif args.json_file:
        with open(args.json_file, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    elif args.json:
        raw = args.json
    else:
        fail("缺少输入：需 --stdin / --json-file / --json 之一")
    try:
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        fail(f"输入 JSON 解析失败: {e}；含中文时建议先写 <PROJECT_ROOT>\\tmp\\*.json 再 --json-file")


def main():
    p = argparse.ArgumentParser(description="P4 hypotheses 硬化写入（account.db.hypotheses 唯一入口）")
    p.add_argument("--db-root", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db"))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stdin", action="store_true")
    g.add_argument("--json-file")
    g.add_argument("--json")
    args = p.parse_args()

    data = load_payload(args)

    db_path = os.path.join(args.db_root, "account.db")
    if not os.path.exists(db_path):
        fail(f"account.db 不存在: {db_path}")

    cycle_id = data.get("cycle_id")
    hypothesis = (data.get("hypothesis") or "").strip()
    falsifiable = (data.get("falsifiable_condition") or data.get("falsifiable") or "").strip()
    confidence = data.get("confidence")
    if cycle_id in (None, ""):
        fail("cycle_id 必填（= 本轮 cycle_runs.cycle_count）")
    if not hypothesis:
        fail("hypothesis 必填（本轮决策假设正文）")
    if not falsifiable:
        fail("falsifiable_condition 必填（可证伪条件——没有它假设无法复盘）")
    if confidence is None:
        fail("confidence 必填（0~1）")

    now = now_cst()
    ts = data.get("ts") or now.strftime(TS_FMT)
    hypothesis_id = data.get("hypothesis_id") or f"H{cycle_id}-{now.strftime('%H%M%S')}"
    rationale = data.get("rationale") or ""
    status = data.get("status") or "open"
    if "playbook" not in (hypothesis + rationale).lower():
        print("[hypotheses_writer][WARN] hypothesis/rationale 未引用 playbook #N——"
              "P7 update_playbook_stats 将无法关联本条（建议补引用）", file=sys.stderr)

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO hypotheses (cycle_id, ts, hypothesis_id, hypothesis, "
            "falsifiable_condition, confidence, rationale, status) VALUES (?,?,?,?,?,?,?,?)",
            (str(cycle_id), ts, hypothesis_id, hypothesis, falsifiable,
             str(confidence), rationale, status),
        )
        conn.commit()
        rowid = cur.lastrowid
        v = conn.execute(
            "SELECT id, cycle_id, hypothesis_id, status FROM hypotheses WHERE id=?",
            (rowid,),
        ).fetchone()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        fail(f"写入异常: {e}")
    finally:
        conn.close()

    if not v:
        fail("写后校验失败：回读不到刚写入的行", code=4)

    print(json.dumps({
        "ok": True,
        "id": v[0],
        "cycle_id": v[1],
        "hypothesis_id": v[2],
        "status": v[3],
        "table": "account.db.hypotheses",
    }, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
