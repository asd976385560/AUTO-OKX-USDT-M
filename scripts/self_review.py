# -*- coding: utf-8 -*-
"""
self_review.py —— Job C 自省与学习脚本（每日一次，独立于 Job A / Job B）。

职责：
    1) 从 .okx/records/ 中识别指定日期的已平仓样本及其盈亏
    2) 结合 account.db.scoring_history 反向归因 dim1..dim5 的近 7 / 30 天表现
    3) 更新 lessons.db.signal_perf / error_patterns / param_suggestions
    4) 维护 .okx/playbook.md（月度滚动，主文件保留最近 30 条）
    5) 生成 .okx/self-reviews/self-review-YYYY-MM-DD.md 详版复盘

约束：
    - 仅使用 Python 3 标准库
    - 只读 account.db 与 records/；只写 lessons.db、playbook.md、self-reviews/
    - 数据缺失时可降级跳过，但 lessons.db 不可写时必须退出 1
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
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
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily self-review and update lessons.db.")
    parser.add_argument("--date", dest="review_date", help="UTC review date in YYYY-MM-DD, default=yesterday")
    parser.add_argument("--db-root", default=r"E:\OKX\db", help=r"DB root, default E:\OKX\db")
    parser.add_argument(
        "--okx-root",
        default=str(Path.home() / ".openclaw" / "workspace" / ".okx"),
        help=r"Runtime root, default %%USERPROFILE%%\.openclaw\workspace\.okx",
    )
    return parser.parse_args()


def resolve_review_day(raw_value: str | None) -> date:
    if raw_value:
        return date.fromisoformat(raw_value)
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


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
            for dimension in DIMENSIONS:
                dim_samples = [sample for sample in symbol_samples if dimension in sample.dims]
                if not dim_samples:
                    continue
                win_count = sum(1 for sample in dim_samples if sample.pnl_value > 0)
                avg_return = sum(sample.pnl_value for sample in dim_samples) / len(dim_samples)
                perf_rows.append(
                    PerfRow(
                        symbol=symbol,
                        dimension=dimension,
                        window_days=window_days,
                        win_rate=win_count / len(dim_samples),
                        sample_n=len(dim_samples),
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
    high_score_count = sum(1 for sample in day_samples if sample.score_total is not None and sample.score_total >= 35)
    win_count = sum(1 for sample in day_samples if sample.pnl_value > 0)
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
        f"- 高分样本: {high_score_count}\n"
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
        "## 近 7 / 30 天信号表现",
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


# -------- 审计三件套（G1 反事实回看 / G7 周度活跃度 / G12 分数桶分布） --------

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
    lines: list[str] = ["", "## 审计三件套", ""]
    refl = audit.get("reflective") or {}
    if isinstance(refl, dict) and "error" in refl:
        lines.append(f"### 反事实回看（错失机会）\n- 失败：{refl['error']}")
    else:
        lines.append("### 反事实回看（错失机会）")
        lines.append(
            f"- 当日扫描擦边轮次（30-37 分且 IDLE）：{refl.get('scanned', 0)} 条；"
            f"写入 missed_opportunities：{refl.get('written', 0)} 条；"
            f"其中后续 4h 会触发 1R 的：{refl.get('would_hit_1R', 0)} 条"
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

    buckets = audit.get("buckets") or {}
    lines.append("### 近 30 日评分桶分布")
    if isinstance(buckets, dict) and "error" in buckets:
        lines.append(f"- 失败：{buckets['error']}")
    elif not buckets:
        lines.append("- 无数据")
    else:
        total = sum(int(v) for v in buckets.values())
        for k in ("<25", "25-29", "30-37", "38-44", ">=45"):
            v = int(buckets.get(k, 0))
            pct = (v / total * 100.0) if total else 0.0
            lines.append(f"- {k}：{v} 条（{pct:.1f}%）")
        edge = int(buckets.get("30-37", 0))
        if edge >= max(1, int(total * 0.4)):
            lines.append(f"- 提示：擦边桶（30-37）占比 ≥ 40%，说明大量机会处于临界区，建议结合趋势环境复盘是否过于保守")
    lines.append("")
    return lines


def run_review(review_day: date, db_root: Path, okx_root: Path) -> dict[str, object]:
    lessons_path = db_root / "lessons.db"
    account_path = db_root / "account.db"
    if not lessons_path.exists():
        raise ReviewError(f"lessons.db not found: {lessons_path}")
    if not account_path.exists():
        raise ReviewError(f"account.db not found: {account_path}")

    # 先打开 lessons_con 跑审计三件套；任一失败不阻塞主流程
    audit: dict[str, object] = {"reflective": None, "weekly": None, "buckets": None}
    audit_lessons_con = connect_rw(lessons_path)
    try:
        try:
            audit["reflective"] = reflective_lookback(review_day, db_root, audit_lessons_con)
        except (sqlite3.Error, OSError, ValueError) as exc:
            audit["reflective"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            audit["weekly"] = weekly_activity(review_day, db_root, okx_root, audit_lessons_con)
        except (sqlite3.Error, OSError, ValueError) as exc:
            audit["weekly"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            audit["buckets"] = threshold_bucket(review_day, db_root)
        except (sqlite3.Error, OSError, ValueError) as exc:
            audit["buckets"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        audit_lessons_con.close()

    all_records = [record for path in list_record_files(okx_root / "records") if (record := parse_record_file(path))]
    if not all_records:
        # 冷启动也写带审计内容的最小 review
        cold_lines = [
            f"# 自省日报 {review_day.isoformat()}",
            "",
            "> 今日无平仓样本，仅输出审计三件套。",
        ] + format_audit_block(audit)
        review_path = write_self_review(okx_root, review_day, "\n".join(cold_lines))
        return {
            "samples": [],
            "perf_rows": [],
            "error_events": [],
            "suggestions_inserted": 0,
            "playbook_path": None,
            "review_path": review_path,
            "warning": "no closed records found",
            "audit": audit,
        }

    start_day = review_day - timedelta(days=29)
    scoring_con = connect_readonly(account_path)
    lessons_con = connect_rw(lessons_path)
    try:
        scoring_rows = load_scoring_rows(scoring_con, start_day, review_day)
        samples = build_trade_samples(scoring_rows, [record for record in all_records if start_day <= record.close_day <= review_day])
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
        review_path = write_self_review(okx_root, review_day, review_text)
        return {
            "samples": day_samples,
            "perf_rows": perf_rows,
            "error_events": day_events,
            "suggestions_inserted": suggestions_inserted,
            "playbook_path": playbook_path,
            "review_path": review_path,
            "warning": None,
            "audit": audit,
        }
    finally:
        scoring_con.close()
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
    if result["warning"]:
        print(f"[self_review] warning={result['warning']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
