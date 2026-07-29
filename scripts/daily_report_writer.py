# -*- coding: utf-8 -*-
"""日报/周报/月报硬化写入器。

当前约束：
1. 周期编号禁跳号/回滚：trade_day_num = MAX(trade_day_num)+1（事务内）
2. ts 为 UTC+8 字符串 YYYY-MM-DD HH:MM:SS
3. 写后 read-after-write 校验
4. 绝不执行 DELETE/UPDATE 已有 trade_day_num（只 INSERT）
5. --rewrite-null-and-renumber 仅用于显式维护：把 #NULL 行重新编号并补缺号
6. 默认 dry-run 模式（--apply 才真写）
7. 同时落盘 reports/daily-reports/daily-YYYY-MM-DD.md（markdown 全文）

调用：
  echo '<json>' | run_okx_python.ps1 scripts/daily_report_writer.py --stdin
  run_okx_python.ps1 scripts/daily_report_writer.py --json-file path.json [--apply] [--profiles live|demo|both]
  run_okx_python.ps1 scripts/daily_report_writer.py --rewrite-null-and-renumber [--apply]
  run_okx_python.ps1 scripts/daily_report_writer.py --backfill-daily-revision --report-ts "YYYY-MM-DD HH:MM:SS" [--apply]

说明：默认 --profiles both，一次 payload 同时写 live/demo 双段；成功后不要再单独重复写 demo。

退出码：0=成功且校验通过；非0=失败（Agent 须视为 P0）
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
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import trade_report_stats


def sanitize_text(value: str) -> str:
    """Drop invalid surrogate code points that can appear from PowerShell pipes."""
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
DB_PATH = Path(os.environ.get('OKX_ACCOUNT_DB', _project_path('db', 'account.db')))
REPORTS_DIR = Path(os.environ.get('OKX_DAILY_REPORTS_DIR', _project_path('reports', 'daily-reports')))
WEEKLY_REPORTS_DIR = Path(os.environ.get(
    'OKX_WEEKLY_REPORTS_DIR', _project_path('reports', 'weekly')))
LIVE_TRADES_DB = Path(os.environ.get(
    'OKX_LIVE_TRADES_DB', _project_path('db', 'live_trades.db')))
DEMO_TRADES_DB = Path(os.environ.get(
    'OKX_DEMO_TRADES_DB', _project_path('db', 'demo_trades.db')))
LEDGER_DB = Path(os.environ.get('OKX_LEDGER_DB', _project_path('db', 'ledger.db')))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


def now_cst() -> str:
    return datetime.now(CST).strftime(TS_FMT)


def _snapshot_equity(db_path, profile: str, as_of_ts: str | None = None):
    """account.db.account_snapshots 截至报告时点的最新 totalEq。

    按 datetime(ts),rowid DESC，避开 MAX(ts) 词典序坑。
    返回 float 或 None（库缺/锁/异常一律降级 None，不抛、不拖垮日报渲染）。"""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            if as_of_ts:
                r = con.execute(
                    "SELECT totalEq FROM account_snapshots WHERE profile=? AND datetime(ts)<=datetime(?) "
                    "ORDER BY datetime(ts) DESC,rowid DESC LIMIT 1", (profile, as_of_ts)).fetchone()
            else:
                r = con.execute(
                    "SELECT totalEq FROM account_snapshots WHERE profile=? ORDER BY rowid DESC LIMIT 1",
                    (profile,)).fetchone()
        finally:
            con.close()
        return float(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


def _authoritative_cum_pnl(db_path, profile: str, as_of_ts: str | None = None):
    """复用 cum_pnl.py 累计交易PnL口径；失败返回 None，绝不回退裸 SUM。"""
    try:
        import cum_pnl
        info = cum_pnl.cum_for(Path(db_path).parent, profile, as_of_ts=as_of_ts)
        return float(info["cum_pnl"]) if info.get("ok") else None
    except Exception:
        return None


def _account_bill_net_for_day(
    db_path, profile: str, date_str: str, as_of_ts: str
):
    """OKX 账单当日净变动：交易(type=2)+资金费(type=8)，含手续费。

    返回值带账单覆盖上限，避免把尚未采到报告时点的部分账单冒充完整日净收益。
    """
    try:
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT SUM(COALESCE(bal_change,0)),"
                "SUM(COALESCE(fee,0)),SUM(COALESCE(pnl,0)),"
                "MIN(ts),MAX(ts),COUNT(*) FROM account_bills "
                "WHERE profile=? AND type IN ('2','8') "
                "AND ts>=? AND ts<=?",
                (profile, f"{date_str} 00:00:00", as_of_ts),
            ).fetchone()
        finally:
            con.close()
        if not row or not row[5]:
            return None
        return {
            "net": float(row[0] or 0),
            "fees": float(row[1] or 0),
            "pnl_and_funding": float(row[2] or 0),
            "first_ts": row[3], "last_ts": row[4], "rows": int(row[5]),
        }
    except Exception:
        return None


def _fmt_num(value):
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "-"


def _snapshot_positions_summary(db_path, profile: str, as_of_ts: str | None = None,
                                max_age_min: int = 30):
    """读取报告时点之前最近一批 OKX API position_snapshots，精确按批次、不 GROUP BY。

    有 __FLAT__ 哨兵返回“空仓”；无批次或批次距报告时点过旧返回 None，防把缺数据写成空仓。
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            if as_of_ts:
                batch = con.execute(
                    "SELECT ts FROM position_snapshots WHERE profile=? AND datetime(ts)<=datetime(?) "
                    "ORDER BY datetime(ts) DESC,rowid DESC LIMIT 1", (profile, as_of_ts)).fetchone()
            else:
                batch = con.execute(
                    "SELECT ts FROM position_snapshots WHERE profile=? ORDER BY rowid DESC LIMIT 1",
                    (profile,)).fetchone()
            if not batch:
                return None
            ref = datetime.strptime(as_of_ts or now_cst(), TS_FMT).replace(tzinfo=CST)
            batch_dt = datetime.strptime(str(batch[0]), TS_FMT).replace(tzinfo=CST)
            age_min = (ref - batch_dt).total_seconds() / 60.0
            if age_min < -5 or age_min > max_age_min:
                return None
            rows = con.execute(
                "SELECT symbol,side,sz,avgPx,lev,upl FROM position_snapshots "
                "WHERE profile=? AND ts=? ORDER BY rowid", (profile, batch[0])).fetchall()
        finally:
            con.close()
        if not rows:
            return None
        real = [r for r in rows if str(r[0] or "").strip() != "__FLAT__"]
        if not real:
            return "空仓"
        lines = []
        for symbol, side, sz, avg_px, lev, upl in real:
            side_cn = {"long": "多", "short": "空"}.get(str(side or "").lower(), str(side or "-"))
            line = f"- {symbol} {side_cn} {_fmt_num(sz)}张 @{_fmt_num(avg_px)} {_fmt_num(lev)}x"
            if upl is not None:
                line += f" | 浮盈 {float(upl):+.2f}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return None


def fail(msg: str, code: int = 2):
    print(f"[daily_report_writer][FAIL] {msg}", file=sys.stderr)
    sys.exit(code)


def read_stdin_text() -> str:
    if hasattr(sys.stdin, "buffer"):
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def _anomaly_items(value) -> list[str]:
    items: list[str] = []
    for line in str(value or "").splitlines():
        text = line.strip()
        if not text or text in ("无", "- 无"):
            continue
        item = text if text.startswith("-") else f"- {text}"
        if item not in items:
            items.append(item)
    return items


def _append_anomaly(payload: dict, text: str) -> None:
    items = _anomaly_items(payload.get("anomalies"))
    item = text.strip()
    item = item if item.startswith("-") else f"- {item}"
    if item not in items:
        items.append(item)
    payload["anomalies"] = "\n".join(items) if items else "无"


def _raw_object(value) -> dict:
    """Keep reviewer raw facts while adding deterministic report audit data."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {"reviewer_raw_text": value}
        if isinstance(decoded, dict):
            return decoded
        return {"reviewer_raw_value": decoded}
    return {}


def _initial_daily_revision(existing: object = None) -> dict:
    """Return a machine-readable, non-sending revision state for a new report."""
    if isinstance(existing, dict) and existing:
        revision = dict(existing)
        revision.setdefault("schema_version", 1)
        revision.setdefault("number", 1)
        revision.setdefault("kind", "initial")
        revision.setdefault("corrected", False)
        revision.setdefault("resend_review_required", False)
        revision.setdefault("resend_status", "not_requested")
        revision["auto_resend"] = False
        return revision
    return {
        "schema_version": 1,
        "number": 1,
        "kind": "initial",
        "corrected": False,
        "generated_at": now_cst(),
        "resend_review_required": False,
        "resend_status": "not_requested",
        "auto_resend": False,
    }


def _mark_daily_corrected(
    payload: dict, previous_raw_values: list[object]
) -> dict:
    """Advance the daily artifact revision without sending or scheduling a push."""
    previous_number = 0
    for value in previous_raw_values:
        raw = _raw_object(value)
        audit = raw.get("report_audit")
        if not isinstance(audit, dict):
            continue
        revision = audit.get("revision")
        if not isinstance(revision, dict):
            continue
        try:
            previous_number = max(previous_number, int(revision.get("number") or 0))
        except (TypeError, ValueError):
            continue

    raw = _raw_object(payload.get("raw"))
    audit = raw.get("report_audit")
    if not isinstance(audit, dict):
        audit = {}
        raw["report_audit"] = audit
    revision_number = max(previous_number, 1) + 1
    audit["revision"] = {
        "schema_version": 1,
        "number": revision_number,
        "artifact_version": (
            f"daily:{str(payload.get('ts') or '')[:10]}:r{revision_number}"
        ),
        "kind": "corrected",
        "corrected": True,
        "corrected_at": now_cst(),
        "resend_review_required": True,
        "resend_status": "review_required",
        "auto_resend": False,
    }
    payload["report_revision"] = revision_number
    payload["report_revision_kind"] = "corrected"
    payload["resend_review_required"] = True
    payload["raw"] = json.dumps(raw, ensure_ascii=False)
    return payload


REVISION_LINE_RE = re.compile(
    r"(?m)^>\s*report_revision:\s*(\d+)\s*\|\s*"
    r"revision_kind:\s*([a-z_]+)\s*\|\s*"
    r"resend_review_required:\s*(true|false)\s*\|\s*"
    r"auto_resend:\s*(true|false)\s*$",
    re.IGNORECASE,
)
REQUIRED_REVISION_FIELDS = frozenset({
    "number",
    "kind",
    "corrected",
    "resend_review_required",
    "resend_status",
    "auto_resend",
})


def _revision_line(revision: dict) -> str:
    return (
        f"> report_revision: {int(revision['number'])} | "
        f"revision_kind: {revision['kind']} | "
        "resend_review_required: "
        f"{str(bool(revision['resend_review_required'])).lower()} | "
        "auto_resend: false"
    )


def _patch_revision_line(content: str, revision: dict) -> tuple[str, bool]:
    """Insert only the machine revision line; never re-render report facts."""
    expected = {
        "number": int(revision["number"]),
        "kind": str(revision["kind"]).lower(),
        "resend_review_required": bool(
            revision["resend_review_required"]),
        "auto_resend": False,
    }
    match = REVISION_LINE_RE.search(content)
    if match:
        actual = {
            "number": int(match.group(1)),
            "kind": match.group(2).lower(),
            "resend_review_required": match.group(3).lower() == "true",
            "auto_resend": match.group(4).lower() == "true",
        }
        if actual != expected:
            raise RuntimeError(
                "日报已有 revision 行但与数据库修订状态不一致；"
                "拒绝由 metadata backfill 覆盖")
        return content, False

    report_ts = re.search(
        r"(?m)^>\s*ts:\s*(\d{4}-\d{2}-\d{2} "
        r"\d{2}:\d{2}:\d{2})\b",
        content,
    )
    if not report_ts:
        raise RuntimeError("日报缺少规范 ts 行，拒绝 revision backfill")

    lines = content.splitlines(keepends=True)
    insert_after = None
    for index, line in enumerate(lines):
        if line.startswith("> **报告状态："):
            insert_after = index
            break
    if insert_after is None:
        for index, line in enumerate(lines):
            if re.match(r"^>\s*ts:", line):
                insert_after = index
                break
    if insert_after is None:
        raise RuntimeError("日报缺少可定位的元数据区，拒绝 revision backfill")

    newline = "\r\n" if "\r\n" in content else "\n"
    if not lines[insert_after].endswith(("\n", "\r")):
        lines[insert_after] += newline
    lines.insert(insert_after + 1, _revision_line(revision) + newline)
    return "".join(lines), True


def plan_daily_revision_backfill(
    con: sqlite3.Connection,
    report_ts: str,
    report_path: Path,
) -> dict:
    """Plan a metadata-only repair for one existing live/demo daily pair."""
    canonical_ts = trade_report_stats.fmt_ts(report_ts)
    rows = con.execute(
        "SELECT rowid,ts,profile,trade_day_num,raw "
        "FROM daily_reports WHERE ts=? ORDER BY profile",
        (canonical_ts,),
    ).fetchall()
    profiles = [str(row[2]) for row in rows]
    if len(rows) != 2 or set(profiles) != {"live", "demo"}:
        raise RuntimeError(
            "revision backfill 要求 report_ts 恰有 live/demo 两行")
    if not report_path.exists():
        raise FileNotFoundError(f"日报 Markdown 不存在：{report_path}")

    existing_revisions = []
    row_state = []
    for row in rows:
        raw = _raw_object(row[4])
        audit = raw.get("report_audit")
        if not isinstance(audit, dict):
            raise RuntimeError(
                f"{row[2]} 缺少 report_audit；metadata backfill 不重算事实")
        if audit.get("version") != 1 or audit.get("period_kind") != "daily":
            raise RuntimeError(
                f"{row[2]} report_audit 版本/周期无效；拒绝扩大修复范围")
        revision = audit.get("revision")
        if revision is not None:
            if not isinstance(revision, dict):
                raise RuntimeError(f"{row[2]} revision 不是对象")
            if not REQUIRED_REVISION_FIELDS.issubset(revision):
                raise RuntimeError(f"{row[2]} revision 字段不完整")
            if revision.get("auto_resend") is not False:
                raise RuntimeError(f"{row[2]} auto_resend 必须为 false")
            existing_revisions.append(dict(revision))
        row_state.append({
            "rowid": int(row[0]),
            "ts": row[1],
            "profile": row[2],
            "trade_day_num": row[3],
            "old_raw": row[4],
            "raw_object": raw,
            "has_revision": isinstance(revision, dict),
        })

    if existing_revisions:
        revision = existing_revisions[0]
        if any(item != revision for item in existing_revisions[1:]):
            raise RuntimeError("live/demo 已有 revision 不一致，拒绝自动选择")
    else:
        revision = _initial_daily_revision()
        revision.update({
            "artifact_version": f"daily:{canonical_ts[:10]}:r1",
            "metadata_backfilled_at": now_cst(),
            "metadata_backfill_only": True,
            "auto_resend": False,
        })

    updates = []
    for item in row_state:
        if item["has_revision"]:
            continue
        raw = item["raw_object"]
        raw["report_audit"]["revision"] = dict(revision)
        updates.append({
            "rowid": item["rowid"],
            "profile": item["profile"],
            "old_raw": item["old_raw"],
            "new_raw": json.dumps(raw, ensure_ascii=False),
        })

    original_bytes = report_path.read_bytes()
    content = original_bytes.decode("utf-8")
    embedded_ts = re.search(
        r"(?m)^>\s*ts:\s*(\d{4}-\d{2}-\d{2} "
        r"\d{2}:\d{2}:\d{2})\b",
        content,
    )
    if not embedded_ts or embedded_ts.group(1) != canonical_ts:
        raise RuntimeError("日报 Markdown ts 与 --report-ts 不一致")
    patched_content, markdown_change = _patch_revision_line(
        content, revision)
    return {
        "report_ts": canonical_ts,
        "report_path": str(report_path),
        "revision": revision,
        "row_updates": updates,
        "markdown_change": markdown_change,
        "_original_markdown_sha256": hashlib.sha256(
            original_bytes).hexdigest(),
        "_patched_content": patched_content,
    }


def apply_daily_revision_backfill_db(
    con: sqlite3.Connection, plan: dict
) -> None:
    """Apply only ``daily_reports.raw`` with optimistic read-before-write."""
    for update in plan["row_updates"]:
        cursor = con.execute(
            "UPDATE daily_reports SET raw=? "
            "WHERE rowid=? AND raw IS ?",
            (update["new_raw"], update["rowid"], update["old_raw"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"revision backfill 并发校验失败 rowid={update['rowid']}")
        stored = con.execute(
            "SELECT raw FROM daily_reports WHERE rowid=?",
            (update["rowid"],),
        ).fetchone()
        if not stored or stored[0] != update["new_raw"]:
            raise RuntimeError(
                f"revision backfill 回读失败 rowid={update['rowid']}")


def apply_daily_revision_backfill_markdown(plan: dict) -> None:
    if plan["markdown_change"]:
        path = Path(plan["report_path"])
        current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if current_hash != plan["_original_markdown_sha256"]:
            raise RuntimeError(
                "revision backfill Markdown 并发校验失败；拒绝覆盖新内容")
        _atomic_write_text(path, plan["_patched_content"])


def public_daily_revision_backfill_plan(
    plan: dict, applied: bool, backup: dict | None = None,
    idempotent_verified: bool = False,
) -> dict:
    return {
        "backfill_daily_revision": True,
        "dry_run": not applied,
        "applied": applied,
        "report_ts": plan["report_ts"],
        "report_path": plan["report_path"],
        "profiles": sorted(
            update["profile"] for update in plan["row_updates"]),
        "database_rows_changed": (
            len(plan["row_updates"]) if applied else 0),
        "database_rows_planned": len(plan["row_updates"]),
        "database_columns": ["raw"],
        "markdown_change": plan["markdown_change"],
        "revision": plan["revision"],
        "facts_recomputed": False,
        "auto_send": False,
        "auto_resend": False,
        "backup": backup,
        "backup_required_for_apply": True,
        "idempotent_verified": idempotent_verified,
    }


def create_daily_revision_backup(
    db_path: Path,
    report_path: Path,
    backup_dir: Path,
    report_ts: str,
) -> dict:
    """Create and verify a consistent DB backup plus the original Markdown."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S-%f")
    date_key = report_ts[:10].replace("-", "")
    db_backup = backup_dir / (
        f"{db_path.stem}.daily-revision-{date_key}-before-{stamp}.db")
    report_backup = backup_dir / (
        f"{report_path.stem}.daily-revision-before-{stamp}.md")
    if db_backup.exists() or report_backup.exists():
        raise FileExistsError("revision backfill backup target exists")

    source = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    destination = sqlite3.connect(db_backup)
    try:
        source.execute("PRAGMA busy_timeout=5000")
        source.backup(destination)
        check = destination.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError("revision backfill DB backup quick_check failed")
    finally:
        destination.close()
        source.close()
    shutil.copy2(report_path, report_backup)
    return {
        "database": str(db_backup),
        "database_sha256": hashlib.sha256(
            db_backup.read_bytes()).hexdigest(),
        "markdown": str(report_backup),
        "markdown_sha256": hashlib.sha256(
            report_backup.read_bytes()).hexdigest(),
    }


def _report_state(payload: dict) -> dict:
    """Classify a report without blocking publication on live reconcile drift."""
    raw_status = str(payload.get("live_reconcile_status") or "").strip().lower()
    try:
        issue_count = int(payload.get("live_reconcile_issue_count") or 0)
    except (TypeError, ValueError):
        issue_count = 0
    summary = str(payload.get("summary") or "")

    clean_mark = bool(re.search(
        r"Live对账.*(?:已清零|一致|无\s+GHOST/OVER_CLOSED/UNRECORDED|:\s*OK)",
        summary,
        re.IGNORECASE | re.DOTALL,
    ))
    pending_mark = bool(re.search(
        r"Live对账.*(?:未消|待对账|GHOST|OVER_CLOSED|UNRECORDED|LEFTOVER)",
        summary,
        re.IGNORECASE | re.DOTALL,
    ))
    if raw_status in {"clean", "ok", "cleared", "final"}:
        status, reason = "final", "live 对账已清零"
    elif issue_count > 0 or raw_status in {
            "pending", "dirty", "unresolved", "provisional"}:
        status = "provisional"
        reason = (
            f"live 对账待清零（{issue_count} 项）"
            if issue_count > 0 else "live 对账待清零"
        )
    elif raw_status in {"unavailable", "unknown", "error"}:
        status, reason = "provisional", "live 对账状态不可用"
    elif clean_mark:
        status, reason = "final", "live 对账已清零（由 summary 识别）"
    elif pending_mark:
        status, reason = "provisional", "live 对账待清零（由 summary 识别）"
    else:
        status, reason = "provisional", "live 对账状态未声明"
    return {
        "status": status,
        "reason": reason,
        "live_reconcile_status": raw_status or "unspecified",
        "live_reconcile_issue_count": issue_count,
    }


def _compact_trade_metrics(stats: dict) -> dict:
    rejects = stats["risk_rejected_open_attempts"]
    return {
        "source": stats["source"],
        "period_start_ts": stats["period_start_ts"],
        "period_end_ts": stats["period_end_ts"],
        "period_end_exclusive": stats["period_end_exclusive"],
        "open_count": stats["open_count"],
        "close_count": stats["close_count"],
        "realized_pnl": stats["realized_pnl"],
        "win_rate_pct": stats["win_rate_pct"],
        "best_trade": stats["best_trade"],
        "worst_trade": stats["worst_trade"],
        "excluded_rejected_rows": stats["excluded_rejected_rows"],
        "excluded_incomplete_rows": stats["excluded_incomplete_rows"],
        "risk_rejected_open_attempts": {
            "count": rejects["count"],
            "reasons": rejects["reasons"],
            "items": rejects["items"],
        },
    }


def _risk_reject_text(stats: dict) -> str:
    rejected = stats["risk_rejected_open_attempts"]
    if not rejected["count"]:
        return "0 笔"
    symbols = sorted({
        str(item["symbol"]).replace("-USDT-SWAP", "")
        for item in rejected["items"]
    })
    reasons = "、".join(
        f"{reason}×{count}"
        for reason, count in rejected["reasons"].items()
    )
    detail = "/".join(symbols)
    suffix = f"；{reasons}" if reasons else ""
    return f"{rejected['count']} 笔（{detail}{suffix}）"


def _numeric_diff(left, right, tolerance: float = 1e-9) -> bool:
    try:
        return abs(float(left) - float(right)) > tolerance
    except (TypeError, ValueError):
        return True


def _prepare_trade_payload(
    payload: dict,
    *,
    start_ts: str,
    end_ts: str,
    end_exclusive: bool,
    include_avg_hold: bool,
    period_kind: str,
) -> dict:
    """Hydrate report metrics from fill/intent ledgers and audit overrides."""
    out = dict(payload)
    stats_by_profile = {}
    corrections = []
    paths = {"live": LIVE_TRADES_DB, "demo": DEMO_TRADES_DB}
    for profile in ("live", "demo"):
        stats = trade_report_stats.profile_statistics(
            profile,
            paths[profile],
            LEDGER_DB,
            start_ts,
            end_ts,
            end_exclusive=end_exclusive,
            include_avg_hold=include_avg_hold,
        )
        stats_by_profile[profile] = stats
        authoritative = {
            "open_count": stats["open_count"],
            "close_count": stats["close_count"],
            "total_pnl": stats["realized_pnl"],
            "best_trade": stats["best_trade"],
            "worst_trade": stats["worst_trade"],
            "risk_rejected_open_count":
                stats["risk_rejected_open_attempts"]["count"],
            "risk_rejected_open_summary": _risk_reject_text(stats),
        }
        if include_avg_hold:
            authoritative["avg_hold_hours"] = (
                stats.get("open_position_avg_hold_hours")
            )
        if period_kind == "weekly":
            authoritative["win_rate"] = stats["win_rate_pct"]

        for key in ("open_count", "close_count", "total_pnl"):
            field = f"{profile}_{key}"
            if field in out and _numeric_diff(
                    out[field], authoritative[key],
                    tolerance=1e-6 if key == "total_pnl" else 0):
                corrections.append(
                    f"{profile}.{key} {out[field]}→{authoritative[key]}")
        for key, value in authoritative.items():
            out[f"{profile}_{key}"] = value

    if corrections:
        _append_anomaly(
            out,
            "成交统计已按有效 fill 自动校正: " + "；".join(corrections),
        )

    report_state = _report_state(out)
    out["report_status"] = report_state["status"]
    out["report_status_reason"] = report_state["reason"]
    raw = _raw_object(out.get("raw"))
    raw["report_audit"] = {
        "version": 1,
        "period_kind": period_kind,
        "report_state": report_state,
        "trade_metrics": {
            profile: _compact_trade_metrics(stats)
            for profile, stats in stats_by_profile.items()
        },
    }
    if period_kind == "daily":
        previous_audit = _raw_object(payload.get("raw")).get("report_audit")
        previous_revision = (
            previous_audit.get("revision")
            if isinstance(previous_audit, dict) else None
        )
        revision = _initial_daily_revision(previous_revision)
        revision.setdefault(
            "artifact_version",
            f"daily:{str(out.get('ts') or '')[:10]}:r{revision['number']}",
        )
        raw["report_audit"]["revision"] = revision
        out["report_revision"] = revision["number"]
        out["report_revision_kind"] = revision["kind"]
        out["resend_review_required"] = bool(
            revision["resend_review_required"])
    out["raw"] = json.dumps(raw, ensure_ascii=False)
    return out


def prepare_daily_payload(payload: dict) -> dict:
    """Make filled trades/reject attempts authoritative for a daily report."""
    report_ts = trade_report_stats.fmt_ts(payload.get("ts") or now_cst())
    start_ts, end_ts = trade_report_stats.daily_window(report_ts)
    out = {**payload, "ts": report_ts}
    return _prepare_trade_payload(
        out,
        start_ts=start_ts,
        end_ts=end_ts,
        end_exclusive=False,
        include_avg_hold=False,
        period_kind="daily",
    )


def prepare_weekly_payload(payload: dict) -> dict:
    """Use the previous complete Monday-to-Monday interval for weekly facts."""
    week_start_raw = payload.get("week_start_ts")
    if not week_start_raw:
        raise ValueError(
            "weekly 必填 week_start_ts（报告键：本周一 00:00:00）")
    week_start = trade_report_stats.parse_cst(str(week_start_raw))
    start_ts = payload.get("period_start_ts")
    if start_ts:
        start = trade_report_stats.fmt_ts(start_ts)
    else:
        start = (week_start - timedelta(days=7)).strftime(TS_FMT)
    end = trade_report_stats.fmt_ts(
        payload.get("period_end_ts") or week_start.strftime(TS_FMT))
    out = {
        **payload,
        "week_start_ts": week_start.strftime(TS_FMT),
        "period_start_ts": start,
        "period_end_ts": end,
        "period_end_exclusive": True,
    }
    return _prepare_trade_payload(
        out,
        start_ts=start,
        end_ts=end,
        end_exclusive=True,
        include_avg_hold=True,
        period_kind="weekly",
    )


def _augment_operational_anomalies(payload: dict) -> None:
    """从详细 summary 确定性提取丢轮/对账自修，避免顶部仍显示“无”。"""
    for line in str(payload.get("summary") or "").splitlines():
        text = line.strip().lstrip("-").strip()
        if not text:
            continue
        if "丢轮:" in text and not re.search(r"丢轮:\s*(?:PASS|无|0\s*轮)", text):
            _append_anomaly(payload, f"WARN: {text}")
        if ("reconcile补账" in text or "对账补账" in text
                or "对账已自动补账" in text):
            _append_anomaly(payload, f"自修: {text}")
    if not _anomaly_items(payload.get("anomalies")):
        payload["anomalies"] = "无"


def load_payload(args) -> dict:
    if args.stdin:
        raw = read_stdin_text()
    elif args.json_file:
        with open(args.json_file, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    elif args.json:
        raw = args.json
    elif args.rewrite_null_and_renumber:
        return {"_mode": "rewrite_null_and_renumber"}
    elif args.markdown_only and args.kind == "weekly" and args.week_start:
        return {"week_start_ts": args.week_start}
    elif args.backfill_daily_revision:
        if not args.report_ts:
            fail("--backfill-daily-revision 要求 --report-ts")
        return {
            "_mode": "backfill_daily_revision",
            "ts": args.report_ts,
        }
    else:
        fail(
            "缺少输入：需 --stdin / --json-file / --json / "
            "--rewrite-null-and-renumber / --backfill-daily-revision 之一")
    raw = sanitize_text(raw)
    try:
        return json.loads(raw)
    except Exception as e:
        fail(f"输入 JSON 解析失败: {e}；含中文/特殊符号时建议先写 <PROJECT_ROOT>\\tmp\\*.json 再用 --json-file")


def next_trade_day_num(con, report_ts: str | None = None) -> int:
    """返回日报 trade_day_num。

    v7.0e.7 修复：live/demo 同一天应共享同一个 trade_day_num，不能每 INSERT 一行就 +1。
    - 若当天已有非空 trade_day_num：复用当天编号
    - 否则：取所有历史 MAX(trade_day_num)+1
    """
    date_str = (report_ts or now_cst())[:10]
    cur = con.execute(
        "SELECT MIN(trade_day_num) FROM daily_reports "
        "WHERE substr(ts,1,10)=? AND trade_day_num IS NOT NULL",
        (date_str,),
    )
    same_day = cur.fetchone()[0]
    if same_day is not None:
        return int(same_day)
    cur = con.execute("SELECT MAX(trade_day_num) FROM daily_reports WHERE trade_day_num IS NOT NULL")
    mx = cur.fetchone()[0]
    return (mx if mx is not None else 0) + 1


def _daily_fields(payload: dict, profile: str) -> dict:
    """把 payload 规范成一盘 daily_reports 字段（不含 trade_day_num）。"""
    now = now_cst()

    def pf(key, default=0):
        """按 profile 优先读取 live_/demo_ 前缀字段，兼容旧无前缀字段。"""
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    # v7.0e.1/e.7: payload 拆分 live / demo 两套字段
    return {
        "ts": payload.get("ts") or now,
        "profile": profile,
        "open_count": int(pf("open_count", 0)),
        "close_count": int(pf("close_count", 0)),
        "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
        "total_fees": float(pf("total_fees", 0.0) or 0.0),
        "best_trade": pf("best_trade", None) or None,
        "worst_trade": pf("worst_trade", None) or None,
        "summary": payload.get("summary") or "",
        "lessons": payload.get("lessons") or "",
        "raw": payload.get("raw") or "",
    }


def _inherit_existing_daily_revision(con, payload: dict) -> None:
    """Preserve the stored revision state during Markdown-only re-rendering."""
    report_ts = str(payload.get("ts") or "").strip()
    if not report_ts:
        return
    row = con.execute(
        "SELECT raw FROM daily_reports WHERE ts=? "
        "ORDER BY CASE profile WHEN 'live' THEN 0 ELSE 1 END LIMIT 1",
        (report_ts,),
    ).fetchone()
    if not row:
        return
    stored_audit = _raw_object(row[0]).get("report_audit")
    stored_revision = (
        stored_audit.get("revision")
        if isinstance(stored_audit, dict) else None
    )
    if not isinstance(stored_revision, dict):
        return
    raw = _raw_object(payload.get("raw"))
    audit = raw.get("report_audit")
    if not isinstance(audit, dict):
        audit = {}
        raw["report_audit"] = audit
    audit["revision"] = dict(stored_revision)
    payload["raw"] = json.dumps(raw, ensure_ascii=False)
    payload["report_revision"] = stored_revision.get("number")
    payload["report_revision_kind"] = stored_revision.get("kind")
    payload["resend_review_required"] = bool(
        stored_revision.get("resend_review_required", False))


def write_daily(con, payload: dict, apply: bool) -> dict:
    """INSERT 一行 daily_reports，apply=False 只 print。返回结果 dict。"""
    profile = payload.get("profile", "live")
    fields = _daily_fields(payload, profile)

    if not apply:
        print(f"[DRY-RUN] would INSERT daily_reports:")
        for k, v in fields.items():
            v_disp = (v[:120] + '...') if isinstance(v, str) and len(v) > 120 else v
            print(f"  {k:14}= {v_disp}")
        return {"dry_run": True, "fields": fields}

    # 计算 trade_day_num（v7.0e.7：同一天 live/demo 共享编号）
    fields["trade_day_num"] = next_trade_day_num(con, fields["ts"])

    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    try:
        con.execute(
            f"INSERT INTO daily_reports ({cols}) VALUES ({placeholders})",
            list(fields.values())
        )
    except sqlite3.IntegrityError as e:
        fail(f"INSERT 失败（IntegrityError）: {e}")

    # read-after-write 校验：用 last_insert_rowid，避免同一天 live/demo 共享 trade_day_num 时回读到另一行
    rowid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = con.execute(
        "SELECT rowid, trade_day_num, ts, profile, open_count, close_count, total_pnl "
        "FROM daily_reports WHERE rowid = ?",
        (rowid,)
    ).fetchone()
    if not row:
        fail(f"read-after-write 校验失败：rowid={rowid} 未找到")
    print(f"[OK] INSERT daily_reports: rowid={row[0]} trade_day_num={row[1]} ts={row[2]} "
          f"profile={row[3]} opn={row[4]} cls={row[5]} pnl={row[6]}")
    return {"applied": True, "rowid": row[0], "trade_day_num": row[1], "fields": fields}


def correct_existing_daily(
    con, payload: dict, profiles: list[str], apply: bool
) -> dict:
    """精确更正已存在日报，不新增行、不改主键/rowid/trade_day_num。

    这是账务迟到修复后的受控路径。每个目标 ``(ts, profile)`` 必须恰好存在
    一行；任一缺失即整批拒绝。更新后逐字段回读，事务由 main 统一提交。
    """
    report_ts = str(payload.get("ts") or "").strip()
    if not report_ts:
        raise ValueError("--correct-existing 要求 payload.ts 精确锁定原日报")
    update_cols = (
        "open_count", "close_count", "total_pnl", "total_fees",
        "best_trade", "worst_trade", "summary", "lessons", "raw",
    )
    identities = []
    previous_raw_values = []
    for profile in profiles:
        rows = con.execute(
            "SELECT rowid,trade_day_num,ts,profile,raw FROM daily_reports "
            "WHERE ts=? AND profile=?",
            (report_ts, profile),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                f"daily_reports(ts={report_ts},profile={profile}) "
                f"必须恰好 1 行，实际={len(rows)}")
        row = rows[0]
        identities.append({
            "rowid": int(row[0]),
            "trade_day_num": row[1],
            "profile": profile,
        })
        previous_raw_values.append(row[4])

    _mark_daily_corrected(payload, previous_raw_values)
    targets = []
    for identity in identities:
        profile = identity["profile"]
        fields = _daily_fields(payload, profile)
        targets.append({
            "rowid": identity["rowid"],
            "trade_day_num": identity["trade_day_num"],
            "profile": profile,
            "fields": fields,
        })

    if not apply:
        return {"dry_run": True, "correct_existing": True, "targets": targets}

    assignments = ",".join(f"{col}=?" for col in update_cols)
    for target in targets:
        fields = target["fields"]
        con.execute(
            f"UPDATE daily_reports SET {assignments} WHERE rowid=?",
            [fields[col] for col in update_cols] + [target["rowid"]],
        )
        row = con.execute(
            "SELECT rowid,trade_day_num,ts,profile,"
            + ",".join(update_cols)
            + " FROM daily_reports WHERE rowid=?",
            (target["rowid"],),
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"更正回读失败：rowid={target['rowid']} 不存在")
        if (int(row[0]) != target["rowid"]
                or row[1] != target["trade_day_num"]
                or row[2] != report_ts
                or row[3] != target["profile"]):
            raise RuntimeError(
                f"更正越界：rowid/编号/主键发生变化 profile={target['profile']}")
        stored = dict(zip(update_cols, row[4:]))
        expected = {col: fields[col] for col in update_cols}
        if stored != expected:
            raise RuntimeError(
                f"更正字段回读不一致 profile={target['profile']}: "
                f"stored={stored} expected={expected}")
    return {"applied": True, "correct_existing": True, "targets": targets}


def correct_existing_weekly(
    con, payload: dict, profiles: list[str], apply: bool
) -> dict:
    """Correct an existing weekly pair without changing its identity/number."""
    week_start = str(payload.get("week_start_ts") or "").strip()
    if not week_start:
        raise ValueError(
            "--correct-existing --kind weekly 要求 week_start_ts")
    update_cols = (
        "open_count", "close_count", "total_pnl", "win_rate",
        "avg_hold_hours", "margin_util_pct", "idle_ratio",
        "summary", "lessons", "raw",
    )
    targets = []

    for profile in profiles:
        rows = con.execute(
            "SELECT rowid,trade_week_num,week_start_ts,profile "
            "FROM weekly_reports WHERE week_start_ts=? AND profile=?",
            (week_start, profile),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                f"weekly_reports(week_start_ts={week_start},"
                f"profile={profile}) 必须恰好 1 行，实际={len(rows)}")
        row = rows[0]

        def pf(key, default=None):
            return payload.get(
                f"{profile}_{key}", payload.get(key, default))

        fields = {
            "open_count": int(pf("open_count", 0) or 0),
            "close_count": int(pf("close_count", 0) or 0),
            "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
            "win_rate": pf("win_rate", None),
            "avg_hold_hours": pf("avg_hold_hours", None),
            "margin_util_pct": pf("margin_util_pct", None),
            "idle_ratio": pf("idle_ratio", None),
            "summary": payload.get("summary") or "",
            "lessons": payload.get("lessons") or "",
            "raw": payload.get("raw") or "",
        }
        targets.append({
            "rowid": int(row[0]),
            "trade_week_num": row[1],
            "profile": profile,
            "fields": fields,
        })

    if not apply:
        return {
            "dry_run": True, "correct_existing": True,
            "kind": "weekly", "targets": targets,
        }

    assignments = ",".join(f"{col}=?" for col in update_cols)
    for target in targets:
        fields = target["fields"]
        con.execute(
            f"UPDATE weekly_reports SET {assignments} WHERE rowid=?",
            [fields[col] for col in update_cols] + [target["rowid"]],
        )
        row = con.execute(
            "SELECT rowid,trade_week_num,week_start_ts,profile,"
            + ",".join(update_cols)
            + " FROM weekly_reports WHERE rowid=?",
            (target["rowid"],),
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"weekly 更正回读失败：rowid={target['rowid']} 不存在")
        if (int(row[0]) != target["rowid"]
                or row[1] != target["trade_week_num"]
                or row[2] != week_start
                or row[3] != target["profile"]):
            raise RuntimeError(
                "weekly 更正越界：rowid/编号/主键发生变化 "
                f"profile={target['profile']}")
        stored = dict(zip(update_cols, row[4:]))
        expected = {col: fields[col] for col in update_cols}
        if stored != expected:
            raise RuntimeError(
                f"weekly 更正字段回读不一致 profile={target['profile']}: "
                f"stored={stored} expected={expected}")
    return {
        "applied": True, "correct_existing": True,
        "kind": "weekly", "targets": targets,
    }


def _shared_period_num(con, table: str, num_col: str, ts_col: str, ts_val: str) -> int:
    """同一周期 live/demo 共享编号；无则 MAX+1（禁跳号/回滚）。"""
    cur = con.execute(
        f"SELECT MIN({num_col}) FROM {table} WHERE {ts_col}=? AND {num_col} IS NOT NULL",
        (ts_val,),
    )
    same = cur.fetchone()[0]
    if same is not None:
        return int(same)
    mx = con.execute(f"SELECT MAX({num_col}) FROM {table} WHERE {num_col} IS NOT NULL").fetchone()[0]
    return (mx if mx is not None else 0) + 1


def write_weekly(con, payload: dict, apply: bool) -> dict:
    """INSERT 一行 weekly_reports（PK: week_start_ts+profile；重复即报错，不覆盖）。"""
    profile = payload.get("profile", "live")

    def pf(key, default=None):
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    week_start = payload.get("week_start_ts")
    if not week_start:
        fail("weekly 必填 week_start_ts（本周一 'YYYY-MM-DD HH:MM:SS' UTC+8）")
    fields = {
        "week_start_ts": week_start,
        "profile": profile,
        "open_count": int(pf("open_count", 0) or 0),
        "close_count": int(pf("close_count", 0) or 0),
        "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
        "win_rate": pf("win_rate", None),
        "avg_hold_hours": pf("avg_hold_hours", None),
        "margin_util_pct": pf("margin_util_pct", None),
        "idle_ratio": pf("idle_ratio", None),
        "summary": payload.get("summary") or "",
        "lessons": payload.get("lessons") or "",
        "raw": payload.get("raw") or "",
    }
    if not apply:
        print("[DRY-RUN] would INSERT weekly_reports:")
        for k, v in fields.items():
            print(f"  {k:16}= {(str(v)[:100] if v is not None else None)}")
        return {"dry_run": True, "kind": "weekly", "fields": fields}

    fields["trade_week_num"] = _shared_period_num(con, "weekly_reports", "trade_week_num",
                                                  "week_start_ts", week_start)
    cols = ", ".join(fields.keys())
    ph = ", ".join(["?"] * len(fields))
    try:
        con.execute(f"INSERT INTO weekly_reports ({cols}) VALUES ({ph})", list(fields.values()))
    except sqlite3.IntegrityError as e:
        fail(f"weekly INSERT 失败（该周期+profile 已存在，禁覆盖；如需重写请人工处理）: {e}")
    row = con.execute(
        "SELECT trade_week_num, week_start_ts, profile, total_pnl FROM weekly_reports "
        "WHERE week_start_ts=? AND profile=?",
        (week_start, profile),
    ).fetchone()
    if not row:
        fail("weekly read-after-write 校验失败")
    print(f"[OK] INSERT weekly_reports: trade_week_num={row[0]} week={row[1]} profile={row[2]} pnl={row[3]}")
    return {"applied": True, "kind": "weekly", "trade_week_num": row[0], "fields": fields}


def write_monthly(con, payload: dict, apply: bool) -> dict:
    """INSERT 一行 monthly_reports（PK: month_start_ts+profile；重复即报错，不覆盖）。"""
    profile = payload.get("profile", "live")

    def pf(key, default=None):
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    month_start = payload.get("month_start_ts")
    if not month_start:
        fail("monthly 必填 month_start_ts（本月 1 号 'YYYY-MM-DD HH:MM:SS' UTC+8）")
    fields = {
        "month_start_ts": month_start,
        "profile": profile,
        "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
        "max_drawdown": pf("max_drawdown", None),
        "sharpe_approx": pf("sharpe_approx", None),
        "summary": payload.get("summary") or "",
        "lessons": payload.get("lessons") or "",
        "raw": payload.get("raw") or "",
    }
    if not apply:
        print("[DRY-RUN] would INSERT monthly_reports:")
        for k, v in fields.items():
            print(f"  {k:16}= {(str(v)[:100] if v is not None else None)}")
        return {"dry_run": True, "kind": "monthly", "fields": fields}

    fields["trade_month_num"] = _shared_period_num(con, "monthly_reports", "trade_month_num",
                                                   "month_start_ts", month_start)
    cols = ", ".join(fields.keys())
    ph = ", ".join(["?"] * len(fields))
    try:
        con.execute(f"INSERT INTO monthly_reports ({cols}) VALUES ({ph})", list(fields.values()))
    except sqlite3.IntegrityError as e:
        fail(f"monthly INSERT 失败（该周期+profile 已存在，禁覆盖；如需重写请人工处理）: {e}")
    row = con.execute(
        "SELECT trade_month_num, month_start_ts, profile, total_pnl FROM monthly_reports "
        "WHERE month_start_ts=? AND profile=?",
        (month_start, profile),
    ).fetchone()
    if not row:
        fail("monthly read-after-write 校验失败")
    print(f"[OK] INSERT monthly_reports: trade_month_num={row[0]} month={row[1]} profile={row[2]} pnl={row[3]}")
    return {"applied": True, "kind": "monthly", "trade_month_num": row[0], "fields": fields}


def rewrite_null_and_renumber(con, apply: bool) -> dict:
    """C 方案：把所有 trade_day_num=NULL 的行重新编号（按 ts 升序）"""
    # 现有 #NULL 行
    cur = con.execute("SELECT rowid, ts, substr(summary, 1, 60) FROM daily_reports "
                       "WHERE trade_day_num IS NULL ORDER BY ts")
    nulls = cur.fetchall()
    print(f"[C 方案] 找到 {len(nulls)} 行 trade_day_num=NULL:")
    for r in nulls:
        print(f"  rowid={r[0]} ts={r[1]} summary={r[2]}...")

    # 当前最大 trade_day_num
    cur = con.execute("SELECT MAX(trade_day_num) FROM daily_reports WHERE trade_day_num IS NOT NULL")
    mx = cur.fetchone()[0] or 0
    print(f"[C 方案] 当前 MAX(trade_day_num)={mx}")

    if not apply:
        print(f"[DRY-RUN] C 方案：会按 ts 升序给 NULL 行分配 #{mx+1} ~ #{mx+len(nulls)}")
        return {"dry_run": True, "nulls_count": len(nulls), "next_num": mx+1}

    # 真写
    next_num = mx
    for rowid, ts, _summ in nulls:
        next_num += 1
        con.execute("UPDATE daily_reports SET trade_day_num = ? WHERE rowid = ?", (next_num, rowid))
        print(f"  [OK] rowid={rowid} ts={ts} → trade_day_num={next_num}")

    return {"applied": True, "renumbered": len(nulls), "next_num": next_num}


def write_markdown(payload: dict, apply: bool) -> str:
    """写 reports/daily-reports/daily-YYYY-MM-DD.md（v7.3 交易 PnL + 账户账单净变动）"""
    if not apply:
        ts = payload.get("ts", now_cst())
        date_str = ts[:10]
        path = REPORTS_DIR / f"daily-{date_str}.md"
        print(f"[DRY-RUN] would write markdown: {path}")
        return str(path)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = payload.get("ts", now_cst())
    date_str = ts[:10]
    path = REPORTS_DIR / f"daily-{date_str}.md"
    _augment_operational_anomalies(payload)

    # v7.0e.1: live / demo 数据分别读
    def v(prefix, key, default=0):
        """读 payload[key]，优先用 live_/demo_ 前缀"""
        return payload.get(f"{prefix}_{key}", payload.get(key, default))

    # writer 自取权威值：避免 reviewer 漏传字段时把顶部默认为 0/空仓，而详细 summary 又写真值。
    # 全部按报告 ts 回看，允许安全重渲染历史日报，不误用当前仓位/当前累计收益。
    _live_eq = v('live', 'equity', payload.get('current_equity', None))
    _live_eq_db = _snapshot_equity(DB_PATH, 'live', ts)
    live_eq = _live_eq_db if _live_eq_db is not None else (_live_eq or 0)
    _live_cum_db = _authoritative_cum_pnl(DB_PATH, 'live', ts)
    live_realized_pnl = _live_cum_db if _live_cum_db is not None else v('live', 'realized_pnl', 0)
    live_bill_net = _account_bill_net_for_day(DB_PATH, 'live', date_str, ts)
    live_open = v('live', 'open_count')
    live_close = v('live', 'close_count')
    live_pnl_today = v('live', 'total_pnl', 0)
    live_fees = v('live', 'total_fees', 0)
    live_best = v('live', 'best_trade', '—') or '—'
    live_worst = v('live', 'worst_trade', '—') or '—'
    _live_pos_db = _snapshot_positions_summary(DB_PATH, 'live', ts)
    live_pos = _live_pos_db if _live_pos_db is not None else v(
        'live', 'positions_summary', payload.get('positions_summary', '持仓数据不可用'))

    # P3b (2026-06-29)：payload 优先；缺失/为 0 回退 demo 自身快照（绝不 fallback 到 live，避免收益混淆）
    _demo_eq = v('demo', 'equity', None)
    _demo_eq_db = _snapshot_equity(DB_PATH, 'demo', ts)
    demo_eq = _demo_eq_db if _demo_eq_db is not None else _demo_eq
    # v7.1.2：demo 仍缺（payload 与 account.db 快照均无）→ 显式标 0 + 异常
    if demo_eq is None:
        demo_eq = 0
        _append_anomaly(
            payload,
            "WARN: demo_equity 缺失（payload 与 account.db 快照均无），"
            "已禁止 fallback 到 live equity。",
        )
    _demo_cum_db = _authoritative_cum_pnl(DB_PATH, 'demo', ts)
    demo_realized_pnl = _demo_cum_db if _demo_cum_db is not None else v('demo', 'realized_pnl', 0)
    demo_bill_net = _account_bill_net_for_day(DB_PATH, 'demo', date_str, ts)
    try:
        le, de = float(live_eq), float(demo_eq)
        lr, dr = float(live_realized_pnl), float(demo_realized_pnl)
        # P3b (2026-06-29)：仅当两盘 equity 均非 0 且相同（且 realized 也同）才告警；全 0/缺数据不再误触
        if le != 0 and de != 0 and le == de and lr == dr:
            _append_anomaly(
                payload,
                "WARN: demo/live equity 与 realized_pnl 完全相同，"
                "疑似口径混淆，请核验 demo 数据源。",
            )
    except Exception:
        pass
    demo_open = v('demo', 'open_count')
    demo_close = v('demo', 'close_count')
    demo_pnl_today = v('demo', 'total_pnl', 0)
    demo_fees = v('demo', 'total_fees', 0)
    demo_best = v('demo', 'best_trade', '—') or '—'
    demo_worst = v('demo', 'worst_trade', '—') or '—'
    _demo_pos_db = _snapshot_positions_summary(DB_PATH, 'demo', ts)
    demo_pos = _demo_pos_db if _demo_pos_db is not None else v(
        'demo', 'positions_summary', '持仓数据不可用')
    live_rejects = v('live', 'risk_rejected_open_summary', '0 笔')
    demo_rejects = v('demo', 'risk_rejected_open_summary', '0 笔')
    report_state = _report_state(payload)
    if report_state["status"] == "final":
        report_banner = f"最终报告｜{report_state['reason']}"
    else:
        report_banner = (
            f"临时报告｜{report_state['reason']}；允许发布，"
            "成交与收益以后续对账补正为准"
        )
    raw_for_revision = _raw_object(payload.get("raw"))
    audit_for_revision = raw_for_revision.get("report_audit")
    revision = (
        audit_for_revision.get("revision")
        if isinstance(audit_for_revision, dict) else {}
    )
    if not isinstance(revision, dict):
        revision = {}
    try:
        revision_number = int(revision.get("number") or 1)
    except (TypeError, ValueError):
        revision_number = 1
    revision_kind = str(revision.get("kind") or "initial")
    resend_review_required = bool(
        revision.get("resend_review_required", False))

    live_bill_line = (
        f"${live_bill_net['net']:.2f}（账单至 {live_bill_net['last_ts']}）"
        if live_bill_net else "账单未覆盖"
    )
    demo_bill_line = (
        f"${demo_bill_net['net']:.2f}（账单至 {demo_bill_net['last_ts']}）"
        if demo_bill_net else "账单未覆盖"
    )

    md = f"""# 📊 小灵日报 {date_str}（v7.3 交易PnL + 账户账单净变动）

> 自动生成 by daily_report_writer.py (P7 复盘写入器) — v7.3 收益口径拆分
> ts: {ts} | live/demo 同日共享 trade_day_num（见 db）
> **报告状态：{report_banner}**
> report_revision: {revision_number} | revision_kind: {revision_kind} | resend_review_required: {str(resend_review_required).lower()} | auto_resend: false

---

## 💰 资产（实盘 / 模拟盘分开）

### 🟢 实盘（live）
| 项 | 数值 |
|---|---|
| 资金总额 | ${float(live_eq):.2f} |
| 累计交易PnL（未扣手续费/资金费） | ${float(live_realized_pnl):.2f} |
| 当日账户账单净变动（含手续费/资金费） | {live_bill_line} |

### 🟡 模拟盘（demo）
| 项 | 数值 |
|---|---|
| 资金总额 | ${float(demo_eq):.2f} |
| 累计交易PnL（未扣手续费/资金费） | ${float(demo_realized_pnl):.2f} |
| 当日账户账单净变动（含手续费/资金费） | {demo_bill_line} |

> 累计交易PnL = 冻结基线 + reset 后 trades.pnl；不含手续费、资金费和浮动盈亏。
> 当日账户账单净变动 = OKX account_bills 中 type=2/8 的 bal_change；仅代表上表注明的采集覆盖时段。

> 严禁 live+demo 收益混合 / 用 demo 收益粉饰 live

## 📈 持仓（实盘 / 模拟盘分开）

### 🟢 实盘
{live_pos}

### 🟡 模拟盘
{demo_pos}

## 🎯 交易（实盘 / 模拟盘分开）

### 🟢 实盘
- 今日成交开仓: {int(live_open)} 笔
- 今日成交平仓: {int(live_close)} 笔
- 开仓尝试被风控拒绝: {live_rejects}
- 净 PnL: ${float(live_pnl_today):.2f}
- 手续费: ${float(live_fees):.2f}
- 最佳: {live_best} | 最差: {live_worst}

### 🟡 模拟盘
- 今日成交开仓: {int(demo_open)} 笔
- 今日成交平仓: {int(demo_close)} 笔
- 开仓尝试被风控拒绝: {demo_rejects}
- 净 PnL: ${float(demo_pnl_today):.2f}
- 手续费: ${float(demo_fees):.2f}
- 最佳: {demo_best} | 最差: {demo_worst}

## ⚠️ 异常 / 🛠 自修

{payload.get('anomalies', '无')}

## 🌍 市场

{payload.get('market', '见 push_archive latest')}

## 🧠 教训

{payload.get('lessons', '见 lessons.db')}

---

## 详细 summary

{payload.get('summary', '')}

## 详细 lessons (JSON)

```json
{payload.get('lessons', '')}
```

---

🤖 自动生成 by 小灵 🧚‍♀️ | {now_cst()} CST | daily_report_writer.py v1.3 (v7.3)
"""
    _atomic_write_text(path, md)
    print(f"[OK] wrote markdown: {path} ({path.stat().st_size}B)")
    return str(path)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write a UTF-8 text artifact with same-directory atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def write_weekly_markdown(payload: dict, apply: bool) -> str:
    """Render the authoritative weekly payload to a durable Markdown artifact."""
    week_key = str(payload.get("week_start_ts") or "").strip()
    if not week_key:
        raise ValueError("weekly markdown 要求 week_start_ts")
    path = WEEKLY_REPORTS_DIR / f"weekly-{week_key[:10]}.md"
    if not apply:
        print(f"[DRY-RUN] would atomically write weekly markdown: {path}")
        return str(path)

    def value(profile: str, key: str, default=None):
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    def number(raw, digits=4):
        if raw in (None, ""):
            return "—"
        try:
            return f"{float(raw):.{digits}f}"
        except (TypeError, ValueError):
            return str(raw)

    report_state = _report_state(payload)
    report_label = (
        f"最终报告｜{report_state['reason']}"
        if report_state["status"] == "final"
        else f"临时报告｜{report_state['reason']}"
    )
    start = str(payload.get("period_start_ts") or "")
    end = str(payload.get("period_end_ts") or "")
    if start and end:
        period_line = f"[{start}, {end})，UTC+8"
    else:
        period_line = "历史记录未声明（以 account.db 原行及 summary 为准）"
    week_num = payload.get("trade_week_num")
    rows = []
    for profile, label in (("live", "实盘"), ("demo", "模拟盘")):
        rows.append(
            "| {label} | {opens} | {closes} | {pnl} | {win_rate}% | "
            "{rejects} | {avg_hold} |".format(
                label=label,
                opens=int(value(profile, "open_count", 0) or 0),
                closes=int(value(profile, "close_count", 0) or 0),
                pnl=number(value(profile, "total_pnl", 0.0)),
                win_rate=number(value(profile, "win_rate"), 2),
                rejects=str(
                    value(profile, "risk_rejected_open_summary", "0 笔")
                    or "0 笔"
                ),
                avg_hold=number(value(profile, "avg_hold_hours"), 2),
            )
        )

    content = f"""# 小灵周报 {week_key[:10]}

> 报告键：{week_key}（本周一边界）
> 统计窗口：{period_line}
> trade_week_num：{week_num if week_num is not None else "见 account.db"}
> 报告状态：{report_label}
> 胜率单位：百分数（0–100）

## 成交与绩效

| 盘别 | 成交开仓 | 成交平仓 | 已实现 PnL | 胜率 | 风控拒绝开仓尝试 | 平均持仓小时 |
|---|---:|---:|---:|---:|---|---:|
{chr(10).join(rows)}

## 复盘摘要

{payload.get("summary") or "无"}

## 教训

{payload.get("lessons") or "无"}

---

自动生成：daily_report_writer.py | {now_cst()} CST
"""
    _atomic_write_text(path, content)
    print(f"[OK] atomically wrote weekly markdown: {path} ({path.stat().st_size}B)")
    return str(path)


def _weekly_percent_value(value, raw: dict, profile: str):
    """Normalize legacy ratio rows for read-only Markdown backfill display."""
    try:
        audited = raw["report_audit"]["trade_metrics"][profile]["win_rate_pct"]
        if audited is not None:
            return float(audited)
    except (KeyError, TypeError, ValueError):
        pass
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return value
    units = raw.get("metric_units")
    marked_percent = (
        isinstance(units, dict)
        and units.get("weekly_reports.win_rate") == "percent_0_100"
    )
    if number is not None and 0 <= number <= 1 and not marked_percent:
        return number * 100.0
    return number


def load_existing_weekly_payload(
    con: sqlite3.Connection, week_start: str
) -> dict:
    """Merge one existing live/demo weekly pair using a read-only connection."""
    rows = con.execute(
        "SELECT week_start_ts,profile,open_count,close_count,total_pnl,"
        "win_rate,avg_hold_hours,margin_util_pct,idle_ratio,summary,"
        "lessons,raw,trade_week_num FROM weekly_reports "
        "WHERE week_start_ts=? ORDER BY profile",
        (week_start,),
    ).fetchall()
    if len(rows) != 2 or {str(row[1]) for row in rows} != {"live", "demo"}:
        raise RuntimeError(
            f"weekly markdown backfill requires one live+demo pair: {week_start}")
    payload = {
        "week_start_ts": week_start,
        "trade_week_num": rows[0][12],
        "summary": rows[0][9] or "",
        "lessons": rows[0][10] or "",
        "raw": rows[0][11] or "",
    }
    if rows[0][12] != rows[1][12]:
        raise RuntimeError("weekly live/demo trade_week_num differs")
    raw = _raw_object(payload["raw"])
    audit = raw.get("report_audit")
    if isinstance(audit, dict):
        state = audit.get("report_state")
        if isinstance(state, dict):
            payload["live_reconcile_status"] = state.get(
                "live_reconcile_status")
            payload["live_reconcile_issue_count"] = state.get(
                "live_reconcile_issue_count")
        metrics = audit.get("trade_metrics")
        if isinstance(metrics, dict):
            live_metrics = metrics.get("live")
            if isinstance(live_metrics, dict):
                payload["period_start_ts"] = live_metrics.get(
                    "period_start_ts")
                payload["period_end_ts"] = live_metrics.get(
                    "period_end_ts")
    for row in rows:
        profile = str(row[1])
        row_raw = _raw_object(row[11])
        payload.update({
            f"{profile}_open_count": row[2],
            f"{profile}_close_count": row[3],
            f"{profile}_total_pnl": row[4],
            f"{profile}_win_rate": _weekly_percent_value(
                row[5], row_raw, profile),
            f"{profile}_avg_hold_hours": row[6],
            f"{profile}_margin_util_pct": row[7],
            f"{profile}_idle_ratio": row[8],
        })
        try:
            reject_count = row_raw[
                "report_audit"]["trade_metrics"][profile][
                    "risk_rejected_open_attempts"]["count"]
            payload[f"{profile}_risk_rejected_open_summary"] = (
                f"{int(reject_count)} 笔")
        except (KeyError, TypeError, ValueError):
            payload[f"{profile}_risk_rejected_open_summary"] = "历史未记录"
    return payload


def _commit_then_write_weekly(
    con: sqlite3.Connection, payload: dict, apply: bool
) -> str:
    """Commit DB facts before atomic file replacement; backfill repairs failures."""
    con.commit()
    return write_weekly_markdown(payload, apply)


def _commit_then_write_daily(
    con: sqlite3.Connection, payload: dict, apply: bool
) -> str:
    """Commit DB facts before atomic file replacement; backfill repairs failures."""
    con.commit()
    return write_markdown(payload, apply)


def main():
    global DB_PATH, REPORTS_DIR, WEEKLY_REPORTS_DIR
    global LIVE_TRADES_DB, DEMO_TRADES_DB, LEDGER_DB
    ap = argparse.ArgumentParser(description="Daily Report Writer (P7 hardened writer)")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读 JSON")
    ap.add_argument("--json-file", help="从文件读 JSON")
    ap.add_argument("--json", help="JSON 字符串")
    ap.add_argument("--apply", action="store_true", help="真写模式（默认 dry-run）")
    ap.add_argument("--rewrite-null-and-renumber", action="store_true",
                    help="C 方案：把 trade_day_num=NULL 的行重新编号（需 --apply 才生效）")
    ap.add_argument("--no-markdown", action="store_true", help="不写 markdown 文件")
    ap.add_argument("--markdown-only", action="store_true",
                    help="仅重渲染 daily/weekly markdown，不改报告表；仍需 --apply")
    ap.add_argument(
        "--week-start",
        help="weekly --markdown-only 的现存周报键 YYYY-MM-DD HH:MM:SS",
    )
    ap.add_argument("--correct-existing", action="store_true",
                    help="精确更正已存在 daily/weekly 行；不插入、不改 rowid/编号/主键")
    ap.add_argument(
        "--backfill-daily-revision",
        action="store_true",
        help=(
            "仅为既有日报补 raw.report_audit.revision 与 Markdown revision 行；"
            "默认 dry-run，不重算其他事实、不自动重发"
        ),
    )
    ap.add_argument(
        "--report-ts",
        help="revision backfill 精确日报键 YYYY-MM-DD HH:MM:SS",
    )
    ap.add_argument(
        "--report-file",
        help="revision backfill Markdown 路径；默认按 report-ts 日期定位",
    )
    ap.add_argument(
        "--backup-dir",
        help="revision backfill --apply 必填；更新前备份并校验数据库和 Markdown",
    )
    ap.add_argument("--kind", choices=("daily", "weekly", "monthly"), default="daily",
                    help="报告类型：daily=daily_reports（默认）；weekly=weekly_reports（需 week_start_ts）；monthly=monthly_reports（需 month_start_ts）")
    ap.add_argument("--profiles", choices=("live", "demo", "both"), default="both",
                    help="写入 profile 范围：both=同一 payload 写 live+demo（默认）；live/demo=仅写单段，避免重复调用冲突")
    ap.add_argument("--db-path", default=str(DB_PATH), help="account.db 路径（默认 <PROJECT_ROOT>\\db\\account.db；测试可传临时库）")
    ap.add_argument("--reports-dir", default=str(REPORTS_DIR), help="日报 markdown 输出目录")
    ap.add_argument(
        "--weekly-reports-dir",
        default=str(WEEKLY_REPORTS_DIR),
        help="周报 markdown 输出目录",
    )
    ap.add_argument("--live-trades-db", default=str(LIVE_TRADES_DB),
                    help="live_trades.db 路径")
    ap.add_argument("--demo-trades-db", default=str(DEMO_TRADES_DB),
                    help="demo_trades.db 路径")
    ap.add_argument("--ledger-db", default=str(LEDGER_DB),
                    help="ledger.db 路径（风控拒绝尝试事实源）")
    args = ap.parse_args()

    DB_PATH = Path(args.db_path)
    REPORTS_DIR = Path(args.reports_dir)
    WEEKLY_REPORTS_DIR = Path(args.weekly_reports_dir)
    LIVE_TRADES_DB = Path(args.live_trades_db)
    DEMO_TRADES_DB = Path(args.demo_trades_db)
    LEDGER_DB = Path(args.ledger_db)

    payload = (
        {}
        if args.markdown_only and args.kind == "weekly"
        else load_payload(args)
    )

    if not DB_PATH.exists():
        fail(f"db 不存在：{DB_PATH}")
    if args.correct_existing and args.kind == "monthly":
        fail("--correct-existing 暂不支持 --kind monthly")
    if sum(bool(x) for x in (
            args.markdown_only, args.correct_existing,
            args.rewrite_null_and_renumber,
            args.backfill_daily_revision)) > 1:
        fail(
            "--markdown-only/--correct-existing/--rewrite-null-and-renumber/"
            "--backfill-daily-revision 互斥")
    if args.backfill_daily_revision:
        if args.kind != "daily":
            fail("--backfill-daily-revision 仅支持 --kind daily")
        if args.no_markdown:
            fail("--backfill-daily-revision 不允许 --no-markdown")
        if args.profiles != "both":
            fail("--backfill-daily-revision 必须同时校验 live/demo")
        if args.apply and not args.backup_dir:
            fail("--backfill-daily-revision --apply 必须提供 --backup-dir")
    if args.markdown_only and args.kind == "weekly":
        if not args.apply:
            fail("weekly --markdown-only 需同时给 --apply")
        if args.no_markdown:
            fail("--markdown-only 与 --no-markdown 冲突")
        if not args.week_start:
            fail("weekly --markdown-only 要求 --week-start")
        ro = sqlite3.connect(
            f"file:{DB_PATH.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=10,
        )
        try:
            ro.execute("PRAGMA busy_timeout=5000")
            payload = load_existing_weekly_payload(ro, args.week_start)
        finally:
            ro.close()
        result = {
            "markdown_only": True,
            "kind": "weekly",
            "path": write_weekly_markdown(payload, True),
            "database_write": False,
        }
        print(f"\n[result] {json.dumps(result, ensure_ascii=False)}")
        sys.exit(0)

    if args.backfill_daily_revision:
        report_ts = trade_report_stats.fmt_ts(args.report_ts)
        report_path = (
            Path(args.report_file)
            if args.report_file
            else REPORTS_DIR / f"daily-{report_ts[:10]}.md"
        )
        if not report_path.exists():
            fail(f"revision backfill 日报 Markdown 不存在：{report_path}")
        if not args.apply:
            try:
                ro = sqlite3.connect(
                    f"file:{DB_PATH.resolve().as_posix()}?mode=ro",
                    uri=True,
                    timeout=10,
                )
                try:
                    ro.execute("PRAGMA busy_timeout=5000")
                    plan = plan_daily_revision_backfill(
                        ro, report_ts, report_path)
                finally:
                    ro.close()
            except Exception as exc:
                fail(f"revision backfill dry-run 失败：{exc}")
            result = public_daily_revision_backfill_plan(
                plan, False)
        else:
            # Validate the exact scope before creating backup artifacts.
            try:
                preflight = sqlite3.connect(
                    f"file:{DB_PATH.resolve().as_posix()}?mode=ro",
                    uri=True,
                    timeout=10,
                )
                try:
                    preflight.execute("PRAGMA busy_timeout=5000")
                    plan_daily_revision_backfill(
                        preflight, report_ts, report_path)
                finally:
                    preflight.close()
                backup = create_daily_revision_backup(
                    DB_PATH,
                    report_path,
                    Path(args.backup_dir),
                    report_ts,
                )
            except Exception as exc:
                fail(f"revision backfill 备份前检查失败：{exc}")
            con = sqlite3.connect(DB_PATH, timeout=10)
            try:
                con.execute("PRAGMA busy_timeout=5000")
                plan = plan_daily_revision_backfill(
                    con, report_ts, report_path)
                con.execute("BEGIN IMMEDIATE")
                apply_daily_revision_backfill_db(con, plan)
                con.commit()
                # DB metadata is the durable fact.  If atomic file replacement
                # fails, rerunning this idempotent command repairs only the file.
                apply_daily_revision_backfill_markdown(plan)
                verification = plan_daily_revision_backfill(
                    con, report_ts, report_path)
                idempotent_verified = (
                    not verification["row_updates"]
                    and not verification["markdown_change"]
                )
                if not idempotent_verified:
                    raise RuntimeError(
                        "revision backfill 二次幂等校验失败")
                check = con.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError(
                        "revision backfill apply 后 quick_check 失败")
                result = public_daily_revision_backfill_plan(
                    plan,
                    True,
                    backup=backup,
                    idempotent_verified=True,
                )
            except Exception as exc:
                con.rollback()
                fail(f"revision backfill 失败：{exc}")
            finally:
                con.close()
        print(f"\n[result] {json.dumps(result, ensure_ascii=False)}")
        sys.exit(0)

    con = sqlite3.connect(DB_PATH)
    weekly_markdown_pending = False
    daily_markdown_pending = False
    try:
        if (args.kind == "daily"
                and payload.get("_mode") != "rewrite_null_and_renumber"):
            payload = prepare_daily_payload(payload)
        elif args.kind == "weekly":
            payload = prepare_weekly_payload(payload)

        if args.markdown_only:
            if args.kind != "daily":
                fail("--markdown-only 仅支持 --kind daily")
            if not args.apply:
                fail("--markdown-only 需同时给 --apply")
            _inherit_existing_daily_revision(con, payload)
            result = {"markdown_only": True, "path": write_markdown(payload, True)}
        elif args.correct_existing:
            profiles = (
                ["live", "demo"] if args.profiles == "both"
                else [args.profiles]
            )
            corrector = (
                correct_existing_daily
                if args.kind == "daily" else correct_existing_weekly
            )
            result = corrector(con, payload, profiles, args.apply)
            if args.kind == "daily" and args.apply and not args.no_markdown:
                daily_markdown_pending = True
            elif args.kind == "weekly" and not args.no_markdown:
                if result.get("targets"):
                    payload["trade_week_num"] = result["targets"][0].get(
                        "trade_week_num")
                weekly_markdown_pending = True
        elif payload.get("_mode") == "rewrite_null_and_renumber":
            result = rewrite_null_and_renumber(con, args.apply)
        else:
            writer = {"daily": write_daily, "weekly": write_weekly, "monthly": write_monthly}[args.kind]
            # weekly/monthly 判断 demo 段是否需要：demo_total_pnl / demo_equity 任一存在即写
            has_demo = any(payload.get(k) is not None for k in
                           ("demo_equity", "demo_session_pnl", "demo_total_pnl", "demo_realized_pnl"))
            if args.profiles == "demo":
                result = writer(con, {**payload, "profile": "demo"}, args.apply)
            else:
                result = writer(con, payload, args.apply)
                if args.profiles == "both" and has_demo:
                    result["demo"] = writer(con, {**payload, "profile": "demo"}, args.apply)
            if args.kind == "daily" and not args.no_markdown:
                if args.apply:
                    daily_markdown_pending = True
                else:
                    write_markdown(payload, False)
            elif args.kind == "weekly" and not args.no_markdown:
                payload["trade_week_num"] = result.get("trade_week_num")
                write_result = result.get("demo")
                if payload["trade_week_num"] is None and isinstance(
                        write_result, dict):
                    payload["trade_week_num"] = write_result.get(
                        "trade_week_num")
                weekly_markdown_pending = True
        if weekly_markdown_pending:
            result["markdown"] = _commit_then_write_weekly(
                con, payload, args.apply)
        elif daily_markdown_pending:
            result["markdown"] = _commit_then_write_daily(
                con, payload, args.apply)
        else:
            con.commit()
    except Exception as e:
        con.rollback()
        fail(f"执行失败：{e}")
    finally:
        con.close()

    print(f"\n[result] {json.dumps(result, ensure_ascii=False, default=str)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
