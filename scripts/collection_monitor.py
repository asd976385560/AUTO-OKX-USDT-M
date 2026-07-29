# -*- coding: utf-8 -*-
r"""collection_monitor.py — within-day 采集/派单/推送健康监控（纯脚本）。

确定性检查账本健康、源时效和注入话术；超阈经 qq_push.py 推统一默认 target
（告警用途键）。LLM 不参与检测环。

只在下述“真故障”告警，其余一律 audit-only（记 audit.jsonl 不推 QQ）：
  QQ 告警（P0/P1）：
    · 必需源断流/超时（source_freshness abort_sources：required 缺数据 OR 超其 staleness_sec）→ P0
    · 同源连续 ≥3 轮 error/timeout → P1
    · 派单丢轮 ≥2（form① decision_fired_no_analysis + form② 采集齐但从未派过窗）→ P1
    · trader 已派但超宽限仍未落 trade_cycles：单轮即 P1
    · 单槽 ≥2 源同时 error/timeout（齐活率成片塌陷）→ P1
    · 注入话术命中（脚本关键词扫，非 LLM 语义）→ P1
    · 执行 journal 已成交未入账 >15min：live → P1 人工；demo 自动
      重放失败/含糊/ordId 身份冲突 → P1
  audit-only（不推 QQ）：非必需源 stale、孤立单轮丢轮、push 未送达、周末稀疏、
      demo journal 自动重放成功（自愈留痕）。

降噪：
  · health_check 三态分流：全通=采集器性能超时(降级)／某源不可达=疑真断／工具自身不可用=分流不确定(不臆断)
  · Memory Dreaming 窗（03:00–03:45）：按**丢轮 cycle 槽时刻**落窗抑制（非 now），只抑丢轮/缺槽类、不抑真源断/注入
  · 已报去重（cycle 级）+ cooldown（无 cycle 型 3h）；状态文件原子写 + 损坏隔离；单实例锁防 cron 重叠

红线：全 DB mode=ro 只读；**只告警，绝不补派/重试/拉起/热改 registry**（≠watchdog）。
**唯一例外**：demo 执行 journal 未入账自动经
trades_writer --from-journal 补账本行——补的是账（写 demo_trades.db，经硬化 writer
合并闸幂等），非补派/拉起 agent；live 永远只 dry+P1 人工。
账本不变量命中只同步 account.db.repair_queue 元数据（幂等开/闭工单），不改交易事实。

用法：
  collection_monitor.py --db-root <PROJECT_ROOT>\db            # 真跑（超阈会真推 QQ）
  collection_monitor.py --db-root <PROJECT_ROOT>\db --dry-run  # 只检测+报告，不推 QQ、不写状态
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
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ledger_invariants as li

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CST = timezone(timedelta(hours=8))
PWSH = os.environ.get("OKX_PWSH_BIN", r"C:\Program Files\PowerShell\7\pwsh.exe")
WRAP = _project_path('scripts', 'run_okx_python.ps1')
STATE_DIR = Path(_project_path('logs', 'monitor'))
STATE_FILE = STATE_DIR / "alert_state.json"
AUDIT_FILE = STATE_DIR / "audit.jsonl"
LOCK_FILE = STATE_DIR / "monitor.lock"
_CREATE_NO_WINDOW = 0x08000000

DREAM_START, DREAM_END = (3, 0), (3, 45)     # Memory Dreaming 窗（CST，按 cycle 槽时刻判）
COOLDOWN_SEC = 3 * 3600                        # 无 cycle 型持续信号再报冷却
ERROR_STREAK_THRESHOLD = 3                      # 同源连续 error 轮数
SIMULTANEOUS_ERR_THRESHOLD = 2                  # 单槽同时 error 源数（成片塌陷）
PUSH_VERIFY_MIN_SEC, PUSH_VERIFY_MAX_SEC = 900, 2400
SLOT_MIN = 15
NEVER_DISPATCH_GRACE_SEC = SLOT_MIN * 60 + 900  # 采集齐后过窗判从未派（一个槽长 + 15min）
COMPLETION_LOOKBACK_SEC = 24 * 3600             # on-demand 运行也覆盖当日 trader 失败；cycle 去重防重复告警
TRADER_GRACE_SEC = 900                          # trader 派后给 15min 写 trade_cycles，超则判 launch-but-failed
UNIFIED_LIVE_GRACE_SEC = 1800                   # 合并分析+实盘首棒给 30min；full live/demo 仍 15min
JOURNAL_GRACE_SEC = 900                         # journal 落痕后给 15min 让 trader 正常喂 writer，超则判未入账
LOCK_STALE_SEC = 180                            # 锁陈旧阈（超此视为死锁可抢）
TRADES_WRITER = _project_path('collectors', 'trades_writer.py')
PROD_DB_ROOT = _project_path('db')                     # journal 自动重放仅限生产 root（writer DB_MAP 硬编码生产库）
# P13（2026-07-14）：trader launch-but-failed 三态归因的 audit 账本落点（OpenClaw 2026.7.1+；
# 表不存在/库不可读一律 fail-safe 回退单态口径，不影响告警本体）
OPENCLAW_STATE_DB = os.environ.get(
    "OKX_OPENCLAW_STATE_DB",
    str(_ProjectPath.home().joinpath('.openclaw', 'state', 'openclaw.sqlite')))
STAGE_STATUS_DIR = Path(os.environ.get(
    "OKX_STAGE_STATUS_DIR", _project_path('logs', 'stage-status')))

# 注入话术关键词（脚本级，非 LLM 语义）。匹配文本仅报"命中模式名"，不回灌原文（防自激）。
INJECTION_PATTERNS = {
    "proceed-without-asking": r"proceed without asking",
    "ignore-previous": r"ignore (all )?previous instruction",
    "override-system-prompt": r"override.{0,20}system prompt|覆盖.{0,6}系统提示",
    "disregard-instruction": r"disregard.{0,20}instruction",
    "ignore-prior-cn": r"无视.{0,4}(先前|之前|以上).{0,6}指令",
    "authorized-implied": r"授权.{0,4}隐含",
}
_INJ_RE = {name: re.compile(p, re.IGNORECASE) for name, p in INJECTION_PATTERNS.items()}


def now_cst() -> datetime:
    return datetime.now(CST)


def log(msg: str) -> None:
    print(f"[{now_cst():%Y-%m-%d %H:%M:%S}] MONITOR: {msg}", flush=True)


def _ro(db_root: str, name: str):
    p = Path(db_root) / name
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=8)
    con.row_factory = sqlite3.Row
    return con


def run_script(script: str, args: list, timeout: int = 90) -> tuple[int, str]:
    cmd = [PWSH, "-NoProfile", "-File", WRAP, script, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=_CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "")
    except Exception as e:
        return 99, f"__RUN_ERR__ {type(e).__name__}: {e}"


def _parse_json(out: str):
    try:
        return json.loads(out[out.index("{"):out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def _slot_dt(cycle_id: str):
    try:
        return datetime.strptime(str(cycle_id), "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    except (ValueError, TypeError):
        return None


def _cycle_in_dreaming(cycle_id: str) -> bool:
    dt = _slot_dt(cycle_id)
    if dt is None:
        return False
    return DREAM_START <= (dt.hour, dt.minute) <= DREAM_END


def _sig(key, sev, detail, *, cycles=None, source_like=False, audit_only=False,
         is_lost=False, force_alert_single=False):
    return {"key": key, "sev": sev, "detail": detail, "cycles": cycles or [],
            "cycle": (cycles[0] if cycles else None), "source_like": source_like,
            "audit_only": audit_only, "is_lost": is_lost,
            "force_alert_single": force_alert_single}


# ── 检测 ────────────────────────────────────────────────────────────────────
def detect_source_freshness(db_root: str) -> list[dict]:
    """B. 必需源断流/超时告警（abort_sources=required 缺 OR required present-but-stale）→ P0。
    非必需源 stale → audit-only，只标记调研不告警。"""
    rc, out = run_script(_project_path('scripts', 'source_freshness.py'), ["--db-root", db_root])
    data = _parse_json(out)
    if data is None:
        return [_sig("source_freshness_unrunnable", "P2",
                     f"source_freshness 无法解析 (rc={rc})", audit_only=True)]
    sigs = []
    abort = data.get("abort_sources") or []          # required 缺 ∪ required 超时
    if abort or data.get("should_abort"):
        mr = data.get("missing_required") or []
        sigs.append(_sig("required_source_down", "P0",
                         f"必需源断流/超时: abort={abort} missing={mr}（should_abort=True）",
                         source_like=True))
    # 非必需 stale（abort 之外的 stale）→ audit-only
    non_req_stale = [s for s in (data.get("stale") or []) if s not in abort]
    if non_req_stale:
        sigs.append(_sig("non_required_stale", "P2",
                         f"非必需源 stale（只标记调研，不告警）: {non_req_stale}",
                         source_like=True, audit_only=True))
    return sigs


def detect_error_streak(db_root: str) -> list[dict]:
    """C. 同源连续 ≥3 轮 error/timeout → P1。"""
    con = _ro(db_root, "ledger.db")
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT source, status FROM collection_runs ORDER BY rowid DESC LIMIT 300").fetchall()
    finally:
        con.close()
    per_src: dict[str, list] = {}
    for r in rows:
        per_src.setdefault(r["source"], []).append(str(r["status"] or "").lower())
    sigs = []
    for src, statuses in per_src.items():
        streak = 0
        for st in statuses:
            if st in ("error", "timeout", "fail", "failed"):
                streak += 1
            else:
                break
        if streak >= ERROR_STREAK_THRESHOLD:
            sigs.append(_sig(f"error_streak:{src}", "P1",
                             f"源 {src} 连续 {streak} 轮 error/timeout", source_like=True))
    return sigs


def detect_completion(db_root: str, now: datetime) -> list[dict]:
    """C2. 齐活率塌陷（单槽 ≥2 源同时 error/timeout）→ P1；
    D2. 派单缺口 form②（采集齐但 stage_dispatch 无分析首棒；历史 analyst/新 unified live）→ P1。"""
    con = _ro(db_root, "ledger.db")
    if con is None:
        return []
    sigs = []
    try:
        # 成片：最近 ~8 槽内，单 cycle_id 里 error/timeout 的 distinct 源数 ≥ 阈
        rows = con.execute(
            "SELECT cycle_id, source, status FROM collection_runs "
            "WHERE cycle_id NOT LIKE 'TEST-%' ORDER BY rowid DESC LIMIT 300").fetchall()
        by_cycle: dict[str, dict] = {}
        for r in rows:
            cy = r["cycle_id"]
            dt = _slot_dt(cy)
            if dt is None or (now - dt).total_seconds() > COMPLETION_LOOKBACK_SEC:
                continue  # 只看最近 2h，陈旧塌陷交每日复盘（避免反复触发已知历史丢轮）
            c = by_cycle.setdefault(cy, {"err": set(), "ok": set()})
            st = str(r["status"] or "").lower()
            (c["err"] if st in ("error", "timeout", "fail", "failed") else c["ok"]).add(r["source"])
        mass = [cy for cy, d in by_cycle.items() if len(d["err"]) >= SIMULTANEOUS_ERR_THRESHOLD]
        if mass:
            worst = max(mass, key=lambda cy: len(by_cycle[cy]["err"]))
            sigs.append(_sig("collect_mass_degrade", "P1",
                             f"齐活率塌陷: {len(mass)} 个槽单轮 ≥{SIMULTANEOUS_ERR_THRESHOLD} 源同时失败"
                             f"（最重 {worst}: {sorted(by_cycle[worst]['err'])}）"))
        # form②：市场采集成功但 stage_dispatch 无分析首棒、已过窗。
        # 要求 fast ok 而非任意 ok——采集不全时 dispatcher 本就正确不派（gate 挡），非丢轮 bug。
        collected = {cy for cy, d in by_cycle.items() if "fast" in d["ok"]}
        never = []
        for cy in collected:
            dt = _slot_dt(cy)
            if dt is None or (now - dt).total_seconds() <= NEVER_DISPATCH_GRACE_SEC:
                continue  # 未过窗，采集可能在路上/派发在即
            n = con.execute("SELECT COUNT(*) FROM stage_dispatch "
                            "WHERE cycle_id=? AND stage IN ('analyst','live')",
                            (cy,)).fetchone()[0]
            if n == 0:
                never.append(cy)
        if never:
            sigs.append(_sig("never_dispatched", "P1",
                             f"派单缺口 form②: {len(never)} 槽采集齐但从未派分析/实盘首棒且已过窗: "
                             f"{sorted(never)[:5]}", cycles=sorted(never), is_lost=True))
    finally:
        con.close()
    return sigs


def detect_lost_cycles(db_root: str) -> list[dict]:
    """D1. 丢轮 form① decision_fired_no_analysis（复用 query_state --check lost_cycles）→ P1。
    只消费 lost_cycles，**不用 --check all**（避免 news/account/cycle_fresh/regime 越权误报）。"""
    rc, out = run_script(_project_path('scripts', 'query_state.py'),
                         ["--check", "lost_cycles", "--db-root", db_root, "--json"])
    data = _parse_json(out)
    if data is None:
        return [_sig("lost_cycles_unrunnable", "P2",
                     f"query_state lost_cycles 无法解析 (rc={rc})", audit_only=True)]
    sigs = []
    for r in data.get("results", []):
        if r.get("name") == "lost_cycles" and r.get("status") == "FAIL":
            lc = r.get("lost_cycles") or []
            sigs.append(_sig("lost_cycles", "P1",
                             f"分析/实盘首棒已派 >12min 无 analysis 产出（form①）: {lc[:5]}",
                             cycles=lc, is_lost=True))
    return sigs


_ATTR_HINT = {
    "no-run": "no-run=起棒通道未产生会话(查tests契约测试/trigger日志)",
    "run-failed": "run-failed=LLM层失败/超时/中途死亡",
    "run-ok-no-db-row": "run-ok-no-db-row=agent跑完未落库(writer层或未调writer)",
    "audit-unavailable": "audit不可用(回退单态口径)",
}


def _audit_attribution(book: str, cyc: str) -> str:
    """优先读 stage_runner 确定性终态，再回退 OpenClaw audit_events 三态归因。

    P13（2026-07-14）：直读 OpenClaw audit_events（metadata-only 账本，mode=ro）
    判 launch-but-failed 属哪层。session_key 复刻 trigger_agent.session_key 规则
    （例如 `<SESSION_KEY>`）。任何异常返 'audit-unavailable'（fail-safe）。"""
    status_path = STAGE_STATUS_DIR / f"{book}-{cyc.replace(':', '-')}.json"
    try:
        runner = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        runner = {}
    runner_status = runner.get("status")
    if (runner_status == "failed"
            and runner.get("failure_kind") == "business_output_missing"):
        return "run-ok-no-db-row"
    if runner_status in ("failed", "running"):
        return "run-failed"
    if runner_status == "succeeded":
        return "run-ok-no-db-row"

    safe = cyc.replace("-", "").replace(":", "").replace("T", "-")
    sk = f"agent:okx-{book}-trader:{book}-{safe}"
    try:
        con = sqlite3.connect(f"file:{OPENCLAW_STATE_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT action, status FROM audit_events WHERE session_key=? "
                "ORDER BY sequence", (sk,)).fetchall()
        finally:
            con.close()
    except Exception:
        return "audit-unavailable"
    if not rows:
        return "no-run"
    if any((r[1] or "") in ("failed", "timed_out", "blocked", "error") for r in rows):
        return "run-failed"
    if str(rows[-1][0] or "").endswith("started"):
        return "run-failed"  # 最后动作有始无终=死在执行中途（如 gateway 重启杀会话）
    return "run-ok-no-db-row"


def detect_trader_incomplete(db_root: str, now: datetime) -> list[dict]:
    """D3. trader launch-but-failed（架构评审 #7）：stage_dispatch 显示 live/demo 已派
    （>15min 前）但该盘 trade_cycles 无该 cycle 行 = trader 起来后崩在写库前，该轮交易决策
    静默蒸发。本检测提供与 push 送达核验对称的 trader 落库核验。
    →丢轮 P1，alert-only 不补派（守红线）。
    P13（2026-07-14）：每条缺行经 audit_events 三态归因（no-run/run-failed/run-ok-no-db-row）；
    同盘 ≥2 轮 run-ok-no-db-row 按本模块常量口径升 P0。"""
    con = _ro(db_root, "ledger.db")
    if con is None:
        return []
    lo = (now - timedelta(seconds=COMPLETION_LOOKBACK_SEC)).strftime("%Y-%m-%d %H:%M:%S")
    hi = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = con.execute(
            "SELECT s.cycle_id, s.stage, s.dispatched_at, "
            "EXISTS(SELECT 1 FROM stage_dispatch a "
            "       WHERE a.cycle_id=s.cycle_id AND a.stage='analyst') AS legacy_cycle "
            "FROM stage_dispatch s WHERE s.stage IN ('live','demo') "
            "AND s.dispatched_at>=? AND s.dispatched_at<=? AND s.cycle_id NOT LIKE 'TEST-%'",
            (lo, hi)).fetchall()
    finally:
        con.close()
    missing = []
    for r in rows:
        cyc, book = r["cycle_id"], r["stage"]
        try:
            dispatched = datetime.strptime(
                str(r["dispatched_at"])[:19], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=CST)
            grace = (UNIFIED_LIVE_GRACE_SEC
                     if book == "live" and not r["legacy_cycle"]
                     else TRADER_GRACE_SEC)
            if (now - dispatched).total_seconds() <= grace:
                continue
        except Exception:
            continue
        bcon = _ro(db_root, f"{book}_trades.db")
        if bcon is None:
            continue
        try:
            n = bcon.execute("SELECT COUNT(*) FROM trade_cycles WHERE cycle_id=?",
                             (cyc,)).fetchone()[0]
        except sqlite3.OperationalError:
            n = 1  # 表异常不误报
        finally:
            bcon.close()
        if n == 0:
            missing.append((book, cyc, _audit_attribution(book, cyc)))
    if not missing:
        return []
    cycles = sorted({cyc for _, cyc, _ in missing})
    # 同盘 ≥2 轮 run-ok-no-db-row → 升 P0（本函数确定性口径）
    wr_by_book: dict[str, int] = {}
    for book, _, attr in missing:
        if attr == "run-ok-no-db-row":
            wr_by_book[book] = wr_by_book.get(book, 0) + 1
    sev = "P0" if any(v >= 2 for v in wr_by_book.values()) else "P1"
    shown = [f"{b}:{c}[{a}]" for b, c, a in missing[:6]]
    hints = "；".join(sorted({_ATTR_HINT[a] for _, _, a in missing}))
    return [_sig("trader_incomplete", sev,
                 f"trader 已派 >15min 但 trade_cycles 缺行（launch-but-failed，决策蒸发）: "
                 f"{shown}；归因：{hints}", cycles=cycles, is_lost=True,
                 force_alert_single=True)]


def detect_push_undelivered(db_root: str, now: datetime) -> list[dict]:
    """E. push 派发后 [15,40]min 窗内 qq_push_dedupe 无送达记录 → **audit-only**（不推 QQ，
    送达判定按时间窗而非 cycle 指纹，可能误判，因此仅作观察。"""
    con = _ro(db_root, "ledger.db")
    if con is None:
        return []
    lo = (now - timedelta(seconds=PUSH_VERIFY_MAX_SEC)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (now - timedelta(seconds=PUSH_VERIFY_MIN_SEC)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = con.execute(
            "SELECT cycle_id, dispatched_at FROM stage_dispatch WHERE stage='push' "
            "AND dispatched_at>=? AND dispatched_at<=? ORDER BY dispatched_at",
            (lo, hi)).fetchall()
    finally:
        con.close()
    if not rows:
        return []
    dcon = _ro(db_root, "qq_push_dedupe.db")
    sigs = []
    for r in rows:
        cyc, at = r["cycle_id"], r["dispatched_at"]
        delivered = False
        if dcon is not None:
            try:
                delivered = dcon.execute(
                    "SELECT 1 FROM sent WHERE status='sent' AND updated_at>=? LIMIT 1",
                    (at,)).fetchone() is not None
            except sqlite3.OperationalError:
                delivered = True
        if not delivered:
            sigs.append(_sig(f"push_undelivered:{cyc}", "P2",
                             f"push 派于 {at} 后 15min 内无送达记录（观察项）",
                             cycles=[cyc], audit_only=True))
    if dcon is not None:
        dcon.close()
    return sigs


def detect_injection() -> list[dict]:
    """F. 注入话术脚本扫描：只扫 logs/trigger（**排除 logs/monitor 自身防自激**）；
    命中只报模式名 + 文件，**不回灌匹配原文** → P1。"""
    sigs = []
    d = Path(_project_path('logs', 'trigger'))
    if not d.exists():
        return sigs
    files = sorted(d.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
    for f in files:
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")[-8000:]
        except OSError:
            continue
        hits = [name for name, rx in _INJ_RE.items() if rx.search(txt)]
        if hits:
            sigs.append(_sig(f"injection:{f.name}", "P1",
                             f"日志 {f.name} 命中注入话术模式 {hits}（脚本扫描，需人工核实，原文见该文件）",
                             source_like=False))
    return sigs


def detect_journal_unaccounted(db_root: str, now: datetime,
                               dry_run: bool = False) -> list[dict]:
    """H. order_executor 执行 journal ↔ 账本对账。

    journal=成交即留痕（executor 成功路径同进程落 JSONL）；落痕 >15min 仍未入账
    =trader 会话在成交后、喂 writer 前被杀。
    处置分级：demo → 自动 trades_writer --from-journal 重放
    （成功=audit-only 自愈留痕，失败=P1）；live → 永远只 P1 附重放命令，人工核实执行。
    检测复用 writer --replay-dry-run（监控与重放同一套过滤/归因逻辑，不分叉）；
    无 ordId 的记录（如 recovered_timeout）两盘一律 P1 人工——含糊面不自动动账。
    本函数使 monitor 成为 demo_trades.db 第二写入方：低碰撞（:09/:39 vs trader
    ~:03-:25）+ WAL + 合并闸幂等可容。"""
    sigs: list[dict] = []
    jdir = Path(os.environ.get("OKX_EXEC_JOURNAL_DIR") or (Path(db_root) / "journal"))
    # writer 的 DB_MAP 硬编码生产库：db_root 指向副本时扫的是
    # 副本 journal、写的却是生产账本（拿测试副本的 journal 污染真账）。非生产 root
    # 一律降级 report-only，绝不自动重放。
    prod_root = str(Path(db_root).resolve()).lower() == PROD_DB_ROOT
    for profile in ("live", "demo"):
        jf = jdir / f"exec_{profile}.jsonl"
        if not jf.exists():
            continue
        rc, out = run_script(TRADES_WRITER, ["--from-journal", str(jf),
                                             "--profile", profile, "--replay-dry-run"])
        plan = _parse_json(out)
        if rc != 0 or not isinstance(plan, dict):
            sigs.append(_sig(f"journal_scan_unrunnable:{profile}", "P2",
                             f"journal 扫描不可用 profile={profile} rc={rc}",
                             audit_only=True))
            continue
        # 核验修：撕裂/坏行 = 安全网上有洞（坏行里可能藏着真实成交）→ P1 人工查文件
        if plan.get("bad_lines"):
            sigs.append(_sig(f"journal_corrupt:{profile}", "P1",
                             f"journal 含 {plan['bad_lines']} 条无法解析行"
                             f"（可能藏未入账成交）: {jf}｜人工查该文件"))
        conflicts = plan.get("identity_conflicts") or []
        if conflicts:
            cycles = sorted({str(c.get("cycle") or "") for c in conflicts
                             if c.get("cycle")})
            shown = "; ".join(
                f"{c.get('cycle')} {c.get('symbol')} sz={c.get('sz')} "
                f"journal={c.get('journal_ordId')} ledger={c.get('ledger_ordId')}"
                for c in conflicts[:6]
            )
            sigs.append(_sig(
                f"journal_identity_conflict:{profile}", "P1",
                f"{profile} journal/账本 ordId 身份冲突 {len(conflicts)} 笔，"
                f"重放已硬阻断: {shown}｜禁止重放，人工核交易所订单真值",
                cycles=cycles,
            ))
        entries = [dict(e, cycle=cyc)
                   for cyc, es in (plan.get("plan") or {}).items() for e in es]
        aged = []
        for e in entries:
            try:
                ets = datetime.strptime(str(e.get("ts"))[:19],
                                        "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
                if (now - ets).total_seconds() >= JOURNAL_GRACE_SEC:
                    aged.append(e)
            except (ValueError, TypeError):
                aged.append(e)  # ts 缺/坏按已超龄（保守方向=告警）
        if not aged:
            continue

        def _desc(es):
            return "; ".join(
                f"{e.get('cycle')} {e.get('symbol')} {e.get('action')} "
                f"sz={e.get('sz')} ordId={e.get('ordId')}"
                + ("[unwind]" if e.get("unwind") else "") for e in es[:6])

        hint = (f"｜人工核实后重放: trades_writer.py --from-journal {jf} "
                f"--profile {profile}（先 --replay-dry-run）")
        if profile == "live":
            sigs.append(_sig("journal_unaccounted:live", "P1",
                             f"live 已成交未入账 {len(aged)} 笔(>15min): "
                             f"{_desc(aged)}{hint}"))
            continue
        # demo 分流（核验修：混批不瘫痪自愈——含糊面 P1、可自动面照常重放）：
        #   无 ordId（recovered_timeout 等）/ unwind（close-without-open）→ P1 人工；
        #   带真 ordId 且非 unwind → 逐笔 --ordid 自动重放。
        manual = [e for e in aged if not e.get("ordId") or e.get("unwind")]
        autoable = [e for e in aged if e.get("ordId") and not e.get("unwind")]
        if manual:
            sigs.append(_sig("journal_unaccounted:demo_manual", "P1",
                             f"demo 含糊记录(无ordId/unwind)未入账 {len(manual)} 笔: "
                             f"{_desc(manual)}{hint}"))
        if not autoable:
            continue
        if dry_run or not prod_root:
            why = "[dry-run]" if dry_run else f"[非生产 db-root {db_root}，禁自动动账]"
            sigs.append(_sig("journal_unaccounted:demo", "P2",
                             f"{why} demo 将自动重放 {len(autoable)} 笔: {_desc(autoable)}",
                             audit_only=dry_run))
            continue
        failed = []
        for e in autoable:
            rc2, out2 = run_script(TRADES_WRITER, ["--from-journal", str(jf),
                                                   "--profile", "demo",
                                                   "--ordid", str(e["ordId"])])
            res2 = _parse_json(out2)
            if rc2 != 0 or not (res2 or {}).get("ok"):
                failed.append(f"{e.get('symbol')} ordId={e.get('ordId')} rc={rc2}")
        if failed:
            sigs.append(_sig("journal_unaccounted:demo", "P1",
                             f"demo journal 自动重放失败 {len(failed)}/{len(autoable)}: "
                             f"{'; '.join(failed[:4])}｜需人工处置"))
        else:
            sigs.append(_sig("journal_replayed:demo", "P2",
                             f"demo journal 自动重放成功 {len(autoable)} 笔: "
                             f"{_desc(autoable)}", audit_only=True))
    return sigs


def detect_ledger_invariants(
    db_root: str, now: datetime
) -> tuple[list[dict], list[dict]]:
    """I. 主账负净额、重复成交及未决执行意图；只读事实检测。"""
    root = Path(db_root)
    since = (now - timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
    findings: list[dict] = []
    for profile in ("live", "demo"):
        findings.extend(li.trade_net_findings(root, profile))
        findings.extend(li.duplicate_execution_findings(root, profile, since))
        findings.extend(li.execution_intent_findings(root, profile, now))
    sigs = []
    for finding in findings:
        cycle = finding.get("cycle_id")
        sigs.append(_sig(
            finding["check_name"], "P1", finding["issue"],
            cycles=[cycle] if cycle else None,
            force_alert_single=True))
    return sigs, findings


# ── 降噪 ────────────────────────────────────────────────────────────────────
def triage_source_signals(sigs: list[dict]) -> None:
    """源类信号 health_check 三态分流：0=全通降级perf／1=某源不可达标疑真断／99=工具不可用标不确定。"""
    if not any(s.get("source_like") and not s.get("audit_only") for s in sigs):
        return
    rc, _ = run_script(_project_path('scripts', 'health_check.py'), [], timeout=60)
    for s in sigs:
        if not s.get("source_like"):
            continue
        if rc == 0:
            s["detail"] += "｜health_check 全通=采集器性能超时非网络断，已降级"
            s["sev"] = {"P0": "P1", "P1": "P2"}.get(s["sev"], s["sev"])
            s["perf_timeout"] = True
        elif rc == 99:
            s["detail"] += "｜health_check 自身不可用(rc=99)，分流不确定、维持原级需人工核"
        else:
            s["detail"] += f"｜health_check 未全通(rc={rc})，未能排除真断，需人工核"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            try:  # 损坏隔离：备份坏文件供排查，返回空（下轮重建，去重态一次性丢=可接受）
                STATE_FILE.replace(STATE_DIR / f"alert_state.corrupt.{now_cst():%Y%m%d_%H%M%S}.json")
            except OSError:
                pass
            log("alert_state.json 损坏，已隔离备份，本轮按空态处理")
    return {}


def save_state(state: dict) -> None:
    """原子写：temp + os.replace，防进程被杀/并发写截断成坏 JSON。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), prefix=".alert_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def acquire_lock(now: datetime) -> bool:
    """单实例锁防 cron 重叠双发。陈旧锁（>LOCK_STALE_SEC）可抢。返回是否取得。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOCK_FILE.exists():
            age = now.timestamp() - LOCK_FILE.stat().st_mtime
            if age < LOCK_STALE_SEC:
                return False
        LOCK_FILE.write_text(f"{os.getpid()} {now:%Y-%m-%d %H:%M:%S}", encoding="utf-8")
        return True
    except OSError:
        return True  # 锁机制故障不阻断监控（fail-open，宁可偶发重叠也别漏检）


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def denoise(sigs: list[dict], state: dict, now: datetime) -> tuple[list[dict], list[dict]]:
    """audit-only 抑制 / Dreaming 按丢轮槽时刻剔除 / 孤立丢轮静默 / 已报去重 + cooldown。"""
    to_alert, suppressed = [], []
    now_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    for s in sigs:
        # audit-only（非必需 stale / push 未送达 / 无法解析）→ 只记录不告警
        if s.get("audit_only") or s["sev"] == "P2":
            s["suppressed_reason"] = "audit-only（不推 QQ）"
            suppressed.append(s)
            continue
        # 丢轮/缺槽类：按 cycle 槽时刻剔 Dreaming 窗内的，再判 ≥2（Dreaming 只作用丢轮类）
        if s.get("is_lost"):
            # trader_incomplete 是“已派后失败”的确定性故障，不属于 Dreaming 计划性缺槽；
            # 即使 cycle 恰落 Dreaming 窗也必须保留，且单轮即告警。
            force_single = bool(s.get("force_alert_single"))
            fresh = (list(s["cycles"]) if force_single
                     else [c for c in s["cycles"] if not _cycle_in_dreaming(c)])
            dreamed = ([] if force_single
                       else [c for c in s["cycles"] if _cycle_in_dreaming(c)])
            if dreamed:
                s["detail"] += f"｜known-dreaming 剔除 {len(dreamed)} 槽: {dreamed[:3]}"
            if len(fresh) < 2 and not force_single:
                s["suppressed_reason"] = (f"孤立丢轮（去 Dreaming 后 {len(fresh)}<2 静默记 audit）"
                                          if not dreamed or fresh else "全部 known-dreaming")
                suppressed.append(s)
                continue
            s["cycles"] = fresh
            s["cycle"] = fresh[0]
        # 已报去重（cycle 型：同一 cycle 集不重复；用最小未报 cycle 判）
        st = state.get(s["key"], {})
        if s.get("cycle"):
            if st.get("last_cycle") == s["cycle"] and set(st.get("last_cycles") or []) == set(s["cycles"]):
                s["suppressed_reason"] = f"已报同批 {s['cycle']}"
                suppressed.append(s)
                continue
        else:  # 无 cycle 型：cooldown
            last_ts = st.get("last_alert_ts")
            if last_ts:
                try:
                    age = (now - datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)).total_seconds()
                    if age < COOLDOWN_SEC:
                        s["suppressed_reason"] = f"cooldown 内（{int(age)}s<{COOLDOWN_SEC}s）"
                        suppressed.append(s)
                        continue
                except (ValueError, TypeError):
                    pass
        to_alert.append(s)
    for s in to_alert:
        state[s["key"]] = {"last_cycle": s.get("cycle"), "last_cycles": s.get("cycles"),
                           "last_alert_ts": now_ts, "sev": s["sev"]}
    return to_alert, suppressed


# ── 告警 ────────────────────────────────────────────────────────────────────
def build_alert(sigs: list[dict], now: datetime) -> str:
    order = {"P0": 0, "P1": 1, "P2": 2}
    sigs = sorted(sigs, key=lambda s: order.get(s["sev"], 9))
    top = sigs[0]["sev"]
    lines = [f"⚠️ OKX 采集/派单/推送监控 [{top}] @ {now:%Y-%m-%d %H:%M} (统一QQ告警)",
             f"命中 {len(sigs)} 项："]
    for s in sigs:
        lines.append(f"· [{s['sev']}] {s['detail']}")
    lines.append("（脚本级监控·只告警不自动补派/重试，需人工核实处置）")
    return "\n".join(lines)


def send_alert(text: str) -> tuple[int, str]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 显式 --dedupe-key（批次戳=每批告警必达）；语义级重复抑制由本脚本
    # alert_state.json denoise 层负责。
    stamp = f"{now_cst():%Y%m%d_%H%M%S}"
    f = STATE_DIR / f"alert_{stamp}.txt"
    f.write_text(text, encoding="utf-8")
    rc, out = run_script(_project_path('scripts', 'qq_push.py'),
                         ["--content-file", str(f), "--dedupe-key", f"monitor:{stamp}"],
                         timeout=60)
    return rc, out[:400]


def audit(record: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="within-day 采集/派单/推送健康监控（纯脚本）")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--dry-run", action="store_true", help="只检测+报告，不推 QQ、不写状态")
    args = ap.parse_args()
    now = now_cst()

    if not args.dry_run and not acquire_lock(now):
        log("另一 monitor 实例运行中（锁未陈旧），本轮跳过")
        return 0
    try:
        sigs: list[dict] = []
        invariant_findings: list[dict] = []
        for fn, need_now in ((detect_source_freshness, False), (detect_error_streak, False),
                             (detect_completion, True), (detect_lost_cycles, False),
                             (detect_trader_incomplete, True),
                             (detect_push_undelivered, True), (lambda *_: detect_injection(), False)):
            try:
                sigs.extend(fn(args.db_root, now) if need_now else fn(args.db_root))
            except Exception as e:
                log(f"{getattr(fn, '__name__', 'detect')} 异常（跳过）: {e}")
        # journal 对账单独接入；--dry-run 下绝不真重放。
        try:
            sigs.extend(detect_journal_unaccounted(args.db_root, now, dry_run=args.dry_run))
        except Exception as e:
            log(f"detect_journal_unaccounted 异常（跳过）: {e}")
        try:
            invariant_sigs, invariant_findings = detect_ledger_invariants(
                args.db_root, now)
            sigs.extend(invariant_sigs)
        except Exception as e:
            log(f"detect_ledger_invariants 异常（跳过）: {e}")

        queue_sync = None
        if not args.dry_run:
            try:
                account = sqlite3.connect(
                    str(Path(args.db_root) / "account.db"), timeout=8)
                try:
                    account.execute("BEGIN IMMEDIATE")
                    queue_sync = li.sync_repair_queue(
                        account, family_prefix="ledger_invariant:",
                        findings=invariant_findings,
                        ts=now.strftime("%Y-%m-%d %H:%M:%S"))
                    account.commit()
                except Exception:
                    account.rollback()
                    raise
                finally:
                    account.close()
            except Exception as e:
                log(f"repair_queue 不变量同步失败（不影响告警）: {e}")

        triage_source_signals(sigs)
        state = load_state()
        to_alert, suppressed = denoise(sigs, state, now)

        report = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "detected": len(sigs),
            "to_alert": [{"key": s["key"], "sev": s["sev"], "detail": s["detail"]} for s in to_alert],
            "suppressed": [{"key": s["key"], "reason": s.get("suppressed_reason")} for s in suppressed],
            "dry_run": args.dry_run,
            "repair_queue": queue_sync,
        }

        if to_alert and not args.dry_run:
            rc, out = send_alert(build_alert(to_alert, now))
            report["alert_sent"] = {"rc": rc, "out": out}
            if rc == 0 or "messageid" in out.lower():   # 送达（含偶发 rc=1 带 messageId）才固化去重
                save_state(state)
            log(f"告警已推 rc={rc}: {len(to_alert)} 项")
        elif to_alert and args.dry_run:
            report["alert_sent"] = {"dry_run": True, "would_send": build_alert(to_alert, now)}
            log(f"[dry-run] 将告警 {len(to_alert)} 项（未推、未写状态）")
        else:
            log(f"无需告警（检测 {len(sigs)} / 抑制 {len(suppressed)}）")

        # dry-run 契约：只检测并打印；不写去重状态、不写审计文件。
        if not args.dry_run:
            audit(report)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    finally:
        if not args.dry_run:
            release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
