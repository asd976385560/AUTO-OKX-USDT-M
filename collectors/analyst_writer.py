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
          "raw": "{...}"
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

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import json
import math
import re
import sys
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

if _project_path() not in sys.path:
    sys.path.insert(0, _project_path())
from core.decision_card import PROTOCOL as DECISION_PROTOCOL  # noqa: E402
from core.decision_card import validate_card  # noqa: E402

# HANDOFF-4A（2026-07-16）：CLI 落库成功后 detached 拍一次 dispatcher（事件驱动派发）。
# 守卫导入：任何异常→None→静默禁用——writer 落库优先，nudge 永不致命。守护闸详见模块 docstring。
try:
    if _project_path('collectors') not in sys.path:
        sys.path.insert(0, _project_path('collectors'))
    import _dispatch_nudge as _nudge_mod
except Exception:  # noqa: BLE001
    _nudge_mod = None

_PRODUCTION_DB_ROOT = Path(_project_path('db')).resolve()


def _runtime_db_root(explicit: str | Path | None = None) -> Path:
    """Resolve the runtime DB root without silently falling back to production."""
    value = explicit if explicit is not None else _project_os.environ.get("OKX_DB_ROOT")
    return Path(value or _PRODUCTION_DB_ROOT).expanduser().resolve()


DB_PATH = _runtime_db_root() / "analysis.db"


def connect(write: bool = False, db_path: Path | None = None) -> sqlite3.Connection:
    target = Path(db_path or DB_PATH)
    uri = f"file:{target}" + ("?mode=ro" if not write else "")
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    if write:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=5000;")
    return con


# ---------------------------------------------------------------------------
# Schema 验证（与 init_v20_dbs.py DDL_ANALYSIS 对齐）
# ---------------------------------------------------------------------------
REQUIRED_RUN_COLS = ["cycle_id", "ts", "mode", "status", "decision_protocol"]
OPTIONAL_RUN_COLS = ["regime", "regime_stale", "market_summary", "missing_sources", "raw"]

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


def validate_receipt(data: dict) -> list[str]:
    """返回错误列表；空=验证通过。"""
    if not isinstance(data, dict):
        return ["回执必须是 dict"]
    data = normalize_receipt(data)
    errors = []
    protocol = data.get("decision_protocol")
    if protocol != DECISION_PROTOCOL:
        errors.append(
            f"decision_protocol 必须是 {DECISION_PROTOCOL}，got: {protocol!r}"
        )
    card_mode = protocol == DECISION_PROTOCOL
    for col in REQUIRED_RUN_COLS:
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
    errors = validate_receipt(data)
    if errors:
        return {"ok": False, "error": "; ".join(errors)}

    cycle_id = data["cycle_id"]
    # ts 一律取 writer 落库时刻，不信 agent 自报值；原报 ts 仍在回执/raw 可溯源。
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    mode = data["mode"]
    regime = data.get("regime")
    regime_stale = data.get("regime_stale", 0)
    market_summary = data.get("market_summary")
    missing_sources = data.get("missing_sources")
    reported_ts = normalize_ts(str(data.get("ts") or ""))
    incoming_raw = data.get("raw")
    if isinstance(incoming_raw, dict):
        raw = {**incoming_raw, "reported_ts": reported_ts}
    elif incoming_raw is None:
        raw = {"reported_ts": reported_ts}
    else:
        raw = {
            "reported_ts": reported_ts,
            "payload_raw": incoming_raw,
        }
    status = data["status"]

    # JSON 序列化 dict 字段
    market_summary_json = json.dumps(market_summary, ensure_ascii=False) if market_summary is not None else None
    missing_json = json.dumps(missing_sources, ensure_ascii=False) if missing_sources is not None else None
    raw_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None

    con = connect(write=True, db_path=db_path)
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
            decision_card_json = (
                json.dumps(decision_card, ensure_ascii=False)
                if decision_card is not None and not isinstance(decision_card, str)
                else decision_card
            )
            signal_raw = sig.get("raw")
            signal_raw_json = (
                json.dumps(signal_raw, ensure_ascii=False)
                if signal_raw is not None and not isinstance(signal_raw, str)
                else signal_raw
            )
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

    errors = validate_receipt(data)
    if errors:
        out = {"ok": False, "error": "; ".join(errors)}
        print(json.dumps(out, ensure_ascii=False))
        return 1

    # --validate-only=只验不写，复用上面同一套硬校验与坏码哨兵。
    # 禁另写 _preflight 脚本重复实现 writer 校验；
    # 常规轮无需预检（writer ok:true 即落库确认），确要预检只准用本 flag。
    if "--validate-only" in sys.argv:
        print(json.dumps({"ok": True, "validate_only": True,
                          "note": "receipt valid, nothing written"}, ensure_ascii=False))
        return 0

    # 提前归一化 ts，验证 ISO8601 / 纯字符串都能进
    if "ts" in data and data["ts"]:
        data["ts"] = normalize_ts(data["ts"])

    result = write_analysis(data, db_path=db_path)
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
