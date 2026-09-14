# -*- coding: utf-8 -*-
"""Read-only evidence for the Push header's fixed business-cycle duration."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from scripts import _acceptance_thresholds as thresholds


BUSINESS_DURATION_REQUIRED_FROM = "2026-09-06T21:30"
MEASUREMENT = "cycle_start_to_analysis_judgment_trade_completed"
CLOCK_STOP = "analysis_judgment_trade_completed_before_persistence"
DISPLAY_SEMANTICS = "耗时口径：本轮开始→业务完成"
_CYCLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)$")


def duration_contract_active(cycle_id: object) -> bool:
    if not isinstance(cycle_id, str) or not _CYCLE_RE.fullmatch(cycle_id):
        return False
    try:
        return thresholds.parse_cst(cycle_id) >= thresholds.parse_cst(
            BUSINESS_DURATION_REQUIRED_FROM)
    except (TypeError, ValueError):
        return False


def _read_row(path: Path, sql: str, cycle_id: str) -> dict | None:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                          timeout=2)
    try:
        con.execute("PRAGMA query_only=ON")
        con.row_factory = sqlite3.Row
        row = con.execute(sql, (cycle_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        con.close()


def read_business_duration(db_root: str | Path, cycle_id: object) -> dict:
    """Require the exact completed live receipt AND its persisted business proof.

    No wall clock, trade-cycle ts, dispatch time, or Agent duration is a fallback.
    The supplied database root also owns logs/stage-status; isolated roots never
    fall back to production logs. Failed/missing evidence yields unknown only.
    """
    result = {
        "schema_version": 1, "cycle_id": cycle_id,
        "measurement": MEASUREMENT, "status": "unknown",
        "elapsed_seconds": None, "reason": "cycle_id_invalid",
    }

    def unknown(reason: str) -> dict:
        return {**result, "reason": reason}

    if not isinstance(cycle_id, str) or not _CYCLE_RE.fullmatch(cycle_id):
        return result
    try:
        start = thresholds.parse_cst(cycle_id)
        db = Path(db_root).resolve()
        path = db.parent / "logs" / "stage-status" / (
            "live-" + cycle_id.replace(":", "-") + ".json")
        if not path.is_file():
            return unknown("live_receipt_missing")
        live = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(live, dict) or (
            live.get("stage") != "live" or live.get("cycle_id") != cycle_id
            or live.get("status") != "succeeded"
            or live.get("returncode") != 0
        ):
            return unknown("live_receipt_not_successful")
        check = live.get("business_check")
        collection = live.get("collection_gate")
        if not isinstance(check, dict) or check.get("ok") is not True:
            return unknown("business_check_not_proven")
        if not isinstance(collection, dict) or (
            collection.get("cycle_id") != cycle_id
            or collection.get("status") != "met"
        ):
            return unknown("collection_gate_not_proven")
        terminal = check.get("business_terminal")
        if not isinstance(terminal, dict) or (
            type(terminal.get("schema_version")) is not int
            or terminal.get("schema_version") != 1
            or terminal.get("cycle_id") != cycle_id
            or terminal.get("status") != "completed"
            or terminal.get("clock_stop") != CLOCK_STOP
        ):
            return unknown("business_terminal_invalid")
        completed = thresholds.parse_cst(terminal["completed_at_cst"])
        collected = thresholds.parse_cst(collection["completed_at"])
        finished = thresholds.parse_cst(live["finished_at"])
        if not start <= collected <= completed <= finished:
            return unknown("business_time_order_invalid")
        row = _read_row(db / "live_trades.db",
                        "SELECT mode, decision, raw FROM trade_cycles "
                        "WHERE cycle_id=?", cycle_id)
        if row is None:
            return unknown("trade_cycle_missing")
        raw = json.loads(row["raw"])
        if not isinstance(raw, dict) or (
            row.get("mode") != "live"
            or row.get("decision") not in {"traded", "hold", "skip"}
            or raw.get("status") != "ok"
            or raw.get("batch_status") != "completed"
            or raw.get("runner_in_progress") is not False
        ):
            return unknown("trade_cycle_not_completed")
        saved = raw.get("business_terminal")
        if not isinstance(saved, dict) or any(
            saved.get(key) != terminal.get(key)
            for key in ("schema_version", "cycle_id", "status", "clock_stop")
        ) or thresholds.parse_cst(saved["completed_at_cst"]) != completed:
            return unknown("business_terminal_mismatch")
        analysis = _read_row(db / "analysis.db",
                             "SELECT ts, status FROM analysis_runs "
                             "WHERE cycle_id=?", cycle_id)
        if analysis is None or analysis.get("status") != "ok":
            return unknown("analysis_not_completed")
        if not collected <= thresholds.parse_cst(analysis["ts"]) <= completed:
            return unknown("analysis_time_order_invalid")
        seconds = (completed - start).total_seconds()
        if seconds != int(seconds):
            return unknown("business_timestamp_not_whole_seconds")
        return {
            **result, "status": "known", "reason": "ok",
            "started_at_cst": start.isoformat(),
            "completed_at_cst": completed.isoformat(),
            "elapsed_seconds": int(seconds),
            "source": "live_stage_receipt+trade_cycles.raw.business_terminal",
        }
    except (OSError, sqlite3.Error):
        return unknown("duration_evidence_unreadable")
    except (KeyError, TypeError, ValueError, OverflowError):
        return unknown("duration_evidence_invalid")


def unknown_message(reason: str) -> str:
    labels = {
        "live_receipt_missing": "业务完成凭证缺失",
        "live_receipt_not_successful": "本轮业务未确认成功完成",
        "business_terminal_mismatch": "业务完成凭证与账本不一致",
        "duration_evidence_unreadable": "计时证据暂时无法读取",
    }
    return "本轮耗时未知：" + labels.get(reason, "计时证据不完整或不一致")
