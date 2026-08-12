# -*- coding: utf-8 -*-
r"""Wave1 序6 —— 模型接管重验凭证（终稿边界表 #3 / T4）。

背景：2026-08-10T11:15 轮，分析由首个模型完成，11:32:03 overloaded_error 后备用
模型接管，仅核对机械状态（gate/analysis/facts/回执）便在 160 秒内执行了 DOT
开仓——对前任的决策实质（新闻时效、EV）零复核。本模块把"接管必须重验"变成
executor 前的确定性闸。

设计纪律：
  - **零模型名红线**：业务数据只存不透明指纹 actor_fp = sha256(runtime 身份串)
    前 12 位；审计时人工关联 OpenClaw 会话日志还原。本模块不打印、不落库任何
    runtime 身份明文。
  - **凭证不可手写**：attestation 内容 = 本库确定性重跑 Wave0/1 复算器（analysis
    状态、facts 年龄、证据契约 hash、EV 重算）+ 全文 sha256 指纹；executor 侧
    用同一库**独立重算**时间线并比对指纹，接管模型只能附上工具产物。
  - **同 actor 零负担**：分析与执行同 epoch 时不要求凭证；CLOSE/REDUCE 去风险
    路径不受本闸阻断（与 facts-blocking 同型：缺失事实不得反向堵死平仓）。
  - **OPEN/ADD fail-closed**：非 dry-run 会话文件解析不到时不能证明“未发生接管”，
    executor 在任何账户/订单 I/O 前拒绝 `actor_timeline_required`。dry-run 不要求
    时间线；CLOSE/REDUCE 去风险路径不受本闸阻断。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

CST = timezone(timedelta(hours=8))
ATTESTATION_VERSION = "actor_attestation_v2"
ATTESTATION_MAX_AGE_S = 600  # 凭证时效：生成后 10 分钟内进 executor

_OPENCLAW_AGENT_DIR = Path(
    os.environ.get(
        "OKX_OPENCLAW_AGENT_DIR",
        str(Path.home() / ".openclaw" / "agents" / "okx-live-trader"),
    )
)


def _fp(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def session_key_for_cycle(cycle_id: str, stage: str = "live") -> Optional[str]:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})",
                     str(cycle_id or ""))
    if not m:
        return None
    return f"{stage}-{m.group(1)}{m.group(2)}{m.group(3)}-{m.group(4)}{m.group(5)}"


def resolve_session_file(cycle_id: str, stage: str = "live") -> Optional[Path]:
    """session-key → jsonl 文件；任何一步缺失返回 None（调用方决定告警口径）。"""
    key = session_key_for_cycle(cycle_id, stage)
    if not key:
        return None
    index = _OPENCLAW_AGENT_DIR / "sessions" / "sessions.json"
    if not index.exists():
        return None
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(f"agent:okx-live-trader:{key}")
    if not isinstance(entry, dict):
        return None
    session_id = entry.get("sessionId")
    if not session_id:
        return None
    path = _OPENCLAW_AGENT_DIR / "sessions" / f"{session_id}.jsonl"
    return path if path.exists() else None


def extract_actor_timeline(session_file: Path) -> list[dict[str, Any]]:
    """会话 jsonl → epoch 列表 [{epoch, actor_fp, start_utc, last_utc, turns}]。

    epoch 边界 = assistant 消息的 runtime 身份串发生变化处。身份串本身不返回、
    不落任何输出（零模型名）；同一身份串在任何进程里得到同一 actor_fp，
    executor 独立重算可比对。
    """
    epochs: list[dict[str, Any]] = []
    try:
        with open(session_file, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = d.get("message")
                if not isinstance(m, dict) or m.get("role") != "assistant":
                    continue
                identity = m.get("model")
                if not identity:
                    continue
                ts = str(d.get("timestamp") or "")
                fp = _fp(str(identity))
                if epochs and epochs[-1]["actor_fp"] == fp:
                    epochs[-1]["last_utc"] = ts
                    epochs[-1]["turns"] += 1
                else:
                    epochs.append({
                        "epoch": len(epochs),
                        "actor_fp": fp,
                        "start_utc": ts,
                        "last_utc": ts,
                        "turns": 1,
                    })
    except OSError:
        return []
    return epochs


def actor_chain_hash(epochs: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        "|".join(e["actor_fp"] for e in epochs).encode("utf-8")
    ).hexdigest()[:16]


def _cst_to_utc_iso(ts_cst: str) -> Optional[str]:
    try:
        dt = datetime.strptime(str(ts_cst), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=CST).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def epoch_at(epochs: list[dict[str, Any]], utc_iso: Optional[str]) -> Optional[int]:
    """UTC ISO 时刻落在哪个 epoch（按 start_utc 边界；时刻早于首 epoch → 0）。"""
    if not epochs or not utc_iso:
        return None
    current = 0
    for e in epochs:
        if e["start_utc"] <= utc_iso:
            current = e["epoch"]
        else:
            break
    return current


def timeline_state(cycle_id: str, analysis_ts_cst: Optional[str],
                   stage: str = "live") -> dict[str, Any]:
    """当前时间线状态（executor 与凭证生成共用的独立重算入口）。"""
    session_file = resolve_session_file(cycle_id, stage)
    if session_file is None:
        return {"available": False, "reason": "session_unresolvable"}
    epochs = extract_actor_timeline(session_file)
    if not epochs:
        return {"available": False, "reason": "no_assistant_turns"}
    analysis_utc = _cst_to_utc_iso(analysis_ts_cst) if analysis_ts_cst else None
    analysis_epoch = epoch_at(epochs, analysis_utc)
    current_epoch = epochs[-1]["epoch"]
    return {
        "available": True,
        "session_file": str(session_file),
        "epoch_count": len(epochs),
        "actor_chain_hash": actor_chain_hash(epochs),
        "analysis_epoch": analysis_epoch,
        "current_epoch": current_epoch,
        "handoff_detected": (
            analysis_epoch is not None and analysis_epoch != current_epoch),
    }


def _revalidate(cycle_id: str, db_root: Path) -> dict[str, Any]:
    """接管后的确定性重验包：重跑 Wave0/1 复算器，全部布尔化。"""
    import sqlite3

    checks: dict[str, Any] = {}
    ok = True

    con = sqlite3.connect(
        f"file:{Path(db_root) / 'analysis.db'}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        run = con.execute(
            "SELECT status, ts FROM analysis_runs WHERE cycle_id=?",
            (cycle_id,)).fetchone()
        checks["analysis_status_ok"] = bool(run and run["status"] == "ok")
        ok = ok and checks["analysis_status_ok"]

        signals = con.execute(
            "SELECT symbol, action, side, decision_card FROM analysis_signals "
            "WHERE cycle_id=? AND action IN ('open_long','open_short')",
            (cycle_id,)).fetchall()
    finally:
        con.close()

    try:
        from core.experience_contract import validate_contract
        from core.ev_calculator import build_ev_check
    except ImportError:  # executor 语境：core/ 目录裸导入风格
        from experience_contract import validate_contract
        from ev_calculator import build_ev_check

    contract_ok = True
    ev_ok = True
    news_ok = True
    for sig in signals:
        try:
            card = json.loads(sig["decision_card"] or "{}")
        except json.JSONDecodeError:
            contract_ok = False
            ev_ok = False
            news_ok = False
            continue
        history = card.get("historical_experience") or {}
        errs = validate_contract(
            history.get("evidence_contract"),
            expected_symbol=sig["symbol"],
            expected_side="long" if sig["action"] == "open_long" else "short",
        )
        if errs:
            contract_ok = False
        stored_ev = card.get("ev_check")
        fresh_ev, ev_errs = build_ev_check(
            card, "long" if sig["action"] == "open_long" else "short")
        if ev_errs:
            ev_ok = False
        elif not isinstance(stored_ev, dict) or stored_ev != fresh_ev:
            ev_ok = False
        try:
            from core.news_context import build_news_context
            fresh_news = build_news_context(db_root, cycle_id, window_hours=6)
        except ImportError:
            from news_context import build_news_context
            fresh_news = build_news_context(db_root, cycle_id, window_hours=6)
        if card.get("news_context") != fresh_news:
            news_ok = False
    checks["open_signal_count"] = len(signals)
    checks["evidence_contracts_ok"] = contract_ok
    checks["ev_recompute_ok"] = ev_ok
    checks["news_context_recompute_ok"] = news_ok
    ok = ok and contract_ok and ev_ok and news_ok

    facts_path = Path(db_root).parent / "tmp" / (
        "live_facts_" + str(cycle_id).replace(":", "-") + ".json")
    checks["facts_file_exists"] = facts_path.exists()
    facts_errors: list[str] = []
    facts_payload = None
    if facts_path.exists():
        try:
            facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            facts_errors.append(f"facts_file_invalid:{type(exc).__name__}")
    else:
        facts_errors.append("facts_file_missing")
    if facts_payload is not None:
        root = str(Path(__file__).resolve().parents[1])
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from scripts.live_decision_facts import validate_facts
            facts_errors.extend(validate_facts(
                facts_payload, expected_cycle=cycle_id,
                expected_profile="live", require_ok=True, max_age_s=1800,
            ))
        except Exception as exc:  # noqa: BLE001
            facts_errors.append(f"facts_revalidate_error:{type(exc).__name__}")
    checks["facts_errors"] = facts_errors
    checks["facts_fresh"] = not facts_errors
    checks["facts_hash"] = (
        facts_payload.get("facts_hash") if isinstance(facts_payload, dict) else None)
    ok = ok and not facts_errors

    checks["all_ok"] = ok
    return checks


def build_attestation(cycle_id: str, db_root: str | os.PathLike = r"./db",
                      stage: str = "live") -> dict[str, Any]:
    """生成接管重验凭证（确定性；接管模型只能整体附上，不能改字段）。"""
    import sqlite3

    analysis_ts = None
    con = None
    try:
        con = sqlite3.connect(
            f"file:{Path(db_root) / 'analysis.db'}?mode=ro", uri=True,
            timeout=5)
        row = con.execute(
            "SELECT ts FROM analysis_runs WHERE cycle_id=?",
            (cycle_id,)).fetchone()
        analysis_ts = row[0] if row else None
    except sqlite3.Error:
        pass
    finally:
        if con is not None:
            con.close()

    state = timeline_state(cycle_id, analysis_ts, stage)
    body: dict[str, Any] = {
        "version": ATTESTATION_VERSION,
        "cycle_id": cycle_id,
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "analysis_ts": analysis_ts,
        "timeline": {k: v for k, v in state.items() if k != "session_file"},
    }
    if state.get("available") and state.get("handoff_detected"):
        body["revalidation"] = _revalidate(cycle_id, Path(db_root))
    body["attestation_hash"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    return body


def verify_attestation(attestation: Any, cycle_id: str,
                       db_root: str | os.PathLike = r"./db",
                       stage: str = "live") -> list[str]:
    """executor 侧独立校验：指纹重算 + 时间线独立比对 + 重验结论 + 时效。"""
    errors: list[str] = []
    if not isinstance(attestation, dict):
        return ["actor_attestation 必须是 dict（由 scripts/actor_attestation.py 生成）"]
    if attestation.get("version") != ATTESTATION_VERSION:
        errors.append(f"attestation version 必须是 {ATTESTATION_VERSION}")
    if str(attestation.get("cycle_id")) != str(cycle_id):
        errors.append(
            f"attestation cycle_id={attestation.get('cycle_id')!r} "
            f"与执行 cycle {cycle_id!r} 不符")
    supplied_hash = attestation.get("attestation_hash")
    unsigned = {k: v for k, v in attestation.items()
                if k != "attestation_hash"}
    expected = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    if supplied_hash != expected:
        errors.append("attestation_hash 校验失败（凭证被改写或手写）")
    try:
        generated = datetime.strptime(
            str(attestation.get("generated_at")), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=CST)
        age = (datetime.now(CST) - generated).total_seconds()
        if age > ATTESTATION_MAX_AGE_S or age < -60:
            errors.append(
                f"attestation 已过期（{age:.0f}s > {ATTESTATION_MAX_AGE_S}s），"
                "重跑 scripts/actor_attestation.py")
    except (TypeError, ValueError):
        errors.append("attestation generated_at 非法")

    # 时间线独立重算：chain hash 必须一致（凭证生成后又发生切换 → 不一致 → 拒）
    claimed = attestation.get("timeline")
    state = timeline_state(
        cycle_id,
        attestation.get("analysis_ts"),
        stage,
    )
    if state.get("available"):
        if not isinstance(claimed, dict):
            errors.append("attestation.timeline 缺失")
        elif claimed.get("actor_chain_hash") != state.get("actor_chain_hash"):
            errors.append(
                "actor_chain_hash 与当前会话时间线不符"
                "（凭证生成后 actor 再次变化，重新生成）")
        if state.get("handoff_detected"):
            reval = attestation.get("revalidation")
            if not isinstance(reval, dict) or not reval.get("all_ok"):
                errors.append(
                    "接管已检测到但重验包缺失或未全过"
                    "（revalidation.all_ok 必须为 true）")
            else:
                fresh_reval = _revalidate(cycle_id, Path(db_root))
                if reval != fresh_reval or not fresh_reval.get("all_ok"):
                    errors.append(
                        "接管重验包与 executor 当前独立重算不一致；"
                        "facts/news/EV/证据可能已变化，重新生成凭证"
                    )
    return errors
