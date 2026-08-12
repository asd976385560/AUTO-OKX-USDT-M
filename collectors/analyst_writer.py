# -*- coding: utf-8 -*-
"""V2.0 analysis 落库 writer（统一 live 分析阶段写 analysis.db 的唯一通道）。

接收统一 live Agent（或回滚 analyst）的回执 JSON，验证后写进 analysis.db 的两张表。
红线「写库必走 writer」：Agent 严禁手写 INSERT，强制走本脚本。

输入两种（**优先 --input-file**）：
  - `--input-file <path>`：读 UTF-8 文件（strict 解码）。**推荐**——彻底绕开 `echo|PowerShell`
    管道的 cp936/GBK 坏码（2026-07-09 简报乱码根因：约 1/4 轮中文经 echo|pipe 坏成 �）。
  - `--stdin`（缺省）：读 stdin（历史 echo|管道路径，PowerShell 下中文会 GBK 坏）。
  两路均设**坏码哨兵**：解码后含 ≥3 个 U+FFFD(`�`) 即拒写（rc=1）防污染 analysis.db+经验库+简报。

输入 JSON 结构：
    {
      "cycle_id": "2026-06-18T14:00",
      "ts": "2026-06-18 14:05:30",   -- 完成时刻 UTC+8（协议必填；存库时以 writer 落库时刻覆盖）
      "mode": "full",
      "regime": "risk_on",            -- 'risk_on'|'risk_off'|'range'|...
      "regime_stale": 0,              -- 0=新鲜, 1=carry-forward
      "market_summary": { ... },      -- 5段结构化 JSON dict
      "missing_sources": null,        -- ['x_search',...] 或 null
      "decision_protocol": "decision_card_v1",
      "signals": [                    -- 0..n 行
        {
          "symbol": "BTC-USDT-SWAP",
          "action": "hold",            -- 'open_long'|'open_short'|'hold'|'close'|'wait'
          "side": null,                -- open_long=long/open_short=short/hold|wait=null/close=long|short
          "entry_hint": null,
          "stop_hint": null,
          "tp_hint": null,
          "reasoning": "...",
          "decision_card": {
            "direction_evidence": [...],
            "opposing_evidence": [...],
            "execution_conditions": {...},
            "invalidation_point": {...},
            "risk_reward": {...},
            "portfolio_impact": {...},
            "historical_experience": {
              "matched_wins": [], "matched_losses": [],
              "missed_opportunities": [],
              "usage": "partial", "reason": "..."
            },
            "agent_judgement": "...",
            "reference_overrides": []
          },
          "raw": {...}                 -- writer 统一封装为对象 JSON
        }
      ],
      "raw": "{...完整原始报告 JSON...}",
      "status": "ok"                  -- 必填：'ok'|'skipped'|'stale'|'error'
    }

- `decision_protocol=decision_card_v1`、`mode=full`、`status` 均为必填，缺失或未知即拒写
- `status=ok`：market_summary 必须包含 5 个结构段，signals 必须是 list
- `status=skipped|stale|error`：signals 必须为空；regime/market_summary 可为空
- `signals=[]`：合法（无机会时给 trader 全 hold 信号）
- `missing_sources=null` / `missing_sources=[]`：等价，无缺源
- action/side 必须严格对应；未知 status/action 或组合冲突均拒写，下游不得猜测
- CLI 与直接调用 `write_analysis()` 使用同一套规范化和校验，不能绕过当前回执契约

输出（stdout）：
    {"ok": true, "cycle_id": "...", "signals_written": N}
    exit 0 = 成功

失败（exit 非 0）：
    stderr: 错误原因
    stdout: {"ok": false, "error": "..."}（输出恒为 JSON）
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sys
from typing import Any, Optional
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
_TS_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)


def normalize_ts(ts: str) -> str:
    """归一化时间为 UTC+8 纯字符串 'YYYY-MM-DD HH:MM:SS'。

    兼容输入：
      - ISO8601 带毫秒 + 时区（'2026-06-19T15:34:25.719189+08:00'）
      - ISO8601 UTC-Z（'2026-06-19T07:34:25Z'）
      - ISO8601 无时区（'2026-06-19T15:34:25'）
      - 已是纯字符串（'2026-06-19 15:34:25'）
    """
    if not ts:
        return ts
    s = ts.strip()
    # 已经是纯字符串（UTC+8 'YYYY-MM-DD HH:MM:SS'）
    if "T" not in s and " " in s and len(s) >= 19 and s[10] == " ":
        return s[:19]
    # 纯字符串但缺秒（'YYYY-MM-DD HH:MM'）→ 补 ':00'（防 bookkeeping_health strptime 崩）
    if "T" not in s and " " in s and len(s) == 16 and s[10] == " " and s[13] == ":":
        return s + ":00"
    m = _TS_ISO_RE.match(s)
    if not m:
        return s
    date, time, tz = m.groups()
    if tz in (None, ""):
        # 无时区：按 UTC+8 处理
        dt = datetime.fromisoformat(f"{date}T{time}+08:00")
    elif tz == "Z":
        dt = datetime.fromisoformat(f"{date}T{time}+00:00").astimezone(CST)
    else:
        # +08:00 / +0800
        norm_tz = tz if ":" in tz else tz[:3] + ":" + tz[3:]
        dt = datetime.fromisoformat(f"{date}T{time}{norm_tz}").astimezone(CST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
import sqlite3
from pathlib import Path

_PROJECT_ROOT = Path(
    os.environ.get("OKX_ROOT") or Path(__file__).resolve().parents[1]
).resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from core.decision_card import PROTOCOL as DECISION_PROTOCOL  # noqa: E402
from core.decision_card import (  # noqa: E402
    validate_card,
    validate_multitimeframe_analysis,
)
from core.ev_calculator import build_ev_check  # noqa: E402
from core.experience_contract import validate_contract as validate_experience_contract  # noqa: E402
from core.multitimeframe_gate import check_multitimeframe_readiness  # noqa: E402

# HANDOFF-4A（2026-07-16）：CLI 落库成功后 detached 拍一次 dispatcher（事件驱动派发）。
# 守卫导入：任何异常→None→静默禁用——writer 落库优先，nudge 永不致命。守护闸详见模块 docstring。
try:
    if str(_PROJECT_ROOT / "collectors") not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT / "collectors"))
    import _dispatch_nudge as _nudge_mod
except Exception:  # noqa: BLE001
    _nudge_mod = None

_PRODUCTION_DB_ROOT = (_PROJECT_ROOT / "db").resolve()


def _runtime_db_root(explicit: str | Path | None = None) -> Path:
    value = explicit if explicit is not None else os.environ.get("OKX_DB_ROOT")
    return Path(value or _PRODUCTION_DB_ROOT).expanduser().resolve()


DB_PATH = _runtime_db_root() / "analysis.db"
VALIDATION_STATE_DIR = Path(os.environ.get(
    "OKX_ANALYSIS_VALIDATION_STATE_DIR",
    str(_PROJECT_ROOT / "logs" / "analysis-validation"),
))
MAX_VALIDATION_FAILURES = 2


def connect(
    write: bool = False,
    db_path: Path | None = None,
) -> sqlite3.Connection:
    target = Path(db_path or DB_PATH)
    uri = f"file:{target}" + ("?mode=ro" if not write else "")
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    if write:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=5000;")
    return con


def _validation_state_path(cycle_id: Any) -> Optional[Path]:
    cycle = str(cycle_id or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:(?:00|15|30|45)", cycle):
        return None
    return VALIDATION_STATE_DIR / f"analysis-{cycle.replace(':', '-')}.json"


def _load_validation_state(cycle_id: Any) -> dict:
    path = _validation_state_path(cycle_id)
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        # Corrupt guard state must fail closed for this cycle; otherwise an
        # agent could escape a spent retry budget through a torn file.
        return {
            "schema_version": 1,
            "cycle_id": str(cycle_id or ""),
            "failed_attempts": MAX_VALIDATION_FAILURES,
            "blocked": True,
            "state_error": "unreadable_validation_state",
        }
    return data if isinstance(data, dict) else {
        "schema_version": 1,
        "cycle_id": str(cycle_id or ""),
        "failed_attempts": MAX_VALIDATION_FAILURES,
        "blocked": True,
        "state_error": "invalid_validation_state",
    }


def _save_validation_state(cycle_id: Any, state: dict) -> dict:
    path = _validation_state_path(cycle_id)
    if path is None:
        return state
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(state, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return state


def _validation_guard(cycle_id: Any, payload_hash: str,
                      *, validate_only: bool) -> tuple[Optional[str], dict]:
    """Enforce two failed validations and same-payload formal submission.

    A missing state remains compatible with the legacy/manual writer CLI.
    Once a cycle uses ``--validate-only``, however, its retry budget and the
    exact validated payload become deterministic rather than model-enforced.
    """
    state = _load_validation_state(cycle_id)
    if not state:
        return None, state
    if state.get("blocked") is True:
        return (
            "analysis validation budget exhausted (2/2); 本 cycle 已 fail-closed，"
            "禁止继续重写或正式写库",
            state,
        )
    validated_hash = str(state.get("validated_payload_sha256") or "")
    if validate_only and validated_hash and validated_hash != payload_hash:
        return (
            "本 cycle 已有通过 validate-only 的回执；只允许用同一文件正式写入，"
            "禁止通过后再次改写",
            state,
        )
    if not validate_only and validated_hash != payload_hash:
        return (
            "正式 writer 输入与 validate-only 通过的文件不一致，或失败后尚未通过预检",
            state,
        )
    return None, state


def _record_validation_failure(cycle_id: Any, payload_hash: str,
                               errors: list[str]) -> dict:
    state = _load_validation_state(cycle_id)
    failed = min(
        int(state.get("failed_attempts") or 0) + 1,
        MAX_VALIDATION_FAILURES,
    )
    state = {
        "schema_version": 1,
        "cycle_id": str(cycle_id or ""),
        "failed_attempts": failed,
        "max_failed_attempts": MAX_VALIDATION_FAILURES,
        "blocked": failed >= MAX_VALIDATION_FAILURES,
        "last_payload_sha256": payload_hash,
        "last_error_sha256": hashlib.sha256(
            "\n".join(errors).encode("utf-8")
        ).hexdigest(),
        "updated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    return _save_validation_state(cycle_id, state)


def _record_validation_success(cycle_id: Any, payload_hash: str) -> dict:
    prior = _load_validation_state(cycle_id)
    state = {
        "schema_version": 1,
        "cycle_id": str(cycle_id or ""),
        "failed_attempts": int(prior.get("failed_attempts") or 0),
        "max_failed_attempts": MAX_VALIDATION_FAILURES,
        "blocked": False,
        "validated_payload_sha256": payload_hash,
        "validated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    return _save_validation_state(cycle_id, state)


def _record_validation_written(cycle_id: Any) -> None:
    state = _load_validation_state(cycle_id)
    if not state:
        return
    state["written_at"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    _save_validation_state(cycle_id, state)


# ---------------------------------------------------------------------------
# 回执协议验证。注意 decision_protocol 是回执键，不是 analysis_runs 表列；
# 完整规范化回执会持久化到 analysis_runs.raw。
# ---------------------------------------------------------------------------
REQUIRED_RECEIPT_KEYS = ["cycle_id", "ts", "mode", "status", "decision_protocol"]

# missing_sources 标签归一（2026-08-05）。该字段由 Agent 自由文本写入，同一含义
# 出现过多种拼写（实测 dxy_zone_stale_carryforward 9 轮 / dxy_zone_stale_carry_forward 3 轮），
# 会让任何按 key 聚合的统计把同一件事算成两件。
#
# 只做**已知别名**的确定性映射，不改写未登记的标签——这是数据卫生，不是给判断设闸：
# Agent 报什么缺源仍完全由它自己决定，这里只统一同义标签的写法。
# 规范形：小写 + snake_case。新别名出现时在此登记，不要在读取侧各自 hack。
MISSING_SOURCE_ALIASES = {
    "dxy_zone_stale_carryforward": "dxy_zone_stale_carry_forward",
    "dxy_zone_stale_carry-forward": "dxy_zone_stale_carry_forward",
    "dxy_stale_carryforward": "dxy_zone_stale_carry_forward",
}


def normalize_missing_sources(value):
    """把 missing_sources 里的已知别名归一到规范形（保序去重）。

    非 list 原样返回（None/[] 语义等价，见文件头）；元素非 str 的保持原值不动。
    """
    if not isinstance(value, list):
        return value
    out, seen = [], set()
    for item in value:
        if isinstance(item, str):
            key = item.strip()
            item = MISSING_SOURCE_ALIASES.get(key, MISSING_SOURCE_ALIASES.get(key.lower(), key))
        marker = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out

SIGNAL_COLS = [
    "symbol", "dim1", "dim2", "dim3", "dim4", "dim5",
    "total", "action", "side", "confidence",
    "entry_hint", "stop_hint", "tp_hint", "reasoning", "decision_card", "raw",
]
ALLOWED_RUN_STATUSES = {"ok", "skipped", "stale", "error"}
ALLOWED_SIGNAL_ACTIONS = {
    "open_long",
    "open_short",
    "hold",
    "close",
    "wait",
}
SIGNAL_RAW_SCHEMA_VERSION = 1


def signal_raw_object(signal: dict, symbol: str) -> dict:
    """Return the one persistent object schema for ``analysis_signals.raw``.

    The complete original receipt remains in ``analysis_runs.raw``.  This
    column-level envelope makes every signal independently queryable even when
    an agent supplied a short string or omitted ``raw`` entirely.
    """
    original = signal.get("raw")
    if isinstance(original, dict):
        input_kind = "object"
    elif original is None:
        input_kind = "missing"
    else:
        input_kind = type(original).__name__
    return {
        "schema_version": SIGNAL_RAW_SCHEMA_VERSION,
        "source": "analyst_writer",
        "input_kind": input_kind,
        "payload": original,
        "canonical_signal": {
            "symbol": symbol,
            "action": signal.get("action"),
            "side": signal.get("side"),
            "entry_hint": signal.get("entry_hint"),
            "stop_hint": signal.get("stop_hint"),
            "tp_hint": signal.get("tp_hint"),
            "reasoning": signal.get("reasoning"),
        },
    }


def normalize_receipt(data: dict) -> dict:
    """Return a canonical copy used by both validation and persistence.

    Current receipts use lower-case machine labels.  Canonicalizing once keeps
    the validator and dispatcher from disagreeing about values such as
    ``OK``/``OPEN_LONG`` that would otherwise validate but be stored verbatim.
    """
    normalized = dict(data)
    for key in ("mode", "status", "decision_protocol"):
        value = normalized.get(key)
        if value is not None:
            normalized[key] = str(value).strip().lower()
    signals = normalized.get("signals")
    if isinstance(signals, list):
        normalized_signals = []
        for signal in signals:
            if not isinstance(signal, dict):
                normalized_signals.append(signal)
                continue
            item = dict(signal)
            if item.get("action") is not None:
                item["action"] = str(item["action"]).strip().lower()
            raw_side = item.get("side")
            item["side"] = (
                None
                if raw_side is None or not str(raw_side).strip()
                else str(raw_side).strip().lower()
            )
            normalized_signals.append(item)
        normalized["signals"] = normalized_signals
    return normalized


# 评分字段仅用于兼容格式校验；decision_card_v1 不要求或消费评分。
SIGNAL_SCORE_COLS = ["dim1", "dim2", "dim3", "dim4", "dim5", "total", "confidence"]
MARKET_SUMMARY_SECTIONS = ["macro", "news", "tech", "sentiment", "quant"]


def normalize_symbol(sym: str) -> str:
    """symbol 规范化为 <BASE>-USDT-SWAP 全称（治裸 'HBAR' 与 'HBAR-USDT-SWAP' 并存坏 join）。"""
    s = str(sym).strip().upper()
    if s.endswith("-USDT-SWAP"):
        return s
    if s.endswith("-USDT"):
        return s + "-SWAP"
    return s + "-USDT-SWAP"


def _validate_sample_membership(history: Any, contract: Any) -> list[str]:
    """Wave1 序8：卡内样本行 ⊆ 契约 sample_ids（按 scope 分桶）。

    matched_wins/matched_losses 是同标的邻居（same_symbol_similar 池的截断
    样例）、cross_symbol_wins/losses 属 cross 池；每行必须带工具输出的
    experience_id 且落在对应 scope 的 sample_ids 内。空数组合法。
    """
    if not isinstance(history, dict) or not isinstance(contract, dict):
        return []
    summaries = contract.get("summaries")
    if not isinstance(summaries, dict):
        return []

    def _ids(scope_key: str) -> set:
        summary = summaries.get(scope_key)
        ids = summary.get("sample_ids") if isinstance(summary, dict) else None
        return set(ids) if isinstance(ids, list) else set()

    same_ids = _ids("same_symbol_similar") | _ids("exact_setup")
    cross_ids = _ids("cross_symbol_similar")
    errors: list[str] = []
    buckets = (
        ("matched_wins", same_ids, "same_symbol"),
        ("matched_losses", same_ids, "same_symbol"),
        ("cross_symbol_wins", cross_ids, "cross_symbol"),
        ("cross_symbol_losses", cross_ids, "cross_symbol"),
    )
    for field, allowed, label in buckets:
        rows = history.get(field)
        if not isinstance(rows, list):
            continue
        for j, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"{field}[{j}] 必须是 dict")
                continue
            eid = row.get("experience_id")
            if not isinstance(eid, int) or isinstance(eid, bool):
                errors.append(
                    f"{field}[{j}] 缺 experience_id（样本行只准原样复制 "
                    "find_similar 输出，禁止手写/改写样本）")
            elif eid not in allowed:
                errors.append(
                    f"{field}[{j}].experience_id={eid} 不在契约 {label} "
                    "sample_ids 内（样本与计数必须同一次工具输出）")
    return errors


_HISTORY_NUMERIC_PROSE = (
    re.compile(r"(?i)\bn\s*=\s*\d+"),
    re.compile(r"(?i)\bwr\s*[=:]?\s*\d"),
    re.compile(r"(?i)\b\d+\s*w\s*/?\s*\d+\s*l\b"),
    re.compile(r"胜率\s*[=:：]?\s*\d"),
    re.compile(r"\d+\s*胜\s*\d+\s*负"),
)


def _validate_history_numeric_prose(card: Any) -> list[str]:
    """Historical counts may appear only in writer-injected scope_counts."""
    if not isinstance(card, dict):
        return []
    history = card.get("historical_experience")
    fields: list[tuple[str, Any]] = []
    if isinstance(history, dict):
        fields.append(("historical_experience.reason", history.get("reason")))
    fields.append(("agent_judgement", card.get("agent_judgement")))
    for name in ("direction_evidence", "opposing_evidence"):
        value = card.get(name)
        if isinstance(value, list):
            fields.extend((f"{name}[{i}]", item) for i, item in enumerate(value))
    errors = []
    for field, value in fields:
        text = str(value or "")
        if any(pattern.search(text) for pattern in _HISTORY_NUMERIC_PROSE):
            errors.append(
                f"{field} 含手写历史计数/胜率；数字只准引用 writer 注入的 "
                "historical_experience.scope_counts"
            )
    return errors


def _validate_setup_contract(card: Any, contract: Any) -> list[str]:
    if not isinstance(card, dict) or not isinstance(contract, dict):
        return []
    rr = card.get("risk_reward")
    query = contract.get("query")
    if not isinstance(rr, dict) or not isinstance(query, dict):
        return []
    try:
        entry = float(rr["entry"])
        stop = float(rr["stop"])
        target = float(rr["target"])
        expected = {
            "stop_distance_pct": round(abs(entry - stop) / entry, 8),
            "planned_rr": round(abs(target - entry) / abs(entry - stop), 8),
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return []  # EV calculator owns the primary geometry error.
    expected["setup_hash"] = hashlib.sha256(json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    actual = query.get("setup")
    if not isinstance(actual, dict):
        return [
            "evidence_contract.query.setup 缺失；open 检索必须把本卡止损距离与 RR 冻结进契约"
        ]
    errors = []
    for key in ("stop_distance_pct", "planned_rr"):
        try:
            same = abs(float(actual.get(key)) - float(expected[key])) <= 1e-8
        except (TypeError, ValueError):
            same = False
        if not same:
            errors.append(
                f"evidence_contract.query.setup.{key} 与本卡 risk_reward 不一致"
            )
    if actual.get("setup_hash") != expected["setup_hash"]:
        errors.append("evidence_contract.query.setup_hash 与本卡计划参数不一致")
    return errors


def _evidence_delta(con, cycle_id: str, symbol: str, side: Any,
                    close_contract: Any) -> dict:
    """close 卡证据相对最近一次 open 基线的确定性差分（Wave1 序8）。

    基线 = analysis_signals 中同 symbol、对应 open_* 动作、cycle 早于本轮、
    卡内含 v2 契约（有 sample_ids）的最近一行。找不到 → baseline_unavailable
    如实标注（旧卡无 ids，不伪造差分）。差分按 scope 给 new_ids/removed_ids/
    count_delta——"3 小时前 direct n=0、现在 0W/5L"这类翻转从此必须能指出
    新增了哪几条样本。
    """
    if not isinstance(close_contract, dict):
        return {"status": "no_close_contract"}
    close_summaries = close_contract.get("summaries")
    if not isinstance(close_summaries, dict):
        return {"status": "no_close_contract"}
    open_action = (
        "open_long" if str(side or "").lower() == "long" else "open_short")
    try:
        row = con.execute(
            "SELECT cycle_id, decision_card FROM analysis_signals "
            "WHERE symbol=? AND action=? AND cycle_id<? "
            "ORDER BY cycle_id DESC LIMIT 1",
            (symbol, open_action, cycle_id)).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return {"status": "baseline_unavailable",
                "reason": f"无更早的 {open_action} 卡"}
    try:
        base_card = json.loads(row[1]) if isinstance(row[1], str) else row[1]
    except (TypeError, json.JSONDecodeError):
        base_card = None
    base_contract = None
    if isinstance(base_card, dict):
        base_history = base_card.get("historical_experience")
        if isinstance(base_history, dict):
            base_contract = base_history.get("evidence_contract")
    base_summaries = (
        base_contract.get("summaries")
        if isinstance(base_contract, dict) else None
    )
    if not isinstance(base_summaries, dict):
        return {"status": "baseline_unavailable",
                "reason": f"{row[0]} 的 open 卡无契约", "baseline_cycle": row[0]}
    per_scope = {}
    legacy = False
    for key in ("exact_setup", "same_symbol_similar", "cross_symbol_similar"):
        c_sum = close_summaries.get(key)
        b_sum = base_summaries.get(key)
        c_ids = (c_sum or {}).get("sample_ids") if isinstance(c_sum, dict) \
            else None
        b_ids = (b_sum or {}).get("sample_ids") if isinstance(b_sum, dict) \
            else None
        if not isinstance(c_ids, list) or not isinstance(b_ids, list):
            legacy = True
            continue
        c_set, b_set = set(c_ids), set(b_ids)
        per_scope[key] = {
            "new_ids": sorted(c_set - b_set),
            "removed_ids": sorted(b_set - c_set),
            "count_delta": len(c_set) - len(b_set),
        }
    if legacy and not per_scope:
        return {"status": "baseline_unavailable",
                "reason": "open 基线为 v1 契约（无 sample_ids）",
                "baseline_cycle": row[0]}
    return {"status": "ok", "baseline_cycle": row[0], "per_scope": per_scope}


def _regime_scope_block(
    symbol: str,
    cycle_regime: Any,
    cycle_id: str,
    db_root: str | Path | None = None,
) -> Optional[dict]:
    """Wave2 序11：regime 口径拆分（btc_crypto_regime vs instrument_regime）。

    全局 regime 是 BTC/crypto 口径；对股票/商品/ETF 型永续只算 context——
    DOT 事故同期，SNDK 空单曾拿 `regime=range` 当主论据，实为口径错配。
    instrument_regime 按标的自身 4H MA20/MA50 结构确定性判定；K 线不足 =
    not_available（未知不冒充 range）。派生失败返回 None（注入跳过，不阻断）。
    """
    try:
        from core.instrument_context import build_instrument_context
        resolved_root = (
            Path(db_root).expanduser().resolve()
            if db_root is not None else DB_PATH.parent.resolve()
        )
        return build_instrument_context(
            symbol, cycle_regime, cycle_id, resolved_root)
    except Exception:  # noqa: BLE001  注入失败跳过，不阻断落库
        return None


def _scope_counts_from_contract(contract: Any) -> Optional[dict]:
    """从已验证契约派生 canonical 计数块（display 真源，禁模型手写）。"""
    if not isinstance(contract, dict):
        return None
    summaries = contract.get("summaries")
    if not isinstance(summaries, dict):
        return None
    out = {}
    for key in ("exact_setup", "same_symbol_similar", "cross_symbol_similar"):
        summary = summaries.get(key)
        if isinstance(summary, dict):
            out[key] = {
                "n": summary.get("n"),
                "wins": summary.get("wins"),
                "losses": summary.get("losses"),
            }
    return out or None


def validate_receipt(
    data: dict,
    db_root: str | Path | None = None,
) -> list[str]:
    """返回错误列表；空=验证通过。"""
    if not isinstance(data, dict):
        return ["回执必须是 dict"]
    data = normalize_receipt(data)
    validation_root = (
        Path(db_root).expanduser().resolve()
        if db_root is not None else DB_PATH.parent.resolve()
    )
    errors = []
    protocol = data.get("decision_protocol")
    if protocol != DECISION_PROTOCOL:
        errors.append(
            f"decision_protocol 必须是 {DECISION_PROTOCOL}，got: {protocol!r}"
        )
    card_mode = protocol == DECISION_PROTOCOL
    for col in REQUIRED_RECEIPT_KEYS:
        if col not in data or data[col] is None:
            errors.append(f"缺少必填字段: {col}")
    if data.get("mode") != "full":
        errors.append(f"mode 必须是 'full'，got: {data.get('mode')!r}")
    status = str(data.get("status") or "").strip().lower()
    if status not in ALLOWED_RUN_STATUSES:
        errors.append(
            f"status 不支持: {status!r}（仅允许 "
            f"{'|'.join(sorted(ALLOWED_RUN_STATUSES))}）"
        )
    if status == "ok" and not isinstance(data.get("market_summary"), dict):
        errors.append("status=ok 时 market_summary 必须是 dict")
    if "market_summary" in data and data["market_summary"] is not None:
        if not isinstance(data["market_summary"], dict):
            errors.append("market_summary 必须是 dict 或 null")
        elif status == "ok":
            for section in MARKET_SUMMARY_SECTIONS:
                if section not in data["market_summary"]:
                    errors.append(f"market_summary 缺少结构段: {section}")
                elif not isinstance(data["market_summary"][section], dict):
                    errors.append(f"market_summary.{section} 必须是 dict")
    if status == "ok" and not isinstance(data.get("signals"), list):
        errors.append("status=ok 时 signals 必须是 list")
    if (
        status in ALLOWED_RUN_STATUSES - {"ok"}
        and data.get("signals") not in (None, [])
    ):
        errors.append(f"status={status} 时 signals 必须为空")
    if "signals" in data and data["signals"] is not None:
        if not isinstance(data["signals"], list):
            errors.append("signals 必须是 list 或 null")
        else:
            for i, sig in enumerate(data["signals"]):
                if not isinstance(sig, dict):
                    errors.append(f"signals[{i}] 必须是 dict")
                    continue
                if "symbol" not in sig or not sig["symbol"]:
                    errors.append(f"signals[{i}] 缺少 symbol")
                if "action" not in sig:
                    errors.append(f"signals[{i}] 缺少 action")
                    action = ""
                else:
                    action = str(sig.get("action") or "").strip().lower()
                    if action not in ALLOWED_SIGNAL_ACTIONS:
                        errors.append(
                            f"signals[{i}].action 不支持: {action!r}")
                raw_side = sig.get("side")
                side = (
                    None
                    if raw_side is None or not str(raw_side).strip()
                    else str(raw_side).strip().lower()
                )
                # hold = 持有既有仓位，本身无方向可言 → 必须 null。
                # wait = 看到方向但本轮不入场，方向正是可检验的信息：
                #   scripts/missed_opps_writer.py 靠它回填「不开仓」的机会成本对照组。
                #   2026-07-29 该字段被一并收紧为 null 后，对照组静默停写两天
                #   （missed_opportunities 最后一条 = 最后一条带方向的 wait/hold）。
                #   故 wait 恢复为「可选方向」：能判方向就填，纯观望留 null。
                expected_sides = {
                    "open_long": {"long"},
                    "open_short": {"short"},
                    "hold": {None},
                    "wait": {None, "long", "short"},
                    "close": {"long", "short"},
                }.get(action)
                if expected_sides is not None and side not in expected_sides:
                    rendered = "null" if side is None else repr(side)
                    allowed = ",".join(
                        "null" if value is None else value
                        for value in sorted(
                            expected_sides,
                            key=lambda value: "" if value is None else value,
                        )
                    )
                    errors.append(
                        f"signals[{i}].action={action} 与 side={rendered} "
                        f"不一致（允许 {allowed}）")
                if card_mode:
                    errors.extend(
                        validate_card(sig.get("decision_card"), f"signals[{i}].decision_card")
                    )
                    if action in {"open_long", "open_short"}:
                        card = sig.get("decision_card")
                        canonical_symbol = normalize_symbol(
                            str(sig.get("symbol") or ""))
                        multitimeframe_errors = validate_multitimeframe_analysis(
                            card,
                            f"signals[{i}].decision_card",
                            expected_cycle=str(data.get("cycle_id") or ""),
                            expected_side=(
                                "long" if action == "open_long" else "short"
                            ),
                            expected_symbol=canonical_symbol,
                        )
                        errors.extend(multitimeframe_errors)
                        # 目标2证据真值闸：自洽 hash 只能证明卡片未被意外改写，
                        # 不能证明卡片值真的来自 market.db。只有结构/hash/身份先验
                        # 全部通过后，才只读重建同 cycle 的 exact 已闭合三周期契约并
                        # 逐字段比较。这样伪造后重新 seal、旧周期证据或库中缺口在
                        # analysis 入库前即失败，而不是等到 executor 才拦下。
                        if not multitimeframe_errors:
                            actual_multitimeframe = (
                                check_multitimeframe_readiness(
                                    validation_root,
                                    canonical_symbol,
                                    str(data.get("cycle_id") or ""),
                                )
                            )
                            mtf_path = (
                                f"signals[{i}].decision_card."
                                "multitimeframe_analysis.evidence_contract"
                            )
                            if not actual_multitimeframe.get("ready"):
                                gaps = [
                                    f"{row.get('timeframe')}:"
                                    f"{row.get('classification')}"
                                    for row in actual_multitimeframe.get(
                                        "timeframes", [])
                                    if not row.get("ready")
                                ]
                                errors.append(
                                    f"{mtf_path}: market.db 本 cycle 的 "
                                    "15m/1H/4H 未全部就绪: "
                                    f"{','.join(gaps) or actual_multitimeframe.get('error') or 'unknown'}"
                                )
                            else:
                                supplied_multitimeframe = (
                                    card.get("multitimeframe_analysis", {}).get(
                                        "evidence_contract")
                                    if isinstance(card, dict) else None
                                )
                                if supplied_multitimeframe != (
                                    actual_multitimeframe.get(
                                        "evidence_contract")
                                ):
                                    errors.append(
                                        f"{mtf_path}: 与 market.db 本 cycle "
                                        "exact 已闭合真值不一致"
                                    )
                        history = (
                            card.get("historical_experience")
                            if isinstance(card, dict) else None
                        )
                        contract = (
                            history.get("evidence_contract")
                            if isinstance(history, dict) else None
                        )
                        cycle_id = str(data.get("cycle_id") or "")
                        cycle_as_of = (
                            cycle_id.replace("T", " ") + ":00"
                            if re.fullmatch(
                                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", cycle_id
                            ) else None
                        )
                        contract_errors = validate_experience_contract(
                            contract,
                            expected_symbol=str(sig.get("symbol") or ""),
                            expected_side=(
                                "long" if action == "open_long" else "short"
                            ),
                            expected_regime=str(data.get("regime") or ""),
                            expected_action="open",
                            expected_profile="live",
                            expected_as_of=cycle_as_of,
                        )
                        errors.extend(
                            f"signals[{i}].decision_card.historical_experience."
                            f"evidence_contract: {item}"
                            for item in contract_errors
                        )
                        # Wave1 序5：RR/EV 确定性重算。算术不一致（rr 字段 vs
                        # entry/stop/target 几何、方向几何非法、数字缺失）= 拒写；
                        # 负 EV 不拒单但必须带结构化 ev_override（边界表 #1/#4）。
                        _, ev_errors = build_ev_check(
                            card, "long" if action == "open_long" else "short")
                        errors.extend(
                            f"signals[{i}].decision_card.ev_check: {item}"
                            for item in ev_errors
                        )
                        # Wave1 序8：matched/cross 样本行必须携带契约内的
                        # experience_id——计数与样本身份同源，杜绝 HYPE 型
                        # "reason 写 direct n=0、卡内又摆同标的亏损"的手写口径。
                        errors.extend(
                            f"signals[{i}].decision_card.historical_experience"
                            f": {item}"
                            for item in _validate_sample_membership(
                                history, contract)
                        )
                        errors.extend(
                            f"signals[{i}].decision_card: {item}"
                            for item in _validate_history_numeric_prose(card)
                        )
                        errors.extend(
                            f"signals[{i}].decision_card: {item}"
                            for item in _validate_setup_contract(card, contract)
                        )
                        expected_context = _regime_scope_block(
                            str(sig.get("symbol") or ""), data.get("regime"),
                            str(data.get("cycle_id") or ""), validation_root)
                        actual_context = (
                            (contract.get("query") or {}).get("instrument_context")
                            if isinstance(contract, dict) else None
                        )
                        if expected_context != actual_context:
                            errors.append(
                                f"signals[{i}].decision_card.historical_experience."
                                "evidence_contract.query.instrument_context 与本轮 as-of "
                                "标的口径不一致"
                            )
                else:
                    missing_scores = [c for c in SIGNAL_SCORE_COLS if sig.get(c) is None]
                    if missing_scores:
                        errors.append(
                            f"signals[{i}]({sig.get('symbol')}) 兼容格式打分字段缺失: "
                            f"{','.join(missing_scores)}"
                        )
                # 兼容字段若存在仍做域校验；decision_card_v1 允许全部为 NULL。
                def _num(v):
                    return isinstance(v, (int, float)) and not isinstance(v, bool)
                for c in ("dim1", "dim2", "dim3", "dim4", "dim5"):
                    v = sig.get(c)
                    if v is not None and (not _num(v) or not (1 <= v <= 100)):
                        errors.append(
                            f"signals[{i}]({sig.get('symbol')}) {c}={v!r} 越域（须为 1-100 数值）")
                v = sig.get("total")
                if v is not None and (not _num(v) or not (0 <= v <= 100)):
                    errors.append(
                        f"signals[{i}]({sig.get('symbol')}) total={v!r} 越域（须为 0-100 数值）")
                v = sig.get("confidence")
                if v is not None and (not _num(v) or not (0.0 <= v <= 1.0)):
                    errors.append(
                        f"signals[{i}]({sig.get('symbol')}) confidence={v!r} 越域（须为 0.0-1.0 数值）")
                for c in ("entry_hint", "stop_hint", "tp_hint"):
                    v = sig.get(c)
                    if v is not None and (
                        not _num(v)
                        or not math.isfinite(float(v))
                        or float(v) <= 0
                    ):
                        errors.append(
                            f"signals[{i}]({sig.get('symbol')}) {c}={v!r} "
                            "须为正有限数值或 null")
                if action in {"hold", "wait"}:
                    non_null_hints = [
                        c for c in ("entry_hint", "stop_hint", "tp_hint")
                        if sig.get(c) is not None
                    ]
                    if non_null_hints:
                        errors.append(
                            f"signals[{i}].action={action} 时价格提示必须全为 null，"
                            f"got: {','.join(non_null_hints)}")
    return errors


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def write_analysis(data: dict, db_path: Path | None = None) -> dict:
    """写 analysis_runs + analysis_signals；成功返回 ok，失败返回错误 dict。

    闩锁规则（防 race condition）：
    - 若 analysis_runs 已有同 cycle_id 且 status='ok' 的行 → 拒绝覆盖，返回 already_exists。
    - status='skipped'/'stale'/'error' 的旧行允许覆盖（失败重写是合法的）。
    - 写入用 INSERT OR REPLACE + 事务，保证原子性。
    """
    if not isinstance(data, dict):
        return {"ok": False, "error": "回执必须是 dict"}
    data = normalize_receipt(data)
    target = Path(db_path or DB_PATH)
    errors = validate_receipt(data, db_root=target.parent)
    if errors:
        return {"ok": False, "error": "; ".join(errors)}

    cycle_id = data["cycle_id"]
    # ts 一律取 writer 落库时刻，不信 agent 自报值；原报 ts 仍在回执/raw 可溯源。
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    mode = data["mode"]
    regime = data.get("regime")
    regime_stale = data.get("regime_stale", 0)
    market_summary = data.get("market_summary")
    missing_sources = normalize_missing_sources(data.get("missing_sources"))
    reported_ts = normalize_ts(str(data.get("ts") or ""))
    status = data["status"]

    # analysis_runs.raw 的 schema 注释承诺“完整结构化报告 JSON”。历史实现却只
    # 保存调用方 data.raw 子对象，导致已验证的 decision_protocol、signals 与运行
    # 状态在持久层消失。这里保存规范化后的完整回执；调用方原 raw 仍原样保留在
    # 嵌套 ``raw`` 字段中。列 ``ts`` 继续由 writer 掌权，Agent 自报时间单列留痕。
    raw = dict(data)
    raw["missing_sources"] = missing_sources
    raw["reported_ts"] = reported_ts
    raw["writer_ts"] = ts
    raw["raw_schema_version"] = 2

    # JSON 序列化 dict 字段
    market_summary_json = json.dumps(market_summary, ensure_ascii=False) if market_summary is not None else None
    missing_json = json.dumps(missing_sources, ensure_ascii=False) if missing_sources is not None else None
    raw_json = json.dumps(raw, ensure_ascii=False)

    con = connect(write=True, db_path=target)
    try:
        # 闩锁：查是否已有 status='ok' 的行 → 有则拒绝（race condition 防护）
        existing = con.execute(
            "SELECT status, ts FROM analysis_runs WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if existing:
            existing_status = existing[0]
            existing_ts = existing[1]
            if existing_status == "ok":
                return {
                    "ok": False,
                    "error": "already_exists",
                    "cycle_id": cycle_id,
                    "detail": f"analysis_runs 已有 status=ok 行 (ts={existing_ts})，拒绝覆盖（race condition 防护）",
                }

        con.execute(
            "INSERT OR REPLACE INTO analysis_runs"
            "(cycle_id, ts, mode, regime, regime_stale, market_summary, missing_sources, raw, status)"
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (cycle_id, ts, mode, regime, regime_stale, market_summary_json, missing_json, raw_json, status),
        )

        # 分析信号：先删本 cycle 旧行，再插入新行（signals=[] 时只删不插）
        con.execute("DELETE FROM analysis_signals WHERE cycle_id=?", (cycle_id,))
        signals_written = 0
        symbols_normalized = []
        for sig in (data.get("signals") or []):
            sym_in = sig.get("symbol")
            sym = normalize_symbol(sym_in) if sym_in else sym_in
            if sym != sym_in:
                symbols_normalized.append(f"{sym_in}->{sym}")
            decision_card = sig.get("decision_card")
            # Wave1 序5：open 卡注入 canonical ev_check（writer 重算值覆盖模型
            # 手写同名块——"文字结论由字段生成"的字段真源只此一家）。validate
            # 阶段已保证无算术错误，此处 errors 恒空。
            if (isinstance(decision_card, dict)
                    and sig.get("action") in ("open_long", "open_short")):
                ev_block, ev_errs = build_ev_check(
                    decision_card,
                    "long" if sig.get("action") == "open_long" else "short")
                if not ev_errs and ev_block:
                    decision_card = {**decision_card, "ev_check": ev_block}
                try:
                    from core.news_context import build_news_context
                    decision_card = {
                        **decision_card,
                        "news_context": build_news_context(
                            target.parent, cycle_id, window_hours=6),
                    }
                except Exception as exc:  # noqa: BLE001  未知必须显式，不伪造空新闻
                    decision_card = {
                        **decision_card,
                        "news_context": {
                            "version": "news_context_v1",
                            "as_of": cycle_id,
                            "status": "unavailable",
                            "error": type(exc).__name__,
                        },
                    }
            # Wave2 序11：regime 口径拆分块（open/close 卡注入；派生失败跳过）
            if (isinstance(decision_card, dict)
                    and sig.get("action") in (
                        "open_long", "open_short", "close")):
                _rs = _regime_scope_block(
                    sym, data.get("regime"), cycle_id, target.parent
                )
                if _rs:
                    decision_card = {**decision_card, "regime_scope": _rs}
            # Wave1 序8：历史经验计数的 canonical 块（scope_counts 由契约派生）
            # + close 卡相对最近 open 基线的 evidence_delta（口径翻转可追溯）。
            if isinstance(decision_card, dict):
                _history = decision_card.get("historical_experience")
                _contract = (
                    _history.get("evidence_contract")
                    if isinstance(_history, dict) else None
                )
                _counts = _scope_counts_from_contract(_contract)
                if isinstance(_history, dict) and _counts:
                    _history = {**_history, "scope_counts": _counts}
                    if sig.get("action") == "close":
                        _history["evidence_delta"] = _evidence_delta(
                            con, cycle_id, sym, sig.get("side"), _contract)
                    decision_card = {
                        **decision_card, "historical_experience": _history}
            decision_card_json = (
                json.dumps(decision_card, ensure_ascii=False)
                if decision_card is not None and not isinstance(decision_card, str)
                else decision_card
            )
            signal_raw_json = json.dumps(
                signal_raw_object(sig, sym), ensure_ascii=False)
            con.execute(
                "INSERT INTO analysis_signals"
                "(cycle_id, symbol, dim1, dim2, dim3, dim4, dim5,"
                " total, action, side, confidence,"
                " entry_hint, stop_hint, tp_hint, reasoning, decision_card, raw)"
                "VALUES"
                "(?,?,?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?)",
                (
                    cycle_id,
                    sym,
                    sig.get("dim1"), sig.get("dim2"), sig.get("dim3"),
                    sig.get("dim4"), sig.get("dim5"),
                    sig.get("total"),
                    sig.get("action"),
                    sig.get("side"),
                    sig.get("confidence"),
                    sig.get("entry_hint"),
                    sig.get("stop_hint"),
                    sig.get("tp_hint"),
                    sig.get("reasoning"),
                    decision_card_json,
                    signal_raw_json,
                ),
            )
            signals_written += 1

        con.commit()
    finally:
        con.close()

    result = {"ok": True, "cycle_id": cycle_id, "signals_written": signals_written}
    if symbols_normalized:
        result["symbols_normalized"] = symbols_normalized
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    # 输入源：优先 --input-file <path>（agent 写 UTF-8 文件，**彻底绕开 echo|PowerShell 管道**
    # ——中文 JSON 经 `echo '…' | pwsh … --stdin` 时，若 echo 在 PowerShell 下按 cp936/GBK 出字节，
    # python 读原始管道字节 decode utf-8 会坏成 U+FFFD（约 1/4 轮中招、污染 analysis.db+经验库+简报，
    # 2026-07-09 owner 报乱码根因）。文件路径是 UTF-8 单一权威，同 trigger --message-file 成方。
    input_file = None
    if "--input-file" in sys.argv:
        _i = sys.argv.index("--input-file")
        if _i + 1 < len(sys.argv):
            input_file = sys.argv[_i + 1]
    db_root = None
    if "--db-root" in sys.argv:
        _i = sys.argv.index("--db-root")
        if _i + 1 >= len(sys.argv) or sys.argv[_i + 1].startswith("--"):
            print(json.dumps({"ok": False, "error": "--db-root 缺少目录参数"},
                             ensure_ascii=False))
            return 1
        db_root = sys.argv[_i + 1]
    db_path = _runtime_db_root(db_root) / "analysis.db"
    try:
        if input_file:
            with open(input_file, "rb") as _f:
                raw_bytes = _f.read()
            if not raw_bytes.strip():
                out = {"ok": False, "error": f"--input-file 为空: {input_file}"}
                print(json.dumps(out, ensure_ascii=False))
                return 1
            # 干净 UTF-8 源 → strict 解码；坏码宁可失败也不静默入库
            try:
                raw_input = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as e:
                out = {"ok": False, "error": f"--input-file 非合法 UTF-8（写文件时坏码?）: {e}"}
                print(json.dumps(out, ensure_ascii=False))
                return 1
        else:
            # 历史 stdin 路径（echo|管道）——保留兜底，但 PowerShell 下中文会 GBK 坏成 �
            raw_bytes = sys.stdin.buffer.read()
            if not raw_bytes.strip():
                out = {"ok": False, "error": "stdin 为空"}
                print(json.dumps(out, ensure_ascii=False))
                return 1
            raw_input = raw_bytes.decode("utf-8", errors="replace")
            raw_input = re.sub(r"[\udc80-\udcff]", "?", raw_input)
        # 坏码哨兵：含多个 U+FFFD 替换符 = 源在传输中被编码搅坏（多为 echo|PowerShell 管道）。
        # **拒写**防污染 analysis.db+经验库+简报（garbage-in→garbage-briefing）；让 agent 走 §6 重写
        # （改用 --input-file 即干净）。阈值 ≥3 避开偶发单个 �（news 标题等）误伤。
        _nrepl = raw_input.count("�")
        if _nrepl >= 3:
            sys.stderr.write(f"[analyst_writer][REFUSE] 输入含 {_nrepl} 个 U+FFFD 编码坏码，"
                             f"拒写防污染库——改用 --input-file 写 UTF-8 文件后重试（禁 echo|管道传中文）\n")
            out = {"ok": False, "error": f"输入含 {_nrepl} 个替换符(编码坏码)——禁 echo|管道传中文，"
                                          f"改用 --input-file 写 UTF-8 文件后重试"}
            print(json.dumps(out, ensure_ascii=False))
            return 1
        data = json.loads(raw_input)
    except json.JSONDecodeError as e:
        out = {"ok": False, "error": f"JSON 解析失败: {e}"}
        print(json.dumps(out, ensure_ascii=False))
        return 1

    validate_only = "--validate-only" in sys.argv
    payload_hash = hashlib.sha256(raw_bytes).hexdigest()
    guard_error, guard_state = _validation_guard(
        data.get("cycle_id"), payload_hash, validate_only=validate_only)
    if guard_error:
        out = {
            "ok": False,
            "error": guard_error,
            "validation_budget": guard_state,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 1

    errors = validate_receipt(data, db_root=db_path.parent)
    if errors:
        budget = (
            _record_validation_failure(data.get("cycle_id"), payload_hash, errors)
            if validate_only else guard_state
        )
        out = {
            "ok": False,
            "error": "; ".join(errors),
        }
        if budget:
            out["validation_budget"] = budget
        print(json.dumps(out, ensure_ascii=False))
        return 1

    # --validate-only=只验不写，复用上面同一套硬校验与坏码哨兵。
    # 禁另写 _preflight 脚本重复实现 writer 校验；
    # 常规轮无需预检（writer ok:true 即落库确认），确要预检只准用本 flag。
    if validate_only:
        budget = _record_validation_success(data.get("cycle_id"), payload_hash)
        print(json.dumps({"ok": True, "validate_only": True,
                          "note": "receipt valid, nothing written",
                          "validation_budget": budget}, ensure_ascii=False))
        return 0

    # 提前归一化 ts，验证 ISO8601 / 纯字符串都能进
    if "ts" in data and data["ts"]:
        data["ts"] = normalize_ts(data["ts"])

    result = write_analysis(data, db_path=db_path)
    if result.get("ok"):
        _record_validation_written(data.get("cycle_id"))
    print(json.dumps(result, ensure_ascii=False))
    # already_exists 是闩锁拒绝（race condition 防护），不是真失败
    # 返回 exit 0 让 agent 不 panic，但 result.ok=false 让 agent 知道没写进去
    if result.get("ok"):
        # HANDOFF-4A：真写入成功才 nudge（already_exists 不拍——先前那次成功写已拍过，cron 兜底）。
        # 放 print 之后：stdout JSON 契约不受任何影响；nudge 内部全 try/except 非致命。
        if _nudge_mod is not None:
            _nudge_mod.nudge("analyst_writer", db_root=db_path.parent)
        return 0
    # already_exists: exit 0（幂等成功——数据已在库里，只是不是本次写的）
    if result.get("error") == "already_exists":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
