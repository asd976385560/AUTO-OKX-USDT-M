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
`uncertain_delivery` 表示外发命令超时且没有messageId；它与sent一样阻断同键
再次发送，但审计仍按未确认送达计失败，禁止用幂等重跑猜测结果。

⚠️ --dedupe-key 是本 wrapper 专属参数：qq_push_raw 是严格 argparse，runpy 前必须
_strip_wrapper_args 剥掉，否则 raw SystemExit(2) → 所有带键推送全灭。

Structured events are appended to <PROJECT_ROOT>/logs/push/qq_push_dedupe.jsonl.
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import hashlib
import json
import os
import re
import runpy
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(_public_project_path())
RAW = ROOT / "scripts" / "qq_push_raw.py"
DB = ROOT / "db" / "qq_push_dedupe.db"
EVENT_LOG = ROOT / "logs" / "push" / "qq_push_dedupe.jsonl"
CST = timezone(timedelta(hours=8))
SENT_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS sent ("
    "k TEXT PRIMARY KEY, "
    "content_hash TEXT, "
    "status TEXT, "
    "first_seen TEXT, "
    "updated_at TEXT, "
    "preview TEXT)"
)
UNCERTAIN_DELIVERY_STATUS = "uncertain_delivery"
UNCERTAIN_DELIVERY_EXIT_CODE = 3
PENDING_IN_FLIGHT_EXIT_CODE = 4
STALE_PENDING_EXIT_CODE = 5
CLAIM_ACQUIRED = "claimed"
CLAIM_DUPLICATE_SENT = "duplicate_sent"
CLAIM_DUPLICATE_UNCERTAIN = "duplicate_uncertain_delivery"
CLAIM_PENDING_IN_FLIGHT = "pending_in_flight"
CLAIM_STALE_PENDING = "stale_pending_manual_intervention"
REVIEWER_REPORT_KEY = re.compile(
    r"^reviewer:(\d{4}-\d{2}-\d{2}):(daily|weekly|monthly)(?::[\w.-]+)?$"
)


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
    # --alert 走 C2C 私聊（告警与业务播报分流，2026-08-04）。target 参与 dedupe basis，
    # 所以同一内容发到群和发到告警私聊互不去重——否则改路由后首条告警会被历史键吞掉。
    # reviewer 报告族（daily|weekly|monthly）2026-08-26 主人拍板改 C2C 私聊：路由在
    # 本 wrapper 按 dedupe-key 确定性判定并注入 --report，调用方与 15m 战报不变。
    target = _arg_value(("--target", "--group", "--to", "--group-openid"))
    if not target:
        if "--alert" in sys.argv or "--report" in sys.argv:
            target = "alert" if "--alert" in sys.argv else "report"
        elif dkey and REVIEWER_REPORT_KEY.fullmatch(str(dkey)):
            target = "report"
        else:
            target = "default"
    basis = f"{target}|{dkey or content_hash}"
    key = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return key, content_hash, dkey, target


def _validate_reviewer_report_before_push(
    content: str,
    dedupe_key: str | None,
) -> dict | None:
    """Fail closed for reviewer report identities before claiming a send."""
    identity = str(dedupe_key or "")
    match = REVIEWER_REPORT_KEY.fullmatch(identity)
    if match is None:
        if identity.startswith("reviewer:"):
            raise ValueError(
                "reviewer dedupe identity must be "
                "reviewer:<YYYY-MM-DD>:daily|weekly|monthly")
        return None
    report_day, kind = match.groups()
    supplied = _arg_value(("--content-file", "--file"))
    if not supplied:
        raise ValueError(
            f"reviewer {kind} push requires a canonical report file")
    report_path = Path(supplied).resolve()
    expected_dir = (
        ROOT / "reports" /
        ({"daily": "daily-reports", "weekly": "weekly", "monthly": "monthly"}[kind])
    ).resolve()
    expected_name = f"{kind}-{report_day}.md"
    if report_path.parent != expected_dir or report_path.name != expected_name:
        raise ValueError(
            f"reviewer {kind} push path must be "
            f"{expected_dir / expected_name}")
    file_content = report_path.read_text(encoding="utf-8", errors="strict")
    if file_content != content:
        raise ValueError("reviewer report content changed after initial read")

    if kind == "daily":
        import validate_daily_report
        result = validate_daily_report.validate_report(
            report_path=report_path,
            account_db=ROOT / "db" / "account.db",
            live_trades_db=ROOT / "db" / "live_trades.db",
            ledger_db=ROOT / "db" / "ledger.db",
            market_db=ROOT / "db" / "market.db",
            lessons_db=ROOT / "db" / "lessons.db",
        )
        observed_day = str(result.get("report_ts") or "")[:10]
    else:
        import validate_periodic_report
        result = validate_periodic_report.validate_report(
            kind=kind,
            report_path=report_path,
            account_db=ROOT / "db" / "account.db",
            live_trades_db=ROOT / "db" / "live_trades.db",
            ledger_db=ROOT / "db" / "ledger.db",
            lessons_db=ROOT / "db" / "lessons.db",
        )
        observed_day = str(result.get("report_key") or "")[:10]
    if not bool(result.get("ok")):
        errors = "; ".join(str(item) for item in result.get("errors") or [])
        raise ValueError(
            f"reviewer {kind} report validator rejected artifact: {errors}")
    if observed_day != report_day:
        raise ValueError(
            f"reviewer {kind} identity date {report_day} differs from "
            f"validated report date {observed_day or '<missing>'}")
    return {
        "kind": kind,
        "report_day": report_day,
        "report_path": str(report_path),
        "checks": list(result.get("checks") or []),
    }


def _connect() -> sqlite3.Connection:
    """统一 dedupe 连接策略；NORMAL 为每条写连接显式设置，禁止依赖默认值。"""
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=5000")
        return con
    except Exception:
        con.close()
        raise


def _claim(
    key: str,
    content_hash: str,
    preview: str,
    dkey: str | None,
    target: str,
) -> str:
    con = _connect()
    try:
        con.execute(SENT_TABLE_DDL)
        now = _now()
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT status, updated_at FROM sent WHERE k=?", (key,)).fetchone()
        # pending 只表示 claim→send→mark 之间的在飞状态。超过 30 分钟时真相
        # 已不可判定，禁止自动重发猜测外部送达结果；保留原行供人工处置。
        stale_pending = False
        if row and row[0] == "pending":
            try:
                age = (datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                       - datetime.strptime(str(row[1])[:19], "%Y-%m-%d %H:%M:%S")
                       ).total_seconds()
                stale_pending = age > 1800
            except (ValueError, TypeError):
                stale_pending = True
        if row and row[0] in {
            "sent", UNCERTAIN_DELIVERY_STATUS, "pending"
        }:
            if row[0] == "sent":
                claim_result = CLAIM_DUPLICATE_SENT
            elif row[0] == UNCERTAIN_DELIVERY_STATUS:
                claim_result = CLAIM_DUPLICATE_UNCERTAIN
            elif stale_pending:
                claim_result = CLAIM_STALE_PENDING
            else:
                claim_result = CLAIM_PENDING_IN_FLIGHT
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
                claim_result=claim_result,
                manual_intervention_required=(
                    claim_result == CLAIM_STALE_PENDING),
                preview=preview[:160],
            )
            print(json.dumps({
                "ok": claim_result == CLAIM_DUPLICATE_SENT,
                "send_status": claim_result,
                "existing_status": row[0],
                "existing_updated_at": row[1],
                "manual_intervention_required": (
                    claim_result == CLAIM_STALE_PENDING),
            }, ensure_ascii=False))
            return claim_result
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
        return CLAIM_ACQUIRED
    finally:
        con.close()


def _mark(key: str, status: str, dkey: str | None, target: str, exit_code: int | None = None) -> None:
    con = _connect()
    try:
        now = _now()
        con.execute("BEGIN IMMEDIATE")
        cursor = con.execute(
            "UPDATE sent SET status=?, updated_at=? WHERE k=?",
            (status, now, key),
        )
        if cursor.rowcount != 1:
            con.rollback()
            raise RuntimeError(
                f"dedupe mark expected one row, updated {cursor.rowcount}")
        row = con.execute(
            "SELECT status,updated_at FROM sent WHERE k=?", (key,)
        ).fetchone()
        if row != (status, now):
            con.rollback()
            raise RuntimeError("dedupe mark read-after-write mismatch")
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


def _configure_runtime_paths() -> Path:
    """Bind dedupe truth and event evidence to the selected DB root."""
    global DB, EVENT_LOG
    raw_root = _arg_value(("--db-root",)) or os.environ.get("OKX_DB_ROOT")
    runtime_root = Path(raw_root or DEFAULT_DB_ROOT).expanduser().resolve()
    DB = runtime_root / "qq_push_dedupe.db"
    if os.path.normcase(os.fspath(runtime_root)) == os.path.normcase(
        os.fspath(DEFAULT_DB_ROOT)
    ):
        EVENT_LOG = ROOT / "logs" / "push" / "qq_push_dedupe.jsonl"
    else:
        tag = "r" + hashlib.sha256(
            os.path.normcase(os.fspath(runtime_root)).encode("utf-8")
        ).hexdigest()[:10]
        EVENT_LOG = ROOT / "logs" / "push" / f"qq_push_dedupe-{tag}.jsonl"
    return runtime_root


_PROJECT_ROOT = Path(_public_project_path()).resolve()
_PRODUCTION_DB_ROOT = (_PROJECT_ROOT / 'db').resolve()
DEFAULT_DB_ROOT = _PRODUCTION_DB_ROOT

def main() -> int:
    _configure_runtime_paths()
    content = _read_content_once()
    if not content.strip():
        # 2026-07-02：空内容不 claim（防空 hash 毒化 dedup key、吞掉后续真实推送）、不外发。
        print("qq_push: 空内容，拒绝外发（exit 2）", file=sys.stderr)
        return 2
    key, content_hash, dkey, target = _dedupe_key(content)
    try:
        validation = _validate_reviewer_report_before_push(content, dkey)
    except (OSError, ValueError, sqlite3.Error) as exc:
        _append_event(
            event="validation_reject",
            dedupe_key=dkey,
            target=target,
            error=f"{type(exc).__name__}: {exc}",
            preview=content[:160],
        )
        print(f"qq_push: 报告校验拒绝外发：{exc}", file=sys.stderr)
        return 2
    if validation is not None:
        _append_event(
            event="validation_pass",
            dedupe_key=dkey,
            target=target,
            **validation,
        )
    claim_result = _claim(key, content_hash, content, dkey, target)
    if claim_result != CLAIM_ACQUIRED:
        if claim_result == CLAIM_DUPLICATE_SENT:
            return 0
        if claim_result == CLAIM_DUPLICATE_UNCERTAIN:
            return UNCERTAIN_DELIVERY_EXIT_CODE
        if claim_result == CLAIM_PENDING_IN_FLIGHT:
            return PENDING_IN_FLIGHT_EXIT_CODE
        if claim_result == CLAIM_STALE_PENDING:
            print(
                "qq_push: stale pending requires manual delivery review; "
                "automatic resend is forbidden",
                file=sys.stderr,
            )
            return STALE_PENDING_EXIT_CODE
        raise RuntimeError(f"unknown claim result: {claim_result}")
    if (
        target == "report"
        and "--report" not in sys.argv
        and _arg_value(("--target", "--group", "--to", "--group-openid")) is None
    ):
        # 确定性注入 raw 侧路由旗标；显式 --target 已在 target 解析层优先。
        sys.argv.append("--report")
    _strip_wrapper_args()
    try:
        runpy.run_path(str(RAW), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        status = (
            "sent" if code == 0
            else UNCERTAIN_DELIVERY_STATUS
            if code == UNCERTAIN_DELIVERY_EXIT_CODE
            else "failed"
        )
        _mark(key, status, dkey, target, code)
        raise
    except Exception:
        _mark(key, "failed", dkey, target, None)
        raise
    else:
        _mark(key, "sent", dkey, target, 0)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
