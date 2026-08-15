# -*- coding: utf-8 -*-
r"""V2.0 §3/§8 —— 硬化派发器（就绪驱动派发 + DB 闩锁幂等）。

unified-live/push 的幂等统一使用（demo stage 已于 2026-08-06 下线，历史行保留）
`ledger.db.stage_dispatch(cycle_id,stage)` 唯一约束闩锁。
按上游业务产物就绪条件派发，而非把派发闩锁解释成完成证明
（分析时长不定；阶段终态由 stage_runner/目标业务产物另行核验）。

逻辑（对最近 LOOKBACK_SLOTS+1（当前=5）个 15min 槽，命令型 cron */2min）：
  - stage live（mode=unified）：`analysis_runs[cycle]` 未写且采集必需源齐且新鲜
        → 抢 live 闩锁 → 起 okx-live-trader，一次完成分析+实盘；每 tick 至多派 1 个。
  - stage demo：**2026-08-06 全量下线**，stage 已从 trigger_agent 的路由表移除，
        手动也起不了。人工回滚 analyst 写入 analysis 后，仍补派 full live。
  - stage push：**live** 的有效业务终态已写、且同 cycle live profile 租约已释放、
        push 未派 → 抢锁 → fire。业务终态
        包括成交/HOLD，也包括可证明零交易所副作用的显式 REJECT/ERROR；后者按
        原始错误事实出报告，绝不伪造成 WAIT。
        从 2026-08-13T04:00 起，若监督器已证明 live 失败、租约已释放且仍无有效
        trade terminal，则仅派发 `failure_report`（WAIT/零新增风险）；不补写业务库、
        不重起 Agent。有效 trade terminal 永远优先于失败报告。
  - **过窗处置**：过窗仍无 analysis / 采集未齐 → **不空起、不补派**，仅 WARN（alert-only，交 on-demand collection_monitor / failureAlert 巡检）。
  - push 送达核验：派发 15-40min 窗内 qq_push_dedupe 无送达记录 → WARN
        （alert-only，不补派不重试）。

幂等真值只认 DB（stage_dispatch）；`logs/trigger/*.log` 降为 debug 日志。
零模型名（红线 #1）：起棒经 trigger_agent（agent-id 集中在那）。

用法（OpenClaw 命令型 cron */2min）：
    pwsh -NoProfile -File ./scripts/run_okx_python.ps1 ./core/dispatcher.py --db-root ./db
    OKX_TRIGGER_DRYRUN=1 + 上述 wrapper 命令   # 只验逻辑不真起棒
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

_PROJECT_ROOT = Path(
    os.environ.get("OKX_ROOT") or Path(__file__).resolve().parents[1]
).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


_COLLECTORS = os.environ.get("OKX_COLLECTORS_DIR", _project_path("collectors"))
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)
_SCRIPTS = os.environ.get("OKX_SCRIPTS_DIR", _project_path("scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import ledger          # noqa: E402  connect/cycle_id_for/try_stage/stage_dispatched
import trigger_agent   # noqa: E402  fire 适配层
from cycle_contract import validate_cycle_id  # noqa: E402
from stage_failure_contract import (  # noqa: E402
    load_live_failure,
    load_live_report_barrier,
    load_upstream_failure,
)

CST = timezone(timedelta(hours=8))
MAX_AGE_SEC = 900  # analysis 写出 ≤15min 内仍可起 trader；超过视陈旧不追
COLLECT_MAX_AGE = 900  # 采集齐活后 ≤15min 内仍可起 unified live；gate 继续 registry-aware
LOOKBACK_SLOTS = 4  # P4b：dispatch_once 回扫最近 N+1 个 15min 槽，给 push 等晚到 stage 多轮补派机会（stage_dispatch 闩锁保幂等）
PUSH_VERIFY_MIN_SEC = 900   # push 派发后给足 15min 送达，再核验
PUSH_VERIFY_MAX_SEC = 2400  # 只核验最近 40min 内派发的 push；窗口自然滚动即幂等，无需去重状态
PUSH_EVENT_LOG = Path(_project_path("logs", "push", "qq_push_dedupe.jsonl"))
STAGE_STATUS_DIR = Path(os.environ.get(
    "OKX_STAGE_STATUS_DIR", _project_path("logs", "stage-status")))
CANONICAL_DB_ROOT = Path(_project_path("db")).resolve()
BUSINESS_ERROR_REPORTABLE_FROM = "2026-08-14T02:15"
# Same-slot reconciled closes before this boundary keep their historical
# dispatch/report treatment.  This prevents deployment from chasing a slot
# whose legacy close report may already have been delivered.
RECONCILE_ANALYSIS_TERMINAL_FROM = "2026-08-14T04:00"


def _root_namespace(db_root: Path | str) -> str:
    resolved = Path(db_root).resolve()
    if os.path.normcase(os.fspath(resolved)) == os.path.normcase(
        os.fspath(CANONICAL_DB_ROOT)
    ):
        return ""
    return "r" + hashlib.sha256(
        os.path.normcase(os.fspath(resolved)).encode("utf-8")
    ).hexdigest()[:10]


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_cst()}] DISPATCH: {msg}", flush=True)


# ---------------------------------------------------------------------------
# 只读探针
# ---------------------------------------------------------------------------
def analysis_row(db_root: Path, cycle: str) -> Optional[dict]:
    db = db_root / "analysis.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute(
            "SELECT ts, mode, status FROM analysis_runs WHERE cycle_id=?",
            (cycle,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def trade_written(db_root: Path, profile: str, cycle: str) -> bool:
    db = db_root / f"{profile}_trades.db"
    if not db.exists():
        return False
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT decision,n_orders FROM trade_cycles WHERE cycle_id=?",
            (cycle,)).fetchone()
        if row is None:
            return False
        decision = str(row["decision"] or "").strip().lower()
        try:
            n_orders = int(row["n_orders"])
        except (TypeError, ValueError):
            return False
        return (
            (decision == "traded" and n_orders > 0)
            or (decision in {"hold", "skip"} and n_orders == 0)
        )
    finally:
        con.close()


def trade_cycle_present(db_root: Path, profile: str, cycle: str) -> bool:
    """Return whether any cycle row exists, including malformed/partial rows.

    A partial business row is not a valid push terminal, but it is also too
    authoritative to replace with a synthetic failure WAIT report.  Keeping
    its push latch free lets a later controlled correction become reportable.
    """
    db = db_root / f"{profile}_trades.db"
    if not db.exists():
        return False
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(
            "SELECT 1 FROM trade_cycles WHERE cycle_id=? LIMIT 1",
            (cycle,),
        ).fetchone() is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def trade_terminal_requires_analysis(db_root: Path, profile: str,
                                     cycle: str) -> bool:
    """Return whether a trade row is maintenance evidence, not an Agent terminal.

    Exchange-triggered closes can be reconciled while the scheduled unified
    Agent for the same 15-minute slot is still waiting on the profile lease.
    Those rows are authoritative transaction facts, but they must not satisfy
    the scheduled analysis/trade terminal by themselves.  Otherwise the
    dispatcher sends a legacy close report and silently skips that slot's
    full analysis.  The marker is read only from the maintenance raw payload;
    ordinary current receipts never gain this bypass.
    """
    if str(cycle) < RECONCILE_ANALYSIS_TERMINAL_FROM:
        return False
    db = db_root / f"{profile}_trades.db"
    if not db.exists():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{db.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
        row = connection.execute(
            "SELECT raw FROM trade_cycles WHERE cycle_id=?", (cycle,)
        ).fetchone()
        if row is None:
            return False
        raw = json.loads(row[0] or "")
        return (
            isinstance(raw, dict)
            and bool(str(raw.get("reconcile_source") or "").strip())
            and str(raw.get("cycle_ts_source") or "").strip()
            == "trusted_internal_override"
        )
    except (sqlite3.Error, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    finally:
        if connection is not None:
            connection.close()


def live_cycle_in_progress(ledger_path: Path, cycle: str,
                           now: Optional[datetime] = None) -> bool:
    """Return whether the same cycle still owns the live profile lease.

    ``trades_writer`` and ``analyst_writer`` both nudge the dispatcher before
    the supervising stage runner has finished.  A same-slot reconciled close
    may therefore already look like a valid trade terminal while the Agent is
    still deciding or executing another action.  The profile lease is the
    authoritative cross-process lifetime marker: defer push until the runner
    releases it, so the one-shot push latch cannot capture a partial cycle.

    Probe errors fail closed for this tick.  The dispatcher is periodic, so a
    later tick can safely retry without ever sending an early report.
    """
    at = now or datetime.now(CST)
    at_text = at.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    connection: sqlite3.Connection | None = None
    try:
        connection = ledger.connect(ledger_path, readonly=True)
        row = connection.execute(
            "SELECT cycle_id,expires_at FROM stage_profile_leases "
            "WHERE profile='live'"
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["cycle_id"] or "") == str(cycle)
            and str(row["expires_at"] or "") > at_text
        )
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        log(f"{cycle}: live profile lease probe error (defer push): {exc}")
        return True
    finally:
        if connection is not None:
            connection.close()


def _live_lease_blocks_push(
    ledger_path: Path,
    cycle: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Apply the live-lease push barrier only to its forward activation set.

    Older cycles retain their historical dispatch treatment.  For activated
    cycles, ``live_cycle_in_progress`` remains fail-closed on probe errors so a
    partial same-cycle terminal can never consume the push latch early.
    """
    if str(cycle) < RECONCILE_ANALYSIS_TERMINAL_FROM:
        return False
    return live_cycle_in_progress(ledger_path, cycle, now=now)


def trade_error_reportable(db_root: Path, profile: str, cycle: str) -> bool:
    """Prove an explicit business error is safe to report as itself.

    A persisted error is reportable only when both ledgers prove there was no
    exchange side effect: zero order/trade rows and either no execution intent
    or only pristine ``failed_clean`` intents.  Unknown/partial rows remain
    fail-closed and keep the push latch free for controlled correction.
    """
    if str(cycle) < BUSINESS_ERROR_REPORTABLE_FROM:
        return False
    trade_db = db_root / f"{profile}_trades.db"
    intent_db = db_root / "ledger.db"
    if not trade_db.is_file() or not intent_db.is_file():
        return False
    trade_connection: sqlite3.Connection | None = None
    intent_connection: sqlite3.Connection | None = None
    try:
        trade_connection = sqlite3.connect(
            f"file:{trade_db.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        trade_connection.row_factory = sqlite3.Row
        row = trade_connection.execute(
            "SELECT decision,n_orders,raw FROM trade_cycles WHERE cycle_id=?",
            (cycle,),
        ).fetchone()
        if row is None:
            return False
        decision = str(row["decision"] or "").strip().lower()
        try:
            n_orders = int(row["n_orders"])
        except (TypeError, ValueError):
            return False
        if decision not in {"error", "degraded"} or n_orders != 0:
            return False
        trade_count = int(trade_connection.execute(
            "SELECT COUNT(*) FROM trades WHERE cycle_id=?", (cycle,)
        ).fetchone()[0])
        if trade_count != 0:
            return False
        try:
            raw = json.loads(row["raw"] or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(raw, dict):
            return False
        if str(raw.get("status") or "").strip().lower() != decision:
            return False
        try:
            if int(raw.get("n_orders")) != 0:
                return False
        except (TypeError, ValueError):
            return False
        raw_action = str(raw.get("action_taken") or "").strip().upper()
        allowed_actions = (
            {"REJECT"} if decision == "error"
            else {"REJECT", "WAIT", "HOLD"}
        )
        if raw_action not in allowed_actions:
            return False
        raw_trades = raw.get("trades")
        if not isinstance(raw_trades, list) or raw_trades:
            return False
        reason = (
            raw.get("reject_reason")
            or next((item for item in raw.get("errors") or [] if item), None)
        )
        if not reason:
            return False

        intent_connection = sqlite3.connect(
            f"file:{intent_db.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        intent_connection.row_factory = sqlite3.Row
        intents = intent_connection.execute(
            "SELECT state,ord_id,submitted_at,completed_at "
            "FROM execution_intents WHERE profile=? AND cycle_id=?",
            (profile, cycle),
        ).fetchall()
        return all(
            str(item["state"] or "").strip().lower() == "failed_clean"
            and item["ord_id"] in (None, "")
            and item["submitted_at"] in (None, "")
            and item["completed_at"] in (None, "")
            for item in intents
        )
    except (sqlite3.Error, OSError):
        return False
    finally:
        if trade_connection is not None:
            trade_connection.close()
        if intent_connection is not None:
            intent_connection.close()


def live_failure_terminal(
    cycle: str, now: Optional[datetime] = None,
    db_root: Optional[Path] = None,
) -> Optional[dict]:
    """Return a future-activated, redacted, immutable upstream failure.

    ``db_root=None`` preserves the historical live-only probe used by isolated
    callers.  The dispatcher supplies the root so an explicit collection
    terminal can be considered only after execution-path absence is proved.
    """
    if db_root is not None and _root_namespace(db_root):
        # The shared failure-contract helper uses canonical status filenames.
        # Never let an isolated tree consume a production Agent terminal.
        return None
    if db_root is not None:
        return load_upstream_failure(
            cycle,
            db_root=db_root,
            status_dir=STAGE_STATUS_DIR,
            now=now,
        )
    return load_live_failure(
        cycle, status_dir=STAGE_STATUS_DIR, now=now)


def live_report_barrier_ready(
    cycle: str,
    db_root: Path | str | None = None,
) -> bool:
    """Require the post-Agent exchange/ledger read before future push slots."""
    try:
        barrier = load_live_report_barrier(
            cycle, status_dir=STAGE_STATUS_DIR)
    except Exception as exc:
        log(f"{cycle}: report reconcile barrier probe error (defer push): {exc}")
        return False
    if barrier is None:
        log(f"{cycle}: report reconcile barrier missing/unsafe (defer push)")
        return False
    if (db_root is not None and _root_namespace(db_root)
            and barrier.get("required") is not False):
        log(f"{cycle}: isolated db_root has no canonical report barrier (defer push)")
        return False
    return True


def _age_sec(ts_str: str, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(CST)
    try:
        t = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        return int((now - t).total_seconds())
    except (ValueError, TypeError):
        return 10 ** 9


def _collection_ready(ledger_path, cycle: str, max_age: int = COLLECT_MAX_AGE) -> bool:
    """采集必需源齐且新鲜 → True。任何异常一律视作未就绪——绝不让统一 live 触发
    的探测拖垮整个派发器（live/push 派发优先存活）。"""
    try:
        g = ledger.gate_collection_fresh(ledger_path, cycle, max_age_sec=max_age)
        return g.get("status") == "ok"
    except Exception as e:
        log(f"{cycle}: collection_ready 探测失败（视为未就绪）: {e}")
        return False


# ---------------------------------------------------------------------------
# 单 cycle 派发
# ---------------------------------------------------------------------------
def _fire_stage(ledger_path, cycle: str, stage: str, mode: str,
                fire_fn: Callable, result: list) -> None:
    """抢 profile 租约 + cycle 闩锁 → fire；起棒失败释放（下轮重试）。

    Dry-run only records the intended launch.  It must not acquire any
    persistent stage or profile latch.
    """
    cycle = validate_cycle_id(cycle)
    if ledger.stage_dispatched(ledger_path, cycle, stage):
        return
    if os.environ.get("OKX_TRIGGER_DRYRUN") == "1":
        try:
            key = fire_fn(stage, cycle, mode)
            result.append(f"dry_run fired {stage} {cycle} (key={key})")
            log(f"{cycle}: dry-run fired {stage} (mode={mode}); no latch written")
        except Exception as e:
            result.append(f"fire_failed {stage} {cycle}: {e}")
            log(f"{cycle}: dry-run fire {stage} FAILED; no latch written: {e}")
        return
    leased = stage == "live"
    if leased and not ledger.try_profile_lease(ledger_path, stage, cycle):
        result.append(f"deferred {stage} {cycle}: profile lease busy")
        log(f"{cycle}: stage={stage} deferred (same-profile task still active)")
        return
    if not ledger.try_stage(ledger_path, cycle, stage):
        if leased:
            ledger.release_profile_lease(ledger_path, stage, cycle)
        return  # 并发对手赢了锁
    try:
        key = fire_fn(stage, cycle, mode)
        try:
            con = ledger.connect(ledger_path)
            try:
                con.execute(
                    "UPDATE stage_dispatch SET card_id=? WHERE cycle_id=? AND stage=?",
                    (str(key) if key is not None else None, cycle, stage),
                )
                con.commit()
            finally:
                con.close()
        except Exception as e:
            # card_id 只用于排障可观测性；写回失败不影响已赢得的派发闩锁。
            log(f"{cycle}: stage={stage} card_id 写回失败（忽略）: {e}")
        result.append(f"fired {stage} {cycle} (key={key})")
        log(f"{cycle}: fired {stage} (mode={mode})")
    except Exception as e:
        ledger.release_stage(ledger_path, cycle, stage)
        if leased:
            ledger.release_profile_lease(ledger_path, stage, cycle)
        result.append(f"fire_failed {stage} {cycle}: {e}")
        log(f"{cycle}: fire {stage} FAILED, latch released: {e}")


def _slot_expired(cycle: str, now: datetime) -> bool:
    """槽位过窗判定：槽起点已过去 一个槽长 + COLLECT_MAX_AGE → 本槽采集不会再来。

    采集器按启动时刻归槽（ledger.cycle_id_for），跨过下一个槽界后不可能再落本槽
    新行；再叠 COLLECT_MAX_AGE 宽限防慢采迟启动/长跑误报。cycle 解析失败视为
    未过窗（fail-safe，不误告警）。
    """
    try:
        slot = datetime.strptime(cycle, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    except (ValueError, TypeError):
        return False
    return (now - slot).total_seconds() > ledger.SLOT_MINUTES * 60 + COLLECT_MAX_AGE


def _unified_live_candidate(db_root: Path, ledger_path, cycles: list,
                            now: Optional[datetime] = None) -> tuple:
    """单发闸：本 tick 至多派 1 个统一 live Agent，最新槽优先。

    分析+实盘由 okx-live-trader 承担，首棒闩锁使用 stage=live。
    若某槽已有人工回滚 analyst 或 live 闩锁，不得再起统一 live，防同轮并发。
    从最新往回找第一个合格槽做唯一候选；其余合格槽 defer 给下一 tick（走既有
    就绪判定的首次派发，非重试）；过窗槽（gate=stale，或 gate=abort 且已过窗）
    只 WARN 永不派——丢轮只告警不自动重试（主人红线）。
    返回 (candidate_or_None, deferred_cycles, expired_descriptions)。
    """
    now = now or datetime.now(CST)
    cand = None
    deferred: list = []
    expired: list = []
    for cycle in reversed(cycles):
        try:
            if analysis_row(db_root, cycle) is not None:
                continue
            if (ledger.stage_dispatched(ledger_path, cycle, "analyst")
                    or ledger.stage_dispatched(ledger_path, cycle, "live")):
                continue
            g = ledger.gate_collection_fresh(ledger_path, cycle,
                                             max_age_sec=COLLECT_MAX_AGE)
            st = g.get("status")
            if st == "ok":
                if cand is None:
                    cand = cycle
                else:
                    deferred.append(cycle)
            elif st == "stale":
                expired.append(f"{cycle}(age={g.get('age_sec')}s)")
            elif _slot_expired(cycle, now):
                # abort（必需源未齐）且已过窗 → 采集不会再来，同样按过窗告警，
                # 防止整轮无声缺失。
                missing = ",".join(g.get("missing") or []) or "unknown"
                expired.append(f"{cycle}(missing={missing})")
            # 窗内 abort → 静默：采集可能在路上，下一 tick 自然复查
        except Exception as e:
            log(f"{cycle}: unified live candidate probe error (skip): {e}")
    return cand, deferred, expired


def _fire_failure_report_if_terminal(
    ledger_path: Path,
    cycle: str,
    now: Optional[datetime],
    fire_fn: Callable,
    out: list,
) -> bool:
    """Fire one future-only WAIT report after a proved upstream failure.

    This never releases/retries the live latch and never writes an analysis or
    trade terminal.  The push process independently revalidates the same live
    status or collection receipt plus execution-path absence before build/send.
    """
    try:
        terminal = live_failure_terminal(
            cycle, now=now, db_root=ledger_path.parent)
    except Exception as exc:
        log(f"{cycle}: live failure report probe error (skip): {exc}")
        return False
    if terminal is None:
        return False
    _fire_stage(
        ledger_path, cycle, "push", "failure_report", fire_fn, out)
    return True


def dispatch_cycle(db_root: Path, ledger_path, cycle: str,
                   max_age: int = MAX_AGE_SEC, now: Optional[datetime] = None,
                   fire_fn: Callable = trigger_agent.fire,
                   allow_unified_live: bool = True,
                   allow_agent_stages: Optional[bool] = None) -> list:
    cycle = validate_cycle_id(cycle)
    trigger_db_root = Path(db_root)
    db_root = Path(db_root).resolve()
    out: list = []
    default_trigger = fire_fn is trigger_agent.fire
    effective_fire = fire_fn
    if default_trigger:
        effective_fire = lambda stage, dispatch_cycle_id, mode: trigger_agent.fire(
            stage, dispatch_cycle_id, mode, db_root=trigger_db_root
        )
    if allow_agent_stages is None:
        allow_agent_stages = (
            not default_trigger
            or os.environ.get("OKX_TRIGGER_DRYRUN") == "1"
            or _root_namespace(db_root) == ""
        )
    a = analysis_row(db_root, cycle)
    if not a:
        # 极端顺序下即使 analysis 行不可见，有效 trade terminal 仍是最高权威，
        # 绝不能被失败报告覆盖。唯一例外是同槽自动对账 close：它是权威成交
        # 事实，却不能取代本槽 scheduled unified Agent 的完整分析终态。
        maintenance_only = trade_terminal_requires_analysis(
            db_root, "live", cycle)
        if (not maintenance_only and (
                trade_written(db_root, "live", cycle)
                or trade_error_reportable(db_root, "live", cycle))):
            if not _live_lease_blocks_push(ledger_path, cycle, now=now):
                if live_report_barrier_ready(cycle, db_root):
                    _fire_stage(
                        ledger_path, cycle, "push", "full", effective_fire, out)
            return out
        if trade_cycle_present(db_root, "live", cycle) and not maintenance_only:
            # 行已出现但不是合法终态：保留 push 闩锁，既不伪造失败报告，
            # 也不重复启动统一 Agent；等待受控纠正后下一 tick 自然放行。
            return out
        if _fire_failure_report_if_terminal(
                ledger_path, cycle, now, effective_fire, out):
            return out
        # 采集齐且新鲜后直接起 unified live（分析+实盘同一 Agent）。
        # 人工回滚 analyst 槽由 candidate 跳过，待其写 analysis 后走 full live。
        try:
            if allow_unified_live and _collection_ready(ledger_path, cycle):
                if allow_agent_stages:
                    _fire_stage(
                        ledger_path, cycle, "live", "unified",
                        effective_fire, out)
                else:
                    out.append(
                        f"blocked live/demo {cycle}: non-default db_root requires dry-run"
                    )
                    log(
                        f"{cycle}: live Agent blocked before lease/latch; "
                        "non-default db_root requires OKX_TRIGGER_DRYRUN=1"
                    )
        except Exception as e:
            log(f"{cycle}: unified live dispatch error (ignored): {e}")
        return out
    mode = str(a.get("mode") or "").strip().lower()
    status = str(a.get("status") or "").strip().lower()
    age = _age_sec(a.get("ts"), now)

    # analysis 只有精确 status=ok + mode=full 才可放行；旧库缺失值、未知值、
    # failed/timeout 或非现役 mode 一律 fail-closed。
    # live/push 闩锁不写、每 tick 重查；人工重写为 status=ok 后自然放行。
    # mode 固定为 full；是否允许下游仅由 analysis status 决定。
    # WARN 防刷屏：stage_dispatch 哨兵行 (cycle,'skip_warn') 唯一约束保每 cycle 只打一次。
    if status != "ok" or mode != "full":
        if (not trade_cycle_present(db_root, "live", cycle)
                and _fire_failure_report_if_terminal(
                ledger_path, cycle, now, effective_fire, out)):
            return out
        reason = f"status={status or '<missing>'},mode={mode or '<missing>'}"
        if os.environ.get("OKX_TRIGGER_DRYRUN") == "1":
            out.append(f"dry_run skip {cycle} analysis {reason}, no latch written")
            log(
                f"{cycle}: dry-run WARN analysis {reason} -> skip live/push; "
                "no skip_warn latch written"
            )
            return out
        if (not ledger.stage_dispatched(ledger_path, cycle, "skip_warn")
                and ledger.try_stage(ledger_path, cycle, "skip_warn")):
            out.append(f"skip {cycle} analysis {reason}, no trader/push")
            log(f"{cycle}: WARN analysis {reason} -> skip live/push "
                f"(re-check every tick; recovers if analyst rewrites status=ok)")
        return out

    # 人工回滚 analyst 轮若 live 尚未派，在此补派 full live；unified 轮的 live 闩锁
    # 已在首棒存在，_fire_stage 静默跳过。
    # **2026-08-06：demo 全量下线。** 起因是三重口径分叉——标的范围（live 36 个 /
    # demo 17 个，OKX 对 demo 不可交易标的统一返 51001）、仓位授权（max-size vs
    # 组合 IMR≤0.666）、可用保证金（demo availBal 仅占 totalEq 0.5%，实际可冒险
    # 资金比 live 还少）。当日先停自动派发，随后决定连运行能力与历史数据一并清除；
    # order_executor 现以 _require_live_profile() 硬拒任何非 live profile。
    if age <= max_age:
        if allow_agent_stages:
            _fire_stage(
                ledger_path, cycle, "live", mode, effective_fire, out)
        else:
            out.append(
                f"blocked live/demo {cycle}: non-default db_root requires dry-run"
            )
    else:
        if not ledger.stage_dispatched(ledger_path, cycle, "live"):
            out.append(f"stale {cycle} age={age}s>{max_age}s skip trader")
            log(f"{cycle}: analysis 陈旧 age={age}s，不追起 trader (status={status})")

    # stage push：有效 live trade_cycles 永远优先；否则仅在未来激活边界后，
    # 监督器已证明 live 失败时，生成 WAIT/零新增风险的失败报告。
    # 2026-08-06 解耦：push 是纯汇报、一分钱不碰，不得被 demo 账本卡住（8-05 一个
    # 自愈范围内的 demo 幽灵仓曾让 demo+push 死锁 2h14m）——与 trigger_agent 对
    # autoheal blocking「只告警不停 stage」同一条边界，真正的防线在 executor pretrade。
    # demo 停自动派发后本就不落库，故不再为此告警；payload 侧仍把该盘标 PENDING。
    if (trade_written(db_root, "live", cycle)
            or trade_error_reportable(db_root, "live", cycle)):
        if not _live_lease_blocks_push(ledger_path, cycle, now=now):
            if live_report_barrier_ready(cycle, db_root):
                _fire_stage(
                    ledger_path, cycle, "push", mode, effective_fire, out)
    elif not trade_cycle_present(db_root, "live", cycle):
        _fire_failure_report_if_terminal(
            ledger_path, cycle, now, effective_fire, out)
    return out


def _delivered_dedupe_keys(event_log: Path, floor_ts: str) -> set:
    """qq_push_dedupe.jsonl 尾部「已送达」的显式身份键集合（ts>=floor_ts）。

    送达形态两种：mark(status=sent)=真发成功；duplicate_skip=同键已有 sent、
    本次派发被合并（skip 不写 dedupe 库，只能从事件流看到）。按 dedupe_key
    归集，避免相邻 push 互相掩盖。
    """
    if not event_log.exists():
        return set()
    lines = event_log.read_text(encoding="utf-8", errors="replace").splitlines()
    out: set = set()
    for line in lines[-400:]:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if (ev.get("ts") or "") < floor_ts or not ev.get("dedupe_key"):
            continue
        if (ev.get("event") == "duplicate_skip"
                and ev.get("existing_status") == "sent") or (
                ev.get("event") == "mark" and ev.get("status") == "sent"):
            out.add(ev["dedupe_key"])
    return out


def verify_push_delivery(
    db_root: Path,
    ledger_path,
    now: Optional[datetime] = None,
    event_log: Path | None = None,
) -> None:
    """push 送达核验（alert-only，只告警不补派不重试）。

    trigger 起纯脚本 push 后，进程启动成功不代表消息已送达。送达真值 =
    qq_push_dedupe.db sent 表（status='sent' 且 updated_at 晚于派发）
    或事件流 duplicate_skip 合并送达。只核验派发时间落在 [now-40min, now-15min]
    的 push 行：给足 15min 送达；窗口自然滚动即幂等（缺号在窗内每 tick 重复
    WARN，滚出窗口自然停）。
    """
    now = now or datetime.now(CST)
    db_root = Path(db_root).resolve()
    namespace = _root_namespace(db_root)
    if event_log is None:
        event_log = (
            PUSH_EVENT_LOG
            if not namespace
            else PUSH_EVENT_LOG.with_name(
                f"qq_push_dedupe-{namespace}.jsonl"
            )
        )
    lo = (now - timedelta(seconds=PUSH_VERIFY_MAX_SEC)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (now - timedelta(seconds=PUSH_VERIFY_MIN_SEC)).strftime("%Y-%m-%d %H:%M:%S")
    con = ledger.connect(ledger_path, readonly=True)
    try:
        rows = con.execute(
            "SELECT cycle_id, dispatched_at FROM stage_dispatch "
            "WHERE stage='push' AND dispatched_at>=? AND dispatched_at<=? "
            "ORDER BY dispatched_at",
            (lo, hi)).fetchall()
    finally:
        con.close()
    if not rows:
        return
    dedupe_db = db_root / "qq_push_dedupe.db"
    delivered_keys = _delivered_dedupe_keys(event_log, lo)
    for r in rows:
        cyc, at = r["cycle_id"], r["dispatched_at"]
        # 按 cycle 精确关联：pipeline 的身份键=push:{cycle}、
        # 无显式 --target 时 target='default'（qq_push._dedupe_key 同式），sent 表主键
        # 可确定性重算，禁止用“派发后有任意 sent”宽判。
        dkey = f"push:{namespace}:{cyc}" if namespace else f"push:{cyc}"
        skey = hashlib.sha256(f"default|{dkey}".encode("utf-8")).hexdigest()
        delivered = dkey in delivered_keys
        if not delivered and dedupe_db.exists():
            dcon = sqlite3.connect(f"file:{dedupe_db}?mode=ro", uri=True, timeout=5)
            try:
                delivered = dcon.execute(
                    "SELECT 1 FROM sent WHERE k=? AND status='sent' LIMIT 1",
                    (skey,)).fetchone() is not None
            finally:
                dcon.close()
        if not delivered:
            log(f"push dispatched cycle={cyc} at={at}, no delivery record in "
                f"qq_push_dedupe after 15min (alert-only)")


def dispatch_once(db_root: Path, ledger_path=None, max_age: int = MAX_AGE_SEC,
                  now: Optional[datetime] = None,
                  fire_fn: Callable = trigger_agent.fire,
                  lookback_slots: int = LOOKBACK_SLOTS) -> list:
    """回扫最近 lookback_slots+1 个槽各派发一遍（cur..cur-N，从旧到新）。

    P4b（2026-06-29）：原仅 prev+cur 两槽。trader 常在槽内 ~20min+ 才写完 trade_cycles，
    叠加 dispatcher cron 被 gateway 饿（实测 10-28min 间隔），push 往往只剩一次「在窗且双盘
    trade_cycles 齐」的机会即丢→该 cycle 滑出两槽窗后永不补派（实测 ~18% 完成交易的 cycle 无 push）。
    拉宽回扫窗让 push 多轮补派；stage_dispatch UNIQUE(cycle_id,stage) 闩锁保幂等，已派 stage
    再扫一律静默跳过、绝不重复起棒；live 另有 age<=max_age 新鲜度闸挡住旧轮（不会迟起 trader）。
    """
    db_root = Path(db_root).resolve()
    if ledger_path is None:
        ledger_path = db_root / "ledger.db"
    effective_fire = fire_fn
    default_trigger = fire_fn is trigger_agent.fire
    if default_trigger:
        effective_fire = lambda stage, cycle, mode: trigger_agent.fire(
            stage, cycle, mode, db_root=db_root
        )
    allow_agent_stages = (
        not default_trigger
        or os.environ.get("OKX_TRIGGER_DRYRUN") == "1"
        or _root_namespace(db_root) == ""
    )
    now = now or datetime.now(CST)
    cycles = [
        ledger.cycle_id_for(now - timedelta(minutes=ledger.SLOT_MINUTES * i))
        for i in range(lookback_slots, -1, -1)
    ]
    cand, deferred, expired = _unified_live_candidate(db_root, ledger_path, cycles,
                                                       now=now)
    for c in deferred:
        log(f"{c}: unified live deferred (per-tick single-fire, candidate={cand})")
    for c in expired:
        log(f"{c}: unified live never dispatched, collection window expired "
            f"(alert-only, no retry)")
    out: list = []
    for cycle in cycles:
        out += dispatch_cycle(db_root, ledger_path, cycle, max_age=max_age,
                              now=now, fire_fn=effective_fire,
                              allow_unified_live=(cycle == cand),
                              allow_agent_stages=allow_agent_stages)
    try:
        verify_push_delivery(db_root, ledger_path, now=now)
    except Exception as e:
        log(f"push delivery verify error (ignored, alert-only): {e}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="V2.0 硬化派发器（就绪驱动派发 + DB 闩锁）")
    ap.add_argument("--db-root", required=True)
    ap.add_argument("--max-age", type=int, default=MAX_AGE_SEC)
    args = ap.parse_args()
    db_root = Path(args.db_root).resolve()
    ledger.init_ledger(db_root / "ledger.db")  # 幂等保 stage_dispatch 存在
    actions = dispatch_once(db_root, max_age=args.max_age)
    if not actions:
        log("nothing to dispatch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
