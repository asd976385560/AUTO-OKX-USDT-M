# -*- coding: utf-8 -*-
"""QQ push idempotency wrapper.

Prevents duplicate sends when an agent retries after sending or two push attempts
race. The original implementation lives in `qq_push_raw.py` and is executed
unchanged after this wrapper wins the dedupe lock.

Dedupe layers:
  1. 显式身份键：调用方传 --dedupe-key（如 push:{cycle_id} / monitor:{stamp} /
     reviewer:{date}:{用途}），同 target+key 只发一次——身份由调用方声明。
  2. 无显式键：纯 content-hash（同 target 完全相同内容只发一次）。

调用方不得从正文猜测 cycle 或轮次；业务身份必须通过 --dedupe-key 显式声明。
sent 表中其他格式的历史行只读保留，不参与当前键匹配。

⚠️ --dedupe-key 是本 wrapper 专属参数：qq_push_raw 是严格 argparse，runpy 前必须
_strip_wrapper_args 剥掉，否则 raw SystemExit(2) → 所有带键推送全灭。

Structured events are appended to <PROJECT_ROOT>/logs/push/qq_push_dedupe.jsonl.
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import hashlib
import json
import os
import runpy
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(_project_path())
RAW = ROOT / "scripts" / "qq_push_raw.py"
DB = ROOT / "db" / "qq_push_dedupe.db"
EVENT_LOG = ROOT / "logs" / "push" / "qq_push_dedupe.jsonl"
CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _append_event(**event) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": _now(), **event}
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=f)


def _arg_value(names: tuple[str, ...]) -> str | None:
    for i, arg in enumerate(sys.argv):
        if arg in names and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        for name in names:
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def _read_content_once() -> str:
    fp = _arg_value(("--content-file", "--file"))
    if fp:
        return Path(fp).read_text(encoding="utf-8", errors="replace")
    # --message 必须参与 dedupe 内容读取，禁止以空内容 hash 代替。
    msg = _arg_value(("--message", "--content", "--text"))
    if msg is not None:
        return msg
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        # 文件名带 pid+毫秒，防并发 stdin 推送互相覆盖内容文件。
        tmp = ROOT / "tmp" / f"_qq_push_stdin_{os.getpid()}_{int(time.time() * 1000)}.txt"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(data, encoding="utf-8")
        sys.argv.extend(["--content-file", str(tmp)])
        return data
    return ""


def _dedupe_key(content: str) -> tuple[str, str, str | None, str]:
    """(key, content_hash, dkey, target)——身份优先显式 --dedupe-key，无则 content-hash。"""
    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    dkey = _arg_value(("--dedupe-key",))
    target = _arg_value(("--target", "--group", "--to", "--group-openid")) or "default"
    basis = f"{target}|{dkey or content_hash}"
    key = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return key, content_hash, dkey, target


def _claim(key: str, content_hash: str, preview: str, dkey: str | None, target: str) -> bool:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            "CREATE TABLE IF NOT EXISTS sent ("
            "k TEXT PRIMARY KEY, "
            "content_hash TEXT, "
            "status TEXT, "
            "first_seen TEXT, "
            "updated_at TEXT, "
            "preview TEXT)"
        )
        now = _now()
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT status, updated_at FROM sent WHERE k=?", (key,)).fetchone()
        # 核验修（2026-07-16）：pending 加陈旧豁免——claim 后、mark 前进程被杀（重启/树杀）
        # 会留永久 pending，同键当日全部重发被 duplicate_skip 吞（且 skip 走 exit 0，上游
        # 误报"已推"）。pending 超 30min 视为死 claim，放行重发；sent 仍永久挡。
        stale_pending = False
        if row and row[0] == "pending":
            try:
                age = (datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                       - datetime.strptime(str(row[1])[:19], "%Y-%m-%d %H:%M:%S")
                       ).total_seconds()
                stale_pending = age > 1800
            except (ValueError, TypeError):
                stale_pending = True  # updated_at 坏 → 按死 claim 放行（保送达）
        if row and (row[0] == "sent" or (row[0] == "pending" and not stale_pending)):
            con.rollback()
            _append_event(
                event="duplicate_skip",
                key=key,
                key_prefix=key[:12],
                content_hash=content_hash,
                dedupe_key=dkey,
                target=target,
                existing_status=row[0],
                existing_updated_at=row[1],
                preview=preview[:160],
            )
            print(f"[qq_push_dedupe] skip duplicate key={key[:12]} status={row[0]}")
            return False
        con.execute(
            "INSERT OR REPLACE INTO sent(k, content_hash, status, first_seen, updated_at, preview) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (key, content_hash, "pending", now, now, preview[:500]),
        )
        con.commit()
        _append_event(
            event="claim",
            key=key,
            key_prefix=key[:12],
            content_hash=content_hash,
            dedupe_key=dkey,
            target=target,
            preview=preview[:160],
        )
        return True
    finally:
        con.close()


def _mark(key: str, status: str, dkey: str | None, target: str, exit_code: int | None = None) -> None:
    con = sqlite3.connect(DB, timeout=10)
    try:
        now = _now()
        con.execute("UPDATE sent SET status=?, updated_at=? WHERE k=?", (status, now, key))
        con.commit()
    finally:
        con.close()
    _append_event(event="mark", key=key, key_prefix=key[:12], dedupe_key=dkey, target=target,
                  status=status, exit_code=exit_code)


def _strip_wrapper_args() -> None:
    """qq_push_raw 是严格 argparse：wrapper 专属参数必须在 runpy 前从 sys.argv 剥掉。"""
    argv: list[str] = []
    skip = False
    for a in sys.argv:
        if skip:
            skip = False
            continue
        if a == "--dedupe-key":
            skip = True
            continue
        if a.startswith("--dedupe-key="):
            continue
        argv.append(a)
    sys.argv[:] = argv


def main() -> int:
    content = _read_content_once()
    if not content.strip():
        # 2026-07-02：空内容不 claim（防空 hash 毒化 dedup key、吞掉后续真实推送）、不外发。
        print("qq_push: 空内容，拒绝外发（exit 2）", file=sys.stderr)
        return 2
    key, content_hash, dkey, target = _dedupe_key(content)
    if not _claim(key, content_hash, content, dkey, target):
        return 0
    _strip_wrapper_args()
    try:
        runpy.run_path(str(RAW), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        _mark(key, "sent" if code == 0 else "failed", dkey, target, code)
        raise
    except Exception:
        _mark(key, "failed", dkey, target, None)
        raise
    else:
        _mark(key, "sent", dkey, target, 0)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
