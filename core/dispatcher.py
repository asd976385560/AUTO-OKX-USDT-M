# -*- coding: utf-8 -*-
r"""V2.0 §3/§8 —— 硬化派发器（就绪驱动派发 + DB 闩锁幂等）。

unified-live/demo/push 的幂等统一使用
`ledger.db.stage_dispatch(cycle_id,stage)` 唯一约束闩锁。
按上游业务产物就绪条件派发，而非把派发闩锁解释成完成证明
（分析时长不定；阶段终态由 stage_runner/目标业务产物另行核验）。

逻辑（对最近 LOOKBACK_SLOTS+1（当前=5）个 15min 槽，命令型 cron */2min）：
  - stage live（mode=unified）：`analysis_runs[cycle]` 未写且采集必需源齐且新鲜
        → 抢 live 闩锁 → 起 okx-live-trader，一次完成分析+实盘；每 tick 至多派 1 个。
  - stage demo：analysis 已写且新鲜、demo 未派 → 起 demo。
        人工回滚 analyst 写入 analysis 后，补派 full live。
  - stage push：live+demo 的 `trade_cycles[cycle]` 都已写、push 未派 → 抢锁 → fire。
  - **过窗处置**：过窗仍无 analysis / 采集未齐 → **不空起、不补派**，仅 WARN（alert-only，交 on-demand collection_monitor / failureAlert 巡检）。
  - push 送达核验：派发 15-40min 窗内 qq_push_dedupe 无送达记录 → WARN
        （alert-only，不补派不重试）。

幂等真值只认 DB（stage_dispatch）；`logs/trigger/*.log` 降为 debug 日志。
零模型名（红线 #1）：起棒经 trigger_agent（agent-id 集中在那）。

用法（OpenClaw 命令型 cron */2min）：
    pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\core\dispatcher.py --db-root <PROJECT_ROOT>\db
    OKX_TRIGGER_DRYRUN=1 + 上述 wrapper 命令   # 只验逻辑不真起棒
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


import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

_COLLECTORS = os.environ.get("OKX_COLLECTORS_DIR", _project_path('collectors'))
if _COLLECTORS not in sys.path:
    sys.path.insert(0, _COLLECTORS)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import ledger          # noqa: E402  connect/cycle_id_for/try_stage/stage_dispatched
import trigger_agent   # noqa: E402  fire 适配层

CST = timezone(timedelta(hours=8))
MAX_AGE_SEC = 900  # analysis 写出 ≤15min 内仍可起 trader；超过视陈旧不追
COLLECT_MAX_AGE = 900  # 采集齐活后 ≤15min 内仍可起 unified live；gate 继续 registry-aware
LOOKBACK_SLOTS = 4  # P4b：dispatch_once 回扫最近 N+1 个 15min 槽，给 push 等晚到 stage 多轮补派机会（stage_dispatch 闩锁保幂等）
PUSH_VERIFY_MIN_SEC = 900   # push 派发后给足 15min 送达，再核验
PUSH_VERIFY_MAX_SEC = 2400  # 只核验最近 40min 内派发的 push；窗口自然滚动即幂等，无需去重状态
PUSH_EVENT_LOG = Path(_project_path('logs', 'push', 'qq_push_dedupe.jsonl'))


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
    try:
        return con.execute(
            "SELECT COUNT(*) FROM trade_cycles WHERE cycle_id=?",
            (cycle,)).fetchone()[0] > 0
    finally:
        con.close()


def _age_sec(ts_str: str, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(CST)
    try:
        t = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        return int((now - t).total_seconds())
    except (ValueError, TypeError):
        return 10 ** 9


def _collection_ready(ledger_path, cycle: str, max_age: int = COLLECT_MAX_AGE) -> bool:
    """采集必需源齐且新鲜 → True。任何异常一律视作未就绪——绝不让统一 live 触发
    的探测拖垮整个派发器（live/demo/push 派发优先存活）。"""
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
    """抢锁 → fire；起棒失败释放锁（下轮重试）。幂等真值认 stage_dispatch。"""
    if ledger.stage_dispatched(ledger_path, cycle, stage):
        return
    if not ledger.try_stage(ledger_path, cycle, stage):
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


def dispatch_cycle(db_root: Path, ledger_path, cycle: str,
                   max_age: int = MAX_AGE_SEC, now: Optional[datetime] = None,
                   fire_fn: Callable = trigger_agent.fire,
                   allow_unified_live: bool = True) -> list:
    out: list = []
    a = analysis_row(db_root, cycle)
    if not a:
        # 采集齐且新鲜后直接起 unified live（分析+实盘同一 Agent）。
        # 人工回滚 analyst 槽由 candidate 跳过，待其写 analysis 后走 full live。
        try:
            if allow_unified_live and _collection_ready(ledger_path, cycle):
                _fire_stage(ledger_path, cycle, "live", "unified", fire_fn, out)
        except Exception as e:
            log(f"{cycle}: unified live dispatch error (ignored): {e}")
        return out
    mode = str(a.get("mode") or "").strip().lower()
    status = str(a.get("status") or "").strip().lower()
    age = _age_sec(a.get("ts"), now)

    # analysis 只有精确 status=ok + mode=full 才可放行；旧库缺失值、未知值、
    # failed/timeout 或非现役 mode 一律 fail-closed。
    # live/demo/push 闩锁不写、每 tick 重查；人工重写为 status=ok 后自然放行。
    # mode 固定为 full；是否允许下游仅由 analysis status 决定。
    # WARN 防刷屏：stage_dispatch 哨兵行 (cycle,'skip_warn') 唯一约束保每 cycle 只打一次。
    if status != "ok" or mode != "full":
        reason = f"status={status or '<missing>'},mode={mode or '<missing>'}"
        if (not ledger.stage_dispatched(ledger_path, cycle, "skip_warn")
                and ledger.try_stage(ledger_path, cycle, "skip_warn")):
            out.append(f"skip {cycle} analysis {reason}, no trader/push")
            log(f"{cycle}: WARN analysis {reason} -> skip live/demo/push "
                f"(re-check every tick; recovers if analyst rewrites status=ok)")
        return out

    # unified 轮的 live 闩锁已在首棒存在，只会派 demo；人工回滚 analyst 轮若
    # live 尚未派，则在此补派 full live。同一循环由闩锁区分两种路径。
    if age <= max_age:
        for stage in ("live", "demo"):
            _fire_stage(ledger_path, cycle, stage, mode, fire_fn, out)
    else:
        if not ledger.stage_dispatched(ledger_path, cycle, "live"):
            out.append(f"stale {cycle} age={age}s>{max_age}s skip trader")
            log(f"{cycle}: analysis 陈旧 age={age}s，不追起 trader (status={status})")

    # stage push：双盘 trade_cycles 都已写 → 抢锁起 push（独立于 analysis 新鲜度）
    if (trade_written(db_root, "live", cycle)
            and trade_written(db_root, "demo", cycle)):
        _fire_stage(ledger_path, cycle, "push", mode, fire_fn, out)
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
        if ev.get("event") == "duplicate_skip" or (
                ev.get("event") == "mark" and ev.get("status") == "sent"):
            out.add(ev["dedupe_key"])
    return out


def verify_push_delivery(db_root: Path, ledger_path, now: Optional[datetime] = None,
                         event_log: Path = PUSH_EVENT_LOG) -> None:
    """push 送达核验（alert-only，只告警不补派不重试）。

    trigger 起纯脚本 push 后，进程启动成功不代表消息已送达。送达真值 =
    qq_push_dedupe.db sent 表（status='sent' 且 updated_at 晚于派发）
    或事件流 duplicate_skip 合并送达。只核验派发时间落在 [now-40min, now-15min]
    的 push 行：给足 15min 送达；窗口自然滚动即幂等（缺号在窗内每 tick 重复
    WARN，滚出窗口自然停）。
    """
    now = now or datetime.now(CST)
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
        dkey = f"push:{cyc}"
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
    再扫一律静默跳过、绝不重复起棒；live/demo 另有 age<=max_age 新鲜度闸挡住旧轮（不会迟起 trader）。
    """
    if ledger_path is None:
        ledger_path = db_root / "ledger.db"
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
                              now=now, fire_fn=fire_fn,
                              allow_unified_live=(cycle == cand))
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
    db_root = Path(args.db_root)
    ledger.init_ledger(db_root / "ledger.db")  # 幂等保 stage_dispatch 存在
    actions = dispatch_once(db_root, max_age=args.max_age)
    if not actions:
        log("nothing to dispatch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
