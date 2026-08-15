# -*- coding: utf-8 -*-
"""V2.0 起棒适配层：用 `openclaw agent` 拉起 stage 对应 agent 的一个 turn。

现役定位：**唯一 caller = core/dispatcher.py**（_fire_stage 抢 stage_dispatch 闩锁后调
fire()）；本模块集中管理 agent-id / 分级 timeout / --message-file 主路径，自身不做幂等
（幂等真值 = ledger.stage_dispatch）。

Agent stage（analyst/live）与纯脚本 push stage 均由 core/dispatcher.py 确定性起棒。

三条设计红线
------------
1. **零模型名**（红线 #8）：本模块只含 agent-id / session-key（路由标识，非模型）。
   模型分配只在 `openclaw config agents.list.<id>.model`，本桥永不碰。
2. **每 cycle 独立 session**：session-key 带 cycle 槽位 → 每轮每 agent 一个新会话，
   单轮跑完即弃。根治持久会话 context overflow（OKXV7 2026-06-18 13:01 事故）。
   所有跨轮状态在 DB（analysis.db / *_trades.db），不靠会话记忆。
3. **detached 异步启动**：闩锁赢家立即返回，不阻塞等 agent turn 跑完——采集脚本有
   硬超时（快采 ≤360s），analyst turn 可能数分钟。Gateway 服务端跑 turn，CLI 客户端
   detached 退出不影响。

用法
----
作为库（现役主路径，core/dispatcher._fire_stage 调）：
    import trigger_agent
    trigger_agent.fire(stage, cycle_id, mode)   # -> session-key

作为 CLI（人工排障/补起单棒用）：
    python trigger_agent.py --stage live --cycle 2026-06-18T14:00
    python trigger_agent.py --stage push --cycle 2026-06-18T14:00  # 永久走 push_pipeline.py

环境变量（覆盖默认 agent-id / 二进制 / dry-run）：
    OKX_ANALYST_AGENT  OKX_LIVE_AGENT
    OKX_OPENCLAW_BIN（默认 'openclaw'）
    OKX_LAUNCH_PROBE_S（默认 3 秒；检测子进程启动后立即非零退出）
    OKX_STAGE_RUNNER / OKX_STAGE_STATUS_DIR（终态监督脚本 / 状态目录）
    OKX_TRIGGER_DRYRUN=1（不真起 agent，只把命令写日志，用于 tmp 验证 plumbing）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(
    os.environ.get("OKX_ROOT") or Path(__file__).resolve().parents[1]
).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


if _project_path() not in sys.path:
    sys.path.insert(0, _project_path())
from core.decision_card import compact_text  # noqa: E402
from core.risk_validator import (  # noqa: E402
    MAX_PORTFOLIO_IMR_RATIO,
    MAX_SINGLE_ORDER_IMR_RATIO,
    SINGLE_ORDER_SIZING_HEADROOM_PCT,
)
from collectors.cycle_contract import (  # noqa: E402
    cycle_session_token,
    cycle_status_token,
    validate_cycle_id,
)

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
}

# 显式 agent turn 超时（秒）：人工回滚 analyst 900；full live 720；
# push 纯脚本不使用 Agent timeout。
# 勿盲目加大：超时=挂死会话占据 gateway lane 的上限（拥塞治理约束）。
STAGE_TIMEOUTS = {
    "analyst": int(os.environ.get("OKX_ANALYST_TIMEOUT_S", "900")),
    "live": int(os.environ.get("OKX_TRADER_TIMEOUT_S", "720")),
}
# 合并轮的配置上限仍保留 1500s；真正下发给 Gateway 的 timeout 会再按
# ``cycle+12:00`` 的绝对时钟收紧。Gateway 自己先结束 turn，给 CLI 30 秒返回缓冲，
# 随后 stage_runner 仍在 ``cycle+13:00`` 整树兜底，最后 1 分钟留给失败报告/推送。
# 人工回滚后的 full live 沿用上面的 720s。
UNIFIED_LIVE_TIMEOUT = int(os.environ.get("OKX_UNIFIED_LIVE_TIMEOUT_S", "1500"))
# 15 分钟固定节奏下的业务完成目标，不是硬杀进程超时。统一轮应把主要时间留给
# 当轮判断与确定性落库；25 分钟 hard timeout 仍只负责兜住真正挂死，避免在订单或
# writer 临界区强杀。候选深挖数只约束串行取证成本，不限制 Agent 的多空、退出或
# 保护调整裁决权。深挖 2..3 是动态评估区间，不是最低开仓数或
# 方向配额；最终 signals 仍允许为空。
UNIFIED_ANALYSIS_TARGET_MIN = 5
UNIFIED_FINALIZE_RESERVE_MIN = 3
UNIFIED_OPEN_REVIEW_MIN = 2
UNIFIED_OPEN_FINALISTS = 3
UNIFIED_COMPLETE_SLA_MIN = 14
UNIFIED_PUSH_RECONCILE_RESERVE_MIN = 1
UNIFIED_GATEWAY_DEADLINE_SECONDS = 12 * 60


def _unified_live_timeout_seconds(
    cycle_id: str,
    *,
    now: datetime | None = None,
) -> int:
    """Return the bounded Gateway run budget for one unified live cycle.

    ``openclaw agent --timeout`` is forwarded to the Gateway as the actual
    agent-run timeout.  Keeping it below the supervisor's cycle+13:00 hard
    kill prevents the Gateway-owned turn from continuing to call tools after
    its local CLI process has already been terminated.
    """
    cycle_start = datetime.strptime(
        str(cycle_id), "%Y-%m-%dT%H:%M",
    ).replace(tzinfo=CST)
    current = now or datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    current = current.astimezone(CST)
    deadline = cycle_start + timedelta(
        seconds=UNIFIED_GATEWAY_DEADLINE_SECONDS)
    remaining = int((deadline - current).total_seconds())
    return max(1, min(UNIFIED_LIVE_TIMEOUT, remaining))

# 直起 node + openclaw.mjs（绕开 openclaw.cmd）：
#   ① 不经 cmd.exe → 无控制台弹窗（CREATE_NO_WINDOW 对 .cmd 仍会闪 cmd 窗）；
#   ② 消息主路径使用 --message-file（UTF-8 文件官方契约 + 每轮落盘审计，
#     见 _write_message_file）；中文 --message 仅作为
#     文件写失败时的 argv 兜底路径依据。
_NODE_BIN = os.environ.get("OKX_NODE_BIN", r"C:\Program Files\nodejs\node.exe")
_OPENCLAW_MJS = os.environ.get(
    "OKX_OPENCLAW_MJS",
    str(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" /
        "openclaw" / "openclaw.mjs"),
)
# 兼容：设了 OKX_OPENCLAW_BIN 则仍用单一 bin（自定义 wrapper）；否则走 node+mjs。
OPENCLAW_BIN = os.environ.get("OKX_OPENCLAW_BIN", "")

# push stage 固定执行纯脚本 push_pipeline.py，避免 LLM 临场拼装报告产生结构漂移。
# 起法用 **python.exe 直起**（原生 exe，同 node 路径可 DETACHED 存活）——不经 pwsh wrapper：
#   pwsh 跑 .ps1 在 DETACHED_PROCESS 下可能静默不执行。
#   push_pipeline 自身只读库 + 内部各步仍走 wrapper（拿 UTF-8/PYTHONPATH/MX_APIKEY），故裸 python 起足够。
_PUSH_PIPELINE = os.environ.get(
    "OKX_PUSH_PIPELINE", _project_path("scripts", "push_pipeline.py")
)
_PYTHON_EXE = os.environ.get(
    "OKX_PYTHON_BIN",
    sys.executable)
_STAGE_RUNNER = os.environ.get(
    "OKX_STAGE_RUNNER", _project_path("scripts", "stage_runner.py"))
_STAGE_STATUS_DIR = Path(os.environ.get(
    "OKX_STAGE_STATUS_DIR", _project_path("logs", "stage-status")))
_OKX_DB_ROOT = os.environ.get("OKX_DB_ROOT", _project_path("db"))
_CANONICAL_DB_ROOT = Path(_project_path("db")).resolve()


def _root_namespace(db_root: str | os.PathLike | None = None) -> str:
    """Return a stable suffix for artifacts bound to an isolated DB root."""
    resolved = Path(db_root or _OKX_DB_ROOT).resolve()
    if os.path.normcase(os.fspath(resolved)) == os.path.normcase(
        os.fspath(_CANONICAL_DB_ROOT)
    ):
        return ""
    return "r" + hashlib.sha256(
        os.path.normcase(os.fspath(resolved)).encode("utf-8")
    ).hexdigest()[:10]


def _launcher() -> list[str]:
    """起棒命令前缀：优先 node + openclaw.mjs；env 覆盖或 mjs 缺失时兜底。"""
    if OPENCLAW_BIN:
        return [OPENCLAW_BIN]
    try:
        bundled_launcher_available = (
            Path(_OPENCLAW_MJS).is_file() and Path(_NODE_BIN).is_file()
        )
    except OSError:
        bundled_launcher_available = False
    if bundled_launcher_available:
        # --stack-size=8192：node 一开始就带大栈，OpenClaw entry.js 的 hasStackSizeConfigured()
        # 检测到即**不 re-spawn worker**——worker re-spawn 没设 windowsHide、Windows 下 detached 被强制
        # false，会自建控制台被 Windows Terminal DefTerm 弹「openclaw-agent」窗。绕过 respawn = 单进程
        # （配合 fire() 的 DETACHED 无控制台）= 不弹窗。见 entry.js:86-90 spawn + :290 hasStackSizeConfigured。
        return [_NODE_BIN, "--stack-size=8192", _OPENCLAW_MJS]
    # 兜底（弹窗/坏码风险）：仅当 node/mjs 缺失
    return [str(Path.home() / "AppData" / "Roaming" / "npm" / "openclaw.cmd")]


LOG_DIR = Path(_project_path("logs", "trigger"))

# Windows detached 启动标志：子进程脱离父，父退出不带走它。
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _launch_probe_seconds() -> float:
    try:
        return min(max(float(os.environ.get("OKX_LAUNCH_PROBE_S", "3")), 0.0), 10.0)
    except (TypeError, ValueError):
        return 3.0


def _probe_launch(
    proc: subprocess.Popen,
    stage: str,
    cycle_id: str,
    fh,
    db_root: str | os.PathLike | None = None,
) -> None:
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
        status_path = _stage_status_path(stage, cycle_id, db_root)
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


def _supervised_cmd(
    stage: str,
    cycle_id: str,
    mode: str,
    command: list[str],
    db_root: str | os.PathLike | None = None,
) -> list[str]:
    """独立 runner 等待 detached 真子进程并持久化终态；不负责释放闩锁或重试。"""
    cycle_id = validate_cycle_id(cycle_id)
    resolved_db_root = os.fspath(Path(db_root or _OKX_DB_ROOT).resolve())
    return [
        _PYTHON_EXE, _STAGE_RUNNER,
        "--stage", stage, "--cycle", cycle_id, "--mode", mode,
        "--db-root", resolved_db_root,
        "--", *command,
    ]


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _safe_cycle(cycle_id: str) -> str:
    """'2026-06-18T14:00' -> '20260618-1400'（session-key 不含冒号，避免分段歧义）。"""
    return cycle_session_token(cycle_id)


def session_key(
    stage: str,
    cycle_id: str,
    db_root: str | os.PathLike | None = None,
) -> str:
    """bare key（不带 agent: 前缀）；交给 openclaw --agent 拼成 agent:<id>:<key>。

    （MEMORY 教训：自己拼前缀会变 4 段双前缀 → setup timeout。）
    """
    suffix = _root_namespace(db_root)
    tail = f"-{suffix}" if suffix else ""
    return f"{stage}-{_safe_cycle(cycle_id)}{tail}"


def _stage_status_path(
    stage: str,
    cycle_id: str,
    db_root: str | os.PathLike | None = None,
) -> Path:
    suffix = _root_namespace(db_root)
    tail = f"-{suffix}" if suffix else ""
    return _STAGE_STATUS_DIR / f"{stage}-{cycle_status_token(cycle_id)}{tail}.json"


# 操作手册＝各 agent 的 workspace AGENTS.md（OpenClaw 每轮自动加载）；fire 消息只指 AGENTS.md。


_DB_ROOT = Path(os.environ.get("OKX_DB_ROOT", _project_path("db")))
_AUTOHEAL_CONTRACT_VERSION = 1


def _resolve_db_root(db_root: str | os.PathLike | None = None) -> Path:
    return Path(db_root or _DB_ROOT).resolve()


def _autoheal_client_result(profile: str | None, cycle_id: str,
                            request_id: str, *, status: str, rc: int,
                            reason: str, db_root: Path | None = None,
                            finding_kind: str | None =
                            "AUTOHEAL-CONTRACT-INVALID") -> dict:
    """Build a caller-side result when no trustworthy producer result exists."""
    root = Path(db_root or _DB_ROOT).resolve()
    finding = ({"kind": finding_kind, "sev": "P1", "reason": reason}
               if finding_kind else None)
    return {
        "contract_version": _AUTOHEAL_CONTRACT_VERSION,
        "request_id": request_id,
        "profile": profile,
        "cycle": cycle_id,
        "db_root": str(root),
        "status": status,
        "applied": False,
        "p0": False,
        "blocking": rc != 0,
        "reason": reason,
        "findings": [finding] if finding else [],
        "healed": [],
        "needs_human": [finding] if finding else [],
        "rc": rc,
    }


def _read_autoheal_contract(path: Path, *, request_id: str, profile: str,
                            cycle_id: str, db_root: Path,
                            returncode: int) -> dict:
    """Read and strictly bind a v1 result to this exact subprocess request."""
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > 2_000_000:
            raise ValueError("json-out exceeds 2 MB")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("json-out root is not an object")
        checks = {
            "contract_version": data.get("contract_version") == _AUTOHEAL_CONTRACT_VERSION,
            "request_id": data.get("request_id") == request_id,
            "profile": data.get("profile") == profile,
            "cycle": data.get("cycle") == cycle_id,
            "db_root": Path(str(data.get("db_root") or "")).resolve()
                       == db_root.resolve(),
            "status": data.get("status") in {
                "ok", "applied", "needs_human", "error", "skipped",
                "p0_blocked",
            },
            "applied": type(data.get("applied")) is bool,
            "p0": type(data.get("p0")) is bool,
            "blocking": type(data.get("blocking")) is bool,
            "findings": isinstance(data.get("findings"), list),
            "healed": isinstance(data.get("healed"), list),
            "needs_human": isinstance(data.get("needs_human"), list),
            "rc": type(data.get("rc")) is int
                  and data.get("rc") in (0, 1, 2, 3, 4),
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise ValueError("contract fields invalid: " + ",".join(failed))
        if data["blocking"] != (data["rc"] != 0):
            raise ValueError("blocking/rc mismatch")
        if data["p0"] != (data["rc"] == 4):
            raise ValueError("p0/rc mismatch")
        status_by_rc = {
            0: {"ok", "applied"}, 1: {"needs_human"}, 2: {"error"},
            3: {"skipped"}, 4: {"p0_blocked"},
        }
        if data["status"] not in status_by_rc[data["rc"]]:
            raise ValueError("status/rc mismatch")
        healed_applied = any(
            isinstance(item, dict) and item.get("applied") is True
            for item in data["healed"])
        if data["applied"] != healed_applied:
            raise ValueError("applied/healed mismatch")
        findings_p0 = any(
            isinstance(item, dict)
            and str(item.get("sev") or "").upper() == "P0"
            for item in data["findings"])
        if data["p0"] != findings_p0:
            raise ValueError("p0/findings mismatch")
        if int(returncode) != data["rc"]:
            raise ValueError(
                f"process rc={returncode} differs from contract rc={data['rc']}")
        return data
    except Exception as exc:  # noqa: BLE001
        return _autoheal_client_result(
            profile, cycle_id, request_id, status="contract_invalid", rc=2,
            reason=f"{type(exc).__name__}: {exc}", db_root=db_root,
        )


def _run_briefing(db_root: str | os.PathLike | None = None) -> str:
    """跑一次 decision_briefing（五库简报）。失败返回空串（agent 退回自查，不阻断）。"""
    try:
        brief_py = Path(__file__).parent.parent / "scripts" / "decision_briefing.py"
        resolved_db_root = _resolve_db_root(db_root)
        p = subprocess.run(
            [sys.executable, str(brief_py), "--db-root", str(resolved_db_root)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=_CREATE_NO_WINDOW,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _unified_live_message(
    cycle_id: str,
    brief: str,
    *,
    now: datetime | None = None,
    db_root: str | os.PathLike | None = None,
) -> str:
    """Build the unified-route prompt with the writer contract in-band.

    ``unified`` is a dispatcher routing mode, while analysis_runs.mode has
    always been ``full``.  Keeping that distinction only in a long workspace
    manual proved too easy to lose next to a large briefing, so the money-path
    handoff states the minimal receipt shape explicitly.
    """
    safe_cycle = cycle_id.replace(":", "-")
    runtime_db_root = _resolve_db_root(db_root).as_posix()
    tmp_root = (_PROJECT_ROOT / "tmp").as_posix()
    python_wrapper = (_PROJECT_ROOT / "scripts" / "run_okx_python.ps1").as_posix()
    position_runner = (
        _PROJECT_ROOT / "scripts" / "live_position_action_runner.py"
    ).as_posix()
    mtf_runner = (
        _PROJECT_ROOT / "scripts" / "multitimeframe_decision_evidence.py"
    ).as_posix()
    cycle_start = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    analysis_deadline = cycle_start + timedelta(minutes=9, seconds=30)
    terminal_deadline = cycle_start + timedelta(
        minutes=UNIFIED_COMPLETE_SLA_MIN - UNIFIED_PUSH_RECONCILE_RESERVE_MIN)
    completion_deadline = cycle_start + timedelta(minutes=UNIFIED_COMPLETE_SLA_MIN)
    analysis_deadline_text = analysis_deadline.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    terminal_deadline_text = terminal_deadline.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    completion_deadline_text = completion_deadline.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    observed_at = now or datetime.now(CST)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=CST)
    observed_at = observed_at.astimezone(CST)
    analysis_remaining_seconds = max(
        0, int((analysis_deadline - observed_at).total_seconds()))
    if analysis_remaining_seconds <= 150:
        dynamic_candidate_limit = 1
    elif analysis_remaining_seconds <= 300:
        dynamic_candidate_limit = 2
    else:
        dynamic_candidate_limit = UNIFIED_OPEN_FINALISTS
    dynamic_candidate_floor = min(
        UNIFIED_OPEN_REVIEW_MIN, dynamic_candidate_limit)
    observed_at_text = observed_at.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    brief_block = (
        "\n【本轮统一决策简报（分析前预读，直接据此完成分析+实盘）】\n"
        f"--- decision_briefing ---\n{brief}\n--- end ---\n"
    ) if brief else "\n【本轮统一决策简报缺块：按 AGENTS.md 自行补跑 decision_briefing】\n"
    analysis_receipt_contract = (
        "【analysis writer 契约】dispatch_mode=unified 仅表示同一 Agent 承担分析+实盘；"
        "写给 analyst_writer 的 JSON 顶层 mode 必须固定为 full，并包含 "
        "cycle_id/ts/status=ok/decision_protocol=decision_card_v1/regime/"
        "regime_stale/market_summary/missing_sources/signals/raw。"
        "market_summary 必须直接包含 macro/news/tech/sentiment/quant 五个 dict。"
        "每个 signals[].decision_card 必须直接包含 direction_evidence(list)、"
        "opposing_evidence(list)、execution_conditions、invalidation_point、"
        "risk_reward、portfolio_impact、historical_experience(dict，内含 "
        "matched_wins/matched_losses/missed_opportunities 三个 list 及 usage/reason)、"
        "agent_judgement、reference_overrides(list)。这些字段不得放在 signal 顶层，"
        "也不得改名为 rationale/final_judgement/overrides；动作允许 "
        "open_long/open_short/hold/close/reduce/adjust_protection/wait，"
        "open_long/open_short 必须显式写 side=long/short 且与 action 一致，"
        "字段枚举契约记为 side=long|short；"
        "不得依赖 writer 兼容归一化；"
        "凡写入 signals 的动作都必须给完整卡，HOLD/WAIT 若写入同样必须给完整卡。"
        "但本自动 unified 路由的 signals 是最终开仓短名单，不是 briefing 逐项转录："
        f"只允许 0..{UNIFIED_OPEN_FINALISTS} 项且只保留最终决定 open_long/open_short 的"
        "候选；没有最终开仓候选就写 signals=[]。未入选候选不得展开成 WAIT/HOLD signal，"
        "现有持仓也不得在 pre-facts analysis 中逐仓展开成 HOLD signal；全局取舍浓缩进 "
        "market_summary，现仓管理留到同轮 live facts 后逐仓判断。"
        "0..3 只是容量上限，不设最低开仓数、多空配额或强制交易。"
        "open_* 的 risk_reward.exit_mode 必须明确为 "
        "fixed_tp/dynamic_exit/no_fixed_tp；target 仍用于 EV 与参考，只有 fixed_tp 附挂。"
        "每个 open_long/open_short 候选还必须先运行只读工具 "
        "multitimeframe_decision_evidence.py，以本轮固定 cycle 和完整 instId 生成证据文件；"
        "15m/1H/4H 任一 exact 已收盘 K 线、指标或至少 34 根历史不足即不得产出 open。"
        "把工具返回的完整 evidence_contract 原样放进 "
        "decision_card.multitimeframe_analysis.evidence_contract，并对 15m/1H/4H "
        "分别填写 direction/evidence/relative_rank；每个 evidence 固定为非空 "
        "JSON list[string]（一条也写成 [\"...\"]，禁裸字符串/空串/object）；"
        "三个 rank 必须恰为 1/2/3，"
        "selected_timeframe 指向 rank=1 且方向与开仓 side 一致。selection_method 固定为 "
        "relative_rank_1_among_15m_1H_4H_not_calibrated，"
        "calibrated_confidence=null、confidence_claim_allowed=false；"
        "在独立前瞻验证达到 90% 前不得写 90% 可信度。"
        "每个 open_* 还须把本卡三价原样传给 find_similar_experience.py："
        f"--as-of {cycle_id} --entry <entry> --stop <stop> --target <target>；"
        "禁止自行换算百分比或 RR，工具与 writer 共用规范化函数。"
    )
    trade_receipt_contract = (
        "【trade writer 契约】交易阶段的 cycle 顶层 decision_card 不是摘要容器，"
        "必须直接包含同一组固定键：direction_evidence(list)、opposing_evidence(list)、"
        "execution_conditions、invalidation_point、risk_reward、portfolio_impact、"
        "historical_experience(dict)、agent_judgement、reference_overrides(list)。"
        "HOLD/WAIT/REDUCE/ADJUST_PROTECTION 也必须完整填写，禁止改成 "
        "summary/open_candidates/hold_positions。"
        "只有 executor 成功返回 action_taken=ADJUST_PROTECTION 且保留 "
        "protection_change、path、protection_state.ok=true 与 applied，才可报告保护调整；"
        "未调用 executor 或无终局回读时必须写 HOLD，不得自行写模糊 ADJUST。"
        "OPEN/ADD 必须原样保留 analysis 的 multitimeframe_analysis；executor 会在任何"
        "交易所账户/订单 I/O 前按同 cycle 重读 market.db，当前三周期未就绪则 clean reject。"
        "完全一致使用 current_market_exact；若同槽后续采集修订已收盘数据，仅当卡内契约"
        "逐字段命中同 cycle/symbol/side 的 analysis.db writer 已验证锚点"
        "analysis_db_writer_validated 才继续，并在回执保留 post_analysis_market_revision"
        "和 supplied/current/persisted hash；否则 clean reject。持久化锚点不替代 readiness。"
        "调用 trades_writer.py --facts-file 时，回执应省略 live_facts 让 writer 原样注入；"
        "若携带则必须与 facts 文件整份完全相同，禁止摘要或重算。"
        "本轮不论是否包含 OPEN/ADD，都必须一次 write 完整 position plan 后立即且只调用一次 "
        "live_position_action_runner.py；actions=[] 即 HOLD，OPEN/ADD/CLOSE/REDUCE/"
        "ADJUST_PROTECTION 可在同一 plan 混合。runner 不替你判断，也不设退出阈值；OPEN/ADD"
        "只写 side/target_stop_risk_pct_equity/lev，runner 从 analysis.db 只读绑定同 symbol"
        " canonical card 并确定性换算张数。计划 receipt_context 只放 cycle/status/protocol/card/"
        "equity/regime，禁止预填 decision/action_taken/n_orders/trades/errors/ok；"
        f"plan={tmp_root}/position_plan_{safe_cycle}.json，"
        f"receipt={tmp_root}/_receipt_live_{safe_cycle}.json。"
        f"命令：pwsh -NoProfile -File {python_wrapper} "
        f"{position_runner} "
        f"--cycle-id {cycle_id} --plan-file {tmp_root}/position_plan_{safe_cycle}.json "
        f"--facts-file {tmp_root}/live_facts_{safe_cycle}.json "
        f"--receipt-file {tmp_root}/_receipt_live_{safe_cycle}.json "
        f"--db-root {runtime_db_root}。"
        "plan 后立即 runner/30s 机器闸核验 live_runner_state 的 cycle/facts_hash/"
        "plan_sha256。runner 在后续 OPEN/ADD 前会先提交此前成交、显式带 "
        "runner_in_progress=true 的 partial superset；batch_status=partial|failed、"
        "interim/final writer 失败或非零退出就是 terminal failure，禁止重跑、补动作或另写 HOLD。"
    )
    throughput_contract = (
        "【周期内吞吐契约】统一轮的业务完成目标为起棒后 8 分钟内形成成功终态："
        f"前 {UNIFIED_ANALYSIS_TARGET_MIN} 分钟内冻结 analysis，至少预留 "
        f"{UNIFIED_FINALIZE_RESERVE_MIN} 分钟完成 live facts、持仓管理/交易、writer 与"
        "只读终态核验。优先直接消费已预读 briefing，open 候选深挖的动态目标区间为 "
        f"{UNIFIED_OPEN_REVIEW_MIN}..{UNIFIED_OPEN_FINALISTS} 个：候选充足时先完整检查前 "
        f"{UNIFIED_OPEN_REVIEW_MIN} 个，如因数据不就绪或 Agent 判断否决，就在同一 5 分钟"
        f"分析预算内顺延第 3 个，上限 {UNIFIED_OPEN_FINALISTS} 个。现有持仓数量、"
        "已持有同向/反向仓、软集中度或未触发硬风控的组合占用，不得单独成为停止候选求证的"
        "理由。到分析预算仍无可辩护开仓时，立即写完整 "
        "market_summary 与 signals=[]，而不是继续搜索或生成候选 WAIT/HOLD 卡。随后交易阶段"
        "仍以完整顶层 HOLD/WAIT 回执记录无动作结论。深挖 2..3 不是强制交易，最终开仓卡可为 0..3，"
        "不设最低数和方向配额。该预算只消除串行探查和收尾拖延，"
        "不限制做多、做空、"
        "全平、减仓、止损或止盈裁决，也不得用来跳过 gate、证据契约、executor、writer、"
        "保护单确认或失败重写规则。分析落库后立即进入 facts；trades_writer 返回 ok:true 后立即给出"
        "简短终答，不再复盘、扩展研究或调用任何工具；trade_cycles 的独立落库后置核验只由"
        "stage_runner 承担。"
        "【本轮动态时间预算】本消息生成时刻为 "
        f"{observed_at_text}，距 analysis 绝对闸尚余 "
        f"{analysis_remaining_seconds} 秒；本轮候选深挖区间据此收敛为 "
        f"{dynamic_candidate_floor}..{dynamic_candidate_limit} 个、硬上限="
        f"{dynamic_candidate_limit}。这是迟起小时轮的耗时预算，不是方向、开仓数或仓位"
        "约束；完整 300+ 宇宙判断仍由已预读 briefing/确定性快照承担。达到本轮上限后"
        "立即完成取舍，禁止再开新候选。消息已给出权威调度时钟和完整 writer 契约；"
        "自动轮禁止调用 session_status，禁止读取 analysis_template、"
        "trade_template、writer 源码或无关手册。允许读取 MEMORY.md 并调用 memory_search"
        "（两者合计≤2 次，建议用于最终 open/风险动作候选的同标的历史教训）；"
        "记忆命中与本消息、briefing 或权威事实脚本冲突时一律以后者为准，"
        "不得因记忆检索逼近本轮时间闸。只允许为最终 open 候选补齐必需的三周期/"
        "经验契约、上述受限记忆检索，以及随后 facts、executor、writer 所需工具。"
        f"本轮绝对时间闸：analysis 最迟 {analysis_deadline_text} 冻结，Agent 业务终态最迟 "
        f"{terminal_deadline_text} 落库，为 push+对账预留 {UNIFIED_PUSH_RECONCILE_RESERVE_MIN} 分钟；"
        f"完整周期必须严格早于 {completion_deadline_text}。临近 analysis 绝对时间闸时不再开新的"
        "候选深挖，立即完成已有证据的取舍与 writer；这不允许跳过风控、executor、成交确认或终态落库。"
        "到达 Agent 业务终态硬截止后不得再发起新的 OPEN/ADD/CLOSE/REDUCE 或独立保护调整；"
        "runner 会按本轮隔离 session 发 Gateway abort，executor 仍按 cycle 二次拒绝后台残留 turn；"
        "已开始订单的保护确认与安全回滚仍须完整收尾。"
    )
    terminal_contract = (
        "【交易阶段终止契约】live facts 文件读完后，writer 命令、回执字段与流程已由本消息"
        "和 AGENTS.md 完整给出；禁止再读取 trades_writer.py 源码、探查 schema、搜索或读取"
        "历史 _receipt_live_*.json、回看无关手册"
        "或继续研究实现。必须立即形成最终交易判断：若不 "
        "OPEN/ADD/CLOSE/REDUCE/ADJUST_PROTECTION 就生成完整 HOLD/WAIT 回执；"
        "随后调用 trades_writer；writer 返回前禁止最终答复、禁止无内容 stop。writer 返回 ok:true 后，"
        "严禁再调用 query_db、--help、--schema 或"
        "任何其他工具，必须立即发送简短最终答复；stage_runner 会独立核验本 cycle 的 trade_cycles，"
        "Agent 不得重复核验。即使零成交，HOLD 也必须先落库且只能由 writer 完成；writer 失败则按手册保留"
        "文件并报告 terminal failure，不得把未落库当作正常结束。"
    )
    return (
        f"OKX 本轮工作：stage=live cycle={cycle_id} dispatch_mode=unified；"
        "analysis_receipt_mode=full。"
        f"你是本轮唯一分析+实盘决策 Agent：先以 cycle={cycle_id} 执行采集 gate，"
        f"生成并经 analyst_writer 落 analysis.db；仅 status=ok 且 writer 成功后，"
        f"再直查 OKX live 现仓/余额，完成实盘决策、executor 调用和 trades_writer 落库。"
        f"{analysis_receipt_contract}{trade_receipt_contract}{throughput_contract}"
        f"{terminal_contract}"
        f"多周期证据命令格式：pwsh -NoProfile -File {python_wrapper} "
        f"{mtf_runner} --db-root {runtime_db_root} "
        f"--symbol <完整instId> --cycle-id {cycle_id} "
        f"--out-file {tmp_root}/mtf_{safe_cycle}_<symbol>.json；只读取该文件，禁止手算/hash。"
        "analysis 回执必须直接用 write 一次完整写最终 JSON 文件，禁止先建 Python/PowerShell"
        "生成器，先 validate-only 后正式 writer，禁止 edit/局部补丁循环；校验失败最多用 write"
        "整文件覆盖一次，第二次失败即停止，禁止跳过 writer。"
        "禁止调用 sqlite3 CLI 临时探查 schema 或业务库；schema 只读 db/schema.sql，"
        "业务查询只用已批准的 query_db.py/事实脚本。"
        "gate/两个 writer/executor 的 cycle_id 均固定为上述派单 cycle，禁墙钟重解析。"
        "不得等待或调用 push；dispatcher 会在 analysis/live 落库后接力。"
        f"按你的 AGENTS.md（操作手册）执行。{brief_block}"
    )


def _send_autoheal_p0_alert(
    profile: str,
    cycle_id: str,
    findings: list[dict],
    db_root: str | os.PathLike | None = None,
) -> bool:
    """Best-effort private P0 alert; delivery failure never clears the block."""
    kinds = sorted({str(item.get("kind") or "UNKNOWN") for item in findings})
    locations = sorted({
        f"{item.get('symbol')}/{item.get('side')}"
        for item in findings if item.get("symbol") and item.get("side")
    })
    message = compact_text(
        f"[P0] {profile} 账本自愈阻断交易起棒；cycle={cycle_id}；"
        f"问题={','.join(kinds[:5])}；"
        f"位置={','.join(locations[:5]) or '未提供标的'}。"
        "请人工核验交易所保护单与账本，系统未下单。",
        480,
    )
    fingerprint = hashlib.sha256(json.dumps(
        {"profile": profile, "cycle": cycle_id,
         "kinds": kinds, "locations": locations},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()[:16]
    push_py = Path(__file__).parent.parent / "scripts" / "qq_push.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(push_py), "--alert", "--message", message,
             "--dedupe-key", f"autoheal-p0:{profile}:{cycle_id}:{fingerprint}",
             "--db-root", str(_resolve_db_root(db_root))],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            # qq_push 内部总预算封顶 55s；外层 60s 保留状态收尾时间。
            timeout=60, creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _check_tmp_stdlib_shadow(
    stage: str,
    cycle_id: str,
    db_root: str | os.PathLike | None = None,
) -> list[str]:
    """插入点 A0：起棒前查 tmp 有没有文件遮蔽标准库（2026-08-06）。

    trader 的当轮执行脚本按契约只能写项目 `tmp/`，Python 会把该目录放进
    `sys.path[0]`；tmp 里一旦有 `bisect.py` 之类调试残留，`order_executor` 的
    `import tempfile` 会直接 ImportError，**下一笔 OPEN/CLOSE 必炸**。

    **只告警不阻断**（同 2026-08-05 autoheal 拍板的边界）：HOLD 轮的回执走
    `collectors/trades_writer.py`（sys.path[0] 是 collectors/），不受影响；
    在派发层阻断会把本来能正常完成的 HOLD 轮一起杀掉，比问题本身更糟。
    """
    try:
        scripts_dir = _PROJECT_ROOT / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        from tmp_cleanup import find_stdlib_shadows

        names = [p.name for p in find_stdlib_shadows(_PROJECT_ROOT / "tmp")]
    except Exception as exc:            # 探测本身绝不拖垮起棒
        print(f"[trigger] WARN tmp 遮蔽探测失败（忽略）: {exc}", file=sys.stderr)
        return []
    if not names:
        return []
    print(f"[trigger] WARN tmp 下有文件遮蔽标准库 stage={stage} "
          f"cycle={cycle_id} files={','.join(names)}"
          "——本轮若产生成交，执行脚本会炸在 import；不阻断起棒",
          file=sys.stderr)
    _send_tmp_shadow_alert(stage, cycle_id, names, db_root)
    return names


def _send_tmp_shadow_alert(
    stage: str,
    cycle_id: str,
    names: list[str],
    db_root: str | os.PathLike | None = None,
) -> bool:
    """按遮蔽文件名集合去重的 P1 告警——同一批残留只吵一次，不是每轮一条。"""
    message = compact_text(
        f"[P1] {_PROJECT_ROOT.as_posix()}/tmp 下有 {len(names)} 个文件遮蔽标准库："
        f"{','.join(sorted(names)[:5])}；stage={stage} cycle={cycle_id}。"
        "trader 当轮执行脚本写在 tmp、sys.path[0] 即该目录，下一笔 OPEN/CLOSE "
        "会 ImportError（HOLD 轮不受影响）。处理：删除或改名这些文件，"
        "或跑 tmp_cleanup.py --apply。",
        480,
    )
    fingerprint = hashlib.sha256(
        json.dumps(sorted(names), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    push_py = Path(__file__).parent.parent / "scripts" / "qq_push.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(push_py), "--alert", "--message", message,
             "--dedupe-key", f"tmp-stdlib-shadow:{fingerprint}",
             "--db-root", str(_resolve_db_root(db_root))],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            # qq_push 内部总预算封顶 55s；外层 60s 保留状态收尾时间。
            timeout=60, creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _autoheal_ledger(
    stage: str,
    cycle_id: str,
    db_root: str | os.PathLike | None = None,
) -> dict:
    """插入点 A：起棒前只读检查账本（公开版永久不自动补账）。

    必须跑在 `_run_briefing()` **之前**——简报里的持仓视图喂给 Agent 决策，
    幽灵仓会让 Agent 基于「不存在的持仓」做判断（2026-08-04 SKHY 事故即如此），
    同时也早于 `order_executor` 的 pretrade 闸，顺带消除拒单冻结。

    确定性子进程、零 LLM。只有 v1 契约 rc=0（干净或安全写入完成）
    才能继续起棒；未解决、错误、跳过、P0 以及缺失/损坏/过期契约均阻断。
    `--self-cycle` 让本 stage 自己的 running runner 不被当成互斥冲突。
    """
    profile = stage if stage == "live" else None
    if not profile:
        return _autoheal_client_result(
            None, cycle_id, "not-applicable", status="not_applicable", rc=0,
            reason="stage does not use a trading ledger",
            finding_kind=None,
        )
    request_id = uuid.uuid4().hex
    resolved_db_root = _resolve_db_root(db_root)
    if os.environ.get("OKX_DISABLE_LEDGER_AUTOHEAL") == "1":
        return _autoheal_client_result(
            profile, cycle_id, request_id, status="disabled", rc=0,
            reason="OKX_DISABLE_LEDGER_AUTOHEAL=1", db_root=resolved_db_root,
            finding_kind=None,
        )
    try:
        heal_py = Path(__file__).parent.parent / "scripts" / "ledger_autoheal.py"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        out_json = LOG_DIR / (
            f"autoheal-{profile}-{_safe_cycle(cycle_id)}-{request_id}.json")
        out_json.unlink(missing_ok=True)
        cmd = [sys.executable, str(heal_py), "--profile", profile,
               "--db-root", str(resolved_db_root),
               "--self-cycle", cycle_id, "--request-id", request_id,
               "--json-out", str(out_json)]
        # Public-release boundary: never append --apply or
        # --enable-unrecorded, regardless of inherited environment values.
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, creationflags=_CREATE_NO_WINDOW,
        )
        result = _read_autoheal_contract(
            out_json, request_id=request_id, profile=profile,
            cycle_id=cycle_id, db_root=resolved_db_root,
            returncode=proc.returncode,
        )
    except Exception as exc:
        result = _autoheal_client_result(
            profile, cycle_id, request_id, status="client_error", rc=2,
            reason=f"{type(exc).__name__}: {exc}", db_root=resolved_db_root,
        )
    if result.get("p0"):
        p0_findings = [
            item for item in result.get("findings", [])
            if isinstance(item, dict)
            and str(item.get("sev") or "").upper() == "P0"
        ]
        result["alerted"] = _send_autoheal_p0_alert(
            profile, cycle_id, p0_findings, resolved_db_root)
        result["p0_kinds"] = sorted({
            str(item.get("kind") or "UNKNOWN") for item in p0_findings
        })
    result["json_out"] = str(out_json) if "out_json" in locals() else None
    return result


def _analyst_briefing(
    cycle_id: str,
    db_root: str | os.PathLike | None = None,
) -> str:
    """为 analyst 预读 decision_briefing 塞进 fire 消息，省去 analyst 临场摸库（降时延）。"""
    return _run_briefing(db_root)


def _briefing_for_traders(
    cycle_id: str,
    db_root: str | os.PathLike | None = None,
) -> str:
    """trader 预载简报——每 cycle 只真跑一次，走文件缓存（
    第二棒直接读缓存，避免 2×60s 最坏）。

    与 analyst 预载**刻意不共缓存**：analyst 简报生成于分析之前（无本轮 signals）；
    trader 派发时本轮 analysis 已落库，预载决策卡与历史正反样本，减少重复摸库
    。缓存 logs/trigger/briefing-<cycle>-trader.txt，
    随 log_rotate 每日轮转回收。全程 fail-safe：缓存读写失败照常直跑/直用。"""
    suffix = _root_namespace(db_root)
    tail = f"-{suffix}" if suffix else ""
    cache = LOG_DIR / f"briefing-{_safe_cycle(cycle_id)}-trader{tail}.txt"
    try:
        if cache.exists() and cache.stat().st_size > 0:
            return cache.read_text(encoding="utf-8")
    except OSError:
        pass
    brief = _run_briefing(db_root)
    if brief:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(brief, encoding="utf-8", newline="\n")
        except OSError:
            pass
    return brief


def _ro_db(
    name: str,
    db_root: str | os.PathLike | None = None,
) -> sqlite3.Connection | None:
    p = _resolve_db_root(db_root) / name
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con




# `_demo_swap_pool()` 随 2026-08-06 demo 全量下线移除：它整池取 Demo 环境 SWAP
# 合约供预载标注「池内有无」，只服务已删除的 ③.5 合约可用性块。


def _trader_preload(
    cycle_id: str,
    stage: str,
    db_root: str | os.PathLike | None = None,
) -> str:
    """live 触发消息预载块。

    dispatcher 起 trader 前已核过 analysis 就绪/新鲜/status=ok——把已证事实与分析内容
    直接塞进触发消息，消 trader 冷启动逐库自查（demo 失败调用 2.6x 于 live 的主源=
    开场摸库列名瞎猜）。每块独立 fail-safe：取不到→留显式缺块标记，trader 按 AGENTS.md
    自取兜底，绝不阻断派发。**OKX API 现仓/余额刻意不预载**——现仓唯一权威=交易所 API
    且随时变（SL 成交/手动平仓），预载快照会诱导 agent 跳过 API 真查，而
    risk_validator 的 open_positions 必须是下单现场真值（红线）。"""
    parts: list[str] = []
    # ①② 派发确认 + 分析预读（analysis.db ro；dispatcher 刚核过 status=ok 才会走到这）
    try:
        con = _ro_db("analysis.db", db_root)
        run = con.execute(
            "SELECT ts, status, mode, regime, regime_stale, market_summary, "
            "missing_sources FROM analysis_runs WHERE cycle_id=?",
            (cycle_id,)).fetchone()
        sigs = con.execute(
            "SELECT symbol, total, action, side, confidence, entry_hint, stop_hint, "
            "tp_hint, reasoning, decision_card FROM analysis_signals WHERE cycle_id=? "
            "ORDER BY CASE action WHEN 'close' THEN 0 WHEN 'reduce' THEN 0 "
            "WHEN 'adjust_protection' THEN 0 WHEN 'open_long' THEN 1 "
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
        parts.append("【分析预读缺块——按 AGENTS.md 的 DB_ACCESS 自查 analysis.db】")
    # ③ 账户参考（system_state 4 键）。
    # demo 分支（读 account_snapshots(profile='demo') 作资产/绩效展示）随
    # 2026-08-06 全量下线移除。
    try:
        con = _ro_db("account.db", db_root)
        rows = con.execute(
            "SELECT key, value, updated_utc FROM system_state WHERE key IN "
            "('live_totalEq','live_availBal','live_position_count',"
            "'last_live_account_check')").fetchall()
        con.close()
        if not rows:
            raise LookupError("no system_state keys")
        kv = "; ".join(f"{r['key']}={r['value']}(@{r['updated_utc']})" for r in rows)
        parts.append(f"【账户参考（system_state，仅参考——现仓/余额以 OKX API 为准）】\n  {kv}")
    except Exception:
        parts.append("【账户参考缺块——按 AGENTS.md 的 DB_ACCESS 自查 account.db】")
    # ③.5 Demo 合约可用性预载块随 2026-08-06 全量下线移除。它当年解决的是
    # 「analysis 按 live 行情选标的、Demo 池远小（实测 169 vs 400+）」导致 agent
    # 逐个试 API 烧预算的问题（2026-08-05T10:15 那轮烧光 720s 一条回执没写）。
    # ④ 决策简报（含历史正反样本与错失机会；每 cycle 一次）
    brief = _briefing_for_traders(cycle_id, db_root)
    if brief:
        parts.append("【决策简报（已预读，历史盈利/亏损/错失机会均为参考）】\n"
                     f"--- decision_briefing ---\n{brief}\n--- end ---")
    else:
        parts.append("【决策简报缺块——按 AGENTS.md 自跑 decision_briefing.py 兜底】")
    # ⑤ 必须自取项（防预载诱导偷懒）
    profile = "live"
    safe_cycle = cycle_id.replace(":", "-")
    runtime_db_root = _resolve_db_root(db_root).as_posix()
    tmp_root = (_PROJECT_ROOT / "tmp").as_posix()
    python_wrapper = (_PROJECT_ROOT / "scripts" / "run_okx_python.ps1").as_posix()
    facts_runner = (_PROJECT_ROOT / "scripts" / "live_decision_facts.py").as_posix()
    position_runner = (
        _PROJECT_ROOT / "scripts" / "live_position_action_runner.py"
    ).as_posix()
    facts_file = f"{tmp_root}/live_facts_{safe_cycle}.json"
    # demo 的 role_policy（max-size 容量口径）随 2026-08-06 全量下线移除。
    role_policy = (
        "Live OPEN/ADD 的组合保证金闸只认执行时同次 "
        "account.balance.imr/totalEq 与本单 incremental_order_imr，"
        f"预计成交后须≤{MAX_PORTFOLIO_IMR_RATIO:.1%}，超限整笔拒绝；"
        "mgnRatio/gross/net 不得替代，"
        "CLOSE/REDUCE 不受该闸影响。另有单笔增量保证金硬上限 "
        f"MAX_SINGLE_ORDER_IMR_RATIO={MAX_SINGLE_ORDER_IMR_RATIO:g}"
        f"（≤{MAX_SINGLE_ORDER_IMR_RATIO:.0%} 净值，定仓预算 "
        f"{MAX_SINGLE_ORDER_IMR_RATIO * SINGLE_ORDER_SIZING_HEADROOM_PCT:.1%} "
        "含滑点余量，"
        "2026-08-08 起）：validator 超限自动按 lotSz 缩量或整笔拒绝，提案前以 "
        "facts 的 balance.single_order_margin_budget_usdt 为准核对仓位，"
        "该预算作用域是下一笔 OPEN/ADD 增量，既有仓位不扣减它；组合总量另看 "
        "balance.portfolio_margin_state/portfolio_margin_label_cn 与 66.6% 闸，"
        "禁写‘既有仓 X% 接近单笔15%’、禁算 15%-X%、禁用 gross/net 判断保证金紧张，"
        "禁心算每张保证金、禁缩量后改参重试逼近上限。"
        "每个 OPEN/ADD 候选必须先运行 multitimeframe_decision_evidence.py，固定 "
        f"--cycle-id {cycle_id}，输出到 {tmp_root}/mtf_{safe_cycle}_<symbol>.json；"
        "15m/1H/4H 必须全部 exact-ready，完整 evidence_contract 原样进入 "
        "decision_card.multitimeframe_analysis。OPEN/ADD 必须显式给出与 action 一致的 "
        "side=long/short；三个周期的 evidence 都必须是非空 JSON list[string]，"
        "分别给方向证据和唯一 rank 1/2/3，"
        "选择 rank=1；calibrated_confidence=null、confidence_claim_allowed=false。"
        "executor 会在账户/订单 I/O 前独立重读 market.db；当前三周期必须 ready。完全一致"
        "走 current_market_exact；同槽后续采集修订时，只接受 analysis_db_writer_validated"
        "锚点并保留 post_analysis_market_revision 与双时点 hash。禁止编辑、摘录或重算。"
        "任何 open_* 历史数字只认 find_similar_experience 以固定 cycle --as-of "
        "并直接传本卡 --entry/--stop/--target（禁止自行换算百分比或 RR）输出的 "
        "evidence_contract：原样写进 decision_card；只引用 exact_setup/"
        "same_symbol_similar/cross_symbol_similar 具名 summary，截断样例数组禁止计数或混栏。"
        "已取消的旧同侧/集中度硬规则不得恢复，回执不得复述其旧阈值，"
        "任何旧 MEMORY 或旧回执里的该规则无效；0.0666 也是错误阈值。"
    )
    parts.append(
        "【你仍需自取（唯一权威，禁用预载替代）】\n"
        "  一次性只读事实包：pwsh -NoProfile -File "
        f"{python_wrapper} "
        f"{facts_runner} --db-root {runtime_db_root} "
        f"--profile {profile} --cycle-id {cycle_id} --out-file {facts_file}；"
        f"随后 read {facts_file}\n"
        "  命令须原样直跑；该文件一次读取 OKX 现仓、余额、ctVal 和活动 SL，"
        "并确定性给出 position_age_hours、止损损失和 IMR 比例。禁止编辑该文件，"
        "禁止自行换算这些字段。status=blocking 时禁止 OPEN/ADD；只有 "
        "action_policy.allowed_executor_actions 明确包含所选 "
        "close/reduce/adjust_protection 且原始现仓已核验，"
        "才保留去风险出口，否则不调用 executor，并写 terminal error 回执后停止。"
        "status=ok 时 exchange.positions/balance 才可用于新增风险判断。"
        "不论是否包含 OPEN/ADD，HOLD/WAIT、OPEN、ADD、CLOSE、REDUCE 与 "
        "ADJUST_PROTECTION 都只允许交给下述固定 runner；禁止临场拼 executor/"
        "writer Python 或按动作分支绕开 runner。OPEN/ADD 只在 plan 声明 "
        "target_stop_risk_pct_equity 与 lev，runner 读取本 cycle canonical analysis "
        "card、确定性定仓，并把逐笔 card 与完整 live_facts 绑定到回执。"
        "必须一次 write 完整 "
        f"{tmp_root}/position_plan_{safe_cycle}.json，落盘后立即且只调用一次 "
        "live_position_action_runner.py；plan 后 30s 机器闸要求出现合法 runner marker；"
        "actions=[] 即 HOLD，runner 不产生判断，只按你逐仓裁决执行、需要时先提交 "
        "runner_in_progress=true 的 interim superset，再同进程落唯一 final 回执。命令："
        "pwsh -NoProfile -File "
        f"{python_wrapper} {position_runner} "
        f"--cycle-id {cycle_id} --plan-file {tmp_root}/position_plan_{safe_cycle}.json "
        f"--facts-file {facts_file} --receipt-file "
        f"{tmp_root}/_receipt_live_{safe_cycle}.json --db-root {runtime_db_root}。"
        "batch_status=partial|failed 或非零退出即 terminal failure，禁止重跑或写 HOLD 覆盖。"
        "facts 文件读完后禁止再读 trades_writer.py 源码、探查 schema、搜索历史回执或研究实现；"
        "立即决定并生成完整回执；writer 返回前禁止最终答复、禁止无内容 stop。writer 返回 ok:true 后"
        "严禁 query_db、--help、--schema 或任何其他"
        "工具，立即给出简短最终答复；stage_runner 独立核验本 cycle trade_cycles，Agent 不得重复"
        "核验。零成交 HOLD 也必须先落库且只能由 writer 完成。"
        f"{role_policy}"
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


def build_cmd(
    stage: str,
    cycle_id: str,
    mode: str,
    db_root: str | os.PathLike | None = None,
) -> list[str]:
    cycle_id = validate_cycle_id(cycle_id)
    resolved_db_root = _resolve_db_root(db_root)
    agent = STAGE_AGENTS[stage]
    # 插入点 A0：tmp 遮蔽标准库探测（纯只读，只告警）。放在最前面：它与账本无关，
    # 失败也不该影响自愈/简报，而一旦命中就说明本轮任何成交都写不进去。
    _check_tmp_stdlib_shadow(stage, cycle_id, resolved_db_root)
    # 插入点 A：先自愈账本，再生成简报——顺序不可颠倒（简报要反映修好后的持仓）。
    # dry-run 走不到这里（fire 在 build_cmd 前就已返回），故干跑不会写库。
    autoheal = _autoheal_ledger(stage, cycle_id, resolved_db_root)
    if autoheal.get("blocking"):
        kinds = sorted({
            str(item.get("kind") or "UNKNOWN")
            for item in autoheal.get("findings", [])
            if isinstance(item, dict)
        })
        alert_state = (
            "alerted" if autoheal.get("alerted") is True
            else "alert_failed" if autoheal.get("p0") else "not_p0"
        )
        # **fail-safe：只告警不阻断**（主人 2026-08-05 拍板，事故后回退）。
        #
        # 曾经这里 raise 掉整个 stage。后果：demo 出现一个自愈范围内的幽灵仓 →
        # 每 2 分钟派发、每 2 分钟被挡、无人真正修 → demo 与依赖它的 **push 一起死锁
        # 2h14m**（2026-08-05 17:25→19:39）。症状（收不到推送）离根因（demo 账本幽灵仓）
        # 隔三层，且 60+ 次重试零告警。
        #
        # 为什么不该在派发层挡：真正的防线是 `order_executor` 的 pretrade 闸——它紧贴下单、
        # 用当场 API 现仓、只挡这一单。派发层阻断则连 push 一起杀，而 **push 是纯汇报、
        # 一分钱不碰**。对照：前一日 live 被 pretrade 闸冻结 9h 期间推送始终正常，
        # 系统保持可观测；本次派发层阻断直接让系统变哑。
        #
        # 所以：自愈尽力而为，修不成就让流程照常走，由 pretrade 闸 fail-closed 兜底。
        print(f"[trigger] WARN ledger_autoheal 未清干净但不阻断 "
              f"stage={stage} cycle={cycle_id} status={autoheal.get('status')} "
              f"rc={autoheal.get('rc')} findings={','.join(kinds) or 'UNKNOWN'} "
              f"alert={alert_state}（pretrade 闸仍会 fail-closed 兜底）",
              file=sys.stderr)
    # 触发消息只写"本轮工作"（stage/cycle/mode + analyst 的数据简报）；
    # 流程/红线/注意事项全在各 agent 的 AGENTS.md（OpenClaw 每轮自动加载），不再塞触发消息。
    if stage == "analyst":
        brief = _analyst_briefing(cycle_id, resolved_db_root)
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
        brief = _analyst_briefing(cycle_id, resolved_db_root)
        msg = _unified_live_message(
            cycle_id, brief, db_root=resolved_db_root
        )
    elif stage == "live":
        # trader 预载减少冷启动逐库自查；各块独立 fail-safe，缺块留标记回退自查。
        preload = _trader_preload(cycle_id, stage, resolved_db_root)
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
    key = session_key(stage, cycle_id, resolved_db_root)
    msg_file = _write_message_file(key, msg)
    msg_args = (["--message-file", str(msg_file)] if msg_file
                else ["--message", msg])
    timeout = (_unified_live_timeout_seconds(cycle_id)
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


def _fire_push_script(
    cycle_id: str,
    mode: str = "script",
    db_root: str | os.PathLike | None = None,
) -> str:
    """push stage 纯脚本路径：detached 起 push_pipeline.py
    （build→render→validate→qq_push→archive→system_state）。
    返回 session-key 作 card_id（与 agent 路径同签名，dispatcher._fire_stage 语义不变）。
    dry-run（OKX_TRIGGER_DRYRUN=1）只落命令日志不真起。python.exe 直起（原生 exe，
    DETACHED 存活；不经 pwsh wrapper——pwsh 跑 .ps1 在 DETACHED_PROCESS 下可能静默不执行；
    管道内部各步自走 wrapper 拿 UTF-8/MX_APIKEY）。
    起棒失败抛异常由 _fire_stage 释放闩锁重试。"""
    cycle_id = validate_cycle_id(cycle_id)
    resolved_db_root = _resolve_db_root(db_root)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    key = session_key("push", cycle_id, resolved_db_root)
    inner_cmd = [_PYTHON_EXE, _PUSH_PIPELINE, "--cycle", cycle_id,
                 "--db-root", str(resolved_db_root)]
    if mode == "failure_report":
        # push_pipeline 会独立重验未来激活边界、精确 cycle 身份、failed 终态
        # 与 profile 租约释放；这里只传递意图，不携带可伪造的失败详情。
        inner_cmd.append("--upstream-failure-report")
    cmd = _supervised_cmd(
        "push", cycle_id, "script", inner_cmd, db_root=resolved_db_root
    )
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
            creationflags=flags, cwd=_project_path(), close_fds=True)
        _probe_launch(proc, "push", cycle_id, fh, resolved_db_root)
    return key


def fire(
    stage: str,
    cycle_id: str,
    mode: str = "full",
    db_root: str | os.PathLike | None = None,
) -> str:
    """detached 拉起 Agent stage，或对 push 无条件起纯脚本管道。

    返回 session-key（作 card_id 用）。

    dry-run（OKX_TRIGGER_DRYRUN=1）：不真起，只把命令落日志——tmp 验证 plumbing 用。
    启动失败（如 openclaw 不在 PATH）会抛 FileNotFoundError，由 dispatcher._fire_stage
    捕获后释放 stage 闩锁，下一 tick 重试。
    """
    cycle_id = validate_cycle_id(cycle_id)
    if stage == "push":
        if mode in {"full", "script"}:
            return _fire_push_script(cycle_id, db_root=db_root)
        return _fire_push_script(cycle_id, mode, db_root=db_root)
    if stage not in STAGE_AGENTS:
        raise ValueError(f"unknown stage: {stage}")
    resolved_db_root = _resolve_db_root(db_root)
    dry = os.environ.get("OKX_TRIGGER_DRYRUN") == "1"
    if not dry and os.path.normcase(os.fspath(resolved_db_root)) != os.path.normcase(
        os.fspath(_CANONICAL_DB_ROOT)
    ):
        raise RuntimeError(
            "non-default db_root is supported for Agent stages only with "
            "OKX_TRIGGER_DRYRUN=1; Gateway tool DB-root propagation is not guaranteed"
        )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    key = session_key(stage, cycle_id, resolved_db_root)
    logf = LOG_DIR / f"{key}.log"

    if dry:
        # dry 判定必须在 build_cmd 之前，避免启动 decision_briefing 子进程或写
        # msg/briefing 文件。dry 只验 plumbing：落意图日志即返回，
        # 不组消息、不起任何子进程、不写消息/简报文件。
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write(f"\n[{now_cst()}] stage={stage} cycle={cycle_id} mode={mode} "
                     f"agent={STAGE_AGENTS[stage]} dry=True\n")
            fh.write("  (dry-run: 未组消息/未真起 agent)\n")
        return key
    inner_cmd = build_cmd(stage, cycle_id, mode, db_root=resolved_db_root)
    cmd = _supervised_cmd(
        stage, cycle_id, mode, inner_cmd, db_root=resolved_db_root
    )
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
            cwd=_project_path(),
            close_fds=True,
        )
        _probe_launch(proc, stage, cycle_id, fh, resolved_db_root)
    return key


def main() -> int:
    ap = argparse.ArgumentParser(description="agent 起棒适配层（唯一 caller=core/dispatcher.py；人工排障可 CLI 调）")
    ap.add_argument("--stage", required=True, choices=sorted((*STAGE_AGENTS, "push")))
    ap.add_argument("--cycle", required=True, help="cycle_id 如 2026-06-18T14:00")
    ap.add_argument("--mode", default="full", choices=["full", "unified"])
    ap.add_argument("--db-root", default=_project_path("db"))
    args = ap.parse_args()
    if args.mode == "unified" and args.stage != "live":
        ap.error("mode=unified 仅适用于 stage=live")
    key = fire(args.stage, args.cycle, args.mode, db_root=args.db_root)
    print(f"fired stage={args.stage} cycle={args.cycle} -> session-key={key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
