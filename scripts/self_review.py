# -*- coding: utf-8 -*-
"""
self_review.py —— Job C 自省与学习脚本（每日一次，独立于 Job A / Job B）。

职责：
    1) 从 .okx/records/ 中识别指定日期的已平仓样本及其盈亏
    2) 汇总交易经验的正样本、负样本与错失机会；旧 scoring_history 只保留历史兼容
    3) 更新 lessons.db.signal_perf / error_patterns / param_suggestions
    4) 维护 .okx/playbook.md（月度滚动，主文件保留最近 30 条）
    5) 生成 .okx/self-reviews/self-review-YYYY-MM-DD.md 详版复盘

约束：
    - 仅使用 Python 3 标准库
    - 只读 account.db 与 records/；只写 lessons.db、playbook.md、self-reviews/
    - 数据缺失时可降级跳过，但 lessons.db 不可写时必须退出 1
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

DIMENSIONS = ("dim1", "dim2", "dim3", "dim4", "dim5")
WINDOWS = (7, 30)
ENTRY_HEADER_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
FILE_NAME_RE = re.compile(
    r"trade-record-(?P<day>\d{8})-(?P<symbol>[A-Za-z0-9-]+?)-(?P<side>long|short)(?:-live)?-(?P<seq>\d+)\.md$"
)
ISO_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
ISO_TS_RE = re.compile(r"(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")
PNL_PATTERNS = (
    r"净收益[:：]\s*(-?\d+(?:\.\d+)?)",
    r"pnl[:：]\s*(-?\d+(?:\.\d+)?)",
    r"收益率[:：]\s*(-?\d+(?:\.\d+)?)%",
)
CLOSE_TS_PATTERNS = (
    r"close_utc[:：]\s*(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)",
    r"平仓时间[:：]\s*(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)",
    r"close_time[:：]\s*(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)",
)


@dataclass
class ClosedRecord:
    symbol: str
    close_day: date
    close_ts: str | None
    pnl_value: float
    pnl_is_percent: bool
    source_file: Path


@dataclass
class TradeSample:
    symbol: str
    close_day: date
    close_ts: str | None
    pnl_value: float
    pnl_is_percent: bool
    score_total: int | None
    dims: dict[str, int]
    ts: str | None
    source_file: Path


@dataclass
class PerfRow:
    symbol: str
    dimension: str
    window_days: int
    win_rate: float
    sample_n: int
    avg_return: float
    updated_utc: str


@dataclass
class ErrorEvent:
    pattern_name: str
    trigger_condition: str
    post_behavior: str


@dataclass
class SuggestionRow:
    suggestion: str
    current_value: str
    suggested_value: str
    evidence: str


class ReviewError(RuntimeError):
    pass


def utc_now_iso() -> str:
    # 2026-07-15（D4 批0 写方封口）：UTC-Z 改 CST——四处调用（repair_queue 文本头/
    # lessons.db reviewed_utc/updated/seen_utc）全是业务域，统一 CST；函数名沿用。
    # 注意：本文件 :885/:1197-1198 查 market.db（Z 域）的 Z 串查询边界是另一回事，禁动。
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily self-review and update lessons.db.")
    parser.add_argument("--date", dest="review_date", help="UTC+8 review date in YYYY-MM-DD, default=yesterday in Asia/Shanghai")
    parser.add_argument("--db-root", default=_project_path('db'), help=r"DB root, default <PROJECT_ROOT>\db")
    parser.add_argument(
        "--okx-root",
        default=str(Path.home() / ".openclaw" / "workspace" / ".okx"),
        help=r"Runtime root, default %%USERPROFILE%%\.openclaw\workspace\.okx",
    )
    return parser.parse_args()


def resolve_review_day(raw_value: str | None) -> date:
    if raw_value:
        return date.fromisoformat(raw_value)
    # Job C runs at 00:30 Asia/Shanghai and reviews the previous UTC+8 calendar day.
    # Using UTC here shifts the report two local dates back, so keep the default aligned with skill.md.
    shanghai_tz = timezone(timedelta(hours=8))
    return (datetime.now(shanghai_tz) - timedelta(days=1)).date()


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ReviewError(f"missing database: {db_path}")
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def connect_rw(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db_path))


def list_record_files(records_dir: Path) -> list[Path]:
    if not records_dir.exists():
        return []
    return sorted(path for path in records_dir.glob("trade-record-*.md") if path.is_file())


def parse_iso_ts(raw_value: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(raw_value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None




def extract_close_ts(text: str) -> str | None:
    for pattern in CLOSE_TS_PATTERNS:
        matched = re.search(pattern, text, flags=re.IGNORECASE)
        if matched:
            return matched.group(1)
    matched = ISO_TS_RE.search(text)
    return matched.group(1) if matched else None


def extract_close_day(text: str, file_name_day: str | None) -> date | None:
    close_ts = extract_close_ts(text)
    if close_ts:
        parsed = parse_iso_ts(close_ts)
        if parsed:
            return parsed.date()
    matched = re.search(r"平仓日期[:：]\s*(20\d{2}-\d{2}-\d{2})", text)
    if matched:
        return date.fromisoformat(matched.group(1))
    matched = ISO_DATE_RE.search(text)
    if matched:
        return date.fromisoformat(matched.group(1))
    if file_name_day:
        return datetime.strptime(file_name_day, "%Y%m%d").date()
    return None


def extract_pnl(text: str) -> tuple[float | None, bool]:
    for pattern in PNL_PATTERNS:
        matched = re.search(pattern, text, flags=re.IGNORECASE)
        if matched:
            value = float(matched.group(1))
            return value, "%" in matched.group(0)
    return None, False


def parse_record_file(path: Path) -> ClosedRecord | None:
    matched = FILE_NAME_RE.match(path.name)
    file_name_day = matched.group("day") if matched else None
    symbol = matched.group("symbol") if matched else "UNKNOWN"
    text = path.read_text(encoding="utf-8")
    close_day = extract_close_day(text, file_name_day)
    pnl_value, pnl_is_percent = extract_pnl(text)
    close_ts = extract_close_ts(text)
    if close_day is None or pnl_value is None:
        return None
    return ClosedRecord(
        symbol=symbol,
        close_day=close_day,
        close_ts=close_ts,
        pnl_value=pnl_value,
        pnl_is_percent=pnl_is_percent,
        source_file=path,
    )


def normalize_pnl(record: ClosedRecord) -> float:
    return record.pnl_value / 100.0 if record.pnl_is_percent else record.pnl_value


def load_scoring_rows(
    con: sqlite3.Connection,
    start_day: date,
    end_day: date,
) -> dict[str, list[sqlite3.Row]]:
    con.row_factory = sqlite3.Row
    start_ts = datetime.combine(start_day, time.min, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ts = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = con.execute(
        """
        SELECT ts, symbol, dim1, dim2, dim3, dim4, dim5, total
        FROM scoring_history
        WHERE ts >= ? AND ts < ?
        ORDER BY symbol, ts
        """,
        (start_ts, end_ts),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["symbol"]), []).append(row)
    return grouped


def choose_scoring_row(record: ClosedRecord, rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    if not rows:
        return None
    target_ts = parse_iso_ts(record.close_ts) if record.close_ts else None
    if target_ts is None:
        same_day_rows = [row for row in rows if str(row["ts"]).startswith(record.close_day.isoformat())]
        return same_day_rows[-1] if same_day_rows else rows[-1]

    chosen: sqlite3.Row | None = None
    for row in rows:
        row_ts = parse_iso_ts(str(row["ts"]))
        if row_ts is None:
            continue
        if row_ts <= target_ts:
            chosen = row
        else:
            break
    return chosen or rows[-1]


def build_trade_samples(
    scoring_rows_by_symbol: dict[str, list[sqlite3.Row]],
    records: Iterable[ClosedRecord],
) -> list[TradeSample]:
    samples: list[TradeSample] = []
    for record in records:
        row = choose_scoring_row(record, scoring_rows_by_symbol.get(record.symbol, []))
        dims = {dimension: int(row[dimension]) for dimension in DIMENSIONS if row and row[dimension] is not None}
        sample = TradeSample(
            symbol=record.symbol,
            close_day=record.close_day,
            close_ts=record.close_ts,
            pnl_value=normalize_pnl(record),
            pnl_is_percent=record.pnl_is_percent,
            score_total=int(row["total"]) if row and row["total"] is not None else None,
            dims=dims,
            ts=str(row["ts"]) if row else None,
            source_file=record.source_file,
        )
        samples.append(sample)
    return samples


def filter_window(samples: Iterable[TradeSample], review_day: date, window_days: int) -> list[TradeSample]:
    start_day = review_day - timedelta(days=window_days - 1)
    return [sample for sample in samples if start_day <= sample.close_day <= review_day]


def build_perf_rows(samples: Iterable[TradeSample], review_day: date) -> list[PerfRow]:
    """按真实平仓结果聚合；不再按旧五维评分拆桶。"""
    perf_rows: list[PerfRow] = []
    updated_utc = datetime.combine(review_day, time.min, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    samples_list = list(samples)
    symbols = sorted({sample.symbol for sample in samples_list})
    for window_days in WINDOWS:
        window_samples = filter_window(samples_list, review_day, window_days)
        for symbol in symbols:
            symbol_samples = [sample for sample in window_samples if sample.symbol == symbol]
            if not symbol_samples:
                continue
            win_count = sum(1 for sample in symbol_samples if sample.pnl_value > 0)
            avg_return = sum(sample.pnl_value for sample in symbol_samples) / len(symbol_samples)
            perf_rows.append(
                PerfRow(
                    symbol=symbol,
                    dimension="closed_outcome",
                    window_days=window_days,
                    win_rate=win_count / len(symbol_samples),
                    sample_n=len(symbol_samples),
                    avg_return=avg_return,
                    updated_utc=updated_utc,
                )
            )
    return perf_rows


def detect_error_events(samples: Iterable[TradeSample]) -> list[ErrorEvent]:
    # 小灵自主判断模式：不通过硬编码阈值自动标记错误事件
    # 复盘分析由小灵在对话中综合判断
    return []


def build_suggestions(perf_rows: Iterable[PerfRow]) -> list[SuggestionRow]:
    # 小灵自主判断模式：不通过硬编码阈值自动生成建议
    # 策略调整由小灵在对话中综合判断
    return []


def upsert_signal_perf(con: sqlite3.Connection, rows: Iterable[PerfRow]) -> None:
    con.executemany(
        """
        INSERT INTO signal_perf(symbol, dimension, window_days, win_rate, sample_n, avg_return, updated_utc)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, dimension, window_days) DO UPDATE SET
            win_rate=excluded.win_rate,
            sample_n=excluded.sample_n,
            avg_return=excluded.avg_return,
            updated_utc=excluded.updated_utc
        """,
        [
            (
                row.symbol,
                row.dimension,
                row.window_days,
                row.win_rate,
                row.sample_n,
                row.avg_return,
                row.updated_utc,
            )
            for row in rows
        ],
    )


def upsert_error_patterns(con: sqlite3.Connection, events: Iterable[ErrorEvent], seen_utc: str) -> None:
    for event in events:
        existing = con.execute(
            "SELECT pattern_id, hit_count FROM error_patterns WHERE pattern_name=? AND trigger_condition=?",
            (event.pattern_name, event.trigger_condition),
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE error_patterns SET hit_count=?, post_behavior=?, last_seen_utc=? WHERE pattern_id=?",
                (int(existing[1]) + 1, event.post_behavior, seen_utc, int(existing[0])),
            )
        else:
            con.execute(
                """
                INSERT INTO error_patterns(pattern_name, trigger_condition, post_behavior, hit_count, last_seen_utc)
                VALUES(?, ?, ?, 1, ?)
                """,
                (event.pattern_name, event.trigger_condition, event.post_behavior, seen_utc),
            )


def upsert_param_suggestions(con: sqlite3.Connection, suggestions: Iterable[SuggestionRow], created_utc: str) -> int:
    inserted = 0
    for row in suggestions:
        existing = con.execute(
            "SELECT id FROM param_suggestions WHERE suggestion=? AND status='pending'",
            (row.suggestion,),
        ).fetchone()
        if existing:
            continue
        con.execute(
            """
            INSERT INTO param_suggestions(suggestion, current_value, suggested_value, evidence, status, created_utc, decided_utc)
            VALUES(?, ?, ?, ?, 'pending', ?, NULL)
            """,
            (row.suggestion, row.current_value, row.suggested_value, row.evidence, created_utc),
        )
        inserted += 1
    return inserted


def load_pending_suggestions(con: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    return con.execute(
        """
        SELECT suggestion, suggested_value, evidence, created_utc
        FROM param_suggestions
        WHERE status='pending'
        ORDER BY created_utc DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def read_playbook_entries(playbook_path: Path) -> list[tuple[str, str]]:
    if not playbook_path.exists():
        return []
    text = playbook_path.read_text(encoding="utf-8")
    matches = list(ENTRY_HEADER_RE.finditer(text))
    if not matches:
        return []
    entries: list[tuple[str, str]] = []
    for index, matched in enumerate(matches):
        start = matched.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        entries.append((matched.group(1), text[start:end].strip() + "\n"))
    return entries


def roll_playbook_month(playbook_path: Path, review_day: date) -> None:
    entries = read_playbook_entries(playbook_path)
    if not entries:
        return
    first_month = entries[0][0][:7]
    review_month = review_day.strftime("%Y-%m")
    if first_month == review_month:
        return
    archive_path = playbook_path.with_name(f"playbook-{first_month}.md")
    playbook_path.replace(archive_path)


def write_playbook(playbook_path: Path, review_day: date, entry_text: str) -> None:
    playbook_path.parent.mkdir(parents=True, exist_ok=True)
    roll_playbook_month(playbook_path, review_day)
    entries = read_playbook_entries(playbook_path)
    entries.append((review_day.isoformat(), entry_text.strip() + "\n"))
    entries = entries[-30:]
    playbook_path.write_text("\n\n".join(text.strip() for _, text in entries) + "\n", encoding="utf-8")


def build_playbook_entry(
    review_day: date,
    day_samples: list[TradeSample],
    day_events: list[ErrorEvent],
    perf_rows: list[PerfRow],
) -> str:
    total_count = len(day_samples)
    win_count = sum(1 for sample in day_samples if sample.pnl_value > 0)
    loss_count = sum(1 for sample in day_samples if sample.pnl_value < 0)
    hit_rate = (win_count / total_count) if total_count else 0.0
    day_perf = [row for row in perf_rows if row.window_days == 7]
    best_rows = sorted(day_perf, key=lambda row: (row.win_rate, row.sample_n), reverse=True)[:3]
    weak_rows = sorted(day_perf, key=lambda row: (row.win_rate, -row.sample_n))[:3]
    best_line = "；".join(f"{row.symbol}-{row.dimension} {row.win_rate:.0%}/{row.sample_n}" for row in best_rows) or "无"
    weak_line = "；".join(f"{row.symbol}-{row.dimension} {row.win_rate:.0%}/{row.sample_n}" for row in weak_rows) or "无"
    error_line = "；".join(f"{event.pattern_name}({event.trigger_condition})" for event in day_events[:3]) or "无"
    return (
        f"## {review_day.isoformat()}\n"
        f"- 总闭环样本: {total_count}\n"
        f"- 盈利/亏损样本: {win_count}/{loss_count}\n"
        f"- 命中率: {hit_rate:.2%}\n"
        f"- 强项: {best_line}\n"
        f"- 弱项: {weak_line}\n"
        f"- 错判模式: {error_line}\n"
    )


def build_self_review_text(
    review_day: date,
    day_samples: list[TradeSample],
    day_events: list[ErrorEvent],
    perf_rows: list[PerfRow],
    pending_rows: Iterable[sqlite3.Row],
) -> str:
    lines = [
        f"# Self Review {review_day.isoformat()}",
        "",
        "## 样本概览",
        f"- 闭环样本数: {len(day_samples)}",
        f"- 盈利样本数: {sum(1 for sample in day_samples if sample.pnl_value > 0)}",
        f"- 亏损样本数: {sum(1 for sample in day_samples if sample.pnl_value < 0)}",
        "",
        "## 近 7 / 30 天真实平仓表现",
    ]
    ordered_rows = sorted(perf_rows, key=lambda row: (row.window_days, row.symbol, row.dimension))
    if ordered_rows:
        for row in ordered_rows:
            lines.append(
                f"- {row.window_days}d {row.symbol} {row.dimension}: win_rate={row.win_rate:.2%}, sample_n={row.sample_n}, avg_return={row.avg_return:.4f}"
            )
    else:
        lines.append("- 无有效表现数据")
    lines.extend(["", "## 错判模式"])
    if day_events:
        for event in day_events:
            lines.append(f"- {event.pattern_name}: {event.trigger_condition}; {event.post_behavior}")
    else:
        lines.append("- 无")
    lines.extend(["", "## 待主人裁定建议"])
    pending_list = list(pending_rows)
    if pending_list:
        for row in pending_list:
            lines.append(
                f"- {row['suggestion']} | {row['suggested_value']} | {row['evidence']} | created={row['created_utc']}"
            )
    else:
        lines.append("- 无 pending 建议")
    lines.append("")
    return "\n".join(lines)


def write_self_review(okx_root: Path, review_day: date, text: str) -> Path:
    # v3.0 README/config.md define reports/self-reviews as the canonical output dir.
    review_dir = okx_root / "reports" / "self-reviews"
    review_dir.mkdir(parents=True, exist_ok=True)
    output_path = review_dir / f"self-review-{review_day.isoformat()}.md"
    output_path.write_text(text, encoding="utf-8")
    return output_path


# -------- 运行异常巡检 / 任务自修记录 --------

def issue_severity(text: str, default: str = "P2") -> str:
    lowered = text.lower()
    # P0 只给可能直接影响实盘安全的异常；不要因为 prompt 里出现“风控/风险”，
    # 或 JSON timestamp/call id 中偶然包含 401 这类数字片段就误判。
    p0_tokens = ("no stop", "无止损", "止损失败", "algo failed", "wrong order", "错误下单", "signature", "签名失败", "profile 异常", "profile mismatch")
    if any(token in lowered for token in p0_tokens) or re.search(r"\b(?:http\s*)?401\b|\bunauthori[sz]ed\b", lowered):
        return "P0"
    if any(token in lowered for token in ("joba", "jobe", "collect", "schema", "database", "db", "python", "command not found", "file not found", "no such file", "write-error")):
        return "P1"
    return default


def add_repair_issue(
    issues: list[dict[str, str]],
    severity: str,
    source: str,
    title: str,
    detail: str,
    action: str,
    confirm: str = "否",
) -> None:
    fingerprint = (severity, source, title, detail[:300])
    for existing in issues:
        if existing.get("fingerprint") == repr(fingerprint):
            return
    issues.append(
        {
            "id": f"RQ-{len(issues) + 1:03d}",
            "severity": severity,
            "source": source,
            "title": title,
            "detail": detail.strip().replace("\r", " ").replace("\n", " ")[:1200],
            "action": action.strip().replace("\r", " ").replace("\n", " ")[:1200],
            "confirm": confirm,
            "fingerprint": repr(fingerprint),
        }
    )


def recent_cutoff_iso(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def is_runtime_failure(text: str) -> bool:
    lowered = (text or "").lower()
    hard_markers = (
        "traceback", "syntaxerror", "exception", "operationalerror", "permission denied",
        "command not found", "can't open file", "no such file", "file not found",
        "timed out", "timeout", "interrupted", "exit code 1", "returned non-zero",
        "edit failed", "oldtext must match", "could not find edits", "write-error",
        "401", "signature", "schema mismatch", "database is locked", "failed"
    )
    # cycle_runs.error 里大量存的是“决策理由”，例如 IDLE/HOLD_SHORT/CLOSE_SHORT；这些不是运行异常。
    benign_prefixes = ("idle:", "hold_", "close_short", "close_long", "hypothesis:")
    stripped = lowered.strip()
    if stripped.startswith(benign_prefixes) and not any(marker in lowered for marker in hard_markers[:15]):
        return False
    return any(marker in lowered for marker in hard_markers)


def scan_db_runtime_issues(db_root: Path, issues: list[dict[str, str]]) -> None:
    cutoff = recent_cutoff_iso(24)
    account_path = db_root / "account.db"
    if not account_path.exists():
        add_repair_issue(issues, "P1", "account.db", "account.db 不存在", str(account_path), "当前 JobC 检查数据库目录与初始化状态；必要时在确认目标目录后运行 scripts/init_v20_dbs.py --db-root <DB_DIR>。", "是")
        return
    con = connect_readonly(account_path)
    try:
        con.row_factory = sqlite3.Row
        cols = fetch_table_columns(con, "cycle_runs")
        if {"ts_start", "job_id", "error"}.issubset(cols):
            grouped: dict[tuple[str, str], dict[str, object]] = {}
            for row in con.execute(
                """
                SELECT ts_start, ts_end, job_id, state_before, state_after, error
                FROM cycle_runs
                WHERE ts_start >= ? AND error IS NOT NULL AND TRIM(error) <> ''
                ORDER BY ts_start DESC
                LIMIT 200
                """,
                (cutoff,),
            ).fetchall():
                detail = f"{row['ts_start']} {row['job_id']} {row['state_before']}->{row['state_after']} error={row['error']}"
                if not is_runtime_failure(detail):
                    continue
                key = (str(row["job_id"]), str(row["error"])[:300])
                item = grouped.setdefault(key, {"count": 0, "first": row["ts_start"], "latest": row["ts_start"], "detail": detail})
                item["count"] = int(item["count"]) + 1
                item["first"] = min(str(item["first"]), str(row["ts_start"]))
                item["latest"] = max(str(item["latest"]), str(row["ts_start"]))
            for (_job, _err), item in grouped.items():
                detail = f"count={item['count']} first={item['first']} latest={item['latest']} sample={item['detail']}"
                add_repair_issue(
                    issues,
                    issue_severity(detail, "P1"),
                    "account.db.cycle_runs",
                    "cycle_runs 存在运行失败",
                    detail,
                    "当前 JobC/对应任务读取相关脚本/报告，先复现最小失败命令；低风险代码或环境问题立即修复并验证。",
                    "视情况",
                )
        cols = fetch_table_columns(con, "trade_events")
        if {"ts", "action"}.issubset(cols):
            grouped: dict[tuple[str, str], dict[str, object]] = {}
            for row in con.execute(
                """
                SELECT ts, symbol, action, side, sz, fill_px, pnl, ai_reasoning, raw
                FROM trade_events
                WHERE (CASE WHEN ts LIKE '%Z' THEN datetime(ts)
                            ELSE datetime(ts, '-8 hours') END) >= datetime(?) AND (
                    UPPER(action) LIKE '%FAIL%' OR UPPER(action) LIKE '%ERROR%' OR
                    UPPER(action) LIKE '%REJECT%' OR UPPER(action) LIKE '%PAUSE%'
                )
                ORDER BY ts DESC
                LIMIT 100
                """,
                (cutoff,),
            ).fetchall():
                raw = row["raw"] or row["ai_reasoning"] or ""
                detail = f"{row['ts']} {row['symbol']} action={row['action']} side={row['side']} sz={row['sz']} px={row['fill_px']} pnl={row['pnl']} raw={raw}"
                if not is_runtime_failure(detail):
                    continue
                key = (str(row["symbol"]), str(row["action"]))
                item = grouped.setdefault(key, {"count": 0, "first": row["ts"], "latest": row["ts"], "detail": detail})
                item["count"] = int(item["count"]) + 1
                item["first"] = min(str(item["first"]), str(row["ts"]))
                item["latest"] = max(str(item["latest"]), str(row["ts"]))
            for (_sym, _action), item in grouped.items():
                detail = f"count={item['count']} first={item['first']} latest={item['latest']} sample={item['detail']}"
                sev = issue_severity(detail, "P1")
                add_repair_issue(
                    issues,
                    sev,
                    "account.db.trade_events",
                    "trade_events 存在失败/暂停事件",
                    detail,
                    "当前 JobC/对应任务优先确认是否涉及止损、下单、签名或 profile；P0 必须先保证实盘安全，触碰交易/凭证/风控边界时请求主人确认。",
                    "是" if sev == "P0" else "视情况",
                )
        cols = fetch_table_columns(con, "system_state")
        if {"key", "value"}.issubset(cols):
            for key in ("pause_reason", "panic_reason", "last_error"):
                row = con.execute("SELECT value, updated_utc FROM system_state WHERE key=?", (key,)).fetchone()
                if row and str(row["value"] or "").strip():
                    detail = f"{key}={row['value']} updated={row['updated_utc'] if 'updated_utc' in row.keys() else 'N/A'}"
                    add_repair_issue(issues, issue_severity(detail, "P1"), "account.db.system_state", f"system_state.{key} 非空", detail, "当前 JobC 查看 PAUSE/错误原因；不得自动清除，除非已确认根因修复且得到主人确认。", "是")
    finally:
        con.close()


def scan_script_health(okx_root: Path, db_root: Path, issues: list[dict[str, str]]) -> None:
    wrapper = okx_root / "scripts" / "run_okx_python.ps1"
    if not wrapper.exists():
        add_repair_issue(issues, "P1", "scripts", "Python wrapper 缺失", str(wrapper), "当前 JobC 在安全边界内恢复 run_okx_python.ps1；恢复前 JobA/B/E/C 可能无法稳定运行。", "否")
    scripts = [
        okx_root / "scripts" / "collect_data.py",
        okx_root / "scripts" / "collect_slow.py",
        okx_root / "scripts" / "self_review.py",
        okx_root / "scripts" / "jobb_live_account_check.py",
        okx_root / "scripts" / "query_db.py",
    ]
    for script in scripts:
        if not script.exists():
            add_repair_issue(issues, "P1", "scripts", "关键脚本缺失", str(script), "当前 JobC 从备份或版本记录恢复该脚本，并做最小验证。", "否")
            continue
        proc = subprocess.run([sys.executable, "-m", "py_compile", str(script)], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            detail = f"{script}: {proc.stderr or proc.stdout}"
            add_repair_issue(issues, "P1", "py_compile", "关键脚本语法检查失败", detail, "当前 JobC 先 read 该文件，再使用最小 edit 修复并重新 py_compile 验证。", "否")
    schema_path = db_root / "schema.sql"
    if not schema_path.exists():
        add_repair_issue(issues, "P1", "db", "schema.sql 缺失", str(schema_path), "当前 JobC 恢复 schema.sql，否则后续 AI 可能猜表结构。", "否")


def scan_openclaw_runtime_logs(okx_root: Path, issues: list[dict[str, str]]) -> None:
    sessions_dir = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
    if not sessions_dir.exists():
        return
    scan_started_ts = datetime.now().timestamp()
    since = scan_started_ts - 24 * 3600
    include_markers = ("OKX-JobA", "OKX-JobB", "OKX-JobC", "OKX-JobE", _project_path(), _project_path())
    error_markers = (
        "edit failed", "oldtext must match", "could not find edits", "could not find the exact text",
        "file not found", "no such file", "permission denied", "command not found", "can't open file",
        "write-error", "traceback", "syntaxerror", "operationalerror", "schema mismatch"
    )
    scanned = 0
    grouped: dict[tuple[str, str], str] = {}
    for path in sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.name.endswith(".trajectory.jsonl"):
            continue
        try:
            stat = path.stat()
            if stat.st_mtime < since:
                continue
            # 跳过仍在写入的会话（通常就是当前 JobC）。否则读取 latest.md/self-review
            # 的成功输出会把旧修复队列里的 traceback/no such file 递归识别为新异常。
            if stat.st_mtime > scan_started_ts - 120:
                continue
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        full_text = "\n".join(lines)
        # 只扫 cron 隔离会话；不要把人工排查/修复时的临时错误也塞进次日队列。
        # cron 会话的开头 user 消息会带 [cron:...]；其他会话后续工具输出里也可能出现该字符串，不能用全文判断。
        head_text = "\n".join(lines[:20])
        if "[cron:" not in head_text and "agent:main:cron:" not in head_text:
            continue
        session_has_okx = any(marker in full_text for marker in include_markers)
        if not session_has_okx:
            continue
        scanned += 1
        for line in lines:
            lowered = line.lower()
            if not any(marker in lowered for marker in error_markers):
                continue
            # 只采集真实消息/工具结果里的错误，避免 system prompt/tool schema 的 oldText/timeout 噪音。
            # 成功 read 报告文件时，内容里会包含历史 traceback/no such file；这不是新的运行异常。
            if '"toolName":"read"' in line and '"status":"error"' not in line and "enoent" not in lowered:
                continue
            if "# okx 修复队列" in lowered:
                continue
            if not (('"isError":true' in line) or ('"status":"error"' in line) or ('"exitCode":1' in line) or ('command exited with code 1' in lowered) or ('"error"' in lowered and 'toolresult' in lowered)):
                continue
            if not is_runtime_failure(line):
                continue
            marker = next((m for m in error_markers if m in lowered), "runtime error")
            grouped.setdefault((marker, path.name), line[:1500])
        if scanned >= 80:
            break
    for (marker, name), snippet in grouped.items():
        add_repair_issue(
            issues,
            issue_severity(snippet, "P2"),
            f"openclaw session {name}",
            f"隔离会话日志发现 {marker}",
            snippet,
            "当前 JobC/对应任务读取对应 session 日志/cron runs，确认是否已修复；若是 edit/oldText 类问题，必须先 read 文件再构造 edit，或改为脚本化补丁并验证。",
            "视情况",
        )


def build_repair_queue_text(review_day: date, issues: list[dict[str, str]]) -> str:
    generated = utc_now_iso()
    actionable = [i for i in issues if i["severity"] in ("P0", "P1", "P2")]
    lines = [
        f"# OKX 修复队列 {review_day.isoformat()}",
        "",
        f"- generated_utc: {generated}",
        f"- actionable_count(P0/P1/P2): {len(actionable)}",
        f"- total_count: {len(issues)}",
        "",
    ]
    if not actionable:
        lines.append("今日无待处理异常。")
    else:
        lines.append("## 待处理/已自修事项")
        lines.append("")
        for issue in sorted(actionable, key=lambda i: (i["severity"], i["id"])):
            lines.extend([
                f"### {issue['id']} [{issue['severity']}] {issue['title']}",
                f"- 来源：{issue['source']}",
                f"- 详情：{issue['detail']}",
                f"- 建议处理：{issue['action']}",
                f"- 是否需要主人确认：{issue['confirm']}",
                "",
            ])
    p3_items = [i for i in issues if i["severity"] == "P3"]
    if p3_items:
        lines.append("## P3 观察项")
        for issue in p3_items:
            lines.append(f"- {issue['id']} {issue['title']}：{issue['detail']}")
    return "\n".join(lines).rstrip() + "\n"


def write_repair_queue(okx_root: Path, db_root: Path, review_day: date) -> tuple[Path, Path, list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    try:
        scan_db_runtime_issues(db_root, issues)
    except Exception as exc:  # 巡检本身不能阻断 JobC
        add_repair_issue(issues, "P1", "repair_queue", "数据库异常巡检失败", f"{type(exc).__name__}: {exc}", "当前 JobC 检查 self_review.py 的 repair_queue 逻辑和 DB 访问。", "否")
    try:
        scan_script_health(okx_root, db_root, issues)
    except Exception as exc:
        add_repair_issue(issues, "P1", "repair_queue", "脚本健康巡检失败", f"{type(exc).__name__}: {exc}", "当前 JobC 检查 py_compile/wrapper 路径。", "否")
    try:
        scan_openclaw_runtime_logs(okx_root, issues)
    except Exception as exc:
        add_repair_issue(issues, "P2", "repair_queue", "OpenClaw 日志巡检失败", f"{type(exc).__name__}: {exc}", "当前 JobC 检查日志目录权限或路径。", "否")

    repair_dir = okx_root / "reports" / "repair-queue"
    repair_dir.mkdir(parents=True, exist_ok=True)
    dated_path = repair_dir / f"repair-queue-{review_day.isoformat()}.md"
    latest_path = repair_dir / "latest.md"
    text = build_repair_queue_text(review_day, issues)
    dated_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")
    return dated_path, latest_path, issues


# -------- 历史审计（错失机会由 missed_opps_writer 的 decision_card_v1 路径负责） --------

def reflective_lookback(
    review_day: date,
    db_root: Path,
    lessons_con: sqlite3.Connection,
) -> dict[str, int]:
    """扫当日 scoring_history WHERE total∈[30,37] AND action LIKE 'IDLE%'，
    用之后 4h 的 1H K 线估算 |最大顺向幅度|，写入 missed_opportunities。
    冷启动（无 scoring/无 K 线）graceful 返回。"""
    account_path = db_root / "account.db"
    market_path = db_root / "market.db"
    summary = {"scanned": 0, "written": 0, "would_hit_1R": 0}
    if not account_path.exists():
        return summary

    day_str = review_day.isoformat()
    scon = connect_readonly(account_path)
    try:
        scon.row_factory = sqlite3.Row
        try:
            rows = scon.execute(
                """
                SELECT ts, symbol, dim4, total, action, regime, ai_reasoning
                FROM scoring_history
                WHERE substr(ts,1,10) = ?
                  AND total BETWEEN 30 AND 37
                  AND action LIKE 'IDLE%'
                ORDER BY ts
                """,
                (day_str,),
            ).fetchall()
        except sqlite3.OperationalError:
            return summary
    finally:
        scon.close()

    summary["scanned"] = len(rows)
    if not rows or not market_path.exists():
        return summary

    reviewed_utc = utc_now_iso()
    mcon = connect_readonly(market_path)
    try:
        mcon.row_factory = sqlite3.Row
        for row in rows:
            ts_dt = parse_iso_ts(row["ts"])
            if not ts_dt:
                continue
            upper_ts = (ts_dt + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                ks = mcon.execute(
                    """
                    SELECT high, low, close FROM kline_cache
                    WHERE symbol = ? AND timeframe = '1H'
                      AND ts >= ? AND ts <= ?
                    ORDER BY ts
                    """,
                    (row["symbol"], row["ts"], upper_ts),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            if not ks:
                continue
            try:
                entry = float(ks[0]["close"])
            except (TypeError, ValueError):
                continue
            if entry <= 0:
                continue
            highs = [float(k["high"]) for k in ks if k["high"] is not None]
            lows = [float(k["low"]) for k in ks if k["low"] is not None]
            if not highs or not lows:
                continue

            regime = row["regime"]
            ai_text = row["ai_reasoning"] or ""
            if regime == "trend_up":
                direction = "long"
            elif regime == "trend_down":
                direction = "short"
            elif any(token in ai_text for token in ("看涨", "做多", "long", "Long", "LONG")):
                direction = "long"
            elif any(token in ai_text for token in ("看跌", "做空", "short", "Short", "SHORT")):
                direction = "short"
            else:
                direction = "long"  # 缺方向时默认按多向估算，避免噪声

            if direction == "long":
                actual_pct = (max(highs) - entry) / entry * 100.0
            else:
                actual_pct = (entry - min(lows)) / entry * 100.0
            would_hit = 1 if actual_pct >= 1.0 else 0  # 1R≈1% 顺向作保守近似

            try:
                lessons_con.execute(
                    """
                    INSERT INTO missed_opportunities(
                        ts, symbol, score, regime, direction_hint,
                        actual_4h_pct, would_hit_1R, notes, reviewed_utc
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (row["ts"], row["symbol"], int(row["total"]), regime, direction,
                     actual_pct, would_hit, row["action"], reviewed_utc),
                )
                summary["written"] += 1
                if would_hit:
                    summary["would_hit_1R"] += 1
            except sqlite3.Error:
                pass
        lessons_con.commit()
    finally:
        mcon.close()

    return summary


def weekly_activity(
    review_day: date,
    db_root: Path,
    okx_root: Path,
    lessons_con: sqlite3.Connection,
) -> dict[str, object] | None:
    """仅 review_day 是周日（weekday()==6）时跑；统计本周开/平仓、平均持仓、IDLE 占比；
    连续 2 周 open<3 且 idle>0.7 → over_conservative=1。冷启动 graceful。"""
    if review_day.weekday() != 6:
        return None

    week_start = review_day - timedelta(days=6)
    week_start_utc = datetime.combine(week_start, time.min, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    week_end_utc = datetime.combine(review_day + timedelta(days=1), time.min, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    open_count = 0
    close_count = 0
    open_times: dict[str, datetime] = {}
    hold_hours: list[float] = []
    events_path = okx_root / "trade-events.jsonl"
    if not events_path.exists():
        candidate = okx_root / "reports" / "trade-events" / "trade-events.jsonl"
        if candidate.exists():
            events_path = candidate
    if events_path.exists():
        try:
            for line in events_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = evt.get("ts") or evt.get("timestamp_utc") or ""
                if not (week_start_utc <= ts < week_end_utc):
                    continue
                kind = evt.get("kind") or evt.get("event") or ""
                sym = evt.get("symbol", "")
                if kind in ("order_filled", "position_open", "open"):
                    open_count += 1
                    ts_dt = parse_iso_ts(ts)
                    if ts_dt and sym:
                        open_times[sym] = ts_dt
                elif kind in ("position_close", "close", "closed"):
                    close_count += 1
                    ts_dt = parse_iso_ts(ts)
                    if ts_dt and sym in open_times:
                        hold_hours.append((ts_dt - open_times[sym]).total_seconds() / 3600.0)
                        del open_times[sym]
        except OSError:
            pass

    idle_ratio: float | None = None
    account_path = db_root / "account.db"
    if account_path.exists():
        acon = connect_readonly(account_path)
        try:
            try:
                row = acon.execute(
                    """
                    SELECT
                        SUM(CASE WHEN state LIKE 'IDLE%' THEN 1 ELSE 0 END) AS idle_n,
                        COUNT(*) AS total_n
                    FROM cycle_runs
                    WHERE ts >= ? AND ts < ?
                    """,
                    (week_start_utc, week_end_utc),
                ).fetchone()
                if row and row[1]:
                    idle_ratio = float(row[0] or 0) / float(row[1])
            except sqlite3.OperationalError:
                pass
        finally:
            acon.close()

    avg_hold = sum(hold_hours) / len(hold_hours) if hold_hours else None

    over_conservative = 0
    if open_count < 3 and idle_ratio is not None and idle_ratio > 0.7:
        prev_week_start_utc = datetime.combine(week_start - timedelta(days=7), time.min, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            prev_row = lessons_con.execute(
                "SELECT open_count, idle_ratio FROM weekly_activity WHERE week_start_utc=?",
                (prev_week_start_utc,),
            ).fetchone()
            if prev_row and prev_row[0] is not None and prev_row[0] < 3 \
                    and prev_row[1] is not None and prev_row[1] > 0.7:
                over_conservative = 1
        except sqlite3.Error:
            pass

    notes = f"hold_samples={len(hold_hours)}"
    updated = utc_now_iso()
    try:
        lessons_con.execute(
            """
            INSERT INTO weekly_activity(
                week_start_utc, open_count, close_count, avg_hold_hours,
                margin_util_pct, idle_ratio, over_conservative, notes, updated_utc
            ) VALUES(?, ?, ?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(week_start_utc) DO UPDATE SET
                open_count        = excluded.open_count,
                close_count       = excluded.close_count,
                avg_hold_hours    = excluded.avg_hold_hours,
                idle_ratio        = excluded.idle_ratio,
                over_conservative = excluded.over_conservative,
                notes             = excluded.notes,
                updated_utc       = excluded.updated_utc
            """,
            (week_start_utc, open_count, close_count, avg_hold, idle_ratio,
             over_conservative, notes, updated),
        )
        lessons_con.commit()
    except sqlite3.Error:
        pass

    return {
        "week_start_utc": week_start_utc,
        "open_count": open_count,
        "close_count": close_count,
        "avg_hold_hours": avg_hold,
        "idle_ratio": idle_ratio,
        "over_conservative": over_conservative,
    }


def threshold_bucket(review_day: date, db_root: Path) -> dict[str, int]:
    """统计最近 30 天 scoring_history 各分数桶分布（冷启动返回空 dict）。"""
    account_path = db_root / "account.db"
    # 评分桶分布，仅用于自评/复盘统计（非决策门槛）；门槛见 Trade Judgment.md §3 / §3.2
    buckets = {"<25": 0, "25-29": 0, "30-37": 0, "38-44": 0, ">=45": 0}
    if not account_path.exists():
        return buckets
    start = (review_day - timedelta(days=29)).isoformat()
    end = (review_day + timedelta(days=1)).isoformat()
    con = connect_readonly(account_path)
    try:
        try:
            rows = con.execute(
                """
                SELECT total FROM scoring_history
                WHERE substr(ts,1,10) >= ? AND substr(ts,1,10) < ? AND total IS NOT NULL
                """,
                (start, end),
            ).fetchall()
        except sqlite3.OperationalError:
            return buckets
    finally:
        con.close()
    for r in rows:
        try:
            t = int(r[0])
        except (TypeError, ValueError):
            continue
        if t < 25:
            buckets["<25"] += 1
        elif t < 30:
            buckets["25-29"] += 1
        elif t < 38:
            buckets["30-37"] += 1
        elif t < 45:
            buckets["38-44"] += 1
        else:
            buckets[">=45"] += 1
    return buckets


def format_audit_block(audit: dict[str, object]) -> list[str]:
    """把审计三件套结果格式化成 markdown 段落。"""
    lines: list[str] = ["", "## 历史与执行审计", ""]
    refl = audit.get("reflective") or {}
    if isinstance(refl, dict) and "error" in refl:
        lines.append(f"### 反事实回看（错失机会）\n- 失败：{refl['error']}")
    else:
        lines.append("### 反事实回看（错失机会）")
        if refl.get("source") == "decision_card_v1":
            lines.append("- 错失机会由 missed_opps_writer 按 wait/hold 决策卡回填，不使用分数阈值")
        else:
            lines.append(
                f"- 历史兼容扫描：{refl.get('scanned', 0)} 条；"
                f"写入：{refl.get('written', 0)} 条"
            )
    lines.append("")

    weekly = audit.get("weekly")
    lines.append("### 周度活跃度 KPI")
    if weekly is None:
        lines.append("- 非周日，跳过本次统计")
    elif isinstance(weekly, dict) and "error" in weekly:
        lines.append(f"- 失败：{weekly['error']}")
    else:
        avg_hold = weekly.get("avg_hold_hours")
        idle_r = weekly.get("idle_ratio")
        lines.append(
            f"- 周起：{weekly.get('week_start_utc')} | 开仓 {weekly.get('open_count', 0)} | "
            f"平仓 {weekly.get('close_count', 0)} | "
            f"avg_hold={avg_hold:.2f}h" if avg_hold is not None else
            f"- 周起：{weekly.get('week_start_utc')} | 开仓 {weekly.get('open_count', 0)} | "
            f"平仓 {weekly.get('close_count', 0)} | avg_hold=N/A"
        )
        lines.append(
            f"- IDLE 占比：{idle_r:.2%}" if idle_r is not None else "- IDLE 占比：N/A"
        )
        if weekly.get("over_conservative"):
            lines.append("- ⚠️ over_conservative=1（连续 2 周开仓 <3 且 IDLE>70%；建议主人复盘是否过于保守）")
    lines.append("")

    return lines


def write_daily_report_row(
    db_root: Path,
    review_day: date,
    samples: list[TradeSample],
    audit: dict[str, object],
    repair_issues: list[dict[str, str]],
    warning: str | None = None,
) -> None:
    """Upsert account.db.daily_reports for Job C's report contract.

    The table is an audit/report table only; this function never creates trade records and never
    changes system_state or risk settings.
    """
    account_path = db_root / "account.db"
    con = connect_rw(account_path)
    try:
        con.row_factory = sqlite3.Row
        day_start_local = datetime.combine(review_day, time.min, tzinfo=timezone(timedelta(hours=8)))
        day_end_local = day_start_local + timedelta(days=1)
        start_utc = day_start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = day_end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        open_count = 0
        close_count = 0
        total_pnl = 0.0
        try:
            row = con.execute(
                """
                SELECT
                    SUM(CASE WHEN UPPER(action) LIKE 'OPEN%' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN UPPER(action) LIKE 'CLOSE%' THEN 1 ELSE 0 END) AS close_count,
                    SUM(CASE WHEN UPPER(action) LIKE 'CLOSE%' THEN COALESCE(pnl, 0) ELSE 0 END) AS total_pnl
                FROM trade_events
                WHERE (CASE WHEN ts LIKE '%Z' THEN datetime(ts)
                            ELSE datetime(ts, '-8 hours') END) >= datetime(?)
                  AND (CASE WHEN ts LIKE '%Z' THEN datetime(ts)
                            ELSE datetime(ts, '-8 hours') END) < datetime(?)
                """,
                (start_utc, end_utc),
            ).fetchone()
            if row:
                open_count = int(row["open_count"] or 0)
                close_count = int(row["close_count"] or 0)
                total_pnl = float(row["total_pnl"] or 0.0)
        except sqlite3.Error:
            # 冷启动/旧 schema 时仍写入复盘摘要，交易计数降级为 0。
            pass

        best_sample = max(samples, key=lambda s: normalize_pnl(s), default=None)
        worst_sample = min(samples, key=lambda s: normalize_pnl(s), default=None)
        actionable = [issue for issue in repair_issues if issue.get("severity") in ("P0", "P1", "P2")]
        reflective = audit.get("reflective") if isinstance(audit, dict) else None
        buckets = audit.get("buckets") if isinstance(audit, dict) else None
        summary = (
            f"JobC daily review {review_day.isoformat()}: samples={len(samples)}, "
            f"open_count={open_count}, close_count={close_count}, total_pnl={total_pnl:.4f}, "
            f"repair_actionable={len(actionable)}"
        )
        if warning:
            summary += f", warning={warning}"
        lessons = json.dumps(
            {
                "audit_reflective": reflective,
                "legacy_score_audit": buckets,
                "repair_actionable": len(actionable),
                "sample_count": len(samples),
            },
            ensure_ascii=False,
            default=str,
        )
        raw = json.dumps(
            {
                "review_day": review_day.isoformat(),
                "window_utc": [start_utc, end_utc],
                "warning": warning,
                "audit": audit,
                "repair_issue_count": len(repair_issues),
            },
            ensure_ascii=False,
            default=str,
        )
        con.execute(
            """
            INSERT OR REPLACE INTO daily_reports
                (ts, profile, open_count, close_count, total_pnl, total_fees, best_trade, worst_trade, summary, lessons, raw)
            VALUES (?, 'live', ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                review_day.isoformat(),
                open_count,
                close_count,
                total_pnl,
                best_sample.symbol if best_sample else None,
                worst_sample.symbol if worst_sample else None,
                summary,
                lessons,
                raw,
            ),
        )
        con.commit()
    finally:
        con.close()


def run_review(review_day: date, db_root: Path, okx_root: Path) -> dict[str, object]:
    lessons_path = db_root / "lessons.db"
    account_path = db_root / "account.db"
    if not lessons_path.exists():
        raise ReviewError(f"lessons.db not found: {lessons_path}")
    if not account_path.exists():
        raise ReviewError(f"account.db not found: {account_path}")

    # 先打开 lessons_con 跑审计三件套；任一失败不阻塞主流程
    audit: dict[str, object] = {
        "reflective": {
            "source": "decision_card_v1",
            "scanned": 0,
            "written": 0,
            "would_hit_1R": 0,
        },
        "weekly": None,
        "buckets": {"retired": True},
    }
    audit_lessons_con = connect_rw(lessons_path)
    try:
        try:
            audit["weekly"] = weekly_activity(review_day, db_root, okx_root, audit_lessons_con)
        except (sqlite3.Error, OSError, ValueError) as exc:
            audit["weekly"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        audit_lessons_con.close()

    all_records = [record for path in list_record_files(okx_root / "records") if (record := parse_record_file(path))]
    if not all_records:
        # 冷启动也写带审计内容的最小 review
        repair_path, repair_latest_path, repair_issues = write_repair_queue(okx_root, db_root, review_day)
        actionable_count = sum(1 for issue in repair_issues if issue["severity"] in ("P0", "P1", "P2"))
        cold_lines = [
            f"# 自省日报 {review_day.isoformat()}",
            "",
            "> 今日无平仓样本，仅输出审计三件套。",
        ] + format_audit_block(audit) + [
            "## 运行异常自修记录",
            f"- 待处理/已自修事项(P0/P1/P2): {actionable_count}",
            f"- latest: {repair_latest_path}",
            "",
        ]
        review_path = write_self_review(okx_root, review_day, "\n".join(cold_lines))
        write_daily_report_row(db_root, review_day, [], audit, repair_issues, "no closed records found")
        return {
            "samples": [],
            "perf_rows": [],
            "error_events": [],
            "suggestions_inserted": 0,
            "playbook_path": None,
            "review_path": review_path,
            "repair_path": repair_path,
            "repair_latest_path": repair_latest_path,
            "repair_issues": repair_issues,
            "warning": "no closed records found",
            "audit": audit,
        }

    start_day = review_day - timedelta(days=29)
    lessons_con = connect_rw(lessons_path)
    try:
        samples = build_trade_samples(
            {},
            [record for record in all_records if start_day <= record.close_day <= review_day],
        )
        day_samples = [sample for sample in samples if sample.close_day == review_day]
        perf_rows = build_perf_rows(samples, review_day)
        day_events = detect_error_events(day_samples)
        suggestion_rows = build_suggestions(perf_rows)
        seen_utc = utc_now_iso()

        upsert_signal_perf(lessons_con, perf_rows)
        upsert_error_patterns(lessons_con, day_events, seen_utc)
        suggestions_inserted = upsert_param_suggestions(lessons_con, suggestion_rows, seen_utc)
        lessons_con.commit()

        pending_rows = load_pending_suggestions(lessons_con, limit=3)
        playbook_path = okx_root / "playbook.md"
        playbook_entry = build_playbook_entry(review_day, day_samples, day_events, perf_rows)
        write_playbook(playbook_path, review_day, playbook_entry)
        review_text = build_self_review_text(review_day, day_samples, day_events, perf_rows, pending_rows)
        review_text += "\n" + "\n".join(format_audit_block(audit))
        repair_path, repair_latest_path, repair_issues = write_repair_queue(okx_root, db_root, review_day)
        write_daily_report_row(db_root, review_day, day_samples, audit, repair_issues, None)
        review_text += "\n## 运行异常自修记录\n"
        actionable_count = sum(1 for issue in repair_issues if issue["severity"] in ("P0", "P1", "P2"))
        review_text += f"- 待处理/已自修事项(P0/P1/P2): {actionable_count}\n"
        review_text += f"- latest: {repair_latest_path}\n"
        review_path = write_self_review(okx_root, review_day, review_text)
        return {
            "samples": day_samples,
            "perf_rows": perf_rows,
            "error_events": day_events,
            "suggestions_inserted": suggestions_inserted,
            "playbook_path": playbook_path,
            "review_path": review_path,
            "repair_path": repair_path,
            "repair_latest_path": repair_latest_path,
            "repair_issues": repair_issues,
            "warning": None,
            "audit": audit,
        }
    finally:
        lessons_con.close()


def main() -> int:
    args = parse_args()
    review_day = resolve_review_day(args.review_date)
    db_root = Path(args.db_root)
    okx_root = Path(args.okx_root)
    try:
        result = run_review(review_day, db_root, okx_root)
    except (OSError, sqlite3.Error, ValueError, ReviewError, json.JSONDecodeError) as exc:
        print(f"[self_review] ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"[self_review] review_day={review_day.isoformat()}")
    print(f"[self_review] samples={len(result['samples'])}")
    print(f"[self_review] perf_rows={len(result['perf_rows'])}")
    print(f"[self_review] error_events={len(result['error_events'])}")
    print(f"[self_review] suggestions_inserted={result['suggestions_inserted']}")
    if result["playbook_path"]:
        print(f"[self_review] playbook={result['playbook_path']}")
    if result["review_path"]:
        print(f"[self_review] self_review={result['review_path']}")
    if result.get("repair_latest_path"):
        print(f"[self_review] repair_latest={result['repair_latest_path']}")
        repair_issues = result.get("repair_issues") or []
        actionable_count = sum(1 for issue in repair_issues if issue["severity"] in ("P0", "P1", "P2"))
        print(f"[self_review] repair_actionable={actionable_count}")
    if result["warning"]:
        print(f"[self_review] warning={result['warning']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
