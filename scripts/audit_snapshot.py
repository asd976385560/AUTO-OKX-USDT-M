# -*- coding: utf-8 -*-
"""audit_events 增量快照导出（D'-16，2026-07-14 建）。

OpenClaw 2026.7.1 的 audit 账本（state/openclaw.sqlite 表 audit_events）是滚动窗
（maxEntries 10 万行 / 30 天先到先滚，实测按现网速率窗口约 4~10 天），排障与
launch-but-failed 归因（P13）依赖它——不导出即永久丢失。本脚本按 `sequence`
主键游标增量导出到 gzip JSONL，幂等可重跑，专供 cron 日频调度。

用法:
  audit_snapshot.py            # 增量导出（无新行=exit 0 空跑）
  audit_snapshot.py --status   # 只打统计（行数/速率/滚动窗估算），不导出

产物: <PROJECT_ROOT>\\reports\\audit_snapshots\\audit_<UTC+8时间戳>_seq<起>-<止>.jsonl.gz
游标: 同目录 _cursor.json（导出成功后才推进；文件先写 .tmp 再改名，断电安全）。
告警: 若表内最小 sequence > 游标+1，说明两次导出间隔超过滚动窗、有行已滚丢——
      打 [GAP] 并照常导出剩余行（exit 0，丢失量记录在案）。
只读保证: 源库一律 file:...?mode=ro 打开，本脚本绝不写 openclaw.sqlite。
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
import gzip
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

STATE_DB = str(_ProjectPath.home().joinpath('.openclaw', 'state', 'openclaw.sqlite'))
OUT_DIR = Path(_project_path('reports', 'audit_snapshots'))
CURSOR_FILE = OUT_DIR / "_cursor.json"
LOCK_FILE = OUT_DIR / "_lock"
LOCK_STALE_SEC = 3600
BATCH = 5000
ROLLING_CAP = 100_000  # OpenClaw audit maxEntries 默认
CST = timezone(timedelta(hours=8))


def _now_cst() -> datetime:
    return datetime.now(CST)


def _connect_ro() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _load_cursor() -> int:
    try:
        return int(json.loads(CURSOR_FILE.read_text(encoding="utf-8"))["last_sequence"])
    except Exception:
        return 0


def _save_cursor(seq: int) -> None:
    tmp = CURSOR_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"last_sequence": seq, "updated": _now_cst().strftime("%Y-%m-%d %H:%M:%S")},
        ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CURSOR_FILE)


def _acquire_lock() -> bool:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except OSError:
            return False
        if age > LOCK_STALE_SEC:
            try:
                LOCK_FILE.unlink()
            except OSError:
                return False
            return _acquire_lock()
        return False


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def _stats(con: sqlite3.Connection) -> dict:
    row = con.execute(
        "SELECT COUNT(*) AS n, MIN(sequence) AS lo, MAX(sequence) AS hi, "
        "MIN(occurred_at) AS t0, MAX(occurred_at) AS t1 FROM audit_events").fetchone()
    n, lo, hi, t0, t1 = row["n"], row["lo"], row["hi"], row["t0"], row["t1"]
    per_day = None
    days_to_cap = None
    if n and t1 and t0 and t1 > t0:
        span_h = (t1 - t0) / 3600_000
        if span_h >= 0.5:
            per_day = n / span_h * 24
            days_to_cap = ROLLING_CAP / per_day if per_day > 0 else None
    return {"rows": n or 0, "seq_min": lo, "seq_max": hi,
            "t0": t0, "t1": t1, "per_day": per_day, "days_to_cap": days_to_cap}


def _fmt_ms(ms) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只打统计不导出")
    args = ap.parse_args()

    con = _connect_ro()
    try:
        st = _stats(con)
        cursor = _load_cursor()
        rate = f"{st['per_day']:.0f} 行/天" if st["per_day"] else "样本不足"
        cap = f"{st['days_to_cap']:.1f} 天" if st["days_to_cap"] else "-"
        print(f"[audit_snapshot] 表内 {st['rows']} 行 seq[{st['seq_min']}..{st['seq_max']}] "
              f"span {_fmt_ms(st['t0'])} ~ {_fmt_ms(st['t1'])} | 速率≈{rate} | "
              f"滚动窗(10万行)≈{cap} | 游标={cursor}")
        if args.status:
            return 0

        if not _acquire_lock():
            print("[audit_snapshot] 另一实例在跑（或锁未过期），本次跳过", file=sys.stderr)
            return 0
        try:
            if st["seq_min"] is not None and cursor and st["seq_min"] > cursor + 1:
                lost = st["seq_min"] - cursor - 1
                print(f"[audit_snapshot][GAP] seq {cursor + 1}..{st['seq_min'] - 1} "
                      f"共 {lost} 行已滚出窗口丢失——导出频率不足，需加密", file=sys.stderr)

            if st["seq_max"] is None or st["seq_max"] <= cursor:
                print("[audit_snapshot] 无新行，空跑")
                return 0

            first_seq = last_seq = None
            n_out = 0
            ts_tag = _now_cst().strftime("%Y%m%d_%H%M%S")
            tmp_path = OUT_DIR / f".audit_{ts_tag}.tmp"
            cur = con.execute(
                "SELECT * FROM audit_events WHERE sequence > ? ORDER BY sequence",
                (cursor,))
            with gzip.open(tmp_path, "wt", encoding="utf-8") as gz:
                while True:
                    rows = cur.fetchmany(BATCH)
                    if not rows:
                        break
                    for r in rows:
                        d = dict(r)
                        if first_seq is None:
                            first_seq = d["sequence"]
                        last_seq = d["sequence"]
                        gz.write(json.dumps(d, ensure_ascii=False) + "\n")
                        n_out += 1
            final = OUT_DIR / f"audit_{ts_tag}_seq{first_seq}-{last_seq}.jsonl.gz"
            os.replace(tmp_path, final)
            _save_cursor(last_seq)
            size_kb = final.stat().st_size / 1024
            print(f"[audit_snapshot] 导出 {n_out} 行 seq[{first_seq}..{last_seq}] "
                  f"-> {final.name} ({size_kb:.0f}KB)，游标推进至 {last_seq}")
            return 0
        finally:
            _release_lock()
    finally:
        con.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[audit_snapshot] FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
