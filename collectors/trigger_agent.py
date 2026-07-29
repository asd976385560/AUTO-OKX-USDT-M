# -*- coding: utf-8 -*-
"""V2.0 起棒适配层：用 `openclaw agent` 拉起 stage 对应 agent 的一个 turn。

现役定位：**唯一 caller = core/dispatcher.py**（_fire_stage 抢 stage_dispatch 闩锁后调
fire()）；本模块集中管理 agent-id / 分级 timeout / --message-file 主路径，自身不做幂等
（幂等真值 = ledger.stage_dispatch）。

Agent stage（analyst/live/demo）与纯脚本 push stage 均由 core/dispatcher.py 确定性起棒。

三条设计红线
------------
1. **零模型名**（红线 #8）：本模块只含 agent-id / session-key（路由标识，非模型）。
   模型分配只在 `openclaw config agents.list.<id>.model`，本桥永不碰。
2. **每 cycle 独立 session**：session-key 带 cycle 槽位 → 每轮每 agent 一个新会话，
   单轮跑完即弃，避免持久会话发生 context overflow。
   所有跨轮状态在 DB（analysis.db / *_trades.db），不靠会话记忆。
3. **detached 异步启动**：闩锁赢家立即返回，不阻塞等 agent turn 跑完——采集脚本有
   硬超时（快采 ≤240s），analyst turn 可能数分钟。Gateway 服务端跑 turn，CLI 客户端
   detached 退出不影响。

用法
----
作为库（现役主路径，core/dispatcher._fire_stage 调）：
    import trigger_agent
    trigger_agent.fire(stage, cycle_id, mode)   # -> session-key

作为 CLI（人工排障/补起单棒用）：
    python trigger_agent.py --stage live --cycle 2026-06-18T14:00
    python trigger_agent.py --stage demo --cycle 2026-06-18T14:00
    python trigger_agent.py --stage push --cycle 2026-06-18T14:00  # 永久走 push_pipeline.py

环境变量（覆盖默认 agent-id / 二进制 / dry-run）：
    OKX_ANALYST_AGENT  OKX_LIVE_AGENT  OKX_DEMO_AGENT
    OKX_OPENCLAW_BIN（默认 'openclaw'）
    OKX_LAUNCH_PROBE_S（默认 3 秒；检测子进程启动后立即非零退出）
    OKX_STAGE_RUNNER / OKX_STAGE_STATUS_DIR（终态监督脚本 / 状态目录）
    OKX_TRIGGER_DRYRUN=1（不真起 agent，只把命令写日志，用于 tmp 验证 plumbing）
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
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if _project_path() not in sys.path:
    sys.path.insert(0, _project_path())
from core.decision_card import compact_text  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))

# stage -> agent-id。env 可覆盖。
# 主链由 okx-live-trader 合并承担分析+实盘；analyst 映射只服务主人明确要求的人工回滚。
# push 不属于 Agent 路由；fire(stage="push") 无条件起纯脚本管道。
STAGE_AGENTS = {
    "analyst": os.environ.get("OKX_ANALYST_AGENT", "okx-analyst"),
    "live": os.environ.get("OKX_LIVE_AGENT", "okx-live-trader"),
    "demo": os.environ.get("OKX_DEMO_AGENT", "okx-demo-trader"),
}

# 显式 agent turn 超时（秒）：人工回滚 analyst 900；full live/demo 720；
# push 纯脚本不使用 Agent timeout。
# 勿盲目加大：超时=挂死会话占据 gateway lane 的上限（拥塞治理约束）。
STAGE_TIMEOUTS = {
    "analyst": int(os.environ.get("OKX_ANALYST_TIMEOUT_S", "900")),
    "live": int(os.environ.get("OKX_TRADER_TIMEOUT_S", "720")),
    "demo": int(os.environ.get("OKX_TRADER_TIMEOUT_S", "720")),
}
# 合并轮同时做 gate、分析落库、实盘执行和交易落库，独立给 1500s；
# 人工回滚后的 full live 沿用上面的 720s。
UNIFIED_LIVE_TIMEOUT = int(os.environ.get("OKX_UNIFIED_LIVE_TIMEOUT_S", "1500"))

# 直起 node + openclaw.mjs（绕开 openclaw.cmd）：
#   ① 不经 cmd.exe → 无控制台弹窗（CREATE_NO_WINDOW 对 .cmd 仍会闪 cmd 窗）；
#   ② 消息主路径使用 --message-file（UTF-8 文件官方契约 + 每轮落盘审计，
#     见 _write_message_file）；中文 --message 仅作为
#     文件写失败时的 argv 兜底路径依据。
_NODE_BIN = os.environ.get("OKX_NODE_BIN", r"C:\Program Files\nodejs\node.exe")
_OPENCLAW_MJS = os.environ.get(
    "OKX_OPENCLAW_MJS",
    str(_ProjectPath.home().joinpath('AppData', 'Roaming', 'npm', 'node_modules', 'openclaw', 'openclaw.mjs')),
)
# 兼容：设了 OKX_OPENCLAW_BIN 则仍用单一 bin（自定义 wrapper）；否则走 node+mjs。
OPENCLAW_BIN = os.environ.get("OKX_OPENCLAW_BIN", "")

# push stage 固定执行纯脚本 push_pipeline.py，避免 LLM 临场拼装报告产生结构漂移。
# 起法用 **python.exe 直起**（原生 exe，同 node 路径可 DETACHED 存活）——不经 pwsh wrapper：
#   pwsh 跑 .ps1 在 DETACHED_PROCESS 下可能静默不执行。
#   push_pipeline 自身只读库 + 内部各步仍走 wrapper（拿 UTF-8/PYTHONPATH/MX_APIKEY），故裸 python 起足够。
_PUSH_PIPELINE = os.environ.get("OKX_PUSH_PIPELINE", _project_path('scripts', 'push_pipeline.py'))
_PYTHON_EXE = os.environ.get("OKX_PYTHON_BIN", sys.executable)
_STAGE_RUNNER = os.environ.get(
    "OKX_STAGE_RUNNER", _project_path('scripts', 'stage_runner.py'))
_STAGE_STATUS_DIR = Path(os.environ.get(
    "OKX_STAGE_STATUS_DIR", _project_path('logs', 'stage-status')))
_OKX_DB_ROOT = os.environ.get("OKX_DB_ROOT", _project_path('db'))


def _launcher() -> list[str]:
    """起棒命令前缀：优先 node + openclaw.mjs；env 覆盖或 mjs 缺失时兜底。"""
    if OPENCLAW_BIN:
        return [OPENCLAW_BIN]
    if Path(_OPENCLAW_MJS).exists() and Path(_NODE_BIN).exists():
        # --stack-size=8192：node 一开始就带大栈，OpenClaw entry.js 的 hasStackSizeConfigured()
        # 检测到即**不 re-spawn worker**——worker re-spawn 没设 windowsHide、Windows 下 detached 被强制
        # false，会自建控制台被 Windows Terminal DefTerm 弹「openclaw-agent」窗。绕过 respawn = 单进程
        # （配合 fire() 的 DETACHED 无控制台）= 不弹窗。见 entry.js:86-90 spawn + :290 hasStackSizeConfigured。
        return [_NODE_BIN, "--stack-size=8192", _OPENCLAW_MJS]
    # 兜底（弹窗/坏码风险）：仅当 node/mjs 缺失
    return [str(_ProjectPath.home().joinpath('AppData', 'Roaming', 'npm', 'openclaw.cmd'))]


LOG_DIR = Path(_project_path('logs', 'trigger'))

# Windows detached 启动标志：子进程脱离父，父退出不带走它。
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _launch_probe_seconds() -> float:
    try:
        return min(max(float(os.environ.get("OKX_LAUNCH_PROBE_S", "3")), 0.0), 10.0)
    except (TypeError, ValueError):
        return 3.0


def _probe_launch(proc: subprocess.Popen, stage: str, cycle_id: str, fh) -> None:
    """短暂观察 detached 子进程；启动期非零退出必须冒泡给 dispatcher 释放闩锁。

    超过探针窗口仍在运行即视为已正常进入主流程，后续完成仍由 DB 生命周期记录判定，
    不把异步 turn 改成同步等待。
    """
    probe_s = _launch_probe_seconds()
    try:
        rc = proc.wait(timeout=probe_s)
    except subprocess.TimeoutExpired:
        return
    fh.write(f"  launch_probe: process exited during {probe_s:g}s probe rc={rc}\n")
    fh.flush()
    if rc != 0:
        # supervised runner 已成功启动且明确记录 child=failed 时，属于业务终态失败：
        # 闩锁必须保留（只告警不重试），不能让 dispatcher 当“起棒失败”释放后重派。
        status_path = _STAGE_STATUS_DIR / f"{stage}-{cycle_id.replace(':', '-')}.json"
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            state = {}
        if (state.get("status") == "failed"
                and int(state.get("runner_pid") or -1) == int(proc.pid)):
            fh.write("  launch_probe: supervised child failed; stage latch retained "
                     "(alert-only, no retry)\n")
            fh.flush()
            return
        raise RuntimeError(
            f"{stage} cycle={cycle_id} child exited during launch probe rc={rc}"
        )


def _supervised_cmd(stage: str, cycle_id: str, mode: str,
                    command: list[str]) -> list[str]:
    """独立 runner 等待 detached 真子进程并持久化终态；不负责释放闩锁或重试。"""
    return [
        _PYTHON_EXE, _STAGE_RUNNER,
        "--stage", stage, "--cycle", cycle_id, "--mode", mode,
        "--", *command,
    ]


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _safe_cycle(cycle_id: str) -> str:
    """'2026-06-18T14:00' -> '20260618-1400'（session-key 不含冒号，避免分段歧义）。"""
    return cycle_id.replace("-", "").replace(":", "").replace("T", "-")


def session_key(stage: str, cycle_id: str) -> str:
    """bare key（不带 agent: 前缀）；交给 openclaw --agent 拼成 agent:<id>:<key>。

    调用方不得自行拼接 agent 前缀，否则会形成重复前缀并导致 setup timeout。
    """
    return f"{stage}-{_safe_cycle(cycle_id)}"


# 操作手册＝各 agent 的 workspace AGENTS.md（OpenClaw 每轮自动加载）；fire 消息只指 AGENTS.md。


_DB_ROOT = Path(os.environ.get("OKX_DB_ROOT", _project_path('db')))


def _run_briefing() -> str:
    """跑一次 decision_briefing（五库简报）。失败返回空串（agent 退回自查，不阻断）。"""
    try:
        brief_py = Path(__file__).parent.parent / "scripts" / "decision_briefing.py"
        p = subprocess.run(
            [sys.executable, str(brief_py), "--db-root", str(_DB_ROOT)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=_CREATE_NO_WINDOW,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _analyst_briefing(cycle_id: str) -> str:
    """为 analyst 预读 decision_briefing 塞进 fire 消息，省去 analyst 临场摸库（降时延）。"""
    return _run_briefing()


def _briefing_for_traders(cycle_id: str) -> str:
    """trader 预载简报——每 cycle 只真跑一次，live/demo 共享文件缓存（同 tick 顺序起两棒，
    第二棒直接读缓存，避免 2×60s 最坏）。

    与 analyst 预载**刻意不共缓存**：analyst 简报生成于分析之前（无本轮 signals）；
    trader 派发时本轮 analysis 已落库，预载决策卡与历史正反样本，减少重复摸库
    。缓存 logs/trigger/briefing-<cycle>-trader.txt，
    随 log_rotate 每日轮转回收。全程 fail-safe：缓存读写失败照常直跑/直用。"""
    cache = LOG_DIR / f"briefing-{_safe_cycle(cycle_id)}-trader.txt"
    try:
        if cache.exists() and cache.stat().st_size > 0:
            return cache.read_text(encoding="utf-8")
    except OSError:
        pass
    brief = _run_briefing()
    if brief:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(brief, encoding="utf-8", newline="\n")
        except OSError:
            pass
    return brief


def _ro_db(name: str) -> sqlite3.Connection | None:
    p = _DB_ROOT / name
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _trader_preload(cycle_id: str, stage: str) -> str:
    """live/demo 触发消息预载块。

    dispatcher 起 trader 前已核过 analysis 就绪/新鲜/status=ok——把已证事实与分析内容
    直接塞进触发消息，消 trader 冷启动逐库自查（demo 失败调用 2.6x 于 live 的主源=
    开场摸库列名瞎猜）。每块独立 fail-safe：取不到→留显式缺块标记，trader 按 AGENTS.md
    自取兜底，绝不阻断派发。**OKX API 现仓/余额刻意不预载**——现仓唯一权威=交易所 API
    且随时变（SL 成交/手动平仓），预载快照会诱导 agent 跳过 API 真查，而
    risk_validator 的 open_positions 必须是下单现场真值（红线）。"""
    parts: list[str] = []
    # ①② 派发确认 + 分析预读（analysis.db ro；dispatcher 刚核过 status=ok 才会走到这）
    try:
        con = _ro_db("analysis.db")
        run = con.execute(
            "SELECT ts, status, mode, regime, regime_stale, market_summary, "
            "missing_sources FROM analysis_runs WHERE cycle_id=?",
            (cycle_id,)).fetchone()
        sigs = con.execute(
            "SELECT symbol, total, action, side, confidence, entry_hint, stop_hint, "
            "tp_hint, reasoning, decision_card FROM analysis_signals WHERE cycle_id=? "
            "ORDER BY CASE action WHEN 'close' THEN 0 WHEN 'open_long' THEN 1 "
            "WHEN 'open_short' THEN 1 WHEN 'hold' THEN 2 ELSE 3 END, rowid",
            (cycle_id,)).fetchall()
        con.close()
        if run is None:
            raise LookupError("no analysis row")
        parts.append(
            "【本轮派发确认（dispatcher 已核，勿再查派发/分析就绪性）】\n"
            f"analysis: status={run['status']} mode={run['mode']} ts={run['ts']} "
            f"regime={run['regime']} regime_stale={run['regime_stale']} "
            f"missing_sources={run['missing_sources'] or '无'}")
        lines = []
        for s in sigs:
            rs = str(s["reasoning"] or "").replace("\n", " ")[:200]
            try:
                card = json.loads(s["decision_card"]) if s["decision_card"] else None
            except (json.JSONDecodeError, TypeError):
                card = None
            if isinstance(card, dict):
                hist = card.get("historical_experience") or {}
                lines.append(
                    f"  {s['symbol']} action={s['action']} side={s['side'] or '-'} "
                    f"entry={s['entry_hint'] or '-'} stop={s['stop_hint'] or '-'} "
                    f"tp={s['tp_hint'] or '-'}\n"
                    f"    方向={compact_text(card.get('direction_evidence'), 150)}\n"
                    f"    反对={compact_text(card.get('opposing_evidence'), 150)}\n"
                    f"    裁决={compact_text(card.get('agent_judgement'), 180)}\n"
                    f"    历史={hist.get('usage', 'none')}:"
                    f"{compact_text(hist.get('reason'), 120)}"
                )
            else:
                # 兼容格式仅展示文字理由，不把评分解释为当前协议依据。
                lines.append(
                    f"  {s['symbol']} action={s['action']} side={s['side'] or '-'} "
                    f"entry={s['entry_hint'] or '-'} stop={s['stop_hint'] or '-'} "
                    f"tp={s['tp_hint'] or '-'} | 兼容格式: {rs}")
        ms = str(run["market_summary"] or "")[:2500]
        parts.append(
            "【本轮分析预读（analysis.db 已读，勿再查）】\n"
            f"signals（{len(sigs)} 行，决策卡顺序）:\n"
            + ("\n".join(lines) if lines else "  （空=本轮无信号，全 hold）")
            + (f"\nmarket_summary: {ms}" if ms else ""))
    except Exception:
        parts.append("【分析预读缺块——按 AGENTS.md §2 自查 analysis.db】")
    # ③ 账户参考（live=system_state 4 键 / demo=account_snapshots 最新行=权威口径）
    try:
        con = _ro_db("account.db")
        if stage == "live":
            rows = con.execute(
                "SELECT key, value, updated_utc FROM system_state WHERE key IN "
                "('live_totalEq','live_availBal','live_position_count',"
                "'last_live_account_check')").fetchall()
            con.close()
            if not rows:
                raise LookupError("no system_state keys")
            kv = "; ".join(f"{r['key']}={r['value']}(@{r['updated_utc']})" for r in rows)
            parts.append(f"【账户参考（system_state，仅参考——现仓/余额以 OKX API 为准）】\n  {kv}")
        else:
            r = con.execute(
                "SELECT ts, totalEq, availBal, upl FROM account_snapshots "
                "WHERE profile='demo' ORDER BY rowid DESC LIMIT 1").fetchone()
            con.close()
            if r is None:
                raise LookupError("no demo snapshot")
            parts.append(
                "【账户参考（demo equity 唯一口径=account_snapshots，此即权威）】\n"
                f"  ts={r['ts']} totalEq={r['totalEq']} availBal={r['availBal']} upl={r['upl']}")
    except Exception:
        parts.append("【账户参考缺块——按 AGENTS.md §2 自查 account.db】")
    # ④ 决策简报（含历史正反样本与错失机会；live/demo 共享每 cycle 一次）
    brief = _briefing_for_traders(cycle_id)
    if brief:
        parts.append("【决策简报（已预读，历史盈利/亏损/错失机会均为参考）】\n"
                     f"--- decision_briefing ---\n{brief}\n--- end ---")
    else:
        parts.append("【决策简报缺块——按 AGENTS.md 自跑 decision_briefing.py 兜底】")
    # ⑤ 必须自取项（防预载诱导偷懒）
    profile = "live" if stage == "live" else "demo"
    safe_cycle = cycle_id.replace(":", "-")
    positions_file = _project_path(
        "tmp", f"okx_{profile}_{safe_cycle}_positions.json")
    balance_file = _project_path(
        "tmp", f"okx_{profile}_{safe_cycle}_balance.json")
    wrapper = "'" + _project_path(
        "scripts", "run_okx_python.ps1").replace("'", "''") + "'"
    cli = "'" + _project_path(
        "scripts", "_okxcli.py").replace("'", "''") + "'"
    parts.append(
        "【你仍需自取（唯一权威，禁用预载替代）】\n"
        "  OKX API 现仓：pwsh -NoProfile -File "
        f"{wrapper} {cli} "
        f"--profile {profile} --compact --out-file {positions_file} "
        "account positions --instType SWAP；随后 read "
        f"{positions_file}\n"
        "  OKX API 余额：pwsh -NoProfile -File "
        f"{wrapper} {cli} "
        f"--profile {profile} --compact --out-file {balance_file} "
        "account balance；随后 read "
        f"{balance_file}\n"
        "  两条命令均须原样直跑；stdout 仅为写入回执，完整 JSON 在 out-file。"
        "禁止改成 scripts/okx.py、裸 okx、管道或重定向；"
        "现仓真值喂 order_executor，预载里没有它，别猜。"
    )
    return "\n\n" + "\n\n".join(parts) + "\n"


def _write_message_file(key: str, msg: str) -> Path | None:
    """把触发消息写成 UTF-8 文件（--message-file 用），兼审计留痕。

      ① 使用官方 UTF-8 文件契约，消除编码、引号和长度对 argv 的依赖；
      ② 每轮触发指令落盘 logs/trigger/msg-<session-key>.txt，排障（改标/丢会话）
        可直接查当轮确切指令。写失败返 None，caller 回退 --message argv（保底不断链）。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        p = LOG_DIR / f"msg-{key}.txt"
        # newline="\n"：禁 Windows 文本模式把 \n 翻成 \r\n（否则 agent 收到的消息混入 CR，
        # 与源文差 60+ 字符——2026-07-03 金丝雀实测）。
        p.write_text(msg, encoding="utf-8", newline="\n")
        return p
    except OSError as e:
        print(f"[trigger_agent] WARN message-file 写失败，回退 --message argv: {e}",
              file=sys.stderr)
        return None


def build_cmd(stage: str, cycle_id: str, mode: str) -> list[str]:
    agent = STAGE_AGENTS[stage]
    # 触发消息只写"本轮工作"（stage/cycle/mode + analyst 的数据简报）；
    # 流程/红线/注意事项全在各 agent 的 AGENTS.md（OpenClaw 每轮自动加载），不再塞触发消息。
    if stage == "analyst":
        brief = _analyst_briefing(cycle_id)
        brief_block = (
            "\n【本轮数据简报（已预读，直接据此分析）】\n"
            f"--- decision_briefing ---\n{brief}\n--- end ---\n"
        ) if brief else "\n"
        msg = (
            f"OKX 本轮工作：stage=analyst cycle={cycle_id} mode={mode}。"
            f"本轮 gate/落库/回执的 cycle_id 一律用上面的 cycle={cycle_id}，"
            f"即使你的会话晚起、墙钟已进下一槽也不换标（禁 cycle_id_for() 重解析）。"
            f"按你的 AGENTS.md（操作手册）执行本轮分析。{brief_block}"
        )
    elif stage == "live" and mode == "unified":
        # 统一 live 在 analysis 尚未产出时起棒：预载采集后的全量 briefing，
        # 同一会话先 analyst_writer，成功后再走 OKX API + executor + trades_writer。
        brief = _analyst_briefing(cycle_id)
        brief_block = (
            "\n【本轮统一决策简报（分析前预读，直接据此完成分析+实盘）】\n"
            f"--- decision_briefing ---\n{brief}\n--- end ---\n"
        ) if brief else "\n【本轮统一决策简报缺块：按 AGENTS.md 自行补跑 decision_briefing】\n"
        msg = (
            f"OKX 本轮工作：stage=live cycle={cycle_id} mode=unified。"
            f"你是本轮唯一分析+实盘决策 Agent：先以 cycle={cycle_id} 执行采集 gate，"
            f"生成并经 analyst_writer 落 analysis.db；仅 status=ok 且 writer 成功后，"
            f"再直查 OKX live 现仓/余额，完成实盘决策、executor 调用和 trades_writer 落库。"
            f"gate/两个 writer/executor 的 cycle_id 均固定为上述派单 cycle，禁墙钟重解析。"
            f"不得等待或调用 demo/push；dispatcher 会在 analysis/live 落库后接力。"
            f"按你的 AGENTS.md（操作手册）执行。{brief_block}"
        )
    elif stage in ("live", "demo"):
        # trader 预载减少冷启动逐库自查；各块独立 fail-safe，缺块留标记回退自查。
        preload = _trader_preload(cycle_id, stage)
        msg = (
            f"OKX 本轮工作：stage={stage} cycle={cycle_id} mode={mode}。"
            f"本轮写库/回执/executor 调用的 cycle_id 一律用上面的 cycle={cycle_id}，"
            f"即使会话晚起、墙钟已进下一槽也不换标（禁墙钟重解析）。"
            f"按你的 AGENTS.md（操作手册）执行。{preload}"
        )
    else:
        msg = (
            f"OKX 本轮工作：stage={stage} cycle={cycle_id} mode={mode}。"
            f"按你的 AGENTS.md（操作手册）执行。"
        )
    key = session_key(stage, cycle_id)
    msg_file = _write_message_file(key, msg)
    msg_args = (["--message-file", str(msg_file)] if msg_file
                else ["--message", msg])
    timeout = (UNIFIED_LIVE_TIMEOUT
               if stage == "live" and mode == "unified"
               else STAGE_TIMEOUTS.get(stage, 720))
    return _launcher() + [
        "agent",
        "--agent", agent,
        "--session-key", key,
        *msg_args,
        "--timeout", str(timeout),
        "--json",
    ]


def _fire_push_script(cycle_id: str) -> str:
    """push stage 纯脚本路径：detached 起 push_pipeline.py
    （build→render→validate→qq_push→archive→system_state）。
    返回 session-key 作 card_id（与 agent 路径同签名，dispatcher._fire_stage 语义不变）。
    dry-run（OKX_TRIGGER_DRYRUN=1）只落命令日志不真起。python.exe 直起（原生 exe，
    DETACHED 存活；不经 pwsh wrapper——pwsh 跑 .ps1 在 DETACHED_PROCESS 下可能静默不执行；
    管道内部各步自走 wrapper 拿 UTF-8/MX_APIKEY）。
    起棒失败抛异常由 _fire_stage 释放闩锁重试。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    key = session_key("push", cycle_id)
    inner_cmd = [_PYTHON_EXE, _PUSH_PIPELINE, "--cycle", cycle_id,
                 "--db-root", _OKX_DB_ROOT]
    cmd = _supervised_cmd("push", cycle_id, "script", inner_cmd)
    logf = LOG_DIR / f"{key}.log"
    dry = os.environ.get("OKX_TRIGGER_DRYRUN") == "1"
    with open(logf, "a", encoding="utf-8") as fh:
        fh.write(f"\n[{now_cst()}] stage=push cycle={cycle_id} mode=script dry={dry}\n")
        fh.write("  cmd: " + " ".join(cmd) + "\n")
        if dry:
            fh.write("  (dry-run: 未真起 push_pipeline)\n")
            return key
        fh.flush()
        # 仅 DETACHED（无控制台，stdout 重定向到 log）——同 fire()，Windows 下不弹窗。
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            creationflags=flags, cwd=str(Path(_project_path())), close_fds=True)
        _probe_launch(proc, "push", cycle_id, fh)
    return key


def fire(stage: str, cycle_id: str, mode: str = "full") -> str:
    """detached 拉起 Agent stage，或对 push 无条件起纯脚本管道。

    返回 session-key（作 card_id 用）。

    dry-run（OKX_TRIGGER_DRYRUN=1）：不真起，只把命令落日志——tmp 验证 plumbing 用。
    启动失败（如 openclaw 不在 PATH）会抛 FileNotFoundError，由 dispatcher._fire_stage
    捕获后释放 stage 闩锁，下一 tick 重试。
    """
    if stage == "push":
        return _fire_push_script(cycle_id)
    if stage not in STAGE_AGENTS:
        raise ValueError(f"unknown stage: {stage}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    key = session_key(stage, cycle_id)
    logf = LOG_DIR / f"{key}.log"

    dry = os.environ.get("OKX_TRIGGER_DRYRUN") == "1"
    if dry:
        # dry 判定必须在 build_cmd 之前，避免启动 decision_briefing 子进程或写
        # msg/briefing 文件。dry 只验 plumbing：落意图日志即返回，
        # 不组消息、不起任何子进程、不写消息/简报文件。
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write(f"\n[{now_cst()}] stage={stage} cycle={cycle_id} mode={mode} "
                     f"agent={STAGE_AGENTS[stage]} dry=True\n")
            fh.write("  (dry-run: 未组消息/未真起 agent)\n")
        return key
    inner_cmd = build_cmd(stage, cycle_id, mode)
    cmd = _supervised_cmd(stage, cycle_id, mode, inner_cmd)
    with open(logf, "a", encoding="utf-8") as fh:
        fh.write(f"\n[{now_cst()}] stage={stage} cycle={cycle_id} mode={mode} "
                 f"agent={STAGE_AGENTS[stage]} dry={dry}\n")
        fh.write("  cmd: " + " ".join(cmd) + "\n")
        fh.flush()
        # 仅 DETACHED（无控制台）：DETACHED 与 CREATE_NO_WINDOW 互斥(MSDN)，同设致隐藏失效
        # → 子进程获控制台 → Windows Terminal DefTerm 弹「openclaw-agent」窗。
        # stdout/stderr 已重定向到 log（见上），DETACHED 单独安全、无控制台。
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            cwd=str(Path(_project_path())),
            close_fds=True,
        )
        _probe_launch(proc, stage, cycle_id, fh)
    return key


def main() -> int:
    ap = argparse.ArgumentParser(description="agent 起棒适配层（唯一 caller=core/dispatcher.py；人工排障可 CLI 调）")
    ap.add_argument("--stage", required=True, choices=sorted((*STAGE_AGENTS, "push")))
    ap.add_argument("--cycle", required=True, help="cycle_id 如 2026-06-18T14:00")
    ap.add_argument("--mode", default="full", choices=["full", "unified"])
    args = ap.parse_args()
    if args.mode == "unified" and args.stage != "live":
        ap.error("mode=unified 仅适用于 stage=live")
    key = fire(args.stage, args.cycle, args.mode)
    print(f"fired stage={args.stage} cycle={args.cycle} -> session-key={key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
