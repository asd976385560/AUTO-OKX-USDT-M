# -*- coding: utf-8 -*-
"""V2.0 采集账本与阶段派发闩锁。

唯一权威：判断"某 cycle 采集是否完成且新鲜"只读本模块的账本，不再猜 cycle / 读滞后
时间戳（根治 watchdog 误报）。

职责：
  - collection_runs 账本（采集器结尾必写）
  - stage_dispatch 闩锁（try_stage/stage_dispatched/release_stage，见 §5 段）——
    core/dispatcher.py 对 analyst/live/demo/push 四个 stage 全走它，唯一约束 race-safe
  - gate_collection_fresh（registry-aware 时效判定，analyst 开场 + dispatcher 派发闸共用）

设计要点：
  - 全库 WAL + busy_timeout=5000 + synchronous=NORMAL
  - cycle_id = UTC+8 槽位归一时间戳 'YYYY-MM-DDTHH:MM'（:00/:15/:30/:45）
  - ts/dispatched_at = UTC+8 字符串 'YYYY-MM-DD HH:MM:SS'（与 account.db 同轨）
  - dispatcher 先检查采集 gate，再 INSERT stage_dispatch(cycle_id,stage) 抢锁；
    起棒失败释放锁，下一轮可重试。

本模块是纯 plumbing：不含任何模型名 / provider 字段（红线 #8 天然合规）。
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
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))

# 生产账本路径；测试传自己的 path 覆盖。
DEFAULT_LEDGER = Path(_project_path('db', 'ledger.db'))

# 每个采集器在一轮里的 source 标签。
SRC_FAST = "fast"
SRC_SLOW = "slow"
SRC_REGIME = "regime"
SRC_XSEARCH = "x_search"

# 计入"齐活"的状态（degraded 仍算完成——失败信源由分析员降级处理，不阻断派单）。
DONE_STATUS = ("ok", "degraded")

SLOT_MINUTES = 15
PROFILE_LEASE_COLUMNS = ("profile", "cycle_id", "acquired_at", "expires_at")
PROFILE_LEASE_SCHEMA = (
    ("profile", "TEXT", 0, 1),
    ("cycle_id", "TEXT", 1, 0),
    ("acquired_at", "TEXT", 1, 0),
    ("expires_at", "TEXT", 1, 0),
)


# ---------------------------------------------------------------------------
# 时间 / cycle_id
# ---------------------------------------------------------------------------
def now_cst() -> str:
    """UTC+8 字符串 'YYYY-MM-DD HH:MM:SS'（与 account.db 同轨）。"""
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def cycle_id_for(dt: datetime | None = None) -> str:
    """把时刻归一到所属 15 分钟槽位 → 'YYYY-MM-DDTHH:MM'（UTC+8）。

    14:07 -> '...T14:00'；14:22 -> '...T14:15'；14:59 -> '...T14:45'。
    """
    if dt is None:
        dt = datetime.now(CST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    else:
        dt = dt.astimezone(CST)
    slot_min = (dt.minute // SLOT_MINUTES) * SLOT_MINUTES
    slot = dt.replace(minute=slot_min, second=0, microsecond=0)
    return slot.strftime("%Y-%m-%dT%H:%M")


def is_top_of_hour(cycle_id: str) -> bool:
    """:00 槽位才有慢采 + regime。"""
    return cycle_id.endswith(":00")


def expected_sources(cycle_id: str) -> set[str]:
    """本轮该有哪些采集器（必需集）。

    每轮都要 fast；仅 :00 轮额外需要 slow + regime。
    x_search **不进必需集**——突发新闻信源，失败不阻断派单。
    """
    need = {SRC_FAST}
    if is_top_of_hour(cycle_id):
        need |= {SRC_SLOW, SRC_REGIME}
    return need


# ---------------------------------------------------------------------------
# 连接 / 建表
# ---------------------------------------------------------------------------
def connect(path: str | os.PathLike, readonly: bool = False) -> sqlite3.Connection:
    """WAL + busy_timeout=5000 + synchronous=NORMAL。readonly 走 uri mode=ro。"""
    path = str(path)
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000;")
        return con
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


def _require_profile_lease_migrated(path: Path) -> None:
    """Existing databases must receive the lease table through the guarded migration.

    This inspection is deliberately read-only.  It prevents dispatcher startup from
    turning ``CREATE TABLE IF NOT EXISTS`` into an implicit, unbacked schema change.
    A brand-new database is still initialized in one pass by :func:`init_ledger`.
    """
    con = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
    )
    try:
        present = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("stage_profile_leases",),
        ).fetchone() is not None
        table_info = (
            tuple(
                con.execute("PRAGMA table_info(stage_profile_leases)")
            )
            if present
            else ()
        )
        schema = tuple(
            (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in table_info
        )
    finally:
        con.close()

    if not present:
        raise RuntimeError(
            "existing ledger.db is missing stage_profile_leases; run "
            "scripts/apply_stage_profile_lease_schema.py --apply "
            "--backup-dir <verified-backup-dir> before starting dispatcher"
        )
    if schema != PROFILE_LEASE_SCHEMA:
        raise RuntimeError(
            "incompatible stage_profile_leases schema; guarded migration or "
            f"manual review required (schema={list(schema)})"
        )


def init_ledger(path: str | os.PathLike = DEFAULT_LEDGER) -> None:
    """Initialize a new ledger or verify an existing ledger before idempotent DDL.

    Existing databases are never upgraded implicitly: the profile-lease table must
    already have been installed by the backup-guarded migration script.
    """
    ledger_path = Path(str(path))
    if ledger_path.is_file():
        _require_profile_lease_migrated(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(ledger_path)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS collection_runs (
                cycle_id   TEXT NOT NULL,   -- 槽位归一时间戳 'YYYY-MM-DDTHH:MM'（UTC+8）
                source     TEXT NOT NULL,   -- 'fast' | 'slow' | 'regime' | 'x_search'
                status     TEXT NOT NULL,   -- 'ok' | 'degraded' | 'timeout' | 'error'
                ts         TEXT NOT NULL,   -- 实际完成时刻（UTC+8 'YYYY-MM-DD HH:MM:SS'）
                rows       INTEGER,
                latency_ms INTEGER,
                err        TEXT,
                PRIMARY KEY (cycle_id, source)
            );

            -- V2.0 §5：阶段派发闩锁。dispatcher 起 analyst/live/demo/push 前
            -- INSERT (cycle_id,stage)，唯一约束用于防止双起棒竞态。
            CREATE TABLE IF NOT EXISTS stage_dispatch (
                cycle_id      TEXT NOT NULL,
                stage         TEXT NOT NULL,      -- 'analyst'|'live'|'demo'|'push'|'skip_warn'
                dispatched_at TEXT NOT NULL,
                card_id       TEXT,
                PRIMARY KEY (cycle_id, stage)
            );

            -- 跨 cycle 的同 profile 串行租约。stage_dispatch 只按 cycle 幂等，
            -- 无法阻止上一轮长任务与下一轮同 profile 重叠。
            CREATE TABLE IF NOT EXISTS stage_profile_leases (
                profile       TEXT PRIMARY KEY,   -- 'live'|'demo'
                cycle_id      TEXT NOT NULL,
                acquired_at   TEXT NOT NULL,
                expires_at    TEXT NOT NULL
            );

            -- 开仓副作用幂等：同 profile/cycle/symbol/side 只允许一个逻辑意图。
            -- completed 保存原回执供安全重跑；in-flight/uncertain 一律 fail-closed。
            CREATE TABLE IF NOT EXISTS execution_intents (
                profile             TEXT NOT NULL,
                cycle_id            TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                action              TEXT NOT NULL,
                side                TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                request_json        TEXT NOT NULL,
                state               TEXT NOT NULL,
                reserved_at         TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                submitted_at        TEXT,
                completed_at        TEXT,
                ord_id              TEXT,
                receipt_json        TEXT,
                error               TEXT,
                PRIMARY KEY (profile, cycle_id, symbol, action, side)
            );

            CREATE INDEX IF NOT EXISTS idx_cr_cycle ON collection_runs(cycle_id);
            CREATE INDEX IF NOT EXISTS idx_sd_cycle ON stage_dispatch(cycle_id);
            CREATE INDEX IF NOT EXISTS idx_execution_intents_state
                ON execution_intents(state, updated_at);
            """
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 写账本
# ---------------------------------------------------------------------------
def record_collection(
    path: str | os.PathLike,
    cycle_id: str,
    source: str,
    status: str,
    rows: int | None = None,
    latency_ms: int | None = None,
    err: str | None = None,
) -> None:
    """采集器结尾必调（成功/降级/超时/失败都写）。同轮同源幂等（INSERT OR REPLACE）。"""
    con = connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO collection_runs"
            "(cycle_id, source, status, ts, rows, latency_ms, err) "
            "VALUES (?,?,?,?,?,?,?)",
            (cycle_id, source, status, now_cst(), rows, latency_ms, err),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 分析员开场二次校验（防脏触发）—— V2.0 §6 registry-aware（2026-07-03 落地）
# ---------------------------------------------------------------------------
def _load_registry_module():
    """惰性导入 collectors/sources/_registry。

    ledger 有两种被 import 的身份：wrapper 路径（<PROJECT_ROOT> 在 PYTHONPATH →
    `collectors.ledger`）和 dispatcher 路径（collectors 目录在 sys.path →
    顶层 `ledger`），两条导入路都试；任何失败返回 None（调用方回退 flat）。
    """
    try:
        from collectors.sources import _registry as m
        return m
    except Exception:
        pass
    try:
        from sources import _registry as m
        return m
    except Exception:
        return None


def _registry_slow_gate(reg_mod, registry_path=None):
    """构造 slow/regime 的 registry-aware 判定闭包（§6 时效感知）。

    slow/regime 账本行由慢采写（hourly/daily/weekday/weekly 源）：按 registry
    里 enabled 慢源的各自原生节奏判——任一慢源按其 `staleness_sec`（缺省
    `DEFAULT_STALENESS[cadence]`，weekday 叠周末宽限）判 stale 即整体 stale。
    返回 check(ts_str, now) -> (stale: bool, min_threshold_sec: int|None)；
    registry 缺文件/解析失败/无慢源 → 返回 None（调用方回退 flat，fail-safe）。
    """
    try:
        reg = (reg_mod.load_registry(registry_path) if registry_path is not None
               else reg_mod.load_registry())
        srcs = reg_mod.slow_sources(reg)
        if not srcs:
            return None

        def check(ts_str: str, now: datetime):
            stale_any = False
            min_thr = None
            for s in srcs:
                cadence = s.get("native_cadence")
                thr = s.get("staleness_sec")
                eff = thr if thr is not None else reg_mod.default_staleness(cadence)
                if eff is not None:
                    min_thr = eff if min_thr is None else min(min_thr, eff)
                if reg_mod.is_stale(cadence, ts_str, now, thr):
                    stale_any = True
            return stale_any, min_thr

        return check
    except Exception:
        return None


def gate_collection_fresh(
    path: str | os.PathLike, cycle_id: str, max_age_sec: int = 900,
    registry_path: str | os.PathLike | None = None,
) -> dict:
    # 默认 900s（2026-07-02 600→900，治时效闸倒挂）：dispatcher 判"采集就绪可起 analyst"
    # 与 dispatcher 的 COLLECT_MAX_AGE=900 对齐，避免派发后到场时被更紧阈值拒绝。
    # 二次否决。
    # 注：dispatcher._collection_ready 显式传 900，本默认仅 analyst 开场路径用（改此不影响派发闸）。
    """分析员 claim 后开场调：必需集齐 + 新鲜 → ok；缺 → abort；过期 → stale。

    V2.0 §6 registry-aware（2026-07-03 落地）：
      - fast（15m 类）：保持 flat `max_age_sec`（900）语义不变——整轮时效由它压阵。
      - slow/regime（仅 :00 槽必需）：按 `sources/registry.json` 慢源原生节奏判
        （hourly 2h / daily 26h / weekday 叠周末宽限），不再被 flat 900 一刀切
        （慢采 :00 完成、analyst 拥塞晚到 >15min 不再误判 stale）。
      - registry 读取失败/缺文件 → 完整回退旧 flat 行为（fail-safe，绝不因新
        逻辑抛异常挡派发）。
    返回结构兼容：{status: ok|abort|stale, missing?, age_sec?, max_age_sec?}；
    新增追加字段 per_source（逐源 age/threshold/stale）、stale_sources、
    freshness_mode（'flat'|'registry'）。

    regime/slow 缺失时由调用方按 skill.md carry-forward（沿用上一轮 regime + 标 stale），
    不在此静默放行成空 regime。
    """
    con = connect(path, readonly=True)
    try:
        cur = con.execute(
            "SELECT source, status, ts FROM collection_runs WHERE cycle_id=?",
            (cycle_id,),
        )
        rows = cur.fetchall()
    finally:
        con.close()

    need = expected_sources(cycle_id)
    got_ok = {r["source"] for r in rows if r["status"] in DONE_STATUS}
    missing = need - got_ok
    if missing:
        return {"status": "abort", "missing": sorted(missing)}

    # 逐必需源取 (age_sec, ts)；ts 解析失败时跳过，不计龄、不判 stale。
    now = datetime.now(CST)
    ages: dict[str, tuple[int, str]] = {}
    for r in rows:
        if r["source"] in need and r["status"] in DONE_STATUS:
            try:
                t = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            except (ValueError, TypeError):
                continue
            ages[r["source"]] = (int((now - t).total_seconds()), r["ts"])
    oldest_age = max((a for a, _ in ages.values()), default=0)

    # registry-aware：仅 :00 槽（need 含 slow/regime）才需要读 registry。
    slow_check = None
    freshness_mode = "flat"
    if need - {SRC_FAST}:
        reg_mod = _load_registry_module()
        if reg_mod is not None:
            slow_check = _registry_slow_gate(reg_mod, registry_path)
            if slow_check is not None:
                freshness_mode = "registry"

    per_source: dict[str, dict] = {}
    stale_sources: list[str] = []
    for src, (age, ts_str) in ages.items():
        if src != SRC_FAST and slow_check is not None:
            try:
                stale, thr = slow_check(ts_str, now)
            except Exception:
                stale, thr = (age > max_age_sec), max_age_sec  # fail-safe 回退 flat
        else:
            stale, thr = (age > max_age_sec), max_age_sec
        per_source[src] = {"age_sec": age, "threshold_sec": thr, "stale": stale}
        if stale:
            stale_sources.append(src)

    if stale_sources:
        return {"status": "stale", "age_sec": oldest_age, "max_age_sec": max_age_sec,
                "stale_sources": sorted(stale_sources), "per_source": per_source,
                "freshness_mode": freshness_mode}
    return {"status": "ok", "age_sec": oldest_age, "per_source": per_source,
            "freshness_mode": freshness_mode}


# ---------------------------------------------------------------------------
# V2.0 §5：阶段派发闩锁（dispatcher 用；analyst/live/demo/push 各 stage 幂等 race-safe）
# ---------------------------------------------------------------------------
def try_stage(path: str | os.PathLike, cycle_id: str, stage: str,
              card_id: str | None = None) -> bool:
    """抢某 cycle 某 stage 的派发锁。INSERT 成功（唯一约束）= 本进程赢、应起 agent；
    撞约束 = 已派、静默返回 False（替掉 log 文件幂等，根治双起棒）。"""
    con = connect(path)
    try:
        con.execute(
            "INSERT INTO stage_dispatch(cycle_id, stage, dispatched_at, card_id) "
            "VALUES (?,?,?,?)",
            (cycle_id, stage, now_cst(), card_id),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()


def stage_dispatched(path: str | os.PathLike, cycle_id: str, stage: str) -> bool:
    con = connect(path, readonly=True)
    try:
        cur = con.execute(
            "SELECT 1 FROM stage_dispatch WHERE cycle_id=? AND stage=?",
            (cycle_id, stage))
        return cur.fetchone() is not None
    finally:
        con.close()


def stages_for(path: str | os.PathLike, cycle_id: str) -> set[str]:
    con = connect(path, readonly=True)
    try:
        cur = con.execute(
            "SELECT stage FROM stage_dispatch WHERE cycle_id=?", (cycle_id,))
        return {r["stage"] for r in cur.fetchall()}
    finally:
        con.close()


def release_stage(path: str | os.PathLike, cycle_id: str, stage: str) -> None:
    """释放 stage 闩锁（起 agent 失败时回滚，允许下轮重试）。"""
    con = connect(path)
    try:
        con.execute("DELETE FROM stage_dispatch WHERE cycle_id=? AND stage=?",
                    (cycle_id, stage))
        con.commit()
    finally:
        con.close()


def try_profile_lease(
    path: str | os.PathLike,
    profile: str,
    cycle_id: str,
    ttl_sec: int = 3600,
    now: datetime | None = None,
) -> bool:
    """原子抢同 profile 跨 cycle 租约；未过期的其他 cycle 一律 defer。

    监督 runner 正常结束时显式释放；进程被强杀时由一小时 TTL 兜底，避免永久
    卡死。TTL 高于 live/demo 现役超时，禁止活任务仍在时被下一轮抢占。
    """
    if profile not in {"live", "demo"}:
        raise ValueError(f"profile lease only supports live|demo, got {profile!r}")
    if ttl_sec <= 0:
        raise ValueError("ttl_sec must be positive")
    at = now or datetime.now(CST)
    acquired_at = at.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (at + timedelta(seconds=ttl_sec)).strftime(
        "%Y-%m-%d %H:%M:%S")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT cycle_id,expires_at FROM stage_profile_leases "
            "WHERE profile=?",
            (profile,),
        ).fetchone()
        if row is not None and str(row["expires_at"]) > acquired_at:
            con.rollback()
            # 即使 owner cycle 相同也不能“可重入”：这通常是并发 dispatcher
            # 在首个进程尚未写 stage_dispatch 前撞入。若放行，输掉 cycle 闩锁
            # 的进程会把赢家租约误释放。
            return False
        if row is not None:
            con.execute(
                "DELETE FROM stage_profile_leases WHERE profile=?",
                (profile,),
            )
        con.execute(
            "INSERT INTO stage_profile_leases"
            "(profile,cycle_id,acquired_at,expires_at) VALUES(?,?,?,?)",
            (profile, cycle_id, acquired_at, expires_at),
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def release_profile_lease(
    path: str | os.PathLike,
    profile: str,
    cycle_id: str,
) -> bool:
    """只释放匹配 owner cycle 的租约，防旧 runner 误删新租约。"""
    con = connect(path)
    try:
        cur = con.execute(
            "DELETE FROM stage_profile_leases WHERE profile=? AND cycle_id=?",
            (profile, cycle_id),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI（仅 init / inspect；禁中文写库经命令行——本表全英文，安全）
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="采集账本/闩锁工具（collection_runs + stage_dispatch）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="幂等建表")
    p_init.add_argument("--db", default=str(DEFAULT_LEDGER))
    p_show = sub.add_parser("show", help="打印某 cycle 账本 + 派单状态")
    p_show.add_argument("--db", default=str(DEFAULT_LEDGER))
    p_show.add_argument("--cycle", default=None)
    # gate_collection_fresh 的稳定 CLI。--cycle **必填无墙钟默认**（analyst.md §2
    # 红线：禁 cycle_id_for() 按墙钟重解析——晚到 session 会错标 cycle）；输出 ASCII JSON
    # （CLI stdout 经 cp936 pwsh exec，中文会坏码；本 gate 返回结构全英文，安全）。
    p_gate = sub.add_parser("gate", help="collection freshness gate (JSON stdout; exit 0=ok, 1=stale/missing)")
    p_gate.add_argument("--db", default=str(DEFAULT_LEDGER))
    # --db-root 为目录别名（全系统惯例是 --db-root 目录，本 CLI 的 --db 收
    # ledger.db 单文件路径——隔离演练误传目录给 --db 会 sqlite 打不开）。给了 --db-root
    # 则解析为 <db-root>/ledger.db 并优先于 --db。
    p_gate.add_argument("--db-root", default=None,
                        help="db 根目录（取其下 ledger.db；与 --db 二选一，本参数优先）")
    p_gate.add_argument("--cycle", required=True)
    p_gate.add_argument("--max-age-sec", type=int, default=900)
    args = ap.parse_args()

    if args.cmd == "init":
        init_ledger(args.db)
        print(f"ledger initialized: {args.db}")
        return 0
    if args.cmd == "gate":
        gate_db = str(Path(args.db_root) / "ledger.db") if getattr(args, "db_root", None) else args.db
        g = gate_collection_fresh(gate_db, args.cycle, max_age_sec=args.max_age_sec)
        print(json.dumps(g, ensure_ascii=True))
        return 0 if g.get("status") == "ok" else 1
    if args.cmd == "show":
        cid = args.cycle or cycle_id_for()
        con = connect(args.db, readonly=True)
        try:
            print(f"cycle_id={cid} expected={sorted(expected_sources(cid))}")
            for r in con.execute(
                "SELECT source,status,ts,rows,latency_ms,err FROM collection_runs "
                "WHERE cycle_id=? ORDER BY source", (cid,)):
                print(" ", dict(r))
            stages = list(con.execute(
                "SELECT stage,dispatched_at,card_id FROM stage_dispatch "
                "WHERE cycle_id=? ORDER BY dispatched_at", (cid,)))
            print("  stages:", [dict(r) for r in stages])
        finally:
            con.close()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
